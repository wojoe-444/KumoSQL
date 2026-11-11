import random
import sqlite3
from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, TableConstraints

U = "(SELECT a, k FROM A UNION ALL SELECT a, k FROM B)"

PROVEN = [
    pytest.param(
        f"SELECT u.a, c.x FROM {U} AS u JOIN C AS c ON u.k = c.k",
        "SELECT a.a, c.x FROM A AS a JOIN C AS c ON a.k = c.k UNION ALL SELECT b.a, c.x FROM B AS b JOIN C AS c ON b.k = c.k",
        id="join-distributes-over-union-all",
    ),
    pytest.param(
        f"SELECT u.a FROM {U} AS u WHERE u.a > 1",
        "SELECT a FROM A WHERE a > 1 UNION ALL SELECT a FROM B WHERE a > 1",
        id="filter-distributes-over-union-all",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u",
        "SELECT SUM(s) AS s FROM (SELECT SUM(a) AS s FROM A UNION ALL SELECT SUM(a) AS s FROM B) AS v",
        id="sum-of-union-all-is-sum-of-partial-sums",
    ),
    pytest.param(
        f"SELECT COUNT(*) AS n FROM {U} AS u",
        "SELECT SUM(n) AS n FROM (SELECT COUNT(*) AS n FROM B UNION ALL SELECT COUNT(*) AS n FROM A) AS v",
        id="count-of-union-all-is-sum-of-partial-counts",
    ),
    pytest.param(
        f"SELECT MAX(u.a) AS m FROM {U} AS u",
        "SELECT MAX(m) AS m FROM (SELECT MAX(a) AS m FROM A UNION ALL SELECT MAX(a) AS m FROM B) AS v",
        id="max-of-union-all",
    ),
    pytest.param(
        f"SELECT u.k, COUNT(*) AS n FROM {U} AS u GROUP BY u.k",
        "SELECT v.k, SUM(v.n) AS n FROM (SELECT k, COUNT(*) AS n FROM A GROUP BY k "
        "UNION ALL SELECT k, COUNT(*) AS n FROM B GROUP BY k) AS v GROUP BY v.k",
        id="grouped-count-of-union-all",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u WHERE u.a > 1",
        "SELECT SUM(s) AS s FROM (SELECT SUM(a) AS s FROM A WHERE a > 1 UNION ALL SELECT SUM(a) AS s FROM B WHERE a > 1) AS v",
        id="filtered-sum-of-union-all",
    ),
]

NOT_PROVEN = [
    pytest.param(
        f"SELECT COUNT(*) AS n FROM {U} AS u",
        "SELECT COUNT(*) AS n FROM A UNION ALL SELECT COUNT(*) AS n FROM B",
        id="count-of-union-is-not-union-of-counts",
    ),
    pytest.param(
        f"SELECT SUM(u.a) AS s FROM {U} AS u",
        "SELECT SUM(a) AS s FROM A",
        id="dropping-a-branch",
    ),
    pytest.param(
        f"SELECT AVG(u.a) AS s FROM {U} AS u",
        "SELECT AVG(s) AS s FROM (SELECT AVG(a) AS s FROM A UNION ALL SELECT AVG(a) AS s FROM B) AS v",
        id="average-of-averages",
    ),
    pytest.param(
        "SELECT DISTINCT u.a FROM (SELECT a FROM A UNION ALL SELECT a FROM B) AS u",
        "SELECT DISTINCT a FROM A UNION ALL SELECT DISTINCT a FROM B",
        id="distinct-does-not-distribute",
    ),
]


@pytest.mark.parametrize("left,right", PROVEN)
def test_algebraic_identities_are_proven(left, right):
    assert prove_equivalent_algebraic(left, right).proven


@pytest.mark.parametrize("left,right", NOT_PROVEN)
def test_non_identities_are_not_proven(left, right):
    assert not prove_equivalent_algebraic(left, right).proven


def test_normalize_is_idempotent():
    for case in PROVEN:
        for sql in case.values:
            once = normalize(sql)
            assert normalize(once) == once


# Union branches that name their columns differently: copies of the outer select
# must still read the first branch's names positionally.
MISALIGNED = [
    "SELECT u.a FROM (SELECT a, k FROM A UNION ALL SELECT k, a FROM A) AS u",
    "SELECT u.a, COUNT(*) AS n FROM (SELECT a, k FROM A UNION ALL SELECT k AS b, a AS c FROM B) AS u GROUP BY u.a",
    "SELECT SUM(u.k) AS s FROM (SELECT a, k FROM A UNION ALL SELECT k, a FROM B) AS u WHERE u.a > 0",
    "SELECT u.x, c.x FROM (SELECT a AS x, k FROM A UNION ALL SELECT k, a FROM B) AS u JOIN C AS c ON u.k = c.k",
]


@pytest.mark.parametrize(
    "sql", [v for case in PROVEN + NOT_PROVEN for v in case.values] + MISALIGNED
)
def test_normalization_preserves_results_on_random_databases(sql):
    rng = random.Random(7)
    normalized = normalize(sql)
    runnable = sqlglot.transpile(normalized, read="bigquery", write="sqlite")[0]
    for _ in range(40):
        db = sqlite3.connect(":memory:")
        for table in ("A", "B", "C"):
            db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
            for _ in range(rng.choice([0, 0, 1, 3, 5])):
                row = [rng.choice([None, 0, 1, 2, 3]) for _ in range(3)]
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", row)
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(runnable).fetchall()), normalized


def test_distinct_aggregate_regrouping():
    schema = {"emp": ["deptno", "sal", "comm"]}
    flat = "SELECT deptno, SUM(comm), SUM(DISTINCT sal), COUNT(DISTINCT sal) FROM emp GROUP BY deptno"
    staged = (
        "SELECT deptno, SUM(s), SUM(sal), COUNT(sal) FROM "
        "(SELECT deptno, sal, SUM(comm) AS s FROM emp GROUP BY deptno, sal) t GROUP BY deptno"
    )
    assert prove_equivalent_algebraic(flat, staged, schema=schema, compare_names=False).proven
    # COUNT(*) of the staged rows counts NULL values of sal as a group: not COUNT(DISTINCT sal).
    wrong = "SELECT deptno, COUNT(*) FROM (SELECT deptno, sal FROM emp GROUP BY deptno, sal) t GROUP BY deptno"
    count = "SELECT deptno, COUNT(DISTINCT sal) FROM emp GROUP BY deptno"
    assert not prove_equivalent_algebraic(count, wrong, schema=schema, compare_names=False).proven
    # SUM of a staged MIN is not a SUM.
    bad = (
        "SELECT deptno, SUM(s) FROM (SELECT deptno, sal, MIN(comm) AS s FROM emp GROUP BY deptno, sal) t GROUP BY deptno"
    )
    assert not prove_equivalent_algebraic(
        "SELECT deptno, SUM(comm) FROM emp GROUP BY deptno", bad, schema=schema, compare_names=False
    ).proven


def test_aggregates_of_group_keys():
    schema = {"emp": ["deptno", "sal"]}
    left = "SELECT sal, MIN(sal), SUM(DISTINCT sal) FROM emp GROUP BY sal"
    right = "SELECT sal, sal, sal FROM emp GROUP BY sal"
    assert prove_equivalent_algebraic(left, right, schema=schema, compare_names=False).proven
    # SUM(sal) without DISTINCT adds each row's value: not the key.
    assert not prove_equivalent_algebraic(
        "SELECT sal, SUM(sal) FROM emp GROUP BY sal", right.replace("sal, sal, sal", "sal, sal"), schema=schema, compare_names=False
    ).proven


def test_constant_dates_are_folded():
    plain = "SELECT a FROM t WHERE d >= DATE '1994-09-01' AND d < DATE '1994-12-01'"
    spark = "SELECT a FROM t WHERE d >= date('1994-09-01 +08') AND d < date('1994-09-01 +08') + INTERVAL '3' MONTH"
    clamped = "SELECT a FROM t WHERE d < DATE_ADD(DATE '2020-01-31', INTERVAL 1 MONTH)"
    leap = "SELECT a FROM t WHERE d < DATE '2020-02-29'"
    schema = {"t": ["a", "d"]}
    assert prove_equivalent_algebraic(plain, spark, schema=schema, dialect="mysql").proven
    assert prove_equivalent_algebraic(clamped, leap, schema=schema).proven
    other = "SELECT a FROM t WHERE d >= DATE '1994-09-01' AND d < DATE '1994-12-02'"
    assert not prove_equivalent_algebraic(plain, other, schema=schema).proven


def test_string_and_date_ordering_is_fast_and_exact():
    schema = {"t": ["a", "d"]}
    base = "SELECT a FROM t WHERE d >= DATE '1994-09-01' AND d < DATE '1994-12-01'"
    assert prove_equivalent_algebraic(
        base, "SELECT a FROM t WHERE NOT (d < DATE '1994-09-01') AND NOT (d >= DATE '1994-12-01')", schema=schema
    ).proven
    # Strings exist between '1994-08-31' and '1994-09-01', so these differ.
    assert not prove_equivalent_algebraic(
        base, "SELECT a FROM t WHERE d > DATE '1994-08-31' AND d < DATE '1994-12-01'", schema=schema
    ).proven


# Pre-aggregated tables joined and re-aggregated: the flat form must give the same rows.
EAGER = [
    "SELECT COALESCE(SUM(p.c * q.c), 0) AS n FROM (SELECT k, COUNT(*) AS c FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k = q.k",
    "SELECT p.k, SUM(p.s * q.c) AS t FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k = q.k GROUP BY p.k",
    "SELECT q.k, MIN(p.m) AS lo, MAX(p.m) AS hi, SUM(p.c) AS n FROM "
    "(SELECT k, MIN(x) AS m, COUNT(*) AS c FROM A WHERE a > 0 GROUP BY k) AS p JOIN C AS q ON p.k = q.k GROUP BY q.k",
    "SELECT q.a, SUM(p.s) AS t FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p JOIN C AS q ON p.k = q.k GROUP BY q.a",
    # A join of grouped tables read off by arithmetic.
    "SELECT p.k, q.k AS k2, p.s * q.c AS t, p.c * q.c AS n, p.m FROM "
    "(SELECT k, SUM(x) AS s, COUNT(*) AS c, MIN(x) AS m FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k <= q.k",
    "SELECT q.k, p.s, p.c FROM (SELECT k, SUM(x) AS s, COUNT(*) AS c FROM A GROUP BY k) AS p "
    "JOIN (SELECT k FROM B GROUP BY k) AS q ON p.k = q.k",
    "SELECT p.k, p.s + 1 AS t FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p "
    "JOIN (SELECT k, COUNT(*) AS c FROM B GROUP BY k) AS q ON p.k = q.k",
    # Not rewritable: these would change how many times a group is counted.
    "SELECT COUNT(*) AS n FROM (SELECT k, COUNT(*) AS c FROM A GROUP BY k) AS p JOIN C AS q ON p.k = q.k",
    "SELECT SUM(q.x) AS n FROM (SELECT k, COUNT(*) AS c FROM A GROUP BY k) AS p JOIN C AS q ON p.k = q.k",
    "SELECT p.k, SUM(p.s + 1) AS n FROM (SELECT k, SUM(x) AS s FROM A GROUP BY k) AS p GROUP BY p.k",
]


