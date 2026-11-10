"""Read Calcite's eager ``SUM`` through a join back into the plain ``SUM``.

Calcite's ``AggregateReduceFunctionsRule`` writes ``SUM(x)`` as
``CASE WHEN COUNT(x) = 0 THEN NULL ELSE SUM0(x) END`` (``SUM0`` is ``COALESCE(SUM(x), 0)``), and
``AggregateJoinTransposeRule`` then pushes both aggregates below a join: each side is grouped by
the join key, the ``EMP`` side computes ``COALESCE(SUM(sal), 0)`` and ``COUNT(*)``, the other side
``COUNT(*)``, and the outer aggregate multiplies one side's sum by the other side's count. Three
identities read that back:

* ``COALESCE(g.s, 0)``, with ``g`` a grouped derived table (plain ``GROUP BY``, so every group has
  a row) that no outer join pads and ``s`` its ``SUM(x)``, is ``g.s0`` for ``SUM(COALESCE(x, 0))``
  (or ``g.s`` itself when ``x`` is never NULL): a non-empty group sums its non-NULL values or 0.
* A global ``SUM(g.c)`` of such a table's ``COUNT(*)`` adds counts of at least 1 each, so it is NULL
  or at least 1, never 0: it is ``NULLIF(SUM(g.c), 0)``.
  Both only make ``eager_aggregation`` (``unnest_grouped_source``, ``flatten_grouped_join``) accept
  the shape, so they are applied only when one of those then flattens the select.
* ``CASE WHEN n = 0 THEN NULL ELSE COALESCE(s, 0) END`` over one row of an aggregate table, with
  ``n`` its ``COUNT(*)`` and ``s`` its ``SUM(y)`` of the same rows and ``y`` never NULL (or ``n``
  ``COUNT(y)`` of the same ``y``), is ``s``: no counted row means ``s`` is NULL, and a counted row
  means ``s`` has a non-NULL term. ``n`` may be read as ``COALESCE(n, 0)`` or
  ``COALESCE(NULLIF(n, 0), 0)``, both ``n`` for a count.

With ``y`` nullable and ``COUNT(*)`` the last identity does not hold (rows whose ``y`` are all NULL
give 0, not NULL), and nothing here rewrites it. See ``read_back_eager_sums``.
"""

from __future__ import annotations

import itertools

from sqlglot import exp

from .ast_utils import extended_grouping

_counter = itertools.count()
_EXTRAS = ("distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with", "laterals", "pivots")


def _from(select: exp.Select):
    return select.args.get("from_") or select.args.get("from")


def _unparen(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _zero(node: exp.Expression | None) -> bool:
    node = _unparen(node)
    return isinstance(node, exp.Literal) and not node.is_string and node.this == "0"


def _inner_sources(select: exp.Select) -> list[exp.Expression] | None:
    """The FROM and JOIN sources when every join is inner or cross (no source is NULL-padded)."""

    from_ = _from(select)
    if from_ is None:
        return None
    items = [from_.this]
    for join in select.args.get("joins") or []:
        kind = (join.args.get("kind") or "").upper()
        if join.args.get("side") or kind not in ("", "INNER", "CROSS") or join.args.get("using") or join.args.get("method"):
            return None
        items.append(join.this)
    return items


def _aliases(select: exp.Select, items: list[exp.Expression]) -> dict[str, exp.Expression] | None:
    names = [(i.alias_or_name or "").lower() for i in items]
    if "" in names or len(set(names)) != len(names):
        return None
    return dict(zip(names, items))


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select and node.find_ancestor(exp.Window) is None


def _plain_aggregate(node: exp.Expression, select: exp.Select, kind: type) -> bool:
    """A non-DISTINCT ``kind`` aggregate of ``select`` itself over plain row values."""

    if type(node) is not kind or not _own(node, select) or node.args.get("distinct") or isinstance(node.this, exp.Distinct):
        return False
    if node.args.get("expressions") or isinstance(node.parent, exp.Filter):
        return False  # COUNT(a, b), or an aggregate over the rows a FILTER keeps
    if node.this is None or isinstance(node.this, exp.Star):
        return kind is exp.Count
    return not any(isinstance(n, (exp.AggFunc, exp.Window, exp.Select, exp.Subquery, exp.Exists)) for n in node.this.walk())


def _items(select: exp.Select) -> dict[str, exp.Expression] | None:
    """Output name -> value of a select's items (``None`` for a star or a repeated or missing name)."""

    items = {}
    for item in select.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        name = (item.alias_or_name or "").lower()
        if not name or name in items:
            return None
        items[name] = item.this if isinstance(item, exp.Alias) else item
    return items


def _derived(source: exp.Expression, *, grouped: bool) -> exp.Select | None:
    """The select of a derived table whose columns can be read and extended by name."""

    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    alias = source.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in _EXTRAS) or any(inner.find_all(exp.Window)):
        return None
    group = inner.args.get("group")
    if extended_grouping(group):
        return None  # a grand-total row exists even over no input rows
    if grouped and (group is None or not group.expressions):
        return None
    if _items(inner) is None:
        return None
    return inner


