# Understanding the evaluations

[All simple guides](../README.md) · [Full reference and results-file index](../../docs/evals/README.md)

An evaluation, or eval, is a repeatable test suite measuring a feature. Some use public SQL test cases; others generate projects whose expected behavior is known in advance. None calls a language model while deciding the results.

## Read a score carefully

“X/Y, 0 wrong” needs two questions: what counts as success, and how was correctness checked? An equivalence proof, a bounded check, and agreement on executed data are different evidence levels.

An unknown answer is allowed. A false proof or a rewrite accepted despite changing results is a bug. A suite can avoid wrong answers by declining everything, so coverage and usefulness matter too.

**Held-out** cases were kept out of development. **Tuned on test** means developers used those cases while building the feature. **Adapted** SQL was changed to fit a dialect or test harness; it is reported separately from original SQL.

For comparing slow evals between code versions, see [Eval diff](eval-diff.md).

## Query equivalence and safety

| Guide | Question it tests |
| --- | --- |
| [SQLSolver and related corpora](sqlsolver.md) | Which optimizer query pairs can be proved? |
| [VeriEQL](verieql.md) | Can queries be proved or separated on constraint-respecting data? |
| [Singh and Bedathur](singh-bedathur.md) | Do alternative LeetCode solutions agree? |
| [Logos' TPC-H, DSB and TPC-DS pairs](logos.md) | Are Calcite's rewrites of benchmark queries really equivalent? |
| [Equivalent under conditions](conditional-equivalence.md) | How often is a pair equal under a short list of facts, and is any answer wrong? |
| [Bounded verification](bounded-verification.md) | Do queries agree on every modeled small database? |
| [SQL-IQ](sql-iq.md) | Equivalence, candidate choice, and error classification |
| [LLM-SQL-Solver](llm-sql-solver.md) | Does the checker reject wrong query pairs and handle expert labels? |
| [QUITE LLM rewrites](quite.md) | When language models rewrite a query, which rewrites can be proved right, and are any wrongly proved? |
| [Join rewrites](join-rewrites.md) | When does changing a join type preserve results? |
| [Optimizer wrong-result bugs](optimizer-bugs.md) | Does the prover avoid accepting known faulty rewrites? |
| [Arcwise corrections of BIRD](arcwise-corrections.md) | Does the checker see that a human-corrected BIRD query differs from the original? |
| [Numeric traps](numeric-traps.md) | Does the prover handle BigQuery's number and error rules, and tell when a rewrite could start failing? |
| [Sample databases](sample-databases.md) | Do rewrites and proofs hold on complete real databases with declared keys? |
| [Sample databases: Pagila](sample-databases-pagila.md) | Do rewrites and proofs also hold on Pagila, a PostgreSQL sample with a partitioned table and many nullable columns? |
| [DB-GPT examples](dbgpt-rules.md) | Which demonstration rewrites preserve results under reviewed schemas? |
| [Documented rewrites](documented-rewrites.md) | Do rewrites recommended by vendor docs keep the results? |
| [Paired engine tests](engine-paired-tests.md) | Which query pairs from Trino, Spark, PostgreSQL and DuckDB tests can be proved equal or shown different? |
| [DLBench](dlbench.md) | Are translations across SQL dialects faithful? |
| [Whole-pipeline equivalence](pipeline-equivalence.md) | Are observable outputs preserved across several changed models? |
| [Targeted test data](targeted-test-data.md) | Can carefully chosen data reveal subtle differences? |
| [Fuzzing](fuzzing.md) | Can generated queries expose unsafe proofs or rewrites? |

## Rewriting and performance

| Guide | Question it tests |
| --- | --- |
| [Rewrite benchmarks](rewrite-benchmarks.md) | Are rewrites correct and measurably useful? |
| [Transformation workloads](transformation-bench.md) | What happens on standard workloads with real data? |
| [LLM-R2 query sets](llmr2-bench.md) | How do the rules behave across many queries? |
| [Materialized-view rewriting](mv-benchmark.md) | Can shared joins supply other queries? |
| [Table minimization](table-minimization.md) | Can the search remove models while protecting outputs? |
| [Project reduction](project-reduction.md) | How small can a whole Dataform project get while its chosen outputs stay the same? |
| [Duplicate detection](duplicate-detection.md) | Can it find copies without confusing similar queries? |

## Coverage, lineage, and Dataform

| Guide | Question it tests |
| --- | --- |
| [Engine test suites](engine-suites.md) | Do rewrites preserve results on other engines' test data? |
| [Analytical SQL coverage](analytical-sql-coverage.md) | Which stages handle large analytical queries? |
| [BigQuery syntax coverage](bigquery-syntax-coverage.md) | Is each syntax feature supported or explicitly declined? |
| [BigQuery behavior](bigquery-behavior-eval.md) | Do supported rewrites preserve tested BigQuery-specific behavior, and does the BigQuery to DuckDB translation compute the values BigQuery does? |
| [GoogleSQL type inference](googlesql-types.md) | Does the type checker name the column types Google's compliance tests print? |
| [GoogleSQL expected rows](googlesql-expected-results.md) | Does the local BigQuery translation return the rows Google's compliance tests expect? |
| [GoogleSQL conformance](googlesql-conformance.md) | Does the new Python evaluator return Google's expected answers, or say it cannot? |
| [SQLFluff fixtures](sqlfluff-fixtures.md) | Which formatting and lint fixes preserve meaning? |
| [Lineage and impact](lineage-bench.md) | Are dependencies and change effects traced correctly? |
| [Lineage goldens](lineage-goldens-bench.md) | Do results match other projects' expected lineage? |
| [Spider 2.0](spider2-bench.md) | Can the analysis handle reference BigQuery queries? |
| [Dataform preservation](dataform-bench.md) | Are protected SQLX blocks and references kept intact? |
| [Real BigQuery projects](bq-real-corpora.md) | What can the analysis handle in open-source projects? |
| [Schema changes](schema-change-bench.md) | Which downstream models break after a column change? |
| [Public-source inventory](public-sources.md) | Which research sources are covered, planned, or blocked? |

Feature guides also explain evaluations for [model reuse](../model-reuse.md), [join ordering](../joinorder.md), [output properties](../output-properties.md), [constraint-dependent rewrites](../constraint-rewrites.md), [incremental models](../incremental.md), and [scripts](../scripts.md).

## The agent-facing product

| Guide | Question it tests |
| --- | --- |
| [Agent-facing server: analyst questions](agent-floor.md) | How much does an agent read, and how long does it wait, to answer an analyst's question? |

## Find numbers or rerun a suite

Use the [full eval index](../../docs/evals/README.md) to find each `benchmarks/results/*.json` file. Its `command` field is the rerun command, and its date and caveats describe the measurement. The [project scoreboard](../../README.md#benchmark-scoreboard) displays those recorded results.

From a development checkout, `python tools/run_tests.py --evals` runs the benchmark floor tests. Full corpus runs can require downloads, databases, or hours of execution; read the matching full guide first. When adding a docs page, add its simple counterpart. If you skip the index entry, `python tools/docs_index.py --write` adds it to a generated list at the end of the index.
