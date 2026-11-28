"""``trace_column`` shows the logic of every hop: the expression, the grain and the row conditions (docs/pipeline-analysis.md)."""

import json

import pytest

from kumosql.pipeline import Pipeline, _merge_records
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.pipeline_types import ColumnFilter, ColumnLineage, ColumnRef, ColumnTrace, LogicStep, Model, Target, TraceHop
from kumosql.trace_memo import FORMAT, MemoError, decode_delta, encode_delta

SCHEMA = {
    "p.d.raw_events": {
        "order_id": "INT64", "text": "STRING", "status": "STRING", "ts": "TIMESTAMP", "rec": "STRUCT<a INT64, b STRING>",
    },
    "p.d.orders": {"id": "INT64", "cust": "INT64", "ts": "TIMESTAMP"},
}

FLAG = "MAX(CASE WHEN UPPER(text) LIKE '%X%' THEN 1 END)"
FLAG_MODEL = f"SELECT order_id, {FLAG} AS has_x FROM p.d.raw_events WHERE status = 'ok' GROUP BY order_id"


def pipeline(**models: str) -> Pipeline:
    built = {name: Model(Target(name=name), "table", sql) for name, sql in models.items()}
    return Pipeline(built, {key: Target(*key.split(".")) for key in SCHEMA}, {k: dict(v) for k, v in SCHEMA.items()})


def record(sql: str, column: str, **more: str) -> ColumnLineage:
    return pipeline(m=sql, **more).explain_lineage()[ColumnRef("m", column)]


def hop(sql: str, column: str, **more: str) -> TraceHop:
    return pipeline(m=sql, **more).trace_column(ColumnRef("m", column)).hops[0]


# ------------------------------------------------------------ the motivating flag


def test_a_flag_shows_its_expression_its_grain_and_its_where():
    found = hop(FLAG_MODEL, "has_x")
    assert found.expression == FLAG
    assert found.aggregation_keys == ("order_id",)
    assert found.transform == "aggregate" and found.status == "traced"
    assert found.sources == (ColumnRef("p.d.raw_events", "text"),)
    assert [(f.kind, f.condition) for f in found.filters] == [("where", "raw_events.status = 'ok'")]


def test_a_flag_computed_in_a_cte_shows_the_cte_expression_and_grain():
    sql = f"WITH agg AS ({FLAG_MODEL}) SELECT order_id, has_x FROM agg"
    found = hop(sql, "has_x")
    assert found.expression == FLAG
    assert found.aggregation_keys == ("order_id",)
    assert found.steps == ()
    assert [(f.kind, f.scope) for f in found.filters] == [("where", "agg")]


def test_a_chain_of_ctes_is_inlined_into_one_formula():
    sql = """
        WITH a1 AS (SELECT order_id, text FROM p.d.raw_events WHERE status = 'ok'),
             a2 AS (SELECT order_id, UPPER(text) AS t FROM a1),
             a3 AS (SELECT order_id, MAX(CASE WHEN t LIKE '%X%' THEN 1 END) AS f FROM a2 GROUP BY order_id)
        SELECT order_id, IFNULL(f, 0) AS flag FROM a3
    """
    found = record(sql, "flag")
    assert found.expression == f"IFNULL({FLAG}, 0)"
    assert found.aggregation_keys == ("order_id",)


def test_one_call_answers_how_a_flag_is_computed_and_at_what_grain_across_models():
    models = pipeline(
        a=FLAG_MODEL,
        b="""SELECT a.order_id, a.has_x FROM a JOIN p.d.orders AS o ON a.order_id = o.id
             QUALIFY ROW_NUMBER() OVER (PARTITION BY a.order_id ORDER BY o.ts DESC) = 1""",
        c="SELECT has_x, COUNT(*) AS orders FROM b GROUP BY 1 HAVING COUNT(*) > 1",
    )
    trace = models.trace_column(ColumnRef("c", "has_x"))
    assert [str(step.column) for step in trace.hops] == ["c.has_x", "b.has_x", "a.has_x"]
    c, b, a = trace.hops

    assert (c.expression, c.aggregation_keys) == ("has_x", ("has_x",))
    assert [(f.kind, f.condition) for f in c.filters] == [("having", "COUNT(*) > 1")]

    assert (b.expression, b.aggregation_keys, b.transform) == ("has_x", None, "passthrough")
    assert [(f.kind, f.condition) for f in b.filters] == [
        ("join", "a.order_id = o.id"),
        ("qualify", "ROW_NUMBER() OVER (PARTITION BY a.order_id ORDER BY o.ts DESC) = 1"),
    ]

    assert (a.expression, a.aggregation_keys) == (FLAG, ("order_id",))
    assert a.sources == (ColumnRef("p.d.raw_events", "text"),)
    assert [f.kind for f in a.filters] == ["where"]
    assert trace.complete and trace.sources == {ColumnRef("p.d.raw_events", "text")}

    row = json.loads(json.dumps(trace.to_json()))
    assert [h["column"] for h in row["hops"]] == ["c.has_x", "b.has_x", "a.has_x"]
    assert row["hops"][2]["expression"] == FLAG and row["hops"][2]["aggregation_keys"] == ["order_id"]
    assert row["hops"][1]["qualify"][0]["condition"].startswith("ROW_NUMBER()")
    assert row["hops"][1]["join_on"][0]["condition"] == "a.order_id = o.id"
    assert row["hops"][0]["having"][0]["condition"] == "COUNT(*) > 1"


