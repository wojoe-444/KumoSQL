# Checking a Malloy model

`python -m kumosql check-malloy PATH --project DIR` answers "can I trust the numbers this Malloy model gives?". Malloy's aggregates stay correct across joins only if every source's `primary_key` really identifies one row of its table and every `join_one` really matches at most one row. Get either wrong and sums and counts come out wrong without any error. The check reads the `.malloy` files ([`kumosql.malloy_check`](../src/kumosql/malloy_check.py), on the shared [`kumosql.malloy_reader`](../src/kumosql/malloy_reader.py)), and confronts them with what KumoSQL already holds about the tables: the project's grain, saved profiles and the schema snapshot. It never queries the warehouse.

The same check is the `check_malloy` tool of the [agent server](mcp-server.md).

## An example

The project declares `wh.orders` with `uniqueKey: ["order_id", "line_no"]` and builds `daily` with `GROUP BY customer_id, order_day`. A model says:

```
source: orders is bigquery.table('wh.orders') extend { primary_key: order_id }
source: daily is bigquery.table('ds.daily') extend {
  primary_key: customer_id
  measure: avg_discount is discount_rate.avg()
}
source: customers is bigquery.table('wh.customers')
source: sales is daily extend { join_one: customers with customer_id }
```

```
python -m kumosql check-malloy semantic/ --project . --schema-snapshot columns.csv
```

```
check-malloy: 4 contradicted, 0 unconfirmed, 3 confirmed (4 sources in 1 file)
evidence: project ., schema snapshot, 0 saved key measurements
CONTRADICTED join_without_key  shop.malloy:7  sales: join_one customers with customer_id
             contradicted by the model text: customers (shop.malloy:6) has no primary_key, which `with` needs
CONTRADICTED primary_key       shop.malloy:1  orders: primary_key order_id of wh.orders
             contradicted by declared uniqueKey in the declaration of orders (order_id, line_no): one row per (order_id, line_no), so order_id repeats wherever one order_id has several line_no
CONTRADICTED primary_key       shop.malloy:3  daily: primary_key customer_id of ds.daily
             contradicted by the query of model daily, whose grain is (customer_id, order_day): one row per (customer_id, order_day), so customer_id repeats wherever one customer_id has several order_day
CONTRADICTED ratio_measure     shop.malloy:4  daily: measure avg_discount averages discount_rate
             contradicted by the lineage of model daily: discount_rate is agg:SUM(col:wh.orders.discount) / agg:SUM(col:wh.orders.amount): each row's ratio is already an aggregate, and avg() weighs every row the same, so it is not the ratio over the rows it covers
CONFIRMED    missing_column    shop.malloy:1  orders: 1 column read from wh.orders
             confirmed by the schema snapshot
...
```

(Output shortened: each finding also has a `next:` line with the fix or the command to run, and two more columns findings are confirmed.) The exit code is 1 because something is contradicted. `--json` prints the same report as one JSON object (`checked`, `evidence`, `counts`, `findings`, `unknown`).

## How sure each finding is

Every finding has a `status`, and its `why` starts with the same words:

