"""Column descriptions declared in JavaScript and in a compiled graph: read like the ``.sqlx`` config, never guessed."""

import json

import pytest

from kumosql import column_docs, mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_compiled_graph, load_sqlx_project

from test_column_meaning import notes
from test_mcp_server import ask, block_network, write_project

BOUND = 2000

FILES = {
    "sources.js": """
declare({
  schema: "raw", name: "orders",
  columns: {
    created_at: { description: "Time the order was placed", timezone: "UTC" },
    total: { description: "Order total", unit: "USD", scale: "0-1" },
    // a comment between entries
    status: 'Order "state"',
    note: `Free text for ${label}`,
    region: lookup("region"),
    channel,
  },
});
declare({ schema: "raw", name: "customers", columns: customerColumns });
declare({ schema: "raw", name: "stores", columns: { open_date: "Day the store opened" } });
""",
    "stg_orders.js": """
publish("stg_orders")
  .columns({ placed_at: "Local placement time", total: { description: "Total in the order currency" } })
  .query(ctx => `SELECT id, created_at AS placed_at, total, note, status FROM ${ctx.ref("raw", "orders")}`);
""",
    "orders_clean.js": """
publish("orders_clean", { type: "table" })
  .config({ columns: { id: { description: "Order id" } } })
  .query(ctx => `SELECT id, placed_at AS order_time, total AS amount, note FROM ${ctx.ref("stg_orders")}`);
""",
    "store_names.js": """
publish("store_names", { columns: { name: "Store display name" } })
  .query(ctx => `SELECT open_date AS name FROM ${ctx.ref("raw", "stores")}`);
""",
}


def build(tmp_path, files=FILES):
    return load_sqlx_project(write_project(tmp_path / "project", files))


@pytest.fixture
def server(tmp_path, monkeypatch):
    block_network(monkeypatch)
    return mcp_server.AgentServer(Session(build(tmp_path)))


def unknowns(body):
    return body.get("unknown", [])


# ------------------------------------------------------------------ declare() and publish() in JavaScript


def test_a_declare_call_with_a_columns_block_gives_its_columns_a_description_with_the_file(server):
    body, failed, used = ask(server, "describe_table", table="raw.orders")
    assert not failed and used <= BOUND
    found = notes(body)
    assert found["created_at"]["description"] == "Time the order was placed" and found["created_at"]["timezone"] == "UTC"
    assert found["created_at"]["described_in"] == "columns config in definitions/sources.js"
    assert found["total"]["unit"] == "USD" and found["total"]["scale"] == "0-1"
    assert found["status"]["description"] == 'Order "state"'


def test_a_description_declared_in_javascript_shows_two_hops_downstream_with_inherited_from(server):
    body, _, used = ask(server, "describe_table", table="orders_clean", max_bytes=6000)
    assert used <= 6000
    found = notes(body)
    assert found["order_time"]["description"] == "Local placement time"
    assert found["order_time"]["inherited_from"] == {"description": "ds.stg_orders.placed_at", "timezone": "raw.orders.created_at"}
    assert found["amount"]["unit"] == "USD" and found["amount"]["inherited_from"]["unit"] == "raw.orders.total"
    assert found["id"]["description"] == "Order id" and found["id"]["described_in"] == "columns config in definitions/orders_clean.js"
    trace, _, _ = ask(server, "trace_column", table="orders_clean", column="order_time")
    assert trace["meaning"]["description"] == "Local placement time" and trace["meaning"]["described_in"].endswith("stg_orders.js")


def test_columns_chained_before_the_query_do_not_hide_the_model(server):
    body, failed, _ = ask(server, "describe_table", table="stg_orders")
    assert not failed and body["kind"] == "table" and body["file"] == "definitions/stg_orders.js"
    assert notes(body)["placed_at"]["description"] == "Local placement time"


def test_columns_in_the_config_object_of_publish_are_read_with_their_file(server):
    body, _, _ = ask(server, "describe_table", table="store_names")
    assert notes(body)["name"]["description"] == "Store display name"
    assert notes(body)["name"]["described_in"] == "columns config in definitions/store_names.js"


def test_a_column_declared_in_javascript_that_a_model_redeclares_keeps_the_models_own_text(server):
    body, _, _ = ask(server, "describe_table", table="stg_orders")
    total = notes(body)["total"]
    assert total["description"] == "Total in the order currency"
    assert total["unit"] == "USD" and total["inherited_from"] == "raw.orders.total"  # only the unit is inherited


# ------------------------------------------------------------------ what cannot be evaluated is never guessed


