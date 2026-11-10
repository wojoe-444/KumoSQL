"""The distinct values of a column that are among its own table's groups are those groups.

``SELECT DISTINCT x FROM t WHERE x IN (SELECT x FROM t GROUP BY x HAVING h)`` is
``SELECT x FROM t GROUP BY x HAVING h AND x IS NOT NULL``.

The subquery returns the key of every group of ``t`` (by ``x``) that passes ``h``. ``x IN (..)`` is TRUE for a
row exactly when its ``x`` is not NULL and equals one of those keys, so the outer query returns the distinct
non-NULL keys that pass ``h`` and occur in ``t``. Every key occurs in ``t``: a group is made of rows of ``t``
with that ``x``. That leaves the passing groups with a non-NULL key, once each, which is the grouped query with
``x IS NOT NULL`` added. The NULL test matters: ``HAVING COUNT(*) > 1`` can keep the group of NULLs, which
``IN`` never matches, while ``HAVING COUNT(x) > 1`` cannot.

The subquery may filter its rows first (``WHERE``): its groups are still made of rows of ``t``. The outer
query may add ``x IS NOT NULL``, which the membership test already implies. Anything else is left alone:

* outer select: ``SELECT DISTINCT`` of the one column, one base table, no join, and nothing but ``WHERE``;
* subquery: the same base table, the same one column selected and as the only grouping key (no ``ROLLUP``),
  optional ``WHERE`` and ``HAVING``, no ``LIMIT``, window or set operation, and no column that names the outer
  table's alias (a correlated subquery is a different test per row).

Without ``DISTINCT`` the outer query repeats a value once per row that has it, so it is not rewritten; an
aggregate that ignores repeats (``MAX``, ``COUNT(DISTINCT ..)``) reaches this rule through the set-of-values
reduction (``set_aggregates``), which compares the ``DISTINCT`` queries.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import extended_grouping, plain_distinct, same_table, select_sources, visible_ctes

_OUTER_ARGS = frozenset({"expressions", "from_", "from", "where", "distinct"})
_INNER_ARGS = frozenset({"expressions", "from_", "from", "where", "group", "having", "distinct"})
_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    node = _unwrap(node)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _plain_table(source: exp.Expression) -> bool:
    if type(source) is not exp.Table or not isinstance(source.this, exp.Identifier):
        return False
    if any(value for key, value in source.args.items() if key not in _TABLE_ARGS):
        return False
    alias = source.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return False
    return bool(source.args.get("db")) or source.name.lower() not in visible_ctes(source)


def _only_source(select: exp.Select) -> exp.Table | None:
    sources = select_sources(select)
    return sources[0] if len(sources) == 1 and _plain_table(sources[0]) else None


def _column_of(node: exp.Expression, table: exp.Table) -> exp.Column | None:
    """``node`` when it is a plain column, bare or qualified by ``table``'s name."""

    node = _unwrap(node)
    if type(node) is not exp.Column or isinstance(node.this, exp.Star) or node.args.get("db") or node.args.get("catalog"):
        return None
    return node if node.table.lower() in ("", table.alias_or_name.lower()) else None


def _not_null_test(test: exp.Expression, table: exp.Table, name: str) -> bool:
    test = _unwrap(test)
    if type(test) is not exp.Not:
        return False
    inner = _unwrap(test.this)
    if type(inner) is not exp.Is or type(inner.expression) is not exp.Null:
        return False
    column = _column_of(inner.this, table)
    return column is not None and column.name.lower() == name


def _rewrite(outer: exp.Select, dialect: str) -> exp.Select | None:
    if any(value for key, value in outer.args.items() if key not in _OUTER_ARGS) or not plain_distinct(outer):
        return None
    table = _only_source(outer)
    where = outer.args.get("where")
    if table is None or where is None or len(outer.expressions) != 1:
        return None
    picked = _column_of(outer.expressions[0].unalias(), table)
    if picked is None:
        return None
    name = picked.name.lower()
    membership = None
    for test in _conjuncts(where.this):
        if _not_null_test(test, table, name):
            continue
        if type(test) is not exp.In or membership is not None:
            return None
        membership = test
    if membership is None or any(value for key, value in membership.args.items() if key not in ("this", "query")):
        return None
    value = _column_of(membership.this, table)
    query = membership.args.get("query")
    inner = query.this if type(query) is exp.Subquery and not query.alias else None
    if value is None or value.name.lower() != name or type(inner) is not exp.Select:
        return None
    output = outer.expressions[0].alias_or_name
    lifted = _lifted_groups(inner, table, name, output, dialect)
    if lifted is not None:
        return lifted
    if any(v for key, v in inner.args.items() if key not in _INNER_ARGS):
        return None
    if inner.args.get("distinct") is not None and not plain_distinct(inner):
        return None
    source = _only_source(inner)
    group = inner.args.get("group")
    if source is None or not same_table(source, table, dialect) or group is None:
        return None
    if extended_grouping(group) or group.args.get("all") or len(group.expressions) != 1 or len(inner.expressions) != 1:
        return None
    key = _column_of(group.expressions[0], source)
    shown = _column_of(inner.expressions[0].unalias(), source)
    if key is None or shown is None or key.name.lower() != name or shown.name.lower() != name:
        return None
    if any(isinstance(node, exp.Window) for node in inner.walk()):
        return None
    # The subquery must be the same test for every outer row: nothing in it may name the outer table.
    # A bare name binds to the subquery's own table first (it has every column the outer one has); a name
    # qualified by the outer alias reaches the outer row unless the subquery's table goes by that alias too.
    outer_name = table.alias_or_name.lower()
    if source.alias_or_name.lower() != outer_name and any(c.table.lower() == outer_name for c in inner.find_all(exp.Column)):
        return None
    result = inner.copy()
    result.set("distinct", None)
    shown = result.expressions[0].unalias()
    result.set("expressions", [shown if output.lower() == name else exp.alias_(shown, output)])
    not_null = exp.Not(this=exp.Is(this=shown.copy(), expression=exp.Null()))
    having = result.args.get("having")
    result.set("having", exp.Having(this=exp.and_(exp.Paren(this=having.this), not_null) if having is not None else not_null))
    return result


