# Splitting the prover out

[Plain-language version](../docs_simple/prover-split.md)

Step 8 of the [roadmap](roadmap.md): the equivalence prover moves to its own repository, [`wojoe-444/Kumo_SQL_Prover`](https://github.com/wojoe-444/Kumo_SQL_Prover), with its tests, benchmark runners, results files and docs. KumoSQL then depends on a pinned release of it. Progress is tracked in [issue 839](https://github.com/wojoe-444/KumoSQL/issues/839).

## What the prover is

The prover is the import closure of these modules in `src/kumosql`: `equivalence`, `smt_equivalence`, `algebraic_equivalence`, `bounded_equivalence`, `result_equivalence`, `sqlsolver_backend`, `tie_determinism`, `tie_witness`, `uexpr`, `proof_registry` and `proof_format` (the roadmap's list), plus `canonical_rules` and `query_mutants`, two leaf modules that the prover's own benchmark runners and fuzzers import. Imports inside functions count; the walk is over the syntax tree, so nothing is executed.

```sh
python tools/prover_split.py            # the closure, with line counts
python tools/prover_split.py --list     # every module, and which ones the product also imports
python tools/prover_split.py --plan     # the tests and benchmark runners that move with it
python tools/prover_split.py --check    # exit 1 when the closure reaches a product-only module
```

`tests/test_prover_split.py` runs the check in the fast tier, so a new import from the prover into the product shows up in the pull request that adds it.

## Step 1: cutting the scripting edge

Before the split, `equivalence` imported `lift_subqueries`, which imported `engine`, which imported `scripts` (BigQuery scripting, about 2,800 lines) and `sqlx` (Dataform file parsing). That one chain put scripting, the rule registry and the Dataform parser inside the prover.

`engine.py` is now two modules:

| Module | Holds | In the prover |
| --- | --- | --- |
| `rule_driver` | `RewriteRule`, `RuleOutput`, `RuleDiagnostic`: the shared driver that handles SQLX sections, strict parsing, source-span preserving edits, no-ops and CTE dependency checks | yes |
| `engine` | The registry (`register_rule`, `get_rule`, `available_rules`), `lift_subqueries` registered first, and the hook that gives the driver BigQuery scripting | no |

`lift_subqueries` imports only the driver. The driver runs rules inside `BEGIN ... END` blocks only after `set_script_support` was called with the scripts module, which `kumosql.engine` does when KumoSQL is imported. Without it a script is parsed like any other text; the prover only ever receives one query, so its results do not change. Dataform's `sqlx.py` (337 lines, no imports of its own) stays in the prover: the verifier accepts SQLX files, masks `${...}` interpolations in them, and `sqlx_fragments` decides which ones a proof may treat as one opaque name.

After the cut the prover closure has 123 modules and about 55,000 lines (it was 122 modules and 57,800 lines when the roadmap was written, including `engine`, `scripts` and `sqlx`). 29 of those modules are also imported by 50 product modules, `ast_utils` most of all.

## Where the shared helpers live

Inside the prover package. A helper both sides use (`ast_utils`, `canonical`, `string_literals`, `type_names`, `sqlx` and the like) moves with the prover, and the product imports it from there. This is the simplest option: there is one copy, the dependency points one way (the product imports the prover, never the reverse), and a change to a shared helper is a prover release that KumoSQL takes as a version bump. A third "common" package would add a second release train for 29 modules.

## Limits

- The closure is computed from imports. A module the prover reaches only by a string (a plugin name, a subprocess command) is not found; `tests/test_prover_split.py` and the prover's own tests are what catch that.
- The roots are a choice. Adding a root grows the package; the two benchmark-support roots were added because the prover's own evals import them.
