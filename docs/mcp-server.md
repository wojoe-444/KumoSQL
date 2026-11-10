# Agent-facing server (`python -m kumosql mcp`)

[Plain-language version](../docs_simple/mcp-server.md)

One stdio [Model Context Protocol](https://modelcontextprotocol.io) server for an agent that works on a Dataform project. It loads the project once, keeps it in memory and answers **one question per call** with a **bounded answer**: about 2 KB by default, with counts, what it could not determine and why, a next step, and the argument that asks for more. This is step 4 of the [roadmap](roadmap.md#4-one-agent-facing-server) (the first four tools) and step 6 ([preflight, comparison and on-demand profiling](roadmap.md#6-preflight-comparison-and-on-demand-profiling), the rest).

```
python -m kumosql mcp PROJECT [--source-schema FILE] [--profiles DIR]
python -m kumosql mcp --git URL [--branch B] [--refresh]
python -m kumosql mcp                      # saved data profiles only, no project
```

`PROJECT` is what `pipeline-report` takes: a Dataform project folder, a folder of `.sql` files or the JSON of `dataform compile --json`. The server answers `initialize` at once and parses and analyses the project in the background (about 3 seconds for the 106-model public project in the fixtures); the first question waits for it, and every later one is served from memory (a column trace or an impact answer takes 10 milliseconds or less). Register it with an MCP client as the command `python -m kumosql mcp /path/to/project`. Messages are JSON-RPC 2.0, one per line; progress logs go to stderr, never stdout.

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

Every tool also takes `max_bytes` (the size bound, 1,000 to 20,000, default 2,000) and `offset` (where the answer's main list starts). A `table` is a model or declared source of the project, written as the full key, `dataset.table` or the bare table name, in any case. A name that matches several tables is not guessed: the error lists the candidates.

- **`describe_table`** returns the table's kind and file, its grain (`["id"]`, `"single row"` or unknown with the reason), whether its rows can be compared, one line per column (`name = meaning`, or `name = ? (reason)` when the meaning is unknown), the conditions in its own query once each (not once per column), the tables it reads and the tables that read it, columns nothing reads, and whether a saved data profile exists (with the resource to read).
- **`trace_column`** returns the one-hop sources, the ultimate source columns, the models the value passes through (nearest first) and the filters and joins that limit its rows, written **once per model**. `column` may name a field inside a STRUCT column (`payload.user.id`). `complete: false` plus an `unknown` entry means a step could not be followed, so the sources are a lower bound.
- **`impact`** takes `change` of `drop_column`, `rename_column`, `change_expression` or `drop_table` (default `drop_column` when a column is given, `drop_table` otherwise). It lists the models that break, change values, may change behaviour or break indirectly, nearest first, then the readers that could not be analysed (grouped by reason), and the files to open. `safe_to_delete` is always `unknown`: readers outside the project (query history, dashboards) are not visible.
- **`find_existing`** compares the table (or a proposed `SELECT` written against the project's tables) with every other table. It returns `verdict`: `found` (same meaning, contains or partial matches, best first), `rollup_possible` (a finer-grain table could be aggregated), `none_found` (with how many tables were compared) or `cannot_tell`. When nothing can be compared the answer is a few lines saying so, naming what blocks it (the target's own grain or column meanings, or the other tables') instead of listing every table that was skipped.
- **`find_tables`** takes a need in plain words (`need="daily revenue by customer"`), and optionally a `column` name (an exact name counts most) and a `kind` (`table`, `view`, `incremental`, `source`, or a role: `fact`, `dimension`, `bridge`). It returns up to five candidates, best first, each with its `score`, why it matched (`name`, `columns`, `tag`, `meaning of`, `saved profile columns`, `saved catalog`), how many of your words matched, its grain, how many models read it and what is unknown about it. The ranking is fixed word matching (lower case, singular, camelCase and underscores split, a shared start of four letters counts half) with fixed weights: a word in the table name 4, in a column name or tag 2, in the dataset, a tracked column meaning, a saved profile or the saved catalog 1, counted once at its best place; an exact `column` adds 6; ties go to the table more models read, then to the name. The same question always gives the same answer. Assertions are not offered. A table with an unknown grain or unknown column meanings can only be matched by name, and the answer says how many tables that is. `offset` pages the candidates. It reads only what the loaded project, the saved data profiles and the saved catalog hold: no BigQuery, no language model.
- **`compare_queries`** takes two queries written against the project's tables. It runs the structural and algebraic proof first (what `prove-sql-equivalent` runs), and when that does not settle it the SMT prover with the columns, keys and NOT NULL columns the project declares, which can also return a database on which the queries differ. `verdict` is `equivalent` (with `proof`: `unconditional`, or `given declared project facts`, and the `assumptions` the proof rests on), `not_equivalent` (with a `counterexample`: made-up rows per table, at most three each and clipped, and what each query returns on them) or `unknown` (with the reason; unknown is not a claim that the queries differ, and is never turned into a proof). Rows are compared as bags; `respect_row_order=true` also requires the same `ORDER BY` (only the structural proof checks that, so more pairs stay unknown). Column types and names are not compared: the answer's next step is `check_query` with a dry run. `time_limit` is the cap in seconds (default 10, from 1 to 60): the check runs in a forked process that is killed at the cap, and a check that does not finish is `unknown`. It touches no database and no network.
- **`check_query`** takes a draft `SELECT` written against the project's tables (real names, no `${ref()}`) and runs the offline checks in one call. `types`: the output columns and their types ([type inference](type-inference.md); a project model has no stored column types, so most read `?`) and the errors the query certainly has (an unknown column, an ambiguous name, a UNION of different widths). `ties`: places where the result can change from run to run because rows tie (`ROW_NUMBER` with a non-unique `ORDER BY`, `LIMIT` without a total order, `ANY_VALUE`, `ARRAY_AGG`, ...), judged with the grain of the project's tables as unique keys (`ties_judged_assuming` says which). `fan_out`: an `INNER` or `LEFT` join whose condition does not match every grain column of the joined table, a join without a condition, judged for project tables, CTEs and subqueries with a known grain; with a `SUM`, `COUNT` or `AVG` in the same `SELECT` the finding says the totals would count rows more than once. `RIGHT` and `FULL` joins and tables whose grain is unknown are listed under `unknown`. Each entry of `findings` is `{check, what, fix}`; `verdict` is `problems_found`, `no_problems_found` (the checks that ran found none; the answer says this is not a guarantee) or `cannot_check` (the draft does not parse, is not a `SELECT`, or has `${...}`). **The BigQuery dry run runs only with `dry_run=true` and an explicit `project`** (the project is never guessed; either argument alone is refused). It is BigQuery's free planning step: no data is read and nothing is billed. The answer then has `dry_run.estimated_bytes` against the `byte_cap` of Settings → Scopes (over the cap is a finding with its fix), BigQuery's own error as a finding if it rejects the query, and the column types BigQuery returns (`column_types_from`). With no credentials, no network or an unreachable BigQuery the answer says `not checked: <reason>` under `unknown` and still returns the offline checks. Without `dry_run` the tool never touches the network.
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

`describe_table` on a table whose lineage reads a nested field (shortened):

```json
{"answer": "csa_3_11_intermediary_stats (incremental): one row per day; 7 columns, 2 of 7 column meanings unknown; reads 1, read by 2.",
 "table": "[PROJECT_ID].csa.csa_3_11_intermediary_stats", "kind": "incremental", "grain": ["day"], "grain_status": "derived",
 "counts": {"columns": 7, "meaning_unknown": 2, "reads": 1, "read_by": 2},
 "columns": ["day = expr:EXTRACT(DATE FROM col:[LOGS_DATASET_ID]._AllLogs.timestamp)", "actions = ? (nested_field)", "..."],
 "columns_shown": "1-6 of 7",
 "unknown": [{"what": "2 column meanings: actions, actors", "reason": "nested_field: reads a field inside a STRUCT or JSON column, whose meaning is not tracked yet"}],
 "next": ["trace_column table=... column=actions shows how far its source can be followed."],
 "more": "Ask again with offset=6 for the next columns, or max_bytes=2129 for everything on this page."}
```

## Measured sizes

On `tests/fixtures/bq_corpora/security-analytics` (106 models, 626 output columns), default bound, compact JSON. The sizes below come from asking every model and column; `tests/test_mcp_server.py` repeats it (every `describe_table` and `trace_column` question, `impact` for every model and a sample of columns, six `find_existing` questions, `find_tables` for every model name, one `compare_queries`, `check_query` and `profile_table` for every model, all with the network blocked) and fails when an answer exceeds 2,000 bytes.

| Tool | Questions asked | Median | 95th percentile | Largest |
| --- | ---: | ---: | ---: | ---: |
| `describe_table` | 106 (every model) | 1,658 B | 1,954 B | 1,986 B |
| `trace_column` | 626 (every column) | 782 B | 1,239 B | 1,984 B |
| `impact` | 318 (each model, plus two columns each) | 561 B | 914 B | 1,116 B |
| `find_existing` | 20 | 822 B | 823 B | 823 B |
| `find_tables` | 106 (each model's own name as the need) | 1,762 B | 1,944 B | 1,950 B |
| `compare_queries` | 2 (one proof, one counterexample) | 644 B | 865 B | 865 B |
| `check_query` | 106 (`SELECT *` from every model) | 820 B | 933 B | 956 B |
| `profile_table` | 106 (the plan for every model) | 538 B | 564 B | 577 B |

For comparison, the roadmap measured 141 KB for reading every `.sqlx` file, 1,197 KB for the `pipeline-report` JSON, and about 50 KB for the overlap search that compared 0 of 105 tables. The same question through `find_existing` is 822 bytes. Latency is dominated by the one-time load; the figures come from one 4-core container and indicate scale only.

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

- On the 106-model public project 351 of 626 column meanings are unknown because they read fields inside nested columns, and 41 tables have `source_without_grain` because they read tables the project does not define. A table with an unknown grain or unknown meanings cannot be compared, so `find_existing` there returns `cannot_tell` (accurately, and in 800 bytes). Closing those gaps means tracking meanings through STRUCT fields and source schemas; it is not part of this step.
- `impact` does not read job history yet, so readers seen only in query history are not listed, and `safe_to_delete` stays `unknown`.
- The BigQuery paths (`check_query` with `dry_run=true`, `profile_table` with `project`) are tested against stubs only: no test reaches BigQuery. `profile_table` with `run=true` is the one call that can bill, and it is capped, so try it on a small table first.
- `check_query` does not run the query or compare its rows with anything, and models have no stored column types, so a dry run is what returns real types. The fan-out check needs a known grain; on the public project 41 tables read tables the project does not define and have none.
- `find_tables` matches words, not meanings: "revenue" does not find a table whose columns are called `sales`. On the public project the top candidate for each model's own name is that model, and a need such as "intermediary activity per day" finds the right table by its name and its `day` column, but 351 of 626 column meanings are unknown there, so a better table may hide behind a name the words do not match.
- `compare_queries` proves what the prover can prove today; most real rewrites that change a join or an aggregate come back `unknown`. A counterexample is a small made-up database, not data from your tables.
- Answers are measured in bytes of JSON, not tokens.
- The server handles one request at a time on stdin, and does not support JSON-RPC batches or cancellation.
