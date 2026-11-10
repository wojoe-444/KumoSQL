"""Filter-split UNION DISTINCT branches merged into one SELECT DISTINCT (src/kumosql/distinct_partition_rules.py)."""

import pytest
import sqlglot

pytest.importorskip("z3")

from kumosql.algebraic_equivalence import prove_equivalent_algebraic  # noqa: E402
from kumosql.distinct_partition_rules import merge_distinct_partitions  # noqa: E402
from kumosql.smt_equivalence import SmtStatus  # noqa: E402

SCHEMA = {"t": ["a", "b", "c"], "u": ["a", "b", "c"]}
TYPES = {table: {column: "INT64" for column in ("a", "b", "c")} for table in SCHEMA}
PROVEN = SmtStatus.PROVEN_EQUIVALENT


def _prove(left, right):
    return prove_equivalent_algebraic(left, right, schema=SCHEMA, types=TYPES, compare_names=False).status


def _rule(sql):
    return merge_distinct_partitions(sqlglot.parse_one(sql, read="bigquery")).sql(dialect="bigquery")


def _same(sql):
    return sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery")


def _tlp(query, p, op="UNION DISTINCT", parts=3):
    filters = [f"({p})", f"NOT ({p})", f"({p}) IS NULL"][:parts]
    return f" {op} ".join(f"{query} WHERE {f}" for f in filters)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT a AS k0, b AS k1 FROM t",
        "SELECT y.b AS k0, y.a AS k1, x.b AS k2 FROM t AS x JOIN u AS y ON y.a IN (2, 2)",
        "SELECT x.a AS k0, y.b AS k1 FROM t AS x LEFT JOIN u AS y ON (y.b BETWEEN 1 AND 3) AND (x.b <= 2)",
        "SELECT y.b AS k0, x.a AS k1 FROM t AS x CROSS JOIN u AS y",
    ],
)
def test_three_distinct_partitions_are_the_distinct_query(query):
    p = "(x.b > 0 AND y.a <> 0) OR x.c IN (1, 2)" if " x " in f"{query} " else "(b > 0 AND a <> 0) OR c IN (1, 2)"
    distinct = query.replace("SELECT ", "SELECT DISTINCT ", 1)
    assert _rule(_tlp(query, p)) == _same(distinct)
    assert _prove(distinct, _tlp(query, p)) is PROVEN


def test_a_distinct_union_of_union_all_partitions_is_the_distinct_query():
    q = "SELECT a AS k FROM t"
    nested = f"{q} WHERE (b > 0) UNION DISTINCT ({q} WHERE NOT (b > 0) UNION ALL {q} WHERE (b > 0) IS NULL)"
    assert _rule(nested) == "SELECT DISTINCT a AS k FROM t"
    assert _prove("SELECT DISTINCT a AS k FROM t", nested) is PROVEN
    # A branch that is SELECT DISTINCT itself merges too: the union removes the duplicates anyway.
    marked = f"SELECT DISTINCT a AS k FROM t WHERE (b > 0) UNION DISTINCT {q} WHERE NOT (b > 0) UNION DISTINCT {q} WHERE (b > 0) IS NULL"
    assert _rule(marked) == "SELECT DISTINCT a AS k FROM t"


def test_overlapping_filters_merge_into_an_or_under_set_semantics():
    q = "SELECT a AS k FROM t"
    three = f"{q} WHERE b > 0 UNION DISTINCT {q} WHERE c = 1 UNION DISTINCT {q} WHERE b > 0 OR a = 2"
    assert _rule(three) == "SELECT DISTINCT a AS k FROM t WHERE (b > 0) OR (c = 1) OR (b > 0 OR a = 2)"
    assert _prove("SELECT DISTINCT a AS k FROM t WHERE b > 0 OR c = 1 OR a = 2", three) is PROVEN
    assert _prove("SELECT DISTINCT a AS k FROM t WHERE b > 0 OR c = 1", three) is not PROVEN


