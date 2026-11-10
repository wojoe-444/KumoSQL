# KumoSQL evaluation integrity audit

> Received 2026-10-03 from an external audit of commit `b7d7dc6` (2026-09-29), kept as written apart from repository-relative links (line numbers in the original links pointed at that older commit). What each finding means on current master, and what was fixed, is in [Eval integrity audit status](eval-integrity-status.md).

The current numbers cannot establish rewrite correctness or generalization. The audit reproduced an SMT proof that ignores a table snapshot and collisions in the execution comparator. The fixture and fuzz gates also give substantially more credit than their apparent coverage suggests. A meaning-changing static proof was reproduced in the baseline and corrected by concurrent work before this audit finished.

This is an audit of commit `b7d7dc6` and the working tree on October 2, 2026. The pre-existing edits to `src/kumosql/rewrite.py` and `tests/test_cleanup_rules.py` were included in the baseline test run and left intact. Implementation changes and other audit artifacts appeared concurrently and were left intact. A final focused recheck confirmed that the name-collision reproduction now preserves rows and the formerly failing credential test passes. The remaining fast reproductions still hold. Baseline evidence and the final recheck are saved separately, with source hashes for the latter. The final full suite was not rerun after concurrent changes. This audit adds this report, JSON evidence, and a reproduction script; it does not change implementation or existing tests.

No standalone leaderboard, train/test split, or protected held-out corpus was found. The evaluated mechanisms are the workbook fixture gate, authored equivalence pairs, independent execution checks, SMT differential fuzzing, and CI selection. No evidence of deliberate cheating or rules branching on benchmark query text was found in the transformation and verifier modules. The findings concern what these checks can establish.

Evidence is saved in `eval-integrity-evidence-2026-10-02.json` (not committed; it describes commit `b7d7dc6`). Reproduce it with:

```powershell
python tools/audit_eval_integrity.py --fuzz --historical --output build/eval-integrity-reproduction.json
```

The reproduction script records observations and returns JSON; it is not a release gate. The saved evidence also records the separately run full pytest suite.

## Observed results

| Check | Observed result | What the number establishes |
| --- | --- | --- |
| Current fixture | 32 successful rows; 41 lifts across 31 rows | Parsed FROM/JOIN subqueries were removed according to the lifter's own counter |
| Current fixture through the verified API | 29 proven, 1 unchanged, 2 unproven | The structural gate's 32 successes are not 32 verified rewrites |
| Historical fixture from `99f5c0d^` | 533 successful rows; 354 rows changed; 58 rows used input recovery; 45 used output recovery | Historical structural coverage still passes, with substantial recovery and no-op credit |
| SMT differential fuzzing | 133 proofs, 83 counterexamples, 34 unresolved pairs | 106 of the 133 proofs were identical input strings; only 27 proofs changed text |
| Baseline full suite including slow tests | 300 passed, 1 failed | The failure depends on ambient Google credentials |
| Credential test after concurrent patch | 1 passed | The specific ambient-credential failure is addressed |

Versions: Python 3.12.10, sqlglot 28.10.0, DuckDB 1.5.1, Z3 solver 5.1.0.0, SQLFluff 4.3.0, pytest 9.0.2. The CI sqlglot matrix versions were not replayed locally. No live BigQuery validation was performed; GoogleSQL documentation was used to check the dialect rules relevant to the findings.

## Findings affecting correctness scores

### Circular verification can approve a changed result

**P1 at baseline; reproduced case addressed during the audit.** The baseline [lift_subqueries.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/lift_subqueries.py) reserved existing root CTE names, but did not reserve physical relation names. It could create a CTE that shadows a table already read elsewhere in the query. [equivalence.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/equivalence.py) normalizes both sides using that same lifter, so the verifier repeated the transformation's mistake and approved it.

```sql
SELECT a.x AS ax, b.x AS bx
FROM (SELECT 1 AS x) a
JOIN __lifted_subquery_001 b ON TRUE
```

With one physical table row `x = 7`, the original returns `(1, 7)`. At baseline, lifting created `WITH __lifted_subquery_001 AS (SELECT 1 AS x)` and the result became `(1, 1)`. Both `prove_equivalent` and `apply_rule` approved the rewrite; the latter reported `success=True` and `verification=proven`. SQLite independently demonstrated the differing rows. The DuckDB harness also found a difference on empty input.

The concurrent patch reserves visible relation names and now chooses `__lifted_subquery_002`. The final recheck returns `(1, 7)` on both sides and conservatively marks the rewrite unproven. This resolves the reproduced capture. The broader need for an independent oracle remains: both sides still pass through the lifter during static normalization.

