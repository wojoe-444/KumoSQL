"""A second start shows the saved project at once, whatever its size, while the refresh runs in the background.

The saved parse restores the models only; analysing them again (reading every model, tracing every column) is what
takes minutes on a large project, so the unscoped graph is also saved by commit and shown from that copy while the
project is analysed again. These tests make the refresh slow on purpose and check what the graph route answers.
"""

import json
import threading
import time
from threading import Thread
from urllib.request import Request

import pytest
from ui_http import urlopen

from kumosql import live_graph, repositories
from kumosql.ui import UIHandler, UIServer

from test_git_repo import commit, run

MODELS = 120  # mid-size: the point is the order of events, not the size


def project_files() -> dict:
    files = {
        "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: d\n",
        "includes/sources.js": "module.exports = { SOURCES: buildSources() };\n",
        # A computed declaration list: without Dataform's compilation the next load parses the project again.
        "definitions/decl.js": 'const { SOURCES } = require("includes/sources");\nSOURCES.forEach((s) => declare({ schema: s.schema, name: s.name }));\n',
        "definitions/m0.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS amount',
    }
    for index in range(1, MODELS):
        files[f"definitions/m{index}.sqlx"] = (
            f'config {{ type: "table" }}\nSELECT id, amount + {index} AS amount_{index} FROM ${{ref("m{index - 1}")}}')
    return files


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KUMOSQL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KUMOSQL_GIT_CACHE", str(tmp_path / "cache"))
    live_graph.clear_project()
    yield
    live_graph.clear_project()
    live_graph.forget_project()


@pytest.fixture
def remote(tmp_path):
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    run("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    run("clone", str(bare), str(work), cwd=tmp_path)
    run("checkout", "-b", "main", cwd=work)
    commit(work, project_files(), "first")
    return str(bare)


@pytest.fixture
def server():
    httpd = UIServer(("127.0.0.1", 0), UIHandler)
    thread = Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def get(base, path, timeout=20):
    with urlopen(Request(base + path), timeout=timeout) as response:
        return json.load(response)


def restart() -> None:
    """What a new process starts with: nothing in memory, the data folder untouched."""

    with live_graph._LOCK:
        live_graph._LOADED = None
        live_graph._PROJECT_CACHE.clear()
        getattr(live_graph, "_GRAPH_SAVED", {}).clear()
        live_graph._ANALYSIS.clear()


def first_start(remote, monkeypatch) -> dict:
    def no_compilation():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(live_graph, "_compiled_targets", lambda url: no_compilation)
    repo_id = repositories.replace([{"url": remote, "branch": "main"}])["repositories"][0]["id"]
    repositories.load(repo_id)
    payload = live_graph.graph_or_empty()
    assert len(payload["nodes"]) >= MODELS
    return payload


def test_second_start_answers_the_graph_before_the_slow_refresh_finishes(remote, server, monkeypatch):
    first_payload = first_start(remote, monkeypatch)
    restart()

    release = threading.Event()
    analysed, reloaded = [], []
    real_inputs, real_files = live_graph._graph_inputs, live_graph.pipeline_from_files

    def slow_inputs(*args, **kwargs):
        assert release.wait(60)
        analysed.append(1)
        return real_inputs(*args, **kwargs)

    def slow_files(*args, **kwargs):
        assert release.wait(60)  # the reload parses again: there is no compilation to read
        reloaded.append(1)
        return real_files(*args, **kwargs)

    monkeypatch.setattr(live_graph, "_graph_inputs", slow_inputs)
    monkeypatch.setattr(live_graph, "pipeline_from_files", slow_files)
    thread = repositories.autoload(background=True)
    try:
        started, nodes = time.monotonic(), 0
        while time.monotonic() - started < 20 and not nodes:
            try:
                nodes = len(get(server, "/api/graph", timeout=10).get("nodes", []))
            except OSError:
                pass
            time.sleep(0.05)
        assert nodes >= MODELS, "the saved project was not answerable"
        assert not analysed and not reloaded and not release.is_set(), "the answer waited for the refresh"

        # The server stays responsive while the refresh runs, and the sidebar says what is running.
        for path in ("/api/version", "/api/repositories", "/api/status", "/api/graph"):
            began = time.monotonic()
            get(server, path, timeout=10)
            assert time.monotonic() - began < 2, f"{path} waited for the refresh"
        busy = {item["label"] for item in get(server, "/api/status")["busy"]}
        assert "Building graph" in busy
        assert not analysed and not reloaded
    finally:
        release.set()
        thread.join(60)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and getattr(live_graph.loaded()["pipeline"], "_graph_state", "") != "fresh":
        get(server, "/api/graph")  # a request starts the refresh of the project the reload just parsed
        time.sleep(0.05)
    assert analysed and reloaded

    # Once analysed, the answer is the analysis, and it matches what the saved copy showed.
    final = get(server, "/api/graph")
    assert {n["id"] for n in final["nodes"]} == {n["id"] for n in first_payload["nodes"]}
    assert final["edges"] == first_payload["edges"] and final["column_lineage"] == first_payload["column_lineage"]


def test_the_saved_copy_of_the_graph_equals_the_analysis_it_came_from(remote, monkeypatch):
    first_start(remote, monkeypatch)
    pipeline = live_graph.loaded()["pipeline"]
    fresh = live_graph._graph_from_inputs(pipeline, live_graph._graph_inputs(pipeline, [], None), None)
    saved = live_graph._load_saved_graph(pipeline.content_key)
    assert saved is not None
    assert json.dumps(live_graph._graph_from_inputs(pipeline, saved, None), sort_keys=True) == json.dumps(fresh, sort_keys=True)


def test_a_damaged_saved_graph_is_a_miss_and_the_project_is_analysed_normally(remote, monkeypatch):
    first_start(remote, monkeypatch)
    key = live_graph.loaded()["pipeline"].content_key
    live_graph._graph_cache_file(key).write_text("not json", encoding="utf-8")
    restart()
    assert live_graph.restore_snapshot(key, "saved")
    assert len(live_graph.graph_or_empty()["nodes"]) >= MODELS
    assert live_graph.loaded()["pipeline"]._graph_state == "fresh"


def test_a_failed_refresh_is_reported_by_the_next_request_not_hidden(remote, monkeypatch):
    first_start(remote, monkeypatch)
    key = live_graph.loaded()["pipeline"].content_key
    restart()
    assert live_graph.restore_snapshot(key, "saved")
    real = live_graph._graph_inputs
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(live_graph, "_graph_inputs", broken)
    assert len(live_graph.graph_or_empty()["nodes"]) >= MODELS  # the saved copy
    pipeline = live_graph.loaded()["pipeline"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and pipeline._graph_state == "refreshing":
        time.sleep(0.02)
    assert pipeline._graph_state == "failed"
    assert len(live_graph.graph_or_empty()["nodes"]) >= MODELS  # analysed by the request itself
    assert len(calls) == 2 and pipeline._graph_state == "fresh"
