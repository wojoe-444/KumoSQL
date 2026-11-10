# Proof re-check

[Plain-language version](../docs_simple/proof-recheck.md)

The evals count a pair as proven when the prover says the two queries are equal, then run a few dozen to a thousand random databases as a cross-check. A proof the prover gets wrong survives that cross-check when the database that separates the queries is rare. The proof re-check is a developer tool that runs a much heavier executed search on every pair an eval counts as proven, to find those false proofs. It changes no score and no prover rule. A false proof it finds is a bug in the prover, filed against the rule that makes the proof go through.

```sh
python tools/proof_recheck.py --list
python tools/proof_recheck.py qed-calcite --jobs 2 --budget 3000 --seconds 150 --out proof-recheck/raw
python tools/proof_recheck.py singh --pairs KEY1,KEY2 --budget 20000
python tools/proof_recheck.py verieql --every 40 --budget 150 --seconds 30 --out /tmp/rc
```

`--out` takes a folder for one `<eval>.jsonl` per eval, one record per pair; a rerun skips pairs already written unless `--fresh`. `--since <folder>` skips the pairs an earlier run already re-checked (survived or differs) and runs only the rest: pairs it did not prove then, timeouts, unrunnable ones and pairs added since, which is how to re-check what a prover change newly proves. A machine with 4 cores is oversubscribed by more than about `--jobs 2` of the heavier evals. The raw output is large, so it is not committed.

## The search

`tools/recheck/engine.py` takes a `Case`: the DuckDB SQL of both sides, the tables (columns with a kind, NOT NULL, keys, foreign keys, allowed values), an optional `legal(db)` callback for other declared constraints such as CHECKs, `setup` statements (collations, the BigQuery-on-DuckDB settings and macros), and a comparison `mode` (`bag`, `set`, `list`, `contained`, `contained-set`). `engine.recheck(case, budget=..., seconds=...)` returns one JSON record.

- **Databases.** Exhaustive tiny databases (0 to 3 rows per table, each column drawn from NULL and two values, foreign keys from the parent's chosen rows); edge databases (all empty, each table empty in turn, all-NULL rows, doubled rows); and random databases whose profile varies (0 to 20 rows, NULL rates to 80%, duplicate-heavy rows, ties, values next to the queries' literals, boundary numbers such as 2^24+1 and 2^53+1, strings that differ in case, trailing spaces or Unicode case mapping, dates on month and year boundaries). Every database honours the declared keys, NOT NULL columns, foreign keys and `legal`.
- **What counts as a difference.** The bags differ after floats are rounded to 9 significant digits; DuckDB's unoptimized plan returns the same results (DuckDB 1.5.6 has optimizer bugs); neither query changes when every table's rows are reversed, rotated or shuffled; and, for a query with `LIMIT` or `OFFSET`, the two queries still differ when every output column is added as the last `ORDER BY` key, ascending and descending (`recheck/ties.py`). A difference that only shows which of several tied rows an engine returned is `nondeterministic`; a `*` or an unparsable query is left alone. A difference that fails a check is counted in the record's `notes` (`optimizer`, `nondeterministic`, `float-noise`) and the pair still `survived`. The witness is shrunk row by row.
- **Float noise.** A result that differs only in float digits is noise. So is an integer beside a double: DuckDB's `AVG` returns DOUBLE, so over one row the value 2^53+1 comes back as 2^53, where MySQL's exact decimal keeps it. The check reads integers in a column that holds a float on either side as doubles before comparing at 6 digits. Integers in columns without a float still compare exactly, so an integer-only difference is never hidden.
- **A difference the optimizer hides.** DuckDB's wrong rows can make two different queries agree as easily as they make two equal ones differ. Every edge database, and every fifth database after them, on which the optimized results agree is also run with the optimizer off (`engine.masked`). Results that differ only there, beyond float noise and for every row order, end the search with the verdict `masked`: one of the two plans is wrong, so the pair is neither a confirmed false proof nor a survivor and needs a reading.
- **Verdicts.** `survived`, `differs`, `masked`, `not-proven` (the eval does not count the pair), `unrunnable` (an engine rejects both queries), `timeout` and `search-error`. Each record also lists order-sensitive constructs (`LIMIT`, `ANY_VALUE`, `DISTINCT ON`, `ROW_NUMBER`, unordered `ARRAY_AGG`).

