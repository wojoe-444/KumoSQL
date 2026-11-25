"""Wrong proofs found on master, kept as regression cases.

Each pair here returns different rows (DuckDB shows it on the database next to it), so neither prover
may call it equivalent. Near misses that are equivalent stay proven, so a fix cannot just decline more.
"""

from collections import Counter

import pytest
import sqlglot

pytest.importorskip("z3")

from sqlglot_support import skip_if_unparseable

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.ast_utils import LossySql, expand_alias_columns, faithful_sql
from kumosql.smt_equivalence import TableConstraints, prove_equivalent_smt

CALCITE = {"emp": ["empno", "ename", "job", "sal", "deptno"], "dept": ["deptno", "name"]}
CALCITE_KEYS = {
    "emp": TableConstraints(not_null=frozenset({"empno", "ename", "job", "sal", "deptno"}), keys=(("empno",),)),
    "dept": TableConstraints(not_null=frozenset({"deptno", "name"}), keys=(("deptno",),)),
}
CALCITE_DDL = {
    "emp": "CREATE TABLE emp (empno BIGINT, ename VARCHAR, job VARCHAR, sal BIGINT, deptno BIGINT)",
    "dept": "CREATE TABLE dept (deptno BIGINT, name VARCHAR)",
}


def _bags_differ(left: str, right: str, ddl: dict[str, str], rows: dict[str, list[tuple]]) -> bool:
    duckdb = pytest.importorskip("duckdb")
    db = duckdb.connect()
    for name, create in ddl.items():
        db.execute(create)
        for row in rows.get(name, []):
            db.execute(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in row)})", row)
    run = lambda sql: Counter(db.execute(sqlglot.transpile(sql, read="mysql", write="duckdb")[0]).fetchall())  # noqa: E731
    return run(left) != run(right)


# (id, left, right, schema, constraints, DDL, rows on which they differ)
WRONG_PROOFS = [
    pytest.param(
        "SELECT x.SAL2 FROM (SELECT t.SAL2 FROM (SELECT SAL AS SAL2 FROM EMP) t) x",
        "SELECT SAL2 FROM EMP",
        {"emp": ["empno", "sal", "sal2"]},
        None,
        {"emp": "CREATE TABLE emp (empno BIGINT, sal BIGINT, sal2 BIGINT)"},
        {"emp": [(1, 10, 20)]},
        id="inlined-projection-keeps-the-renamed-output",
    ),
    pytest.param(
        "SELECT t5.c5 FROM (SELECT t2.sal c5, t3.c1 c10 FROM EMP t2 LEFT JOIN (SELECT d.deptno c0, TRUE c1, d.name c2 FROM DEPT d) t3"
        " ON t2.empno = t3.c0 AND t2.job = t3.c2) t5 WHERE t5.c10 IS NULL",
        "SELECT sal FROM emp WHERE 1 = 0",
        CALCITE,
        CALCITE_KEYS,
        CALCITE_DDL,
        {"emp": [(1, "a", "b", 10, 1)]},
        id="constant-from-the-null-side-of-a-left-join",
    ),
    pytest.param(
        "SELECT name FROM dept AS d(name, x)",
        "SELECT name FROM dept",
        CALCITE,
        None,
        CALCITE_DDL,
        {"dept": [(1, "a")]},
        id="table-alias-column-list",
    ),
    pytest.param(
        "SELECT d.name FROM (SELECT deptno, name FROM dept) AS d(name, a)",
        "SELECT name FROM dept",
        CALCITE,
        None,
        CALCITE_DDL,
        {"dept": [(1, "a")]},
        id="derived-table-alias-column-list",
    ),
    pytest.param(
        "SELECT deptno DIV 2 FROM dept",
        "SELECT CAST(deptno / 2 AS SIGNED) FROM dept",
        CALCITE,
        None,
        CALCITE_DDL,
        {"dept": [(7, "a")]},
        id="mysql-div-is-not-a-rounding-cast",
    ),
    pytest.param(
        "SELECT d.deptno, d.name FROM (SELECT deptno, name, 2 AS c FROM dept) AS d ORDER BY d.c, d.deptno LIMIT 1",
        "SELECT deptno, name FROM dept ORDER BY name, deptno LIMIT 1",
        CALCITE,
        None,
        CALCITE_DDL,
        {"dept": [(1, "z"), (2, "a")]},
        id="inlined-constant-sort-key-is-not-a-column-position",
    ),
    pytest.param(
        "SELECT CAST(deptno AS BOOLEAN) FROM dept",
        "SELECT CAST(deptno AS SIGNED) FROM dept",
        CALCITE,
        None,
        CALCITE_DDL,
        {"dept": [(7, "a")]},
        id="mysql-boolean-cast-is-not-an-integer-cast",
    ),
    pytest.param(
        "SELECT empno, deptno IN (SELECT deptno FROM emp WHERE empno < 20) AS d FROM emp",
        "SELECT empno, EXISTS(SELECT 1 FROM emp WHERE empno < 20) AS d FROM emp",
        CALCITE,
        CALCITE_KEYS,
        CALCITE_DDL,
        {"emp": [(1, "a", "j", 1, 10), (30, "b", "j", 1, 20)]},
        id="in-to-exists-keeps-the-outer-column-outside",
    ),
    pytest.param(
        "SELECT e.empno, e.deptno IN (SELECT e.deptno FROM emp AS e WHERE e.empno < 20) AS d FROM emp AS e",
        "SELECT e.empno, EXISTS(SELECT 1 FROM emp AS e WHERE e.empno < 20) AS d FROM emp AS e",
        CALCITE,
        CALCITE_KEYS,
        CALCITE_DDL,
        {"emp": [(1, "a", "j", 1, 10), (30, "b", "j", 1, 20)]},
        id="in-to-exists-keeps-a-shadowed-alias-outside",
    ),
    pytest.param(
        "SELECT name FROM dept WHERE EXISTS (SELECT 1 FROM (SELECT 2 * deptno AS f FROM dept) AS t WHERE deptno = t.f)",
        "SELECT name FROM dept WHERE EXISTS (SELECT 1 FROM dept AS t WHERE t.deptno = 2 * t.deptno)",
        CALCITE,
        None,
        CALCITE_DDL,
        {"dept": [(2, "a"), (4, "b")]},
        id="merged-derived-table-keeps-the-correlated-column-outside",
    ),
]


