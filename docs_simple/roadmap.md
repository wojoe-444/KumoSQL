# Where KumoSQL is going next

[All simple guides](README.md) · [Full reference](../docs/roadmap.md)

This page explains the plan that came out of a review of the whole repository in November 2026. If you are about to work on KumoSQL, read the [full roadmap](../docs/roadmap.md) first: it lists the work in order and says what is decided.

## Why there is a plan

Work slowed down because every change, however small, had to wait for a long test run. Most of that run was benchmarks for the part of KumoSQL that proves two queries return the same results. The parts that help someone understand a pipeline were quick to test but waited in the same line.

The review also found that KumoSQL's most useful answers are hard for an AI agent to ask for. The answers exist, but they come bundled into one very large report.

## What changes

- **A short test run before merging.** The check that guards a merge should take about five minutes. Benchmarks run on a schedule instead and report their scores.
- **The prover gets its own home.** The code that proves queries equivalent moves to a separate package, with its own tests and scoreboard. KumoSQL uses a fixed version of it.
- **One server that answers one question at a time.** An agent asks "where does this column come from?" or "what breaks if I drop it?" and gets a short answer, not the whole project.

## An example

Take a Dataform project with 107 models. Today an agent that wants to know where one column comes from has two choices: read all the model files (about 141 KB of text) or run the pipeline report (about 1,200 KB). The answer it needs is under 1 KB, and KumoSQL can already compute it in a few milliseconds once the project is loaded. The plan is to make that small answer the thing an agent gets.

## Limits of the evidence

- The size and speed figures come from one public project and one machine.
- The count of past changes is a sample from one week, grouped by file name.
- Nobody has yet measured an agent doing real analyst work with and without the new server. That trial is a step in the plan, and until it runs, "fewer turns" is an expectation, not a result.
- Only the prover split is decided. Changes to the project's working rules still need the owner's agreement.

The full roadmap has the numbers, the list of proposed tools and the order of work.