| Status | Meaning | Exit code |
| --- | --- | --- |
| `contradicted by <evidence>` | The evidence shows the model is wrong here. A structural problem is contradicted by the model text itself. | 1 |
| `unconfirmed: ...` | Nothing KumoSQL holds decides it. `next` names the opt-in command that would measure it: [`key-check`](reconcile.md#checking-a-key-key-check) answers at once, and `profile-table` (or `join_profile` on the agent server, for a key of several columns) saves a measurement the next run reads. Unconfirmed is not a pass. | 0 |
| `confirmed by <evidence>` | The evidence agrees. | 0 |

A key is never called wrong without evidence. Exit code 2 means the input could not be read (no such path, not a `.malloy` file, a project that does not load).

## What is checked

**From the files alone.** Each of these was confirmed against the Malloy compiler (`@malloydata/malloy` 0.0.435), which rejects every case:

| Check | What |
| --- | --- |
| `join_without_key` | `join_one: x with k` where `x` has no `primary_key` (a table joined inline has none) |
| `join_many_with` | `join_many: x with k` (`with` matches one row by the primary key, so a `join_many` cannot use it) |
| `join_cycle` | sources that join or extend each other in a cycle, a source joining or extending itself included |
| `duplicate_name` | a source defined twice in a file or also imported; a dimension, measure, view or join name used twice in one source; a field that redefines one of the source it extends (unless `except:` removes it first); a field with the name of one of its table's columns (needs a column list) |
| `undefined_source` | a source extended, joined or run that is defined nowhere, or only further down the file (Malloy reads a file from the top) |
| `undefined_field` | `aggregate: x` where `x` is not a measure, `nest: x` or `view: v is x + {...}` where `x` is not a view, `run: s -> x` where `s` has no view `x` |

**Against the evidence.**

| Check | Evidence used |
| --- | --- |
| `primary_key` | the table's grain from `table_profile` (derived from a model's query, or declared by its `uniqueKey`); the grain the project states for a table it does not build (a declaration's `assertions.uniqueKey`, a duplicate-key assertion, `--docs`, `--assume-grain`; see [where a grain comes from](mcp-server.md#where-a-grain-comes-from)); saved data profiles and saved `join_profile` runs ([key measurements](mcp-server.md#join_profile-and-key-measurements)) |
| `join_fan_out` | the same evidence, for the joined table and the joined columns: the joined source's `primary_key` for `with`, the joined side of each equality for `on` |
| `missing_column` | the schema snapshot (`--schema-snapshot`), the output columns of a model, the columns `--source-schema` declares for a source |
| `ratio_measure` | the column's lineage (a division, and whether both sides are aggregates), a `scale` declared with `--column-attributes`, a Malloy dimension defined with `/`, or the name (`_pct`, `_rate`, `_ratio`, `share`) |

How the evidence decides a key: a key equal to the grain, or containing it, is confirmed. A key that is part of a longer grain (`order_id` against `(order_id, line_no)`) is contradicted, with the reason in words. A key of other columns than the grain is unconfirmed. A saved measurement with repeats on exactly these columns contradicts; no repeats confirms (a subset with no repeats counts too); a measurement of a longer key says nothing about a shorter one. When the grain allows repeats but a measurement found none, neither decides: the key is unconfirmed and the answer gives both. A measurement is a count on one day, and the answer gives its date.

A Malloy `primary_key` is itself a stated grain elsewhere in KumoSQL (`--malloy DIR` on the agent server), but it is never evidence for itself here: the check leaves out every Malloy claim.

Ratios: `sum()` of a column the project shows is a ratio is contradicted (a sum of ratios is not a ratio). `avg()` of a ratio of aggregates (such as `SUM(discount) / SUM(amount)` per row of a daily table) is contradicted, because it weighs every row the same. `avg()` of a per-row ratio, a declared scale or a ratio known only by its name is unconfirmed: an unweighted mean is right only if every row should count the same. The ratio re-aggregation detector (`kumosql.ratio_check`) is being built separately; [`ratio_verdict`](../src/kumosql/malloy_check.py) is the hook that uses its verdict when it exists.

## Options

```
python -m kumosql check-malloy PATH [--project DIR] [--source-schema FILE] [--schema-snapshot FILE] [--profiles DIR]
                                    [--assume-grain TABLE=COL[,COL]]... [--docs DIR]... [--column-attributes FILE] [--json]
```

`PATH` is a `.malloy` file or a folder (every `.malloy` file under it, at most 200 files of 1 MB). `import "other.malloy"` is followed when the imported file is inside that folder (or, for a single file, next to it). `--project` is a Dataform folder, a folder of `.sql` files or a compiled graph, as for `pipeline-report`. `--profiles` is the folder of saved data profiles (default: `data-profiles` in the KumoSQL data folder); saved join profiles are read from its `join-profiles` folder.

On the agent server, `check_malloy` checks the `--malloy` folders it was started with, or `path=` (a file or folder inside the project or a `--malloy` folder), or `text=` (Malloy source; imports are not followed). `status=contradicted` lists only one kind. The answer stays within the usual size bound and lists the findings contradicted first.

## Limits

- The reader is an approximation, not the Malloy compiler. It reads sources, fields, joins, views and `run:` heads; what it cannot read (a source built from `sql(...)` or a query, unbalanced braces) is listed under `unknown`, and that source's key and columns are not checked. A model that compiles was checked to produce no contradicted finding, but the reader may still miss a problem the compiler would catch.
- Column checks see top-level columns only. A nested field (`items.sku` on a record column) is checked as the column `items`.
- A source with a `where:` keeps only some rows, so whole-table repeats do not contradict its key; it stays unconfirmed.
- The agent server's schema snapshot holds only the tables of the project, so a warehouse table the project does not declare has no column list there; the CLI reads the whole snapshot.
- A declared grain is a claim somebody wrote, and a measurement is a count on one day; neither is a constraint the warehouse enforces. `key-check` prints its answer and saves none, so the check cannot read it; only saved data profiles and saved join profiles count as measurements.
- Malloy notebooks (`.malloynb`), parameters of sources and query results are not read.
