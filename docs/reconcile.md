# Reconciling two tables by key

`python -m kumosql reconcile` answers "do these two tables hold the same data, and where they don't, do we know why?". It joins a left and a right table on a key and puts every row in one of three groups:

- **matched**: the key is on both sides and every compared column agrees;
- **explained**: the row differs (its key is only on one side, or a column differs) and a named *known-difference rule* says why;
- **unexplained**: the row differs and no rule covers it. These are the rows to look at.

A typical use is a migration: `orders_v2` replaces `orders_v1`, and the team has already agreed that test orders were dropped and refunds are now zeroed. Those agreements become rules; after that, a reconciliation reports only the differences nobody has explained yet. `python -m kumosql rules check` later re-runs the rules on current data and flags the ones that have gone stale.

Everything is one read-only SQL query, so on BigQuery it is a single job, dry-run first and capped in bytes billed, like every other query KumoSQL runs. It also runs on DuckDB.

## An example

```
python -m kumosql reconcile --left my-proj.sales.orders_v1 --right my-proj.sales.orders_v2 \
    --key order_id --map crosswalk.yml --rules rules.yml --project my-billing-project
```

`crosswalk.yml` says the amount column was renamed:

```yaml
- left: orders_v1
  right: orders_v2
  columns:
    - [amount, amount_usd]
```

`rules.yml` holds the two agreed differences:

```yaml
rules:
  - name: refunds_zeroed
    predicate: "l.status = 'refunded' AND r.amount_usd = 0"
    verified_at: 2026-11-01
    verified_count: 1
    description: The new table zeroes refunded amounts.
  - name: test_orders
    predicate: "status = 'test'"
    verified_at: 2026-11-01
    verified_count: 1
```

On the seven-row and six-row tables of `tests/test_reconcile.py` it prints (shortened):

```json
{
  "keys": ["order_id"],
  "compared_columns": ["status", "amount", "note"],
  "column_map": {"amount": "amount_usd"},
  "left_rows": 7, "right_rows": 6,
  "keys_only_left": 2, "keys_only_right": 1, "keys_both": 5,
  "matched_rows": 3, "mismatched_rows": 5,
  "explained_rows": 2, "unexplained_rows": 3,
  "unexplained_by_kind": {"only_left": 1, "only_right": 1, "value_mismatch": 1},
  "column_mismatches": {"amount": 1, "status": 1},
  "unexplained_column_mismatches": {"status": 1},
  "rules": [{"name": "refunds_zeroed", "verified_at": "2026-11-01", "matched_rows": 1},
            {"name": "test_orders", "verified_at": "2026-11-01", "matched_rows": 1}],
  "samples_included": false, "samples": null
}
```

Read it as: of 8 joined rows, 3 match, 2 differ for an agreed reason, and 3 differ for no known reason: one order only in `orders_v1`, one only in `orders_v2`, and one whose `status` changed. The exit status is 1 because something is unexplained; it is 0 when every row matches or is explained, 2 when the inputs are wrong, and 3 when BigQuery's dry run is over the byte cap.

## Inputs

| Option | Meaning |
| --- | --- |
| `--left`, `--right` | A table (`project.dataset.table`, or `dataset.table` in the billing project on BigQuery; `table` or `schema.table` on DuckDB), a path to a `.sql` file, or an inline `SELECT ...`/`WITH ...` query. A query must be one read-only `SELECT`; it is checked before anything runs |
| `--key a,b` | The key columns. Write `left_col=right_col` when the name differs on the right; a crosswalk mapping also applies to keys |
| `--ignore c,d` | Columns not to compare, on either side, case ignored. A key column cannot be ignored |
| `--map FILE` or `--map saved` | A crosswalk from left to right column names (below), or the saved equivalences (`python -m kumosql equivalence list`) that link these two tables, in either direction |
| `--rules FILE` | Known-difference rules (below), tried in the order listed |
| `--include-samples`, `--sample-limit N` | Return a few keys of unexplained rows (below); off by default |
| `--duckdb FILE` | Run on a DuckDB database file, opened read-only; `--duckdb :memory:` compares queries over files, for example `--left "SELECT * FROM read_csv_auto('old.csv')"` |
| `--project`, `--max-bytes` | BigQuery billing project and byte cap (defaults: Settings → BigQuery projects and Settings → Scopes) |
| `--dry-run` | Print the query and, on BigQuery, the dry run's byte estimate; run nothing |
| `--sql-only` | Print the query; run nothing |
| `-o FILE` | Also write the printed text to this file |

