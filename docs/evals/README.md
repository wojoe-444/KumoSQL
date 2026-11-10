# Evals

[Plain-language version](../../docs_simple/evals/README.md)

KumoSQL is scored on public benchmarks and on suites generated for its own features. None of them calls a language model at run time. This folder has one page per eval family: where the data comes from, how a case is scored, how to rerun it and what the limits are. The headline numbers are in the [README scoreboard](../../README.md#benchmark-scoreboard).

## How the evals are organised

- **Results.** Every eval owns one file, `benchmarks/results/<name>.json` ([format](../../benchmarks/README.md)). `python tools/scoreboard.py` turns those files into the README scoreboard; a score is never edited by hand. The tables below name the file for each eval.
- **Evidence level.** A score says how strong its positive answers are: an unbounded proof, bounded verification (no difference on any database up to a row limit) or agreement on executed datasets. A suite that mixes levels has one results file per level.
- **Zero wrong.** An eval reports `X/Y, 0 wrong`. A false proof, an incorrect counterexample or a behaviour-changing rewrite is a bug; an unknown is allowed.
- **Held-out cases.** Where a held-out split exists, the page and the results file say so. Cases a rule was developed against are marked `tuned on test`.
- **Rerun.** `command` in each results file reruns the eval. `python tools/run_tests.py --evals` runs every benchmark floor, and `python tools/eval_diff.py` compares every eval on `origin/master` with your checkout. It masks timing before comparing (including fields ending in `_seconds`, numbers with a unit, `HH:MM:SS`, and the bare elapsed-seconds column that ends a SQLSolver summary row), so only answers and counts count as a difference. See [eval diff](eval-diff.md) for stable samples, per-eval time caps, process cleanup, and timing records.

## Source inventory

[Public SQL evaluation sources](public-sources.md) lists every public suite, database and project considered, whether an eval below already scores it, and what is being added.

## Equivalence and proofs

| Page | What it scores | Results files |
| --- | --- | --- |
| [Algebraic prover on SQLSolver, R-Bot, QED, Cosette, SPES and mined Calcite tests](sqlsolver.md) | Equivalent query pairs proved, with refutations by a replayed counterexample | `sqlsolver-calcite`, `sqlsolver-spark`, `sqlsolver-tpch`, `sqlsolver-tpcc`, `rbot-calcite`, `qed-calcite`, `cosette`, `cosette-adapted`, `spes-only`, `calcite-mined` |
| [VeriEQL](verieql.md) | LeetCode, Literature and Calcite suites, proofs and counterexamples scored separately | `verieql-leetcode-proof`, `verieql-leetcode-executed`, `verieql-literature-proof`, `verieql-literature-executed`, `verieql-calcite-proof`, `verieql-calcite-executed` |
| [Logos' TPC-H, DSB and TPC-DS pairs](logos.md) | R-Bot's Calcite rewrites of TPC-H and DSB queries and TPC-DS query variants: proved, or shown different on generated data | `logos-core-proof`, `logos-core-executed` |
| [Singh and Bedathur](singh-bedathur.md) | 2,800 LeetCode equivalence pairs | `singh-bedathur-leetcode` |
| [Equivalent under conditions](conditional-equivalence.md) | The fourth verdict (proved equal under minimal NOT NULL, unique-key and foreign-key conditions) on the Singh and VeriEQL LeetCode pairs, plus three hand-checked suites | `conditional-equivalence-singh`, `conditional-equivalence-verieql` |
| [Bounded verification](bounded-verification.md) | The same suites under the z3 bounded checker (at most 3 rows per table) | `bounded-sqlsolver-calcite`, `bounded-sqlsolver-spark`, `bounded-sqlsolver-tpch`, `bounded-sqlsolver-tpcc`, `bounded-qed`, `bounded-rbot`, `bounded-cosette`, `bounded-spes`, `bounded-singh`, `bounded-literature`, `bounded-calcite`, `bounded-leetcode` |
| [SQL-IQ](sql-iq.md) | Equivalence judge, SQL judge and error classification | `sql-iq-equivalence`, `sql-iq-judge`, `sql-iq-errors` |
| [LLM-SQL-Solver](llm-sql-solver.md) | 180 Spider pairs that must never be proved, 70 pairs with expert labels | `llm-sql-solver-negatives`, `llm-sql-solver-relaxed` |
| [QUITE LLM rewrites](quite.md) | The 4,160 rewrites that 13 LLM and learned rewriters published for TPC-H, DSB, Calcite and SQLStorm queries, each flagged equal or not on the authors' instance: flagged-equal pairs proved, flagged-unequal pairs refuted by a replayed database and never proved on grounds a replay contradicts | `quite-rewrites`, `quite-negatives` |
| [DLBench](dlbench.md) | Cross-dialect translations from SQLite, MySQL and PostgreSQL into six databases: parsed, and proved equal to the source | `dlbench` |
| [DB-GPT rewrite examples](dbgpt-rules.md) | DB-GPT's 36 PostgreSQL before/after rewrites, labelled by hand and checked on DuckDB | `dbgpt-rules` |
| [Documented rewrites](documented-rewrites.md) | Rewrites recommended by vendor and style-guide docs, written as KumoSQL's own cases; the ones that change results must not be proved | `documented-rewrites` |
| [Arcwise-Plat corrections of BIRD](arcwise-corrections.md) | 144 BIRD gold queries against their human repair, downloaded at run time (CC BY-SA, never committed): the two differ in meaning, so none may be proved; refuted by a database KumoSQL builds | `arcwise-corrections` |
| [Optimizer wrong-result bugs](optimizer-bugs.md) | Query pairs from public optimizer bug reports (Calcite, Spark, CockroachDB, DuckDB, MySQL, ClickHouse): none may be proved | `optimizer-bugs` |
| [Paired engine tests](engine-paired-tests.md) | Trino's two-query join assertions, Spark's predicate-subquery tests, a PostgreSQL join-removal regression and a DuckDB collation fixture, with authored JoinEquiv and jOOQ guards: proved, refuted or unknown against hand labels | `engine-paired-tests` |
| [Numeric traps](numeric-traps.md) | BigQuery number and error rules around the SMT prover (2**53, INT64 overflow, NaN, `-0.0`, NUMERIC, `/` against `DIV`, an operation moved ahead of its guard): trap pairs never proved, and whether a rewrite can raise an error the original cannot | `numeric-traps` |
| [Join rewrites to LEFT JOIN](join-rewrites.md) | Hand-checked rewrites between CROSS, INNER, RIGHT, FULL, semi and anti joins and LEFT JOIN, proved or refuted | `join-rewrites` |
| [Whole-pipeline equivalence](pipeline-equivalence.md) | Multi-model refactors that keep, or break, every consumer-visible output; the Jaffle Shop dbt project built, loaded, rewritten and refactored | `pipeline-equivalence`, `pipeline-refutation`, `jaffle-shop`, `jaffle-shop-refactors`, `jaffle-shop-refutation` |
| [Targeted test data](targeted-test-data.md) | Targeted databases, multi-database checking and counterexample minimization | `targeted-test-data`, `multi-database-semantic`, `counterexample-minimization`, `unsafe-rewrite-variants` |
| [Metamorphic fuzzing](fuzzing.md) | TLP/NoREC fuzzing, unsafe-rewrite detection, rewrite composition and the typed soundness fuzzer | `sqlancer-tlp-norec`, `unsafe-rewrite-detection`, `rewrite-composition`, `soundness-fuzz` |

## Rewriting and performance

| Page | What it scores | Results files |
| --- | --- | --- |
| [Query rewriting benchmarks](rewrite-benchmarks.md) | SQL-RewriteBench, WeTune's GitHub issues, QueryBooster's experiment rewrites, ClickBench and cost-recommendation validity | `sql-rewritebench`, `wetune-issues`, `querybooster`, `clickbench-rewrites`, `cost-recommendation-validity` |
| [Transformations on TPC-H, TPC-DS and JOB](transformation-bench.md) | Transformations on standard workloads with real data | `transformation-workloads`, `job-alternative-forms` |
| [LLM-R2 query sets](llmr2-bench.md) | Scale test of the rewrites on 11,353 queries, test files held out | `llm-r2-scale` |
| [Sample databases](sample-databases.md) | Chinook, Northwind, Sakila and the Oracle HR and Customer Orders schemas loaded whole into DuckDB from their pinned scripts; Northwind's 16 views, Sakila's 7 views and 6 routines, Oracle's 5 views and authored workloads through every rewrite, checked on the real data; authored equivalent pairs and key-dependent siblings through the provers | `sample-databases-rewrites`, `sample-databases-pairs`, `sample-databases-sakila-rewrites`, `sample-databases-sakila-pairs`, `sample-databases-oracle_hr-rewrites`, `sample-databases-oracle_hr-pairs`, `sample-databases-oracle_co-rewrites`, `sample-databases-oracle_co-pairs` |
| [Sample databases: Pagila](sample-databases-pagila.md) | Pagila 4.1.1 (PostgreSQL's DVD rental sample, 16 tables and 122,209 rows) loaded whole into DuckDB from its pinned scripts; its views, function bodies, README queries and an authored workload through every rewrite, checked on the real data; authored pairs and key-dependent siblings through the provers | `sample-databases-pagila-rewrites`, `sample-databases-pagila-pairs` |
| [MV-based rewriting](mv-benchmark.md) | View mining and rewriting on JOB, SCALE, STATS and TPC-DS | `mv-benchmark` |
| [Table minimization](table-minimization.md) | Simplest pipeline that keeps the protected tables identical, from 3 to 20 tables, with traps; adapted cases from sqlglot, Fivetran and jaffle_shop scored apart | `table-minimization`, `table-minimization-sourced` |
| [Project reduction](project-reduction.md) | Smallest Dataform project that keeps the chosen outputs, as a patch: converted minimization cases checked on DuckDB, and open-source Dataform projects | `project-reduction` |
| [Duplicate detection](duplicate-detection.md) | Exact and similar duplicates, shared-model refactors | `duplicate-exact`, `duplicate-similar`, `shared-refactors-proof`, `shared-refactors-executed` |

## Coverage and engine behaviour

| Page | What it scores | Results files |
| --- | --- | --- |
| [Engine test suites](engine-suites.md) | DuckDB, SQLite and SQLGlot test queries run through every rewrite and checked by execution | `engine-duckdb-slt-plain`, `engine-duckdb-slt-amplified`, `engine-sqlite-slt-plain`, `engine-sqlite-slt-amplified`, `engine-sqlglot-fixtures-plain`, `engine-sqlglot-fixtures-amplified` |
| [Analytical SQL coverage](analytical-sql-coverage.md) | TPC-DS, DSB and SQLStorm through every stage | `analytical-sql-coverage` |
| [BigQuery and Dataform syntax coverage](bigquery-syntax-coverage.md) | One case per GoogleSQL or Dataform construct (a checked-in manifest, run by the test suite) | none |
| [BigQuery behaviour](bigquery-behavior-eval.md) | GoogleSQL compliance queries and edge cases; BigQuery Utils UDF tests run through the BigQuery to DuckDB translation | `googlesql-behavior`, `bigquery-edge-cases`, `bigquery-utils-udfs` |
| [GoogleSQL type inference](googlesql-types.md) | The type checker's output column types against the types the GoogleSQL compliance tests print for each query; sqlglot's annotator as the baseline; a held-out split | `googlesql-types` |
| [GoogleSQL compliance expected results](googlesql-expected-results.md) | The compliance tests' expected rows as an oracle for the BigQuery-to-DuckDB translation | `googlesql-expected-results` |
| [GoogleSQL conformance](googlesql-conformance.md) | The pure-Python GoogleSQL evaluator on the compliance tests' expected results: exact, unsupported or mismatch | `googlesql-conformance` |
| [SQLFluff rule fixtures](sqlfluff-fixtures.md) | Lint fail-to-fix pairs: semantic fixes proved, layout fixes checked, KumoSQL's formatter against them; KumoSQL's rewrite rules on the queries sqlfluff refuses to fix | `sqlfluff-semantic-fixes`, `sqlfluff-layout-fixes`, `sqlfluff-kumosql-formatter`, `sqlfluff-refusals` |

## Lineage, impact and Dataform

| Page | What it scores | Results files |
| --- | --- | --- |
| [Lineage and change impact](lineage-bench.md) | SQLLineage cases and generated lineage pipelines | `sqllineage`, `lineage-impact` |
| [Lineage goldens](lineage-goldens-bench.md) | DataHub and OpenLineage lineage tests (OpenLineage is the independent oracle) | `lineage-goldens-openlineage`, `lineage-goldens-datahub` |
| [Spider 2.0](spider2-bench.md) | Spider 2.0 BigQuery reference queries as inputs to KumoSQL's analyses | `spider2-bigquery` |
| [Dataform preservation](dataform-bench.md) | Protected SQLX text and dependencies | `dataform-preservation` |
| [Real BigQuery projects](bq-real-corpora.md) | Open-source Dataform projects and BigQuery SQL loaded whole, cleaned up and formatted | `bq-real-corpora` |
| [Schema-change compatibility](schema-change-bench.md) | Which models break when a column changes | `schema-change` |

## Evals documented with their feature

These features keep their eval results on the feature's own page.

| Page | What it scores | Results files |
| --- | --- | --- |
| [Model reuse and containment](../model-reuse.md) | View reuse, query containment, aggregate decomposition | `mv-reuse-calcite`, `containment`, `aggregate-decomposition` |
| [Join ordering and cardinality](../joinorder.md) | Sub-join sizes and join orders on STATS-CEB and JOB | `stats-ceb-cardinality`, `stats-ceb-endtoend`, `job-cardinality`, `job-endtoend` |
| [Output properties](../output-properties.md) | Never-NULL columns, unique keys and row bounds | `output-properties`, `output-properties-adapted` |
| [Constraint-dependent rewrites](../constraint-rewrites.md) | Rewrites valid only under declared keys and constraints | `constraint-rewrites` |
| [Incremental models](../incremental.md) | Whether an incremental run equals a full refresh | `incremental-detection`, `incremental-proofs`, `incremental-pgivm` |
| [Multi-statement scripts](../scripts.md) | Splitting BigQuery scripts, temporary tables and variables, MERGE lineage | `script-splitting` |

## Adding an eval

1. Write the harness under `tools/` and a test under `tests/`; put `pytestmark = pytest.mark.eval` in the test file (no list to edit). Use [eval diff](eval-diff.md) to compare slow evals without running their full corpora.
2. Pin the source version, keep original and adapted cases apart, and reserve held-out cases.
3. Add `benchmarks/results/<name>.json` and run `python tools/scoreboard.py`.
4. Add or extend a page in this folder, list it in the table above and in [docs/README.md](../README.md), or leave that to `python tools/docs_index.py --write`, which adds any page or results file no index names to a generated block at the end of each index. `tests/test_docs.py` checks the links and that every page has a plain-language companion.
