"""Partition pruning (``kumosql.partition_pruning``): filters on a partitioned table that BigQuery cannot prune with.

Every rule is checked on static SQL in both directions (a filter that prunes, one that does not). The BigQuery evidence
behind the rules (free dry runs on two public partitioned tables) is recorded in ``docs/partition-pruning.md``;
``test_the_docs_table_records_what_the_detector_says`` keeps that table and the code in step. Nothing here reaches the
network: the evidence script runs with a fake dry-run transport.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest
import sqlglot

from kumosql import bigquery_catalog, mcp_server, partition_pruning as pp
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from test_mcp_server import ask, block_network, write_project

ROOT = Path(__file__).resolve().parent.parent
mcp_server.load_tools()

DATE = pp.Partitioning("d", "DATE", False, "test")
TS = pp.Partitioning("ts", "TIMESTAMP", False, "test")
TS_REQUIRED = pp.Partitioning("ts", "TIMESTAMP", True, "test")
DATETIME = pp.Partitioning("dt", "DATETIME", False, "test")
RANGE = pp.Partitioning("customer_id", "INT64", False, "test")
INGESTION = pp.Partitioning("_PARTITIONTIME", "TIMESTAMP", False, "test")
UNTYPED = pp.Partitioning("d", None, None, "test")


def judge(sql, spec, variables=()):
    """``(issues, gaps)`` for a query in which ``ds.events`` has partitioning ``spec`` and nothing else is known."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    return pp.check_tree(tree, lambda table: spec if table.name.casefold() == "events" else None, variables)


def outcome(sql, spec=DATE, variables=()):
    """``silent``, ``unknown`` (a gap), or the rule of the first finding."""

    issues, gaps = judge(sql, spec, variables)
    if issues:
        return issues[0].rule
    return "unknown" if gaps else "silent"


def where(text, spec=DATE):
    return outcome(f"SELECT * FROM ds.events WHERE {text}", spec)


# ------------------------------------------------------------------------------------------------ the rules


PRUNES = [
    # the column alone, compared with constants
    (DATE, "d = DATE '2024-01-01'"),
    (DATE, "d IN (DATE '2024-01-01', DATE '2024-01-02')"),
    (DATE, "d BETWEEN DATE '2024-01-01' AND DATE '2024-01-31'"),
    (DATE, "d = '2024-01-01'"),
    (DATE, "d >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"),
    (DATE, "PARSE_DATE('%Y%m%d', '20240101') = d"),
    (DATE, "d != DATE '2024-01-01'"),
    (DATE, "d IS NULL"),
    (TS, "ts IS NOT NULL"),
    (TS, "NOT (ts < TIMESTAMP '2024-12-30')"),
    (TS, "ts IN UNNEST([TIMESTAMP '2024-01-01', TIMESTAMP '2024-01-02'])"),
    (TS, "ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"),
    (DATE, "d = @day"),
    (DATE, "d = (SELECT DATE '2024-01-01')"),
    # functions the page lists, or a dry run showed, as keeping pruning
    (TS, "DATE(ts) = '2024-03-30'"),
    (TS, "DATE(ts, 'America/Los_Angeles') = '2024-03-30'"),
    (TS, "CAST(ts AS DATE) IN ('2024-03-30', '2024-03-31')"),
    (TS, "EXTRACT(DATE FROM ts) = '2024-03-30'"),
    (TS, "EXTRACT(YEAR FROM ts) = 2024"),
    (DATE, "EXTRACT(YEAR FROM d) = 2024"),
    (TS, "TIMESTAMP_TRUNC(ts, MONTH) = TIMESTAMP '2024-03-01'"),
    (TS, "TIMESTAMP_ADD(ts, INTERVAL 1 DAY) = TIMESTAMP '2024-03-31'"),
    (TS, "TIMESTAMP_SUB(ts, INTERVAL 1 DAY) = TIMESTAMP '2024-03-29'"),
    (TS, "TIMESTAMP_DIFF(ts, TIMESTAMP '2024-03-30', DAY) = 0"),
    (TS, "FORMAT_TIMESTAMP('%F', ts) = '2024-03-30'"),
    (TS, "FORMAT_TIMESTAMP('%Y%m%d', ts) = '20240330'"),
    (DATE, "DATE_TRUNC(d, MONTH) = DATE '2024-01-01'"),
    (DATE, "DATE_ADD(d, INTERVAL 1 DAY) = DATE '2024-01-02'"),
    (DATE, "DATE_SUB(d, INTERVAL 1 DAY) = DATE '2023-12-31'"),
    (DATE, "DATE_DIFF(d, DATE '2024-01-01', DAY) = 0"),
    (DATE, "CAST(d AS TIMESTAMP) = TIMESTAMP '2024-01-01'"),
    (DATE, "TIMESTAMP(d) = TIMESTAMP '2024-01-01'"),
    (DATETIME, "DATETIME_DIFF(dt, DATETIME '2024-01-01 00:00:00', DAY) = 0"),
    (INGESTION, "_PARTITIONTIME > TIMESTAMP_SUB(TIMESTAMP '2016-04-15', INTERVAL 5 DAY)"),
    (INGESTION, "_PARTITIONDATE = '2016-04-15'"),
    (INGESTION, "DATE(_PARTITIONTIME) = '2016-04-15'"),
    (RANGE, "customer_id BETWEEN 30 AND 50"),
    # AND and OR
    (DATE, "d = DATE '2024-01-01' AND CAST(d AS STRING) LIKE '2024%'"),
    (DATE, "status = 'x' AND d = DATE '2024-01-01'"),
    (DATE, "d = DATE '2024-01-01' OR d = DATE '2024-01-02'"),
    (DATE, "(d = DATE '2024-01-01' AND a = 1) OR (d = DATE '2024-01-02' AND b = 2)"),
    (TS_REQUIRED, "ts = TIMESTAMP '2024-03-30' OR ts = TIMESTAMP '2024-03-31'"),
    (TS_REQUIRED, "ts >= TIMESTAMP '2024-03-30' AND (wiki = 'en' OR title = 'x')"),
]


