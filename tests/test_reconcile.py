"""Reconcile two tables by key: the one comparison query, run for real on DuckDB, and the BigQuery guards.

The fixture has every kind of row: matched (NULLs on both sides agree), only on the left, only on the right,
and value differences, some explained by a rule and some not. BigQuery is faked by running the BigQuery SQL
KumoSQL sends on DuckDB, translated, the way the data profile tests do.
"""

import json

import duckdb
import pytest
import sqlglot

from kumosql import data_sources, dryrun, equivalences, reconcile_cli, scope_queries, state
from kumosql.reconcile import (
    BigQueryExecutor, ByteCapExceeded, Column, DuckDBExecutor, ReconcileError, build_reconcile_sql, check_rules,
    estimate_bytes, load_crosswalk, plan_reconcile, reconcile,
)
from kumosql.reconcile_rules import KnownDiffRule, RuleError
from kumosql.sql_validation import validate_readonly_query

LEFT_ROWS = [
    (1, "paid", 10.0, "a"),
    (2, "paid", 20.0, None),
    (3, "refunded", 30.0, "c"),
    (4, "paid", None, "d"),
    (5, "paid", 50.0, "e"),
    (6, "test", 60.0, "f"),
    (7, "paid", 70.0, "g"),
]
RIGHT_ROWS = [
    (1, "paid", 10.0, "a"),      # matches
    (2, "paid", 20.0, None),     # matches: NULL note on both sides
    (3, "refunded", 0.0, "c"),   # amount differs: explained by refunds_zeroed
    (4, "paid", None, "d"),      # matches: NULL amount on both sides
    (5, "PAID", 50.0, "e"),      # status differs: unexplained
    (8, "paid", 80.0, "h"),      # only on the right: unexplained
]                                # 6 only on the left (explained by test_orders), 7 only on the left (unexplained)
REFUNDS = KnownDiffRule("refunds_zeroed", "l.status = 'refunded' AND r.amount = 0", "2026-11-01", verified_count=1)
TESTS = KnownDiffRule("test_orders", "status = 'test'", "2026-11-01", verified_count=1)
MAP = {"amount": "amount_usd"}


def make_tables(connection):
    connection.execute("CREATE TABLE orders_v1 (order_id INTEGER, status VARCHAR, amount DOUBLE, note VARCHAR)")
    connection.execute("CREATE TABLE orders_v2 (order_id INTEGER, status VARCHAR, amount_usd DOUBLE, note VARCHAR)")
    connection.executemany("INSERT INTO orders_v1 VALUES (?, ?, ?, ?)", LEFT_ROWS)
    connection.executemany("INSERT INTO orders_v2 VALUES (?, ?, ?, ?)", RIGHT_ROWS)


@pytest.fixture()
def db():
    connection = duckdb.connect()
    make_tables(connection)
    yield connection
    connection.close()


@pytest.fixture()
def database(tmp_path):
    """The same tables in a DuckDB file, closed, for the command line (which opens it read-only)."""

    path = tmp_path / "orders.duckdb"
    connection = duckdb.connect(str(path))
    make_tables(connection)
    connection.close()
    return path


def run(db, **options):
    options.setdefault("crosswalk", MAP)
    return reconcile("orders_v1", "orders_v2", "order_id", DuckDBExecutor(db), **options)


# ------------------------------------------------------------------ counts


