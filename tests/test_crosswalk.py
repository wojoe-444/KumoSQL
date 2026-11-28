"""The crosswalk: equivalence entries with a status, looking them up, and suggesting new ones from column meanings."""

import json
import shlex

import pytest
import sqlglot

from kumosql import equivalences, load_sqlx_project, state
from kumosql.crosswalk import select_tables, suggest_equivalences
from kumosql.equivalences import Equivalence
from kumosql.pipeline_equivalence import equivalence_main


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def two_versions(root):
    """An old sandbox (dataset ``old``) and its rebuild (dataset ``mart``) over the same source, columns renamed."""

    write(root, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(root, "definitions/src.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(root, "definitions/old/o1.sqlx", 'config { type: "table", schema: "old" }\n'
          'SELECT id AS order_key, amt, customer_id AS cust, UPPER(status) AS st, ts FROM ${ref("raw","orders")}\n')
    write(root, "definitions/old/o2.sqlx", 'config { type: "table", schema: "old" }\n'
          'SELECT customer_id, SUM(amt) AS revenue, COUNT(*) AS n, MAX(CASE WHEN status = "x" THEN amt END) AS mx,\n'
          '  SUM(amt * 3) AS triple FROM ${ref("raw","orders")} GROUP BY customer_id\n')
    write(root, "definitions/new/n1.sqlx", 'config { type: "table", schema: "mart" }\n'
          'SELECT id, amt AS amount, customer_id, upper(status) AS status_up, DATE(ts) AS d FROM ${ref("raw","orders")}\n')
    write(root, "definitions/new/n2.sqlx", 'config { type: "table", schema: "mart" }\n'
          'SELECT customer_id AS cid, SUM(amt) AS total, COUNT(*) AS cnt, MAX(CASE WHEN status = "x" THEN amt END) AS biggest,\n'
          '  SUM(amt * 2) AS dbl FROM ${ref("raw","orders")} GROUP BY customer_id\n')
    return load_sqlx_project(root)


def suggested(result, left, right):
    return next(s for s in result["suggestions"] if (s["left_table"], s["right_table"]) == (left, right))


def pairs(suggestion):
    return {(p["left_column"], p["right_column"]): p for p in suggestion["matched_columns"]}


def store_files():
    return {name: state.data_path(name) for name in (equivalences.EQUAL_FILE, equivalences.CROSSWALK_FILE)}


# --------------------------------------------------------------------- the store


def test_entries_keep_status_explanation_evidence_and_date_and_round_trip():
    equal = equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]],
                              "evidence": "https://example.test/check", "verified_at": "2026-11-20"})
    differs = equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["ts", "ts_est"]], "status": "differs",
                                "explanation": "timezone_utc_vs_est", "evidence": "SELECT COUNT(*) FROM a JOIN b USING (id)\nWHERE a.ts != b.ts_est"})
    column = equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["legacy", None]], "status": "missing"})
    other_side = equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["", "extra"]], "status": "MISSING"})
    table = equivalences.add({"left": "p.d.old_report", "right": "-", "status": "missing", "verified_at": "2026-11-21"})

    assert (equal.status, equal.explanation, equal.verified_at) == ("equal", None, "2026-11-20")
    assert (differs.status, differs.explanation) == ("differs", "timezone_utc_vs_est")
    assert column.columns == (("legacy", None),) and other_side.columns == ((None, "extra"),)
    assert (table.left, table.right, table.columns) == ("p.d.old_report", None, ())
    assert table.label == "p.d.old_report has no counterpart"
    assert column.label == "p.d.a.legacy has no counterpart in p.d.b"
    assert differs.label == "p.d.a differs from p.d.b (ts vs ts_est) [timezone_utc_vs_est]"

    assert equivalences.load() == [equal]  # what a proof may assume
    assert equivalences.load_all() == [equal, differs, column, other_side, table]
    files = store_files()
    assert [i["status"] for i in json.loads(files["equivalences.json"].read_text(encoding="utf-8"))] == ["equal"]
    records = json.loads(files["crosswalk.json"].read_text(encoding="utf-8"))
    assert [i["status"] for i in records] == ["differs", "missing", "missing", "missing"]
    for item in equivalences.load_all():
        assert equivalences.parse(json.loads(json.dumps(item.to_json()))) == item


