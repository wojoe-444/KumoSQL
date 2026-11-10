"""The proof re-check adapters of the cross-dialect, rewrite and BigQuery evals (tools/recheck/dialect_rewrites.py)
and the tie probe (tools/recheck/ties.py).

Each adapter imports, lists its pairs, and turns one tiny proven pair into a Case (or a verdict). Evals whose data
is not committed (SQL-RewriteBench, WeTune, ClickBench, LLM-R2) run on a few lines of stand-in data written to a
temporary folder. Every test builds its own folders, connections and cases; the engine extensions the adapters
install are removed again after each test.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
import sys

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("yaml")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "src"))

from recheck import dialect_rewrites as dr  # noqa: E402
from recheck import engine, ties  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

pytestmark = pytest.mark.eval

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

EXTERNAL = ("KUMOSQL_BENCH_DIR", "KUMOSQL_RECHECK_REWRITEBENCH", "KUMOSQL_RECHECK_TPCDS_KIT", "KUMOSQL_RECHECK_WETUNE", "KUMOSQL_RECHECK_CLICKBENCH")
NAMES = {
    "dlbench", "dlbench-target", "llm-sql-solver-relaxed", "llm-sql-solver-negatives", "llm-sql-solver-uncounted",
    "sql-rewritebench", "wetune-issues", "wetune-issues-mysql-ci", "clickbench-rewrites", "llm-r2-scale", "llm-r2-scale-train",
    "spider2-bigquery",
}


@pytest.fixture(autouse=True)
def clean_engine(monkeypatch):
    """No data folders from the environment; the engine's own Runner, Generator and literal_pool are restored."""

    for name in EXTERNAL:
        monkeypatch.delenv(name, raising=False)
    yield
    dr.uninstall()


def _verdict(case: Case, budget: int = 60) -> dict:
    return engine.recheck(case, budget=budget, seconds=30)


def test_the_adapters_import_and_register():
    assert set(dr.ADAPTERS) == NAMES
    for name, adapter in dr.ADAPTERS.items():
        assert adapter.name == name


def test_adapters_whose_data_is_not_committed_list_nothing_without_it():
    for name in ("sql-rewritebench", "wetune-issues", "wetune-issues-mysql-ci", "clickbench-rewrites", "llm-r2-scale", "llm-r2-scale-train"):
        assert dr.ADAPTERS[name].items() == []


def test_listed_pairs_are_the_evals_pairs():
    import dlbench_bench
    import llm_sql_solver_bench
    import spider2_bench

    assert len(dr.ADAPTERS["dlbench"].items()) == len(dr.ADAPTERS["dlbench-target"].items()) == len(dlbench_bench.load_pairs())
    cases = llm_sql_solver_bench.load_cases()
    assert len(dr.ADAPTERS["llm-sql-solver-relaxed"].items()) == sum(c.suite == "relaxed" for c in cases)
    assert len(dr.ADAPTERS["llm-sql-solver-negatives"].items()) == sum(c.suite == "negatives" for c in cases)
    assert len(dr.ADAPTERS["llm-sql-solver-uncounted"].items()) == len(cases)
    rules = len(spider2_bench.cov.CLEANUP_RULES) + 1  # the cleanup rules and the formatter
    assert len(dr.ADAPTERS["spider2-bigquery"].items()) == len(spider2_bench.cases()) * rules
    for adapter in dr.ADAPTERS.values():
        for item in adapter.items()[:3]:
            assert item["pair"]


# --- DLBench -------------------------------------------------------------------------------------


def _pair(adapter: str, name: str) -> dict:
    return next(i for i in dr.ADAPTERS[adapter].items() if i["pair"] == name)


def test_dlbench_runs_a_sqlite_pair_in_sqlite_as_the_prover_read_it():
    case = dr.ADAPTERS["dlbench"].case(_pair("dlbench", "BIRDTrans/clickhouse/2"))
    assert case is not None
    assert case.meta["engines"] == ("sqlite", "sqlite")
    assert case.left.upper().startswith("SELECT FULLNAME FROM CONFERENCE") and "LENGTH" in case.left.upper()
    assert _verdict(case)["verdict"] == "survived"


