"""Wrappers, joins and ``EXISTS`` tests that hide a chain of ``IN`` tests (``kumosql.semijoin_shapes``)."""

import random

import pytest
import sqlglot
from sqlglot import exp

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.semijoin_shapes import rewrite_semijoin_shapes

SCHEMA = {"c": ["id", "name"], "o": ["oid", "id", "p"], "w": ["id", "z"]}


def rewritten(sql, types=None, dialect="mysql"):
    """``sql`` after every step that applies (``None`` when none does)."""

    tree = sqlglot.parse_one(sql, read=dialect)
    text = tree.sql(dialect=dialect)
    for _ in range(20):
        for select in list(tree.find_all(exp.Select)):
            result = rewrite_semijoin_shapes(select, SCHEMA, types)
            if result is None:
                continue
            if result is not select:
                if select is tree:
                    tree = result
                else:
                    select.replace(result)
            break
        else:
            break
    after = tree.sql(dialect=dialect)
    return after if after != text else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql").proven


def run(rows, *queries):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name, columns in SCHEMA.items():
        db.execute(f"CREATE TABLE {name} ({', '.join(f'{column} BIGINT' for column in columns)})")
        insert_rows(db, name, rows.get(name, []))
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])]


def differ_on_duckdb(left, right, rows):
    a, b = run(rows, left, right)
    return a != b


def test_wrapper_in_an_in_subquery_is_merged():
    assert rewritten("SELECT id FROM c WHERE id IN (SELECT d.id FROM (SELECT id FROM o WHERE p = 1) AS d WHERE d.id > 2)") == (
        "SELECT id FROM c WHERE id IN (SELECT id AS id FROM o WHERE p = 1 AND id > 2)"
    )
    assert rewritten("SELECT id FROM c WHERE NOT id IN (SELECT d.k FROM (SELECT x.id AS k, x.p FROM o AS x WHERE x.p = 1) AS d WHERE d.p = c.name)") == (
        "SELECT id FROM c WHERE NOT id IN (SELECT x.id AS k FROM o AS x WHERE x.p = 1 AND x.p = c.name)"
    )
    # a wrapper that only picks columns takes any inner select without grouping, DISTINCT or LIMIT
    assert rewritten("SELECT id FROM c WHERE id IN (SELECT d.k FROM (SELECT a.id + 1 AS k, b.p FROM o AS a JOIN w AS b ON a.id = b.id AND a.oid < b.z) AS d)") == (
        "SELECT id FROM c WHERE id IN (SELECT a.id + 1 AS k FROM o AS a JOIN w AS b ON a.id = b.id AND a.oid < b.z)"
    )


def test_exists_tied_by_one_equality_is_an_in():
    assert rewritten("SELECT id FROM c WHERE EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND o.p IN (SELECT z FROM w))") == (
        "SELECT id FROM c WHERE c.id IN (SELECT o.id FROM o WHERE o.p IN (SELECT z FROM w))"
    )
    assert rewritten("SELECT id FROM c WHERE name > 1 AND EXISTS(SELECT 1 FROM (SELECT DISTINCT id FROM o) AS d WHERE c.id = d.id)") == (
        "SELECT id FROM c WHERE name > 1 AND c.id IN (SELECT d.id FROM (SELECT DISTINCT id FROM o) AS d)"
    )


def test_closed_filter_moves_into_distinct_source():
    assert rewritten("SELECT d.id FROM (SELECT DISTINCT id FROM o WHERE p = 1) AS d WHERE NOT d.id IN (SELECT id FROM w) AND d.id > 0") == (
        "SELECT d.id FROM (SELECT DISTINCT id FROM o WHERE p = 1 AND NOT id IN (SELECT id FROM w)) AS d WHERE d.id > 0"
    )
    assert rewritten("SELECT COUNT(*) FROM (SELECT DISTINCT x.id AS k, x.p FROM o AS x) AS d WHERE k IN (SELECT id FROM w WHERE z = 2) OR p = 3") == (
        "SELECT COUNT(*) FROM (SELECT DISTINCT x.id AS k, x.p FROM o AS x WHERE (x.id IN (SELECT id FROM w WHERE z = 2) OR x.p = 3)) AS d"
    )


