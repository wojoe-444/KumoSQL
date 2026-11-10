"""A correlated scalar aggregate over a WITH table or derived table is decorrelated like one over a table (``kumosql.closed_sources``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.closed_sources import closed_derived_source, inner_join_conditions, plain_inner_join

SCHEMA = {"r": ["store", "cust", "amt", "d"], "s": ["store", "state"], "c": ["cust", "grp"]}
TYPES = {name: {column: "INT" for column in columns} for name, columns in SCHEMA.items()}
CTE = "WITH ctr AS (SELECT r.cust AS ctr_cust, r.store AS ctr_store, SUM(r.amt) AS ctr_total FROM r WHERE r.d = 1 GROUP BY r.cust, r.store) "
QUERY = (
    CTE + "SELECT c.grp FROM ctr AS ctr1, s, c "
    "WHERE ctr1.ctr_total > (SELECT AVG(ctr2.ctr_total) * 1.2 FROM ctr AS ctr2 WHERE ctr1.ctr_store = ctr2.ctr_store) "
    "AND s.store = ctr1.ctr_store AND s.state = 1 AND ctr1.ctr_cust = c.cust"
)
CORRELATED_BODY = (
    "SELECT c.cust FROM c WHERE c.grp > (SELECT MAX(x.t) FROM (SELECT r.store, SUM(r.amt) AS t FROM r WHERE r.cust = c.cust GROUP BY r.store) AS x WHERE x.store = c.cust)"
)
JOINED = (
    CTE + ", av AS (SELECT ctr_store, AVG(ctr_total) * 1.2 AS lim FROM ctr GROUP BY ctr_store) "
    "SELECT c.grp FROM ctr AS ctr1 JOIN av ON av.ctr_store = ctr1.ctr_store JOIN s ON s.store = ctr1.ctr_store JOIN c ON ctr1.ctr_cust = c.cust "
    "WHERE ctr1.ctr_total > av.lim AND s.state = 1"
)


def table_columns(source):
    if isinstance(source, sqlglot.exp.Table):
        return SCHEMA.get(source.name.lower())
    names = [item.alias_or_name.lower() for item in source.this.expressions]
    return names if "" not in names else None


def closed(sql):
    return closed_derived_source(sqlglot.parse_one(f"SELECT 1 FROM {sql}", read="postgres").args.get("from_").this, table_columns)


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect="postgres", compare_names=False).proven


def rows_on_duckdb(queries, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name, columns in SCHEMA.items():
        db.execute(f"CREATE TABLE {name} ({', '.join(f'{c} BIGINT' for c in columns)})")
        insert_rows(db, name, rows.get(name, []))
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *(sqlglot.transpile(q, read="postgres", write="duckdb")[0] for q in queries))]


def differ_on_duckdb(left, right, rows):
    a, b = rows_on_duckdb([left, right], rows)
    return a != b


@pytest.mark.parametrize("source", [
    "(SELECT r.cust AS k, SUM(r.amt) AS total FROM r WHERE r.d = 1 GROUP BY r.cust) AS x",
    "(SELECT cust, store, amt FROM r) AS x",  # unqualified names one source has
    "(SELECT r.cust, s.state, COUNT(*) AS n FROM r JOIN s ON r.store = s.store GROUP BY r.cust, s.state) AS x",
    "(SELECT y.k FROM (SELECT r.cust AS k FROM r WHERE r.amt > 0) AS y WHERE y.k > 1) AS x",  # nested, each select reads its own source
])
def test_bodies_that_read_only_their_own_sources_are_closed(source):
    assert closed(source)


@pytest.mark.parametrize("source", [
    "r AS x",  # a table: the caller's own case
    "(SELECT r.cust AS k FROM r WHERE r.store = o.store) AS x",  # reads the enclosing query's o
    "(SELECT r.cust AS k FROM r WHERE r.store = outer_store) AS x",  # a name no source of the body has
    "(SELECT r.cust AS k FROM r WHERE EXISTS (SELECT 1 FROM s WHERE s.store = r.store)) AS x",  # correlated inside the body: not resolved here
    "(SELECT store AS k FROM r, s) AS x",  # a name two sources have
    "(SELECT * FROM r) AS x",
    "(SELECT r.* FROM r) AS x",
    "(SELECT r.cust AS k FROM r UNION ALL SELECT s.store FROM s) AS x",
    "(SELECT r.cust AS k FROM r) AS x (kk)",
    "(SELECT u.k FROM unknown_table AS u) AS x",
    "(SELECT a.cust AS k FROM r AS a, r AS a) AS x",
    "(SELECT r.cust AS k FROM r ORDER BY total) AS x",  # not a column of r
    "LATERAL (SELECT r.cust AS k FROM r) AS x",
    "(SELECT r.cust AS k FROM r)",  # no alias
])
def test_other_sources_are_not(source):
    assert not closed(source)


def test_the_subquery_becomes_a_join_with_the_grouped_source():
    out = normalize(QUERY, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "kqd" in out and "(SELECT AVG" not in out.upper().replace("( SELECT", "(SELECT")
    # a grouped body that reads the outer row is not one relation: the subquery stays
    assert "kqd" not in normalize(CORRELATED_BODY, schema=SCHEMA, types=TYPES, dialect="postgres")


TARGETS = [
    (QUERY, QUERY.replace("ctr1", "a").replace("ctr2", "b")),
    # the rewrite an optimizer or a person makes: a second WITH table, one row per store
    (QUERY, JOINED),
    # the same subquery under explicit joins, with its columns unqualified
    (
        QUERY,
        CTE + "SELECT c.grp FROM ctr AS ctr1 JOIN s ON s.store = ctr1.ctr_store JOIN c ON ctr1.ctr_cust = c.cust "
        "WHERE ctr1.ctr_total > (SELECT AVG(ctr_total) * 1.2 FROM ctr AS ctr2 WHERE ctr1.ctr_store = ctr2.ctr_store) AND s.state = 1",
    ),
    # a derived table written in place, with a filter of its own
    (
        "SELECT c.cust FROM c WHERE c.grp < (SELECT MAX(x.total) FROM (SELECT r.cust AS k, SUM(r.amt) AS total FROM r GROUP BY r.cust, r.store) AS x WHERE x.k = c.cust AND x.total > 0)",
        "SELECT c.cust FROM c, (SELECT x.k, MAX(x.total) AS m FROM (SELECT r.cust AS k, SUM(r.amt) AS total FROM r GROUP BY r.cust, r.store) AS x WHERE x.total > 0 GROUP BY x.k) AS y "
        "WHERE y.k = c.cust AND c.grp < y.m",
    ),
]


@pytest.mark.parametrize("left, right", TARGETS)
def test_decorrelated_forms_prove(left, right):
    assert proved(left, right)


ROWS = {
    "r": [
        (1, 1, 10, 1), (1, 2, 30, 1), (1, 3, 12, 1), (1, 2, 6, 1), (1, 6, 14, 1), (1, 7, 14, 1),  # store 1: one customer above the limit
        (2, 1, 5, 1), (2, 2, 5, 1), (2, 4, 50, 1), (2, 1, 100, 0),  # store 2: a large return outside the date filter
        (3, 5, 7, 1), (3, 6, 2, 1),  # store 3: small totals, below the average over all stores
        (4, 7, 9, 1),  # store 4: one customer, exactly on the average
    ],
    "s": [(1, 1), (2, 1), (3, 1), (4, 1)],
    "c": [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50), (6, 60), (7, 70), (8, 80)],  # customer 8 has no returns
}
NEAR_MISSES = [
    # another comparison: a customer exactly on the limit
    (QUERY.replace("* 1.2", "* 1"), JOINED.replace("* 1.2", "* 1").replace("ctr1.ctr_total > av.lim", "ctr1.ctr_total >= av.lim")),
    # the average per store and customer, not per store
    (QUERY, JOINED.replace("FROM ctr GROUP BY ctr_store)", "FROM ctr GROUP BY ctr_store, ctr_cust)")),
    # the average over every store
    (QUERY, CTE + "SELECT c.grp FROM ctr AS ctr1, s, c WHERE ctr1.ctr_total > (SELECT AVG(ctr2.ctr_total) * 1.2 FROM ctr AS ctr2) AND s.store = ctr1.ctr_store AND s.state = 1 AND ctr1.ctr_cust = c.cust"),
    # the limit computed over another WITH table (no date filter)
    (QUERY, JOINED.replace("FROM ctr GROUP BY ctr_store)", "FROM (SELECT r.store AS ctr_store, SUM(r.amt) AS ctr_total FROM r GROUP BY r.cust, r.store) AS z GROUP BY ctr_store)")),
    # a body that reads the outer row is not one relation: the join form would total every customer's returns
    (
        CORRELATED_BODY,
        "SELECT c.cust FROM c, (SELECT x.store AS k, MAX(x.t) AS m FROM (SELECT r.store, SUM(r.amt) AS t FROM r GROUP BY r.store) AS x GROUP BY x.store) AS y "
        "WHERE y.k = c.cust AND c.grp > y.m",
    ),
    # COUNT is 0 for a customer with no rows; the join drops that customer
    (
        "SELECT c.cust FROM c WHERE c.grp > (SELECT COUNT(*) FROM (SELECT r.cust AS k FROM r WHERE r.d = 1) AS x WHERE x.k = c.cust)",
        "SELECT c.cust FROM c JOIN (SELECT x.k, COUNT(*) AS n FROM (SELECT r.cust AS k FROM r WHERE r.d = 1) AS x GROUP BY x.k) AS y ON y.k = c.cust WHERE c.grp > y.n",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


DECORRELATED_QUERIES = [
    QUERY,
    QUERY.replace("AVG(ctr2.ctr_total) * 1.2", "MIN(ctr2.ctr_total) + 1"),
    "SELECT c.cust FROM c WHERE c.grp < (SELECT MAX(x.total) FROM (SELECT r.cust AS k, SUM(r.amt) AS total FROM r GROUP BY r.cust, r.store) AS x WHERE x.k = c.cust AND x.total > 0)",
    "SELECT c.cust, c.grp FROM c WHERE c.grp >= (SELECT SUM(x.amt) FROM (SELECT r.cust, r.amt FROM r WHERE r.d = 1) AS x WHERE x.cust = c.cust)",
]


def random_rows(rng):
    r = [(rng.choice([None, 1, 2]), rng.choice([None, 1, 2, 3]), rng.choice([None, 0, 1, 5, 9]), rng.choice([None, 0, 1, 1])) for _ in range(rng.randint(0, 9))]
    s = [(rng.choice([None, 1, 2]), rng.choice([None, 0, 1, 1])) for _ in range(rng.randint(0, 3))]
    c = [(rng.choice([None, 1, 2, 3]), rng.choice([None, 0, 3, 8, 20])) for _ in range(rng.randint(0, 5))]
    return {"r": r + r[:2], "s": s + s[:1], "c": c + c[:1]}  # with duplicate rows


@pytest.mark.parametrize("sql", DECORRELATED_QUERIES)
def test_decorrelated_queries_give_the_same_rows(sql):
    out = normalize(sql, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "kqd" in out
    rng = random.Random(len(sql))
    for rows in [{}, ROWS] + [random_rows(rng) for _ in range(14)]:
        assert not differ_on_duckdb(sql, out, rows)


# --- inner joins written with ON inside the subquery -----------------------------------------------

COMMA = "SELECT c.cust FROM c WHERE c.grp > (SELECT 1.5 * MAX(r.amt) FROM r, s WHERE r.cust = c.cust AND r.store = s.store AND s.state = 1)"
ON_JOIN = "SELECT c.cust FROM c WHERE c.grp > (SELECT 1.5 * MAX(r.amt) FROM r JOIN s ON r.store = s.store WHERE r.cust = c.cust AND s.state = 1)"


def test_only_plain_inner_joins_are_read():
    def joins(sql, dialect="postgres"):
        return sqlglot.parse_one(sql, read=dialect).args["joins"]

    for sql in ("SELECT 1 FROM r JOIN s ON r.store = s.store", "SELECT 1 FROM r INNER JOIN s ON r.store = s.store", "SELECT 1 FROM r CROSS JOIN s", "SELECT 1 FROM r, s"):
        assert all(plain_inner_join(j) for j in joins(sql)), sql
    for sql in (
        "SELECT 1 FROM r LEFT JOIN s ON r.store = s.store",
        "SELECT 1 FROM r RIGHT JOIN s ON r.store = s.store",
        "SELECT 1 FROM r FULL JOIN s ON r.store = s.store",
        "SELECT 1 FROM r NATURAL JOIN s",
        "SELECT 1 FROM r JOIN s USING (store)",
    ):
        assert not any(plain_inner_join(j) for j in joins(sql)), sql
    assert not any(plain_inner_join(j) for j in joins("SELECT 1 FROM r SEMI JOIN s ON r.store = s.store", "duckdb"))
    assert not any(plain_inner_join(j) for j in joins("SELECT 1 FROM r ASOF JOIN s ON r.store >= s.store", "duckdb"))
    select = sqlglot.parse_one("SELECT 1 FROM r JOIN s ON r.store = s.store LEFT JOIN c ON c.cust = r.cust CROSS JOIN o", read="postgres")
    assert [c.sql() for c in inner_join_conditions(select)] == ["r.store = s.store"]


JOIN_TARGETS = [
    (COMMA, ON_JOIN),
    # the correlation itself written in the ON
    (COMMA, "SELECT c.cust FROM c WHERE c.grp > (SELECT 1.5 * MAX(r.amt) FROM r INNER JOIN s ON r.store = s.store AND r.cust = c.cust AND s.state = 1)"),
    # in the select list: NULL for a customer with no rows
    (
        "SELECT c.cust, (SELECT SUM(r.amt) FROM r, s WHERE r.cust = c.cust AND r.store = s.store) AS total FROM c",
        "SELECT c.cust, (SELECT SUM(r.amt) FROM r JOIN s ON r.store = s.store WHERE r.cust = c.cust) AS total FROM c",
    ),
]


@pytest.mark.parametrize("left, right", JOIN_TARGETS)
def test_joins_with_on_prove(left, right):
    assert proved(left, right)


JOIN_ROWS = {
    "r": [(1, 1, 10, 1), (1, 2, 30, 1), (9, 3, 99, 1), (1, 3, 12, 1), (2, 4, 5, 1), (2, 4, 5, 1), (None, 5, 70, 1), (3, 6, 0, 1)],
    "s": [(1, 1), (2, 0), (3, 1)],
    "c": [(1, 20), (2, 40), (3, 30), (4, 8), (5, 200), (6, 1)],
}
JOIN_NEAR_MISSES = [
    # an outer join keeps the returns of a store that s does not have
    (ON_JOIN, ON_JOIN.replace("FROM r JOIN s ON r.store = s.store WHERE r.cust = c.cust AND s.state = 1", "FROM r LEFT JOIN s ON r.store = s.store AND s.state = 1 WHERE r.cust = c.cust")),
    # NATURAL JOIN matches the stores; a comma does not
    (
        "SELECT c.cust FROM c WHERE c.grp > (SELECT MAX(r.amt) FROM r NATURAL JOIN s WHERE r.cust = c.cust)",
        "SELECT c.cust FROM c WHERE c.grp > (SELECT MAX(r.amt) FROM r, s WHERE r.cust = c.cust)",
    ),
    # another ON condition, and the state filter left out
    (COMMA, ON_JOIN.replace("ON r.store = s.store", "ON r.store = s.state")),
    (COMMA, ON_JOIN.replace(" AND s.state = 1", "")),
]


@pytest.mark.parametrize("left, right", JOIN_NEAR_MISSES)
def test_join_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, JOIN_ROWS)


@pytest.mark.parametrize("sql", [ON_JOIN, JOIN_TARGETS[1][1], JOIN_TARGETS[2][1]])
def test_decorrelated_joins_give_the_same_rows(sql):
    out = normalize(sql, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "kqd" in out
    rng = random.Random(len(sql))
    for rows in [{}, JOIN_ROWS] + [random_rows(rng) for _ in range(14)]:
        assert not differ_on_duckdb(sql, out, rows)
