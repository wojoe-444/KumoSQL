"""Refutations that need values to meet several constraints at once: z3-built candidates
(``bounded_refutation``), each boundary row alone in its table (``isolated_boundary_datasets``) and
the BigQuery-faithful guards they are replayed under (``executed_refutation._faithful``)."""

from collections import Counter

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

import sqlglot  # noqa: E402

from kumosql.bounded_refutation import encodable  # noqa: E402
from kumosql.duckdb_load import insert_rows, run_unoptimized  # noqa: E402
from kumosql.executed_refutation import search_counterexample  # noqa: E402
from kumosql.targeted_data import isolated_boundary_datasets, targeted_datasets  # noqa: E402

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
TYPES = {name: {c: "INT64" for c in cols} for name, cols in SCHEMA.items()}


def _bags(tables: dict, left: str, right: str, types=TYPES, optimized=True):
    db = duckdb.connect()
    for name, columns in types.items():
        db.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {_DUCK[t]}' for c, t in columns.items())})")
        insert_rows(db, name, [tuple(row.get(c) for c in columns) for row in tables.get(name, [])])
    queries = [sqlglot.transpile(sql, read="bigquery", write="duckdb")[0] for sql in (left, right)]
    rows = [db.execute(q).fetchall() for q in queries] if optimized else run_unoptimized(db, *queries)
    return [Counter(r) for r in rows]


_DUCK = {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "STRING": "VARCHAR"}


def _replays(counterexample, left, right, types=TYPES) -> bool:
    """Both queries, as written, return different bags on the counterexample, optimizer on and off."""

    a, b = _bags(counterexample.tables, left, right, types)
    c, d = _bags(counterexample.tables, left, right, types, optimized=False)
    return a != b and c != d and (a, b) == (c, d)


# A chain of CTEs: a t row whose b is the a of another t row that has a u partner with a NULL c.
_CHAIN = (
    "WITH c0 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM t AS x WHERE EXISTS (SELECT 1 FROM u AS y WHERE {c0})), "
    "c1 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM t AS x WHERE x.a IN (SELECT y.b FROM c0 AS y WHERE y.c = 3)) "
    "SELECT x.a AS a, x.b AS b, x.c AS c FROM t AS x WHERE x.a IN (SELECT y.c FROM c1 AS y{final})"
)


def _chain(c0="y.a = x.a AND y.c IS NULL", final=" WHERE y.c IN (3, 1)"):
    return _CHAIN.format(c0=c0, final=final)


_HAVING = (
    "WITH c0 AS (SELECT x.a AS a, MIN(x.b) AS b, COUNT(*) AS c FROM (SELECT x.a AS a, x.b AS b, x.c AS c FROM t AS x "
    "WHERE (x.b < 0) AND (x.c BETWEEN 0 AND 2)) AS x GROUP BY x.a HAVING COUNT(*) > 1), "
    "c1 AS (SELECT x.a AS a, y.b AS b, x.c AS c FROM (SELECT x.a AS a, CASE WHEN x.b > 3 THEN x.b ELSE x.c END AS b, "
    "x.c AS c FROM u AS x) AS x JOIN t AS y ON x.a = y.a), "
    "dup AS (SELECT x.a AS a, MIN(x.b) AS b, COUNT(*) AS c FROM (SELECT x.a AS a, x.b AS b, x.c AS c FROM u AS x "
    "WHERE EXISTS (SELECT 1 FROM c1 AS y WHERE {exists})) AS x GROUP BY x.a) "
    "SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT x.a AS a, x.b AS b, x.c AS c FROM dup AS x UNION ALL "
    "SELECT y.a AS a, y.b AS b, y.c AS c FROM c1 AS y) AS x WHERE EXISTS (SELECT 1 FROM c0 AS y WHERE y.a = x.a AND y.c = y.c)"
)

_ABOVE = (
    "WITH c0 AS (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM t AS x), "
    "c1 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT x.a AS a, SUM(x.b) AS b, COUNT(x.c) AS c FROM c0 AS x "
    "WHERE {where} GROUP BY x.a) AS x WHERE x.a IN (SELECT y.b FROM u AS y{inner})) SELECT x.a AS a, x.b AS b, x.c AS c FROM c1 AS x"
)

_QUALIFY = (
    "WITH dup AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DATE_DIFF(DATE_ADD(DATE '2024-01-01', INTERVAL x.a DAY), "
    "DATE '2024-01-01', DAY) AS a, x.b AS b, x.c AS c FROM u AS x) AS x QUALIFY {count} OVER (PARTITION BY x.a) > 2) "
    "SELECT x.a AS a, SUM(x.b) AS b, COUNT(x.c) AS c FROM dup AS x WHERE x.a < 0 GROUP BY x.a"
)

