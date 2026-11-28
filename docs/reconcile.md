# Reconciling two tables by key

`python -m kumosql reconcile` answers "do these two tables hold the same data, and where they don't, do we know why?". It joins a left and a right table on a key and puts every row in one of three groups:

- **matched**: the key is on both sides and every compared column agrees;
- **explained**: the row differs (its key is only on one side, or a column differs) and a named *known-difference rule* says why;
- **unexplained**: the row differs and no rule covers it. These are the rows to look at.

A typical use is a migration: `orders_v2` replaces `orders_v1`, and the team has already agreed that test orders were dropped and refunds are now zeroed. Those agreements become rules; after that, a reconciliation reports only the differences nobody has explained yet. `python -m kumosql rules check` later re-runs the rules on current data and flags the ones that have gone stale. Before either, [`python -m kumosql key-check`](#checking-a-key-key-check) says whether the key is a key: which values repeat, which are NULL, and what a proposed join does.

Floating-point columns agree within a small tolerance, and STRUCT and array columns are compared as whole values on both engines (see [how columns are compared](#how-columns-are-compared)), so the usual hand-written mistakes (float equality, `DISTINCT` on a STRUCT, a reserved word as an alias, a `FULL OUTER JOIN` without an equality condition) are not in the generated SQL. Tests parse every generated query and check for each of them.

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
  "float_tolerance": {"absolute": 1e-12, "relative": 1e-09},
  "comparisons": {"status": {"method": "exact"},
                  "amount": {"method": "tolerance", "absolute": 1e-12, "relative": 1e-09, "source": "default"},
                  "note": {"method": "exact"}},
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

Read it as: of 8 joined rows, 3 match, 2 differ for an agreed reason, and 3 differ for no known reason: one order only in `orders_v1`, one only in `orders_v2`, and one whose `status` changed. `comparisons` says that `amount`, a `DOUBLE`, was matched within the default float tolerance rather than exactly. The exit status is 1 because something is unexplained; it is 0 when every row matches or is explained, 2 when the inputs are wrong, and 3 when BigQuery's dry run is over the byte cap.

## Inputs

| Option | Meaning |
| --- | --- |
| `--left`, `--right` | A table (`project.dataset.table`, or `dataset.table` in the billing project on BigQuery; `table` or `schema.table` on DuckDB), a path to a `.sql` file, or an inline `SELECT ...`/`WITH ...` query. A query must be one read-only `SELECT`; it is checked before anything runs |
| `--key a,b` | The key columns. Write `left_col=right_col` when the name differs on the right; a crosswalk mapping also applies to keys |
| `--ignore c,d` | Columns not to compare, on either side, case ignored; either name of a crosswalk pair ignores the pair. A key column cannot be ignored |
| `--map FILE` or `--map saved` | A crosswalk from left to right column names (below), or the saved equivalences (`python -m kumosql equivalence list`) that link these two tables, in either direction |
| `--rules FILE` | Known-difference rules (below), tried in the order listed |
| `--float-tolerance ABS`, `--float-relative REL` | How close two floating-point values must be to agree (defaults `1e-12` and `1e-9`; [below](#floating-point-columns)). `0` and `0` compare floats exactly |
| `--tolerance COL=ABS[:REL]` | One column's own tolerance, replacing the float defaults for it: `price=0.01`, `rate=0:1e-6`, or `score=exact`. It also gives a `NUMERIC` or integer column a tolerance. Comma separated or repeated |
| `--include-samples`, `--sample-limit N` | Return a few keys of unexplained rows (below); off by default |
| `--duckdb FILE` | Run on a DuckDB database file, opened read-only; `--duckdb :memory:` compares queries over files, for example `--left "SELECT * FROM read_csv_auto('old.csv')"` |
| `--project`, `--max-bytes` | BigQuery billing project and byte cap (defaults: Settings → BigQuery projects and Settings → Scopes) |
| `--dry-run` | Print the query and, on BigQuery, the dry run's byte estimate; run nothing |
| `--sql-only` | Print the query; run nothing |
| `-o FILE` | Also write the printed text to this file |

Columns are matched by name, case ignored, after the crosswalk. Columns only on one side are listed under `left_only_columns` and `right_only_columns` and not compared. How each compared column was compared is in the result's `comparisons` ([below](#how-columns-are-compared)).

## How columns are compared

Every comparison is NULL-safe: two NULLs agree, and a NULL against a value differs. Beyond that, the column's type decides, and `comparisons` records the outcome per column, so a match is never mistaken for an exact one:

| Column type | Agrees when | `comparisons` entry |
| --- | --- | --- |
| text, integers, `NUMERIC`/`BIGNUMERIC`/`DECIMAL`, dates, booleans | equal (`IS NOT DISTINCT FROM`) | `{"method": "exact"}` |
| `FLOAT64` (BigQuery); `FLOAT`, `DOUBLE`, `REAL` (DuckDB) | within the float tolerance | `{"method": "tolerance", "absolute": 1e-12, "relative": 1e-09, "source": "default"}` |
| a column given `--tolerance` | within its own tolerance | the same, with `"source": "--tolerance"` |
| STRUCT, array, JSON, GEOGRAPHY, map | the whole value is equal | `{"method": "whole_value", "fields": "same", "floats_inside": false}` |

### Floating-point columns

Two floats `a` and `b` agree when `|a - b| <= max(ABS, REL × max(|a|, |b|))`. The defaults are `REL = 1e-9` and `ABS = 1e-12`. That allows the noise in the last few digits that summing in another order produces (`0.1 + 0.2` against `0.3`, or `1e9 + 0.5` against `1e9`, or `1e-13` against `0`). A difference in about the ninth significant digit, or bigger, still counts. The special values follow fixed rules on both engines:

- NaN agrees with NaN and differs from every number. BigQuery's own `NaN = NaN` is false and DuckDB's is true; the generated SQL tests `IS_NAN`/`isnan` explicitly, so both engines answer the same.
- An infinity agrees only with the same infinity: `+inf` against `-inf`, or against any finite number, differs. No tolerance makes them close.
- `-0.0` agrees with `0.0`. Two huge values of opposite sign, whose difference overflows to infinity, differ.

`--float-tolerance` and `--float-relative` change the defaults for every float column; `0` and `0` compare floats exactly (NaN still agrees with NaN). `--tolerance` sets one column's own tolerance and replaces both defaults for it: `price=0.01` agrees within a cent and nothing else, `rate=0:1e-6` within one part in a million, `price=0.01:1e-6` within either, and `score=exact` turns the tolerance off for that column. The column can be named by its left or right name. `NUMERIC`, `BIGNUMERIC`, `DECIMAL` and integer columns stay exact unless `--tolerance` names them; `amount=0.005` gives a `NUMERIC` amount a half-cent tolerance. A `--tolerance` on a text column, a STRUCT or array, a key column or a column neither side has is refused; on an ignored column it only adds a note.

The tolerance never applies to the key: a floating-point key joins only exactly equal values, and a note says so.

### STRUCT and array columns

A STRUCT or an array is compared as one whole value, exactly. Field by field, a NULL field agrees with a NULL field. A NULL STRUCT differs from a STRUCT whose fields are all NULL, and an empty array differs from a NULL one. Arrays are compared element by element in order, so `[1, 2]` and `[2, 1]` differ. Both engines give the same answer:

- **DuckDB** compares nested values itself (`IS DISTINCT FROM`). STRUCT fields are matched by name, so the same fields declared in another order agree.
- **BigQuery** cannot compare arrays, or STRUCTs that hold them, at all, so it compares each side's `TO_JSON_STRING` text. That text keeps the declared field order. When the right side declares the same fields in another order (or another letter case), the right side is rebuilt in the left side's order first, at every level and inside arrays (`STRUCT(r.s.x AS x, r.s.y AS y)`, `ARRAY(SELECT AS STRUCT ... ORDER BY offset)`), so the two texts line up. `"fields": "reordered"` in `comparisons` says this happened. A NULL stays NULL through the rebuild.

`"fields": "differ"` means the two sides' STRUCT fields differ in name or nesting. No row with a value on both sides can match then, and a note says so; compare the fields themselves with `--left`/`--right` queries. `"floats_inside": true` marks a STRUCT or array that holds floating-point values. **The float tolerance does not reach inside it**: those values are part of the whole value and compared exactly, and a note names the columns. To compare them within a tolerance, select the fields as columns of their own in a `--left`/`--right` query.

No generated query uses `DISTINCT` (BigQuery can group neither a STRUCT nor an array). A STRUCT, array, JSON or GEOGRAPHY column cannot be a key on either engine. It is refused before anything runs, with a hint to key on its fields.

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

A column whose `status` is `missing`, or whose partner is null, is skipped on each side the link names: it is listed under `skipped_columns` (with its `explanation`) instead of being reported as only on one side. `explanation` names the rule that accounts for a column; the result lists these under `column_explanations`, and a note says when one names a rule that is not in `--rules`. Table names match when the shorter is a suffix of the longer (`orders_v1` matches `proj.sales.orders_v1`). When `--left` or `--right` is a query, entries are applied without checking table names, and a note says so.

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

A key that is not unique joins row by row: two left rows and one right row with key 7 make two joined pairs. The counts of keys on both sides, matched rows and mismatched rows are then counts of joined pairs, not of rows. `duplicate_keys` counts the key values that repeat on each side, and a note says the counts are joined pairs. `left_rows` and `right_rows` are always the rows of each input. Run `key-check` (next) first to see which values repeat.

## Checking a key: `key-check`

`python -m kumosql key-check TABLE --key a,b` answers "is this a key?" before a reconciliation or a join relies on it. With `--join OTHER --on a=x,b=y` it also answers "what does joining OTHER on it do?". It is one read-only query, a single BigQuery job under the same dry run and byte cap as `reconcile` and profiling.

```
python -m kumosql key-check my-proj.sales.orders --key customer_id \
    --join my-proj.sales.customers --on customer_id=id --project my-billing-project
```

On the nine orders and six customers of `tests/test_key_check.py` it prints (shortened):

```json
{
  "table": "orders", "key": ["customer_id"],
  "rows": 9, "distinct_keys": 5, "unique": false,
  "duplicates": {"groups": 2, "rows": 5, "extra_rows": 3, "max_rows_per_key": 3,
                 "largest_groups": [{"rows": 3}, {"rows": 2}]},
  "nulls": {"rows": 1, "by_column": {"customer_id": 1}},
  "join": {
    "other": "customers", "on": {"customer_id": "id"}, "cardinality": "N:M",
    "other_rows": 6, "other_null_key_rows": 1, "other_distinct_keys": 4,
    "other_duplicate_groups": 1, "other_max_rows_per_key": 2, "matched_keys": 3,
    "unmatched": {"rows": 5, "keys": 2, "other_rows": 2, "other_keys": 1},
    "fan_out": {"max_rows_per_key": 2, "keys_with_several_rows": 1, "rows_multiplied": 1,
                "inner_join_rows": 5, "left_join_rows": 10, "added_rows": 1}
  },
  "ok": false, "problems": ["duplicate_keys", "null_keys", "join_fans_out"],
  "samples_included": false
}
```

Read it as: two customer ids repeat, covering 5 of the 9 rows (3 more rows than one per key), and the most rows for one id is 3. One row has a NULL key. Joining `customers` on `customer_id = id` finds 3 shared ids. One of them has 2 customer rows, so a join repeats 1 order and a `LEFT JOIN` returns 10 rows from 9. 5 orders and 2 customers find no partner.

| Field | Meaning |
| --- | --- |
| `rows`, `distinct_keys` | Rows of TABLE, and distinct key values with no NULL in them |
| `duplicates.groups`, `.rows`, `.extra_rows` | Key values that repeat, the rows they cover, and the rows beyond one per key |
| `duplicates.largest_groups` | The `--top` (default 5, at most 100) largest groups, largest first, ties in key order: sizes always, key values only with `--include-samples` |
| `nulls.rows`, `nulls.by_column` | Rows with a NULL in any key column, and the NULLs per key column. A row with a NULL key never joins and never counts as a duplicate |
| `join.unmatched` | TABLE rows and key values with no partner in OTHER (`rows` includes NULL keys), and the same for OTHER |
| `join.fan_out` | Among the shared key values: the most OTHER rows for one of them, how many have more than one, the TABLE rows those hold, and the rows an inner and a left join return. `added_rows` is the left join's rows beyond TABLE's |
| `join.cardinality` | `1:1`, `1:N`, `N:1` or `N:M`, judged on whole tables as `join_profile` does |
| `problems` | `duplicate_keys`, `null_keys` and, with `--join`, `join_fans_out` (some shared key has more than one OTHER row) |

TABLE and `--join` take the same forms as `--left` and `--right`: a table, a `.sql` file or a `SELECT` query. `--on` maps each key column to OTHER's column; a key column with the same name in OTHER needs no entry. Key values are table data, so, as in `reconcile`, `--include-samples` is off by default and without it the query selects no key value at all. `--dry-run` prints the query and BigQuery's byte estimate, and `--sql-only` prints the query; `-o FILE` also writes the output. A STRUCT, array, JSON or GEOGRAPHY key column is refused, and a floating-point one is flagged in `notes`.

The exit status is 0 when the key is unique and never NULL (and, with `--join`, the join repeats no TABLE row), 1 when it is not, 2 on a usage or input error (an unknown column, `--on` without `--join`, a bad `--top`), and 3 when BigQuery's dry run is over the byte cap, as for `reconcile`.

`key-check` is the command-line face of the agent server's [`join_profile`](mcp-server.md#join_profile-and-key-measurements) plus duplicate groups. Both group each side by its key once and read every number from those groups, built from the same SQL pieces in [`kumosql.join_sql`](../src/kumosql/join_sql.py), so each table is read once and the shared numbers agree. Unlike `join_profile`, `key-check` saves nothing.

## Checking the rules later

```
python -m kumosql rules check --rules rules.yml --left my-proj.sales.orders_v1 --right my-proj.sales.orders_v2 \
    --key order_id --map crosswalk.yml
python -m kumosql rules list --rules rules.yml
```

`rules check` runs the same comparison, again as one query, and reports each rule. It takes the same `--float-tolerance`, `--float-relative` and `--tolerance` options, which decide which rows differ in the first place; give it the ones the reconciliation used.

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

`kumosql.reconcile.reconcile(left, right, keys, executor, ignore=, crosswalk=, rules=, include_samples=, sample_limit=, float_tolerance=, float_relative=, tolerance=)` returns a `ReconcileResult` (`to_json()` is the printed form), `check_rules(...)` a `RulesCheckResult`, and `build_reconcile_sql(...)` / `plan_reconcile(...)` the query without running it. `tolerance` is the `--tolerance` text, a list of such texts, or a mapping such as `{"price": 0.01, "rate": (0, 1e-6), "score": "exact"}`. `kumosql.key_check.key_check(table, keys, executor, join=, on=, include_samples=, top=)` returns a `KeyCheckResult`, and `build_key_check_sql(...)` / `plan_key_check(...)` its query. The executors follow [data profiling](data-profiling.md#how-it-runs):

- `DuckDBExecutor(connection)` reads each side's columns with `DESCRIBE` and runs the query on DuckDB.
- `BigQueryExecutor(project=None, max_bytes=None)` reads each side's columns with a dry run (free, nothing is processed), then runs the comparison through the same path as saved data sources: checked locally as one read-only `SELECT`, dry-run first and refused when the estimate is over the cap, run with `maximumBytesBilled`, in the billing project, never one guessed from a table name. The job location is the one in Settings; there is no per-command `--location`.

The query reads each side in a `WITH` clause, joins them once with `FULL OUTER JOIN`, computes one flag per compared column and per rule, sorts each row with `CASE WHEN rule_1 THEN 1 WHEN rule_2 THEN 2 ... ELSE 0 END`, and aggregates everything with `COUNTIF`. Each side is also counted on its own (rows, and keys that repeat), so its table is read twice within the one job; the dry run's estimate is the number to trust for cost.

Every generated query passes the same local read-only check as every other query KumoSQL runs (`kumosql.sql_validation.validate_readonly_query`), on DuckDB as well as BigQuery, including its 20,000-character limit. A comparison too wide for that limit is refused before anything runs, with a message to compare fewer columns (`--ignore`), leave samples off or shorten the `--left`/`--right` SQL. It is never sent unchecked. Measured with 8-character column names on DuckDB, the base is about 1,500 characters (2,100 with samples). Each compared text, integer or `NUMERIC` column adds about 180 (330 with samples), so roughly 100 fit, or about 50 with samples. Each floating-point column adds about 550 (700 with samples), because its tolerance test names both values about eight times, so about 33 fit, or 25 with samples. Longer names fit fewer; compare a wide table in groups of columns with `--ignore`.

## Limits

- The result counts rows and, with samples, shows keys; it never shows the differing values themselves. Use the keys to look the rows up.
- Apart from the float tolerance, equality is SQL equality: `1.0` and `1` agree, and trailing spaces and letter case count. A tolerance is a fixed number per column, not a rule about the data; a difference that depends on other columns is a rule's job (`l.currency = 'JPY' AND ABS(l.amount - r.amount) < 1`).
- Inside a STRUCT or an array, floats are compared exactly, never within a tolerance. On BigQuery they are compared as the numbers `TO_JSON_STRING` writes, so values the text writes the same way agree; `-0.0` against `0.0` inside a STRUCT may differ there while DuckDB says they agree. A field whose type differs between the sides (`INT64` on one, `FLOAT64` on the other) may also differ on BigQuery as text while DuckDB, which converts, says they agree. Select such fields as their own columns when it matters.
- A rule's predicate holds or not per joined row; it cannot compare a row with other rows (no aggregates or subqueries).
- Very wide tables hit the 20,000-character limit described above; compare them in groups of columns with `--ignore`.
