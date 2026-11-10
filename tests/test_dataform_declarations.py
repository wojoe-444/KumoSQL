"""Refs resolved through Dataform declarations (``.sqlx`` and JavaScript) instead of the default schema."""

from kumosql import load_sqlx_project
from kumosql.pipeline_loading import js_declared_targets
from kumosql.pipeline_types import Target

DEFAULT = Target("proj", "analytics", "")


def project(tmp_path, files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: analytics\n")
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


def reads(pl, model="proj.analytics.m"):
    return set(pl.upstream[model])


def test_sqlx_declaration_with_its_own_schema_and_database(tmp_path):
    pl = project(tmp_path, {
        "definitions/src.sqlx": 'config { type: "declaration", database: "other", schema: "raw", name: "orders" }',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"other.raw.orders"}


def test_js_declare_resolves_ref(tmp_path):
    pl = project(tmp_path, {
        "definitions/decl.js": 'declare({ schema: "raw", name: "orders" });\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"proj.raw.orders"}


def test_declarations_in_a_literal_loop_and_in_includes(tmp_path):
    pl = project(tmp_path, {
        "includes/decl.js": 'const schema = "raw";\n["orders", "users"].forEach((t) => declare({ schema, name: t }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("orders")} JOIN ${ref("users")} USING (id)',
    })
    assert reads(pl) == {"proj.raw.orders", "proj.raw.users"}


def test_declaration_without_schema_uses_project_defaults():
    declared, _, complete = js_declared_targets('declare({ name: "t" })', DEFAULT)
    assert declared == [Target("proj", "analytics", "t")] and complete


def test_dynamic_declarations_leave_unlisted_refs_unresolved(tmp_path):
    pl = project(tmp_path, {
        "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == set()
    kinds = {d.code for d in pl.diagnostics}
    assert {"js_declaration_dynamic", "unsupported_ref"} <= kinds


def test_a_name_declared_in_two_schemas_is_not_guessed(tmp_path):
    pl = project(tmp_path, {
        "definitions/a.js": 'declare({ schema: "a", name: "t" }); declare({ schema: "b", name: "t" });',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("t")}',
        "definitions/n.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a", "t")}',
    })
    assert reads(pl) == set()
    assert reads(pl, "proj.analytics.n") == {"proj.a.t"}


def test_without_any_declaration_the_default_schema_still_applies(tmp_path):
    pl = project(tmp_path, {"definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("t")}'})
    assert {d.key for d in pl.models["proj.analytics.m"].declared_dependencies} == {"proj.analytics.t"}


DYNAMIC = {
    "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
    "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
}


def load(tmp_path, compiled):
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: proj\ndefaultDataset: analytics\n")
    for name, text in DYNAMIC.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path, compiled_targets=compiled)


def test_dynamic_declarations_fall_back_to_the_compiled_graph(tmp_path):
    pl = load(tmp_path, lambda: [(("lake", "raw", "orders"), True)])
    assert reads(pl) == {"lake.raw.orders"}
    assert "lake.raw.orders" in pl.sources
    assert not any(d.code == "unsupported_ref" for d in pl.diagnostics)


def test_an_unreachable_compiled_graph_leaves_refs_unresolved(tmp_path):
    def broken():
        raise RuntimeError("no credentials")

    pl = load(tmp_path, broken)
    assert reads(pl) == set()
    assert {"compiled_graph_unavailable", "unsupported_ref"} <= {d.code for d in pl.diagnostics}


def test_the_compiled_graph_is_not_asked_when_the_files_are_enough(tmp_path):
    def forbidden():
        raise AssertionError("should not be called")

    pl = project(tmp_path, {"definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("t")}'})
    assert pl is not None
    (tmp_path / "definitions/decl.js").write_text('declare({ schema: "raw", name: "t" });')
    assert load_sqlx_project(tmp_path, compiled_targets=forbidden) is not None


def test_compiled_targets_reads_the_newest_release_compilation(monkeypatch):
    from kumosql import workflow_configs as wc

    monkeypatch.setattr(wc, "search_for", lambda url: {"projects": ["p"], "location": "l"})
    monkeypatch.setattr(wc, "_bearer", lambda: "token")
    monkeypatch.setattr(wc, "_search_repositories", lambda *a: (["projects/p/locations/l/repositories/r"], []))
    seen = []

    def fake_list(url, key):
        seen.append(url)
        if key == "releaseConfigs":
            return [{"releaseCompilationResult": "projects/p/locations/l/repositories/r/compilationResults/c1"}]
        return [
            {"target": {"database": "d", "schema": "raw", "name": "orders"}, "declaration": {}},
            {"target": {"database": "d", "schema": "m", "name": "x"}, "relation": {}},
        ]

    monkeypatch.setattr(wc, "_list_all", fake_list)
    assert wc.compiled_targets("https://github.com/o/r") == [(("d", "raw", "orders"), True), (("d", "m", "x"), False)]
    assert seen[-1].endswith("compilationResults/c1:query")


def graph_edges(pl):
    return {(e["upstream"]["key"], e["downstream"]["key"]) for e in pl.report()["graph"]["edges"]}


STG = 'config {{ type: "table", schema: "staging", name: "orders" }}\nSELECT id FROM {source}'
DECLARED = 'config { type: "declaration", schema: "raw", name: "orders" }'


def test_a_model_reading_a_same_named_table_elsewhere_has_an_upstream_edge(tmp_path):
    for index, source in enumerate((
        '${ref("raw", "orders")}', '${ref({ schema: "raw", name: "orders" })}', "`proj.raw.orders`", "raw.orders",
    )):
        root = tmp_path / str(index)
        pl = project(root, {"definitions/d.sqlx": DECLARED, "definitions/s.sqlx": STG.format(source=source)})
        assert pl.upstream["proj.staging.orders"] == {"proj.raw.orders"}, source


def test_a_same_named_source_that_is_not_declared_is_not_the_model_itself(tmp_path):
    for index, source in enumerate(('${ref("raw", "orders")}', "raw.orders", '${ref("lake", "raw", "orders")}')):
        root = tmp_path / str(index)
        pl = project(root, {"definitions/s.sqlx": STG.format(source=source)})
        edges = {up for up, down in graph_edges(pl) if down == "proj.staging.orders"}
        assert edges and "proj.staging.orders" not in edges, (source, edges)
        assert "proj.staging.orders" not in pl.upstream["proj.staging.orders"]


def test_a_qualified_name_never_resolves_to_a_model_that_only_shares_the_table_name(tmp_path):
    pl = project(tmp_path, {
        "definitions/a.sqlx": 'config { type: "table", schema: "staging", name: "orders" }\nSELECT 1 AS id',
        "definitions/b.sqlx": 'config { type: "table", schema: "marts", name: "report" }\nSELECT id FROM raw.orders',
    })
    assert pl.upstream["proj.marts.report"] == set()
    assert pl.models["proj.marts.report"] is not None


def test_two_models_with_one_name_are_each_read_through_their_own_schema(tmp_path):
    pl = project(tmp_path, {
        "definitions/a.sqlx": 'config { type: "table", schema: "a", name: "t" }\nSELECT 1 AS id',
        "definitions/b.sqlx": 'config { type: "table", schema: "b", name: "t" }\nSELECT id FROM ${ref("a", "t")}',
        "definitions/c.sqlx": 'config { type: "table", schema: "c", name: "t" }\nSELECT id FROM ${ref({ schema: "b", name: "t" })}',
    })
    assert pl.upstream["proj.b.t"] == {"proj.a.t"}
    assert pl.upstream["proj.c.t"] == {"proj.b.t"}


SOURCES_MODULE = 'module.exports = {\n  SOURCES: [\n    { database: "lake", schema: "raw", name: "orders" },\n    { schema: "ext", name: "users" }, // no database: the project default\n  ],\n};\n'


def test_declarations_loaded_through_require_are_followed(tmp_path):
    pl = project(tmp_path, {
        "includes/sources.js": SOURCES_MODULE,
        "definitions/decl.js": 'const { SOURCES } = require("includes/sources");\nSOURCES.forEach((s) => declare({ database: s.database, schema: s.schema, name: s.name }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("orders")} JOIN ${ref("ext", "users")} USING (id)',
    })
    # The second source names no database; the loop leaves it to the project default, which is what Dataform does.
    assert reads(pl) == {"lake.raw.orders", "proj.ext.users"} or reads(pl) == {"lake.raw.orders", ".ext.users"}


def test_require_with_relative_path_exports_member_and_for_of(tmp_path):
    pl = project(tmp_path, {
        "includes/tables.js": 'exports.list = ["orders", "users"];\n',
        "definitions/sub/decl.js": 'const t = require("../../includes/tables");\nfor (const name of t.list) { declare({ schema: "raw", name }); }\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("orders")} JOIN ${ref("raw", "users")} USING (id)',
    })
    assert reads(pl) == {"proj.raw.orders", "proj.raw.users"}


def test_two_part_refs_take_the_database_from_the_declaration_not_the_project(tmp_path):
    pl = project(tmp_path, {
        "definitions/d.sqlx": 'config { type: "declaration", database: "lake", schema: "raw", name: "orders" }',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("raw", "orders")}',
    })
    assert reads(pl) == {"lake.raw.orders"}


def test_a_one_part_ref_uses_its_declaration_before_the_default_schema(tmp_path):
    pl = project(tmp_path, {
        "definitions/d.js": 'declare({ database: "lake", schema: "raw", name: "orders" });',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("orders")}',
    })
    assert reads(pl) == {"lake.raw.orders"}


def test_a_required_list_that_is_computed_stays_unresolved(tmp_path):
    pl = project(tmp_path, {
        "includes/sources.js": 'module.exports = { SOURCES: buildSources() };\n',
        "definitions/decl.js": 'const { SOURCES } = require("includes/sources");\nSOURCES.forEach((s) => declare({ schema: s.schema, name: s.name }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("orders")}',
    })
    assert reads(pl) == set()
    assert "js_declaration_dynamic" in {d.code for d in pl.diagnostics}


def test_an_unresolved_ref_adds_no_placeholder_node_to_the_graph(tmp_path):
    from kumosql.graph import build_query_graph

    pl = project(tmp_path, {
        "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    graph = build_query_graph(pl).to_json()
    assert not any("sqlx_token" in node["id"] for node in graph["nodes"])
    assert "unresolved_template" in {d.code for d in pl.all_diagnostics()}


def test_a_two_part_ref_to_an_unlisted_name_is_exact_when_no_declaration_sets_a_database(tmp_path):
    pl = project(tmp_path, {
        "definitions/decl.js": 'getTables().forEach((t) => declare({ schema: "raw", name: t }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("raw", "orders")} JOIN ${ref("orders2")} USING (id)',
    })
    assert {d.key for d in pl.models["proj.analytics.m"].declared_dependencies} == {"proj.raw.orders"}
    assert {"js_declaration_dynamic", "unsupported_ref", "compiled_graph_not_requested"} <= {d.code for d in pl.diagnostics}


def test_a_two_part_ref_stays_unresolved_when_a_computed_declaration_may_set_the_database(tmp_path):
    pl = project(tmp_path, {
        "definitions/decl.js": 'getTables().forEach((t) => declare({ database: t.db, schema: "raw", name: t.name }));\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT 1 FROM ${ref("raw", "orders")}',
    })
    assert pl.models["proj.analytics.m"].declared_dependencies == ()


def test_a_second_copy_of_the_declarations_does_not_make_refs_ambiguous(tmp_path):
    decl = 'declare({ schema: "raw", name: "orders" });\n'
    model = 'config { type: "table" }\nSELECT id FROM ${ref("orders")}'
    for copy in ("definitions", "backup/definitions", "wt/definitions"):
        (tmp_path / copy).mkdir(parents=True)
        (tmp_path / copy / "src.js").write_text(decl)
    (tmp_path / "wt" / ".git").write_text("gitdir: elsewhere\n")  # a git worktree
    (tmp_path / "backup" / "workflow_settings.yaml").write_text("defaultDataset: other\n")  # a second project root
    pl = project(tmp_path, {"definitions/m.sqlx": model})
    assert reads(pl) == {"proj.raw.orders"}
    assert not [d for d in pl.diagnostics if d.code == "unsupported_ref"]


def test_copies_outside_definitions_are_not_scanned_for_declarations(tmp_path):
    pl = project(tmp_path, {
        "definitions/src.js": 'declare({ schema: "raw", name: "orders" });\n',
        "vendored/definitions/src.js": 'declare({ schema: "raw", name: "orders" });\n',
        "vendored/other.js": 'declare({ schema: "stale", name: "orders" });\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"proj.raw.orders"}


def test_identical_declarations_repeated_in_one_project_are_one_table(tmp_path):
    pl = project(tmp_path, {
        "definitions/a.js": 'declare({ schema: "raw", name: "orders" });\n',
        "definitions/b.js": 'declare({ schema: "raw", name: "orders" });\n',
        "definitions/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"proj.raw.orders"}


def test_a_nested_copy_without_a_definitions_folder_is_not_read(tmp_path):
    pl = project(tmp_path, {
        "src.js": 'declare({ schema: "raw", name: "orders" });\n',
        "m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
        "wt/src.js": 'declare({ schema: "raw", name: "orders" });\n',
        "wt/.git": "gitdir: elsewhere\n",
        "wt/m.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("orders")}',
    })
    assert reads(pl) == {"proj.raw.orders"}
    assert set(pl.models) == {"proj.analytics.m"}
