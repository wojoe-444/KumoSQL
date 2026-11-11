# Picking up a workstream

KumoSQL's ongoing work is split into workstreams. Each one has a GitHub issue labelled
[`workstream`](https://github.com/walterogozaly/KumoSQL/issues?q=is%3Aissue+is%3Aopen+label%3Aworkstream), and that
issue is the whole hand-off: the goal, the current state, the branches and pull requests, what is done and left, the
known failing cases, the files it owns and how to test it. Anyone can continue a workstream from its issue alone,
with a clone of this repository and nothing else. The [workstream index](https://github.com/walterogozaly/KumoSQL/issues/528)
lists them all in priority order: wrong answers and safety first, then work close to merging, then the prover frontier,
then product features.

## 1. Pick an issue

1. Start from the [workstream index](https://github.com/walterogozaly/KumoSQL/issues/528), or open the
   [workstream issues](https://github.com/walterogozaly/KumoSQL/issues?q=is%3Aissue+is%3Aopen+label%3Aworkstream)
   directly. Comments on an issue are newer than its body and win where they differ.
2. Prefer an issue whose open pull requests are merged or closed, or one whose "Left to do" list has unchecked items
   that no open pull request covers. An open pull request listed in the issue is still someone's work in progress:
   build on it only after it merges.
3. Claim it with a short comment ("Picking up: the first two unchecked items"), so two agents don't do the same work.
4. Read the issues it links. "Files owned and collisions" names files another workstream is changing; if you must
   touch one, say so on both issues first.

## 2. Work on a fresh clone of master

```shell
git clone https://github.com/walterogozaly/KumoSQL.git && cd KumoSQL
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
git checkout -b <short-topic-name> origin/master
```

Start every pull request from the current `master`, not from an older branch of the workstream. One pull request per
root cause or per case family keeps review small.

## 3. Test only what you changed

Never run the whole suite (`python tools/run_tests.py` with no arguments). It takes about 40 minutes on four cores,
and the maintainers run it once for a batch of queued pull requests before merging them. Run instead:

- **Targeted tests**, the files your change is aimed at:
  `python tools/run_tests.py --label "<issue title>" --target tests/test_x.py tests/test_x.py`
- **Affected eval floors**, every eval your change could move:
  `python tools/run_tests.py --evals --label "<issue title>" tests/test_<eval>_eval.py`
- **Unchanged answers**, when the change should leave every score alone:
  `python tools/eval_diff.py --only <words>` reruns the matching evals on `origin/master` and on your checkout and
  prints any case whose answer differs.

The issue's "How to test" section names the targets and floors for that workstream.

## 4. Rules that are never relaxed

- **0 wrong.** An eval may never count a wrong proof or a wrong refutation. "Unknown" always beats "wrong": when in
  doubt, decline to prove.
- **Prover floors don't drop**, except where the lost proofs were false proofs; say which in the pull request.
- **Held-out cases stay held out.** Never look at, tune on or debug against a held-out split (for example the mined
  Calcite pairs marked `new`, or the Singh and Bedathur held-out set). If you did, record it as "tuned on test" in the
  eval's results file and docs page.
- **DuckDB's optimizer has wrong-result bugs.** A counterexample found by running SQL on DuckDB counts only if
  `kumosql.duckdb_load.run_unoptimized` returns the same difference.
- **Scores live in `benchmarks/results/<eval>.json`.** When a score moves, update that file (score, size, date,
  caveats) and run `python tools/scoreboard.py`; never edit the scoreboard page by hand. Mention the new numbers on the
  eval's page under `docs/evals/`.
- **New normalization rules go in a new module** with one entry in `algebraic_equivalence.normalize`.
- **sqlglot is pinned below 31** and tested on several versions; fix sqlglot gaps inside KumoSQL rather than waiting
  for an upstream release, and never subclass a sqlglot `Expression`.
- **Tests run in parallel**: no shared files or module-level state between tests.
- **Docs change with the code**: update the matching page in `docs/` in the same pull request (see `CLAUDE.md`).
- **This repository is public.** No secrets, credentials, private repository names, cloud project or dataset names,
  or names taken from anyone's own data in code, tests, issues or pull requests. Describe behaviour only.

## 5. Hand the work back

1. Push your branch and open a pull request against `master` whose description says `Part of #<issue>` (or
   `Closes #<issue>` when it finishes the workstream), lists what you ran from step 3 with the results, and names any
   eval whose score moved.
2. Don't merge it yourself; the maintainers merge after the full suite passes on a batch.
3. Post a short comment on the issue: what changed, the pull request, and what is left. Tick the items you finished.
