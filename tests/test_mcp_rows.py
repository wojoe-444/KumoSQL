"""``rows_filtered`` (where rows are dropped on the way to a column) and the filter order of ``trace_column``."""

import json

import pytest

from kumosql import mcp_answers, mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project

BOUND = mcp_answers.DEFAULT_BYTES

CHAIN = {
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, 3 AS region_code, "open" AS status, 5 AS amount\n',
    "shipping_zones.sqlx": 'config { type: "table" }\nSELECT 3 AS code, "north" AS zone_name\n',
    "orders_clean.sqlx": 'config { type: "view" }\nSELECT order_id, region_code, status, amount FROM ${ref("orders")} WHERE status != \'cancelled\'\n',
    "orders_enriched.sqlx": 'config { type: "view" }\nSELECT o.order_id, o.region_code, o.status, o.amount, z.zone_name '
    'FROM ${ref("orders_clean")} o INNER JOIN ${ref("shipping_zones")} z ON o.region_code = z.code\n',
    "orders_report.sqlx": 'config { type: "view" }\nSELECT order_id, status, zone_name, amount FROM ${ref("orders_enriched")}\n',
}


def write_project(folder, files):
    (folder / "definitions").mkdir(parents=True, exist_ok=True)
    (folder / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    for name, text in files.items():
        (folder / "definitions" / name).write_text(text)
    return folder


def serve(folder, files):
    return mcp_server.AgentServer(Session(load_sqlx_project(write_project(folder, files))))


def ask(server, name, **arguments):
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]
    text = result["content"][0]["text"]
    return json.loads(text), result["isError"], len(text.encode("utf-8"))


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    return serve(tmp_path_factory.mktemp("chain"), CHAIN)


def test_rows_filtered_lists_the_cancelled_filter_and_the_zone_join_in_path_order(chain):
    body, failed, used = ask(chain, "rows_filtered", table="orders_report", column="status")
    assert not failed and used <= BOUND
    # path order: from the table towards the sources, so the join of orders_enriched comes before the filter of orders_clean
    join, cancelled = body["filters"]
    assert (join["model"], join["kind"], join["on"]) == ("ds.orders_enriched", "inner_join", "joined")
    assert "z.code" in join["clause"]
    assert (cancelled["model"], cancelled["kind"], cancelled["on"]) == ("ds.orders_clean", "where", "column")
    assert "cancelled" in cancelled["clause"]
    assert body["counts"]["conditions"] == 2 and body["counts"]["on_column"] == 1 and body["counts"]["joined"] == 1
    assert body["next"]


def test_a_predicate_the_filter_contradicts_is_reported_as_removed(chain):
    body, failed, used = ask(chain, "rows_filtered", table="orders_report", column="status", predicate="status = 'cancelled'")
    assert not failed and used <= BOUND
    removed = [item for item in body["filters"] if item["result"] == "can_remove"]
    assert len(removed) == 1 and removed[0]["model"] == "ds.orders_clean" and "cancelled" in removed[0]["clause"]
    assert body["filters"][0]["result"] == "can_remove"  # what removes them is listed first
    # the zone join reads another table: it may remove them, and nothing is claimed
    joined = [item for item in body["filters"] if item["kind"] == "inner_join"]
    assert joined and joined[0]["result"] == "may_remove" and joined[0]["why"]
    assert body["counts"]["can_remove"] == 1 and body["counts"]["may_remove"] == 1


def test_a_predicate_the_filter_passes_is_counted_as_kept_not_removed(chain):
    body, _, _ = ask(chain, "rows_filtered", table="orders_report", predicate="status = 'open'")
    assert body["counts"]["keeps"] == 1 and body["counts"]["can_remove"] == 0
    assert all("cancelled" not in item["clause"] for item in body["filters"])


def test_a_predicate_alone_names_the_columns_to_follow(chain):
    body, failed, _ = ask(chain, "rows_filtered", table="orders_report", predicate="status IN ('cancelled', 'open')")
    assert not failed and body["counts"]["can_remove"] == 0 and body["counts"]["keeps"] == 0 and body["counts"]["may_remove"] == 2


def test_a_condition_on_another_column_is_compared_through_the_unchanged_copies(chain):
    body, _, _ = ask(chain, "rows_filtered", table="orders_clean", predicate="region_code = 3")
    # the cancelled filter reads another column: it may drop such orders, and the answer does not claim more
    assert body["counts"]["can_remove"] == 0 and body["counts"]["may_remove"] == 1 and body["filters"][0]["why"] == "reads other columns"
    body, _, _ = ask(chain, "rows_filtered", table="orders_report", column="amount", predicate="status = 'cancelled'")
    assert body["counts"]["can_remove"] == 1


