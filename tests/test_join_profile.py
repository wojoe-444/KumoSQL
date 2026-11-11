"""join_profile, the grain sources behind it, and how check_query uses what they know (synthetic tables throughout)."""

import json
from pathlib import Path

import duckdb
import pytest

from kumosql import data_profile, mcp_server
from kumosql import docs_keys, join_profile_store as joins, malloy_keys

mcp_server.load_tools()  # register the tools in the server's order before any tool module is imported on its own
from kumosql.data_profile import DuckDBExecutor
from kumosql.mcp_join_profile import build_sql, parse_keys, profile_join
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from test_mcp_server import BOUND, ask, block_network, write_project

PROJECT = {
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, 10 AS customer_id, "w1" AS order_week\n',
    "customers_view.js": 'declare({schema: "wh", name: "customers_view"});\n',
}
DRAFT = "SELECT o.order_id, SUM(o.customer_id) FROM p.ds.orders o JOIN p.wh.order_events e ON e.order_id = o.order_id GROUP BY o.order_id"


def tables(connection):
    connection.execute("CREATE TABLE orders AS SELECT * FROM (VALUES (1,10,'w1'),(2,10,'w1'),(3,11,'w2'),(4,12,'w2'),(5,NULL,'w3'),(6,99,'w3')) t(order_id,customer_id,order_week)")
    connection.execute("CREATE TABLE customers AS SELECT * FROM (VALUES (10,'a'),(11,'b'),(12,'c'),(13,'d')) t(id,name)")
    # three events for order 1, two for order 2, one for order 3, none for 4 to 6, one orphan event
    connection.execute("CREATE TABLE order_events AS SELECT * FROM (VALUES (1,'x'),(1,'y'),(1,'z'),(2,'x'),(2,'y'),(3,'x'),(77,'x')) t(order_id,kind)")
    connection.execute("CREATE TABLE ones AS SELECT * FROM (VALUES (1),(2),(3)) t(id)")
    connection.execute("CREATE TABLE also_ones AS SELECT * FROM (VALUES (2),(3),(4),(NULL)) t(id)")
    connection.execute("CREATE TABLE pairs_a AS SELECT * FROM (VALUES (1,'a'),(1,'a'),(1,'b'),(2,NULL),(NULL,'a')) t(x,y)")
    connection.execute("CREATE TABLE pairs_b AS SELECT * FROM (VALUES (1,'a'),(1,'a'),(2,'b'),(3,'c')) t(x,y)")
    return connection


@pytest.fixture
def conn():
    connection = tables(duckdb.connect())
    yield connection
    connection.close()


def run(conn, left, right, keys, split=None, top=5):
    return profile_join(left, right, parse_keys(keys), DuckDBExecutor(conn), split, top)


# ------------------------------------------------------- the numbers, against ground truth


def test_a_lookup_join_is_n_to_1_and_the_rates_count_rows_with_null_keys_in_the_whole_population(conn):
    result = run(conn, "orders", "customers", "customer_id=id")
    left, right = result.sides["left"], result.sides["right"]
    assert result.cardinality == "N:1"
    # 6 orders: customer 10 twice, 11, 12, one NULL key, one unknown customer 99
    assert (left["rows"], left["null_key_rows"], left["distinct_keys"], left["duplicate_rows"], left["max_rows_per_key"]) == (6, 1, 4, 1, 2)
    assert (left["matched_rows"], left["match_rate"], left["null_key_rate"]) == (4, round(4 / 6, 6), round(1 / 6, 6))
    assert (right["rows"], right["duplicate_rows"], right["matched_rows"], right["match_rate"]) == (4, 0, 3, 0.75)
    assert result.join_rows == 4 and result.matched_keys == 3


def test_one_to_many_and_the_inner_join_row_count_show_the_fan_out(conn):
    result = run(conn, "orders", "order_events", "order_id")
    assert result.cardinality == "1:N"
    right = result.sides["right"]
    # events: order 1 three times, order 2 twice, 3 once, 77 once = 7 rows, 4 keys, 3 repeated rows
    assert (right["rows"], right["distinct_keys"], right["duplicate_rows"], right["max_rows_per_key"]) == (7, 4, 3, 3)
    assert (right["matched_rows"], result.sides["left"]["matched_rows"]) == (6, 3)
    assert result.join_rows == 3 + 2 + 1  # orders 1, 2 and 3 match 3, 2 and 1 events
    assert result.sides["left"]["match_rate"] == 0.5


