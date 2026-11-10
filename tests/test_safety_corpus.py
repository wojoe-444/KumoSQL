"""Independent semantic gate for every built-in rule and their full pipeline."""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from tools.check_safety_corpus import (
    _check_successful_output,
    _load_corpus,
    _sqlx_interpolations,
    _sqlx_placeholders_preserved,
    run_gate,
)

pytestmark = pytest.mark.eval


def test_every_builtin_and_full_pipeline_pass_independent_safety_gate():
    totals = run_gate()

    assert totals["rule_count"] > 0
    assert totals["rule_applications"] > 0
    assert totals["pipeline_applications"] > 0
    assert totals["rules_with_observed_changes"] == totals["rule_count"]
    assert totals["rule_failures"] == 0
    assert totals["unproven_outputs"] == 0
    assert totals["invalid_successes"] == 0
    assert totals["unsafe_successes"] == 0
    assert totals["uncheckable_successes"] == 0
    assert totals["dml_cases"] == 3
    assert totals["dml_rule_applications"] == totals["rule_count"] * 3
    assert totals["dml_pipeline_applications"] == 3
    assert totals["dml_rule_failures"] == 0
    assert totals["dml_unproven_outputs"] == 0
    assert totals["dml_invalid_or_changed_outputs"] == 0


def test_sqlx_placeholder_multiset_allows_reordering_and_handles_nested_braces():
    source = '''config { type: "table" }
SELECT * FROM ${ref("generic_table")} JOIN ${ref("aux_table")} ON TRUE'''
    reordered = '''config { type: "table" }
SELECT * FROM ${ref("aux_table")} JOIN ${ref("generic_table")} ON TRUE'''
    nested = '${when(incremental(), {"filter": {"enabled": true}}, "TRUE")}'
    nested_token = '${when(incremental(), {"filter": ${ref("generic_table")}}, "TRUE")}'

    assert _sqlx_placeholders_preserved(source, reordered)
    assert _sqlx_interpolations(nested) == [nested]
    assert _sqlx_interpolations(nested_token) == [
        nested_token,
        '${ref("generic_table")}',
    ]


def test_independent_oracle_detects_a_known_unsafe_output():
    source = "SELECT DISTINCT category FROM `generic_table`"
    unsafe_candidate = "SELECT category FROM `generic_table`"
    totals = {
        "invalid_successes": 0,
        "placeholder_mismatches": 0,
        "unsafe_successes": 0,
        "uncheckable_successes": 0,
    }

    _check_successful_output(source, unsafe_candidate, totals)

    assert totals["unsafe_successes"] == 1
    assert totals["invalid_successes"] == 0


def test_independent_guard_detects_an_invalid_successful_candidate():
    totals = {
        "invalid_successes": 0,
        "placeholder_mismatches": 0,
        "unsafe_successes": 0,
        "uncheckable_successes": 0,
    }

    _check_successful_output("SELECT 1", "SELECT * FROM", totals)

    assert totals["invalid_successes"] == 1
    assert totals["unsafe_successes"] == 0


def test_independent_guard_detects_a_lost_sqlx_placeholder():
    source = next(sql for sql in _load_corpus() if "${ref(" in sql)
    lost_placeholder = source.replace('${ref("generic_table")}', "generic_table")
    totals = {"invalid_successes": 0, "placeholder_mismatches": 0}

    assert not _sqlx_placeholders_preserved(source, lost_placeholder)
    _check_successful_output(source, lost_placeholder, totals)
    assert totals["placeholder_mismatches"] == 1
    assert totals["invalid_successes"] == 1