# ------------------------------------------------------------ grain


@pytest.mark.parametrize(
    "sql, keys",
    [
        ("SELECT order_id, DATE(ts) AS day, COUNT(*) AS n FROM p.d.raw_events GROUP BY 1, 2", ("order_id", "day")),
        ("SELECT order_id, DATE(ts) AS day, COUNT(*) AS n FROM p.d.raw_events GROUP BY order_id, day", ("order_id", "day")),
        ("SELECT order_id, DATE(ts) AS day, COUNT(*) AS n FROM p.d.raw_events GROUP BY ALL", ("order_id", "day")),
        ("SELECT order_id AS k, COUNT(*) AS n FROM p.d.raw_events GROUP BY order_id", ("k",)),
        ("SELECT COUNT(*) AS n FROM p.d.raw_events GROUP BY status", ("status",)),
        ("SELECT COUNT(*) AS n FROM p.d.raw_events", ()),
        ("SELECT COUNT(*) AS n FROM p.d.raw_events HAVING COUNT(*) > 1", ()),
        ("SELECT order_id, status, COUNT(*) AS n FROM p.d.raw_events GROUP BY ROLLUP(order_id, status)", ("ROLLUP (order_id, status)",)),
    ],
)
def test_aggregation_keys_name_the_grouping_the_way_the_output_does(sql, keys):
    assert record(sql, "n").aggregation_keys == keys


def test_bigquery_sketch_aggregates_count_as_aggregates():
    assert record("SELECT HLL_COUNT.INIT(order_id) AS n FROM p.d.raw_events", "n").aggregation_keys == ()
    assert record("SELECT HLL_COUNT.EXTRACT(rec) AS n FROM p.d.raw_events", "n").aggregation_keys is None


def test_a_column_of_an_aggregating_query_carries_its_keys_and_a_plain_select_carries_none():
    assert record("SELECT order_id, COUNT(*) AS n FROM p.d.raw_events GROUP BY order_id", "order_id").aggregation_keys == ("order_id",)
    assert record("SELECT order_id, UPPER(text) AS t FROM p.d.raw_events WHERE status = 'ok'", "t").aggregation_keys is None


def test_keys_from_two_joined_tables_stay_apart():
    sql = """
        SELECT o.id, e.order_id AS id2, COUNT(*) AS n FROM p.d.orders AS o JOIN p.d.raw_events AS e ON o.id = e.order_id
        GROUP BY o.id, e.order_id, e.status
    """
    assert record(sql, "n").aggregation_keys == ("id", "id2", "e.status")


def test_an_aggregate_over_an_aggregate_lists_the_inner_one_as_a_step():
    sql = """
        WITH daily AS (SELECT order_id, DATE(ts) AS day, MAX(LENGTH(text)) AS m FROM p.d.raw_events GROUP BY 1, 2)
        SELECT order_id, SUM(m) AS total FROM daily GROUP BY ALL
    """
    found = record(sql, "total")
    assert found.expression == "SUM(daily.m)"
    assert found.aggregation_keys == ("order_id",)
    assert found.steps == (LogicStep("daily.m", "daily", "MAX(LENGTH(text))", ("order_id", "day")),)