@pytest.mark.parametrize("spec, text", PRUNES, ids=[t for _s, t in PRUNES])
def test_a_filter_bigquery_prunes_with_is_never_reported(spec, text):
    assert where(text, spec) == "silent"


DOES_NOT_PRUNE = [
    # a function or cast BigQuery does not prune through
    (DATE, "CAST(d AS STRING) IN ('2024-01-01', '2024-01-02')", "function"),
    (DATE, "CAST(d AS STRING) = '2024-01-01'", "function"),
    (DATE, "SAFE_CAST(d AS STRING) = '2024-01-01'", "function"),
    (TS, "SAFE_CAST(ts AS DATE) = '2024-03-30'", "function"),
    (TS, "STRING(ts) = '2024-03-30 12:00:00+00'", "function"),
    (DATE, "FORMAT_DATE('%Y%m%d', d) = '20240101'", "function"),
    (DATE, "FORMAT_DATE('%Y-%m', d) = '2024-01'", "function"),
    (TS, "FORMAT_TIMESTAMP('%Y-%m-%d %H', ts) = '2024-03-30 12'", "function"),
    (TS, "FORMAT_TIMESTAMP('%F', ts, 'Asia/Tokyo') = '2024-03-30'", "function"),
    (DATE, "EXTRACT(MONTH FROM d) = 11", "function"),
    (TS, "EXTRACT(HOUR FROM ts) = 12", "function"),
    (DATE, "UNIX_DATE(d) = 19723", "function"),
    (TS, "UNIX_SECONDS(ts) = 1711800000", "function"),
    (TS, "DATETIME(ts) = DATETIME '2024-03-30 12:00:00'", "function"),
    (TS, "DATETIME_TRUNC(DATETIME(ts), DAY) = DATETIME '2024-03-30 00:00:00'", "function"),
    (TS, "IFNULL(ts, TIMESTAMP '2024-03-30') = TIMESTAMP '2024-03-30'", "function"),
    (TS, "CAST(CAST(ts AS DATE) AS STRING) = '2024-03-30'", "function"),
    (DATE, "STARTS_WITH(CAST(d AS STRING), '2024')", "function"),
    (DATE, "DATE_DIFF(d, other_day, DAY) < 3", "function"),
    (RANGE, "ABS(customer_id) = 3", "function"),
    (RANGE, "MOD(customer_id, 10) = 3", "arithmetic"),
    (UNTYPED, "CAST(d AS STRING) = '2024-01-01'", "function"),
    # arithmetic on the column
    (TS, "ts + INTERVAL 1 DAY > CURRENT_TIMESTAMP()", "arithmetic"),
    (DATE, "d + INTERVAL 1 DAY > DATETIME '2024-01-01 00:00:00'", "arithmetic"),
    (RANGE, "customer_id + 1 BETWEEN 30 AND 50", "arithmetic"),
    (INGESTION, "_PARTITIONTIME + field1 = TIMESTAMP '2016-03-28'", "arithmetic"),
    # a value that is not constant when BigQuery plans the query
    (DATE, "d = (SELECT MAX(d) FROM ds.loads)", "dynamic_value"),
    (DATE, "d IN (SELECT d FROM ds.loads)", "dynamic_value"),
    (DATE, "d IN (SELECT DATE '2024-01-01')", "dynamic_value"),
    (DATE, "d >= week", "dynamic_value"),
    # OR, another column only
    (DATE, "d = DATE '2024-01-01' OR status = 'x'", "or_other_column"),
    (DATE, "(d = DATE '2024-01-01' OR status = 'x') AND kind = 'y'", "or_other_column"),
    (DATE, "CAST(d AS STRING) = '2024-01-01' OR d = DATE '2024-01-02'", "function"),
    (DATE, "status = 'x'", "other_column"),
    (DATE, "week = DATE '2024-01-01' AND status IN ('a', 'b')", "other_column"),
]


@pytest.mark.parametrize("spec, text, rule", DOES_NOT_PRUNE, ids=[t for _s, t, _r in DOES_NOT_PRUNE])
def test_a_filter_bigquery_cannot_prune_with_is_reported_with_its_rule(spec, text, rule):
    assert where(text, spec) == rule


