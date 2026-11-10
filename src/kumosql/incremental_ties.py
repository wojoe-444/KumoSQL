"""Incremental models whose full refresh depends on how the engine breaks ties.

A query like ``QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY ts DESC) = 1`` keeps one of
the rows that share the newest ``ts``, and which one is up to the engine. Such a model has no single full
refresh to agree with, so neither ``safe`` nor ``diverges`` describes it. :func:`tie_reasons` names the
constructs that can depend on tie-breaking, and :func:`tie_witness` looks for a reachable source state on
which two evaluations of the full query, reading the same rows in opposite orders, give different
results. Only a witness makes the verdict ``nondeterministic``; without one the model goes on to the
usual divergence search.

The constructs are ``ROW_NUMBER``, ``FIRST_VALUE``/``LAST_VALUE``/``NTH_VALUE``, ``LAG``/``LEAD``,
``NTILE`` and ``ROWS`` frames (each deterministic when its partition and ordering columns determine the
row), ``ANY_VALUE`` and an unordered ``ARRAY_AGG``/``STRING_AGG`` (deterministic when the group, plus the
aggregate's own ordering, determines the row), ``LIMIT`` (when the ``ORDER BY`` determines the row) and
``RAND``/``GENERATE_UUID``. "Determines the row" is read with
:func:`kumosql.output_properties.infer_properties` under the contract's key facts; rows that are exact
copies never tie, because picking either gives the same output.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

import sqlglot
from sqlglot import exp

from .incremental import (
    Counterexample,
    IncrementalError,
    IncrementalModel,
    Simulation,
    SourceTable,
    effective_model,
    random_sequence,
)
from .incremental_monotone import _has_aggregate, _own_nodes, contract_constraints, source_schema

_ORDER_SENSITIVE_WINDOWS = tuple(
    getattr(exp, name)
    for name in ("RowNumber", "FirstValue", "LastValue", "NthValue", "Lag", "Lead", "Ntile", "ArrayAgg", "GroupConcat")
    if hasattr(exp, name)
)
_ORDER_SENSITIVE_AGGREGATES = tuple(getattr(exp, name) for name in ("ArrayAgg", "GroupConcat", "AnyValue") if hasattr(exp, name))
_RANDOM_NAMES = {"RAND", "GENERATE_UUID"}
# sqlglot parses RAND() and GENERATE_UUID() as their own node types, not as anonymous functions
_RANDOM_NODES = tuple(getattr(exp, name) for name in ("Rand", "Uuid") if hasattr(exp, name))


def tie_reasons(
    query: str | exp.Expression,
    constraints: dict | None = None,
    schema: dict[str, list[str]] | None = None,
    *,
    dialect: str = "bigquery",
) -> list[str]:
    """Why ``query``'s result can depend on tie-breaking or chance; empty when it cannot.

    ``constraints`` (``TableConstraints`` per table) give keys that determine a row. Anything that
    cannot be shown deterministic is reported, so a reason is a suspicion, not a proof.
    """

    try:
        tree = sqlglot.parse_one(query, read=dialect) if isinstance(query, str) else query
    except sqlglot.errors.SqlglotError as error:
        return [f"cannot parse: {error}"]
    with_ = tree.args.get("with_") or tree.args.get("with")
    reasons: list[str] = []
    for select in tree.find_all(exp.Select):
        reasons += _select_ties(select, with_, constraints or {}, schema or {}, dialect)
    for node in tree.find_all(exp.SetOperation):
        if node.args.get("limit") is not None or node.args.get("offset") is not None:
            reasons.append("LIMIT on a set operation")
    for node in tree.walk():
        if isinstance(node, _RANDOM_NODES) or (isinstance(node, exp.Anonymous) and str(node.this).upper() in _RANDOM_NAMES):
            reasons.append(f"{node.sql(dialect=dialect)} is random")
    return list(dict.fromkeys(reasons))


def _group_term(select: exp.Select, node: exp.Expression) -> exp.Expression:
    """``node`` with a positional (``GROUP BY 1``) or output-alias reference replaced by the projection."""

    outputs = select.expressions
    if isinstance(node, exp.Literal) and node.is_int and 1 <= int(node.this) <= len(outputs):
        return outputs[int(node.this) - 1].unalias()
    if isinstance(node, exp.Column) and not node.table:
        names = {c.name.lower() for c in select.find_all(exp.Column) if c.find_ancestor(exp.Alias) is None}
        for e in outputs:
            if isinstance(e, exp.Alias) and e.alias.lower() == node.name.lower() and node.name.lower() not in names:
                return e.unalias()
    return node


def _determines_row(select: exp.Select, parts: list[exp.Expression], with_, constraints, schema, dialect) -> bool:
    """``parts`` (expressions over the select's FROM, after WHERE and GROUP BY) determine the input row."""

    from kumo_sql_prover.output_properties import infer_properties

    group = select.args.get("group")
    if group is not None and group.expressions:
        # in a grouped select a window or LIMIT reads the groups, and the group keys are unique
        if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
            return False
        wanted = {_group_term(select, p).sql() for p in parts}
        return all(_group_term(select, g).sql() in wanted for g in group.expressions)
    probe = exp.Select(expressions=[p.copy().as_(f"__k{i}") for i, p in enumerate(parts)] or [exp.Literal.number(1).as_("__k0")])
    for key in ("from_", "from", "joins", "where"):
        value = select.args.get(key)
        if value is not None:
            probe.set(key, [j.copy() for j in value] if isinstance(value, list) else value.copy())
    if with_ is not None:
        probe.set("with_", with_.copy())
    props = infer_properties(probe.sql(dialect=dialect), constraints, schema, dialect)
    if props.unsupported:
        return False
    names = {f"__k{i}" for i in range(len(parts))}
    return any(set(k.columns) <= names for k in props.keys)


def _own_nodes_of_select(select: exp.Select) -> Iterable[exp.Expression]:
    """Nodes of ``select``'s own clauses, not entering FROM items or nested queries (each is checked on its own)."""

    for key, value in select.args.items():
        if key in ("from_", "from", "joins", "with_", "with"):
            continue
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, exp.Expression) and not isinstance(child, (exp.Subquery, exp.Select, exp.SetOperation)):
                yield from _own_nodes(child)