@pytest.mark.parametrize("sql", EAGER)
def test_eager_aggregation_unnesting_preserves_results(sql):
    rng = random.Random(11)
    normalized = normalize(sql)
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        for table in ("A", "B", "C"):
            db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
            for _ in range(rng.choice([0, 1, 2, 4, 6])):
                row = [rng.choice([None, 0, 1, 2]) for _ in range(3)]
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", row)
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_eager_aggregation_is_proved_equal_to_the_flat_join():
    schema = {"a": ["a", "k", "x"], "b": ["a", "k", "x"]}
    eager = (
        "SELECT p.k, SUM(p.s * q.c) AS t FROM (SELECT k, SUM(x) AS s FROM a GROUP BY k) AS p "
        "JOIN (SELECT k, COUNT(*) AS c FROM b GROUP BY k) AS q ON p.k = q.k GROUP BY p.k"
    )
    flat = "SELECT a.k, SUM(a.x) AS t FROM a JOIN b ON a.k = b.k GROUP BY a.k"
    assert prove_equivalent_algebraic(eager, flat, schema=schema).proven
    # Summing the count instead of the sum is a different query.
    other = eager.replace("SUM(p.s * q.c)", "SUM(q.c)")
    assert not prove_equivalent_algebraic(other, flat, schema=schema).proven


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT * FROM dept WHERE (SELECT 1) = 1", "SELECT * FROM dept WHERE 1 = 1"),
        ("SELECT * FROM dept WHERE deptno IN (SELECT deptno FROM dept WHERE FALSE)", "SELECT * FROM dept WHERE FALSE"),
        ("SELECT * FROM dept WHERE NOT EXISTS (SELECT 1 FROM dept LIMIT 0)", "SELECT * FROM dept"),
        ("SELECT COUNT(DISTINCT deptno) FILTER(WHERE 3 > 2) FROM dept", "SELECT COUNT(DISTINCT deptno) FROM dept"),
        ("SELECT SUM(deptno) FILTER(WHERE deptno > 2) FROM dept", "SELECT SUM(CASE WHEN deptno > 2 THEN deptno END) FROM dept"),
    ],
)
def test_trivial_identities_are_proven(left, right):
    result = prove_equivalent_algebraic(left, right, schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_filter_on_a_different_condition_is_not_proven():
    result = prove_equivalent_algebraic(
        "SELECT SUM(deptno) FILTER(WHERE deptno > 2) FROM dept",
        "SELECT SUM(deptno) FILTER(WHERE deptno > 3) FROM dept",
        schema={"dept": ["deptno"]}, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


SCHEMA = {"emp": ["empno", "deptno", "sal"], "dept": ["deptno", "name"]}


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql")


def test_the_same_scalar_subquery_on_both_sides_is_one_value():
    result = _prove(
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 3) AND deptno = 1",
        "SELECT empno FROM emp WHERE deptno = 1 AND (SELECT MAX(sal) FROM emp WHERE deptno > 3) = sal",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # a global aggregate returns exactly one row, so nothing is assumed about its size
    assert "scalar subqueries return at most one row" not in " ".join(result.assumptions)


def test_a_scalar_subquery_that_may_return_several_rows_is_listed_as_an_assumption():
    left = "SELECT empno FROM emp WHERE sal = (SELECT sal FROM emp WHERE empno = 7) AND deptno = 1"
    right = "SELECT empno FROM emp WHERE deptno = 1 AND (SELECT sal FROM emp WHERE empno = 7) = sal"
    result = _prove(left, right)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert "scalar subqueries return at most one row" in " ".join(result.assumptions)
    from kumosql.smt_equivalence import TableConstraints

    keyed = prove_equivalent_algebraic(
        left, right, schema=SCHEMA, compare_names=False, dialect="mysql",
        constraints={"emp": TableConstraints(not_null=frozenset({"empno"}), keys=(("empno",),))},
    )
    assert keyed.status is SmtStatus.PROVEN_EQUIVALENT, keyed.reason
    assert "scalar subqueries return at most one row" not in " ".join(keyed.assumptions)


def test_differently_written_equivalent_scalar_subqueries_are_matched():
    result = _prove(
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 3)",
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(x.sal) FROM (SELECT sal FROM emp WHERE deptno > 3 AND deptno IS NOT NULL) AS x)",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_different_scalar_subqueries_are_not_equated():
    result = _prove(
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 3)",
        "SELECT empno FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > 4)",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_correlated_scalar_subquery_is_never_replaced_by_a_shared_value():
    # The same text reads a different outer column on each side: it must not be equated.
    result = _prove(
        "SELECT e.empno FROM emp e WHERE e.sal = (SELECT MAX(sal) FROM emp WHERE deptno = e.deptno)",
        "SELECT e.empno FROM emp e WHERE e.sal = (SELECT MAX(sal) FROM emp WHERE deptno = e.empno)",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_an_unresolvable_column_counts_as_correlated():
    from kumosql import scalar_subqueries
    import sqlglot

    tree = sqlglot.parse_one("SELECT 1 FROM emp WHERE sal = (SELECT MAX(sal) FROM emp WHERE deptno > mystery)")
    node = next(tree.find_all(sqlglot.exp.Subquery))
    assert not scalar_subqueries.is_uncorrelated(node, SCHEMA)
    assert not scalar_subqueries.is_uncorrelated(node, None)


@pytest.mark.parametrize(
    "left, right",
    [
        # column names are case-insensitive
        ("SELECT T.EMPNO FROM EMP AS T WHERE T.SAL > 1", "SELECT t.empno FROM EMP AS t WHERE t.sal > 1"),
        # a UNION ALL source is read only for the columns the query uses, in any order
        (
            "SELECT t.ename, AVG(t.empno) FROM (SELECT * FROM emp UNION ALL SELECT * FROM emp) AS t GROUP BY t.ename",
            "SELECT t.ename, AVG(t.empno) FROM (SELECT ename, empno FROM emp UNION ALL SELECT ename, empno FROM emp) AS t GROUP BY t.ename",
        ),
        # a HAVING conjunct on the group key is a WHERE, whatever else the HAVING holds
        (
            "SELECT name FROM dept WHERE name > 'b' GROUP BY name HAVING name > 'c' AND (COUNT(*) > 3 OR name < 'z')",
            "SELECT name FROM dept WHERE name > 'b' AND name > 'c' GROUP BY name HAVING COUNT(*) > 3 OR name < 'z'",
        ),
        # GROUP BY TRUE over constants is an existence test
        (
            "SELECT e.empno FROM emp e WHERE EXISTS (SELECT 1 FROM emp WHERE empno < 20)",
            "SELECT e.empno FROM emp e, (SELECT 1 AS i FROM emp WHERE empno < 20 GROUP BY TRUE) AS t",
        ),
        # the same derived relation under different aliases is one relation
        (
            "SELECT d.deptno FROM dept d RIGHT JOIN (SELECT x.deptno FROM emp x WHERE x.sal > 1 GROUP BY x.deptno) t ON d.deptno = t.deptno",
            "SELECT d0.deptno FROM dept d0 RIGHT JOIN (SELECT y.deptno FROM emp y WHERE y.sal > 1 GROUP BY y.deptno) u ON d0.deptno = u.deptno",
        ),
    ],
)
def test_more_shapes_are_proven(left, right):
    result = prove_equivalent_algebraic(
        left, right, schema={"emp": ["empno", "ename", "sal", "deptno"], "dept": ["deptno", "name"]}, compare_names=False, dialect="mysql"
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_a_constant_set_source_still_needs_the_same_filter():
    result = prove_equivalent_algebraic(
        "SELECT e.empno FROM emp e WHERE EXISTS (SELECT 1 FROM emp WHERE empno < 20)",
        "SELECT e.empno FROM emp e, (SELECT 1 AS i FROM emp WHERE empno < 21 GROUP BY TRUE) AS t",
        schema={"emp": ["empno"]}, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


OJ_SCHEMA = {"emp": ["ename", "sal", "deptno"], "bonus": ["ename"]}


@pytest.mark.parametrize(
    "left, right",
    [
        (
            "SELECT COUNT(*), CASE WHEN emp.sal < 11 THEN -1 * emp.sal ELSE emp.sal END FROM bonus LEFT JOIN emp ON bonus.ename = emp.ename GROUP BY CASE WHEN emp.sal < 11 THEN -1 * emp.sal ELSE emp.sal END",
            "SELECT COUNT(*), t3.e FROM (SELECT b.ename FROM bonus b) t2 LEFT JOIN (SELECT x.ename, CASE WHEN x.sal < 11 THEN -1 * x.sal ELSE x.sal END AS e FROM emp x) t3 ON t2.ename = t3.ename GROUP BY t3.e",
        ),
        (
            "SELECT COUNT(*), emp.deptno FROM emp FULL JOIN bonus ON emp.ename = bonus.ename GROUP BY emp.deptno",
            "SELECT COUNT(*), a.deptno FROM emp a FULL JOIN bonus b ON a.ename = b.ename GROUP BY a.deptno",
        ),
    ],
)
def test_aggregates_over_outer_joins(left, right):
    result = prove_equivalent_algebraic(left, right, schema=OJ_SCHEMA, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_outer_join_and_inner_join_aggregates_differ():
    result = prove_equivalent_algebraic(
        "SELECT COUNT(*), emp.deptno FROM bonus LEFT JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        "SELECT COUNT(*), emp.deptno FROM bonus JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        schema=OJ_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_left_join_is_not_a_right_join_in_an_aggregate():
    result = prove_equivalent_algebraic(
        "SELECT COUNT(*), emp.deptno FROM bonus LEFT JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        "SELECT COUNT(*), emp.deptno FROM bonus RIGHT JOIN emp ON bonus.ename = emp.ename GROUP BY emp.deptno",
        schema=OJ_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT COUNT(NULL) FROM dept", "SELECT 0"),
        ("SELECT COUNT(DISTINCT (deptno = NULL)) FROM dept", "SELECT 0"),
        ("SELECT SUM(NULL) FROM dept", "SELECT NULL"),
        ("SELECT UPPER(UPPER(name)) FROM dept", "SELECT UPPER(name) FROM dept"),
        ("SELECT POSITIVE(deptno) FROM dept", "SELECT deptno FROM dept"),
        ('SELECT CONCAT("a", CONCAT("b", "c")) AS c1 FROM dept', 'SELECT CONCAT("a", "b", "c") AS c1 FROM dept'),
    ],
)
def test_constant_aggregates_and_function_identities(left, right):
    result = prove_equivalent_algebraic(left, right, schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT COUNT(NULL) FROM dept", "SELECT 1"),
        ("SELECT COUNT(NULL) FROM dept", "SELECT 0 FROM dept"),  # one row versus one per department
        ("SELECT LOWER(UPPER(name)) FROM dept", "SELECT UPPER(name) FROM dept"),
        ("SELECT UPPER(LOWER(name)) FROM dept", "SELECT UPPER(name) FROM dept"),  # UPPER(LOWER('İ')) is 'I'
        ("SELECT deptno FROM dept WHERE (deptno, name) IN (SELECT deptno, name FROM dept WHERE deptno > 1)", "SELECT deptno FROM dept WHERE deptno IN (SELECT deptno FROM dept WHERE deptno > 1)"),
    ],
)
def test_constant_aggregate_and_tuple_in_negatives(left, right):
    result = prove_equivalent_algebraic(left, right, schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_tuple_in_is_an_existence_test_on_every_column():
    result = prove_equivalent_algebraic(
        "SELECT deptno FROM dept WHERE (deptno, name) IN (SELECT deptno, name FROM dept WHERE deptno > 1)",
        "SELECT deptno FROM dept WHERE EXISTS (SELECT 1 FROM dept d WHERE d.deptno = dept.deptno AND d.name = dept.name AND d.deptno > 1)",
        schema={"dept": ["deptno", "name"]}, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


LINEITEM = {"lineitem": ["l_suppkey", "l_extendedprice", "l_shipdate"]}


def test_aggregate_over_grouped_derived_table_in_a_scalar_subquery_keeps_its_grouping():
    # MAX over per-supplier totals is not a flat aggregate: the derived table must stay grouped.
    result = prove_equivalent_algebraic(
        "SELECT (SELECT MAX(t) FROM (SELECT SUM(l_extendedprice) AS t FROM lineitem GROUP BY l_suppkey) AS d) AS m",
        "SELECT (SELECT MAX(l_extendedprice) FROM lineitem) AS m",
        schema=LINEITEM, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_filtering_derived_table_under_a_grouping_is_folded_in():
    result = prove_equivalent_algebraic(
        "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice) AS t FROM lineitem WHERE l_shipdate >= 5 GROUP BY l_suppkey) AS r",
        "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice) AS t FROM (SELECT l_suppkey, l_extendedprice FROM lineitem WHERE l_shipdate >= 5) AS z GROUP BY l_suppkey) AS q",
        schema=LINEITEM, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_null_guards_and_trailing_zeros_do_not_change_a_derived_relation():
    left = "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice * (1 - l_suppkey)) AS t FROM lineitem WHERE l_shipdate >= 5 GROUP BY l_suppkey) AS r"
    right = (
        "SELECT MAX(t) FROM (SELECT SUM(l_extendedprice * (1.00 - l_suppkey)) AS t FROM lineitem "
        "WHERE l_shipdate IS NOT NULL AND (l_shipdate >= 5) AND l_suppkey IS NOT NULL GROUP BY l_suppkey) AS q"
    )
    constraints = {"lineitem": TableConstraints(not_null=frozenset({"l_suppkey"}))}
    result = prove_equivalent_algebraic(left, right, schema=LINEITEM, constraints=constraints, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # Without the declaration the guard on l_suppkey filters rows, so the queries differ.
    result = prove_equivalent_algebraic(left, right, schema=LINEITEM, compare_names=False, dialect="mysql")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_null_guard_removal_keeps_or_precedence():
    result = prove_equivalent_algebraic(
        "SELECT l_suppkey FROM lineitem WHERE l_shipdate IS NOT NULL AND (l_shipdate = 1 OR l_suppkey = 2) AND l_extendedprice > 0",
        "SELECT l_suppkey FROM lineitem WHERE l_shipdate = 1 OR l_suppkey = 2 AND l_extendedprice > 0",
        schema=LINEITEM, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


T_SCHEMA = {"a": ["a", "k", "x"], "b": ["a", "k", "x"]}
DECORRELATE = [
    "SELECT a.a FROM a WHERE a.x < (SELECT 2 * AVG(b.x) FROM b WHERE b.k = a.k)",
    "SELECT a.a FROM a WHERE a.x = (SELECT MIN(b.x) FROM b WHERE b.k = a.k AND b.a > 0)",
    "SELECT a.a FROM a WHERE (SELECT SUM(b.x) FROM b WHERE b.k = a.k) > a.x",
    "SELECT a.a FROM a WHERE a.x < (SELECT COUNT(*) FROM b WHERE b.k = a.k)",  # COUNT is left alone
    "SELECT a.a FROM a WHERE a.x < (SELECT MAX(b.x) FROM b WHERE b.k = a.k AND b.x > a.x)",  # not an equality
]


@pytest.mark.parametrize("sql", DECORRELATE)
def test_decorrelating_a_scalar_aggregate_preserves_results(sql):
    rng = random.Random(5)
    normalized = normalize(sql, schema=T_SCHEMA, dialect="sqlite")
    for _ in range(80):
        db = sqlite3.connect(":memory:")
        for table in ("a", "b"):
            db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
            for _ in range(rng.choice([0, 1, 3, 5])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", [rng.choice([None, 0, 1, 2, 3]) for _ in range(3)])
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_a_correlated_scalar_aggregate_equals_its_join_with_a_grouped_table():
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a WHERE a.x < (SELECT 2 * AVG(b.x) FROM b WHERE b.k = a.k)",
        "SELECT a.a FROM a JOIN (SELECT b.k, 2 * AVG(b.x) AS m FROM b GROUP BY b.k) AS g ON g.k = a.k WHERE a.x < g.m",
        schema=T_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_a_correlated_count_is_not_a_join():
    # With no matching rows COUNT reads 0, so the outer row stays; the inner join would drop it.
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a WHERE a.x > (SELECT COUNT(*) FROM b WHERE b.k = a.k)",
        "SELECT a.a FROM a JOIN (SELECT b.k, COUNT(*) AS m FROM b GROUP BY b.k) AS g ON g.k = a.k WHERE a.x > g.m",
        schema=T_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_derived_aggregates_that_differ_only_in_names_and_layers_are_one_relation():
    constraints = {"b": TableConstraints(not_null=frozenset({"k", "x"}))}
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a JOIN (SELECT k, MIN(x) AS m FROM b GROUP BY k) AS g ON g.k = a.k WHERE a.x = g.m",
        "SELECT a.a FROM a JOIN (SELECT k AS kk, MIN(x) AS lowest FROM (SELECT k, x FROM b WHERE x > 0 OR x <= 0) AS z "
        "GROUP BY k HAVING MIN(x) IS NOT NULL) AS h ON h.kk = a.k WHERE a.x = h.lowest",
        schema=T_SCHEMA, constraints=constraints, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # A different aggregate is a different relation.
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a JOIN (SELECT k, MIN(x) AS m FROM b GROUP BY k) AS g ON g.k = a.k WHERE a.x = g.m",
        "SELECT a.a FROM a JOIN (SELECT k AS kk, MAX(x) AS lowest FROM b GROUP BY k) AS h ON h.kk = a.k WHERE a.x = h.lowest",
        schema=T_SCHEMA, constraints=constraints, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def _random_ab(rng):
    db = sqlite3.connect(":memory:")
    for table in ("a", "b"):
        db.execute(f"CREATE TABLE {table} (a INT, k INT, x INT)")
        for _ in range(rng.choice([0, 1, 3, 6])):
            db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", [rng.choice([None, 0, 1, 2, 3]) for _ in range(3)])
    return db


@pytest.mark.parametrize(
    "spark, reference",
    [
        (
            "SELECT a.a FROM a LEFT SEMI JOIN b ON a.k = b.k",
            "SELECT a.a FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.k = b.k)",
        ),
        (
            "SELECT a.a FROM a LEFT ANTI JOIN b ON a.k = b.k",
            "SELECT a.a FROM a WHERE NOT EXISTS (SELECT 1 FROM b WHERE a.k = b.k)",
        ),
        (
            "SELECT a.a FROM a WHERE a.k IN (SELECT k FROM b GROUP BY k HAVING SUM(x) > 2 AND COUNT(*) < 4)",
            "SELECT a.a FROM a WHERE a.k IN (SELECT g.k FROM (SELECT k, SUM(x) AS s, COUNT(*) AS c FROM b GROUP BY k) AS g WHERE g.s > 2 AND g.c < 4)",
        ),
    ],
)
def test_semi_joins_and_grouped_in_keep_their_results(spark, reference):
    rng = random.Random(3)
    normalized = normalize(spark, schema=T_SCHEMA, dialect="mysql")
    for _ in range(80):
        db = _random_ab(rng)
        assert Counter(db.execute(reference).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_semi_join_and_in_over_a_grouped_table_are_proven_equal_to_their_plain_forms():
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a LEFT SEMI JOIN b ON a.k = b.k",
        "SELECT a.a FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.k = b.k)",
        schema=T_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a WHERE a.k IN (SELECT k FROM b GROUP BY k HAVING SUM(x) > 2)",
        "SELECT a.a FROM a LEFT SEMI JOIN (SELECT k FROM (SELECT k, SUM(x) AS s FROM b GROUP BY k) AS t WHERE s IS NOT NULL AND s > 2) AS u ON a.k = u.k",
        schema=T_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # A different threshold is a different relation.
    result = prove_equivalent_algebraic(
        "SELECT a.a FROM a WHERE a.k IN (SELECT k FROM b GROUP BY k HAVING SUM(x) > 2)",
        "SELECT a.a FROM a WHERE a.k IN (SELECT k FROM b GROUP BY k HAVING SUM(x) > 3)",
        schema=T_SCHEMA, compare_names=False, dialect="mysql",
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_an_existence_test_repeated_on_an_equal_column_is_one_test():
    schema = {"o": ["k", "v"], "l": ["k", "q"], "b": ["k", "x"]}
    once = "SELECT o.k FROM o, l WHERE o.k = l.k AND EXISTS (SELECT 1 FROM b WHERE b.k = o.k)"
    twice = once + " AND EXISTS (SELECT 1 FROM b WHERE b.k = l.k)"
    result = prove_equivalent_algebraic(once, twice, schema=schema, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # Without the join condition the second test is about a different column.
    loose = "SELECT o.k FROM o, l WHERE EXISTS (SELECT 1 FROM b WHERE b.k = o.k) AND EXISTS (SELECT 1 FROM b WHERE b.k = l.k)"
    result = prove_equivalent_algebraic(
        "SELECT o.k FROM o, l WHERE EXISTS (SELECT 1 FROM b WHERE b.k = o.k)", loose, schema=schema, compare_names=False, dialect="mysql"
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


WINDOW_SCHEMA = {"t": ["a", "b", "c"]}


def test_window_functions_over_the_same_input_are_proven_equal():
    left = "SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY c) AS n FROM t WHERE a > 1 QUALIFY n = 1"
    right = "SELECT x.a, x.n FROM (SELECT t0.a, ROW_NUMBER() OVER (PARTITION BY t0.b ORDER BY t0.c) AS n FROM t AS t0 WHERE t0.a > 1) AS x WHERE x.n = 1"
    result = prove_equivalent_algebraic(left, right, schema=WINDOW_SCHEMA, compare_names=False, dialect="bigquery")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert any("window functions" in note for note in result.assumptions)


@pytest.mark.parametrize(
    "right",
    [
        # another partition, another order, another function
        "SELECT a, ROW_NUMBER() OVER (PARTITION BY a ORDER BY c) AS n FROM t WHERE a > 1 QUALIFY n = 1",
        "SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY c DESC) AS n FROM t WHERE a > 1 QUALIFY n = 1",
        "SELECT a, RANK() OVER (PARTITION BY b ORDER BY c) AS n FROM t WHERE a > 1 QUALIFY n = 1",
        # a filter applied after the window keeps rows the window already counted
        "SELECT a, n FROM (SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY c) AS n FROM t) AS x WHERE a > 1 AND n = 1",
        "SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY c) AS n FROM t WHERE a > 1 QUALIFY n = 2",
    ],
)
def test_window_functions_that_differ_are_not_proven_equal(right):
    left = "SELECT a, ROW_NUMBER() OVER (PARTITION BY b ORDER BY c) AS n FROM t WHERE a > 1 QUALIFY n = 1"
    result = prove_equivalent_algebraic(left, right, schema=WINDOW_SCHEMA, compare_names=False, dialect="bigquery")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, SUM(c) OVER (PARTITION BY b) AS s FROM t WHERE a > 0",
        "SELECT a + 1 AS a1, COUNT(*) OVER (PARTITION BY b) AS n, c FROM t WHERE c IS NOT NULL ORDER BY a",
        "SELECT DISTINCT b, MAX(c) OVER (PARTITION BY b) AS m FROM t",
        "SELECT a, b FROM t WHERE a IN (SELECT a FROM (SELECT a, SUM(c) OVER (PARTITION BY b) AS s FROM t) AS q WHERE s > 2)",
    ],
)
def test_isolating_windows_preserves_results(sql):
    rng = random.Random(9)
    normalized = normalize(sql, schema=WINDOW_SCHEMA, dialect="sqlite")
    assert "kqw" in normalized
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (a INT, b INT, c INT)")
        for _ in range(rng.choice([0, 1, 4, 7])):
            db.execute("INSERT INTO t VALUES (?, ?, ?)", [rng.choice([None, 0, 1, 2]) for _ in range(3)])
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_avg_is_a_sum_divided_by_a_count():
    schema = {"dept": ["deptno", "name"]}
    constraints = {"dept": TableConstraints(not_null=frozenset({"deptno"}))}
    avg = "SELECT name, AVG(deptno) FROM dept GROUP BY name"
    quotient = "SELECT name, SUM(deptno) / COUNT(*) FROM dept GROUP BY name"
    result = prove_equivalent_algebraic(avg, quotient, schema=schema, constraints=constraints, compare_names=False, dialect="mysql")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    # COUNT(*) counts NULL values too, so without the NOT NULL fact the quotient differs.
    result = prove_equivalent_algebraic(avg, quotient, schema=schema, compare_names=False, dialect="mysql")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
    result = prove_equivalent_algebraic(avg, "SELECT name, SUM(deptno) / COUNT(DISTINCT deptno) FROM dept GROUP BY name", schema=schema, constraints=constraints, compare_names=False, dialect="mysql")
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


JOIN_SCHEMA = {"o": ["id", "k", "amount"], "c": ["k", "name"], "d": ["k", "tag"]}


def _random_ocd(rng):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE o (id INT, k INT, amount INT)")
    db.execute("CREATE TABLE c (k INT, name INT)")
    db.execute("CREATE TABLE d (k INT, tag INT)")
    for table in ("o", "c", "d"):
        for _ in range(rng.choice([0, 1, 3, 5])):
            width = 3 if table == "o" else 2
            db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * width)})", [rng.choice([None, 0, 1, 2]) for _ in range(width)])
    return db


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id, c.name FROM o JOIN c USING (k)",
        "SELECT id, k, name FROM o JOIN c USING (k) WHERE k > 0",
        "SELECT o.id, k FROM o LEFT JOIN c USING (k)",
        "SELECT id, name, tag FROM o JOIN c USING (k) JOIN d USING (k)",
        "WITH x AS (SELECT * FROM o WHERE amount > 0), y AS (SELECT k, id FROM x) SELECT y.id, c.name FROM y JOIN c ON y.k = c.k",
        "WITH x AS (SELECT k, amount FROM o) SELECT a.k, b.amount FROM x a JOIN x b ON a.k = b.k",
    ],
)
def test_using_and_with_rewrites_keep_their_results(sql):
    rng = random.Random(21)
    normalized = normalize(sql, schema=JOIN_SCHEMA, dialect="sqlite")
    assert "USING" not in normalized and "WITH" not in normalized
    for _ in range(80):
        db = _random_ocd(rng)
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


def test_star_after_using_lists_the_merged_column_first_and_except_drops_columns():
    merged = normalize("SELECT * FROM o JOIN c USING (k)", schema=JOIN_SCHEMA, dialect="bigquery")
    explicit = normalize(
        "SELECT o.k, o.id, o.amount, c.name FROM o JOIN c ON o.k = c.k", schema=JOIN_SCHEMA, dialect="bigquery"
    )
    assert merged == explicit
    result = prove_equivalent_algebraic(
        "SELECT * EXCEPT (amount) REPLACE (k + 1 AS k) FROM o", "SELECT id, k + 1 AS k FROM o", schema=JOIN_SCHEMA, compare_names=False
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_countif_and_safe_divide_have_their_plain_spellings():
    schema = {"o": ["id", "k", "amount"]}
    for left, right in [
        ("SELECT k, COUNTIF(amount > 1) AS n FROM o GROUP BY k", "SELECT k, COUNT(CASE WHEN amount > 1 THEN 1 END) AS n FROM o GROUP BY k"),
        ("SELECT SAFE_DIVIDE(amount, k) AS q FROM o", "SELECT IF(k = 0, NULL, amount / k) AS q FROM o"),
    ]:
        result = prove_equivalent_algebraic(left, right, schema=schema, compare_names=False)
        assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    result = prove_equivalent_algebraic(
        "SELECT SAFE_DIVIDE(amount, k) AS q FROM o", "SELECT amount / k AS q FROM o", schema=schema, compare_names=False
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


PULL_UP_SCHEMA = {"orders": ["order_id", "customer_id", "amount"], "customers": ["customer_id", "name", "country"]}
PULL_UP_KEYS = {"customers": TableConstraints(keys=(("customer_id",),))}
PULL_UP_GROUPED = (
    "SELECT c.name, t.s FROM customers c JOIN "
    "(SELECT customer_id, SUM(amount) AS s FROM orders GROUP BY customer_id) t "
    "ON c.customer_id = t.customer_id WHERE t.s > 5 AND c.country = 'US'"
)
PULL_UP_FLAT = (
    "SELECT c.name, SUM(o.amount) AS s FROM customers c JOIN orders o ON c.customer_id = o.customer_id "
    "WHERE c.country = 'US' GROUP BY c.customer_id, c.name HAVING SUM(o.amount) > 5"
)


def test_keyed_table_joined_to_grouped_fact_is_the_flat_aggregate():
    result = prove_equivalent_algebraic(
        PULL_UP_GROUPED, PULL_UP_FLAT, schema=PULL_UP_SCHEMA, constraints=PULL_UP_KEYS
    )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT


def test_pull_up_needs_the_declared_key():
    result = prove_equivalent_algebraic(PULL_UP_GROUPED, PULL_UP_FLAT, schema=PULL_UP_SCHEMA)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_pull_up_near_miss_with_different_threshold():
    result = prove_equivalent_algebraic(
        PULL_UP_GROUPED, PULL_UP_FLAT.replace("> 5", "> 6"), schema=PULL_UP_SCHEMA, constraints=PULL_UP_KEYS
    )
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_pull_up_preserves_results_on_random_databases():
    rng = random.Random(11)
    flat = normalize(PULL_UP_GROUPED, schema=PULL_UP_SCHEMA, keys={"customers": [("customer_id",)]})
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE customers (customer_id INT PRIMARY KEY, name TEXT, country TEXT)")
        db.execute("CREATE TABLE orders (order_id INT, customer_id INT, amount INT)")
        for i in range(rng.choice([0, 2, 4])):
            db.execute("INSERT INTO customers VALUES (?, ?, ?)", (i, rng.choice("ab"), rng.choice(["US", "DE"])))
        for i in range(rng.choice([0, 3, 8])):
            db.execute("INSERT INTO orders VALUES (?, ?, ?)", (i, rng.choice([None, 0, 1, 2, 3]), rng.choice([None, 1, 3, 9])))
        assert Counter(db.execute(PULL_UP_GROUPED).fetchall()) == Counter(db.execute(flat).fetchall()), flat


OUTER_SCHEMA = {"customers": ["cid", "name"], "orders": ["oid", "ocid", "note"]}
OUTER_COUNTS = "SELECT cid, COUNT(oid) AS n FROM customers LEFT JOIN orders ON cid = ocid AND note <> 'x' GROUP BY cid"
OUTER_COUNTS_DERIVED = (
    "SELECT cid, COUNT(oid) AS n FROM (SELECT c.cid, o.oid FROM customers AS c LEFT JOIN "
    "(SELECT oid, ocid FROM orders WHERE note <> 'x') AS o ON c.cid = o.ocid) AS j GROUP BY cid"
)


def test_filter_in_derived_right_side_of_left_join_is_the_on_clause():
    result = prove_equivalent_algebraic(OUTER_COUNTS, OUTER_COUNTS_DERIVED, schema=OUTER_SCHEMA)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_filter_moved_to_where_after_left_join_is_not_the_on_clause():
    where_form = (
        "SELECT cid, COUNT(oid) AS n FROM customers LEFT JOIN orders ON cid = ocid "
        "WHERE note <> 'x' GROUP BY cid"
    )
    result = prove_equivalent_algebraic(OUTER_COUNTS, where_form, schema=OUTER_SCHEMA)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_outer_join_rewrites_preserve_results_on_random_databases():
    rng = random.Random(5)
    for sql in (OUTER_COUNTS, OUTER_COUNTS_DERIVED):
        normalized = normalize(sql, schema=OUTER_SCHEMA)
        for _ in range(60):
            db = sqlite3.connect(":memory:")
            db.execute("CREATE TABLE customers (cid INT, name TEXT)")
            db.execute("CREATE TABLE orders (oid INT, ocid INT, note TEXT)")
            for i in range(rng.choice([0, 2, 4])):
                db.execute("INSERT INTO customers VALUES (?, ?)", (rng.choice([None, 1, 2, 3]), "n"))
            for i in range(rng.choice([0, 3, 6])):
                db.execute("INSERT INTO orders VALUES (?, ?, ?)", (i, rng.choice([None, 1, 2, 3]), rng.choice([None, "x", "y"])))
            assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(normalized).fetchall()), normalized


CAST_SCHEMA = {"t": ["id", "price", "qty"], "u": ["id", "limit_price"]}
CAST_TYPES = {"t": {"id": "INT", "price": "DECIMAL(15, 2)", "qty": "INT"}}


def test_cast_to_a_wider_decimal_is_dropped_when_the_declared_type_fits():
    plain = "SELECT t.id FROM t WHERE t.price < 5"
    cast = "SELECT t.id FROM t WHERE CAST(t.price AS DECIMAL(21, 7)) < 5"
    result = prove_equivalent_algebraic(plain, cast, schema=CAST_SCHEMA, types=CAST_TYPES)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
    assert prove_equivalent_algebraic(plain, cast, schema=CAST_SCHEMA).status is not SmtStatus.PROVEN_EQUIVALENT


def test_cast_to_a_narrower_decimal_is_kept():
    plain = "SELECT t.id FROM t WHERE t.price < 5"
    narrow = "SELECT t.id FROM t WHERE CAST(t.price AS DECIMAL(15, 1)) < 5"
    result = prove_equivalent_algebraic(plain, narrow, schema=CAST_SCHEMA, types=CAST_TYPES)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_integer_cast_to_decimal_needs_enough_digits():
    plain = "SELECT t.id FROM t WHERE t.qty > 1"
    assert prove_equivalent_algebraic(
        plain, "SELECT t.id FROM t WHERE CAST(t.qty AS DECIMAL(27, 3)) > 1", schema=CAST_SCHEMA, types=CAST_TYPES
    ).proven
    assert not prove_equivalent_algebraic(
        plain, "SELECT t.id FROM t WHERE CAST(t.qty AS DECIMAL(5, 0)) > 1", schema=CAST_SCHEMA, types=CAST_TYPES
    ).proven


def test_grouped_derived_table_columns_may_be_listed_in_any_order():
    a = "SELECT t.id FROM t JOIN (SELECT id AS k, SUM(qty) AS s FROM t GROUP BY id) g ON t.id = g.k AND t.qty < g.s"
    b = "SELECT t.id FROM t JOIN (SELECT SUM(qty) AS s, id AS k FROM t GROUP BY id) g ON t.id = g.k AND t.qty < g.s"
    assert prove_equivalent_algebraic(a, b, schema=CAST_SCHEMA).proven


def test_bigquery_column_types_make_a_widening_cast_free():
    from kumosql.prover_schema import from_bigquery

    facts = from_bigquery([("p", "d", "t", {"schema": [{"name": "id", "type": "INTEGER"}, {"name": "amt", "type": "NUMERIC"}]})])
    plain = "SELECT id FROM d.t WHERE id > 1"
    cast = "SELECT id FROM d.t WHERE CAST(id AS NUMERIC) > 1"
    with_types = prove_equivalent_algebraic(plain, cast, schema=facts.columns, constraints=facts.constraints, types=facts.types)
    assert with_types.status is SmtStatus.PROVEN_EQUIVALENT
    narrow = "SELECT id FROM d.t WHERE CAST(amt AS NUMERIC(10, 2)) > 1"
    assert not prove_equivalent_algebraic(
        "SELECT id FROM d.t WHERE amt > 1", narrow, schema=facts.columns, types=facts.types
    ).proven


REWRITE_SQL = [
    "SELECT a.id, (SELECT SUM(b.y) FROM b WHERE b.id = a.id) AS t FROM a",
    "SELECT a.id, COALESCE((SELECT MAX(b.y) FROM b WHERE b.id = a.id AND b.y > 1), 0) + 1 AS t FROM a",
    "SELECT SUM(s) AS total FROM (SELECT id, SUM(y) AS s FROM b GROUP BY id)",
    "SELECT MIN(s) AS m, MAX(s) AS x FROM (SELECT id, MIN(y) AS s FROM b GROUP BY id)",
    "SELECT a.id FROM a WHERE a.id IN (SELECT id FROM b UNION ALL SELECT x FROM a)",
    "SELECT a.id FROM a WHERE a.id NOT IN (SELECT id FROM b UNION ALL SELECT x FROM a)",
    "SELECT MIN(y) AS m, COUNT(*) AS n, SUM(y) AS s FROM b WHERE y IS NOT NULL",
    "SELECT COUNT(*) AS n FROM b WHERE y IS NOT NULL",
]


@pytest.mark.parametrize("sql", REWRITE_SQL)
def test_select_list_rollup_union_in_and_null_guard_rewrites_preserve_results(sql):
    rng = random.Random(3)
    schema = {"a": ["id", "x"], "b": ["id", "y"]}
    normalized = normalize(sql, schema=schema)
    for _ in range(80):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE a (id INT, x INT)")
        db.execute("CREATE TABLE b (id INT, y INT)")
        for table in ("a", "b"):
            for _ in range(rng.choice([0, 1, 3, 5])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?)", [rng.choice([None, 0, 1, 2, 3]) for _ in range(2)])
        runnable = sqlglot.transpile(normalized, read="bigquery", write="sqlite")[0]
        assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(runnable).fetchall()), normalized


SEMI_SCHEMA = {"p": ["k", "a"], "g": ["k", "x"], "s": ["k"]}
SEMI_NOT_NULL = {"g": TableConstraints(not_null=frozenset({"x"}))}
SEMI_OUTER = (
    "SELECT p.a FROM p JOIN (SELECT k, SUM(x) AS t FROM g GROUP BY k) AS d ON p.k = d.k "
    "WHERE EXISTS (SELECT 1 FROM s WHERE s.k = p.k) AND p.a > d.t"
)
SEMI_PUSHED = (
    "SELECT p.a FROM p JOIN (SELECT k, SUM(x) AS t FROM g WHERE EXISTS (SELECT 1 FROM s WHERE s.k = g.k) GROUP BY k) AS d "
    "ON p.k = d.k WHERE EXISTS (SELECT 1 FROM s WHERE s.k = p.k) AND p.a > d.t"
)


def test_exists_repeated_inside_a_grouped_join_partner_is_redundant():
    result = prove_equivalent_algebraic(SEMI_OUTER, SEMI_PUSHED, schema=SEMI_SCHEMA, constraints=SEMI_NOT_NULL)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


def test_exists_inside_the_grouped_side_alone_is_not_redundant():
    without = SEMI_OUTER.replace("EXISTS (SELECT 1 FROM s WHERE s.k = p.k) AND ", "")
    result = prove_equivalent_algebraic(without, SEMI_PUSHED, schema=SEMI_SCHEMA, constraints=SEMI_NOT_NULL)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_a_different_exists_inside_the_grouped_side_is_not_redundant():
    other = SEMI_PUSHED.replace("s.k = g.k)", "s.k = g.k AND s.k > 1)", 1)
    result = prove_equivalent_algebraic(SEMI_OUTER, other, schema=SEMI_SCHEMA, constraints=SEMI_NOT_NULL)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


def test_null_guard_on_a_sum_of_a_not_null_column_is_redundant():
    guarded = "SELECT d.t FROM (SELECT k, 0.5 * SUM(x) AS t FROM g GROUP BY k) AS d WHERE d.t IS NOT NULL"
    plain = "SELECT 0.5 * SUM(x) AS t FROM g GROUP BY k"
    assert prove_equivalent_algebraic(guarded, plain, schema=SEMI_SCHEMA, constraints=SEMI_NOT_NULL, compare_names=False).proven
    assert not prove_equivalent_algebraic(guarded, plain, schema=SEMI_SCHEMA, compare_names=False).proven


def test_semi_join_rewrites_preserve_results_on_random_databases():
    rng = random.Random(9)
    forms = [SEMI_OUTER, SEMI_PUSHED, "SELECT d.t FROM (SELECT k, 0.5 * SUM(x) AS t FROM g GROUP BY k) AS d WHERE d.t IS NOT NULL"]
    normalized = [normalize(f, schema=SEMI_SCHEMA, not_null={"g": frozenset({"x"})}) for f in forms]
    for _ in range(80):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE p (k INT, a INT)")
        db.execute("CREATE TABLE g (k INT, x INT NOT NULL)")
        db.execute("CREATE TABLE s (k INT)")
        for table, width in (("p", 2), ("g", 2), ("s", 1)):
            for _ in range(rng.choice([0, 1, 3, 5])):
                row = [rng.choice([None, 0, 1, 2, 3]) for _ in range(width)]
                if table == "g":
                    row[1] = rng.choice([0, 1, 2, 3])
                db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * width)})", row)
        for sql, norm in zip(forms, normalized):
            runnable = sqlglot.transpile(norm, read="bigquery", write="sqlite")[0]
            assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(runnable).fetchall()), norm


def test_group_by_constants_reads_literals_as_calcite_does():
    schema = {"emp": ["empno", "deptno", "mgr"]}
    pairs = [
        ("SELECT deptno, MAX(mgr) FROM emp GROUP BY deptno, 4", "SELECT deptno, MAX(mgr) FROM emp GROUP BY deptno"),
        ("SELECT 4, MAX(5) FROM emp GROUP BY 4, 2 + 3", "SELECT 4, MAX(5) FROM emp GROUP BY 4"),
    ]
    for left, right in pairs:
        on = prove_equivalent_algebraic(left, right, schema=schema, dialect="mysql", compare_names=False, group_by_constants=True)
        assert on.proven
    # constants-only grouping is not a global aggregate: an empty input gives no row, not one
    off = prove_equivalent_algebraic(
        "SELECT MAX(mgr) FROM emp GROUP BY 4", "SELECT MAX(mgr) FROM emp", schema=schema, dialect="mysql", compare_names=False, group_by_constants=True
    )
    assert not off.proven


INDICATOR_SCHEMA = {"p": ["id", "k", "a"], "q": ["id", "k", "b"]}
INDICATOR_KEYS = {"q": [("id",)], "p": [("id",)]}
INDICATOR_KEYED = (
    "SELECT p.id FROM p LEFT JOIN (SELECT id, 1 AS i FROM q WHERE b < 3) AS d ON p.id = d.id WHERE d.i IS NOT NULL OR p.a < 2"
)
INDICATOR_GROUPED = (
    "SELECT p.id FROM p LEFT JOIN (SELECT k, TRUE AS i FROM q WHERE b < 3 GROUP BY k, TRUE) AS d ON p.k = d.k WHERE d.i IS NOT NULL OR p.a < 2"
)
INDICATOR_DUPLICATING = (
    "SELECT p.id FROM p LEFT JOIN (SELECT k, 1 AS i FROM q WHERE b < 3) AS d ON p.k = d.k WHERE d.i IS NOT NULL"
)


def test_left_join_indicator_becomes_an_existence_test():
    exists = (
        "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM q WHERE b < 3 AND q.id = p.id) OR p.a < 2",
        "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM q WHERE b < 3 AND q.k = p.k) OR p.a < 2",
    )
    for joined, probe in zip((INDICATOR_KEYED, INDICATOR_GROUPED), exists):
        result = prove_equivalent_algebraic(
            joined, probe, schema=INDICATOR_SCHEMA, dialect="bigquery", compare_names=False, constraints={
                t: TableConstraints(keys=tuple(k)) for t, k in INDICATOR_KEYS.items()
            }
        )
        assert result.proven, result.reason


def test_left_join_indicator_needs_a_matching_key():
    # q.k is not a key, so the join repeats p rows and the existence test would drop the repeats
    result = prove_equivalent_algebraic(
        INDICATOR_DUPLICATING,
        "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM q WHERE b < 3 AND q.k = p.k)",
        schema=INDICATOR_SCHEMA,
        dialect="bigquery",
        compare_names=False,
        constraints={"q": TableConstraints(keys=(("id",),))},
    )
    assert not result.proven


def test_left_join_indicator_rewrite_preserves_results_on_random_databases():
    rng = random.Random(21)
    forms = [INDICATOR_KEYED, INDICATOR_GROUPED, INDICATOR_DUPLICATING]
    normalized = [normalize(f, schema=INDICATOR_SCHEMA, keys=INDICATOR_KEYS) for f in forms]
    for _ in range(80):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE p (id INT, k INT, a INT)")
        db.execute("CREATE TABLE q (id INT, k INT, b INT)")
        for table in ("p", "q"):
            for n in range(rng.choice([0, 1, 3, 5])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", [n, rng.choice([None, 0, 1, 2]), rng.choice([None, 0, 1, 2, 3])])
        for sql, norm in zip(forms, normalized):
            runnable = sqlglot.transpile(norm, read="bigquery", write="sqlite")[0]
            assert Counter(db.execute(sql).fetchall()) == Counter(db.execute(runnable).fetchall()), norm


def test_unnamed_values_columns_are_expr_n():
    pairs = [
        ("SELECT t.expr$0 + t.expr$1 FROM (VALUES (10, 1), (20, 3)) AS t", "SELECT * FROM (VALUES (11), (23)) AS t1"),
        ("SELECT * FROM (VALUES (10, 'x'), (20, 'y')) AS t WHERE t.expr$0 < 15", "SELECT * FROM (VALUES (10, 'x')) AS t1"),
        ("SELECT * FROM (VALUES (1), (2)) WHERE expr$0 > 1", "SELECT * FROM (VALUES (2))"),
    ]
    for left, right in pairs:
        assert prove_equivalent_algebraic(left, right, dialect="mysql", compare_names=False, exact_arithmetic=True).proven, left
    near = prove_equivalent_algebraic(
        "SELECT * FROM (VALUES (1), (2)) AS t WHERE t.expr$0 > 0", "SELECT * FROM (VALUES (2)) AS t1", dialect="mysql", compare_names=False
    )
    assert not near.proven


SIMPLIFY_SCHEMA = {"a": ["id", "k", "v"], "b": ["id", "k", "w"]}
SIMPLIFY_PAIRS = [
    ("SELECT 1 FROM a FULL JOIN b ON a.k = b.k WHERE b.w > 3", "SELECT 1 FROM a RIGHT JOIN (SELECT * FROM b WHERE w > 3) AS t ON a.k = t.k"),
    ("SELECT 1 FROM a FULL JOIN b ON a.k = b.k WHERE a.v = 2", "SELECT 1 FROM (SELECT * FROM a WHERE v = 2) AS t LEFT JOIN b ON t.k = b.k"),
    ("SELECT a.id FROM a LEFT JOIN (SELECT k FROM b WHERE w > 1 GROUP BY k) AS d ON a.k = d.k", "SELECT a.id FROM a"),
]


@pytest.mark.parametrize("left,right", SIMPLIFY_PAIRS)
def test_full_join_filters_and_unused_grouped_joins(left, right):
    assert prove_equivalent_algebraic(left, right, schema=SIMPLIFY_SCHEMA, dialect="mysql", compare_names=False).proven


def test_outer_join_simplifications_keep_their_boundaries():
    # an unused left join to a table that is not unique on the join column repeats rows of a
    assert not prove_equivalent_algebraic(
        "SELECT a.id FROM a LEFT JOIN b ON a.k = b.k", "SELECT a.id FROM a", schema=SIMPLIFY_SCHEMA, dialect="mysql", compare_names=False
    ).proven
    # a null-tolerant test leaves the unmatched rows of both sides
    assert not prove_equivalent_algebraic(
        "SELECT 1 FROM a FULL JOIN b ON a.k = b.k WHERE b.w IS NULL", "SELECT 1 FROM a LEFT JOIN b ON a.k = b.k WHERE b.w IS NULL",
        schema=SIMPLIFY_SCHEMA, dialect="mysql", compare_names=False,
    ).proven


def test_outer_join_simplifications_preserve_results_on_random_databases():
    rng = random.Random(33)
    forms = [pair[0] for pair in SIMPLIFY_PAIRS] + ["SELECT a.id FROM a LEFT JOIN b ON a.k = b.k WHERE b.w IS NULL"]
    normalized = [normalize(f, schema=SIMPLIFY_SCHEMA, dialect="mysql") for f in forms]
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE a (id INT, k INT, v INT)")
        db.execute("CREATE TABLE b (id INT, k INT, w INT)")
        for table in ("a", "b"):
            for n in range(rng.choice([0, 1, 3, 5])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", [n, rng.choice([None, 0, 1, 2]), rng.choice([None, 0, 2, 4])])
        for sql, norm in zip(forms, normalized):
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            try:
                expected = Counter(db.execute(plain).fetchall())
            except sqlite3.OperationalError:
                continue  # old SQLite without FULL/RIGHT JOIN
            assert expected == Counter(db.execute(runnable).fetchall()), norm


def test_nested_set_operations_keep_their_shape_in_the_normalized_text():
    # a bare "a INTERSECT b UNION ALL c" would read back as "(a INTERSECT b) UNION ALL c"
    schema = {"t": ["a"], "u": ["a"], "v": ["a"]}
    text = normalize(
        "SELECT a FROM t INTERSECT SELECT a FROM (SELECT a FROM u UNION ALL SELECT a FROM v) AS d WHERE a > 1", schema=schema, dialect="mysql"
    )
    tree = sqlglot.parse_one(text, read="mysql")
    assert isinstance(tree, sqlglot.exp.Intersect)
    assert isinstance(tree.expression, sqlglot.exp.Subquery) and isinstance(tree.expression.this, sqlglot.exp.Union)


DERIVED_SCHEMA = {"t": ["id", "k", "v"], "u": ["id", "k", "v"]}
DERIVED_PROVEN = {
    "filter into distinct": (
        "SELECT * FROM (SELECT DISTINCT k FROM t) AS d WHERE d.k > 1",
        "SELECT DISTINCT k FROM t WHERE k > 1",
    ),
    "filter on a group key": (
        "SELECT * FROM (SELECT k, MIN(v) FROM t GROUP BY k) AS d WHERE d.k > 1",
        "SELECT k, MIN(v) FROM t WHERE k > 1 GROUP BY k",
    ),
    "distinct is redundant under count distinct": (
        "SELECT COUNT(DISTINCT k) FROM (SELECT k FROM t GROUP BY k) AS d",
        "SELECT COUNT(DISTINCT k) FROM (SELECT k FROM t) AS d",
    ),
    "distinct is redundant under max": (
        "SELECT MAX(k) FROM (SELECT DISTINCT k FROM t) AS d",
        "SELECT MAX(k) FROM t",
    ),
    "except of two filters": (
        "SELECT * FROM t WHERE id > 0 EXCEPT SELECT * FROM t WHERE id < 10",
        "SELECT DISTINCT * FROM t WHERE id > 0 AND NOT COALESCE(id < 10, FALSE)",
    ),
    "exists as a limit one probe": (
        "SELECT * FROM t WHERE EXISTS (SELECT k FROM u WHERE v > 1)",
        "SELECT * FROM t WHERE (SELECT 1 FROM (SELECT k FROM u WHERE v > 1) AS x LIMIT 1) IS NOT NULL",
    ),
    "nth value one is first value": (
        "SELECT FIRST_VALUE(v) OVER (PARTITION BY k ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t",
        "SELECT NTH_VALUE(v, 1) OVER (PARTITION BY k ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t",
    ),
    "unnamed derived column under a star": (
        "SELECT * FROM (SELECT k, MIN(v) FROM t GROUP BY k) AS d WHERE d.k > 1",
        "SELECT k, MIN(v) FROM t WHERE k > 1 GROUP BY k",
    ),
    "inner lateral is a join": (
        "SELECT * FROM t INNER JOIN LATERAL (SELECT * FROM u WHERE t.k = u.k) AS x",
        "SELECT * FROM t INNER JOIN u ON t.k = u.k",
    ),
    "left lateral aggregate is a scalar subquery": (
        "SELECT t.id, (SELECT MAX(u.v) FROM u WHERE t.k = u.k) FROM t",
        "SELECT t.id, s.m FROM t LEFT JOIN LATERAL (SELECT MAX(u.v) AS m FROM u WHERE t.k = u.k) AS s",
    ),
}
DERIVED_NOT_PROVEN = {
    "filter on an aggregate is not a group key": (
        "SELECT * FROM (SELECT k, MIN(v) AS m FROM t GROUP BY k) AS d WHERE d.m > 1",
        "SELECT k, MIN(v) FROM t WHERE v > 1 GROUP BY k",
    ),
    "distinct matters to count": (
        "SELECT COUNT(k) FROM (SELECT DISTINCT k FROM t) AS d",
        "SELECT COUNT(k) FROM t",
    ),
    "distinct matters to sum": (
        "SELECT SUM(k) FROM (SELECT k FROM t GROUP BY k) AS d",
        "SELECT SUM(k) FROM t",
    ),
    "except with a subset of the columns": (
        "SELECT k FROM t WHERE id > 0 EXCEPT SELECT k FROM t WHERE id < 10",
        "SELECT DISTINCT k FROM t WHERE id > 0 AND NOT COALESCE(id < 10, FALSE)",
    ),
    "nth value two is not first value": (
        "SELECT FIRST_VALUE(v) OVER (PARTITION BY k ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t",
        "SELECT NTH_VALUE(v, 2) OVER (PARTITION BY k ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t",
    ),
    "lateral with a group by repeats rows": (
        "SELECT t.id, s.m FROM t INNER JOIN LATERAL (SELECT MAX(u.v) AS m FROM u WHERE t.k = u.k GROUP BY u.id) AS s",
        "SELECT t.id, (SELECT MAX(u.v) FROM u WHERE t.k = u.k) FROM t",
    ),
}


@pytest.mark.parametrize("name", DERIVED_PROVEN)
def test_derived_table_rules_prove(name):
    left, right = DERIVED_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=DERIVED_SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True)
    assert result.proven, result.reason


@pytest.mark.parametrize("name", DERIVED_NOT_PROVEN)
def test_derived_table_rules_keep_their_boundaries(name):
    left, right = DERIVED_NOT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=DERIVED_SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True)
    assert not result.proven


def test_derived_table_rewrites_preserve_results_on_random_databases():
    rng = random.Random(77)
    forms = [pair[0] for pair in DERIVED_PROVEN.values() if "LATERAL" not in pair[0]] + [
        pair[0] for pair in DERIVED_NOT_PROVEN.values() if "LATERAL" not in pair[0]
    ]
    normalized = [normalize(f, schema=DERIVED_SCHEMA, dialect="mysql") for f in forms]
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        for table in ("t", "u"):
            db.execute(f"CREATE TABLE {table} (id INT, k INT, v INT)")
            for n in range(rng.choice([0, 1, 4, 6])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", [rng.choice([None, 1, 2, 3, 4]), rng.choice([None, 0, 1, 2]), rng.choice([None, 0, 2, 5])])
        for sql, norm in zip(forms, normalized):
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            try:
                expected = Counter(db.execute(plain).fetchall())
            except sqlite3.OperationalError:
                continue
            assert expected == Counter(db.execute(runnable).fetchall()), (sql, norm)


GROUPED_JOIN_SCHEMA = {"e": ["id", "k", "v"], "d": ["k", "name"]}
GROUPED_JOIN_KEYS = {"e": TableConstraints(keys=(("id",),)), "d": TableConstraints(keys=(("k",),))}
GROUPED_JOIN_PROVEN = [
    (
        "SELECT e.k FROM e INNER JOIN d ON e.k = d.k GROUP BY e.k",
        "SELECT t.k FROM (SELECT k FROM e GROUP BY k) AS t INNER JOIN d ON t.k = d.k",
    ),
    (
        "SELECT e.k, d.k AS k2 FROM e INNER JOIN d ON e.k = d.k GROUP BY e.k, d.k",
        "SELECT t.k, d.k AS k2 FROM (SELECT k FROM e GROUP BY k) AS t INNER JOIN d ON t.k = d.k",
    ),
    (
        "SELECT t.v, d.name FROM (SELECT * FROM e WHERE e.id = 10) AS t INNER JOIN d ON t.v = d.name GROUP BY t.v, d.name",
        "SELECT a.v, b.name FROM (SELECT v FROM e WHERE id = 10 GROUP BY v) AS a INNER JOIN (SELECT name FROM d GROUP BY name) AS b ON a.v = b.name",
    ),
]


@pytest.mark.parametrize("left,right", GROUPED_JOIN_PROVEN)
def test_distinct_pushed_into_join_sources(left, right):
    result = prove_equivalent_algebraic(left, right, schema=GROUPED_JOIN_SCHEMA, constraints=GROUPED_JOIN_KEYS, dialect="mysql", compare_names=False)
    assert result.proven, result.reason


def test_distinct_pushed_into_join_sources_needs_a_key():
    # d.k is not a key here, so the pushed-down form repeats a row of e once per matching row of d
    result = prove_equivalent_algebraic(
        GROUPED_JOIN_PROVEN[0][0],
        GROUPED_JOIN_PROVEN[0][1],
        schema=GROUPED_JOIN_SCHEMA,
        constraints={"e": TableConstraints(keys=(("id",),))},
        dialect="mysql",
        compare_names=False,
    )
    assert not result.proven


def test_distinct_pushed_into_join_sources_preserves_results_on_random_databases():
    rng = random.Random(55)
    forms = [pair[0] for pair in GROUPED_JOIN_PROVEN]
    normalized = [normalize(f, schema=GROUPED_JOIN_SCHEMA, dialect="mysql", keys={"e": [("id",)], "d": [("k",)]}) for f in forms]
    for _ in range(80):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE e (id INT, k INT, v INT)")
        db.execute("CREATE TABLE d (k INT, name INT)")
        for n in range(rng.choice([0, 2, 5, 8])):
            db.execute("INSERT INTO e VALUES (?, ?, ?)", [rng.choice([10, 10, n]), rng.choice([None, 0, 1, 2]), rng.choice([None, 1, 2])])
        for n in range(rng.choice([0, 2, 4])):
            db.execute("INSERT INTO d VALUES (?, ?)", [n, rng.choice([None, 1, 2])])
        for sql, norm in zip(forms, normalized):
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            assert Counter(db.execute(plain).fetchall()) == Counter(db.execute(runnable).fetchall()), (sql, norm)


IN_LIST_SCHEMA = {"p": ["id", "k", "a"], "q": ["id", "k", "b"]}
IN_LIST_NOT_NULL = {"p": TableConstraints(not_null=frozenset({"id", "k"})), "q": TableConstraints(not_null=frozenset({"id", "k"}))}


def test_select_list_in_is_exists_when_both_sides_are_not_null():
    result = prove_equivalent_algebraic(
        "SELECT p.id, p.k IN (SELECT q.k FROM q WHERE q.id < 20) AS hit FROM p",
        "SELECT p.id, CASE WHEN EXISTS (SELECT 1 FROM q WHERE q.id < 20 AND q.k = p.k) THEN TRUE ELSE FALSE END AS hit FROM p",
        schema=IN_LIST_SCHEMA,
        constraints=IN_LIST_NOT_NULL,
        dialect="mysql",
        compare_names=False,
    )
    assert result.proven, result.reason


def test_select_list_in_keeps_unknown_when_a_side_may_be_null():
    # p.a may be NULL: IN is then UNKNOWN, not FALSE, so it is not the EXISTS
    result = prove_equivalent_algebraic(
        "SELECT p.id, p.a IN (SELECT q.k FROM q) AS hit FROM p",
        "SELECT p.id, CASE WHEN EXISTS (SELECT 1 FROM q WHERE q.k = p.a) THEN TRUE ELSE FALSE END AS hit FROM p",
        schema=IN_LIST_SCHEMA,
        constraints=IN_LIST_NOT_NULL,
        dialect="mysql",
        compare_names=False,
    )
    assert not result.proven


def test_left_join_to_a_constant_set_is_an_exists_flag():
    result = prove_equivalent_algebraic(
        "SELECT p.id, EXISTS (SELECT * FROM q WHERE q.id < 20) AS hit FROM p",
        "SELECT p.id, CASE WHEN d.i IS NOT NULL THEN TRUE ELSE FALSE END AS hit FROM p LEFT JOIN (SELECT DISTINCT 1 AS i FROM q WHERE q.id < 20) AS d ON TRUE",
        schema=IN_LIST_SCHEMA,
        dialect="mysql",
        compare_names=False,
    )
    assert result.proven, result.reason


def test_select_list_tuple_in_is_exists_when_all_columns_are_not_null():
    result = prove_equivalent_algebraic(
        "SELECT p.id, (p.id, p.k) IN (SELECT q.id, q.k FROM q WHERE q.id < 20) AS hit FROM p",
        "SELECT p.id, CASE WHEN t.i IS NOT NULL THEN TRUE ELSE FALSE END AS hit FROM p LEFT JOIN (SELECT q.id, q.k, 1 AS i FROM q WHERE q.id < 20) AS t ON p.id = t.id AND p.k = t.k",
        schema=IN_LIST_SCHEMA,
        constraints={
            "p": TableConstraints(not_null=frozenset({"id", "k"}), keys=(("id",),)),
            "q": TableConstraints(not_null=frozenset({"id", "k"}), keys=(("id",),)),
        },
        dialect="mysql",
        compare_names=False,
    )
    assert result.proven, result.reason


GROUPING_SCHEMA = {"t": ["a", "b", "v"]}
GROUPING_PROVEN = {
    "sets are a union all": (
        "SELECT a, b, SUM(v) FROM t GROUP BY GROUPING SETS ((a), (b))",
        "SELECT a, NULL, SUM(v) FROM t GROUP BY a UNION ALL SELECT NULL, b, SUM(v) FROM t GROUP BY b",
    ),
    "grouping is a bit mask": (
        "SELECT a, b, GROUPING(a, b) AS g, COUNT(*) FROM t GROUP BY GROUPING SETS ((a, b), (a), ())",
        "SELECT a, b, 0, COUNT(*) FROM t GROUP BY a, b UNION ALL SELECT a, NULL, 1, COUNT(*) FROM t GROUP BY a UNION ALL SELECT NULL, NULL, 3, COUNT(*) FROM t",
    ),
}
GROUPING_NOT_PROVEN = {
    "grouping flag has the other value": (
        "SELECT a, GROUPING(a, b) AS g, COUNT(*) FROM t GROUP BY GROUPING SETS ((a), (b))",
        "SELECT a, 0, COUNT(*) FROM t GROUP BY a UNION ALL SELECT NULL, 2, COUNT(*) FROM t GROUP BY b",
    ),
    "an empty set always returns its row": (
        "SELECT COUNT(*) FROM t GROUP BY GROUPING SETS (())",
        "SELECT COUNT(*) FROM t GROUP BY a",
    ),
}


@pytest.mark.parametrize("name", GROUPING_PROVEN)
def test_grouping_sets_are_a_union_of_grouped_selects(name):
    left, right = GROUPING_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=GROUPING_SCHEMA, dialect="bigquery", compare_names=False, exact_arithmetic=True)
    assert result.proven, result.reason


@pytest.mark.parametrize("name", GROUPING_NOT_PROVEN)
def test_grouping_sets_keep_their_boundaries(name):
    left, right = GROUPING_NOT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=GROUPING_SCHEMA, dialect="bigquery", compare_names=False, exact_arithmetic=True)
    assert not result.proven


CONSTANT_SCHEMA = {"t": ["a", "b"]}
CONSTANT_PROVEN = {
    "constant derived column": (
        "SELECT d.a FROM (SELECT a, TRUE AS keep FROM t) AS d WHERE d.keep",
        "SELECT a FROM t",
    ),
    "case on a constant flag": (
        "SELECT a, CASE WHEN 1 = 1 THEN b END FROM t",
        "SELECT a, b FROM t",
    ),
    "count of nothing in an aggregated select": (
        "SELECT COUNT(a), COUNT(NULL) FROM t",
        "SELECT COUNT(a), 0 FROM t",
    ),
    "constant source": (
        "SELECT c.x + t.a FROM (SELECT 5 AS x) AS c, t",
        "SELECT 5 + a FROM t",
    ),
    "intersect with an empty side": (
        "SELECT a FROM t INTERSECT SELECT a FROM t WHERE 1 = 2",
        "SELECT a FROM t WHERE 1 = 2",
    ),
}
CONSTANT_NOT_PROVEN = {
    "count of nothing alone keeps its row": (
        "SELECT COUNT(NULL) FROM t",
        "SELECT 0 FROM t",
    ),
    "constant flag that is false": (
        "SELECT d.a FROM (SELECT a, FALSE AS keep FROM t) AS d WHERE d.keep",
        "SELECT a FROM t",
    ),
}


@pytest.mark.parametrize("name", CONSTANT_PROVEN)
def test_constants_fold_through_derived_tables_and_set_operations(name):
    left, right = CONSTANT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=CONSTANT_SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True)
    assert result.proven, result.reason


@pytest.mark.parametrize("name", CONSTANT_NOT_PROVEN)
def test_constant_folding_keeps_its_boundaries(name):
    left, right = CONSTANT_NOT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=CONSTANT_SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True)
    assert not result.proven


def test_constant_folding_rewrites_preserve_results_on_random_databases():
    rng = random.Random(91)
    forms = [pair[0] for pair in CONSTANT_PROVEN.values()] + [pair[0] for pair in CONSTANT_NOT_PROVEN.values()]
    normalized = [normalize(f, schema=CONSTANT_SCHEMA, dialect="mysql") for f in forms]
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (a INT, b INT)")
        for _ in range(rng.choice([0, 1, 4])):
            db.execute("INSERT INTO t VALUES (?, ?)", [rng.choice([None, 1, 2]), rng.choice([None, 0, 5])])
        for sql, norm in zip(forms, normalized):
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            assert Counter(db.execute(plain).fetchall()) == Counter(db.execute(runnable).fetchall()), (sql, norm)


def test_constant_column_of_a_null_extended_derived_table_is_not_inlined():
    # d.i is NULL for a p row with no match, so "d.i IS NULL" is not FALSE
    result = prove_equivalent_algebraic(
        "SELECT p.id FROM p LEFT JOIN (SELECT k, 1 AS i FROM q) AS d ON p.k = d.k WHERE d.i IS NULL",
        "SELECT p.id FROM p WHERE 1 = 2",
        schema=IN_LIST_SCHEMA,
        dialect="mysql",
        compare_names=False,
    )
    assert not result.proven


BOOLEAN_SCHEMA = {"t": ["a", "b"]}
BOOLEAN_PROVEN = {
    "is null of a null test": ("SELECT * FROM t WHERE (a IS NULL) IS NULL", "SELECT * FROM t WHERE 1 = 2"),
    "is not null of a null test": ("SELECT * FROM t WHERE ((a IS NULL) IS NOT NULL) OR NULL", "SELECT * FROM t"),
    "false and unknown": ("SELECT * FROM t WHERE FALSE AND NULL", "SELECT * FROM t WHERE 1 = 2"),
    "cast of a null test above one": ("SELECT * FROM t WHERE CAST((a IS NULL) AS SIGNED) > 1000", "SELECT * FROM t WHERE 1 = 2"),
    "cast of a null test below": ("SELECT * FROM t WHERE CAST((a IS NULL) AS SIGNED) < 1000", "SELECT * FROM t"),
    "group by in an in test": (
        "SELECT * FROM t WHERE (a, b) IN (SELECT a, b FROM t GROUP BY a, b) OR a < 40 + 60",
        "SELECT * FROM t WHERE (a, b) IN (SELECT a, b FROM t) OR a < 100",
    ),
    "group by in an exists test": (
        "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM t AS u GROUP BY u.a)",
        "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM t AS u)",
    ),
}
BOOLEAN_NOT_PROVEN = {
    "cast of a null test inside the range": (
        "SELECT * FROM t WHERE CAST((a IS NULL) AS SIGNED) > 0",
        "SELECT * FROM t WHERE 1 = 2",
    ),
    "true and unknown is unknown": ("SELECT * FROM t WHERE TRUE AND NULL", "SELECT * FROM t"),
    "global count under exists keeps its row": (
        "SELECT * FROM t WHERE EXISTS (SELECT COUNT(*) FROM t AS u WHERE u.a > 100)",
        "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM t AS u WHERE u.a > 100)",
    ),
}


@pytest.mark.parametrize("name", BOOLEAN_PROVEN)
def test_boolean_constant_folds_and_membership_group_by(name):
    left, right = BOOLEAN_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=BOOLEAN_SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True)
    assert result.proven, result.reason


