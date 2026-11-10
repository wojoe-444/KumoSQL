"""Qualify a bare column with the source it comes from, in selects that read two or more sources.

``SELECT id, total FROM orders o JOIN customers c ON o.cid = c.id`` becomes
``SELECT o.id, ...`` only where the column has exactly one owner among the select's sources, so the
rewrite never has to guess. Output column names do not change: BigQuery names ``o.total`` ``total``.

Which source owns a column comes from what the query itself says (a CTE or derived table that lists its
columns, an ``UNNEST`` alias) and from the columns the loaded project and saved BigQuery catalog know for
a table (``prover_context.current_schema()``). Without those facts a plain table is unknown.

A select is left alone when it:

- reads fewer than two sources, or any source has unknown columns (a table with no known columns, a table
  function, ``PIVOT``, an unaliased ``UNNEST``, a recursive CTE), or two sources share a name;
- has a ``NATURAL`` join, whose merged columns have no single owner.

Inside a qualifying select a column stays bare when:

- it is a ``USING`` column: after ``JOIN b USING (k)`` the bare ``k`` is the merged value, and ``a.k`` is
  not the same for an outer join;
- two sources have it (the query is ambiguous as written) or none has it (a correlated reference to an
  outer select, a pseudo column such as ``_PARTITIONTIME``);
- it names a source, an ``UNNEST`` value or offset (``FROM UNNEST(xs) AS x`` makes ``x`` the element);
- it names a SELECT alias in GROUP BY, HAVING, QUALIFY or ORDER BY, where the alias takes the name;
- it is in the statement's final ORDER BY, because verification only accepts a rewrite that leaves the
  ordering text exactly as it was;
- it sits inside a star's ``EXCEPT``, ``REPLACE`` or ``RENAME``, where BigQuery reads bare names;
- it belongs to a set operation's own ORDER BY or LIMIT, or to the ORDER BY of a parenthesized query, which name output columns;
- it sits where only some of the sources are readable and its owner is not one of them: a JOIN ... ON reads the sources up to
  and including its own join, and an ``UNNEST`` argument reads only the sources before it (a later source's column of the same
  name means an outer column there; checked on BigQuery);
- it is spelled like an output name that is not a plain column of this select (the field of ``s.f``, a ``* REPLACE`` alias), in
  GROUP BY, HAVING, QUALIFY, ORDER BY or a named window;
- it is an argument of a function and is spelled like a date part (``FOO(d, MONTH)`` parses ``MONTH`` as a column).

A relation whose columns are not certain is unknown: a ``SELECT AS VALUE`` or ``SELECT AS STRUCT`` derived table, a set
operation that matches columns by name, and a table that carries a pivot or another clause beyond an alias, a snapshot or a sample.

The independent check ``proof_qualify`` re-derives every one of these decisions from the SQL alone.

Statements that change data (UPDATE, DELETE, MERGE) are left alone.
"""

from __future__ import annotations

from sqlglot import exp

from kumo_sql_prover.ast_utils import top_level_query
from .engine import RewriteRule, RuleDiagnostic, register_rule

_ALIAS_CLAUSES = ("group", "having", "qualify", "order", "windows")
_VALUE_CLAUSES = ("expressions", "where")
#: The names of date parts: sqlglot reads an unknown function's ``MONTH`` argument as a column.
_DATE_PARTS = frozenset({
    "MICROSECOND", "MILLISECOND", "NANOSECOND", "SECOND", "MINUTE", "HOUR", "DAY", "DAYOFWEEK", "DAYOFYEAR", "WEEK",
    "ISOWEEK", "MONTH", "QUARTER", "YEAR", "ISOYEAR", "DATE", "TIME", "DATETIME", "DECADE", "CENTURY", "MILLENNIUM",
})
_HARMLESS_TABLE_ARGS = frozenset({"this", "alias", "db", "catalog", "comments", "version", "sample", "hints"})


def _schema_columns() -> dict[str, list[str]]:
    try:
        from . import prover_context

        return prover_context.current_schema().columns
    except Exception:  # noqa: BLE001 - no project loaded means no known tables, not a failure
        return {}


class _Source:
    """One relation of a select: the name a qualifier would use, and the columns it is known to have."""

    def __init__(self, qualifier: exp.Identifier | None, columns: list[str] | None, reserved: frozenset[str] = frozenset()) -> None:
        self.qualifier = qualifier
        self.columns = columns  # None: not known
        self.reserved = reserved  # names this source gives that must stay bare (an UNNEST value)

    @property
    def name(self) -> str:
        return (self.qualifier.name if self.qualifier is not None else "").lower()