Columns are matched by name, case ignored, after the crosswalk. Columns only on one side are listed under `left_only_columns` and `right_only_columns` and not compared. Compared columns use `IS DISTINCT FROM`, so two NULLs agree and a NULL against a value differs. On BigQuery, arrays, STRUCTs, JSON and GEOGRAPHY values are compared as their `TO_JSON_STRING` text, since BigQuery cannot compare them directly.

### Crosswalk files

A crosswalk is YAML or JSON in one of three shapes:

```yaml
# entries for a pair of tables (an entry for other tables is skipped; the pair the other way round is reversed)
- left: orders_v1
  right: orders_v2
  columns:
    - [amount, amount_usd]
    - {left: legacy_flag, right: null, status: missing, explanation: legacy_flag_dropped}
```

```yaml
# column links on their own
- {left: amount, right: amount_usd}
```

```yaml
# a plain mapping; null means the column has no partner
amount: amount_usd
legacy_flag: null
```

A column whose `status` is `missing`, or whose partner is null, is skipped: it is listed under `skipped_columns` (with its `explanation`) instead of being reported as only on one side. `explanation` names the rule that accounts for a column; the result lists these under `column_explanations`, and a note says when one names a rule that is not in `--rules`. Table names match when the shorter is a suffix of the longer (`orders_v1` matches `proj.sales.orders_v1`). When `--left` or `--right` is a query, entries are applied without checking table names, and a note says so.

## Known-difference rules

A rules file is YAML or JSON: a list of rules, or a mapping with one key, `rules`.

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | The rule's identifier, unique (case ignored). A crosswalk `explanation` refers to it |
| `predicate` (or `sql`, `where`) | yes | One SQL condition over the joined row (BigQuery syntax) |
| `verified_at` (or `verified`, `verified_date`) | yes | The date someone checked the rule, `YYYY-MM-DD` |
| `description` | no | Why the rows differ |
| `verified_count` | no | How many rows the rule explained when it was verified; `rules check` uses it |
| `max_growth_ratio` | no | This rule's own growth factor for `rules check` (at least 1) |
| `columns` | no | The columns the rule is about, as a hint for readers |

In a predicate, `l.col` is the left row's value and `r.col` the right row's. On a row that is only on one side, the other side's columns are NULL, so `r.order_id IS NULL` means "only on the left". A bare `col` matches when the left row satisfies the predicate or the right row does; a side that has no row for the key does not count, and a side without that column is not tried. Crosswalk names work on either side: with `amount: amount_usd`, both `r.amount` and `r.amount_usd` name the right column. A predicate that comes out NULL does not match.

Rules are tried in the order listed and the **first one that holds explains the row**, so put narrow rules before broad ones. Each rule's `matched_rows` counts the rows it explained in that order.

Predicates are checked before anything runs, with sqlglot in the BigQuery dialect: exactly one condition, no semicolon outside a string, no subquery or statement (`SELECT`, `DROP`, `DELETE`, `CALL` ...), no aggregate or window function, no query parameter, no qualifier other than `l` and `r`, and no nested field (`l.address.city`). The query uses the predicate re-written from its parse tree, never the text as typed.

## Sample keys

Key values can be sensitive, so samples are **off by default**: without `--include-samples` the query selects no key value at all and the result has `"samples": null`. With it, the result lists up to `--sample-limit` keys (default 5, at most 100) per group, **of unexplained rows only**; a row a rule explains is never sampled:

```json
"samples": {
  "only_left": [{"order_id": 7}],
  "only_right": [{"order_id": 8}],
  "value_mismatch": [{"order_id": 5}],
  "by_column": {"status": [{"order_id": 5}]}
}
```

Keys are listed in key order, one object per row, named by the left key columns.

## Keys that are NULL or repeat

A NULL in a key never matches (SQL equality), so such a row counts as only on its side. `null_keys` counts them, and a note says so.

A key that is not unique joins row by row: two left rows and one right row with key 7 make two joined pairs. The counts of keys on both sides, matched rows and mismatched rows are then counts of joined pairs, not of rows. `duplicate_keys` counts the key values that repeat on each side, and a note says the counts are joined pairs. `left_rows` and `right_rows` are always the rows of each input.

## Checking the rules later

```
python -m kumosql rules check --rules rules.yml --left my-proj.sales.orders_v1 --right my-proj.sales.orders_v2 \
    --key order_id --map crosswalk.yml
python -m kumosql rules list --rules rules.yml
```

`rules check` runs the same comparison, again as one query, and reports each rule:

```json
{"name": "refunds_zeroed", "status": "ok", "flags": [], "verified_at": "2026-11-01",
 "verified_count": 1, "current_count": 1, "standalone_count": 1, "growth_limit": 11}
```