def test_a_finding_names_the_table_the_column_the_filter_why_and_the_fix():
    [issue], gaps = judge("SELECT n FROM ds.events WHERE CAST(d AS STRING) IN ('2024-01-01')", DATE)
    found = issue.finding()
    assert gaps == [] and found["check"] == "partition_pruning" and found["level"] == "warning" and found["rule"] == "function"
    assert "ds.events is partitioned on d (test)" in found["what"] and "CAST(d AS STRING) IN ('2024-01-01')" in found["what"]
    assert "CAST(... AS STRING) is not one of the functions BigQuery prunes through" in found["what"] and "every partition" in found["what"]
    assert found["fix"] == "Write d IN (DATE '2024-01-01') instead (same rows; BigQuery prunes on it)."
    assert len(found["what"]) <= 300 and len(found["fix"]) <= 240


def test_findings_stay_short_for_long_filters():
    long = " OR ".join(f"(ts = TIMESTAMP '2024-03-{day:02d}' AND wiki = 'w{day}')" for day in range(1, 29))
    [issue], _ = judge(f"SELECT * FROM ds.events WHERE {long}", TS_REQUIRED)
    found = issue.finding()
    assert issue.rule == "or_mixed" and len(found["what"]) <= 300 and len(found["fix"]) <= 240


# ------------------------------------------------------------------------------------------------ joins and scopes


def test_joins_inner_on_prunes_outer_on_does_not_and_a_join_column_is_not_a_constant():
    assert outcome("SELECT * FROM ds.events e JOIN ds.users u ON u.id = e.user_id AND e.d = DATE '2024-01-01'") == "silent"
    assert outcome("SELECT * FROM ds.users u LEFT JOIN ds.events e ON e.user_id = u.id AND e.d = DATE '2024-01-01'") == "outer_join_on"
    assert outcome("SELECT * FROM ds.events e LEFT JOIN ds.users u ON u.id = e.user_id") == "silent"  # no filter: a full read may be meant
    assert outcome("SELECT * FROM ds.events e JOIN ds.calendar c ON e.d = c.d WHERE c.d = DATE '2024-01-01'") == "dynamic_value"
    # a FROM-less subquery is one row of constants: BigQuery folds it (measured)
    assert outcome("SELECT * FROM ds.events e JOIN (SELECT DATE '2024-01-01' AS d) c ON e.d = c.d") == "silent"


def test_an_unqualified_column_with_two_tables_in_scope_is_a_gap_never_a_finding():
    issues, gaps = judge("SELECT * FROM ds.events, ds.other WHERE CAST(d AS STRING) = '2024-01-01'", DATE)
    assert issues == [] and gaps and "whether events is pruned on d" in gaps[0][0].replace("ds.events", "events")
    # qualified, it is certain
    assert outcome("SELECT * FROM ds.events e, ds.other o WHERE CAST(e.d AS STRING) = '2024-01-01'") == "function"


def test_a_filter_outside_a_cte_or_subquery_is_pushed_into_it():
    assert outcome("WITH x AS (SELECT d AS day, n FROM ds.events) SELECT * FROM x WHERE day = DATE '2024-01-01'") == "silent"
    assert outcome("SELECT * FROM (SELECT d, COUNT(*) AS c FROM ds.events GROUP BY d) WHERE d = DATE '2024-01-01'") == "silent"
    assert outcome("SELECT d, COUNT(*) FROM ds.events GROUP BY d HAVING d = DATE '2024-01-01'") == "silent"
    # a filter on another table of the outer query does not reach the CTE
    sql = ("WITH x AS (SELECT * FROM ds.events WHERE CAST(d AS STRING) = '2024-01-01') "
           "SELECT * FROM x JOIN ds.users u ON u.id = x.user_id WHERE u.country = 'JP'")
    assert outcome(sql) == "function"
    # nor does one on an aggregate of the subquery, or a filter outside an IN subquery
    assert outcome("SELECT * FROM (SELECT d, COUNT(*) AS c FROM ds.events WHERE CAST(d AS STRING) = '2024-01-01' GROUP BY d) WHERE c > 5") == "function"
    assert outcome("SELECT * FROM ds.users WHERE id IN (SELECT user_id FROM ds.events WHERE CAST(d AS STRING) = '2024-01-01') AND status = 'x'") == "function"


def test_a_window_partitioned_by_another_column_stops_the_pushdown_when_its_value_is_read():
    used = "SELECT SUM(v) FROM (SELECT ts, SUM(n) OVER (PARTITION BY wiki) AS v FROM ds.events) WHERE ts = TIMESTAMP '2024-03-30'"
    by_column = "SELECT SUM(v) FROM (SELECT ts, SUM(n) OVER (PARTITION BY ts) AS v FROM ds.events) WHERE ts = TIMESTAMP '2024-03-30'"
    unused = "SELECT COUNT(*) FROM (SELECT ts, SUM(n) OVER (PARTITION BY wiki) AS v FROM ds.events) WHERE ts = TIMESTAMP '2024-03-30'"
    assert outcome(used, TS_REQUIRED) == "no_filter"
    assert outcome(by_column, TS_REQUIRED) == "silent"
    assert outcome(unused, TS_REQUIRED) == "silent"


def test_a_script_variable_is_a_constant():
    assert outcome("SELECT * FROM ds.events WHERE d = max_day", DATE, variables=("max_day",)) == "silent"
    assert outcome("SELECT * FROM ds.events WHERE d = max_day", DATE) == "dynamic_value"


