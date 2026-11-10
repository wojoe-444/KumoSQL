"""Arcwise-Plat corrections of BIRD's gold SQL: each original/corrected pair differs, and none is wrongly proved.

The data (CC BY-SA 4.0) is never committed: tools/arcwise_bench.py downloads it at a pinned commit and
checks its SHA-256. Every test that needs it skips when the pinned files cannot be fetched (no network, or
GitHub unreachable); a file that is fetched but does not match its digest fails. ``FLOORS`` only ever goes up.
"""

import importlib.util
import os
import sys
import time
import urllib.error
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")

_path = Path(__file__).resolve().parent.parent / "tools" / "arcwise_bench.py"
_spec = importlib.util.spec_from_file_location("arcwise_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["arcwise_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"pairs": 144, "refuted": 109}  # measured 144 pairs, 111 refuted; two z3 crashes or timeouts of slack
OFFLINE = (urllib.error.URLError, TimeoutError, ConnectionError)

MSCHEMA = """【DB_ID】 shop
【Schema】
# Table: Orders
[
(id:INTEGER, Primary Key, Examples: [1, 2]),
(customer_id:INTEGER, Examples: [7, 8]),
(total:REAL, Examples: [1.5]),
(note:VARCHAR(20), Examples: [x])
]
# Table: Customers
[
(id:INTEGER, Primary Key, Examples: [7, 8]),
(name:TEXT, Examples: [a])
]
【Foreign keys】
Orders.customer_id=Customers.id
Orders.missing=Customers.id
"""


def test_the_pins_are_complete():
    assert len(bench.COMMIT) == 40
    assert set(bench.DATA_FILES) == {"sql-only", "full"}
    digests = [d for _, d in bench.DATA_FILES.values()] + list(bench.SCHEMAS.values())
    assert len(digests) == 13 and all(len(d) == 64 and set(d) <= set("0123456789abcdef") for d in digests)
    assert bench.BASE.endswith(bench.COMMIT + "/")


def test_a_schema_gives_tables_types_keys_and_foreign_keys():
    tables, keys, foreign = bench.parse_mschema(MSCHEMA)
    assert tables == {
        "orders": {"id": "INTEGER", "customer_id": "INTEGER", "total": "REAL", "note": "VARCHAR"},
        "customers": {"id": "INTEGER", "name": "TEXT"},
    }
    assert keys == {"orders": ("id",), "customers": ("id",)}
    assert foreign == (("orders", "customer_id", "customers", "id"),)  # a foreign key to a missing column is dropped


def test_pairs_that_cannot_differ_are_recognised():
    assert bench.same_text("SELECT  a\nFROM t;", "SELECT a FROM t")
    assert not bench.same_text("SELECT a FROM t", "SELECT b FROM t")
    assert bench.same_parsed("SELECT a FROM t INNER JOIN u ON t.x = u.x", "SELECT a FROM t JOIN u ON t.x = u.x")
    assert not bench.same_parsed("SELECT a FROM t LEFT JOIN u ON t.x = u.x", "SELECT a FROM t JOIN u ON t.x = u.x")
    assert not bench.same_parsed("SELECT COUNT(a) FROM t", "SELECT COUNT(DISTINCT a) FROM t")


def test_a_fifth_of_the_ids_is_held_out_and_stays_so():
    def case(suite, question_id):
        return bench.Case(suite, str(question_id), "db", "SELECT 1", "SELECT 2", False, {}, {}, ())

    held = [i for i in range(1000) if case("sql-only", i).held_out]
    assert 150 < len(held) < 250
    assert held == [i for i in range(1000) if case("sql-only", i).held_out]
    assert case("sql-only", 1362).id == "sql-only-1362"


def _slow(case, connection):
    time.sleep(60)


def _dies(case, connection):
    os._exit(1)


def test_a_pair_that_hangs_or_crashes_is_unknown_never_decided(monkeypatch):
    case = bench.Case("sql-only", "1", "db", "SELECT 1", "SELECT 2", False, {}, {}, ())
    monkeypatch.setattr(bench, "_worker", _slow)
    hung = bench.decide_guarded(case, timeout=1)
    monkeypatch.setattr(bench, "_worker", _dies)
    crashed = bench.decide_guarded(case, timeout=30)
    for result, how in ((hung, "timeout"), (crashed, "crash")):
        assert (result["outcome"], result["how"], result["wrong"]) == ("unknown", how, False)


@pytest.fixture(scope="module")
def loaded():
    try:
        return bench.load_cases()
    except OFFLINE as error:
        pytest.skip(f"the pinned Arcwise-Plat files cannot be fetched: {error}")


def test_the_pinned_files_give_the_expected_pairs(loaded):
    cases, skipped = loaded
    assert len(cases) == FLOORS["pairs"]
    assert (skipped["sql-only records"], skipped["full records"], skipped["full pairs already in sql-only"]) == (498, 498, 72)
    assert len({c.id for c in cases}) == len(cases)
    assert not any(bench.same_text(c.original, c.corrected) or bench.same_parsed(c.original, c.corrected) for c in cases)
    assert {c.suite for c in cases} == {"sql-only", "full"}
    assert all(c.original != c.corrected and c.tables for c in cases)
    assert 15 < sum(c.held_out for c in cases) < 45


def test_count_of_a_column_against_count_distinct_is_refuted_on_a_database(loaded):
    case = next(c for c in loaded[0] if c.id == "sql-only-1362")
    assert "COUNT(distinct city)" in case.corrected and not case.held_out
    result = bench.decide_guarded(case)
    assert (result["outcome"], result["verdict"], result["wrong"]) == ("refuted", "refuted", False)


@pytest.fixture(scope="module")
def results(loaded):
    return bench.run(loaded[0], jobs=2)


def test_no_pair_is_wrongly_proved(results):
    assert not [r["id"] for r in results if r["wrong"]]
    proved = [r["id"] for r in results if r["outcome"] == "proven"]
    assert all(i in bench.COSMETIC and i not in bench.FALSE_PROOFS for i in proved)


def test_the_corrections_hold_their_refutation_floor(results):
    assert len(results) == FLOORS["pairs"]
    assert sum(r["outcome"] == "refuted" for r in results) >= FLOORS["refuted"]
    assert not [r["id"] for r in results if r["outcome"] == "unsupported"]
