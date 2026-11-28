# Query runner: `python -m kumosql q run`

`q run` runs one read-only BigQuery query and behaves the same in PowerShell, cmd and bash, so an agent can run a query in one call and read the outcome without parsing console noise. It was built for a test user whose agent sessions hit the same Windows traps about 300 times, with about 150 errors or retries: multi-line SQL passed as an argument was cut to its first line, a byte-order mark failed the query, progress banners mixed with the results, PowerShell turned stderr into errors, and every query took two tool calls (write the file, run it).

What it does about each trap:

| Trap | What `q run` does |
| --- | --- |
| Multi-line SQL as an argument is cut to its first line | The SQL comes from a file or from stdin (`-`), never from an argument |
| A byte-order mark fails the query | A UTF-8 or UTF-16 byte-order mark is removed; Windows line ends become `\n`, so BigQuery sees, and caches on, the same text on every OS |
| Progress banners mix with the results | stdout carries exactly one line, a JSON summary, and nothing prints progress |
| PowerShell turns stderr into errors | The outcome is in the exit code and the stdout summary. stderr is empty on success and has one short line on failure, which you can drop (`2>$null`) without losing anything |
| Two calls per query | Pipe the SQL in: one call runs it and writes the result to a file |

## Example

The same query in each shell. Each command prints one line of JSON and writes `result.csv`:

```powershell
# PowerShell: a here-string piped to stdin
@'
SELECT order_id,
       status
FROM `my-project.sales.orders`
WHERE status = 'open'
'@ | python -m kumosql q run - -o result.csv --project my-billing-project
```

```bash
# bash
python -m kumosql q run - -o result.csv --project my-billing-project <<'SQL'
SELECT order_id,
       status
FROM `my-project.sales.orders`
WHERE status = 'open'
SQL
```

```bat
:: cmd has no here-document: give the file (or `type query.sql | ...`)
python -m kumosql q run query.sql -o result.csv --project my-billing-project
```

The summary, on one line (wrapped here):

```json
{"status": "ok", "exit_code": 0, "error": null, "backend": "bigquery", "rows": 42, "total_rows": 42, "truncated": false,
 "columns": 2, "bytes_estimated": 1048576, "bytes_accuracy": "exact", "within_cap": true, "max_bytes_billed": 1073741824,
 "bytes_processed": 1048576, "bytes_billed": 10485760, "cache_hit": false, "job_id": "job_abc123",
 "output": "C:\\work\\result.csv", "format": "csv", "checked": ["read_only", "static", "dry_run"], "not_checked": [], "warnings": []}
```

`python -m kumosql query run` is the same command (an alias, for reading next to the other commands).

## The summary

Every run prints the same keys in the same order, on success and on failure. The line is ASCII (other characters are escaped as `\uXXXX`), so it reads the same in every console code page.

| Key | Meaning |
| --- | --- |
| `status` | `ok`, `dry_run` or `error` |
| `exit_code` | the process exit code (below) |
| `error` | `null`, or `{"kind": "sql" \| "auth" \| "network" \| "byte_cap" \| "usage" \| "output" \| "internal", "message": "..."}` |
| `backend` | `bigquery` or `duckdb` |
| `rows`, `total_rows`, `truncated` | rows written to the file, rows the query returned, and whether the file holds fewer (see `--max-rows`) |
| `columns` | number of result columns |
| `bytes_estimated`, `bytes_accuracy` | the dry run's estimate and how far to trust it (next section) |
| `within_cap`, `max_bytes_billed` | whether the estimate is under the byte cap, and the cap |
| `bytes_processed`, `bytes_billed`, `cache_hit`, `job_id` | what BigQuery reported for the job; `null` when its response did not say |
| `output`, `format` | the results file and its format |
| `checked`, `not_checked`, `warnings` | the checks that ran (`read_only`, `static`, `schema_snapshot`, `dry_run` or `duckdb_bind`), what could not be checked and why, and findings that did not stop the run. At most 5 entries each, then a count |

On failure stderr also gets one line, such as `q: sql error (exit 2): Unrecognized name: nope at [1:8]`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success; a `--dry-run` that planned is a success too |
| 1 | other: the results file could not be written, or an unexpected error |
| 2 | SQL error: it does not parse, is not one read-only SELECT, a pre-flight check found an error, or BigQuery (or DuckDB) rejected it |
| 3 | auth: no credentials, credentials refused (HTTP 401), or access denied (`accessDenied`, `billingNotEnabled`, other 403s) |
| 4 | network: BigQuery could not be reached, timed out, or is busy (HTTP 5xx, `rateLimitExceeded`, `quotaExceeded`); the same query may work later |
| 5 | byte cap: the dry-run estimate is over `--max-bytes`, or BigQuery refused the run under `maximumBytesBilled` (`bytesBilledLimitExceeded`) |
| 64 | usage: a bad option, a missing or unreadable SQL file, or no billing project |

