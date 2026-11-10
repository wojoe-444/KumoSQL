"""Column meanings for a field inside a STRUCT or JSON column, and grain that a project declares.

``field(col:<table>.<column>, a.b.c)`` is equal between two tables only when both read the same path of the same
source column; everything else stays unknown or different.
"""

import re

from kumosql import Pipeline, Target, find_overlaps, find_rollups, profile_pipeline
from kumosql.pipeline import Model
from kumosql.table_profile import COLUMN_MEANING

EVENTS = Target("proj", "raw", "events")
OTHER = Target("proj", "raw", "other_events")
SOURCES = {EVENTS.key: EVENTS, OTHER.key: OTHER}
SCHEMA = {
    EVENTS.key: {"id": "INT64", "day": "DATE", "payload": "STRUCT<a STRUCT<b STRUCT<c STRING, d STRING>>>", "meta": "STRUCT<a STRUCT<b STRUCT<c STRING>>>", "arr": "ARRAY<STRING>"},
    OTHER.key: {"id": "INT64", "day": "DATE", "payload": "STRUCT<a STRUCT<b STRUCT<c STRING, d STRING>>>"},
}
FIELD = "field(col:proj.raw.events.payload, a.b.c)"


def build(models, kinds=None, schema=SCHEMA, unique=None, sources=SOURCES):
    built = {}
    for name, sql in models.items():
        target = Target("proj", "core", name)
        built[target.key] = Model(target, (kinds or {}).get(name, "table"), sql, unique_keys=(unique or {}).get(name, ()))
    return Pipeline(built, sources=dict(sources), source_schema=dict(schema))


def profiles(models, **kw):
    return {key.split(".")[-1]: value for key, value in profile_pipeline(build(models, **kw)).items()}


def meaning(models, model, column):
    attr = profiles(models)[model].attribute(column)
    return attr.meaning if attr.status == "known" else f"unknown:{attr.reason}"


# ---------------------------------------------------------------- equal meanings


def test_every_spelling_of_one_path_has_the_same_meaning():
    p = profiles({
        "plain": "SELECT payload.a.b.c AS v FROM proj.raw.events",
        "aliased": "SELECT e.payload.a.b.c AS x FROM proj.raw.events AS e",
        "cased": "SELECT PAYLOAD.A.B.C AS y FROM proj.raw.events",
        "project_qualified": "SELECT proj.raw.events.payload.a.b.c AS w FROM proj.raw.events",
        "step": "SELECT payload FROM proj.raw.events",
        "through_a_view": "SELECT payload.a.b.c AS q FROM proj.core.step",
        "through_a_cte": "WITH s AS (SELECT payload AS pl FROM proj.raw.events) SELECT pl.a.b.c AS r FROM s",
    })
    found = {(name, a.column): a.meaning for name, prof in p.items() for a in prof.attributes if len(a.column) == 1}
    assert set(found.values()) == {FIELD}, found
    assert len(found) == 6
    assert all(prof.attribute(c).equality_only for prof in p.values() for c in "vxywqr" if prof.attribute(c))


def test_field_names_are_case_sensitive_unless_the_schema_says_struct():
    models = {"t": "SELECT payload.Foo.bar AS lower_case, PAYLOAD.foo.BAR AS other_case, payload.foo.bar AS plain FROM proj.raw.events"}
    schema = {**SCHEMA, EVENTS.key: {**SCHEMA[EVENTS.key], "payload": "JSON"}}
    json_case = profiles(models, schema=schema)["t"]
    assert len({a.meaning for a in json_case.attributes}) == 3  # JSON field names are case-sensitive: three different values
    assert json_case.attribute("plain").meaning == "field(col:proj.raw.events.payload, foo.bar)"
    unknown_type = profiles(models, schema={})["t"]
    assert len({a.meaning for a in unknown_type.attributes}) == 3  # no schema: not folded, so never equal by accident
    struct = profiles(models)["t"]  # the default schema types payload as a STRUCT
    assert {a.meaning for a in struct.attributes} == {"field(col:proj.raw.events.payload, foo.bar)"}