## Adapters

An adapter proves each pair exactly as its eval does (same entry point, options and constraints) and returns the `Case` that eval's own executed check runs. Each module in `tools/recheck/` exposes an `ADAPTERS` dict.

| Evals | Module |
| --- | --- |
| `sqlsolver-calcite`, `sqlsolver-spark`, `sqlsolver-tpch`, `sqlsolver-tpcc`, `qed-calcite`, `calcite-mined`, `rbot-calcite`, `cosette`, `spes-only` | `calcite_family.py` |
| the same `sqlsolver-calcite` and `sqlsolver-spark` adapters, with the three pairs round one could not run made runnable | `calcite_unrunnable.py` (loads after `calcite_family.py` and replaces those two entries) |
| `singh`, `singh-fractions`, `singh-leetcode-types` | `singh.py` |
| `verieql` (pairs are named `<suite>:<VeriEQL index>`) | `verieql.py` |
| `conditional-equivalence-singh`, `conditional-equivalence-verieql` (proofs that hold only under named NOT NULL, unique-key and foreign-key conditions; the conditions are declared to the search, so every database meets them) | `conditional.py` |
| `sqlancer-tlp-norec`, `unsafe-rewrite-detection`, `rewrite-composition`, `join-rewrites`, `constraint-rewrites` | `fuzz_rewrites.py` |
| `mv-reuse-calcite`, `mv-benchmark`, `containment`, `aggregate-decomposition` | `reuse_containment.py` |
| `dlbench`, `dlbench-target`, `llm-sql-solver-relaxed`, `llm-sql-solver-negatives`, `llm-sql-solver-uncounted`, `sql-rewritebench`, `wetune-issues`, `wetune-issues-mysql-ci`, `clickbench-rewrites`, `llm-r2-scale`, `llm-r2-scale-train`, `spider2-bigquery` | `dialect_rewrites.py` |
| `sqlfluff-semantic-fixes`, `shared-refactors-proof`, `duplicate-exact`, `pipeline-equivalence`, `incremental-proofs`, `table-minimization`, `output-properties`, `output-properties-adapted` | `pipelines_refactors.py` |
| `bounded-calcite`, `bounded-cosette`, `bounded-leetcode`, `bounded-literature`, `bounded-qed`, `bounded-rbot`, `bounded-singh`, `bounded-spes`, `bounded-sqlsolver-calcite`, `bounded-sqlsolver-spark`, `bounded-sqlsolver-tpch`, `bounded-sqlsolver-tpcc` | `bounded.py` |

Each adapter was checked against its eval's harness: the same prover entry point, options, constraints and DuckDB translation as the eval's own executed check, and the same set of counted pairs. The adapters count what the eval's code counts today, which for a few evals is not what the results file says (the file is older): SQLancer 640 (file 639), mined view reuse 138 over 225 cases (file 112, Calcite cases only), TPC-DS materialized views 92 (file 86), and composition 129 proved of 143 changed steps (file 146 of 150). Refreshing those files is a separate change.

Notes on individual families:

