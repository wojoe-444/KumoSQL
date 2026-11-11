# GoogleSQL type inference

[Plain-language version](../../docs_simple/evals/googlesql-types.md)

Does the [type checker](../type-inference.md) give each output column of a BigQuery query the exact type GoogleSQL gives it? The labels come from Google's own compliance tests, which print every query's result as a typed value (`ARRAY<STRUCT<a INT64, b STRING>>[...]`), so the header names each output column and its type. Nothing here runs the query: the checker sees only the SQL and the table schemas the test files create.

```
python tools/googlesql_types_eval.py                    # dev split, KumoSQL's checker
python tools/googlesql_types_eval.py --typer sqlglot    # dev split, sqlglot's annotate_types (the baseline)
python tools/googlesql_types_eval.py --failures 20      # list unknown and wrong columns
python -m pytest tests/test_googlesql_types_eval.py     # the floors, in the test suite
```

## Current score

The only place on this page with the checker's numbers; `benchmarks/results/googlesql-types.json` holds the same figures for the scoreboard page. Update both together, then run `python tools/scoreboard.py`.

<!-- dev-score:begin (copy from `python tools/googlesql_types_eval.py`; keep in step with benchmarks/results/googlesql-types.json) -->

Development split, measured 2026-10-05:

| Columns | Labelled | Exact | Unknown | Wrong |
| --- | ---: | ---: | ---: | ---: |
| BigQuery types only (the headline) | 16,404 | 14,755 | 1,649 | 0 |
| All labelled columns | 18,072 | 15,564 | 2,508 | 0 |

No query crashed the checker.

Held-out split, measured once on 2026-10-05 after the development work was frozen:

| Columns | Labelled | Exact | Unknown | Wrong |
| --- | ---: | ---: | ---: | ---: |
| BigQuery types only (the headline) | 4,647 | 3,299 (71.0%) | 1,348 | 0 |
| All labelled columns | 5,256 | 3,732 | 1,524 | 0 |

The held-out score is lower than the development score (89.9%) because several rules (ALIGN, multiway UNNEST naming, MATCH_RECOGNIZE and differential-privacy rewrites, the least-evidenced operators) were written from development cases only. Zero wrong held-out is the property that matters. Any further change made after looking at held-out results must be recorded as "tuned on test".

<!-- dev-score:end -->

An exact column has the same type text as the label. An unknown column got no type from the checker (or no schema for the query). A wrong column got a different type, or a schema of a different width, and **must stay 0**.

## Source, pin and licence

