# Running tests and reading their history

[All simple guides](README.md) · [Full reference](../docs/test-history.md)

Test history records what ran, what failed, and how long it took. It helps distinguish a failure in the feature being changed from a failure elsewhere.

From a development checkout, install test dependencies:

```sh
python -m pip install -e ".[dev]"
```

For a documentation change:

```sh
python tools/run_tests.py --label "simple documentation" --target tests/test_docs.py tests/test_docs.py
```

`--target` records the intended test file in history. The final positional path chooses which tests actually run. Use `-j 2` to choose two workers or `-j 1` for a serial run.

## What to run before merging

Walter approved running broad expensive coverage intermittently so changes get feedback faster. For each code candidate, run the tests and benchmarks affected by the change, then `python tools/run_tests.py --routine`. That checks the fast non-eval regressions. It does not mean every benchmark floor passed.

Changes to shared prover or parser logic still need comparisons across the affected cases and checks that prevent wrong proofs. A gain elsewhere cannot hide a loss. Investigate failures before merging.

Run the full default suite nightly and before releases. Nothing in GitHub schedules that coverage (the owner decided that no CI runs through GitHub); someone starts the full command by hand, locally or on the Dell. Record the exact commit and investigate periodic failures promptly, blocking merges that touch the failing area. Run affected tests marked `slow` explicitly.

One measured Dell candidate took about 10.6 minutes for routine pytest checks versus 65 minutes for full pytest coverage: 83.7% less time. This saves time by changing how often expensive coverage runs. Actual prover improvements need separate before/after measurements. The full run had one subprocess-exit timeout that passed isolated rechecking; it was not a green full result. See the [full procedure](../docs/test-history.md#merge-testing-procedure).

## The quick check on every pull request

`python tools/run_tests.py --gate` is the check to run locally on every candidate before it merges; GitHub does not run it. It runs the fast tests only and must finish within five minutes; if it takes longer, it fails and lists the slowest tests so someone can move them out or make them smaller. The slow benchmark checks run in the periodic full run instead; nothing opens an issue automatically when one fails, so someone has to look at the result. Benchmark runs can also remember which query pairs the prover already proved (`--verdict-cache PATH`), so they re-prove only when the prover's own code changed. "The prover's own code" has to mean everything that can change an answer. For example, the memory used to ignore the file that teaches the parser BigQuery-only syntax and the benchmark script itself, so a change to either could replay answers that were no longer true; both are now part of what is checked. The test for this is a list of known ways an answer can change, so a way nobody has thought of would still be missed. See the [full reference](../docs/test-history.md#the-pull-request-gate) for the details.

The limit of the evidence: the gate shows that the fast tests and the small canaries still pass. It does not show that every benchmark score still holds; the periodic full run does.

## Other useful runs

| Command option | What runs |
| --- | --- |
| No selection | The default suite, excluding tests marked slow |
| `--evals` | Benchmark floor tests |
| `--no-evals` | Tests other than benchmark floors |
| `--quick` | Skips the slow tier (tests that usually take 2 seconds or more, or 10 seconds or more in any run); about 9,500 tests in a few minutes |

The recorder writes one JSON-lines file per run in the shared history directory when configured. Set `KUMOSQL_TEST_HISTORY` to choose the folder, or `off` to disable recording. An ordinary laptop checkout without a shared folder does not record it automatically.

## What the history can tell you

It can highlight tests that failed outside a task's targets even when the targets passed. Those tests may protect behavior that many changes accidentally disturb.

It can also compare durations and place likely failures earlier in a parallel test run. `python tools/test_history.py order --write` regenerates `tests/order.json` from recorded runs.

Example of using it: 29 changes in the recorded history broke a test outside their own targets. Most of the broken tests were cheap (28 of 43 took under a second) and different each time: only 6 of the 43 had failed in an earlier change, and the one real repeat offender, the scoreboard tests (the check that every eval test file is listed in `tests/conftest.py` and the check that the scoreboard page is current), accounts for 6 of the 29 changes. So a short fixed list of "tests that always fail" would have caught only a minority; running every test that is not slow caught 27 of the 29. The two it missed were broken only in slow evals. Treat `--quick` as the run to make before queueing a pull request, and the merge train's full run as the check for the slow evals. This is a small sample from one project's first days of history, so the percentages are a guide, not a promise.

History explains past runs; it does not establish that your current change passes. Run the relevant tests for the current checkout. The full reference covers reports, importing JUnit results, and sharing history across runs.
