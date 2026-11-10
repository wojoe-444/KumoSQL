# GoogleSQL reference evaluator

[Plain-language version](../docs_simple/gsql-eval.md)

`kumosql.gsql_eval` runs a BigQuery query on in-memory tables in pure Python and returns the rows BigQuery returns, or declines. It reads the sqlglot BigQuery tree, analyses it once (names resolved, every expression typed) and then executes closures. It exists to be an independent oracle: [the BigQuery-on-DuckDB layer](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/bigquery-on-duckdb.md) translates SQL for another engine, while this package implements GoogleSQL's own rules, so the two can be compared. Its score is on the [GoogleSQL conformance eval](evals/googlesql-conformance.md).

```python
from kumosql.gsql_eval import Database, Table, evaluate, Unsupported
from kumosql.gsql_eval import types as T

db = Database({"t": Table([("a", T.INT64), ("b", T.STRING)], [(1, "x"), (None, "y")])})
result = evaluate("SELECT a + 1 AS a1, UPPER(b) FROM t ORDER BY a1", db, time_zone="UTC")
result.rows           # [(None, 'Y'), (2, 'X')]
result.ordered        # True: ORDER BY makes the order part of the answer
result.deterministic  # False when the answer depends on an undetermined choice (ties under LIMIT, ANY_VALUE, ...)
result.inexact        # True when float arithmetic may differ in the last bits between engines
```

