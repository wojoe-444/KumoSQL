"""``unnest_literals.split_unnest_literals``: a cross join with UNNEST of an array literal as a union (BigQuery)."""

from collections import Counter

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.unnest_literals import split_unnest_literals

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"], "v": ["a", "e"], "w": ["a", "f"]}
TYPES = {
    "t": {"a": "INT64", "b": "INT64", "c": "INT64"},
    "u": {"a": "INT64", "b": "INT64", "c": "INT64"},
    "v": {"a": "INT64", "e": "INT64"},
    "w": {"a": "INT64", "f": "FLOAT64"},
}

# NULL elements, duplicates, an empty table
DATASETS = [
    {"t": [], "u": []},
    {"t": [(1, None, 2), (1, None, 2), (None, None, None)], "u": [(1, 1, 1), (None, 0, None)]},
    {"t": [(0, 1, 2), (3, 2, None), (None, 3, 3), (0, 1, 2)], "u": [(None, 0, 0), (3, 3, 3), (0, None, 1), (0, None, 1)]},
    {"t": [(2, 2, 2)], "u": []},
    {"t": [], "u": [(2, 0, 2), (0, 2, 0)]},
]


def _proof(left: str, right: str):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False)


def _rule(sql: str) -> str:
    return split_unnest_literals(sqlglot.parse_one(sql, read="bigquery"), SCHEMA, TYPES).sql(dialect="bigquery")


def _unchanged(sql: str) -> bool:
    return _rule(sql) == sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")


def _same_bags_on_duckdb(left: str, right: str) -> bool:
    db = duckdb.connect()
    for table in ("t", "u"):
        db.execute(f"CREATE TABLE {table} (a BIGINT, b BIGINT, c BIGINT)")
    for dataset in DATASETS:
        for table, rows in dataset.items():
            db.execute(f"DELETE FROM {table}")
            if rows:
                db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows)
        a, b = run_unoptimized(db, *(sqlglot.transpile(q, read="bigquery", write="duckdb")[0] for q in (left, right)))
        if Counter(a) != Counter(b):
            return False
    return True


def test_split_into_one_select_per_element():
    assert _rule("SELECT x.a AS a, e AS b FROM u AS x CROSS JOIN UNNEST([x.a, x.b, 2]) AS e WHERE e IS NOT NULL") == (
        "SELECT x.a AS a, x.a AS b FROM u AS x WHERE NOT x.a IS NULL"
        " UNION ALL SELECT x.a AS a, x.b AS b FROM u AS x WHERE NOT x.b IS NULL"
        " UNION ALL SELECT x.a AS a, 2 AS b FROM u AS x WHERE NOT 2 IS NULL"
    )
    # a bare ``e`` keeps its output name; DISTINCT removes duplicates across all the branches
    assert _rule("SELECT DISTINCT e FROM u AS x, UNNEST([x.a, -1]) AS e") == (
        "SELECT x.a AS e FROM u AS x UNION DISTINCT SELECT -1 AS e FROM u AS x"
    )
    # one element makes no union, so the select keeps its DISTINCT (rows of u repeat)
    assert _rule("SELECT DISTINCT x.a AS a, e AS e FROM u AS x CROSS JOIN UNNEST([2]) AS e") == "SELECT DISTINCT x.a AS a, 2 AS e FROM u AS x"
    # an operand of a set operation stays one operand
    assert _rule("SELECT 1 AS k FROM t AS z EXCEPT DISTINCT SELECT e FROM u AS x, UNNEST([x.a, x.b]) AS e") == (
        "SELECT 1 AS k FROM t AS z EXCEPT DISTINCT (SELECT x.a AS e FROM u AS x UNION ALL SELECT x.b AS e FROM u AS x)"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x.a AS a, e AS b, x.c AS c FROM u AS x CROSS JOIN UNNEST([x.a, x.b, 2]) AS e WHERE e IS NOT NULL",
        "SELECT e FROM u AS x, UNNEST([x.a, x.b]) AS e",
        "SELECT DISTINCT e FROM u AS x, UNNEST([x.a, -1]) AS e",
        "SELECT DISTINCT x.a AS a, e AS e FROM u AS x CROSS JOIN UNNEST([2]) AS e",
        "SELECT DISTINCT e AS e FROM t AS z JOIN u AS x ON x.a = z.b CROSS JOIN UNNEST([x.c]) AS e WHERE e > 1 OR x.a IS NULL",
        "SELECT 1 AS k FROM t AS z EXCEPT DISTINCT SELECT e FROM u AS x, UNNEST([x.a, x.b]) AS e",
        "SELECT e, y.b FROM u AS x, UNNEST([x.a, x.b]) AS e LEFT JOIN t AS y ON y.a = e",
        "SELECT x.a FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e CROSS JOIN UNNEST([e, 2]) AS f WHERE f > 0",
        "SELECT x.a, e FROM t AS y JOIN u AS x ON x.a = y.b CROSS JOIN UNNEST([y.c, x.c, 1, 1]) AS e WHERE e > x.b OR e IS NULL",
        "SELECT y.a FROM t AS y WHERE y.a IN (SELECT e FROM u AS x CROSS JOIN UNNEST([x.a, x.c]) AS e WHERE e <> 2)",
        "SELECT COUNT(*) AS n, SUM(d.b) AS s FROM (SELECT e AS b FROM u AS x CROSS JOIN UNNEST([x.a, x.b, 3]) AS e) AS d",
    ],
)
def test_rewrite_keeps_the_rows_on_duckdb(sql):
    rewritten = _rule(sql)
    assert "UNNEST" not in rewritten
    assert _same_bags_on_duckdb(sql, rewritten)


