"""R-Bot's Calcite rewrite pairs: coverage floor and zero wrong proofs (see tools/rbot_bench.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "rbot_bench.py"
_spec = importlib.util.spec_from_file_location("rbot_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["rbot_bench"] = bench
_spec.loader.exec_module(bench)

FLOOR = 42


def test_rbot_calcite_pairs():
    result = bench.run()
    assert result["total"] == 45
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert result["proved"] >= FLOOR, f"proved {result['proved']}, floor {FLOOR}"
    assert result["scored"] == result["total"] - len(result["different"])
