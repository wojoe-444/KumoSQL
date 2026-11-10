# Numeric assumption report

[Plain-language version](../docs_simple/numeric-assumption-report.md)

Issue #484 asked that, across the prover evals, at least 80% of the proofs that touch numeric expressions are re-proved with fewer assumptions. `tools/numeric_assumption_report.py` measures that: it proves the pairs of the prover evals on the commit before PR #602 (the first parent of its merge commit, `b77a2595`) and on this checkout, and compares the assumption labels each proof lists. It does not change the prover. The prover side is in [Equivalence provers](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/provers.md#numbers-and-runtime-errors) and the [numeric traps eval](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/numeric-traps.md).

```
python tools/numeric_assumption_report.py                       # the prover evals, before = b77a2595, after = this checkout (about 80 minutes)
python tools/numeric_assumption_report.py --evals qed rbot      # some evals
python tools/numeric_assumption_report.py --bigquery --evals qed rbot sqlsolver-calcite   # the same pairs read as BigQuery SQL
python tools/numeric_assumption_report.py --evals numeric-traps # the BigQuery-dialect eval of #602 (a minute)
python tools/numeric_assumption_report.py --save both.json      # keep both collections; --before-json and --after-json compare saved ones
python tools/numeric_assumption_report.py --before <commit>     # measure from another commit
```

The command prints the tables below. Exit status 1 means a lost proof that no search shows to be false.

## How it measures

- **Pairs and calls.** Each eval's own loader supplies its pairs (QED, RBOT, the four SQLSolver suites, Singh and Bedathur, Cosette, Cosette adapted, SPES, QueryBooster, mined Calcite), with the schema, types and normalisation that eval uses, and each pair goes through the eval's own prover call (`prove_equivalent_algebraic`, which calls `prove_equivalent_smt`). The last result is the pair's answer and its assumption labels are recorded. A pair labelled different, and the SQLSolver pairs the eval keeps out of its score, are skipped as the eval skips them. Nothing is copied from the evals' fixtures.
- **Two trees.** The same collection runs under the `kumosql` of a git worktree of the before commit and under this checkout, so only the prover differs. The pairs and the loaders always come from this checkout.
- **Touches numeric expressions (the rule).** A proof touches them when it carried a numeric label before (a label that mentions NaN, runtime errors, FLOAT64 or INT64 values, SUM and AVG, exact arithmetic, or a numeric type), or when its two queries contain arithmetic (`+ - * / %`, `DIV`, unary minus on a column, `ABS`, `ROUND`, `CEIL`, `FLOOR`, `SQRT`, `POW`, `LN`, `EXP`, `LOG`, `SIGN`), a decimal or exponent literal, or a plain number (`x > 5`, `LIMIT 10`). Every SMT proof carried the NaN, runtime-error and SUM-order labels before, so this rule covers every proof the SMT prover made. The report therefore also gives the figure for the queries alone (arithmetic or any number) and for arithmetic alone (no plain numbers).
- **Fewer labels.** The proof's label set after is smaller than before. A label swapped for another of the same count (the runtime-error label replaced by the verdict `same` or `refines`) is `replaced` and is not counted as fewer. A proof with a bigger set is `more`.
- **Percentage.** Fewer, over the numeric-touching proofs that are still proven after. A second figure divides by the numeric-touching proofs before, so a lost proof counts against it.
- **Lost proofs.** A proof that was lost (proven before, not after) is checked with the eval's own executed counterexample search (`numeric-traps` has none and uses the case label, written from BigQuery's documented rules) and listed. A lost proof is acceptable only when the pair is really different.

## Result on the prover evals as they run

Measured 2026-10-04 on `80c0720d` against `b77a2595`: 4,276 pairs, 2,220 proven before and the same 2,220 after, no proof lost or gained. **0 of 2,220 numeric-touching proofs were re-proved with fewer labels (0.0%); the 80% measure is not met.** Not one proof of these evals changed a label.

