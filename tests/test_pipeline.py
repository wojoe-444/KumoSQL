import json

from kumosql import ColumnRef, load_compiled_graph, load_sqlx_project
from kumosql.cli import pipeline_main


RAW_ORDERS = {
    "proj.raw.orders": {
        "id": "INT64",
        "customer_id": "INT64",
        "amount": "FLOAT64",
        "status": "STRING",
        "created_at": "TIMESTAMP",
    }
}


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def dataform_project(root):
    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: analytics\n")
    write(root, "definitions/sources.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(
        root,
        "definitions/staging/stg_orders.sqlx",
        'config { type: "view" }\n'
        "SELECT id, customer_id, amount, status, created_at\n"
        'FROM ${ref("raw", "orders")}\n'
        "WHERE status != 'test'\n",
    )
    write(
        root,
        "definitions/customer_totals.sqlx",
        'config { type: "table" }\n'
        "WITH recent AS (SELECT * FROM ${ref(\"stg_orders\")} WHERE created_at > '2024-01-01')\n"
        "SELECT customer_id, SUM(amount) AS total FROM recent GROUP BY customer_id\n",
    )
    write(
        root,
        "definitions/customer_counts.sqlx",
        'config { type: "table" }\n'
        "SELECT o.customer_id, COUNT(*) AS n\n"
        "FROM ${ref({name: \"stg_orders\"})} AS o\n"
        "${when(incremental(), `WHERE o.created_at > (SELECT MAX(created_at) FROM ${self()})`)}\n"
        "GROUP BY o.customer_id\n",
    )
    return root


def test_sqlx_project_builds_model_graph(tmp_path):
    pipeline = load_sqlx_project(dataform_project(tmp_path), source_schema=RAW_ORDERS)

    assert set(pipeline.models) == {
        "proj.analytics.stg_orders",
        "proj.analytics.customer_totals",
        "proj.analytics.customer_counts",
    }
    assert set(pipeline.sources) == {"proj.raw.orders"}
    assert pipeline.upstream["proj.analytics.stg_orders"] == {"proj.raw.orders"}
    assert pipeline.upstream["proj.analytics.customer_totals"] == {"proj.analytics.stg_orders"}
    assert pipeline.upstream["proj.analytics.customer_counts"] == {"proj.analytics.stg_orders"}
    order = pipeline.topological_order()
    assert order.index("proj.analytics.stg_orders") < order.index("proj.analytics.customer_totals")
    assert pipeline.downstream["proj.analytics.stg_orders"] == {
        "proj.analytics.customer_totals",
        "proj.analytics.customer_counts",
    }


def test_ref_by_name_finds_a_model_in_its_own_dataset(tmp_path):
    # Dataform resolves ref("name") by the action's name, not the default dataset.
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: analytics\n")
    write(tmp_path, "definitions/raw.sqlx", 'config { type: "declaration", database: "lake", schema: "raw", name: "events" }\n')
    write(tmp_path, "definitions/stg.sqlx", 'config { type: "table", schema: "staging", name: "stg" }\nSELECT id FROM ${ref("events")}\n')
    write(tmp_path, "definitions/kpi.sqlx", 'config { type: "table", schema: "marts" }\nSELECT id FROM ${ref("stg")}\n')
    write(tmp_path, "definitions/obj.sqlx", 'config { type: "table" }\nSELECT id FROM ${ref({name: "kpi"})} JOIN ${ref("staging", "stg")} USING (id)\n')
    write(tmp_path, "definitions/a/dup.sqlx", 'config { type: "table", schema: "one" }\nSELECT 1 AS id\n')
    write(tmp_path, "definitions/b/dup.sqlx", 'config { type: "table", schema: "two" }\nSELECT 1 AS id\n')
    write(tmp_path, "definitions/uses_dup.sqlx", 'config { type: "table" }\nSELECT id FROM ${ref("dup")}\n')

    pipeline = load_sqlx_project(tmp_path)

    assert pipeline.upstream["proj.staging.stg"] == {"lake.raw.events"}
    assert pipeline.upstream["proj.marts.kpi"] == {"proj.staging.stg"}
    assert pipeline.upstream["proj.analytics.obj"] == {"proj.marts.kpi", "proj.staging.stg"}
    # A name two actions share is ambiguous (Dataform refuses to compile it): it is not guessed.
    assert pipeline.models["proj.analytics.uses_dup"].declared_dependencies == ()
    assert any(d.code == "unsupported_ref" for d in pipeline.diagnostics)


