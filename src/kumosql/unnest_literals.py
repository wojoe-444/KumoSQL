"""Split a cross join with ``UNNEST`` of an array literal into one select per element.

``SELECT L FROM s CROSS JOIN UNNEST([e1, .., en]) AS e WHERE w`` pairs every row of ``s`` with each of the ``n``
elements (a NULL element included: inside a query a BigQuery array may hold NULLs, and ``UNNEST`` returns one row per
element), so as a bag it is ``SELECT L[e := e1] FROM s WHERE w[e := e1] UNION ALL .. UNION ALL SELECT L[e := en] FROM s
WHERE w[e := en]``; the order of rows is not part of a bag. Joins after the ``UNNEST`` come along: an inner, cross or
left join treats each row of its left input on its own, so it distributes over the union of its left input. A plain
``SELECT DISTINCT`` becomes ``UNION DISTINCT`` (duplicates removed over all the branches together; with a
single element there is no union and the select keeps its DISTINCT). The provers model
``UNNEST`` only as a cross join with an unknown array, and not at all inside a derived table; the split gives them plain
selects.

Each element is an integer literal or a column of a source listed before the ``UNNEST`` in the same ``FROM``,
qualified by that source's alias, so in every branch it reads the same value from the same row. All elements have one
type (integer literals with ``INT64`` columns, or columns of one declared type), so BigQuery converts none of them to a
common supertype and ``e`` has that type, as each substituted element does.

Declined (left as is): ``WITH OFFSET``; an array that is not a literal (a column, ``ARRAY(subquery)``,
``GENERATE_ARRAY``, a typed ``ARRAY<T>[..]``); an empty or very long literal; an element that is a NULL, a string, a
float, an expression, an unqualified column or a column of a later or unknown source or of an undeclared type; mixed
types; an ``UNNEST`` that is not a plain ``CROSS JOIN`` or comma join (``LEFT JOIN UNNEST`` keeps a row for an empty
array, ``JOIN .. ON`` filters); a ``RIGHT``, ``FULL``, ``USING`` or ``NATURAL`` join after it; a name ``e`` that is also a
relation of the select or a column of one of its sources (or of a source whose columns are unknown); ``e`` read
qualified (``e.x``), in a nested subquery or in a join before the ``UNNEST``; a select that aggregates, groups, has a
window, ``QUALIFY``, ``ORDER BY``, ``LIMIT``, ``DISTINCT ON``, a star, a WITH clause or a random function; a select that
sits anywhere other than at the top, in a derived table, a WITH table, ``EXISTS``, ``IN`` or a set operation.
Only BigQuery is rewritten.
"""

from __future__ import annotations

import re

from sqlglot import exp

from .ast_utils import _output_names

_MAX_ELEMENTS = 16
_MAX_ROUNDS = 8
_MAX_BRANCHES = 64  # selects added to one query, so nested or repeated UNNESTs cannot blow up the prover's input

_INTEGER_TYPES = {"INT64", "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"}
_SCOPES = (exp.Select, exp.SetOperation, exp.Subquery)
_PARENTS = (exp.Subquery, exp.SetOperation, exp.Exists, exp.CTE)


def _type_name(raw: str) -> str:
    name = re.sub(r"\s+", "", str(raw)).upper()
    return "INT64" if name in _INTEGER_TYPES else name


def _own_scope(select: exp.Select):
    """The nodes of ``select`` outside its nested queries (a nested query's root is yielded, not entered)."""

    return select.dfs(prune=lambda n: n is not select and isinstance(n, _SCOPES))


def _integer_literal(node: exp.Expression) -> bool:
    if isinstance(node, exp.Neg):
        node = node.this
    # at most 18 digits: always an INT64 (a longer literal may not fit and is not rewritten)
    return isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"[0-9]{1,18}", node.name) is not None


def _source_name(source: exp.Expression) -> str:
    return (source.alias_or_name or "").lower()


def _source_columns(source: exp.Expression, schema: dict[str, list[str]]) -> set[str] | None:
    if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
        key = ".".join(p.name for p in source.parts).lower()
        columns = schema.get(key) or (schema.get(source.name.lower()) if source.db else None)
        return {c.lower() for c in columns} if columns is not None else None
    if isinstance(source, exp.Subquery):
        names = _output_names(source.this)
        return {n.lower() for n in names} if names is not None else None
    if isinstance(source, exp.Unnest):
        alias = source.args.get("alias")
        columns = alias.args.get("columns") if alias is not None else None
        return {c.name.lower() for c in columns or []}
    return None


def _element_type(element: exp.Expression, preceding: dict[str, exp.Expression], types: dict[str, dict[str, str]]) -> str | None:
    if _integer_literal(element):
        return "INT64"
    if not isinstance(element, exp.Column) or not element.table or element.args.get("db") or element.args.get("catalog"):
        return None
    source = preceding.get(element.table.lower())
    if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
        return None
    key = ".".join(p.name for p in source.parts).lower()
    columns = types.get(key) or (types.get(source.name.lower()) if source.db else None)
    raw = (columns or {}).get(element.name.lower())
    return _type_name(raw) if raw else None


