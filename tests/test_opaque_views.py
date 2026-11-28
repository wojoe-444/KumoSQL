"""Views the project reads but does not define (kumosql.view_definitions): ``opaque_view`` and the opt-in definition fetch."""

import json
from pathlib import Path

import pytest

from kumosql import ColumnRef, bigquery_catalog, load_compiled_graph, schema_fetch
from kumosql import view_definitions
from kumosql.job_sql import load_job_sql, with_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "job_lineage"

GRAPH = {
    "projectConfig": {"defaultDatabase": "p", "defaultSchema": "m"},
    "declarations": [{"target": {"database": "p", "schema": "rep", "name": "v_seg"}}],
    "tables": [
        {"target": {"database": "p", "schema": "m", "name": "out"}, "query": "SELECT customer_id, segment FROM `p.rep.v_seg`"},
    ],
}
SEGMENT = ColumnRef("p.m.out", "segment")


@pytest.fixture(autouse=True)
def clean_caches():
    bigquery_catalog.clear_cache()
    schema_fetch._DENIED.clear()
    schema_fetch._NO_CREDENTIALS.clear()
    yield
    bigquery_catalog.clear_cache()


def project(graph=GRAPH):
    return load_compiled_graph(json.loads(json.dumps(graph)))


def fake_bigquery(monkeypatch, tables, denied=(), missing=(), listing=None):
    """Stand in for ``tables.get``: ``tables`` is ``{"p.d.t": metadata}``; returns the tables asked about, in order."""

    calls = []

    def get_table(project_id, dataset, table):
        full = f"{project_id}.{dataset}.{table}"
        calls.append(full)
        if full in denied:
            raise bigquery_catalog.CatalogError("Access Denied", 403)
        if full in missing or full not in tables:
            raise bigquery_catalog.CatalogError("Not found", 404)
        return {"id": table, "schema": [], **tables[full]}

    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: "token")
    monkeypatch.setattr(bigquery_catalog, "get_table", get_table)
    monkeypatch.setattr(
        bigquery_catalog, "list_tables",
        lambda project_id, dataset: [
            {"id": name.split(".")[2], "type": kind} for name, kind in (listing or {}).items() if name.startswith(f"{project_id}.{dataset}.")
        ],
    )
    return calls


def view(sql, legacy=False):
    return {"type": "VIEW", "view_query": sql, "view_legacy_sql": legacy}


def gaps(pipeline):
    return {(gap["asset"], gap["code"]): gap for gap in pipeline.completeness()["gaps"]}


# ------------------------------------------------------------------ the stopping reason


def test_a_table_of_unknown_type_is_still_a_source():
    trace = project().trace_column(SEGMENT)

    assert {str(ref) for ref in trace.sources} == {"p.rep.v_seg.segment"}
    assert trace.complete


def test_a_known_view_without_its_definition_stops_as_opaque_view():
    pipeline = project()
    pipeline.table_types["p.rep.v_seg"] = "VIEW"
    trace = pipeline.trace_column(SEGMENT)

    assert not trace.sources
    assert trace.unknown == ((ColumnRef("p.rep.v_seg", "segment"), "opaque_view"),)
    stop = trace.hops[-1]
    assert (stop.model, stop.status, stop.reason) == ("p.rep.v_seg", "unknown", "opaque_view")
    assert trace.to_json()["unknown"] == [{"node": "p.rep.v_seg", "column": "segment", "reason": "opaque_view"}]


def test_a_known_table_is_a_source():
    pipeline = project()
    pipeline.table_types["p.rep.v_seg"] = "TABLE"

    assert pipeline.trace_column(SEGMENT).complete


def test_the_saved_catalog_says_which_tables_are_views():
    # an earlier lookup (the UI's table browser, or a schema lookup) saved the table's type; reading it calls nothing
    bigquery_catalog.cached(schema_fetch._key("p", "rep", "v_seg"), lambda: {"id": "v_seg", "type": "VIEW", "schema": []})

    trace = project().trace_column(SEGMENT)
    assert trace.unknown == ((ColumnRef("p.rep.v_seg", "segment"), "opaque_view"),)


