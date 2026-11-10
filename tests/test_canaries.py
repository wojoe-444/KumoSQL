"""Canaries: a handful (10) of cases of each benchmark family, run in seconds, so a change that breaks the wiring of a family is
caught by the pull-request gate although the family's floor runs only nightly.

A canary asserts *zero wrong answers* on its cases and that the family proves something. It never claims the family's score
(the floors in the ``eval`` test files do that), and it passes on a subset. Add a family by adding a line to ``FAMILIES``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
CASES = 10

pytestmark = pytest.mark.canary


def _tool(name: str):
    sys.path.insert(0, str(TOOLS))
    try:
        spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TOOLS))


@pytest.mark.parametrize("suite", ["calcite", "spark", "tpch", "tpcc"])
def test_sqlsolver_suite_canary(suite):
    bench = _tool("sqlsolver_bench")
    result = bench.run_suite(suite, bench.default_prove, limit=CASES, trials=4)
    assert result.wrong == [], f"wrong proofs: {result.wrong}"
    assert 1 <= result.total <= CASES and result.proved >= 1


def test_qed_canary():
    result = _tool("qed_bench").run(limit=CASES, trials=4)
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert result["total"] == CASES and result["proved"] >= 1


def test_mined_calcite_canary():
    result = _tool("calcite_mined_bench").run(limit=CASES, trials=4)
    assert result["wrong"] == [], f"wrong proofs: {result['wrong']}"
    assert result["total"] == CASES and result["proven"]
