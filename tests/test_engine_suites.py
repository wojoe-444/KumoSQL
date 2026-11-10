"""The engine-suite harness: parsing, classification and the correctness gate."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import engine_suites as suites  # noqa: E402

pytestmark = pytest.mark.eval

SLT = """\
# tiny synthetic file in the SQLLogicTest format
statement ok
CREATE TABLE t(a INTEGER, b INTEGER)

statement ok
INSERT INTO t VALUES (1, 10), (2, NULL), (3, 30)

query II rowsort
SELECT a, b FROM t WHERE 1 = 1 AND (a > 1)
----
2
NULL
3
30

query I nosort
WITH unused AS (SELECT 1 AS z) SELECT a FROM t ORDER BY a
----
1
2
3

query I
SELECT random()
----
0

skipif duckdb
query I
SELECT 99
----
99
"""


def test_parser_reads_statements_queries_and_skips():
    records, skipped = suites.parse_slt(SLT)
    assert skipped is None
    assert [r.kind for r in records] == ["statement", "statement", "query", "query", "query", "query"]
    assert records[2].sort_mode == "rowsort" and records[2].expected == ["2", "NULL", "3", "30"]
    assert records[-1].skip


def test_loops_expand_and_unknown_requirements_skip_the_file():
    records, skipped = suites.parse_slt("loop i 0 2\n\nstatement ok\nSELECT ${i}\n\nendloop\n")
    assert skipped is None and [r.sql for r in records] == ["SELECT 0", "SELECT 1"]
    assert suites.parse_slt("require httpfs\n")[1] == "require httpfs"


def test_run_file_classifies_cases_and_finds_no_wrong(tmp_path):
    (tmp_path / "tiny.test").write_text(SLT)
    cases = suites.run_file(("duckdb-slt", str(tmp_path), "tiny.test"))
    plain = {c.sql: c for c in cases if c.variant == "plain"}
    assert plain["SELECT a, b FROM t WHERE 1 = 1 AND (a > 1)"].status == "transformed"
    assert plain["SELECT random()"].status == "unsupported"  # random() is not repeatable
    assert not [c for c in cases if c.status in ("wrong", "error")]
    # the wrapped variants give the rules work to do, and the results still match
    assert any(c.status == "transformed" for c in cases if c.variant != "plain")
    assert all(c.expected_match for c in plain.values() if c.expected_match is not None)


def test_a_behaviour_changing_rewrite_is_scored_wrong(monkeypatch, tmp_path):
    import sqlglot

    class Result:
        verification = type("V", (), {"status": type("S", (), {"value": "proven"})()})()
        steps = ()
        sql = "SELECT a FROM t WHERE a > 100"

    monkeypatch.setattr(suites, "apply_rules", lambda *a, **k: Result())
    (tmp_path / "tiny.test").write_text(SLT)
    cases = suites.run_file(("duckdb-slt", str(tmp_path), "tiny.test"))
    wrong = [c for c in cases if c.status == "wrong"]
    assert wrong
    summary = suites.summarise(cases)
    assert summary["all"]["wrong"] == len(wrong) and summary["all"]["wrong_but_proven"] == len(wrong)
    assert sqlglot  # the monkeypatched pipeline returned a different query, not an error


def test_fixture_parser_pairs_inputs_with_expected_outputs():
    text = "-- heading\n# dialect: duckdb\nSELECT a FROM x;\nSELECT x.a FROM x;\n\nSELECT 1;\nSELECT 2;\n"
    items = suites.parse_fixture(text, identity=False)
    assert [(m.get("dialect"), q, e) for m, q, e, _ in items] == [
        ("duckdb", "SELECT a FROM x", "SELECT x.a FROM x"),
        (None, "SELECT 1", "SELECT 2"),
    ]
    assert [q for _, q, _, _ in suites.parse_fixture("SELECT 1\nSELECT 2\n", identity=True)] == ["SELECT 1", "SELECT 2"]


def test_duplicates_collapse_and_keep_provenance():
    def case(i, file):
        return suites.CaseResult(f"s:{file}:{i}", "s", file, i, "k1", False, "declined")

    summary = suites.summarise([case(1, "a.test"), case(2, "b.test")])
    assert summary["all"]["cases"] == 1 and summary["duplicates_collapsed"] == 1


@pytest.mark.slow
def test_duckdb_suite_sample_has_no_behaviour_changes():
    try:
        root = suites.fetch_suite("duckdb-slt")
    except Exception as exc:  # no network
        pytest.skip(f"suite not available: {exc}")
    files = suites.collect_files("duckdb-slt", root, 40, 60)
    cases = [c for f in files for c in suites.run_file(("duckdb-slt", str(root), f))]
    summary = suites.summarise(cases)
    assert summary["all"]["wrong"] == 0
    assert summary["all"]["transformed"] > 100