def test_a_source_the_project_does_not_build_has_nothing_upstream_and_says_so(tmp_path):
    files = {"report.sqlx": 'config { type: "view" }\nSELECT id, status FROM raw.events WHERE status = \'done\'\n'}
    server = serve(tmp_path, files)
    body, failed, _ = ask(server, "rows_filtered", table="report", column="status")
    assert not failed and body["counts"]["conditions"] == 1
    assert any("source table" in item["what"] for item in body["unknown"])


def test_rows_filtered_refuses_a_question_without_a_column_or_predicate_and_bad_predicates(chain):
    body, failed, _ = ask(chain, "rows_filtered", table="orders_report")
    assert failed and "column or a predicate" in body["error"] and body["next"]
    body, failed, _ = ask(chain, "rows_filtered", table="orders_report", predicate="nope = 1")
    assert failed and "no column" in body["error"]
    body, failed, _ = ask(chain, "rows_filtered", table="orders_report", predicate="status IN (SELECT 1)")
    assert failed and "simple" in body["error"]
    body, failed, _ = ask(chain, "rows_filtered", table="orders_report", predicate="region_code = EXTRACT(DAY FROM CURRENT_DATE())")
    assert failed and "time it runs" in body["error"]


SEMI = {
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, "open" AS status, 7 AS customer_id\n',
    "customers.sqlx": 'config { type: "table" }\nSELECT 7 AS customer_id, TRUE AS active\n',
    "recent.sqlx": 'config { type: "view" }\nSELECT order_id, status FROM ${ref("orders")} '
    "WHERE customer_id IN (SELECT customer_id FROM ${ref(\"customers\")} WHERE active) QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY status) = 1\n",
    "outer_kept.sqlx": 'config { type: "view" }\nSELECT o.order_id, o.status FROM ${ref("orders")} o LEFT JOIN ${ref("customers")} c ON o.customer_id = c.customer_id AND c.active\n',
}


def test_semi_joins_and_qualify_are_listed_and_outer_join_conditions_are_counted_not_listed(tmp_path):
    server = serve(tmp_path, SEMI)
    body, failed, _ = ask(server, "rows_filtered", table="recent", column="status")
    assert not failed
    kinds = {item["kind"] for item in body["filters"]}
    assert {"semi_join", "qualify"} <= kinds
    body, _, _ = ask(server, "rows_filtered", table="recent", column="status", predicate="status = 'open'")
    assert all(item["result"] == "may_remove" for item in body["filters"])
    body, _, _ = ask(server, "rows_filtered", table="outer_kept", column="status")
    assert body["counts"]["conditions"] == 0 and body["counts"]["not_listed"] == {"outer_join_on": 1}
    assert "no condition" in body["answer"]


TRICKY = {
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, "open" AS status\n',
    "shouted.sqlx": 'config { type: "view" }\nWITH u AS (SELECT order_id, UPPER(status) AS status, status AS raw_status FROM ${ref("orders")}) '
    "SELECT order_id, raw_status AS status FROM u WHERE status = 'CANCELLED'\n",
    "twice.sqlx": 'config { type: "view" }\nSELECT a.order_id, a.status FROM ${ref("orders")} a JOIN ${ref("orders")} b ON a.order_id = b.order_id WHERE b.status = \'open\'\n',
}


def test_a_condition_on_a_changed_copy_or_the_other_side_of_a_self_join_is_never_called_a_proven_removal(tmp_path):
    server = serve(tmp_path, TRICKY)
    for table in ("shouted", "twice"):
        body, failed, _ = ask(server, "rows_filtered", table=table, column="status", predicate="status = 'cancelled'")
        assert not failed and body["counts"]["can_remove"] == 0 and body["counts"]["may_remove"] >= 1 and body["counts"]["keeps"] == 0, table


# ------------------------------------------------------- the long chain and trace_column

LAYERS = 18
MODELS = 7


def long_chain():
    """7 models (the first only a source) with 18 conditions each in the other 6 (108): a WHERE per layer on columns other than status, and one on status in two models, in a middle layer."""

    files = {"m0.sqlx": 'config { type: "table" }\nSELECT 1 AS status, 1 AS amount, ' + ", ".join(f"{i} AS k{i}" for i in range(LAYERS)) + "\n"}
    columns = ", ".join(["status", "amount"] + [f"k{i}" for i in range(LAYERS)])
    for number in range(1, MODELS):
        ctes = []
        previous = f'${{ref("m{number - 1}")}}'
        for layer in range(LAYERS - 1):
            test = "status != 99" if layer == 5 and number in (2, 5) else f"k{layer} > {number}"
            ctes.append(f"s{layer} AS (SELECT {columns} FROM {previous} WHERE {test})")
            previous = f"s{layer}"
        last = f"SELECT {columns} FROM {previous} WHERE " + f"k{LAYERS - 1} > {number}"
        files[f"m{number}.sqlx"] = 'config { type: "view" }\nWITH ' + ",\n".join(ctes) + "\n" + last + "\n"
    return files


