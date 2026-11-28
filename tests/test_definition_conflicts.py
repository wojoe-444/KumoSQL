"""One business term, several definitions: the same column name computed differently in different models (or Malloy fields)."""

import json

from kumosql import Pipeline, Target, definition_conflicts as dc, mcp_server
from kumosql.malloy_reader import read_malloy
from kumosql.mcp_session import Session
from kumosql.pipeline import Model

from test_mcp_server import ask, block_network, write_project

BOUND = 2000
ORDERS = Target("proj", "raw", "orders")
SCHEMA = {ORDERS.key: {"order_id": "INT64", "customer_id": "INT64", "amount": "FLOAT64", "net_amount": "FLOAT64",
                       "status": "STRING", "created_at": "TIMESTAMP"}}


def build(models: dict[str, str]) -> Pipeline:
    built = {}
    for name, sql in models.items():
        target = Target("proj", "core", name)
        built[target.key] = Model(target, "table", sql)
    return Pipeline(built, sources={ORDERS.key: ORDERS}, source_schema=dict(SCHEMA))


def report(models: dict[str, str], **kwargs) -> dc.Report:
    return dc.check_project(build(models), equivalences=kwargs.pop("equivalences", ()), **kwargs)


def tables(term: dc.Term, number: int) -> set[str]:
    return {m.table.split(".")[-1] for m in term.definitions[number - 1].members}


# --------------------------------------------------------------- what is reported


def test_a_differing_aggregate_is_a_computed_conflict_that_names_the_two_functions():
    found = report({
        "rev_sum": "SELECT customer_id, SUM(amount) AS revenue FROM proj.raw.orders GROUP BY customer_id",
        "rev_avg": "SELECT customer_id, AVG(amount) AS revenue FROM proj.raw.orders GROUP BY customer_id",
    })
    (term,) = found.conflicts
    assert term.term == "revenue" and term.tier == "computed" and len(term.definitions) == 2
    (difference,) = term.differences
    assert difference["kind"] == "aggregate" and "SUM" in difference["text"] and "AVG" in difference["text"]
    assert {m.grain for e in term.definitions for m in e.members} == {"customer_id"}


def test_definitions_that_differ_only_in_a_filter_say_which_filter():
    found = report({
        "paid": "SELECT customer_id, SUM(amount) AS revenue FROM proj.raw.orders WHERE status = 'paid' GROUP BY customer_id",
        "gross": "SELECT customer_id, SUM(amount) AS revenue FROM proj.raw.orders GROUP BY customer_id",
    })
    (term,) = found.conflicts
    (difference,) = term.differences
    assert difference["kind"] == "filter"
    assert difference["text"].startswith("differ only in WHERE raw.orders.status = 'paid'"), difference["text"]
    paid = next(e for e in term.definitions if tables(term, e.number) == {"paid"})
    assert paid.to_json()["filters"] == ["raw.orders.status = 'paid'"]


def test_a_filter_inside_the_aggregate_and_a_differing_constant_are_named():
    found = report({
        "plain": "SELECT customer_id, SUM(amount) AS revenue, SUM(amount * 1.2) AS gross FROM proj.raw.orders GROUP BY 1",
        "cased": "SELECT customer_id, SUM(CASE WHEN status = 'paid' THEN amount END) AS revenue, SUM(amount * 1.1) AS gross FROM proj.raw.orders GROUP BY 1",
    })
    terms = {t.term: t for t in found.conflicts}
    gross = terms["gross"].differences[0]
    assert gross["kind"] == "constant" and gross["text"].startswith("differ only in a constant: ") and ("1.1 vs 1.2" in gross["text"] or "1.2 vs 1.1" in gross["text"])
    revenue = terms["revenue"].differences[0]
    assert revenue["kind"] == "filter" and "raw.orders.status = 'paid'" in revenue["text"] and "inside the aggregate" in revenue["text"]


def test_different_source_columns_passed_through_are_the_lower_tier():
    found = report({
        "stg_a": "SELECT order_id, amount AS revenue FROM proj.raw.orders",
        "stg_b": "SELECT order_id, net_amount AS revenue FROM proj.raw.orders",
    })
    (term,) = found.conflicts
    assert term.tier == "passthrough" and term.differences[0]["kind"] == "column"
    assert "raw.orders.net_amount" in term.differences[0]["text"] and "raw.orders.amount" in term.differences[0]["text"]


