"""Producers and consumers outside the repository (``--boundary``): traces continue past a declared source, impact lists outside readers."""

import json

import pytest

from kumosql import boundary as outside, mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from test_mcp_server import ask, write_project

PROJECT = {
    "region.sqlx": 'config { type: "declaration", schema: "lookups", name: "region" }\n',
    "events.sqlx": 'config { type: "declaration", schema: "raw", name: "events" }\n',
    "orders.sqlx": (
        'config { type: "table" }\n'
        'SELECT e.id AS order_id, e.amount, r.region_name, e.created_week AS order_week\n'
        'FROM ${ref("raw", "events")} AS e JOIN ${ref("lookups", "region")} AS r ON e.region_code = r.region_code\n'
    ),
    "weekly.sqlx": 'config { type: "view" }\nSELECT order_week, SUM(amount) AS total FROM ${ref("orders")} GROUP BY order_week\n',
}
SCHEMA = {
    "p.raw.events": {"id": "INT64", "amount": "INT64", "region_code": "STRING", "created_week": "STRING"},
    "p.lookups.region": {"region_code": "STRING", "region_name": "STRING"},
}
FILES = {
    "external/region.sql": (
        "SELECT g.code AS region_code, g.label AS region_name\n"
        "FROM legacy.geo.regions AS g JOIN p.lookups.region_extra AS x ON g.code = x.code\n"
    ),
    "external/dashboard_source.sql": "SELECT order_week, total FROM ds.weekly WHERE total > 0\n",
    "external/orders_export.sql": "SELECT order_id, amount FROM ds.orders\n",
    "semantic/sales.malloy": "source: sales is bigquery.table('ds.orders') extend {\n  measure: revenue is amount.sum()\n  dimension: week is order_week\n}\n",
    "semantic/other.malloy": "source: other is bigquery.table('ds.customers') extend { dimension: amount is amount }\n",
}
BOUNDARY = """
producers:
  - table: lookups.region
    by: {kind: scheduled_query, id: "1234", url: "https://example.test/q/1234"}
    sql: external/region.sql
consumers:
  - {name: sales_semantic_model, kind: semantic, path: "semantic/sales.malloy"}
  - {name: weekly_dashboard, kind: dashboard, sql: external/dashboard_source.sql}
"""


def make(tmp_path, boundary_text=BOUNDARY, files=FILES, project=PROJECT):
    root = write_project(tmp_path / "proj", project)
    folder = tmp_path / "outside"
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    spec = folder / "boundary.yaml"
    spec.write_text(boundary_text)
    pipeline = load_sqlx_project(root, source_schema=SCHEMA)
    session = Session(pipeline, boundary=spec)
    return mcp_server.AgentServer(session), session, spec


def test_impact_drop_column_lists_a_semantic_model_file_as_a_reader_outside_the_project(tmp_path):
    server, _, _ = make(tmp_path)
    body, failed, _ = ask(server, "impact", table="orders", column="amount", change="drop_column")
    assert not failed
    readers = {item["name"]: item for item in body["readers_outside_project"]}
    assert readers["sales_semantic_model"]["how"] == "reads (Malloy)"
    assert readers["sales_semantic_model"]["sources"] == ["sales"]
    assert readers["sales_semantic_model"]["evidence"] == ["semantic/sales.malloy"]
    assert "Malloy" in body["outside_note"] and body["counts"]["outside_readers"] == 1
    assert "outside the project" in body["answer"]
    assert "not visible" in body["safe_to_delete"]


def test_a_malloy_source_is_a_reader_only_when_its_table_and_a_column_match(tmp_path):
    # the file reads ds.orders but never refers to amount
    server, _, _ = make(tmp_path, files={**FILES, "semantic/sales.malloy": "source: s is bigquery.table('ds.orders') extend { dimension: week is order_week }\n"})
    body, _, _ = ask(server, "impact", table="orders", column="amount")
    assert "readers_outside_project" not in body
    body, _, _ = ask(server, "impact", table="orders", column="order_week")
    assert body["readers_outside_project"][0]["sources"] == ["s"]
    # the other file refers to amount, but over another table
    server2, _, _ = make(tmp_path / "b", boundary_text=BOUNDARY.replace("semantic/sales.malloy", "semantic/other.malloy"))
    body, _, _ = ask(server2, "impact", table="orders", column="amount")
    assert "readers_outside_project" not in body


