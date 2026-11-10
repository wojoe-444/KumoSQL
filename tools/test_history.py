"""Record every test run, and rank tests by how often they break.

Every pytest run in this repository (``python tools/run_tests.py`` or ``python -m pytest``) appends one record to a
shared history directory when it can find one: ``$KUMOSQL_TEST_HISTORY``, else ``/mnt/project-files/test-history``
when ``/mnt/project-files`` exists (``KUMOSQL_TEST_HISTORY=off`` turns recording off). A record is one JSON line in its
own file under ``runs/``, so threads running at the same time never collide. It holds the commit, branch, a task
label, the tests the run was aiming at (the *targets*), the tests that failed and which of those were outside the
targets, per-file test counts, and the times: the run's wall and CPU time, the machine it ran on, per-file wall and CPU
time and the slow tests' durations.

``tests/conftest.py`` installs the recorder, so nothing else needs to change in a test file.

Commands (``python tools/test_history.py <command>``):

* ``report``: rank tests by how often they broke a change that otherwise worked (failures outside the targets, in a
  run whose targets all passed), then by failures overall; list flaky candidates and slow tests.
* ``trend``: test times over time: whole runs (wall and CPU, machine, workers), the files whose time changed most, and
  one test's or file's history with ``--test``.
* ``order``: write ``tests/order.json`` (slow tests with their durations, tests that fail often) from the history.
  ``tests/conftest.py`` uses it to run likely failures first, then the fast tests, then the slow tests longest-first.
* ``import-junit``: add an existing JUnit file to the history (used to seed it).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = Path("/mnt/project-files/test-history")
ORDER_FILE = ROOT / "tests" / "order.json"
SCHEMA = 3  # 2 added the times: cpu_seconds, test_seconds, test_cpu_seconds, file_seconds, slow_cpu, machine;
# 3 times a slow test with its setup and teardown (2 timed the call only) and adds slow_setup
SLOW_SECONDS = 2.0  # a test at least this slow (setup, call and teardown) is recorded with its duration and runs in the slow tier
USUALLY_FAST_SECONDS = 10.0  # a test slower than this stays in the slow tier even when it is slow in only some runs
MASS_FAILURE = 20  # a run with this many failures is an environment problem, not a test that broke
MESSAGE_CHARS = 200


# ---------------------------------------------------------------------------------------------------------------
# where the history lives


def history_dir() -> Path | None:
    """The directory records are written to, or None when recording is off or there is no shared folder."""

    setting = os.environ.get("KUMOSQL_TEST_HISTORY")
    if setting is not None:
        return None if setting.strip().lower() in ("", "0", "off", "false", "no") else Path(setting)
    return SHARED_DIR if SHARED_DIR.parent.is_dir() else None


def load_records(directory: Path | None = None, since_days: float | None = None) -> list[dict]:
    directory = directory or history_dir()
    if directory is None or not directory.is_dir():
        return []
    cutoff = None if since_days is None else (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).isoformat()
    records = []
    for path in sorted(directory.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and (cutoff is None or record.get("ts", "") >= cutoff):
                records.append(record)
    records.sort(key=lambda r: r.get("ts", ""))
    return records


def write_record(record: dict, directory: Path) -> Path:
    runs = directory / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = record["ts"].replace(":", "").replace("-", "")[:15]
    name = f"{stamp}-{(record.get('commit') or 'nocommit')[:8]}-{uuid.uuid4().hex[:6]}.jsonl"
    temporary = runs / (name + ".tmp")
    temporary.write_text(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    final = runs / name
    os.replace(temporary, final)
    return final


# ---------------------------------------------------------------------------------------------------------------
# what a run was aiming at


def _git(*args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def git_state() -> dict:
    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    base = _git("merge-base", "HEAD", "origin/master") or _git("merge-base", "HEAD", "master")
    return {
        "commit": head,
        "branch": branch if branch and branch != "HEAD" else os.environ.get("GITHUB_REF_NAME", "") or "(detached)",
        "base": base,
        "dirty": bool(_git("status", "--porcelain", "-uno")),
    }


def changed_test_files(base: str) -> list[str]:
    """Test files this checkout adds or changes against the master it branched from."""

    if not base:
        return []
    names = set(_git("diff", "--name-only", base).splitlines()) | set(_git("ls-files", "-o", "--exclude-standard", "tests").splitlines())
    return sorted(n for n in names if n.startswith("tests/") and n.endswith(".py") and Path(n).name.startswith("test_"))


def is_target(nodeid: str, targets: list[str]) -> bool:
    """A target is a test file, a directory or a full test id; a test id matches itself and its parametrisations."""

    for target in targets:
        target = target[2:] if target.startswith("./") else target
        if nodeid == target or nodeid.startswith(target + "::") or nodeid.startswith(target + "[") or (target.endswith("/") and nodeid.startswith(target)):
            return True
    return False


def resolve_targets(explicit_args: list[str], partial: bool, base: str) -> tuple[list[str], str]:
    """The tests this run is aiming at, and where that came from: the environment, the command line, or the test
    files the branch changed."""

    env = [t.strip() for t in os.environ.get("KUMOSQL_TEST_TARGETS", "").split(",") if t.strip()]
    if env:
        return env, "declared"
    if partial and explicit_args:
        return explicit_args, "args"
    changed = changed_test_files(base)
    return (changed, "changed-tests") if changed else ([], "none")


# ---------------------------------------------------------------------------------------------------------------
# the pytest plugin


def _versions() -> dict:
    from importlib import metadata

    found = {"python": ".".join(map(str, sys.version_info[:3]))}
    for name in ("sqlglot", "sqlglotc", "sqlfluff", "z3-solver", "duckdb", "pytest", "pytest-xdist"):
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return found


def process_cpu(children: bool = True) -> float:
    """CPU seconds used so far by this process (every thread, DuckDB's included) and, unless ``children`` is False,
    its finished child processes.

    Windows has no ``resource`` module; there only this process's own time is counted."""

    try:
        import resource
    except ImportError:
        return time.process_time()
    own = resource.getrusage(resource.RUSAGE_SELF)
    total = own.ru_utime + own.ru_stime
    if children:
        finished = resource.getrusage(resource.RUSAGE_CHILDREN)
        total += finished.ru_utime + finished.ru_stime
    return total


def _machine() -> dict:
    """What the run's times depend on: cores, processor, memory and the sqlglot build (no host or user names)."""

    import importlib.util

    found: dict = {"cpus": os.cpu_count() or 0, "system": platform.system(), "arch": platform.machine()}
    model = platform.processor()
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if model:
        found["cpu_model"] = model[:80]
    try:
        found["memory_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (AttributeError, ValueError, OSError):
        pass
    try:
        origin = importlib.util.find_spec("sqlglot.parser").origin or ""
        found["sqlglot_build"] = "compiled" if origin.endswith((".so", ".pyd")) else "pure"
    except (ImportError, AttributeError, ValueError):
        pass
    return found


try:
    import pytest

    _runs_first = pytest.hookimpl(tryfirst=True)
    _optional = pytest.hookimpl(optionalhook=True)  # a pytest-xdist hook, absent when xdist is not installed
except ImportError:  # the report commands do not need pytest
    def _runs_first(function):
        return function

    _optional = _runs_first


class Clock:
    """Runs where the tests run (each pytest-xdist worker, or the only process of a serial run): stamps each test's
    CPU seconds, setup to teardown, on its teardown report, which pytest-xdist forwards to the controller with the
    report's other fields; and hands the worker's total CPU to the controller when the worker finishes."""

    def __init__(self, config):
        self.config = config
        self.started: dict[str, float] = {}

    def pytest_runtest_logstart(self, nodeid, location) -> None:
        self.started[nodeid] = process_cpu()

    @_runs_first  # before pytest-xdist sends the report on, and before the Recorder reads it in a serial run
    def pytest_runtest_logreport(self, report) -> None:
        started = self.started.pop(report.nodeid, None) if report.when == "teardown" else None
        if started is not None and not hasattr(report, "cpu_seconds"):  # a pytest-xdist controller keeps its worker's stamp
            report.cpu_seconds = round(process_cpu() - started, 3)

    def pytest_sessionfinish(self) -> None:
        output = getattr(self.config, "workeroutput", None)
        if output is not None:
            output["kumosql_cpu_seconds"] = process_cpu()


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, rest = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else (f"{minutes}m{rest:02d}s" if minutes else f"{rest}s")


def _first_line(text: object) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()[:MESSAGE_CHARS]
    return ""


class Recorder:
    """Collects outcomes on the controller process (pytest-xdist forwards every worker's reports to it)."""

    def __init__(self, config, directory: Path):
        self.config = config
        self.directory = directory
        self.started = time.time()
        self.counts: collections.Counter = collections.Counter()
        self.failed: dict[str, dict] = {}
        self.slow: dict[str, float] = {}
        self.slow_cpu: dict[str, float] = {}
        self.slow_setup: dict[str, float] = {}
        self.files: collections.Counter = collections.Counter()
        self.wall: collections.Counter = collections.Counter()  # per test: setup + call + teardown seconds
        self.file_times: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0])  # per file: wall, CPU
        self.test_seconds = self.test_cpu_seconds = 0.0
        self.worker_cpu: list[float] = []
        self.written: Path | None = None

    # -- outcomes
    def pytest_runtest_logreport(self, report) -> None:
        self._time(report)
        outcome = None
        wasxfail = hasattr(report, "wasxfail")
        if report.when == "call":
            if report.passed:
                outcome = "xpassed" if wasxfail else "passed"
            elif report.failed:
                outcome = "failed"
            else:
                outcome = "xfailed" if wasxfail else "skipped"
        elif report.when in ("setup", "teardown") and report.failed:
            outcome = "error"
        elif report.when == "setup" and report.skipped:
            outcome = "xfailed" if wasxfail else "skipped"
        if outcome is None:
            return
        self.counts[outcome] += 1
        nodeid = report.nodeid
        if report.when != "teardown" or outcome != "error":
            self.files[nodeid.split("::", 1)[0]] += 1
        if outcome in ("failed", "error"):
            crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
            message = _first_line(getattr(crash, "message", "") or report.longrepr)
            self.failed.setdefault(nodeid, {"id": nodeid, "kind": outcome, "seconds": round(report.duration, 2), "msg": message})

    def _time(self, report) -> None:
        nodeid = report.nodeid
        self.wall[nodeid] += report.duration
        if report.when == "setup" and report.duration >= SLOW_SECONDS:
            self.slow_setup[nodeid] = round(report.duration, 1)  # a shared fixture built for this test
        if report.when != "teardown":
            return
        wall, cpu = self.wall.pop(nodeid), getattr(report, "cpu_seconds", None)
        if wall >= SLOW_SECONDS:
            self.slow[nodeid] = round(wall, 1)
        times = self.file_times[nodeid.split("::", 1)[0]]
        times[0] += wall
        self.test_seconds += wall
        if cpu is not None:
            times[1] += cpu
            self.test_cpu_seconds += cpu
            if nodeid in self.slow:
                self.slow_cpu[nodeid] = round(cpu, 1)

    @_optional
    def pytest_testnodedown(self, node, error) -> None:
        """A pytest-xdist worker finished; its total CPU comes back with it (see Clock)."""

        seconds = getattr(node, "workeroutput", {}).get("kumosql_cpu_seconds")
        if isinstance(seconds, (int, float)):
            self.worker_cpu.append(seconds)

    def cpu_seconds(self) -> float:
        """The run's CPU from start-up (imports and collection included): this process plus every worker's total, or
        in a serial run this process and its children. The workers are this process's children too, so with workers
        only their own reports count (each includes the processes the worker started)."""

        if self.worker_cpu:
            return process_cpu(children=False) + sum(self.worker_cpu)
        return process_cpu()

    # -- the record
    def mode(self) -> tuple[str, list[str]]:
        """full, evals, no-evals or partial, and the positional arguments that made a run partial."""

        option = self.config.option
        positional = [a for a in getattr(self.config, "args", []) if a]
        defaults = [str(p) for p in self.config.getini("testpaths")]
        partial = bool(getattr(option, "keyword", "")) or bool(getattr(option, "deselect", None)) or bool(getattr(option, "quick", False))
        if positional and positional != defaults:
            return "partial", positional
        if partial:
            return "partial", []
        expression = getattr(option, "markexpr", "") or ""
        if "not eval" in expression:
            return "no-evals", []
        if "eval" in expression:
            return "evals", []
        return "full", []

    def build(self, exitstatus: int) -> dict:
        state = git_state()
        mode, explicit = self.mode()
        targets, source = resolve_targets(explicit, mode == "partial", state["base"])
        failed = []
        for entry in self.failed.values():
            failed.append({**entry, "target": is_target(entry["id"], targets)})
        failed.sort(key=lambda e: e["id"])
        label = os.environ.get("KUMOSQL_TASK", "").strip() or state["branch"]
        workers = getattr(self.config.option, "numprocesses", None)
        machine, versions = _machine(), _versions()  # before the CPU is read: finding sqlglot's build can import it
        return {
            "v": SCHEMA,
            "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **state,
            "label": label,
            "targets": targets,
            "targets_source": source,
            "mode": mode,
            "workers": workers if isinstance(workers, int) else 1,
            "versions": versions,
            "seconds": round(time.time() - self.started, 1),
            "cpu_seconds": round(self.cpu_seconds(), 1),
            "test_seconds": round(self.test_seconds, 1),
            "test_cpu_seconds": round(self.test_cpu_seconds, 1),
            "machine": machine,
            "exit": int(exitstatus),
            "counts": dict(self.counts),
            "failed": failed,
            "slow": dict(sorted(self.slow.items())),
            "slow_cpu": dict(sorted(self.slow_cpu.items())),
            "slow_setup": dict(sorted(self.slow_setup.items())),
            "files": dict(sorted(self.files.items())),
            "file_seconds": {name: [round(wall, 1), round(cpu, 1)] for name, (wall, cpu) in sorted(self.file_times.items())},
        }

    def pytest_terminal_summary(self, terminalreporter, exitstatus) -> None:
        if not sum(self.counts.values()) or getattr(self.config.option, "collectonly", False):
            return
        try:
            record = self.build(exitstatus)
            before = load_records(self.directory) if record["failed"] else []  # earlier runs only, not this one
            self.written = write_record(record, self.directory)
        except Exception as error:  # recording never fails a test run
            terminalreporter.write_line(f"test history: not recorded ({type(error).__name__}: {error})")
            return
        outside = [f["id"] for f in record["failed"] if not f["target"]] if record["targets"] else []
        if record["failed"] and record["targets"]:
            earlier = collateral_counts(before) if outside else {}
            terminalreporter.section("test history")
            hit = [f for f in record["failed"] if f["target"]]
            terminalreporter.write_line(f"targets ({record['targets_source']}): {', '.join(record['targets'][:6])}{' ...' if len(record['targets']) > 6 else ''}")
            terminalreporter.write_line(f"{len(hit)} failed inside the targets, {len(outside)} outside them")
            for nodeid in outside[:12]:
                times = earlier.get(nodeid, {}).get("runs", 0)
                terminalreporter.write_line(f"  outside the targets: {nodeid}" + (f" (broke {times} earlier change{'s' if times != 1 else ''} that otherwise worked)" if times else ""))
        terminalreporter.write_line(
            f"test history: recorded to {self.written} (wall {_duration(record['seconds'])}, CPU {_duration(record['cpu_seconds'])}, {record['workers']} worker{'s' if record['workers'] != 1 else ''})"
        )


