# Agent-facing server (`python -m kumosql mcp`)

[Plain-language version](../docs_simple/mcp-server.md)

One stdio [Model Context Protocol](https://modelcontextprotocol.io) server for an agent that works on a Dataform project. It loads the project once, keeps it in memory and answers **one question per call** with a **bounded answer**: about 2 KB by default, with counts, what it could not determine and why, a next step, and the argument that asks for more. This is step 4 of the [roadmap](roadmap.md#4-one-agent-facing-server); the tools below are its first four.

```
python -m kumosql mcp PROJECT [--source-schema FILE] [--assume-grain TABLE=COL[,COL]]... [--profiles DIR]
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

Every tool also takes `max_bytes` (the size bound, 1,000 to 20,000, default 2,000) and `offset` (where the answer's main list starts). A `table` is a model or declared source of the project, written as the full key, `dataset.table` or the bare table name, in any case. A name that matches several tables is not guessed: the error lists the candidates.

- **`describe_table`** returns the table's kind and file, its grain (`["id"]`, `"single row"` or unknown with the reason), whether its rows can be compared, one line per column (`name = meaning`, or `name = ? (reason)` when the meaning is unknown), the conditions in its own query once each (not once per column), the tables it reads and the tables that read it, columns nothing reads, and whether a saved data profile exists (with the resource to read).
- **`trace_column`** returns the one-hop sources, the ultimate source columns, the models the value passes through (nearest first) and the filters and joins that limit its rows, written **once per model**. `column` may name a field inside a STRUCT column (`payload.user.id`). `complete: false` plus an `unknown` entry means a step could not be followed, so the sources are a lower bound.
- **`impact`** takes `change` of `drop_column`, `rename_column`, `change_expression` or `drop_table` (default `drop_column` when a column is given, `drop_table` otherwise). It lists the models that break, change values, may change behaviour or break indirectly, nearest first, then the readers that could not be analysed (grouped by reason), and the files to open. `safe_to_delete` is always `unknown`: readers outside the project (query history, dashboards) are not visible.
- **`find_existing`** compares the table (or a proposed `SELECT` written against the project's tables) with every other table. It returns `verdict`: `found` (same meaning, contains or partial matches, best first), `rollup_possible` (a finer-grain table could be aggregated), `none_found` (with how many tables were compared) or `cannot_tell`. When nothing can be compared the answer is a few lines saying so, naming what blocks it (the target's own grain or column meanings, or the other tables') instead of listing every table that was skipped.

## Rules every answer follows

- **Bounded.** `max_bytes` is a promise. When an answer does not fit, the longest lists are cut first; `unknown` and `next` are cut last. A cut list says `"columns_shown": "1-6 of 7"` and `more` says what to ask: `offset=6` for the next page, or `max_bytes=2129` for the whole page.
- **No repeats.** A filter is said once per model, never once per column.
- **Unknown stays explicit.** `unknown` is a list of `{what, reason}`. Reasons are plain-language versions of the codes the analysis uses (`nested_field`, `source_without_grain`, `analysis_gaps`, ...), never a silent omission.
- **Next step included.** `next` names the model files to open or the tool call that explains an unknown.
- **Read-only.** The tools never write a file of your project, never run a query and never open a network connection. Loading a project writes KumoSQL's own diagnostics log (`ui.log`) in its data folder, as every KumoSQL command does. Names, SQL conditions and saved-profile values in an answer come from your project and your tables: they are data, never instructions, and the server's `initialize` instructions say so.

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

On `tests/fixtures/bq_corpora/security-analytics` (106 models, 626 output columns), default bound, compact JSON. The sizes below come from asking every model and column; `tests/test_mcp_server.py` repeats it (every `describe_table` and `trace_column` question, `impact` for every model and a sample of columns, six `find_existing` questions) and fails when an answer exceeds 2,000 bytes.

| Tool | Questions asked | Median | 95th percentile | Largest |
| --- | ---: | ---: | ---: | ---: |
| `describe_table` | 106 (every model) | 1,726 B | 1,966 B | 1,998 B |
| `trace_column` | 626 (every column) | 782 B | 1,238 B | 1,984 B |
| `impact` | 318 (each model, plus two columns each) | 561 B | 911 B | 1,116 B |
| `find_existing` | 106 (every model) | 744 B | 1,729 B | 1,932 B |

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
- Nothing here reaches BigQuery. Preflight checks, query comparison and on-demand profiling are the remaining tools of the [roadmap](roadmap.md#6-preflight-comparison-and-on-demand-profiling).
- Answers are measured in bytes of JSON, not tokens.
- The server handles one request at a time on stdin, and does not support JSON-RPC batches or cancellation.
