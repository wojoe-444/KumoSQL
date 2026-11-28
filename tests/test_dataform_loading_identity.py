"""Dataform loading must know which table an action writes and which it names, and report what the compiler would reject.

Synthetic projects from an outside audit of the two loaders against Dataform's own compiler. Expected targets were
recorded from ``@dataform/cli`` 3.0.71 (``compile --json``); nothing here runs Node.
"""

import pytest

from kumosql import load_compiled_graph, load_sqlx_project
from kumosql.pipeline_loading import _parse_ref_args, _plain_strings
from kumosql.pipeline_types import Target
from kumosql.sqlx import mask_sqlx_interpolations


def project(tmp_path, files, settings="defaultProject: p\ndefaultDataset: ds\n", name="workflow_settings.yaml"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(settings)
    for file, text in files.items():
        path = tmp_path / "definitions" / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")  # exactly these line endings, on Windows too
    return load_sqlx_project(tmp_path)


def codes(pipeline, code):
    return [(d.model, d.message) for d in pipeline.diagnostics if d.code == code]


# ------------------------------------------------------- computed names and ref arguments


@pytest.mark.parametrize("key", ["name", "schema", "database"])
def test_a_computed_identity_is_flagged_and_the_action_is_not_read_as_a_table(tmp_path, key):
    pl = project(tmp_path, {
        "a.sqlx": f'config {{ type: "table", {key}: dataform.projectConfig.vars.where }}\nSELECT 1 AS id\n',
        "b.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
    })
    (model,) = (m for m in pl.models.values() if m.path.endswith("a.sqlx"))
    assert model.kind == "unknown"
    assert key in codes(pl, "dynamic_config")[0][1]


def test_a_declaration_with_a_computed_schema_is_still_found_by_its_name(tmp_path):
    pl = project(tmp_path, {
        "src.sqlx": 'config { type: "declaration", schema: "raw_" + constants.ID, name: "events" }\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("events")}\n',
    })
    assert [t.key for t in pl.models["p.ds.r"].declared_dependencies] == ["p.ds.events"]  # the project default stands in
    assert "schema" in codes(pl, "dynamic_config")[0][1] and not codes(pl, "unsupported_ref")


def test_a_ref_never_finds_an_action_whose_name_is_computed(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", name: dataform.projectConfig.vars.n }\nSELECT 1 AS id\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("a")}\n',
    })
    # Dataform's name for the first is not "a": the ref cannot be pinned to it, and is left masked.
    assert "p.ds.r" in pl.models and pl.models["p.ds.r"].declared_dependencies == ()
    assert codes(pl, "unsupported_ref")


def test_a_computed_ref_argument_is_not_read_as_two_strings(tmp_path):
    pl = project(tmp_path, {
        "reader.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("f" + "eed")} JOIN ${resolve("f" + "eed")} USING (id)\n',
    })
    model = pl.models["p.ds.reader"]
    assert model.declared_dependencies == ()
    assert "f.eed" not in model.sql and "__sqlx_token_" in model.sql
    assert [d.code for d in pl.diagnostics if d.model == "p.ds.reader"] == ["unsupported_ref"]


@pytest.mark.parametrize("args", ['"a" + "b"', "name", '"a", schemaName', '{ name: "a" + "b" }', '{ name: "a", schema: s }', '`${x}`'])
def test_ref_arguments_that_are_not_plain_strings_raise(args):
    with pytest.raises(ValueError, match="computed"):
        _parse_ref_args(args, Target("p", "ds", ""))


def test_plain_ref_arguments_still_read():
    assert _plain_strings('"a", \'b\'') == ["a", "b"]
    assert _parse_ref_args('"s", "t"', Target("p", "ds", "")) == Target("p", "s", "t")
    assert _parse_ref_args('{ name: "t", schema: "s", database: "d" }', Target("p", "ds", "")) == Target("d", "s", "t")
    assert _parse_ref_args('"d", "s", "t"', Target("p", "ds", "")) == Target("d", "s", "t")


