# Refactor: keep some tables, rearrange the rest

[Plain-language version](../docs_simple/refactor.md)

Say which tables must keep existing and keep returning the same rows, which may be dropped, merged or rewritten, and let KumoSQL look for simpler pipelines. Every option it returns has every protected table **proved** equal to the original; a move it cannot prove is rejected, never accepted.

## Classes

Each model is in one class, saved like scopes (a list of saved scopes plus explicit model names, in the data folder, never written to BigQuery):

| Class | Meaning |
| --- | --- |
| Protected | Must keep existing and return the same rows. Its SQL may change. |
| Editable | May be dropped, merged into another model, inlined into its readers or rewritten. |
| Neither | Read as it is, never modified. A model a "neither" model reads is *exposed*: it is checked like a protected one, because its reader cannot change. |

Protected wins when a model is in both. Models that are not queries (declarations, operations) and assertions are always "neither". Dataform models are what the search rearranges; other objects (views, functions and procedures seen in the BigQuery explorer) can be named in a class but are only read.

## Use it

- **UI:** the Refactor page. Tick saved scopes for each class or set a class per model (the filter and "Set shown to" classify many at once), Save, then *Find simpler pipelines*. The result is the Pareto front: for each number of models, the lowest total complexity found, with the moves, the changed SQL and the assumptions the proofs used.
- **CLI:** `python -m kumosql refactor DIR --protect report --editable stg1 --editable stg2` (also `--protect-scope`, `--editable-scope`, `--save`, `--max-states`, `--max-seconds`). Without class options it uses the saved classes. Output is JSON; progress goes to stderr.
- **API:** `GET /api/refactor`, `PUT /api/settings/refactor`, `POST /api/refactor/run` (starts a background search), `GET /api/refactor/status` (progress and the front so far), `POST /api/refactor/cancel`. In the UI the page keeps working while it runs, shows the front as it grows, and has a Cancel button; a cancelled run keeps what it proved.

## What it does

Moves: drop an unread editable model; inline an editable model into all its readers (as derived tables); merge an editable model into another that returns the same columns from the same tables. A move is kept only when `prove_models` proves each protected or exposed model of the new pipeline equal to the original (layer lemmas, then inlining; saved equivalences and declared source columns apply). An inline or merge that would put a table read under a `WITH` table of the same name in the reader is not tried, and incremental, script and run-dependent (clock, `RAND`) models are never matched with another model or inlined by the check (see [provers](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/provers.md)), so moving them is never proved. The search runs one greedy descent per weight on model count against complexity (`WEIGHTS` in `refactor.py`): at each step the cheapest moves are proved first and the first proved one is taken. Every proved state is a candidate, and the candidates are reduced to the Pareto front over total sqlfluff complexity (`kumosql.formatting.complexity`) and model count. Proofs are cached by the SQL of the models a protected table reads, so each is done once across descents.

## Fold tables into one

Name the intermediate tables and the table that ends the chain, and KumoSQL rewrites that table to read straight from what the chain read, then proves the new table returns the same rows. With `upstream -> A -> (B, C) -> D`, folding `A`, `B` and `C` into `D` leaves `upstream -> D`.