def test_a_grouped_value_mixed_with_a_row_value_is_named_not_inlined():
    agg = "WITH agg AS (SELECT order_id, COUNT(*) AS n FROM p.d.raw_events GROUP BY order_id) "
    mixed = record(agg + "SELECT o.id, COALESCE(agg.n, 0) + o.cust AS v FROM p.d.orders AS o LEFT JOIN agg ON agg.order_id = o.id", "v")
    assert mixed.expression == "COALESCE(agg.n, 0) + o.cust"
    assert mixed.aggregation_keys is None
    assert mixed.steps == (LogicStep("agg.n", "agg", "COUNT(*)", ("order_id",)),)

    alone = record(agg + "SELECT o.id, COALESCE(agg.n, 0) AS v FROM p.d.orders AS o LEFT JOIN agg ON agg.order_id = o.id", "v")
    assert alone.expression == "COALESCE(COUNT(*), 0)"
    assert alone.aggregation_keys == ("order_id",)


def test_deduplicated_rows_are_not_hidden_by_inlining_into_an_aggregate():
    sql = "WITH d AS (SELECT DISTINCT order_id FROM p.d.raw_events) SELECT COUNT(order_id) AS n FROM d"
    found = record(sql, "n")
    assert found.expression == "COUNT(d.order_id)"
    assert found.steps == (LogicStep("d.order_id", "d", "order_id", None),)
    filtered = record("WITH f AS (SELECT order_id FROM p.d.raw_events WHERE status = 'ok') SELECT COUNT(order_id) AS n FROM f", "n")
    assert filtered.expression == "COUNT(order_id)"


# ------------------------------------------------------------ windows, set operations, plain columns


def test_a_window_shows_its_partition():
    found = record("SELECT order_id, SUM(LENGTH(text)) OVER (PARTITION BY order_id ORDER BY ts) AS running FROM p.d.raw_events", "running")
    assert found.transform == "window"
    assert found.expression == "SUM(LENGTH(text)) OVER (PARTITION BY order_id ORDER BY ts)"
    assert found.partition_keys == ("order_id",)
    assert found.aggregation_keys is None
    assert record("SELECT COUNT(*) OVER () AS total FROM p.d.raw_events", "total").partition_keys == ()


def test_a_named_window_does_not_guess_its_partition():
    found = record("SELECT SUM(order_id) OVER w AS s FROM p.d.raw_events WINDOW w AS (PARTITION BY status)", "s")
    assert found.expression == "SUM(order_id) OVER w"
    assert found.partition_keys is None


def test_a_window_value_is_not_inlined_into_an_aggregate():
    sql = """
        WITH w AS (SELECT order_id, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY ts) AS rn FROM p.d.raw_events)
        SELECT order_id, MAX(rn) AS m FROM w GROUP BY order_id
    """
    found = record(sql, "m")
    assert found.expression == "MAX(w.rn)"
    assert found.steps == (LogicStep("w.rn", "w", "ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY ts)", None),)


def test_union_branches_that_differ_are_steps_and_identical_ones_one_expression():
    differ = record("SELECT order_id AS id, UPPER(text) AS v FROM p.d.raw_events UNION ALL SELECT id, CAST(cust AS STRING) FROM p.d.orders", "v")
    assert differ.transform == "union" and differ.expression is None
    assert differ.steps == (
        LogicStep("v", "main branch 1", "UPPER(text)", None),
        LogicStep("v", "main branch 2", "CAST(cust AS STRING)", None),
    )
    same = record("SELECT order_id AS v FROM p.d.raw_events UNION ALL SELECT order_id FROM p.d.raw_events WHERE status = 'x'", "v")
    assert same.expression == "order_id" and same.steps == ()


def test_a_union_in_a_cte_is_named_with_its_branches():
    sql = "WITH u AS (SELECT order_id AS k FROM p.d.raw_events UNION ALL SELECT id FROM p.d.orders) SELECT k FROM u"
    found = record(sql, "k")
    assert found.expression == "u.k"
    assert [(s.reference, s.scope, s.expression) for s in found.steps] == [("u.k", "u branch 1", "order_id"), ("u.k", "u branch 2", "id")]


def test_passthrough_rename_and_constant_columns():
    sql = "SELECT order_id, text AS body, 1 AS one, rec.a AS a FROM p.d.raw_events"
    assert (record(sql, "order_id").expression, record(sql, "order_id").transform) == ("order_id", "passthrough")
    assert (record(sql, "body").expression, record(sql, "body").transform) == ("text", "renamed")
    assert (record(sql, "one").expression, record(sql, "one").status) == ("1", "constant")
    assert record(sql, "a").expression == "rec.a"


