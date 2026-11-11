"""Script suite: floors, and zero on everything wrong (see docs/scripts.md#evaluation).

The numbers are the floors behind the scoreboard row; raise them when a fix moves a score.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


script_bench = _load("script_bench")


@pytest.mark.parametrize("seed", [1, 2])
def test_generated_scripts_are_exact(seed):
    dev = script_bench.run_suite(script_bench.DEV_FAMILIES, 30, seed)
    mixed = script_bench.run_suite(script_bench.MIXED, 30, seed)
    for result in (dev, mixed):
        totals = result["totals"]
        assert totals.get("wrong", 0) == 0 and totals.get("columns_wrong", 0) == 0, result["details"]
        assert totals.get("missed", 0) == 0, result["details"]
        assert totals["edges_correct"] == totals["edges_true"] == totals["edges_found"]
    assert dev["totals"]["columns_exact"] == dev["totals"]["columns_total"] >= 300


def test_job_history_scripts_leave_no_temporary_tables():
    import random

    result = script_bench.score_jobs(random.Random(1), 50)
    assert result["wrong"] == 0 and result["missed"] == 0, result["details"]


def test_public_scripting_examples_match_hand_written_labels():
    result = script_bench.run_public()
    assert result["wrong"] == 0, result["details"]
    assert result["exact"] == result["cases"] == 22