def test_in_to_exists_still_proves_the_correlated_form():
    left = "SELECT empno, deptno IN (SELECT deptno FROM emp WHERE empno < 20) AS d FROM emp"
    right = "SELECT o.empno, EXISTS(SELECT 1 FROM emp AS i WHERE i.empno < 20 AND i.deptno = o.deptno) AS d FROM emp AS o"
    assert prove_equivalent_algebraic(left, right, schema=CALCITE, dialect="mysql", constraints=CALCITE_KEYS, compare_names=False).proven


@pytest.mark.parametrize("left,right,schema,constraints,ddl,rows", WRONG_PROOFS)
def test_pairs_that_differ_are_never_proven(left, right, schema, constraints, ddl, rows):
    assert _bags_differ(left, right, ddl, rows)
    kwargs = {"schema": schema, "dialect": "mysql", "compare_names": False}
    if constraints:
        kwargs["constraints"] = constraints
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, **kwargs).proven, prove.__name__



def _prints_div() -> bool:
    try:
        faithful_sql(sqlglot.parse_one("SELECT a DIV 2 FROM t", read="mysql"), "mysql")
    except LossySql:
        return False
    return True


# sqlglot 26 has no generator that writes MySQL's DIV back as DIV, so a query using it is refused there
PRINTS_DIV = _prints_div()
STILL_PROVEN = [
    pytest.param("SELECT x.k FROM (SELECT t.a AS k FROM (SELECT a FROM s) t) x", "SELECT a FROM s", id="renamed-column-passthrough"),
    pytest.param(
        "SELECT p.id, d.y FROM p LEFT JOIN (SELECT k, w + 1 AS y FROM q) AS d ON p.k = d.k",
        "SELECT p.id, d.w + 1 FROM p LEFT JOIN q AS d ON p.k = d.k",
        id="arithmetic-from-the-null-side-folds",
    ),
    pytest.param("SELECT x FROM s AS d(x, y)", "SELECT a FROM s", id="alias-column-list-renames-by-position"),
    pytest.param(
        "SELECT a DIV 2 FROM s", "SELECT a DIV 2 FROM s WHERE TRUE", id="div-matches-div",
        marks=pytest.mark.skipif(not PRINTS_DIV, reason="this sqlglot version cannot print DIV"),
    ),
    pytest.param(
        "SELECT p.id, d.y FROM p LEFT JOIN (SELECT k, CASE WHEN w < 11 THEN -1 * w ELSE w END AS y FROM q) AS d ON p.k = d.k",
        "SELECT p.id, CASE WHEN d.w < 11 THEN -1 * d.w ELSE d.w END FROM p LEFT JOIN q AS d ON p.k = d.k",
        id="case-whose-every-result-is-null-on-null-folds",
    ),
    pytest.param(
        "SELECT p.id, d.y FROM p LEFT JOIN (SELECT k, CASE WHEN w < 11 THEN 11 ELSE -w END AS y FROM q) AS d ON p.k = d.k",
        "SELECT p.id, CASE WHEN d.w < 11 THEN 11 ELSE -d.w END FROM p LEFT JOIN q AS d ON p.k = d.k",
        id="case-whose-constant-branch-needs-a-column-folds",
    ),
    pytest.param("SELECT a, b FROM s ORDER BY 2, 1 LIMIT 3", "SELECT a, b FROM s ORDER BY b, a LIMIT 3", id="column-positions"),
    pytest.param(
        "SELECT d.a, d.b FROM (SELECT a, b, 2 AS c FROM s) AS d ORDER BY d.c, d.a LIMIT 1",
        "SELECT a, b FROM s ORDER BY a LIMIT 1",
        id="constant-sort-key-orders-nothing",
    ),
]
STILL_SCHEMA = {"s": ["a", "b"], "p": ["id", "k"], "q": ["k", "w"]}


