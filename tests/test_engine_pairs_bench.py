"""Paired engine tests (Trino, Spark, PostgreSQL, DuckDB) and authored guards: no wrong verdicts.

The pairs are pinned in tests/fixtures/engine_pairs (see tools/engine_pairs_bench.py). Cases are split
into three groups so the work spreads over test workers; ``FLOORS`` (correct verdicts per group) only
ever go up. Pairs on TPC-H run on the data only when the tiny TPC-H file is already cached (the
harness command makes it); the provers and refuters run on every pair.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "engine_pairs_bench.py"
_spec = importlib.util.spec_from_file_location("engine_pairs_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["engine_pairs_bench"] = bench
_spec.loader.exec_module(bench)

# Correct verdicts per group, measured 2026-10-04: trino-a 11, trino-b 17, other 13 (floors leave room for a
# slow machine: the targeted refuter works to a time budget)
FLOORS = {"trino-a": 9, "trino-b": 15, "other": 13}


def _group(case) -> str:
    if case.source != "trino":
        return "other"
    return "trino-a" if case.line < 694 else "trino-b"


def test_fixture_shape():
    cases = bench.load_cases()
    assert len(cases) == 163
    assert len({c.id for c in cases}) == len(cases)
    scored = [c for c in cases if c.scored]
    assert len(scored) == 157
    assert {c.label for c in scored} == {"equivalent", "fixture", "not_equivalent"}
    assert {c.origin for c in cases} == {"original", "adapted", "authored"}
    fixtures = bench.load_fixtures()
    assert {c.fixture for c in cases} <= set(fixtures)
    # every Trino two-literal assertion is recorded with its original text, scored or not
    assert sum(c.source == "trino" for c in cases) == 137
    assert all(c.original and c.original["left"] and c.original["right"] for c in cases if c.source == "trino")
    assert sum(c.held_out for c in scored) == 36


def test_extracts_java_two_query_assertions():
    text = '''
    @Test
    public void testThing()
    {
        assertQuery("SELECT 1");
        assertQuery(
                "SELECT a " +
                        "FROM t", // a comment between the arguments
                "SELECT b FROM u");
        assertQuery(noJoinReordering(), "SELECT 2", "SELECT 3");
        assertQuery(format("SELECT %s", x), "SELECT 4");
    }
    '''
    pairs = bench._java_literal_pairs(text)
    assert [(p["test"], p["left"], p["right"]) for p in pairs] == [
        ("testThing", "SELECT a FROM t", "SELECT b FROM u"),
        ("testThing", "SELECT 2", "SELECT 3"),
    ]


def test_inline_struct_rows_keeps_the_rows():
    sql = "SELECT * FROM UNNEST([STRUCT(1 AS a, NULL AS b), STRUCT(2 AS a, 3 AS b)]) AS t JOIN UNNEST([1, 2]) AS v ON t.a = v"
    out = bench.inline_struct_rows(sql)
    assert "STRUCT" not in out and "UNNEST([1, 2])" in out
    with bench.Engine("closed", {"tables": {}}) as engine:
        assert sorted(engine.db.execute(engine.text(out)).fetchall()) == [(1, None, 1), (2, 3, 2)]


@pytest.mark.parametrize("group", sorted(FLOORS))
def test_no_wrong_verdict(group):
    cases = [c for c in bench.load_cases() if c.scored and _group(c) == group]
    rows = bench.run(cases, skip_tpch=not _tpch_cached())
    assert not [r["id"] for r in rows if r["wrong"]]
    # each pair's label holds on its own data: same rows unless labelled not equivalent
    assert not [r["id"] for r in rows if r.get("fixture_agrees") is False]
    assert not [r["id"] for r in rows if (r.get("execution") or {}).get("error")]
    assert sum(r["correct"] for r in rows) >= FLOORS[group]


def _tpch_cached() -> bool:
    import benchmark_corpora

    return (benchmark_corpora.BENCH_DIR / f"tpch-sf{bench.TPCH_SCALE:g}.duckdb").exists()
