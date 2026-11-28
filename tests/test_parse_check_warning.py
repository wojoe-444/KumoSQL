"""A warning when sqlglot reads SQL differently from BigQuery (``kumosql.parse_warning``), in ``check_query`` and ``describe_table``.

The misread texts are the prover's own known cases (``tests/test_parse_check.py`` of Kumo_SQL_Prover): GoogleSQL puts ``|``
and ``&`` on separate levels and does not chain comparisons, and sqlglot reads both differently. Each test builds its own
project under ``tmp_path``, so the tests are parallel-safe; nothing reaches the network.
"""

import pytest

from kumosql import mcp_server, parse_warning
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from test_mcp_server import ask, findings_of, no_network, write_project  # noqa: F401

BOUND = 2000
SOURCE = {"p.src.raw": {"id": "INT64", "a": "INT64", "b": "INT64", "c": "INT64"}}
PROJECT = {
    "raw.sqlx": 'config { type: "declaration", schema: "src", name: "raw" }\n',
    "clean.sqlx": 'config { type: "table" }\nSELECT id, a FROM ${ref("src", "raw")} WHERE a > 10\n',
    "bits.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("src", "raw")} WHERE a | b & c > 0\n',
    "chained.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("src", "raw")} WHERE a > 10 IS TRUE\n',
}


def build(tmp_path):
    folder = write_project(tmp_path / "project", PROJECT)
    return mcp_server.AgentServer(Session(load_sqlx_project(folder, source_schema=SOURCE)))


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    return build(tmp_path_factory.mktemp("parse_warning"))


# ----------------------------------------------------------------- check_query


@pytest.mark.parametrize("sql, construct", [
    ("SELECT id FROM ${ref('clean')} WHERE a | 1 & 0 > 0", "GoogleSQL reads (a) | (1 & 0)"),
    ("SELECT id FROM ${ref('clean')} WHERE a > 10 IS TRUE", "comparisons are not associative"),
])
def test_a_draft_sqlglot_misreads_is_a_parse_finding_that_names_the_construct_and_why(served, no_network, sql, construct):
    body, failed, used = ask(served, "check_query", sql=sql)
    assert not failed and used <= BOUND and body["verdict"] == "problems_found"
    [found] = findings_of(body, "parse")
    assert body["findings"][0] is not None and body["findings"][0]["check"] == "parse"  # first: it puts the others in doubt
    assert construct in found["what"] and "The other checks read sqlglot's version" in found["what"]
    assert "parentheses" in found["fix"] and "parse" in body["checked"]
    assert "Apply the fix of the first finding (parse)" in " ".join(body["next"])


def test_the_same_draft_with_parentheses_passes_the_parse_check(served, no_network):
    body, _, _ = ask(served, "check_query", sql="SELECT id FROM ${ref('clean')} WHERE (a > 10) IS TRUE AND (a | (1 & 0)) > 0")
    assert not findings_of(body, "parse") and "parse" in body["checked"]
    assert not any(u["what"] == parse_warning.GAP for u in body.get("unknown", []))


def test_a_construct_the_independent_reader_does_not_know_is_listed_as_not_checked_never_passed(served, no_network):
    body, _, _ = ask(served, "check_query", sql="SELECT id FROM ${ref('clean')} TABLESAMPLE SYSTEM (10 PERCENT)")
    assert "parse" not in body["checked"] and not findings_of(body, "parse")
    [gap] = [u for u in body["unknown"] if u["what"] == parse_warning.GAP]
    assert "TABLESAMPLE" in gap["reason"] and "does not know" in gap["reason"]


def test_a_parse_check_that_fails_is_unchecked_never_a_warning(served, no_network, monkeypatch):
    from kumo_sql_prover import parse_check

    def broken(sql, dialect="bigquery"):
        raise RuntimeError("boom")

    monkeypatch.setattr(parse_check, "check_query", broken)
    body, failed, _ = ask(served, "check_query", sql="SELECT id FROM ${ref('clean')}")
    assert not failed and not findings_of(body, "parse") and "parse" not in body["checked"]
    assert any(u["what"] == parse_warning.GAP and "stopped" in u["reason"] for u in body["unknown"])


def test_a_round_trip_that_regroups_operators_is_a_warning_too(monkeypatch):
    from kumo_sql_prover import parse_check

    monkeypatch.setattr(parse_check, "round_trip", lambda sql, dialect="bigquery": "sqlglot reads its own bigquery SQL back with different operator grouping")
    found = parse_warning.read("SELECT 1 AS x")
    assert found.misread and "different operator grouping" in found.reason
    findings, gaps, ran = parse_warning.check_draft("SELECT 1 AS x")
    assert ran and not gaps and findings[0]["check"] == "parse"


# --------------------------------------------------------------- describe_table


@pytest.mark.parametrize("table, construct", [("bits", "GoogleSQL reads (a) | (b & c)"), ("chained", "comparisons are not associative")])
def test_describe_table_warns_when_sqlglot_misreads_the_models_sql(served, table, construct):
    body, failed, used = ask(served, "describe_table", table=table)
    assert not failed and used <= BOUND
    assert construct in body["parse_warning"] and "may be wrong" in body["parse_warning"] and "parentheses" in body["parse_warning"]
    assert "sqlglot misreads its SQL" in body["answer"]
    body, failed, used = ask(served, "describe_table", table=table, max_bytes=1000)
    assert not failed and used <= 1000 and "parse_warning" in body  # a field, never cut with the lists


def test_a_model_sqlglot_reads_right_has_no_warning(served):
    body, _, _ = ask(served, "describe_table", table="clean")
    assert "parse_warning" not in body and "misreads" not in body["answer"]


def test_a_models_parse_check_runs_once_per_session(tmp_path, monkeypatch):
    from kumo_sql_prover import parse_check

    served = build(tmp_path)
    calls = []
    real = parse_check.check_query

    def counting(sql, dialect="bigquery"):
        calls.append(sql)
        return real(sql, dialect)

    monkeypatch.setattr(parse_check, "check_query", counting)
    ask(served, "describe_table", table="bits")
    ask(served, "describe_table", table="bits")
    assert len(calls) == 1
    ask(served, "describe_table", table="clean")
    assert len(calls) == 2
