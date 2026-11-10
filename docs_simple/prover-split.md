# Splitting the prover out

[All simple guides](README.md) · [Full reference](../docs/prover-split.md)

The part of KumoSQL that proves two queries return the same rows is large, slow to test and changes for different reasons than the part that reads Dataform projects. The plan is to give it its own repository, [Kumo_SQL_Prover](https://github.com/wojoe-444/Kumo_SQL_Prover), and have KumoSQL use a fixed released version of it.

## What was done first

The prover used to reach, through one chain of imports, code that has nothing to do with proving: BigQuery scripting and the registry of rewrite rules. That chain was cut. You can check it any time:

```sh
python tools/prover_split.py --check
```

It prints how many modules the prover is made of and exits with an error if the prover starts depending on the product again.

## An example

`lift_subqueries` turns `FROM (SELECT ...) AS t` into a named `WITH` entry. The prover uses it to normalize a query before comparing. Before the cut, using it also loaded the script parser. Now the prover uses only the shared driver and the script parser is loaded only when KumoSQL itself is.

## Limits

- The list of modules is computed from imports in the source, not from running anything.
- Which scores move to the new repository, and how the scoreboard shows them, is described in the [full page](../docs/prover-split.md) as each step lands.
