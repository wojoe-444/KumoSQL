"""Tags on objects inside datasets: manual tags, batch tag rules, and the scope field ``tag``."""

import json
import threading
import time
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from kumosql import bigquery_catalog as bq
from kumosql import live_graph, scopes, state, tags
from kumosql.ui import UIHandler, UIServer

from ui_http import urlopen

FILES = {
    "dataform.json": '{"defaultDataset": "stg", "defaultProject": "proj"}',
    "definitions/stg_orders.sqlx": 'config { type: "table" }\nSELECT id FROM `proj.raw.orders`',
    "definitions/old_report.sqlx": 'config { type: "view", schema: "RETIRED" }\nSELECT id FROM ${ref("stg_orders")}',
}
TABLES = {
    "datasets\x1fbrowsable\x1fp1": ["ignored"],
    "tables\x1fp1\x1fRETIRED": [{"id": "legacy_t", "type": "TABLE"}, {"id": "legacy_fn", "type": "UDF"}],
    "tables\x1fp1\x1fprod": [{"id": "orders", "type": "TABLE"}, {"id": "load_orders", "type": "PROCEDURE"}],
}


REAL_PEEK = bq.peek
REAL_CACHED = bq.cached


def settle_inventory():
    """Wait for the background catalog fill (``tags.start_inventory``) that an earlier snapshot started.

    It is a module-level thread: left running, it writes its errors into the next test's ``tags._inventory``, clears
    that test's ``running`` flag while the test's own fill is still going, and calls BigQuery after this test's stubs
    are undone."""

    for thread in threading.enumerate():
        if thread.name == "tag-inventory":
            thread.join(30)


def until_synced(seconds=30.0):
    """Snapshots until the background fill has finished (or ``seconds`` passed, so a loaded machine is not a failure)."""

    deadline = time.monotonic() + seconds
    while True:
        snap = tags.snapshot()
        if not snap["syncing"] or time.monotonic() > deadline:
            return snap
        time.sleep(0.05)


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    settle_inventory()
    live_graph.clear_project()
    tags._inventory.update(running=False, errors={}, failed_at={})

    def no_network(*args, **kwargs):
        raise bq.CatalogError("offline in tests")

    monkeypatch.setattr(bq, "cached", no_network)
    state.set_section("bigquery", {"projects": ["p1"]})

    def peek(key):
        if key == "datasets\x1fbrowsable\x1fp1":
            return {"data": [{"id": "RETIRED"}, {"id": "prod"}]}
        return {"data": TABLES[key]} if key in TABLES else None

    monkeypatch.setattr(bq, "peek", peek)
    yield
    settle_inventory()  # while this test's stubs are still in place
    live_graph.clear_project()


def retired_rule(tag="Retired", **extra):
    return {"tag": tag, "rule": {"field": "dataset", "op": "eq", "value": "retired"}, **extra}


def test_rule_tags_everything_in_a_dataset_including_functions_and_procedures():
    tags.save_rules([retired_rule()])
    snap = tags.snapshot()
    assert set(snap["objects"]) == {"p1.retired.legacy_t", "p1.retired.legacy_fn"}
    assert snap["objects"]["p1.retired.legacy_fn"] == {"manual": [], "rules": ["Retired"]}
    assert snap["rules"][0]["matched"] == 2 and snap["tags"] == [{"tag": "Retired", "count": 2}]


def test_rules_follow_the_data_without_being_saved_again():
    tags.save_rules([retired_rule()])
    TABLES["tables\x1fp1\x1fRETIRED"].append({"id": "later", "type": "VIEW"})
    try:
        assert "p1.retired.later" in tags.snapshot()["objects"]
    finally:
        TABLES["tables\x1fp1\x1fRETIRED"].pop()


def test_manual_tags_are_kept_apart_from_rule_tags_and_reuse_casing():
    tags.save_rules([retired_rule()])
    tags.change_manual(["p1.retired.legacy_t", "p1.prod.orders"], add=["Retired", "pii"])
    snap = tags.change_manual(["P1.prod.orders"], add=["PII", "  Needs   review "])
    assert snap["objects"]["p1.prod.orders"] == {"manual": ["Retired", "pii", "Needs review"], "rules": []}
    assert snap["objects"]["p1.retired.legacy_t"] == {"manual": ["Retired", "pii"], "rules": ["Retired"]}
    snap = tags.change_manual(["p1.prod.orders"], remove=["RETIRED", "pii"])
    assert snap["objects"]["p1.prod.orders"]["manual"] == ["Needs review"]
    snap = tags.change_manual(["p1.prod.orders"], remove=["needs review"])
    assert "p1.prod.orders" not in snap["objects"]


