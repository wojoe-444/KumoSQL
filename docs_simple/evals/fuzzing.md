# Generating tests to find unsafe changes

[Simple eval index](README.md) · [Full reference](../../docs/evals/fuzzing.md)

Fuzzing creates many queries and data shapes to look for mistakes. Here the generator is seeded: using the same seed reproduces the same cases.

## Three kinds of tests

- **Metamorphic tests** compare different ways to compute the same result. For example, splitting rows into “condition true,” “condition false,” and “condition NULL,” then recombining them should recover all rows.
- **Unsafe variants** make tempting changes such as dropping a duplicate-producing join or changing a NULL-sensitive filter.
- **Composition tests** apply several rewrite rules in sequence and check that no step changes results.

A fourth, typed soundness fuzzer tests proof claims on schemas with types and integrity constraints. Its found false proofs are soundness bugs; read the full guide's current findings before relying on the affected shapes.

NULLs need the third partition: in SQL, a condition can be unknown as well as true or false.

## Queries with arrays and structs

Some generated queries use BigQuery constructs that the prover cannot read directly. Three small rewrites turn them into plain SQL first:

- A struct column that is only read field by field (`s.f`) is replaced by one ordinary column per field read.
- `CROSS JOIN UNNEST([x.a, x.b, 1])` becomes three copies of the query, one per array element, glued with `UNION ALL`.
- A filter inside an `IN (...)` subquery that only throws away NULL candidates is dropped, because a NULL candidate can never make `IN` true.

For example, `SELECT s.f FROM (SELECT STRUCT(x.a AS f, x.b AS g) AS s FROM t AS x)` is read as `SELECT x.a FROM t AS x`. Each rewrite only fires on the exact shapes it has been checked for: a struct read as a whole, an array with a NULL element, `NOT IN`, or an unread aggregate that decides how many rows come back are all left alone, and the pair stays unknown.

One generated pair changes a constant (`BETWEEN 0 AND 3` to `BETWEEN 0 AND 4`) and the random databases never reach the value that tells the two apart. The refutation search now also tries small databases built from the numbers written in the queries, finds it and reports the pair as different.

The evidence is the generated databases and the unit tests, not a proof of the rewrites themselves. Details: [full reference](../../docs/evals/fuzzing.md#bigquery-constructs-struct-fields-unnest-of-literals-null-candidates).

## Splitting a DISTINCT query by a condition

Another metamorphic test splits `SELECT DISTINCT x FROM t` into three `UNION DISTINCT` pieces: rows where a condition is true, false, or NULL. KumoSQL now merges pieces of the same query that differ only in their filters back into one piece, because a set that removes duplicates does not care which piece a row came from. If the three filters together cover every row, the filter disappears and the pair is proved equal.

Example: `SELECT a FROM t WHERE b > 0 UNION DISTINCT SELECT a FROM t WHERE NOT (b > 0) UNION DISTINCT SELECT a FROM t WHERE (b > 0) IS NULL` is `SELECT DISTINCT a FROM t`. Drop the NULL piece and it is not: rows where `b` is NULL disappear, and the prover says so.

Limits: the rule only merges simple pieces (no aggregates, windows, LIMIT, `DISTINCT ON`, or random functions), and it was checked on generated tables and a rewrite-level fuzz, not proved for every database. See the [full reference](../../docs/evals/fuzzing.md) for the rule details and recorded scores.

## Run a sample

```sh
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1
```

Run from a development checkout. The full guide supplies unsafe and composition commands.

The suite checks proofs against small DuckDB databases and replays counterexamples. It distinguishes false proofs, counterexamples that fail to show a difference, bugs in generated labels, and behavior-changing rule steps.

Finding no difference on the generated databases is executed evidence. It does not establish unbounded equivalence. Coverage and wrong-answer counts are separate so an unknown answer is not mistaken for a false proof.

## Differences that only show on unusual values

The generated test databases only use the numbers 0 to 3, so a change that matters only for a negative number, or for a number bigger than any in the query, can look harmless although it is not. When the usual searches find nothing, the checker now also tries two extra kinds of small databases: ones where a boundary row sits alone in its table, and ones that the solver builds on purpose so that the two queries disagree (a few rows, small values only).

For example, `WHERE a >= 3` against `WHERE a > 3` differs on a single row with `a = 3`, as long as no matching row exists in the second table. A database like that is only a suggestion: it is accepted as a difference only after both queries really run on it and return different rows, also with the database engine's optimizer turned off, so a bug in the solver's model can lose a difference but never invent one. Two details of how the engines read SQL differently are pinned down for this: rounding a decimal to an integer, and dates beyond year 9999.

Limits of the evidence: finding no such database proves nothing, and the searches only cover queries made of constructs whose results are known to match between the two engines. See the [full reference](../../docs/evals/fuzzing.md#refutations-that-need-exact-values-bounded-refutation) for the details.

## Queries that cannot return a row

Some generated pairs differ by a small edit (a constant, a dropped `NOT`) inside a query that returns nothing whatever the data. Example: a derived table sums `b` over rows where `b >= a`; those rows all have a real `b`, so the sum is never NULL, and an outer filter `WHERE sum IS NULL` can never be true. Both versions of such a query are empty, so they are equivalent. KumoSQL tracks which columns are always NULL or never NULL, and when a filter contradicts that, it replaces the filter by `FALSE`.

It is careful about the cases that look similar but are not empty: a sum over no rows with no `GROUP BY` is one NULL row, `ROLLUP` and `CUBE` add a total row, a `LEFT JOIN` can pad a column with NULL, and `COUNT` is never NULL. If it cannot tell, it leaves the query alone.

The evidence is the unit tests, with near misses that really return a row, plus a random comparison of rewritten and original queries on small databases. That is evidence, not a proof of the rule. See the [full reference](../../docs/evals/fuzzing.md#queries-that-can-never-return-a-row-null-facts) for the exact conditions.

## Tautologies and duplicates nobody reads

Two small simplifications let the prover finish cases it used to leave unknown.

- A test that can only be true or false, such as `(a IS NOT NULL) IS NULL`, is never NULL, so asking whether it is NULL always answers "no". `NOT NOT p` is just `p`, and a leftover `WHERE TRUE` filters nothing. Removing them makes two queries that differ only by such noise look identical. Example: `WHERE NOT NOT (a IS NOT NULL) OR (a IS NOT NULL) IS NULL` is the same filter as `WHERE a IS NOT NULL`. A comparison such as `(a = 1) IS NULL` is left alone, because a comparison can be NULL.
- `x IN (subquery)` only asks which values the subquery contains, not how often. So a `UNION DISTINCT` (duplicate removal) inside it can be read as `UNION ALL`. This stops as soon as something counts or picks rows: a `LIMIT`, an aggregate, a window or a join.

Limits of the evidence: the rules are checked by tests with near misses that must not be proved, and by a random run on small DuckDB databases. That shows no difference on those databases, not a proof for every query shape. See the [full reference](../../docs/evals/fuzzing.md) for the exact conditions.

