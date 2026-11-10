import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import cost_validity_bench as cvb  # noqa: E402

pytestmark = pytest.mark.eval


def test_rule_recommendation_compares_against_the_round_tripped_original():
    rec = cvb.rule_recommendation("WITH unused AS (SELECT 1 AS x) SELECT a FROM t WHERE 1 = 1")
    assert rec["steps"] and "remove_unused_ctes" in rec["steps"]
    assert "unused" in rec["baseline"].lower() and "unused" not in rec["sql"].lower()
    assert rec["label"] == "proven"


def test_rule_recommendation_is_empty_when_nothing_changes():
    assert cvb.rule_recommendation("SELECT a FROM t") == {}


def test_load_workload_names_each_statement(tmp_path):
    (tmp_path / "q1.sql").write_text("SELECT 1;")
    (tmp_path / "q2.sql").write_text("SELECT 1; SELECT 2;")
    names = [q["name"] for q in cvb.load_workload(f"db={tmp_path}")]
    assert names == ["db/q1", "db/q2a", "db/q2b"]


def test_summary_keeps_estimates_and_observations_apart():
    def rec(outcome, estimate, speedup):
        return {"sql": "x", "label": "proven", "outcome": outcome, "estimate": estimate, "speedup": speedup}

    records = [
        {"rules": rec("same_rows", [100.0, 50.0], 2.0)},  # predicted and realized
        {"rules": rec("same_rows", [100.0, 50.0], 1.0)},  # predicted, not realized
        {"rules": rec("same_rows", [100.0, 100.0], 1.5)},  # faster without a predicted saving
        {"rules": rec("different_rows", [100.0, 10.0], 9.0)},  # wrong rows never count as a saving
        {"rules": {}},
    ]
    s = cvb.summarize(records, "rules")
    assert s["recommendations"] == 4 and s["same_rows"] == 3 and s["different_rows"] == 1
    assert s["estimated_cheaper"] == 2 and s["estimated_unchanged"] == 1
    assert s["observed_faster"] == 2 and s["estimated_cheaper_and_observed_faster"] == 1


def test_summary_counts_proof_and_agreement_apart():
    def rec(label, outcome):
        return {"sql": "x", "label": label, "outcome": outcome, "estimate": [1.0, 1.0], "speedup": 1.0}

    records = [
        {"rules": rec("proven", "same_rows")},
        {"rules": rec("unproven", "same_rows")},
        {"rules": rec("unproven", "baseline_error")},
        {"rules": rec("proven", "different_schema")},
    ]
    s = cvb.summarize(records, "rules")
    assert (s["proven"], s["unproven"], s["unproven_judged"]) == (2, 2, 1)
    assert (s["judged"], s["unjudged"], s["same_rows"], s["different_schema"]) == (3, 1, 2, 1)


def test_rows_tied_on_the_sort_keys_may_come_back_in_any_order():
    sql = "SELECT k, v FROM t ORDER BY k LIMIT 10"
    assert cvb.order_key_positions(sql) == [0]
    assert cvb.same_rows([(1, "a"), (1, "b"), (2, "c")], [(1, "b"), (1, "a"), (2, "c")], sql)
    assert not cvb.same_rows([(1, "a"), (2, "c")], [(2, "c"), (1, "a")], sql)
    assert not cvb.same_rows([(1, "a")], [(1, "b")], sql)


# The comparator on small pairs. ``judge`` is the real function; the executor is DuckDB standing in for
# PostgreSQL, over t(k, x, g) with k unique and g shared by two rows each.


class _DuckExecutor:
    """The part of ``rewrite_bench.Executor`` that ``judge`` uses."""

    def __init__(self):
        import duckdb

        self.con = duckdb.connect()
        self.con.execute("CREATE TABLE t(k INTEGER, x INTEGER, g INTEGER)")
        self.con.execute("INSERT INTO t VALUES (10, 1, 1), (20, 2, 1), (30, 3, 2), (40, 4, 2)")
        self.statements = []

    def _connection(self, database):
        return self

    def execute(self, sql):  # there is no EXPLAIN (FORMAT JSON) here, so the estimates come back as None
        raise RuntimeError("no EXPLAIN")

    def run(self, database, sql):
        self.statements.append(sql)
        try:
            cur = self.con.execute(sql.strip().rstrip(";"))
            return {"ok": True, "rows": cur.fetchall(), "ms": 1.0, "schema": [[d[0], str(d[1])] for d in cur.description]}
        except Exception as error:  # noqa: BLE001 - a database error is a result
            return {"ok": False, "error": str(error)}

    def compare(self, database, benchmark, rewrite):
        b, r = self.run(database, benchmark), self.run(database, rewrite)
        if not b["ok"]:
            return {"benchmark_error": b["error"]}
        if not r["ok"]:
            return {"rewrite_error": r["error"], "benchmark_rows": b["rows"]}
        return {
            "benchmark_rows": b["rows"],
            "rewrite_rows": r["rows"],
            "benchmark_schema": b["schema"],
            "rewrite_schema": r["schema"],
            "benchmark_ms": 1.0,
            "rewrite_ms": 1.0,
        }