def test_every_row_is_matched_explained_or_unexplained(db):
    result = run(db, rules=[REFUNDS, TESTS])
    data = result.to_json()
    assert (data["left_rows"], data["right_rows"]) == (7, 6)
    assert (data["keys_only_left"], data["keys_only_right"], data["keys_both"]) == (2, 1, 5)
    assert (data["matched_rows"], data["mismatched_rows"]) == (3, 5)
    assert (data["explained_rows"], data["unexplained_rows"]) == (2, 3)
    assert data["unexplained_by_kind"] == {"only_left": 1, "only_right": 1, "value_mismatch": 1}
    assert data["column_mismatches"] == {"amount": 1, "status": 1}
    assert data["unexplained_column_mismatches"] == {"status": 1}
    assert data["rules"] == [{"name": "refunds_zeroed", "verified_at": "2026-11-01", "matched_rows": 1},
                             {"name": "test_orders", "verified_at": "2026-11-01", "matched_rows": 1}]
    assert data["keys"] == ["order_id"] and data["compared_columns"] == ["status", "amount", "note"]
    assert data["column_map"] == {"amount": "amount_usd"} and data["left_only_columns"] == data["right_only_columns"] == []
    assert data["duplicate_keys"] == {"left": 0, "right": 0} and data["null_keys"] == {"left": 0, "right": 0}
    assert not result.ok and json.loads(json.dumps(data)) == data


def test_without_rules_every_difference_is_unexplained(db):
    data = run(db).to_json()
    assert (data["mismatched_rows"], data["explained_rows"], data["unexplained_rows"]) == (5, 0, 5)
    assert data["rules"] == [] and data["unexplained_column_mismatches"] == {"amount": 1, "status": 1}


def test_the_first_rule_that_holds_explains_the_row(db):
    broad = KnownDiffRule("any_left_only", "r.order_id IS NULL", "2026-11-01")
    first = run(db, rules=[TESTS, broad]).to_json()["rules"]
    assert [rule["matched_rows"] for rule in first] == [1, 1]  # 6 goes to test_orders, 7 to the broad rule
    second = run(db, rules=[broad, TESTS]).to_json()["rules"]
    assert [rule["matched_rows"] for rule in second] == [2, 0]  # the broad rule now takes both


def test_null_is_compared_safely_and_null_keys_never_match(db):
    db.execute("CREATE TABLE a AS SELECT * FROM (VALUES (1, NULL), (2, 5), (NULL, 1)) t(id, v)")
    db.execute("CREATE TABLE b AS SELECT * FROM (VALUES (1, 5), (2, 5), (NULL, 1)) t(id, v)")
    data = reconcile("a", "b", "id", DuckDBExecutor(db)).to_json()
    assert data["column_mismatches"] == {"v": 1} and data["matched_rows"] == 1  # NULL against 5 differs
    assert data["null_keys"] == {"left": 1, "right": 1}
    assert (data["keys_only_left"], data["keys_only_right"]) == (1, 1)
    assert any("NULL in the key" in note for note in data["notes"])


def test_a_key_that_repeats_is_reported_not_silently_counted(db):
    db.execute("INSERT INTO orders_v1 VALUES (1, 'paid', 10.0, 'a')")
    data = run(db).to_json()
    assert data["duplicate_keys"] == {"left": 1, "right": 0} and data["left_rows"] == 8
    assert data["keys_both"] == 6  # key 1 joins twice
    assert any("not unique on the left" in note and "joined pairs" in note for note in data["notes"])


def test_composite_and_renamed_keys(db):
    db.execute("CREATE TABLE a AS SELECT * FROM (VALUES (1, 'x', 1), (1, 'y', 2), (2, 'x', 3)) t(k1, k2, v)")
    db.execute("CREATE TABLE b AS SELECT * FROM (VALUES (1, 'x', 1), (1, 'y', 9), (3, 'x', 3)) t(id, k2, v)")
    data = reconcile("a", "b", "k1=id, k2", DuckDBExecutor(db)).to_json()
    assert data["keys"] == ["k1", "k2"] and data["column_map"] == {"k1": "id"}
    assert (data["matched_rows"], data["keys_only_left"], data["keys_only_right"], data["column_mismatches"]) == (1, 1, 1, {"v": 1})


# ----------------------------------------------------------------- samples


def test_sample_keys_are_off_by_default_and_never_queried(db):
    executor = DuckDBExecutor(db)
    data = run(db, rules=[REFUNDS]).to_json()
    assert data["samples_included"] is False and data["samples"] is None
    sql = build_reconcile_sql("orders_v1", "orders_v2", "order_id", executor, crosswalk=MAP)
    assert "kumo_k0" not in sql and "json_object" not in sql and "COALESCE(l." not in sql