@pytest.mark.parametrize("name", BOOLEAN_NOT_PROVEN)
def test_boolean_constant_folds_keep_their_boundaries(name):
    left, right = BOOLEAN_NOT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=BOOLEAN_SCHEMA, dialect="mysql", compare_names=False, exact_arithmetic=True)
    assert not result.proven


def test_boolean_folds_preserve_results_on_random_databases():
    rng = random.Random(5)
    forms = [pair[0] for pair in BOOLEAN_PROVEN.values()] + [pair[0] for pair in BOOLEAN_NOT_PROVEN.values()]
    normalized = [normalize(f, schema=BOOLEAN_SCHEMA, dialect="mysql") for f in forms]
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE t (a INT, b INT)")
        for _ in range(rng.choice([0, 1, 4])):
            db.execute("INSERT INTO t VALUES (?, ?)", [rng.choice([None, 1, 2, 150]), rng.choice([None, 0, 5])])
        for sql, norm in zip(forms, normalized):
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            try:
                expected = Counter(db.execute(plain).fetchall())
            except sqlite3.OperationalError:
                continue  # SQLite lacks row-value IN over a subquery in old versions
            assert expected == Counter(db.execute(runnable).fetchall()), (sql, norm)


EMPTY_SIDE_SCHEMA = {"p": ["id", "k"], "q": ["id", "k"]}
EMPTY_SIDE_PROVEN = {
    "right join to an empty left": (
        "SELECT e.id, e.k, q.id, q.k FROM (SELECT * FROM p WHERE FALSE) AS e RIGHT JOIN q ON e.k = q.k",
        "SELECT CAST(NULL AS SIGNED), CAST(NULL AS SIGNED), q.id, q.k FROM q",
    ),
    "left join to an empty right": (
        "SELECT p.id, e.id FROM p LEFT JOIN (SELECT * FROM q WHERE 1 = 2) AS e ON p.k = e.k",
        "SELECT p.id, NULL FROM p",
    ),
}
EMPTY_SIDE_NOT_PROVEN = {
    "inner join to an empty side has no rows": (
        "SELECT p.id FROM p JOIN (SELECT * FROM q WHERE FALSE) AS e ON p.k = e.k",
        "SELECT p.id FROM p",
    ),
    "empty preserved side of a left join": (
        "SELECT e.id FROM (SELECT * FROM p WHERE FALSE) AS e LEFT JOIN q ON e.k = q.k",
        "SELECT p.id FROM p",
    ),
}


