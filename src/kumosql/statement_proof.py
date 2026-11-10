"""Prove two ``CREATE TABLE ... AS`` / ``CREATE VIEW ... AS`` statements, and layer the provers for the commands.

A ``CREATE`` statement that writes a query's result is a write whose query is analysed: two such
statements are equivalent when they write the same kind of object under the same name with the same
options, and their queries return the same rows. ``split_create`` separates the two parts; the header
(everything but the query) is compared as text, so any difference in name, ``OR REPLACE``, ``TEMP``,
partitioning, clustering or options keeps the pair unproven.

``prove_statements`` is what the structural command runs: the structural prover first, then the SMT-based
prover (which includes the algebraic normalizer) for what the structure check cannot see. ``prove_statements_smt``
is the same unwrapping in front of the SMT-based prover for the SMT command.
"""

from __future__ import annotations

import dataclasses

import sqlglot
from sqlglot import exp

from .equivalence import EquivalenceResult, EquivalenceStatus, prove_equivalent
from .string_literals import canonical_literals
from .type_names import invalid_type_name

_OBJECTS = {"TABLE", "VIEW"}


def split_create(sql: str) -> tuple[str, str, str] | None:
    """``(object description, header text, query text)`` for ``CREATE [OR REPLACE] TABLE|VIEW name AS query``.

    ``None`` for anything else (functions, procedures, schemas, a ``CREATE`` without a query, a script).
    """

    try:
        parsed = [p for p in sqlglot.parse(sql, read="bigquery") if p is not None]
    except (sqlglot.errors.SqlglotError, RecursionError):
        return None
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Create):
        return None
    create = parsed[0]
    kind = str(create.args.get("kind") or "").upper()
    query = create.expression
    while isinstance(query, exp.Subquery):
        query = query.this
    if kind not in _OBJECTS or not isinstance(query, exp.Query):
        return None
    header = create.copy()
    header.set("expression", exp.Select(expressions=[exp.Literal.number(1)]))
    target = create.this.this if isinstance(create.this, exp.Schema) else create.this
    name = ".".join(part.name for part in target.parts) if isinstance(target, exp.Table) else ""
    description = f"{kind} {name}".strip()
    return description, header.sql(dialect="bigquery"), query.sql(dialect="bigquery")


def _unwrap(left_sql: str, right_sql: str):
    """``(left query, right query, description)``; a pair of different statements gives a reason instead."""

    left, right = split_create(left_sql), split_create(right_sql)
    if left is None and right is None:
        return left_sql, right_sql, None, None
    if left is None or right is None:
        return left_sql, right_sql, None, None
    if left[1] != right[1]:
        return None, None, None, "the statements create different objects, or the same object with different names or options"
    return left[2], right[2], left[0], None


def _smt_available() -> bool:
    try:
        import z3  # noqa: F401
    except ImportError:
        return False
    return True


def prove_statements(left_sql: str, right_sql: str, *, ignore_row_order: bool = True) -> EquivalenceResult:
    """Structural proof first, then the SMT-based prover when row order does not matter."""

    unknown_type = invalid_type_name(left_sql) or invalid_type_name(right_sql)
    if unknown_type:  # the whole statements: a column list of a CREATE TABLE names types the query alone does not
        return EquivalenceResult(EquivalenceStatus.NOT_PROVEN, f"BigQuery would reject the query: {unknown_type}")
    left_query, right_query, description, mismatch = _unwrap(left_sql, right_sql)
    if mismatch:
        return EquivalenceResult(EquivalenceStatus.NOT_PROVEN, mismatch)
    prefix = f"both are CREATE {description} with the same options; " if description else ""
    result = prove_equivalent(left_query, right_query, ignore_row_order=ignore_row_order)
    if not result.proven and ignore_row_order and _smt_available():
        from .algebraic_equivalence import prove_equivalent_algebraic

        try:
            smt = prove_equivalent_algebraic(canonical_literals(left_query), canonical_literals(right_query))
        except Exception:  # noqa: BLE001 - a crash in the solver is a failure to prove, never a proof
            smt = None
        if smt is not None and smt.proven:
            return EquivalenceResult(
                EquivalenceStatus.PROVEN_EQUIVALENT,
                f"{prefix}SMT proof: {smt.reason}",
                diagnostics=tuple(f"assumption: {a}" for a in smt.assumptions),
            )
    if description and result.proven:
        return dataclasses.replace(result, reason=prefix + result.reason)
    return result


def prove_statements_smt(left_sql: str, right_sql: str, **kwargs):
    """The SMT-based proof of two queries, or of two ``CREATE TABLE|VIEW ... AS`` statements' queries."""

    from .algebraic_equivalence import prove_equivalent_algebraic
    from .smt_equivalence import SmtEquivalenceResult, SmtStatus

    unknown_type = invalid_type_name(left_sql) or invalid_type_name(right_sql)
    if unknown_type:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: BigQuery would reject the query: {unknown_type}")
    left_query, right_query, description, mismatch = _unwrap(left_sql, right_sql)
    if mismatch:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, mismatch)
    result = prove_equivalent_algebraic(left_query, right_query, **kwargs)
    if description and result.proven:
        return dataclasses.replace(result, reason=f"both are CREATE {description} with the same options; {result.reason}")
    return result