def _judge(baseline, sql):
    return cvb.judge(_DuckExecutor(), "db", baseline, sql)


def _outcome(baseline, sql):
    return _judge(baseline, sql)["outcome"]


def test_reversed_order_on_a_hidden_unique_key_is_different():
    # The sort key k is not projected, so the bag of x values is identical; only the order differs.
    assert _outcome("SELECT x FROM t ORDER BY k ASC", "SELECT x FROM t ORDER BY k DESC") == "different_rows"
    assert _outcome("SELECT x FROM t ORDER BY k ASC", "SELECT x FROM t ORDER BY k") == "same_rows"
    assert _outcome("SELECT x FROM t ORDER BY k + 1 DESC", "SELECT x FROM t ORDER BY k + 1 ASC") == "different_rows"
    assert _outcome("SELECT x FROM t ORDER BY k DESC LIMIT 2", "SELECT x FROM t ORDER BY k ASC LIMIT 2") == "different_rows"
    assert _outcome("SELECT * FROM t ORDER BY k", "SELECT * FROM t ORDER BY k DESC") == "different_rows"


def test_the_judge_records_what_the_comparison_covered():
    record = _judge("SELECT x FROM t ORDER BY k", "SELECT x FROM t ORDER BY k")
    assert record["outcome"] == "same_rows"
    assert record["agreement_basis"] == {
        "rows": "same bag of rows",
        "order": "hidden sort keys appended",
        "schema": "output column names and types",
        "float_places": 9,
    }
    assert [name for name, _ in record["schema"]] == ["x"]
    assert _judge("SELECT x FROM t", "SELECT x FROM t")["agreement_basis"]["order"] == "unordered"
    assert _judge("SELECT k FROM t ORDER BY k", "SELECT k FROM t ORDER BY k")["agreement_basis"]["order"] == "output sort keys"


def test_hidden_sort_keys_are_appended_for_the_check():
    sql, positions, hidden = cvb.with_sort_keys("SELECT x, k AS kk FROM t ORDER BY kk DESC, x, g")
    assert (positions, hidden) == ([1, 0, -1], 1)
    assert "g AS _kumo_sort_0" in sql and sql.endswith("ORDER BY kk DESC, x, g")
    # A qualified name is the input column, not the output alias, so it is hidden even when the same value is projected.
    assert cvb.with_sort_keys("SELECT x, k AS kk FROM t ORDER BY t.k")[1:] == ([-1], 1)
    # Every key already projected: the statement is left alone.
    assert cvb.with_sort_keys("SELECT x, k FROM t ORDER BY k") == ("SELECT x, k FROM t ORDER BY k", [1], 0)


def test_hidden_keys_are_not_added_where_that_would_change_the_result():
    assert cvb.with_sort_keys("SELECT DISTINCT x FROM t ORDER BY k") is None
    assert cvb.with_sort_keys("SELECT x FROM t UNION ALL SELECT x FROM t ORDER BY 1") is None
    assert cvb.with_sort_keys("SELECT x FROM t") is None
    # The order is then not checked, and the record says so.
    record = _judge("SELECT DISTINCT x FROM t ORDER BY k", "SELECT DISTINCT x FROM t ORDER BY k DESC")
    assert record["outcome"] == "same_rows" and record["agreement_basis"]["order"].startswith("bag only")


def test_a_hidden_key_is_matched_with_its_row():
    # Same x values and the same sequence of g, but x is attached to another k: the rows differ.
    assert _outcome("SELECT x FROM t ORDER BY g, k", "SELECT x FROM t ORDER BY g, k DESC") == "different_rows"


def test_rows_tied_on_a_hidden_key_may_come_back_in_any_order():
    executor = _DuckExecutor()
    # g ties for two rows each: a rewrite may order them by an extra key, and tied rows may swap.
    assert cvb.sort_keys_agree(executor, "db", "SELECT x FROM t ORDER BY g", "SELECT x FROM t ORDER BY g, k DESC") == (True, "hidden sort keys appended")
    assert cvb.sort_keys_agree(executor, "db", "SELECT x FROM t ORDER BY g", "SELECT x FROM t ORDER BY g DESC")[0] is False
    assert cvb.sort_keys_agree(executor, "db", "SELECT x FROM t ORDER BY g, k", "SELECT x FROM t ORDER BY g")[1].startswith("bag only")


