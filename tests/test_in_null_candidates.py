"""``in_null_candidates.drop_null_candidate_filters``: an IN subquery's filter that only drops NULL candidates."""

from collections import Counter

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.duckdb_load import run_unoptimized
from kumosql.in_null_candidates import drop_null_candidate_filters

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"], "f": ["a", "b", "c"]}
TYPES = {
    "t": {"a": "INT64", "b": "INT64", "c": "INT64"},
    "u": {"a": "INT64", "b": "INT64", "c": "INT64"},
    "f": {"a": "INT64", "b": "INT64", "c": "FLOAT64"},
}

# a partition whose c is all NULL (its MIN is NULL), NULL probes, duplicates, an empty table
DATASETS = [
    {"t": [], "u": []},
    {"t": [(1, 0, 0), (None, 0, 0), (2, 1, 1)], "u": [(5, 0, None), (5, 1, None), (1, 0, 1), (1, 0, 1)]},
    {"t": [(3, None, 2), (0, 2, 2)], "u": [(None, 0, None), (0, None, 3), (0, 1, None), (2, 2, 0)]},
    {"t": [(0, 0, 0)], "u": [(1, 1, None)]},
    {"t": [], "u": [(0, 0, 0)]},
    {"t": [(1, 0, 0)], "u": [(5, None, 1)]},
]

WINDOWED = "(SELECT x.a AS a, {fn}(x.c) OVER ({over}) AS b, x.c AS c FROM {table} AS x) AS y"


def _subquery(fn: str = "MIN", over: str = "PARTITION BY x.a", table: str = "u", where: str = "y.b >= y.b") -> str:
    return f"(SELECT y.c FROM {WINDOWED.format(fn=fn, over=over, table=table)} WHERE {where})"


def _proof(left: str, right: str):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False)


def _rule(sql: str) -> str:
    return drop_null_candidate_filters(sqlglot.parse_one(sql, read="bigquery"), TYPES).sql(dialect="bigquery")


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


def test_filter_is_dropped():
    assert _rule(f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery(where='y.b >= y.b AND y.a > 0')}") == (
        "SELECT z.a FROM t AS z WHERE z.a IN (SELECT y.c FROM (SELECT x.a AS a, MIN(x.c) OVER (PARTITION BY x.a) AS b,"
        " x.c AS c FROM u AS x) AS y WHERE y.a > 0)"
    )


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery()}",
        f"SELECT z.a FROM t AS z WHERE z.b > 0 AND z.a IN {_subquery(fn='MAX', over='PARTITION BY x.a ORDER BY x.b', where='y.a > 0 AND NOT y.b IS NULL')}",
        f"SELECT z.a FROM t AS z WHERE (z.a IN {_subquery(over='ORDER BY x.b DESC', where='y.b = y.b AND y.c <> 2')})",
        f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery(over='', where='y.b <= y.b')}",
    ],
)
def test_rewrite_keeps_the_rows_on_duckdb(sql):
    rewritten = _rule(sql)
    assert rewritten != sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")
    assert _same_bags_on_duckdb(sql, rewritten)


def test_dropped_filter_proves():
    left = f"SELECT z.a AS a, z.b AS b FROM u AS z WHERE z.a IN {_subquery()}"
    assert _proof(left, "SELECT z.a AS a, z.b AS b FROM u AS z WHERE z.a IN (SELECT y.c FROM u AS y)").proven


@pytest.mark.parametrize(
    "sql",
    [
        # NOT IN: a NULL candidate turns FALSE into UNKNOWN, which NOT keeps UNKNOWN
        f"SELECT z.a FROM t AS z WHERE z.a NOT IN {_subquery()}",
        f"SELECT z.a FROM t AS z WHERE NOT (z.a IN {_subquery()})",
        # a value, not a filter
        f"SELECT z.a, z.a IN {_subquery()} AS hit FROM t AS z",
        # a frame without the current row
        f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery(over='PARTITION BY x.a ORDER BY x.b ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING')}",
        # the window reads another column than the candidate
        "SELECT z.a FROM t AS z WHERE z.a IN (SELECT y.c FROM (SELECT x.a AS a, MIN(x.b) OVER (PARTITION BY x.a) AS b, x.c AS c FROM u AS x) AS y WHERE y.b >= y.b)",
        # not MIN or MAX
        f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery(fn='SUM', over='PARTITION BY x.a ORDER BY x.b ROWS BETWEEN 1 FOLLOWING AND 1 FOLLOWING')}",
        # a test that is not ``b IS NOT NULL``
        f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery(where='y.b > y.b')}",
        # FLOAT64: NaN >= NaN is FALSE, so the comparison also drops NaN candidates
        f"SELECT z.a FROM t AS z WHERE z.a IN {_subquery(table='f')}",
        # a named window (its frame is elsewhere)
        "SELECT z.a FROM t AS z WHERE z.a IN (SELECT y.c FROM (SELECT x.a AS a, MIN(x.c) OVER w AS b, x.c AS c FROM u AS x"
        " WINDOW w AS (PARTITION BY x.a ORDER BY x.b ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING)) AS y WHERE y.b >= y.b)",
    ],
)
def test_declined_shapes(sql):
    assert _unchanged(sql)


def test_not_in_with_the_filter_dropped_does_not_prove():
    left = f"SELECT z.a AS a FROM t AS z WHERE z.a NOT IN {_subquery()}"
    right = "SELECT z.a AS a FROM t AS z WHERE z.a NOT IN (SELECT y.c FROM u AS y)"
    assert not _proof(left, right).proven
    assert not _same_bags_on_duckdb(left, right)


def test_frame_without_the_current_row_does_not_prove():
    left = f"SELECT z.a AS a FROM t AS z WHERE z.a IN {_subquery(over='PARTITION BY x.a ORDER BY x.b ROWS BETWEEN 1 FOLLOWING AND 2 FOLLOWING')}"
    right = "SELECT z.a AS a FROM t AS z WHERE z.a IN (SELECT y.c FROM u AS y)"
    assert not _proof(left, right).proven
    assert not _same_bags_on_duckdb(left, right)


def test_window_over_another_column_does_not_prove():
    left = "SELECT z.a AS a FROM t AS z WHERE z.a IN (SELECT y.c FROM (SELECT x.a AS a, MIN(x.b) OVER (PARTITION BY x.a) AS b, x.c AS c FROM u AS x) AS y WHERE y.b >= y.b)"
    right = "SELECT z.a AS a FROM t AS z WHERE z.a IN (SELECT y.c FROM u AS y)"
    assert not _proof(left, right).proven
    assert not _same_bags_on_duckdb(left, right)
