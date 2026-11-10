"""Write ``t.x`` for a bare ``x`` where a quantified comparison or a renaming projection is read.

``SELECT deptno FROM dept WHERE emp.job = name`` is ``SELECT dept.deptno FROM dept WHERE emp.job =
dept.name``: SQL resolves a bare name in the innermost query that has a source with a column of that
name, and a select with one FROM item and no joins has only that one source to offer. The rewrites
that read a derived table as its base table (``_inline_expression_projection``, the decorrelation
steps) and the quantified read-back need every column to name its source and decline on a bare one,
so two spellings of one query otherwise stay apart.

Where it is applied (and nowhere else):

* the select that holds a quantified comparison ``x > ANY (SELECT ...)`` (``ANY``, ``SOME``,
  ``ALL``) and the select that is its subquery: Calcite prints ``empno > ANY (SELECT deptno FROM
  dept WHERE emp.job = dept.name)`` with bare columns;
* a select whose only source is a derived table that just renames or projects the columns of one
  source (``FROM dept AS dept(deptno0, name)`` once ``expand_alias_columns`` has made the column
  list a projection): its bare columns then name the derived table, which the existing rules can
  merge, so the table's NOT NULL columns are seen through the renaming.

Every other select keeps its bare columns for the normalizer's later qualification passes and the
SMT compiler's own lookup. Those places are where ``proof_columns`` re-derives each choice, and
``tests/test_proof_columns.py`` injects its faults there on a bare column inside ``EXISTS``; a
pass that qualified every one-source select first would be correct, but those injected faults
would no longer reach a bare column.

Only qualifiers are added, and only where the owner is certain:

* the select has one FROM item and no join, lateral view or WITH of its own;
* the item is a base table whose columns the caller supplied (and no WITH table of that name is
  left in the statement), or a derived table that names every output (no star, no repeats, no
  unnamed expression);
* the bare name is one of the item's columns, is not the item's own name, and is not a name the
  select outputs (an alias, a qualified column or a field): GROUP BY, HAVING and ORDER BY may read
  the output first, and which one wins differs by dialect;
* the column is read in the select's own scope: not in the FROM clause and not below a nested
  query, a set operation or a lambda, whose own names come first;
* the name is not the head of a field path (``x.f`` printed as ``t.x.f`` would be read back as a
  table path) and is not a date part given to a function (``DATE_DIFF(a, b, day)``).

A bare name the item does not have (a correlated reference, a name nobody listed) stays as it is.
On BigQuery the added qualifiers are re-derived from the text by ``proof_columns``, as for the
normalizer's other qualification passes; a disagreement declines the query.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import select_sources

# Words a function may take as a unit; sqlglot reads some of them as columns.
_DATE_PARTS = frozenset(
    "MICROSECOND MILLISECOND NANOSECOND SECOND MINUTE HOUR DAY DAYOFWEEK DAYOFYEAR WEEK ISOWEEK MONTH QUARTER YEAR ISOYEAR "
    "DATE TIME DATETIME DECADE CENTURY MILLENNIUM".split()
)
# What a FROM item may carry without changing its columns; anything else (PIVOT, a nested join, ...) is not read.
_TABLE_ARGS = frozenset({"this", "alias", "db", "catalog", "comments", "version", "sample", "hints"})
_SUBQUERY_ARGS = frozenset({"this", "alias", "comments"})
# A select that only lists columns of its one source: no filter, grouping, DISTINCT, ordering or row limit.
_PROJECTION_ARGS = frozenset({"expressions", "from", "from_", "comments"})


def qualify_single_source_columns(tree: exp.Expression, schema: dict[str, list[str]] | None, dialect: str = "bigquery") -> exp.Expression:
    """Qualify, in place, the bare columns of the single-source selects named in the module docstring."""

    if not schema:
        return tree
    from .algebraic_equivalence import _checked_qualification

    known = {key.lower(): {c.lower() for c in columns} for key, columns in schema.items()}
    return _checked_qualification(tree, lambda t: _qualify(t, known), schema, dialect, "single_source_qualification")


def _output_names(query: exp.Expression) -> set[str] | None:
    """The column names of a derived table's query, or None when one is unnamed, repeated or a star."""

    if not isinstance(query, exp.Select):
        return None  # a set operation or a nested query: not needed here, so not read
    names = []
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        if not isinstance(item, (exp.Alias, exp.Column)):
            return None  # an unnamed expression gets a name the dialect makes up
        names.append(item.alias_or_name.lower())
    if not names or "" in names or len(set(names)) != len(names):
        return None
    return set(names)


def _carries(node: exp.Expression, allowed: frozenset[str]) -> bool:
    """Whether ``node`` has a clause outside ``allowed`` (a PIVOT, a nested join, ...) that could change its columns."""

    return any(value not in (None, False, [], "") for key, value in node.args.items() if key not in allowed)