def test_columns_of_two_tables_keep_their_qualifiers():
    sql = "SELECT o.id + e.order_id AS s FROM p.d.orders AS o JOIN p.d.raw_events AS e ON o.id = e.order_id"
    assert record(sql, "s").expression == "o.id + e.order_id"


def test_one_alias_for_two_tables_in_two_ctes_is_named_by_table():
    sql = """
        WITH x AS (SELECT r.order_id AS k FROM p.d.raw_events AS r), y AS (SELECT r.id AS k FROM p.d.orders AS r)
        SELECT x.k + y.k AS s FROM x JOIN y ON x.k = y.k
    """
    assert record(sql, "s").expression == "raw_events.order_id + orders.id"


def test_an_inlined_sum_is_parenthesised():
    assert record("WITH c AS (SELECT order_id + 1 AS x FROM p.d.raw_events) SELECT x * 2 AS y FROM c", "y").expression == "(order_id + 1) * 2"


def test_a_scalar_subquery_is_shown_without_the_qualifier_names():
    found = record("SELECT (SELECT MAX(o.ts) FROM p.d.orders AS o WHERE o.id = e.order_id) AS last FROM p.d.raw_events AS e", "last")
    assert found.expression == "(SELECT MAX(o.ts) FROM p.d.orders AS o WHERE o.id = e.order_id)"


def test_a_recursive_cte_is_not_inlined():
    found = record("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r", "n")
    assert found.expression == "r.n" and found.steps == ()


def test_a_name_selected_twice_has_no_expression():
    found = record("SELECT o.id, e.order_id AS id FROM p.d.orders AS o JOIN p.d.raw_events AS e ON o.id = e.order_id", "id")
    assert found.expression is None


# ------------------------------------------------------------ hops that cannot be followed


def test_an_unparsed_model_is_an_unknown_hop_with_its_reason():
    models = pipeline(a="SELECT FROM WHERE (", b="SELECT x FROM a")
    trace = models.trace_column(ColumnRef("b", "x"))
    assert [(str(h.column), h.status) for h in trace.hops] == [("b.x", "traced"), ("a.x", "unknown")]
    unknown = trace.hops[1]
    assert unknown.reason == "unparsed_model" and unknown.filters_unknown and unknown.expression is None
    assert not trace.complete


def test_a_struct_field_hop_takes_its_root_column_logic():
    trace = pipeline(m="SELECT rec FROM p.d.raw_events").trace_column(ColumnRef("m", "rec", ("a",)))
    assert trace.hops[0].column == ColumnRef("m", "rec", ("a",))
    assert trace.hops[0].expression == "rec"
    assert trace.hops[0].sources == (ColumnRef("p.d.raw_events", "rec", ("a",)),)


def test_a_dataform_expression_is_shown_as_written(tmp_path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "dataform.json").write_text('{"defaultDatabase": "p", "defaultSchema": "d"}', encoding="utf-8")
    (tmp_path / "definitions" / "src.sqlx").write_text(
        'config { type: "declaration", database: "p", schema: "d", name: "raw_events" }', encoding="utf-8"
    )
    (tmp_path / "definitions" / "m.sqlx").write_text(
        'config { type: "table" }\n'
        "SELECT order_id, MAX(CASE WHEN text LIKE ${dataform.projectConfig.vars.pattern} THEN 1 END) AS f "
        'FROM ${ref("raw_events")} GROUP BY 1',
        encoding="utf-8",
    )
    found = load_sqlx_project(tmp_path).explain_lineage()[ColumnRef("p.d.m", "f")]
    assert found.expression == "MAX(CASE WHEN text LIKE ${dataform.projectConfig.vars.pattern} THEN 1 END)"
    assert found.aggregation_keys == ("order_id",)


# ------------------------------------------------------------ shapes