def test_a_renamed_output_column_is_a_different_schema():
    record = _judge("SELECT 1 AS old_name", "SELECT 1 AS new_name")
    assert record["outcome"] == "different_schema"
    assert record["schema"][0][0] == "old_name" and record["rewrite_schema"][0][0] == "new_name"
    assert _outcome("SELECT x FROM t", "SELECT x AS x FROM t") == "same_rows"


def test_ordinary_differences_are_still_different():
    assert _outcome("SELECT x FROM t", "SELECT x + 1 AS x FROM t") == "different_rows"
    assert _outcome("SELECT x FROM t", "SELECT x FROM t WHERE x > 1") == "different_rows"
    assert _outcome("SELECT k FROM t ORDER BY k ASC", "SELECT k FROM t ORDER BY k DESC") == "different_rows"
    assert _outcome("SELECT k FROM t", "SELECT k FROM t UNION ALL SELECT k FROM t") == "different_rows"
    assert _outcome("SELECT x FROM t", "SELECT nope FROM t") == "rewrite_error"


def test_documented_limits_of_the_comparator():
    # Floats compare rounded to 9 decimal places: differences below about 5e-10 are not seen.
    assert cvb.FLOAT_PLACES == 9
    assert _outcome("SELECT CAST(1.0000000001 AS DOUBLE) AS v", "SELECT CAST(1.0000000002 AS DOUBLE) AS v") == "same_rows"
    assert _outcome("SELECT CAST(1.001 AS DOUBLE) AS v", "SELECT CAST(1.002 AS DOUBLE) AS v") == "different_rows"
    # Agreement is on one dataset: no row has k in 11..19, so these differ in meaning but not here.
    assert _outcome("SELECT x FROM t WHERE k = 10", "SELECT x FROM t WHERE k < 15") == "same_rows"


# The advisor part: store_view and unstore_table recommendations on synthetic projects, executed in DuckDB.
# Each case is built and run inside its own test, so the tests share nothing and can run in parallel.


def _by_id(records):
    return {r["id"]: r for r in records}


def _advise_case(name):
    import cost_validity_projects as projects

    case = next(c for c in projects.cases() if c.name == name)
    records = cvb.advisor_records(case)
    return _by_id(records), cvb.summarize_advisor(records)


def test_advisor_retail_stores_the_hot_aggregate_and_refuses_the_clock_view():
    rec, summary = _advise_case("retail")
    daily = rec["store:p.core.daily"]
    assert (daily["label"], daily["recommended"], daily["chosen"], daily["outcome"]) == ("proven", True, True, "same_rows")
    assert daily["saving_per_day"] > 0 and daily["reads_compared"] > 0
    # Un-storing a table that nothing reads is proven, recommended and agrees.
    mart = rec["unstore:p.core.mart"]
    assert (mart["label"], mart["recommended"], mart["outcome"]) == ("proven", True, "same_rows")
    # A view reading CURRENT_DATE is refused, and the execution shows why: the table keeps yesterday's date until the refresh.
    clock = rec["store:p.core.recent"]
    assert (clock["label"], clock["recommended"], clock["chosen"]) == ("changes_results", False, False)
    assert clock["outcome"] == "different_rows" and clock["first_difference"]["phase"] == "between runs"
    # A view over a declared source is conditional: listed with its conditions, never counted, and stale between runs.
    direct = rec["store:p.core.direct"]
    assert (direct["label"], direct["recommended"], direct["chosen"]) == ("conditional", False, False)
    assert direct["conditions"] and direct["outcome"] == "different_rows"
    # The chosen set runs together without a difference, and only proven changes are in it.
    selection = rec["selection"]
    assert selection["members"] == ["store:p.core.daily", "unstore:p.core.mart"] and selection["outcome"] == "same_rows"
    assert selection["counted_unproven"] == [] and "store:p.core.direct" in selection["listed_as_needs_proof"]
    assert summary["wrong"] == 0 and summary["unproven_counted_as_saving"] == 0


def test_advisor_sampling_refuses_random_and_uuid_views_even_through_a_view():
    rec, summary = _advise_case("sampling")
    for node in ("sample", "tagged", "tagged_totals"):
        row = rec[f"store:p.stage.{node}"]
        assert (row["label"], row["recommended"], row["chosen"]) == ("changes_results", False, False), node
        assert row["outcome"] == "different_rows", node
    assert "GENERATE_UUID" in rec["store:p.stage.tagged_totals"]["reason"]  # found by expanding the view it reads
    assert rec["store:p.stage.per_session"]["recommended"] and rec["store:p.stage.per_session"]["outcome"] == "same_rows"
    # A hot view over a proven view is proven too but does not pay for its own refresh: not recommended.
    busy = rec["store:p.stage.busy"]
    assert (busy["label"], busy["recommended"], busy["outcome"]) == ("proven", False, "same_rows")
    assert summary["wrong"] == 0 and summary["refused"] == 3 and summary["refused_difference_seen"] == 3


