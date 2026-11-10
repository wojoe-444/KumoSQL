"""A filter above the derived table that wraps an outer join is evaluated inside it.

The prover reads an aggregate over an outer join through one derived table of the join's columns
(``algebraic_equivalence._wrap_outer_join_aggregate``):

    SELECT kqj.c0, COUNT(kqj.c1) FROM (SELECT a.k AS c0, b.x AS c1, a.f AS c2 FROM a LEFT JOIN b ON ..) AS kqj
    WHERE kqj.c2 = 1 GROUP BY kqj.c0

The ``WHERE`` stays above the derived table there. A rewrite that pushes the filter below the join instead
(``FROM (SELECT * FROM a WHERE f = 1) AS a LEFT JOIN b ON ..``, Calcite's filter pushdown) has no ``WHERE`` above
it, and the two forms never met: an aggregate over a filtered relation against an aggregate over another relation.

``push_filter_into_wrapped_join`` moves the filter inside:

    SELECT kqj.c0, COUNT(kqj.c1) FROM (SELECT a.k AS c0, b.x AS c1 .. FROM a LEFT JOIN b ON .. WHERE a.f = 1) AS kqj
    GROUP BY kqj.c0

This is exact for every condition, whichever side of the join it reads: the derived table outputs one row per
joined row and only passes columns on, so a condition on its outputs keeps exactly the rows on which the same
condition, written over the columns those outputs read, holds. In one select a ``WHERE`` is applied to the rows
the joins produce (padded rows included), which is the same set of rows the outer ``WHERE`` saw. TRUE, FALSE and
NULL fall the same way, duplicates are kept or dropped together, and with no rows nothing changes. Inside, the
join and its filter are one select, where the prover reads outer joins directly and can compare the filter with
one pushed below the join.

A conjunct moves only if every column in it is an output of the derived table and it holds no subquery,
aggregate or window. The derived table must be the select's only source and must only pass columns on under
distinct names: no ``DISTINCT``, grouping, ``HAVING``, window, ``ORDER BY``, ``LIMIT`` or ``OFFSET`` (a filter does not
commute with those), and at least one outer join (other derived tables have their own rules). A ``WHERE`` it
already has is kept and the moved conjuncts are added to it.
"""

from __future__ import annotations

from sqlglot import exp

_OUTER = ("LEFT", "RIGHT", "FULL")
_INNER_ARGS = frozenset({"expressions", "from_", "from", "joins", "where"})


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _and_all(parts: list[exp.Expression]) -> exp.Expression:
    parts = [exp.Paren(this=p) if isinstance(p, (exp.Or, exp.Xor)) else p for p in parts]  # they bind looser than AND
    result = parts[0]
    for part in parts[1:]:
        result = exp.And(this=result, expression=part)
    return result


def _passed_columns(inner: exp.Select) -> dict[str, exp.Column] | None:
    """Output name -> the column it passes on, for a select that only passes columns on under distinct names."""

    outputs: dict[str, exp.Column] = {}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        name = (item.alias_or_name or "").lower()
        if not isinstance(value, exp.Column) or not isinstance(value.this, exp.Identifier) or not name or name in outputs:
            return None
        outputs[name] = value
    return outputs


def push_filter_into_wrapped_join(select: exp.Select) -> exp.Expression | None:
    """``select`` with the conjuncts of its ``WHERE`` that read only its derived outer join moved into it, or None."""

    where = select.args.get("where")
    from_ = select.args.get("from_") or select.args.get("from")
    if where is None or from_ is None or select.args.get("joins"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    if any(value for key, value in source.args.items() if key not in ("this", "alias")) or source.args["alias"].args.get("columns"):
        return None
    inner = source.this
    if any(value for key, value in inner.args.items() if key not in _INNER_ARGS):
        return None
    joins = inner.args.get("joins") or []
    if not any((j.args.get("side") or "").upper() in _OUTER for j in joins):
        return None
    if any(isinstance(n, (exp.Window, exp.AggFunc)) for item in inner.expressions for n in item.walk()):
        return None
    outputs = _passed_columns(inner)
    if not outputs:
        return None
    alias = source.alias.lower()
    moved, kept = [], []
    for part in _conjuncts(where.this):
        columns = list(part.find_all(exp.Column))
        movable = (
            bool(columns)
            and not any(isinstance(n, (exp.Query, exp.Subquery, exp.Exists, exp.Window, exp.AggFunc, exp.Star)) for n in part.walk())
            and all(c.table.lower() == alias and isinstance(c.this, exp.Identifier) and c.name.lower() in outputs and not c.args.get("db") for c in columns)
        )
        (moved if movable else kept).append(part)
    if not moved:
        return None
    copy = select.copy()
    new_inner = (copy.args.get("from_") or copy.args.get("from")).this.this
    pushed = []
    for part in moved:
        holder = exp.Paren(this=part.copy())
        for column in list(holder.find_all(exp.Column)):
            column.replace(outputs[column.name.lower()].copy())
        pushed.append(holder.this)
    existing = new_inner.args.get("where")
    new_inner.set("where", exp.Where(this=_and_all(([existing.this] if existing is not None else []) + pushed)))
    copy.set("where", exp.Where(this=_and_all([k.copy() for k in kept])) if kept else None)
    return copy
