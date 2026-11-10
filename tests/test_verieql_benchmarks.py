"""VeriEQL benchmark floors on small, evenly spaced samples (the suites are downloaded and cached).

``FLOORS`` only ever goes up: a drop means a regression in proving or counterexample power. A wrong
verdict (a proof contradicted by a counterexample) fails the test whatever the floors say.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "verieql_bench.py"
_spec = importlib.util.spec_from_file_location("verieql_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["verieql_bench"] = bench
_spec.loader.exec_module(bench)

# suite -> (every Nth case, minimum equivalent proofs, minimum executed counterexamples)
SAMPLES = {
    "literature": (4, 2, 4),
    "calcite": (20, 7, 0),
    "leetcode": (200, 19, 21),
}


@pytest.mark.parametrize("suite", sorted(SAMPLES))
def test_verieql_sample(suite):
    every, proofs, refutations = SAMPLES[suite]
    try:
        result = bench.run_suite(suite, every=every, jobs=1, audit=True)
    except bench.DataUnavailable as error:  # no network and nothing cached, or a download that kept failing
        pytest.skip(str(error))
    assert bench.wrong_count(result) == 0, f"wrong verdicts in {suite}: {result.audit_cases}"
    assert result.counts[bench.EQUIVALENT] >= proofs
    assert result.counts[bench.DIFFERENT] >= refutations


# Pairs known not to be equivalent. The prover is run on them directly, so a counterexample found first by the
# harness cannot hide a proof: (suite, 0-based position in the file, why).
NOT_EQUIVALENT = [
    ("calcite", 12, "testAggregateCaseToFilter, CALCITE-5578: SUM(CASE WHEN d = 20 THEN s ELSE 0 END) is 0 where SUM(s) FILTER (WHERE d = 20) is NULL"),
    ("calcite", 231, "testReduceWithNonTypePredicate, CALCITE-5516: AVG(sal) against an INTEGER cast of the SUM/COUNT form"),
    ("literature", 46, "a pair the paper says needs more than 1,000 tuples to tell apart (groups merge across year and month)"),
    ("literature", 47, "a pair the paper says needs more than 1,000 tuples to tell apart (groups merge across year and month)"),
]


@pytest.mark.parametrize("suite,position,why", NOT_EQUIVALENT, ids=lambda value: str(value)[:24])
def test_pairs_known_not_to_be_equivalent_are_never_proved(suite, position, why):
    try:
        cases = bench.load_cases(suite)
    except bench.DataUnavailable as error:
        pytest.skip(str(error))
    case = cases[position]
    spec = bench.build_spec(case)
    left, right, _ = bench.repaired_pair(case, spec)
    try:
        result = bench.prove(case, spec, 3000, (left, right))
    except Exception:  # a crash is a failure to prove
        return
    assert not result.proven, why
