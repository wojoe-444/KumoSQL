"""Pagila in the sample databases eval: the load checked against upstream, workload, pairs with 0 wrong.

The shared checks of ``tests/test_sample_db_bench.py`` (load against upstream, every pair of every database) also
cover Pagila. Here: the Pagila adapter itself (the COPY reader, the folded payment partitions, the computed
generated column), a pinned subset of its workload through the rewrite pipeline, its pairs with their own floors,
and its results files. The full run is ``python tools/sample_db_bench.py --database pagila --write-results``.
"""

import datetime as dt
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

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

pagila = bench.ADAPTERS["pagila"]

# floors only ever go up (recorded run: 22 of 27 proved, 31 of 33 refuted)
FLOORS = {"proven": 22, "refuted": 30}
# workload queries the pipeline and lift_subqueries change: upstream views and functions, README, authored
SUBSET = [
    "pg-view-customer-list",
    "pg-view-sales-by-film-category",
    "pg-func-film-in-stock",
    "pg-readme-length-hours",
    "pg-a22-cte-with-unused",
    "pg-a23-derived-table-trivial-predicate",
    "pg-a11-not-in-with-nulls",
    "pg-a30-customers-per-signup-year",
]


def test_the_copy_reader_undoes_the_text_format():
    dump = (
        "COPY public.t (a, b, c) FROM stdin;\n"
        "1\tit's\t\\N\n"
        '2\ttab\\there\t{x,"y z"}\n'
        "\\.\n"
        "COPY public.payment_p2022_01 (a, b, c) FROM stdin;\n"
        "3\t\t\\\\x1F\n"
        "\\.\n"
    )
    rows = bench.read_copy(dump)
    assert rows["t"][0][1][0].text == "1" and rows["t"][0][1][1].text == "it's"
    assert rows["t"][0][1][2] == bench.Raw("null", None)
    assert rows["t"][1][1][1].text == "tab\there"
    assert rows["t"][1][1][2].text == '{x,"y z"}'
    assert rows["payment_p2022_01"][0][0] == ("a", "b", "c")
    assert (
        rows["payment_p2022_01"][0][1][2].text == "\\x1F"
    )  # bytea hex, undone later by convert
    assert pagila.convert(
        "staff", "picture", "BYTES", bench.Raw("string", "\\x1F")
    ) == bytes([0x1F])


def test_the_payment_partitions_fold_into_one_table_without_foreign_keys():
    raw = bench.read_ddl(pagila.ddl_text())
    partitions = [n for n in pagila.renames]
    assert len(partitions) == 55 and set(partitions) <= set(raw)
    with_fks = [n for n in partitions if raw[n].foreign_keys]
    # upstream declares payment's foreign keys on the first six partitions only (a quirk of the pinned release)
    assert with_fks == partitions[:6]
    tables = pagila.upstream_tables()
    assert not any(n in tables for n in partitions)
    assert tables["payment"].primary_key == ("payment_date", "payment_id")
    assert tables["payment"].foreign_keys == []
    assert pagila.schema()["payment"].foreign_keys == []


def test_upstream_views_and_the_generated_column():
    views = pagila.upstream_views()
    assert sorted(views) == [
        "actor_info",
        "customer_list",
        "film_list",
        "nicer_but_slower_film_list",
        "rental_by_category",
        "sales_by_film_category",
        "sales_by_store",
        "staff_list",
    ]
    queries = [q for q in pagila.workload() if q["origin"] == "upstream-view"]
    assert sorted(q["name"] for q in queries) == sorted(views)
    con = pagila.connect()
    try:
        # film.length_hours is round(length / 60.0, 2), computed from the upstream expression
        assert con.execute(
            "SELECT length, length_hours FROM film WHERE film_id = 1"
        ).fetchall() == [(86, Decimal("1.43"))]
        assert con.execute(
            "SELECT COUNT(*) FROM film WHERE length_hours <> ROUND(length / 60.0, 2)"
        ).fetchone() == (0,)
    finally:
        con.close()


def test_dates_booleans_and_fresh_foreign_key_values_in_counterexamples():
    assert bench._typed("2024-02-29", "DATE") == dt.date(2024, 2, 29)
    assert bench._typed(3, "DATE") == dt.date(2000, 1, 4)
    assert bench._typed("true", "BOOL") is True and bench._typed(0, "BOOL") is False
    # a counterexample that leaves film.language_id out: the completion must not reuse the value 0 it gives
    # other columns, or the missing foreign key parent coincides with the original language it names
    data, broken = bench.complete_database(
        pagila,
        {"film": [{"film_id": 1, "title": "a", "original_language_id": 0}]},
        ("fk:film.original_language_id",),
    )
    assert not broken
    film = dict(zip(pagila.schema()["film"].columns, data["film"][0]))
    assert film["language_id"] != 0 and film["original_language_id"] == 0
    assert [row[0] for row in data["language"]] == [film["language_id"]]
    # the default adapters keep the type default, as before
    assert not bench.ADAPTERS["chinook"].fresh_foreign_key_values


def test_every_pagila_pair_is_decided_without_a_wrong_answer():
    rows = bench.run_pairs([pagila])
    summary = bench.summarize_pairs(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["wrong"]]
    assert summary["labels_unverified"] == 0, [
        r["id"] for r in rows if r["witness_ok"] is False
    ]
    assert (
        summary["proven"] >= FLOORS["proven"]
        and summary["refuted"] >= FLOORS["refuted"]
    )
    assert summary["constraint_siblings_refuted"] == summary["constraint_siblings"]
    # a pair annotated with a prover bug is the replay gate catching an illegal counterexample: it must stay
    # unknown, never a refutation or a proof
    annotated = {p["id"] for p in pagila.pairs() if p.get("known_prover_bug")}
    assert {r["id"] for r in rows if r.get("prover_bug")} <= annotated
    assert all(
        r["outcome"] == "unknown"
        for r in rows
        if r["id"] in annotated and r.get("prover_bug")
    )


def test_rewrites_keep_the_results_on_the_real_data():
    rows = bench.rewrite_cases(("pagila", SUBSET, False))
    summary = bench.summarize_rewrites(rows)["all"]
    assert summary["wrong"] == 0, [r for r in rows if r["status"] == "wrong"]
    assert summary["queries"] == len(SUBSET) and summary["unsupported"] == 0
    assert summary["verified"] > 0


def test_results_files_report_zero_wrong():
    for name in ("sample-databases-pagila-rewrites", "sample-databases-pagila-pairs"):
        row = json.loads((ROOT / "benchmarks" / "results" / f"{name}.json").read_text())
        assert row["docs"] == "docs/evals/sample-databases-pagila.md"
        assert row["command"].endswith("--database pagila --write-results")
        assert ", 0 wrong" in row["score"] or row["score"].startswith("0 wrong")
    # the first two databases' rows keep their own files and numbers
    legacy = json.loads(
        (ROOT / "benchmarks" / "results" / "sample-databases-rewrites.json").read_text()
    )
    assert legacy["size"] == 515
