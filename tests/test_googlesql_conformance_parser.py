"""The GoogleSQL conformance runner reads the compliance files' printed results and compares them the way the
compliance driver does. These tests cover that reading and comparing only, not the evaluator."""

import importlib.util
import math
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.eval

_PATH = Path(__file__).resolve().parent.parent / "tools" / "googlesql_conformance.py"
_SPEC = importlib.util.spec_from_file_location("googlesql_conformance", _PATH)
G = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = G
_SPEC.loader.exec_module(G)
T = G.T
V = G.V


def rows(text):
    expected = G.parse_expected(text)
    assert expected.kind == "rows"
    return expected


def result(columns, data, ordered=True, value_table=False, deterministic=True):
    return SimpleNamespace(columns=columns, rows=data, ordered=ordered, value_table=value_table, deterministic=deterministic)


# --- reading printed results -------------------------------------------------------------------


def test_scalars_follow_the_header_types():
    e = rows('ARRAY<STRUCT<a INT64, b STRING, c BOOL, d DOUBLE, e NUMERIC>>[known order:{1, "x\\ny", true, -0.5, 1.50}]')
    assert e.type == T.struct([("a", T.INT64), ("b", T.STRING), ("c", T.BOOL), ("d", T.FLOAT64), ("e", T.NUMERIC)])
    assert e.rows == [(1, "x\ny", True, -0.5, Decimal("1.50"))]
    assert not e.unordered


def test_order_markers_and_nulls():
    e = rows("ARRAY<STRUCT<INT64, STRING>>[unknown order:\n  {1, NULL},\n  {NULL, \"a\"}\n]")
    assert e.unordered and e.rows == [(1, None), (None, "a")]
    assert e.type == T.struct([(None, T.INT64), (None, T.STRING)])
    assert rows("ARRAY<STRUCT<INT64>>[]").rows == []


def test_header_on_its_own_line_and_nested_arrays_keep_their_prefix():
    e = rows(
        "ARRAY<STRUCT<k INT64, v ARRAY<>, w ARRAY<>>>\n[{1, ARRAY<INT64>[known order:3, 5], ARRAY<STRING>(NULL)},\n"
        ' {2, ARRAY<INT64>[unknown order:4, 6], ARRAY<STRING>["a"]}]'
    )
    assert e.type == T.struct([("k", T.INT64), ("v", T.array(T.INT64)), ("w", T.array(T.STRING))])
    first, second = e.rows
    assert first == (1, (3, 5), None)
    assert not isinstance(first[1], V.UnorderedArray)
    assert isinstance(second[1], V.UnorderedArray) and second[2] == ("a",)


def test_struct_values_inside_structs_and_arrays_of_structs():
    e = rows("ARRAY<STRUCT<INT64, s STRUCT<a BOOL, b_struct STRUCT<c INT32, d INT64>>>>[known order:\n  {0, {NULL, {NULL, 1}}},\n  {1, {true, {-1, -1}}}\n]")
    assert e.rows == [(0, (None, (None, 1))), (1, (True, (-1, -1)))]
    e = rows('ARRAY<STRUCT<ARRAY<>>>[{ARRAY<STRUCT<x INT64, y STRING>>[{1, "a"}, {2, "b"}]}]')
    assert e.rows == [(((1, "a"), (2, "b")),)]
    assert e.type.fields[0][1] == T.array(T.struct([("x", T.INT64), ("y", T.STRING)]))


def test_floats_special_values():
    e = rows("ARRAY<STRUCT<DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE>>[{nan, -inf, inf, -0, 1.0786158809173895e+308}]")
    nan, neg, pos, zero, big = e.rows[0]
    assert math.isnan(nan) and neg == -math.inf and pos == math.inf and zero == 0.0 and big == 1.0786158809173895e308


def test_strings_and_bytes_escapes():
    e = rows('ARRAY<STRUCT<STRING, BYTES, STRING>>[{"a\\"b\\\\", b"\\x00\\xe2\\x82\\xac", \'it\\\'s\'}]')
    assert e.rows == [('a"b\\', b"\x00\xe2\x82\xac", "it's")]
    assert G.unescape_literal("\\123\\x41\\u00e9", False) == "SAé"
    assert G.unescape_literal("é", True) == b"\xc3\xa9"
    with pytest.raises(G.ExpectedError):
        G.unescape_literal("\\q", False)
    assert rows('ARRAY<STRUCT<STRING>>[{"a, b}]"}]').rows == [("a, b}]",)]


