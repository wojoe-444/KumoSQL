"""More proof rules for incremental models (see :mod:`kumosql.incremental`).

:func:`kumosql.incremental.prove_watermark` covers row-wise single-table
models (R1 append, R2 merge). The rules here cover four shapes it declines:

* **R3, key de-duplication.** The R1/R2 query plus
  ``QUALIFY ROW_NUMBER() OVER (PARTITION BY <source key> ...) = 1``. Exact
  re-deliveries (``duplicate``) are then absorbed.
* **R4, truncated watermark.** An R2 merge whose watermark compares a
  monotone truncation of the event time (``DATE(ts)``, ``TIMESTAMP_TRUNC``,
  ``CAST(ts AS DATE)``) with ``>=`` against the same truncation projected by
  the model, so a run reloads the whole newest day.
* **R5, group re-aggregation.** ``SELECT g, aggregates, MAX(ts) AS m ... GROUP
  BY g`` merged on ``g``, whose incremental run keeps only the groups that
  received a row at or after the watermark on ``m``:
  ``WHERE g IN (SELECT g FROM source WHERE ts >= COALESCE((SELECT MAX(m) FROM
  self), <old date>))``. Every touched group is recomputed in full.
* **R6, frozen dimensions.** An R1 append over a fact table joined (inner or
  left, fact table first) to tables the contract never changes.

Each rule states its assumptions in its docstring; all of them share R1/R2's:
no incremental ``pre_operations``, the ``COALESCE`` default precedes every
event time, and a declared source key is unique and non-NULL.
"""

from __future__ import annotations

from dataclasses import replace

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import extended_grouping
from .incremental import (
    IncrementalModel,
    SourceTable,
    Verdict,
    _conjuncts,
    _projected_as,
    _row_wise,
    _strip,
    _watermark_predicate,
    modelled_exactly,
    prove_watermark,
)

_CLOCK = (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime)
_UNSAFE = (exp.Window, exp.Subquery, exp.Select, exp.Rand, exp.Unnest)
_GROUP_AGGREGATES = (exp.Sum, exp.Count, exp.Max, exp.Min, exp.Avg, exp.CountIf)


def _parse(model: IncrementalModel) -> tuple[exp.Expression, exp.Expression] | None:
    if model.pre_operations or not modelled_exactly(model):
        return None
    try:
        full = sqlglot.parse_one(model.full_sql, read=model.dialect)
        incremental = sqlglot.parse_one(model.incremental_sql, read=model.dialect)
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(full, exp.Select) or not isinstance(incremental, exp.Select):
        return None
    return full, incremental


def _from_table(select: exp.Select) -> exp.Table | None:
    source = select.args.get("from_") or select.args.get("from")
    return source.this if source is not None and isinstance(source.this, exp.Table) else None


def _watermark(incremental: exp.Select, target: str, tc: str, target_tc: str) -> tuple[exp.Expression, str] | None:
    where = incremental.args.get("where")
    marks = [(c, _watermark_predicate(c, target, tc, target_tc)) for c in _conjuncts(where.this if where else None)]
    marks = [(c, op) for c, op in marks if op]
    return marks[0] if len(marks) == 1 else None


def _audit_only(select: exp.Select, ignore: frozenset[str], banned: tuple) -> bool:
    """No ``banned`` node, and clock functions only as the value of an ignored output column."""

    for node in select.walk():
        if node is select:
            continue
        if isinstance(node, _CLOCK):
            if not (isinstance(node.parent, exp.Alias) and node.parent.alias.lower() in ignore and node.parent.parent is select):
                return False
        elif isinstance(node, banned):
            return False
    return True


# ---------------------------------------------------------------------------
# R3: key de-duplication
# ---------------------------------------------------------------------------


def _key_dedup(select: exp.Select, key: tuple[str, ...]) -> exp.Select | None:
    """``select`` without its ``QUALIFY ROW_NUMBER() OVER (PARTITION BY key ...) = 1``."""

    qualify = select.args.get("qualify")
    if qualify is None or not key or not isinstance(qualify.this, exp.EQ):
        return None
    window, one = qualify.this.this, qualify.this.expression
    if isinstance(one, exp.Window):
        window, one = one, window
    if not (isinstance(window, exp.Window) and isinstance(window.this, exp.RowNumber)):
        return None
    if not (isinstance(one, exp.Literal) and not one.is_string and one.name == "1"):
        return None
    partition = window.args.get("partition_by") or []
    if not all(isinstance(p, exp.Column) for p in partition):
        return None
    names = [p.name.lower() for p in partition]
    if len(names) != len(key) or set(names) != {k.lower() for k in key}:
        return None
    if any(isinstance(n, (exp.Subquery, exp.Select, exp.Rand) + _CLOCK) for n in window.walk()):
        return None
    stripped = select.copy()
    stripped.set("qualify", None)
    return stripped


