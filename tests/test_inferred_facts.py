"""Inferred column and row facts (``kumosql.inferred_facts``) in ``describe_table`` and ``check_query``.

The prover (``kumo_sql_prover.output_properties``) proves from a query's structure which output columns are never NULL,
which column sets are unique and how many rows it returns. Every fact carries its source (inferred from the query, schema
snapshot, declared, derived grain), a disagreement shows both sides, and a model's facts are computed once per session.
Each test builds its own project under ``tmp_path``, so the tests are parallel-safe.
"""

import pytest

from kumosql import mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from test_mcp_server import ask, findings_of, no_network, write_project  # noqa: F401

BOUND = 2000
SOURCE = {"p.src.raw_orders": {"order_id": "INT64", "customer_id": "INT64", "status": "STRING", "amount": "FLOAT64"},
          "p.src.raw_customers": {"id": "INT64", "name": "STRING"}}
PROJECT = {
    "raw_orders.sqlx": 'config { type: "declaration", schema: "src", name: "raw_orders" }\n',
    "raw_customers.sqlx": 'config { type: "declaration", schema: "src", name: "raw_customers" }\n',
    # never NULL: COALESCE with a constant, WHERE x IS NOT NULL, an inner-join key
    "orders.sqlx": 'config { type: "table" }\nSELECT order_id, customer_id, COALESCE(status, "open") AS status, amount FROM ${ref("src", "raw_orders")} WHERE amount IS NOT NULL\n',
    "named.sqlx": 'config { type: "table" }\nSELECT o.order_id, c.id AS customer_key, c.name FROM ${ref("src", "raw_orders")} o JOIN ${ref("src", "raw_customers")} c ON o.customer_id = c.id\n',
    # unique: GROUP BY, DISTINCT, QUALIFY ROW_NUMBER() = 1
    "per_customer.sqlx": 'config { type: "table" }\nSELECT customer_id, COUNT(*) AS n, SUM(amount) AS total FROM ${ref("orders")} GROUP BY customer_id\n',
    "statuses.sqlx": 'config { type: "view" }\nSELECT DISTINCT status, customer_id FROM ${ref("orders")}\n',
    "latest.sqlx": 'config { type: "view" }\nSELECT * FROM ${ref("orders")} QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1\n',
    # rows: a global aggregate returns exactly one row, LIMIT 1 at most one
    "grand_total.sqlx": 'config { type: "table" }\nSELECT SUM(amount) AS total, COUNT(*) AS n FROM ${ref("orders")}\n',
    "one_order.sqlx": 'config { type: "view" }\nSELECT order_id FROM ${ref("orders")} LIMIT 1\n',
    # declared facts, an incremental table, a source whose columns are not known
    "checked.sqlx": 'config { type: "table", assertions: { nonNull: ["customer_id"], uniqueKey: ["order_id", "customer_id"] } }\nSELECT order_id, customer_id FROM ${ref("orders")}\n',
    "growing.sqlx": 'config { type: "incremental" }\nSELECT customer_id, COUNT(*) AS n FROM ${ref("orders")} GROUP BY customer_id\n',
    "raw_events.sqlx": 'config { type: "declaration", schema: "src", name: "raw_events" }\n',
    "events.sqlx": 'config { type: "table" }\nSELECT DISTINCT user_id FROM ${ref("src", "raw_events")}\n',
    # a cross join of a keyed table with itself: the derived grain is unknown, the prover keeps the pair of keys
    "pairs.sqlx": 'config { type: "table" }\nSELECT a.customer_id AS left_id, b.customer_id AS right_id, a.n FROM ${ref("per_customer")} a CROSS JOIN ${ref("per_customer")} b\n',
}


def build(tmp_path, snapshot=None, files=PROJECT):
    folder = write_project(tmp_path / "project", files)
    path = None
    if snapshot is not None:
        path = tmp_path / "columns.csv"
        path.write_text(snapshot)
    return mcp_server.AgentServer(Session(load_sqlx_project(folder, source_schema=SOURCE), schema_snapshot=path))


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """One project for the tests that only read it (the caching test builds its own, so nothing is cached before it)."""

    return build(tmp_path_factory.mktemp("inferred_facts"))