def test_a_file_written_before_statuses_reads_as_equal_declarations():
    store_files()["equivalences.json"].write_text(
        json.dumps([{"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "whole": False}]), encoding="utf-8")
    assert equivalences.load() == [Equivalence("p.d.a", "p.d.b", (("x", "y"),))]
    assert equivalences.load()[0].status == "equal"


def test_a_difference_written_into_the_proof_file_by_hand_is_not_assumed():
    store_files()["equivalences.json"].write_text(json.dumps([
        {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "differs"},
        {"left": "p.d.a", "right": "p.d.c", "columns": [["x", "y"]]},
    ]), encoding="utf-8")
    assert [i.right for i in equivalences.load()] == ["p.d.c"]


@pytest.mark.parametrize("bad", [
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "maybe"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": 1},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "verified_at": "2026-02-30"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "verified_at": "2026/01/01"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "verified_at": "26-1-1"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "verified_at": 20260101},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "explanation": "an equal pair has no known difference"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "differs", "explanation": "two\nlines"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "differs", "explanation": "r" * 121},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "evidence": "e" * 4001},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "evidence": 5},
    {"left": "p.d.a", "right": "p.d.b", "status": "differs", "whole": True},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "differs", "whole": True},
    {"left": "-", "right": None, "status": "missing"},
    {"left": "p.d.a", "right": "p.d.b", "status": "missing"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "missing"},
    {"left": "p.d.a", "right": "p.d.b", "columns": [["", None]], "status": "missing"},
    {"left": "p.d.a", "right": None, "columns": [["x", None]], "status": "missing"},
    {"left": "p.d.a", "right": "-", "columns": [["x", "y"]]},  # "-" is no table outside a missing entry
    {"left": "p.d.a", "right": "p.d.b", "columns": [["x", ""]]},  # nor an empty column
])
def test_bad_entries_are_refused_and_nothing_is_saved(bad):
    with pytest.raises(ValueError):
        equivalences.add(bad)
    assert equivalences.load_all() == []


def test_contradicting_or_repeated_entries_are_refused():
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["id", "order_id"]], "whole": True})
    for contradiction in (
        {"left": "p.d.a", "right": "p.d.b", "columns": [["ts", "ts"]], "status": "differs"},  # whole says every column is equal
        {"left": "p.d.b", "right": "p.d.a", "columns": [["ts", "ts"]], "status": "differs"},  # in either direction
        {"left": "p.d.a", "right": "p.d.b", "columns": [["extra", None]], "status": "missing"},
        {"left": "p.d.b", "right": "-", "status": "missing"},  # p.d.b has a counterpart
    ):
        with pytest.raises(ValueError):
            equivalences.add(contradiction)
    equivalences.add({"left": "p.d.c", "right": "p.d.d", "columns": [["x", "y"]]})
    equivalences.add({"left": "p.d.c", "right": "p.d.d", "columns": [["ts", "ts_est"]], "status": "differs"})  # another column
    equivalences.add({"left": "p.d.c", "right": "p.d.d", "status": "differs", "explanation": "test_rows_dropped"})  # the rows
    for repeat in (
        {"left": "p.d.c", "right": "p.d.d", "columns": [["ts", "ts2"]], "status": "differs"},  # ts already has an entry
        {"left": "p.d.d", "right": "p.d.c", "columns": [["y", "x"]], "status": "differs"},  # x = y, written the other way
        {"left": "p.d.c", "right": "p.d.d", "status": "differs"},  # the rows, twice
    ):
        with pytest.raises(ValueError):
            equivalences.add(repeat)
    equivalences.add({"left": "p.d.lonely", "right": None, "status": "missing"})
    with pytest.raises(ValueError):
        equivalences.add({"left": "p.d.lonely", "right": None, "status": "missing"})
    with pytest.raises(ValueError):
        equivalences.add({"left": "p.d.lonely", "right": "p.d.e", "columns": [["x", "y"]]})
    equivalences.add({"left": "p.d.g", "right": "p.d.h", "columns": [["ts", "ts"]], "status": "differs"})
    with pytest.raises(ValueError):  # a whole-table declaration over a pair with a recorded difference
        equivalences.add({"left": "p.d.g", "right": "p.d.h", "whole": True})
    assert len(equivalences.load_all()) == 6


