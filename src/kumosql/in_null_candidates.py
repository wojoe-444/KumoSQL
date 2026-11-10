"""Drop a filter of an ``IN`` subquery that only removes NULL candidates, where NULL candidates cannot matter.

``WHERE v IN (SELECT y.c FROM (SELECT .., MIN(x.c) OVER (PARTITION BY ..) AS b, x.c AS c FROM ..) AS y WHERE y.b >= y.b)``
keeps the candidates whose window value ``b`` is not NULL. ``b`` is the minimum (or maximum) of ``x.c`` over a window
frame that holds the row itself, so ``b`` is NULL only when the row's own ``c`` is NULL: the filter removes NULL
candidates and nothing else. A NULL candidate never makes ``v IN (..)`` TRUE; it can only turn FALSE into UNKNOWN, and
a WHERE (through ``AND``) rejects a row for either. So in that position the filter can go:
``WHERE v IN (SELECT y.c FROM (..) AS y)``.

The filter is ``y.b >= y.b``, ``y.b <= y.b``, ``y.b = y.b`` or ``y.b IS NOT NULL`` (all TRUE exactly when ``y.b`` is
not NULL), one conjunct of the subquery's WHERE. Declined unless: the ``IN`` (not ``NOT IN``) sits in a select's
WHERE under ``AND`` and parentheses only; the subquery is a plain select (no grouping, aggregate, window, ``QUALIFY``,
``LIMIT``) whose one output is ``y.c`` and whose only source is the derived table ``y``; ``y`` is a plain select
without ``DISTINCT``, grouping, aggregate, star, ``ORDER BY`` or ``LIMIT`` whose outputs ``b`` and ``c`` are named once
each; ``b`` is ``MIN(col)`` or ``MAX(col)`` ``OVER`` a partition and/or order with no explicit frame (the default frame
always includes the current row) and ``c`` is that same column ``col``.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import conjuncts

_SCOPES = (exp.Select, exp.SetOperation, exp.Subquery)
_ALWAYS_TRUE_UNLESS_NULL = (exp.GTE, exp.LTE, exp.EQ)
# types whose every non-NULL value equals itself (FLOAT64 has NaN, for which NaN >= NaN is FALSE)
_REFLEXIVE_TYPES = {
    "INT64", "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT", "NUMERIC", "BIGNUMERIC", "DECIMAL",
    "BIGDECIMAL", "STRING", "BYTES", "DATE", "DATETIME", "TIME", "TIMESTAMP", "BOOL", "BOOLEAN",
}


def _own_scope(select: exp.Select):
    return select.dfs(prune=lambda n: n is not select and isinstance(n, _SCOPES))


def _positive_where(node: exp.Expression) -> bool:
    parent = node.parent
    while isinstance(parent, (exp.And, exp.Paren)):
        parent = parent.parent
    return isinstance(parent, exp.Where) and isinstance(parent.parent, exp.Select)


def _plain(select: exp.Select) -> bool:
    if any(select.args.get(k) for k in ("group", "having", "qualify", "order", "limit", "offset", "windows", "with", "with_")):
        return False
    return not any(isinstance(n, (exp.AggFunc, exp.Window)) for n in _own_scope(select))


def _null_test_of(node: exp.Expression, alias: str) -> tuple[str, bool] | None:
    """``(b, reflexive)`` when ``node`` is TRUE exactly when ``alias.b`` is not NULL: ``alias.b IS NOT NULL``, or a
    comparison ``alias.b >= alias.b`` (``reflexive``: only when every value of ``b`` equals itself)."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, _ALWAYS_TRUE_UNLESS_NULL):
        left, right = node.this, node.expression
        same = isinstance(left, exp.Column) and isinstance(right, exp.Column) and left == right
        if same and left.table.lower() == alias and not left.args.get("db"):
            return left.name.lower(), True
    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
        column = node.this.this
        if isinstance(column, exp.Column) and column.table.lower() == alias and not column.args.get("db"):
            return column.name.lower(), False
    return None