This is valid GoogleSQL name shadowing: a WITH name hides an unqualified permanent table with the same name. See [GoogleSQL CTE visibility](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#cte_name).

Keep the reproduced collision as a mandatory regression, and test changed queries against an oracle that does not normalize them using the transformation under test. Similar scope movement needs separate coverage for references to outer queries and nested WITH scopes.

### SMT proofs discard snapshot semantics

**P1.** [smt_equivalence.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/smt_equivalence.py) builds a relation identity from a table's name and ignores its version modifier. The prover reports `proven_equivalent` for:

```sql
SELECT a FROM t
FOR SYSTEM_TIME AS OF TIMESTAMP '2026-10-01 00:00:00+00'
```

compared with `SELECT a FROM t`. These queries can read different rows after a table change. [GoogleSQL time travel semantics](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#for_system_time_as_of) explicitly describe the historical definition and rows. This difference is not covered by the prover's stated assumptions about NaN, errors, arithmetic, or output types.

Reject unmodeled table modifiers, or include the snapshot expression in the modeled relation identity. The audit also tried differently cased UDF names as a control; those returned `not_proven` and are not a finding.

### Execution comparison merges different values

**P1.** [result_equivalence.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/result_equivalence.py) loses type and shape information before building result multisets:

- Python equality makes `True` and `1` the same Counter key. `SELECT TRUE AS x` and `SELECT 1 AS x` are reported equivalent on all eight default seeds.
- NaN is encoded as `("NaN",)`, which collides with a normalized array containing the string `"NaN"`.
- A structure `{"a": 1}` and an array shaped like `[["a", 1]]` normalize to the same tuple.
- The default rounds floats to 12 significant digits. `CAST(1.0000000000001 AS FLOAT64)` and `CAST(1.0000000000002 AS FLOAT64)` are reported equivalent.

Agreement is already documented as evidence rather than proof, but an oracle should still distinguish the values it actually observed. Preserve scalar types, arrays, structures, and NaN with separate tagged encodings. Make approximate float comparison an explicit policy recorded in results, with exact comparison available for correctness gates.

### Fixture success accepts invalid inputs and unverified outputs

**P1.** [test_workbook_fixture.py](../tests/test_workbook_fixture.py) only checks `lift_subqueries(...).success`. [LiftResult.success](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/lift_subqueries.py) means zero counted relational subqueries and no diagnostic from a selected fatal-code list. It does not check semantics, column binding, or BigQuery validity.

The checked-in fixture includes two invalid cases:

- `q09` selects `customer_id` from the CTE `regions`, whose only output is `region`.
- `q21` puts HAVING on an outer query that has neither grouping nor aggregation. The inner aggregate does not aggregate the outer query. See [GoogleSQL HAVING requirements](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#having_clause).

DuckDB rejects both originals before comparing any rewrite. Both nevertheless earn structural success and a static proof. These are parser examples, not valid semantic benchmark cases.

The gate also counts `q16` as successful even though its MERGE USING subquery is unchanged: `_is_relation_subquery` only recognizes FROM/JOIN parents. `q17` and `q20` earn structural success but the verified API declines their rewrites.

Recovery contributes additional false confidence. [rule_driver.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/src/kumo_sql_prover/rule_driver.py) uses parser recovery, and recovery diagnostics are not fatal. `SELECT 1 FROM t WHERE 1 =` is returned unchanged with `success=True`. An input ending in `AS q garbage extra` is transformed after discarding the extra text and also reports success. The historical fixture earns success for 58 input-recovery rows, including 45 output-recovery rows.

Validate and label fixture inputs, require a real change for transformation cases, and report strict parsing, recovery, syntactic removal, schema validity, and semantic verification separately. Keep unsupported cases in the denominator with their outcome recorded.

### Fixture overrides can reduce the denominator to zero

**P1.** [test_workbook_fixture.py](../tests/test_workbook_fixture.py) skips missing paths. Its required row count and coverage floors apply only when no override is set. With `KUMOSQL_TEST_FIXTURE` set, all of these pass:

```json
[]
```

```json
[{"id": "1"}]
```

```json
[{"id": "1", "sql_text": ""}]
```

`processed == len(rows)` is tautological after iterating every row. Missing SQL is coerced to empty text, which the lifter treats as success. A mistyped path skips the only fixture test instead of rejecting the intended evaluation.

Require a nonempty, schema-validated manifest with unique IDs, source hash, declared case count, and explicit expected outcomes. An explicitly requested missing fixture should fail.

## Findings affecting coverage and score comparability

### The benchmark population changed substantially

**P2.** Git history documents 621 supplied workbook rows becoming 533 token-pattern representatives in `00119cb`, then 32 authored samples in `99f5c0d`. The final corpus is about 5.2% of the initial row count and has different provenance. README changes acknowledge these replacements, so the reduction was not hidden in the repository. Nevertheless, percentages across these populations cannot establish improvement without a common manifest.

The current [sanitizer](../tools/sanitize_fixture.py) erases identifier and literal values in its deduplication key. It gives the same pattern to `SELECT a FROM t WHERE a = 1` and `SELECT a FROM t WHERE b = 2`, despite different reference relationships and predicate values. It reports retained rows without reporting the number or identity of discarded rows. Case-folding table and dataset names can also change relationships in case-sensitive datasets; see [GoogleSQL identifier case rules](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/lexical#case_sensitivity).

The historical replay passed all 533 structural cases, so this audit does not establish that replacement concealed existing failures. It establishes lost coverage and incompatible denominators. Restore a frozen evaluation population or version each corpus explicitly, retaining original counts and exclusion reasons.

### Default and CI runs omit the public fixture

**P2.** [pyproject.toml](../pyproject.toml) deselects slow tests by default. [tests.yml](../.github/workflows/tests.yml) explicitly excludes the fixture, with an outdated comment claiming it needs a private CSV. The current fixture is checked in and public. The ordinary shape test only checks fixture structure and uniqueness.

Run the authored fixture in required CI and put any genuinely large external corpus in a separate mandatory evaluation job when making benchmark claims. Record selections and deselections alongside the number of cases.

### Fuzz proof floors mostly reward identity pairs

**P2.** [test_smt_fuzz.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/tests/test_smt_fuzz.py) uses text replacements that often do not change the query. Of 133 proofs in the observed run, 106 were identical strings and 27 changed text. Identity proofs alone satisfy the `> 50` floor. The count is therefore not a floor on nontrivial rewrite coverage.

The same RNG generates query pairs and validation databases. Only proven pairs consume 25 subsequent database draws. Solver outcome changes therefore change later query pairs, even with the same seed. Omitting proof-driven validation draws changes 247 of 250 pair positions. A fixed seed does not freeze this benchmark across implementations or timeouts.

The 34 unresolved pairs receive no independent truth label. The test appropriately checks accepted proofs and emitted counterexamples, but its status counts cannot be interpreted as accuracy or recall.

Freeze pair generation before invoking the prover, use separate RNGs for queries and data, save the pair manifest, and require per-family coverage of changed positive pairs and labeled negatives. Keep identity pairs as a separate sanity check.

### Some independent checks can pass with no coverage

**P2.** [test_result_equivalence.py](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/tests/test_result_equivalence.py) only executes pairs when the static prover reports success. Replacing its prover with one that declines every pair leaves the test passing. This is a reasonable conditional soundness test, but it supplies no coverage floor and cannot demonstrate that any proof was independently checked.

Several execution and SMT modules use `pytest.importorskip`, making local suite counts dependency-dependent. CI installs the relevant extras, which reduces this issue there. Record checked pairs, unproven pairs, skipped modules, and required dependencies explicitly; assert an expected checked set where the test intends to guarantee coverage.

The synthetic generator also uses fixed small domains. `WHERE a = 1000` and `WHERE FALSE` agree on every default seed because 1000 is never generated. More seeds cannot fix that omission. Add values drawn from query constants, boundary cases, and independently generated challenge data. The harness correctly documents finite agreement as evidence; a scoreboard must preserve that qualification.

### Credential state makes a test environment dependent

**P2 at baseline; credential isolation addressed during the audit.** The baseline [test_dryrun.py](../tests/test_dryrun.py) removed three environment variables and assumed no credentials remained. [dryrun.py](../src/kumosql/dryrun.py) also discovers Application Default Credentials. On this machine, the full suite found ambient ADC and failed with a fake-transport `KeyError` instead of the expected missing-credentials exception. This was the sole failure in 301 tests. The concurrent patch blocks auth imports in this test, and a focused rerun passed in 2.01 seconds.

Most dependency versions still have lower bounds without a lock, including Z3 and SQLFluff. The sqlglot CI matrix is pinned, but solver outcomes and default runtime dependencies can still differ. Record exact versions and timeout settings with benchmark outputs and separate compatibility testing from a fixed evaluation environment.

## Recommended order of repair

1. Reject unmodeled SMT table snapshots and repair typed result comparison. Retain the corrected static name-shadowing case and the other reproduced false positives as mandatory negative tests.
2. Validate fixture inputs and manifests, require the public fixture in CI, and make malformed, missing, empty, and recovered cases explicit outcomes.
3. Freeze the evaluation corpus, eliminate proof-driven RNG coupling, and measure changed pairs by feature family with independent labels and execution.
4. Introduce a protected challenge set with separate authorship and access from rule development. Split by query family and workload source, not only by raw text; deduplicate before splitting while preserving reference relationships. Keep the current authored tests as development regressions.
5. Publish each result with corpus and code hashes, exact versions, expected and observed counts, identity/change counts, skips, recovery counts, unresolved outcomes, and the comparison policy. Preserve the full expected denominator for the headline score.

These changes would make future score increases interpretable. The present suite provides useful regression coverage, but its apparent pass rate should not be presented as a semantic correctness rate.
