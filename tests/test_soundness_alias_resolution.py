"""A bare name in ORDER BY or QUALIFY that is both a select alias and a source column.

BigQuery reads the alias in ORDER BY (inside expressions and windows too) and everywhere in QUALIFY; it reads the
source column in WHERE and in the select list. DuckDB reads the source column everywhere except a bare ORDER BY key.
The provers used to read the source column inside an ORDER BY expression and inside a QUALIFY window on every
dialect, which proved pairs BigQuery answers differently. Every BigQuery expectation below was run there on
2026-11-10 with inline values (zero bytes billed); the DuckDB ones run in ``test_duckdb_reads_the_source_column``.
"""

import pytest

pytest.importorskip("z3")

from sqlglot import exp, parse_one  # noqa: E402

from kumosql import clause_alias_reads as R  # noqa: E402
from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic  # noqa: E402
from kumosql.ast_utils import UnmodeledConstruct  # noqa: E402
from kumosql.equivalence import prove_equivalent  # noqa: E402
from kumosql.smt_equivalence import prove_equivalent_smt  # noqa: E402

SCHEMA = {"t": ["id", "a", "b"]}
TYPES = {"t": {"id": "INT64", "a": "INT64", "b": "INT64"}}

# Pairs BigQuery answers differently on t = (1, 1, 30), (2, 2, 20), (3, 3, 10): the left side reads the alias.
DIFFERENT = [
    # ORDER BY: a name that is one output's alias and another output's expression
    ("SELECT a AS b, b AS a FROM t ORDER BY a LIMIT 1", "SELECT a AS b, b AS a FROM t ORDER BY t.a LIMIT 1"),
    # ORDER BY: the alias inside an expression
    ("SELECT b AS a FROM t ORDER BY a + 1 LIMIT 1", "SELECT b AS a FROM t ORDER BY t.a + 1 LIMIT 1"),
    ("SELECT id, b AS a FROM t ORDER BY -a LIMIT 1", "SELECT id, b AS a FROM t ORDER BY -t.a LIMIT 1"),
    # ORDER BY: a window that reads the alias
    ("SELECT id, b AS a FROM t ORDER BY RANK() OVER (ORDER BY a) LIMIT 1", "SELECT id, b AS a FROM t ORDER BY RANK() OVER (ORDER BY t.a) LIMIT 1"),
    # QUALIFY: the alias as a window's ORDER BY, PARTITION BY and argument
    ("SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1", "SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY t.a) = 1"),
    ("SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY a) = 2", "SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY t.a) = 2"),
    ("SELECT id, b AS a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY a DESC) = 1", "SELECT id, b AS a FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY t.a DESC) = 1"),
    ("SELECT id, b AS a FROM t QUALIFY a = MAX(a) OVER ()", "SELECT id, b AS a FROM t QUALIFY a = MAX(t.a) OVER ()"),
    ("SELECT -a AS a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1", "SELECT -a AS a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY t.a) = 1"),
    ("SELECT id, a + 10 AS a FROM t QUALIFY SUM(a) OVER () > 30", "SELECT id, a + 10 AS a FROM t QUALIFY SUM(t.a) OVER () > 30"),
    # QUALIFY: the alias outside any window
    ("SELECT id, b AS a FROM t QUALIFY a = 2 AND COUNT(*) OVER () > 0", "SELECT id, b AS a FROM t WHERE a = 2 QUALIFY COUNT(*) OVER () > 0"),
    ("SELECT a AS x, b AS a FROM t QUALIFY a = 2 AND COUNT(*) OVER () > 0", "SELECT a AS x, b AS a FROM t QUALIFY t.a = 2 AND COUNT(*) OVER () > 0"),
    # the select list reads the source column, so its window is not the QUALIFY window of the same text
    ("SELECT id, b AS a, RANK() OVER (ORDER BY a) AS r FROM t", "SELECT id, b AS a, RANK() OVER (ORDER BY b) AS r FROM t"),
]


@pytest.mark.parametrize("left,right", DIFFERENT)
@pytest.mark.parametrize("types", [TYPES, None], ids=["typed", "untyped"])
@pytest.mark.parametrize("dialect", ["bigquery", "duckdb"])
def test_no_prover_calls_them_equivalent(left, right, types, dialect):
    for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
        for a, b in ((left, right), (right, left)):
            result = prove(a, b, schema=SCHEMA, types=types, dialect=dialect)
            assert not result.proven, (prove.__name__, result.reason)
    assert not prove_equivalent(left, right).proven


