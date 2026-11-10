"""Read a derived table's ``STRUCT`` column field by field, as plain columns.

In BigQuery ``SELECT s.f AS a FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)`` reads field ``f`` of the
derived table's STRUCT column ``s``: no relation in scope is named ``s``, so ``s.f`` is a field access, not a
qualified column. The provers model a column as one scalar value and read ``s`` as an unknown table alias, so the
derived table here outputs every field that is read as a column of its own, under a fresh name, and each read
``s.f`` reads that column: ``SELECT kq_struct0.kq_field0 AS a FROM (SELECT x.a AS kq_field0 FROM t AS x) AS kq_struct0``.

Sound because a ``STRUCT(e1 AS f1, ..)`` constructor is never NULL and its field ``fi`` is ``ei`` evaluated on the
same row (NULL when ``ei`` is), with no conversion; the rewrite keeps every row of the derived table (no row is added,
dropped or repeated) and evaluates each field once per row, as the struct did. A field that is never read is dropped,
like any unread column of a derived table. Fresh names occur nowhere else in the query, so nothing captures them.

Declined (left as is) whenever the struct might be read other than field by field, or ``s.f`` might not be a field
access: a relation, table or WITH table named ``s`` anywhere in the query; another source of the reading select that
has (or may have) a column ``s``; a select-list alias ``s`` in the reading select; ``s`` read whole (``SELECT s``,
``s IS NULL``, comparisons, ``GROUP BY s``), through a deeper path (``s.f.g``, ``d.s.f``), from a nested subquery or
through a star; the derived table read as a whole row; a ``USING`` or ``NATURAL`` join; ``DISTINCT``, a star or
any other mention of ``s`` inside the derived table; ``GROUP BY ALL`` or an ordinal in its ``GROUP BY`` or
``ORDER BY`` (they read the select list the rewrite changes); a field without a name, repeated (case-insensitively) or read but
not declared; a typed ``STRUCT<..>(..)`` (a cast) or a struct under any other expression; a derived table that is a
set operation. Only BigQuery is rewritten.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import _output_names, extended_grouping, select_sources

_MAX_ROUNDS = 64


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _identifiers(tree: exp.Expression) -> set[str]:
    return {node.name.lower() for node in tree.find_all(exp.Identifier) if node.name}


def _relation_names(tree: exp.Expression) -> set[str]:
    """Every name a relation could go by anywhere in ``tree``: aliases, table names (each part), WITH tables."""

    names: set[str] = set()
    for alias in tree.find_all(exp.TableAlias):
        names.add(alias.name.lower())
        names |= {c.name.lower() for c in alias.args.get("columns") or []}  # an UNNEST alias names a value table
    for table in tree.find_all(exp.Table):
        names |= {p.name.lower() for p in table.parts if p.name}
    for cte in tree.find_all(exp.CTE):
        names.add(cte.alias_or_name.lower())
    return names - {""}


def _source_columns(source: exp.Expression, schema: dict[str, list[str]]) -> set[str] | None:
    """The column names a FROM item exposes, or ``None`` when they are not known."""

    if isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
        key = ".".join(p.name for p in source.parts).lower()
        columns = schema.get(key) or (schema.get(source.name.lower()) if source.db else None)
        return {c.lower() for c in columns} if columns is not None else None
    if isinstance(source, exp.Subquery):
        names = _output_names(source.this)
        return {n.lower() for n in names} if names is not None else None
    return None


def _fields(struct: exp.Struct) -> list[tuple[str, exp.Expression]] | None:
    """``(name, expression)`` of each field of ``STRUCT(e1 AS f1, ..)``, or ``None`` unless every field is named once."""

    fields = []
    for item in struct.expressions:
        if isinstance(item, exp.PropertyEQ) and isinstance(item.this, exp.Identifier) and item.this.name:
            fields.append((item.this.name.lower(), item.expression))
        elif isinstance(item, exp.Alias) and item.alias:
            fields.append((item.alias.lower(), item.this))
        else:
            return None
    names = [name for name, _ in fields]
    if not fields or len(set(names)) != len(names):
        return None
    return fields


def _mentions(column: exp.Column, name: str) -> bool:
    return any(part.lower() == name for part in (column.name, column.table, column.db, column.catalog) if part)


def _fresh(prefix: str, taken: set[str]) -> str:
    index = 0
    while f"{prefix}{index}" in taken:
        index += 1
    name = f"{prefix}{index}"
    taken.add(name)
    return name


def _split_one(subquery: exp.Subquery, schema: dict[str, list[str]], relations: set[str], taken: set[str]) -> bool:
    holder = subquery.parent
    outer = holder.parent if isinstance(holder, (exp.From, exp.Join)) else None
    inner = subquery.this
    if not isinstance(outer, exp.Select) or not isinstance(inner, exp.Select):
        return False
    if inner.args.get("distinct") or inner.args.get("with") or inner.args.get("with_") or any(_is_star(e) for e in inner.expressions):
        return False
    group = inner.args.get("group")
    if group is not None and (group.args.get("all") or any(isinstance(e, exp.Literal) for e in group.expressions)):
        return False  # GROUP BY ALL / GROUP BY 2 read the select list, which the rewrite changes
    if any(isinstance(o.this, exp.Literal) for o in (inner.args.get("order") or exp.Order()).expressions):
        return False
    candidates = [
        item for item in inner.expressions
        if isinstance(item, exp.Alias) and isinstance(item.this, exp.Struct) and item.alias and item.alias.lower() not in relations
    ]
    if not candidates:
        return False
    if any(j.args.get("using") or j.args.get("method") or (j.args.get("kind") or "").upper() == "NATURAL" for j in outer.args.get("joins") or []):
        return False
    if any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in outer.find_all(exp.Star) if not _within(s, subquery)):
        return False
    derived = (subquery.alias or "").lower()
    outputs = [(item.alias_or_name or "").lower() for item in inner.expressions]
    outer_aliases = {(item.alias or "").lower() for item in outer.expressions if isinstance(item, exp.Alias)}
    for item in candidates:
        name = item.alias.lower()
        fields = _fields(item.this)
        if fields is None or outputs.count(name) != 1 or name in outer_aliases:
            continue
        # no other mention of ``s`` inside the derived table (GROUP BY s, ORDER BY s, a field reading s, ...)
        if any(_mentions(c, name) for c in inner.find_all(exp.Column)):
            continue
        # no other source of the reading select may hold a column ``s``
        others = [s for s in select_sources(outer) if s is not subquery]
        known = [_source_columns(s, schema) for s in others]
        if any(columns is None or name in columns for columns in known):
            continue
        reads = _field_reads(outer, subquery, name, derived, {f for f, _ in fields})
        if reads is None or _loses_aggregation(inner, item, fields, reads):
            continue
        _rewrite(subquery, item, fields, reads, taken)
        return True
    return False


def _own_aggregates(node: exp.Expression) -> bool:
    """Whether ``node`` holds an aggregate of its own select (not one of a window or of a nested query)."""

    def other_scope(n: exp.Expression) -> bool:
        return isinstance(n, exp.Window) or (n is not node and isinstance(n, (exp.Select, exp.SetOperation, exp.Subquery)))

    return any(isinstance(n, exp.AggFunc) for n in node.dfs(prune=other_scope))


def _loses_aggregation(inner: exp.Select, item: exp.Alias, fields: list[tuple[str, exp.Expression]], reads: list[exp.Column]) -> bool:
    """Whether dropping the unread fields could drop the only aggregate of ``inner``.

    A select with an aggregate and no ``GROUP BY`` returns one row even over no input, one without returns a row per
    input row. So an aggregate inside a field nobody reads must not vanish unless something else keeps ``inner`` an
    aggregation: a plain ``GROUP BY`` (it fixes the rows whatever the select list holds), or another aggregate that
    stays (another select item, a field that is read, ``HAVING``, ``QUALIFY`` or ``ORDER BY``).
    """

    kept_names = {column.name.lower() for column in reads}
    if not kept_names and len(inner.expressions) == 1:
        kept_names = {fields[0][0]}  # see ``_rewrite``: one field stays so the derived table keeps a column
    dropped = [expression for field, expression in fields if field not in kept_names]
    if not any(_own_aggregates(expression) for expression in dropped):
        return False
    group = inner.args.get("group")
    if group is not None and group.expressions and not extended_grouping(group):
        return False
    stays = [e for e in inner.expressions if e is not item]
    stays += [expression for field, expression in fields if field in kept_names]
    stays += [inner.args[k] for k in ("having", "qualify", "order") if inner.args.get(k) is not None]
    return not any(_own_aggregates(part) for part in stays)


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _field_reads(outer: exp.Select, subquery: exp.Subquery, name: str, derived: str, fields: set[str]) -> list[exp.Column] | None:
    """Every read of struct column ``name`` in ``outer`` as ``name.<field>``, or ``None`` if any read is another kind."""

    reads = []
    for column in outer.find_all(exp.Column):
        if _within(column, subquery):
            continue
        whole_row = bool(derived) and not column.table and column.name.lower() == derived
        if not _mentions(column, name) and not whole_row:
            continue
        if (
            whole_row
            or column.table.lower() != name
            or column.db
            or column.catalog
            or column.name.lower() not in fields
            or isinstance(column.parent, exp.Dot)
            or column.find_ancestor(exp.Select) is not outer
        ):
            return None
        reads.append(column)
    return reads


def _rewrite(subquery: exp.Subquery, item: exp.Alias, fields: list[tuple[str, exp.Expression]], reads: list[exp.Column], taken: set[str]) -> None:
    alias = subquery.alias
    if not alias:
        alias = _fresh("kq_struct", taken)
        subquery.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    expressions = dict(fields)
    fresh: dict[str, str] = {}
    for column in reads:
        field = column.name.lower()
        if field not in fresh:
            fresh[field] = _fresh("kq_field", taken)
    inner = subquery.this
    items = list(inner.expressions)
    position = next(i for i, e in enumerate(items) if e is item)
    added = [exp.alias_(expressions[field].copy(), new) for field, new in fresh.items()]
    if not added and len(items) == 1:
        # nothing reads the struct and it is the only output: keep one field so the derived table keeps a column
        first, expression = fields[0]
        added = [exp.alias_(expression.copy(), _fresh("kq_field", taken))]
    inner.set("expressions", items[:position] + added + items[position + 1 :])
    for column in reads:
        column.replace(exp.column(fresh[column.name.lower()], table=alias))


def split_struct_fields(tree: exp.Expression, schema: dict[str, list[str]] | None = None, dialect: str = "bigquery") -> exp.Expression:
    """Rewrite every derived table whose STRUCT column is only read field by field (module doc). In place."""

    if dialect != "bigquery" or not any(isinstance(n, exp.Struct) for n in tree.walk()):
        return tree
    lowered = {k.lower(): v for k, v in (schema or {}).items()}
    relations = _relation_names(tree)
    taken = _identifiers(tree)
    for _ in range(_MAX_ROUNDS):
        if not any(_split_one(subquery, lowered, relations, taken) for subquery in list(tree.find_all(exp.Subquery))):
            break
    return tree
