"""An aggregate over an outer join that also has ``ORDER BY`` / ``LIMIT`` / ``OFFSET`` reads the join as one derived relation.

``algebraic_equivalence._wrap_outer_join_aggregate`` turns

    SELECT a.k, COUNT(b.x) FROM a LEFT JOIN b ON c GROUP BY a.k

into ``SELECT kqj.c0 AS k, COUNT(kqj.c1) FROM (SELECT a.k AS c0, b.x AS c1 FROM a LEFT JOIN b ON c) AS kqj GROUP BY kqj.c0``,
the only form in which the prover reads an aggregate over an outer join. It leaves a select with a row tail alone,
so ``... GROUP BY a.k ORDER BY a.k LIMIT 10`` was declined outright ("aggregate over an outer join"), even against
its own text. This rule does the same renaming with the tail kept on the select:

    SELECT a.k, COUNT(b.x) FROM a LEFT JOIN b ON c GROUP BY a.k ORDER BY COUNT(b.x) DESC, a.k LIMIT 10
    SELECT kqj.c0 AS k, COUNT(kqj.c1) FROM (...) AS kqj GROUP BY kqj.c0 ORDER BY COUNT(kqj.c1) DESC, kqj.c0 LIMIT 10

The derived table has exactly the joined rows (one output row per joined row, the columns the select reads), and
every column the select reads is renamed to the derived column that carries it, in the select list, ``WHERE``,
``GROUP BY``, ``HAVING`` and ``ORDER BY`` alike. So each clause sees the same values on the same rows: the same
groups, the same sort keys, the same rows under the same ``LIMIT``.

An ``ORDER BY`` key is handled by what it names:

* an output position (``ORDER BY 2``) or a bare name that is an output column of the select (``ORDER BY cnt``)
  names the select's output before and after, and stays as written. A bare name is taken as an output name only
  when exactly one output has it; ``ORDER BY`` resolves a bare name to an output column before an input column
  (in a ``GROUP BY`` it is the other way round, which is why only ``ORDER BY`` gets this reading);
* any other key is an expression over the joined columns, and goes through the same renaming as the select list.
  Every column in it must be qualified by one of the select's sources, as the wrapped form requires everywhere.

``LIMIT`` and ``OFFSET`` are put back exactly as written (they read no column), so they cut the same sorted rows.

Declined (None): no tail (the plain rule handles it), and whatever the plain rule declines: ``DISTINCT``,
``QUALIFY``, windows, a ``WITH`` on the select, a subquery outside the ``FROM`` (in an ``ORDER BY`` key too),
a column without a source, a star other than ``COUNT(*)``.
"""

from __future__ import annotations

from sqlglot import exp

_TAIL = ("order", "limit", "offset")
_MARK = "kq_tail"


def _output_names(select: exp.Select) -> list[str]:
    return [(item.alias_or_name or "").lower() for item in select.expressions]


def _names_output(key: exp.Expression, names: list[str]) -> bool:
    """Whether an ORDER BY key names an output column of the select: by position, or by a name one output has."""

    if isinstance(key, exp.Literal):
        return not key.is_string and key.name.isdigit() and 1 <= int(key.name) <= len(names)
    return isinstance(key, exp.Column) and not key.table and isinstance(key.this, exp.Identifier) and names.count(key.name.lower()) == 1


def wrap_tailed_outer_join_aggregate(select: exp.Select) -> exp.Expression | None:
    """``select`` with its outer join read as the derived table ``kqj``, the row tail kept; None when not that shape."""

    from .algebraic_equivalence import _wrap_outer_join_aggregate

    order = select.args.get("order")
    if not any(select.args.get(k) for k in _TAIL):
        return None
    if not any((j.args.get("side") or "").upper() in ("LEFT", "RIGHT", "FULL") for j in select.args.get("joins") or []):
        return None
    if order is not None and (type(order) is not exp.Order or any(not isinstance(o, exp.Ordered) for o in order.expressions)):
        return None
    if order is not None and any(v for k, v in order.args.items() if k != "expressions"):
        return None  # ORDER SIBLINGS BY and the like
    if any(n.find_ancestor(exp.Select) is select for n in select.find_all(exp.Star) if not isinstance(n.parent, exp.Count)):
        return None
    names = _output_names(select)
    if any(name.startswith(_MARK) for name in names):
        return None
    copy = select.copy()
    tail = {key: copy.args.get(key) for key in _TAIL}
    for key in _TAIL:
        copy.set(key, None)
    moved: list[int] = []  # positions in ORDER BY whose key is renamed with the select list
    extras = []
    ordered = list(tail["order"].expressions) if tail["order"] is not None else []
    for position, item in enumerate(ordered):
        if _names_output(item.this, names):
            continue
        if any(not c.table for c in item.this.find_all(exp.Column)):
            return None  # a bare name that is not an output: its source is not known here
        extras.append(exp.alias_(item.this.copy(), f"{_MARK}{len(moved)}"))
        moved.append(position)
    width = len(copy.expressions)
    copy.set("expressions", list(copy.expressions) + extras)
    wrapped = _wrap_outer_join_aggregate(copy)
    if wrapped is None or len(wrapped.expressions) != width + len(extras):
        return None
    renamed = wrapped.expressions[width:]
    if any(not isinstance(e, exp.Alias) or e.alias != f"{_MARK}{i}" for i, e in enumerate(renamed)):
        return None
    wrapped.set("expressions", wrapped.expressions[:width])
    for position, extra in zip(moved, renamed):
        ordered[position].set("this", extra.this.copy())
    for key in _TAIL:
        wrapped.set(key, tail[key])
    return wrapped
