"""Two exact readings of ``CASE`` arms.

**Arithmetic moves into literal arms.** ``e * CASE WHEN c1 THEN k1 WHEN c2 THEN k2 ELSE k3 END`` is
``CASE WHEN c1 THEN e * k1 WHEN c2 THEN e * k2 ELSE e * k3 END``, and the same for ``+`` and ``-`` with the
``CASE`` on either side. Exactly one arm is chosen either way, and ``e`` combined with that arm's value is the
whole result; with no ``ELSE`` and no arm taken both are NULL (``e * NULL``). So a rate picked by a ``CASE`` and
applied afterwards (``salary * rate``, ``salary * (1 - rate)``) compares with the ``CASE`` that applies it per
arm, and the prover's own literal folding and ``x * 1`` reading then finish the job inside each arm.

The arms must be numeric literals (or ``+ - *`` over literals) of one kind, because a ``CASE`` gives all of its
arms one type. With integer arms that type is the integer type, and with decimal arms the decimal (or, in
BigQuery, ``FLOAT64``) type, so every arm already has it. A mix of integer and decimal arms is accepted only
where decimal literals are exact (MySQL, PostgreSQL, DuckDB): there ``e * 1`` and ``e * 1.00`` are the same
value for an integer, decimal or floating ``e``. In BigQuery ``CASE WHEN c THEN 3 ELSE 0.5 END`` makes the ``3`` a
``FLOAT64``, and ``CAST(e AS FLOAT64) * 3.0`` is not ``CAST(e * 3 AS FLOAT64)`` for an ``INT64`` ``e`` past 2^53, so a
mix is left alone there, and so is any literal with an exponent (a floating literal everywhere).

Not modeled, here or in the SMT prover (which already equates the two forms in these dialects by case
analysis): the width of the common type. DuckDB caps a DECIMAL at 38 places, and its ``CASE`` over a
``DECIMAL(38, 3)`` arm and a ``DECIMAL(38, 4)`` arm (a ``SUM`` times ``2`` and times ``0.5``) keeps scale 3,
rounding the second arm, while the product with the ``CASE`` keeps scale 4.

**An arm that is never taken is dropped.** A searched ``CASE`` arm whose condition is ``NULL``, or a comparison
(``=``, ``<>``, ``<``, ``<=``, ``>``, ``>=``) with a ``NULL`` literal, is never TRUE, so its ``THEN`` is never
returned. The arm is removed only when that ``THEN`` is itself a ``NULL`` literal: any other value would still
take part in the common type of the arms. A ``CASE`` left with no arm is its ``ELSE`` (``CASE WHEN SUM(x) = NULL
THEN NULL ELSE MAX(x) END`` is ``MAX(x)``), or NULL.

Neither reading looks inside nested queries (each select is rewritten on its own), and the moved operand may
hold no subquery, window or random function. Runtime errors are not modeled, as elsewhere in the prover: the
operand is evaluated in one arm instead of before the ``CASE``.
"""

from __future__ import annotations

import re

from sqlglot import exp

# Dialects whose decimal literals are exact DECIMAL/NUMERIC values (``smt_equivalence._EXACT_DECIMAL_LITERALS``).
_EXACT_DECIMALS = frozenset({"mysql", "postgres", "duckdb"})
_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_VOLATILE = (exp.Rand, exp.Randn, exp.Randstr, exp.Uuid, exp.Anonymous, exp.CurrentTimestamp)
_INTEGER = re.compile(r"\d+")
_DECIMAL = re.compile(r"\d+\.\d*|\.\d+")
_MAX_ARMS = 8
_MAX_OPERAND = 240  # characters of SQL; the operand is copied into every arm
_MAX_STEPS = 12


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def _literal_kind(node: exp.Expression) -> str | None:
    """``int`` or ``dec`` for a numeric literal or ``+ - *`` over numeric literals (``dec`` when any literal is
    a decimal: the type the engine gives the value), else ``None``."""

    node = _unwrap(node)
    if type(node) is exp.Literal:
        if node.is_string:
            return None
        if _INTEGER.fullmatch(node.this):
            return "int"
        if _DECIMAL.fullmatch(node.this):
            return "dec"
        return None  # an exponent or another spelling: a floating literal, or one this rule does not read
    if type(node) is exp.Neg:
        return _literal_kind(node.this)
    if type(node) in _ARITHMETIC:
        left, right = _literal_kind(node.this), _literal_kind(node.expression)
        if left is None or right is None:
            return None
        return "dec" if "dec" in (left, right) else "int"
    return None


