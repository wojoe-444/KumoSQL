# Agent-facing server: analyst questions

[Plain-language version](../../docs_simple/evals/agent-floor.md)

The product's own floor, next to the correctness tests ([roadmap](../roadmap.md) step 5). It scores how much an agent has to read, how many calls it makes and how long each takes when it asks the [agent-facing server](../mcp-server.md) the questions an analyst asks. It is the measure for analyst-facing work: a change that makes an answer bigger than its bound fails here even when every correctness test passes.

## What is asked

About twenty questions on the largest public Dataform project in the fixtures, `tests/fixtures/bq_corpora/security-analytics` (106 models, 626 output columns). Each is one task: the calls an agent makes to answer it.

| Group | Questions |
| --- | --- |
| What is this table? | the most-read table, a table nothing reads, an incremental table, the widest table, the table that reads the most others, a table whose column meanings are unknown |
| Where does this value come from? | a column of the most-read table, of a middle table, the last column of the widest table, a column that reads a nested field, a column of an incremental table |
| What breaks if I change this? | drop a column of the most-read table, rename a column of a middle table, change how a column is computed, drop the most-read table, a column nothing reads |
| Does one already exist? | a table like the most-read one, a draft query, a computation that appears in several models (`find_existing` with `expression`: copies and variants), a table or view that carries two given columns (`find_tables` with `has_columns`) |
| Several calls | can I delete a table nothing reads (describe, then impact), orient me in an unfamiliar table (describe, trace, impact) |

The tables and columns are chosen by fixed rules from the project's own table cards (most readers, no readers, widest, and so on), so the same questions are asked every run. Questions for tools that do not exist yet are listed as `waiting` and do not fail. The questions for `find_tables`, `profile_table`, `check_query` and `compare_queries` are in the list (no BigQuery is touched: the dry-run and run paths are not part of this floor).

## What is measured and what fails the floor

For each task: the number of calls, the bytes of every answer (compact JSON, as the server sends it), and the latency of each call after the one-time project load.

| Bound | Value | Why |
| --- | ---: | --- |
| Bytes of one answer | 2,000 | the server's default; the roadmap's "about 2 KB" |
| Calls per task | 3 | an agent should not need a long conversation for one question |
| Latency of one call after the load | 1,000 ms | a regression guard, not a benchmark of the machine; the median is recorded |
| An answer that is an error | none | a question the server cannot answer is a failure here |

The run also measures the alternatives on the same project, for scale: the bytes of every `.sqlx` file and of the `pipeline-report` JSON. `tests/test_agent_floor.py` additionally requires a median task to cost under 2% of the `.sqlx` bytes and under 0.5% of the report.

## Run it

```
python tools/agent_floor.py                  # print every task
python tools/agent_floor.py --json           # the full result
python tools/agent_floor.py --write-results  # write benchmarks/results/agent-floor.json and regenerate the README scoreboard
```

The result file is `agent-floor`. The test is `tests/test_agent_floor.py` (an eval floor, run nightly).

## Results

With all fourteen tools: 41 of 41 questions inside their bounds, largest answer 1,995 bytes, median task 1,037 bytes in total, at most 3 calls, median latency under 7 ms per task after a 9 s load (on a busy machine) (the first run, with four tools, was 20 of 20, median 1,036 bytes; three `list_tables` and two `clean_query` questions took it from 26, one `project_checks` question made it 32 and one `check_query` question about a reserved word used as an alias, added with the static checks, 33, the expression and link-table questions 35, one plan-only `join_profile` question 36, and two `rows_filtered` questions 38, one `check_query` question about comparing column types 39, one about the silent UTC default of `CURRENT_DATE()` 40, and one `deployed_status` question 41; the fixture has no schema snapshot, so that answer is the short `unknown` one). Reading every `.sqlx` file costs 141,354 bytes and the `pipeline-report` JSON 894,869 bytes. Numbers are bytes of JSON, not tokens.

## Limits of the evidence

- One public project with no source schemas and no job history, so `impact` cannot read readers seen only in query history and `safe_to_delete` stays unknown.
- Many answers say `unknown` with a reason (45 of 626 column meanings are unknown on this project, and `find_existing` can compare 54 of its 106 models; see [the agent server's limits](../mcp-server.md#limits)). An honest unknown fits its bound, so this floor does not measure how useful an answer is, only how big and how fast. Correctness is tested in `tests/test_mcp_server.py`.
- Calls are made in process, so transfer over stdio is not included.
- No language model is involved. How many turns and tokens a real agent spends with and without the server is the [field trial](../roadmap.md#7-field-trial), a periodic report and never a gate.
