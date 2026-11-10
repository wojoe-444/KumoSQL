"""The fuzzing, rewrite, view-reuse and containment adapters of tools/proof_recheck.py.

Each adapter proves a pair as its eval does and returns the Case that eval's own executed check runs. These
tests check that the modules import and register their evals, that the item lists are what the fixtures hold, and
that one tiny pair per eval is proven and turned into a Case the search runs (a tiny budget; no pair here is
expected to differ). Nothing is written to a shared file and no module state is shared between tests.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("z3")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "src"))
logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

from recheck import engine  # noqa: E402
from recheck import fuzz_rewrites as fr  # noqa: E402
from recheck import reuse_containment as rc  # noqa: E402

pytestmark = pytest.mark.eval

FUZZ_EVALS = {"sqlancer-tlp-norec", "unsafe-rewrite-detection", "rewrite-composition", "join-rewrites", "constraint-rewrites"}
REUSE_EVALS = {"mv-reuse-calcite", "containment", "aggregate-decomposition", "mv-benchmark"}


def _verdict(case, budget: int = 60) -> dict:
    record = engine.recheck(case, budget=budget, seconds=10, exhaustive_cap=40)
    assert record["verdict"] in ("survived", "differs"), record
    return record


def test_adapters_register_their_evals():
    assert set(fr.ADAPTERS) == FUZZ_EVALS
    assert set(rc.ADAPTERS) == REUSE_EVALS
    for name, adapter in {**fr.ADAPTERS, **rc.ADAPTERS}.items():
        assert adapter.name == name


def test_proof_recheck_lists_every_adapter():
    import proof_recheck

    assert (FUZZ_EVALS | REUSE_EVALS) <= set(proof_recheck.adapters())


# --- fuzzing and rewrite evals -------------------------------------------------------------------------


def test_fuzz_suites_list_the_eval_pairs():
    unsafe = fr.ADAPTERS["unsafe-rewrite-detection"].items()
    assert len(unsafe) == 560  # tools/unsafe_fuzz.py unsafe --count 20 --seed 1
    assert {i["pair"] for i in unsafe} and len({i["pair"] for i in unsafe}) == len(unsafe)
    assert any(i["held_out"] for i in unsafe)
    fuzz = fr.ADAPTERS["sqlancer-tlp-norec"]
    assert (fuzz.suite, fuzz.count, fuzz.seed) == ("fuzz", 60, 2)


def test_fuzz_suite_pair_becomes_a_case():
    adapter = fr.ADAPTERS["unsafe-rewrite-detection"]
    item = {
        "pair": "tiny", "family": "tiny", "expect": "equivalent", "held_out": True,
        "left": "SELECT a FROM t WHERE a > 1", "right": "SELECT a FROM t WHERE NOT (a <= 1)",
    }
    case = adapter.case(item)
    assert case is not None and case.held_out and case.dialect == "bigquery"
    assert set(case.tables) == {"t", "u"} and case.setup
    assert _verdict(case)["verdict"] == "survived"


def test_fuzz_suite_skips_a_pair_the_prover_does_not_prove():
    adapter = fr.ADAPTERS["unsafe-rewrite-detection"]
    item = {"pair": "wrong", "family": "tiny", "expect": "different", "held_out": False,
            "left": "SELECT a FROM t WHERE a > 1", "right": "SELECT a FROM t WHERE a > 2"}
    assert adapter.case(item) is None


def test_composition_step_becomes_a_case():
    adapter = fr.Compose(count=4, seed=21)
    steps = adapter.items()
    assert all({"pair", "rule", "left", "right"} <= set(s) for s in steps)
    item = {"pair": "tiny", "rule": "tiny", "eval_oracle": "agrees",
            "left": "WITH x AS (SELECT a FROM t) SELECT a FROM x", "right": "SELECT a FROM t"}
    case = adapter.case(item)
    assert case is not None
    assert _verdict(case)["verdict"] == "survived"


def test_join_rewrites_pair_runs_the_evals_own_sql():
    import join_rewrite_bench as jb

    adapter = fr.ADAPTERS["join-rewrites"]
    items = adapter.items()
    assert len(items) == len(jb.load_pairs(False)) + len(jb.load_pairs(True))
    assert sum(i["held_out"] for i in items) == len(jb.load_pairs(True))
    item = next(i for i in items if i["pair"] == "cross_vs_comma")
    case = adapter.case(item)
    assert case is not None and not case.held_out
    assert (case.left, case.right) == (jb._duckdb(item["left"]), jb._duckdb(item["right"]))
    assert not case.setup
    assert _verdict(case)["verdict"] == "survived"


def test_constraint_rewrites_pair_keeps_the_declared_facts():
    adapter = fr.ADAPTERS["constraint-rewrites"]
    items = adapter.items()
    kinds = {i["facts"] for i in items}
    assert kinds == {"offered", "needed", "without"}
    assert any(i["held_out"] for i in items)
    main = next(i for i in items if i["facts"] == "offered" and not i["held_out"])
    found = None
    for item in [i for i in items if i["facts"] == "offered" and not i["held_out"]][:12]:
        found = adapter.case(item)
        if found is not None and any(t.keys or t.foreign_keys or any(c.not_null for c in t.columns) for t in found.tables.values()):
            break
    assert found is not None, main
    assert found.meta["kind"] == "offered" and found.meta["facts"]
    assert _verdict(found)["verdict"] == "survived"


# --- view reuse, containment, decomposition and the MV workloads -----------------------------------------


def test_mv_reuse_pair_runs_the_evals_check():
    adapter = rc.ADAPTERS["mv-reuse-calcite"]
    items = adapter.items()
    assert len(items) == 225  # tools/mv_reuse_bench.py --all: 196 Calcite cases and 29 adapted ones
    assert {i["source"] for i in items} == {"calcite", "adapted"}
    item = next(i for i in items if i["pair"] == "shared.same-query")
    case = adapter.case(item)
    assert case is not None and case.dialect == "postgres" and case.meta["strategy"]
    assert case.meta["as_written"]  # the view as written is kept for triage
    assert _verdict(case)["verdict"] == "survived"


def test_containment_pair_uses_the_containment_mode():
    adapter = rc.ADAPTERS["containment"]
    items = adapter.items()
    assert len(items) == 636
    assert {i["semantics"] for i in items} == {"set", "bag"}
    modes = {}
    for semantics in ("set", "bag"):
        item = next(i for i in items if i["id"] == "filters.gt10-in-gt5" and i["semantics"] == semantics)
        case = adapter.case(item)
        assert case is not None
        modes[semantics] = case.mode
        assert _verdict(case)["verdict"] == "survived"
    assert modes == {"set": "contained-set", "bag": "contained"}
    refuted = next(i for i in items if i["id"] == "filters.gt10-in-lt5" and i["semantics"] == "bag")
    assert adapter.case(refuted) is None


def test_decomposition_pair_compares_the_raw_target_query():
    adapter = rc.ADAPTERS["aggregate-decomposition"]
    items = adapter.items()
    assert {i["kind"] for i in items} == {"synth", "trap"}
    item = next(i for i in items if i["pair"] == "basic.sum#synth")
    case = adapter.case(item)
    assert case is not None
    assert case.left == rc.duck(item["query"])
    assert _verdict(case)["verdict"] == "survived"


def test_mv_benchmark_pair_runs_a_view_rewrite():
    import mv_workload_bench as mw

    try:
        queries, schema = mw.load_workload("job", None, None)
        given = {n: s for n, s in mw.load_given(None).items()}
    except Exception as error:  # noqa: BLE001 - the workload files are downloaded on first use
        pytest.skip(f"benchmark data not available: {error}")
    from kumosql import view_candidates as vc

    views = [v for v in (mw.given_record(n, s, schema) for n, s in given.items()) if v]
    adapter = rc.ADAPTERS["mv-benchmark"]
    case = None
    for ident, record in queries.items():
        graph = vc.graph_of(record["sql"], schema.columns)
        usable = [v for v in views if mw.applicable(graph, v["tables"])]
        if not usable:
            continue
        usable.sort(key=lambda v: (-len(v["tables"]), v["name"]))
        item = {"pair": f"given/{ident}", "workload": "job", "track": "given", "sql": record["sql"], "views": usable, "held_out": record["held_out"]}
        case = adapter.case(item)
        if case is not None:
            break
    assert case is not None and case.meta["track"] == "given"
    assert _verdict(case, budget=20)["verdict"] in ("survived", "differs")