| | |
| --- | --- |
| Source | `googlesql/compliance/testdata/*.test` in [google/googlesql](https://github.com/google/googlesql) (formerly ZetaSQL), the same pin as the [expected-results eval](googlesql-expected-results.md) and the [behaviour eval](bigquery-behavior-eval.md) |
| Commit | `d82db99` |
| Licence | Apache-2.0 ([LICENSE](../../tests/fixtures/googlesql_types/LICENSE), kept beside the fixtures the way `tests/fixtures/googlesql_results/` keeps its own) |
| Fixtures | `tests/fixtures/googlesql_types/dev.json.gz` and `heldout.json.gz`: each case's SQL and labelled columns, and each file's tables, function names and `CREATE FUNCTION` statements. `python tools/googlesql_types_eval.py --harvest <googlesql checkout>/googlesql/compliance/testdata` rewrites both from a checkout of the pin |
| Original or adapted | all original; no query is rewritten |

## What is a case, and how labels are read

A case is a test in a `.test` file that has a name, a SQL text that starts with `SELECT`, `WITH`, `FROM` or a parenthesis, exactly one expected result, no query parameters, and a result rather than an expected error. Tests that expect an error are not in the eval.

- The header of the printed result gives each column's name and type. ARRAY columns print as `ARRAY<>` in the header and carry their element type on each value, so the eval reads it from the first value that has one; a column with no such value is **unlabelled** and not scored.
- A file's `[prepare_database]` blocks create the tables, typed by those blocks' own printed results, exactly as a BigQuery catalog would give a schema. The query that creates a table is itself a case (its result is the same schema). Value tables (`SELECT AS VALUE`/`AS STRUCT`) and tables loaded from protos are left out of the catalog, so queries over them have unknown tables. Functions a file creates are passed as user-defined, and the checker reads each return type from the file's `CREATE FUNCTION` statement.
- The catalog is **not** marked `complete`, so a table the eval does not create is unknown, never a finding.
- **BigQuery columns** are the labelled ones whose label uses only BigQuery types: not `INT32`, `UINT32`, `UINT64`, `FLOAT32`, `ENUM`, `PROTO`, `UUID`, `MAP`, graph types or measures. The headline is over them, because a type BigQuery lacks is not a type a BigQuery user needs.

## The split

The split is by file, so tables, functions and near-identical queries stay on one side:

```
a file is held out when int(sha256(file stem), 16) % 4 == 0
```

| Split | Cases | Files |
| --- | ---: | ---: |
| Development | 7,878 | 222 |
| Held out | 2,602 | 63 |

(285 compliance files in all.) The rule is in the fixtures (`split_rule`) and in the script (`held_out()`); a test checks that no development case comes from a held-out file.

## Baseline: sqlglot's annotate_types

`--typer sqlglot` runs sqlglot's `qualify` and `annotate_types` over the same cases with the same schemas, mapping sqlglot's type names to GoogleSQL's (`BIGINT` to `INT64`, `TIMESTAMPTZ` to `TIMESTAMP`, ...). A column it leaves `UNKNOWN`, or a query it cannot qualify, is unknown; a query that raises is a crash.

| | Exact | Of | Wrong | Crashed queries |
| --- | ---: | ---: | ---: | ---: |
| First recorded, in the tracking issue | 7,143 | 16,416 BigQuery columns | 846 | 1,314 |
| Rerun on the current dev fixture | 7,147 | 16,404 BigQuery columns | 834 | 1,310 |

The two rows differ slightly; the rerun is what the command prints today, with sqlglot 30.21. The point of the comparison: sqlglot's annotator types fewer than half of the columns exactly, gives a different type for hundreds, and crashes on over a thousand queries, because it guesses where GoogleSQL has a rule. It was not built to be exact, so this is not a criticism of it; it shows why a rule-driven checker is needed to lean on the types.

## How to run

```
python tools/googlesql_types_eval.py                    # dev; prints exact / unknown / WRONG
python tools/googlesql_types_eval.py --json             # the counts as JSON
python tools/googlesql_types_eval.py --only <text>      # cases whose id contains the text
python tools/googlesql_types_eval.py --failures 50      # WRONG first, then crashes, then unknown
python tools/googlesql_types_scan.py --chain            # the unlabelled scan: findings must stay 0
python -m pytest tests/test_googlesql_types_eval.py tests/test_googlesql_types.py
```

The command exits 1 when any column is wrong. `tests/test_googlesql_types_eval.py` holds the floors:

- dev has no wrong column and no crashed query, and 7,878 cases;
- the BigQuery exact count may only go up (`FLOOR_EXACT`; raise it when the score rises, never lower it);
- a lower floor applies when sqlglot cannot parse `BY NAME` / `CORRESPONDING` set-operation modes, which is the case on sqlglot 26.0.0, the oldest the project allows (those queries are unknown there, and still never wrong), and a three-column allowance for the compiled sqlglot;
- the number of held-out cases is pinned at 2,602. The test reads only that count.

The scoreboard row is in `benchmarks/results/googlesql-types.json`. When the dev score moves, edit that file and the table above, then run `python tools/scoreboard.py`; never edit the scoreboard page by hand.

## Held-out discipline

The held-out split exists to tell whether the checker works on SQL it was not built against, so:

1. **Develop on the dev split only.** `--heldout` is the only way to score the held-out file, and nothing else in the repository reads its cases (the test reads its length).
2. **Do not read the held-out cases** to see what kinds of query are there, to explain a miss, or to choose the next rule.
3. **Measure it once the dev work is frozen.** This was done on 2026-10-05 (the result is above and in `benchmarks/results/googlesql-types.json`). That first number is the honest unseen estimate.
4. **A held-out miss counts as dev afterwards.** If a held-out failure is looked at and fixed, say so in `held_out`, as the [schema-change eval](schema-change-bench.md) and the [expected-results eval](googlesql-expected-results.md) do, and call the split *tuned on test* from then on. A wrong answer in the held-out split is reported even if it is fixed.

The held-out fixture is checked in, so the discipline rests on people (and agents) following these rules, not on the file being hidden. The one measurement has been made, so any later look at held-out results makes it *tuned on test*.

## Caveats and limits

- **Agreement with the reference implementation's printed types, not with BigQuery.** Where GoogleSQL and BigQuery differ (the compliance tests exercise features BigQuery lacks), the headline's BigQuery-types-only filter narrows the gap but does not close it.
- **Developed against these files.** The checker is developed against these cases and its misses are read one by one, so the dev score is an optimistic figure until the held-out score is in.
- **The catalog is the test files' own tables.** A query over a table the file did not create is unknown; a real project's catalog is larger, and not marked complete here.
- **Columns, not queries.** A query with five columns counts five times; a query that fails to parse counts all its columns as unknown.
- **Unknown is allowed.** The score rewards exactness without punishing caution, so a checker that answers unknown everywhere would have 0 wrong; the exact count is what must go up. The unlabelled [scan](../type-inference.md#the-scan-tool) is the check on the other side: findings (claims of invalidity) must be 0 on queries that run.
- **Version-dependent.** The score depends on the sqlglot version; see [sqlglot versions](../type-inference.md#sqlglot-versions).
