"""The agent-facing server (``python -m kumosql mcp``): protocol, the four tools, bounded answers, read-only."""

import io
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from kumosql import data_profile_store as store, mcp_answers, mcp_server, profile_mcp
from kumosql.data_profile import DataProfile
from kumosql.mcp_answers import Answer, render, size
from kumosql.mcp_session import Session, load_project
from kumosql.pipeline_loading import load_sqlx_project

ROOT = Path(__file__).resolve().parent.parent
BIG = ROOT / "tests" / "fixtures" / "bq_corpora" / "security-analytics"
BOUND = mcp_answers.DEFAULT_BYTES

SMALL = {
    "raw.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS amount, "eu" AS region, 7 AS spare\n',
    "totals.sqlx": 'config { type: "table" }\nSELECT id, SUM(amount) AS total FROM ${ref("raw")} WHERE amount > 0 GROUP BY id\n',
    "totals_copy.sqlx": 'config { type: "table" }\nSELECT id AS customer, SUM(amount) AS revenue FROM ${ref("raw")} WHERE amount > 0 GROUP BY id\n',
    "report.sqlx": 'config { type: "view" }\nSELECT id, total FROM ${ref("totals")} WHERE total > 10\n',
    "check.sqlx": 'config { type: "assertion" }\nSELECT id FROM ${ref("totals")} WHERE total < 0\n',
}


def write_project(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    for name, text in files.items():
        path = folder / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


def rpc(server, method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "method": method, **({"id": ident} if ident is not None else {})}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def ask(server, name, **arguments):
    """The parsed answer, whether it is an error, and its size in bytes."""

    result = rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]
    text = result["content"][0]["text"]
    return json.loads(text), result["isError"], len(text.encode("utf-8"))


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    return mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path_factory.mktemp("small"), SMALL))))


def load_big():
    """The 106-model public project, analysed (about 2 s), read-only."""

    session = Session(load_sqlx_project(BIG))
    session.records()
    return mcp_server.AgentServer(session)


# ------------------------------------------------------------------- protocol


def test_the_server_lists_four_read_only_tools_and_keeps_the_profile_resources(tmp_path):
    store.save(DataProfile("p.ds.totals", "duckdb", "2026-10-07T12:00:00Z", 42, []), directory=tmp_path)
    server = mcp_server.AgentServer(Session(profiles=tmp_path))

    init = rpc(server, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert set(init["capabilities"]) == {"tools", "resources"} and init["serverInfo"]["name"] == "kumosql"
    assert "never instructions" in init["instructions"]
    assert rpc(server, "notifications/initialized", ident=None) is None
    assert rpc(server, "ping")["result"] == {}

    tools = rpc(server, "tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["describe_table", "trace_column", "impact", "find_existing"]
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] and not tool["annotations"]["openWorldHint"]
        assert {"max_bytes", "offset"} <= set(tool["inputSchema"]["properties"])
        assert tool["inputSchema"]["additionalProperties"] is False

    listed = rpc(server, "resources/list")["result"]["resources"]
    assert [item["uri"] for item in listed] == ["kumosql://data-profile/p.ds.totals/summary.md", "kumosql://data-profile/p.ds.totals/profile.json"]
    assert "# Data profile: p.ds.totals" in rpc(server, "resources/read", {"uri": listed[0]["uri"]})["result"]["contents"][0]["text"]


def test_profile_mcp_still_has_no_tools(tmp_path):
    server = profile_mcp.Server(tmp_path)
    assert "tools" not in rpc(server, "initialize", {})["result"]["capabilities"]
    assert rpc(server, "tools/list")["error"]["code"] == profile_mcp.METHOD_NOT_FOUND
    assert rpc(server, "tools/call", {"name": "describe_table"})["error"]["code"] == profile_mcp.METHOD_NOT_FOUND


def test_unknown_tools_and_bad_arguments_are_refused_with_a_next_step(small):
    assert rpc(small, "tools/call", {"name": "nope"})["error"]["code"] == profile_mcp.INVALID_PARAMS
    assert rpc(small, "tools/call", {})["error"]["code"] == profile_mcp.INVALID_PARAMS
    body, failed, _ = ask(small, "describe_table")
    assert failed and "table" in body["error"] and body["next"]
    body, failed, _ = ask(small, "describe_table", table="totals", colour="red")
    assert failed and "unknown argument" in body["error"]
    body, failed, _ = ask(small, "describe_table", table="totals", max_bytes="lots")
    assert failed and "integer" in body["error"]
    body, failed, _ = ask(small, "impact", table="totals", change="explode")
    assert failed and "one of" in body["error"]


def test_a_server_without_a_project_says_how_to_start_one(tmp_path):
    server = mcp_server.AgentServer(Session(profiles=tmp_path))
    body, failed, _ = ask(server, "describe_table", table="x")
    assert failed and "no project" in body["error"] and "python -m kumosql mcp" in body["next"][0]


def test_a_project_that_cannot_be_loaded_is_reported_not_raised(tmp_path):
    server = mcp_server.AgentServer(Session(loader=lambda: load_project(tmp_path / "missing")))
    body, failed, _ = ask(server, "describe_table", table="x")
    assert failed and "not found" in body["error"]
    assert ask(server, "describe_table", table="x")[1]  # asking again does not load again


def test_the_stdio_loop_answers_one_line_per_request(small):
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "describe_table", "arguments": {"table": "totals"}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
    ])
    out = io.StringIO()
    profile_mcp.serve(small, io.StringIO(lines), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 2 and json.loads(replies[0]["result"]["content"][0]["text"])["table"] == "p.ds.totals"
    assert replies[1]["error"]["code"] == profile_mcp.PARSE_ERROR


def test_python_dash_m_kumosql_mcp_speaks_json_rpc_on_stdout_only(tmp_path):
    project = write_project(tmp_path / "proj", SMALL)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "impact", "arguments": {"table": "raw", "column": "amount"}}},
    ]
    done = subprocess.run(
        [sys.executable, "-m", "kumosql", "mcp", str(project)], input="\n".join(json.dumps(r) for r in requests) + "\n",
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0
    replies = [json.loads(line) for line in done.stdout.splitlines()]  # every stdout line is a JSON-RPC message
    assert [r["id"] for r in replies] == [1, 2]
    answer = json.loads(replies[1]["result"]["content"][0]["text"])
    assert answer["counts"]["breaks"] == 2


def test_profile_mcp_command_still_runs(tmp_path):
    done = subprocess.run([sys.executable, "-m", "kumosql", "profile-mcp", "--profiles", str(tmp_path)],
                          input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}) + "\n",
                          capture_output=True, text=True, timeout=60)
    assert json.loads(done.stdout)["result"] == {"resources": []}