def test_a_field_of_a_field_extends_the_path_and_a_whole_column_stays_a_column():
    p = profiles({
        "whole": "SELECT payload AS x FROM proj.raw.events",
        "narrow": "SELECT payload.a AS x FROM proj.raw.events",
        "view": "SELECT x.b.c AS y FROM proj.core.narrow",
    })
    assert p["whole"].attribute("x").meaning == "col:proj.raw.events.payload"
    assert p["narrow"].attribute("x").meaning == "field(col:proj.raw.events.payload, a)"
    assert p["view"].attribute("y").meaning == FIELD


def test_a_dataset_qualified_column_is_still_the_plain_column():
    p = profiles({"q": "SELECT raw.events.id AS a, proj.raw.events.id AS b, events.id AS c, id AS d FROM proj.raw.events"})["q"]
    assert {a.meaning for a in p.attributes} == {"col:proj.raw.events.id"}


# ------------------------------------------------------------ never equal by accident


def test_different_paths_and_prefixes_never_have_the_same_meaning():
    p = profiles({
        "t": "SELECT payload.a.b.c AS c, payload.a.b.d AS d, payload.a.b AS b, payload.a AS a, payload AS whole FROM proj.raw.events",
    })["t"]
    meanings = [a.meaning for a in p.attributes]
    assert len(set(meanings)) == 5 and None not in meanings


def test_the_same_path_of_another_column_or_table_never_has_the_same_meaning():
    p = profiles({
        "t": "SELECT payload.a.b.c AS mine, meta.a.b.c AS other_column FROM proj.raw.events",
        "u": "SELECT payload.a.b.c AS other_table FROM proj.raw.other_events",
    })
    assert p["t"].attribute("mine").meaning == FIELD
    assert p["t"].attribute("other_column").meaning == "field(col:proj.raw.events.meta, a.b.c)"
    assert p["u"].attribute("other_table").meaning == "field(col:proj.raw.other_events.payload, a.b.c)"
    assert len({p["t"].attribute("mine").meaning, p["t"].attribute("other_column").meaning, p["u"].attribute("other_table").meaning}) == 3


def test_a_path_through_a_join_is_found_only_when_one_table_can_have_the_column():
    both = {"j": "SELECT payload.a.b.c AS v FROM proj.raw.events JOIN proj.raw.other_events USING (id)"}
    assert meaning(both, "j", "v").startswith("unknown:")  # both tables have a column called payload
    one = {"j": "SELECT meta.a.b.c AS v FROM proj.raw.events JOIN proj.raw.other_events USING (id)"}
    assert meaning(one, "j", "v") == "field(col:proj.raw.events.meta, a.b.c)"
    qualified = {"j": "SELECT o.payload.a.b.c AS v FROM proj.raw.events AS e JOIN proj.raw.other_events AS o USING (id)"}
    assert meaning(qualified, "j", "v") == "field(col:proj.raw.other_events.payload, a.b.c)"


def test_a_field_of_a_computed_value_or_an_unnested_element_stays_unknown():
    models = {
        "computed": "SELECT IF(id > 1, payload, payload) AS p FROM proj.raw.events",
        "reader": "SELECT p.a.b.c AS v FROM proj.core.computed",
        "unnested": "SELECT payload.a.b.c AS ok, item.value AS el FROM proj.raw.events, UNNEST(payload.arr) AS item",
    }
    assert meaning(models, "reader", "v") == "unknown:nested_field"
    assert meaning(models, "unnested", "el") == "unknown:unnest_element"
    assert meaning(models, "unnested", "ok") == FIELD  # the unnested alias hides the struct's fields, so payload is events.payload


def test_unknown_stays_unknown_when_a_path_is_not_plain_names():
    models = {"t": "SELECT payload.a.`b-c` AS v FROM proj.raw.events"}
    assert meaning(models, "t", "v") == "unknown:nested_field"


# ------------------------------------------------------------------- comparisons


