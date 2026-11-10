"""GoogleSQL compliance expected results: KumoSQL's BigQuery-to-DuckDB translation must return them.

A pinned sample of the compliance cases (tests/fixtures/googlesql_results/sample.json.gz, Apache-2.0,
with the fixture tables of their files) runs here; ``python tools/googlesql_results_eval.py`` runs
them all. See docs/evals/googlesql-expected-results.md.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

duckdb = pytest.importorskip("duckdb")

SPEC = importlib.util.spec_from_file_location(
    "googlesql_results_eval", Path(__file__).parent.parent / "tools" / "googlesql_results_eval.py"
)
ev = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ev  # dataclasses (postponed annotations) look their module up here
SPEC.loader.exec_module(ev)


def test_sample_agrees_with_the_expected_rows():
    cases, recorded = ev.load_sample()
    results = ev.run(cases)
    by_id = {r["id"]: r for r in results}
    wrong = [r for r in results if r["class"] == "DISAGREE" and r.get("kind") in ("bug", "decline", "unclassified")]
    assert not wrong, [(r["id"], r["detail"]) for r in wrong]
    # every case keeps its recorded outcome: an agreement lost, or a skip that became a case, shows up here
    changed = {i: (recorded[i], by_id[i]["class"]) for i in recorded if by_id[i]["class"] != recorded[i]}
    assert not changed, changed
    summary = ev.summarize(results)
    assert summary["agree"] >= 1


def test_expected_result_text_is_read_with_types_and_order():
    t, value = ev.parse_result(
        'ARRAY<STRUCT<a INT64, b DOUBLE, c STRING, d ARRAY<>>>[unknown order:'
        '{1, nan, "x\\"y", ARRAY<INT64>[known order:1, 2]}, {NULL, -inf, NULL, ARRAY<INT64>(NULL)}]'
    )
    assert value.unordered and len(value.items) == 2
    first = value.items[0]
    assert first[0] == 1 and first[1] != first[1] and first[2] == 'x"y' and first[3].items == [1, 2]
    assert value.items[1][3] is None
    assert ev.float_equal(0.1 + 0.2, 0.3) and not ev.float_equal(1.0, 1.0001)


def test_unknown_order_compares_as_a_multiset_and_known_order_as_a_list():
    t, value = ev.parse_result("ARRAY<STRUCT<INT64>>[unknown order:{1}, {2}]")
    assert ev._rows_equal(t, value, [(2,), (1,)], False)
    t, value = ev.parse_result("ARRAY<STRUCT<INT64>>[known order:{1}, {2}]")
    assert not ev._rows_equal(t, value, [(2,), (1,)], False)


def test_cases_outside_bigquery_are_skipped_with_a_reason():
    case = ev.Case("f/a", "f", "a", "SELECT CAST(1 AS INT32)", "ARRAY<STRUCT<INT32>>[{1}]")
    assert ev.skip_reason(case, set()).startswith("type outside BigQuery")
    case = ev.Case("f/b", "f", "b", "SELECT 1", "ARRAY<STRUCT<INT64>>[{1}]", {"required_features": "MAP_TYPE"})
    assert ev.skip_reason(case, set()) == "feature outside BigQuery"
    case = ev.Case("f/c", "f", "c", "SELECT 1", "ARRAY<STRUCT<INT64>>[{1}]", {"required_features": "ANALYTIC_FUNCTIONS"})
    assert ev.skip_reason(case, set()) is None


def test_held_out_files_are_a_fifth_by_hash():
    cases, _ = ev.load_sample()
    files = {c.file for c in cases}
    assert any(ev.held_out(f) for f in files) and not all(ev.held_out(f) for f in files)


def test_a_default_time_zone_in_the_first_block_covers_the_file():
    text = (
        "[default_time_zone=Pacific/Chatham]\n[default global_labels=x]\n\n[prepare_database]\nCREATE TABLE T AS SELECT 1 AS a\n--\n"
        "ARRAY<STRUCT<a INT64>>[{1}]\n==\n[name=q]\nSELECT 1\n--\nARRAY<STRUCT<INT64>>[{1}]\n"
    )
    cases = ev.parse_test_file("f", text)
    assert [c.options.get("default_time_zone") for c in cases] == ["Pacific/Chatham"] * 2
    assert ev.skip_reason(cases[1], set()) == "case-specific default time zone"


def test_every_pinned_test_file_has_a_sha256():
    pinned = [line.split() for line in ev.MANIFEST.read_text().splitlines()]
    assert len(pinned) > 250 and all(len(h) == 64 and name.endswith(".test") for h, name in pinned)
    assert len({name for _, name in pinned}) == len(pinned)
