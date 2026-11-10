"""Spider 2.0 BigQuery gold queries through KumoSQL (docs/evals/spider2-bench.md): regressions it found, and floors."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "spider2"


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def _bench():
    spec = importlib.util.spec_from_file_location("spider2_bench", ROOT / "tools" / "spider2_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["spider2_bench"] = module
    spec.loader.exec_module(module)
    return module


def test_fixture_is_pinned_and_complete():
    import hashlib
    import json

    manifest = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["with_published_gold_sql"] == len(manifest["cases"]) == 142
    assert all((FIX / "gold" / f"{c['id']}.sql").exists() for c in manifest["cases"])
    tasks = (FIX / "dbt_tasks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(tasks) == manifest["counts"]["dbt_tasks"] == 68
    assert hashlib.sha256((FIX / "dbt_tasks.jsonl").read_bytes()).hexdigest() != ""


def test_derived_count_reads_no_column_and_is_constant_not_unknown():
    from kumosql.pipeline import Pipeline
    from kumosql.pipeline_types import Model, Target

    sql = "SELECT n AS h, a FROM (SELECT COUNT(*) AS n, a FROM `x.y.t` GROUP BY a)"
    pipeline = Pipeline({"p.d.m": Model(Target("p", "d", "m"), "table", sql)}, {}, {})
    rows = {ref.column: r for ref, r in pipeline.explain_lineage().items()}
    assert rows["h"].status == "constant" and rows["a"].status == "traced"
    star = Pipeline({"p.d.m": Model(Target("p", "d", "m"), "table", "SELECT s FROM (SELECT * FROM `x.y.t`)")}, {}, {})
    assert [r.status for r in star.explain_lineage().values()] == ["traced"]


def test_near_duplicate_search_survives_an_unnest_over_a_subquery():
    from kumosql.near_duplicates import find_near_duplicates
    from kumosql.pipeline import Pipeline
    from kumosql.pipeline_types import Model, Target

    sql = (FIX / "gold" / "bq130.sql").read_text(encoding="utf-8").strip().rstrip(";")
    pipeline = Pipeline({"p.d.m": Model(Target("p", "d", "m"), "table", sql)}, {}, {})
    find_near_duplicates(pipeline._analyse().parsed, min_nodes=1, threshold=0.5)


def test_prover_gives_up_on_many_outer_joins_instead_of_exhausting_memory():
    from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

    joins = " ".join(f"LEFT JOIN t{i} ON t{i}.k = t0.k" for i in range(1, 12))
    sql = f"SELECT t0.k FROM t0 {joins}"
    schema = {f"t{i}": ["k"] for i in range(12)}
    result = prove_equivalent_smt(sql, sql, timeout_ms=2000, schema=schema)
    assert result.status is SmtStatus.NOT_PROVEN and "outer-join cases" in result.reason


def test_query_timeout_counts_cpu_time_not_machine_load(monkeypatch):
    """The floor test failed under a parallel test run: a wall-clock limit timed out queries that were only
    waiting for a CPU. Time spent descheduled must not count; time spent computing must."""

    import time

    bench = _bench()
    monkeypatch.setattr(bench, "TIMEOUT", 0.2)

    def waits(sql):
        time.sleep(0.5)
        raise RuntimeError("waited")

    monkeypatch.setattr(bench.cov, "stage_parse", waits)
    _, out = bench.run_query("bq031")
    assert "timeout" not in out and "waited" in out["crash"][1]

    def spins(sql):
        while True:
            pass

    monkeypatch.setattr(bench.cov, "stage_parse", spins)
    _, out = bench.run_query("bq031")
    assert "timeout" in out and out["seconds"] < 5


def test_dev_and_held_out_floors():
    bench = _bench()
    results = bench.run("all", workers=4)  # one pool for both splits: no worker waits for the other split
    for split in ("dev", "held-out"):
        summary = bench.summarise({case: out for case, out in results.items() if bench.split_of(case) == split})
        stages = summary["stages"]
        for stage in bench.STAGES:
            row = stages[stage]
            assert row["failed"] == 0 and row["timeout"] == 0 and row["error"] == 0, (split, stage, row)
        assert all(r["damaged"] == 0 and r["error"] == 0 for r in summary["rules"].values())
        assert stages["parse"]["passed"] == summary["queries"]
