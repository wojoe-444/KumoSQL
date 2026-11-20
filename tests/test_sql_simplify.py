"""Simpler equivalent forms of one query (kumosql.sql_simplify)."""

import types

import pytest
import sqlglot
from sqlglot import exp

from kumosql.formatting import complexity
from kumosql.sql_simplify import simpler_forms, tidy


@pytest.fixture(autouse=True)
def idle_clock(monkeypatch):
    """simpler_forms spends less effort (a second rewrite round, the candidates it scores) when its own
    clock shows a slow host, so the exact forms asserted here are those of an idle machine: pin the
    clock it reads, and a loaded test run cannot change them."""

    monkeypatch.setattr("kumosql.sql_simplify.time", types.SimpleNamespace(perf_counter=lambda: 0.0))


COLUMNS = {
    "m.orders": ["id", "customer_id", "amount", "status"],
    "m.customers": ["id", "name", "region"],
    "m.t": ["k", "x"],
    "m.a": ["id"],
    "m.b": ["id", "v"],
}

NESTED = (
    "SELECT customer_id, SUM(amount) AS total FROM (SELECT id, customer_id, amount, status FROM "
    "(SELECT id, customer_id, amount, status FROM m.orders) AS t1 WHERE amount > 0) AS t2 "
    "WHERE status = 'paid' GROUP BY customer_id"
)
FOLDED = "SELECT customer_id, SUM(amount) AS total FROM m.orders WHERE status = 'paid' AND amount > 0 GROUP BY customer_id"


def names(sql):
    select = sqlglot.parse_one(sql, read="bigquery")
    while not isinstance(select, exp.Select):
        select = select.this
    return [item.alias_or_name for item in select.expressions]


def check_contract(sql, out, columns=COLUMNS):
    """Every candidate parses, scores no worse than the input, and keeps the output names."""

    base = complexity(sql).score
    assert len(set(out)) == len(out) and sql not in out
    keys = []
    for candidate in out:
        sqlglot.parse_one(candidate, read="bigquery")
        score = complexity(candidate).score
        assert score <= base
        assert names(candidate) == names(sql) or "*" in names(sql)
        assert tidy(candidate, columns) == candidate
        keys.append((score, len(candidate)))
    assert keys == sorted(keys)


def proven(left, right, columns=COLUMNS):
    pytest.importorskip("z3")
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    return prove_equivalent_algebraic(left, right, schema=columns).proven


@pytest.mark.parametrize("columns", [COLUMNS, None], ids=["columns", "no-columns"])
def test_nested_derived_tables_fold_into_one_select(columns):
    out = simpler_forms(NESTED, columns)
    assert out[0] == FOLDED
    check_contract(NESTED, out)
    assert proven(NESTED, out[0])


def test_cte_chain_folds():
    sql = (
        "WITH t1 AS (SELECT id, customer_id, amount, status FROM m.orders), "
        "t2 AS (SELECT id, customer_id, amount, status FROM t1 WHERE amount > 0) "
        "SELECT customer_id, SUM(amount) AS total FROM t2 WHERE status = 'paid' GROUP BY customer_id"
    )
    out = simpler_forms(sql, COLUMNS)
    assert out[0] == FOLDED
    check_contract(sql, out)
    assert proven(sql, out[0])


def test_join_of_two_folded_tables():
    sql = (
        "SELECT o.id, c.name FROM (SELECT id, customer_id FROM m.orders WHERE amount > 0) AS o "
        "JOIN (SELECT id, name FROM m.customers WHERE region = 'EU') AS c ON o.customer_id = c.id"
    )
    out = simpler_forms(sql, COLUMNS)
    assert out
    metrics = complexity(out[0]).metrics
    assert metrics["subqueries"] == 0 and metrics["ctes"] == 0 and metrics["joins"] == 1
    check_contract(sql, out)
    assert proven(sql, out[0])


def test_filter_over_grouped_table_becomes_having():
    sql = "SELECT k, n FROM (SELECT k, COUNT(*) AS n FROM m.t GROUP BY k) AS d WHERE n > 1"
    out = simpler_forms(sql, COLUMNS)
    assert out[0] == "SELECT k, COUNT(*) AS n FROM m.t GROUP BY k HAVING COUNT(*) > 1"
    # the filter never lands in a WHERE below the grouping
    assert all("WHERE COUNT" not in o and "GROUP BY k WHERE" not in o for o in out)
    check_contract(sql, out)
    assert proven(sql, out[0])


def test_aggregate_over_folded_table():
    sql = (
        "SELECT customer_id, total FROM (SELECT customer_id, SUM(amount) AS total FROM "
        "(SELECT customer_id, amount FROM m.orders WHERE status = 'paid') AS t1 GROUP BY customer_id) AS t2 "
        "WHERE total > 100"
    )
    out = simpler_forms(sql, COLUMNS)
    assert out[0] == (
        "SELECT customer_id, SUM(amount) AS total FROM m.orders WHERE status = 'paid' "
        "GROUP BY customer_id HAVING SUM(amount) > 100"
    )
    check_contract(sql, out)
    assert proven(sql, out[0])


def test_filter_moves_under_distinct_only_when_every_column_is_kept():
    kept = "SELECT x, k FROM (SELECT DISTINCT k, x FROM m.t) AS d WHERE x > 1"
    out = simpler_forms(kept, COLUMNS)
    assert out[0] == "SELECT DISTINCT x, k FROM m.t WHERE x > 1"
    assert proven(kept, out[0])
    dropped = "SELECT x FROM (SELECT DISTINCT k, x FROM m.t) AS d WHERE x > 1"
    out = simpler_forms(dropped, COLUMNS)
    assert out and all("DISTINCT k, x" in o for o in out)
    check_contract(dropped, out)