def test_one_to_one_and_many_to_many_classes(conn):
    one = run(conn, "ones", "also_ones", "id")
    assert one.cardinality == "1:1" and one.sides["left"]["match_rate"] == round(2 / 3, 6) and one.sides["right"]["null_key_rows"] == 1
    assert one.sides["right"]["match_rate"] == 0.5 and one.join_rows == 2
    many = run(conn, "pairs_a", "pairs_b", "x,y")
    left, right = many.sides["left"], many.sides["right"]
    assert many.cardinality == "N:M"
    # pairs_a: (1,a) twice, (1,b), (2,NULL) and (NULL,a): the last two have a null in the key and never match
    assert (left["rows"], left["null_key_rows"], left["distinct_keys"], left["duplicate_rows"]) == (5, 2, 2, 1)
    assert (right["rows"], right["distinct_keys"], right["duplicate_rows"]) == (4, 3, 1)
    assert many.join_rows == 4 and left["matched_rows"] == 2 and right["matched_rows"] == 2  # (1,a): 2 x 2


def test_a_side_without_any_key_value_has_no_class(conn):
    conn.execute("CREATE TABLE nothing AS SELECT CAST(NULL AS INTEGER) AS id FROM range(3)")
    assert run(conn, "nothing", "ones", "id").cardinality == "unknown"


def test_split_by_counts_unmatched_rows_per_value_largest_first_with_ties_in_value_order(conn):
    result = run(conn, "orders", "customers", "customer_id=id", ("left", "order_week"))
    # unmatched: order 5 (NULL key) and order 6 (customer 99), both week w3
    assert result.unmatched_by == [{"value": "w3", "rows": 2, "unmatched": 2}]
    wide = run(conn, "orders", "order_events", "order_id", ("left", "order_week"))
    # events exist for orders 1 to 3 only: order 4 (w2) and orders 5, 6 (w3) are unmatched, and w1 has none
    assert wide.unmatched_by == [{"value": "w3", "rows": 2, "unmatched": 2}, {"value": "w2", "rows": 2, "unmatched": 1}]
    assert run(conn, "orders", "order_events", "order_id", ("left", "order_week"), top=1).unmatched_by == [{"value": "w3", "rows": 2, "unmatched": 2}]
    right = run(conn, "customers", "orders", "id=customer_id", ("right", "order_week"))
    assert right.unmatched_by == [{"value": "w3", "rows": 2, "unmatched": 2}]


def test_the_same_question_gives_the_same_answer_every_time(conn):
    first = run(conn, "orders", "order_events", "order_id", ("left", "order_week")).to_json()
    again = run(conn, "orders", "order_events", "order_id", ("left", "order_week")).to_json()
    first.pop("generated_at"), again.pop("generated_at")
    assert first == again


def test_the_query_is_one_statement_and_reads_each_table_once_before_grouping():
    sql = build_sql('"a"', '"b"', parse_keys("x=y"), "duckdb", ("left", "w"), 3)
    assert sql.count("FROM \"a\"") == 1 and sql.count("FROM \"b\"") == 1 and ";" not in sql and "LIMIT 3" in sql and "RANDOM" not in sql.upper()


def test_keys_and_split_are_plain_column_names():
    assert parse_keys("a, b=c") == [("a", "a"), ("b", "c")]
    for bad in ("", "a; DROP TABLE x", "a=b=c d", "a.b"):
        with pytest.raises(Exception):
            parse_keys(bad)


# --------------------------------------------------------------------- the tool


def server_for(tmp_path, files=PROJECT, **session):
    project = write_project(tmp_path / "proj", files)
    pipeline = load_sqlx_project(project)
    return mcp_server.AgentServer(Session(pipeline, root=project, profiles=tmp_path / "profiles", **session)), project


def fake_warehouse(monkeypatch, estimate=300_000, cap=1_000_000):
    """BigQuery backed by DuckDB tables named by their last part; records what ran."""

    connection = tables(duckdb.connect())
    ran = []

    class Fake(DuckDBExecutor):
        max_bytes = cap

        def __init__(self, project=None, max_bytes=None):
            super().__init__(connection)

        def table_sql(self, table):
            return super().table_sql(table.split(".")[-1])

        def fields(self, table):
            return super().fields(table.split(".")[-1])

        def estimate(self, sql):
            return estimate

        def run(self, sql):
            ran.append(sql)
            return super().run(sql)

    monkeypatch.setattr(data_profile, "BigQueryExecutor", Fake)
    return ran


