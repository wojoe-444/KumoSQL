"""Column meaning that carries through lineage: descriptions, time zone, unit, scale; the checks built on them."""

import json

import pytest

from kumosql import column_docs, column_docs_files, mcp_server
from kumosql.mcp_session import Session, load_project, with_column_meaning
from kumosql.pipeline_loading import load_sqlx_project

from test_mcp_server import ask, block_network, write_project

BOUND = 2000

FILES = {
    "orders_decl.sqlx": (
        'config { type: "declaration", schema: "raw", name: "orders", columns: {'
        ' created_at: "Time the order was placed",'
        ' total: {description: "Order total", unit: "USD"},'
        ' margin_pct: {scale: "0-100", description: "Margin as a percentage"} } }'
    ),
    "stores_decl.sqlx": 'config { type: "declaration", schema: "raw", name: "stores" }',
    "stg_orders.sqlx": 'config { type: "table" }\nSELECT id, created_at AS placed_at, CAST(total AS NUMERIC) AS amount, margin_pct FROM ${ref("raw", "orders")}\n',
    "orders_clean.sqlx": (
        'config { type: "table" }\nWITH x AS (SELECT * FROM ${ref("stg_orders")})\n'
        "SELECT id, placed_at AS order_time, amount, margin_pct, amount * 2 AS doubled, margin_pct + 0 AS shifted FROM x\n"
    ),
    "local_orders.sqlx": 'config { type: "table" }\nSELECT id, DATETIME(created_at, "America/New_York") AS local_time FROM ${ref("raw", "orders")}\n',
}
SCHEMA = {
    "p.raw.orders": {"id": "INT64", "created_at": "TIMESTAMP", "updated_at": "TIMESTAMP", "total": "FLOAT64", "margin_pct": "FLOAT64"},
    "p.raw.stores": {"id": "INT64", "local_open": "DATETIME", "synced_at": "TIMESTAMP"},
}
SIDECAR = {"orders.created_at": {"timezone": "UTC"}, "stores.local_open": {"timezone": "America/Chicago"}}


def build(tmp_path, files=FILES, sidecar=SIDECAR, docs=()):
    pipeline = load_sqlx_project(write_project(tmp_path / "project", files), source_schema=SCHEMA)
    if sidecar:
        column_docs.apply_attributes(pipeline, sidecar)
    if docs:
        column_docs_files.apply_docs(pipeline, docs)
    return pipeline


@pytest.fixture
def server(tmp_path, monkeypatch):
    block_network(monkeypatch)
    return mcp_server.AgentServer(Session(build(tmp_path)))


def notes(body):
    return {item["column"]: item for item in body.get("column_notes", [])}


# ------------------------------------------------------------------ carried along lineage


def test_a_description_declared_on_a_source_shows_two_hops_downstream_with_inherited_from(server):
    body, failed, used = ask(server, "describe_table", table="orders_clean")
    assert not failed and used <= BOUND
    order_time = notes(body)["order_time"]
    assert order_time["description"] == "Time the order was placed"
    assert order_time["inherited_from"] == "raw.orders.created_at"
    assert order_time["described_in"] == "columns config"
    trace, _, _ = ask(server, "trace_column", table="orders_clean", column="order_time")
    assert trace["meaning"]["description"] == "Time the order was placed"
    assert trace["meaning"]["inherited_from"] == "raw.orders.created_at" and trace["meaning"]["timezone"] == "UTC"


def test_a_cast_of_a_column_keeps_its_unit_and_description(server):
    body, _, _ = ask(server, "describe_table", table="orders_clean")
    amount = notes(body)["amount"]
    assert amount["unit"] == "USD" and amount["description"] == "Order total" and amount["inherited_from"] == "raw.orders.total"


def test_a_declared_fact_on_the_source_column_itself_has_no_inherited_from(server):
    body, _, _ = ask(server, "trace_column", table="raw.orders", column="created_at")
    assert body["meaning"]["timezone"] == "UTC" and "inherited_from" not in body["meaning"]