def test_each_read_of_a_table_is_judged_on_its_own():
    sql = ("WITH a AS (SELECT * FROM ds.events WHERE d = DATE '2024-01-01'), b AS (SELECT * FROM ds.events WHERE status = 'x') "
           "SELECT * FROM a UNION ALL SELECT * FROM b")
    issues, _ = judge(sql, DATE)
    assert [i.rule for i in issues] == ["other_column"]


# ------------------------------------------------------------------------------------------------ unknown stays unknown


def test_unknown_partitioning_says_nothing():
    for sql in ("SELECT * FROM ds.events WHERE CAST(d AS STRING) = '2024-01-01'", "SELECT * FROM ds.events"):
        assert judge(sql, None) == ([], [])
    tree = sqlglot.parse_one("SELECT * FROM ds.other WHERE CAST(d AS STRING) = '2024-01-01'", read="bigquery")
    assert pp.check_tree(tree, lambda table: DATE if table.name == "events" else None) == ([], [])


def test_a_masked_dataform_expression_is_never_a_finding():
    """``${...}`` in a model is masked as ``__sqlx_token_000__``; it compiles to text KumoSQL does not see."""

    assert where("d >= DATE_SUB(CURRENT_DATE(), INTERVAL __sqlx_token_000__ DAY)") == "unknown"
    for text in ("__sqlx_token_001__ = DATE '2024-01-01'", "__sqlx_token_001__", "CAST(__sqlx_token_001__ AS STRING) = 'x'"):
        assert where(text) in ("silent", "unknown"), text


def test_a_shape_nothing_settles_is_a_gap():
    for spec, text in ((TS, "MY_UDF(ts) = 1"), (DATETIME, "DATETIME_TRUNC(dt, DAY) = DATETIME '2024-01-01 00:00:00'"),
                       (TS, "CAST(ts AS DATETIME) = DATETIME '2024-01-01 00:00:00'"), (DATE, "d = MY_UDF(1)")):
        issues, gaps = judge(f"SELECT * FROM ds.events WHERE {text}", spec)
        assert issues == [] and len(gaps) == 1 and "neither BigQuery's documentation nor a dry run" in gaps[0][1], text


def test_no_filter_is_silent_unless_the_table_requires_one():
    assert outcome("SELECT * FROM ds.events", TS) == "silent"
    assert outcome("SELECT COUNT(*) FROM ds.events e JOIN ds.users u ON u.id = e.user_id", TS) == "silent"


# ------------------------------------------------------------------------------------------------ required partition filter


def test_a_table_that_requires_a_partition_filter_is_an_error_without_one():
    [issue], _ = judge("SELECT * FROM ds.events", TS_REQUIRED)
    found = issue.finding()
    assert found["check"] == "partition_filter_required" and found["level"] == "error" and issue.rule == "no_filter"
    assert "requires a partition filter" in found["what"] and "BigQuery refuses the query" in found["what"]
    for text, rule in (("CAST(ts AS STRING) = 'x'", "function"), ("wiki = 'en'", "other_column"), ("ts = TIMESTAMP '2024-03-30' OR wiki = 'en'", "or_other_column")):
        [issue], _ = judge(f"SELECT * FROM ds.events WHERE {text}", TS_REQUIRED)
        assert (issue.rule, issue.level) == (rule, "error"), text


def test_an_or_whose_branches_also_read_other_columns_prunes_but_is_not_the_required_filter():
    sql = "SELECT * FROM ds.events WHERE (ts = TIMESTAMP '2024-03-30' AND wiki = 'en') OR (ts = TIMESTAMP '2024-03-31' AND wiki = 'de')"
    assert outcome(sql, TS) == "silent"
    [issue], _ = judge(sql, TS_REQUIRED)
    assert issue.rule == "or_mixed" and issue.level == "error" and "is not accepted as the partition filter" in issue.what()
    assert issue.rewrite.startswith("(ts = TIMESTAMP '2024-03-30' OR ts = TIMESTAMP '2024-03-31') AND (")
    assert outcome(f"SELECT * FROM ds.events WHERE {issue.rewrite}", TS_REQUIRED) == "silent"


def test_errors_come_before_warnings():
    sql = "SELECT * FROM ds.events a JOIN ds.events2 b ON a.id = b.id WHERE CAST(a.d AS STRING) = 'x'"
    tree = sqlglot.parse_one(sql, read="bigquery")
    specs = {"events": DATE, "events2": pp.Partitioning("d", "DATE", True, "test")}
    issues, _ = pp.check_tree(tree, lambda table: specs.get(table.name))
    assert [i.level for i in issues] == ["error", "warning"]


# ------------------------------------------------------------------------------------------------ rewrites


