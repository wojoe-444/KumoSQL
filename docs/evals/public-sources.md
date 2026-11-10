# Public SQL evaluation sources: inventory

[Plain-language version](../../docs_simple/evals/public-sources.md)

[Public SQL evaluation sources](../public-sql-evaluation-sources.md) (research dated 2026-10-02) lists 50 public suites, databases, rewrite corpora and BigQuery projects; [Additional public SQL sources](../additional-public-sql-sources.md) adds 60 more (see [Additional sources](#additional-sources)). This page records, for each one, whether KumoSQL already scores it, what it overlaps, its licence, whether it can be downloaded from here, and what is being added. It was checked on 2026-10-03 by cloning every repository at its current head; the commit is the pin for anything added.

Downloads go through a proxy that blocks HuggingFace, Google Drive, Dropbox, Git LFS, Aliyun OSS, `yale-lily.github.io`, `downloads.mysql.com`, `postgrespro.ru`, `duckdb.org` and `docs.cloud.google.com`. GitHub clones, raw files and release assets work.

Status: **covered** (an existing eval already scores it), **new** (being added, one pull request per source), **not added** (with the reason). Sources without a licence, or with GPL or share-alike terms, are downloaded at a pinned commit when the eval runs and never committed.

## Evaluation suites (E01–E15)

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| E01 | SQLSolver Calcite, Spark, TPC-C, TPC-H pairs | Apache-2.0 | yes | 232 + 124 + TPC-C + TPC-H pairs | covered: `sqlsolver-calcite`, `sqlsolver-spark`, `sqlsolver-tpcc`, `sqlsolver-tpch` and the bounded rows ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/sqlsolver.md)) |
| E02 | SQL-RewriteBench | Apache-2.0 with exclusions | yes | 180 cases | covered: `sql-rewritebench` ([page](rewrite-benchmarks.md)) |
| E03 | DuckDB SQLLogicTests | MIT | yes | every `test/sql` file | covered: `engine-duckdb-slt-plain`, `engine-duckdb-slt-amplified` ([page](engine-suites.md)) |
| E04 | Original SQLite SQLLogicTest | public domain | yes (GitHub mirror) | every 4th file | covered: `engine-sqlite-slt-plain`, `engine-sqlite-slt-amplified` ([page](engine-suites.md)) |
| E05 | SQLancer | MIT | yes | generator | covered: KumoSQL's own TLP/NoREC fuzzer, `sqlancer-tlp-norec` ([page](fuzzing.md)) |
| E06 | Spider distilled test suites (`taoyds/test-suite-sql-eval`, `ruiqi-zhong/TestSuiteEval`) | Apache-2.0 (code); TestSuiteEval has no licence | code yes; test-suite databases are on Google Drive (blocked) | 557 hand-labelled ESM false negatives | **new** (batch 1, with E07): the hand-labelled equivalent pairs, downloaded at run time, refuted only on databases KumoSQL builds |
| E07 | Spider 1.0 (`taoyds/spider`) | Apache-2.0 repo, CC BY-SA 4.0 data | queries, `tables.json` yes; SQLite databases blocked (Drive, Yale site) | 1,034 dev and 7,000 train gold queries | **new** (batch 1): gold queries as rewrite inputs, checked by proof or on generated databases that respect the keys |
| E08 | Spider 2.0 | MIT | yes | 547 Lite tasks, 68 dbt | covered for the 142 published BigQuery gold queries: `spider2-bigquery` ([page](spider2-bench.md)). The SQLite and Snowflake gold are not scored (KumoSQL reads BigQuery); the dbt project archives are on Drive |
| E09 | BIRD Mini-Dev | CC BY-SA 4.0 | no: databases and gold SQL are on HuggingFace, Drive and Aliyun | 3 × 500 questions | not added: the GitHub repository holds only model predictions |
| E10 | VeriEQL | CC BY-NC-SA 4.0 | yes | 397 Calcite, 64 literature, 23,224 LeetCode | covered: six `verieql-*` rows and the bounded rows ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/verieql.md)) |
| E11 | ParSEval (`sfu-db/ParSEval`) | Apache-2.0 | yes | 21,955 LeetCode pairs, 1,534 BIRD dev questions | not added: its LeetCode pairs are VeriEQL's LeetCode set (covered), and its BIRD pairs (gold against DAIL-SQL predictions) carry no equivalence label, so a proof could not be scored |
| E12 | QED prover tests | MIT / Apache-2.0 | yes | 373 pairs | covered: `qed-calcite` ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/sqlsolver.md)) |
| E13 | Logos core corpus (`WindOctober/Logos`) | MIT, source families keep their terms | yes | 22 R-Bot TPC-H, 37 R-Bot DSB, 14 TPC-DS variant pairs; its VeriEQL, WeTune parts are covered | covered: `logos-core-proof`, `logos-core-executed`, the 73 TPC-H, DSB and TPC-DS pairs proved and checked on generated data ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/logos.md)) |
| E14 | PARROT | README says MIT, no LICENSE file | yes (1.4 GB) | 598 curated pairs not in the repository | not added: the curated pairs are leaderboard-only; the folder redistributes about 38 datasets under their own terms (see [DLBench](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/dlbench.md)) |
| E15 | Feedback-driven SQL optimization artifact | GPL-3.0-or-later | yes (zipped run bundles) | PostgreSQL run data | **new** (batch 2, investigate): only if the run bundles hold original and rewritten SQL with a result check; otherwise reference only |
| — | QUITE (`Yuyang-Song/QUITE`) | none stated | yes | 4,160 LLM rewrites of TPC-H, DSB, Calcite and SQLStorm queries, each flagged equal or not on the benchmark instance (587 not) | covered: `quite-rewrites` (1,352/2,609 flagged-equal pairs proved), `quite-negatives` (135 of 498 flagged-unequal pairs refuted, none proved wrongly), 0 wrong ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/quite.md)); pinned at commit `0cffd7c`, downloaded at run time |
| — | E3-Rewrite | — | — | — | not added: no public pair release was found |
| — | DBridge, EQUITAS | — | — | — | not added: DBridge optimizes imperative programs; EQUITAS has no released corpus beyond the Calcite families above |