@pytest.mark.parametrize("name", EMPTY_SIDE_PROVEN)
def test_empty_null_extended_side_of_an_outer_join(name):
    left, right = EMPTY_SIDE_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=EMPTY_SIDE_SCHEMA, dialect="mysql", compare_names=False)
    assert result.proven, result.reason


@pytest.mark.parametrize("name", EMPTY_SIDE_NOT_PROVEN)
def test_empty_side_rules_keep_their_boundaries(name):
    left, right = EMPTY_SIDE_NOT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=EMPTY_SIDE_SCHEMA, dialect="mysql", compare_names=False)
    assert not result.proven


def test_empty_side_rules_preserve_results_on_random_databases():
    rng = random.Random(8)
    forms = [pair[0] for pair in EMPTY_SIDE_PROVEN.values()] + [pair[0] for pair in EMPTY_SIDE_NOT_PROVEN.values()]
    normalized = [normalize(f, schema=EMPTY_SIDE_SCHEMA, dialect="mysql") for f in forms]
    for _ in range(40):
        db = sqlite3.connect(":memory:")
        for table in ("p", "q"):
            db.execute(f"CREATE TABLE {table} (id INT, k INT)")
            for n in range(rng.choice([0, 1, 4])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?)", [n, rng.choice([None, 0, 1])])
        for sql, norm in zip(forms, normalized):
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            try:
                expected = Counter(db.execute(plain).fetchall())
            except sqlite3.OperationalError:
                continue  # old SQLite without RIGHT JOIN
            assert expected == Counter(db.execute(runnable).fetchall()), (sql, norm)