def prove_key_dedup(model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str]) -> Verdict | None:
    """R3: an R1/R2 model that keeps one row per source key.

    Exact re-deliveries leave every key's rows identical copies (an update
    matches a row by all its values, so it changes every copy), and the
    row-wise query maps copies to copies. De-duplicating on the key then makes
    the full query the R1/R2 query over the distinct source rows, and a
    re-delivered copy changes nothing: it is either not re-read or merges to
    the row already there, and a run never feeds two rows for one key to the
    merge. So the model is safe under ``kinds`` when the query without the
    ``QUALIFY`` is safe under ``kinds`` minus ``duplicate``.
    """

    parsed = _parse(model)
    if parsed is None:
        return None
    full, incremental = parsed
    table = _from_table(full)
    source = sources.get(table.name) if table is not None else None
    if source is None or not source.key:
        return None
    bare_full, bare_incremental = _key_dedup(full, source.key), _key_dedup(incremental, source.key)
    if bare_full is None or bare_incremental is None:
        return None
    if full.args["qualify"].sql() != incremental.args["qualify"].sql():
        return None
    bare = replace(
        model,
        full_sql=bare_full.sql(dialect=model.dialect),
        incremental_sql=bare_incremental.sql(dialect=model.dialect),
    )
    proof = prove_watermark(bare, sources, kinds - {"duplicate"})
    if proof is None:
        return None
    return Verdict("safe", "R3 key de-duplication", f"one row per source key, then {proof.rule}; re-delivered rows are absorbed")


# ---------------------------------------------------------------------------
# R4: truncated (day) watermark
# ---------------------------------------------------------------------------


def _truncation_of(node: exp.Expression, tc: str) -> bool:
    """``node`` is a monotone non-decreasing truncation of column ``tc``."""

    if isinstance(node, exp.Date):
        if any(v for k, v in node.args.items() if k != "this"):
            return False  # a time zone or a date built from parts
        arg = node.this
    elif isinstance(node, (exp.TimestampTrunc, exp.DatetimeTrunc, exp.DateTrunc)):
        if node.args.get("zone"):
            return False
        arg = node.this
    elif isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DATE:
        arg = node.this
    else:
        return False
    return isinstance(arg, exp.Column) and arg.name.lower() == tc.lower()


def prove_truncated_watermark(model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str]) -> Verdict | None:
    """R4: R2 with the watermark on ``f(ts)`` for a monotone truncation ``f``.

    The model projects ``f(ts) AS d`` and its incremental run adds
    ``f(ts) >= COALESCE((SELECT MAX(d) FROM self), <old date>)``. Every changed
    row has ``ts`` at or after the newest event time in the table, so, ``f``
    being non-decreasing, ``f(ts)`` is at least ``MAX(d)`` and the row is
    re-read; the merge on the source's key then replaces it, as in R2. A strict
    ``>`` is refused: a new row on the newest day would be skipped. As in R2,
    ``update_touch`` needs a query without ``WHERE``.
    """

    if not model.unique_key:
        return None
    parsed = _parse(model)
    if parsed is None:
        return None
    full, incremental = parsed
    ignore = frozenset(c.lower() for c in model.ignore_columns)
    if not _row_wise(full, ignore):
        return None
    table = _from_table(full)
    source = sources.get(table.name) if table is not None else None
    if source is None or source.time_column is None or not source.key:
        return None
    tc = source.time_column
    if source.columns.get(tc, "").upper() not in ("TIMESTAMP", "DATETIME", "DATE"):
        return None
    truncated = {
        e.this.sql(): e.alias for e in full.expressions if isinstance(e, exp.Alias) and _truncation_of(e.this, tc)
    }
    where = incremental.args.get("where")
    marks = []
    for conjunct in _conjuncts(where.this if where else None):
        if not isinstance(conjunct, (exp.GT, exp.GTE)) or conjunct.left.sql() not in truncated:
            continue
        # Read the conjunct as a watermark on the truncated column itself.
        as_column = conjunct.copy()
        as_column.set("this", exp.column(tc))
        op = _watermark_predicate(as_column, model.target, tc, truncated[conjunct.left.sql()])
        if op:
            marks.append((conjunct, op))
    if len(marks) != 1 or marks[0][1] != ">=":
        return None
    conjunct = marks[0][0]
    if _strip(incremental, conjunct).sql() != _strip(full, exp.Null()).sql():
        return None
    if set(model.unique_key) != set(source.key) or any(_projected_as(full, k) != k for k in source.key):
        return None
    if not kinds <= {"insert_new", "insert_boundary", "update_touch", "empty"}:
        return None
    if "update_touch" in kinds and full.args.get("where"):
        return None  # as in R2: an update can move a row out of the filter
    return Verdict("safe", "R4 merge on a truncated watermark", "row-wise query, merge on the source's unique key, every changed row falls on or after the table's newest day")