def test_split_proves():
    assert _proof(
        "SELECT x.a AS a, e AS b FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e WHERE e IS NOT NULL",
        "SELECT x.a AS a, x.a AS b FROM u AS x WHERE x.a IS NOT NULL UNION ALL SELECT x.a AS a, x.b AS b FROM u AS x WHERE x.b IS NOT NULL",
    ).proven
    assert _proof(
        "SELECT DISTINCT e AS b FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e",
        "SELECT x.a AS b FROM u AS x UNION DISTINCT SELECT x.b AS b FROM u AS x",
    ).proven


def test_literal_element_the_filter_cannot_reach_proves():
    left = "SELECT d.a AS a FROM (SELECT x.a AS a, e AS b FROM u AS x CROSS JOIN UNNEST([x.a, 2]) AS e) AS d WHERE d.b = 0"
    assert _proof(left, left.replace("[x.a, 2]", "[x.a, 3]")).proven


def test_literal_element_the_filter_reaches_does_not_prove():
    left = "SELECT d.a AS a FROM (SELECT x.a AS a, e AS b FROM u AS x CROSS JOIN UNNEST([x.a, 2]) AS e) AS d WHERE d.b = 2"
    assert not _proof(left, left.replace("[x.a, 2]", "[x.a, 3]")).proven


def test_left_join_unnest_does_not_prove():
    # LEFT JOIN .. ON keeps every row of u, padded where the ON fails
    sql = "SELECT x.a AS a, e AS b FROM u AS x LEFT JOIN UNNEST([x.a, x.b]) AS e ON e > 0"
    assert _unchanged(sql)
    assert not _proof(
        sql, "SELECT x.a AS a, x.a AS b FROM u AS x WHERE x.a > 0 UNION ALL SELECT x.a AS a, x.b AS b FROM u AS x WHERE x.b > 0"
    ).proven


def test_with_offset_does_not_prove():
    sql = "SELECT e AS b FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e WITH OFFSET AS o WHERE o = 0"
    assert _unchanged(sql)
    assert not _proof(sql, "SELECT x.a AS b FROM u AS x UNION ALL SELECT x.b AS b FROM u AS x").proven


def test_global_aggregate_over_the_unnest_does_not_prove():
    sql = "SELECT COUNT(*) AS n FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e"
    assert _unchanged(sql)
    assert not _proof(sql, "SELECT COUNT(*) AS n FROM u AS x").proven


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT e FROM u AS x CROSS JOIN UNNEST(x.a) AS e",  # an array column
        "SELECT e FROM u AS x CROSS JOIN UNNEST(GENERATE_ARRAY(1, 3)) AS e",
        "SELECT e FROM u AS x CROSS JOIN UNNEST(ARRAY(SELECT y.a FROM t AS y)) AS e",
        "SELECT e FROM u AS x CROSS JOIN UNNEST(ARRAY<INT64>[1, 2]) AS e",
        "SELECT e FROM u AS x JOIN UNNEST([x.a, x.b]) AS e ON e > 0",
        "SELECT e FROM u AS x CROSS JOIN UNNEST([x.a, NULL]) AS e",
        "SELECT e FROM u AS x CROSS JOIN UNNEST([x.a, 1.5]) AS e",  # coerced to FLOAT64
        "SELECT e FROM w AS x CROSS JOIN UNNEST([x.f, 1]) AS e",
        "SELECT e FROM w AS x CROSS JOIN UNNEST([x.f, x.a]) AS e",
        "SELECT e FROM u AS x CROSS JOIN UNNEST([a, 1]) AS e",  # unqualified element
        "SELECT x.a FROM u AS x CROSS JOIN UNNEST([y.a, 1]) AS e CROSS JOIN t AS y",  # a later source
        "SELECT x.a FROM v AS x CROSS JOIN UNNEST([x.a, 1]) AS e",  # v has a column e
        "SELECT x.a FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e WHERE EXISTS (SELECT 1 FROM t AS y WHERE y.a = e)",
        "SELECT x.a FROM u AS x JOIN t AS y ON y.a = e CROSS JOIN UNNEST([x.a, 1]) AS e",  # e before the UNNEST
        "SELECT x.a FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e ORDER BY e LIMIT 1",
        "SELECT e FROM u AS x, UNNEST([x.a, x.b]) AS e RIGHT JOIN t AS y ON y.a = e",
        "SELECT * FROM u AS x CROSS JOIN UNNEST([x.a, 1]) AS e",
        "SELECT ARRAY(SELECT e FROM u AS x CROSS JOIN UNNEST([x.a, 1]) AS e) AS arr FROM t",
        "SELECT x.a, SUM(e) OVER () AS s FROM u AS x CROSS JOIN UNNEST([x.a, 1]) AS e",
    ],
)
def test_declined_shapes(sql):
    assert _unchanged(sql)


def test_other_dialects_are_left_alone():
    tree = sqlglot.parse_one("SELECT e FROM u AS x CROSS JOIN UNNEST([x.a, x.b]) AS e", read="bigquery")
    assert split_unnest_literals(tree.copy(), SCHEMA, TYPES, dialect="duckdb") == tree