def test_dlbench_target_runs_the_translation_in_duckdb_with_the_targets_meaning():
    case = dr.ADAPTERS["dlbench-target"].case(_pair("dlbench-target", "BIRDTrans/clickhouse/2"))
    assert case is not None and case.meta["engines"] == ("sqlite", "duckdb")
    assert "strlen" in case.right.lower()  # ClickHouse's length counts bytes
    # SQLite's LENGTH counts characters, ClickHouse's counts bytes: a dialect gap, and the search finds it
    record = _verdict(case, budget=600)
    assert record["verdict"] == "differs"
    assert record["witness"]["left"] != record["witness"]["right"]


def test_dlbench_pairs_the_eval_does_not_prove_give_no_case():
    unproven = next(i for i in dr.ADAPTERS["dlbench"].items() if i["target_dbms"] == "mysql" and i["source_dbms"] == "sqlite")
    assert dr.ADAPTERS["dlbench"].case(unproven) is None  # a MySQL string comparison is a dialect gap


def test_hybrid_runner_runs_each_side_on_its_own_engine():
    tables = {"t": Table("t", [Column("a", "text")])}
    case = Case("test", "length", "SELECT LENGTH(a) FROM t", "SELECT strlen(a) FROM t", tables,
                meta={"engines": ("sqlite", "duckdb"), "sqlite_tables": {"t": [("a", "TEXT")]}})
    dr.install()
    runner = engine.Runner(case)
    try:
        assert isinstance(runner, dr.HybridRunner)
        outcome = engine.compare(runner, {"t": [("é",)]})
        assert outcome.kind == "differs" and outcome.left == [(1,)] and outcome.right == [(2,)]
        assert engine.compare(runner, {"t": [("e",)]}).kind == "same"
    finally:
        runner.close()
    assert _verdict(case, budget=400)["verdict"] == "differs"


def test_typed_division_follows_the_sources_dialect():
    sql = dr.to_duckdb("SELECT a / b FROM t", "postgres")
    assert "kumo_tdiv(" in sql.lower()
    import duckdb

    db = duckdb.connect()
    for statement in dr.MACROS:
        db.execute(statement)
    db.execute("CREATE TABLE t (a BIGINT, b BIGINT)")
    db.execute("INSERT INTO t VALUES (7, 2)")
    assert db.execute(sql).fetchall() == [(3,)]  # PostgreSQL truncates integer division
    lite = dr.to_duckdb("SELECT a / b FROM t", "sqlite")  # SQLite truncates too, and a zero divisor gives NULL
    assert "kumo_tdiv_null(" in lite.lower() and db.execute(lite).fetchall() == [(3,)]


def test_catalog_types_become_engine_columns():
    column = dr.engine_column
    assert column("a", "UBIGINT(20)").ddl_type() == "UBIGINT" and column("a", "UINT(10)").ddl_type() == "UINTEGER"  # MySQL unsigned
    assert column("a", "numeric", precision=7, scale=2).ddl_type() == "DECIMAL(7,2)"
    assert (column("a", "character varying").kind, column("a", "timestamp without time zone").kind, column("a", "int(11)").kind) == ("text", "timestamp", "int")
    assert engine.fits(column("a", "UINT(10)"), 4294967295) and not engine.fits(column("a", "UINT(10)"), -1)
    assert not engine.fits(column("a", "UBIGINT"), 2**64) and engine.fits(column("a", "UBIGINT"), 2**63)


# --- LLM-SQL-Solver ------------------------------------------------------------------------------