## Complete databases (D01–D16)

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| D01 | Jaffle Shop DuckDB (`dbt-labs/jaffle_shop_duckdb`, `duckdb` branch) | Apache-2.0 | yes | 3 seeds, 5 models, YAML tests | covered: `jaffle-shop`, `jaffle-shop-refactors`, `jaffle-shop-refutation` ([page](pipeline-equivalence.md#jaffle-shop-a-real-dbt-project)) |
| D02 | Chinook | MIT-style | yes | 11 tables, no views | **new** (batch 1, with D03): `sample-databases-rewrites`, `sample-databases-pairs` ([page](sample-databases.md)) |
| D03 | Northwind (`instnwnd.sql`) | MIT | yes (raw file) | 13 tables, 16 views | **new** (batch 1): the 16 upstream views are original workload queries; `sample-databases-rewrites`, `sample-databases-pairs` ([page](sample-databases.md)) |
| D04 | Pagila (`devrimgunduz/pagila`, release 4.1.1) | PostgreSQL | yes (the 13 MB data file is committed) | 16 tables (`payment` in 55 monthly partitions), 122,209 rows, 8 views (one materialized), 9 SQL and plpgsql functions and one aggregate | **new** (batch 2): `sample-databases-pagila-rewrites`, `sample-databases-pagila-pairs` ([page](sample-databases-pagila.md)); the 8 views and the function bodies are workload queries, the README's portable queries too |
| D05 | Sakila (`datacharmer/test_db/sakila`, Oracle's BSD scripts) | New BSD | yes (the MySQL download site is blocked; the mirror holds Sakila Spatial 0.9 as `sakila-mv-schema.sql` and `sakila-mv-data.sql`, not the 1.2 files of that site) | 16 tables, 7 views, 6 routines, 47,273 rows | covered: `sample-databases-sakila-rewrites` (0 wrong in 394 executed, 180 verified), `sample-databases-sakila-pairs` (23/27 proved, 37/37 refuted, 0 wrong) ([page](sample-databases.md#sakila)) |
| D06 | TPC-H | Apache-2.0 generator, TPC terms | yes (`tpchgen-cli`) | 22 queries | covered: `transformation-workloads`, `sqlsolver-tpch` ([page](transformation-bench.md)) |
| D07 | TPC-DS | TPC terms | yes | 99 queries | covered: `transformation-workloads`, `analytical-sql-coverage`, `mv-benchmark` |
| D08 | Microsoft DSB | MIT | yes | 52 templates | covered: `analytical-sql-coverage` ([page](analytical-sql-coverage.md)) |
| D09 | AdventureWorks OLTP | MIT | yes (release asset) | about 70 tables, 20 views | **new** (batch 2): views as workload queries if the T-SQL adapts cleanly |
| D10 | WideWorldImporters | MIT | release holds only SQL Server `.bak` backups | — | not added: needs a SQL Server restore |
| D11 | Employees (`datacharmer/test_db`) | CC BY-SA 3.0 | yes (167 MB) | 6 tables, 3.9 M rows | **new** (batch 2): downloaded at run time, slow lane |
| D12 | Postgres Professional Airlines | MIT | no: `postgrespro.ru` is blocked | 9 tables, 133 MB dump | not added |
| D13 | Join Order Benchmark / IMDb | unverified, IMDb terms | yes | 113 queries | covered: `job-cardinality`, `job-endtoend`, `job-alternative-forms`, `mv-benchmark` |
| D14 | BigQuery Stack Overflow | hosted | needs a billed Google project | — | not added: evals run offline without credentials. The Dataform example project (below) models the same tables |
| D15 | GA4 obfuscated sample | hosted | needs a billed Google project | — | not added: as D14. GA4-shaped SQL is already in `terashim-ga4` ([page](bq-real-corpora.md)) and Spider 2.0's GA4 tasks |
| D16 | Apache Sedona SpatialBench | Apache-2.0 | yes | spatial queries | not added: KumoSQL has no GEOGRAPHY support |

## SQL before/after sources (R01–R09)

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| R01 | SQLGlot optimizer fixtures | MIT | yes | optimizer, qualify, simplify, identity | covered: `engine-sqlglot-fixtures-plain`, `-amplified`; each fixture's expected output is executed ([page](engine-suites.md)) |
| R02 | SQLSolver artifacts | Apache-2.0 | yes | as E01 | covered with E01 |
| R03 | WeTune Calcite pairs (`winoros/wetune`) | Apache-2.0 | yes | 232 pairs | covered: the same 232 pairs, in the same order, as SQLSolver's Calcite file (165 identical, 67 with small text changes). WeTune's 50 issues: `wetune-issues`. Its application workloads are in Git LFS (blocked) |
| R04 | QueryBooster experiments | GPL-3.0 | yes | 30 WeTune-application pairs, 14 rule-training pairs, 18 Twitter CAST pairs; its 228 Calcite pairs are SQLSolver's | added: `querybooster` ([page](rewrite-benchmarks.md#querybooster-experiment-rewrites)), downloaded at run time; the Calcite pairs are inventoried against SQLSolver's, not re-scored |
| R05 | Calcite `RelOptRulesTest.xml` | Apache-2.0 | yes | — | covered: `calcite-mined` ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/sqlsolver.md)) |
| R06 | Cosette | BSD-2-Clause | yes | 59 cases | covered: `cosette`. The two cases the source document names: `countbug` is scored (not equivalent); `SelfJoin0` is skipped because its table has no declared columns. Added (batch 2): `cosette-adapted`, `SelfJoin0` and the other three files skipped for no declared columns, written with declared columns, plus 5 negative siblings ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/sqlsolver.md#adapted-cosette-pairs)) |
| R07 | Logos core corpus | as E13 | yes | as E13 | with E13 |
| R08 | BigQuery computation and nested-field guides | Google docs terms | `docs.cloud.google.com` is blocked here | — | left to the vendor-documentation rewrites eval of the "New equivalence evals" thread; the nested-layout migration needs a schema-mapping contract KumoSQL does not have |
| R09 | SPES | Apache-2.0 | yes | Calcite pairs | covered: `spes-only` and `bounded-spes` |
| — | R-Bot (`curtis-sun/LLM4Rewrite`) | Apache-2.0 | yes | 43 pairs | covered: `rbot-calcite`; the TPC-H and DSB pairs come with E13 |

## BigQuery and Dataform projects

| Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- |
| Dataform BigQuery example (`dataform-co/dataform-example-project-bigquery`) | MIT | yes | 10 SQLX files | covered: `bq-real-corpora` ([page](bq-real-corpora.md)), project `dataform-stackoverflow` |
| BigQuery Utils (`GoogleCloudPlatform/bigquery-utils`) | Apache-2.0 | yes | 136 SQL UDFs with 206 test groups; 18 views and Dataform examples | covered for the UDF tests: `bigquery-utils-udfs` ([page](bigquery-behavior-eval.md#bigquery-utils-udf-tests)); views and Dataform examples covered: `bq-real-corpora` ([page](bq-real-corpora.md)), projects `bqutils-views` (the five `.sql` files of `views/`) and `bqutils-datavault`; the other two Dataform examples hold only JavaScript |
| Marketing Analytics Jumpstart Dataform | Apache-2.0 | yes | 54 SQLX files | covered: `bq-real-corpora` ([page](bq-real-corpora.md)), project `marketing-jumpstart` |
| Security Analytics Dataform | Apache-2.0 (archived) | yes | 107 SQLX files | covered: `bq-real-corpora` ([page](bq-real-corpora.md)), project `security-analytics` |
| ClickBench BigQuery | CC BY-NC-SA 4.0 | yes | 43 queries | covered: `clickbench-rewrites` |

## Batches

Every source marked new is one pull request with its own harness, test marked `eval`, results file and docs page or section. Each pins its source commit, keeps original and adapted cases apart, records a baseline before any fix, keeps failures as regressions and holds out a fifth of the cases by hash. A false proof found by a new eval is kept as a regression and reported to the prover's owner rather than fixed in the eval's pull request.

- **Batch 1:** Chinook and Northwind; Jaffle Shop; the four BigQuery and Dataform projects; BigQuery Utils UDF tests; Logos TPC-H, DSB and TPC-DS pairs; QUITE; QueryBooster; Spider gold queries and the TestSuiteEval false negatives.
- **Batch 2:** Pagila, Sakila, AdventureWorks, Employees, Oracle HR/CO/SH and FIBEN in the sample-database eval; Cosette `SelfJoin0`; the feedback-driven optimization artifact; Arcwise corrections; SQLFluff refusal cases; Trino, Spark and engine-regression pairs; Mozilla `bigquery-etl` tests; GoogleSQL compliance expected rows; the demo pipeline, mimic-code and patents SQL in the real-projects corpus.

## Additional sources

From [Additional public SQL sources](../additional-public-sql-sources.md), checked on 2026-10-03. IDs are that document's; they restart at 1, so they are prefixed `A-`. `postgresql.org`, `sqlite.org`, `physionet.org`, `ldbcouncil.org`, `relational.fel.cvut.cz`, the Göttingen Mondial site and the GitHub issues API are also blocked here.

### Evaluation artifacts

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| A-E01 | Arcwise-Plat-SQL corrections (`uiuc-kang-lab/text_to_sql_benchmarks`) | CC BY-SA 4.0 | yes | 498 BIRD records with original and corrected SQL, plus BIRD schemas | covered: `arcwise-corrections` ([page](arcwise-corrections.md)): original against corrected SQL as negatives, refuted on databases KumoSQL builds; downloaded at run time, never committed |
| A-E02 | Dr.Spider | Apache-2.0, CC BY 4.0 | no: `data.tar.gz` is a Git LFS pointer | 17 perturbation suites | not added |
| A-E03 | IBM text2sql eval toolkit results | CC BY-SA 4.0 | no: results are on HuggingFace | — | not added |
| A-E04 | SQL-IQ | MIT | yes | — | covered: `sql-iq-equivalence`, `sql-iq-judge`, `sql-iq-errors` ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/sql-iq.md)) |
| A-E05 | BIRD-CRITIC | CC BY-SA 4.0 | no: HuggingFace; solutions by email | 500 + 530 tasks | not added |
| A-E06 | SQLStorm | MIT | yes | — | covered: `analytical-sql-coverage`, `transformation-workloads` |
| A-E07 | LiveSQLBench | CC BY(-SA) 4.0 | no: HuggingFace; gold by email | — | not added |
| A-E08 | BIRD-INTERACT | CC BY-SA 4.0 | no: as A-E07 | — | not added |
| A-E09 | CEB IMDb and Stack workloads | MIT | no: the download scripts fetch from Dropbox | — | not added; STATS-CEB and JOB are covered ([page](../joinorder.md)) |
| A-E10 | PolySQL | MIT | yes | — | not added: migration infrastructure, not a corpus |
| A-E11 | BEAVER | MIT | no: contact-gated HuggingFace files | — | not added |

### Before/after sources

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| A-SQLFluff | ST01, ST02, ST04, ST05, ST06, ST09, CV12 fixes | MIT | yes | 119 fixes | covered: all 119 are in `sqlfluff-semantic-fixes` (sqlfluff 4.3.0). covered (batch 2): the 212 ST and CV12 cases with no fix, PR 8169's later-branch correlation included, in [`sqlfluff-refusals`](sqlfluff-fixtures.md#refusal-cases) |
| A-R01 | Trino `AbstractTestJoinQueries` two-query assertions | Apache-2.0 | yes (raw file) | 6 named, more in the file | covered: `engine-paired-tests` (41/157 decided correctly across all sources, 0 wrong), all 137 two-literal assertions at a pinned commit, 134 scored ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/engine-paired-tests.md)) |
| A-R02 | Spark `SubquerySuite` EXISTS/IN/NOT IN cases | Apache-2.0 | yes (raw file) | 5 | covered: `engine-paired-tests`, nine pairs on the l/r tables with the NOT IN / NOT EXISTS negative, seven scored ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/engine-paired-tests.md)) |
| A-R03 | EET bug bundles | GPL-3.0 | links only | — | not added: the index links to reports on blocked hosts |
| A-R04 | PostgreSQL EET regressions | PostgreSQL | reports blocked; the PG17976 fix's `join.sql`/`join.out` are on GitHub | 3 | covered: `engine-paired-tests`, PG17976's regression query from `join.sql` with its join-removed and dropped-qual counterparts ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/engine-paired-tests.md)); the other two reports are blocked |
| A-R05 | SQLite `OR FALSE` report | public domain | no: `sqlite.org` is blocked | 1 | not added |
| A-R06 | JoinEquiv | no licence found | yes | — | not added as code; its projection boundary is an authored negative in `engine-paired-tests` ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/engine-paired-tests.md)) |
| A-R07 | jOOQ documented transforms | documentation | no fixtures | 3 patterns | covered: `engine-paired-tests`, eight authored pairs and guards citing the pages ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/engine-paired-tests.md)) |
| A-R08 | DuckDB JoinEquiv issues 20483, 20486, 20608 | MIT | the MIT test file is on GitHub; the issues API is blocked | 3 | covered: `engine-paired-tests`, the 20608 fixture and the file's collation pair ([page](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs/evals/engine-paired-tests.md)); 20483 and 20486 live only in the blocked issues |

