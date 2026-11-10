"""Bare columns around a quantified comparison or over a renaming projection name their source (``kumosql.single_source_columns``).

Also the three R-Bot Calcite pairs this closes (with ``kumosql.mysql_exists_casts`` and the
parenthesis fix in ``kumosql.nonnull_any``), their nullable twins that must stay unproved, and
a differential check of the qualification on random tables.
"""

from collections import Counter
import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.single_source_columns import qualify_single_source_columns
from kumosql.smt_equivalence import TableConstraints

EMP_COLUMNS = ["empno", "ename", "job", "mgr", "hiredate", "sal", "comm", "deptno", "slacker"]
EMP_TYPES = dict(zip(EMP_COLUMNS, ["INT", "TEXT", "TEXT", "INT", "DATE", "INT", "INT", "INT", "BOOLEAN"]))
SCHEMA = {"emp": EMP_COLUMNS, "empnullables": EMP_COLUMNS, "dept": ["deptno", "name"], "t": ["a", "b", "c"], "u": ["a", "b", "d"]}
TYPES = {
    "emp": EMP_TYPES,
    "empnullables": EMP_TYPES,
    "dept": {"deptno": "INT", "name": "TEXT"},
    "t": {"a": "INT", "b": "INT", "c": "INT"},
    "u": {"a": "INT", "b": "INT", "d": "INT"},
}
# R-Bot's schema: every EMP column but MGR is NOT NULL; EMPNULLABLES only has its key
CONSTRAINTS = {
    "emp": TableConstraints(not_null=frozenset(set(EMP_COLUMNS) - {"mgr"}), keys=(("empno",),)),
    "empnullables": TableConstraints(not_null=frozenset({"empno"}), keys=(("empno",),)),
    "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
}
DDL = {
    "emp": "empno BIGINT, ename VARCHAR, job VARCHAR, mgr BIGINT, hiredate DATE, sal BIGINT, comm BIGINT, deptno BIGINT, slacker BOOLEAN",
    "dept": "deptno BIGINT, name VARCHAR",
    "t": "a BIGINT, b BIGINT, c BIGINT",
    "u": "a BIGINT, b BIGINT, d BIGINT",
}
DDL["empnullables"] = DDL["emp"]
RENAMED = "(`empno`, `ename`, `job`, `mgr`, `hiredate`, `sal`, `comm`, `deptno0`, `slacker`)"


def emp(empno, job="x", deptno=1):
    return (empno, "n", job, None, "2020-01-01", 1, 1, deptno, False)


def qualified(sql, dialect="mysql"):
    return qualify_single_source_columns(sqlglot.parse_one(sql, read=dialect), SCHEMA, dialect).sql(dialect=dialect)


def proved(left, right):
    """As ``tools/rbot_bench.py`` asks: MySQL reading, Calcite's constant GROUP BY keys, R-Bot's constraints."""

    return prove_equivalent_algebraic(
        left, right, schema=SCHEMA, constraints=CONSTRAINTS, types=TYPES, compare_names=False, dialect="mysql", exact_arithmetic=True, group_by_constants=True
    ).proven


def mysql_values(rows):
    return Counter(tuple(int(v) if isinstance(v, bool) else v for v in row) for row in rows)


def run_on_duckdb(queries, rows):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name, columns in DDL.items():
        db.execute(f"CREATE TABLE {name} ({columns})")
        insert_rows(db, name, rows.get(name, []))
    return run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])


def differ_on_duckdb(left, right, rows):
    a, b = run_on_duckdb([left, right], rows)
    return mysql_values(a) != mysql_values(b)


# --- the qualification itself ---------------------------------------------------------------------------

PROJECTION = "(SELECT x.a AS a0, x.b, x.c FROM t AS x) AS s"  # what the column list of ``t AS s(a0, b, c)`` is made into
Q = "t.c > ANY (SELECT u.d FROM u)"  # puts a select in this pass's scope without a bare column of its own