def test_a_dataset_listing_in_the_catalog_says_so_too():
    bigquery_catalog.cached(bigquery_catalog.tables_key("p", "rep"), lambda: [{"id": "v_seg", "type": "VIEW"}, {"id": "t", "type": "TABLE"}])

    assert view_definitions.table_type(project(), "p.rep.v_seg") == "VIEW"
    assert view_definitions.table_type(project(), "rep.t") == "TABLE"  # a partial name takes the project's default


def test_a_view_reached_through_an_old_job_stops_there(tmp_path):
    from kumosql import load_sqlx_project

    (tmp_path / "j.sql").write_text(
        "CREATE OR REPLACE TABLE sandbox.orders_daily AS\nSELECT order_day, revenue FROM reporting.v_revenue;", encoding="utf-8"
    )
    (tmp_path / "jobs.yaml").write_text("defaults: {project: shop-legacy}\njobs:\n  - {job_id: j, script: j.sql}\n", encoding="utf-8")
    pipeline = with_jobs(load_sqlx_project(FIXTURE / "dataform"), [load_job_sql(tmp_path)])
    pipeline.table_types["shop-legacy.reporting.v_revenue"] = "VIEW"

    trace = pipeline.trace_column(ColumnRef("shop-analytics.marts.daily_revenue", "daily_revenue"))
    assert [(hop.model, hop.job_id, hop.reason) for hop in trace.hops] == [
        ("shop-analytics.marts.daily_revenue", None, None),
        ("shop-legacy.sandbox.orders_daily", "j", None),
        ("shop-legacy.reporting.v_revenue", None, "opaque_view"),
    ]


# ------------------------------------------------------------------ the fetch is opt-in


def test_the_fetch_is_off_by_default(monkeypatch):
    def refuse(*args):
        raise AssertionError("BigQuery was asked while the fetch is off")

    monkeypatch.setattr(bigquery_catalog, "get_table", refuse)
    monkeypatch.setattr(bigquery_catalog, "_token_cached", refuse)
    pipeline = project()

    assert not schema_fetch.enabled()
    assert view_definitions.attach(pipeline) is pipeline
    assert view_definitions.attach(pipeline, billed=True) is pipeline  # the billed path is behind the same opt-in


def test_without_credentials_nothing_is_read(monkeypatch):
    def no_token():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(bigquery_catalog, "_token_cached", no_token)
    monkeypatch.setattr(bigquery_catalog, "get_table", lambda *args: pytest.fail("asked without credentials"))
    pipeline = view_definitions.attach(project(), fetch=True)

    assert [d.code for d in pipeline.diagnostics] == ["view_fetch_skipped"]
    assert set(pipeline.models) == {"p.m.out"}


def test_a_fetched_definition_continues_the_trace(monkeypatch):
    calls = fake_bigquery(monkeypatch, {
        "p.rep.v_seg": view("SELECT c.id AS customer_id, s.name AS segment FROM rep.customers c JOIN `p.rep.segments` s ON c.seg = s.id"),
        "p.rep.customers": {"type": "TABLE"},
        "p.rep.segments": {"type": "TABLE"},
    })
    monkeypatch.setenv("KUMOSQL_SCHEMA_FETCH", "1")  # the same opt-in as the schema lookup
    pipeline = view_definitions.attach(project())

    model = pipeline.models["p.rep.v_seg"]
    assert (model.kind, model.fetched_from) == ("view", "tables.get")
    assert model.fetched_at.endswith("Z")
    assert "p.rep.customers" in model.sql  # the view's partial names mean its own project
    trace = pipeline.trace_column(SEGMENT)
    assert trace.complete
    assert {str(ref) for ref in trace.sources} == {"p.rep.segments.name"}
    assert [hop.model for hop in trace.hops] == ["p.m.out", "p.rep.v_seg"]
    assert trace.hops[1].filters  # the view's join is a filter of its hop
    assert pipeline.table_types["p.rep.customers"] == "TABLE"
    assert calls[0] == "p.rep.v_seg"
    report = pipeline.report(include_duplicates=False)
    assert report["node_identities"]["p.rep.v_seg"]["fetched_from"] == "tables.get"


def test_a_fetched_view_survives_a_saved_snapshot(monkeypatch):
    from kumosql.storage import pipeline_from_snapshot, pipeline_snapshot

    fake_bigquery(monkeypatch, {"p.rep.v_seg": view("SELECT 1 AS customer_id, 'a' AS segment")})
    pipeline = view_definitions.attach(project(), fetch=True)
    again = pipeline_from_snapshot(json.loads(json.dumps(pipeline_snapshot(pipeline, "k"))), "k")

    assert again.models["p.rep.v_seg"].fetched_from == "tables.get"


