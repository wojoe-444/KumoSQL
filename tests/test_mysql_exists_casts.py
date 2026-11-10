"""MySQL's ``CAST(EXISTS(...) AS SIGNED)`` output is the ``EXISTS`` (``kumosql.mysql_exists_casts``)."""

from collections import Counter
import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.mysql_exists_casts import drop_mysql_exists_cast

SCHEMA = {"t": ["a", "b", "s"], "u": ["a", "b"]}
TYPES = {"t": {"a": "INT", "b": "INT", "s": "DOUBLE"}, "u": {"a": "INT", "b": "INT"}}
DDL = {"t": "a BIGINT, b BIGINT, s DOUBLE", "u": "a BIGINT, b BIGINT"}

EXISTS = "EXISTS(SELECT 1 FROM u WHERE u.a = t.a)"
SUBQUERIES = [
    "SELECT 1 FROM u WHERE u.a = t.a",
    "SELECT 1 FROM u WHERE u.a > t.b OR u.b IS NULL",
    "SELECT u.b FROM u",
    "SELECT 1 FROM u WHERE u.a = NULL",
    "SELECT NULL FROM u WHERE u.b = t.b",
    "SELECT COUNT(*) FROM u WHERE u.a = t.a",  # one row always
    "SELECT u.a FROM u WHERE u.b = t.b UNION ALL SELECT u.b FROM u WHERE u.a = t.a",
]


def dropped(sql, dialect="mysql"):
    out = drop_mysql_exists_cast(sqlglot.parse_one(sql, read=dialect), dialect)
    return out.sql(dialect=dialect) if out is not None else None


def proved(left, right, dialect="mysql"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False, dialect=dialect).proven


def mysql_values(rows):
    """Rows as MySQL would show them: a truth value is the integer 1 or 0."""

    return Counter(tuple(int(v) if isinstance(v, bool) else v for v in row) for row in rows)


def run_on_duckdb(queries, rows, dialect="mysql"):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name, columns in DDL.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        insert_rows(db, name, rows.get(name, []))
    return run_unoptimized(db, *[sqlglot.transpile(sql, read=dialect, write="duckdb")[0] for sql in queries])


def differ_on_duckdb(left, right, rows, dialect="mysql"):
    a, b = run_on_duckdb([left, right], rows, dialect)
    if dialect == "mysql":
        return mysql_values(a) != mysql_values(b)
    return sorted(map(repr, a)) != sorted(map(repr, b))  # elsewhere TRUE and 1 are different values


@pytest.mark.parametrize("subquery", SUBQUERIES)
def test_cast_of_an_exists_output_is_dropped(subquery):
    expected = sqlglot.parse_one(f"SELECT t.b, EXISTS({subquery}) AS f FROM t", read="mysql").sql(dialect="mysql")
    assert dropped(f"SELECT t.b, CAST(EXISTS({subquery}) AS SIGNED) AS f FROM t") == expected
    assert dropped(f"SELECT t.b, CAST((EXISTS({subquery})) AS SIGNED INTEGER) AS f FROM t") == expected


def test_the_cast_and_the_bare_exists_are_proved_equal_on_mysql():
    assert proved(f"SELECT t.b, CAST({EXISTS} AS SIGNED) AS f FROM t", f"SELECT t.b, {EXISTS} AS f FROM t")
    assert proved(f"SELECT CAST({EXISTS} AS SIGNED) FROM t WHERE t.b > 1", f"SELECT {EXISTS} FROM t WHERE t.b > 1")