def test_a_text_file_is_matched_by_text_and_a_column_without_the_table_is_unknown(tmp_path):
    lookml = "view: orders { sql_table_name: `p.ds.orders` ;; dimension: amount { sql: ${TABLE}.amount ;; } }\n"
    other = "view: customers { sql_table_name: ds.customers ;; dimension: amount { sql: ${TABLE}.amount ;; } }\n"
    boundary_text = BOUNDARY.replace('path: "semantic/sales.malloy"', 'path: "bi/*.lkml"')
    server, _, _ = make(tmp_path, boundary_text, {**FILES, "bi/orders.lkml": lookml})
    body, _, _ = ask(server, "impact", table="orders", column="amount")
    (reader,) = [r for r in body["readers_outside_project"] if r["name"] == "sales_semantic_model"]
    assert reader["how"] == "reads (by text)" and reader["evidence"] == ["bi/orders.lkml"]
    assert "by text" in body["outside_note"]
    body, _, _ = ask(server, "impact", table="orders", column="order_id")
    assert "sales_semantic_model" not in json.dumps(body.get("readers_outside_project", []))
    server2, _, _ = make(tmp_path / "b", boundary_text, {**FILES, "bi/customers.lkml": other})
    body, _, _ = ask(server2, "impact", table="orders", column="amount")
    assert all(r["name"] != "sales_semantic_model" for r in body.get("readers_outside_project", []))
    assert any("names the column amount but not the table" in u["reason"] for u in body["unknown"])


def test_a_consumer_sql_file_is_parsed_so_its_column_reads_are_exact(tmp_path):
    server, _, _ = make(tmp_path)
    body, _, _ = ask(server, "impact", table="weekly", column="total")
    dashboard = next(i for i in body["readers_outside_project"] if i["name"] == "weekly_dashboard")
    assert dashboard["how"] == "reads" and "total" in dashboard["columns"]
    assert dashboard["effect"] == "breaks" and dashboard["file"] == "external/dashboard_source.sql"
    body, _, _ = ask(server, "impact", table="weekly", column="order_week", change="rename_column")
    assert any(i["name"] == "weekly_dashboard" for i in body["readers_outside_project"])
    # a column the SQL does not read is not reported for it
    body, _, _ = ask(server, "impact", table="orders", column="region_name")
    assert "weekly_dashboard" not in json.dumps(body.get("readers_outside_project", []))


def test_trace_column_names_the_scheduled_query_and_continues_through_the_producer_sql(tmp_path):
    server, _, _ = make(tmp_path)
    body, failed, size = ask(server, "trace_column", table="orders", column="region_name")
    assert not failed and size <= 2000
    (item,) = body["outside_the_project"]
    assert item["source"] == "p.lookups.region.region_name"
    assert item["built_by"] == "scheduled_query 1234 (https://example.test/q/1234)"
    assert item["file"] == "external/region.sql"
    assert item["reads"] == ["legacy.geo.regions.label"]
    assert "outside" in body["outside_note"]
    # a source column asked about directly goes through the same producer
    body, _, _ = ask(server, "trace_column", table="lookups.region", column="region_code")
    assert body["outside_the_project"][0]["reads"] == ["legacy.geo.regions.code"]
    # the declared source with no producer is named as having none
    body, _, _ = ask(server, "trace_column", table="orders", column="amount")
    assert any("no producer" in u["reason"] for u in body["unknown"])


