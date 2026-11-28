"""Re-aggregated ratios: an AVG or SUM of a column that is itself a division (``kumosql.ratio_check``).

The fixture project follows the public ``wojoe-444/monorail-default-company`` repository: ``budget_variance`` computes
``budget_utilization_ratio = SAFE_DIVIDE(total_actual_cost_usd, budget_usd)`` per department and month, and
``department_scorecard`` rolls the on-time ratio up the right way, ``SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips))``
(its operations handbook: "sum both counts before dividing"). The other models re-aggregate a ratio wrongly, rightly, or
at the grain it was computed at.
"""

import pytest
from sqlglot import exp

from kumosql import mcp_server, ratio_check
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project

from test_mcp_server import ask, block_network, write_project

BOUND = 2000

FILES = {
    "sources.js": "\n".join(
        f'declare({{ schema: "raw", name: "{name}" }});' for name in ("costs", "route_days", "incidents", "jobs", "orders", "fx", "left_side", "right_side")
    ),
    "department_cost_monthly.sqlx": (
        'config { type: "table", schema: "finance_reporting" }\n'
        "SELECT report_month, department_id, SUM(cost_usd) AS total_actual_cost_usd, SUM(plan_usd) AS budget_usd\n"
        'FROM ${ref("raw", "costs")} GROUP BY report_month, department_id\n'
    ),
    "budget_variance.sqlx": (
        'config { type: "table", schema: "finance_reporting" }\n'
        "SELECT *, budget_usd - total_actual_cost_usd AS remaining_budget_usd,\n"
        "  SAFE_DIVIDE(total_actual_cost_usd, budget_usd) AS budget_utilization_ratio,\n"
        "  IF(total_actual_cost_usd > budget_usd, 'OverBudget', 'WithinBudget') AS budget_status\n"
        'FROM ${ref("finance_reporting", "department_cost_monthly")}\n'
    ),
    "budget_view.sqlx": (
        'config { type: "view", schema: "finance_reporting" }\n'
        "SELECT report_month, department_id, total_actual_cost_usd AS actual_usd, budget_usd AS plan_usd, budget_utilization_ratio AS utilization\n"
        'FROM ${ref("budget_variance")}\n'
    ),
    "route_performance_daily.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        "SELECT p.service_date, p.route_id, p.department_id, p.completed_trips, p.on_time_trips,\n"
        "  SAFE_DIVIDE(p.on_time_trips, p.completed_trips) AS on_time_ratio,\n"
        "  ROUND(SAFE_DIVIDE(p.on_time_trips, p.completed_trips) * 100, 1) AS on_time_pct\n"
        'FROM ${ref("raw", "route_days")} p\n'
    ),
    "route_share.sqlx": (
        'config { type: "view", schema: "ops_reporting" }\n'
        'WITH r AS (SELECT department_id, service_date, SAFE_DIVIDE(on_time_trips, completed_trips) AS pct, on_time_trips FROM ${ref("route_performance_daily")})\n'
        "SELECT department_id, service_date, pct AS on_time_share, on_time_trips FROM r\n"
    ),
    "department_scorecard.sqlx": (
        'config { type: "table", schema: "executive_reporting" }\n'
        "SELECT department_id, SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips)) AS on_time_ratio\n"
        'FROM ${ref("route_performance_daily")} GROUP BY department_id\n'
    ),
    "monthly_budget_report.sqlx": (
        'config { type: "table", schema: "finance_reporting" }\n'
        'SELECT report_month, AVG(budget_utilization_ratio) AS avg_utilization FROM ${ref("budget_variance")} GROUP BY report_month\n'
    ),
    "route_rollup.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        'WITH daily AS (SELECT service_date, department_id, IEEE_DIVIDE(on_time_trips, completed_trips) AS r FROM ${ref("route_performance_daily")})\n'
        "SELECT department_id, SUM(r) AS r_total FROM daily GROUP BY department_id\n"
    ),
    "budget_same_grain.sqlx": (
        'config { type: "table", schema: "finance_reporting" }\n'
        "SELECT report_month, department_id, AVG(budget_utilization_ratio) AS utilization\n"
        'FROM ${ref("budget_variance")} GROUP BY report_month, department_id\n'
    ),
    "route_same_hop.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        'SELECT department_id, AVG(on_time_trips / completed_trips) AS mean_ratio FROM ${ref("route_performance_daily")} GROUP BY 1\n'
    ),
    "weighted_right.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        "SELECT department_id, SUM(on_time_ratio * completed_trips) / SUM(completed_trips) AS weighted_on_time,\n"
        "  MIN(on_time_ratio) AS worst, MAX(on_time_ratio) AS best, ANY_VALUE(on_time_ratio) AS some\n"
        'FROM ${ref("route_performance_daily")} GROUP BY department_id\n'
    ),
    "incident_daily.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        "SELECT incident_date, AVG(resolution_minutes) AS avg_minutes, SUM(resolution_minutes) AS total_minutes,\n"
        '  COUNT(resolution_minutes) AS resolved FROM ${ref("raw", "incidents")} GROUP BY incident_date\n'
    ),
    "job_bytes.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        'SELECT day, SUM(bytes) / POWER(2, 40) AS tb, SUM(bytes) / 1024 AS kb FROM ${ref("raw", "jobs")} GROUP BY day\n'
    ),
    "orders_usd.sqlx": (
        'config { type: "table", schema: "sales" }\n'
        'SELECT o.order_id, o.order_day, o.amount / f.rate AS amount_usd FROM ${ref("raw", "orders")} o JOIN ${ref("raw", "fx")} f ON f.currency = o.currency\n'
    ),
    "blind_ratio.sqlx": (
        'config { type: "table", schema: "ops_reporting" }\n'
        'SELECT k, x / y AS xy FROM ${ref("raw", "left_side")} JOIN ${ref("raw", "right_side")} USING (k)\n'
    ),
}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    return mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path_factory.mktemp("ratios"), FILES))))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    block_network(monkeypatch)


