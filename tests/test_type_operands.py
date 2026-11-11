"""The operand checks of ``check_query`` (kumosql.type_operands): silent on SQL that runs, and exact on what it reports."""

import pathlib

import pytest

from kumosql.googlesql_types import Catalog, infer
from kumosql.type_operands import check_operands

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = {"sch.t": {"i": "INT64", "f": "FLOAT64", "n": "NUMERIC", "s": "STRING", "b": "BOOL", "d": "DATE", "dt": "DATETIME", "ts": "TIMESTAMP",
                   "arr": "ARRAY<INT64>", "rec": "STRUCT<a INT64>", "u": "STRING"}}


CORPORA = ("dataform-stackoverflow", "terashim-ga4", "wintermi-fashion", "wintermi-bqe", "wintermi-imdb", "wintermi-movielens", "dataform-bqml")


def run(sql):
    typed = infer(sql, Catalog.from_types(CATALOG))
    return check_operands(typed.tree, typed)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM sch.t WHERE i = f AND i = n AND f > n",  # different numeric types are never reported
        "SELECT 1 FROM sch.t WHERE d = dt AND dt >= '2026-01-01' AND ts > '2026-01-01 00:00:00' AND d < '2026-01-01'",
        "SELECT 1 FROM sch.t WHERE s = 'x' AND s IN ('a', 'b') AND s BETWEEN 'a' AND 'b' AND i IN (1, 2.5) AND f BETWEEN 1 AND 2",
        "SELECT 1 FROM sch.t WHERE s IS NULL AND i = NULL AND s = CAST(i AS STRING) AND i IN UNNEST(arr) AND b = TRUE",
        "SELECT 1 FROM sch.t WHERE rec = rec AND arr = arr AND s = @param AND i = (SELECT MAX(i) FROM sch.t)",
        "SELECT MOD(i, 2), MOD(i, i), DIV(n, 2), POW(f, 2), SAFE_DIVIDE(i, f), i / f, i + n, ABS(f), SUM(i), AVG(f), SUM(n) FROM sch.t",
        "SELECT d + 1, DATE_ADD(d, INTERVAL 1 DAY), ts - TIMESTAMP '2026-01-01', dt + INTERVAL 1 HOUR FROM sch.t",
        "SELECT 1 FROM sch.t a JOIN sch.t b USING (i) JOIN sch.t c ON a.s = c.s AND a.d = c.dt",
        "SELECT i, s FROM sch.t UNION ALL SELECT i, s FROM sch.t",
        "SELECT i FROM sch.t UNION ALL SELECT f FROM sch.t",
        "WITH x AS (SELECT s AS k FROM sch.t) SELECT 1 FROM x JOIN sch.t ON x.k = t.s",
    ],
)
def test_sql_that_runs_in_bigquery_has_no_operand_finding(sql):
    report = run(sql)
    assert report.findings == [] and report.decided >= 0


@pytest.mark.parametrize(
    "sql, code",
    [
        ("SELECT 1 FROM sch.t WHERE s = i", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE i = 'a'", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE s > 5", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE s BETWEEN 1 AND 9", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE i IN ('a')", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE dt = ts", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE d = ts", "incompatible_operands"),
        ("SELECT 1 FROM sch.t WHERE b = 1", "incompatible_operands"),
        ("SELECT 1 FROM sch.t a JOIN sch.t c ON a.i = c.s", "incompatible_operands"),
        ("SELECT 1 FROM sch.t a JOIN (SELECT s AS i FROM sch.t) c USING (i)", "incompatible_operands"),
        ("SELECT MOD(f, i) FROM sch.t", "no_matching_signature"),
        ("SELECT MOD(f, 2) FROM sch.t", "no_matching_signature"),
        ("SELECT s + 1 FROM sch.t", "no_matching_signature"),
        ("SELECT b * i FROM sch.t", "no_matching_signature"),
        ("SELECT SUM(s) FROM sch.t", "no_matching_signature"),
        ("SELECT ABS(d) FROM sch.t", "no_matching_signature"),
    ],
)
def test_a_mismatch_with_known_types_is_reported_once_with_its_code(sql, code):
    report = run(sql)
    assert [f.code for f in report.findings] == [code], report
    assert report.findings[0].fix


def test_a_between_with_two_wrong_bounds_is_one_finding_that_names_both_bounds():
    [finding] = run("SELECT 1 FROM sch.t WHERE s BETWEEN 202601 AND 202610").findings
    assert "202601" in finding.what and "202610" in finding.what and "BETWEEN" in finding.what and "'202601'" in finding.fix


def test_unknown_types_are_returned_not_guessed_and_a_null_is_never_a_mismatch():
    typed = infer("SELECT 1 FROM sch.t WHERE x.unknown_column = 5 AND s = NULL AND i = NULL", Catalog.from_types(CATALOG))
    report = check_operands(typed.tree, typed)
    assert report.findings == [] and report.decided == 0
    typed = infer("SELECT 1 FROM other.t o WHERE o.k = 5", Catalog.from_types(CATALOG))
    report = check_operands(typed.tree, typed)
    assert report.findings == [] and [u.column.name for u in report.unknown] == ["k"] and report.undecided == 1


def test_no_operand_finding_on_the_fixture_projects_models_with_types_carried_through():
    """Every model of the checked-in Dataform corpora runs in BigQuery: an operand finding on one is a bug in the check."""

    from kumosql.googlesql_types import Column
    from kumosql.mcp_session import Session, load_project

    decided = 0
    for name in CORPORA:  # the small projects; the large ones take seconds just to analyse
        corpus = ROOT / "tests" / "fixtures" / "bq_corpora" / name
        session = Session(load_project(corpus))
        types = session.column_types()
        catalog = Catalog()
        for key, columns in types.types.items():
            catalog.add(key, [Column(name, kind) for name, kind in columns.items()])
        for key, model in session.pipeline().models.items():
            typed = infer(model.sql, catalog)
            if typed.columns is None:
                continue
            report = check_operands(typed.tree, typed)
            decided += report.decided
            assert report.findings == [], (corpus.name, key, [f.what for f in report.findings])
    assert decided > 0
