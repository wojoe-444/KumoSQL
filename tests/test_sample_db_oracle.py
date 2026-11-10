"""Sample databases eval, Oracle schemas (HR, Customer Orders): the Oracle readers, the loads, pairs and rewrites.

The full run is ``python tools/sample_db_bench.py --database oracle_hr --database oracle_co --write-results``.
The shared checks of ``test_sample_db_bench.py`` (every pair of every database with its floors, the load against
upstream) also cover both schemas. Here: the Oracle readers, the three HR pairs whose counterexamples once repeated
a DATE key, a pinned subset of the workload through the rewrite pipeline, lift_subqueries and (for the
key-dependent ones) the proof-gated optimizer, and the results files.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("sample_db_bench_oracle", ROOT / "tools" / "sample_db_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["sample_db_bench_oracle"] = bench
_spec.loader.exec_module(bench)

ORACLE = ["oracle_co", "oracle_hr"]
# Pairs on the DATE key of job_history: the bounded checker used to render a DATE below its first day as 0001-01-01,
# so two rows that differ in the key came out equal and the counterexample repeated the primary key. They are refuted
# with a counterexample that replays on a legal database.
DATE_KEY_PAIRS = {
    "hr-left-join-elimination-not-unique",
    "hr-self-join-on-part-of-key",
    "hr-distinct-on-part-of-key",
}
SUBSET = {
    "oracle_hr": [
        "hr-view-emp-details-view",
        "hr-a10-not-in-with-nulls",
        "hr-a21-cte-chain-unused-cte",
        "hr-a22-derived-table-trivial-predicate",
    ],
    "oracle_co": [
        "co-view-product-reviews",
        "co-view-store-orders",
        "co-a09-not-in-with-nulls",
        "co-a20-cte-chain-unused-cte",
    ],
}
KEYED = {
    "oracle_hr": ["hr-a05-distinct-on-key", "hr-a26-job-history-composite-key"],
    "oracle_co": ["co-a05-distinct-on-composite-key"],
}


def test_the_oracle_schemas_have_results_files_of_their_own():
    assert all(n in bench.ADAPTERS and n not in bench.COMBINED for n in ORACLE)
    # each database's two rows (rewrites at the order, pairs one after) sit apart from every other database's
    slots = [o for n, a in bench.ADAPTERS.items() if n not in bench.COMBINED for o in (a.results_order, a.results_order + 1)]
    assert all(bench.ADAPTERS[n].results_order for n in ORACLE) and len(slots) == len(set(slots))


@pytest.mark.parametrize("name", ORACLE)
def test_oracle_databases_load_as_the_scripts_declare_them(name):
    adapter = bench.ADAPTERS[name]
    report = bench.check_database(adapter)
    assert report["problems"] == []
    assert report["counts"] == adapter.published_counts
    assert report["declared"]["primary_keys"] == report["tables"] == 7
    # every upstream file is pinned, with the licence next to it
    assert any(u.local == "upstream/LICENSE.txt" for u in adapter.upstream)
    assert (adapter.folder / "adapted" / "schema.sql").read_text().startswith("-- ADAPTED, not upstream.")


def test_the_first_values_of_each_table_are_the_scripts():
    hr = bench.ADAPTERS["oracle_hr"].connect()
    assert hr.execute("SELECT employee_id, last_name, hire_date, salary, manager_id FROM employees WHERE employee_id = 101").fetchall() == [
        (101, "Yang", __import__("datetime").date(2015, 9, 21), __import__("decimal").Decimal("17000.00"), 100)
    ]
    assert hr.execute("SELECT COUNT(*) FROM employees WHERE department_id IS NULL").fetchone() == (1,)
    co = bench.ADAPTERS["oracle_co"].connect()
    # TO_TIMESTAMP('04-FEB-2021 13.20.22.245676861', ...): nine fractional digits, kept to the microsecond
    assert co.execute("SELECT order_tms FROM orders WHERE order_id = 1").fetchone()[0].isoformat() == "2021-02-04T13:20:22.245676"
    # the INSERTs that leave the identity column out are numbered 1, 2, ... in script order
    assert co.execute("SELECT MIN(inventory_id), MAX(inventory_id), COUNT(DISTINCT inventory_id) FROM inventory").fetchone() == (1, 566, 566)
    # product 4's JSON is a PL/SQL variable split over two strings with chr(10) between them
    details = json.loads(co.execute("SELECT product_details FROM products WHERE product_id = 4").fetchone()[0])
    assert details["colour"] == "white" and len(details["reviews"]) == 30


def test_the_oracle_readers_handle_the_sqlplus_scripts(tmp_path):
    (tmp_path / "create.sql").write_text(
        """
