"""An aggregate over an outer join with ORDER BY / LIMIT reads the join as a derived table (``kumosql.tailed_outer_aggregates``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.tailed_outer_aggregates import wrap_tailed_outer_join_aggregate

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"], "w": ["a", "b", "c"]}
TYPES = {name: {column: "INT" for column in columns} for name, columns in SCHEMA.items()}


def wrapped(sql, dialect="postgres"):
    out = wrap_tailed_outer_join_aggregate(sqlglot.parse_one(sql, read=dialect))
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
    # PostgreSQL sorts NULL as the largest value; DuckDB is told to do the same
    db.execute("SET default_null_order = 'nulls_last_on_asc_first_on_desc'")
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *(sqlglot.transpile(q, read="postgres", write="duckdb")[0] for q in queries))]


def differ_on_duckdb(left, right, rows):
    a, b = rows_on_duckdb([left, right], rows)
    return a != b


def test_the_join_becomes_a_derived_table_and_the_tail_stays():
    assert wrapped("SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5") == (
        "SELECT kqj.c0 AS a, COUNT(kqj.c1) AS n FROM (SELECT t.a AS c0, u.b AS c1 FROM t LEFT JOIN u ON t.a = u.a) AS kqj "
        "GROUP BY kqj.c0 ORDER BY kqj.c0 LIMIT 5"
    )
    # a key that is an expression over the join, an aggregate, an output name and an output position
    assert wrapped(
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a WHERE t.c > 0 GROUP BY t.a, t.b "
        "ORDER BY SUM(u.c) DESC NULLS LAST, t.b + 1, n, 1 LIMIT 5 OFFSET 2"
    ) == (
        "SELECT kqj.c0 AS a, COUNT(kqj.c2) AS n FROM (SELECT t.a AS c0, t.b AS c1, u.b AS c2, u.c AS c3, t.c AS c4 FROM t LEFT JOIN u ON t.a = u.a) AS kqj "
        "WHERE kqj.c4 > 0 GROUP BY kqj.c0, kqj.c1 ORDER BY SUM(kqj.c3) DESC NULLS LAST, kqj.c1 + 1, n, 1 LIMIT 5 OFFSET 2"
    )
    # ORDER BY alone
    assert wrapped("SELECT t.a, MAX(u.b) FROM t FULL JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a DESC") == (
        "SELECT kqj.c0 AS a, MAX(kqj.c1) FROM (SELECT t.a AS c0, u.b AS c1 FROM t FULL JOIN u ON t.a = u.a) AS kqj GROUP BY kqj.c0 ORDER BY kqj.c0 DESC"
    )


@pytest.mark.parametrize("sql", [
    "SELECT t.a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a",  # no tail: the plain rule's case
    "SELECT t.a, COUNT(u.b) FROM t JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",  # no outer join
    "SELECT t.a, u.b FROM t LEFT JOIN u ON t.a = u.a ORDER BY t.a LIMIT 5",  # no aggregate
    "SELECT t.a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY b LIMIT 5",  # a bare name that is no output
    "SELECT t.a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY b + 1 LIMIT 5",
    "SELECT t.b AS x, u.b AS x, COUNT(*) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.b, u.b ORDER BY x LIMIT 5",  # two outputs named x
    "SELECT a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY a ORDER BY t.a LIMIT 5",  # a column without its source
    "SELECT DISTINCT t.a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
    "SELECT t.a, COUNT(u.b), SUM(COUNT(u.b)) OVER () FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
    "SELECT t.a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY (SELECT MAX(w.a) FROM w WHERE w.b = t.a) LIMIT 5",
    "SELECT t.a, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a WHERE EXISTS (SELECT 1 FROM w WHERE w.a = t.a) GROUP BY t.a ORDER BY t.a LIMIT 5",
    "SELECT t.*, COUNT(u.b) FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a, t.b, t.c ORDER BY t.a LIMIT 5",
])
def test_other_shapes_stay(sql):
    assert wrapped(sql) is None


TARGETS = [
    # the same query, spelled with other aliases and the join condition turned round
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
        "SELECT p.a, COUNT(q.b) AS n FROM t AS p LEFT JOIN u AS q ON q.a = p.a GROUP BY p.a ORDER BY p.a LIMIT 5",
    ),
    # sorted by the aggregate, named three ways
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY COUNT(u.b) DESC, t.a LIMIT 3",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY n DESC, 1 LIMIT 3",
    ),
    # two outer joins, a filter on the preserved side written in WHERE or pushed below the join
    (
        "SELECT t.a, COUNT(u.b) AS n, COUNT(DISTINCT w.b) AS m FROM t LEFT JOIN u ON t.a = u.a LEFT JOIN w ON t.a = w.a WHERE t.c = 1 GROUP BY t.a ORDER BY t.a DESC LIMIT 10",
        "SELECT x.a, COUNT(u.b) AS n, COUNT(DISTINCT w.b) AS m FROM (SELECT * FROM t WHERE c = 1) AS x LEFT JOIN u ON x.a = u.a LEFT JOIN w ON x.a = w.a GROUP BY x.a ORDER BY x.a DESC LIMIT 10",
    ),
    # an inner join beside the outer one, written with a comma or JOIN
    (
        "SELECT t.a, SUM(u.b) AS s FROM t JOIN w ON t.b = w.b LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 4 OFFSET 1",
        "SELECT t.a, SUM(u.b) AS s FROM t INNER JOIN w ON w.b = t.b LEFT OUTER JOIN u ON u.a = t.a GROUP BY t.a ORDER BY t.a LIMIT 4 OFFSET 1",
    ),
]


@pytest.mark.parametrize("left, right", TARGETS)
def test_tailed_outer_join_aggregates_prove(left, right):
    assert proved(left, right)


def test_a_tailed_outer_join_aggregate_proves_against_its_own_text():
    sql = "SELECT t.a, t.b, COUNT(u.b) AS n, COUNT(w.c) AS m FROM t JOIN u ON t.a = u.a LEFT JOIN w ON t.a = w.a WHERE t.c = 1 GROUP BY t.a, t.b ORDER BY t.b DESC LIMIT 10"
    assert proved(sql, sql)


ROWS = {"t": [(1, 10, 1), (2, 20, 1), (3, 30, 1), (4, 5, 1)], "u": [(1, 7, 1), (1, None, 2), (2, 8, 3), (2, 9, 4), (2, 9, 5)], "w": [(1, 1, 1)]}
NEAR_MISSES = [
    # the inner join drops the groups with no match
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
        "SELECT t.a, COUNT(u.b) AS n FROM t JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
    ),
    # COUNT(*) counts the padded row
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
        "SELECT t.a, COUNT(*) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
    ),
    # the other end of the order
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 2",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a DESC LIMIT 2",
    ),
    # another row count, another offset
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 2",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 3",
    ),
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 2",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 2 OFFSET 1",
    ),
    # ORDER BY a names the output column a (t.b here), not the input column t.a
    (
        "SELECT t.b AS a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.b, t.a ORDER BY a LIMIT 1",
        "SELECT t.b AS a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.b, t.a ORDER BY t.a LIMIT 1",
    ),
    # sorted by another aggregate
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY COUNT(u.b) DESC, t.a LIMIT 1",
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY MAX(u.c) DESC, t.a LIMIT 1",
    ),
    # the outer join on the other side
    (
        "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
        "SELECT t.a, COUNT(u.b) AS n FROM t RIGHT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 5",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


# Each query's ORDER BY ends in its grouping key, so LIMIT cuts the same rows however ties would fall.
WRAPPED_QUERIES = [
    "SELECT t.a, COUNT(u.b) AS n FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY t.a LIMIT 3",
    "SELECT t.a, COUNT(u.b) AS n, SUM(u.c) AS s FROM t LEFT JOIN u ON t.a = u.a GROUP BY t.a ORDER BY COUNT(u.b) DESC, SUM(u.c), t.a LIMIT 2 OFFSET 1",
    "SELECT t.a, t.b, COUNT(*) AS n, MIN(w.c) AS m FROM t LEFT JOIN u ON t.a = u.a AND u.b > 1 LEFT JOIN w ON w.a = u.a WHERE t.c IS NOT NULL GROUP BY t.a, t.b HAVING COUNT(*) > 0 ORDER BY n DESC, 1, 2 LIMIT 4",
    "SELECT u.a, COUNT(t.b) AS n FROM t RIGHT JOIN u ON t.a = u.a GROUP BY u.a ORDER BY u.a DESC NULLS FIRST LIMIT 3",
    "SELECT COALESCE(t.a, u.a) AS k, COUNT(*) AS n FROM t FULL JOIN u ON t.a = u.a GROUP BY COALESCE(t.a, u.a) ORDER BY k NULLS FIRST",
    "SELECT COUNT(u.b) AS n, MAX(t.b) AS m FROM t LEFT JOIN u ON t.a = u.a ORDER BY 1 LIMIT 1",  # one global group, over no rows too
]


def random_rows(rng):
    def table(size):
        rows = [(rng.choice([None, 1, 2, 3]), rng.choice([None, 0, 1, 2, 5]), rng.choice([None, 1, 2])) for _ in range(rng.randint(0, size))]
        return rows + rows[:2]  # with duplicate rows

    return {"t": table(5), "u": table(6), "w": table(3)}


@pytest.mark.parametrize("sql", WRAPPED_QUERIES)
def test_wrapped_queries_give_the_same_rows(sql):
    out = wrapped(sql)
    assert out is not None and "kqj" in out
    rng = random.Random(len(sql))
    for rows in [{}, {"t": [(None, None, None)]}] + [random_rows(rng) for _ in range(14)]:
        assert not differ_on_duckdb(sql, out, rows)
