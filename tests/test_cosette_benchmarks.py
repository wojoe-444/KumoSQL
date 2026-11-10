"""Cosette's examples and SPES-only Calcite pairs: floors and zero wrong verdicts (see tools/cosette_bench.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "cosette_bench.py"
_spec = importlib.util.spec_from_file_location("cosette_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["cosette_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"cosette": 59, "spes": 30, "cosette-adapted": 9}


@pytest.mark.parametrize("suite", ["cosette", "spes", "cosette-adapted"])
def test_suite(suite):
    result = bench.run(suite)
    assert result["wrong"] == [], f"wrong verdicts in {suite}: {result['wrong']}"
    assert result["correct"] >= FLOORS[suite], f"{suite}: {result['correct']} correct, floor {FLOORS[suite]}"
    assert result["scored"] == result["total"] - len(result["disputed"])


def test_adapted_pairs_are_answered_and_held_out_ones_named():
    result = bench.run("cosette-adapted")
    assert result["unknown"] == [] and result["disputed"] == []
    assert result["held_out_correct"] == len(result["held_out"]) == 2