def test_overlap_matches_tables_that_group_by_the_same_path_and_not_a_different_one():
    pipeline = build({
        "by_c": "SELECT payload.a.b.c AS k, COUNT(*) AS n FROM proj.raw.events GROUP BY payload.a.b.c",
        "by_c_again": "SELECT e.payload.a.b.c AS key, COUNT(*) AS total FROM proj.raw.events AS e GROUP BY 1",
        "by_d": "SELECT payload.a.b.d AS k, COUNT(*) AS n FROM proj.raw.events GROUP BY payload.a.b.d",
        "by_other_column": "SELECT meta.a.b.c AS k, COUNT(*) AS n FROM proj.raw.events GROUP BY meta.a.b.c",
        "by_other_table": "SELECT payload.a.b.c AS k, COUNT(*) AS n FROM proj.raw.other_events GROUP BY payload.a.b.c",
    })
    result = find_overlaps(pipeline, model="proj.core.by_c")
    kinds = {m.table.split(".")[-1]: m.kind for m in result.matches}
    assert kinds["by_c_again"] == "same_meaning"
    assert all(kinds.get(other, "none") in ("none", "unknown") for other in ("by_d", "by_other_column", "by_other_table"))
    assert result.compared == 4


def test_rollups_read_a_field_as_a_raw_value():
    pipeline = build({
        "fine": "SELECT day, payload.a.b.c AS k, COUNT(*) AS n FROM proj.raw.events GROUP BY day, payload.a.b.c",
        "coarse": "SELECT payload.a.b.c AS k, COUNT(*) AS n FROM proj.raw.events GROUP BY payload.a.b.c",
        "wrong": "SELECT payload.a.b.d AS k, COUNT(*) AS n FROM proj.raw.events GROUP BY payload.a.b.d",
    })
    found = {(r.table.split(".")[-1], r.derivability) for r in find_rollups(pipeline, model="proj.core.coarse").rollups}
    assert ("fine", "derivable_exact") in found or ("fine", "derivable_with_conditions") in found
    assert not any(table == "wrong" and kind.startswith("derivable") for table, kind in found)


def test_meanings_are_found_whole_inside_filters_and_aggregates():
    placeholder = "col:[PROJECT_ID].csa.[LOGS_DATASET_ID]._AllLogs.timestamp"
    text = f"expr:{placeholder} > x AND field({placeholder}, a.b) = 1 AND agg:SUM(col:p.d.t.items[0])"
    assert re.findall(COLUMN_MEANING, text) == [placeholder, f"field({placeholder}, a.b)", "col:p.d.t.items"]


# ------------------------------------------------------------ grain the project declares


def test_a_declared_unique_key_gives_a_grain_only_where_the_query_shows_none():
    models = {
        "passthrough": "SELECT id, day FROM proj.raw.events",
        "keyed": "SELECT id, day FROM proj.raw.events",
        "grouped": "SELECT day, COUNT(*) AS n FROM proj.raw.events GROUP BY day",
        "reader": "SELECT id FROM proj.core.keyed",
        "missing_column": "SELECT day FROM proj.raw.events",
    }
    unique = {"keyed": (("ID",),), "grouped": (("day", "n"),), "missing_column": (("id",),)}
    p = profiles(models, unique=unique)
    assert not p["passthrough"].grain.known and p["passthrough"].grain.reason == "source_without_grain"
    assert (p["keyed"].grain.status, p["keyed"].grain.keys) == ("declared", ("id",))
    assert (p["grouped"].grain.status, p["grouped"].grain.keys) == ("derived", ("day",))  # derived wins over declared
    assert (p["reader"].grain.status, p["reader"].grain.keys) == ("derived", ("id",))  # and the declared grain flows on
    assert not p["missing_column"].grain.known  # a key the model does not output is not used


def test_the_shortest_declared_key_stands():
    p = profiles({"t": "SELECT id, day FROM proj.raw.events"}, unique={"t": (("id", "day"), ("day",))})["t"]
    assert p.grain.keys == ("day",)


def test_a_declared_source_grain_flows_through_a_view_that_keeps_the_key():
    models = {"keeps": "SELECT id, day FROM proj.raw.events", "drops": "SELECT day FROM proj.raw.events"}
    pipeline = build(models)
    got = profile_pipeline(pipeline, declared_grain={EVENTS.key: ["id"]})
    assert (got["proj.core.keeps"].grain.status, got["proj.core.keeps"].grain.keys) == ("derived", ("id",))
    assert got["proj.core.drops"].grain.reason == "grain_not_in_output"  # the key is not in the output: still unknown