@pytest.mark.parametrize("sql, expected", [
    # around a quantified comparison: its select and its subquery. The subquery's own table comes first for a
    # name both tables have (a); a name only the outer table has (c) stays bare
    ("SELECT a > ANY (SELECT a FROM u WHERE d = c) FROM t", "SELECT t.a > ANY (SELECT u.a FROM u WHERE u.d = c) FROM t"),
    (
        "SELECT b FROM t AS x WHERE c <= ALL (SELECT d FROM u) GROUP BY b HAVING SUM(a) > 0 ORDER BY b",
        "SELECT x.b FROM t AS x WHERE x.c <= ALL (SELECT u.d FROM u) GROUP BY x.b HAVING SUM(x.a) > 0 ORDER BY x.b",
    ),
    ("SELECT empno > ANY (SELECT deptno FROM dept WHERE emp.job = dept.name) FROM emp", "SELECT emp.empno > ANY (SELECT dept.deptno FROM dept WHERE emp.job = dept.name) FROM emp"),
    # a select that reads a renaming projection (the projection itself is not rewritten)
    (f"SELECT a0, MIN(b) FROM {PROJECTION} WHERE c > 0 GROUP BY a0", f"SELECT s.a0, MIN(s.b) FROM {PROJECTION} WHERE s.c > 0 GROUP BY s.a0"),
    ("SELECT x, b FROM (SELECT a AS x, b FROM t) AS s WHERE b > 0", "SELECT s.x, s.b FROM (SELECT a AS x, b FROM t) AS s WHERE s.b > 0"),
])
def test_bare_columns_in_scope_are_qualified(sql, expected):
    assert qualified(sql) == expected


def test_a_column_list_made_a_projection_is_read_by_name():
    from kumosql.ast_utils import expand_alias_columns

    tree = expand_alias_columns(sqlglot.parse_one("SELECT MIN(deptno0), name FROM dept AS d(deptno0, name) GROUP BY name", read="mysql"), SCHEMA)
    assert qualify_single_source_columns(tree, SCHEMA, "mysql").sql(dialect="mysql") == (
        "SELECT MIN(d.deptno0), d.name FROM (SELECT kq_renamed.deptno AS deptno0, kq_renamed.name AS name FROM dept AS kq_renamed) AS d GROUP BY d.name"
    )


@pytest.mark.parametrize("sql", [
    # no quantified comparison and no renaming projection: left to the later passes, as before
    "SELECT a, b + 1 FROM t WHERE c > 1",
    "SELECT a FROM t GROUP BY a HAVING SUM(b) > 0",
    "SELECT a FROM t WHERE b IN (SELECT a FROM u)",
    "SELECT a, (SELECT MAX(d) FROM u WHERE b = c) FROM t",
    # tests/test_proof_columns.py injects its faults on these bare columns; this pass must not get there first
    "SELECT t.b FROM t WHERE EXISTS(SELECT 1 FROM u WHERE a = t.b)",
    "SELECT t.a FROM t WHERE EXISTS(SELECT 1 FROM u WHERE b = 1)",
    # a derived table that filters, removes duplicates or computes is not a renaming
    "SELECT x FROM (SELECT a AS x FROM t WHERE b > 0) AS s",
    "SELECT x FROM (SELECT DISTINCT a AS x FROM t) AS s",
    "SELECT x FROM (SELECT a + 1 AS x FROM t) AS s",
    "SELECT x FROM (SELECT a AS x FROM t LIMIT 1) AS s",
    "SELECT x FROM (SELECT t.a AS x FROM t JOIN u ON t.b = u.b) AS s",
])
def test_selects_out_of_scope_are_left_alone(sql):
    assert qualified(sql) == sqlglot.parse_one(sql, read="mysql").sql(dialect="mysql")


@pytest.mark.parametrize("sql", [
    f"SELECT a, d FROM t JOIN u ON c = d WHERE {Q}",  # two sources: not this pass
    f"SELECT a, d FROM t, u WHERE {Q}",
    "SELECT a FROM unknown_table WHERE a > ANY (SELECT u.d FROM u)",  # columns nobody listed
    "SELECT a FROM other.t WHERE a > ANY (SELECT u.d FROM u)",
    f"SELECT zzz FROM t WHERE {Q}",  # not a column of the source
    f"SELECT * FROM t WHERE {Q}",
    "SELECT a FROM (SELECT * FROM t) AS s WHERE a > ANY (SELECT u.d FROM u)",  # outputs not named
    "SELECT a FROM (SELECT t.a FROM t UNION ALL SELECT u.a FROM u) AS s WHERE a > ANY (SELECT u.d FROM u)",
    f"WITH t AS (SELECT 1 AS zz) SELECT a FROM t WHERE {Q}",  # (WITH tables are inlined before this pass runs in normalize)
    f"SELECT t.b AS a FROM t WHERE {Q} ORDER BY a",  # ORDER BY reads the output named a
    f"SELECT t.b AS a, COUNT(*) FROM t WHERE {Q} GROUP BY a",  # which a GROUP BY reads differs by dialect
    f"SELECT t.b AS a FROM t WHERE a > 1 AND {Q}",  # left alone everywhere in such a select, to keep it simple
    f"SELECT t.c FROM t WHERE {Q} ORDER BY c",  # a qualified output's name is reserved the same way
])
def test_uncertain_owners_in_scope_stay_bare(sql):
    assert qualified(sql) == sqlglot.parse_one(sql, read="mysql").sql(dialect="mysql")