def facts(served, table):
    body, failed, used = ask(served, "describe_table", table=table)
    assert not failed and used <= BOUND, (used, body)
    return body.get("facts", []), body


# ------------------------------------------------------------------ never NULL


def test_coalesce_and_is_not_null_make_columns_never_null_inferred_from_the_query(served):
    lines, body = facts(served, "orders")
    assert "never NULL: status, amount (inferred from the query)" in lines
    assert not any("order_id" in line for line in lines)  # nothing proves it: absent, never guessed


def test_an_inner_join_key_is_never_null(served):
    lines, _ = facts(served, "named")
    assert "never NULL: customer_key (inferred from the query)" in lines


def test_the_session_hook_now_answers_from_the_query_where_it_said_not_known(served):
    session = served.session
    assert session.column_is_nullable("p.ds.orders", "status") is False  # COALESCE with a constant
    assert session.column_is_nullable("p.ds.orders", "order_id") is None  # still not known
    assert session.column_is_nullable("p.ds.growing", "customer_id") is None  # an incremental table's query is not its table


# ---------------------------------------------------------------------- unique


@pytest.mark.parametrize("table, line", [
    ("per_customer", "unique: (customer_id) (inferred from the query, derived grain)"),
    ("statuses", "unique: (status, customer_id) (inferred from the query, derived grain)"),
    ("latest", "unique: (customer_id) (inferred from the query, derived grain)"),
])
def test_group_by_distinct_and_qualify_row_number_make_a_unique_key(served, table, line):
    lines, body = facts(served, table)
    assert line in lines and lines.index(line) == 0  # keys first: the most useful fact


def test_a_key_the_derived_grain_misses_is_reported_and_sharpens_the_grain_unknown(served):
    lines, body = facts(served, "pairs")
    assert body["grain"] is None
    [line] = [line for line in lines if line.startswith("unique:")]
    assert line.startswith("unique: (left_id, right_id) (inferred from the query; given ds.per_customer unique on (customer_id) [derived grain]")
    [grain] = [u for u in body["unknown"] if u["what"] == "grain"]
    assert "its query does prove (left_id, right_id) unique (see facts)" in grain["reason"]


def test_the_fan_out_check_uses_a_key_the_query_proves_when_the_grain_is_unknown(served, no_network):
    on_both = "SELECT p.n FROM ${ref('per_customer')} c JOIN ${ref('pairs')} p ON p.left_id = c.customer_id AND p.right_id = c.customer_id"
    body, _, _ = ask(served, "check_query", sql=on_both)
    assert not findings_of(body, "fan_out") and not any("pairs" in u["what"] and "fan-out" in u["what"] for u in body.get("unknown", []))
    on_one = "SELECT SUM(p.n) FROM ${ref('per_customer')} c JOIN ${ref('pairs')} p ON p.left_id = c.customer_id"
    [found] = findings_of(ask(served, "check_query", sql=on_one)[0], "fan_out")
    assert "one row per (left_id, right_id)" in found["what"] and "right_id is not matched" in found["what"]


# ------------------------------------------------------------------------ rows


def test_a_global_aggregate_returns_exactly_one_row_and_limit_1_at_most_one(served):
    lines, _ = facts(served, "grand_total")
    assert lines[0] == "exactly one row (inferred from the query, derived grain)"
    assert "never NULL: n (inferred from the query)" in lines and not any("total" in line for line in lines)  # SUM over no rows is NULL
    lines, body = facts(served, "one_order")
    assert lines[0] == "at most one row (inferred from the query)"
    [grain] = [u for u in body["unknown"] if u["what"] == "grain"]  # orders has no known grain, so neither has its LIMIT 1
    assert "its query does prove at most one row (see facts)" in grain["reason"]


# -------------------------------------------------------------- other sources


def test_declared_assertions_are_labelled_declared_and_the_query_is_labelled_apart(served):
    lines, _ = facts(served, "checked")
    assert "unique: (order_id, customer_id) (declared)" not in lines  # the declared grain alone: describe_table's grain says it
    assert "never NULL: customer_id (declared)" in lines


