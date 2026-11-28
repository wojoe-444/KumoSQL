"""Partition pruning: a filter on a partitioned table that BigQuery cannot use to skip partitions.

BigQuery reads only the partitions a filter selects, but only when the filter has a shape it recognizes. A filter such as
``CAST(event_date AS STRING) IN ('2024-01-01')`` returns the right rows and still reads every partition of the table,
and on a table created with ``require_partition_filter`` BigQuery refuses to run it. This module finds those filters from
the query text and the table's partitioning, and suggests the rewrite when it is mechanical
(``event_date IN (DATE '2024-01-01')``).

The rules come from Google's page "Query partitioned tables" (:data:`DOC_URL`, read 2026-11-28) and were checked with free
dry runs on two public partitioned tables on the same day (``tools/partition_pruning_evidence.py``; the measurements are in
``docs/partition-pruning.md``). Each rule below quotes the page; "measured" means a dry run showed it. A shape that neither
the page nor a dry run settles is never reported: it is a gap (``unknown``), and a partitioning that is not known is
silence, never a guess.

==================== ===============================================================================================
rule                 what it finds
==================== ===============================================================================================
``function``         the partition column inside a function or cast BigQuery does not prune through
                     (``CAST(d AS STRING)``, ``FORMAT_DATE``, ``EXTRACT(MONTH ...)``, ``DATETIME(ts)``, ``IFNULL``)
``arithmetic``       arithmetic on the partition column (``ts + INTERVAL 1 DAY > ...``)
``dynamic_value``    the column compared with a value BigQuery does not know when it plans the query: a subquery that
                     reads a table, ``IN (SELECT ...)``, another column
``or_other_column``  an ``OR`` with a branch that does not filter the partition column
``or_mixed``         (tables that require a partition filter) an ``OR`` whose branches each filter the column but also
                     other columns: it prunes, yet BigQuery does not accept it as the required filter
``outer_join_on``    the only filter on the column is in the ``ON`` of an outer join
``other_column``     the table is filtered, but never on its partition column
``no_filter``        (tables that require a partition filter) no filter at all
==================== ===============================================================================================

The quotes, from :data:`DOC_URL`:

* constant values: "To limit the partitions that are scanned in a query, filter the partitioning column using a constant
  expression, rather than a dynamic expression." and, for ``WHERE t1.ts = (SELECT timestamp FROM table3 WHERE key = 2)``,
  "This query compares the partitioning column to a dynamic value, what prevents partition pruning."
* functions: "isolate the partitioning column on one side of a comparison operator" ... "The following built-in functions
  on the partitioning column support partition pruning, if their additional arguments are constant: DATE_ADD, DATE_DIFF,
  DATE_SUB, DATE_TRUNC, EXTRACT with YEAR part, DATETIME_DIFF, TIMESTAMP_ADD, TIMESTAMP_DIFF, TIMESTAMP_SUB,
  TIMESTAMP_TRUNC, EXTRACT with DATE or YEAR parts, FORMAT_TIMESTAMP with the following format specifiers: %F, %Y-%m-%d and
  %Y%m%d." and "Other functions and complex mathematical operations will require a full table scan." Its examples that
  prune include ``DATE(datehour)`` and ``CAST(datehour AS DATE)``.
* arithmetic: "a query with the following predicate doesn't prune partitions because it manipulates the partitioning column
  with an arithmetic operation: WHERE ts + INTERVAL 1 DAY > CURRENT_TIMESTAMP()".
* integer range: "Partition pruning is not supported for functions over an integer range partitioned column."
* OR: "If AND is changed to OR, partition pruning won't work".
* required filter: "Cannot query over table 'project_id.dataset.table' without a filter that can be used for partition
  elimination" and "There must be at least one predicate that only references a partitioning column".

Measured beyond the page (2026-11-28): ``CAST(d AS TIMESTAMP)``, ``TIMESTAMP(d)`` and ``DATE(ts, 'zone')`` prune; a scalar
subquery with no ``FROM`` (``(SELECT DATE '2024-01-01')``) prunes; ``SAFE_CAST``, ``FORMAT_DATE``, ``DATETIME(ts)``,
``UNIX_*``, ``IFNULL`` do not; ``IN (SELECT ...)`` never prunes; a join on the partition column, even with a constant filter
on the other table, does not count as a partition filter; ``ON`` of an ``INNER JOIN`` counts and ``ON`` of a ``LEFT JOIN``
does not; a filter outside a CTE or subquery is pushed into it, through a renamed column, a ``GROUP BY`` or ``HAVING`` on
the column, but not through a window partitioned by another column; ``(d = A AND x) OR (d = B AND y)`` prunes but does not
satisfy ``require_partition_filter``.

Entry points: :func:`check_tree` (a parsed query and a table lookup), :func:`check_sql`, :func:`session_lookup` (the
partitioning ``check_query`` knows: schema snapshot, Dataform config, saved table metadata) and :func:`project_findings`
(every model of the project, for ``project_checks``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable

import sqlglot
from sqlglot import exp

DOC_URL = "https://cloud.google.com/bigquery/docs/querying-partitioned-tables"

#: BigQuery's refusal for a table that requires a partition filter (the text the documentation gives)
REFUSAL = "Cannot query over table ... without a filter that can be used for partition elimination"

PRUNES, NO, UNKNOWN = "prunes", "no", "unknown"
_PSEUDO = ("_partitiontime", "_partitiondate")
_FORMATS = frozenset({"%F", "%Y-%m-%d", "%Y%m%d"})  # FORMAT_TIMESTAMP specifiers the page lists
_COMPARE = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv, exp.Neg)
#: functions the page lists as keeping pruning (their other arguments must be constant)
_SUPPORTED = (exp.DateAdd, exp.DateSub, exp.DateDiff, exp.DateTrunc, exp.TimestampAdd, exp.TimestampSub, exp.TimestampDiff,
              exp.TimestampTrunc, exp.DatetimeDiff, exp.Date)
#: functions measured not to prune (2026-11-28), besides casts and FORMAT_*
_UNSUPPORTED = (exp.String, exp.TsOrDsToDatetime, exp.Coalesce, exp.UnixDate, exp.UnixSeconds)
_FLIP = {exp.LT: exp.GT, exp.LTE: exp.GTE, exp.GT: exp.LT, exp.GTE: exp.LTE, exp.EQ: exp.EQ, exp.NEQ: exp.NEQ}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: the loader's placeholder for a masked Dataform expression (``${...}``): whatever it compiles to is not known here
_TEMPLATE = re.compile(r"__sqlx_token_\d+__")

#: rule -> why it does not prune, as the finding says it (the quote is in the module docstring and the docs page)
WHY = {
    "function": "is not one of the functions BigQuery prunes through",
    "arithmetic": "arithmetic on the column stops pruning: isolate the column on one side",
    "subquery": "the value comes from a subquery that reads a table, unknown when BigQuery plans the query",
    "in_subquery": "IN (SELECT ...) is never pruned",
    "column": "it is compared with another column, not a constant",
    "or_other_column": "a branch of the OR does not filter the column",
    "or_mixed": "its OR branches also read other columns",
    "outer_join_on": "a condition in the ON of an outer join does not prune",
    "other_column": "no filter on the partition column",
    "no_filter": "no filter on the partition column",
}


@dataclass(frozen=True)
class Partitioning:
    """How a table is partitioned, as KumoSQL knows it; never guessed."""

    #: the partitioning column as BigQuery names it, ``_PARTITIONTIME`` for ingestion-time partitioning
    column: str
    #: ``DATE``, ``TIMESTAMP``, ``DATETIME`` or ``INT64``; ``None`` when not known
    column_type: str | None = None
    #: whether queries must carry a partition filter (``require_partition_filter``); ``None`` when not known
    require_filter: bool | None = None
    #: where it was read: ``schema snapshot``, ``Dataform config``, ``BigQuery table metadata``
    source: str = ""

    @property
    def names(self) -> frozenset[str]:
        """The names a filter on the partitioning may use (both pseudo-columns for ingestion-time partitioning)."""

        name = self.column.casefold()
        return frozenset(_PSEUDO) if name in _PSEUDO else frozenset({name})

    @property
    def integer_range(self) -> bool:
        return (self.column_type or "").upper() in ("INT64", "INTEGER")


@dataclass(frozen=True)
class Issue:
    """One filter (or missing filter) that does not let BigQuery prune a table."""

    table: str
    partitioning: Partitioning
    rule: str
    #: ``error`` when BigQuery refuses the query (the table requires a partition filter), else ``warning``
    level: str
    #: the offending predicate as written (``None`` for ``no_filter``)
    filter: str | None
    detail: str
    rewrite: str | None = None

    @property
    def check(self) -> str:
        return "partition_filter_required" if self.level == "error" else "partition_pruning"

    def what(self) -> str:
        spec = self.partitioning
        head = f"{self.table} is partitioned on {spec.column}" + (f" ({spec.source})" if spec.source else "")
        if self.level == "error":
            head += ", requires a partition filter"
        outcome = "BigQuery refuses the query" if self.level == "error" else "BigQuery reads every partition"
        shown = _clip(self.filter or "", 80)
        if self.rule == "no_filter":
            return f"{head}: the query has no filter on {spec.column}; {outcome}"
        if self.rule == "other_column":
            return f"{head}: the query filters it ({shown}) but never on {spec.column}; {outcome}"
        if self.rule == "or_mixed":
            return f"{head}: {shown} is not accepted as the partition filter ({self.detail}); {outcome}"
        return f"{head}: {shown} does not prune ({self.detail}); {outcome}"

    def fix(self) -> str:
        column = self.partitioning.column
        if self.rewrite and self.rule == "dynamic_value":
            return f"Also filter {column} itself: add {self.rewrite} (the join alone does not prune)."
        if self.rewrite:
            return f"Write {self.rewrite} instead (same rows; BigQuery prunes on it)."
        if self.rule == "dynamic_value":
            return f"Compare {column} with constants (the values themselves, or CURRENT_DATE() arithmetic); look a value up before the query, not in it."
        if self.rule == "or_other_column":
            return f"Add a condition on {column} that every branch meets outside the OR (... AND {column} BETWEEN ...), or split the OR into UNION ALL branches."
        if self.rule == "outer_join_on":
            return f"If the condition is meant to limit this table's rows, filter it before the join: (SELECT * FROM table WHERE <the {column} condition>) AS alias."
        if self.rule in ("other_column", "no_filter"):
            return f"Add a filter on {column} with constant bounds ({column} >= ..., BETWEEN ... AND ...)" + ("; BigQuery refuses the query without one." if self.level == "error" else ".")
        return f"Compare {column} itself (or through DATE, DATE_TRUNC, TIMESTAMP_TRUNC) with constants."

    def finding(self) -> dict:
        return {"check": self.check, "level": self.level, "rule": self.rule, "what": _clip(self.what(), 300), "fix": _clip(self.fix(), 240)}


Lookup = Callable[[exp.Table], "Partitioning | None"]


# ------------------------------------------------------------------------------------------------ entry points


def check_sql(sql: str, lookup: Callable[[str], Partitioning | None]) -> tuple[list[Issue], list[tuple[str, str]]]:
    """:func:`check_tree` on SQL text; ``lookup`` takes the table name as spelled (lower case, dot-separated)."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    return check_tree(tree, lambda table: lookup(_spelled(table)))


