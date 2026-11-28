"""Which full-refresh tables could become incremental, by which pattern, and what blocks the rest.

``python -m kumosql incremental-report PROJECT --advise`` reads every ``type: "table"`` action under
``definitions/`` and asks, for each table it reads (its *parent*), how an incremental run could
recompute only what changed in that parent:

* ``partition_aligned``: the table's partition column is the parent's partition (or watermark)
  column passed through row by row, directly or through ``DATE()``, also through CTEs and
  subqueries, and every ``GROUP BY``, window and ``DISTINCT`` that reads the parent includes it. A
  run rewrites only the newest partitions.
* ``partition_other_column``: the declared partition column is another column of the parent (an
  ``order_date`` while the parent changes by ``updated_at``), so the partitions a run rewrites are
  the ones the changed rows fall in, old and new.
* ``keyed_merge``: every ``GROUP BY``, window and ``DISTINCT`` that reads the parent is keyed by one
  parent column that is in the output, or the query keeps rows one by one together with the
  parent's unique key. A run recomputes only the keys that changed and merges on ``uniqueKey``.
* ``outer_join_dimension``: the parent is the nullable side of one ``LEFT`` or ``RIGHT`` join on one
  equality, in a plain row-by-row query. A run recomputes the rows whose join key changed.

Each pattern needs the parent to have a *watermark*, a column that tells new or changed rows from
old ones: its ``partitionBy`` column, a ``TIMESTAMP`` column of ``--source-schema``, or a column named
like one (``*_at``, ``*_ts``), in that order. A parent without one is the ``no_watermark`` blocker.

The patterns and their soundness conditions are ported from KingyoOrchestrator's incremental
analysis (``analysis.py``, ``flatten.py``, ``outer_join.py``, and the advice of its ``check``
command); whatever they cannot show is a named blocker, never a guess.

**This is advice, not a verdict.** Nothing here says an incremental version would equal the table.
For a single-parent table whose shape has a Dataform rendering, the advice sketches the conversion
(the ``uniqueKey`` and the ``when(incremental(), ...)`` condition) and asks
:func:`kumosql.incremental.prove` about the sketch on the sources ``incremental-report`` would
infer. When a proof rule applies, the advice says the conversion is "checkable with
incremental-report once converted"; the check that counts is ``incremental-report`` on the
converted model. Otherwise it says that only the counterexample search applies, which is bounded
evidence.

Cost figures (``--table-bytes``) follow :mod:`kumosql.savings`: a modelled ``estimate`` with its
assumptions and an ``upper_bound`` are reported apart, never added, and nothing is ``measured``
until a deployed change has before and after windows of real runs. Nothing here runs a query.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope

from kumo_sql_prover.ast_utils import binding_cte
from kumo_sql_prover.sqlx import split_sqlx_sections

from .cost_model import MIN_BYTES_PER_TABLE
from .incremental import _STRING, IncrementalError, IncrementalModel, _config_list, _js_string, _projected_as, _resolve, prove
from .incremental_rules import _truncation_of
from .incremental_scan import CONTRACTS, _target_name, guess_type, infer_sources
from .resilience import extended_path
from .savings import DEFAULT_UNIT, SavingClaim

PATTERNS = ("partition_aligned", "partition_other_column", "keyed_merge", "outer_join_dimension")
STATUSES = ("candidate", "partial", "blocked", "no_suggestion")

_FIXES = {
    "aggregate_without_key": "group by a key column that you also select, or by the partition column; a small table can stay a full refresh",
    "group_not_keyed": "group by the partition column, or by one key column that you also select",
    "window_over_whole_table": "PARTITION BY the partition column or one selected key column",
    "window_not_keyed": "PARTITION BY the partition column or one selected key column",
    "distinct_not_keyed": "keep the partition column or a key column in the DISTINCT",
    "limit": "avoid LIMIT/OFFSET; for the latest row per key use ROW_NUMBER() OVER (PARTITION BY key ...) = 1",
    "read_twice": "read the changed table once: no self-join, no CTE over it used twice, no subquery over it beside the FROM",
    "subquery_expression": "move the subquery over the changed table into a join or a separate model",
    "set_operation": "split the branches into separate models, or keep the table a full refresh",
    "nullable_side": "join the changed table with one equality, in a plain row-by-row SELECT",
    "no_watermark": "give {parent} a load or update timestamp, or declare its partitionBy",
    "no_unique_key": "keep a unique key of {parent} in the output (declare it as uniqueKey), or group by a selected key",
    "partition_outside_parent": "derive the partition column from the changed table only",
    "unresolved_partition": "select the partition column straight from the parent (no aggregate or struct on it), or key the model",
    "nondeterministic": "move the random or clock-dependent part out of the query; a clock value as an output column is fine (name it in ignore_columns)",
    "not_analysed": "",
}

_RANDOM = tuple(getattr(exp, name) for name in ("Rand", "Uuid") if hasattr(exp, name))
_CLOCK = (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime) + ((exp.CurrentTime,) if hasattr(exp, "CurrentTime") else ())
_ORDER_FREE = (exp.Sum, exp.Count, exp.Max, exp.Min, exp.Avg, exp.CountIf)
_TYPE = re.compile(r"\btype\s*:\s*[\"'`](\w+)[\"'`]")
_PARTITION = re.compile(rf"\bpartitionBy\s*:\s*({_STRING})")
_OLD = "TIMESTAMP '1970-01-01'"
_SELF = "${self()}"


@dataclass(frozen=True)
class Blocker:
    """Why a pattern does not apply, concretely, and what would change that."""

    code: str
    message: str
    fix: str = ""

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message, "fix": self.fix}


def _blocker(code: str, message: str, parent: str = "") -> Blocker:
    return Blocker(code, message, _FIXES.get(code, "").replace("{parent}", parent))


@dataclass(frozen=True)
class EdgeFit:
    """How the table could follow changes in one parent: a pattern, or the blockers."""

    parent: str
    pattern: str | None = None
    detail: str = ""
    partition: str | None = None  # the table's partitionBy: declared, or suggested (``partition_suggested``)
    partition_suggested: bool = False
    unique_key: tuple[str, ...] = ()  # merge on these output columns; empty: append
    watermark: str | None = None
    watermark_basis: str = ""
    key: str | None = None  # the parent column changes are recomputed by (keyed and outer-join patterns)
    key_basis: str = ""
    preserved: str | None = None  # outer_join_dimension: the table whose rows are recomputed
    blockers: tuple[Blocker, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def fits(self) -> bool:
        return self.pattern is not None and not self.blockers

    def to_json(self) -> dict:
        row = {
            "parent": self.parent,
            "pattern": self.pattern,
            "fits": self.fits,
            "detail": self.detail,
            "partition": self.partition,
            "partition_suggested": self.partition_suggested,
            "unique_key": list(self.unique_key),
            "watermark": self.watermark,
            "watermark_basis": self.watermark_basis,
            "blockers": [b.to_json() for b in self.blockers],
        }
        if self.key:
            row["key"], row["key_basis"] = self.key, self.key_basis
        if self.preserved:
            row["preserved"] = self.preserved
        if self.notes:
            row["notes"] = list(self.notes)
        return row


@dataclass
class FitAdvice:
    """The advice for one full-refresh table."""

    model: str
    path: str
    status: str  # candidate | partial | blocked | no_suggestion
    edges: list[EdgeFit] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)  # about the whole query, whichever parent changes
    notes: list[str] = field(default_factory=list)
    check: dict | None = None  # the sketched conversion and which proof rules apply to it
    cost: dict | None = None

    def to_json(self) -> dict:
        return {
            "model": self.model,
            "path": self.path,
            "status": self.status,
            "patterns": {e.parent: e.pattern for e in self.edges if e.fits},
            "parents": [e.to_json() for e in self.edges],
            "blockers": [b.to_json() for b in self.blockers],
            "check": self.check,
            "cost": self.cost,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class CostAssumptions:
    """What the cost estimate assumes; printed with every figure (Kingyo's KPI model)."""

    changed_fraction: float = 1 / 365  # share of a parent's partitions with new or changed rows per run
    late_spread: float = 2.0  # partition_other_column: table partitions touched per changed parent partition
    key_span: float = 3.0  # keyed and outer-join: partitions holding the old and new rows of the changed keys

    def __post_init__(self) -> None:
        if not 0 < self.changed_fraction <= 1:
            raise ValueError("changed_fraction must be in (0, 1]")
        if self.late_spread < 0 or self.key_span < 0:
            raise ValueError("late_spread and key_span cannot be negative")

    def to_json(self) -> dict:
        return {"changed_fraction": self.changed_fraction, "late_spread": self.late_spread, "key_span": self.key_span}


# --------------------------------------------------------------------------- project


@dataclass(frozen=True)
class Action:
    """One Dataform action as the advisor needs it. ``kind`` ``source`` is a table the project does not define."""

    name: str
    kind: str
    path: str = ""
    sql: str = ""
    partition_by: str | None = None
    unique_key: tuple[str, ...] = ()
    error: str = ""


def read_project(root: str | Path) -> dict[str, Action]:
    """The project's actions by lower-case name, with ``${ref()}`` resolved to the bare table name."""

    root = extended_path(root)
    base = root / "definitions" if (root / "definitions").is_dir() else root
    actions: dict[str, Action] = {}
    for path in sorted(base.rglob("*.sqlx")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sections = split_sqlx_sections(text)
        config = next((t for kind, t in sections if kind == "block" and t.lstrip().startswith("config")), "")
        found = _TYPE.search(config)
        if not found:
            continue
        name = _target_name(text, path)
        partition = _PARTITION.search(config)
        action = Action(name, found.group(1).lower(), path.relative_to(root).as_posix(),
                        partition_by=_js_string(partition.group(1)).strip() if partition else None,
                        unique_key=_config_list(config, "uniqueKey"))
        if action.kind in ("table", "view", "incremental"):
            body = "".join(t for kind, t in sections if kind == "sql").strip()
            try:
                action = replace(action, sql=_resolve(body, name, False).strip().rstrip(";"))
            except IncrementalError as exc:
                action = replace(action, error=str(exc))
        actions[name.lower()] = action
    return actions


def _output_columns(action: Action) -> dict[str, str]:
    if not action.sql:
        return {}
    try:
        tree = sqlglot.parse_one(action.sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return {}
    if not isinstance(tree, exp.Select) or any(isinstance(p, exp.Star) or isinstance(p.this, exp.Star) for p in tree.selects):
        return {}
    return {p.alias_or_name: "" for p in tree.selects if p.alias_or_name}


def _known_columns(action: Action, overrides: dict | None) -> dict[str, str]:
    for name, columns in (overrides or {}).items():
        if name.lower() == action.name.lower():
            return dict(columns)
    return _output_columns(action)


def _watermark(parent: Action, columns: dict[str, str], read: list[str]) -> tuple[str | None, str]:
    if parent.partition_by:
        try:
            node = sqlglot.parse_one(parent.partition_by, read="bigquery")
        except sqlglot.errors.SqlglotError:
            node = None
        found = list(node.find_all(exp.Column)) if node is not None else []
        if len(found) == 1 and (node is found[0] or _truncation_of(node, found[0].name)):
            return found[0].name, "partitionBy"

    def pick(names: list[str]) -> str:
        read_lower = {c.lower() for c in read}
        return next((n for n in names if n.lower() in read_lower), names[0])

    typed = [c for c, t in columns.items() if (t or "").upper().startswith(("TIMESTAMP", "DATETIME"))]
    if typed:
        return pick(typed), "source schema"
    named = list(dict.fromkeys(c for c in (*columns, *read) if guess_type(c) == "TIMESTAMP"))
    if named:
        return pick(named), "column name"
    return None, ""


def _parent_key(parent: Action, columns: dict[str, str], read: list[str]) -> tuple[str | None, str]:
    if len(parent.unique_key) == 1:
        return parent.unique_key[0], "declared uniqueKey"
    if not parent.unique_key and "id" in {c.lower() for c in (*columns, *read)}:
        return "id", "assumed: a column named id, as incremental-report assumes"
    return None, ""


# --------------------------------------------------------------------------- query analysis


def _from(select: exp.Expression):
    return select.args.get("from_") or select.args.get("from")


def _sources(scope: Scope) -> dict:
    """alias -> Table | Scope that the scope actually selects from (``Scope.sources`` also lists every visible CTE)."""

    return {alias: source for alias, (_, source) in scope.selected_sources.items()}


def _norm(node: exp.Expression) -> str:
    def strip(n):
        if isinstance(n, exp.Column):
            return exp.column(n.name.lower())
        if isinstance(n, exp.Date) and isinstance(n.this, exp.Date):
            return n.this
        return n

    return node.copy().transform(strip).sql(dialect="bigquery").lower()


def _show(node: exp.Expression) -> str:
    """SQL for a message, without the table qualifiers the analysis added."""

    return node.copy().transform(lambda n: exp.column(n.name) if isinstance(n, exp.Column) and not isinstance(n.this, exp.Star) else n).sql(dialect="bigquery")


def _over_watermark(node: exp.Expression, watermark: str | None) -> bool:
    if watermark is None:
        return False
    return (isinstance(node, exp.Column) and node.name.lower() == watermark.lower()) or _truncation_of(node, watermark)


def _conjuncts(node):
    if node is None:
        return []
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


class _Query:
    """One table's query, qualified, seen from one parent: which scopes read it and how."""

    def __init__(self, sql: str, parent: str, schema: dict[str, dict[str, str]]):
        tree = sqlglot.parse_one(sql, read="bigquery")
        self.tree = qualify(tree, schema=schema or None, dialect="bigquery", quote_identifiers=False, identify=False,
                            validate_qualify_columns=False)
        self.scopes = traverse_scope(self.tree)
        if not self.scopes:
            raise ValueError("no query scopes")
        self.root = self.scopes[-1]
        self.name = parent
        self.parent = parent.lower()
        count: dict[int, int] = {}
        for sc in self.scopes:  # children before parents; a CTE that reads the parent, used twice, counts twice
            if sc.scope_type == ScopeType.SET_OPERATION:
                count[id(sc)] = sum(count.get(id(u), 0) for u in sc.union_scopes)
                continue
            count[id(sc)] = sum(
                (src.name.lower() == self.parent) if isinstance(src, exp.Table) else count.get(id(src), 0)
                for src in _sources(sc).values()
            )
        self.count = count
        self.reading = [sc for sc in self.scopes if count[id(sc)]]
        self.occurrences = sum(1 for t in self.tree.find_all(exp.Table) if t.name.lower() == self.parent)

    def B(self, code: str, message: str) -> Blocker:  # noqa: N802
        return _blocker(code, message, self.name)

    def read_columns(self) -> list[str]:
        out: dict[str, None] = {}
        for sc in self.scopes:
            for column in sc.columns:
                src = _sources(sc).get(column.table) if column.table else None
                if src is None and not column.table and len(_sources(sc)) == 1:
                    src = next(iter(_sources(sc).values()))
                if isinstance(src, exp.Table) and src.name.lower() == self.parent:
                    out.setdefault(column.name, None)
        return list(out)

    def _reads(self, src) -> bool:
        return (isinstance(src, exp.Table) and src.name.lower() == self.parent) or (
            isinstance(src, Scope) and self.count.get(id(src), 0) > 0
        )

    # ---- shapes no pattern survives
    def shape(self) -> Blocker | None:
        p = self.name
        if self.occurrences > 1 or self.count[id(self.root)] > 1:
            n = max(self.occurrences, self.count[id(self.root)])
            return self.B("read_twice", f"{p} is read {n} times (a self-join, a CTE over it used twice, or a subquery over it)")
        for sc in self.reading:
            if sc.scope_type == ScopeType.SET_OPERATION or not isinstance(sc.expression, exp.Select):
                return self.B("set_operation", f"{p} is read through a set operation (UNION, INTERSECT or EXCEPT)")
            if sc.scope_type in (ScopeType.SUBQUERY, ScopeType.UDTF):
                return self.B("subquery_expression", f"{p} is read inside a subquery expression (in WHERE, the SELECT list or EXISTS)")
        if self.count[id(self.root)] == 0:
            return self.B("subquery_expression", f"{p} is read in a way the analysis does not follow")
        for sc in self.reading:
            if sc.expression.args.get("limit") or sc.expression.args.get("offset"):
                return self.B("limit", f"LIMIT/OFFSET over {p}: which rows survive depends on the whole table")
        return None

    # ---- provenance: an expression over the parent's own columns (Kingyo flatten.resolve)
    def _col(self, c: exp.Column, sc: Scope, why: list[str]):
        srcs = _sources(sc)
        src = srcs.get(c.table) if c.table else (next(iter(srcs.values())) if len(srcs) == 1 else None)
        if isinstance(src, exp.Table):
            if src.name.lower() == self.parent:
                return exp.column(c.name)
            why.append("other_table")
            return None
        if not isinstance(src, Scope) or not isinstance(src.expression, exp.Select):
            return None
        select = src.expression
        for p in select.selects:
            if not isinstance(p, exp.Star) and p.alias_or_name.lower() == c.name.lower():
                return self.resolve(p.unalias(), src, why)
        stars = [p for p in select.selects if isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))]
        if len(stars) == 1 and len(_sources(src)) == 1:  # SELECT * over one source passes the column through
            return self.resolve(exp.column(c.name, table=next(iter(_sources(src)))), src, why)
        return None

    def resolve(self, node: exp.Expression, sc: Scope, why: list[str] | None = None):
        why = [] if why is None else why
        if node.find(exp.AggFunc, exp.Window):
            why.append("aggregate")
            return None
        if node.find(exp.Subquery, exp.Select):
            why.append("subquery")
            return None
        node = node.copy()
        if isinstance(node, exp.Column):
            return self._col(node, sc, why)
        for c in list(node.find_all(exp.Column)):
            r = self._col(c, sc, why)
            if r is None:
                return None
            c.replace(r)
        return node

    def _key_expr(self, key: exp.Expression, sc: Scope) -> exp.Expression:
        if isinstance(key, exp.Literal) and key.is_int and isinstance(sc.expression, exp.Select):
            selects = sc.expression.selects
            index = int(key.name) - 1
            if 0 <= index < len(selects):
                return selects[index].unalias()
        return key

    def _keyset(self, keys, sc: Scope) -> set[str]:
        out = set()
        for key in keys:
            r = self.resolve(self._key_expr(key, sc), sc)
            if isinstance(r, exp.Column):
                out.add(r.name.lower())
        return out

    def _window_blocker(self, window: exp.Window) -> Blocker:
        fn = _show(window.this) if window.this is not None else "a window function"
        if not window.args.get("partition_by"):
            return self.B("window_over_whole_table", f"non-monotone window over the whole table: {fn} OVER (...) has no "
                          "PARTITION BY, so one new row can change every row")
        cols = ", ".join(_show(p) for p in window.args["partition_by"])
        return self.B("window_not_keyed", f"{fn} is partitioned by {cols}, which is neither the partition column nor a "
                      f"column of {self.name} kept in the output")

    def _nullable(self, sc: Scope) -> Blocker | None:
        e = sc.expression
        srcs = [_from(e).this] if _from(e) is not None else []
        for join in e.args.get("joins") or []:
            side = (join.side or "").upper()
            nulled = ([join.this] if side in ("LEFT", "FULL") else []) + (srcs if side in ("RIGHT", "FULL") else [])
            if any(self._reads(_sources(sc).get(n.alias_or_name)) for n in nulled):
                return self.B("nullable_side", f"{self.name} is on the nullable side of an outer join")
            srcs.append(join.this)
        return None

    @staticmethod
    def _own(node: exp.Expression, select: exp.Expression) -> bool:
        return node.find_ancestor(exp.Select) is select

    def _global_aggregate(self, e: exp.Expression) -> exp.Expression | None:
        return next((a for a in e.find_all(exp.AggFunc) if self._own(a, e) and not a.find_ancestor(exp.Window)), None)

    # ---- partition patterns (Kingyo flatten.analyze_partition_scoped)
    def aligned_partition(self, parent_partition: str | None, watermark: str | None) -> str | None:
        """A partitionBy for an unpartitioned table: an output column that carries the parent's partition or watermark."""

        try:
            ppart = sqlglot.parse_one(parent_partition, read="bigquery") if parent_partition else None
        except sqlglot.errors.SqlglotError:
            ppart = None
        if not isinstance(self.root.expression, exp.Select):
            return None
        for p in self.root.expression.selects:
            if isinstance(p, exp.Star) or not p.alias_or_name:
                continue
            r = self.resolve(p.unalias(), self.root)
            if r is None:
                continue
            if isinstance(r, exp.Column) and watermark and r.name.lower() == watermark.lower():
                return f"DATE({p.alias_or_name})"
            if (ppart is not None and _norm(r) == _norm(ppart)) or _truncation_of(r, watermark or ""):
                return p.alias_or_name
        return None

    def partition(self, partition_by: str, parent_partition: str | None, watermark: str | None):
        """``("aligned" | "other", expression over the parent)`` or a Blocker."""

        pexpr = sqlglot.parse_one(partition_by, read="bigquery")
        top = {p.alias_or_name.lower(): p.unalias() for p in self.root.expression.selects if not isinstance(p, exp.Star)}
        columns = list(pexpr.find_all(exp.Column))
        if not columns:
            return self.B("unresolved_partition", f"the partition expression {partition_by} names no column")
        for c in columns:
            why: list[str] = []
            inner = top.get(c.name.lower())
            r = self.resolve(inner, self.root, why) if inner is not None else self._col(exp.column(c.name), self.root, why)
            if r is None:
                if "other_table" in why:
                    return self.B("partition_outside_parent", f"the partition column {c.name} comes from a table other than {self.name}")
                if "aggregate" in why:
                    return self.B("unresolved_partition", f"the partition column {c.name} is computed by an aggregate or window")
                return self.B("unresolved_partition", f"the partition column {c.name} cannot be traced row by row to {self.name}")
            if c is pexpr:
                pexpr = r
            else:
                c.replace(r)
        over = pexpr
        pcols = {c.name.lower() for c in over.find_all(exp.Column)}
        pnorm = _norm(over)

        def keyed(keys, sc) -> bool:
            have_cols, have_expr = set(), False
            for key in keys:
                r = self.resolve(self._key_expr(key, sc), sc)
                if r is None:
                    continue
                have_expr |= _norm(r) == pnorm
                if isinstance(r, exp.Column):
                    have_cols.add(r.name.lower())
            return have_expr or pcols <= have_cols

        for sc in self.reading:
            e = sc.expression
            group = e.args.get("group")
            if group:
                if not keyed(group.expressions, sc):
                    return self.B("group_not_keyed", f"GROUP BY does not include the partition column ({partition_by})")
            else:
                agg = self._global_aggregate(e)
                if agg is not None:
                    return self.B("aggregate_without_key", f"aggregate without a partition key: {_show(agg)} "
                                  f"over all of {self.name} with no GROUP BY")
            for window in (w for w in e.find_all(exp.Window) if self._own(w, e)):
                if not keyed(window.args.get("partition_by") or [], sc):
                    return self._window_blocker(window)
            if e.args.get("distinct") and not keyed([p.unalias() for p in e.selects if not isinstance(p, exp.Star)], sc):
                return self.B("distinct_not_keyed", "SELECT DISTINCT does not include the partition column")
            blocker = self._nullable(sc)
            if blocker:
                return blocker
        try:
            ppart = sqlglot.parse_one(parent_partition, read="bigquery") if parent_partition else None
        except sqlglot.errors.SqlglotError:
            ppart = None
        aligned = (ppart is not None and pnorm == _norm(ppart)) or (
            _over_watermark(over, watermark) and (ppart is None or _over_watermark(ppart, watermark)))
        return ("aligned" if aligned else "other"), over

    def merge_key(self) -> tuple[str, ...] | None:
        """Output names of the outermost GROUP BY's keys over the parent: ``()`` with no GROUP BY, None when a key is not kept.

        Each key and each output column is traced to the parent's columns, so a GROUP BY in a CTE whose keys the
        query passes through still gives the output names. Whether a join above it keeps them unique is not checked.
        """

        groups = [sc for sc in self.reading if isinstance(sc.expression, exp.Select) and sc.expression.args.get("group")]
        if not groups:
            return ()
        scope = groups[-1]  # scopes come children first: the last is the outermost
        if not isinstance(self.root.expression, exp.Select):
            return None
        outputs: dict[str, str] = {}
        for p in self.root.expression.selects:
            if isinstance(p, exp.Star):
                continue
            r = self.resolve(p.unalias(), self.root)
            if r is not None:
                outputs.setdefault(_norm(r), p.alias_or_name)
        names = []
        for key in scope.expression.args["group"].expressions:
            r = self.resolve(self._key_expr(key, scope), scope)
            if r is None or _norm(r) not in outputs:
                return None
            names.append(outputs[_norm(r)])
        return tuple(dict.fromkeys(names))

    def exposed(self) -> dict[str, str]:
        """Parent column (lower case) -> output name, for output columns that are a parent column passed through."""

        out: dict[str, str] = {}
        if not isinstance(self.root.expression, exp.Select):
            return out
        for p in self.root.expression.selects:
            if isinstance(p, exp.Star):
                continue
            r = self.resolve(p.unalias(), self.root)
            if isinstance(r, exp.Column):
                out.setdefault(r.name.lower(), p.alias_or_name)
        return out

    def _dedup_filtered(self) -> bool:
        """A ``ROW_NUMBER() ... = 1`` keeps one row per window partition somewhere in the query."""

        numbered = {
            p.alias_or_name.lower()
            for p in self.tree.find_all(exp.Alias)
            if isinstance(p.this, exp.Window) and isinstance(p.this.this, exp.RowNumber)
        }
        for eq in self.tree.find_all(exp.EQ):
            sides = (eq.this, eq.expression)
            if not any(isinstance(s, exp.Literal) and s.name == "1" for s in sides):
                continue
            for s in sides:
                if isinstance(s, exp.Window) and isinstance(s.this, exp.RowNumber):
                    return True
                if isinstance(s, exp.Column) and s.name.lower() in numbered:
                    return True
        return False

    # ---- keyed pattern (Kingyo analysis._analyze_keyed), with a merge key Dataform can use
    def keyed(self, parent_key: str | None):
        """``(parent key column, merge key)`` or a Blocker."""

        constraints: list[tuple[str, str, set[str]]] = []
        for sc in self.reading:
            e = sc.expression
            group = e.args.get("group")
            if group:
                constraints.append(("group_not_keyed", "GROUP BY " + ", ".join(_show(g) for g in group.expressions),
                                    self._keyset(group.expressions, sc)))
            else:
                agg = self._global_aggregate(e)
                if agg is not None:
                    return self.B("aggregate_without_key", f"aggregate without a partition key: {_show(agg)} "
                                  f"over all of {self.name} with no GROUP BY, so any change rewrites the one result row")
            for window in (w for w in e.find_all(exp.Window) if self._own(w, e)):
                if not window.args.get("partition_by"):
                    return self._window_blocker(window)
                constraints.append(("window_not_keyed", _show(window.this) if window.this else "window",
                                    self._keyset(window.args["partition_by"], sc)))
            if e.args.get("distinct"):
                constraints.append(("distinct_not_keyed", "SELECT DISTINCT",
                                    self._keyset([p.unalias() for p in e.selects if not isinstance(p, exp.Star)], sc)))
            blocker = self._nullable(sc)
            if blocker:
                return blocker
        exposed = self.exposed()
        keys = set(exposed)
        for code, what, keyset in constraints:
            if not keys & keyset:
                if code == "window_not_keyed":
                    return self.B(code, f"{what} is partitioned by columns that are not a column of {self.name} kept in the output")
                return self.B(code, f"{what} is not keyed by a column of {self.name} that is kept in the output")
            keys &= keyset
        pk = parent_key.lower() if parent_key else None
        grouped = any(code == "group_not_keyed" for code, _, _ in constraints)
        if not constraints or (not grouped and not self._dedup_filtered()):
            # rows are kept one by one (windows add values, not rows): a merge needs the parent's unique key
            if pk is None or pk not in exposed:
                return self.B("no_unique_key", f"rows of {self.name} are kept one by one, and no unique key of {self.name} "
                              "is in the output to merge on")
            if constraints and pk not in keys:
                return self.B("window_not_keyed", f"the windows over {self.name} are not partitioned by its key {parent_key}")
            return pk, (exposed[pk],)
        key = pk if pk in keys else sorted(keys)[0]
        if grouped:
            merge = self.merge_key()
            if not merge:
                return self.B("no_unique_key", "a GROUP BY key is not in the output, so the rows have no key to merge on")
            return key, merge
        return key, (exposed[key],)

    # ---- outer-join pattern (Kingyo outer_join.analyze)
    def outer_join(self, actions: dict[str, Action]):
        """``(detail, parent join column, merge key, preserved table)`` or a Blocker."""

        p = self.name

        def no(reason: str) -> Blocker:
            return self.B("nullable_side", f"{p} is on the nullable side of an outer join, but {reason}")

        if len(self.scopes) != 1 or not isinstance(self.root.expression, exp.Select):
            return no("the query has CTEs or subqueries")
        t = self.root.expression
        for k in ("group", "having", "qualify", "distinct"):
            if t.args.get(k):
                return no(f"it has {k.upper()}, which is not row by row")
        if t.find(exp.Window) or t.find(exp.AggFunc):
            return no("it has an aggregate or window function")
        ptab = next(x for x in t.find_all(exp.Table) if x.name.lower() == self.parent)
        joins = t.args.get("joins") or []
        srcs = [(_from(t).this, "")] + [(j.this, (j.side or "").upper()) for j in joins]
        pidx = next((i for i, (s, _) in enumerate(srcs) if s is ptab), None)
        if pidx is None:
            return no("it is read inside a subquery")
        palias = ptab.alias_or_name
        if any(side == "FULL" for _, side in srcs):
            return no("the join is a FULL join")
        if pidx > 0 and srcs[pidx][1] == "LEFT":
            join = joins[pidx - 1]
            if any(side == "RIGHT" for _, side in srcs):
                return no("LEFT and RIGHT joins are mixed")
            candidates = [s for s, _ in srcs[:pidx]]
        elif pidx == 0 and len(srcs) > 1 and srcs[1][1] == "RIGHT":
            join = joins[0]
            if any(side == "RIGHT" for _, side in srcs[2:]):
                return no("there are several RIGHT joins")
            candidates = [srcs[1][0]]
        else:
            return no("it is not the nullable side of a single LEFT or RIGHT join")
        tables = {s.alias_or_name: s for s in candidates if isinstance(s, exp.Table)}
        pairs = []
        for u in join.args.get("using") or []:
            if len(tables) == 1:
                pairs.append((next(iter(tables)), u.name, u.name))
        for c in _conjuncts(join.args.get("on")):
            if isinstance(c, exp.EQ) and isinstance(c.left, exp.Column) and isinstance(c.right, exp.Column):
                left, right = c.left, c.right
                if right.table == palias and left.table in tables:
                    pairs.append((left.table, left.name, right.name))
                elif left.table == palias and right.table in tables:
                    pairs.append((right.table, right.name, left.name))
        if not pairs:
            return no("no equality joins it to a base table")
        pairs = [x for x in pairs if x[0] == pairs[0][0]]
        if len(pairs) != 1:
            return no("it is joined on more than one column")
        pa, a_col, b_col = pairs[0]
        preserved = tables[pa]
        pi = next(i for i, (s, _) in enumerate(srcs) if s is preserved)
        if pidx != 0 and ((pi > 0 and srcs[pi][1] not in ("", "INNER", "CROSS")) or any(side == "RIGHT" for _, side in srcs[pi + 1:])):
            return no(f"the preserved table {preserved.name} is itself null-extended")
        outputs = {}
        star = False
        for e in t.selects:
            u = e.unalias()
            if isinstance(u, exp.Column) and isinstance(u.this, exp.Star) and u.table == pa:
                star = True
            elif isinstance(u, exp.Column) and u.table == pa:
                outputs.setdefault(u.name.lower(), e.alias_or_name)
        detail = (f"recompute the rows whose {preserved.name}.{a_col} matches a changed {p}.{b_col}")
        info = actions.get(preserved.name.lower(), Action(preserved.name, "source"))
        fact_key = info.unique_key[0] if len(info.unique_key) == 1 else "id"
        merge_out = outputs.get(fact_key.lower(), fact_key if star else None)
        if merge_out is None:
            return self.B("no_unique_key", f"no unique key of the preserved table {preserved.name} ({fact_key}) is in the "
                          "output to merge the recomputed rows on")
        return detail, b_col, (merge_out,), preserved.name


# --------------------------------------------------------------------------- one table


def _edge(action: Action, parent: Action, actions: dict[str, Action], schema: dict, overrides: dict | None) -> EdgeFit:
    try:
        q = _Query(action.sql, parent.name, schema)
    except Exception as exc:  # the analysis must never crash a report: unknown is a blocker, not a pattern
        return EdgeFit(parent.name, blockers=(Blocker("not_analysed", f"the query could not be analysed: {type(exc).__name__}"),))
    columns = _known_columns(parent, overrides)
    read = q.read_columns()
    watermark, wbasis = _watermark(parent, columns, read)
    key, kbasis = _parent_key(parent, columns, read)
    base = EdgeFit(parent.name, watermark=watermark, watermark_basis=wbasis)
    blockers: list[Blocker] = []
    fit = base
    try:
        shape = q.shape()
        if shape:
            blockers.append(shape)
        else:
            fit, blockers = _patterns(q, action, parent, actions, key, kbasis, base)
    except Exception as exc:  # unknown => no pattern
        blockers = [Blocker("not_analysed", f"the query could not be analysed: {type(exc).__name__}")]
        fit = base
    if watermark is None:
        blockers.append(_blocker("no_watermark", f"source has no watermark column: {parent.name} has no partitionBy and no "
                                 "TIMESTAMP column, so an incremental run cannot tell new rows from old", parent.name))
    return replace(fit, blockers=tuple(blockers))


def _patterns(q: _Query, action: Action, parent: Action, actions, key, kbasis, base: EdgeFit):
    partition, suggested = action.partition_by, False
    if partition is None:
        partition = q.aligned_partition(parent.partition_by, base.watermark)
        suggested = partition is not None
    tried: list[Blocker] = []
    if partition:
        result = q.partition(partition, parent.partition_by, base.watermark)
        if isinstance(result, Blocker):
            tried.append(result)
        else:
            kind, over = result
            merge = q.merge_key()
            exposed = q.exposed()
            notes = ()
            if merge is None:
                return base, [q.B("no_unique_key", "a GROUP BY expression is not in the output, so the rewritten "
                                  "partitions have no key to merge on")]
            keyed_by = None
            if not merge and key and key.lower() in exposed:
                merge, keyed_by = (exposed[key.lower()],), key
            windowed = any(w for sc in q.reading for w in sc.expression.find_all(exp.Window))
            if not merge and (windowed or any(sc.expression.args.get("distinct") for sc in q.reading)):
                return base, [q.B("no_unique_key", f"window or DISTINCT values change within a partition, and no unique key "
                                  f"of {parent.name} is in the output to merge the rewritten rows on")]
            if not merge and any(q._global_aggregate(sc.expression) is not None for sc in q.reading):
                return base, [q.B("no_unique_key", "aggregated rows need a key to merge the rewritten partitions on")]
            if not merge:
                notes = (f"appends: assumes {parent.name} only gains rows; declare uniqueKey on it if rows are updated",)
            over_sql = _show(over)
            if kind == "aligned":
                detail = f"partition by {partition}, which is {over_sql} of {parent.name}: a run rewrites only the newest partitions"
                pattern = "partition_aligned"
            else:
                detail = (f"partition by {partition}, which is {over_sql} of {parent.name}, not its partition or watermark: a "
                          "run rewrites the partitions the changed rows fall in, old and new")
                pattern = "partition_other_column"
            return replace(base, pattern=pattern, detail=detail, partition=partition, partition_suggested=suggested,
                           unique_key=merge, key=keyed_by, key_basis=kbasis if keyed_by else "", notes=notes), []
    result = q.keyed(key)
    if not isinstance(result, Blocker):
        column, merge = result
        detail = f"recompute only the {parent.name}.{column} values that changed, merge on {', '.join(merge)}"
        return replace(base, pattern="keyed_merge", detail=detail, partition=action.partition_by, unique_key=merge,
                       key=column, key_basis=kbasis if key and column == key.lower() else "group or window key"), []
    if result.code == "nullable_side":
        oj = q.outer_join(actions)
        if not isinstance(oj, Blocker):
            detail, column, merge, preserved = oj
            return replace(base, pattern="outer_join_dimension", detail=detail, partition=action.partition_by,
                           unique_key=merge, key=column, key_basis="join column", preserved=preserved), []
        result = oj
    blockers = [b for b in tried if b.code != result.code] + [result]
    return base, blockers


def _query_blockers(tree: exp.Expression) -> tuple[list[Blocker], list[str]]:
    blockers, notes = [], []
    random = next(iter(tree.find_all(*_RANDOM)), None) if _RANDOM else None
    if random is not None:
        blockers.append(_blocker("nondeterministic", f"the query is not deterministic: {_show(random)}"))
    for node in tree.find_all(*_CLOCK):
        alias = node.parent if isinstance(node.parent, exp.Alias) else None
        if alias is not None and isinstance(alias.parent, exp.Select) and alias in alias.parent.selects:
            notes.append(f"{alias.alias} is the run's clock: an audit column; name it in ignore_columns when you check the conversion")
        else:
            blockers.append(_blocker("nondeterministic", f"the result depends on when it runs: {_show(node)} "
                                     "outside an output column"))
            break
    return blockers, list(dict.fromkeys(notes))


def _parents(tree: exp.Expression, target: str) -> list[str]:
    out: dict[str, None] = {}
    for table in tree.find_all(exp.Table):
        if not table.name or binding_cte(table) is not None or table.name.lower() == target.lower():
            continue
        out.setdefault(table.name, None)
    return list(out)


def advise_table(action: Action, actions: dict[str, Action], *, source_schema: dict | None = None,
                 table_bytes: dict[str, float] | None = None, assumptions: CostAssumptions | None = None) -> FitAdvice:
    """The advice for one ``type: "table"`` action (see the module docstring)."""

    advice = FitAdvice(action.name, action.path, "no_suggestion")
    if action.error:
        advice.notes.append(f"not analysed: {action.error}")
        return advice
    try:
        tree = sqlglot.parse_one(action.sql, read="bigquery")
    except sqlglot.errors.SqlglotError as exc:
        advice.notes.append(f"not analysed: the SQL does not parse ({str(exc).splitlines()[0][:120]})")
        return advice
    if not isinstance(tree, exp.Query):
        advice.notes.append("not analysed: the body is not a query")
        return advice
    names = _parents(tree, action.name)
    if not names:
        advice.notes.append("reads no table, so there is nothing to follow incrementally")
        return advice
    advice.blockers, advice.notes = _query_blockers(tree)
    parents = [actions.get(n.lower(), Action(n, "source")) for n in names]
    schema = {}
    for parent in parents:
        columns = _known_columns(parent, source_schema)
        if columns:
            schema[parent.name] = {c: (t or "STRING") for c, t in columns.items()}
    advice.edges = [_edge(action, parent, actions, schema, source_schema) for parent in parents]
    fitting = [e for e in advice.edges if e.fits]
    if advice.blockers or not fitting:
        advice.status = "blocked"
    elif len(fitting) == len(advice.edges):
        advice.status = "candidate"
    else:
        advice.status = "partial"
        others = ", ".join(e.parent for e in advice.edges if not e.fits)
        advice.notes.append(f"whenever {others} changes the table still needs a full refresh (proof rule R6 covers an append "
                            "over joins only while the joined tables never change)")
    if advice.status == "candidate":
        advice.check = _check(action, advice.edges, source_schema)
        if table_bytes is not None:
            advice.cost = run_cost(action, advice.edges, parents, table_bytes, assumptions or CostAssumptions())
    return advice


# --------------------------------------------------------------------------- the sketch and its proof rule


def _row_wise(select: exp.Select) -> bool:
    return not any(select.args.get(k) for k in ("joins", "group", "having", "qualify", "distinct", "limit", "offset", "with_", "with")) \
        and not select.find(exp.AggFunc, exp.Window, exp.Subquery) and isinstance(_from(select).this, exp.Table)


def _check(action: Action, edges: list[EdgeFit], source_schema: dict | None) -> dict:
    """Sketch the Dataform conversion and ask the proof rules about it (on the sources incremental-report infers)."""

    search_only = ("once converted, incremental-report can only search for counterexamples, which is bounded evidence, "
                   "not a proof")
    none = {"checkable": False, "sketch": None, "rules": {},
            "message": f"no proof rule covers this shape: {search_only}"}
    if len(edges) != 1:
        return {**none, "message": f"the table reads several tables, so there is no sketch to check: {search_only}"}
    edge = edges[0]
    try:
        sketch = _sketch(action, edge)
    except Exception:  # a sketch is a convenience; never let it fail the report
        sketch = None
    if sketch is None:
        return none
    full_sql, incremental_sql, condition, unique, steps = sketch
    model = IncrementalModel(action.name, full_sql, incremental_sql, unique_key=unique)
    condition = condition.format(self=_SELF)
    rules = {}
    try:
        sources = infer_sources(model, source_schema)
        for contract, kinds in CONTRACTS.items():
            verdict = prove(model, sources, kinds)
            if verdict is not None:
                rules[contract] = verdict.rule
    except Exception:
        rules = {}
    config = 'type: "incremental"' + (f", uniqueKey: {json.dumps(list(unique))}" if unique else "")
    if edge.partition:
        config += f', bigquery: {{ partitionBy: "{edge.partition}" }}'
    shown = {"config": config, "incremental_condition": condition, "steps": steps}
    if rules:
        named = "; ".join(f"{rule} under {contract}" for contract, rule in rules.items())
        message = (f"checkable with incremental-report once converted: on the sources incremental-report would infer, "
                   f"{named} applies to this sketch. Run incremental-report on your converted model; that is the check")
        return {"checkable": True, "sketch": shown, "rules": rules, "message": message}
    return {**none, "sketch": shown,
            "message": "no proof rule applies to this sketch on the sources incremental-report would infer (it takes a "
                       "column named id as a source's key and the first *_at or ts column it reads as its event time; "
                       f"--source-schema sets the columns): {search_only}"}


def _sketch(action: Action, edge: EdgeFit):
    """``(full SQL, incremental SQL, condition, uniqueKey, steps)`` for the shapes with a plain Dataform rendering.

    ``condition`` names the table itself as ``{self}``, for ``str.format``.
    """

    full = sqlglot.parse_one(action.sql, read="bigquery")
    if not isinstance(full, exp.Select) or full.args.get("with_") or full.args.get("with") or edge.watermark is None:
        return None
    wm, target, steps = edge.watermark, action.name, []
    source = _from(full).this if _from(full) is not None else None
    if not isinstance(source, exp.Table):
        return None
    if _row_wise(full) and edge.pattern in ("partition_aligned", "partition_other_column", "keyed_merge"):
        out = _projected_as(full, wm)
        if out is None:
            return None
        condition = f"{wm} > COALESCE((SELECT MAX({out}) FROM {{self}}), {_OLD})"
        incremental = full.copy().where(condition.format(self=target), dialect="bigquery")
        return full.sql(dialect="bigquery"), incremental.sql(dialect="bigquery"), condition, edge.unique_key, steps
    group = full.args.get("group")
    if (group and len(group.expressions) == 1 and not any(full.args.get(k) for k in ("joins", "having", "qualify", "distinct", "limit", "offset"))
            and not full.find(exp.Window, exp.Subquery)
            and all(isinstance(a, _ORDER_FREE) for a in full.find_all(exp.AggFunc))):
        g = group.expressions[0]
        if isinstance(g, exp.Literal) and g.is_int:
            g = full.selects[int(g.name) - 1].unalias()
        newest = next((e.alias for e in full.selects if isinstance(e, exp.Alias) and isinstance(e.this, exp.Max)
                       and isinstance(e.this.this, exp.Column) and e.this.this.name.lower() == wm.lower()), None)
        if isinstance(g, exp.Column) and edge.pattern == "keyed_merge":
            if newest is None:
                newest = f"{wm}_max"
                full = full.copy()
                full.select(f"MAX({wm}) AS {newest}", copy=False)
                steps.append(f"add MAX({wm}) AS {newest} to the output: the run finds where it left off from it")
            name = g.name
            table = source.copy()
            table.set("alias", None)
            condition = (f"{name} IN (SELECT {name} FROM {table.sql(dialect='bigquery')} WHERE {wm} >= "
                         f"COALESCE((SELECT MAX({newest}) FROM {{self}}), {_OLD}))")
            incremental = full.copy().where(condition.format(self=target), dialect="bigquery")
            return full.sql(dialect="bigquery"), incremental.sql(dialect="bigquery"), condition, (name,), steps
        out = next((e.alias_or_name for e in full.selects if e.unalias().sql() == g.sql()), None)
        if out is not None and _truncation_of(g, wm) and edge.pattern == "partition_aligned":
            condition = f"{g.sql(dialect='bigquery')} >= COALESCE((SELECT MAX({out}) FROM {{self}}), DATE '1970-01-01')"
            incremental = full.copy().where(condition.format(self=target), dialect="bigquery")
            steps.append(f"each run rebuilds the newest {out} and every later one, merged on {out}")
            return full.sql(dialect="bigquery"), incremental.sql(dialect="bigquery"), condition, (out,), steps
    return None


# --------------------------------------------------------------------------- cost


_SHARE = {
    "partition_aligned": lambda a: a.changed_fraction,
    "partition_other_column": lambda a: a.changed_fraction * a.late_spread + a.changed_fraction,
    "keyed_merge": lambda a: a.changed_fraction * a.key_span + a.changed_fraction,
    "outer_join_dimension": lambda a: a.changed_fraction * a.key_span + a.changed_fraction,
}


def run_cost(action: Action, edges: list[EdgeFit], parents: list[Action], table_bytes: dict[str, float],
             assumptions: CostAssumptions) -> dict:
    """Bytes scanned per run, full rebuild against the suggested incremental run, with every basis kept apart.

    ``estimate``: Kingyo's KPI model. A parent with a pattern is read over the share of it the pattern
    touches (``changed_fraction``, widened by ``late_spread`` or ``key_span``), but in full when it declares
    no ``partitionBy`` (BigQuery prunes a filtered read only by partition); a merge also reads the table
    itself; every table referenced bills at least 10 MiB. Table bytes stand in for the bytes of the columns
    read, on both sides. ``upper_bound``: a rebuild bills at most its tables' bytes and an incremental run
    at least 10 MiB per table it references, so the saving cannot exceed the difference. ``measured``:
    always None here; it needs before and after windows of real runs (:class:`kumosql.savings.Ledger`).
    """

    sizes = {name.lower(): float(value) for name, value in table_bytes.items()}
    unit = DEFAULT_UNIT
    missing = [p.name for p in parents if p.name.lower() not in sizes]
    if missing:
        return {"unit": unit, "per": "run", "full_rebuild": None, "incremental_run": None,
                "saving": {"estimate": None, "upper_bound": None, "measured": None},
                "notes": [f"no figures: the bytes of {', '.join(missing)} are unknown (give them in --table-bytes)"]}
    caveats = [f"assumes {k}={v:g}" for k, v in assumptions.to_json().items()]
    caveats.append("table bytes stand in for the bytes of the columns read, on both sides")
    full = sum(max(sizes[p.name.lower()], MIN_BYTES_PER_TABLE) for p in parents)
    shares: dict[str, float] = {}
    partitioned = {p.name.lower(): bool(p.partition_by) for p in parents}
    for edge, parent in zip(edges, parents):
        reads = [(parent.name, min(1.0, _SHARE[edge.pattern](assumptions)))]
        if edge.preserved:  # the preserved side's rows with a changed join key are read again
            reads.append((edge.preserved, min(1.0, assumptions.changed_fraction * assumptions.key_span)))
        for name, share in reads:
            if not partitioned.get(name.lower(), False):
                share = 1.0
                caveats.append(f"{name} declares no partitionBy, so a filtered read still scans all of it")
            shares[name.lower()] = max(shares.get(name.lower(), 0.0), share)
    incremental = sum(max(sizes[name] * share, MIN_BYTES_PER_TABLE) for name, share in shares.items())
    own = sizes.get(action.name.lower())
    if any(edge.unique_key for edge in edges):
        aligned = all(edge.pattern == "partition_aligned" for edge in edges) and any(edge.partition for edge in edges)
        if own is None:
            caveats.append("the table's own bytes are unknown, so the merge's read of it counts only the 10 MiB minimum")
            incremental += MIN_BYTES_PER_TABLE
        else:
            incremental += max(own * (assumptions.changed_fraction if aligned else 1.0), MIN_BYTES_PER_TABLE)
    else:
        incremental += MIN_BYTES_PER_TABLE  # the watermark's MAX() over the table
    estimate = SavingClaim("estimate", unit, "incremental-fit model of one run (not a dry run, not measured)",
                           full - incremental, caveats=tuple(dict.fromkeys(caveats)))
    upper = SavingClaim("upper_bound", unit, "a rebuild bills at most its tables' bytes; an incremental run at least "
                        "10 MiB per table it references", full - MIN_BYTES_PER_TABLE * (len(parents) + 1))
    notes = ["estimate and upper_bound are different bases: never add them, and neither is a measurement"]
    if estimate.value < 0:
        notes.append("the estimate is a regression: on these sizes an incremental run reads more than a rebuild")
    return {
        "unit": unit,
        "per": "run",
        "full_rebuild": {"value": full, "basis": "estimate"},
        "incremental_run": {"value": incremental, "basis": "estimate"},
        "saving": {"estimate": estimate.to_json(), "upper_bound": upper.to_json(), "measured": None},
        "assumptions": assumptions.to_json(),
        "notes": notes,
    }


# --------------------------------------------------------------------------- project report


def advise_project(root: str | Path, *, source_schema: dict | None = None, table_bytes: dict[str, float] | None = None,
                   assumptions: CostAssumptions | None = None, limit: int | None = None) -> list[FitAdvice]:
    """Advice for every ``type: "table"`` action of a Dataform project, in file order."""

    actions = read_project(root)
    tables = [a for a in actions.values() if a.kind == "table"]
    return [advise_table(a, actions, source_schema=source_schema, table_bytes=table_bytes, assumptions=assumptions)
            for a in tables[:limit]]


def summarise(advice: list[FitAdvice]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for item in advice:
        counts[item.status] += 1
    return counts


NOTICE = ("Suggestions only: nothing here says an incremental version equals the table; check a converted model with "
          "incremental-report.")


def report_json(advice: list[FitAdvice]) -> dict:
    return {"notice": NOTICE, "summary": summarise(advice), "models": [a.to_json() for a in advice]}


def report_text(advice: list[FitAdvice]) -> str:
    counts = summarise(advice)
    lines = [f"{len(advice)} full-refresh tables: {counts['candidate']} could become incremental, {counts['partial']} "
             f"partly, {counts['blocked']} blocked, {counts['no_suggestion']} nothing to suggest. {NOTICE}"]
    for item in advice:
        if item.status in ("candidate", "partial"):
            fits = "; ".join(f"{e.parent} -> {e.pattern} ({e.detail})" for e in item.edges if e.fits)
            lines.append(f"  {item.status} {item.model}: {fits}")
            if item.check:
                lines.append(f"    {item.check['message']}")
        elif item.status == "blocked":
            found = [*item.blockers, *(b for e in item.edges for b in e.blockers)]
            text = "no pattern applies"
            if found:
                text = found[0].message + (f"; fix: {found[0].fix}" if found[0].fix else "")
            more = f" (+{len(found) - 1} more in --json)" if len(found) > 1 else ""
            lines.append(f"  blocked {item.model}: {text}{more}")
        else:
            lines.append(f"  no suggestion {item.model}: {'; '.join(item.notes) or 'nothing to analyse'}")
    return "\n".join(lines)
