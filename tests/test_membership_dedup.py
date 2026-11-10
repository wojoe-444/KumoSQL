"""Duplicate removal under an IN or EXISTS subquery is read as none (``kumosql.membership_dedup``)."""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.membership_dedup import relax_membership_dedup

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
TYPES = {name: {column: "INT64" for column in "abc"} for name in SCHEMA}
DUPLICATES = {"t": [(1, 2, 1), (1, 2, 1), (2, 1, 1), (3, 3, None)], "u": [(1, 1, 1), (2, 2, 2), (3, 3, 3), (6, 6, 6), (None, 0, 0)]}
UNION = "SELECT a FROM t UNION DISTINCT SELECT b FROM t"


def relaxed(sql, dialect="bigquery"):
    out = relax_membership_dedup(sqlglot.parse_one(sql, read=dialect))
    return out.sql(dialect=dialect) if out is not None else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False).proven


def differ_on_duckdb(left, right, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name in SCHEMA:
        db.execute(f"CREATE TABLE {name} (a BIGINT, b BIGINT, c BIGINT)")
        insert_rows(db, name, rows.get(name, []))
    queries = [sqlglot.transpile(sql, read="bigquery", write="duckdb")[0] for sql in (left, right)]
    a, b = run_unoptimized(db, *queries)
    return sorted(map(repr, a)) != sorted(map(repr, b))


def test_union_and_distinct_on_the_membership_path_are_relaxed():
    assert relaxed(f"SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT DISTINCT x.a FROM ({UNION}) AS x WHERE x.a > 0) AS y)") == (
        "SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT x.a FROM (SELECT a FROM t UNION ALL SELECT b FROM t) AS x WHERE x.a > 0) AS y)"
    )
    assert relaxed(f"SELECT a FROM u WHERE EXISTS (SELECT 1 FROM ({UNION}) AS x WHERE x.a = u.a)") == (
        "SELECT a FROM u WHERE EXISTS(SELECT 1 FROM (SELECT a FROM t UNION ALL SELECT b FROM t) AS x WHERE x.a = u.a)"
    )
    assert relaxed("SELECT a FROM u WHERE a IN (SELECT DISTINCT a FROM t)") == "SELECT a FROM u WHERE a IN (SELECT a FROM t)"


@pytest.mark.parametrize("sql, dialect", [
    (f"SELECT a FROM u WHERE a IN (SELECT z.a FROM ({UNION}) AS z ORDER BY z.a LIMIT 2)", "bigquery"),
    (f"SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT z.a FROM ({UNION}) AS z LIMIT 1) AS y)", "bigquery"),
    (f"SELECT a FROM u WHERE a IN (SELECT COUNT(*) FROM ({UNION}) AS z)", "bigquery"),
    (f"SELECT a FROM u WHERE a IN (SELECT z.a FROM ({UNION}) AS z GROUP BY z.a HAVING COUNT(*) = 1)", "bigquery"),
    (f"SELECT a FROM u WHERE a IN (SELECT ROW_NUMBER() OVER (ORDER BY z.a) FROM ({UNION}) AS z)", "bigquery"),
    (f"SELECT a FROM u WHERE a IN (SELECT y.r FROM (SELECT RAND() AS r FROM ({UNION}) AS x) AS y)", "bigquery"),
    (f"SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT x.a FROM ({UNION}) AS x JOIN u ON TRUE) AS y)", "bigquery"),
    ("SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT a FROM t EXCEPT DISTINCT SELECT a FROM u) AS y)", "bigquery"),
    ("SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT DISTINCT ON (b) a, b FROM t) AS y)", "postgres"),
    ("SELECT a FROM u WHERE a IN ((SELECT DISTINCT a FROM t))", "bigquery"),  # may read as one scalar subquery
    ("SELECT a FROM u WHERE a IN ((SELECT DISTINCT a FROM t), 3)", "bigquery"),
    (f"SELECT (SELECT COUNT(*) FROM ({UNION}) AS z) AS n FROM u", "bigquery"),
    (f"SELECT z.a FROM ({UNION}) AS z", "bigquery"),
])
def test_paths_that_count_or_pick_rows_stay(sql, dialect):
    assert relaxed(sql, dialect) is None


def test_union_distinct_under_in_proves():
    # unsafe_fuzz dev case mut-flip-union-all-23-5
    branch = "SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DATE_DIFF(DATE_ADD(DATE '2024-01-01', INTERVAL x.a DAY), DATE '2024-01-01', DAY) AS a, x.b AS b, x.c AS c FROM t AS x) AS x"
    query = (
        f"WITH c0 AS ({branch} UNION ALL SELECT y.a AS a, y.b AS b, y.c AS c FROM t AS y), dup AS ({branch} {{op}} SELECT y.a AS a, y.b AS b, y.c AS c FROM t AS y) "
        "SELECT x.a AS a, x.b AS b, x.c AS c FROM u AS x WHERE x.a IN (SELECT y.a FROM (SELECT COALESCE(x.a, 3) AS a, IFNULL(x.b, x.c) AS b, x.c AS c FROM dup AS x) AS y WHERE y.c >= y.c)"
    )
    assert proved(query.format(op="UNION ALL"), query.format(op="UNION DISTINCT"))


@pytest.mark.parametrize("template", [
    "SELECT u.a FROM u WHERE u.a IN (SELECT y.a FROM (SELECT COALESCE(x.a, 3) AS a, x.c AS c FROM (SELECT a, b, c FROM t {op} SELECT b, a, c FROM t) AS x) AS y WHERE y.c > 0)",
    "SELECT u.a FROM u WHERE u.a NOT IN (SELECT y.a FROM (SELECT x.a + 1 AS a FROM (SELECT a FROM t {op} SELECT b FROM t) AS x) AS y)",
])
def test_nested_union_distinct_under_membership_proves(template):
    assert proved(template.format(op="UNION ALL"), template.format(op="UNION DISTINCT"))


NEAR_MISSES = [
    # a LIMIT under the IN keeps the first rows, and repeats crowd others out
    "SELECT a FROM u WHERE a IN (SELECT z.a FROM ({}) AS z ORDER BY z.a LIMIT 2)",
    # a count of the rows sees every repeat
    "SELECT a FROM u WHERE a IN (SELECT COUNT(*) FROM ({}) AS z)",
    "SELECT (SELECT COUNT(*) FROM ({}) AS z) AS n FROM u",
    "SELECT a FROM u WHERE a IN (SELECT z.a FROM ({}) AS z GROUP BY z.a HAVING COUNT(*) = 1)",
    "SELECT a FROM u WHERE a IN (SELECT ROW_NUMBER() OVER (ORDER BY z.a) FROM ({}) AS z)",
    # a plain FROM is no membership test
    "SELECT z.a FROM ({}) AS z",
]


@pytest.mark.parametrize("template", NEAR_MISSES)
def test_union_distinct_outside_a_membership_test_does_not_prove(template):
    left, right = template.format(UNION), template.format(UNION.replace("DISTINCT", "ALL"))
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, DUPLICATES)


