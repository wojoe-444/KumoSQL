"""A model's ``assertions: { uniqueKey }`` is its declared grain, with no ``declared_grain`` argument.

End to end: a Dataform folder (``load_sqlx_project``) or the JSON of ``dataform compile --json`` (``load_compiled_graph``),
then ``profile_pipeline(pipeline)`` with nothing else. The compiled-graph cases copy what Dataform 3.0.71 printed for the
same configs (a table entry keeps no ``assertions`` block; each key becomes an assertion with a ``parentAction``).
"""

from __future__ import annotations

import json

from kumosql import load_compiled_graph, load_sqlx_project, profile_pipeline
from kumosql.pipeline import Pipeline
from kumosql.pipeline_loading import _config_assertions, _generated_unique_key, declared_unique_keys
from kumosql.pipeline_types import Model, Target
from kumosql.table_profile import _grain_context, profile_query
from kumosql.trace_memo import definition_digest

SETTINGS = "defaultProject: p\ndefaultDataset: ds\ndefaultAssertionDataset: ds_assert\n"
EVENTS = 'config { type: "declaration", schema: "raw", name: "events" }\n'


def project(tmp_path, files):
    (tmp_path / "workflow_settings.yaml").write_text(SETTINGS)
    for name, text in files.items():
        path = tmp_path / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


def grains(pipeline):
    return {key.split(".")[-1]: (p.grain.status, p.grain.keys) if p.grain.known else ("unknown", p.grain.reason)
            for key, p in profile_pipeline(pipeline).items()}


def sqlx_grains(tmp_path, files):
    return grains(project(tmp_path, {"events.sqlx": EVENTS, **files}))


# ------------------------------------------------------------------ SQLX models


def test_a_table_view_and_incremental_with_a_unique_key_are_declared(tmp_path):
    got = sqlx_grains(tmp_path, {
        "t.sqlx": 'config { type: "table", assertions: { uniqueKey: ["id"] } }\nSELECT id, day FROM ${ref("events")}\n',
        "v.sqlx": 'config { type: "view", assertions: { uniqueKey: ["id", "day"] } }\nSELECT id, day FROM ${ref("events")}\n',
        "i.sqlx": 'config { type: "incremental", assertions: { uniqueKey: ["id"] } }\nSELECT id, day FROM ${ref("events")}\n',
        "plain.sqlx": 'config { type: "table" }\nSELECT id, day FROM ${ref("events")}\n',
    })
    assert got["t"] == ("declared", ("id",))
    assert got["v"] == ("declared", ("day", "id"))
    assert got["i"] == ("declared", ("id",))
    assert got["plain"] == ("unknown", "source_without_grain")


def test_one_name_multiline_lists_comments_and_trailing_commas_are_read(tmp_path):
    got = sqlx_grains(tmp_path, {
        "one_name.sqlx": 'config { type: "table", assertions: { uniqueKey: "id" } }\nSELECT id, day FROM ${ref("events")}\n',
        "multiline.sqlx": (
            'config {\n  type: "table",\n  assertions: {\n    uniqueKey: [\n      "id",  // the event "id" ]\n'
            '      /* and ] the day */ "day",\n    ],\n  },\n}\nSELECT id, day, n FROM ${ref("events")}\n'),
        "keys.sqlx": (
            'config {\n  type: "table",\n  assertions: {\n    uniqueKeys: [\n      ["id", "day"],\n      // also\n'
            '      ["n"],\n    ],\n  },\n}\nSELECT id, day, n FROM ${ref("events")}\n'),
    })
    assert got["one_name"] == ("declared", ("id",))
    assert got["multiline"] == ("declared", ("day", "id"))
    assert got["keys"] == ("declared", ("n",))  # the shortest key stands