def check_tree(tree: exp.Expression, lookup: Lookup, variables: Iterable[str] = ()) -> tuple[list[Issue], list[tuple[str, str]]]:
    """``(issues, gaps)`` for every read of a partitioned table in ``tree``.

    ``lookup`` gives the partitioning of a table reference, or ``None`` when it is not known (then nothing is said about
    it). ``variables`` are script variable names (``DECLARE``) that a filter may compare with: they are constants.
    """

    ctes = {cte.alias.casefold(): cte for cte in tree.find_all(exp.CTE) if cte.alias}
    names = frozenset(v.casefold() for v in variables)
    issues: list[Issue] = []
    gaps: list[tuple[str, str]] = []
    seen: set[tuple] = set()
    for select in tree.find_all(exp.Select):
        sources = _sources(select)
        for source in sources:
            if not isinstance(source, exp.Table) or not source.name:
                continue
            if not source.args.get("db") and source.name.casefold() in ctes:
                continue
            spec = lookup(source)
            if spec is None:
                continue
            scan = _Scan(source, select, spec, len(sources) == 1, names)
            result = _judge(scan, ctes)
            if isinstance(result, Issue):
                key = (result.table, result.rule, result.filter)
                if key not in seen:
                    seen.add(key)
                    issues.append(result)
            elif result is not None:
                gaps.append(result)
    issues.sort(key=lambda i: i.level != "error")
    return issues, gaps


# ------------------------------------------------------------------------------------------------ the scan


