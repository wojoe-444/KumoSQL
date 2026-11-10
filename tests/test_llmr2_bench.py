"""Checks on the LLM-R2 scale-test harness (no benchmark data needed)."""

from __future__ import annotations

import pytest

from tools import llmr2_bench as bench

pytestmark = pytest.mark.eval


def test_transformations_leave_the_formatter_out():
    rules = bench.transformations()
    assert "format_sql" not in rules["rules"]
    assert rules["lift_subqueries"] == ("lift_subqueries",)


def test_outcomes():
    assert bench.outcome({"changed": False, "verification": "unchanged"}) == "unchanged"
    assert bench.outcome({"changed": True, "verification": "proven", "real": {"status": "same"}}) == "same"
    assert bench.outcome({"changed": True, "verification": "proven", "real": {"status": "different"}}) == "wrong"
    assert bench.outcome({"changed": True, "verification": "unproven", "real": {"status": "different"}}) == "caught"
    assert bench.outcome({"changed": True, "verification": "proven", "real": {"status": "original_fails"}, "synthetic": "same"}) == "same"
    assert bench.outcome({"changed": True, "verification": "proven", "real": {"status": "original_fails"}, "synthetic": "error"}) == "unverified"
    assert bench.outcome({"status": "timeout"}) == "timeout"


def test_large_results_compare_by_hash(monkeypatch):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS x FROM range(50)")
    monkeypatch.setattr(bench, "MAX_ROWS", 10)
    forward = bench._rows_or_hash(con, "SELECT x FROM t")
    backward = bench._rows_or_hash(con, "SELECT x FROM t ORDER BY x DESC")
    assert forward == backward and forward[0] == 50
    assert bench._rows_or_hash(con, "SELECT x FROM t WHERE x < 49") != forward
    assert bench._rows_or_hash(con, "SELECT x FROM t WHERE x < 5") == bench._rows_or_hash(con, "SELECT x FROM t WHERE x < 5 ORDER BY x DESC")


def test_summary_counts_wrong_and_useful():
    results = {
        "llm-r2/tpch/test/1": {"rules": {"changed": True, "verification": "proven", "seconds": 0.1,
                                         "real": {"status": "same", "plan_changed": True, "original_s": 1.0, "transformed_s": 0.5}}},
        "llm-r2/tpch/test/2": {"rules": {"changed": True, "verification": "proven", "seconds": 0.1, "real": {"status": "different"}}},
        "llm-r2/tpch/test/3": {"convert": "unsupported"},
    }
    summary = bench.summarise(results)["datasets"]["tpch"]
    assert summary["rules"]["useful"] == 1 and summary["rules"]["wrong"] == 1
    assert summary["queries"] == {"converted": 2, "convert": 1}