def test_scopes_other_than_the_selects_own_stay_bare():
    # ORDER BY of a set operation reads the operation's output, not the table of an enclosing select
    sql = f"SELECT t.a FROM t WHERE {Q} AND t.b IN (SELECT u.a FROM u UNION ALL SELECT u.b FROM u ORDER BY a)"
    assert qualified(sql) == sql
    # a lambda's own parameter
    lambdas = "SELECT LIST_TRANSFORM([1], a -> a + 1), {b} > ANY (SELECT u.d FROM u) FROM t"
    assert qualified(lambdas.format(b="b"), "duckdb") == lambdas.format(b="t.b")
    # a field path: its head may be a column, and x.f printed as s.x.f would be read back as a table path
    fields = {"t": ["x", "f"]}
    tree = sqlglot.parse_one("SELECT (x).f FROM (SELECT t.x FROM t) AS s", read="bigquery")
    assert qualify_single_source_columns(tree, fields, "bigquery").sql(dialect="bigquery") == "SELECT (s.x).f FROM (SELECT t.x FROM t) AS s"
    tree = sqlglot.parse_one("SELECT x.f FROM (SELECT t.x, t.f FROM t) AS s", read="bigquery")
    assert qualify_single_source_columns(tree, fields, "bigquery").sql(dialect="bigquery") == "SELECT x.f FROM (SELECT t.x, t.f FROM t) AS s"
    # a date part given to a function is not the column of that name
    tree = sqlglot.parse_one("SELECT MY_DIFF(a, b, day) FROM (SELECT t.a, t.b, t.day FROM t) AS s", read="mysql")
    assert qualify_single_source_columns(tree, {"t": ["a", "b", "day"]}, "mysql").sql(dialect="mysql") == (
        "SELECT MY_DIFF(s.a, s.b, day) FROM (SELECT t.a, t.b, t.day FROM t) AS s"
    )


def test_differential_qualified_queries_return_the_same_rows():
    """Random small tables with NULLs and duplicates: the qualified query is the query."""

    queries = [
        "SELECT a > ANY (SELECT a FROM u WHERE d = c) FROM t",
        "SELECT c <= ALL (SELECT d FROM u WHERE a = b) FROM t",
        "SELECT a, b FROM t WHERE c > ANY (SELECT d FROM u WHERE b = a)",
        "SELECT a FROM t WHERE NOT b <> ALL (SELECT a FROM u WHERE d IS NULL OR d = c)",
        "SELECT b, COUNT(*), SUM(a) FROM t WHERE c >= ANY (SELECT d FROM u) GROUP BY b HAVING MIN(c) > 0",
        "SELECT b AS a, COUNT(*) FROM t WHERE c < ANY (SELECT b FROM u) GROUP BY b",
        "SELECT a = ANY (SELECT a FROM u WHERE b IN (SELECT b FROM u AS v WHERE d = c)) FROM t",
        "SELECT x, b FROM (SELECT a AS x, b FROM t) AS s WHERE b > 0",
        f"SELECT a0, MIN(b), COUNT(*) FROM {PROJECTION} WHERE c > 0 GROUP BY a0",
        "SELECT a FROM t WHERE c > (SELECT COUNT(*) FROM (SELECT d AS c, a FROM u) AS s WHERE c > 0 AND a = b)",
        "SELECT a FROM t WHERE EXISTS(SELECT 1 FROM u WHERE a = c)",  # out of scope: the same text
    ]
    rewritten = [qualified(sql) for sql in queries]
    assert sum(before != after for before, after in zip(queries, rewritten)) == len(queries) - 1
    rng = random.Random(20261110)
    value = lambda: rng.choice([None, 0, 1, 2, 2])  # noqa: E731
    for _ in range(25):
        rows = {name: [(value(), value(), value()) for _ in range(rng.randint(0, 5))] for name in ("t", "u")}
        results = run_on_duckdb(queries + rewritten, rows)
        for index, sql in enumerate(queries):
            assert Counter(results[index]) == Counter(results[len(queries) + index]), (sql, rewritten[index], rows)


