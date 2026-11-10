"""Floors and regressions for the model-reuse, containment and aggregate-decomposition evals.

Each runner re-checks every positive answer on random databases, so ``wrong`` must stay 0. ``FLOORS``
only ever go up. The full-corpus runs are marked slow; the fast tests cover the adapted cases, the
decomposition corpus and the regressions found while building the evals.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mv_bench = _load("mv_reuse_bench")
containment_bench = _load("containment_bench")
decomposition_bench = _load("decomposition_bench")


def _run(module, args, tmp_path):
    out = tmp_path / "out.json"
    module.main([*args, "--json", str(out)])
    return json.loads(out.read_text(encoding="utf-8"))["summary"]


def test_adapted_shared_model_reuse_floor(tmp_path):
    summary = _run(mv_bench, ["--source", "adapted", "--all"], tmp_path)["adapted"]
    assert summary["wrong"] == 0 and summary["rewritten_beyond_label"] == 0
    assert summary["rewritten_of_expected"] >= 20 and summary["no_rewrite_of_none"] == summary["expect_none"]


def test_aggregate_decomposition_floor(tmp_path):
    summary = _run(decomposition_bench, ["--all"], tmp_path)["all"]
    assert summary["wrong"] == 0
    assert summary["correct"] >= 58
    assert summary["traps_refuted"] == summary["traps"] and summary["impossible_declined"] == summary["impossible_cases"]


@pytest.mark.slow
def test_calcite_materialized_view_floor(tmp_path):
    summary = _run(mv_bench, ["--source", "calcite", "--all"], tmp_path)["calcite"]
    assert summary["wrong"] == 0 and summary["rewritten_beyond_label"] == 0
    assert summary["rewritten_of_expected"] >= 109


@pytest.mark.slow
def test_containment_floor(tmp_path):
    summary = _run(containment_bench, ["--all"], tmp_path)["all"]
    assert summary["wrong"] == 0
    assert summary["decided_correctly"] >= 628


def test_containment_sample_has_no_wrong_answers(tmp_path):
    ids = ["filters.outside-in-lt5", "nulls.split-in-le10", "nulls.gt10_or_null-in-split"]
    args = ["--all"]
    for case_id in ids:
        args += ["--id", case_id]
    summary = _run(containment_bench, args, tmp_path)["all"]
    assert summary["wrong"] == 0 and summary["items"] == 2 * len(ids)


from kumosql.containment import check_containment  # noqa: E402
from kumosql.model_reuse import rewrite_over_model  # noqa: E402

pytestmark = pytest.mark.eval

ORDERS = {"orders": ["id", "amount"]}


def test_prefilter_keeps_a_disjunction_together():
    """The restriction added to q2 is parenthesized: ``a<5 AND (a<5 OR a>10)`` is not ``a<5 AND a<5 OR a>10``."""

    result = check_containment("SELECT id, amount FROM orders WHERE amount < 5 OR amount > 10", "SELECT id, amount FROM orders WHERE amount < 5", schema=ORDERS)
    assert result.status != "contained"


def test_set_and_bag_containment_differ():
    q1, q2 = "SELECT amount FROM orders", "SELECT DISTINCT amount FROM orders"
    assert check_containment(q1, q2, schema=ORDERS, semantics="set").contained
    assert not check_containment(q1, q2, schema=ORDERS, semantics="bag").contained
    assert check_containment(q2, q1, schema=ORDERS, semantics="bag").contained


SALES = {"sales": ["region", "product", "amount"]}


def test_average_is_rebuilt_from_a_sum_and_a_count():
    model = "SELECT region, product, SUM(amount) AS s, COUNT(amount) AS n FROM sales GROUP BY region, product"
    reuse = rewrite_over_model("SELECT region, AVG(amount) FROM sales GROUP BY region", model, schema=SALES)
    assert reuse.rewritten, reuse.reason
    assert "AVG" not in reuse.sql.upper() and "NULLIF" in reuse.sql.upper()


def test_average_is_not_rebuilt_from_a_sum_alone():
    model = "SELECT region, product, SUM(amount) AS s FROM sales GROUP BY region, product"
    assert not rewrite_over_model("SELECT region, AVG(amount) FROM sales GROUP BY region", model, schema=SALES).rewritten


T = {"t": ["a", "b", "c", "d"]}


@pytest.mark.parametrize(
    "query, model",
    [
        ("SELECT a, b FROM t WHERE d > 0 AND (b = 2 OR c = 3) AND a = 1", "SELECT a, b, c, d FROM t WHERE d > 0"),
        ("SELECT a, b FROM t WHERE (b = 2 OR c = 3) AND (a = 1 OR d = 4)", "SELECT a, b, c, d FROM t"),
        ("SELECT a, SUM(b) FROM t GROUP BY a HAVING (SUM(b) > 2 OR a = 3) AND COUNT(*) > 1", "SELECT a, SUM(b) AS s, COUNT(*) AS n FROM t GROUP BY a"),
    ],
)
def test_residual_disjunctions_keep_their_parentheses(query, model):
    reuse = rewrite_over_model(query, model, schema=T)
    assert reuse.rewritten, reuse.reason
    assert "(" in reuse.sql