def test_join_profile_without_a_connection_returns_the_plan_and_sends_nothing(tmp_path):
    server, _ = server_for(tmp_path)
    block_network(pytest.MonkeyPatch())
    body, failed, used = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", split_by="order_week")
    assert not failed and used <= BOUND and body["mode"] == "plan_only" and "nothing was sent" in body["answer"]
    assert body["left"] == "p.ds.orders" and body["right"] == "p.wh.order_events" and "cardinality" in body["returns"]
    assert body["sql"][0].startswith("WITH l AS (SELECT `order_id` AS k0, `order_week` AS s FROM `p.ds.orders`)")
    assert body["unknown"][0]["what"] == "row counts and match rates" and "project=" in body["next"][0]
    assert not (tmp_path / "profiles").exists()


def test_a_warehouse_table_written_as_dataset_table_is_read_in_the_default_project(tmp_path):
    server, _ = server_for(tmp_path)
    body, failed, _ = ask(server, "join_profile", left="orders", right="wh.order_events", keys="order_id", max_bytes=6000)
    assert not failed and body["right"] == "p.wh.order_events"
    body, failed, _ = ask(server, "join_profile", left="orders", right="nowhere", keys="order_id")
    assert failed and body["next"]


def test_with_a_project_join_profile_is_a_dry_run_until_run_is_true_and_a_run_is_saved_outside_the_project(tmp_path, monkeypatch):
    ran = fake_warehouse(monkeypatch)
    server, project = server_for(tmp_path)
    before = sorted(str(p) for p in project.rglob("*"))
    body, failed, used = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing")
    assert not failed and used <= BOUND and body["mode"] == "dry_run" and ran == [] and body["scan"] == {"estimated_bytes": 300_000, "byte_cap": 1_000_000}
    assert "run=true" in body["next"][0] and "left=p.ds.orders right=p.wh.order_events keys=order_id" in body["next"][0]
    body, failed, used = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing", run=True, split_by="order_week")
    assert not failed and used <= BOUND and body["mode"] == "profiled" and len(ran) == 1
    assert body["cardinality"] == "1:N" and body["join_rows"] == 6 and body["left"]["match_rate"] == 0.5 and body["right"]["duplicate_rows"] == 3
    assert body["unmatched_by"] == ['"w3": 2 of 2 unmatched (100.0%)', '"w2": 1 of 2 unmatched (50.0%)'] and body["unmatched_rows"] == 3
    assert body["saved"].startswith("join-profiles/") and (tmp_path / "profiles" / "join-profiles").is_dir()
    assert sorted(str(p) for p in project.rglob("*")) == before  # nothing written into the project


def test_join_profile_refuses_a_plan_over_the_cap_and_a_run_without_a_project(tmp_path, monkeypatch):
    ran = fake_warehouse(monkeypatch, estimate=2_000_000)
    server, _ = server_for(tmp_path)
    for extra in ({}, {"run": True}):
        body, failed, _ = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing", **extra)
        assert not failed and body["mode"] == "refused" and "over the 1,000,000-byte cap" in body["answer"] and ran == []
    body, failed, _ = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", run=True)
    assert failed and "explicit project" in body["error"]
    assert not (tmp_path / "profiles").exists()


def test_join_profile_says_not_checked_without_credentials(tmp_path, monkeypatch):
    def refuse(project=None, max_bytes=None):
        raise data_profile.ProfileError("no BigQuery credentials found")

    monkeypatch.setattr(data_profile, "BigQueryExecutor", refuse)
    server, _ = server_for(tmp_path)
    body, failed, _ = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing", run=True)
    assert not failed and body["mode"] == "not_run" and body["unknown"][0]["reason"] == "not checked: no BigQuery credentials found"


def test_a_local_duckdb_run_is_free_immediate_and_not_saved(tmp_path):
    server, project = server_for(tmp_path)
    database = project / "replica.duckdb"
    connection = duckdb.connect(str(database))
    tables(connection)
    connection.close()
    body, failed, used = ask(server, "join_profile", left="orders", right="customers", keys="customer_id=id", duckdb="replica.duckdb")
    assert not failed and used <= BOUND and body["mode"] == "profiled" and body["cardinality"] == "N:1" and "saved" not in body
    assert "not saved" in body["next"][0]
    body, failed, _ = ask(server, "join_profile", left="orders", right="customers", keys="customer_id=id", duckdb="replica.duckdb", project="billing")
    assert failed
    assert not (tmp_path / "profiles").exists()