def test_a_key_with_a_computed_entry_is_no_key(tmp_path):
    got = sqlx_grains(tmp_path, {
        "partly.sqlx": 'config { type: "table", assertions: { uniqueKey: ["id", constants.DAY] } }\nSELECT id, day FROM ${ref("events")}\n',
        "computed.sqlx": 'config { type: "table", assertions: { uniqueKey: constants.KEYS } }\nSELECT id, day FROM ${ref("events")}\n',
    })
    # ["id", x] says nothing about id alone, so it is never shortened to its literal entries
    assert got["partly"] == ("unknown", "source_without_grain")
    assert got["computed"] == ("unknown", "source_without_grain")


def test_the_merge_key_of_an_incremental_table_is_not_a_declared_grain(tmp_path):
    pl = project(tmp_path, {
        "events.sqlx": EVENTS,
        "merged.sqlx": 'config { type: "incremental", uniqueKey: ["id"] }\nSELECT id, day FROM ${ref("events")}\n',
        "both.sqlx": 'config { type: "incremental", uniqueKey: ["id"], assertions: { uniqueKey: ["id", "day"] } }\nSELECT id, day FROM ${ref("events")}\n',
    })
    assert pl.models["p.ds.merged"].unique_keys == ()  # Dataform merges on it but never checks it
    got = grains(pl)
    assert got["merged"] == ("unknown", "source_without_grain")
    assert got["both"] == ("declared", ("day", "id"))


def test_a_query_grain_wins_and_a_key_the_model_does_not_output_is_not_used(tmp_path):
    got = sqlx_grains(tmp_path, {
        "grouped.sqlx": 'config { type: "table", assertions: { uniqueKey: ["day", "n"] } }\nSELECT day, COUNT(*) AS n FROM ${ref("events")} GROUP BY day\n',
        "missing.sqlx": 'config { type: "table", assertions: { uniqueKey: ["id"] } }\nSELECT day FROM ${ref("events")}\n',
        "reader.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("keyed")}\n',
        "keyed.sqlx": 'config { type: "table", assertions: { uniqueKey: ["ID"] } }\nSELECT id, day FROM ${ref("events")}\n',
    })
    assert got["grouped"] == ("derived", ("day",))
    assert got["missing"] == ("unknown", "source_without_grain")
    assert got["keyed"] == ("declared", ("id",))  # the output's spelling
    assert got["reader"] == ("derived", ("id",))  # and the declared grain flows on


def test_a_key_under_an_unexpanded_star_is_declared_unless_except_drops_it(tmp_path):
    got = sqlx_grains(tmp_path, {
        "star.sqlx": 'config { type: "table", assertions: { uniqueKey: ["Event_Id"] } }\nSELECT * FROM ${ref("events")}\n',
        "star_and_named.sqlx": 'config { type: "table", assertions: { uniqueKey: ["event_id", "EXTRA"] } }\nSELECT *, 1 AS extra FROM ${ref("events")}\n',
        "except.sqlx": 'config { type: "table", assertions: { uniqueKey: ["event_id"] } }\nSELECT * EXCEPT (event_id) FROM ${ref("events")}\n',
        "except_other.sqlx": 'config { type: "table", assertions: { uniqueKey: ["event_id"] } }\nSELECT * EXCEPT (payload) FROM ${ref("events")}\n',
        "union.sqlx": 'config { type: "table", assertions: { uniqueKey: ["event_id"] } }\nSELECT * FROM ${ref("events")}\nUNION ALL\nSELECT * FROM ${ref("events")}\n',
    })
    assert got["star"] == ("declared", ("Event_Id",))  # the casing the project wrote
    assert got["star_and_named"] == ("declared", ("event_id", "extra"))
    assert got["except"][0] == "unknown"
    assert got["except_other"] == ("declared", ("event_id",))
    assert got["union"] == ("declared", ("event_id",))


