import re
import subprocess
import sys
from pathlib import Path

from kumosql.__main__ import COMMANDS, resolve

ROOT = Path(__file__).resolve().parents[1]


def test_commands_are_the_console_scripts_of_pyproject():
    """There is no second list: the commands are read from ``[project.scripts]``."""

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project.scripts]")[1].split("[build-system]")[0]
    declared = dict(re.findall(r'^([\w-]+) = "([^"]+)"', block, re.M))
    assert COMMANDS == declared and len(COMMANDS) > 20


def test_short_names_resolve():
    assert resolve("ui") == COMMANDS["kumosql-ui"]
    assert resolve("rewrite-sql") == COMMANDS["rewrite-sql"]
    assert resolve("nope") is None


def test_python_dash_m_runs_a_command():
    done = subprocess.run([sys.executable, "-m", "kumosql", "rewrite-sql", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "usage" in done.stdout.lower()
    done = subprocess.run([sys.executable, "-m", "kumosql", "bogus"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "unknown command" in done.stderr


def test_user_facing_text_never_tells_people_to_run_a_launcher():
    """Launchers (kumosql-ui.exe) are blocked or off PATH on locked-down machines: messages use python -m."""

    launchers = [name for name in COMMANDS if name != "kumosql-ui"] + ["kumosql-ui"]
    pattern = re.compile(r"(?<![\w.\-/<!\"'])(" + "|".join(re.escape(n) for n in launchers) + r")(?![\w\-\"'])")
    offenders = []
    for path in (ROOT / "src" / "kumosql").rglob("*"):
        if path.suffix not in (".py", ".js", ".html") or path.name == "__main__.py" or "vendor" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not any(ok in line for ok in ("STORAGE_KEY", "python -m", "<!--")):
                offenders.append(f"{path.name}:{number}: {line.strip()[:80]}")
    assert not offenders, offenders


def test_help_of_every_command_only_prints(tmp_path, monkeypatch, capsys):
    """--help exits 0 before any work: no file is created in the working folder or the data folder."""

    import pytest

    from kumosql.__main__ import main

    cwd, home = tmp_path / "cwd", tmp_path / "home"
    cwd.mkdir(), home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("KUMOSQL_HOME", str(home))
    for name in COMMANDS:
        try:
            code = main([name, "--help"])
        except SystemExit as stop:
            code = stop.code
        assert code in (0, None), name
        assert "usage" in capsys.readouterr().out.lower(), name
    assert list(cwd.iterdir()) == [] and list(home.iterdir()) == []


def test_top_level_usage_says_which_commands_can_write():
    from kumosql.__main__ import usage

    text = usage()
    assert "--help only prints text" in text
    assert "reduce-project" in text and "--write is the only option that edits a project folder" in text


def test_commands_that_can_edit_or_write_files_say_so_in_their_help():
    """The wording the permission checks and people read: preview by default, and the one option that writes."""

    def help_of(*args):
        done = subprocess.run([sys.executable, "-m", "kumosql", *args, "--help"], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, args
        return " ".join(done.stdout.split())

    assert "PREVIEW BY DEFAULT" in help_of("reduce-project") and "Only --write edits the project folder" in help_of("reduce-project")
    assert "READ-ONLY" in help_of("refactor") and "never edits the project folder" in help_of("refactor")
    assert "READ-ONLY" in help_of("minimize-tables")
    assert "never edits the project folder" in help_of("shared-model")
