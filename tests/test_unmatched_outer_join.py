"""A ``LEFT JOIN`` that the ``WHERE`` clause proves unmatched is dropped (``kumosql.unmatched_outer_join``)."""

import random

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.ast_utils import canonical_negation
from kumosql.unmatched_outer_join import drop_unmatched_left_join

SCHEMA = {"t": ["a", "b", "c"], "u": ["x", "y"], "v": ["p", "q"]}


def rewritten(sql, schema=SCHEMA, dialect="mysql"):
    out = drop_unmatched_left_join(canonical_negation(sqlglot.parse_one(sql, read=dialect)), schema, dialect)
    return out.sql(dialect=dialect) if out is not None else None


def proved(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, compare_names=False, dialect="mysql").proven


def run(rows, *queries):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name, columns in SCHEMA.items():
        db.execute(f"CREATE TABLE {name} ({', '.join(c + ' BIGINT' for c in columns)})")
        insert_rows(db, name, rows.get(name, []))
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *[sqlglot.transpile(sql, read="mysql", write="duckdb")[0] for sql in queries])]


def differ_on_duckdb(left, right, rows):
    a, b = run(rows, left, right)
    return a != b


def test_join_ruled_out_by_not_in_is_dropped():
    assert rewritten("SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)") == (
        "SELECT t.a, t.b FROM t WHERE NOT t.a IN (SELECT x FROM u)"
    )
    # more ON conjuncts only narrow the matches; other WHERE conjuncts and later joins stay
    assert rewritten(
        "SELECT t.a, v.q FROM t LEFT JOIN u AS o ON o.y > 3 AND o.x = t.a JOIN v ON v.p = t.b WHERE t.b > 1 AND t.a NOT IN (SELECT DISTINCT z.x FROM u AS z)"
    ) == "SELECT t.a, v.q FROM t JOIN v ON v.p = t.b WHERE t.b > 1 AND NOT t.a IN (SELECT DISTINCT z.x FROM u AS z)"
    # bare names the joined table does not have belong to the other source
    assert rewritten("SELECT a, COUNT(*) FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) GROUP BY a") == (
        "SELECT a, COUNT(*) FROM t WHERE NOT t.a IN (SELECT x FROM u) GROUP BY a"
    )


def test_join_ruled_out_by_not_exists_is_dropped():
    assert rewritten("SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a)") == (
        "SELECT t.a FROM t WHERE NOT EXISTS(SELECT 1 FROM u AS z WHERE z.x = t.a)"
    )


@pytest.mark.parametrize("sql", [
    "SELECT t.a FROM t LEFT JOIN u ON t.b = u.x WHERE t.a NOT IN (SELECT x FROM u)",  # the join matches on another column
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.y WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u WHERE y = 1)",  # only some matches ruled out
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a IN (SELECT x FROM u)",
    "SELECT t.a FROM t JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)",  # an inner join drops the unmatched rows
    "SELECT t.a FROM t RIGHT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) OR t.b = 1",  # not a conjunct
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x OR u.y = 1 WHERE t.a NOT IN (SELECT x FROM u)",  # ON is no conjunction
    "SELECT t.a FROM t LEFT JOIN u ON t.a <= u.x WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT p FROM v)",  # another table
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u LIMIT 1)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT MAX(x) FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a + 1 NOT IN (SELECT x FROM u)",
    "SELECT t.a, u.y FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)",  # the joined table is read
    "SELECT t.a, y FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT * FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) AND u.y IS NULL",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x JOIN v ON v.p = u.y WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) AND EXISTS (SELECT 1 FROM v WHERE v.p = u.y)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) AND EXISTS (SELECT 1 FROM v WHERE v.p = y)",
    "SELECT t.a FROM t LEFT JOIN u USING (a) WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x GROUP BY t.a HAVING t.a NOT IN (SELECT x FROM u)",  # HAVING tests groups
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a AND z.y = 1)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.b)",
    "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a LIMIT 0)",
])
def test_other_joins_stay(sql):
    assert rewritten(sql) is None


def test_nothing_is_dropped_without_the_joined_tables_columns():
    sql = "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)"
    assert rewritten(sql, schema=None) is None
    assert rewritten(sql, schema={"t": ["a", "b", "c"]}) is None


PROOFS = [
    ("SELECT name FROM (SELECT a AS id, b AS name FROM t) AS c WHERE id NOT IN (SELECT x FROM u)",
     "SELECT c.b FROM t AS c LEFT JOIN u AS o ON c.a = o.x WHERE c.a NOT IN (SELECT x FROM u)"),
    ("SELECT t.a, t.b FROM t WHERE t.a NOT IN (SELECT x FROM u)", "SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)"),
    ("SELECT a, b FROM t WHERE a NOT IN (SELECT x FROM u)", "SELECT a, b FROM t LEFT JOIN u ON a = x WHERE a NOT IN (SELECT x FROM u)"),
    ("SELECT t.a, v.q FROM t JOIN v ON v.p = t.b WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a)",
     "SELECT t.a, v.q FROM t LEFT JOIN u AS o ON o.x = t.a JOIN v ON v.p = t.b WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a)"),
    ("SELECT t.a, COUNT(*) FROM t WHERE t.a NOT IN (SELECT x FROM u) GROUP BY t.a",
     "SELECT t.a, COUNT(*) FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) GROUP BY t.a"),
]