# ------------------------------------------------------------ describe_table


def test_describe_table_joins_grain_columns_filters_and_neighbours(small):
    body, failed, used = ask(small, "describe_table", table="TOTALS")  # any case, bare name
    assert not failed and used <= BOUND
    assert body["table"] == "p.ds.totals" and body["grain"] == ["id"] and body["grain_status"] == "derived"
    assert body["counts"]["columns"] == 2 and body["counts"]["read_by"] == 2
    assert body["reads"] == ["p.ds.raw"] and body["read_by"] == ["p.ds.check", "p.ds.report"]
    assert body["own_filters"] == ["where: raw.amount > 0"]
    assert body["file"] == "definitions/totals.sqlx"
    assert any(item["what"] == "value statistics" for item in body["unknown"])  # no saved profile: said, not hidden
    assert body["next"] and "more" not in body


def test_describe_table_lists_columns_nothing_reads_and_a_single_row_grain(small):
    body, _, _ = ask(small, "describe_table", table="raw")
    assert body["grain"] == "single row" and body["unused_columns"] == ["region", "spare"]


def test_describe_table_names_close_tables_for_a_wrong_or_ambiguous_name(small):
    body, failed, _ = ask(small, "describe_table", table="total")
    assert failed and body["candidates"] == ["p.ds.totals", "p.ds.totals_copy"] and "p.ds.totals" in body["next"][0]
    body, failed, _ = ask(small, "describe_table", table="zzz")
    assert failed and "no table named" in body["error"]


def test_describe_table_points_at_a_saved_data_profile(tmp_path):
    store.save(DataProfile("p.ds.totals", "duckdb", "2026-10-07T12:00:00Z", 42, []), directory=tmp_path / "profiles")
    pipeline = load_sqlx_project(write_project(tmp_path / "proj", SMALL))
    server = mcp_server.AgentServer(Session(pipeline, profiles=tmp_path / "profiles"))
    body, _, _ = ask(server, "describe_table", table="totals")
    assert body["data_profile"] == {"rows": 42, "columns": 0, "generated": "2026-10-07T12:00:00Z", "read": "kumosql://data-profile/p.ds.totals/summary.md"}
    assert not any(item["what"] == "value statistics" for item in body.get("unknown", []))
    assert ask(server, "describe_table", table="raw")[0]["data_profile"] == "none saved"


