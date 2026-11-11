# Lineage goldens: DataHub and OpenLineage

[Plain-language version](../../docs_simple/evals/lineage-goldens-bench.md)

Two public test suites state, for a SQL statement, the table and column lineage it must produce. This scores KumoSQL
against them (`python tools/lineage_goldens_bench.py [--details] [--write-results]`). They are scored separately and never
added together, because only one of them is an independent oracle.

| Corpus | Pin | Oracle |
| --- | --- | --- |
| [OpenLineage](https://github.com/OpenLineage/OpenLineage) `integration/sql/impl/tests` (Apache-2.0) | `7a84fd4d48aeed4d0e6f391e2b7fbc11c0ff4fd9` | a Rust parser (sqlparser-rs) that shares no code with sqlglot: **independent** |
| [DataHub](https://github.com/datahub-project/datahub) `metadata-ingestion/tests/unit/sql_parsing` (Apache-2.0) | `8a117ba1f5331b4133976fae4d5a99e20c3b67da` | goldens are sqlglot's own output, reviewed by maintainers: **not independent**, read as regression coverage |

`tools/lineage_goldens_harvest.py` rebuilds `tests/fixtures/lineage_goldens/{datahub,openlineage}.json` from clones of the
two repositories at those commits (the commands are in its docstring). Nothing about an expectation is changed.

## What was harvested

- DataHub: all 101 `assert_sql_result` calls in `test_sqlglot_lineage.py`; 99 harvested (2 reference golden files that do
  not exist upstream). 3 of them are skipped upstream (`pytest.mark.skip`) and are left out of the score.
- OpenLineage: all 158 `#[test]` functions under `integration/sql/impl/tests`; 140 harvested. 18 are not (a SQL string that
  is built, Snowflake stage and `IDENTIFIER()` syntax, an error-message assertion). A test marked `#[ignore]` upstream is
  left out of the score. Some table tests assert only the inputs or only the outputs; only that side is checked.

Original and adapted cases are kept apart: the fixtures hold the upstream SQL and expectation unchanged. The adaptation is
only in how a case is run and compared:

- one case is one single-model KumoSQL pipeline; table inputs and outputs come from its parsed statements, so multiple written targets are checked;
- DataHub schemas are handed over under the table spelling the query uses (DataHub keys them by full name and resolves
  unqualified names against `default_db`/`default_schema`);
- DataHub names a date shard or wildcard `table_yyyymmdd` and drops a partition decorator (`$...`): applied to both sides;
- OpenLineage calls an unnamed output `_0`; KumoSQL `_col_N`; both compare as "an expression";
- when a case supplies a default db or schema, table names match on the shorter name's dotted suffix.

## Outcomes

`exact`, `coarse` (no extra edge; a struct sub-field edge `a.b` is met only by an edge to its root column `a`), `finer`
(no extra edge; a golden edge to a struct's root column `a` is met by edges to the exact fields the SQL reads, `a.b`;
the golden is the coarser side), `unknown`
(KumoSQL said it could not trace something and claimed nothing wrong), `missed` (confident, but something expected is
absent), `wrong` (a table or edge is claimed that is not expected) and `disputed` (the oracle defines the answer differently;
each has one written reason in `DISPUTED`, and a test fails if one is used on a case that is not actually a mismatch).

Scope `in`: BigQuery (DataHub `dialect=bigquery`; OpenLineage `postgres`/`generic`/`bigquery`, which are plain ANSI here)
that parses as BigQuery and is not skipped upstream. Cases that are not BigQuery (`USE` state across statements, T-SQL
`SELECT INTO`, a table name hidden by its alias, Snowflake load statements) are listed in the tool output as left out.

## Baseline and what the first run found

First run, before any adapter fix: DataHub 7/18 exact with 3 wrong, OpenLineage 70/96 exact with 7 wrong, no product change
yet. Every one traced to the harness, not to KumoSQL: schemas keyed under two spellings made sqlglot report an ambiguous
table (DataHub), the DataHub shard and partition naming, OpenLineage's `_0` naming, two cases that are not BigQuery, and four
tests that treat an unused CTE as reading nothing. After those the in-scope numbers are in the scoreboard rows.

The four `disputed` cases: `WITH unused AS (SELECT * FROM users) SELECT ... FROM other`. OpenLineage reports data flow, so
`users` is not an input. KumoSQL lists every table the statement names, because dropping `users` still breaks the query.

Recorded on 2026-10-05 with sqlglot 30.21.0: **OpenLineage 90/94 exact, 4 disputed, 0 unknown, 0 missed, 0 wrong**;
**DataHub 17/18 exact plus 1 finer, 0 coarse, 0 unknown, 0 missed, 0 wrong** (18/18 matched).
Before field paths on `ColumnRef` (2026-10-03) DataHub was 15/18 exact plus 3 coarse: `test_select_with_full_col_name`,
`test_select_from_struct_subfields` and `test_select_struct_subfields_from_cte` expect an edge to `widget.asset.id` (and
`widget.metric.*`) and KumoSQL named only `widget`. KumoSQL now reads the fields off the expression that reads the struct
and names them, so those three are exact. OpenLineage is unchanged: it has no struct field case.

The one `finer` case, `test_join_struct_subfields_shared_base_name`, reads `a.widget.color` and `b.widget.size`; its golden
names the root column `widget` on each table, which is DataHub's own coarser answer (the golden for the same shape in the
three cases above names the field). KumoSQL names `widget.color` and `widget.size`. This is more precise than the golden,
not a disagreement, so it counts as matched but is kept apart from `exact`; the fixture is not edited. Because the DataHub
goldens are sqlglot's own output, this says nothing independent about which answer a user wants; the field is what the SQL reads.
The same-version baseline was OpenLineage 85/94 exact with 5 unknown, and DataHub 14/18 exact plus 3 coarse with
1 unknown. The recovered cases cover table rename, a DELETE without FROM, multi-table DROP, scripts with multiple
INSERT targets, and partition schema resolution. The fixtures and their expected answers are unchanged.

## Held out and limits

The other-dialect cases (109: Snowflake, MySQL, T-SQL and so on, read as BigQuery) were never adjudicated or used to
shape the adapter, so they are an unseen generalisation check; their wrong and missed counts are dialect differences and stay out of
the headline. The in-scope cases are not held out: each mismatch was read while building the adapter.

Current other-dialect run (the results file counts the 80 dialect cases, without the one case upstream skips): DataHub 41/81 exact, 1 coarse, 15 unknown, 16 missed, 8 wrong (one case moved from coarse to exact with the field paths; it was not tuned on); OpenLineage 14/46 exact,
0 unknown, 11 missed, 21 wrong. Scoping WITH names per reference (audit 1002 F11, `ast_utils.binding_cte`) moved one
OpenLineage case here from unknown to exact (13/46 before). It was not tuned on and not looked at while fixing; the
headline above does not change.

Table inputs and explicit outputs are checked for DELETE, UPDATE, MERGE (including a UNION source), ALTER, DROP,
TRUNCATE, CREATE LIKE/CLONE, and INSERT VALUES containing a scalar subquery. Every script write target is checked,
instead of inferring outputs from the final model name. A rename reads the old name and writes the new name; a DML
target alone is not counted as a table input. Partition-decorated names use the base table's schema.
These table matches do not establish complete column lineage for DML, copy statements or every script statement.
A STRUCT field read by a plain name chain (`rec.a`, `t.rec.a.b`) is traced to that field; a subscripted, called or whole-struct
read stays at the root column rather than a guessed field, and the rows-deciding columns of a model stay per root column
([field paths](../pipeline-analysis.md)). STRUCTs built inside a query can retain precise field lineage.
Unsupported column shapes continue to report unknown. On the older supported parser versions, syntax that cannot be
parsed stays outside the in-scope denominator, so the recorded score is tied to the stated parser version.
