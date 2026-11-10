"""Behaviour eval: KumoSQL rewrites of BigQuery edge cases and GoogleSQL compliance queries.

Rewrites KumoSQL accepts must give identical results in DuckDB (0 wrong). See
docs/evals/bigquery-behavior-eval.md and tools/bq_behavior_eval.py.
"""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("z3")

SPEC = importlib.util.spec_from_file_location(
    "bq_behavior_eval", Path(__file__).parent.parent / "tools" / "bq_behavior_eval.py"
)
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)

# Floors, raised as coverage improves. "wrong" is always 0. Lowered by 18 on 2026-10-04: the parser check (docs/parser-checks.md)
# refuses 18 seeded fuzz texts that BigQuery rejects or reads differently from sqlglot.
EDGE_MIN_HANDLED = {"semantic": 402, "lift": 407}
EDGE_MAX_UNSUPPORTED = 1


@pytest.mark.parametrize("pipeline", ["semantic", "lift"])
def test_edge_cases_never_change_behaviour(pipeline):
    results, _ = ev.run_corpus(ev.load_edge(), pipeline)
    summary = ev.summarize(results)
    wrong = [r["id"] for r in results if r["class"] == "WRONG"]
    assert not wrong, wrong
    assert summary["error"] == 0
    assert summary["unsupported"] <= EDGE_MAX_UNSUPPORTED
    assert summary["handled"] >= EDGE_MIN_HANDLED[pipeline]


def _googlesql(step):
    return ev.load_googlesql()[::step]


def test_googlesql_sample_never_changes_behaviour():
    results, _ = ev.run_corpus(_googlesql(40), "lift")
    assert [r["id"] for r in results if r["class"] == "WRONG"] == []
    assert not [r for r in results if r["class"] == "error"]


@pytest.mark.slow
@pytest.mark.parametrize("pipeline", ["semantic", "lift"])
def test_googlesql_full_corpus_never_changes_behaviour(pipeline):
    results, _ = ev.run_corpus(ev.load_googlesql(), pipeline)
    assert [r["id"] for r in results if r["class"] == "WRONG"] == []
    assert not [r for r in results if r["class"] == "error"]


def test_lifting_keeps_pivot_and_unpivot_on_the_relation():
    from kumosql import lift_subqueries

    for sql in (
        "SELECT * FROM (SELECT id, a, b FROM t) UNPIVOT (v FOR k IN (a, b)) ORDER BY id, k",
        "SELECT * FROM (SELECT id, a FROM t) AS q PIVOT (SUM(a) FOR id IN (1, 2))",
    ):
        out = lift_subqueries(sql).sql
        assert "PIVOT" in out.upper(), out


def test_prover_never_calls_a_pivot_equivalent_to_its_source():
    from kumosql import SmtStatus, prove_equivalent_smt

    pivoted = "SELECT * FROM (SELECT id, a FROM t) PIVOT (SUM(a) FOR id IN (1, 2))"
    for other in ("SELECT * FROM (SELECT id, a FROM t)", pivoted):
        assert prove_equivalent_smt(pivoted, other).status is not SmtStatus.PROVEN_EQUIVALENT


def test_unordered_aggregates_compare_as_multisets():
    sql = "SELECT ARRAY_CONCAT_AGG(x) FROM (SELECT [1, 2] x UNION ALL SELECT [3])"
    assert ev.unordered_aggregates(sql) == {"ArrayConcatAgg"}
    assert ev.unordered_aggregates("SELECT ARRAY_AGG(x ORDER BY x) FROM t") == set()
    assert ev.same_results([((3, 1, 2),)], [((1, 2, 3),)], False, unordered_elements=True)
    assert not ev.same_results([((3, 1, 2),)], [((1, 2, 4),)], False, unordered_elements=True)
    assert not ev.same_results([((3, 1, 2),)], [((1, 2, 3),)], False)


def test_array_concat_agg_json_case_is_stable():
    case = next(c for c in ev.load_googlesql() if c["id"] == "array_aggregation/array_concat_agg_json")
    for _ in range(5):
        assert ev.evaluate(case["sql"], ev.PIPELINES["lift"])["class"] != "WRONG"
