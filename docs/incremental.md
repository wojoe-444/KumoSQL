# Incremental models: does the incremental run equal a full refresh?

[Plain-language version](../docs_simple/incremental.md)

A Dataform incremental table runs its full query once, then on later runs only the `when(incremental(), ...)` form, appending (or merging on `uniqueKey`). It is correct when, after every batch of source changes, the table equals what a full refresh would produce. `kumosql.incremental` checks that.

```python
from kumosql.incremental import SourceTable, check_incremental, parse_incremental_sqlx

model = parse_incremental_sqlx(sqlx_text, target="orders_inc")       # ${self()} resolves to this
sources = {"events": SourceTable({"id": "INT64", "ts": "TIMESTAMP", "v": "INT64"}, key=("id",), time_column="ts")}
verdict = check_incremental(model, sources, kinds={"insert_new", "insert_late"})
verdict.outcome          # "safe" | "diverges" | "unknown" | "unsupported" | "timeout"
verdict.counterexample   # for "diverges": initial rows and batches that replay the failure
```

## What it models

The simulator (DuckDB) follows Dataform's run cycle:

* The first run builds the table with the full query; later runs apply the incremental query.
* Incremental `pre_operations` run first (for example a `DELETE FROM ${self()} ...` reload window).
* Without `uniqueKey` the result is appended. With it the run is Dataform's BigQuery `MERGE`: matched target rows are updated in place (two copies of a row in the table stay two copies), unmatched source rows are inserted, a target row matching more than one source row is an **error** (BigQuery raises), and NULL keys never match, so such rows are inserted again every run.
* `bigquery.updatePartitionFilter` is added to the `MERGE` condition as Dataform writes it (`AND DATAFORM_DEST.<filter>`), so a target row outside it is never matched and a changed row lands beside its old version. The proof rules do not apply to such a model; only the counterexample search does.
* Settings that change the run and are not modelled make the model `unsupported`, never `safe`: an `insert_overwrite` (or any other explicit) `incrementalStrategy` that is not the default for the model, `incrementalPredicates`, `post_operations`, `pre_operations` that run on full builds, and a `uniqueKey` or `updatePartitionFilter` that is not a literal value.
* `CURRENT_TIMESTAMP` is pinned to a per-run clock; audit columns are excluded by name (`ignore_columns`) rather than making every run differ.
* `${self()}`, `${ref()}`, `when(incremental(), a, b)` and `when(!incremental(), a)` are evaluated; any other interpolation is refused (`unsupported`) rather than guessed. SQLX splitting reuses `kumosql.sqlx`.

Source changes are DuckDB/ANSI DML; model SQL is BigQuery. DuckDB is where results are executed, so BigQuery behaviour that DuckDB does not share (type coercion, `MERGE` rules) is modelled explicitly as above or not claimed.

## Contracts

A contract is the set of source-change kinds allowed: `insert_new`, `insert_late`, `insert_boundary` (same event time as the newest), `duplicate` (exact re-delivery), `update`, `update_touch` (update that also moves the event time to the newest), `delete`, `null_key`, `empty` (a run with no change). `tables` restricts which source tables change. "Safe" always means *under that contract*; there is no contract-free correctness.

## Verdicts

* **safe**: a proof rule applies. R1 (append): no `uniqueKey`, strict watermark `ts > COALESCE((SELECT MAX(ts) FROM self), <old date>)`, row-wise single-table query, only in-order inserts. R2 (merge): `uniqueKey` equal to the source's declared key, `>` with `insert_new`/`update_touch` (`update_touch` only without a `WHERE`, since an update can move a row out of the filter and a merge never deletes it), or `>=` or a `TIMESTAMP_SUB` lookback also allowing `insert_boundary`. The watermark must be exactly `COALESCE((SELECT MAX(ts) FROM self), <literal date no later than 2000>)`: a `WHERE`, `HAVING`, join, `LIMIT`, sample, snapshot or another dataset's table of the same name in the subquery, or a computed default, is not a watermark (`HAVING FALSE` makes the `MAX` NULL, so every run re-reads everything). A lookback must subtract a constant, non-negative interval (`INTERVAL -1 DAY` raises the boundary). Assumptions are in the `prove_watermark` docstring. Further rules are in `kumosql.incremental_rules`:
  * R3 (key de-duplication): R1 or R2 plus `QUALIFY ROW_NUMBER() OVER (PARTITION BY <source key> ...) = 1`; re-delivered rows (`duplicate`) are then absorbed.
  * R4 (truncated watermark): R2 whose watermark is `DATE(ts)`, `TIMESTAMP_TRUNC(ts, ...)` or `CAST(ts AS DATE)` `>=` the `MAX` of the same expression projected by the model, so a run reloads the newest day. A strict `>` is refused.
  * R5 (group re-aggregation): `SELECT g, <SUM/COUNT/MIN/MAX/AVG>, MAX(ts) AS m ... GROUP BY g` merged on `g`, whose incremental run keeps the groups with a row at or after the watermark on `m` (`g IN (SELECT g FROM source WHERE ts >= ...)`). Inserts only; no `HAVING`. Assumes `g` is never NULL (the change kinds only make NULLs through `null_key`, which is refused).
  * R6 (unchanged joined tables): R1 over a fact table joined (`INNER` or `LEFT`, fact table first) to tables the contract's `tables` never changes.
