"""One business term, several definitions: the same column name computed differently in different models.

Analysts and agents assume that ``revenue`` in one table is ``revenue`` in another. This check finds where that is
false. It is the reverse of :mod:`kumosql.expression_search`, which finds one expression under many names.

A *term* is a column name, case ignored (``--variants`` also groups ``revenue_usd`` and ``total_revenue`` with
``revenue``). Every model output with that name, and every Malloy dimension and measure of that name when Malloy folders
are given, is a *definition*. Each definition has the column's meaning from :func:`kumosql.table_profile.profile_pipeline`
(``col:`` for a source column, ``agg:SUM(...)`` for an aggregate, ``expr:...`` for anything else), the table's row filters
and its grain. Definitions with the same meaning are one definition, wherever they are carried; for an aggregate the row
filters are part of the meaning (``SUM(amount)`` over paid orders is not ``SUM(amount)`` over all orders). Three more pairs
count as the same definition: a ``SUM`` of a column over the same rows as that column (a roll-up), a filter written as
``WHERE c`` in one model and as ``SUM(CASE WHEN c THEN x END)`` in the other, and a type cast.

A term with two or more definitions is a *conflict*. Conflicts are ranked:

* ``computed``: at least one definition is an aggregate, an expression, a window or a constant;
* ``passthrough``: the definitions are pass-throughs of different source columns.

Within a tier a conflict a saved ``differs`` record already explains comes after one nobody looked at. Key-like and
housekeeping names (``id``, ``*_id``, ``created_at``, ``_loaded_at``...) are skipped unless asked for.

What differs is a structural diff of the two canonical expressions, never a proof: ``differ only in WHERE status = 'paid'``,
``differ only in a constant: 1.1 vs 1``, ``SUM vs AVG``, different source columns, or the smallest pair of
sub-expressions that differ. A definition whose meaning is unknown is counted, never compared.

Everything is offline and read-only. Saved equivalence declarations are read from the data folder (or given by the
caller) and shown on the conflict they concern.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import quiet_parser

from .table_profile import COLUMN_MEANING

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .table_profile import TableProfile

FORMAT = "definition-conflicts-1"
#: definitions shown per conflict, and tables per definition, in a compact listing
DEFINITIONS_SHOWN = 5
TABLES_SHOWN = 4
MAX_MALLOY_FILES = 500
MAX_MALLOY_FILE = 1_000_000

TIERS = ("computed", "passthrough")
COMPUTED = ("aggregate", "window", "expression", "constant", "union")

# ------------------------------------------------------------------- names

_KEY_NAMES = {
    "id", "uuid", "guid", "pk", "sk", "key", "rn", "row_number", "row_num", "hash", "row_hash",
    "created_at", "updated_at", "deleted_at", "inserted_at", "modified_at", "loaded_at", "ingested_at", "extracted_at",
    "synced_at", "processed_at", "load_date", "load_ts", "created", "updated", "created_on", "updated_on", "created_date",
    "updated_date", "created_by", "updated_by", "is_deleted", "valid_from", "valid_to", "batch_id", "run_id",
}
_KEY_SUFFIXES = ("_id", "_ids", "_key", "_pk", "_sk", "_uuid", "_guid", "_hash")
_KEY_PREFIXES = ("_", "dbt_", "etl_", "meta_")

_VARIANT_SUFFIXES = ("_usd", "_eur", "_gbp", "_jpy", "_local", "_amount", "_amt", "_value", "_total", "_sum")
_VARIANT_PREFIXES = ("total_", "sum_")


def is_key_like(name: str) -> bool:
    """Whether a column name is a key or a housekeeping column (``id``, ``order_id``, ``created_at``, ``_loaded_at``)."""

    folded = name.casefold()
    return folded in _KEY_NAMES or folded.endswith(_KEY_SUFFIXES) or folded.startswith(_KEY_PREFIXES)


def term_of(name: str, variants: bool = False) -> str:
    """The term a column name belongs to: the name in lower case; with ``variants``, without a unit or total affix."""

    folded = name.casefold().strip("`")
    if not variants:
        return folded
    for prefix in _VARIANT_PREFIXES:
        if folded.startswith(prefix) and len(folded) - len(prefix) >= 3:
            folded = folded[len(prefix):]
            break
    for suffix in _VARIANT_SUFFIXES:
        if folded.endswith(suffix) and len(folded) - len(suffix) >= 3:
            folded = folded[: -len(suffix)]
            break
    return folded


# --------------------------------------------------------------- definitions


@dataclass
class Definition:
    """One column (or Malloy field) that bears a term's name."""

    table: str  #: model key, or ``file:source`` for a Malloy source
    column: str
    origin: str = "model"  #: ``model`` or ``malloy``
    meaning: str | None = None
    kind: str = "unknown"  #: aggregate, window, expression, constant, union, passthrough or unknown
    filters: tuple[str, ...] = ()  #: the table's row filters, in meaning form
    grain: str | None = None  #: what one row is, when known
    reason: str | None = None  #: why the meaning is unknown
    carried: bool = False  #: passed through from an upstream table with this meaning
    written: str | None = None  #: as the model writes it (normalized) or as the Malloy file writes it
    measure: bool = False  #: a Malloy measure: computed at whatever grouping a query asks for, so it has no grain of its own

    @property
    def aggregated(self) -> bool:
        """Whether rows are combined, so which rows are kept is part of the meaning."""

        return bool(self.meaning) and ("agg:" in self.meaning or self.meaning.startswith("window:"))


def kind_of(meaning: str | None) -> str:
    if meaning is None:
        return "unknown"
    if meaning.startswith(("col:", "field(")):
        return "passthrough"
    for prefix, kind in (("agg:", "aggregate"), ("window:", "window"), ("const:", "constant"), ("union(", "union")):
        if meaning.startswith(prefix):
            return kind
    if meaning.startswith("expr:") and "agg:" in meaning:
        return "aggregate"
    return "expression"


def _grain_text(profile: "TableProfile") -> str | None:
    grain = profile.grain
    if not grain.known:
        return None
    return ", ".join(grain.keys) if grain.keys else "single row"


def _written_columns(index, key: str) -> dict[str, str]:
    """``{column: canonical expression}`` of a model's final SELECT, from an :class:`~kumosql.expression_search.ExpressionIndex`."""

    part = getattr(index, "parts", {}).get(key) if index is not None else None
    found: dict[str, str] = {}
    for row in (part or {}).get("rows", ()):
        place, text = row[0], row[3]
        if place.startswith("column ") and " in " not in place and text:
            found.setdefault(place[len("column "):].casefold(), text)
    return found


def model_definitions(pipeline: "Pipeline", profiles: Mapping[str, "TableProfile"] | None = None, index=None) -> list[Definition]:
    """A definition for every output column of every model that builds a table (assertions and bare operations are left out).

    ``profiles`` are :func:`~kumosql.table_profile.profile_pipeline` results (computed when not given); ``index`` is an
    optional expression index whose canonical text is shown as ``written``.
    """

    if profiles is None:
        from .table_profile import profile_pipeline

        profiles = profile_pipeline(pipeline)
    found: list[Definition] = []
    placeholders = _Placeholders(pipeline)
    for key, model in pipeline.models.items():
        if model.kind == "assertion" or (model.kind == "operations" and not model.has_output):
            continue
        profile = profiles.get(key)
        if profile is None:
            continue
        grain = _grain_text(profile)
        written = _written_columns(index, key)
        filters = tuple(profile.row_scope.filters)
        for attribute in profile.attributes:
            meaning = attribute.meaning if attribute.status == "known" else None
            kind = kind_of(meaning)
            carried = attribute.transform in ("passthrough", "renamed") and kind in COMPUTED
            definition = Definition(
                key, attribute.column, "model", meaning, kind, filters, grain,
                None if meaning else (attribute.reason or "unknown"), carried, written.get(attribute.column.casefold()),
            )
            if meaning is not None:
                resolved = placeholders.resolve(key, meaning, filters if definition.aggregated else ())
                if resolved is None:
                    definition.meaning, definition.kind = None, "unknown"
                    definition.reason = ("a Dataform ${...} placeholder in its meaning or row filters stands for different "
                                         "expressions in this model and the models it reads, so it cannot be compared")
                else:
                    definition.meaning, resolved_filters = resolved
                    if definition.aggregated:
                        definition.filters = resolved_filters
            found.append(definition)
    return found