def test_a_template_string_with_a_variable_and_a_call_are_reported_under_unknown_with_the_reason(server):
    body, _, used = ask(server, "describe_table", table="raw.orders")
    assert used <= BOUND
    found = notes(body)
    assert "note" not in found and "region" not in found and "channel" not in found
    reasons = {item["what"]: item["reason"] for item in unknowns(body) if item["what"].startswith("declared description")}
    assert any("note" in what and "template string with a variable" in reason for what, reason in reasons.items())
    assert any("region" in what and "function call" in reason for what, reason in reasons.items())
    assert any("channel" in what and "variable" in reason for what, reason in reasons.items())
    assert all(reason.startswith("not read:") and "sources.js" in reason for reason in reasons.values())


def test_one_unreadable_entry_does_not_hide_the_readable_ones_next_to_it(server):
    body, _, _ = ask(server, "describe_table", table="raw.orders")
    assert {"created_at", "total", "status"} <= set(notes(body))


def test_a_whole_columns_value_that_is_a_variable_is_one_unknown_for_the_table(server):
    body, _, _ = ask(server, "describe_table", table="raw.customers")
    [item] = [u for u in unknowns(body) if "column descriptions" in u["what"]]
    assert "customers" in item["what"] and "variable" in item["reason"] and "sources.js" in item["reason"]
    assert not body.get("column_notes")


def test_an_unread_description_is_named_on_the_models_that_copy_the_column(server):
    body, _, _ = ask(server, "describe_table", table="orders_clean")
    assert "note" not in notes(body)
    assert any("note (from raw.orders.note)" in u["what"] and "template string" in u["reason"] for u in unknowns(body))
    trace, _, _ = ask(server, "trace_column", table="orders_clean", column="note")
    assert "meaning" not in trace
    assert any("declared description of raw.orders.note" == u["what"] and "template string" in u["reason"] for u in unknowns(trace))


def test_a_description_with_an_escape_sequence_or_a_concatenation_is_not_decoded(tmp_path):
    pipeline = build(tmp_path, {"s.js": r'''declare({ schema: "raw", name: "t", columns: { a: "line\nbreak", b: "x" + "y", c: ok ? "p" : "q", d: "plain \"quote\"" } });'''})
    assert pipeline.column_docs["p.raw.t"] == {"d": {"description": 'plain "quote"'}}
    reasons = {column: reason for column, _attribute, reason, _file in pipeline.column_doc_unread["p.raw.t"]}
    assert "escape sequence" in reasons["a"] and "built with +" in reasons["b"] and "conditional" in reasons["c"]


def test_the_sqlx_config_reports_an_unreadable_description_the_same_way(tmp_path, monkeypatch):
    block_network(monkeypatch)
    files = {
        "src.sqlx": 'config { type: "declaration", schema: "raw", name: "orders", columns: { id: "Order id", note: `Text ${x}`, amount: {description: "Total", unit: units.money} } }',
        "m.sqlx": 'config { type: "table" }\nSELECT id, note, amount FROM ${ref("raw", "orders")}\n',
    }
    pipeline = build(tmp_path, files)
    body, _, _ = ask(mcp_server.AgentServer(Session(pipeline)), "describe_table", table="raw.orders")
    assert notes(body)["id"]["description"] == "Order id" and notes(body)["amount"]["description"] == "Total"
    assert "unit" not in notes(body)["amount"]
    whats = [u["what"] for u in unknowns(body)]
    assert any("declared description" in w and "note" in w for w in whats) and any("declared unit" in w and "amount" in w for w in whats)


# ------------------------------------------------------------------ the pieces of the JavaScript forms


def test_two_readings_of_one_action_agree_or_are_dropped_and_reported(tmp_path):
    files = {
        "s.js": """
declare({ schema: "raw", name: "t", columns: { a: "Same text", b: "First text" } });
""",
        "p.js": """
publish("m", { columns: { a: "Same text", b: "First text", c: "Only here" } })
  .columns({ a: "Same text", b: "Second text" })
  .query(ctx => `SELECT 1 AS a, 2 AS b, 3 AS c`);
""",
    }
    pipeline = build(tmp_path, files)
    assert pipeline.column_docs["p.ds.m"] == {"a": {"description": "Same text"}, "c": {"description": "Only here"}}
    assert [(c, a, r) for c, a, r, _ in pipeline.column_doc_unread["p.ds.m"]] == [("b", "description", "declared twice with different text")]


def test_a_loop_over_a_literal_list_reads_each_tables_columns(tmp_path):
    files = {"loop.js": """
const tables = [{ name: "events_a", about: "Clicks" }, { name: "events_b", about: "Views" }];
tables.forEach(t => declare({ schema: "raw", name: t.name, columns: { id: t.about } }));
"""}
    pipeline = build(tmp_path, files)
    assert pipeline.column_docs["p.raw.events_a"] == {"id": {"description": "Clicks"}}
    assert pipeline.column_docs["p.raw.events_b"] == {"id": {"description": "Views"}}


