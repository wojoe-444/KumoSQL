"""QueryBooster's experiment rewrites: pinned counts, zero wrong verdicts and per-pair regressions.

The QueryBooster files (GPL-3.0) and WeTune's schema dumps are downloaded at a pinned commit on
first use (see tools/querybooster_bench.py); the tests that need them skip when they cannot be
fetched. The full run goes through ``python tools/querybooster_bench.py``; here a fixed subset
keeps the test short.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "querybooster_bench.py"
_spec = importlib.util.spec_from_file_location("querybooster_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["querybooster_bench"] = bench
_spec.loader.exec_module(bench)

# Outcomes measured on the full run, for a subset that covers every family. A proven pair must stay
# proven, and a refuted pair is a label failure that must stay refuted (never proven).
EXPECTED = {
    "wetune-app-03": "refuted",  # join to o_auth_applications dropped; the id is NULL
    "wetune-app-10": "refuted",  # LEFT to INNER JOIN under an OR that keeps unmatched rows
    "wetune-app-13": "refuted",  # join elimination that needs an undeclared foreign key
    "wetune-app-23": "proven",
    "wetune-app-24": "refuted",
    "train-loj-0": "proven",
    "train-loj-5": "refuted",  # LEFT to INNER JOIN with no filter on the inner side
    "train-join-0": "refuted",
    "train-join-agg-1": "refuted",
    "tweets-cast-0": "refuted",  # CAST(ts AS DATE) = midnight is not ts = midnight
    "tpch-q8-human-1": "proven",  # a pg_hint_plan hint only
}
# A pair proved that a replayed counterexample separates would be a false proof; none is known.
KNOWN_WRONG: set[str] = set()


def _load():
    try:
        return bench.load_cases()
    except OSError as error:
        pytest.skip(f"QueryBooster or WeTune files not available: {error}")


def test_the_pinned_files_have_the_published_counts():
    cases, schemas = _load()
    families = {f: [c for c in cases if c.family == f] for f in bench.FAMILIES}
    assert {f: len(v) for f, v in families.items()} == {"wetune-app": 30, "rule-training": 14, "tweets-cast": 5, "tpch-pg": 19}
    assert sum(len(c.rows) for c in families["tweets-cast"]) == 14
    assert bench.template_rows(bench.fetch()) == 4
    assert len({c.id for c in cases}) == len(cases)
    assert {c.schema_name for c in families["wetune-app"]} == {"broadleaf", "diaspora", "discourse"}
    # one collation per MySQL application, so DuckDB can replay its string comparisons
    assert schemas["broadleaf"].case_insensitive and not schemas["diaspora"].case_insensitive


def test_calcite_tests_are_sqlsolvers_calcite_pairs():
    _load()  # skips when the files cannot be fetched
    overlap = bench.calcite_overlap(bench.fetch()["calcite_tests.csv"])
    assert overlap["rows"] == 228 and overlap["named_in_sqlsolver"] == 228
    assert overlap["same_text_as_sqlsolver"] == 137


def test_subset_outcomes_and_zero_wrong():
    cases, schemas = _load()
    chosen = [c for c in cases if c.id in EXPECTED]
    assert len(chosen) == len(EXPECTED)
    results = {r["id"]: r for r in bench.run(chosen, schemas)}
    assert not [i for i, r in results.items() if r["wrong"] and i not in KNOWN_WRONG]
    assert {i: r["outcome"] for i, r in results.items()} == EXPECTED
    for r in results.values():
        if r["outcome"] == "refuted":
            assert r["counterexample"]["left"] != r["counterexample"]["right"] or r["counterexample"]["how"].startswith("the two sides")


def test_markdown_blocks_lose_the_text_after_the_statement_and_the_public_schema():
    block = 'SELECT "t"."a" FROM "public"."t" "t"\nGROUP BY 1;\n\n-- Explanation:\n/** words; more */\n'
    assert bench.markdown_sql(block) == 'SELECT "t"."a" FROM "t" "t"\nGROUP BY 1'


def test_mysql_collation_is_read_from_table_options():
    binary = "CREATE TABLE a (x int) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;"
    default = "CREATE TABLE b (x int) ENGINE=InnoDB DEFAULT CHARSET=utf8;"
    assert bench.mysql_collation(binary) == "bin"
    assert bench.mysql_collation(default) == "ci"
    assert bench.mysql_collation(binary + "\n" + default) == "mixed"


def test_held_out_split_is_a_fifth_by_hash():
    cases = [bench.Case(f"x-{i}", "wetune-app", "", "", "", "wetune", "inferred") for i in range(500)]
    assert 70 <= sum(c.held_out for c in cases) <= 130
