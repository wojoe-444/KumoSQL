"""SQLSolver's published benchmark pairs: coverage floors and zero wrong proofs.

Every proof is re-checked by running both queries on random DuckDB databases that
respect the schema's constraints. ``FLOORS`` only ever goes up: a drop means a
regression in proving power, a wrong proof means a soundness bug.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlglot

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqlsolver_bench.py"
_spec = importlib.util.spec_from_file_location("sqlsolver_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sqlsolver_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"calcite": 227, "spark": 123, "tpch": 22, "tpcc": 19}
if int(sqlglot.__version__.split(".")[0]) < 30:
    # sqlglot 26 parses some constructs differently, so fewer pairs reach the prover (measured 2026-10-02).
    FLOORS = {"calcite": 177, "spark": 118, "tpch": 12, "tpcc": 18}


@pytest.mark.parametrize(
    "suite",
    [
        "calcite",
        "spark",
        "tpcc",
        pytest.param("tpch", marks=pytest.mark.slow),
    ],
)
def test_benchmark_suite(suite):
    result = bench.run_suite(suite, bench.default_prove)
    assert result.wrong == [], f"wrong proofs in {suite}: {[i for i, _ in result.wrong]}"
    assert result.proved >= FLOORS[suite], f"{suite}: proved {result.proved}, floor {FLOORS[suite]}"
    assert result.scored == result.total - len(bench.must_not_prove(suite))


def test_pairs_that_hold_only_with_fixed_tie_breaking_must_stay_unproven():
    """LIMIT without ORDER BY keeps whichever rows the engine picks, and UPPER(LOWER(x)) is not UPPER(x) on
    Unicode text, so these Spark pairs leave the score's denominator and a proof of one is a wrong proof."""

    pairs = bench.load_pairs(bench.FIXTURES / bench.SUITES["spark"][0])
    reasons = bench.must_not_prove("spark")
    assert sorted(reasons) == [44, 50, 60, 61] and all(reasons.values())
    chosen = {pairs[index] for index in reasons}
    result = bench.run_suite("spark", lambda left, right, tables: (left, right) in chosen, limit=62, trials=2)
    assert result.excluded == [44, 50, 60, 61]
    assert [index for index, _ in result.wrong] == [44, 50, 60, 61]
    assert result.scored == 62 - 4
