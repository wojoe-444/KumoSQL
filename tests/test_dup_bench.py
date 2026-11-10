"""Duplicate-detection benchmark: floors on a generated project, and a check that the checker can fail."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import dup_bench as gen  # noqa: E402
import dup_bench_run as run  # noqa: E402

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def result():
    return run.run_project(gen.generate(60, seed=1))


def test_nothing_wrong_is_offered_as_a_duplicate_or_a_ready_refactor(result):
    for split in ("dev", "held_out"):
        assert result[split]["exact_wrong"] == 0
    assert result["refactors"]["proof_ready_unsafe"] == 0
    assert result["refactors"]["executed_disagree"] == 0


def test_renamed_and_reordered_copies_are_found(result):
    assert result["dev"]["exact_recall_textual"] == 1.0
    assert result["held_out"]["exact_recall_textual"] == 1.0
    assert result["dev"]["exact_recall_semantic"] >= 0.7


def test_similar_logic_is_found_with_high_precision(result):
    assert result["dev"]["similar"]["precision"] >= 0.95
    assert result["dev"]["similar"]["recall"] >= 0.9


def test_every_family_with_equal_copies_gets_a_verified_refactor(result):
    f = result["refactors"]
    assert f["families_with_verified_refactor"] == f["families_eligible"] > 0


def test_labels_hold_up_under_execution(result):
    check = result["label_check"]
    assert check["copies_compared"] > 0
    assert check["equal_but_different_rows"] == 0
    assert check["decoys_not_told_apart"] <= check["copies_compared"] // 10


def test_the_executed_check_catches_a_refactor_that_changes_results():
    databases = [gen.make_database(["t000"], 100 + i) for i in range(3)]
    before = "SELECT id FROM t000 WHERE amt > 5"
    shared = "SELECT id, amt FROM t000"
    good = "SELECT id FROM shared_model AS s WHERE amt > 5"
    bad = "SELECT id FROM shared_model AS s WHERE amt > 6"
    assert run._executed_agrees(databases, before, good, shared) is True
    assert run._executed_agrees(databases, before, bad, shared) is False
    assert run._executed_agrees(databases, before, good, "SELECT id FROM missing_table") is False
