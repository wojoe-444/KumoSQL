"""Whole-pipeline equivalence eval: zero wrong answers, known answers hold, coverage floor (tools/pipeline_bench.py)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pipeline_bench", ROOT / "tools" / "pipeline_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["pipeline_bench"] = bench
_spec.loader.exec_module(bench)

GAPS = json.loads((ROOT / "tests" / "fixtures" / "pipeline_equiv" / "known_gaps.json").read_text(encoding="utf-8"))

# floors for the full development run (50 equivalent cases, 66 different)
PROVED_FLOOR = 50
REFUTED_FLOOR = 66


def small(case) -> bool:
    return int(case.id.rsplit("/", 1)[1]) <= 3


def test_every_case_id_is_unique_and_answers_are_balanced():
    ids = [c.id for c in bench.all_cases() + bench.all_cases(held_out=True)]
    assert len(ids) == len(set(ids))
    assert {c.label for c in bench.all_cases()} == {"equivalent", "different"}
    assert all(c.after for c in bench.all_cases())  # every case changes the pipeline
    assert {c.family for c in bench.all_cases(held_out=True)} == bench.HELD_OUT_FAMILIES


def test_small_pipelines_have_no_wrong_answers_and_known_answers_hold():
    cases = [c for c in bench.all_cases() if small(c)]
    results = [bench.run_case(c, trials=25) for c in cases]
    out = bench.summarise(cases, results)
    assert out["wrong"] == [], f"wrong answers: {out['wrong']}"
    assert out["bad_ground_truth"] == [], f"generator labels disagree with execution: {out['bad_ground_truth']}"
    assert out["error"] == 0, out["errors"]
    assert out["refuted"] == out["different_cases"], out["unrefuted"]
    assert out["proved"] >= out["equivalent_cases"] - len([i for i in GAPS["unproved"] if i in {c.id for c in cases}])


@pytest.mark.slow
def test_full_development_run():
    out = bench.run(trials=25)
    assert out["wrong"] == [] and out["bad_ground_truth"] == [] and out["error"] == 0
    assert out["proved"] >= PROVED_FLOOR, out["unproved"]
    assert out["refuted"] >= REFUTED_FLOOR, out["unrefuted"]