def test_union_all_partitions_are_not_the_distinct_query():
    # Near miss: the same partitions under UNION ALL keep every duplicate the DISTINCT query drops.
    q = "SELECT a AS k FROM t"
    assert _rule(_tlp(q, "b > 0", op="UNION ALL")) == _same(_tlp(q, "b > 0", op="UNION ALL"))
    assert _prove("SELECT DISTINCT a AS k FROM t", _tlp(q, "b > 0", op="UNION ALL")) is not PROVEN
    # ... and the distinct partitions are not the query with its duplicates.
    assert _prove(q, _tlp(q, "b > 0")) is not PROVEN
    # A trailing UNION ALL branch is outside the duplicate removal of the unions before it.
    tail = f"{q} WHERE (b > 0) UNION DISTINCT {q} WHERE NOT (b > 0) UNION ALL {q} WHERE (b > 0) IS NULL"
    assert _rule(tail) == f"(SELECT DISTINCT a AS k FROM t WHERE (b > 0) OR (NOT (b > 0))) UNION ALL {q} WHERE (b > 0) IS NULL"
    assert _prove("SELECT DISTINCT a AS k FROM t", tail) is not PROVEN


def test_true_and_false_partitions_miss_the_null_rows():
    # Near miss: without the IS NULL branch a row whose filter is NULL is lost.
    q = "SELECT a AS k FROM t"
    two = _tlp(q, "b > 0", parts=2)
    assert _rule(two) == "SELECT DISTINCT a AS k FROM t WHERE (b > 0) OR (NOT (b > 0))"
    assert _prove("SELECT DISTINCT a AS k FROM t", two) is SmtStatus.NOT_EQUIVALENT


def test_partitions_inside_a_larger_union_keep_its_duplicate_removal():
    q = "SELECT a AS k FROM t"
    sql = f"{_tlp(q, 'c = 1')} UNION DISTINCT SELECT c AS k FROM u"
    assert _rule(sql) == "SELECT a AS k FROM t UNION DISTINCT SELECT c AS k FROM u"
    assert _prove("SELECT DISTINCT a AS k FROM t UNION DISTINCT SELECT c AS k FROM u", sql) is PROVEN
    assert _prove("SELECT DISTINCT a AS k FROM t UNION ALL SELECT c AS k FROM u", sql) is not PROVEN
    # Under an aggregate the merged branch keeps the union's DISTINCT: COUNT(*) counts the set.
    counted = f"SELECT COUNT(*) AS n FROM ({_tlp(q, 'c = 1')}) AS d"
    assert _rule(counted) == "SELECT COUNT(*) AS n FROM (SELECT DISTINCT a AS k FROM t) AS d"
    assert _prove("SELECT COUNT(*) AS n FROM (SELECT DISTINCT a AS k FROM t) AS d", counted) is PROVEN
    assert _prove("SELECT COUNT(*) AS n FROM t", counted) is not PROVEN


def test_a_tail_on_the_union_stays_on_the_union():
    q = "SELECT a AS k FROM t"
    sql = f"{_tlp(q, 'b > 0')} ORDER BY k LIMIT 1"
    # Only the operands below the ORDER BY / LIMIT union merge; the tail keeps reading the whole set.
    assert _rule(sql) == (
        "(SELECT DISTINCT a AS k FROM t WHERE (b > 0) OR (NOT (b > 0))) UNION DISTINCT "
        "SELECT a AS k FROM t WHERE (b > 0) IS NULL ORDER BY k LIMIT 1"
    )