def test_tag_validation():
    for bad in ("", "   ", "x" * 61, 3):
        with pytest.raises(ValueError):
            tags.parse_tag(bad)
    with pytest.raises(ValueError):
        tags.change_manual(["p1.prod.orders"])
    with pytest.raises(ValueError):
        tags.change_manual([], add=["a"])


def test_rules_reuse_the_scope_builder_nesting_lists_and_saved_scopes():
    scopes.save_scope(scopes.Scope("legacy_names", rule={"field": "name", "op": "prefix", "value": "legacy"}))
    tags.save_rules([
        {"tag": "Legacy", "rule": {"all": [{"scope": "legacy_names"}, {"not": {"field": "type", "op": "eq", "value": "UDF"}}]}},
        {"tag": "Routine", "rule": {"field": "type", "op": "in", "value": ["udf", "PROCEDURE"]}},
        {"tag": "Pattern", "rule": {"field": "project", "op": "glob", "value": "p*"}},
    ])
    objects = tags.snapshot()["objects"]
    assert objects["p1.retired.legacy_t"]["rules"] == ["Legacy", "Pattern"]
    assert objects["p1.retired.legacy_fn"]["rules"] == ["Routine", "Pattern"]
    assert objects["p1.prod.load_orders"]["rules"] == ["Routine", "Pattern"]
    # An edit to the scope applies to the rule on the next read.
    scopes.save_scope(scopes.Scope("legacy_names", rule={"field": "name", "op": "prefix", "value": "orders"}))
    assert tags.snapshot()["objects"]["p1.prod.orders"]["rules"] == ["Legacy", "Pattern"]


@pytest.mark.parametrize("rule, message", [
    ({"field": "tag", "op": "eq", "value": "x"}, "tags are what the rule produces"),
    ({"field": "user_email", "op": "eq", "value": "x"}, "no field 'user_email'"),
    ({"scope": "missing"}, "not saved"),
])
def test_rules_that_cannot_run_on_objects_are_refused(rule, message):
    with pytest.raises(ValueError, match=message):
        tags.save_rules([{"tag": "X", "rule": rule}])
    assert tags.list_rules() == []


def test_a_scope_edited_later_to_use_tags_marks_the_rule_broken_instead_of_looping():
    scopes.save_scope(scopes.Scope("s", rule={"field": "name", "op": "eq", "value": "orders"}))
    tags.save_rules([{"tag": "X", "rule": {"scope": "s"}}])
    scopes.save_scope(scopes.Scope("s", rule={"field": "tag", "op": "eq", "value": "X"}))
    row = tags.snapshot()["rules"][0]
    assert row["matched"] == 0 and "cannot use 'tag'" in row["error"]


def test_dataform_models_are_tagged_under_every_name_the_graph_uses():
    live_graph.load_files(FILES, "t")
    tags.save_rules([retired_rule()])
    snap = tags.snapshot()
    assert snap["objects"]["retired.old_report"]["rules"] == ["Retired"]
    tags.change_manual(["RETIRED.old_report"], add=["Review"])  # the model key, as a graph node id shows it
    assert tags.snapshot()["objects"]["retired.old_report"]["manual"] == ["Review"]


def test_a_model_without_a_project_joins_the_one_bigquery_object_it_names():
    files = {**FILES, "definitions/legacy_t.sqlx": 'config { type: "table", schema: "RETIRED" }\nSELECT 1 AS id'}
    live_graph.load_files(files, "t")
    tags.change_manual(["RETIRED.legacy_t"], add=["Mine"])
    objects = tags.snapshot()["objects"]
    assert objects["p1.retired.legacy_t"]["manual"] == ["Mine"] and "retired.legacy_t" not in objects
    merged = tags.collect_objects()["p1.retired.legacy_t"]
    assert merged["source"] == ["bigquery", "dataform"] and merged["kind"] == "table" and merged["type"] == "TABLE"