def check(server, sql, **more):
    body, failed, used = ask(server, "check_query", sql=sql, **more)
    assert not failed and used <= BOUND, body
    return body


def ratios(body):
    return [item for item in body.get("findings", []) if item["check"] == "ratio"]


# ------------------------------------------------------------------ check_query: found upstream


def test_avg_of_a_ratio_computed_in_an_upstream_model_is_a_caution_with_the_ratio_of_sums(server):
    body = check(server, "SELECT report_month, AVG(budget_utilization_ratio) FROM finance_reporting.budget_variance GROUP BY report_month")
    [found] = ratios(body)
    assert found["level"] == "caution" and "ratio" in body["checked"]
    assert "AVG(budget_utilization_ratio) averages a ratio" in found["what"]
    assert "finance_reporting.budget_variance computes it as SAFE_DIVIDE(total_actual_cost_usd, budget_usd)" in found["what"]
    assert "an average of per-row ratios is not the ratio of the totals" in found["what"]
    assert found["fix"] == "Divide the sums instead: SAFE_DIVIDE(SUM(total_actual_cost_usd), SUM(budget_usd))."
    assert found["numerator"] == "total_actual_cost_usd" and found["denominator"] == "budget_usd"
    assert "exchange rate" not in found["fix"]  # both parts are sums upstream: not a unit conversion


def test_sum_of_a_ratio_is_a_caution_and_the_rewrite_keeps_the_alias(server):
    body = check(server, 'SELECT b.report_month, SUM(b.budget_utilization_ratio) FROM ${ref("budget_variance")} b GROUP BY 1')
    [found] = ratios(body)
    assert "SUM(b.budget_utilization_ratio) adds up a ratio" in found["what"] and "a sum of ratios" in found["what"]
    assert "SAFE_DIVIDE(SUM(b.total_actual_cost_usd), SUM(b.budget_usd))" in found["fix"]


def test_numerator_and_denominator_are_recovered_through_a_view_that_renames_them(server):
    body = check(server, "SELECT report_month, AVG(utilization) FROM finance_reporting.budget_view GROUP BY report_month")
    [found] = ratios(body)
    assert "finance_reporting.budget_variance computes budget_utilization_ratio" in found["what"]  # where it is computed, not the view
    assert found["numerator"] == "actual_usd" and found["denominator"] == "plan_usd"  # the view's own names for them
    assert "SAFE_DIVIDE(SUM(actual_usd), SUM(plan_usd))" in found["fix"]