def test_a_producer_chain_is_followed_through_every_outside_table(tmp_path):
    spec = BOUNDARY.replace("consumers:", """  - table: raw.events
    by: {kind: notebook_job, id: nb-7}
    sql: external/events.sql
consumers:""")
    files = {**FILES, "external/region.sql": "SELECT s.code AS region_code, s.label AS region_name FROM p.raw.events AS s0 JOIN staging.codes AS s ON s0.region_code = s.code\n",
             "external/events.sql": "SELECT id, amount, region_code, created_week FROM legacy.tool.dump\n"}
    server, _, _ = make(tmp_path, spec, files)
    body, _, _ = ask(server, "trace_column", table="lookups.region", column="region_code")
    (item,) = body["outside_the_project"]
    assert item["reads"] == ["staging.codes.code"]
    body, _, _ = ask(server, "trace_column", table="raw.events", column="amount")
    assert body["outside_the_project"][0]["reads"] == ["legacy.tool.dump.amount"]


def test_describe_table_says_who_builds_a_source_and_who_reads_a_table_outside(tmp_path):
    server, _, _ = make(tmp_path)
    body, _, _ = ask(server, "describe_table", table="lookups.region")
    assert body["built_outside_the_project"] == "scheduled_query 1234 (https://example.test/q/1234)"
    assert body["outside_reads"] == ["legacy.geo.regions", "p.lookups.region_extra"]
    assert all("outside reader" not in u["what"] for u in body["unknown"])
    body, _, _ = ask(server, "describe_table", table="orders")
    assert body["read_by_outside_project"] == ["sales_semantic_model"]
    body, _, _ = ask(server, "describe_table", table="raw.events")
    assert any("who builds it" == u["what"] for u in body["unknown"])


def test_a_consumer_whose_files_cannot_be_read_is_unknown_not_silence(tmp_path):
    server, _, _ = make(tmp_path, boundary_text=BOUNDARY.replace("semantic/sales.malloy", "semantic/missing/*.malloy"))
    body, _, _ = ask(server, "impact", table="orders", column="amount")
    assert "readers_outside_project" not in body
    unknown = [u for u in body["unknown"] if "sales_semantic_model" in u["what"]]
    assert unknown and unknown[0]["reason"] == "no file matches the path"
    assert "No outside reader" not in " ".join(body["next"])
    # a file that is not text is unreadable too
    server, session, spec = make(tmp_path / "b", files={**FILES})
    (spec.parent / "semantic" / "sales.malloy").write_bytes(b"\x00\x01\x02ds.orders amount")
    body, _, _ = ask(server, "impact", table="orders", column="amount")
    assert any("sales_semantic_model" in u["what"] and "not text" in u["reason"] for u in body["unknown"])


def test_a_bad_boundary_file_is_a_bounded_specific_error_at_load_time(tmp_path):
    bad = tmp_path / "bad.yaml"

    def error(text, base=tmp_path):
        bad.write_text(text)
        with pytest.raises(outside.BoundaryError) as caught:
            outside.read_file(bad)
        return str(caught.value)

    (tmp_path / "broken.sql").write_text("SELECT FROM WHERE (((")
    assert "does not parse" in error("producers:\n  - {table: a.b, by: {kind: q}, sql: broken.sql}\n")
    assert "sql nope.sql is not a file" in error("consumers:\n  - {name: x, sql: nope.sql}\n")
    assert "not valid YAML or JSON" in error("producers: [unclosed\n")
    assert "must be relative" in error("consumers:\n  - {name: x, path: /etc/passwd}\n")
    assert "did you mean table" in error("producers:\n  - {tabel: a.b, by: {kind: q}}\n")
    assert "needs a path" in error("consumers:\n  - {name: x}\n")
    assert "names no producers" in error("producers: []\n")
    many = "consumers:\n" + "".join(f"  - {{name: c{i}}}\n" for i in range(9))
    message = error(many)
    assert "and 4 more" in message and message.count("needs a path") == 5
    # a JSON file works the same
    bad_json = tmp_path / "ok.json"
    bad_json.write_text(json.dumps({"consumers": [{"name": "n", "path": "x/*.txt"}]}))
    assert outside.read_file(bad_json).consumers[0].patterns == ("x/*.txt",)