def test_remove_by_status_and_left_table():
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]]})
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["ts", "ts"]], "status": "differs"})
    equivalences.add({"left": "p.d.c", "right": "p.d.b", "columns": [["z", None]], "status": "missing"})
    equivalences.add({"left": "p.d.gone", "right": None, "status": "missing"})
    assert equivalences.remove("p.d.b") is True  # by default only the equal declaration, as the Settings page shows
    assert [i.status for i in equivalences.load_all()] == ["differs", "missing", "missing"]
    assert equivalences.remove("p.d.b", left="p.d.x", status=None) is False
    assert equivalences.remove("p.d.b", left="p.d.c", status="missing") is True
    assert equivalences.remove("p.d.gone", status="missing") is True  # a table with no counterpart, by its name
    assert [i.label for i in equivalences.load_all()] == ["p.d.a differs from p.d.b (ts vs ts)"]
    assert equivalences.remove("p.d.b", status=None) is True and equivalences.load_all() == []


# ------------------------------------------------------------------ proof safety


DIFFERS = Equivalence("p.d.a", "p.d.b", (("x", "y"),), status="differs", explanation="rounding")
MISSING = Equivalence("p.d.a", "p.d.b", (("x", None),), status="missing")


def test_only_equal_declarations_rewrite_a_query():
    sql = "SELECT y, COUNT(*) FROM `p.d.b` GROUP BY y"
    for record in (DIFFERS, MISSING, Equivalence(None, "p.d.b", status="missing")):
        assert equivalences.rewrite_sql(sql, [record]) == (sql, [])
        tree = sqlglot.parse_one(sql, read="bigquery")
        rewritten, used = equivalences.rewrite_tree(tree, [record])
        assert used == [] and "p.d.a" not in rewritten.sql(dialect="bigquery")
    with pytest.raises(ValueError):
        equivalences.replacement(DIFFERS, "b")
    equal = Equivalence("p.d.a", "p.d.b", (("x", "y"),))
    rewritten, used = equivalences.rewrite_sql(sql, [DIFFERS, equal])
    assert used == [equal] and "p.d.a" in rewritten  # the equal one alone applies


def test_saved_records_never_reach_a_proof():
    pytest.importorskip("z3")
    from kumosql import prover_context
    from kumosql.prover_schema import ProverSchema
    from kumosql.smt_equivalence import SmtStatus

    old = "SELECT y, COUNT(*) AS n FROM p.d.b GROUP BY y"
    new = "SELECT x AS y, COUNT(*) AS n FROM p.d.a GROUP BY x"
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "status": "differs", "explanation": "rounding"})
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["z", None]], "status": "missing"})
    assert prover_context.prove(old, new, schema=ProverSchema()).status is not SmtStatus.PROVEN_EQUIVALENT
    equivalences.remove("p.d.b", status=None)
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]]})
    assert prover_context.prove(old, new, schema=ProverSchema()).status is SmtStatus.PROVEN_EQUIVALENT


def test_a_recorded_difference_does_not_prove_two_pipelines_equal(tmp_path):
    pytest.importorskip("z3")
    from kumosql.pipeline_equivalence import prove_models

    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/s.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(tmp_path, "definitions/s2.sqlx", 'config { type: "declaration", schema: "raw", name: "orders_v2" }\n')
    write(tmp_path, "definitions/c.sqlx", 'config { type: "table" }\nSELECT id, SUM(amt) AS tot FROM ${ref("raw","orders")} GROUP BY id\n')
    write(tmp_path, "definitions/h.sqlx", 'config { type: "table" }\nSELECT order_id AS k, SUM(amount) AS total FROM ${ref("raw","orders_v2")} GROUP BY order_id\n')
    pipeline = load_sqlx_project(tmp_path)
    mapping = {"left": "proj.raw.orders", "right": "proj.raw.orders_v2", "columns": [["id", "order_id"], ["amt", "amount"]]}
    equivalences.add({**mapping, "status": "differs", "explanation": "currency"})
    assert not prove_models(pipeline, "c", "h").proven  # the saved record is not an assumption
    assert not prove_models(pipeline, "c", "h", declared=[equivalences.parse({**mapping, "status": "differs"})]).proven
    assert prove_models(pipeline, "c", "h", declared=[equivalences.parse({**mapping, "whole": True})]).proven


# ---------------------------------------------------------------------- lookup