_SQLX_TOKEN = re.compile(r"__sqlx_token_(\d+)__")


class _Placeholders:
    """The Dataform expression behind each ``__sqlx_token_N__`` a meaning or filter carries.

    Placeholders are numbered per model, and a meaning or row filter may carry them from any model upstream. A placeholder
    is replaced by its ``${...}`` text when the model and every model it reads agree on what number ``N`` stands for;
    otherwise :meth:`resolve` gives ``None`` and the definition is left unknown rather than compared on a guess.
    """

    def __init__(self, pipeline: "Pipeline") -> None:
        self.pipeline = pipeline
        self._texts: dict[str, dict[int, set[str]]] = {}

    def _closure(self, key: str) -> dict[int, set[str]]:
        if key in self._texts:
            return self._texts[key]
        upstream = getattr(self.pipeline, "upstream", {}) or {}
        seen, todo = set(), [key]
        while todo:
            item = todo.pop()
            if item in seen:
                continue
            seen.add(item)
            todo.extend(upstream.get(item, ()))
        found: dict[int, set[str]] = {}
        for item in seen:
            model = self.pipeline.models.get(item)
            for number, text in enumerate(getattr(model, "masked_expressions", ()) or ()):
                found.setdefault(number, set()).add(" ".join(str(text).split()))
        self._texts[key] = found
        return found

    def resolve(self, key: str, meaning: str, filters: Sequence[str]) -> tuple[str, tuple[str, ...]] | None:
        if "__sqlx_token_" not in meaning and not any("__sqlx_token_" in f for f in filters):
            return meaning, tuple(filters)
        texts = self._closure(key)
        failed = False

        def swap(match: re.Match) -> str:
            nonlocal failed
            options = texts.get(int(match.group(1)), set())
            if len(options) != 1:
                failed = True
                return match.group(0)
            return next(iter(options))

        meaning = _SQLX_TOKEN.sub(swap, meaning)
        filters = tuple(sorted(_SQLX_TOKEN.sub(swap, f) for f in filters))
        return None if failed else (meaning, filters)


# ------------------------------------------------------------------- Malloy


class _Untranslatable(Exception):
    pass


_MALLOY_TOKEN = re.compile(
    r"""(?P<ws>\s+)|(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")|(?P<quoted>`[^`]*`)|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"""
    r"""|(?P<number>\d+(?:\.\d+)?)|(?P<op>::|->|!=|>=|<=|!~|.)""",
    re.DOTALL,
)
_MALLOY_AGGREGATES = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}
_MALLOY_REJECT = {"?", "@", "->", "::", "~", "!~", "|", "{", "}", "[", "]"}
_MALLOY_REJECT_WORDS = {"ungrouped", "all", "exclude", "source", "now"}
_FILTER_TAIL = re.compile(r"^(?P<base>.*?)\{\s*where\s*:\s*(?P<cond>.*)\}\s*$", re.DOTALL)


def _malloy_tokens(text: str) -> list[tuple[str, str]]:
    return [(m.lastgroup or "op", m.group()) for m in _MALLOY_TOKEN.finditer(text) if m.lastgroup != "ws"]


def split_filter(text: str) -> tuple[str, str | None]:
    """``amount.sum() { where: status = 'paid' }`` as ``("amount.sum()", "status = 'paid'")``."""

    match = _FILTER_TAIL.match(text)
    if match is None:
        return text, None
    return match.group("base").strip(), match.group("cond").strip().rstrip(",;").strip()


def malloy_sql(text: str, fields: Mapping[str, dict], trail: tuple[str, ...] = ()) -> str:
    """A Malloy dimension, measure or filter as BigQuery SQL over the source's table; raises when that is beyond this reader.

    Handled: ``x.sum()`` and the other aggregate methods, ``sum(x)``, ``count()``, ``count(x)`` (distinct), ``pick ... when
    ... else``, arithmetic, comparisons, ``and``/``or``/``not``, and names of the source's other fields, inlined. Not handled
    (the field stays unknown): joined paths, ``?`` and ``~`` matching, ``@`` time literals, ``::`` casts, ``ungrouped`` and
    ``all``, and a reference to a field that has its own filter.
    """

    if len(trail) > 6:
        raise _Untranslatable("fields refer to each other too deeply")
    tokens = _malloy_tokens(text)
    if not tokens:
        raise _Untranslatable("an empty expression")
    out: list[str] = []
    i = 0
    if tokens[0] == ("word", "pick"):
        return _pick(text, fields, trail)
    while i < len(tokens):
        kind, value = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ("", "")
        if kind == "op" and value in _MALLOY_REJECT:
            raise _Untranslatable(f"uses {value}, which this reader does not translate")
        if kind == "word" and value.lower() in _MALLOY_REJECT_WORDS:
            raise _Untranslatable(f"uses {value}, which this reader does not translate")
        if kind == "word" and value.lower() == "pick":
            raise _Untranslatable("a pick inside a larger expression")
        if kind == "word" and nxt == ("op", ".") :
            method = tokens[i + 2] if i + 2 < len(tokens) else ("", "")
            closes = tokens[i + 3 : i + 5] == [("op", "("), ("op", ")")]
            if method[0] == "word" and method[1].lower() in _MALLOY_AGGREGATES and closes:
                argument = _quote(value)
                if value in fields:  # the aggregate of a dimension of the source: inline it
                    other = fields[value]
                    if other["kind"] != "dimension" or value in trail or split_filter(other["expression"])[1] is not None:
                        raise _Untranslatable(f"aggregates {value}, which this reader does not inline")
                    argument = f"({malloy_sql(other['expression'], fields, (*trail, value))})"
                out.append(f"{_MALLOY_AGGREGATES[method[1].lower()]}({argument})")
                i += 5
                continue
            raise _Untranslatable(f"reads {value}.{method[1]}, a joined field or method this reader does not translate")
        if kind == "word" and nxt == ("op", "("):
            name = value.lower()
            if name == "count":
                close = _closing(tokens, i + 1)
                inner = tokens[i + 2 : close]
                if not inner:
                    out.append("COUNT(*)")
                else:
                    out.append(f"COUNT(DISTINCT {malloy_sql(_join(inner), fields, trail)})")
                i = close + 1
                continue
            out.append(_MALLOY_AGGREGATES.get(name, value.upper()))
            i += 1
            continue
        if kind == "word" and value in fields and value not in trail:
            other = fields[value]
            base, cond = split_filter(other["expression"])
            if cond is not None:
                raise _Untranslatable(f"refers to {value}, a field with its own filter")
            out.append(f"({malloy_sql(base, fields, (*trail, value))})")
            i += 1
            continue
        if kind == "word" and value.lower() not in ("and", "or", "not", "null", "true", "false", "is", "like", "in", "case", "when", "then", "else", "end"):
            out.append(_quote(value))
        elif kind == "quoted":
            out.append(_quote(value.strip("`")))
        else:
            out.append(value)
        i += 1
    sql = " ".join(out).replace("( ", "(").replace(" )", ")")
    try:
        with quiet_parser():
            tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception as exc:  # noqa: BLE001 - sqlglot raises several kinds of error
        raise _Untranslatable("the translated expression did not parse") from exc
    if tree is None:
        raise _Untranslatable("an empty expression")
    return tree.sql(dialect="bigquery")


