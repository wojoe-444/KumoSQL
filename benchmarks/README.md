# Benchmark results format

[Plain-language version](../docs_simple/benchmarks/README.md)

The scoreboard page is generated from the files here: one `results/<eval>.json` per eval, one object each. Edit your file, then run `python tools/scoreboard.py` (the tool fails on a missing required key or an unknown value). `python tools/scoreboard.py --check` is what the test suite runs.

An eval script can write its file itself: `tools/bench_common.py` has `write_results(name, row)`, which writes `results/<name>.json` (adding an `environment` key with the Python, sqlglot, sqlglotc, DuckDB, z3 and SQLFluff versions and the git commit measured, `+changes` when the checkout had edits) and regenerates the scoreboard, plus `today()` for `date` and `quiet()` for command-line runs. The lineage, Dataform, schema-change and SQLLineage benches use it (`--write-results`).

Required keys:

| Key | Meaning |
| --- | --- |
| `suite` | Row name, for example `SQLSolver Calcite` |
| `order` | Sort position; leave gaps (10, 11, 20, ...) so new rows slot in |
| `size` | Number of items scored |
| `score` | Headline score, such as `192/232, 0 wrong` or `83.24% (1157/1390)` |
| `metric` | One sentence on what the score means |
| `evidence` | Strength of the evidence behind each positive answer: `proof` (unbounded proof), `bounded` (bounded verification, VeriEQL-style bounded model checking) or `executed` (agreement on executed datasets). Keep these distinct; a suite that mixes them gets one results file per level |
| `correctness` | False proofs, incorrect counterexamples, behaviour-changing rewrites. Say how it was checked |
| `coverage` | Object with any of `proven`, `refuted`, `unknown`, `unsupported`, `timeout`, `error` (counts; `{}` if the eval has no such outcomes) |
| `held_out` | Score on a held-out split, or `none` if every case was available while developing |
| `docs` | Link (relative path, anchor allowed) to the eval's docs |
| `command` | Command that reruns the eval |
| `date` | `YYYY-MM-DD` the numbers were measured |
| `caveats` | Honest limits: `Tuned on test`, subset only, number copied rather than rerun, and so on |

Optional keys, shown in their own table when any row has them: `usefulness` (how often a rewrite gives a verified improvement), `analysis` (lineage and duplicate-detection precision and recall) and `performance` (runtime and memory by query complexity).

Optional keys that are checked but not shown in the README:

| Key | Meaning |
| --- | --- |
| `coverage_of` | One sentence naming what `coverage` counts and its total, when that is not the `size` items: another unit (traced columns, the rewrite steps that changed the SQL), a stage (cases skipped before translation counted as `unsupported` beside the scored ones), a subset (the pairs another row's prover leaves unknown) or failures only (`{"error": 0}` over estimates that have no equivalence outcome) |
| `environment` | Object recording where the numbers were measured, such as library versions and the git commit; optional, and must be an object when present |

Rule: report like `X/Y, 0 wrong`; unknown beats wrong. Nonempty `coverage` counts must add up to `size`, or the row says what they count instead in `coverage_of`; `python tools/scoreboard.py` (and `--check`) fails otherwise. Fix a count that misses an outcome rather than explaining it away: `coverage_of` is for a different unit, stage or subset, not for cases that went uncounted.

## Held-out splits

Where an eval has a held-out split, its bench takes `--split dev|held-out|all` and reports the held-out cases on their own; the results file's `held_out` gives that score. Develop on `dev`. `tools/llm_sql_solver_bench.py` defaults to `dev` (its `--write-results` scores every pair), `tools/analytical_coverage.py` to `dev`, and `tools/calcite_mined_bench.py` to `all`, the published command, which prints the held-out pairs (those new to every other corpus) separately.

## Checking that a change moves no score

`python tools/eval_diff.py` reruns every eval's `command` on `origin/master` (in a temporary worktree) and on your checkout, side by side, and prints `same` or the lines that differ for each, with timings masked. Run it before merging a refactor or shared helper that should not change any answer, or to see exactly which evals a change moves. `--only WORD...` and `--skip WORD...` pick commands, `--base REF` changes the comparison point, and `--list` prints the commands. Commands that need a local checkout (`<SQL-IQ checkout>`, `PATH`) are skipped. A full run is long (hours on four cores); the slowest are `targeted_data_bench`, `verieql_bench leetcode` and `dup_bench`.
