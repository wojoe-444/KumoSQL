"""GROUPING SETS / ROLLUP / CUBE key matching, GROUP BY ALL with window functions, and the conformance runner's
reading of STRUCT field names that hold spaces."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kumosql.gsql_eval import AnalysisError, Database, Table, Unsupported, evaluate
from kumosql.gsql_eval import types as T

pytestmark = pytest.mark.eval

DATA = "WITH T AS (SELECT 1 AS x, 'p' AS s UNION ALL SELECT 2, 'q' UNION ALL SELECT 2, 'q' UNION ALL SELECT NULL, 'p') "


def rows(sql, mode="bigquery"):
    return evaluate(DATA + sql, Database(), mode=mode).rows


def sort_key(row):
    return tuple((v is not None, v) if v is not None else (False, 0) for v in row)


def same(actual, expected):
    return sorted(actual, key=sort_key) == sorted(expected, key=sort_key)


# --- an expression over a grouping key is computed from the key ----------------------------------------------


def test_expression_over_a_key_is_computed_from_the_key_in_grouping_sets():
    got = rows("SELECT x, x + 10, COUNT(*) FROM T GROUP BY GROUPING SETS (x, x + 10)")
    # set {x}: x + 10 is computed from x; set {x + 10}: x is NULL, so x + 10 (computed from x) is NULL too
    assert same(got, [(1, 11, 1), (2, 12, 2), (None, None, 1), (None, None, 1), (None, None, 1), (None, None, 2)])


def test_expression_with_a_column_that_is_not_a_key_matches_the_key_it_is_written_as():
    got = rows("SELECT x + 10, COUNT(*) FROM T GROUP BY GROUPING SETS (x + 10, ())")
    assert same(got, [(11, 1), (12, 2), (None, 1), (None, 4)])


def test_rollup_with_the_expression_after_its_column():
    got = rows("SELECT x, x * 2, SUM(x) FROM T GROUP BY ROLLUP (x, x * 2)")
    assert same(
        got,
        [(1, 2, 1), (2, 4, 4), (None, None, None), (1, 2, 1), (2, 4, 4), (None, None, None), (None, None, 5)],
    )


def test_cube_with_the_expression_after_its_column():
    got = rows("SELECT x, x * 2, COUNT(*) FROM T GROUP BY CUBE (x, x * 2)")
    assert same(
        got,
        [(1, 2, 1), (2, 4, 2), (None, None, 1)]  # {x, x * 2}
        + [(1, 2, 1), (2, 4, 2), (None, None, 1)]  # {x}: x * 2 is computed from x
        + [(None, None, 1), (None, None, 1), (None, None, 2)]  # {x * 2}: x is NULL, so x * 2 is NULL
        + [(None, None, 4)],  # {}
    )


def test_duplicate_keys_and_repeated_columns_group_once():
    assert same(
        rows("SELECT x, COUNT(*) FROM T GROUP BY GROUPING SETS ((x, x), (x, x, x))"),
        [(1, 1), (2, 2), (None, 1)] * 2,
    )
    assert same(rows("SELECT x, COUNT(*) FROM T GROUP BY ROLLUP (x, x)"), [(1, 1), (2, 2), (None, 1)] * 2 + [(None, 4)])


def test_multi_column_items_in_rollup_repeat_like_single_ones():
    got = rows("SELECT x, s, UPPER(s), COUNT(*) FROM T GROUP BY ROLLUP ((x, s), (x, s), UPPER(s))")
    full = [(1, "p", "P", 1), (2, "q", "Q", 2), (None, "p", "P", 1)]
    # sets {x, s, U}, {x, s}, {x, s}, {}: UPPER(s) is computed from the key s where U is not grouped
    assert same(got, full * 3 + [(None, None, None, 4)])


def test_select_item_named_by_alias_or_ordinal_is_the_key_itself():
    got = rows("SELECT x, UPPER(s) AS u, COUNT(*) FROM T GROUP BY GROUPING SETS (x, u)")
    # s is not a key: UPPER(s) is the key, NULL where it is not grouped
    assert same(got, [(1, None, 1), (2, None, 2), (None, None, 1), (None, "P", 2), (None, "Q", 2)])
    # the same select item reached by an ordinal
    assert same(rows("SELECT x, UPPER(s), COUNT(*) FROM T GROUP BY GROUPING SETS (x, 2)"), got)


def test_select_item_named_by_alias_is_the_key_even_when_it_reads_another_key():
    # s is a key too, but the item UPPER(s) is named by the alias u: it is the key u, NULL in the set {s}
    got = rows("SELECT s, UPPER(s) AS u, COUNT(*) FROM T GROUP BY GROUPING SETS (s, u)")
    assert same(got, [("p", None, 2), ("q", None, 2), (None, "P", 2), (None, "Q", 2)])
    # written out instead of named, it is computed from the key s
    got = rows("SELECT s, UPPER(s), COUNT(*) FROM T GROUP BY GROUPING SETS (s, UPPER(s))")
    assert same(got, [("p", "P", 2), ("q", "Q", 2), (None, None, 2), (None, None, 2)])


def test_int_literal_in_grouping_sets_is_a_select_ordinal_and_the_item_it_names_is_a_key():
    got = rows("SELECT x, 7, GROUPING(x) FROM T GROUP BY GROUPING SETS (x, 2, 1)")
    assert same(got, [(1, None, 0), (2, None, 0), (None, None, 0), (1, None, 0), (2, None, 0), (None, None, 0), (None, 7, 1)])


def test_ordinal_out_of_range_and_non_integer_literal():
    with pytest.raises(AnalysisError):
        rows("SELECT x FROM T GROUP BY GROUPING SETS (x, 3)")
    with pytest.raises(AnalysisError):
        rows("SELECT x FROM T GROUP BY GROUPING SETS (x, 0)")
    with pytest.raises(Unsupported):
        rows("SELECT x FROM T GROUP BY GROUPING SETS (x, 1.5)")


def test_constant_equal_to_a_key_some_sets_leave_out_is_declined():
    with pytest.raises(Unsupported):
        rows("SELECT x, 'k', COUNT(*) FROM T GROUP BY GROUPING SETS (x, 'k')")
    with pytest.raises(Unsupported):
        rows("SELECT x, 1 + 2, COUNT(*) FROM T GROUP BY ROLLUP (x, 1 + 2)")
    # a key every set keeps is the same constant either way
    assert same(rows("SELECT x, 'k', COUNT(*) FROM T GROUP BY x, 'k'"), [(1, "k", 1), (2, "k", 2), (None, "k", 1)])


def test_ungrouped_column_is_still_an_error():
    with pytest.raises(AnalysisError):
        rows("SELECT x, s FROM T GROUP BY GROUPING SETS (x, UPPER(s))")


def test_keyword_looking_names_in_grouping_sets():
    sql = (
        "WITH T AS (SELECT 10 AS `GROUPING SETS`, 'bar' AS `ROLLUP`, true AS `CUBE` UNION ALL SELECT 11, 'bar', false) "
        "SELECT `GROUPING SETS`, `ROLLUP`, `CUBE`, COUNT(*) FROM T GROUP BY "
    )
    assert same(
        evaluate(sql + "ROLLUP(`GROUPING SETS`, `ROLLUP`, `CUBE`)").rows,
        [(None, None, None, 2), (10, None, None, 1), (10, "bar", None, 1), (10, "bar", True, 1),
         (11, None, None, 1), (11, "bar", None, 1), (11, "bar", False, 1)],
    )
    assert same(
        evaluate(sql + "GROUPING SETS(`GROUPING SETS`, `ROLLUP`, `CUBE`)").rows,
        [(None, None, False, 1), (None, None, True, 1), (None, "bar", None, 2), (10, None, None, 1), (11, None, None, 1)],
    )
    # the CUBE's 8 sets: GROUPING SETS and CUBE take 2 values each, ROLLUP (a constant) one
    assert len(evaluate(sql + "CUBE(`GROUPING SETS`, `ROLLUP`, `CUBE`)").rows) == 2 * 6 + 1 + 1


def test_rollup_and_cube_inside_grouping_sets_contribute_their_sets():
    got = rows("SELECT x, s, COUNT(*) FROM T GROUP BY GROUPING SETS (ROLLUP (x, s), CUBE (s), ())")
    sets = [(1, "p", 1), (2, "q", 2), (None, "p", 1)]  # {x, s}
    sets += [(1, None, 1), (2, None, 2), (None, None, 1)]  # {x}
    sets += [(None, None, 4)] * 3  # the {} of the ROLLUP, of the CUBE, and ()
    sets += [(None, "p", 2), (None, "q", 2)]  # {s}
    assert same(got, sets)


def test_grouping_sets_with_recursive_cte():
    sql = (
        "WITH RECURSIVE c AS ((SELECT i AS it, i + 1 AS it1 FROM UNNEST([1, 2]) AS i GROUP BY GROUPING SETS (i, i + 1)) "
        "UNION ALL (SELECT it + 1, it1 + 1 FROM c WHERE it < 2)) SELECT it, it1 FROM c ORDER BY 1, 2"
    )
    # base: {i}: (1, 2), (2, 3); {i + 1}: i is NULL so i + 1 is NULL: (NULL, NULL) twice. Step: (it + 1, it1 + 1) of it < 2.
    assert same(evaluate(sql).rows, [(1, 2), (2, 3), (None, None), (None, None), (2, 3)])


# --- GROUP BY ALL ---------------------------------------------------------------------------------------------


def test_group_by_all_skips_window_function_items():
    got = rows("SELECT x, ROW_NUMBER() OVER (ORDER BY x) AS rn FROM T GROUP BY ALL ORDER BY x")
    assert got == [(None, 1), (1, 2), (2, 3)]
    got = rows("SELECT x, SUM(x), ROW_NUMBER() OVER () AS rn FROM T GROUP BY ALL QUALIFY rn = 1")
    assert len(got) == 1 and got[0][2] == 1


def test_group_by_all_uses_the_select_item_as_the_key():
    got = rows("SELECT x, x + 100 FROM T GROUP BY ALL ORDER BY 1")
    assert got == [(None, None), (1, 101), (2, 102)]


def test_group_by_all_with_a_window_over_an_aggregate():
    assert rows("SELECT x, SUM(COUNT(*)) OVER () AS total FROM T GROUP BY ALL ORDER BY x") == [(None, 4), (1, 4), (2, 4)]


# --- a SELECT alias inside a subquery of HAVING --------------------------------------------------------------


def test_alias_used_inside_a_having_subquery_is_declined_not_misreported():
    sql = "SELECT s, ARRAY_AGG(x) AS xs FROM T GROUP BY s HAVING EXISTS (SELECT 1 FROM UNNEST(xs) AS v WHERE v > 1)"
    with pytest.raises(Unsupported):
        rows(sql)
    with pytest.raises(AnalysisError):  # a name that is no alias stays an analysis error
        rows("SELECT s, COUNT(*) FROM T GROUP BY s HAVING EXISTS (SELECT 1 FROM UNNEST(nope) AS v)")


# --- the conformance runner reads STRUCT field names with spaces ---------------------------------------------


def _runner():
    path = Path(__file__).resolve().parent.parent / "tools" / "googlesql_conformance.py"
    spec = importlib.util.spec_from_file_location("googlesql_conformance_b", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_reads_struct_field_names_that_hold_spaces_and_keywords():
    G = _runner()
    expected = G.parse_expected(
        "ARRAY<STRUCT<GROUPING SETS INT64, ROLLUP STRING, CUBE BOOL, INT64>>[known order:\n"
        '  {NULL, "bar", true, 2},\n  {10, NULL, NULL, 1}\n]'
    )
    assert [n for n, _ in expected.type.fields] == ["GROUPING SETS", "ROLLUP", "CUBE", None]
    assert expected.rows == [(None, "bar", True, 2), (10, None, None, 1)]


def test_runner_header_with_nested_types_and_unnamed_fields():
    G = _runner()
    t = G.parse_header_type("STRUCT<a b ARRAY<STRUCT<c d INT64, STRING>>, ARRAY<INT64>, e NUMERIC>")
    assert [n for n, _ in t.fields] == ["a b", None, "e"]
    assert t.fields[0][1] == T.Type("ARRAY", elem=T.struct([("c d", T.INT64), (None, T.STRING)]))
    assert t.fields[2][1] == T.NUMERIC