def test_producer_table_names_are_checked_against_the_project_with_close_names(tmp_path):
    server, session, _ = make(tmp_path, boundary_text=BOUNDARY.replace("lookups.region", "lookups.regions"))
    body, failed, _ = ask(server, "impact", table="orders", column="amount")
    assert not failed and session.boundary_error and "lookups.regions" in session.boundary_error and "close: p.lookups.region" in session.boundary_error
    assert any("boundary file was not applied" in u["reason"] for u in body["unknown"])
    # a table the project builds itself cannot have an outside producer
    server, session, _ = make(tmp_path / "b", boundary_text=BOUNDARY.replace("lookups.region", "orders"))
    ask(server, "describe_table", table="orders")
    assert "built by the project" in session.boundary_error


def test_without_a_boundary_file_answers_are_unchanged(tmp_path):
    root = write_project(tmp_path / "proj", PROJECT)
    server = mcp_server.AgentServer(Session(load_sqlx_project(root, source_schema=SCHEMA)))
    body, _, _ = ask(server, "impact", table="orders", column="amount")
    assert "readers_outside_project" not in body and "outside_note" not in body
    body, _, _ = ask(server, "trace_column", table="orders", column="region_name")
    assert "outside_the_project" not in body and all("producer" not in u["reason"] for u in body.get("unknown", []))