# ---------------------------------------------------------------------------
# R5: group re-aggregation
# ---------------------------------------------------------------------------


def _group_reaggregation_filter(conjunct: exp.Expression, group: str, table: exp.Table, target: str, tc: str, target_tc: str) -> str | None:
    """``g IN (SELECT g FROM table WHERE <watermark on target_tc>)``: the watermark operator."""

    if not isinstance(conjunct, exp.In) or conjunct.args.get("expressions"):
        return None
    if not (isinstance(conjunct.this, exp.Column) and not conjunct.this.table and conjunct.this.name.lower() == group.lower()):
        return None
    query = conjunct.args.get("query")
    inner = query.this if isinstance(query, exp.Subquery) else query
    if not isinstance(inner, exp.Select) or len(inner.expressions) != 1:
        return None
    if any(inner.args.get(k) for k in ("joins", "group", "having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals", "with_", "with")):
        return None
    picked = inner.expressions[0]
    if not (isinstance(picked, exp.Column) and picked.name.lower() == group.lower()):
        return None
    source = _from_table(inner)
    if source is None or (source.name, source.db, source.catalog) != (table.name, table.db, table.catalog):
        return None
    where = inner.args.get("where")
    conjuncts = _conjuncts(where.this if where else None)
    if len(conjuncts) != 1:
        return None
    return _watermark_predicate(conjuncts[0], target, tc, target_tc)


def prove_group_reaggregation(model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str]) -> Verdict | None:
    """R5: re-aggregate every group that received a new row, merge on the group.

    The full query is ``SELECT g, <aggregates> FROM source [WHERE p] GROUP BY
    g`` with one output ``m = MAX(ts)``; the incremental query adds
    ``g IN (SELECT g FROM source WHERE ts >[=] COALESCE((SELECT MAX(m) FROM
    self), <old date>))`` and the model merges on ``g``. Under inserts only
    (``insert_new``, and ``insert_boundary`` with ``>=``), a group's result
    changes only when it receives a row, whose time is at or after every
    earlier one and so at or after ``MAX(m)``; the group is then recomputed
    over all its rows and replaces the old one. No ``HAVING`` (a group could
    leave the result), and updates, deletes and late rows are refused (they
    change a group without a new time). Assumes ``g`` is never NULL: ``IN``
    and the merge never match a NULL group, and the contract's change kinds
    only produce NULLs through ``null_key``, which is refused.
    """

    if not model.unique_key or len(model.unique_key) != 1:
        return None
    parsed = _parse(model)
    if parsed is None:
        return None
    full, incremental = parsed
    if any(full.args.get(k) for k in ("joins", "having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals", "with_", "with")):
        return None
    group = full.args.get("group")
    keys = group.expressions if group is not None else []
    if len(keys) != 1 or not isinstance(keys[0], exp.Column) or extended_grouping(group):
        return None
    g = keys[0].name
    table = _from_table(full)
    source = sources.get(table.name) if table is not None else None
    if source is None or source.time_column is None:
        return None
    tc = source.time_column
    ignore = frozenset(c.lower() for c in model.ignore_columns)
    if not _audit_only(full, ignore, _UNSAFE):
        return None
    # Order-insensitive aggregates only, so recomputing an unchanged group gives the same row.
    if any(not isinstance(a, _GROUP_AGGREGATES) for a in full.find_all(exp.AggFunc)):
        return None
    # Outside aggregates, the output reads only the group column.
    for node in full.find_all(exp.Column):
        if node.find_ancestor(exp.AggFunc, exp.Where) is None and node.name.lower() != g.lower():
            return None
    if _projected_as(full, g) != g or set(model.unique_key) != {g}:
        return None
    newest = [
        e.alias
        for e in full.expressions
        if isinstance(e, exp.Alias) and isinstance(e.this, exp.Max) and isinstance(e.this.this, exp.Column) and e.this.this.name.lower() == tc.lower()
    ]
    if len(newest) != 1:
        return None
    where = incremental.args.get("where")
    marks = [(c, _group_reaggregation_filter(c, g, table, model.target, tc, newest[0])) for c in _conjuncts(where.this if where else None)]
    marks = [(c, op) for c, op in marks if op]
    if len(marks) != 1:
        return None
    conjunct, op = marks[0]
    if _strip(incremental, conjunct).sql() != _strip(full, exp.Null()).sql():
        return None
    allowed = {"insert_new", "empty"} | ({"insert_boundary"} if op == ">=" else set())
    if not kinds <= allowed:
        return None
    return Verdict("safe", "R5 group re-aggregation", f"inserts only; every group that receives a row is recomputed in full and merged on {g} (assumes {g} is never NULL)")