def test_temporal_values():
    e = rows(
        "ARRAY<STRUCT<DATE, DATETIME, TIME, TIMESTAMP, TIMESTAMP, INTERVAL, INTERVAL>>[{2020-01-02, 2020-12-31 14:00:00.5, 01:02:03.000001,"
        " 1970-01-01 00:00:01.001001+00, 2020-01-01 10:00:00-08, -1-8 30 0:0:0, 0-0 0 -166:40:0.5}]"
    )
    d, dt, t, ts, ts2, i1, i2 = e.rows[0]
    assert d == date(2020, 1, 2)
    assert dt == datetime(2020, 12, 31, 14, 0, 0, 500000)
    assert t == time(1, 2, 3, 1)
    assert ts == 1_001_001
    assert ts2 == V.utc_to_micros(datetime(2020, 1, 1, 18, 0, 0))
    assert i1 == V.Interval(-20, 30, 0) and i2 == V.Interval(0, 0, -(166 * 3600 + 40 * 60) * 1_000_000 - 500000)


def test_nanoseconds_are_a_separate_failure():
    with pytest.raises(G.NanoPrecision):
        rows("ARRAY<STRUCT<TIMESTAMP>>[{2020-01-01 00:00:00.123456789+00}]")


def test_types_the_evaluator_does_not_model_keep_their_text():
    e = rows(
        "ARRAY<STRUCT<ARRAY<>, p PROTO<googlesql_test.M>, RANGE<DATE>>>[{ARRAY<PROTO<googlesql_test.M>>[{i2: 1}, {}],\n  {\n    a: 1\n    b: 2\n  },\n  [2020-01-01, NULL)}]"
    )
    assert e.type.fields[1][1].kind == "OTHER" and e.type.fields[2][1].kind == "OTHER"
    arrays, proto, range_ = e.rows[0]
    assert len(arrays) == 2 and isinstance(proto, G.Raw) and "a: 1" in proto and range_ == "[2020-01-01, NULL)"


def test_value_table_results():
    e = rows('ARRAY<INT64>[unknown order:1, 2, 3]')
    assert e.type == T.INT64 and e.unordered and e.rows == [1, 2, 3]
    assert rows('ARRAY<STRING>["a"]').rows == ["a"]


def test_errors_and_notes():
    e = G.parse_expected("ERROR: generic::out_of_range: division by zero: 1 / 0")
    assert (e.kind, e.code) == ("error", "out_of_range") and e.message.startswith("division by zero")
    e = G.parse_expected("ERROR: generic::invalid_argument: Argument 2 [at 1:24]\nSELECT x\n         ^")
    assert e.code == "invalid_argument"
    e = rows("ARRAY<STRUCT<BOOL>>[{true}]\n\nNOTE: Reference implementation reports non-determinism.")
    assert e.rows == [(True,)] and e.nondeterministic


@pytest.mark.parametrize("text", ["", "SELECT 1", "ARRAY<STRUCT<INT64>>[{1, 2}]", 'ARRAY<STRUCT<INT64>>[{"a"}]', "ARRAY<STRUCT<INT64>>[{1}", "STRUCT<a INT64>{1}"])
def test_unreadable_text_is_an_expected_error_not_a_crash(text):
    with pytest.raises(G.ExpectedError):
        G.parse_expected(text)


def test_header_types():
    assert G.parse_header_type("STRUCT<int64 INT64, ARRAY<STRING>, x STRUCT<DOUBLE>>") == T.struct(
        [("int64", T.INT64), (None, T.array(T.STRING)), ("x", T.struct([(None, T.FLOAT64)]))]
    )
    assert G.has_placeholder(G.parse_header_type("ARRAY<STRUCT<ARRAY<>>>"))
    assert G.parse_header_type("ARRAY<INT32>").foreign


def test_every_claimed_dev_result_is_readable():
    unreadable = []
    for case in G.cases("dev"):
        if case.claimed:
            try:
                G.parse_expected(case.expected)
            except G.NanoPrecision:
                pass
            except G.ExpectedError as error:
                unreadable.append((case.file, case.name, str(error)))
    assert not unreadable, unreadable[:5]


