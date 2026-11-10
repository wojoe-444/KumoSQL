import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.null_facts import null_contradiction

SCHEMA = {t: ["a", "b", "c"] for t in ("t", "u")}
TYPES = {t: {c: "INT64" for c in "abc"} for t in ("t", "u")}
FULL = "SELECT x.a AS a, y.b AS b FROM t AS x FULL OUTER JOIN u AS y ON x.a = y.b"
EMPTY1 = "SELECT x.a AS a FROM t AS x WHERE FALSE"
EMPTY2 = "SELECT x.a AS a, x.b AS b FROM t AS x WHERE FALSE"
EMPTY3 = "SELECT x.a AS a, x.b AS b, x.c AS c FROM t AS x WHERE FALSE"


def _proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False).proven


def _rule(sql):
    out = null_contradiction(sqlglot.parse_one(sql, read="bigquery"))
    return out.sql(dialect="bigquery") if out is not None else None


def _rows(sql, data):
    db = duckdb.connect(":memory:")
    for table in ("t", "u"):
        db.execute(f"CREATE TABLE {table} (a BIGINT, b BIGINT, c BIGINT)")
        if data.get(table):
            db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", data[table])
    return db.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]).fetchall()


def test_grouped_sum_of_a_null_rejected_column_is_never_null():
    sql = "SELECT x.a AS a FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY x.a) AS x WHERE x.b IS NULL"
    assert _rule(sql).endswith("WHERE FALSE")
    grouped = "SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY x.a HAVING SUM(x.b) IS NULL AND x.a > 0"
    assert _rule(grouped).endswith("HAVING FALSE")


def test_a_column_null_on_every_row_rejects_a_comparison_above_it():
    sql = f"SELECT x.a AS a, MIN(x.b) AS b FROM ({FULL} WHERE y.b IS NULL) AS x GROUP BY x.a HAVING MIN(x.b) > 1"
    assert _rule(sql).endswith("HAVING FALSE")
    assert _rule("SELECT y.a AS a FROM t AS y WHERE y.b IS NULL AND y.b + 1 > 2").endswith("WHERE FALSE")


def test_rule_leaves_satisfiable_conditions_alone():
    assert _rule("SELECT x.a AS a FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY ROLLUP(x.a)) AS x WHERE x.b IS NULL") is None
    assert _rule("SELECT x.a AS a FROM (SELECT x.a AS a, COUNT(x.b) AS b FROM t AS x GROUP BY x.a) AS x WHERE x.b IS NULL") is not None
    assert _rule("SELECT x.a AS a FROM (SELECT SUM(x.b) AS b, MIN(x.a) AS a FROM t AS x WHERE x.b >= x.a) AS x WHERE x.b IS NULL") is None
    assert _rule("SELECT x.a AS a FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x GROUP BY x.a) AS x WHERE x.b IS NULL") is None
    assert _rule(f"SELECT x.a AS a, MIN(x.b) AS b FROM ({FULL} WHERE y.b IS NULL) AS x GROUP BY x.a HAVING MIN(x.b) IS NULL") is None
    assert _rule(f"SELECT x.a AS a FROM ({FULL} WHERE y.b IS NULL) AS x WHERE COALESCE(x.b, 2) > 1") is None
    assert _rule("SELECT y.a AS a FROM t AS y WHERE y.b IS NULL AND NOT y.b IN (SELECT w.a FROM u AS w)") is None
    assert _rule("SELECT y.a AS a FROM t AS y WHERE y.b IS NULL AND y.b > ALL (SELECT w.a FROM u AS w)") is None
    assert _rule("SELECT y.a AS a FROM t AS y WHERE y.b IS NULL AND y.a > 1") is None


# --- the SQLancer mutation pairs this rule was written for (dev cases 21 and 10) ----------------

