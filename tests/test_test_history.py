"""The test-history recorder and its report (tools/test_history.py)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import test_history as th  # noqa: E402


def record(label, failed, *, targets=("tests/test_a.py",), branch="feature", source="changed-tests", commit="c1", files=None, **extra):
    """A run record; ``failed`` is a list of (test id, inside the targets)."""

    return {
        "v": 1, "ts": extra.pop("ts", "2026-10-02T10:00:00Z"), "commit": commit, "branch": branch, "dirty": False, "label": label,
        "targets": list(targets), "targets_source": source, "mode": "full",
        "failed": [{"id": nodeid, "kind": "failed", "msg": "boom", "target": inside} for nodeid, inside in failed],
        "files": files or {"tests/test_a.py": 3, "tests/test_b.py": 3}, "slow": extra.pop("slow", {}), **extra,
    }


def test_targets_match_files_ids_and_parametrisations():
    targets = ["tests/test_a.py", "tests/test_b.py::test_x", "tests/sub/"]
    assert th.is_target("tests/test_a.py::test_one", targets)
    assert th.is_target("tests/test_b.py::test_x[3]", targets)
    assert th.is_target("tests/test_b.py::test_x", targets)
    assert th.is_target("tests/sub/test_c.py::test_y", targets)
    assert not th.is_target("tests/test_b.py::test_xy", targets)
    assert not th.is_target("tests/test_ab.py::test_one", ["tests/test_a.py"])


def test_a_failure_outside_the_targets_of_an_otherwise_passing_change_is_collateral():
    records = [
        record("fix A", [("tests/test_b.py::query_12", False)]),
        record("fix B", [("tests/test_b.py::query_12", False), ("tests/test_a.py::mine", True)]),  # a target failed: not collateral
        record("fix C", [("tests/test_b.py::query_12", False)], branch="other"),
        record("on master", [("tests/test_b.py::query_12", False)], branch="master"),  # master was already red
        record("no targets", [("tests/test_b.py::query_12", False)], targets=(), source="none"),
    ]
    found = th.collateral_counts(records)
    assert found["tests/test_b.py::query_12"]["runs"] == 2
    assert found["tests/test_b.py::query_12"]["labels"] == {"fix A", "fix C"}
    counts = th.failure_counts(records)["tests/test_b.py::query_12"]
    assert (counts["failed"], counts["outside"], counts["master"], counts["unattributed"]) == (5, 3, 1, 1)
    assert counts["runs"] == 5


def test_environment_sized_failures_stay_out_of_the_rankings():
    many = [(f"tests/test_b.py::t{i}", False) for i in range(th.MASS_FAILURE)]
    records = [record("no z3", many), record("fix", [("tests/test_b.py::t0", False)])]
    assert th.collateral_counts(records)["tests/test_b.py::t0"]["runs"] == 1
    assert th.failure_counts(records)["tests/test_b.py::t0"]["failed"] == 1


def test_a_test_that_fails_and_passes_at_one_commit_is_flaky():
    failing = record("one", [("tests/test_b.py::slow", False)], commit="abc12345")
    passing = record("two", [], commit="abc12345")
    other = record("three", [("tests/test_b.py::real", False)], commit="def")
    flaky = th.flaky_candidates([failing, passing, other])
    assert [(n, f, p) for n, f, p, _ in flaky] == [("tests/test_b.py::slow", 1, 1)]


def test_order_lists_slow_tests_with_medians_and_often_failing_tests():
    records = [
        record("a", [("tests/test_b.py::flaky", False)], slow={"tests/test_b.py::big": 40.0}),
        record("b", [("tests/test_b.py::flaky", False), ("tests/test_b.py::other", False)], slow={"tests/test_b.py::big": 60.0}),
    ]
    order = th.build_order(records)
    assert order["slow"] == {"tests/test_b.py::big": 50.0}
    assert order["risky"][0] == "tests/test_b.py::flaky"


def test_a_test_that_was_slow_in_a_few_runs_of_its_file_stays_in_the_quick_tier():
    # recorded only when slow: 'spike' was 4 s on a loaded machine in 2 of 10 runs; 'usual' is slow in 6 of 10; 'rare_big' is
    # 40 s in 2 of 10 (a long test never counts as fast); 'new' was slow in the only run that has it
    records = [
        record(f"r{i}", [], slow={
            **({"tests/test_b.py::spike": 4.0} if i < 2 else {}),
            **({"tests/test_b.py::usual": 5.0} if i < 6 else {}),
            **({"tests/test_b.py::rare_big": 40.0} if i < 2 else {}),
        })
        for i in range(10)
    ]
    records.append(record("n", [], files={"tests/test_c.py": 1}, slow={"tests/test_c.py::new": 4.0}))
    assert th.slow_tests(records) == {"tests/test_b.py::rare_big": 40.0, "tests/test_b.py::usual": 5.0, "tests/test_c.py::new": 4.0}


def test_order_leaves_out_tests_whose_files_are_not_in_this_checkout(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_b.py").write_text("")
    records = [record("a", [("tests/test_new.py::t", False), ("tests/test_b.py::t", False)], slow={"tests/test_new.py::big": 9.0, "tests/test_b.py::big": 5.0})]
    order = th.build_order(records, root=tmp_path)
    assert order["slow"] == {"tests/test_b.py::big": 5.0} and order["risky"] == ["tests/test_b.py::t"]


def timed(label, ts, mode="full", cores=4, files=None, **extra):
    return record(label, [], ts=ts, mode=mode, workers=4, machine={"cpus": cores}, seconds=600.0, cpu_seconds=2000.0, file_seconds=files or {}, **extra)


def test_trend_compares_file_times_across_like_runs():
    records = [
        timed("old", "2026-10-01T10:00:00Z", files={"tests/test_a.py": [100.0, 98.0], "tests/test_b.py": [10.0, 9.0]}),
        timed("other machine", "2026-10-01T12:00:00Z", cores=16, files={"tests/test_a.py": [5.0, 5.0]}),
        timed("partial", "2026-10-01T13:00:00Z", mode="partial", files={"tests/test_a.py": [1.0, 1.0]}),
        timed("new", "2026-10-02T10:00:00Z", files={"tests/test_a.py": [40.0, 39.0], "tests/test_b.py": [11.0, 10.0]}),
    ]
    changes = {name: (then, now, runs) for name, then, now, runs in th.file_changes(records)}
    assert changes == {"tests/test_a.py": (100.0, 40.0, 2), "tests/test_b.py": (10.0, 11.0, 2)}
    found = th.history_of(records, "test_a.py")
    assert [(r["label"], wall, cpu) for r, _, wall, cpu in found] == [("old", 100.0, 98.0), ("other machine", 5.0, 5.0), ("partial", 1.0, 1.0), ("new", 40.0, 39.0)]
    slow = record("before times", [], slow={"tests/test_a.py::big": 30.0, "tests/test_a.py::small": 4.0})
    assert th.file_times(slow) == {"tests/test_a.py": (34.0, None)}  # older records: the slow tests' sum, no CPU
    assert [(wall, cpu) for _, _, wall, cpu in th.history_of([slow], "tests/test_a.py::big")] == [(30.0, None)]


def test_trend_prints_whole_runs_and_where_the_time_goes(tmp_path, capsys):
    for index, (label, wall) in enumerate([("first", 100.0), ("second", 40.0)]):
        saved = timed(label, f"2026-10-0{index + 1}T10:00:00Z", files={"tests/test_a.py": [wall, wall], "tests/test_b.py": [1.0, 1.0]}, test_cpu_seconds=wall + 1)
        th.write_record(saved, tmp_path)
    assert th.main(["trend", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "10m00s" in out and "33m20s" in out  # wall and CPU of each whole run
    assert "tests/test_a.py  100.0  40.0  -60.0" in out
    assert th.main(["trend", "--dir", str(tmp_path), "--test", "test_b.py"]) == 0
    assert capsys.readouterr().out.count("tests/test_b.py") == 2


def test_a_junit_file_becomes_a_record(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite timestamp="2026-10-02T20:00:00.5">'
        '<testcase classname="tests.test_a" name="test_ok" time="0.1"/>'
        '<testcase classname="tests.test_a.TestK" name="test_slow[a.b]" time="5.0"/>'
        '<testcase classname="tests.test_a" name="test_bad" time="0.2"><failure message="assert 1 == 2&#10;more"/></testcase>'
        '<testcase classname="tests.test_a" name="test_skip" time="0"><skipped type="pytest.xfail" message="xfail"/></testcase>'
        "</testsuite></testsuites>"
    )
    result = th.record_from_junit(junit, commit="abc", branch="master", label="seed", root=tmp_path)
    assert result["counts"] == {"passed": 2, "failed": 1, "xfailed": 1}
    assert result["failed"][0]["id"] == "tests/test_a.py::test_bad"
    assert result["failed"][0]["msg"] == "assert 1 == 2"
    assert result["slow"] == {"tests/test_a.py::TestK::test_slow[a.b]": 5.0}
    assert result["ts"] == "2026-10-02T20:00:00Z"


def children_cpu() -> float | None:
    try:
        import resource
    except ImportError:  # Windows
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


@pytest.mark.parametrize("workers", [1, 2])
def test_a_pytest_run_writes_a_record_with_targets_and_collateral(tmp_path, workers):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    (project / "tests" / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT / 'tools')!r})\nimport test_history\n\ntest_history.SLOW_SECONDS = 0.25\n\n\ndef pytest_configure(config):\n    test_history.install(config)\n"
    )
    (project / "tests" / "test_mine.py").write_text("def test_target():\n    assert True\n")
    (project / "tests" / "test_fixture.py").write_text(
        "import time\n\nimport pytest\n\n\n@pytest.fixture(scope='module')\ndef built():\n    time.sleep(0.3)\n    return 1\n\n\ndef test_uses_it(built):\n    assert built\n"
    )
    (project / "tests" / "test_other.py").write_text("def test_query_12():\n    assert 1 == 2\n\n\ndef test_fine():\n    pass\n")
    history = tmp_path / "history"
    env = {**os.environ, "KUMOSQL_TEST_HISTORY": str(history), "KUMOSQL_TEST_TARGETS": "tests/test_mine.py", "KUMOSQL_TASK": "my task"}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(["-n", str(workers), "--dist", "loadgroup"] if workers > 1 else [])]
    before = children_cpu()
    done = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True)
    spent = None if before is None else children_cpu() - before  # the pytest run and every process it started
    assert done.returncode == 1, done.stdout + done.stderr
    assert "outside the targets: tests/test_other.py::test_query_12" in done.stdout
    (path,) = list((history / "runs").glob("*.jsonl"))
    saved = json.loads(path.read_text())
    assert saved["label"] == "my task" and saved["targets"] == ["tests/test_mine.py"] and saved["targets_source"] == "declared"
    assert saved["counts"] == {"passed": 3, "failed": 1}
    assert [(f["id"], f["target"]) for f in saved["failed"]] == [("tests/test_other.py::test_query_12", False)]
    assert saved["files"] == {"tests/test_fixture.py": 1, "tests/test_mine.py": 1, "tests/test_other.py": 2}
    # a test is timed whole: a slow shared fixture makes the test that built it slow, and is recorded as a slow setup
    assert list(saved["slow_setup"]) == ["tests/test_fixture.py::test_uses_it"] and "tests/test_fixture.py::test_uses_it" in saved["slow"]
    assert th.working_change_run(saved) or saved["branch"] in ("master", "main")  # on a feature branch this is collateral
    # the times: every file's wall and CPU, the run's CPU across the workers, and the machine
    assert set(saved["file_seconds"]) == {"tests/test_fixture.py", "tests/test_mine.py", "tests/test_other.py"}
    assert all(wall >= 0 and cpu >= 0 for wall, cpu in saved["file_seconds"].values())
    assert saved["cpu_seconds"] >= saved["test_cpu_seconds"] >= 0 and saved["test_seconds"] >= 0
    assert saved["cpu_seconds"] > 0  # start-up and collection count too
    # every process of the run counted once: the workers are also finished children of the controller
    assert spent is None or spent * 0.5 <= saved["cpu_seconds"] <= spent * 1.1 + 0.2, (saved["cpu_seconds"], spent)
    assert saved["machine"]["cpus"] == os.cpu_count() and saved["v"] == 3
    assert any(line.startswith("test history: recorded to") and ", CPU " in line for line in done.stdout.splitlines())


def test_recording_is_off_without_a_history_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_TEST_HISTORY", "off")
    assert th.history_dir() is None
    monkeypatch.setenv("KUMOSQL_TEST_HISTORY", str(tmp_path / "h"))
    assert th.history_dir() == tmp_path / "h"
    assert th.load_records(tmp_path / "missing") == []


def test_order_runs_files_with_a_slow_shared_fixture_together_and_lists_the_files_it_has_seen(tmp_path):
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py", "test_c.py"):
        (tmp_path / "tests" / name).write_text("")
    records = [
        record("a", [], files={"tests/test_a.py": 4, "tests/test_b.py": 2}, slow={"tests/test_b.py::x": 25.0}, slow_setup={"tests/test_b.py::x": 23.0},
               file_seconds={"tests/test_a.py": [1.0, 1.0], "tests/test_b.py": [30.0, 29.0]}),
        record("b", [], files={"tests/test_a.py": 4, "tests/test_b.py": 2, "tests/test_gone.py": 1}, file_seconds={"tests/test_b.py": [50.0, 49.0]}),
    ]
    order = th.build_order(records, root=tmp_path)
    assert order["together"] == {"tests/test_b.py": 40.0}  # the whole file's median
    assert order["files"] == ["tests/test_a.py", "tests/test_b.py"]  # test_c.py has never run; test_gone.py is not here


def test_the_order_file_is_valid_and_names_real_test_files():
    order = json.loads((ROOT / "tests" / "order.json").read_text())
    assert set(order) >= {"slow", "risky", "runs", "slow_seconds", "together", "files"}
    files = {nodeid.split("::")[0] for nodeid in [*order["slow"], *order["risky"]]} | set(order["together"]) | set(order["files"])
    assert all((ROOT / name).is_file() for name in files), sorted(f for f in files if not (ROOT / f).is_file())


def test_routine_profile_keeps_full_and_eval_profiles_available(monkeypatch):
    import prover_pin
    import run_tests
    commands = []
    monkeypatch.setattr(run_tests, "_compiled", lambda: False)
    monkeypatch.setattr(run_tests.subprocess, "call", lambda command, **kwargs: commands.append(command) or 0)
    monkeypatch.setattr(prover_pin, "mismatch", lambda root=None: None)
    for flags in (["--routine"], [], ["--evals"]):
        assert run_tests.main([*flags, "-j", "1"]) == 0
    routine, full, evals = commands
    assert routine[routine.index("-m", 2) + 1] == "not eval and not slow"
    assert "--quick" in routine
    assert full[full.index("-m", 2) + 1] == "not slow"
    assert "--quick" not in full
    assert evals[evals.index("-m", 2) + 1] == "eval and not slow"
    assert "--quick" not in evals


def test_a_sqlglotc_hidden_by_a_second_sqlglot_is_named_and_reinstalled_beside_the_one_in_use(monkeypatch, capsys, tmp_path):
    import prover_pin
    import run_tests
    commands = []
    monkeypatch.setattr(run_tests, "_compiled", lambda: False)
    monkeypatch.setattr(run_tests, "_sqlglotc_elsewhere", lambda: tmp_path / "other-site-packages")
    monkeypatch.setattr(run_tests.subprocess, "call", lambda command, **kwargs: commands.append(command) or 0)
    monkeypatch.setattr(prover_pin, "mismatch", lambda root=None: None)
    assert run_tests.main(["--install-compiled", "-j", "1"]) == 0
    from importlib.metadata import version

    install, pytest_run = commands
    assert install[2:5] == ["pip", "install", f"sqlglotc=={version('sqlglot')}"]
    assert "--force-reinstall" in install and "--no-deps" in install
    assert "-m" in pytest_run and "pytest" in pytest_run
    assert "sqlglotc is installed in" in capsys.readouterr().err


def test_a_prover_that_is_not_the_pin_stops_the_run_before_pytest(monkeypatch, capsys):
    import prover_pin
    import run_tests
    commands = []
    monkeypatch.setattr(run_tests, "_compiled", lambda: False)
    monkeypatch.setattr(run_tests.subprocess, "call", lambda command, **kwargs: commands.append(command) or 0)
    monkeypatch.setattr(prover_pin, "mismatch", lambda root=None: "the installed kumo-sql-prover is commit bbb, but pyproject.toml pins aaa")
    assert run_tests.main(["--gate", "-j", "1"]) == run_tests.PROVER_EXIT
    assert commands == [] and "commit bbb, but pyproject.toml pins aaa" in capsys.readouterr().err
    assert run_tests.main(["--gate", "-j", "1", "--any-prover"]) == 0
    assert len(commands) == 1 and "WARNING: the installed kumo-sql-prover" in capsys.readouterr().err