class _Scan:
    """One read of a partitioned table in one ``SELECT``."""

    def __init__(self, table: exp.Expression, select: exp.Select, spec: Partitioning, sole: bool, variables: frozenset[str], alias: str | None = None, names: frozenset[str] | None = None):
        self.table = table
        self.select = select
        self.spec = spec
        self.sole = sole
        self.variables = variables
        self.alias = (alias if alias is not None else (table.alias_or_name or "")).casefold()
        self.names = names if names is not None else spec.names
        #: aliases of FROM-less subqueries in the same FROM (``JOIN (SELECT DATE '...' AS d) c``): their columns are constants
        self.constants = frozenset(_source_name(s) for s in _sources(select) if _constant_row(s)) if isinstance(select, exp.Select) else frozenset()

    def owns(self, column: exp.Column) -> str:
        """``yes``, ``maybe`` (an unqualified partition column name with other tables in scope) or ``no``."""

        if _TEMPLATE.fullmatch(column.name):
            return "maybe"  # a masked Dataform expression may stand for the partition column itself
        if column.name.casefold() not in self.names:
            return "no"
        if column.table:
            return "yes" if column.table.casefold() == self.alias else "no"
        return "yes" if self.sole else "maybe"

    def label(self) -> str:
        if isinstance(self.table, exp.Table):
            parts = [p.name for p in (self.table.args.get("db"), self.table.this) if p is not None and p.name]
            return ".".join(parts) or self.table.name
        return self.alias


@dataclass(frozen=True)
class _V:
    kind: str  # PRUNES, NO, UNKNOWN
    rule: str | None = None
    node: exp.Expression | None = None
    detail: str = ""
    touches: bool = False  # the predicate reads the partition column
    pure: bool = True  # it reads no other column
    side: "_Side | None" = None


@dataclass(frozen=True)
class _Side:
    kind: str  # direct, supported, function, arithmetic, unknown
    label: str = ""
    layers: tuple[exp.Expression, ...] = ()
    column: exp.Column | None = None


def _judge(scan: _Scan, ctes: dict) -> Issue | tuple[str, str] | None:
    counted, outer_on = _conditions(scan)
    strict = bool(scan.spec.require_filter)
    verdicts = [_predicate(c, scan, strict) for c in counted]
    combined = _and(verdicts)
    if combined.kind == PRUNES:
        return None
    if _pushed_down(scan, ctes):
        return None  # a filter outside the CTE or subquery may reach the table (measured through renames and GROUP BY)
    level = "error" if strict else "warning"
    if combined.kind == UNKNOWN:
        node = next((v.node for v in verdicts if v.kind == UNKNOWN and v.node is not None), None)
        text = _clip(_text(node), 60) if node is not None else scan.spec.column
        return (f"whether {scan.label()} is pruned on {scan.spec.column}", f"the filter {text} has a shape neither BigQuery's documentation nor a dry run here settles; check_query dry_run=true shows the bytes")
    touching = [v for v in verdicts if v.kind == NO and v.touches]
    if touching:
        first = touching[0]
        return Issue(scan.label(), scan.spec, first.rule or "function", level, _text(first.node), first.detail, _rewrite(first, scan))
    for item in outer_on:
        verdict = _predicate(item, scan, strict)
        if verdict.kind == PRUNES:
            return Issue(scan.label(), scan.spec, "outer_join_on", level, _text(item), WHY["outer_join_on"])
    own = [c for c in counted if _filters_scan(c, scan)]
    if own:
        return Issue(scan.label(), scan.spec, "other_column", level, _text(own[0]), WHY["other_column"])
    if strict:
        return Issue(scan.label(), scan.spec, "no_filter", level, None, WHY["no_filter"])
    return None


def _conditions(scan: _Scan) -> tuple[list[exp.Expression], list[exp.Expression]]:
    """``(conjuncts that can prune the scan, conjuncts of outer-join ON clauses)``."""

    select = scan.select
    counted: list[exp.Expression] = []
    outer: list[exp.Expression] = []
    where = select.args.get("where")
    if where is not None:
        counted += _flatten(where.this, exp.And)
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is None:
            continue
        if (join.side or "").upper() in ("LEFT", "RIGHT", "FULL"):
            outer += _flatten(on, exp.And)  # measured: an outer join's ON does not prune either side
        else:
            counted += _flatten(on, exp.And)
    having = select.args.get("having")
    if having is not None:
        counted += _flatten(having.this, exp.And)  # measured: HAVING on the grouped column is pushed down
    return counted, outer


def _filters_scan(node: exp.Expression, scan: _Scan) -> bool:
    """Whether a conjunct compares a column of this table with a constant (a filter on the table, not a join)."""

    if not isinstance(node, (*_COMPARE, exp.In, exp.Between, exp.Like, exp.Is)):
        return False
    if any(isinstance(n, (exp.Subquery, exp.Select)) for n in node.walk()):
        return False
    columns = _columns(node)

    def mine(column: exp.Column) -> bool:
        return column.table.casefold() == scan.alias if column.table else scan.sole

    return bool(columns) and all(mine(c) for c in columns)


# ------------------------------------------------------------------------------------------------ predicates


def _and(verdicts: list[_V]) -> _V:
    if any(v.kind == PRUNES for v in verdicts):
        return next(v for v in verdicts if v.kind == PRUNES)
    if any(v.kind == UNKNOWN for v in verdicts):
        return next(v for v in verdicts if v.kind == UNKNOWN)
    touching = [v for v in verdicts if v.touches]
    return touching[0] if touching else _V(NO, touches=False, pure=False)


