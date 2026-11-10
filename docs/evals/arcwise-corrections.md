# Arcwise-Plat corrections of BIRD

[Plain-language version](../../docs_simple/evals/arcwise-corrections.md)

[Arcwise-Plat](https://github.com/uiuc-kang-lab/text_to_sql_benchmarks) (CC BY-SA 4.0; Jin et al., "Pervasive Annotation Errors Break Text-to-SQL Benchmarks and Leaderboards", VLDB 2026) re-annotates the 498 questions of BIRD Mini-Dev. Where BIRD's gold SQL was wrong, a record keeps it as `original_SQL` next to the repaired `SQL`. Each such record is a pair of queries a human judged to **differ in meaning**, so the set is **must-not-prove**. Results file: `arcwise-corrections` ([inventory row A-E01](public-sources.md)).

| File (pinned commit `fe76604`) | Records with a repair | Scored |
| --- | ---: | ---: |
| `data/arcwise_plat_sql_only_with_diff.json` (Arcwise-Plat-SQL: only the SQL is repaired) | 83 | 83 |
| `data/arcwise_plat_full_with_diff.json` (Arcwise-Plat: question, evidence and SQL may be repaired) | 134 | 61 |

72 of the 134 full-file pairs are the same pair as in the SQL-only file and are not counted twice; one more differs only by `INNER JOIN` against `JOIN` (the same query once parsed) and is left out. The scored pairs are tagged `sql-only-<question id>` and `full-<question id>`; 60 of the 144 come from a record whose question or evidence was also repaired.

**The data is never committed.** It is CC BY-SA, so `tools/arcwise_bench.py` downloads the two files and the eleven BIRD schemas at the pinned commit into `$KUMOSQL_BENCH_DATA/arcwise` (default `~/.cache/kumosql-bench/arcwise`) and checks each against a SHA-256 digest in the script. A download that does not match fails; the test skips when GitHub cannot be reached. The original and the corrected query are kept apart in every record, and neither is rewritten.

```
python tools/arcwise_bench.py                     # the 115 dev pairs (the default); about 90 seconds on 2 cores
python tools/arcwise_bench.py --split all         # every pair: dev, held-out and all reported apart
python tools/arcwise_bench.py --show proven,unknown
python tools/arcwise_bench.py --overlap           # queries shared with ParSEval's BIRD dev and DLBench's BIRDTrans
python tools/arcwise_bench.py --write-results     # every pair; update the results file and the scoreboard
```

## How a pair is decided

BIRD's databases are on Google Drive and HuggingFace, which are not reachable here, so the pairs are decided on databases KumoSQL builds. The tables, declared types, primary keys and foreign keys come from the M-Schema files the same repository ships for BIRD dev (`text_to_sql_agents/Contextual-SQL/few_shots/mschema`, MIT). The decision is [LLM-SQL-Solver's](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/llm-sql-solver.md), unchanged: the corrected query plays Spider's gold query.

1. **proven**: `prove_equivalent_algebraic` (SQLite dialect, no keys, output names ignored) proves the pair, and neither query compares a text column with a number. Results are compared as lists when the corrected query ends in `ORDER BY`, as bags otherwise. A proof of a pair that differs in meaning is **wrong** unless the two queries return the same result on every database; each proved pair is inspected by hand and either listed in `COSMETIC` of the script with the reason, or in `FALSE_PROOFS` (counted as wrong, kept as a regression). A proof in neither list counts as wrong.
2. **refuted**: SQLite returns different results for the two queries on a database that respects the keys and foreign keys: 1,000 random databases, then the targeted suite, then the z3 bounded check, each replayed in SQLite. Ties under a `LIMIT` never count (each database is loaded in two row orders and used only if each query returns the same both times).
3. **unknown**: anything else, and any pair that runs past 75 seconds or crashes z3 (each pair runs in its own process; `how` is `timeout` or `crash`). **unsupported**: SQLite rejects a query.

The replay engine is SQLite, not DuckDB: BIRD's SQL is SQLite SQL (`IIF`, `STRFTIME`, backtick quoting, `LIMIT 9, 2`). DuckDB's optimizer has wrong-result bugs, so a counterexample from DuckDB would have to agree with `kumosql.duckdb_load.run_unoptimized`; none is used here.

**Held out.** One pair in five, by the SHA-1 of `arcwise\n<pair id>` (29 of 144), is held out. A run without `--split` reads only the 115 dev pairs; `--split held-out` is for final scoring, and `--split all` (also what `--write-results` needs) prints dev, held-out and combined counts on separate lines. `--show` prints a held-out pair's SQL only under `--split held-out`. The held-out pairs were first run in the final scoring run.

## Scores

2026-10-04: **111/144 refuted, 0 proved (0 cosmetic), 0 wrong**. Held out: 20/29 refuted, 0 proved.

| Part | Pairs | Refuted | Proved | Unknown |
| --- | ---: | ---: | ---: | ---: |
| Arcwise-Plat-SQL | 83 | 70 | 0 | 13 |
| Arcwise-Plat, pairs not already above | 61 | 41 | 0 | 20 |

Of the 111 refutations, 96 come from random databases, 4 from the targeted suite and 11 from the z3 bounded check. Of the 33 unknown, 1 timed out and 1 crashed z3; `sql-only-963` and `full-1524` are the two on the dev side (substring arithmetic on a time string; a prover counterexample that z3 could not turn into a model). No pair is proved.

Example: record 1362 changes `COUNT(city)` to `COUNT(DISTINCT city)` over a zip-code table. The random databases do not reach it (the filter needs one county and state), but the z3 bounded check builds a database with two rows in the same city (2 against 1), and SQLite runs both and returns different counts.

## Limits

* The refutations are on databases KumoSQL builds, with BIRD's declared types and keys but not its data. A repair whose effect needs realistic values (a birthday, `STRFTIME` over a real date, a time string such as `1:23.456`) stays unknown: the random values do not have that shape. Most of the unknown pairs are of this kind. A refutation is real (a valid database and a SQLite run); an unknown is not a claim that the repair is cosmetic.
* The repairs are the authors' judgement of what the question asks. This eval scores that the two queries differ in meaning, not that the corrected query is right.
* The M-Schema files carry BIRD's column types, not its constraints beyond primary and foreign keys, so a database may be one BIRD's real data could not be.
* Results are compared as bags (lists under `ORDER BY`), not as BIRD's sets: a repair that only removes duplicates (`DISTINCT`) counts as different here.
* Overlap: 68 of the 144 original queries (and 1 corrected query) also appear, by normalized text, in the BIRD dev file ParSEval ships (`--overlap`); none appears in DLBench's BIRDTrans, which is translated from BIRD's training data. Neither is an eval here, so no number is counted twice.
* The harness was built on the 115 dev pairs; no prover change was made for this eval, and the held-out pairs were not shown before the final run.