# ------------------------------------------------------------- what is not reported


def test_a_column_carried_downstream_or_passed_through_twice_is_one_definition():
    found = report({
        "stg_a": "SELECT order_id, amount AS revenue FROM proj.raw.orders",
        "stg_b": "SELECT order_id, amount AS revenue FROM proj.raw.orders WHERE status = 'paid'",  # other rows, same values
        "mart": "SELECT order_id, revenue FROM proj.core.stg_a",
        "totals": "SELECT customer_id, SUM(amount) AS total FROM proj.raw.orders GROUP BY 1",
        "report": "SELECT customer_id, total FROM proj.core.totals",
    })
    assert not found.conflicts
    assert found.terms["revenue"].tables == 3 and len(found.terms["revenue"].definitions) == 1
    assert found.terms["total"].definitions[0].members[1].carried is True or found.terms["total"].definitions[0].members[0].carried is True
    assert found.counts()["consistent"] == 2


def test_a_sum_of_the_column_over_the_same_rows_and_a_cast_are_the_same_definition():
    found = report({
        "stg": "SELECT order_id, amount AS revenue, CAST(net_amount AS NUMERIC) AS net FROM proj.raw.orders",
        "by_customer": "SELECT customer_id, SUM(amount) AS revenue, net_amount AS net FROM proj.raw.orders GROUP BY customer_id, net_amount",
        "paid_where": "SELECT customer_id, COUNT(*) AS paid_orders FROM proj.raw.orders WHERE status = 'paid' GROUP BY 1",
        "paid_case": "SELECT customer_id, COUNTIF(status = 'paid') AS paid_orders FROM proj.raw.orders GROUP BY 1",
    })
    assert not found.conflicts, [t.summary() for t in found.conflicts]
    assert len(found.terms["revenue"].definitions[0].groups) == 2  # the row-level column and its SUM, shown as one


def test_a_cast_that_can_change_values_and_a_filter_inside_an_aggregate_that_keeps_nulls_are_differences():
    found = report({
        "stg": "SELECT order_id, amount AS price, CAST(amount AS STRING) AS price_text FROM proj.raw.orders",
        "rounded": "SELECT order_id, CAST(amount AS INT64) AS price, amount AS price_text FROM proj.raw.orders",
        "kept": "SELECT customer_id, ARRAY_AGG(amount IGNORE NULLS) AS amounts FROM proj.raw.orders WHERE status = 'paid' GROUP BY 1",
        "cased": "SELECT customer_id, ARRAY_AGG(CASE WHEN status = 'paid' THEN amount END IGNORE NULLS) AS amounts FROM proj.raw.orders GROUP BY 1",
    })
    terms = {t.term: t for t in found.conflicts}
    assert "price_text" not in terms  # a number written as text is the same value
    assert terms["price"].differences[0]["kind"] == "cast" and "INT64" in terms["price"].differences[0]["text"]
    # ARRAY_AGG is not among the aggregates known to skip NULLs, so the two placements stay apart (conservative: with
    # IGNORE NULLS they agree), and the text names both filters
    assert "inside the aggregate" in terms["amounts"].differences[0]["text"] and "(in 2 only)" in terms["amounts"].differences[0]["text"]


def test_key_like_and_housekeeping_names_are_skipped_unless_asked_for():
    models = {
        "a": "SELECT order_id AS id, customer_id, created_at, amount AS _loaded_at FROM proj.raw.orders",
        "b": "SELECT customer_id AS id, order_id AS customer_id, TIMESTAMP_TRUNC(created_at, DAY) AS created_at, status AS _loaded_at FROM proj.raw.orders",
    }
    found = report(models)
    assert not found.conflicts and found.skipped_key_like == 4
    loud = report(models, include_keys=True)
    assert {t.term for t in loud.conflicts} == {"id", "customer_id", "created_at", "_loaded_at"}
    assert dc.is_key_like("order_id") and dc.is_key_like("updated_at") and not dc.is_key_like("revenue")