def _never_null(value: exp.Expression, select: exp.Select, not_null: dict[str, set[str]]) -> bool:
    """Whether ``value``, read on an input row of ``select``, is never NULL (``not_null`` keys lowercased)."""

    value = _unparen(value)
    if isinstance(value, exp.Literal):
        return True
    if isinstance(value, exp.Coalesce):
        return any(_never_null(v, select, not_null) for v in [value.this, *value.expressions])
    if isinstance(value, (exp.Add, exp.Sub, exp.Mul)):
        return _never_null(value.this, select, not_null) and _never_null(value.expression, select, not_null)
    if isinstance(value, exp.Neg):
        return _never_null(value.this, select, not_null)
    if type(value) is exp.Cast:
        return _never_null(value.this, select, not_null)
    if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star):
        items = _inner_sources(select)  # an outer join could pad the column with NULLs
        if items is None:
            return False
        tables = [i for i in items if isinstance(i, exp.Table)]
        if value.table:
            matches = [t for t in tables if (t.alias_or_name or "").lower() == value.table.lower()]
            if any((i.alias_or_name or "").lower() == value.table.lower() for i in items if not isinstance(i, exp.Table)):
                return False
        else:
            matches = tables if len(items) == 1 else []
        if len(matches) != 1 or matches[0].args.get("db") or matches[0].args.get("catalog"):
            return False
        return value.name.lower() in not_null.get(matches[0].name.lower(), ())
    return False


# --- making the eager form flatten -----------------------------------------------------------------


def _zero_when_empty(call: exp.Expression) -> bool:
    parent = call.parent
    if isinstance(parent, exp.Nullif):
        return parent.this is call and _zero(parent.expression)
    return isinstance(parent, exp.Coalesce) and parent.this is call and len(parent.expressions) == 1 and _zero(parent.expressions[0])


def _fresh(base: str, taken: set[str]) -> str:
    name = f"{base}{next(_counter)}"
    while name.lower() in taken:
        name = f"{base}{next(_counter)}"
    return name


