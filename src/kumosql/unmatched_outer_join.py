"""A ``LEFT JOIN`` whose every match the ``WHERE`` clause rules out adds nothing.

``SELECT c.name FROM c LEFT JOIN o ON c.id = o.cid WHERE c.id NOT IN (SELECT cid FROM o)`` is
``SELECT c.name FROM c WHERE c.id NOT IN (SELECT cid FROM o)``.

Take a row ``r`` of the left side. The ``LEFT JOIN`` keeps it once per row of ``o`` that makes the ``ON``
condition TRUE, or once, padded with NULLs, when no row does. The ``WHERE`` conjunct ``c.id NOT IN (SELECT cid
FROM o)`` is TRUE only when ``c.id = o.cid`` is FALSE for every row of ``o`` (a NULL ``c.id`` or a NULL ``o.cid``
makes it NULL, never TRUE, unless ``o`` is empty). The ``ON`` condition is a conjunction that contains that same
equality, so it is TRUE for no row of ``o`` either: every row that passes the ``WHERE`` came out of the join
exactly once, padded. Rows that fail the ``WHERE`` are dropped on both sides. When nothing else reads ``o``,
the padded columns are never seen, and the join can go.

``NOT EXISTS (SELECT .. FROM o AS p WHERE p.cid = c.id)`` rules matches out the same way (it is TRUE exactly
when the equality is TRUE for no row).

Conditions, all checked:

* the join is a plain ``LEFT [OUTER] JOIN`` of a base table with an ``ON`` condition (no ``USING``, ``NATURAL``,
  lateral or sampled source), and no join of the select uses ``USING`` or ``NATURAL`` (they read columns by name);
* the test is a top-level ``WHERE`` conjunct of the same select (under ``OR`` or in ``HAVING`` it does not
  hold for every surviving row);
* its subquery reads the joined table and nothing else: one bare column, no ``WHERE`` (a filtered subquery
  rules out only the matches that pass the filter), no grouping, ``LIMIT`` or set operation; the ``EXISTS``
  form has the equality as its whole ``WHERE``;
* the tested value is a column of a source that comes before the join, and the ``ON`` condition has the
  conjunct ``that column = joined.column`` with the subquery's column;
* no other expression of the select, at any depth, reads the joined table: every column there names another
  source, or is a bare name that the schema shows the joined table does not have (inside a nested query, a bare
  name that a table of that query has binds there first). Without a schema for the joined table nothing is
  rewritten.

Later joins may follow: they do not read the joined table, so each row derived from ``r`` is repeated once per
match of ``r`` either way, and rows a later ``RIGHT`` or ``FULL`` join pads on the left never met the joined
table at all.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import same_table, select_sources, visible_ctes

_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
_TEST_ARGS = frozenset({"expressions", "from_", "from", "where", "distinct"})


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
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


def _columns_of(table: exp.Table, declared: dict[str, set[str]]) -> set[str] | None:
    full = ".".join(part.name for part in table.parts).lower()
    return declared.get(full) or declared.get(table.name.lower())


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _plain_column(node: exp.Expression) -> exp.Column | None:
    node = _unwrap(node)
    if type(node) is not exp.Column or isinstance(node.this, exp.Star) or node.args.get("db") or node.args.get("catalog"):
        return None
    return node


def _unread(select: exp.Select, join: exp.Join, name: str, columns: set[str], declared: dict[str, set[str]]) -> bool:
    """Whether nothing in ``select`` outside ``join`` can read the table joined as ``name``."""

    own = _source_names(select)

    for item in select.expressions:
        inner = item.unalias()
        if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
            return False
    for node in select.walk():
        if not isinstance(node, exp.Column) or _inside(node, join):
            continue
        qualifiers = [part.name.lower() for part in node.parts[:-1]]
        nearer = _nearer_scopes(node, select)
        if nearer is None:
            return False
        if qualifiers:
            if len(qualifiers) == 1 and any(qualifiers[0] in _source_names(scope) for scope in nearer):
                continue  # a nested query declares that name itself: the column is its own
            if len(qualifiers) == 1 and qualifiers[0] != name and qualifiers[0] in own:
                continue  # another source of this select
            if name in qualifiers or any(q in columns for q in qualifiers):
                return False  # a read of the joined table, or of a field of a column it may own
            continue
        if isinstance(node.this, exp.Star):
            if node.find_ancestor(exp.Select) is select:
                return False
            continue  # a nested query's own star
        if node.name.lower() not in columns:
            continue
        if not any(_provides(scope, node.name.lower(), declared) for scope in nearer):
            return False  # a bare name the joined table has, and no nearer source is known to have
    return True


def _nearer_scopes(node: exp.Expression, select: exp.Select) -> list[exp.Select] | None:
    """The selects strictly inside ``select`` that enclose ``node``, innermost first (``None`` if it is not inside)."""

    scopes = []
    scope = node.find_ancestor(exp.Select)
    while scope is not None and scope is not select:
        scopes.append(scope)
        scope = scope.find_ancestor(exp.Select)
    return scopes if scope is select else None


def _source_names(scope: exp.Select) -> set[str]:
    return {(s.alias_or_name or "").lower() for s in select_sources(scope)}


def _provides(scope: exp.Select, column: str, declared: dict[str, set[str]]) -> bool:
    """Whether a source of ``scope`` is known to expose ``column`` (so a bare name binds there)."""

    for source in select_sources(scope):
        if type(source) is exp.Table and _plain_table(source) and column in (_columns_of(source, declared) or ()):
            return True
        if type(source) is exp.Subquery and type(source.this) is exp.Select:
            alias = source.args.get("alias")
            if alias is not None and alias.args.get("columns"):
                continue
            items = source.this.expressions
            if not any(isinstance(i.unalias(), exp.Star) or (isinstance(i.unalias(), exp.Column) and isinstance(i.unalias().this, exp.Star)) for i in items):
                if column in {i.alias_or_name.lower() for i in items}:
                    return True
    return False


def _ruled_out(test: exp.Expression, joined: exp.Table, columns: set[str], dialect: str) -> tuple[exp.Column, str] | None:
    """``(x, y)`` when the ``WHERE`` conjunct ``test`` is TRUE only if ``x = joined.y`` is TRUE for no row."""

    if type(test) is not exp.Not:
        return None
    inner = _unwrap(test.this)
    if type(inner) is exp.In:
        query = inner.args.get("query")
        if query is None or any(value for key, value in inner.args.items() if key not in ("this", "query")):
            return None
        probe = query.this if type(query) is exp.Subquery and not query.alias else None
        value = _plain_column(inner.this)
        if type(probe) is not exp.Select or value is None or not value.table:
            return None
        if any(v for key, v in probe.args.items() if key not in _TEST_ARGS) or probe.args.get("where") is not None:
            return None
        distinct = probe.args.get("distinct")
        if distinct is not None and distinct.args.get("on"):
            return None
        sources = select_sources(probe)
        if len(sources) != 1 or not _plain_table(sources[0]) or not same_table(sources[0], joined, dialect):
            return None
        if len(probe.expressions) != 1:
            return None
        picked = _plain_column(probe.expressions[0])
        if picked is None or picked.table.lower() not in ("", sources[0].alias_or_name.lower()):
            return None
        if picked.name.lower() not in columns or value.table.lower() == sources[0].alias_or_name.lower():
            return None
        return value, picked.name.lower()
    if type(inner) is exp.Exists:
        probe = inner.this
        if type(probe) is exp.Subquery and not probe.alias:
            probe = probe.this
        if type(probe) is not exp.Select or any(v for key, v in probe.args.items() if key not in _TEST_ARGS) or probe.args.get("distinct") is not None:
            return None
        if any(isinstance(n, (exp.AggFunc, exp.Window)) for item in probe.expressions for n in item.walk()):
            return None
        sources = select_sources(probe)
        if len(sources) != 1 or not _plain_table(sources[0]) or not same_table(sources[0], joined, dialect):
            return None
        local = sources[0].alias_or_name.lower()
        tests = _conjuncts(probe.args["where"].this) if probe.args.get("where") is not None else []
        if len(tests) != 1 or type(tests[0]) is not exp.EQ:
            return None
        sides = [_plain_column(tests[0].this), _plain_column(tests[0].expression)]
        if None in sides:
            return None
        for picked, value in (sides, sides[::-1]):
            if picked.table.lower() == local and picked.name.lower() in columns and value.table and value.table.lower() != local:
                return value, picked.name.lower()
    return None


def _on_equates(on: exp.Expression, value: exp.Column, name: str, column: str) -> bool:
    for part in _conjuncts(on):
        if type(part) is not exp.EQ:
            continue
        sides = [_plain_column(part.this), _plain_column(part.expression)]
        if None in sides:
            continue
        for mine, other in (sides, sides[::-1]):
            if (
                mine.table.lower() == name
                and mine.name.lower() == column
                and other.table.lower() == value.table.lower()
                and other.name.lower() == value.name.lower()
            ):
                return True
    return False


def drop_unmatched_left_join(select: exp.Select, schema: dict | None, dialect: str = "bigquery") -> exp.Expression | None:
    """``select`` (in place) without a ``LEFT JOIN`` that a ``WHERE`` conjunct proves unmatched, else ``None``."""

    joins = select.args.get("joins") or []
    where = select.args.get("where")
    if not joins or where is None or not schema or select.args.get("laterals"):
        return None
    if any(j.args.get("using") or j.args.get("method") for j in joins):
        return None
    declared = {str(t).lower(): {str(c).lower() for c in columns} for t, columns in schema.items()}
    sources = select_sources(select)
    names = [(s.alias_or_name or "").lower() for s in sources]
    tests = _conjuncts(where.this)
    for index, join in enumerate(joins):
        joined = join.this
        if (join.args.get("side") or "").upper() != "LEFT" or (join.args.get("kind") or "").upper() not in ("", "OUTER"):
            continue
        if join.args.get("on") is None or any(v for key, v in join.args.items() if key not in ("this", "side", "kind", "on")):
            continue
        if not _plain_table(joined):
            continue
        name = (joined.alias_or_name or "").lower()
        columns = _columns_of(joined, declared)
        if not name or not columns or names.count(name) != 1:
            continue
        before = set(names[: index + 1])
        for test in tests:
            found = _ruled_out(test, joined, columns, dialect)
            if found is None:
                continue
            value, column = found
            if value.table.lower() == name or value.table.lower() not in before:
                continue
            if not _on_equates(join.args["on"], value, name, column):
                continue
            if not _unread(select, join, name, columns, declared):
                continue
            join.pop()
            return select
    return None