def test_variants_of_a_name_are_one_term_only_when_asked():
    models = {
        "a": "SELECT customer_id, SUM(amount) AS revenue FROM proj.raw.orders GROUP BY 1",
        "b": "SELECT customer_id, SUM(net_amount) AS revenue_usd FROM proj.raw.orders GROUP BY 1",
    }
    assert not report(models).conflicts
    (term,) = report(models, variants=True).conflicts
    assert term.term == "revenue" and term.names == ("revenue", "revenue_usd")


def test_a_dataform_placeholder_that_means_different_things_upstream_is_unknown_not_compared():
    upstream, downstream = Target("proj", "core", "raw_window"), Target("proj", "core", "report")
    models = {
        upstream.key: Model(upstream, "table", "SELECT customer_id, amount FROM proj.raw.orders WHERE amount > __sqlx_token_000__",
                            masked_expressions=("${vars.raw_days}",)),
        downstream.key: Model(downstream, "table", "SELECT customer_id, SUM(amount) AS revenue FROM proj.core.raw_window "
                              "WHERE customer_id > __sqlx_token_000__ GROUP BY 1", masked_expressions=("${vars.report_days}",)),
        "proj.core.other": Model(Target("proj", "core", "other"), "table", "SELECT customer_id, SUM(amount) AS revenue FROM proj.raw.orders GROUP BY 1"),
    }
    pipeline = Pipeline(models, sources={ORDERS.key: ORDERS}, source_schema=dict(SCHEMA))
    found = dc.check_project(pipeline, equivalences=())
    term = found.terms["revenue"]
    assert not term.conflict and len(term.unknown) == 1 and "placeholder" in term.unknown[0].reason
    assert found.counts()["unchecked"] == 1


# ------------------------------------------------------------------- Malloy


MALLOY = """
source: orders is duckdb.table('raw.orders') extend {
  primary_key: order_id
  dimension: paid is status = 'paid'
  measure: revenue is amount.sum() { where: status = 'paid' }
  measure: order_count is count()
}
source: gross is duckdb.table('raw.orders') extend {
  dimension: is_paid is status = 'paid'
  measure: revenue is net_amount.sum()
  measure: order_count is count() { where: is_paid }
  dimension: size is pick 'big' when amount > 100 else 'small'
}
"""


def test_the_malloy_reader_keeps_each_field_expression_and_the_source_filters():
    sources = {s["name"]: s for s in read_malloy(MALLOY + "source: big is orders extend { where: amount > 10 }")}
    fields = {f["name"]: f for f in sources["orders"]["fields"]}
    assert fields["revenue"] == {"kind": "measure", "name": "revenue", "expression": "amount.sum() { where: status = 'paid' }", "line": 5}
    assert sources["big"]["filters"] == ["amount > 10"] and {f["name"] for f in sources["big"]["fields"]} == {"paid", "revenue", "order_count"}


def test_malloy_measures_of_one_name_that_differ_clash_with_each_other_and_with_models(tmp_path):
    folder = tmp_path / "semantic"
    folder.mkdir()
    (folder / "sales.malloy").write_text(MALLOY, encoding="utf-8")
    found = dc.check_project(build({
        "paid": "SELECT customer_id, SUM(amount) AS revenue FROM proj.raw.orders WHERE status = 'paid' GROUP BY 1",
    }), malloy=[folder], equivalences=())
    revenue = found.terms["revenue"]
    assert revenue.conflict and len(revenue.definitions) == 2
    same = next(e for e in revenue.definitions if any(m.table == "proj.core.paid" for m in e.members))
    assert {m.table for m in same.members} == {"proj.core.paid", "sales.malloy:orders"}  # the filtered measure is the model's definition
    other = next(e for e in revenue.definitions if e is not same)
    assert [m.table for m in other.members] == ["sales.malloy:gross"] and other.members[0].measure
    order_count = found.terms["order_count"]
    assert order_count.conflict and order_count.differences[0]["kind"] == "filter"
    assert "raw.orders.status = 'paid'" in order_count.differences[0]["text"]
    assert found.terms["size"].definitions[0].lead.meaning.startswith("expr:CASE WHEN")


