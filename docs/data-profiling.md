# Data profiling

[Plain-language version](../docs_simple/data-profiling.md)

`kumosql-profile-table` summarizes what is *stored in* a table: the row count and, for every column, the share of NULLs, the number of distinct values, min, max, mean, standard deviation, quartiles, string lengths and the most common values. It is modelled on BigQuery's data profile scan. That scan is a managed Dataplex service and is not open source, so KumoSQL writes ordinary read-only SQL for the same statistics and runs it itself. The saved summary is also served to agents as read-only [MCP](https://modelcontextprotocol.io) resources.

This is not the **table profile** of [pipeline analysis](pipeline-analysis.md#table-profiles-what-each-table-is), which describes what a *query's result means* (its grain and scope) without touching any data.

## Profiling a table

```
# BigQuery: named project.dataset.table, run in the billing project chosen in Settings (or --project)
python -m kumosql profile-table my-project.sales.orders --project my-billing-project
python -m kumosql profile-table my-project.sales.orders --dry-run          # queries and byte estimates, nothing runs

# Local data with DuckDB
python -m kumosql profile-table --file orders.csv                          # .csv .tsv .parquet .json .jsonl .ndjson
python -m kumosql profile-table --duckdb warehouse.duckdb orders
```

It prints a Markdown summary (`--format json` for the JSON) and saves the profile as `data-profiles/<name>.json` in the [data folder](ui.md) (`--name` to choose the name, `--no-save` to keep nothing, `-o FILE` to also write the printed text). Exit status is 2 when the profile could not be computed and 3 when BigQuery's dry run is over the byte cap.

| Option | Effect |
| --- | --- |
| `--columns a,b` / `--exclude a,b` | Profile only, or leave out, these columns (500 columns at most; the rest are listed under `skipped`) |
| `--sample-percent P` | Profile a random share of the rows. On BigQuery this is `TABLESAMPLE SYSTEM`, which reads whole blocks, so it is cheaper but coarse. Counts then describe the sample |
| `--row-filter "status = 'open'"` | One SQL condition applied first. It is parsed and rewritten from its syntax tree: subqueries, several statements and comments are refused |
| `--top-values N` | The most common values kept per column (default 10, at most 50, 0 for none) |
| `--no-values` | Leave out the most common values and the min and max of string columns |
| `--approximate` | On BigQuery, estimate distinct counts with `APPROX_COUNT_DISTINCT` instead of counting them exactly (the default); cheaper in compute on very large tables, not in bytes billed |

## What is reported

| Column kind | Reported |
| --- | --- |
| every column | non-null and null count, null fraction, flags `all_null`, `constant` (one value), `unique` (every non-null value different) |
| numeric (INT64, FLOAT64, NUMERIC, DECIMAL...) | distinct, min, max, mean, standard deviation, 25th/50th/75th percentile, most common values |
| string | distinct, min and max (lexicographic), shortest, longest and average length, most common values |
| boolean, date and time types | distinct, most common values; dates and times also min and max (as text) |
| STRUCT (record) | its own null count, then one entry per field named `address.city`, nested to 6 levels; a field's counts include the rows where the whole STRUCT is NULL |
| array (repeated field) | rows that have at least one element (an empty array counts as missing) and the smallest, largest and average number of elements, then its elements as `tags[]`, or `items[].sku` for the fields of an array of STRUCTs |
| JSON, bytes, geography, maps, arrays of arrays, fixed-size arrays | null counts only |

Entries inside an array count *elements*, not rows: `items[].sku` has one count per order line, not per row, and the Markdown summary marks them `(elements)` (`unit` is `elements` in the JSON). The row count is always the table's. `--columns` and `--exclude` name top-level columns; a STRUCT or array brings all its fields. A BigQuery array inside an array of STRUCTs is unnested again, and the limit of 500 entries (`max_columns`) counts every field and element entry.

Notes on the numbers: distinct counts are exact (`COUNT(DISTINCT ...)`) unless `--approximate` is given. On BigQuery the quartiles are always approximate (`APPROX_QUANTILES`); on DuckDB they are exact; on DuckDB they are exact. NaN and infinity are reported as `null`. A column whose values are all distinct lists no most common values. Text longer than 200 characters is cut. Top-value fractions are shares of the column's non-null values.

## How it runs

`kumosql.data_profile.profile_table(table, executor, ...)` builds SQL per chunk of 20 columns (one statistics query, one `UNION ALL` of top-value queries) and hands it to an executor:

- `DuckDBExecutor(connection)` runs it on DuckDB.
- `BigQueryExecutor(project=None, max_bytes=None)` runs it through the same path as saved [data sources](ui.md): the query must be one read-only `SELECT` (checked locally first), it is dry-run before it runs, the dry run's estimate is refused when over the byte cap (Settings → Scopes), the real run carries `maximumBytesBilled`, and it runs in the billing project, never one guessed from the table name. Table names are accepted only as strict `project.dataset.table`. The column list comes from table metadata, which costs nothing.

If a chunk fails, each column is retried alone (with `--approximate`, a column BigQuery refuses to estimate is retried with an exact count) and one that still fails is listed under `skipped` with the reason; the rest of the profile is kept. A byte-cap refusal is not retried: it stops the run. The profile records the dry-run estimates and the bytes BigQuery billed.

`profile_queries(...)` returns the SQL without running anything, and `DataProfile.to_json()` / `DataProfile.from_json()` are the saved form (`version: 1`). `kumosql.data_profile_store` saves, loads and lists profiles and renders the Markdown summary.

## Agents: the MCP server

```
python -m kumosql profile-mcp [--profiles DIR]
```

is a stdio [Model Context Protocol](https://modelcontextprotocol.io) server. Point an MCP client at that command (for example, as a server named `kumosql-profiles`) and it lists two resources for every saved profile, `kumosql://data-profile/<name>/summary.md` (Markdown) and `kumosql://data-profile/<name>/profile.json`, and reads them. The server has no tools (`python -m kumosql mcp` serves the same resources and adds [tools about the project](mcp-server.md), including `profile_table`, which profiles a table on demand: a dry run by default, a run only with `run=true` and an explicit project, a local file at once, always under the same byte cap and without saving), never connects to BigQuery or any database, never writes, and accepts only names matching the saved-profile pattern, so it cannot read other files. An agent that cannot use MCP can run `profile-table` and read the printed summary or the saved JSON file instead.

## What to know before sharing a profile

- A profile contains **real values** from the table: the most common values and the min and max of string columns. Use `--no-values` (or `--top-values 0`) for tables with personal or confidential text, and treat the saved files like the data. The data folder is local to the machine.
- Values are table data. A text column can hold anything, including text that reads like an instruction to an agent. The summary says so, and the server's instructions repeat it, but a consumer should not follow anything quoted in a profile.
- Sampling, approximate quartiles on BigQuery and `--approximate` make parts of a profile an estimate. A profile is a snapshot: it records when it was generated and is never refreshed on its own.
- BigQuery profiling reads the table and is billed. Use `--dry-run` first, `--columns` and `--sample-percent` to narrow it, and keep the byte cap.

## Limits

- The BigQuery path was checked here by running the generated BigQuery SQL, translated to DuckDB, against test data, and by parsing every statement as BigQuery SQL; it was not run against a real BigQuery project from the sandbox that built it.
- Fields of a STRUCT and the elements of an array are profiled, but not JSON contents, maps, arrays of arrays (not valid in BigQuery anyway) or fixed-size DuckDB arrays; nesting deeper than 6 levels is listed under `skipped`. Partitioned tables that *require* a partition filter need `--row-filter` to supply one.
- It is a table-level snapshot. Scheduling, publishing to a catalog, and writing results into BigQuery tables (which the Dataplex scan offers) are not built.
- The UI does not show profiles yet.