rem a comment with an apostrophe: don't
CREATE TABLE a ( id NUMBER CONSTRAINT id_nn NOT NULL, n VARCHAR2(5 CHAR) NOT NULL
  , CONSTRAINT a_pk PRIMARY KEY (id) ) ORGANIZATION INDEX;
CREATE TABLE b ( x NUMBER(4), y NUMBER, z DATE
  , CONSTRAINT b_chk CHECK (x > 0), CONSTRAINT b_uk UNIQUE (z) );
ALTER TABLE b ADD ( CONSTRAINT b_pk PRIMARY KEY (x)
  , CONSTRAINT b_fk FOREIGN KEY (y) REFERENCES a );
CREATE OR REPLACE VIEW v (id) AS SELECT id FROM a WITH READ ONLY;
""",
        encoding="utf-8",
    )
    (tmp_path / "populate.sql").write_text(
        """
REM *** insert data, it's here
DECLARE
  js VARCHAR2(32767);
BEGIN
  INSERT INTO a VALUES (1, 'x');
  js := 'p''q' || chr(10) ||
'r';
  INSERT INTO b VALUES (7, 1, TO_DATE('17-06-2013', 'dd-MM-yyyy'));
  INSERT INTO c (k, v) VALUES (1, UTL_RAW.CAST_TO_RAW( js ) );