# --- the R-Bot Calcite pairs (as the R-Bot harness prints them; its floor in test_rbot_benchmarks.py reads the fixture) -------------------------------

ANY_IN_PROJECT = (
    "SELECT name, deptno > ANY (SELECT deptno FROM {emp}) FROM dept",
    "SELECT `dept`.`name`, CAST((`dept`.`deptno` > `t1`.`m`) IS TRUE AND `t1`.`c` <> 0 OR `t1`.`c` > `t1`.`c0` AND NULL AND `t1`.`c` <> 0 AND NOT (`dept`.`deptno` > `t1`.`m`) IS TRUE"
    " OR `dept`.`deptno` > `t1`.`m` AND `t1`.`c` <> 0 AND NOT (`dept`.`deptno` > `t1`.`m`) IS TRUE AND `t1`.`c` <= `t1`.`c0` AS SIGNED)"
    " FROM `dept`, (SELECT {agg}(`deptno0`) AS `m`, COUNT(*) AS `c`, COUNT(*) AS `c0` FROM `{emp}` AS `{emp}`" + RENAMED + ") AS `t1`",
)
_CORRELATED_CONDITION = (
    "(`emp`.`empno` > `t1`.`m`) IS TRUE AND NOT (`t1`.`trueliteral` IS NULL OR `t1`.`c` = 0) IS TRUE"
    " OR (`t1`.`c` > `t1`.`c0`) IS TRUE AND NULL AND NOT (`t1`.`trueliteral` IS NULL OR `t1`.`c` = 0) IS TRUE AND NOT (`emp`.`empno` > `t1`.`m`) IS TRUE"
    " OR `emp`.`empno` > `t1`.`m` AND NOT (`t1`.`trueliteral` IS NULL OR `t1`.`c` = 0) IS TRUE AND NOT (`emp`.`empno` > `t1`.`m`) IS TRUE AND NOT (`t1`.`c` > `t1`.`c0`) IS TRUE"
)
SELECT_ANY_CORRELATED = (
    "SELECT empno > ANY (SELECT deptno FROM dept WHERE emp.job = dept.name) FROM emp",
    "SELECT CAST(" + _CORRELATED_CONDITION + " AS SIGNED) FROM `emp` LEFT JOIN (SELECT {agg}(`deptno0`) AS `m`, COUNT(*) AS `c`, COUNT(*) AS `c0`, TRUE AS `trueliteral`, `name`"
    " FROM `dept` AS `dept`(`deptno0`, `name`) GROUP BY `name`) AS `t1` ON `emp`.`job` = `t1`.`name`",
)
WHERE_ANY_CORRELATED = (
    "SELECT * FROM emp WHERE empno > ANY (SELECT deptno FROM dept WHERE emp.job = dept.name)",
    "SELECT `emp`.`empno`, `emp`.`ename`, `emp`.`job`, `emp`.`mgr`, `emp`.`hiredate`, `emp`.`sal`, `emp`.`comm`, `emp`.`deptno`, `emp`.`slacker` FROM `emp`"
    " INNER JOIN (SELECT {agg}(`deptno0`) AS `m`, COUNT(*) AS `c`, COUNT(*) AS `c0`, TRUE AS `trueliteral`, `name` FROM `dept` AS `dept`(`deptno0`, `name`) GROUP BY `name`) AS `t1`"
    " ON `emp`.`job` = `t1`.`name` AND (`emp`.`empno` > `t1`.`m` AND NOT (`t1`.`trueliteral` IS NULL OR `t1`.`c` = 0) IS TRUE"
    " OR `emp`.`empno` > `t1`.`m` AND NOT (`t1`.`trueliteral` IS NULL OR `t1`.`c` = 0) IS TRUE AND NOT (`emp`.`empno` > `t1`.`m`) IS TRUE AND NOT (`t1`.`c` > `t1`.`c0`) IS TRUE)",
)
# the correlated pairs over a table whose value and key columns may be NULL
_NULLABLE_SOURCE = "FROM `empnullables` AS `e`(`empno`, `ename`, `name`, `mgr`, `hiredate`, `sal`, `comm`, `deptno0`, `slacker`) GROUP BY `name`"
SELECT_ANY_NULLABLE = (
    "SELECT empno > ANY (SELECT deptno FROM empnullables WHERE emp.job = empnullables.job) FROM emp",
    SELECT_ANY_CORRELATED[1].format(agg="MIN").replace("FROM `dept` AS `dept`(`deptno0`, `name`) GROUP BY `name`", _NULLABLE_SOURCE),
)
# a WHERE drops NULL and FALSE alike, so there the NULL only shows once it is asked for
WHERE_ANY_NULLABLE = (
    "SELECT * FROM emp WHERE (empno > ANY (SELECT deptno FROM empnullables WHERE emp.job = empnullables.job)) IS NOT FALSE",
    WHERE_ANY_CORRELATED[1].format(agg="MIN").replace("FROM `dept` AS `dept`(`deptno0`, `name`) GROUP BY `name`", _NULLABLE_SOURCE),
)