def _predicate(node: exp.Expression, scan: _Scan, strict: bool) -> _V:
    """Whether one predicate lets BigQuery prune the scan (``strict``: and counts as the required partition filter)."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        kids = [_predicate(k, scan, strict) for k in _flatten(node, exp.And)]
        verdict = _and(kids)
        return _V(verdict.kind, verdict.rule, verdict.node if verdict.kind != PRUNES else node, verdict.detail,
                  any(k.touches for k in kids), all(k.pure for k in kids), verdict.side)
    if isinstance(node, exp.Or):
        branches = _flatten(node, exp.Or)
        kids = [_predicate(b, scan, strict) for b in branches]
        touches = any(k.touches for k in kids)
        pure = all(k.pure for k in kids)
        if not touches:
            return _V(NO, touches=False, pure=False)
        failing = next((k for k in kids if k.kind == NO), None)
        if failing is not None:
            if not failing.touches:
                return _V(NO, "or_other_column", node, WHY["or_other_column"], True, pure)
            return _V(NO, failing.rule, node, failing.detail, True, pure, failing.side)
        if any(k.kind == UNKNOWN for k in kids):
            return _V(UNKNOWN, node=node, touches=True, pure=pure)
        if strict and not pure:
            return _V(NO, "or_mixed", node, WHY["or_mixed"], True, False)  # measured: prunes, but is not the required filter
        return _V(PRUNES, node=node, touches=True, pure=pure)
    if isinstance(node, exp.Not):
        inner = node.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if isinstance(inner, (*_COMPARE, exp.In, exp.Between, exp.Is)):
            verdict = _predicate(inner, scan, strict)
            return _V(verdict.kind, verdict.rule, node, verdict.detail, verdict.touches, verdict.pure, verdict.side)  # measured: NOT (ts < c) prunes
        return _unknown_if_touched(node, scan)
    return _atom(node, scan)


def _unknown_if_touched(node: exp.Expression, scan: _Scan) -> _V:
    columns = _columns(node)
    owned = [scan.owns(c) for c in columns]
    if not any(o != "no" for o in owned):
        return _V(NO, touches=False, pure=False)
    return _V(UNKNOWN, node=node, touches=True, pure=all(o != "no" for o in owned))


def _atom(node: exp.Expression, scan: _Scan) -> _V:
    columns = _columns(node)
    owned = {id(c): scan.owns(c) for c in columns}
    touched = [c for c in columns if owned[id(c)] != "no"]
    if not touched:
        return _V(NO, touches=False, pure=False)
    pure = all(owned[id(c)] != "no" for c in columns)
    maybe = any(owned[id(c)] == "maybe" for c in touched)

    def result(kind: str, rule: str | None = None, detail: str = "", side: _Side | None = None) -> _V:
        if maybe and kind == NO:
            kind = UNKNOWN  # the column may belong to another table of the FROM: never report what is not certain
        return _V(kind, rule, node, detail, True, pure, side)

    if isinstance(node, _COMPARE):
        left, right = node.this, node.expression
        in_left, in_right = _touches(left, scan), _touches(right, scan)
        if in_left and in_right:
            return result(UNKNOWN)
        column_side, others = (left, [right]) if in_left else (right, [left])
    elif isinstance(node, exp.Between):
        if _touches(node.args.get("low"), scan) or _touches(node.args.get("high"), scan):
            return result(UNKNOWN)
        column_side, others = node.this, [node.args.get("low"), node.args.get("high")]
    elif isinstance(node, exp.In):
        column_side = node.this
        if not _touches(column_side, scan):
            return result(UNKNOWN)
        if node.args.get("query") is not None:
            side = _column_side(column_side, scan)
            if side.kind in ("function", "arithmetic"):
                return result(NO, side.kind, _side_detail(side), side)
            return result(NO, "dynamic_value", WHY["in_subquery"], side)
        unnest = node.args.get("unnest")
        others = list(unnest.expressions) if unnest is not None else list(node.expressions)
    elif isinstance(node, exp.Is):
        column_side, others = node.this, []
    elif isinstance(node, (exp.Like, exp.ILike, exp.RegexpLike)) or isinstance(node, exp.Func):
        side = _column_side(_touching_argument(node, scan), scan)
        if side.kind in ("function", "arithmetic"):
            return result(NO, side.kind, _side_detail(side), side)
        return result(UNKNOWN)
    else:
        return result(UNKNOWN)

    side = _column_side(column_side, scan)
    if side.kind in ("function", "arithmetic"):
        return result(NO, side.kind, _side_detail(side), side)
    value = _constness([o for o in others if o is not None], scan)
    if value == "subquery":
        return result(NO, "dynamic_value", WHY["subquery"], side)
    if value == "column":
        return result(NO, "dynamic_value", WHY["column"], side)
    if value == "unknown" or side.kind == "unknown":
        return result(UNKNOWN, side=side)
    return result(PRUNES, side=side)


def _touches(node: exp.Expression | None, scan: _Scan) -> bool:
    return node is not None and any(scan.owns(c) != "no" for c in _columns(node))


def _touching_argument(node: exp.Expression, scan: _Scan) -> exp.Expression:
    for child in node.iter_expressions():
        if _touches(child, scan):
            return child
    return node


def _column_side(node: exp.Expression, scan: _Scan) -> _Side:
    """How the side of a comparison that holds the partition column treats it: ``direct`` (the column alone),
    ``supported`` (only functions that keep pruning), ``function``, ``arithmetic`` or ``unknown``."""

    while isinstance(node, exp.Paren):
        node = node.this
    owned = [c for c in _columns(node) if scan.owns(c) != "no"]
    if len(owned) != 1:
        return _Side("unknown")
    column = owned[0]
    if column is node:
        return _Side("direct", column=column)
    layers = []
    current: exp.Expression = column
    while current is not node and current.parent is not None:
        child, current = current, current.parent
        layers.append((current, child))
    worst, label = "supported", ""
    rank = {"supported": 0, "unknown": 1, "function": 2, "arithmetic": 2}
    for layer, child in layers:
        kind, text = _layer(layer, child, scan)
        if kind == "transparent":
            continue
        if kind == "supported" and not _other_arguments_constant(layer, child, scan):
            kind, text = "function", f"{text} with an argument that is not constant"
        if rank[kind] > rank[worst] or (rank[kind] == rank[worst] == 2 and not label):
            worst, label = kind, text
    return _Side(worst, label, tuple(layer for layer, _child in layers), column)


def _layer(node: exp.Expression, child: exp.Expression, scan: _Scan) -> tuple[str, str]:
    """``(kind, label)`` of one function or operator around the partition column."""

    if isinstance(node, (exp.Paren, exp.Alias)):
        return "transparent", ""
    if isinstance(node, _ARITHMETIC):
        return "arithmetic", "arithmetic"
    if scan.spec.integer_range:
        return "function", _function_name(node)  # "Partition pruning is not supported for functions over an integer range"
    if isinstance(node, (exp.TsOrDsToTimestamp, exp.TsOrDsToDate)) and isinstance(node.parent, exp.TimeToStr):
        return "transparent", ""
    if isinstance(node, exp.TimeToStr):
        name = _function_name(node)
        fmt = node.args.get("format")
        spec = fmt.name if isinstance(fmt, exp.Literal) else None
        if name == "FORMAT_TIMESTAMP":
            if node.args.get("zone") is None and spec in _FORMATS:
                return "supported", name
            return "function", f"FORMAT_TIMESTAMP with {spec!r}" if spec else name
        if name == "FORMAT_DATE":
            return "function", "FORMAT_DATE"  # not on the page's list; measured: no pruning with '%Y%m%d', '%Y-%m-%d', '%Y-%m'
        return "unknown", name
    if isinstance(node, exp.Cast):
        target = node.to.sql(dialect="bigquery").upper() if isinstance(node.to, exp.DataType) else ""
        safe = isinstance(node, exp.TryCast) or bool(node.args.get("safe"))
        label = f"{'SAFE_CAST' if safe else 'CAST'}(... AS {target})"
        if target in ("STRING", "BYTES", "INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64", "BOOL", "JSON") or target.startswith(("STRING(", "BYTES(")):
            return "function", label
        if target == "DATE":
            return ("function", label) if safe else ("supported", label)  # measured: SAFE_CAST(ts AS DATE) does not prune
        if target == "TIMESTAMP" and not safe and (scan.spec.column_type or "").upper() == "DATE":
            return "supported", label  # measured on a DATE column
        return "unknown", label
    if isinstance(node, exp.Extract):
        part = node.this.name.upper() if isinstance(node.this, (exp.Var, exp.Identifier, exp.Literal)) else ""
        if part == "YEAR" or (part == "DATE" and (scan.spec.column_type or "").upper() != "DATE"):
            return "supported", f"EXTRACT({part} ...)"
        return "function", f"EXTRACT({part} ...)"
    if isinstance(node, exp.Timestamp):
        if (scan.spec.column_type or "").upper() == "DATE" and len([a for a in node.iter_expressions()]) == 1:
            return "supported", "TIMESTAMP(...)"  # measured on a DATE column
        return "unknown", "TIMESTAMP(...)"
    if isinstance(node, _SUPPORTED):
        return "supported", _function_name(node)
    if isinstance(node, _UNSUPPORTED):
        return "function", _function_name(node)
    return "unknown", _function_name(node)


def _other_arguments_constant(layer: exp.Expression, child: exp.Expression, scan: _Scan) -> bool:
    for value in layer.iter_expressions():
        if value is child or isinstance(value, (exp.DataType, exp.Var)):
            continue
        if _constness([value], scan) != "constant":
            return False
    return True


def _constness(nodes: list[exp.Expression], scan: _Scan | None, variables: frozenset[str] = frozenset()) -> str:
    """``constant``, ``subquery`` (reads a table), ``column`` or ``unknown`` for the values a column is compared with."""

    worst = "constant"
    for node in nodes:
        found = _const_of(node, scan.variables if scan is not None else variables, scan.constants if scan is not None else frozenset())
        if found in ("subquery", "column"):
            return found
        if found == "unknown":
            worst = "unknown"
    return worst


def _constant_row(source: exp.Expression) -> bool:
    """A subquery with no FROM (one row of constants), which BigQuery folds into the query (measured)."""

    query = source.this if isinstance(source, exp.Subquery) else None
    return (isinstance(query, exp.Select) and query.args.get("from_") is None and query.args.get("from") is None
            and not query.args.get("joins") and _constness(list(query.expressions), None) == "constant")


def _source_name(source: exp.Expression) -> str:
    return (source.alias_or_name or "").casefold() if hasattr(source, "alias_or_name") else ""


def _const_of(node: exp.Expression, variables: frozenset[str], constants: frozenset[str] = frozenset()) -> str:
    if isinstance(node, (exp.Subquery, exp.Select)):
        query = node.this if isinstance(node, exp.Subquery) else node
        if not isinstance(query, exp.Select) or query.args.get("from_") is not None or query.args.get("from") is not None or query.args.get("joins"):
            return "subquery"
        # a scalar subquery with no FROM is folded into a constant (measured)
        return _constness(list(query.expressions), None, variables)
    if isinstance(node, exp.Column):
        if _TEMPLATE.fullmatch(node.name):
            return "unknown"  # a Dataform ${...} expression: often a constant, but its text is not known here
        if not node.table and node.name.casefold() in variables:
            return "constant"
        if node.table and node.table.casefold() in constants:
            return "constant"  # a column of a FROM-less subquery joined in (measured: BigQuery folds it and prunes)
        return "column"
    if isinstance(node, (exp.Anonymous, exp.Rand)):
        return "unknown"
    worst = "constant"
    for child in node.iter_expressions():
        found = _const_of(child, variables, constants)
        if found in ("subquery", "column"):
            return found
        if found == "unknown":
            worst = "unknown"
    return worst


def _side_detail(side: _Side) -> str:
    if side.kind == "arithmetic":
        return WHY["arithmetic"]
    return f"{side.label} {WHY['function']}"


# ------------------------------------------------------------------------------------------------ pushdown


def _pushed_down(scan: _Scan, ctes: dict, depth: int = 0) -> bool:
    """Whether a query that reads this scan's rows (as a subquery or a CTE) has a filter that may reach it.

    Conservative: any filter of a reader that compares a column of the derived table with constants counts (a renamed
    partition column is pushed down: measured), except a column that is an aggregate or a window value, and any column
    when the reader uses a window of the derived table that is not partitioned by it (measured: not pushed; a window
    nothing reads is dropped by BigQuery, and the filter is pushed)."""

    derived = scan.select
    for reader, alias in _readers(scan.select, ctes):
        windows = _windows_read(derived, reader, alias)
        counted, _outer = _conditions(_Scan(scan.table, reader, scan.spec, False, scan.variables, alias=alias))
        for atom in counted:
            for column in _columns(atom):
                if column.table and column.table.casefold() != alias:
                    continue
                if not _pushable(derived, column.name.casefold(), windows):
                    continue
                probe = _Scan(scan.table, reader, scan.spec, True, scan.variables, alias=alias, names=frozenset({column.name.casefold()}))
                if _predicate(atom, probe, False).kind == PRUNES:
                    return True
        if depth < 6 and _pushed_down(_Scan(scan.table, reader, scan.spec, False, scan.variables), ctes, depth + 1):
            return True
    return False


def _windows_read(derived: exp.Expression, reader: exp.Select, alias: str) -> list[exp.Window]:
    """The windows of ``derived``'s output that ``reader`` reads (all of them when it reads ``*``)."""

    if not isinstance(derived, exp.Select):
        return []
    read = {c.name.casefold() for c in reader.find_all(exp.Column) if not c.table or c.table.casefold() == alias}
    star = any(isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)) for item in reader.expressions)
    found = []
    for item in derived.expressions:
        if star or (item.alias_or_name or "").casefold() in read:
            found += list(item.find_all(exp.Window))
    return found


