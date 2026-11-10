# KumoSQL, explained simply

KumoSQL helps you understand and change BigQuery SQL and Dataform projects. You can use it from Python, a terminal, or a browser running on your own computer.

It helps answer three everyday questions:

- If I change this column, what else might break?
- Are several queries doing work that could be shared?
- Will this new SQL return the same results as the old SQL?

Start with [Getting started](getting-started.md). It includes an example you can run without a Google Cloud account.

This folder is the plain-language companion to [the full documentation](../docs/README.md). It follows the same filenames and `evals/` structure. Each guide links to its full reference for API details and exceptions. The [project README](../README.md) has the command list and recorded benchmark scores.

## Choose a guide

| I want to… | Read this |
| --- | --- |
| Install KumoSQL and try a query | [Getting started](getting-started.md) |
| Use the browser app | [Browser UI](ui.md) |
| Load a Dataform git repository | [Dataform repositories](dataform-repositories.md) |
| Clean up one query | [Rewrite rules](rewrite-rules.md) |
| Understand whether two queries match | [Provers](provers.md) |
| Understand singleton joins and nested UNION identities in a proof | [Singleton aggregation and set identity](singleton-and-set-identity.md) |
| See how rewrites are double-checked | [Proof safeguards](proof-safeguards.md) |
| See how KumoSQL checks it read a query correctly | [Parser checks](parser-checks.md) |
| See when two queries match only if some facts hold | [Equivalent under conditions](conditional-equivalence.md) |
| See why two queries differ, as one condition | [Difference explanations](difference-explanations.md) |
| Find dependencies and repeated work | [Pipeline analysis](pipeline-analysis.md) |
| Review cost or a proposed change | [Cost and change reports](cost-and-change-reports.md) |
| Reduce the number of models | [Refactor](refactor.md) |
| Simplify an explicit set of table definitions | [Table minimization](table-minimization.md) |
| Extract a copied WITH query into one model | [Shared models](shared-models.md) |
| Shrink a Dataform project to the outputs you need | [Project reduction](project-reduction.md) |
| Summarize what is stored in a table, for people or agents | [Data profiling](data-profiling.md) |
| Let an AI agent ask short questions about a project | [Agent server](mcp-server.md) |
| Learn what a query guarantees about its rows | [Output properties](output-properties.md) |
| Know the column types a query returns without running it | [Type inference](type-inference.md) |
| Find results that change when rows tie | [Ties](ties.md) |
| Use keys and other data guarantees in a proof | [Constraint-dependent rewrites](constraint-rewrites.md) |
| Check incremental updates | [Incremental models](incremental.md) |
| Understand join size estimates | [Join ordering](joinorder.md) |
| Work on the UI's data endpoints | [UI roadmap](ui-roadmap.md) |
| Create a real BigQuery demo dataset | [BigQuery test bed](bigquery-testbed.md) |
| Analyze scripts or MERGE | [Scripts](scripts.md) |
| Reuse an existing summary table | [Model reuse](model-reuse.md) |
| Search harder for wrong proofs | [Proof re-check](proof-recheck.md) |
| Test each rewrite on its own | [Rule-level fuzzing](rule-fuzzing.md) |
| See how many proofs now need fewer number assumptions | [Numeric assumption report](numeric-assumption-report.md) |
| Run tests and understand their history | [Test history](test-history.md) |
| Continue a workstream someone else started | [Picking up a workstream](handoff.md) |
| See what is planned next and why | [Roadmap](roadmap.md) |
| See how the prover is being moved to its own repository | [Splitting the prover out](prover-split.md) |
| See what an outside audit of the checks found | [Eval integrity audit status](eval-integrity-status.md), [the audit itself](eval-integrity-audit-2026-10-02.md) |
| Understand the test suites and their scores | [Evals](evals/README.md) |
| Record a benchmark result | [Benchmark results format](benchmarks/README.md) |
| Understand local BigQuery-to-DuckDB execution | [BigQuery on DuckDB](bigquery-on-duckdb.md) |
| Understand the Python GoogleSQL evaluator | [GoogleSQL evaluator](gsql-eval.md) |
| Find proposed public test material | [Public SQL sources](public-sql-evaluation-sources.md) |
| Find more research leads | [Additional public SQL sources](additional-public-sql-sources.md) |

## Words you will see

| Word | Plain meaning |
| --- | --- |
| Model | A query that defines a table or view in a project |
| Pipeline | Models connected by the tables they read and produce |
| Lineage | Where a table or column gets its data |
| Downstream | Something that reads this model, directly or through other models |
| CTE | A named query inside a `WITH` clause |
| SQLX | SQL with Dataform configuration and `${...}` expressions |
| Grain | What one row represents, such as one customer per day |
| Bag of rows | Rows with duplicate counts preserved; two copies differ from one |
| Counterexample | A database on which the two queries give different results |
| Eval | A repeatable test suite that measures a feature |

## Read the evidence before accepting a change

A proof establishes equivalence within the prover's supported SQL and stated assumptions. A bounded check covers every modeled database up to a row limit. An executed check compares results on particular datasets. A BigQuery dry run checks planning and schemas. These are different strengths of evidence.

`unknown` or `unproven` means KumoSQL has not established the answer. It does not mean the queries are different. The [provers guide](provers.md) explains how to read these results.
