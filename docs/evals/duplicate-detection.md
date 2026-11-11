# Duplicate detection and shared-model extraction benchmark

[Plain-language version](../../docs_simple/evals/duplicate-detection.md)

`python tools/dup_bench.py` generates Dataform-style projects of a few to thousands of models around known families of SELECTs, runs KumoSQL's duplicate analyses on them and scores what they find. No language model is involved at any point: the cases come from a seeded Python generator, and the truth about every copy is known when it is written.

```
python tools/dup_bench.py                                   # 6, 60 and 600 models
python tools/dup_bench.py --sizes 6,60,600,3000 --seed 1 --json out.json
pytest tests/test_dup_bench.py                              # the floors, on 60 models
```

## What is generated

Every family is a base SELECT (a fact table, optionally joined to a dimension, with filters, an optional aggregate) written several ways:

| Kind | What changes | Truth |
| --- | --- | --- |
| `equiv_a` | table aliases, conjunct order, flipped comparisons (`5 < x`), `<>` against `!=`, `INNER JOIN`, `IN` list order, CTE name and where the copy sits (a CTE, a derived table or the whole model) | the same query |
| `equiv_b` | the above plus `IN` against `OR` of equalities, `BETWEEN` against two comparisons, `IFNULL` against `COALESCE` | the same query |
| `near_literal`, `near_filter`, `near_cols` | a different constant, an extra filter, an extra column | similar, not the same |
| `decoy_*` | `>` against `>=`, a different literal, `AND` against `OR`, a negated filter, `INNER` against `LEFT JOIN`, `SUM` against `AVG`, a dropped or added `DISTINCT` | deceptively similar, **not** the same |
| `decoy_upstream` | the same `agg` SELECT text over a CTE with a different body | not the same, and the SELECT cannot be shared |

Models also include readers downstream of the copies and unrelated noise models. Families whose number is divisible by 5 are held out: nothing was tuned on them and they are scored separately.

Labels are checked by execution: copies labelled the same query must return the same rows on six random DuckDB databases (150 rows, small value domains, NULLs), and a decoy that the random data cannot tell from the original is left out of the scores (`label_check` in the JSON).

## What is measured

Four things are kept apart:

1. **Correctness** (must be 0): `exact_wrong` is pairs offered as the same query that are not; `proof_ready_unsafe` is refactors marked ready whose edited models return different rows when run.
2. **Analysis quality**: pair precision and recall of exact duplicates (`duplicate_selects`, split into textual rewrites and semantic ones), of similar logic (exact duplicates plus near-duplicate clusters) and of the repeated-work report (`find_repeated_work`). A finding counts only at the copy's own place (or the whole query holding it).
3. **Usefulness**: families with at least two truly equal copies that have a refactor offered **and verified ready**.
4. **Performance**: seconds for each stage.

Evidence levels stay separate: a refactor is **ready** only on proof (the prover shows each edited model unchanged, or the copy is the shared SELECT itself after canonicalization). Independently the harness runs each refactor as it would ship (the shared SELECT as a table the edited model reads, in DuckDB) and counts agreeing and disagreeing proposals; that is executed agreement, not proof, and it never makes a refactor ready. Bounded verification is not used here.

## How KumoSQL answers

- **Exact duplicates** use a canonical text (`src/kumosql/canonical.py`): aliases renamed by position (correlated references included), `AND`/`OR` flattened and sorted, comparisons read `column OP constant`, `IN` lists sorted, `BETWEEN`, `IN` and `OR` of equalities agree, `IFNULL` is `COALESCE`, `INNER JOIN` is `JOIN`. A SELECT that reads a CTE defined outside it also hashes that CTE's content.
- **Near-duplicates** shingle the canonical text, require the same source tables and the same outside CTEs, and are compared in the centre's own aliases, so renamed copies still produce a shared SELECT with residual filters.
- **Refactors** (`propose_shared_logic`) are applied to every copy in memory (`refactor_sql`), then each edited model is checked with the prover and the readers downstream are `unchanged` when all models above them are proven.

## Bugs this found

- `near_duplicate_selects` raised an assertion error on any model that wraps a copy as `FROM (SELECT ...) AS s`; the whole analysis failed.
- Copies were compared by raw text, so a renamed alias or reordered conjunct hid a duplicate.
- The same SELECT text over two different CTEs was reported as a duplicate, and the refactor would have referenced a CTE the shared model cannot see.
- Near-duplicate clusters were never offered as refactors when copies used different aliases, and logic over different tables was clustered as a near-duplicate.
- A duplicate group was dropped when its copies sat inside larger duplicates that did not match each other, hiding the link between those duplicates.
- Guided refactors always read "Not ready": `ready` was fixed to false.

## Baseline (master before this work, seed 1, 60 to 600 models)

Measured with the same generator before any change: exact-duplicate textual recall 0% to 57% (any renamed alias hid a copy), near-duplicate analysis raised an error on any model that wraps a copy in a derived table, no near-duplicate refactor was ever offered for copies with different aliases, and every refactor was Not ready (`ready` was fixed to false). Cases are all generated here; none are adapted from a public suite, and no existing eval covers duplicate detection.

Every failure found is kept as a regression test: `tests/test_canonical.py`, `tests/test_near_duplicates.py`, `tests/test_shared_logic.py` and `tests/test_dup_bench.py`.

## Scores

Numbers are in the scoreboard page (`benchmarks/results/duplicate-*.json`, `shared-refactors-*.json`) and are regenerated with the command above.

Seed 1, 3,000 models (450 families, 2,173 copies), measured 2026-10-02:

| Measure | Dev families | Held-out families |
| --- | --- | --- |
| Exact duplicates: pairs offered that are not the same query | 0 | 0 |
| Exact duplicates: textual / semantic recall | 100% / 100% | 100% / 100% |
| Similar logic (exact + near-duplicates): precision / recall | 89.9% / 99.3% | 97.5% / 99.4% |
| Repeated-work report: precision / recall | 89.7% / 97.2% | 97.4% / 96.0% |

Refactors: 749 proposed, 714 ready on proof (0 unsafe), 749 agree when executed (0 disagree), and all 450 families with equal copies have a verified refactor. The 35 not ready are extra-filter extractions the prover cannot prove. A fresh seed (`--seed 2`, 600 models, not used to guide the fixes) gives 0 wrong, 100% exact recall, 99.6% recall and 92.1% precision for similar logic, 99.3% recall and 86.3% precision for near-duplicate pairs the exact check does not find, and 136 of 137 refactors ready (readiness also uses view merging: the shared SELECT merged back into a copy is the copy). Seed 1 refactor counts above predate that and had 35 not ready. Stage times at 3,000 models: exact 18 s, near-duplicates 77 s, repeated work 8 s, refactor verification 64 s.

Caveats: the rules were developed while reading this generator's misses, so seed 1 is tuned on test and the held-out families were not hidden while debugging; the fresh seed is the cleaner check. Similar logic from two generated families on the same table and join counts as a false pair, which lowers precision.

## Speed on large projects

`tests/test_analysis_scale.py` generates a 1,000-model Dataform project (`tools/make_dataform_fixture.py`) and holds each analysis stage (exact duplicates, near-duplicates, repeated work) to a time budget, so a change that scales badly fails the suite instead of hanging a real project. Run the same measurement on the 3,000-model fixture before merging any change to these stages. Fingerprints and canonical text are remembered on each SELECT, a level is copied without the queries nested in it, and near-duplicate candidates are checked for the same sources before their shingles are compared. On the 3,444-file fixture: exact duplicates 13 s, near-duplicates 18 s (was 29 s), repeated work 1 s.