def check_describe_table_states_unknown_column_meanings_with_their_reason(big):
    body, _, used = ask(big, "describe_table", table="csa_3_11_intermediary_stats")
    assert used <= BOUND and body["grain"] == ["day"]
    unknown = {item["what"]: item["reason"] for item in body["unknown"]}
    assert any(what.startswith("2 column meanings") and reason.startswith("nested_field") for what, reason in unknown.items())
    assert body["counts"]["meaning_unknown"] == 2
    assert any("actions = ? (nested_field)" in column for column in body["columns"])


# -------------------------------------------------------------- trace_column


def test_trace_column_says_where_rows_are_limited_once_per_model(small):
    body, failed, used = ask(small, "trace_column", table="report", column="TOTAL")
    assert not failed and used <= BOUND and body["column"] == "p.ds.report.total" and body["complete"] is True
    assert body["via_models"] == ["p.ds.totals", "p.ds.raw"] and body["from"] == ["p.ds.totals.total"]
    assert body["filters_by_model"] == [
        {"model": "p.ds.report", "filters": ["where: totals.total > 10"]},
        {"model": "p.ds.totals", "filters": ["where: raw.amount > 0"]},
    ]
    assert "literals only" in body["answer"]


def check_trace_column_reports_the_sources_of_a_real_source_and_what_it_could_not_follow(big):
    body, _, used = ask(big, "trace_column", table="csa_3_11_intermediary_stats", column="day")
    assert used <= BOUND and body["sources"] == ["[LOGS_PROJECT_ID].[LOGS_DATASET_ID]._AllLogs.timestamp"]
    assert body["counts"]["models_passed"] == 1 and len(body["filters_by_model"]) == 2
    body, _, _ = ask(big, "trace_column", table="csa_3_11_intermediary_stats", column="actions")
    assert body["complete"] is True and "actions" in body["column"]  # a nested field is traced to its source column


def test_trace_column_with_an_unknown_column_lists_the_real_ones(small):
    body, failed, _ = ask(small, "trace_column", table="totals", column="tota")
    assert failed and "no column" in body["error"] and "total" in body["next"][0]


def test_trace_column_marks_an_incomplete_trace(tmp_path):
    files = {"a.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("ghost")}\n', "b.sqlx": 'config { type: "table" }\nSELECT x FROM ${ref("a")}\n'}
    pipeline = load_sqlx_project(write_project(tmp_path, files))
    server = mcp_server.AgentServer(Session(pipeline))
    body, failed, _ = ask(server, "trace_column", table="b", column="x")
    assert not failed and body["complete"] is False
    assert body["unknown"] and all(item["reason"] for item in body["unknown"])
    assert "lower bound" in body["answer"]


# -------------------------------------------------------------------- impact


def test_impact_lists_nearest_first_and_never_claims_a_deletion_is_safe(small):
    body, failed, used = ask(small, "impact", table="raw", column="amount")
    assert not failed and used <= BOUND
    assert body["change"] == "drop_column" and body["safe_to_delete"].startswith("unknown")
    assert [(a["model"], a["effect"], a["depth"]) for a in body["affected"]] == [
        ("p.ds.totals", "breaks", 1), ("p.ds.totals_copy", "breaks", 1), ("p.ds.check", "indirect", 2), ("p.ds.report", "indirect", 2)]
    assert body["counts"] == {"affected": 4, "breaks": 2, "indirect": 2, "unknown_readers": 0, "max_depth": 2}
    assert body["next"][0].startswith("Open definitions/totals.sqlx")


def test_impact_without_a_column_asks_about_the_table_and_a_leaf_has_no_readers(small):
    body, _, _ = ask(small, "impact", table="totals")
    assert body["change"] == "drop_table" and [a["model"] for a in body["affected"]] == ["p.ds.check", "p.ds.report"]
    body, _, _ = ask(small, "impact", table="report")
    assert body["affected"] if "affected" in body else True
    assert body["counts"]["affected"] == 0 and "final output" in body["answer"] and "query history" in body["next"][-1]


def test_impact_refuses_contradictory_requests_and_wrong_names(small):
    body, failed, _ = ask(small, "impact", table="raw", change="rename_column")
    assert failed and "needs a column" in body["error"]
    body, failed, _ = ask(small, "impact", table="raw", column="amount", change="drop_table")
    assert failed and "no column" in body["error"]
    body, failed, _ = ask(small, "impact", table="raw", column="nope")
    assert failed and "no column" in body["error"]


def test_impact_lists_readers_it_could_not_analyse_with_their_reason(tmp_path):
    files = {"a.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS v\n', "bad.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")} WHERE (\n'}
    server = mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path, files))))
    body, failed, _ = ask(server, "impact", table="a", column="v")
    assert not failed and body["counts"]["unknown_readers"] == 1
    assert body["unknown"][0]["what"].startswith("1 reader: p.ds.bad") and body["unknown"][0]["reason"]