REWRITES = [
    (DATE, "CAST(d AS STRING) IN ('2024-01-01', '2024-01-02')", "d IN (DATE '2024-01-01', DATE '2024-01-02')"),
    (DATE, "CAST(d AS STRING) >= '2024-01-01'", "d >= DATE '2024-01-01'"),
    (DATE, "SAFE_CAST(d AS STRING) BETWEEN '2024-01-01' AND '2024-01-31'", "d BETWEEN DATE '2024-01-01' AND DATE '2024-01-31'"),
    (DATE, "'2024-01-01' = CAST(d AS STRING)", "d = DATE '2024-01-01'"),
    (DATE, "FORMAT_DATE('%Y%m%d', d) = '20240101'", "d = DATE '2024-01-01'"),
    (DATE, "FORMAT_DATE('%Y-%m-%d', d) IN ('2024-01-01')", "d IN (DATE '2024-01-01')"),
    (DATE, "FORMAT_DATE('%Y-%m', d) = '2024-01'", "DATE_TRUNC(d, MONTH) = DATE '2024-01-01'"),
    (DATE, "FORMAT_DATE('%Y%m', d) < '202402'", "DATE_TRUNC(d, MONTH) < DATE '2024-02-01'"),
    (TS, "FORMAT_TIMESTAMP('%Y-%m-%d %H', ts) = '2024-03-30 12'", "TIMESTAMP_TRUNC(ts, HOUR) = TIMESTAMP '2024-03-30 12:00:00'"),
    (TS, "ts + INTERVAL 1 DAY > CURRENT_TIMESTAMP()", "ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"),
    (TS, "CURRENT_TIMESTAMP() < ts + INTERVAL 1 DAY", "ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"),
    (TS, "ts - INTERVAL 2 HOUR <= TIMESTAMP '2024-01-01 00:00:00'", "ts <= TIMESTAMP_ADD(TIMESTAMP '2024-01-01 00:00:00', INTERVAL 2 HOUR)"),
    (DATE, "d + INTERVAL 1 DAY > DATETIME '2024-01-01 00:00:00'", "d > DATETIME_SUB(DATETIME '2024-01-01 00:00:00', INTERVAL 1 DAY)"),
    (RANGE, "customer_id + 1 BETWEEN 30 AND 50", "customer_id BETWEEN 29 AND 49"),
    (RANGE, "customer_id - 5 = 10", "customer_id = 15"),
]


@pytest.mark.parametrize("spec, text, rewrite", REWRITES, ids=[t for _s, t, _r in REWRITES])
def test_a_mechanical_rewrite_is_suggested_and_it_prunes(spec, text, rewrite):
    [issue], _ = judge(f"SELECT * FROM ds.events WHERE {text}", spec)
    assert issue.rewrite == rewrite and f"Write {rewrite} instead" in issue.finding()["fix"]
    assert where(rewrite, spec) == "silent"


NO_REWRITE = [
    (DATE, "CAST(d AS STRING) = 'yesterday'"),
    (DATE, "CAST(d AS STRING) = '2024-02-30'"),
    (DATE, "CAST(d AS STRING) LIKE '2024-01%'"),
    (UNTYPED, "CAST(d AS STRING) = '2024-01-01'"),
    (TS, "CAST(ts AS STRING) = '2024-03-30 12:00:00+00'"),
    (DATE, "EXTRACT(MONTH FROM d) = 11"),
    (TS, "ts * 2 > TIMESTAMP '2024-01-01'"),
    (TS, "TIMESTAMP '2024-01-01' - ts > INTERVAL 1 DAY"),
    (DATE, "d + INTERVAL 1 DAY > '2024-01-01'"),
    (DATE, "d = (SELECT MAX(d) FROM ds.loads)"),
]


@pytest.mark.parametrize("spec, text", NO_REWRITE, ids=[t for _s, t in NO_REWRITE])
def test_no_rewrite_is_suggested_when_it_is_not_mechanical(spec, text):
    [issue], _ = judge(f"SELECT * FROM ds.events WHERE {text}", spec)
    assert issue.rewrite is None and "Write" not in issue.finding()["fix"]


def test_a_join_on_the_column_suggests_filtering_the_column_with_the_other_sides_constant():
    sql = "SELECT * FROM ds.events e JOIN ds.calendar c ON e.d = c.d WHERE c.d BETWEEN DATE '2024-01-01' AND DATE '2024-01-07'"
    [issue], _ = judge(sql, DATE)
    assert issue.rule == "dynamic_value" and issue.rewrite == "e.d BETWEEN DATE '2024-01-01' AND DATE '2024-01-07'"
    assert issue.finding()["fix"].startswith("Also filter d itself: add e.d BETWEEN")
    assert outcome(f"{sql} AND {issue.rewrite}") == "silent"


# ------------------------------------------------------------------------------------------------ where the partitioning comes from


def test_partitioning_from_a_dataform_partition_by_expression():
    types = {"event_date": "DATE", "created_at": "TIMESTAMP"}.get
    assert pp.partitioning_from_expression("event_date", types) == pp.Partitioning("event_date", "DATE")
    assert pp.partitioning_from_expression("DATE(created_at)", types) == pp.Partitioning("created_at", "TIMESTAMP")
    assert pp.partitioning_from_expression("DATE(created_at)") == pp.Partitioning("created_at", None)
    assert pp.partitioning_from_expression("TIMESTAMP_TRUNC(ts, HOUR)") == pp.Partitioning("ts", "TIMESTAMP")
    assert pp.partitioning_from_expression("DATETIME_TRUNC(dt, MONTH)") == pp.Partitioning("dt", "DATETIME")
    assert pp.partitioning_from_expression("RANGE_BUCKET(customer_id, GENERATE_ARRAY(0, 100, 10))") == pp.Partitioning("customer_id", "INT64")
    assert pp.partitioning_from_expression("DATE(_PARTITIONTIME)").names == {"_partitiontime", "_partitiondate"}
    for unreadable in ("", "CONCAT(a, b)", "a + b", "not sql ((("):
        assert pp.partitioning_from_expression(unreadable) is None, unreadable


