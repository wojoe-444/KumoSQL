"""A ``UNION`` of branches that cannot repeat a row read as ``UNION ALL`` (``kumosql.disjoint_union``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.disjoint_union import separate_disjoint_unions

SCHEMA = {"t": ["a", "b", "c"]}


def rewritten(sql, dialect="mysql"):
    before = sqlglot.parse_one(sql, read=dialect)
    text = before.sql(dialect=dialect)
    after = separate_disjoint_unions(before).sql(dialect=dialect)
    return after if after != text else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql").proven


def run(rows, *queries):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    db.execute("SET default_collation = 'nocase'")  # MySQL's default: 'A' and 'a' are one value to UNION
    db.execute("CREATE TABLE t (a BIGINT, b BIGINT, c BIGINT)")
    insert_rows(db, "t", rows)
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])]


def differ_on_duckdb(left, right, rows):
    a, b = run(rows, left, right)
    return a != b


def test_tagged_single_rows_are_a_union_all():
    assert rewritten("SELECT 'low' AS bin, COUNT(a) AS n FROM t WHERE b < 5 UNION SELECT 'high', COUNT(a) FROM t WHERE b >= 5") == (
        "SELECT 'low' AS bin, COUNT(a) AS n FROM t WHERE b < 5 UNION ALL SELECT 'high', COUNT(a) FROM t WHERE b >= 5"
    )
    # every UNION of a chain, an integer tag at another position, a branch with no FROM, a DISTINCT that changes nothing
    assert rewritten("SELECT MAX(a), 1 FROM t UNION SELECT MIN(a), 2 FROM t UNION SELECT 0, 3 UNION SELECT DISTINCT SUM(b), 4 FROM t") == (
        "SELECT MAX(a), 1 FROM t UNION ALL SELECT MIN(a), 2 FROM t UNION ALL SELECT 0, 3 UNION ALL SELECT DISTINCT SUM(b), 4 FROM t"
    )
    assert rewritten("SELECT * FROM (SELECT '[0-5>' AS bin, SUM(a) AS n FROM t UNION SELECT '[5-10>', SUM(b) FROM t) AS r") == (
        "SELECT * FROM (SELECT '[0-5>' AS bin, SUM(a) AS n FROM t UNION ALL SELECT '[5-10>', SUM(b) FROM t) AS r"
    )


@pytest.mark.parametrize("sql", [
    "SELECT 'x', COUNT(a) FROM t UNION SELECT 'x', COUNT(b) FROM t",  # the same tag
    "SELECT 'A', COUNT(a) FROM t UNION SELECT 'a ', COUNT(b) FROM t",  # equal in a case-insensitive, padded collation
    "SELECT 'a-b', COUNT(a) FROM t UNION SELECT 'ab', COUNT(b) FROM t",  # punctuation alone does not tell tags apart
    "SELECT 'é', COUNT(a) FROM t UNION SELECT 'e', COUNT(b) FROM t",  # nor do accents
    "SELECT '1', COUNT(a) FROM t UNION SELECT 1, COUNT(b) FROM t",  # a string and a number
    "SELECT 1.0, COUNT(a) FROM t UNION SELECT 1.00, COUNT(b) FROM t",  # decimal tags are not read
    "SELECT 'x', a FROM t UNION SELECT 'y', b FROM t",  # a branch may repeat a row
    "SELECT DISTINCT 'x', a FROM t UNION SELECT DISTINCT 'y', b FROM t",  # exact, but left to the set comparison of UNION branches
    "SELECT 'x', COUNT(a) FROM t GROUP BY b UNION SELECT 'y', COUNT(b) FROM t",
    "SELECT 'x', COUNT(a) OVER () FROM t UNION SELECT 'y', COUNT(b) FROM t",  # a window is no aggregate
    "SELECT 'x', (SELECT COUNT(a) FROM t) FROM t UNION SELECT 'y', COUNT(b) FROM t",
    "SELECT COUNT(a), COUNT(b) FROM t UNION SELECT COUNT(b), COUNT(c) FROM t",  # no tag at all
    "SELECT c AS tag, COUNT(a) FROM t UNION SELECT 'y', COUNT(b) FROM t",
    "SELECT 'x', COUNT(a) FROM t INTERSECT SELECT 'y', COUNT(b) FROM t",
    "SELECT 'x', COUNT(a) FROM t EXCEPT SELECT 'y', COUNT(b) FROM t",
    "SELECT 'x', COUNT(a) FROM t UNION ALL SELECT 'y', COUNT(b) FROM t",
])
def test_other_set_operations_stay(sql):
    assert rewritten(sql) is None


def test_a_set_returning_function_in_the_select_list_may_repeat_a_row():
    """PostgreSQL returns a row per element: ``SELECT 'x', GENERATE_SERIES(1, 2) * 0`` is two equal rows, which UNION folds."""

    sql = "SELECT 'x', GENERATE_SERIES(1, 2) * 0 UNION SELECT 'y', COUNT(b) FROM t"
    tree = sqlglot.parse_one(sql, read="postgres")
    assert separate_disjoint_unions(tree).sql(dialect="postgres") == sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres")


PROOFS = [
    ("SELECT 'low' AS bin, COUNT(a) AS n FROM t WHERE b < 5 UNION SELECT 'high', COUNT(a) FROM t WHERE b >= 5",
     "SELECT 'low' AS bin, COUNT(CASE WHEN b < 5 THEN a END) AS n FROM t UNION ALL SELECT 'high', COUNT(CASE WHEN b >= 5 THEN a END) FROM t"),
    ("SELECT 1 AS k, MAX(a) AS v FROM t UNION SELECT 2, MIN(a) FROM t UNION SELECT 3, SUM(a) FROM t",
     "SELECT 1 AS k, MAX(a) AS v FROM t UNION ALL SELECT 2, MIN(a) FROM t UNION ALL SELECT 3, SUM(a) FROM t"),
    ("SELECT 'p' AS k, SUM(b) AS v FROM t WHERE a > 1 UNION ALL SELECT 'q', COUNT(*) FROM t",
     "SELECT 'p' AS k, SUM(b) AS v FROM t WHERE a > 1 UNION SELECT 'q', COUNT(*) FROM t"),
    # tagged branches of many rows are still compared as sets, with or without DISTINCT
    ("SELECT a, 'x' AS k, b FROM t WHERE b IS NOT NULL UNION SELECT a, 'y', c FROM t WHERE c IS NOT NULL",
     "SELECT DISTINCT a, 'x' AS k, b FROM t WHERE b IS NOT NULL UNION SELECT DISTINCT a, 'y', c FROM t WHERE c IS NOT NULL"),
]


@pytest.mark.parametrize("left, right", PROOFS)
def test_target_pairs_are_proved(left, right):
    assert proved(left, right)


# Each pair differs on the rows given (checked below), so none may be proved.
NEAR_MISSES = [
    # the same tag: equal counts collapse to one row
    ("SELECT 'x', COUNT(a) FROM t UNION SELECT 'x', COUNT(b) FROM t", "SELECT 'x', COUNT(a) FROM t UNION ALL SELECT 'x', COUNT(b) FROM t", [(1, 1, 1)]),
    # tags that differ only by case are one value where strings compare without case
    ("SELECT 'A', COUNT(a) FROM t UNION SELECT 'a', COUNT(b) FROM t", "SELECT 'A', COUNT(a) FROM t UNION ALL SELECT 'a', COUNT(b) FROM t", [(1, 1, 1)]),
    # a tagged branch that is not grouped repeats its own rows
    ("SELECT 'x', a FROM t UNION SELECT 'y', COUNT(b) FROM t", "SELECT 'x', a FROM t UNION ALL SELECT 'y', COUNT(b) FROM t", [(1, 1, 1), (1, 2, 2)]),
    # no tag: two single rows can be the same row
    ("SELECT COUNT(a), COUNT(b) FROM t UNION SELECT COUNT(b), COUNT(a) FROM t", "SELECT COUNT(a), COUNT(b) FROM t UNION ALL SELECT COUNT(b), COUNT(a) FROM t", [(1, 1, 1)]),
    # a window function returns a row per input row
    ("SELECT 'x', COUNT(a) OVER () FROM t UNION SELECT 'y', COUNT(b) FROM t", "SELECT 'x', COUNT(a) OVER () FROM t UNION ALL SELECT 'y', COUNT(b) FROM t",
     [(1, 1, 1), (2, 2, 2)]),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_differ_and_stay_unproved(left, right, rows):
    assert differ_on_duckdb(left, right, rows)
    assert not proved(left, right)


REWRITES = [
    "SELECT 'low' AS bin, COUNT(a) AS n FROM t WHERE b < 2 UNION SELECT 'high', COUNT(a) FROM t WHERE b >= 2",
    "SELECT MAX(a), 1 FROM t UNION SELECT MIN(a), 2 FROM t UNION SELECT 0, 3 UNION SELECT DISTINCT SUM(b), 4 FROM t",
    "SELECT 'n', COUNT(*) FROM t HAVING COUNT(*) > 2 UNION SELECT 'd', COUNT(DISTINCT a) FROM t UNION SELECT 's', SUM(c) FROM t WHERE c IS NULL",
    "SELECT * FROM (SELECT '[0-5>' AS bin, SUM(a) AS n FROM t UNION SELECT '[5-10>', SUM(b) FROM t) AS r WHERE n IS NOT NULL",
    "SELECT 'k', COUNT(a), MAX(b) FROM t UNION SELECT 'm', COUNT(b), MAX(a) FROM t UNION ALL SELECT 'n', 1, 1",
]


def test_rewrites_keep_every_result():
    """Original and rewritten query return the same bag on random small tables with NULLs and duplicate rows."""

    rng = random.Random(20261110)
    pairs = [(sql, rewritten(sql)) for sql in REWRITES]
    assert all(after is not None for _, after in pairs)
    for _ in range(60):
        rows = [tuple(rng.choice([None, 1, 2, 3]) for _ in range(3)) for _ in range(rng.randint(0, 6))]
        rows += rows[:2]  # exact duplicate rows
        results = run(rows, *[sql for pair in pairs for sql in pair])
        for index, (before, after) in enumerate(pairs):
            assert results[2 * index] == results[2 * index + 1], (before, after, rows)
