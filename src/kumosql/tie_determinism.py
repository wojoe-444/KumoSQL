"""Whether a query's result can depend on how ties are broken.

A window's ``ORDER BY`` sorts each partition into groups of tied rows (peers); BigQuery leaves the
order inside a peer group unspecified, and without ``ORDER BY`` the whole partition is one group.
Most window functions give peers the same value (``RANK``, ``DENSE_RANK``, ``PERCENT_RANK``,
``CUME_DIST``, and an aggregate over a whole partition or a ``RANGE`` frame), so their result is the
same however ties fall. Others hand out one value per physical position: ``ROW_NUMBER``, ``NTILE``,
``LAG``/``LEAD``, ``FIRST_VALUE``/``LAST_VALUE``/``NTH_VALUE`` and an aggregate over a ``ROWS`` frame.
For those, ``QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY ts DESC) = 1`` keeps *some* latest
row per ``k``, and which one may change from run to run. The same holds for the rows an
``ORDER BY .. LIMIT`` keeps, for ``LIMIT`` without ``ORDER BY``, and for aggregates that pick or
collect rows in an order: ``ANY_VALUE``, ``MAX_BY``/``MIN_BY``, ``ARRAY_AGG``/``STRING_AGG`` with or
without their own ``ORDER BY``, and ``ARRAY(SELECT ..)``.

``analyze(sql, schema=..., constraints=...)`` lists every such *site* with a verdict:

* ``deterministic``: the result is the same whichever way ties fall. Either the function gives tied
  rows the same value, or the rows cannot tie: the ``PARTITION BY`` and ``ORDER BY`` columns cover a
  unique key of the window's input (a declared key, ``GROUP BY`` keys, ...; inferred by
  :mod:`kumosql.output_properties`), or rows that tie show the same values to everything that reads
  them afterwards (``SELECT k, MAX(ts)``-style dedups that only output the tie columns, or a
  ``COUNT(*)`` over the deduplicated rows). ``facts`` lists the declared facts the verdict rests on.
* ``unknown``: no such reason was found. With ties in the data the result may change from run to
  run; ``fix`` says what would pin it down (usually: add a unique key to the ``ORDER BY``).

A verdict covers the query as written, with every declared key and NOT NULL column holding. Rows that
are exact duplicates never count as a tie: swapping them changes nothing. ``tie_dependence`` is the
short form other modules use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .ast_utils import conjuncts
from .named_windows import inline_named_windows
from .smt_equivalence import TableConstraints

DETERMINISTIC = "deterministic"
UNKNOWN = "unknown"

# Window functions that give every row of a peer group the same value.
_PEER_STABLE = {"RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST"}
# Window functions whose value depends on a row's physical position among its peers.
_POSITIONAL = {"ROW_NUMBER", "NTILE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "ANY_VALUE"}
# Aggregates whose value does not depend on the order of the rows they read.
_ORDER_FREE = {
    "SUM", "COUNT", "MIN", "MAX", "AVG", "COUNTIF", "LOGICAL_AND", "LOGICAL_OR", "BIT_AND", "BIT_OR", "BIT_XOR",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP", "CORR", "COVAR_POP", "COVAR_SAMP",
    "PERCENTILE_CONT", "PERCENTILE_DISC", "APPROX_COUNT_DISTINCT",
}
# Aggregates that collect values in an order.
_COLLECTORS = {"ARRAY_AGG", "STRING_AGG", "ARRAY_CONCAT_AGG"}

_CLASS_NAMES = {
    "RowNumber": "ROW_NUMBER", "Ntile": "NTILE", "Lag": "LAG", "Lead": "LEAD", "FirstValue": "FIRST_VALUE",
    "LastValue": "LAST_VALUE", "NthValue": "NTH_VALUE", "AnyValue": "ANY_VALUE", "Rank": "RANK",
    "DenseRank": "DENSE_RANK", "PercentRank": "PERCENT_RANK", "CumeDist": "CUME_DIST", "Sum": "SUM",
    "Count": "COUNT", "Min": "MIN", "Max": "MAX", "Avg": "AVG", "CountIf": "COUNTIF", "LogicalAnd": "LOGICAL_AND",
    "LogicalOr": "LOGICAL_OR", "BitwiseAndAgg": "BIT_AND", "BitwiseOrAgg": "BIT_OR", "BitwiseXorAgg": "BIT_XOR",
    "Stddev": "STDDEV", "StddevPop": "STDDEV_POP", "StddevSamp": "STDDEV_SAMP", "Variance": "VARIANCE",
    "VariancePop": "VAR_POP", "Corr": "CORR", "CovarPop": "COVAR_POP", "CovarSamp": "COVAR_SAMP",
    "PercentileCont": "PERCENTILE_CONT", "PercentileDisc": "PERCENTILE_DISC", "ApproxDistinct": "APPROX_COUNT_DISTINCT",
    "ArrayAgg": "ARRAY_AGG", "GroupConcat": "STRING_AGG", "ArrayConcatAgg": "ARRAY_CONCAT_AGG",
    "ArgMax": "MAX_BY", "ArgMin": "MIN_BY",
}


@dataclass(frozen=True)
class TieSite:
    """One place in a query whose result may depend on how ties are broken (module doc)."""

    kind: str  # "window", "limit", "aggregate" or "array"
    function: str  # ROW_NUMBER, LAG, LIMIT, ANY_VALUE, ARRAY_AGG, ...
    sql: str  # the call or clause, as BigQuery SQL
    partition: tuple[str, ...]  # PARTITION BY expressions, or the GROUP BY expressions of an aggregate
    order: tuple[str, ...]  # ORDER BY items
    verdict: str  # DETERMINISTIC or UNKNOWN
    reason: str
    facts: tuple[str, ...] = ()  # declared facts a deterministic verdict rests on
    fix: str = ""  # what would make an unknown site deterministic
    scope: str = ""  # where it is: "query", "WITH name" or "subquery name"

    @property
    def deterministic(self) -> bool:
        return self.verdict == DETERMINISTIC

    def to_json(self) -> dict:
        return {
            "kind": self.kind, "function": self.function, "sql": self.sql, "partition": list(self.partition),
            "order": list(self.order), "verdict": self.verdict, "reason": self.reason, "facts": list(self.facts),
            "fix": self.fix, "scope": self.scope,
        }


@dataclass(frozen=True)
class TieReport:
    """Every tie site of one query; ``unsupported`` says why the query could not be read."""

    sites: tuple[TieSite, ...] = ()
    unsupported: str = ""

    @property
    def deterministic(self) -> bool:
        """No site depends on ties (and the query could be read)."""

        return not self.unsupported and all(site.deterministic for site in self.sites)

    @property
    def undetermined(self) -> tuple[TieSite, ...]:
        return tuple(site for site in self.sites if not site.deterministic)

    @property
    def facts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fact for site in self.sites for fact in site.facts))

    def to_json(self) -> dict:
        return {
            "deterministic": self.deterministic, "unsupported": self.unsupported,
            "sites": [site.to_json() for site in self.sites],
        }


def analyze(
    query: exp.Expression | str,
    *,
    schema: Mapping[str, Sequence[str]] | None = None,
    constraints: Mapping[str, TableConstraints] | None = None,
    dialect: str = "bigquery",
) -> TieReport:
    """Every tie site of ``query`` with its verdict (module doc).

    ``schema`` maps table names to their columns and ``constraints`` to their declared NOT NULL
    columns and keys (as for the provers). Without a schema the columns a query reads stand in for
    each table's columns, which is enough to find keys but not to expand ``SELECT *``.
    """

    try:
        tree = sqlglot.parse_one(query, read=dialect) if isinstance(query, str) else query.copy()
    except sqlglot.errors.SqlglotError as error:
        return TieReport(unsupported=f"parse error: {error}")
    except RecursionError:
        return TieReport(unsupported="parse error: the query is nested too deeply to read")
    if tree is None:
        return TieReport(unsupported="empty query")
    try:
        tree = inline_named_windows(tree)
        return TieReport(sites=tuple(_Analysis(tree, schema, constraints, dialect).sites()))
    except _Unreadable as error:
        return TieReport(unsupported=str(error))
    except RecursionError:
        return TieReport(unsupported="the query is nested too deeply to analyze")


def tie_dependence(
    query: exp.Expression | str,
    *,
    keys: Mapping[str, Sequence[tuple[str, ...]]] | None = None,
    dialect: str = "bigquery",
) -> list[str]:
    """Why ``query``'s result may depend on how ties are broken; empty when it cannot.

    ``keys`` maps a table name to column sets that are unique and never NULL in that table. A site
    the analysis cannot read counts as a reason.
    """

    constraints = {
        table: TableConstraints(
            not_null=frozenset(c.lower() for key in table_keys for c in key),
            keys=tuple(tuple(c.lower() for c in key) for key in table_keys),
        )
        for table, table_keys in (keys or {}).items()
    }
    report = analyze(query, constraints=constraints, dialect=dialect)
    if report.unsupported:
        return [f"not analyzed: {report.unsupported}"]
    return [f"{site.function} ({site.sql}): {site.reason}" for site in report.undetermined]


# ----- internals ---------------------------------------------------------------------------------


class _Unreadable(Exception):
    pass


def _function_name(node: exp.Expression) -> str:
    while isinstance(node, (exp.IgnoreNulls, exp.RespectNulls)):
        node = node.this
    if isinstance(node, exp.Anonymous):
        return (node.name or "").upper()
    return _CLASS_NAMES.get(type(node).__name__) or (node.sql_name() if isinstance(node, exp.Func) else type(node).__name__.upper())


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, (exp.IgnoreNulls, exp.RespectNulls)):
        node = node.this
    return node


def _bare(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _scope_of(node: exp.Expression) -> exp.Expression | None:
    """The query (select or set operation) a node belongs to."""

    return node.find_ancestor(exp.Select, exp.Union, exp.Intersect, exp.Except)


def _owned(select: exp.Expression, node_type) -> list[exp.Expression]:
    """Nodes of ``node_type`` that belong to ``select`` itself, not to a query nested in it."""

    return [n for n in select.find_all(node_type) if n is not select and n.find_ancestor(exp.Select) is select]


def _frame_is_tie_free(window: exp.Window) -> bool:
    """Whether an order-free aggregate over this window's frame reads whole peer groups (or one row)."""

    spec = window.args.get("spec")
    if spec is None:
        return True  # the whole partition without ORDER BY, RANGE .. CURRENT ROW with one
    kind = (spec.args.get("kind") or "").upper()
    if kind == "RANGE":
        return True  # a RANGE frame is bounded by values: peers enter and leave it together
    if kind != "ROWS":
        return False

    def bound(name: str) -> str:
        value = spec.args.get(name)
        text = value if isinstance(value, str) else (value.sql() if value is not None else "")
        side = spec.args.get(f"{name}_side") or ""
        return f"{text} {side}".strip().upper()

    start, end = bound("start"), bound("end") or "CURRENT ROW"
    return (start, end) in {("UNBOUNDED PRECEDING", "UNBOUNDED FOLLOWING"), ("CURRENT ROW", "CURRENT ROW")}