# ---------------------------------------------------------------------------
# R6: append over joins to unchanged tables
# ---------------------------------------------------------------------------


def _owner(column: exp.Column, aliases: dict[str, str], sources: dict[str, SourceTable]) -> str | None:
    if column.table:
        return aliases.get(column.table.lower())
    owners = {t for t in aliases.values() if column.name.lower() in {c.lower() for c in sources[t].columns}}
    return owners.pop() if len(owners) == 1 else None


def prove_frozen_dimensions(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str], tables: tuple[str, ...] | None
) -> Verdict | None:
    """R6: an R1 append over a fact table joined to tables that never change.

    The full query reads the fact table first and joins other source tables
    with ``INNER`` or ``LEFT`` joins (no aggregates, windows, subqueries,
    ``DISTINCT`` or ``LIMIT``), and the contract changes only the fact table.
    Each output row then comes from exactly one fact row and depends only on
    it and the unchanged tables, so the query is row-wise in the fact table:
    a new row (strictly after every earlier one, so after ``MAX(ts)`` of the
    table) adds exactly its own output, and an older row is either already in
    the table (time at most ``MAX(ts)``) or produces no output at all.
    """

    if model.unique_key or not tables:
        return None
    parsed = _parse(model)
    if parsed is None:
        return None
    full, incremental = parsed
    fact = _from_table(full)
    if fact is None or fact.name not in sources or set(tables) != {fact.name}:
        return None
    if any(full.args.get(k) for k in ("group", "having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals", "with_", "with")):
        return None
    aliases = {(fact.alias or fact.name).lower(): fact.name}
    for join in full.args.get("joins") or []:
        dim = join.this
        if not isinstance(dim, exp.Table) or dim.name not in sources or dim.name == fact.name or dim.name == model.target:
            return None
        if join.args.get("using") or join.args.get("on") is None:
            return None
        if (join.kind or "").upper() not in ("", "INNER") or (join.side or "").upper() not in ("", "LEFT"):
            return None
        alias = (dim.alias or dim.name).lower()
        if alias in aliases:
            return None
        aliases[alias] = dim.name
    if len(aliases) == 1:
        return None  # single-table: R1 already decides it
    ignore = frozenset(c.lower() for c in model.ignore_columns)
    if not _audit_only(full, ignore, (exp.AggFunc,) + _UNSAFE):
        return None
    if any(isinstance(e, exp.Star) or isinstance(getattr(e, "this", None), exp.Star) for e in full.expressions):
        return None
    for column in full.find_all(exp.Column):
        if _owner(column, aliases, sources) is None:
            return None
    tc = sources[fact.name].time_column
    if tc is None:
        return None
    projected = [
        e.alias_or_name
        for e in full.expressions
        if isinstance(e.unalias(), exp.Column) and e.unalias().name.lower() == tc.lower() and _owner(e.unalias(), aliases, sources) == fact.name
    ]
    if len(projected) != 1:
        return None
    mark = _watermark(incremental, model.target, tc, projected[0])
    if mark is None or mark[1] != ">":
        return None
    conjunct = mark[0]
    if not isinstance(conjunct.left, exp.Column) or _owner(conjunct.left, aliases, sources) != fact.name:
        return None
    if _strip(incremental, conjunct).sql() != _strip(full, exp.Null()).sql():
        return None
    if not kinds <= {"insert_new", "empty"}:
        return None
    return Verdict("safe", "R6 append over unchanged joined tables", f"only {fact.name} changes; each output row comes from one {fact.name} row, strict watermark on its time")


def prove_more(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str], tables: tuple[str, ...] | None = None
) -> Verdict | None:
    """Try R3 to R7 in turn; ``None`` when none applies."""

    from .incremental_merge import prove_full_rerun_merge

    return (
        prove_key_dedup(model, sources, kinds)
        or prove_truncated_watermark(model, sources, kinds)
        or prove_group_reaggregation(model, sources, kinds)
        or prove_frozen_dimensions(model, sources, kinds, tables)
        or prove_full_rerun_merge(model, sources, kinds, tables)
    )