@pytest.mark.parametrize(
    "sql",
    [
        # different select lists or sources
        "SELECT a AS k FROM t WHERE (b > 0) UNION DISTINCT SELECT b AS k FROM t WHERE NOT (b > 0) UNION DISTINCT SELECT c AS k FROM t WHERE (b > 0) IS NULL",
        "SELECT a AS k FROM t WHERE (b > 0) UNION DISTINCT SELECT a AS k FROM u WHERE NOT (b > 0)",
        "SELECT x.a AS k FROM t AS x JOIN u AS y ON x.a = y.a WHERE (x.b > 0) UNION DISTINCT SELECT x.a AS k FROM t AS x LEFT JOIN u AS y ON x.a = y.a WHERE NOT (x.b > 0)",
        # a LIMIT, a volatile filter or select list, an aggregate, a window, a grouping, a subquery in the filter
        "(SELECT a AS k FROM t WHERE (b > 0) LIMIT 1) UNION DISTINCT SELECT a AS k FROM t WHERE NOT (b > 0)",
        "SELECT a AS k FROM t WHERE RAND() < 0.5 UNION DISTINCT SELECT a AS k FROM t WHERE NOT (RAND() < 0.5)",
        "SELECT a + RAND() AS k FROM t WHERE b > 0 UNION DISTINCT SELECT a + RAND() AS k FROM t WHERE NOT (b > 0)",
        "SELECT COUNT(*) AS k FROM t WHERE b > 0 UNION DISTINCT SELECT COUNT(*) AS k FROM t WHERE NOT (b > 0)",
        "SELECT ROW_NUMBER() OVER (ORDER BY a) AS k FROM t WHERE b > 0 UNION DISTINCT SELECT ROW_NUMBER() OVER (ORDER BY a) AS k FROM t WHERE NOT (b > 0)",
        "SELECT a AS k FROM t WHERE b > 0 GROUP BY a UNION DISTINCT SELECT a AS k FROM t WHERE NOT (b > 0) GROUP BY a",
        "SELECT a AS k FROM t WHERE b IN (SELECT a FROM u) UNION DISTINCT SELECT a AS k FROM t WHERE NOT (b IN (SELECT a FROM u))",
        "SELECT a AS k FROM (SELECT * FROM t LIMIT 2) AS d WHERE b > 0 UNION DISTINCT SELECT a AS k FROM (SELECT * FROM t LIMIT 2) AS d WHERE NOT (b > 0)",
    ],
)
def test_branches_that_differ_or_cannot_move_their_filter_are_left_alone(sql):
    assert _rule(sql) == _same(sql)


@pytest.mark.parametrize(
    "right",
    [
        "SELECT a AS k FROM t WHERE (b > 0) UNION DISTINCT SELECT b AS k FROM t WHERE NOT (b > 0) UNION DISTINCT SELECT a AS k FROM t WHERE (b > 0) IS NULL",
        "SELECT a AS k FROM t WHERE (b > 0) UNION DISTINCT SELECT a AS k FROM u WHERE NOT (b > 0) UNION DISTINCT SELECT a AS k FROM t WHERE (b > 0) IS NULL",
        "SELECT a AS k FROM t WHERE (b > 0) UNION DISTINCT (SELECT a AS k FROM t WHERE NOT (b > 0) LIMIT 1) UNION DISTINCT SELECT a AS k FROM t WHERE (b > 0) IS NULL",
        "SELECT a AS k FROM t WHERE (RAND() < 0.5) UNION DISTINCT SELECT a AS k FROM t WHERE NOT (RAND() < 0.5) UNION DISTINCT SELECT a AS k FROM t WHERE (RAND() < 0.5) IS NULL",
    ],
)
def test_near_miss_partitions_are_not_the_distinct_query(right):
    assert _prove("SELECT DISTINCT a AS k FROM t", right) is not PROVEN


def test_distinct_on_and_non_complementary_filters_are_not_the_distinct_query():
    q = "SELECT a AS k FROM t"
    on = "SELECT DISTINCT ON (b) a AS k, b FROM t"
    on_parts = f"{on} WHERE (c > 0) UNION DISTINCT {on} WHERE NOT (c > 0) UNION DISTINCT {on} WHERE (c > 0) IS NULL"
    assert _rule(on_parts) == _same(on_parts)  # DISTINCT ON keeps one row per key: a moved filter changes which
    assert _prove("SELECT DISTINCT ON (b) a AS k, b FROM t", on_parts) is not PROVEN
    # the third filter tests a different atom than the first two, so the three do not cover the rows
    other = f"{q} WHERE b > 0 UNION DISTINCT {q} WHERE NOT (c > 0) UNION DISTINCT {q} WHERE (b > 0) IS NULL"
    assert _prove("SELECT DISTINCT a AS k FROM t", other) is not PROVEN


def test_a_limit_tail_on_partitions_is_not_proved_against_the_wrong_distinct_query():
    q = "SELECT a AS k FROM t"
    tail = f"{_tlp(q, 'b > 0')} LIMIT 1"
    assert _prove("SELECT DISTINCT a AS k FROM t LIMIT 2", tail) is not PROVEN
    assert _prove("SELECT a AS k FROM t LIMIT 1", tail) is not PROVEN
