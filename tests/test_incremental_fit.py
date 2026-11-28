"""Incremental-fit advice: which full-refresh tables could become incremental, by which pattern, and what blocks them.

Each pattern and each blocker has a case, built as a small Dataform project. The advice only suggests: where it says a
sketch is checkable, the converted model is run through ``incremental-report`` (DuckDB) to show the two agree, and the
contracts the proof rule does not cover still diverge there. Cost figures keep their bases apart.
"""

import json

import pytest

pytest.importorskip("duckdb")

from kumosql.incremental_fit import (  # noqa: E402
    CostAssumptions,
    advise_project,
    report_json,
    report_text,
)
from kumosql.incremental_scan import main as incremental_report  # noqa: E402
from kumosql.incremental_scan import CONTRACTS, scan_project  # noqa: E402

ORDERS = 'config { type: "declaration", name: "orders", uniqueKey: ["order_id"], bigquery: { partitionBy: "DATE(last_upd_ts)" } }\n'
EVENTS = 'config { type: "declaration", name: "events" }\n'


def write(root, name, text):
    path = root / "definitions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def table(root, name, sql, config=""):
    write(root, f"{name}.sqlx", f'config {{ type: "table"{config} }}\n{sql}\n')


def advise(root, **options):
    return {a.model: a for a in advise_project(root, **options)}


def edge(advice, parent):
    return next(e for e in advice.edges if e.parent == parent)


def codes(advice):
    return {b.code for b in advice.blockers} | {b.code for e in advice.edges for b in e.blockers}


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "orders.sqlx", ORDERS)
    write(tmp_path, "events.sqlx", EVENTS)
    return tmp_path


# ------------------------------------------------------------------ patterns


def test_partition_aligned_flat_and_through_a_cte(project):
    table(project, "daily_revenue", 'select date(last_upd_ts) as day, sum(amount) as revenue from ${ref("orders")} group by 1')
    table(project, "daily_cte", 'with o as (select * from ${ref("orders")} where amount > 0)\n'
                                "select date(o.last_upd_ts) as day, count(*) as n from o group by day")
    found = advise(project)
    for name in ("daily_revenue", "daily_cte"):
        fit = edge(found[name], "orders")
        assert found[name].status == "candidate", found[name].to_json()
        assert fit.pattern == "partition_aligned"
        assert fit.partition == "day" and fit.partition_suggested
        assert fit.unique_key == ("day",)  # a grouped table rewrites whole days, merged on the day
        assert fit.watermark == "last_upd_ts" and fit.watermark_basis == "partitionBy"
    # The day-reload sketch has no proof rule: the advice says so instead of claiming anything.
    check = found["daily_revenue"].check
    assert check["checkable"] is False and "search for counterexamples" in check["message"]
    assert "DATE '1970-01-01'" in check["sketch"]["incremental_condition"] and "${self()}" in check["sketch"]["incremental_condition"]


def test_partition_aligned_grouped_in_a_cte_merges_on_the_passed_through_day(project):
    table(project, "daily_inner", 'with d as (select date(last_upd_ts) as day, sum(amount) as revenue from ${ref("orders")} '
                                  "group by 1) select day, revenue from d")
    fit = edge(advise(project)["daily_inner"], "orders")
    assert fit.pattern == "partition_aligned" and fit.partition == "day" and fit.unique_key == ("day",)
    # Without the day in the output there is no partition to suggest and no key to merge the rewritten days on.
    table(project, "revenue_only", 'with d as (select date(last_upd_ts) as day, sum(amount) as revenue from ${ref("orders")} '
                                   "group by 1) select revenue from d")
    revenue_only = advise(project)["revenue_only"]
    assert revenue_only.status == "blocked" and codes(revenue_only) == {"group_not_keyed"}


def test_partition_aligned_row_by_row_appends_and_is_checkable(project):
    table(project, "event_copy", 'select id, created_at, v from ${ref("events")} where v > 0')
    found = advise(project)["event_copy"]
    fit = edge(found, "events")
    assert fit.pattern == "partition_aligned" and fit.partition == "DATE(created_at)"
    assert fit.unique_key == ("id",)  # incremental-report's convention: a column named id is the key
    assert "assumed" in fit.key_basis
    assert fit.watermark_basis == "column name"
    assert found.check["checkable"] is True
    assert found.check["rules"]["append_only"].startswith("R2")


def test_partition_other_column(project):
    table(project, "by_order_date", 'select order_id, order_date, amount, last_upd_ts from ${ref("orders")}',
          ', bigquery: { partitionBy: "order_date" }')
    fit = edge(advise(project)["by_order_date"], "orders")
    assert fit.pattern == "partition_other_column"
    assert fit.partition == "order_date" and not fit.partition_suggested
    assert fit.unique_key == ("order_id",) and fit.key_basis == "declared uniqueKey"
    assert "old and new" in fit.detail