def test_a_malloy_expression_beyond_the_reader_is_an_unknown_definition():
    sources = [("m.malloy", source) for source in read_malloy(
        "source: s is t.table('raw.orders') extend { measure: revenue is customers.amount.sum()\n measure: ok is amount.sum() }")]
    definitions, _ = dc.malloy_definitions(build({}), sources=sources)
    by_name = {d.column: d for d in definitions}
    assert by_name["revenue"].meaning is None and "not translated" in by_name["revenue"].reason
    assert by_name["ok"].meaning == "agg:SUM(col:proj.raw.orders.amount)"


# ------------------------------------------------------- equivalences and ranking


def test_a_saved_equivalence_on_two_definitions_is_shown_and_a_differs_record_ranks_it_later():
    models = {
        "paid": "SELECT customer_id, SUM(amount) AS revenue, SUM(amount) AS gross FROM proj.raw.orders WHERE status = 'paid' GROUP BY 1",
        "gross": "SELECT customer_id, SUM(amount) AS revenue, SUM(net_amount) AS gross FROM proj.raw.orders GROUP BY 1",
    }
    recorded = [{"left": "proj.core.paid", "right": "core.gross", "columns": [["revenue", "revenue"]], "status": "differs", "explanation": "paid only"},
                {"left": "proj.core.gross", "right": "proj.core.paid", "columns": [["gross", "gross"]], "whole": False}]
    found = report(models, equivalences=recorded)
    terms = {t.term: t for t in found.conflicts}
    (revenue,) = terms["revenue"].declared
    assert revenue["status"] == "differs" and "[paid only]" in revenue["text"] and terms["revenue"].acknowledged
    (gross,) = terms["gross"].declared
    assert gross["status"] == "equal" and "declared equal" in gross["text"] and "not a proof" in gross["text"]
    assert [t.term for t in found.conflicts] == ["gross", "revenue"]  # the explained conflict comes after
    assert [t.term for t in report(models).conflicts] == ["gross", "revenue"]  # alphabetical when nothing else differs


def test_computed_conflicts_rank_before_pass_throughs_and_wider_terms_first():
    found = report({
        "a": "SELECT order_id, amount AS label, SUM(amount) AS revenue, MAX(amount) AS peak FROM proj.raw.orders GROUP BY 1, 2",
        "b": "SELECT order_id, net_amount AS label, AVG(amount) AS revenue, MIN(amount) AS peak FROM proj.raw.orders GROUP BY 1, 2",
        "c": "SELECT order_id, AVG(amount) AS revenue FROM proj.raw.orders GROUP BY 1",
    })
    assert [(t.term, t.tier) for t in found.conflicts] == [("revenue", "computed"), ("peak", "computed"), ("label", "passthrough")]


def test_a_name_nearly_every_table_defines_its_own_way_is_marked_generic_and_ranked_last_in_its_tier():
    models = {f"m{n}": f"SELECT order_id, COUNT(*) OVER (PARTITION BY customer_id) AS counter FROM proj.raw.orders WHERE amount > {n}"
              for n in range(5)}
    models["m0"] = "SELECT order_id, ROUND(amount, 2) AS price, COUNT(*) OVER (PARTITION BY customer_id) AS counter FROM proj.raw.orders WHERE amount > 0"
    models["m1"] = "SELECT order_id, ROUND(amount, 2) AS price, COUNT(*) OVER (PARTITION BY customer_id) AS counter FROM proj.raw.orders WHERE amount > 1"
    models["other"] = "SELECT order_id, ROUND(amount, 0) AS price FROM proj.raw.orders"
    found = report(models)
    assert [t.term for t in found.conflicts] == ["price", "counter"]  # counter is in more tables, but price is one term
    assert found.terms["counter"].generic and not found.terms["price"].generic and len(found.terms["counter"].definitions) == 5
    assert "likely a generic name" in found.terms["counter"].summary()


# ------------------------------------------------------------------- surfaces


