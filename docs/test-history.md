# Test history

[Plain-language version](../docs_simple/test-history.md)

Every test run in this repository is recorded, so a report can show which tests break most often and which of them break changes that otherwise work.

## Merge testing procedure

Walter fully approved intermittent full-suite coverage on October 5, 2026 to shorten routine feedback by more than 70%. SQLGlot/sqlglotc remains pinned to 30.21.0, one compiled configuration. This replaces the earlier full-suite requirement for every combined merge batch.

1. **Every code candidate:** run targeted regressions, then `python tools/run_tests.py --routine --label "candidate <sha>"`. This is `--no-evals --quick`: broad fast regression coverage, not a claim that every eval floor passed. Positional paths select tests; `--target` only labels intended coverage.
2. **Affected evals before merge:** an eval-score change must run that eval. Shared prover, normalization, parsing or execution changes require broader base/candidate comparisons of case verdicts and soundness guards, not just a net score. Select impacted expensive checks explicitly, including affected `slow` cases. Preserve floors and zero wrong proofs; investigate failures and crashes. A soundness correction must document corrected scores and guards.
3. **Intermittent broad coverage:** run `python tools/run_tests.py --label "periodic full <sha>"` nightly on the current integration revision, and before a release. GitHub's scheduled and manually dispatched workflow uses this full default suite; pull requests and pushes use the routine profile. Actions must be enabled for GitHub to execute either workflow. The Dell can supply the same commands. Full coverage includes eval floors and excludes `slow`; run affected slow checks explicitly. Diagnose a full-suite regression promptly and block affected merges until resolved; do not describe routine results as full validation.
4. **Evidence:** record exact candidate SHA, commands, tests, failures and elapsed time. A new candidate needs its relevant checks again. SQLSolver's optional [execution cache](evals/sqlsolver.md#reusing-sample-execution-checks) never bypasses the current prover or targeted difference search; `--eval-cache off` forces fresh sample execution.

On this Dell, the same compiled candidate `e684f16461`, four workers, took 635.69 seconds for the routine profile (10,853 passed) and 3,897.96 seconds for the full default run (11,645 passed and one subprocess-exit timeout). That is **83.7% shorter pytest runtime**; runner wall time including setup was approximately 12 versus 71 minutes. The full-run timeout is a separate failure, not a passing result: all ten cold-start proof verdicts printed, and both affected cases passed an isolated compiled recheck. These timings show the coverage-profile difference, not a prover algorithm speed claim or a guarantee on every machine.

The merge operator must inspect relevant candidate evidence. GitHub branch protection presently does not enforce it; post-merge Dell monitoring is not pre-merge validation.

## The pull-request gate

`python tools/run_tests.py --gate` is the check every pull request and push runs on GitHub Actions ([roadmap](roadmap.md) step 2). It is the `--routine` profile (`--no-evals --quick`) held to a time budget: `--budget SECONDS` fails any run that takes longer, and `--gate` sets it to **300 seconds** (five minutes on four cores). When the budget is exceeded the run prints the slowest tests (everything over 2 seconds) and exits with code 3, so the gate cannot grow back unnoticed.

What keeps it short:

- **Nothing over about 2 seconds runs in the gate.** A test of 2 seconds or more (setup, call and teardown) is recorded in the slow tier of `tests/order.json`, and `--quick` skips that tier. Regenerate the file with `python tools/test_history.py order --write` in a pull request of its own when the history shows new slow tests.
- **Benchmark floors do not run in the gate.** Files that declare `pytestmark = pytest.mark.eval` are the floors; the nightly run owns them and opens an issue when one fails.
- **Canaries.** A change that breaks the wiring of a benchmark family is caught by its small canary test, which runs a few cases in seconds. A canary never claims the family's score.
- **Cached proof verdicts.** The benchmark runs can remember a proof's yes or no in a folder (`--verdict-cache PATH`, or `KUMOSQL_VERDICT_CACHE`), keyed by the query pair, every option that changes the verdict, the library versions and a hash of the source of every module the provers import (`kumosql.verdict_cache.prover_modules`). A prover change is a miss for every pair, so a benchmark re-proves only when the prover changed. Only the verdict is stored, as JSON; a damaged file is a miss. The cache is off unless a folder is named, and only runners that need just the yes or no use it (`tools/sqlsolver_bench.default_prove`, which the SQLSolver, QED, R-Bot, mined Calcite and bounded suites call). What the hash covers was too narrow until 2026-11-10: it left out `bigquery_syntax` (the parser patch that every prover reads its input through, installed by the package and imported by no prover module), followed only imports spelled with the literal package name `kumosql` (so in the split-out prover package, which has another name, absolute imports were not followed), read only the `__init__` of a subpackage, and ignored the runner itself, so an edit to `tools/sqlsolver_bench.py` that changed how a pair is put to the prover replayed the old verdicts. `PROVER_ROOTS` now names the parser patch, imports are resolved against the package's own name and from files inside subpackages, and the runner adds a hash of its own source to the key (`verdict_cache.source_digest`). `tests/test_verdict_cache.py` covers each. The versions of the libraries and of Python are in the key; an edit to a fixture is not, because the query texts themselves are.