def _source_columns(source: exp.Expression | None, known: dict[str, set[str]], with_names: set[str]) -> set[str] | None:
    alias = source.args.get("alias") if isinstance(source, (exp.Table, exp.Subquery)) else None
    if alias is not None and (not isinstance(alias, exp.TableAlias) or alias.args.get("columns")):
        return None  # a column list renames the columns (``expand_alias_columns`` has replaced the ones it could)
    if isinstance(source, exp.Table):
        if not isinstance(source.this, exp.Identifier) or _carries(source, _TABLE_ARGS):
            return None
        if not source.args.get("db") and source.name.lower() in with_names:
            return None  # a WITH table that was not inlined: its columns are its query's
        return known.get(".".join(p.name for p in source.parts).lower())
    if isinstance(source, exp.Subquery) and source.alias and not _carries(source, _SUBQUERY_ARGS):
        return _output_names(source.this)
    return None


def _reserved_names(select: exp.Select) -> set[str]:
    """Names the select outputs, which GROUP BY, HAVING and ORDER BY may read before a source column."""

    names: set[str] = set()
    for item in select.expressions:
        value = item
        while isinstance(value, exp.Paren):
            value = value.this
        if isinstance(item, exp.Alias):
            names.add(item.alias.lower())
        elif isinstance(value, exp.Column) and value.args.get("table") is not None:
            names.add(value.name.lower())
        elif isinstance(value, exp.Dot):
            names.add(value.text("expression").lower())
        for star in value.find_all(exp.Star):
            for part in (*(star.args.get("replace") or []), *(star.args.get("rename") or [])):
                names.update(identifier.name.lower() for identifier in part.find_all(exp.Identifier))
    return names


def _own_scope(column: exp.Column, select: exp.Select) -> bool:
    """Whether ``column`` is read by ``select`` itself, outside its FROM clause."""

    node = column.parent
    while node is not None and node is not select:
        if isinstance(node, (exp.Query, exp.From, exp.Join, exp.Lambda, exp.Values, exp.Table)):
            return False
        node = node.parent
    return node is select


def _renaming_projection(source: exp.Expression) -> bool:
    """Whether ``source`` is ``(SELECT a.c1 [AS n1], a.c2 [AS n2] FROM <one source> AS a) AS d``."""

    if not isinstance(source, exp.Subquery) or not source.alias or _carries(source, _SUBQUERY_ARGS):
        return False
    inner = source.this
    if not isinstance(inner, exp.Select) or _carries(inner, _PROJECTION_ARGS) or len(select_sources(inner)) != 1:
        return False
    qualifiers = {"", select_sources(inner)[0].alias_or_name.lower()}
    for item in inner.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(value, exp.Column) or not isinstance(value.this, exp.Identifier) or value.table.lower() not in qualifiers:
            return False
        if value.args.get("db") or value.args.get("catalog"):
            return False
    return bool(inner.expressions)


def _targets(tree: exp.Expression) -> list[exp.Select]:
    """The selects around a quantified comparison, and the selects that read a renaming projection."""

    found: dict[int, exp.Select] = {}
    for node in tree.find_all(exp.Any, exp.All):
        query = node.this
        while isinstance(query, (exp.Subquery, exp.Paren)):
            query = query.this
        holder = node.find_ancestor(exp.Select)
        if not isinstance(query, exp.Select) or not isinstance(node.parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            continue  # ``= ANY(array)`` and ``LIKE ANY (...)`` are other constructs
        found[id(query)] = query
        if holder is not None:
            found[id(holder)] = holder
    for select in tree.find_all(exp.Select):
        sources = select_sources(select)
        if len(sources) == 1 and _renaming_projection(sources[0]):
            found[id(select)] = select
    return list(found.values())


def _qualify(tree: exp.Expression, known: dict[str, set[str]]) -> exp.Expression:
    with_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for select in _targets(tree):
        if any(select.args.get(k) for k in ("joins", "laterals", "with", "with_")):
            continue
        sources = select_sources(select)
        if len(sources) != 1:
            continue
        source = sources[0]
        columns = _source_columns(source, known, with_names)
        if not columns:
            continue
        alias = source.args.get("alias")
        qualifier = alias.this if isinstance(alias, exp.TableAlias) and alias.this is not None else source.this
        if not isinstance(qualifier, exp.Identifier) or not qualifier.name:
            continue
        reserved = _reserved_names(select) | {qualifier.name.lower()}
        for column in list(select.find_all(exp.Column)):
            if column.args.get("table") or not isinstance(column.this, exp.Identifier):
                continue
            name = column.name.lower()
            if name not in columns or name in reserved:
                continue
            parent = column.parent
            if isinstance(parent, (exp.Dot, exp.PropertyEQ, exp.Kwarg)):
                continue
            if isinstance(parent, exp.Func) and name.upper() in _DATE_PARTS:
                continue
            if not _own_scope(column, select):
                continue
            column.set("table", qualifier.copy())
    return tree
