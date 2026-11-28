"""Suggest crosswalk entries between two sets of tables from what their columns mean.

``suggest_equivalences(pipeline, left, right)`` compares every table of one set with every table of the other
through their table profiles (``table_profile``): each output column's meaning says which source column it
passes on (``col:``), which aggregate it computes (``agg:``) or which expression it is (``expr:``), whatever
the column is called. Column pairs are matched one to one, strongest first:

1. the same meaning: the same source column (``same_source_column``), the same aggregate (``same_aggregate``)
   or the same normalized expression (``same_expression``);
2. the same aggregate function over the same source columns, through a different expression (``same_aggregate``);
3. the same source columns through a different transformation, such as ``ts`` and ``DATE(ts)``
   (``same_source_column``; never an aggregate with a column that is not one);
4. the same expression over different source columns (``same_expression``).

A shared column name adds a little to a pair that matched on meaning, and pairs two otherwise unmatched columns
only as ``same_name`` (worth nothing to the table's score): a name alone never makes a suggestion. Saved
``equal`` declarations are applied to the meanings first, so a source declared equal to another reads as that
source and the tables built on the two match. An aggregate matches fully only at the same grain.

A table pair's score (0 to 1) is how much of both tables the matched pairs cover, plus a little when the grains
and the row filters agree. Suggestions are ranked by score, then by name, so the order is the same on every run.

Nothing is saved. Each suggestion carries the ``python -m kumosql equivalence add`` command that would record
it, for a person to check and run. The same meaning row by row is not the same rows, so it proposes ``equal``
only for pairs with the same meaning in tables whose grains and row filters are both known to agree, and
otherwise ``--status differs``, a record that never enters a proof; pairs matched on anything weaker than the
same meaning always get ``--status differs``, in a hint of their own. Columns left unmatched on either side are
listed (``left_only_columns``, ``right_only_columns``) as candidates for ``--status missing``; a column whose
meaning is unknown is listed apart, since nothing can be concluded about it. Declarations act on source columns:
one between two models changes no meaning, because a meaning names the sources a model reads, not the model.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from . import equivalences
from .equivalences import Equivalence, names_table
from .table_profile import COLUMN_MEANING, SOURCE_COLUMN, TableProfile, profile_pipeline

__all__ = ["select_tables", "suggest_equivalences"]

DEFAULT_TOP = 20
DEFAULT_MIN_SCORE = 0.3

_SOURCE = re.compile(SOURCE_COLUMN)
_ANY_COLUMN = re.compile(COLUMN_MEANING)
_FUNCTION = re.compile(r"agg:([A-Za-z_][A-Za-z0-9_]*)\(")
_TIERS = ("exact", "aggregate", "sources", "shape", "name")
_EXACT_SCORE, _AGGREGATE_SCORE, _SOURCES_SCORE, _SHAPE_SCORE, _NAME_SCORE = 0.9, 0.7, 0.5, 0.35, 0.15
_NAME_BONUS = 0.1
_COMMAND = "python -m kumosql equivalence add"


# ----------------------------------------------------------------- selection


def select_tables(pipeline, specs: Iterable[str]) -> tuple[list[str], list[dict]]:
    """The model keys a list of specs names, and how each spec matched.

    Each spec (comma-separated specs are split) is tried as, in order: a glob (``*``, ``?`` or ``[``) over the
    key, the table name and the file path; a table (its key, or its last dotted parts: ``orders``,
    ``raw.orders``); a dataset (``mart`` or ``proj.mart``); a folder of the project (``definitions/old``); a
    prefix of the table name or key (``stg_``). The first that matches a model wins. A spec that matches nothing
    raises ``ValueError``. Assertions and operations without an output are never selected.
    """

    tokens = [part.strip() for spec in specs for part in str(spec).split(",") if part.strip()]
    if not tokens:
        raise ValueError("name at least one table, dataset, folder, prefix or glob")
    chosen: list[str] = []
    how: list[dict] = []
    for token in tokens:
        keys, rule = _select(pipeline, token)
        if not keys:
            raise ValueError(f"{token} matches no table of the project (give a table, a dataset, a folder, a prefix or a glob)")
        how.append({"spec": token, "matched_by": rule, "tables": len(keys)})
        chosen += [key for key in keys if key not in chosen]
    return chosen, how


def _selectable(model) -> bool:
    return model.kind != "assertion" and (model.kind != "operations" or model.has_output)


def _select(pipeline, token: str) -> tuple[list[str], str]:
    text = token.strip().strip("`").lower().replace("\\", "/")
    models = [(key, pipeline.models[key]) for key in sorted(pipeline.models) if _selectable(pipeline.models[key])]

    def path(model) -> str:
        return (model.path or "").lower().replace("\\", "/")

    if any(c in text for c in "*?["):
        return [key for key, model in models if any(
            fnmatch.fnmatchcase(value, text) for value in (key.lower(), model.target.name.lower(), path(model)) if value
        )], "glob"
    tables = [key for key, _ in models if names_table(text, key) and len(text.split(".")) <= len(key.split("."))]
    if tables:
        return tables, "table"
    datasets = [key for key, model in models if text in (
        model.target.schema.lower(), f"{model.target.database}.{model.target.schema}".lower()
    )]
    if datasets:
        return datasets, "dataset"
    folder = text.strip("/")
    folders = [key for key, model in models if folder and (
        path(model).startswith(folder + "/") or f"/{folder}/" in "/" + path(model)
    )]
    if folders:
        return folders, "folder"
    return [key for key, model in models if model.target.name.lower().startswith(text) or key.lower().startswith(text)], "prefix"


# ------------------------------------------------------------------ features


@dataclass(frozen=True)
class _Column:
    name: str
    meaning: str | None  # as profiled
    canonical: str | None  # with saved equal declarations applied
    family: str  # column, aggregate, expression, constant or unknown
    sources: frozenset[str]
    function: str | None  # an aggregate's function
    shape: str | None  # the canonical meaning with every column replaced, for expressions and aggregates
    declared: bool  # a saved declaration changed the meaning
    reason: str | None  # why the meaning is unknown


class _Canon:
    """Rewrites source columns through the saved ``equal`` declarations: ``right.y`` reads as ``left.x``."""

    def __init__(self, items: Sequence[Equivalence], tables: set[str]):
        self.items = [item for item in items if item.status == "equal"]
        self.tables = tables

    def _full(self, name: str) -> str:
        hits = [table for table in self.tables if names_table(name, table)]
        return hits[0] if len(hits) == 1 else name

    def _split(self, text: str) -> tuple[str, str, list[str]]:
        """``(table, column, field path)``: a nested field's source is ``table.column.a.b``."""

        parts = text.split(".")
        at = next((i for i in range(len(parts) - 1, 0, -1) if ".".join(parts[:i]) in self.tables), len(parts) - 1)
        return ".".join(parts[:at]), parts[at], parts[at + 1:]

    def source(self, text: str) -> str:
        """``table.column`` (or ``table.column.field``) after following the declarations, one hop per declaration."""

        for _ in range(len(self.items)):
            table, column, path = self._split(text)
            hits = [item for item in self.items if table and names_table(table, item.right)]
            if len(hits) != 1:
                return text  # no declaration, or an ambiguous spelling that says nothing certain
            item = hits[0]
            mapped = {b: a for a, b in item.columns}
            listed = {a for a, _ in item.columns}
            if column in mapped:
                column = mapped[column]
            elif not item.whole or column in listed:
                return text  # the declaration says nothing about this column
            moved = ".".join([self._full(item.left), column, *path])
            if moved == text:
                return text
            text = moved
        return text

    def meaning(self, text: str) -> str:
        return _SOURCE.sub(lambda m: "col:" + self.source(m.group(0)[4:]), text) if self.items else text


def _family(meaning: str | None) -> str:
    if meaning is None:
        return "unknown"
    if meaning.startswith(("col:", "field(")):
        return "column"
    if meaning.startswith("agg:"):
        return "aggregate"
    if meaning.startswith("const:"):
        return "constant"
    return "expression"


def _columns(profile: TableProfile, canon: _Canon) -> list[_Column]:
    found = []
    for attr in profile.attributes:
        meaning = attr.meaning if attr.status == "known" else None
        canonical = canon.meaning(meaning) if meaning is not None else None
        family = _family(meaning)
        shape = None
        if family in ("aggregate", "expression") and canonical is not None:
            shape = _ANY_COLUMN.sub("col:?", canonical)
            shape = shape if "col:?" in shape else None
        function = _FUNCTION.match(canonical) if family == "aggregate" and canonical else None
        found.append(_Column(
            attr.column, meaning, canonical, family, frozenset(canon.source(s) for s in attr.sources),
            function.group(1).upper() if function else None, shape, canonical != meaning,
            None if meaning is not None else (attr.reason or "unknown"),
        ))
    return found


def _grain(profile: TableProfile, columns: list[_Column]) -> frozenset[str] | None:
    if not profile.grain.known:
        return None
    by_name = {c.name.lower(): c for c in columns}
    meanings = [by_name.get(key.lower()) for key in profile.grain.keys]
    if any(c is None or c.canonical is None for c in meanings):
        return None
    return frozenset(c.canonical for c in meanings)


def _check(left, right) -> str:
    if left is None or right is None:
        return "unknown"
    return "matched" if left == right else "differs"


def _scope(profile: TableProfile, canon: _Canon) -> frozenset[str] | None:
    if not profile.row_scope.comparable:
        return None
    return frozenset(canon.meaning(f) for f in profile.row_scope.filters)


# ------------------------------------------------------------------- scoring


def _pair(left: _Column, right: _Column, grain: str) -> tuple[float, str, str, list[str]] | None:
    """``(score, tier, match_kind, reasons)`` for two columns, or ``None`` when nothing links them."""

    same_name = left.name.lower() == right.name.lower()
    comparable = {"unknown", "constant"}.isdisjoint((left.family, right.family))
    if not comparable or left.canonical is None or right.canonical is None:
        if not same_name:
            return None
        if "unknown" in (left.family, right.family):
            return _NAME_SCORE, "name", "same_name", ["same name; the meaning of one of them is unknown"]
        return _NAME_SCORE, "name", "same_name", ["same name; a constant is matched by name only"]
    if left.canonical == right.canonical:
        kind = {"column": "same_source_column", "aggregate": "same_aggregate"}.get(left.family, "same_expression")
        score, tier, reasons = _EXACT_SCORE, "exact", ["same meaning"]
    elif (left.family == right.family == "aggregate" and left.function and left.function == right.function
          and left.sources and left.sources == right.sources):
        score, tier, kind = _AGGREGATE_SCORE, "aggregate", "same_aggregate"
        reasons = [f"{left.function} over the same source columns, through a different expression"]
    elif (left.sources and right.sources and (left.family == "aggregate") == (right.family == "aggregate")
          and not left.family == right.family == "column"  # two fields of one record column are two values
          and len(left.sources & right.sources) * 2 >= len(left.sources | right.sources)):
        shared, union = len(left.sources & right.sources), len(left.sources | right.sources)
        score, tier, kind = _SOURCES_SCORE * shared / union, "sources", "same_source_column"
        reasons = [f"shares {shared} of {union} source columns, through a different transformation"]
    elif left.shape is not None and left.shape == right.shape:
        score, tier, kind = _SHAPE_SCORE, "shape", "same_expression"
        reasons = ["same expression over different source columns"]
    elif same_name:
        return _NAME_SCORE, "name", "same_name", ["same name, different meaning: the values may differ"]
    else:
        return None
    if same_name:
        score += _NAME_BONUS
        reasons.append("same name")
    if left.declared or right.declared:
        reasons.append("matched through a saved equal declaration")
    if "aggregate" in (left.family, right.family) and grain != "matched":
        score *= 0.6 if grain == "differs" else 0.85
        reasons.append("the tables have different grains, so the aggregates cover different groups" if grain == "differs"
                       else "the grain of one table is unknown, so the aggregates may cover different groups")
    return round(score, 3), tier, kind, reasons


def _assign(lefts: list[_Column], rights: list[_Column], grain: str) -> list[dict]:
    """The column pairs, one to one, strongest first."""

    candidates = []
    for i, left in enumerate(lefts):
        for j, right in enumerate(rights):
            found = _pair(left, right, grain)
            if found is not None:
                score, tier, kind, reasons = found
                candidates.append((-score, _TIERS.index(tier), left.name.lower(), right.name.lower(), i, j, tier, kind, reasons, score))
    candidates.sort(key=lambda c: c[:4])
    used_left, used_right, pairs = set(), set(), []
    for *_, i, j, tier, kind, reasons, score in candidates:
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        pairs.append({
            "left_column": lefts[i].name, "right_column": rights[j].name, "score": score, "match_kind": kind,
            "exact": tier == "exact", "left_meaning": lefts[i].meaning, "right_meaning": rights[j].meaning,
            "reasons": reasons,
        })
    return pairs


def _features(columns: list[_Column]) -> set[str]:
    found = set()
    for column in columns:
        if column.family in ("unknown", "constant"):
            continue
        found.add("m:" + column.canonical)
        found.update("s:" + source for source in column.sources)
        if column.shape:
            found.add("p:" + column.shape)
    return found


def _hint(left: str, right: str, pairs: list[dict], status: str) -> str:
    words = [left, right, *(f"{p['left_column']}={p['right_column']}" for p in pairs)]
    if status != "equal":
        words += ["--status", status]
    return " ".join([_COMMAND, *(shlex.quote(w) for w in words)])


def _declared(items: Sequence[Equivalence], left: str, right: str) -> list[str]:
    return [item.label for item in items if (
        (names_table(left, item.left) and names_table(right, item.right))
        or (names_table(right, item.left) and names_table(left, item.right))
    )]


# ---------------------------------------------------------------- public API


def suggest_equivalences(
    pipeline,
    left_tables: Sequence[str],
    right_tables: Sequence[str],
    *,
    items: Sequence[Equivalence] | None = None,
    profiles: Mapping[str, TableProfile] | None = None,
    top: int | None = DEFAULT_TOP,
    min_score: float = DEFAULT_MIN_SCORE,
) -> dict:
    """Ranked candidate pairs between two sets of models of ``pipeline``; reads only, and never saves anything.

    ``left_tables`` and ``right_tables`` are model keys (see ``select_tables``). ``items`` are the saved entries
    (default: every entry in the data folder); their ``equal`` declarations are applied to the meanings and any
    entry naming a pair marks it ``already_declared``. ``profiles`` defaults to ``profile_pipeline(pipeline)``.
    Pairs scoring under ``min_score`` are counted, not listed; ``top`` caps the list (``None`` for all).

    Returns ``{suggestions, shown, total, compared, skipped, left_tables, right_tables, note}``; each suggestion
    has ``left_table``, ``right_table``, ``score``, ``checks`` (``grain`` and ``row_scope``: ``matched``,
    ``differs`` or ``unknown``), ``matched_columns`` (``left_column``, ``right_column``, ``score``,
    ``match_kind``, ``exact``, ``left_meaning``, ``right_meaning``, ``reasons``), ``left_only_columns``,
    ``right_only_columns``, ``unknown_meaning`` (per side, each such column with the profile's reason),
    ``already_declared``, ``declared``, ``command_hint``, ``differs_hint`` and ``review``.
    """

    items = list(equivalences.load_all() if items is None else items)
    profiles = profile_pipeline(pipeline) if profiles is None else profiles
    lefts = [key for key in dict.fromkeys(left_tables)]
    rights = [key for key in dict.fromkeys(right_tables)]
    for key in (*lefts, *rights):
        if key not in pipeline.models:
            raise ValueError(f"{key} is not a model of the loaded project")
    # the source tables, from the meanings: ``col:<table>.<column>`` never carries a field path, ``sources`` may
    tables = {found[4:].rpartition(".")[0] for profile in profiles.values() for attr in profile.attributes
              if attr.meaning for found in _SOURCE.findall(attr.meaning)}
    canon = _Canon(items, tables)

    prepared: dict[str, tuple[list[_Column], frozenset[str] | None, frozenset[str] | None, set[str]]] = {}
    skipped: dict[str, int] = {}
    for key in {*lefts, *rights}:
        profile = profiles.get(key)
        if profile is None or not profile.attributes:
            continue
        columns = _columns(profile, canon)
        prepared[key] = (columns, _grain(profile, columns), _scope(profile, canon), _features(columns))

    suggestions: list[dict] = []
    compared = below = 0
    order = {key: i for i, key in enumerate(lefts)}
    both = set(lefts) & set(rights)
    for left in lefts:
        for right in rights:
            if left == right or (left in both and right in both and order[right] < order[left]):
                continue  # a table with itself, or a pair of tables in both sets that was compared the other way
            if left not in prepared or right not in prepared:
                skipped["no_columns_known"] = skipped.get("no_columns_known", 0) + 1
                continue
            l_columns, l_grain, l_scope, l_features = prepared[left]
            r_columns, r_grain, r_scope, r_features = prepared[right]
            if not l_features & r_features:
                skipped["nothing_in_common"] = skipped.get("nothing_in_common", 0) + 1
                continue
            compared += 1
            grain, scope = _check(l_grain, r_grain), _check(l_scope, r_scope)
            pairs = _assign(l_columns, r_columns, grain)
            cover = sum(p["score"] for p in pairs if p["match_kind"] != "same_name")
            score = 0.9 * 2 * cover / (len(l_columns) + len(r_columns))
            score = round(min(1.0, score + 0.05 * (grain == "matched") + 0.05 * (scope == "matched")), 3)
            if score < min_score:
                below += 1
                continue
            suggestions.append(_suggestion(left, right, score, grain, scope, pairs, l_columns, r_columns, items))
    suggestions.sort(key=lambda s: (-s["score"], s["left_table"], s["right_table"]))
    total = len(suggestions)
    shown = suggestions if top is None else suggestions[:max(0, top)]
    if below:
        skipped["below_min_score"] = below
    return {
        "suggestions": shown, "shown": len(shown), "total": total, "compared": compared,
        "skipped": dict(sorted(skipped.items())), "left_tables": lefts, "right_tables": rights,
        "note": "nothing was saved. An equal declaration becomes an assumption of every proof that reads the right table: "
                "check a suggestion before running its command_hint",
    }


def _suggestion(left, right, score, grain, scope, pairs, l_columns, r_columns, items) -> dict:
    paired_left = {p["left_column"] for p in pairs if p["match_kind"] != "same_name"}
    paired_right = {p["right_column"] for p in pairs if p["match_kind"] != "same_name"}
    unknown_left = {c.name: c.reason for c in l_columns if c.family == "unknown"}
    unknown_right = {c.name: c.reason for c in r_columns if c.family == "unknown"}
    exact = [p for p in pairs if p["exact"]]
    partial = [p for p in pairs if not p["exact"] and p["match_kind"] != "same_name"]
    review: list[str] = []
    status = "equal"
    if exact and (scope != "matched" or grain != "matched"):
        # the same meaning per row is not the same rows: equal needs the same grain and the same filters
        status = "differs"
        why = []
        if grain != "matched":
            why.append("the tables have different grains" if grain == "differs" else "the grain of a table is unknown")
        if scope != "matched":
            why.append("their row filters differ" if scope == "differs" else "their row filters cannot be compared")
        review.append(" and ".join(why) + ", so the hint records a difference: if you check that the rows match, "
                      "drop --status differs to declare them equal")
    # the columns that read their sources differently stay in a hint of their own, so that dropping --status differs
    # from the first hint never declares a transformed column equal
    command = _hint(left, right, exact, status) if exact else None
    differs = _hint(left, right, partial, "differs") if partial else None
    if command is None:
        command, differs = differs, None
    if partial:
        review.append("columns matched on less than the same meaning are proposed as differs: check how their values differ")
    left_only = [c.name for c in l_columns if c.name not in paired_left and c.family != "unknown"]
    right_only = [c.name for c in r_columns if c.name not in paired_right and c.family != "unknown"]
    if left_only or right_only:
        review.append("an unmatched column may have no counterpart: record it with --status missing after checking")
    if unknown_left or unknown_right:
        review.append("columns with an unknown meaning could not be matched, so nothing is concluded about them")
    declared = _declared(items, left, right)
    return {
        "left_table": left, "right_table": right, "score": score,
        "checks": {"grain": grain, "row_scope": scope},
        "matched_columns": pairs,
        "left_only_columns": left_only, "right_only_columns": right_only,
        "unknown_meaning": {"left": unknown_left, "right": unknown_right},
        "already_declared": bool(declared), "declared": declared,
        "command_hint": command, "differs_hint": differs, "review": review,
    }