def _window_needs(window: exp.Window) -> tuple[str | None, str]:
    """``(need, reason)``: need is None (the value ignores ties), "order" (it needs the rows of each
    partition in a total order) or "unknown" (not modeled)."""

    name = _function_name(window.this)
    if name in _PEER_STABLE:
        return None, f"{name} gives tied rows the same value"
    if name in _ORDER_FREE:
        if _frame_is_tie_free(window):
            return None, f"{name} over a whole partition or a RANGE frame reads tied rows together"
        return "order", f"{name} over a ROWS frame: the frame can end between tied rows"
    if name in _POSITIONAL:
        if not window.args.get("order"):
            return "order", f"{name} without ORDER BY reads the rows of each partition in an unspecified order"
        return "order", f"{name} reads rows tied on the ORDER BY in an unspecified order"
    if name in _COLLECTORS:
        return "unknown", f"the element order of an analytic {name} is not specified"
    return "unknown", f"window function {name} is not modeled"


class _Analysis:
    def __init__(self, tree: exp.Expression, schema, constraints, dialect: str) -> None:
        self.tree = tree
        self.dialect = dialect
        self.constraints = {k.lower().strip("`"): v for k, v in (constraints or {}).items()}
        self.schema = self._schema(schema)

    # ----- schema used by the key inference

    def _schema(self, schema) -> dict[str, list[str]]:
        """``schema`` plus, for every table it lacks, the columns the query reads from that table."""

        known = {k.lower().strip("`"): [c.lower() for c in v] for k, v in (schema or {}).items()}
        ctes = {cte.alias_or_name.lower() for cte in self.tree.find_all(exp.CTE)}
        inferred: dict[str, list[str]] = {}

        def add(name: str, column: str) -> None:
            columns = inferred.setdefault(name, [])
            if column not in columns:
                columns.append(column)

        for select in self.tree.find_all(exp.Select):
            sources = self._sources(select)
            tables = {alias: table for alias, table in sources.items() if isinstance(table, exp.Table)}
            for alias, table in tables.items():
                name = _table_name(table)
                if not name or (name in ctes and not table.args.get("db")) or _lookup_name(table, known) is not None:
                    continue
                facts = _lookup_constraints(table, self.constraints)
                for column in sorted(set(facts.not_null) | {c for key in facts.keys for c in key}) if facts else ():
                    add(name, column.lower())
                aliases = {item.alias.lower() for item in select.expressions if isinstance(item, exp.Alias)}
                for column in select.find_all(exp.Column):
                    if isinstance(column.this, exp.Star):
                        continue
                    qualifier = column.table.lower()
                    own = column.find_ancestor(exp.Select) is select
                    if qualifier == alias or (own and not qualifier and len(sources) == 1 and column.name.lower() not in aliases | {alias}):
                        add(name, column.name.lower())
                if not inferred.get(name):
                    add(name, "kq_any")  # a table read only by COUNT(*) still needs a column
        for name, columns in inferred.items():
            known.setdefault(name, columns)
        return known

    @staticmethod
    def _sources(select: exp.Select) -> dict[str, exp.Expression]:
        found: dict[str, exp.Expression] = {}
        from_ = select.args.get("from_") or select.args.get("from")
        items = ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]
        for item in items:
            alias = (item.alias_or_name or "").lower()
            if alias:
                found[alias] = item
        return found

    # ----- sites

    def sites(self) -> Iterable[TieSite]:
        for node in list(self.tree.walk()):
            if isinstance(node, exp.Select):
                yield from self._select_sites(node)
            if isinstance(node, (exp.Union, exp.Intersect, exp.Except)) and (node.args.get("limit") or node.args.get("offset")):
                site = self._limit_site(node)
                if site is not None:
                    yield site
            if isinstance(node, exp.Array) and len(node.expressions) == 1 and isinstance(_bare(node.expressions[0]), (exp.Select, exp.Subquery)):
                site = self._array_site(node)
                if site is not None:
                    yield site

    def _select_sites(self, select: exp.Select) -> Iterable[TieSite]:
        live = self._live_parts(select)
        windows = [w for part in live for w in part.find_all(exp.Window) if w.find_ancestor(exp.Select) is select]
        seen: set[int] = set()
        for window in windows:
            if id(window) in seen:
                continue
            seen.add(id(window))
            yield self._window_site(select, window, live)
        for part in live:
            for node in part.find_all(exp.AggFunc, exp.AnyValue, exp.ArgMax, exp.ArgMin):
                if node.find_ancestor(exp.Select) is not select or node.find_ancestor(exp.Window) is not None or id(node) in seen:
                    continue
                seen.add(id(node))
                site = self._aggregate_site(select, node)
                if site is not None:
                    yield site
        if select.args.get("limit") or select.args.get("offset"):
            site = self._limit_site(select)
            if site is not None:
                yield site

    # ----- liveness: which parts of a select can change what the query returns

    def _live_outputs(self, select: exp.Expression) -> set[str] | None:
        """Output names some reader uses; None means all of them (or unknown)."""

        parent = select.parent
        if isinstance(parent, exp.Exists) or (isinstance(parent, exp.Subquery) and isinstance(parent.parent, exp.Exists)):
            return set()
        if isinstance(parent, exp.CTE):
            name = parent.alias_or_name.lower()
            readers = [
                t for t in self.tree.find_all(exp.Table)
                if t.name.lower() == name and not t.args.get("db") and not _inside(t, parent)
            ]
            names: set[str] = set()
            for table in readers:
                reader = table.find_ancestor(exp.Select)
                used = self._read_from(reader, (table.alias_or_name or name).lower()) if reader is not None else None
                if used is None:
                    return None
                names |= used
            return names
        if isinstance(parent, exp.Subquery) and isinstance(parent.parent, (exp.From, exp.Join)) and parent.alias:
            reader = parent.parent.parent
            if isinstance(reader, exp.Select):
                return self._read_from(reader, parent.alias.lower())
        return None

    def _read_from(self, reader: exp.Select, alias: str) -> set[str] | None:
        """Names ``reader`` reads from its source ``alias``; None when it may read every column."""

        if any(j.args.get("using") or "NATURAL" in (j.args.get("method") or "").upper() for j in reader.args.get("joins") or []):
            return None
        names: set[str] = set()
        for column in reader.find_all(exp.Column):
            qualifier = column.table.lower()
            if column.find_ancestor(exp.Select) is not reader and qualifier != alias:
                continue  # a column of a nested query (a WITH body, a subquery) reads its own sources
            if isinstance(column.this, exp.Star):
                if qualifier in ("", alias):
                    return None
                continue
            if qualifier == alias:
                names.add(column.name.lower())
            elif not qualifier:
                if column.name.lower() == alias:
                    return None  # the row read as a value
                names.add(column.name.lower())
        if any(isinstance(s, exp.Star) for s in reader.expressions):
            return None
        return names

    def _live_parts(self, select: exp.Select) -> list[exp.Expression]:
        """The expressions of ``select`` evaluated after its windows that can reach the result."""

        live = self._live_outputs(select)
        items = list(select.expressions)
        aliases = {item.alias.lower(): item for item in items if isinstance(item, exp.Alias)}
        parts = []
        named = {(item.alias_or_name or "").lower() for item in items if not _is_star(item)}
        for item in items:
            if _is_star(item) and live is not None:
                # a star with known readers stands for the columns they read
                qualifier = item.table if isinstance(item, exp.Column) else None
                for name in sorted(live - named):
                    column = exp.column(name, table=qualifier or None)
                    column.parent = select
                    parts.append(column)
            elif live is None or _is_star(item) or (item.alias_or_name or "").lower() in live:
                parts.append(item)
        if select.args.get("qualify") is not None:
            parts.append(select.args["qualify"])
        if select.args.get("having") is not None:
            parts.append(select.args["having"])
        if select.args.get("distinct") is not None and live is not None and len(parts) < len(items):
            parts = items + parts[len(items):]  # DISTINCT compares every output
        order = select.args.get("order")
        if order is not None and (select.args.get("limit") or select.args.get("offset") or isinstance(select.parent, exp.Array)):
            parts.append(order)
        # an output read by name from a live part (QUALIFY rn = 1) is live too
        pending = list(parts)
        while pending:
            part = pending.pop()
            for column in part.find_all(exp.Column):
                item = aliases.get(column.name.lower()) if not column.table else None
                if item is not None and item not in parts and column.find_ancestor(exp.Select) is select:
                    parts.append(item)
                    pending.append(item)
        return parts

    # ----- window sites

    def _window_site(self, select: exp.Select, window: exp.Window, live: list[exp.Expression]) -> TieSite:
        need, reason = _window_needs(window)
        partition = list(window.args.get("partition_by") or [])
        order_node = window.args.get("order")
        ordered = list(order_node.expressions) if order_node is not None else []
        base = dict(
            kind="window", function=_function_name(window.this), sql=self._sql(window),
            partition=tuple(self._sql(p) for p in partition), order=tuple(self._sql(o) for o in ordered),
            scope=self._scope_name(select),
        )
        if need is None:
            return TieSite(verdict=DETERMINISTIC, reason=reason, **base)
        if need == "unknown":
            return TieSite(verdict=UNKNOWN, reason=reason, fix="", **base)
        keys = partition + [o.this if isinstance(o, exp.Ordered) else o for o in ordered]
        covered = self._covering_key(select, keys)
        if covered is not None:
            facts, how = covered
            return TieSite(verdict=DETERMINISTIC, reason=f"no two rows tie: {how}", facts=facts, **base)
        if self._indistinguishable(select, keys, live):
            return TieSite(
                verdict=DETERMINISTIC,
                reason="rows tied on PARTITION BY and ORDER BY show the same values to everything that reads them",
                **base,
            )
        return TieSite(verdict=UNKNOWN, reason=reason, fix=self._fix(select, keys, "the window's ORDER BY"), **base)

    # ----- LIMIT sites

    def _limit_site(self, node: exp.Expression) -> TieSite | None:
        limit, offset, order = node.args.get("limit"), node.args.get("offset"), node.args.get("order")
        count = _constant(limit.expression) if isinstance(limit, exp.Limit) else None
        skip = _constant(offset.expression) if offset is not None else 0
        if count == 0:
            return None  # LIMIT 0 keeps nothing, whatever the order
        clause = " ".join(self._sql(part) for part in (order, limit, offset) if part is not None)
        ordered = list(order.expressions) if order is not None else []
        base = dict(
            kind="limit", function="LIMIT" if limit is not None else "OFFSET", sql=clause, partition=(),
            order=tuple(self._sql(o) for o in ordered), scope=self._scope_name(node),
        )
        parent = node.parent
        if isinstance(parent, exp.Exists) or (isinstance(parent, exp.Subquery) and isinstance(parent.parent, exp.Exists)):
            if not skip:
                return TieSite(verdict=DETERMINISTIC, reason="EXISTS only asks whether a row is left", **base)
        bare = node.copy()
        for key in ("limit", "offset", "order"):
            bare.set(key, None)
        if count is not None and count >= 1 and not skip and self._at_most_one_row(node, bare):
            return TieSite(verdict=DETERMINISTIC, reason="the query returns at most one row, so the cut keeps it", **base)
        if order is None:
            return TieSite(
                verdict=UNKNOWN, reason="LIMIT or OFFSET without ORDER BY keeps arbitrary rows",
                fix="add an ORDER BY on a unique key", **base,
            )
        outputs = self._outputs(node)
        live = self._live_outputs(node)
        positions = self._order_positions(node, ordered, outputs)
        if outputs is not None and positions is not None:
            visible = [i for i, (name, _) in enumerate(outputs) if live is None or name in live]
            if set(visible) <= set(positions):
                return TieSite(
                    verdict=DETERMINISTIC,
                    reason="rows tied on the ORDER BY are identical in every column read, so any of them can be kept",
                    **base,
                )
        if isinstance(node, exp.Select):
            distinct = node.args.get("distinct") is not None
            covered = self._covering_key(node, [o.this if isinstance(o, exp.Ordered) else o for o in ordered], outputs=distinct)
            if covered is not None:
                facts, how = covered
                return TieSite(verdict=DETERMINISTIC, reason=f"no two rows tie on the ORDER BY: {how}", facts=facts, **base)
            fix = self._fix(node, [o.this if isinstance(o, exp.Ordered) else o for o in ordered], "the ORDER BY", outputs=distinct)
        else:
            fix = "add a unique key to the ORDER BY"
        return TieSite(verdict=UNKNOWN, reason="rows tied on the ORDER BY may be cut either way by the LIMIT", fix=fix, **base)

    def _outputs(self, node: exp.Expression) -> list[tuple[str, exp.Expression]] | None:
        first = node
        while isinstance(first, (exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
            first = first.this
        if not isinstance(first, exp.Select) or any(_is_star(e) for e in first.expressions):
            return None
        return [((e.alias_or_name or "").lower(), e.this if isinstance(e, exp.Alias) else e) for e in first.expressions]

    def _order_positions(self, node, ordered, outputs) -> list[int] | None:
        if outputs is None:
            return None
        positions = []
        for item in ordered:
            key = _bare(item.this if isinstance(item, exp.Ordered) else item)
            if isinstance(key, exp.Literal) and not key.is_string and key.this.isdigit():
                positions.append(int(key.this) - 1)
                continue
            text = key.sql(dialect=self.dialect)
            for index, (name, value) in enumerate(outputs):
                if (isinstance(key, exp.Column) and not key.table and key.name.lower() == name) or value.sql(dialect=self.dialect) == text:
                    positions.append(index)
        return positions

    # ----- aggregate sites

    def _aggregate_site(self, select: exp.Select, node: exp.Expression) -> TieSite | None:
        name = _function_name(node)
        if name not in _COLLECTORS and name not in ("ANY_VALUE", "MAX_BY", "MIN_BY"):
            return None
        inner = _unwrap(node)
        group = self._group_keys(select)
        base = dict(kind="aggregate", function=name, sql=self._sql(node), partition=tuple(self._sql(g) for g in group), scope=self._scope_name(select))
        if name in ("MAX_BY", "MIN_BY"):
            value, by = inner.this, inner.expression
            order_keys, ordered_text = [by], (self._sql(by),)
            reason = f"{name} picks among rows tied on {self._sql(by)}"
        elif name == "ANY_VALUE":
            value = inner.this
            if isinstance(value, exp.HavingMax):
                order_keys, ordered_text = [value.expression], (self._sql(value.expression),)
                value = value.this
                reason = "ANY_VALUE .. HAVING picks among rows tied on its HAVING value"
            else:
                order_keys, ordered_text = [], ()
                reason = "ANY_VALUE picks an arbitrary row of the group"
        else:
            value = inner.this
            limit = None
            if isinstance(value, exp.Limit):
                limit, value = value, value.this
            order = value if isinstance(value, exp.Order) else None
            if order is not None:
                value = order.this
            distinct = isinstance(value, exp.Distinct)
            if distinct:
                value = value.expressions[0] if value.expressions else value
            order_keys = [o.this if isinstance(o, exp.Ordered) else o for o in (order.expressions if order is not None else [])]
            ordered_text = tuple(self._sql(o) for o in (order.expressions if order is not None else []))
            if distinct and order is not None and any(_same(k, value) for k in order_keys):
                return TieSite(verdict=DETERMINISTIC, reason=f"{name}(DISTINCT x ORDER BY x) sorts distinct values", order=ordered_text, **base)
            reason = (
                f"{name} without ORDER BY collects values in an unspecified order" if order is None
                else f"{name} orders values tied on its ORDER BY arbitrarily" + (" before the LIMIT" if limit is not None else "")
            )
        keys = group + order_keys
        covered = self._covering_key(select, keys, grouped_input=True)
        if covered is not None:
            facts, how = covered
            return TieSite(verdict=DETERMINISTIC, reason=f"no two rows of a group tie: {how}", facts=facts, order=ordered_text, **base)
        if value is not None and self._determined_by(select, [value], keys):
            return TieSite(verdict=DETERMINISTIC, reason="rows that tie give the same value", order=ordered_text, **base)
        what = "the aggregate's ORDER BY" if order_keys else "an ORDER BY in the aggregate"
        return TieSite(verdict=UNKNOWN, reason=reason, fix=self._fix(select, keys, what, grouped_input=True), order=ordered_text, **base)

    def _group_keys(self, select: exp.Select) -> list[exp.Expression]:
        group = select.args.get("group")
        if group is None:
            return []
        if any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube")):
            return []  # the coarsest set is the safe one; ROLLUP's grand total has no keys
        outputs = {(e.alias_or_name or "").lower(): (e.this if isinstance(e, exp.Alias) else e) for e in select.expressions}
        keys = []
        for item in group.expressions:
            if isinstance(item, exp.Literal) and not item.is_string and item.this.isdigit() and 1 <= int(item.this) <= len(select.expressions):
                chosen = select.expressions[int(item.this) - 1]
                keys.append(chosen.this if isinstance(chosen, exp.Alias) else chosen)
            elif isinstance(item, exp.Column) and not item.table and item.name.lower() in outputs and not self._is_source_column(select, item):
                keys.append(outputs[item.name.lower()])
            else:
                keys.append(item)
        return keys

    def _is_source_column(self, select: exp.Select, column: exp.Column) -> bool:
        name = column.name.lower()
        for alias, source in self._sources(select).items():
            if isinstance(source, exp.Table):
                columns = self.schema.get(_lookup_name(source, self.schema) or "", [])
                if name in columns:
                    return True
            else:
                return True  # a derived table: assume it may hold the name
        return False

    # ----- ARRAY(SELECT ..) sites

    def _array_site(self, node: exp.Array) -> TieSite | None:
        query = _bare(node.expressions[0])
        while isinstance(query, exp.Subquery):
            query = query.this
        if not isinstance(query, exp.Select):
            return None
        order = query.args.get("order")
        ordered = list(order.expressions) if order is not None else []
        base = dict(kind="array", function="ARRAY", sql=self._sql(node), partition=(), order=tuple(self._sql(o) for o in ordered), scope=self._scope_name(query))
        if self._at_most_one_row(query, query):
            return TieSite(verdict=DETERMINISTIC, reason="the subquery returns at most one row", **base)
        keys = [o.this if isinstance(o, exp.Ordered) else o for o in ordered]
        if ordered:
            covered = self._covering_key(query, keys, outputs=query.args.get("distinct") is not None)
            if covered is not None:
                facts, how = covered
                return TieSite(verdict=DETERMINISTIC, reason=f"no two elements tie on the ORDER BY: {how}", facts=facts, **base)
            outputs = self._outputs(query)
            positions = self._order_positions(query, ordered, outputs)
            if outputs is not None and positions is not None and set(range(len(outputs))) <= set(positions):
                return TieSite(verdict=DETERMINISTIC, reason="elements tied on the ORDER BY are equal", **base)
        reason = "ARRAY(SELECT ..) without ORDER BY lists elements in an unspecified order" if not ordered else "ARRAY(SELECT ..) lists elements tied on its ORDER BY in an unspecified order"
        return TieSite(verdict=UNKNOWN, reason=reason, fix="order the subquery by a unique key", **base)

    # ----- the facts behind a verdict

    def _probe(self, select: exp.Select, exprs: list[exp.Expression], *, outputs: bool = False, grouped_input: bool = False) -> str | None:
        """SQL whose output rows are ``select``'s window input (or, with ``outputs``, its rows before
        any LIMIT), projected on ``exprs`` (and, with ``outputs``, also on every output)."""

        if any(e.find(exp.Window) for e in exprs) or any(e.find(exp.Select) for e in exprs):
            return None
        probe = select.copy()
        for key in ("qualify", "order", "limit", "offset", "windows"):
            probe.set(key, None)
        if grouped_input:
            for key in ("group", "having", "distinct"):
                probe.set(key, None)
        aliases = {item.alias.lower(): item.this for item in select.expressions if isinstance(item, exp.Alias)}
        resolved = []
        for e in exprs:
            e = _bare(e)
            if isinstance(e, exp.Column) and not e.table and e.name.lower() in aliases and not self._is_source_column(select, e):
                e = aliases[e.name.lower()]
            elif isinstance(e, exp.Literal) and not e.is_string and e.this.isdigit() and 1 <= int(e.this) <= len(select.expressions):
                chosen = select.expressions[int(e.this) - 1]
                e = chosen.this if isinstance(chosen, exp.Alias) else chosen
            if e.find(exp.Window):
                return None
            resolved.append(e)
        items = [exp.alias_(e.copy(), f"kq_tie{i}") for i, e in enumerate(resolved)]
        if outputs:
            if any(_is_star(e) for e in select.expressions):
                return None
            items += [exp.alias_(e.copy(), f"kq_out{i}") for i, e in enumerate(e.this if isinstance(e, exp.Alias) else e for e in select.expressions)]
        else:
            probe.set("distinct", None)
        if not items:
            items = [exp.alias_(exp.Literal.number(1), "kq_tie_one")]
        probe.set("expressions", items)
        ctes = self._visible_ctes(select)
        if ctes:
            own = probe.args.get("with_") or probe.args.get("with")
            names = {c.alias_or_name.lower() for c in own.expressions} if own is not None else set()
            extra = [c.copy() for c in ctes if c.alias_or_name.lower() not in names]
            if extra:
                with_ = exp.With(expressions=extra + (list(own.expressions) if own is not None else []))
                probe.set("with_" if "with_" in probe.arg_types else "with", with_)
        return probe.sql(dialect=self.dialect)

    def _visible_ctes(self, select: exp.Expression) -> list[exp.CTE]:
        found: list[exp.CTE] = []
        node = select
        while node is not None:
            parent = node.parent
            if isinstance(parent, exp.CTE):
                with_ = parent.parent
                for cte in with_.expressions:
                    if cte is parent:
                        break
                    found.insert(0, cte)
            with_ = node.args.get("with_") or node.args.get("with") if isinstance(node, exp.Expression) and node is not select else None
            if with_ is not None:
                found = list(with_.expressions) + found
            node = parent
        unique: dict[str, exp.CTE] = {}
        for cte in found:
            unique.setdefault(cte.alias_or_name.lower(), cte)
        return list(unique.values())

    def _properties(self, sql: str):
        from .output_properties import infer_properties

        return infer_properties(sql, self.constraints, self.schema, dialect=self.dialect)

    def _covering_key(self, select, exprs, *, outputs: bool = False, grouped_input: bool = False):
        """``(facts, how)`` when ``exprs`` cover a unique key of the rows they sort, else None."""

        sql = self._probe(select, exprs, outputs=outputs, grouped_input=grouped_input)
        if sql is None:
            return None
        properties = self._properties(sql)
        if properties.unsupported:
            return None
        width = len(exprs)
        for key in properties.keys:
            if set(key.positions) <= set(range(width)):
                facts = tuple(key.assumptions)
                if not key.positions:
                    how = "at most one row"
                else:
                    how = "(" + ", ".join(self._sql(exprs[i]) for i in key.positions) + ") is unique"
                return facts, how + (f" ({'; '.join(facts)})" if facts else "")
        return None

    def _fix(self, select, exprs, where: str, *, outputs: bool = False, grouped_input: bool = False) -> str:
        """A suggestion: a known unique key of the rows that the ORDER BY could end with."""

        if outputs or not isinstance(select, exp.Select):
            return f"add a unique key to {where}"
        columns = self._source_columns(select)
        if columns:
            sql = self._probe(select, list(exprs) + columns, grouped_input=grouped_input)
            properties = self._properties(sql) if sql else None
            if properties is not None and not properties.unsupported:
                width = len(exprs)
                for key in properties.keys:
                    extra = [columns[i - width].sql(dialect=self.dialect) for i in key.positions if i >= width]
                    if key.positions and extra:
                        return f"add {', '.join(extra)} (unique with the existing keys) to {where}"
        return f"add columns that are unique within each partition to {where}, or keep every tied row with RANK"

    def _source_columns(self, select: exp.Select) -> list[exp.Expression]:
        columns = []
        for alias, source in self._sources(select).items():
            if isinstance(source, exp.Table):
                for name in self.schema.get(_lookup_name(source, self.schema) or "", []):
                    if name != "kq_any":
                        columns.append(exp.column(name, table=alias))
        return columns

    def _at_most_one_row(self, node: exp.Expression, bare: exp.Expression) -> bool:
        target = bare if isinstance(bare, exp.Select) else bare
        ctes = self._visible_ctes(node)
        query = target.copy()
        if ctes and isinstance(query, exp.Select):
            own = query.args.get("with_") or query.args.get("with")
            names = {c.alias_or_name.lower() for c in own.expressions} if own is not None else set()
            extra = [c.copy() for c in ctes if c.alias_or_name.lower() not in names]
            if extra:
                query.set("with_" if "with_" in query.arg_types else "with", exp.With(expressions=extra + (list(own.expressions) if own is not None else [])))
        properties = self._properties(query.sql(dialect=self.dialect))
        return not properties.unsupported and properties.at_most_one_row

    # ----- tied rows that nobody can tell apart

    def _equalities(self, select: exp.Select) -> tuple[dict[str, str], set[str]]:
        """Union-find parents over column keys from the WHERE and inner-join ON equalities, and the
        column keys fixed to one value there."""

        parent: dict[str, str] = {}

        def find(key: str) -> str:
            while parent.get(key, key) != key:
                key = parent[key]
            return key

        fixed: set[str] = set()
        conditions = []
        if select.args.get("where") is not None:
            conditions += conjuncts(select.args["where"].this)
        for join in select.args.get("joins") or []:
            if not join.args.get("side") and join.args.get("on") is not None:
                conditions += conjuncts(join.args["on"])
        for condition in conditions:
            condition = _bare(condition)
            if isinstance(condition, exp.EQ):
                left, right = _bare(condition.this), _bare(condition.expression)
                left_key = self._column_key(select, left) if isinstance(left, exp.Column) else None
                right_key = self._column_key(select, right) if isinstance(right, exp.Column) else None
                if left_key and right_key:
                    parent[find(left_key)] = find(right_key)
                elif left_key and isinstance(right, exp.Literal):
                    fixed.add(left_key)
                elif right_key and isinstance(left, exp.Literal):
                    fixed.add(right_key)
            elif isinstance(condition, exp.Is) and isinstance(_bare(condition.this), exp.Column) and isinstance(condition.expression, exp.Null):
                key = self._column_key(select, _bare(condition.this))
                if key:
                    fixed.add(key)
        roots = {k: find(k) for k in list(parent)}
        return roots, {roots.get(k, k) for k in fixed}

    def _column_key(self, select: exp.Select, column: exp.Column) -> str | None:
        sources = self._sources(select)
        qualifier = column.table.lower()
        if not qualifier:
            if len(sources) != 1:
                return None
            qualifier = next(iter(sources))
        if qualifier not in sources:
            return None  # an outer column: fixed for the whole select, so never a tie breaker
        return f"{qualifier}.{column.name.lower()}"

    def _determined_by(self, select: exp.Select, values: list[exp.Expression], keys: list[exp.Expression]) -> bool:
        """Whether every input column in ``values`` is one of the plain columns in ``keys`` (or equal
        to one, or fixed to a constant by WHERE): rows that agree on ``keys`` agree on ``values``."""

        roots, fixed = self._equalities(select)
        cover = set()
        for key in keys:
            key = _bare(key)
            if isinstance(key, exp.Column):
                found = self._column_key(select, key)
                if found is None:
                    return False
                cover.add(roots.get(found, found))
        sources = self._sources(select)
        aliases = {item.alias.lower(): item for item in select.expressions if isinstance(item, exp.Alias)}
        for value in values:
            for node in value.walk():
                if isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star)):
                    return False
                if isinstance(node, exp.Column):
                    qualifier = node.table.lower()
                    if node.find_ancestor(exp.Select) is not select and not qualifier:
                        continue  # a column of a nested query's own source
                    if qualifier and qualifier not in sources:
                        continue  # an outer or nested-query column
                    if not qualifier and node.name.lower() in aliases and not self._is_source_column(select, node):
                        continue  # an output read by name; its own parts are checked as live parts
                    if not qualifier and node.name.lower() in sources and not self._is_source_column(select, node):
                        return False  # a whole row read as a value
                    key = self._column_key(select, node)
                    if key is None:
                        return False
                    root = roots.get(key, key)
                    if root not in cover and root not in fixed:
                        return False
        return True

    def _indistinguishable(self, select: exp.Select, keys: list[exp.Expression], live: list[exp.Expression]) -> bool:
        if select.args.get("group") is not None or any(n.find_ancestor(exp.Window) is None for part in live for n in part.find_all(exp.AggFunc) if n.find_ancestor(exp.Select) is select):
            return False
        reads = []
        for part in live:
            if isinstance(part, exp.Having):
                continue
            reads.append(part)
        return self._determined_by(select, reads, keys)

    # ----- display

    def _sql(self, node: exp.Expression) -> str:
        return node.sql(dialect=self.dialect)

    def _scope_name(self, node: exp.Expression) -> str:
        current = node
        while current is not None:
            parent = current.parent
            if isinstance(parent, exp.CTE):
                return f"WITH {parent.alias_or_name}"
            if isinstance(parent, exp.Subquery) and parent.alias and isinstance(current, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                return f"subquery {parent.alias}"
            current = parent
        return "query"


def _constant(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and node.this.isdigit():
        return int(node.this)
    return None


def _same(a: exp.Expression, b: exp.Expression) -> bool:
    return _bare(a).sql().lower() == _bare(b).sql().lower()


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _name_parts(table: exp.Table) -> list[exp.Expression]:
    """The named parts of a table reference. ``t..u`` (an empty middle part) leaves a plain empty string there."""

    return [p for p in (table.args.get("catalog"), table.args.get("db"), table.this) if isinstance(p, exp.Expression) and p.name]


def _table_name(table: exp.Table) -> str:
    return ".".join(p.name.lower() for p in _name_parts(table))


def _lookup_name(table: exp.Table, known: Mapping[str, object]) -> str | None:
    parts = [p.name.lower() for p in _name_parts(table)]
    for index in range(len(parts)):
        name = ".".join(parts[index:])
        if name in known:
            return name
    return None


def _lookup_constraints(table: exp.Table, constraints: Mapping[str, TableConstraints]) -> TableConstraints | None:
    name = _lookup_name(table, constraints)
    return constraints.get(name) if name else None