def _unnest_guarded(select: exp.Select, not_null: dict[str, set[str]]) -> exp.Expression | None:
    from .eager_aggregation import flatten_grouped_join, unnest_grouped_source

    if _inner_sources(select) is None:
        return None
    copy = select.copy()
    items = _inner_sources(copy)
    by_alias = _aliases(copy, items)
    if by_alias is None:
        return None
    grouped = {alias: inner for alias, source in by_alias.items() if (inner := _derived(source, grouped=True)) is not None}
    if not grouped:
        return None
    if _has_star(copy):
        return None  # a star would show a column added to a grouped table
    changed = False

    def column_of(node: exp.Expression) -> tuple[str, str] | None:
        node = _unparen(node)
        if isinstance(node, exp.Column) and node.table.lower() in grouped and node.find_ancestor(exp.Select) is copy:
            return node.table.lower(), node.name.lower()
        return None

    # COALESCE(g.s, 0) of a group's SUM(x): SUM(COALESCE(x, 0)), or g.s when x is never NULL
    for coalesce in list(copy.find_all(exp.Coalesce)):
        if coalesce.find_ancestor(exp.Select) is not copy or len(coalesce.expressions) != 1 or not _zero(coalesce.expressions[0]):
            continue
        found = column_of(coalesce.this)
        if found is None:
            continue
        inner = grouped[found[0]]
        values = _items(inner)
        total = values.get(found[1])
        if total is None or not _plain_aggregate(total, inner, exp.Sum):
            continue
        if _never_null(total.this, inner, not_null):
            replacement = exp.column(found[1], table=found[0])
        else:
            summed = exp.Sum(this=exp.Coalesce(this=total.this.copy(), expressions=[exp.Literal.number(0)]))
            name = next((n for n, v in values.items() if v.sql() == summed.sql()), None)
            if name is None:
                name = _fresh("kumosql_sum0_", set(values))
                inner.set("expressions", [*inner.expressions, exp.alias_(summed, name)])
            replacement = exp.column(name, table=found[0])
        coalesce.replace(replacement)
        changed = True

    # a global SUM of a group's COUNT(*) is never 0
    if not copy.args.get("group"):
        for call in list(copy.find_all(exp.Sum)):
            if not _own(call, copy) or call.args.get("distinct") or _zero_when_empty(call):
                continue
            found = column_of(call.this)
            if found is None:
                continue
            inner = grouped[found[0]]
            count = _items(inner).get(found[1])
            if count is None or not _plain_aggregate(count, inner, exp.Count) or not (count.this is None or isinstance(count.this, exp.Star)):
                continue
            call.replace(exp.Nullif(this=call.copy(), expression=exp.Literal.number(0)))
            changed = True
    if not changed:
        return None
    return unnest_grouped_source(copy) or flatten_grouped_join(copy)


# --- CASE WHEN n = 0 THEN NULL ELSE COALESCE(s, 0) END --------------------------------------------


def _fold_count_guarded_sum(select: exp.Select, not_null: dict[str, set[str]]) -> exp.Expression | None:
    """Fold the CASE over the select's own aggregates, or over the aggregates of a derived table it reads."""

    group = select.args.get("group")
    if not extended_grouping(group) and not select.find(exp.Window):
        rewritten = _fold_cases(select, select, None, {}, not_null)
        if rewritten is not None:
            return rewritten
    items = _inner_sources(select)
    if items is None:
        return None
    by_alias = _aliases(select, items)
    if by_alias is None:
        return None
    for alias, source in by_alias.items():
        inner = _derived(source, grouped=False)
        if inner is None or _has_star(select):
            continue  # a star would show a column added to the derived table
        values = _items(inner)
        if not any(isinstance(n, exp.AggFunc) and _own(n, inner) for n in inner.find_all(exp.AggFunc)):
            continue
        rewritten = _fold_cases(select, inner, alias, values, not_null)
        if rewritten is not None:
            return rewritten
    return None


