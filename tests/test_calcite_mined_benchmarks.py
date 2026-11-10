"""Pairs mined from Calcite's current rule tests: floor and zero wrong proofs (see tools/calcite_mined_bench.py)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "calcite_mined_bench.py"
_spec = importlib.util.spec_from_file_location("calcite_mined_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["calcite_mined_bench"] = bench
_spec.loader.exec_module(bench)

FLOOR = 410


@pytest.mark.slow
def test_calcite_mined_pairs():
    result = bench.run()
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert len(result["proven"]) >= FLOOR, f"proved {len(result['proven'])}, floor {FLOOR}"
    assert result["scored"] == result["total"] - len(result["refuted"])


def test_the_held_out_split_is_the_pairs_new_to_every_other_corpus():
    pairs = bench.load_pairs()
    dev, held = bench.split_pairs(pairs, "dev"), bench.split_pairs(pairs, "held-out")
    assert all(p["new"] for p in held) and not any(p["new"] for p in dev)
    assert (len(dev), len(held)) == (334, 168)
    assert bench.split_pairs(pairs) == pairs  # the default is the published command's: every pair
    with pytest.raises(ValueError):
        bench.split_pairs(pairs, "test")
