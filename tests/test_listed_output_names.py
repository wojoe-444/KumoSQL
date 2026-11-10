"""A column list on a derived table names outputs that have no name of their own (``kumosql.listed_output_names``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.listed_output_names import name_listed_outputs

SCHEMA = {"c": ["k", "g"], "o": ["id", "k", "v"]}
TYPES = {name: {column: "INT" for column in columns} for name, columns in SCHEMA.items()}
LISTED = "SELECT d.n, COUNT(*) AS m FROM (SELECT c.k, COUNT(o.id) FROM c LEFT JOIN o ON c.k = o.k AND o.v > 1 GROUP BY c.k) AS d (k, n) GROUP BY d.n"
ALIASED = "SELECT d.n, COUNT(*) AS m FROM (SELECT c.k AS k, COUNT(o.id) AS n FROM c LEFT JOIN o ON c.k = o.k AND o.v > 1 GROUP BY c.k) AS d GROUP BY d.n"


def named(sql, dialect="postgres"):
    return name_listed_outputs(sqlglot.parse_one(sql, read=dialect)).sql(dialect=dialect)


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


def test_unnamed_outputs_under_a_full_column_list_get_a_name():
    assert named("SELECT d.n FROM (SELECT c.k, COUNT(*) FROM c GROUP BY c.k) AS d (k, n)") == (
        "SELECT d.n FROM (SELECT c.k, COUNT(*) AS kq_col1 FROM c GROUP BY c.k) AS d(k, n)"
    )
    # an alias and a plain column stay as written; an expression, a literal and a cast are named by position
    assert named("SELECT d.a FROM c JOIN (SELECT o.k + 1, o.v AS w, 7, CAST(o.id AS TEXT), id FROM o) AS d (a, b, e, f, g) ON d.a = c.k") == (
        "SELECT d.a FROM c JOIN (SELECT o.k + 1 AS kq_col0, o.v AS w, 7 AS kq_col2, CAST(o.id AS TEXT) AS kq_col3, id FROM o) AS d(a, b, e, f, g) ON d.a = c.k"
    )
    # nested lists, innermost included
    assert named("SELECT d.x FROM (SELECT MAX(e.y) FROM (SELECT o.v * 2 FROM o) AS e (y)) AS d (x)") == (
        "SELECT d.x FROM (SELECT MAX(e.y) AS kq_col0 FROM (SELECT o.v * 2 AS kq_col0 FROM o) AS e(y)) AS d(x)"
    )


@pytest.mark.parametrize("sql", [
    "SELECT d.n FROM (SELECT c.k, COUNT(*) AS n FROM c GROUP BY c.k) AS d (k, n)",  # every output has a name
    "SELECT d.k FROM (SELECT c.k, COUNT(*) FROM c GROUP BY c.k) AS d (k)",  # the second output keeps the engine's name
    "SELECT d.k FROM (SELECT c.k, COUNT(*) FROM c GROUP BY c.k ORDER BY 2 LIMIT 3) AS d (k, n)",  # ORDER BY can name an output
    "SELECT d.k FROM (SELECT c.k, COUNT(*) FROM c GROUP BY c.k ORDER BY count) AS d (k, n)",
    "SELECT d.k FROM (SELECT c.*, 1 FROM c) AS d (k, g, one)",
    "SELECT d.k FROM (SELECT *, 1 FROM c) AS d (k, g, one)",
    "SELECT d.k FROM (SELECT c.k, 1 FROM c UNION ALL SELECT o.k, 2 FROM o) AS d (k, n)",  # names come from the first branch of a set operation
    "SELECT d.k FROM (VALUES (1, 2)) AS d (k, n)",
    "SELECT d.k FROM c AS d (k, n)",  # a table
    "SELECT d.k FROM (SELECT c.k AS kq_col1, COUNT(*) FROM c GROUP BY c.k) AS d (k, n)",  # the name is taken
    "SELECT d.k FROM (SELECT c.k, COUNT(*) FROM c GROUP BY c.k) AS d",  # no list
])
def test_other_shapes_stay(sql):
    assert named(sql) == sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres")


TARGETS = [
    (LISTED, LISTED.replace("AS d (k, n)", "AS x (kk, n)").replace("d.n", "x.n")),
    (LISTED, ALIASED),
    # COUNT(*) and an expression without a name
    (
        "SELECT d.n, d.t FROM (SELECT o.k, COUNT(*), SUM(o.v) + 1 FROM o GROUP BY o.k) AS d (k, n, t) WHERE d.n > 1",
        "SELECT d.n, d.t FROM (SELECT o.k AS k, COUNT(*) AS n, SUM(o.v) + 1 AS t FROM o GROUP BY o.k) AS d WHERE d.n > 1",
    ),
    # the list swaps two names
    (
        "SELECT d.a FROM (SELECT o.k, MAX(o.v) FROM o GROUP BY o.k) AS d (b, a)",
        "SELECT MAX(o.v) AS a FROM o GROUP BY o.k",
    ),
]


@pytest.mark.parametrize("left, right", TARGETS)
def test_listed_outputs_prove(left, right):
    assert proved(left, right)


ROWS = {"c": [(1, 1), (2, 1), (3, 2), (4, 2)], "o": [(1, 1, 5), (2, 1, 7), (None, 1, 9), (3, 2, 1), (4, 2, 8), (5, 3, None), (5, 3, None)]}
NEAR_MISSES = [
    # COUNT(o.id) skips the NULL id, COUNT(*) does not
    (LISTED, ALIASED.replace("COUNT(o.id) AS n", "COUNT(*) AS n")),
    # the list names the outputs by position: (n, k) reads the key as n
    (LISTED, LISTED.replace("AS d (k, n)", "AS d (n, k)")),
    # another aggregate, another join
    (LISTED, ALIASED.replace("COUNT(o.id) AS n", "MAX(o.id) AS n")),
    (LISTED, ALIASED.replace("LEFT JOIN", "JOIN")),
    (
        "SELECT d.a FROM (SELECT o.k, MAX(o.v) FROM o GROUP BY o.k) AS d (b, a)",
        "SELECT o.k AS a FROM o GROUP BY o.k",
    ),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


LISTED_QUERIES = [
    LISTED,
    "SELECT d.n, d.t, d.k FROM (SELECT o.k, COUNT(*), SUM(o.v) + 1 FROM o GROUP BY o.k) AS d (k, n, t) WHERE d.n > 1 OR d.t IS NULL",
    "SELECT d.a, c.g FROM c LEFT JOIN (SELECT o.k, MAX(o.v), 7 FROM o GROUP BY o.k) AS d (b, a, seven) ON d.b = c.k",
    "SELECT d.x FROM (SELECT MAX(e.y), COUNT(*) FROM (SELECT o.v * 2, o.k FROM o) AS e (y, k)) AS d (x, n)",  # a global aggregate, over no rows too
]


def random_rows(rng):
    c = [(rng.choice([None, 1, 2, 3]), rng.choice([None, 1, 2])) for _ in range(rng.randint(0, 5))]
    o = [(rng.choice([None, 1, 2, 3, 4]), rng.choice([None, 1, 2, 3]), rng.choice([None, 0, 1, 2, 5])) for _ in range(rng.randint(0, 8))]
    return {"c": c + c[:1], "o": o + o[:2]}  # with duplicate rows


@pytest.mark.parametrize("sql", LISTED_QUERIES)
def test_named_and_normalized_queries_give_the_same_rows(sql):
    renamed = named(sql)
    out = normalize(sql, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "kq_col" in renamed and renamed != sql
    rng = random.Random(len(sql))
    for rows in [{}, ROWS] + [random_rows(rng) for _ in range(14)]:
        assert not differ_on_duckdb(sql, renamed, rows)
        assert not differ_on_duckdb(sql, out, rows)
