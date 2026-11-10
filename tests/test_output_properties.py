"""Output-property inference: the worked examples, then the labelled corpus against executed data."""

import importlib.util
import sys
from pathlib import Path

import pytest

from kumosql.output_properties import infer_properties
from kumosql.smt_equivalence import TableConstraints

pytestmark = pytest.mark.eval

SCHEMA = {"orders": ["id", "customer_id", "amount"], "customers": ["id", "name"], "items": ["order_id", "line", "qty"]}
CONSTRAINTS = {
    "orders": TableConstraints(not_null=frozenset({"id", "customer_id"}), keys=(("id",),)),
    "customers": TableConstraints(not_null=frozenset({"id", "name"}), keys=(("id",),)),
    "items": TableConstraints(not_null=frozenset({"order_id", "line", "qty"}), keys=(("order_id", "line"),)),
}


def props(sql, constraints=CONSTRAINTS):
    result = infer_properties(sql, constraints, SCHEMA)
    assert not result.unsupported, result.unsupported
    return result


def test_group_by_makes_one_row_per_group():
    result = props("SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id")
    assert result.is_unique("customer_id")
    assert result.non_null("n") and result.non_null("customer_id")
    assert not result.at_most_one_row


def test_a_join_can_destroy_uniqueness_that_the_input_had():
    assert props("SELECT id FROM orders").is_unique("id")
    fanned = props("SELECT o.id FROM orders o JOIN items i ON i.order_id = o.id")
    assert not fanned.is_unique("id")
    kept = props("SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id")
    assert kept.is_unique("id")


def test_count_star_and_sum_differ_on_empty_input():
    result = props("SELECT COUNT(*) AS n, SUM(amount) AS s FROM orders")
    assert result.exactly_one_row
    assert result.non_null("n") and not result.non_null("s")
    grouped = props("SELECT customer_id, SUM(amount) AS s FROM orders GROUP BY customer_id")
    assert not grouped.non_null("s")  # amount itself may be NULL
    notnull = props("SELECT order_id, SUM(qty) AS q FROM items GROUP BY order_id")
    assert notnull.non_null("q")  # a group is never empty
    assert not props("SELECT SUM(qty) AS q FROM items").non_null("q")  # the table may be empty


def test_a_filter_establishes_non_null_and_a_later_outer_join_undoes_it():
    assert props("SELECT amount FROM orders WHERE amount IS NOT NULL").non_null("amount")
    assert props("SELECT amount FROM orders WHERE amount > 5").non_null("amount")
    assert not props("SELECT amount FROM orders WHERE amount > 5 OR id = 1").non_null("amount")
    joined = props(
        "SELECT f.id, c.id AS cid FROM (SELECT id, customer_id FROM orders WHERE amount IS NOT NULL) AS f "
        "LEFT JOIN customers c ON c.id = f.customer_id"
    )
    assert joined.non_null("id") and not joined.non_null("cid")
    assert props("SELECT c.name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE c.name = 'x'").non_null("name")


def test_scalar_subquery_cardinality():
    assert props("SELECT name FROM customers WHERE id = 5").scalar_subquery == "at_most_one"
    assert props("SELECT name FROM customers WHERE id = o.customer_id").scalar_subquery == "at_most_one"
    assert props("SELECT i.qty FROM items i WHERE i.order_id = o.id").scalar_subquery == "unknown"
    assert props("SELECT i.qty FROM items i WHERE i.order_id = o.id AND i.line = 1").scalar_subquery == "at_most_one"
    assert props("SELECT MAX(qty) FROM items WHERE order_id = o.id").scalar_subquery == "exactly_one"
    assert props("SELECT id FROM orders LIMIT 1").scalar_subquery == "at_most_one"


def test_without_the_declaration_nothing_is_claimed_and_declared_facts_are_named():
    bare = infer_properties("SELECT id FROM orders", {}, SCHEMA)
    assert not bare.non_null("id") and not bare.is_unique("id")
    declared = props("SELECT id FROM orders")
    assert declared.assumptions_for("id") == ("orders.id is NOT NULL",)
    assert declared.keys[0].assumptions == ("(id) is unique in orders",)
    # a fact that follows from the query alone rests on nothing
    assert props("SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id").keys[0].assumptions == ()