def test_an_operate_call_or_an_unmodelled_publish_is_a_diagnostic_not_a_silent_loss(tmp_path):
    files = {
        "ops.js": """
operate("refresh_orders", ctx => `SELECT 1`).config({ hasOutput: true, columns: { id: "Row id" } });
publish("computed_query", { columns: { id: "Row id" } }).query(ctx => buildSql(ctx));
""",
        "m.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
    }
    pipeline = build(tmp_path, files)
    [item] = [d for d in pipeline.diagnostics if d.code == "js_column_docs_unattached"]
    assert item.model == "definitions/ops.js" and "2 column declarations" in item.message
    assert not any(d.code == "js_declaration_dynamic" for d in pipeline.diagnostics)  # operate settles no table name
    assert "p.ds.refresh_orders" not in pipeline.column_docs


def test_a_sidecar_value_replaces_a_description_declared_in_javascript(tmp_path):
    pipeline = build(tmp_path)
    column_docs.apply_attributes(pipeline, {"orders.total": {"unit": "EUR"}})
    server = mcp_server.AgentServer(Session(pipeline))
    body, _, _ = ask(server, "describe_table", table="raw.orders")
    total = notes(body)["total"]
    assert total["unit"] == "EUR" and total["description"] == "Order total"


def test_reading_javascript_columns_never_writes_into_the_project(tmp_path):
    root = write_project(tmp_path / "project", FILES)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    pipeline = load_sqlx_project(root)
    ask(mcp_server.AgentServer(Session(pipeline)), "describe_table", table="raw.orders")
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


# ------------------------------------------------------------------ compiled graph


def compiled(extra=None):
    graph = {
        "tables": [{"target": {"database": "p", "schema": "ds", "name": "t"}, "type": "table", "fileName": "definitions/t.sqlx",
                    "query": "SELECT id, created_at AS made_at, total, address FROM `p.raw.s`",
                    "actionDescriptor": {"columns": [
                        {"path": ["id"], "description": "Row id"},
                        {"path": ["address", "city"], "description": "City inside the address"},
                        {"path": ["total"], "description": 5},
                    ]}}],
        "declarations": [{"target": {"database": "p", "schema": "raw", "name": "s"},
                          "actionDescriptor": {"columns": [
                              {"path": ["created_at"], "description": "When it was made", "timezone": "UTC"},
                              {"path": ["total"], "description": "Total", "unit": "USD", "scale": "0-100"},
                          ]}}],
        **(extra or {}),
    }
    return load_compiled_graph(graph)


def test_a_compiled_graph_gives_descriptions_zone_unit_and_scale_with_its_origin(monkeypatch):
    block_network(monkeypatch)
    server = mcp_server.AgentServer(Session(compiled()))
    body, _, _ = ask(server, "describe_table", table="t")
    found = notes(body)
    assert found["made_at"]["description"] == "When it was made" and found["made_at"]["timezone"] == "UTC"
    assert found["made_at"]["inherited_from"] == "raw.s.created_at"
    assert found["made_at"]["described_in"] == "compiled graph actionDescriptor"
    assert found["id"]["description"] == "Row id" and "inherited_from" not in found["id"]
    source, _, _ = ask(server, "describe_table", table="raw.s")
    assert notes(source)["total"]["unit"] == "USD" and notes(source)["total"]["scale"] == "0-100"


def test_a_compiled_description_that_is_not_text_or_names_a_nested_field_is_reported_not_read(monkeypatch):
    block_network(monkeypatch)
    pipeline = compiled()
    reasons = {(column, reason) for column, _a, reason, _f in pipeline.column_doc_unread["p.ds.t"]}
    assert ("address", "a description for a field inside the column is not read") in reasons
    assert ("total", "the declared value is not text") in reasons
    server = mcp_server.AgentServer(Session(pipeline))
    body, _, _ = ask(server, "describe_table", table="t")
    assert "address" not in notes(body)
    assert any("address" in u["what"] and "inside the column" in u["reason"] for u in unknowns(body))


def test_a_compiled_entry_may_also_use_the_config_mapping_form():
    graph = {"declarations": [{"target": {"database": "p", "schema": "raw", "name": "s"},
                               "actionDescriptor": {"columns": {"a": "From a mapping", "b": {"description": "Object form", "unit": "kg"}}}}]}
    docs = load_compiled_graph(graph).column_docs["p.raw.s"]
    assert docs == {"a": {"description": "From a mapping"}, "b": {"description": "Object form", "unit": "kg"}}


def test_compiled_graph_json_on_disk_loads_the_same_way(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"declarations": [{"target": {"database": "p", "schema": "raw", "name": "s"},
                                                   "actionDescriptor": {"columns": [{"path": ["a"], "description": "From disk"}]}}]}))
    assert load_compiled_graph(path).column_docs["p.raw.s"]["a"]["description"] == "From disk"