@pytest.mark.parametrize("left,right", STILL_PROVEN)
def test_equivalent_near_misses_stay_proven(left, right):
    assert prove_equivalent_algebraic(left, right, schema=STILL_SCHEMA, dialect="mysql", compare_names=False).proven


def test_constants_on_the_null_side_of_any_outer_join_are_not_folded():
    for join in ("LEFT JOIN", "FULL JOIN"):
        sql = f"SELECT p.id, d.i FROM p {join} (SELECT k, 1 AS i FROM q) AS d ON p.k = d.k"
        assert "1 AS i" in normalize(sql, schema=STILL_SCHEMA, dialect="mysql").replace("1 AS I", "1 AS i")
    for case in ("CASE WHEN w IS NULL THEN 0 ELSE w END", "CASE WHEN w < 11 THEN w ELSE 0 END", "IF(w < 11, w, 0)"):
        sql = f"SELECT p.id, d.i FROM p LEFT JOIN (SELECT k, {case} AS i FROM q) AS d ON p.k = d.k"
        assert " AS i" in normalize(sql, schema=STILL_SCHEMA, dialect="mysql"), case
    right = "SELECT d.i, p.id FROM (SELECT k, COALESCE(w, 0) AS i FROM q) AS d RIGHT JOIN p ON p.k = d.k"
    assert "COALESCE" in normalize(right, schema=STILL_SCHEMA, dialect="mysql").upper()


def test_faithful_sql_refuses_what_a_dialect_cannot_print():
    if PRINTS_DIV:
        assert faithful_sql(sqlglot.parse_one("SELECT a DIV 2 FROM t", read="mysql"), "mysql") == "SELECT a DIV 2 FROM t"
    else:  # MySQL's own generator writes CAST(a / 2 AS SIGNED), which rounds where DIV truncates
        with pytest.raises(LossySql):
            faithful_sql(sqlglot.parse_one("SELECT a DIV 2 FROM t", read="mysql"), "mysql")
    full = sqlglot.parse_one("SELECT COUNT(*) FROM a FULL JOIN b ON a.k = b.k", read="mysql")
    assert "FULL JOIN" in faithful_sql(full, "mysql")  # MySQL's generator writes a LEFT/RIGHT union that doubles the count
    hyphen = sqlglot.parse_one("SELECT 1 FROM `my-project.d.t`", read="bigquery")
    assert sqlglot.parse_one(faithful_sql(hyphen, "bigquery"), read="bigquery").find(sqlglot.exp.Table).catalog == "my-project"
    # an OR built under an AND without parentheses prints as (a AND b) OR c: the OR-under-AND class of wrong proof
    unparenthesized = sqlglot.exp.select("x").from_("t").where(
        sqlglot.exp.And(this=sqlglot.parse_one("a = 1"), expression=sqlglot.parse_one("b = 2 OR c = 3"))
    )
    with pytest.raises(LossySql):
        faithful_sql(unparenthesized, "mysql")


def test_alias_column_lists_become_explicit_renames():
    tree = expand_alias_columns(sqlglot.parse_one("SELECT name FROM dept AS d(name, x)", read="postgres"), CALCITE)
    assert "deptno AS name" in tree.sql() and "name AS x" in tree.sql()
    cte = expand_alias_columns(sqlglot.parse_one("WITH c AS (SELECT 1 AS p, 2 AS q) SELECT r FROM c AS z(r)", read="postgres"), None)
    assert "p AS r" in cte.sql()
    for unresolved in ("SELECT a FROM unknown_table AS u(a)", "SELECT a FROM dept AS d(a, b, c)", "SELECT a FROM (SELECT * FROM dept) AS d(a)"):
        assert not prove_equivalent_algebraic(unresolved, unresolved, schema=CALCITE, dialect="postgres").proven


def test_column_positions_that_cannot_be_spelled_out_are_declined():
    for sql in ("SELECT * FROM s ORDER BY 1 LIMIT 2", "SELECT a FROM s ORDER BY 3 LIMIT 2", "SELECT COUNT(*) FROM s GROUP BY 1"):
        assert not prove_equivalent_algebraic(sql, sql, schema=STILL_SCHEMA, dialect="mysql").proven


