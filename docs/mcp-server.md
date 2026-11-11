# Agent-facing server (`python -m kumosql mcp`)

[Plain-language version](../docs_simple/mcp-server.md)

One stdio [Model Context Protocol](https://modelcontextprotocol.io) server for an agent that works on a Dataform project. It loads the project once, keeps it in memory and answers **one question per call** with a **bounded answer**: about 2 KB by default, with counts, what it could not determine and why, a next step, and the argument that asks for more. This is step 4 of the [roadmap](roadmap.md#4-one-agent-facing-server) (the first four tools), step 6 ([preflight, comparison and on-demand profiling](roadmap.md#6-preflight-comparison-and-on-demand-profiling), four more) and the [analyst-agent trial](https://github.com/wojoe-444/KumoSQL/issues/872) (`list_tables`, `clean_query`, and reading `${ref()}` in drafts) and [`project_checks`](https://github.com/wojoe-444/KumoSQL/issues/881).

```
python -m kumosql mcp PROJECT [--source-schema FILE] [--schema-snapshot FILE] [--assume-grain TABLE=COL[,COL]]... [--profiles DIR]
python -m kumosql mcp --git URL [--branch B] [--refresh]
python -m kumosql mcp                      # saved data profiles only, no project
```

`PROJECT` is what `pipeline-report` takes: a Dataform project folder, a folder of `.sql` files or the JSON of `dataform compile --json`. The server answers `initialize` at once and parses and analyses the project in the background (about 3 seconds for the 106-model public project in the fixtures); the first question waits for it, and every later one is served from memory (a column trace or an impact answer takes 10 milliseconds or less). `--assume-grain` declares which columns identify a row of a table the project does not define, for example `--assume-grain logs.events=insert_id` (repeatable; the table is written as the other tools take it). Such a grain is reported as `declared`, never as derived, and a table or column that does not exist is dropped with a line on stderr. Without it the server uses only what the project itself shows: grains derived from the queries and `assertions: {uniqueKey}` in the models' configs. Register it with an MCP client as the command `python -m kumosql mcp /path/to/project`. Messages are JSON-RPC 2.0, one per line; progress logs go to stderr, never stdout.

## The questions

| Tool | The question | Arguments | Built on |
| --- | --- | --- | --- |
| `describe_table` | What is this table? | `table` | `profile_pipeline` (grain, row scope, column meanings), lineage filters, inferred role, readers, saved data profiles |
| `trace_column` | Where does this value come from, and which rows reach it? | `table`, `column` | `Pipeline.trace_column`, `explain_lineage` |
| `impact` | What breaks if I change this? | `table`, optional `column`, optional `change` | `Pipeline.assess_change` |
| `find_existing` | Does a table for this already exist, or one I can roll up? | one of `table`, `sql` | `find_overlaps`, `find_rollups` |
| `find_tables` | Which table should I use? | `need`, optional `column`, optional `kind` | table profiles, tags, saved data profiles, the saved catalog |
| `compare_queries` | Is my rewrite still the same query? | `left`, `right`, optional `time_limit`, optional `respect_row_order` | `prove_statements`, the SMT prover and its counterexample search |
| `check_query` | Is this draft sound before I run it? | `sql`, optional `dry_run` and `project` | type inference, tie detection, fan-out from table grain, the BigQuery dry run (on request) |
| `profile_table` | What do the values look like? | `table`, optional `project`, `run`, `file`, `duckdb`, `columns`, `sample_percent`, `row_filter`, `top_values`, `show_values` | `kumosql.data_profile` behind its byte cap |
| `list_tables` | What is in this project, and which tables have this shape? | optional `kind`, `tag`, `folder`, `name_contains`, `min_reads`, `max_reads`, `min_readers`, `max_readers`, `min_depth`, `sort` | the loaded project's graph, kinds, tags and file paths |
| `clean_query` | Can this query be tidied, and what exactly changes? | one of `sql`, `table`; optional `inline_ctes`, `time_limit` | the verified cleanup rules of `python -m kumosql rewrite-sql`, the equivalence prover and the independent checkers |
| `project_checks` | What is wrong with the repository itself, before any warehouse is asked? | optional `check` | the loaded project's declarations, `disabled` flags and loader diagnostics ([`kumosql.project_checks`](../src/kumosql/project_checks.py)) |

Every tool also takes `max_bytes` (the size bound, 1,000 to 20,000, default 2,000) and `offset` (where the answer's main list starts). **`${ref()}` in a draft.** `check_query` and `compare_queries` read `${ref("table")}`, `${ref("dataset", "table")}` and the same with `resolve` as that table of the project, so a draft written inside a Dataform project needs no editing; `refs_read_as` lists each as `ref(x) = dataset.table`. A reference to no table (or to several) is refused with the close table names, like every other tool. `${self()}`, `${when(...)}` and other expressions stay unread: `check_query` answers `cannot_check` naming the first of them, and `compare_queries` answers `unknown` because the text is not SQL.

A `table` is a model or declared source of the project, written as the full key, `dataset.table` or the bare table name, in any case. A name that matches several tables, or none, is not guessed: the error lists up to five close tables in `candidates`, the closest first (a misspelling puts its likely target first), and `next` names the best one once.

- **`describe_table`** returns the table's kind and file, its grain (`["id"]`, `"single row"` or unknown with the reason), whether its rows can be compared, one line per column (`name = meaning`, or `name = ? (reason)` when the meaning is unknown), the conditions in its own query once each (not once per column), the tables it reads and the tables that read it, columns nothing reads, and whether a saved data profile exists (with the resource to read).
- **`trace_column`** returns the one-hop sources, the ultimate source columns, the models the value passes through (nearest first) and the filters and joins that limit its rows, written **once per model**. `column` may name a field inside a STRUCT column (`payload.user.id`). `complete: false` plus an `unknown` entry means a step could not be followed, so the sources are a lower bound.
- **`impact`** takes `change` of `drop_column`, `rename_column`, `change_expression` or `drop_table` (default `drop_column` when a column is given, `drop_table` otherwise). It lists the models that break, change values, may change behaviour or break indirectly, nearest first, then the readers that could not be analysed (grouped by reason), and the files to open. `safe_to_delete` is always `unknown`: readers outside the project (query history, dashboards) are not visible.
- **`find_existing`** compares the table (or a proposed `SELECT` written against the project's tables) with every other table. It returns `verdict`: `found` (same meaning, contains or partial matches, best first), `rollup_possible` (a finer-grain table could be aggregated), `none_found` (with how many tables were compared) or `cannot_tell`. When nothing can be compared the answer is a few lines saying so, naming what blocks it (the target's own grain or column meanings, or the other tables') instead of listing every table that was skipped.
- **`find_tables`** takes a need in plain words (`need="daily revenue by customer"`), and optionally a `column` name (an exact name counts most) and a `kind` (`table`, `view`, `incremental`, `source`, or a role: `fact`, `dimension`, `bridge`). It returns up to five candidates, best first, each with its `score`, why it matched (`name`, `columns`, `tag`, `meaning of`, `saved profile columns`, `saved catalog`), how many of your words matched, its grain, how many models read it and what is unknown about it. The ranking is fixed word matching (lower case, singular, camelCase and underscores split, a shared start of four letters counts half) with fixed weights: a word in the table name 4, in a column name or tag 2, in the dataset, a tracked column meaning, a saved profile or the saved catalog 1, counted once at its best place; an exact `column` adds 6; ties go to the table more models read, then to the name. The same question always gives the same answer. Assertions are not offered. A table with an unknown grain or unknown column meanings can only be matched by name, and the answer says how many tables that is. `offset` pages the candidates. It reads only what the loaded project, the saved data profiles and the saved catalog hold: no BigQuery, no language model.
- **`compare_queries`** takes two queries written against the project's tables. It runs the structural and algebraic proof first (what `prove-sql-equivalent` runs), and when that does not settle it the SMT prover with the columns, keys and NOT NULL columns the project declares, which can also return a database on which the queries differ. `verdict` is `equivalent` (with `proof`: `unconditional`, or `given declared project facts`, and the `assumptions` the proof rests on), `not_equivalent` (with a `counterexample`: made-up rows per table, at most three each and clipped, and what each query returns on them) or `unknown` (with the reason; unknown is not a claim that the queries differ, and is never turned into a proof). Rows are compared as bags; `respect_row_order=true` also requires the same `ORDER BY` (only the structural proof checks that, so more pairs stay unknown). Column types and names are not compared: the answer's next step is `check_query` with a dry run. `time_limit` is the cap in seconds (default 10, from 1 to 60): the check runs in a forked process that is killed at the cap, and a check that does not finish is `unknown`. It touches no database and no network.
- **`check_query`** takes a draft `SELECT` written against the project's tables (a table name, `dataset.table`, or a Dataform `${ref("table")}`; see above) and runs the offline checks in one call. `types`: the output columns and their types ([type inference](type-inference.md)), the errors the query certainly has (an unknown column, an ambiguous name, a UNION of different widths) and **operands with no common type**, each naming both sides and a fix: a join key, comparison, `BETWEEN` or `IN` between `STRING` and `INT64` (or a quoted text against a number column, or an integer bound on a `STRING` column), `DATETIME` against `TIMESTAMP`, `UNION` branches that disagree by position, and a numeric operator or function with no signature for its arguments (`MOD(FLOAT64, INT64)`, `STRING + INT64`, `SUM(STRING)`); see [column types](#column-types-and-the-schema-snapshot). A column whose type is unknown is never guessed: `unknown` has `types of calendar.order_week unknown` with the reason, and when no operand check could be decided `types` is left out of `checked` and the answer says so. `ties`: places where the result can change from run to run because rows tie (`ROW_NUMBER` with a non-unique `ORDER BY`, `LIMIT` without a total order, `ANY_VALUE`, `ARRAY_AGG`, ...), judged with the grain of the project's tables as unique keys (`ties_judged_assuming` says which). `fan_out`: an `INNER` or `LEFT` join whose condition does not match every grain column of the joined table, a join without a condition, judged for project tables, CTEs and subqueries with a known grain; with a `SUM`, `COUNT` or `AVG` in the same `SELECT` the finding says the totals would count rows more than once. `RIGHT` and `FULL` joins and tables whose grain is unknown are listed under `unknown`. Each entry of `findings` is `{check, what, fix}`; `verdict` is `problems_found`, `no_problems_found` (the checks that ran found none; the answer says this is not a guarantee) or `cannot_check` (the draft does not parse, is not a `SELECT`, or has a `${...}` expression other than `ref()`, which the answer names). **The BigQuery dry run runs only with `dry_run=true` and an explicit `project`** (the project is never guessed; either argument alone is refused). It is BigQuery's free planning step: no data is read and nothing is billed. The answer then has `dry_run.estimated_bytes` against the `byte_cap` of Settings → Scopes (over the cap is a finding with its fix), BigQuery's own error as a finding if it rejects the query, and the column types BigQuery returns (`column_types_from`). With no credentials, no network or an unreachable BigQuery the answer says `not checked: <reason>` under `unknown` and still returns the offline checks. Without `dry_run` the tool never touches the network.
- **`list_tables`** is the whole-project question the per-table tools cannot answer. Each table is one line, `name | kind | reads N (+n src) | read by N | depth N`, where `name` is `dataset.table`, `reads` counts the other models of the project it reads (`+n src`: declared sources it also reads), `read by` counts the models that read it (assertions included) and `depth` is the number of models between it and a source (0 reads no model). With no argument it is the overview: counts of models, sources, models nothing reads and kinds, the commonest tags, then the first page. Filters combine: `kind`, `tag`, `folder` (a text inside the model's file path, such as `definitions/alerts`), `name_contains`, and the bounds `min_reads`, `max_reads`, `min_readers`, `max_readers` (`max_readers=0` lists what nothing reads) and `min_depth`. `sort` is `name` (default) or `readers`, `reads`, `depth`, largest first and ties by name. Assertions and operations without an output are not tables to use and are left out unless `kind` names them. An unknown `kind` is refused with the kinds the project has. It reads only the loaded project (no BigQuery, no model), and the same call always gives the same list. `offset` pages the list. `count_only=true` answers with the counts alone (about 150 to 350 bytes), for "how many" questions.
- **`clean_query`** runs the product's cleanup rules (`remove_trivial_predicates`, `remove_redundant_parentheses`, `deduplicate_ctes`, `remove_unused_ctes`, `remove_redundant_distinct`; see [rewrite rules](rewrite-rules.md)) on a draft (`sql`) or on a model's own file (`table`, read from inside the project folder and never written) and answers with a **diff**, not the whole query. `inline_single_use_ctes` is off unless `inline_ctes=true`, because it reprints the query as nested subqueries and changes its layout. Every change is checked by the equivalence prover and an independent checker, as `python -m kumosql rewrite-sql` does. `verdict` is `cleaned` (`changes` lists `rule xN`; `diff` is a unified diff with one line of context and whitespace kept, paged by `offset`; for a draft whose cleaned text is at most 1,200 characters, `cleaned_sql` is the text itself), `already_clean` (nothing to change; when some rules left the text as written because it holds a `${...}` expression other than `ref()`, pipe syntax or a Jinja tag, an `unknown` entry says so instead of claiming it is clean), `unproven` (something would change but a proof was not established: **no diff is offered**) or `cannot_clean` (the text does not parse, or the time cap `time_limit`, default 20 seconds and at most 60, was reached). It touches no database and no network.
- **`project_checks`** answers "is the repository itself consistent?" without a warehouse or a schema snapshot, from the loaded project only. Four checks, each a group with `count`, three `examples`, what it means and its `fix`: `declared_and_built` (a name declared as a source, by `declare()` or `type: "declaration"`, that the project also builds as a model: Dataform rejects the duplicate action name), `disabled_read_by_enabled` (a model with `disabled: true`, which Dataform compiles but never runs, that an enabled model or assertion reads: its table may never have been built or may be stale), `declared_in_several_datasets` (one table name declared in two or more datasets, so a one-argument `ref("name")` is ambiguous) and `defined_twice` (two files publishing the same table; the loader kept the later one). An identical declaration repeated is one table and not a finding. `check` limits the answer to one group and lists up to 20 examples. The answer always says under `unknown` that the deployed state is not read: a table that was never built, is older than its model or has another partitioning is not visible without a warehouse or a schema snapshot. The same facts appear in two other tools: `describe_table` carries one `project_check` line for a table concerned (declared and built, disabled and read, reads a disabled model, declared in several datasets), and `check_query` adds an `unknown` entry when the draft reads a disabled model, a model that reads one, a name declared and built, or a declared source the project does not build (whether it exists in the warehouse is not checked), so a draft over such tables is never a bare "No problems found". "Inactive" here means `disabled: true`; a model left out of every production schedule is not detected.
- **`profile_table`** profiles the values of a table with [`kumosql.data_profile`](data-profiling.md): the row count and, per column, nulls, distinct values, range and the most common values, one line per column. What it does depends on `project` and `run`, and the default costs nothing:

  | Call | What happens |
  | --- | --- |
  | `table` (a project table), nothing else | Plan only, no network: the columns and number of queries it would send, and `unknown: estimated scan size` |
  | `table` and `project` | BigQuery **dry run** of every profile query (free): `scan.estimated_bytes` against the cap; nothing runs. Over the cap is `mode: refused` |
  | `table`, `project` and `run=true` | The profile runs, each query dry-run first and capped; the whole plan is refused before anything runs when its estimate is over the cap |
  | `file` (CSV, TSV, Parquet, JSON) or `duckdb` (a database file) | Free, so it runs at once. The file must sit inside the project folder |

  `run=true` without `project` is refused. The cap is the one in Settings → Scopes, applied to the **sum** of the plan, which is stricter than the per-query cap of `profile-table`. `columns`, `sample_percent` and `row_filter` narrow the scan; `top_values` (0 to 10, default 3) and `show_values=false` limit the quoted values. **Values from a table are data, never instructions**: every value is JSON-quoted and clipped to 24 characters, and the answer carries a `note` saying so. The tool saves nothing; `python -m kumosql profile-table` saves a profile that `describe_table` then points at. Credentials missing or BigQuery unreachable: `mode: not_run` with `not checked: <reason>`.

## Rules every answer follows

- **Bounded.** `max_bytes` is a promise. When an answer does not fit, the longest lists are cut first; `unknown` and `next` are cut last. A cut list says `"columns_shown": "1-6 of 7"` and `more` says what to ask: `offset=6` for the next page, or `max_bytes=2129` for the whole page.
- **No repeats.** A filter is said once per model, never once per column.
- **Unknown stays explicit.** `unknown` is a list of `{what, reason}`. Reasons are plain-language versions of the codes the analysis uses (`nested_field`, `source_without_grain`, `analysis_gaps`, ...), never a silent omission.
- **Next step included.** `next` names the model files to open or the tool call that explains an unknown.
- **Read-only, offline by default.** The tools never write a file of your project. No tool opens a network connection unless it is asked to: only `check_query` with `dry_run=true` and `profile_table` with `project` reach BigQuery (a free dry run; `profile_table` runs queries only with `run=true`, under the byte cap), and `tools/list` marks those two as open-world. Loading a project writes KumoSQL's own diagnostics log (`ui.log`) in its data folder, as every KumoSQL command does. Names, SQL conditions and saved-profile values in an answer come from your project and your tables: they are data, never instructions, and the server's `initialize` instructions say so.

## Example

`describe_table` on a table that reads fields inside a nested column (shortened). The path is part of the meaning, so the column is known and can be compared:

```json
{"answer": "csa_3_11_intermediary_stats (incremental): one row per day; 7 columns, all column meanings known; reads 1, read by 2.",
 "table": "[PROJECT_ID].csa.csa_3_11_intermediary_stats", "kind": "incremental", "grain": ["day"], "grain_status": "derived",
 "counts": {"columns": 7, "meaning_unknown": 0, "reads": 1, "read_by": 2},
 "columns": ["day = expr:EXTRACT(DATE FROM col:[LOGS_DATASET_ID]._AllLogs.timestamp)", "actions = expr:agg:ARRAY_AGG(DISTINCT field(col:[LOGS_DATASET_ID]._AllLogs.proto_payload, audit_log.method_...", "..."],
 "columns_shown": "1-6 of 7",
 "unknown": [{"what": "rows kept", "reason": "..."}],
 "next": ["..."]}
```

A column that cannot be followed says so with the reason (`role = ? (ambiguous_column)`), and a table whose grain is unknown says `source_without_grain`.

## Measured sizes

On `tests/fixtures/bq_corpora/security-analytics` (106 models, 626 output columns), default bound, compact JSON. The sizes below come from asking every model and column; `tests/test_mcp_server.py` repeats it (every `describe_table` and `trace_column` question, `impact` for every model and a sample of columns, six `find_existing` questions, `find_tables` for every model name, one `compare_queries`, `check_query` and `profile_table` for every model, all with the network blocked) and fails when an answer exceeds 2,000 bytes.

| Tool | Questions asked | Median | 95th percentile | Largest |
| --- | ---: | ---: | ---: | ---: |
| `describe_table` | 106 (every model) | 1,726 B | 1,966 B | 1,998 B |
| `trace_column` | 626 (every column) | 782 B | 1,238 B | 1,984 B |
| `impact` | 318 (each model, plus two columns each) | 561 B | 911 B | 1,116 B |
| `find_existing` | 106 (every model) | 744 B | 1,729 B | 1,932 B |
| `find_tables` | 106 (each model's own name as the need) | 1,762 B | 1,944 B | 1,950 B |
| `compare_queries` | 2 (one proof, one counterexample) | 644 B | 865 B | 865 B |
| `check_query` | 106 (`SELECT *` from every model) | 820 B | 933 B | 956 B |
| `profile_table` | 106 (the plan for every model) | 538 B | 564 B | 577 B |

For comparison, the roadmap measured 141 KB for reading every `.sqlx` file, 1,197 KB for the `pipeline-report` JSON, and about 50 KB for the overlap search that compared 0 of 105 tables. The same question through `find_existing` is at most 1,932 bytes, and 744 bytes at the median. Latency is dominated by the one-time load; the figures come from one 4-core container and indicate scale only.

### What `find_existing` can decide on this project

Before and after closing the two analysis gaps (nested-field meanings, grain from declarations), asking `find_existing` about each of the 106 models:

| | Before | After |
| --- | ---: | ---: |
| Column meanings unknown (of 626) | 402 (351 `nested_field`) | 45 |
| Tables with grain `source_without_grain` | 41 | 41 |
| Models for which `find_existing` compared other tables | 3 | 54 |
| `find_existing` answers `found` / `none_found` | 2 / 1 | 43 / 11 |
| `find_existing` answers `cannot_tell` | 103 | 52 |

A decided answer compared 62 other tables at the median. All 43 `found` answers are `partial` matches (`low` confidence): no table on this project has the same filters as another, because most of them filter on the time they run (`CURRENT_TIMESTAMP()`), so the row scope cannot be compared. The remaining 45 unknown meanings are `unnest_element` (26: a value of an unnested array), `ambiguous_column` (13), `unresolved_column` (4) and `non_deterministic` (2).

The 41 `source_without_grain` tables all read `_AllLogs`, a log table the project declares without any key, and the project declares no `uniqueKey` assertion anywhere, so nothing in the project can give them a grain. Declaring `--assume-grain` for that table (an event id, for instance) removes the reason, but the grain stays unknown on this project because none of those views keeps the id column in its output (`grain_not_in_output`: 32; the 17 that unnest an array say so: a join that can multiply rows), so the number of comparable tables stays at 54. The `cannot_tell` answers are the 49 tables with no known grain (31 of them with all meanings known) and 3 with a known grain but an unknown meaning.

## Column types and the schema snapshot

A Dataform project stores no column types, so there was nothing for `check_query` to compare. Give the server a **schema snapshot**: a file with one row per column in the shape of BigQuery's `INFORMATION_SCHEMA.COLUMNS`, produced once outside the server (a free metadata query run by someone with warehouse access) and refreshed when tables change. The server only reads the file; it never fetches a schema.

```
python -m kumosql mcp PROJECT --schema-snapshot columns.csv
```

```
table_catalog,table_schema,table_name,column_name,data_type,is_nullable
my-project,sales,calendar,order_week,STRING,YES
my-project,sales,orders,order_id,INT64,NO
```

CSV (with a header) and JSON lines (or one JSON array of objects) are read; the headers `catalog`, `schema`, `table`, `column`, `data_type` and `is_nullable` work too, and `ordinal_position` orders the columns. A bad file stops the server at start with the reason. A row without a table, column or type, or a repeated column, is skipped and counted.

How the types reach a query ([`kumosql.project_types`](../src/kumosql/project_types.py), cached once in the `Session`):

1. A snapshot table is joined to a project table or declared source by name, case-insensitively: `project.dataset.table`, or the trailing parts when either side lacks the project. A name that fits several tables, or none, is not guessed (it is kept in `unmatched`). Rows of tables the project does not use are dropped while the file is read and never kept (`ignored` counts them), so an unscoped warehouse export of hundreds of thousands of rows is fine: the loader streams the file at about a second per 100,000 rows. A draft that reads a table the project does not define gets no types for it (it stays under `not in the project`).
2. A **source** takes the snapshot's types, else those of its `--source-schema` declaration.
3. A **model** takes the snapshot's types when the snapshot lists its table (the deployed table is what a query runs against), and otherwise the types [type inference](type-inference.md) derives from the model's own query, reading the types of the tables above it. Models are typed in dependency order, once. A model with a column it cannot type gets `None` for that column only, and a model it cannot read at all (a `${...}` expression other than `ref()`, a script) has no types.

The loader is a library for other tools too (for example the schema-drift checks); none of it writes a file:

| Call | Returns |
| --- | --- |
| `Session.schema_snapshot()` | the `SchemaSnapshot` the server started with (only the rows of tables the project uses), or `None`; `Session.snapshot_error` says why a file could not be read |
| `Session.column_types()` | the typed project, computed once and cached: `type(key, column)`, `nullable(key, column)`, `columns(key)`, `reason(key, column, is_model)`, `origin[key]` (`snapshot`, `declared` or `derived`), `unmatched`, `models_typed` / `models_untyped` |
| `Session.column_type(key, column)` | the `GType` of a column of a project table, or `None` |
| `Session.column_nullable(key, column)` | `True` (may be NULL), `False` (never; the snapshot's `is_nullable = NO`, or a literal or `COUNT` in a model's query) or `None` (not known) |
| `Session(schema_snapshot=path_or_SchemaSnapshot)` | the same option for code that builds a session |
| `kumosql.schema_snapshot.load_schema_snapshot(path, keep=None)` | reads one file, streaming; `keep(catalog, schema, table)` says which tables' rows to keep (`kumosql.project_types.snapshot_filter(keys)` is the project's); `SchemaSnapshot.tables()`, `columns_of(name)`, `types_of(name)`, `skipped`, `ignored` |
| `check_schema_snapshot(path)` | reads the header and the first rows only and raises `SchemaSnapshotError` for a file that is not a snapshot (the server calls it at start) |

Types for models are cached in the session, so a project of about 400 models and 1,000 declared sources is typed once (`tests/test_check_query_types.py` sizes it with a counter, not a clock). A JSON array file is parsed whole; use JSON lines for large files.

## Saved data profiles

The server also serves the resources that [`profile-mcp`](data-profiling.md#agents-the-mcp-server) serves: two per saved profile, `kumosql://data-profile/<name>/summary.md` and `profile.json`, read-only, from the data folder or `--profiles DIR`. `python -m kumosql profile-mcp` keeps working unchanged (resources only, no tools). `describe_table` points at the saved profile of a table when one exists.

## Adding a tool

A tool is one function registered in `kumosql.mcp_tools` (or another module listed in `TOOL_MODULES` in `kumosql/mcp_server.py`):

```python
@tool("find_tables", "Find a table", "Which table should I use?", {"need": {"type": "string"}}, required=("need",))
def find_tables(session, args):
    answer = Answer("...")          # summary, then .fields, .counts, .add_list(...), .unknown(...), .next(...)
    return answer
```

`session` is the loaded project (`session.pipeline()`, cached `profiles()`, `roles()`, `records()`, name resolution with `session.resolve(...)`). The size bound, paging, argument checking and error answers come from `kumosql.mcp_answers`, so a tool only builds the answer. Raise `ToolError(message, [next steps])` to refuse a question with a way forward.

## Limits

- On the 106-model public project 45 of 626 column meanings are unknown and 49 tables have no known grain (41 `source_without_grain`, 8 `grain_not_in_output`); `find_existing` there answers for 54 of 106 tables and says `cannot_tell` for the other 52, naming the blocker. Tables that read the same log table cannot be told apart by grain unless the project, or `--assume-grain`, names the columns that identify an event and the view keeps them. Values of an unnested array (`UNNEST(...) AS item`, then `item.name`) and fields of a computed value stay unknown. See [what `find_existing` can decide](#what-find_existing-can-decide-on-this-project).
- A declared grain is a claim. `assertions: {uniqueKey}` is checked by Dataform when the assertions run; `--assume-grain` is not checked by anything, so a wrong one makes `find_existing` match tables that differ. Answers show it as `declared`.
- `impact` does not read job history yet, so readers seen only in query history are not listed, and `safe_to_delete` stays `unknown`.
- The BigQuery paths (`check_query` with `dry_run=true`, `profile_table` with `project`) are tested against stubs only: no test reaches BigQuery. `profile_table` with `run=true` is the one call that can bill, and it is capped, so try it on a small table first.
- `check_query` does not run the query or compare its rows with anything. Without a schema snapshot a model's column types are only what its own query fixes (a literal, a `CAST`, a function of those); columns read from a table with no declared types stay unknown, and a dry run returns real types. The operand checks never compare two different numeric types, STRUCT, ARRAY, JSON or GEOGRAPHY values, `CASE`/`COALESCE` branches or the arguments of functions other than the numeric ones listed above, so a clean answer is not a type-correct query. The fan-out check needs a known grain; on the public project 41 tables read tables the project does not define and have none.
- `find_tables` matches words, not meanings: "revenue" does not find a table whose columns are called `sales`. On the public project the top candidate for each model's own name is that model, and a need such as "intermediary activity per day" finds the right table by its name and its `day` column, but 351 of 626 column meanings are unknown there, so a better table may hide behind a name the words do not match.
- `compare_queries` proves what the prover can prove today; most real rewrites that change a join or an aggregate come back `unknown`. A counterexample is a small made-up database, not data from your tables.
- Answers are measured in bytes of JSON, not tokens.
- The server handles one request at a time on stdin, and does not support JSON-RPC batches or cancellation.
