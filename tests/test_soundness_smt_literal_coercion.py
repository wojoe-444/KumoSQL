"""A BigQuery decimal literal takes the type of the NUMERIC or BIGNUMERIC operand next to it.

The prover used to read every decimal literal as the nearest FLOAT64. BigQuery converts a *literal* to the type of
the operands it is typed with, so ``NUMERIC '0.1' = 0.1`` is TRUE and ``BIGNUMERIC '0.1' = 0.10000000000000001`` is
FALSE although both literals are one double. Every expectation below was run on BigQuery on 2026-11-10 with inline
values (zero bytes billed); ``docs/provers.md`` lists the queries' results.
"""

from fractions import Fraction

import pytest

pytest.importorskip("z3")

from sqlglot import parse_one  # noqa: E402

from kumosql import smt_literal_coercion as C  # noqa: E402
from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt  # noqa: E402

SCHEMA = {"t": ["k", "n", "b", "f", "i"], "u": ["k", "m"]}
TYPES = {"t": {"k": "INT64", "n": "NUMERIC", "b": "BIGNUMERIC", "f": "FLOAT64", "i": "INT64"}, "u": {"k": "INT64", "m": "NUMERIC"}}
W = "SELECT k FROM t WHERE "

# Pairs BigQuery answers differently on some table: no prover may call them equivalent, typed or not.
DIFFERENT = [
    # reported by the Kumo_SQL_Prover bug hunt: the literal never met the nine-digit NUMERIC value
    (W + "n = 0.1", W + "1 = 0"),
    (W + "n > 0.1", W + "n >= 0.1"),
    (W + "n IN (0.1, 0.2)", W + "1 = 0"),
    (W + "n BETWEEN 0.1 AND 0.1", W + "1 = 0"),
    # two literals that are one double are two BIGNUMERIC values
    (W + "b = 0.1", W + "b = 0.10000000000000001"),
    (W + "b > 0.1", W + "b > 0.10000000000000001"),
    (W + "b IN (0.1)", W + "b IN (0.10000000000000001)"),
    (W + "b - 0.1 = 0", W + "b - 0.10000000000000001 = 0"),
    (W + "CASE b WHEN 0.1 THEN 1 ELSE 0 END = 1", W + "CASE b WHEN 0.10000000000000001 THEN 1 ELSE 0 END = 1"),
    (W + "NULLIF(b, 0.1) IS NULL", W + "NULLIF(b, 0.10000000000000001) IS NULL"),
    (W + "COALESCE(b, 0.1) = 0.1", W + "COALESCE(b, 0.10000000000000001) = 0.1"),
    (W + "GREATEST(b, 0.1) = b", W + "GREATEST(b, 0.10000000000000001) = b"),
    (W + "b = 1e-1", W + "b = 10000000000000001e-17"),
    ("SELECT b FROM t UNION ALL SELECT 0.1", "SELECT b FROM t UNION ALL SELECT 0.10000000000000001"),
    ("SELECT SUM(b) AS s FROM t HAVING SUM(b) > 0.1", "SELECT SUM(b) AS s FROM t HAVING SUM(b) > 0.10000000000000001"),
    # a FLOAT64 that is not a literal converts the other operand instead: a BIGNUMERIC within half an ulp matches
    (W + "b = 0.1 + 0.2", W + "1 = 0"),
    (W + "b < 0.5 + 0.25", W + "b < 0.75"),
    (W + "b IN (SELECT 0.5 FROM u)", W + "b = 0.5 AND EXISTS (SELECT 1 FROM u)"),
    (W + "n IN (SELECT 0.1 FROM u)", W + "1 = 0"),
    # a function whose signature is not modelled: the readings of two different literals are not assumed equal
    (W + "IEEE_DIVIDE(n, 0.1) > 1", W + "IEEE_DIVIDE(n, 0.1000000001) > 1"),
]


@pytest.mark.parametrize("left,right", DIFFERENT)
@pytest.mark.parametrize("types", [TYPES, None], ids=["typed", "untyped"])
def test_no_prover_calls_them_equivalent(left, right, types):
    for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
        result = prove(left, right, schema=SCHEMA, types=types)
        assert not result.proven, (prove.__name__, result.reason)


# Pairs that are equivalent on BigQuery once the column types are declared, and stay proven.
EQUIVALENT_TYPED = [
    (W + "n = 0.1", W + "n = 0.1000000001"),  # both literals round to the NUMERIC 0.1
    (W + "n = 0.1", W + "n = 0.10000000000000001"),
    (W + "n BETWEEN 0.1 AND 0.2", W + "n >= 0.1 AND n <= 0.2"),
    (W + "n BETWEEN 0.1 AND 0.1", W + "n = 0.1"),
    (W + "CASE n WHEN 0.1 THEN 1 ELSE 0 END = 1", W + "n = 0.1"),
    (W + "f = 0.1", W + "f = 0.10000000000000001"),  # one double
    (W + "i > 0.1", W + "i >= 1"),
    (W + "b > 0.1", W + "0.1 < b"),
    (W + "b = 0.5", W + "b = 0.50"),
    ("SELECT SUM(n) AS s FROM t HAVING SUM(n) > 0.1", "SELECT SUM(n) AS s FROM t HAVING SUM(n) > 0.1000000001"),
]


