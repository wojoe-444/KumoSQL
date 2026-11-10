# KumoSQL documentation

[Plain-language version](../docs_simple/README.md)

Working on KumoSQL itself? Read the [roadmap](roadmap.md) first: why work stalled in October 2026, what was decided, and what to build next, in order.

Start with [Getting started](getting-started.md): install, a first verified rewrite, a pipeline impact report and the browser UI. The [README](../README.md) has the benchmark scoreboard, a summary of every feature and the CLI table.

## Using KumoSQL

| Page | What it covers |
| --- | --- |
| [Local browser UI](ui.md) | Starting the UI, its pages, saved state, scopes, data sources and tags |
| [Connecting Dataform repositories](dataform-repositories.md) | Private repositories through local `git`, the data folder, logging and diagnostics, production schedules |
| [Rewrite rules](rewrite-rules.md) | The rule registry and the subquery lifter |
| [GoogleSQL reference evaluator](gsql-eval.md) | A pure-Python GoogleSQL interpreter that returns BigQuery's rows or declines: values, coverage, errors and guards |
| [Whole-pipeline analysis](pipeline-analysis.md) | Loading a project, lineage and impact, table profiles, work already done elsewhere, comparing outputs |
| [Cost, change reports and the BigQuery dry run](cost-and-change-reports.md) | Dry-run checks, cost attribution, change reports and refactoring proposals |
| [Refactor](refactor.md) | Protected and editable tables, searching for simpler pipelines, and folding chosen tables into one |
| [Shared models](shared-models.md) | Moving a CTE repeated across Dataform models into one shared model, as a patch checked by the prover |
| [Table minimization](table-minimization.md) | The lowest-complexity set of tables that keeps every protected table proved unchanged |
| [Project reduction](project-reduction.md) | The smallest Dataform project that still produces the outputs you keep, as a proved patch on the `.sqlx` files |
| [GoogleSQL type inference](type-inference.md) | The output column types of a BigQuery query, STRUCT and ARRAY included, without running it: the catalog, unknown, coercion and supertype rules, set operations, pipe syntax, findings and the scan tool |
| [Data profiling](data-profiling.md) | Per-column statistics of a table's values (nulls, distinct, min, max, quartiles, most common values) on BigQuery or DuckDB, saved profiles, and the read-only MCP resources agents read |
| [Agent-facing server](mcp-server.md) | `python -m kumosql mcp`: one stdio MCP server that loads a project once and answers one question per call (describe a table, trace a column, assess a change, find an existing table) in about 2 KB, with counts, explicit unknowns and a next step |
| [Incremental models](incremental.md) | Whether an incremental run equals a full refresh |
| [Join ordering and cardinality](joinorder.md) | Sub-join size estimates and join orders, in pure Python |
| [UI roadmap](ui-roadmap.md) | The JSON each graph, cost and change view reads |
| [BigQuery test bed](bigquery-testbed.md) | A messy, low-cost model layer with real job history |
| [Multi-statement scripts and MERGE](scripts.md) | Splitting BigQuery scripts, following temporary tables and variables, MERGE lineage, job history, and the script eval |
| [Model reuse and containment](model-reuse.md) | View reuse, query containment, aggregate decomposition |
| [Public SQL evaluation sources](public-sql-evaluation-sources.md) | The research list of public suites, databases and projects; what KumoSQL already scores from it is in the [inventory](evals/public-sources.md) |
| [Additional public SQL sources](additional-public-sql-sources.md) | A second research list: engine paired tests, repair corpora, more sample databases and GoogleSQL projects; see the [inventory](evals/public-sources.md#additional-sources) |
| [Eval integrity audit status](eval-integrity-status.md) | What each finding of the October 2026 external [eval integrity audit](eval-integrity-audit-2026-10-02.md) means on current master, and what was fixed |
| [Eval integrity audit, 2026-10-02](eval-integrity-audit-2026-10-02.md) | The external audit as received: SMT snapshots, typed result comparison, the workbook fixture gate, fuzz floors |
| [Proof re-check](proof-recheck.md) | The heavy executed search that hunts for wrong proofs among the pairs the evals count as proven: the engine, its adapters, how to triage a difference and what the first runs found |
| [Proof admission](proof-admission.md) | The ledger of proofs each eval counts, the gate that fails when a count moves without the checks, the near-miss probe that mutates proven pairs, and the false proofs found on 2026-11-10 |
| [Rule-level fuzzing](rule-fuzzing.md) | Checking each rewrite `normalize` applies on its own, on the exact query it saw, with DuckDB as the oracle: how a difference is confirmed, the corpora and the limits |
| [Numeric assumption report](numeric-assumption-report.md) | Counting, per assumption label, how many proofs of the prover evals carried it before and after the numeric semantics of issue #484, and how many numeric proofs were re-proved with fewer |
| [Splitting the prover out](prover-split.md) | Step 8 of the roadmap: what the equivalence prover is made of, the script that computes its import closure, and what moves to its own repository |
| [Test history](test-history.md) | Recording every test run and its times, ranking the tests that break changes that otherwise work, tracing test times over time, and running likely failures first |
| [Picking up a workstream](handoff.md) | How an outside contributor or agent continues a `workstream` issue: a fresh clone of master, targeted tests only, the rules that never relax, and handing the work back as a pull request |

## Evals

The [evals folder](evals/README.md) has one page per eval family (the SQLSolver, VeriEQL, Logos' TPC-H, DSB and TPC-DS pairs, Singh and Bedathur, SQL-IQ and LLM-SQL-Solver equivalence suites, DLBench's cross-dialect translations, pairs from optimizer wrong-result bugs, Arcwise-Plat's corrections of BIRD's gold SQL, BigQuery number and error traps for the SMT prover, DB-GPT's rewrite examples, rewrites recommended by vendor docs, bounded verification, rewriting benchmarks, engine test suites, syntax and behaviour coverage, lineage and Dataform evals, fuzzing) and a table naming the `benchmarks/results/*.json` file behind every eval. Each eval's numbers are in the README scoreboard ([format](../benchmarks/README.md)).

## More pages (generated)

<!-- generated-pages:start (python tools/docs_index.py --write) -->
- [Field trial: analyst tasks with and without the agent server](field-trial.md): Step 7 of the roadmap.
<!-- generated-pages:end -->
