# BigQuery behaviour eval (GoogleSQL compliance queries, edge cases and BigQuery Utils UDF tests)

[Plain-language version](../../docs_simple/evals/bigquery-behavior-eval.md)

Does a KumoSQL rewrite (inline single-use CTEs, trivial-predicate and parenthesis cleanup, CTE dedupe, unused-CTE removal, redundant DISTINCT, and optionally subquery lifting) keep the query's behaviour on BigQuery-specific syntax and semantics, or does it say so and decline?

```
python tools/bq_behavior_eval.py                      # both corpora, pipeline "semantic"
python tools/bq_behavior_eval.py --pipeline lift      # plus subquery lifting
python tools/bq_behavior_eval.py --corpus edge --failures --details
python -m pytest tests/test_bq_behavior_eval.py       # floors; the full GoogleSQL run is marked slow
```

Needs `duckdb` and `z3-solver` (the `dev` extra); neither is a runtime requirement.

## How a case is scored

The rewrite pipeline runs on the query. If the text only changes layout, or does not change, the case is **declined**. Otherwise both versions are executed in DuckDB (after sqlglot's BigQuery to DuckDB transpile) and the results compared as bags (as lists when there is an `ORDER BY`). DuckDB runs on one thread so the same query gives the same answer every time (with several threads it can build `ARRAY(SELECT .. UNION ALL ..)` in a different order on each run).

| Outcome | Meaning |
|---|---|
| handled | KumoSQL rewrote it, accepted the rewrite as proven, and the results match |
| declined | unchanged, layout only, or the rewrite was not accepted (unproven) |
| unsupported | sqlglot does not parse it (opaque command or parse error) |
| error | KumoSQL crashed |
| **WRONG** | KumoSQL accepted a rewrite whose results differ. This must stay at 0 |
| not executable | DuckDB cannot run the original, so there is no verdict |

Correctness (WRONG), coverage (handled / declined / unsupported / error) and performance (seconds) are reported separately. DuckDB is a differential oracle only: the same engine runs both sides, so an engine difference from BigQuery cannot hide or fake a change, except where the rewrite itself folds a constant. Those folds were checked on real BigQuery (project `kumosql`, zero bytes billed): `1 = 1.0`, `'a' = 'A'`, `9007199254740993 = 9007199254740992.0` (true in BigQuery, which is why KumoSQL only folds INT64 pairs and identical literal text), `NULL AND FALSE`, `NULL OR TRUE`.

## Corpora

- **GoogleSQL compliance queries** (`tests/fixtures/googlesql/queries.json.gz`): the SQL text of every self-contained `SELECT`/`WITH` case without parameters and not expected to error, from `googlesql/compliance/testdata/*.test` in [google/googlesql](https://github.com/google/googlesql) (formerly ZetaSQL), Apache-2.0, 7,870 queries. Only the SQL is used; the expected results are ignored, so nothing is fitted to them (the [expected results have their own eval](googlesql-expected-results.md)). Regenerate with `python tools/bq_behavior_eval.py --googlesql <testdata dir>`.
- **BigQuery edge cases** (`tests/fixtures/bq_edge/cases.json`): 967 queries over two small tables, all executable in DuckDB. By tag: three-valued logic and literal folding in predicates, parentheses and operator precedence (including BigQuery's `^`, `<<`, `||`), CTE inlining and dedupe (nondeterministic CTEs, shadowing, recursion, UNNEST, `SELECT * EXCEPT/REPLACE`), DISTINCT, QUALIFY and window ties, SAFE_ functions, casts and numeric boundaries, UNNEST/ARRAY/STRUCT, PIVOT/UNPIVOT, and 588 seeded fuzz cases (random predicate trees in WHERE/HAVING/ON/QUALIFY/CASE/IF/COUNTIF, random operator trees with redundant parentheses). Cases DuckDB cannot run are dropped.

## What this found and fixed

- **Subquery lifting dropped `PIVOT`, `UNPIVOT` and `TABLESAMPLE`** attached to the lifted subquery (`FROM (SELECT ...) UNPIVOT (...)` became `FROM cte`), changing the result's columns and rows.
- **The SMT prover called that pair, and any query with `PIVOT`/`UNPIVOT` against its source, equivalent.** It now declines any query containing a pivot.
- **`FLOAT` (32-bit in GoogleSQL) and `UUID`** were printed as `FLOAT64` and `STRING` by the BigQuery generator on both sides of the check, so a rewrite passed while changing the type. Such queries are now not accepted.

## Gaps and checks still wanted

- 1,356 GoogleSQL queries do not parse in sqlglot's BigQuery dialect (GoogleSQL-only features: protos, enums, graph queries, `FLOAT32`, newer pipe and table syntax). They are counted as unsupported, not hidden.
- The 81 pipe-syntax (`|>`) queries are left as written. sqlglot parses pipe syntax into nested CTEs, so the rewrites that used to count for 7 of them were of that translation, printed as standard SQL.
- Most GoogleSQL cases are declined because KumoSQL has nothing to rewrite in a bare `SELECT`; the handled count measures rewrites that happened and held.
- Still wanted on real BigQuery (to run on the laptop replica): every rewritten before/after pair from the edge suite (`--failures` lists none; use `evaluate()` for the pairs), especially the CTE-inlining cases with `RAND()`, `GENERATE_UUID()` and `CURRENT_*`, and `SAFE_`/cast cases whose result a DuckDB transpile may not model.

## Scores (2026-10-04)

| Corpus | Pipeline | Cases | Rewritten and identical | Wrong | Declined | Unsupported | Not executable |
|---|---|---:|---:|---:|---:|---:|---:|
| GoogleSQL compliance (googlesql @ d82db99, 7,870 original queries) | semantic | 7,870 | 31 | 0 | 6,674 | 946 | 219 |
| GoogleSQL compliance | lift | 7,870 | 230 | 0 | 6,081 | 946 | 613 |
| Edge cases (967 custom: 379 hand-written, 588 seeded fuzz) | semantic | 967 | 410 | 0 | 556 | 0 | 1 |
| Edge cases | lift | 967 | 417 | 0 | 549 | 0 | 1 |
| Held-out fuzz (662, seeds 101 and 103, not used while fixing) | semantic / lift | 662 | 395 | 0 | 267 | 0 | 0 |

The GoogleSQL lift row fell from 259 to 230 on 2026-10-04: the proof layer now refuses a query that casts to a type name BigQuery does not have (291 of the 7,870 queries use `DOUBLE`, `INT32`, `FLOAT`, `UUID`, `MAP` or `VECTOR`; BigQuery dry runs return "Type not found" or "MAP datatype is not supported"), so the 29 rewrites accepted among them are declined (master measured 259 with 6,052 declined and 613 not executable on the same day; this branch measures 230 with 6,081 declined and the same 613). The semantic row stays at 31 and the edge-case rows are unchanged by it. This removes rewrites whose proof was credited on an input BigQuery rejects, not coverage of valid SQL.

On 2026-10-04 the edge-case numbers fell (lift 435 to 417, semantic 428 to 410, held-out 421 to 395) because every prover now refuses a text that an independent reading groups differently from sqlglot ([parser checks](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/parser-checks.md)). The refused cases are seeded fuzz texts with a non-associative comparison BigQuery rejects or a bitwise chain sqlglot regroups; `wrong` stays 0 and no rewritten result differed. The held-out run was repeated only to update this count.

Unsupported fell from 1,356 to 994 when KumoSQL started reading `GRAPH_TABLE`, pipe `SET`/`DROP` and `ML.` functions with a `MODEL` argument (`bigquery_syntax.py`); those queries now count as declined or not executable instead, and the rewritten count is unchanged. It fell again to 946 when `x LIKE ALL UNNEST(...)`, aggregate `WHERE` filters, the `WITH(a AS 1, a + 1)` expression and `t.arr elem WITH OFFSET off` became readable ([#520](https://github.com/walterogozaly/KumoSQL/issues/520)); again the rewritten count and the 0 wrong are unchanged. Baseline before the fixes above: edge cases 1 wrong with lifting (UNPIVOT), GoogleSQL 3 wrong with lifting (FLOAT and UUID types); no wrong without lifting. Each found failure stays in the corpus as a regression case. A pipeline can score 0 wrong by changing nothing, so the rewritten count is reported beside it. `heldout.json` is for final measurement only: add new bug-hunting cases to `cases.json`.

## BigQuery Utils UDF tests

Does KumoSQL's BigQuery to DuckDB translation (`kumosql.bigquery_duckdb.to_duckdb`, used only by this eval so far) compute the value BigQuery computes? The oracle is independent: [GoogleCloudPlatform/bigquery-utils](https://github.com/GoogleCloudPlatform/bigquery-utils) ships a unit test for each of its UDFs (`generate_udf_test` / `generate_udaf_test` calls in `udfs/community/test_cases.js` and `udfs/migration/*/test_cases.js`, inputs and an expected output), and Google runs them on BigQuery.

```
python tools/bq_utils_udf_eval.py                     # score, with unsupported counted by reason
python tools/bq_utils_udf_eval.py --failures          # wrong development cases (held-out cases stay hidden)
python tools/bq_utils_udf_eval.py --baseline          # sqlglot's translation alone, for comparison
python tools/bq_utils_udf_eval.py --write-results     # benchmarks/results/bigquery-utils-udfs.json
python -m pytest tests/test_bq_utils_udf_eval.py      # floors, 0 wrong, the translation fixes one by one
```

**Source.** bigquery-utils @ d5bde4f (Apache-2.0). The SQL UDF files and the seven `test_cases.js` files are copied unchanged into `tests/fixtures/bigquery_utils_udfs/` with the licence; `manifest.json` holds the commit, the SHA-256 of every copied file, and the name, language and SHA-256 of the 78 JavaScript and Python UDFs that are not copied. `--vendor <clone>` regenerates it. 241 test calls (227 scalar, 14 aggregate) give 807 cases over 210 UDFs, 134 of them SQL. All cases are original; none is adapted. No other eval uses this source.

**How a case is built.** The test's UDF is inlined: its body is parsed, each parameter is replaced by the test input (cast to the declared type unless the parameter is `ANY TYPE`), the body is cast to the `RETURNS` type, and calls to other SQL UDFs of the same folder are inlined the same way. An aggregate UDF runs over the test's `input_rows`, with `NOT AGGREGATE` arguments passed as constants. A parameter name that a `FROM` item inside the body also offers is not substituted (the case is unsupported). The expected output becomes `SELECT <expected_output>`. Both are translated with `to_duckdb` and run in DuckDB (one thread, session time zone UTC as in BigQuery).

**How a case is scored.** Values compare as GoogleSQL values: numbers across INT64, FLOAT64 and NUMERIC with a relative tolerance of 1e-9, STRUCT fields by position and name, JSON as parsed JSON, and a NULL array as an empty one (BigQuery returns a NULL array as `[]`). A difference counts only if DuckDB with its optimizer off agrees. When the query builds an array or string with `ARRAY_AGG`, `STRING_AGG` or `ARRAY(SELECT ...)` without `ORDER BY` and the values match as multisets, the case is not compared (GoogleSQL leaves that order unspecified).

| Outcome | Meaning |
|---|---|
| agree | translated, executed, and equal to BigQuery's expected value |
| **wrong** | translated and executed, but a different value: a translation bug. Must stay 0 |
| unsupported | no verdict: a JavaScript or Python UDF (or a SQL UDF calling one), KumoSQL declines to translate, DuckDB cannot run the translation or the expected output, or the element order is unspecified |

A fifth of the cases (SHA-1 of the case id) is held out: `--failures` never prints them, and none was looked at while fixing the translation.

### Scores (2026-10-04)

| Run | Cases | Supported | Agree | Wrong | Unsupported |
|---|---:|---:|---:|---:|---:|
| Baseline: sqlglot's translation alone | 807 | 337 | 277 | **60** | 470 |
| With KumoSQL's translation fixes | 807 | 300 | 300 | 0 | 507 |
| Held-out fifth (with the fixes) | 143 | 49 | 49 | 0 | 94 |
| Held-out fifth, baseline | 143 | 55 | 45 | 10 | 88 |

Unsupported with the fixes: JavaScript UDF 342, a SQL UDF calling a JavaScript UDF 28, Python UDF 2; DuckDB cannot run the translation 68 (functions DuckDB lacks such as `TO_CODE_POINTS`, `ST_GEOGPOINT`, `TIMESTAMP_BUCKET`; `RANGE<...>` types; BIGNUMERIC values beyond DECIMAL(38); `CAST ... FORMAT`; bytes functions) or the expected output 22; KumoSQL declines 41 (`FORMAT` with `%T`, mostly the `typeof` UDF, or with a computed format); element order unspecified 2; test input not a plain literal 2 (the two `exif` tests take a Dataform template for a Cloud Storage path).

### What this found and fixed

The 50 development cases that differed at baseline were all sqlglot translation bugs; each is fixed or now declined in `src/kumosql/bigquery_duckdb.py`, and every case stays in the corpus as a regression (`BASELINE_WRONG_UDFS` in the test). The 10 held-out cases that differed at baseline were never printed; with the fixes none differs.

- **Array subscripts** `a[OFFSET(i)]` with a non-literal index of unknown type were not shifted to DuckDB's 1-based subscripts (`cw_map_create`, `cw_twograms`, `translate`). Every `OFFSET`/`ORDINAL` subscript is now shifted by KumoSQL; a plain `a[i]` with a non-literal index is declined unless `a` is known to be an array (it may be a JSON subscript).
- **`UNNEST ... WITH OFFSET`** became `WITH ORDINALITY`, which counts from 1 (`cw_find_in_list`, `cw_instr4`, `cw_td_nvp`, `translate`).
- **Operator precedence.** `DIV(a + 1, b * c)` printed as `a + 1 // b * c` (`cw_stringify_interval`), and sqlglot reads `<<`, `>>`, `&`, `^` and `|` as one left-to-right level, so `x & 1 << n` meant `(x & 1) << n` (`getbit`); GoogleSQL binds shifts tightest, then `&`, `^`, `|`. Operands that are operators are now parenthesized and bitwise chains are re-associated. The misreading is sqlglot's BigQuery parser, so the provers read such expressions the same way; only the translation is fixed here.
- **`FORMAT`** stayed `FORMAT`, which is fmt-style (`{}`) in DuckDB and printed the format unchanged (`to_hex`, `convert_numeric_string`, `cw_stringify_interval`). It becomes `printf` when every specifier is one printf shares; `%t`, `%T`, `%'d`, `*` widths and computed formats are declined (inside `ERROR(...)` only the message text changes, so it is kept).
- **`EXTRACT(DAYOFWEEK ...)`** is 1 (Sunday) to 7 in BigQuery and 0 to 6 in DuckDB (`cw_next_day`). `EXTRACT(WEEK ...)` is a Sunday-based week number in BigQuery and the ISO week in DuckDB; it becomes `strftime('%U')` (`%W` for `WEEK(MONDAY)`), other week starts are declined.
- **`REGEXP_EXTRACT`** returns NULL when nothing matches, DuckDB an empty string (`url_parse`); and with a pattern that is not a literal sqlglot could not tell whether it has a capture group and returned the whole match (`cw_url_extract_parameter`). Patterns built from constants are folded, capture groups counted, and other patterns declined.
- **`SPLIT(s, NULL)`** is NULL in BigQuery and `[s]` in DuckDB (`cw_split_part_delimstr_idx`).
- **STRUCT casts** are positional in BigQuery and by field name in DuckDB, where a field whose name does not match silently becomes NULL (`ts_linear_interpolate`). Struct casts are rebuilt field by field (`STRUCT_EXTRACT_AT`, `LIST_TRANSFORM` for arrays of structs).
- `FORMAT('%T')` (`typeof`, 30 cases) and a computed `FORMAT` (`cw_comparable_format_bigint`) are now declined instead of wrong.

The harness itself also had to read Dataform's test files faithfully: JavaScript template literals with their escapes and `+` concatenation, `RETURNS` types next to descriptions that mention "returns", UDF bodies that call other UDFs, and `TIMESTAMP` values fetched without `pytz`.

### Gaps and checks still wanted

- 372 cases are JavaScript or Python UDFs (or call one): there is no SQL to translate.
- DuckDB lacks some GoogleSQL functions and types (code points, geography, `RANGE`, `TIMESTAMP_BUCKET`, BIGNUMERIC); these are counted, not hidden.
- The fixes live in `kumosql.bigquery_duckdb`; the compliance and edge-case runs above, `result_equivalence`, `random_check` and `counterexample` still call sqlglot's translation directly, so they do not yet benefit from them (switching them over would move those evals' scores and is left to its own change).
- The Dataform test passes all inputs of a test group as one `UNION ALL` view, so their types are unified across the group; here each case is run on its own inputs.