def install(config) -> None:
    """Called from tests/conftest.py, only when a history directory exists: every process that runs tests times them,
    and only the controller records."""

    directory = history_dir()
    if directory is None:
        return
    config.pluginmanager.register(Clock(config), "kumosql-test-clock")
    if not hasattr(config, "workerinput"):
        config.pluginmanager.register(Recorder(config, directory), "kumosql-test-history")


# ---------------------------------------------------------------------------------------------------------------
# analysis


def working_change_run(record: dict) -> bool:
    """A run aimed at known tests where all of them passed, on a branch other than master."""

    if record.get("targets_source") in (None, "none") or not record.get("targets"):
        return False
    if record.get("branch") in ("master", "main"):
        return False
    return not any(f.get("target") for f in record.get("failed", []))


def mass_failure(record: dict) -> bool:
    return len(record.get("failed", [])) >= MASS_FAILURE


def collateral_counts(records: list[dict]) -> dict[str, dict]:
    """Per test: runs where it failed outside the targets while every target passed."""

    found: dict[str, dict] = {}
    for record in records:
        if not working_change_run(record) or mass_failure(record):
            continue
        for failure in record["failed"]:
            entry = found.setdefault(failure["id"], {"runs": 0, "labels": set(), "branches": set(), "last": "", "msg": ""})
            entry["runs"] += 1
            entry["labels"].add(record.get("label") or record.get("branch") or "?")
            entry["branches"].add(record.get("branch", "?"))
            entry["last"] = max(entry["last"], record.get("ts", ""))
            entry["msg"] = failure.get("msg", "") or entry["msg"]
    return found


