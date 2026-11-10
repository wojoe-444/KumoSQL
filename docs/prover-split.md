# Splitting the prover out

[Plain-language version](../docs_simple/prover-split.md)

Step 8 of the [roadmap](roadmap.md): the equivalence prover lives in its own repository, [`wojoe-444/Kumo_SQL_Prover`](https://github.com/wojoe-444/Kumo_SQL_Prover), with its tests, benchmark runners, results files and docs. KumoSQL depends on a pinned release of it (`kumo-sql-prover @ git+https://github.com/wojoe-444/Kumo_SQL_Prover@cf1f74a...` in `pyproject.toml`: the commit released as v0.1.0), so a prover change reaches KumoSQL only as a bump of that pin. The pin names the commit because the `v0.1.0` tag could not be created from the session that merged the prover; once the tag exists the pin can read `@v0.1.0`. Progress is tracked in [issue 839](https://github.com/wojoe-444/KumoSQL/issues/839).

## What the prover is

The prover is the import closure of these modules: `equivalence`, `smt_equivalence`, `algebraic_equivalence`, `bounded_equivalence`, `result_equivalence`, `sqlsolver_backend`, `tie_determinism`, `tie_witness`, `uexpr`, `proof_registry` and `proof_format` (the roadmap's list), plus four small modules: `canonical_rules` and `query_mutants`, leaves that the prover's own benchmark runners and fuzzers import, `verdict_cache`, the cache of proof verdicts for the benchmark runs (it hashes the prover's own source, and no prover module imports it, so it is a root), and `bigquery_syntax`, which teaches sqlglot's BigQuery parser the syntax it lacks (`TABLE` arguments, `LIKE ALL UNNEST`, the `WITH(a AS 1, a + 1)` expression) the moment it is imported. That last one is not reached by any import from the roots, but every parse the prover made relied on it because `import kumosql` loads it first; a prover installed alone would have read the same query differently, so it is a root and the package imports it first. Imports inside functions count; the walk is over the syntax tree, so nothing is executed.

That is 137 modules and 59,490 lines (the roadmap counted 122 and 57,800 before the scripting edge was cut). 31 of them were also imported by 52 product modules, `ast_utils` most of all.

```sh
python tools/prover_split.py            # the closure of the installed prover, with line counts
python tools/prover_split.py --list     # every module
python tools/prover_split.py --check    # exit 1 when the closure reaches a product module, or the package imports kumosql
```

`tests/test_prover_split.py` runs the check in the fast tier. Before the split the same script, run on `src/kumosql`, also printed what moved (`--plan`) and a `git filter-repo` paths file (`--paths`); both need the pre-split tree, which `git show 7011437d:src/kumosql` still has.

## What moved

| What | Moved | Now |
| --- | --- | --- |
| Source modules | 137 modules, 59,490 lines | `src/kumo_sql_prover/` in the prover repository |
| Test files | 150 of 379 | `tests/` there (plus the docs, scoreboard, eval-diff and canary tests, copied) |
| Benchmark runners (`tools/`) | 40 runners; the 8 only the prover uses are gone from here | `tools/` there. The other 32 are imported by KumoSQL's proof re-check (and a few by staying evals), so a copy stays here; see below |
| Results files | 50 of 126 | `benchmarks/results/` there; the scoreboard of those evals is generated there |
| Docs | 10 feature pages and 15 eval pages, each with its plain-language companion | `docs/` and `docs_simple/` there |
| Fixtures | the corpora only the moved tests read | `tests/fixtures/` there; fixtures both sides read are copied |

The history of every moved file came along: the new repository was made with `git filter-repo --paths-from-file` on a full clone, with `src/kumosql/` renamed to `src/kumo_sql_prover/`, so `git log --follow` on a prover file runs back to its first commit in KumoSQL. The first commit on top of that history renames the package in imports and docstrings and adds the packaging.

## Where the shared helpers live

Inside the prover package. A helper both sides use (`ast_utils`, `canonical`, `string_literals`, `type_names`, `sqlx` and the like) moved with the prover, and the product imports it from there. This is the simplest option: there is one copy, the dependency points one way (the product imports the prover, never the reverse), and a change to a shared helper is a prover release that KumoSQL takes as a version bump. A third "common" package would add a second release train for 30 modules.

## Package name and import path

The distribution is `kumo-sql-prover` and the import name `kumo_sql_prover`. KumoSQL's own modules import it by that name (122 import statements were rewritten mechanically).

Everything that used to be `kumosql.<name>` for a moved module still works, and is the same module object: `import kumosql.equivalence` returns `kumo_sql_prover.equivalence`. A small import hook (`kumosql/_prover_aliases.py`, installed first thing in `kumosql/__init__.py`) answers for the 132 top-level modules and packages that moved, and for nothing else. Why this rather than moving the import path or keeping a re-export file per module:

- About 50 product modules, 227 test files and any outside code import the old paths, and the tests monkeypatch module attributes by dotted name (`monkeypatch.setattr("kumosql.smt_equivalence.f", ...)`). One module object means a patch through either spelling is seen by both.
- A re-export file per module would copy names, leave the private ones behind (the product's own tests reach into them), and be 137 files to keep in step.
- A namespace package shared by two distributions does not work with `kumosql/__init__.py`, which is a regular package, and breaks editable installs.

The hook has a price: `python -m kumosql.<moved name>` does not work (use `python -m kumo_sql_prover.<name>`, or `python -m kumosql <command>` for the console commands), and `tests/test_prover_aliases.py` pins the list to what the installed package contains, so a module added to the prover must be added to `MOVED` in the same bump. `kumosql.lift_subqueries` stays the function it always was (the package attribute is not rebound to the module).

## The pin, and bumping it

`pyproject.toml` pins a release. To take a prover change: release it in the prover repository (a `vX.Y.Z` tag, with release notes saying which evals moved and the commit tested), change the pin here, and run the routine tier plus the canary. A KumoSQL pull request therefore cannot change a prover benchmark: none of the prover's evals, floors or results files live here.

After the switch, an existing checkout needs `pip install -e .` once more: it installs the pinned prover next to KumoSQL. To work on both at once, clone the prover and `pip install -e path/to/Kumo_SQL_Prover` after that; the import aliases follow whatever is installed, and `tests/test_prover_aliases.py` fails when the list of moved modules no longer matches it.

## Keeping the prover repository in step

While a prover change can still land here (a rule, a fix, a floor), `tools/prover_resync.py FROM TO PROVER_CHECKOUT` carries it over: it lists what changed between two KumoSQL commits in the files that live in the prover, writes the patch with the package renamed, and with `--apply` applies it there; `--verify` compares every moved source file, test, runner and results file at a commit with the prover checkout and names any that differ other than on purpose (the pytest gate rules, runner options). A hunk that does not apply is left in a `.rej` file for a hand edit. After the carry, the prover repository's pull request records its gate, the floors of the evals it moved and the comparison of the changed ones, as for the first one, and KumoSQL bumps its pin.

## Canaries

`tests/test_prover_canary.py` keeps real pairs from the SQLSolver suites that the algebraic prover proves (14 each from Calcite, TPC-C and Spark SQL, 8 from TPC-H, which has 22 pairs in all) and four pairs the suites must never prove, from `tests/fixtures/prover_canary.json`. They run in seconds through the entry points KumoSQL imports. `tests/test_canaries.py` (the gate's canaries, ten cases each of the SQLSolver suites, QED and the mined Calcite set) stays here too and calls the runners through their copies in `tools/` (`sqlsolver_bench`, `qed_bench`, `calcite_mined_bench`); the prover repository carries the same file. A failure here means the wiring broke (the pin, the import paths, the parser extension); a score that drops is for the prover repository's scheduled run to report.

## Not moved yet

- **Proof re-check.** `tools/proof_recheck.py` and `tools/recheck/` hunt for wrong proofs among the pairs the evals count as proven, and some adapters need KumoSQL itself (rewrites, containment, pipelines). They stay, with copies of the 32 prover runners they import; the next step is to split the adapters so the prover owns its own re-check and these copies can go.
- **Evals that drive KumoSQL's rewrites** stay with KumoSQL even though they use the prover: `unsafe_fuzz` and `compose_fuzz`, the targeted-data evals (`targeted_data_bench` imports `unsafe_fuzz`), rule-level fuzzing, the numeric assumption report (it imports a rewriting benchmark) and every rewrite, pipeline and lineage eval.
- **The GoogleSQL evaluator, the DuckDB translation used by the product (`bigquery_duckdb`) and type inference** were not in the roadmap's roots and stay.

## Limits

- The closure is computed from imports. A module the prover reaches only by a string (a plugin name, a subprocess command) is not found; the prover's own tests and `tests/test_prover_aliases.py` are what catch that.
- The roots are a choice. Adding a root grows the package.
- Import-time side effects are invisible to an import walk. `bigquery_syntax` is the one known today (it patches sqlglot); a new module that patches a library when imported must be added as a root by hand.
- "No score moved" rests on the comparison recorded in the pull request that switched KumoSQL over (the commands of the moved evals run in both trees, answers compared) and on the prover repository's floors. It covers the evals that were run, not every case of every eval.
