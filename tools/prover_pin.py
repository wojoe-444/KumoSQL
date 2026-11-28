"""Is the installed equivalence prover the commit ``pyproject.toml`` pins? ``python tools/prover_pin.py``.

``pip install -e .`` does not replace a prover that is already installed when the pin moves, because every commit of
``kumo-sql-prover`` carries the same version string (docs/prover-split.md#the-pin-and-bumping-it). The tests then run
against an older prover and fail in ways that point nowhere near the cause. ``tools/run_tests.py`` and
``tests/test_prover_pin.py`` call :func:`mismatch`, which names both commits and the command that fixes it.

A prover installed from a local folder (``pip install -e path/to/Kumo_SQL_Prover`` while working on both) records no
commit and is not compared: :func:`mismatch` returns None and :func:`describe` says where it came from.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISTRIBUTION = "kumo-sql-prover"
_PIN = re.compile(r"^\s*kumo-sql-prover\s*@\s*(?P<url>git\+\S+?)@(?P<commit>[0-9a-fA-F]{7,40})\s*$")


def pinned(root: Path = ROOT) -> tuple[str, str] | None:
    """``(repository URL, commit)`` of the ``kumo-sql-prover`` requirement in ``pyproject.toml``, or None."""

    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for requirement in project.get("project", {}).get("dependencies", []):
        found = _PIN.match(requirement)
        if found:
            return found["url"].removeprefix("git+"), found["commit"].lower()
    return None


def installed() -> dict | None:
    """Where the installed prover came from: ``{"commit": sha or None, "source": text}``; None when it is not installed."""

    try:
        dist = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except (OSError, ValueError):
        direct = {}
    commit = (direct.get("vcs_info") or {}).get("commit_id")
    if commit:
        return {"commit": commit.lower(), "source": f"{direct.get('url', 'git')}@{commit[:12]}"}
    if (direct.get("dir_info") or {}).get("editable"):
        return {"commit": None, "source": f"an editable install of {direct.get('url', 'a local folder')}"}
    if direct.get("url"):
        return {"commit": None, "source": direct["url"]}
    return {"commit": None, "source": f"version {dist.version} with no recorded origin"}


def mismatch(root: Path = ROOT) -> str | None:
    """A message naming both commits and the fix when the installed prover is a different commit than the pin."""

    pin = pinned(root)
    if pin is None:
        return None
    url, commit = pin
    fix = f'python -m pip install --force-reinstall --no-deps "kumo-sql-prover @ git+{url}@{commit}"'
    have = installed()
    if have is None:
        return f"kumo-sql-prover is not installed; pyproject.toml pins {commit[:12]}. Install it with:\n  {fix}"
    if have["commit"] is None or have["commit"].startswith(commit) or commit.startswith(have["commit"]):
        return None
    return (
        f"the installed kumo-sql-prover is commit {have['commit'][:12]}, but pyproject.toml pins {commit[:12]}. "
        f"Tests would run against the wrong prover. Reinstall the pinned commit with:\n  {fix}\n"
        f"(the interpreter is {sys.executable}; see docs/prover-split.md#the-pin-and-bumping-it)"
    )


def describe(root: Path = ROOT) -> str:
    """One line for a run's header: the installed prover, and whether it matches the pin."""

    have = installed()
    pin = pinned(root)
    if have is None:
        return "kumo-sql-prover: not installed"
    if have["commit"] is None:
        return f"kumo-sql-prover: {have['source']} (not compared with the pin {pin[1][:12] if pin else 'in pyproject.toml'})"
    state = "matches the pin" if mismatch(root) is None else f"pin is {pin[1][:12]}" if pin else "no pin found"
    return f"kumo-sql-prover: {have['commit'][:12]} ({state})"


def main() -> int:
    problem = mismatch()
    print(problem or describe(), file=sys.stderr if problem else sys.stdout)
    return 1 if problem else 0


if __name__ == "__main__":
    raise SystemExit(main())