* **diverges**: a random search over change sequences allowed by the contract found one on which the model differs from a full refresh (or fails), shrunk by dropping batches and statements while it still diverges. It is returned with the verdict and replays on its own.
* **unknown**: no proof rule applies and the search found nothing. This is bounded evidence, not a proof.

## The change generator

`random_sequence` (used by `search_divergence`, so by `check_incremental`) builds an initial load and a few batches of source DML from the contract's kinds. Each seed gives one fixed sequence. About one in five (`GAP_RATE = 0.2`) `insert_new` rows lands 3 to 30 hours (`GAP_HOURS`) after the table's newest event time instead of one hour after it, so a run can carry rows further apart than a delete-then-reload window or a lookback.

The gap decisions come from a second generator seeded from the main seed (`random.Random(f"gap:{seed}")`), never from the main one. Every choice the main stream makes (operation, table, row picked, column values) is therefore the same with or without gaps, and only event times move. `gap_rate=0` reproduces the sequences of earlier versions exactly. `tests/test_incremental_generator.py` pins digests of those sequences for five seeds, compares gap and no-gap output with the times removed, and checks the gap share.

With gaps, `pre-operation-window-from-source` (a window deleted and reloaded from the source's own maximum) is refuted instead of left unknown. On the dev split (`--split dev`, 86 cases) this moves decided cases from 83 to 84 (refuted 47 to 48, unknown 3 to 2) and changes no other verdict, with 0 wrong and 0 fidelity failures; the pg_ivm track is unchanged.

## Scores

`python tools/incremental_bench.py` runs the corpus in `tests/fixtures/incremental/` and reports counts per outcome; the scoreboard page has two rows (detection, evidence `executed`; proofs, evidence `proof`).

| | cases | result |
| --- | --- | --- |
| Diverging cases refuted | 34 | 34, 0 unknown, 0 false alarms |
| Safe cases proven | 15 | 15 proven, 0 false proofs |
| Held-out (12 blind cases) | 12 | 9/9 refuted, 3/3 proven |
| Baseline (first 37 cases, before generalising the proof rules) | 37 | 24/24 refuted, 2/13 proven |

Before R3 to R6, five safe cases were unknown: a de-duplicating merge (`QUALIFY ROW_NUMBER`), a re-aggregating merge, a pre-operation reload window, a day-granular watermark and a join against an unchanged dimension. R3 to R6 prove four of them (written for those cases, so the dev number is optimistic; the held-out cases were not used). The fifth, `pre-operation-window-from-source`, was relabelled `diverges`: it deletes and reloads the last 2 hours before the source's newest row, so a run that adds two rows more than 2 hours apart (allowed by `insert_new`) never loads the earlier one. The change generator used to step new rows one hour at a time, so the search missed it; with the gap rows described under The change generator it is refuted.

While testing R3, R2 turned out to prove a merge with a `WHERE` under `update_touch`, which diverges when an update makes a row fail the filter (the old row stays). No corpus case had that shape; R2 now refuses it, and `tests/test_incremental_rules.py` keeps it as a regression case alongside near misses for every rule.

**How cases are labelled.** Each case is authored with a label for its contract and a scripted change sequence. A fidelity check replays the script in the simulator and requires the outcome to match the label (this caught two authoring mistakes in the blind set). Detection never sees the script. Authored cases are original; adapted material (pg_ivm, the fixture repository) is recorded separately when added. No existing eval covers incremental maintenance (the SQLSolver, R-Bot, SQL-IQ and rewriting evals compare queries, not run histories); `synthetic_check` checks rewrites on random data and is the nearest related code.

**Caveats.** The proof rules and the change generator were developed on the 37 dev cases, so the dev numbers are optimistic; the held-out set is small. Every failure found is kept as a case.

## pg_ivm workloads

`python tools/incremental_pgivm.py <pg_ivm checkout>` reads pg_ivm's `sql/pg_ivm.sql` and `sql/outer_join.sql` (v1.16, commit 22b4b45, PostgreSQL License) and writes `tests/fixtures/incremental/pgivm_cases.json`. Each case keeps the **original** workload (view query, statements before and after it, verbatim Postgres SQL, file and line) apart from the **adapted** one (DuckDB statements and the Dataform model). Savepoint rollbacks start a new workload from the same base state; exact duplicates are dropped.

pg_ivm maintains views with its own engine, which Dataform lacks. The adapted model is the common Dataform append pattern, the same for every workload and fixed before running: every source table gains a `_loaded_at` load time (insertion order), the model projects the first table's `_loaded_at` (its `MAX` for an aggregate) and each incremental run reads only rows past `MAX(__loaded_at)` of its own table. Inserts into the first table keep it correct; updates, deletes, changes to joined tables and re-aggregation break it.

`python tools/incremental_bench.py --track pgivm`: 1,069 workloads extracted, 336 after de-duplication, **110 adapted**, 226 unsupported (216 use data-modifying CTEs, which apply several changes in one snapshot and cannot be replayed one by one; 10 use CTEs, subqueries, `DISTINCT` or non-table sources). Of the 110: **100 drift on their script and all 100 are refuted again by `check_incremental`, without the script**, 8 agree with their script (not scored: no label), 2 could not be run. The first run, at 20 seeds, refuted 95 and left 5 unknown; the seed count was then raised to 30 (the default), which refutes all 100. **That 100/100 is tuned on test; the clean number is 95/100.** No proof was given for a drifting workload. Held-out: `--track pgivm-heldout` gives each of the 29 distinct adapted views one fresh random insert/update/delete script (generator seed 9001, never seen while tuning) and scores the drifting ones once at the frozen 30 seeds: 28 of 29 drift and all 28 are refuted, 0 wrong.

## Scanning a project

`python -m kumosql incremental-report PROJECT` (`kumosql-incremental-report`) checks every `type: "incremental"` action under `definitions/` against three contracts: `append_only`, `late_and_duplicate` and `mutable` (updates and deletes). Source columns are inferred from what the query reads (`id` taken as the unique key, `*_at` / `ts` as event time), so each answer is about the model on those assumed sources; `--source-schema` supplies real columns. Models it cannot simulate (JavaScript helpers in the query, `pre_operations` that run on every run, unparsable SQL) are listed as unsupported, never guessed.

On the generated fixture project (`python tools/make_dataform_fixture.py OUT --models 600 --seed 11`): 64 incremental models, 54 simulated and 10 unsupported (7 always-run `pre_operations`, 2 JavaScript date helpers, 1 unparsable). Their definition re-runs the full query and merges on `id`. Under `append_only` all 54 are unknown (no proof rule covers merges of a full re-run). Under `late_and_duplicate`, 19 diverge: delivering a row again makes the `MERGE` match one target row twice, which BigQuery rejects. Under `mutable`, 40 diverge: a deleted or changed source row leaves its old row in the table. This is a scan, not a scored eval: the sources are inferred, so there are no labels.

On the real fixture repository (`kumosql-dataform-fixture`, commit f056260, read-only): 316 incremental models, 267 simulated and 49 unsupported (35 always-run `pre_operations`, 13 JavaScript `dateFilter` helpers, 1 unparsable). `append_only`: 265 unknown, 2 timeout (no proof rule covers the fixture's merge-of-a-full-re-run definition). `late_and_duplicate`: 105 diverge (a re-delivered row makes the `MERGE` match one target row twice), 162 unknown. `mutable`: 206 diverge (a deleted or changed source row leaves its old row), 61 unknown. Run took about 17 minutes. Unscored, for the same reason.

Known limits: a model whose result depends on tie-breaking (an `ORDER BY` or `ROW_NUMBER` with equal keys) can differ between two evaluations; the simulator compares what DuckDB returns and does not detect that.

## Which tables could become incremental

`python -m kumosql incremental-report PROJECT --advise` (`kumosql.incremental_fit`) asks the opposite question: for each `type: "table"` action, which are rebuilt in full on every run, could it be incremental, and what blocks it? It is read-only and runs nothing; add `--json` for the full answer, `--source-schema` for real source columns, and `--table-bytes sizes.json` (with `--changed-fraction`) for cost figures.

For each table a model reads (its *parent*), the advice names one pattern, or the blockers:

| Pattern | When it applies | What a run recomputes |
| --- | --- | --- |
| `partition_aligned` | the table's partition column is the parent's partition (or watermark) column passed through row by row, directly or through `DATE()`, also through CTEs and subqueries; every `GROUP BY`, window and `DISTINCT` over the parent includes it. An unpartitioned table gets a suggested `partitionBy` | only the newest partitions |
| `partition_other_column` | the declared partition column is another column of the parent (`order_date` while the parent changes by `last_upd_ts`) | the partitions the changed rows fall in, old and new |
| `keyed_merge` | every `GROUP BY`, window and `DISTINCT` over the parent is keyed by one parent column kept in the output (including the latest row per key with `ROW_NUMBER() ... = 1`), or the query keeps rows one by one with the parent's unique key | the keys that changed, merged on `uniqueKey` |
| `outer_join_dimension` | the parent is the nullable side of one `LEFT`/`RIGHT` join on one equality, in a plain row-by-row query | the rows whose join key changed |

Each pattern also needs a **watermark** on the parent, a column that tells new or changed rows from old ones: its `partitionBy` column, a `TIMESTAMP` column from `--source-schema`, or a column named like one (`*_at`, `*_ts`), in that order, with the basis shown. A merge key comes from the table's `GROUP BY`, from the parent's `uniqueKey` (on its declaration or `assertions`), or, as `incremental-report` assumes, from a column named `id` (marked "assumed").

Blockers are named with the shape that causes them and a fix, for example:

```
blocked order_count: aggregate without a partition key: count(*) over all of orders with no GROUP BY, so any change rewrites the one result row; fix: group by a key column that you also select, or by the partition column; a small table can stay a full refresh
blocked ranked: non-monotone window over the whole table: row_number() OVER (...) has no PARTITION BY, so one new row can change every row; fix: PARTITION BY the partition column or one selected key column
```

The other codes are `group_not_keyed`, `window_not_keyed`, `distinct_not_keyed`, `limit`, `read_twice` (a self-join, a CTE over the parent used twice, a subquery over it beside the `FROM`), `subquery_expression`, `set_operation`, `nullable_side` (an outer join on more than one column, or not row by row), `no_unique_key`, `partition_outside_parent`, `unresolved_partition`, `no_watermark` ("source has no watermark column") and `nondeterministic` (`RAND()`, `GENERATE_UUID()`, or the clock outside an output column; a clock value as an output column is an audit column and only gets a note). A table is a `candidate` when every parent fits, `partial` when some do (a change in the others still needs a full refresh; R6 proves an append over joins only while the joined tables never change), `blocked`, or `no_suggestion` (it reads no table, or its SQLX uses JavaScript the advisor does not evaluate).

**It suggests; it never says the incremental version is equivalent.** For a single-parent candidate whose shape has a plain Dataform rendering, the advice sketches the conversion (the config and the `when(incremental(), ...)` condition) and asks the proof rules about the sketch, on the sources `incremental-report` would infer. When one applies, the advice says "checkable with incremental-report once converted" and names the rule and contract; the check that counts is `incremental-report` on the model you write. For example, `select customer_id, sum(amount) as total, max(last_upd_ts) as last_upd from orders group by customer_id` gets `keyed_merge` on `customer_id` and the sketch `customer_id IN (SELECT customer_id FROM orders WHERE last_upd_ts >= COALESCE((SELECT MAX(last_upd) FROM ${self()}), TIMESTAMP '1970-01-01'))`, which R5 proves under `append_only`; `tests/test_incremental_fit.py` converts it and `incremental-report` proves it there, and finds a divergence under `late_and_duplicate`. A daily total grouped by `DATE(ts)` gets a day-reload sketch that no rule covers, and the advice says that only the counterexample search applies.

**Cost figures** (`--table-bytes`) are bytes scanned per run, in the vocabulary of `kumosql.savings`, and the three bases never mix. The `estimate` is KingyoOrchestrator's KPI model: each parent is read over the share its pattern touches (`changed_fraction`, 1/365 by default, widened by `late_spread` 2 or `key_span` 3), in full when the parent declares no `partitionBy` (BigQuery prunes a filtered read only by partition), a merge also reads the table itself, and every table referenced bills at least 10 MiB; the assumptions are listed as caveats. The `upper_bound` is the saving a run cannot exceed: a rebuild bills at most its tables' bytes, an incremental run at least 10 MiB per table it references. `measured` is always null here: a measured saving needs before and after windows of real runs of a deployed change, recorded in the savings ledger. An estimate below zero (small tables, where the 10 MiB minimum dominates) is kept as a regression, never clamped.

**Limits.** The patterns are ported from KingyoOrchestrator's offline prototype (`incremental/analysis.py`, `flatten.py`, `outer_join.py`, the rules of `docs/incremental-supported-sql.md`), whose soundness argument is about recomputing partitions or keys, not about Dataform's `MERGE`; the Dataform rendering is a sketch. Unqualified columns in a join resolve only with `--source-schema`; without it such a column is not traced and the pattern is not found. Whether a suggested merge key is unique above a join is not checked. Deletes in a parent are invisible to a watermark. Multi-table candidates get no sketch.

## Test coverage for R7, key growth, script variables and ties

Four test files pin the rules that the corpus only exercises through whole models. Each documented rule has a positive case and a near miss that differs from it in exactly one condition, so a rule that is widened by accident fails a test.

* `tests/test_incremental_merge.py`: R7 (`kumosql.incremental_merge`). Near misses for each condition: a query that differs from the full query (a real filter, `WHERE 1 = 2`); a `uniqueKey` that is not unique (not the `GROUP BY` or partition, or `duplicate` re-deliveries); a key that can be NULL (a nullable column, `null_key`); keys that can leave the output (`delete`, `update` under a filter or a group, `LIMIT`, a comparison with an aggregate); ties (a `QUALIFY ROW_NUMBER` order that is not total, `ANY_VALUE`, `RAND`). It also covers `canonical_query` and `dedup_abstraction`: `QUALIFY ... = 1`, `<= 1`, an alias, and `WHERE rn = 1` over a derived table or a CTE read once, against a CTE read twice, a second row per partition, a join beside the derived table and an aggregate that already groups.
* `tests/test_incremental_monotone.py`: `analyze` and `contract_constraints` (`kumosql.incremental_monotone`). One case per documented rule (frozen, growing and keyed sources; positive filters; `LEFT` and `INNER` joins; `GROUP BY`; `QUALIFY` de-duplication; `UNION`; `EXCEPT` against a frozen query; `HAVING` with monotone `COUNT`/`MAX`/`MIN`), each with a near miss, and the key facts a contract keeps (`duplicate` breaks uniqueness, `null_key` breaks both, other columns get no `NOT NULL`).
* `tests/test_incremental_variables.py`: statement splitting on `;` and the SQLX `---` line, data-neutral statements (`GRANT`/`REVOKE`, `ALTER ... SET OPTIONS`), `DECLARE`/`SET`, the liveness pass, `effective_model` substitution and its refusals (clock, `RAND`, a table written in between), and `_strip_old_bounds`.
* `tests/test_incremental_ties.py`: `tie_reasons`, the row-order witness `tie_witness`, the `nondeterministic` verdict, and the near miss that must not be nondeterministic (a total order).

`test_proven_cases_never_diverge_under_a_deeper_search` in `tests/test_incremental.py` now calls `prove()`, so every rule, R7 included, is rechecked against a deeper search (25 seeds, 5 batches) on the dev cases. It reads only the dev split.

**Bugs these tests found.** `SELECT * EXCEPT (...)` was read as a plain `SELECT *` (sqlglot 30 names the argument `except_`), so R7 treated an incremental query that selects fewer columns as the full query; it is now a different query. `GENERATE_UUID()` was not recognised as random, so R7 could prove a model with a random column; it is now reported as nondeterministic, like `RAND()`.