@pytest.mark.parametrize("sql, dialect", [
    # other operands: 1, 0 or NULL on MySQL too, but not identified by default (see tests/test_mysql_boolean_outputs.py)
    ("SELECT CAST(t.a = 1 AS SIGNED) AS f FROM t", "mysql"),
    ("SELECT CAST(t.a IS NULL AS SIGNED) AS f FROM t", "mysql"),
    (f"SELECT CAST(NOT {EXISTS} AS SIGNED) AS f FROM t", "mysql"),
    (f"SELECT CAST({EXISTS} AND t.b > 1 AS SIGNED) AS f FROM t", "mysql"),
    ("SELECT CAST(t.a IN (SELECT u.a FROM u) AS SIGNED) AS f FROM t", "mysql"),
    ("SELECT CAST(t.a AS SIGNED) AS f FROM t", "mysql"),
    ("SELECT CAST(t.s AS SIGNED) AS f FROM t", "mysql"),
    # other targets
    (f"SELECT CAST({EXISTS} AS CHAR) AS f FROM t", "mysql"),
    (f"SELECT CAST({EXISTS} AS DECIMAL(3, 1)) AS f FROM t", "mysql"),
    (f"SELECT CAST({EXISTS} AS UNSIGNED) AS f FROM t", "mysql"),  # not needed, so not vouched for
    # other places: only a whole item of the outermost select
    (f"SELECT CAST({EXISTS} AS SIGNED) + 1 AS f FROM t", "mysql"),
    (f"SELECT t.b FROM t WHERE CAST({EXISTS} AS SIGNED) = 1", "mysql"),
    (f"SELECT d.f FROM (SELECT CAST({EXISTS} AS SIGNED) AS f FROM t) AS d", "mysql"),
    (f"SELECT CAST({EXISTS} AS SIGNED) AS f, COUNT(*) FROM t GROUP BY CAST({EXISTS} AS SIGNED)", "mysql"),
    (f"SELECT CAST({EXISTS} AS SIGNED) AS f FROM t UNION ALL SELECT u.a FROM u", "mysql"),
    # other dialects: 1 is not TRUE there
    (f"SELECT CAST({EXISTS} AS INT64) AS f FROM t", "bigquery"),
    (f"SELECT SAFE_CAST({EXISTS} AS INT64) AS f FROM t", "bigquery"),
    (f"SELECT CAST({EXISTS} AS BIGINT) AS f FROM t", "postgres"),
    (f"SELECT CAST({EXISTS} AS BIGINT) AS f FROM t", "duckdb"),
    (f"SELECT TRY_CAST({EXISTS} AS BIGINT) AS f FROM t", "duckdb"),
    ("SELECT CAST(EXISTS(arr, x -> x > 1) AS BIGINT) AS f FROM t", "spark"),  # the array form can be NULL
])
def test_other_operands_targets_places_and_dialects_stay(sql, dialect):
    tree = sqlglot.parse_one(sql, read=dialect)
    before = tree.sql(dialect=dialect)
    for select in list(tree.find_all(sqlglot.exp.Select)):
        assert drop_mysql_exists_cast(select, dialect) is None
        assert drop_mysql_exists_cast(select, "mysql" if dialect == "spark" else dialect) is None
    assert tree.sql(dialect=dialect) == before


def test_the_recorded_decision_on_comparisons_is_kept():
    # true on MySQL, and deliberately not proved: this module only reads EXISTS
    assert not proved("SELECT t.a = 1 AS f FROM t", "SELECT CAST(t.a = 1 AS SIGNED) AS f FROM t")


# near misses: each pair really differs (confirmed on DuckDB) and must stay unproved
NEAR_MISSES = [
    # EXISTS is never NULL; IN over a NULL is
    (f"SELECT CAST({EXISTS} AS SIGNED) AS f FROM t", "SELECT t.a IN (SELECT u.a FROM u) AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(None, 1)]}, "mysql"),
    # the cast of a number is not the number: 2.5 becomes a whole number
    ("SELECT CAST(t.s AS SIGNED) AS f FROM t", "SELECT t.s AS f FROM t", {"t": [(1, 1, 2.5)]}, "mysql"),
    # a cast inside arithmetic is not the output: 2 against 1
    (f"SELECT CAST({EXISTS} AS SIGNED) + 1 AS f FROM t", f"SELECT {EXISTS} AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(1, 1)]}, "mysql"),
    # another subquery under the cast
    (f"SELECT CAST({EXISTS} AS SIGNED) AS f FROM t", "SELECT EXISTS(SELECT 1 FROM u WHERE u.b = t.a) AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(1, 2)]}, "mysql"),
    # outside MySQL the integer 1 is not the boolean TRUE
    (f"SELECT CAST({EXISTS} AS INT64) AS f FROM t", f"SELECT {EXISTS} AS f FROM t", {"t": [(1, 1, 0.0)], "u": [(1, 1)]}, "bigquery"),
]


@pytest.mark.parametrize("left, right, rows, dialect", NEAR_MISSES)
def test_near_misses_differ_and_are_not_proved(left, right, rows, dialect):
    assert differ_on_duckdb(left, right, rows, dialect)
    assert not proved(left, right, dialect)


def test_differential_cast_and_exists_give_the_same_mysql_values():
    """Random small tables with NULLs and duplicates: the cast and the rewritten output hold the same 1 and 0."""

    rng = random.Random(20261110)
    value = lambda: rng.choice([None, 0, 1, 2, 2])  # noqa: E731
    originals = [f"SELECT t.a, t.b, CAST(EXISTS({subquery}) AS SIGNED) AS f FROM t" for subquery in SUBQUERIES]
    rewritten = [dropped(sql) for sql in originals]
    assert all(sql is not None and "CAST" not in sql for sql in rewritten)
    for _ in range(25):
        rows = {
            "t": [(value(), value(), 0.0) for _ in range(rng.randint(0, 5))],
            "u": [(value(), value()) for _ in range(rng.randint(0, 4))],
        }
        results = run_on_duckdb(originals + rewritten, rows)
        for index, sql in enumerate(originals):
            before, after = results[index], results[len(originals) + index]
            assert all(row[2] in (0, 1) and type(row[2]) is int for row in before), sql  # the cast gives 1 or 0, never NULL
            assert all(type(row[2]) is bool for row in after), sql
            assert mysql_values(before) == mysql_values(after), (sql, rows)
