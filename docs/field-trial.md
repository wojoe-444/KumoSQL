# Field trial: analyst tasks with and without the agent server

Step 7 of the [roadmap](roadmap.md). Ten analyst tasks on a public Dataform project, each done by an agent twice: once with the [agent-facing server](mcp-server.md) and once with only files, search and the existing command line. It counts tool calls and tokens and checks the answers. An agent is in the loop, so this is a **periodic report, never a gate**; only the unit tests of the harness run in the suite (`tests/test_field_trial.py`).

## How it runs

`tools/field_trial.py` has four parts:

- `tasks` prints the ten tasks with their expected answers (`--json` for machines).
- `relay SOCKET` keeps the server loaded behind a unix socket, so each `ask` is one warm call and the agent's shell command is the only difference from a real MCP client.
- `ask SOCKET TOOL key=value ...` makes one tool call.
- `score TASK ANSWER` compares an answer with the expected one (exact set match, otherwise precision and recall).

The project is the 106-model security-analytics project in `tests/fixtures/bq_corpora/`. Tasks T01 to T08 are about the model graph; their expected answers come from an independent regular-expression reading of the `ref("...")` calls in the `.sqlx` files, not from KumoSQL. T09 and T10 are about columns; their expected answers come from KumoSQL's own analysis, so they measure agreement with it, not independent truth.

Each agent was a fresh Sonnet subagent with a short prompt and one question. The "without" agents could use any file tool and `python -m kumosql pipeline-report`; the "with" agents were pointed at the four tools the server had at the time and were allowed to read files as well.

## Results (2026-11-10, server with four tools)

| Task | Question | With server: calls / tokens | Without: calls / tokens | Correct (with / without) |
| --- | --- | --- | --- | --- |
| T01 | Who reads the busiest model? | 2 / 43,942 | 4 / 44,721 | yes / yes |
| T02 | What does the deepest model depend on? | 3 / 44,705 | 5 / 48,061 | yes / yes |
| T03 | How many models read the raw log source? | 2 / 44,657 | 5 / 47,684 | yes / yes |
| T04 | Which models read two or more other models? | 5 / 52,913 (did not use the server) | 6 / 51,543 | yes / yes |
| T05 | How many models does nobody read? | 8 / 60,445 (did not use the server) | 6 / 49,997 | yes / yes |
| T06 | What breaks if the busiest model is deleted? | 3 / 43,918 | 6 / 46,640 | yes / yes |
| T07 | What is the declared unique key of a model? | 3 / 44,765 | 3 / 46,893 | yes / yes |
| T08 | Which alerts read another model? | 3 / 47,465 (did not use the server) | 6 / 49,677 | yes / yes |
| T09 | What breaks if the first column of a model is dropped? | 4 / 45,210 | 5 / 47,641 | yes / yes |
| T10 | Which source columns feed the last column of a report? | 4 / 46,028 | 6 / 48,428 | yes* / partly |

Token counts are what the harness reports for the whole subagent, so each includes a base of roughly 40,000 tokens that is the same for every run. Only the differences between a pair mean anything. The base cost dominates these small tasks.

\* Strict scoring marks both T10 answers wrong. The "with" agent gave `[LOGS_DATASET_ID]._AllLogs.json_payload.start_time` and the reference is `[LOGS_PROJECT_ID].[LOGS_DATASET_ID]._AllLogs.json_payload.start_time`: it dropped the placeholder project prefix, so the nesting and column are right and the string is not. The "without" agent answered `_AllLogs.json_payload`, which is the right column but stops before the field `start_time`.

## What it shows

- **Fewer calls on column questions, a little less on graph questions.** On the seven pairs where the agent used the server, calls were lower or equal every time (3.0 against 4.9 on average) and tokens lower by about 800 to 3,400. With ten small tasks and one run each, that is a direction, not a measured effect.
- **Same correctness on graph questions.** Both ways were right on every task from T01 to T09. The tasks are small enough that grepping works. The server's value shows when the question needs the column-level analysis (T10), where the grep route stops one level short.
- **Gaps in the server.** On three tasks (T04, T05, T08) the agent with the server ignored it and read files, because none of the four tools lists models or answers a count over the whole graph. In T04 and T05 those runs cost as many or more calls than the file route. The remaining tools of roadmap step 6 and a `list_models` style question are where to look next.
- **Honest caveats.** One run per cell, one model, no BigQuery dataset behind the project (the trial has no cloud credentials), tasks written by the same team that wrote the server. Ten tasks cannot tell a 10% effect from noise.

## Re-running

```
python tools/field_trial.py relay /tmp/ft.sock &
python tools/field_trial.py tasks
```

Give a fresh agent the question and either `python tools/field_trial.py ask /tmp/ft.sock TOOL key=value ...` or the file tools, then run `python tools/field_trial.py score T01 '["a", "b"]'` on its answer. Re-run it after a change to the server and compare against the table above; update the table and its date in the same pull request.
