"""The conditional verdict on Singh & Bedathur's and VeriEQL's LeetCode pairs: floors, and zero wrong or crashed.

``tools/conditional_bench.py`` re-runs every conditional proof on random DuckDB databases repaired to meet its conditions and
checks that dropping any one condition loses the proof. The Singh data is downloaded on first use (see
tools/singh_bedathur_bench.py); the tests skip when it cannot be fetched. ``FLOORS`` only ever goes up.
"""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_tools = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_tools))
_spec = importlib.util.spec_from_file_location("conditional_bench", _tools / "conditional_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["conditional_bench"] = bench
_spec.loader.exec_module(bench)

# measured 20 of 120 Singh pairs, 13 of 160 VeriEQL cases and 610 of 2,800 Singh pairs; a little room for solver timeouts under load
FLOORS = {"singh-sample": 17, "verieql-sample": 11, "singh-all": 570}


@pytest.fixture(autouse=True)
def _own_singh_module(monkeypatch):
    """The Singh eval's own test loads ``singh_bedathur_bench`` under the same name; pickling a ``Pair`` for the worker
    processes finds the class by that name, so point it at the module these pairs come from while a test runs."""
    monkeypatch.setitem(sys.modules, "singh_bedathur_bench", bench.singh)


def _singh_pairs():
    try:
        return bench.singh.load_pairs()
    except OSError as error:
        pytest.skip(f"benchmark data not available: {error}")


def _check(report, floor):
    counts = report.counts
    assert counts["wrong"] == 0, [o.detail for o in report.outcomes if o.kind == "wrong"]
    assert counts["crash"] == 0, [o.detail for o in report.outcomes if o.kind == "crash"]
    assert counts["conditional"] >= floor, report.line()
    conditional = [o for o in report.outcomes if o.kind == "conditional"]
    assert all(o.minimal for o in conditional), "a named condition could be dropped"
    assert all(o.conditions for o in conditional)


def test_singh_sample():
    pairs = random.Random(2024).sample(_singh_pairs(), 120)
    _check(bench.run_singh(pairs), FLOORS["singh-sample"])


def test_verieql_sample():
    veri = pytest.importorskip("verieql_bench")
    try:
        cases = veri.load_cases("leetcode")[::150]
    except OSError as error:
        pytest.skip(f"benchmark data not available: {error}")
    _check(bench.run_verieql(cases), FLOORS["verieql-sample"])


@pytest.mark.slow
def test_all_singh_pairs():
    _check(bench.run_singh(_singh_pairs()), FLOORS["singh-all"])
