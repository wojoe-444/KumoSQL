"""Find existing tables that already hold an attribute at a finer grain.

When a proposed query (or model) aggregates an attribute to a coarser grain, for
example from city to state, an existing table may already hold the same
attribute at a finer grain. This module reports such tables and says whether
the coarser result can be computed from them. It builds on ``TableProfile``
(grain, attribute meaning, row scope) and the ``Check``/``MatchRole`` shapes of
``find_overlaps``.

Derivability, per target attribute and candidate table:

* ``derivable_exact``: every aggregate in the attribute decomposes (sum, count,
  min, max, and sums of products, or an average or ratio whose parts are all
  held), and the finer table's grain covers every coarser key, directly or
  through a known many-to-one mapping.
* ``derivable_with_conditions``: decomposes only if something further holds: an
  average needs the count and the sum, a ratio needs both parts (the missing
  pieces are named), or the mapping between grains changes over time.
* ``not_derivable``: distinct aggregates (a value can recur across finer groups),
  medians, percentiles and any other aggregate that needs the raw rows.
  Informational: the table still shows where the raw data lives.
* ``unknown``: the mapping between the two grains is not known, or the row
  scope of the finer table cannot be lined up with the target's. The missing
  piece is named.

Every result carries the ``conditions`` that must hold and the ``missing``
pieces. A table that holds the attribute at the same grain is an overlap, not a
roll-up, and a coarser table cannot produce a finer result; both are ruled out.
Reports describe behavior only (no query text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from sqlglot import exp, parse

from .overlap import Check, MatchRole
from .pipeline import Pipeline
from .scopes import Scope
from .table_profile import COLUMN_MEANING, AttributeMeaning, TableProfile, profile_pipeline, profile_query
from .table_roles import TableRole, infer_roles

__all__ = ["GrainMapping", "Rollup", "RollupResult", "find_rollups"]

_ORDER = {"derivable_exact": 0, "derivable_with_conditions": 1, "not_derivable": 2, "unknown": 3}
_DECOMPOSABLE = {"SUM", "COUNT", "MIN", "MAX", "LOGICAL_AND", "LOGICAL_OR"}
_COL = re.compile(COLUMN_MEANING)
_AGG = re.compile(r"agg:([A-Z_0-9]+)\(")
_JOIN = "join:"


@dataclass(frozen=True)
class GrainMapping:
    """A relationship from a finer key to a coarser one.

    ``child`` and ``parent`` are column meanings as they appear in a profile
    (``col:<table>.<column>``). ``changes_over_time`` and ``complete`` state what
    the caller knows about the relationship; ``None`` means not verified.
    Mappings found in the pipeline (a model whose grain is the child key and that
    holds the parent) are many-to-one by construction and leave both ``None``.
    """

    child: str
    parent: str
    many_to_one: bool = True
    changes_over_time: bool | None = None
    complete: bool | None = None


@dataclass(frozen=True)
class Rollup:
    table: str
    #: the target column this result is about; None when the whole table is undecided
    attribute: str | None
    derivability: str  # derivable_exact, derivable_with_conditions, not_derivable or unknown
    #: aggregate functions in the target attribute, for example ("SUM",)
    aggregate: tuple[str, ...] = ()
    #: the finer table's columns the result would be computed from
    columns: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    role: MatchRole | None = None
    reason: str = ""

    def to_json(self) -> dict:
        return {
            "table": self.table,
            "attribute": self.attribute,
            "derivability": self.derivability,
            "aggregate": list(self.aggregate),
            "columns": list(self.columns),
            "conditions": list(self.conditions),
            "missing": list(self.missing),
            "checks": [c.to_json() for c in self.checks],
            "role": self.role.to_json() if self.role else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RollupResult:
    """Roll-up results plus the coverage that gives them meaning (see ``OverlapResult``)."""

    target: TableProfile
    rollups: tuple[Rollup, ...] = ()
    compared: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict)
    candidates_in_scope: int = 0

    @property
    def summary(self) -> str:
        skipped = sum(self.skipped.values())
        reasons = ", ".join(f"{reason} {count}" for reason, count in sorted(self.skipped.items()))
        text = f"compared {self.compared} of {self.compared + skipped} tables; {skipped} skipped"
        if reasons:
            text += f": {reasons}"
        counts: dict[str, int] = {}
        for item in self.rollups:
            counts[item.derivability] = counts.get(item.derivability, 0) + 1
        if counts:
            text += ". Finer-grain sources: " + ", ".join(f"{name} {counts[name]}" for name in _ORDER if name in counts)
        else:
            text += ". No finer-grain source among the compared tables"
        return text

    def to_json(self) -> dict:
        return {
            "summary": self.summary,
            "target": self.target.to_json(),
            "rollups": [r.to_json() for r in self.rollups],
            "compared": self.compared,
            "skipped": dict(sorted(self.skipped.items())),
            "candidates_in_scope": self.candidates_in_scope,
        }


# ------------------------------------------------------------------ public API


def find_rollups(
    pipeline: Pipeline,
    sql: str | None = None,
    *,
    model: str | None = None,
    scope: Scope | None = None,
    roles: Mapping[str, TableRole] | None = None,
    declared_grain: Mapping[str, Sequence[str]] | None = None,
    mappings: Sequence[GrainMapping] = (),
) -> RollupResult:
    """Existing tables that hold the aggregated attributes of ``sql`` (or ``model``) at a finer grain.

    Arguments are as for ``find_overlaps``, plus ``mappings``: many-to-one
    relationships between keys that the pipeline cannot show (a dimension that
    is a source, or one whose time behavior you know). Mappings are also found
    in the pipeline itself: a model whose grain is one key and that holds the
    other. Never raises on odd profiles.
    """

    if (sql is None) == (model is None):
        raise ValueError("give exactly one of sql and model")
    profiles = profile_pipeline(pipeline, declared_grain=declared_grain)
    if sql is not None:
        target_key = None
        target = profile_query(pipeline, sql, declared_grain=declared_grain)
    else:
        text = str(model)
        try:
            target_key = pipeline.resolve(text) or text
        except Exception:  # noqa: BLE001
            target_key = text
        if target_key not in profiles:
            raise ValueError("the model is not in the pipeline")
        target = profiles[target_key]
    if roles is None:
        try:
            roles = infer_roles(pipeline)
        except Exception:  # noqa: BLE001
            roles = {}
    known = _mappings(profiles, mappings)
    joins = _JoinCache(pipeline)

    results: list[Rollup] = []
    skipped: dict[str, int] = {}
    compared = in_scope = 0
    keep = pipeline.scope_keys(scope, profiles) if scope is not None else None
    for key in sorted(pipeline.models):
        if key == target_key:
            continue
        if keep is not None and key not in keep:
            skipped["outside_scope"] = skipped.get("outside_scope", 0) + 1
            continue
        in_scope += 1
        try:
            found = _compare(target, profiles[key], key, known, joins)
        except Exception as exc:  # noqa: BLE001 - never raise on odd profiles
            found = [Rollup(key, None, "unknown", reason=f"compare_error: {type(exc).__name__}")]
        role = roles.get(key)
        role_info = MatchRole(role.role, role.confidence) if role is not None else None
        undecided = [r for r in found if r.attribute is None]
        if undecided:
            reason = undecided[0].reason.split(":")[0]
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            compared += 1
        results.extend(_with_role(item, role_info) for item in found)
    results.sort(key=lambda r: (_ORDER[r.derivability], r.table, r.attribute or ""))
    return RollupResult(target, tuple(results), compared, skipped, in_scope)


# ------------------------------------------------------------------- internals


def _with_role(item: Rollup, role: MatchRole | None) -> Rollup:
    return Rollup(item.table, item.attribute, item.derivability, item.aggregate, item.columns,
                  item.conditions, item.missing, item.checks, role, item.reason)  # fmt: skip


def _short(meaning: str) -> str:
    """The column part of a ``col:`` meaning, without the table (a field keeps its path: ``column.a.b``)."""

    field = re.fullmatch(r"field\((.+), ([a-z0-9_.]+)\)", meaning)
    if field is not None:
        return f"{_short(field.group(1))}.{field.group(2)}"
    return meaning.rsplit(".", 1)[-1]


def _split_calls(meaning: str) -> list[tuple[str, str]]:
    """Every ``agg:FN(args)`` in a meaning as (FN, args); args are balanced-paren text."""

    calls = []
    for found in _AGG.finditer(meaning):
        depth, i = 1, found.end()
        while i < len(meaning) and depth:
            depth += {"(": 1, ")": -1}.get(meaning[i], 0)
            i += 1
        if depth == 0:
            calls.append((found.group(1), meaning[found.end() : i - 1]))
    return calls


def _mappings(profiles: Mapping[str, TableProfile], given: Sequence[GrainMapping]) -> list[GrainMapping]:
    found = list(given)
    for profile in profiles.values():
        if len(profile.grain.keys) != 1 or not profile.grain.known:
            continue
        key_attr = profile.attribute(profile.grain.keys[0])
        if key_attr is None or key_attr.status != "known" or not (key_attr.meaning or "").startswith("col:"):
            continue
        for attr in profile.attributes:
            if attr.status == "known" and (attr.meaning or "").startswith("col:") and attr.meaning != key_attr.meaning:
                found.append(GrainMapping(key_attr.meaning, attr.meaning))
    return found


_TRUNCATE = re.compile(
    r"expr:(?:DATE|DATETIME|TIMESTAMP)_TRUNC\((?P<inner>.+), "
    r"(?:YEAR|ISOYEAR|QUARTER|MONTH|WEEK(?:\([A-Z]+\))?|ISOWEEK|DAY|HOUR|MINUTE)\)"
)


def _truncation(parent: str, child: str) -> GrainMapping | None:
    """A calendar truncation of a held value is a many-to-one, unchanging, total mapping."""

    found = _TRUNCATE.fullmatch(parent)
    if found is None or child == parent:
        return None
    inner = child[len("expr:"):] if child.startswith("expr:") else child
    if found.group("inner") != inner:
        return None
    return GrainMapping(child, parent, True, False, True)


class _JoinCache:
    """Whether a model, or anything it reads, joins tables (rows may repeat)."""

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self._own: dict[str, bool] = {}
        self._upstream: dict | None = None

    def _joins(self, key: str) -> bool:
        if key not in self._own:
            try:
                trees = parse(self.pipeline.models[key].sql, read="bigquery")
                self._own[key] = any(t is not None and t.find(exp.Join) is not None for t in trees)
            except Exception:  # noqa: BLE001
                self._own[key] = False
        return self._own[key]

    def any_join(self, key: str) -> bool:
        if self._upstream is None:
            try:
                self._upstream = self.pipeline.upstream()
            except Exception:  # noqa: BLE001
                self._upstream = {}
        seen: set[str] = set()
        todo = [key]
        while todo:
            current = todo.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in self.pipeline.models and self._joins(current):
                return True
            todo.extend(self._upstream.get(current, ()))
        return False


def _unknown(key: str, reason: str) -> list[Rollup]:
    checks = (Check("lineage", "unknown", reason), Check("grain", "unknown", reason), Check("row_scope", "unknown"))
    return [Rollup(key, None, "unknown", missing=(reason.split(": ", 1)[-1],), checks=checks, reason=reason)]


def _compare(
    target: TableProfile, candidate: TableProfile, key: str, mappings: list[GrainMapping], joins: _JoinCache
) -> list[Rollup]:
    """Roll-up results of ``candidate`` for each aggregated attribute of ``target``; [] when none apply."""

    if not target.grain.known:
        return _unknown(key, "grain_unknown: the target's grain is unknown")
    target_keys: list[str] = []
    for column in target.grain.keys:
        attr = target.attribute(column)
        if attr is None or attr.status != "known" or attr.meaning is None:
            return _unknown(key, "grain_unknown: a target grain column has unknown meaning")
        target_keys.append(attr.meaning)
    measures = [
        a for a in target.attributes
        if a.status == "known" and a.column not in target.grain.keys and _split_calls(a.meaning or "")
        and not (a.meaning or "").startswith("window:")  # a rank or running total is not combined from finer rows
    ]  # fmt: skip
    if not measures:
        return []

    known_attrs = [a for a in candidate.attributes if a.status == "known" and a.meaning is not None]
    hidden = not candidate.attributes or len(known_attrs) != len(candidate.attributes)
    by_meaning: dict[str, AttributeMeaning] = {}
    for attr in sorted(known_attrs, key=lambda a: a.column.lower()):
        by_meaning.setdefault(attr.meaning or "", attr)
    same = _equivalences(target)
    raw = {same.get(m, m): a for m, a in by_meaning.items() if m.startswith(("col:", "field("))}
    held = {same.get(m, m): a for m, a in by_meaning.items() if m.startswith(("col:", "field(", "expr:"))}
    target_keys = [same.get(m, m) for m in target_keys]
    mappings = [GrainMapping(same.get(m.child, m.child), same.get(m.parent, m.parent), m.many_to_one, m.changes_over_time, m.complete) for m in mappings]
    mappings += [m for m in (_truncation(tk, h) for tk in target_keys for h in held) if m is not None]

    out = [
        one
        for attr in measures
        if (one := _one_attribute(target, attr, target_keys, candidate, key, by_meaning, raw, held, mappings, joins)) is not None
    ]
    if not out:
        return _unknown(key, "incomplete_lineage: the table's lineage is incomplete") if hidden else []
    if not candidate.grain.known and any(r.checks[1].detail != "raw rows" for r in out):
        return _unknown(key, "grain_unknown: the table's grain is unknown")
    return out


def _equivalences(target: TableProfile) -> dict[str, str]:
    """Columns the target equates in its joins, each mapped to one representative."""

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for text in target.row_scope.filters:
        if text.startswith(_JOIN):
            cols = _COL.findall(text)
            if len(cols) == 2:
                a, b = find(cols[0]), find(cols[1])
                if a != b:
                    parent[max(a, b)] = min(a, b)
    return {x: find(x) for x in list(parent)}


def _availability(
    calls: list[tuple[str, str]], meaning: str, by_meaning: dict, raw: dict
) -> tuple[list[str], list[str], set[str], bool, bool]:
    """(missing pieces, uncombinable aggregates, columns used, relevant, computed from raw rows)."""

    missing: list[str] = []
    uncombinable: list[str] = []
    columns: set[str] = set()
    relevant = meaning in by_meaning
    used_raw = False

    def hold(fn: str, args: str) -> bool:
        found = by_meaning.get(f"agg:{fn}({args})")
        if found is not None:
            columns.add(found.column)
        return found is not None

    pending: list[str] = []
    for fn, args in calls:
        cols = _COL.findall(args)
        label = ", ".join(sorted({_short(c) for c in cols})) or "the value"
        if cols and all(c in raw for c in cols):
            relevant = used_raw = True
            columns.update(raw[c].column for c in cols)
        elif fn == "AVG" and not args.startswith("DISTINCT "):
            # an AVG(DISTINCT) is not SUM(DISTINCT)/COUNT(DISTINCT) of the finer groups: a value
            # held by two finer groups is counted twice; it falls through as uncombinable
            has_sum, has_count = hold("SUM", args), hold("COUNT", args)
            relevant = relevant or hold("AVG", args) or has_sum or has_count
            if not has_sum:
                pending.append(f"sum of {label} at the finer grain")
            if not has_count:
                pending.append(f"count of non-null {label} at the finer grain")
        elif fn in _DECOMPOSABLE and not args.startswith("DISTINCT "):
            if hold(fn, args):
                relevant = True
            else:
                pending.append("row count at the finer grain" if not args else f"{fn.lower()} of {label} at the finer grain")
        else:
            relevant = hold(fn, args) or relevant
            uncombinable.append("COUNT DISTINCT" if args.startswith("DISTINCT ") else fn)
    if relevant:
        missing.extend(pending)
    return missing, uncombinable, columns, relevant, used_raw


def _one_attribute(
    target: TableProfile,
    attr: AttributeMeaning,
    target_keys: list[str],
    candidate: TableProfile,
    key: str,
    by_meaning: dict,
    raw: dict,
    held: dict,
    mappings: list[GrainMapping],
    joins: _JoinCache,
) -> Rollup | None:
    meaning = attr.meaning or ""
    calls = _split_calls(meaning)
    functions = tuple(sorted({"COUNT DISTINCT" if a.startswith("DISTINCT ") else f for f, a in calls}))
    missing, uncombinable, columns, relevant, used_raw = _availability(calls, meaning, by_meaning, raw)
    if not relevant:
        return None

    # 1. grain: does the finer table cover every coarser key?
    cand_keys = {
        found.meaning
        for column in candidate.grain.keys
        if (found := candidate.attribute(column)) is not None and found.meaning is not None
    }
    if not used_raw and candidate.grain.known and cand_keys <= set(target_keys):
        return None  # same grain (an overlap) or coarser (cannot produce a finer result)
    conditions: list[str] = []
    grain_missing: list[str] = []
    grain_detail: list[str] = []
    mapping_changes = False
    for column, tk in zip(target.grain.keys, target_keys):
        if tk in held:
            grain_detail.append(f"{column} is held directly")
            columns.add(held[tk].column)
            continue
        via = next((m for m in mappings if m.parent == tk and m.child in held), None)
        if via is None:
            grain_missing.append(f"mapping from the finer table's grain to '{column}' (many-to-one)")
        elif not via.many_to_one:
            grain_missing.append(f"a many-to-one mapping to '{column}' (the known mapping is not many-to-one)")
        else:
            columns.add(held[via.child].column)
            grain_detail.append(f"{column} is reached through a mapping from {held[via.child].column}")
            if via.changes_over_time is True:
                mapping_changes = True
                conditions.append(f"rows must be assigned to '{column}' using the mapping that applied when they occurred")
            elif via.changes_over_time is None:
                conditions.append(f"the mapping to '{column}' does not change over time")
            if via.complete is not True:
                conditions.append(f"every finer-grain value has a '{column}' (rows with none would be dropped)")
    detail = "; ".join(grain_detail + grain_missing) if (grain_detail or grain_missing) else "raw rows"
    grain_check = Check("grain", "unknown" if grain_missing else "matched", "raw rows" if used_raw and not grain_missing else detail)

    # 2. row scope
    scope_check, scope_conditions, scope_missing = _scope(target, candidate, raw)
    conditions += scope_conditions
    checks = (
        Check("lineage", "matched", "the attribute's raw values are held" if used_raw else "the attribute is held at a finer grain"),
        grain_check,
        scope_check,
    )

    def result(kind: str, extra: list[str], gaps: list[str], reason: str) -> Rollup:
        return Rollup(
            key, attr.column, kind, functions, tuple(sorted(columns)),
            tuple(dict.fromkeys(conditions + extra)), tuple(dict.fromkeys(gaps)), checks, None, reason,
        )  # fmt: skip

    unresolved = grain_missing + scope_missing
    if unresolved:
        return result("unknown", [], unresolved, "the finer table cannot be lined up with the target: " + "; ".join(unresolved))
    if uncombinable and not used_raw:
        names = ", ".join(sorted(set(uncombinable)))
        return result("not_derivable", [], [], f"{names} cannot be combined from finer results; the table shows where the raw data lives")

    extra: list[str] = []
    if "ROUND(" in meaning:
        extra.append("rounding is applied once, after combining, as in the target")
    if joins.any_join(key):
        extra.append("joins in the finer table neither repeat nor drop the measure's rows (no double counting)")
    if missing:
        return result("derivable_with_conditions", extra, missing, "needs additional columns at the finer grain: " + "; ".join(missing))
    if mapping_changes:
        return result("derivable_with_conditions", extra, [], "the grain mapping changes over time")
    return result("derivable_exact", extra, [], "computed from the raw rows" if used_raw else "the aggregates combine exactly")


def _scope(target: TableProfile, candidate: TableProfile, raw: dict) -> tuple[Check, list[str], list[str]]:
    """Compare row scopes. Join predicates only line rows up; they are not filters."""

    t, c = target.row_scope, candidate.row_scope
    t_filters = {f for f in t.filters if not f.startswith(_JOIN)}
    c_filters = {f for f in c.filters if not f.startswith(_JOIN)}
    if not (t.comparable and c.comparable):
        who = "target" if not t.comparable else "finer table"
        return Check("row_scope", "unknown", f"the {who}'s row scope cannot be compared"), [], [f"a comparable row scope for the {who}"]
    if t_filters == c_filters:
        return Check("row_scope", "matched", "same filters"), [], []
    if c_filters < t_filters:
        same = _equivalences(target)
        needed = {same.get(m, m) for f in t_filters - c_filters for m in _COL.findall(f)}
        if needed <= set(raw):
            return (
                Check("row_scope", "differs", "the finer table's filters are a subset of the target's"),
                ["the target's extra filters are applied to the finer rows before combining"],
                [],
            )
        return (
            Check("row_scope", "differs", "the target filters on a column the finer table does not provide"),
            [],
            ["the column the target filters on, at the finer grain"],
        )
    return (
        Check("row_scope", "differs", "the row scopes (filters or time windows) differ"),
        [],
        ["confirmation that the finer table's row scope (filters or time window) matches the target's"],
    )
