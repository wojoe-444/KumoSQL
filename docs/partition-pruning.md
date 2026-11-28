# Partition pruning: filters BigQuery cannot prune with

`check_query` and `project_checks` warn when a query filters a partitioned table in a way BigQuery will not use to skip partitions, so the query reads the whole table. On a table created with `require_partition_filter`, BigQuery refuses such a query, and the warning becomes an error. Each finding names the table, its partition column, the filter, the rule it breaks, and a rewrite when one is mechanical. The code is [`kumosql.partition_pruning`](../src/kumosql/partition_pruning.py), the measurements come from [`tools/partition_pruning_evidence.py`](../tools/partition_pruning_evidence.py), and the tests are in [`tests/test_partition_pruning.py`](../tests/test_partition_pruning.py).

## Example

`events` is a Dataform table with `bigquery: { partitionBy: "event_date" }`. An analyst writes:

```sql
SELECT status, COUNT(*) FROM ${ref("events")}
WHERE CAST(event_date AS STRING) IN ('2024-01-01', '2024-01-02')
GROUP BY status
```

`check_query` answers with one finding:

```json
{"check": "partition_pruning", "level": "warning", "rule": "function",
 "what": "ds.events is partitioned on event_date (Dataform config): CAST(event_date AS STRING) IN ('2024-01-01', '2024-01-02') does not prune (CAST(... AS STRING) is not one of the functions BigQuery prunes through); BigQuery reads every partition",
 "fix": "Write event_date IN (DATE '2024-01-01', DATE '2024-01-02') instead (same rows; BigQuery prunes on it)."}
```

On the public table `google_trends.top_terms`, partitioned on `refresh_date`, BigQuery's dry run puts the two shapes at 285,845,280 bytes (every partition) and 17,137,680 bytes (two days). The cases `d_cast_string_in` and `d_in` are in the measurements below. If the table also had `requirePartitionFilter: true`, the finding would be `partition_filter_required` with `level: error`, because BigQuery then refuses the first query.

## Where the partitioning comes from

KumoSQL never guesses. A table is judged only when one of these says how it is partitioned:

