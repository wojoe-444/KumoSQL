"""Measure which filters let BigQuery prune partitions, with free dry runs on two public partitioned tables.

The rules of ``kumosql.partition_pruning`` come from Google's documentation ("Query partitioned tables"). This script
checks them against BigQuery itself. Only dry runs are sent (``configuration.dryRun``: BigQuery plans the query and
reports ``totalBytesProcessed``, without reading data or billing anything) plus one table-metadata read per table
(``tables.get``, which reads no rows). It never runs a query.

Two tables of ``bigquery-public-data`` are used, after checking their partitioning in the table metadata:

* ``google_trends.top_terms``: partitioned by day on ``refresh_date`` (DATE), no partition filter required. A case's
  bytes are compared with a **reference** query that reads the same columns and cannot prune (the filter moved into
  ``COUNTIF``). Fewer bytes than the reference means the filter pruned; the same bytes means every partition was read.
* ``wikipedia.pageviews_2024``: partitioned by day on ``datehour`` (TIMESTAMP) with ``require_partition_filter``. BigQuery
  refuses a query without a filter it can use for partition elimination, so "accepted" or "refused" is the answer
  (and the bytes of an accepted one show how much is read).

Each case also shows what KumoSQL's detector says about the same SQL, so a disagreement is visible.

Credentials: ``BQ_ACCESS_TOKEN``, a service account, or application-default credentials (``gcloud auth
application-default login``); nothing is printed or stored about them. The project billed for the dry runs must be given;
it is never guessed, and nothing is billed to it.

    python tools/partition_pruning_evidence.py --project MY_PROJECT
    python tools/partition_pruning_evidence.py --project MY_PROJECT --out measured.json --only cast_string,or_other
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SOURCE = str(Path(__file__).resolve().parent.parent / "src")
if __name__ == "__main__" and SOURCE not in sys.path:
    sys.path.insert(0, SOURCE)  # run from a checkout: use its kumosql

#: the public tables, with the partitioning the metadata must show before any case on them is sent
TABLES = {
    "date": {"name": "bigquery-public-data.google_trends.top_terms", "column": "refresh_date", "type": "DATE", "require": False},
    "timestamp": {"name": "bigquery-public-data.wikipedia.pageviews_2024", "column": "datehour", "type": "TIMESTAMP", "require": True},
}

D1, D2 = "DATE '2026-11-20'", "DATE '2026-11-21'"
T1, T2 = "TIMESTAMP '2024-03-30 12:00:00'", "TIMESTAMP '2024-03-31 12:00:00'"

#: ``(id, table, rule, documented, where-clause or full SQL)``. ``documented`` is what Google's page says: ``prunes``,
#: ``full_scan``, or ``None`` when the page does not say. A full statement uses ``{t}`` for the table.
CASES: list[tuple[str, str, str, str | None, str]] = [
    # direct comparisons with constants
    ("d_eq", "date", "direct", "prunes", f"refresh_date = {D1}"),
    ("d_in", "date", "direct", "prunes", f"refresh_date IN ({D1}, {D2})"),
    ("d_between", "date", "direct", "prunes", f"refresh_date BETWEEN {D1} AND {D2}"),
    ("d_string_literal", "date", "direct", "prunes", "refresh_date = '2026-11-20'"),
    ("d_current_date", "date", "direct", "prunes", "refresh_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)"),
    ("d_constant_left", "date", "direct", "prunes", "PARSE_DATE('%Y%m%d', '20261120') = refresh_date"),
    ("d_neq", "date", "direct", None, f"refresh_date != {D1}"),
    ("d_is_null", "date", "direct", None, "refresh_date IS NULL"),
    ("t_eq", "timestamp", "direct", "prunes", f"datehour = {T1}"),
    ("t_between", "timestamp", "direct", "prunes", f"datehour BETWEEN {T1} AND {T2}"),
    ("t_current_timestamp", "timestamp", "direct", "prunes", "datehour > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"),
    ("t_neq", "timestamp", "direct", None, f"datehour != {T1}"),
    ("t_is_not_null", "timestamp", "direct", None, "datehour IS NOT NULL"),
    # functions BigQuery's page lists (or shows in its examples) as keeping pruning
    ("t_date", "timestamp", "supported_function", "prunes", "DATE(datehour) = '2024-03-30'"),
    ("t_cast_date", "timestamp", "supported_function", "prunes", "CAST(datehour AS DATE) = '2024-03-30'"),
    ("t_cast_date_in", "timestamp", "supported_function", "prunes", "CAST(datehour AS DATE) IN ('2024-03-30', '2024-03-31')"),
    ("t_extract_date", "timestamp", "supported_function", "prunes", "EXTRACT(DATE FROM datehour) = '2024-03-30'"),
    ("t_extract_year", "timestamp", "supported_function", "prunes", "EXTRACT(YEAR FROM datehour) = 2024"),
    ("t_timestamp_trunc", "timestamp", "supported_function", "prunes", "TIMESTAMP_TRUNC(datehour, MONTH) = TIMESTAMP '2024-03-01'"),
    ("t_timestamp_add", "timestamp", "supported_function", "prunes", f"TIMESTAMP_ADD(datehour, INTERVAL 1 DAY) = {T2}"),
    ("t_timestamp_sub", "timestamp", "supported_function", "prunes", f"TIMESTAMP_SUB(datehour, INTERVAL 1 DAY) = {T1}"),
    ("t_timestamp_diff", "timestamp", "supported_function", "prunes", f"TIMESTAMP_DIFF(datehour, {T1}, DAY) = 0"),
    ("t_format_f", "timestamp", "supported_function", "prunes", "FORMAT_TIMESTAMP('%F', datehour) = '2024-03-30'"),
    ("t_format_ymd", "timestamp", "supported_function", "prunes", "FORMAT_TIMESTAMP('%Y%m%d', datehour) = '20240330'"),
    ("d_date_trunc", "date", "supported_function", "prunes", "DATE_TRUNC(refresh_date, MONTH) = DATE '2026-11-01'"),
    ("d_date_add", "date", "supported_function", "prunes", f"DATE_ADD(refresh_date, INTERVAL 1 DAY) = {D2}"),
    ("d_date_sub", "date", "supported_function", "prunes", f"DATE_SUB(refresh_date, INTERVAL 1 DAY) = {D1}"),
    ("d_date_diff", "date", "supported_function", "prunes", f"DATE_DIFF(refresh_date, {D1}, DAY) = 0"),
    # other functions and casts around the partition column
    ("d_cast_string_in", "date", "function", "full_scan", "CAST(refresh_date AS STRING) IN ('2026-11-20', '2026-11-21')"),
    ("d_cast_string_eq", "date", "function", "full_scan", "CAST(refresh_date AS STRING) = '2026-11-20'"),
    ("d_safe_cast_string", "date", "function", "full_scan", "SAFE_CAST(refresh_date AS STRING) = '2026-11-20'"),
    ("d_format_date_ymd", "date", "function", "full_scan", "FORMAT_DATE('%Y%m%d', refresh_date) = '20261120'"),
    ("d_format_date_iso", "date", "function", "full_scan", "FORMAT_DATE('%Y-%m-%d', refresh_date) = '2026-11-20'"),
    ("d_extract_month", "date", "function", "full_scan", "EXTRACT(MONTH FROM refresh_date) = 11"),
    ("d_unix_date", "date", "function", "full_scan", "UNIX_DATE(refresh_date) = 20412"),
    ("d_cast_timestamp", "date", "function", None, "CAST(refresh_date AS TIMESTAMP) = TIMESTAMP '2026-11-20'"),
    ("d_timestamp_fn", "date", "function", None, "TIMESTAMP(refresh_date) = TIMESTAMP '2026-11-20'"),
    ("t_cast_string_in", "timestamp", "function", "full_scan", "CAST(datehour AS STRING) IN ('2024-03-30 12:00:00+00')"),
    ("t_string_fn", "timestamp", "function", "full_scan", "STRING(datehour) = '2024-03-30 12:00:00+00'"),
    ("t_format_hour", "timestamp", "function", "full_scan", "FORMAT_TIMESTAMP('%Y-%m-%d %H', datehour) = '2024-03-30 12'"),
    ("t_extract_hour", "timestamp", "function", "full_scan", "EXTRACT(HOUR FROM datehour) = 12"),
    ("t_unix_seconds", "timestamp", "function", "full_scan", "UNIX_SECONDS(datehour) = 1711800000"),
    ("t_date_zone", "timestamp", "function", None, "DATE(datehour, 'America/Los_Angeles') = '2024-03-30'"),
    ("t_datetime", "timestamp", "function", None, "DATETIME(datehour) = DATETIME '2024-03-30 12:00:00'"),
    ("t_datetime_trunc", "timestamp", "function", None, "DATETIME_TRUNC(DATETIME(datehour), DAY) = DATETIME '2024-03-30 00:00:00'"),
    # arithmetic on the partition column
    ("t_plus_interval", "timestamp", "arithmetic", "full_scan", "datehour + INTERVAL 1 DAY > TIMESTAMP '2024-12-30'"),
    ("d_plus_interval", "date", "arithmetic", "full_scan", "refresh_date + INTERVAL 1 DAY > DATETIME '2026-11-20 00:00:00'"),
    # a value that is not constant
    ("d_scalar_subquery", "date", "dynamic_value", "full_scan", f"refresh_date = (SELECT {D1})"),
    ("d_in_subquery", "date", "dynamic_value", "full_scan", f"refresh_date IN (SELECT {D1})"),
    ("d_other_column", "date", "dynamic_value", "full_scan", "refresh_date >= week"),
    ("t_scalar_subquery", "timestamp", "dynamic_value", "full_scan", f"datehour = (SELECT {T1})"),
    ("t_in_subquery", "timestamp", "dynamic_value", "full_scan", f"datehour IN (SELECT {T1})"),
    # OR
    ("d_or_other", "date", "or_other_column", "full_scan", f"refresh_date = {D1} OR term = 'x'"),
    ("d_or_same", "date", "or_other_column", None, f"refresh_date = {D1} OR refresh_date = {D2}"),
    ("t_or_other", "timestamp", "or_other_column", "full_scan", f"datehour = {T1} OR wiki = 'en'"),
    ("t_or_same", "timestamp", "or_other_column", None, f"datehour = {T1} OR datehour = {T2}"),
    ("t_or_both_sides", "timestamp", "or_other_column", None, f"(datehour = {T1} AND wiki = 'en') OR (datehour = {T2} AND wiki = 'de')"),
    # a filter on another column only, and AND with one pruning filter
    ("d_other_only", "date", "other_column", "full_scan", "week = DATE '2026-11-16'"),
    ("t_other_only", "timestamp", "other_column", "full_scan", "wiki = 'en'"),
    ("t_and_other", "timestamp", "other_column", "prunes", f"datehour = {T1} AND wiki = 'en'"),
    ("d_and_cast", "date", "function", "prunes", f"refresh_date = {D1} AND CAST(refresh_date AS STRING) LIKE '2026%'"),
    # through a CTE, a subquery, an aggregate and a join
    ("d_cte_renamed", "date", "pushdown", None,
     f"WITH x AS (SELECT refresh_date AS day, term FROM {{t}}) SELECT COUNT(term) FROM x WHERE day = {D1}"),
    ("d_group_by", "date", "pushdown", None,
     f"SELECT COUNT(*) FROM (SELECT refresh_date, COUNT(*) AS c FROM {{t}} GROUP BY refresh_date) WHERE refresh_date = {D1}"),
    ("t_cte_renamed", "timestamp", "pushdown", None,
     f"WITH x AS (SELECT datehour AS h, views FROM {{t}}) SELECT SUM(views) FROM x WHERE h = {T1}"),
    ("t_group_by", "timestamp", "pushdown", None,
     f"SELECT COUNT(*) FROM (SELECT datehour, SUM(views) AS v FROM {{t}} GROUP BY datehour) WHERE datehour = {T1}"),
    ("t_window", "timestamp", "pushdown", None,
     f"SELECT COUNT(*) FROM (SELECT datehour, SUM(views) OVER (PARTITION BY wiki) AS v FROM {{t}}) WHERE datehour = {T1}"),
    ("t_join_constant", "timestamp", "join", None,
     f"SELECT COUNT(*) FROM {{t}} AS p JOIN (SELECT {T1} AS h) AS c ON p.datehour = c.h"),
    ("d_join_constant", "date", "join", None,
     f"SELECT COUNT(*) FROM {{t}} AS p JOIN (SELECT {D1} AS d) AS c ON p.refresh_date = c.d"),
    # round 2: shapes the first run left open
    ("d_extract_year_none", "date", "supported_function", "prunes", "EXTRACT(YEAR FROM refresh_date) = 2020"),
    ("t_extract_year_none", "timestamp", "supported_function", "prunes", "EXTRACT(YEAR FROM datehour) = 2023"),
    ("d_scalar_subquery_table", "date", "dynamic_value", "full_scan", f"refresh_date = (SELECT MAX(refresh_date) FROM {{t}} WHERE refresh_date = {D1})"),
    ("t_scalar_subquery_table", "timestamp", "dynamic_value", "full_scan",
     f"datehour = (SELECT TIMESTAMP(MAX(refresh_date)) FROM `{TABLES['date']['name']}` WHERE refresh_date = {D1})"),
    ("d_or_both_sides", "date", "or_other_column", None, f"(refresh_date = {D1} AND term = 'x') OR (refresh_date = {D2} AND term = 'y')"),
    ("t_in_unnest", "timestamp", "direct", None, f"datehour IN UNNEST([{T1}, {T2}])"),
    ("t_not", "timestamp", "direct", None, "NOT (datehour < TIMESTAMP '2024-12-30')"),
    ("t_ifnull", "timestamp", "function", None, f"IFNULL(datehour, {T1}) = {T1}"),
    ("t_safe_cast_date", "timestamp", "function", None, "SAFE_CAST(datehour AS DATE) = '2024-03-30'"),
    ("t_string_of_date", "timestamp", "function", "full_scan", "CAST(CAST(datehour AS DATE) AS STRING) = '2024-03-30'"),
    ("t_join_table", "timestamp", "join", None,
     f"SELECT COUNT(*) FROM {{t}} AS p JOIN (SELECT TIMESTAMP(refresh_date) AS h FROM `{TABLES['date']['name']}` WHERE refresh_date = {D1}) AS c ON p.datehour = c.h"),
    ("t_join_transitive", "timestamp", "join", None,
     f"SELECT COUNT(*) FROM {{t}} AS p JOIN (SELECT TIMESTAMP(refresh_date) AS h FROM `{TABLES['date']['name']}`) AS c ON p.datehour = c.h WHERE c.h = {T1}"),
    ("t_left_join_on", "timestamp", "join", None,
     f"SELECT COUNT(*) FROM (SELECT 1 AS k) AS a LEFT JOIN {{t}} AS p ON p.datehour = {T1}"),
    ("t_window_used", "timestamp", "pushdown", None,
     f"SELECT SUM(v) FROM (SELECT datehour, SUM(views) OVER (PARTITION BY wiki) AS v FROM {{t}}) WHERE datehour = {T1}"),
    ("t_window_by_column", "timestamp", "pushdown", None,
     f"SELECT SUM(v) FROM (SELECT datehour, SUM(views) OVER (PARTITION BY datehour) AS v FROM {{t}}) WHERE datehour = {T1}"),
    ("t_having", "timestamp", "pushdown", None,
     f"SELECT datehour, COUNT(*) FROM {{t}} GROUP BY datehour HAVING datehour = {T1}"),
    ("t_inner_join_on", "timestamp", "join", None,
     f"SELECT COUNT(*) FROM {{t}} AS p JOIN (SELECT 1 AS k) AS a ON a.k = 1 AND p.datehour = {T1}"),
    ("d_join_table", "date", "join", None,
     f"SELECT COUNT(*) FROM {{t}} AS p JOIN (SELECT week AS d FROM {{t}} WHERE refresh_date = {D1}) AS c ON p.refresh_date = c.d"),
    ("d_left_join_on", "date", "join", None,
     f"SELECT COUNT(*) FROM (SELECT 1 AS k) AS a LEFT JOIN {{t}} AS p ON p.refresh_date = {D1}"),
    # the rewrites the detector suggests, measured as written
    ("t_plus_interval_rewritten", "timestamp", "arithmetic", "prunes", "datehour > TIMESTAMP_SUB(TIMESTAMP '2024-12-30', INTERVAL 1 DAY)"),
    ("d_plus_interval_rewritten", "date", "arithmetic", None, "refresh_date > DATETIME_SUB(DATETIME '2026-11-20 00:00:00', INTERVAL 1 DAY)"),
    ("d_format_month_rewritten", "date", "supported_function", "prunes", "DATE_TRUNC(refresh_date, MONTH) = DATE '2026-11-01'"),
    ("d_format_month", "date", "function", "full_scan", "FORMAT_DATE('%Y-%m', refresh_date) = '2026-11'"),
]


def statement(case: tuple) -> tuple[str, str | None]:
    """``(sql, reference sql or None)`` for a case; the reference reads the same columns and cannot prune."""

    _id, table, _rule, _documented, text = case
    name = f"`{TABLES[table]['name']}`"
    if text.lstrip().upper().startswith(("SELECT", "WITH")):
        sql = text.replace("{t}", name)
        column = TABLES[table]["column"]
        reference = None if TABLES[table]["require"] else f"SELECT COUNT({column}), COUNT(*) FROM {name}" if "term" not in text else f"SELECT COUNT({column}), COUNT(term) FROM {name}"
        return sql, reference
    text = text.replace("{t}", name)
    sql = f"SELECT COUNT(*) FROM {name} WHERE {text}"
    if TABLES[table]["require"]:
        return sql, None
    if "SELECT" in text.upper():
        # an aggregate cannot hold a subquery: read the partition column whole instead
        return sql, f"SELECT COUNT({TABLES[table]['column']}) FROM {name}"
    return sql, f"SELECT COUNTIF({text}) FROM {name}"


def table_metadata(name: str, token: str) -> dict:
    """The table's size and partitioning from ``tables.get`` (metadata only: no rows are read, nothing is billed)."""

    from kumosql.bigquery_catalog import partitioning_of

    project, dataset, table = (urllib.parse.quote(part, safe="") for part in name.split("."))
    url = (f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/{dataset}/tables/{table}"
           "?fields=type,numRows,numBytes,timePartitioning,rangePartitioning,requirePartitionFilter,schema")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read())
    layout = partitioning_of(data) or {}
    return {
        "type": data.get("type"),
        "num_rows": int(data.get("numRows") or 0),
        "num_bytes": int(data.get("numBytes") or 0),
        "partition_type": (data.get("timePartitioning") or {}).get("type"),
        "partition_column": layout.get("column"),
        "partition_column_type": layout.get("column_type"),
        "require_partition_filter": bool(layout.get("require_filter")),
    }