@pytest.mark.parametrize("name, pair", [
    ("llm-sql-solver-relaxed", "relaxed-041"),
    ("llm-sql-solver-negatives", "negatives-063"),
    ("llm-sql-solver-uncounted", "negatives-060"),
])
def test_llm_sql_solver_runs_a_counted_pair_in_sqlite(name, pair):
    adapter = dr.ADAPTERS[name]
    case = adapter.case(next(i for i in adapter.items() if i["pair"] == pair))
    assert case is not None and case.meta["engines"] == ("sqlite", "sqlite") and case.dialect == "sqlite"
    assert case.meta["mixed_types"] is (name == "llm-sql-solver-uncounted")
    assert _verdict(case)["verdict"] == "survived"


def test_llm_sql_solver_keeps_counted_and_uncounted_proofs_apart():
    counted = dr.ADAPTERS["llm-sql-solver-negatives"]
    mixed = next(i for i in dr.ADAPTERS["llm-sql-solver-uncounted"].items() if i["pair"] == "negatives-060")
    assert counted.case(mixed) is None  # proved, but a text column against a number: the eval leaves it uncounted


# --- Spider 2.0 ----------------------------------------------------------------------------------


def test_spider2_proven_rewrite_becomes_a_case_and_an_unchanged_one_does_not():
    adapter = dr.ADAPTERS["spider2-bigquery"]
    case = adapter.case({"pair": "bq096#inline_single_use_ctes", "id": "bq096", "rule": "inline_single_use_ctes"})
    assert case is not None and case.dialect == "bigquery" and case.meta["rule"] == "inline_single_use_ctes"
    assert case.left != case.right
    assert _verdict(case, budget=40)["verdict"] in ("survived", "unrunnable")
    rules = [r for r in dr._cleanup_rules() if r != "inline_single_use_ctes"]
    unchanged = [r for r in rules if adapter.case({"pair": f"bq096#{r}", "id": "bq096", "rule": r}) is None]
    assert unchanged  # most rules leave this query alone: not a rewrite, so no pair to re-check


# --- stand-in data for the evals whose data is not committed -------------------------------------


def test_wetune_developer_and_kumosql_rewrites(tmp_path, monkeypatch):
    root = tmp_path / "WeTune-code" / "wtune_data"
    (root / "issues").mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "issues" / "issues").write_text(
        "1\tdemo\tdistinct\tabc\tSELECT DISTINCT id FROM users\tSELECT id FROM users\n", encoding="utf-8")
    (root / "schemas" / "demo.base.schema.sql").write_text(
        "CREATE TABLE users (id integer NOT NULL PRIMARY KEY, name varchar(20));\n", encoding="utf-8")
    monkeypatch.setenv("KUMOSQL_RECHECK_WETUNE", str(tmp_path / "WeTune-code"))
    adapter = dr.ADAPTERS["wetune-issues"]
    assert [i["pair"] for i in adapter.items()] == ["1:developer", "1:kumosql"]
    for item in adapter.items():
        case = adapter.case(item)
        assert case is not None, item
        assert case.tables["users"].keys == [("id",)] and case.tables["users"].columns[0].not_null
        assert _verdict(case, budget=100)["verdict"] == "survived"
    assert dr.ADAPTERS["wetune-issues-mysql-ci"].case({"pair": "1:developer", "id": 1, "kind": "developer"}) is None  # not a MySQL schema


def test_clickbench_rewrite_of_one_query(tmp_path, monkeypatch):
    root = tmp_path / "ClickBench" / "postgresql"
    root.mkdir(parents=True)
    (root / "create.sql").write_text("CREATE TABLE hits (\n    CounterID INTEGER NOT NULL,\n    URL TEXT NOT NULL\n);\n", encoding="utf-8")
    (root / "queries.sql").write_text(
        "SELECT COUNT(*) FROM hits WHERE CounterID IS NULL OR CounterID IS NOT NULL;\nSELECT URL FROM hits ORDER BY URL LIMIT 3;\n", encoding="utf-8")
    monkeypatch.setenv("KUMOSQL_RECHECK_CLICKBENCH", str(tmp_path / "ClickBench"))
    adapter = dr.ADAPTERS["clickbench-rewrites"]
    assert [i["pair"] for i in adapter.items()] == ["Q1", "Q2"]
    case = adapter.case({"pair": "Q1", "number": 1})
    assert case is not None and case.tables["hits"].columns[0].not_null
    assert _verdict(case, budget=100)["verdict"] == "survived"
    assert adapter.case({"pair": "Q2", "number": 2}) is None  # nothing to rewrite