def _pushable(derived: exp.Expression, name: str, windows: list[exp.Window]) -> bool:
    """Whether a filter on output ``name`` of ``derived`` can be pushed below it."""

    if not isinstance(derived, exp.Select):
        return True
    found = None
    for item in derived.expressions:
        if not isinstance(item, exp.Star) and (item.alias_or_name or "").casefold() == name:
            found = item.this if isinstance(item, exp.Alias) else item
            break
    if found is not None and (found.find(exp.AggFunc) is not None or found.find(exp.Window) is not None):
        return False
    if windows:
        source = found.name.casefold() if isinstance(found, exp.Column) else name
        for window in windows:
            keys = {c.name.casefold() for p in (window.args.get("partition_by") or []) for c in p.find_all(exp.Column)}
            if source not in keys:
                return False
    return True


def _readers(select: exp.Expression, ctes: dict) -> list[tuple[exp.Select, str]]:
    """``(select, alias)`` for every ``SELECT`` that reads the rows of ``select`` as a derived table."""

    node = select
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, exp.CTE):
            name = (parent.alias or "").casefold()
            found = []
            for cte_reader in (parent.parent.parent if isinstance(parent.parent, exp.With) else parent).find_all(exp.Select):
                for source in _sources(cte_reader):
                    if isinstance(source, exp.Table) and not source.args.get("db") and source.name.casefold() == name:
                        found.append((cte_reader, (source.alias_or_name or name).casefold()))
            return found
        if isinstance(parent, (exp.From, exp.Join)) and isinstance(parent.parent, exp.Select):
            alias = node.alias_or_name if isinstance(node, exp.Subquery) else ""
            return [(parent.parent, (alias or "").casefold())]
        if isinstance(parent, (exp.Subquery, exp.SetOperation, exp.Paren)):
            node = parent
            continue
        return []
    return []


