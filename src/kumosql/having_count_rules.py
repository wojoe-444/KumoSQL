"""``COUNT`` tests in ``HAVING``: an implied lower bound is dropped, and a counted key is not NULL.

Every aggregate of one ``HAVING`` clause reads the same group, and for any group
``COUNT(*) >= COUNT(x) >= COUNT(DISTINCT x)`` (the rows, the rows where ``x`` is not NULL, the different
values among those). A ``COUNT`` is never NULL, so a comparison of one with a literal is TRUE or FALSE.

**Implied bound.** In a conjunction, ``COUNT(*) >= n`` says nothing new next to a conjunct that puts a lower
bound of at least ``n`` on any ``COUNT`` (``COUNT(DISTINCT article_id) > 1`` gives ``COUNT(*) >= 2``), and
``COUNT(x) >= n`` nothing next to such a bound on ``COUNT(DISTINCT x)`` of the same ``x``. The bounding
conjunct is TRUE only when the implied one is, so the conjunction keeps the same groups without it.
A lower bound is read from ``>= m`` (``m``), ``> m`` (``m + 1``) and ``= m`` (``m``), with the literal on
either side; only ``>=`` and ``>`` tests are ever dropped.

**Counted key.** Under a plain ``GROUP BY`` every row of a group has the group's key ``k`` (as the grouping
compares it), so in the group whose ``k`` is NULL ``COUNT(k)`` and ``COUNT(DISTINCT k)`` are 0. A conjunct
that bounds either from below by 1 or more therefore fails for that group, and ``k IS NOT NULL`` may be added
to the conjunction: it removes no group the bound keeps. Written out, the test moves to ``WHERE``
(``having_rules``), where ``GROUP BY email HAVING COUNT(email) > 1`` meets
``WHERE email IS NOT NULL GROUP BY email HAVING COUNT(*) > 1``. Needs a ``GROUP BY`` of plain keys (no
``ROLLUP``, ``CUBE`` or grouping sets, which NULL a key in their super-groups), a key that is a column, and
no select alias that takes the key's name.

Counts with ``FILTER``, ``OVER`` or other modifiers are not read: they do not count the group's rows.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import extended_grouping

_FLIP = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.LT: exp.GT, exp.EQ: exp.EQ}


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    node = _unwrap(node)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _plain_count(node: exp.Expression) -> tuple[str, str] | None:
    """``("*", "")``, ``("all", arg)`` or ``("distinct", args)`` for an unmodified ``COUNT``, else ``None``."""

    node = _unwrap(node)
    if type(node) is not exp.Count or any(value for key, value in node.args.items() if key not in ("this", "big_int")):
        return None
    arg = node.this
    if isinstance(arg, exp.Star):
        return "*", ""
    if type(arg) is exp.Distinct:
        if arg.args.get("on") or not arg.expressions:
            return None
        return "distinct", ", ".join(e.sql().lower() for e in arg.expressions)
    if not isinstance(arg, exp.Expression) or any(isinstance(n, (exp.Select, exp.Subquery, exp.Window, exp.AggFunc)) for n in arg.walk()):
        return None
    return "all", _unwrap(arg).sql().lower()


def _natural(node: exp.Expression) -> int | None:
    node = _unwrap(node)
    if type(node) is exp.Literal and not node.is_string and node.this.isdigit():
        return int(node.this)
    return None


def _bound(test: exp.Expression) -> tuple[tuple[str, str], int, bool, exp.Count] | None:
    """``(count, lower bound, droppable, node)`` for ``COUNT(..) >= m``, ``> m`` or ``= m`` (either order)."""

    kind = type(test)
    if kind not in _FLIP:
        return None
    count, number, node = _plain_count(test.this), _natural(test.expression), test.this
    if count is None or number is None:
        count, number, node, kind = _plain_count(test.expression), _natural(test.this), test.expression, _FLIP[kind]
    if count is None or number is None or kind in (exp.LTE, exp.LT):
        return None
    return count, number + 1 if kind is exp.GT else number, kind is not exp.EQ, _unwrap(node)


def _implies(strong: tuple[str, str], weak: tuple[str, str]) -> bool:
    """Whether a lower bound on the ``strong`` count is one on the ``weak`` count (``weak >= strong`` always)."""

    if weak[0] == "*" or strong == weak:
        return True
    return weak[0] == "all" and strong == ("distinct", weak[1])


def _is_not_null(test: exp.Expression, key_sql: str) -> bool:
    test = _unwrap(test)
    if type(test) is not exp.Not:
        return False
    inner = _unwrap(test.this)
    return type(inner) is exp.Is and type(inner.expression) is exp.Null and _unwrap(inner.this).sql().lower() == key_sql


def rewrite_count_tests(select: exp.Select) -> exp.Expression | None:
    """``select`` (in place) with implied ``COUNT`` bounds dropped and counted keys tested, else ``None``."""

    having = select.args.get("having")
    if having is None or any(select.args.get(k) for k in ("qualify", "windows")):
        return None
    tests = _conjuncts(having.this)
    bounds = [_bound(t) for t in tests]
    alive = set(range(len(tests)))
    for index, found in enumerate(bounds):
        if found is None or not found[2]:
            continue
        count, low = found[0], found[1]
        for other_index, other in enumerate(bounds):
            if other is None or other_index == index or other_index not in alive or not _implies(other[0], count):
                continue
            # of two equal droppable tests the first stays; otherwise the implication runs one way only
            twin = other[0] == count and other[1] == low and other[2]
            if other[1] >= low and (not twin or other_index < index):
                alive.discard(index)
                break
    changed = len(alive) != len(tests)
    kept = [tests[i] for i in sorted(alive)]

    group = select.args.get("group")
    added: list[exp.Expression] = []
    if group is not None and group.expressions and not extended_grouping(group) and not group.args.get("all"):
        keys = {k.sql().lower(): k for k in group.expressions if type(k) is exp.Column and not isinstance(k.this, exp.Star)}
        shadowed = {
            item.alias.lower() for item in select.expressions
            if isinstance(item, exp.Alias) and not (isinstance(item.this, exp.Column) and item.this.name.lower() == item.alias.lower())
        }
        where = select.args.get("where")
        present = kept + (_conjuncts(where.this) if where is not None else [])
        for index, found in enumerate(bounds):
            if found is None or index not in alive or found[1] < 1 or found[0][0] == "*":
                continue
            key = keys.get(found[0][1])
            if key is None or key.name.lower() in shadowed:
                continue
            if any(_is_not_null(p, found[0][1]) for p in present + added):
                continue
            added.append(exp.Not(this=exp.Is(this=key.copy(), expression=exp.Null())))
    if not changed and not added:
        return None
    having.set("this", exp.and_(*[t.copy() for t in kept], *added))
    return select