def test_keyed_merge_group_and_latest_per_key(project):
    table(project, "customer_totals",
          'select customer_id, sum(amount) as total, max(last_upd_ts) as last_upd from ${ref("orders")} group by customer_id')
    table(project, "latest_status",
          "select order_id, status from (select order_id, status, row_number() over (partition by order_id order by "
          'last_upd_ts desc) as rn from ${ref("orders")}) where rn = 1')
    found = advise(project)
    totals = edge(found["customer_totals"], "orders")
    assert totals.pattern == "keyed_merge" and totals.unique_key == ("customer_id",) and totals.key == "customer_id"
    assert found["customer_totals"].check["checkable"] is True
    assert found["customer_totals"].check["rules"] == {"append_only": "R5 group re-aggregation"}
    latest = edge(found["latest_status"], "orders")
    assert latest.pattern == "keyed_merge" and latest.unique_key == ("order_id",)
    assert latest.key_basis == "declared uniqueKey"


def test_outer_join_dimension(project):
    write(project, "customers.sqlx", 'config { type: "declaration", name: "customers" }\n')
    table(project, "enriched",
          'select o.order_id, o.last_upd_ts, o.customer_id, c.name from ${ref("orders")} o '
          'left join ${ref("customers")} c on o.customer_id = c.id')
    schema = {"customers": {"id": "INT64", "name": "STRING", "updated_at": "TIMESTAMP"}}
    found = advise(project, source_schema=schema)["enriched"]
    assert found.status == "candidate"
    dim = edge(found, "customers")
    assert dim.pattern == "outer_join_dimension" and dim.preserved == "orders"
    assert dim.key == "id" and dim.unique_key == ("order_id",)
    assert dim.watermark == "updated_at" and dim.watermark_basis == "source schema"
    assert edge(found, "orders").pattern == "partition_aligned"
    assert found.check["checkable"] is False and "several tables" in found.check["message"]


# ------------------------------------------------------------------ blockers


@pytest.mark.parametrize(
    "sql, code, words",
    [
        ('select count(*) as n from ${ref("orders")}', "aggregate_without_key", "aggregate without a partition key"),
        ('select order_id, row_number() over (order by amount) as rnk from ${ref("orders")}',
         "window_over_whole_table", "non-monotone window over the whole table"),
        ('select sum(amount) as total from ${ref("orders")} group by region', "group_not_keyed", "GROUP BY region"),
        ('select order_id, amount from ${ref("orders")} order by amount limit 10', "limit", "LIMIT"),
        ('select a.order_id from ${ref("orders")} a join ${ref("orders")} b on a.order_id = b.parent_id',
         "read_twice", "read 2 times"),
        ('select id, v from ${ref("events")} where v > (select avg(amount) from ${ref("orders")})',
         "subquery_expression", "subquery expression"),
        ('select id, created_at, rand() as r from ${ref("events")}', "nondeterministic", "not deterministic"),
        ('select id, created_at from ${ref("events")} where created_at < current_timestamp()', "nondeterministic",
         "when it runs"),
    ],
)
def test_each_blocker_is_named(project, sql, code, words):
    table(project, "m", sql)
    found = advise(project)["m"]
    assert found.status == "blocked", found.to_json()
    assert code in codes(found)
    messages = [b.message for b in found.blockers] + [b.message for e in found.edges for b in e.blockers]
    assert any(words in m for m in messages), messages
    assert not found.check and not found.cost


def test_source_without_a_watermark(project):
    write(project, "accounts.sqlx", 'config { type: "declaration", name: "accounts", uniqueKey: ["account_id"] }\n')
    table(project, "account_copy", 'select account_id, owner from ${ref("accounts")}')
    found = advise(project)["account_copy"]
    fit = edge(found, "accounts")
    assert fit.pattern == "keyed_merge"  # the shape fits, but nothing tells new rows from old
    assert [b.code for b in fit.blockers] == ["no_watermark"]
    assert "source has no watermark column" in fit.blockers[0].message and "accounts" in fit.blockers[0].fix
    assert found.status == "blocked"


def test_outer_join_on_two_columns_is_a_named_blocker(project):
    write(project, "rates.sqlx", 'config { type: "declaration", name: "rates" }\n')
    table(project, "priced",
          'select o.order_id, o.last_upd_ts, r.rate from ${ref("orders")} o left join ${ref("rates")} r '
          "on o.currency = r.currency and o.day = r.day")
    found = advise(project, source_schema={"rates": {"currency": "STRING", "day": "DATE", "rate": "FLOAT64", "updated_at": "TIMESTAMP"}})
    priced = found["priced"]
    assert priced.status == "partial"  # orders still fits; a change in rates needs a full refresh
    assert [b.code for b in edge(priced, "rates").blockers] == ["nullable_side"]
    assert "more than one column" in edge(priced, "rates").blockers[0].message
    assert any("R6" in note for note in priced.notes)


# ------------------------------------------------------------------ no suggestion


