"""A WHERE or HAVING that NULL facts make impossible becomes FALSE (the select then holds no row).

Two facts about a column, read per select from its own WHERE and from the derived tables it reads:

* NULL on every row: a top-level WHERE conjunct ``c IS NULL`` (WHERE runs after the joins, so every
  row it keeps has ``c`` NULL, padded or not), or ``x.c`` where the derived table ``x`` outputs ``c``
  as a column that is NULL on every one of its rows (an outer join only pads with more NULLs).
* never NULL: a top-level WHERE conjunct that cannot be TRUE when ``c`` is NULL (a comparison,
  ``BETWEEN``, ``IN``, ``LIKE`` or ``IS NOT NULL`` on ``c`` or NULL-propagating arithmetic on it), or
  ``x.c`` where ``x`` outputs ``c`` never NULL and no outer join of this select can pad ``x``.

A derived table outputs a NULL-everywhere column when it projects a NULL-everywhere column, NULL, or
MIN/MAX/SUM/AVG of a NULL-everywhere expression (all NULL in every group, an empty one included). It
outputs a never-NULL column when it projects a never-NULL column (not under ROLLUP/CUBE/GROUPING SETS,
which pad keys with NULL), ``COUNT(..)``, or MIN/MAX/SUM/AVG of a never-NULL column under a plain,
non-empty ``GROUP BY`` (each group holds a row, all of whose values are numbers; a global aggregate
over no row is NULL, and so is the grand-total row of a ROLLUP over no row).

A top-level WHERE conjunct that cannot be TRUE under these facts (a strict comparison on a
NULL-everywhere column, ``c IS NULL`` on a never-NULL one) makes the whole WHERE never TRUE; in HAVING,
MIN/MAX/SUM/AVG of a NULL-everywhere expression is NULL and of a never-NULL column (plain non-empty
``GROUP BY``) is never NULL, and ``COUNT`` is never NULL. Such a WHERE or HAVING is replaced by FALSE;
:mod:`kumosql.empty_rules` then empties whatever reads that select.
"""

from __future__ import annotations

from typing import Callable

from sqlglot import exp

from .ast_utils import extended_grouping

Key = tuple[str, str]
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)
# NULL as soon as one operand is NULL
_STRICT_BINARY = (exp.Add, exp.Sub, exp.Mul, exp.Div)
_STRICT_UNARY = (exp.Paren, exp.Neg, exp.Cast, exp.TryCast)
# NULL over a bag of NULLs (and over no row), never NULL over a non-empty bag of non-NULL values
_VALUE_AGGREGATES = (exp.Min, exp.Max, exp.Sum, exp.Avg)


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def _alias(source: exp.Expression) -> str | None:
    if isinstance(source, exp.Table):
        return (source.alias_or_name or "").lower() or None
    alias = source.args.get("alias")
    if isinstance(alias, exp.TableAlias) and alias.name:
        return alias.name.lower()
    return None


