"""The installed prover is the commit ``pyproject.toml`` pins, and the check that says so reads the pin correctly."""

import json
import sys
from pathlib import Path

import pytest

TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
sys.path.insert(0, TOOLS)
import prover_pin  # noqa: E402

sys.path.remove(TOOLS)

PIN = "a" * 40


def test_the_installed_prover_is_the_pinned_commit():
    problem = prover_pin.mismatch()
    assert problem is None, problem


def _project(tmp_path, requirement):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\ndependencies = [\n    "sqlglot==1",\n    {json.dumps(requirement)},\n]\n', encoding="utf-8")
    return tmp_path


def test_the_pin_is_read_from_the_dependency_line(tmp_path):
    root = _project(tmp_path, f"kumo-sql-prover @ git+https://github.com/o/Kumo_SQL_Prover@{PIN}")
    assert prover_pin.pinned(root) == ("https://github.com/o/Kumo_SQL_Prover", PIN)
    assert prover_pin.pinned(_project(tmp_path, "kumo-sql-prover==0.1.0")) is None


@pytest.mark.parametrize(
    ("have", "expected"),
    [
        ({"commit": PIN, "source": "git"}, None),
        ({"commit": PIN[:12], "source": "git"}, None),
        ({"commit": None, "source": "an editable install of file:///p"}, None),
    ],
)
def test_a_matching_or_local_prover_is_not_reported(tmp_path, monkeypatch, have, expected):
    monkeypatch.setattr(prover_pin, "installed", lambda: have)
    assert prover_pin.mismatch(_project(tmp_path, f"kumo-sql-prover @ git+https://h/r@{PIN}")) is expected


def test_another_commit_is_reported_with_both_commits_and_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(prover_pin, "installed", lambda: {"commit": "b" * 40, "source": "git"})
    problem = prover_pin.mismatch(_project(tmp_path, f"kumo-sql-prover @ git+https://h/r@{PIN}"))
    assert "bbbbbbbbbbbb" in problem and "aaaaaaaaaaaa" in problem
    assert f'pip install --force-reinstall --no-deps "kumo-sql-prover @ git+https://h/r@{PIN}"' in problem


def test_a_missing_prover_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(prover_pin, "installed", lambda: None)
    assert "not installed" in prover_pin.mismatch(_project(tmp_path, f"kumo-sql-prover @ git+https://h/r@{PIN}"))