def test_a_key_fills_the_grain_of_a_fan_out_join_and_a_union_all(tmp_path):
    got = sqlx_grains(tmp_path, {
        "items.sqlx": 'config { type: "declaration", schema: "raw", name: "items" }\n',
        "joined.sqlx": (
            'config { type: "table", assertions: { uniqueKey: ["id", "sku"] } }\n'
            'SELECT e.*, i.sku FROM ${ref("events")} AS e JOIN ${ref("items")} AS i ON e.id = i.event_id\n'),
        "stacked.sqlx": (
            'config { type: "table", assertions: { uniqueKey: ["id", "origin"] } }\n'
            'SELECT id, "a" AS origin FROM ${ref("events")} UNION ALL SELECT id, "b" AS origin FROM ${ref("items")}\n'),
    })
    assert got["joined"] == ("declared", ("id", "sku"))
    assert got["stacked"] == ("declared", ("id", "origin"))


def test_a_model_that_cannot_be_parsed_keeps_its_declared_key():
    target = Target("p", "ds", "broken")
    model = Model(target, "table", "SELECT FROM WHERE (((", unique_keys=(("id",),))
    got = profile_pipeline(Pipeline({target.key: model}))[target.key].grain
    assert (got.status, got.keys) == ("declared", ("id",))
    operation = Model(target, "operations", "CALL p.ds.proc()", unique_keys=(("id",),))
    assert not profile_pipeline(Pipeline({target.key: operation}))[target.key].grain.known  # not a query


# ------------------------------------------------------------------ declarations


def test_a_declared_source_key_flows_into_the_models_that_read_it(tmp_path):
    pl = project(tmp_path, {
        "events.sqlx": 'config {\n  type: "declaration",\n  schema: "raw",\n  name: "events",\n  assertions: {\n    uniqueKey: ["id"], // checked upstream\n  },\n}\n',
        "keeps.sqlx": 'config { type: "view" }\nSELECT id, day FROM ${ref("events")}\n',
        "star.sqlx": 'config { type: "view" }\nSELECT * FROM ${ref("events")}\n',
        "drops.sqlx": 'config { type: "view" }\nSELECT day FROM ${ref("events")}\n',
    })
    assert pl.source_unique_keys == {"p.raw.events": (("id",),)}
    got = grains(pl)
    assert got["keeps"] == ("derived", ("id",))
    assert got["star"] == ("derived", ("id",))
    assert got["drops"] == ("unknown", "grain_not_in_output")
    query = profile_query(pl, "SELECT id FROM p.raw.events").grain
    assert (query.status, query.keys) == ("derived", ("id",))


def test_the_caller_and_the_source_columns_still_decide_over_a_declared_source_key():
    events = Target("p", "raw", "events")
    model = Model(Target("p", "ds", "v"), "view", "SELECT id, day FROM p.raw.events")
    pipeline = Pipeline({"p.ds.v": model}, sources={events.key: events}, source_unique_keys={events.key: (("id",), ("id", "day"))})
    assert profile_pipeline(pipeline)["p.ds.v"].grain.keys == ("id",)
    overridden = profile_pipeline(pipeline, declared_grain={events.key: ["day"]})["p.ds.v"].grain
    assert overridden.keys == ("day",)
    without_column = Pipeline({"p.ds.v": model}, sources={events.key: events}, source_schema={events.key: {"day": "DATE"}},
                              source_unique_keys={events.key: (("id",),)})
    assert profile_pipeline(without_column)["p.ds.v"].grain.reason == "source_without_grain"  # the table has no column id


def test_js_declare_and_publish_state_keys(tmp_path):
    pl = project(tmp_path, {
        "defs.js": (
            'declare({ schema: "raw", name: "users", uniqueKey: ["user_id"] });\n'
            'publish("by_user", {\n  type: "table",\n  assertions: {\n    uniqueKey: [\n      "user_id",\n    ],\n  },\n})'
            '.query(ctx => `SELECT * FROM ${ctx.ref("users")} WHERE active`);\n'
            'publish("keyed", { type: "table", assertions: { uniqueKeys: [["user_id", "day"]] } })'
            '.query(ctx => `SELECT user_id, day, n FROM ${ctx.ref("other")}`);\n'),
        "other.sqlx": 'config { type: "table" }\nSELECT 1 AS user_id, CURRENT_DATE() AS day, 2 AS n\nUNION ALL\nSELECT 1, CURRENT_DATE(), 3\n',
    })
    assert pl.source_unique_keys == {"p.raw.users": (("user_id",),)}
    assert pl.models["p.ds.by_user"].unique_keys == (("user_id",),)
    got = grains(pl)
    assert got["by_user"] == ("derived", ("user_id",))  # the query carries the declaration's key; it wins
    assert got["keyed"] == ("declared", ("day", "user_id"))


