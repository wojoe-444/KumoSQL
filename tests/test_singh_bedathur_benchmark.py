"""Singh & Bedathur's LeetCode equivalence pairs: decided-count floors and zero wrong verdicts.

The data is downloaded on first use (see tools/singh_bedathur_bench.py); the tests skip when
it cannot be fetched. ``FLOORS`` only ever goes up.
"""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "singh_bedathur_bench.py"
_spec = importlib.util.spec_from_file_location("singh_bedathur_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["singh_bedathur_bench"] = bench
_spec.loader.exec_module(bench)

SAMPLE = 120
FLOORS = {"sample": 104, "all": 2610}  # measured 106 and 2,626; a little room for solver timeouts under load


def _pairs():
    try:
        return bench.load_pairs()
    except OSError as error:
        pytest.skip(f"benchmark data not available: {error}")


def test_sample_of_pairs():
    pairs = random.Random(2024).sample(_pairs(), SAMPLE)
    for number, pair in enumerate(pairs):
        pair.index = number
    report = bench.run(pairs)
    assert report.counts["wrong"] == 0, [i for i, v in report.verdicts.items() if v.kind == "wrong"]
    decided = report.counts["equivalent"] + report.counts["different"]
    assert decided >= FLOORS["sample"], report.line()


@pytest.mark.slow
def test_all_pairs():
    report = bench.run(_pairs())
    assert report.counts["wrong"] == 0, [i for i, v in report.verdicts.items() if v.kind == "wrong"]
    decided = report.counts["equivalent"] + report.counts["different"]
    assert decided >= FLOORS["all"], report.line()
