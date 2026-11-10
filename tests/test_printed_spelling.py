"""Concatenations and one-column grouping sets are spelled the way they print (``kumosql.printed_spelling``)."""

import random

import pytest
import sqlglot
from sqlglot import exp

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic
from kumosql.ast_utils import faithful_sql
from kumosql.printed_spelling import spell_as_printed

SCHEMA = {"t": ["a", "b", "c", "k"], "u": ["a", "b", "c", "k"]}
TYPES = {name: {"a": "TEXT", "b": "TEXT", "c": "TEXT", "k": "INT"} for name in SCHEMA}


def proved(left, right, dialect="postgres"):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, dialect=dialect, compare_names=False).proven


def rows_on_duckdb(queries, rows, read="postgres"):
    duckdb = pytest.importorskip("duckdb")
    from kumosql.duckdb_load import insert_rows, run_unoptimized

    db = duckdb.connect()
    for name in SCHEMA:
        db.execute(f"CREATE TABLE {name} (a VARCHAR, b VARCHAR, c VARCHAR, k BIGINT)")
        insert_rows(db, name, rows.get(name, []))
    return [sorted(map(repr, result)) for result in run_unoptimized(db, *(sqlglot.transpile(q, read=read, write="duckdb")[0] for q in queries))]


def differ_on_duckdb(left, right, rows):
    a, b = rows_on_duckdb([left, right], rows)
    return a != b


def concat(*names, **flags):
    return exp.Concat(expressions=[exp.column(n) for n in names], **flags)


def test_a_flagless_concat_becomes_a_left_nested_pipe_chain():
    tree = exp.select(exp.alias_(concat("a", "b", "c"), "n")).from_("t")
    for dialect in ("postgres", "redshift", "duckdb"):
        out = spell_as_printed(tree, dialect)
        assert not list(out.find_all(exp.Concat))
        chain = out.expressions[0].this
        assert type(chain) is exp.DPipe and type(chain.this) is exp.DPipe and chain.expression.name == "c"
        assert faithful_sql(out, dialect) == "SELECT a || b || c AS n FROM t"
    assert tree.sql(dialect="bigquery") == "SELECT CONCAT(a, b, c) AS n FROM t"  # the input tree is not changed


def test_an_operator_operand_keeps_its_grouping():
    inner = exp.EQ(this=exp.column("a"), expression=exp.column("b"))
    tree = exp.select(exp.alias_(exp.Concat(expressions=[inner, exp.column("c")]), "n")).from_("t")
    assert faithful_sql(spell_as_printed(tree, "postgres"), "postgres") == "SELECT (a = b) || c AS n FROM t"
    nested = exp.select(exp.alias_(exp.Concat(expressions=[exp.column("a"), exp.Upper(this=concat("b", "c"))]), "n")).from_("t")
    assert faithful_sql(spell_as_printed(nested, "postgres"), "postgres") == "SELECT a || UPPER(b || c) AS n FROM t"


def test_other_concats_and_dialects_stay():
    skipping = exp.select(exp.alias_(concat("a", "b", coalesce=True), "n")).from_("t")  # PostgreSQL's CONCAT(): NULL is ''
    assert spell_as_printed(skipping, "postgres") is skipping
    single = exp.select(exp.alias_(concat("a"), "n")).from_("t")
    assert spell_as_printed(single, "postgres") is single
    plain = exp.select(exp.alias_(concat("a", "b"), "n")).from_("t")
    for dialect in ("bigquery", "mysql", "oracle", "snowflake", "spark", "tsql", ""):
        assert spell_as_printed(plain, dialect) is plain


def test_queries_with_pipes_normalize_and_print_faithfully():
    for dialect in ("postgres", "duckdb"):
        assert normalize("SELECT a || b AS n FROM t", schema=SCHEMA, dialect=dialect) == "SELECT a || b AS n FROM t"
        assert normalize("SELECT a || ', ' || b AS n FROM t", schema=SCHEMA, dialect=dialect) == "SELECT a || ', ' || b AS n FROM t"
        # written grouping is kept: only directly nested concatenations are flattened upstream
        assert normalize("SELECT a || (b || c) AS n FROM t", schema=SCHEMA, dialect=dialect) == "SELECT a || (b || c) AS n FROM t"
    # PostgreSQL's CONCAT() keeps its own spelling, and BigQuery's keeps its
    assert normalize("SELECT CONCAT(a, b) AS n FROM t", schema=SCHEMA, dialect="postgres") == "SELECT CONCAT(a, b) AS n FROM t"
    assert normalize("SELECT a || b AS n FROM t", schema=SCHEMA, dialect="bigquery") == "SELECT CONCAT(a, b) AS n FROM t"


TARGETS = [
    ("SELECT a || b AS n FROM t", "SELECT a || b AS n FROM t"),
    ("SELECT COALESCE(a, '') || ', ' || COALESCE(b, '') AS n FROM t ORDER BY k LIMIT 10", "SELECT COALESCE(x.a, '') || ', ' || COALESCE(x.b, '') AS n FROM t AS x ORDER BY x.k LIMIT 10"),
    ("SELECT a || (b || c) AS n FROM t", "SELECT x.a || (x.b || x.c) AS n FROM t AS x"),
    ("SELECT 'store' || t.a AS id, SUM(t.k) AS s FROM t, u WHERE t.k = u.k GROUP BY t.a", "SELECT 'store' || t.a AS id, SUM(t.k) AS s FROM t JOIN u ON t.k = u.k GROUP BY t.a"),
    ("SELECT k FROM t WHERE a || b = 'xy'", "SELECT k FROM (SELECT * FROM t WHERE a || b = 'xy') AS d"),
]


