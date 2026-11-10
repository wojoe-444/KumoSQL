"""A column list on a derived table names outputs that have no name of their own.

    FROM (SELECT c.k, COUNT(o.id) FROM c LEFT JOIN o ON .. GROUP BY c.k) AS d (k, n)

``ast_utils.expand_alias_columns`` spells a column list as a select over the source's own output names
(``SELECT kq_renamed.k AS k, kq_renamed.<name> AS n FROM (..) AS kq_renamed``), so it needs every output to have
one. ``COUNT(o.id)`` has none that the SQL text fixes (each engine invents its own), and the query was declined:
"a column list on a table alias could not be resolved" (TPC-H Q13 is written this way).

``name_listed_outputs`` gives each such output a name before the list is expanded:

    FROM (SELECT c.k, COUNT(o.id) AS kq_col1 FROM c LEFT JOIN o ON .. GROUP BY c.k) AS d (k, n)

An alias on a select-list item changes no row and no value. The only thing it changes is the name of that output,
and that name cannot be read from anywhere:

* outside the derived table every output is renamed by the column list, which must have exactly one name per
  output here (a shorter list would leave the engine's invented name readable as ``d.count``);
* inside it, only ``ORDER BY`` (and, in some engines, ``GROUP BY``) can name an output column, and they name it
  through an alias written in the query; a select with an ``ORDER BY`` is declined all the same, since PostgreSQL
  lets ``ORDER BY count`` name an output it called ``count`` itself.

Outputs that already have a name (an alias, or a plain column) are left exactly as written, so every reference
that could name them resolves as before.

Declined (left for ``expand_alias_columns`` to decline or read): a set operation or ``VALUES`` as the body, a
star in the select list, a list with fewer or more names than outputs, an ``ORDER BY`` in the body, an output
already called ``kq_col<i>``, and sources that are not derived tables in a ``FROM`` or ``JOIN``.
"""

from __future__ import annotations

from sqlglot import exp

_PREFIX = "kq_col"


def _named(item: exp.Expression) -> bool:
    if isinstance(item, exp.Alias):
        return isinstance(item.args.get("alias"), exp.Identifier) and bool(item.alias)
    return isinstance(item, exp.Column) and isinstance(item.this, exp.Identifier)


def name_listed_outputs(tree: exp.Expression) -> exp.Expression:
    """``tree`` with the unnamed outputs of every derived table that has a full column list given a name (in place)."""

    for alias in list(tree.find_all(exp.TableAlias)):
        listed = alias.args.get("columns") or []
        source = alias.parent
        if not listed or not isinstance(source, exp.Subquery) or not isinstance(source.parent, (exp.From, exp.Join)):
            continue
        body = source.this
        if not isinstance(body, exp.Select) or body.args.get("order") or len(listed) != len(body.expressions):
            continue
        items = list(body.expressions)
        if any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in items):
            continue
        if all(_named(i) for i in items) or any(_named(i) and i.alias_or_name.lower().startswith(_PREFIX) for i in items):
            continue
        if any(isinstance(i, exp.Alias) and not _named(i) for i in items):
            continue  # an alias that is not a plain identifier: not ours to read
        body.set("expressions", [item if _named(item) else exp.alias_(item, f"{_PREFIX}{index}", copy=False) for index, item in enumerate(items)])
    return tree
