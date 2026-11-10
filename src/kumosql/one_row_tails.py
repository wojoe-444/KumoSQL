"""A select that returns at most one row needs no ``ORDER BY`` and no ``LIMIT n`` with ``n >= 1``.

    SELECT COUNT(DISTINCT o.id) AS orders, SUM(o.cost) AS cost FROM o WHERE .. ORDER BY COUNT(DISTINCT o.id) LIMIT 100

An aggregate without ``GROUP BY`` returns exactly one row (none if a ``HAVING`` rejects it), whatever the input.
Sorting at most one row changes nothing, and neither does keeping the first ``n >= 1`` of them, so the tail is
dropped. TPC-DS writes its one-row reports with such a tail, and a rewrite that leaves the pointless ``ORDER BY``
or ``LIMIT`` out was compared as a query with a ``LIMIT`` against one without.

The select must be a global aggregate by its select list or ``HAVING`` alone: an aggregate that appears only in
the ``ORDER BY`` would stop making the select an aggregate once the ``ORDER BY`` is gone, and the select would
return a row per input row.

Declined (the tail stays):

* a select inside another select (a subquery, a derived table): there an aggregate over an enclosing query's
  column belongs to that query, and the subquery itself may return many rows. Only a select with no select
  around it is read;
* a ``GROUP BY`` of any kind (``GROUP BY ()`` included), ``DISTINCT ON``, ``QUALIFY``, a window function;
* ``OFFSET`` (it can drop the one row), ``LIMIT 0``, a ``LIMIT`` that is not a plain integer literal (a parameter,
  an expression, ``LIMIT m, n``, ``WITH TIES``, ``PERCENT``), ``FETCH``;
* a select list with a function that can return several rows (``UNNEST``, ``EXPLODE``, ``GENERATE_SERIES``, a
  table function, a function sqlglot does not know): PostgreSQL then returns a row per element, and the tail
  would choose among them;
* ``ORDER BY`` forms other than a list of keys.
"""

from __future__ import annotations

import re

from sqlglot import exp

_TAIL = ("order", "limit")
_ROW_MAKERS = (exp.UDTF, exp.Unnest, exp.Explode, exp.GenerateSeries, exp.Anonymous)
_POSITIVE = re.compile(r"[1-9][0-9]*")


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def _aggregates_outside_tail(select: exp.Select) -> bool:
    """Whether the select list or HAVING holds an aggregate of this select (not one inside a window)."""

    parts = list(select.expressions) + ([select.args["having"]] if select.args.get("having") is not None else [])
    return any(_own(a, select) and a.find_ancestor(exp.Window) is None for part in parts for a in part.find_all(exp.AggFunc))


def drop_one_row_tail(select: exp.Select) -> exp.Expression | None:
    """``select`` without its ``ORDER BY`` / ``LIMIT n`` when it is a global aggregate (at most one row), else None."""

    order, limit = select.args.get("order"), select.args.get("limit")
    if order is None and limit is None:
        return None
    if select.find_ancestor(exp.Select) is not None:
        return None  # inside another select an aggregate may belong to that select (SUM(outer.x)): top level only
    if select.args.get("group") is not None or select.args.get("offset") is not None or select.args.get("qualify") is not None:
        return None
    distinct = select.args.get("distinct")
    if isinstance(distinct, exp.Distinct) and distinct.args.get("on"):
        return None
    if not _aggregates_outside_tail(select):
        return None
    if any(_own(n, select) for n in select.find_all(exp.Window)):
        return None
    if any(isinstance(n, _ROW_MAKERS) and _own(n, select) for item in select.expressions for n in item.walk()):
        return None
    if order is not None and (type(order) is not exp.Order or any(v for k, v in order.args.items() if k != "expressions")):
        return None
    if limit is not None:
        if type(limit) is not exp.Limit or any(v for k, v in limit.args.items() if k != "expression"):
            return None
        count = limit.expression
        if not isinstance(count, exp.Literal) or count.is_string or not _POSITIVE.fullmatch(count.name):
            return None
    copy = select.copy()
    for key in _TAIL:
        copy.set(key, None)
    return copy
