"""Jaffle Shop eval (tools/jaffle_shop_bench.py): a real dbt project built in DuckDB and loaded into KumoSQL.

Zero wrong answers, the upstream expectations hold, and the coverage floors of the full run
(``python tools/jaffle_shop_bench.py``) are kept, on fewer random databases.
"""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("jaffle_shop_bench", ROOT / "tools" / "jaffle_shop_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["jaffle_shop_bench"] = bench
_spec.loader.exec_module(bench)

# Equivalent authored refactors the prover leaves unknown (regression cases: unknown is allowed, wrong is not).
KNOWN_UNPROVED = {
    "inline_staging_into_customers",
    "lifetime_value_from_orders_mart",
    "customer_payments_inner_join",
    "customer_orders_from_orders_mart",
}


@pytest.fixture(scope="module")
def project():
    models = bench.dbt_models()
    return models, bench.load(bench.sqlx_models(models))


def test_the_copy_is_the_pinned_upstream_commit():
    assert bench.verify_pin() == []
    assert (bench.FIXTURE / "LICENSE").read_text().startswith(" " * 33 + "Apache License")


def test_renderer_matches_jinja_and_refuses_anything_else():
    models = bench.dbt_models()
    jinja2 = pytest.importorskip("jinja2")
    assert jinja2
    for model in models.values():
        assert bench.render(model.text, lambda r: r) == bench.render_with_jinja2(model.text, lambda r: r), model.name
    for text in ("{{ var('x') }}", "{% if x %}a{% endif %}", "{{ config(materialized='table') }}"):
        with pytest.raises(ValueError):
            bench.render(text, lambda r: r)


def test_upstream_dbt_tests_pass_on_the_dbt_build():
    out = bench.track_build(bench.dbt_models())
    assert out["total"] == 20
    assert out["failed"] == []
    assert out["rows"] == {"customers": 100, "orders": 99, "stg_customers": 100, "stg_orders": 99, "stg_payments": 113}


def test_dbt_tests_catch_a_broken_build():
    """The test SQL is not vacuous: a build that breaks the data fails the matching tests."""

    models = bench.dbt_models()
    data = bench.seed_rows()
    data["raw_orders"].append((1, None, None, "lost"))
    con = bench.connect(data)
    bench.build_dbt(con, models)
    failed = {t.id for t in bench.dbt_tests() if con.execute(bench.dbt_test_sql(t, lambda n: f"main.{n}")).fetchall()}
    assert failed == {"unique/stg_orders.order_id", "accepted_values/stg_orders.status", "unique/orders.order_id",
                      "not_null/orders.customer_id", "accepted_values/orders.status"}


def test_graph_is_dbts_ref_graph(project):
    models, pipeline = project
    out = bench.track_graph(models, pipeline)
    assert (out["found"], out["expected"], out["extra"], out["missing"]) == (8, 8, [], [])
    assert out["order_respects_refs"] and out["complete"]


def test_lineage_matches_the_hand_written_expectation(project):
    models, pipeline = project
    con = bench.connect(bench.seed_rows())
    bench.build_dbt(con, models)
    out = bench.track_lineage(pipeline, {name: bench.columns_of(con, "main", name) for name in models})
    assert out["expected_matches_built_columns"] and out["kumosql_columns_match_built_columns"]
    assert out["wrong"] == [] and out["correct"] == out["columns"] == 27


def test_loader_round_trip_returns_the_dbt_rows(project):
    models, pipeline = project
    out = bench.track_round_trip(models, pipeline, bench.databases(4))
    assert out["differing"] == []


def test_every_rewrite_keeps_every_model_output(project):
    _, pipeline = project
    out = bench.track_rewrites(pipeline, bench.databases(3))
    assert out["wrong"] == [], [r for r in out["rows"] if r["status"] == "wrong"]
    assert out["errors"] == {}
    assert out["cases"] == 45
    assert out["changed_tree"] >= 10


def test_output_comparison_api_agrees_on_the_unchanged_pipeline(project):
    models, pipeline = project
    out = bench.track_comparison(models, pipeline)
    assert out["disagreements"] == [] and out["agreed"] == out["checks"] == 30


def test_random_databases_hold_what_the_refactors_need():
    rng = random.Random(1)
    rows = [bench.random_rows(rng) for _ in range(20)]
    assert any(not r["raw_payments"] for r in rows) and any(None in [o[0] for o in r["raw_orders"]] for r in rows)


REFACTORS = {case.id: case for case in bench.refactors()}


def test_refactor_ids_are_unique_and_both_answers_occur():
    cases = bench.refactors()
    assert len(cases) == len(REFACTORS) == 15
    assert {c.label for c in cases} == {"equivalent", "different"}


def test_every_breaking_refactor_shows_on_the_edge_database_and_no_equivalent_one_does(project):
    """The labels do not rest on a timed solver search: the pivot COALESCE and the distinct count first show on random
    database 14 and 17 of seed 23, past the 8 the test runs, so without the edge database the test passed only while
    the prover's 5 s counterexample search finished in time (it failed on a loaded machine)."""
    models, _ = project
    edge = bench.edge_rows()
    for case in REFACTORS.values():
        files, rename = bench.world(models, case)
        shown = bool(bench._outputs_differ(bench.load(files), edge, list(models), rename))
        assert shown == (case.label == "different"), case.id


# The quicker cases run in the fast tier; the rest (the prover takes 10-25 s on each) in the slow tier.
FAST = {"extract_order_payments", "simple_case_pivot", "right_join_order_payments", "inner_join_order_payments",
        "pivot_coalesce", "customers_inner_join_orders"}


@pytest.mark.parametrize(
    "case_id", [i if i in FAST else pytest.param(i, marks=pytest.mark.slow) for i in sorted(REFACTORS)])
def test_authored_refactor(project, case_id):
    models, pipeline = project
    case = REFACTORS[case_id]
    row = bench.run_refactor(models, pipeline, case, bench.databases(8, seed=23, edge=True))
    assert row["verdict"] != "error", row["detail"]
    assert not row["wrong"], row
    assert row["ground_truth_ok"], f"the label disagrees with execution: {row}"
    assert row["comparison_agrees"], row["comparison"]
    if case.label == "different":
        assert row["verdict"] == "different", row
    elif case_id not in KNOWN_UNPROVED:
        assert row["verdict"] == "equivalent", row
