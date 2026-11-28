import random

import pytest

from kumosql import ColumnRef, bigquery_catalog, load_compiled_graph, schema_fetch


@pytest.fixture(autouse=True)
def clean_caches():
    bigquery_catalog.clear_cache()
    schema_fetch._DENIED.clear()
    schema_fetch._NO_CREDENTIALS.clear()
    yield
    bigquery_catalog.clear_cache()


def fake_bigquery(monkeypatch, tables, denied=(), listing=None):
    """Stand in for BigQuery: ``tables`` is ``{"p.d.t": [column names]}``; returns the list of fetched tables."""

    calls = []

    def get_table(project, dataset, table):
        full = f"{project}.{dataset}.{table}"
        calls.append(full)
        if full in denied or full not in tables:
            raise bigquery_catalog.CatalogError("no access", 403)
        return {"id": table, "schema": [{"name": c, "type": "INTEGER" if c == "id" else "STRING"} for c in tables[full]]}

    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "1")
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: "token")
    monkeypatch.setattr(bigquery_catalog, "get_table", get_table)
    monkeypatch.setattr(
        bigquery_catalog, "list_tables",
        lambda project, dataset: [{"id": t.split(".")[2]} for t in (listing or tables) if t.startswith(f"{project}.{dataset}.")],
    )
    return calls


def project(**queries):
    graph = {"tables": [{"target": {"database": "p", "schema": "m", "name": n}, "query": q} for n, q in queries.items()]}
    return load_compiled_graph(graph)


def codes(pipeline):
    return {(d.model, d.code) for d in pipeline._analyse().diagnostics}


def test_star_over_unknown_table_stays_unknown_when_fetching_is_off():
    pipeline = project(s="SELECT * FROM `p.ext.raw`")
    assert ("p.m.s", "unexpanded_star") in codes(pipeline)


def test_star_over_unknown_table_is_expanded_from_fetched_columns(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id", "name"]})
    pipeline = project(s="SELECT * FROM `p.ext.raw`", t="SELECT id, name FROM `p.m.s`")

    assert ("p.m.s", "unexpanded_star") not in codes(pipeline)
    assert calls == ["p.ext.raw"]
    assert pipeline.explain_lineage()[ColumnRef("p.m.s", "name")].status == "traced"
    assert pipeline.trace_column(ColumnRef("p.m.t", "id")).complete


def test_each_table_is_fetched_once_across_models(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]})
    project(**{f"m{i}": "SELECT * FROM `p.ext.raw`" for i in range(20)})._analyse()
    assert calls == ["p.ext.raw"]


def test_inaccessible_table_stays_unknown(monkeypatch):
    fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]}, denied={"p.ext.raw"})
    schema_fetch._DENIED.clear()
    pipeline = project(s="SELECT * FROM `p.ext.raw`")
    assert ("p.m.s", "unexpanded_star") in codes(pipeline)


def test_wildcard_table_uses_union_of_matching_schemas(monkeypatch):
    fake_bigquery(monkeypatch, {"p.ext.ev_1": ["id", "a"], "p.ext.ev_2": ["id", "b"], "p.ext.other": ["z"]})
    schema, stats = schema_fetch.resolve(["p.ext.ev_*"])
    assert list(schema["p.ext.ev_*"]) == ["id", "a", "b"]
    assert stats["unknown"] == 0


def test_wildcard_with_an_unreadable_member_stays_unknown(monkeypatch):
    fake_bigquery(monkeypatch, {"p.ext.ev_1": ["id"], "p.ext.ev_2": ["id"]}, denied={"p.ext.ev_2"})
    schema_fetch._DENIED.clear()
    schema, stats = schema_fetch.resolve(["p.ext.ev_*"])
    assert schema == {} and stats["unknown"] == 1


def test_saved_catalog_answers_without_calling_bigquery(monkeypatch):
    calls = fake_bigquery(monkeypatch, {})
    monkeypatch.setattr(bigquery_catalog, "saved_tables", lambda: [("p", "ext", "raw", {"schema": [{"name": "id", "type": "INT64"}]})])
    schema, stats = schema_fetch.resolve(["ext.raw"], default_project="p")
    assert schema == {"ext.raw": {"id": "INT64"}} and calls == [] and stats["from_catalog"] == 1