def test_tag_is_a_scope_field_for_models():
    live_graph.load_files(FILES, "t")
    tags.save_rules([retired_rule()])
    tags.change_manual(["stg_orders"], add=["Gold"])
    pipeline = live_graph.loaded()["pipeline"]
    assert "tag" in {info.name for info in scopes.discover_fields(pipeline)}
    gold = scopes.Scope("gold", rule={"field": "tag", "op": "eq", "value": "gold"})
    retired = scopes.Scope("r", rule={"field": "tag", "op": "in", "value": ["Retired", "x"]})
    assert pipeline.scope_keys(gold) == {"stg_orders"}
    assert {k.split(".")[-1] for k in pipeline.scope_keys(retired)} == {"old_report"}


def test_preview_does_not_save():
    result = tags.preview_rule(retired_rule())
    assert result["matched"] == 2 and result["examples"] == ["p1.RETIRED.legacy_fn", "p1.RETIRED.legacy_t"]
    assert tags.list_rules() == []


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


def call(base, method, path, payload=None):
    request = Request(base + path, method=method, headers={"Content-Type": "application/json"},
                      data=None if payload is None else json.dumps(payload).encode())
    try:
        with urlopen(request) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def test_http_round_trip(server):
    status, saved = call(server, "PUT", "/api/settings/tag_rules", [retired_rule()])
    assert status == 200 and saved[0]["tag"] == "Retired"
    assert call(server, "GET", "/api/settings")[1]["tag_rules"] == saved
    status, snap = call(server, "PUT", "/api/tags", {"keys": ["p1.prod.orders"], "add": ["Gold"]})
    assert status == 200 and snap["objects"]["p1.prod.orders"]["manual"] == ["Gold"]
    assert call(server, "GET", "/api/tags")[1]["tags"] == [{"tag": "Gold", "count": 1}, {"tag": "Retired", "count": 2}]
    assert call(server, "POST", "/api/tag-rules/preview", retired_rule())[1]["matched"] == 2
    assert call(server, "PUT", "/api/tags", {"keys": ["x"]})[0] == 400
    assert call(server, "PUT", "/api/settings/tag_rules", [{"tag": "", "rule": {}}])[0] == 400
    with urlopen(server + "/assets/tags.js") as response:
        assert response.status == 200 and b"KumoTags" in response.read()


def test_catalog_lists_functions_and_procedures_after_tables(monkeypatch):
    def fake_list(path, key, params=None):
        if key == "tables":
            return [{"tableReference": {"tableId": "t"}, "type": "VIEW"}]
        return [{"routineReference": {"routineId": "f"}, "routineType": "SCALAR_FUNCTION"},
                {"routineReference": {"routineId": "p"}, "routineType": "PROCEDURE"}]

    monkeypatch.setattr(bq, "_list", fake_list)
    assert bq.list_tables("p", "d") == [
        {"id": "t", "type": "VIEW"}, {"id": "f", "type": "UDF"}, {"id": "p", "type": "PROCEDURE"}]

    def no_routines(path, key, params=None):
        if key == "routines":
            raise bq.CatalogError("denied", 403)
        return fake_list(path, key)

    monkeypatch.setattr(bq, "_list", no_routines)
    assert bq.list_tables("p", "d") == [{"id": "t", "type": "VIEW"}]


def test_a_rule_can_tag_what_a_sql_query_returns_using_the_scope_query_cache(monkeypatch):
    from kumosql import scope_queries

    calls = []

    def runner(sql, column, project, max_bytes):
        calls.append(project)
        return scope_queries.QueryRun(["P1.prod.orders", "p1.prod.load_orders"], "full_name", 1, 1)

    state.set_section("bigquery", {"projects": ["p1"], "billingProject": "bill"})
    scope_queries.clear_cache()
    monkeypatch.setattr(scope_queries, "RUNNER", runner)
    tags.save_rules([{"tag": "Listed", "rule": {"field": "full_name", "op": "in_query", "query": "SELECT n FROM x"}}])
    for _ in range(2):
        objects = tags.snapshot()["objects"]
    assert set(objects) == {"p1.prod.orders", "p1.prod.load_orders"} and calls == ["bill"]
    scope_queries.clear_cache()


