# Knowing what a query returns, without running it

[Simple index](README.md) · [Full reference](../docs/type-inference.md)

Every BigQuery query returns columns, and every column has a type: a number, some text, a date, a list, a record with named fields. Often you need to know those types without paying to run the query. If you rename a column in one model, what type does the next model see? If two `SELECT`s are glued together with `UNION`, what type does the result get?

KumoSQL has a type checker for this. You give it a query and a description of your tables, and it tells you the name and type of each output column.

## An example

Say the `orders` table has an integer `id`, an exact decimal `amount`, a list of text `tags` and a record `buyer` with a `name`. For

```sql
SELECT id, amount * 2 AS twice, buyer.name, ARRAY_LENGTH(tags) AS n FROM orders
```

the checker answers: `id` is an integer, `twice` is an exact decimal, `name` is text, `n` is an integer. The record's field `buyer.name` was found inside the record, and doubling a decimal gives a decimal, as GoogleSQL's rules say. The Python call is in the [full reference](../docs/type-inference.md#quick-start).

## "I don't know" is a real answer

The checker is cautious on purpose. It gives a type only when the language's own rules settle it. When they do not, or when it lacks information (a table it was not told about, a function it does not know, a query that mentions `${ref(...)}` placeholders from a Dataform file), it says *unknown* for that column and carries on. A wrong type would be worse than none, because anything built on it would be built on sand.

Two settings matter:

- Tell the checker when your list of tables is **complete**. Then a table it does not find is reported as a mistake. Without that, a missing table just means "I was not told about it".
- Mistakes it reports (a column that does not exist, a `UNION` with different numbers of columns, a field that is not in a record) are only reported when it is certain. It never says a valid query is broken.

## What it handles

Numbers that mix types (is `1 + 2.5` an integer or a decimal?), literals like `NULL` and `[]`, records and lists, `UNION` in all its variants, the newer pipe syntax (`FROM t |> WHERE ... |> SELECT ...`), your own SQL functions, and a large set of built-in functions. The full reference lists the rules it follows and which of them are easy to get wrong.

## What it does not do

- It does not run queries, read data or call BigQuery.
- It is not a complete validator: a query it does not complain about can still fail in BigQuery.
- Some parts of the language are left unknown on purpose: graph queries, protocol buffers, maps and a few uncommon number types.
- Only the agent server's `check_query` uses it so far: with the column types of your tables it spots comparisons such as text against a number (see [the agent server](mcp-server.md)). It is also a building block for later features such as checking schema changes more precisely.

## How we know it is right

Google publishes compliance tests for GoogleSQL, and each test prints the types of its result. The [type inference eval](evals/googlesql-types.md) compares the checker with those printed types and counts exact answers, unknowns and wrong answers. "Wrong" has to stay at zero. A second check runs the checker over real-world BigQuery projects whose queries are known to run: it must find nothing to complain about.

The evidence has limits. The scores come from Google's test queries, which are not your queries, and the checker is developed against part of them; a held-out part is kept for an honest final measurement. The eval page says where that stands. Scores and the list of supported syntax change often, so they are kept in the [full reference](../docs/type-inference.md) and the [eval](../docs/evals/googlesql-types.md), not repeated here.