def test_the_projects_own_default_settings_are_not_computed(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", database: dataform.projectConfig.defaultDatabase, schema: dataform.projectConfig.defaultSchema }\n'
                  'SELECT 1 AS id\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref({ name: "a", database: dataform.projectConfig.defaultDatabase })}\n',
    })
    assert set(pl.models) == {"p.ds.a", "p.ds.r"} and pl.models["p.ds.a"].kind == "table"
    assert [t.key for t in pl.models["p.ds.r"].declared_dependencies] == ["p.ds.a"]
    assert not codes(pl, "dynamic_config") and not codes(pl, "unsupported_ref")


def test_project_vars_that_are_strings_name_the_dataset(tmp_path):
    settings = 'defaultProject: p\ndefaultDataset: ds\nvars:\n  RAW: raw_data  # where sources land\n  OUT: "out_data"\nother: x\n'
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", schema: dataform.projectConfig.vars.OUT }\nSELECT 1 AS id\n',
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref({ schema: dataform.projectConfig.vars.OUT, name: "a" })} '
                  'JOIN ${ref({ schema: dataform.projectConfig.vars.RAW, name: "x" })} USING (id)\n',
    }, settings)
    assert set(pl.models) == {"p.out_data.a", "p.ds.r"}
    assert {t.key for t in pl.models["p.ds.r"].declared_dependencies} == {"p.out_data.a", "p.raw_data.x"}
    assert not codes(pl, "dynamic_config")


def test_a_var_the_project_does_not_define_stays_computed(tmp_path):
    pl = project(tmp_path, {"a.sqlx": 'config { type: "table", schema: dataform.projectConfig.vars.NOPE }\nSELECT 1 AS id\n'})
    assert codes(pl, "dynamic_config") and pl.models["p.ds.a"].kind == "unknown"


# ------------------------------------------------------------- prefixes and suffixes

SUFFIXED = {
    "a.sqlx": 'config { type: "table", schema: "s1", database: "d1" }\nSELECT 1 AS id\n',
    "b.sqlx": 'config { type: "operations", hasOutput: true }\nSELECT 2 AS id\n',
    "c.sqlx": 'config { type: "assertion" }\nSELECT id FROM ${ref("s1", "a")} WHERE id < 0\n',
    "d.sqlx": 'config { type: "view" }\nSELECT * FROM ${ref("d1", "s1", "a")} JOIN ${ref("b")} USING (id) '
              'JOIN ${resolve("a")} USING (id) JOIN ${self()} USING (id)\n',
    "e.sqlx": 'config { type: "declaration", schema: "raw", name: "ext" }\n',
    "f.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("ext")} JOIN ${ref("raw", "ext")} USING (id)\n',
}


def test_workflow_settings_prefix_and_suffixes_rename_actions_not_declarations(tmp_path):
    pl = project(tmp_path, SUFFIXED, "defaultProject: proj\ndefaultDataset: ds\nprojectSuffix: ps\ndatasetSuffix: sbx\nnamePrefix: t\n")
    # Targets as Dataform 3.0.71 compiles them.
    assert set(pl.models) == {"d1_ps.s1_sbx.t_a", "proj_ps.ds_sbx.t_b", "proj_ps.ds_sbx.t_c", "proj_ps.ds_sbx.t_d", "proj_ps.ds_sbx.t_f"}
    assert set(pl.sources) == {"proj.raw.ext"}
    view = pl.models["proj_ps.ds_sbx.t_d"]
    assert view.sql.strip() == ("SELECT * FROM `d1_ps.s1_sbx.t_a` JOIN `proj_ps.ds_sbx.t_b` USING (id) "
                                "JOIN `d1_ps.s1_sbx.t_a` USING (id) JOIN `proj_ps.ds_sbx.t_d` USING (id)")
    assert {t.key for t in view.declared_dependencies} == {"d1_ps.s1_sbx.t_a", "proj_ps.ds_sbx.t_b"}
    assert {t.key for t in pl.models["proj_ps.ds_sbx.t_f"].declared_dependencies} == {"proj.raw.ext"}
    assert {t.key for t in pl.models["proj_ps.ds_sbx.t_c"].declared_dependencies} == {"d1_ps.s1_sbx.t_a"}
    assert pl.models["d1_ps.s1_sbx.t_a"].logical == ("d1", "s1", "a")
    assert pl.models["proj_ps.ds_sbx.t_d"].logical == ("proj", "ds", "d")
    assert not codes(pl, "missing_ref") and not codes(pl, "unsupported_ref")