`evaluate(sql_or_tree, database, time_zone, params, mode)` takes SQL text or a parsed tree. `mode` is `bigquery` (the default: recursion limit 500, arrays of arrays are an error) or `googlesql` (the compliance tests' language: recursion limit 10,000, arrays of arrays allowed).

## Three ways to not return rows

| Exception | Meaning |
| --- | --- |
| `Unsupported` | the evaluator does not implement the construct, or cannot be sure how BigQuery reads it. Never a verdict about the query; callers fall back or report unknown |
| `AnalysisError` | BigQuery rejects the query before running it (a type error, an unknown name, a misplaced aggregate); `code` is `invalid_argument` |
| `EvalError` | BigQuery fails while running the query on this data (division by zero, overflow, a bad cast); `code` is `out_of_range`. `SAFE.` calls and `SAFE_CAST` turn it into `NULL` |

The evaluator never approximates. Every handler names the sqlglot arguments it reads (`_only`), so a flag sqlglot sets that the handler ignores raises `Unsupported` instead of being evaluated as if absent.

## What it covers

- **Values.** INT64 (overflow is an error), NUMERIC and BIGNUMERIC (exact decimals, rounding half away from zero), FLOAT64 (NaN, infinities, -0.0), BOOL, STRING, BYTES, DATE, DATETIME, TIME, TIMESTAMP with time zones, INTERVAL (months, days and microseconds kept apart), ARRAY and STRUCT. Types travel with columns, so `TRUE` never equals `1`. Columns of types BigQuery lacks (INT32, UINT64, FLOAT32, PROTO, ENUM) raise `Unsupported`.
- **Queries.** SELECT, FROM, joins (including correlated UNNEST), WHERE, GROUP BY with ROLLUP, CUBE and GROUPING SETS, HAVING, QUALIFY, windows with exact frames, set operations (`ALL`/`DISTINCT`, `BY NAME`, `CORRESPONDING`, `STRICT`), ORDER BY with NULL ordering, LIMIT and OFFSET, non-recursive and recursive CTEs, correlated subqueries, PIVOT and UNPIVOT, UNNEST with offsets, STRUCT and ARRAY constructors and paths.
- **Functions.** Math, strings and bytes (including RE2 regular expressions translated for Python's `re` on the shared subset only), arrays, date, time, timestamp and interval functions, aggregates (exact sums, averages, variance and correlation computed without intermediate overflow), and analytic functions. Anything outside what is implemented raises `Unsupported`.
- **Determinism tracking.** The result says when it depends on an undetermined choice: `LIMIT` over ties, element access on an unordered `ARRAY_AGG`, `ANY_VALUE`, ties under a window `ORDER BY`. Float aggregation sets `inexact`.

## Layout

| Module | Role |
| --- | --- |
| `types.py`, `values.py` | the type model, payloads, parsers, formatters, the cast matrix, comparison and grouping keys |
| `compiler.py`, `expressions.py` | scopes and name resolution, the query pipeline, expression handlers |
| `functions.py` and `fn_math.py`, `fn_string.py`, `fn_array.py`, `fn_time.py`, `fn_misc.py` | scalar functions: `register_node` maps a sqlglot class to a BigQuery name and argument list, `register` implements the call |
| `aggregates.py`, `windows.py` | aggregate and analytic functions |
| `datetimes.py` | calendar arithmetic, intervals, format and parse elements |
| `literals.py`, `text_guards.py` | string-literal escapes; names and arities sqlglot reads differently from BigQuery, refused from the query text |

## Text-level guards

sqlglot reads some names BigQuery does not have (`LEN`, `HEX`, `CHARINDEX`...) as functions it knows, drops extra arguments of a few functions, and drops the `STRICT` keyword of a set operation. `text_guards.py` scans the query text outside strings and comments and raises `Unsupported` for those; `STRICT` is detected on the token stream and implemented. On the text path every BigQuery string escape is decoded once (`literals.py`); on the tree path a literal holding a backslash is refused.

## Differential check against the DuckDB layer

`python tools/gsql_differential.py [--limit N] [--random N --seed S] [--json PATH]` runs every query the evaluator answers (the conformance development cases, plus seeded random expression queries over INT64, FLOAT64, NUMERIC, STRING and DATE) through the [BigQuery-on-DuckDB layer](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/bigquery-on-duckdb.md) on identical typed tables, and shrinks each difference to a small repro. A difference counts only when DuckDB with its optimizer off (`run_unoptimized`) returns the same rows as with it on. The run is deterministic, reads only the development split and takes about 20 s for 6,000 queries.

First run (`--random 3000 --seed 0`, 6,149 queries): 4,715 answered by the evaluator, 2,950 compared, 2,847 agree, 103 differ (28 in value, 75 only in type), 53 distinct repros after shrinking. Findings for the BigQuery-vs-DuckDB guards workstream (#487), each with the evaluator believed right from BigQuery's documentation (not re-checked against live BigQuery):

- `FROM UNNEST(SPLIT(...))` and other macro calls in a table-function argument return the lambda variable's name as text.
- `CAST(FLOAT64 AS INT64)` rounds halves to even in DuckDB; BigQuery rounds them away from zero.
- `ROUND`/`TRUNC` of NUMERIC leaks DuckDB's narrow `DECIMAL` type into `IFNULL`, `COALESCE` and `GREATEST`, changing values.
- `CAST(DATE_ADD(...) AS STRING)` gains `00:00:00`; `%e` and `%c` lose their space padding in `FORMAT_DATE`/`FORMAT_TIMESTAMP`; `BIT_COUNT` of a small literal counts 32 or 8 bits.
- `CAST(FLOAT64 AS NUMERIC)` is imprecise for large magnitudes; decimal literals are `DECIMAL` in DuckDB but FLOAT64 in BigQuery (`0.1 + 0.2` differs); `SIGN` returns an integer; some NUMERIC quotients come back as DOUBLE.

Queries the evaluator rejects are skipped even when DuckDB returns rows; reporting those would show missing run-time guards and is a natural follow-up.

## Limits

- It is as good as the reference results it was checked against: Google's compliance tests, not BigQuery itself. Rules fitted to those results (for example how a grouping key is matched by expression) are exact only where the tests pin them; where they do not, the evaluator declines.
- Some behaviours rest on a single compliance case or on documentation; the conformance page lists the open points.
- It does not replace the DuckDB layer yet. Adding an `engine="googlesql"` option to the executed checks, and making the differential run clean (`tools/gsql_differential.py`), come before any eval's default engine changes.