C0_21 = (
    "SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT x.a AS a, SUM(x.b) AS b, COUNT(x.c) AS c FROM t AS x "
    "WHERE (x.b >= x.a) AND (x.a <> {k}) GROUP BY x.a) AS x WHERE (x.b IS NULL) AND ({nn})"
)
TAIL_21 = (
    ", c1 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM c0 AS x WHERE x.a NOT IN (SELECT y.c FROM t AS y)), "
    "c2 AS (SELECT DISTINCT x.a AS a, x.b AS b, x.c AS c FROM (SELECT x.a AS a, x.b AS b, x.c AS c FROM c0 AS x "
    "WHERE x.a IN (SELECT y.a FROM u AS y WHERE y.b = 0)) AS x) SELECT x.a AS a, y.b AS b, x.c AS c FROM c2 AS x LEFT JOIN t AS y ON x.a = y.b"
)
C0_10 = "SELECT x.a AS a, e AS b, x.c AS c FROM t AS x CROSS JOIN UNNEST([x.a, x.b, {k}]) AS e WHERE {cond}"
REST_10 = (
    ", c1 AS (SELECT x.a AS a, x.b AS b, x.c AS c FROM (SELECT x.a AS a, y.b AS b, x.c AS c FROM c0 AS x FULL OUTER JOIN u AS y "
    "ON x.a = y.b {where}) AS x WHERE NOT EXISTS (SELECT 1 FROM (SELECT x.a AS a, x.b AS b, x.c AS c FROM t AS x "
    "WHERE (x.b >= x.c) AND (x.b IN (0, 3))) AS y WHERE y.a = x.a AND y.b > 1)) "
    "SELECT x.a AS a, MIN(x.b) AS b, COUNT(*) AS c FROM c1 AS x GROUP BY x.a HAVING {having}"
)


def _case21(k, nn="x.c IS NOT NULL", c0=C0_21):
    return "WITH c0 AS (" + c0.format(k=k, nn=nn) + ")" + TAIL_21


def _case10(k=1, cond="e IS NOT NULL", where="WHERE y.b IS NULL", having="MIN(x.b) > 1"):
    return "WITH c0 AS (" + C0_10.format(k=k, cond=cond) + ")" + REST_10.format(where=where, having=having)


def test_sqlancer_empty_derived_aggregate_pair_is_proved():
    assert _proved(_case21(0), _case21(1, nn="NOT x.c IS NULL"))


def test_sqlancer_null_column_under_full_join_pair_is_proved():
    assert _proved(_case10(), _case10(k=2, cond="NOT e IS NULL"))
    assert _proved(_case10(), _case10(having="MIN(x.b) > 2"))


def test_sqlancer_near_misses_are_not_proved():
    # an inner WHERE that keeps NULL b: a group of NULLs sums to NULL
    loose = C0_21.replace("WHERE (x.b >= x.a)", "WHERE (x.b IS NULL OR x.b >= x.a)")
    assert not _proved(_case21(0, c0=loose), _case21(1, c0=loose))
    # the IS NULL test in ON keeps u's rows with their b
    # (the rule alone must leave it; whether a pair built on it is provable is up to other rules, e.g. the UNNEST split
    # makes the k=1 and k=2 forms of this case provably equal, which they are: the unnested value is never read)
    assert _rule(
        "SELECT x.a AS a, MIN(x.b) AS b FROM (SELECT x.a AS a, y.b AS b FROM t AS x FULL OUTER JOIN u AS y "
        "ON x.a = y.b AND y.b IS NULL) AS x GROUP BY x.a HAVING MIN(x.b) > 1"
    ) is None
    # MIN of an all-NULL column IS NULL in every group: the groups survive
    assert not _proved(_case10(having="MIN(x.b) IS NULL"), _case10(cond="e IS NULL", having="MIN(x.b) IS NULL"))


