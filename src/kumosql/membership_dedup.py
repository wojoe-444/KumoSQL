"""Duplicate removal under an ``IN`` or ``EXISTS`` subquery is not needed.

``x IN (q)`` and ``EXISTS (q)`` read only which rows ``q`` has, never how many times each appears: ``IN`` is
TRUE when some row equals ``x``, NULL when none does but one is NULL (or ``x`` is), FALSE otherwise; ``EXISTS``
is whether ``q`` has a row. So under them ``A UNION DISTINCT B`` can be read as ``A UNION ALL B`` and
``SELECT DISTINCT`` as ``SELECT``: both have the same set of rows.

The reading carries down a chain of derived tables as long as each level keeps "the set of rows below
decides the set of rows here": a ``SELECT`` of deterministic expressions from one derived table filtered
by ``WHERE`` (its output set is ``{f(r) | r in set(source), p(r)}``), or a ``UNION`` (``set(A ∪ B)`` is
``set(A) ∪ set(B)``, ``ALL`` or not). It stops at anything that counts or picks rows: an aggregate,
``GROUP BY``/``HAVING``, a window, ``QUALIFY``, ``ORDER BY``, ``LIMIT``/``OFFSET``, ``DISTINCT ON``, a join, a
sample, ``INTERSECT``/``EXCEPT`` (``EXCEPT ALL`` keeps surplus copies), and at a random or unknown function
(a repeated row draws its own random value).
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import plain_distinct

_SELECT_ARGS = frozenset({"expressions", "from_", "where", "distinct", "kind"})
_UNION_ARGS = frozenset({"this", "expression", "distinct"})
_VOLATILE = (exp.Rand, exp.Randn, exp.Randstr, exp.Uuid, exp.Anonymous)


def _own_scope(select: exp.Expression):
    """The nodes of ``select`` outside its nested queries."""

    return select.walk(prune=lambda n: n is not select and isinstance(n, (exp.Select, exp.SetOperation)))


def _set_preserving(select: exp.Select) -> bool:
    """Whether the set of ``select``'s rows is decided by the set of its one derived source's rows."""

    if any(value for key, value in select.args.items() if key not in _SELECT_ARGS):
        return False
    if select.args.get("kind") or (select.args.get("distinct") is not None and not plain_distinct(select)):
        return False
    if any(isinstance(node, (exp.AggFunc, exp.Window)) for node in _own_scope(select)):
        return False
    read = [*select.expressions, select.args.get("where")]  # the source's own levels are checked when reached
    return not any(isinstance(node, _VOLATILE) for part in read if part is not None for node in part.walk())


def _relax(query: exp.Expression) -> bool:
    """Read the duplicate removal in ``query`` (whose row set alone matters) as none; True if any changed."""

    if isinstance(query, exp.Subquery):
        if any(value for key, value in query.args.items() if key not in ("this", "alias")):
            return False
        return _relax(query.this)
    if type(query) is exp.Union:
        if any(value for key, value in query.args.items() if key not in _UNION_ARGS):
            return False
        changed = bool(query.args.get("distinct"))
        if changed:
            query.set("distinct", False)
        changed = _relax(query.this) or changed
        return _relax(query.expression) or changed
    if isinstance(query, exp.Select) and _set_preserving(query):
        changed = query.args.get("distinct") is not None
        if changed:
            query.set("distinct", None)
        source = query.args.get("from_")
        if source is not None and isinstance(source.this, exp.Subquery):
            changed = _relax(source.this) or changed
        return changed
    return False


def relax_membership_dedup(select: exp.Select) -> exp.Select | None:
    """``select`` with the duplicate removal under each of its own ``IN``/``EXISTS`` subqueries read as none."""

    changed = False
    for node in list(_own_scope(select)):
        query = None
        if isinstance(node, exp.In) and not any(node.args.get(k) for k in ("expressions", "unnest", "field")):
            query = node.args.get("query")
            if isinstance(query, exp.Subquery) and not any(v for k, v in query.args.items() if k != "this"):
                query = query.this
        elif isinstance(node, exp.Exists) and not node.args.get("expression"):
            query = node.this
        # only the query itself: ``x IN ((SELECT ..))`` may be read as a list holding one scalar subquery,
        # which fails on a second row, so a doubly parenthesized query is left alone
        if isinstance(query, (exp.Select, exp.SetOperation)):
            changed = _relax(query) or changed
    return select if changed else None