TABLE_FUNCTION_CTE = [
    pytest.param(
        "WITH cte AS (SELECT 1 AS l UNION ALL SELECT 2) SELECT * FROM histogram_values(cte, l)",
        "SELECT * FROM histogram_values(cte, l)",
        id="cte-passed-by-bare-name-is-read",
    ),
    pytest.param(
        "WITH cte AS (SELECT a AS l FROM t) SELECT * FROM histogram_values(cte, l)",
        "WITH cte AS (SELECT b AS l FROM t) SELECT * FROM histogram_values(cte, l)",
        id="different-ctes-passed-by-bare-name",
    ),
    pytest.param(
        "WITH c AS (SELECT a FROM t) SELECT * FROM ML.PREDICT(MODEL m, TABLE c)",
        "WITH c AS (SELECT b AS a FROM t) SELECT * FROM ML.PREDICT(MODEL m, TABLE c)",
        id="cte-passed-as-table-argument",
    ),
]


@pytest.mark.parametrize("left,right", TABLE_FUNCTION_CTE)
def test_a_cte_read_by_a_table_function_is_never_dropped(left, right):
    from kumosql.equivalence import prove_equivalent
    from kumosql.rewrite import verify_rewrite

    assert not prove_equivalent(left, right).proven
    assert verify_rewrite(left, right).status.value != "proven"
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, schema={"t": ["a", "b"]}).proven


def test_a_comparison_followed_by_is_without_parentheses_is_declined():
    # sqlglot reads a = b IS TRUE as a = (b IS TRUE); the engines read (a = b) IS TRUE
    left, right = "SELECT * FROM s WHERE a = b IS TRUE", "SELECT * FROM s WHERE a = (b IS TRUE)"
    for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
        assert not prove(left, right, schema=STILL_SCHEMA, dialect="mysql").proven
    assert prove_equivalent_algebraic(right, "SELECT * FROM s WHERE (b IS TRUE) = a", schema=STILL_SCHEMA, dialect="mysql").proven


def test_a_bare_column_keeps_its_source_when_a_derived_table_is_read_as_its_base_table():
    schema = {"t": ["k", "a", "b"], "u": ["k", "a", "c"]}
    left = "SELECT a FROM t JOIN (SELECT k, a + 1 AS y FROM u) AS g ON g.k = t.k"
    # reading g as u would put a second column a in scope; the bare a stays t's
    assert normalize(left, schema=schema, dialect="mysql").startswith("SELECT t.a ")
    assert prove_equivalent_algebraic(left, "SELECT t.a FROM t JOIN u AS g ON g.k = t.k", schema=schema, dialect="mysql").proven
    assert not prove_equivalent_algebraic(left, "SELECT g.a FROM t JOIN u AS g ON g.k = t.k", schema=schema, dialect="mysql").proven


def test_a_correlated_column_whose_table_name_is_reused_inside_is_read_as_the_outer_row():
    # the derived table named dept has only f, so the bare deptno is the outer row's (#370 declined this
    # before the prover named the derived table and the source inside it apart; reading it as the inner row's
    # is the wrong answer, and it is what the second assertion keeps out)
    left = "SELECT name FROM dept WHERE EXISTS (SELECT 1 FROM (SELECT 2 * deptno AS f FROM dept) AS dept WHERE deptno = dept.f)"
    right = "SELECT o.name FROM dept AS o WHERE EXISTS (SELECT 1 FROM dept AS t WHERE o.deptno = 2 * t.deptno)"
    captured = "SELECT o.name FROM dept AS o WHERE EXISTS (SELECT 1 FROM dept AS t WHERE t.deptno = 2 * t.deptno)"
    assert prove_equivalent_algebraic(left, right, schema=CALCITE, dialect="mysql", compare_names=False).proven
    assert not prove_equivalent_algebraic(left, captured, schema=CALCITE, dialect="mysql", compare_names=False).proven
    fixed = "SELECT name FROM dept WHERE EXISTS (SELECT 1 FROM (SELECT 2 * deptno AS f FROM dept) AS t WHERE deptno = t.f)"
    assert prove_equivalent_algebraic(fixed, right, schema=CALCITE, dialect="mysql", compare_names=False).proven


