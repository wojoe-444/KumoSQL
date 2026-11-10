"""R7: an incremental model that merges a full re-run of its query.

Many incremental models run the same query on every run and rely on ``uniqueKey`` to merge the result
into the table (no ``${when(incremental(), ...)}`` filter, or one that is always true). Whether that
equals a full refresh depends on the query and on how the sources change, and this rule decides it.

**Theorem.** Let ``Q`` be the model's query (incremental and full the same, after dropping always-true
filters and ``SELECT * FROM (...)`` wrappers), ``K`` its ``uniqueKey``, with no ``updatePartitionFilter``
and no statement in the incremental ``pre_operations`` besides script variables that substitute exactly.
Suppose that in every source state ``S`` the contract reaches

1. ``K`` is unique in ``Q(S)`` and never NULL there;
2. ``Q(S)`` does not depend on tie-breaking or chance (it is a function of ``S``);
3. the set of ``K`` values only grows: after an allowed change from ``S`` to ``S'``,
   ``π_K Q(S) ⊆ π_K Q(S')``.

Then after every run the table equals ``Q`` of the current sources.

*Proof.* By induction on runs. The first run builds ``T_0 = Q(S_0)``. Suppose ``T = Q(S)`` before a run
on sources ``S'``. Dataform runs ``MERGE T USING Q(S') ON T.K = Q.K WHEN MATCHED UPDATE all columns WHEN
NOT MATCHED INSERT``. By (1) applied to ``S`` and ``S'``, every row on both sides has a non-NULL ``K``
that no other row on its side shares, so each target row matches at most one source row (BigQuery's
"one target row, several source rows" error cannot happen) and each source row at most one target row.
By (3), every target row's key is in ``π_K Q(S')``, so every target row is matched and replaced by the
source row with its key; no old row survives. Source rows whose key was not in ``T`` are inserted. The
result holds each row of ``Q(S')`` exactly once, so it is ``Q(S')``. (2) makes "the" result of ``Q``
meaningful, so the full refresh it is compared with is the same table. ∎

The conditions are established statically: (1) with :func:`kumosql.output_properties.infer_properties`
under the key facts the contract preserves (:func:`kumosql.incremental_monotone.contract_constraints`),
or for a ``UNION ALL`` of branches that each carry a distinct constant tag; (2) with
:func:`kumosql.incremental_ties.tie_reasons`; (3) with :func:`kumosql.incremental_monotone.analyze`,
which must find every key column stable. Any doubt returns no proof.
"""

from __future__ import annotations

from typing import Iterable

import sqlglot
from sqlglot import exp

from .incremental import IncrementalModel, SourceTable, Verdict, modelled_exactly
from .incremental_monotone import analyze, contract_constraints, source_schema

RULE = "R7 merge of a full re-run"

# ``SELECT * EXCEPT/REPLACE/RENAME/ILIKE``: sqlglot 30 names the EXCEPT argument "except_", older releases "except".
_STAR_MODIFIERS = ("except_", "except", "replace", "rename", "ilike")


