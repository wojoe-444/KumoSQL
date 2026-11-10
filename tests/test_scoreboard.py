import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_scoreboard_is_current():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "scoreboard.py"), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_every_test_file_that_loads_an_eval_script_is_marked_eval():
    """A test that imports a script named by a results file's ``command`` holds that eval's floors, so it must be
    declare ``pytestmark = pytest.mark.eval`` (``pytest -m eval`` and ``tools/run_tests.py --evals`` select by it)."""

    import importlib.util
    import json
    import re

    spec = importlib.util.spec_from_file_location("eval_conftest", ROOT / "tests" / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    declared = conftest.eval_files()
    scripts = set()
    for path in (ROOT / "benchmarks" / "results").glob("*.json"):
        scripts.update(re.findall(r"tools/(\w+)\.py", json.loads(path.read_text(encoding="utf-8"))["command"]))
    missing = []
    for test in sorted((ROOT / "tests").glob("test_*.py")):
        text = test.read_text(encoding="utf-8")
        loads = [s for s in scripts if re.search(rf"(import {s}\b|from tools(\.{s})? import|[\"']{s}(\.py)?[\"'])", text) and re.search(rf"\b{s}\b", text)]
        if loads and test.name not in declared:
            missing.append(f"{test.name} (loads {', '.join(sorted(loads))})")
    assert not missing, "add `pytestmark = pytest.mark.eval` to: " + "; ".join(missing)


def _scoreboard():
    import importlib.util

    spec = importlib.util.spec_from_file_location("scoreboard_tool", ROOT / "tools" / "scoreboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_coverage_must_add_up_to_size_unless_it_says_what_it_counts():
    check = _scoreboard().check
    row = {"size": 10, "coverage": {"proven": 7, "unknown": 3}}
    assert check(row) is None
    assert check({"size": 10, "coverage": {}}) is None  # no such outcomes
    assert "adds up to 9, not size 10" in check({"size": 10, "coverage": {"proven": 7, "unknown": 2}})
    assert "adds up to 0" in check({"size": 10, "coverage": {"error": 0}})
    stage = {"size": 10, "coverage": {"proven": 7, "unknown": 3, "unsupported": 4}, "coverage_of": "All 14 cases: the 10 scored plus 4 skipped before translation."}
    assert check(stage) is None
    assert "coverage_of must be" in check({**stage, "coverage_of": " "})
    assert "whole numbers" in check({"size": 10, "coverage": {"proven": 10.0}})
    assert "unknown coverage outcome" in check({"size": 10, "coverage": {"passed": 10}})


def test_environment_is_optional_and_an_object():
    check = _scoreboard().check
    row = {"size": 1, "coverage": {"proven": 1}}
    assert check({**row, "environment": {"git_commit": "abc", "sqlglot": "30.21.0"}}) is None
    assert "environment must be an object" in check({**row, "environment": "sqlglot 30.21.0"})