def verdict(table: str, result, reference) -> str:
    """``prunes``, ``full_scan`` or ``error: ...`` from the dry run of a case (and of its reference)."""

    if not result.ok:
        message = (result.error_message or "").casefold()
        if "partition elimination" in message:
            return "full_scan"  # refused by require_partition_filter: no filter BigQuery can prune with
        return f"error: {result.error_reason or 'rejected'}"
    if TABLES[table]["require"]:
        return "prunes"  # accepted although the table requires a filter that can be used for partition elimination
    if reference is None or not reference.ok or reference.total_bytes_processed is None:
        return "error: reference failed"
    return "prunes" if (result.total_bytes_processed or 0) < reference.total_bytes_processed else "full_scan"


def detector_says(case: tuple) -> str:
    """What ``kumosql.partition_pruning`` says about the case: ``silent``, ``warning``, ``error`` or ``unknown`` (a gap)."""

    from kumosql import partition_pruning

    table = TABLES[case[1]]
    sql, _ = statement(case)
    spec = partition_pruning.Partitioning(column=table["column"], column_type=table["type"], require_filter=table["require"], source="test")
    issues, gaps = partition_pruning.check_sql(sql, lambda name: spec if name.casefold() == table["name"].casefold() else None)
    if issues:
        return "error" if any(i.level == "error" for i in issues) else "warning"
    return "unknown" if gaps else "silent"