def failure_counts(records: list[dict]) -> dict[str, dict]:
    """Per test: failures overall, split by where they happened, and how many comparable runs ran it."""

    found: dict[str, dict] = {}
    comparable = [r for r in records if not mass_failure(r)]
    ran_by_file: collections.Counter = collections.Counter()
    for record in comparable:
        for file in record.get("files", {}):
            ran_by_file[file] += 1
    for record in comparable:
        for failure in record.get("failed", []):
            entry = found.setdefault(failure["id"], {"failed": 0, "targeted": 0, "outside": 0, "master": 0, "unattributed": 0, "last": "", "msg": ""})
            entry["failed"] += 1
            entry["last"] = max(entry["last"], record.get("ts", ""))
            entry["msg"] = failure.get("msg", "") or entry["msg"]
            if record.get("branch") in ("master", "main"):
                entry["master"] += 1
            elif record.get("targets_source") in (None, "none") or not record.get("targets"):
                entry["unattributed"] += 1
            elif failure.get("target"):
                entry["targeted"] += 1
            else:
                entry["outside"] += 1
    for nodeid, entry in found.items():
        entry["runs"] = ran_by_file.get(nodeid.split("::", 1)[0], 0)
    return found


def flaky_candidates(records: list[dict]) -> list[tuple[str, int, int, str]]:
    """Tests that failed in one clean run and passed in another at the same commit."""

    by_commit: dict[str, list[dict]] = collections.defaultdict(list)
    for record in records:
        if record.get("commit") and not record.get("dirty") and not mass_failure(record):
            by_commit[record["commit"]].append(record)
    found: dict[str, list] = {}
    for commit, runs in by_commit.items():
        if len(runs) < 2:
            continue
        failed_ids = {f["id"] for r in runs for f in r.get("failed", [])}
        for nodeid in failed_ids:
            file = nodeid.split("::", 1)[0]
            failing = sum(1 for r in runs if any(f["id"] == nodeid for f in r["failed"]))
            passing = sum(1 for r in runs if file in r.get("files", {}) and not any(f["id"] == nodeid for f in r["failed"]))
            if failing and passing:
                entry = found.setdefault(nodeid, [0, 0, ""])
                entry[0] += failing
                entry[1] += passing
                entry[2] = commit[:8]
    return sorted(((n, e[0], e[1], e[2]) for n, e in found.items()), key=lambda x: -x[1])


