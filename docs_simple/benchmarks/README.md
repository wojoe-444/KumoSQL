# Recording benchmark results

[All simple guides](../README.md) · [Full reference](../../benchmarks/README.md)

The large scoreboard in the project README comes from JSON files in `benchmarks/results/`. Each file describes one evaluation at one evidence level.

## Update a result

1. Run the evaluation's recorded command.
2. Update its results JSON with the measured counts, date, and limitations.
3. Run `python tools/scoreboard.py` to regenerate the scoreboard page.
4. Update the matching full evaluation guide and its index when needed.

Do not edit the generated scoreboard table by hand. `python tools/scoreboard.py --check` checks that it matches the recorded files.

## What the fields tell readers

| Field | Plain meaning |
| --- | --- |
| `suite`, `order` | Row name and display order |
| `size`, `score`, `metric` | How many items were tested, the headline result, and what it measures |
| `evidence` | `proof`, `bounded`, or `executed`; keep the levels separate |
| `correctness`, `coverage` | Wrong answers and counts of proved, refuted, unknown, or other outcomes |
| `held_out` | Results on cases kept out of development, or `none` |
| `docs`, `command`, `date` | Where to read more, how to rerun it, and when it was measured |
| `caveats` | Limits such as tuning on test cases, samples, or adapted SQL |

The full reference lists required keys and optional usefulness, analysis, and performance fields.

“0 wrong” means the specified checks found no wrong result. Read it alongside coverage: doing nothing can avoid wrong rewrites while helping no queries. Cases used to develop a rule should be marked “tuned on test,” even if they later pass.

For a code refactor expected to preserve scores, `python tools/eval_diff.py` compares evaluations on the base branch and your checkout. It ignores how long each run took, so only changed answers show up as a difference. It can take hours and needs each evaluation's data and dependencies. Documentation-only changes do not require rerunning the SQL corpora to regenerate unchanged numbers.