# ------------------------------------------------------------------ compiled graphs


def _target(schema, name):
    return {"database": "p", "schema": schema, "name": name}


def _generated(table, columns, index=0):
    """The assertion entry Dataform 3.0.71 compiles from ``assertions: { uniqueKey: [...] }`` on ``ds.<table>``."""

    listed = ", ".join(columns)
    return {
        "query": (f"\nSELECT\n  *\nFROM (\n  SELECT\n    {listed},\n    COUNT(1) AS index_row_count\n  FROM `p.ds.{table}`\n"
                  f"  GROUP BY {listed}\n  ) AS data\nWHERE index_row_count > 1\n"),
        "fileName": f"definitions/{table}.sqlx",
        "target": _target("ds_assert", f"ds_{table}_assertions_uniqueKey_{index}"),
        "dependencyTargets": [_target("ds", table)],
        "canonicalTarget": _target("ds_assert", f"ds_{table}_assertions_uniqueKey_{index}"),
        "parentAction": _target("ds", table),
    }


def _table(name, query, **extra):
    return {"type": "table", "target": _target("ds", name), "query": query, "fileName": f"definitions/{name}.sqlx",
            "dependencyTargets": [_target("raw", "events")], **extra}


def test_a_compiled_graph_reads_the_assertions_dataform_generates(tmp_path):
    graph = {
        "tables": [
            _table("t", "SELECT * FROM `p.raw.events`"),
            _table("v", "SELECT id, day, n FROM `p.raw.events`", type="view"),
            {"type": "incremental", "target": _target("ds", "inc"), "query": "SELECT id, day FROM `p.raw.events`",
             "uniqueKey": ["id"], "dependencyTargets": [_target("raw", "events")]},
            _table("filtered", "SELECT id, day FROM `p.raw.events`"),
        ],
        "assertions": [
            _generated("t", ["id"]),
            _generated("v", ["id", "day"]), _generated("v", ["n"], 1),
            {**_generated("filtered", ["id"]), "query": _generated("filtered", ["id"])["query"].replace("GROUP BY", "WHERE day > '2020-01-01' GROUP BY")},
        ],
        "declarations": [{"target": _target("raw", "events"), "fileName": "definitions/events.sqlx"}],
    }
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph))
    pl = load_compiled_graph(path)
    assert pl.models["p.ds.t"].unique_keys == (("id",),)
    assert pl.models["p.ds.v"].unique_keys == (("id", "day"), ("n",))
    assert pl.models["p.ds.inc"].unique_keys == ()  # the merge key, not an assertion
    assert pl.models["p.ds.filtered"].unique_keys == ()  # a filtered check is about some rows only
    got = grains(pl)
    assert got["t"] == ("declared", ("id",))
    assert got["v"] == ("declared", ("n",))
    assert got["inc"] == ("unknown", "source_without_grain")


def test_a_compiled_assertions_block_and_declaration_keys_are_read():
    graph = {
        "tables": [
            _table("listed", "SELECT id, day FROM `p.raw.events`", assertions={"uniqueKey": ["id"]}),
            _table("one_name", "SELECT id, day FROM `p.raw.events`", assertions={"uniqueKey": "id"}),
            _table("entries", "SELECT id, day FROM `p.raw.events`", assertions={"uniqueKeys": [{"uniqueKey": ["id", "day"]}, ["day"]]}),
            _table("mixed", "SELECT id, day FROM `p.raw.events`", assertions={"uniqueKey": ["id", 3]}),
            _table("reader", "SELECT id, day FROM `p.raw.users`"),
        ],
        "declarations": [
            {"target": _target("raw", "events")},
            {"target": _target("raw", "users"), "assertions": {"uniqueKey": ["id"]}},
        ],
    }
    pl = load_compiled_graph(graph)
    assert pl.source_unique_keys == {"p.raw.users": (("id",),)}
    got = grains(pl)
    assert got["listed"] == ("declared", ("id",))
    assert got["one_name"] == ("declared", ("id",))
    assert got["entries"] == ("declared", ("day",))
    assert got["mixed"] == ("unknown", "source_without_grain")  # not shortened to ("id",)
    assert got["reader"] == ("derived", ("id",))