def test_a_scaled_and_rounded_ratio_keeps_its_scale_in_the_rewrite(server):
    body = check(server, "SELECT department_id, AVG(on_time_pct) FROM ops_reporting.route_performance_daily GROUP BY department_id")
    [found] = ratios(body)
    assert "ROUND(SAFE_DIVIDE(p.on_time_trips, p.completed_trips) * 100, 1)" in found["what"]
    assert "SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips)) * 100" in found["fix"]
    assert found["numerator"] == "on_time_trips" and found["denominator"] == "completed_trips"


def test_an_average_of_an_upstream_average_is_found_with_its_sum_and_count(server):
    body = check(server, "SELECT AVG(avg_minutes) FROM ops_reporting.incident_daily")
    [found] = ratios(body)
    assert "an average, AVG(resolution_minutes)" in found["what"] and "average of averages" in found["what"]
    assert "SUM(total_minutes) / SUM(resolved)" in found["fix"]


# ------------------------------------------------------------------ check_query: CTEs and this hop


def test_a_ratio_computed_in_the_drafts_own_cte_is_found(server):
    body = check(server, "WITH r AS (SELECT department_id, SAFE_DIVIDE(on_time_trips, completed_trips) * 100 AS pct, on_time_trips, completed_trips "
                         "FROM ops_reporting.route_performance_daily) SELECT department_id, AVG(pct) FROM r GROUP BY department_id")
    [found] = ratios(body)
    assert "the draft's CTE r computes it as SAFE_DIVIDE(on_time_trips, completed_trips) * 100" in found["what"]
    assert found["numerator"] == "on_time_trips" and found["denominator"] == "completed_trips"
    assert "SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips)) * 100" in found["fix"]


def test_a_cte_that_drops_the_parts_names_them_unknown_and_says_where_the_ratio_divides(server):
    body = check(server, "WITH r AS (SELECT department_id, SAFE_DIVIDE(on_time_trips, completed_trips) AS pct FROM ops_reporting.route_performance_daily) "
                         "SELECT AVG(pct) FROM r")
    [found] = ratios(body)
    assert found["numerator"] == "unknown" and found["denominator"] == "unknown"
    assert "SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips)), where the draft's CTE r computes the ratio" in found["fix"]
    assert "no column of CTE r holding the numerator and denominator" in found["fix"]


def test_a_ratio_computed_in_a_models_cte_is_found_through_the_model(server):
    body = check(server, "SELECT department_id, AVG(on_time_share) FROM ops_reporting.route_share GROUP BY 1")
    [found] = ratios(body)
    assert "ops_reporting.route_share (CTE r) computes pct as SAFE_DIVIDE(on_time_trips, completed_trips)" in found["what"]
    # the model outputs the numerator but not the denominator: one part is recovered, the other is unknown
    assert found["numerator"] == "on_time_trips" and found["denominator"] == "unknown"
    assert "no column of ops_reporting.route_share holding the denominator" in found["fix"]
    # a SUM of ratios already summed by the model is a sum of sums here; the model itself is reported by project_checks
    assert ratios(check(server, "SELECT SUM(r_total) FROM ops_reporting.route_rollup")) == []


def test_a_ratio_computed_in_an_unaliased_subquery_is_found(server):
    body = check(server, 'SELECT department_id, AVG(r) FROM (SELECT department_id, IEEE_DIVIDE(on_time_trips, completed_trips) AS r, '
                         'on_time_trips, completed_trips FROM ${ref("route_performance_daily")}) GROUP BY 1')
    [found] = ratios(body)
    assert "the draft's a subquery computes it as IEEE_DIVIDE(on_time_trips, completed_trips)" in found["what"]
    assert "IEEE_DIVIDE(SUM(on_time_trips), SUM(completed_trips))" in found["fix"]


def test_a_division_written_inside_the_aggregate_counts_as_this_hop(server):
    body = check(server, "SELECT department_id, AVG(on_time_trips / completed_trips) FROM ops_reporting.route_performance_daily GROUP BY 1")
    [found] = ratios(body)
    assert "averages a per-row ratio" in found["what"]
    assert found["fix"].startswith("Divide the sums instead: SUM(on_time_trips) / SUM(completed_trips).")
    assert found["numerator"] == "on_time_trips" and found["denominator"] == "completed_trips"