# ------------------------------------------------------------- find_existing


def test_find_existing_finds_a_table_with_the_same_meaning(small):
    body, failed, used = ask(small, "find_existing", table="totals")
    assert not failed and used <= BOUND and body["verdict"] == "found"
    assert body["matches"][0] == {"table": "p.ds.totals_copy", "kind": "same_meaning", "confidence": "high", "columns": "2 of 2",
                                  "why": "same attributes, grain and row scope"}
    assert body["counts"]["compared"] == 4 and body["counts"]["skipped"] == 0


def test_find_existing_checks_a_proposed_query_and_says_when_it_cannot_read_it(small):
    body, _, _ = ask(small, "find_existing", sql="SELECT id AS k, SUM(amount) AS s FROM p.ds.raw WHERE amount > 0 GROUP BY id")
    assert body["verdict"] == "found" and body["matches"][0]["table"] in ("p.ds.totals", "p.ds.totals_copy")
    body, _, _ = ask(small, "find_existing", sql="SELECT 1 FROM not_in_the_project")
    assert body["verdict"] == "cannot_tell" and body["unknown"]


def test_find_existing_needs_exactly_one_of_table_and_sql(small):
    assert ask(small, "find_existing")[1] and ask(small, "find_existing", table="raw", sql="SELECT 1")[1]


def check_find_existing_is_short_and_explicit_when_nothing_can_be_compared(big):
    """The overlap search used to return about 50 KB to say it compared 0 of 105 tables."""

    body, failed, used = ask(big, "find_existing", table="csa_3_11_intermediary_stats")
    assert not failed and used < 1000
    assert body["verdict"] == "cannot_tell" and body["counts"]["compared"] == 0 and body["counts"]["tables_considered"] == 105
    assert "compared 0 of 105" in body["answer"] and body["unknown"] and body["next"]
    assert all(item["reason"] for item in body["unknown"])


# ----------------------------------------------------------- bounded answers


def check_every_default_answer_on_the_big_project_fits_the_bound(big):
    pipeline = big.session.pipeline()
    sizes: dict[str, list[int]] = {"describe_table": [], "trace_column": [], "impact": [], "find_existing": []}
    for number, key in enumerate(pipeline.models):
        body, failed, used = ask(big, "describe_table", table=key)
        assert not failed
        sizes["describe_table"].append(used)
        columns = pipeline.output_columns(key)
        for column in columns:
            body, failed, used = ask(big, "trace_column", table=key, column=column)
            assert not failed, (key, column, body)
            sizes["trace_column"].append(used)
        for arguments in [{}, *({"column": column} for column in columns[:2] if number % 6 == 0)]:
            body, failed, used = ask(big, "impact", table=key, **arguments)
            assert not failed, (key, arguments, body)
            sizes["impact"].append(used)
    for key in list(pipeline.models)[:6]:
        sizes["find_existing"].append(ask(big, "find_existing", table=key)[2])
    assert len(sizes["trace_column"]) >= 600
    for name, values in sizes.items():
        assert max(values) <= BOUND, (name, max(values))


def check_a_wider_bound_returns_more_and_the_answer_says_how_to_ask(big):
    key = "csa_5_11_summary_daily"
    narrow, _, small_size = ask(big, "describe_table", table=key, max_bytes=mcp_answers.MIN_BYTES)
    assert small_size <= mcp_answers.MIN_BYTES and "more" in narrow and "columns_shown" in narrow
    assert narrow["unknown"] and narrow["next"]  # what it could not see and the next step are the last things cut
    wide, _, wide_size = ask(big, "describe_table", table=key, max_bytes=mcp_answers.MAX_BYTES)
    assert len(wide["columns"]) == wide["counts"]["columns"] and "more" not in wide and wide_size > small_size


