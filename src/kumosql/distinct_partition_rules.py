"""Merge ``UNION DISTINCT`` branches that are one query split by WHERE filters (TLP DISTINCT partitions).

Under set semantics, branches that are the same ``SELECT`` (same select list, FROM and joins) except for
their WHERE filters ``f1 .. fk`` are one branch filtered by ``f1 OR .. OR fk``: a row of the shared source
reaches the union exactly when some filter is TRUE for it, which is exactly when the three-valued OR is
TRUE, and the union removes every duplicate, so unlike the ``UNION ALL`` merge in ``partition_rules`` the
filters need not be disjoint. When the OR is TRUE for every row the filter is dropped, so
``Q WHERE p UNION DISTINCT Q WHERE NOT p UNION DISTINCT Q WHERE p IS NULL`` is ``SELECT DISTINCT`` of ``Q``.
That check reuses ``partition_rules``' three-valued encoding, which reads every comparison as an opaque
atom (the same atom has the same value in every branch because the branches read the same rows), so it
can only miss a covering, never invent one.

A ``UNION DISTINCT`` removes duplicates across all of its branches, nested ``UNION ALL`` operands
included, so the leaves of a distinct union are read as one set: the merged branch stays in a
``UNION DISTINCT`` with the leaves it is not merged with, or becomes ``SELECT DISTINCT`` when it is the only
branch left. Whether a branch was ``SELECT DISTINCT`` itself does not matter there (``DISTINCT ON`` does).

A branch only merges when moving its filter cannot change which rows its source yields or what they
project: no aggregates, windows, LIMIT/OFFSET, sampling, nondeterministic or unknown functions anywhere in
it, no GROUP BY, HAVING, QUALIFY, ORDER BY or other clauses, and no subquery in the filter.
"""

from __future__ import annotations

import z3
from sqlglot import exp

from .partition_rules import _Logic, _shape, _volatile

_MAX_BRANCHES = 32
_BRANCH_ARGS = {"expressions", "from_", "joins", "where", "distinct"}
_UNSAFE_ANYWHERE = (exp.AggFunc, exp.Window, exp.Limit, exp.Offset, exp.Fetch, exp.TableSample, exp.Anonymous)
_UNSAFE_IN_FILTER = (exp.Query, exp.Exists)
_WRAPPED_SELECT_ARGS = ("with_", "order", "limit", "offset", "locks", "sample", "settings", "format", "options", "for_")


def merge_distinct_partitions(tree: exp.Expression) -> exp.Expression:
    """Merge the filter-split branches of every ``UNION DISTINCT`` in ``tree`` (innermost first)."""

    for node in list(tree.find_all(exp.Union))[::-1]:  # breadth-first, reversed: a node after its descendants
        if not _plain_union(node) or not node.args.get("distinct") or _covered(node):
            continue
        replacement = _merge(node)
        if replacement is None:
            continue
        if node is tree:
            tree = replacement
            continue
        if isinstance(node.parent, exp.SetOperation):
            replacement = exp.Subquery(this=replacement)
        node.replace(replacement)
    return tree


# -- the distinct region of a union -----------------------------------------------------------------


def _plain_union(node: exp.Expression | None) -> bool:
    """A UNION whose result is just its operands' rows (no BY NAME, ORDER BY, LIMIT, WITH, ...)."""

    return type(node) is exp.Union and not any(v for k, v in node.args.items() if k not in ("this", "expression", "distinct"))


def _plain_wrapper(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.Subquery) and not any(v for k, v in node.args.items() if k != "this")


def _region_parent(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while _plain_wrapper(parent):
        parent = parent.parent
    return parent if _plain_union(parent) else None


def _covered(node: exp.Expression) -> bool:
    """Whether an enclosing UNION DISTINCT already reads ``node``'s leaves as part of its own set."""

    parent = _region_parent(node)
    while parent is not None:
        if parent.args.get("distinct"):
            return True
        parent = _region_parent(parent)
    return False


def _leaves(node: exp.Expression, out: list[exp.Expression]) -> None:
    while _plain_wrapper(node):
        node = node.this
    if _plain_union(node):
        _leaves(node.this, out)
        _leaves(node.expression, out)
    else:
        out.append(node)


# -- branches -----------------------------------------------------------------------------------------


def _branch_key(leaf: exp.Expression) -> str | None:
    """The branch without its WHERE (and plain DISTINCT), when its filter can move between branches."""

    if not isinstance(leaf, exp.Select) or any(v for k, v in leaf.args.items() if k not in _BRANCH_ARGS):
        return None
    distinct = leaf.args.get("distinct")
    if distinct is not None and any(distinct.args.values()):
        return None  # DISTINCT ON keeps one row per key, which a moved filter changes
    if _volatile(leaf) or any(isinstance(n, _UNSAFE_ANYWHERE) for n in leaf.walk()):
        return None
    where = leaf.args.get("where")
    if where is not None and any(isinstance(n, _UNSAFE_IN_FILTER) for n in where.walk()):
        return None
    rest = leaf.copy()
    rest.set("where", None)
    rest.set("distinct", None)
    return _shape(rest)


def _operand(leaf: exp.Expression) -> exp.Expression:
    if isinstance(leaf, exp.Subquery) or (isinstance(leaf, exp.Select) and not any(leaf.args.get(k) for k in _WRAPPED_SELECT_ARGS)):
        return leaf
    return exp.Subquery(this=leaf)


def _parenthesized(condition: exp.Expression) -> exp.Expression:
    return condition.copy() if isinstance(condition, exp.Paren) else exp.Paren(this=condition.copy())


def _merge(node: exp.Expression) -> exp.Expression | None:
    leaves: list[exp.Expression] = []
    _leaves(node, leaves)
    if len(leaves) < 2 or len(leaves) > _MAX_BRANCHES:
        return None
    groups: dict[str, list[int]] = {}
    for index, leaf in enumerate(leaves):
        key = _branch_key(leaf)
        if key is not None:
            groups.setdefault(key, []).append(index)
    replaced: dict[int, exp.Expression | None] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        filters = [leaves[i].args.get("where") for i in members]
        logic = _Logic()
        passes = [logic.encode(where.this if where is not None else None)[0] for where in filters]
        merged = leaves[members[0]].copy()
        merged.set("distinct", None)
        if any(where is None for where in filters) or logic.unsat(z3.Not(z3.Or(*passes))):
            merged.set("where", None)  # every row passes some filter
        else:
            merged.set("where", exp.Where(this=exp.or_(*[_parenthesized(where.this) for where in filters])))
        replaced[members[0]] = merged
        for index in members[1:]:
            replaced[index] = None
    if not replaced:
        return None
    kept = [replaced[i] if i in replaced else leaf.copy() for i, leaf in enumerate(leaves)]
    kept = [leaf for leaf in kept if leaf is not None]
    if len(kept) == 1:
        kept[0].set("distinct", exp.Distinct())  # the union's duplicate removal, now that it is the only branch
        return kept[0]
    result = _operand(kept[0])
    for leaf in kept[1:]:
        result = exp.Union(this=result, expression=_operand(leaf), distinct=True)
    return result