def _sources(select: exp.Select) -> dict[str, tuple[exp.Expression, bool]] | None:
    """The select's sources by alias, each with whether a join of the select can pad it with NULLs."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    joins = select.args.get("joins") or []
    # RIGHT, FULL and POSITIONAL (and any join this does not know) can pad the sources before them
    padding_all = any(
        join.side in ("RIGHT", "FULL")
        or join.method not in ("", "NATURAL")
        or join.kind not in ("", "INNER", "CROSS", "SEMI", "ANTI", "OUTER")
        or (join.kind == "OUTER" and join.side != "LEFT")
        for join in joins
    )
    found: dict[str, tuple[exp.Expression, bool]] = {}
    for node, padded in [(from_.this, padding_all)] + [
        (join.this, padding_all or join.side == "LEFT" or join.kind not in ("", "INNER", "CROSS")) for join in joins
    ]:
        name = _alias(node)
        if name is None:
            continue
        if name in found:
            return None
        found[name] = (node, padded)
    return found


def _key(node: exp.Expression, sources: dict) -> Key | None:
    if not isinstance(node, exp.Column) or isinstance(node.this, exp.Star) or node.args.get("db") or node.args.get("catalog"):
        return None
    table = (node.table or "").lower()
    if not table or table not in sources:
        return None
    return table, node.name.lower()


def _null_value(node: exp.Expression, is_null: Callable[[exp.Expression], bool]) -> bool:
    """``node`` is NULL whenever every term ``is_null`` names is NULL."""

    if isinstance(node, exp.Null) or is_null(node):
        return True
    if isinstance(node, _STRICT_BINARY):
        return _null_value(node.this, is_null) or _null_value(node.expression, is_null)
    if isinstance(node, _STRICT_UNARY):
        return _null_value(node.this, is_null)
    return False


def _never_true(cond: exp.Expression, is_null: Callable, is_nonnull: Callable) -> bool:
    """``cond`` is NULL or FALSE on every row where the named terms are NULL / not NULL."""

    if isinstance(cond, exp.Paren):
        return _never_true(cond.this, is_null, is_nonnull)
    if isinstance(cond, exp.Null) or (isinstance(cond, exp.Boolean) and not cond.this):
        return True
    if isinstance(cond, exp.And):
        return _never_true(cond.left, is_null, is_nonnull) or _never_true(cond.right, is_null, is_nonnull)
    if isinstance(cond, exp.Or):
        return _never_true(cond.left, is_null, is_nonnull) and _never_true(cond.right, is_null, is_nonnull)
    if isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null):
        return is_nonnull(cond.this)
    if isinstance(cond, exp.Not):
        inner = cond.this.this if isinstance(cond.this, exp.Paren) else cond.this
        return isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null) and _null_value(inner.this, is_null)
    if isinstance(cond, _COMPARISONS):
        sides = (cond.this, cond.expression)
        # x > ALL (empty) is TRUE whatever x is
        if cond.args.get("escape") is not None or any(isinstance(s, (exp.Any, exp.All)) for s in sides):
            return False
        return any(_null_value(side, is_null) for side in sides)
    if isinstance(cond, exp.Between):
        return any(_null_value(cond.args.get(k), is_null) for k in ("this", "low", "high") if cond.args.get(k) is not None)
    if isinstance(cond, exp.In) and not cond.args.get("unnest") and not cond.args.get("field"):
        # NULL IN (list) is NULL or FALSE, NULL IN (subquery) is NULL, or FALSE over no row
        if cond.expressions or isinstance(cond.args.get("query"), (exp.Select, exp.Subquery)):
            return _null_value(cond.this, is_null)
    return False


def _row_columns(cond: exp.Expression, sources: dict) -> set[Key]:
    """Columns of this select read in ``cond`` outside any subquery."""

    found = set()
    stack = [cond]
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Select, exp.Subquery, exp.SetOperation)):
            continue
        key = _key(node, sources)
        if key is not None:
            found.add(key)
        stack.extend(node.iter_expressions())
    return found


def _never(_node: exp.Expression) -> bool:
    return False


def _row_facts(select: exp.Select, memo: dict) -> tuple[dict, set[Key], set[Key]] | None:
    """The select's sources and the columns NULL on every row / never NULL after its WHERE."""

    sources = _sources(select)
    if sources is None:
        return None
    null: set[Key] = set()
    nonnull: set[Key] = set()
    for name, (node, padded) in sources.items():
        if not isinstance(node, exp.Subquery) or node.args["alias"].args.get("columns") or node.args.get("pivots"):
            continue
        always, never = _output_facts(node.this, memo)
        null |= {(name, column) for column in always}
        if not padded:
            nonnull |= {(name, column) for column in never}
    where = select.args.get("where")
    if where is not None:
        for part in _conjuncts(where.this):
            if isinstance(part, exp.Is) and isinstance(part.expression, exp.Null):
                key = _key(part.this, sources)
                if key is not None:
                    null.add(key)
                continue
            for key in _row_columns(part, sources):
                if _never_true(part, lambda node, key=key: _key(node, sources) == key, _never):
                    nonnull.add(key)
    return sources, null, nonnull


def _aggregate_argument(node: exp.Expression) -> exp.Expression | None:
    """The argument of a plain MIN/MAX/SUM/AVG (no window, FILTER, NULLS handling or other modifier)."""

    if not isinstance(node, _VALUE_AGGREGATES) or isinstance(node.parent, (exp.Window, exp.Filter, exp.IgnoreNulls, exp.RespectNulls)):
        return None
    if any(value not in (None, []) for name, value in node.args.items() if name != "this"):
        return None
    argument = node.this
    if isinstance(argument, exp.Distinct):
        if len(argument.expressions) != 1 or argument.args.get("on"):
            return None
        argument = argument.expressions[0]
    return argument


