"""Bag-equivalence backend: date arithmetic with INTERVAL, SEMI and ANTI joins, and the TPC-H shapes behind them."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")

from kumosql.smt_equivalence import TableConstraints  # noqa: E402
from kumosql.uexpr import prove_bag_equivalent  # noqa: E402

SCHEMA = {"t": ["id", "d", "x"], "u": ["id", "x"]}
CONSTRAINTS = {"t": TableConstraints(keys=(("id",),)), "u": TableConstraints(keys=(("id",),))}
TYPES = {"t": {"id": "INT", "d": "DATE", "x": "DECIMAL(15, 2)"}, "u": {"id": "INT", "x": "INT"}}


def prove(left: str, right: str, dialect: str = "mysql") -> bool:
    return prove_bag_equivalent(
        left, right, schema=SCHEMA, constraints=CONSTRAINTS, types=TYPES, dialect=dialect,
        exact_arithmetic=True, compare_names=False, use_foreign_keys=False,
    ).proven


# ---- INTERVAL folding ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interval, folded",
    [
        ("DATE '1994-01-01' + INTERVAL '1' YEAR", "DATE '1995-01-01'"),
        ("DATE '1994-09-01' + INTERVAL '3' MONTH", "DATE '1994-12-01'"),
        ("DATE '1994-11-01' + INTERVAL 3 MONTH", "DATE '1995-02-01'"),
        ("DATE '1994-01-01' - INTERVAL '1' YEAR", "DATE '1993-01-01'"),
        ("DATE '1994-03-01' - INTERVAL 1 DAY", "DATE '1994-02-28'"),
        ("INTERVAL 10 DAY + DATE '1994-02-20'", "DATE '1994-03-02'"),
        ("DATE '1996-02-28' + INTERVAL 2 DAY", "DATE '1996-03-01'"),
        ("DATE '2023-01-31' + INTERVAL 1 MONTH", "DATE '2023-02-28'"),  # clamped to the month's last day
        ("DATE '2024-02-29' + INTERVAL 1 YEAR", "DATE '2025-02-28'"),
        ("DATE '2023-12-31' - INTERVAL 10 MONTH", "DATE '2023-02-28'"),
        ("DATE('1994-01-01 +08') + INTERVAL '1' YEAR", "DATE('1995-01-01 +08')"),
    ],
)
def test_constant_interval_arithmetic_folds_exactly(interval, folded):
    assert prove(f"SELECT id FROM t WHERE d >= {interval}", f"SELECT id FROM t WHERE d >= {folded}")


@pytest.mark.parametrize(
    "left, right",
    [
        ("DATE '1994-01-01' + INTERVAL '1' YEAR", "DATE '1995-01-02'"),
        ("DATE '2023-01-31' + INTERVAL 1 MONTH", "DATE '2023-03-03'"),  # not the overflow reading
        ("DATE '1994-01-01' - INTERVAL 1 MONTH", "DATE '1993-12-02'"),
        ("DATE '1994-01-01' + INTERVAL 1 YEAR", "DATE '1994-01-01' + INTERVAL 1 MONTH"),
    ],
)
def test_wrong_interval_arithmetic_is_not_proven(left, right):
    assert not prove(f"SELECT id FROM t WHERE d >= {left}", f"SELECT id FROM t WHERE d >= {right}")


def test_interval_over_a_column_is_not_folded_but_stays_sound():
    assert prove("SELECT id FROM t WHERE d + INTERVAL 1 YEAR > DATE '1995-01-01'", "SELECT id FROM t WHERE d + INTERVAL 1 YEAR > DATE '1995-01-01'")
    assert not prove("SELECT id FROM t WHERE d + INTERVAL 1 YEAR > DATE '1995-01-01'", "SELECT id FROM t WHERE d > DATE '1994-01-01'")


def test_year_function_matches_extract_over_a_date_column():
    assert prove("SELECT EXTRACT(YEAR FROM d) AS y FROM t", "SELECT year(d) AS y FROM t")


# ---- SEMI and ANTI joins ---------------------------------------------------------------------------------


def test_semi_join_is_an_exists_filter():
    assert prove("SELECT t.id FROM t LEFT SEMI JOIN u ON t.x = u.x", "SELECT id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.x = u.x)")


def test_anti_join_is_a_not_exists_filter():
    assert prove("SELECT t.id FROM t LEFT ANTI JOIN u ON t.x = u.x", "SELECT id FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE t.x = u.x)")


def test_semi_join_keeps_each_left_row_once():
    # u may hold several matching rows, so an inner join would multiply the left row.
    assert prove("SELECT t.id FROM t LEFT SEMI JOIN u ON t.id = u.x", "SELECT id FROM t WHERE id IN (SELECT x FROM u)")
    assert not prove("SELECT t.id FROM t LEFT SEMI JOIN u ON t.id = u.x", "SELECT t.id FROM t JOIN u ON t.id = u.x")


def test_anti_join_is_not_the_complement_of_a_nullable_not_in():
    # NOT IN is UNKNOWN for a NULL in the subquery; an anti join keeps the row.
    assert not prove("SELECT t.id FROM t LEFT ANTI JOIN u ON t.id = u.x", "SELECT id FROM t WHERE id NOT IN (SELECT x FROM u)")


def test_semi_and_anti_are_not_interchangeable():
    assert not prove("SELECT t.id FROM t LEFT SEMI JOIN u ON t.x = u.x", "SELECT t.id FROM t LEFT ANTI JOIN u ON t.x = u.x")


def test_columns_of_the_right_side_are_not_visible_after_a_semi_join():
    assert not prove("SELECT u.x FROM t LEFT SEMI JOIN u ON t.x = u.x", "SELECT t.x FROM t")


def test_right_semi_join_is_not_modeled():
    assert not prove("SELECT u.id FROM t RIGHT SEMI JOIN u ON t.x = u.x", "SELECT id FROM u WHERE EXISTS (SELECT 1 FROM t WHERE t.x = u.x)")


# ---- shapes behind the remaining TPC-H pairs -------------------------------------------------------------


def test_shared_conjuncts_of_an_or_are_pulled_out_so_keys_can_apply():
    left = "SELECT SUM(u.x) FROM t, u WHERE (t.id = u.id AND t.x > 1) OR (t.id = u.id AND t.x < -1)"
    right = "SELECT SUM(u.x) FROM t, u WHERE t.id = u.id AND (t.x > 1 OR t.x < -1)"
    assert prove(left, right)
    assert not prove(left, "SELECT SUM(u.x) FROM t, u WHERE t.id = u.id AND t.x > 1")


def test_widening_decimal_cast_is_the_identity_but_narrowing_is_not():
    assert prove("SELECT id FROM t WHERE CAST(x AS DECIMAL(21, 7)) < 5", "SELECT id FROM t WHERE x < 5")
    assert not prove("SELECT id FROM t WHERE CAST(x AS DECIMAL(15, 1)) < 5", "SELECT id FROM t WHERE x < 5")
    assert not prove("SELECT id FROM t WHERE CAST(x AS DECIMAL(10, 7)) < 5", "SELECT id FROM t WHERE x < 5")


def test_widening_cast_is_seen_through_a_derived_table():
    assert prove(
        "SELECT id FROM (SELECT id, x FROM t) AS s WHERE CAST(x AS DECIMAL(21, 7)) < 5",
        "SELECT id FROM t WHERE x < 5",
    )


def test_not_null_test_is_implied_by_a_comparison_of_the_same_value():
    assert prove("SELECT id FROM t WHERE x > 1 AND x IS NOT NULL", "SELECT id FROM t WHERE x > 1")
    assert prove("SELECT id FROM t WHERE x * 2 > 1 AND x IS NOT NULL", "SELECT id FROM t WHERE x * 2 > 1")
    assert not prove("SELECT id FROM t WHERE x > 1 OR x IS NOT NULL", "SELECT id FROM t WHERE x > 1")


def test_a_semi_join_applied_twice_equals_once():
    once = "SELECT t.id FROM t LEFT SEMI JOIN (SELECT x FROM u GROUP BY x HAVING SUM(id) > 3) AS g ON t.x = g.x"
    twice = f"SELECT s.id FROM ({once}) AS s LEFT SEMI JOIN (SELECT x FROM u GROUP BY x HAVING SUM(id) > 3) AS g ON s.id = g.x"
    assert not prove(once, twice)  # a different join condition: never merged
    same = "SELECT t.id FROM t LEFT SEMI JOIN (SELECT x FROM u GROUP BY x HAVING SUM(id) > 3) AS g ON t.x = g.x LEFT SEMI JOIN (SELECT x FROM u GROUP BY x HAVING SUM(id) > 3) AS h ON t.x = h.x"
    assert prove(once, same)


def test_comparison_is_null_means_an_operand_is_null():
    assert prove("SELECT id FROM t WHERE (x = d) IS NULL", "SELECT id FROM t WHERE x IS NULL OR d IS NULL")
    assert not prove("SELECT id FROM t WHERE (x = d) IS NULL", "SELECT id FROM t WHERE x IS NULL")
    assert prove("SELECT id FROM t WHERE (x = id) IS NULL", "SELECT id FROM t WHERE x IS NULL")  # id is a key, never NULL


# ---- a few TPC-H pairs, end to end -----------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import uexpr_bench  # noqa: E402
import sqlsolver_bench as bench  # noqa: E402

pytestmark = pytest.mark.eval

TPCH_PROVED = [3, 4, 14, 16, 17, 1, 15]  # INTERVAL, SEMI/ANTI, OR-factoring, key-elimination order and widening casts


@pytest.mark.parametrize("index", TPCH_PROVED)
def test_tpch_pairs_with_interval_and_semi_anti_joins(index):
    tables = bench.load_schema(bench.FIXTURES / "tpch.schema.sql")
    pairs = bench.load_pairs(bench.FIXTURES / "tpch_pairs.txt")
    assert uexpr_bench.prove(*pairs[index][:2], tables)