def test_samples_list_only_unexplained_rows(db):
    data = run(db, rules=[REFUNDS, TESTS], include_samples=True).to_json()
    assert data["samples_included"] is True
    assert data["samples"] == {
        "only_left": [{"order_id": 7}], "only_right": [{"order_id": 8}], "value_mismatch": [{"order_id": 5}],
        "by_column": {"status": [{"order_id": 5}]},
    }
    everything = json.dumps(data["samples"])
    assert '"order_id": 3' not in everything and '"order_id": 6' not in everything  # explained rows are never sampled
    limited = run(db, include_samples=True, sample_limit=1).to_json()["samples"]
    assert limited["only_left"] == [{"order_id": 6}] and len(limited["value_mismatch"]) == 1
    with pytest.raises(ReconcileError, match="sample limit"):
        run(db, include_samples=True, sample_limit=0)


# ----------------------------------------------------- columns and crosswalk


def test_ignore_is_case_insensitive_and_a_key_cannot_be_ignored(db):
    data = run(db, ignore="STATUS, nothing_here").to_json()
    assert data["ignored_columns"] == ["status"] and "status" not in data["compared_columns"]
    assert data["column_mismatches"] == {"amount": 1}
    assert any("nothing_here" in note for note in data["notes"])
    with pytest.raises(ReconcileError, match="part of the key"):
        run(db, ignore="order_id")
    for name in ("amount", "amount_usd"):  # either name of a crosswalk pair ignores the pair
        data = run(db, ignore=name).to_json()
        assert "amount" not in data["compared_columns"] and data["left_only_columns"] == data["right_only_columns"] == []


def test_without_a_crosswalk_renamed_columns_are_reported_not_compared(db):
    data = reconcile("orders_v1", "orders_v2", "order_id", DuckDBExecutor(db)).to_json()
    assert data["left_only_columns"] == ["amount"] and data["right_only_columns"] == ["amount_usd"]
    assert "amount" not in data["column_mismatches"]


def test_a_crosswalk_file_maps_skips_missing_columns_and_names_explanations(db, tmp_path):
    path = tmp_path / "crosswalk.yml"
    path.write_text("""
- left: proj.ds.other_a
  right: proj.ds.other_b
  columns: [[status, other_status]]
- left: orders_v2
  right: orders_v1
  columns:
    - [amount_usd, amount]
    - {left: note, right: note, status: missing, explanation: notes_dropped}
""", encoding="utf-8")
    rules = [KnownDiffRule("notes_dropped", "TRUE", "2026-11-01")]
    data = run(db, crosswalk=str(path), rules=rules).to_json()
    assert data["column_map"] == {"amount": "amount_usd"}  # the reversed entry applies the other way round
    assert data["skipped_columns"] == [{"column": "note", "side": "left", "reason": "missing", "explanation": "notes_dropped"},
                                       {"column": "note", "side": "right", "reason": "missing", "explanation": "notes_dropped"}]
    assert data["right_only_columns"] == []
    assert "note" not in data["compared_columns"] and data["column_explanations"] == {"note": "notes_dropped"}
    assert any("other tables did not apply" in note for note in data["notes"])
    json_path = tmp_path / "crosswalk.json"
    json_path.write_text(json.dumps({"amount": "amount_usd", "note": None}), encoding="utf-8")
    data = run(db, crosswalk=str(json_path)).to_json()
    assert data["column_map"] == {"amount": "amount_usd"} and data["skipped_columns"][0]["column"] == "note"
    assert any("not one of the rules" in note for note in run(db, crosswalk=str(path), rules=[REFUNDS]).to_json()["notes"])


def test_saved_equivalences_are_a_crosswalk(db):
    equivalences.add({"left": "orders_v1", "right": "orders_v2", "columns": [["amount", "amount_usd"]]})
    data = run(db, crosswalk="saved").to_json()
    assert data["column_map"] == {"amount": "amount_usd"} and data["crosswalk"] == "saved"
    reverse = reconcile("orders_v2", "orders_v1", "order_id", DuckDBExecutor(db), crosswalk="saved").to_json()
    assert reverse["column_map"] == {"amount_usd": "amount"}
    walk = load_crosswalk("saved", left_table="a", right_table="b")
    assert walk.links == () and "no saved equivalence" in walk.notes[0]