@pytest.mark.parametrize(
    "left, right",
    [
        ("SELECT x.a AS a, x.b AS b FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY x.a) AS x WHERE x.b IS NULL", EMPTY2),
        ("SELECT x.a AS a, x.b AS b FROM (SELECT x.a AS a, MAX(x.b) AS b FROM t AS x WHERE x.b IN (0, 3) GROUP BY x.a) AS x JOIN u AS y ON x.a = y.a WHERE x.b IS NULL", EMPTY2),
        (f"SELECT x.a AS a, MIN(x.b) AS b, COUNT(*) AS c FROM ({FULL} WHERE y.b IS NULL) AS x GROUP BY x.a HAVING MIN(x.b) > 1", EMPTY3),
        ("SELECT x.a AS a FROM (SELECT y.a AS a, y.b AS b FROM t AS y WHERE y.b IS NULL) AS x WHERE x.b = 1", EMPTY1),
        ("SELECT x.a AS a FROM (SELECT x.a AS a, x.b AS b FROM (SELECT y.a AS a, y.b AS b FROM t AS y WHERE y.b IS NULL) AS x LEFT JOIN u AS z ON x.a = z.a) AS x WHERE x.b + 1 > 2", EMPTY1),
        # a global aggregate over the impossible filter is one row of COUNT 0
        ("SELECT COUNT(*) AS n FROM (SELECT y.a AS a, y.b AS b FROM t AS y WHERE y.b IS NULL) AS x WHERE x.b > 1", "SELECT COUNT(*) AS n FROM t AS x WHERE FALSE"),
    ],
)
def test_impossible_null_facts_prove_emptiness(left, right):
    assert _proved(left, right)


# Each left side returns a row on the database next to it, so it is not the empty right side.
NEAR_MISSES = [
    # not null-rejecting: a group whose b are all NULL sums to NULL
    ("SELECT x.a AS a, x.b AS b FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE (x.b IS NULL OR x.b >= x.a) GROUP BY x.a) AS x WHERE x.b IS NULL", EMPTY2, {"t": [(1, None, 0)]}),
    # a global SUM over no row is NULL
    ("SELECT x.a AS a, x.b AS b FROM (SELECT MIN(x.a) AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a) AS x WHERE x.b IS NULL", EMPTY2, {}),
    # ROLLUP's grand total exists over no row
    ("SELECT x.a AS a, x.b AS b FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY ROLLUP(x.a)) AS x WHERE x.b IS NULL", EMPTY2, {}),
    # MIN of an all-NULL column is NULL, so IS NULL keeps the group
    (f"SELECT x.a AS a, MIN(x.b) AS b, COUNT(*) AS c FROM ({FULL} WHERE y.b IS NULL) AS x GROUP BY x.a HAVING MIN(x.b) IS NULL", EMPTY3, {"t": [(1, 1, 1)]}),
    # IS NULL in ON instead of WHERE: u's own rows keep their b
    (f"SELECT x.a AS a, MIN(x.b) AS b, COUNT(*) AS c FROM ({FULL} AND y.b IS NULL) AS x GROUP BY x.a HAVING MIN(x.b) > 1", EMPTY3, {"u": [(0, 2, 0)]}),
    # CUBE and GROUPING SETS add grand-total rows whose SUM is NULL
    ("SELECT x.a AS a, x.b AS b FROM (SELECT x.a AS a, SUM(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY CUBE(x.a)) AS x WHERE x.b IS NULL", EMPTY2, {}),
    ("SELECT x.a AS a, x.b AS b FROM (SELECT x.a AS a, MAX(x.b) AS b FROM t AS x WHERE x.b >= x.a GROUP BY GROUPING SETS ((x.a), ())) AS x WHERE x.b IS NULL", EMPTY2, {}),
    # a ROLLUP over an all-NULL column still returns its grand total, which a HAVING on COUNT keeps
    (f"SELECT x.a AS a, COUNT(*) AS c FROM ({FULL} WHERE y.b IS NULL) AS x GROUP BY ROLLUP(x.a) HAVING COUNT(*) >= 0", EMPTY1, {}),
    # a LEFT JOIN pads the never-NULL SUM with NULL
    ("SELECT x.a AS a FROM t AS x LEFT JOIN (SELECT y.a AS a, SUM(y.b) AS s FROM u AS y WHERE y.b > 0 GROUP BY y.a) AS z ON x.a = z.a WHERE z.s IS NULL", EMPTY1, {"t": [(1, 1, 1)]}),
    ("SELECT z.a AS a FROM (SELECT y.a AS a, SUM(y.b) AS s FROM u AS y WHERE y.b > 0 GROUP BY y.a) AS z RIGHT JOIN t AS x ON x.a = z.a WHERE z.s IS NULL", EMPTY1, {"t": [(1, 1, 1)]}),
    # a COALESCE output is not the NULL column
    ("SELECT x.a AS a FROM (SELECT y.a AS a, COALESCE(y.b, 1) AS b FROM t AS y WHERE y.b IS NULL) AS x WHERE x.b = 1", EMPTY1, {"t": [(1, None, 1)]}),
    # NOT (NULL IN (no row)) and NULL > ALL (no row) are TRUE
    ("SELECT x.a AS a FROM (SELECT y.a AS a, y.b AS b FROM t AS y WHERE y.b IS NULL) AS x WHERE NOT x.b IN (SELECT w.a FROM u AS w)", EMPTY1, {"t": [(1, None, 1)]}),
    ("SELECT x.a AS a FROM (SELECT y.a AS a, y.b AS b FROM t AS y WHERE y.b IS NULL) AS x WHERE x.b > ALL (SELECT w.a FROM u AS w)", EMPTY1, {"t": [(1, None, 1)]}),
    # a global COUNT over the impossible filter is still one row
    ("SELECT COUNT(*) AS n FROM (SELECT y.a AS a, y.b AS b FROM t AS y WHERE y.b IS NULL) AS x WHERE x.b > 1", "SELECT 0 AS n FROM t AS x WHERE FALSE", {}),
    # without GROUP BY, SUM over no row is NULL
    ("SELECT COUNT(*) AS a FROM t AS x WHERE x.b > 0 HAVING SUM(x.b) IS NULL", EMPTY1, {}),
    # a padded column read after a LEFT JOIN
    ("SELECT x.a AS a FROM t AS x LEFT JOIN u AS y ON x.a = y.a WHERE y.b IS NULL AND x.b = 1", EMPTY1, {"t": [(1, 1, 1)]}),
]