def test_no_credentials_means_no_lookup(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]})
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: (_ for _ in ()).throw(RuntimeError("no credentials")))
    schema_fetch._NO_CREDENTIALS.clear()
    assert schema_fetch.resolve(["p.ext.raw"])[0] == {} and calls == []


def test_log_holds_counts_not_names(monkeypatch):
    fake_bigquery(monkeypatch, {"p.secret_ds.secret_table": ["secret_col"]})
    messages = []
    monkeypatch.setattr(schema_fetch.console, "say", lambda message, **kw: messages.append(message))
    schema_fetch.resolve(["p.secret_ds.secret_table"])
    assert messages and not any("secret" in m for m in messages)


def test_settings_round_trip(monkeypatch):
    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH")
    assert schema_fetch.settings() == {"enabled": False}
    assert schema_fetch.save_settings(True) == {"enabled": True}
    assert schema_fetch.save_settings(False) == {"enabled": False}
    with pytest.raises(ValueError):
        schema_fetch.save_settings("yes")


def test_generated_job_log_star_failures_are_resolved(monkeypatch):
    """A job-log-shaped corpus: many statements over a few hundred outside tables, some unreadable."""

    rng = random.Random(7)
    tables = {f"p.ext{i % 12}.t{i}": ["id", "name", "amount"] for i in range(300)}
    denied = {name for name in tables if rng.random() < 0.1}
    calls = fake_bigquery(monkeypatch, tables, denied=denied)
    schema_fetch._DENIED.clear()
    queries = {}
    for n in range(1200):
        source = rng.choice(list(tables))
        queries[f"j{n}"] = rng.choice(["SELECT * FROM `{t}`", "SELECT * EXCEPT (name) FROM `{t}` WHERE id > 1"]).format(t=source)
    pipeline = project(**queries)

    stars = [(m, c) for m, c in codes(pipeline) if c == "unexpanded_star"]
    readable = {q.split("`")[1] for q in queries.values()} - denied
    assert len(set(calls)) == len(calls) <= 300
    # every statement over a readable table is resolved; every one over an unreadable table stays unknown
    unresolved_models = {m for m, _ in stars}
    assert unresolved_models == {f"p.m.{n}" for n, q in queries.items() if q.split("`")[1] in denied}
    assert len(stars) < 1200 and readable


def test_lookup_reads_metadata_only_even_for_partition_filtered_tables(monkeypatch):
    """Only ``tables.get`` calls are made, so a table that requires a partition filter is looked up like any other."""

    paths = []

    def fake_get_once(path, params=None):
        paths.append(path)
        if not path.endswith("/tables/events") or "queries" in path or "data" in path.split("/")[-1]:
            raise bigquery_catalog.CatalogError("Cannot query over table without a filter over partition column", 400)
        return {"tableReference": {"tableId": "events"}, "schema": {"fields": [{"name": "id", "type": "INTEGER"}]},
                "timePartitioning": {"requirePartitionFilter": True}}

    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "1")
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: "token")
    monkeypatch.setattr(bigquery_catalog, "_get_once", fake_get_once)
    schema, _ = schema_fetch.resolve(["p.ext.events"])
    assert schema == {"p.ext.events": {"id": "INT64"}}
    assert paths == ["projects/p/datasets/ext/tables/events"]