def test_a_profile_folder_inside_the_project_is_not_written_to(tmp_path, monkeypatch):
    fake_warehouse(monkeypatch)
    project = write_project(tmp_path / "proj", PROJECT)
    server = mcp_server.AgentServer(Session(load_sqlx_project(project), root=project, profiles=project / "saved"))
    body, failed, _ = ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing", run=True)
    assert not failed and body["mode"] == "profiled" and "saved" not in body and not (project / "saved").exists()


# ------------------------------------------------- check_query: without and with a profile


def fan_out(body):
    return [f for f in body.get("findings", []) if f["check"] == "fan_out"]


def test_without_a_profile_the_unknown_names_the_exact_join_profile_call(tmp_path):
    server, _ = server_for(tmp_path)
    body, failed, used = ask(server, "check_query", sql=DRAFT)
    assert not failed and used <= BOUND and not fan_out(body)
    [gap] = [u for u in body["unknown"] if u["what"].startswith("whether the JOIN to e multiplies rows")]
    assert "join_profile left=p.ds.orders right=p.wh.order_events keys=order_id" in gap["reason"] and "no known grain" not in gap["reason"].replace("grain is unknown", "")
    assert any(step.startswith("join_profile left=p.ds.orders right=p.wh.order_events keys=order_id") for step in body["next"])


def test_after_join_profile_ran_check_query_reports_the_fan_out_with_no_extra_call(tmp_path, monkeypatch):
    fake_warehouse(monkeypatch)
    server, _ = server_for(tmp_path)
    ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing", run=True)
    body, failed, used = ask(server, "check_query", sql=DRAFT)
    assert not failed and used <= BOUND and body["verdict"] == "problems_found"
    [found] = fan_out(body)
    assert "repeats on (order_id)" in found["what"] and "3 duplicate rows in 7" in found["what"] and "join profile of " in found["what"] and "SUM" in found["what"]
    assert "near-key" not in found["what"] and found["fix"].startswith("Reduce e to one row per (order_id)")
    assert not any(u["what"].startswith("whether the JOIN to e") for u in body["unknown"])


def test_a_saved_measurement_with_no_repeats_removes_the_unknown_and_says_it_is_assumed(tmp_path, monkeypatch):
    fake_warehouse(monkeypatch)
    server, _ = server_for(tmp_path)
    draft = "SELECT o.order_id FROM p.ds.orders o JOIN p.wh.customers c ON c.id = o.customer_id"
    ask(server, "join_profile", left="orders", right="p.wh.customers", keys="customer_id=id", project="billing", run=True)
    body, _, _ = ask(server, "check_query", sql=draft)
    assert not fan_out(body) and not any(u["what"].startswith("whether the JOIN to c") for u in body["unknown"])
    assert "no repeats on (id)" in body["fan_out_judged_assuming"][0] and "assumed to hold now" in body["fan_out_judged_assuming"][0]


def test_a_measurement_of_another_key_does_not_decide_a_join_on_different_columns(tmp_path, monkeypatch):
    fake_warehouse(monkeypatch)
    server, _ = server_for(tmp_path)
    ask(server, "join_profile", left="orders", right="p.wh.order_events", keys="order_id", project="billing", run=True)
    body, _, _ = ask(server, "check_query", sql="SELECT o.order_id FROM p.ds.orders o JOIN p.wh.order_events e ON e.kind = o.order_week")
    [gap] = [u for u in body["unknown"] if u["what"].startswith("whether the JOIN to e")]
    assert "keys=order_week=kind" in gap["reason"]


# ------------------------------------------------- profile_table: key candidates and near-keys


def big_events(connection, rows=200, repeats=1):
    connection.execute(f"CREATE TABLE order_events AS SELECT i AS order_id, 'x' AS kind FROM range({rows}) t(i)")
    for _ in range(repeats):
        connection.execute("INSERT INTO order_events VALUES (0, 'dup')")


