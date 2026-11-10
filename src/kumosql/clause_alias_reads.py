"""What an unqualified name means in QUALIFY and in a window of ORDER BY when a select alias has that name.

``SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1`` has two candidates for ``a``: the output ``b AS a`` and
the column ``t.a``. The engines disagree. Checked on BigQuery (2026-11-10, inline values, zero bytes billed) and on
DuckDB 1.5.6:

* BigQuery reads the **alias** everywhere in QUALIFY (a bare name, a window's argument, its ``PARTITION BY`` and its
  ``ORDER BY``) and in a window inside ORDER BY; it reads the **column** in WHERE and in the select list, windows
  there included;
* DuckDB reads the **column** in all of those places whenever the source has one of that name.

The algebraic prover computes a select's windows in a derived table over its FROM (``_isolate_windows``), where a bare
name can only be a source column. It used to move a QUALIFY window there unchanged, so the BigQuery query above was
read as ranking by ``t.a`` and proven equal to the one that says ``ORDER BY t.a``; BigQuery returns different rows.

:func:`resolve_alias_reads` runs before any window is compared or moved and leaves no such name behind:

* where the alias is the only reading (BigQuery, or the source has no column of that name) the name is replaced by
  the alias's expression, so every bare name left in QUALIFY is a source column or an alias that *is* that column;
* where the engine would read the column, or the columns of the source are not known, the query is refused
  (:class:`~kumosql.ast_utils.UnmodeledConstruct`): unknown beats wrong.

An ORDER BY key outside a window is not touched here: ``smt_equivalence._split_limit`` reads a bare key as the output
of that name (both engines do) and refuses an expression that reads a shadowing alias.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import UnmodeledConstruct, binding_cte, select_sources

_VOLATILE = {"Rand", "Randn", "Uuid"}
_VOLATILE_NAMES = {"RAND", "RANDOM", "GENERATE_UUID", "UUID"}
# Expressions that read the same with or without parentheses around them once they replace a name, and the parents
# under which any other expression needs them (as a function argument or a sort key it does not).
_ATOMS = (exp.Column, exp.Literal, exp.Paren, exp.Window, exp.Null, exp.Boolean)
_OPERATORS = (exp.Binary, exp.Unary, exp.Predicate, exp.Bracket, exp.Dot)


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def _volatile(node: exp.Expression) -> bool:
    return any(
        type(child).__name__ in _VOLATILE or (isinstance(child, exp.Anonymous) and (child.name or "").upper() in _VOLATILE_NAMES)
        for child in node.walk()
    )


def shadowing_aliases(select: exp.Select) -> dict[str, exp.Expression | None]:
    """Lower-cased alias -> its expression, for the select's aliases that are not simply the source column of the
    same name. ``None`` marks a name two outputs share (BigQuery rejects a read of it as ambiguous)."""

    single = len(select_sources(select)) == 1
    names = [item.alias_or_name.lower() for item in select.expressions]
    out: dict[str, exp.Expression | None] = {}
    for item in select.expressions:
        if not isinstance(item, exp.Alias) or not item.alias:
            continue
        name, value = item.alias.lower(), item.this
        if names.count(name) > 1:
            out[name] = None
        elif not (isinstance(value, exp.Column) and value.name.lower() == name and (not value.table or single)):
            out[name] = value
    return out


def _output_names(query: exp.Expression) -> set[str] | None:
    while isinstance(query, (exp.Subquery, exp.SetOperation)):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    names = set()
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)) or not item.alias_or_name:
            return None
        names.add(item.alias_or_name.lower())
    return names


def may_be_source_column(select: exp.Select, name: str, schema: dict | None) -> bool:
    """Whether a source of ``select`` has (or may have) a column called ``name``. True whenever a source's columns
    are not known: an undeclared table, a ``SELECT *``, a table function."""

    known = {str(key).lower(): {str(c).lower() for c in columns} for key, columns in (schema or {}).items()}
    for source in select_sources(select):
        columns = None
        if isinstance(source, exp.Table):
            cte = binding_cte(source)
            if cte is not None:
                columns = _output_names(cte.this)
                declared = cte.args.get("alias")
                if declared is not None and declared.columns:
                    columns = {c.name.lower() for c in declared.columns}
            else:
                parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
                columns = known.get(".".join(parts).lower())
        elif isinstance(source, exp.Subquery):
            columns = _output_names(source.this)
            declared = source.args.get("alias")
            if declared is not None and declared.columns:
                columns = {c.name.lower() for c in declared.columns}
        if columns is None or name in columns:
            return True
    return False


def _reads(select: exp.Select, shadow: dict) -> list[tuple[exp.Column, str, bool]]:
    """``(column, clause, inside a window)`` for the bare names of QUALIFY, and of windows in ORDER BY, that a
    shadowing alias also has."""

    found = []
    for clause, node, windows_only in (("QUALIFY", select.args.get("qualify"), False), ("ORDER BY", select.args.get("order"), True)):
        if node is None:
            continue
        for column in node.find_all(exp.Column):
            if column.table or column.name.lower() not in shadow or not _own(column, select):
                continue
            window = column.find_ancestor(exp.Window)
            inside = window is not None and _own(window, select)
            if inside or not windows_only:
                found.append((column, clause, inside))
    return found


def _replacement(select: exp.Select, name: str, value: exp.Expression, clause: str, inside: bool, taken: set[str], parent) -> exp.Expression:
    if _volatile(value):
        raise UnmodeledConstruct(f"{clause} reads the select alias {name} of a value that changes from call to call")
    if value.find(exp.Select) is not None:
        raise UnmodeledConstruct(f"{clause} reads the select alias {name} of a subquery")
    if inside and value.find(exp.Window) is not None:
        raise UnmodeledConstruct(f"a window in {clause} reads the select alias {name} of another window")
    inserted = value.copy()
    sources = select_sources(select)
    for column in list(inserted.find_all(exp.Column)):
        if column.table or column.name.lower() not in taken:
            continue
        # The inserted text is read as source columns; a bare name that is also an alias would be read as the alias again.
        qualifier = sources[0].alias_or_name if len(sources) == 1 else ""
        if not qualifier:
            raise UnmodeledConstruct(f"{clause} reads the select alias {name}, whose expression reads a column named like an alias")
        qualified = exp.column(column.name, table=qualifier)
        if column is inserted:
            inserted = qualified
        else:
            column.replace(qualified)
    plain = isinstance(inserted, _ATOMS) or (isinstance(inserted, exp.Func) and not isinstance(inserted, _OPERATORS))
    return exp.paren(inserted) if not plain and isinstance(parent, _OPERATORS) else inserted


def resolve_alias_reads(tree: exp.Expression, schema: dict | None = None, dialect: str | None = "bigquery") -> exp.Expression:
    """Replace every bare name in QUALIFY (and in a window of ORDER BY) that means a select alias by the alias's
    expression, in place; refuse the names whose reading depends on the engine or on columns that are not known
    (module doc)."""

    bigquery = (dialect or "bigquery") == "bigquery"
    for select in list(tree.find_all(exp.Select)):
        if select.args.get("qualify") is None and select.args.get("order") is None:
            continue
        shadow = shadowing_aliases(select)
        if not shadow:
            continue
        taken = {item.alias.lower() for item in select.expressions if isinstance(item, exp.Alias) and item.alias}
        for column, clause, inside in _reads(select, shadow):
            name = column.name.lower()
            if not bigquery and may_be_source_column(select, name, schema):
                raise UnmodeledConstruct(f"{clause} reads {name}, which is both a select alias and (possibly) a column of the source")
            value = shadow[name]
            if value is None:
                raise UnmodeledConstruct(f"{clause} reads the select alias {name}, which two outputs share")
            column.replace(_replacement(select, name, value, clause, inside, taken, column.parent))
    return tree