def test_only_the_exact_generated_check_of_the_parent_is_a_key():
    parent = Target("p", "ds", "t")
    generated = _generated("t", ["a", "b"])["query"]
    assert _generated_unique_key(parent, generated) == ("p.ds.t", ("a", "b"))
    assert _generated_unique_key(Target("p", "ds", "other"), generated) is None
    for changed in (
        generated.replace("COUNT(1)", "COUNT(DISTINCT a)"),
        generated.replace("> 1", "> 2"),
        generated.replace("GROUP BY a, b", "GROUP BY a"),
        generated.replace("    a, b,", "    a, x.b,").replace("GROUP BY a, b", "GROUP BY a, x.b"),
        generated.replace("AS data", "AS data JOIN `p.ds.u` AS u ON TRUE"),
        "SELECT a FROM `p.ds.t` GROUP BY a HAVING COUNT(*) > 1",
        "not sql at all (((",
    ):
        assert _generated_unique_key(parent, changed) is None, changed


# ------------------------------------------------------------------ the readers and the caches


def test_the_config_readers_read_javascript_as_javascript():
    assert _config_assertions('config { description: "assertions: { uniqueKey: [\\"x\\"] }" }') == ((), ())
    assert _config_assertions('config { columns: { a: { assertions: { uniqueKey: ["z"] } } } }') == ((), ())
    assert _config_assertions('config { assertions: { uniqueKey: cond ? ["a"] : ["b"] } }') == ((), ())
    assert _config_assertions('config { assertions: { uniqueKey: [] } }') == ((), ())
    assert _config_assertions('config { "assertions": { "uniqueKey": [`id`] } }') == ((), (("id",),))
    assert _config_assertions('config { assertions: { nonNull: ["a", x, "b"], uniqueKey: ["a", x] } }') == (("a", "b"), ())
    # a spread may set any key: what is written before it is not known, what is written after it is
    assert _config_assertions('config { assertions: { uniqueKey: ["id"], ...shared } }') == ((), ())
    assert _config_assertions('config { assertions: { ...shared, uniqueKey: ["id"] } }') == ((), (("id",),))
    assert _config_assertions('config { assertions: { uniqueKey: ["id"] }, ...base }') == ((), ())
    assert declared_unique_keys('{ name: "u", uniqueKey: ["id"], ...rest }') == ()
    assert declared_unique_keys('{ schema: "raw", name: "u", uniqueKey: ["id"], description: "uniqueKey: [\'q\']" }') == (("id",),)
    assert declared_unique_keys('config { type: "declaration", assertions: { uniqueKey: ["id"] }, uniqueKeys: [["a"],] }') == (("id",), ("a",))


def test_a_changed_key_changes_what_a_saved_profile_is_kept_under():
    events = Target("p", "raw", "events")
    model = Model(Target("p", "ds", "v"), "view", "SELECT id FROM p.raw.events")
    one = Pipeline({"p.ds.v": model}, sources={events.key: events}, source_unique_keys={events.key: (("id",),)})
    other = Pipeline({"p.ds.v": model}, sources={events.key: events}, source_unique_keys={events.key: (("id", "day"),)})
    none = Pipeline({"p.ds.v": model}, sources={events.key: events})
    assert len({_grain_context(one, None), _grain_context(other, None), _grain_context(none, None)}) == 3
    keyed = Model(model.target, model.kind, model.sql, unique_keys=(("id",),))
    assert definition_digest(keyed) != definition_digest(model)  # a model's own key is part of its closure key
