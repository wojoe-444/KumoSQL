"""``deployed_status`` and the deployed-table notes of ``check_query``: repository against the schema snapshot, offline.

Everything here is a synthetic project (orders, a daily rollup, a never-built summary) and a synthetic snapshot export."""

import os
import time

from kumosql import mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.schema_snapshot import parse_schema_snapshot
from test_mcp_server import ask, findings_of, no_network, write_project  # noqa: F401

PROJECT = {
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, DATE "2026-01-05" AS placed_on, "eu" AS region, 5 AS amount\n',
    "orders_daily.sqlx": ('config { type: "table", bigquery: { partitionBy: "order_day", clusterBy: ["region"] } }\n'
                          'SELECT placed_on AS order_day, DATE_TRUNC(placed_on, WEEK) AS order_week, region, SUM(amount) AS total_amount\n'
                          'FROM ${ref("orders")} GROUP BY 1, 2, 3\n'),
    "customers_summary.sqlx": 'config { type: "table" }\nSELECT region, COUNT(*) AS customers FROM ${ref("orders")} GROUP BY region\n',
}
HEADER = "table_catalog,table_schema,table_name,column_name,data_type,is_nullable,is_partitioning_column,clustering_ordinal_position\n"
#: orders_daily was built before the repository renamed order_wk to order_week, and total_amount was a STRING then
SNAPSHOT = HEADER + """p,ds,orders,order_id,INT64,YES,NO,
p,ds,orders,placed_on,DATE,YES,NO,
p,ds,orders,region,STRING,YES,NO,
p,ds,orders,amount,INT64,YES,NO,
p,ds,orders_daily,order_day,DATE,YES,YES,
p,ds,orders_daily,order_wk,DATE,YES,NO,
p,ds,orders_daily,region,STRING,YES,NO,1
p,ds,orders_daily,total_amount,STRING,YES,NO,
"""


def server(tmp_path, snapshot=SNAPSHOT, *, age_days=None, project=None):
    folder = write_project(tmp_path / "project", project or PROJECT)
    path = None
    if snapshot is not None:
        path = tmp_path / "columns.csv"
        path.write_text(snapshot)
        if age_days is not None:
            then = time.time() - age_days * 86400 - 600
            os.utime(path, (then, then))
    return mcp_server.AgentServer(Session(load_sqlx_project(folder), root=folder, schema_snapshot=path))


def unknown_of(body):
    return {item["what"]: item["reason"] for item in body.get("unknown", [])}


def test_check_query_on_a_model_never_deployed_does_not_say_no_problems_found_silently_and_names_deployed_status(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path), "check_query", sql='SELECT region FROM ${ref("customers_summary")}')
    assert not failed
    gap = [item for item in body["unknown"] if "customers_summary is not in the schema snapshot" in item["what"]]
    assert gap and "may not be built" in gap[0]["reason"] and "deployed_status table=customers_summary" in gap[0]["reason"]
    assert any("deployed_status table=customers_summary" in step for step in body["next"])
    assert body["counts"]["not_checked"] >= 1 and body["unknown"][0] == gap[0]  # first, so a cut cannot hide it


def test_check_query_without_a_snapshot_lists_the_deployed_check_as_not_checked(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path, snapshot=None), "check_query", sql='SELECT region FROM ${ref("customers_summary")}')
    assert not failed
    assert "no schema snapshot" in unknown_of(body)["whether the tables it reads are built and current"]


def test_check_query_says_a_column_is_in_the_repository_but_not_in_the_deployed_table(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path), "check_query", sql='SELECT order_day, order_week FROM ${ref("orders_daily")}')
    assert not failed and body["verdict"] == "problems_found"
    [found] = findings_of(body, "deployed")
    assert "order_week" in found["what"] and "in the repository but not in the deployed table (snapshot" in found["what"]
    assert "deployed_status table=orders_daily" in found["fix"]
    # a column the deployed table has raises nothing
    assert not findings_of(ask(server(tmp_path), "check_query", sql='SELECT order_day, region FROM ${ref("orders_daily")}')[0], "deployed")


def test_check_query_says_how_old_the_snapshot_file_is(tmp_path, no_network):
    body, _, _ = ask(server(tmp_path, age_days=3), "check_query", sql='SELECT region FROM ${ref("orders")}')
    assert "snapshot file is 3 days old" in unknown_of(body)
    fresh, _, _ = ask(server(tmp_path / "fresh"), "check_query", sql='SELECT region FROM ${ref("orders")}')
    assert not [what for what in unknown_of(fresh) if "old" in what]


def test_deployed_status_reports_the_renamed_column_old_name_only_in_warehouse_new_name_only_in_repository_and_a_possible_rename(tmp_path, no_network):
    body, failed, used = ask(server(tmp_path), "deployed_status", table="orders_daily")
    assert not failed and used <= 2000 and body["deployed"] == "in_snapshot"
    assert body["only_in_warehouse"] == ["order_wk DATE"] and body["only_in_repository"] == ["order_week DATE"]
    [rename] = body["possible_renames"]
    assert "order_wk (warehouse) may now be order_week (repository)" in rename and "not certain" in rename
    assert body["counts"]["only_in_repository"] == 1 and body["counts"]["only_in_warehouse"] == 1 and body["counts"]["possible_renames"] == 1
    assert any("builds nothing" in step for step in body["next"])


def test_deployed_status_reports_a_type_difference(tmp_path, no_network):
    body, _, _ = ask(server(tmp_path), "deployed_status", table="orders_daily")
    assert body["type_differences"] == [{"column": "total_amount", "repository": "INT64", "snapshot": "STRING"}]
    assert "1 type difference" in body["answer"]


