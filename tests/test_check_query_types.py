"""``check_query`` types: column types from a schema snapshot, carried through the models, compared in the draft."""

import json

import pytest

from kumosql import mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.project_types import snapshot_filter
from kumosql.schema_snapshot import SchemaSnapshotError, check_schema_snapshot, load_schema_snapshot, parse_schema_snapshot
from test_mcp_server import ask, findings_of, no_network, write_project  # noqa: F401

#: a tiny Dataform project: raw_dates is declared without types, calendar and orders read it
PROJECT = {
    "raw_dates.sqlx": 'config { type: "declaration", schema: "src", name: "raw_dates" }\n',
    "calendar.sqlx": 'config { type: "table" }\nSELECT week_label AS order_week, week_start FROM ${ref("src", "raw_dates")}\n',
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, 7 AS customer_id, "open" AS status, 2.5 AS amount, CAST(1 AS FLOAT64) AS weight, DATETIME "2026-01-01 00:00:00" AS placed_local\n',
    "customers.sqlx": 'config { type: "table" }\nSELECT "7" AS customer_id, "Ann" AS name\n',
}
SNAPSHOT_CSV = """table_catalog,table_schema,table_name,column_name,data_type,is_nullable
p,ds,calendar,order_week,STRING,YES
p,ds,calendar,week_start,DATE,YES
p,src,raw_dates,week_label,STRING,YES
p,src,raw_dates,week_start,DATE,YES
"""
BETWEEN = "SELECT 1 FROM ${ref('calendar')} c WHERE c.order_week BETWEEN 202601 AND 202610"


def server(tmp_path, snapshot=None, name="columns.csv"):
    folder = write_project(tmp_path / "project", PROJECT)
    path = None
    if snapshot is not None:
        path = tmp_path / name
        path.write_text(snapshot)
    return mcp_server.AgentServer(Session(load_sqlx_project(folder), schema_snapshot=path))


def type_findings(body):
    return findings_of(body, "types")


def test_a_string_column_between_integer_bounds_is_a_types_finding_with_a_fix_when_a_snapshot_gives_the_type(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path, SNAPSHOT_CSV), "check_query", sql=BETWEEN)
    assert not failed and body["verdict"] == "problems_found"
    [found] = type_findings(body)
    assert "c.order_week (STRING)" in found["what"] and "202601 (INT64 literal)" in found["what"] and "BETWEEN" in found["what"]
    assert "'202601'" in found["fix"] and "CAST(c.order_week AS INT64)" in found["fix"]
    assert "types" in body["checked"] and body["counts"]["not_checked"] == 0


def test_without_a_snapshot_the_same_query_says_the_column_type_is_unknown_and_types_were_not_checked(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path), "check_query", sql=BETWEEN)
    assert not failed and not type_findings(body)
    assert body["counts"]["not_checked"] == 1
    [gap] = body["unknown"]
    assert gap["what"] == "types of calendar.order_week unknown" and "--schema-snapshot" in gap["reason"]
    assert "types" not in body["checked"] and "NOT be checked" in body["answer"]


def test_a_snapshot_that_cannot_be_read_is_named_in_the_gap_instead_of_passing(tmp_path, no_network):
    body, _, _ = ask(server(tmp_path, "not,a,schema\n1,2,3\n"), "check_query", sql=BETWEEN)
    [gap] = body["unknown"]
    assert "could not be read" in gap["reason"] and "types" not in body["checked"]


def test_model_types_are_carried_from_their_queries_so_a_join_on_text_and_integer_keys_is_found(tmp_path, no_network):
    served = server(tmp_path)
    types = served.session.column_types()
    assert str(types.type("p.ds.orders", "customer_id")) == "INT64" and str(types.type("p.ds.customers", "customer_id")) == "STRING"
    assert types.type("p.ds.calendar", "order_week") is None  # read from an undeclared source: unknown for that column only
    body, _, _ = ask(served, "check_query", sql="SELECT o.order_id FROM ${ref('orders')} o JOIN ${ref('customers')} c ON o.customer_id = c.customer_id")
    [found] = type_findings(body)
    assert found["what"].startswith("join condition") and "o.customer_id (INT64)" in found["what"] and "c.customer_id (STRING)" in found["what"]
    assert "CAST(o.customer_id AS STRING)" in found["fix"]


