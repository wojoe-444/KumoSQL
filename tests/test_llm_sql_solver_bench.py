"""LLM-SQL-Solver's Spider pairs: the 180 negatives are never proved, and the relaxed pairs hold their floor.

The data is pinned in tests/fixtures/llm_sql_solver (see tools/llm_sql_solver_bench.py). ``FLOORS`` only
ever goes up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")

_path = Path(__file__).resolve().parent.parent / "tools" / "llm_sql_solver_bench.py"
_spec = importlib.util.spec_from_file_location("llm_sql_solver_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["llm_sql_solver_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"negatives_refuted": 177, "relaxed_agree": 20}  # measured 177 and 20
# Pairs that were proved before the fixes that came with this eval; none may be proved again
REGRESSIONS = {"negatives-060", "negatives-099", "negatives-124", "negatives-125", "negatives-147"}
TABLES = {"t": {"id": "INTEGER", "name": "TEXT"}, "u": {"ref": "TEXT", "n": "INTEGER"}}


@pytest.fixture(scope="module")
def results():
    return {r["id"]: r for r in bench.run(bench.load_cases())}


def test_the_pinned_files_have_the_published_counts():
    cases = bench.load_cases()
    assert sum(c.suite == "negatives" for c in cases) == 180
    relaxed = [c for c in cases if c.suite == "relaxed"]
    assert (len(relaxed), sum(c.label == "equivalent" for c in relaxed)) == (70, 52)


def test_no_negative_is_proved(results):
    negatives = [r for r in results.values() if r["suite"] == "negatives"]
    assert not [r["id"] for r in negatives if r["wrong"]]
    # the only proofs are pairs whose two queries are the same text (label errors)
    assert {r["id"] for r in negatives if r["outcome"] == "proven"} == {r["id"] for r in negatives if r["label_error"]}
    assert not [i for i in REGRESSIONS if results[i]["outcome"] == "proven"]
    assert sum(r["outcome"] == "refuted" for r in negatives) >= FLOORS["negatives_refuted"]


def test_relaxed_pairs_hold_their_floor(results):
    relaxed = [r for r in results.values() if r["suite"] == "relaxed"]
    assert not [r["id"] for r in relaxed if r["wrong"]]
    agree = sum(r["outcome"] == ("proven" if r["label"] == "equivalent" else "refuted") for r in relaxed)
    assert agree >= FLOORS["relaxed_agree"]


def test_a_double_quoted_name_that_is_no_column_is_a_string():
    sql = 'SELECT name FROM t WHERE name = "Bob" ORDER BY "name"'
    assert bench.adapt(sql, TABLES) == "SELECT name FROM t WHERE name = 'Bob' ORDER BY \"name\""


def test_comparing_text_with_a_number_is_outside_the_proofs():
    assert bench.mixed_type_comparison("SELECT u.ref FROM t JOIN u ON t.id = u.ref", TABLES)
    assert bench.mixed_type_comparison("SELECT id FROM t WHERE id = '3'", TABLES)
    assert not bench.mixed_type_comparison("SELECT u.ref FROM t JOIN u ON t.name = u.ref WHERE u.n > 2", TABLES)


def test_an_order_by_without_limit_is_compared_as_a_list():
    assert bench.for_prover("SELECT a FROM t ORDER BY b") == "SELECT a FROM t ORDER BY b LIMIT 1000000000"
    assert bench.for_prover("SELECT a FROM t ORDER BY b LIMIT 3") == "SELECT a FROM t ORDER BY b LIMIT 3"
    case = bench.Case("negatives", 0, "db", "", "", "inequivalent", TABLES, {"t": ("id",)})
    asc, desc = "SELECT id FROM t ORDER BY id", "SELECT id FROM t ORDER BY id DESC"
    assert bench.differs_on_random_databases(case, asc, desc) == "differs"
    assert bench.differs_on_random_databases(case, "SELECT id FROM t", desc) == "agree"  # bags when the first query has no order


def test_development_runs_read_the_dev_split_and_results_score_every_pair():
    assert bench.choose_split(None, write=False) == "dev"
    assert bench.choose_split(None, write=True) == "all"  # the results files' command: --write-results
    assert bench.choose_split("held-out", write=False) == "held-out"
    with pytest.raises(ValueError):
        bench.choose_split("dev", write=True)
    cases = bench.load_cases()
    dev, held = bench.split_cases(cases, "dev"), bench.split_cases(cases, "held-out")
    assert not any(c.held_out for c in dev) and all(c.held_out for c in held)
    assert (len(dev), len(held), len(bench.split_cases(cases, "all"))) == (193, 57, 250)
