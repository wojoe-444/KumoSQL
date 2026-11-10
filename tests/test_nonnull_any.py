from collections import Counter
import duckdb
import pytest
import sqlglot
from kumosql.nonnull_any import rewrite_nonnull_any

SCHEMA = {"a": ["x", "g"], "b": ["y", "g"]}
NN = {"a": {"x"}, "b": {"y"}}
TYPES = {"a": {"x": "INT", "g": "VARCHAR"}, "b": {"y": "INT", "g": "VARCHAR"}}
LEFT = "SELECT a.x,a.x>ANY(SELECT b.y FROM b WHERE b.g=a.g) AS f FROM a"
RIGHT = "SELECT a.x,(a.x>q.m) IS TRUE AND q.c IS NOT NULL AS f FROM a LEFT JOIN (SELECT g,MIN(y) m,COUNT(*) c FROM b GROUP BY g) q ON a.g=q.g"


@pytest.mark.parametrize(
    "rows_a,rows_b",
    [
        ([], []),
        ([(1, "a")], []),
        ([(1, "a"), (1, "a")], [(2, "a")]),
        ([(1, "a"), (3, "b"), (5, None)], [(2, "a"), (1, "b"), (3, "b")]),
        ([(3, "a"), (1, "a")], [(1, "a"), (1, "a"), (2, "a")]),
    ],
)
def test_grouped_nonnull_any_preserves_values_and_bags(rows_a, rows_b):
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g VARCHAR)")
    if rows_a:
        db.executemany("INSERT INTO a VALUES (?,?)", rows_a)
    if rows_b:
        db.executemany("INSERT INTO b VALUES (?,?)", rows_b)
    queries = [LEFT, RIGHT] + [
        rewrite_nonnull_any(
            sqlglot.parse_one(q, read="duckdb"), SCHEMA, NN, TYPES, set()
        ).sql(dialect="duckdb")
        for q in (LEFT, RIGHT)
    ]
    bags = [Counter(db.execute(q).fetchall()) for q in queries]
    assert all(bag == bags[0] for bag in bags)
    assert "LEFT JOIN" not in queries[-1]


@pytest.mark.parametrize(
    "query",
    [
        RIGHT.replace("MIN(y)", "MAX(y)"),
        RIGHT.replace("GROUP BY g", "GROUP BY ROLLUP(g)"),
        RIGHT.replace("GROUP BY g", "GROUP BY g,y"),
        RIGHT.replace("q.c IS NOT NULL", "q.c IS NULL"),
        RIGHT.replace("MIN(y)", "MIN(DISTINCT y)"),
        RIGHT.replace("SELECT a.x,", "SELECT a.x,q.c,"),
        RIGHT.replace("FROM b GROUP", "FROM other.b GROUP"),
        RIGHT.replace("AND q.c IS NOT NULL", "AND q.c IS NOT NULL AND 'A'='a'"),
    ],
)
def test_grouped_any_declines_changed_premises(query):
    original = sqlglot.parse_one(query, read="duckdb")
    before = original.sql()
    assert rewrite_nonnull_any(original, SCHEMA, NN, TYPES, set()).sql() == before


def test_nullable_values_remain_three_valued():
    for query in (LEFT, RIGHT):
        original = sqlglot.parse_one(query, read="duckdb")
        before = original.sql()
        assert (
            rewrite_nonnull_any(original, SCHEMA, {"a": {"x"}}, TYPES, set()).sql()
            == before
        )


def test_min_comparison_needs_integer_order_types():
    original = sqlglot.parse_one(RIGHT, read="duckdb")
    before = original.sql()
    wrong = {"a": {"x": "INT"}, "b": {"y": "VARCHAR"}}
    assert rewrite_nonnull_any(original, SCHEMA, NN, wrong, set()).sql() == before


def test_window_null_ordering_is_observable():
    queries = [
        f"SELECT x,ROW_NUMBER() OVER(ORDER BY x NULLS {order}) r FROM a"
        for order in ("FIRST", "LAST")
    ]
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER,g VARCHAR)")
    db.execute("INSERT INTO a VALUES (NULL,'a'),(1,'a')")
    bags = []
    for query in queries:
        parsed = sqlglot.parse_one(query, read="duckdb")
        before = parsed.sql()
        candidate = rewrite_nonnull_any(parsed, SCHEMA, NN, TYPES, set())
        assert candidate.sql() == before
        bags.append(Counter(db.execute(candidate.sql(dialect="duckdb")).fetchall()))
    assert bags[0] != bags[1]


def test_direct_any_preserves_alias_shadowing():
    query = "SELECT a.x>aNY(SELECT a.y FROM b a WHERE a.g IS NOT NULL) FROM a"
    converted = rewrite_nonnull_any(
        sqlglot.parse_one(query, read="duckdb"), SCHEMA, NN
    ).sql(dialect="duckdb")
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g VARCHAR)")
    db.execute("INSERT INTO a VALUES (2,'a')")
    db.execute("INSERT INTO b VALUES (1,'b')")
    assert db.execute(query).fetchall() == db.execute(converted).fetchall()


EXPANDED = """CASE WHEN EXISTS(SELECT 1 FROM b WHERE a.x>b.y)
THEN TRUE WHEN EXISTS(SELECT 1 FROM b WHERE a.x>b.y OR a.x IS NULL OR b.y IS NULL)
THEN NULL ELSE FALSE END"""