def pair(template, **names):
    return tuple(sql.format(**names) for sql in template)


@pytest.mark.parametrize("left, right", [
    pair(ANY_IN_PROJECT, emp="emp", agg="MIN"),
    pair(SELECT_ANY_CORRELATED, agg="MIN"),
    pair(WHERE_ANY_CORRELATED, agg="MIN"),
], ids=["testAnyInProjectNonNullable", "testSelectAnyCorrelated", "testWhereAnyCorrelatedInSelect"])
def test_rbot_any_pairs_are_proved(left, right):
    assert proved(left, right)


DEPTS = [(1, "x"), (3, "x"), (2, "y")]
NEAR_MISSES = [
    # R-Bot prints COUNT(*) twice where Calcite counts the column: with a NULL the left side is NULL, the right FALSE
    (*pair(ANY_IN_PROJECT, emp="empnullables", agg="MIN"), {"dept": [(1, "x")], "empnullables": [emp(1, deptno=5), emp(2, deptno=None)]}),
    (*SELECT_ANY_NULLABLE, {"emp": [emp(1)], "empnullables": [emp(1, deptno=5), emp(2, deptno=None)]}),
    (*WHERE_ANY_NULLABLE, {"emp": [emp(1)], "empnullables": [emp(1, deptno=5), emp(2, deptno=None)]}),
    # MAX answers > ALL, not > ANY
    (*pair(ANY_IN_PROJECT, emp="emp", agg="MAX"), {"dept": [(2, "x")], "emp": [emp(1, deptno=1), emp(2, deptno=3)]}),
    (*pair(SELECT_ANY_CORRELATED, agg="MAX"), {"emp": [emp(2)], "dept": DEPTS}),
    (*pair(WHERE_ANY_CORRELATED, agg="MAX"), {"emp": [emp(2)], "dept": DEPTS}),
    # a bare name the subquery's own table has is the subquery's, not the outer table's
    (
        "SELECT empno FROM emp WHERE EXISTS(SELECT 1 FROM dept WHERE deptno = 10)",
        "SELECT empno FROM emp WHERE EXISTS(SELECT 1 FROM dept WHERE emp.deptno = 10)",
        {"emp": [emp(1, deptno=10)], "dept": [(20, "x")]},
    ),
    (
        "SELECT empno > ANY (SELECT deptno FROM dept WHERE job = name) FROM emp",
        "SELECT empno > ANY (SELECT emp.deptno FROM dept WHERE job = name) FROM emp",
        {"emp": [emp(5, deptno=9)], "dept": [(1, "x")]},
    ),
    # a column list renames by position: d.name is DEPT's first column
    ("SELECT d.name FROM dept AS d(name, other)", "SELECT name FROM dept", {"dept": [(1, "x")]}),
    ("SELECT MIN(name) FROM dept AS d(name, other)", "SELECT MIN(deptno0) FROM dept AS d(other, deptno0)", {"dept": [(1, "x")]}),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_differ_and_are_not_proved(left, right, rows):
    assert differ_on_duckdb(left, right, rows)
    assert not proved(left, right)


def test_rbot_pairs_agree_on_random_tables():
    """The three proved pairs on random R-Bot tables (NOT NULL columns filled, duplicates in the non-key columns)."""

    pairs = [pair(ANY_IN_PROJECT, emp="emp", agg="MIN"), pair(SELECT_ANY_CORRELATED, agg="MIN"), pair(WHERE_ANY_CORRELATED, agg="MIN")]
    rng = random.Random(7)
    for _ in range(25):
        rows = {
            "emp": [emp(number, job=rng.choice("xyz"), deptno=rng.choice([1, 2, 2, 3])) for number in rng.sample(range(6), rng.randint(0, 4))],
            "dept": [(number, rng.choice("xyy")) for number in rng.sample(range(6), rng.randint(0, 4))],
        }
        for left, right in pairs:
            assert not differ_on_duckdb(left, right, rows), (left, rows)