def test_advisor_staleness_never_counts_an_unproven_saving():
    rec, summary = _advise_case("staleness")
    facts = rec["store:p.sales.order_facts"]
    # The biggest saving of the project, but its inputs follow different schedules: it needs proof.
    assert facts["label"] == "conditional" and facts["saving_per_day"] > rec["store:p.sales.revenue_by_customer"]["saving_per_day"]
    assert not facts["recommended"] and not facts["chosen"] and facts["conditions"]
    selection = rec["selection"]
    assert "store:p.sales.order_facts" not in selection["members"]
    assert "store:p.sales.order_facts" in selection["listed_as_needs_proof"] and selection["counted_unproven"] == []
    assert rec["unstore:p.sales.rollup"]["recommended"] and rec["unstore:p.sales.rollup"]["outcome"] == "same_rows"
    # Un-storing a table that reads a source (stale until refreshed) or stamps a clock changes what readers see.
    for node, label in (("order_copy", "conditional"), ("snapshot", "changes_results")):
        row = rec[f"unstore:p.sales.{node}"]
        assert (row["label"], row["recommended"], row["outcome"]) == (label, False, "different_rows"), node
    assert summary["wrong"] == 0 and summary["candidates"] == 7


def test_the_executed_comparison_does_see_a_change_that_alters_results():
    import cost_validity_projects as projects

    case = projects.retail()
    stored = cvb.execute_change(case, store=frozenset(["p.core.recent"]))
    assert stored["outcome"] == "different_rows" and stored["differences"] > 0
    assert cvb.execute_change(case, store=frozenset(["p.core.daily"]))["outcome"] == "same_rows"
    # Nothing changed: the two worlds are the same project.
    none = cvb.execute_change(case)
    assert none["outcome"] == "same_rows" and none["reads_compared"] == 60


def test_advisor_summary_counts_a_wrong_proven_change_and_an_unproven_saving():
    def single(id, label, outcome, recommended=False):
        return {"case": "c", "id": id, "kind": "store_view", "label": label, "outcome": outcome, "recommended": recommended, "chosen": False, "reads_compared": 3}

    def selection(members, outcome="same_rows", counted=()):
        return {"case": "c", "id": "selection", "kind": "selection", "members": members, "outcome": outcome, "reads_compared": 3, "counted_unproven": list(counted)}

    good = [single("a", "proven", "same_rows", True), single("b", "conditional", "different_rows"), single("c", "changes_results", "different_rows"), selection(["a"])]
    s = cvb.summarize_advisor(good)
    assert (s["wrong"], s["recommended"], s["needs_proof"], s["needs_proof_difference_seen"], s["refused_difference_seen"]) == (0, 1, 1, 1, 1)
    # A not-proven change that differs is the expected refusal. A proven one that differs is wrong, as is a chosen set that differs.
    assert cvb.summarize_advisor([single("a", "proven", "different_rows", True), selection(["a"])])["wrong"] == 1
    assert cvb.summarize_advisor([single("a", "proven", "same_rows", True), selection(["a"], "different_schema")])["wrong"] == 1
    assert cvb.summarize_advisor([single("a", "proven", "rewrite_error")])["wrong"] == 1
    # Counting a conditional change as a saving is wrong even if it executes identically.
    assert cvb.summarize_advisor([single("b", "conditional", "same_rows"), selection(["b"], counted=["b"])])["wrong"] == 1
    # A difference that DuckDB's unoptimized run does not reproduce is not judged, either way.
    odd = cvb.summarize_advisor([single("a", "proven", "optimizer_disagrees", True)])
    assert (odd["wrong"], odd["not_judged"]) == (0, 1)


def test_duckdb_sql_flattens_names_keeps_aliases_and_fixes_the_clock():
    from datetime import datetime, timezone

    import cost_validity_projects as projects

    pipeline = projects.staleness().pipeline()
    sql = "SELECT o.id, CURRENT_DATE() AS d, CURRENT_TIMESTAMP() AS t FROM `p.sales.orders_clean` AS o"
    out = cvb.duckdb_sql(pipeline, sql, datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc))
    assert "p__sales__orders_clean" in out and "AS o" in out
    assert "CAST('2026-09-03' AS DATE)" in out and "CAST('2026-09-03 12:30:00' AS TIMESTAMP)" in out
    assert "CURRENT_" not in out.upper()
