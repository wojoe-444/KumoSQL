"""Helpers shared by the eval scripts in tools/.

Each eval owns ``benchmarks/results/<eval>.json`` (format in benchmarks/README.md), and the README
scoreboard is generated from those files by ``tools/scoreboard.py``. Import from a script with::

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bench_common import quiet, today, write_results
"""

from __future__ import annotations

import datetime
import json
import logging
import importlib.metadata
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "benchmarks" / "results"


def quiet() -> None:
    """Command-line runs print results, not per-stage timings or sqlglot warnings.

    Call it from ``main`` rather than at import, so tests that import a bench are unaffected.
    """

    os.environ.setdefault("KUMOSQL_TIMING", "0")
    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)


def today() -> str:
    """The ``date`` a results file records: the day the numbers were measured."""

    return datetime.date.today().isoformat()


_PACKAGES = ("sqlglot", "sqlglotc", "duckdb", "z3-solver", "sqlfluff")


def environment() -> dict:
    """The code and library versions a run measured, so scores from different setups aren't compared blindly.

    Solver outcomes depend on the z3 version and timeouts, and parsing on the sqlglot version.
    """

    versions = {"python": platform.python_version()}
    for package in _PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        versions["commit"] = commit + ("+changes" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        pass
    return versions


def write_results(name: str, row: dict, *, scoreboard: bool = True) -> Path:
    """Write ``benchmarks/results/<name>.json`` (with :func:`environment`) and regenerate the scoreboard page (docs/scoreboard.md)."""

    row = {**row, "environment": row.get("environment") or environment()}
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    if scoreboard:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import scoreboard as board

        board.main([])
    return path