def expected(table: str, measured: str) -> str | None:
    """What the detector should say about a case BigQuery measured: silent when it prunes, else a warning (an error on the
    table that requires a partition filter); ``None`` when the measurement failed."""

    if measured == "prunes":
        return "silent"
    if measured == "full_scan":
        return "error" if TABLES[table]["require"] else "warning"
    return None


def shape(case: tuple) -> str:
    """The case's filter (or query) as the docs table shows it, with the table written ``t``."""

    text = case[4].replace("{t}", "t").replace(f"`{TABLES['date']['name']}`", "top_terms")
    return text if len(text) <= 110 else text[:107] + "..."


def markdown(rows: list[dict]) -> str:
    lines = ["| case | table | filter or query | the page says | measured | bytes (reference) | KumoSQL |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        if row["bytes"] is None:
            size = "refused" if row["measured"] == "full_scan" else "error"
        elif row["reference_bytes"] is not None:
            size = f"{row['bytes']:,} ({row['reference_bytes']:,})"
        else:
            size = f"{row['bytes']:,} (accepted)"
        text = row["shape"].replace("|", "\\|")
        lines.append(f"| `{row['id']}` | {row['table'].split('.')[-1]} | `{text}` | {row['documented'] or 'not said'} | {row['measured']} | {size} | {row['detector']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="the project the dry runs are created in (nothing is billed); never guessed")
    parser.add_argument("--location", default="US")
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--out", help="write the measurements as JSON to this file")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    from kumosql.dryrun import access_token, dry_run

    token = access_token()
    meta = {key: table_metadata(spec["name"], token) for key, spec in TABLES.items()}
    for key, spec in TABLES.items():
        got = meta[key]
        if (got["partition_column"], got["partition_column_type"], got["require_partition_filter"]) != (spec["column"], spec["type"], spec["require"]):
            print(f"{spec['name']}: partitioning is not what the cases assume: {got}", file=sys.stderr)
            return 2
    wanted = set(args.only.split(",")) if args.only else None
    cases = [c for c in CASES if wanted is None or c[0] in wanted]

    def run(case):
        sql, reference_sql = statement(case)
        result = dry_run(sql, args.project, location=args.location, token=token)
        reference = dry_run(reference_sql, args.project, location=args.location, token=token) if reference_sql else None
        return case, sql, result, reference

    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for case, sql, result, reference in pool.map(run, cases):
            case_id, table, rule, documented, _text = case
            measured = verdict(table, result, reference)
            try:
                detector = detector_says(case)
            except Exception as exc:  # noqa: BLE001 - the measurement stands without the detector
                detector = f"error: {type(exc).__name__}"
            rows.append({
                "id": case_id, "table": TABLES[table]["name"], "rule": rule, "documented": documented, "sql": sql, "shape": shape(case),
                "bytes": result.total_bytes_processed if result.ok else None,
                "reference_bytes": reference.total_bytes_processed if reference is not None and reference.ok else None,
                "error": None if result.ok else f"{result.error_reason}: {(result.error_message or '')[:160]}",
                "measured": measured, "detector": detector, "agrees": detector == expected(table, measured),
            })
    report = {"date": _dt.date.today().isoformat(), "location": args.location, "tables": meta, "cases": rows}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(markdown(rows))
    disagree = [row["id"] for row in rows if not row["agrees"]]
    print(f"\n{len(rows)} cases measured on {report['date']}; the detector disagrees on {len(disagree)}: {', '.join(disagree) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