- **Calcite family, round two.** `calcite_unrunnable.py` adds the `unix_timestamp` and `single_value` macros to every SQLSolver Calcite and Spark case and hand-translates the DuckDB SQL of two pairs for DuckDB: the scalar subqueries of the left-join pair become a one-row derived table cross-joined to the left side, and the integer branches of the Spark `CASE` are cast to string as Spark does. Each override applies only to the exact source text, and the prover reads the same SQL as the eval; the tests include a negative control for both.
- **Fuzz and rewrite evals.** Pairs come from the eval's own generators (SQLancer and unsafe-rewrite cases, replayed composition chains, the join and constraint cases) and are proved with the eval's entry point. `constraint-rewrites` yields three kinds of pair per case: the main proof under every offered fact (the one the eval scores), the proof under only the needed facts (`<id>@needed`), and each single-fact ablation the prover would prove (`<id>@without:<fact>`, which the eval says is none).
- **View reuse and containment.** The right side is the view-inlined query the eval's check runs; the view as written is kept in the record's `meta`. Containment pairs run as subset or sub-bag checks.
- **Dialect evals.** `dlbench` runs both queries as the prover read them (SQLite stays SQLite); `dlbench-target` runs the source against the translation read in the target's dialect through DuckDB, an extension of the eval for every target but DuckDB. SQL-RewriteBench, WeTune and ClickBench run the optimizer's proven rewrites on DuckDB, without the PostgreSQL cost guard or PostgreSQL execution those evals use. Spider 2.0 runs the format and cleanup rewrites over tables inferred from the SQL, so about 57% of its proven pairs are unrunnable in DuckDB.
- **Pipelines, refactors and properties.** Every proved item becomes a case translated as the eval translates it. Where the eval calls a pair not re-checkable the record is `unrunnable` or `search-error`. A MERGE conflict in an incremental proof is a marked row and shows as `differs`. Table minimization re-checks the minimizer's actual output for every protected table. Output-property claims become violation queries.
- **Bounded evals.** A pair counts when the eval's status is `bounded` with bound 3 or more; a counterexample on at most 3 rows per table contradicts that verdict. The adapter patches engine hooks (row cap, lazy product, watchdog) only for these pairs.
- **Unreviewed or unreproducible parts.** The engine ignores `bigquery_rows`, so a BigQuery `differs` needs that replay at triage. `mv-benchmark` downloads its workload files on first use. Counts that rest on a wall-clock budget (WeTune's kumosql rewrites, TPC-H bounded counts) move with machine load.

`singh-fractions` widens every whole-number column to `DECIMAL(18,3)` (the files give no column types, so a proof claims every typing); `singh-leetcode-types` takes each column's type from VeriEQL's copy of the same LeetCode problem. `recheck/singh_mysql.py` runs the Singh pairs' original text on a MySQL 8 server instead of a DuckDB translation, to see what the translation hides (a string compared with a number, inexact decimal division, accent-insensitive `LIKE`). It needs a server started with `--lower-case-table-names=1` and `pip install pymysql`:

```sh
python tools/recheck/singh_mysql.py singh --socket /tmp/mysql.sock --jobs 4 --out proof-recheck/raw
```

## Triaging a `differs` record

1. **Is it really proven the eval's way?** Rerun the eval's harness on the pair.
2. **Dialect gap?** The prover reads the eval's dialect (MySQL for the Calcite family, LeetCode and VeriEQL; BigQuery elsewhere) while DuckDB runs a translation. Suspect string case and trailing spaces, integer and decimal division, NULL ordering, implicit casts, date functions, integer overflow and float precision. Confirm on a second engine where possible.
3. **Ties.** A `LIMIT` with tied rows or a hash-ordered pick is nondeterministic, not a false proof.
4. **Minimize** the pair and the witness, then name the rule that makes the proof go through (`module.py:function`, found by switching rules off one at a time).
5. **Fix or route.** A false proof gets a regression test, and the eval's results file and scoreboard are corrected in the same pull request. Verdict words: `false proof`, `dialect gap`, `nondeterministic`, `harness`, `already fixed`.

## Rules for the tool

- Held-out pairs run only to record verdicts. Never derive a fix from one; a forced fix is recorded as "tuned on test".
- 0 wrong always; unknown beats wrong.
- New search code goes in new modules under `tools/recheck/`. The tool does not edit `kumosql.result_equivalence`, the `kumosql.counterexample` searcher or `DatasetRunner`.
- A difference counts only if DuckDB's unoptimized plan agrees and no query depends on row order. Results that differ only with the optimizer off are `masked`, never `survived`.
- A proof enters an eval's count through [proof admission](proof-admission.md), which runs this search on each new proof.

## What the runs found

Recorded 2026-10-03 and 2026-10-04 on master, 3,000 databases per pair (Singh: about 18,000 per pair across the DuckDB and MySQL runs). `survived` counts proofs the eval counts; `not-proven` pairs are ones the eval does not count.

| Eval | Proven pairs re-checked | Result |
| --- | --- | --- |
| QED (Calcite) | 366 | all survived |
| SQLSolver Calcite | 222 of 232 | all survived; 2 unrunnable (`UNIX_TIMESTAMP` does not exist in DuckDB; a left join on subqueries DuckDB cannot execute) |
| SQLSolver Spark | 122 of 127 | all survived; 1 unrunnable (a `CASE` mixing BIGINT and VARCHAR that DuckDB refuses) |
| SQLSolver TPC-H, TPC-C | 22, 19 | all survived |
| R-Bot (Calcite) | 38 | all survived |
| Cosette | 54 | all survived |
| SPES only | 30 | all survived |
| Mined Calcite | 446 of 502 (115 held-out; run for verdicts only) | all survived |
| Singh and Bedathur | 862 (163 held-out) | all survived about 15.5 million databases, on DuckDB and on MySQL 8.0 |
| VeriEQL | 7,062 (Literature 25, Calcite-397 318, LeetCode 6,719) | 0 false proofs; one `differs` record, the integer-beside-a-double case above |

The remaining adapters were checked against their evals without a false proof. `join-rewrites` (68 proven), `constraint-rewrites` (64) and `aggregate-decomposition` (34) were re-checked in full at 1,500 databases per pair, all survived. View reuse, containment, the other fuzz and rewrite evals, the pipeline, refactor and minimization evals, the output properties and the bounded evals were smoke-run (a few hundred databases per pair, most of the counted pairs) with no `differs`. DLBench had two `differs` records, neither a false proof. `BIRDTrans/clickhouse/43` is a tie under `LIMIT 1` (every `emp_id` NULL, so two groups count 0; the tie probe now catches it, and it survives 4,000 databases). `BIRDTrans/clickhouse/2` is a dialect gap seen only by `dlbench-target`: SQLite's `LENGTH` counts characters and ClickHouse's counts bytes, so `ORDER BY LENGTH(x) DESC LIMIT 1` differs on non-ASCII text; the eval has no rule for it and the prover reads both as the same function. Full-budget runs of those evals are tracked in [#497](https://github.com/walterogozaly/KumoSQL/issues/497).

The search finds 48 of the 50 false proofs from earlier audits, most within a few dozen databases. One of the misses needs DuckDB INT32 literal overflow handling; the other has catalog-qualified table names in the audit's schema.

The Singh runs also found a rule the proofs use that is unsound for MySQL: a string literal is treated as never equal to a number, so `WHERE '2' <> 2` is proven equal to no filter, while MySQL reads `'2' <> 2` as false. No counted pair rests on it. It is tracked in [#542](https://github.com/walterogozaly/KumoSQL/issues/542).

## Tests

`tests/test_proof_recheck.py` covers the engine: it finds a duplicate-sensitive difference, lets an equivalent pair survive, keeps databases legal under keys, NOT NULL and foreign keys, honours each comparison mode, ignores row-order dependence and integer-versus-double noise, and keeps an integer-only difference. Run it with `python tools/run_tests.py --label "proof recheck" --target tests/test_proof_recheck.py tests/test_proof_recheck.py`.

The adapter tests are `tests/test_proof_recheck_fuzz_reuse.py`, `tests/test_proof_recheck_dialect.py` (including the tie probe) and `tests/test_proof_recheck_pipelines_bounded.py` and `tests/test_proof_recheck_calcite.py` (the three formerly unrunnable SQLSolver pairs): they check that each adapter imports, lists its items and builds a case or a verdict for a tiny pair, and the tie probe's cases.