# Wrong proofs from the S006 audit of set operations and outer joins (BigQuery SQL).
# (left, right, schema, rows on which they differ); table names with dots are loaded with "_" for DuckDB.
S006_WRONG_PROOFS = [
    pytest.param(
        "SELECT x FROM t UNION DISTINCT SELECT x FROM t LIMIT 0",
        "SELECT DISTINCT x FROM t",
        {"t": ["x"]},
        {"t": [(1,)]},
        id="s006-001-union-limit-is-kept",
    ),
    pytest.param(
        "SELECT x FROM t INTERSECT DISTINCT SELECT y FROM u LIMIT 0",
        "SELECT DISTINCT kumosql_s0.x AS x FROM (SELECT x FROM t) AS kumosql_s0"
        " WHERE EXISTS(SELECT 1 FROM (SELECT y FROM u) AS kumosql_r0 WHERE kumosql_s0.x IS NOT DISTINCT FROM kumosql_r0.y)",
        {"t": ["x"], "u": ["y"]},
        {"t": [(1,)], "u": [(1,)]},
        id="s006-002-intersect-limit-is-kept",
    ),
    pytest.param(
        "SELECT x FROM p1.d.t UNION DISTINCT SELECT x FROM p2.d.t",
        "SELECT DISTINCT x FROM p1.d.t",
        {"p1.d.t": ["x"], "p2.d.t": ["x"]},
        {"p1.d.t": [(1,)], "p2.d.t": [(2,)]},
        id="s006-003-same-table-name-in-two-projects",
    ),
    pytest.param(
        "SELECT a.x, d.marker IS NULL AS missing FROM a LEFT JOIN (SELECT u.k, 1 AS marker FROM u GROUP BY u.k) d ON d.k = a.x AND d.k = a.y",
        "SELECT a.x, NOT EXISTS(SELECT 1 FROM u AS kqj0 WHERE kqj0.k = a.x) AS missing FROM a",
        {"a": ["x", "y"], "u": ["k"]},
        {"a": [(1, 2)], "u": [(1,)]},
        id="s006-004-indicator-keeps-every-on-equality",
    ),
    pytest.param(
        "SELECT b.z, d.marker IS NULL AS missing FROM a LEFT JOIN (SELECT DISTINCT 1 AS marker FROM u) d ON TRUE RIGHT JOIN b ON FALSE",
        "SELECT b.z, NOT EXISTS(SELECT 1 FROM u AS kqj3) AS missing FROM a RIGHT JOIN b ON FALSE",
        {"a": ["x"], "b": ["z"], "u": ["k"]},
        {"a": [(1,)], "b": [(7,)], "u": [(1,)]},
        id="s006-005-indicator-padded-by-a-later-right-join",
    ),
    pytest.param(
        "SELECT b.z, d.marker IS NULL AS missing FROM a JOIN (SELECT k, 1 AS marker FROM u GROUP BY k) d ON d.k = a.x FULL JOIN b ON FALSE",
        "SELECT b.z, FALSE AS missing FROM a FULL JOIN b ON FALSE WHERE EXISTS(SELECT 1 FROM u WHERE u.k = a.x)",
        {"a": ["x"], "b": ["z"], "u": ["k"]},
        {"a": [(1,)], "b": [(7,)], "u": [(1,)]},
        id="s006-005-inner-indicator-before-a-full-join",
    ),
    pytest.param(
        "SELECT q.v FROM (SELECT y AS v FROM u ORDER BY v LIMIT 1) AS q, t",
        "SELECT q.w FROM (SELECT y AS w FROM u ORDER BY v LIMIT 1) AS q, t",
        {"t": ["x"], "u": ["y", "v"]},
        {"t": [(5,)], "u": [(1, 9), (9, 1)]},
        id="order-by-output-alias-in-a-derived-limit",
    ),
    pytest.param(
        "SELECT x FROM t WHERE EXISTS (SELECT 1 FROM (SELECT y AS v FROM u ORDER BY v LIMIT 1) AS q WHERE q.v = t.x)",
        "SELECT x FROM t WHERE EXISTS (SELECT 1 FROM (SELECT y AS w FROM u ORDER BY v LIMIT 1) AS q WHERE q.w = t.x)",
        {"t": ["x"], "u": ["y", "v"]},
        {"t": [(1,)], "u": [(1, 9), (9, 1)]},
        id="order-by-output-alias-in-a-derived-limit-under-exists",
    ),
    pytest.param(
        "SELECT t.x FROM t WHERE t.x > ANY(SELECT y AS v FROM u ORDER BY v LIMIT 1)",
        "SELECT t.x FROM t WHERE EXISTS(SELECT 1 FROM (SELECT y AS kumosql_v FROM u ORDER BY v LIMIT 1) AS kumosql_q0 WHERE t.x > kumosql_q0.kumosql_v)",
        {"t": ["x"], "u": ["y", "v"]},
        {"t": [(5,)], "u": [(1, 9), (9, 1)]},
        id="s005-003-order-by-output-alias-under-any",
    ),
    pytest.param(
        "SELECT x FROM t EXCEPT DISTINCT SELECT y FROM u LIMIT 0",
        "SELECT DISTINCT s.x AS x FROM (SELECT x FROM t) AS s WHERE NOT EXISTS(SELECT 1 FROM (SELECT y FROM u) AS r WHERE s.x IS NOT DISTINCT FROM r.y)",
        {"t": ["x"], "u": ["y"]},
        {"t": [(1,)], "u": [(2,)]},
        id="s006-except-limit-is-kept",
    ),
    pytest.param(
        "SELECT x FROM p1.d.t INTERSECT DISTINCT SELECT x FROM p2.d.t",
        "SELECT DISTINCT x FROM p1.d.t",
        {"p1.d.t": ["x"], "p2.d.t": ["x"]},
        {"p1.d.t": [(1,)], "p2.d.t": [(2,)]},
        id="s006-intersect-of-two-projects",
    ),
    pytest.param(
        "SELECT k, (SELECT COUNT(*)) AS c FROM t GROUP BY CUBE(k)",
        "SELECT k, (SELECT COUNT(*)) AS c FROM t GROUP BY k UNION ALL SELECT NULL AS k, (SELECT COUNT(*)) AS c FROM t",
        {"t": ["k"]},
        {"t": [(1,), (2,), (3,)]},
        id="s007-002-cube-with-a-nested-count",
    ),
]