def slow_tests(records: list[dict]) -> dict[str, float]:
    """The slow tier: tests that took ``SLOW_SECONDS`` or more, with the median of their slow runs.

    A run records a test only when it was slow, so the median alone cannot tell a test that is always slow from one that
    was slow on a loaded machine in a few runs of its file. A test of under ``USUALLY_FAST_SECONDS`` that was slow in less
    than half the runs that ran its file stays out of the tier, so ``--quick`` still runs it.
    """

    seen: dict[str, list[float]] = collections.defaultdict(list)
    ran: dict[str, int] = collections.Counter()
    for record in records:
        if mass_failure(record):
            continue
        ran.update(record.get("files", {}).keys())
        for nodeid, seconds in record.get("slow", {}).items():
            seen[nodeid].append(seconds)
    tier = {}
    for nodeid, values in seen.items():
        median = round(statistics.median(values), 1)
        slow_share = len(values) / max(ran[nodeid.split("::", 1)[0]], len(values))
        if median >= USUALLY_FAST_SECONDS or slow_share >= 0.5:
            tier[nodeid] = median
    return tier


def build_order(records: list[dict], risky_limit: int = 40, root: Path | None = None) -> dict:
    """The order file; with ``root``, only tests whose files exist there (other branches' new tests are left out)."""

    def here(nodeid: str) -> bool:
        return root is None or (root / nodeid.split("::", 1)[0]).is_file()

    counts = failure_counts(records)
    collateral = collateral_counts(records)
    risky = sorted(
        (n for n, e in counts.items() if e["failed"] >= 1 and here(n)),
        key=lambda n: (-collateral.get(n, {}).get("runs", 0), -counts[n]["failed"], n),
    )[:risky_limit]
    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs": len(records),
        "slow_seconds": SLOW_SECONDS,
        "slow": dict(sorted(((n, s) for n, s in slow_tests(records).items() if here(n)), key=lambda kv: -kv[1])),
        "together": {name: seconds for name, seconds in shared_fixture_files(records).items() if here(name)},
        "risky": risky,
        "files": sorted({name for record in records for name in record.get("files", {}) if here(name)}),
    }