def test_lookup_by_table_column_and_status():
    equivalences.add({"left": "proj.raw.orders", "right": "raw.orders_v2", "columns": [["id", "order_id"]], "whole": True})
    equivalences.add({"left": "proj.raw.orders", "right": "proj.raw.orders_v3", "columns": [["ts", "ts_est"]],
                      "status": "differs", "explanation": "timezone_utc_vs_est"})
    equivalences.add({"left": "proj.raw.legacy", "right": None, "status": "missing"})

    by_short = equivalences.lookup("orders")
    assert [(e["right"], e["matched_on"], e["counterpart"]) for e in by_short] == [
        ("raw.orders_v2", "left", "raw.orders_v2"), ("proj.raw.orders_v3", "left", "proj.raw.orders_v3")]
    # a longer spelling finds an entry saved under a shorter one, and the other way round
    assert [e["counterpart"] for e in equivalences.lookup("proj.raw.orders_v2")] == ["proj.raw.orders"]
    assert [e["status"] for e in equivalences.lookup("orders_v3")] == ["differs"]
    assert equivalences.lookup("orders", status="differs")[0]["explanation"] == "timezone_utc_vs_est"
    assert equivalences.lookup("raw.orders_v9") == []

    [renamed] = equivalences.lookup("orders_v2", "order_id")
    assert renamed["column_matches"] == [{"left_column": "id", "right_column": "order_id", "implied": False}]
    [same_name] = equivalences.lookup("orders_v2", "amt")  # a whole-table declaration keeps other names
    assert same_name["column_matches"] == [{"left_column": "amt", "right_column": "amt", "implied": True}]
    assert [e["status"] for e in equivalences.lookup("orders", "ts")] == ["equal", "differs"]
    assert [e["status"] for e in equivalences.lookup(column="ts_est")] == ["differs"]
    assert equivalences.lookup("orders_v2", "id") == []  # renamed away: orders_v2 has order_id, not id

    [legacy] = equivalences.lookup("legacy")
    assert (legacy["label"], legacy["matched_on"], legacy["counterpart"]) == ("proj.raw.legacy has no counterpart", "left", None)
    assert len(equivalences.lookup()) == 3 and len(equivalences.lookup(status="missing")) == 1


def test_listed_entries_are_a_crosswalk_file_for_reconcile(tmp_path, capsys):
    from kumosql.reconcile import load_crosswalk

    equivalences.add({"left": "proj.old.orders", "right": "proj.mart.orders", "columns": [["amt", "amount"]]})
    equivalences.add({"left": "proj.old.orders", "right": "proj.mart.orders", "columns": [["ts", "ts_est"]],
                      "status": "differs", "explanation": "timezone_utc_vs_est"})
    equivalences.add({"left": "proj.old.orders", "right": "proj.mart.orders", "columns": [["legacy", None]], "status": "missing"})
    equivalences.add({"left": "proj.old.weekly", "right": None, "status": "missing"})
    assert equivalence_main(["list", "--table", "mart.orders"]) == 0
    path = tmp_path / "map.json"
    path.write_text(capsys.readouterr().out, encoding="utf-8")
    walk = load_crosswalk(str(path), left_table="proj.old.orders", right_table="proj.mart.orders")
    assert [(link.left, link.right, link.status, link.explanation, link.missing) for link in walk.links] == [
        ("amt", "amount", "equal", "", False),
        ("ts", "ts_est", "differs", "timezone_utc_vs_est", False),
        ("legacy", None, "missing", "", True),
    ]
    assert walk.notes == ()  # the table with no counterpart is not listed for this pair, so nothing is left unchecked
    reversed_walk = load_crosswalk(str(path), left_table="mart.orders", right_table="old.orders")
    assert [(link.left, link.right) for link in reversed_walk.links] == [("amount", "amt"), ("ts_est", "ts"), (None, "legacy")]


# -------------------------------------------------------------------- suggest


def test_suggestions_pair_columns_by_meaning_whatever_their_names(tmp_path):
    pipeline = two_versions(tmp_path)
    result = suggest_equivalences(pipeline, ["proj.old.o1", "proj.old.o2"], ["proj.mart.n1", "proj.mart.n2"])

    rows = pairs(suggested(result, "proj.old.o1", "proj.mart.n1"))
    assert rows["order_key", "id"]["match_kind"] == "same_source_column" and rows["order_key", "id"]["exact"]
    assert rows["cust", "customer_id"]["match_kind"] == "same_source_column"
    assert rows["st", "status_up"]["match_kind"] == "same_expression"  # UPPER and upper: the same expression
    assert rows["st", "status_up"]["left_meaning"] == "expr:UPPER(col:proj.raw.orders.status)"
    assert rows["ts", "d"]["match_kind"] == "same_source_column" and not rows["ts", "d"]["exact"]  # DATE(ts)

    aggregates = pairs(suggested(result, "proj.old.o2", "proj.mart.n2"))
    assert {k: p["match_kind"] for k, p in aggregates.items() if p["exact"]} == {
        ("customer_id", "cid"): "same_source_column", ("revenue", "total"): "same_aggregate",
        ("n", "cnt"): "same_aggregate", ("mx", "biggest"): "same_aggregate",
    }
    variant = aggregates["triple", "dbl"]  # SUM(amt * 3) and SUM(amt * 2): the same aggregate of the same column
    assert variant["match_kind"] == "same_aggregate" and not variant["exact"]
    assert "SUM over the same source columns" in variant["reasons"][0]