# What BigQuery does read: equivalent there, and the algebraic prover proves them.
EQUIVALENT_BIGQUERY = [
    ("SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1", "SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY b) = 1"),
    ("SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY a) = 2", "SELECT b AS a FROM t QUALIFY COUNT(*) OVER (PARTITION BY b) = 2"),
    ("SELECT id, b AS a FROM t QUALIFY a = MAX(a) OVER ()", "SELECT id, b AS a FROM t QUALIFY b = MAX(b) OVER ()"),
    ("SELECT id, a + 10 AS a FROM t QUALIFY SUM(a) OVER () > 30", "SELECT id, a + 10 AS a FROM t QUALIFY SUM(t.a + 10) OVER () > 30"),
    ("SELECT id, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS n FROM t QUALIFY n = 1", "SELECT id, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS n FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1"),
]


@pytest.mark.parametrize("left,right", EQUIVALENT_BIGQUERY)
def test_bigquery_alias_reads_are_proven(left, right):
    assert prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="bigquery").proven


def test_bare_order_by_key_is_the_output_of_that_name():
    # both engines read a bare ORDER BY key as the select alias
    for dialect in ("bigquery", "duckdb"):
        assert prove_equivalent_smt("SELECT id, b AS a FROM t ORDER BY a LIMIT 1", "SELECT id, b AS a FROM t ORDER BY 2 LIMIT 1", schema=SCHEMA, dialect=dialect).proven
    assert prove_equivalent_smt("SELECT a AS b, b AS a FROM t ORDER BY a LIMIT 1", "SELECT a AS b, b AS a FROM t ORDER BY 2 LIMIT 1", schema=SCHEMA).proven


def test_an_unaliased_expression_is_not_an_output_named_after_the_column_it_reads():
    # ``CAST(a AS FLOAT64)`` and ``-a`` have no name ORDER BY can use, so ``ORDER BY a`` is the column and nothing is
    # ambiguous. The guard above once took such an entry for a second output called ``a`` and refused the pair
    # (VeriEQL Calcite-397 pair 8, ``ORDER BY deptno`` beside an unaliased ``CAST(deptno AS DOUBLE)``).
    for dialect in ("bigquery", "duckdb"):
        assert prove_equivalent_smt(
            "SELECT a, CAST(a AS FLOAT64) FROM t ORDER BY a LIMIT 1", "SELECT a, CAST(a AS FLOAT64) FROM t ORDER BY 1 LIMIT 1", schema=SCHEMA, types=TYPES, dialect=dialect
        ).proven, dialect
        assert prove_equivalent_smt("SELECT id, -a FROM t ORDER BY a + 1 LIMIT 1", "SELECT id, -a FROM t ORDER BY t.a + 1 LIMIT 1", schema=SCHEMA, types=TYPES, dialect=dialect).proven, dialect
        # the column is still the column: ordering by it is not ordering by the expression that reads it
        assert not prove_equivalent_smt("SELECT id, -a FROM t ORDER BY a LIMIT 1", "SELECT id, -a FROM t ORDER BY 2 LIMIT 1", schema=SCHEMA, types=TYPES, dialect=dialect).proven, dialect


# sqlglot gives an unaliased CAST the name of the column it reads, and ``_split_limit`` matched ORDER BY keys by that
# name, so ``ORDER BY b`` was read as ordering by the CAST (a false proof from 2026-10-02 until 2026-11-10). On b = 9, 10, 100 BigQuery returns the row with 9 for the left side of each pair and
# the row with 10 for the right side ('10' sorts before '9'); run there on 2026-11-10, zero bytes billed.
CAST_IS_NOT_THE_COLUMN = [
    ("SELECT id, CAST(b AS STRING) FROM t ORDER BY b LIMIT 1", "SELECT id, CAST(b AS STRING) FROM t ORDER BY 2 LIMIT 1"),
    ("SELECT id, CAST(b AS STRING) FROM t ORDER BY b LIMIT 1", "SELECT id, CAST(b AS STRING) FROM t ORDER BY CAST(b AS STRING) LIMIT 1"),
    ("SELECT id, SAFE_CAST(b AS STRING) FROM t ORDER BY b LIMIT 1", "SELECT id, SAFE_CAST(b AS STRING) FROM t ORDER BY 2 LIMIT 1"),
    ("SELECT id, (CAST(b AS STRING)) FROM t ORDER BY b LIMIT 1", "SELECT id, (CAST(b AS STRING)) FROM t ORDER BY 2 LIMIT 1"),
]


