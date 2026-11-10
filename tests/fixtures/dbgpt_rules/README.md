# DB-GPT query-rewrite examples

The 36 before/after PostgreSQL rewrites DB-GPT uses as demonstrations for its LLM query rewriter,
scored by `tools/dbgpt_rules_bench.py`.

* Source: https://github.com/TsinghuaDatabaseGroup/DB-GPT, commit
  `73cb3297fae0823c4bf8c3451513f7dbd3961f4b` (2025-12-27), file
  `multiagents/prompt_template_scripts/query_rewrite/data/raw/train/rules.json`.
* Licence: Apache-2.0 (see `LICENSE`, copied from the repository; the repository has no NOTICE file).

## Files

| file | content |
| --- | --- |
| `rules.json` | the source file, unchanged (`examples` maps "1".."36" to `input` and `output`) |
| `cases.jsonl` | one scored case per example: `id`, `left`, `right` (the queries as scored), `label`, `note`, `adapted` (how the text differs from `rules.json`, or null), `schema` (table -> column -> type), optional `constraints` (keys, NOT NULL) and `counterexample` (table -> rows) |
| `LICENSE` | DB-GPT's Apache-2.0 licence |

## Labels

The source has no labels and no schemas; both were written for this eval and are checked on DuckDB by
`tests/test_dbgpt_rules_bench.py` (random databases for `equivalent`, the stored counterexample for
`not_equivalent`, DuckDB rejecting a query for `invalid`, each difference confirmed with DuckDB's
optimizer off).

| label | cases |
| --- | --- |
| equivalent | 30 |
| not_equivalent | 14 (`LIKE 'abc%'` against `> 'abc' AND < 'abd'`), 30 (`OR` split into `UNION ALL` repeats rows), 34 (the rewrite joins on the wrong column) |
| invalid | 10 (a scalar compared with a three-column row), 33 (a LEFT JOIN with no ON), 36 (`agg()` placeholder, `FROM t2.c2>t1.c2`) |

Adapted cases: 5 (`int4ge(1,3)`, PostgreSQL's internal int4 `>=`, written `1 >= 3`), 25 and 26 (a
leading `table student(...)` DDL fragment moved into the case's schema and constraints), 32 (`tc.c1`
read as `t2.c1`, the only table it can mean, and the derived table given an alias).

## Overlap with other evals

None found: the cases are short textbook rewrites on tables `t`, `t1`..`t4`, `student` and `score`,
none of which appear in the SQLSolver, Calcite-mined, QED, R-Bot, VeriEQL or SQL-IQ fixtures.
