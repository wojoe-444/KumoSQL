"""What a column means in words, carried along lineage: descriptions, time zone, unit and scale.

Declared facts (see :mod:`kumosql.column_docs`) sit on the columns the person wrote them for, usually a source column.
A model column inherits them when it is a plain copy:

* a column that is just another column, possibly renamed, or a ``CAST`` of one (also through CTEs and subqueries), takes
  every fact the upstream column has unless the model declares its own for that attribute. ``inherited_from`` names the
  column that declared it;
* an explicit conversion such as ``DATETIME(ts, 'America/New_York')`` with a literal zone sets the time zone;
* anything else (arithmetic, a function, an aggregate, a window, a ``UNION``, a field of a struct) does not carry the
  declared time zone, unit or scale, and the answer says which ones were not carried and why. Descriptions are never
  carried across a computation: a computed value is not what the upstream text describes.

Nothing is guessed from names. A TIMESTAMP or DATETIME column with no declared zone is reported as unknown, never as an error.
The column types come from the declared source schema and from typing each model's query in dependency order
(:mod:`kumosql.googlesql_types`); a column whose type cannot be worked out is not reported.

:func:`origin` is also what :mod:`kumosql.column_lint` uses to find the column an operand of a draft really is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from sqlglot import exp

from .column_docs import ATTRIBUTES
from .mcp_answers import Answer, clip
from .mcp_session import Session
from .pipeline_types import ColumnRef

#: the attributes a computation does not carry (a description is simply not inherited by a computation)
UNCARRIED = ("timezone", "unit", "scale")
TEMPORAL = ("TIMESTAMP", "DATETIME")
EXAMPLES = 3
MAX_DEPTH = 12

_UTC_NAMES = frozenset({"utc", "z", "etc/utc", "gmt", "etc/gmt", "+00:00", "+0000", "+00", "utc+0", "utc+00:00"})

LOST_REASONS = {
    "expression": "computed by an expression or function",
    "aggregate": "aggregated",
    "window": "computed by a window function",
    "union": "a UNION of several columns",
    "field": "reads a field inside a STRUCT column",
}


def short(key: str) -> str:
    return ".".join(key.split(".")[-2:])


def ref_text(key: str, column: str) -> str:
    return f"{short(key)}.{column}"


def zone_name(zone: str) -> str:
    """A time zone in one spelling for comparing: every spelling of UTC is ``utc``, others compare case-insensitively."""

    text = " ".join(str(zone).split()).casefold()
    return "utc" if text in _UTC_NAMES else text


# ------------------------------------------------------------------ declared facts


def declared(pipeline, key: str, column: str) -> dict[str, str]:
    """The attributes declared for exactly this column (config or sidecar), matched in any case."""

    docs = getattr(pipeline, "column_docs", None) or {}
    for name, attributes in (docs.get(key) or {}).items():
        if name.casefold() == column.casefold():
            return dict(attributes)
    return {}


@dataclass(frozen=True)
class Facts:
    """The declared facts of one column after inheritance."""

    values: dict[str, str] = field(default_factory=dict)
    #: attribute -> ``(table key, column)`` of the column that declared it (the column itself when it declared it)
    origin: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: attribute -> where the declaring column got it: "columns config", "column attributes file" or "docs <file>"
    labels: dict[str, str] = field(default_factory=dict)
    #: attributes set by an explicit conversion in this model's own query
    converted: tuple[str, ...] = ()
    #: ``(attribute, upstream column text, reason)`` for declared facts a computation did not carry
    lost: tuple[tuple[str, str, str], ...] = ()


_NONE = Facts()


def facts(session: Session, key: str, column: str) -> Facts:
    """The facts of column ``column`` of table ``key``, inherited along passthroughs, renames and casts (cached per session)."""

    cache: dict = session._once("column_facts", dict)
    ident = (key, column.casefold())
    if ident not in cache:
        cache[ident] = _NONE  # a cycle ends here
        try:
            cache[ident] = _facts(session, key, column, depth=0)
        except Exception:  # noqa: BLE001 - what cannot be carried is simply not shown
            cache[ident] = _own(session.pipeline(), key, column)
    return cache[ident]


def _own(pipeline, key: str, column: str) -> Facts:
    """What the column itself declares: its config or sidecar attributes, then a docs page's description and zone mention.

    A docs mention of a zone is kept as ``timezone_hint`` and never as ``timezone``: it is a hint, not a declaration."""

    from .column_docs_files import documented

    mine = declared(pipeline, key, column)
    values = dict(mine)
    sources = ((getattr(pipeline, "column_doc_sources", None) or {}).get(key) or {}).get(column, {})
    labels = {name: sources.get(name, "columns config") for name in mine}
    page = documented(pipeline, key, column)
    if page is not None:
        if "description" not in values:
            values["description"], labels["description"] = page["description"], f"docs {page['file']}"
        if page["hint"] and "timezone" not in values:
            values["timezone_hint"], labels["timezone_hint"] = page["hint"], f"docs {page['file']}"
    return Facts(values, {name: (key, column) for name in values}, labels)


def _facts(session: Session, key: str, column: str, depth: int) -> Facts:
    pipeline = session.pipeline()
    own = _own(pipeline, key, column)
    if key not in pipeline.models:
        return own
    names = {c.casefold(): c for c in pipeline.output_columns(key)}
    name = names.get(column.casefold())
    record = session.records().get(ColumnRef(key, name)) if name else None
    if record is None or record.status != "traced" or not record.sources:
        return own
    sources = sorted(record.sources)
    single = sources[0] if len(sources) == 1 else None
    carry: str | None = None  # "copy" or "convert"
    zone = ""
    if single is not None and not single.path and record.transform in ("passthrough", "renamed"):
        carry = "copy"
    elif single is not None and not single.path:
        found = origin(_parsed(session, key), name, _context(session))
        if found.kind == "column" and (found.table, found.column.casefold()) == (single.table, single.column.casefold()):
            carry = "copy"
        elif found.kind == "zone":
            carry, zone = "convert", found.zone
    if carry == "copy":
        up = facts(session, single.table, single.column)
        values, origins, labels = dict(up.values), dict(up.origin), dict(up.labels)
        values.update(own.values)
        origins.update(own.origin)
        labels.update(own.labels)
        return Facts(values, origins, labels, tuple(a for a in up.converted if a not in own.values))
    if carry == "convert":
        values, origins, labels = {}, {}, {}
        if "timezone" not in own.values:
            values["timezone"], origins["timezone"], labels["timezone"] = zone, (key, name), "conversion"
        values.update(own.values)
        origins.update(own.origin)
        labels.update(own.labels)
        return Facts(values, origins, labels, tuple(a for a in ("timezone",) if a not in own.values))
    reason = LOST_REASONS["field"] if any(s.path for s in sources) else LOST_REASONS.get(record.transform, "built from several columns" if len(sources) > 1 else "computed from it")
    if len(sources) > 1 and record.transform not in LOST_REASONS:
        reason = "built from several columns"
    lost: list[tuple[str, str, str]] = []
    for source in sources:
        up = facts(session, source.table, source.column)
        for attribute in UNCARRIED:
            if attribute in up.values and attribute not in own.values and not any(item[0] == attribute and item[1] == ref_text(source.table, source.column) for item in lost):
                lost.append((attribute, ref_text(source.table, source.column), reason))
    return Facts(own.values, own.origin, own.labels, (), tuple(lost))


def _parsed(session: Session, key: str):
    return session.pipeline()._analyse().parsed.get(key)


# ------------------------------------------------------------------ the column an expression really is


@dataclass(frozen=True)
class Origin:
    """What an output column or operand resolves to: ``column`` of a project table, a ``zone`` set by an explicit
    conversion, a computed ``expression``, or ``unknown`` (with a ``reason``)."""

    kind: str
    table: str = ""
    column: str = ""
    zone: str = ""
    reason: str = ""


@dataclass
class Context:
    resolve: Callable[[exp.Table], str | None]
    columns: Callable[[str], list[str]]
    #: whether adding or subtracting an interval, or truncating, keeps the operand's zone (a check may assume it; lineage does not)
    keep_zone: bool = False


def _context(session: Session, keep_zone: bool = False) -> Context:
    pipeline = session.pipeline()

    def resolve(table: exp.Table) -> str | None:
        try:
            key = pipeline.resolve(table)
        except Exception:  # noqa: BLE001
            return None
        return key if key in session.tables() else None

    def columns(key: str) -> list[str]:
        return list(pipeline.output_columns(key)) if key in pipeline.models else list(pipeline.source_schema.get(key, {}))

    return Context(resolve, columns, keep_zone)


def _with_key(tree: exp.Expression) -> str:
    return "with_" if tree.args.get("with_") is not None else "with"


def origin(query: exp.Expression | None, name: str, context: Context) -> Origin:
    """The column or conversion that output column ``name`` of ``query`` is, followed through CTEs and subqueries."""

    if query is None:
        return Origin("unknown", reason="the query was not parsed")
    ctes: dict[str, list[exp.Expression]] = {}
    for cte in query.find_all(exp.CTE):
        ctes.setdefault(cte.alias.casefold(), []).append(cte.this)
    return _in_query(query, name, context, ctes, 0)


def _unwrap(query: exp.Expression) -> exp.Expression:
    while isinstance(query, (exp.Subquery, exp.Paren)) and query.this is not None:
        query = query.this
    return query


def _sources(select: exp.Select) -> dict[str, exp.Expression]:
    found: dict[str, exp.Expression] = {}
    clause = select.args.get("from_") or select.args.get("from")
    nodes = [clause.this] if clause is not None and clause.this is not None else []
    nodes += [join.this for join in select.args.get("joins") or []]
    for node in nodes:
        alias = node.alias_or_name if isinstance(node, (exp.Table, exp.Subquery)) else ""
        if alias:
            found[alias.casefold()] = node
    return found


def _in_query(query: exp.Expression, name: str, context: Context, ctes: dict, depth: int) -> Origin:
    query = _unwrap(query)
    if depth > MAX_DEPTH:
        return Origin("unknown", reason="too many nested queries")
    if not isinstance(query, exp.Select):
        return Origin("unknown", reason="a set operation or other statement")
    sources = _sources(query)
    stars = []
    for projection in query.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(projection, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
            stars.append(inner)
            continue
        if projection.alias_or_name.casefold() == name.casefold():
            return _operand(inner, query, sources, context, ctes, depth)
    if stars:
        qualified = {s.table.casefold() for s in stars if isinstance(s, exp.Column) and s.table}
        pool = {alias: node for alias, node in sources.items() if not qualified or alias in qualified}
        if len(pool) == 1:
            return _in_source(next(iter(pool.values())), name, context, ctes, depth)
        return Origin("unknown", reason="SELECT * over several tables")
    return Origin("unknown", reason="the column is not among the query's outputs")


def _in_source(node: exp.Expression, name: str, context: Context, ctes: dict, depth: int) -> Origin:
    if isinstance(node, exp.Table):
        if not node.args.get("db") and node.name.casefold() in ctes:
            queries = ctes[node.name.casefold()]
            if len(queries) != 1:
                return Origin("unknown", reason="two CTEs share a name")
            return _in_query(queries[0], name, context, ctes, depth + 1)
        key = context.resolve(node)
        return Origin("column", key, name) if key else Origin("unknown", reason="a table outside the project")
    if isinstance(node, exp.Subquery):
        return _in_query(node.this, name, context, ctes, depth + 1)
    return Origin("unknown", reason="a source that is not a table or subquery")


def _has_column(node: exp.Expression, name: str, context: Context, ctes: dict) -> bool | None:
    """Whether the source has the column; ``None`` when that cannot be told."""

    if isinstance(node, exp.Table) and (node.args.get("db") or node.name.casefold() not in ctes):
        key = context.resolve(node)
        listed = context.columns(key) if key else []
        return name.casefold() in {c.casefold() for c in listed} if listed else None
    query = _unwrap(ctes[node.name.casefold()][0]) if isinstance(node, exp.Table) and len(ctes.get(node.name.casefold(), ())) == 1 else _unwrap(node.this) if isinstance(node, exp.Subquery) else None
    if not isinstance(query, exp.Select) or any(isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star)) for p in query.expressions):
        return None
    return name.casefold() in {p.alias_or_name.casefold() for p in query.expressions}


def _operand(node: exp.Expression, select: exp.Select, sources: dict, context: Context, ctes: dict, depth: int) -> Origin:
    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast, exp.Alias)):
        node = node.this
    if isinstance(node, exp.Column):
        name = node.name
        if node.table:
            source = sources.get(node.table.casefold())
            if source is None:
                return Origin("unknown", reason=f"the table alias {node.table} is not in this query's FROM")
            return _in_source(source, name, context, ctes, depth)
        if len(sources) == 1:
            return _in_source(next(iter(sources.values())), name, context, ctes, depth)
        having = {alias: _has_column(source, name, context, ctes) for alias, source in sources.items()}
        found = [alias for alias, has in having.items() if has]
        if len(found) == 1 and None not in having.values():
            return _in_source(sources[found[0]], name, context, ctes, depth)
        return Origin("unknown", reason=f"which table {name} comes from is not known")
    zone = _literal_zone(node)
    if zone:
        return Origin("zone", zone=zone)
    if context.keep_zone and isinstance(node, _SHIFTS) and node.this is not None:
        return _operand(node.this, select, sources, context, ctes, depth)
    return Origin("expression")


_SHIFTS = (exp.TimestampAdd, exp.TimestampSub, exp.DatetimeAdd, exp.DatetimeSub, exp.DateAdd, exp.DateSub)


def _literal_zone(node: exp.Expression) -> str:
    """The zone of an explicit conversion with a literal zone: ``DATETIME(ts, 'zone')`` or ``DATE(ts, 'zone')``; else ``""``."""

    if isinstance(node, exp.Datetime):
        argument = node.args.get("expression")
    elif isinstance(node, exp.Date):
        argument = node.args.get("zone")
    else:
        return ""
    if isinstance(argument, exp.Literal) and argument.is_string and argument.name.strip():
        return argument.name.strip()
    return ""


class Resolver:
    """Finds the project column (or explicit zone conversion) that an operand of a draft query is."""

    def __init__(self, session: Session, tree: exp.Expression) -> None:
        self.session = session
        self.context = _context(session, keep_zone=True)
        self.ctes: dict[str, list[exp.Expression]] = {}
        for cte in tree.find_all(exp.CTE):
            self.ctes.setdefault(cte.alias.casefold(), []).append(cte.this)
        self._sources: dict[int, dict] = {}

    def operand(self, node: exp.Expression) -> Origin:
        select = node.find_ancestor(exp.Select)
        if select is None:
            return Origin("expression")
        sources = self._sources.setdefault(id(select), _sources(select))
        return _operand(node, select, sources, self.context, self.ctes, 0)

    def zone(self, found: Origin) -> str:
        """The declared (or explicitly converted) zone of an operand, or ``""``."""

        if found.kind == "zone":
            return found.zone
        return declared_zone(self.session, found.table, found.column) if found.kind == "column" else ""

    def kind(self, found: Origin) -> str:
        """The type kind of a resolved column (``TIMESTAMP``, ``DATETIME``, ...), or ``""`` when not known."""

        if found.kind != "column":
            return ""
        return column_types(self.session).get(found.table, {}).get(found.column.casefold(), "")


def declared_zone(session: Session, table: str, column: str) -> str:
    return facts(session, table, column).values.get("timezone", "")


# ------------------------------------------------------------------ column types


def column_types(session: Session) -> dict[str, dict[str, str]]:
    """``{table key: {column (lower case): type kind}}`` for every column whose type is known (computed once per session).

    Source types come from the source schema; a model's come from typing its query against the tables it reads, in
    dependency order. A type that cannot be worked out is left out, never guessed.
    """

    return session._once("column_types", lambda: _infer_types(session))


def _infer_types(session: Session) -> dict[str, dict[str, str]]:
    from .googlesql_types import Catalog, Column, infer, parse_type

    pipeline = session.pipeline()
    catalog = Catalog()
    types: dict[str, dict[str, str]] = {}
    for key, columns in pipeline.source_schema.items():
        entries = [Column(name, parse_type(str(kind)) if kind else None) for name, kind in columns.items()]
        catalog.add(key, entries)
        types[key] = {c.name.casefold(): c.type.kind for c in entries if c.type is not None}
    parsed = pipeline._analyse().parsed
    for key in pipeline.topological_order():
        tree = parsed.get(key)
        model = pipeline.models.get(key)
        if tree is None or model is None or not model.is_query:
            continue
        try:
            typed = infer(tree.copy(), catalog)
        except Exception:  # noqa: BLE001 - an untyped model leaves its columns untyped
            continue
        if typed.columns is None:
            continue
        entries = [Column(c.name, c.type) for c in typed.columns if c.name]
        catalog.add(key, entries)
        types[key] = {c.name.casefold(): c.type.kind for c in entries if c.type is not None}
    return types


def temporal_without_zone(session: Session) -> dict[str, list[str]]:
    """``{table key: [columns]}``: TIMESTAMP and DATETIME columns whose time zone no declaration or conversion gives."""

    def compute() -> dict[str, list[str]]:
        pipeline = session.pipeline()
        found: dict[str, list[str]] = {}
        for key, kinds in column_types(session).items():
            if key in pipeline.models:
                names = list(pipeline.output_columns(key))
            else:
                names = list(pipeline.source_schema.get(key, {}))
            missing = [n for n in names if kinds.get(n.casefold()) in TEMPORAL and not facts(session, key, n).values.get("timezone")]
            if missing:
                found[key] = missing
        return found

    return session._once("temporal_without_zone", compute)


# ------------------------------------------------------------------ what the tools show


def item_of(session: Session, key: str, column: str, limit: int) -> dict | None:
    """One column's declared meaning as an answer item, or ``None`` when nothing is declared or inherited."""

    found = facts(session, key, column)
    if not found.values:
        return None
    item: dict = {}
    if "description" in found.values:
        item["description"] = clip(found.values["description"], limit)
        item["described_in"] = found.labels.get("description", "columns config")
    for attribute in UNCARRIED:
        if attribute in found.values:
            item[attribute] = clip(found.values[attribute], 60)
    if "timezone_hint" in found.values and "timezone" not in found.values:
        item["timezone_hint"] = clip(f"docs mention {found.values['timezone_hint']} (not declared)", 60)
    inherited = {a: ref_text(*found.origin[a]) for a in found.values if a in found.origin and found.origin[a] != (key, column) and not (a == "timezone_hint" and "timezone" in found.values)}
    if inherited:
        sole = set(inherited.values())
        item["inherited_from"] = next(iter(sole)) if len(sole) == 1 else inherited
    if found.converted:
        item["set_by"] = "an explicit time zone conversion in this model's query"
    return item