def test_a_crosswalk_cannot_map_one_column_two_ways(db):
    with pytest.raises(ReconcileError, match="maps the left column"):
        run(db, crosswalk=[{"left": "amount", "right": "amount_usd"}, {"left": "amount", "right": "note"}])
    with pytest.raises(ReconcileError, match="crosswalk"):
        run(db, crosswalk="crosswalk.txt")


# -------------------------------------------------------------- rule binding


def test_rule_columns_bind_to_their_side_and_through_the_crosswalk(db):
    # r.amount is the right side's amount_usd through the crosswalk; a bare column is tried on each side.
    data = run(db, rules=[REFUNDS, KnownDiffRule("shouting", "status = 'PAID'", "2026-11-01")]).to_json()
    assert [rule["matched_rows"] for rule in data["rules"]] == [1, 1]
    with pytest.raises(ReconcileError, match="right side has no column 'missing'"):
        run(db, rules=[KnownDiffRule("bad", "r.missing = 1", "2026-11-01")])
    with pytest.raises(ReconcileError, match="no side has all of the columns"):
        run(db, rules=[KnownDiffRule("bad", "nowhere = 1", "2026-11-01")])
    one_column = KnownDiffRule("flag", "flagged", "2026-11-01")
    db.execute("CREATE TABLE a AS SELECT * FROM (VALUES (1, TRUE), (2, FALSE)) t(id, flagged)")
    db.execute("CREATE TABLE b AS SELECT * FROM (VALUES (1, FALSE)) t(id, flagged)")
    data = reconcile("a", "b", "id", DuckDBExecutor(db), rules=[one_column]).to_json()
    assert data["explained_rows"] == 1 and data["unexplained_rows"] == 1  # 1 left row is flagged; 2 is not


def test_a_rule_is_checked_again_even_when_built_by_hand(db):
    with pytest.raises(RuleError, match="subquery"):
        run(db, rules=[KnownDiffRule("sneaky", "l.order_id IN (SELECT 1)", "2026-11-01")])


# -------------------------------------------------------------------- inputs


def test_inline_queries_and_sql_files_are_inputs(db, tmp_path):
    query = "SELECT order_id, status, amount AS amount_usd, note FROM orders_v1 -- trailing comment"
    path = tmp_path / "right.sql"
    path.write_text("-- the new table\nSELECT * FROM orders_v2;\n-- done\n", encoding="utf-8")
    data = reconcile(query, str(path), "order_id", DuckDBExecutor(db)).to_json()
    assert data["compared_columns"] == ["status", "amount_usd", "note"] and data["right"] == str(path)
    assert data["unexplained_rows"] == 5


@pytest.mark.parametrize("left", [
    "DELETE FROM orders_v1", "SELECT 1; DROP TABLE orders_v1", "WITH x AS (DELETE FROM t) SELECT 1", "orders; DROP",
    "missing.sql",
])
def test_anything_but_a_read_only_query_or_a_table_name_is_refused(db, left):
    with pytest.raises(ReconcileError):
        reconcile(left, "orders_v2", "order_id", DuckDBExecutor(db))


def test_unknown_keys_and_marker_clashes_are_refused(db):
    with pytest.raises(ReconcileError, match="left side has no key column 'nope'"):
        reconcile("orders_v1", "orders_v2", "nope", DuckDBExecutor(db))
    with pytest.raises(ReconcileError, match="needs at least one column"):
        reconcile("orders_v1", "orders_v2", " , ", DuckDBExecutor(db))
    with pytest.raises(ReconcileError, match="clashes"):
        reconcile("SELECT order_id, 1 AS kumo_in FROM orders_v1", "SELECT order_id, 1 AS kumo_in FROM orders_v2",
                  "order_id", DuckDBExecutor(db))


