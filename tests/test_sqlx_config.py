"""The SQLX config code mod: a JavaScript reader that keeps spans, the edit language, and preview / plan / apply.

Every fixture is written by hand in a deliberately different layout. The checks are on exact bytes: an edit changes
only the text it means to, inside the config block, and keeps the file's line ends, BOM, indentation, quotes and
trailing-comma habit.
"""

from __future__ import annotations

import hashlib
import json
import random
import re

import pytest

from kumosql import sqlx_config as sc


def edit(text: str | bytes, command: str, path: str = "definitions/m.sqlx") -> sc.FileResult:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return sc.edit_file(data, path, sc.parse_command(command))


def after(text: str, command: str, path: str = "definitions/m.sqlx") -> str:
    result = edit(text, command, path)
    assert result.status == "changed", (result.status, result.reason)
    return result.after.decode("utf-8")


def project(tmp_path, files: dict[str, str | bytes]):
    root = tmp_path / "project"
    (root / "definitions").mkdir(parents=True)
    (root / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n", encoding="utf-8")
    for name, body in files.items():
        path = root / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8") if isinstance(body, str) else body)
    return root


# ------------------------------------------------------------------------------------------------ reading


def test_reader_keeps_spans_of_keys_and_values():
    text = 'config {\n  type: "table", // the kind\n  \'schema\': `raw`,\n  tags: ["a", /* b */ "c",],\n  n: -2,\n}\nSELECT 1\n'
    block = sc.find_config(text)
    members = {e.key: e for e in block.root.entries}
    assert list(members) == ["type", "schema", "tags", "n"]
    assert text[members["type"].value.start : members["type"].value.end] == '"table"'
    assert members["schema"].key_quote == "'" and members["schema"].value.value == "raw"
    assert members["schema"].value.quote == "`"
    tags = members["tags"].value
    assert [e.value.value for e in tags.entries] == ["a", "c"] and tags.entries[-1].comma is not None
    assert members["n"].value.value == -2.0
    assert text[block.start] == "{" and text[block.end - 1] == "}" and text[block.end :] == "\nSELECT 1\n"


def test_reader_marks_computed_parts():
    text = ('config {\n  type: dataform.projectConfig.vars.kind,\n  description: `built ${when(x, "a")}`,\n'
            '  tags: ["a", constants.TAG],\n  bigquery: { ...shared, labels: { team: "x" } },\n  name: f("x") + "y",\n}\n')
    block = sc.find_config(text)
    members = {e.key: e.value for e in block.root.entries}
    assert isinstance(members["type"], sc.Computed) and members["type"].reason == "a variable"
    assert members["description"].reason == "a template literal with ${...}"
    assert isinstance(members["tags"].entries[1].value, sc.Computed)
    assert members["bigquery"].entries[0].kind == "spread"
    assert members["name"].reason == "a function call"


def test_reader_finds_the_block_after_comments_and_rejects_two():
    assert sc.find_config("-- header\nconfig { type: 'view' }\nSELECT 1") is not None
    assert sc.find_config("SELECT 1") is None
    with pytest.raises(sc.ConfigSyntaxError, match="two config blocks"):
        sc.find_config("config { type: 'view' }\nconfig { type: 'table' }\nSELECT 1")


def test_strip_comments_keeps_strings_and_templates_whole():
    text = 'a // x "y"\nb /* } */ "c // d" `e ${ `f // g` } h` \'i /* j */\''
    assert sc.strip_comments(text) == 'a  \nb   "c // d" `e ${ `f // g` } h` \'i /* j */\''
    assert sc.strip_comments("no comments") == "no comments"


# ------------------------------------------------------------------------------------------------ formats


def test_one_line_block_gets_an_inline_label():
    text = 'config { type: "table", schema: "mart" }\nSELECT 1 AS x\n'
    assert after(text, 'labels.team = "finance"') == (
        'config { type: "table", schema: "mart", bigquery: { labels: { team: "finance" } } }\nSELECT 1 AS x\n')


def test_multi_line_block_without_trailing_commas_adds_a_comma_to_the_last_member():
    text = 'config {\n  type: "view",\n  schema: "mart" // reporting\n}\nSELECT 1\n'
    assert after(text, 'labels.team = "finance"') == (
        'config {\n  type: "view",\n  schema: "mart", // reporting\n'
        '  bigquery: {\n    labels: {\n      team: "finance"\n    }\n  }\n}\nSELECT 1\n')


def test_trailing_commas_four_spaces_and_single_quotes_are_matched():
    text = "config {\n    type: 'table',\n    tags: ['daily'],\n}\nSELECT 1\n"
    assert after(text, 'labels.team = "finance"') == (
        "config {\n    type: 'table',\n    tags: ['daily'],\n"
        "    bigquery: {\n        labels: {\n            team: 'finance',\n        },\n    },\n}\nSELECT 1\n")


def test_tabs_and_quoted_keys_are_matched():
    text = 'config {\n\t"type": "table",\n\t"bigquery": {\n\t\t"partitionBy": "DATE(ts)"\n\t}\n}\nSELECT 1\n'
    assert after(text, 'labels.team = "finance"') == (
        'config {\n\t"type": "table",\n\t"bigquery": {\n\t\t"partitionBy": "DATE(ts)",\n'
        '\t\t"labels": {\n\t\t\t"team": "finance"\n\t\t}\n\t}\n}\nSELECT 1\n')


def test_existing_labels_get_a_new_key_and_a_changed_value_keeps_its_quote():
    text = ("config {\n  type: \"table\",\n  bigquery: {\n    labels: { env: 'dev', team: 'ops' },\n"
            "    clusterBy: [\"id\"],\n  },\n}\nSELECT 1\n")
    assert after(text, 'labels.team = "finance"') == text.replace("team: 'ops'", "team: 'finance'")
    # A key that needs quotes takes its neighbours' quote, like a new string value.
    assert after(text, 'labels."cost-center" = "c1"') == text.replace("team: 'ops' }", "team: 'ops', 'cost-center': 'c1' }")


def test_crlf_files_keep_crlf_and_change_only_the_block():
    text = 'config {\r\n  type: "table",\r\n  tags: ["a"]\r\n}\r\nSELECT 1\r\nFROM x\r\n'
    out = after(text, 'labels.team = "finance"; tags += "b"')
    assert "\n" not in out.replace("\r\n", "")
    assert out == ('config {\r\n  type: "table",\r\n  tags: ["a", "b"],\r\n  bigquery: {\r\n    labels: {\r\n'
                   '      team: "finance"\r\n    }\r\n  }\r\n}\r\nSELECT 1\r\nFROM x\r\n')
    assert edit(text, 'labels.team = "finance"').line_ends == "crlf"


def test_a_bom_is_kept():
    data = b"\xef\xbb\xbfconfig { type: \"table\" }\nSELECT 1\n"
    result = edit(data, 'tags += "x"')
    assert result.bom and result.after == b"\xef\xbb\xbfconfig { type: \"table\", tags: [\"x\"] }\nSELECT 1\n"


def test_an_empty_block_and_an_empty_multi_line_object():
    assert after("config {}\nSELECT 1", 'tags += "x"') == 'config { tags: ["x"] }\nSELECT 1'
    text = "config {\n  type: 'table',\n  bigquery: {\n  },\n}\nSELECT 1"
    assert after(text, "labels.team = 'f'") == (
        "config {\n  type: 'table',\n  bigquery: {\n    labels: {\n      team: 'f',\n    },\n  },\n}\nSELECT 1")


def test_comment_lines_and_block_comments_stay_where_they_are():
    text = ('config {\n  type: "table", /* a block\n  comment */\n  // tags: ["old"],\n  schema: "s"\n'
            '  // the end\n}\nSELECT 1\n')
    assert after(text, 'tags += "x"') == (
        'config {\n  type: "table", /* a block\n  comment */\n  // tags: ["old"],\n  schema: "s",\n  tags: ["x"]\n'
        '  // the end\n}\nSELECT 1\n')


def test_values_of_every_kind_and_multi_line_replacements():
    text = 'config {\n  type: "table",\n  disabled: false,\n  columns: {\n    id: "the id",\n  },\n}\nSELECT 1'
    assert after(text, "disabled = true") == text.replace("false", "true")
    assert after(text, "bigquery.partitionExpirationDays = 30") == text.replace(
        "  },\n}", "  },\n  bigquery: {\n    partitionExpirationDays: 30,\n  },\n}")
    assert after(text, 'columns = {id: "key", ts: "time"}') == text.replace(
        '{\n    id: "the id",\n  }', '{\n    id: "key",\n    ts: "time",\n  }')
    assert after(text, 'description = "It\'s \\"quoted\\""') == text.replace(
        "  },\n}", '  },\n  description: "It\'s \\"quoted\\"",\n}')


# ------------------------------------------------------------------------------------------------ computed parts


@pytest.mark.parametrize("config, command, reason", [
    ('{ type: "table", bigquery: sharedOptions }', 'labels.team = "x"', "bigquery is computed (a variable: sharedOptions)"),
    ('{ type: "table", bigquery: { ...base, labels: {} } }', 'labels.team = "x"', "bigquery holds a spread"),
    ('{ type: "table", bigquery: { labels: constants.LABELS } }', 'labels.team = "x"', "bigquery.labels is computed"),
    ('{ type: "table", description: `as of ${vars.day}` }', 'description = "x"', "a template literal with ${...}"),
    ('{ type: "table", tags: ["a", constants.T] }', 'tags += "b"', "tags holds a computed entry"),
    ('{ type: "table", tags: base.concat(["a"]) }', 'tags -= "a"', "tags is computed (a function call"),
    ('{ type: "table", tags: ["a"], tags: ["b"] }', 'tags += "c"', "tags is written twice"),
    ('{ type: dataform.projectConfig.vars.kind }', 'tags += "c"', "type is computed (a variable"),
])
def test_an_edit_that_touches_a_computed_part_is_refused(config, command, reason):
    result = edit(f"config {config}\nSELECT 1", command)
    assert result.status == "refused" and reason in result.reason and not result.after


def test_computed_parts_elsewhere_do_not_stop_an_edit():
    text = 'config {\n  type: "table",\n  schema: dataform.projectConfig.vars.mart,\n  tags: ["a"],\n}\nSELECT ${ref("x")}'
    assert after(text, 'tags += "b"') == text.replace('["a"]', '["a", "b"]')


def test_unreadable_blocks_and_files_are_refused_with_the_reason():
    assert "could not be read: expected ',' between two values" in edit("config { type: 'table' tags: [] }\nSELECT 1", 'tags += "x"').reason
    assert "expected ',' before the next key" in edit("config { schema: vars.s tags: [] }\nSELECT 1", 'tags += "x"').reason
    assert "expected ',' between two values" in edit("config { tags: ['a' 'b'] }\nSELECT 1", 'tags += "x"').reason
    assert "expected ',' or '}'" in edit("config { type: 'table'; }\nSELECT 1", 'tags += "x"').reason
    assert edit("config { type: 'table', name: x ? 'a' : 'b' }\nSELECT 1", 'tags += "x"').status == "changed"
    assert edit("config { type: 'table', }}\nSELECT 1", 'tags += "x"').status == "changed"  # the second } is SQL text
    assert "line break inside a string" in edit("config { description: 'a\nb' }\nSELECT 1", 'tags += "x"').reason
    assert edit(b"config { type: 'table' }\n-- caf\xe9\n", 'tags += "x"').reason.startswith("not UTF-8")
    assert edit("SELECT 1", 'tags += "x"').reason.startswith("no config block")


# ------------------------------------------------------------------------------------------------ assertions


ASSERTION = 'config {\n  type: "assertion",\n  tags: ["dq"],\n}\nSELECT * FROM ${ref("t")} WHERE id IS NULL\n'
TABLE_WITH_ASSERTIONS = ('config {\n  type: "table",\n  assertions: {\n    uniqueKey: ["id"],\n    nonNull: ["id"]\n  },\n'
                         '  tags: ["daily"]\n}\nSELECT 1 AS id\n')


def test_assertion_files_are_left_alone_unless_named():
    result = edit(ASSERTION, 'tags += "finance"')
    assert result.status == "skipped" and result.category == "assertion" and not result.after
    assert edit(ASSERTION, 'tags += "finance" where name ~ "."').status == "skipped"
    assert edit(ASSERTION, 'tags += "finance" where not type in (table)').status == "skipped"
    named = edit(ASSERTION, 'tags += "finance" where type in (table, assertion)')
    assert named.status == "changed" and '["dq", "finance"]' in named.after.decode()


def test_the_inline_assertions_block_is_edited_only_by_its_own_path():
    out = after(TABLE_WITH_ASSERTIONS, 'labels.team = "f"; tags += "x"')
    inline = '  assertions: {\n    uniqueKey: ["id"],\n    nonNull: ["id"]\n  },\n'
    assert inline in out
    assert after(TABLE_WITH_ASSERTIONS, 'assertions.nonNull += "ts"').replace('["id", "ts"]', '["id"]', 1) == TABLE_WITH_ASSERTIONS


def test_labels_apply_only_to_tables_views_and_incremental_tables():
    operations = 'config { type: "operations", hasOutput: true }\nCREATE TABLE x AS SELECT 1'
    result = edit(operations, 'labels.team = "f"')
    assert result.status == "skipped" and result.category == "labels-type"
    assert edit(operations, 'bigquery.labels.team = "f"').status == "changed"
    assert edit('config { type: "incremental" }\nSELECT 1', 'labels.team = "f"').status == "changed"
    assert edit("SELECT 1\n", 'labels.team = "f"').status == "refused"  # no block: a table, but nothing to edit


def test_a_top_level_labels_member_makes_the_alias_ambiguous():
    text = 'config { type: "table", labels: { team: "a" } }\nSELECT 1'
    result = edit(text, 'labels.team = "b"')
    assert result.status == "refused" and "ambiguous" in result.reason
    assert after(text, 'config.labels.team = "b"') == text.replace('"a"', '"b"')
    assert after(text, 'bigquery.labels.team = "b"').endswith('labels: { team: "a" }, bigquery: { labels: { team: "b" } } }\nSELECT 1')


# ------------------------------------------------------------------------------------------------ tags and delete


def test_tags_add_and_remove_are_idempotent():
    text = 'config {\n  type: "table",\n  tags: [\n    "a",\n    "old",\n    "b"\n  ]\n}\nSELECT 1'
    once = after(text, 'tags += "x"')
    assert once == text.replace('"b"\n', '"b",\n    "x"\n')
    assert edit(once, 'tags += "x"').status == "unchanged"
    removed = after(text, 'tags -= "old"')
    assert removed == text.replace('    "old",\n', "")
    assert edit(removed, 'tags -= "old"').status == "unchanged"
    assert after(text, 'tags -= "b"') == text.replace('"old",\n    "b"\n', '"old"\n')
    assert after('config { tags: ["a", "b", "a"] }', 'tags -= "a"') == 'config { tags: ["b"] }'
    assert after('config { tags: ["a",] }', 'tags += "b"') == 'config { tags: ["a", "b",] }'
    assert after('config { tags: ["a", "b",] }', 'tags -= "b"') == 'config { tags: ["a",] }'
    assert after('config { tags: "a" }', 'tags += "b"') == 'config { tags: ["a", "b"] }'
    assert after('config { tags: "a" }', 'tags -= "a"') == "config { tags: [] }"
    assert after("config { tags: [\n  'a'\n] }", 'tags -= "a"') == "config { tags: [] }"
    assert edit('config { tags: "a" }', 'tags += "a"').status == "unchanged"


def test_delete_removes_a_member_and_keeps_the_comma_habit():
    text = ('config {\n  type: "table",\n  bigquery: {\n    labels: {\n      tmp: "1", // temporary\n'
            '      team: "f"\n    }\n  }\n}\nSELECT 1')
    assert after(text, "delete labels.tmp") == text.replace('      tmp: "1", // temporary\n', "")
    assert after(text, "delete labels.team") == text.replace('"1", // temporary\n      team: "f"\n', '"1" // temporary\n')
    assert after('config { a: 1, b: 2, c: 3 }', "delete config.b") == "config { a: 1, c: 3 }"
    assert after('config { a: 1, b: 2 }', "delete config.b") == "config { a: 1 }"
    assert after('config { type: "table", bigquery: { labels: { tmp: "x" } } }', "delete labels.tmp") == (
        'config { type: "table", bigquery: { labels: {} } }')
    assert edit(text, "delete labels.missing").status == "unchanged"
    assert edit('config { tags: vars.t }', "delete tags").status == "refused"


def test_a_no_op_leaves_the_bytes_alone():
    text = "config {\r\n  type: 'table',\r\n  bigquery: { labels: { team: 'finance' } },\r\n}\r\nSELECT 1"
    result = edit(text, 'labels.team = "finance"; tags -= "missing"')
    assert result.status == "unchanged" and result.after == b"" and result.sha256 == hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ the language


def test_filters_select_by_type_name_tags_file_and_values():
    table = 'config { type: "table", name: "stg_orders", tags: ["pii", "daily"], bigquery: { labels: { team: "ops" } } }\nSELECT 1'
    facts = sc._Facts("definitions/staging/a.sqlx", table, sc.find_config(table))

    def check(where: str):
        return sc._evaluate(sc.parse_command(f"tags += 'x' where {where}").where, facts)[0]

    assert check('type in (table, view) and name ~ "^stg_" and tags has "pii"') is True
    assert check('tags has pii and not tags has "weekly"') is True
    assert check('type = view or labels.team = "ops"') is True
    assert check('labels has team and labels.team != "finance"') is True
    assert check('file like "definitions/staging/**" and file like "**/*.sqlx" and not file like "definitions/*.sqlx"') is True
    assert check("tags in (weekly, monthly)") is False
    assert check("labels.missing = x") is False and check("labels.missing != x") is True
    view = sc.find_config("config { type: 'view' }")
    assert sc._evaluate(sc.parse_command("tags += 'x' where name = m").where, sc._Facts("definitions/m.sqlx", "", view))[0] is True


def test_a_filter_on_a_computed_value_refuses_the_file():
    result = edit('config { type: "table", tags: constants.TAGS }\nSELECT 1', 'labels.x = "y" where tags has "pii"')
    assert result.status == "refused" and "cannot be decided" in result.reason
    assert edit('config { type: "table", tags: ["pii", constants.T] }', 'labels.x = "y" where tags has "pii"').status == "changed"
    assert edit('config { type: "table", tags: constants.TAGS }', 'labels.x = "y" where name = "other"').status == "unmatched"


@pytest.mark.parametrize("command, message", [
    ('labels.x = "y" where type in (tabel)', "not a Dataform action type"),
    ('labels.x = y z', "must be written out"),
    ('tags += x where type in (table) z', "unexpected 'z'"),
    ("labels.x = vars.y", "must be written out"),
    ("tags += [1]", "+= and -= take one string or number"),
    ("where type in (table)", "an edit is missing"),
    ("labels.x", "expected =, += or -="),
    ('labels.x = "y" where name ~ "("', "not a regular expression"),
])
def test_bad_commands_say_what_is_wrong(command, message):
    with pytest.raises(sc.CommandError, match=re.escape(message)):
        sc.parse_command(command)


def test_paths_and_aliases():
    command = sc.parse_command('labels.team = "f"; config.labels.x = 1; bigquery.labels."cost-center" = c; delete tags')
    assert [e.path.keys for e in command.edits] == [
        ("bigquery", "labels", "team"), ("labels", "x"), ("bigquery", "labels", "cost-center"), ("tags",)]
    assert [e.op for e in command.edits] == ["set", "set", "set", "delete"]
    assert command.edits[0].path.alias and not command.edits[1].path.alias
    assert command.edits[2].value == "c"  # a bare word is a string
    assert sc.parse_command("tags += x where type in (assertion)").names_assertions


# ------------------------------------------------------------------------------------------------ plan and apply


def test_preview_plan_and_apply_with_hash_refusal(tmp_path):
    root = project(tmp_path, {
        "a.sqlx": 'config { type: "table" }\nSELECT 1',
        "b.sqlx": "config {\r\n  type: 'view',\r\n}\r\nSELECT 2\r\n",
        "c.sqlx": ASSERTION,
        "d.sqlx": 'config { type: "table", bigquery: { labels: { team: "finance" } } }\nSELECT 3',
    })
    before = {p.name: p.read_bytes() for p in (root / "definitions").iterdir()}
    plan = sc.plan_edit(root, 'labels.team = "finance"')
    assert {p.name: p.read_bytes() for p in (root / "definitions").iterdir()} == before  # planning writes nothing
    summary = plan.summary()
    assert (summary["files"], summary["changed"], summary["unchanged"], summary["skipped_assertion"]) == (4, 2, 1, 1)
    saved = json.loads(json.dumps(plan.to_json()))
    assert [f["path"] for f in saved["files"]] == ["definitions/a.sqlx", "definitions/b.sqlx"]
    assert saved["files"][0]["sha256"] == hashlib.sha256(before["a.sqlx"]).hexdigest()
    (root / "definitions" / "a.sqlx").write_bytes(before["a.sqlx"] + b"\n-- edited since the plan\n")
    written, _ = sc.apply_saved_plan(root, saved)
    assert [(w.path, w.ok) for w in written] == [("definitions/a.sqlx", False), ("definitions/b.sqlx", True)]
    assert "changed since the plan" in written[0].reason
    assert (root / "definitions" / "b.sqlx").read_bytes() == (
        b"config {\r\n  type: 'view',\r\n  bigquery: {\r\n    labels: {\r\n      team: 'finance',\r\n    },\r\n  },\r\n}\r\nSELECT 2\r\n")
    assert (root / "definitions" / "c.sqlx").read_bytes() == before["c.sqlx"]
    assert (root / "definitions" / "d.sqlx").read_bytes() == before["d.sqlx"]
    again, _ = sc.apply_saved_plan(root, saved)
    assert not again[1].ok  # b's hash changed when it was written: a plan is applied once
    assert [p.name for p in (root / "definitions").iterdir() if p.name.endswith(".tmp")] == []


def test_apply_refuses_a_file_changed_between_plan_and_write(tmp_path):
    root = project(tmp_path, {"a.sqlx": 'config { type: "table" }\nSELECT 1'})
    plan = sc.plan_edit(root, 'tags += "x"')
    (root / "definitions" / "a.sqlx").write_text('config { type: "view" }\nSELECT 1', encoding="utf-8")
    [written] = sc.apply_plan(plan)
    assert not written.ok and "changed since it was planned" in written.reason
    assert (root / "definitions" / "a.sqlx").read_text(encoding="utf-8") == 'config { type: "view" }\nSELECT 1'


def test_files_globs_nested_projects_and_node_modules(tmp_path):
    root = project(tmp_path, {
        "staging/stg_a.sqlx": "config { type: 'view' }\nSELECT 1",
        "marts/m.sqlx": "config { type: 'table' }\nSELECT 1",
        "node_modules/pkg/x.sqlx": "config { type: 'table' }\nSELECT 1",
        "copy/workflow_settings.yaml": "defaultProject: p\n",
        "copy/definitions/y.sqlx": "config { type: 'table' }\nSELECT 1",
    })
    assert [r.path for r in sc.plan_edit(root, "tags += x").results] == ["definitions/marts/m.sqlx", "definitions/staging/stg_a.sqlx"]
    assert [r.path for r in sc.plan_edit(root, "tags += x", ["definitions/staging/**"]).results] == ["definitions/staging/stg_a.sqlx"]


def test_cli_previews_saves_a_plan_and_applies_it(tmp_path, capsys):
    root = project(tmp_path, {"a.sqlx": 'config {\n  type: "table",\n  tags: ["pii"],\n}\nSELECT 1\n', "c.sqlx": ASSERTION,
                              "x.sqlx": 'config { type: "table", tags: vars.t }\nSELECT 1'})
    plan_file = tmp_path / "plan.json"
    original = (root / "definitions" / "a.sqlx").read_bytes()
    code = sc.main(["edit", str(root), 'labels.team = "finance" where tags has "pii" and type = table', "--plan", str(plan_file)])
    out = capsys.readouterr().out
    assert code == sc.EXIT_REFUSED  # x.sqlx: the filter cannot be decided
    assert "+++ b/definitions/a.sqlx" in out and '+      team: "finance",' in out
    assert "PREVIEW: nothing was written" in out and "skipped: assertion       1" in out
    assert "definitions/x.sqlx: the where clause cannot be decided" in out
    assert (root / "definitions" / "a.sqlx").read_bytes() == original
    assert sc.main(["edit", str(root), "--apply", "--plan", str(plan_file)]) == sc.EXIT_OK
    assert "1 of 1 planned files written" in capsys.readouterr().out
    assert '      team: "finance",\n' in (root / "definitions" / "a.sqlx").read_text(encoding="utf-8")
    assert sc.main(["edit", str(root), 'tags += "x"', "--apply", "--plan", str(plan_file)]) == sc.EXIT_USAGE
    assert sc.main(["edit", str(root), "labels.team = ("]) == sc.EXIT_USAGE
    assert "sqlx-config:" in capsys.readouterr().err


def test_cli_apply_without_a_plan_and_a_command_file(tmp_path, capsys):
    root = project(tmp_path, {"a.sqlx": "config { type: 'table' }\nSELECT 1"})
    command = tmp_path / "command.txt"
    command.write_bytes(b'\xef\xbb\xbftags += "a b" where type in (table)\n')
    assert sc.main(["edit", str(root), "--command-file", str(command), "--apply", "--stat"]) == sc.EXIT_OK
    out = capsys.readouterr().out
    assert "definitions/a.sqlx  +1 -1  tags: added [\"a b\"]" in out and "written                  1" in out
    assert (root / "definitions" / "a.sqlx").read_text(encoding="utf-8") == "config { type: 'table', tags: ['a b'] }\nSELECT 1"


def test_cli_show_lists_values(tmp_path, capsys):
    root = project(tmp_path, {"a.sqlx": 'config { type: "table", tags: ["pii"], bigquery: { labels: { team: "f" } } }\nSELECT 1',
                              "b.sqlx": "config { type: 'view' }\nSELECT 1", "c.sqlx": ASSERTION})
    assert sc.main(["show", str(root), "labels.team, tags, file where tags has pii or type = assertion"]) == sc.EXIT_OK
    out = capsys.readouterr().out
    assert 'definitions/a.sqlx  labels.team="f"  tags=["pii"]  file="definitions/a.sqlx"' in out
    assert "definitions/c.sqlx  labels.team=-" in out and "definitions/b.sqlx" not in out
    assert "2 of 3 files selected" in out
    assert sc.main(["show", str(root), "--json"]) == sc.EXIT_OK
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert [row["values"][0] for row in rows] == [["type", '"table"'], ["type", '"view"'], ["type", '"assertion"']]


# ------------------------------------------------------------------------------------------------ at scale


def _varied_file(number: int, rng: random.Random) -> tuple[str, str]:
    """One of many layouts, as a real project accumulates them."""

    kind = rng.choice(["table", "table", "view", "incremental", "assertion", "operations", "declaration"])
    q = rng.choice(['"', "'"])
    newline = rng.choice(["\n", "\n", "\r\n"])
    unit = rng.choice(["  ", "    ", "\t"])
    comma = rng.choice([",", ""])
    members = [f"type: {q}{kind}{q}"]
    if rng.random() < 0.5:
        members.append(f"tags: [{q}daily{q}, {q}t{number % 7}{q}]")
    if rng.random() < 0.3:
        members.append(f"description: {q}Model {number}{q} /* why */")
    if kind in ("table", "incremental") and rng.random() < 0.4:
        members.append(f"assertions: {{ uniqueKey: [{q}id{q}], nonNull: [{q}id{q}] }}")
    if rng.random() < 0.3:
        members.append(f"bigquery: {{ partitionBy: {q}DATE(ts){q}, labels: {{ env: {q}prod{q} }} }}")
    if rng.random() < 0.1 and not any(m.startswith("bigquery") for m in members):
        members.append(f"bigquery: {{ labels: {{ team: {q}finance{q} }} }}")  # already labelled: a no-op
    style = rng.choice(["one-line", "multi", "multi", "commented"])
    if style == "one-line":
        block = "config { " + ", ".join(members) + " }"
    else:
        lines = [unit + m + "," for m in members[:-1]] + [unit + members[-1] + comma]
        if style == "commented":
            lines.insert(1, unit + "// owned by the data team")
        block = "config {" + newline + newline.join(lines) + newline + "}"
    body = f"SELECT id, ts FROM ${{ref({q}src_{number}{q})}} -- config {{ not: 'a block' }}"
    bom = "﻿" if rng.random() < 0.05 else ""
    return f"m{number:03d}.sqlx", bom + block + newline + body + newline


def test_one_label_on_three_hundred_varied_files(tmp_path):
    rng = random.Random(8)
    files = dict(_varied_file(n, rng) for n in range(300))
    before = {name: body.encode("utf-8") for name, body in files.items()}
    root = project(tmp_path, before)
    plan = sc.plan_edit(root, 'labels.team = "finance"')
    summary = plan.summary()
    assert summary["files"] == 300 and summary["refused"] == 0
    assertions = sum(1 for body in files.values() if 'type: "assertion"' in body.replace("'", '"'))
    assert assertions > 20 and summary["skipped_assertion"] == assertions
    assert summary["unchanged"] > 0 and summary["skipped_labels_type"] > 0  # already labelled; operations, declarations
    written = sc.apply_plan(plan)
    assert written and all(w.ok for w in written) and len(written) == summary["changed"]
    for name in files:
        old, new = before[name], (root / "definitions" / name).read_bytes()
        old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
        result = next(r for r in plan.results if r.path == f"definitions/{name}")
        if result.status != "changed":
            assert new == old, name
            continue
        assert old.startswith(b"\xef\xbb\xbf") == new.startswith(b"\xef\xbb\xbf")
        old_block, new_block = sc.find_config(old_text.lstrip("﻿")), sc.find_config(new_text.lstrip("﻿"))
        assert old_text.lstrip("﻿")[: old_block.start] == new_text.lstrip("﻿")[: new_block.start], name
        assert old_text.lstrip("﻿")[old_block.end :] == new_text.lstrip("﻿")[new_block.end :], name
        if "\r\n" in old_text:
            assert "\n" not in new_text.replace("\r\n", ""), name
        assert sc._tree(new_block.root, new_text.lstrip("﻿"))["bigquery"]["labels"]["team"] == "finance"
        old_tree = sc._tree(old_block.root, old_text.lstrip("﻿"))
        assert old_tree.get("assertions") == sc._tree(new_block.root, new_text.lstrip("﻿")).get("assertions")
    kinds = {r.action_type for r in plan.results if r.status == "changed"}
    assert kinds <= set(sc.LABEL_TYPES) and "assertion" not in kinds
    # Done once, nothing is left to do (a sample of the folder, to keep the test short).
    again = sc.plan_edit(root, 'labels.team = "finance"', ["definitions/m0[0-4]*.sqlx"]).summary()
    assert again["files"] == 50 and again["changed"] == 0
