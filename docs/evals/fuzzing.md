# Metamorphic fuzzing and unsafe-rewrite detection

[Plain-language version](../../docs_simple/evals/fuzzing.md)

Three seeded suites check KumoSQL's rewrites and its equivalence prover without any LLM at run time (a fourth, the [typed soundness fuzzer](#typed-soundness-fuzzer), hunts false proofs on typed schemas with integrity constraints). They share one oracle (random small DuckDB databases with NULLs, duplicates and empty tables) and one report format. Every case id names its seed, so a failure is reproduced by rerunning the same command.

```
python tools/unsafe_fuzz.py fuzz --count 60 --seed 1     # SQLancer-style TLP, NoREC and mutants
python tools/unsafe_fuzz.py unsafe --count 20            # plausible-looking unsafe rewrites
python tools/unsafe_fuzz.py compose --count 60          # rule chains (tools/compose_fuzz.py)
```

`--show` prints every false proof or bad counterexample, `--json FILE` writes the summary. Tests live in `tests/test_unsafe_fuzz.py`; the long runs are marked `slow`.

## What is measured

Keep these apart: they are different kinds of evidence.

| Level | Meaning |
| --- | --- |
| Unbounded proof | The prover claims the queries agree on every database. |
| Replayed counterexample | The prover returns a database; both queries are run on it in DuckDB and must really differ. |
| Agreement on executed datasets | Both queries agree on N random databases. Evidence only. |

**Correctness** (all must be 0): `false_proofs` (proved, but the oracle finds a separating database), `bad_counterexamples` (a prover counterexample that does not separate the queries when replayed), `label_errors` (a pair constructed to be equivalent that the oracle separates, which means a bug in the generator), and for the composition suite `behaviour_changes` (a rule step whose output differs from its input on some database).

**Coverage**: proved / refuted / unknown / unsupported / timeout / error for the prover, and, for pairs the prover does not refute, whether KumoSQL's own executed check (`check_result_equivalence`) finds the difference. For pairs that really differ the report gives the share refuted by the prover, the share whose counterexample replays, and the share found either way.

**Held out**: some families are marked `heldout` (TLP aggregates, NoREC, a handful of unsafe-rewrite templates). They were not looked at while fixing bugs. A fix that only helps the development families does not move the held-out numbers.

## Suites

**`fuzz`**: for random sources (one table, inner/left/cross joins) and random three-valued predicates, TLP (`Q` = `Q[p]` UNION ALL `Q[NOT p]` UNION ALL `Q[p IS NULL]`, also with DISTINCT/UNION and recombined COUNT/SUM/MIN/MAX) and NoREC (`WHERE p` against `COUNT(CASE WHEN p ...)` and a projected-then-filtered subquery). Mutated TLP/NoREC pairs (lost or duplicated partition, `SUM` instead of `COUNT`) and single-site mutations of generated multi-CTE queries (shapes: filters, DISTINCT, inner/left joins, GROUP BY with HAVING, CASE, COALESCE/IFNULL, UNION [ALL], IN / NOT IN / EXISTS subqueries) (flipped comparison, AND/OR swap, dropped conjunct or DISTINCT or WHERE or NOT, LEFT JOIN made INNER, UNION flipped, COUNT(*) made COUNT(col), constant bumped). A mutant is labelled *different* only if the oracle separates it.

**`unsafe`**: `NOT IN` against `NOT EXISTS` / anti-join, `COUNT(*)` against `COUNT(col)`, duplicate-producing joins against `IN`/`EXISTS`, `UNION` against `UNION ALL`, and WHERE/ON predicates moved across outer joins, each next to the variant that is correct.

**`compose`**: chains of the registered rules (random subset, random order, one to three repeats) over generated queries with CTE chains, duplicated and unused CTEs, subqueries and trivial predicates. After every step the output is compared with the step's input; the prover's verdict on each changed step is counted. The canonical pipeline (`canonical_rule_order()`) must reach a fixed point after one application (idempotence), within a bounded number of rounds (termination), and the same chain must give the same text twice (determinism).

## Partition recombination

TLP pairs used to stay unknown: the prover compared one block against three `UNION ALL` branches and found no row-preserving mapping. `src/kumosql/partition_rules.py` now puts a split query back together before the prover runs:

* `UNION ALL` branches that are the same query except for their WHERE filters, where no row can make two filters TRUE, become one branch filtered by the `OR` of the filters, and the filter is dropped when the `OR` is TRUE for every row. Both checks are made in three-valued logic with Z3: every comparison is an opaque TRUE/FALSE/NULL atom (the same atom has the same value in every branch), `x IS NULL` tests read the column's NULL state, and a comparison of columns and literals is NULL exactly when one of its columns is. So `p`, `NOT p`, `p IS NULL` (or `p IS TRUE`, `p IS NOT TRUE`) merge to the unfiltered query, while `p` and `NOT p` alone become `WHERE (p) OR (NOT p)`, which the prover refutes with a row where `p` is NULL. Complete partitions are taken first, largest first, so a duplicated partition stays a separate branch and remains refutable. Branches with aggregates, windows, DISTINCT, LIMIT, subqueries in the filter or nondeterministic functions are left alone.
* `SELECT SUM(v) FROM (SELECT COUNT(*) AS v FROM s WHERE p UNION ALL ... WHERE NOT p UNION ALL ... WHERE p IS NULL)` (also `SUM` of `SUM`, `MIN` of `MIN`, `MAX` of `MAX`) is the aggregate over the arguments' `UNION ALL`, but only when those branches merge into one: otherwise an aggregate over a `UNION ALL` keeps the per-branch partial form that `_split_aggregates` normalizes to.

Seed 2 (count 60) went from 238/691 to 569/751 equivalent pairs proved (575/735 after the generator change below and the aggregate facts in `docs/provers.md`; 587/735, held-out 359/420, after the outer-join flattening rules, whose unaliased-derived-table rule was written from a description of the NoREC shape), with 0 false proofs and 0 bad counterexamples: TLP WHERE 60/60 (was 2/60), TLP aggregates 234/240 held out (was 0; 6 are aggregates over an outer join the SMT model does not support), and more TLP mutants refuted (191/271, was 140/211). The held-out TLP aggregate and NoREC cases of seed 2 were not looked at; the aggregate rule was developed on seed 101 (237/240). Still open: TLP DISTINCT (1/60, needs `UNION DISTINCT` of same-source filters merged), NoREC `COUNT(CASE WHEN p THEN 1 END)` against `COUNT(*) ... WHERE p` is now proved by the structural aggregate rules (`src/kumosql/aggregate_rules.py`, which move a filter shared by every aggregate into `WHERE`): 639/735 proved, held-out 411/420, on master 80ac740.

Re-measured on 2026-10-04 (master 4d294afd, sqlglot 30.21): seed 2 proves 640/735 equivalent pairs (was 639) with 0 false proofs and 532/533 refuted. Rewrite composition (seed 21, 60 queries) runs 430 steps, 143 of which change the SQL, with 0 behaviour changes; the prover verifies 129 and leaves 14 unsupported (150 steps, 146 and 4 on 2026-10-02: the same seed draws different chains once the registered rule set changes, so the counts are not comparable and the extra unsupported steps were not investigated).

## Bugs these suites found