def test_deployed_status_puts_partitioning_and_clustering_under_unknown_when_the_snapshot_lacks_them(tmp_path, no_network):
    plain = "table_catalog,table_schema,table_name,column_name,data_type\n" + "".join(
        f"p,ds,orders_daily,{c},{t}\n" for c, t in (("order_day", "DATE"), ("order_week", "DATE"), ("region", "STRING"), ("total_amount", "INT64")))
    body, failed, _ = ask(server(tmp_path, snapshot=plain), "deployed_status", table="orders_daily")
    assert not failed
    unknown = unknown_of(body)
    assert "is_partitioning_column" in unknown["partitioning of the deployed table"] and "clustering_ordinal_position" in unknown["clustering of the deployed table"]
    assert "partitioning_and_clustering" not in body and "match" in body["answer"] and "partitioning" not in body["answer"].split("match")[0]


def test_deployed_status_compares_partitioning_and_clustering_with_the_snapshot_and_warns_about_the_rebuild(tmp_path, no_network):
    body, _, _ = ask(server(tmp_path), "deployed_status", table="orders_daily")
    assert body["partitioning_and_clustering"] == ["partitioning: repository order_day, snapshot order_day: same column", "clustering: repository region, snapshot region: same"]
    changed = SNAPSHOT.replace("orders_daily,order_day,DATE,YES,YES,", "orders_daily,order_day,DATE,YES,NO,").replace("orders_daily,order_wk,DATE,YES,NO,", "orders_daily,order_wk,DATE,YES,YES,")
    body, _, _ = ask(server(tmp_path / "other", snapshot=changed), "deployed_status", table="orders_daily")
    assert "DIFFERENT" in body["partitioning_and_clustering"][0] and body["counts"]["layout_differences"] == 1
    assert any("different partitioning spec" in step for step in body["next"])


def test_deployed_status_says_a_model_missing_from_the_snapshot_may_not_be_built_and_builds_nothing(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path), "deployed_status", table="customers_summary")
    assert not failed and body["deployed"] == "not_in_snapshot"
    assert "may not be built" in body["answer"] and "only_in_repository" not in body
    assert any("builds nothing" in step for step in body["next"])


def test_deployed_status_without_a_snapshot_is_unknown_with_the_reason_and_a_next_step(tmp_path, no_network):
    body, failed, _ = ask(server(tmp_path, snapshot=None), "deployed_status", table="orders")
    assert not failed and body["deployed"] == "unknown" and "--schema-snapshot" in unknown_of(body)["whether the table is built and matches"]
    assert "--schema-snapshot" in body["next"][0]


def test_deployed_status_matches_when_nothing_differs_and_says_the_snapshot_is_old(tmp_path, no_network):
    body, _, _ = ask(server(tmp_path, age_days=4), "deployed_status", table="orders")
    assert body["answer"].startswith("orders is built and its columns") and body["snapshot"].startswith("snapshot file is 4 days old")
    assert "may have changed" in body["snapshot"] and "refresh" in body["next"][0]


def test_a_snapshot_with_an_export_timestamp_column_is_aged_by_it_and_a_table_of_an_uncovered_dataset_is_said_not_to_be_covered(tmp_path, no_network):
    stamped = "table_catalog,table_schema,table_name,column_name,data_type,exported_at\np,ds,orders,order_id,INT64,2026-01-01T00:00:00Z\n"
    snapshot = parse_schema_snapshot(stamped)
    assert snapshot.exported_at == 1767225600.0 and snapshot.modified_at is None
    body, _, _ = ask(server(tmp_path, snapshot=stamped), "deployed_status", table="orders")
    assert body["snapshot"].startswith("snapshot export is ") and "days old" in body["snapshot"]
    other = "table_catalog,table_schema,table_name,column_name,data_type\np,elsewhere,thing,x,INT64\n"
    body, _, _ = ask(server(tmp_path / "o", snapshot=other), "deployed_status", table="customers_summary")
    assert "whether the snapshot covers this dataset" in unknown_of(body)


def test_possible_renames_are_named_only_when_one_candidate_fits_each_side():
    from kumosql.mcp_deployed import possible_renames

    assert possible_renames({"order_week": "DATE"}, {"order_wk": "DATE"}) == [("order_week", "order_wk")]
    assert possible_renames({"order_week": "DATE"}, {"order_wk": "STRING"}) == []  # a different type is no rename
    assert possible_renames({"order_week": "DATE", "order_wave": "DATE"}, {"order_wk": "DATE"}) == []  # two candidates: no guess
    assert possible_renames({"alpha": "DATE"}, {"zzz": "DATE"}) == []  # unrelated names


def test_the_snapshot_loader_reads_partitioning_clustering_and_whether_the_file_has_them():
    snapshot = parse_schema_snapshot(SNAPSHOT)
    rows = {c.column: c for c in snapshot.columns_of("p.ds.orders_daily")}
    assert snapshot.has_partitioning and snapshot.has_clustering
    assert rows["order_day"].is_partitioning is True and rows["region"].clustering_position == 1 and rows["region"].is_partitioning is False
    bare = parse_schema_snapshot("table_name,column_name,data_type\nt,a,INT64\n")
    assert not bare.has_partitioning and not bare.has_clustering and bare.columns[0].is_partitioning is None


def test_deployed_status_reads_nothing_but_the_project_and_the_snapshot(tmp_path, no_network):
    served = server(tmp_path)
    mine = [tmp_path / "project", tmp_path / "columns.csv"]
    before = sorted((str(p), p.stat().st_mtime_ns) for root in mine for p in ([root] if root.is_file() else root.rglob("*")))
    for table in ("orders", "orders_daily", "customers_summary"):
        assert not ask(served, "deployed_status", table=table)[1]
    assert sorted((str(p), p.stat().st_mtime_ns) for root in mine for p in ([root] if root.is_file() else root.rglob("*"))) == before