def test_suggestions_rank_the_same_meaning_over_the_same_aggregate_over_shared_sources(tmp_path):
    pipeline = two_versions(tmp_path)
    left, right = ["proj.old.o1", "proj.old.o2"], ["proj.mart.n1", "proj.mart.n2"]
    result = suggest_equivalences(pipeline, left, right)
    assert [(s["left_table"], s["right_table"]) for s in result["suggestions"]] == [
        ("proj.old.o2", "proj.mart.n2"), ("proj.old.o1", "proj.mart.n1")]
    scores = [s["score"] for s in result["suggestions"]]
    assert scores == sorted(scores, reverse=True) and all(0 < s <= 1 for s in scores)

    aggregates = pairs(suggested(result, "proj.old.o2", "proj.mart.n2"))
    rows = pairs(suggested(result, "proj.old.o1", "proj.mart.n1"))
    exact, variant, shared = aggregates["revenue", "total"]["score"], aggregates["triple", "dbl"]["score"], rows["ts", "d"]["score"]
    assert exact > variant > shared
    for suggestion in result["suggestions"]:  # strongest column pair first
        column_scores = [p["score"] for p in suggestion["matched_columns"]]
        assert column_scores == sorted(column_scores, reverse=True)
    # the order is the same whichever way the tables are listed
    again = suggest_equivalences(pipeline, list(reversed(left)), list(reversed(right)))
    assert again["suggestions"] == result["suggestions"]
    assert suggest_equivalences(pipeline, left, right, top=1)["suggestions"] == result["suggestions"][:1]
    assert suggest_equivalences(pipeline, left, right, top=1)["total"] == 2
    everything = left + right  # one set against itself: each pair once, never a table with itself
    mirrored = suggest_equivalences(pipeline, everything, everything)["suggestions"]
    assert [(s["left_table"], s["right_table"]) for s in mirrored] == [
        ("proj.old.o2", "proj.mart.n2"), ("proj.old.o1", "proj.mart.n1")]


