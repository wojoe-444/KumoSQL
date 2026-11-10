"""Incremental-versus-full-refresh model: Dataform semantics, proof soundness, eval floor."""

import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental import (  # noqa: E402
    IncrementalError,
    SourceTable,
    check_incremental,
    prove,
    first_divergence,
    parse_incremental_sqlx,
    replay,
    search_divergence,
)

pytestmark = pytest.mark.eval

_path = Path(__file__).resolve().parent.parent / "tools" / "incremental_bench.py"
_spec = importlib.util.spec_from_file_location("incremental_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["incremental_bench"] = bench
_spec.loader.exec_module(bench)

EVENTS = {"events": SourceTable({"id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, ("id",), "ts")}
COAL = "COALESCE((SELECT MAX(ts) FROM ${self()}), TIMESTAMP '1970-01-01')"


def ins(i, hour):
    return f"INSERT INTO events (id, ts, v) VALUES ({i}, TIMESTAMP '2024-01-01 {hour:02d}:00:00', 1)"


def model(config, where):
    sqlx = f'config {{ type: "incremental"{config} }}\nSELECT id, ts, v FROM ${{ref("events")}}\n${{when(incremental(), `{where}`)}}\n'
    return parse_incremental_sqlx(sqlx, "m")


def test_parse_resolves_both_modes():
    m = model(', uniqueKey: ["id"]', "WHERE ts > " + COAL)
    assert "WHERE" not in m.full_sql and "FROM m" in m.incremental_sql
    assert m.unique_key == ("id",)


def test_parse_rejects_unknown_interpolation():
    with pytest.raises(IncrementalError):
        parse_incremental_sqlx('config { type: "incremental" }\nSELECT ${foo()} FROM x', "m")


def test_pre_operations_run_only_incrementally():
    m = parse_incremental_sqlx(
        'config { type: "incremental" }\npre_operations {\n ${when(incremental(), `DELETE FROM ${self()} WHERE v > 5`)}\n}\nSELECT 1 AS v',
        "m",
    )
    assert m.pre_operations == ("DELETE FROM m WHERE v > 5",)


def test_gte_without_key_duplicates_and_merge_fixes_it():
    appended = model("", "WHERE ts >= " + COAL)
    assert first_divergence(replay(appended, EVENTS, [ins(1, 1)], [[]])).index == 1
    merged = model(', uniqueKey: ["id"]', "WHERE ts >= " + COAL)
    assert first_divergence(replay(merged, EVENTS, [ins(1, 1)], [[], [ins(2, 1)]])) is None


def test_merge_with_two_source_rows_for_one_target_row_errors():
    merged = model(', uniqueKey: ["id"]', "WHERE ts >= " + COAL)
    result = first_divergence(replay(merged, EVENTS, [ins(1, 1)], [[ins(1, 1)]]))
    assert result.status == "error" and "MERGE" in result.error


def test_null_keys_never_match():
    merged = model(', uniqueKey: ["id"]', "WHERE ts >= " + COAL)
    null_row = "INSERT INTO events (id, ts, v) VALUES (NULL, TIMESTAMP '2024-01-01 01:00:00', 1)"
    assert first_divergence(replay(merged, EVENTS, [null_row], [[]])).index == 1


def test_counterexample_replays_and_is_small():
    m = model("", "WHERE ts > " + COAL)
    found = search_divergence(m, EVENTS, {"insert_new", "insert_late"})
    assert found is not None and found.size <= 3
    assert first_divergence(replay(m, EVENTS, found.initial, found.batches)) is not None


def test_proof_rules_answer_safe_only_inside_their_contract():
    m = model("", "WHERE ts > " + COAL)
    assert check_incremental(m, EVENTS, {"insert_new", "empty"}).outcome == "safe"
    assert check_incremental(m, EVENTS, {"insert_new", "insert_boundary"}).outcome == "diverges"
    no_default = model("", "WHERE ts > (SELECT MAX(ts) FROM ${self()})")
    assert check_incremental(no_default, EVENTS, {"insert_new"}).outcome == "diverges"


def test_proven_cases_never_diverge_under_a_deeper_search():
    for case in bench.load_cases("dev"):
        m, sources = bench.build(case)
        contract = case["contract"]
        tables = tuple(contract.get("tables") or ()) or None
        kinds = frozenset(contract["kinds"])
        if prove(m, sources, kinds, tables) is not None:
            found = search_divergence(m, sources, contract["kinds"], seeds=25, batches=5, seed=7, tables=tables)
            assert found is None, case["id"]


def test_corpus_floor_and_zero_wrong():
    result = bench.run("all", seeds=15)
    assert result["total"] >= 49
    assert result["wrong"] == []
    assert result["fidelity_failures"] == []
    assert result["coverage"]["refuted"] >= 33
    assert result["coverage"]["proven"] >= 15
    assert result["coverage"]["error"] == 0


def test_pgivm_workloads_are_adapted_with_provenance_and_refuted():
    import json

    path = Path(__file__).resolve().parent / "fixtures" / "incremental" / "pgivm_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 336 and sum("adapted" in c for c in cases) >= 110
    assert all(c["source"]["commit"].startswith("22b4b45") and c["original"]["query"] for c in cases)
    assert all("unsupported" in c or "adapted" in c for c in cases)
    result = bench.run_pgivm(seeds=10, limit=12)
    assert result["wrong"] == [] and result["coverage"]["proven"] == 0
    assert result["coverage"]["refuted"] >= 10
