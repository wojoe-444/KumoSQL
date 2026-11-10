"""Sample databases eval (Chinook, Northwind, Sakila): loads checked against upstream, rewrites and pairs with 0 wrong.

The full run is ``python tools/sample_db_bench.py --write-results`` (every workload query through every
rewrite stage, including the slow proof-gated optimizer). Here every pair runs, and a pinned subset of the
workload runs through the rewrite pipeline and lift_subqueries.
"""

import datetime
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
_spec = importlib.util.spec_from_file_location(
    "sample_db_bench", ROOT / "tools" / "sample_db_bench.py"
)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sample_db_bench"] = bench
_spec.loader.exec_module(bench)

# floors only ever go up (recorded run: 20 proved, 27 refuted; 28 on an idle machine, see the docs); they cover the
# combined Chinook and Northwind results. Every further database has floors of its own.
FLOORS = {"proven": 20, "refuted": 27}
DATABASE_FLOORS = {
    "sakila": {"proven": 23, "refuted": 36},
    "oracle_hr": {"proven": 18, "refuted": 30},
    "oracle_co": {"proven": 18, "refuted": 27},
}
# Siblings the bounded checker cannot decide: Oracle CO's stores has a BYTES column (logo) and the checker has no NULL
# padding for that type in a LEFT JOIN, so it raises KeyError (a crash is counted as unknown, not as a refutation).
UNDECIDED_SIBLINGS = {
    "co-left-join-elimination-without-key": "bounded checker crashed: KeyError"
}
# workload queries the pipeline and lift_subqueries change; upstream and authored, both databases
SUBSET = {
    "chinook": [
        "ch-test-invoices-without-lines",
        "ch-a18-cte-best-sellers",
        "ch-a19-derived-table-trivial-predicate",
        "ch-a24-not-in-null-trap",
    ],
    "northwind": [
        "nw-view-invoices",
        "nw-view-category-sales-for-1997",
        "nw-a10-not-in-with-nulls",
        "nw-a21-cte-chain",
    ],
    "sakila": [
        "sk-view-actor-info",
        "sk-routine-rewards-report-rewardees",
        "sk-a20-cte-chain-with-unused",
        "sk-a22-derived-table-trivial",
    ],
}


@pytest.mark.parametrize("name", sorted(bench.ADAPTERS))
def test_database_loads_as_upstream_declares_it(name):
    adapter = bench.ADAPTERS[name]
    report = bench.check_database(adapter)
    assert report["problems"] == []
    counts = report["counts"]
    # an adapter whose upstream publishes only some counts (Pagila) is checked on those
    if not adapter.published_counts_complete:
        counts = {table: counts[table] for table in adapter.published_counts}
    assert counts == adapter.published_counts
    assert report["declared"]["primary_keys"] == report["tables"]


def test_the_upstream_workload_is_upstream():
    northwind = bench.ADAPTERS["northwind"]
    views = bench.read_views(northwind.upstream_text())
    assert len(views) == 16
    workload = northwind.workload()
    assert sorted(
        q["name"] for q in workload if q["origin"] == "upstream-view"
    ) == sorted(views)
    for adapter in bench.ADAPTERS.values():
        for query in adapter.workload():
            assert query["origin"] in (
                "upstream-view",
                "upstream-procedure",
                "upstream-test",
                "upstream-readme",
                "authored",
            )
            assert query["origin"] == "authored" or query["adaptation"], query["id"]


def test_sakila_views_are_upstreams_and_film_text_is_the_trigger_output():
    sakila = bench.ADAPTERS["sakila"]
    views = sakila.upstream_views()
    assert sorted(views) == sorted(
        q["name"] for q in sakila.workload() if q["origin"] == "upstream-view"
    )
    assert len(views) == 7
    rows = sakila.rows()
    assert len(rows["film_text"]) == len(rows["film"]) == 1000
    # nothing in the data script inserts into film_text: the ins_film trigger of the schema script does
    assert "film_text" not in bench.read_inserts(sakila.upstream_text())
    # the executable comment /*!50705 ... */ is read as MySQL 5.7.5 and later run it
    assert "location" in sakila.schema()["address"].columns
    assert (
        sakila.schema()["address"].columns["location"] == "BYTES"
        and len(rows["address"][0]) == 9
    )


def test_readers_handle_the_mysql_dump_dialect():
    sql = """
    CREATE TABLE t (
      id SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
      rate DECIMAL(4,2) NOT NULL DEFAULT 4.99,
      kind ENUM('G','PG') DEFAULT 'G',
      PRIMARY KEY  (id),
      FULLTEXT KEY idx (kind),
      SPATIAL KEY idx_location (rate),
      UNIQUE KEY u (rate)
    )ENGINE=InnoDB DEFAULT CHARSET=utf8;
    CREATE TABLE u (id INT NOT NULL, t_id SMALLINT UNSIGNED NOT NULL, PRIMARY KEY (id),
      CONSTRAINT `fk_u_t` FOREIGN KEY (t_id) REFERENCES t (id) ON DELETE RESTRICT ON UPDATE CASCADE);
    INSERT INTO t VALUES (1,'2.99','PG'),(2,3,NULL);
    """
    tables = bench.read_ddl(sql)
    assert list(tables["t"].columns) == ["id", "rate", "kind"]
    assert tables["t"].columns["id"] == "SMALLINT UNSIGNED"
    assert tables["t"].columns["rate"] == "DECIMAL(4, 2)"
    assert tables["t"].primary_key == ("id",) and tables["t"].not_null == {"id", "rate"}
    assert tables["u"].foreign_keys == [(("t_id",), "t", ("id",))]
    assert [v.text for _, values in bench.read_inserts(sql)["t"] for v in values] == [
        "1",
        "2.99",
        "PG",
        "2",
        "3",
        None,
    ]


