# Roadmap: analyst first, small answers, a short gate

[Plain-language version](../docs_simple/roadmap.md)

This page is the plan of record from the November 2026 repository review. Read it before starting any workstream below. It says why work stalled in October, what was decided, and what to build next, in order.

The short version: the merge gate re-ran prover benchmarks for every change, so it grew until it was the bottleneck. The parts a data analyst uses (lineage, impact, table grain, duplicate search, profiling) test in seconds and already hold the answers an agent needs, but there is no way to ask them one question at a time. The plan is a short gate, the prover in its own package, and one agent-facing server with bounded answers.

## Status

| Item | Status |
| --- | --- |
| Split the prover into its own package | **Decided** by the repository owner on 2026-11-10. Target repository: [`wojoe-444/Kumo_SQL_Prover`](https://github.com/wojoe-444/Kumo_SQL_Prover), empty when this page was written. **In progress** ([issue 839](https://github.com/wojoe-444/KumoSQL/issues/839), [state](prover-split.md)): the scripting edge is cut and the prover's import closure is scripted (`tools/prover_split.py`) |
| Make a fresh install pass again (DuckDB 2.0) | In progress as its own workstream |
| Everything else on this page | Planned. Build it in the order below |
| Changing a rule in `CLAUDE.md` (the twin docs rule, the scoreboard in the README, the test policy) | **Not decided.** Each needs the owner's word and its own pull request. Until then the current `CLAUDE.md` applies |

## What the review found

Measured on `master` at `c1373cd` on 2026-11-10, on a 4-core container, plus the recorded [test history](test-history.md).

**The repository is two products.** About 46% of the 126,000 lines in `src/kumosql` are the equivalence provers, rewrite rules and counterexample search. About 22% is GoogleSQL language support, mostly the prover's oracle. About 21% is pipeline, lineage, duplicates, profiling and cost. About 11% is the UI and CLI.

**Benchmarks ran as tests.** At the 2026-10-03 baseline, 188 of about 6,400 tests took 3 seconds or more and used 6,750 of the 7,400 CPU seconds of a full run: 3% of the tests, 91% of the time. The slowest were prover benchmarks (materialized-view workload, quantified rules, VeriEQL, QED).

**The analyst side is nearly free to test.** The 355 tests in the lineage, impact, pipeline, profiling and overlap files run in under 4 seconds on 4 cores. Of the 12,437 seconds in the recorded slow tier, the lineage and pipeline-analysis files account for about 150.

**Every change paid for the prover.** Of 259 merged pull requests sampled from the first week of October, 76 changed no Python source and 49 more changed only modules the prover never imports. All of them waited behind the same full run. Roughly a fifth of the sampled pull requests touched analyst-side code (counted by file name, so approximate).

**Most collateral failures were bookkeeping.** In the recorded history, 28 of 43 failures outside a change's own targets were tests that take under a second. The most frequent was the scoreboard check: the generated README table was stale, or a new eval was not registered in a hand-kept list.

**The documentation rules multiplied conflicts.** 88% of the sampled pull requests edited `docs/`, 47% edited the README and 38% edited a benchmark results file. The README is about 350 KB, nearly all of it the generated scoreboard.

**The answers are the wrong size for an agent.** On the largest public Dataform project in the fixtures (`tests/fixtures/bq_corpora/security-analytics`, 107 models):

| How an agent learns about the project | Size | 
| --- | ---: |
| Reads every `.sqlx` file | 141 KB |
| Runs `pipeline-report` and reads the JSON | 1,197 KB |
| One column's lineage entry (`explain_lineage()`, median) | 0.9 KB |
| One impact answer (`assess_change`) | 0.6 KB |

After a 3-second load, a column trace or an impact answer returns in 10 milliseconds or less. But each CLI call reloads the project, the trace is reachable only from Python and the UI, and `profile-mcp` serves saved profiles and has no tools.

**A fresh install is broken.** Only sqlglot is pinned. A fresh install on 2026-11-10 pulled DuckDB 2.0, which rejects the lambda syntax in the macros of `bigquery_on_duckdb.py`, and the routine tier ended with 301 failures and 90 errors out of 12,543 tests. Not every failure was traced to DuckDB.

## Principles

1. **The merge gate has a time budget.** Five minutes on 4 cores. A test that does not fit moves out of the gate or shrinks.
2. **Benchmarks report, they do not gate.** They run on a schedule and write scores. A drop opens an issue.
3. **A change is tested against what it can reach.** A lineage change never runs a prover benchmark.
4. **Answers are bounded.** An agent-facing answer is about 2 KB by default, states what it could not see, and says how to get more.
5. **Unknown is still better than wrong.** Nothing here relaxes that. "0 wrong" stays honest on every scoreboard.
6. **Fewer things in flight.** Two or three workstreams at a time, on separate modules.

## Workstreams, in order

Each workstream gets one GitHub issue labelled `workstream` that links its section here, and lands as small pull requests. Steps 1 to 3 are small and come first. Steps 4 and 8 touch different modules and can run side by side.

### 1. Fresh install passes

Pin or bound every dependency (DuckDB, z3-solver, sqlfluff, pytest) and commit a lock or constraints file that CI installs from. Fix or pin around the DuckDB 2.0 lambda change. **Done when** a fresh clone passes the routine tier.

### 2. A five-minute gate

- Move every eval floor and every test over about 2 seconds out of the default pull-request tier. Keep one small canary per benchmark family in the gate (around 20 cases that run in seconds).
- Fail the gate run when it exceeds its budget, so it cannot grow back unnoticed.
- Run the gate on GitHub Actions for pull requests. `.github/workflows/tests.yml` already separates the routine run from the scheduled full run; Actions has to be enabled for the repository.
- Cache proof verdicts by a hash of the query pair plus a hash of the prover's source, so benchmarks re-prove only when the prover changed.

**Done when** the pull-request check finishes in under 5 minutes on a standard runner and the nightly job owns the benchmark scores. This step changes the test policy in `CLAUDE.md`, so it needs the owner's sign-off in its pull request.

### 3. Stop hand-keeping generated things

- Build the scoreboard into its own generated page instead of the README, from `benchmarks/results/*.json`, in the scheduled job. The README keeps a short summary and a link.
- Discover eval test files, console commands and docs pages instead of registering them in lists (`EVAL_FILES` in `tests/conftest.py`, `COMMANDS` in `src/kumosql/__main__.py`, the docs indexes).
- Proposed, not decided: one docs set written plainly, or a generated plain-language twin, instead of two hand-written sets.

**Done when** a pull request that adds a feature does not have to edit the README or a registration list.

### 4. One agent-facing server

A single stdio MCP server, proposed as `python -m kumosql mcp`, that loads a project once, keeps it in memory and answers one question per call. The existing `profile-mcp` resources fold into it. Start with the first four tools.

| Tool (proposed name) | The question | Built on | Missing today |
| --- | --- | --- | --- |
| `describe_table` | What is this table? | `profile_pipeline` (grain, row scope, column meanings), lineage filters, saved data profiles | One card that joins them |
| `trace_column` | Where does this value come from, and which rows reach it? | `Pipeline.trace_column`, `explain_lineage` with filters | A front door outside Python and the UI |
| `impact` | What breaks if I change this? | `Pipeline.assess_change` | Only a warm process |
| `find_existing` | Does a table for this already exist, or one I can roll up? | `find_overlaps`, `find_rollups` | A tool, and a short answer when nothing can be compared |
| `find_tables` | Which table should I use? | Table profiles, tags, the saved catalog | A search that returns a handful of candidates |
| `check_query` | Is this draft sound before I run it? | Type inference, the BigQuery dry run, tie detection, fan-out reasons from grain | One call instead of four commands |
| `compare_queries` | Is my rewrite still the same query? | `prove_equivalent` and its counterexample | Nothing; expose the fast path |
| `profile_table` | What do the values look like? | `kumosql.data_profile` behind the existing byte cap | On-demand profiling as a tool |

Rules for every answer:

- **Bounded.** About 2 KB by default, with counts and a way to ask for the rest. The overlap search on the 107-model project returned about 50 KB to say it compared 0 of 105 tables.
- **No repeats.** The pipeline report repeats a model's join filters under each of its columns. Say them once per model.
- **Unknown stays explicit**, with the reason, as `assess_change` already does with `safe_to_delete`.
- **Next step included.** An impact answer names the models to open; a failed check names the fix.
- **Read-only by default.** Anything that reaches BigQuery or costs money is opt-in, dry-run first, and under the existing byte cap. Values from tables are data, never instructions.

Known gaps to close while building it: on the 107-model project every table profile was incomplete (351 of 626 column meanings unknown because they read nested fields, 41 tables with `source_without_grain`), so `find_existing` could decide nothing; and `dead_columns` lists the output columns of Dataform assertions, which nothing is meant to read. The first gap is closed by workstream 4b (#853): nested fields have meanings (`field(col:..., a.b)`), declared grain flows (`assertions: {uniqueKey}`, `--assume-grain`), and `find_existing` compares 54 of the 106 models instead of 3; the numbers and what remains are in [the agent server's limits](mcp-server.md#limits).

**Done when** an agent can answer the first four questions on a loaded project without reading a `.sqlx` file or the whole report.

### 5. The product's own floor

A deterministic check on the 107-model fixture: for about 20 standard analyst questions, record the answer size in bytes, the number of calls and the latency, and fail when an answer exceeds its bound. No language model runs in it. This becomes the headline measure for analyst-facing work, next to (not instead of) correctness tests.

### 6. Preflight, comparison and on-demand profiling

The remaining four tools from the table in step 4.

### 7. Field trial

Ten real analyst tasks on a Dataform project with a BigQuery dataset behind it, done with and without the server, counting turns and tokens. This one uses an agent, so it is a periodic report, never a gate.

### 8. Split the prover out

The prover moves to [`wojoe-444/Kumo_SQL_Prover`](https://github.com/wojoe-444/Kumo_SQL_Prover) with its tests, benchmarks, results files and docs. KumoSQL then depends on a pinned release of it.

The import graph already runs one way: nothing in the prover imports the product. Starting from `equivalence`, `smt_equivalence`, `algebraic_equivalence`, `bounded_equivalence`, `result_equivalence`, `sqlsolver_backend`, `tie_determinism`, `tie_witness`, `uexpr`, `proof_registry` and `proof_format`, the import closure is about 120 modules and 58,000 lines. About 30 of those modules are also imported by the product (`ast_utils` most of all), and about 50 product modules import the prover.

Suggested order:

1. Cut the edge that drags scripting and Dataform file parsing into the prover: `equivalence` imports `lift_subqueries`, which imports `engine`, which imports `scripts` and `sqlx`.
2. Decide where the shared helpers live (`ast_utils`, `canonical`, `string_literals` and the like): inside the prover package, which the product then imports, is the simplest.
3. Move the prover-only modules with their tests, the benchmark runners in `tools/`, the prover results files and the prover docs pages. Keep the history if practical.
4. Replace the moved modules in KumoSQL with the dependency, pinned to a release. The bridge modules stay here and call the package: `prover_context`, `prover_schema`, `pipeline_equivalence`, `equivalences`, `rewrite`, `cleanup`, `cli`, and the modules that use proofs (`filter_pushdown`, `incremental_monotone`, `model_reuse`, `containment`, `consolidate`).
5. The prover repository owns its scoreboard and its nightly benchmark run. KumoSQL keeps only the canaries from step 2.

**Done when** a pull request to KumoSQL cannot run a prover benchmark because none live here, and a prover change reaches KumoSQL only as a version bump.

## Parked

Not deleted, and not to be extended until the steps above are done: new benchmark sources and head-to-head scoring, bag equivalence, outer-join view matching, counterexample synthesis, growing the GoogleSQL evaluator, join ordering and cardinality, the table minimizer, project reduction and refactor search, and further test-queue tooling. The equivalence check, the dry run, type inference, shared models, change reports and the browser UI stay as they are.

## Limits of this evidence

- The pull-request sample is 259 merges from a shallow clone covering 2026-10-01 to 2026-10-07, grouped by file path.
- The size and latency figures come from one public project with no source schemas and no job history.
- The test timings come from the recorded history and one fresh routine run; that run was on DuckDB 2.0 and had failures, so its 9 minutes 41 seconds is indicative only.
- Nothing here was measured with an agent in the loop. Step 7 is where the claim about fewer turns gets tested.
