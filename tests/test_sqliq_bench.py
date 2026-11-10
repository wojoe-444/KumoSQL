"""SQL-IQ harness: the judge's decisions on small hand-written pairs (no SQL-IQ checkout needed)."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqliq_bench.py"
_spec = importlib.util.spec_from_file_location("sqliq_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sqliq_bench"] = bench
_spec.loader.exec_module(bench)

TABLES = {"t": {"id": "INTEGER", "a": "INTEGER", "name": "TEXT"}, "u": {"id": "INTEGER", "t_id": "INTEGER"}}


def pair(sql1, sql2, label="yes", keys=None):
    return bench.Pair(0, sql1, sql2, TABLES, keys or {"t": ("id",)}, label)


def test_a_rewrite_is_proved():
    answer, how = bench.judge(pair("SELECT a FROM t WHERE a > 1 AND a < 5", "SELECT a FROM t WHERE a < 5 AND a > 1"))
    assert (answer, how) == ("yes", "proved")


def test_a_changed_literal_is_found_by_testing():
    answer, how = bench.judge(pair("SELECT id FROM t WHERE name = 'x'", "SELECT id FROM t WHERE name = 'X'", "no"))
    assert answer == "no"
    assert how == "differs"


def test_output_names_do_not_matter():
    assert bench.judge(pair("SELECT a AS x FROM t", "SELECT a AS y FROM t"))[0] == "yes"


def test_unparseable_sql_is_answered_no():
    assert bench.judge(pair("SELECT nope FROM missing", "SELECT a FROM t", "no"))[0] == "no"


def test_score_reports_both_classes():
    pairs = [pair("SELECT 1", "SELECT 1", "yes"), pair("SELECT 1", "SELECT 2", "no")]
    metrics = bench.score(pairs, [("yes", "proved"), ("yes", "tested")])
    assert metrics["correct"] == 1
    assert metrics["equivalent_accuracy"] == 1.0
    assert metrics["non_equivalent_accuracy"] == 0.0
    assert metrics["geometric_mean"] == 0.0


SCHEMA = """【DB_ID】 shop
【Schema】
# Table: main.orders
[
(id:INTEGER, id, Primary Key, Examples: [1, 2]),
(status:TEXT, order status, Examples: [shipped, open]),
(amount:REAL, order amount, Examples: [9.5])
]
"""


def test_judge_prefers_the_query_the_question_supports():
    import sqliq_judge

    question = "How many orders are shipped?"
    good = "SELECT COUNT(*) FROM orders WHERE status = 'shipped'"
    bad = "SELECT id, amount FROM orders WHERE status = 'returned' AND amount > 77 LIMIT 5"
    assert sqliq_judge.judge(question, "", SCHEMA, good, bad) == "A"
    assert sqliq_judge.judge(question, "", SCHEMA, bad, good) == "B"


def test_error_rules_call_a_grounded_query_correct_and_an_invented_column_wrong():
    import sqliq_errors

    question = "How many orders are shipped?"
    assert sqliq_errors.classify(question, "", SCHEMA, "SELECT COUNT(*) FROM orders WHERE status = 'shipped'") == []
    wrong = sqliq_errors.classify(question, "", SCHEMA, "SELECT COUNT(*) FROM orders WHERE state = 'shipped'")
    assert "Attribute-Related Errors" in wrong
