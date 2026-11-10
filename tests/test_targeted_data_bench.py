"""Floors for the targeted-data eval: regression cases stay caught, the suite beats the old checker."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("targeted_data_bench", ROOT / "tools" / "targeted_data_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["targeted_data_bench"] = bench
_spec.loader.exec_module(bench)

from kumosql.result_equivalence import BigQueryWouldFail, DatasetRunner, compare_outputs  # noqa: E402
from kumosql.targeted_data import database_suite  # noqa: E402

pytestmark = pytest.mark.eval

CASES = json.loads((ROOT / "tests" / "fixtures" / "targeted_data" / "cases.json").read_text(encoding="utf-8"))
ITEMS, SUITES = bench.build_corpus("dev")


# Told apart only on databases with a zero divisor, where BigQuery fails (``10 / comm``), so no
# longer caught: such a database is no BigQuery witness (kumosql.bigquery_on_duckdb).
_ZERO_DIVISOR_ONLY = {("calcite", 204, " / EMP.COMM"), ("calcite", 205, " / t1.COMM")}


def test_every_regression_case_is_still_caught_by_the_suite():
    missed = []
    for case in CASES:
        if any((case["suite"], case["index"]) == key[:2] and key[2] in case["mutant"] for key in _ZERO_DIVISOR_ONLY):
            continue
        suite = SUITES[case["suite"]]
        with DatasetRunner(suite["schema"]) as runner:
            for labeled in database_suite(case["original"], suite["schema"], suite["rules"]):
                try:
                    a = runner.run(case["original"], labeled.dataset)
                    b = runner.run(case["mutant"], labeled.dataset)
                except BigQueryWouldFail:
                    continue  # BigQuery fails on this database, so it catches nothing
                if not compare_outputs(a, b, check_column_names=False)[0]:
                    break
            else:
                missed.append((case["suite"], case["index"], case["operator"]))
    assert not missed


def test_university_set_floors():
    items = [(i, SUITES["university"]) for i in ITEMS if i["suite"] == "university"]
    records = [bench.process(item) for item in items]
    result = bench.score(records)
    assert result["configs"]["suite"]["score"] >= 0.80
    assert result["configs"]["suite"]["killed"] > result["configs"]["random_8"]["killed"]
    assert result["configs"]["single_seed"]["killed"] < result["configs"]["suite"]["killed"]


def test_unsafe_rewrite_variants_are_all_caught():
    items, suite = bench._unsafe_items("all")
    records = [bench.process((item, suite)) for item in items]
    result = bench.score(records)
    assert result["mutants"] >= 339
    assert result["configs"]["suite"]["killed"] == result["mutants"]


def test_intersect_against_a_join_needs_null_keys_and_is_caught():
    items, suite = bench._unsafe_items("all")
    item = next(i for i in items if i["index"] == "union-intersect-5")
    entry = bench.process((item, suite))["mutants"][0]
    assert "suite" in entry["kills"]