def test_arithmetic_drops_the_declared_unit_and_scale_and_says_why(server):
    body, _, used = ask(server, "describe_table", table="orders_clean")
    assert used <= BOUND
    assert "doubled" not in notes(body) and "shifted" not in notes(body)
    lost = [item for item in body["unknown"] if "declared time zone, unit or scale" in item["what"]]
    assert lost and "doubled" in lost[0]["what"] and "not carried" in lost[0]["reason"] and "expression" in lost[0]["reason"]
    trace, _, _ = ask(server, "trace_column", table="orders_clean", column="doubled")
    assert "meaning" not in trace
    assert any("declared unit" in item["what"] and "not carried" in item["reason"] for item in trace["unknown"])


def test_an_explicit_conversion_with_a_literal_zone_sets_the_zone(server):
    body, _, _ = ask(server, "describe_table", table="local_orders")
    local = notes(body)["local_time"]
    assert local["timezone"] == "America/New_York" and "conversion" in local["set_by"] and "inherited_from" not in local


def test_a_model_that_declares_its_own_description_keeps_it(tmp_path, monkeypatch):
    block_network(monkeypatch)
    files = dict(FILES)
    files["stg_orders.sqlx"] = (
        'config { type: "table", columns: { placed_at: "Local placement time" } }\n'
        'SELECT id, created_at AS placed_at FROM ${ref("raw", "orders")}\n'
    )
    server = mcp_server.AgentServer(Session(build(tmp_path, files)))
    body, _, _ = ask(server, "describe_table", table="stg_orders")
    placed = notes(body)["placed_at"]
    assert placed["description"] == "Local placement time" and placed["timezone"] == "UTC"
    assert placed["inherited_from"] == "raw.orders.created_at"  # only the zone is inherited; the description is its own


# ------------------------------------------------------------------ zones that are not declared


def test_timestamp_columns_with_no_declared_zone_are_listed_as_unknown_and_grouped(server):
    body, failed, used = ask(server, "describe_table", table="raw.orders")
    assert not failed and used <= BOUND
    [gap] = [item for item in body["unknown"] if "time zone of" in item["what"]]
    assert "updated_at" in gap["what"] and "no zone is declared" in gap["reason"]
    assert body["counts"]["no_declared_zone"] == 1
    checks, _, _ = ask(server, "project_checks")
    [overall] = [item for item in checks["unknown"] if "TIMESTAMP/DATETIME" in item["what"]]
    assert "synced_at" in overall["what"] and "not an error" in overall["reason"]
    assert checks["counts"]["findings"] == 0  # unknown, not a finding


def test_the_unknown_zone_note_names_at_most_three_examples(tmp_path, monkeypatch):
    block_network(monkeypatch)
    wide = {f"t{i}": "TIMESTAMP" for i in range(8)}
    pipeline = load_sqlx_project(write_project(tmp_path / "p", {"d.sqlx": 'config { type: "declaration", schema: "raw", name: "wide" }'}), source_schema={"p.raw.wide": wide})
    body, _, used = ask(mcp_server.AgentServer(Session(pipeline)), "describe_table", table="raw.wide")
    [gap] = [item for item in body["unknown"] if "time zone of" in item["what"]]
    assert "8 TIMESTAMP/DATETIME columns" in gap["what"] and "t3" not in gap["what"] and gap["what"].endswith("...") and used <= BOUND


# ------------------------------------------------------------------ check_query lint


def draft(server, sql):
    body, failed, used = ask(server, "check_query", sql=sql)
    assert not failed and used <= BOUND
    return body


def by_check(body, name):
    return [item for item in body.get("findings", []) if item["check"] == name]


def test_comparing_a_utc_column_with_a_local_zone_column_is_a_finding_naming_both_zones_and_a_fix(server):
    body = draft(server, "SELECT o.id FROM ds.orders_clean AS o JOIN raw.stores AS w ON o.id = w.id WHERE o.order_time > w.local_open")
    [found] = by_check(body, "zones")
    assert "UTC" in found["what"] and "America/Chicago" in found["what"] and found["fix"]
    assert "orders_clean.order_time" in found["what"] and "stores.local_open" in found["what"]
    assert body["verdict"] == "problems_found"