def _output_names(query: exp.Expression | None) -> list[str] | None:
    """Names a query's result columns carry, or ``None`` when a ``*``, an unnamed expression or a value table hides some."""

    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        if any(query.args.get(key) for key in ("by_name", "on", "side", "kind")):
            return None  # columns matched by name, not position
        return _output_names(query.this)
    if not isinstance(query, exp.Select) or query.args.get("kind"):
        return None  # ``SELECT AS VALUE`` and ``SELECT AS STRUCT`` build a value table, not named columns
    names: list[str] = []
    for item in query.expressions:
        if isinstance(item, exp.Alias):
            name = item.alias
        elif isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
            name = item.name
        else:
            return None
        if not name:
            return None
        names.append(name.lower())
    return names if len(set(names)) == len(names) else None


def _find_cte(table: exp.Table) -> exp.CTE | None | bool:
    """The WITH table a one-part name reads (``None`` when it reads none, ``False`` when it is recursive or unreadable)."""

    if table.args.get("db") is not None or table.args.get("catalog") is not None:
        return None
    wanted = table.name.lower()
    child, parent = table, table.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            ctes = list(parent.expressions)
            if parent.args.get("recursive"):
                if any(c.alias_or_name.lower() == wanted for c in ctes):
                    return False
            else:
                visible = ctes[: next((i for i, c in enumerate(ctes) if c is child), len(ctes))]
                for cte in reversed(visible):
                    if cte.alias_or_name.lower() == wanted:
                        return cte
        else:
            clause = parent.args.get("with_") or parent.args.get("with")
            if isinstance(clause, exp.With) and child is not clause:
                if clause.args.get("recursive"):
                    if any(c.alias_or_name.lower() == wanted for c in clause.expressions):
                        return False
                else:
                    for cte in clause.expressions:
                        if cte.alias_or_name.lower() == wanted:
                            return cte
        child, parent = parent, parent.parent
    return None


def _table_source(table: exp.Table) -> _Source:
    alias = table.args.get("alias")
    carried = {key for key, value in table.args.items() if value not in (None, [], "", False)}
    if carried - _HARMLESS_TABLE_ARGS or not isinstance(table.this, exp.Identifier):
        return _Source(None, None)  # PIVOT, a table function, a nested join or a clause the columns may depend on
    qualifier = alias.this.copy() if alias is not None and alias.this is not None else table.this.copy()
    if alias is not None and alias.args.get("columns"):
        return _Source(qualifier, None)  # GoogleSQL has no column list on a table alias
    cte = _find_cte(table)
    if cte is False:
        return _Source(qualifier, None)
    if cte is not None:
        listed = cte.args["alias"].args.get("columns") if cte.args.get("alias") is not None else None
        names = [c.name.lower() for c in listed] if listed else _output_names(cte.this)
        return _Source(qualifier, names)
    parts = [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]
    known = _schema_columns().get(".".join(parts))
    return _Source(qualifier, list(known) if known else None)


def _sources_of(select: exp.Select) -> list[_Source] | None:
    """The select's relations in order, or ``None`` when it joins ``NATURAL``ly or lists no FROM."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    sources = []
    for relation, join in [(from_.this, None)] + [(j.this, j) for j in select.args.get("joins") or []]:
        if join is not None and (
            (join.args.get("method") or "").upper() == "NATURAL" or (join.args.get("kind") or "").upper() in ("SEMI", "ANTI")
        ):
            return None
        if isinstance(relation, exp.Table):
            sources.append(_table_source(relation))
        elif isinstance(relation, exp.Subquery) and relation.alias:
            carried = {key for key, value in relation.args.items() if value not in (None, [], "", False)}
            alias = relation.args["alias"]
            if carried - {"this", "alias", "comments"} or alias.args.get("columns"):
                sources.append(_Source(None, None))  # a pivot, a sample or a column list on the derived table
            else:
                sources.append(_Source(alias.this.copy(), _output_names(relation.this)))
        elif isinstance(relation, exp.Unnest) and relation.alias_column_names:
            # ``UNNEST(xs) AS x``: the alias names the element, so it is a column, not a qualifier.
            offset = relation.args.get("offset")
            offset_name = offset.name.lower() if isinstance(offset, exp.Identifier) else ""
            reserved = frozenset(n for n in (*(c.lower() for c in relation.alias_column_names), offset_name) if n)
            sources.append(_Source(relation.args["alias"].columns[0].copy(), [], reserved))
        else:
            sources.append(_Source(None, None))
    return sources


def _using_names(select: exp.Select) -> set[str]:
    return {i.name.lower() for join in select.args.get("joins") or [] for i in join.args.get("using") or []}


def _owner_select(column: exp.Column) -> exp.Select | None:
    """The select whose scope reads ``column``; ``None`` for a column of a set operation's own ORDER BY or LIMIT,
    of a parenthesized query's ORDER BY or LIMIT, or inside a lambda or a PIVOT."""

    child, node = column, column.parent
    while node is not None:
        if isinstance(node, exp.Select):
            return node
        if isinstance(node, (exp.SetOperation, exp.Lambda, exp.Pivot)):
            return None
        if isinstance(node, exp.Subquery) and child.arg_key != "this":
            return None
        child, node = node, node.parent
    return None