# ------------------------------------------------------------------------------------------------ rewrites


def _rewrite(verdict: _V, scan: _Scan) -> str | None:
    """A pruning-friendly predicate with the same rows, when it is mechanical; ``None`` otherwise."""

    node = verdict.node
    try:
        if verdict.rule == "or_mixed" and isinstance(node, exp.Or):
            return _or_rewrite(node, scan)
        side = verdict.side
        if side is None or side.column is None or node is None:
            return None
        if verdict.rule == "function":
            return _function_rewrite(node, side, scan)
        if verdict.rule == "arithmetic":
            return _arithmetic_rewrite(node, side, scan)
        if verdict.rule == "dynamic_value":
            return _transitive_rewrite(node, side, scan)
    except Exception:  # noqa: BLE001 - a rewrite is a convenience; the finding stands without it
        return None
    return None


def _column_text(side: _Side) -> str:
    return side.column.sql(dialect="bigquery")


def _parts(node: exp.Expression, side: _Side):
    """``(operator class, values)`` of a comparison whose column side is ``side``, with the operator as seen from the column."""

    if isinstance(node, _COMPARE):
        column_first = side.column in set(node.this.walk())
        value = node.expression if column_first else node.this
        op = type(node) if column_first else _FLIP[type(node)]
        return op, [value]
    if isinstance(node, exp.In) and not node.args.get("query") and not node.args.get("unnest"):
        return exp.In, list(node.expressions)
    if isinstance(node, exp.Between):
        return exp.Between, [node.args["low"], node.args["high"]]
    return None, []


def _emit(column: str, op, values: list[str]) -> str:
    if op is exp.In:
        return f"{column} IN ({', '.join(values)})"
    if op is exp.Between:
        return f"{column} BETWEEN {values[0]} AND {values[1]}"
    symbol = {exp.EQ: "=", exp.NEQ: "!=", exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}[op]
    return f"{column} {symbol} {values[0]}"


def _function_rewrite(node: exp.Expression, side: _Side, scan: _Scan) -> str | None:
    """``CAST(d AS STRING) = '2024-01-01'`` -> ``d = DATE '2024-01-01'``; ``FORMAT_DATE('%Y%m%d', d)`` and friends likewise."""

    layers = [layer for layer in side.layers if not isinstance(layer, (exp.Paren, exp.TsOrDsToDate, exp.TsOrDsToTimestamp))]
    if len(layers) != 1:
        return None
    layer = layers[0]
    op, values = _parts(node, side)
    if op is None or not values or not all(isinstance(v, exp.Literal) and v.is_string for v in values):
        return None
    texts = [v.name for v in values]
    column = _column_text(side)
    kind = (scan.spec.column_type or "").upper()
    if kind == "DATE" and isinstance(layer, exp.Cast) and layer.to.sql(dialect="bigquery").upper() == "STRING":
        days = [_day(t) for t in texts]
        return _emit(column, op, [f"DATE '{d}'" for d in days]) if all(days) else None
    if isinstance(layer, exp.TimeToStr) and isinstance(layer.args.get("format"), exp.Literal) and layer.args.get("zone") is None:
        fmt = layer.args["format"].name
        name = _function_name(layer)
        if kind == "DATE" and name == "FORMAT_DATE":
            if fmt in _FORMATS:
                days = [_day(t if fmt != "%Y%m%d" else f"{t[:4]}-{t[4:6]}-{t[6:]}") if len(t) in (8, 10) else None for t in texts]
                return _emit(column, op, [f"DATE '{d}'" for d in days]) if all(days) else None
            if fmt in ("%Y-%m", "%Y%m"):
                months = [_day((t if fmt == "%Y-%m" else f"{t[:4]}-{t[4:]}") + "-01") if len(t) in (6, 7) else None for t in texts]
                return _emit(f"DATE_TRUNC({column}, MONTH)", op, [f"DATE '{d}'" for d in months]) if all(months) else None
        if kind == "TIMESTAMP" and name == "FORMAT_TIMESTAMP" and fmt in ("%Y-%m-%d %H", "%F %H") and op in (exp.EQ, exp.In):
            hours = [_hour(t) for t in texts]
            return _emit(f"TIMESTAMP_TRUNC({column}, HOUR)", op, [f"TIMESTAMP '{h}'" for h in hours]) if all(hours) else None
    return None