def test_subtracting_columns_of_different_zones_through_a_cte_is_found(server):
    body = draft(server, "WITH e AS (SELECT id, order_time FROM ds.orders_clean) SELECT TIMESTAMP_DIFF(w.local_open, e.order_time, SECOND) FROM e JOIN raw.stores AS w USING (id)")
    assert len(by_check(body, "zones")) == 1


def test_the_same_declared_zone_on_both_sides_is_not_a_finding(server):
    body = draft(server, "SELECT 1 FROM ds.orders_clean AS a JOIN raw.orders AS b ON a.id = b.id WHERE a.order_time = b.created_at")
    assert not by_check(body, "zones")


def test_a_comparison_where_one_side_has_no_declared_zone_is_listed_not_checked(server):
    body = draft(server, "SELECT 1 FROM raw.orders AS o JOIN raw.stores AS w ON o.id = w.id WHERE o.updated_at > w.local_open")
    assert not by_check(body, "zones")
    [gap] = [item for item in body["unknown"] if "comparison of time columns" in item["what"]]
    assert "o.updated_at" in gap["what"] and "no declared time zone" in gap["reason"]


def test_a_project_with_no_declared_zone_says_zone_mixes_are_not_checked(tmp_path, monkeypatch):
    block_network(monkeypatch)
    plain = mcp_server.AgentServer(Session(build(tmp_path, sidecar=None)))
    body = draft(plain, "SELECT 1 FROM raw.orders AS o JOIN raw.stores AS w ON o.id = w.id WHERE o.updated_at > w.synced_at")
    [gap] = [item for item in body["unknown"] if "comparison of time columns" in item["what"]]
    assert "no time zone is declared for either side" in gap["reason"]
    assert "column_meaning" in body["checked"]


def test_current_date_with_no_zone_argument_gets_the_lint_with_a_fix(server):
    body = draft(server, "SELECT id FROM ds.orders_clean WHERE DATE(order_time, 'UTC') = CURRENT_DATE()")
    [found] = by_check(body, "time_zone_default")
    assert "CURRENT_DATE()" in found["what"] and found["level"] == "warning" and "CURRENT_DATE('America/New_York')" in found["fix"]
    assert not by_check(draft(server, "SELECT CURRENT_DATE('America/New_York') AS today"), "time_zone_default")


def test_date_of_a_timestamp_with_no_zone_is_linted_and_names_the_declared_zone(server):
    body = draft(server, "SELECT DATE(created_at) AS day, DATE(created_at, 'UTC') AS fine FROM raw.orders")
    [found] = by_check(body, "time_zone_default")
    assert "DATE(created_at)" in found["what"] and "declared UTC" in found["what"] and "DATE(created_at, '" in found["fix"]


def test_a_month_boundary_cut_without_a_zone_is_linted_but_a_datetime_column_is_not(server):
    assert len(by_check(draft(server, "SELECT TIMESTAMP_TRUNC(created_at, MONTH) AS w FROM raw.orders"), "time_zone_default")) == 1
    assert not by_check(draft(server, "SELECT DATE(local_open) AS d FROM raw.stores"), "time_zone_default")
    assert not by_check(draft(server, "SELECT TIMESTAMP_TRUNC(created_at, HOUR) AS h FROM raw.orders"), "time_zone_default")


def test_multiplying_a_column_declared_on_a_0_100_scale_by_100_is_found(server):
    body = draft(server, "SELECT margin_pct * 100 AS pct FROM raw.orders")
    [found] = by_check(body, "scale")
    assert "0-100" in found["what"] and "second time" in found["what"] and found["fix"] and found["level"] == "warning"
    assert by_check(draft(server, "SELECT margin_pct * 100 AS pct FROM ds.orders_clean"), "scale")  # the declaration carried down


def test_multiplying_by_100_where_no_scale_is_declared_is_listed_not_checked_never_silent(server):
    body = draft(server, "SELECT total * 100 AS cents FROM raw.orders")
    assert not by_check(body, "scale")
    [gap] = [item for item in body["unknown"] if "by 100" in item["what"]]
    assert "total * 100" in gap["what"] and "no scale is declared" in gap["reason"]


