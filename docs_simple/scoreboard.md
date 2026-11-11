# The scoreboard, explained simply

[All simple guides](README.md) · [Full reference](../docs/scoreboard.md)

The scoreboard is one page that lists, for every evaluation of KumoSQL, how many cases it checked and how many it got right. An evaluation (eval) is a repeatable test suite for one feature. No language model is used when an eval runs.

## How to read a row

Each row says what kind of evidence stands behind a positive answer, how many cases were scored, the score, the date and the caveats. "0 wrong" means no answer was contradicted by a check on random data. "Unknown" is always preferred to a wrong answer: a row with many unknowns is a row that was careful, not necessarily a row that failed.

For example, a row reading `192/232, 0 wrong` means 192 of 232 cases were proven and none of the proofs was later contradicted. The other 40 are unknown, unsupported or timed out; the coverage table shows which.

## Where the numbers come from

Every row comes from one file in `benchmarks/results/`. A tool builds the page from those files, so nobody edits the table by hand. The equivalence prover's own evals are scored in its separate repository, which is linked from the page.

## What the scoreboard does not show

It shows what was measured on one date on one machine, as the caveats say. It does not show that a score still holds today unless the full benchmark run was repeated since. See the [full page](../docs/scoreboard.md) for every number.