def test_dataform_json_settings_apply_the_same_way(tmp_path):
    settings = '{"warehouse": "bigquery", "defaultDatabase": "proj", "defaultSchema": "ds", "schemaSuffix": "x", "tablePrefix": "pre"}'
    pl = project(tmp_path, {"a.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
                            "c.sqlx": 'config { type: "assertion" }\nSELECT id FROM ${ref("a")}\n'}, settings, "dataform.json")
    assert set(pl.models) == {"proj.ds_x.pre_a", "proj.ds_x.pre_c"}  # the assertion lives in the default dataset, suffixed


def test_without_settings_nothing_is_renamed_and_assertions_use_the_default_dataset(tmp_path):
    pl = project(tmp_path, {"a.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
                            "c.sqlx": 'config { type: "assertion" }\nSELECT id FROM ${ref("a")}\n'})
    assert set(pl.models) == {"p.ds.a", "p.ds.c"} and pl.models["p.ds.a"].logical == ()


def test_an_assertion_dataset_setting_is_kept_and_suffixed(tmp_path):
    pl = project(tmp_path, {"c.sqlx": 'config { type: "assertion" }\nSELECT 1 AS id\n'},
                 "defaultProject: p\ndefaultDataset: ds\ndefaultAssertionDataset: chk\ndatasetSuffix: x\n")
    assert set(pl.models) == {"p.chk_x.c"}


def test_the_default_location_is_read(tmp_path):
    pl = project(tmp_path, {"a.sqlx": "SELECT 1 AS id\n"}, "defaultProject: p\ndefaultDataset: ds\ndefaultLocation: EU\n")
    assert pl.default_location == "EU"


# ------------------------------------------------- what the compiler rejects, and other text


def test_refs_to_missing_names_wrong_case_and_operations_without_output_are_reported(tmp_path):
    pl = project(tmp_path, {
        "Mixed.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
        "make.sqlx": 'config { type: "operations" }\nSELECT 1 AS id\n',
        "ok.sqlx": 'config { type: "operations", hasOutput: true }\nSELECT 1 AS id\n',
        "reader.sqlx": 'config { type: "table", dependencies: ["ghost"] }\n'
                       'SELECT * FROM ${ref("mixed")} JOIN ${ref("nope")} USING (id) JOIN ${resolve("absent")} USING (id) '
                       'JOIN ${ref("make")} USING (id) JOIN ${ref("ok")} USING (id) JOIN ${ref("Mixed")} USING (id)\n',
    })
    missing = codes(pl, "missing_ref")
    assert len(missing) == 4 and all(model == "p.ds.reader" for model, _ in missing)
    assert any("'Mixed'" in message and "case-sensitive" in message for _, message in missing)
    assert any("'nope'" in message for _, message in missing) and any("'absent'" in message for _, message in missing)
    assert any("config dependency" in message and "'ghost'" in message for _, message in missing)
    assert [m for m, _ in codes(pl, "ref_to_operation_without_output")] == ["p.ds.reader"]
    assert "make" in codes(pl, "ref_to_operation_without_output")[0][1]
    assert pl.models["p.ds.ok"].has_output and not pl.models["p.ds.make"].has_output