def test_views_read_through_views_are_followed(monkeypatch):
    fake_bigquery(monkeypatch, {
        "p.rep.v_seg": view("SELECT customer_id, segment FROM `p.rep.v_base`"),
        "p.rep.v_base": view("SELECT id AS customer_id, seg AS segment FROM `p.raw.customers`"),
        "p.raw.customers": {"type": "TABLE"},
    })
    pipeline = view_definitions.attach(project(), fetch=True)

    trace = pipeline.trace_column(SEGMENT)
    assert [hop.model for hop in trace.hops] == ["p.m.out", "p.rep.v_seg", "p.rep.v_base"]
    assert {str(ref) for ref in trace.sources} == {"p.raw.customers.seg"}


def test_a_view_named_without_its_project_is_one_model(monkeypatch):
    graph = {
        "projectConfig": {"defaultDatabase": "p", "defaultSchema": "m"},
        "tables": [{"target": {"database": "p", "schema": "m", "name": "out"}, "query": "SELECT segment FROM rep.v_seg"}],
    }
    fake_bigquery(monkeypatch, {"p.rep.v_seg": view("SELECT seg AS segment FROM `p.raw.customers`"), "p.raw.customers": {"type": "TABLE"}})
    pipeline = view_definitions.attach(project(graph), fetch=True)

    assert "p.rep.v_seg" in pipeline.models and "rep.v_seg" not in pipeline.models
    lineage = pipeline.explain_lineage()[ColumnRef("p.rep.v_seg", "segment")]
    assert {str(ref) for ref in lineage.sources} == {"p.raw.customers.seg"}
    # (A model read by a shorter spelling of its key, here ``rep.v_seg`` for ``p.rep.v_seg``, is a qualify_error for the
    # reader on master too; that gap is reported, not hidden, and is outside this module.)


# ------------------------------------------------------------------ what cannot be read is a gap


def test_a_view_whose_definition_is_denied_is_a_gap_and_stops_the_trace(monkeypatch):
    fake_bigquery(monkeypatch, {}, denied={"p.rep.v_seg"}, listing={"p.rep.v_seg": "VIEW"})
    pipeline = view_definitions.attach(project(), fetch=True)

    gap = gaps(pipeline)[("p.rep.v_seg", "view_definition_unreadable")]
    assert gap["message"].startswith("view p.rep.v_seg: definition not readable (permission)")
    assert gap["blocking"] and "lineage" in gap["effect"]
    trace = pipeline.trace_column(SEGMENT)
    assert trace.unknown == ((ColumnRef("p.rep.v_seg", "segment"), "opaque_view"),)


def test_a_denied_table_of_unknown_type_is_a_gap_but_not_called_a_view(monkeypatch):
    fake_bigquery(monkeypatch, {}, denied={"p.rep.v_seg"})
    pipeline = view_definitions.attach(project(), fetch=True)

    assert "whether it is a view" in gaps(pipeline)[("p.rep.v_seg", "view_definition_unreadable")]["message"]
    assert pipeline.trace_column(SEGMENT).sources  # its type is unknown: nothing is guessed


def test_a_missing_table_is_a_gap(monkeypatch):
    fake_bigquery(monkeypatch, {}, missing={"p.rep.v_seg"})
    pipeline = view_definitions.attach(project(), fetch=True)

    assert "(not found)" in gaps(pipeline)[("p.rep.v_seg", "view_definition_unreadable")]["message"]


def test_a_legacy_sql_view_is_a_gap(monkeypatch):
    fake_bigquery(monkeypatch, {"p.rep.v_seg": view("SELECT a FROM [p:rep.t]", legacy=True)})
    pipeline = view_definitions.attach(project(), fetch=True)

    assert "legacy SQL" in gaps(pipeline)[("p.rep.v_seg", "view_definition_unreadable")]["message"]
    assert pipeline.trace_column(SEGMENT).unknown[0][1] == "opaque_view"


