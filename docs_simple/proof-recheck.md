# Hunting for wrong proofs

[All simple guides](README.md) · [Full reference](../docs/proof-recheck.md)

When KumoSQL says two queries always return the same rows, that is a proof. The evals check each proof against a modest number of random databases. A wrong proof, one where some unusual database gives different rows, can slip through if that database is rare. The proof re-check is a developer tool that goes looking for those databases much harder.

## An example

Suppose KumoSQL has proved `SELECT name FROM dept WHERE deptno > 1` equal to `SELECT name FROM dept WHERE NOT deptno <= 1`. The tool builds thousands of small databases for the pair: empty tables, tables full of NULLs, duplicated rows, values next to the numbers and strings the queries mention, big numbers, strings that differ only in capitals or trailing spaces. Every database respects the keys and other rules the eval declares. It runs both queries on each one. If the results ever differ, the proof is a candidate false proof, and the tool keeps the smallest database that shows it.

Before it counts a difference, the tool rules out noise: rounding in decimal digits, rows returned in a different order, the database engine picking among tied rows, and a known DuckDB optimizer bug. Only a difference that survives all of that is reported. The optimizer bug can also work the other way and make two different queries look the same, so the tool also runs a share of the databases with the optimizer switched off; a pair that differs only then is flagged for a person to read. New proofs go through this search before they are counted; see [checking a proof before it is counted](proof-admission.md).

## What it has found so far

Every counted proof that has been re-checked so far survived: QED (366), the SQLSolver suites on Calcite, Spark, TPC-H and TPC-C, R-Bot, Cosette, SPES, the mined Calcite pairs (446), Singh and Bedathur (862, tested on DuckDB and on a real MySQL 8) and VeriEQL (7,062). A candidate in VeriEQL was a rounding artifact of the checking engine, which now counts as noise. A candidate in DLBench was two groups tied under `LIMIT 1`, where each engine picks a different one; the tool now tests for that and calls it a tie. The Singh runs did turn up one rule that is wrong for MySQL: it treats a string and a number as never equal, which MySQL does not. No counted proof depended on it.

The tool also covers the fuzzing, rewrite, view-reuse, containment, dialect, pipeline, minimization and bounded-verification evals. Each of those was compared with how its eval proves pairs, and has been run on a small sample, with full runs done for three of them and the rest still to do. Where an eval's recorded count is out of date, the tool follows the eval's code, and the full reference lists the differences.

Some proofs hold only under named conditions (a column is never empty, a column is unique). The tool declares those conditions to the search, so every database it builds meets them, and a difference on one would be a wrong proof. Without the conditions a difference is expected and means nothing.

## Limits

- Surviving the search is evidence, not a proof. A database the search never builds can still hide a bug.
- A difference can also come from the checking engine rather than the proof, for example a different rounding, so each one is reviewed by hand before it is called a bug.
- Some proofs cannot be run on the checking engine (a function DuckDB lacks, a subquery shape it cannot execute); those are listed as not runnable, not as passed. Three SQLSolver pairs that first fell into this group now run, after a small translation for DuckDB (a missing function, a subquery in a join condition, a `CASE` mixing numbers and text); the translation is written out in the reference and has tests that it still separates a deliberately broken copy.
- It is a tool for developers. It changes no scores and no rules; the full reference covers the commands and the output.