def test_a_shared_name_alone_never_makes_a_suggestion(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/src.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(tmp_path, "definitions/src2.sqlx", 'config { type: "declaration", schema: "raw", name: "refunds" }\n')
    write(tmp_path, "definitions/a.sqlx", 'config { type: "table", schema: "old" }\nSELECT id, amt FROM ${ref("raw","orders")}\n')
    write(tmp_path, "definitions/b.sqlx", 'config { type: "table", schema: "mart" }\nSELECT id, amt FROM ${ref("raw","refunds")}\n')
    result = suggest_equivalences(load_sqlx_project(tmp_path), ["proj.old.a"], ["proj.mart.b"], min_score=0)
    assert result["suggestions"] == [] and result["skipped"] == {"nothing_in_common": 1}


def test_hints_propose_equal_only_at_the_same_grain_and_row_filters(tmp_path):
    pipeline = two_versions(tmp_path)
    write(tmp_path, "definitions/new/n3.sqlx", 'config { type: "table", schema: "mart" }\n'
          'SELECT customer_id AS cid, SUM(amt) AS total FROM ${ref("raw","orders")} WHERE amt > 0 GROUP BY customer_id\n')
    pipeline = load_sqlx_project(tmp_path)
    result = suggest_equivalences(pipeline, ["proj.old.o1", "proj.old.o2"], ["proj.mart.n1", "proj.mart.n2", "proj.mart.n3"], min_score=0)

    grouped = suggested(result, "proj.old.o2", "proj.mart.n2")
    assert grouped["checks"] == {"grain": "matched", "row_scope": "matched"}
    assert grouped["command_hint"] == ("python -m kumosql equivalence add proj.old.o2 proj.mart.n2 "
                                       "customer_id=cid mx=biggest n=cnt revenue=total")
    assert grouped["differs_hint"] == "python -m kumosql equivalence add proj.old.o2 proj.mart.n2 triple=dbl --status differs"

    rows = suggested(result, "proj.old.o1", "proj.mart.n1")  # a source without a declared key: the grain is unknown
    assert rows["checks"]["grain"] == "unknown"
    assert rows["command_hint"].endswith("--status differs") and "ts=d" not in rows["command_hint"]
    assert rows["differs_hint"] == "python -m kumosql equivalence add proj.old.o1 proj.mart.n1 ts=d --status differs"
    assert any("drop --status differs" in note for note in rows["review"])

    filtered = suggested(result, "proj.old.o2", "proj.mart.n3")  # WHERE amt > 0 keeps other rows
    assert filtered["checks"]["row_scope"] == "differs" and filtered["command_hint"].endswith("--status differs")
    assert filtered["left_only_columns"] == ["n", "mx", "triple"] and filtered["right_only_columns"] == []

    # a hint is a command a person can run as it is; afterwards the pair is marked as declared
    assert equivalence_main(shlex.split(grouped["command_hint"])[4:]) == 0
    assert equivalences.lookup("proj.mart.n2")[0]["columns"] == [["customer_id", "cid"], ["mx", "biggest"], ["n", "cnt"], ["revenue", "total"]]
    after = suggested(suggest_equivalences(pipeline, ["proj.old.o2"], ["proj.mart.n2"]), "proj.old.o2", "proj.mart.n2")
    assert after["already_declared"] is True and after["declared"] == [equivalences.load()[0].label]
    assert suggested(result, "proj.old.o1", "proj.mart.n1")["already_declared"] is False


def test_a_declared_source_key_gives_two_passthrough_tables_a_grain_and_an_equal_hint(tmp_path):
    two_versions(tmp_path)
    write(tmp_path, "definitions/src.sqlx",
          'config { type: "declaration", schema: "raw", name: "orders", assertions: { uniqueKey: ["id"] } }\n')
    pipeline = load_sqlx_project(tmp_path)
    rows = suggested(suggest_equivalences(pipeline, ["proj.old.o1"], ["proj.mart.n1"]), "proj.old.o1", "proj.mart.n1")
    assert rows["checks"] == {"grain": "matched", "row_scope": "matched"}
    assert "--status" not in rows["command_hint"] and "order_key=id" in rows["command_hint"]
    assert rows["differs_hint"].endswith("ts=d --status differs")  # DATE(ts) is still not ts


def test_a_declaration_between_sources_carries_into_the_suggestions(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/src.sqlx", 'config { type: "declaration", schema: "raw", name: "orders" }\n')
    write(tmp_path, "definitions/src2.sqlx", 'config { type: "declaration", schema: "raw", name: "orders_v2" }\n')
    write(tmp_path, "definitions/o.sqlx", 'config { type: "table", schema: "old" }\n'
          'SELECT customer_id, SUM(amt) AS revenue FROM ${ref("raw","orders")} GROUP BY customer_id\n')
    write(tmp_path, "definitions/n.sqlx", 'config { type: "table", schema: "mart" }\n'
          'SELECT cust_id AS customer_id, SUM(amount) AS revenue FROM ${ref("raw","orders_v2")} GROUP BY cust_id\n')
    pipeline = load_sqlx_project(tmp_path)

    unrelated = suggest_equivalences(pipeline, ["proj.old.o"], ["proj.mart.n"], items=[], min_score=0)
    assert not any(p["exact"] for s in unrelated["suggestions"] for p in s["matched_columns"])
    mapping = {"left": "raw.orders", "right": "raw.orders_v2", "columns": [["customer_id", "cust_id"], ["amt", "amount"]]}
    as_difference = [equivalences.parse({**mapping, "status": "differs"})]
    assert suggest_equivalences(pipeline, ["proj.old.o"], ["proj.mart.n"], items=as_difference, min_score=0)["suggestions"] \
        == unrelated["suggestions"]  # a recorded difference is no reason to call two columns the same

    equivalences.add({**mapping, "whole": True})
    [suggestion] = suggest_equivalences(pipeline, ["proj.old.o"], ["proj.mart.n"])["suggestions"]
    found = pairs(suggestion)
    assert set(found) == {("customer_id", "customer_id"), ("revenue", "revenue")}
    assert all(p["exact"] and "matched through a saved equal declaration" in p["reasons"] for p in found.values())
    assert found["revenue", "revenue"]["right_meaning"] == "agg:SUM(col:proj.raw.orders_v2.amount)"  # as profiled
    assert suggestion["checks"] == {"grain": "matched", "row_scope": "matched"}
    assert "--status" not in suggestion["command_hint"]


def test_nested_fields_follow_a_declaration_and_unknown_meanings_stay_unmatched(tmp_path):
    write(tmp_path, "workflow_settings.yaml", "defaultProject: proj\ndefaultDataset: an\n")
    write(tmp_path, "definitions/src.sqlx", 'config { type: "declaration", schema: "raw", name: "events" }\n')
    write(tmp_path, "definitions/src2.sqlx", 'config { type: "declaration", schema: "raw", name: "events_v2" }\n')
    write(tmp_path, "definitions/o.sqlx", 'config { type: "table", schema: "old" }\n'
          'SELECT id, payload.a.b AS ab, payload.c AS c, (SELECT MAX(x) FROM UNNEST(vals) AS x) AS top_val\n'
          'FROM ${ref("raw","events")}\n')
    write(tmp_path, "definitions/n.sqlx", 'config { type: "table", schema: "mart" }\n'
          'SELECT event_id AS id, payload.a.b AS ab, UPPER(payload.c) AS c_up, (SELECT MAX(x) FROM UNNEST(vals) AS x) AS top_val\n'
          'FROM ${ref("raw","events_v2")}\n')
    pipeline = load_sqlx_project(tmp_path)
    declared = [equivalences.parse({"left": "raw.events", "right": "raw.events_v2", "columns": [["id", "event_id"]], "whole": True})]
    [suggestion] = suggest_equivalences(pipeline, ["proj.old.o"], ["proj.mart.n"], items=declared, min_score=0)["suggestions"]
    found = pairs(suggestion)
    assert found["ab", "ab"]["exact"] and found["ab", "ab"]["right_meaning"] == "field(col:proj.raw.events_v2.payload, a.b)"
    assert found["id", "id"]["exact"]
    assert found["c", "c_up"]["match_kind"] == "same_source_column" and not found["c", "c_up"]["exact"]  # UPPER(payload.c)
    assert found["top_val", "top_val"]["match_kind"] == "same_name"  # a name, not a meaning: worth nothing to the score
    assert suggestion["unknown_meaning"] == {"left": {"top_val": "unresolved_column"}, "right": {"top_val": "unresolved_column"}}
    assert "top_val" not in suggestion["left_only_columns"] + suggestion["right_only_columns"]
    assert "top_val" not in suggestion["command_hint"] + (suggestion["differs_hint"] or "")


def test_suggest_never_writes_the_store(tmp_path):
    pipeline = two_versions(tmp_path / "project")
    files = store_files()
    left, right = ["proj.old.o1", "proj.old.o2"], ["proj.mart.n1", "proj.mart.n2"]
    assert suggest_equivalences(pipeline, left, right)["total"] == 2
    assert equivalence_main(["suggest", "--project", str(tmp_path / "project"), "--left", "old", "--right", "mart"]) == 0
    assert not any(path.exists() for path in files.values())  # nothing created

    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]]})
    equivalences.add({"left": "p.d.a", "right": "p.d.b", "columns": [["z", None]], "status": "missing"})
    before = {name: (path.read_bytes(), path.stat().st_mtime_ns) for name, path in files.items()}
    suggest_equivalences(pipeline, left, right)
    assert equivalence_main(["suggest", "--project", str(tmp_path / "project"), "--left", "old", "--right", "mart"]) == 0
    assert {name: (path.read_bytes(), path.stat().st_mtime_ns) for name, path in files.items()} == before  # nothing changed
    assert not any(p.suffix == ".tmp" for p in files["equivalences.json"].parent.iterdir())


