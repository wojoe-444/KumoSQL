"""ROLLUP / CUBE / GROUPING SETS with ORDER BY / LIMIT: the tail moves one level up (``kumosql.tailed_grouping_sets``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.tailed_grouping_sets import isolate_tailed_grouping_sets

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
TYPES = {name: {column: "INT" for column in columns} for name, columns in SCHEMA.items()}


def isolated(sql, dialect="postgres"):
    tree = sqlglot.parse_one(sql, read=dialect)
    out = isolate_tailed_grouping_sets(tree)
    return out.sql(dialect=dialect)


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="postgres", compare_names=False).proven


def rows_on_duckdb(queries, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name in SCHEMA:
        db.execute(f"CREATE TABLE {name} (a BIGINT, b BIGINT, c BIGINT)")
        insert_rows(db, name, rows.get(name, []))
    db.execute("SET default_null_order = 'nulls_last_on_asc_first_on_desc'")  # PostgreSQL's order
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *(sqlglot.transpile(q, read="postgres", write="duckdb")[0] for q in queries))]


def differ_on_duckdb(left, right, rows):
    a, b = rows_on_duckdb([left, right], rows)
    return a != b


def test_the_tail_moves_onto_a_select_around_the_grouped_one():
    assert isolated("SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, s DESC LIMIT 100") == (
        "SELECT kqt.a AS a, kqt.b AS b, kqt.s AS s FROM (SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b)) AS kqt "
        "ORDER BY kqt.a, kqt.s DESC LIMIT 100"
    )
    # an expression the select list holds, a qualified output column, a key computed over the groups, a constant
    assert isolated(
        "SELECT t.a, b AS bb, SUM(c) AS s FROM t GROUP BY CUBE (t.a, b) ORDER BY SUM(c) DESC NULLS LAST, t.a, MIN(c) + GROUPING(b), 1 OFFSET 2"
    ) == (
        "SELECT kqt.a AS a, kqt.bb AS bb, kqt.s AS s FROM (SELECT t.a, b AS bb, SUM(c) AS s, MIN(c) + GROUPING(b) AS kq_tail0 FROM t GROUP BY CUBE (t.a, b)) AS kqt "
        "ORDER BY kqt.s DESC NULLS LAST, kqt.a, kqt.kq_tail0, 1 OFFSET 2"
    )
    # a bare name that is no output is an input column: a hidden key
    assert isolated("SELECT a, SUM(c) AS s FROM t GROUP BY GROUPING SETS ((a, b), (a)) ORDER BY b LIMIT 3") == (
        "SELECT kqt.a AS a, kqt.s AS s FROM (SELECT a, SUM(c) AS s, b AS kq_tail0 FROM t GROUP BY GROUPING SETS ((a, b), (a))) AS kqt ORDER BY kqt.kq_tail0 LIMIT 3"
    )
    # inside a derived table too
    assert isolated("SELECT d.a FROM (SELECT a, SUM(c) AS s FROM t GROUP BY ROLLUP (a) ORDER BY s LIMIT 2) AS d") == (
        "SELECT d.a FROM (SELECT kqt.a AS a, kqt.s AS s FROM (SELECT a, SUM(c) AS s FROM t GROUP BY ROLLUP (a)) AS kqt ORDER BY kqt.s LIMIT 2) AS d"
    )


@pytest.mark.parametrize("sql", [
    "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b)",  # no tail
    "SELECT a, b, SUM(c) AS s FROM t GROUP BY a, b ORDER BY a LIMIT 5",  # no grouping sets
    "SELECT DISTINCT a, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a LIMIT 5",
    "SELECT a, b, SUM(c) FROM t GROUP BY ROLLUP (a, b) ORDER BY a LIMIT 5",  # an output without a name
    "SELECT a, b AS a, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY s LIMIT 5",  # two outputs named a
    "SELECT a, b, SUM(c) AS s, RANK() OVER (ORDER BY SUM(c)) AS r FROM t GROUP BY ROLLUP (a, b) ORDER BY a LIMIT 5",
    "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY RANK() OVER (ORDER BY SUM(c)) LIMIT 5",
    "SELECT a + 1 AS b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY b + 1 LIMIT 5",  # b: the input column or the output?
    "SELECT a, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY (SELECT MAX(u.a) FROM u) LIMIT 5",
    "SELECT kqt.a, SUM(kqt.c) AS s FROM t AS kqt GROUP BY ROLLUP (kqt.a, kqt.b) ORDER BY s LIMIT 5",  # the alias is taken
    "SELECT a, SUM(c) AS s FROM t WHERE t.b = kqt.b GROUP BY ROLLUP (a) ORDER BY s LIMIT 1",  # read from an enclosing query's kqt
    "SELECT a, SUM(c) AS kq_tail0 FROM t GROUP BY ROLLUP (a, b) ORDER BY a LIMIT 5",
    "WITH w AS (SELECT * FROM t) SELECT a, SUM(c) AS s FROM w GROUP BY ROLLUP (a, b) ORDER BY a LIMIT 5",  # the WITH sits on the select
])
def test_other_shapes_stay(sql):
    assert isolated(sql) == sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres")


ROLLUP = "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, b LIMIT 10"
TARGETS = [
    (ROLLUP, ROLLUP),
    (ROLLUP, "SELECT x.a, x.b, SUM(x.c) AS s FROM t AS x GROUP BY ROLLUP (x.a, x.b) ORDER BY x.a, x.b LIMIT 10"),
    # the rollup spelled as grouping sets, the sort keys named by position
    (ROLLUP, "SELECT a, b, SUM(c) AS s FROM t GROUP BY GROUPING SETS ((a, b), (a), ()) ORDER BY 1, 2 LIMIT 10"),
    # the rollup spelled as the union of its three groupings, sorted one level up
    (
        ROLLUP,
        "SELECT d.a, d.b, d.s FROM (SELECT a, b, SUM(c) AS s FROM t GROUP BY a, b UNION ALL SELECT a, NULL AS b, SUM(c) AS s FROM t GROUP BY a "
        "UNION ALL SELECT NULL AS a, NULL AS b, SUM(c) AS s FROM t) AS d ORDER BY d.a, d.b LIMIT 10",
    ),
    # a join below, written with a comma or JOIN; sorted by the aggregate
    (
        "SELECT t.a, u.b, SUM(u.c) AS s FROM t, u WHERE t.a = u.a AND t.c > 1 GROUP BY ROLLUP (t.a, u.b) ORDER BY SUM(u.c) DESC, t.a, u.b LIMIT 5",
        "SELECT t.a, u.b, SUM(u.c) AS s FROM t JOIN u ON t.a = u.a WHERE t.c > 1 GROUP BY ROLLUP (t.a, u.b) ORDER BY s DESC, t.a, u.b LIMIT 5",
    ),
]


@pytest.mark.parametrize("left, right", TARGETS)
def test_tailed_grouping_sets_prove(left, right):
    assert proved(left, right)


ROWS = {"t": [(1, 1, 10), (1, 2, 20), (2, 1, 5), (2, 1, 7), (3, None, 1)], "u": [(1, 1, 1), (2, 2, 2)]}
NEAR_MISSES = [
    (ROLLUP, "SELECT a, b, SUM(c) AS s FROM t GROUP BY a, b ORDER BY a, b LIMIT 10"),  # no subtotals
    (ROLLUP, "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (b, a) ORDER BY a, b LIMIT 10"),  # subtotals by b
    (ROLLUP, "SELECT a, b, SUM(c) AS s FROM t GROUP BY CUBE (a, b) ORDER BY a, b LIMIT 10"),
    (ROLLUP, "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a DESC, b LIMIT 2"),  # the other end, and fewer rows
    (
        "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, b LIMIT 3",
        "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, b LIMIT 4",
    ),
    # the cut taken inside each grouping instead of over all of them
    (
        "SELECT a, b, SUM(c) AS s FROM t GROUP BY ROLLUP (a, b) ORDER BY a, b LIMIT 2",
        "SELECT d.a, d.b, d.s FROM ((SELECT a, b, SUM(c) AS s FROM t GROUP BY a, b ORDER BY a, b LIMIT 2) UNION ALL (SELECT a, NULL AS b, SUM(c) AS s FROM t GROUP BY a ORDER BY a LIMIT 2) "
        "UNION ALL SELECT NULL AS a, NULL AS b, SUM(c) AS s FROM t) AS d",
    ),
    # sorted by an output named like an input column: the output (the sum), not the column
    (
        "SELECT a, SUM(c) AS b FROM t GROUP BY ROLLUP (a) ORDER BY b DESC LIMIT 1",
        "SELECT a, SUM(c) AS b FROM t GROUP BY ROLLUP (a) ORDER BY a LIMIT 1",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


# Sort keys are total over the output rows (grouping keys with GROUPING() to tell a subtotal's NULL from a
# data NULL), so the rows under LIMIT do not depend on how ties would fall.
ISOLATED_QUERIES = [
    "SELECT a, b, SUM(c) AS s, GROUPING(a) AS ga, GROUPING(b) AS gb FROM t GROUP BY ROLLUP (a, b) ORDER BY ga, gb, a, b LIMIT 4",
    "SELECT a, b, COUNT(*) AS n, GROUPING(a) + GROUPING(b) AS g FROM t GROUP BY CUBE (a, b) ORDER BY GROUPING(a) DESC, GROUPING(b), a NULLS FIRST, b DESC LIMIT 5 OFFSET 1",
    "SELECT a, MAX(c) AS m FROM t WHERE c IS NOT NULL GROUP BY GROUPING SETS ((a), ()) HAVING COUNT(*) > 0 ORDER BY GROUPING(a), a",
    "SELECT t.a, u.b, SUM(u.c) AS s FROM t JOIN u ON t.a = u.a GROUP BY ROLLUP (t.a, u.b) ORDER BY GROUPING(t.a), GROUPING(u.b), t.a, u.b LIMIT 3",
    "SELECT a, COUNT(b) AS n FROM t GROUP BY ROLLUP (a) ORDER BY GROUPING(a), a LIMIT 1",  # over no rows too: the grand total
]


def random_rows(rng):
    def table(size):
        rows = [(rng.choice([None, 1, 2, 3]), rng.choice([None, 1, 2]), rng.choice([None, 0, 1, 5])) for _ in range(rng.randint(0, size))]
        return rows + rows[:2]  # with duplicate rows

    return {"t": table(6), "u": table(4)}


@pytest.mark.parametrize("sql", ISOLATED_QUERIES)
def test_isolated_queries_give_the_same_rows(sql):
    out = isolated(sql)
    assert "kqt" in out
    rng = random.Random(len(sql))
    for rows in [{}, {"t": [(None, None, None)]}] + [random_rows(rng) for _ in range(14)]:
        assert not differ_on_duckdb(sql, out, rows)


def test_normalize_reads_the_isolated_rollup_as_a_union():
    out = normalize(ROLLUP, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "UNION ALL" in out and "GROUPING SETS" not in out and out.endswith("LIMIT 10")
