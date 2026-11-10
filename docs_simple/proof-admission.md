# Checking a proof before it is counted

[All simple guides](README.md) · [Full reference](../docs/proof-admission.md)

KumoSQL's scoreboard says how many query pairs each benchmark proves equal. A wrong proof in that count is the worst kind of mistake the project can make, because the number looks better while the tool got worse. Before this check, a count could go up as soon as the prover said "equal" and a short random test agreed. Now a new proof has to pass two harder tests first, and the project keeps a list of which proofs have been let in.

## An example

Suppose someone teaches the prover a new rule, and a pair it could not prove yesterday is proven today. The benchmark's count goes from 371 to 372. A quick test in every pull request compares that count with the list of admitted proofs, sees 372 against 371, and fails with the command to run. That command does two things with the new proof:

1. It builds thousands of small databases (empty tables, NULLs, duplicates, values right next to the numbers in the queries) and runs both queries on each. Any database where the answers differ shows the proof is wrong.
2. It makes small mistakes in the queries on purpose, such as `<` instead of `<=`, or an inner join instead of a left join, and asks the prover again. A query with a mistake in it is almost never equal to the original. If the prover still says "equal" and a database shows the two differ, the new rule is too generous, and the proof is rejected.

Only a proof that passes both is added to the list, and only then does the quick test pass.

## What it found on day one

The first run compared nine benchmarks' counts with what the prover really proves. Eight matched. One row on the scoreboard said 22 of 22 while the prover proved 21; an unrelated bug had quietly lost one proof weeks earlier and nobody had noticed. It was fixed the same day.

Looking for wrong proofs by hand at the same time, with BigQuery itself as the judge, found three kinds, all in how the prover reads BigQuery SQL: a decimal written next to an exact-decimal column, and a name that is both a new output and a table column in `ORDER BY` and in `QUALIFY`. All three are fixed, and [how KumoSQL checks whether queries match](provers.md) explains them. A run of 20,000 machine-generated pairs found no wrong proof.

## Limits

- Passing both tests is strong evidence, not a guarantee. A rare database that was never built can still tell two queries apart.
- The tests run the queries on DuckDB. Where BigQuery and DuckDB read a query differently, these tests cannot see a mistake; two of the three kinds found by hand were like that.
- The proofs that were already counted when the list was started were let in without these two tests. They had been through an earlier round of the first test.
- Nine benchmarks have a list so far; the others are not covered.
- The quick test compares counts. If a change loses one proof and gains another, the count stays the same; a slower command finds that, and it has to be run on a schedule.

The [full reference](../docs/proof-admission.md) has the commands, the file format and the recorded numbers.