def test_filter_stays_above_limit():
    sql = "SELECT x FROM (SELECT x FROM m.t ORDER BY x LIMIT 10) AS d WHERE x > 1"
    for candidate in simpler_forms(sql, COLUMNS):
        tree = sqlglot.parse_one(candidate, read="bigquery")
        limited = [s for s in tree.find_all(exp.Select) if s.args.get("limit")]
        assert limited and all(s.args.get("where") is None for s in limited)


@pytest.mark.parametrize("call", ["RAND()", "CURRENT_TIMESTAMP()", "GENERATE_UUID()"])
def test_non_deterministic_values_are_not_copied(call):
    sql = f"SELECT r, r AS r2 FROM (SELECT {call} AS r FROM m.t) AS d"
    for candidate in simpler_forms(sql, COLUMNS):
        assert candidate.upper().count(call.split("(")[0]) == 1


def test_computed_column_of_outer_joined_table_is_not_merged():
    sql = "SELECT a.id, b.v FROM m.a AS a LEFT JOIN (SELECT id, COALESCE(v, 0) AS v FROM m.b) AS b ON a.id = b.id"
    out = simpler_forms(sql, COLUMNS)
    for candidate in out:
        root = sqlglot.parse_one(candidate, read="bigquery")
        assert not any(e.find(exp.Coalesce) for e in root.expressions)
    check_contract(sql, out)


def test_output_names_keep_their_spelling():
    sql = (
        "SELECT CustomerId, `Total`, SUM(n) FROM (SELECT customer_id AS CustomerId, SUM(amount) AS Total, "
        "COUNT(*) AS n FROM m.orders GROUP BY customer_id) AS d GROUP BY CustomerId, `Total`"
    )
    out = simpler_forms(sql, COLUMNS)
    for candidate in out:
        assert names(candidate) == ["CustomerId", "Total", ""]
        assert "_col_" not in candidate
    check_contract(sql, out)


def test_star_folds_without_known_columns():
    sql = "WITH t1 AS (SELECT * FROM m.unknown), t2 AS (SELECT * FROM t1 WHERE amount > 0) SELECT k, COUNT(*) AS n FROM t2 GROUP BY k"
    out = simpler_forms(sql)
    assert out[0] == "SELECT k, COUNT(*) AS n FROM m.unknown AS t2 WHERE amount > 0 GROUP BY k"
    check_contract(sql, out, None)


def test_star_expands_only_to_known_columns():
    sql = "SELECT * FROM (SELECT * FROM m.orders WHERE amount > 0) AS d"
    out = simpler_forms(sql, COLUMNS)
    assert out[0] == "SELECT * FROM m.orders WHERE amount > 0"
    assert proven(sql, out[0])


def test_no_cte_shadows_a_dataset():
    sql = "SELECT x FROM (SELECT x, COUNT(*) AS n FROM m.t GROUP BY x) AS m JOIN m.a ON m.x = a.id"
    for candidate in simpler_forms(sql, COLUMNS):
        assert "WITH m AS" not in candidate


def test_tidy():
    assert tidy("SELECT `orders`.`id` AS id, orders.amount AS amt FROM `m.orders` AS orders WHERE orders.id = 1 AND (orders.id = 1)") == (
        "SELECT id, amount AS amt FROM m.orders WHERE id = 1"
    )
    # a qualifier naming another scope stays; so does one a select-list alias would capture
    assert tidy("SELECT a.x FROM m.a AS a WHERE EXISTS (SELECT 1 FROM m.b AS b WHERE b.y = a.x)") == (
        "SELECT x FROM m.a WHERE EXISTS(SELECT 1 FROM m.b WHERE y = a.x)"
    )
    assert tidy("SELECT t.a AS b, t.b AS a FROM m.t AS t GROUP BY t.a, t.b") == "SELECT a AS b, b AS a FROM m.t GROUP BY t.a, t.b"
    # joins keep their qualifiers; reserved words keep their quotes
    assert tidy("SELECT o.id, `select` FROM m.orders AS o JOIN m.p AS p ON o.id = p.id") == (
        "SELECT o.id, `select` FROM m.orders AS o JOIN m.p ON o.id = p.id"
    )
    assert tidy("SELECT x AS X FROM m.t") == "SELECT x AS X FROM m.t"


@pytest.mark.parametrize("sql", ["garbage (", "", "DELETE FROM m.t WHERE TRUE", "SELECT 1; SELECT 2"])
def test_garbage_gives_nothing(sql):
    assert simpler_forms(sql, COLUMNS) == []
    assert tidy(sql) == sql


def test_already_simple_query_gives_nothing():
    assert simpler_forms("SELECT id FROM m.orders WHERE amount > 0", COLUMNS) == []


def test_a_star_except_source_is_not_folded_into_its_table():
    # current sqlglot keeps EXCEPT under "except_"; reading only "except" folded the subquery away
    for sql in ("SELECT * FROM (SELECT * EXCEPT (b) FROM p.d.t) AS d", "WITH c AS (SELECT * EXCEPT (b) FROM p.d.t) SELECT * FROM c"):
        assert all("EXCEPT" in form for form in simpler_forms(sql)), simpler_forms(sql)
        assert "EXCEPT" in tidy(sql)
