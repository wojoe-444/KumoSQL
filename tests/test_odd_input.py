"""Text and schemas the provers cannot read are declined with a result, never with an exception.

Found by fuzzing the public entry points (2026-11-10). Each case below escaped as a raw exception from at least one
of them: ``RecursionError`` from some fifty nested parentheses or a chain of 142 ``UNION ALL`` branches, sqlglot's
``TokenError`` for an unclosed quote where only ``ParseError`` was caught, ``AttributeError`` for the empty name part
sqlglot leaves in ``t..u``, ``decimal.InvalidOperation`` for the number ``1e``, ``ValueError`` for an unterminated
SQLX block, and DuckDB's own errors for a schema it cannot hold.
"""

import pytest
import sqlglot

pytest.importorskip("z3")
pytest.importorskip("duckdb")

from kumosql import bounded_equivalence, check_result_equivalence, prove_equivalent, prove_equivalent_smt, statement_proof, tie_determinism
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.result_equivalence import ResultEquivalenceStatus, query_constants
from kumosql.smt_equivalence import SmtStatus
from kumosql.tie_witness import find_tie_witness

SCHEMA = {"t": ["a", "b"]}
TYPES = {"t": {"a": "INT64", "b": "STRING"}}
PLAIN = "SELECT a FROM t"

DEEP = "SELECT " + "(" * 60 + "1" + ")" * 60
UNREADABLE = {
    "sixty-nested-parentheses": DEEP,
    "unclosed-string": "SELECT '",
    "unclosed-quoted-name": "SELECT `",
    "stray-parenthesis": ")",
    "empty": "",
    "unterminated-sqlx-block": "config {",
    "empty-name-part": "SELECT a FROM t..u",
    "number-with-a-bare-exponent": "SELECT 1e FROM t",
}
DIALECTS = ["bigquery", "mysql", "postgres", "duckdb"]


def _bounded(left, right, dialect="bigquery"):
    return bounded_equivalence.check_bounded(left, right, bounded_equivalence.schema_from_prover(SCHEMA, types=TYPES), dialect=dialect, rows=1)


PROOFS = {
    "structural": lambda left, right, dialect: prove_equivalent(left, right),
    "smt": lambda left, right, dialect: prove_equivalent_smt(left, right, schema=SCHEMA, dialect=dialect),
    "smt-conditional": lambda left, right, dialect: prove_equivalent_smt(left, right, schema=SCHEMA, dialect=dialect, conditional=True),
    "algebraic": lambda left, right, dialect: prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect=dialect),
    "statements": lambda left, right, dialect: statement_proof.prove_statements(left, right),
    "statements-smt": lambda left, right, dialect: statement_proof.prove_statements_smt(left, right),
}


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case", sorted(UNREADABLE))
@pytest.mark.parametrize("prover", sorted(PROOFS))
def test_a_prover_declines_text_it_cannot_read(prover, case, dialect):
    if dialect != "bigquery" and prover in ("structural", "statements", "statements-smt"):
        pytest.skip("this entry point reads BigQuery only")
    result = PROOFS[prover](UNREADABLE[case], PLAIN, dialect)
    assert result.status in (SmtStatus.NOT_PROVEN, SmtStatus.NOT_EQUIVALENT) or result.status.value in ("not_proven", "not_equivalent"), result


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("case", sorted(UNREADABLE))
def test_the_bounded_check_declines_text_it_cannot_read(case, dialect):
    result = _bounded(UNREADABLE[case], PLAIN, dialect)
    assert result.status is not bounded_equivalence.BoundedStatus.BOUNDED_EQUIVALENT, result


@pytest.mark.parametrize("case", sorted(UNREADABLE))
def test_the_executed_check_reports_an_error_for_text_it_cannot_read(case):
    for left, right in ((UNREADABLE[case], PLAIN), (PLAIN, UNREADABLE[case])):
        result = check_result_equivalence(left, right, TYPES, seeds=(1,))
        assert result.status is not ResultEquivalenceStatus.EQUIVALENT, result
    result = check_result_equivalence(UNREADABLE[case], PLAIN, TYPES, seeds=(1,), targeted=True)
    assert result.status is not ResultEquivalenceStatus.EQUIVALENT, result


@pytest.mark.parametrize("case", sorted(UNREADABLE))
def test_the_tie_analysis_and_the_tie_witness_search_do_not_raise(case):
    report = tie_determinism.analyze(UNREADABLE[case], schema=SCHEMA)
    assert report.sites == () or report.unsupported is None
    assert find_tie_witness(UNREADABLE[case], TYPES, budget=2.0) is None
    assert isinstance(query_constants(UNREADABLE[case], PLAIN), dict)


def test_a_long_chain_of_set_operations_is_an_error_not_a_crash():
    """142 branches are as deep for the SQL writer as fifty nested parentheses are for the parser."""

    chain = " UNION ALL ".join([PLAIN] * 142)
    result = check_result_equivalence(chain, chain, TYPES, seeds=(1,))
    assert result.status is ResultEquivalenceStatus.ERROR and "nested too deeply" in result.reason
    assert prove_equivalent_smt(chain, chain, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT  # the provers read it


def test_deep_nesting_is_a_parse_error_for_every_caller_of_the_bigquery_parser():
    """One place turns the interpreter's ``RecursionError`` into the ``ParseError`` callers already handle."""

    with pytest.raises(sqlglot.errors.ParseError, match="nested too deeply"):
        sqlglot.parse_one(DEEP, read="bigquery")
    with pytest.raises(sqlglot.errors.ParseError, match="nested too deeply"):
        sqlglot.parse(DEEP + "; SELECT 1", read="bigquery")
    assert sqlglot.parse_one("SELECT " + "(" * 20 + "1" + ")" * 20, read="bigquery") is not None  # ordinary nesting still parses


CASE_CONFLICT = {"t": {"a": "INT64"}, "T": {"a": "INT64"}}  # two tables in BigQuery, one name in DuckDB
NO_COLUMNS = {"t": {}}


@pytest.mark.parametrize("schema", [CASE_CONFLICT, NO_COLUMNS], ids=["names-differ-only-by-case", "table-without-columns"])
def test_a_schema_duckdb_cannot_hold_is_an_error_result(schema):
    result = check_result_equivalence("SELECT 1", "SELECT 2", schema, seeds=(1,))
    assert result.status is ResultEquivalenceStatus.ERROR, result
    assert find_tie_witness("SELECT 1", schema, budget=2.0) is None
    names = {name: list(columns) for name, columns in schema.items()}
    bounded = bounded_equivalence.check_bounded("SELECT 1", "SELECT 1", bounded_equivalence.schema_from_prover(names, types=schema), rows=1)
    assert bounded.status in (bounded_equivalence.BoundedStatus.UNKNOWN, bounded_equivalence.BoundedStatus.BOUNDED_EQUIVALENT), bounded
    assert prove_equivalent_smt("SELECT 1", "SELECT 2", schema=names, types=schema).status is SmtStatus.NOT_EQUIVALENT


def test_an_empty_name_part_is_declined_by_both_provers_in_every_dialect():
    same = "SELECT a FROM t..u"
    for dialect in DIALECTS:
        for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
            result = prove(same, same, schema=SCHEMA, dialect=dialect)
            assert result.status is SmtStatus.NOT_PROVEN and "empty part" in result.reason, (dialect, prove.__name__, result.reason)