def _group(answer: Answer, pairs: list[tuple[str, str]], what: str, reason_of: Callable[[str], str] | None = None) -> None:
    by_reason: dict[str, list[str]] = {}
    for label, reason in pairs:
        by_reason.setdefault(reason, []).append(label)
    for reason, labels in sorted(by_reason.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        examples = ", ".join(labels[:EXAMPLES]) + (", ..." if len(labels) > EXAMPLES else "")
        answer.unknown(f"{what} of {len(labels)} column{'s' if len(labels) != 1 else ''}: {examples}", reason)


def describe_notes(answer: Answer, session: Session, key: str, columns: list[str]) -> None:
    """Add to a ``describe_table`` answer: declared meanings (``column_notes``), what a computation did not carry, and the
    TIMESTAMP/DATETIME columns with no declared zone (both under ``unknown``)."""

    pipeline = session.pipeline()
    docs = getattr(pipeline, "column_docs", None) or {}
    if not columns:
        columns = list((docs.get(key) or {}))
    items, lost = [], []
    for name in columns:
        item = item_of(session, key, name, 120)
        if item is not None:
            items.append({"column": name, **item})
        for attribute, upstream, reason in facts(session, key, name).lost:
            lost.append((f"{name} ({attribute} of {upstream})", f"not carried: {reason}; declare it for this column if it still holds"))
    answer.counts["declared_meaning"] = len(items)
    answer.add_list("column_notes", items, total=len(items))
    _group(answer, lost, "declared time zone, unit or scale")
    missing = temporal_without_zone(session).get(key, [])
    if missing:
        examples = ", ".join(missing[:EXAMPLES]) + (", ..." if len(missing) > EXAMPLES else "")
        answer.counts["no_declared_zone"] = len(missing)
        hinted = sum(1 for n in missing if facts(session, key, n).values.get("timezone_hint"))
        answer.unknown(
            f"time zone of {len(missing)} TIMESTAMP/DATETIME column{'s' if len(missing) != 1 else ''}: {examples}",
            "no zone is declared for it (columns config or --column-attributes), so comparing it with another zone is not checked"
            + (f"; docs mention a zone for {hinted} of them (a hint, not a declaration)" if hinted else ""),
        )


def trace_notes(answer: Answer, session: Session, ref: ColumnRef) -> None:
    """Add to a ``trace_column`` answer the column's declared or inherited meaning and what a computation did not carry."""

    found = item_of(session, ref.table, ref.column, 300)
    if found is not None:
        answer.fields["meaning"] = found
    for attribute, upstream, reason in facts(session, ref.table, ref.column).lost:
        answer.unknown(f"declared {attribute} of {upstream}", f"not carried: {reason}")


def zone_unknown(answer: Answer, session: Session) -> None:
    """One grouped ``unknown`` for ``project_checks``: how many TIMESTAMP/DATETIME columns have no declared zone, and where."""

    missing = temporal_without_zone(session)
    if not missing:
        return
    total = sum(len(v) for v in missing.values())
    hinted = sum(1 for key, names in missing.items() for n in names if facts(session, key, n).values.get("timezone_hint"))
    tables = sorted(missing, key=lambda k: (-len(missing[k]), k))
    examples = "; ".join(f"{short(k)} ({', '.join(missing[k][:2])}{', ...' if len(missing[k]) > 2 else ''})" for k in tables[:EXAMPLES])
    answer.unknown(
        f"time zone of {total} TIMESTAMP/DATETIME column{'s' if total != 1 else ''} in {len(missing)} table{'s' if len(missing) != 1 else ''}: {examples}{', ...' if len(tables) > EXAMPLES else ''}",
        "no zone is declared (columns config or --column-attributes); this is not an error, but zone mixes in queries cannot be checked"
        + (f"; docs mention a zone for {hinted} of them (a hint, not a declaration)" if hinted else "")
        + "; describe_table lists a table's own",
    )
