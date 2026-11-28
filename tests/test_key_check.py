"""key-check: duplicate groups, NULL keys and a proposed join's fan-out, in one read-only query.

Every number is checked against a count done by hand (and the join rows against DuckDB running the join itself).
BigQuery is faked as in the reconcile tests: the BigQuery SQL KumoSQL sends is translated and run on DuckDB.
"""

import json

import duckdb
import pytest
import sqlglot

from kumosql import data_profile, join_sql, scope_queries
from kumosql import reconcile_cli
from kumosql.key_check import build_key_check_sql, key_check, plan_key_check
from kumosql.reconcile import BigQueryExecutor, ByteCapExceeded, DuckDBExecutor, ReconcileError, estimate_bytes
from kumosql.sql_validation import validate_readonly_query
from test_reconcile import FakeBigQuery, sql_guard_problems

ORDERS = [  # order_id, line, customer_id, region: order 2 has two lines, order 6 three; one NULL customer, two NULL regions
    (1, 1, 10, "n"), (2, 1, 10, "n"), (2, 2, 11, "s"), (3, 1, 12, None), (4, 1, None, "s"),
    (5, 1, 99, "n"), (6, 1, 98, "e"), (6, 2, 98, "e"), (6, 3, 98, None),
]
CUSTOMERS = [(10, "n", "a"), (11, "s", "b"), (11, "s", "b2"), (12, "w", "c"), (13, "e", "d"), (None, "x", "z")]


def make_tables(connection):
    connection.execute("CREATE TABLE orders (order_id INTEGER, line INTEGER, customer_id INTEGER, region VARCHAR)")
    connection.execute("CREATE TABLE customers (id INTEGER, region VARCHAR, name VARCHAR)")
    connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", ORDERS)
    connection.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)


@pytest.fixture()
def db():
    connection = duckdb.connect()
    make_tables(connection)
    yield connection
    connection.close()


@pytest.fixture()
def database(tmp_path):
    path = tmp_path / "shop.duckdb"
    connection = duckdb.connect(str(path))
    make_tables(connection)
    connection.close()
    return path


def check(db, table, keys, **options):
    return key_check(table, keys, DuckDBExecutor(db), **options).to_json()


# --------------------------------------------------------- duplicates and NULLs


def test_a_unique_key_that_is_never_null_is_ok(db):
    data = check(db, "orders", "order_id, line")
    assert (data["rows"], data["distinct_keys"], data["unique"], data["ok"], data["problems"]) == (9, 9, True, True, [])
    assert data["duplicates"] == {"groups": 0, "rows": 0, "extra_rows": 0, "max_rows_per_key": 1, "largest_groups": []}
    assert data["nulls"] == {"rows": 0, "by_column": {"order_id": 0, "line": 0}}
    assert data["join"] is None and data["samples_included"] is False and data["key"] == ["order_id", "line"]
    assert json.loads(json.dumps(data)) == data


def test_duplicate_groups_are_sized_without_key_values_unless_samples_are_asked_for(db):
    data = check(db, "orders", "order_id")
    assert data["duplicates"] == {"groups": 2, "rows": 5, "extra_rows": 3, "max_rows_per_key": 3,
                                  "largest_groups": [{"rows": 3}, {"rows": 2}]}
    assert (data["unique"], data["ok"], data["problems"], data["distinct_keys"]) == (False, False, ["duplicate_keys"], 6)
    sql = build_key_check_sql("orders", "order_id", DuckDBExecutor(db))
    assert "'order_id'" not in sql and " AS tk" not in sql  # no key value is selected at all
    sampled = check(db, "orders", "ORDER_ID", include_samples=True)
    assert sampled["duplicates"]["largest_groups"] == [{"rows": 3, "key": {"order_id": 6}}, {"rows": 2, "key": {"order_id": 2}}]
    assert sampled["samples_included"] is True and sampled["key"] == ["order_id"]
    assert check(db, "orders", "order_id", top=1)["duplicates"]["largest_groups"] == [{"rows": 3}]