SEMI_SCHEMA2 = {"p": ["id", "k", "a"], "q": ["id", "k", "b"]}
SEMI_KEYS2 = {"p": TableConstraints(keys=(("id",),)), "q": TableConstraints(keys=(("id",),))}
SEMI2_PROVEN = {
    "exists witnessed by a join": (
        "SELECT p.id FROM p JOIN q ON p.k = q.k WHERE EXISTS (SELECT 1 FROM q AS w WHERE w.k = p.k)",
        "SELECT p.id FROM p JOIN q ON p.k = q.k",
    ),
    "inner join to a grouped set is an exists": (
        "SELECT p.id FROM p JOIN (SELECT k FROM q GROUP BY k) AS g ON p.k = g.k",
        "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM q WHERE q.k = p.k)",
    ),
    "left join indicator is null is not exists": (
        "SELECT p.id FROM p LEFT JOIN (SELECT 1 AS i, k FROM q WHERE b < 3 GROUP BY k) AS g ON p.k = g.k WHERE g.i IS NULL",
        "SELECT p.id FROM p WHERE NOT EXISTS (SELECT 1 FROM q WHERE b < 3 AND q.k = p.k)",
    ),
}
SEMI2_NOT_PROVEN = {
    "join to a plain table repeats rows": (
        "SELECT p.id FROM p JOIN q ON p.k = q.k",
        "SELECT p.id FROM p WHERE EXISTS (SELECT 1 FROM q WHERE q.k = p.k)",
    ),
    "exists with another condition is not witnessed": (
        "SELECT p.id FROM p JOIN q ON p.k = q.k WHERE EXISTS (SELECT 1 FROM q AS w WHERE w.k = p.k AND w.b > 5)",
        "SELECT p.id FROM p JOIN q ON p.k = q.k",
    ),
    "select star lists the joined set": (
        "SELECT * FROM p JOIN (SELECT k FROM q GROUP BY k) AS g ON p.k = g.k",
        "SELECT p.id, p.k, p.a FROM p WHERE EXISTS (SELECT 1 FROM q WHERE q.k = p.k)",
    ),
}