def test_a_declaration_and_a_known_action_are_not_missing(tmp_path):
    pl = project(tmp_path, {
        "src.sqlx": 'config { type: "declaration", schema: "raw", name: "src" }\n',
        "a.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
        "r.sqlx": 'config { type: "table" }\nSELECT * FROM ${ref("src")} JOIN ${ref("a")} USING (id)\n',
    })
    assert not codes(pl, "missing_ref")


def test_disabled_is_kept_on_the_model(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table", disabled: true }\nSELECT 1 AS id\n',
        "b.sqlx": 'config { type: "table", disabled: false, description: "disabled: true" }\nSELECT 1 AS id\n',
        "c.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n',
    })
    assert [pl.models[f"p.ds.{n}"].disabled for n in "abc"] == [True, False, False]


def test_a_crlf_operations_separator_splits_the_statements(tmp_path):
    text = 'config { type: "operations" }\r\nCREATE TABLE a AS SELECT 1 AS x\r\n---\r\nCREATE TABLE b AS SELECT 2 AS x\r\n'
    pl = project(tmp_path, {"ops.sqlx": text})
    assert "\n;\n" in pl.models["p.ds.ops"].sql.replace("\r", "") and "---" not in pl.models["p.ds.ops"].sql
    assert not any(d.code in {"parse_error", "unknown_reads"} for d in pl.all_diagnostics())


def test_an_unclosed_interpolation_in_a_comment_does_not_hide_the_asset(tmp_path):
    pl = project(tmp_path, {"a.sqlx": 'config { type: "table" }\nSELECT 1 AS x -- note ${ never closed\n'})
    assert "p.ds.a" in pl.models and not codes(pl, "asset_unreadable")
    assert mask_sqlx_interpolations("SELECT 1 /* ${ open */ FROM ${ref('a')}")[0].startswith("SELECT 1 /* ${ open */ FROM __sqlx_token_")


def test_an_unclosed_interpolation_in_the_query_is_still_unreadable(tmp_path):
    pl = project(tmp_path, {"a.sqlx": 'config { type: "table" }\nSELECT ${ never closed\n'})
    assert "p.ds.a" not in pl.models and codes(pl, "asset_unreadable")


def test_the_loaded_project_survives_a_saved_snapshot(tmp_path):
    import json

    from kumosql.storage import pipeline_from_snapshot, pipeline_snapshot

    pl = project(tmp_path, {"a.sqlx": 'config { type: "table", disabled: true }\nSELECT 1 AS id\n'},
                 "defaultProject: p\ndefaultDataset: ds\nnamePrefix: t\ndefaultLocation: EU\n")
    again = pipeline_from_snapshot(json.loads(json.dumps(pipeline_snapshot(pl, "k"))), "k")
    assert again.models == pl.models and again.default_location == "EU"
    assert again.models["p.ds.t_a"].logical == ("p", "ds", "a") and again.models["p.ds.t_a"].disabled


# ------------------------------------------------------------------- compiled graphs


def compiled(**extra):
    return {
        "projectConfig": {"defaultDatabase": "proj", "defaultSchema": "ds", "defaultLocation": "EU", "warehouse": "bigquery"},
        "tables": [
            {"type": "table", "target": {"database": "proj", "schema": "ds", "name": "src"},
             "query": "SELECT 1 AS id, 2 AS flag", "disabled": False},
            {"type": "incremental", "target": {"database": "proj", "schema": "ds", "name": "inc"},
             "query": "SELECT id FROM `proj.ds.src`", "disabled": True,
             "incrementalQuery": "SELECT id FROM `proj.ds.src` WHERE flag > 0",
             "incrementalPreOps": ["DELETE FROM `proj.ds.inc` WHERE FALSE"],
             "dependencyTargets": [{"database": "proj", "schema": "ds", "name": "src"}]},
            {"type": "table", "target": {"database": "proj", "schema": "ds", "name": "out"},
             "query": "SELECT id FROM `proj.ds.inc`",
             "dependencyTargets": [{"database": "proj", "schema": "ds", "name": "inc"}]},
        ],
        "operations": [
            {"target": {"database": "proj", "schema": "ds", "name": "make"}, "queries": ["SELECT 1 AS x"], "hasOutput": True},
        ],
        **extra,
    }


