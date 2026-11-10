# Rewrite rules

[All simple guides](README.md) · [Full reference](../docs/rewrite-rules.md)

A rewrite rule makes one kind of change to SQL. For example, `remove_trivial_predicates` removes `WHERE 1 = 1` because it excludes no rows.

```sh
python -m kumosql rewrite-sql query.sql -r remove_trivial_predicates
```

KumoSQL checks each changed result against its input. Read the evidence label before using the output.

## Choose a rule

| Rule | Plain meaning |
| --- | --- |
| `lift_subqueries` | Give a nested FROM/JOIN query a name in `WITH` |
| `inline_single_use_ctes` | Move a `WITH` query used once into the place that reads it |
| `remove_trivial_predicates` | Remove conditions that add nothing, such as `AND TRUE` |
| `remove_redundant_parentheses` | Remove brackets that do not affect meaning |
| `deduplicate_ctes` | Share identical `WITH` queries |
| `remove_unused_ctes` | Remove `WITH` queries nobody reads |
| `remove_redundant_distinct` | Remove `DISTINCT` when supported grouping already prevents duplicates |
| `qualify_columns` | Write `orders.id` instead of bare `id` when a query reads two or more tables and only one has that column (opt in) |
| `format_sql` | Apply saved SQLFluff formatting preferences |

These rules have exceptions. For example, combining duplicate queries containing random calls can change results. A rule may leave such SQL alone.

## Qualify columns

`qualify_columns` is for queries that join tables. `SELECT id, name FROM orders o JOIN customers c ON o.cid = c.cid` becomes `SELECT o.id, c.name ...`, so a reader sees where each column comes from. It does not run in the default pipeline; ask for it with `-r qualify_columns`. Every qualification is also re-checked by a separate checker that confirms only table names were added and that each one names the only table the column can come from ([how](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs_simple/proof-safeguards.md#qualifying-columns)). The rule leaves a column alone when it is a nickname in `GROUP BY` or `ORDER BY`, or is used before its table is read.

It only adds a table name when it is sure. It leaves a column alone when:

- the query reads one table;
- a table's columns are not known;
- the join is `NATURAL` or the column is the one in `USING (...)`, because the merged column has no single table;
- two tables both have the column, or an output name from `SELECT ... AS` is used in `GROUP BY` or `ORDER BY`.

Limits: it needs to know each table's columns, which come from the loaded project or the saved BigQuery catalog, so on the command line only queries built from `WITH` queries and subqueries are qualified. Without those columns the checker may call the result `unproven`. The final `ORDER BY` keeps its bare names, because the checker only accepts a rewrite that leaves the ordering text alone. See the [full reference](../docs/rewrite-rules.md) for the complete list of cases that are skipped.

## Understand the label

| Label | What to do with it |
| --- | --- |
| `unchanged` | The text is identical to the input; a rule may have skipped it, and its step says why. Input that only parsed in the parser's recovery mode (for example a query cut off after `WHERE 1 =`) is labelled `unproven` instead |
| `proven` | Equivalence was established; read any assumptions |
| `planner_checked` | BigQuery could plan the query, but equal results were not proved |
| `unproven` | Review it; the checker could not establish equivalence |
| `failed` | The rewrite failed; its output is not accepted |

Only `unchanged` and `proven` count as trusted. The CLI exits 3 for untrusted output unless you explicitly use `--allow-unproven`; that option does not add evidence. Trusted does not mean the input was valid SQL in general, but a change to a query BigQuery would plainly reject (a column that its subquery does not have, `HAVING` with no grouping or aggregate, or a type name BigQuery does not have such as `FLOAT` or `VARCHAR`, in a cast or elsewhere a type is written) is never `proven`. The check only catches those cases, so it can miss other invalid SQL. Fatal rule failures exit 2 without writing the result.

## Rule order matters

Supply multiple `-r` options to run rules in that order. Put formatting last: other rules can change the layout again.

`--check-idempotence` checks that rerunning the rules makes no further change. It exits 4 if the result changes again. Avoid combining `lift_subqueries` and `inline_single_use_ctes` when you want this property: one lifts a query and the other puts it back.

For Dataform SQLX, the driver protects config, JavaScript, operation blocks, and `${...}` expressions. Formatting with `format_sql` is for SQL, not SQLX. Every formatting change is re-checked by a separate checker that requires the same words, comments and parse tree, so only whitespace and keyword case may differ; selecting sqlfluff rules that change more than layout makes the result `unproven` ([how](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs_simple/proof-safeguards.md#formatting)). A `${...}` expression comes back exactly as written, even when it holds a backslash such as `r'\d'` or `\1`. Unsupported syntax and parse recovery are reported in diagnostics. See [Dataform preservation](evals/dataform-bench.md).

KumoSQL now requires SQLGlot 30.21.0. Walter approved using this one version so routine tests run once with its faster compiled build, instead of repeating the full suite on four configurations. The normal Python build still works; focused checks can use it when diagnosing a problem. Helpers written during the old version matrix remain: they prevent mistakes such as reading `SELECT * EXCEPT (b)` as a plain `SELECT *`. Older releases are no longer supported. See [Rewrite rules](../docs/rewrite-rules.md) for setup and helper names.
