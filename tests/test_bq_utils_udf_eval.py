"""GoogleSQL behaviour eval on the BigQuery Utils UDF unit tests, and the BigQuery to DuckDB translation fixes.

See docs/evals/bigquery-behavior-eval.md#bigquery-utils-udf-tests and tools/bq_utils_udf_eval.py.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from kumosql.bigquery_duckdb import UntranslatableError, capture_groups, to_duckdb  # noqa: E402

pytestmark = pytest.mark.eval

SPEC = importlib.util.spec_from_file_location(
    "bq_utils_udf_eval", Path(__file__).parent.parent / "tools" / "bq_utils_udf_eval.py"
)
ev = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("bq_utils_udf_eval", ev)  # its dataclasses look the module up by name
SPEC.loader.exec_module(ev)

# Floors, raised as coverage improves. "wrong" is always 0.
MIN_AGREE = 300
MIN_HELD_OUT_AGREE = 49
CASES = 807

# UDFs with a case that was wrong under sqlglot's translation alone (the baseline); kept as regressions.
BASELINE_WRONG_UDFS = {
    "community/typeof",               # FORMAT('%T'): now declined
    "community/cw_comparable_format_bigint",  # FORMAT with a computed format: now declined
    "community/cw_url_extract_parameter",     # REGEXP_EXTRACT pattern built by || (group not set)
    "community/cw_twograms",          # OFFSET of a column index; ARRAY_AGG order then unspecified
    "community/cw_td_nvp",            # UNNEST ... WITH OFFSET counted from 1
    "community/cw_find_in_list",      # UNNEST ... WITH OFFSET counted from 1
    "community/cw_instr4",            # same
    "community/cw_map_create",        # a[OFFSET(i)] with a column index not shifted
    "community/cw_next_day",          # EXTRACT(DAYOFWEEK) 0-6 instead of 1-7
    "community/cw_split_part_delimstr_idx",  # SPLIT(s, NULL)
    "community/cw_stringify_interval",       # FORMAT printf-style; DIV(a, b * c) precedence
    "community/getbit",               # a & b << c read left to right
    "community/to_hex",               # FORMAT('%02x')
    "community/ts_linear_interpolate",  # STRUCT cast by name instead of position
    "community/url_parse",            # REGEXP_EXTRACT no match: '' instead of NULL
    "migration/redshift/translate",   # OFFSET of a subquery index, WITH OFFSET
    "migration/sqlserver/convert_numeric_string",  # FORMAT('%6g')
}


def _rows(bigquery_sql):
    con = duckdb.connect()
    try:
        return con.execute(to_duckdb(bigquery_sql)).fetchall()
    finally:
        con.close()


def test_fixture_matches_the_pinned_source():
    assert ev.check_manifest() == []


def test_every_test_case_is_read():
    cases = ev.build_cases()
    assert len(cases) == CASES
    assert len({c.id for c in cases}) == CASES
    held = [c for c in cases if ev.held_out(c.id)]
    assert 0.15 < len(held) / len(cases) < 0.25


def test_full_run_has_no_wrong_value():
    results = ev.run_all()
    summary = ev.summarize(results)
    wrong = [r["id"] for r in results if r["outcome"] == "WRONG"]
    assert not wrong, wrong
    assert summary["all"]["agree"] >= MIN_AGREE
    assert summary["held_out"]["agree"] >= MIN_HELD_OUT_AGREE
    by_udf = {}
    for r in results:
        by_udf.setdefault(r["udf"], []).append(r["outcome"])
    for udf in BASELINE_WRONG_UDFS:
        assert "agree" in by_udf[udf] or all(o == "unsupported" for o in by_udf[udf]), udf


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT DIV(7 + 1, 2 * 2)", 2),
        ("SELECT 23 & 1 << 3", 0),
        ("SELECT 1 | 6 & 3", 3),
        ("SELECT (23 & 1) << 3", 8),
        ("SELECT [10, 20, 30][OFFSET(1)]", 20),
        ("SELECT [10, 20, 30][ORDINAL(1)]", 10),
        ("SELECT (SELECT a[OFFSET(i)] FROM UNNEST([1]) i) FROM (SELECT [10, 20] a)", 20),
        ("SELECT (SELECT o FROM UNNEST(['a', 'b']) AS x WITH OFFSET AS o WHERE x = 'b')", 1),
        ("SELECT FORMAT('%04d|%6g', 5, 123.456789)", "0005|123.457"),
        ("SELECT EXTRACT(DAYOFWEEK FROM DATE '2022-09-18')", 1),
        ("SELECT EXTRACT(WEEK FROM DATE '2022-01-01')", 0),
        ("SELECT EXTRACT(WEEK FROM DATE '2022-01-02')", 1),
        ("SELECT REGEXP_EXTRACT('abc', r'x(.)')", None),
        ("SELECT REGEXP_EXTRACT('abc', r'b(.)')", "c"),
        ("SELECT REGEXP_EXTRACT('a=1', CONCAT('a', '=(.)'))", "1"),
        ("SELECT SPLIT('a b', CAST(NULL AS STRING))", None),
        ("SELECT CAST(STRUCT(1 AS x, 2.0) AS STRUCT<x INT64, y FLOAT64>).y", 2.0),
        ("SELECT CAST(z AS STRUCT<a INT64>).a FROM (SELECT STRUCT(5 AS q) AS z)", 5),
    ],
)
def test_translation_keeps_googlesql_values(sql, expected):
    assert _rows(sql) == [(expected,)]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FORMAT('%T', 1)",
        "SELECT FORMAT(f, 1) FROM (SELECT '%d' AS f)",
        "SELECT REGEXP_EXTRACT('abc', p) FROM (SELECT 'b' AS p)",
        "SELECT REGEXP_EXTRACT('abc', '(a)(b)')",
        "SELECT a[i] FROM (SELECT [1] AS a, 0 AS i)",
        "SELECT EXTRACT(WEEK(TUESDAY) FROM DATE '2022-01-01')",
    ],
)
def test_translation_declines_what_it_cannot_keep(sql):
    with pytest.raises(UntranslatableError):
        to_duckdb(sql)


def test_capture_groups():
    assert capture_groups(r"a(b)") == 1
    assert capture_groups(r"(?:a)(b)") == 1
    assert capture_groups(r"\(x\)[(]") == 0
    assert capture_groups(r"(?P<n>a)(b)") == 2
    assert capture_groups(r"\p{L}+(x)") == 1


def test_baseline_translation_is_kept_for_comparison():
    assert to_duckdb("SELECT DIV(7 + 1, 2 * 2)", fix=False) != to_duckdb("SELECT DIV(7 + 1, 2 * 2)")


def test_json_test_file_reader():
    groups = ev.parse_test_file(
        'generate_udf_test("f", [{inputs: [`"a\\\\d"`, `1`], expected_output: `"x"` + `"y"`}]);', "community"
    )
    assert groups[0].cases == [{"inputs": ['"a\\d"', "1"], "expected_output": '"x""y"'}]
