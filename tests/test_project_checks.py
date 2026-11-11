"""Repository problems found without a warehouse: declared and built, disabled but read, declared twice, commented templates."""

import json

import pytest

from kumosql import mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project

from test_mcp_server import ask, block_network, write_project

BOUND = 2000

FILES = {
    "orders_decl.sqlx": 'config { type: "declaration", schema: "raw", name: "orders" }',
    "orders_model.sqlx": 'config { type: "table", schema: "raw", name: "orders" }\nSELECT 1 AS id, 5 AS amount\n',
    "calendar.sqlx": 'config { type: "table", disabled: true }\nSELECT 1 AS day\n',
    "order_week.sqlx": 'config { type: "table" }\nSELECT day, 2 AS n FROM ${ref("calendar")}\n',
    "weekly_view.sqlx": 'config { type: "view" }\nSELECT day FROM ${ref("order_week")}\n',
    "customers_a.js": 'declare({ schema: "raw", name: "customers" });\ndeclare({ schema: "staging", name: "customers" });\n',
    "customers_again.js": 'declare({ schema: "raw", name: "customers" });\n',
    "plain.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
}


@pytest.fixture
def server(tmp_path, monkeypatch):
    block_network(monkeypatch)
    return mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path, FILES))))


def checks(body):
    return {row["check"]: row for row in body["findings"]}


def test_a_name_both_declared_and_built_is_reported_by_project_checks_and_describe_table(server):
    body, failed, used = ask(server, "project_checks")
    assert not failed and used <= BOUND
    found = checks(body)["declared_and_built"]
    assert found["count"] == 1 and "raw.orders" in found["examples"][0] and found["fix"] and body["next"]
    card, _, _ = ask(server, "describe_table", table="raw.orders")
    assert "declared as a source and also built" in card["project_check"]


def test_a_disabled_model_read_by_an_enabled_one_is_reported_and_its_reader_is_told(server):
    body, _, _ = ask(server, "project_checks")
    found = checks(body)["disabled_read_by_enabled"]
    assert found["count"] == 1 and "calendar" in found["examples"][0] and "order_week" in found["examples"][0]
    assert "disabled" in ask(server, "describe_table", table="calendar")[0]["project_check"]
    assert "reads the disabled model" in ask(server, "describe_table", table="order_week")[0]["project_check"]
    assert "project_check" not in ask(server, "describe_table", table="plain")[0]


def test_a_name_declared_in_two_datasets_is_reported_but_an_identical_repeat_is_one_table(server):
    body, _, _ = ask(server, "project_checks")
    found = checks(body)["declared_in_several_datasets"]
    assert found["count"] == 1 and "customers is declared in 2 places" in found["examples"][0]
    assert body["counts"]["findings"] == 3


def test_project_checks_filters_by_check_says_what_it_cannot_see_and_refuses_an_unknown_check(server):
    body, _, _ = ask(server, "project_checks", check="disabled_read_by_enabled")
    assert list(checks(body)) == ["disabled_read_by_enabled"]
    assert any("deployed tables" in item["what"] for item in body["unknown"])
    _, failed, _ = ask(server, "project_checks", check="nope")
    assert failed


def test_a_clean_project_reports_no_problems_and_says_it_did_not_look_at_the_warehouse(tmp_path):
    clean = mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path, {"a.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n'}))))
    body, _, _ = ask(clean, "project_checks")
    assert body["counts"]["findings"] == 0 and "says nothing about the warehouse" in body["answer"] and "findings" not in body


def test_check_query_on_a_disabled_or_declared_and_built_table_is_not_a_silent_pass(server):
    body, failed, used = ask(server, "check_query", sql="SELECT day FROM p.ds.calendar")
    assert not failed and used <= BOUND and body["verdict"] == "no_problems_found"
    assert any("disabled model" in item["what"] and "calendar" in item["what"] for item in body["unknown"])
    body, _, _ = ask(server, "check_query", sql="SELECT day FROM ${ref(\"order_week\")}")
    assert any("read from a disabled model" in item["what"] for item in body["unknown"])
    body, _, _ = ask(server, "check_query", sql="SELECT id FROM p.raw.orders")
    assert any("both declared as a source and built" in item["what"] for item in body["unknown"])
    body, _, _ = ask(server, "check_query", sql="SELECT id FROM p.ds.plain")
    assert not any("disabled" in item["what"] or "declared" in item["what"] for item in body.get("unknown", []))


def test_check_query_on_a_declared_source_the_project_does_not_build_says_so(server):
    body, _, _ = ask(server, "check_query", sql="SELECT * FROM p.staging.customers")
    [note] = [item for item in body["unknown"] if "declared source" in item["what"]]
    assert "staging.customers" in note["what"] and "not checked" in note["reason"]


# ------------------------------------------------------------ templates in comments


COMMENTED = (
    'config { type: "table" }\n'
    'SELECT 1 AS id -- ${ref("plain")}\n'
    '/* ${ref("plain")} and ${unknown_call()} */\n'
    ', "--" AS dashes\n'
    'FROM `p.ds.plain` WHERE TRUE AND ${when(incremental(), "id > 1")}\n'
)


def unresolved(pipeline):
    model = pipeline.models["p.ds.commented"]
    return len(model.masked_expressions), model.sql.count("__sqlx_token_")


def test_templates_inside_comments_are_not_counted_as_unresolved(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path, {"commented.sqlx": COMMENTED, "plain.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n'}))
    model = pipeline.models["p.ds.commented"]
    assert unresolved(pipeline) == (1, 1)  # was (4, 4) before the fix: only the live ${when(...)} outside a comment
    assert model.masked_expressions == ('${when(incremental(), "id > 1")}',)
    assert "${" not in model.sql.replace("__sqlx_token", "")  # a commented template is never live SQL
    assert '-- ${ref("plain")}' not in model.sql and "$ {ref" in model.sql
    assert '"--" AS dashes' in model.sql  # a -- inside a string is not a comment


def test_a_template_after_a_hash_is_still_evaluated_as_dataform_does(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path, {
        "commented.sqlx": 'config { type: "table" }\nSELECT 1 AS id FROM `p.ds.plain` # ${ref("plain")}\n',
        "plain.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
    }))
    assert "p.ds.plain" in pipeline.upstream["p.ds.commented"]


def test_a_commented_ref_adds_no_dependency_and_no_unresolved_ref_diagnostic(tmp_path):
    pipeline = load_sqlx_project(write_project(tmp_path, {
        "a.sqlx": 'config { type: "table" }\nSELECT 1 AS id -- ${ref("nowhere")}\n',
    }))
    assert pipeline.upstream["p.ds.a"] == set()
    assert not [d for d in pipeline.diagnostics if d.code in ("missing_ref", "unsupported_ref")]