The usage code is 64, not argparse's usual 2, because 2 means a SQL error here. `--help` lists the codes too.

## Read-only

KumoSQL only reads. `q run` runs exactly one `SELECT`, with `WITH`, `UNION` and subqueries allowed. A write (`INSERT`, `UPDATE`, `DELETE`, `MERGE`), DDL (`CREATE`, `DROP`), a script (`DECLARE`, several statements) or `EXPORT DATA` is refused with exit 2 before anything reaches BigQuery. Writes are out of scope and there is no flag to allow them. Two checks guard this: the local parser (the same check [data sources and scope queries](ui.md) use), and the dry run's `statementType`, which must be `SELECT` before the job runs. If sqlglot cannot parse the SQL, the dry run's `statementType` decides on its own, and the local checks are listed under `not_checked`.

## Before the job

In order, and each step stops the run on a problem:

1. **Read-only check** (local).
2. **Static checks** (local, [`bq_static_checks`](../src/kumosql/bq_static_checks.py), the rules `check_query` uses on the [agent server](mcp-server.md)). A reserved word used unquoted as an alias (`SELECT id AS new`) and a correlated subquery BigQuery cannot de-correlate stop the run with exit 2 and the fix. The others (`NOT IN` with a NULL, an unreachable `CASE` branch, and so on) are listed under `warnings`, and the query still runs. `--no-preflight` skips these checks if one is wrong about your query; BigQuery's dry run still checks it.
3. **Unknown columns, from a schema snapshot** (local, only with `--schema-snapshot FILE`, the `INFORMATION_SCHEMA.COLUMNS` export described on the [agent server page](mcp-server.md)). An unknown or ambiguous column stops the run before any request. Tables missing from the snapshot, and pseudo-columns such as `_PARTITIONTIME` that a snapshot never lists, are named under `not_checked`.
4. **The dry run** (BigQuery, free, reads no data). It resolves every table and column, so it catches unknown columns without a snapshot too, and it estimates the bytes. On DuckDB the query is bound (`DESCRIBE`) instead, which also catches unknown columns.
5. **The byte cap.** An estimate over the cap is refused with exit 5. The real run passes the cap as `maximumBytesBilled`, so BigQuery refuses it too.

## Dry run and the byte figure

`--dry-run` stops after step 4: no job runs and no file is written. The summary gives `bytes_estimated`, `within_cap` and `bytes_accuracy`, which is BigQuery's own `statistics.query.totalBytesProcessedAccuracy` for the estimate ([Job resource, JobStatistics2](https://cloud.google.com/bigquery/docs/reference/rest/v2/Job)), in these words:

| `bytes_accuracy` | BigQuery's value | Meaning |
| --- | --- | --- |
| `exact` | `PRECISE` | the estimate is precise |
| `upper_bound` | `UPPER_BOUND` | the query will process at most this much |
| `lower_bound` | `LOWER_BOUND` | the query will process at least this much |
| `unknown` | `UNKNOWN`, or not reported | BigQuery did not say how accurate it is |

`q run` reports what BigQuery says and adds no rule of its own about when an estimate is a bound. BigQuery's clustered-table docs say only that the estimate may not be accurate before the query runs. A dry run over the cap still exits 0 and reports `within_cap: false`, because the dry run itself succeeded; the real run would exit 5.

## The results file

The file holds the full result, up to `--max-rows` rows (default 100,000, at most 1,000,000). Rows past that are counted, not fetched, and `truncated` is true. Without `-o`, a new file is written in the temp folder (`<temp>/kumosql-results/q-<time>-<pid>-<hash>.json`) and the summary gives its path. The format is CSV when `-o` ends in `.csv`, else JSON; `--format` overrides it. Files are UTF-8 without a byte-order mark and are replaced atomically.

- **JSON**: one object with `columns` (name and type), `row_count`, `total_rows`, `truncated`, `max_rows`, `backend`, `job_id`, `bytes_processed`, `bytes_billed`, and `rows`, one array per line in column order. Values are typed. INT64 is a number, BOOL is `true` or `false`, TIMESTAMP is ISO 8601 in UTC (`2023-11-14T22:13:20.123456Z`), STRUCT is an object and ARRAY a list. NUMERIC, BIGNUMERIC, DATE, DATETIME, TIME, BYTES (base64) and the rest stay text as BigQuery sends them, so no precision is lost. A non-finite FLOAT64 stays `"NaN"` or `"Infinity"`.
- **CSV**: a header row, `\r\n` line ends, NULL as an empty field, booleans as `true`/`false`, and STRUCT and ARRAY values as JSON text.