def test_tables_load_from_their_printed_rows():
    table = G.load_table('ARRAY<STRUCT<id INT64, tags ARRAY<>, n NUMERIC>>[unknown order:{1, ARRAY<STRING>[unknown order:"a", "b"], 2.5}, {2, ARRAY<STRING>(NULL), NULL}]')
    assert table.columns == [("id", T.INT64), ("tags", T.array(T.STRING)), ("n", T.NUMERIC)]
    assert table.rows == [(1, ("a", "b"), Decimal("2.5")), (2, None, None)]
    assert not isinstance(table.rows[0][1], V.UnorderedArray)
    with pytest.raises(G.ExpectedError):
        G.load_table("ARRAY<INT64>[1]")  # a value table
    with pytest.raises(G.ExpectedError):
        G.load_table("ARRAY<STRUCT<a ARRAY<>>>[]")  # a column whose type nothing says


def test_file_context_blocks_what_it_cannot_provide():
    prepare = [
        {"sql": "CREATE TABLE T AS SELECT 1 a;", "expected": ["ARRAY<STRUCT<a INT64>>[{1}]"], "options": {}},
        {"sql": "CREATE TABLE VT AS SELECT AS VALUE 1;", "expected": ["ARRAY<INT64>[1]"], "options": {}},
        {"sql": "CREATE TEMP FUNCTION F(x INT64) AS (x);", "expected": [], "options": {}},
    ]
    context = G.FileContext(prepare)
    assert context.database.table("t") is not None
    assert context.blocked("SELECT * FROM T") is None
    assert "VT" in context.blocked("select * from vt")
    assert context.blocked("SELECT 1 AS vtx") is None


def test_query_parameter_option_splitting():
    assert G.split_top_level('cast("+inf" as double) as pos_inf,\n cast("a,b" as string) as s') == [
        'cast("+inf" as double) as pos_inf',
        'cast("a,b" as string) as s',
    ]
    assert G.split_top_level("f(1, 2) as x") == ["f(1, 2) as x"]


# --- comparing ---------------------------------------------------------------------------------


def test_floats_compare_within_four_ulps():
    one = 1.0
    near = one
    for _ in range(4):
        near = math.nextafter(near, 2.0)
    assert G.float_close(one, near)
    assert not G.float_close(one, math.nextafter(near, 2.0))
    assert G.float_close(math.nan, math.nan) and not G.float_close(math.nan, 1.0)
    assert G.float_close(math.inf, math.inf) and not G.float_close(math.inf, -math.inf)
    assert G.float_close(0.0, -0.0)
    assert G.float_close(-1.0, math.nextafter(-1.0, -2.0))
    assert not G.float_close(5e-324, 1e-300)


def test_arrays_of_unknown_order_compare_as_multisets():
    t = T.array(T.INT64)
    assert G.values_equal(t, V.UnorderedArray((1, 2, 3)), (3, 1, 2))
    assert not G.values_equal(t, V.UnorderedArray((1, 2, 3)), (3, 1, 1))
    assert not G.values_equal(t, (1, 2, 3), (3, 1, 2))
    # the evaluator's own undetermined order only counts as equal when asked to be lenient
    assert not G.values_equal(t, (1, 2, 3), V.UnorderedArray((3, 1, 2)))
    assert G.values_equal(t, (1, 2, 3), V.UnorderedArray((3, 1, 2)), strict=False)
    nested = T.array(T.struct([("a", T.FLOAT64)]))
    assert G.values_equal(nested, V.UnorderedArray(((1.0,), (2.0,))), ((2.0,), (math.nextafter(1.0, 2.0),)))


def test_equality_keeps_types_apart():
    assert not G.values_equal(T.INT64, 1, True)
    assert not G.values_equal(T.BOOL, True, 1)
    assert not G.values_equal(T.FLOAT64, 1.0, 1)
    assert G.values_equal(T.NUMERIC, Decimal("1.50"), Decimal("1.5"))
    assert G.values_equal(T.INTERVAL, V.Interval(1, 2, 3), V.Interval(1, 2, 3))
    assert not G.values_equal(T.INTERVAL, V.Interval(1, 0, 0), V.Interval(0, 30, 0))
    assert G.values_equal(T.STRING, None, None) and not G.values_equal(T.STRING, "", None)


def test_types_match_names_and_open_arrays():
    a = T.struct([("x", T.INT64)])
    assert G.types_match(a, T.struct([("x", T.INT64)]))
    assert not G.types_match(a, T.struct([("y", T.INT64)]))
    assert not G.types_match(a, T.struct([(None, T.INT64)]))
    open_array = G.parse_header_type("ARRAY<>")
    assert G.types_match(T.struct([("a", open_array)]), T.struct([("a", T.array(T.STRING))]))
    assert not G.types_match(T.INT64, T.FLOAT64)


