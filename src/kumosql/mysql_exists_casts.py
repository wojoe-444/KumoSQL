"""On MySQL, ``CAST(EXISTS(...) AS SIGNED)`` in the query's output is the ``EXISTS`` itself.

MySQL has no boolean type: ``EXISTS (subquery)`` evaluates to the integer 1 or 0 and is never
NULL, and ``CAST(.. AS SIGNED)`` of an integer is that integer. Calcite prints ``CAST(<condition>
AS BOOLEAN)`` around a rewritten quantified comparison, which read as MySQL is ``CAST(<condition>
AS SIGNED)``; once the quantified read-back (``kumosql.nonnull_any``) has turned the condition into
an ``EXISTS``, the cast is all that separates it from the query it was rewritten from.

This is a fact about MySQL only: in BigQuery ``CAST(TRUE AS INT64)`` is the number 1 and not the
boolean TRUE, so every other dialect is left alone. It is applied only to a whole item of the
outermost select's list, where the value is the query's output and nothing else reads it. A cast
inside arithmetic, a comparison, GROUP BY or a derived table keeps its spelling, and so does a
cast of any other operand: a comparison, ``IS``, ``IN``, ``NOT EXISTS``, a column. Those are 1, 0
or NULL on MySQL as well, but ``tests/test_mysql_boolean_outputs.py`` records the decision not to
identify ``x = 1`` with ``CAST(x = 1 AS SIGNED)`` by default, and this module does not revisit it.
"""

from __future__ import annotations

from sqlglot import exp

# CAST(x AS SIGNED) is read as BIGINT; INT holds 0 and 1 as well.
_INTEGER_TARGETS = {exp.DataType.Type.BIGINT, exp.DataType.Type.INT}


def drop_mysql_exists_cast(select: exp.Select, dialect: str) -> exp.Expression | None:
    """``SELECT CAST(EXISTS(q) AS SIGNED)`` is ``SELECT EXISTS(q)`` in the outermost select (MySQL only)."""

    if dialect != "mysql" or select.parent is not None:
        return None
    if any(select.args.get(k) for k in ("group", "having", "qualify", "windows")):
        return None  # GROUP BY may name the item by its text, which must keep matching
    changed = False
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if type(value) is not exp.Cast:  # TRY_CAST is a subclass with other semantics
            continue
        target = value.args.get("to")
        if (
            not isinstance(target, exp.DataType)
            or target.this not in _INTEGER_TARGETS
            or target.expressions
            or any(value.args.get(k) for k in ("format", "safe", "action", "default"))
        ):
            continue
        operand = value.this
        while isinstance(operand, exp.Paren):
            operand = operand.this
        # the subquery form only: Spark's EXISTS(array, lambda) is NULL over a NULL array
        if not isinstance(operand, exp.Exists) or operand.expression is not None or not isinstance(operand.this, (exp.Select, exp.Subquery, exp.SetOperation)):
            continue
        value.replace(operand.copy())
        changed = True
    return select if changed else None