## `--summarize`, for agents

`--summarize` adds two keys to the summary: `column_stats`, one entry per column, and `head`, the first `--head` rows (default 5, at most 50). The statistics use the column kinds of [data profiling](data-profiling.md) and are computed over the rows in the file: nulls and distinct values for every column; min, max and mean for numbers; min and max for dates and times; shortest and longest length for strings; and the three most common values of string and boolean columns that repeat. Cell values are cut to 80 characters.

The summary never exceeds `--summary-bytes` (default 4,000; from 1,000 to 20,000). When it would, rows of `head` go first, then the most common values, then whole column entries from the end, then notes. `summary_truncated` turns true and `summary_omitted` counts what was left out. The results file always keeps everything. With `--dry-run`, `column_stats` lists the result's column names and types, which costs nothing.

## Options

| Option | Effect |
| --- | --- |
| `FILE` or `-` | the SQL file, or stdin |
| `-o FILE`, `--format json\|csv` | where the results go, and their format |
| `--dry-run` | plan only: bytes, accuracy and cap; nothing runs |
| `--project ID` | the billing project the job runs in (default: the one chosen in Settings, BigQuery projects; never guessed from the SQL) |
| `--location LOC` | the BigQuery location (default: the one in Settings) |
| `--max-bytes N` | the byte cap (default: Settings, Scopes; 1 GiB). Accepts `10GB`, `1TiB`; from 10 MB to 1 TiB |
| `--max-rows N` | rows written to the file (default 100,000) |
| `--timeout S` | seconds to wait for the job (default 600). A job still running then exits 4 and its id is in the message; it may still finish in BigQuery |
| `--duckdb PATH` | run on a DuckDB file instead (below) |
| `--schema-snapshot FILE` | catch unknown columns locally |
| `--no-preflight` | skip the static checks (the read-only check always runs) |
| `--summarize`, `--head N`, `--summary-bytes N` | column statistics and first rows, under a hard size cap |

Credentials are found the way every KumoSQL BigQuery feature finds them: `BQ_ACCESS_TOKEN`, a service account key (`GOOGLE_APPLICATION_CREDENTIALS_JSON` or `GOOGLE_APPLICATION_CREDENTIALS`), or Application Default Credentials (`gcloud auth application-default login`, with `pip install 'kumosql[bigquery]'`). See [the dry run](cost-and-change-reports.md). `q run` never asks for, prints or stores them, and it silences google-auth's console warnings so that stderr stays empty. Jobs carry the label `kumosql: q_run`.

## Local runs on DuckDB

`--duckdb PATH` runs the same path on a DuckDB database file, opened read-only (`:memory:` for none), so it can be tried and tested with real execution and no network. The SQL is still read as BigQuery SQL: the same read-only and static checks run, and sqlglot translates it to DuckDB SQL. The dry run binds the query instead of estimating bytes, so `bytes_*` are `null`. It needs `pip install 'kumosql[execution]'`. Results can differ from BigQuery's wherever the two engines differ, so treat it as a local copy, not as BigQuery.

## Limits

- **Not run against a real BigQuery project.** The BigQuery path is tested with a fake REST transport that returns the documented response and error shapes ([`tests/test_query_runner.py`](../tests/test_query_runner.py)). The DuckDB path, the shells and the file handling are tested with real execution.
- **Shells.** One command in cmd and Windows PowerShell 5.1 (and in bash and sh elsewhere) is checked to print the same summary as a direct run (a `slow` test, run by `pytest -m slow tests/test_query_runner.py` and the periodic full run). It was also run by hand in Git Bash on Windows. PowerShell 7 was not tested.
- **Windows PowerShell 5.1 and non-ASCII SQL.** PowerShell encodes text piped to a program with `$OutputEncoding`. When that is ASCII (the 5.1 default, unless a profile changes it), `'café'` arrives as `'caf?'`, and no program can undo that. Pass a file, or first run `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`. A file written by `Out-File` (UTF-16 with a byte-order mark) is read correctly.
- **Error classes come from BigQuery's HTTP status and error reason**, following its [error table](https://docs.cloud.google.com/bigquery/docs/errors). A failure to refresh credentials that is really a network outage is reported as auth (exit 3), because the credential library does not say which it was.
- **The static checks only report what is certain**, and only the two rules BigQuery rejects stop the run. A query they pass can still fail in BigQuery; the dry run is the check that counts.
- **Statistics with `--summarize` describe the rows in the file.** With a truncated result they describe the first `--max-rows` rows, and `not_checked` says so.
- **Scripts and writes are out of scope.**
