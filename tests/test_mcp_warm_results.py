"""The warm start beyond the column trace: saved table profiles, column types, expression index and file effects.

Every project here is generated. "Done again" is counted two ways that must agree: the counters of the index (what the
loader and the result memo report), and calls of the functions that do the work, counted from outside by wrapping them
(the first step of loading a file, ``_Profiler.profile``, ``googlesql_types.infer``, ``expression_search.model_part``).
No test looks at a clock.
"""

import json
import os
import threading
from pathlib import Path

import pytest

from kumosql import expression_search, googlesql_types, load_memo, mcp_cache, mcp_server, pipeline_loading, table_profile
from kumosql.mcp_session import Session, load_project

COUNT = 14  # a binary tree of models below ``raw``: node_1 reads raw, node_i reads node_(i // 2)
CASE = 'CASE WHEN amount > 10 THEN "big" WHEN amount > 5 THEN "mid" ELSE "small" END'


def definitions(count=COUNT, edits=None):
    """``{file: text}``: two declared sources, a raw model, ``count`` models in a tree, a plain ``.sql`` file."""

    files = {
        "src_a.sqlx": 'config { type: "declaration", schema: "raw", name: "src_a", columns: { id: "the id", amount: "the amount" } }\n',
        "src_b.sqlx": 'config { type: "declaration", schema: "raw", name: "src_b" }\n',
        "raw.sqlx": 'config { type: "table" }\nSELECT a.id, a.amount, "eu" AS region FROM ${ref("raw", "src_a")} AS a JOIN ${ref("raw", "src_b")} AS b ON a.id = b.id\n',
        "plain.sql": "SELECT id, amount FROM p.ds.node_3 WHERE amount > 1\n",
    }
    for i in range(1, count + 1):
        parent = "raw" if i == 1 else f"node_{i // 2}"
        extra = (edits or {}).get(i, "")
        files[f"node_{i}.sqlx"] = (
            f'config {{ type: "table" }}\nSELECT id, SUM(amount) AS amount, region, {CASE} AS size{extra} '
            f'FROM ${{ref("{parent}")}} WHERE amount > {i} GROUP BY id, region\n'
        )
    return files


LONG_AGO = 1_500_000_000_000_000_000  # a file this old is known by size and time alone (see ``load_memo.RACY_NS``)


def write_project(folder, files, aged=True):
    """A project on disk. ``aged`` gives every file an old modification time, as files of a checkout have when the server starts."""

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    for name, text in files.items():
        path = folder / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if aged:
        for path in folder.rglob("*"):
            if path.is_file():
                os.utime(path, ns=(LONG_AGO, LONG_AGO))
    return folder


def edit_file(path, old, new, tick):
    """Replace text in a file and give it an old modification time that differs from every earlier one (``tick`` counts the edits)."""

    path.write_text(path.read_text().replace(old, new))
    os.utime(path, ns=(LONG_AGO + tick * 10**9, LONG_AGO + tick * 10**9))


def descendants(number, count=COUNT):
    found = {number}
    for i in range(number + 1, count + 1):
        if i // 2 in found:
            found.add(i)
    return {f"p.ds.node_{i}" for i in found}


class Work:
    """Counts the calls that do the work, so what the index reports is checked from outside."""

    def __init__(self, monkeypatch):
        self.files = self.profiles = self.types = self.expressions = 0
        split, profile, infer, part = pipeline_loading._split_sqlx_sections, table_profile._Profiler.profile, googlesql_types.infer, expression_search.model_part

        def counting(name, real):
            def wrapped(*args, **kwargs):
                setattr(self, name, getattr(self, name) + 1)
                return real(*args, **kwargs)

            return wrapped

        monkeypatch.setattr(pipeline_loading, "_split_sqlx_sections", counting("files", split))
        monkeypatch.setattr(table_profile._Profiler, "profile", counting("profiles", profile))
        monkeypatch.setattr(googlesql_types, "infer", counting("types", infer))
        monkeypatch.setattr(expression_search, "model_part", counting("expressions", part))

    def reset(self):
        self.files = self.profiles = self.types = self.expressions = 0


