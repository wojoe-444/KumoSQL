import functools
import itertools
import json
import os
import re
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
ORDER_FILE = Path(__file__).resolve().parent / "order.json"


def _one_thread(config):
    """``config`` with ``threads`` set to 1, unless the caller chose a thread count; never mutates the caller's dict."""

    if config is None:
        return {"threads": 1}
    if not isinstance(config, dict) or any(str(key).lower() in ("threads", "worker_threads") for key in config):
        return config  # the caller's own choice, or a value DuckDB rejects exactly as before
    return {**config, "threads": 1}


def _single_threaded_duckdb():
    """Run DuckDB on one thread in tests unless a caller sets ``threads`` itself.

    Tests open thousands of tiny databases. Each ``duckdb.connect`` otherwise starts a thread per core, and under
    pytest-xdist those threads compete with the other workers for the same cores while doing nothing useful on a few
    rows. Every other argument and config key is passed on unchanged. Pool workers forked by a test inherit this.
    """

    try:
        import duckdb
    except ImportError:
        return
    connect = duckdb.connect
    if getattr(connect, "_kumosql_one_thread", False):
        return

    @functools.wraps(connect)
    def connect_one_thread(*args, **kwargs):
        if len(args) >= 3:  # connect(database, read_only, config)
            args = (*args[:2], _one_thread(args[2]), *args[3:])
        else:
            kwargs["config"] = _one_thread(kwargs.get("config"))
        return connect(*args, **kwargs)

    connect_one_thread._kumosql_one_thread = True
    duckdb.connect = connect_one_thread


def _no_pandas_probe():
    """Mark pandas as missing in ``sys.modules`` when it is not installed, so DuckDB stops searching for it.

    DuckDB tries ``import pandas`` for every bound parameter of ``execute`` and ``executemany``; without pandas each try
    is a full, failing search of ``sys.path``. A ``None`` entry fails the same import at once. ``import pandas`` still
    raises ``ModuleNotFoundError`` and ``importlib.util.find_spec("pandas")`` still returns None, and an installed pandas
    is never shadowed.
    """

    import importlib.util

    try:
        missing = importlib.util.find_spec("pandas") is None
    except (ImportError, ValueError):
        return
    if missing:
        sys.modules.setdefault("pandas", None)


@pytest.fixture(scope="session")
def _homes(tmp_path_factory):
    return tmp_path_factory.mktemp("homes"), itertools.count()


@pytest.fixture(autouse=True)
def isolated_state(request, _homes, monkeypatch):
    """Keep saved UI, scope and formatting state out of the real user directory.

    A test that asks for ``tmp_path`` finds the folder at ``tmp_path / "kumosql-home"``; any other test gets an
    empty folder of its own without the cost of making a ``tmp_path`` for it."""

    if "tmp_path" in request.fixturenames:
        parent = request.getfixturevalue("tmp_path")
    else:
        parent = _homes[0] / str(next(_homes[1]))
        os.mkdir(parent)
    monkeypatch.setenv("KUMOSQL_HOME", str(parent / "kumosql-home"))


@pytest.fixture(autouse=True)
def no_project_loaded(monkeypatch):
    """The loaded project is process-wide state; a test that loads one must not leak it into the next test
    on the same worker (the empty-state UI tests failed under pytest-xdist because of that)."""

    import sys

    module = sys.modules.get("kumosql.live_graph")
    if module is not None:
        monkeypatch.setattr(module, "_LOADED", None)
        monkeypatch.setattr(module, "_JOBS", {"records": (), "label": "", "restored_from": None})
        # Parsed projects are cached in memory by content: a later test loading the same files would hit the
        # cache and never write its own snapshot into its own data folder.
        monkeypatch.setattr(module, "_PROJECT_CACHE", type(module._PROJECT_CACHE)())


@pytest.fixture(autouse=True)
def no_schema_fetch(monkeypatch):
    """Tests never look tables up in BigQuery unless they switch it on with a stub."""

    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "0")


@pytest.fixture(autouse=True)
def fresh_redactor(monkeypatch):
    """Placeholders are per session; keep names one test registered from changing another test's log text."""

    from kumosql import redact

    monkeypatch.setattr(redact, "GLOBAL", redact.Redactor(enabled=True))


# Files whose tests take the longest, slowest first. Under pytest-xdist the scheduler hands tests out in
# collection order, so starting these first keeps one long benchmark from running alone at the end.
HEAVY_FILES = [
    "test_spider2_bench.py",
    "test_sqlfluff_fixtures_bench.py",
    "test_lineage_benchmarks.py",
    "test_pipeline_bench.py",
    "test_jaffle_shop_bench.py",
    "test_minimization_bench.py",
    "test_reduction_bench.py",
    "test_unsafe_fuzz.py",
    "test_safety_corpus.py",
    "test_bq_syntax_coverage.py",
    "test_targeted_data_bench.py",
    "test_sample_db_bench.py",
    "test_sample_db_pagila.py",
    "test_bq_behavior_eval.py",
    "test_bq_corpus_bench.py",
    "test_incremental.py",
    "test_model_reuse_evals.py",
]