def check_offset_pages_the_main_list_without_repeating_it(big):
    key = "csa_5_11_summary_daily"
    first, _, _ = ask(big, "describe_table", table=key)
    shown = len(first["columns"])
    assert shown < first["counts"]["columns"] and f"offset={shown}" in first["more"]
    second, _, _ = ask(big, "describe_table", table=key, offset=shown)
    assert second["columns_shown"].startswith(f"{shown + 1}-")
    wide, _, _ = ask(big, "describe_table", table=key, max_bytes=mcp_answers.MAX_BYTES)
    assert first["columns"] + second["columns"] == wide["columns"][: shown + len(second["columns"])]


def test_the_big_project_answers_are_bounded_and_explicit():
    """One test, so the project is analysed once per run, not once per xdist worker (the gate keeps tests under 2 s)."""

    big = load_big()
    check_describe_table_states_unknown_column_meanings_with_their_reason(big)
    check_trace_column_reports_the_sources_of_a_real_source_and_what_it_could_not_follow(big)
    check_find_existing_is_short_and_explicit_when_nothing_can_be_compared(big)
    check_every_default_answer_on_the_big_project_fits_the_bound(big)
    check_a_wider_bound_returns_more_and_the_answer_says_how_to_ask(big)
    check_offset_pages_the_main_list_without_repeating_it(big)


def test_the_tools_neither_write_files_nor_open_connections(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", SMALL)
    before = sorted(str(p) for p in (tmp_path / "proj").rglob("*"))

    def refuse(*_args, **_kwargs):
        raise AssertionError("the server opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    server = mcp_server.AgentServer(Session(load_sqlx_project(project)))
    for name, arguments in [("describe_table", {"table": "totals"}), ("trace_column", {"table": "report", "column": "total"}),
                            ("impact", {"table": "raw", "column": "amount"}), ("find_existing", {"table": "totals"})]:
        assert not ask(server, name, **arguments)[1]
    assert sorted(str(p) for p in (tmp_path / "proj").rglob("*")) == before  # the project is untouched
    # the only files written are KumoSQL's own diagnostics log and its name map, which every command writes in the data folder
    elsewhere = {p.name for p in tmp_path.rglob("*") if p.is_file() and "proj" not in p.relative_to(tmp_path).parts}
    assert elsewhere <= {"ui.log", "redaction-map.json"}


# ------------------------------------------------------------- the answer type


def test_render_cuts_the_longest_list_first_and_keeps_unknowns_and_the_next_step():
    answer = Answer("summary")
    answer.counts = {"items": 100}
    answer.add_list("items", [f"item-{n:03d}" + "x" * 30 for n in range(100)], primary=True)
    answer.add_list("tags", ["a", "b"])
    answer.unknown("the grain", "source_without_grain")
    answer.next("do this")
    out = render(answer, max_bytes=900)
    assert size(out) <= 900
    assert out["unknown"] == [{"what": "the grain", "reason": "source_without_grain"}] and out["next"] == ["do this"]
    assert out["tags"] == ["a", "b"] and 0 < len(out["items"]) < 100
    assert out["items_shown"] == f"1-{len(out['items'])} of 100"
    assert f"offset={len(out['items'])}" in out["more"] and "max_bytes=" in out["more"]


def test_render_is_untouched_when_everything_fits():
    answer = Answer("small")
    answer.add_list("items", ["a", "b"], primary=True)
    out = render(answer)
    assert out == {"answer": "small", "items": ["a", "b"]}


def test_render_never_exceeds_the_bound_even_for_one_huge_string():
    answer = Answer("s" * 5000)
    answer.unknown("w" * 3000, "r" * 3000)
    answer.next("n" * 3000)
    assert size(render(answer, max_bytes=mcp_answers.MIN_BYTES)) <= mcp_answers.MIN_BYTES


def test_registering_a_tool_twice_is_refused_and_a_new_tool_is_one_function(small):
    @mcp_answers.tool("echo_for_test", "Echo", "Echo the table name.", {"table": {"type": "string"}}, required=("table",))
    def echo(session, args):
        return Answer(f"echo {args['table']}")

    try:
        with pytest.raises(ValueError):
            mcp_answers.tool("echo_for_test", "Echo", "again")(echo)
        assert "echo_for_test" in {tool["name"] for tool in rpc(small, "tools/list")["result"]["tools"]}
        assert ask(small, "echo_for_test", table="x")[0] == {"answer": "echo x"}
    finally:
        mcp_answers.REGISTRY.pop("echo_for_test", None)
