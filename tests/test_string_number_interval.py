"""``x + INTERVAL ..`` is a date, a timestamp or an interval, not a number (``kumosql.string_number_compare``).

``d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) + INTERVAL '60' DAY`` compares a date column with a date string
and a date. The guard against a string compared with a number read the ``+`` as arithmetic on numbers and declined every
query with such a bound, even against itself. Arithmetic on numbers is still declined.
"""

import datetime
import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql import string_number_compare
from kumosql.algebraic_equivalence import prove_equivalent_algebraic

SCHEMA = {"t": ["k", "d", "n"], "u": ["k", "d", "n"]}
TYPES = {name: {"k": "INT", "d": "DATE", "n": "INT"} for name in SCHEMA}
BOUND = "CAST('2000-10-01' AS DATE) + INTERVAL '60' DAY"


def proved(left, right, dialect="postgres"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, compare_names=False).proven


def differ_on_duckdb(left, right, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    for name in SCHEMA:
        db.execute(f"CREATE TABLE {name} (k BIGINT, d DATE, n BIGINT)")
        if rows.get(name):
            db.executemany(f"INSERT INTO {name} VALUES (?, ?, ?)", rows[name])
    a, b = run_unoptimized(db, *(sqlglot.transpile(q, read="postgres", write="duckdb")[0] for q in (left, right)))
    return sorted(map(repr, a)) != sorted(map(repr, b))


def day(text):
    return datetime.date.fromisoformat(text)


def test_interval_arithmetic_is_not_a_number():
    problem = string_number_compare.problem
    for dialect in ("postgres", "duckdb", "mysql"):
        assert problem(f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", dialect) is None
        assert problem("SELECT k FROM t WHERE '2000-10-01' <= d - INTERVAL '1' DAY", dialect) is None
        assert problem("SELECT k FROM t WHERE d IN ('2000-10-01', d + INTERVAL '1' DAY)", dialect) is None
    # arithmetic on numbers, and a number bound beside the interval bound, are declined as before
    assert problem("SELECT k FROM t WHERE '2000' = n + 60", "postgres")
    assert problem("SELECT k FROM t WHERE '2000' = n - 60", "postgres")
    assert problem(f"SELECT k FROM t WHERE '2000' = n * 2 AND d <= {BOUND}", "postgres")
    assert problem(f"SELECT k FROM t WHERE n BETWEEN '10' AND 20 + 5 AND d <= {BOUND}", "bigquery")
    assert problem("SELECT k FROM t WHERE '2000' = (n + 60) + 1", "postgres")


TARGETS = [
    # the bound against itself, under an alias and under a derived table
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT x.k FROM t AS x WHERE x.d BETWEEN '2000-10-01' AND {BOUND}"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT k FROM (SELECT * FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}) AS x"),
    # BETWEEN is its two comparisons
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT k FROM t WHERE d >= '2000-10-01' AND d <= {BOUND}"),
    # a filter pushed below a join
    (
        f"SELECT t.k, COUNT(*) AS c FROM t JOIN u ON t.k = u.k WHERE t.d BETWEEN '2000-10-01' AND {BOUND} GROUP BY t.k",
        f"SELECT x.k, COUNT(*) AS c FROM (SELECT * FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}) AS x JOIN u ON x.k = u.k GROUP BY x.k",
    ),
]


@pytest.mark.parametrize("left, right", TARGETS)
def test_interval_bounds_prove(left, right):
    assert proved(left, right)


ROWS = {
    "t": [(1, day("2000-10-01"), 1), (2, day("2000-10-20"), 2), (3, day("2000-11-15"), 3), (4, day("2000-11-30"), 4), (5, day("2000-12-01"), 5), (6, None, 6), (2, day("2000-10-20"), 2)],
    "u": [(1, None, 1), (2, None, 2), (3, None, 3), (4, None, 4), (5, None, 5)],
}
NEAR_MISSES = [
    # another length, another unit, the other sign, a strict bound, no upper bound
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) + INTERVAL '30' DAY"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) + INTERVAL '60' MONTH"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-10-01' AS DATE) - INTERVAL '60' DAY"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", f"SELECT k FROM t WHERE d >= '2000-10-01' AND d < {BOUND}"),
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d >= '2000-10-01'"),
    # another start date
    (f"SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND {BOUND}", "SELECT k FROM t WHERE d BETWEEN '2000-10-01' AND CAST('2000-09-01' AS DATE) + INTERVAL '60' DAY"),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


def test_proved_forms_give_the_same_rows():
    rng = random.Random(11)
    start = day("2000-09-25")
    for _ in range(8):
        rows = {
            name: [(rng.choice([None, 1, 2, 3]), rng.choice([None] + [start + datetime.timedelta(days=rng.randint(0, 80)) for _ in range(3)]), rng.randint(0, 3)) for _ in range(rng.randint(0, 7))]
            for name in SCHEMA
        }
        rows["t"] += rows["t"][:2]
        for left, right in TARGETS:
            assert not differ_on_duckdb(left, right, rows)