def _arithmetic_rewrite(node: exp.Expression, side: _Side, scan: _Scan) -> str | None:
    """``ts + INTERVAL 1 DAY > c`` -> ``ts > TIMESTAMP_SUB(c, INTERVAL 1 DAY)``; ``id + 1 BETWEEN 30 AND 50`` -> ``id BETWEEN 29 AND 49``."""

    layers = [layer for layer in side.layers if not isinstance(layer, exp.Paren)]
    if len(layers) != 1 or not isinstance(layers[0], (exp.Add, exp.Sub)):
        return None
    arithmetic = layers[0]
    column = _column_text(side)
    column_left = side.column in set(arithmetic.this.walk())
    shift = arithmetic.expression if column_left else arithmetic.this
    if isinstance(arithmetic, exp.Sub) and not column_left:
        return None  # c - ts: the sign of the column flips; leave it to the analyst
    adding = isinstance(arithmetic, exp.Add)
    op, values = _parts(node, side)
    if op is None or op is exp.In:
        return None
    kind = (scan.spec.column_type or "").upper()
    if scan.spec.integer_range:
        if not (isinstance(shift, exp.Literal) and not shift.is_string and shift.name.lstrip("-").isdigit()):
            return None
        step = int(shift.name)
        if not all(isinstance(v, exp.Literal) and not v.is_string and v.name.lstrip("-").isdigit() for v in values):
            return None
        moved = [str(int(v.name) - step if adding else int(v.name) + step) for v in values]
        return _emit(column, op, moved)
    if not isinstance(shift, exp.Interval):
        return None
    interval = _text(shift)
    if kind == "TIMESTAMP":
        function = "TIMESTAMP_SUB" if adding else "TIMESTAMP_ADD"
    elif kind == "DATE" and all(isinstance(v, exp.Cast) and v.to.sql(dialect="bigquery").upper() in ("DATE", "DATETIME") for v in values):
        function = ("DATE_SUB" if adding else "DATE_ADD") if all(v.to.sql(dialect="bigquery").upper() == "DATE" for v in values) else ("DATETIME_SUB" if adding else "DATETIME_ADD")
    else:
        return None
    moved = [f"{function}({_literal_text(v)}, {interval})" for v in values]
    return _emit(column, op, moved)


def _transitive_rewrite(node: exp.Expression, side: _Side, scan: _Scan) -> str | None:
    """``e.d = c.d`` with ``c.d = DATE '2024-01-01'`` in the same query -> ``e.d = DATE '2024-01-01'`` (to add)."""

    if not isinstance(node, exp.EQ) or side.kind != "direct":
        return None
    other = node.expression if side.column in set(node.this.walk()) else node.this
    if not isinstance(other, exp.Column):
        return None
    key = (other.table.casefold(), other.name.casefold())
    counted, _outer = _conditions(scan)
    for conjunct in counted:
        if conjunct is node or not isinstance(conjunct, (*_COMPARE, exp.In, exp.Between)):
            continue
        columns = _columns(conjunct)
        if len(columns) != 1 or (columns[0].table.casefold(), columns[0].name.casefold()) != key:
            continue
        if any(isinstance(n, (exp.Subquery, exp.Select)) for n in conjunct.walk()):
            continue
        copy = conjunct.copy()
        for column in list(copy.find_all(exp.Column)):
            column.replace(side.column.copy())
        return _text(copy)
    return None


def _or_rewrite(node: exp.Or, scan: _Scan) -> str | None:
    """``(d = A AND x) OR (d = B AND y)`` -> ``(d = A OR d = B) AND ((d = A AND x) OR (d = B AND y))``."""

    parts = []
    for branch in _flatten(node, exp.Or):
        conjuncts = _flatten(branch.this if isinstance(branch, exp.Paren) else branch, exp.And)
        own = [c for c in conjuncts if _predicate(c, scan, False).kind == PRUNES and _predicate(c, scan, False).pure]
        if not own:
            return None
        parts.append(" AND ".join(_text(c) for c in own))
    text = f"({' OR '.join(parts)}) AND ({_text(node)})"
    return text if len(text) <= 180 else None


def _literal_text(node: exp.Expression) -> str:
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
        return f"{node.to.sql(dialect='bigquery').upper()} '{node.this.name}'"
    return node.sql(dialect="bigquery")


def _day(text: str) -> str | None:
    if not _ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _hour(text: str) -> str | None:
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{2})", text)
    if not match or not _day(match.group(1)) or int(match.group(2)) > 23:
        return None
    return f"{match.group(1)} {match.group(2)}:00:00"


# ------------------------------------------------------------------------------------------------ helpers


def _sources(select: exp.Select) -> list[exp.Expression]:
    found = []
    source = select.args.get("from_") or select.args.get("from")
    if source is not None and source.this is not None:
        found.append(source.this)
    for join in select.args.get("joins") or []:
        if join.this is not None:
            found.append(join.this)
    return found


def _flatten(node: exp.Expression, kind: type) -> list[exp.Expression]:
    while isinstance(node, exp.Paren) and isinstance(node.this, kind):
        node = node.this
    if isinstance(node, kind):
        return _flatten(node.this, kind) + _flatten(node.expression, kind)
    return [node]


def _columns(node: exp.Expression) -> list[exp.Column]:
    """The columns a predicate reads in its own scope (not inside a subquery)."""

    found: list[exp.Column] = []

    def walk(item: exp.Expression) -> None:
        if isinstance(item, exp.Column):
            if not isinstance(item.this, exp.Star):
                found.append(item)
            return
        if isinstance(item, (exp.Subquery, exp.Select)) and item is not node:
            return
        for child in item.iter_expressions():
            walk(child)

    walk(node)
    return found


def _spelled(table: exp.Table) -> str:
    return ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name).casefold()


