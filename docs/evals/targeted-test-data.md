# Targeted test data, multi-database checking and counterexample minimization

[Plain-language version](../../docs_simple/evals/targeted-test-data.md)

Three evals strengthen and measure the synthetic checker (the DuckDB execution engine in `kumosql.result_equivalence`). They share that one engine; nothing here is a second implementation.

| Piece | Module | Role |
| --- | --- | --- |
| Declared-rule generation, `DatasetRunner` | `result_equivalence.py` | `generate_synthetic_dataset(..., rules=)` respects NOT NULL columns and keys; `DatasetRunner` keeps one DuckDB connection, swaps rows and times out runaway queries |
| Targeted databases and the multi-database suite | `targeted_data.py` | Rows below, at and above each constant a query compares with, shared join and group pools, each table empty in turn, all NULL, doubled rows, one value everywhere |
| Faulty variants | `query_mutants.py` | One-site mutation operators (comparison and boundary, constant, AND/OR, dropped or negated predicate, join type, DISTINCT, aggregate, arithmetic, grouping, COALESCE, BETWEEN, LIMIT) |
| Minimization | `minimize.py` | Shrinks a failing pair, its schema and its database while the pair still differs; replayable JSON |
| Harness | `tools/targeted_data_bench.py` | Scores all of it |

Method after XData (Chandra et al., VLDB J. 2015, mutation-killing data generation) and SQLess (failure-preserving reduction). No code or data of either is used; the operators and generators are written for sqlglot trees. SQLess reduction preserves a *failure*; it is not an equivalence-preserving simplification, and the minimizer says so.

## What is scored

For each original query the harness builds mutants and records which strategy tells each one from the original on a database that respects declared constraints:

* `single_seed`: one random database (seed 1);
* `random_8`: the engine's default eight random databases (the baseline: the checker as it was);
* `targeted`: databases built around the query;
* `suite`: corner cases plus targeted plus four random databases.

A mutant that no strategy kills (and that 150 further random databases do not kill) is passed to the equivalence prover. Proven-equivalent mutants are not faults and leave the denominator; mutants that are neither killed nor proved stay in it as *survived, unclassified*, so the score never rewards an unproven guess. Mutants that are not valid queries (`error`) or run past the time limit (`timeout`) are discarded and counted. A kill must repeat on both sides (no nondeterminism) before it counts.

* **Originals** (not adapted): the SQLSolver query corpora in `tests/fixtures/sqlsolver` (Apache-2.0; Calcite, Spark SQL, TPC-H, TPC-C), plus a 16-query university-schema set written for this eval (`UNIVERSITY` in the tool). **Adapted cases** are the mutants. Calcite and Spark queries and the university set are the *development* split; TPC-H and TPC-C are *held out* and run once at the end.
* Overlap: the same query texts are used by the SQLSolver Calcite/Spark/TPC-H/TPC-C evals (equivalence pairs, a different task) and R-Bot (Calcite). Mutation scoring shares no labels with them.
* Declared NOT NULL and keys are respected; foreign keys are not modelled, so a kill that needs a dangling foreign key would count. Float results compare to 12 digits.

Run: `python tools/targeted_data_bench.py [--split dev|heldout|all]`; detail goes to `benchmarks/targeted_data/` (ignored by git except the summaries).

## Results

See the README scoreboard (rows *Targeted test data*, *Multi-database semantic suite*, *Counterexample minimization*) and `benchmarks/targeted_data/summary-*.json` for per-operator and per-suite tables.

| | Dev (Calcite, Spark, university) | Held out (TPC-H, TPC-C) |
| --- | ---: | ---: |
| Original queries run / unsupported | 667 / 28 | 82 / 0 |
| Mutants (adapted cases) | 5,019 | 2,846 |
| Proven equivalent, discarded | 1,261 | 280 |
| Invalid (error) / timeout | 244 / 0 | 19 / 775 |
| Scored denominator | 3,508 | 1,772 |
| Single random seed kills | 11.7% | 4.9% |
| Default 8 random databases (baseline) | 52.7% | 11.6% |
| Targeted databases only | 82.5% | 69.5% |
| Multi-database suite | 83.2% | 69.2% |
| Median counterexample rows (8 random / suite) | 38 / 6 | 27 / 5 |
| Survived, not proven equivalent | 572 | 545 |