def test_ties_are_listed_in_key_order_so_sizes_and_keys_line_up(db):
    data = check(db, "orders", "region", include_samples=True)
    # n three times, s and e twice each; the two NULL regions are not a group
    assert data["duplicates"]["largest_groups"] == [{"rows": 3, "key": {"region": "n"}}, {"rows": 2, "key": {"region": "e"}},
                                                    {"rows": 2, "key": {"region": "s"}}]
    assert (data["duplicates"]["groups"], data["duplicates"]["rows"], data["nulls"]["rows"]) == (3, 7, 2)


def test_null_keys_are_counted_per_column_and_never_as_duplicates(db):
    single = check(db, "orders", "customer_id")
    assert single["nulls"] == {"rows": 1, "by_column": {"customer_id": 1}}
    assert single["problems"] == ["duplicate_keys", "null_keys"]
    assert any("NULL in a key column" in note for note in single["notes"])
    pair = check(db, "orders", "customer_id,region")
    assert pair["nulls"] == {"rows": 3, "by_column": {"customer_id": 1, "region": 2}}  # (4), (3) and (6, line 3)
    assert pair["duplicates"]["groups"] == 2 and pair["duplicates"]["rows"] == 4  # (10, n) and (98, e)
    assert pair["distinct_keys"] == 4  # (10, n), (11, s), (99, n) and (98, e); (12, NULL) is not a key value
    db.execute("CREATE TABLE only_nulls AS SELECT CAST(NULL AS INTEGER) AS k FROM range(3)")
    data = check(db, "only_nulls", "k")
    assert (data["duplicates"]["groups"], data["nulls"]["rows"], data["distinct_keys"], data["problems"]) == (0, 3, 0, ["null_keys"])


# ----------------------------------------------------------------- the join


def test_fan_out_of_a_proposed_join_matches_the_join_itself(db):
    data = check(db, "orders", "customer_id", join="customers", on="customer_id=id")
    join = data["join"]
    inner = db.execute("SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id").fetchone()[0]
    left = db.execute("SELECT COUNT(*) FROM orders o LEFT JOIN customers c ON o.customer_id = c.id").fetchone()[0]
    assert (inner, left) == (5, 10)
    assert join == {
        "other": "customers", "on": {"customer_id": "id"}, "cardinality": "N:M",
        "other_rows": 6, "other_null_key_rows": 1, "other_distinct_keys": 4, "other_duplicate_groups": 1,
        "other_max_rows_per_key": 2, "matched_keys": 3,  # 10, 11 and 12
        "unmatched": {"rows": 5, "keys": 2, "other_rows": 2, "other_keys": 1},  # NULL, 99, 98 x3; customer 13 and NULL
        "fan_out": {"max_rows_per_key": 2, "keys_with_several_rows": 1, "rows_multiplied": 1,  # customer 11 twice
                    "inner_join_rows": inner, "left_join_rows": left, "added_rows": left - len(ORDERS)},
    }
    assert data["problems"] == ["duplicate_keys", "null_keys", "join_fans_out"]
    assert any("a join repeats 1 row(s) of the table and adds 1" in note for note in data["notes"])
    assert any("--join table have a NULL" in note for note in data["notes"])


def test_key_check_is_join_profile_plus_duplicate_groups(db):
    """The same groups, the same SQL pieces and the same numbers as join_profile, not a third copy.

    join_sql is the module join_profile runs (it registers no MCP tool, so importing it here leaves the agent
    server's tool order alone)."""

    ours = check(db, "orders", "customer_id", join="customers", on="customer_id=id")["join"]
    theirs = join_sql.profile_join("orders", "customers", [("customer_id", "id")],
                                           data_profile.DuckDBExecutor(db)).to_json()
    assert (ours["matched_keys"], ours["fan_out"]["inner_join_rows"], ours["cardinality"]) == (
        theirs["matched_keys"], theirs["join_rows"], theirs["cardinality"])
    assert ours["other_max_rows_per_key"] == theirs["sides"]["right"]["max_rows_per_key"]
    sql = build_key_check_sql("orders", "customer_id", DuckDBExecutor(db), join="customers", on="customer_id=id")
    for piece in (join_sql.grouped_cte("l", 1), join_sql.grouped_cte("r", 1)):
        assert piece in sql
    assert "lg a JOIN rg b ON a.k0 = b.k0" in sql and sql.count("FROM \"orders\"") == 1