def _function_name(node: exp.Expression) -> str:
    try:
        text = node.sql(dialect="bigquery")
    except Exception:  # noqa: BLE001
        return type(node).__name__.upper()
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\(", text)
    return match.group(1).upper() if match else type(node).__name__.upper()


def _text(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    try:
        text = node.sql(dialect="bigquery")
    except Exception:  # noqa: BLE001
        return str(node)
    # typed literals and intervals read back as an analyst writes them
    text = re.sub(r"CAST\('([^']*)' AS (DATE|TIMESTAMP|DATETIME)\)", r"\2 '\1'", text)
    return re.sub(r"INTERVAL '(-?\d+)' ", r"INTERVAL \1 ", text)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ------------------------------------------------------------------------------------------------ what KumoSQL knows


#: model kinds that are stored tables with a partitioning of their own
_STORED = ("table", "incremental")


def partitioning_from_expression(expression: str, column_type: Callable[[str], str | None] = lambda _c: None) -> Partitioning | None:
    """The partitioning a Dataform ``partitionBy`` expression gives (``DATE(created_at)`` -> ``created_at``), or ``None``."""

    try:
        tree = sqlglot.parse_one(expression, read="bigquery")
    except Exception:  # noqa: BLE001
        return None
    columns = list({c.name.casefold(): c.name for c in tree.find_all(exp.Column)}.values())
    if len(columns) != 1:
        return None
    column = columns[0]
    known = (column_type(column) or "").upper() or None
    if column.casefold() in _PSEUDO:
        return Partitioning(column.upper(), "TIMESTAMP" if column.casefold() == "_partitiontime" else "DATE")
    if isinstance(tree, exp.Column):
        return Partitioning(column, known)
    if isinstance(tree, exp.TimestampTrunc):
        return Partitioning(column, "TIMESTAMP")
    if isinstance(tree, exp.DatetimeTrunc):
        return Partitioning(column, "DATETIME")
    if isinstance(tree, (exp.Date, exp.DateTrunc)) or (isinstance(tree, exp.Cast) and tree.to.sql(dialect="bigquery").upper() == "DATE"):
        return Partitioning(column, known)
    if _function_name(tree) == "RANGE_BUCKET":
        return Partitioning(column, "INT64")
    return None


def _type_text(gtype) -> str | None:
    if gtype is None:
        return None
    text = str(gtype).upper()
    for name in ("DATETIME", "TIMESTAMP", "DATE", "INT64"):
        if text == name:
            return name
    return None


def _catalog_partitioning(name: str) -> Partitioning | None:
    """From the saved BigQuery table metadata (the opt-in schema fetch keeps it); never calls BigQuery."""

    parts = [p.strip("`") for p in name.split(".")]
    if len(parts) != 3 or not all(parts):
        return None
    from . import bigquery_catalog, schema_fetch

    entry = bigquery_catalog.peek(schema_fetch._key(*parts))
    data = entry.get("data") if isinstance(entry, dict) else None
    layout = data.get("partitioning") if isinstance(data, dict) else None
    if not isinstance(layout, dict) or not layout.get("column"):
        return None
    return Partitioning(str(layout["column"]), layout.get("column_type"), layout.get("require_filter"), "BigQuery table metadata")


def _snapshot_partitioning(session, key: str) -> tuple[Partitioning | None, bool]:
    """``(partitioning, known)`` from the schema snapshot; ``known`` is true when the snapshot says the table is not partitioned."""

    snapshot = session.schema_snapshot()
    if snapshot is None or not snapshot.has_partitioning:
        return None, False
    rows = session.column_types().snapshot_rows.get(key)
    if not rows or all(r.is_partitioning is None for r in rows):
        return None, False
    columns = [r for r in rows if r.is_partitioning]
    require = next((r.require_partition_filter for r in rows if r.require_partition_filter is not None), None)
    if len(columns) == 1:
        found = columns[0]
        kind = found.data_type.upper().split("<")[0].strip()
        return Partitioning(found.column, kind if kind in ("DATE", "TIMESTAMP", "DATETIME", "INT64") else None, require, "schema snapshot"), True
    if not columns:
        return None, True
    if {c.column.casefold() for c in columns} <= set(_PSEUDO):
        return Partitioning("_PARTITIONTIME", "TIMESTAMP", require, "schema snapshot"), True
    return None, False


def _config_partitioning(session, key: str) -> Partitioning | None:
    from . import mcp_deployed

    layout = mcp_deployed._config_layout(session, key)
    if layout.problem or not layout.partition_by:
        return None
    spec = partitioning_from_expression(layout.partition_by, lambda column: _type_text(session.column_type(key, column)))
    if spec is None:
        return None
    return Partitioning(spec.column, spec.column_type, layout.require_partition_filter, "Dataform config")


def table_partitioning(session, key: str | None, spelled: str = "") -> Partitioning | None:
    """What KumoSQL knows about how a table is partitioned: the schema snapshot, the model's Dataform config, the saved table
    metadata. ``None`` when none of them says, or when they disagree (``deployed_status`` reports that drift)."""

    cache = session._once("partitioning", dict) if hasattr(session, "_once") else {}
    marker = (key, spelled)
    if marker in cache:
        return cache[marker]
    found = None
    try:
        found = _table_partitioning(session, key, spelled)
    except Exception:  # noqa: BLE001 - an unreadable source of partitioning is silence, never a finding
        found = None
    cache[marker] = found
    return found


def _table_partitioning(session, key: str | None, spelled: str) -> Partitioning | None:
    snapshot, says = (None, False)
    config = None
    if key:
        snapshot, says = _snapshot_partitioning(session, key)
        pipeline = session.pipeline()
        model = pipeline.models.get(key)
        if model is not None and model.kind in _STORED:
            config = _config_partitioning(session, key)
    catalog = _catalog_partitioning(key or spelled) if (key or spelled) else None
    if snapshot is not None and config is not None and snapshot.names != config.names:
        return None
    if says and snapshot is None:
        return None  # the deployed table is not partitioned (whatever the config says until it is rebuilt)
    chosen = snapshot or config or catalog
    if chosen is None:
        return None
    requires = [s.require_filter for s in (snapshot, config, catalog) if s is not None and s.names == chosen.names]
    require = True if True in requires else False if False in requires else None
    kind = chosen.column_type or next((s.column_type for s in (snapshot, config, catalog) if s is not None and s.names == chosen.names and s.column_type), None)
    return Partitioning(chosen.column, kind, require, chosen.source)


def session_lookup(session, tables: dict[str, str | None]) -> Lookup:
    """The partitioning of the tables a ``check_query`` draft reads (``tables``: spelling -> project key or ``None``)."""

    def lookup(table: exp.Table) -> Partitioning | None:
        spelled = _spelled(table)
        if spelled not in tables:
            return None
        return table_partitioning(session, tables[spelled], spelled)

    return lookup


@dataclass(frozen=True)
class ProjectFinding:
    model: str
    table: str
    issue: Issue


def _declared(statements: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for text in statements:
        for match in re.finditer(r"(?i)\bDECLARE\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*)", text or ""):
            names.update(part.strip() for part in match.group(1).split(","))
    return names


def project_findings(session) -> list[ProjectFinding]:
    """Every model of the project that reads a partitioned table without a filter BigQuery prunes with (once per session)."""

    return session._once("partition_findings", lambda: _project_findings(session))


def _project_findings(session) -> list[ProjectFinding]:
    pipeline = session.pipeline()
    parsed = pipeline._analyse().parsed
    found: list[ProjectFinding] = []
    reads = pipeline.table_reads()
    for key in sorted(pipeline.models):
        model = pipeline.models[key]
        if model.disabled or key not in parsed:
            continue
        upstream = reads.get(key)
        if upstream is not None and not any(table_partitioning(session, k) for k in upstream if k != key and (k in pipeline.models or k in pipeline.sources)):
            continue
        variables = _declared(model.operations_sql)

        def lookup(table: exp.Table, key=key) -> Partitioning | None:
            try:
                target = pipeline.resolve(table)
            except Exception:  # noqa: BLE001
                return None
            if not target or target == key:
                return None
            return table_partitioning(session, target)

        try:
            issues, _gaps = check_tree(parsed[key], lookup, variables)
        except Exception:  # noqa: BLE001 - one model failing must not hide the others
            continue
        for issue in issues:
            found.append(ProjectFinding(key, issue.table, issue))
    return found
