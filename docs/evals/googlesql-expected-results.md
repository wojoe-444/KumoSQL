# GoogleSQL compliance expected results

[Plain-language version](../../docs_simple/evals/googlesql-expected-results.md)

KumoSQL's counterexample searches, bounded replay, random-database checks and synthetic comparisons run BigQuery SQL on DuckDB after sqlglot translates it, through one layer ([`bigquery_on_duckdb.py`](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/bigquery-on-duckdb.md)). A difference that layer reports must be a difference BigQuery would show, or the refutation is wrong. The [BigQuery behaviour eval](bigquery-behavior-eval.md) uses the SQL of the same compliance tests and never their results. This eval uses the results: for each query, the typed rows Google's reference implementation returns, as an independent oracle for the layer.

```
python tools/googlesql_results_eval.py                  # fetches google/googlesql at the pin (sparse, GitHub only), runs all
python tools/googlesql_results_eval.py --failures       # lists every disagreement in the development files
python tools/googlesql_results_eval.py --write-results  # updates benchmarks/results/googlesql-expected-results.json
python -m pytest tests/test_googlesql_results_eval.py   # the floor: a pinned sample, in the test suite
```

## Source, pin and licence

| | |
| --- | --- |
| Source | `googlesql/compliance/testdata/*.test` in [google/googlesql](https://github.com/google/googlesql) (formerly ZetaSQL) |
| Commit | `d82db99a923a46571f543a13899dc791a7b6f743`, the pin the BigQuery behaviour eval's queries came from |
| SHA-256 | of each of the 285 `.test` files in [`testdata.sha256`](../../tests/fixtures/googlesql_results/testdata.sha256); a run refuses a checkout that differs |
| Licence | Apache-2.0 ([LICENSE](../../tests/fixtures/googlesql_results/LICENSE)). A sample of the cases and the fixture blocks of their files is checked in as `tests/fixtures/googlesql_results/sample.json.gz`; the full set is fetched at run time |
| Original or adapted | all original; no case is rewritten |

## How a case is scored

1. **Supported or skipped.** A case is supported when BigQuery has what it needs: every `required_features` entry is a BigQuery feature, no protos or enums, no query parameters, no type BigQuery lacks (`INT32`, `UINT64`, `FLOAT`, ...), no prepared function or graph, no case-specific default time zone, a deterministic reference result, and an expected result rather than an expected error. The rest are skipped and counted by reason.
2. **Fixtures.** Each file's `[prepare_database]` blocks create tables. The tables are loaded into DuckDB from the expected rows of those blocks, never by running the setup SQL, so the oracle does not depend on the translation.
3. **Run.** The query goes through `to_duckdb(sql, "bigquery")` (which applies the layer) and runs on one thread with timestamps read in UTC. Rows are read back the way BigQuery returns them.
4. **Compare.** The expected rows are read from the compliance text with their types. `unknown order` arrays compare as multisets, `known order` ones as lists, floats within four ULP bits (the compliance `FloatMargin::UlpMargin(4)`), and a NULL array equals an empty one, as in BigQuery.

| Outcome | Meaning |
| --- | --- |
| agree | the translation returned the expected rows (also when DuckDB's optimizer is off and on only the unoptimized run matches: see below) |
| declined | KumoSQL says no faithful DuckDB reading exists, or a guard says BigQuery would fail |
| not executable | DuckDB or sqlglot cannot run the translation |
| not compared | the result has a type the harness does not read (JSON, intervals, ranges, ...) |
| **wrong** (`DISAGREE` that is a bug) | the translation ran and returned other rows. This must stay 0 |
| oracle-side difference | `DISAGREE` that disappears when DuckDB reads times in `America/Los_Angeles`: the compliance driver's default zone is Los Angeles and BigQuery's is UTC |

A disagreement counts only when DuckDB with its optimizer off ([`run_unoptimized`](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/duckdb_load.py)) returns the same rows, since DuckDB 1.5's optimizer has wrong-result bugs. Cases BigQuery fails at run time (`out_of_range`) are tracked separately: whether the translation also fails, is declined, or returns rows.

## Splits and exposure

One test file in five, by the SHA-1 of its name (`held_out` in `tools/benchmark_corpora.py`), is held out.

1. A baseline ran first, on master's layer: 1,692 of 4,671 supported cases agreed and **97 were wrong** (52 in development files, 45 in held-out files).
2. The first fixes came from the development files only. After them the held-out files had 38 wrong of 1,385 supported: that is the unseen estimate.
3. Those 38 were then looked at and fixed (hex, unicode and octal string escapes, `BYTES` escapes, `IN` over a struct with a `NULL`, `ANY_VALUE .. HAVING MAX`, `NUMERIC` variance, `SPLIT` with a `NULL` delimiter, `REGEXP_INSTR` occurrences, and a file-wide default time zone). The held-out result below is therefore **tuned on test**; read the 38 as the honest unseen figure.

## Scores (2026-10-04)

| Split | Supported | Agree | Declined | Not executable | Not compared | Oracle-side | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 4,633 | 1,612 | 1,191 | 1,338 | 484 | 8 | 0 |
| Development files | 3,265 | 1,062 | 768 | 1,040 | 389 | 6 | 0 |
| Held-out files (tuned on test) | 1,368 | 550 | 423 | 298 | 95 | 2 | 0 |

12,735 cases, 8,102 skipped: 4,214 need a feature BigQuery lacks, 930 are DML or DDL, 673 are fixtures, 494 use a type BigQuery lacks, 476 read a fixture column of one, 411 take parameters, 329 expect an error, 252 are non-deterministic, 213 use protos or enums, 72 use prepared functions, 38 need their own default time zone. Of the 329 expected errors, 291 are run-time errors (`out_of_range`): the translation fails on 110, declines 143, does not get to run 20, and returns rows on 18 (not counted, and not tuned: they show where a guard is missing, so a counterexample on such a database would be one BigQuery rejects). The run takes about 90 s.

Agreement fell from 1,692 to 1,612 because the fixes are refusals: constructs DuckDB reads differently now decline, and 38 cases that need a file's own default time zone are skipped (some of them had agreed only because the zone happened not to matter). The pinned sample (every twelfth supported case plus every disagreement, 80 KB) runs in the suite and each case keeps its recorded outcome.

## What this found

Fixed in the layer, each with a regression test in `tests/test_bigquery_on_duckdb.py`:

- `EXTRACT(MILLISECOND/MICROSECOND)` counted the whole seconds; a backslash in a `LIKE` pattern, `LIKE ANY/ALL`; `SPLIT(s, NULL)`; a set operation operand that is a set operation lost its grouping; `ASC NULLS LAST` and `COUNTIF` over no rows (found first here, fixed on master meanwhile).
- Guards for `AVG`, `STDDEV` and `VARIANCE` of `NUMERIC` and for fractional seconds in `CAST(TIMESTAMP AS STRING)`.
- Refusals where sqlglot's translation changes the meaning: `PARSE_*` and `CAST .. FORMAT`, time zones, `UNPIVOT`, grouping keys that are not columns, `SELECT AS STRUCT` as a table, `GROUP BY ALL` without keys, date text in a set operation, `ANY_VALUE .. HAVING`, `REGEXP_INSTR` occurrences, `IN` over a struct with a `NULL`, string escapes sqlglot leaves undecoded. The list is in [Running BigQuery SQL on DuckDB](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/bigquery-on-duckdb.md).

## Limits

- DuckDB is not BigQuery. An agreement shows the translation returns what the reference implementation returns; the two can differ (the oracle's time zone is one case).
- 1,338 supported cases are not executable and 484 are not compared, so a third of the cases carry no verdict. Mostly sqlglot or DuckDB cannot read the construct (`SELECT * EXCEPT`, pipe syntax, `FULL JOIN USING`) or the result type is not read.
- A refusal is coarse: a construct is declined whether or not the query's values trip the difference (a `NUMERIC` variance is declined even where DuckDB's double arithmetic is exact).
- Guards cover what the compliance tests exercise. A difference those tests do not show (a struct in a table column holding a `NULL` field under `IN`, for example) is not covered.
- The 18 cases where BigQuery fails at run time and the translation returns rows are open.
