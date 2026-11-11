# Spider 2.0 repurposed for KumoSQL

[Plain-language version](../../docs_simple/evals/spider2-bench.md)

Spider 2.0 ([xlang-ai/Spider2](https://github.com/xlang-ai/Spider2), MIT, Copyright (c) 2024 bird_sql) is a text-to-SQL
benchmark. Its official score (a model writes SQL that returns the right rows) is not what this measures. This reuses
its published **BigQuery reference queries** as realistic inputs for KumoSQL's own analyses, with no model at run time.

`python tools/spider2_bench.py [--split dev|held-out|all] [--failures] [--write-results]`

## Source and pin

- Fetched 2026-10-02 from the upstream `main` branch (`spider2-lite/evaluation_suite/gold/sql`,
  `spider2-lite/spider2-lite.jsonl`, `spider2-dbt/examples/spider2-dbt.jsonl`). github.com and its API are blocked from
  here, so the commit id could not be read; the files are pinned by SHA-256 in `tests/fixtures/spider2/manifest.json`.
- 205 BigQuery and GA4 tasks exist; upstream publishes gold SQL for **142** (the rest are withheld). Those 142 are the
  corpus, kept unmodified in `tests/fixtures/spider2/gold`.
- The 68 **dbt tasks** are kept as instructions (`dbt_tasks.jsonl`) but not scored: their project archives are on
  Google Drive, which the proxy blocks. They count as unavailable, not as passed or failed.
- Overlap: the SQL-IQ, QED and R-Bot evals use other query sets; the analytical coverage suite runs the same syntax
  stages on TPC-DS/DSB/SQLStorm. No Spider 2.0 query appears in them. `tools/spider2_bench.py` reuses that suite's
  parse/load/graph stage functions.
- Split: every fifth task by SHA-1 of its id is held out (30 queries); the other 112 are dev. Dev was used to find and
  fix bugs; the held-out first run came after.

## Stages (original and adapted cases kept separate)

| Stage | Kind | What it checks |
| --- | --- | --- |
| parse, load, graph | original | the unmodified gold query parses as BigQuery, loads as a model, its table reads match an independent extraction |
| lineage | original | every output column is traced to source columns or honestly reported unknown |
| rename | adapted, answer by construction | a model that renames every output column must trace to exactly the sources of the original column |
| drop | adapted, answer by construction | dropping an output column must break the renaming model and nothing else |
| cleanup, format | rewrite | each cleanup rule and the formatter run on every query; a change is checked by the prover |

Outcomes per stage over the whole corpus are passed, failed, unsupported, timeout and error, plus the score on the
supported subset (passed out of passed + failed). A query times out after 60 CPU seconds (the slowest takes about 9),
with a 600 s wall-clock backstop for a query that blocks; measuring CPU rather than wall time keeps a busy machine,
such as a parallel test run, from timing queries out. For rewrites the table reports how many queries each rule changed, how
many of those changes the prover proved equivalent (`verified`), and how many were damaged (disproved). Changes the
prover cannot prove are never applied. There is no execution evidence: the data are public BigQuery tables, so
correctness rests on proof and on construction.

## Results

See the scoreboard row and `benchmarks/results/spider2-bigquery.json`. Dev found and fixed three real bugs:

- the SMT prover compiled every outer join into twice the cases, so a query with many left joins exhausted memory in
  Python before Z3's timeout applied (killed after minutes); it now gives up past 256 cases and the rewrite stays unproven;
- near-duplicate search crashed on an `UNNEST` over a subquery (a BigQuery generator assertion);
- a derived column such as `COUNT(*)` read through a subquery was reported as unknown lineage instead of constant.

Unknown lineage that remains is a `SELECT *` over a public table whose columns are not known (no schema is supplied)
or an unresolved reference; supplying a catalog would resolve them.