def test_a_join_that_repeats_no_row_is_ok_and_same_named_keys_need_no_on(db):
    lookup = "SELECT * FROM customers WHERE name <> 'b2' AND id IS NOT NULL"  # one row per customer: a query works too
    data = check(db, "orders", "order_id,line", join=lookup, on="order_id=id, line=id")
    assert data["ok"] and data["join"]["fan_out"]["keys_with_several_rows"] == 0
    data = check(db, "orders", "customer_id", join=lookup, on="customer_id=id")
    assert "join_fans_out" not in data["problems"] and data["join"]["cardinality"] == "N:1"
    assert data["join"]["fan_out"]["added_rows"] == 0 and data["join"]["other_duplicate_groups"] == 0
    same = check(db, "orders", "region", join="customers")
    assert same["join"]["on"] == {"region": "region"} and same["join"]["matched_keys"] == 3  # n, s and e
    assert same["join"]["fan_out"]["keys_with_several_rows"] == 1  # s: two customers


@pytest.mark.parametrize("keys,options,message", [
    ("nope", {}, "the table has no column 'nope'"),
    ("customer_id", {"on": "customer_id=id"}, "give --join too"),
    ("customer_id", {"join": "customers", "on": "region=region"}, "not one of the --key columns"),
    ("customer_id", {"join": "customers", "on": "customer_id"}, "key_column=other_column"),
    ("customer_id", {"join": "customers", "on": "customer_id=nope"}, "the --join table has no column 'nope'"),
    ("customer_id=id", {}, "map them to the other table's with --on"),
    ("order_id,ORDER_ID", {}, "names a column twice"),
    ("a,b,c,d,e,f,g", {}, "at most 6"),
    (" , ", {}, "at least one column"),
    ("order_id", {"top": 0}, "--top"),
    ("order_id", {"top": 101}, "--top"),
])
def test_input_errors_say_what_to_change(db, keys, options, message):
    with pytest.raises(ReconcileError, match=message):
        check(db, "orders", keys, **options)


def test_a_nested_key_is_refused_and_a_float_key_is_flagged(db):
    db.execute("CREATE TABLE nested AS SELECT {'a': 1} AS s, [1] AS tags, 1.5::DOUBLE AS f")
    for name in ("s", "tags"):
        with pytest.raises(ReconcileError, match=f"key column {name} is"):
            check(db, "nested", name)
    assert any("floating-point" in note for note in check(db, "nested", "f")["notes"])


def test_the_generated_query_is_one_read_only_select_that_passes_the_sql_guards(db, monkeypatch):
    duck = [build_key_check_sql("orders", "order_id", DuckDBExecutor(db)),
            build_key_check_sql("orders", "customer_id,region", DuckDBExecutor(db), join="customers",
                                on="customer_id=id", include_samples=True)]
    for sql in duck:
        validate_readonly_query(sql)
        assert sql_guard_problems(sql, "duckdb") == [] and "FULL" not in sql.upper().split("FROM", 1)[1]
    FakeBigQuery(db, monkeypatch)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    sql = build_key_check_sql("proj.ds.orders", "customer_id,region", executor, join="proj.ds.customers",
                              on="customer_id=id", include_samples=True)
    sqlglot.parse_one(sql, read="bigquery")
    validate_readonly_query(sql)
    assert sql_guard_problems(sql, "bigquery") == []
    assert "TO_JSON_STRING(ARRAY_AGG(IF(ok AND n > 1, STRUCT(n AS `n`), NULL) IGNORE NULLS ORDER BY n DESC, k0, k1 LIMIT 5))" in sql


# ------------------------------------------------------------------ BigQuery