@pytest.mark.parametrize(
    "sql, needle",
    [
        ("SELECT 1 FROM ${ref('orders')} o WHERE o.status = 5", "o.status (STRING) with 5 (INT64 literal)"),
        ("SELECT 1 FROM ${ref('orders')} o WHERE o.order_id IN ('a', 'b')", "IN compares o.order_id (INT64)"),
        ("SELECT 1 FROM ${ref('orders')} o WHERE o.placed_local > TIMESTAMP '2026-01-01'", "o.placed_local (DATETIME)"),
        ("SELECT MOD(o.weight, o.order_id) FROM ${ref('orders')} o", "MOD has no signature for (FLOAT64, INT64)"),
        ("SELECT o.status + 1 FROM ${ref('orders')} o", "+ has no signature for (STRING, INT64)"),
        ("SELECT SUM(o.status) FROM ${ref('orders')} o", "SUM has no signature for STRING"),
        ("SELECT o.order_id FROM ${ref('orders')} o UNION ALL SELECT c.name FROM ${ref('customers')} c", "no common type"),
    ],
)
def test_check_query_names_both_sides_and_a_fix_for_each_kind_of_type_mismatch(tmp_path, no_network, sql, needle):
    body, _, _ = ask(server(tmp_path), "check_query", sql=sql)
    [found] = type_findings(body)
    assert needle in found["what"] and found["fix"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM ${ref('orders')} o WHERE o.status = 'open' AND o.order_id BETWEEN 1 AND 5 AND o.amount > 2",
        "SELECT 1 FROM ${ref('orders')} o WHERE o.placed_local >= '2026-01-01' AND o.order_id IN (1, 2) AND o.weight < o.order_id",
        "SELECT MOD(o.order_id, 2), SAFE_DIVIDE(o.weight, o.order_id), o.weight + o.order_id FROM ${ref('orders')} o",
        "SELECT o.order_id FROM ${ref('orders')} o JOIN ${ref('orders')} p ON o.order_id = p.order_id",
        "SELECT o.status FROM ${ref('orders')} o UNION ALL SELECT c.name FROM ${ref('customers')} c",
    ],
)
def test_valid_comparisons_and_calls_produce_no_types_finding(tmp_path, no_network, sql):
    body, _, _ = ask(server(tmp_path), "check_query", sql=sql)
    assert not type_findings(body) and "types" in body["checked"]


def test_a_using_join_between_text_and_integer_columns_is_found(tmp_path, no_network):
    body, _, _ = ask(server(tmp_path), "check_query", sql="SELECT 1 FROM ${ref('orders')} o JOIN ${ref('customers')} c USING (customer_id)")
    [found] = type_findings(body)
    assert "USING (customer_id)" in found["what"] and "INT64" in found["what"] and "STRING" in found["what"] and "ON CAST" in found["fix"]


def test_snapshot_types_win_over_derived_ones_and_rows_of_tables_the_project_does_not_use_are_dropped_unread(tmp_path, no_network):
    csv = SNAPSHOT_CSV + "p,ds,orders,customer_id,STRING,NO\np,other,elsewhere,k,INT64,YES\n"
    served = server(tmp_path, csv)
    types = served.session.column_types()
    assert str(served.session.column_type("p.ds.orders", "customer_id")) == "STRING"
    assert served.session.column_nullable("p.ds.orders", "customer_id") is False and served.session.column_nullable("p.ds.calendar", "order_week") is True
    assert served.session.schema_snapshot().ignored == 1 and "p.other.elsewhere" not in served.session.schema_snapshot().tables()
    assert types.unmatched == []
    body, _, _ = ask(served, "check_query", sql="SELECT 1 FROM p.other.elsewhere e JOIN ${ref('orders')} o ON o.customer_id = e.k")
    assert not type_findings(body) and any("not in the project" in g["what"] for g in body["unknown"])


def test_nullability_comes_from_the_snapshot_or_from_what_a_models_query_guarantees(tmp_path):
    served = server(tmp_path)
    assert served.session.column_nullable("p.ds.orders", "order_id") is False  # a literal is never NULL
    assert served.session.column_nullable("p.ds.calendar", "order_week") is None  # read from an undeclared source: not known


def test_many_unknown_columns_are_one_line_with_a_count(tmp_path, no_network):
    sql = "SELECT 1 FROM ${ref('calendar')} c WHERE c.order_week = 'a' AND c.week_start = 'b' AND c.week_start > c.order_week AND c.order_week = c.week_start"
    body, _, _ = ask(server(tmp_path), "check_query", sql=sql)
    assert body["counts"]["not_checked"] == 2 and {g["what"] for g in body["unknown"]} == {"types of calendar.order_week unknown", "types of calendar.week_start unknown"}


# ---------------------------------------------------------------- the snapshot loader