def _in_star_names(column: exp.Column) -> bool:
    node = column.parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, exp.Star):
            return True
        node = node.parent
    return False


def _place(column: exp.Column, select: exp.Select, count: int) -> tuple[str, int] | None:
    """The clause of ``select`` that holds ``column`` and how many of its leading sources it can read.

    Returns ``None`` where no source can be named: inside a table's own clause (a snapshot), or in the
    first relation's ``UNNEST`` argument. A JOIN ... ON reads the sources up to its own join, and a joined
    ``UNNEST`` reads only the sources before it.
    """

    below, top = None, column
    while top.parent is not None and top.parent is not select:
        below, top = top, top.parent
    if top.parent is not select:
        return None
    key = top.arg_key
    if key in _VALUE_CLAUSES or key in _ALIAS_CLAUSES:
        return key, count
    if key == "joins" and below is not None:
        index = next((i for i, join in enumerate(select.args.get("joins") or []) if join is top), None)
        if index is None:
            return None
        if below.arg_key == "on":
            return "on", index + 2
        if below.arg_key == "this" and isinstance(below, exp.Unnest):
            return "from", index + 1
    return None


def _blocked_names(select: exp.Select) -> set[str]:
    """Names that GROUP BY, HAVING, QUALIFY, ORDER BY and a named window may read as an output of ``select``.

    Every select alias, every ``* REPLACE`` or ``RENAME`` name, and the name of any item that is not a bare column (the
    field of ``s.f``, a path): BigQuery prefers these over a source column of the same name there.
    """

    names: set[str] = set()
    for item in select.expressions:
        value = item
        while isinstance(value, exp.Paren):
            value = value.this
        if isinstance(item, exp.Alias):
            names.add(item.alias.lower())
        elif isinstance(value, exp.Column) and not isinstance(value.this, exp.Star):
            if value.table:
                names.add(value.name.lower())  # ``t.y`` outputs ``y``; a bare ``y`` is the column itself
        elif isinstance(value, exp.Dot):
            names.add(value.text("expression").lower())
        for node in value.find_all(exp.Star):
            for part in (*(node.args.get("replace") or []), *(node.args.get("rename") or [])):
                names.update(identifier.name.lower() for identifier in part.find_all(exp.Identifier))
    return names


def _qualify_select(select: exp.Select, columns_of: dict[int, list[exp.Column]], final: exp.Expression | None) -> int:
    sources = _sources_of(select)
    if sources is None or len(sources) < 2 or any(s.qualifier is None or s.columns is None for s in sources):
        return 0
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        return 0
    skip = _using_names(select) | set(names) | {n for s in sources for n in s.reserved}
    blocked = _blocked_names(select)
    changed = 0
    for column in columns_of.get(id(select), []):
        name = column.name.lower()
        if name in skip or _in_star_names(column):
            continue
        place = _place(column, select, len(sources))
        if place is None:
            continue
        clause, readable = place
        if name in blocked and clause in _ALIAS_CLAUSES:
            continue
        if clause == "order" and select is final:
            continue
        if isinstance(column.parent, exp.Func) and name.upper() in _DATE_PARTS:
            continue
        owners = [i for i, s in enumerate(sources) if name in s.columns]
        if len(owners) != 1 or owners[0] >= readable:
            continue
        column.set("table", sources[owners[0]].qualifier.copy())
        changed += 1
    return changed


@register_rule
class QualifyColumnsRule(RewriteRule):
    """Add the source name to a bare column that exactly one source of a multi-source select owns."""

    name = "qualify_columns"
    summary = "Qualify bare columns with their source in selects that read two or more sources"
    opt_in = True

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        # DML rewrites are not proven, so they are left alone.
        if isinstance(statement, (exp.Update, exp.Delete, exp.Merge)):
            return 0, []
        columns_of: dict[int, list[exp.Column]] = {}
        for column in statement.find_all(exp.Column):
            if column.table or isinstance(column.this, exp.Star) or not isinstance(column.this, exp.Identifier):
                continue
            owner = _owner_select(column)
            if owner is not None:
                columns_of.setdefault(id(owner), []).append(column)
        final = top_level_query(statement)
        return sum(_qualify_select(select, columns_of, final) for select in list(statement.find_all(exp.Select))), []
