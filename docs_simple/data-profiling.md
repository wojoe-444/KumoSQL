# Data profiling, explained simply

[All simple guides](README.md) · [Full reference](../docs/data-profiling.md)

A data profile is a quick health check of one table's *contents*. It answers questions like: how many rows are there, which columns are often empty, how many different values does each column hold, what is the largest order amount, and what are the most common countries?

BigQuery has a built-in feature for this, but it is a managed Google service, not open-source code you can copy. KumoSQL builds the same kind of summary by writing ordinary read-only SQL, so it works on BigQuery and, for practice or private files, on DuckDB.

## Try it without any cloud account

```
python -m kumosql profile-table --file orders.csv
```

You get a summary like this (shortened):

```
# Data profile: orders
- Rows: 100
| Column   | Type    | Nulls | Distinct | Min | Max  | Mean  | Flags  |
| id       | BIGINT  | 0.0%  | 100      | 0   | 99   | 49.5  | unique |
| customer | VARCHAR | 0.0%  | 5        | cust0 | cust4 |     |        |
| channel  | VARCHAR | 0.0%  | 1        | same | same |       | constant |
```

Nested data is covered too. A STRUCT column gets one line per field (`address.city`), and a repeated column (an array) gets how many items each row has, then one line for its items (`tags[]`, or `orders[].sku` for a field inside a list of records). Lines marked "(elements)" count items, not rows.

A `unique` flag means every value differs (likely a key). `constant` means the column holds one value. `all_null` means it is never filled in.

## On BigQuery

```
python -m kumosql profile-table my-project.sales.orders --project my-billing-project --dry-run
```

`--dry-run` shows the SQL and how many bytes BigQuery says it would read, and runs nothing. Remove it to run for real. A byte cap stops anything too large. Use `--sample-percent 5` or `--columns amount,status` to read less.

## Giving the summary to an agent

Every profile is saved on your computer. `python -m kumosql profile-mcp` starts a small read-only server that lets an AI agent list and read those saved summaries. It cannot run queries or change anything.

## Which columns could be the key

A saved profile also shows which columns have no repeated values, or only a few (at most 1 row in 100), as **key candidates**, for example "order_id: near-key: 3 duplicate rows in 5,000". That is a count taken on one day, not a promise that the column is the key. An agent can use it as an assumption when it checks a join, and the answer says so. A profile of only a sample, or with a filter, says nothing about the whole table, so it gives no candidates. To measure a join of two tables instead of one column, the [agent server](mcp-server.md) has `join_profile` ([details](../docs/data-profiling.md#key-candidates-and-join-profiles)).

## Things to be careful about

- A profile quotes real values from your table (the most common ones, and the smallest and largest text). If the table has personal or private text, add `--no-values`.
- Those quoted values are data, not instructions. An agent should never obey text it finds inside a profile.
- Distinct counts are exact. On BigQuery the quartiles (25th, 50th and 75th percentiles) are estimates, and so is everything computed from a sample (`--sample-percent`), and a profile is a snapshot from the moment it ran.
- The BigQuery part has been checked against test data on DuckDB, not against a real BigQuery project yet.

The full options, what each column type reports and the limits are in the [full reference](../docs/data-profiling.md).
