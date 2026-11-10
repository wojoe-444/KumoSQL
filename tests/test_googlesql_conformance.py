"""GoogleSQL conformance: the pure-Python evaluator against the compliance tests' expected results.

Runs the whole claimed subset of the checked-in fixture (tests/fixtures/googlesql_conformance, Apache-2.0, see its NOTICE)
in-process. A case is exact, unsupported or a mismatch; a mismatch is a wrong answer and must stay 0 on both splits.
See docs/evals/googlesql-conformance.md.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

SPEC = importlib.util.spec_from_file_location(
    "googlesql_conformance_floor", Path(__file__).parent.parent / "tools" / "googlesql_conformance.py"
)
conformance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = conformance  # dataclasses (postponed annotations) look their module up here
SPEC.loader.exec_module(conformance)

# Floors: exact answers must not drop. Raise them with a results-file update when the evaluator gains cases.
FLOORS = {"dev": 2694, "heldout": 1024}


def run(split):
    selected, total = conformance.select(split, [], None)
    outcomes = conformance.run_all(selected, 2, 30.0)
    return selected, outcomes


@pytest.mark.parametrize("split", ["dev", "heldout"])
def test_no_wrong_answers_and_exact_floor(split):
    selected, outcomes = run(split)
    wrong = [f"{o.file}/{o.name}: {o.reason} {o.detail[:200]}" for o in outcomes if o.verdict in ("mismatch", "unscored")]
    assert not wrong, wrong[:10]
    exact = sum(1 for o in outcomes if o.verdict == "exact")
    assert len(outcomes) == len(selected)
    assert exact >= FLOORS[split], (exact, FLOORS[split])
    assert not any(o.timeout for o in outcomes)
