"""Reused execution evidence must never suppress changed inputs or new proof checks."""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval


@pytest.fixture
def bench(monkeypatch, tmp_path):
    import sys

    path = Path(__file__).resolve().parents[1] / "tools/sqlsolver_bench.py"
    spec = importlib.util.spec_from_file_location("execution_cache_bench", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setenv("KUMOSQL_EVAL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(module, "targeted_differ", lambda *args: None)
    return module


class CountingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.queries = 0

    def execute(self, sql, *args):
        if sql.startswith("SELECT a"):
            self.queries += 1
        return self.connection.execute(sql, *args)

    def executemany(self, *args):
        return self.connection.executemany(*args)


def check(bench, left="SELECT a FROM t", right="SELECT a FROM t", *, seed=11, trials=8, columns=None):
    tables = {"t": bench.Table("t", columns or [bench.Column("a", "INTEGER")])}
    connection = bench.new_database(tables)
    db = CountingConnection(connection)
    try:
        result = bench.differ(left, right, tables, db, trials=trials, seed=seed, cache_random=True)
        return result, db.queries
    finally:
        connection.close()


def test_warm_cache_skips_samples_but_runs_targeted_check(bench, monkeypatch):
    assert check(bench)[1] > 0
    calls = []
    monkeypatch.setattr(bench, "targeted_differ", lambda *args: calls.append(args) or "new counterexample")
    result, queries = check(bench)
    assert result == "new counterexample"
    assert queries == 0
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["query", "seed", "trials", "schema", "engine", "source"])
def test_changed_execution_inputs_miss(bench, monkeypatch, tmp_path, change):
    if change == "source":
        source = tmp_path / "sqlsolver_bench.py"
        source.write_bytes(Path(bench.__file__).read_bytes())
        (tmp_path / "bench_sql_repairs.py").write_text("# source fixture\n")
        monkeypatch.setattr(bench, "__file__", str(source))
    assert check(bench)[1] > 0
    assert check(bench)[1] == 0
    args = {}
    if change == "query":
        args = {"left": "SELECT a + 0 FROM t", "right": "SELECT a + 0 FROM t"}
    elif change == "seed":
        args = {"seed": 19}
    elif change == "trials":
        args = {"trials": 9}
    elif change == "schema":
        args = {"columns": [bench.Column("a", "INTEGER", not_null=True)]}
    elif change == "engine":
        import duckdb
        monkeypatch.setattr(duckdb, "__version__", "changed-engine")
    else:
        source.write_text(source.read_text() + "\n# changed checker\n")
    assert check(bench, **args)[1] > 0


def test_counterexample_and_execution_error_are_not_cached(bench, tmp_path):
    for _ in range(2):
        result, queries = check(bench, right="SELECT a + 1 FROM t")
        assert result is not None and result is not False
        assert queries > 0
        assert check(bench, right="SELECT absent FROM t")[0] is False
    assert not list((tmp_path / "cache").glob("*.json"))


def test_corrupt_and_unwritable_cache_fall_back(bench, tmp_path, monkeypatch):
    assert check(bench)[1] > 0
    for path in (tmp_path / "cache").glob("*.json"):
        path.write_text("broken json")
    assert check(bench)[1] > 0
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    monkeypatch.setenv("KUMOSQL_EVAL_CACHE", str(blocked))
    assert check(bench)[1] > 0


def test_prover_and_excluded_pair_guard_still_run_on_warm_cache(bench, monkeypatch):
    calls = []
    monkeypatch.setattr(bench, "load_schema", lambda *args: {"t": bench.Table("t", [bench.Column("a", "INTEGER")])})
    monkeypatch.setattr(bench, "load_pairs", lambda *args: [("SELECT a FROM t", "SELECT a FROM t")])
    monkeypatch.setattr(bench, "must_not_prove", lambda *args: {})
    def prove(*args):
        calls.append(args)
        return True
    for _ in range(2):
        result = bench.run_suite("spark", prove, trials=8)
        assert result.proved == 1 and not result.wrong
    assert len(calls) == 2
    monkeypatch.setattr(bench, "must_not_prove", lambda *args: {0: "must stay unproven"})
    assert bench.run_suite("spark", prove, trials=8).wrong


def test_default_without_cache_executes_again(bench, monkeypatch):
    monkeypatch.delenv("KUMOSQL_EVAL_CACHE")
    assert check(bench)[1] > 0
    assert check(bench)[1] > 0


def test_old_evidence_is_rechecked(bench, tmp_path):
    import os
    import time

    assert check(bench)[1] > 0
    assert check(bench)[1] == 0
    old = time.time() - 8 * 86400
    for path in (tmp_path / "cache").glob("*.json"):
        os.utime(path, (old, old))
    assert check(bench)[1] > 0
