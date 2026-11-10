"""A derived table kept whole is compared with the prover itself, in the dialect of the proof it belongs to.

``smt_equivalence._unify_opaque`` proves two differently written derived tables equal so they can share one identity.
It used to read both bodies as BigQuery whatever dialect the caller asked for. Two things went wrong under MySQL,
PostgreSQL and DuckDB:

* a proof was lost: ``1.00`` is an exact decimal there and a FLOAT64 in BigQuery, so ``p * (1 - d)`` and
  ``p * (1.00 - d)`` stopped matching once NUMERIC arithmetic was modelled for BigQuery (SQLSolver's TPC-H pair 20);
* a pair was proved that is not equivalent: ``0.1`` and ``0.10000000000000000001`` are one FLOAT64 in BigQuery and
  two different decimals elsewhere.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

from kumosql import prove_equivalent_smt
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, _Compiler

EXACT_DECIMAL_DIALECTS = ["mysql", "postgres", "duckdb"]
PROVERS = [prove_equivalent_smt, prove_equivalent_algebraic]

ORDERS = {"s": ["k", "name"], "li": ["k", "p", "d"]}
ORDER_TYPES = {"s": {"k": "INT", "name": "VARCHAR(20)"}, "li": {"k": "INT", "p": "DECIMAL(15, 2)", "d": "DECIMAL(15, 2)"}}
REVENUE = "SELECT s.k, r.total FROM s JOIN (SELECT k, SUM(p * ({one} - d)) AS total FROM li GROUP BY k) AS r ON s.k = r.k"

T = {"t": ["k", "x"]}
T_TYPES = {"t": {"k": "INT", "x": "DECIMAL(30, 25)"}}
SHORT, LONG = "0.1", "0.10000000000000000001"
WRAPPED = {
    "grouped": "SELECT q.k, q.c FROM (SELECT k, COUNT(*) AS c FROM t WHERE x = {literal} GROUP BY k) AS q",
    "grouped-and-joined": "SELECT a.k, q.c FROM t AS a JOIN (SELECT k, COUNT(*) AS c FROM t WHERE x = {literal} GROUP BY k) AS q ON a.k = q.k",
    "distinct-and-joined": "SELECT a.k FROM t AS a JOIN (SELECT DISTINCT k FROM t WHERE x = {literal}) AS q ON a.k = q.k",
    "under-a-scalar-subquery": "SELECT a.k FROM t AS a WHERE a.x = (SELECT MAX(x) FROM (SELECT k, SUM(x) AS x FROM t WHERE x = {literal} GROUP BY k) AS q)",
}


@pytest.mark.parametrize("dialect", EXACT_DECIMAL_DIALECTS)
@pytest.mark.parametrize("prove", PROVERS, ids=lambda f: f.__name__)
def test_an_exact_decimal_literal_inside_a_derived_table_is_read_as_the_dialect_reads_it(prove, dialect):
    result = prove(REVENUE.format(one="1"), REVENUE.format(one="1.00"), schema=ORDERS, types=ORDER_TYPES, dialect=dialect, compare_names=False, exact_arithmetic=True)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


@pytest.mark.parametrize("prove", PROVERS, ids=lambda f: f.__name__)
def test_bigquery_still_reads_a_decimal_literal_as_float64(prove):
    """``1.00 - d`` converts a NUMERIC ``d`` to FLOAT64 in BigQuery, so the two sums can differ in the last digits."""

    result = prove(REVENUE.format(one="1"), REVENUE.format(one="1.00"), schema=ORDERS, types=ORDER_TYPES, dialect="bigquery", compare_names=False, exact_arithmetic=True)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT


@pytest.mark.parametrize("dialect", EXACT_DECIMAL_DIALECTS)
@pytest.mark.parametrize("shape", sorted(WRAPPED))
@pytest.mark.parametrize("prove", PROVERS, ids=lambda f: f.__name__)
def test_two_decimals_that_are_one_float64_are_not_one_value_outside_bigquery(prove, shape, dialect):
    left, right = WRAPPED[shape].format(literal=SHORT), WRAPPED[shape].format(literal=LONG)
    result = prove(left, right, schema=T, types=T_TYPES, dialect=dialect, compare_names=False)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT, f"false proof: {result.reason}"


def test_the_two_decimals_really_select_different_rows():
    """The witness for the test above: one row whose ``x`` is exactly 0.1."""

    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("CREATE TABLE t (k INTEGER, x DECIMAL(30, 25))")
        connection.execute("INSERT INTO t VALUES (1, 0.1)")
        shape = WRAPPED["grouped"]
        assert connection.execute(shape.format(literal=SHORT)).fetchall() == [(1, 1)]
        assert connection.execute(shape.format(literal=LONG)).fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("dialect", ["bigquery", *EXACT_DECIMAL_DIALECTS])
def test_reordered_derived_tables_still_share_one_identity(dialect):
    shape = "SELECT s.k, r.c FROM s JOIN (SELECT k, COUNT(*) AS c FROM li WHERE {filter} GROUP BY k) AS r ON s.k = r.k"
    result = prove_equivalent_smt(shape.format(filter="p = 1 AND d = 2"), shape.format(filter="d = 2 AND p = 1"), schema=ORDERS, dialect=dialect)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason


BODIES = [
    ("mysql", "SELECT e.sal AS c0 FROM emp AS e FULL JOIN bonus AS b ON e.ename = b.ename"),
    ("mysql", "SELECT k AS c0, SUM(p) DIV COUNT(p) AS c1 FROM li GROUP BY k"),
    ("mysql", "SELECT k AS c0, SUM(p * (1.00 - d)) AS c1 FROM li GROUP BY k"),
    ("mysql", "SELECT CAST(a AS CHAR) AS c0 FROM t WHERE a <=> b"),
    ("postgres", "SELECT a AS c0 FROM t WHERE s = 'it''s' ORDER BY a LIMIT 3"),
    ("duckdb", "SELECT a AS c0 FROM t WHERE x = 0.10000000000000000001"),
    ("sqlite", "SELECT a || b AS c0 FROM t"),
]


@pytest.mark.parametrize("dialect, body", BODIES)
def test_a_body_is_handed_to_the_nested_proof_in_a_spelling_its_dialect_reads_back_unchanged(dialect, body):
    import sqlglot

    tree = sqlglot.parse_one(body, read=dialect)
    text = tree.sql(dialect="bigquery", normalize_functions="upper")
    own = _Compiler(None, False, dialect)._own_dialect_text(tree, text)
    assert own is not None
    assert sqlglot.parse_one(own, read=dialect).sql(dialect="bigquery", normalize_functions="upper") == text
    assert _Compiler(None, False, "bigquery")._own_dialect_text(tree, text) == text


def test_mysql_keeps_a_full_join_and_an_integer_division_as_written():
    """MySQL's writer turns FULL JOIN into a union of two joins (the benchmarks read Calcite's SQL as MySQL, FULL JOIN
    included) and ``a DIV b`` into ``CAST(a / b AS SIGNED)``, which rounds where DIV truncates."""

    import sqlglot

    compiler = _Compiler(None, False, "mysql")
    tree = sqlglot.parse_one(BODIES[0][1], read="mysql")
    own = compiler._own_dialect_text(tree, tree.sql(dialect="bigquery", normalize_functions="upper"))
    assert "FULL JOIN" in own and "UNION" not in own
    tree = sqlglot.parse_one(BODIES[1][1], read="mysql")
    own = compiler._own_dialect_text(tree, tree.sql(dialect="bigquery", normalize_functions="upper"))
    assert " DIV " in own and "CAST" not in own


def test_a_body_with_no_faithful_spelling_is_never_re_proved(monkeypatch):
    """Its text stays its only identity: handing the nested proof another reading would be a guess. The refusal is a
    lost unification, never an exception."""

    from kumosql import smt_equivalence
    from kumosql.ast_utils import LossySql

    shape = "SELECT s.k, r.c FROM s JOIN (SELECT k, COUNT(*) AS c FROM li WHERE {filter} GROUP BY k) AS r ON s.k = r.k"
    pair = shape.format(filter="p = 1 AND d = 2"), shape.format(filter="d = 2 AND p = 1")
    assert prove_equivalent_smt(*pair, schema=ORDERS, dialect="mysql").status is SmtStatus.PROVEN_EQUIVALENT

    def lossy(tree, dialect):
        raise LossySql("no spelling reads back the same")

    monkeypatch.setattr(smt_equivalence, "faithful_sql", lossy)
    assert prove_equivalent_smt(*pair, schema=ORDERS, dialect="mysql").status is SmtStatus.NOT_PROVEN
    assert prove_equivalent_smt(*pair, schema=ORDERS, dialect="bigquery").status is SmtStatus.PROVEN_EQUIVALENT  # BigQuery bodies are handed over as they are


@pytest.mark.canary  # one case of the family, kept in the gate; the floor itself stays in tests/test_sqlsolver_benchmarks.py
def test_sqlsolver_tpch_pair_20_is_proved():
    """The pair that lost its proof on 2026-10-04 while the scoreboard still said 22/22 (the TPC-H floor is in the slow tier)."""

    tools = Path(__file__).resolve().parent.parent / "tools"
    spec = importlib.util.spec_from_file_location("sqlsolver_bench_for_pair_20", tools / "sqlsolver_bench.py")
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench
    try:
        spec.loader.exec_module(bench)
        pairs_file, schema_file = bench.SUITES["tpch"]
        left, right = bench.load_pairs(bench.FIXTURES / pairs_file)[20]
        result = bench.prove_result(left, right, bench.load_schema(bench.FIXTURES / schema_file))
    finally:
        sys.modules.pop(spec.name, None)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT, result.reason