def test_column_of_a_joined_distinct_is_read_from_the_base_table():
    assert rewritten("SELECT d.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id WHERE d.id > 1 ORDER BY d.id") == (
        "SELECT c.id AS id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id WHERE c.id > 1 ORDER BY c.id"
    )
    # a plain SELECT DISTINCT of one table keeps its column in the select list
    assert rewritten("SELECT d.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE p = 1) AS d ON c.id = d.id") is None
    # a second derived table compared with the first is compared with the base table
    assert rewritten("SELECT c.name FROM (SELECT DISTINCT id FROM o) AS a, (SELECT id, z FROM w) AS b, c WHERE a.id = b.id AND a.id = c.id") == (
        "SELECT c.name FROM (SELECT DISTINCT id FROM o) AS a, (SELECT id, z FROM w) AS b, c WHERE c.id = b.id AND a.id = c.id"
    )
    # an ON clause is left alone where it could not read the base table yet
    assert rewritten("SELECT c.name FROM (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d JOIN w ON w.z < d.id JOIN c ON c.id = d.id") is None


def test_tested_join_partner_in_an_in_subquery_is_a_nested_in():
    assert rewritten("SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id WHERE a.p = 1 AND b.p = 2)") == (
        "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a WHERE a.p = 1 AND a.id IN (SELECT b.id FROM o AS b WHERE b.p = 2))"
    )
    assert rewritten("SELECT id FROM c WHERE NOT id IN (SELECT DISTINCT b.p FROM w AS a, o AS b WHERE a.z = b.p AND a.id > 1)") == (
        "SELECT id FROM c WHERE NOT id IN (SELECT DISTINCT b.p FROM o AS b WHERE b.p IN (SELECT a.z FROM w AS a WHERE a.id > 1))"
    )


@pytest.mark.parametrize("sql", [
    # step 1: not an IN subquery, an inner select that groups, deduplicates or limits, a name the moved table would capture
    "SELECT d.id FROM (SELECT id FROM o WHERE p = 1) AS d WHERE d.id > 2",
    "SELECT id FROM c WHERE NOT EXISTS(SELECT 1 FROM (SELECT x.id AS k, x.p FROM o AS x WHERE x.p = 1) AS d WHERE d.k = c.id)",
    "SELECT id FROM c WHERE id IN (SELECT d.m FROM (SELECT MAX(id) AS m FROM o) AS d WHERE d.m < 3)",
    "SELECT id FROM c WHERE id IN (SELECT d.id FROM (SELECT id FROM o ORDER BY oid LIMIT 1) AS d)",
    "SELECT id FROM c WHERE id IN (SELECT d.id FROM (SELECT id, COUNT(*) AS n FROM o GROUP BY id) AS d WHERE d.n = 1)",
    "SELECT o.id FROM c AS o WHERE NOT o.id IN (SELECT d.id FROM (SELECT id FROM o WHERE p = 1) AS d WHERE o.id IS NULL)",
    "SELECT id FROM c WHERE id IN (SELECT COUNT(d.id) FROM (SELECT id FROM o) AS d)",
    # step 2: under NOT, as a value, two equalities to the outer row, no derived table or membership test to open
    "SELECT id FROM c WHERE NOT EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND o.p IN (SELECT z FROM w))",
    "SELECT id, EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND o.p IN (SELECT z FROM w)) FROM c",
    "SELECT id FROM c WHERE EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND o.p = c.name AND o.oid IN (SELECT z FROM w))",
    "SELECT id FROM c WHERE EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND o.p IN (SELECT z FROM w)) OR name = 1",
    "SELECT id FROM c WHERE EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND o.p = 1)",
    "SELECT id FROM c WHERE EXISTS(SELECT MAX(o.p) FROM o WHERE o.id = c.id AND o.p IN (SELECT z FROM w))",
    # step 3: no DISTINCT to move under, a correlated subquery, a LIMIT below the filter
    "SELECT d.id FROM (SELECT id, COUNT(*) AS n FROM o GROUP BY id) AS d WHERE NOT d.n IN (SELECT z FROM w)",
    "SELECT d.id FROM (SELECT DISTINCT id FROM o) AS d WHERE d.id IN (SELECT w.id FROM w WHERE w.z = d.id)",
    "SELECT d.id FROM (SELECT DISTINCT id FROM o ORDER BY id LIMIT 1) AS d WHERE NOT d.id IN (SELECT id FROM w)",
    # step 4: no base table to read from, an outer join, an equality under OR, a derived table that is not DISTINCT
    "SELECT a.id FROM (SELECT DISTINCT id FROM o) AS a, (SELECT id FROM w) AS b, (SELECT id FROM c) AS e WHERE a.id = b.id AND a.id = e.id",
    "SELECT d.id, c.name FROM c LEFT JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id",
    "SELECT d.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id OR c.name = d.id",
    "SELECT d.id, c.name FROM c JOIN (SELECT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id",
    # step 5: not an IN subquery, a partner joined on another column or shown, tied twice, outer joined, or tested with the other table
    "SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id WHERE a.p = 1 AND b.p = 2",
    "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a JOIN w AS b ON a.p = b.z WHERE b.id = 1)",  # exact, but not a test on a.id
    "SELECT id FROM c WHERE id IN (SELECT a.id + b.oid FROM o AS a JOIN o AS b ON a.id = b.id)",
    "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id AND a.oid = b.p)",
    "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a LEFT JOIN o AS b ON a.id = b.id AND b.p = 2 WHERE a.p = 1)",
    "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id WHERE a.p < b.p)",
    "SELECT id FROM c WHERE id IN (SELECT COUNT(a.id) FROM o AS a JOIN o AS b ON a.id = b.id)",
])
def test_other_queries_stay(sql):
    assert rewritten(sql) is None


