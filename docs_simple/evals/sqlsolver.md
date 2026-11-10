# Algebraic proofs and optimizer query pairs

[Simple eval index](README.md) · [Full reference](../../docs/evals/sqlsolver.md)

The algebraic prover treats row counts as arithmetic. UNION ALL adds copies; joins combine them. This helps reason about queries that look different but compute the same result.

KumoSQL implements this reasoning in Python on top of Z3. The optional Java SQLSolver backend is separate; installing ordinary SMT support does not require Java or administrator access.

## What the corpora test

This guide covers SQLSolver pairs and related R-Bot, QED, Cosette, SPES, and mined Calcite optimizer tests. Each corpus has its own conversion limits, declared keys, disputed cases, and overlap with other corpora.

Many pairs are intended to be equivalent. Failure to prove one is **unknown**, not evidence of a difference. Some audited pairs have real counterexamples and must stay unproven.

Proofs are cross-checked on random databases. That can expose a false proof, but passing the random checks is not what makes the result an unbounded proof. Small random databases often repeat (an empty table, a one-row table), so the cross-check skips a database it already ran on the same pair; the answer would be the same, so this only saves time.

## Reusing checks from an earlier run

The SQLSolver eval can remember sample-data checks in a local folder selected with `KUMOSQL_EVAL_CACHE`. The prover still runs on every pair, and the separate search for a counterexample still runs. It only skips repeated execution of the same SQL against the same generated samples. For example, a change to the prover that proves the same pair can reuse that pair's sample check; changed SQL, tables, samples, checking code or engine versions require fresh checks.

The samples use a fixed random seed, so they repeat across unchanged runs. Remembered checks expire after seven days. A broken or unavailable cache simply causes the checks to run again. Leave the setting unset, or set it to `off`, for a full fresh check; CI does that by default. This saves repeated work and does not turn a few sample executions into a proof. See the [full reference](../../docs/evals/sqlsolver.md#reusing-sample-execution-checks) for the exact inputs and limits.

## A cut after a parenthesized query

`(a UNION b) ORDER BY k LIMIT 1` keeps one row of the combined result. An earlier version of the prover lost that `LIMIT` when it looked inside the parentheses and called the query equal to `a UNION b`, which keeps every row. For example, over the values 3, 3, 3, 4 and NULL, the first returns one row and the second returns three. Both provers now keep the cut, in a derived table, a CTE, a subquery or at the top. When two cuts are stacked in a way that cannot be combined, the answer is "not proven". This covers only the shapes that were tested; the full guide lists them and the regression tests.

## Quantified comparisons written two ways

`x > ANY (SELECT ...)` asks whether `x` is larger than at least one value of the subquery. An optimizer can rewrite it with `MIN` and `COUNT`. KumoSQL now proves the last three such pairs in R-Bot's set, where the columns are declared NOT NULL. For example, `SELECT empno > ANY (SELECT deptno FROM dept) FROM emp` and its `MIN`-based form agree when `deptno` can never be NULL; when it can be, a NULL among the values makes the first form NULL where the second says FALSE, and that pair stays unproven. The rules were written with those three pairs in view, so this is not evidence about quantified comparisons in general. See the [full reference](../../docs/evals/sqlsolver.md#r-bots-last-three-any-pairs).

## Eager sums through a join

An optimizer can turn `SELECT SUM(sal) FROM emp JOIN dept ON ...` into a sum of per-job sums multiplied by the number of matching departments, with a `CASE` that returns NULL when nothing was counted. KumoSQL reads that form back into the plain `SUM`, but only where the two really agree: the salary column must be declared NOT NULL (otherwise a group whose salaries are all NULL sums to NULL in the plain query and to 0 in the eager one), the joins in between must not pad rows with NULLs, and the grouping must be a plain `GROUP BY`. For example, with a nullable salary and one employee whose salary is NULL, the two queries return NULL and 0, so that pair is shown different, not proved. Every rewrite was also compared with the original on small random databases, but that is a check on the rule, not a proof for every query shape; shapes outside the tested list stay unknown. See the [full reference](../../docs/evals/sqlsolver.md) for the exact conditions and the recorded score.

One mined pair, `testSortJoinTranspose1`, is deliberately left unproven: it moves a `LIMIT 10` below a join while ordering by a column with repeated values, so which tied rows survive can differ between the two forms (DuckDB shows it on small random data). Unknown is the honest answer there.

## Read the caveats

A recorded score is only as fresh as the last run that checked it. The TPC-H row said every pair was proved for five weeks in late 2026 while one pair had quietly stopped proving, because its check sat in a slow group of tests that the everyday runs skip. For example, the pair compared `SUM(price * (1 - discount))` with `SUM(price * (1.00 - discount))`. The pair is proved again and now has a fast test of its own; other slow checks may still hide a similar drift. The [full guide](../../docs/evals/sqlsolver.md) has the dates.

Some rules were built while inspecting failing corpus cases, so those scores are “tuned on test.” A case once held out ceases to be an untouched test if it is later used in development. The full guide tracks that exposure.

Other suites overlap. Adding their case counts does not produce a count of unique independent tests.

KumoSQL requires SQLGlot 30.21.0, and routine testing uses the matching compiled build. Older parser scores in the reference describe historical measurements.

The reference includes Python and optional Java setup, supported refactors, ORDER BY/LIMIT rules, cases that must remain unknown, source conversion, and corpus-specific results. Start with [provers](../provers.md) for the evidence levels, or [bounded verification](bounded-verification.md) for the separate small-database check.
