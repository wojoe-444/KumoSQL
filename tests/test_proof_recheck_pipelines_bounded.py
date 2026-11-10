"""The re-check adapters for the pipeline, refactor, fix, output-property and bounded evals.

Each adapter has to import, list its items, and turn one tiny pair into a ``Case`` the heavy search can
run (tools/recheck/pipelines_refactors.py and tools/recheck/bounded.py). A pair the eval does not count as
proven gives no case. The search itself is covered by tests/test_proof_recheck.py; here a few budgeted
runs show the cases are meaningful: an equivalent pair survives and a known-wrong one is found.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

from recheck import engine  # noqa: E402
from recheck import pipelines_refactors as pr  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

pytestmark = pytest.mark.eval

PIPELINE_EVALS = [
    "sqlfluff-semantic-fixes", "duplicate-exact", "shared-refactors-proof", "pipeline-equivalence", "incremental-proofs",
    "table-minimization", "output-properties", "output-properties-adapted",
]
BOUNDED_EVALS = [
    "bounded-sqlsolver-calcite", "bounded-sqlsolver-spark", "bounded-sqlsolver-tpch", "bounded-sqlsolver-tpcc", "bounded-qed",
    "bounded-rbot", "bounded-cosette", "bounded-spes", "bounded-literature", "bounded-calcite", "bounded-leetcode", "bounded-singh",
]


def _verdict(case: Case, budget: int = 120) -> str:
    return engine.recheck(case, budget=budget, seconds=20)["verdict"]


def test_every_adapter_is_registered_with_items_and_case():
    for name in PIPELINE_EVALS:
        adapter = pr.ADAPTERS[name]
        assert adapter.name == name and callable(adapter.items) and callable(adapter.case)
    bounded = pytest.importorskip("recheck.bounded")
    for name in BOUNDED_EVALS:
        adapter = bounded.ADAPTERS[name]
        assert adapter.name == name and callable(adapter.items) and callable(adapter.case)


def test_tools_discovers_the_adapters():
    import proof_recheck

    found = proof_recheck.adapters()
    for name in PIPELINE_EVALS + BOUNDED_EVALS:
        assert name in found


# ------------------------------------------------------------------ sqlfluff semantic fixes


def test_sqlfluff_pair_gives_a_case_in_the_fixtures_dialect_and_survives():
    adapter = pr.ADAPTERS["sqlfluff-semantic-fixes"]
    items = adapter.items()
    assert len(items) > 300 and all({"pair", "held_out"} <= set(i) for i in items)
    item = next(i for i in items if i["pair"] == "AL01/test_fail_explicit")
    case = adapter.case(item)
    assert case is not None and case.eval == "sqlfluff-semantic-fixes" and case.meta["label"] == ""
    assert _verdict(case) == "survived"


def test_sqlfluff_pair_the_prover_does_not_prove_gives_no_case():
    adapter = pr.ADAPTERS["sqlfluff-semantic-fixes"]
    items = {i["pair"]: i for i in adapter.items()}
    # a fix that changes meaning by design (= NULL becomes IS NULL) is refuted; one the prover leaves unknown is not proved
    assert adapter.case(items["CV05/test_not_equals_null_upper"]) is None
    assert adapter.case(items["CV11/test_fail_tsql_convert_still_rewritten"]) is None


# ------------------------------------------------------------------ duplicate and shared-refactor items


@pytest.mark.parametrize("name", ["duplicate-exact", "shared-refactors-proof"])
def test_dup_items_become_cases_run_as_the_eval_runs_them(name):
    adapter = pr.ADAPTERS[name]
    base = "SELECT id, amt FROM `proj.raw.t001` AS o WHERE o.amt > 3 AND o.status = 'paid'"
    same = "SELECT id, amt FROM (SELECT t.id, t.amt FROM `proj.raw.t001` AS t WHERE 'paid' = t.status AND NOT t.amt <= 3) AS _shared"
    wrong = "SELECT id, amt FROM `proj.raw.t001` AS o WHERE o.amt >= 3 AND o.status = 'paid'"
    case = adapter.case({"pair": "x=y", "left": base, "right": same, "held_out": False})
    assert case is not None and list(case.tables) == ["t001"] and "proj" not in case.left  # qualifiers dropped as dup_bench does
    assert case.setup == ()  # the eval runs plain DuckDB
    assert _verdict(case) == "survived"
    case = adapter.case({"pair": "x=z", "left": base, "right": wrong, "held_out": False})
    assert _verdict(case, 300) == "differs"
    assert adapter.case({"pair": "none", "left": base, "right": None, "held_out": False}) is None


def test_standalone_copy_keeps_the_ctes_it_reads():
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one("WITH a AS (SELECT id FROM t001), b AS (SELECT id FROM a WHERE id > 1) SELECT id FROM b", read="bigquery")
    cte_select = next(c for c in tree.find_all(exp.CTE) if c.alias == "b").this
    whole = pr._standalone(cte_select)
    assert whole is not None and "WITH a AS" in whole.sql(dialect="bigquery")


# ------------------------------------------------------------------ pipeline equivalence


def test_pipeline_equivalence_output_pair_is_a_case_and_survives():
    adapter = pr.ADAPTERS["pipeline-equivalence"]
    items = adapter.items()
    assert len(items) > 200
    proven = next(i for i in items if i["pair"] == "filter_upstream/push_to_first/1::final")
    case = adapter.case(proven)
    assert case is not None and case.dialect == "bigquery" and "an__final" in case.left and "raw__orders" in case.left
    assert _verdict(case, 80) == "survived"
    # an exposed intermediate the refactor leaves unproved is not counted by the eval, so there is no case
    assert adapter.case(next(i for i in items if i["pair"] == "filter_upstream/exposed_intermediate/1::stg1")) is None


# ------------------------------------------------------------------ incremental proofs


def test_incremental_safe_case_survives_and_diverging_ones_are_found():
    import incremental_bench as ib

    adapter = pr.ADAPTERS["incremental-proofs"]
    items = adapter.items()
    assert len(items) == len(ib.load_cases()) and any(i["held_out"] for i in items)
    safe = adapter.case(next(i for i in items if i["pair"] == "watermark-strict-coalesce"))
    assert safe is not None and safe.legal is not None and safe.meta["rule"].startswith("R1")
    assert _verdict(safe, 100) == "survived"
    # a diverging case is not counted as proven ...
    assert adapter.case(next(i for i in items if i["pair"] == "watermark-strict-late-arrival")) is None
    # ... and the model of one run shows its divergence, so the cases built are not vacuous
    for name in ("watermark-strict-late-arrival", "merge-error-duplicate-of-newest"):
        case = next(c for c in ib.load_cases() if c["id"] == name)
        left, right, tables, legal, meta = pr._incremental_case(case, SimpleNamespace(rule=""))
        built = Case("incremental-proofs", name, left, right, tables, legal=legal, setup=pr.bq_setup(), meta=meta)
        assert _verdict(built, 300) == "differs", name


# ------------------------------------------------------------------ table minimization


def test_minimization_items_and_pair_of_a_reference_and_a_trap():
    import minimization_cases as mc

    adapter = pr.ADAPTERS["table-minimization"]
    items = adapter.items()
    assert len(items) == len(mc.load_cases()) and any(i["held_out"] for i in items)
    case = next(c for c in mc.load_cases() if c["traps"] and not all(mc.unchanged(c["tables"], c["reference"]["tables"], p) for p in c["protected"]))
    changed = [p for p in case["protected"] if not mc.unchanged(case["tables"], case["reference"]["tables"], p)]
    left, right, tables = pr.minimization_pair(case, {k.lower(): v for k, v in case["reference"]["tables"].items()}, changed)
    assert _verdict(Case("table-minimization", case["id"], left, right, tables), 100) == "survived"
    trap = {k.lower(): v for k, v in case["traps"][0]["tables"].items()}
    names = [p for p in case["protected"] if not mc.unchanged(case["tables"], trap, p)]
    left, right, tables = pr.minimization_pair(case, trap, names)
    assert _verdict(Case("table-minimization", case["id"], left, right, tables), 400) == "differs"


# ------------------------------------------------------------------ output properties


def test_output_property_claims_become_a_violation_query():
    adapter = pr.ADAPTERS["output-properties"]
    items = adapter.items()
    assert len(items) > 100 and any(i["held_out"] for i in items)
    case = adapter.case(next(i for i in items if i["pair"] == "grp-count"))
    assert case is not None and case.left.startswith("SELECT '[") and case.right.endswith("WHERE FALSE")
    assert _verdict(case, 80) == "survived"


def test_a_false_claim_is_found_by_its_violation_query():
    tables = {"t": Table("t", [Column("a", "int"), Column("b", "int")])}
    left = pr._violations("SELECT a, b FROM t", [["non_null", "a"], ["unique", ["b"]], ["rows", "at_most_one"]], ["a", "b"], None)
    case = Case("output-properties", "false", left, f"SELECT * FROM ({left}) AS kumo_v WHERE FALSE", tables)
    assert _verdict(case, 100) == "differs"


def test_adapted_output_property_queries_list_and_build():
    adapter = pr.ADAPTERS["output-properties-adapted"]
    items = adapter.items()
    assert len(items) > 700 and items[0]["pair"].startswith("calcite#")
    built = [adapter.case(i) for i in items[2:6]]
    assert any(c is not None and c.dialect == "mysql" for c in built)


# ------------------------------------------------------------------ bounded


def test_bounded_pair_suite_gives_a_case_capped_at_three_rows():
    bounded = pytest.importorskip("recheck.bounded")
    pytest.importorskip("z3")
    adapter = bounded.ADAPTERS["bounded-sqlsolver-tpcc"]
    items = adapter.items()
    assert len(items) == 19
    case = adapter.case(items[0])
    assert case is not None and case.meta["max_rows"] == 3 and case.legal is not None
    table = next(iter(case.tables.values()))
    row = tuple(None if c.not_null is False else 1 for c in table.columns)
    assert not case.legal({table.name: [row, row, row, row]})  # four rows are outside what the bounded check covers
    assert _verdict(case, 60) == "survived"


# one pair per bounded eval that the eval counts as bounded at 3 rows (a small, fast one)
BOUNDED_PAIRS = {
    "bounded-sqlsolver-calcite": "calcite-11", "bounded-sqlsolver-spark": "spark-4", "bounded-sqlsolver-tpcc": "tpcc-7",
    "bounded-qed": "testAggregateExtractProjectRule", "bounded-rbot": "testEmptyFilterProjectUnion",
    "bounded-cosette": "testAggregateConstantKeyRule2", "bounded-spes": "testReduceConstantsRequiresExecutor",
    "bounded-literature": "4", "bounded-calcite": "5", "bounded-leetcode": "192", "bounded-singh": "c087a1b3a419",
}


@pytest.mark.parametrize("name", sorted(BOUNDED_PAIRS))
def test_each_bounded_eval_turns_one_pair_into_a_capped_case(name):
    bounded = pytest.importorskip("recheck.bounded")
    pytest.importorskip("z3")
    import verieql_bench as vb

    adapter = bounded.ADAPTERS[name]
    try:
        items = adapter.items()
    except vb.DataUnavailable as error:  # the VeriEQL files are downloaded once; no network and nothing cached
        pytest.skip(str(error))
    item = next(i for i in items if i["pair"] == BOUNDED_PAIRS[name])
    case = adapter.case(item)
    assert case is not None, name
    assert case.eval == name and case.meta["max_rows"] == 3 and case.legal is not None
    assert case.left and case.right and case.tables
    assert engine.recheck(case, budget=40, seconds=20)["verdict"] in ("survived", "differs", "unrunnable")


def test_bounded_singh_pair_the_suite_refutes_has_no_bounded_verdict():
    bounded = pytest.importorskip("recheck.bounded")
    pytest.importorskip("z3")
    adapter = bounded.ADAPTERS["bounded-singh"]
    items = adapter.items()
    assert len(items) > 900 and all({"pair", "position"} <= set(i) for i in items)
    assert adapter.case(next(i for i in items if i["pair"] == "38268449e30e")) is None


def test_bounded_tpch_lists_its_pairs():
    bounded = pytest.importorskip("recheck.bounded")
    assert len(bounded.ADAPTERS["bounded-sqlsolver-tpch"].items()) == 22


def test_bounded_veri_eql_suite_lists_when_its_files_are_cached():
    bounded = pytest.importorskip("recheck.bounded")
    pytest.importorskip("z3")
    import verieql_bench as vb

    try:
        items = bounded.ADAPTERS["bounded-literature"].items()
    except vb.DataUnavailable as error:
        pytest.skip(str(error))
    assert len(items) == 64 and all("pair" in i for i in items)