def test_a_draft_that_reads_no_declared_meaning_has_no_new_noise(server):
    body = draft(server, "SELECT id FROM raw.orders WHERE id > 3")
    assert body["verdict"] == "no_problems_found" and not any("time columns" in i["what"] or "by 100" in i["what"] for i in body.get("unknown", []))


# ------------------------------------------------------------------ docs, sidecar and loader


DOCS = """# Views

## raw.orders

| Column | Type | Description |
| --- | --- | --- |
| `id` | INT64 | Order id. |
| `updated_at` | TIMESTAMP | Last change, in UTC. |
| `created_at` | TIMESTAMP | Docs text that the config replaces. |
| `ghost` | STRING | A column the table does not have. |

## Not a table of the project

| Column | Description |
| --- | --- |
| `x` | Nothing to attach this to. |
"""


def test_descriptions_are_read_from_markdown_docs_with_their_file_and_the_config_wins(tmp_path, monkeypatch):
    block_network(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "views.md").write_text(DOCS)
    pipeline = build(tmp_path, docs=[docs])
    server = mcp_server.AgentServer(Session(pipeline))
    body, _, _ = ask(server, "describe_table", table="raw.orders")
    found = notes(body)
    assert found["id"]["description"] == "Order id." and found["id"]["described_in"] == "docs views.md"
    assert found["created_at"]["description"] == "Time the order was placed" and found["created_at"]["described_in"] == "columns config"
    downstream, _, _ = ask(server, "describe_table", table="stg_orders")
    assert notes(downstream)["id"]["inherited_from"] == "raw.orders.id"
    assert any(d.code == "column_docs_unmatched" and "2 documented" in d.message for d in pipeline.diagnostics)


def test_a_zone_mentioned_in_docs_is_a_hint_and_the_zone_stays_undeclared(tmp_path, monkeypatch):
    block_network(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "views.md").write_text(DOCS)
    server = mcp_server.AgentServer(Session(build(tmp_path, docs=[docs])))
    body, _, _ = ask(server, "describe_table", table="raw.orders")
    updated = notes(body)["updated_at"]
    assert updated["timezone_hint"] == "docs mention UTC (not declared)" and "timezone" not in updated
    [gap] = [item for item in body["unknown"] if "time zone of" in item["what"]]
    assert "updated_at" in gap["what"] and "a hint, not a declaration" in gap["reason"]
    # a hint never feeds the lint: it is not a declared zone
    check = draft(server, "SELECT 1 FROM raw.orders AS o JOIN raw.stores AS w ON o.id = w.id WHERE o.updated_at > w.local_open")
    assert not by_check(check, "zones")
    assert any("o.updated_at" in item["what"] for item in check["unknown"])
    # a declared zone replaces the hint
    declared, _, _ = ask(server, "trace_column", table="raw.orders", column="created_at")
    assert "timezone_hint" not in declared["meaning"]


def test_reading_docs_never_writes_into_the_project_or_docs_folder(tmp_path, monkeypatch):
    block_network(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "views.md").write_text(DOCS)
    sidecar = tmp_path / "attributes.json"
    sidecar.write_text(json.dumps(SIDECAR))
    project = write_project(tmp_path / "project", FILES)

    def snapshot():
        return sorted((str(p.relative_to(tmp_path)), p.stat().st_mtime_ns, p.stat().st_size) for root in (tmp_path / "project", docs, sidecar) for p in ([root] if root.is_file() else root.rglob("*")) if p.is_file())

    before = snapshot()
    session = Session(load_project(project, None, sidecar, [docs]))
    server = mcp_server.AgentServer(session)
    for table in ("raw.orders", "orders_clean", "local_orders"):
        ask(server, "describe_table", table=table)
    ask(server, "check_query", sql="SELECT CURRENT_DATE()")
    assert snapshot() == before