def test_partitioning_from_bigquery_table_metadata():
    schema = {"fields": [{"name": "day", "type": "DATE"}, {"name": "id", "type": "INTEGER"}]}
    assert bigquery_catalog.partitioning_of({"schema": schema}) is None
    assert bigquery_catalog.partitioning_of({"schema": schema, "timePartitioning": {"type": "DAY", "field": "day"}, "requirePartitionFilter": True}) == {
        "column": "day", "column_type": "DATE", "require_filter": True}
    assert bigquery_catalog.partitioning_of({"timePartitioning": {"type": "DAY"}}) == {"column": "_PARTITIONTIME", "column_type": "TIMESTAMP", "require_filter": None}
    assert bigquery_catalog.partitioning_of({"schema": schema, "rangePartitioning": {"field": "id", "range": {}}})["column_type"] == "INT64"
    # the deprecated place of the flag
    assert bigquery_catalog.partitioning_of({"timePartitioning": {"field": "day", "requirePartitionFilter": False}, "schema": schema})["require_filter"] is False


def test_get_table_keeps_the_partitioning_of_the_metadata(monkeypatch):
    payload = {"tableReference": {"tableId": "t"}, "schema": {"fields": [{"name": "day", "type": "DATE"}]}, "timePartitioning": {"type": "DAY", "field": "day"}}
    asked = {}

    def fake_get(path, params=None):
        asked["fields"] = (params or {}).get("fields", "")
        return payload

    monkeypatch.setattr(bigquery_catalog, "_get", fake_get)
    entry = bigquery_catalog.get_table("p", "d", "t")
    assert entry["partitioning"] == {"column": "day", "column_type": "DATE", "require_filter": None}
    assert {"timePartitioning", "rangePartitioning", "requirePartitionFilter"} <= set(asked["fields"].split(","))


PROJECT = {
    "events.sqlx": ('config { type: "table", bigquery: { partitionBy: "event_date", requirePartitionFilter: true } }\n'
                    "SELECT DATE '2024-01-01' AS event_date, 'x' AS status, 1 AS n\n"),
    "visits.sqlx": ('config { type: "incremental", bigquery: { partitionBy: "DATE(seen_at)" } }\n'
                    "SELECT TIMESTAMP '2024-01-01 00:00:00' AS seen_at, 1 AS user_id\n"),
    "plain.sqlx": 'config { type: "table" }\nSELECT DATE \'2024-01-01\' AS event_date, 1 AS n\n',
    "raw.sqlx": 'config { type: "declaration", schema: "raw", name: "logs" }',
}
SNAPSHOT = ("table_catalog,table_schema,table_name,column_name,data_type,is_partitioning_column,require_partition_filter\n"
            "p,raw,logs,log_day,DATE,YES,NO\n"
            "p,raw,logs,message,STRING,NO,NO\n")


@pytest.fixture
def project(tmp_path, monkeypatch):
    block_network(monkeypatch)
    folder = write_project(tmp_path / "project", PROJECT)
    path = tmp_path / "columns.csv"
    path.write_text(SNAPSHOT)
    return mcp_server.AgentServer(Session(load_sqlx_project(folder), root=folder, schema_snapshot=path))


def partition_findings(body):
    return [f for f in body.get("findings", []) if f["check"] in ("partition_pruning", "partition_filter_required")]


def test_check_query_reads_partitioning_from_the_dataform_config(project):
    body, failed, used = ask(project, "check_query", sql="SELECT n FROM events WHERE CAST(event_date AS STRING) IN ('2024-01-01')")
    [found] = partition_findings(body)
    assert not failed and used <= 2000 and body["verdict"] == "problems_found"
    assert found["check"] == "partition_filter_required" and found["level"] == "error"
    assert "events is partitioned on event_date (Dataform config), requires a partition filter" in found["what"]
    assert "event_date IN (DATE '2024-01-01')" in found["fix"] and body["counts"]["partition_filter_required"] == 1
    body, _, _ = ask(project, "check_query", sql="SELECT n FROM events WHERE event_date IN (DATE '2024-01-01')")
    assert partition_findings(body) == []
    # DATE(seen_at) partitions visits on seen_at
    body, _, _ = ask(project, "check_query", sql="SELECT user_id FROM visits WHERE CAST(seen_at AS STRING) LIKE '2024%'")
    [found] = partition_findings(body)
    assert found["level"] == "warning" and "visits is partitioned on seen_at (Dataform config)" in found["what"]
    body, _, _ = ask(project, "check_query", sql="SELECT user_id FROM visits WHERE DATE(seen_at) = '2024-01-01'")
    assert partition_findings(body) == []


def test_check_query_reads_partitioning_from_the_schema_snapshot(project):
    body, _, used = ask(project, "check_query", sql="SELECT message FROM raw.logs WHERE FORMAT_DATE('%Y%m%d', log_day) = '20240101'")
    [found] = partition_findings(body)
    assert used <= 2000 and found["level"] == "warning" and "logs is partitioned on log_day (schema snapshot)" in found["what"]
    assert "log_day = DATE '2024-01-01'" in found["fix"]