# A test file that scores KumoSQL against a benchmark corpus and holds its floors declares it with
# ``pytestmark = pytest.mark.eval``: ``pytest -m eval`` runs just the floors and ``pytest -m "not eval"`` everything
# else. Nothing registers the file here; ``eval_files`` finds the declaration.
_EVAL_DECLARATION = re.compile(r"^pytestmark\s*=.*\bmark\.eval\b", re.M)


@functools.lru_cache(maxsize=1)
def eval_files() -> frozenset[str]:
    """Names of the test files that declare themselves benchmark floors."""

    found = set()
    for path in Path(__file__).resolve().parent.glob("test_*.py"):
        try:
            if _EVAL_DECLARATION.search(path.read_text(encoding="utf-8")):
                found.add(path.name)
        except OSError:
            continue
    return frozenset(found)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_make_scheduler(config, log):
    """``--dist loadgroup`` that never queues a test behind a slow one (``tools/xdist_scheduler.py``)."""

    if config.getvalue("dist") != "loadgroup":
        return None
    sys.path.insert(0, str(TOOLS))
    try:
        import xdist_scheduler
    finally:
        sys.path.remove(str(TOOLS))
    seconds, together = _timing(_order_data())
    return xdist_scheduler.DurationScheduling(config, log, seconds=seconds, together=together)


def pytest_addoption(parser):
    parser.addoption("--quick", action="store_true", default=False, help="skip the slow tier (tests listed as slow in tests/order.json and the heavy files)")


def pytest_configure(config):
    """Run DuckDB single-threaded without the pandas probe, and record every run in the shared test history
    (tools/test_history.py; a no-op without a history folder)."""

    _single_threaded_duckdb()
    _no_pandas_probe()
    sys.path.insert(0, str(TOOLS))
    try:
        import test_history

        test_history.install(config)
    except Exception:  # recording must never stop a test run
        pass
    finally:
        sys.path.remove(str(TOOLS))


def _order_data():
    """tests/order.json is written by ``python tools/test_history.py order --write`` from the recorded runs."""

    try:
        return json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


NEW_EVAL_SECONDS = 60.0  # a test in an eval file the history has never run counts as this slow until it is timed


def _timing(order):
    """``seconds(nodeid)``: how long a slow-tier test is expected to take, None for a fast test; and the files whose
    tests run together on one worker (a slow shared fixture), with the whole file's seconds."""

    slow = order.get("slow", {})
    together = order.get("together", {})
    known = set(order.get("files", []))

    def seconds(nodeid: str) -> float | None:
        name = nodeid.split("::", 1)[0]
        if name in together:
            return together[name]
        if nodeid in slow:
            return slow[nodeid]
        if known and name not in known and name.rsplit("/", 1)[-1] in eval_files():
            return NEW_EVAL_SECONDS
        return None

    return seconds, together


def pytest_collection_modifyitems(config, items):
    order = _order_data()
    slow = order.get("slow", {})
    seconds, together = _timing(order)
    risky = {nodeid: position for position, nodeid in enumerate(order.get("risky", []))}
    heavy = {name: position for position, name in enumerate(HEAVY_FILES)}

    def is_slow(item):
        if item.get_closest_marker("canary"):
            return False  # a canary stands for its benchmark family in the gate, so it is never moved to the slow tier
        # without recorded durations the hand-kept list of heavy files stands in for the slow tier
        return seconds(item.nodeid) is not None or (not slow and item.path.name in heavy)

    if config.getoption("--quick"):
        dropped = [item for item in items if is_slow(item)]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
            items[:] = [item for item in items if not is_slow(item)]
    parallel = hasattr(config, "workerinput") or getattr(config.option, "numprocesses", None)
    if not parallel:
        return  # a serial run keeps the natural order

    # Tests that failed before come first, then the fast tests (a broken one shows up in minutes), then the slow
    # tests longest-first so one long benchmark never runs alone at the end. A test that alone takes more than half
    # of one worker's share of the run starts before all of them: started after the fast tests it would end the run
    # late. A file with a slow shared fixture counts as one unit of its whole time. Without recorded durations the
    # heavy files go first, as they always did.
    if not slow:
        items.sort(key=lambda item: heavy.get(item.path.name, len(heavy)))
        return
    workers = getattr(config, "workerinput", {}).get("workercount") or 1
    units = {}
    for item in items:
        name = item.nodeid.split("::", 1)[0]
        expected = seconds(item.nodeid)
        if expected is not None:
            units[name if name in together else item.nodeid] = expected
    share = sum(units.values()) / workers

    def rank(item):
        nodeid = item.nodeid
        expected = seconds(nodeid)
        if expected is not None and expected > share / 2:
            return (-1, -expected, 0)
        if nodeid in risky:
            return (0, risky[nodeid], 0)
        if expected is not None:
            return (2, -expected, 0)
        return (1, int(item.path.name in heavy), 0)  # a new test in a heavy file waits behind the other fast ones

    items.sort(key=rank)
