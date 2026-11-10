"""Implied ``COUNT`` bounds dropped from ``HAVING``, and counted keys tested for NULL (``kumosql.having_count_rules``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import canonical_negation
from kumosql.having_count_rules import rewrite_count_tests

SCHEMA = {"t": ["a", "b", "c"]}


def rewritten(sql, dialect="mysql"):
    out = rewrite_count_tests(canonical_negation(sqlglot.parse_one(sql, read=dialect)))
    return out.sql(dialect=dialect) if out is not None else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql").proven


def run(rows, *queries):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    db.execute("CREATE TABLE t (a BIGINT, b BIGINT, c BIGINT)")
    insert_rows(db, "t", rows)
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])]


def differ_on_duckdb(left, right, rows):
    a, b = run(rows, left, right)
    return a != b


def test_implied_bounds_are_dropped():
    assert rewritten("SELECT a FROM t GROUP BY a, c HAVING COUNT(*) > 1 AND COUNT(DISTINCT b) > 1") == (
        "SELECT a FROM t GROUP BY a, c HAVING COUNT(DISTINCT b) > 1"
    )
    assert rewritten("SELECT a FROM t GROUP BY a, c HAVING COUNT(b) >= 3 AND 2 <= COUNT(*) AND c > 0") == (
        "SELECT a FROM t GROUP BY a, c HAVING COUNT(b) >= 3 AND c > 0"
    )
    assert rewritten("SELECT a FROM t GROUP BY a, c HAVING COUNT(b) >= 2 AND COUNT(DISTINCT b) = 2") == (
        "SELECT a FROM t GROUP BY a, c HAVING COUNT(DISTINCT b) = 2"
    )
    assert rewritten("SELECT a FROM t GROUP BY a, c HAVING COUNT(*) >= 2 AND COUNT(*) > 4") == "SELECT a FROM t GROUP BY a, c HAVING COUNT(*) > 4"
    assert rewritten("SELECT a FROM t GROUP BY a, c HAVING COUNT(*) >= 2 AND COUNT(*) >= 2") == "SELECT a FROM t GROUP BY a, c HAVING COUNT(*) >= 2"
    assert rewritten("SELECT MAX(a) FROM t HAVING COUNT(*) > 0 AND COUNT(b) = 3") == "SELECT MAX(a) FROM t HAVING COUNT(b) = 3"


def test_counted_keys_are_tested_for_null():
    assert rewritten("SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1") == "SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1 AND NOT a IS NULL"
    assert rewritten("SELECT a, b FROM t GROUP BY a, b HAVING COUNT(DISTINCT b) = 1 AND SUM(c) > 2") == (
        "SELECT a, b FROM t GROUP BY a, b HAVING COUNT(DISTINCT b) = 1 AND SUM(c) > 2 AND NOT b IS NULL"
    )
    # once is enough, and a test already in WHERE counts
    assert rewritten("SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1 AND NOT a IS NULL") is None
    assert rewritten("SELECT a FROM t WHERE a IS NOT NULL GROUP BY a HAVING COUNT(a) > 1") is None


@pytest.mark.parametrize("sql", [
    "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1",  # COUNT(*) counts the NULL group's rows
    "SELECT a FROM t GROUP BY a HAVING COUNT(b) > 1",  # b is no key
    "SELECT a FROM t GROUP BY a HAVING COUNT(a) >= 0",  # true for the NULL group
    "SELECT a FROM t GROUP BY a HAVING COUNT(a) < 2",
    "SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1 OR SUM(b) > 3",  # not a conjunct
    "SELECT a FROM t GROUP BY a WITH ROLLUP HAVING COUNT(a) > 1",  # the total row has a NULL key and counts every a
    "SELECT b AS a FROM t GROUP BY t.a, b HAVING COUNT(a) > 1",  # the name is the select alias in HAVING
    "SELECT a FROM t GROUP BY a HAVING COUNT(*) >= 3 AND COUNT(DISTINCT b) >= 2",  # 2 different values need only 2 rows
    "SELECT a FROM t GROUP BY a HAVING COUNT(b) >= 2 AND COUNT(DISTINCT c) >= 2",  # another column
    "SELECT a FROM t GROUP BY a HAVING COUNT(DISTINCT b) >= 2 AND COUNT(b) >= 2 OR c > 1",
    "SELECT a FROM t GROUP BY a HAVING COUNT(*) = 2 AND COUNT(b) >= 2",  # an equality is never dropped
    "SELECT a FROM t GROUP BY a HAVING COUNT(*) <= 2 AND COUNT(b) <= 2",  # upper bounds run the other way
    "SELECT a FROM t GROUP BY a HAVING COUNT(*) FILTER (WHERE b > 1) >= 1 AND COUNT(b) >= 2",
])
def test_other_tests_stay(sql):
    assert rewritten(sql, "postgres" if "FILTER" in sql else "mysql") is None


PROOFS = [
    ("SELECT DISTINCT a FROM t GROUP BY c, a HAVING COUNT(DISTINCT b) > 1",
     "SELECT DISTINCT a FROM t GROUP BY c, a HAVING COUNT(*) > 1 AND COUNT(DISTINCT b) > 1"),
    ("SELECT a, SUM(c) FROM t GROUP BY a HAVING COUNT(b) >= 2", "SELECT a, SUM(c) FROM t GROUP BY a HAVING COUNT(b) >= 2 AND COUNT(*) >= 1"),
    ("SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1", "SELECT a FROM t WHERE a IS NOT NULL GROUP BY a HAVING COUNT(*) > 1"),
    ("SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1", "SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1 AND a IS NOT NULL"),
    ("SELECT a, MIN(b) FROM t GROUP BY a HAVING COUNT(DISTINCT a) = 1", "SELECT a, MIN(b) FROM t WHERE a IS NOT NULL GROUP BY a"),
]


@pytest.mark.parametrize("left, right", PROOFS)
def test_target_pairs_are_proved(left, right):
    assert proved(left, right)


# Each pair differs on the rows given (checked below), so none may be proved.
NEAR_MISSES = [
    # COUNT(*) >= 3 is not implied by two different values
    ("SELECT a FROM t GROUP BY a HAVING COUNT(DISTINCT b) >= 2", "SELECT a FROM t GROUP BY a HAVING COUNT(*) >= 3 AND COUNT(DISTINCT b) >= 2", [(1, 1, 0), (1, 2, 0)]),
    # a bound on COUNT(*) says nothing about COUNT(b): the rows may hold NULLs
    ("SELECT a FROM t GROUP BY a HAVING COUNT(*) >= 2", "SELECT a FROM t GROUP BY a HAVING COUNT(*) >= 2 AND COUNT(b) >= 2", [(1, 1, 0), (1, None, 0)]),
    # nor does COUNT(b) bound COUNT(DISTINCT b)
    ("SELECT a FROM t GROUP BY a HAVING COUNT(b) >= 2", "SELECT a FROM t GROUP BY a HAVING COUNT(b) >= 2 AND COUNT(DISTINCT b) >= 2", [(1, 5, 0), (1, 5, 0)]),
    # COUNT(*) keeps the group of NULL keys that COUNT(a) drops
    ("SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1", "SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1", [(None, 1, 0), (None, 2, 0)]),
    ("SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1", "SELECT a FROM t WHERE a IS NOT NULL GROUP BY a HAVING COUNT(*) > 1", [(None, 1, 0), (None, 2, 0)]),
    # a counted column that is no key may be NULL in a group that still passes on other rows
    ("SELECT a FROM t GROUP BY a HAVING COUNT(b) >= 1", "SELECT a FROM t WHERE b IS NOT NULL GROUP BY a HAVING COUNT(*) >= 2", [(1, 1, 0), (1, None, 0)]),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_differ_and_stay_unproved(left, right, rows):
    assert differ_on_duckdb(left, right, rows)
    assert not proved(left, right)


REWRITES = [
    "SELECT a, c FROM t GROUP BY a, c HAVING COUNT(*) > 1 AND COUNT(DISTINCT b) > 1",
    "SELECT a FROM t GROUP BY a HAVING COUNT(b) >= 2 AND COUNT(*) >= 2 AND COUNT(DISTINCT b) = 2",
    "SELECT MAX(a) FROM t HAVING COUNT(*) >= 1 AND COUNT(b) > 1",
    "SELECT a, COUNT(*) FROM t GROUP BY a HAVING COUNT(a) > 1",
    "SELECT a, b, SUM(c) FROM t GROUP BY a, b HAVING COUNT(DISTINCT b) >= 1 AND COUNT(a) = 2",
    "SELECT a FROM t WHERE c > 1 GROUP BY a HAVING 1 <= COUNT(a) AND COUNT(*) > 0",
]


def test_rewrites_keep_every_result():
    """Original and rewritten select return the same bag on random small tables with NULLs and duplicate rows."""

    rng = random.Random(20261110)
    pairs = [(sql, rewritten(sql)) for sql in REWRITES]
    assert all(after is not None and after != before for before, after in pairs)
    for _ in range(60):
        rows = [tuple(rng.choice([None, 1, 2]) for _ in range(3)) for _ in range(rng.randint(0, 7))]
        rows += rows[:2]  # exact duplicate rows
        results = run(rows, *[sql for pair in pairs for sql in pair])
        for index, (before, after) in enumerate(pairs):
            assert results[2 * index] == results[2 * index + 1], (before, after, rows)
