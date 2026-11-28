# Checking incremental models

[All simple guides](README.md) · [Full reference](../docs/incremental.md)

An incremental model updates an existing table instead of rebuilding it from scratch. The question is whether, after each allowed source change, that table matches a full rebuild.

## A small example

Suppose each run loads events newer than the largest timestamp already saved. That can work when events arrive in timestamp order. A late event with an older timestamp gets missed. An update or deletion may leave an old row in the target.

This is why the checker needs a **contract**: which changes the source is allowed to make.

| Contract used by the project scan | What it tests |
| --- | --- |
| `append_only` | New rows under its insert assumptions |
| `late_and_duplicate` | Late arrivals and redelivered rows |
| `mutable` | Updates and deletes as well |

The Python API can specify individual change kinds and which source tables change.

## Scan a project

Install DuckDB support from a checkout with `python -m pip install ".[execution]"`, then:

```sh
python -m kumosql incremental-report path/to/project
```

The scan checks incremental Dataform actions under the three contracts. By default it infers source columns and assumes an `id` key and timestamp-shaped columns. These are assumptions to review. Use `--source-schema` to supply real source information; see command help for the file format.

## Which tables could become incremental

`python -m kumosql incremental-report path/to/project --advise` looks at the tables that are rebuilt in full every run and says, for each, which incremental pattern could fit (new partitions only, partitions found from changed rows, recompute changed keys, or recompute rows whose dimension changed) and what blocks the rest, such as a total over the whole table or a source with no timestamp. These are suggestions: when a proof rule covers the suggested shape it says the conversion can be checked with `incremental-report`, and that check is the one that counts. With `--table-bytes` it adds per-run cost figures, an estimate and an upper bound kept apart; nothing is measured. See the [full reference](../docs/incremental.md#which-tables-could-become-incremental).

## How the search picks changes

When the checker looks for a counterexample it generates random source changes. Some new rows arrive a few hours to a day later than the previous newest row, not just one hour later. That matters for models that delete and reload a short window: if a run adds rows that are further apart than the window, the older ones are never loaded. A separate random stream decides the gaps, so the other choices stay the same as before. The generator can only find problems; failing to find one never counts as a proof. See the [full reference](../docs/incremental.md) for the exact rates and tests.

## Read the verdict

- **safe**: a proof rule applies under the stated contract and assumptions.
- **diverges**: a tested change sequence makes the incremental table differ from a full rebuild or fail. The report includes a replayable example.
- **unknown**: no proof applies and the search found no difference.
- **unsupported** or **timeout**: the model could not be checked within the available support or time.

An unknown is not a safety guarantee.

The simulator models appends and merges. A merge can fail if several source rows match one target row; NULL keys do not match and can be inserted repeatedly. It runs adapted SQL in DuckDB, so it only claims the BigQuery behavior explicitly modeled.

The full guide covers watermark rules, lookback windows, deduplication, grouped summaries, unchanged joined tables, pre-operations, and adapted pg_ivm workloads. Always read the contract alongside the verdict.

## How the rules are tested

Every rule that proves a model safe has a test that shows a model it proves, plus "near miss" tests: the same model changed in exactly one way, which must no longer be proven. For a model that re-runs its whole query and merges on a key, the near misses are a query that differs between runs, a key that is not unique, a key that can be NULL, a key that can disappear from the result, and a result that depends on how ties are broken. The same is done for the rules that decide whether keys can disappear, for script variables and statements that change no data, and for ties. A tie-dependent model is reported as `nondeterministic` only when two evaluations of its query actually give different results; a query with a total order is never reported that way.

This is evidence that the rules are not wider than documented, not a proof that they are right. See the [full reference](../docs/incremental.md#test-coverage-for-r7-key-growth-script-variables-and-ties) for the list of tests and the two bugs they found.