def test_declared_types_must_agree():
    two_derived = "SELECT c.name FROM (SELECT DISTINCT id FROM o) AS a, (SELECT id FROM w) AS b, c WHERE a.id = b.id AND a.id = c.id"
    joined = "SELECT d.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id"
    same = {"c": {"id": "INT64"}, "o": {"id": "INT64"}, "w": {"id": "INT64"}}
    assert rewritten(two_derived, same) and rewritten(joined, same)
    for types in (
        {"c": {"id": "INT64"}, "o": {"id": "STRING"}, "w": {"id": "INT64"}},  # 1 = '1' and '1' = '01' is not 1 = '01'
        {"c": {"id": "FLOAT64"}, "o": {"id": "FLOAT64"}, "w": {"id": "FLOAT64"}},  # 0.0 = -0.0, yet they print differently
        {"c": {"id": "INT64"}},  # the other two are not declared
    ):
        assert rewritten(two_derived, types) is None and rewritten(joined, types) is None


A, B, C = "(SELECT id FROM o WHERE p = 1)", "(SELECT id FROM o WHERE p = 2)", "(SELECT id FROM o WHERE p = 3)"
CHAIN = f"SELECT id, name FROM c WHERE id IN {A} AND id IN {B} AND id NOT IN {C}"
PROOFS = [
    # a CTE read through a wrapper
    (CHAIN, f"WITH x AS (SELECT DISTINCT id FROM o WHERE id NOT IN {C} AND id IN {A} AND id IN {B}) SELECT * FROM c WHERE id IN (SELECT * FROM x)"),
    # EXISTS over a derived table
    (CHAIN, f"SELECT c.id, c.name FROM c WHERE EXISTS (SELECT 1 FROM (SELECT id FROM o WHERE id IN {A} AND id IN {B} AND id NOT IN {C}) AS x WHERE x.id = c.id)"),
    # a filter above a DISTINCT derived table, whose column the select list reads
    (CHAIN, f"SELECT y.id, name FROM c JOIN (SELECT id FROM (SELECT DISTINCT id FROM o WHERE p = 1 AND id IN {B}) AS x WHERE id NOT IN {C}) AS y ON c.id = y.id"),
    # two DISTINCT derived tables joined to each other
    (CHAIN, f"WITH a AS (SELECT DISTINCT id FROM o WHERE p = 1), b AS (SELECT DISTINCT id FROM o WHERE p = 2) "
            f"SELECT c.id, c.name FROM a, b, c WHERE a.id = b.id AND a.id = c.id AND c.id NOT IN {C}"),
    # a self-join inside the CTE
    (CHAIN, "WITH x AS (SELECT DISTINCT a.id FROM o AS a INNER JOIN o AS b ON a.id = b.id WHERE a.p = 1 AND b.p = 2 "
            "AND a.id NOT IN (SELECT DISTINCT c.id FROM o AS c WHERE c.p = 3)) SELECT x.id, c.name FROM x INNER JOIN c ON x.id = c.id"),
]


@pytest.mark.parametrize("left, right", PROOFS)
def test_target_pairs_are_proved(left, right):
    assert proved(left, right)


