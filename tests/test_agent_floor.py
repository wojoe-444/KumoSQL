"""The product's own floor: standard analyst questions asked of the agent-facing server stay inside their size, call and latency bounds
(see tools/agent_floor.py and docs/evals/agent-floor.md). No benchmark corpus is downloaded; the 106-model fixture is in the repository."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

_path = Path(__file__).resolve().parent.parent / "tools" / "agent_floor.py"
_spec = importlib.util.spec_from_file_location("agent_floor", _path)
floor = importlib.util.module_from_spec(_spec)
sys.modules["agent_floor"] = floor
_spec.loader.exec_module(floor)

QUESTIONS_FLOOR = 39


def test_every_standard_question_is_answered_inside_its_bounds():
    result = floor.run()
    failed = [(row["question"], row["problems"]) for row in result["tasks"] if row["status"] == "FAILED"]
    assert failed == [], failed
    summary = result["summary"]
    assert summary["measured"] >= QUESTIONS_FLOOR and summary["within_bounds"] == summary["measured"]
    # the point of the server: a task costs a small fraction of reading the project
    assert summary["median_task_bytes"] * 50 < summary["all_sqlx_files_bytes"]
    assert summary["median_task_bytes"] * 200 < summary["pipeline_report_json_bytes"]


def test_a_question_for_a_tool_that_does_not_exist_is_waiting_not_failing():
    server, _ = floor.load_server()
    ask = floor.Asker(server)
    assert {"describe_table", "trace_column", "impact", "find_existing"} <= ask.tools
    needed = ("a_tool_that_is_not_there",)
    assert not set(needed) <= ask.tools