def test_the_mcp_command_refuses_a_bad_boundary_file_before_serving(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("producers: nope\n")
    with pytest.raises(SystemExit):
        mcp_server.main([str(tmp_path), "--boundary", str(bad)])
    assert "--boundary: boundary file bad.yaml" in capsys.readouterr().err


def test_a_notebook_producer_without_sql_says_why_the_trace_ends_there(tmp_path):
    spec = BOUNDARY.replace("consumers:", """  - table: raw.events
    by: {kind: notebook, id: nb-7}
    sql_unavailable: the notebook code was not exported
consumers:""")
    server, _, _ = make(tmp_path, spec)
    body, failed, _ = ask(server, "trace_column", table="orders", column="amount")
    assert not failed
    (item,) = body["outside_the_project"]
    assert item["built_by"] == "notebook nb-7" and item["sql"] == "not available: the notebook code was not exported"
    unknown = [u for u in body["unknown"] if "p.raw.events.amount" in u["what"]]
    assert unknown and "built outside the project by notebook nb-7; its SQL is not available (the notebook code was not exported)" in unknown[0]["reason"]
    body, _, _ = ask(server, "describe_table", table="raw.events")
    assert body["built_outside_the_project"] == "notebook nb-7"
    assert any(u["what"] == "what the outside job reads" and "not available" in u["reason"] for u in body["unknown"])
    both = tmp_path / "both.yaml"
    both.write_text("producers:\n  - {table: a.b, by: {kind: notebook}, sql: x.sql, sql_unavailable: why}\n")
    (tmp_path / "x.sql").write_text("SELECT 1 AS a")
    with pytest.raises(outside.BoundaryError, match="both sql and sql_unavailable"):
        outside.read_file(both)


def test_a_producer_can_list_several_sql_snippets_that_each_build_some_columns(tmp_path):
    spec = BOUNDARY.replace("    sql: external/region.sql", "    sql: [external/region_code.sql, external/region_name.sql]")
    files = {**FILES, "external/region_code.sql": "SELECT c.code AS region_code FROM staging.codes AS c\n",
             "external/region_name.sql": "SELECT n.label AS region_name FROM staging.names AS n\n"}
    server, _, _ = make(tmp_path, spec, files)
    body, _, _ = ask(server, "trace_column", table="lookups.region", column="region_code")
    assert body["outside_the_project"][0]["reads"] == ["staging.codes.code"]
    assert body["outside_the_project"][0]["sql_snippets"] == 2
    body, _, _ = ask(server, "trace_column", table="lookups.region", column="region_name")
    assert body["outside_the_project"][0]["reads"] == ["staging.names.label"]
    body, _, _ = ask(server, "describe_table", table="lookups.region")
    assert body["outside_reads"] == ["staging.codes", "staging.names"]


def test_malloy_joins_and_sources_extended_in_another_file_count_as_reads(tmp_path):
    files = {
        **FILES,
        "semantic/base.malloy": "source: base is bigquery.table('lookups.region') extend { primary_key: region_code }\n",
        "semantic/sales.malloy": (
            "source: sales is bigquery.table('ds.orders') extend {\n  join_one: base with region_code\n  join_one: r is bigquery.table('lookups.region') on region_code = r.region_name\n}\n"
            "source: names is base extend { dimension: label is region_name }\n"
        ),
    }
    boundary_text = BOUNDARY.replace('path: "semantic/sales.malloy"', 'path: "semantic/*.malloy"')
    server, _, _ = make(tmp_path, boundary_text, files)
    # region_name is read by the extending source and through the inline join
    body, _, _ = ask(server, "impact", table="lookups.region", column="region_name")
    (reader,) = [r for r in body["readers_outside_project"] if r["name"] == "sales_semantic_model"]
    assert reader["how"] == "reads (Malloy)" and set(reader["sources"]) == {"sales", "names"}
    # the key of a source is a column it reads
    body, _, _ = ask(server, "impact", table="lookups.region", column="region_code")
    assert {"names", "base"} <= set(next(r for r in body["readers_outside_project"] if r["name"] == "sales_semantic_model")["sources"])
    body, _, _ = ask(server, "describe_table", table="lookups.region")
    assert body["read_by_outside_project"] == ["sales_semantic_model"]


def test_malloy_the_reader_cannot_read_is_unknown_and_the_file_is_still_matched_as_text(tmp_path):
    odd = "source: s is duckdb.sql(\"select 1\") extend { dimension: x is y }\nsource: t is bigquery.table('ds.orders') extend { dimension: total is amount.sum() }\n// ds.orders amount\nsource: u is table('ds.orders') extend { where: (\n"
    server, _, _ = make(tmp_path, files={**FILES, "semantic/sales.malloy": odd})
    body, _, _ = ask(server, "impact", table="orders", column="amount")
    (reader,) = [r for r in body["readers_outside_project"] if r["name"] == "sales_semantic_model"]
    assert reader["how"] == "reads (by text)"  # brackets do not balance: nothing was read as Malloy, the whole file is text
    assert any("Malloy file has parts the reader could not read" in u["reason"] for u in body["unknown"])


def test_many_outside_readers_are_counted_grouped_by_kind_and_paged_inside_the_bound(tmp_path):
    files = dict(FILES)
    lines = ["consumers:"]
    for i in range(120):
        files[f"bi/m{i}.malloy"] = f"source: s{i} is bigquery.table('ds.orders') extend {{ measure: total{i} is amount.sum() }}\n"
        lines.append(f"  - {{name: report_{i:03d}, kind: {'semantic' if i % 3 else 'dashboard'}, path: bi/m{i}.malloy}}")
    for i in range(30):
        files[f"bi/d{i}.sql"] = "SELECT order_id, amount FROM ds.orders\n"
        lines.append(f"  - {{name: sheet_{i:03d}, kind: sheet, sql: bi/d{i}.sql}}")
    server, _, _ = make(tmp_path, "\n".join(lines) + "\n", files)
    first, failed, size = ask(server, "impact", table="orders", column="amount")
    assert not failed and size <= 2000
    assert first["counts"]["outside_readers"] == 150
    assert first["counts"]["outside_by_kind"] == {"semantic": 80, "dashboard": 40, "sheet": 30}
    assert first["readers_outside_project_shown"].endswith("of 150") and "offset=" in first["more"]
    shown = len(first["readers_outside_project"])
    assert 3 <= shown < 150
    second, _, size = ask(server, "impact", table="orders", column="amount", offset=shown)
    assert size <= 2000 and second["readers_outside_project"][0]["name"] != first["readers_outside_project"][0]["name"]
    assert "affected" not in first or first["affected"]  # the inside list is still there, unpaged by this offset