def test_safe_divide_and_a_constant_factor_in_the_aggregate_are_peeled(server):
    body = check(server, "SELECT SUM(100 * SAFE_DIVIDE(on_time_trips, completed_trips)) FROM ops_reporting.route_performance_daily")
    [found] = ratios(body)
    assert "SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips)) * 100" in found["fix"]


# ------------------------------------------------------------------ not crying wolf


def test_a_correct_weighted_average_is_not_a_finding(server):
    body = check(server, "SELECT department_id, SUM(on_time_ratio * completed_trips) / SUM(completed_trips) FROM ops_reporting.route_performance_daily GROUP BY 1")
    assert ratios(body) == [] and "notes" not in body


def test_min_max_and_any_value_of_a_ratio_are_not_findings(server):
    body = check(server, "SELECT department_id, MIN(on_time_ratio), MAX(on_time_ratio), ANY_VALUE(on_time_ratio), COUNT(on_time_ratio) "
                         "FROM ops_reporting.route_performance_daily GROUP BY 1")
    assert ratios(body) == [] and "notes" not in body


def test_division_by_a_constant_and_a_moving_average_are_not_findings(server):
    body = check(server, "SELECT SUM(tb), AVG(kb) FROM ops_reporting.job_bytes")
    assert ratios(body) == []
    body = check(server, "SELECT service_date, AVG(on_time_ratio) OVER (ORDER BY service_date ROWS 6 PRECEDING) FROM ops_reporting.route_performance_daily")
    assert ratios(body) == []


def test_the_same_grain_is_only_a_note(server):
    body = check(server, "SELECT report_month, department_id, AVG(budget_utilization_ratio) FROM finance_reporting.budget_variance GROUP BY report_month, department_id")
    assert ratios(body) == [] and body["verdict"] == "no_problems_found"
    [note] = body["notes"]
    assert "one row per (department_id, report_month)" in note and "the AVG is the ratio itself" in note
    assert "SAFE_DIVIDE(SUM(total_actual_cost_usd), SUM(budget_usd))" in note
    # grouping by position and dropping one grain column changes the grain again
    body = check(server, "SELECT report_month, AVG(budget_utilization_ratio) FROM finance_reporting.budget_variance GROUP BY 1")
    assert len(ratios(body)) == 1 and "notes" not in body


def test_a_division_by_a_per_row_value_says_it_may_be_a_unit_conversion(server):
    body = check(server, "SELECT order_day, SUM(amount_usd) FROM sales.orders_usd GROUP BY order_day")
    [found] = ratios(body)
    assert "If the division converts a unit, such as an exchange rate, the SUM is right" in found["fix"]


# ------------------------------------------------------------------ unknown lineage


def test_parts_lineage_cannot_trace_are_unknown_and_the_ratio_is_still_shown(server):
    body = check(server, "SELECT AVG(xy) FROM ops_reporting.blind_ratio")
    [found] = ratios(body)
    assert "ops_reporting.blind_ratio computes it as x / y" in found["what"]
    assert found["numerator"] == "unknown" and found["denominator"] == "unknown"
    assert "SUM(x) / SUM(y), where ops_reporting.blind_ratio computes the ratio" in found["fix"]


def test_a_ratio_whose_parts_are_aggregated_upstream_is_rewritten_from_where_it_is_computed(server):
    body = check(server, "SELECT AVG(on_time_ratio) FROM executive_reporting.department_scorecard")
    [found] = ratios(body)
    assert found["numerator"] == "unknown"
    assert "SAFE_DIVIDE(SUM(on_time_trips), SUM(completed_trips)), where executive_reporting.department_scorecard computes the ratio" in found["fix"]


def test_an_argument_that_cannot_be_followed_is_listed_as_not_checked(server):
    body = check(server, "SELECT SUM(v) FROM (SELECT a AS v FROM raw.costs UNION ALL SELECT b FROM raw.costs)")
    assert any("whether 1 AVG/SUM argument is a ratio: SUM(v)" in item["what"] and "UNION" in item["reason"] for item in body["unknown"])
    # a column of a declared source is a stored value: nothing to say
    body = check(server, "SELECT SUM(cost_usd) FROM raw.costs")
    assert not any("AVG/SUM argument" in item["what"] for item in body.get("unknown", []))