def test_the_json_report_has_a_stable_shape(tmp_path, capsys):
    project = write_project(tmp_path / "proj", {
        "orders.sqlx": 'config { type: "declaration", schema: "raw", name: "orders" }',
        "paid.sqlx": 'config { type: "table" }\nSELECT customer_id, SUM(amount) AS revenue FROM ${ref("raw", "orders")} WHERE status = "paid" GROUP BY 1\n',
        "gross.sqlx": 'config { type: "table" }\nSELECT customer_id, SUM(amount) AS revenue FROM ${ref("raw", "orders")} GROUP BY 1\n',
    })
    saved = tmp_path / "equivalences.json"
    saved.write_text(json.dumps([{"left": "p.ds.paid", "right": "p.ds.gross", "columns": [["revenue", "revenue"]], "status": "differs"}]))
    assert dc.main(["--project", str(project), "--json", "--equivalences", str(saved)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["format"] == dc.FORMAT and data["conflicts_shown"] == "1 of 1"
    assert set(data["counts"]) == {"definitions", "terms_in_several_tables", "conflicts", "computed", "passthrough", "consistent", "unchecked", "key_like_skipped"}
    (conflict,) = data["conflicts"]
    assert set(conflict) == {"term", "names", "conflict", "tier", "tables", "generic_name", "definitions", "differences", "declared", "acknowledged", "unknown_definitions"}
    definition = conflict["definitions"][0]
    assert set(definition) == {"number", "expression", "meaning", "kind", "filters", "tables"}
    assert set(definition["tables"][0]) == {"table", "column", "origin", "grain", "measure", "carried", "as", "written"}
    assert {t["written"] for d in conflict["definitions"] for t in d["tables"]} == {"SUM(amount)"}
    assert conflict["declared"][0]["status"] == "differs" and conflict["differences"][0]["between"] == [2, 1]
    assert dc.main(["--project", str(project)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("1 term with several definitions") and "differ only in WHERE raw.orders.status = 'paid'" in text


def _many_conflicts() -> Pipeline:
    columns = ", ".join(f"SUM(amount * {n}) AS metric_{n}" for n in range(1, 13))
    other = ", ".join(f"AVG(net_amount + {n}) AS metric_{n}" for n in range(1, 13))
    return build({
        "wide_a": f"SELECT customer_id, {columns} FROM proj.raw.orders WHERE status = 'a_rather_long_status_value_to_fill_the_answer' GROUP BY 1",
        "wide_b": f"SELECT customer_id, {other} FROM proj.raw.orders WHERE status = 'another_long_status_value_for_the_other_side' GROUP BY 1",
    })


def test_the_agent_answers_stay_within_the_size_bound(monkeypatch):
    block_network(monkeypatch)
    server = mcp_server.AgentServer(Session(_many_conflicts()))
    body, failed, used = ask(server, "project_checks", check="definition_conflicts")
    assert not failed and used <= BOUND
    (row,) = body["findings"]
    assert row["count"] == 12 and 1 < len(row["examples"]) < 12 and "find_existing term=" in row["fix"]
    body, failed, used = ask(server, "find_existing", term="metric_7")
    assert not failed and used <= BOUND and body["verdict"] == "conflict" and body["counts"]["definitions"] == 2
    assert body["differences"] and "AVG" in body["differences"][0]
    card, failed, used = ask(server, "describe_table", table="wide_a")
    assert not failed and used <= BOUND and card["definition_conflicts"].startswith("defined differently in other tables: ")
    assert "+8 more" in card["definition_conflicts"]


def test_find_existing_term_says_when_a_name_means_one_thing_or_is_not_there(monkeypatch):
    block_network(monkeypatch)
    server = mcp_server.AgentServer(Session(build({
        "stg": "SELECT order_id, customer_id, amount AS revenue FROM proj.raw.orders",
        "mart": "SELECT order_id, customer_id, revenue FROM proj.core.stg",
    })))
    body, failed, _ = ask(server, "find_existing", term="revenue")
    assert not failed and body["verdict"] == "one_definition" and "means the same in all 2 tables" in body["answer"]
    body, _, _ = ask(server, "find_existing", term="customer_id")  # key-like, but asked for by name
    assert body["verdict"] == "one_definition"
    body, _, _ = ask(server, "find_existing", term="nope")
    assert body["verdict"] == "not_found" and body["next"]
    assert ask(server, "find_existing", term="revenue", table="stg")[1]
    assert ask(server, "find_existing", term="not a name")[1]
    assert "definition_conflicts" not in ask(server, "describe_table", table="stg")[0]