`current_count` is what the rule explains now, in first-match order. `standalone_count` is how many differing rows its predicate holds for on its own, whatever comes first. A rule is flagged:

- `no_longer_explains` when it explains no row. If an earlier rule takes all its rows, a `note` says so;
- `grew_significantly` when it explains more than `max(verified_count × factor, verified_count + min_growth)` rows, where the factor is the rule's `max_growth_ratio` or `--growth-factor` (default 2) and `--min-growth` defaults to 10. A rule verified at 0 rows is flagged as soon as it explains any. A rule without `verified_count` is only checked for staleness.

The exit status is 0 when every rule is `ok`, 1 when any is flagged, 2 on an input error and 3 over the byte cap. `rules list` prints the rules as JSON.

## Rules from "equivalent except when P"

When two queries are not equivalent, `python -m kumosql prove-sql-equivalent left.sql right.sql --explain-difference` can print the rows they disagree on as a verified predicate `P` ([difference explanations](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/difference-explanations.md)). `--emit-rule` also prints `P` as a rule in the format above, `--rule-name NAME` names it, and `--rule-output rules.yml` writes it to a `.yml`, `.yaml` or `.json` file instead. Each of the three implies `--explain-difference`.

```yaml
# Known-difference rules for python -m kumosql reconcile and rules check (docs/reconcile.md)
rules:
  - name: "status_null"
    predicate: "status IS NULL"
    verified_at: "2026-11-28"
    description: "From python -m kumosql prove-sql-equivalent --explain-difference: ..."
    columns: ["status"]
```

The prover's predicate reads one table's columns; their table qualifiers are dropped, so in a reconciliation the rule matches a row when either side's row satisfies `P`. `verified_at` is the day it was emitted, and `verified_count` is left empty because the prover checked query text, not rows: run the reconciliation once and record the count. In Python, `kumosql.reconcile_rules.rule_from_except_when(payload, name=..., verified_at=...)` turns any `except_when` result into a `KnownDiffRule`, and `dump_rules` / `save_rules` write it. When no predicate is found, or the pair is proven equivalent, no rule is written and a line on standard error says why. The difference search is still being built in the prover repository and may return no predicate for pairs it will cover later.

## How it runs

`kumosql.reconcile.reconcile(left, right, keys, executor, ignore=, crosswalk=, rules=, include_samples=, sample_limit=)` returns a `ReconcileResult` (`to_json()` is the printed form), `check_rules(...)` a `RulesCheckResult`, and `build_reconcile_sql(...)` / `plan_reconcile(...)` the query without running it. The executors follow [data profiling](data-profiling.md#how-it-runs):

- `DuckDBExecutor(connection)` reads each side's columns with `DESCRIBE` and runs the query on DuckDB.
- `BigQueryExecutor(project=None, max_bytes=None)` reads each side's columns with a dry run (free, nothing is processed), then runs the comparison through the same path as saved data sources: checked locally as one read-only `SELECT`, dry-run first and refused when the estimate is over the cap, run with `maximumBytesBilled`, in the billing project, never one guessed from a table name. The job location is the one in Settings; there is no per-command `--location`.

The query reads each side in a `WITH` clause, joins them once with `FULL OUTER JOIN`, computes one flag per compared column and per rule, sorts each row with `CASE WHEN rule_1 THEN 1 WHEN rule_2 THEN 2 ... ELSE 0 END`, and aggregates everything with `COUNTIF`. Each side is also counted on its own (rows, and keys that repeat), so its table is read twice within the one job; the dry run's estimate is the number to trust for cost.

Every generated query passes the same local read-only check as every other query KumoSQL runs (`kumosql.sql_validation.validate_readonly_query`), on DuckDB as well as BigQuery, including its 20,000-character limit. A comparison too wide for that limit is refused before anything runs, with a message to compare fewer columns (`--ignore`), leave samples off or shorten the `--left`/`--right` SQL. It is never sent unchecked. Measured with 8-character column names, each compared column adds about 185 characters (about 340 with samples) to a base of about 1,500, so roughly 100 columns fit, or about 50 with samples; longer names fit fewer.

## Limits

- The result counts rows and, with samples, shows keys; it never shows the differing values themselves. Use the keys to look the rows up.
- Equality is SQL equality: `1.0` and `1` agree, trailing spaces and letter case count, and floating-point values that differ in the last digit differ. A rule such as `ABS(l.amount - r.amount) < 0.01` is the way to allow a tolerance.
- A rule's predicate holds or not per joined row; it cannot compare a row with other rows (no aggregates or subqueries).
- Very wide tables hit the 20,000-character limit described above; compare them in groups of columns with `--ignore`.