def _bigquery_bags_differ(left: str, right: str, schema: dict[str, list[str]], rows: dict[str, list[tuple]]) -> bool:
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    for name, columns in schema.items():
        db.execute(f"CREATE TABLE {name.replace('.', '_')} ({', '.join(c + ' BIGINT' for c in columns)})")
        for row in rows.get(name, []):
            db.execute(f"INSERT INTO {name.replace('.', '_')} VALUES ({', '.join('?' for _ in row)})", row)

    def duck(sql: str) -> str:
        tree = sqlglot.parse_one(sql, read="bigquery")
        for table in tree.find_all(sqlglot.exp.Table):
            if table.args.get("db"):
                table.replace(sqlglot.exp.to_table("_".join(p for p in (table.catalog, table.db, table.name) if p)).as_(table.alias_or_name))
        return tree.sql(dialect="duckdb")

    found_left, found_right = run_unoptimized(db, duck(left), duck(right))
    return Counter(found_left) != Counter(found_right)


@pytest.mark.parametrize("left,right,schema,rows", S006_WRONG_PROOFS)
def test_s006_pairs_that_differ_are_never_proven(left, right, schema, rows):
    assert _bigquery_bags_differ(left, right, schema, rows)
    assert not prove_equivalent_algebraic(left, right, schema=schema, dialect="bigquery", compare_names=False).proven


S006_STILL_PROVEN = [
    pytest.param("SELECT x FROM t UNION DISTINCT SELECT x FROM t", "SELECT DISTINCT x FROM t", id="union-of-one-table"),
    pytest.param("SELECT x FROM p1.d.t UNION DISTINCT SELECT x FROM p1.d.t", "SELECT DISTINCT x FROM p1.d.t", id="union-of-one-qualified-table"),
    pytest.param(
        "SELECT x FROM p1.d.t WHERE x > 1 UNION DISTINCT SELECT x FROM p1.d.t WHERE x < 0",
        "SELECT DISTINCT x FROM p1.d.t WHERE x > 1 OR x < 0",
        id="union-of-filters-of-one-qualified-table",
    ),
    pytest.param("SELECT x FROM p1.d.t UNION DISTINCT SELECT x FROM p2.d.t", "SELECT x FROM p2.d.t UNION DISTINCT SELECT x FROM p1.d.t", id="union-of-two-projects-commutes"),
    pytest.param(
        "SELECT a.x, d.marker IS NULL AS missing FROM a LEFT JOIN (SELECT u.k, 1 AS marker FROM u GROUP BY u.k) d ON d.k = a.x AND d.k = a.y",
        "SELECT a.x, NOT EXISTS(SELECT 1 FROM u AS q WHERE q.k = a.x AND q.k = a.y) AS missing FROM a",
        id="indicator-with-two-equalities",
    ),
    pytest.param(
        "SELECT a.x, d.marker IS NULL AS missing FROM a LEFT JOIN (SELECT DISTINCT 1 AS marker FROM u) d ON TRUE",
        "SELECT a.x, NOT EXISTS(SELECT 1 FROM u) AS missing FROM a",
        id="indicator-without-a-later-join",
    ),
    pytest.param(
        "SELECT b.z, d.marker IS NULL AS missing FROM a LEFT JOIN (SELECT DISTINCT 1 AS marker FROM u) d ON TRUE LEFT JOIN b ON FALSE",
        "SELECT b.z, NOT EXISTS(SELECT 1 FROM u) AS missing FROM a LEFT JOIN b ON FALSE",
        id="indicator-before-a-later-left-join",
    ),
    pytest.param(
        "SELECT b.z, d.marker IS NULL AS missing FROM b RIGHT JOIN a ON FALSE LEFT JOIN (SELECT DISTINCT 1 AS marker FROM u) d ON TRUE",
        "SELECT b.z, NOT EXISTS(SELECT 1 FROM u) AS missing FROM b RIGHT JOIN a ON FALSE",
        id="indicator-after-an-earlier-right-join",
    ),
    pytest.param(
        "SELECT q.v FROM (SELECT y AS v FROM u ORDER BY v LIMIT 1) AS q, t",
        "SELECT q.w FROM (SELECT y AS w FROM u ORDER BY y LIMIT 1) AS q, t",
        id="order-by-output-alias-is-its-expression",
    ),
    pytest.param(
        "SELECT q.v FROM (SELECT y AS v FROM u ORDER BY y LIMIT 1) AS q, t",
        "SELECT q.w FROM (SELECT y AS w FROM u ORDER BY y LIMIT 1) AS q, t",
        id="renamed-output-of-a-derived-limit",
    ),
]