def _join_kind(join: exp.Join) -> tuple[str, str]:
    return (join.args.get("side") or "").upper(), (join.args.get("kind") or "").upper()


def _split(select: exp.Select, schema: dict[str, list[str]], types: dict[str, dict[str, str]]) -> exp.Expression | None:
    joins = list(select.args.get("joins") or [])
    from_ = select.args.get("from_") or select.args.get("from")
    index = next((i for i, j in enumerate(joins) if isinstance(j.this, exp.Unnest)), None)
    if from_ is None or index is None:
        return None
    if select.parent is not None and not isinstance(select.parent, _PARENTS):
        return None
    join, unnest = joins[index], joins[index].this
    if _join_kind(join) != ("", "CROSS") or join.args.get("on") or join.args.get("using") or join.args.get("method"):
        return None
    alias = unnest.args.get("alias")
    columns = alias.args.get("columns") if alias is not None else None
    if unnest.args.get("offset") or len(unnest.expressions) != 1 or not columns or len(columns) != 1 or alias.name:
        return None
    array = unnest.expressions[0]
    if type(array) is not exp.Array or not 1 <= len(array.expressions) <= _MAX_ELEMENTS:
        return None
    name = columns[0].name.lower()
    for later in joins[index + 1 :]:
        side, kind = _join_kind(later)
        if side not in ("", "LEFT") or kind not in ("", "INNER", "CROSS", "OUTER") or (kind == "OUTER" and not side):
            return None
        if later.args.get("using") or later.args.get("method"):
            return None
    # the select: a plain projection and filter of its sources
    if any(select.args.get(k) for k in ("group", "having", "qualify", "order", "limit", "offset", "windows", "with", "with_", "prewhere", "connect", "sort", "cluster", "distribute")):
        return None
    distinct = select.args.get("distinct")
    if distinct is not None and (not isinstance(distinct, exp.Distinct) or distinct.args.get("on")):
        return None
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
        return None
    own = list(_own_scope(select))
    if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Rand)) for n in own):
        return None
    if any(isinstance(n, exp.Anonymous) and n.name.upper() in ("RAND", "GENERATE_UUID") for n in own):
        return None
    # ``e`` names the element and nothing else in the select
    sources = [from_.this] + [j.this for j in joins]
    if any(_source_name(s) == name for s in sources if s is not unnest):
        return None
    for source in sources:
        if source is unnest:
            continue
        known = _source_columns(source, schema)
        if known is None or name in known:
            return None
    preceding = {_source_name(s): s for s in sources[: index + 1]}
    kinds = {_element_type(element, preceding, types) for element in array.expressions}
    if None in kinds or len(kinds) != 1 or (any(_integer_literal(e) for e in array.expressions) and kinds != {"INT64"}):
        return None
    own_ids = {id(n) for n in own}
    earlier = {id(n) for j in joins[:index] for n in j.walk()} | {id(n) for n in unnest.walk()}
    for column in select.find_all(exp.Column):
        parts = [p.lower() for p in (column.name, column.table, column.db, column.catalog) if p]
        if name not in parts:
            continue
        if id(column) not in own_ids or id(column) in earlier or column.table or isinstance(column.parent, exp.Dot):
            return None
    branches = []
    for element in array.expressions:
        branch = select.copy()
        branch.args["joins"][index].pop()
        if not branch.args.get("joins"):
            branch.set("joins", None)
        if distinct is not None and len(array.expressions) > 1:  # one branch is no union: it keeps its own DISTINCT
            branch.set("distinct", None)
        for column in [c for c in _own_scope(branch) if isinstance(c, exp.Column) and not c.table and c.name.lower() == name]:
            value = element.copy()
            if column.parent is branch:  # a bare projection keeps its output name
                value = exp.alias_(value, column.this.copy())
            column.replace(value)
        branches.append(branch)
    result: exp.Expression = branches[0]
    for branch in branches[1:]:
        result = exp.Union(this=result, expression=branch, distinct=distinct is not None)
    if isinstance(select.parent, exp.SetOperation) and isinstance(result, exp.SetOperation):
        result = exp.Subquery(this=result)
    return result


def split_unnest_literals(
    tree: exp.Expression,
    schema: dict[str, list[str]] | None = None,
    types: dict[str, dict[str, str]] | None = None,
    dialect: str = "bigquery",
) -> exp.Expression:
    """Split every select that cross joins ``UNNEST`` of an array literal into a union (module doc)."""

    if dialect != "bigquery" or not any(isinstance(n, exp.Unnest) for n in tree.walk()):
        return tree
    lowered = {k.lower(): v for k, v in (schema or {}).items()}
    typed = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in (types or {}).items()}
    budget = _MAX_BRANCHES
    for _ in range(_MAX_ROUNDS):
        changed = False
        for select in reversed(list(tree.find_all(exp.Select))):
            if select is not tree and select.parent is None:
                continue  # replaced earlier in this round
            replacement = _split(select, lowered, typed)
            added = len(list(replacement.find_all(exp.Select))) - 1 if replacement is not None else 0
            if replacement is None or added > budget:
                continue
            budget -= added
            changed = True
            if select is tree:
                tree = replacement
            else:
                select.replace(replacement)
        if not changed:
            break
    return tree
