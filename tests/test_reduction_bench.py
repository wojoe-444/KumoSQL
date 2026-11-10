"""Project-reduction eval: the converted projects are faithful, the checker catches wrong patches, and the floor holds."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mc = _load("minimization_cases")
bench = _load("reduction_bench")

CASES = [c for c in mc.load_cases() if not mc.sourced(c)]  # adapted cases are scored apart
DEV = [c for c in CASES if c["split"] == "dev"]

# Floors on every 18th dev case (15 cases; see floor_cases), measured at 14 reduced, 27.2% of the
# complexity removed (8.8% by dropping alone) and 49 of 67 steps proved.
FLOOR_REDUCED = 13
FLOOR_REMOVED_SHARE = 0.25
FLOOR_STEPS_PROVED = 44
# Real-project cases the floor runs, all on the dev split: each is reduced and re-proved.
REAL_FLOOR = ["real-wintermi-imdb-season_ratings", "real-wintermi-fashion-product", "real-wintermi-bqe-WAITFOR"]


def floor_cases():
    return DEV[::18]


def test_every_case_renders_and_compiles_back_to_its_own_tables(tmp_path):
    features = set()
    for case in CASES:
        files, meta = bench.render(case)
        features |= set(meta["features"])
        assert all(not path.startswith("/") and ".." not in path for path in files)
    assert features == {"assert_kept", "assert_inner", "config_assertion", "vars", "incremental", "dependency", "views"}
    for case in DEV[:40:4]:
        files, _meta = bench.render(case)
        root = tmp_path / case["id"]
        bench.write_files(root, files)
        compiled = bench.compile_project(root)
        tables = {k: v for k, v in compiled.items() if not k.startswith("assert_")}
        assert set(tables) == set(case["tables"])
        status, reason, _proofs = bench.mb.check_output(case, tables, databases=10, prove=False)
        assert status in ("same", "agreed"), (case["id"], reason)


def test_the_checker_counts_a_trap_patch_wrong(tmp_path):
    traps = [c for c in DEV if c.get("traps")][:6]
    assert len(traps) == 6
    for case in traps:
        files, _meta = bench.render(case)
        original = tmp_path / case["id"] / "original"
        bench.write_files(original, files)
        trap = {**case, "tables": case["traps"][0]["tables"]}
        trap_files, _ = bench.render(trap)
        reduced = tmp_path / case["id"] / "reduced"
        bench.write_files(reduced, trap_files)
        status, reason, _o, _r = bench.check_project(case, original, reduced, databases=10)
        assert status == "wrong", case["id"]
    # so is one that lost a kept output
    shutil.rmtree(reduced / "definitions" / "reports")
    assert bench.check_project(case, original, reduced, databases=5)[0] == "wrong"


def test_converted_floor():
    results = bench.run([("converted", c) for c in floor_cases()], databases=30)
    summary = bench.summarise(results)["converted"]
    assert summary["status"]["wrong"] == 0, summary["wrong"]
    assert summary["status"]["error"] == summary["status"]["unverified"] == 0, summary["errors"]
    assert summary["improved"] >= FLOOR_REDUCED
    assert summary["complexity"]["removed_share"] >= FLOOR_REMOVED_SHARE
    assert summary["steps"]["proved"] >= FLOOR_STEPS_PROVED


def test_real_projects_floor():
    cases = {c["id"]: c for c in bench.real_cases()}
    assert all(cases[name]["split"] == "dev" for name in REAL_FLOOR)
    results = bench.run([("real", cases[name]) for name in REAL_FLOOR])
    for result in results:
        assert result.status == "reduced" and result.verified, (result.id, result.reason)
        assert result.score_after < result.score_before


def test_jaffle_shop_floor():
    # a real dbt project with its seed data: the reduction is executed on DuckDB as well as proved
    case = next(c for c in bench.jaffle_cases() if c["id"] == "jaffle-orders")
    result = bench.run([("jaffle", case)], databases=20)[0]
    assert result.status == "reduced" and result.verified and result.executed == "agreed", result.reason
    assert result.score_after < result.graph_only_score < result.score_before


def test_results_file_matches_the_case_set():
    row = json.loads((ROOT / "benchmarks" / "results" / "project-reduction.json").read_text(encoding="utf-8"))
    real = [c for c in bench.real_cases() if c["split"] == "dev"]
    jaffle = [c for c in bench.jaffle_cases() if c["split"] == "dev"]
    assert row["size"] == len(DEV) + len(real) + len(jaffle)