def start(project, cache, *, interval=0.0, watch=True, wait=60.0):
    session = Session(loader=lambda: load_project(project), root=project)
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
    assert index.wait_ready(120)
    return server, index


def ask(server, name, **arguments):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    result = server.handle(message)["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def answers(server, count=COUNT):
    """What agents ask for every table: describe, trace, impact; and find_existing and check_query."""

    found = {}
    for i in range(1, count + 1):
        table = f"node_{i}"
        found[("describe_table", table)] = ask(server, "describe_table", table=table)
        found[("trace_column", table)] = ask(server, "trace_column", table=table, column="amount")
        found[("impact", table)] = ask(server, "impact", table=table, column="amount")
        found[("find_existing", table)] = ask(server, "find_existing", table=table)
    found[("describe_table", "raw")] = ask(server, "describe_table", table="raw")
    found[("impact", "raw")] = ask(server, "impact", table="raw", column="amount")
    found[("find_existing", "expression")] = ask(server, "find_existing", expression=CASE)
    found[("find_existing", "sql")] = ask(server, "find_existing", sql="SELECT id, SUM(amount) AS s, region FROM p.ds.node_2 WHERE amount > 2 GROUP BY id, region")
    found[("find_tables", "keys")] = ask(server, "find_tables", has_columns=["id", "amount"])
    found[("check_query", "node_2")] = ask(server, "check_query", sql="SELECT id, SUM(amount) FROM p.ds.node_2 GROUP BY id")
    return found


def done_again(index):
    """What the last build did, as plain numbers."""

    last = index.last
    return {
        "traced": last["traced"], "files_read": last["files_read"], "files_parsed": last["files_parsed"], "assets_resolved": last["assets_resolved"],
        "profiles": last["profiles_computed"], "types": last["types_computed"], "expressions": last["expression_computed"],
    }


NOTHING = {"traced": 0, "files_read": 0, "files_parsed": 0, "assets_resolved": 0, "profiles": 0, "types": 0, "expressions": 0}


# ------------------------------------------------------------------ the acceptance sentence


def test_cold_warm_and_incrementally_updated_sessions_answer_identically_and_redo_only_what_an_edit_reaches(tmp_path, monkeypatch):
    project, cache = write_project(tmp_path / "proj", definitions()), tmp_path / "cache"
    work = Work(monkeypatch)

    cold, cold_index = start(project, None)
    cold_answers = answers(cold)
    total = len(cold.session.pipeline().models)  # raw, the tree and plain.sql; the two sources are declarations
    assert total == COUNT + 2

    # the first start with a cache does everything, once
    work.reset()
    first, first_index = start(project, cache)
    assert (work.files, work.profiles, work.types, work.expressions) == (len(definitions()) - 1, total, total - 1, total)  # plain.sql has no config to split and no types (kind sql)
    assert first_index.last["files_replayed"] == 0 and first_index.last["traced"] == total
    assert answers(first) == cold_answers

    # a restart on the unchanged project: zero files read, zero parsed, zero models analysed, no profile, type or expression computed
    work.reset()
    warm, warm_index = start(project, cache)
    assert done_again(warm_index) == NOTHING
    assert (work.files, work.profiles, work.types, work.expressions) == (0, 0, 0, 0)
    assert warm_index.last["files_replayed"] == len(definitions())
    assert warm_index.last["profiles_replayed"] == total and warm_index.last["types_replayed"] == total - 1 and warm_index.last["expression_replayed"] == total
    status = warm_index.status()
    assert status["state"] == "ready" and status["results_cache"] == "loaded" and status["last_files_parsed"] == 0
    assert answers(warm) == cold_answers

    # one edit, then a restart: the changed file is the only one read and parsed, and only it and what is downstream is done again
    edit = project / "definitions" / "node_5.sqlx"
    edit_file(edit, "amount > 5", "amount > 50", 1)
    reference, _ = start(project, None)  # a cold start of the edited project
    reference_answers = answers(reference)
    assert reference_answers != cold_answers  # the edit changes some answers, so equal answers below mean something
    work.reset()
    updated, updated_index = start(project, cache)
    below = descendants(5)
    redone = done_again(updated_index)
    assert redone == {"traced": len(below), "files_read": 1, "files_parsed": 1, "assets_resolved": 1, "profiles": len(below), "types": len(below), "expressions": len(below)}
    assert sorted(updated_index.memo.traced) == sorted(below)
    assert (work.files, work.profiles, work.types, work.expressions) == (1, len(below), len(below), len(below))
    assert answers(updated) == reference_answers

    # the same edit noticed while the server runs (the refresh path) gives the same answers and the same counts
    live, live_index = start(project, cache)
    assert done_again(live_index) == NOTHING
    edit_file(edit, "amount > 50", "amount > 51", 2)
    other, _ = start(project, None)
    expected = answers(other)
    work.reset()
    assert live_index.check_for_edits()
    assert live_index.wait_ready(60)
    live.session  # the server adopts the rebuilt project on the next call
    assert answers(live) == expected
    assert done_again(live_index) == {"traced": len(below), "files_read": 1, "files_parsed": 1, "assets_resolved": 1, "profiles": len(below), "types": len(below), "expressions": len(below)}


def test_an_edit_that_adds_a_model_parses_only_the_new_file_and_gives_the_answers_of_a_cold_start(tmp_path, monkeypatch):
    project, cache = write_project(tmp_path / "proj", definitions(6)), tmp_path / "cache"
    start(project, cache)
    (project / "definitions" / "added.sqlx").write_text('config { type: "table" }\nSELECT id, amount FROM ${ref("node_2")} WHERE amount > 9\n')
    os.utime(project / "definitions" / "added.sqlx", ns=(LONG_AGO, LONG_AGO))
    cold, _ = start(project, None)
    work = Work(monkeypatch)
    server, index = start(project, cache)
    assert (work.files, index.last["files_read"], index.last["files_parsed"]) == (1, 1, 1)
    assert index.last["traced"] == 9  # a new name changes the project-wide facts every trace depends on, so every model is traced again (not a file cost)
    assert answers(server, 6) == answers(cold, 6)
    assert ask(server, "describe_table", table="added")[1] is False


def test_removing_a_file_gives_the_answers_of_a_cold_start(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(6)), tmp_path / "cache"
    start(project, cache)
    (project / "definitions" / "plain.sql").unlink()
    cold, _ = start(project, None)
    server, index = start(project, cache)
    assert index.last["files_read"] == 0 and index.last["files_parsed"] == 0
    assert answers(server, 6) == answers(cold, 6)


def test_a_same_size_edit_made_within_the_same_clock_tick_is_still_noticed(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(6), aged=False), tmp_path / "cache"
    start(project, cache)
    edit = project / "definitions" / "node_4.sqlx"
    before = edit.stat()
    edit.write_text(edit.read_text().replace("amount > 4", "amount > 7"))  # the same number of characters
    os.utime(edit, ns=(before.st_atime_ns, before.st_mtime_ns))  # and the same modification time
    assert (edit.stat().st_size, edit.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    cold, _ = start(project, None)
    server, index = start(project, cache)
    assert index.last["files_parsed"] == 1  # read again because it was too recent to trust by size and time
    assert answers(server, 6) == answers(cold, 6)


def test_a_file_that_is_old_enough_is_known_by_size_and_time_alone(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(4)), tmp_path / "cache"
    long_ago = LONG_AGO
    start(project, cache)
    _, index = start(project, cache)
    assert index.last["files_read"] == 0 and index.last["files_replayed"] == len(definitions(4))
    shots = mcp_cache.snapshot(mcp_cache.project_files(project))
    again = mcp_cache.snapshot(mcp_cache.project_files(project), {k: (m, s, "stale") for k, (m, s, _h) in shots.items()}, previous_taken_ns=long_ago + 10**13)
    assert {v[2] for v in again.values()} == {"stale"}  # size and time match and are old: not read again
    racy = mcp_cache.snapshot(mcp_cache.project_files(project), {k: (m, s, "stale") for k, (m, s, _h) in shots.items()}, previous_taken_ns=long_ago + 10**9)
    assert "stale" not in {v[2] for v in racy.values()}  # changed within two seconds of the snapshot: read again


# ------------------------------------------------------------------ what a saved file may and may not contain


def damaged(path, mutate):
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def test_a_corrupt_or_old_format_or_foreign_session_file_is_ignored_and_the_answers_equal_a_cold_start(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(6)), tmp_path / "cache"
    cold, _ = start(project, None)
    expected = answers(cold, 6)
    path = mcp_cache.session_file(mcp_cache.cache_file(cache, project))
    start(project, cache)
    good = path.read_text()

    def header(**changes):
        return lambda data: data["header"].update(changes)

    for damage in (
        lambda: path.write_text("{not json"),
        lambda: path.write_text(""),
        lambda: path.write_text("[]"),
        lambda: path.write_bytes(b"\x00\xff\xfe"),
        lambda: path.write_text("[" * 5000),
        lambda: (path.write_text(good), damaged(path, header(format="other"))),
        lambda: (path.write_text(good), damaged(path, header(loader=0))),
        lambda: (path.write_text(good), damaged(path, header(results=0))),
        lambda: (path.write_text(good), damaged(path, header(kumosql="0.0.0-other"))),
        lambda: (path.write_text(good), damaged(path, header(sqlglot="1.0.0"))),
        lambda: (path.write_text(good), damaged(path, lambda d: d.update(files=[]))),
        lambda: (path.write_text(good), damaged(path, lambda d: d["files"].update({"x": [1, 2]}))),
        lambda: (path.write_text(good), damaged(path, lambda d: d.update(taken_ns="soon"))),
    ):
        damage()
        server, index = start(project, cache)
        assert index.results_state in ("ignored", "saved")
        assert answers(server, 6) == expected
        assert mcp_cache.load_session(path) is not None  # replaced by a valid file
        assert index.last["files_parsed"] == len(definitions(6))  # nothing was trusted from the damaged file


def test_saved_entries_that_do_not_decode_are_done_again_and_the_rest_are_replayed(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(6)), tmp_path / "cache"
    cold, _ = start(project, None)
    expected = answers(cold, 6)
    start(project, cache)
    path = mcp_cache.session_file(mcp_cache.cache_file(cache, project))

    def spoil(data):
        loader = data["loader"]
        loader["definitions/node_2.sqlx"]["s1"]["events"] = [["model", {"target": "not a target"}]]
        loader["definitions/node_3.sqlx"]["s1"]["asset"] = [1, 2, 3, 4, 5]
        loader["definitions/node_4.sqlx"]["s2"] = [["diag", 1, 2, 3]]
        loader["definitions/node_5.sqlx"]["s1"]["events"] = "nonsense"
        for section, count in (("profiles", 2), ("types", 2), ("expression", 2)):
            for token in list(data["results"][section])[:count]:
                data["results"][section][token] = {"nonsense": True} if section != "types" else {"cols": [["a", "not a type", None]]}

    damaged(path, spoil)
    server, index = start(project, cache)
    assert index.last["files_parsed"] == 3 and index.last["assets_resolved"] >= 1
    assert index.last["profiles_computed"] == 2 and index.last["types_computed"] == 2 and index.last["expression_computed"] == 2
    assert answers(server, 6) == expected


def test_a_session_file_of_another_project_is_never_used_for_this_one(tmp_path):
    first = write_project(tmp_path / "one", definitions(6))
    second = write_project(tmp_path / "two", definitions(6, edits={3: ", amount + 1 AS extra"}))
    cache = tmp_path / "cache"
    start(first, cache)
    foreign = mcp_cache.session_file(mcp_cache.cache_file(cache, first)).read_text()
    mine = mcp_cache.session_file(mcp_cache.cache_file(cache, second))
    mine.write_text(foreign)
    cold, _ = start(second, None)
    server, index = start(second, cache)
    assert answers(server, 6) == answers(cold, 6)
    assert index.last["files_parsed"] == 1  # only the edited file: the others have the same path and text, which is all a saved effect depends on


def test_a_change_to_the_project_settings_resolves_every_asset_again_but_the_answers_equal_a_cold_start(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(6)), tmp_path / "cache"
    start(project, cache)
    (project / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\nnamePrefix: pre\n")
    os.utime(project / "workflow_settings.yaml", ns=(LONG_AGO + 10**9, LONG_AGO + 10**9))
    cold, _ = start(project, None)
    server, index = start(project, cache)
    assert index.last["files_parsed"] == len(definitions(6))  # the settings are part of what the first step read
    assert answers(server, 6) == answers(cold, 6)


def test_the_saved_files_never_go_into_the_project_and_the_folder_ignores_itself(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(4)), tmp_path / "cache"
    before = sorted(p.relative_to(project) for p in project.rglob("*"))
    start(project, cache)
    assert sorted(p.relative_to(project) for p in project.rglob("*")) == before
    names = sorted(p.name for p in cache.iterdir())
    assert any(n.endswith(".session.json") for n in names) and (cache / ".gitignore").read_text() == "*\n"
    assert not [n for n in names if n.endswith(".tmp")]


def test_a_second_unchanged_start_does_not_rewrite_the_saved_file(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(4)), tmp_path / "cache"
    start(project, cache)
    saved = mcp_cache.session_file(mcp_cache.cache_file(cache, project))
    before = saved.stat().st_mtime_ns
    _, index = start(project, cache)
    assert saved.stat().st_mtime_ns == before and index.results_state == "loaded"


# ------------------------------------------------------------------ the loader and the results alone


def view(pipeline):
    return (pipeline.models, pipeline.sources, pipeline.source_schema, pipeline.diagnostics, pipeline.default_project,
            pipeline.default_dataset, pipeline.default_location, pipeline.column_docs, pipeline.source_unique_keys)


def test_a_project_loaded_from_saved_effects_equals_a_cold_load_including_diagnostics_and_declarations(tmp_path):
    files = definitions(6)
    files["broken.sqlx"] = 'config { type: "table" }\nSELECT id FROM ${ref("no_such_table")}\n'
    files["keys.sqlx"] = 'config { type: "declaration", schema: "raw", name: "keys", assertions: { uniqueKey: ["id"] } }\n'
    files["dup_a.sqlx"] = 'config { type: "table", name: "dup" }\nSELECT 1 AS a\n'
    files["dup_b.sqlx"] = 'config { type: "table", name: "dup" }\nSELECT 2 AS a\n'
    files["computed.sqlx"] = 'config { type: "table", name: dynamic_name }\nSELECT 3 AS a\n'
    files["bad_config.sqlx"] = 'config { type: "banana" }\nSELECT 1\n'
    files["publish.js"] = 'publish("from_js", { type: "table" }).query(ctx => `SELECT 5 AS v`)\n'
    project = write_project(tmp_path / "proj", files)
    cold = load_project(project)
    shot = mcp_cache.snapshot(mcp_cache.project_files(project))
    first = load_memo.LoadMemo(files=shot, taken_ns=2**62)
    token = load_memo.CURRENT.set(first)
    try:
        recorded = load_project(project)
    finally:
        load_memo.CURRENT.reset(token)
    saved = json.loads(json.dumps(first.entries))
    again = load_memo.LoadMemo(saved, shot, 2**62)
    token = load_memo.CURRENT.set(again)
    try:
        replayed = load_project(project)
    finally:
        load_memo.CURRENT.reset(token)
    assert view(recorded) == view(cold) == view(replayed)
    assert again.read == [] and again.parsed == [] and again.resolved == []
    assert len(again.replayed) == len(first.parsed) and "definitions/publish.js#from_js" in again.replayed
    assert {"unsupported_ref", "duplicate_model", "invalid_config", "dynamic_config"} <= {d.code for d in cold.diagnostics}


def test_a_loader_memo_is_ignored_when_no_session_asks_for_one(tmp_path):
    project = write_project(tmp_path / "proj", definitions(3))
    assert load_memo.CURRENT.get() is None
    assert view(load_project(project)) == view(load_project(project))


def test_profiles_types_and_the_expression_index_replayed_for_part_of_a_project_equal_computing_them_all(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(), ), tmp_path / "cache"
    cold, _ = start(project, None)
    start(project, cache)
    edit_file(project / "definitions" / "node_6.sqlx", "amount > 6", "amount > 66", 1)
    partial, index = start(project, cache)
    assert 0 < index.last["profiles_replayed"] < index.last["profiles_replayed"] + index.last["profiles_computed"]
    reference, _ = start(project, None)
    assert partial.session.profiles() == reference.session.profiles() != cold.session.profiles()
    assert partial.session.column_types().types == reference.session.column_types().types
    assert partial.session.column_types().nullability == reference.session.column_types().nullability
    assert partial.session.records() == reference.session.records()
    from kumosql.mcp_expression import expression_index

    assert expression_index(partial.session).parts == expression_index(reference.session).parts


def test_a_change_to_a_declared_grain_computes_every_profile_again(tmp_path):
    project, cache = write_project(tmp_path / "proj", definitions(4)), tmp_path / "cache"
    start(project, cache)
    session = Session(loader=lambda: load_project(project), root=project, assume_grain={"node_2": ["id", "region"]})
    index = mcp_cache.Index(session, cache=mcp_cache.cache_file(cache, project), wait_seconds=60, log=lambda _t: None)
    index.start()
    assert index.wait_ready(120)
    assert index.last["profiles_replayed"] == 0 and index.last["profiles_computed"] > 0
    assert index.last["traced"] == 0  # the trace does not depend on the grain


def test_a_project_with_a_dependency_cycle_computes_the_models_on_it_every_time_and_replays_the_rest(tmp_path):
    files = definitions(4)
    files["cyc_a.sqlx"] = 'config { type: "table" }\nSELECT id FROM ${ref("cyc_b")}\n'
    files["cyc_b.sqlx"] = 'config { type: "table" }\nSELECT id FROM ${ref("cyc_a")}\n'
    project, cache = write_project(tmp_path / "proj", files), tmp_path / "cache"
    cold, _ = start(project, None)
    start(project, cache)
    server, index = start(project, cache)
    assert index.last["profiles_computed"] >= 2 and index.last["profiles_replayed"] >= 4
    assert answers(server, 4) == answers(cold, 4)


def test_a_profile_that_failed_is_not_saved():
    from kumosql.result_memo import profile_is_cacheable

    good = table_profile.TableProfile("t", table_profile.Grain(("id",), "derived"), (table_profile.AttributeMeaning("id", "col:s.id", "known"),), table_profile.RowScope())
    bad = table_profile.TableProfile("t", table_profile.Grain(reason="profile_error: ValueError"), (), table_profile.RowScope())
    assert profile_is_cacheable(good) and not profile_is_cacheable(bad)


def test_types_survive_a_round_trip_through_json_including_structs_and_arrays():
    from kumosql.result_memo import decode_type, encode_type

    for text in ("INT64", "ARRAY<STRING>", "STRUCT<a INT64, b ARRAY<STRUCT<c TIMESTAMP>>>", "RANGE<DATE>", "STRUCT<INT64, STRING>"):
        kind = googlesql_types.parse_type(text)
        assert kind is not None and decode_type(json.loads(json.dumps(encode_type(kind)))) == kind
    assert decode_type(None) is None


# ------------------------------------------------------------------ the public project


@pytest.mark.slow
def test_replaying_every_saved_result_of_the_106_model_public_project_equals_a_cold_start(tmp_path, monkeypatch):
    big = Path(__file__).resolve().parent / "fixtures" / "bq_corpora" / "security-analytics"
    cold, _ = start(big, None, watch=False)
    first, _ = start(big, tmp_path / "cache", watch=False)
    second, index = start(big, tmp_path / "cache", watch=False)
    assert index.last["profiles_computed"] == 0 and index.last["types_computed"] == 0 and index.last["expression_computed"] == 0
    assert index.last["profiles_replayed"] == len(cold.session.pipeline().models)
    from kumosql.mcp_expression import expression_index

    for server in (first, second):
        assert server.session.profiles() == cold.session.profiles()
        assert server.session.column_types().types == cold.session.column_types().types
        assert server.session.roles() == cold.session.roles()
        assert expression_index(server.session).parts == expression_index(cold.session).parts
    for table in sorted(cold.session.pipeline().models)[::20]:
        for tool in ("describe_table", "impact"):
            args = {"table": table} if tool == "describe_table" else {"table": table}
            assert ask(second, tool, **args) == ask(cold, tool, **args)


@pytest.mark.slow
def test_a_public_project_edited_in_the_middle_replays_the_rest_and_equals_a_cold_start(tmp_path):
    import shutil

    source = Path(__file__).resolve().parent / "fixtures" / "bq_corpora" / "security-analytics"
    project = tmp_path / "project"
    shutil.copytree(source, project)
    first, _ = start(project, tmp_path / "cache", watch=False)
    pipeline = first.session.pipeline()
    downstream = first.session.downstream()
    middle = max((m for m in pipeline.models if pipeline.models[m].path), key=lambda m: (len(downstream.get(m, ())) < 40, len(downstream.get(m, ()))))
    path = project / pipeline.models[middle].path
    path.write_text(path.read_text() + "\n-- a note added by the test\n")
    cold, _ = start(project, None, watch=False)
    server, index = start(project, tmp_path / "cache", watch=False)
    assert 0 < index.last["profiles_computed"] < len(pipeline.models) and index.last["profiles_replayed"] > 0
    assert index.last["files_parsed"] == 1
    from kumosql.mcp_expression import expression_index

    assert server.session.profiles() == cold.session.profiles()
    assert server.session.column_types().types == cold.session.column_types().types
    assert expression_index(server.session).parts == expression_index(cold.session).parts
    assert server.session.records() == cold.session.records()
    for table in sorted(pipeline.models)[::10]:
        for tool in ("describe_table", "impact"):
            assert ask(server, tool, table=table) == ask(cold, tool, table=table)


# ------------------------------------------------------------------ many writers and readers


def test_concurrent_saves_and_loads_of_the_session_file_never_show_a_half_written_file(tmp_path):
    path = mcp_cache.session_file(mcp_cache.cache_file(tmp_path / "cache", tmp_path / "proj"))
    errors, stop = [], threading.Event()

    def writer(number):
        for round_ in range(40):
            try:
                files = {f"/p/f{i}": (1, 2, "abc") for i in range(100 + round_)}
                mcp_cache.save_session(path, files, number, {f"k{i}": {"sha": "s"} for i in range(100 + round_)}, {"profiles": {}, "types": {}, "expression": {}})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    def reader():
        while not stop.is_set():
            found = mcp_cache.load_session(path)
            if found is not None and not (len(found["files"]) >= 100 and found["taken_ns"] in (0, 1, 2) and len(found["loader"]) >= 100):
                errors.append(AssertionError("a partial file was read"))

    readers = [threading.Thread(target=reader) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for thread in (*readers, *writers):
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in readers:
        thread.join()
    assert errors == [] and mcp_cache.load_session(path) is not None
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_servers_started_at_once_on_one_project_all_answer_and_leave_valid_files(tmp_path):
    import subprocess
    import sys

    project, cache = write_project(tmp_path / "proj", definitions(6)), tmp_path / "cache"
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "describe_table", "arguments": {"table": "node_3"}}}) + "\n"
    command = [sys.executable, "-m", "kumosql", "mcp", str(project), "--cache-dir", str(cache), "--warm-wait", "120"]
    for _round in range(2):  # the second round starts from the files the first one left
        processes = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        outputs = [p.communicate(request, timeout=180) for p in processes]
        for process, (out, _err) in zip(processes, outputs):
            assert process.returncode == 0
            assert json.loads(json.loads(out)["result"]["content"][0]["text"])["table"] == "p.ds.node_3"
        assert mcp_cache.load_session(mcp_cache.session_file(mcp_cache.cache_file(cache, project))) is not None
        assert mcp_cache.load_cache(mcp_cache.cache_file(cache, project)) is not None
    assert not [p for p in cache.iterdir() if p.name.endswith(".tmp")]
