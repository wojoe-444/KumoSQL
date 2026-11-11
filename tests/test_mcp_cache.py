"""The warm start of the agent-facing server: a saved column trace, a background build, a refresh after edits.

Every project here is generated. "Analysed" is counted two ways that must agree: the memo's list of models it traced,
and calls of ``qualify`` (the first step of tracing a model) counted from outside by wrapping it. No test looks at a clock.
"""

import json
import threading
from dataclasses import fields
from pathlib import Path

import pytest

from kumosql import mcp_answers, mcp_cache, mcp_server, pipeline as pipeline_module
from kumosql.mcp_session import Session, load_project

LAYERS = 4  # a binary tree of models below ``raw``: node_1 reads raw, node_i reads node_(i // 2)


def definitions(count=14, edits=None):
    """``{file: text}`` of ``count`` models; ``edits`` maps a node number to extra select-list text."""

    files = {"raw.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS amount, "eu" AS region\n'}
    for i in range(1, count + 1):
        parent = "raw" if i == 1 else f"node_{i // 2}"
        extra = (edits or {}).get(i, "")
        files[f"node_{i}.sqlx"] = (
            f'config {{ type: "table" }}\nSELECT id, SUM(amount) AS amount, region{extra} FROM ${{ref("{parent}")}} '
            f"WHERE amount > {i} GROUP BY id, region\n"
        )
    return files


def write_project(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    for name, text in files.items():
        path = folder / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


def descendants(number, count=14):
    found = {number}
    for i in range(number + 1, count + 1):
        if i // 2 in found:
            found.add(i)
    return {f"p.ds.node_{i}" for i in found}


class Counter:
    """Counts calls of ``pipeline.qualify``, one per traced model, so the memo's own numbers are checked from outside."""

    def __init__(self, monkeypatch):
        self.calls = 0
        real = pipeline_module.qualify

        def counted(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, "qualify", counted)


def start(project, cache, *, wait=30.0, loader=None, interval=0.0, watch=True):
    """A server over ``project`` with the warm-start index; returns ``(server, index)`` once the first build is over."""

    session = Session(loader=loader or (lambda: load_project(project)), root=project)
    index = mcp_cache.Index(
        session,
        cache=mcp_cache.cache_file(cache, project) if cache is not None else None,
        watch=(lambda: mcp_cache.project_files(project)) if watch else None,
        refresh_interval=interval,
        wait_seconds=wait,
        log=lambda _text: None,
    )
    server = mcp_server.AgentServer(session)
    index.start()
    assert index.wait_ready(60)
    return server, index


def ask(server, name, **arguments):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    result = server.handle(message)["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def answers(server, count=14):
    """What agents ask: describe, trace and impact for every table (the answers a cold start must equal)."""

    found = {}
    for i in range(1, count + 1):
        table = f"node_{i}"
        found[("describe_table", table)] = ask(server, "describe_table", table=table)
        found[("trace_column", table)] = ask(server, "trace_column", table=table, column="amount")
        found[("impact", table)] = ask(server, "impact", table=table, column="amount")
    found[("impact", "raw")] = ask(server, "impact", table="raw", column="amount")
    found[("describe_table", "raw")] = ask(server, "describe_table", table="raw")
    found[("trace_column", "node_1")] = ask(server, "trace_column", table="node_1", column="region")
    return found


def analysis_of(server):
    analysis = server.session.pipeline()._analyse()
    return {
        f.name: (getattr(analysis, f.name) if f.name not in ("parsed",) else sorted(analysis.parsed))
        for f in fields(analysis)
    }


# ------------------------------------------------------------------ the acceptance sentence


def test_a_second_start_on_an_unchanged_project_analyses_no_model_and_reports_ready(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions())
    cache = tmp_path / "cache"
    counter = Counter(monkeypatch)

    first, index1 = start(project, cache)
    assert (len(index1.memo.traced), len(index1.memo.reused)) == (15, 0) and counter.calls == 15
    cold_answers, cold_analysis = answers(first), analysis_of(first)

    counter.calls = 0
    second, index2 = start(project, cache)
    assert counter.calls == 0 and index2.memo.traced == [] and len(index2.memo.reused) == 15
    status = index2.status()
    assert status["state"] == "ready" and status["percent"] == 100 and status["cache"] == "loaded"
    assert (status["last_traced"], status["last_reused"]) == (0, 15)
    assert answers(second) == cold_answers
    assert analysis_of(second) == cold_analysis  # the same records, order and diagnostics, not just the same answers


def test_after_an_edit_only_that_file_and_the_models_downstream_of_it_are_analysed_again(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions())
    counter = Counter(monkeypatch)
    server, index = start(project, tmp_path / "cache")
    counter.calls = 0

    (project / "definitions" / "node_2.sqlx").write_text(definitions(edits={2: ", MAX(amount) AS peak"})["node_2.sqlx"])
    body, failed = ask(server, "describe_table", table="node_2")  # the call notices the edit and waits for the rebuild
    assert not failed and "peak" in json.dumps(body)

    assert set(index.memo.traced) == descendants(2) == {"p.ds.node_2", "p.ds.node_4", "p.ds.node_5", "p.ds.node_8", "p.ds.node_9", "p.ds.node_10", "p.ds.node_11"}
    assert counter.calls == 7 and len(index.memo.reused) == 8  # raw and the models beside and above node_2 were replayed

    cold_project = write_project(tmp_path / "cold", definitions(edits={2: ", MAX(amount) AS peak"}))
    cold, _ = start(cold_project, None)
    updated = answers(server)
    assert updated == answers(cold)
    assert analysis_of(server) == analysis_of(cold)


def test_an_edit_to_a_leaf_analyses_one_model_and_an_edit_to_the_root_analyses_every_model(tmp_path):
    project = write_project(tmp_path / "proj", definitions())
    server, index = start(project, tmp_path / "cache")
    leaf = definitions()["node_14.sqlx"].replace("amount > 14", "amount > 15")
    (project / "definitions" / "node_14.sqlx").write_text(leaf)
    ask(server, "describe_table", table="node_14")
    assert index.memo.traced == ["p.ds.node_14"]

    (project / "definitions" / "raw.sqlx").write_text('config { type: "table" }\nSELECT 1 AS id, 2 AS amount, "eu" AS region, 3 AS extra\n')
    body, _ = ask(server, "describe_table", table="raw")
    assert len(index.memo.traced) == 15 and "extra" in json.dumps(body)


def test_a_model_added_or_removed_gives_the_answers_of_a_cold_start(tmp_path):
    project = write_project(tmp_path / "proj", definitions())
    server, index = start(project, tmp_path / "cache")
    (project / "definitions" / "node_14.sqlx").unlink()
    (project / "definitions" / "node_15.sqlx").write_text(definitions(15)["node_15.sqlx"])
    ask(server, "describe_table", table="node_1")
    cold, _ = start(write_project(tmp_path / "cold", {k: v for k, v in definitions(15).items() if k != "node_14.sqlx"}), None)
    assert answers(server, 13) == answers(cold, 13)
    assert ask(server, "describe_table", table="node_15") == ask(cold, "describe_table", table="node_15")
    assert ask(server, "describe_table", table="node_14")[1]  # gone
    assert analysis_of(server) == analysis_of(cold)


def test_saving_a_file_without_changing_it_does_not_rebuild(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions())
    server, index = start(project, tmp_path / "cache")
    counter = Counter(monkeypatch)
    path = project / "definitions" / "node_3.sqlx"
    path.write_text(path.read_text())  # a new modification time, the same content
    assert index.check_for_edits() is False and not index.building and counter.calls == 0


def test_a_call_notices_an_edit_at_most_once_per_refresh_interval(tmp_path):
    project = write_project(tmp_path / "proj", definitions(4))
    now = [100.0]
    session = Session(loader=lambda: load_project(project), root=project)
    index = mcp_cache.Index(session, cache=None, watch=lambda: mcp_cache.project_files(project), refresh_interval=10, clock=lambda: now[0], log=lambda _t: None)
    server = mcp_server.AgentServer(session)
    index.start()
    assert index.wait_ready(60)
    (project / "definitions" / "node_4.sqlx").write_text(definitions(4, {4: ", 1 AS one"})["node_4.sqlx"])
    now[0] += 11
    assert index.check_for_edits() is True  # the interval passed: the edit is seen
    assert index.wait_ready(60)
    (project / "definitions" / "node_4.sqlx").write_text(definitions(4, {4: ", 2 AS two"})["node_4.sqlx"])
    now[0] += 1
    assert index.check_for_edits() is False  # within the interval: not looked at
    now[0] += 20
    body, _ = ask(server, "describe_table", table="node_4")
    assert "two" in json.dumps(body)


# ------------------------------------------------------------------ warming


def test_a_call_while_the_index_is_warming_says_index_warming_with_a_percentage(tmp_path):
    project = write_project(tmp_path / "proj", definitions(4))
    release = threading.Event()

    def slow_loader():
        release.wait(30)
        return load_project(project)

    session = Session(loader=slow_loader, root=project)
    index = mcp_cache.Index(session, cache=None, wait_seconds=0, log=lambda _t: None)
    server = mcp_server.AgentServer(session)
    index.start()
    try:
        body, failed = ask(server, "describe_table", table="node_1")
        assert not failed
        assert body["answer"].startswith("index warming, ") and "% done" in body["answer"]
        assert body["index"]["state"] == "warming" and body["index"]["percent"] == 2 and body["index"]["stage"] == "loading project"
        assert body["index"]["ask_again_in_seconds"] >= 1 and "again" in body["next"][0]
        assert "table" not in body  # no answer was given from an unfinished analysis
        assert len(json.dumps(body)) <= mcp_answers.DEFAULT_BYTES
    finally:
        release.set()
    assert index.wait_ready(60)
    body, failed = ask(server, "describe_table", table="node_1")
    assert not failed and body["table"] == "p.ds.node_1" and "index" not in body


def test_a_call_in_the_middle_of_the_trace_reports_the_stage_and_a_percentage_between_the_ends(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions(6))
    reached, release = threading.Event(), threading.Event()
    real = pipeline_module.qualify
    calls = []

    def stalled(*args, **kwargs):
        calls.append(1)
        if len(calls) == 4:  # three models are traced, the fourth waits
            reached.set()
            release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "qualify", stalled)
    session = Session(loader=lambda: load_project(project), root=project)
    index = mcp_cache.Index(session, cache=None, wait_seconds=0, log=lambda _t: None)
    server = mcp_server.AgentServer(session)
    index.start()
    try:
        assert reached.wait(30)
        body, failed = ask(server, "trace_column", table="node_2", column="amount")
        assert not failed and body["answer"].startswith("index warming, ")
        assert body["index"]["stage"] == "tracing columns" and 5 < body["index"]["percent"] < 90
        assert f"{body['index']['percent']}% done" in body["answer"]
    finally:
        release.set()
    assert index.wait_ready(60) and index.status()["percent"] == 100


def test_a_call_waits_for_a_build_that_finishes_within_the_wait_and_then_answers(tmp_path):
    project = write_project(tmp_path / "proj", definitions(4))
    release = threading.Event()
    session = Session(loader=lambda: (release.wait(30), load_project(project))[1], root=project)
    index = mcp_cache.Index(session, cache=None, wait_seconds=30, log=lambda _t: None)
    server = mcp_server.AgentServer(session)
    index.start()
    threading.Timer(0.2, release.set).start()
    body, failed = ask(server, "describe_table", table="node_1")  # blocks until the build is done, then answers
    assert not failed and body["table"] == "p.ds.node_1"


def test_percent_follows_the_trace_and_never_goes_backwards(tmp_path):
    project = write_project(tmp_path / "proj", definitions(6))
    server, index = start(project, tmp_path / "cache")
    memo = index.memo
    assert memo.percent() == 100 and memo.done == memo.total == 7
    seen = []
    memo.state, memo.total = "tracing", 4
    index._idle.clear()
    index.stage = "tracing columns"
    for done in range(0, 5):
        memo.done = done
        seen.append(index.percent())
    index._idle.set()
    assert seen == sorted(seen) and seen[0] == 5 and seen[-1] <= 90


def test_a_project_that_cannot_be_loaded_ends_the_warming_and_reports_the_error(tmp_path):
    server, index = start(tmp_path / "missing", None, watch=False)
    body, failed = ask(server, "describe_table", table="x")
    assert failed and "not found" in body["error"] and index.status()["state"] == "ready"


# ------------------------------------------------------------------ the saved file


def test_a_corrupt_cache_file_is_ignored_and_written_again(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions(6))
    cache = tmp_path / "cache"
    path = mcp_cache.cache_file(cache, project)
    cold, _ = start(project, None)
    expected = answers(cold, 6)
    counter = Counter(monkeypatch)
    for damage in ("{not json", "", "[]", '{"header": {"format": "other"}}', "\x00\xff", "[" * 5000):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(damage, encoding="utf-8", errors="replace")
        counter.calls = 0
        server, index = start(project, cache)
        assert index.cache_state in ("ignored", "saved") and counter.calls == 7
        assert answers(server, 6) == expected
        assert mcp_cache.load_cache(path) is not None  # replaced by a valid file
    counter.calls = 0
    start(project, cache)
    assert counter.calls == 0


def test_a_cache_entry_that_does_not_decode_is_traced_again_and_the_rest_are_reused(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions(6))
    cache = tmp_path / "cache"
    cold, _ = start(project, cache)
    expected = answers(cold, 6)
    path = mcp_cache.cache_file(cache, project)
    data = json.loads(path.read_text())
    data["entries"]["p.ds.node_3"]["delta"] = {"records": "nonsense"}
    data["entries"]["p.ds.node_5"]["delta"]["records"][0][1] = [["t", 1, []]]  # a column that is not a string
    path.write_text(json.dumps(data))
    counter = Counter(monkeypatch)
    server, index = start(project, cache)
    assert sorted(index.memo.traced) == ["p.ds.node_3", "p.ds.node_5"] and counter.calls == 2
    assert answers(server, 6) == expected


def test_a_saved_file_from_another_kumosql_or_sqlglot_version_is_not_read(tmp_path, monkeypatch):
    import kumosql

    project = write_project(tmp_path / "proj", definitions(4))
    cache = tmp_path / "cache"
    start(project, cache)
    counter = Counter(monkeypatch)
    monkeypatch.setattr(kumosql, "__version__", "99.0.0")
    server, index = start(project, cache)
    assert index.cache_state in ("ignored", "saved") and counter.calls == 5  # every model traced again, none replayed


def test_a_change_to_the_project_settings_traces_every_model_again(tmp_path, monkeypatch):
    project = write_project(tmp_path / "proj", definitions(4))
    cache = tmp_path / "cache"
    start(project, cache)
    (project / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: other\n")
    counter = Counter(monkeypatch)
    _, index = start(project, cache)
    assert counter.calls == 5 and index.memo.reused == []


def test_models_on_a_dependency_cycle_are_traced_every_time_and_the_others_are_replayed(tmp_path, monkeypatch):
    files = definitions(4)
    files["loop_a.sqlx"] = 'config { type: "table" }\nSELECT id FROM ${ref("loop_b")}\n'
    files["loop_b.sqlx"] = 'config { type: "table" }\nSELECT id FROM ${ref("loop_a")}\n'
    project = write_project(tmp_path / "proj", files)
    cache = tmp_path / "cache"
    cold, _ = start(project, cache)
    counter = Counter(monkeypatch)
    again, index = start(project, cache)
    assert sorted(index.memo.traced) == ["p.ds.loop_a", "p.ds.loop_b"] and counter.calls == 2
    assert analysis_of(again) == analysis_of(cold)


def test_the_saved_index_never_goes_into_the_project_and_its_folder_ignores_itself(tmp_path):
    project = write_project(tmp_path / "proj", definitions(4))
    before = sorted(p.relative_to(project) for p in project.rglob("*"))
    cache = tmp_path / "cache"
    start(project, cache)
    assert sorted(p.relative_to(project) for p in project.rglob("*")) == before  # the server stayed read-only
    assert (cache / ".gitignore").read_text() == "*\n"
    saved = json.loads(mcp_cache.cache_file(cache, project).read_text())
    assert saved["header"]["format"] == mcp_cache.CACHE_NAME and set(saved) == {"header", "fingerprint", "entries"}
    assert str(tmp_path) not in json.dumps(saved)  # names and hashes only, no machine paths


def test_a_cache_folder_inside_the_project_is_not_mistaken_for_an_edit(tmp_path):
    project = write_project(tmp_path / "proj", definitions(4))
    cache = project / "state"
    session = Session(loader=lambda: load_project(project), root=project)
    index = mcp_cache.Index(
        session, cache=mcp_cache.cache_file(cache, project), watch=lambda: mcp_cache.project_files(project, skip=[cache]), log=lambda _t: None
    )
    index.start()
    assert index.wait_ready(60) and (cache / ".gitignore").exists()
    assert index.check_for_edits() is False


def test_the_cache_option_and_the_warm_start_flags_are_wired_into_the_command(tmp_path):
    import io
    import sys

    project = write_project(tmp_path / "proj", definitions(4))
    cache = tmp_path / "cache"
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "describe_table", "arguments": {"table": "node_1"}}}) + "\n"
    for _ in range(2):
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO(request), io.StringIO()
        try:
            assert mcp_server.main([str(project), "--cache-dir", str(cache), "--warm-wait", "60"]) == 0
            reply = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        assert json.loads(reply["result"]["content"][0]["text"])["table"] == "p.ds.node_1"
    assert mcp_cache.cache_file(cache, project).is_file()
    no_cache = tmp_path / "none"
    sys.stdin, sys.stdout = io.StringIO(request), io.StringIO()
    try:
        mcp_server.main([str(project), "--cache-dir", str(no_cache), "--no-cache", "--warm-wait", "60"])
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    assert not no_cache.exists()


# ------------------------------------------------------------------ the public project


@pytest.mark.slow
def test_replaying_the_saved_trace_of_the_106_model_public_project_equals_a_cold_analysis(tmp_path, monkeypatch):
    big = Path(__file__).resolve().parent / "fixtures" / "bq_corpora" / "security-analytics"
    cold, _ = start(big, None, watch=False)
    counter = Counter(monkeypatch)
    first, _ = start(big, tmp_path / "cache", watch=False)
    assert counter.calls == 106
    counter.calls = 0
    second, index = start(big, tmp_path / "cache", watch=False)
    assert counter.calls == 0 and len(index.memo.reused) == 106
    expected = analysis_of(cold)
    # reverse_lineage is built from sets, whose order is not the same twice; every other field must match, in order
    for server in (first, second):
        found = analysis_of(server)
        assert {k: list(v.items()) if isinstance(v, dict) and k != "reverse_lineage" else v for k, v in found.items()} == {
            k: list(v.items()) if isinstance(v, dict) and k != "reverse_lineage" else v for k, v in expected.items()
        }
    for table in sorted(cold.session.pipeline().models)[::20]:
        answer = ask(cold, "describe_table", table=table)
        assert not answer[1] and ask(second, "describe_table", table=table) == answer