def _select_ties(select: exp.Select, with_, constraints, schema, dialect) -> list[str]:
    reasons = []
    group = select.args.get("group")
    nodes = list(_own_nodes_of_select(select))
    for window in (n for n in nodes if isinstance(n, exp.Window)):
        spec = window.args.get("spec")
        rows_frame = spec is not None and str(spec.args.get("kind") or "").upper() == "ROWS"
        if not (isinstance(window.this, _ORDER_SENSITIVE_WINDOWS) or rows_frame):
            continue
        order = window.args.get("order")
        parts = list(window.args.get("partition_by") or []) + [o.this for o in (order.expressions if order else [])]
        if not _determines_row(select, parts, with_, constraints, schema, dialect):
            reasons.append(f"{window.sql(dialect=dialect)} can tie")
    for aggregate in (n for n in nodes if isinstance(n, _ORDER_SENSITIVE_AGGREGATES) and n.find_ancestor(exp.Window) is None):
        keys = [_group_term(select, g) for g in group.expressions] if group is not None else []
        if isinstance(aggregate, getattr(exp, "AnyValue", ())) and any(aggregate.this.sql() == k.sql() for k in keys):
            continue
        ordered = aggregate.find(exp.Order)
        parts = keys + ([o.this for o in ordered.expressions] if ordered is not None else [])
        ungrouped = select.copy()
        ungrouped.set("group", None)
        if not _determines_row(ungrouped, parts, with_, constraints, schema, dialect):
            reasons.append(f"{aggregate.sql(dialect=dialect)} depends on row order")
    limit = select.args.get("limit")
    if limit is not None:
        order = select.args.get("order")
        if order is None:
            reasons.append("LIMIT without ORDER BY")
        else:
            outputs = {e.alias_or_name.lower(): e.unalias() for e in select.expressions}
            parts = [
                outputs.get(o.this.name.lower(), o.this) if isinstance(o.this, exp.Column) and not o.this.table else o.this
                for o in order.expressions
            ]
            if select.args.get("distinct") is not None or group is not None or any(_has_aggregate(e) for e in select.expressions):
                wanted = {p.sql() for p in parts}
                ok = all(e.unalias().sql() in wanted for e in select.expressions)
            else:
                inner = select.copy()
                for key in ("order", "limit", "offset"):
                    inner.set(key, None)
                ok = _determines_row(inner, parts, with_, constraints, schema, dialect)
            if not ok:
                reasons.append(f"ORDER BY {', '.join(o.sql(dialect=dialect) for o in order.expressions)} LIMIT can tie")
    return reasons


