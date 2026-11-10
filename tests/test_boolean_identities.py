"""Exact three-valued Boolean identities, and constant-TRUE filters dropped (``kumosql.boolean_identities``)."""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import canonical_negation
from kumosql.boolean_identities import fold_boolean_identities

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
TYPES = {name: {column: "INT64" for column in "abc"} for name in SCHEMA}


def folded(sql, dialect="bigquery"):
    out = fold_boolean_identities(canonical_negation(sqlglot.parse_one(sql, read=dialect)))
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


# SQLancer NoREC mutants (unsafe_fuzz dev cases norec-notnot-15/21/49)
NOREC = [
    ("SELECT COUNT(*) AS v FROM t WHERE a IS NOT NULL", "SELECT COUNT(*) AS v FROM t WHERE NOT NOT (a IS NOT NULL) OR (a IS NOT NULL) IS NULL"),
    ("SELECT COUNT(*) AS v FROM u WHERE c IS NOT NULL", "SELECT COUNT(*) AS v FROM u WHERE NOT NOT (c IS NOT NULL) OR (c IS NOT NULL) IS NULL"),
]


def test_double_negation_and_never_null_tests_fold():
    assert folded(NOREC[0][1]) == "SELECT COUNT(*) AS v FROM t WHERE NOT a IS NULL"
    assert folded("SELECT a FROM t WHERE b = 1 AND NOT NOT (a = 1 OR a = 2)") == "SELECT a FROM t WHERE b = 1 AND (a = 1 OR a = 2)"
    assert folded("SELECT a FROM t WHERE ((a IS NULL) AND (b IS TRUE)) IS NULL") == "SELECT a FROM t WHERE FALSE"
    assert folded("SELECT a FROM t WHERE (a IS DISTINCT FROM b) IS NOT NULL AND EXISTS(SELECT 1 FROM u) IS NULL") == "SELECT a FROM t WHERE FALSE"
    assert folded("SELECT NOT NOT (a > 1), (a > 1) OR FALSE, TRUE AND (a > 1) FROM t", "mysql") == "SELECT a > 1, a > 1, a > 1 FROM t"


@pytest.mark.parametrize("sql, dialect", [
    ("SELECT a FROM t WHERE (a = 1) IS NULL", "bigquery"),  # a comparison can be NULL
    ("SELECT a FROM t WHERE ((a IS NULL) AND (b = 1)) IS NULL", "bigquery"),  # TRUE AND NULL is NULL
    ("SELECT a FROM t WHERE (a IN (SELECT b FROM u)) IS NULL", "bigquery"),
    ("SELECT (a > 1 OR NULL) AS p FROM t", "bigquery"),  # NULL is no identity of OR
    ("SELECT NOT NOT a, a OR FALSE, a AND TRUE FROM t", "mysql"),  # NOT NOT 5 is 1 there, not 5
])
def test_nullable_or_non_boolean_operands_stay(sql, dialect):
    assert folded(sql, dialect) is None


def test_true_filters_are_dropped():
    assert folded("SELECT a FROM t WHERE TRUE") == "SELECT a FROM t"
    assert folded("SELECT a FROM t WHERE NOT FALSE") == "SELECT a FROM t"
    assert folded("SELECT a FROM t QUALIFY TRUE") == "SELECT a FROM t"
    assert folded("SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING TRUE") == "SELECT a, COUNT(*) AS n FROM t GROUP BY a"
    assert folded("SELECT COUNT(*) AS n FROM t HAVING TRUE") == "SELECT COUNT(*) AS n FROM t"  # one row either way


@pytest.mark.parametrize("sql", [
    "SELECT a FROM t WHERE NULL",
    "SELECT a FROM t WHERE FALSE",
    "SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING FALSE",
    "SELECT 1 AS one FROM t HAVING TRUE",  # HAVING makes it one group: one row, however many rows t has
    "SELECT SUM(a) OVER () AS s FROM t HAVING TRUE",  # a window is no aggregate
    "SELECT (SELECT COUNT(*) FROM u) AS s FROM t HAVING TRUE",  # nor is a nested one
])
def test_other_filters_stay(sql):
    assert folded(sql) is None


@pytest.mark.parametrize("left, right", NOREC)
def test_norec_double_negation_mutants_prove(left, right):
    assert proved(left, right)