def test_compiled_defaults_come_from_project_config():
    pl = load_compiled_graph(compiled())
    assert (pl.default_project, pl.default_dataset, pl.default_location) == ("proj", "ds", "EU")
    old = compiled()
    old["defaultDatabase"], old["defaultSchema"] = old["projectConfig"].pop("defaultDatabase"), old["projectConfig"].pop("defaultSchema")
    older = load_compiled_graph(old)
    assert (older.default_project, older.default_dataset) == ("proj", "ds")


def test_compiled_graph_errors_become_diagnostics():
    errors = {"compilationErrors": [
        {"fileName": "definitions/bad.sqlx", "message": 'Could not resolve "nope"', "stack": "Error: ..."},
        {"fileName": "definitions/bad.sqlx", "actionName": "proj.ds.bad", "message": "Missing dependency\n detected"},
    ]}
    pl = load_compiled_graph(compiled(graphErrors=errors))
    found = [d for d in pl.diagnostics if d.code == "compilation_error"]
    assert [(d.model, d.message) for d in found] == [
        ("definitions/bad.sqlx", 'Could not resolve "nope"'), ("proj.ds.bad", "Missing dependency detected")]
    assert not [d for d in load_compiled_graph(compiled(graphErrors={})).diagnostics if d.code == "compilation_error"]
    empty = load_compiled_graph({"graphErrors": {"compilationErrors": [{"message": "x"}]}})
    assert empty.models == {} and [d.code for d in empty.diagnostics] == ["compilation_error"]


def test_compiled_incremental_branch_disabled_and_output_are_kept():
    pl = load_compiled_graph(compiled())
    inc = pl.models["proj.ds.inc"]
    assert inc.incremental_sql == ("SELECT id FROM `proj.ds.src` WHERE flag > 0", "DELETE FROM `proj.ds.inc` WHERE FALSE")
    assert inc.disabled and not pl.models["proj.ds.src"].disabled
    assert pl.models["proj.ds.make"].has_output and not pl.models["proj.ds.src"].has_output


def test_a_column_only_the_incremental_query_reads_is_not_dead():
    pl = load_compiled_graph(compiled())
    assert "flag" not in pl.dead_columns().get("proj.ds.src", ())
    full_only = compiled()
    del full_only["tables"][1]["incrementalQuery"]
    assert "flag" in load_compiled_graph(full_only).dead_columns().get("proj.ds.src", ())


# ------------------------------------------------------------------ invalid config


def test_config_the_compiler_rejects_is_reported(tmp_path):
    pl = project(tmp_path, {
        "policy.sqlx": 'config { type: "table", bigqueryPolicy: { enabled: true } }\nSELECT 1 AS id\n',  # audit case: top-level object
        "key.sqlx": 'config { type: "table", uniqueKey: ["id"] }\nSELECT 1 AS id\n',  # an incremental-only key
        "view.sqlx": 'config { type: "view", updatePartitionFilter: "id > 0" }\nSELECT 1 AS id\n',
        "assert.sqlx": 'config { type: "assertion", partitionBy: "id" }\nSELECT 1 AS id WHERE FALSE\n',
        "type.sqlx": 'config { type: "inline" }\nSELECT 1 AS id\n',
        "typo.sqlx": "config { hasoutput: true }\nSELECT 1 AS id\n",
    })
    found = {path: message for path, message in codes(pl, "invalid_config")}
    assert set(found) == {f"definitions/{n}.sqlx" for n in ("policy", "key", "view", "assert", "type", "typo")}
    assert "'bigqueryPolicy'" in found["definitions/policy.sqlx"] and "'uniqueKey'" in found["definitions/key.sqlx"]
    assert "'inline'" in found["definitions/type.sqlx"] and "Dataform rejects the project" in found["definitions/typo.sqlx"]
    assert "p.ds.policy" in pl.models  # still loaded, so the rest of the project can be analysed