_ALONE = (
    "SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM t AS x "
    "WHERE ({cmp}) AND (x.b BETWEEN 0 AND 2)) AS x WHERE NOT EXISTS (SELECT 1 FROM u AS y WHERE y.a = x.a AND y.b = y.b)"
)

REFUTED = {
    # = against <> in the innermost EXISTS: t = {(3, 3, 3)}, u = {(0, 0, NULL)}
    "chain_swap_eq": (_chain(), _chain(c0="y.a <> x.a AND y.c IS NULL")),
    # the last filter dropped: a c1 row whose c is outside (3, 1)
    "chain_drop_where": (_chain(), _chain(final="")),
    # IN (3, 1) against IN (4, 1)
    "chain_bump_constant": (_chain(), _chain(final=" WHERE y.c IN (4, 1)")),
    # two t rows with b < 0 sharing a, and a u partner whose c is NULL
    "negative_group": (_HAVING.format(exists="y.a = x.a AND y.c >= y.c"), _HAVING.format(exists="y.a = x.a")),
    # a b above every literal
    "above_literals": (
        _ABOVE.format(where="NOT (x.b <= 3)", inner=" WHERE y.c IS NOT NULL"),
        _ABOVE.format(where="NOT (x.b <= 3)", inner=""),
    ),
    # QUALIFY COUNT(*) against COUNT(b): three rows of one partition below zero, one b NULL
    "qualify_negative_partition": (_QUALIFY.format(count="COUNT(*)"), _QUALIFY.format(count="COUNT(x.b)")),
    # >= against >: one t row at a = 3 and no u row
    "boundary_alone": (_ALONE.format(cmp="x.a >= 3"), _ALONE.format(cmp="x.a > 3")),
}


@pytest.mark.parametrize("name", sorted(REFUTED))
def test_refutes_with_a_database_that_replays(name):
    left, right = REFUTED[name]
    counterexample = search_counterexample(left, right, schema=SCHEMA, types=TYPES)
    assert counterexample is not None
    assert _replays(counterexample, left, right)
    assert sum(len(rows) for rows in counterexample.tables.values()) <= 6


NEAR_MISSES = {
    # the same literals, equivalent: IN lists reordered or spelled out
    "chain_in_reordered": (_chain(), _chain(final=" WHERE y.c IN (1, 3)")),
    "chain_in_spelled": (_chain(), _chain(final=" WHERE (y.c = 3 OR y.c = 1)")),
    # y.a = x.a already rejects a NULL y.a
    "negative_group_redundant": (
        _HAVING.format(exists="y.a = x.a AND y.a = y.a"),
        _HAVING.format(exists="y.a = x.a"),
    ),
    # NOT (b <= 3) is b > 3; c IS NOT NULL is implied by c >= 0
    "above_literals_spelled": (
        _ABOVE.format(where="NOT (x.b <= 3)", inner=" WHERE y.c >= 0"),
        _ABOVE.format(where="x.b > 3", inner=" WHERE y.c >= 0 AND y.c IS NOT NULL"),
    ),
    # the partitions COUNT(a) and COUNT(*) disagree on (a NULL) are dropped by a < 0
    "qualify_count_key": (_QUALIFY.format(count="COUNT(*)"), _QUALIFY.format(count="COUNT(x.a)")),
    # INT64: >= 3 is > 2
    "boundary_integer": (_ALONE.format(cmp="x.a >= 3"), _ALONE.format(cmp="x.a > 2")),
    # NOT (a < 3) keeps the NULL rows out as a >= 3 does
    "boundary_negated": (_ALONE.format(cmp="x.a >= 3"), _ALONE.format(cmp="NOT (x.a < 3)")),
    # a join with itself on a key column is read in either order
    "join_commuted": (
        "SELECT x.a AS a, y.b AS b FROM t AS x JOIN u AS y ON x.a = y.a WHERE x.c > 1",
        "SELECT x.a AS a, y.b AS b FROM u AS y JOIN t AS x ON y.a = x.a WHERE 1 < x.c",
    ),
    # NOT IN over a column that may be NULL against its three-valued spelling
    "not_in_spelled": (
        "SELECT x.a AS a FROM t AS x WHERE x.a NOT IN (SELECT y.b FROM u AS y)",
        "SELECT x.a AS a FROM t AS x WHERE NOT (x.a IN (SELECT y.b FROM u AS y))",
    ),
    # day arithmetic read back: the day count itself (BigQuery fails outside its date range)
    "date_round_trip": (
        "SELECT DATE_DIFF(DATE_ADD(DATE '2024-01-01', INTERVAL x.a DAY), DATE '2024-01-01', DAY) AS a FROM u AS x",
        "SELECT x.a AS a FROM u AS x",
    ),
}


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_equivalent_pairs_with_the_same_literals_are_not_refuted(name):
    left, right = NEAR_MISSES[name]
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None