def test_bigquery_runs_one_capped_job_with_the_same_answer(db, monkeypatch):
    fake = FakeBigQuery(db, monkeypatch)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    bigquery = key_check("proj.ds.orders", "customer_id", executor, join="proj.ds.customers", on="customer_id=id",
                         include_samples=True).to_json()
    native = check(db, "orders", "customer_id", join="customers", on="customer_id=id", include_samples=True)
    drop = ("table", "dialect", "estimated_bytes", "bytes_billed")
    same = lambda data: {k: v for k, v in data.items() if k not in drop} | {"join": {**data["join"], "other": None}}  # noqa: E731
    assert same(bigquery) == same(native)
    assert len(fake.sent) == 1 and fake.sent[0][1:] == ("bill-proj", 50_000_000)  # one job, under the cap
    assert len(fake.planned) == 2 and bigquery["dialect"] == "bigquery" and bigquery["bytes_billed"] == 10_485_760


def test_bigquery_dry_run_estimates_and_over_the_cap_runs_nothing(db, monkeypatch):
    fake = FakeBigQuery(db, monkeypatch)
    executor = BigQueryExecutor("bill-proj", 50_000_000)
    plan = plan_key_check("proj.ds.orders", "order_id", executor)
    assert estimate_bytes(plan, executor) == 1234 and fake.sent == []
    FakeBigQuery(db, monkeypatch, estimate=10**12)
    with pytest.raises(ByteCapExceeded):
        estimate_bytes(plan_key_check("proj.ds.orders", "order_id", executor), executor)


# -------------------------------------------------------------- command line


def test_exit_codes(database, tmp_path, monkeypatch, capsys, db):
    base = ["--duckdb", str(database)]
    target = tmp_path / "out.json"
    assert reconcile_cli.key_check_main(["orders", "--key", "order_id,line", *base, "-o", str(target)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and json.loads(target.read_text(encoding="utf-8")) == data
    assert reconcile_cli.key_check_main(["orders", "--key", "order_id", *base]) == 1  # repeats
    assert json.loads(capsys.readouterr().out)["problems"] == ["duplicate_keys"]
    assert reconcile_cli.key_check_main(["orders", "--key", "order_id,line", "--join", "customers", "--on",
                                         "order_id=id,line=id", *base]) == 0
    capsys.readouterr()
    assert reconcile_cli.key_check_main(["orders", "--key", "customer_id", "--join", "customers", "--on",
                                         "customer_id=id", "--include-samples", "--top", "1", *base]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["duplicates"]["largest_groups"] == [{"rows": 3, "key": {"customer_id": 98}}]
    assert reconcile_cli.key_check_main(["orders", "--key", "nope", *base]) == 2  # an input error
    assert "error:" in capsys.readouterr().err
    assert reconcile_cli.key_check_main(["orders", "--key", "order_id", "--duckdb", str(tmp_path / "none.duckdb")]) == 2
    with pytest.raises(SystemExit) as stop:  # a usage error: --key is required
        reconcile_cli.key_check_main(["orders", *base])
    assert stop.value.code == 2
    capsys.readouterr()
    FakeBigQuery(db, monkeypatch, fail=lambda sql: scope_queries.QueryError("over the 50.0 MB cap"))
    assert reconcile_cli.key_check_main(["proj.ds.orders", "--key", "order_id", "--project", "bill-proj"]) == 3
    assert "cap" in capsys.readouterr().err


def test_the_command_prints_the_query_without_running_it(database, db, monkeypatch, capsys):
    assert reconcile_cli.key_check_main(["orders", "--key", "order_id", "--duckdb", str(database), "--sql-only"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("WITH l AS") and out.rstrip().endswith(";")
    assert reconcile_cli.key_check_main(["orders", "--key", "order_id", "--duckdb", str(database), "--dry-run"]) == 0
    assert "DuckDB: nothing was run" in capsys.readouterr().out
    fake = FakeBigQuery(db, monkeypatch)
    assert reconcile_cli.key_check_main(["proj.ds.orders", "--key", "order_id", "--project", "bill-proj",
                                         "--max-bytes", "50000000", "--dry-run"]) == 0
    assert "estimated bytes processed: 1,234 (cap: 50,000,000); nothing was run" in capsys.readouterr().out
    assert fake.sent == []


def test_python_dash_m_runs_key_check(database, capsys):
    from kumosql.__main__ import main

    assert main(["key-check", "orders", "--key", "order_id", "--duckdb", str(database)]) == 1
    assert json.loads(capsys.readouterr().out)["duplicates"]["groups"] == 2