def test_an_incremental_table_gets_no_facts_from_its_query_and_the_reason_is_kept(served):
    from kumosql import inferred_facts

    lines, body = facts(served, "growing")
    assert lines == [] and not any("facts" in u["what"] for u in body.get("unknown", []))  # nothing inferred adds nothing to the answer
    found = inferred_facts.model_facts(served.session, "p.ds.growing")
    assert found.not_the_table and "earlier runs" in found.unsupported


def test_a_model_reading_a_table_with_unknown_columns_gets_no_facts_never_a_guess(served):
    from kumosql import inferred_facts

    lines, body = facts(served, "events")
    assert lines == [] and not any("facts" in u["what"] for u in body.get("unknown", []))
    found = inferred_facts.model_facts(served.session, "p.ds.events")
    assert "raw_events" in found.unsupported and "columns are not known" in found.unsupported


# ---------------------------------------------------------------- disagreement

SNAPSHOT = """table_catalog,table_schema,table_name,column_name,data_type,is_nullable
p,ds,orders,order_id,INT64,NO
p,ds,orders,customer_id,INT64,YES
p,ds,orders,status,STRING,YES
p,ds,orders,amount,FLOAT64,YES
"""


def test_a_snapshot_that_says_nullable_where_the_query_never_writes_null_shows_both_and_the_checks_keep_the_snapshot(tmp_path, no_network):
    served = build(tmp_path, SNAPSHOT)
    lines, _ = facts(served, "orders")
    assert "NULLABLE in the schema snapshot, but never NULL (inferred from the query): status, amount" in lines
    assert "never NULL: order_id (schema snapshot)" in lines
    assert not any(line.startswith("never NULL:") and "status" in line for line in lines)  # never one side alone
    assert served.session.column_is_nullable("p.ds.orders", "status") is True  # the deployed table's mode wins in the checks

    sql = "SELECT c.customer_id FROM ${ref('per_customer')} c WHERE CAST(c.customer_id AS STRING) NOT IN (SELECT o.status FROM ${ref('orders')} o)"
    body, _, _ = ask(served, "check_query", sql=sql)
    assert findings_of(body, "not_in_null")  # still a finding, from the snapshot
    [line] = [line for line in body["facts"] if line.startswith("orders:")]
    assert "status" in line and "NULLABLE in the schema snapshot" in line and "never writes NULL" in line and "the checks used the snapshot" in line


# -------------------------------------------------------------------- drafts


def test_check_query_reports_the_facts_of_the_draft_output_with_their_sources(served, no_network):
    body, failed, used = ask(served, "check_query", sql="SELECT customer_id, COUNT(*) AS n FROM ${ref('orders')} GROUP BY customer_id")
    assert not failed and used <= BOUND
    assert body["facts"][:2] == ["unique: (customer_id) (inferred from the query)", "never NULL: n (inferred from the query)"]
    body, _, _ = ask(served, "check_query", sql="SELECT DISTINCT status FROM ${ref('orders')}")
    assert "never NULL: status (inferred from the query; given ds.orders.status NOT NULL [inferred from the query of orders])" in body["facts"]
    body, _, _ = ask(served, "check_query", sql="SELECT SUM(amount) AS total FROM ${ref('orders')}")
    assert body["facts"] == ["exactly one row (inferred from the query)"]


def test_a_draft_over_a_table_with_unknown_columns_has_no_facts_and_no_extra_unknown(served, no_network):
    body, _, _ = ask(served, "check_query", sql="SELECT user_id FROM ${ref('src', 'raw_events')}")
    assert "facts" not in body and not any("facts" in u["what"] for u in body["unknown"])