def _lifted_groups(inner: exp.Select, table: exp.Table, name: str, output: str, dialect: str) -> exp.Select | None:
    """The same reading when the groups are already a derived table (``_grouped_in_to_derived``'s form):
    ``x IN (SELECT g.k FROM (SELECT x AS k, agg AS a FROM t GROUP BY x) AS g WHERE f(g.a))`` keeps the rows of
    ``g`` that pass ``f``, one per group, so the outer ``SELECT DISTINCT x`` is that select with
    ``g.k IS NOT NULL``."""

    if any(v for key, v in inner.args.items() if key not in ("expressions", "from_", "from", "where")):
        return None
    sources = select_sources(inner)
    if len(sources) != 1 or type(sources[0]) is not exp.Subquery or not sources[0].alias or type(sources[0].this) is not exp.Select:
        return None
    derived, groups = sources[0], sources[0].this
    if any(v for key, v in derived.args.items() if key not in ("this", "alias")) or derived.args["alias"].args.get("columns"):
        return None
    if any(v for key, v in groups.args.items() if key not in ("expressions", "from_", "from", "where", "group")):
        return None
    source, group = _only_source(groups), groups.args.get("group")
    if source is None or not same_table(source, table, dialect) or group is None:
        return None
    if extended_grouping(group) or group.args.get("all") or len(group.expressions) != 1:
        return None
    key = _column_of(group.expressions[0], source)
    if key is None or key.name.lower() != name:
        return None
    names = [item.alias_or_name.lower() for item in groups.expressions]
    if "" in names or len(set(names)) != len(names):
        return None
    key_names = []
    for item in groups.expressions:
        shown = item.unalias()
        if any(isinstance(n, (exp.Window, exp.Select, exp.Subquery)) or (isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count)) for n in shown.walk()):
            return None
        column = _column_of(shown, source)
        if column is not None and column.name.lower() == name:
            key_names.append(item.alias_or_name.lower())
        elif not any(isinstance(n, exp.AggFunc) for n in shown.walk()):
            return None  # neither the key nor an aggregate of the group
    alias = derived.alias.lower()
    picked = inner.expressions[0].unalias() if len(inner.expressions) == 1 else None
    if type(picked) is not exp.Column or picked.table.lower() != alias or picked.name.lower() not in key_names:
        return None
    where = inner.args.get("where")
    if where is not None:
        if any(isinstance(n, (exp.Select, exp.Subquery, exp.Window, exp.AggFunc)) for n in where.walk()):
            return None
        if any(c.table.lower() != alias or c.name.lower() not in names for c in where.find_all(exp.Column)):
            return None
    outer_name = table.alias_or_name.lower()
    if source.alias_or_name.lower() != outer_name and any(c.table.lower() == outer_name for c in groups.find_all(exp.Column)):
        return None
    result = inner.copy()
    shown = result.expressions[0].unalias()
    result.set("expressions", [exp.alias_(shown, output)])
    not_null = exp.Not(this=exp.Is(this=shown.copy(), expression=exp.Null()))
    kept = result.args.get("where")
    result.set("where", exp.Where(this=exp.and_(exp.Paren(this=kept.this), not_null) if kept is not None else not_null))
    return result


def read_grouped_self_membership(tree: exp.Expression, dialect: str = "bigquery") -> exp.Expression:
    """``tree`` with every such ``SELECT DISTINCT`` read as the grouped query (a pre-pass of ``normalize``)."""

    for select in list(tree.find_all(exp.Select)):
        if isinstance(select.parent, exp.SetOperation) and select.parent.args.get("by_name"):
            continue
        rewritten = _rewrite(select, dialect)
        if rewritten is None:
            continue
        if select is tree:
            tree = rewritten
        else:
            select.replace(rewritten)
    return tree