def _always_true(node: exp.Expression) -> bool:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.EQ) and isinstance(node.left, exp.Literal) and isinstance(node.right, exp.Literal):
        return node.left.this == node.right.this and node.left.is_string == node.right.is_string
    return False


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def canonical_query(sql: str, dialect: str = "bigquery") -> str | None:
    """``sql`` without always-true filters (``TRUE``, ``1 = 1``) and ``SELECT * FROM (q) [AS alias]`` wrappers."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None:
            continue
        kept = [c for c in _conjuncts(where.this) if not _always_true(c)]
        if not kept:
            select.set("where", None)
        else:
            condition = kept[0]
            for c in kept[1:]:
                condition = exp.and_(condition, c)
            select.set("where", exp.Where(this=condition))
    while True:
        inner = _wrapped(tree)
        if inner is None:
            break
        tree = inner
    return tree.sql(dialect=dialect)


def _wrapped(tree: exp.Expression) -> exp.Expression | None:
    if not isinstance(tree, exp.Select) or len(tree.expressions) != 1 or not isinstance(tree.expressions[0], exp.Star):
        return None
    if any(tree.expressions[0].args.get(k) for k in _STAR_MODIFIERS):
        return None
    if any(tree.args.get(k) for k in tree.args if k not in ("expressions", "from_", "from")):
        return None
    source = tree.args.get("from_") or tree.args.get("from")
    if source is None or not isinstance(source.this, exp.Subquery):
        return None
    inner = source.this.this
    with_ = tree.args.get("with_") or tree.args.get("with")
    if with_ is not None:
        return None
    return inner.copy()


def _tagged_union_unique(tree: exp.Expression, key: list[str], constraints, schema, dialect: str) -> bool:
    """A ``UNION [ALL]`` whose branches each select a different constant in one key column, and are
    each unique and non-NULL on the key: the tag tells branches apart, so the union is unique too."""

    from kumo_sql_prover.output_properties import infer_properties

    with_ = tree.args.get("with_") or tree.args.get("with")
    branches: list[exp.Expression] = []

    def collect(node):
        if isinstance(node, exp.Union):
            collect(node.left)
            collect(node.right)
        else:
            branches.append(node)

    if not isinstance(tree, exp.Union) or tree.args.get("limit") is not None or tree.args.get("order") is not None:
        return False
    collect(tree)
    if not all(isinstance(b, exp.Select) for b in branches):
        return False
    names = [e.alias_or_name.lower() for e in branches[0].expressions]
    if any(isinstance(e, exp.Star) for b in branches for e in b.expressions) or any(len(b.expressions) != len(names) for b in branches):
        return False
    try:
        positions = [names.index(k) for k in key]
    except ValueError:
        return False
    tagged = []
    for p in positions:
        values = [b.expressions[p].unalias() for b in branches]
        if all(isinstance(v, exp.Literal) for v in values) and len({(v.this, v.is_string) for v in values}) == len(values):
            tagged.append(p)
    if not tagged:
        return False
    for branch in branches:
        probe = branch.copy()
        if with_ is not None:
            probe.set("with_", with_.copy())
        props = infer_properties(probe.sql(dialect=dialect), constraints, schema, dialect)
        if props.unsupported or len(props.columns) != len(names):
            return False
        if not all(props.columns[p].non_null for p in positions):
            return False
        if not any(set(k.positions) <= set(positions) for k in props.keys):
            return False
    return True


# ---------------------------------------------------------------------------
# One row per partition: ROW_NUMBER de-duplication read as GROUP BY
# ---------------------------------------------------------------------------


def _is_one(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and node.this == "1"


def _rank_filter(condition: exp.Expression) -> exp.Expression | None:
    """The ranked side of ``x = 1``, ``x <= 1`` or ``x < 2`` (either way round), else None."""

    condition = condition.unnest() if isinstance(condition, exp.Paren) else condition
    if isinstance(condition, exp.EQ):
        if _is_one(condition.right):
            return condition.left
        if _is_one(condition.left):
            return condition.right
    if isinstance(condition, exp.LTE) and _is_one(condition.right):
        return condition.left
    if isinstance(condition, exp.GTE) and _is_one(condition.left):
        return condition.right
    if isinstance(condition, exp.LT) and isinstance(condition.right, exp.Literal) and condition.right.this == "2":
        return condition.left
    return None


def _row_number(node: exp.Expression | None) -> exp.Window | None:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Window) and isinstance(node.this, exp.RowNumber) and node.args.get("partition_by"):
        return node
    return None


def _grouped_select(select: exp.Select, window: exp.Window, schema: dict[str, list[str]]) -> exp.Select | None:
    """``select`` (which keeps the rows where ``window`` is 1) as ``GROUP BY`` the window's partition.

    Both have exactly one row per partition value, so the partition columns have the same values, the
    same keys and the same NULLs; every other column becomes ``MAX(...)``, which no analysis reads as
    stable or as part of a key. The result is only ever analysed, never run.
    """

    if select.args.get("group") is not None or select.args.get("having") is not None or select.args.get("limit") is not None:
        return None
    if any(isinstance(n, exp.AggFunc) and n.find_ancestor(exp.Window) is None for e in select.expressions for n in e.walk()):
        return None
    partition = [p.copy() for p in window.args["partition_by"]]
    wanted = {p.sql() for p in partition}
    names = {p.name.lower() for p in partition if isinstance(p, exp.Column)}
    outputs: list[exp.Expression] = []
    for e in select.expressions:
        if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
            source = select.args.get("from_") or select.args.get("from")
            if select.args.get("joins") or source is None or not isinstance(source.this, exp.Table):
                return None
            columns = schema.get(source.this.name.lower()) or schema.get(source.this.name)
            if columns is None:
                return None
            star = e if isinstance(e, exp.Star) else e.this
            dropped = {c.name.lower() for c in (star.args.get("except_") or star.args.get("except") or [])}
            if any(star.args.get(k) for k in ("replace", "rename", "ilike")):
                return None
            for column in columns:
                if column.lower() not in dropped:
                    outputs.append(exp.column(column) if column.lower() in names else exp.Max(this=exp.column(column)).as_(column))
            continue
        value, name = e.unalias(), e.alias_or_name
        if value.sql() == window.sql():
            outputs.append(exp.Literal.number(1).as_(name))
        elif value.sql() in wanted or (isinstance(value, exp.Column) and value.name.lower() in names and len(wanted) == len(names)):
            outputs.append(e.copy())
        elif value.find(exp.Window) is not None:
            outputs.append(exp.cast(exp.Null(), "INT64").as_(name))
        else:
            outputs.append(exp.Max(this=value.copy()).as_(name))
    grouped = select.copy()
    grouped.set("expressions", outputs)
    grouped.set("qualify", None)
    grouped.set("group", exp.Group(expressions=partition))
    return grouped


def dedup_abstraction(sql: str, schema: dict[str, list[str]], dialect: str = "bigquery") -> str | None:
    """``sql`` with each ``ROW_NUMBER() ... = 1`` de-duplication written as a ``GROUP BY`` (see
    :func:`_grouped_select`); None when there is none. Two shapes are read:

    * ``SELECT ... QUALIFY ROW_NUMBER() OVER (PARTITION BY p ...) = 1`` (or ``QUALIFY rn = 1`` on an
      output alias), the whole ``QUALIFY``;
    * ``SELECT ... FROM (SELECT ..., ROW_NUMBER() OVER (PARTITION BY p ...) AS rn ...) WHERE rn = 1``,
      the derived table (or a CTE read only there) the only ``FROM`` item and ``rn = 1`` the whole ``WHERE``.
    """

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    changed = False
    for select in list(tree.find_all(exp.Select)):
        qualify = select.args.get("qualify")
        if qualify is None:
            continue
        ranked = _rank_filter(qualify.this)
        window = _row_number(ranked)
        if window is None and isinstance(ranked, exp.Column) and not ranked.table:
            window = next((_row_number(e.unalias()) for e in select.expressions if e.alias_or_name.lower() == ranked.name.lower()), None)
        grouped = _grouped_select(select, window, schema) if window is not None else None
        if grouped is not None:
            if select is tree:
                tree = grouped
            else:
                select.replace(grouped)
            changed = True
    with_ = tree.args.get("with_") or tree.args.get("with")
    ctes = {c.alias_or_name.lower(): c for c in (with_.expressions if with_ is not None else [])}
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        source = select.args.get("from_") or select.args.get("from")
        if where is None or source is None or select.args.get("joins"):
            continue
        ranked = _rank_filter(where.this)
        if not isinstance(ranked, exp.Column):
            continue
        item = source.this
        if isinstance(item, exp.Subquery) and isinstance(item.this, exp.Select):
            inner = item.this
        elif isinstance(item, exp.Table) and not item.args.get("db") and item.name.lower() in ctes:
            readers = [t for t in tree.find_all(exp.Table) if not t.args.get("db") and t.name.lower() == item.name.lower()]
            inner = ctes[item.name.lower()].this
            if len(readers) != 1 or not isinstance(inner, exp.Select):
                continue
        else:
            continue
        window = next((_row_number(e.unalias()) for e in inner.expressions if e.alias_or_name.lower() == ranked.name.lower()), None)
        if window is None or inner.args.get("qualify") is not None:
            continue
        grouped = _grouped_select(inner, window, schema)
        if grouped is None:
            continue
        inner.replace(grouped)
        select.set("where", None)
        changed = True
    return tree.sql(dialect=dialect) if changed else None


def key_unique_and_non_null(
    sql: str, key: Iterable[str], sources: dict[str, SourceTable], kinds: Iterable[str], tables=None, dialect: str = "bigquery"
) -> bool:
    """Condition (1): ``key`` is unique and non-NULL in the query's output in every reachable state."""

    from kumo_sql_prover.output_properties import infer_properties

    key = [k.lower() for k in key]
    constraints = contract_constraints(sources, kinds, tables)
    schema = source_schema(sources)
    for query in filter(None, (sql, dedup_abstraction(sql, schema, dialect))):
        props = infer_properties(query, constraints, schema, dialect)
        if props.unsupported:
            continue
        names = [c.name for c in props.columns]
        if all(names.count(k) == 1 for k in key):
            positions = {names.index(k) for k in key}
            if all(props.columns[p].non_null for p in positions) and any(set(k.positions) <= positions for k in props.keys):
                return True
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return False
    return _tagged_union_unique(tree, key, constraints, schema, dialect)