def test_a_comparison_too_wide_for_the_read_only_check_is_refused_not_sent(db):
    columns = ", ".join(f"i AS column_with_a_long_name_{n}" for n in range(300))
    db.execute(f"CREATE TABLE wide_a AS SELECT i AS id, {columns} FROM range(3) t(i)")
    db.execute(f"CREATE TABLE wide_b AS SELECT i AS id, {columns} FROM range(3) t(i)")
    with pytest.raises(ReconcileError, match="20,000-character limit"):
        reconcile("wide_a", "wide_b", "id", DuckDBExecutor(db))
    narrow = ",".join(f"column_with_a_long_name_{n}" for n in range(20, 300))
    assert reconcile("wide_a", "wide_b", "id", DuckDBExecutor(db), ignore=narrow).matched_rows == 3


def test_the_generated_query_is_one_read_only_statement(db):
    sql = build_reconcile_sql("orders_v1", "orders_v2", "order_id", DuckDBExecutor(db), crosswalk=MAP,
                              rules=[REFUNDS, TESTS], include_samples=True)
    validate_readonly_query(sql)
    assert sql.count("FULL OUTER JOIN") == 1 and "IS DISTINCT FROM" in sql
    assert "CASE WHEN NOT kumo_mis THEN 0 WHEN kumo_p1 THEN 1 WHEN kumo_p2 THEN 2 ELSE 0 END" in sql


# --------------------------------------------------------------- rules check


def test_rules_check_flags_stale_and_grown_rules(db):
    stale = KnownDiffRule("stale", "l.status = 'cancelled'", "2026-10-01", verified_count=4)
    grown = KnownDiffRule("grown", "r.order_id IS NULL OR l.order_id IS NULL", "2026-10-01", verified_count=0)
    result = check_rules("orders_v1", "orders_v2", "order_id", DuckDBExecutor(db), [REFUNDS, stale, grown], crosswalk=MAP)
    data = result.to_json()
    by_name = {rule["name"]: rule for rule in data["rules"]}
    assert by_name["refunds_zeroed"] == {"name": "refunds_zeroed", "status": "ok", "flags": [], "verified_at": "2026-11-01",
                                         "verified_count": 1, "current_count": 1, "standalone_count": 1, "growth_limit": 11}
    assert by_name["stale"]["flags"] == ["no_longer_explains"] and by_name["stale"]["current_count"] == 0
    assert by_name["grown"]["flags"] == ["grew_significantly"] and by_name["grown"]["current_count"] == 3
    assert data["summary"] == {"rules": 3, "ok": 1, "flagged": 2, "no_longer_explains": 1, "grew_significantly": 1}
    assert not result.ok


def test_growth_uses_the_factor_the_minimum_and_a_rules_own_ratio(db):
    db.execute("CREATE TABLE a AS SELECT i AS id, i AS v FROM range(100) t(i)")
    db.execute("CREATE TABLE b AS SELECT i AS id, CASE WHEN i < 40 THEN i + 1 ELSE i END AS v FROM range(100) t(i)")
    executor = DuckDBExecutor(db)
    drift = KnownDiffRule("drift", "r.v = l.v + 1", "2026-10-01", verified_count=5)
    status = lambda **kw: check_rules("a", "b", "id", executor, [drift], **kw).rules[0]  # noqa: E731
    assert status()["flags"] == ["grew_significantly"] and status()["growth_limit"] == 15  # max(5 x 2, 5 + 10)
    assert status(growth_factor=10)["status"] == "ok"  # limit max(50, 15)
    assert status(min_growth=40)["status"] == "ok"  # limit max(10, 45)
    own = KnownDiffRule("drift", "r.v = l.v + 1", "2026-10-01", verified_count=5, max_growth_ratio=8)
    assert check_rules("a", "b", "id", executor, [own]).rules[0]["status"] == "ok"  # limit max(40, 15)
    with pytest.raises(ReconcileError, match="growth factor"):
        status(growth_factor=0.5)


