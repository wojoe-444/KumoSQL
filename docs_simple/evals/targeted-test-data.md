# Choosing data that exposes SQL differences

[Simple eval index](README.md) · [Full reference](../../docs/evals/targeted-test-data.md)

Random data can miss the exact value that makes two queries differ. Targeted data is built around the queries' literals, comparisons, joins, and NULL behavior.

For example, `x > 10` and `x >= 10` agree on many datasets. Including `x = 10` exposes the difference immediately. Duplicated join keys and empty tables reveal other traps.

## What this suite checks

- Whether targeted data reveals differences missed by ordinary random data.
- Whether checking several databases improves detection.
- Whether a found counterexample can be reduced while preserving the difference.
- Whether plausible unsafe rewrite variants are correctly rejected or refuted.

Reducing a counterexample makes it easier to read. The smaller database must still make the executed queries disagree; a tidy-looking example that does not replay is not evidence.

## Reading the percentages

The score is the share of faulty variants that the databases tell apart, among variants that were not proven equivalent and did not time out. Because timed-out variants leave the count, a run where more queries time out can show a higher percentage even though fewer variants were caught. Read the kills and the timeouts together; the [full guide](../../docs/evals/targeted-test-data.md) has the numbers and the per-variant comparison that checked no caught variant became a missed one.

## What agreement means

If every tried database agrees, that is executed evidence, not an unbounded proof. The distinction remains even when the databases are carefully chosen.

The full guide explains the default checker, targeted generators, regression cases, and corpus integration. It also separates detection from correctness and minimization results. [Bounded verification](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs_simple/evals/bounded-verification.md) describes a different approach that covers all modeled databases within a row limit.