@pytest.mark.parametrize("left, right", PROOFS)
def test_target_pairs_are_proved(left, right):
    assert proved(left, right)


PLAIN = "SELECT t.a, t.b FROM t WHERE t.a NOT IN (SELECT x FROM u)"
# Each pair differs on the rows given (checked below), so none may be proved.
NEAR_MISSES = [
    # the join matches on b, which NOT IN does not test: the row comes out twice
    (PLAIN, "SELECT t.a, t.b FROM t LEFT JOIN u ON t.b = u.x WHERE t.a NOT IN (SELECT x FROM u)", {"t": [(1, 2, 0)], "u": [(2, 0), (2, 0)]}),
    # only the y = 1 matches are ruled out; the row matches two other rows
    ("SELECT t.a, t.b FROM t WHERE t.a NOT IN (SELECT x FROM u WHERE y = 1)",
     "SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u WHERE y = 1)", {"t": [(1, 2, 0)], "u": [(1, 0), (1, 0)]}),
    # IN keeps the matched rows, once per match
    ("SELECT t.a, t.b FROM t WHERE t.a IN (SELECT x FROM u)", "SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x WHERE t.a IN (SELECT x FROM u)",
     {"t": [(1, 2, 0)], "u": [(1, 0), (1, 0)]}),
    # an inner join keeps no unmatched row
    (PLAIN, "SELECT t.a, t.b FROM t JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)", {"t": [(1, 2, 0)], "u": [(5, 0)]}),
    # under OR a matched row survives through the other disjunct, once per match
    ("SELECT t.a, t.b FROM t WHERE t.a NOT IN (SELECT x FROM u) OR t.b = 2",
     "SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) OR t.b = 2", {"t": [(1, 2, 0)], "u": [(1, 0), (1, 0)]}),
    # ON with OR matches rows the equality does not
    (PLAIN, "SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x OR u.y = 7 WHERE t.a NOT IN (SELECT x FROM u)", {"t": [(1, 2, 0)], "u": [(5, 7), (6, 7)]}),
    # NOT EXISTS with a narrower test than the join
    ("SELECT t.a FROM t WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a AND z.y = 1)",
     "SELECT t.a FROM t LEFT JOIN u ON t.a = u.x WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a AND z.y = 1)", {"t": [(1, 2, 0)], "u": [(1, 0), (1, 0)]}),
]


@pytest.mark.parametrize("left, right, rows", NEAR_MISSES)
def test_near_misses_differ_and_stay_unproved(left, right, rows):
    assert differ_on_duckdb(left, right, rows)
    assert not proved(left, right)


REWRITES = [
    "SELECT t.a, t.b FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a, t.c FROM t LEFT JOIN u AS o ON o.x = t.b AND o.y > 1 WHERE t.c > 0 AND t.b NOT IN (SELECT z.x FROM u AS z)",
    "SELECT t.a, v.q FROM t LEFT JOIN u ON t.a = u.x LEFT JOIN v ON v.p = t.b WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a, v.q FROM t LEFT JOIN u ON t.a = u.x RIGHT JOIN v ON v.p = t.b WHERE t.a NOT IN (SELECT x FROM u)",
    "SELECT t.a, v.q FROM v JOIN t ON v.p = t.b LEFT JOIN u ON t.a = u.x WHERE NOT EXISTS (SELECT 1 FROM u AS z WHERE z.x = t.a)",
    "SELECT t.b, COUNT(*), SUM(t.a) FROM t LEFT JOIN u ON t.a = u.x WHERE t.a NOT IN (SELECT x FROM u) GROUP BY t.b",
    "SELECT DISTINCT t.c FROM t LEFT JOIN u ON u.x = t.c WHERE t.c NOT IN (SELECT x FROM u) AND t.a IN (SELECT p FROM v)",
]


def test_rewrites_keep_every_result():
    """Original and rewritten select return the same bag on random small tables with NULLs and duplicate rows
    (a NULL in ``u.x`` makes ``NOT IN`` never TRUE; an empty ``u`` makes it TRUE even for a NULL value)."""

    rng = random.Random(20261110)
    pairs = [(sql, rewritten(sql)) for sql in REWRITES]
    assert all(after is not None and after != before for before, after in pairs)
    for trial in range(60):
        value = lambda: rng.choice([None, 1, 2, 3] if trial % 2 else [1, 2, 3, 4])  # noqa: E731
        rows = {name: [tuple(value() for _ in columns) for _ in range(rng.randint(0, 4))] for name, columns in SCHEMA.items()}
        for name in rows:
            rows[name] += rows[name][:1]  # an exact duplicate row
        results = run(rows, *[sql for pair in pairs for sql in pair])
        for index, (before, after) in enumerate(pairs):
            assert results[2 * index] == results[2 * index + 1], (before, after, rows)