def _literal_case(node: exp.Expression, dialect: str) -> exp.Case | None:
    """``node`` when it is a ``CASE`` whose arms are numeric literals of one kind."""

    node = _unwrap(node)
    if type(node) is not exp.Case:
        return None
    ifs = node.args.get("ifs") or []
    if not ifs or len(ifs) > _MAX_ARMS or any(type(arm) is not exp.If for arm in ifs):
        return None
    values = [arm.args.get("true") for arm in ifs]
    if node.args.get("default") is not None:
        values.append(node.args["default"])
    kinds = {_literal_kind(value) if value is not None else None for value in values}
    if None in kinds or (len(kinds) > 1 and dialect not in _EXACT_DECIMALS):
        return None
    return node


def _movable(operand: exp.Expression) -> bool:
    if len(operand.sql()) > _MAX_OPERAND:
        return False
    return not any(isinstance(n, (exp.Select, exp.Subquery, exp.Window, exp.Interval, *_VOLATILE)) for n in operand.walk())


def _atom(node: exp.Expression) -> exp.Expression:
    node = node.copy()
    return node if isinstance(node, (exp.Column, exp.Literal, exp.Paren, exp.Func)) else exp.Paren(this=node)


def _distribute(node: exp.Expression, dialect: str) -> exp.Expression | None:
    """``e op CASE`` (or ``CASE op e``) with the operation moved into each arm, or ``None``."""

    left, right = node.this, node.expression
    case, operand, case_first = _literal_case(right, dialect), left, False
    if case is None:
        case, operand, case_first = _literal_case(left, dialect), right, True
    if case is None or not _movable(operand):
        return None
    result = case.copy()

    def combined(value: exp.Expression) -> exp.Expression:
        parts = (_atom(value), _atom(operand)) if case_first else (_atom(operand), _atom(value))
        return type(node)(this=parts[0], expression=parts[1])

    for arm in result.args["ifs"]:
        arm.set("true", combined(arm.args["true"]))
    if result.args.get("default") is not None:
        result.set("default", combined(result.args["default"]))
    return result


def _never_true(condition: exp.Expression) -> bool:
    condition = _unwrap(condition)
    if type(condition) is exp.Null:
        return True
    return type(condition) in _COMPARISONS and (type(_unwrap(condition.this)) is exp.Null or type(_unwrap(condition.expression)) is exp.Null)


def _drop_dead_arms(case: exp.Case) -> exp.Expression | None:
    """``case`` without the arms that are never taken and return NULL, or ``None`` when there is none."""

    if case.args.get("this") is not None:
        return None  # a simple CASE compares its operand; only searched conditions are read here
    ifs = case.args.get("ifs") or []
    if any(type(arm) is not exp.If for arm in ifs):
        return None
    kept = [arm for arm in ifs if not (_never_true(arm.this) and type(_unwrap(arm.args["true"])) is exp.Null)]
    if len(kept) == len(ifs):
        return None
    if kept:
        result = case.copy()
        result.set("ifs", [arm.copy() for arm in kept])
        return result
    default = case.args.get("default")
    return _atom(default) if default is not None else exp.Null()


def rewrite_case_arms(select: exp.Select, dialect: str = "bigquery") -> exp.Expression | None:
    """Apply both readings to ``select``'s own expressions (in place); ``None`` when nothing changes."""

    changed = False
    for _ in range(_MAX_STEPS):
        step = False
        for case in [c for c in select.find_all(exp.Case) if _own(c, select)]:
            reduced = _drop_dead_arms(case)
            if reduced is not None:
                case.replace(reduced)
                step = True
                break
        if not step:
            for node in [n for n in select.find_all(*_ARITHMETIC) if _own(n, select)]:
                moved = _distribute(node, dialect)
                if moved is not None:
                    node.replace(moved)
                    step = True
                    break
        if not step:
            break
        changed = True
    return select if changed else None