@pytest.mark.parametrize("left,right", S006_STILL_PROVEN)
def test_s006_near_misses_stay_proven(left, right):
    schema = {"t": ["x"], "p1.d.t": ["x"], "p2.d.t": ["x"], "a": ["x", "y"], "b": ["z"], "u": ["k"]}
    if "ORDER BY" in left:
        schema = {"t": ["x"], "u": ["y", "v"]}
    assert prove_equivalent_algebraic(left, right, schema=schema, dialect="bigquery", compare_names=False).proven


def test_s006_dataset_names_are_case_sensitive():
    # BigQuery dataset and table names are case-sensitive: p.d.t and p.D.t are two tables
    from kumosql.setop_rules import merge_same_source

    schema = {"p.d.t": ["x"], "p.D.t": ["x"]}
    left, right = "SELECT x FROM p.d.t UNION DISTINCT SELECT x FROM p.D.t", "SELECT DISTINCT x FROM p.d.t"
    assert merge_same_source(sqlglot.parse_one(left, read="bigquery")) is None
    assert not prove_equivalent_algebraic(left, right, schema=schema, dialect="bigquery").proven


def test_s006_set_operation_rules_do_not_pair_by_name_columns_by_position():
    # S006-006 and S006-007: the public prover aligns BY NAME first, so these are checked on the rules themselves
    from kumosql.set_split_rules import split_distinct_select
    from kumosql.setop_rules import merge_same_source, set_operation_to_exists

    union = sqlglot.parse_one("SELECT x AS a, y AS b FROM t UNION DISTINCT BY NAME SELECT x AS b, y AS a FROM t", read="bigquery")
    assert merge_same_source(union) is None
    except_ = sqlglot.parse_one("SELECT x AS a, y AS b FROM t EXCEPT DISTINCT BY NAME SELECT x AS b, y AS a FROM u", read="bigquery")
    assert set_operation_to_exists(except_) is None
    over = sqlglot.parse_one("SELECT DISTINCT d.a, d.b FROM (SELECT x AS a, y AS b FROM t UNION ALL BY NAME SELECT x AS b, y AS a FROM u) d", read="bigquery")
    assert split_distinct_select(over) is None


def test_an_order_key_that_is_not_an_output_never_stands_in_for_an_output_column():
    # LLM-SQL-Solver negatives 124/125: the hidden order key became a second core column and matched Population
    schema = {"city": ["name", "population"]}
    two = "SELECT name, population FROM city ORDER BY population DESC LIMIT 1"
    one = "SELECT name FROM city ORDER BY population DESC LIMIT 1"
    for dialect in ("bigquery", "sqlite"):
        for prove in (prove_equivalent_algebraic, prove_equivalent_smt):
            assert not prove(two, one, schema=schema, dialect=dialect, compare_names=False).proven
            assert not prove(one, two, schema=schema, dialect=dialect, compare_names=False).proven
        same = "SELECT c.name FROM city AS c ORDER BY c.population DESC LIMIT 1"
        assert prove_equivalent_algebraic(one, same, schema=schema, dialect=dialect, compare_names=False).proven