def _rewritebench(tmp_path):
    bench = tmp_path / "benchmark"
    case = bench / "benchmark" / "benchmark_inputs packages" / "cases" / "EQUIV-004"
    (case / "schema").mkdir(parents=True)
    (case / "sql").mkdir()
    (case / "schema" / "schema_profile.yaml").write_text(
        "database: dsb\ntables:\n- name: item\n  primary_key: [i_item_sk]\n  columns:\n"
        "  - {column_name: i_item_sk, data_type: integer, is_nullable: 'NO', ordinal_position: 1}\n"
        "  - {column_name: i_color, data_type: character varying, is_nullable: 'YES', ordinal_position: 2}\n", encoding="utf-8")
    (case / "sql" / "benchmark_input.sql").write_text(
        "WITH unused AS (SELECT i_color FROM item) SELECT i_item_sk FROM item WHERE i_color = 'red'", encoding="utf-8")
    data = tmp_path / "bench"
    (data / "dsb" / "code" / "tools").mkdir(parents=True)
    (data / "dsb" / "code" / "tools" / "tpcds.sql").write_text(
        "create table item (i_item_sk integer not null, i_color char(20), primary key (i_item_sk));\n"
        "create table store (s_store_sk integer not null, s_name varchar(50), primary key (s_store_sk));\n", encoding="utf-8")
    return bench, data


def test_sql_rewritebench_proven_rewrite(tmp_path, monkeypatch):
    bench, data = _rewritebench(tmp_path)
    monkeypatch.setenv("KUMOSQL_RECHECK_REWRITEBENCH", str(bench))
    monkeypatch.setenv("KUMOSQL_BENCH_DIR", str(data))
    adapter = dr.ADAPTERS["sql-rewritebench"]
    assert [i["pair"] for i in adapter.items()] == ["EQUIV-004"]
    case = adapter.case(adapter.items()[0])
    assert case is not None and case.tables["item"].keys == [("i_item_sk",)]
    assert "unused" not in case.right.lower() and "unused" in case.left.lower()
    assert _verdict(case, budget=100)["verdict"] == "survived"


def test_llm_r2_proven_rewrite(tmp_path, monkeypatch):
    import benchmark_corpora as corpora

    queries = tmp_path / "llm-r2" / "data" / "data_llmr2" / "queries"
    queries.mkdir(parents=True)
    (queries / "queries_tpch_test.csv").write_text(
        'id,original_sql\n1,"WITH x AS (SELECT n_name, n_nationkey FROM nation) SELECT n_name FROM x WHERE n_nationkey > 3"\n', encoding="utf-8")
    (queries / "queries_dsb_test.csv").write_text("id,original_sql\n", encoding="utf-8")
    (tmp_path / "dsb" / "code" / "tools").mkdir(parents=True)
    (tmp_path / "dsb" / "code" / "tools" / "tpcds.sql").write_text("create table item (i_item_sk integer not null);\n", encoding="utf-8")
    monkeypatch.setenv("KUMOSQL_BENCH_DIR", str(tmp_path))
    monkeypatch.setattr(corpora, "BENCH_DIR", tmp_path)
    adapter = dr.ADAPTERS["llm-r2-scale"]
    items = adapter.items()
    assert {i["pair"] for i in items} == {"llm-r2/tpch/test/1#rules", "llm-r2/tpch/test/1#lift_subqueries"}
    case = adapter.case(next(i for i in items if i["transformation"] == "rules"))
    assert case is not None and case.held_out
    assert "WITH" in case.left.upper() and "WITH" not in case.right.upper()
    assert case.tables["nation"].columns[0].kind == "int"
    assert _verdict(case, budget=100)["verdict"] == "survived"
    assert adapter.case(next(i for i in items if i["transformation"] == "lift_subqueries")) is None  # nothing to lift