def test_the_loader_reads_both_header_spellings_csv_and_json_lines_in_column_order():
    short = "catalog,schema,table,column,data_type,is_nullable\np,ds,t,b,STRING,NO\np,ds,t,a,INT64,YES\n"
    lines = "\n".join(json.dumps(r) for r in [
        {"table_catalog": "p", "table_schema": "ds", "table_name": "t", "column_name": "b", "data_type": "STRING", "is_nullable": "NO", "ordinal_position": 1},
        {"table_catalog": "p", "table_schema": "ds", "table_name": "t", "column_name": "a", "data_type": "INT64", "is_nullable": "YES", "ordinal_position": 2},
    ])
    for text in (short, lines, "[" + lines.replace("\n", ",") + "]"):
        snapshot = parse_schema_snapshot(text)
        assert snapshot.types_of("ds.t") == {"b": "STRING", "a": "INT64"} and snapshot.types_of("T") == {"b": "STRING", "a": "INT64"}
        assert [c.is_nullable for c in snapshot.columns_of("p.ds.t")] == [False, True]
        assert list(snapshot.tables()) == ["p.ds.t"] and len(snapshot) == 1


def test_the_loader_skips_unusable_rows_and_refuses_a_file_without_the_fields(tmp_path):
    snapshot = parse_schema_snapshot("table_name,column_name,data_type\nt,a,INT64\nt,a,STRING\nt,,INT64\n")
    assert snapshot.types_of("t") == {"a": "INT64"} and snapshot.skipped == 2
    for bad in ("", "x,y\n1,2\n", "table_name,column_name,data_type\n"):
        with pytest.raises(SchemaSnapshotError):
            parse_schema_snapshot(bad)
    with pytest.raises(SchemaSnapshotError, match="cannot read"):
        load_schema_snapshot(tmp_path / "missing.csv")
    (tmp_path / "c.csv").write_text("table_name,column_name,data_type\nt,a,INT64\n")
    assert load_schema_snapshot(tmp_path / "c.csv").types_of("t") == {"a": "INT64"}


def test_a_bad_snapshot_stops_the_server_at_start(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n")
    with pytest.raises(SystemExit):
        mcp_server.main([str(tmp_path), "--schema-snapshot", str(bad)])
    assert "--schema-snapshot" in capsys.readouterr().err


def test_a_filter_drops_the_rows_of_other_tables_while_reading_and_counts_them(tmp_path):
    rows = "table_catalog,table_schema,table_name,column_name,data_type,is_nullable\n" + "".join(
        f"p,ds,t{i},c{j},INT64,YES\n" for i in range(50) for j in range(4))
    path = tmp_path / "wide.csv"
    path.write_text(rows)
    snapshot = load_schema_snapshot(path, keep=snapshot_filter(["p.ds.t3", "ds.t7"]))
    assert sorted(snapshot.tables()) == ["p.ds.t3", "p.ds.t7"] and len(snapshot.columns) == 8 and snapshot.ignored == 192
    assert len(load_schema_snapshot(path).columns) == 200
    check_schema_snapshot(path)
    with pytest.raises(SchemaSnapshotError):
        check_schema_snapshot(tmp_path / "missing.csv")


def test_a_project_of_400_models_and_1000_declared_sources_is_typed_once_from_a_large_snapshot(tmp_path):
    """Sizing, not a timing test: every model is typed once, rows of tables the project does not use are never kept."""

    files = {f"src_{i}.sqlx": f'config {{ type: "declaration", schema: "src", name: "s{i}" }}\n' for i in range(1000)}
    for i in range(400):
        upstream = f'${{ref("src", "s{i}")}}' if i % 2 == 0 else f'${{ref("m{i - 1}")}}'
        files[f"m{i}.sqlx"] = f'config {{ type: "table" }}\nSELECT id, total FROM {upstream}\n'
    folder = write_project(tmp_path / "big", files)
    lines = ["table_catalog,table_schema,table_name,column_name,data_type,is_nullable"]
    lines += [f"p,src,s{i},{c},{kind},YES" for i in range(1000) for c, kind in (("id", "STRING"), ("total", "INT64"))]
    lines += [f"p,archive,old{i},c{j},STRING,YES" for i in range(2000) for j in range(10)]  # 20,000 rows of tables nobody uses
    path = tmp_path / "columns.csv"
    path.write_text("\n".join(lines) + "\n")
    session = Session(load_sqlx_project(folder), schema_snapshot=path)
    types = session.column_types()
    assert types.models_typed == 400 and types.models_untyped == 0
    assert session.schema_snapshot().ignored == 20000 and len(session.schema_snapshot()) == 1000
    assert str(session.column_type("p.ds.m399", "id")) == "STRING" and str(session.column_type("p.ds.m398", "total")) == "INT64"
    assert session.column_types() is types  # computed once and cached in the session