def test_config_the_compiler_accepts_is_not_reported(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "incremental", uniqueKey: ["id"], onSchemaChange: "IGNORE", protected: true, tags: ["t"], '
                  'bigquery: { partitionBy: "id", clusterBy: ["id"], updatePartitionFilter: "id > 0" }, '
                  'assertions: { nonNull: ["id"] }, columns: { id: "the id" }, description: "d: 1", hermetic: false }\nSELECT 1 AS id\n',
        "b.sqlx": 'config { type: "operations", hasOutput: true, dependencies: ["a"] }\nSELECT 1 AS id\n',
        "c.sqlx": 'config { type: "declaration", schema: "raw", name: "c", columns: {} }\n',
        "d.sqlx": "config { schema: \"x\", partitionBy: \"id\" }\nSELECT 1 AS id\n",  # no type: a table
        "e.sqlx": "SELECT 1 AS id\n",
        "f.sqlx": "config { type: dataform.projectConfig.vars.kind, bigqueryPolicy: {} }\nSELECT 1 AS id\n",  # computed type: not judged
    })
    assert not codes(pl, "invalid_config")


# --------------------------------------------------------------- JavaScript-published actions

PUBLISH = '''
publish("generated", { type: "table", tags: ["g"] }).query(ctx => `SELECT 1 AS id, 2 AS unused`);
publish("viewed").query((ctx) => `SELECT id FROM ${ctx.ref("generated")}`).config({ type: "view" });
publish("plain", ctx => "SELECT 3 AS id");
publish("block", { type: "table" }, (ctx) => { return `SELECT 4 AS id` });
function later() { publish("inside").query("SELECT 5 AS id"); }
["x", "y"].forEach((n) => publish(n).query("SELECT 6 AS id"));
publish("computed").query(ctx => "SELECT " + 7 + " AS id");
'''


def test_literal_js_publish_actions_become_models(tmp_path):
    pl = project(tmp_path, {
        "gen.js": PUBLISH,
        "r.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("viewed")} JOIN ${ref("plain")} USING (id)\n',
    })
    assert {"p.ds.generated", "p.ds.viewed", "p.ds.plain", "p.ds.block"} <= set(pl.models)
    generated, viewed = pl.models["p.ds.generated"], pl.models["p.ds.viewed"]
    assert (generated.kind, generated.tags, generated.path) == ("table", ("g",), "definitions/gen.js")
    assert generated.sql.strip() == "SELECT 1 AS id, 2 AS unused" and viewed.kind == "view"
    assert [t.key for t in viewed.declared_dependencies] == ["p.ds.generated"]
    assert {t.key for t in pl.models["p.ds.r"].declared_dependencies} == {"p.ds.viewed", "p.ds.plain"}
    # what only running the JavaScript could produce is not guessed (it is listed by the declaration scan)
    assert not {"p.ds.inside", "p.ds.x", "p.ds.y", "p.ds.computed"} & set(pl.models)
    assert "unused" in pl.dead_columns()["p.ds.generated"]


def test_js_publish_follows_prefixes_and_ignores_includes(tmp_path):
    pl = project(tmp_path, {"gen.js": 'publish("a").query("SELECT 1 AS id");\n'},
                 "defaultProject: p\ndefaultDataset: ds\nnamePrefix: t\n")
    assert set(pl.models) == {"p.ds.t_a"} and pl.models["p.ds.t_a"].logical == ("p", "ds", "a")
    second = tmp_path / "second"
    again = project(second, {"x.sqlx": "SELECT 1 AS id\n"}, "defaultProject: p\ndefaultDataset: ds\n")
    (second / "includes").mkdir()
    (second / "includes" / "lib.js").write_text('publish("a").query("SELECT 1 AS id");\n')
    assert set(load_sqlx_project(second).models) == set(again.models) == {"p.ds.x"}  # includes are required, not run as definitions
