"""What a BigQuery decimal literal is worth next to a NUMERIC or BIGNUMERIC operand (for the SMT prover).

BigQuery reads ``0.1`` as a FLOAT64, but a *literal* takes the type of the operands it is combined with: next to a
NUMERIC operand it is the NUMERIC nearest its decimal text (nine decimal digits, half away from zero), next to a
BIGNUMERIC operand the BIGNUMERIC nearest it (38 digits), and only otherwise the nearest double. Checked on BigQuery
(2026-11-10, zero bytes billed) for ``=``, ``<``, ``IN``, ``BETWEEN``, ``+ - *``, ``COALESCE``, ``IF``, ``IFNULL``,
``NULLIF``, a simple ``CASE``, ``GREATEST``, ``SAFE_DIVIDE`` and the columns of a ``UNION ALL``:

* ``NUMERIC '0.1' = 0.1`` is TRUE, and so is ``NUMERIC '0.1' = 0.1000000001`` (the literal rounds to nine digits);
* ``BIGNUMERIC '0.1' = 0.10000000000000001`` is FALSE although both literals are the same double;
* ``-(-0.1)``, ``(0.1)`` and ``1e-1`` are literals too; ``0.1 + 0.2``, ``CAST(0.1 AS FLOAT64)`` and ``(SELECT 0.1)``
  are FLOAT64 expressions, and the NUMERIC operand is converted to FLOAT64 instead.

So the three readings of one literal are different numbers unless its text is a short decimal that is also exactly a
double (``0.5``, ``2.25``, ``1e3``). This module computes the readings and names the operands a literal is typed with;
``smt_equivalence`` picks the reading from the types of those operands, and keeps every reading open when a type is
not known. Before this module the prover always used the double, which proved ``WHERE n = 0.1`` empty for a NUMERIC
``n`` and ``b = 0.1`` equal to ``b = 0.10000000000000001`` for a BIGNUMERIC ``b``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from fractions import Fraction

from sqlglot import exp

from . import smt_values as S

# The numeric kinds, ordered as BigQuery widens them: the widest operand decides what a literal becomes.
INT64, NUMERIC, BIGNUMERIC, FLOAT64 = 0, 1, 2, 3
KIND = {S.INT64: INT64, S.NUMERIC: NUMERIC, S.BIGNUMERIC: BIGNUMERIC, S.FLOAT64: FLOAT64}

_NUMERIC_LIMIT = Fraction(10) ** (S.NUMERIC_DIGITS - S.NUMERIC_SCALE)
_BIGNUMERIC_LIMIT = Fraction(S.BIGNUMERIC_LIMIT, 10**S.BIGNUMERIC_SCALE)


def _rounded(text: str, places: int) -> Fraction:
    with localcontext() as context:
        context.prec = 500
        return Fraction(Decimal(text).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def readings(text: str, negate: bool = False) -> tuple[Fraction, Fraction | None, Fraction | None]:
    """``(FLOAT64, NUMERIC, BIGNUMERIC)`` values of the decimal or exponent literal ``text``.

    A reading is ``None`` when the literal does not fit that type (BigQuery then rejects the query or compares as
    FLOAT64; neither is modelled). Raises :class:`smt_values.NotModeled` when the literal has no FLOAT64 value."""

    floating = S.literal_value(text, negate)
    try:
        numeric, bignumeric = _rounded(text, S.NUMERIC_SCALE), _rounded(text, S.BIGNUMERIC_SCALE)
    except (InvalidOperation, ValueError) as error:
        raise S.NotModeled(f"numeric literal {text[:40]!r}") from error
    if negate:
        numeric, bignumeric = -numeric, -bignumeric
    return (
        floating,
        numeric if abs(numeric) < _NUMERIC_LIMIT else None,
        bignumeric if abs(bignumeric) < _BIGNUMERIC_LIMIT else None,
    )


def literal_leaf(node) -> tuple[exp.Literal, bool] | None:
    """``(literal, negated)`` when ``node`` is a decimal or exponent literal under any parentheses and minus signs
    (BigQuery still treats ``-(-0.1)`` as a literal), else ``None``. Integer literals are not included: an INT64
    literal is the same number in every numeric type."""

    negate = False
    while isinstance(node, (exp.Paren, exp.Neg)):
        negate ^= isinstance(node, exp.Neg)
        node = node.this
    if isinstance(node, exp.Literal) and not node.is_string and not S.is_integer_text(node.this):
        return node, negate
    return None


def unstable(node) -> bool:
    """Whether ``node`` is a decimal literal whose value depends on the type it is converted to."""

    leaf = literal_leaf(node)
    if leaf is None:
        return False
    try:
        floating, numeric, bignumeric = readings(leaf[0].this, leaf[1])
    except S.NotModeled:
        return True
    return not floating == numeric == bignumeric


_BINARY = (
    exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.NullSafeEQ, exp.NullSafeNEQ,
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod,
)
# Calls whose arguments are not typed together (a cast names its own type; the rest take one value).
_NOT_TYPED_TOGETHER = (exp.Cast, exp.TryCast, exp.Paren, exp.Neg, exp.Alias, exp.Subquery, exp.Predicate, exp.Connector, exp.Not)


def _arguments(node) -> list:
    out = []
    for value in node.args.values():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, exp.Expression):
                out.append(item)
    return out


def groups(node) -> list[list]:
    """The operand lists of ``node`` whose members BigQuery brings to one common type: both sides of a comparison or
    of ``+ - * /``, the value and the items of ``IN``, the three operands of ``BETWEEN``, the results of ``CASE``,
    ``IF``, ``COALESCE`` and ``NULLIF``, the operand and the ``WHEN`` values of a simple ``CASE``, and the arguments of
    any other function call. Empty for every other node."""

    if isinstance(node, _BINARY):
        return [[node.this, node.expression]]
    if isinstance(node, exp.In):
        return [[node.this, *node.expressions]]
    if isinstance(node, exp.Between):
        return [[node.this, node.args["low"], node.args["high"]]]
    if isinstance(node, exp.Case):
        ifs = node.args.get("ifs") or []
        default = node.args.get("default")
        results = [[b.args["true"] for b in ifs] + ([default] if default is not None else [])]
        operand = node.args.get("this")
        return results + ([[operand, *[b.this for b in ifs]]] if operand is not None else [])
    if isinstance(node, exp.If):
        other = node.args.get("false")
        return [[node.args["true"]] + ([other] if other is not None else [])]
    if isinstance(node, exp.Coalesce):
        return [[node.this, *node.expressions]]
    if isinstance(node, exp.Nullif):
        return [[node.this, node.expression]]
    if isinstance(node, exp.Func) and not isinstance(node, _NOT_TYPED_TOGETHER):
        arguments = _arguments(node)
        return [arguments] if len(arguments) > 1 else []
    return []