def test_tables_are_chosen_by_name_dataset_folder_prefix_or_glob(tmp_path):
    pipeline = two_versions(tmp_path)
    assert select_tables(pipeline, ["o1"]) == (["proj.old.o1"], [{"spec": "o1", "matched_by": "table", "tables": 1}])
    assert select_tables(pipeline, ["old.o2,proj.mart.n1"])[0] == ["proj.old.o2", "proj.mart.n1"]
    assert select_tables(pipeline, ["mart"])[0] == ["proj.mart.n1", "proj.mart.n2"]
    assert select_tables(pipeline, ["proj.old"])[1][0]["matched_by"] == "dataset"
    assert select_tables(pipeline, ["definitions/new"]) == (
        ["proj.mart.n1", "proj.mart.n2"], [{"spec": "definitions/new", "matched_by": "folder", "tables": 2}])
    assert select_tables(pipeline, ["new"])[0] == ["proj.mart.n1", "proj.mart.n2"]  # a folder anywhere in the path
    assert select_tables(pipeline, ["n"])[1][0]["matched_by"] == "prefix"
    assert select_tables(pipeline, ["proj.*.o?"])[0] == ["proj.old.o1", "proj.old.o2"]
    assert select_tables(pipeline, ["o1", "o1"])[0] == ["proj.old.o1"]
    for nothing in (["nowhere"], [""], [" , "]):
        with pytest.raises(ValueError):
            select_tables(pipeline, nothing)
    with pytest.raises(ValueError):
        suggest_equivalences(pipeline, ["proj.old.missing"], ["proj.mart.n1"])