def test_ctx_ref_resolve_and_config_dependencies_are_edges(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: analytics\n")
    write(tmp_path, "definitions/a.sqlx", 'config { type: "declaration", schema: "raw", name: "a" }\n')
    write(tmp_path, "definitions/b.sqlx", 'config { type: "declaration", schema: "raw", name: "b" }\n')
    write(tmp_path, "definitions/c.sqlx", 'config { type: "declaration", schema: "raw", name: "c" }\n')
    write(tmp_path, "definitions/m.sqlx", (
        'config { type: "table", columns: { name: "Customer name" }, dependencies: ["b", { name: "c", schema: "raw" }] }\n'
        'SELECT id FROM ${ctx.ref("a")} WHERE FALSE -- ${resolve("a")}\n'
    ))
    write(tmp_path, "definitions/single.sqlx", 'config { type: "operations", dependencies: "b" }\nSELECT 1\n')

    pipeline = load_sqlx_project(tmp_path)

    assert pipeline.upstream["proj.analytics.m"] == {"proj.raw.a", "proj.raw.b", "proj.raw.c"}
    assert pipeline.upstream["proj.analytics.single"] == {"proj.raw.b"}
    assert "${" not in pipeline.models["proj.analytics.m"].sql


def test_column_lineage_crosses_models_and_ctes(tmp_path):
    pipeline = load_sqlx_project(dataform_project(tmp_path), source_schema=RAW_ORDERS)

    total = ColumnRef("proj.analytics.customer_totals", "total")
    assert pipeline.column_lineage()[total] == {ColumnRef("proj.analytics.stg_orders", "amount")}
    assert pipeline.upstream_columns(total) == {
        ColumnRef("proj.analytics.stg_orders", "amount"),
        ColumnRef("proj.raw.orders", "amount"),
    }
    assert pipeline.downstream_columns(ColumnRef("proj.raw.orders", "amount")) == {
        ColumnRef("proj.analytics.stg_orders", "amount"),
        total,
    }


def test_dead_columns_count_filter_and_join_reads(tmp_path):
    pipeline = load_sqlx_project(dataform_project(tmp_path), source_schema=RAW_ORDERS)

    # created_at is only read in a WHERE clause downstream, so it is live.
    # status is only read inside stg_orders itself, and id is never read.
    assert pipeline.dead_columns() == {"proj.analytics.stg_orders": ("id", "status")}
    assert pipeline.output_columns("proj.analytics.stg_orders") == (
        "id",
        "customer_id",
        "amount",
        "status",
        "created_at",
    )


def test_unknown_source_schema_makes_dead_columns_conservative(tmp_path):
    root = dataform_project(tmp_path)
    write(
        root,
        "definitions/staging/stg_orders.sqlx",
        'config { type: "view" }\nSELECT * FROM ${ref("raw", "orders")}\n',
    )
    write(
        root,
        "definitions/everything.sqlx",
        'config { type: "table" }\nSELECT * FROM ${ref("stg_orders")}\n',
    )

    pipeline = load_sqlx_project(root)

    assert pipeline.dead_columns() == {}
    codes = {d.code for d in pipeline.all_diagnostics()}
    assert "unexpanded_star" in codes


def test_select_star_expands_with_known_source_schema(tmp_path):
    root = dataform_project(tmp_path)
    write(
        root,
        "definitions/everything.sqlx",
        'config { type: "table" }\nSELECT * FROM ${ref("stg_orders")}\n',
    )

    pipeline = load_sqlx_project(root, source_schema=RAW_ORDERS)

    assert pipeline.dead_columns() == {}
    assert pipeline.output_columns("proj.analytics.everything") == pipeline.output_columns(
        "proj.analytics.stg_orders"
    )


def test_lineage_through_star_ctes_names_the_column_read(tmp_path):
    # JOB-style filtered CTEs: each column read through `SELECT *` is that column of the table,
    # not `*`, and a CTE defined earlier is not a source of a later `SELECT *` CTE.
    write(
        tmp_path,
        "definitions/q.sql",
        "WITH f_ct AS (SELECT * FROM company_type AS ct WHERE ct.kind = 'x'), "
        "f_mc AS (SELECT * FROM movie_companies AS mc WHERE mc.note LIKE '%a%') "
        "SELECT MIN(mc.note) AS note, MIN(ct.kind) AS kind FROM f_ct AS ct, f_mc AS mc WHERE ct.id = mc.company_type_id",
    )
    rows = {row["column"]: row for row in load_sqlx_project(tmp_path).lineage_report()}
    assert rows["note"]["sources"] == [{"node": "movie_companies", "column": "note"}]
    assert rows["kind"]["sources"] == [{"node": "company_type", "column": "kind"}]


def compiled_graph():
    shared = (
        "SELECT customer_id, SUM(amount) AS total, COUNT(*) AS n "
        "FROM `proj.raw.orders` WHERE status = 'paid' GROUP BY customer_id"
    )
    return {
        "tables": [
            {
                "target": {"database": "proj", "schema": "mart", "name": "a"},
                "type": "table",
                "query": f"WITH paid AS ({shared}) SELECT customer_id, total FROM paid",
                "dependencyTargets": [{"database": "proj", "schema": "raw", "name": "orders"}],
                "fileName": "definitions/a.sqlx",
            },
            {
                "target": {"database": "proj", "schema": "mart", "name": "b"},
                "type": "view",
                "query": f"SELECT p.customer_id, p.n FROM (\n  -- same logic, different layout\n  {shared.lower().replace('`proj.raw.orders`', '`proj.raw.orders`')}\n) AS p",
                "dependencyTargets": [{"database": "proj", "schema": "raw", "name": "orders"}],
                "fileName": "definitions/b.sqlx",
            },
        ],
        "declarations": [{"target": {"database": "proj", "schema": "raw", "name": "orders"}}],
    }


def test_compiled_graph_finds_duplicate_logic_across_models():
    pipeline = load_compiled_graph(compiled_graph(), source_schema=RAW_ORDERS)

    groups = pipeline.duplicate_selects()

    assert len(groups) == 1
    locations = {(o.model, o.location) for o in groups[0].occurrences}
    assert locations == {("proj.mart.a", "cte:paid"), ("proj.mart.b", "subquery:p")}
    assert "sum(amount)" in groups[0].sql.lower()


def test_duplicate_report_skips_small_and_nested_matches():
    pipeline = load_compiled_graph(compiled_graph(), source_schema=RAW_ORDERS)

    assert pipeline.duplicate_selects(min_nodes=10_000) == []


def test_cycles_are_reported_not_fatal():
    graph = {
        "tables": [
            {"target": {"schema": "d", "name": "x"}, "query": "SELECT id FROM d.y"},
            {"target": {"schema": "d", "name": "y"}, "query": "SELECT id FROM d.x"},
        ]
    }

    pipeline = load_compiled_graph(graph)

    assert set(pipeline.topological_order()) == {"d.x", "d.y"}
    assert any(d.code == "cycle" for d in pipeline.all_diagnostics())
    # The fallback order is not a topological order, and the report says so.
    assert pipeline.cyclic_models() == ["d.x", "d.y"]
    report = pipeline.report()
    assert report["order_complete"] is False and report["cyclic_models"] == ["d.x", "d.y"]
    assert load_compiled_graph(compiled_graph(), source_schema=RAW_ORDERS).report()["order_complete"] is True


def test_duplicates_keep_the_case_of_qualified_table_names():
    def graph(one, two):
        return load_compiled_graph(
            {"tables": [
                {"target": {"schema": "d", "name": "one"}, "query": one},
                {"target": {"schema": "d", "name": "two"}, "query": two},
            ]}
        )

    # BigQuery table names are case-sensitive; column names are not.
    assert graph("SELECT id FROM p.d.Orders", "SELECT id FROM p.d.orders").duplicate_selects(min_nodes=1) == []
    [group] = graph("SELECT Id FROM p.d.Orders", "SELECT id FROM p.d.Orders").duplicate_selects(min_nodes=1)
    assert group.sql == "SELECT id FROM p.d.Orders"
    # A bare name resolved by a default catalog is a physical table too, unless a CTE of that name is visible.
    for one, two in (
        ("SELECT id FROM Orders", "SELECT id FROM orders"),
        ("SELECT id FROM ORDERS", "SELECT id FROM orders"),
        ("SELECT o.id FROM Orders AS o", "SELECT o.id FROM orders AS o"),
        ("SELECT id FROM Orders WHERE id > 1", "SELECT id FROM orders WHERE id > 1"),
    ):
        assert graph(one, two).duplicate_selects(min_nodes=1) == [], (one, two)
    assert len(graph("SELECT id FROM Orders", "SELECT id FROM Orders").duplicate_selects(min_nodes=1)) == 1
    # CTE names have no case: the same body read under differently cased names is still one duplicate group.
    cte = "WITH Base AS (SELECT id FROM p.d.t) SELECT id FROM {}"
    assert graph(cte.format("Base"), cte.format("base")).duplicate_selects(min_nodes=1)
    # A CTE does not make a same-named physical table, read elsewhere, case-insensitive.
    assert graph("WITH Orders AS (SELECT 1 AS id) SELECT id FROM orders", "SELECT id FROM Orders").duplicate_selects(min_nodes=1) == []


def test_plain_sql_folder_resolves_bare_table_names(tmp_path):
    write(tmp_path, "base.sql", "SELECT id, name, extra FROM `proj.raw.people`")
    write(tmp_path, "report.sql", "WITH base AS (SELECT 1 AS id) SELECT b.id, p.name FROM base b JOIN proj.ds.base p USING (id)")

    pipeline = load_sqlx_project(
        tmp_path, source_schema={"proj.raw.people": {"id": "INT64", "name": "STRING", "extra": "STRING"}}
    )

    assert pipeline.upstream["report"] == {"base"}
    assert pipeline.dead_columns() == {"base": ("extra",)}


def test_unparseable_model_is_a_diagnostic_and_blocks_dead_column_claims(tmp_path):
    write(tmp_path, "base.sql", "SELECT id, extra FROM `proj.raw.people`")
    write(tmp_path, "broken.sql", "SELECT id FROM base WHERE (")
    write(tmp_path, "ok.sql", "SELECT id FROM base")

    pipeline = load_sqlx_project(
        tmp_path, source_schema={"proj.raw.people": {"id": "INT64", "extra": "STRING"}}
    )

    assert any(d.code == "parse_error" and d.model == "broken" for d in pipeline.all_diagnostics())
    # broken.sql might read base.extra, so nothing may be called dead.
    assert pipeline.dead_columns() == {}
    assert any(d.code == "unknown_reads" for d in pipeline.all_diagnostics())


def test_reads_inside_masked_dataform_expressions_keep_columns_live(tmp_path):
    root = dataform_project(tmp_path)
    write(
        root,
        "definitions/customer_totals.sqlx",
        'config { type: "table" }\nSELECT customer_id, SUM(amount) AS total FROM ${ref("stg_orders")} GROUP BY customer_id\n',
    )

    pipeline = load_sqlx_project(root, source_schema=RAW_ORDERS)

    # created_at is now read only inside customer_counts' ${when(...)} block.
    assert pipeline.dead_columns() == {"proj.analytics.stg_orders": ("id", "status")}


def test_pipeline_cli_writes_json_report(tmp_path, capsys):
    root = dataform_project(tmp_path / "project")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(RAW_ORDERS), encoding="utf-8")

    assert pipeline_main([str(root), "--source-schema", str(schema)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["models"] == 3
    assert report["dead_columns"] == {"proj.analytics.stg_orders": ["id", "status"]}


# ------------------------------------------------------------ explain lineage

LINEAGE_SCHEMA = {
    "p.raw.a": {"id": "INT64", "x": "INT64"},
    "p.raw.b": {"id": "INT64", "x": "INT64", "y": "INT64"},
}


def lineage_pipeline(**queries):
    graph = {"tables": [{"target": {"database": "p", "schema": "m", "name": n}, "query": q} for n, q in queries.items()]}
    return load_compiled_graph(graph, source_schema=LINEAGE_SCHEMA)


def record(pipeline, model, column):
    return pipeline.explain_lineage()[ColumnRef(f"p.m.{model}", column)]


def test_constant_columns_are_distinct_from_untraceable_ones():
    pipeline = lineage_pipeline(
        t="SELECT 1 AS one, COUNT(*) AS n, id AS id FROM `p.raw.a` GROUP BY id",
    )

    assert record(pipeline, "t", "one").status == "constant"
    assert record(pipeline, "t", "n").status == "constant"
    assert record(pipeline, "t", "id").status == "traced"
    assert record(pipeline, "t", "id").transform == "passthrough"


def test_ambiguous_bare_column_is_unknown_not_empty():
    pipeline = lineage_pipeline(t="SELECT x FROM `p.raw.a` AS a JOIN `p.raw.b` AS b ON a.id = b.id")

    item = record(pipeline, "t", "x")
    assert item.status == "unknown" and item.reason == "unresolved_column"
    assert pipeline.trace_column(ColumnRef("p.m.t", "x")).complete is False


def test_column_missing_from_known_schema_is_unknown():
    pipeline = lineage_pipeline(t="SELECT zzz AS z, id FROM `p.raw.a`")

    assert record(pipeline, "t", "z").reason == "unknown_column"
    assert record(pipeline, "t", "id").status == "traced"
    assert not pipeline.column_lineage().get(ColumnRef("p.m.t", "z"))


def test_unexpanded_star_is_marked_unknown_and_propagates():
    pipeline = lineage_pipeline(
        s="SELECT * FROM `p.ext.unknown_table`",
        t="SELECT id, 1 AS one FROM s",
    )

    star = record(pipeline, "s", "*")
    assert star.status == "unknown" and star.reason == "unexpanded_star"
    assert not any(ref.column == "*" for parents in pipeline.column_lineage().values() for ref in parents)
    trace = pipeline.trace_column(ColumnRef("p.m.t", "id"))
    assert not trace.complete
    assert dict(trace.unknown)[ColumnRef("p.m.s", "id")] == "unexpanded_star"
    assert pipeline.trace_column(ColumnRef("p.m.t", "one")).complete


def test_failed_lineage_call_still_gets_an_entry(monkeypatch):
    import kumosql.pipeline as module

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(module, "lineage", boom)
    pipeline = lineage_pipeline(t="SELECT id FROM `p.raw.a`")

    item = record(pipeline, "t", "id")
    assert item.status == "unknown" and item.reason == "lineage_error"
    assert any(d.code == "lineage_error" for d in pipeline.all_diagnostics())


def test_unparseable_upstream_model_makes_downstream_unknown():
    pipeline = lineage_pipeline(bad="SELECT id FROM `p.raw.a` WHERE (", t="SELECT id FROM bad")

    trace = pipeline.trace_column(ColumnRef("p.m.t", "id"))

    assert dict(trace.unknown) == {ColumnRef("p.m.bad", "id"): "unparsed_model"}
    assert trace.sources == frozenset()


def test_trace_reaches_sources_through_join_alias_cte_and_expression():
    pipeline = lineage_pipeline(
        c="WITH j AS (SELECT l.x AS lx, r.y + r.x AS s FROM `p.raw.a` AS l JOIN `p.raw.b` AS r ON l.id = r.id) "
        "SELECT lx AS out_x, s AS out_s FROM j",
    )

    assert record(pipeline, "c", "out_x").transform == "renamed"
    assert record(pipeline, "c", "out_s").transform == "expression"
    trace = pipeline.trace_column(ColumnRef("p.m.c", "out_x"))
    assert trace.complete and trace.sources == {ColumnRef("p.raw.a", "x")}
    assert pipeline.trace_column(ColumnRef("p.m.c", "out_s")).sources == {
        ColumnRef("p.raw.b", "y"),
        ColumnRef("p.raw.b", "x"),
    }


def test_self_join_with_two_aliases_traces_both_sides_to_one_column():
    pipeline = lineage_pipeline(t="SELECT a1.x + a2.x AS total FROM `p.raw.a` a1 JOIN `p.raw.a` a2 ON a1.id = a2.id")

    assert record(pipeline, "t", "total").sources == {ColumnRef("p.raw.a", "x")}


def test_transform_kinds_and_scalar_subquery_and_unnest_literal():
    pipeline = lineage_pipeline(
        t="SELECT SUM(x) AS s, ROW_NUMBER() OVER (ORDER BY id) AS rn, (SELECT MAX(y) FROM `p.raw.b`) AS m FROM `p.raw.a`",
        u="SELECT n FROM UNNEST([1, 2]) AS n",
        v="SELECT id FROM `p.raw.a` UNION ALL SELECT id FROM `p.raw.b`",
    )

    assert record(pipeline, "t", "s").transform == "aggregate"
    assert record(pipeline, "t", "rn").transform == "window"
    assert record(pipeline, "t", "m").sources == {ColumnRef("p.raw.b", "y")}
    assert record(pipeline, "u", "n").status == "constant"
    assert record(pipeline, "v", "id").sources == {ColumnRef("p.raw.a", "id"), ColumnRef("p.raw.b", "id")}
    assert record(pipeline, "v", "id").transform == "union"


def test_external_table_columns_end_the_trace_as_sources():
    pipeline = lineage_pipeline(t="SELECT q FROM `other.ds.outside`")

    trace = pipeline.trace_column(ColumnRef("p.m.t", "q"))

    assert trace.complete and trace.sources == {ColumnRef("other.ds.outside", "q")}


def test_lineage_report_rows_and_pipeline_report_section():
    pipeline = lineage_pipeline(s="SELECT * FROM `p.ext.unknown_table`", t="SELECT id AS k FROM `p.raw.a`")

    rows = {(r["node"], r["column"]): r for r in pipeline.lineage_report()}

    assert rows[("p.m.t", "k")] == {
        "node": "p.m.t",
        "column": "k",
        "sources": [{"node": "p.raw.a", "column": "id"}],
        "transform": "renamed",
        "status": "traced",
        "complete": True,
        "filters": [],
        "expression": "id",
    }
    assert rows[("p.m.s", "*")]["status"] == "unknown" and rows[("p.m.s", "*")]["reason"] == "unexpanded_star"
    assert pipeline.report()["column_lineage"] == pipeline.lineage_report()


# ------------------------------------------------------------ gaps (#28)


def _gap_codes(report):
    return {g["code"] for g in report["completeness"]["gaps"]}


def test_complete_pipeline_reports_complete(tmp_path):
    write(tmp_path, "a.sql", "SELECT id FROM `proj.raw.people`")
    write(tmp_path, "b.sql", "SELECT id FROM a")

    report = load_sqlx_project(tmp_path).report()

    assert report["completeness"]["complete"] is True
    assert all(report["completeness"]["views"].values())
    assert report["graph"]["completeness"]["complete"] is True


def test_unparseable_model_marks_every_view_incomplete(tmp_path):
    write(tmp_path, "base.sql", "SELECT id FROM `proj.raw.people`")
    write(tmp_path, "broken.sql", "SELECT id FROM base WHERE (")

    report = load_sqlx_project(tmp_path).report()
    completeness = report["completeness"]

    assert completeness["complete"] is False
    assert not any(completeness["views"].values())
    assert {"parse_error", "unknown_reads"} <= _gap_codes(report)
    assert completeness["assets_not_analyzed"] == 1
    assert report["graph"]["completeness"]["complete"] is False
    assert {g["asset"] for g in completeness["gaps"] if g["blocking"]} == {"broken"}


def test_unknown_reads_is_reported_per_model_and_survives_scoping(tmp_path):
    from kumosql.scopes import parse_scope

    write(tmp_path, "one.sql", "SELECT id FROM (")
    write(tmp_path, "two.sql", "SELECT id FROM (")
    write(tmp_path, "fine.sql", "SELECT 1 AS id")
    pipeline = load_sqlx_project(tmp_path)

    unknown = [d for d in pipeline.all_diagnostics() if d.code == "unknown_reads"]
    assert sorted(d.model for d in unknown) == ["one", "two"]

    report = pipeline.report(scope=parse_scope({"name": "one-only", "fields": {"name": ["one"]}}))
    assert any(d["code"] == "unknown_reads" and d["model"] == "one" for d in report["diagnostics"])
    assert report["completeness"]["complete"] is False
    assert {g["asset"] for g in report["completeness"]["gaps"]} == {"one"}

    clean = pipeline.report(scope=parse_scope({"name": "fine", "fields": {"name": ["fine"]}}))
    assert clean["completeness"]["complete"] is True


def test_cycle_marks_graph_and_impact_incomplete():
    graph = {
        "tables": [
            {"target": {"schema": "d", "name": "x"}, "query": "SELECT id FROM d.y"},
            {"target": {"schema": "d", "name": "y"}, "query": "SELECT id FROM d.x"},
        ]
    }
    completeness = load_compiled_graph(graph).completeness()

    assert completeness["views"]["graph"] is False
    assert completeness["views"]["impact"] is False
    assert completeness["views"]["lineage"] is True


def test_extra_queries_in_a_script_are_reported():
    graph = {
        "tables": [
            {
                "target": {"schema": "d", "name": "x"},
                "queries": ["SELECT id FROM d.a", "SELECT id FROM d.b"],
            },
            {"target": {"schema": "d", "name": "a"}, "query": "SELECT 1 AS id"},
            {"target": {"schema": "d", "name": "b"}, "query": "SELECT 1 AS id"},
        ]
    }
    pipeline = load_compiled_graph(graph)

    assert any(d.code == "skipped_statements" and d.model == "d.x" for d in pipeline.all_diagnostics())
    assert pipeline.completeness()["complete"] is False


def test_operations_are_read_as_scripts():
    graph = {
        "tables": [
            {"target": {"schema": "d", "name": "a"}, "query": "SELECT 1 AS id"},
            {"target": {"schema": "d", "name": "raw"}, "query": "SELECT 1 AS id"},
        ],
        "operations": [
            {
                "target": {"schema": "d", "name": "load"},
                "queries": ["INSERT INTO d.a SELECT id FROM d.raw"],
                "dependencyTargets": [{"schema": "d", "name": "a"}],
            }
        ],
    }
    pipeline = load_compiled_graph(graph)

    assert pipeline.completeness()["complete"] is True
    assert "d.raw" in pipeline.upstream["d.load"]
    # what the operation writes is fed by what it reads
    assert "d.raw" in pipeline.upstream["d.a"]


def test_an_operation_with_dynamic_sql_is_listed_as_not_analysed():
    graph = {
        "tables": [{"target": {"schema": "d", "name": "a"}, "query": "SELECT 1 AS id"}],
        "operations": [
            {
                "target": {"schema": "d", "name": "load"},
                "queries": ["EXECUTE IMMEDIATE FORMAT('INSERT INTO d.a SELECT 2 FROM %s', CAST(1 AS STRING))"],
                "dependencyTargets": [{"schema": "d", "name": "a"}],
            }
        ],
    }
    completeness = load_compiled_graph(graph).completeness()

    assert completeness["complete"] is False
    assert completeness["by_code"]["unparsed_operation"] == 1
    assert completeness["gaps"][0]["asset"] == "d.load"


def test_ambiguous_table_reference_is_distinct_from_external():
    graph = {
        "tables": [
            {"target": {"schema": "a", "name": "events"}, "query": "SELECT 1 AS id"},
            {"target": {"schema": "b", "name": "events"}, "query": "SELECT 1 AS id"},
            {
                "target": {"schema": "c", "name": "report"},
                "query": "SELECT id FROM events JOIN elsewhere.t USING (id)",
            },
        ]
    }
    pipeline = load_compiled_graph(graph)
    codes = {(d.model, d.code) for d in pipeline.all_diagnostics()}

    assert ("c.report", "ambiguous_reference") in codes
    assert ("c.report", "external_tables") in codes
    assert pipeline.completeness()["views"]["graph"] is False


def test_external_tables_are_listed_but_do_not_block():
    graph = {"tables": [{"target": {"schema": "c", "name": "report"}, "query": "SELECT id FROM elsewhere.t"}]}
    completeness = load_compiled_graph(graph).completeness()

    assert completeness["complete"] is True
    assert completeness["gaps"][0]["kind"] == "unmatched_reference"
    assert completeness["gaps"][0]["blocking"] is False


def test_unattributed_observations_make_the_graph_incomplete(tmp_path):
    from kumosql.graph import ObservedRead

    write(tmp_path, "a.sql", "SELECT 1 AS id")
    pipeline = load_sqlx_project(tmp_path)
    read = ObservedRead("job-1", "2024-01-01T00:00:00Z", None, ("a",))

    report = pipeline.report(observed_reads=[read])

    assert report["completeness"]["complete"] is False
    assert "unattributed_reads" in _gap_codes(report)


def test_ctas_view_insert_select_and_export_data_reads_become_edges(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: analytics\n")
    write(tmp_path, "definitions/a.sql", "CREATE OR REPLACE TABLE `proj.analytics.a` AS SELECT id FROM `proj.raw.orders`\n")
    write(tmp_path, "definitions/b.sql", "CREATE VIEW `proj.analytics.b` AS SELECT id FROM `proj.raw.orders`\n")
    write(tmp_path, "definitions/c.sql", "INSERT INTO `proj.analytics.c` (id) SELECT id FROM `proj.analytics.a`\n")

    pipeline = load_sqlx_project(tmp_path)

    assert pipeline.upstream["c"] == {"a"}
    assert not [d for d in pipeline.all_diagnostics() if d.code in {"no_query", "unknown_reads"}]
    external = {d.model: d.message for d in pipeline.all_diagnostics() if d.code == "external_tables"}
    assert "proj.raw.orders" in external["a"] and "proj.raw.orders" in external["b"]
