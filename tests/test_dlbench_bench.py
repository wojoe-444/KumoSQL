"""DLBench translation pairs (pinned subset): parse and proof floors, no proof contradicted by running it.

The data is pinned in tests/fixtures/dlbench (see tools/dlbench_bench.py). ``FLOORS`` only ever goes up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "dlbench_bench.py"
_spec = importlib.util.spec_from_file_location("dlbench_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["dlbench_bench"] = bench
_spec.loader.exec_module(bench)

FLOORS = {"parsed": 799, "exact_proven": 109}  # measured 799/807 and 109/604


def _pair(source_dbms, target_dbms, source, target, schema=(), renames=None):
    return bench.Pair("BIRDTrans", 1, "db", source_dbms, target_dbms, source, target, "exact_equivalence", list(schema), renames or {})


def test_the_subset_has_every_approximate_pair():
    pairs = bench.load_pairs()
    labels = [p.label for p in pairs]
    assert (len(pairs), labels.count("approximate")) == (807, 203)
    assert {p.label_raw for p in pairs} == {"exact_equivalence", "approximate_equivalence", "Approximate equivalence"}


def test_renamed_columns_are_read_back():
    schema = ["Table: `playstore`\nColumns:\n(`App`, text)\n(`Type`, text)\n"]
    pair = _pair("sqlite", "postgresql", "SELECT App FROM playstore WHERE Type = 'Free'",
                 'SELECT "App" FROM "playstore" WHERE "_Type" = \'Free\';', schema, {"_Type": "Type"})
    left, right = bench.as_source(pair)
    assert left == right
    assert bench.decide(pair)["outcome"] == "proven"


@pytest.mark.parametrize("source, target, left, right, gap", [
    ("sqlite", "postgresql", "SELECT a FROM t WHERE b LIKE 'x%'", "SELECT a FROM t WHERE b LIKE 'x%'", "LIKE and case"),
    ("sqlite", "mysql", "SELECT a FROM t", "SELECT a FROM t", "MySQL string comparison"),
    ("sqlite", "duckdb", "SELECT a / b FROM t", "SELECT a / b FROM t", "division by zero"),
    ("sqlite", "duckdb", "SELECT CAST(a AS REAL) FROM t", "SELECT CAST(a AS REAL) FROM t", "32-bit float"),
    ("sqlite", "clickhouse", "SELECT SUM(a) FROM t", "SELECT SUM(a) FROM t", "ClickHouse defaults"),
])
def test_dialect_gaps_block_proofs(source, target, left, right, gap):
    schema = ["Table: `t`\nColumns:\n(`a`, integer)\n(`b`, text)\n"]
    result = bench.decide(_pair(source, target, left, right, schema))
    assert (result["outcome"], result["detail"]) == ("unknown", f"dialect gap: {gap}")


def test_the_subset_holds_its_floors():
    results = bench.run(bench.load_pairs(), jobs=1)
    assert not [r["id"] for r in results if r["wrong"]]
    assert sum(r["parsed"] for r in results) >= FLOORS["parsed"]
    assert sum(r["outcome"] == "proven" for r in results if r["label"] == "exact") >= FLOORS["exact_proven"]