def test_one_credential_is_used_for_every_concurrent_request(monkeypatch):
    import threading
    import time

    refreshes = []

    def slow_token():
        refreshes.append(1)
        time.sleep(0.05)
        return "token"

    monkeypatch.delenv("BQ_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(bigquery_catalog, "access_token", slow_token)
    monkeypatch.setattr(bigquery_catalog, "_token", None)
    threads = [threading.Thread(target=bigquery_catalog._token_cached) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(refreshes) == 1


def test_many_tables_are_fetched_concurrently(monkeypatch):
    import threading
    import time

    tables = {f"p.ext{i % 7}.t{i}": ["id"] for i in range(210)}
    fake_bigquery(monkeypatch, tables)
    real = bigquery_catalog.get_table
    lock, in_flight, peak = threading.Lock(), [0], [0]

    def slow(project, dataset, table):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        try:
            time.sleep(0.05)
            return real(project, dataset, table)
        finally:
            with lock:
                in_flight[0] -= 1

    monkeypatch.setattr(bigquery_catalog, "get_table", slow)
    schema, stats = schema_fetch.resolve(list(tables))
    assert stats["found"] == 210
    # Counted, not timed, so a busy machine cannot fail it: three or more lookups at once is a threefold speed-up over
    # one at a time (serial would take 10.5 s; a pool of 8 takes about 1.3 s).
    assert 3 <= peak[0] <= schema_fetch.WORKERS


def test_many_tables_write_the_catalog_file_once(monkeypatch):
    tables = {f"p.ext.t{i}": ["id", "name"] for i in range(40)}
    fake_bigquery(monkeypatch, tables)
    writes = []
    real = bigquery_catalog._save_disk
    monkeypatch.setattr(bigquery_catalog, "_save_disk", lambda: (writes.append(1), real())[1])
    answers, stats = schema_fetch.resolve([f"p.ext.t{i}" for i in range(40)])
    assert stats["found"] == 40 and len(answers) == 40
    assert len(writes) == 1  # not one rewrite of the whole file per table
    assert bigquery_catalog.saved_tables()  # and it was written


def test_lookup_stops_waiting_after_its_time_budget(monkeypatch):
    fake_bigquery(monkeypatch, {f"p.ext.t{i}": ["id"] for i in range(10)})
    monkeypatch.setattr(schema_fetch, "MAX_SECONDS", -1.0)
    answers, stats = schema_fetch.resolve([f"p.ext.t{i}" for i in range(10)])
    assert answers == {} and stats["unknown"] == 10


def forbid_network(monkeypatch):
    """Any BigQuery client call, token request or socket connection fails the test."""

    import socket

    def boom(*args, **kwargs):
        raise AssertionError("the network was reached without an opt-in")

    monkeypatch.setattr(bigquery_catalog, "_token_cached", boom)
    monkeypatch.setattr(bigquery_catalog, "get_table", boom)
    monkeypatch.setattr(bigquery_catalog, "list_tables", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


def test_nothing_reaches_the_network_without_an_opt_in(monkeypatch):
    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH")
    forbid_network(monkeypatch)
    assert schema_fetch.enabled() is False
    pipeline = project(s="SELECT * FROM `p.ext.raw`", t="SELECT id FROM `p.m.s`")
    assert ("p.m.s", "unexpanded_star") in codes(pipeline)
    schema, stats = schema_fetch.resolve(["p.ext.raw", "p.ext.events_*"])
    assert schema == {} and stats["unknown"] == 2


def test_environment_switch_wins_over_a_saved_opt_in(monkeypatch):
    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH")
    schema_fetch.save_settings(True)
    assert schema_fetch.enabled() is True
    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "0")
    assert schema_fetch.enabled() is False
    forbid_network(monkeypatch)
    assert schema_fetch.resolve(["p.ext.raw"])[0] == {}


def test_opt_in_by_environment_still_fetches(monkeypatch):
    calls = fake_bigquery(monkeypatch, {"p.ext.raw": ["id"]})
    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH", raising=False)
    assert schema_fetch.resolve(["p.ext.raw"])[0] == {}  # still off: nothing asked for it
    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "1")
    assert schema_fetch.resolve(["p.ext.raw"])[0] == {"p.ext.raw": {"id": "INT64"}}
    assert calls == ["p.ext.raw"]


def test_cli_flag_is_the_opt_in(monkeypatch, tmp_path):
    from kumosql.cli import pipeline_main

    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH")
    (tmp_path / "a.sql").write_text("SELECT id FROM `p.raw.people`", encoding="utf-8")
    forbid_network(monkeypatch)
    assert pipeline_main([str(tmp_path), "-o", str(tmp_path / "r.json")]) == 0
    assert schema_fetch.enabled() is False
    calls = fake_bigquery(monkeypatch, {"p.raw.people": ["id"]})
    monkeypatch.delenv("KUMOSQL_SCHEMA_FETCH")
    assert pipeline_main([str(tmp_path), "--fetch-schema", "-o", str(tmp_path / "r.json")]) == 0
    assert schema_fetch.enabled() is True