def test_check_query_says_nothing_about_a_table_whose_partitioning_is_unknown(project):
    body, _, _ = ask(project, "check_query", sql="SELECT n FROM plain WHERE CAST(event_date AS STRING) = '2024-01-01'")
    assert partition_findings(body) == [] and not any("pruned" in item["what"] for item in body.get("unknown", []))
    body, _, _ = ask(project, "check_query", sql="SELECT * FROM elsewhere.t WHERE CAST(d AS STRING) = '2024-01-01'")
    assert partition_findings(body) == []


def test_check_query_reads_partitioning_from_saved_table_metadata(project, monkeypatch):
    saved = {"data": {"partitioning": {"column": "day", "column_type": "DATE", "require_filter": True}}}
    monkeypatch.setattr(bigquery_catalog, "peek", lambda key: saved if key.endswith("other-proj\x1fds\x1fext") else None)
    body, _, _ = ask(project, "check_query", sql="SELECT * FROM `other-proj.ds.ext` WHERE status = 'x'")
    [found] = partition_findings(body)
    assert found["check"] == "partition_filter_required" and "(BigQuery table metadata)" in found["what"] and found["rule"] == "other_column"


def test_repository_and_snapshot_that_disagree_are_not_judged(tmp_path, monkeypatch):
    block_network(monkeypatch)
    folder = write_project(tmp_path / "project", {"t.sqlx": 'config { type: "table", bigquery: { partitionBy: "a" } }\nSELECT DATE \'2024-01-01\' AS a, DATE \'2024-01-01\' AS b\n'})
    for snapshot in ("table_schema,table_name,column_name,data_type,is_partitioning_column\nds,t,a,DATE,NO\nds,t,b,DATE,YES\n",
                     "table_schema,table_name,column_name,data_type,is_partitioning_column\nds,t,a,DATE,NO\nds,t,b,DATE,NO\n"):
        path = tmp_path / "columns.csv"
        path.write_text(snapshot)
        server = mcp_server.AgentServer(Session(load_sqlx_project(folder), root=folder, schema_snapshot=path))
        body, _, _ = ask(server, "check_query", sql="SELECT * FROM t WHERE CAST(a AS STRING) = '2024-01-01' AND CAST(b AS STRING) = '2024-01-01'")
        assert partition_findings(body) == [], snapshot


def test_a_session_without_the_hooks_is_silent():
    class Bare:
        def pipeline(self):
            raise RuntimeError("no project")

    tree = sqlglot.parse_one("SELECT * FROM ds.events WHERE CAST(d AS STRING) = 'x'", read="bigquery")
    assert pp.check_tree(tree, pp.session_lookup(Bare(), {"ds.events": "p.ds.events"})) == ([], [])


# ------------------------------------------------------------------------------------------------ the project check


PIPELINE = {
    **PROJECT,
    "daily.sqlx": 'config { type: "table" }\nSELECT status, COUNT(*) AS n FROM ${ref("events")} WHERE CAST(event_date AS STRING) = \'2024-01-01\' GROUP BY status\n',
    "good.sqlx": 'config { type: "table" }\nSELECT n FROM ${ref("events")} WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)\n',
    "everything.sqlx": 'config { type: "table" }\nSELECT n FROM ${ref("events")}\n',
    "recent.sqlx": 'config { type: "table" }\nSELECT user_id FROM ${ref("visits")} WHERE seen_at + INTERVAL 1 DAY > CURRENT_TIMESTAMP()\n',
    "off.sqlx": 'config { type: "table", disabled: true }\nSELECT n FROM ${ref("events")}\n',
    # the shape of the one finding on the public example projects (wintermi-bqe, v_project_storage): a "latest" view
    "latest.sqlx": 'config { type: "view" }\nSELECT user_id FROM ${ref("visits")} WHERE seen_at IN (SELECT MAX(seen_at) FROM ${ref("visits")})\n',
}


@pytest.fixture
def pipeline_server(tmp_path, monkeypatch):
    block_network(monkeypatch)
    folder = write_project(tmp_path / "project", PIPELINE)
    return mcp_server.AgentServer(Session(load_sqlx_project(folder), root=folder))


def test_project_checks_reports_models_that_filter_a_partitioned_table_without_pruning(pipeline_server):
    body, failed, used = ask(pipeline_server, "project_checks")
    assert not failed and used <= 2000 and {"partition_not_pruned", "partition_filter_required"} <= set(body["checked"])
    assert body["counts"]["partition_filter_required"] == 2 and body["counts"]["partition_not_pruned"] == 2
    assert body["findings"][0]["check"] == "partition_filter_required"  # BigQuery refuses these: they come first
    body, _, used = ask(pipeline_server, "project_checks", check="partition_filter_required")
    [required] = body["findings"]
    assert used <= 2000 and required["count"] == 2 and {e.split(" reads ")[0] for e in required["examples"]} == {"ds.daily", "ds.everything"}
    assert any("CAST(event_date AS STRING)" in e and "(function)" in e for e in required["examples"])
    assert any(e.endswith("no filter (no_filter)") for e in required["examples"])
    body, _, _ = ask(pipeline_server, "project_checks", check="partition_not_pruned")
    [warning] = body["findings"]
    assert warning["count"] == 2 and {e.split(" reads ")[0] for e in warning["examples"]} == {"ds.latest", "ds.recent"}
    assert any("IN (SELECT MAX(seen_at)" in e and "(dynamic_value)" in e for e in warning["examples"])
    assert any("seen_at + INTERVAL 1 DAY" in e and "(arithmetic)" in e for e in warning["examples"])