def test_not_in_over_a_subquery_the_prover_shows_never_null_is_cleared(served, no_network):
    cleared = "SELECT o.order_id FROM ${ref('orders')} o WHERE o.order_id NOT IN (SELECT COALESCE(customer_id, 0) FROM ${ref('orders')})"
    body, _, _ = ask(served, "check_query", sql=cleared)
    assert not findings_of(body, "not_in_null") and not any(u["what"] == "NOT IN subqueries not checked" for u in body.get("unknown", []))
    # a model column the query proves never NULL clears it too; one it does not prove stays undecided
    body, _, _ = ask(served, "check_query", sql="SELECT 1 FROM ${ref('orders')} o WHERE o.amount NOT IN (SELECT amount FROM ${ref('orders')})")
    assert not any(u["what"] == "NOT IN subqueries not checked" for u in body.get("unknown", []))
    body, _, _ = ask(served, "check_query", sql="SELECT 1 FROM ${ref('orders')} o WHERE o.order_id NOT IN (SELECT order_id FROM ${ref('orders')})")
    assert any(u["what"] == "NOT IN subqueries not checked" for u in body["unknown"])


def test_a_not_in_subquery_over_a_cte_is_never_read_as_the_project_table_of_the_same_name(served, no_network):
    # ``orders`` here is the draft's own CTE, whose column may be NULL: the model orders (status never NULL) must not clear it
    sql = ("WITH orders AS (SELECT CAST(NULL AS STRING) AS status) "
           "SELECT 1 FROM ${ref('per_customer')} c WHERE CAST(c.customer_id AS STRING) NOT IN (SELECT status FROM orders)")
    body, _, _ = ask(served, "check_query", sql=sql)
    assert any(u["what"] == "NOT IN subqueries not checked" for u in body["unknown"])
    assert served.session.query_never_null("SELECT status FROM orders", {}) is None  # a name the draft does not map is not looked up


# ------------------------------------------------------------- size and cost


def test_many_never_null_columns_are_one_short_line_and_answers_stay_inside_their_bound(tmp_path, no_network):
    wide = ", ".join(f"COALESCE(status, 'x') AS c{i:02d}" for i in range(60))
    files = {**PROJECT, "wide.sqlx": f'config {{ type: "table" }}\nSELECT {wide} FROM ${{ref("orders")}}\n'}
    served = build(tmp_path, files=files)
    for max_bytes in (1000, 2000):
        body, failed, used = ask(served, "describe_table", table="wide", max_bytes=max_bytes)
        assert not failed and used <= max_bytes
    lines, _ = facts(served, "wide")
    [line] = lines
    assert line.startswith("never NULL: c00, c01") and "(+52 more)" in line and len(line) <= 220
    body, failed, used = ask(served, "check_query", sql=f"SELECT {wide} FROM ${{ref('orders')}}")
    assert not failed and used <= BOUND and "(+52 more)" in body["facts"][0]


def test_a_models_facts_are_inferred_once_per_session(tmp_path, monkeypatch):
    from kumo_sql_prover import output_properties

    served = build(tmp_path)
    calls = []
    real = output_properties.infer_properties

    def counting(sql, *args, **kwargs):
        calls.append(sql)
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(output_properties, "infer_properties", counting)
    facts(served, "per_customer")
    first = len(calls)
    assert first >= 1
    facts(served, "per_customer")
    served.session.column_is_nullable("p.ds.per_customer", "n")
    served.session.column_is_nullable("p.ds.per_customer", "total")
    assert len(calls) == first  # kept in the session, not inferred again
    facts(served, "statuses")
    assert len(calls) > first  # another model is its own entry


def test_a_full_check_query_answer_drops_its_facts_before_any_finding():
    from kumosql.mcp_answers import Answer, render

    answer = Answer("2 problems found.")
    answer.add_list("findings", [{"check": "types", "what": "x" * 700, "fix": "y"}, {"check": "zones", "what": "z" * 700, "fix": "w"}], primary=True)
    answer.add_list("facts", ["never NULL: id (inferred from the query)"], optional=True)
    answer.unknown("something", "r" * 400)
    out = render(answer, max_bytes=2000)
    assert len(out["findings"]) == 2 and "facts" not in out and "facts_shown" not in out and "more" not in out
    roomy = render(answer, max_bytes=4000)
    assert roomy["facts"] == ["never NULL: id (inferred from the query)"]