@pytest.mark.parametrize("left,right", EQUIVALENT_TYPED)
def test_typed_equivalences_stay_proven(left, right):
    assert prove_equivalent_smt(left, right, schema=SCHEMA, types=TYPES).proven


# With no declared types a proof has to hold for every typing; these hold for all of them.
EQUIVALENT_UNTYPED = [
    (W + "n > 0.1", W + "0.1 < n"),
    (W + "n > 0.1 AND k = 1", W + "k = 1 AND n > 0.1"),
    (W + "n > 0.1 AND n > 0.2", W + "n > 0.2"),
    (W + "n BETWEEN 0.1 AND 0.2", W + "n >= 0.1 AND n <= 0.2"),
    (W + "n * 0.1 > 1", W + "0.1 * n > 1"),
    ("SELECT n * 0.9 AS y FROM t", "SELECT 0.9 * n AS y FROM t"),
    ("SELECT SUM(n) AS s FROM t HAVING SUM(n) > 0.1", "SELECT SUM(n) AS s FROM t HAVING 0.1 < SUM(n)"),
    (W + "n = 0.5", W + "n = 0.50"),
]


@pytest.mark.parametrize("left,right", EQUIVALENT_UNTYPED)
def test_untyped_equivalences_stay_proven(left, right):
    assert prove_equivalent_smt(left, right, schema=SCHEMA).proven


def test_untyped_literals_of_one_double_are_not_decided():
    # equal for a FLOAT64 or NUMERIC column, different for a BIGNUMERIC one: neither a proof nor a counterexample
    result = prove_equivalent_smt(W + "n = 0.1", W + "n = 0.10000000000000001", schema=SCHEMA)
    assert result.status is SmtStatus.NOT_PROVEN


def test_untyped_counterexamples_read_the_column_as_float64():
    # the database is shown as plain numbers, so it must separate the queries when the column is FLOAT64
    result = prove_equivalent_smt(W + "n > 0.1", W + "n > 0.2", schema=SCHEMA)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    (row,) = result.counterexample.tables["t"]
    assert 0.1 < row["n"] <= 0.2


def test_typed_refutation_uses_the_numeric_reading():
    result = prove_equivalent_smt(W + "n = 0.1", W + "1 = 0", schema=SCHEMA, types=TYPES)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert result.counterexample.tables["t"][0]["n"] == 0.1


def test_readings():
    floating, numeric, bignumeric = C.readings("0.1")
    assert floating == Fraction(0.1) and numeric == bignumeric == Fraction(1, 10)
    assert C.readings("0.1000000005")[1] == Fraction(100000001, 10**9)  # half away from zero
    assert C.readings("0.10000000049")[1] == Fraction(1, 10)
    assert C.readings("0.1000000005", negate=True)[1] == -Fraction(100000001, 10**9)
    assert C.readings("0.10000000000000001")[2] == Fraction(10000000000000001, 10**17)
    assert C.readings("1e-1")[1:] == (Fraction(1, 10), Fraction(1, 10))
    assert C.readings("1e30")[1] is None and C.readings("1e30")[2] == Fraction(10**30)  # past NUMERIC, inside BIGNUMERIC
    assert C.readings("1e40")[2] is None


@pytest.mark.parametrize("text,expected", [("0.1", True), ("0.5", False), ("2.25", False), ("1e3", False), ("0.10", True), ("5", False), ("'0.1'", False), ("-(-0.1)", True), ("x", False)])
def test_unstable(text, expected):
    assert C.unstable(parse_one(text, read="bigquery")) is expected


def test_groups():
    def texts(sql):
        return [[m.sql(dialect="bigquery") for m in group] for group in C.groups(parse_one(sql, read="bigquery"))]

    assert texts("n = 0.1") == [["n", "0.1"]]
    assert texts("n IN (0.1, 0.2)") == [["n", "0.1", "0.2"]]
    assert texts("n BETWEEN 0.1 AND m") == [["n", "0.1", "m"]]
    assert texts("CASE n WHEN 0.1 THEN a ELSE 0.2 END") == [["a", "0.2"], ["n", "0.1"]]
    assert texts("IF(c, n, 0.1)") == [["n", "0.1"]]
    assert texts("COALESCE(n, 0.1)") == [["n", "0.1"]]
    assert texts("GREATEST(n, 0.1)") == [["n", "0.1"]]
    assert texts("CAST(0.1 AS NUMERIC)") == []
    assert texts("SUM(0.1)") == []
    assert texts("a AND b") == []
