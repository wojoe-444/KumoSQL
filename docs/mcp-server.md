# Agent-facing server (`python -m kumosql mcp`)

[Plain-language version](../docs_simple/mcp-server.md)

One stdio [Model Context Protocol](https://modelcontextprotocol.io) server for an agent that works on a Dataform project. It loads the project once, keeps it in memory and answers **one question per call** with a **bounded answer**: about 2 KB by default, with counts, what it could not determine and why, a next step, and the argument that asks for more. This is step 4 of the [roadmap](roadmap.md#4-one-agent-facing-server); the tools below are its first four.

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

On `tests/fixtures/bq_corpora/security-analytics` (106 models, 626 output columns), default bound, compact JSON. `tests/test_mcp_server.py` asks every question below for every model or column and fails when an answer exceeds 2,000 bytes.

| Tool | Questions asked | Median | 95th percentile | Largest |
| --- | ---: | ---: | ---: | ---: |
| `describe_table` | 106 (every model) | 1,658 B | 1,954 B | 1,986 B |
| `trace_column` | 626 (every column) | 782 B | 1,239 B | 1,984 B |
| `impact` | 318 (each model, plus two columns each) | 561 B | 914 B | 1,116 B |
| `find_existing` | 20 | 822 B | 823 B | 823 B |

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
- Nothing here reaches BigQuery. Preflight checks, query comparison and on-demand profiling are the remaining tools of the [roadmap](roadmap.md#6-preflight-comparison-and-on-demand-profiling).
- Answers are measured in bytes of JSON, not tokens.
- The server handles one request at a time on stdin, and does not support JSON-RPC batches or cancellation.