def test_trace_hop_json_groups_conditions_by_kind():
    where = ColumnFilter("where", "status = 'ok'", "main", (ColumnRef("t", "status"),))
    join = ColumnFilter("join", "a.id = b.id", "main", (), "matches_only")
    found = TraceHop(
        ColumnRef("m", "f"), "m", "aggregate", "traced", (ColumnRef("t", "x"),), "MAX(x)", (where, join),
        aggregation_keys=("k",), steps=(LogicStep("c.y", "c", "y + 1"),), job_id="job_1", step=2,
    )
    assert found.aggregated
    assert found.to_json() == {
        "column": "m.f",
        "model": "m",
        "transform": "aggregate",
        "status": "traced",
        "expression": "MAX(x)",
        "aggregation_keys": ["k"],
        "sources": [{"node": "t", "column": "x"}],
        "steps": [{"reference": "c.y", "scope": "c", "expression": "y + 1", "aggregation_keys": None}],
        "where": [{"condition": "status = 'ok'", "scope": "main", "effect": "limits_rows", "columns": [{"node": "t", "column": "status"}]}],
        "join_on": [{"condition": "a.id = b.id", "scope": "main", "effect": "matches_only", "columns": []}],
        "job_id": "job_1",
        "step": 2,
    }
    plain = TraceHop(ColumnRef("m", "x"), "m", "unknown", "unknown", filters_unknown=True, reason="unparsed_model").to_json()
    assert plain["aggregation_keys"] is None and plain["filters_unknown"] is True and plain["reason"] == "unparsed_model"
    assert "job_id" not in plain and "step" not in plain and "where" not in plain


def test_old_callers_build_lineage_and_traces_without_the_new_fields():
    ref = ColumnRef("m", "x")
    old = ColumnLineage(ref, frozenset(), "constant", "constant")
    assert (old.expression, old.aggregation_keys, old.partition_keys, old.steps, old.filters) == (None, None, None, (), None)
    assert ColumnTrace(ref, frozenset(), frozenset(), ()).hops == ()


def test_lineage_report_rows_carry_the_expression_and_keys():
    rows = {row["column"]: row for row in pipeline(m=FLAG_MODEL + "").lineage_report() if row["node"] == "m"}
    assert rows["has_x"]["expression"] == FLAG and rows["has_x"]["aggregation_keys"] == ["order_id"]
    assert "steps" not in rows["has_x"] and "partition_keys" not in rows["has_x"]
    single = pipeline(m="SELECT MAX(ts) AS last FROM p.d.raw_events").lineage_report()
    assert single[0]["aggregation_keys"] == []
    plain = pipeline(m="SELECT order_id FROM p.d.raw_events").lineage_report()
    assert plain[0]["expression"] == "order_id" and "aggregation_keys" not in plain[0]


def test_two_writers_keep_the_logic_only_when_they_agree():
    ref = ColumnRef("m", "x")
    one = ColumnLineage(ref, frozenset(), "traced", "aggregate", None, (), "MAX(a)", ("k",))
    same = ColumnLineage(ref, frozenset(), "traced", "aggregate", None, (), "MAX(a)", ("k",))
    other = ColumnLineage(ref, frozenset(), "traced", "aggregate", None, (), "MIN(a)", ("k",))
    assert (_merge_records(one, same).expression, _merge_records(one, same).aggregation_keys) == ("MAX(a)", ("k",))
    assert (_merge_records(one, other).expression, _merge_records(one, other).aggregation_keys) == (None, None)


# ------------------------------------------------------------ saved traces


def _delta(records):
    return {"records": records, "direct": [], "outputs": None, "schema": None, "consumed": None, "conditions": None,
            "star": None, "template": None, "opaque": set(), "diagnostics": []}


def test_saved_traces_keep_the_logic():
    assert FORMAT >= 2
    records = list(pipeline(
        m="WITH daily AS (SELECT order_id, MAX(ts) AS m FROM p.d.raw_events GROUP BY 1) SELECT SUM(UNIX_SECONDS(m)) AS s, "
          "SUM(order_id) OVER (PARTITION BY order_id) AS w FROM daily GROUP BY order_id"
    ).explain_lineage().values())
    assert any(r.steps for r in records) and any(r.partition_keys for r in records)
    saved = json.loads(json.dumps(encode_delta(_delta(records))))
    assert decode_delta(saved)["records"] == records


def test_a_trace_saved_in_the_old_format_is_a_miss_not_a_crash():
    record_ = ColumnLineage(ColumnRef("m", "x"), frozenset(), "constant", "constant", None, ())
    saved = json.loads(json.dumps(encode_delta(_delta([record_]))))
    saved["records"][0] = saved["records"][0][:6]  # what FORMAT 1 wrote
    with pytest.raises(MemoError):
        decode_delta(saved)
    saved["records"][0] = [*saved["records"][0], [None, None, None, [["c.y", "c", 5, None]]]]
    with pytest.raises(MemoError):
        decode_delta(saved)
