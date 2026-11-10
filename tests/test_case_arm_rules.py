"""Arithmetic moved into literal ``CASE`` arms, and arms never taken dropped (``kumosql.case_arm_rules``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import canonical_negation
from kumosql.case_arm_rules import rewrite_case_arms

SCHEMA = {"t": ["a", "b", "c"], "s": ["k", "v"]}
DDL = {"t": "a DECIMAL(18,3), b BIGINT, c BIGINT", "s": "k BIGINT, v DECIMAL(18,3)"}
FLOAT_DDL = {"t": "a DOUBLE, b BIGINT, c BIGINT", "s": "k BIGINT, v DOUBLE"}


def rewritten(sql, dialect="mysql"):
    out = rewrite_case_arms(canonical_negation(sqlglot.parse_one(sql, read=dialect)), dialect)
    return out.sql(dialect=dialect) if out is not None else None


def proved(left, right, dialect="mysql"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect=dialect).proven


def run(rows, *queries, ddl=DDL):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name, columns in ddl.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        insert_rows(db, name, rows.get(name, []))
    return run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])


def bag(rows):
    return sorted(repr(tuple(None if v is None else round(float(v), 6) for v in row)) for row in rows)


def differ_on_duckdb(left, right, rows, ddl=DDL):
    a, b = run(rows, left, right, ddl=ddl)
    return bag(a) != bag(b)


def test_arithmetic_moves_into_literal_arms():
    assert rewritten("SELECT a * CASE WHEN b < 10 THEN 1 WHEN b <= 100 THEN 0.76 ELSE 0.51 END FROM t") == (
        "SELECT CASE WHEN b < 10 THEN a * 1 WHEN b <= 100 THEN a * 0.76 ELSE a * 0.51 END FROM t"
    )
    # no ELSE stays no ELSE: with no arm taken both are NULL
    assert rewritten("SELECT CASE WHEN b < 10 THEN 2 END * (a + c) FROM t") == "SELECT CASE WHEN b < 10 THEN 2 * (a + c) END FROM t"
    # subtraction keeps its operand order, and the outer product then moves in as well
    assert rewritten("SELECT a * (1 - CASE WHEN b < 10 THEN 0 WHEN b <= 100 THEN 0.24 ELSE 0.49 END) FROM t") == (
        "SELECT CASE WHEN b < 10 THEN a * (1 - 0) WHEN b <= 100 THEN a * (1 - 0.24) ELSE a * (1 - 0.49) END FROM t"
    )
    assert rewritten("SELECT CASE WHEN b < 10 THEN 5 ELSE 7 END - a FROM t") == "SELECT CASE WHEN b < 10 THEN 5 - a ELSE 7 - a END FROM t"
    assert rewritten("SELECT a FROM t WHERE c + CASE b WHEN 1 THEN 10 ELSE 20 END > 25") == (
        "SELECT a FROM t WHERE CASE b WHEN 1 THEN c + 10 ELSE c + 20 END > 25"
    )


@pytest.mark.parametrize("sql, dialect", [
    ("SELECT a * CASE WHEN b < 10 THEN 3 ELSE 0.5 END FROM t", "bigquery"),  # 3 becomes FLOAT64 there: INT64 a past 2^53 rounds first
    ("SELECT a * CASE WHEN b < 10 THEN 1 ELSE 5e-1 END FROM t", "mysql"),  # an exponent literal is a DOUBLE everywhere
    ("SELECT a * CASE WHEN b < 10 THEN c ELSE 2 END FROM t", "mysql"),  # an arm that is no literal
    ("SELECT a * CASE WHEN b < 10 THEN '1' ELSE 2 END FROM t", "mysql"),  # a string arm
    ("SELECT a / CASE WHEN b < 10 THEN 2 ELSE 4 END FROM t", "mysql"),  # division is not read (its scale depends on the divisor)
    ("SELECT RAND() * CASE WHEN b < 10 THEN 2 ELSE 4 END FROM t", "mysql"),
    ("SELECT (SELECT MAX(v) FROM s) * CASE WHEN b < 10 THEN 2 ELSE 4 END FROM t", "mysql"),
    ("SELECT SUM(a) OVER (ORDER BY c) * CASE WHEN b < 10 THEN 2 ELSE 4 END FROM t", "mysql"),
    ("SELECT a * IF(b < 10, 2, 4) FROM t", "mysql"),
])
def test_other_arithmetic_stays(sql, dialect):
    assert rewritten(sql, dialect) is None


def test_same_kind_arms_move_in_every_dialect():
    assert rewritten("SELECT a * CASE WHEN b < 10 THEN 3 ELSE 5 END FROM t", "bigquery") == "SELECT CASE WHEN b < 10 THEN a * 3 ELSE a * 5 END FROM t"
    assert rewritten("SELECT 1 - CASE WHEN b < 10 THEN 0.25 ELSE 0.5 END FROM t", "bigquery") == (
        "SELECT CASE WHEN b < 10 THEN 1 - 0.25 ELSE 1 - 0.5 END FROM t"
    )


def test_arms_never_taken_are_dropped():
    assert rewritten("SELECT CASE WHEN SUM(a) = NULL THEN NULL ELSE MAX(a) END FROM t") == "SELECT MAX(a) FROM t"
    assert rewritten("SELECT CASE WHEN NULL THEN NULL WHEN b > 1 THEN a END FROM t") == "SELECT CASE WHEN b > 1 THEN a END FROM t"
    assert rewritten("SELECT CASE WHEN NULL <> b THEN NULL ELSE a + c END FROM t") == "SELECT (a + c) FROM t"
    assert rewritten("SELECT CASE WHEN b >= NULL THEN NULL END FROM t") == "SELECT NULL FROM t"


@pytest.mark.parametrize("sql", [
    "SELECT CASE WHEN b IS NULL THEN NULL ELSE a END FROM t",  # IS NULL can be TRUE
    "SELECT CASE WHEN b <=> NULL THEN NULL ELSE a END FROM t",  # so can the NULL-safe comparison
    "SELECT CASE WHEN b = NULL THEN 1.5 ELSE c END FROM t",  # the arm's value still takes part in the CASE's type
    "SELECT CASE b WHEN NULL THEN NULL ELSE a END FROM t",  # a simple CASE is not read
    "SELECT CASE WHEN b = NULL OR c = 1 THEN NULL ELSE a END FROM t",
])
def test_other_arms_stay(sql):
    assert rewritten(sql) is None


RATE_TABLE = (
    "SELECT s.k, ROUND(CASE WHEN x.m BETWEEN 1000 AND 10000 THEN s.v * 0.76 WHEN x.m > 10000 THEN s.v * 0.51 ELSE s.v END, 0) AS pay "
    "FROM s JOIN (SELECT k, MAX(v) AS m FROM s GROUP BY k) AS x ON s.k = x.k"
)
PROOFS = [
    ("SELECT a * CASE WHEN b < 10 THEN 1 WHEN b <= 100 THEN 0.76 ELSE 0.51 END FROM t",
     "SELECT CASE WHEN b < 10 THEN a WHEN b <= 100 THEN a * 0.76 ELSE a * 0.51 END FROM t"),
    ("SELECT a * (1 - CASE WHEN b < 10 THEN 0 WHEN b <= 100 THEN 0.24 ELSE 0.49 END) FROM t",
     "SELECT CASE WHEN b < 10 THEN a WHEN b <= 100 THEN a * 0.76 ELSE a * 0.51 END FROM t"),
    # the rate in a per-group table, applied afterwards (the Singh & Bedathur salary pairs)
    (RATE_TABLE,
     "SELECT s.k, ROUND(s.v * r.rate, 0) AS pay FROM s JOIN (SELECT k, CASE WHEN MAX(v) < 1000 THEN 1 WHEN MAX(v) <= 10000 THEN 0.76 ELSE 0.51 END AS rate FROM s GROUP BY k) AS r ON s.k = r.k"),
    (RATE_TABLE,
     "SELECT s.k, ROUND((1 - r.tax) * s.v) AS pay FROM s JOIN (SELECT k, CASE WHEN MAX(v) < 1000 THEN 0 WHEN MAX(v) BETWEEN 1000 AND 10000 THEN 0.24 WHEN MAX(v) > 10000 THEN 0.49 END AS tax FROM s GROUP BY k) AS r ON s.k = r.k"),
    ("SELECT MAX(a) FROM (SELECT a FROM t GROUP BY a HAVING COUNT(a) = 1) AS q",
     "SELECT CASE WHEN SUM(a) = NULL THEN NULL ELSE MAX(a) END FROM (SELECT a FROM t GROUP BY a HAVING COUNT(*) = 1) AS q"),
]


@pytest.mark.parametrize("left, right", PROOFS)
def test_target_pairs_are_proved(left, right):
    assert proved(left, right)


# Each pair differs on the rows given (checked below), so none may be proved.
NEAR_MISSES = [
    # the boundary moved by one: b = 10 takes different arms
    ("SELECT a * CASE WHEN b < 10 THEN 1 ELSE 0.5 END FROM t", "SELECT CASE WHEN b <= 10 THEN a ELSE a * 0.5 END FROM t", {"t": [(4, 10, 0)]}, DDL),
    # no ELSE is NULL, not the operand
    ("SELECT a * CASE WHEN b < 10 THEN 2 END FROM t", "SELECT CASE WHEN b < 10 THEN a * 2 ELSE a END FROM t", {"t": [(4, 20, 0)]}, DDL),
    # subtraction does not commute
    ("SELECT 1 - CASE WHEN b < 10 THEN 0.24 ELSE 0.49 END FROM t", "SELECT CASE WHEN b < 10 THEN 0.24 - 1 ELSE 0.49 - 1 END FROM t", {"t": [(4, 5, 0)]}, DDL),
    # IS NULL and the NULL-safe comparison can be TRUE
    ("SELECT CASE WHEN b IS NULL THEN NULL ELSE a END FROM t", "SELECT a FROM t", {"t": [(4, None, 0)]}, DDL),
    ("SELECT CASE WHEN b <=> NULL THEN NULL ELSE a END FROM t", "SELECT a FROM t", {"t": [(4, None, 0)]}, DDL),
    # the rate table with the lower boundary on the other side: a top salary of exactly 1000
    (RATE_TABLE,
     "SELECT s.k, ROUND(s.v * r.rate, 0) AS pay FROM s JOIN (SELECT k, CASE WHEN MAX(v) <= 1000 THEN 1 WHEN MAX(v) <= 10000 THEN 0.76 ELSE 0.51 END AS rate FROM s GROUP BY k) AS r ON s.k = r.k",
     {"s": [(1, 1000)]}, DDL),
    # salary - 0.24 * salary is salary * 0.76 only in exact arithmetic: a DOUBLE salary rounds the other way
    ("SELECT ROUND(v - 0.24 * v, 0) FROM s", "SELECT ROUND(v * 0.76, 0) FROM s", {"s": [(1, 16.44736842105263)]}, FLOAT_DDL),
    ("SELECT s.k, ROUND(CASE WHEN x.m BETWEEN 10 AND 10000 THEN s.v * 0.76 ELSE s.v END, 0) AS pay FROM s JOIN (SELECT k, MAX(v) AS m FROM s GROUP BY k) AS x ON s.k = x.k",
     "SELECT s.k, ROUND(s.v - s.v * r.tax, 0) AS pay FROM s JOIN (SELECT k, CASE WHEN MAX(v) BETWEEN 10 AND 10000 THEN 0.24 ELSE 0 END AS tax FROM s GROUP BY k) AS r ON s.k = r.k",
     {"s": [(1, 16.44736842105263)]}, FLOAT_DDL),
]


@pytest.mark.parametrize("left, right, rows, ddl", NEAR_MISSES)
def test_near_misses_differ_and_stay_unproved(left, right, rows, ddl):
    assert differ_on_duckdb(left, right, rows, ddl)
    assert not proved(left, right)


REWRITES = [
    "SELECT a * CASE WHEN b < 2 THEN 1 WHEN b <= 3 THEN 0.76 ELSE 0.51 END FROM t",
    "SELECT a * (1 - CASE WHEN b < 2 THEN 0 WHEN b <= 3 THEN 0.24 WHEN b > 3 THEN 0.49 END) FROM t",
    "SELECT CASE WHEN b = 1 THEN (1 - .49) WHEN c = 2 THEN 2 END * a + c FROM t",
    "SELECT c - CASE b WHEN 1 THEN 10 WHEN 2 THEN 20 END, CASE WHEN c > 1 THEN 3 ELSE 4 END + b FROM t",
    # MAX, not SUM: DuckDB's SUM of a DECIMAL is DECIMAL(38, s), and at that width its CASE drops the extra scale
    # of one arm (50.0625 becomes 50.063), which MySQL's wider decimals do not. The prover does not model the
    # width of a CASE's common type, with or without this rule.
    "SELECT b, MAX(a) * CASE WHEN COUNT(*) > 1 THEN 2 ELSE 0.5 END FROM t GROUP BY b",
    "SELECT a FROM t WHERE c + CASE WHEN b > 1 THEN 1 ELSE 2 END > 3",
    "SELECT CASE WHEN SUM(a) = NULL THEN NULL ELSE MAX(a) END, CASE WHEN NULL THEN NULL WHEN MIN(b) > 1 THEN COUNT(*) END FROM t",
    "SELECT CASE WHEN b <> NULL THEN NULL WHEN c < NULL THEN NULL END, CASE WHEN NULL > c THEN NULL ELSE a * 2 END FROM t",
]


@pytest.mark.parametrize("ddl", [DDL, FLOAT_DDL])
def test_rewrites_keep_every_result(ddl):
    """Original and rewritten select return the same bag on random small tables with NULLs and duplicate rows."""

    rng = random.Random(20261110)
    pairs = [(sql, rewritten(sql)) for sql in REWRITES]
    assert all(after is not None and after != before for before, after in pairs)
    for _ in range(40):
        rows = [(rng.choice([None, 0, 1, 2.5, 3, 12.5, 100.125]), rng.choice([None, 0, 1, 2, 3, 4]), rng.choice([None, 1, 2, 3])) for _ in range(rng.randint(0, 6))]
        rows += rows[:2]  # exact duplicate rows
        results = run({"t": rows}, *[sql for pair in pairs for sql in pair], ddl=ddl)
        for index, (before, after) in enumerate(pairs):
            assert bag(results[2 * index]) == bag(results[2 * index + 1]), (before, after, rows)