def test_a_changed_declaration_is_caught():
    class Drifted(bench.Northwind):
        def schema(self):
            schema = super().schema()
            schema["Orders"].foreign_keys.pop()
            schema["Products"].not_null.discard("ProductName")
            return schema

    problems = bench.check_database(Drifted())["problems"]
    assert any("Orders: foreign keys" in p for p in problems)
    assert any("Products: NOT NULL" in p for p in problems)


def test_readers_handle_the_sample_dialects():
    sql = """
    CREATE TABLE [dbo].[T] ([a] [int] NOT NULL, "b" nvarchar (10) NULL, c NUMERIC(10,2),
      CONSTRAINT "PK_T" PRIMARY KEY CLUSTERED ("a"));
    CREATE TABLE U (x INTEGER NOT NULL PRIMARY KEY, y INT REFERENCES T (a));
    ALTER TABLE U ADD CONSTRAINT fk FOREIGN KEY ([x]) REFERENCES [dbo].[T] ([a]) ON [PRIMARY]
    GO
    INSERT "T"("a","b","c") VALUES(1,N'it''s',-2.5)
    INSERT INTO [T] ([a], [b], [c]) VALUES (2, NULL, 0x1F), (3, 'x', 4);
    Insert Into U Values (7, 1)
    """
    tables = bench.read_ddl(sql)
    assert (
        list(tables["T"].columns) == ["a", "b", "c"]
        and tables["T"].columns["c"] == "NUMERIC(10, 2)"
    )
    assert tables["T"].primary_key == ("a",) and tables["T"].not_null == {"a"}
    assert tables["U"].primary_key == ("x",)
    assert sorted(tables["U"].foreign_keys) == [
        (("x",), "T", ("a",)),
        (("y",), "T", ("a",)),
    ]
    rows = bench.read_inserts(sql)
    assert [values[1].text for _, values in rows["T"]] == ["it's", None, "x"]
    assert rows["T"][0][1][2].text == "-2.5" and rows["T"][1][1][2] == bench.Raw(
        "hex", "1F"
    )
    assert rows["U"] == [(None, (bench.Raw("number", "7"), bench.Raw("number", "1")))]


def test_every_pair_is_decided_without_a_wrong_answer():
    rows = bench.run_pairs(list(bench.ADAPTERS.values()))
    summary = bench.summarize_pairs(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["wrong"]]
    assert summary["labels_unverified"] == 0, [
        r["id"] for r in rows if r["witness_ok"] is False
    ]
    combined = bench.summarize_pairs(
        [r for r in rows if r["database"] in bench.COMBINED]
    )
    assert (
        combined["all"]["proven"] >= FLOORS["proven"]
        and combined["all"]["refuted"] >= FLOORS["refuted"]
    )
    for name, floors in DATABASE_FLOORS.items():
        own = bench.summarize_pairs(rows)["by_database"][name]
        assert (
            own["proven"] >= floors["proven"] and own["refuted"] >= floors["refuted"]
        ), own
    # every sibling that drops a guarantee is refuted on a database that keeps the other guarantees, except the
    # listed ones, which the bounded checker cannot decide (unknown, never proved)
    undecided = {
        r["id"]: r["how"]
        for r in rows
        if r["drop"] and r["outcome"] != "refuted"
    }
    assert undecided == UNDECIDED_SIBLINGS, undecided


def test_a_bounded_null_for_bytes_is_no_value():
    sakila = bench.ADAPTERS["sakila"]
    row = (1, "a", None, "d", 1, None, "p", None, datetime.datetime(2000, 1, 1))
    given = bench._bounded_rows(sakila, {"address": [row]})["address"][0]
    assert (
        "location" not in given and given["address2"] is None
    )  # NOT NULL BYTES: completed, nullable text: kept
    data, broken = bench.complete_database(sakila, {"address": [given]})
    assert not broken and data["address"][0][7] == b""


def test_a_counterexample_must_respect_the_declarations():
    chinook = bench.ADAPTERS["chinook"]
    data, broken = bench.complete_database(
        chinook, {"Track": [{"TrackId": 1, "MediaTypeId": 99}]}
    )
    assert not broken and data["MediaType"] == [
        (99, None)
    ]  # the completion adds the parent
    data["MediaType"] = []
    assert bench.violations(chinook, data) == [
        "Track('MediaTypeId',) = (99,) has no MediaType row"
    ]
    assert bench.violations(chinook, data, ("fk:Track.MediaTypeId",)) == []


@pytest.mark.parametrize("name", sorted(SUBSET))
def test_rewrites_keep_the_results_on_the_real_data(name):
    rows = bench.rewrite_cases((name, SUBSET[name], False))
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(SUBSET[name]) and summary["unsupported"] == 0
    assert summary["verified"] > 0


def test_results_files_report_zero_wrong():
    for name in (
        "sample-databases-rewrites",
        "sample-databases-pairs",
        "sample-databases-sakila-rewrites",
        "sample-databases-sakila-pairs",
    ):
        row = json.loads((ROOT / "benchmarks" / "results" / f"{name}.json").read_text())
        database = name.split("-")[2] if name.count("-") == 3 else None
        command = (
            bench.COMMAND
            if database is None
            else bench.COMMAND.replace(
                "sample_db_bench.py", f"sample_db_bench.py --database {database}"
            )
        )
        assert (
            row["command"] == command
            and row["docs"] == "docs/evals/sample-databases.md"
        )
        assert ", 0 wrong" in row["score"] or row["score"].startswith("0 wrong")
