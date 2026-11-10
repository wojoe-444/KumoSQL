"""Two spellings of a string value that are the value itself.

**A cast of a column to the string type it is declared with.** ``CAST(c AS CHAR(n))`` is ``c`` when ``c`` reads a
column declared ``CHAR(n)`` with that same ``n``, and ``CAST(c AS VARCHAR(n))`` is ``c`` when ``c`` is declared
``VARCHAR(m)`` with ``m <= n``: every value the column can hold already has the target type, so the cast neither
pads nor truncates it, and NULL stays NULL. Calcite writes these casts when it compares a ``CHAR`` column with a
literal (``CAST(ca_state AS CHAR(2)) IN ('GA', 'IL')``).

Everything else keeps its cast, because it can change the value:

* ``CHAR(m)`` to a shorter ``CHAR(n)`` truncates (``CAST(d_day_name AS CHAR(6)) = 'Sunday'`` is also true for
  a ``'Sundays'``), and ``VARCHAR(m)`` to a shorter ``VARCHAR(n)`` does too;
* ``CHAR(m)`` to a longer ``CHAR(n)`` pads, which ``LENGTH``, ``LIKE`` and ``||`` can see;
* ``CHAR`` to ``VARCHAR`` or back changes how trailing blanks compare (PostgreSQL drops them when a ``CHAR``
  becomes a ``VARCHAR``);
* a column whose declared type is not known, an expression, a literal, ``TRY_CAST``.

The declared type is followed through derived tables that pass the column on unchanged
(``algebraic_equivalence._origin_type``), as for the numeric identity casts.

**A prefix.** ``LEFT(x, n)`` is ``SUBSTRING(x, 1, n)`` for an integer literal ``n >= 0``: both are the first ``n``
characters of ``x`` (all of ``x`` when it is shorter, the empty string for ``n = 0``) and NULL when ``x`` is NULL.
A negative ``n`` is kept: PostgreSQL's ``LEFT(x, -2)`` drops the last two characters, while ``SUBSTRING`` with a
negative length is an error there. A length that is not a literal is kept for the same reason.
"""

from __future__ import annotations

import re

from sqlglot import exp

# Dialects where a CHAR(n) or VARCHAR(n) value cast to its own type is known to be returned unchanged.
_DIALECTS = frozenset({"postgres", "redshift", "duckdb", "mysql", "snowflake"})
_INTEGER = re.compile(r"\d+")


def _sized(datatype: exp.Expression | None) -> tuple[str, int] | None:
    """``("CHAR" | "VARCHAR", n)`` for a string type with one explicit length, else None."""

    if not isinstance(datatype, exp.DataType) or datatype.args.get("nested") or datatype.args.get("values"):
        return None
    if datatype.this not in (exp.DataType.Type.CHAR, exp.DataType.Type.VARCHAR):
        return None
    sizes = datatype.expressions
    if len(sizes) != 1 or not isinstance(sizes[0], exp.DataTypeParam):
        return None
    size = sizes[0].this
    if sizes[0].args.get("expression") is not None or not isinstance(size, exp.Literal) or size.is_string or not _INTEGER.fullmatch(size.name):
        return None
    return datatype.this.name, int(size.name)


def _declared(type_sql: str | None, dialect: str) -> tuple[str, int] | None:
    if not type_sql:
        return None
    try:
        return _sized(exp.DataType.build(type_sql, dialect=dialect))
    except Exception:  # noqa: BLE001 - an unreadable type is an unknown type
        return None


def _keeps_every_value(declared: tuple[str, int], target: tuple[str, int]) -> bool:
    if declared[0] != target[0]:
        return False
    if declared[0] == "CHAR":
        return declared[1] == target[1]
    return declared[1] <= target[1]


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def fold_identity_string_cast(cast: exp.Expression, select: exp.Select, types: dict[str, dict[str, str]], dialect: str) -> exp.Expression | None:
    """The column a ``CAST(column AS CHAR(n) | VARCHAR(n))`` reads, when the cast keeps every value of it."""

    from .algebraic_equivalence import _origin_type

    if type(cast) is not exp.Cast or dialect not in _DIALECTS or any(cast.args.get(k) for k in ("format", "safe", "action", "default")):
        return None
    column = cast.this
    if not isinstance(column, exp.Column) or not isinstance(column.this, exp.Identifier):
        return None
    target = _sized(cast.args.get("to"))
    if target is None:
        return None
    declared = _declared(_origin_type(select, column, types), dialect)
    if declared is None or not _keeps_every_value(declared, target):
        return None
    return column.copy()


def left_to_substring(node: exp.Expression) -> exp.Expression | None:
    """``SUBSTRING(x, 1, n)`` for ``LEFT(x, n)`` with an integer literal ``n >= 0``."""

    if type(node) is not exp.Left:
        return None
    length = node.expression
    if not isinstance(length, exp.Literal) or length.is_string or not _INTEGER.fullmatch(length.name):
        return None
    return exp.Substring(this=node.this.copy(), start=exp.Literal.number(1), length=length.copy())


def string_spelling_rules(select: exp.Select, types: dict[str, dict[str, str]] | None, dialect: str = "bigquery") -> exp.Expression | None:
    """Fold identity string casts and spell ``LEFT`` as ``SUBSTRING`` in ``select``'s own scope; None if nothing changed."""

    if not any(_own(n, select) for n in select.find_all(exp.Cast, exp.Left)):
        return None
    copy = select.copy()
    changed = False
    for node in list(copy.find_all(exp.Cast, exp.Left)):
        if not _own(node, copy):
            continue
        replacement = left_to_substring(node)
        if replacement is None and types:
            replacement = fold_identity_string_cast(node, copy, types, dialect)
        if replacement is not None:
            node.replace(replacement)
            changed = True
    return copy if changed else None