def test_profile_table_saves_a_bigquery_profile_and_lists_its_key_candidates_with_their_rates(tmp_path, monkeypatch):
    fake_warehouse(monkeypatch)
    server, _ = server_for(tmp_path)
    body, failed, used = ask(server, "profile_table", table="p.wh.order_events", project="billing", run=True)
    assert not failed and used <= BOUND and body["mode"] == "profiled"
    assert body["saved"] == "kumosql://data-profile/p.wh.order_events/summary.md"
    assert "key_candidates" not in body  # 3 repeated rows in 7 is not a near-key
    assert (tmp_path / "profiles" / "p.wh.order_events.json").is_file()
    from kumosql import data_profile_store as store

    assert "## Key candidates" not in store.to_markdown(store.load("p.wh.order_events", tmp_path / "profiles"))


def test_a_near_key_in_a_saved_profile_is_reported_as_one_with_its_counts_and_describe_table_shows_it_as_assumed(tmp_path, monkeypatch):
    connection = duckdb.connect()
    big_events(connection, rows=300, repeats=2)
    class Fake(DuckDBExecutor):
        max_bytes = 10**9

        def __init__(self, project=None, max_bytes=None):
            super().__init__(connection)

        def table_sql(self, table):
            return super().table_sql(table.split(".")[-1])

        def fields(self, table):
            return super().fields(table.split(".")[-1])

        def estimate(self, sql):
            return 1

    monkeypatch.setattr(data_profile, "BigQueryExecutor", Fake)
    server, _ = server_for(tmp_path, {**PROJECT, "events.js": 'declare({schema: "wh", name: "order_events"});\n'})
    body, _, _ = ask(server, "profile_table", table="p.wh.order_events", project="billing", run=True)
    [line] = [c for c in body["key_candidates"] if c.startswith("order_id")]
    assert line.startswith("order_id: near-key: 2 duplicate rows in 302 (0.66%), 0% null")
    from kumosql import data_profile_store as store

    assert "- order_id: near-key: 2 duplicate rows in 302" in store.to_markdown(store.load("p.wh.order_events", tmp_path / "profiles"))
    body, _, _ = ask(server, "describe_table", table="p.wh.order_events")
    assert body["grain_assumed"].startswith("near-key on (order_id): 2 duplicate rows in 302") and "assumption" in body["grain_assumed"] and "grain_origin" not in body
    body, _, _ = ask(server, "check_query", sql=DRAFT)
    [found] = fan_out(body)
    assert "is a near-key on (order_id)" in found["what"] and "2 duplicate rows in 302" in found["what"] and "data profile of" in found["what"] and "assumption" in found["what"]


def test_a_sampled_or_filtered_profile_says_nothing_about_the_table(tmp_path):
    connection = duckdb.connect()
    big_events(connection, rows=50, repeats=0)
    exact = data_profile.profile_table("order_events", DuckDBExecutor(connection))
    assert joins.observations_from_profile(exact)
    for options in ({"sample_percent": 50}, {"row_filter": "order_id > 3"}):
        assert joins.observations_from_profile(data_profile.profile_table("order_events", DuckDBExecutor(connection), **options)) == []


# ----------------------------------------------------------- stated grain: every source says where it came from


def grain_project(tmp_path, extra=None, **session):
    files = {**PROJECT, **(extra or {})}
    return server_for(tmp_path, files, **session)


def test_a_declared_unique_key_on_a_source_removes_the_unknown_and_names_its_origin(tmp_path):
    files = {"events.js": 'declare({schema: "wh", name: "order_events", uniqueKey: ["order_id", "kind"]});\n'}
    server, _ = grain_project(tmp_path, files)
    body, _, _ = ask(server, "describe_table", table="p.wh.order_events")
    assert body["grain"] == ["order_id", "kind"] and body["grain_status"] == "declared" and "declared uniqueKey in the declaration of order_events" in body["grain_origin"]
    assert not any(u["what"].startswith("grain") for u in body.get("unknown", []))
    body, _, _ = ask(server, "check_query", sql=DRAFT)
    [found] = fan_out(body)
    assert "one row per (kind, order_id)" in found["what"] or "one row per (order_id, kind)" in found["what"]
    assert "stated by declared uniqueKey" in body["fan_out_judged_assuming"][0]
    body, _, _ = ask(server, "check_query", sql=DRAFT.replace("e.order_id = o.order_id", "e.order_id = o.order_id AND e.kind = o.order_week"))
    assert not fan_out(body) and not any(u["what"].startswith("whether the JOIN to e") for u in body["unknown"])