def model_tie_reasons(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: Iterable[str], tables: tuple[str, ...] | None = None
) -> list[str]:
    """:func:`tie_reasons` for the model's full query, under the contract's key facts."""

    constraints = contract_constraints(sources, kinds, tables, exact_copies=True)
    return tie_reasons(effective_model(model).full_sql, constraints, source_schema(sources), dialect=model.dialect)


# ---------------------------------------------------------------------------
# Witnesses
# ---------------------------------------------------------------------------


def differs_by_row_order(model: IncrementalModel, sources: dict[str, SourceTable], statements: list[str]) -> str | None:
    """Evaluate the full query on the state ``statements`` build, then on the same rows read in reverse."""

    try:
        sim = Simulation(model, sources, statements)
        _, forward = sim.full_rows()
        names = list(sources)
        for name in names:
            sim.con.execute(f'ALTER TABLE "{name}" RENAME TO "__fwd_{name}"')
            sim.con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "__fwd_{name}" ORDER BY rowid DESC')
        columns, backward = sim.full_rows()
    except IncrementalError:
        return None
    except Exception:
        return None
    ignore = {c.lower() for c in model.ignore_columns}
    keep = [i for i, c in enumerate(columns) if c.lower() not in ignore]
    a = Counter(tuple(r[i] for i in keep) for r in forward)
    b = Counter(tuple(r[i] for i in keep) for r in backward)
    if a == b:
        return None
    only_a = sorted(map(repr, (a - b).elements()))[:2]
    only_b = sorted(map(repr, (b - a).elements()))[:2]
    return f"one evaluation returns {', '.join(only_a) or 'fewer rows'}, the other {', '.join(only_b) or 'fewer rows'}"


def tie_witness(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    *,
    reasons: list[str] | None = None,
    seeds: int = 60,
    batches: int = 4,
    seed: int = 0,
    tables: tuple[str, ...] | None = None,
    sequences: Iterable[tuple[Iterable[str], Iterable[Iterable[str]]]] = (),
) -> Counterexample | None:
    """A reachable source state on which the full query gives two different results, minimized.

    Every state of the search's random sequences (and of ``sequences``) is tried: the full query is
    evaluated once on the rows as loaded and once on the same rows stored in reverse order. Both are
    evaluations a database may legally make, so a difference shows the full refresh is not a function
    of the sources. The witness is the source DML of that state, as ``initial`` with no batches.
    """

    kinds = frozenset(kinds)
    reasons = reasons if reasons is not None else model_tie_reasons(model, sources, kinds, tables)
    if not reasons:
        return None
    plans = list(sequences) + [random_sequence(sources, kinds, seed * 100003 + s, batches, tables) for s in range(seeds)]
    for initial, plan in plans:
        state = list(initial)
        for batch in [[], *plan]:
            state += list(batch)
            if differs_by_row_order(model, sources, state) is None:
                continue
            state = _shrink(model, sources, state)
            detail = differs_by_row_order(model, sources, state) or "the full query's result depends on row order"
            return Counterexample(tuple(state), (), 0, "nondeterministic", f"{'; '.join(reasons)}: {detail}")
    return None


def _shrink(model: IncrementalModel, sources: dict[str, SourceTable], state: list[str]) -> list[str]:
    changed = True
    while changed:
        changed = False
        for i in range(len(state) - 1, -1, -1):
            trial = state[:i] + state[i + 1 :]
            if differs_by_row_order(model, sources, trial) is not None:
                state, changed = trial, True
    return state