@pytest.mark.parametrize("name", SEMI2_PROVEN)
def test_semi_join_shapes_prove(name):
    left, right = SEMI2_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=SEMI_SCHEMA2, constraints=SEMI_KEYS2, dialect="mysql", compare_names=False)
    assert result.proven, result.reason


@pytest.mark.parametrize("name", SEMI2_NOT_PROVEN)
def test_semi_join_shapes_keep_their_boundaries(name):
    left, right = SEMI2_NOT_PROVEN[name]
    result = prove_equivalent_algebraic(left, right, schema=SEMI_SCHEMA2, constraints=SEMI_KEYS2, dialect="mysql", compare_names=False)
    assert not result.proven


def test_semi_join_shapes_preserve_results_on_random_databases():
    rng = random.Random(12)
    forms = [pair[0] for pair in SEMI2_PROVEN.values()] + [pair[0] for pair in SEMI2_NOT_PROVEN.values()]
    normalized = [normalize(f, schema=SEMI_SCHEMA2, dialect="mysql", keys={"p": [("id",)], "q": [("id",)]}) for f in forms]
    for _ in range(60):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE p (id INT, k INT, a INT)")
        db.execute("CREATE TABLE q (id INT, k INT, b INT)")
        for table in ("p", "q"):
            for n in range(rng.choice([0, 1, 4, 6])):
                db.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", [n, rng.choice([None, 0, 1, 2]), rng.choice([None, 0, 2, 7])])
        for sql, norm in zip(forms, normalized):
            plain = sqlglot.transpile(sql, read="mysql", write="sqlite")[0]
            runnable = sqlglot.transpile(norm, read="mysql", write="sqlite")[0]
            assert Counter(db.execute(plain).fetchall()) == Counter(db.execute(runnable).fetchall()), (sql, norm)


