"""Logos' TPC-H, DSB and TPC-DS rewrite pairs (tools/logos_bench.py): pins, adaptations, zero wrong proofs.

The pairs are fetched at a pinned commit on first use (the TPC-derived query text is never committed);
the eval tests skip when it cannot be fetched. Without the generated benchmark databases the data check
falls back to the small databases alone. ``FLOOR`` only ever goes up.
"""

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

_path = Path(__file__).resolve().parent.parent / "tools" / "logos_bench.py"
_spec = importlib.util.spec_from_file_location("logos_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["logos_bench"] = bench
_spec.loader.exec_module(bench)

FLOOR = {"rbot-tpch": 15}  # proved R-Bot TPC-H pairs; measured 16 of 21 (the 22nd is a label failure)


def test_manifest_is_pinned():
    data = bench.fixture()
    families = Counter(case["family"] for case in data["cases"].values())
    assert families == {"rbot-tpch": 22, "rbot-dsb": 37, "tpcds-variants": 14}
    assert data["source"]["commit"] == bench.COMMIT
    for case in data["cases"].values():
        assert case["source"] in data["files"] and case["target"] in data["files"]
    assert set(data["label_failures"]) <= set(data["cases"]) and set(data["known_wrong"]) <= set(data["cases"])
    held = [case_id for case_id in data["cases"] if bench.corpora.held_out(case_id)]
    assert 0 < len(held) < len(data["cases"]) // 3


def test_tpcds_kit_spellings_are_adapted():
    text = "-- start\nselect top 100 a from t where d between cast('2000-01-01' as date) and (cast('2000-01-01' as date) + 30 days) order by a;\n"
    (sql,), adaptations = bench.statements(text)
    assert "LIMIT 100" in sql and "INTERVAL '30 DAY'" in sql and "TOP" not in sql.upper()
    assert adaptations == ["date + n days as an interval", "TOP n as LIMIT n"]
    plain, none = bench.statements("select a from t; select b from u;")
    assert len(plain) == 2 and none == []


def test_outcomes():
    def case(proof, data="same", small="none"):
        return {"statements": [{"proof": {"status": proof}, "data": {"status": data}, "small": {"status": small}}]}

    assert bench.outcome(case("proven")) == "proven"
    assert bench.outcome(case("proven", data="different")) == "wrong"
    assert bench.outcome(case("proven", small="different")) == "wrong"
    assert bench.outcome(case("proven", data="ties")) == "proven"  # a different pick among ties is no difference
    assert bench.outcome(case("unknown", data="different")) == "refuted"
    assert bench.outcome(case("unknown", data="unconfirmed")) == "unknown"
    assert bench.outcome(case("unsupported")) == "unsupported"


def _cases():
    pytest.importorskip("z3")
    pytest.importorskip("duckdb")
    try:
        return bench.load_cases()
    except OSError as error:
        pytest.skip(f"Logos pairs not available: {error}")


def test_tpch_pairs_and_label_failures(monkeypatch):
    """The R-Bot TPC-H pairs: no wrong proof and the proof floor. Every pair a database showed different
    (a label failure) stays unproved; for those only the proof step runs here, with a shorter timeout."""

    cases = _cases()
    records = bench.run([c for c in cases if c.family == "rbot-tpch"])
    outcomes = {r["id"]: r["outcome"] for r in records}
    assert not [i for i, o in outcomes.items() if o == "wrong"], bench.line(records)
    assert sum(o == "proven" for o in outcomes.values()) >= FLOOR["rbot-tpch"], bench.line(records)

    monkeypatch.setattr(bench, "PROOF_TIMEOUT_S", 30)
    failures = bench.fixture()["label_failures"]
    root = bench.core_root()
    for case in cases:
        if case.id not in failures:
            continue
        workload = bench.load_workload(root / bench.FAMILIES[case.family][0] / "create_tables.sql")
        for left, right in case.statements:
            assert bench.prove(left, right, workload)["status"] != "proven", case.id