The workflow in `.github/workflows/tests.yml` has two jobs: `gate` on pull requests and pushes, and `nightly` on the schedule and by hand, which runs the full default suite with the verdict cache and opens an issue when it fails. GitHub Actions has to be enabled for the repository for either to run, and the proposed wording for `CLAUDE.md` (benchmarks report and do not gate) waits for the repository owner.

## What is recorded

`tests/conftest.py` installs the recorder from `tools/test_history.py`. After any `python tools/run_tests.py` or `python -m pytest` run it writes one JSON line to its own file under `runs/` in the history folder, so runs on different threads never collide. The folder is `$KUMOSQL_TEST_HISTORY`, or `/mnt/project-files/test-history` when `/mnt/project-files` exists; `KUMOSQL_TEST_HISTORY=off` turns recording off. Without a shared folder (a laptop checkout) nothing is written.

A record holds:

- the commit, branch, whether the checkout had uncommitted changes, and a task label (`--label`, `$KUMOSQL_TASK`, else the branch name);
- the **targets**: the tests the run was aiming at. They come from `--target PATH` (repeatable; `$KUMOSQL_TEST_TARGETS` for plain pytest), else the test files given on the command line, else the test files the branch added or changed against the master it branched from. A run with none of these has no targets and cannot be attributed;
- the tests that failed, each marked as inside or outside the targets, with the first line of the message;
- the run mode (`full`, `evals`, `no-evals` or `partial`), counts, library versions (sqlglot, sqlfluff, z3, DuckDB) and how many tests ran per file;
- the times: the run's wall time (`seconds`) and CPU time across every worker (`cpu_seconds`, start-up and collection included), each file's wall and CPU seconds (`file_seconds`, setup to teardown, summed over its tests), their totals (`test_seconds`, `test_cpu_seconds`), and, for every test that took 2 seconds or more from setup to teardown, that duration (`slow`) and its CPU seconds (`slow_cpu`), and every setup of 3 seconds or more (`slow_setup`: a shared fixture built for that test). Records before version 3 timed the slow tests' call only;
- the machine: cores, processor, memory, operating system and whether sqlglot ran compiled or pure (no host or user names).

