# Job lineage fixture

Hand-written for `tests/test_job_lineage.py` and `tests/test_opaque_views.py`. All names are made up.

- `dataform/`: a Dataform project (`shop-analytics.marts`) whose models read old tables in the shared sandbox
  dataset `shop-legacy.sandbox` through declarations.
- `jobs/`: the old scheduled jobs, one script per job, with `jobs.yaml`.
- `history.jsonl`: an `INFORMATION_SCHEMA.JOBS` export: three runs of one scheduled query and two runs of one script.
