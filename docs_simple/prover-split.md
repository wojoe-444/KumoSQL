# Splitting the prover out

[All simple guides](README.md) · [Full reference](../docs/prover-split.md)

The part of KumoSQL that proves two queries return the same rows was large, slow to test and changed for different reasons than the part that reads Dataform projects. It now has its own repository, [Kumo_SQL_Prover](https://github.com/wojoe-444/Kumo_SQL_Prover), and KumoSQL uses a fixed released version of it (the commit released as `v0.1.0`, named in `pyproject.toml`).

## What this means in practice

- **Changing the prover** happens in the prover repository, with its own tests and its own benchmark scores. KumoSQL picks the change up only when someone raises the version it names.
- **Changing KumoSQL** no longer runs the prover's benchmarks. KumoSQL keeps a small set of real prover cases (the canary, a few seconds) that proves the two are still wired together.
- **Your code does not need to change.** `from kumosql.equivalence import prove_equivalent` still works; it gives you the very same module as `from kumo_sql_prover.equivalence import prove_equivalent`.

## Check it

```sh
python tools/prover_split.py --check
```

It prints how many modules the prover is made of and exits with an error if the prover starts depending on KumoSQL, or reaches into parts of it that should stay outside (scripting, the rule registry).

## An example

`lift_subqueries` turns `FROM (SELECT ...) AS t` into a named `WITH` entry; the prover uses it to normalize a query before comparing. It is written once, in the prover. KumoSQL's `lift-subqueries` command and its rewrite pipeline call the same code through the same module.

## Limits

- Some benchmark code the prover's evals share with KumoSQL's proof re-check is still copied in both repositories, and the evals that drive KumoSQL's own rewrites stayed. The [full page](../docs/prover-split.md#not-moved-yet) lists them.
- "No score moved" was checked by running the moved evals in both places and comparing answers; the full page says which ones.

After this change, run `pip install -e .` once in an existing checkout so the prover is installed next to KumoSQL.
