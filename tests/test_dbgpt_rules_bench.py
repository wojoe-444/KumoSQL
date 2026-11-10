"""DB-GPT's rewrite examples: every hand label holds on DuckDB, and the prover holds its floor with 0 wrong.

The data is pinned in tests/fixtures/dbgpt_rules (see tools/dbgpt_rules_bench.py). ``FLOORS`` only ever goes up.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "dbgpt_rules_bench.py"
_spec = importlib.util.spec_from_file_location("dbgpt_rules_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["dbgpt_rules_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"proven": 27, "refuted": 3}  # measured 27/30 and 3/3


def test_every_source_example_has_one_case():
    source = json.loads((bench.FIXTURES / "rules.json").read_text(encoding="utf-8"))["examples"]
    cases = bench.load_cases()
    assert [c.id for c in cases] == sorted(source, key=int)
    for case in cases:
        if not case.adapted:
            assert (case.left, case.right) == (source[case.id]["input"], source[case.id]["output"])


@pytest.mark.parametrize("case", bench.load_cases(), ids=lambda c: c.id)
def test_the_label_holds_on_duckdb(case):
    assert bench.label_problems(case) == []


def test_the_prover_holds_its_floor_with_nothing_wrong():
    results = [bench.decide(c) for c in bench.load_cases()]
    assert not [r["id"] for r in results if r["wrong"]]
    assert sum(r["outcome"] == "proven" for r in results if r["label"] == "equivalent") >= FLOORS["proven"]
    assert sum(r["outcome"] == "refuted" for r in results if r["label"] == "not_equivalent") >= FLOORS["refuted"]
