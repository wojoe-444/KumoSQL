"""A derived table that reads nothing from outside itself is a relation like a table.

``algebraic_equivalence._decorrelated`` reads a correlated scalar aggregate

    x > (SELECT AVG(s.v) * 1.2 FROM s WHERE s.k = outer.k)

as a join with ``(SELECT s.k, AVG(s.v) * 1.2 FROM s GROUP BY s.k)``. Its argument only needs ``s`` to be a relation
whose rows do not depend on the outer row: the subquery's value for an outer row is the aggregate over the rows
of ``s`` with that row's key, which is what the grouped select holds for the key. It accepted base tables only, so
the common shape in which ``s`` is a ``WITH`` table (inlined as a derived table by then)

    WITH totals AS (SELECT store, customer, SUM(amount) AS total FROM returns GROUP BY store, customer)
    SELECT .. FROM totals AS t1 WHERE t1.total > (SELECT AVG(t2.total) * 1.2 FROM totals AS t2 WHERE t2.store = t1.store)

was left as a subquery, which the prover does not read.

``closed_derived_source`` says when a ``FROM`` item of the subquery is such a relation: an aliased derived table
(no ``LATERAL``, no column list, nothing but the body and the alias) whose body is one ``SELECT`` in which every
column, in every select of the body, is read from a source of its own select: qualified by one of that select's
source aliases which has the column, or unqualified and held by exactly one of them. SQL resolves a name in the
nearest enclosing select that has it, so such a column never reaches the outer query, and the body returns the
same rows whichever outer row the subquery is evaluated for.

Everything else is declined: a set operation as the body, a star, a source whose columns are not known (the caller's
``table_columns`` says), two sources with one alias, a name no source of its own select has (an output alias in an
``ORDER BY``, a column of an enclosing select, even one inside the body), or a name two sources have.

The same reading had a second gap. It took the subquery's sources joined by commas only, and a rewrite that
spells the same joins as ``FROM s JOIN d ON s.day = d.day WHERE ..`` was left as a subquery. For an inner join the
``ON`` condition keeps exactly the joined rows on which it is TRUE, as a ``WHERE`` conjunct does, so
``inner_join_conditions`` hands those conditions to ``_decorrelated`` to be read with the ``WHERE`` (a condition
there is either local to the subquery or an equality with an outer column, wherever it is written).
``plain_inner_join`` accepts ``JOIN``, ``INNER JOIN``, ``CROSS JOIN`` and the comma only: an outer, semi, anti,
``NATURAL``, ``ASOF`` or ``USING`` join, or one with any other modifier, is declined.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlglot import exp

from .ast_utils import select_sources


def closed_derived_source(source: exp.Expression, table_columns: Callable[[exp.Expression], list[str] | None]) -> bool:
    """Whether ``source`` is an aliased derived table whose body reads only its own sources."""

    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return False
    if any(value for key, value in source.args.items() if key not in ("this", "alias")) or source.args["alias"].args.get("columns"):
        return False
    body = source.this
    if any(isinstance(n, (exp.Lateral, exp.Unnest, exp.SetOperation)) or (isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count)) for n in body.walk()):
        return False
    for select in body.find_all(exp.Select):
        known: dict[str, set[str]] = {}
        for item in select_sources(select):
            alias = (item.alias_or_name or "").lower()
            columns = table_columns(item)
            if not alias or alias in known or columns is None:
                return False
            known[alias] = {c.lower() for c in columns}
        for column in select.find_all(exp.Column):
            if column.find_ancestor(exp.Select) is not select:
                continue
            if not isinstance(column.this, exp.Identifier) or column.args.get("db"):
                return False
            name = column.name.lower()
            if column.table:
                if name not in known.get(column.table.lower(), ()):
                    return False
            elif sum(name in columns for columns in known.values()) != 1:
                return False
    return True


def plain_inner_join(join: exp.Join) -> bool:
    """``JOIN``, ``INNER JOIN``, ``CROSS JOIN`` or a comma: nothing but the joined source and an optional ``ON``."""

    if any(value for key, value in join.args.items() if key not in ("this", "on", "kind")):
        return False  # LEFT / RIGHT / FULL, NATURAL, ASOF, USING, hints, ...
    return (join.args.get("kind") or "").upper() in ("", "INNER", "CROSS")


def inner_join_conditions(select: exp.Select) -> list[exp.Expression]:
    """The ``ON`` conditions of ``select``'s plain inner joins, which filter the joined rows exactly as ``WHERE`` does."""

    return [join.args["on"] for join in select.args.get("joins") or [] if plain_inner_join(join) and join.args.get("on") is not None]
