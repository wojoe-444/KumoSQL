"""Exact three-valued Boolean identities inside one SELECT, and constant-TRUE filters dropped.

Each identity holds for TRUE, FALSE and NULL alike, so it changes no row:

* ``NOT NOT p`` is ``p`` (``NOT`` swaps TRUE and FALSE and keeps NULL);
* ``p OR FALSE`` and ``p AND TRUE`` are ``p``; ``NOT TRUE`` is FALSE and ``NOT FALSE`` is TRUE;
* ``q IS NULL`` is FALSE (and ``NOT q IS NULL`` TRUE) when ``q`` is never NULL: an ``IS`` test
  (``IS [NOT] NULL``, ``IS [NOT] TRUE/FALSE``), ``IS [NOT] DISTINCT FROM``, ``EXISTS``, a TRUE/FALSE literal,
  and ``NOT``/``AND``/``OR`` over those. ``(a = 1) IS NULL`` is kept: a comparison can be NULL.

``p`` must be a predicate (a comparison, an ``IS``/``IN``/``LIKE``/``BETWEEN``/``EXISTS`` test, a connective or
a TRUE/FALSE literal): in dialects where ``AND``/``OR``/``NOT`` take numbers (MySQL, SQLite) ``NOT NOT 5`` is 1,
not 5, while a predicate is TRUE, FALSE or NULL (1, 0 or NULL) there too.

``WHERE TRUE`` and ``QUALIFY TRUE`` filter nothing and are dropped. ``HAVING TRUE`` is dropped only from a
select that is grouped without it (a ``GROUP BY`` key or an aggregate of its own): ``SELECT 1 FROM t HAVING TRUE``
is a one-group query that returns one row however many rows ``t`` has. A FALSE or NULL filter is never dropped.
"""

from __future__ import annotations

from sqlglot import exp

_PREDICATES = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ,
    exp.Is, exp.In, exp.Exists, exp.Like, exp.ILike, exp.Between,
    exp.And, exp.Or, exp.Not, exp.Boolean,
)
_ATOMS = (exp.Boolean, exp.Column, exp.Literal, exp.Null, exp.Exists, exp.Paren)


def _bare(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _predicate(node: exp.Expression) -> bool:
    return isinstance(_bare(node), _PREDICATES)


def _truth(node: exp.Expression) -> bool | None:
    node = _bare(node)
    return bool(node.this) if isinstance(node, exp.Boolean) else None


def _never_null(node: exp.Expression) -> bool:
    """Whether ``node`` is a Boolean that is TRUE or FALSE on every row, never NULL."""

    node = _bare(node)
    if isinstance(node, exp.Boolean):
        return True
    if isinstance(node, exp.Is):
        return isinstance(node.expression, (exp.Null, exp.Boolean))
    if isinstance(node, (exp.NullSafeEQ, exp.NullSafeNEQ, exp.Exists)):
        return True
    if isinstance(node, exp.Not):
        return _never_null(node.this)
    if isinstance(node, (exp.And, exp.Or)):
        return _never_null(node.this) and _never_null(node.expression)
    return False


def _fold(node: exp.Expression) -> exp.Expression | None:
    """The simpler equal form of ``node``, or None."""

    if isinstance(node, exp.Not):
        inner = _bare(node.this)
        if isinstance(inner, exp.Boolean):
            return exp.Boolean(this=not inner.this)
        if isinstance(inner, exp.Not) and _predicate(inner.this):
            return inner.this
        return None
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) and _never_null(node.this):
        return exp.Boolean(this=bool(node.args.get("negate")))  # some dialects keep IS NOT NULL as a flag
    if isinstance(node, (exp.And, exp.Or)):
        unit = isinstance(node, exp.And)  # TRUE is AND's identity, FALSE is OR's
        for side, other in ((node.this, node.expression), (node.expression, node.this)):
            if _truth(side) is unit and _predicate(other):
                return other
    return None


def _placed(result: exp.Expression, holder: exp.Expression | None) -> exp.Expression:
    """``result`` copied, with parentheses only where printing it under ``holder`` needs them."""

    result = _bare(result).copy()
    if isinstance(result, _ATOMS) or isinstance(holder, (exp.Where, exp.Having, exp.Qualify, exp.Paren, exp.Join, exp.Select, exp.Alias, exp.If)):
        return result
    if isinstance(holder, (exp.And, exp.Or, exp.Not)) and not isinstance(result, exp.Connector):
        return result
    return exp.Paren(this=result)


def _own_nodes(select: exp.Select) -> list[exp.Expression]:
    """The nodes of ``select`` outside its nested queries, children before their parents."""

    order: list[exp.Expression] = []

    def visit(node: exp.Expression) -> None:
        for child in list(node.iter_expressions()):
            if isinstance(child, (exp.Select, exp.SetOperation)):
                continue  # a nested query is a SELECT of its own, folded when it is visited
            visit(child)
        order.append(node)

    for key, value in select.args.items():
        for child in value if isinstance(value, list) else [value]:
            if isinstance(child, exp.Expression) and not isinstance(child, (exp.Select, exp.SetOperation)):
                visit(child)
    return order


def _grouped_without_having(select: exp.Select) -> bool:
    group = select.args.get("group")
    if group is not None and group.expressions:
        return True
    for aggregate in select.find_all(exp.AggFunc):
        if isinstance(aggregate, (exp.Max, exp.Min)) and aggregate.expressions:
            continue  # SQLite's MAX(a, b) is a scalar
        owner = aggregate.parent
        while owner is not None and owner is not select:
            if isinstance(owner, (exp.Window, exp.Select, exp.SetOperation, exp.Having)):
                break  # windowed, nested, or only in the HAVING being dropped
            owner = owner.parent
        if owner is select:
            return True
    return False


def fold_boolean_identities(select: exp.Select) -> exp.Select | None:
    """``select`` with the identities above applied in place, or None when none applies."""

    changed = False
    for node in _own_nodes(select):
        if node.parent is None:
            continue
        replacement = _fold(node)
        if replacement is not None:
            node.replace(_placed(replacement, node.parent))
            changed = True
    for key in ("where", "qualify", "having"):
        clause = select.args.get(key)
        if clause is None or _truth(clause.this) is not True:
            continue
        if key == "having" and not _grouped_without_having(select):
            continue
        select.set(key, None)
        changed = True
    return select if changed else None
