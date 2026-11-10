"""Documented rewrites: every hand label holds on DuckDB, and the prover holds its floor with 0 wrong.

The cases are in tests/fixtures/documented_rewrites (see tools/documented_rewrites_bench.py). ``FLOORS`` only ever goes up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "documented_rewrites_bench.py"
_spec = importlib.util.spec_from_file_location("documented_rewrites_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["documented_rewrites_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"proven": 17, "refuted": 5}  # measured 17/19 and 5/6
MUST_NOT_PROVE = {"R012b-09", "R012b-23", "R012-037", "R012-039", "R012-040", "R012-042"}


@pytest.mark.parametrize("case", bench.load_cases(), ids=lambda c: c.id)
def test_the_label_holds_on_duckdb(case):
    assert case.source.startswith("https://") and case.claim
    assert bench.shared.label_problems(case) == []


def test_the_prover_holds_its_floor_with_nothing_wrong():
    results = [bench.shared.decide(c) for c in bench.load_cases()]
    assert {r["id"] for r in results if r["label"] == "not_equivalent"} == MUST_NOT_PROVE
    assert not [r["id"] for r in results if r["wrong"]]
    assert sum(r["outcome"] == "proven" for r in results if r["label"] == "equivalent") >= FLOORS["proven"]
    assert sum(r["outcome"] == "refuted" for r in results if r["label"] == "not_equivalent") >= FLOORS["refuted"]