def test_in_over_union_split_keeps_a_union_level_limit():
    # the sibling of S009-005: x IN (A UNION B LIMIT n) is not x IN (A) OR x IN (B)
    from kumosql.set_split_rules import _split_in_over_union

    skip_if_unparseable("SELECT x FROM t WHERE x IN ((SELECT a FROM p UNION ALL SELECT b FROM q) ORDER BY 1 LIMIT 1 OFFSET 1)")
    for sub in (
        "SELECT a FROM p UNION DISTINCT SELECT b FROM q LIMIT 1",
        "(SELECT a FROM p UNION ALL SELECT b FROM q) ORDER BY 1 LIMIT 1 OFFSET 1",
        "SELECT a FROM p UNION ALL SELECT b FROM q LIMIT 1",
    ):
        assert _split_in_over_union(sqlglot.parse_one(f"SELECT x FROM t WHERE x IN ({sub})", read="bigquery")) is None, sub
    split = _split_in_over_union(sqlglot.parse_one("SELECT x FROM t WHERE x IN (SELECT a FROM p UNION ALL SELECT b FROM q)", read="bigquery"))
    assert split is not None and "LIMIT" not in split.sql()


DISTINCT_ON_SCHEMA = {"u": ["k"], "t": ["x", "y"]}
DISTINCT_ON_WRONG_PROOFS = [
    pytest.param(
        "SELECT a.k FROM u a LEFT JOIN (SELECT DISTINCT ON (x) 1 AS one FROM t) d ON TRUE WHERE d.one IS NOT NULL",
        "SELECT a.k FROM u a WHERE EXISTS (SELECT 1 FROM t)",
        id="distinct-on-is-not-a-one-row-indicator",
    ),
    pytest.param(
        "SELECT d.y FROM (SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y DESC) d",
        "SELECT d.y FROM (SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y) d",
        id="distinct-on-keeps-its-derived-order",
    ),
    pytest.param(
        "SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y DESC",
        "SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y",
        id="distinct-on-keeps-its-result-order",
    ),
]


@pytest.mark.parametrize("left,right", DISTINCT_ON_WRONG_PROOFS)
def test_distinct_on_pairs_that_differ_are_never_proven(left, right):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import run_unoptimized

    db = duckdb.connect()
    db.execute("CREATE TABLE u (k BIGINT); CREATE TABLE t (x BIGINT, y BIGINT)")
    db.execute("INSERT INTO u VALUES (1); INSERT INTO t VALUES (1, 1), (1, 2), (2, 1)")
    found_left, found_right = run_unoptimized(db, left, right)
    assert Counter(found_left) != Counter(found_right)
    assert not prove_equivalent_algebraic(left, right, schema=DISTINCT_ON_SCHEMA, dialect="duckdb").proven


def test_distinct_on_near_misses_stay_proven():
    same = "SELECT d.y FROM (SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y DESC) d"
    aliased = "SELECT d.y FROM (SELECT DISTINCT ON (x) x, y FROM t ORDER BY x, y DESC) AS d"
    assert prove_equivalent_algebraic(same, aliased, schema=DISTINCT_ON_SCHEMA, dialect="duckdb").proven
    indicator = "SELECT a.k FROM u a LEFT JOIN (SELECT DISTINCT 1 AS one FROM t) d ON TRUE WHERE d.one IS NOT NULL"
    assert prove_equivalent_algebraic(indicator, "SELECT a.k FROM u a WHERE EXISTS (SELECT 1 FROM t)", schema=DISTINCT_ON_SCHEMA, dialect="duckdb").proven


def test_distinct_on_operands_are_not_one_filtered_table():
    # t = {(1, 5), (1, 6)}: the filtered branch keeps 6, the other keeps one row for x = 1, so the union can hold
    # both values; reading both branches as filters of t would make it SELECT DISTINCT y FROM t (an unordered
    # DISTINCT ON picks an arbitrary row, so this is checked on the rule and the prover, not on DuckDB)
    from kumosql.setop_rules import merge_same_source

    left = "SELECT DISTINCT ON (x) y FROM t WHERE 6 = y UNION SELECT DISTINCT ON (x) y FROM t"
    assert merge_same_source(sqlglot.parse_one(left, read="duckdb")) is None
    right = "SELECT DISTINCT ON (x) y FROM t UNION SELECT DISTINCT ON (x) y FROM t"
    assert not prove_equivalent_algebraic(left, right, schema=DISTINCT_ON_SCHEMA, dialect="duckdb").proven
@pytest.mark.parametrize(
    "left,right",
    [
        ("SELECT * EXCEPT (b) FROM t", "SELECT * FROM t"),
        ("SELECT x.* EXCEPT (b) FROM t AS x", "SELECT x.* FROM t AS x"),
        ("SELECT * EXCEPT (b) FROM t UNION ALL SELECT * FROM t", "SELECT * FROM t UNION ALL SELECT * FROM t"),
    ],
)
def test_a_star_except_is_not_read_as_a_plain_star(left, right):
    # sqlglot 30 renamed the star's "except" argument to "except_"; a check of the old name alone missed it
    schema = {"t": ["a", "b"]}
    assert not prove_equivalent_smt(left, right, schema=schema, dialect="bigquery").proven
    assert not prove_equivalent_algebraic(left, right, schema=schema, dialect="bigquery").proven