### Databases

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| A-D01–D03 | Oracle HR, Customer Orders, Sales History (`oracle-samples/db-sample-schemas`) | MIT (the licence file is an MIT permission notice, Copyright (c) 2023 Oracle) | yes | 7 + 7 + 9 tables | HR and Customer Orders **covered** (batch 2): `sample-databases-oracle_hr-rewrites`, `-pairs` and `sample-databases-oracle_co-rewrites`, `-pairs`, both databases loaded whole at commit `6660bad68c07` ([page](sample-databases.md#oracle-schemas-hr-and-customer-orders)); Sales History **not added**: 91 MB of CSV (918,843 sales rows), too large to commit |
| A-D04 | Mondial | CC BY 3.0 | no: host blocked | — | not added |
| A-D05 | IBM FIBEN | Apache-2.0 | yes (80 MB `data.zip`) | 152 tables, 237 distinct SQL targets | **new** (batch 2), not started: 152 tables and an 80 MB archive are not feasible in the Oracle pull request (see [sample databases](sample-databases.md#oracle-schemas-hr-and-customer-orders)); it needs a run-time download from the pinned commit |
| A-D06 | MIMIC-IV Demo | ODbL | no: `physionet.org` is blocked | — | not added; the GoogleSQL concepts (A-G03) are added without data |
| A-D07–D13 | BenchBase SmallBank, TATP, Epinions, Twitter, SEATS, AuctionMark, Wikipedia | Apache-2.0 | yes | 3–17 tables | not added in this pass: the data comes from Java loaders that would have to be ported and frozen |
| A-D14 | LDBC SNB SF0.003 | Apache-2.0 | no: `ldbcouncil.org` is blocked | — | not added |
| A-D15 | Synthea | Apache-2.0 | generator only | — | not added: needs the Java generator |
| A-D16 | OMOP CDM 5.4 | Apache-2.0 | yes | 39 tables | not added: population needs the separately licensed vocabulary |
| A-D17 | MusicBrainz | GPL schema, CC0 data | dumps are multi-GB | — | not added |
| A-D18 | Lahman | rights unclear | — | — | not added |
| A-D19 | Jolpica F1 | CC BY-NC-SA 4.0 data | — | — | not added |
| A-D20 | Star Schema Benchmark | TPC dbgen terms | C generator | 13 queries | not added |
| A-D21 | CH-benCHmark | TPC ancestry | — | 3-table extension | not added |
| A-C01–C03 | RelBench, The Join, CTU repository | various | no: HuggingFace and the CTU MySQL server are blocked | — | not added |

### GoogleSQL and SQLX

| ID | Source | Licence | Download | Size | Status |
| --- | --- | --- | --- | --- | --- |
| A-G01 | Mozilla `bigquery-etl` | MPL-2.0 | yes | 323 UDFs, 162 query tests with `expect` files | **new** (batch 2): UDF assertions and query tests with supplied inputs, as native expected results |
| A-G02 | GCP data pipeline demo | MIT | yes | 4 SQLX, 6 sample rows | **new** (batch 2): real-projects corpus |
| A-G03 | mimic-code concepts | MIT | yes | 65 GoogleSQL files | **new** (batch 2): real-projects corpus (no data) |
| A-G04, G06, G07 | wintermi MovieLens, BQE, IMDb Dataform | Apache-2.0 | yes | — | covered: `wintermi-*` in `bq-real-corpora` ([page](bq-real-corpora.md)) |
| A-G05 | `bq-bench` TPC-DS | Apache-2.0, TPC terms | yes | 99 queries | covered by the TPC-DS evals; needs a billed project to run natively |
| A-G08 | Dataform deployment sample | no licence | — | — | not added |
| A-G09 | Google patents public data examples | Apache-2.0 (archived) | yes | — | **new** (batch 2): real-projects corpus |
| A-G10 | GoogleSQL compliance tests | Apache-2.0 | yes | 7,870 queries already used without their results | **covered** (batch 2): the typed expected rows are an oracle for KumoSQL's BigQuery-to-DuckDB execution: 1,609/4,633 supported cases agree, 0 wrong ([page](googlesql-expected-results.md)) |
| — | BIRD-CRITIC BigQuery, SQLShare, Fashion Dataform, NHANES-GCP, SQLRight, DQETool, AMOEBA, SlabCity, CODDTest | — | — | — | not added: empty, unlicensed or not a released corpus (as the research says) |