**The held-out percentage rose only because timeouts leave the denominator: its kills fell from 1,244 to 1,227 and its timeouts rose from 585 to 775. It is not an improvement.** Rerun 2026-10-04 (the first run was 2026-10-02: dev 71.8%, held out 62.8%, 623 dev originals). The dev rows rose because 44 more originals run, since the inline-table queries that the guards (#446) stopped running are accepted again (#469) and more queries translate; their 477 mutants are mostly easy kills, so the jump says little about the strategies. The held-out suite kills 1,227 against 1,244 before: 8 mutants that now fail in BigQuery on a zero divisor end as errors, and the guards make the TPC-H join queries slower per row, so 190 more mutants run past the 90 s per-query budget (775 timeouts against 585). Timeouts leave the denominator, so the held-out percentage rose although the kills fell; compare kills and timeouts, not the percentage alone. Compared per mutant with the commit before #446 on the same machine, no mutant went from killed to survived (no refutation was lost to a wrong answer), 24 dev kills and 8 held-out kills became errors (a zero divisor fails in BigQuery, so the database is skipped), 2 dev kills became 'unknown' (the prover's own time limit) and 8 held-out kills became timeouts. An earlier rerun showed 50 more Calcite timeouts; they came from nested guard macros that DuckDB expanded exponentially (fixed in #633, see [BigQuery on DuckDB](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/bigquery-on-duckdb.md)). The remaining timeouts are the TPC-H queries whose sweeps now take 20 to 100% longer (for example TPC-H 9: 12.2 s to 17.0 s for the suite sweep), the per-row cost of the arithmetic guards, so queries that finished just inside the 90 s budget now run past it; the original queries themselves run as fast as before. Counterexample minimization was not rerun.

The baseline (the checker before this work: eight random databases) was measured before any targeting code was tuned. Most unclassified survivors on the development split are `DISTINCT` toggles (677 in the first run) that are equivalent under keys but that the prover cannot show; they stay in the denominator. Minimization: 150 dev cases 692 to 216 rows, 100 held-out cases 595 to 250 rows, all replayed, median 0.14 s and 0.45 s.

## Regression cases

Mutants that escape the single seed and the default eight databases but are caught by the suite, and every mutant the suite does not catch, are kept in `tests/fixtures/targeted_data/cases.json`; `tests/test_targeted_data_bench.py` replays them and fails if the suite stops catching one it caught before, and pins floors for the full corpus.

## Unsafe-rewrite variants

`--unsafe` / `--unsafe-only` score the pairs of `tests/fixtures/unsafe_rewrite_cases.jsonl` (from `tools/unsafe_fuzz.py`) whose `expect` is `either` or `different`, left query as original and right query as faulty variant: 340/340 caught by the suite (single seed 322, 8 random databases 340) over the full 560-case fixture, median counterexample 2 rows versus 25. The pair that needs NULLs in both tables (`union-intersect-5`, INTERSECT versus a join) is caught by the `all_null` and `null_keys` databases.

## Default checker

`check_result_equivalence(..., targeted=True)` appends the targeted suite built around the left query after the random seeds, and `attach_synthetic_check` (the executed check on rewrites) now uses it. It only adds databases, so a rewrite that agreed before can now be refuted, never the reverse; the recorded `seeds_checked` lists the random seeds first, then the targeted databases.

## Targeted databases in the equivalence evals

`kumosql.refute.find_targeted_difference` runs two queries over the targeted suites built around each of them (then a few random databases), repairs foreign keys, skips databases where either query errors, and reports a difference only when it repeats. `engine="sqlite"` runs the SQL as written in SQLite for evals labelled by SQLite. The SQL-IQ judge, the Singh and Bedathur search, the SQLSolver family (QED, R-Bot, Cosette, mined Calcite) and the VeriEQL searcher call it after their own random search agrees. `KUMOSQL_TARGETED=0` turns it off for a baseline run.

| Eval | Without | With | Notes |
| --- | --- | --- | --- |
| SQL-IQ Equivalence Judge | 1158/1390 | 1165/1390 | 19 pairs decided by it: 13 agree with the label, 6 are "equivalent" labels refuted on a confirmed database (text and CAST differences), listed as label disputes, not wrong |
| Singh and Bedathur (dev sample of 300) | 261 | 262 | the one new refutation is on a pair labelled equivalent |
| VeriEQL literature | 18 different | 19 different | 0 wrong |
| QED, R-Bot, Cosette, mined Calcite | unchanged | unchanged | 0 wrong |

In Settings → Solver, Compare queries searches the same way when the solver does not prove the pair, and shows the database with the fewest rows (`kumosql.minimize`) on which the results differ.
