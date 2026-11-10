"""The pytest-xdist scheduler that keeps slow tests from queueing behind each other (tools/xdist_scheduler.py)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("xdist")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import xdist_scheduler  # noqa: E402


class Node:
    def __init__(self, name):
        self.gateway = SimpleNamespace(id=name)
        self.shutting_down = False
        self.queue: list[int] = []

    def send_runtest_some(self, indices):
        self.queue.extend(indices)

    def shutdown(self):
        self.shutting_down = True


class Config:
    def __init__(self, workers):
        self.option = SimpleNamespace(tx=[f"{workers}*popen"], loadscopereorder=True)

    def getvalue(self, name):
        return getattr(self.option, name)


def ready(node):
    """A pytest-xdist worker starts a test only once it holds the one after it too, or has been told to stop."""

    return len(node.queue) >= 2 or (len(node.queue) == 1 and node.shutting_down)


def run(collection, slow, workers=2, pick=lambda nodes: nodes[0], stock=False, together=None):
    """Drive the scheduler like pytest-xdist does: each step, one busy worker finishes the test it is running."""

    from xdist.scheduler import LoadGroupScheduling

    scheduler = LoadGroupScheduling(Config(workers)) if stock else xdist_scheduler.DurationScheduling(Config(workers), slow=slow, together=together)
    nodes = [Node(f"gw{i}") for i in range(workers)]
    for node in nodes:
        scheduler.add_node(node)
    for node in nodes:
        scheduler.add_node_collection(node, collection)
    scheduler.schedule()
    ran = {node.gateway.id: [] for node in nodes}
    held_at_once = 0
    while any(node.queue for node in nodes):
        held_at_once = max([held_at_once] + [sum(collection[i] in slow for i in node.queue) for node in nodes])
        busy = [n for n in nodes if ready(n)]
        assert busy, "every worker is waiting for work the scheduler holds back"
        node = pick(busy)
        index = node.queue.pop(0)
        ran[node.gateway.id].append(collection[index])
        scheduler.mark_test_complete(node, index)
    assert all(node.shutting_down for node in nodes)
    return ran, held_at_once


def test_a_worker_never_holds_two_slow_tests_and_every_test_runs_once():
    collection = ["t.py::fast1", "t.py::fast2", "t.py::fast3", "t.py::slow_a", "t.py::slow_b", "t.py::slow_c", "t.py::fast4"]
    slow = {"t.py::slow_a": 100.0, "t.py::slow_b": 90.0, "t.py::slow_c": 80.0}
    ran, held = run(collection, slow)
    assert held == 1
    assert sorted(sum(ran.values(), [])) == sorted(collection)
    _, held = run(collection, slow, stock=True)
    assert held == 2  # pytest-xdist's own loadgroup queues a slow test behind another on one worker


def test_the_order_tests_conftest_chose_is_kept_and_groups_stay_together():
    collection = ["t.py::a", "t.py::b@g", "t.py::c@g", "t.py::d", "t.py::e@g"]
    ran, _ = run(collection, {}, workers=1)
    # loadgroup would move the group (three tests) to the front; this scheduler keeps the collection order
    assert ran["gw0"] == ["t.py::a", "t.py::b@g", "t.py::c@g", "t.py::e@g", "t.py::d"]
    ran, _ = run(collection, {}, workers=2, pick=lambda nodes: nodes[-1])
    together = [worker for worker, tests in ran.items() if "t.py::b@g" in tests]
    assert all(test in ran[together[0]] for test in ("t.py::b@g", "t.py::c@g", "t.py::e@g"))


def test_a_worker_starting_a_slow_test_gets_short_work_next_not_the_next_slow_test():
    collection = ["t.py::slow_a", "t.py::slow_b", "t.py::slow_c", "t.py::fast1", "t.py::fast2", "t.py::fast3"]
    slow = {"t.py::slow_a": 100.0, "t.py::slow_b": 90.0, "t.py::slow_c": 80.0}
    ran, held = run(collection, slow)
    assert held == 1
    assert ran["gw0"][:2] == ["t.py::slow_a", "t.py::fast1"]
    assert ran["gw1"][:2] == ["t.py::slow_b", "t.py::fast2"]
    assert sorted(sum(ran.values(), [])) == sorted(collection)


def test_a_file_with_a_slow_shared_fixture_runs_on_one_worker_with_its_test_ids_unchanged():
    collection = ["s.py::slow", "f.py::a", "f.py::b", "f.py::c", "t.py::fast1", "t.py::fast2", "t.py::fast3", "t.py::fast4"]
    for pick in (lambda nodes: nodes[0], lambda nodes: nodes[-1]):
        ran, _ = run(collection, {"s.py::slow": 50.0}, workers=3, pick=pick, together={"f.py": 30.0})
        (worker,) = [w for w, tests in ran.items() if "f.py::a" in tests]
        assert [t for t in ran[worker] if t.startswith("f.py")] == ["f.py::a", "f.py::b", "f.py::c"]
        assert sorted(sum(ran.values(), [])) == sorted(collection)


def test_conftest_times_shared_fixture_files_whole_and_new_eval_files_as_slow():
    import importlib.util

    spec = importlib.util.spec_from_file_location("timing_conftest", Path(__file__).resolve().parent / "conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    order = {"slow": {"tests/test_a.py::big": 9.0}, "together": {"tests/test_b.py": 40.0}, "files": ["tests/test_a.py", "tests/test_b.py", "tests/test_spider2_bench.py"]}
    seconds, together = conftest._timing(order)
    assert together == {"tests/test_b.py": 40.0}
    assert seconds("tests/test_a.py::big") == 9.0 and seconds("tests/test_a.py::small") is None
    assert seconds("tests/test_b.py::anything") == 40.0
    assert seconds("tests/test_spider2_bench.py::new_case") is None  # a known eval file: its fast tests stay fast
    assert seconds("tests/test_sqlfluff_fixtures_bench.py::t") == conftest.NEW_EVAL_SECONDS  # an eval file never timed
    assert seconds("tests/test_brand_new.py::t") is None  # not an eval file
    assert conftest._timing({"slow": {}})[0]("tests/test_sqlfluff_fixtures_bench.py::t") is None  # no history, no guess