1. **The schema snapshot** (`--schema-snapshot`): `is_partitioning_column = YES` on one column. The optional `require_partition_filter` column (`YES`/`NO`, from `INFORMATION_SCHEMA.TABLE_OPTIONS`, repeated on each row of the table) says whether a filter is required. An ingestion-time partitioned table shows `_PARTITIONTIME`. See [the snapshot format](mcp-server.md#column-types-and-the-schema-snapshot).
2. **The model's Dataform config**: `partitionBy` (top level or under `bigquery`). `"event_date"`, `"DATE(created_at)"`, `"TIMESTAMP_TRUNC(ts, HOUR)"`, `"DATETIME_TRUNC(dt, MONTH)"` and `"RANGE_BUCKET(id, GENERATE_ARRAY(...))"` are read, and `requirePartitionFilter: true|false` is read too. A computed `partitionBy` (a variable or a call) is not read.
3. **Saved BigQuery table metadata**: the opt-in schema fetch (`KUMOSQL_SCHEMA_FETCH=1`, the Settings checkbox, `--fetch-schema`) now keeps a table's `timePartitioning`, `rangePartitioning` and `requirePartitionFilter` with its columns. The check reads the saved copy only and never calls BigQuery.

When the snapshot and the config disagree on the column, or the snapshot says the deployed table is not partitioned while the config says it is, nothing is judged. `deployed_status` reports that drift. A table none of the three describes gets no finding and no gap.

## The rules

Google's page [Query partitioned tables](https://cloud.google.com/bigquery/docs/querying-partitioned-tables) (read 2026-11-28) is the source. Each rule quotes it. Where the page says nothing, a dry run on a public table decided (the measurements below), and a shape that neither settles is listed under `unknown`, never reported.

| `rule` | Finds | The page says | Measured (case ids below) |
| --- | --- | --- | --- |
| `function` | the partition column inside a function or cast BigQuery does not prune through: `CAST`/`SAFE_CAST` to `STRING`, `STRING()`, `FORMAT_DATE`, `FORMAT_TIMESTAMP` with another format or a time zone, `EXTRACT` of a part other than `YEAR`/`DATE`, `UNIX_DATE`, `UNIX_SECONDS`, `DATETIME(ts)`, `IFNULL`, `SAFE_CAST(ts AS DATE)`, and any function over an integer-range column | "The following built-in functions on the partitioning column support partition pruning, if their additional arguments are constant: DATE_ADD, DATE_DIFF, DATE_SUB, DATE_TRUNC, EXTRACT with YEAR part, DATETIME_DIFF, TIMESTAMP_ADD, TIMESTAMP_DIFF, TIMESTAMP_SUB, TIMESTAMP_TRUNC, EXTRACT with DATE or YEAR parts, FORMAT_TIMESTAMP with the following format specifiers: %F, %Y-%m-%d and %Y%m%d." "Other functions and complex mathematical operations will require a full table scan." "Partition pruning is not supported for functions over an integer range partitioned column." | every listed function prunes; so do `DATE(ts)` and `CAST(ts AS DATE)` (the page's examples) and, not on the page, `DATE(ts, 'zone')`, `CAST(d AS TIMESTAMP)` and `TIMESTAMP(d)` (`t_date_zone`, `d_cast_timestamp`, `d_timestamp_fn`). Every function in the first column reads every partition: `d_cast_string_in`, `d_format_date_ymd`, `d_extract_month`, `t_datetime`, `t_ifnull`, `t_safe_cast_date`, ... |
| `arithmetic` | arithmetic on the column (`ts + INTERVAL 1 DAY > ...`, `id + 1 BETWEEN ...`) | "a query with the following predicate doesn't prune partitions because it manipulates the partitioning column with an arithmetic operation: WHERE ts + INTERVAL 1 DAY > CURRENT_TIMESTAMP()" | `t_plus_interval`, `d_plus_interval` read everything; the rewrites `t_plus_interval_rewritten`, `d_plus_interval_rewritten` prune |
| `dynamic_value` | the column compared with something BigQuery does not know when it plans the query: a subquery that reads a table, `IN (SELECT ...)`, another column, a join on the partition column | "To limit the partitions that are scanned in a query, filter the partitioning column using a constant expression, rather than a dynamic expression." For `t1.ts = (SELECT timestamp FROM table3 WHERE key = 2)`: "This query compares the partitioning column to a dynamic value, what prevents partition pruning." | `d_scalar_subquery_table`, `d_in_subquery`, `d_other_column`, `d_join_table`, `t_join_transitive` (a join stays unpruned even with a constant filter on the other table). A scalar subquery with no `FROM` is folded and prunes (`d_scalar_subquery`), and so does a join to one (`t_join_constant`). `IN (SELECT <constant>)` does not (`d_in_subquery`) |
| `or_other_column` | an `OR` with a branch that does not filter the partition column | "If AND is changed to OR, partition pruning won't work" | `d_or_other`, `t_or_other`; an `OR` of filters on the column alone prunes (`d_or_same`) |
| `or_mixed` | on a table that requires a partition filter: an `OR` whose every branch filters the column and other columns. It prunes, but BigQuery does not take it as the required filter | "There must be at least one predicate that only references a partitioning column" | `d_or_both_sides` prunes on the plain table; `t_or_both_sides` is refused on the required one |
| `outer_join_on` | the only filter on the column is in the `ON` of an outer join | not said | `t_left_join_on` refused, `d_left_join_on` reads everything; the `ON` of an inner join counts (`t_inner_join_on`) |
| `other_column` | the table is filtered (a column of it compared with constants) but never on its partition column | the required-filter example: `WHERE partition_id = "20221231" OR f = "20221130"` "is not sufficient" | `d_other_only`, `t_other_only` |
| `no_filter` | on a table that requires a partition filter: no filter at all. On other tables a whole read is often meant, so it is not reported | "Cannot query over table 'project_id.dataset.table' without a filter that can be used for partition elimination" | BigQuery's own message reads "without a filter over column(s) 'datehour' that can be used for partition elimination" |

Filters outside the scan count when BigQuery pushes them down (measured): a filter on a CTE or subquery column, renamed or not (`d_cte_renamed`, `t_cte_renamed`), through `GROUP BY` and `HAVING` on the column (`d_group_by`, `t_having`), and through a window partitioned by the column (`t_window_by_column`). A filter outside a window partitioned by another column whose value is read is not pushed down (`t_window_used`). If the window's value is never read, BigQuery drops the window and pushes the filter (`t_window`). Like everything here, this is conservative: any outer filter that compares a column of the CTE or subquery with constants silences the finding, unless that column is an aggregate or a window value.

`!=`, `IS NULL`, `NOT (...)`, `IN UNNEST([...])` and `IS NOT NULL` with constants are accepted (`d_neq`, `d_is_null`, `t_not`, `t_in_unnest`, `t_is_not_null`). On `pageviews_2024`, `IS NOT NULL` and `!=` read nearly every partition (469,850,211,392 bytes for both, the whole `datehour` column). BigQuery still counts them as partition filters, so they are not findings. Script variables declared in a model's `pre_operations` (`DECLARE cutoff DATE ...`) and query parameters (`@day`) count as constants. They are not measured, and they can only silence a finding, never raise one.

## Suggested rewrites

A rewrite is offered only when it returns the same rows and is mechanical. Each kind is checked in the tests by running the detector on the rewrite. The measured cases show that the shapes prune.

| Finding | Rewrite |
| --- | --- |
| `CAST(d AS STRING) op '2024-01-01'` (also `SAFE_CAST`, `IN`, `BETWEEN`) on a `DATE` column | `d op DATE '2024-01-01'` (ISO dates compare as text in date order; a literal that is not a real date gets no rewrite) |
| `FORMAT_DATE('%Y%m%d' \| '%Y-%m-%d' \| '%F', d) op '...'` | `d op DATE '...'` |
| `FORMAT_DATE('%Y-%m' \| '%Y%m', d) op '2024-01'` | `DATE_TRUNC(d, MONTH) op DATE '2024-01-01'` (`d_format_month_rewritten`) |
| `FORMAT_TIMESTAMP('%Y-%m-%d %H', ts) = '2024-03-30 12'` | `TIMESTAMP_TRUNC(ts, HOUR) = TIMESTAMP '2024-03-30 12:00:00'` |
| `ts + INTERVAL n unit op c` (or `-`) | `ts op TIMESTAMP_SUB(c, INTERVAL n unit)`; `DATE_SUB` or `DATETIME_SUB` for a `DATE` column compared with a typed constant |
| `id + k BETWEEN a AND b` on an integer-range column | `id BETWEEN a-k AND b-k`, computed |
| `e.d = c.d` with `c.d BETWEEN ...` in the same query | also add `e.d BETWEEN ...` (`t_join_transitive`: the join alone is refused) |
| `or_mixed`: `(d = A AND x) OR (d = B AND y)` | `(d = A OR d = B) AND ((d = A AND x) OR (d = B AND y))` |

Other findings name a fix in words: compare with constants instead of a subquery, add a condition on the column outside the `OR`, filter the table in a subquery instead of the outer join's `ON`.

## Measurements

The evidence for every rule above was gathered by `tools/partition_pruning_evidence.py` on 2026-11-28 with free dry runs, location `US`. A dry run plans a query and reports `totalBytesProcessed` without reading data or billing anything. Before any case, the script reads each table's partitioning from its metadata (`tables.get`, no rows read) and stops if it is not what the cases assume. It never runs a query. Rerun it with access to any billing project:

```
python tools/partition_pruning_evidence.py --project MY_BILLING_PROJECT --out measured.json
```

The two public tables:

| Table | Partitioning (metadata, 2026-11-28) | How a case is judged |
| --- | --- | --- |
| `bigquery-public-data.google_trends.top_terms` | `refresh_date` DATE, by day, no filter required; 35,730,660 rows, about 30 days | the case's bytes against a reference that reads the same columns and cannot prune (the filter moved into `COUNTIF`, or the column read whole): fewer bytes means pruned |
| `bigquery-public-data.wikipedia.pageviews_2024` | `datehour` TIMESTAMP, by day, `require_partition_filter`; 58,731,276,424 rows | accepted (with its bytes) or refused with "without a filter ... that can be used for partition elimination" |

On 2026-11-28 the detector agreed with BigQuery on all 91 cases. "The page says" is what Google's page states about the shape (`not said` when it is silent). "KumoSQL" is what the detector says today: `silent`, `warning` or `error` (`tests/test_partition_pruning.py` checks that this column is current and agrees with "measured"). Two cases need a note. In `d_join_table` the joined subquery reads a second column, so the case reads more than its reference: the 571,690,560 bytes are both whole columns, so nothing was pruned. In `d_scalar_subquery_table` the subquery reads one partition of the same table, and BigQuery's estimate still equals the whole column.

| case | table | filter or query | the page says | measured | bytes (reference) | KumoSQL |
| --- | --- | --- | --- | --- | --- | --- |
| `d_eq` | top_terms | `refresh_date = DATE '2026-11-20'` | prunes | prunes | 9,683,520 (285,845,280) | silent |
| `d_in` | top_terms | `refresh_date IN (DATE '2026-11-20', DATE '2026-11-21')` | prunes | prunes | 17,137,680 (285,845,280) | silent |
| `d_between` | top_terms | `refresh_date BETWEEN DATE '2026-11-20' AND DATE '2026-11-21'` | prunes | prunes | 17,137,680 (285,845,280) | silent |
| `d_string_literal` | top_terms | `refresh_date = '2026-11-20'` | prunes | prunes | 9,683,520 (285,845,280) | silent |
| `d_current_date` | top_terms | `refresh_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)` | prunes | prunes | 65,970,240 (285,845,280) | silent |
| `d_constant_left` | top_terms | `PARSE_DATE('%Y%m%d', '20261120') = refresh_date` | prunes | prunes | 9,683,520 (285,845,280) | silent |
| `d_neq` | top_terms | `refresh_date != DATE '2026-11-20'` | not said | prunes | 276,161,760 (285,845,280) | silent |
| `d_is_null` | top_terms | `refresh_date IS NULL` | not said | prunes | 0 (285,845,280) | silent |
| `t_eq` | pageviews_2024 | `datehour = TIMESTAMP '2024-03-30 12:00:00'` | prunes | prunes | 1,169,349,088 (accepted) | silent |
| `t_between` | pageviews_2024 | `datehour BETWEEN TIMESTAMP '2024-03-30 12:00:00' AND TIMESTAMP '2024-03-31 12:00:00'` | prunes | prunes | 2,364,205,680 (accepted) | silent |
| `t_current_timestamp` | pageviews_2024 | `datehour > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)` | prunes | prunes | 0 (accepted) | silent |
| `t_neq` | pageviews_2024 | `datehour != TIMESTAMP '2024-03-30 12:00:00'` | not said | prunes | 469,850,211,392 (accepted) | silent |
| `t_is_not_null` | pageviews_2024 | `datehour IS NOT NULL` | not said | prunes | 469,850,211,392 (accepted) | silent |
| `t_date` | pageviews_2024 | `DATE(datehour) = '2024-03-30'` | prunes | prunes | 1,222,717,184 (accepted) | silent |
| `t_cast_date` | pageviews_2024 | `CAST(datehour AS DATE) = '2024-03-30'` | prunes | prunes | 1,222,717,184 (accepted) | silent |
| `t_cast_date_in` | pageviews_2024 | `CAST(datehour AS DATE) IN ('2024-03-30', '2024-03-31')` | prunes | prunes | 2,474,329,112 (accepted) | silent |
| `t_extract_date` | pageviews_2024 | `EXTRACT(DATE FROM datehour) = '2024-03-30'` | prunes | prunes | 1,222,717,184 (accepted) | silent |
| `t_extract_year` | pageviews_2024 | `EXTRACT(YEAR FROM datehour) = 2024` | prunes | prunes | 469,850,211,392 (accepted) | silent |
| `t_timestamp_trunc` | pageviews_2024 | `TIMESTAMP_TRUNC(datehour, MONTH) = TIMESTAMP '2024-03-01'` | prunes | prunes | 39,628,409,248 (accepted) | silent |
| `t_timestamp_add` | pageviews_2024 | `TIMESTAMP_ADD(datehour, INTERVAL 1 DAY) = TIMESTAMP '2024-03-31 12:00:00'` | prunes | prunes | 1,169,349,088 (accepted) | silent |
| `t_timestamp_sub` | pageviews_2024 | `TIMESTAMP_SUB(datehour, INTERVAL 1 DAY) = TIMESTAMP '2024-03-30 12:00:00'` | prunes | prunes | 1,141,488,496 (accepted) | silent |
| `t_timestamp_diff` | pageviews_2024 | `TIMESTAMP_DIFF(datehour, TIMESTAMP '2024-03-30 12:00:00', DAY) = 0` | prunes | prunes | 3,647,722,344 (accepted) | silent |
| `t_format_f` | pageviews_2024 | `FORMAT_TIMESTAMP('%F', datehour) = '2024-03-30'` | prunes | prunes | 1,222,717,184 (accepted) | silent |
| `t_format_ymd` | pageviews_2024 | `FORMAT_TIMESTAMP('%Y%m%d', datehour) = '20240330'` | prunes | prunes | 1,222,717,184 (accepted) | silent |
| `d_date_trunc` | top_terms | `DATE_TRUNC(refresh_date, MONTH) = DATE '2026-11-01'` | prunes | prunes | 230,909,280 (285,845,280) | silent |
| `d_date_add` | top_terms | `DATE_ADD(refresh_date, INTERVAL 1 DAY) = DATE '2026-11-21'` | prunes | prunes | 9,683,520 (285,845,280) | silent |
| `d_date_sub` | top_terms | `DATE_SUB(refresh_date, INTERVAL 1 DAY) = DATE '2026-11-20'` | prunes | prunes | 7,454,160 (285,845,280) | silent |
| `d_date_diff` | top_terms | `DATE_DIFF(refresh_date, DATE '2026-11-20', DAY) = 0` | prunes | prunes | 9,683,520 (285,845,280) | silent |
| `d_cast_string_in` | top_terms | `CAST(refresh_date AS STRING) IN ('2026-11-20', '2026-11-21')` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_cast_string_eq` | top_terms | `CAST(refresh_date AS STRING) = '2026-11-20'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_safe_cast_string` | top_terms | `SAFE_CAST(refresh_date AS STRING) = '2026-11-20'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_format_date_ymd` | top_terms | `FORMAT_DATE('%Y%m%d', refresh_date) = '20261120'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_format_date_iso` | top_terms | `FORMAT_DATE('%Y-%m-%d', refresh_date) = '2026-11-20'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_extract_month` | top_terms | `EXTRACT(MONTH FROM refresh_date) = 11` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_unix_date` | top_terms | `UNIX_DATE(refresh_date) = 20412` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_cast_timestamp` | top_terms | `CAST(refresh_date AS TIMESTAMP) = TIMESTAMP '2026-11-20'` | not said | prunes | 9,683,520 (285,845,280) | silent |
| `d_timestamp_fn` | top_terms | `TIMESTAMP(refresh_date) = TIMESTAMP '2026-11-20'` | not said | prunes | 9,683,520 (285,845,280) | silent |
| `t_cast_string_in` | pageviews_2024 | `CAST(datehour AS STRING) IN ('2024-03-30 12:00:00+00')` | full_scan | full_scan | refused | error |
| `t_string_fn` | pageviews_2024 | `STRING(datehour) = '2024-03-30 12:00:00+00'` | full_scan | full_scan | refused | error |
| `t_format_hour` | pageviews_2024 | `FORMAT_TIMESTAMP('%Y-%m-%d %H', datehour) = '2024-03-30 12'` | full_scan | full_scan | refused | error |
| `t_extract_hour` | pageviews_2024 | `EXTRACT(HOUR FROM datehour) = 12` | full_scan | full_scan | refused | error |
| `t_unix_seconds` | pageviews_2024 | `UNIX_SECONDS(datehour) = 1711800000` | full_scan | full_scan | refused | error |
| `t_date_zone` | pageviews_2024 | `DATE(datehour, 'America/Los_Angeles') = '2024-03-30'` | not said | prunes | 2,364,205,680 (accepted) | silent |
| `t_datetime` | pageviews_2024 | `DATETIME(datehour) = DATETIME '2024-03-30 12:00:00'` | not said | full_scan | refused | error |
| `t_datetime_trunc` | pageviews_2024 | `DATETIME_TRUNC(DATETIME(datehour), DAY) = DATETIME '2024-03-30 00:00:00'` | not said | full_scan | refused | error |
| `t_plus_interval` | pageviews_2024 | `datehour + INTERVAL 1 DAY > TIMESTAMP '2024-12-30'` | full_scan | full_scan | refused | error |
| `d_plus_interval` | top_terms | `refresh_date + INTERVAL 1 DAY > DATETIME '2026-11-20 00:00:00'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_scalar_subquery` | top_terms | `refresh_date = (SELECT DATE '2026-11-20')` | full_scan | prunes | 9,683,520 (285,845,280) | silent |
| `d_in_subquery` | top_terms | `refresh_date IN (SELECT DATE '2026-11-20')` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `d_other_column` | top_terms | `refresh_date >= week` | full_scan | full_scan | 571,690,560 (571,690,560) | warning |
| `t_scalar_subquery` | pageviews_2024 | `datehour = (SELECT TIMESTAMP '2024-03-30 12:00:00')` | full_scan | prunes | 1,169,349,088 (accepted) | silent |
| `t_in_subquery` | pageviews_2024 | `datehour IN (SELECT TIMESTAMP '2024-03-30 12:00:00')` | full_scan | full_scan | refused | error |
| `d_or_other` | top_terms | `refresh_date = DATE '2026-11-20' OR term = 'x'` | full_scan | full_scan | 1,011,060,540 (1,011,060,540) | warning |
| `d_or_same` | top_terms | `refresh_date = DATE '2026-11-20' OR refresh_date = DATE '2026-11-21'` | not said | prunes | 17,137,680 (285,845,280) | silent |
| `t_or_other` | pageviews_2024 | `datehour = TIMESTAMP '2024-03-30 12:00:00' OR wiki = 'en'` | full_scan | full_scan | refused | error |
| `t_or_same` | pageviews_2024 | `datehour = TIMESTAMP '2024-03-30 12:00:00' OR datehour = TIMESTAMP '2024-03-31 12:00:00'` | not said | prunes | 2,310,837,584 (accepted) | silent |
| `t_or_both_sides` | pageviews_2024 | `(datehour = TIMESTAMP '2024-03-30 12:00:00' AND wiki = 'en') OR (datehour = TIMESTAMP '2024-03-31 12:00:00'...` | not said | full_scan | refused | error |
| `d_other_only` | top_terms | `week = DATE '2026-11-16'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `t_other_only` | pageviews_2024 | `wiki = 'en'` | full_scan | full_scan | refused | error |
| `t_and_other` | pageviews_2024 | `datehour = TIMESTAMP '2024-03-30 12:00:00' AND wiki = 'en'` | prunes | prunes | 246,549,396 (accepted) | silent |
| `d_and_cast` | top_terms | `refresh_date = DATE '2026-11-20' AND CAST(refresh_date AS STRING) LIKE '2026%'` | prunes | prunes | 9,683,520 (285,845,280) | silent |
| `d_cte_renamed` | top_terms | `WITH x AS (SELECT refresh_date AS day, term FROM t) SELECT COUNT(term) FROM x WHERE day = DATE '2026-11-20'` | not said | prunes | 32,021,640 (1,011,060,540) | silent |
| `d_group_by` | top_terms | `SELECT COUNT(*) FROM (SELECT refresh_date, COUNT(*) AS c FROM t GROUP BY refresh_date) WHERE refresh_date =...` | not said | prunes | 9,683,520 (285,845,280) | silent |
| `t_cte_renamed` | pageviews_2024 | `WITH x AS (SELECT datehour AS h, views FROM t) SELECT SUM(views) FROM x WHERE h = TIMESTAMP '2024-03-30 12:...` | not said | prunes | 2,338,698,176 (accepted) | silent |
| `t_group_by` | pageviews_2024 | `SELECT COUNT(*) FROM (SELECT datehour, SUM(views) AS v FROM t GROUP BY datehour) WHERE datehour = TIMESTAMP...` | not said | prunes | 1,169,349,088 (accepted) | silent |
| `t_window` | pageviews_2024 | `SELECT COUNT(*) FROM (SELECT datehour, SUM(views) OVER (PARTITION BY wiki) AS v FROM t) WHERE datehour = TI...` | not said | prunes | 1,169,349,088 (accepted) | silent |
| `t_join_constant` | pageviews_2024 | `SELECT COUNT(*) FROM t AS p JOIN (SELECT TIMESTAMP '2024-03-30 12:00:00' AS h) AS c ON p.datehour = c.h` | not said | prunes | 1,169,349,088 (accepted) | silent |
| `d_join_constant` | top_terms | `SELECT COUNT(*) FROM t AS p JOIN (SELECT DATE '2026-11-20' AS d) AS c ON p.refresh_date = c.d` | not said | prunes | 9,683,520 (285,845,280) | silent |
| `d_extract_year_none` | top_terms | `EXTRACT(YEAR FROM refresh_date) = 2020` | prunes | prunes | 0 (285,845,280) | silent |
| `t_extract_year_none` | pageviews_2024 | `EXTRACT(YEAR FROM datehour) = 2023` | prunes | prunes | 0 (accepted) | silent |
| `d_scalar_subquery_table` | top_terms | `refresh_date = (SELECT MAX(refresh_date) FROM t WHERE refresh_date = DATE '2026-11-20')` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |
| `t_scalar_subquery_table` | pageviews_2024 | `datehour = (SELECT TIMESTAMP(MAX(refresh_date)) FROM top_terms WHERE refresh_date = DATE '2026-11-20')` | full_scan | full_scan | refused | error |
| `d_or_both_sides` | top_terms | `(refresh_date = DATE '2026-11-20' AND term = 'x') OR (refresh_date = DATE '2026-11-21' AND term = 'y')` | not said | prunes | 59,974,740 (1,011,060,540) | silent |
| `t_in_unnest` | pageviews_2024 | `datehour IN UNNEST([TIMESTAMP '2024-03-30 12:00:00', TIMESTAMP '2024-03-31 12:00:00'])` | not said | prunes | 2,310,837,584 (accepted) | silent |
| `t_not` | pageviews_2024 | `NOT (datehour < TIMESTAMP '2024-12-30')` | not said | prunes | 2,527,047,696 (accepted) | silent |
| `t_ifnull` | pageviews_2024 | `IFNULL(datehour, TIMESTAMP '2024-03-30 12:00:00') = TIMESTAMP '2024-03-30 12:00:00'` | not said | full_scan | refused | error |
| `t_safe_cast_date` | pageviews_2024 | `SAFE_CAST(datehour AS DATE) = '2024-03-30'` | not said | full_scan | refused | error |
| `t_string_of_date` | pageviews_2024 | `CAST(CAST(datehour AS DATE) AS STRING) = '2024-03-30'` | full_scan | full_scan | refused | error |
| `t_join_table` | pageviews_2024 | `SELECT COUNT(*) FROM t AS p JOIN (SELECT TIMESTAMP(refresh_date) AS h FROM top_terms WHERE refresh_date = D...` | not said | full_scan | refused | error |
| `t_join_transitive` | pageviews_2024 | `SELECT COUNT(*) FROM t AS p JOIN (SELECT TIMESTAMP(refresh_date) AS h FROM top_terms) AS c ON p.datehour = ...` | not said | full_scan | refused | error |
| `t_left_join_on` | pageviews_2024 | `SELECT COUNT(*) FROM (SELECT 1 AS k) AS a LEFT JOIN t AS p ON p.datehour = TIMESTAMP '2024-03-30 12:00:00'` | not said | full_scan | refused | error |
| `t_window_used` | pageviews_2024 | `SELECT SUM(v) FROM (SELECT datehour, SUM(views) OVER (PARTITION BY wiki) AS v FROM t) WHERE datehour = TIME...` | not said | full_scan | refused | error |
| `t_window_by_column` | pageviews_2024 | `SELECT SUM(v) FROM (SELECT datehour, SUM(views) OVER (PARTITION BY datehour) AS v FROM t) WHERE datehour = ...` | not said | prunes | 2,338,698,176 (accepted) | silent |
| `t_having` | pageviews_2024 | `SELECT datehour, COUNT(*) FROM t GROUP BY datehour HAVING datehour = TIMESTAMP '2024-03-30 12:00:00'` | not said | prunes | 1,169,349,088 (accepted) | silent |
| `t_inner_join_on` | pageviews_2024 | `SELECT COUNT(*) FROM t AS p JOIN (SELECT 1 AS k) AS a ON a.k = 1 AND p.datehour = TIMESTAMP '2024-03-30 12:...` | not said | prunes | 1,169,349,088 (accepted) | silent |
| `d_join_table` | top_terms | `SELECT COUNT(*) FROM t AS p JOIN (SELECT week AS d FROM t WHERE refresh_date = DATE '2026-11-20') AS c ON p...` | not said | full_scan | 571,690,560 (285,845,280) | warning |
| `d_left_join_on` | top_terms | `SELECT COUNT(*) FROM (SELECT 1 AS k) AS a LEFT JOIN t AS p ON p.refresh_date = DATE '2026-11-20'` | not said | full_scan | 285,845,280 (285,845,280) | warning |
| `t_plus_interval_rewritten` | pageviews_2024 | `datehour > TIMESTAMP_SUB(TIMESTAMP '2024-12-30', INTERVAL 1 DAY)` | prunes | prunes | 3,856,753,552 (accepted) | silent |
| `d_plus_interval_rewritten` | top_terms | `refresh_date > DATETIME_SUB(DATETIME '2026-11-20 00:00:00', INTERVAL 1 DAY)` | not said | prunes | 75,653,760 (285,845,280) | silent |
| `d_format_month_rewritten` | top_terms | `DATE_TRUNC(refresh_date, MONTH) = DATE '2026-11-01'` | prunes | prunes | 230,909,280 (285,845,280) | silent |
| `d_format_month` | top_terms | `FORMAT_DATE('%Y-%m', refresh_date) = '2026-11'` | full_scan | full_scan | 285,845,280 (285,845,280) | warning |

## Limits

- **Two tables, one day, one location.** The measurements are from two public tables in `US` on 2026-11-28, partitioned by day on a `DATE` and a `TIMESTAMP` column. Hourly, monthly and yearly partitioning, `DATETIME` columns, integer ranges and ingestion-time tables were not measured. Their rules (`DATETIME_DIFF`, the integer-range rule, `_PARTITIONTIME`) rest on the page alone, and a `DATETIME_TRUNC` on a `DATETIME` column stays `unknown`.
- **Dry runs estimate.** `totalBytesProcessed` is BigQuery's plan-time estimate. A join or subquery that BigQuery prunes at run time would still show as a full read here, and it would still be refused on a table that requires a filter.
- **The page can change.** Google changes BigQuery. Rerun the script after a change in BigQuery's behaviour or the page. A case that stops agreeing shows in its last line, and the docs table must be refreshed from its output.
- **Text, not a plan.** The detector reads the query. A filter it cannot place (an unqualified column with two tables in scope, a function the page does not list and no dry run measured, a user-defined function) is listed under `unknown`, never reported. The pushdown rule silences more than BigQuery does: any outer filter on a CTE or subquery column counts.
- **On real projects.** On the 375 models of the 14 public Dataform projects in `tests/fixtures/bq_corpora` (31 of them partitioned by their config), the project check finds one problem: `wintermi-bqe`'s view `v_project_storage` keeps the latest snapshot with `snapshot_date IN (SELECT MAX(snapshot_date) FROM ...)`, which reads every partition of `project_storage` (measured: `IN (SELECT ...)` never prunes). A first version also reported 21 filters in `security-analytics` such as `day >= DATE_SUB(CURRENT_DATE, INTERVAL ${vars.days} DAY)`. KumoSQL masks a Dataform `${...}` expression before parsing, and the detector took the mask for a column. A masked expression is now unknown, so those 21 are no longer reported. The sweep is not a test, because loading the projects takes minutes.
- **Only what KumoSQL knows.** A table whose partitioning is not in the snapshot, the config or saved metadata is not judged at all, and a stale snapshot gives stale partitioning (its age is in `check_query`'s `unknown`).
- **No real queries.** Nothing here runs a query or reads table data, and the tests use a fake dry-run transport.
