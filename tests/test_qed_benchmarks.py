"""QED's Calcite cases (converted to SQL): coverage floor and zero wrong proofs (see tools/qed_bench.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "qed_bench.py"
_spec = importlib.util.spec_from_file_location("qed_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["qed_bench"] = bench
_spec.loader.exec_module(bench)

FLOOR = 369


def test_qed_calcite_cases():
    result = bench.run()
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert result["proved"] >= FLOOR, f"proved {result['proved']}, floor {FLOOR}"
    assert result["scored"] == result["total"] - len(result["different"])