COLS = [("a", T.INT64), ("b", T.STRING)]


def test_compare_rows_ordered_and_unordered():
    e = rows('ARRAY<STRUCT<a INT64, b STRING>>[unknown order:{1, "x"}, {2, "y"}]')
    assert G.compare(e, result(COLS, [(2, "y"), (1, "x")], ordered=False))[0] == "exact"
    assert G.compare(e, result(COLS, [(2, "y"), (1, "z")]))[0] == "mismatch"
    ordered = rows('ARRAY<STRUCT<a INT64, b STRING>>[known order:{1, "x"}, {2, "y"}]')
    assert G.compare(ordered, result(COLS, [(1, "x"), (2, "y")]))[0] == "exact"
    verdict, reason = G.compare(ordered, result(COLS, [(2, "y"), (1, "x")]))
    assert verdict == "mismatch" and "row 0" in reason
    # the same rows in an order the evaluator did not determine are a decline, not a pass and not a mismatch
    assert G.compare(ordered, result(COLS, [(1, "x"), (2, "y")], ordered=False))[0] == "unsupported"
    assert G.compare(ordered, result(COLS, [(2, "y"), (1, "x")], ordered=False))[0] == "unsupported"
    assert G.compare(ordered, result(COLS, [(1, "x"), (3, "q")], ordered=False))[0] == "mismatch"


def test_compare_checks_column_types_and_names():
    e = rows('ARRAY<STRUCT<a INT64, b STRING>>[{1, "x"}]')
    assert G.compare(e, result([("a", T.INT64), ("c", T.STRING)], [(1, "x")]))[0] == "mismatch"
    assert G.compare(e, result([("a", T.FLOAT64), ("b", T.STRING)], [(1.0, "x")]))[0] == "mismatch"
    assert G.compare(e, result([("a", T.INT64), ("b", T.STRING)], [(1, "x")]))[0] == "exact"


def test_compare_value_tables_and_unnamed_columns():
    e = rows("ARRAY<INT64>[unknown order:1, 2]")
    assert G.compare(e, result([(None, T.INT64)], [(2,), (1,)], value_table=True))[0] == "exact"
    assert G.compare(e, result([(None, T.INT64)], [(2,), (1,)]))[0] == "mismatch"  # a table of one column is not a value table
    e = rows("ARRAY<STRUCT<INT64, DOUBLE>>[{1, 0.1}]")
    assert G.compare(e, result([(None, T.INT64), ("", T.FLOAT64)], [(1, math.nextafter(0.1, 1.0))]))[0] == "exact"


def test_compare_empty_results_and_wrong_kind_of_outcome():
    e = rows("ARRAY<STRUCT<a INT64>>[]")
    assert G.compare(e, result([("a", T.INT64)], []))[0] == "exact"
    assert G.compare(e, result([("a", T.INT64)], [(1,)]))[0] == "mismatch"
    err = G.parse_expected("ERROR: generic::out_of_range: boom")
    assert G.compare(err, result([("a", T.INT64)], [(1,)]))[0] == "mismatch"


def test_nondeterministic_results_that_differ_are_declined():
    e = rows("ARRAY<STRUCT<a INT64>>[{1}]")
    assert G.compare(e, result([("a", T.INT64)], [(2,)], deterministic=False))[0] == "unsupported"
    assert G.compare(e, result([("a", T.INT64)], [(2,)]))[0] == "mismatch"


def test_reason_grouping_folds_numbers_but_keeps_names():
    assert G.reason_group("window function Sum frame 3") == G.reason_group("window function Sum frame 12")
    assert G.reason_group("column of GoogleSQL-only type UINT64") != G.reason_group("column of GoogleSQL-only type INT32")
    assert G.reason_family("function ArraySize") == ("function <name>", "ArraySize")
    assert G.reason_family("PIVOT / UNPIVOT") == ("PIVOT / UNPIVOT", "")


def test_claim_is_decided_from_the_case_not_the_result():
    assert G.claim("strings", "SELECT 1", {"name": "n"}) == (True, "")
    assert not G.claim("dml_insert", "SELECT 1", {"name": "n"})[0]
    assert not G.claim("strings", "INSERT t VALUES (1)", {"name": "n"})[0]
    assert not G.claim("strings", "SELECT CAST(1 AS UINT64)", {"name": "n"})[0]