# ----------------------------------------------------------------- command line


def test_command_line_add_list_and_remove_with_statuses(capsys):
    assert equivalence_main(["add", "proj.raw.orders", "proj.raw.orders_v2", "id=order_id", "amt=amount"]) == 0
    assert equivalence_main(["add", "proj.raw.orders", "proj.raw.orders_v2", "ts=ts_est", "--status", "differs",
                             "--explanation", "timezone_utc_vs_est", "--evidence", "SELECT 1", "--verified-at", "2026-11-20"]) == 0
    assert equivalence_main(["add", "proj.raw.orders", "proj.raw.orders_v2", "legacy=", "=extra", "--status", "missing"]) == 0
    assert equivalence_main(["add", "proj.raw.old_report", "-", "--status", "missing"]) == 0
    out = capsys.readouterr().out
    assert "saved proj.raw.orders differs from proj.raw.orders_v2 (ts vs ts_est) [timezone_utc_vs_est]" in out
    assert "saved proj.raw.old_report has no counterpart" in out

    with pytest.raises(SystemExit):
        equivalence_main(["add", "a.b.c", "a.b.d", "x=y", "--status", "unsure"])
    assert "invalid choice: 'unsure'" in capsys.readouterr().err
    for refused in (
        ["add", "a.b.c", "a.b.d", "x=y", "--verified-at", "2026-13-01"],
        ["add", "a.b.c", "a.b.d", "x=y", "--explanation", "rounding"],  # an explanation is for a difference
        ["add", "a.b.c", "-", "x=y"],
        ["add", "proj.raw.orders", "proj.raw.orders_v2", "ts=ts", "--status", "differs"],  # ts already recorded
    ):
        assert equivalence_main(refused) == 2
    assert capsys.readouterr().err.count("error: ") == 4

    assert equivalence_main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(e["status"], e["verified_at"]) for e in listed] == [
        ("equal", None), ("differs", "2026-11-20"), ("missing", None), ("missing", None)]
    assert equivalence_main(["list", "--status", "missing"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert equivalence_main(["list", "--table", "orders_v2", "--column", "ts_est"]) == 0
    [found] = json.loads(capsys.readouterr().out)
    assert (found["explanation"], found["evidence"], found["counterpart"]) == ("timezone_utc_vs_est", "SELECT 1", "proj.raw.orders")

    assert equivalence_main(["remove", "proj.raw.orders_v2", "--status", "missing"]) == 0
    assert equivalence_main(["remove", "proj.raw.orders_v2", "--left", "proj.raw.elsewhere"]) == 0
    assert capsys.readouterr().out.splitlines() == ["removed", "nothing to remove"]
    assert [i.status for i in equivalences.load_all()] == ["equal", "differs", "missing"]
    assert equivalence_main(["remove", "proj.raw.orders_v2"]) == 0  # without --status: every status
    assert [i.label for i in equivalences.load_all()] == ["proj.raw.old_report has no counterpart"]


def test_command_line_suggest(tmp_path, capsys):
    two_versions(tmp_path)
    assert equivalence_main(["suggest", "--project", str(tmp_path), "--left", "old", "--right", "definitions/new",
                             "--top", "1", "--min-score", "0.5"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["shown"], result["total"]) == (1, 2)
    [best] = result["suggestions"]
    assert (best["left_table"], best["right_table"]) == ("proj.old.o2", "proj.mart.n2")
    assert best["command_hint"].startswith("python -m kumosql equivalence add proj.old.o2 proj.mart.n2 ")
    assert result["selection"] == {"left": [{"spec": "old", "matched_by": "dataset", "tables": 2}],
                                   "right": [{"spec": "definitions/new", "matched_by": "folder", "tables": 2}]}
    assert "nothing was saved" in result["note"]

    assert equivalence_main(["suggest", "--project", str(tmp_path), "--left", "nowhere", "--right", "mart"]) == 2
    assert "nowhere matches no table" in capsys.readouterr().err
    assert equivalence_main(["suggest", "--project", str(tmp_path / "missing"), "--left", "old", "--right", "mart"]) == 2