def _quote(name: str) -> str:
    return f"`{name}`"


def _closing(tokens: list[tuple[str, str]], start: int) -> int:
    depth = 0
    for j in range(start, len(tokens)):
        if tokens[j] == ("op", "("):
            depth += 1
        elif tokens[j] == ("op", ")"):
            depth -= 1
            if depth == 0:
                return j
    raise _Untranslatable("a bracket that never closes")


def _join(tokens: list[tuple[str, str]]) -> str:
    return " ".join(value for _, value in tokens)


def _pick(text: str, fields: Mapping[str, dict], trail: tuple[str, ...]) -> str:
    """``pick 'a' when c1 pick 'b' when c2 else 'z'`` as a CASE."""

    tokens = _malloy_tokens(text)
    depth = 0
    parts: list[tuple[str, list]] = []
    for token in tokens:
        if token == ("op", "("):
            depth += 1
        elif token == ("op", ")"):
            depth -= 1
        if depth == 0 and token[0] == "word" and token[1].lower() in ("pick", "when", "else"):
            parts.append((token[1].lower(), []))
        elif parts:
            parts[-1][1].append(token)
    branches, default = [], "NULL"
    for n, (word, body) in enumerate(parts):
        if word == "pick":
            if n + 1 >= len(parts) or parts[n + 1][0] != "when" or not body:
                raise _Untranslatable("a pick this reader does not translate")
            branches.append((malloy_sql(_join(body), fields, trail), malloy_sql(_join(parts[n + 1][1]), fields, trail)))
        elif word == "else":
            default = malloy_sql(_join(body), fields, trail)
    if not branches:
        raise _Untranslatable("a pick with no branch")
    return "CASE " + " ".join(f"WHEN {cond} THEN {value}" for value, cond in branches) + f" ELSE {default} END"