def test_models_with_nothing_to_suggest(project):
    table(project, "constant", "select 1 as x")
    write(project, "already.sqlx", 'config { type: "incremental" }\nselect id from ${ref("events")}\n')
    write(project, "a_view.sqlx", 'config { type: "view" }\nselect id from ${ref("events")}\n')
    write(project, "js.sqlx", 'config { type: "table" }\nselect id from ${ref("events")} where ${dateFilter("created_at")}\n')
    found = advise(project)
    assert set(found) == {"constant", "js"}  # only full-refresh tables are advised
    for name in ("constant", "js"):
        assert found[name].status == "no_suggestion" and not found[name].edges
    assert "reads no table" in found["constant"].notes[0]
    assert "not analysed" in found["js"].notes[0]


# ------------------------------------------------------------------ cost figures


def test_cost_figures_keep_their_bases_apart(project):
    table(project, "customer_totals",
          'select customer_id, sum(amount) as total, max(last_upd_ts) as last_upd from ${ref("orders")} group by customer_id')
    sizes = {"orders": 5e11, "customer_totals": 1e9}
    cost = advise(project, table_bytes=sizes, assumptions=CostAssumptions(changed_fraction=0.01))["customer_totals"].cost
    assert cost["unit"] == "bytes_scanned" and cost["per"] == "run"
    saving = cost["saving"]
    assert saving["estimate"]["basis"] == "estimate" and saving["upper_bound"]["basis"] == "upper_bound"
    assert saving["measured"] is None
    assert saving["estimate"]["value"] <= saving["upper_bound"]["value"]
    assert "assumes changed_fraction=0.01" in saving["estimate"]["caveats"]
    assert cost["full_rebuild"]["basis"] == cost["incremental_run"]["basis"] == "estimate"
    # keyed: the changed share plus the old and new rows of those keys (key_span 3), and the merge reads the table
    assert cost["incremental_run"]["value"] == pytest.approx(5e11 * 0.04 + 1e9)
    assert any("never add them" in note for note in cost["notes"])


def test_cost_without_sizes_has_no_figures_and_small_tables_show_a_regression(project):
    table(project, "event_copy", 'select id, created_at, v from ${ref("events")}')
    assert advise(project, table_bytes={})["event_copy"].cost["saving"] == {"estimate": None, "upper_bound": None, "measured": None}
    assert advise(project)["event_copy"].cost is None
    small = advise(project, table_bytes={"events": 1000.0})["event_copy"].cost
    assert small["saving"]["estimate"]["value"] < 0  # 10 MiB minimum bills: kept as a regression, never clamped
    assert any("regression" in note for note in small["notes"])
    assert any("declares no partitionBy" in c for c in small["saving"]["estimate"]["caveats"])


# ------------------------------------------------------------------ the advice against incremental-report


def test_a_checkable_sketch_converted_by_hand_is_proven_by_incremental_report(project):
    table(project, "customer_totals",
          'select customer_id, sum(amount) as total, max(last_upd_ts) as last_upd from ${ref("orders")} group by customer_id')
    sketch = advise(project)["customer_totals"].check["sketch"]
    converted = project / "converted"
    write(converted, "orders.sqlx", ORDERS)
    write(converted, "customer_totals.sqlx",
          f"config {{ {sketch['config']} }}\n"
          'select customer_id, sum(amount) as total, max(last_upd_ts) as last_upd from ${ref("orders")}\n'
          f"${{when(incremental(), `where {sketch['incremental_condition']}`)}}\n"
          "group by customer_id\n")
    contracts = {name: CONTRACTS[name] for name in ("append_only", "late_and_duplicate")}
    rows = {r.contract: r for r in scan_project(converted, contracts=contracts, seeds=8)}
    assert rows["append_only"].outcome == "safe" and rows["append_only"].rule.startswith("R5")
    # Under late rows or re-deliveries no rule applies, and the DuckDB search finds the divergence.
    assert rows["late_and_duplicate"].outcome == "diverges"


def test_cli_json_and_text(project, capsys, tmp_path):
    table(project, "customer_totals",
          'select customer_id, sum(amount) as total, max(last_upd_ts) as last_upd from ${ref("orders")} group by customer_id')
    table(project, "n", 'select count(*) as n from ${ref("orders")}')
    sizes = tmp_path / "sizes.json"
    sizes.write_text(json.dumps({"orders": 5e11}), encoding="utf-8")
    assert incremental_report([str(project), "--advise", "--json", "--table-bytes", str(sizes)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["candidate"] == 1 and data["summary"]["blocked"] == 1
    assert "nothing here says an incremental version equals the table" in data["notice"]
    by = {m["model"]: m for m in data["models"]}
    assert by["customer_totals"]["patterns"] == {"orders": "keyed_merge"}
    assert by["customer_totals"]["cost"]["saving"]["upper_bound"]["basis"] == "upper_bound"
    assert incremental_report([str(project), "--advise"]) == 0
    text = capsys.readouterr().out
    assert "candidate customer_totals" in text and "blocked n" in text and "checkable with incremental-report" in text
    with pytest.raises(SystemExit):
        incremental_report([str(project), "--table-bytes", str(sizes)])
    assert report_text(advise_project(project)) and report_json([])["summary"]["candidate"] == 0