TWO = f"SELECT id, name FROM c WHERE id IN {A} AND id IN {B}"
# Each pair differs on the rows given (checked below), so none may be proved.
NEAR_MISSES = [
    # step 1: a filter above an aggregate is not a filter below it
    ("SELECT id FROM c WHERE id IN (SELECT d.m FROM (SELECT MAX(id) AS m FROM o WHERE p = 1) AS d WHERE d.m < 3)",
     "SELECT id FROM c WHERE id IN (SELECT MAX(id) FROM o WHERE p = 1 AND id < 3)", {"c": [(1, 0)], "o": [(0, 1, 1), (0, 3, 1)]}),
    # step 1: the outer row's alias must not be captured by the table that moves up
    ("SELECT o.id FROM c AS o WHERE o.id NOT IN (SELECT d.id FROM (SELECT id FROM o WHERE p = 1) AS d WHERE o.id IS NULL)",
     "SELECT o.id FROM c AS o WHERE o.id NOT IN (SELECT id FROM o WHERE p = 1 AND o.id IS NULL)", {"c": [(5, 0)], "o": [(0, None, 1)]}),
    # step 1: one row of a grouped wrapper per group, not per row
    ("SELECT id FROM c WHERE id IN (SELECT d.n FROM (SELECT id, COUNT(*) AS n FROM o GROUP BY id) AS d)",
     "SELECT id FROM c WHERE id IN (SELECT 1 FROM o)", {"c": [(1, 0)], "o": [(0, 7, 1), (0, 7, 1)]}),
    # step 2: NOT EXISTS is not NOT IN once the subquery returns a NULL
    ("SELECT id FROM c WHERE NOT EXISTS (SELECT 1 FROM (SELECT id FROM o) AS d WHERE d.id = c.id)",
     "SELECT id FROM c WHERE id NOT IN (SELECT d.id FROM (SELECT id FROM o) AS d)", {"c": [(1, 0)], "o": [(0, None, 1)]}),
    # step 2: as a value EXISTS is FALSE where IN is NULL
    ("SELECT id, EXISTS (SELECT 1 FROM (SELECT id FROM o) AS d WHERE d.id = c.id) FROM c",
     "SELECT id, id IN (SELECT d.id FROM (SELECT id FROM o) AS d) FROM c", {"c": [(1, 0)], "o": [(0, None, 1)]}),
    # step 2: a second tie to the outer row cannot stay behind in the subquery as a constant test
    ("SELECT id FROM c WHERE EXISTS (SELECT 1 FROM (SELECT id, p FROM o) AS d WHERE d.id = c.id AND d.p = c.name)",
     "SELECT id FROM c WHERE id IN (SELECT d.id FROM (SELECT id, p FROM o) AS d)", {"c": [(1, 0)], "o": [(0, 1, 1)]}),
    # step 3: a filter does not move below a LIMIT
    ("SELECT d.id FROM (SELECT DISTINCT id FROM o ORDER BY id LIMIT 1) AS d WHERE d.id NOT IN (SELECT id FROM w)",
     "SELECT DISTINCT id FROM o WHERE id NOT IN (SELECT id FROM w) ORDER BY id LIMIT 1", {"o": [(0, 1, 1), (0, 2, 1)], "w": [(1, 0)]}),
    # step 3: the DISTINCT stays
    ("SELECT COUNT(*) FROM (SELECT DISTINCT id FROM o) AS d WHERE d.id NOT IN (SELECT id FROM w)",
     "SELECT COUNT(*) FROM o WHERE id NOT IN (SELECT id FROM w)", {"o": [(0, 1, 1), (0, 1, 1)]}),
    # step 4: only the column the base table is compared with is that column
    ("SELECT c.name FROM (SELECT DISTINCT id FROM o) AS a, (SELECT DISTINCT id FROM w) AS b, c WHERE a.id = b.id AND a.id = c.id",
     "SELECT c.name FROM (SELECT DISTINCT id FROM o) AS a, (SELECT DISTINCT id FROM w) AS b, c WHERE c.name = b.id AND a.id = c.id",
     {"c": [(1, 0)], "o": [(0, 1, 1)], "w": [(1, 0)]}),
    # step 4: an outer join keeps rows where the two columns are not equal
    ("SELECT d.id, c.name FROM c LEFT JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id",
     "SELECT c.id, c.name FROM c LEFT JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id", {"c": [(1, 0)]}),
    # step 4: so does an equality that is one side of an OR
    ("SELECT d.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id OR c.name = d.id",
     "SELECT c.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id OR c.name = d.id", {"c": [(1, 2)], "o": [(0, 2, 1)]}),
    # step 5: outside an IN test the join returns a row per partner
    ("SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id WHERE a.p = 1 AND b.p = 2",
     "SELECT a.id FROM o AS a WHERE a.p = 1 AND a.id IN (SELECT b.id FROM o AS b WHERE b.p = 2)", {"o": [(0, 1, 1), (0, 1, 2), (0, 1, 2)]}),
    # step 5: an outer join keeps rows without a partner
    ("SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a LEFT JOIN o AS b ON a.id = b.id AND b.p = 2 WHERE a.p = 1)",
     "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a WHERE a.p = 1 AND a.id IN (SELECT b.id FROM o AS b WHERE b.p = 2))",
     {"c": [(1, 0)], "o": [(0, 1, 1)]}),
    # step 5: a second condition between the two tables is not a test on the partner alone
    ("SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id AND a.oid < b.oid WHERE b.p = 2)",
     "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a WHERE a.id IN (SELECT b.id FROM o AS b WHERE b.p = 2))", {"c": [(1, 0)], "o": [(5, 1, 2)]}),
    # the whole chain: one of the three tests missing
    (TWO, f"WITH x AS (SELECT DISTINCT id FROM o WHERE id NOT IN {C} AND id IN {A} AND id IN {B}) SELECT * FROM c WHERE id IN (SELECT * FROM x)",
     {"c": [(1, 0)], "o": [(0, 1, 1), (0, 1, 2), (0, 1, 3)]}),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_differ_and_stay_unproved(left, right, rows):
    assert differ_on_duckdb(left, right, rows)
    assert not proved(left, right)


REWRITES = [
    "SELECT id FROM c WHERE id IN (SELECT d.id FROM (SELECT id FROM o WHERE p = 1) AS d WHERE d.id > 2)",
    "SELECT id FROM c WHERE NOT id IN (SELECT d.k FROM (SELECT x.id AS k, x.p FROM o AS x WHERE x.p <> 1) AS d WHERE d.p = 2 OR d.k = 3)",
    "SELECT id, name FROM c WHERE NOT id IN (SELECT d.k FROM (SELECT x.id AS k, x.p FROM o AS x WHERE x.p = 1) AS d WHERE d.p = c.name)",
    "SELECT id FROM c WHERE id IN (SELECT d.k FROM (SELECT a.id + 1 AS k, b.z FROM o AS a JOIN w AS b ON a.id = b.id AND a.oid <= b.z) AS d)",
    "SELECT id FROM c WHERE EXISTS(SELECT 1 FROM o WHERE o.id = c.id AND NOT o.p IN (SELECT z FROM w))",
    "SELECT name FROM c WHERE name > 1 AND EXISTS(SELECT 1 FROM (SELECT DISTINCT id FROM o WHERE p = 1) AS d WHERE c.id = d.id)",
    "SELECT d.id FROM (SELECT DISTINCT id FROM o WHERE p = 1) AS d WHERE NOT d.id IN (SELECT id FROM w) AND d.id > 0",
    "SELECT COUNT(*), MAX(k) FROM (SELECT DISTINCT x.id AS k, x.p FROM o AS x) AS d WHERE k IN (SELECT id FROM w WHERE z = 2) OR p = 3",
    "SELECT c.name FROM (SELECT DISTINCT id FROM o) AS a, (SELECT id, z FROM w) AS b, c WHERE a.id = b.id AND a.id = c.id",
    "SELECT d.id, c.name FROM c JOIN (SELECT DISTINCT id FROM o WHERE NOT p IN (SELECT z FROM w)) AS d ON c.id = d.id WHERE d.id > 1",
    "SELECT d.id, COUNT(*) FROM w JOIN c ON w.z = c.name JOIN (SELECT DISTINCT a.id FROM o AS a JOIN o AS b ON a.p = b.oid) AS d ON c.id = d.id GROUP BY d.id",
    "SELECT id FROM c WHERE id IN (SELECT a.id FROM o AS a JOIN o AS b ON a.id = b.id WHERE a.p = 1 AND b.p = 2)",
    "SELECT id FROM c WHERE NOT id IN (SELECT DISTINCT b.p FROM w AS a, o AS b WHERE a.z = b.p AND a.id > 1)",
    "SELECT id FROM c WHERE NOT id IN (SELECT a.id FROM o AS a JOIN w AS b ON b.id = a.id JOIN o AS e ON e.oid = a.id WHERE NOT e.p IS NULL)",
]


def test_rewrites_keep_every_result():
    """Original and rewritten query return the same bag on random small tables with NULLs and duplicate rows."""

    rng = random.Random(20261110)
    pairs = [(sql, rewritten(sql)) for sql in REWRITES]
    assert all(after is not None for _, after in pairs)
    for _ in range(60):
        rows = {name: [tuple(rng.choice([None, 1, 2, 3]) for _ in columns) for _ in range(rng.randint(0, 6))] for name, columns in SCHEMA.items()}
        for name in rows:
            rows[name] += rows[name][:2]  # exact duplicate rows
        results = run(rows, *[sql for pair in pairs for sql in pair])
        for index, (before, after) in enumerate(pairs):
            assert results[2 * index] == results[2 * index + 1], (before, after, rows)