def test_single_row_source_grouping_is_dropped_and_near_misses_kept():
    from kumosql.algebraic_equivalence import normalize

    one = "SELECT k FROM (SELECT a AS k FROM t ORDER BY a LIMIT 1) AS d GROUP BY k"
    assert "GROUP BY" not in normalize(one).upper()
    two = "SELECT k FROM (SELECT a AS k FROM t ORDER BY a LIMIT 2) AS d GROUP BY k"
    assert "GROUP BY" in normalize(two).upper() or "DISTINCT" in normalize(two).upper()
    glob = "SELECT COUNT(*) FROM (SELECT a AS k FROM t ORDER BY a LIMIT 1) AS d GROUP BY k"
    assert "GROUP BY" in normalize(glob).upper()


def test_printed_normal_form_keeps_or_inside_and_parenthesized():
    from kumosql.algebraic_equivalence import _parenthesize_boolean
    from sqlglot import exp, parse_one

    tree = exp.And(this=parse_one("a >= 1"), expression=parse_one("b = 2 OR a < 2"))
    assert "(b = 2 OR a < 2)" in _parenthesize_boolean(tree).sql()
    negated = exp.Not(this=parse_one("a = 1 OR b = 2"))
    assert parse_one(_parenthesize_boolean(negated).sql()) == parse_one("NOT (a = 1 OR b = 2)")