* The TLP DISTINCT pairs and the `UNION` mutant joined their partitions with a bare `UNION`, which BigQuery (and sqlglot's BigQuery reader) rejects, so 120 pairs per seed ran in neither engine. The generator now writes `UNION DISTINCT`.
* The executed check (`check_result_equivalence`) errored on any query that qualified a column by an unaliased table name (`SELECT t.a FROM t`): renaming the table dropped the name `t.a` refers to. It now keeps the original name as an alias. Before the fix the check could not run most unsafe-rewrite pairs.
* Prover counterexamples could fill a column the model never constrained with an arbitrary string, so an INT64 column held `''` and the counterexample could not be loaded. Unconstrained values are now `0`; the counterexample is still re-evaluated on the database before it is returned.
* `canonical_rule_order()` was not a fixed point: removing an unused CTE could leave another CTE with a single reader after `inline_single_use_ctes` had already run. Inlining now runs after CTE deduplication and removal.

A gap these suites showed: for most non-equivalent unsafe pairs the solver's model gives no counterexample ("no row-preserving mapping", "UNION shapes differ"); it refuted 40 of 339 unsafe pairs and 258 of 457 different fuzz pairs. The harness now calls the prover with `search_counterexample=True` and the column types (INT64), so an unproven pair is also run on databases built for it (`kumosql.executed_refutation`, see [provers.md](../provers.md#smt-equivalence-prover)) and the first database that separates the results comes back as the prover's counterexample, shrunk and replayed like any other. The report counts those as `refuted_by_executed_search`. When such a database separates a pair that the 40-database oracle had called equivalent (the oracle draws values from 0 to 3, so `u.b > 3` never holds), the pair is counted as different, not as a wrong refutation; a pair equivalent by construction would count as a label error. Results with it (2026-10-02, 0 false proofs and 0 bad counterexamples in both): `unsafe --count 20 --seed 1` proves 220/220 correct rewrites and refutes 340/340 unsafe ones (300 by the executed search; held out 80/80 and 60/60); `fuzz --count 60 --seed 2` proves 570/735 equivalent pairs and refutes 532/533 different ones (217 by the executed search; 309/517 before it). The 20 `DISTINCT` join versus `IN` pairs are proved by `kumosql.semijoin_rules`, which reads an inner join that only filters, under a duplicate-blind select, as an `EXISTS` test (tried when the first attempt finds no proof).

## Reusable cases

`python tools/unsafe_fuzz.py unsafe --count 20 --seed 1 --dump-cases tests/fixtures/unsafe_rewrite_cases.jsonl` writes the cases as JSONL (`id`, `family`, `left`, `right`, `expect`, `heldout`). The committed copy is checked against its seed by the tests; other threads can use it as faulty variants. `expect` is `equivalent` (by construction) or `either` (the oracle decides): 220 cases are equivalent by construction and 340 are `either`, of which the oracle separates 339 (all but `union-intersect-5`).

Every discovered failure becomes a regression test in `tests/test_unsafe_fuzz.py`. The pre-fix baseline for the first run: 2 bad counterexamples on `fuzz` seed 1 (count 3) and one non-idempotent canonical pipeline in the first 15 composed queries.

Seeds 31 (fuzz, count 40: 850 cases) and 31 (compose, 60 queries) over the richer shapes found no further bugs: 0 false proofs, 0 bad counterexamples, 0 behaviour changes.

The generated shapes also cover window functions, QUALIFY, SAFE_*/NULLIF/IF, date arithmetic, UNNEST, STRUCT and NULL-heavy LEFT/RIGHT/FULL joins. Counterexamples that need a fractional value are replayed on DOUBLE columns (they are valid for FLOAT64 only). A fix from the first run over these shapes: when no integer model exists, a counterexample column that the queries only compare with numbers could be given a string; the model now prefers any numeric value before any other.

## Typed soundness fuzzer

`tools/soundness_fuzz.py` hunts false proofs: pairs the prover proves while a database separates them. It is adapted from Sol's S015 differential fuzzer (an external review deliverable), with its oracle and execution domain kept and the generator widened.

```
python tools/soundness_fuzz.py --seed 2 --count 2000 --jobs 4 --output run.json   # a long run
python tools/soundness_fuzz.py --replay run.json --show                          # re-evaluate its findings
```

**Schema and databases.** Every pair runs over a small typed schema: `t(id, x, y INT64, s STRING)` keyed on `id`, `u(k INT64, v STRING)` (sometimes keyed on `k`, with `t.y` a foreign key to it) and sometimes a third table, with NULLs allowed except where a key or NOT NULL is declared. The prover gets the same schema, types and constraints (`TableConstraints` with keys, NOT NULL and foreign keys). Each pair runs on six DuckDB databases that obey the constraints: the seeded primary one, two more random ones (`--random-databases`), a NULL-heavy one, a duplicate-heavy one and an empty one.

**Oracle.** Results are exact row bags (duplicates count, order does not; integers and floats compare as exact rationals, so 9007199254740993 differs from its FLOAT64 rounding; NULL, booleans and strings stay apart). A difference counts only when DuckDB with its optimizer off agrees (`kumosql.duckdb_load.run_unoptimized`, #347). Queries are translated from BigQuery only inside a closed list of SQL node types where both engines agree: no division, no failing or narrowing casts, no positive `LIMIT` or `ROW_NUMBER` without a declared-key total order, no large-integer arithmetic, no explicit window frames, no `OFFSET`. Anything else is skipped, and DuckDB binding or runtime errors are never evidence.

**Generators** (mixed per seed; `--generators template,sol,mutant`):

* `template`: 24 rewrite templates that build both sides from shared random predicates, expressions, join types and aggregates. They cover predicate and NULL identities, COUNT/SUM/DISTINCT variants, outer-join type changes, ON/WHERE moves, join commutation, self-joins on keys, semi- and anti-joins, IN as a value, set operations with pushed filters and LIMIT 0 tails, aggregates over UNION ALL, GROUPING SETS/ROLLUP/CUBE expansions, HAVING moves, filters pushed into grouped and windowed derived tables, scalar subqueries against grouped LEFT JOINs (the COUNT bug), CTE scope and shadowing, QUALIFY/ROW_NUMBER, ordered LIMIT, foreign keys and LIKE. Some instances are sound and some are not; the databases decide. A pair is labelled equivalent only when its template is sound on every database, and a labelled pair that a database separates is a `label_error` (a generator bug).
* `sol`: Sol's 24 construct families, with sound identity, wrapper and duplicated-filter mutations on one pass and deliberate semantic changes on the next.
* `mutant`: a template query against one of its single-site mutants from `kumosql.query_mutants`.

The five false proofs Sol's S009 audit published run first (`tests/fixtures/soundness_fuzz/historical.json`); none may be proved again.

**Isolation and reduction.** Each evaluation runs in one of `--jobs` long-lived child processes, with a wall timeout after which the child is killed and restarted. A false proof is reduced while it stays one: SQL edits on either side (each rechecked by the prover and the databases), then the witness database alone, then its rows one at a time (rows need only the oracle; the prover never sees data).

**Known false proofs.** `tests/fixtures/soundness_fuzz/known_false_proofs.json` lists open false proofs with their cause and the thread fixing them. The report marks a finding `known` when its pair, or its reduced pair, is listed; the command exits 1 on any other false proof.

**Results** (seed 2, 2,000 generated pairs plus the 5 published ones, master f26d7f4): 1,059 proved, 224 refuted, 714 unknown, 8 skipped and 0 timeouts, with 0 false proofs and 0 label errors. The `_collapse_aggregate` false proof (folding `COUNT(d.n)` over a derived table with one row per group, including a UNION ALL of global COUNTs, into the inner `COUNT`) is fixed and its entry removed, so the known list is empty. On master 6e86a4b, 26,000 pairs over three seeds also found float widening of INT64 values above 2^53 (fixed by #427) and LIMIT 0 tails dropped from INTERSECT, EXCEPT and same-source UNION DISTINCT (S006-001, fixed since). On the same master, all but 2 of Sol's original 9 false proofs were already gone.

Two oracle bugs were fixed along the way. sqlglot writes `(y IS NOT NULL) IS NULL` for DuckDB as `NOT y IS NULL IS NULL`, which DuckDB reads differently, so the translator parenthesizes every operator operand. sqlglot also reads `t.x = -1 IS NULL` as `t.x = (-1 IS NULL)`, while BigQuery and DuckDB read `(t.x = -1) IS NULL` (as the prover now does), so the database ran a different query from the one the prover proved and reported a false proof that was not one. The translator now declines comparison chains without parentheses (`a = b IS NULL`, `a = NOT b IS NULL`, `a IS NULL IS NULL`), and the predicate-value template writes `(p) IS NULL`.

**Tests.** `tests/test_soundness_fuzz.py` checks the oracle (exact bags, refused queries, legal databases, the unoptimized recheck, worker timeouts), runs every template and checks that pairs labelled equivalent agree on every database, keeps the published S009 pairs unproved, and makes a seeded smoke run (seed 3, 120 pairs, about 20 seconds) that fails on any false proof not in the known list. The file declares `pytestmark = pytest.mark.eval`, so `run_tests.py --evals` includes it. Long runs are opt-in from the command line.
