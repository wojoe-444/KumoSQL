# BigQuery and Dataform syntax coverage

[Plain-language version](../../docs_simple/evals/bigquery-syntax-coverage.md)

`tests/fixtures/bq_syntax/` is a checked-in manifest of small, valid cases, one per construct: every GoogleSQL statement family (query syntax, pipe syntax, DDL, DML, procedural language, transactions, DCL, `EXPORT DATA`/`LOAD DATA`, search and vector functions, JSON, geography, `ML.*`, UDFs, wildcard tables, `FOR SYSTEM_TIME AS OF`, `INFORMATION_SCHEMA`, quoting) and the Dataform action types (table, view, incremental, operations, assertion, declaration, test), their config options, the `ref`/`resolve`/`self`/`when`/`incremental()` helpers, `js` blocks and includes, plus whole-project layouts (`workflow_settings.yaml`, `dataform.json`, `actions.yaml`, the JavaScript API).

```
tests/fixtures/bq_syntax/
  manifest.json        every case: id, file, kind (sql or sqlx), feature tags
  sql/<family>/*.sql   one construct per file (GoogleSQL)
  dataform/sqlx/*.sqlx one construct per file (Dataform)
  dataform/projects/   whole-project layouts
  dry_run.json         BigQuery dry-run result for each case
  known_gaps.json      the gaps below, one entry per case and stage
```

SQL cases read the test bed's `raw_users` and `raw_order_items` or public datasets, so they can be dry-run against project `kumosql`; a few name objects that do not exist (a remote model, a connection, a bucket), which only the dry run can tell apart from a typo.

## What each stage means

Each case runs through every stage that applies. A stage is **✅ pass**, **⚪ unsupported** (KumoSQL or sqlglot says so explicitly and nothing is damaged) or **❌ fail** (a crash, a lost reference, a changed meaning). Only ✅ and ⚪ can be committed: a ⚪ must be listed in `known_gaps.json` with who owns it, so a new gap fails the test suite until it is fixed or written down.

| Stage | What is checked |
|---|---|
| parse | sqlglot's BigQuery dialect parses the text into a structured statement (not an opaque `Command`) |
| load | the case loads as a model (`.sql`) or action (`.sqlx`) without a diagnostic |
| refs | (Dataform) every literal `ref()` and config `dependencies` entry is a graph edge |
| graph | graph, column lineage and report build without error, and the tables the statement reads appear as reads |
| fingerprint | (single queries) the output-comparison SQL built around the query is valid |
| cleanup | the cleanup and rewrite rules run, and their result is proven equivalent or left unchanged; a statement sqlglot could only read in recovery mode counts as ⚪ even when no rule changed it, because an unchanged recovered parse is not trusted (see [Rewrite rules](../rewrite-rules.md)) |
| format | `format_sql` neither crashes nor changes meaning; backticked names are untouched; the result is proven equivalent |
| prover | the SMT prover returns a verdict or "unknown" for the query compared with itself, and never crashes or calls it different |
| dry run | BigQuery accepts the SQL (`dryRun`, never billed). ✅ accepted; ⚠ parsed, then stopped on something outside the SQL (an object, connection, session or permission the project lacks); ❌ rejected; – not submitted (administrative statements, and Dataform actions that need the Dataform compiler) |

Run it yourself:

```
python -m pytest tests/test_bq_syntax_coverage.py          # every case x stage
python tools/bq_syntax_coverage.py --failures              # list failures
python tools/bq_syntax_coverage.py --markdown out.md       # the table below
python tools/bq_dry_run_manifest.py --project kumosql      # free dry runs; needs BQ_ACCESS_TOKEN or ADC
```

After a change that closes (or opens) a gap, run `python tools/bq_syntax_coverage.py --update-known-gaps` under the supported sqlglot 30.21.0 version (routine CI uses its matching compiled build) and delete the entries that now pass.

See also [the behaviour eval](bigquery-behavior-eval.md), which executes rewrites of GoogleSQL compliance queries and BigQuery edge cases and compares results.

## What this found and fixed