def _resolve_table(pipeline: "Pipeline", name: str) -> str:
    """The project key of a table a Malloy source reads, or the name as written when the project does not know it."""

    written = name.strip().strip("`")
    folded = written.casefold()
    keys = [*pipeline.models, *pipeline.sources]
    exact = [key for key in keys if key.casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    try:
        resolved = pipeline.resolve(written)
    except Exception:  # noqa: BLE001
        resolved = None
    if resolved in pipeline.models or resolved in pipeline.sources:
        return resolved
    parts = len(folded.split("."))
    tail = [key for key in keys if ".".join(key.casefold().split(".")[-parts:]) == folded]
    return tail[0] if len(tail) == 1 else written


def read_malloy_folders(folders: Sequence[Path]) -> tuple[list[tuple[str, dict]], list[dict]]:
    """``(file, source)`` for every source of every ``.malloy`` file under ``folders``, and what could not be read.

    Files are read twice so that a source may extend one defined in another file of the same folders.
    """

    from .malloy_reader import read_malloy
    from .paths import relative_posix

    texts: list[tuple[str, str]] = []
    unknown: list[dict] = []
    for folder in folders:
        try:
            files = sorted(p for p in Path(folder).rglob("*.malloy") if p.is_file() and not p.is_symlink())
        except OSError as exc:
            unknown.append({"what": str(folder), "reason": f"the Malloy folder could not be read ({type(exc).__name__})"})
            continue
        if len(files) > MAX_MALLOY_FILES:
            unknown.append({"what": str(folder), "reason": f"{len(files)} Malloy files; only the first {MAX_MALLOY_FILES} were read"})
        for path in files[:MAX_MALLOY_FILES]:
            name = relative_posix(path, Path(folder))
            try:
                if path.stat().st_size > MAX_MALLOY_FILE:
                    unknown.append({"what": name, "reason": "a Malloy file over 1 MB was not read"})
                    continue
                texts.append((name, path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                unknown.append({"what": name, "reason": "the Malloy file could not be read"})
    first = {name: read_malloy(text) for name, text in texts}
    known = {source["name"]: source for sources in first.values() for source in sources}
    found: list[tuple[str, dict]] = []
    for name, text in texts:
        sources = read_malloy(text, known)
        unknown += [{"what": f"{name}: {item['what']}", "reason": item["reason"]} for item in sources.unknown]
        found += [(name, source) for source in sources]
    return found, unknown


def malloy_definitions(pipeline: "Pipeline", folders: Sequence[Path] = (), sources: Sequence[tuple[str, dict]] | None = None) -> tuple[list[Definition], list[dict]]:
    """A definition for every Malloy dimension and measure, with its meaning in the same form as a model column's.

    Each field becomes a small SQL query over the table its source reads (with the source's ``where:`` and the measure's
    own filter), and the queries are profiled together against the project. ``sources`` (``(file, source)`` pairs, as
    :func:`read_malloy_folders` returns) replaces reading ``folders``.
    """

    from .table_profile import profile_queries

    unknown: list[dict] = []
    if sources is None:
        sources, unknown = read_malloy_folders(folders)
    queries: dict[str, str] = {}
    pending: list[tuple[str, Definition]] = []
    found: list[Definition] = []
    for file, source in sources:
        label = f"{file}:{source['name']}"
        table = source.get("table")
        fields = {item["name"]: item for item in source.get("fields", ())}
        grain = source.get("primary_key")
        if not fields:
            continue
        if not table:
            for item in fields.values():
                found.append(Definition(label, item["name"], "malloy", reason="the table of the Malloy source is unknown", written=item["expression"], measure=item["kind"] == "measure"))
            continue
        key = _resolve_table(pipeline, table)
        where, where_problem = [], None
        for condition in source.get("filters", ()):
            try:
                where.append(f"({malloy_sql(condition, fields)})")
            except _Untranslatable as exc:
                where_problem = f"the source's where: could not be translated ({exc})"
        reference = f"`{key}`"
        for item in fields.values():
            name, measure = item["name"], item["kind"] == "measure"
            definition = Definition(label, name, "malloy", grain=None if measure or not grain else f"{grain} (primary_key)", written=item["expression"], measure=measure)
            base, cond = split_filter(item["expression"])
            try:
                select = malloy_sql(base, {k: v for k, v in fields.items() if k != name})
                conditions = list(where) if measure else []
                if measure and cond is not None:
                    conditions.append(f"({malloy_sql(cond, fields)})")
            except _Untranslatable as exc:
                definition.reason = f"Malloy expression not translated: {exc}"
                found.append(definition)
                continue
            if measure and where_problem:
                definition.reason = where_problem
                found.append(definition)
                continue
            query_key = f"{len(queries)}"
            queries[query_key] = f"SELECT {select} AS `{name}` FROM {reference}" + (f" WHERE {' AND '.join(conditions)}" if conditions else "")
            pending.append((query_key, definition))
    profiles = profile_queries(pipeline, queries) if queries else {}
    for query_key, definition in pending:
        profile = profiles.get(query_key)
        attribute = profile.attributes[0] if profile is not None and profile.attributes else None
        if attribute is None or attribute.status != "known":
            definition.reason = (attribute.reason if attribute is not None else None) or "the Malloy field could not be profiled"
        else:
            definition.meaning = attribute.meaning
            definition.kind = kind_of(attribute.meaning)
            definition.filters = tuple(profile.row_scope.filters)
        found.append(definition)
    return found, unknown


# ------------------------------------------------------------- comparing


_PREFIX = re.compile(r"(?<![\w'])(?:agg|expr|window|const|opaque):")
_TEMPLATE = re.compile(r"\$\{[^{}]*\}")
_COLUMN = re.compile(COLUMN_MEANING)
_FIELD = re.compile(r"field\((col:[^,()]+), ([A-Za-z0-9_.]+)\)")
_LITERAL_FIRST = re.compile(r"^('(?:[^']|'')*'|-?\d+(?:\.\d+)?|NULL|TRUE|FALSE) (=|<>|!=) (.+)$")


def _short_column(text: str) -> str:
    """``col:p.raw.orders.amount`` as ``raw.orders.amount``; ``field(col:..., a.b)`` as ``....a.b``."""

    match = _FIELD.fullmatch(text)
    if match:
        return _short_column(match.group(1)) + "." + match.group(2)
    parts = text[len("col:"):].split(".") if text.startswith("col:") else text.split(".")
    return ".".join(parts[1:] if len(parts) >= 4 else parts)


def show(meaning: str | None) -> str:
    """A meaning as an analyst reads it: no ``col:``/``agg:`` markers, table names without their project."""

    if meaning is None:
        return "?"
    text = _FIELD.sub(lambda m: _short_column(m.group(0)), meaning)
    text = _COLUMN.sub(lambda m: _short_column(m.group(0)), text)
    return _PREFIX.sub("", text)


def show_filter(text: str) -> str:
    shown = show(text)
    match = _LITERAL_FIRST.match(shown)
    return f"{match.group(3)} {match.group(2)} {match.group(1)}" if match else shown


def _placeholder(column: str) -> str:
    return "__kc_" + hashlib.sha1(column.encode("utf-8")).hexdigest()[:10] + "__"


def tree_of(meaning: str | None) -> tuple[exp.Expression | None, dict[str, str]]:
    """A meaning parsed as SQL, its columns replaced by placeholders (the same column is the same placeholder everywhere)."""

    names: dict[str, str] = {}
    if not meaning or meaning.startswith("union("):
        return None, names

    def swap(match: re.Match) -> str:
        holder = _placeholder(match.group(0))
        names[holder] = _short_column(match.group(0))
        return holder

    def template(match: re.Match) -> str:
        holder = _placeholder(match.group(0))
        names[holder] = match.group(0)
        return holder

    text = _TEMPLATE.sub(template, meaning)
    text = _FIELD.sub(swap, text)
    text = _COLUMN.sub(swap, text)
    text = _PREFIX.sub("", text)
    try:
        with quiet_parser():
            tree = sqlglot.parse_one(text, read="bigquery")
    except Exception:  # noqa: BLE001 - a meaning this parser cannot read is compared as text only
        return None, names
    return tree, names


def _bare(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _same(a: exp.Expression | None, b: exp.Expression | None) -> bool:
    a, b = _bare(a), _bare(b)
    return a is not None and b is not None and a.sql(dialect="bigquery") == b.sql(dialect="bigquery")


_ATOMS = (exp.Column, exp.Literal, exp.Identifier, exp.Var, exp.Null, exp.Boolean, exp.Star, exp.DataType)


def diff(a: exp.Expression, b: exp.Expression, out: list | None = None, limit: int = 6) -> list[tuple[exp.Expression, exp.Expression]]:
    """The smallest pairs of sub-expressions in which ``a`` and ``b`` differ (empty when they are the same)."""

    out = [] if out is None else out
    a, b = _bare(a), _bare(b)
    if len(out) >= limit or _same(a, b):
        return out
    if type(a) is not type(b) or isinstance(a, _ATOMS):
        out.append((a, b))
        return out
    pairs: list[tuple[exp.Expression, exp.Expression]] = []
    for key in sorted(set(a.args) | set(b.args)):
        left, right = a.args.get(key), b.args.get(key)
        if isinstance(left, exp.Expression) and isinstance(right, exp.Expression):
            pairs.append((left, right))
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right) and all(
            isinstance(x, exp.Expression) and isinstance(y, exp.Expression) for x, y in zip(left, right)
        ):
            pairs.extend(zip(left, right))
        elif not _empty_equal(left, right):
            out.append((a, b))
            return out
    for left, right in pairs:
        diff(left, right, out, limit)
    return out


def _empty_equal(left: object, right: object) -> bool:
    if left in (None, False, [], "") and right in (None, False, [], ""):
        return True
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return left == right


def _condition_of(plain: exp.Expression, wrapped: exp.Expression) -> exp.Expression | None:
    """``c`` when ``wrapped`` keeps ``plain`` only where ``c`` holds (``CASE WHEN c THEN x END``, ``IF(c, x, NULL)``, ``COUNTIF(c)``)."""

    plain, wrapped = _bare(plain), _bare(wrapped)
    if isinstance(wrapped, exp.Case) and wrapped.args.get("this") is None and len(wrapped.args.get("ifs") or []) == 1:
        branch = wrapped.args["ifs"][0]
        default = wrapped.args.get("default")
        if default is None or isinstance(default, exp.Null):
            value = branch.args.get("true")
            if _same(value, plain) or (isinstance(plain, exp.Star) and isinstance(_bare(value), exp.Literal)):
                return branch.this
    if isinstance(wrapped, exp.If):
        default = wrapped.args.get("false")
        if (default is None or isinstance(default, exp.Null)) and _same(wrapped.args.get("true"), plain):
            return wrapped.this
    if isinstance(wrapped, exp.CountIf) and isinstance(plain, exp.Count) and isinstance(_bare(plain.this), exp.Star):
        return wrapped.this
    return None


def _null_default(plain: exp.Expression, wrapped: exp.Expression) -> exp.Expression | None:
    """The value ``wrapped`` puts in place of NULL when it is ``COALESCE(plain, v)`` or ``IFNULL(plain, v)``."""

    plain, wrapped = _bare(plain), _bare(wrapped)
    if isinstance(wrapped, exp.Coalesce) and _same(wrapped.this, plain) and len(wrapped.expressions) == 1:
        return wrapped.expressions[0]
    return None


#: casts that keep the value (a number written as text, or a number in another numeric type); any other cast can change it
#: (``CAST(2.7 AS INT64)`` is 3, ``CAST(ts AS DATE)`` drops the time), so it is a difference
_KEEPING_CASTS = {"STRING", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL", "FLOAT64", "FLOAT", "DOUBLE"}
#: aggregates that skip NULL inputs, so ``AGG(CASE WHEN c THEN x END)`` combines the same values as ``AGG(x) ... WHERE c``
_NULL_SKIPPING = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count, exp.ApproxDistinct)


def _cast_of(a: exp.Expression, b: exp.Expression) -> str | None:
    """The target type when one of the two is the other in a type cast."""

    a, b = _bare(a), _bare(b)
    for outer, inner in ((a, b), (b, a)):
        if isinstance(outer, (exp.Cast, exp.TryCast)) and _same(outer.this, inner):
            return outer.to.sql(dialect="bigquery").upper()
    return None


def _in_null_skipping_aggregate(node: exp.Expression) -> bool:
    """Whether ``node`` is the argument of an aggregate that skips NULLs (looking through parentheses and DISTINCT)."""

    parent = node.parent
    while isinstance(parent, (exp.Paren, exp.Distinct)):
        parent = parent.parent
    return isinstance(parent, _NULL_SKIPPING)


@dataclass
class Group:
    """Definitions with the same meaning (and, for an aggregate, the same row filters)."""

    meaning: str
    filters: tuple[str, ...]
    kind: str
    members: list[Definition] = field(default_factory=list)
    tree: exp.Expression | None = None
    names: dict[str, str] = field(default_factory=dict)

    @property
    def aggregated(self) -> bool:
        return "agg:" in self.meaning or self.meaning.startswith("window:")

    def filter_sets(self) -> list[frozenset[str]]:
        return list(dict.fromkeys(frozenset(m.filters) for m in self.members))


@dataclass
class Relation:
    same: bool
    kind: str  #: same, rollup, filter_placement, cast, filter, constant, nulls, aggregate, column, expression
    text: str
    #: how many separate differences there are (sub-expressions, plus one for the row filters); the closest pair has the fewest
    cost: int = 0


_ORDER = {"same": 0, "rollup": 0, "filter_placement": 0, "cast": 1, "constant": 1, "nulls": 1, "filter": 2, "aggregate": 3, "column": 4, "expression": 5}


def _render(node: exp.Expression, names: Mapping[str, str]) -> str:
    text = node.sql(dialect="bigquery")
    for holder, name in names.items():
        text = text.replace(f"`{holder}`", name).replace(holder, name)
    return text


def _clip(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _filter_phrase(extra_a: Iterable[str], extra_b: Iterable[str], la: str, lb: str) -> str:
    extra_a, extra_b = sorted(extra_a), sorted(extra_b)
    shown_a = " AND ".join(show_filter(f) for f in extra_a)
    shown_b = " AND ".join(show_filter(f) for f in extra_b)
    if extra_a and not extra_b:
        return f"WHERE {_clip(shown_a, 120)} (in {la} only)"
    if extra_b and not extra_a:
        return f"WHERE {_clip(shown_b, 120)} (in {lb} only)"
    return f"WHERE {_clip(shown_a, 80)} in {la}, WHERE {_clip(shown_b, 80)} in {lb}"


def compare(a: Group, b: Group, la: str = "the first", lb: str = "the second") -> Relation:
    """How two groups of one term relate: the same definition seen two ways, or how they differ."""

    names = {**a.names, **b.names}
    filters_matter = a.aggregated or b.aggregated
    fa = set(a.filters) if a.aggregated else (set(a.filter_sets()[0]) if a.members else set())
    fb = set(b.filters) if b.aggregated else (set(b.filter_sets()[0]) if b.members else set())

    # a SUM of a column over the rows that hold it: the same definition at a coarser grain
    for big, small, lbig, lsmall in ((a, b, la, lb), (b, a, lb, la)):
        tree = _bare(big.tree)
        if isinstance(tree, exp.Sum) and not small.aggregated and small.tree is not None and _same(tree.this, small.tree):
            sets = small.filter_sets()
            if frozenset(big.filters) in sets:
                return Relation(True, "rollup", f"{lbig} is the SUM of {lsmall} over the same rows")
            other = sets[0] if sets else frozenset()
            extra = _filter_phrase(set(big.filters) - other, other - set(big.filters), lbig, lsmall)
            return Relation(False, "filter", f"differ only in {extra}; {lbig} sums the values of {lsmall}", 1)

    if a.tree is None or b.tree is None:
        filters = _filter_phrase(fa - fb, fb - fa, la, lb) if filters_matter and fa != fb else ""
        return Relation(False, "expression", f"differ: {_clip(show(a.meaning), 70)} vs {_clip(show(b.meaning), 70)}" + (f"; and in {filters}" if filters else ""), 9)

    pairs = diff(a.tree, b.tree)
    extra_filters = filters_matter and fa != fb
    cost = len(pairs) + (1 if extra_filters else 0)
    if not pairs:
        if not extra_filters:
            return Relation(True, "same", "the same expression")
        return Relation(False, "filter", f"differ only in {_filter_phrase(fa - fb, fb - fa, la, lb)}", cost)

    casts = [_cast_of(x, y) for x, y in pairs]
    if all(casts) and not extra_filters:
        if all(cast.split("(")[0] in _KEEPING_CASTS for cast in casts):
            return Relation(True, "cast", "the same values in another type")
        changing = sorted({cast for cast in casts if cast.split("(")[0] not in _KEEPING_CASTS})
        return Relation(False, "cast", f"differ only in a cast to {', '.join(changing)}, which can change the values", cost)

    if len(pairs) == 1 and not extra_filters:
        x, y = pairs[0]
        for plain, wrapped, lwrapped in ((x, y, lb), (y, x, la)):
            default = _null_default(plain, wrapped)
            if default is not None:
                return Relation(False, "nulls", f"differ only in NULL handling: {lwrapped} replaces NULL with {_clip(_render(default, names), 40)}", cost)

    if len(pairs) == 1:
        x, y = pairs[0]
        for plain, wrapped, lplain, lwrapped, fplain, fwrapped in ((x, y, la, lb, fa, fb), (y, x, lb, la, fb, fa)):
            condition = _condition_of(plain, wrapped)
            if condition is None:
                continue
            shown = show_filter(_render(condition, names))
            skips_nulls = isinstance(_bare(wrapped), exp.CountIf) or _in_null_skipping_aggregate(_bare(wrapped))
            if (filters_matter and skips_nulls and (fplain - fwrapped) and _matches_filter(condition, fplain - fwrapped)
                    and fwrapped <= fplain):
                return Relation(True, "filter_placement", f"the same filter written as WHERE in {lplain} and as CASE WHEN {shown} in {lwrapped}")
            rest = _filter_phrase(fplain - fwrapped, fwrapped - fplain, lplain, lwrapped) if extra_filters else ""
            text = f"differ only in a filter: {lwrapped} keeps only {_clip(shown, 100)} inside the aggregate"
            return Relation(False, "filter", text + (f"; and in {rest}" if rest else ""), cost)

    rest = f"; and in {_filter_phrase(fa - fb, fb - fa, la, lb)}" if extra_filters else ""
    if all(isinstance(_bare(x), (exp.Literal, exp.Neg)) and isinstance(_bare(y), (exp.Literal, exp.Neg)) for x, y in pairs):
        shown = ", ".join(f"{_render(x, names)} vs {_render(y, names)}" for x, y in pairs[:3])
        return Relation(False, "constant", f"differ {'only ' if not rest else ''}in a constant: {shown}" + rest, cost)
    if len(pairs) == 1 and isinstance(_bare(pairs[0][0]), exp.AggFunc) and isinstance(_bare(pairs[0][1]), exp.AggFunc):
        x, y = _bare(pairs[0][0]), _bare(pairs[0][1])
        if _same(x.this, y.this) and type(x) is not type(y):
            return Relation(False, "aggregate", f"{_function(x)} in {la} vs {_function(y)} in {lb} of {_clip(_render(x.this, names), 60)}" + rest, cost)
    if all(isinstance(_bare(x), exp.Column) and isinstance(_bare(y), exp.Column) for x, y in pairs):
        shown = ", ".join(f"{_render(x, names)} vs {_render(y, names)}" for x, y in pairs[:3])
        return Relation(False, "column", f"different source columns: {shown}" + rest, cost)
    x, y = pairs[0]
    more = f" (+{len(pairs) - 1} more)" if len(pairs) > 1 else ""
    return Relation(False, "expression", f"differ in {_clip(_render(x, names), 60)} vs {_clip(_render(y, names), 60)}{more}" + rest, cost)


def _function(node: exp.Expression) -> str:
    return node.sql(dialect="bigquery").split("(")[0]


def _matches_filter(condition: exp.Expression, filters: set[str]) -> bool:
    """Whether the CASE condition is exactly the filters the other definition has and this one lacks."""

    trees = [tree_of(f)[0] for f in sorted(filters)]
    if any(t is None for t in trees):
        return False
    if len(trees) == 1:
        return _same(condition, trees[0])
    return False


# ------------------------------------------------------------------ grouping


@dataclass
class Entry:
    """One definition of a term as shown: a group of the same meaning, or several groups that are the same definition."""

    number: int
    groups: list[Group]

    @property
    def lead(self) -> Group:
        return self.groups[0]

    @property
    def members(self) -> list[Definition]:
        return [m for g in self.groups for m in g.members]

    def to_json(self) -> dict:
        lead = self.lead
        tables = []
        for group in self.groups:
            for member in group.members:
                tables.append({
                    "table": member.table, "column": member.column, "origin": member.origin, "grain": member.grain,
                    "measure": member.measure, "carried": member.carried, "as": None if group is lead else show(group.meaning), "written": member.written,
                })
        return {
            "number": self.number, "expression": show(lead.meaning), "meaning": lead.meaning, "kind": lead.kind,
            "filters": [show_filter(f) for f in lead.filters] if lead.aggregated else [], "tables": tables,
        }


@dataclass
class Term:
    term: str
    names: tuple[str, ...]
    definitions: list[Entry]
    unknown: list[Definition]
    differences: list[dict] = field(default_factory=list)
    declared: list[dict] = field(default_factory=list)

    @property
    def conflict(self) -> bool:
        return len(self.definitions) >= 2

    @property
    def tables(self) -> int:
        return len({m.table for e in self.definitions for m in e.members} | {m.table for m in self.unknown})

    @property
    def tier(self) -> str | None:
        if not self.conflict:
            return None
        return "computed" if any(e.lead.kind in COMPUTED for e in self.definitions) else "passthrough"

    @property
    def acknowledged(self) -> bool:
        """Whether saved ``differs`` (or ``missing``) records already pair every one of these definitions with another."""

        covered = {n for item in self.declared if item["status"] in ("differs", "missing") for n in item["definitions"]}
        return self.conflict and covered >= {entry.number for entry in self.definitions}

    @property
    def generic(self) -> bool:
        """Nearly every table has a definition of its own: more likely a generic name (``counter``) than one business term."""

        known = len({m.table for e in self.definitions for m in e.members})
        return len(self.definitions) >= 4 and len(self.definitions) >= 0.6 * known

    def rank(self) -> tuple:
        """Computed before pass-through; then one business term before a generic name, a conflict nobody recorded before
        one the saved ``differs`` records explain, and the name more tables share (more readers assume it means one thing)."""

        return (TIERS.index(self.tier) if self.tier else 9, self.generic, self.acknowledged, -self.tables, -len(self.definitions), self.term)

    def summary(self) -> str:
        """One line: ``revenue: 3 definitions in 5 tables (computed); 2 vs 1 differ only in WHERE ...``."""

        head = f"{self.term}: {len(self.definitions)} definition{'s' if len(self.definitions) != 1 else ''} in {self.tables} table{'s' if self.tables != 1 else ''}"
        if self.tier:
            head += f" ({self.tier}{', likely a generic name' if self.generic else ''})"
        if self.differences:
            first = self.differences[0]
            head += f"; {first['between'][0]} vs {first['between'][1]}: {first['text']}"
        return head

    def to_json(self) -> dict:
        return {
            "term": self.term, "names": list(self.names), "conflict": self.conflict, "tier": self.tier,
            "tables": self.tables, "generic_name": self.generic,
            "definitions": [e.to_json() for e in self.definitions], "differences": self.differences,
            "declared": self.declared, "acknowledged": self.acknowledged,
            "unknown_definitions": [{"table": m.table, "column": m.column, "origin": m.origin, "reason": m.reason} for m in self.unknown],
        }


@dataclass
class Report:
    terms: dict[str, Term]
    skipped_key_like: int = 0
    unknown: list[dict] = field(default_factory=list)
    definitions: int = 0

    @property
    def conflicts(self) -> list[Term]:
        return sorted((t for t in self.terms.values() if t.conflict), key=Term.rank)

    @property
    def shared(self) -> list[Term]:
        """Terms defined in two or more tables (the ones this check compared)."""

        return [t for t in self.terms.values() if t.tables >= 2]

    def counts(self) -> dict:
        conflicts = self.conflicts
        shared = self.shared
        return {
            "definitions": self.definitions,
            "terms_in_several_tables": len(shared),
            "conflicts": len(conflicts),
            **{tier: sum(1 for t in conflicts if t.tier == tier) for tier in TIERS},
            "consistent": sum(1 for t in shared if not t.conflict and t.definitions),
            "unchecked": sum(1 for t in shared if not t.conflict and t.unknown and len(t.definitions) + len(t.unknown) >= 2),
            "key_like_skipped": self.skipped_key_like,
        }

    def to_json(self, limit: int | None = None) -> dict:
        conflicts = self.conflicts
        shown = conflicts if limit is None else conflicts[:limit]
        return {"format": FORMAT, "counts": self.counts(), "conflicts": [t.to_json() for t in shown],
                "conflicts_shown": f"{len(shown)} of {len(conflicts)}", "unknown": self.unknown}


def _declarations(items: Iterable[object] | None) -> list[dict]:
    """Saved equivalence entries as plain dicts (``status`` is ``equal`` when an older file has none)."""

    found = []
    for item in items or ():
        data = item.to_json() if hasattr(item, "to_json") else item
        if not isinstance(data, dict):
            continue
        columns = []
        for pair in data.get("columns") or ():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                columns.append(tuple(str(c).casefold() if c else None for c in pair))
        found.append({
            "left": str(data["left"]).casefold().strip("`") if data.get("left") else None,
            "right": str(data["right"]).casefold().strip("`") if data.get("right") else None,
            "columns": columns, "whole": bool(data.get("whole")), "status": str(data.get("status") or "equal"),
            "explanation": data.get("explanation"),
        })
    return found


def saved_equivalences() -> list[dict]:
    """The equivalence declarations (and, where the store keeps them, ``differs`` and ``missing`` records) of the data folder."""

    from . import equivalences

    loader = getattr(equivalences, "load_all", None) or equivalences.load
    try:
        return _declarations(loader())
    except Exception:  # noqa: BLE001 - an unreadable store is no declaration
        return []


def _names_table(declared: str | None, key: str) -> bool:
    if not declared:
        return False
    key = key.casefold()
    return key == declared or key.endswith("." + declared)


def _declared_for(term: Term, declarations: list[dict]) -> list[dict]:
    """The saved entries that pair two tables of different definitions of ``term`` on its columns."""

    found: list[dict] = []
    seen: set[tuple] = set()
    entries = term.definitions
    for i, first in enumerate(entries):
        for second in entries[i + 1:]:
            for a in first.members:
                for b in second.members:
                    if a.origin != "model" or b.origin != "model":
                        continue
                    for item in declarations:
                        for x, y in ((a, b), (b, a)):
                            if not (_names_table(item["left"], x.table) and _names_table(item["right"], y.table)):
                                continue
                            pair = (x.column.casefold(), y.column.casefold())
                            if pair not in item["columns"] and not (item["whole"] and pair[0] == pair[1]):
                                continue
                            ident = (x.table, y.table, item["status"])
                            if ident in seen:
                                continue
                            seen.add(ident)
                            verb = {"equal": "declared equal", "differs": "recorded as differing", "missing": "recorded as missing"}.get(item["status"], item["status"])
                            text = f"{x.table}.{x.column} and {y.table}.{y.column} are {verb} in the saved equivalences"
                            if item["status"] == "equal":
                                text += ", yet their definitions differ (an assumption about the data, not a proof)"
                            if item.get("explanation"):
                                text += f" [{item['explanation']}]"
                            found.append({"left": x.table, "right": y.table, "columns": [x.column, y.column],
                                          "definitions": [first.number if x in first.members else second.number,
                                                          second.number if y in second.members else first.number],
                                          "status": item["status"], "explanation": item.get("explanation"), "text": text})
    return found


def find_conflicts(
    definitions: Iterable[Definition], *, include_keys: bool = False, variants: bool = False,
    equivalences: Iterable[object] | None = (), only: Iterable[str] | None = None, unknown: Sequence[dict] = (),
) -> Report:
    """Group ``definitions`` by term and compare the definitions of each term.

    ``only`` limits the report to those terms (key-like names among them are kept). ``equivalences`` are saved
    declarations (``Equivalence`` objects or their JSON); pass ``None`` to read the data folder's.
    """

    wanted = {term_of(name, variants) for name in only} if only is not None else None
    declarations = saved_equivalences() if equivalences is None else _declarations(equivalences)
    by_term: dict[str, list[Definition]] = {}
    skipped: set[str] = set()
    count = 0
    for definition in definitions:
        term = term_of(definition.column, variants)
        if wanted is not None and term not in wanted:
            continue
        if wanted is None and not include_keys and (is_key_like(definition.column) or is_key_like(term)):
            skipped.add(term)
            continue
        by_term.setdefault(term, []).append(definition)
        count += 1
    terms: dict[str, Term] = {}
    for term, items in sorted(by_term.items()):
        terms[term] = _term(term, items, declarations)
    return Report(terms, len(skipped), list(unknown), count)


def _term(term: str, items: list[Definition], declarations: list[dict]) -> Term:
    groups: dict[tuple, Group] = {}
    unknown = [d for d in items if d.meaning is None]
    for definition in items:
        if definition.meaning is None:
            continue
        key = (definition.meaning, tuple(sorted(definition.filters)) if definition.aggregated else ())
        group = groups.get(key)
        if group is None:
            tree, names = tree_of(definition.meaning)
            group = groups[key] = Group(definition.meaning, key[1], definition.kind, [], tree, names)
        group.members.append(definition)
    ordered = list(groups.values())
    parent = list(range(len(ordered)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if len(ordered) <= 60:  # pairwise; a term with more distinct meanings than that is listed as it is
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                if root(i) != root(j) and compare(ordered[i], ordered[j]).same:
                    parent[root(j)] = root(i)
    clusters: dict[int, list[Group]] = {}
    for i, group in enumerate(ordered):
        clusters.setdefault(root(i), []).append(group)

    def lead_order(group: Group) -> tuple:
        return (not group.aggregated, group.kind not in COMPUTED, -len(group.members), group.meaning)

    merged = [sorted(groups_, key=lead_order) for groups_ in clusters.values()]
    merged.sort(key=lambda gs: (-sum(len(g.members) for g in gs), gs[0].kind not in COMPUTED, gs[0].meaning))
    entries = [Entry(number, gs) for number, gs in enumerate(merged, 1)]
    names = tuple(sorted({d.column for d in items}, key=str.casefold))
    result = Term(term, names, entries, unknown)
    if len(entries) >= 2:
        for later in entries[1:]:
            best: tuple | None = None
            for earlier in entries[: later.number - 1]:
                relation = compare(later.lead, earlier.lead, str(later.number), str(earlier.number))
                rank = (relation.cost, _ORDER.get(relation.kind, 9), earlier.number)
                if best is None or rank < best[0]:
                    best = (rank, earlier.number, relation)
            if best is not None:
                result.differences.append({"between": [later.number, best[1]], "kind": best[2].kind, "text": best[2].text})
        result.declared = _declared_for(result, declarations)
    return result


# --------------------------------------------------------------- a project


def check_project(
    pipeline: "Pipeline", *, malloy: Sequence[Path] = (), equivalences: Iterable[object] | None = None,
    include_keys: bool = False, variants: bool = False, profiles: Mapping[str, "TableProfile"] | None = None,
    index=None, only: Iterable[str] | None = None,
) -> Report:
    """Every definition conflict of a loaded project (and its Malloy folders)."""

    definitions = model_definitions(pipeline, profiles, index)
    unknown: list[dict] = []
    if malloy:
        found, unknown = malloy_definitions(pipeline, malloy)
        definitions += found
    return find_conflicts(definitions, include_keys=include_keys, variants=variants, equivalences=equivalences, only=only, unknown=unknown)


def short(key: str) -> str:
    """A model key without its project (``ds.orders``); a Malloy label as it is."""

    return key if ":" in key else ".".join(key.split(".")[-2:])


def entry_line(entry: Entry, limit: int = TABLES_SHOWN, width: int = 120) -> str:
    """``1. SUM(raw.orders.amount) WHERE raw.orders.status = 'paid': ds.rev_a (customer_id), ds.down (carried)``.

    The expression and the filters are clipped to ``width`` characters each; the JSON report has them whole.
    """

    lead = entry.lead
    text = f"{entry.number}. {_clip(show(lead.meaning), width)}"
    if lead.aggregated and lead.filters:
        text += " WHERE " + _clip(" AND ".join(show_filter(f) for f in lead.filters), width)
    tables = []
    for group in entry.groups:
        for member in group.members:
            notes = []
            if member.measure:
                notes.append("Malloy measure")
            elif member.grain:
                notes.append(f"per {member.grain}")
            if member.carried:
                notes.append("carried")
            if group is not lead:
                notes.append(f"as {show(group.meaning)}")
            tables.append(short(member.table) + (f" ({'; '.join(notes)})" if notes else ""))
    shown = ", ".join(tables[:limit]) + (f", +{len(tables) - limit} more" if len(tables) > limit else "")
    return f"{text}: {shown}"


# --------------------------------------------------------- the agent server


def session_definitions(session) -> tuple[list[Definition], list[dict]]:
    """Every definition of the loaded project and its ``--malloy`` folders, computed once per session."""

    return session._once("definition_conflicts_definitions", lambda: _session_definitions(session))


def _session_definitions(session) -> tuple[list[Definition], list[dict]]:
    pipeline = session.pipeline()
    definitions = model_definitions(pipeline, session.profiles())
    unknown: list[dict] = []
    folders = tuple(getattr(session, "_malloy", ()) or ())
    if folders:
        found, unknown = malloy_definitions(pipeline, folders)
        definitions += found
    return definitions, unknown


def session_equivalences(session) -> list[dict]:
    return session._once("saved_equivalences", saved_equivalences)


def session_report(session) -> Report:
    """The conflicts of the loaded project, computed once per session. Never raises: a failure is one unknown."""

    def compute() -> Report:
        try:
            definitions, unknown = session_definitions(session)
            return find_conflicts(definitions, equivalences=session_equivalences(session), unknown=unknown)
        except Exception as exc:  # noqa: BLE001 - one check must not break the tools that show it
            return Report({}, unknown=[{"what": "definition conflicts", "reason": f"the check stopped ({type(exc).__name__})"}])

    return session._once("definition_conflicts", compute)


def table_notes(session, key: str, limit: int = 4) -> str | None:
    """For ``describe_table``: one short line naming the columns of ``key`` that other tables define differently."""

    found = [term for term in session_report(session).conflicts if any(m.table == key for e in term.definitions for m in e.members)]
    if not found:
        return None
    names = ", ".join(f"{t.term} ({len(t.definitions)} ways)" for t in found[:limit]) + (f", +{len(found) - limit} more" if len(found) > limit else "")
    return _clip(f"defined differently in other tables: {names}; find_existing term={found[0].term} shows how", 200)


def term_answer(session, args: dict):
    """``find_existing term=revenue``: every definition of one column name, how they differ, and saved equivalences."""

    from .mcp_answers import Answer, ToolError, clip, offset_wanted

    raw = str(args.get("term") or "").strip().strip("`").strip()
    if not raw or len(raw) > 200 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        raise ToolError("term must be one column name, such as revenue", ["find_tables query=... searches table and column names by words."])
    definitions, unknown = session_definitions(session)
    report = find_conflicts(definitions, only=[raw], equivalences=session_equivalences(session), unknown=unknown)
    term = report.terms.get(term_of(raw))
    answer = Answer("")
    answer.fields["term"] = raw
    if term is None or (not term.definitions and not term.unknown):
        answer.fields["verdict"] = "not_found"
        answer.summary = f"No model{' or Malloy field' if getattr(session, '_malloy', ()) else ''} outputs a column named {raw}."
        answer.next(f"find_tables query={raw} searches names and descriptions for close words.")
        return answer
    offset = offset_wanted(args)
    lines = [clip(entry_line(entry), 300) for entry in term.definitions]
    answer.fields["verdict"] = "conflict" if term.conflict else ("one_definition" if term.definitions else "unknown")
    if term.tier:
        answer.fields["tier"] = term.tier
    answer.counts = {"definitions": len(term.definitions), "tables": term.tables}
    if term.unknown:
        answer.counts["meaning_unknown"] = len(term.unknown)
    answer.add_list("definitions", lines[offset:], total=len(lines), offset=offset, primary=True)
    answer.add_list("differences", [clip(f"{d['between'][0]} vs {d['between'][1]}: {d['text']}", 220) for d in term.differences])
    answer.add_list("saved_equivalences", [clip(item["text"], 220) for item in term.declared])
    by_reason: dict[str, list[str]] = {}
    for member in term.unknown:
        by_reason.setdefault(member.reason or "unknown", []).append(short(member.table))
    for reason, tables in sorted(by_reason.items(), key=lambda p: (-len(p[1]), p[0]))[:3]:
        answer.unknown(f"{len(tables)} definition{'s' if len(tables) != 1 else ''}: {', '.join(tables[:3])}" + (", ..." if len(tables) > 3 else ""), clip(reason, 160))
    if term.conflict:
        answer.summary = f"{raw} has {len(term.definitions)} different definitions in {term.tables} tables ({term.tier}): the same name does not mean the same number."
        second = term.definitions[1].members[0]
        answer.next(f"describe_table table={second.table} shows the table behind definition 2." if second.origin == "model"
                    else f"Open {second.table.split(':')[0]} for the Malloy definition.")
        answer.next("Rename the columns that mean something else (revenue_paid, revenue_gross), or have the models share one definition.")
    elif term.definitions:
        lead = term.definitions[0].lead
        where = "the one table" if term.tables == 1 else f"all {term.tables} tables"
        answer.summary = f"{raw} means the same in {where} whose meaning is known: {clip(show(lead.meaning), 120)}."
        answer.next(f"impact table={term.definitions[0].members[0].table} column={term.definitions[0].members[0].column} lists who reads it.")
    else:
        answer.summary = f"{raw} is output by {term.tables} table{'s' if term.tables != 1 else ''}, but no definition's meaning is known, so they cannot be compared."
        model = next((m for m in term.unknown if m.origin == "model"), None)
        answer.next(f"trace_column table={model.table} column={model.column} shows how far its source can be followed." if model is not None
                    else f"Open {term.unknown[0].table.split(':')[0]} for the Malloy definition.")
    return answer


# ---------------------------------------------------------------------- CLI


def _print_text(report: Report, limit: int, out) -> None:
    counts = report.counts()
    conflicts = report.conflicts
    print(
        f"{counts['conflicts']} term{'s' if counts['conflicts'] != 1 else ''} with several definitions "
        f"({counts['computed']} computed, {counts['passthrough']} pass-through), of {counts['terms_in_several_tables']} "
        f"name{'s' if counts['terms_in_several_tables'] != 1 else ''} used in more than one table; "
        f"{counts['key_like_skipped']} key-like name{'s' if counts['key_like_skipped'] != 1 else ''} skipped.",
        file=out,
    )
    for number, term in enumerate(conflicts[:limit], 1):
        print("", file=out)
        print(f"{number}. {term.term}: {len(term.definitions)} definitions in {term.tables} tables ({term.tier})", file=out)
        for entry in term.definitions[:DEFINITIONS_SHOWN]:
            print(f"   {entry_line(entry, 8)}", file=out)
        if len(term.definitions) > DEFINITIONS_SHOWN:
            print(f"   ... {len(term.definitions) - DEFINITIONS_SHOWN} more definitions (--json lists them all)", file=out)
        if term.generic:
            print("   nearly every table has its own definition: more likely a generic name than one business term", file=out)
        for item in term.differences[: DEFINITIONS_SHOWN - 1]:
            print(f"   {item['between'][0]} vs {item['between'][1]}: {item['text']}", file=out)
        for item in term.declared[:3]:
            print(f"   saved: {item['text']}", file=out)
        if term.unknown:
            print(f"   {len(term.unknown)} more definition(s) could not be read: {', '.join(short(m.table) for m in term.unknown[:3])}", file=out)
    if len(conflicts) > limit:
        print(f"\n... {len(conflicts) - limit} more; --limit {len(conflicts)} or --json lists them all.", file=out)
    if counts["unchecked"]:
        print(f"\n{counts['unchecked']} name(s) in several tables could not be compared: a definition's meaning is unknown.", file=out)
    for item in report.unknown[:5]:
        print(f"not read: {item['what']}: {item['reason']}", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kumosql-definition-conflicts",
        description="List business terms (column names) that different models, or Malloy measures of the same name, define "
        "differently: the same name, a different number. Offline and read-only.",
    )
    parser.add_argument("--project", type=Path, required=True, metavar="DIR", help="Dataform project folder, folder of .sql files or compiled graph JSON")
    parser.add_argument("--malloy", type=Path, action="append", default=[], metavar="DIR", help="folder of .malloy files whose dimensions and measures are definitions too (repeatable)")
    parser.add_argument("--source-schema", type=Path, metavar="FILE", help="JSON object of source table columns, as for pipeline-report")
    parser.add_argument("--equivalences", type=Path, metavar="FILE", help="JSON list of saved equivalence entries to show on conflicts (default: the data folder's)")
    parser.add_argument("--include-keys", action="store_true", help="also compare key-like and housekeeping names (id, *_id, created_at, _loaded_at...)")
    parser.add_argument("--variants", action="store_true", help="group simple variants of a name: revenue_usd, total_revenue and revenue are one term")
    parser.add_argument("--term", action="append", default=[], metavar="NAME", help="only this term, with every definition even when they agree (repeatable)")
    parser.add_argument("--limit", type=int, default=20, help="conflicts shown in the text listing (default 20; --json lists all)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    from .mcp_session import load_project
    from .resilience import PipelineLoadError

    declarations = None
    if args.equivalences:
        try:
            declarations = json.loads(args.equivalences.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"--equivalences: {exc}")
        if not isinstance(declarations, list):
            parser.error("--equivalences: expected a JSON list of entries")
    try:
        pipeline = load_project(args.project, args.source_schema)
    except (PipelineLoadError, OSError, ValueError) as exc:
        print(f"kumosql-definition-conflicts: {exc}", file=sys.stderr)
        return 2
    from .expression_search import build_index

    try:
        index = build_index(pipeline)
    except Exception:  # noqa: BLE001 - the written form is a convenience; the meanings come from the profiles
        index = None
    report = check_project(
        pipeline, malloy=args.malloy, equivalences=declarations, include_keys=args.include_keys,
        variants=args.variants, index=index, only=args.term or None,
    )
    if args.json:
        data = report.to_json()
        if args.term:
            data["terms"] = [term.to_json() for term in report.terms.values()]
        json.dump(data, sys.stdout, indent=1)
        print()
    elif args.term:
        for term in report.terms.values():
            print(f"{term.term}: {len(term.definitions)} definition{'s' if len(term.definitions) != 1 else ''} in {term.tables} tables")
            for entry in term.definitions:
                print(f"   {entry_line(entry, 8)}")
            for item in term.differences:
                print(f"   {item['between'][0]} vs {item['between'][1]}: {item['text']}")
            for item in term.declared[:3]:
                print(f"   saved: {item['text']}")
        if not report.terms:
            print("No model or Malloy field has that name.")
    else:
        _print_text(report, max(args.limit, 0), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