def shared_fixture_files(records: list[dict]) -> dict[str, float]:
    """Files with a slow setup (a module or class fixture that takes 3 seconds or more), with the whole file's median
    seconds, longest first. Their tests run together on one worker so the fixture is built once."""

    found = {nodeid.split("::", 1)[0] for record in records if not mass_failure(record) for nodeid in record.get("slow_setup", {})}
    seconds: dict[str, list[float]] = collections.defaultdict(list)
    for record in records:
        if mass_failure(record) or "file_seconds" not in record:
            continue
        for name in found & set(record["file_seconds"]):
            seconds[name].append(record["file_seconds"][name][0])
    timed = {name: round(statistics.median(seconds[name]), 1) if seconds[name] else SLOW_SECONDS for name in found}
    return dict(sorted(timed.items(), key=lambda kv: (-kv[1], kv[0])))


# ---------------------------------------------------------------------------------------------------------------
# commands


def _table(rows: list[list[str]], header: list[str]) -> str:
    rows = [header] + rows
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(header))]
    lines = ["  ".join(str(c).ljust(widths[i]) if i == 0 or not str(c).replace(".", "").isdigit() else str(c).rjust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def command_report(args) -> int:
    records = load_records(Path(args.dir) if args.dir else None, args.since)
    if not records:
        print("No test history yet. Run tests with a shared folder at /mnt/project-files or set KUMOSQL_TEST_HISTORY.")
        return 0
    working = [r for r in records if working_change_run(r) and not mass_failure(r)]
    mass = [r for r in records if mass_failure(r)]
    print(f"{len(records)} runs from {records[0]['ts'][:10]} to {records[-1]['ts'][:10]}; {len(working)} aimed at known tests that all passed; {len(mass)} environment-sized failures ({MASS_FAILURE}+ at once) left out of the rankings.\n")
    collateral = collateral_counts(records)
    print("Tests that break changes that otherwise work (failed outside the targets while every target passed)")
    rows = [[n, e["runs"], len(e["labels"]), e["last"][:10], e["msg"][:60]] for n, e in sorted(collateral.items(), key=lambda kv: (-kv[1]["runs"], -len(kv[1]["labels"]), kv[0]))[: args.top]]
    print(_table(rows, ["test", "runs", "tasks", "last", "message"]) if rows else "  none recorded yet")
    counts = failure_counts(records)
    print("\nTests that fail most, by where")
    rows = [
        [n, e["failed"], f"{e['failed']}/{e['runs']}" if e["runs"] else "-", e["targeted"], e["outside"], e["master"], e["unattributed"]]
        for n, e in sorted(counts.items(), key=lambda kv: (-kv[1]["failed"], kv[0]))[: args.top]
    ]
    print(_table(rows, ["test", "failed", "of runs", "targeted", "outside targets", "on master", "unknown"]) if rows else "  none")
    flaky = flaky_candidates(records)
    print("\nFlaky candidates (failed and passed at the same commit)")
    print(_table([[n, f, p, c] for n, f, p, c in flaky[: args.top]], ["test", "failed", "passed", "commit"]) if flaky else "  none")
    slow = sorted(slow_tests(records).items(), key=lambda kv: -kv[1])[: args.top]
    print(f"\nSlowest tests (median seconds, runs with at least {SLOW_SECONDS:g} s)")
    print(_table([[n, s] for n, s in slow], ["test", "seconds"]) if slow else "  none")
    return 0


WHOLE_RUNS = ("full", "evals", "no-evals")


def _cores(record: dict) -> str:
    return str(record.get("machine", {}).get("cpus") or "-")


def file_times(record: dict) -> dict[str, tuple[float, float | None]]:
    """Per file (wall, CPU) seconds; records from before the times were kept give the slow tests' sum, no CPU."""

    if "file_seconds" in record:
        return {name: (wall, cpu) for name, (wall, cpu) in record["file_seconds"].items()}
    found: dict[str, float] = collections.Counter()
    for nodeid, seconds in record.get("slow", {}).items():
        found[nodeid.split("::", 1)[0]] += seconds
    return {name: (seconds, None) for name, seconds in found.items()}


def file_changes(records: list[dict]) -> list[tuple[str, float, float, int]]:
    """Files whose wall time moved between the earliest and the latest whole runs on the same core count and mode as
    the latest one: (file, then, now, runs that timed it)."""

    whole = [r for r in records if r.get("mode") in WHOLE_RUNS and "file_seconds" in r and not mass_failure(r)]
    if not whole:
        return []
    latest = whole[-1]
    alike = [r for r in whole if r.get("mode") == latest.get("mode") and _cores(r) == _cores(latest)]
    seen: dict[str, list[float]] = collections.defaultdict(list)
    for record in alike:
        for name, (wall, _) in file_times(record).items():
            seen[name].append(wall)
    return [(name, values[0], values[-1], len(values)) for name, values in seen.items() if len(values) >= 2]


def history_of(records: list[dict], pattern: str) -> list[tuple[dict, str, float, float | None]]:
    """Every recorded time of the tests or files whose id contains ``pattern``: (record, id, wall, CPU)."""

    found = []
    for record in records:
        if "::" in pattern or "[" in pattern:
            cpu = record.get("slow_cpu", {})
            found += [(record, nodeid, seconds, cpu.get(nodeid)) for nodeid, seconds in sorted(record.get("slow", {}).items()) if pattern in nodeid]
        else:
            found += [(record, name, wall, cpu) for name, (wall, cpu) in sorted(file_times(record).items()) if pattern in name]
    return found


def command_trend(args) -> int:
    records = load_records(Path(args.dir) if args.dir else None, args.since)
    if not records:
        print("No test history yet. Run tests with a shared folder at /mnt/project-files or set KUMOSQL_TEST_HISTORY.")
        return 0
    if args.test:
        rows = [
            [r["ts"][:16].replace("T", " "), (r.get("commit") or "")[:8], r.get("mode", "?"), r.get("workers", "-"), _cores(r), name, f"{wall:.1f}", "-" if cpu is None else f"{cpu:.1f}"]
            for r, name, wall, cpu in history_of(records, args.test)
        ]
        print(_table(rows[-args.top * 4 :], ["when", "commit", "mode", "workers", "cores", "test or file", "wall s", "CPU s"]) if rows else f"  nothing recorded for {args.test!r}")
        return 0
    whole = [r for r in records if r.get("mode") in WHOLE_RUNS]
    # an imported JUnit file (workers 0) knows each test's time, not the run's wall time
    print("Whole runs (newest last; wall is what you wait for, CPU is the work done across every worker)")
    rows = [
        [
            r["ts"][:16].replace("T", " "), (r.get("commit") or "")[:8], r.get("mode", "?"), r.get("workers", "-"), _cores(r),
            r.get("machine", {}).get("sqlglot_build", "-"), _duration(r.get("seconds") if r.get("workers") else None), _duration(r.get("cpu_seconds")),
            sum(r.get("counts", {}).values()), len(r.get("failed", [])), (r.get("label") or "")[:40],
        ]
        for r in whole[-args.top :]
    ]
    print(_table(rows, ["when", "commit", "mode", "workers", "cores", "sqlglot", "wall", "CPU", "tests", "failed", "label"]) if rows else "  none yet")
    changes = file_changes(records)
    print("\nFiles whose time changed most (wall seconds, earliest and latest whole runs like the newest one)")
    changes.sort(key=lambda c: -abs(c[2] - c[1]))
    rows = [[name, f"{then:.1f}", f"{now:.1f}", f"{now - then:+.1f}", runs] for name, then, now, runs in changes[: args.top]]
    print(_table(rows, ["file", "then", "now", "change", "runs"]) if rows else "  needs two whole runs with per-file times on the same core count")
    latest = next((r for r in reversed(whole) if "file_seconds" in r), None)
    if latest is not None:
        print(f"\nWhere the time goes in the newest whole run ({latest['ts'][:16].replace('T', ' ')}, {(latest.get('commit') or '')[:8]})")
        times = sorted(file_times(latest).items(), key=lambda kv: -(kv[1][1] if kv[1][1] is not None else kv[1][0]))
        total = latest.get("test_cpu_seconds") or 1
        rows = [[name, f"{wall:.1f}", f"{cpu:.1f}", f"{100 * cpu / total:.0f}%"] for name, (wall, cpu) in times[: args.top]]
        print(_table(rows, ["file", "wall s", "CPU s", "of CPU"]))
    return 0


def command_order(args) -> int:
    records = load_records(Path(args.dir) if args.dir else None, args.since)
    if not records:
        print("No test history to build an order from.", file=sys.stderr)
        return 1
    order = build_order(records, root=ROOT)
    target = Path(args.output)
    if args.write:
        target.write_text(json.dumps(order, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {target}: {len(order['slow'])} slow tests, {len(order['together'])} files with a slow shared fixture, {len(order['risky'])} often-failing tests, from {order['runs']} runs")
    else:
        print(json.dumps(order, indent=1))
    return 0


def _node_id(classname: str, name: str, root: Path) -> str:
    parts = classname.split(".")
    for k in range(len(parts), 0, -1):
        candidate = "/".join(parts[:k]) + ".py"
        if (root / candidate).is_file():
            return "::".join([candidate, *parts[k:], name])
    return "::".join([classname.replace(".", "/") + ".py", name])


def record_from_junit(path: Path, *, commit: str, branch: str, label: str, base: str = "", root: Path = ROOT) -> dict:
    tree = ET.parse(path).getroot()
    counts: collections.Counter = collections.Counter()
    failed, slow, files = [], {}, collections.Counter()
    total = 0.0
    for case in tree.iter("testcase"):
        nodeid = _node_id(case.get("classname", ""), case.get("name", ""), root)
        seconds = float(case.get("time", 0))
        total += seconds
        files[nodeid.split("::", 1)[0]] += 1
        problem = case.find("failure")
        problem = problem if problem is not None else case.find("error")
        if problem is not None:
            counts["error" if problem.tag == "error" else "failed"] += 1
            failed.append({"id": nodeid, "kind": problem.tag if problem.tag == "error" else "failed", "seconds": round(seconds, 2), "msg": _first_line(problem.get("message", "")), "target": False})
        elif case.find("skipped") is not None:
            counts["xfailed" if "xfail" in (case.find("skipped").get("type", "") + case.find("skipped").get("message", "")) else "skipped"] += 1
        else:
            counts["passed"] += 1
        if seconds >= SLOW_SECONDS:
            slow[nodeid] = round(seconds, 1)
    stamp = tree.find("testsuite").get("timestamp") if tree.tag == "testsuites" and tree.find("testsuite") is not None else tree.get("timestamp")
    ts = (stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")).split(".")[0].rstrip("Z") + "Z"
    return {
        "v": SCHEMA, "ts": ts, "commit": commit, "branch": branch, "base": base, "dirty": False, "label": label,
        "targets": [], "targets_source": "none", "mode": "full", "workers": 0, "versions": _versions(),
        "seconds": round(total, 1), "test_seconds": round(total, 1), "exit": 1 if failed else 0, "counts": dict(counts),
        "failed": sorted(failed, key=lambda e: e["id"]), "slow": dict(sorted(slow.items())), "files": dict(sorted(files.items())),
    }


def command_import(args) -> int:
    directory = Path(args.dir) if args.dir else history_dir()
    if directory is None:
        print("No history directory (set KUMOSQL_TEST_HISTORY or pass --dir).", file=sys.stderr)
        return 1
    record = record_from_junit(Path(args.junit), commit=args.commit, branch=args.branch, label=args.label)
    print(f"wrote {write_record(record, directory)} ({record['counts']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="rank tests by how often they break")
    trend = sub.add_parser("trend", help="test times over time")
    order = sub.add_parser("order", help="write tests/order.json from the history")
    imp = sub.add_parser("import-junit", help="add a JUnit file to the history")
    for p in (report, trend, order, imp):
        p.add_argument("--dir", help="history directory (default: the shared one)")
    for p in (report, trend, order):
        p.add_argument("--since", type=float, help="only runs from the last N days")
    for p in (report, trend):
        p.add_argument("--top", type=int, default=15, help="rows per table")
    trend.add_argument("--test", help="the recorded times of every test (an id containing :: or [) or file matching this text")
    order.add_argument("--write", action="store_true", help="write the file instead of printing it")
    order.add_argument("--output", default=str(ORDER_FILE))
    imp.add_argument("junit")
    imp.add_argument("--commit", default="")
    imp.add_argument("--branch", default="master")
    imp.add_argument("--label", default="imported")
    args = parser.parse_args(argv)
    return {"report": command_report, "trend": command_trend, "order": command_order, "import-junit": command_import}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