@pytest.mark.parametrize("left, right", TARGETS)
@pytest.mark.parametrize("dialect", ["postgres", "duckdb"])
def test_pipe_queries_prove(left, right, dialect):
    assert proved(left, right, dialect)


ROWS = {"t": [("x", "y", "z", 1), ("x", None, "z", 2), (None, None, None, 3), ("", "y", "", 4), ("x", "y", "z", 1)]}
NEAR_MISSES = [
    # CONCAT() skips a NULL argument, || is NULL
    ("SELECT a || b AS n FROM t", "SELECT CONCAT(a, b) AS n FROM t"),
    ("SELECT a || b AS n FROM t", "SELECT COALESCE(a, '') || COALESCE(b, '') AS n FROM t"),
    # the order of the operands, and which operands
    ("SELECT a || b AS n FROM t", "SELECT b || a AS n FROM t"),
    ("SELECT a || b || c AS n FROM t", "SELECT a || c AS n FROM t"),
    ("SELECT a || b AS n FROM t", "SELECT a || c AS n FROM t"),
    # a NULL operand makes the filter reject the row
    ("SELECT k FROM t WHERE a || b IS NULL", "SELECT k FROM t WHERE a IS NULL"),
    ("SELECT k FROM t WHERE a || b = 'xy'", "SELECT k FROM t WHERE a = 'x'"),
]


@pytest.mark.parametrize("left, right", NEAR_MISSES)
def test_near_misses_do_not_prove(left, right):
    assert not proved(left, right)
    assert differ_on_duckdb(left, right, ROWS)


NORMALIZED_QUERIES = [
    "SELECT a || b AS n, k FROM t",
    "SELECT a || (b || c) AS n, (a || b) || c AS m FROM t",
    "SELECT COALESCE(a, '') || ', ' || COALESCE(b, '') AS n FROM t",
    "SELECT k FROM t WHERE a || b IS NULL OR 'p' || c = 'pz'",
    "SELECT a || '-' || b AS n, COUNT(*) AS c FROM t GROUP BY a || '-' || b",
    "SELECT t.k FROM t JOIN u ON t.a || t.b = u.a || u.b",
    "SELECT CONCAT(a, b) || c AS n, CONCAT(a || b, c) AS m FROM t",
]


def random_rows(rng):
    def table(size):
        texts = [None, "", "x", "y", "xy", "é"]
        rows = [(rng.choice(texts), rng.choice(texts), rng.choice(texts), rng.choice([None, 1, 2])) for _ in range(rng.randint(0, size))]
        return rows + rows[:2]  # with duplicate rows

    return {"t": table(6), "u": table(4)}


@pytest.mark.parametrize("sql", NORMALIZED_QUERIES)
def test_normalized_queries_give_the_same_rows(sql):
    out = normalize(sql, schema=SCHEMA, types=TYPES, dialect="postgres")
    rng = random.Random(len(sql))
    for rows in [{}, ROWS] + [random_rows(rng) for _ in range(12)]:
        assert not differ_on_duckdb(sql, out, rows)


# --- one-column grouping sets ---------------------------------------------------------------------


def test_a_one_column_grouping_set_is_a_parenthesized_column():
    sets = exp.GroupingSets(expressions=[exp.Tuple(expressions=[exp.column("a"), exp.column("b")]), exp.Tuple(expressions=[exp.column("a")]), exp.Tuple(expressions=[])])
    tree = exp.select("a", "b", "SUM(k) AS s").from_("t").group_by(sets)
    for dialect in ("postgres", "bigquery", "duckdb", "snowflake"):
        out = spell_as_printed(tree, dialect)
        kinds = [type(e) for e in out.args["group"].expressions[0].expressions]
        assert kinds == [exp.Tuple, exp.Paren, exp.Tuple]
        assert faithful_sql(out, dialect) == "SELECT a, b, SUM(k) AS s FROM t GROUP BY GROUPING SETS ((a, b), (a), ())"
    # a one-element tuple elsewhere is a row, not a grouping set
    values = sqlglot.parse_one("SELECT x FROM (VALUES (1), (2)) AS v (x)", read="postgres")
    assert spell_as_printed(values, "postgres") is values


def test_a_rollup_the_prover_cannot_split_still_prints_faithfully():
    # a window reads every grouping set's rows, so the rollup stays GROUPING SETS; it used to be refused as unprintable
    windowed = "SELECT a, b, SUM(k) AS s, RANK() OVER (PARTITION BY a ORDER BY SUM(k)) AS r FROM t GROUP BY ROLLUP (a, b)"
    out = normalize(windowed, schema=SCHEMA, types=TYPES, dialect="postgres")
    assert "GROUP BY GROUPING SETS ((a, b), (a), ())" in out
    result = prove_equivalent_algebraic(windowed, windowed.replace("ROLLUP (a, b)", "a, b"), schema=SCHEMA, types=TYPES, dialect="postgres", compare_names=False)
    assert not result.proven and "printed faithfully" not in result.reason
