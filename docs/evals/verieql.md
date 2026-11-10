# VeriEQL benchmarks (no LLM at evaluation time)

[Plain-language version](../../docs_simple/evals/verieql.md)

`tools/verieql_bench.py` scores KumoSQL on the three suites of [VeriEQL](https://github.com/VeriEQL/VeriEQL) ("VeriEQL: Bounded Equivalence Verification for Complex SQL Queries with Integrity Constraints", OOPSLA 2024): **LeetCode** (about 24,000 pairs of real solutions to LeetCode SQL problems), **Literature** (64 hard pairs from earlier equivalence research) and **Calcite-397** (optimizer rewrites with schemas and integrity constraints). Each case is two queries plus a schema with constraints (primary and foreign keys, NOT NULL, `CHECK`-style predicates, consecutive ids, cross-table implications). Unlike SQLSolver's suites, the pairs include inequivalent ones, and the files carry no labels.

Everything is deterministic Python: `kumosql.counterexample` (a constraint-respecting database generator that runs both queries on DuckDB) and the algebraic z3 prover (`kumosql.algebraic_equivalence`, see [sqlsolver.md](sqlsolver.md)). No model is called and no gold label is read.

## Verdicts and evidence levels

| Verdict | Evidence | Meaning |
| --- | --- | --- |
| `equivalent` | **unbounded proof** | The z3 prover proved the result bags equal on every database that satisfies the declared keys and NOT NULL columns. After a proof, 1,000 more random databases (a different seed) must still agree, or the case counts as `wrong`. |
| `different` | **executed counterexample** | A database satisfying every constraint was built and both queries were run on it with different result bags. The difference has to survive three row-order shuffles, so it never rests on `LIMIT` ties or on MySQL's arbitrary pick for a non-grouped column, and both queries have to return the same rows with DuckDB's optimizer switched off (`PRAGMA disable_optimizer`), because the optimizer has returned wrong rows for some correlated subqueries. Re-checking the full LeetCode run this way dropped 3 of 5,519 refutations; Calcite and Literature did not change. |
| `agrees` | **executed datasets only** | Not proven, and 600 random databases (200, then 400 more for pairs that are not proven; 400 wider ones when a query has `HAVING`) showed no difference. This is not a proof and not bounded verification. |
| `unknown` | none | The pair could not be run (a query DuckDB rejects, unreadable constraints). |
| `wrong` | | An `equivalent` verdict contradicted by a counterexample (our own second search, or VeriEQL's published counterexample replayed on DuckDB). Must stay 0. |

VeriEQL itself is *bounded* model checking: "verified" there means no counterexample exists up to a bound on table size, so a VeriEQL pass is weaker than an unbounded proof. The scoreboard keeps the levels apart: the **proof** rows count `equivalent`, the **executed** rows count `different` plus `agrees`. KumoSQL's own bounded checker ([bounded-verification.md](bounded-verification.md)) is run on all three suites and scored in separate **bounded** rows.

## Counterexample generator

`kumosql.counterexample.find_counterexample(spec, left, right)` returns a `Counterexample` (the rows of every table plus both result bags, and `.script(spec)` for a runnable `CREATE`/`INSERT` script). Databases are small (up to 5 rows per table); values come from small domains seeded with the literals of the two queries (the constant, one below, one above, string and date literals), a "hot subset" per column makes ties and join matches common, and NULLs appear on nullable columns. When a query filters groups (`HAVING`) and nothing else settled the pair, a wider search follows: tables of 3 to 8 rows, plus n to n + 2 rows for each `COUNT(..) > n`-style test up to 12; some columns drawn from their whole domain, so one group can hold many distinct values; and half of each integer literal, so two values can sum to a `SUM(..) >= n` threshold exactly. Proofs of such pairs are re-checked on 200 of these wider databases too. Constraints are respected by construction (keys, foreign keys by drawing from parent rows, consecutive ids) or by rejection (`CHECK`-style predicates and implications; a NULL never satisfies one, which is the strict reading and therefore valid under both).

MySQL lets a grouped query read ungrouped columns; DuckDB refuses. Such columns are wrapped in `ANY_VALUE`, and any difference found must be stable under shuffling the input rows, so only functionally determined columns can produce a counterexample. Keys inside `GROUPING SETS`, `ROLLUP` and `CUBE` count as grouped, and columns inside an aggregate's `FILTER (WHERE ...)` are left alone.

Shuffling cannot move a pick that follows DuckDB's hash order (an `ANY_VALUE` over a join of groups, a `LIMIT` over groups), so a candidate counterexample gets two more checks on the database itself (`Searcher._stable`). Every `ANY_VALUE`, `first`, `last` and `arbitrary` call runs in a guarded copy, `CASE WHEN COUNT(DISTINCT x) <= 1 AND (COUNT(x) = 0 OR COUNT(x) = COUNT(*)) THEN ANY_VALUE(x) ELSE error(..) END`, which fails when a group holds several values (or a value and a NULL), and a pair with such a pick, or with one used as a window function or with its own `ORDER BY`, is never called different. A query with a top-level `LIMIT` (ordered or not) is rerun with every output column appended to its `ORDER BY`, ascending and then descending, and both runs must return the query's own bag. A pick over groups that hold one value each (columns functionally determined by the grouping) still counts. A `LIMIT` inside a subquery is not covered.

A table that a foreign key points at gets rows even when neither query reads it, so a query over a child table alone (EMP, whose DEPTNO references DEPT) is searched with a non-empty child. A database on which a query raises an error (a failed cast, `SINGLE_VALUE` over two rows) is skipped, and the search goes on with the next one.

Most of a run is DuckDB executing these small databases, so the search keeps that cheap without changing which databases it tries: its connection comes from `kumosql.duckdb_load.small_database` (one thread, since with a few rows per table more threads only add scheduling work; `tests/conftest.py` already forces one thread in tests), a load rewrites only the tables whose rows changed, and a database on which both queries already ran and agreed is not run again, since it would agree again. The three samples in `tests/test_verieql_benchmarks.py` took 420 s of CPU on master and 342 s with this (LeetCode 310 s to 244 s, Calcite 62 s to 55 s, Literature 48 s to 44 s, measured with both sides running at once on a 4-core container, one DuckDB thread on both). On Literature, Calcite-397 and every 20th LeetCode pair (1,661 verdicts) the verdicts match master's except pair 19080 of LeetCode, which master refuted and this search does not. That pair is `SELECT U.NAME, SUM(...) ... GROUP BY T.ACCOUNT` over three users sharing an account. It does differ in row count, but the left query's `U.NAME` is an arbitrary pick, and which pick DuckDB makes on a given database depends on how the table was filled (statistics left by earlier loads). Master found a database on which the pick stayed the same through every reload; loading that database fresh gives a different pick and the stability check rejects it, so master's refutation was luck of the table history, and here the pair counts as agreeing. The score rows below were not re-run for this change; a run over all of LeetCode could lose a few refutations of this kind.

Every difference is run a second time with DuckDB's optimizer turned off (`kumosql.duckdb_load.run_unoptimized`) and counts only when both runs return the same rows. DuckDB 1.5's optimizer returns wrong rows for some correlated subqueries (for example `EXISTS (SELECT 1 FROM u WHERE u.d <> t.a AND t.b > u.c)` when the tables hold NULLs), which would otherwise refute an equivalent pair or fail a correct proof. The same recheck guards `sqlsolver_bench.differ` (SQLSolver, QED, R-Bot, Calcite-mined, Cosette, SPES), the Singh & Bedathur search and `kumosql.random_check`.

## Harness translation

The Calcite-397 queries are printed by Calcite, and some of its spellings mean nothing to DuckDB or to the prover. Each translation below has one reading; anything else is left as written, so the pair stays `unknown` instead of being scored under a guess.

| Calcite spelling | Run as | Where |
| --- | --- | --- |
| `$f0`, `EXPR$1`, `$cor0` | `_S_f0`, `EXPR_S_1`, `_S_cor0` (DuckDB rejects `$` in bare names) | `counterexample.to_duckdb` |
| `COUNT(a, b)` | `COUNT(CASE WHEN a IS NOT NULL AND b IS NOT NULL THEN 1 END)` | `to_duckdb` |
| `FIRST_VALUE(x)` / `LAST_VALUE(x)` as an aggregate | DuckDB `first(x)` / `last(x)` (differences that depend on row order are dropped) | `to_duckdb` |
| `SINGLE_VALUE(x)` | `x` of the only row, NULL with none, an error (database skipped) with two | `to_duckdb` |
| `SELECT FROM t` (no columns) | one constant column, excluded again from an outer `*` | `to_duckdb` |
| `ORDER BY NULL` | dropped | `to_duckdb` |
| `CAST(x AS TIMESTAMP(0))` (MySQL's zoneless TIMESTAMP, which sqlglot reads as TIMESTAMPTZ) | DuckDB `TIMESTAMP(0)`: DuckDB returns TIMESTAMPTZ values to Python only through `pytz`, so with TIMESTAMPTZ every non-empty database was skipped and pairs 43 and 256 read `agrees` though their column orders differ | `to_duckdb` (and the replay of VeriEQL's counterexamples) |
| `$cor0.$f0`, where `$f0` is a column of the LATERAL subquery, not of `$cor0` | that subquery's alias, when exactly one source has the column | `tools/bench_sql_repairs.py` |
| `SELECT *` over a join with a repeated column, inside a derived table | the columns spelled out, later copies named `SAL_1` as DuckDB names them (`t.SAL` is the first copy, as in Calcite) | `bench_sql_repairs.py` |
| `a \|\| b` (concatenation; MySQL reads `\|\|` as OR) | `CONCAT(a, b)`, NULL when either is NULL | `bench_sql_repairs.py` |

Literature pairs call uninterpreted predicates on whole rows (`B1(X)` where `X` names a FROM item). The harness spells each out over the row's columns (`B1(X.a, X.b)`): the prover reads it as an uninterpreted function, and DuckDB runs it as a macro with one fixed, arbitrary interpretation (a hash of the arguments). A difference under that interpretation refutes the pair, since an equivalent pair must agree under every interpretation.

The prover gets the schema's foreign keys on a second attempt, after a first attempt with keys and NOT NULL columns alone (the foreign-key rules can rewrite one side out of the shape the other side's proof needs).

Left unknown on purpose: 5 pairs whose Calcite text lost a correlated column (`WHERE * = t5.DEPTNO`), 2 with a subquery in an outer join's `ON` (DuckDB cannot run it), 1 comparing VARCHAR with INT (MySQL coerces, DuckDB refuses), 1 that names a renamed lateral column (`$cor0.SAL0`), and 2 malformed Literature pairs.

## Running

```
python tools/verieql_bench.py literature
python tools/verieql_bench.py calcite --jobs 4
python tools/verieql_bench.py leetcode --every 24 --jobs 4 --audit   # a 1,000-case stratified sample
python tools/verieql_bench.py leetcode --jobs 4 --audit              # all cases (about 2.5 hours on 4 cores)
```

`--audit` also compares with VeriEQL's published per-case outcomes (`NEQ` with a counterexample, `TMO` timeout and so on) and replays VeriEQL's counterexample against every pair we called equivalent. The suites are downloaded once into `~/.cache/kumosql/verieql` (set `KUMOSQL_VERIEQL_CACHE` to change it). The data is CC BY-NC-SA 4.0, so it is not copied into this repository, and none of VeriEQL's code is vendored (its licence is also CC BY-NC-SA 4.0); only its benchmark files are read. Please cite the VeriEQL paper when using these numbers.

`tests/test_verieql_benchmarks.py` pins floors on small samples (skipped, with a "data unavailable" message, when the data cannot be downloaded; downloads are retried, checked against pinned SHA-256 sums and written atomically).

## Pairs known not to be equivalent

The suites carry no labels, but four pairs are known to differ and `tests/test_verieql_benchmarks.py` runs the prover on them directly (so a counterexample the harness finds first cannot hide a proof): Calcite-397 positions 12 and 231 (0-based; indexes 13 and 232 in the artifact), which are `testAggregateCaseToFilter` ([CALCITE-5578](https://issues.apache.org/jira/browse/CALCITE-5578): `SUM(CASE WHEN c THEN x ELSE 0 END)` is 0 where `SUM(x) FILTER (WHERE c)` is NULL, on a table without a row for `c`) and `testReduceWithNonTypePredicate` ([CALCITE-5516](https://issues.apache.org/jira/browse/CALCITE-5516): `AVG` against an integer cast of the `SUM`/`COUNT` quotient), and Literature positions 46 and 47, which the paper says need more than 1,000 tuples to tell apart. The harness runs DuckDB, where `AVG` of an integer is a double, so it refutes the second Calcite pair; under Calcite's own typing (`AVG(INTEGER)` is `INTEGER`) the two sides can agree, and that pair is counted only as refuted, never as a label. The two Literature pairs come out `agrees`: no database of the sizes the generator reaches separates them. None is proved. `tests/test_known_nonequivalences.py` pins the same two Calcite rewrites on plain SQL with DuckDB witnesses. The current Calcite rule tests (the mined set) already carry the fixed form of `testAggregateCaseToFilter`, which is equivalent and proves.

## Results (Literature and Calcite-397 2026-10-04, LeetCode 2026-10-02 less 6 proofs on 2026-10-04)

| Suite | Pairs | Proven equivalent | Refuted (executed) | Agree on random databases | Not run | Wrong |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Literature | 64 | 28 | 25 | 9 | 2 | 0 |
| Calcite-397 | 397 | 335 | 29 | 24 | 9 | 0 |
| LeetCode (all pairs) | 23,994 | 4,787 | 5,676 | 12,460 | 1,071 | 0 |

LeetCode lost 6 proofs (4,793 to 4,787) to the parser check: 6 of the 17 pairs it refuses had been proved, because they use `||` as OR (MySQL's default) where sqlglot reads concatenation. That was measured by re-running those 17 pairs, not the whole suite.

The [harness translation](#harness-translation), with the bare-word and `$` fixes from the full LeetCode rerun, took Calcite-397 from 197 proved, 15 refuted and 96 not run to these numbers, and Literature from 10 proved and 11 not run. VeriEQL marks five of our Calcite refutations equivalent (pairs 80, 120, 126, 257 and 367); each counterexample was executed and read by hand, and the queries do differ (for example pair 120's rewrite counts `DISTINCT ENAME` once per `JOB` in the ROLLUP subtotal rows, and pair 80 returns `'TABLE'` against `'TABLE '` because Calcite pads CHAR literals and DuckDB's VARCHAR does not).

The ORDER BY, LIMIT and OFFSET rules ([sqlsolver.md](sqlsolver.md#order-by-limit-and-offset)) added 5 Calcite-397 proofs (measured against master when they were merged), among them `ORDER BY 2 OFFSET 1` against `ORDER BY DEPTNO OFFSET 1` over a duplicated column, `ORDER BY CAST(DEPTNO AS DOUBLE)`, and a top-k pushed into `UNION ALL` branches.

On LeetCode, 3,310 of the 3,586 pairs VeriEQL itself refutes are refuted here (92%); we also refute 2,366 pairs VeriEQL timed out on, could not read or did not refute. VeriEQL marks 30 of our LeetCode refutations equivalent; the ones read by hand differ on an empty table or a NULL (`NOT IN` over a NULL, `ROUND(SUM(..) / COUNT(*))` against `COALESCE(.., 0)`), cases VeriEQL's bounded search did not reach. VeriEQL refutes one pair we prove (21215) without a counterexample that replays; the two queries differ only by `GROUP BY 1` against `GROUP BY E1.EMPLOYEE_ID` and a `WHERE EMPLOYEE_ID IS NOT NULL` on the primary key, so the proof stands. The LeetCode row was measured on master 7b7ba13 (with the harness translation, the HAVING-only wider search and the optimizer recheck); prover rules merged after it, such as `cast_rules.py`, are not in it yet. The full LeetCode run takes about 5.8 hours on 4 cores. Search settings were tuned on every 24th pair; every other pair is untouched by that tuning. "Not run" means a query that DuckDB or the parser rejects (a table the schema names differently, `GROUP BY` positions that name aggregates, Calcite-only syntax). Translation handles a set operation of bare table names (`(R UNION ALL S)`, relational-algebra shorthand in two Literature pairs, spelled `(SELECT * FROM R UNION ALL SELECT * FROM S)` and marked `adapted`; both are then proved), bare words used as strings, `$` in names, unqualified `GROUP BY` names that DuckDB calls ambiguous, `SUBDATE`/`ADDDATE`, `CROSS JOIN .. ON` and MySQL's ungrouped columns.