def test_where_true_left_by_a_pushed_filter_no_longer_blocks_the_merge():
    # unsafe_fuzz dev case mut-drop-conjunct-this-45-3: the filter moves into the DISTINCT, leaving WHERE TRUE
    left = (
        "WITH c0 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM t AS x) AS x WHERE 1 = 1 AND (x.a <= 2)), "
        "c1 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT x.a AS a, MAX(x.b) AS b, COUNT(*) AS c FROM t AS x GROUP BY x.a) AS x), "
        "dup AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM t AS x) AS x WHERE 1 = 1 AND (x.a <= 2)) "
        "SELECT IFNULL(NULLIF(x.a, 0), x.c) AS a, x.b AS b, x.c AS c FROM dup AS x WHERE x.b > 1"
    )
    right = left.replace("dup AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM t AS x) AS x WHERE 1 = 1 AND (x.a <= 2))",
                         "dup AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM t AS x) AS x WHERE (x.a <= 2))")
    assert right != left
    assert proved(left, right)
    assert proved(
        "SELECT x.a FROM (SELECT x.a AS a, x.b AS b FROM (SELECT DISTINCT a, b FROM t) AS x WHERE TRUE) AS x WHERE x.b > 1",
        "SELECT x.a FROM (SELECT DISTINCT a, b FROM t WHERE b > 1) AS x",
    )


def test_never_null_tests_of_exists_and_is_true_prove():
    assert proved("SELECT COUNT(*) AS v FROM t WHERE a IS NOT NULL OR (EXISTS (SELECT 1 FROM u WHERE u.b = t.b)) IS NULL", "SELECT COUNT(*) AS v FROM t WHERE a IS NOT NULL")
    assert proved("SELECT COUNT(*) AS v FROM t WHERE a IS NOT NULL OR ((b > 1) IS TRUE AND c IS NULL) IS NULL", "SELECT COUNT(*) AS v FROM t WHERE a IS NOT NULL")


NEAR_MISSES = [
    # a comparison can be NULL, so its IS NULL is no constant
    ("SELECT COUNT(*) AS v FROM t WHERE (a = 1) IS NULL", "SELECT COUNT(*) AS v FROM t WHERE FALSE", {"t": [(None, 0, 0)]}),
    ("SELECT a FROM t WHERE ((a IS NULL) AND (b = 1)) IS NULL", "SELECT a FROM t WHERE FALSE", {"t": [(None, None, 0)]}),
    ("SELECT (a > 1 OR NULL) AS p FROM t", "SELECT a > 1 AS p FROM t", {"t": [(0, 0, 0)]}),
    # a NULL or FALSE filter keeps no row
    ("SELECT a FROM t WHERE NULL", "SELECT a FROM t", {"t": [(1, 1, 1)]}),
    ("SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING FALSE", "SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING TRUE", {"t": [(1, 1, 1)]}),
    # HAVING TRUE groups an aggregate-free select into one row
    ("SELECT 1 AS one FROM t HAVING TRUE", "SELECT 1 AS one FROM t", {"t": [(1, 1, 1), (2, 2, 2)]}),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right, rows):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, rows)


FOLDED_QUERIES = [
    "SELECT a, b FROM t WHERE NOT NOT (a IS NOT NULL) OR (a IS NOT NULL) IS NULL",
    "SELECT a, b FROM t WHERE NOT NOT (a > 1) AND TRUE",
    "SELECT a, b FROM t WHERE NOT ((a IS NULL) AND (b IS TRUE)) IS NULL",
    "SELECT a, b FROM t WHERE (a > b) OR FALSE",
    "SELECT (a > b) OR FALSE AS p, NOT NOT (a IN (1, 2)) AS q FROM t",
    "SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING TRUE",
    "SELECT COUNT(*) AS n FROM t HAVING TRUE",
    "SELECT a FROM t WHERE TRUE",
]
EDGE_ROWS = [{}, {"t": [(None, None, None)]}, {"t": [(1, 1, 1), (1, None, 2), (None, 3, 3), (2, 2, 2), (2, 2, 2)]}]


@pytest.mark.parametrize("sql", FOLDED_QUERIES)
def test_folded_queries_give_the_same_rows(sql):
    out = fold_boolean_identities(canonical_negation(sqlglot.parse_one(sql, read="bigquery")))
    assert out is not None
    for rows in EDGE_ROWS:
        assert not differ_on_duckdb(sql, out.sql(dialect="bigquery"), rows)
