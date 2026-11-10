# Understanding a whole pipeline

[All simple guides](README.md) · [Full reference](../docs/pipeline-analysis.md)

A pipeline is a set of queries connected by the tables they read. For example, `orders` feeds `daily_sales`, which feeds `monthly_sales`. Changing `orders` can affect both summaries.

KumoSQL reads a Dataform project or a folder of SQL files and builds this dependency graph.

```sh
python -m kumosql pipeline-report path/to/project -o report.json
python -m kumosql ui --project path/to/project
```

Replace the path with your project folder. The first command writes a JSON report; the second shows the project in the app. When available, a compiled Dataform graph provides exact compiled SQL and is preferable to unresolved templates.

## Follow a change

To ask about removing a column:

```sh
python -m kumosql pipeline-report path/to/project --assess drop_column --target demo.analytics.daily_sales.total
```

Replace the target with your table and column. A direct reader can be marked `breaks`, a reader further along the chain `indirect`, and a model the analyzer cannot understand `unknown`.

**Lineage** follows where data came from. Table lineage says that `monthly_sales` reads `daily_sales`; column lineage says which input columns contribute to `monthly_sales.total`. A column used only in a filter or join has no value descendants but still changes which rows come out, so use the change assessment for the blast radius. If models depend on each other in a loop, no order exists: the report marks the order as incomplete and lists those models.

**Filters on a column.** Lineage also says which conditions limit the rows that reach a column. Say `table_a.product` is `COALESCE(x, y)` built in a CTE, the CTE is joined to the main query, and the main query has `WHERE week = EXTRACT(WEEK FROM CURRENT_DATE())`. The lineage entry for `product` lists that `WHERE` (written in `main`, reading `weeks.week`), the join condition, and any filter inside the CTE, each with the query it was written in. Each filter also says what it does to the rows: `limits_rows` removes rows, while a condition on the optional side of a `LEFT JOIN` (`matches_only`) only decides which rows pair up. The Explain lineage tab on the Query graph page shows them under each step. Filters are not columns, so they do not appear as sources, and a query with no conditions shows none. If the conditions of a model cannot be worked out, the entry says unknown instead of showing an empty list. See [Pipeline analysis](../docs/pipeline-analysis.md) for every field and for what is not listed.

**Fields inside a STRUCT.** A nested column such as `widget` can hold fields (`widget.asset.id`). When a query reads a field by name, lineage now says which field, not only the whole `widget` column. For example, `SELECT widget.asset.id AS id FROM events` traces `id` to `events.widget.asset.id`, and a change to `widget.metric.a` reaches the readers of that field, of anything inside it and of the whole `widget`, but not a reader of `widget.asset.id`. When the query reads the struct in a way that cannot be followed by name (an array subscript such as `items[OFFSET(0)].price`, a function call, or the whole struct), lineage keeps the whole column instead of guessing a field. The rows a model keeps are still tracked per whole column, so a filter on any field counts as a filter on the struct: this can over-report a change, never miss one. The evidence is the hand-made cases in `tests/test_nested_lineage.py` and the DataHub goldens in [the lineage goldens page](evals/lineage-goldens-bench.md), not a benchmark of real nested warehouses. See the [full reference](../docs/pipeline-analysis.md) for the API.

Statements that change tables also retain their inputs and outputs. A script that inserts into `first` and then `second`
lists both written tables, and a rename connects the old name to the new one. These table connections do not guarantee
that every assigned column can be traced. Nested fields of a stored STRUCT still trace to the containing column.
A partition name such as `events$__UNPARTITIONED__` uses the schema of `events` when it is available.

## A query's own WITH table is not the model

A `WITH` table only exists inside the query that defines it. If model `read_secret` reads the model `t`, and a subquery further along declares its own `WITH t AS (SELECT 1 AS id) ...`, that inner `t` is a different, private table. Reading `t` outside it, or inside a `WITH t AS (SELECT * FROM t)` body, still reads the real model. KumoSQL follows this rule, so the dependency on `t` is kept and `t.secret` is not called unused just because a nested `WITH` reused the name. This tracks name scope only; it does not check that the data matches. The exact rule is in the [full reference](../docs/pipeline-analysis.md).

## Find work already done elsewhere

A table profile describes its sources, attributes, and grain. Grain means what one row represents: one sale, one customer, or one customer per day.

- An **overlap** is a table already providing the same attribute at the same grain.
- A **rollup** is a finer summary that may supply a coarser one. Daily sales might supply monthly sales by summing days.
- Two queries that read `Orders` and `orders` are not duplicates: BigQuery table names are case-sensitive. Column names and aliases are not.
- A **near-duplicate** is similar SQL worth investigating. Similar text alone does not prove interchangeable results.

A column that reads a field inside a nested record (`payload.user.id`) is described by the column and the path together. Two tables count as holding the same thing only when both read the same path of the same source column: `payload.user.id` and `payload.user.name` differ, and so do `payload.user.id` and `meta.user.id`. If a field belongs to a computed value, or to an item of an array the query unnests, the meaning stays unknown with the reason.

Where the grain is not visible in a query, KumoSQL uses a key the project declares for the model (`assertions: { uniqueKey: [...] }`) and says it was declared. A key you declare for a table the project does not define is passed on only to models that keep those columns in their output. A grain worked out from the query always takes precedence over a declared one.

Profiles and match reports include reasons and unknowns. Check whether the match supports a real replacement before changing consumers.

## Why missing schemas matter

`SELECT *` over an external table needs that table's column list. Without it, KumoSQL cannot reliably trace the columns. Saved catalog data can fill the gap. A live lookup in BigQuery can too, but it is off by default so that nothing reaches the network unless you ask: tick the checkbox in Settings, pass `--fetch-schema` to the pipeline report command, or set `KUMOSQL_SCHEMA_FETCH=1`. Without it, those columns simply stay unknown. The lookup covers tables the project reads but does not define, and also sources the project declares without listing their columns. When you pass `--fetch-schema`, the command prints one line saying how many tables it fetched, how many failed and why (no credentials, no access, not found, no project in the name), so a lookup that did nothing is never silent.

A `SELECT *` over such a table inside a `UNION` no longer turns the whole model unknown. Each output column is matched by position, so only the columns the starred branch may fill are marked unknown (with the sources the other branches give), and columns the star cannot reach keep their lineage. For example, in `SELECT a, b + c AS x FROM t UNION ALL SELECT * FROM mystery`, both `a` and `x` can really come from `mystery`, so both are unknown but still list `t`'s columns; in `SELECT a, b + c AS x, d FROM t UNION ALL SELECT 1, 2, * FROM mystery`, only `d` is unknown. When every branch has a star, or the star union sits in a CTE, the model's columns all stay unknown. The evidence is a handful of constructed cases in `tests/test_star_branch_lineage.py`, not a benchmark score. The log says only that a lookup ran and how many tables it answered, never their names. See the [full reference](../docs/pipeline-analysis.md) for the details.

An unresolved Dataform template can also hide a dependency. The report marks analysis gaps; it does not silently treat them as no dependency.

For a refactor already built into two datasets, output-comparison plans progress from row counts to fingerprints to exact row comparisons. When you give the comparison a key column, each key is checked as a set of whole rows, so two rows that share a key and swap values (`(1, 10, 100), (1, 20, 200)` becoming `(1, 10, 200), (1, 20, 100)`) are reported as changed even though every column still holds the same values. Those comparisons describe the data in those builds, while static proofs reason under their stated assumptions. See [cost and change reports](cost-and-change-reports.md) and [whole-pipeline equivalence](evals/pipeline-equivalence.md).