- **CLI:** `python -m kumosql consolidate-tables DIR D A B C`. It is a **read-only preview**: it prints JSON and never writes, moves or deletes any of your files, and it has no option that does (like every command that analyzes a project it appends timing lines to KumoSQL's own diagnostic log, `ui.log`, in the data folder) (`--help` only prints the usage; the options are `--timeout-ms` and `--stable-selection`). To see the usage without running anything, run `python -m kumosql consolidate-tables --help`. Output is JSON: `status` (`equivalent` or `unknown`), `sql` (the new SQL of `D`), `original_sql`, `folded` (sources first), the proof's `assumptions` and `notes`. Exit code 0 only when proved, 1 for `unknown`, 2 when the fold is refused.
- **Python:** `kumosql.consolidate.consolidate_tables(pipeline, ["A", "B", "C"], "D", schema=None, timeout_ms=5000, stable_selection=False)` returns a `ConsolidationResult` (`.proven`, `.sql`, `.status`, `.reason`, `.to_json()`).
- **API:** `POST /api/consolidate-tables` with `{"tables": [...], "target": "...", "stable_selection": false}` on the project loaded in the app.

Each folded table becomes a `WITH` table of `D`'s own query, named after the table and ordered sources first; every reference to it, from `D` or from another folded table, points at that name, so a table shared by two readers (`A` feeding `B` and `C`) is written once. A name that clashes with a `WITH` table already in `D` is suffixed (`a_2`). The check is the one the search uses (`check_observable`, built on `prove_models`): the new `D` must be proved equal to the original `D` read through the folded tables, with saved equivalences and declared source columns applied. When it is not proved the status is `unknown`, the SQL is returned for reading only, and nothing is claimed.

A fold through `UNION ALL`, or through a folded table with a `WITH` of its own, is proved like any other. The one common reason for `unknown` is a `SELECT *` over a table whose columns the prover does not know (a declared source without columns, a table the BigQuery catalog has not loaded): the reason then names that table. Declare its columns, or load the catalog in the app, and it proves.

A fold whose proof needs a `LIMIT` to cut the same rows each time is `unknown`, not `equivalent`. That is the case when a folded table or `D` has `ORDER BY .. LIMIT` and the order has ties (the prover's tie assumption) or the limited query is read as a derived table (its same-text assumption). Folding replaces a stored table with a recomputed query, and when the physical or tie order differs the `LIMIT` can pick other rows. A `LIMIT` over a total order (every output column in the `ORDER BY`) folds as before. If you know the selection is stable, state it with `--stable-selection` (Python `stable_selection=True`, API `"stable_selection": true`): the fold is then `equivalent`, the assumptions list the `LIMIT` assumption, and `notes` records that you accepted it. (#538)

A fold is refused, with the offending model named, when a folded table is still read by a model outside the set (the error lists those readers, so drop them from the plan or add them to the set), when a model is not a plain table or view (incremental tables, declarations, assertions and operations are refused), when a table has pre or post operations, when nothing reads a table, or when the SQL is a script, recursive, or still holds Dataform expressions. Only models in the loaded project count as readers: a dashboard or another project reading a folded table is not seen. The original models are not modified and nothing is written to the `.sqlx` files; the result is the SQL to put in `D` and the tables to delete. Assertions declared on a folded table are reported in `notes`. It does not replace the Refactor page's search, which decides what to fold; it is the move you choose yourself. It is not on the Refactor page yet.

## Scale

`python tools/make_refactor_project.py OUT --reports N` generates a project with duplicated filtered views, leftovers and N reports (protected), the rest editable. Measured on one core: 32 models (20 reports) 9 s, 7 front points from 32 to 26 models; 136 models (100 reports) 95 s, 7 front points from 136 to 100 models, every one proved. Cost grows with the number of proved moves times the protected tables each one touches; `--max-seconds` bounds it.

The [table minimization eval](evals/table-minimization.md) scores this search on 334 pipelines of 3 to 20 tables: 0 wrong, 164 of 270 dev cases simplified, 39% of the reference reduction on average.

## Limits

- Evidence is proof only. A move that is not proved is listed with the prover's reason (`rejected_moves`); there is no counterexample for it yet.
- Complexity is the sqlfluff structural score summed over models, so inlining lowers the model count but raises complexity (each derived table counts); the front shows that trade.
- It cannot extract shared logic into a new model, rewrite SQL inside a model, or write the result back to `.sqlx` files; it reports the new SQL. Proof coverage is the prover's: staged LEFT JOIN chains and AVG rebuilt across a rollup are still unknown (see [pipeline-equivalence.md](evals/pipeline-equivalence.md)).
- The search is bounded by `--max-states` and `--max-seconds`; the result says which limit stopped it.
