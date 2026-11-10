"""``SELECT DISTINCT x FROM t WHERE x IN (groups of t by x)`` read as the grouped query (``kumosql.grouped_self_membership``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import canonical_negation
from kumosql.grouped_self_membership import read_grouped_self_membership

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}


def rewritten(sql, dialect="mysql"):
    before = canonical_negation(sqlglot.parse_one(sql, read=dialect))
    text = before.sql(dialect=dialect)
    after = read_grouped_self_membership(before, dialect).sql(dialect=dialect)
    return after if after != text else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql").proven


def run(rows, *queries):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name in SCHEMA:
        db.execute(f"CREATE TABLE {name} (a BIGINT, b BIGINT, c BIGINT)")
        insert_rows(db, name, rows.get(name, []))
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])]


def differ_on_duckdb(left, right, rows):
    a, b = run(rows, left, right)
    return a != b


def test_distinct_members_of_own_groups_are_the_groups():
    assert rewritten("SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)") == (
        "SELECT a FROM t GROUP BY a HAVING (COUNT(a) > 1) AND NOT a IS NULL"
    )
    # the subquery's own filter stays; the outer NULL test is implied; the output keeps its name
    assert rewritten("SELECT DISTINCT p.a AS v FROM t AS p WHERE p.a IS NOT NULL AND p.a IN (SELECT q.a FROM t AS q WHERE q.b > 1 GROUP BY q.a)") == (
        "SELECT q.a AS v FROM t AS q WHERE q.b > 1 GROUP BY q.a HAVING NOT q.a IS NULL"
    )
    # the form the groups take once normalize has lifted them into a derived table
    assert rewritten("SELECT DISTINCT a FROM t WHERE a IN (SELECT g.k FROM (SELECT a AS k, COUNT(*) AS n FROM t GROUP BY a) AS g WHERE g.n = 1)") == (
        "SELECT g.k AS a FROM (SELECT a AS k, COUNT(*) AS n FROM t GROUP BY a) AS g WHERE (g.n = 1) AND NOT g.k IS NULL"
    )


@pytest.mark.parametrize("sql", [
    "SELECT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",  # no DISTINCT: one row per member row
    "SELECT DISTINCT a FROM t WHERE a NOT IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM u GROUP BY a HAVING COUNT(a) > 1)",  # another table's groups
    "SELECT DISTINCT a FROM t WHERE b IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",  # another column is tested
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT b FROM t GROUP BY b HAVING COUNT(b) > 1)",
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a, b HAVING COUNT(a) > 1)",
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT MAX(a) FROM t GROUP BY b)",
    "SELECT DISTINCT a, b FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",
    "SELECT DISTINCT a FROM t WHERE b > 1 AND a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",  # the outer rows are filtered too
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1) OR a = 3",
    "SELECT DISTINCT p.a FROM t AS p WHERE p.a IN (SELECT q.a FROM t AS q GROUP BY q.a HAVING COUNT(*) > p.b)",  # correlated
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1 LIMIT 1)",
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1) LIMIT 1",
    "SELECT DISTINCT a FROM t JOIN u ON t.b = u.b WHERE t.a IN (SELECT a FROM t GROUP BY a)",
    "SELECT DISTINCT a + 1 FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",
])
def test_other_queries_stay(sql):
    assert rewritten(sql) is None


PROOFS = [
    ("SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1", "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)"),
    ("SELECT a FROM t WHERE a IS NOT NULL GROUP BY a HAVING COUNT(*) = 1", "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(*) = 1)"),
    ("SELECT a FROM t WHERE b > 1 AND a IS NOT NULL GROUP BY a", "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t WHERE b > 1 GROUP BY a)"),
    # a duplicate-blind aggregate reaches the rule through the set-of-values reduction
    ("SELECT MAX(a) FROM (SELECT a FROM t GROUP BY a HAVING COUNT(a) = 1) AS q", "SELECT MAX(a) FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) = 1)"),
    ("SELECT COUNT(DISTINCT a) FROM (SELECT a FROM t GROUP BY a HAVING SUM(b) > 3) AS q", "SELECT COUNT(DISTINCT a) FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING SUM(b) > 3)"),
]


@pytest.mark.parametrize("left, right", PROOFS)
def test_target_pairs_are_proved(left, right):
    assert proved(left, right)


GROUPED = "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1"
# Each pair differs on the rows given (checked below), so none may be proved.
NEAR_MISSES = [
    # COUNT(*) keeps the group of NULLs, which IN never matches
    (GROUPED, "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1)", {"t": [(None, 1, 0), (None, 2, 0)]}),
    # without DISTINCT every member row comes back
    (GROUPED, "SELECT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1)", {"t": [(1, 1, 0), (1, 2, 0)]}),
    # the groups of another table need not occur in this one
    ("SELECT a FROM u GROUP BY a HAVING COUNT(a) > 1", "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM u GROUP BY a HAVING COUNT(a) > 1)",
     {"u": [(1, 1, 0), (1, 2, 0)]}),
    # an outer filter drops member rows the groups counted
    ("SELECT a FROM t WHERE b > 1 GROUP BY a HAVING COUNT(a) > 1", "SELECT DISTINCT a FROM t WHERE b > 1 AND a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",
     {"t": [(1, 1, 0), (1, 2, 0)]}),
    # SUM sees repeats that MAX does not
    ("SELECT SUM(a) FROM (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1) AS q", "SELECT SUM(a) FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",
     {"t": [(1, 1, 0), (1, 2, 0)]}),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_differ_and_stay_unproved(left, right, rows):
    assert differ_on_duckdb(left, right, rows)
    assert not proved(left, right)


REWRITES = [
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(a) > 1)",
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1)",
    "SELECT DISTINCT a AS v FROM t WHERE a IN (SELECT a FROM t GROUP BY a)",
    "SELECT DISTINCT p.a FROM t AS p WHERE p.a IS NOT NULL AND p.a IN (SELECT q.a FROM t AS q WHERE q.b > 1 GROUP BY q.a HAVING SUM(q.c) >= 2 OR MIN(q.b) = 2)",
    "SELECT DISTINCT a FROM t WHERE a IN (SELECT g.k FROM (SELECT a AS k, COUNT(*) AS n, MAX(b) AS m FROM t GROUP BY a) AS g WHERE g.n = 1 OR g.m > 1)",
    "SELECT b, (SELECT COUNT(*) FROM (SELECT DISTINCT a FROM u WHERE a IN (SELECT a FROM u GROUP BY a HAVING COUNT(b) = 1)) AS d) FROM t",
]


def test_rewrites_keep_every_result():
    """Original and rewritten query return the same bag on random small tables with NULLs and duplicate rows."""

    rng = random.Random(20261110)
    pairs = [(sql, rewritten(sql)) for sql in REWRITES]
    assert all(after is not None for _, after in pairs)
    for _ in range(60):
        rows = {name: [tuple(rng.choice([None, 1, 2, 3]) for _ in range(3)) for _ in range(rng.randint(0, 6))] for name in SCHEMA}
        for name in rows:
            rows[name] += rows[name][:2]  # exact duplicate rows
        results = run(rows, *[sql for pair in pairs for sql in pair])
        for index, (before, after) in enumerate(pairs):
            assert results[2 * index] == results[2 * index + 1], (before, after, rows)