def test_a_failure_of_the_check_is_reported_and_the_other_checks_still_answer(server, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ratio_check, "_check_draft", broken)
    body = check(server, "SELECT AVG(budget_utilization_ratio) FROM finance_reporting.budget_variance")
    assert any(item["what"] == "re-aggregated ratios" and "failed (RuntimeError)" in item["reason"] for item in body["unknown"])
    assert "ties" in body["checked"]


# ------------------------------------------------------------------ project_checks


def test_project_checks_lists_models_that_reaggregate_an_upstream_ratio(server):
    body, failed, used = ask(server, "project_checks", check="ratio_reaggregated")
    assert not failed and used <= BOUND
    [group] = body["findings"]
    assert group["check"] == "ratio_reaggregated" and group["count"] == 2 and "caution" in group["what"]
    lines = group["examples"]
    assert any(line.startswith("finance_reporting.monthly_budget_report: AVG(budget_utilization_ratio) should be "
                               "SAFE_DIVIDE(SUM(total_actual_cost_usd), SUM(budget_usd))") for line in lines)
    assert any(line.startswith("ops_reporting.route_rollup: SUM(r) re-aggregates a ratio whose parts it does not have; the ratio is from CTE daily")
               for line in lines)
    text = " ".join(lines)
    for fine in ("department_scorecard", "budget_same_grain", "route_same_hop", "weighted_right", "job_bytes"):
        assert fine not in text  # done right, the same grain, a ratio written in the aggregate itself, a division by a constant
    assert "SUM(numerator) / SUM(denominator)" in group["fix"]


def test_project_checks_counts_the_ratio_check_with_the_others(server):
    body, _, used = ask(server, "project_checks")
    assert used <= BOUND and body["counts"]["ratio_reaggregated"] == 2 and "ratio_reaggregated" in body["checked"]


def test_project_findings_name_the_model_the_ratio_comes_from(server):
    found = {item.table: item for item in ratio_check.project_findings(server.session)}
    assert set(found) == {"p.finance_reporting.monthly_budget_report", "p.ops_reporting.route_rollup"}
    assert found["p.finance_reporting.monthly_budget_report"].related == ("p.finance_reporting.budget_variance",)
    assert found["p.ops_reporting.route_rollup"].related == ()  # computed in its own CTE


# ------------------------------------------------------------------ answer size


def test_aggregates_of_one_ratio_are_one_finding(server):
    variants = ["AVG({c})", "SUM({c})", "AVG(ROUND({c}, 2))", "AVG(COALESCE({c}, 0))", "SUM(CAST({c} AS FLOAT64))",
                "AVG(SAFE_CAST({c} AS NUMERIC))", "AVG(IF(department_id > 0, {c}, NULL))", "SUM(TRUNC({c}, 3))"]
    columns = ", ".join(v.format(c="budget_utilization_ratio") for v in variants)
    body = check(server, f"SELECT report_month, {columns} FROM finance_reporting.budget_variance GROUP BY report_month")
    [found] = ratios(body)
    assert body["counts"]["ratio"] == 1
    assert found["what"].startswith("AVG(budget_utilization_ratio), SUM(budget_utilization_ratio) and 6 more re-aggregate a ratio")
    # a scale changes the rewrite, so it is its own finding
    body = check(server, "SELECT AVG(budget_utilization_ratio), AVG(budget_utilization_ratio * 100) FROM finance_reporting.budget_variance")
    assert body["counts"]["ratio"] == 2


def test_many_ratio_findings_stay_within_the_answer_bound_and_page(server):
    reads = ["AVG(budget_utilization_ratio) FROM finance_reporting.budget_variance", "AVG(on_time_ratio) FROM ops_reporting.route_performance_daily",
             "AVG(on_time_pct) FROM ops_reporting.route_performance_daily", "AVG(utilization) FROM finance_reporting.budget_view",
             "AVG(avg_minutes) FROM ops_reporting.incident_daily", "AVG(xy) FROM ops_reporting.blind_ratio",
             "AVG(on_time_share) FROM ops_reporting.route_share", "SUM(amount_usd) FROM sales.orders_usd",
             "AVG(on_time_ratio) FROM executive_reporting.department_scorecard"]
    sql = "SELECT " + ", ".join(f"(SELECT {read}) AS v{index}" for index, read in enumerate(reads))
    body = check(server, sql)
    assert body["counts"]["ratio"] == len(reads)
    assert 0 < len(body["findings"]) < len(reads) and "findings_shown" in body and "offset=" in body["more"]
    following = check(server, sql, offset=len(body["findings"]))
    assert ratios(following) and ratios(following)[0]["what"] not in {item["what"] for item in body["findings"]}