def test_an_assertion_file_that_names_the_source_states_its_grain(tmp_path):
    files = {"events.sqlx": 'config { type: "declaration", schema: "wh", name: "order_events" }\n',
             "events_unique.sqlx": 'config { type: "assertion" }\nSELECT order_id FROM ${ref("wh", "order_events")} GROUP BY order_id HAVING COUNT(*) > 1\n'}
    server, _ = grain_project(tmp_path, files)
    body, _, _ = ask(server, "describe_table", table="p.wh.order_events")
    assert body["grain"] == ["order_id"] and "duplicate-key assertion" in body["grain_origin"]
    body, _, _ = ask(server, "check_query", sql=DRAFT)
    assert not fan_out(body) and not any(u["what"].startswith("whether the JOIN to e") for u in body["unknown"])


def test_an_assertion_with_a_filter_is_not_read_as_a_grain(tmp_path):
    files = {"events.sqlx": 'config { type: "declaration", schema: "wh", name: "order_events" }\n',
             "events_unique.sqlx": 'config { type: "assertion" }\nSELECT order_id FROM ${ref("wh", "order_events")} WHERE kind = \'x\' GROUP BY order_id HAVING COUNT(*) > 1\n'}
    server, _ = grain_project(tmp_path, files)
    body, _, _ = ask(server, "describe_table", table="p.wh.order_events")
    assert body.get("grain") is None and "grain_origin" not in body


def test_primary_keys_in_docs_and_malloy_sources_state_the_grain_with_their_file(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "warehouse.md").write_text("# Warehouse views\n\n## p.wh.order_events\n\nPrimary key: `order_id`, `kind`\n")
    malloy = tmp_path / "models"
    malloy.mkdir()
    (malloy / "shop.malloy").write_text("source: customers is duckdb.table('wh.customers_view') extend {\n  primary_key: id\n}\n")
    files = {"events.js": 'declare({schema: "wh", name: "order_events"});\n'}
    server, _ = grain_project(tmp_path, files, docs=[docs], malloy=[malloy])
    body, _, _ = ask(server, "describe_table", table="p.wh.order_events")
    assert body["grain"] == ["order_id", "kind"] and body["grain_origin"] == "primary key in docs file warehouse.md (section p.wh.order_events)"
    body, _, _ = ask(server, "describe_table", table="p.wh.customers_view")
    assert body["grain"] == ["id"] and body["grain_origin"] == "Malloy primary_key in shop.malloy (source customers)"
    # a warehouse table the project does not declare at all is matched by its trailing parts
    (malloy / "other.malloy").write_text("source: ev is table('conn:wh.order_events_raw') { primary_key: event_id }\n")
    server, _ = grain_project(tmp_path / "again", files, docs=[docs], malloy=[malloy])
    body, _, _ = ask(server, "check_query", sql="SELECT o.order_id FROM p.ds.orders o JOIN wh.order_events_raw r ON r.event_id = o.order_id")
    assert not fan_out(body) and "stated by Malloy primary_key in other.malloy" in body["fan_out_judged_assuming"][0]


def test_assume_grain_wins_over_every_other_source_and_says_so(tmp_path):
    files = {"events.js": 'declare({schema: "wh", name: "order_events", uniqueKey: ["order_id", "kind"]});\n'}
    server, _ = grain_project(tmp_path, files, assume_grain={"p.wh.order_events": ("kind",)})
    body, _, _ = ask(server, "describe_table", table="p.wh.order_events")
    assert body["grain"] == ["kind"] and body["grain_origin"] == "--assume-grain"


def test_a_select_star_over_a_source_with_a_stated_key_gets_that_grain_and_says_where_it_came_from(tmp_path):
    files = {"events.js": 'declare({schema: "wh", name: "order_events", uniqueKey: ["order_id"]});\n',
             "events_copy.sqlx": 'config { type: "view" }\nSELECT * FROM ${ref("wh", "order_events")}\n'}
    server, _ = grain_project(tmp_path, files)
    body, _, _ = ask(server, "describe_table", table="events_copy")
    assert body["grain"] == ["order_id"] and body["grain_origin"] == "derived from the query over order_events, whose grain is stated by declared uniqueKey in the declaration of order_events"