def test_a_rule_shadowed_by_an_earlier_one_says_so(db):
    broad = KnownDiffRule("any_left_only", "r.order_id IS NULL", "2026-11-01")
    rules = check_rules("orders_v1", "orders_v2", "order_id", DuckDBExecutor(db), [broad, TESTS], crosswalk=MAP).rules
    assert rules[1]["flags"] == ["no_longer_explains"] and rules[1]["standalone_count"] == 1
    assert "earlier rule" in rules[1]["note"]


# ------------------------------------------------------------- command line


def rules_file(tmp_path, *rules):
    from kumosql.reconcile_rules import save_rules

    return str(save_rules(list(rules), tmp_path / "rules.yml"))


def cli_args(database, *extra):
    return ["--duckdb", str(database), "--left", "orders_v1", "--right", "orders_v2", "--key", "order_id", *extra]


def test_reconcile_command_exits_1_on_unexplained_rows_and_0_when_all_are_explained(database, tmp_path, capsys):
    target = tmp_path / "out.json"
    code = reconcile_cli.reconcile_main(cli_args(database, "--ignore", "amount,amount_usd", "--output", str(target)))
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["unexplained_rows"] == 4 and json.loads(target.read_text(encoding="utf-8")) == data
    everything = rules_file(tmp_path, TESTS, KnownDiffRule("rest", "TRUE", "2026-11-01"))
    assert reconcile_cli.reconcile_main(cli_args(database, "--rules", everything, "--ignore", "amount,amount_usd")) == 0
    assert json.loads(capsys.readouterr().out)["explained_rows"] == 4


def test_reconcile_command_samples_only_when_asked(database, capsys):
    reconcile_cli.reconcile_main(cli_args(database))
    assert json.loads(capsys.readouterr().out)["samples"] is None
    reconcile_cli.reconcile_main(cli_args(database, "--include-samples", "--sample-limit", "1"))
    samples = json.loads(capsys.readouterr().out)["samples"]
    assert samples["only_right"] == [{"order_id": 8}]


def test_reconcile_command_prints_the_query_without_running_it(database, capsys):
    assert reconcile_cli.reconcile_main(cli_args(database, "--sql-only")) == 0
    out = capsys.readouterr().out
    assert out.startswith("WITH kumo_l AS") and out.rstrip().endswith(";")
    assert reconcile_cli.reconcile_main(cli_args(database, "--dry-run")) == 0
    assert "DuckDB: nothing was run" in capsys.readouterr().out


def test_reconcile_command_exits_2_on_an_input_error(database, tmp_path, capsys):
    assert reconcile_cli.reconcile_main(cli_args(database, "--key", "nope")) == 2
    assert "error:" in capsys.readouterr().err
    assert reconcile_cli.reconcile_main(cli_args(tmp_path / "missing.duckdb")) == 2
    bad = tmp_path / "bad.yml"
    bad.write_text("- name: x\n  predicate: 'x = 1; DROP TABLE t'\n  verified_at: 2026-01-01\n", encoding="utf-8")
    assert reconcile_cli.reconcile_main(cli_args(database, "--rules", str(bad))) == 2
    assert "semicolon" in capsys.readouterr().err


