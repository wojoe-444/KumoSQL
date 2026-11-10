# Documented rewrites

Vendor and style-guide documentation recommends many query rewrites, and states or implies that they keep the results. This eval writes one KumoSQL case per documented rewrite (BigQuery dialect, its own tables, the page linked) and labels each by hand: 19 are equivalent and 6 change the results as documented. The six are a must-not-prove set taken from real advice: a Redshift tuning page turns a one-day date filter into a range that spans a year, another turns `IN (subquery)` into a join that repeats rows and pushes a filter on a different column, and a SQL Server page "makes a filter sargable" by testing another column. Cases and sources: [tests/fixtures/documented_rewrites](../../tests/fixtures/documented_rewrites/README.md). Results file: `documented-rewrites`.

```
python tools/documented_rewrites_bench.py
python tools/documented_rewrites_bench.py --write-results
```

Labels are checked on DuckDB and cases are decided exactly as for [DB-GPT's examples](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/dbgpt-rules.md) (same code): the prover in the cases' dialect, then random DuckDB databases confirmed with the optimizer off.

## Scores

| Date | Equivalent proved | Not equivalent refuted | Wrong |
| --- | ---: | ---: | ---: |
| 2026-10-03 | 17/19 | 5/6 | 0 |
| 2026-11-10 | 18/19 | 5/6 | 0 |

R012b-22 (a filtered `ROW_NUMBER` subquery against `QUALIFY`) is proved since 2026-11-10: `boolean_identities.py` ([fuzzing.md](fuzzing.md)) drops its `WHERE TRUE`, after which the two windows read the same input; that rule was written for the SQLancer families, not for this case. Unknown: R012b-17 (`REGEXP_CONTAINS(body, '.*abc.*')` against `body LIKE '%abc%'`, different functions to the prover) and R012-039 (the random databases rarely hold two matching region rows with one key; the stored counterexample shows the difference). The cases were written and labelled with every case in view and there is no held-out split (tuned on test).