def test_trace_column_shows_the_filters_on_the_traced_column_first_when_it_must_cut(tmp_path):
    server = serve(tmp_path, long_chain())
    body, failed, used = ask(server, "trace_column", table=f"m{MODELS - 1}", column="status")
    assert not failed and used <= BOUND
    assert body["counts"]["filters"] >= 100 and body["counts"]["filters_on_column"] >= 2
    models = [group["model"] for group in body["filters_by_model"]]
    # the models with a condition on status come before the others, and within a model that condition comes first
    assert models[:2] == ["p.ds.m5", "p.ds.m2"]
    assert all("status" in group["filters"][0] for group in body["filters_by_model"][:2])
    # what the three-per-model cap hid is counted per model, and the next step names the tool that lists every condition
    assert all(group["more"] >= 12 for group in body["filters_by_model"])
    assert any(step.startswith("rows_filtered table=") for step in body["next"])
    # a narrower bound cuts whole models, the ones without a condition on the column last, and says how many conditions that hid
    body, _, used = ask(server, "trace_column", table=f"m{MODELS - 1}", column="status", max_bytes=1000)
    assert used <= 1000
    assert [group["model"] for group in body["filters_by_model"]][:1] == ["p.ds.m5"]
    assert "conditions)" in body["filters_by_model_shown"] and "max_bytes=" in body["more"]


def test_rows_filtered_pages_a_long_chain_with_the_column_conditions_marked(tmp_path):
    server = serve(tmp_path, long_chain())
    body, failed, used = ask(server, "rows_filtered", table=f"m{MODELS - 1}", column="status")
    assert not failed and used <= BOUND
    assert body["counts"]["conditions"] >= 100 and body["counts"]["on_column"] == 2
    assert "of " in body["filters_shown"]
    rest, _, _ = ask(server, "rows_filtered", table=f"m{MODELS - 1}", column="status", offset=len(body["filters"]))
    assert rest["filters"][0] != body["filters"][0]
    body, _, _ = ask(server, "rows_filtered", table=f"m{MODELS - 1}", column="status", predicate="status = 99")
    assert body["counts"]["can_remove"] == 2 and body["filters"][0]["result"] == "can_remove"


INACTIVE = {
    "orders.sqlx": 'config { type: "table" }\nSELECT 1 AS order_id, "open" AS status, TIMESTAMP "2026-03-02" AS loaded_at\n',
    "orders_clean.sqlx": 'config { type: "view", disabled: true }\nSELECT order_id, status, loaded_at FROM ${ref("orders")} WHERE status != \'cancelled\'\n',
    "orders_kept.sqlx": 'config { type: "incremental" }\nSELECT order_id, status, loaded_at FROM ${ref("orders_clean")} WHERE status != \'void\' '
    "${when(incremental(), `AND loaded_at > (SELECT MAX(loaded_at) FROM ${self()})`)}\n",
    "orders_report.sqlx": 'config { type: "view" }\nSELECT order_id, status FROM ${ref("orders_kept")}\n',
}


def test_filters_in_inactive_and_incremental_models_are_labelled_and_the_first_removal_is_named(tmp_path):
    server = serve(tmp_path, INACTIVE)
    body, failed, used = ask(server, "rows_filtered", table="orders_report", column="status", predicate="status = 'cancelled'")
    assert not failed and used <= BOUND
    # the first model on the path (nearest the table) where the rows are removed is named, with its inactivity
    assert body["first_removed_at"] == "ds.orders_clean (where, in an inactive model)"
    assert "First removed at ds.orders_clean." in body["answer"]
    removed = body["filters"][0]
    assert removed["result"] == "can_remove" and removed["inactive_model"] is True
    # the incremental-only part of a condition is labelled and never read as a plain filter
    marked = [item for item in body["filters"] if item.get("incremental_only")]
    assert marked and all(item["result"] == "may_remove" and "incremental runs only" in item["why"] for item in marked)
    reasons = " ".join(item["what"] for item in body["unknown"])
    assert "inactive model" in reasons and "incremental model" in reasons
    assert body["counts"]["in_inactive_models"] >= 1
    plain, _, _ = ask(server, "rows_filtered", table="orders_report", column="status")
    assert any(item.get("inactive_model") for item in plain["filters"])


def test_no_proof_says_so_instead_of_naming_a_model(chain):
    body, _, _ = ask(chain, "rows_filtered", table="orders_report", column="status", predicate="status = 'open'")
    assert body["first_removed_at"] == "none proven"


def test_rows_filtered_answers_every_model_of_the_106_model_public_project_inside_the_bound():
    from test_mcp_server import load_big

    server = load_big()
    pipeline = server.session.pipeline()
    asked = 0
    for key in pipeline.models:
        columns = list(pipeline.output_columns(key))
        if not columns:
            continue
        for extra in ({}, {"predicate": f"{columns[0]} IS NULL"}):
            body, failed, used = ask(server, "rows_filtered", table=key, column=columns[0], **extra)
            assert not failed and used <= BOUND, (key, body.get("error"), used)
            asked += 1
    assert asked > 100
