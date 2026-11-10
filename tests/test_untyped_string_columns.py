"""A column the schema does not type, compared with different strings that MySQL reads as the same number (issue #613).

``WHERE t.s = 'x' AND t.s = 'y'`` is empty for a text column and returns the rows where ``t.s`` is 0 for an integer column,
since MySQL compares a number with a string as numbers and reads ``'x'`` and ``'y'`` as 0. The provers read an untyped column as
text, so they proved the pair empty. On MySQL a proof now has to hold with those strings replaced by the number they convert to
(``kumosql.numeric_column_reading``). Other dialects refuse the comparison of an integer column with such a string, so they are
unchanged.
"""

import pytest

pytest.importorskip("z3")

from kumosql import numeric_column_reading, string_number_compare
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

PROVERS = (prove_equivalent_algebraic, prove_equivalent_smt)
NONE = "SELECT t.k FROM t WHERE FALSE"

# MySQL 8.0 returns the row t.s = 0, t.k = 1 of an INT column for each left query and none for the right
WITNESSES = [
    ("SELECT t.k FROM t WHERE t.s = 'x' AND t.s = 'y'", NONE),
    ("SELECT t.k FROM t WHERE t.s = 'a1' AND t.s = 'a2'", NONE),
    ("SELECT t.k FROM t WHERE t.s = 'x' AND t.s <> 'y'", "SELECT t.k FROM t WHERE t.s = 'x'"),
    ("SELECT t.k FROM t JOIN u ON t.k = u.k WHERE t.s = 'x' AND t.s = u.s AND u.s = 'y'", NONE),
    ("SELECT t.k FROM t WHERE t.s IN ('x', 'y') AND t.s NOT IN ('x', 'z')", NONE),
    ("SELECT t.k FROM t WHERE t.s BETWEEN 'x' AND 'y' AND t.s = 'z'", NONE),
]

# proofs that hold for a text column and for an integer column alike
NEAR_MISSES = [
    ("SELECT t.k FROM t WHERE t.s IN ('x', 'y')", "SELECT t.k FROM t WHERE t.s = 'x' OR t.s = 'y'"),
    ("SELECT t.k FROM t WHERE t.s = 'x' AND t.s = 'x'", "SELECT t.k FROM t WHERE t.s = 'x'"),
    ("SELECT t.k FROM t WHERE t.s = 'x' OR t.s = 'y'", "SELECT t.k FROM t WHERE t.s = 'y' OR t.s = 'x'"),
    ("SELECT t.k FROM t WHERE t.s = 'x'", "SELECT t.k FROM t WHERE t.s = 'x' AND TRUE"),
    # one string per column, or strings that no integer column reads alike ('1' is 1, 'x' is 0)
    ("SELECT t.k FROM t WHERE t.s = '1' AND t.s = 'x'", "SELECT t.k FROM t WHERE t.s = 'x' AND t.s = '1'"),
    (
        "SELECT t.k FROM t JOIN u ON t.k = u.k WHERE t.s = 'x' AND u.s = 'y'",
        "SELECT t.k FROM t JOIN u ON t.k = u.k WHERE u.s = 'y' AND t.s = 'x'",
    ),
]


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", WITNESSES)
def test_witness_is_not_proven_on_mysql(prover, left, right):
    result = prover(left, right, dialect="mysql")
    assert not result.proven, (left, result.reason)


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", NEAR_MISSES)
def test_proofs_that_do_not_need_the_strings_to_differ_stay(prover, left, right):
    assert prover(left, right, dialect="mysql").status is SmtStatus.PROVEN_EQUIVALENT, left


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("left,right", WITNESSES[:4])
def test_other_dialects_are_unchanged(prover, left, right):
    for dialect in ("duckdb", "postgres", "bigquery"):
        assert prover(left, right, dialect=dialect).status is SmtStatus.PROVEN_EQUIVALENT, (dialect, left)


@pytest.mark.parametrize("prover", PROVERS)
def test_a_declared_text_column_keeps_the_proof(prover):
    types = {"t": {"s": "VARCHAR", "k": "INT"}}
    left, right = WITNESSES[0]
    assert prover(left, right, dialect="mysql", types=types).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("prover", PROVERS)
def test_a_declared_integer_column_is_not_proven(prover):
    types = {"t": {"s": "INT", "k": "INT"}}
    left, right = WITNESSES[0]
    assert not prover(left, right, dialect="mysql", types=types).proven