| Label | Numeric | Proofs before | Proofs after |
| --- | --- | --- | --- |
| FLOAT64 values are never NaN | yes | 2,220 | 2,220 |
| SUM and AVG are treated as independent of row order | yes | 2,220 | 2,220 |
| runtime errors (division by zero, overflow, failed casts) are not modeled | yes | 2,220 | 2,220 |
| `+`, `-` and `*` are exact | yes | 1,340 | 1,340 |
| values compared or combined have the same numeric type | yes | 812 | 812 |
| x and y in `ABS(x - y)` are numbers | yes | 29 | 29 |
| result column types are not compared | no | 2,220 | 2,220 |
| set operation branches give each output the same type | no | 205 | 205 |
| rows tied on the ORDER BY are cut by LIMIT the same way | no | 23 | 23 |
| a LIMIT subquery with the same text returns the same rows | no | 6 | 6 |
| equalities joining grouped keys use GROUP BY's equality | no | 6 | 6 |
| uncorrelated scalar subqueries return at most one row | no | 3 | 3 |
| window functions over the same input give the same values | no | 3 | 3 |
| string equality in singleton joins uses the right-side collation | no | 1 | 1 |

| Rule | Numeric-touching proofs | Fewer | Same | Replaced |
| --- | --- | --- | --- | --- |
| carried a numeric label, or the queries have a number or arithmetic (stated rule) | 2,220 | 0 (0.0%) | 2,220 | 0 |
| the queries have a number or arithmetic | 1,426 | 0 (0.0%) | 1,426 | 0 |
| the queries have arithmetic | 423 | 0 (0.0%) | 423 | 0 |

| Eval | Pairs | Proofs before | Proofs after | Numeric-touching | Fewer |
| --- | --- | --- | --- | --- | --- |
| qed | 375 | 371 | 371 | 371 | 0 |
| rbot | 45 | 40 | 40 | 40 | 0 |
| sqlsolver-calcite | 232 | 229 | 229 | 229 | 0 |
| sqlsolver-spark | 123 | 123 | 123 | 123 | 0 |
| sqlsolver-tpcc | 19 | 19 | 19 | 19 | 0 |
| sqlsolver-tpch | 22 | 22 | 22 | 22 | 0 |
| singh | 2,800 | 862 | 862 | 862 | 0 |
| cosette | 55 | 54 | 54 | 54 | 0 |
| cosette-adapted | 4 | 4 | 4 | 4 | 0 |
| spes | 31 | 31 | 31 | 31 | 0 |
| querybooster | 68 | 18 | 18 | 18 | 0 |
| calcite-mined | 502 | 447 | 447 | 447 | 0 |

**Why nothing moved.** These evals read their pairs as MySQL or PostgreSQL SQL (`dialect="mysql"` or `"postgres"`), which is how their sources are written, and the prover drops the NaN, SUM-order and exact-arithmetic labels and tracks runtime errors only for BigQuery-dialect proofs (`smt_equivalence.py` tests `dialect == "bigquery"`). So the work of #602 cannot reach them and the labels are the labels of before. The measure as worded cannot be met by the prover alone on these evals: either their pairs have to be read as BigQuery SQL or the numeric reading has to extend to the other dialects. Neither is done here.

## The same pairs read as BigQuery SQL

To show what the measure would look like, the nine faster evals were rerun with `--bigquery`, which forces `dialect="bigquery"` on both sides and keeps each eval's declared column types. This is an experiment, not an eval score: the pairs are MySQL SQL read as BigQuery SQL, and a pair that does not parse that way is simply unproven on both sides. Singh and Bedathur, QueryBooster and mined Calcite were left out (slow). 906 pairs; proofs 743 before, 737 after, 6 lost, none gained.

**653 of 737 numeric-touching proofs (88.6%) were re-proved with fewer labels** (same 24, replaced 59, more 1); counting the 6 lost proofs against the percentage it is 653 of 743 (87.9%). The figure for the queries alone is 425 of 484 (87.8%), for arithmetic alone 74 of 113 (65.5%).

| Label | Proofs before | Proofs after |
| --- | --- | --- |
| `+`, `-` and `*` are exact | 743 | 208 |
| FLOAT64 values are never NaN | 743 | 208 |
| SUM and AVG are treated as independent of row order | 743 | 208 |
| runtime errors are not modeled | 743 | 31 |
| both queries can raise the same runtime errors (the verdict `same`) | 0 | 125 |
| the rewrite raises no runtime error the original cannot (the verdict `refines`) | 0 | 5 |
| values compared or combined have the same numeric type | 79 | 77 |
| result column types are not compared | 743 | 737 |