def test_a_check_query_on_the_models_sql_names_the_rewrite(pipeline_server):
    body, _, _ = ask(pipeline_server, "check_query", sql="SELECT user_id FROM ${ref(\"visits\")} WHERE seen_at + INTERVAL 1 DAY > CURRENT_TIMESTAMP()")
    [found] = partition_findings(body)
    assert "seen_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)" in found["fix"]


def test_a_variable_declared_in_pre_operations_counts_as_a_constant(tmp_path, monkeypatch):
    block_network(monkeypatch)
    files = {**PROJECT, "late.sqlx": ('config { type: "table" }\npre_operations { DECLARE cutoff DATE DEFAULT CURRENT_DATE(); }\n'
                                      'SELECT n FROM ${ref("events")} WHERE event_date >= cutoff\n')}
    folder = write_project(tmp_path / "project", files)
    session = Session(load_sqlx_project(folder), root=folder)
    assert [f for f in pp.project_findings(session) if f.model.endswith("late")] == []


# ------------------------------------------------------------------------------------------------ the BigQuery evidence


def _evidence():
    spec = importlib.util.spec_from_file_location("partition_pruning_evidence", ROOT / "tools" / "partition_pruning_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _docs_rows():
    text = (ROOT / "docs" / "partition-pruning.md").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        match = re.match(r"\| `([a-z0-9_]+)` \| (\w+) \| `(.*)` \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", line)
        if match:
            rows[match.group(1)] = {"table": match.group(2), "page": match.group(4).strip(), "measured": match.group(5).strip(),
                                    "bytes": match.group(6).strip(), "detector": match.group(7).strip()}
    return rows


def test_the_docs_table_records_what_the_detector_says():
    """Every case of the evidence script is in the docs table with its measured outcome, and the detector agrees with it."""

    evidence = _evidence()
    rows = _docs_rows()
    assert set(rows) == {case[0] for case in evidence.CASES}
    for case in evidence.CASES:
        row = rows[case[0]]
        said = evidence.detector_says(case)
        assert row["detector"] == said, case[0]
        assert row["measured"] in ("prunes", "full_scan"), case[0]
        assert said == evidence.expected(case[1], row["measured"]), (case[0], row["measured"], said)


def test_the_evidence_script_sends_only_dry_runs_and_classifies_the_answers(tmp_path, monkeypatch):
    evidence = _evidence()
    from kumosql import dryrun

    sent = []

    def transport(url, headers, body):
        request = json.loads(body)
        assert request["configuration"]["dryRun"] is True and url.endswith("/projects/billing-test/jobs")
        sql = request["configuration"]["query"]["query"]
        sent.append(sql)
        if "pageviews" in sql:
            if "CAST(datehour AS STRING)" in sql:
                return 400, {"error": {"message": "Cannot query over table 'x' without a filter over column(s) 'datehour' that can be used for partition elimination",
                                       "errors": [{"reason": "invalidQuery"}]}}
            return 200, {"statistics": {"totalBytesProcessed": "5", "query": {}}}
        full = "COUNTIF(" in sql or "CAST(refresh_date AS STRING)" in sql
        return 200, {"statistics": {"totalBytesProcessed": "1000" if full else "10", "query": {}}}

    def metadata(name, token):
        spec = next(s for s in evidence.TABLES.values() if s["name"] == name)
        return {"type": "TABLE", "num_rows": 1, "num_bytes": 1, "partition_type": "DAY", "partition_column": spec["column"],
                "partition_column_type": spec["type"], "require_partition_filter": spec["require"]}

    monkeypatch.setenv("BQ_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(dryrun, "_urllib_transport", transport)
    monkeypatch.setattr(evidence, "table_metadata", metadata)
    out = tmp_path / "measured.json"
    assert evidence.main(["--project", "billing-test", "--only", "d_eq,d_cast_string_in,t_eq,t_cast_string_in", "--out", str(out), "--workers", "1"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in report["cases"]}
    assert {k: v["measured"] for k, v in by_id.items()} == {"d_eq": "prunes", "d_cast_string_in": "full_scan", "t_eq": "prunes", "t_cast_string_in": "full_scan"}
    assert {k: v["detector"] for k, v in by_id.items()} == {"d_eq": "silent", "d_cast_string_in": "warning", "t_eq": "silent", "t_cast_string_in": "error"}
    assert all(row["agrees"] for row in report["cases"]) and len(sent) == 6  # two date cases with a reference each, two timestamp cases


def test_the_evidence_script_refuses_a_table_whose_partitioning_changed(monkeypatch, capsys):
    evidence = _evidence()
    monkeypatch.setenv("BQ_ACCESS_TOKEN", "test-token")
    monkeypatch.setattr(evidence, "table_metadata", lambda name, token: {"partition_column": None, "partition_column_type": None, "require_partition_filter": False})
    assert evidence.main(["--project", "billing-test"]) == 2
    assert "partitioning is not what the cases assume" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        evidence.main([])  # the billing project is never guessed
