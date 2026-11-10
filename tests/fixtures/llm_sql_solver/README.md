# LLM-SQL-Solver Spider pairs

Query pairs from LLM-SQL-Solver (Zhao et al., "LLM-SQL-Solver: Can LLMs Determine SQL
Equivalence?", arXiv 2312.10321), scored by `tools/llm_sql_solver_bench.py`.

* Source: https://github.com/ZhaoFuheng/LLM-SQL-Solver, commit
  `5b97da7a2d9ad18e12f8b1f1a186413d1eba6f81` (2024-11-21), folder `data/`.
* Licence: MIT, Copyright (c) 2024 ZhaoFuheng (see `LICENSE`, copied from the repository).
* Each pair is a Spider gold query and a DAIL-SQL query on one of 20 Spider databases.

## Files

| file | content |
| --- | --- |
| `semantic_inequivalent.jsonl` | the source file, unchanged: 180 pairs (`sql1`, `sql2`, `"semantic equivalence": false`, `schema`) whose results differ on the Spider databases |
| `relaxed.jsonl` | the source file, unchanged: 70 pairs (`gold_sql`, `pred_sql`, `hardness`, `database`, `execution_match`, `human_preference`, `schema`) labelled by expert majority vote, 52 `equivalent` and 18 `inequivalent` |
| `spider_column_types.json` | the column types (`number`, `text`, `time`, `others`, `boolean`) of the 20 databases, taken from Spider's `tables.json` as distributed in https://github.com/taoyds/test-suite-sql-eval at commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c` (the same file as `evaluation_examples/examples/tables.json` in https://github.com/taoyds/spider). Spider (Yu et al., 2018) is released under CC BY-SA 4.0; this file is a derived extract under the same licence |
| `LICENSE` | LLM-SQL-Solver's MIT licence |

The source's third file, `semantic_equivalent.jsonl`, is not copied: its 232 pairs are SQLSolver's
Calcite pairs (`tests/fixtures/sqlsolver/calcite_pairs.txt`), all 232 matching after whitespace and
case are normalised, and its DDL is the same Calcite schema.

## Original and adapted

The two `.jsonl` files are the originals. The harness adapts a query in one way only: a name in
double quotes that is no column of the database (for example `"JetBlue Airways"`) is read as a string,
as SQLite reads it. The harness reports which pairs were adapted (52 negatives, 17 relaxed).

## Overlap with other evals

Checked 2026-10-03 by comparing whitespace- and case-normalised query texts: no pair is in SQL-IQ's
Equivalence Judge set (`data/sql_equ_judge/sql_equ_judge.jsonl` at SQL-IQ commit
`fc290c9f4e72d501e05b665f9abbdd2d88d7b2b0`), which also draws on Spider-style schemas. Spider 2.0
(`tests/fixtures/spider2`) is a different, BigQuery-based dataset.