UNMODELED = [
    ("bigquery", "SELECT a FROM t FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()", "SELECT a FROM t"),
    ("mysql", "SELECT a FROM t ORDER BY a FETCH FIRST 2 ROWS WITH TIES", "SELECT a FROM t ORDER BY a FETCH FIRST 2 ROWS ONLY"),
    ("mysql", "SELECT a FROM t TABLESAMPLE (10 PERCENT)", "SELECT a FROM t"),
]


@pytest.mark.parametrize("dialect,left,right", UNMODELED)
def test_constructs_the_provers_do_not_model_are_declined(dialect, left, right):
    from kumosql.smt_equivalence import prove_equivalent_smt

    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, schema={"t": ["a", "b"]}, dialect=dialect).proven
    assert prove_equivalent_algebraic(right, right, schema={"t": ["a", "b"]}, dialect=dialect).proven


def test_check_modeled_names_the_flag():
    import sqlglot
    from kumosql.ast_utils import UnmodeledConstruct, check_modeled

    with pytest.raises(UnmodeledConstruct):
        check_modeled(sqlglot.parse_one("SELECT a FROM t FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()", read="bigquery"))
    plain = sqlglot.parse_one("SELECT a FROM t WHERE a IS NOT NULL", read="mysql")
    assert check_modeled(plain) is plain


def test_level_retries_reuse_normalization_without_losing_assumptions(monkeypatch):
    import kumosql.algebraic_equivalence as ae
    from kumosql.smt_equivalence import SmtEquivalenceResult, SmtStatus

    from kumosql import shape_fallback

    calls = []
    def normalize(sql, **kwargs):
        # a pair that ends without a verdict is tried again with the shape rules off (shape_fallback), which
        # needs its own normal forms: the six normalizations are made once with the rules on and once with them off
        calls.append((sql, kwargs["keyed_distinct"], shape_fallback.enabled()))
        kwargs["_assumptions"].add("normalization evidence")
        return sql
    monkeypatch.setattr(ae, "normalize", normalize)
    monkeypatch.setattr(ae, "prove_equivalent_smt", lambda *a, **k:
                        SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "declined"))
    from kumosql.smt_equivalence import TableConstraints
    result = ae._prove_algebraic_levels("SELECT 1", "SELECT 2", False,
                                       constraints={"t": TableConstraints()})
    assert result.status is SmtStatus.NOT_PROVEN
    assert "normalization evidence" in result.assumptions
    assert len(calls) == 12
    assert len(set(calls)) == 12  # never the same normalization twice in one state
    assert sum(1 for *_, shapes in calls if shapes) == 6


def test_normalization_reuse_is_isolated_between_constraint_contexts():
    from kumosql.smt_equivalence import TableConstraints
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    pair = ("SELECT DISTINCT id FROM t", "SELECT id FROM t")
    options = dict(schema={"t": ["id"]}, types={"t": {"id": "INT"}})
    keyed = {"t": TableConstraints(keys=[("id",)], not_null=frozenset({"id"}))}
    assert prove_equivalent_algebraic(*pair, constraints=keyed, **options).proven
    assert not prove_equivalent_algebraic(*pair, **options).proven
    assert prove_equivalent_algebraic(*pair, constraints=keyed, **options).proven