@pytest.mark.parametrize("left,right", CAST_IS_NOT_THE_COLUMN)
@pytest.mark.parametrize("types", [TYPES, None], ids=["typed", "untyped"])
def test_order_by_a_column_is_not_order_by_an_unaliased_cast_of_it(left, right, types):
    assert not prove_equivalent(left, right).proven
    assert not prove_equivalent_smt(left, right, schema=SCHEMA, types=types, dialect="bigquery").proven
    assert not prove_equivalent_algebraic(left, right, schema=SCHEMA, types=types, dialect="bigquery").proven


def test_other_dialects_refuse_a_name_read_both_ways():
    for sql in (
        "SELECT b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1",
        "SELECT id, b AS a FROM t QUALIFY a = 2 AND COUNT(*) OVER () > 0",
        "SELECT id, b AS a FROM t ORDER BY RANK() OVER (ORDER BY a) LIMIT 1",
    ):
        with pytest.raises(UnmodeledConstruct):
            normalize(sql, schema=SCHEMA, dialect="duckdb")


def test_an_alias_no_source_column_has_is_read_on_every_dialect():
    # ``n`` can only be the output: the usual "keep the first row of each group" spelling
    left = "SELECT id, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS n FROM t QUALIFY n = 1"
    right = "SELECT id, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS n FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1"
    assert prove_equivalent_algebraic(left, right, schema=SCHEMA, dialect="duckdb").proven
    # with no declared columns the source may have an ``n``: refused
    with pytest.raises(UnmodeledConstruct):
        normalize(left, schema=None, dialect="duckdb")


def test_duckdb_reads_the_source_column():
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (id INT, a INT, b INT)")
    connection.execute("INSERT INTO t VALUES (1, 1, 30), (2, 2, 20), (3, 3, 10)")

    def ids(sql):
        return [row[0] for row in connection.execute(sql).fetchall()]

    assert ids("SELECT id, b AS a FROM t QUALIFY RANK() OVER (ORDER BY a) = 1") == [1]  # BigQuery: 3
    assert ids("SELECT id, b AS a FROM t QUALIFY a = 2 AND COUNT(*) OVER () > 0") == [2]  # BigQuery: none
    assert ids("SELECT id, b AS a FROM t ORDER BY a + 0 LIMIT 1") == [1]  # BigQuery: 3
    assert ids("SELECT id, b AS a FROM t ORDER BY a LIMIT 1") == [3]  # BigQuery: 3


def test_shadowing_aliases():
    def names(sql):
        return {name: (value.sql() if value is not None else None) for name, value in R.shadowing_aliases(parse_one(sql, read="bigquery")).items()}

    assert names("SELECT b AS a, a AS a2, id FROM t") == {"a": "b", "a2": "a"}
    assert names("SELECT a AS a, t.b AS b FROM t") == {}  # the alias is the column of that name
    assert names("SELECT u.a AS a FROM t JOIN u ON t.id = u.id") == {"a": "u.a"}  # one of two columns called a
    assert names("SELECT b AS a, id AS a FROM t") == {"a": None}


def test_may_be_source_column():
    def may(sql, name, schema=SCHEMA):
        return R.may_be_source_column(parse_one(sql, read="bigquery").find(exp.Select), name, schema)

    assert may("SELECT 1 FROM t", "a") and not may("SELECT 1 FROM t", "n")
    assert may("SELECT 1 FROM unknown_table", "n")
    assert may("SELECT 1 FROM t", "n", schema=None)
    assert not may("SELECT 1 FROM (SELECT id, a AS x FROM t) AS d", "a") and may("SELECT 1 FROM (SELECT id, a AS x FROM t) AS d", "x")
    assert may("SELECT 1 FROM (SELECT * FROM t) AS d", "n")
    assert not may("WITH c AS (SELECT id FROM t) SELECT 1 FROM c", "a")
    assert may("SELECT 1 FROM t JOIN (SELECT id AS n FROM t) AS d ON t.id = d.n", "n")