END;
/
""",
        encoding="utf-8",
    )

    class Tiny(bench.OracleSample):
        name, ddl_file, data_file = "tiny", "create.sql", "populate.sql"
        folder = property(lambda self: tmp_path)

    tiny = Tiny()
    tables = tiny.upstream_tables()
    assert list(tables["a"].columns) == ["id", "n"] and tables["a"].not_null == {"id", "n"}
    assert tables["b"].primary_key == ("x",) and tables["b"].foreign_keys == [(("y",), "a", ("id",))]
    assert list(tiny.upstream_views()) == ["v"]
    rows = tiny.upstream_rows()
    assert sorted(rows) == ["a", "b", "c"]
    (_, date), = [(c, v[2]) for c, v in rows["b"]]
    assert date == bench.Raw("call", "TO_DATE", (bench.Raw("string", "17-06-2013"), bench.Raw("string", "dd-MM-yyyy")))
    assert tiny.evaluate(date) == bench.Raw("string", "2013-06-17")
    (_, (_, blob)), = rows["c"]
    assert tiny.evaluate(blob) == bench.Raw("string", "p'q\nr")  # the variable is inlined, concatenation done
    stamp = bench.Raw("call", "TO_TIMESTAMP", (bench.Raw("string", "04-FEB-2021 13.20.22.245676861"), bench.Raw("string", "DD-MON-YYYY HH24.MI.SS.FF")))
    assert tiny.evaluate(stamp) == bench.Raw("string", "2021-02-04 13:20:22.245676")
    with pytest.raises(ValueError):
        tiny.evaluate(bench.Raw("call", "SYSDATE", ()))


def test_foreign_keys_without_a_column_list_name_the_parents_primary_key():
    hr = bench.ADAPTERS["oracle_hr"].upstream_tables()
    assert sorted(hr["employees"].foreign_keys) == [
        (("department_id",), "departments", ("department_id",)),
        (("job_id",), "jobs", ("job_id",)),
        (("manager_id",), "employees", ("employee_id",)),
    ]
    assert hr["job_history"].primary_key == ("employee_id", "start_date")
    assert (("manager_id",), "employees", ("employee_id",)) in hr["departments"].foreign_keys
    co = bench.ADAPTERS["oracle_co"].upstream_tables()
    assert co["order_items"].primary_key == ("order_id", "line_item_id")


def test_the_upstream_views_are_the_scripts_views():
    assert sorted(bench.ADAPTERS["oracle_hr"].upstream_views()) == ["emp_details_view"]
    assert sorted(bench.ADAPTERS["oracle_co"].upstream_views()) == [
        "customer_order_products",
        "product_orders",
        "product_reviews",
        "store_orders",
    ]
    for name in ORACLE:
        adapter = bench.ADAPTERS[name]
        views = adapter.upstream_views()
        for query in adapter.workload():
            assert query["origin"] in ("upstream-view", "authored")
            assert query["origin"] == "authored" or (query["adaptation"] and query["name"] in views)
        assert {q["name"] for q in adapter.workload() if q["origin"] == "upstream-view"} == set(views)


def test_a_changed_oracle_declaration_is_caught():
    class Drifted(bench.OracleCO):
        def schema(self):
            schema = super().schema()
            schema["orders"].foreign_keys.pop()
            schema["customers"].not_null.discard("email_address")
            return schema

    problems = bench.check_database(Drifted())["problems"]
    assert any("orders: foreign keys" in p for p in problems)
    assert any("customers: NOT NULL" in p for p in problems)


def test_the_date_key_pairs_are_refuted_with_counterexamples_that_replay():
    rows = bench.run_pairs([bench.ADAPTERS["oracle_hr"]], jobs=2, pair_ids=DATE_KEY_PAIRS)
    assert {r["id"] for r in rows} == DATE_KEY_PAIRS
    for row in rows:
        assert row["label"] == "different" and row["outcome"] == "refuted" and row["wrong"] == "", row
        assert row["counterexample_ok"] is True


@pytest.mark.parametrize("name", sorted(SUBSET))
def test_rewrites_keep_the_results_on_the_real_oracle_data(name):
    rows = bench.rewrite_cases((name, SUBSET[name], False))
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(SUBSET[name]) and summary["unsupported"] == 0
    assert summary["verified"] > 0


@pytest.mark.parametrize("name", sorted(KEYED))
def test_the_optimizer_uses_the_declared_keys_on_the_real_oracle_data(name):
    rows = bench.rewrite_cases((name, KEYED[name], True))
    assert not [r for r in rows if r["status"] == "wrong"]
    changed = {r["query"] for r in rows if r["stage"] == "optimizer" and r["status"] == "transformed"}
    assert changed == set(KEYED[name])  # DISTINCT dropped on the (composite) primary key


def test_oracle_results_files_say_what_they_measured():
    results = ROOT / "benchmarks" / "results"
    for name in ORACLE:
        rewrites = json.loads((results / f"sample-databases-{name}-rewrites.json").read_text())
        pairs = json.loads((results / f"sample-databases-{name}-pairs.json").read_text())
        for row in (rewrites, pairs):
            assert row["command"] == f"python tools/sample_db_bench.py --database {name} --write-results"
            assert row["docs"] == "docs/evals/sample-databases.md"
        # no wrong answer is recorded and no list of known failures is kept
        assert rewrites["score"].startswith("0 wrong")
        assert pairs["score"].endswith(", 0 wrong") and "known failure" not in pairs["caveats"].lower()
        assert "6660bad68c" in rewrites["caveats"]
    # the pairs file of HR records the DATE bug as fixed on master, not as a failure
    hr = json.loads((results / "sample-databases-oracle_hr-pairs.json").read_text())
    assert hr["score"] == "18/22 equivalent proved, 32/32 different refuted (replayed), 0 wrong"
    assert "DATE" in hr["caveats"] and "start_date" in hr["caveats"]
    # the first two databases' rows are the ones recorded before the Oracle schemas existed
    first = json.loads((results / "sample-databases-rewrites.json").read_text())
    assert first["score"] == "0 wrong in 510 executed; 242 rewrites verified on the real data" and first["size"] == 515
    first = json.loads((results / "sample-databases-pairs.json").read_text())
    assert first["score"].startswith("20/24 equivalent proved, 28/30 different refuted") and first["size"] == 54