def _plain_grouping(select: exp.Select) -> bool:
    """A ``GROUP BY`` whose every group holds a row (not ``()``, ``ALL``, ``ROLLUP``, ``CUBE`` or ``GROUPING SETS``)."""

    group = select.args.get("group")
    if group is None or not group.expressions or group.args.get("all") or extended_grouping(group):
        return False
    return not any(isinstance(e, exp.Tuple) for e in group.expressions)


def _aggregates(select: exp.Select) -> bool:
    return any(
        node.find_ancestor(exp.Select) is select and node.find_ancestor(exp.Window) is None
        for projection in select.expressions
        for node in projection.find_all(exp.AggFunc)
    )


def _output_facts(query: exp.Expression, memo: dict) -> tuple[set[str], set[str]]:
    """Output names of a derived query that are NULL on every row / never NULL."""

    if id(query) in memo:
        return memo[id(query)]
    memo[id(query)] = (set(), set())
    if not isinstance(query, exp.Select) or query.args.get("kind") or query.args.get("operation_modifiers"):
        return memo[id(query)]
    facts = _row_facts(query, memo)
    if facts is None:
        return memo[id(query)]
    sources, null, nonnull = facts
    names: dict[str, list[exp.Expression]] = {}
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return memo[id(query)]
        names.setdefault(item.alias_or_name.lower(), []).append(item.this if isinstance(item, exp.Alias) else item)
    group = query.args.get("group")
    plain = _plain_grouping(query)
    columns_keep_values = (group is None and not _aggregates(query)) or plain

    def null_column(node: exp.Expression) -> bool:
        return _key(node, sources) in null

    always: set[str] = set()
    never: set[str] = set()
    for name, exprs in names.items():
        if len(exprs) != 1:
            continue
        expr = exprs[0]
        while isinstance(expr, exp.Paren):
            expr = expr.this
        if isinstance(expr, exp.Column):
            key = _key(expr, sources)
            if key in null and (group is not None or not _aggregates(query)):
                always.add(name)
            if key in nonnull and columns_keep_values:
                never.add(name)
            continue
        if isinstance(expr, exp.Null) or (isinstance(expr, (exp.Cast, exp.TryCast)) and isinstance(expr.this, exp.Null)):
            always.add(name)
            continue
        if isinstance(expr, exp.Count):
            never.add(name)
            continue
        argument = _aggregate_argument(expr)
        if argument is None or argument.find(exp.AggFunc, exp.Window, exp.Select) is not None:
            continue
        if _null_value(argument, null_column):
            always.add(name)
        elif plain and _key(argument, sources) in nonnull:
            never.add(name)
    memo[id(query)] = (always, never)
    return memo[id(query)]


def _impossible(condition: exp.Expression, is_null: Callable, is_nonnull: Callable) -> bool:
    return any(
        not isinstance(part, (exp.Boolean, exp.Null)) and _never_true(part, is_null, is_nonnull) for part in _conjuncts(condition)
    )


def null_contradiction(select: exp.Select) -> exp.Select | None:
    """Replace a WHERE or HAVING that the select's NULL facts make never TRUE by FALSE."""

    memo: dict = {}
    facts = _row_facts(select, memo)
    if facts is None:
        return None
    sources, null, nonnull = facts
    if not null and not nonnull:
        return None

    def row_null(node: exp.Expression) -> bool:
        return _key(node, sources) in null

    def row_nonnull(node: exp.Expression) -> bool:
        return _key(node, sources) in nonnull

    plain = _plain_grouping(select)

    def group_null(node: exp.Expression) -> bool:
        if row_null(node):
            return True  # a grouping key NULL on every row, or NULL in a rolled-up row
        argument = _aggregate_argument(node)
        return argument is not None and argument.find(exp.AggFunc, exp.Window, exp.Select) is None and _null_value(argument, row_null)

    def group_nonnull(node: exp.Expression) -> bool:
        if isinstance(node, exp.Count) and not isinstance(node.parent, exp.Window):
            return True
        argument = _aggregate_argument(node)
        return plain and argument is not None and row_nonnull(argument)

    where = select.args.get("where")
    having = select.args.get("having")
    new_where = where is not None and _impossible(where.this, row_null, row_nonnull)
    new_having = having is not None and _impossible(having.this, group_null, group_nonnull)
    if not new_where and not new_having:
        return None
    copy = select.copy()
    if new_where:
        copy.set("where", exp.Where(this=exp.false()))
    if new_having:
        copy.set("having", exp.Having(this=exp.false()))
    return copy
