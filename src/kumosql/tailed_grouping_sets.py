"""``GROUP BY ROLLUP / CUBE / GROUPING SETS`` with ``ORDER BY`` / ``LIMIT`` / ``OFFSET``: sort and cut one level up.

``grouping_sets.grouping_sets_to_union`` reads ``GROUP BY GROUPING SETS (..)`` as the ``UNION ALL`` of one grouped
select per set, the only form in which the prover reads grouping sets. It cannot do that to a select that also
sorts or cuts its rows (the tail applies to all the sets' rows together, not to one branch), so

    SELECT a, b, SUM(x) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, s DESC LIMIT 100

was declined outright, even against its own text. ``isolate_tailed_grouping_sets`` moves the tail onto a select
around the grouped one:

    SELECT kqt.a AS a, kqt.b AS b, kqt.s AS s
    FROM (SELECT a, b, SUM(x) AS s FROM t GROUP BY ROLLUP (a, b)) AS kqt
    ORDER BY kqt.a, kqt.s DESC LIMIT 100

The derived table has the grouped query's rows, one for one; the outer select sorts them by the same values,
keeps the same positions, and outputs the same columns under the same names. So both return the same rows
(and where sort keys tie, the same choices are open to both).

Each ``ORDER BY`` key is read as what it is in the grouped query:

* a literal stays as written (a constant; ``algebraic_equivalence._resolve_ordinals`` has already spelled out
  positions, and the outer select list has the same positions anyway);
* a bare name that exactly one output column has is that output column: ``kqt.name``. ``ORDER BY`` resolves a
  bare name to an output column before an input column;
* an expression the select list already holds (``ORDER BY SUM(x)`` beside ``SUM(x) AS s``) is that output column;
* any other key is computed over the groups, like a select-list expression. It is added to the grouped select
  as a hidden column ``kq_tail<i>`` that the outer select sorts by and does not output. Inside such an
  expression a bare name is an input column in PostgreSQL but may be an output alias elsewhere, so a key that
  uses a name some output gives to anything but that very column is declined.

Declined (the select is left as it was): no tail or no grouping sets, ``DISTINCT``, ``QUALIFY``, a window (it
would read every set's rows), a ``WITH`` on the select or any other clause, a star or an unnamed or
repeated output name (the outer select could not name it), and a source already called ``kqt``.
"""

from __future__ import annotations

from sqlglot import exp

_TAIL = ("order", "limit", "offset")
_PLAIN = frozenset({"expressions", "from_", "from", "joins", "where", "group", "having", *_TAIL})
_ALIAS = "kqt"
_HIDDEN = "kq_tail"


def _has_grouping_sets(select: exp.Select) -> bool:
    group = select.args.get("group")
    if group is None:
        return False
    if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
        return True
    return any(isinstance(e, (exp.GroupingSets, exp.Rollup, exp.Cube)) for e in group.expressions)


def _output_identifier(item: exp.Expression) -> exp.Identifier | None:
    """The identifier that names a select-list item, None for a star or an unnamed expression."""

    if isinstance(item, exp.Alias):
        alias = item.args.get("alias")
        return alias if isinstance(alias, exp.Identifier) and alias.name else None
    if isinstance(item, exp.Column) and isinstance(item.this, exp.Identifier):
        return item.this
    return None


def _isolated(select: exp.Select) -> exp.Select | None:
    if not any(select.args.get(k) for k in _TAIL) or not _has_grouping_sets(select):
        return None
    if any(value for key, value in select.args.items() if key not in _PLAIN):
        return None  # DISTINCT, QUALIFY, WITH, named windows, locks, hints, ...
    if select.find(exp.Window) is not None:
        return None
    if any(isinstance(n, (exp.Table, exp.TableAlias)) and n.name.lower() == _ALIAS for n in select.walk()):
        return None
    if any(c.table.lower() == _ALIAS for c in select.find_all(exp.Column)):
        return None  # a column of an enclosing query's source of that name
    order = select.args.get("order")
    if order is not None and (type(order) is not exp.Order or any(v for k, v in order.args.items() if k != "expressions")):
        return None
    if order is not None and any(not isinstance(o, exp.Ordered) for o in order.expressions):
        return None
    identifiers = [_output_identifier(item) for item in select.expressions]
    if not identifiers or any(i is None for i in identifiers):
        return None
    names = [i.name.lower() for i in identifiers]
    if len(set(names)) != len(names) or any(n.startswith(_HIDDEN) for n in names):
        return None
    values = [item.this if isinstance(item, exp.Alias) else item for item in select.expressions]
    # a name an output gives to something other than the column of that name: unsafe inside a key expression
    renamed = {
        name for name, value in zip(names, values)
        if not (isinstance(value, exp.Column) and isinstance(value.this, exp.Identifier) and value.name.lower() == name)
    }

    def output(position: int) -> exp.Column:
        return exp.Column(this=identifiers[position].copy(), table=exp.to_identifier(_ALIAS))

    inner = select.copy()
    tail = {key: inner.args.get(key) for key in _TAIL}
    for key in _TAIL:
        inner.set(key, None)
    hidden: list[exp.Expression] = []
    for item in tail["order"].expressions if tail["order"] is not None else []:
        key = item.this
        if isinstance(key, exp.Literal):
            continue
        if isinstance(key, exp.Column) and not key.table and isinstance(key.this, exp.Identifier) and key.name.lower() in names:
            item.set("this", output(names.index(key.name.lower())))
            continue
        if any(isinstance(n, (exp.Query, exp.Subquery, exp.Star)) for n in key.walk()):
            return None
        if any(not c.table and c.name.lower() in renamed for c in key.find_all(exp.Column)):
            return None
        text = key.sql()
        match = next((i for i, value in enumerate(values) if value.sql() == text), None)
        if match is not None:
            item.set("this", output(match))
            continue
        name = f"{_HIDDEN}{len(hidden)}"
        hidden.append(exp.alias_(key.copy(), name))
        item.set("this", exp.column(name, table=_ALIAS))
    inner.set("expressions", list(inner.expressions) + hidden)
    outer = exp.Select(expressions=[exp.Alias(this=output(i), alias=identifiers[i].copy()) for i in range(len(identifiers))])
    outer = outer.from_(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(_ALIAS))), copy=False)
    for key in _TAIL:
        outer.set(key, tail[key])
    return outer


def isolate_tailed_grouping_sets(tree: exp.Expression) -> exp.Expression:
    """Every select of ``tree`` that groups by grouping sets and has a row tail, read as that tail over the grouped select."""

    for select in list(tree.find_all(exp.Select))[::-1]:
        replacement = _isolated(select)
        if replacement is None:
            continue
        if select is tree:
            tree = replacement
        else:
            select.replace(replacement)
    return tree