def _keys_stable(query: str | None, key: list[str], sources, kinds, tables, model: IncrementalModel) -> bool:
    """Condition (3): every key column is stable under :func:`kumosql.incremental_monotone.analyze`."""

    growth = analyze(query, sources, kinds, tables, target=model.target, dialect=model.dialect) if query else None
    if growth is None or growth.stable is None:
        return False
    names = [n.lower() for n in growth.names]
    return all(names.count(k) == 1 for k in key) and {names.index(k) for k in key} <= growth.stable


def prove_full_rerun_merge(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str], tables: tuple[str, ...] | None = None
) -> Verdict | None:
    """R7 (see the module docstring); ``None`` when a condition cannot be shown."""

    from .incremental_ties import tie_reasons

    if not model.unique_key or model.update_partition_filter or model.pre_operations or model.full_pre_operations:
        return None
    if not modelled_exactly(model):
        return None
    full = canonical_query(model.full_sql, model.dialect)
    if full is None or full != canonical_query(model.incremental_sql, model.dialect):
        return None
    key = [k.lower() for k in model.unique_key]
    if not any(_keys_stable(query, key, sources, kinds, tables, model) for query in (full, dedup_abstraction(full, source_schema(sources), model.dialect))):
        return None
    if not key_unique_and_non_null(full, key, sources, kinds, tables, model.dialect):
        return None
    if tie_reasons(full, contract_constraints(sources, kinds, tables, exact_copies=True), source_schema(sources), dialect=model.dialect):
        return None
    return Verdict(
        "safe",
        RULE,
        "every run merges the whole query; uniqueKey is unique and non-NULL in its result and no key ever leaves it",
    )