RELAXED_QUERIES = [
    f"SELECT a FROM u WHERE a IN (SELECT y.a FROM ({UNION}) AS y)",
    f"SELECT a FROM u WHERE a NOT IN (SELECT y.a FROM ({UNION}) AS y)",  # NOT IN reads only which values exist, NULL included
    f"SELECT a FROM u WHERE a NOT IN (SELECT y.b FROM (SELECT DISTINCT a, b FROM t) AS y)",
    f"SELECT a FROM u WHERE NOT EXISTS (SELECT 1 FROM ({UNION}) AS y WHERE y.a = u.a)",
    f"SELECT a FROM u WHERE b IN (SELECT y.a FROM ({UNION}) AS y WHERE y.a <> u.a OR y.a IS NULL)",  # correlated
    "SELECT a FROM u WHERE (a, b) IN (SELECT DISTINCT a, b FROM t)",
]


@pytest.mark.parametrize("sql", RELAXED_QUERIES)
def test_relaxed_queries_give_the_same_rows(sql):
    rewritten = relaxed(sql)
    assert rewritten is not None
    assert not differ_on_duckdb(sql, rewritten, DUPLICATES)
    assert not differ_on_duckdb(sql, rewritten, {})  # empty tables


@pytest.mark.parametrize("template", [
    "SELECT a FROM u WHERE a IN (SELECT y.a FROM (SELECT x.a AS a FROM ({}) AS x LIMIT 1) AS y)",  # a LIMIT picks rows
    "SELECT a FROM u WHERE a IN (SELECT SUM(y.a) FROM ({}) AS y)",  # a sum sees every repeat
])
def test_more_counting_paths_stay(template):
    assert relaxed(template.format(UNION)) is None
    assert not proved(template.format(UNION), template.format(UNION.replace("DISTINCT", "ALL")))