def test_mysql_reads_the_leading_number():
    number = string_number_compare.mysql_number
    assert [number(text) for text in ("x", "", "abc1", "1abc", " 12", "-3x", "+4", "1.5", ".5e1", "1e2z", "0x1A")] == [0, 0, 0, 1, 12, -3, 4, 1.5, 5, 100, 0]


def test_the_reading_replaces_the_strings_by_numbers():
    found = numeric_column_reading.reading("SELECT t.k FROM t WHERE t.s = 'x' AND t.s = 'y1' AND t.s = '-2z'", NONE, "mysql")
    assert found.pair[0] == "SELECT t.k FROM t WHERE t.s = 0 AND t.s = 0 AND t.s = -2"
    assert found.pair[1] == NONE


def test_a_string_with_no_exact_small_integer_is_declined():
    found = numeric_column_reading.reading("SELECT t.k FROM t WHERE t.s = '1.5' AND t.s = '1.5x'", NONE, "mysql")
    assert found.problem and found.pair is None


@pytest.mark.parametrize("dialect", ("duckdb", "postgres", "bigquery", "snowflake"))
def test_only_mysql_is_checked(dialect):
    assert numeric_column_reading.reading(*WITNESSES[0], dialect) is None


THROUGH_DERIVED = (
    "SELECT c FROM (SELECT s AS c FROM t) AS d WHERE c = 'x' AND c = 'y'",
    "SELECT c FROM (SELECT s AS c FROM t) AS d WHERE FALSE",
)


@pytest.mark.parametrize("prover", PROVERS)
def test_a_declared_text_column_read_by_a_bare_name_through_a_derived_table_keeps_the_proof(prover):
    """``c`` is never written ``d.c``; it is still the derived table's output, a declared VARCHAR (VeriEQL Calcite pair 147)."""

    types = {"t": {"s": "VARCHAR", "k": "INT"}}
    assert prover(*THROUGH_DERIVED, dialect="mysql", types=types).status is SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("types", [None, {"t": {"s": "INT", "k": "INT"}}, {"u": {"s": "VARCHAR"}}])
def test_an_untyped_or_integer_column_read_through_a_derived_table_is_not_proven(prover, types):
    assert not prover(*THROUGH_DERIVED, dialect="mysql", types=types).proven


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("types", [{"t": {"s": "VARCHAR", "k": "INT"}}, {"t": {"s": "VARCHAR", "k": "INT"}, "u": {"s": "INT"}}])
def test_a_bare_name_read_from_two_derived_tables_is_not_tied_to_the_typed_one(prover, types):
    """The second branch reads ``c`` from ``u``, which is untyped or an integer: MySQL returns its rows where ``u.s`` is 0."""

    typed = "SELECT c FROM (SELECT s AS c FROM t) AS d WHERE c = 'a'"
    left = f"{typed} UNION ALL SELECT c FROM (SELECT s AS c FROM u) AS e WHERE c = 'x' AND c = 'y'"
    assert not prover(left, typed, dialect="mysql", types=types).proven


def test_a_bare_name_is_tied_only_when_one_source_can_hold_it():
    import sqlglot

    types = {"t": {"s": "VARCHAR", "k": "INT"}}

    def tied(sql):
        kinds = string_number_compare._Kinds(sqlglot.parse_one(sql, read="mysql"), types)
        return kinds.find(("", "c")) == kinds.find(("d", "c"))

    assert tied("SELECT c FROM (SELECT s AS c FROM t) AS d WHERE c = 'x'")
    assert not tied("SELECT c FROM (SELECT s AS c FROM t) AS d, u WHERE c = 'x'")  # u's columns are not known
    assert not tied("SELECT k AS c FROM (SELECT s AS c, k FROM t) AS d ORDER BY c")  # ORDER BY reads the select alias
    assert not tied("SELECT 1 FROM (SELECT s AS c FROM t) AS d WHERE EXISTS (SELECT 1 FROM t WHERE c = 'x')")  # read from the enclosing query


@pytest.mark.parametrize("prover", PROVERS)
@pytest.mark.parametrize("types", [{"u": {"s": "VARCHAR"}}, {"t": {"k": "INT"}, "u": {"s": "VARCHAR"}}])
def test_another_tables_declared_type_does_not_type_a_bare_column(prover, types):
    """``s`` is read from ``t``, which the schema does not type (or types without ``s``); ``u.s VARCHAR`` says nothing
    about it. Both provers once borrowed that type for the bare name and proved the pair, which MySQL separates for an
    integer ``t.s`` that holds 0."""

    assert not prover("SELECT k FROM t WHERE s = 'x' AND s = 'y'", "SELECT k FROM t WHERE FALSE", dialect="mysql", types=types).proven