def test_unknown_tables_and_shapes_are_unsupported_not_guessed():
    assert infer_properties("SELECT x FROM nowhere", {}, SCHEMA).unsupported
    assert infer_properties("SELECT * FROM UNNEST([1,2]) AS x", {}, SCHEMA).unsupported


def test_a_repeated_output_name_keeps_its_facts_by_position():
    result = props("SELECT o.id, c.id FROM orders o JOIN customers c ON c.id = o.customer_id")
    assert [c.name for c in result.columns] == ["id", "id"]
    assert [k.positions for k in result.keys] == [(0,)]  # the orders id, not the customers id
    assert result.column("id") is None  # ambiguous by name
    outer = props("SELECT t.id FROM (SELECT o.id, c.id FROM orders o JOIN customers c ON c.id = o.customer_id) t")
    assert not outer.non_null("id") and not outer.keys  # which id is meant is not known
    star = props("SELECT * FROM (SELECT o.id, c.id FROM orders o JOIN customers c ON c.id = o.customer_id) t")
    assert [k.positions for k in star.keys] == [(0,)]


def test_values_rows_are_read_as_literals():
    two = props("SELECT * FROM (VALUES (1, 'a'), (2, 'A')) AS t(x, y)")
    assert two.non_null("x") and two.non_null("y")
    assert two.is_unique("x") and not two.is_unique("y")  # 'a' and 'A' can compare equal
    one = props("SELECT * FROM (VALUES (1, NULL)) AS t(x, y)")
    assert one.exactly_one_row and one.non_null("x") and not one.non_null("y")
    assert not props("SELECT * FROM (VALUES (1), (1.0)) AS t(x)").is_unique("x")


def test_grouping_sets_null_out_columns_outside_a_set():
    rolled = props("SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY ROLLUP(customer_id)")
    assert not rolled.non_null("customer_id") and rolled.non_null("n")
    assert rolled.is_unique("customer_id")  # the grand total row is the only NULL: customer_id is NOT NULL
    assert not props("SELECT amount FROM orders GROUP BY ROLLUP(amount)").is_unique("amount")  # a NULL amount repeats it
    assert not props("SELECT customer_id FROM orders GROUP BY GROUPING SETS ((customer_id), (customer_id))").keys
    kept = props("SELECT customer_id, id FROM orders GROUP BY customer_id, ROLLUP(id)")
    assert kept.non_null("customer_id") and not kept.non_null("id")
    assert not props("SELECT customer_id AS c FROM orders GROUP BY ROLLUP(c)").non_null("c")
    assert not props("SELECT SUM(qty) AS q FROM items GROUP BY GROUPING SETS ((order_id), ())").non_null("q")  # () sees an empty table


def test_lateral_and_parenthesised_joins():
    lateral = props(
        "SELECT o.id, s.m FROM orders o LEFT JOIN LATERAL (SELECT MAX(i.qty) AS m FROM items i WHERE i.order_id = o.id) s ON TRUE"
    )
    assert lateral.is_unique("id") and not lateral.non_null("m")
    nested = props("SELECT o.id, c.name FROM (orders o LEFT JOIN customers c ON c.id = o.customer_id)")
    assert nested.is_unique("id") and not nested.non_null("name")


def test_column_alias_lists_rename_and_recursion_is_unsupported():
    renamed = props("WITH c(a, b) AS (SELECT id, customer_id FROM orders) SELECT a, b FROM c")
    assert renamed.is_unique("a") and renamed.non_null("b")
    assert props("SELECT p FROM orders AS o(p, q, r)").is_unique("p")
    assert infer_properties("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r) SELECT n FROM r", {}, SCHEMA).unsupported


_path = Path(__file__).resolve().parent.parent / "tools" / "output_properties_bench.py"
_spec = importlib.util.spec_from_file_location("output_properties_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["output_properties_bench"] = bench
_spec.loader.exec_module(bench)

pytest.importorskip("duckdb")

FLOOR_PROVED = 60


def test_labelled_corpus_has_no_wrong_claims():
    result = bench.run("cases.json", trials=40)
    assert result["wrong"] == [] and result["analysis_violations"] == [], result
    assert result["label_wrong"] == [], result["label_wrong"]
    assert result["error"] == 0
    assert result["proved"] >= FLOOR_PROVED, result["proved"]
