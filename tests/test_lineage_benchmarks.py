"""Lineage, change-impact and Dataform-preservation suites: floors, and zero on everything unsafe.

See docs/evals/lineage-bench.md and docs/evals/dataform-bench.md. The numbers here are the floors behind the README
scoreboard rows; raise them when a fix moves a score.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")  # per-stage timing lines slow thousands of small analyses


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sqllineage_bench = _load("sqllineage_bench")
lineage_bench = _load("lineage_bench")
dataform_bench = _load("dataform_bench")


def test_sqllineage_cases_are_exact_or_honestly_unknown():
    result = sqllineage_bench.run()
    scoped = result["in_scope"]
    assert scoped["total"] == 278
    assert scoped["wrong"] == 0 and scoped["missed"] == 0, [r for r in result["rows"] if r["scope"] == "in" and r["outcome"] in {"wrong", "missed"}]
    assert scoped["exact"] >= 254, scoped
    assert result["table"]["wrong"] == 0 and result["column"]["wrong"] == 0


def _assert_safe(result: dict) -> None:
    assert result["columns_wrong"] == 0, result["details"][:5]
    assert result["impact_unsafe_misses"] == 0, result["details"][:5]
    assert result["dead_unsafe"] == 0, result["details"][:5]
    assert result["opaque_traced_wrongly"] == 0
    assert result["cycle_flagged"] == result["cycle_models"]
    assert result["edge_recall"] == 1.0 and result["edge_precision"] == 1.0
    assert result["read_recall"] == 1.0 and result["table_recall"] == 1.0
    assert result["impact_exact"] == result["impact_changes"]


def test_generated_pipelines_dev_families():
    result = lineage_bench.run_suite(lineage_bench.DEV_FAMILIES + lineage_bench.SPECIAL, sizes=(8, 30, 120), seeds=(1, 2, 3))
    _assert_safe(result)
    assert result["coverage"] == 1.0


def test_generated_pipelines_held_out_families():
    result = lineage_bench.run_suite(lineage_bench.HELD_OUT_FAMILIES, sizes=(8, 30, 120), seeds=(1, 2, 3))
    _assert_safe(result)
    assert result["dead_found"] == result["dead_truth"]


def test_generated_pipelines_other_seeds():
    # Seeds the results file does not use, so the floor is not just the cases that were looked at.
    result = lineage_bench.run_suite(lineage_bench.DEV_FAMILIES + lineage_bench.HELD_OUT_FAMILIES + lineage_bench.SPECIAL, sizes=(40,), seeds=(11, 12, 13, 14))
    _assert_safe(result)


def test_dataform_files_keep_protected_text_and_dependencies():
    for families in (dataform_bench.DEV_FAMILIES, dataform_bench.HELD_OUT_FAMILIES):
        result = dataform_bench.run_families(families, per_family=6, seeds=(1, 2))
        assert result["spans_damaged"] == 0 and result["crashes"] == 0, result["details"][:5]
        assert result["deps_wrong"] == 0 and result["unsafe_dead"] == 0, result["details"][:5]
        assert result["dep_recall"] == 1.0, result["details"][:5]
        assert result["unsupported_silent"] == 0
        assert result["fixable_fixed_cases"] == result["fixable_cases"]
