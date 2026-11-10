"""Unit tests of the field-trial harness (the trial itself needs an agent and is a periodic report, not a test)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("field_trial", ROOT / "tools" / "field_trial.py")
field_trial = importlib.util.module_from_spec(SPEC)
sys.modules["field_trial"] = field_trial
SPEC.loader.exec_module(field_trial)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    write(tmp_path / "definitions" / "a.sqlx", 'config {\n  type: "table",\n  uniqueKey: ["day", "ip"]\n}\nSELECT 1\n')
    write(tmp_path / "definitions" / "b.sqlx", 'config {\n  type: "view"\n}\nSELECT * FROM ${ref("a")}\n')
    write(tmp_path / "definitions" / "c.sqlx", 'config {\n  type: "view",\n  name: "named_c"\n}\nSELECT * FROM ${ref("b")} JOIN ${ref("a")}\n')
    return tmp_path


def test_read_graph_reads_refs_names_and_keys(project):
    graph = field_trial.read_graph(project)
    assert sorted(graph) == ["a", "b", "named_c"]
    assert graph["named_c"]["reads"] == {"a", "b"}
    assert graph["a"]["unique_key"] == ["day", "ip"]
    assert graph["b"]["unique_key"] is None


def test_upstream_and_downstream_follow_chains(project):
    graph = field_trial.read_graph(project)
    assert field_trial.upstream(graph, "named_c") == {"a", "b"}
    assert field_trial.downstream(graph, "a") == {"b", "named_c"}
    assert field_trial.downstream(graph, "named_c") == set()


def test_score_sets_numbers_and_strings():
    task = {"expected": ["a", "b"], "answer_type": "set"}
    assert field_trial.score(task, ["B", "a"])["exact"]
    partial = field_trial.score(task, ["a", "x"])
    assert not partial["exact"] and partial["precision"] == 0.5 and partial["recall"] == 0.5
    assert field_trial.score(task, '["a", "b"]')["exact"]
    assert field_trial.score(task, "not json")["recall"] == 0.0
    number = {"expected": 7, "answer_type": "number"}
    assert field_trial.score(number, " 7 ")["exact"]
    assert not field_trial.score(number, "seven")["exact"]


def test_parse_arguments_types_values():
    assert field_trial.parse_arguments(["table=t", "max_bytes=4000", "flag=true"]) == {"table": "t", "max_bytes": 4000, "flag": True}


def test_tasks_on_the_fixture_have_expected_answers():
    tasks = field_trial.build_tasks()
    assert [t["id"] for t in tasks] == [f"T{i:02d}" for i in range(1, 11)]
    graph_tasks = [t for t in tasks if t["kind"] == "graph"]
    assert all(t["expected"] not in (None, [], 0) for t in graph_tasks)
    assert "_AllLogs" not in {name for t in tasks if t["id"] in ("T02", "T04") for name in t["expected"]}
    json.dumps(tasks)