def _column_type(select: exp.Select, column: exp.Column, types: dict[str, dict[str, str]]) -> str | None:
    """The declared type of ``column`` when it reads a base table of ``select``'s own FROM, else ``None``."""

    if not column.table or column.args.get("db"):
        return None
    sources = [(select.args.get("from_") or select.args.get("from"))] + list(select.args.get("joins") or [])
    for holder in sources:
        source = holder.this if holder is not None else None
        if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier) and source.alias_or_name.lower() == column.table.lower():
            key = ".".join(p.name for p in source.parts).lower()
            columns = types.get(key) or (types.get(source.name.lower()) if source.db else None) or {}
            raw = columns.get(column.name.lower())
            return str(raw).upper().replace(" ", "") if raw else None
    return None


def _outputs(select: exp.Select) -> dict[str, exp.Expression] | None:
    out: dict[str, exp.Expression] = {}
    for item in select.expressions:
        name = (item.alias_or_name or "").lower()
        if not name or name in out or isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        out[name] = item.this if isinstance(item, exp.Alias) else item
    return out


def _covers_own_row(window: exp.Expression, column: exp.Expression) -> bool:
    """``window`` is ``MIN(column)``/``MAX(column)`` over a frame that always holds the current row."""

    if not isinstance(window, exp.Window) or not isinstance(window.this, (exp.Min, exp.Max)):
        return False
    if any(window.args.get(k) for k in window.args if k not in ("this", "partition_by", "order", "over")):
        return False
    function = window.this
    if function.expressions or not isinstance(function.this, exp.Column):
        return False
    return function.this == column


def _drop_one(in_node: exp.In, types: dict[str, dict[str, str]]) -> bool:
    query = in_node.args.get("query")
    if in_node.args.get("unnest") or in_node.args.get("field") or in_node.expressions or not isinstance(query, exp.Subquery):
        return False
    if not _positive_where(in_node):
        return False
    select = query.this
    if not isinstance(select, exp.Select) or select.args.get("joins") or not _plain(select) or len(select.expressions) != 1:
        return False
    from_ = select.args.get("from_") or select.args.get("from")
    derived = from_.this if from_ is not None else None
    where = select.args.get("where")
    if not isinstance(derived, exp.Subquery) or not derived.alias or where is None:
        return False
    alias = derived.alias.lower()
    output = select.expressions[0]
    output = output.this if isinstance(output, exp.Alias) else output
    if not isinstance(output, exp.Column) or output.table.lower() != alias or output.args.get("db"):
        return False
    inner = derived.this
    if not isinstance(inner, exp.Select) or inner.args.get("distinct"):
        return False
    if any(inner.args.get(k) for k in ("group", "having", "order", "limit", "offset", "windows", "with", "with_")):
        return False
    if any(isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window) for n in _own_scope(inner)):
        return False
    outputs = _outputs(inner)
    candidate = output.name.lower()
    if outputs is None or candidate not in outputs or not isinstance(outputs[candidate], exp.Column):
        return False
    parts = conjuncts(where.this)
    for part in parts:
        test = _null_test_of(part, alias)
        if test is None or test[0] not in outputs or not _covers_own_row(outputs[test[0]], outputs[candidate]):
            continue
        if test[1] and _column_type(inner, outputs[candidate], types) not in _REFLEXIVE_TYPES:
            continue
        rest = [p for p in parts if p is not part]
        if rest:
            select.set("where", exp.Where(this=exp.and_(*(p.copy() for p in rest))))
        else:
            select.set("where", None)
        return True
    return False


def drop_null_candidate_filters(tree: exp.Expression, types: dict[str, dict[str, str]] | None = None) -> exp.Expression:
    """Apply the rewrite of the module doc to every ``IN`` subquery of ``tree``. Rewrites in place."""

    typed = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in (types or {}).items()}
    for in_node in list(tree.find_all(exp.In)):
        while _drop_one(in_node, typed):
            pass
    return tree