| Eval | Pairs | Proofs before | Proofs after | Numeric-touching | Fewer | % |
| --- | --- | --- | --- | --- | --- | --- |
| qed | 375 | 300 | 297 | 297 | 259 | 87.2% |
| rbot | 45 | 25 | 25 | 25 | 25 | 100.0% |
| sqlsolver-calcite | 232 | 190 | 188 | 188 | 154 | 81.9% |
| sqlsolver-spark | 123 | 115 | 115 | 115 | 113 | 98.3% |
| sqlsolver-tpcc | 19 | 19 | 19 | 19 | 19 | 100.0% |
| sqlsolver-tpch | 22 | 11 | 11 | 11 | 2 | 18.2% |
| cosette | 55 | 53 | 52 | 52 | 51 | 98.1% |
| cosette-adapted | 4 | 4 | 4 | 4 | 4 | 100.0% |
| spes | 31 | 26 | 26 | 26 | 26 | 100.0% |

The TPC-H pairs keep the labels because their columns are DOUBLE (FLOAT64 here), so the NaN, SUM-order and exact-arithmetic labels stay and only the runtime-error label changes. One TPC-H pair (index 10) came out with one label more, the error verdict `same`, and no label dropped.

**Proofs lost (6), and why.** The prover declines them with "the rewrite can raise a runtime error on a database where the original returns rows": a rewrite that folds `DEPTNO = 10` into the select list turns `EMP.DEPTNO + 1` into `EMP.EMPNO + 10`, an `INT64` addition that can overflow on a row the original's `WHERE` would have dropped (BigQuery promises no evaluation order). They are `qed:159`, `qed:160`, `qed:161`, `sqlsolver-calcite:175`, `sqlsolver-calcite:210` and `cosette:14`. The eval's own executed search finds no counterexample for any of them, because it compares rows and DuckDB does not overflow here: they are not false proofs by rows, they are proofs whose error behaviour BigQuery would change. That is the intended effect of the error semantics, but it is not the "false proofs only" the brief allowed, so it is stated here rather than hidden.

## BigQuery-dialect eval of #602

The 59 pairs of the [numeric traps eval](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/numeric-traps.md) are typed and BigQuery-dialect, so the semantics apply to all of them. They are not one of the prover evals above: the author of the traps wrote them alongside the change, with some labels resting on rules marked `unverified` there, so this is a development check, not an independent one. Proofs 28 before and 35 after; 4 lost, 11 gained. **19 of 24 numeric-touching proofs re-proved (79.2%) have fewer labels** (replaced 5); the queries-only rule gives 13 of 18 (72.2%), arithmetic alone 11 of 16 (68.8%), and counting the lost proofs against it 19 of 28 (67.9%). The runtime-error label goes from 28 proofs to 0 (6 carry the verdict `same`, 5 `refines`); the NaN and SUM-order labels from 28 to 24.

The 4 lost proofs (`IEEE_DIVIDE(1, f)` filtered to `f = 0`, `IEEE_DIVIDE(1, 0.0)`, and two `IF(y = 0, ...)` division rewrites without the guard) are pairs the case labels call not equivalent or error-introducing, so they were false proofs.

## Limits

- The headline is 0%, and the cause is the dialect, not a count of cases. The two other figures are experiments (the BigQuery reading) or the development eval (numeric traps).
- The "touches numeric expressions" rule is a reading of the issue's measure. Its first half covers every SMT proof, so the stricter rows (queries only, arithmetic only) are given beside it.
- Pairs are proven under wall-clock timeouts on a shared machine. A proof near its limit can flip between runs. Both sides of the figures above agree on all 4,276 prover-eval pairs, so no timeout noise shows there, but a rerun on a busy machine can differ by a few proofs.
- The lost-proof check is the eval's own executed search with a fixed number of trials; "no counterexample found" is not proof that a pair is right.
- `Replaced` labels are counted as not fewer even though the verdict `same` or `refines` is a more precise statement than "not modeled".
- The prover-eval and BigQuery-reading figures were measured on `80c0720d` (the #602 merge). Master has moved since: the numeric traps figures were rerun on the merged tree and agree, the long runs were not repeated.
- The report compares against a fixed commit, so after other prover changes land it measures their sum with #602. Rerun it then: the command above is all it takes.

Tests: `tests/test_numeric_assumption_report.py`.