def test_project_checks_stays_within_the_bound_with_many_findings(tmp_path):
    files = {"sources.js": 'declare({ schema: "raw", name: "costs" });',
             "base.sqlx": 'config { type: "table" }\nSELECT region, day, SAFE_DIVIDE(SUM(spend), SUM(plan)) AS util FROM ${ref("raw", "costs")} GROUP BY region, day\n'}
    for index in range(25):
        files[f"report_{index:02d}_with_a_rather_long_model_name.sqlx"] = (
            f'config {{ type: "table" }}\nSELECT region, AVG(util) AS u{index} FROM ${{ref("base")}} GROUP BY region\n')
    many = mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path, files))))
    body, failed, used = ask(many, "project_checks")
    assert not failed and used <= BOUND and body["counts"]["ratio_reaggregated"] == 25
    [group] = [g for g in body["findings"] if g["check"] == "ratio_reaggregated"]
    assert group["count"] == 25 and len(group["examples"]) == 3


# ------------------------------------------------------------------ meanings


@pytest.mark.parametrize("meaning, parts", [
    ("expr:SAFE_DIVIDE(agg:SUM(col:p.raw.costs.cost), agg:SUM(col:p.raw.costs.budget))",
     ("agg:SUM(col:p.raw.costs.cost)", "agg:SUM(col:p.raw.costs.budget)")),
    ("expr:ROUND(col:p.raw.t.a / col:p.raw.t.b * 100, 1)", ("col:p.raw.t.a", "col:p.raw.t.b")),
    ("expr:COALESCE(col:p.raw.t.a / NULLIF(col:p.raw.t.b, 0), 0)", ("col:p.raw.t.a", "col:p.raw.t.b")),
    ("agg:AVG(col:p.raw.t.m)", ("agg:SUM(col:p.raw.t.m)", "agg:COUNT(col:p.raw.t.m)")),
    ("expr:IEEE_DIVIDE(field(col:p.raw.t.s, a.b), agg:COUNT(*))", ("field(col:p.raw.t.s, a.b)", "agg:COUNT(*)")),
    ("expr:col:p.raw.t.a / 100", None),
    ("expr:1 / col:p.raw.t.a", None),
    ("agg:AVG(DISTINCT col:p.raw.t.m)", None),
    ("col:p.raw.t.a", None),
    (None, None),
])
def test_ratio_parts_split_a_meaning_at_its_division(meaning, parts):
    assert ratio_check.ratio_parts(meaning) == parts


def test_a_part_matches_a_column_meaning_whatever_its_spelling():
    assert ratio_check.normal_meaning("expr:(col:p.raw.t.a + col:p.raw.t.b)") == ratio_check.normal_meaning("expr:col:p.raw.t.a + col:p.raw.t.b")
    assert ratio_check.normal_meaning("agg:SUM(col:p.raw.t.a)") == "agg:SUM(col:p.raw.t.a)"


@pytest.mark.parametrize("sql, ratio", [
    ("a / b", True), ("SAFE_DIVIDE(a, b)", True), ("IEEE_DIVIDE(a, b)", True), ("ROUND(a / b * 100, 1)", True),
    ("CAST(a / b AS NUMERIC)", True), ("COALESCE(SAFE_DIVIDE(a, b), 0)", True), ("IF(c, a / b, NULL)", True), ("a / NULLIF(b, 0)", True),
    ("a / 100", False), ("a / POWER(2, 40)", False), ("a / (SELECT COUNT(*) FROM t)", False), ("a / SUM(a) OVER ()", False),
    ("1 / a", False), ("a * b", False), ("IF(c, a / b, 0)", False), ("SUM(a) / COUNT(*)", True),
])
def test_what_counts_as_a_ratio(sql, ratio):
    import sqlglot

    node = sqlglot.parse_one(sql, read="bigquery")
    assert (ratio_check._division(ratio_check._peel(node)[0]) is not None) is ratio
    assert isinstance(node, exp.Expression)