def _fold_cases(select, inner, alias, values, not_null) -> exp.Expression | None:
    """``inner`` holds the aggregates: ``select`` itself (``alias`` None) or its derived source ``alias``."""

    def deref(node: exp.Expression | None) -> exp.Expression | None:
        node = _unparen(node)
        if alias is not None and isinstance(node, exp.Column) and node.table.lower() == alias and node.find_ancestor(exp.Select) is select:
            value = values.get(node.name.lower())
            return _unparen(value) if value is not None else None
        return node

    def count_of(node: exp.Expression | None) -> exp.Expression | None:
        """The table's own count that ``node`` always equals (a count is never NULL)."""

        node = deref(node)
        if node is not None and _plain_aggregate(node, inner, exp.Count):
            return node
        if isinstance(node, exp.Coalesce) and len(node.expressions) == 1 and _zero(node.expressions[0]):
            guarded = deref(node.this)
            if isinstance(guarded, exp.Nullif) and _zero(guarded.expression):
                return count_of(guarded.this)  # COALESCE(NULLIF(n, 0), 0) is n
            return count_of(node.this)
        return None

    def zero_test(node: exp.Expression) -> exp.Expression | None:
        node = _unparen(node)
        if not isinstance(node, exp.EQ):
            return None
        for side, other in ((node.this, node.expression), (node.expression, node.this)):
            if _zero(other):
                found = count_of(side)
                if found is not None:
                    return found
        return None

    def sum_of(node: exp.Expression | None) -> tuple[exp.Expression, str | None] | None:
        """``(SUM node, column holding it)`` when ``node`` is ``s`` or ``COALESCE(s, 0)``."""

        plain = _unparen(node)
        if isinstance(plain, exp.Coalesce) and len(plain.expressions) == 1 and _zero(plain.expressions[0]):
            plain = _unparen(plain.this)
        column = plain.name.lower() if alias is not None and isinstance(plain, exp.Column) and plain.table.lower() == alias else None
        value = deref(plain)
        if isinstance(value, exp.Coalesce) and len(value.expressions) == 1 and _zero(value.expressions[0]):
            column, value = None, _unparen(value.this)
        if value is not None and _plain_aggregate(value, inner, exp.Sum):
            return value, column
        return None

    for case in select.find_all(exp.Case):
        if case.find_ancestor(exp.Select) is not select or case.this is not None:
            continue
        arms = case.args.get("ifs") or []
        if len(arms) != 1:
            continue
        then = _unparen(arms[0].args.get("true"))
        if type(then) is exp.Cast:
            if not _integer_type(then.args.get("to")):
                continue  # CAST(NULL AS DOUBLE) widens the CASE; the bare SUM would stay integral
            then = _unparen(then.this)
        if not isinstance(then, exp.Null):
            continue
        count = zero_test(arms[0].this)
        total = sum_of(case.args.get("default"))
        if count is None or total is None:
            continue
        total, column = total
        argument = total.this
        if count.this is None or isinstance(count.this, exp.Star):
            if not _never_null(argument, inner, not_null):
                continue  # rows whose values are all NULL: SUM is NULL, the CASE 0
        elif _unparen(count.this).sql() != _unparen(argument).sql() or not _plain_value(argument):
            continue  # COUNT(x) and SUM(x) must read the very same value of each row
        # one CASE per call: the next pass sees the rewritten select
        copy = select.copy()
        target = _locate(copy, select, case)
        if inner is select:
            target.replace(_locate(copy, select, total).copy())
            return copy
        if column is None:
            target_inner = _locate(copy, select, inner)
            current = _items(target_inner)
            column = next((n for n, v in current.items() if v.sql() == total.sql()), None)
            if column is None:
                column = _fresh("kumosql_sum_", set(current))
                target_inner.set("expressions", [*target_inner.expressions, exp.alias_(total.copy(), column)])
        target.replace(exp.column(column, table=alias))
        return copy
    return None


_INTEGER_TYPES = {exp.DataType.Type.TINYINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.INT, exp.DataType.Type.BIGINT}
_PLAIN_VALUE = (exp.Column, exp.Identifier, exp.Literal, exp.Paren, exp.Neg, exp.Cast, exp.Coalesce, exp.Add, exp.Sub, exp.Mul, exp.DataType)


def _integer_type(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.DataType) and not node.args.get("expressions") and node.this in _INTEGER_TYPES


def _plain_value(node: exp.Expression) -> bool:
    """Built only from columns, literals and arithmetic: evaluating it twice gives the same value."""

    return all(isinstance(n, _PLAIN_VALUE) for n in node.walk())


def _has_star(select: exp.Select) -> bool:
    return any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) and _own(s, select) for s in select.find_all(exp.Star))


def _locate(copy: exp.Expression, original: exp.Expression, node: exp.Expression) -> exp.Expression:
    """The node of ``copy`` at the place ``node`` has in ``original``."""

    path = []
    while node is not original:
        path.append((node.arg_key, node.index))
        node = node.parent
    target = copy
    for key, index in reversed(path):
        child = target.args[key]
        target = child[index] if index is not None else child
    return target


def read_back_eager_sums(select: exp.Select, not_null: dict[str, frozenset[str]] | None) -> exp.Expression | None:
    """One rewrite of the identities above, or ``None``."""

    if not isinstance(select, exp.Select):
        return None
    not_null = {table.lower(): {c.lower() for c in columns} for table, columns in (not_null or {}).items()}
    return _fold_count_guarded_sum(select, not_null) or _unnest_guarded(select, not_null)
