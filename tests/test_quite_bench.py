"""QUITE's published LLM rewrites (tools/quite_bench.py): zero wrong proofs and floors on a pinned sample.

The data is downloaded on first use from a pinned commit (the repository has no licence); the
tests that need it skip when it cannot be fetched. ``FLOORS`` only ever goes up. The full run
goes through ``python tools/quite_bench.py``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "quite_bench.py"
_spec = importlib.util.spec_from_file_location("quite_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["quite_bench"] = bench
_spec.loader.exec_module(bench)

SAMPLE = 48
# measured 26 proved and 2 refuted on this sample (no generated instance); the margin absorbs a slow machine
FLOORS = {"proved": 24, "refuted": 2}


def _pairs():
    try:
        return bench.load_pairs()
    except OSError as error:
        pytest.skip(f"QUITE data not available: {error}")


DDL = """
CREATE TABLE region (r_regionkey integer NOT NULL, r_name char(25) NOT NULL, PRIMARY KEY (r_regionkey));
CREATE TABLE nation (
    n_nationkey integer NOT NULL, n_name char(25) NOT NULL, n_regionkey integer NOT NULL,
    PRIMARY KEY (n_nationkey),
    FOREIGN KEY (n_regionkey) REFERENCES region (r_regionkey) ON DELETE CASCADE
);
CREATE TABLE emp (empno INTEGER NOT NULL, mgr INTEGER);
ALTER TABLE emp ADD CONSTRAINT emp_pkey PRIMARY KEY (empno);
"""


def test_the_pinned_files_have_the_published_counts():
    _pairs()  # skips the test when the data cannot be fetched
    rewrites = bench.load_rewrites()
    assert len(rewrites) == 4160 and sum(not r.flag for r in rewrites) == 587
    assert {b: sum(r.benchmark == b for r in rewrites) for b in bench.BENCHMARKS} == {
        "tpch": 819,
        "dsb": 2028,
        "calcite": 754,
        "sqlstorm": 559,
    }
    assert len({r.system for r in rewrites}) == 13
    pairs = bench.load_pairs()
    assert len(pairs) == 3174 and sum(p.held_out for p in pairs) == 671
    assert sum(p.label == "mixed" for p in pairs) == 2
    assert sum(p.reason == "documented" for p in pairs) == 2


def test_schema_reads_keys_not_null_and_foreign_keys():
    tables = bench.load_schema(DDL)
    assert tables["nation"].keys == [("n_nationkey",)]
    assert tables["nation"].foreign == [(("n_regionkey",), "region", ("r_regionkey",))]
    assert tables["emp"].keys == [("empno",)] and tables["emp"].not_null == {"empno"}
    assert "mgr" not in tables["emp"].not_null


def test_replayed_databases_respect_the_schema():
    tables = bench.load_schema(DDL)
    orphan = {
        "region": [(1, "EUROPE")],
        "nation": [(1, "FRANCE", 2), (1, "SPAIN", 1)],
        "emp": [],
    }
    assert not bench.legal(orphan, tables)
    fixed = bench.repair(orphan, tables)
    assert bench.legal(fixed, tables) and fixed["nation"] == [(1, "FRANCE", 1)]


def test_tie_sensitive_and_random_queries_are_recognised():
    import sqlglot

    def kind(sql):
        return bench.determinism(sqlglot.parse_one(sql, read="postgres"))

    assert kind("SELECT a FROM t ORDER BY a LIMIT 3") == "fixed"
    assert kind("SELECT a, b FROM t ORDER BY a LIMIT 3") == "ties"
    assert kind("SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t") == "ties"
    assert kind("SELECT a FROM t WHERE random() < 0.5") == "random"
    assert "CAST('2024-06-01' AS DATE)" in bench.replay_sql(
        "SELECT current_date FROM t"
    )


def test_flags_are_classified_by_how_the_run_went():
    def rewrite(original, rewritten):
        return bench.Rewrite("tpch", "x", "1", "a", "b", False, original, rewritten)

    assert rewrite(300, 2.0).why == "timeout"
    assert rewrite(4.5, 4.5).why == "error"
    assert rewrite(4.5, 1.2).why == "rows"


def test_pinned_sample_has_no_wrong_proof():
    # development pairs only, and no generated TPC-H instance, so the floors do not depend on tpchgen-cli
    pairs = bench.sample([p for p in _pairs() if not p.held_out], SAMPLE)
    report = bench.run(pairs, workers=1, instance=None)
    wrong = [pairs[i].key for i, v in report.verdicts.items() if v.outcome == "wrong"]
    assert not wrong, wrong
    proved = report.counts(label="equal")["proven"]
    refuted = report.counts()["refuted"]
    assert proved >= FLOORS["proved"], report.equal_line()
    assert refuted >= FLOORS["refuted"], report.counts()