def test_credentials_never_reach_the_answer(monkeypatch):
    fake_bigquery(monkeypatch, {}, denied={"p.rep.v_seg"}, listing={"p.rep.v_seg": "VIEW"})
    monkeypatch.setattr(bigquery_catalog, "_token_cached", lambda: "ya29.secret-token")
    pipeline = view_definitions.attach(project(), fetch=True)

    text = json.dumps(pipeline.completeness()) + json.dumps([d.message for d in pipeline.all_diagnostics()])
    assert "secret-token" not in text


# ------------------------------------------------------------------ the billed path


def test_information_schema_is_asked_only_with_the_billed_flag(monkeypatch):
    fake_bigquery(monkeypatch, {}, denied={"p.rep.v_seg"}, listing={"p.rep.v_seg": "VIEW"})
    sent = []

    def run(sql, billing_project, cap):
        sent.append((sql, billing_project, cap))
        from kumosql.data_sources import Run

        return Run(["table_name", "view_definition", "use_standard_sql"], [["v_seg", "SELECT seg AS segment, id AS customer_id FROM `p.raw.customers`", "YES"]])

    monkeypatch.setattr(view_definitions, "_run_billed", run)
    free_only = view_definitions.attach(project(), fetch=True)
    assert not sent and "p.rep.v_seg" not in free_only.models
    assert "--fetch-views-billed" in gaps(free_only)[("p.rep.v_seg", "view_definition_unreadable")]["message"]

    billed = view_definitions.attach(project(), fetch=True, billed=True, max_bytes=10_485_760)
    assert len(sent) == 1
    sql, _project, cap = sent[0]
    assert "`p.rep.INFORMATION_SCHEMA.VIEWS`" in sql and "'v_seg'" in sql and cap == 10_485_760
    assert billed.models["p.rep.v_seg"].fetched_from == "INFORMATION_SCHEMA.VIEWS"
    assert {str(ref) for ref in billed.trace_column(SEGMENT).sources} == {"p.raw.customers.seg"}
    assert ("p.rep.v_seg", "view_definition_unreadable") not in gaps(billed)


def test_a_refused_billed_query_is_named_in_the_gap(monkeypatch):
    fake_bigquery(monkeypatch, {}, denied={"p.rep.v_seg"}, listing={"p.rep.v_seg": "VIEW"})

    def refuse(sql, billing_project, cap):
        raise RuntimeError("Access Denied: Table p:rep.INFORMATION_SCHEMA.VIEWS: Permission denied")

    monkeypatch.setattr(view_definitions, "_run_billed", refuse)
    pipeline = view_definitions.attach(project(), fetch=True, billed=True)

    message = gaps(pipeline)[("p.rep.v_seg", "view_definition_unreadable")]["message"]
    assert "INFORMATION_SCHEMA.VIEWS: permission" in message


# ------------------------------------------------------------------ the metadata call and the surfaces


def test_tables_get_asks_for_the_view_definition(monkeypatch):
    seen = {}

    def get(path, params=None):
        seen["fields"] = (params or {}).get("fields", "")
        return {"tableReference": {"tableId": "v"}, "type": "VIEW", "view": {"query": "SELECT 1 AS a", "useLegacySql": False}}

    monkeypatch.setattr(bigquery_catalog, "_get", get)
    found = bigquery_catalog.get_table("p", "d", "v")

    assert "view" in seen["fields"].split(",")
    assert (found["type"], found["view_query"], found["view_legacy_sql"]) == ("VIEW", "SELECT 1 AS a", False)


def test_an_old_cache_entry_of_a_view_is_read_again(monkeypatch):
    bigquery_catalog.cached(schema_fetch._key("p", "rep", "v_seg"), lambda: {"id": "v_seg", "type": "VIEW", "schema": []})
    calls = fake_bigquery(monkeypatch, {"p.rep.v_seg": view("SELECT 1 AS customer_id, 'a' AS segment")})
    pipeline = view_definitions.attach(project(), fetch=True)

    assert calls == ["p.rep.v_seg"]
    assert "p.rep.v_seg" in pipeline.models


def test_pipeline_report_needs_the_lookup_for_the_billed_path(tmp_path, capsys):
    from kumosql.cli import pipeline_main

    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(GRAPH), encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        pipeline_main([str(graph), "--fetch-views-billed"])
    assert raised.value.code == 2
    assert "--fetch-views-billed needs --fetch-schema" in capsys.readouterr().err