@pytest.mark.parametrize("left, right, data", NEAR_MISSES)
def test_near_misses_are_not_proved_empty(left, right, data):
    assert _rows(left, data) and not _rows(right, data)
    assert not _proved(left, right)


# empty_rules: a select whose grouping has a grand total returns a row over no input


@pytest.mark.parametrize(
    "left, right",
    [
        (
            "SELECT x.a AS a, MAX(x.b) AS b, COUNT(*) AS c FROM t AS x WHERE FALSE GROUP BY ROLLUP(x.a) HAVING NOT COUNT(x.c) IN (SELECT w.c FROM u AS w)",
            "SELECT x.a AS a, MAX(x.b) AS b, COUNT(*) AS c FROM t AS x WHERE FALSE GROUP BY x.a",
        ),
        (
            "SELECT x.a AS a, COUNT(*) AS c FROM (SELECT x.a FROM t AS x WHERE FALSE) AS x GROUP BY GROUPING SETS ((x.a), ()) HAVING NOT COUNT(*) IN (SELECT w.c FROM u AS w)",
            "SELECT x.a AS a, COUNT(*) AS c FROM t AS x WHERE FALSE GROUP BY x.a",
        ),
        ("SELECT 1 AS one FROM t AS x WHERE FALSE HAVING COUNT(*) = 0", "SELECT 1 AS one FROM t AS x WHERE FALSE"),
        ("SELECT COUNT(*) AS n FROM t AS x WHERE FALSE GROUP BY ()", "SELECT 0 AS n FROM t AS x WHERE FALSE"),
        ("SELECT y.n AS n FROM (SELECT SUM(x.b) AS n FROM t AS x WHERE x.b > 0 GROUP BY ()) AS y WHERE y.n IS NULL", "SELECT 0 AS n FROM t AS x WHERE FALSE"),
    ],
)
def test_grand_total_over_no_input_is_not_empty(left, right):
    assert _rows(left, {}) and not _rows(right, {})
    assert not _proved(left, right)
