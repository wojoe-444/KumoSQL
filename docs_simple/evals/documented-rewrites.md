# Checking rewrites that documentation recommends

[Simple eval index](README.md) · [Full reference](../../docs/evals/documented-rewrites.md)

Vendor and style-guide pages recommend many query rewrites and say or imply that the results stay the same. This evaluation turns 25 of those recommendations into KumoSQL's own small cases, labels each by hand, and checks the labels on DuckDB.

Most recommendations are safe (19 cases). Six change the results: for example, a tuning tip that turns `IN (subquery)` into a join can return a row twice. Those six must never be proved.

## How it decides

It works the same way as the [DB-GPT examples](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs_simple/evals/dbgpt-rules.md): the prover tries first, and then random DuckDB databases look for a difference, which counts only when a run with the optimizer off confirms it.

```sh
python tools/documented_rewrites_bench.py
```

Run from a development checkout. The full reference lists the scores and the cases that stay unknown. The cases were written with every case in view, so there is no held-out split.