CPU time counts every thread of a worker (DuckDB's too) and the child processes a test waited for, such as a benchmark's process pool. On Windows only the worker's own time is counted. Wall time depends on how busy the machine was; CPU time is the steadier measure of the work a test does, and the one to compare when a change is meant to make a test cheaper.

## Reading it

```shell
python tools/test_history.py report            # rankings
python tools/test_history.py report --since 7  # the last week
```

- **Tests that break changes that otherwise work.** Runs aimed at known tests where every target passed, on a branch other than master, and the test that failed anyway. Ranked by runs, then by how many different tasks it hit. A test high on this list guards behaviour that many fixes disturb, so it is worth running first and worth reading before changing the code it covers.
- **Tests that fail most, by where.** Failures inside the targets, outside them, on master and in runs with no known targets, over the number of runs that ran the test's file.
- **Flaky candidates.** Tests that failed in one run and passed in another at the same clean commit.
- **Slowest tests.** Median seconds.

```shell
python tools/test_history.py trend                                       # test times over time
python tools/test_history.py trend --test tests/test_qed_benchmarks.py   # one file's (or one test's) history
```

`trend` lists the whole runs (`full`, `evals`, `no-evals`), newest last, with commit, workers, cores, the sqlglot build, wall and CPU time; then the files whose wall time moved most between the earliest and the latest whole runs of the same mode on the same core count; then where the CPU goes in the newest whole run, file by file. `--test` prints every recorded time of the files, or the slow tests (an id with `::` or `[`), whose name contains the text. Runs recorded before the times were kept show the slow tests' summed seconds and no CPU.

Runs with 20 or more failures at once (a missing z3 or a wrong library version) are left out of the rankings.

When a run has targets and something outside them fails, the end of the pytest output says so and, where history exists, how many earlier changes that test broke:

```
targets (changed-tests): tests/test_quantified_rules.py
0 failed inside the targets, 1 outside them
  outside the targets: tests/test_x.py::test_q12 (broke 4 earlier changes that otherwise worked)
```

## Running failures first

`python tools/test_history.py order --write` writes `tests/order.json` from the history: the slow tier (tests that took 3 seconds or more, usually or by a wide margin, with their median durations), the files with a slow shared fixture (`together`, with the whole file's median seconds), the tests that failed most, and every test file the history has run (`files`). Under pytest-xdist `tests/conftest.py` then runs, in this order:

1. any test that alone takes more than half of one worker's share of the run (started after the fast tests, it would end the run late);
2. tests that failed before;
3. the fast tests, so a broken one shows up in minutes;
4. the slow tests, longest first, so one long benchmark never runs alone at the end.

The workers take tests in exactly that order. `tools/xdist_scheduler.py` replaces pytest-xdist's `loadgroup` scheduler, which kept up to three tests queued on each worker and moved test groups to the front: a worker now takes more work only while everything it holds is fast. A pytest-xdist worker starts a test only once it holds the one it runs next as well, so a worker starting a slow test gets the first fast test left (or the quickest one) as its next test, never the next slow one, and two long benchmarks never wait in one worker's queue while another worker is idle. Groups (`xdist_group`) still run on one worker, and so do the tests of a `together` file: the scheduler hands the file out as one unit, so its fixture is built once instead of once per worker, and its test ids stay as they are (an `xdist_group` mark would change them). The first full run that timed fixtures found five such files (the duplicate, lineage-goldens, LLM-SQL-Solver, analysis-scale and optimizer-bugs benchmarks), whose module fixtures had been built on two or three workers each; run on 4 workers, those five files took 29.0 s wall and 101.5 s CPU split across workers and 24.6 s wall and 52.0 s CPU with one worker per file. In a full run on 4 workers the workers were busy 98% of the run's wall time with this scheduler, against 76% with `loadgroup`, whose last three workers sat idle for the last 13 minutes while one worker finished the MV workload eval and the tests queued behind it.

Without `tests/order.json` data it falls back to starting the files in `HEAVY_FILES` first. `python tools/run_tests.py --quick` skips the slow tier and runs the rest (about 9,500 tests; 4 to 7 minutes on a 4-core container, about a tenth of the CPU time of a full run). A run records a test only when it took 3 seconds or more, so a test that was slow on a loaded machine in two runs out of a hundred looked slow to the median of those two runs and was dropped from `--quick`. The slow tier therefore holds a test only if it was slow in at least half of the runs that ran its file or its median slow run took 10 seconds or more; a cheap test that spiked now and then stays in `--quick`. Across the recorded runs, 27 of the 29 changes whose branch broke a test outside its own targets broke at least one test this tier keeps in `--quick`; the other two were caught only by slow tests (the quantified-rules cases in one, the QED Calcite and Cosette SPES floors in the other). A test that is cheap alone but sits in an eval file the history has never run still counts as slow until `order --write` has seen it, so refresh `tests/order.json` after adding eval tests. `python tools/run_tests.py -x` stops at the first failure.

Regenerate `tests/order.json` now and then, in a small PR of its own, once the history has more runs (the test speed thread does it daily). A test missing from it counts as fast, except in an eval file (one that declares `pytestmark = pytest.mark.eval`) that the history has never run: those count as slow (60 seconds) until a recorded run times them, so a new benchmark neither lands in `--quick` nor starts late.

## DuckDB on one thread

Tests open thousands of tiny in-memory DuckDB databases (a few rows per table). `tests/conftest.py` wraps `duckdb.connect` for the whole test process, and the pool workers its tests fork, so a connection gets `threads=1` unless the caller passes `threads` itself; every other argument and config key goes through unchanged. By default DuckDB starts a thread per core for every database, and under pytest-xdist those threads only compete with the other workers for the same cores. When pandas is not installed the same hook also puts `None` under `pandas` in `sys.modules`: DuckDB otherwise tries `import pandas` for every bound parameter of `execute` and `executemany`, and each try searches all of `sys.path` before it fails. `import pandas` still fails and `importlib.util.find_spec("pandas")` still returns None; an installed pandas is left alone. `tests/test_duckdb_defaults.py` checks both. On nine DuckDB-heavy test files (the VeriEQL, Singh and Bedathur, targeted-data, behaviour, incremental and fuzzing floors, the quantified-rules and result-equivalence tests), run serially before and after on a shared 4-CPU machine, the two together cut CPU time by 22% (2,379 to 1,853 seconds; 3% to 35% per file) and every eval verdict stayed the same. A caller that passes `threads` keeps its own setting: a test or benchmark that needs more threads passes `threads` (or runs `SET threads`), and a helper that already opens its databases with `threads=1` works the same with or without this default.

## Seeding and reuse

`python tools/test_history.py import-junit junit.xml --commit SHA --branch master --label NOTE` adds an existing JUnit file to the history. Records are plain JSON lines, so other tools can read `runs/*.jsonl` directly.