# --- ties ----------------------------------------------------------------------------------------


def _tie_case(left: str, right: str) -> Case:
    return Case("test", "ties", left, right, {"t": Table("t", [Column("a", "int"), Column("b", "text")])})


def test_a_tie_under_limit_is_found_and_distinct_counts_are_not():
    case = _tie_case("SELECT b FROM t GROUP BY b ORDER BY COUNT(a) DESC LIMIT 1", "SELECT b FROM t GROUP BY b ORDER BY COUNT(a) DESC, b DESC LIMIT 1")
    runner = engine.Runner(case)
    try:
        assert ties.tie_dependent(runner, {"t": [(None, "x"), (None, "y")]})  # two groups at 0: either may come back
        assert not ties.tie_dependent(runner, {"t": [(1, "x"), (None, "y")]})
        assert not ties.tie_dependent(runner, {"t": [(1, "x"), (2, "x"), (None, "y")]})
    finally:
        runner.close()


def test_a_difference_that_is_only_a_tie_is_not_a_false_proof():
    case = _tie_case("SELECT b FROM t GROUP BY b ORDER BY COUNT(a) DESC LIMIT 1", "SELECT b FROM t GROUP BY b ORDER BY COUNT(a) DESC, b DESC LIMIT 1")
    record = engine.recheck(case, budget=400, seconds=30)
    assert record["verdict"] == "survived" and record["notes"]["nondeterministic"] > 0


def test_a_difference_without_ties_still_counts():
    case = _tie_case("SELECT b FROM t ORDER BY a DESC LIMIT 1", "SELECT b FROM t ORDER BY a ASC LIMIT 1")
    assert engine.recheck(case, budget=400, seconds=30)["verdict"] == "differs"


def test_the_tie_probe_reads_what_it_can_and_skips_the_rest():
    assert ties.tie_variants("SELECT b FROM t", "duckdb") is None  # no LIMIT, no order that counts
    assert ties.tie_variants("SELECT * FROM t ORDER BY a LIMIT 1", "duckdb") is None  # width unknown
    assert ties.tie_variants("SELECT b FROM t ORDER BY a", "duckdb", list_mode=True) is not None
    assert ties.tie_variants("SELECT b FROM t ORDER BY a", "duckdb", list_mode=False) is None
    ascending, descending = ties.tie_variants("SELECT a, b FROM t ORDER BY a LIMIT 2", "sqlite")
    assert "ORDER BY a, 1 ASC, 2 ASC" in ascending and "ORDER BY a, 1 DESC, 2 DESC" in descending


def test_the_dlbench_tie_witness_is_nondeterministic():
    """BIRDTrans/clickhouse/43: every emp_id NULL, so two year groups tie at 0 under LIMIT 1; SQLite and DuckDB
    pick differently. With one count made larger the engines agree."""

    adapter = dr.ADAPTERS["dlbench-target"]
    case = adapter.case(_pair("dlbench-target", "BIRDTrans/clickhouse/43"))
    assert case is not None
    dr.install()
    runner = engine.Runner(case)
    columns = [c.name for c in case.tables["employee"].columns]
    try:
        def row(emp_id, hired):
            values = [None] * len(columns)
            values[columns.index("emp_id")], values[columns.index("hire_date")] = emp_id, hired
            return tuple(values)

        tied = {"employee": [row(None, None), row(None, dt.datetime(2021, 1, 1))]}
        outcome = engine.compare(runner, tied)
        assert outcome.kind == "differs"
        assert engine.confirm(runner, tied, outcome, engine.random.Random(0)) == "nondeterministic"
        counted = {"employee": [row(None, None), row("x", dt.datetime(2021, 1, 1))]}
        assert engine.compare(runner, counted).kind == "same"
    finally:
        runner.close()
