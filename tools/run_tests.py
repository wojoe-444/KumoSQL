"""Run the test suite in parallel with pytest-xdist: ``python tools/run_tests.py [--evals | --no-evals] [pytest args]``.

* no flag: the whole fast suite (``-m "not slow"``);
* ``--evals``: only the benchmark floors (tests marked ``eval``);
* ``--no-evals``: everything except the floors;
* ``--routine``: quick non-eval regression suite; run affected evals separately;
* ``--gate``: the pull-request gate: the routine suite held to a time budget (``--budget``, default 300 s). A run that takes
  longer fails, so the gate cannot grow back unnoticed; move the slow test out of the gate or make it smaller;
* ``--quick``: skip the slow tier (a few minutes instead of the whole run; ``tests/order.json`` lists the slow tests);
* ``--label TEXT`` and ``--target PATH`` (repeatable): what this run is for, written to the shared test history
  (``tools/test_history.py``) so a later report can tell a failure inside your targets from one outside them. Without
  ``--target`` the test files your branch changed are the targets.

sqlglot runs compiled when ``sqlglotc`` (mypyc wheels, same version as ``sqlglot``) is installed, which is
about 10-15% faster on the prover tests; ``--install-compiled`` installs it beside the sqlglot Python imports
(when a second copy of sqlglot hides an installed sqlglotc, the run says so, and ``--install-compiled`` reinstalls
it there). ``--pure`` runs the same suite against a pure-Python copy for focused diagnostics. Routine CI and Dell full-suite runs use compiled
SQLGlot 30.21.0 once per revision; older versions are no longer supported.
KumoSQL itself is always pure Python.

Before pytest starts, the installed ``kumo-sql-prover`` is compared with the commit ``pyproject.toml`` pins
(``tools/prover_pin.py``). A different commit stops the run with one message that names both commits and the
reinstall command (exit code 4), instead of a scatter of failures from the wrong prover; ``--any-prover`` runs anyway.
A prover installed from a local folder (``pip install -e``) is not compared.

Workers default to the number of CPUs (``-j N`` to change it, ``-j 1`` for a plain serial run). Any other
argument goes to pytest, e.g. ``python tools/run_tests.py tests/test_tags.py -k background``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE_BUDGET_SECONDS = 300  # roadmap step 2: the pull-request check finishes in five minutes on four cores
SLOWEST_SHOWN = 12
BUDGET_EXIT = 3
PROVER_EXIT = 4


def _sqlglot_dir() -> Path:
    spec = importlib.util.find_spec("sqlglot")
    assert spec and spec.submodule_search_locations
    return Path(next(iter(spec.submodule_search_locations)))


def _compiled() -> bool:
    return any(_sqlglot_dir().rglob("*.so")) or any(_sqlglot_dir().rglob("*.pyd"))


def _sqlglotc_elsewhere() -> Path | None:
    """The site-packages folder of an installed sqlglotc whose compiled files are not beside the sqlglot Python imports.

    Two copies of sqlglot (one in the user's site-packages, one in Python's own) leave sqlglotc's files under the copy
    that is not imported, so the run is pure Python although ``pip`` says sqlglotc is installed."""

    from importlib import metadata

    try:
        located = Path(metadata.distribution("sqlglotc").locate_file(""))
    except metadata.PackageNotFoundError:
        return None
    return None if located.resolve() == _sqlglot_dir().parent.resolve() else located


def _install_compiled_command() -> list[str]:
    """``pip install`` of the sqlglotc that matches the imported sqlglot, into the folder that sqlglot is in."""

    import site
    from importlib.metadata import version

    command = [sys.executable, "-m", "pip", "install", f"sqlglotc=={version('sqlglot')}"]
    if _sqlglotc_elsewhere() is not None:
        command += ["--force-reinstall", "--no-deps"]  # pip counts the copy beside the other sqlglot as installed
    user_site = site.getusersitepackages() if site.ENABLE_USER_SITE else None
    if user_site and _sqlglot_dir().parent.resolve() == Path(user_site).resolve():
        command.append("--user")
    return command


def _pure_copy() -> Path:
    """A directory holding sqlglot's .py files only (no compiled extensions), for PYTHONPATH."""

    from importlib.metadata import version

    target = Path(tempfile.gettempdir()) / f"kumosql-pure-sqlglot-{version('sqlglot')}"
    if not (target / "sqlglot").is_dir():
        staging = target.with_name(target.name + f".{os.getpid()}")
        shutil.copytree(_sqlglot_dir(), staging / "sqlglot", ignore=shutil.ignore_patterns("*.so", "*.pyd", "__pycache__"))
        try:
            staging.rename(target)
        except OSError:  # another run made it first
            shutil.rmtree(staging, ignore_errors=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--routine", action="store_true", help="quick non-eval regression suite; affected evals remain required")
    group.add_argument("--evals", action="store_true", help="only the benchmark floors")
    group.add_argument("--no-evals", action="store_true", help="everything except the benchmark floors")
    group.add_argument("--gate", action="store_true", help="the pull-request gate: --routine held to a time budget (default 300 s, see --budget)")
    parser.add_argument("--budget", type=float, metavar="SECONDS", help="fail the run if it takes longer than this many seconds of wall time (the default for --gate is %d)" % GATE_BUDGET_SECONDS)
    parser.add_argument("--quick", action="store_true", help="skip the slow tier of tests (see tests/order.json)")
    parser.add_argument("--label", help="what this run is for (default: the branch name); recorded in the test history")
    parser.add_argument("--target", action="append", default=[], help="a test file or test id this run is aiming at (repeatable); recorded in the test history")
    parser.add_argument("--pure", action="store_true", help="test against pure-Python sqlglot even if sqlglotc is installed")
    parser.add_argument("--install-compiled", action="store_true", help="pip install the sqlglotc that matches the installed sqlglot first")
    parser.add_argument("--eval-cache", metavar="PATH", help="local SQLSolver sample-execution cache directory; 'off' forces fresh checks")
    parser.add_argument("--verdict-cache", metavar="PATH", help="remember proof verdicts of the benchmark runs in this folder, keyed by the query pair and the prover's source ('off' disables)")
    parser.add_argument("--eval-jobs", type=int, help="processes for the large MV workload eval (default: spare CPUs, capped at 3; 1 forces serial)")
    parser.add_argument("--any-prover", action="store_true", help="run even when the installed kumo-sql-prover is not the commit pyproject.toml pins")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="worker processes (default: all CPUs)")
    args, rest = parser.parse_known_args(argv)

    import prover_pin

    problem = prover_pin.mismatch()
    if problem and not args.any_prover:
        print(f"FAILED before running any test: {problem}\n(--any-prover runs the tests against it anyway)", file=sys.stderr)
        return PROVER_EXIT
    print(f"WARNING: {problem}" if problem else prover_pin.describe(), file=sys.stderr)

    if args.install_compiled and not _compiled():
        subprocess.call(_install_compiled_command())
    env = dict(os.environ)
    if args.eval_jobs is not None and args.eval_jobs < 1:
        parser.error("--eval-jobs must be positive")
    spare = max(1, (os.cpu_count() or 1) - max(1, args.jobs) + 1)
    env["KUMOSQL_EVAL_JOBS"] = str(args.eval_jobs or min(3, spare))
    if args.eval_cache is not None:
        env["KUMOSQL_EVAL_CACHE"] = args.eval_cache
    if args.verdict_cache is not None:
        env["KUMOSQL_VERDICT_CACHE"] = args.verdict_cache
    if args.label:
        env["KUMOSQL_TASK"] = args.label
    if args.target:
        env["KUMOSQL_TEST_TARGETS"] = ",".join(args.target)
    if args.pure:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_pure_copy()), env.get("PYTHONPATH")]))
        print("sqlglot: pure Python", file=sys.stderr)
    elif _compiled():
        print("sqlglot: compiled (sqlglotc)", file=sys.stderr)
    elif (elsewhere := _sqlglotc_elsewhere()) is not None:
        print(f"sqlglot: pure Python: sqlglotc is installed in {elsewhere}, but Python imports sqlglot from {_sqlglot_dir()}, "
              "which has no compiled files (python tools/run_tests.py --install-compiled puts them beside it)", file=sys.stderr)
    else:
        print("sqlglot: pure Python (python tools/run_tests.py --install-compiled makes the suite ~10-15% faster)", file=sys.stderr)

    marker = "not slow"
    if args.evals:
        marker = "eval and not slow"
    elif args.no_evals or args.routine or args.gate:
        marker = "not eval and not slow"
    command = [sys.executable, "-m", "pytest", "-m", marker, "-q", *(["--quick"] if args.quick or args.routine or args.gate else []), *rest]
    if args.jobs > 1:
        if importlib.util.find_spec("xdist") is None:
            print("pytest-xdist is not installed (pip install -e '.[dev]'); running serially", file=sys.stderr)
        else:
            command += ["-n", str(args.jobs), "--dist", "loadgroup"]
    budget = args.budget if args.budget is not None else (GATE_BUDGET_SECONDS if args.gate else None)
    if args.gate:
        command += [f"--durations={SLOWEST_SHOWN}", "--durations-min=2"]  # name the tests that eat the budget
    started = time.monotonic()
    code = subprocess.call(command, cwd=ROOT, env=env)
    elapsed = time.monotonic() - started
    if budget is not None:
        print(f"time budget: {elapsed:.0f} s of {budget:.0f} s", file=sys.stderr)
        if elapsed > budget:
            print(
                f"FAILED: the run took {elapsed:.0f} s, over its {budget:.0f} s budget. Move the slowest tests listed above out of the gate "
                "(python tools/test_history.py order --write, then commit tests/order.json) or make them smaller.",
                file=sys.stderr,
            )
            return code or BUDGET_EXIT
    return code


if __name__ == "__main__":
    raise SystemExit(main())