def test_expanded_case_removes_unreachable_null_arm():
    query = f"SELECT {EXPANDED} AS f FROM a"
    converted = rewrite_nonnull_any(
        sqlglot.parse_one(query, read="duckdb"), SCHEMA, NN, TYPES, set()
    ).sql(dialect="duckdb")
    assert "CASE" not in converted
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g VARCHAR)")
    for values in ([], [(1, "a")], [(4, "a"), (4, "a")]):
        db.execute("DELETE FROM a")
        if values:
            db.executemany("INSERT INTO a VALUES (?,?)", values)
        assert db.execute(query).fetchall() == db.execute(converted).fetchall()
        db.execute("DELETE FROM b")
        db.execute("INSERT INTO b VALUES (2,'a')")
        assert db.execute(query).fetchall() == db.execute(converted).fetchall()


@pytest.mark.parametrize("join", ["RIGHT", "FULL"])
def test_expanded_case_does_not_assume_outer_padding_nonnull(join):
    query = f"SELECT {EXPANDED} AS f FROM a {join} JOIN b z ON FALSE"
    original = sqlglot.parse_one(query, read="duckdb")
    before = original.sql()
    assert rewrite_nonnull_any(original, SCHEMA, NN, TYPES, set()).sql() == before
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR)")
    db.execute("CREATE TABLE b(y INTEGER NOT NULL,g VARCHAR)")
    db.execute("INSERT INTO b VALUES (2,'a')")
    assert db.execute(query).fetchall() == [(None,)]


@pytest.mark.parametrize("query", [
    "SELECT COUNT(*),a.x>ANY(SELECT b.y FROM b) FROM a",
    "SELECT COUNT(*),(SELECT a.x>ANY(SELECT b.y FROM b) FROM a LIMIT 1) FROM b",
    f"SELECT COUNT(*),{EXPANDED} FROM a",
])
def test_nonnull_any_keeps_aggregate_output_phase(query):
    tree = sqlglot.parse_one(query)
    before = tree.sql()
    assert rewrite_nonnull_any(tree, SCHEMA, NN, TYPES, set()).sql() == before


# The expansion as the decorrelation steps leave it: the correlation joined by AND, the three-valued part in parentheses.
FIRST = "EXISTS(SELECT 1 FROM b WHERE a.g=b.g AND a.x>b.y)"
SECOND = "EXISTS(SELECT 1 FROM b WHERE a.g=b.g AND (a.x IS NULL OR b.y IS NULL OR a.x>b.y))"
CORRELATED = f"CASE WHEN {FIRST} THEN TRUE WHEN {SECOND} THEN NULL ELSE FALSE END"


def _tables(rows_a, rows_b, y_not_null=True):
    db = duckdb.connect()
    db.execute("CREATE TABLE a(x INTEGER NOT NULL,g VARCHAR)")
    db.execute(f"CREATE TABLE b(y INTEGER{' NOT NULL' if y_not_null else ''},g VARCHAR)")
    if rows_a:
        db.executemany("INSERT INTO a VALUES (?,?)", rows_a)
    if rows_b:
        db.executemany("INSERT INTO b VALUES (?,?)", rows_b)
    return db


@pytest.mark.parametrize(
    "rows_a,rows_b",
    [
        ([], []),
        ([(1, "a")], []),
        ([(2, "a"), (2, "a"), (0, "a")], [(1, "a"), (1, "a")]),
        ([(1, "a"), (3, "b"), (5, None)], [(2, "a"), (1, "b"), (3, None)]),
    ],
)
def test_expanded_case_with_parenthesized_guards_removes_null_arm(rows_a, rows_b):
    query = f"SELECT a.x,{CORRELATED} AS f FROM a"
    converted = rewrite_nonnull_any(
        sqlglot.parse_one(query, read="duckdb"), SCHEMA, NN, TYPES, set()
    ).sql(dialect="duckdb")
    assert "CASE" not in converted and "IS NULL" not in converted
    db = _tables(rows_a, rows_b)
    assert Counter(db.execute(query).fetchall()) == Counter(db.execute(converted).fetchall())


@pytest.mark.parametrize(
    "second,not_null,rows_a,rows_b",
    [
        # y may be NULL: the NULL arm is reached (2 > NULL), where the first EXISTS alone says FALSE
        (SECOND, {"a": {"x"}}, [(2, "a")], [(None, "a")]),
        # another comparison in the parentheses: the second EXISTS is not the first
        (SECOND.replace("a.x>b.y", "a.x>=b.y"), NN, [(2, "a")], [(2, "a")]),
        # the guards joined to another correlation
        (SECOND.replace("a.g=b.g", "a.g<>b.g"), NN, [(1, "a")], [(0, "b")]),
        # OR in place of AND: the parentheses around the comparison are dropped, the two bodies still differ
        (SECOND.replace("a.g=b.g AND", "a.g=b.g OR"), NN, [(1, "a")], [(0, "b")]),
    ],
)
def test_parenthesized_guards_keep_a_reachable_null_arm(second, not_null, rows_a, rows_b):
    query = f"SELECT CASE WHEN {FIRST} THEN TRUE WHEN {second} THEN NULL ELSE FALSE END AS f FROM a"
    original = sqlglot.parse_one(query, read="duckdb")
    before = original.sql()
    assert rewrite_nonnull_any(original, SCHEMA, not_null, TYPES, set()).sql() == before
    # the arm matters: the CASE and its first EXISTS alone give different values
    db = _tables(rows_a, rows_b, y_not_null="y" in not_null.get("b", ()))
    assert db.execute(query).fetchall() != db.execute(f"SELECT {FIRST} AS f FROM a").fetchall()