def test_a_failing_query_marks_only_that_rule(monkeypatch):
    from kumosql import scope_queries

    state.set_section("bigquery", {"projects": ["p1"]})  # no billing project
    scope_queries.clear_cache()
    tags.save_rules([
        {"tag": "Listed", "rule": {"field": "full_name", "op": "in_query", "query": "SELECT n FROM x"}},
        retired_rule(),
    ])
    rows = tags.snapshot()["rules"]
    assert rows[0]["matched"] == 0 and "billing project" in rows[0]["error"] and rows[1]["matched"] == 2


def test_rule_tags_appear_as_soon_as_a_dataset_is_first_listed(server, monkeypatch):
    """The explorer reads tags before a dataset is opened, then again after its tables are saved to the catalog."""

    call(server, "PUT", "/api/settings/tag_rules", [retired_rule()])
    saved = dict(TABLES)
    del TABLES["tables\x1fp1\x1fRETIRED"]
    try:
        assert call(server, "GET", "/api/tags")[1]["objects"] == {}
    finally:
        TABLES.update(saved)
    assert set(call(server, "GET", "/api/tags")[1]["objects"]) == {"p1.retired.legacy_t", "p1.retired.legacy_fn"}


def test_the_explorer_rereads_tags_after_loading_a_dataset():
    from importlib.resources import files

    script = files("kumosql").joinpath("static", "browse.js").read_text(encoding="utf-8")
    assert script.count("await window.KumoTags?.load()") >= 1 and "KumoTags?.load().then(() => { if (node.item.isConnected)" in script


def test_a_saved_rule_lists_its_tag_even_before_it_matches_and_coverage_is_reported():
    saved = dict(TABLES)
    del TABLES["tables\x1fp1\x1fRETIRED"]
    try:
        tags.save_rules([retired_rule()])
        snap = tags.snapshot()
        assert snap["tags"] == [{"tag": "Retired", "count": 0}]
        assert snap["catalog"] == [{"project": "p1", "datasets": 2, "tables": 2, "datasets_without_tables": ["RETIRED"]}]
    finally:
        TABLES.update(saved)


def test_tags_endpoint_reports_a_failure_instead_of_dropping_the_connection(server, monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError("id")

    monkeypatch.setattr(tags, "snapshot", boom)
    status, body = call(server, "GET", "/api/tags")
    assert status == 500 and "KeyError" in body["error"]


def test_chosen_projects_are_listed_in_the_background_so_rules_reach_unopened_datasets(monkeypatch):
    """A rule on a dataset nobody opened still tags its tables: the catalog of chosen projects is filled in."""

    monkeypatch.setattr(bq, "peek", REAL_PEEK)
    monkeypatch.setattr(bq, "cached", REAL_CACHED)
    bq.clear_cache()
    calls = []
    release = threading.Event()  # the background listing waits, so the first snapshot cannot race it

    def list_datasets(project):
        release.wait(10)
        calls.append(("datasets", project))
        return [{"id": "ARCHIVE"}, {"id": "prod"}]

    monkeypatch.setattr(bq, "list_datasets", list_datasets)
    monkeypatch.setattr(bq, "list_tables", lambda project, dataset: calls.append(("tables", dataset)) or (
        [{"id": "a_old", "type": "TABLE"}, {"id": "f", "type": "UDF"}] if dataset == "ARCHIVE" else [{"id": "t", "type": "TABLE"}]))
    tags.save_rules([{"tag": "Archived", "rule": {"field": "dataset", "op": "eq", "value": "archive"}}])
    first = tags.snapshot()
    assert first["syncing"] is True and first["objects"] == {}
    release.set()
    snap = until_synced()
    assert set(snap["objects"]) == {"p1.archive.a_old", "p1.archive.f"}
    assert snap["tags"] == [{"tag": "Archived", "count": 2}] and snap["sync_errors"] == {}
    assert sorted(calls) == [("datasets", "p1"), ("tables", "ARCHIVE"), ("tables", "prod")]
    before = len(calls)
    tags.snapshot()
    assert len(calls) == before  # nothing is missing now, nothing is fetched again
    bq.clear_cache()


def test_a_project_that_cannot_be_listed_is_reported_and_not_retried_at_once(monkeypatch):
    monkeypatch.setattr(bq, "peek", REAL_PEEK)
    bq.clear_cache()
    tags.snapshot()
    snap = until_synced()
    assert "offline in tests" in snap["sync_errors"]["p1"]
    assert tags.start_inventory() is False  # failed a moment ago: wait before asking again