def test_rules_check_command_exit_codes(database, tmp_path, capsys):
    ok = rules_file(tmp_path, TESTS)
    assert reconcile_cli.rules_main(["check", "--rules", ok, *cli_args(database)]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["flagged"] == 0
    stale = rules_file(tmp_path, KnownDiffRule("stale", "l.status = 'cancelled'", "2026-10-01"))
    assert reconcile_cli.rules_main(["check", "--rules", stale, *cli_args(database)]) == 1
    assert json.loads(capsys.readouterr().out)["rules"][0]["flags"] == ["no_longer_explains"]
    assert reconcile_cli.rules_main(["check", "--rules", str(tmp_path / "none.yml"), *cli_args(database)]) == 2
    assert reconcile_cli.rules_main(["check", "--rules", ok, *cli_args(database), "--sql-only"]) == 0
    assert "COUNTIF(kumo_mis AND kumo_p1) AS s1" in capsys.readouterr().out


def test_rules_list_command(tmp_path, capsys):
    assert reconcile_cli.rules_main(["list", "--rules", rules_file(tmp_path, REFUNDS, TESTS)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 2 and data["rules"][0] == REFUNDS.to_json()


def test_python_dash_m_runs_both_commands(database, capsys):
    from kumosql.__main__ import main

    assert main(["reconcile", *cli_args(database, "--ignore", "amount,amount_usd")]) == 1
    assert json.loads(capsys.readouterr().out)["keys"] == ["order_id"]
    with pytest.raises(SystemExit) as stop:
        main(["rules", "--help"])
    assert stop.value.code == 0 and "check" in capsys.readouterr().out


# ------------------------------------------------------------------ BigQuery


BQ_TABLES = {"`proj.ds.orders_v1`": "orders_v1", "`proj.ds.orders_v2`": "orders_v2", "`ds.orders_v2`": "orders_v2"}
BQ_TYPES = {"INTEGER": "INT64", "VARCHAR": "STRING", "DOUBLE": "FLOAT64", "BOOLEAN": "BOOL", "INTEGER[]": "INT64"}


class FakeBigQuery:
    """Answers BigQuery's dry runs and runs from DuckDB, translating the BigQuery SQL KumoSQL sends."""

    def __init__(self, connection, monkeypatch, fail=lambda sql: None, estimate=1234):
        self.connection, self.fail, self.estimate = connection, fail, estimate
        self.sent, self.planned, self.estimated = [], [], []
        state.set_section("bigquery", {"billingProject": "bill-proj"})
        monkeypatch.setitem(data_sources.RUNNERS, "bigquery_sql", self.runner)
        monkeypatch.setattr(dryrun, "dry_run", self.schema)
        monkeypatch.setattr(scope_queries, "dry_run", self.bytes)

    def duckdb_sql(self, sql):
        for quoted, name in BQ_TABLES.items():
            sql = sql.replace(quoted, name)
        return sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]

    def schema(self, sql, project, *, location=None, token=None, transport=None):
        self.planned.append((sql, project))
        rows = self.connection.execute(f"DESCRIBE {self.duckdb_sql(sql)}").fetchall()
        fields = tuple(dryrun.Field(str(r[0]), BQ_TYPES.get(str(r[1]), str(r[1])),
                                    "REPEATED" if str(r[1]).endswith("[]") else "NULLABLE") for r in rows)
        return dryrun.DryRunResult(ok=True, schema=fields, total_bytes_processed=0)

    def bytes(self, sql, column=None, *, project=None, max_bytes=None):
        validate_readonly_query(sql)
        self.estimated.append((sql, project, max_bytes))
        if self.estimate > max_bytes:
            raise scope_queries.QueryError(f"the query would process about {self.estimate} B, over the {max_bytes} B cap.")
        return {"columns": [], "column": "", "estimated_bytes": self.estimate, "max_bytes_billed": max_bytes}

    def runner(self, source, project, max_bytes):
        sql = source.query
        validate_readonly_query(sql)
        self.sent.append((sql, project, max_bytes))
        if (error := self.fail(sql)):
            raise error
        cursor = self.connection.execute(self.duckdb_sql(sql))
        names = [item[0] for item in cursor.description]
        rows = [[None if value is None else str(value) for value in row] for row in cursor.fetchall()]
        return data_sources.Run(names, rows, estimated_bytes=self.estimate, bytes_billed=10_485_760)


def test_bigquery_runs_the_comparison_as_one_capped_job_with_the_same_answer(db, monkeypatch):
    fake = FakeBigQuery(db, monkeypatch)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    bigquery = reconcile("proj.ds.orders_v1", "proj.ds.orders_v2", "order_id", executor, crosswalk=MAP,
                         rules=[REFUNDS, TESTS]).to_json()
    native = run(db, rules=[REFUNDS, TESTS]).to_json()
    same = lambda data: {k: v for k, v in data.items() if k not in ("left", "right", "dialect", "estimated_bytes", "bytes_billed")}  # noqa: E731
    assert same(bigquery) == same(native)
    assert len(fake.sent) == 1 and fake.sent[0][1:] == ("bill-proj", 50_000_000)  # one job, under the cap
    assert len(fake.planned) == 2  # the two column lists, read with free dry runs
    sql = fake.sent[0][0]
    sqlglot.parse_one(sql, read="bigquery")
    assert "`proj.ds.orders_v1`" in sql and "COUNTIF(" in sql and "l.`amount` IS DISTINCT FROM r.`amount_usd`" in sql
    assert bigquery["dialect"] == "bigquery" and bigquery["bytes_billed"] == 10_485_760


def test_bigquery_dry_run_estimates_and_never_runs(db, monkeypatch, capsys):
    fake = FakeBigQuery(db, monkeypatch)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    plan = plan_reconcile("proj.ds.orders_v1", "ds.orders_v2", "order_id", executor, crosswalk=MAP)
    assert estimate_bytes(plan, executor) == 1234 and fake.estimated[0][2] == 50_000_000
    assert reconcile_cli.reconcile_main(["--left", "proj.ds.orders_v1", "--right", "proj.ds.orders_v2", "--key", "order_id",
                                         "--project", "bill-proj", "--max-bytes", "50000000", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "estimated bytes processed: 1,234 (cap: 50,000,000); nothing was run" in out
    assert fake.sent == []


def test_bigquery_over_the_byte_cap_runs_nothing(db, monkeypatch, tmp_path, capsys):
    fake = FakeBigQuery(db, monkeypatch, estimate=10**12)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    plan = plan_reconcile("proj.ds.orders_v1", "proj.ds.orders_v2", "order_id", executor)
    with pytest.raises(ByteCapExceeded):
        estimate_bytes(plan, executor)
    assert fake.sent == []  # refused at the dry run: nothing ran
    FakeBigQuery(db, monkeypatch, fail=lambda sql: scope_queries.QueryError("over the 50.0 MB cap"))
    args = ["--left", "proj.ds.orders_v1", "--right", "proj.ds.orders_v2", "--key", "order_id", "--project", "bill-proj"]
    assert reconcile_cli.reconcile_main(args) == 3 and "cap" in capsys.readouterr().err
    assert reconcile_cli.rules_main(["check", "--rules", rules_file(tmp_path, TESTS), *args]) == 3


def test_bigquery_names_tables_strictly_and_needs_a_billing_project(db, monkeypatch):
    with pytest.raises(ReconcileError, match="billing project"):
        BigQueryExecutor()
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    assert executor.table_sql("ds.orders") == "`ds.orders`" and executor.table_sql("my-proj.ds.t") == "`my-proj.ds.t`"
    for bad in ("orders", "p.d.t`; DROP TABLE x --", "a.b.c.d"):
        with pytest.raises(ReconcileError, match="project.dataset.table"):
            executor.table_sql(bad)


def test_bigquery_compares_arrays_and_structs_as_json_and_samples_with_array_agg(db, monkeypatch):
    FakeBigQuery(db, monkeypatch)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    monkeypatch.setattr(executor, "columns", lambda source: [Column("id", "INT64"), Column("tags", "STRING", True),
                                                             Column("info", "STRUCT<a INT64>")])
    sql = build_reconcile_sql("proj.ds.a", "proj.ds.b", "id", executor, include_samples=True, sample_limit=3)
    sqlglot.parse_one(sql, read="bigquery")
    validate_readonly_query(sql)
    assert "TO_JSON_STRING(l.`tags`) IS DISTINCT FROM TO_JSON_STRING(r.`tags`)" in sql
    assert "TO_JSON_STRING(l.`info`) IS DISTINCT FROM TO_JSON_STRING(r.`info`)" in sql
    assert "ARRAY_AGG(IF(kumo_hl AND NOT kumo_hr AND kumo_mis AND kumo_rule = 0, STRUCT(kumo_k0 AS `id`), NULL) " \
           "IGNORE NULLS ORDER BY kumo_k0 LIMIT 3)" in sql
