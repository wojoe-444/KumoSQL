"""A filter above the derived table that wraps an outer join moves inside it (``kumosql.wrapped_join_filters``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.wrapped_join_filters import push_filter_into_wrapped_join

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"], "w": ["a", "b", "c"]}
TYPES = {name: {column: "INT" for column in columns} for name, columns in SCHEMA.items()}
JOIN = "(SELECT t.a AS c0, u.b AS c1, t.c AS c2, u.c AS c3 FROM t LEFT JOIN u ON t.a = u.a) AS kqj"


def pushed(sql, dialect="postgres"):
    out = push_filter_into_wrapped_join(sqlglot.parse_one(sql, read=dialect))
    return out.sql(dialect=dialect) if out is not None else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="postgres", compare_names=False).proven


def rows_on_duckdb(queries, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name in SCHEMA:
        db.execute(f"CREATE TABLE {name} (a BIGINT, b BIGINT, c BIGINT)")
        insert_rows(db, name, rows.get(name, []))
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *(sqlglot.transpile(q, read="postgres", write="duckdb")[0] for q in queries))]


def differ_on_duckdb(left, right, rows):
    a, b = rows_on_duckdb([left, right], rows)
    return a != b


def test_conditions_on_the_wrapped_join_move_inside():
    assert pushed(f"SELECT kqj.c0, COUNT(kqj.c1) FROM {JOIN} WHERE kqj.c2 = 1 GROUP BY kqj.c0") == (
        "SELECT kqj.c0, COUNT(kqj.c1) FROM (SELECT t.a AS c0, u.b AS c1, t.c AS c2, u.c AS c3 FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1) AS kqj GROUP BY kqj.c0"
    )
    # either side of the join, an OR, a condition between the sides; the tail and HAVING stay
    assert pushed(
        f"SELECT kqj.c0, COUNT(kqj.c1) FROM {JOIN} WHERE (kqj.c2 = 1 OR kqj.c3 IS NULL) AND kqj.c1 > kqj.c2 GROUP BY kqj.c0 HAVING COUNT(*) > 1 ORDER BY kqj.c0 LIMIT 3"
    ) == (
        "SELECT kqj.c0, COUNT(kqj.c1) FROM (SELECT t.a AS c0, u.b AS c1, t.c AS c2, u.c AS c3 FROM t LEFT JOIN u ON t.a = u.a "
        "WHERE (t.c = 1 OR u.c IS NULL) AND u.b > t.c) AS kqj GROUP BY kqj.c0 HAVING COUNT(*) > 1 ORDER BY kqj.c0 LIMIT 3"
    )
    # added to a WHERE the derived table has; a conjunct with a subquery stays above
    assert pushed(
        "SELECT kqj.c0 FROM (SELECT t.a AS c0, u.b AS c1 FROM t LEFT JOIN u ON t.a = u.a WHERE t.b = 2 OR t.b = 3) AS kqj "
        "WHERE kqj.c1 IS NULL AND kqj.c0 IN (SELECT w.a FROM w)"
    ) == (
        "SELECT kqj.c0 FROM (SELECT t.a AS c0, u.b AS c1 FROM t LEFT JOIN u ON t.a = u.a WHERE (t.b = 2 OR t.b = 3) AND u.b IS NULL) AS kqj "
        "WHERE kqj.c0 IN (SELECT w.a FROM w)"
    )


@pytest.mark.parametrize("sql", [
    f"SELECT kqj.c0, COUNT(kqj.c1) FROM {JOIN} GROUP BY kqj.c0",  # no filter
    "SELECT d.c0 FROM (SELECT t.a AS c0, u.b AS c1 FROM t JOIN u ON t.a = u.a) AS d WHERE d.c1 = 1",  # no outer join: other rules' case
    "SELECT d.c0 FROM (SELECT DISTINCT t.a AS c0, u.b AS c1 FROM t LEFT JOIN u ON t.a = u.a) AS d WHERE d.c1 = 1",
    "SELECT d.c0 FROM (SELECT t.a AS c0, COUNT(u.b) AS c1 FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a) AS d WHERE d.c1 = 1",  # a count is no column
    "SELECT d.c0 FROM (SELECT t.a AS c0, u.b AS c1 FROM t LEFT JOIN u ON t.a = u.a ORDER BY t.a LIMIT 5) AS d WHERE d.c1 = 1",  # the cut comes first
    "SELECT d.c0 FROM (SELECT t.a AS c0, u.b + 1 AS c1 FROM t LEFT JOIN u ON t.a = u.a) AS d WHERE d.c1 = 1",  # an expression, not a column
    "SELECT d.c0 FROM (SELECT t.a AS c0, u.b AS c0 FROM t LEFT JOIN u ON t.a = u.a) AS d WHERE d.c0 = 1",  # which c0?
    "SELECT d.c0 FROM (SELECT t.a AS c0, ROW_NUMBER() OVER (ORDER BY u.b) AS c1 FROM t LEFT JOIN u ON t.a = u.a) AS d WHERE d.c1 = 1",
    f"SELECT kqj.c0 FROM {JOIN} JOIN w ON w.a = kqj.c0 WHERE kqj.c2 = 1",  # another source beside it
    f"SELECT kqj.c0 FROM {JOIN} WHERE c2 = 1",  # a column without its source
    f"SELECT kqj.c0 FROM {JOIN} WHERE kqj.c0 IN (SELECT w.a FROM w)",
    f"SELECT kqj.c0 FROM {JOIN} WHERE 1 = 1",
])
def test_other_shapes_stay(sql):
    assert pushed(sql) is None


TARGETS = [
    # a filter on the preserved side: in WHERE, or pushed below the join
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1 GROUP BY t.a",
        "SELECT x.a, COUNT(u.b) AS n FROM (SELECT * FROM t WHERE c = 1) AS x LEFT JOIN u ON x.a = u.a GROUP BY x.a",
    ),
    # two outer joins and a COUNT(DISTINCT)
    (
        "SELECT t.a, COUNT(u.b) AS n, COUNT(DISTINCT w.b) AS m FROM t LEFT JOIN u ON t.a = u.a LEFT JOIN w ON t.a = w.a WHERE t.c = 1 AND t.b > 0 GROUP BY t.a",
        "SELECT x.a, COUNT(u.b) AS n, COUNT(DISTINCT w.b) AS m FROM (SELECT * FROM t WHERE c = 1 AND b > 0) AS x LEFT JOIN u ON x.a = u.a LEFT JOIN w ON x.a = w.a GROUP BY x.a",
    ),
    # a filter on an inner-joined table beside the outer join
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t JOIN w ON t.b = w.b LEFT JOIN u ON t.a = u.a WHERE w.c = 1 GROUP BY t.a",
        "SELECT t.a, COUNT(u.b) AS n FROM t JOIN (SELECT * FROM w WHERE c = 1) AS w ON t.b = w.b LEFT JOIN u ON t.a = u.a GROUP BY t.a",
    ),
    # under a row tail
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1 GROUP BY t.a ORDER BY t.a LIMIT 3",
        "SELECT x.a, COUNT(u.b) AS n FROM (SELECT * FROM t WHERE c = 1) AS x LEFT JOIN u ON x.a = u.a GROUP BY x.a ORDER BY x.a LIMIT 3",
    ),
    # a global aggregate
    (
        "SELECT COUNT(u.b) AS n, MAX(t.b) AS m FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1",
        "SELECT COUNT(u.b) AS n, MAX(x.b) AS m FROM (SELECT * FROM t WHERE c = 1) AS x LEFT JOIN u ON x.a = u.a",
    ),
]


@pytest.mark.parametrize("left, right", TARGETS)
def test_filters_above_and_below_the_join_prove(left, right):
    assert proved(left, right)


ROWS = {"t": [(1, 1, 1), (2, 1, 1), (3, 1, 2), (4, 1, 1)], "u": [(1, 7, 1), (1, 8, 2), (2, 9, 2), (3, 5, 1)], "w": [(1, 1, 1)]}
NEAR_MISSES = [
    # a filter on the padded side in WHERE drops the unmatched rows; below the join it only empties their match
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE u.c = 1 GROUP BY t.a",
        "SELECT t.a, COUNT(x.b) AS n FROM t LEFT JOIN (SELECT * FROM u WHERE c = 1) AS x ON t.a = x.a GROUP BY t.a",
    ),
    # IS NULL on the padded side keeps the unmatched rows only; below the join it filters u's own NULLs
    (
        "SELECT t.a, COUNT(*) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE u.c IS NULL GROUP BY t.a",
        "SELECT t.a, COUNT(*) AS n FROM t LEFT JOIN (SELECT * FROM u WHERE c IS NULL) AS x ON t.a = x.a GROUP BY t.a",
    ),
    # a preserved-side filter written in ON keeps the row, padded
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1 GROUP BY t.a",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a AND t.c = 1 GROUP BY t.a",
    ),
    # another filter, and no filter
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1 GROUP BY t.a",
        "SELECT x.a, COUNT(u.b) AS n FROM (SELECT * FROM t WHERE c = 2) AS x LEFT JOIN u ON x.a = u.a GROUP BY x.a",
    ),
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 1 GROUP BY t.a",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a",
    ),
    # a global aggregate keeps its one row; with HAVING in place of WHERE the row can go
    (
        "SELECT COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c = 9",
        "SELECT COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a HAVING MAX(t.c) = 9",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


PUSHED_QUERIES = [
    f"SELECT kqj.c0, COUNT(kqj.c1) AS n FROM {JOIN} WHERE kqj.c2 = 1 GROUP BY kqj.c0",
    f"SELECT kqj.c0, COUNT(kqj.c1) AS n, COUNT(*) AS m FROM {JOIN} WHERE kqj.c3 = 1 GROUP BY kqj.c0",  # the padded side
    f"SELECT kqj.c0, COUNT(*) AS m FROM {JOIN} WHERE kqj.c3 IS NULL AND (kqj.c2 > 0 OR kqj.c1 IS NULL) GROUP BY kqj.c0",
    f"SELECT COUNT(kqj.c1) AS n, SUM(kqj.c2) AS s FROM {JOIN} WHERE kqj.c1 <> kqj.c2",  # a global aggregate, over no rows too
    f"SELECT kqj.c0, kqj.c1 FROM {JOIN} WHERE NOT (kqj.c3 = 1) ORDER BY kqj.c0, kqj.c1",
    "SELECT d.x, COUNT(d.y) AS n FROM (SELECT u.a AS x, t.b AS y, t.c AS z FROM t RIGHT JOIN u ON t.a = u.a WHERE u.c IS NOT NULL) AS d WHERE d.z = 1 OR d.z IS NULL GROUP BY d.x",
    "SELECT d.x, d.y FROM (SELECT t.a AS x, u.a AS y, w.c AS z FROM t FULL JOIN u ON t.a = u.a LEFT JOIN w ON w.a = t.a) AS d WHERE d.x IS NULL OR d.z = 1",
]


def random_rows(rng):
    def table(size):
        rows = [(rng.choice([None, 1, 2, 3]), rng.choice([None, 0, 1, 2]), rng.choice([None, 1, 2])) for _ in range(rng.randint(0, size))]
        return rows + rows[:2]  # with duplicate rows

    return {"t": table(5), "u": table(6), "w": table(3)}


@pytest.mark.parametrize("sql", PUSHED_QUERIES)
def test_pushed_queries_give_the_same_rows(sql):
    out = pushed(sql)
    assert out is not None and out != sql
    rng = random.Random(len(sql))
    for rows in [{}, {"t": [(None, None, None)]}, ROWS] + [random_rows(rng) for _ in range(14)]:
        assert not differ_on_duckdb(sql, out, rows)