def test_a_date_beyond_bigquerys_range_is_an_error_not_a_difference():
    # For a <> 0 BigQuery fails (the date leaves years 1..9999); DuckDB's dates go on. Whenever
    # BigQuery returns rows, both sides return none.
    left = "SELECT x.a AS a FROM t AS x WHERE DATE_ADD(DATE '2024-01-01', INTERVAL x.a * 10000000 DAY) > DATE '2024-01-01'"
    right = "SELECT x.a AS a FROM t AS x WHERE x.a <> x.a"
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None


def test_a_float_cast_to_int64_rounds_half_away_from_zero():
    # BigQuery: CAST(-2.5 AS INT64) = -3, so the pair is equivalent; DuckDB's cast rounds half to even (-2).
    schema, types = {"w": ["f"]}, {"w": {"f": "FLOAT64"}}
    left = "SELECT x.f AS f FROM w AS x WHERE CAST(x.f AS INT64) = -3"
    right = "SELECT x.f AS f FROM w AS x WHERE x.f > -3.5 AND x.f <= -2.5"
    assert search_counterexample(left, right, schema=schema, types=types) is None


def test_a_string_cast_to_int64_is_not_read_by_duckdb():
    # BigQuery fails on CAST('2.5' AS INT64) wherever the row exists; DuckDB returns 3.
    schema, types = {"w": ["s"]}, {"w": {"s": "STRING"}}
    left = "SELECT CAST(x.s AS INT64) AS v FROM w AS x WHERE x.s = '2.5'"
    right = "SELECT 0 AS v FROM w AS x WHERE x.s = '2.5'"
    assert search_counterexample(left, right, schema=schema, types=types) is None


def test_a_zero_divisor_inside_a_divisor_is_a_bigquery_error():
    # b / c with c = 0 fails in BigQuery before the outer division runs; whenever BigQuery
    # returns rows, both sides return none.
    left = "SELECT x.a / (x.b / x.c) AS v FROM t AS x WHERE x.c = 0 AND x.b IS NOT NULL"
    right = "SELECT 0.5 AS v FROM t AS x WHERE x.a <> x.a"
    assert search_counterexample(left, right, schema=SCHEMA, types=TYPES) is None


def test_an_integer_cast_still_refutes():
    left = "SELECT CAST(x.a AS INT64) AS v FROM t AS x"
    right = "SELECT CAST(x.b AS INT64) AS v FROM t AS x"
    counterexample = search_counterexample(left, right, schema=SCHEMA, types=TYPES)
    assert counterexample is not None and _replays(counterexample, left, right)


def test_isolated_boundary_rows_stand_alone_in_their_table():
    sql = _ALONE.format(cmp="x.a >= 3")
    schema = {"t": TYPES["t"], "u": TYPES["u"]}
    isolated = isolated_boundary_datasets(sql, schema)
    alone = [d for d in isolated if not d.dataset.tables["u"].rows and (3, 0) in {r[:2] for r in d.dataset.tables["t"].rows}]
    assert alone
    # the targeted suite always gives the boundary row a u partner (facts are kept by column name)
    for labeled in targeted_datasets(sql, schema):
        if labeled.label.startswith("boundary:a"):
            assert labeled.dataset.tables["u"].rows


@pytest.mark.parametrize(
    "sql",
    [
        _QUALIFY.format(count="COUNT(x.b)"),
        "SELECT DISTINCT x.a AS a FROM t AS x QUALIFY SUM(x.b) OVER (PARTITION BY x.a) > 1",
        "SELECT x.a AS a, x.b AS b FROM t AS x WHERE x.c > 0 QUALIFY COUNT(*) OVER (PARTITION BY x.b) = 1",
    ],
)
def test_the_encodable_form_returns_the_same_bag(sql):
    rewritten = encodable(sql)
    assert "QUALIFY" not in rewritten.upper()
    rows = {
        "t": [{"a": a, "b": b, "c": c} for a, b, c in [(1, 2, 1), (1, 2, 1), (1, None, 0), (2, 1, 1), (None, 1, 2)]],
        "u": [{"a": a, "b": b, "c": c} for a, b, c in [(-1, 0, 0), (-1, None, 0), (-1, 1, 1), (0, 1, None)]],
    }
    a, b = _bags(rows, sql, rewritten)
    assert a == b
