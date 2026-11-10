"""Join-type rewrites to and from LEFT JOIN: hand-checked pairs, proved or refuted, never wrong."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "join_rewrite_bench.py"
_spec = importlib.util.spec_from_file_location("join_rewrite_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["join_rewrite_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {False: (58, 42), True: (10, 6)}  # (proved, refuted) for the development and held-out pairs


@pytest.mark.parametrize("held_out", [False, True], ids=["dev", "held_out"])
def test_join_rewrite_pairs_keep_their_floor_with_zero_wrong(held_out):
    result = bench.run(held_out=held_out, databases=20)
    assert result["wrong"] == [], result["wrong"]
    assert result["witnessed"] == result["not_equivalent"], "a stored counterexample no longer separates its pair"
    proved, refuted = FLOORS[held_out]
    assert result["proven"] >= proved, result["missed"]
    assert result["refuted"] >= refuted
