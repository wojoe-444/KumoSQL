"""Query pairs from optimizer wrong-result bugs: every pair really differs, and none is proved.

The data is pinned in tests/fixtures/optimizer_bugs (see tools/optimizer_bugs_bench.py). ``FLOORS`` only
ever goes up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "optimizer_bugs_bench.py"
_spec = importlib.util.spec_from_file_location("optimizer_bugs_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["optimizer_bugs_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"refuted": 5}  # measured 5
# Proved before the fix that came with this eval; never proved again
REGRESSIONS = {"bug-004"}


@pytest.fixture(scope="module")
def results():
    return {r["id"]: r for r in bench.run(bench.load_cases())}


def test_every_pair_differs_on_its_own_data(results):
    assert len(results) == 24
    assert not [i for i, r in results.items() if not r["confirmed"]]


def test_no_pair_is_proved(results):
    assert not [i for i, r in results.items() if r["outcome"] == "proven"]
    assert sum(r["outcome"] == "refuted" for r in results.values()) >= FLOORS["refuted"]


def test_the_setup_declares_keys_and_not_null_columns():
    case = next(c for c in bench.load_cases() if c.id == "bug-018")
    schema, constraints = bench.declared(case)
    assert schema == {"t": ["x", "y", "z"]}
    assert constraints["t"].keys == (("x", "y"),) and constraints["t"].not_null == {"x", "y"}