def test_docs_fill_the_grain_of_a_model_whose_query_shows_none_but_never_override_one_it_shows(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "views.md").write_text("## wh_view\nPrimary key: order_id\n\n## totals\nPrimary key: kind\n")
    files = {"wh_view.sqlx": 'config { type: "view" }\nSELECT order_id, kind FROM p.wh.undeclared\n',
             "totals.sqlx": 'config { type: "table" }\nSELECT order_id, COUNT(*) AS n FROM ${ref("orders")} GROUP BY order_id\n'}
    server, _ = grain_project(tmp_path, files, docs=[docs])
    body, _, _ = ask(server, "describe_table", table="wh_view")
    assert body["grain"] == ["order_id"] and body["grain_origin"] == "primary key in docs file views.md (section wh_view)"
    body, _, _ = ask(server, "describe_table", table="totals")
    assert body["grain"] == ["order_id"] and body["grain_origin"] == "derived from the query"


def test_a_model_s_own_unique_key_is_reported_as_declared_on_the_model(tmp_path):
    files = {"events_dim.sqlx": 'config { type: "table", assertions: { uniqueKey: ["order_id"] } }\nSELECT order_id, kind FROM ${ref("wh", "order_events")}\n',
             "events.js": 'declare({schema: "wh", name: "order_events"});\n'}
    server, _ = grain_project(tmp_path, files)
    body, _, _ = ask(server, "describe_table", table="events_dim")
    assert body["grain"] == ["order_id"] and body["grain_origin"] == "declared uniqueKey on model events_dim"


def test_a_model_with_a_query_grain_says_it_is_derived(tmp_path):
    server, _ = grain_project(tmp_path, {"totals.sqlx": 'config { type: "table" }\nSELECT order_id, COUNT(*) AS n FROM ${ref("orders")} GROUP BY order_id\n'})
    body, _, _ = ask(server, "describe_table", table="totals")
    assert body["grain"] == ["order_id"] and body["grain_origin"] == "derived from the query"


# ------------------------------------------------------------------- the readers


def test_the_malloy_keys_come_only_from_sources_built_directly_on_a_table():
    text = """
    // source: ignored is table('x.y') extend { primary_key: nope }
    source: orders is duckdb.table('shop.orders') extend {
      primary_key: order_id
      measure: n is count()
      view: v is { group_by: a }
    }
    source: lines is table('conn:shop.lines') { primary_key: line_id }
    source: derived is orders extend { primary_key: other }
    source: none is table('shop.n') extend { dimension: d is 1 }
    """
    assert [(k.source, k.table, k.column) for k in malloy_keys.keys_in(text)] == [("orders", "shop.orders", "order_id"), ("lines", "shop.lines", "line_id")]


def test_the_docs_reader_takes_a_key_line_or_a_marked_column_inside_a_section_that_names_a_table():
    text = """# orders_view
Primary key: `order_id`, line_no

## customers
| Column | Type | Primary key |
| --- | --- | --- |
| id | INT64 | yes |
| email | STRING | |

## notes
The primary key is something we discuss at length: not a list of columns.
- **Unique key**: some prose that is not a name list.
"""
    found = docs_keys.keys_in(text, "stem", "d.md")
    assert [(k.table_text, k.columns) for k in found] == [("orders_view", ("order_id", "line_no")), ("customers", ("id",))]


def test_a_key_observation_lookup_never_overreaches():
    def seen(columns, dups, rows=100, table="p.wh.t", day="2026-01-01T00:00:00Z"):
        return joins.KeyObservation(table, tuple(columns), rows, 0, dups, day, "join_profile", "n")

    assert joins.lookup([seen(["a"], 0)], ["p.wh.t"], ["a", "b"]).kind == "unique"  # a unique subset makes the superset unique
    assert joins.lookup([seen(["a"], 3)], ["p.wh.t"], ["a", "b"]).kind == "none"  # repeats on a subset decide nothing
    assert joins.lookup([seen(["a", "b"], 0)], ["p.wh.t"], ["a"]).kind == "wider"
    assert joins.lookup([seen(["a"], 3)], ["wh.t"], ["a"]).kind == "repeats"
    assert joins.lookup([seen(["a"], 3)], ["p.wh.other"], ["a"]).kind == "none"
    assert joins.lookup([seen(["a"], 3, day="2026-02-01T00:00:00Z"), seen(["a"], 0)], ["p.wh.t"], ["a"]).kind == "repeats"  # newest first