def test_the_sidecar_file_loads_json_names_what_it_could_not_apply_and_refuses_a_malformed_one(tmp_path, capsys):
    project = write_project(tmp_path / "project", FILES)
    sidecar = tmp_path / "attributes.json"
    sidecar.write_text(json.dumps({**SIDECAR, "stg_orders.nope": {"unit": "x"}, "ghost.col": {"unit": "x"}}))
    pipeline = load_project(project, None, sidecar)
    assert pipeline.column_docs["p.raw.orders"]["created_at"]["timezone"] == "UTC"
    err = capsys.readouterr().err
    assert "stg_orders.nope" in err and "ghost.col" in err
    assert sum(d.code == "column_attributes_unmatched" for d in pipeline.diagnostics) == 2
    for bad in ('["a"]', '{"orders": {"timezone": "UTC"}}', '{"orders.created_at": {"colour": "red"}}', '{"orders.created_at": {"timezone": 5}}', "not json"):
        sidecar.write_text(bad)
        with pytest.raises(Exception, match="column attributes|must|unknown attribute"):
            load_project(project, None, sidecar)


def test_the_sidecar_file_may_be_yaml(tmp_path):
    pytest.importorskip("yaml")
    project = write_project(tmp_path / "project", FILES)
    sidecar = tmp_path / "attributes.yaml"
    sidecar.write_text("orders.created_at:\n  timezone: UTC\norders.margin_pct:\n  scale: 0-100\n")
    pipeline = load_project(project, None, sidecar)
    assert pipeline.column_docs["p.raw.orders"]["created_at"]["timezone"] == "UTC"
    assert pipeline.column_docs["p.raw.orders"]["margin_pct"]["scale"] == "0-100"


def test_the_server_options_are_wired_to_the_loader(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp_server, "load_project", lambda root, schema, attributes, docs: seen.update(attributes=attributes, docs=docs) or load_sqlx_project(root))
    monkeypatch.setattr(mcp_server.profile_mcp, "serve", lambda server, *_: server.session.pipeline())
    monkeypatch.setattr(mcp_server.mcp_cache, "Index", lambda session, **kw: type("I", (), {"start": lambda self: None})())
    project = write_project(tmp_path / "project", FILES)
    mcp_server.main([str(project), "--column-attributes", str(tmp_path / "a.json"), "--docs", str(tmp_path / "d1"), "--docs", str(tmp_path / "d2"), "--no-cache"])
    assert seen["attributes"] == tmp_path / "a.json" and seen["docs"] == [tmp_path / "d1", tmp_path / "d2"]


def test_a_missing_docs_folder_is_named(tmp_path):
    pipeline = build(tmp_path)
    notes_ = column_docs_files.apply_docs(pipeline, [tmp_path / "nowhere"])
    assert notes_ == ["docs folder not found: nowhere"]


def test_compiled_graph_action_descriptors_carry_descriptions(tmp_path):
    from kumosql.pipeline_loading import load_compiled_graph

    graph = {
        "tables": [{"target": {"database": "p", "schema": "ds", "name": "t"}, "type": "table", "query": "SELECT id FROM `p.raw.s`",
                    "actionDescriptor": {"columns": [{"path": ["id"], "description": "Row id"}]}}],
        "declarations": [{"target": {"database": "p", "schema": "raw", "name": "s"}, "actionDescriptor": {"columns": [{"path": ["id"], "description": "Source id"}]}}],
    }
    pipeline = load_compiled_graph(graph)
    server = mcp_server.AgentServer(Session(pipeline))
    body, _, _ = ask(server, "describe_table", table="t")
    assert notes(body)["id"]["description"] == "Row id"
    assert pipeline.column_docs["p.raw.s"]["id"]["description"] == "Source id"


def test_the_saved_index_is_rebuilt_when_the_sidecar_or_a_docs_page_changes(tmp_path, monkeypatch):
    from kumosql import mcp_cache

    captured = {}
    monkeypatch.setattr(mcp_cache, "Index", lambda session, **kw: captured.update(kw) or type("I", (), {"start": lambda self: None})())
    monkeypatch.setattr(mcp_server.profile_mcp, "serve", lambda *_: None)
    docs = tmp_path / "docs"
    docs.mkdir()
    page = docs / "views.md"
    page.write_text(DOCS)
    sidecar = tmp_path / "attributes.json"
    sidecar.write_text(json.dumps(SIDECAR))
    mcp_server.main([str(write_project(tmp_path / "project", FILES)), "--column-attributes", str(sidecar), "--docs", str(docs), "--no-cache"])
    watched = {path.name for path in captured["watch"]()}
    assert {"attributes.json", "views.md"} <= watched