- A nested `name:` (a documented column called `name`, a `{ name, schema }` entry in `dependencies`) renamed the action. Config keys are now read at the top level only.
- `${ctx.ref()}`, `${resolve()}`, `${ctx.self()}` and config `dependencies` were not graph edges.
- Dataform test `input "x" { ... }` blocks were treated as SQL; they are now preserved like `config`.
- The formatter upper-cased backticked routine paths (``DROP FUNCTION `p.d.f` `` became `` `P.D.F` ``), and the equivalence check called that proven. Quoted names are now restored exactly.
- `CREATE TABLE/VIEW ... AS SELECT`, `INSERT ... SELECT` and `EXPORT DATA ... AS SELECT` in `.sql` files had no reads in the graph; they do now.
- A `.sql` file with a `config { }` block is loaded as an action instead of failing to parse.
- Table-valued function calls that pass a table (`FROM dataset.fn(TABLE dataset.input, option => value)`) failed to parse ("Expecting )") in sqlglot's BigQuery parser, so the model lost every read. `kumosql.bigquery_syntax` wraps the BigQuery dialect's `parse` once for the whole package (not the parser itself, so it works on pure and compiled sqlglot alike): SQL sqlglot rejects is parsed again with `TABLE x` arguments marked, the table is read, the SQL prints back unchanged, and the columns the function returns are reported unknown (`untraceable_source`), never attributed to a table named like the function. When the project defines the function (`CREATE TABLE FUNCTION`), the call is read as the function's query and its columns trace to the source ([scripts.md](../scripts.md#table-functions)). Covered by `tests/test_table_function_arguments.py`.
- Analytical corpora ([analytical-sql-coverage.md](analytical-sql-coverage.md)) found that the formatter re-cased unquoted table and column names (BigQuery table names are case sensitive), and that windows and `ORDER BY ... LIMIT` blocked every proof; the `query/analytical_*` cases cover them.
- Formatting a statement sqlglot cannot parse or keeps as an opaque command (DDL for reservations, policies and indexes, `LOAD DATA`, `CALL`, `REPEAT`, `CHANGES(TABLE ...)`) was never proven, so it could not be accepted. A change that only moves whitespace and re-cases reserved keywords and built-in calls is now proven by comparing tokens. Formatting also re-cased user-defined function names (`f(x)` became `F(x)`), which are case sensitive; it now keeps them.
- Cleanup rewrote pipe-syntax queries into sqlglot's standard-SQL translation, and once into a broken query (`FROM t |> AS u` lost its table). Pipe syntax is now left as written.
- A file mixing statements sqlfluff can parse with one it cannot (a `GRANT`, `EXPORT MODEL`, a `CASE` script statement) was not formatted at all; each statement sqlfluff can parse is now formatted and the rest are kept as written.
- The read check counted the table a `REVOKE` names as a read; like `GRANT`, it is not one.
- Pipe `|> SET c = e` and `|> DROP c` did not parse; they are read as `|> SELECT * REPLACE (e AS c)` and `|> SELECT * EXCEPT (c)`, which BigQuery defines them to mean. `|> RENAME` is not: it keeps the column in its place, which no `SELECT` can say without the column list, and a translation that moved it could let a prover equate queries whose columns are in different orders.
- `GRAPH_TABLE(graph MATCH ... RETURN ...)` did not parse; the call is now kept as its own text (printed back unchanged, never read as a table) and the rest of the query is read as usual.
- `ML.` functions sqlglot does not know (`ML.EVALUATE`, `ML.DETECT_ANOMALIES`, `ML.EXPLAIN_PREDICT`, and on sqlglot 26.0.0 `ML.GENERATE_TEXT` and `AI.FORECAST`) did not parse because of their `MODEL m` argument. The model argument is kept as its text (a model is not a table) and `TABLE t` arguments are read as tables; functions sqlglot reads itself, such as `ML.PREDICT`, are left to it.
- `SELECT WITH DIFFERENTIAL_PRIVACY` stays unparsed on purpose: read as a plain query, its noisy aggregates would look exact to every prover. Its table reads still come from the token fallback.
- `DROP TABLE FUNCTION` did not parse; `bigquery_syntax.py` now reads it as a drop of kind `TABLE FUNCTION`. Script cleanup gaps closed by [the script reader](../scripts.md) are no longer listed.
- Raw bytes literals (`br'a\d'`, `rb".."`) did not parse; they are read as the `b'..'` literal with the same bytes. Pipe `|> WITH y AS (...)` did not parse; it is read as a `WITH` at the front of its query, but only when nothing before it spells a name it defines and the query has no `WITH` of its own (otherwise moving it could change what a name means).
- An outside GoogleSQL feature checklist (287 entries from the BigQuery reference and release notes, 2026-10-02) was checked against BigQuery: 31 of its 154 single-statement query examples were not valid GoogleSQL. The 119 distinct valid queries are kept in `tests/fixtures/googlesql_checklist.json` (source noted there), and `tests/test_googlesql_checklist.py` checks on every sqlglot build that each parses and prints back to SQL that reads the same. Three are recorded as not read yet: `MATCH_RECOGNIZE` (sqlglot's BigQuery tokenizer lacks the keyword, and reading it needs column lineage for `MEASURES` first), a parenthesized join that starts with `UNNEST`, and pipe `RENAME` (on purpose, above). The same check found two false proofs, now fixed ([provers.md](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/provers.md)): bytes literals that differ only in backslash escapes, and `CAST` to a parameterized type.
- Five constructs from the GoogleSQL compliance tests ([#520](https://github.com/walterogozaly/KumoSQL/issues/520)) were not read, or were misread, and are now handled in `bigquery_syntax.py` the same way on sqlglot 26.0.0, 30.20, 30.21 and compiled 30.21 (each was first checked with a BigQuery dry run):
  - `x LIKE ALL UNNEST(array)` did not parse (`LIKE ANY UNNEST` did). It is read as `All` over an `Unnest` and prints back as written; `LIKE SOME` is read as `LIKE ANY`, which prints that way.
  - An aggregate with a `WHERE` inside its call (`COUNT(* WHERE c)`, `SUM(DISTINCT x WHERE c)`, `ARRAY_AGG(x IGNORE NULLS WHERE c)`) is read as the standard `AGG(...) FILTER (WHERE c)` (`exp.Filter`) and printed back inside the call. BigQuery rejects the filter inside a window call and next to `ORDER BY`, `LIMIT` or `HAVING`; those stay unparsed, never read as something else. Provers treat the filtered aggregate as they treat `FILTER` from other dialects.
  - `WITH(a AS 1, a + 1)` is read as a marker call that holds each definition and the result, and prints back as written. A bare `a` in a later definition or in the result is the variable, not a column, so it becomes a variable node and counts as no column read. A name defined twice, or a variable name used inside a subquery of the expression (which could mean a column of the subquery), is refused. The SMT prover answers unknown for it.
  - `FROM t, t.arr elem WITH OFFSET off` did not parse; the array path is read as `UNNEST(t.arr)` with its offset, which is what BigQuery defines the path to mean. A plain table with `WITH OFFSET` is not guessed at.
  - `STRUCT<>()` was read as the comparison `STRUCT <> ()`. BigQuery parses it as an empty struct type and then rejects it ("Unsupported empty struct type"), so it is now refused instead of misread.
  `ROUNDING_MODE` was on the list too, but `ROUND(x, 0, 'ROUND_HALF_EVEN')` already parses on every build, and the cast form `CAST(x AS NUMERIC ROUNDING_MODE ...)` is a syntax error in BigQuery. The bitwise operator precedence (`2 | 1 & 0`) and non-associative comparison (`a > 10 IS TRUE`) misreads are left to the parser trust boundary work ([#500](https://github.com/walterogozaly/KumoSQL/issues/500)), which declines them when it lands instead of re-associating the tree, because re-associating would change the printed SQL everywhere. Until then a prover can equate `a > 10 IS TRUE` with `(a > 10) IS TRUE`, which BigQuery rejects.
  The new cases are `query/like_all_any_unnest`, `query/aggregate_where_filter`, `query/with_expression` and `query/unnest_path_with_offset` in the manifest (all four dry-run valid; sqlfluff cannot format the first three, listed as `format` gaps), plus four entries in the checklist file and `tests/test_bigquery_grammar.py`.
- sqlglot read a string, bytes literal or backticked name that runs onto a second line without triple quotes (`'a<line break>b'`), which BigQuery rejects as an unclosed literal, so the provers equated it with `'a\nb'`. `bigquery_syntax.py` now rejects it as BigQuery does; triple-quoted literals still span lines.
- Fixtures themselves: the dry run caught 20 fixtures that were not valid GoogleSQL (qualifying a backticked table by its short name, unsupported `DEFAULT` arguments, `JSON_KEYS` on a string, and so on); they were corrected and re-checked.

## Gaps that are not fixed here

- **sqlglot** keeps procedural statements (`DECLARE`, `IF`, `LOOP`, `BEGIN ... END`, `CALL`, `EXECUTE IMMEDIATE`) and many `ALTER`/`DROP`/`CREATE` forms (reservations, indexes, aggregate and remote functions) as opaque commands, and cannot parse `LOAD DATA`, `CHANGES`/`APPENDS`, `UNION ... CORRESPONDING` and some pipe operators. KumoSQL leaves such statements untouched and says so. The historical 26.0.0 and 30.21.0 runs found different gaps, so `known_gaps.json` still contains their union; this does not imply current support for 26.0.0.
- **Scripts** are read by KumoSQL's own splitter ([scripts.md](../scripts.md)), so the graph reads of `MERGE`, `UPDATE`, `DELETE` and scripts work even where sqlglot's `parse` stage still lists a script case as a gap (sqlglot cannot parse `BEGIN ... END` and procedural statements). Dynamic `EXECUTE IMMEDIATE` and undefined `CALL`s stay unknown.
- **sqlfluff** cannot parse `GRANT`/`REVOKE`, `EXPORT MODEL`, remote functions and models, property graphs, some literals and a few other statements, so they are not formatted (`parse_error`, or `statements_not_formatted` when other statements in the file are).
- **Project layouts**: `actions.yaml` is not read (`tests/test_bq_syntax_projects.py`, as xfail). `projectSuffix`/`datasetSuffix`/`namePrefix` are applied as the compiler does, and a literal `publish(...).query(...)` in a `.js` file is read; `operate()`, `assert()` and publishes inside loops or functions are not.
- **Computed references**: a `ref()` whose argument is computed in JavaScript is reported as unresolved rather than guessed.
- **Prover** (owned by the SQLSolver work): unknown, never wrong, for `LIMIT`, window functions, `TABLESAMPLE`, unaliased subqueries and nondeterministic aggregates.

## Coverage

<!-- coverage-table:start -->
| Family | Cases | parse | load | graph | fingerprint | cleanup | format | prover | dry run |
|---|---:|---|---|---|---|---|---|---|---|
| data | 8 | 5 ✅ 3 ⚪ | 8 ✅ | 8 ✅ | n/a | 5 ✅ 3 ⚪ | 7 ✅ 1 ⚪ | n/a | 1 ✅ 7 ⚠ |
| dcl | 5 | 4 ✅ 1 ⚪ | 5 ✅ | 5 ✅ | n/a | 5 ✅ | 0 ✅ 5 ⚪ | n/a | 0 ✅ 5 – |
| ddl | 74 | 45 ✅ 29 ⚪ | 74 ✅ | 74 ✅ | n/a | 73 ✅ 1 ⚪ | 65 ✅ 9 ⚪ | n/a | 48 ✅ 20 ⚠ 6 – |
| dml | 16 | 16 ✅ | 16 ✅ | 16 ✅ | n/a | 16 ✅ | 16 ✅ | n/a | 16 ✅ |
| query | 138 | 136 ✅ 2 ⚪ | 138 ✅ | 138 ✅ | 135 ✅ | 136 ✅ 2 ⚪ | 132 ✅ 6 ⚪ | 112 ✅ 23 ⚪ | 119 ✅ 16 ⚠ 3 – |
| script | 22 | 6 ✅ 16 ⚪ | 22 ✅ | 21 ✅ 1 ⚪ | n/a | 22 ✅ | 21 ✅ 1 ⚪ | n/a | 20 ✅ 2 ⚠ |
| transaction | 2 | 1 ✅ 1 ⚪ | 2 ✅ | 2 ✅ | n/a | 2 ✅ | 2 ✅ | n/a | 2 ✅ |
| **all GoogleSQL** | 265 | 213 ✅ 52 ⚪ | 265 ✅ | 264 ✅ 1 ⚪ | 135 ✅ | 259 ✅ 6 ⚪ | 243 ✅ 22 ⚪ | 112 ✅ 23 ⚪ | 206 ✅ 45 ⚠ 14 – |

| Dataform | Cases | parse | load | refs | graph | cleanup | format | dry run |
|---|---:|---|---|---|---|---|---|---|
| SQLX actions | 64 | 60 ✅ | 63 ✅ 1 ⚪ | 55 ✅ 1 ⚪ | 62 ✅ | 62 ✅ | 62 ✅ | 40 ✅ 2 ⚠ 22 – |

### Known gaps

| Stage | Owner | Reason | Cases | Examples |
|---|---|---|---:|---|
| parse | sqlglot | ParseError: Invalid expression / Unexpected token. | 26 | `data/export_data`, `data/export_data_connection`, `data/export_data_pubsub` |
| parse | sqlglot | sqlglot keeps CREATE as an opaque command | 11 | `ddl/create_aggregate_function`, `ddl/create_assignment`, `ddl/create_capacity_reservation` |
| parse | sqlglot | sqlglot keeps ALTER as an opaque command | 10 | `ddl/alter_materialized_view`, `ddl/alter_model`, `ddl/alter_organization` |
| parse | sqlglot | sqlglot keeps DROP as an opaque command | 6 | `ddl/drop_all_row_access_policies`, `ddl/drop_external_table`, `ddl/drop_index` |
| parse | sqlglot | sqlglot keeps BEGIN as an opaque command | 5 | `script/begin_end_block`, `script/begin_exception`, `script/raise` |
| parse | sqlglot | sqlglot keeps DECLARE as an opaque command | 3 | `script/declare_set`, `script/declare_struct_array`, `script/set_from_subquery` |
| parse | sqlglot | sqlglot keeps EXECUTE as an opaque command | 3 | `script/execute_immediate`, `script/execute_immediate_concat`, `script/execute_immediate_using_positional` |
| parse | sqlglot | AttributeError: 'NoneType' object has no attribute 'name' | 2 | `data/load_data`, `data/load_data_temp_table` |
| parse | sqlglot | sqlglot keeps END as an opaque command | 2 | `ddl/create_procedure_options`, `script/for_in` |
| parse | sqlglot | ParseError: Expecting ). | 2 | `ddl/create_procedure_sql`, `query/object_table_function` |
| parse | sqlglot | sqlglot keeps CALL as an opaque command | 2 | `script/call_procedure`, `script/call_with_dml` |
| parse | sqlglot | ParseError: Required keyword: 'options' missing for <class 'sqlglot.expressions.dml.Export | 1 | `data/export_model` |
| parse | sqlglot | sqlglot keeps GRANT as an opaque command | 1 | `dcl/grant_project` |
| parse | sqlglot | ParseError: Unsupported pipe syntax operator: 'SET'.. | 1 | `query/pipe_extend_set_drop` |
| parse | sqlglot | ParseError: Required keyword: 'expression' missing for <class 'sqlglot.expressions.Union'> | 1 | `query/set_corresponding` |
| parse | sqlglot | ParseError: Required keyword: 'true' missing for <class 'sqlglot.expressions.functions.If' | 1 | `script/case_when` |
| parse | sqlglot | sqlglot keeps IF as an opaque command | 1 | `script/if_elseif_else` |
| parse | sqlglot | sqlglot keeps LOOP as an opaque command | 1 | `script/loop_leave_iterate` |
| parse | sqlglot | sqlglot keeps WHILE as an opaque command | 1 | `script/while_loop` |
| load | kumosql | ref() with a computed argument is not resolved | 1 | `dataform/table_dynamic_dependencies` |
| graph | kumosql | reads of this DML, script or non-query statement are not extracted | 1 | `script/assert` |
| cleanup | kumosql | recovered_parse | 4 | `data/export_model`, `data/load_data_overwrite`, `ddl/undrop_schema` |
| cleanup | kumosql | parse_error | 4 | `data/load_data`, `data/load_data_partition_columns`, `data/load_data_temp_table` |
| cleanup | kumosql | recovered_parse; output_parse_error; recovered_parse; output_parse_error; recovered_parse; | 1 | `dataform/operations_export` |
| cleanup | kumosql | equivalence could not be proven for every changed statement | 1 | `dataform/table_with_qualify_cte` |
| cleanup | kumosql | recovered_parse; pipe_syntax_kept; recovered_parse; pipe_syntax_kept; recovered_parse; pip | 1 | `query/pipe_extend_set_drop` |
| format | sqlfluff | parse_error | 22 | `data/export_model`, `dcl/grant_project`, `dcl/grant_schema` |
| prover | prover | unsupported: LIMIT is not modeled | 9 | `query/backtick_dashed_project`, `query/backtick_dataset_only`, `query/backtick_whole_path` |
| prover | prover | unsupported: WINDOW is not modeled | 7 | `query/ml_feature_functions`, `query/pipe_select_window_qualify`, `query/pseudo_columns_row_number` |
| prover | prover | unsupported: nondeterministic: TABLESAMPLE SYSTEM (10 PERCENT) | 2 | `query/pipe_call_tablesample`, `query/tablesample` |
| prover | prover | unsupported: nondeterministic: ARRAY_AGG(DISTINCT state) | 1 | `query/aggregate_filter_modifiers` |
| prover | prover | unsupported: nondeterministic: ANY_VALUE(city) | 1 | `query/aggregate_functions` |
| prover | prover | unsupported: nondeterministic: ARRAY_AGG(first_name IGNORE NULLS) | 1 | `query/aggregate_where_filter` |
| prover | prover | unsupported: unaliased subquery in FROM | 1 | `query/pipe_pivot_unpivot` |
| prover | prover | unsupported: nondeterministic: TABLESAMPLE SYSTEM (50 PERCENT) | 1 | `query/tablesample_with_join` |
| refs | kumosql | ref() inside a js block is not resolved: raw_users | 1 | `dataform/js_block_with_ref_in_helper` |
<!-- coverage-table:end -->
