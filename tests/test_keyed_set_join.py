"""Keyed DISTINCT lifting: exact bags, declared facts and deliberate near misses."""
from collections import Counter
import random

import duckdb
import pytest
import sqlglot

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.keyed_set_join import lift_keyed_set_join
from kumosql.smt_equivalence import TableConstraints

pytestmark = pytest.mark.eval

LEFT = "SELECT d.x,d.v,b.y FROM (SELECT DISTINCT f.x,f.v FROM f) d JOIN b ON d.x=b.k"
RIGHT = "SELECT DISTINCT f.x,f.v,b.y FROM f JOIN b ON f.x=b.k"
SCHEMA = {"f": ["x", "v"], "b": ["k", "y"]}
TYPES = {"f": {"x": "INT64", "v": "INT64"}, "b": {"k": "INT64", "y": "INT64"}}
KEYS = {"b": [("k",)]}
NN = {"b": frozenset({"k"})}
CONSTRAINTS = {"b": TableConstraints(keys=(("k",),), not_null=frozenset({"k"}))}


def rewrite(sql=LEFT, keys=KEYS, nn=NN, types=TYPES):
    return lift_keyed_set_join(sqlglot.parse_one(sql), keys, nn, types)


def prove(left, right, constraints=CONSTRAINTS, types=TYPES, schema=SCHEMA):
    return prove_equivalent_algebraic(left, right, schema=schema, types=types,
                                      constraints=constraints, compare_names=False, timeout_ms=1500)


def bags(left, right, f_rows, b_rows, float_x=False):
    with duckdb.connect(":memory:") as db:
        db.execute("SET threads=1")
        db.execute(f"CREATE TABLE f(x {'DOUBLE' if float_x else 'BIGINT'},v BIGINT)")
        db.execute("CREATE TABLE b(k BIGINT,y BIGINT)")
        if f_rows:
            db.executemany("INSERT INTO f VALUES (?,?)", f_rows)
        if b_rows:
            db.executemany("INSERT INTO b VALUES (?,?)", b_rows)
        db.execute("PRAGMA disable_optimizer")
        return [Counter(db.execute(sql).fetchall()) for sql in (left, right)]


@pytest.mark.parametrize("sql", [LEFT,
    "SELECT b.k,d.v,b.y FROM b JOIN (SELECT DISTINCT f.x,f.v FROM f) d ON b.k=d.x",
    "SELECT d.x,d.v,b.y FROM (SELECT DISTINCT f.x,f.v FROM f) d CROSS JOIN b WHERE d.x=b.k AND b.y>0",
])
def test_direct_integer_keyed_shapes_are_proved(sql):
    assert rewrite(sql) is not None
    flat = RIGHT if "b.y>0" not in sql else RIGHT + " WHERE b.y>0"
    assert prove(sql, flat).proven


def test_random_nullable_duplicate_and_empty_bags():
    reduced = rewrite()
    assert reduced is not None
    rng = random.Random(1903)
    fixtures = [([], []), ([(None, None)], []), ([], [(1, None)]),
                ([(1, None), (1, None), (1, 3), (2, 4)], [(1, 5), (2, None)])]
    for _ in range(100):
        f_rows = [(rng.choice([None, -1, 0, 1, 2]), rng.choice([None, -1, 0, 1, 2]))
                  for _ in range(rng.randrange(8))]
        b_rows = [(k, rng.choice([None, -1, 0, 1, 2])) for k in (-1, 0, 1, 2) if rng.choice([False, True])]
        fixtures.append((f_rows, b_rows))
    for f_rows, b_rows in fixtures:
        original, lifted = bags(LEFT, reduced.sql(dialect="duckdb"), f_rows, b_rows)
        assert original == lifted, (f_rows, b_rows)


def test_missing_key_has_a_duplicate_bag_witness():
    assert rewrite(keys={}) is None
    original, distinct = bags(LEFT, RIGHT, [(1, 7)], [(1, 5), (1, 5)])
    assert original != distinct
    assert not prove(LEFT, RIGHT, constraints={}).proven


def test_projection_must_determine_every_distinct_output():
    left = LEFT.replace("d.x,d.v,b.y", "d.x,b.y")
    right = RIGHT.replace("f.x,f.v,b.y", "f.x,b.y")
    assert rewrite(left) is None
    assert bags(left, right, [(1, 7), (1, 8)], [(1, 5)])[0] != bags(left, right, [(1, 7), (1, 8)], [(1, 5)])[1]
    assert not prove(left, right).proven


def test_float_join_coercion_cannot_borrow_integer_key_uniqueness():
    types = {"f": {"x": "FLOAT64", "v": "INT64"}, "b": TYPES["b"]}
    assert rewrite(types=types) is None
    original, distinct = bags(LEFT, RIGHT, [(float(2**53), 7)], [(2**53, 5), (2**53 + 1, 5)], float_x=True)
    assert original != distinct


@pytest.mark.parametrize("sql", [
    LEFT.replace(" JOIN b ", " LEFT JOIN b "),
    LEFT.replace(" JOIN b ", " RIGHT JOIN b "),
    LEFT.replace(" JOIN b ", " FULL JOIN b "),
    LEFT.replace("SELECT DISTINCT f.x,f.v", "SELECT DISTINCT CAST(f.x AS FLOAT64) AS x,f.v"),
    LEFT.replace("SELECT d.x,d.v,b.y", "SELECT d.x*0,d.v,b.y"),
    LEFT + " LIMIT 1",
    LEFT + " ORDER BY d.x",
    LEFT.replace("FROM f) d", "FROM f LIMIT 1) d"),
    LEFT.replace("JOIN b ON", "JOIN b AS b(y,k) ON"),
    LEFT.replace("FROM f) d", "FROM f AS f(v,x)) d"),
    "WITH b AS (SELECT k,y FROM other_b) " + LEFT,
])
def test_unsupported_boundaries_are_declined(sql):
    assert rewrite(sql) is None


def test_or_join_does_not_establish_an_equality_fact():
    sql = LEFT.replace("ON d.x=b.k", "ON d.x=b.k OR b.y=1")
    assert rewrite(sql) is None


def test_composite_key_requires_all_key_columns():
    sql = "SELECT d.x,d.v FROM (SELECT DISTINCT f.x,f.v FROM f) d JOIN b ON d.x=b.k"
    types = {"f": TYPES["f"], "b": {"k": "INT64", "y": "INT64"}}
    assert rewrite(sql, keys={"b": [("k", "y")]}, nn={"b": frozenset({"k", "y"})}, types=types) is None
    good = sql + " AND d.v=b.y"
    assert rewrite(good, keys={"b": [("k", "y")]}, nn={"b": frozenset({"k", "y"})}, types=types) is not None


def test_nullable_key_facts_are_not_silently_strengthened():
    assert rewrite(nn={}) is None


def test_window_nulls_first_and_last_remain_different():
    left = "SELECT f.x,ROW_NUMBER() OVER (ORDER BY f.x NULLS FIRST) AS n FROM f"
    right = left.replace("NULLS FIRST", "NULLS LAST")
    assert rewrite(left) is None and rewrite(right) is None
    original, changed = bags(left, right, [(None, 0), (1, 0)], [])
    assert original != changed
    assert not prove(left, right).proven


def test_cosette_three_conditional_cases_are_publicly_proved():
    from pathlib import Path
    import sys
    tools = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools))
    import cosette_bench as cb
    import sqlsolver_bench as sb
    wanted = {"ex1sigmod92", "ex2sigmod92", "ex2sigmod92simpl"}
    seen = set()
    for case in cb.load("cosette"):
        if case["name"] not in wanted:
            continue
        assert not case.get("held_out") and not case.get("heldout")
        tables = cb._tables(case["ddl"])
        left, right = cb.repaired(case["sql_a"], case["sql_b"], tables)
        assert sb.prove_result(left, right, tables).proven
        seen.add(case["name"])
    assert seen == wanted


# Reviewed near misses: each pair differs on a DuckDB witness (optimizer off), the rule declines
# every SELECT in the left query, and the prover does not prove the pair.
NEAR_DDL = {
    "f": ("x BIGINT, v BIGINT", {"x": "BIGINT", "v": "BIGINT"}, ()),
    "b": ("k BIGINT NOT NULL, y BIGINT", {"k": "BIGINT", "y": "BIGINT"}, (("k",),)),
    "c": ("k1 BIGINT NOT NULL, k2 BIGINT NOT NULL, y BIGINT",
          {"k1": "BIGINT", "k2": "BIGINT", "y": "BIGINT"}, (("k1", "k2"),)),
    "g": ("z BIGINT", {"z": "BIGINT"}, ()),
    "fs": ("x VARCHAR, v BIGINT", {"x": "VARCHAR", "v": "BIGINT"}, ()),
    "fc": ("x VARCHAR COLLATE NOCASE, v BIGINT", {"x": "VARCHAR", "v": "BIGINT"}, ()),
    "bc": ("k VARCHAR NOT NULL, y BIGINT", {"k": "VARCHAR", "y": "BIGINT"}, (("k",),)),
    "o": ("a BIGINT", {"a": "BIGINT"}, ()),
}
NEAR_D = "(SELECT DISTINCT f.x, f.v FROM f) d"
NEAR_MISSES = {
    # b.y is not b's key: one derived row meets two b rows
    "non_key_pin": (f"SELECT d.x, d.v FROM {NEAR_D} JOIN b ON d.x = b.y",
                    "SELECT DISTINCT f.x, f.v FROM f JOIN b ON f.x = b.y",
                    {"f": [(1, 7)], "b": [(1, 1), (2, 1)]}),
    # c.k2 is tied only to c's own column, so the composite key is not pinned by d
    "composite_key_half_pinned": (f"SELECT d.x, d.v FROM {NEAR_D} JOIN c ON d.x = c.k1 AND c.k2 = c.y",
                                  "SELECT DISTINCT f.x, f.v FROM f JOIN c ON f.x = c.k1 AND c.k2 = c.y",
                                  {"f": [(1, 7)], "c": [(1, 5, 5), (1, 6, 6)]}),
    # only b's columns are read: d.v is hidden, so two derived rows give one output twice
    "projection_from_key_side_only": (f"SELECT b.k, b.y FROM {NEAR_D} JOIN b ON d.x = b.k",
                                      "SELECT DISTINCT b.k, b.y FROM f JOIN b ON f.x = b.k",
                                      {"f": [(1, 7), (1, 8)], "b": [(1, 5)]}),
    # a second, unkeyed join multiplies rows
    "second_unkeyed_join": (f"SELECT d.x, d.v, b.y FROM {NEAR_D} JOIN b ON d.x = b.k JOIN g ON g.z = b.k",
                            "SELECT DISTINCT f.x, f.v, b.y FROM f JOIN b ON f.x = b.k JOIN g ON g.z = b.k",
                            {"f": [(1, 7)], "b": [(1, 5)], "g": [(1,), (1,)]}),
    # a string cast is not injective: '1' and '01' both meet key 1
    "string_cast_pin": ("SELECT b.k, d.v FROM (SELECT DISTINCT fs.x, fs.v FROM fs) d JOIN b ON CAST(d.x AS BIGINT) = b.k",
                        "SELECT DISTINCT b.k, fs.v FROM fs JOIN b ON CAST(fs.x AS BIGINT) = b.k",
                        {"fs": [("1", 7), ("01", 7)], "b": [(1, 5)]}),
    # a NOCASE comparison meets two rows of a case-sensitive key
    "collated_pin": ("SELECT d.x, d.v FROM (SELECT DISTINCT fc.x, fc.v FROM fc) d JOIN bc ON d.x = bc.k",
                     "SELECT DISTINCT fc.x, fc.v FROM fc JOIN bc ON fc.x = bc.k",
                     {"fc": [("a", 7)], "bc": [("a", 1), ("A", 2)]}),
    # correlated: the outer reference fixes b.y, not b's key
    "correlated_non_key_pin": (
        f"SELECT o.a, (SELECT COUNT(*) FROM (SELECT d.x, d.v FROM {NEAR_D} JOIN b ON d.x = b.y WHERE b.y = o.a) s) AS n FROM o",
        "SELECT o.a, (SELECT COUNT(*) FROM (SELECT DISTINCT f.x, f.v FROM f JOIN b ON f.x = b.y WHERE b.y = o.a) s) AS n FROM o",
        {"o": [(1,)], "f": [(1, 7)], "b": [(1, 1), (2, 1)]}),
    # DISTINCT ON keeps one row per x, not one per (x, v)
    "inner_distinct_on": ("SELECT d.x, d.v FROM (SELECT DISTINCT ON (f.x) f.x, f.v FROM f ORDER BY f.x, f.v) d JOIN b ON d.x = b.k",
                          "SELECT DISTINCT f.x, f.v FROM f JOIN b ON f.x = b.k",
                          {"f": [(1, 7), (1, 8)], "b": [(1, 5)]}),
    # an outer GROUP BY on a hidden column keeps one row per group
    "outer_group_hidden_key": (f"SELECT d.x FROM {NEAR_D} JOIN b ON d.x = b.k GROUP BY d.x, d.v",
                               "SELECT DISTINCT f.x FROM f JOIN b ON f.x = b.k GROUP BY f.x, f.v",
                               {"f": [(1, 7), (1, 8)], "b": [(1, 5)]}),
}


@pytest.mark.parametrize("name", sorted(NEAR_MISSES))
def test_reviewed_near_misses_differ_and_are_not_proved(name):
    from kumosql.duckdb_load import run_unoptimized

    left, right, rows = NEAR_MISSES[name]
    with duckdb.connect(":memory:") as db:
        db.execute("SET threads=1")
        for table, (columns, _, _) in NEAR_DDL.items():
            db.execute(f"CREATE TABLE {table}({columns})")
        for table, values in rows.items():
            db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(values[0]))})", values)
        original, flattened = run_unoptimized(db, left, right)
    assert Counter(original) != Counter(flattened)
    types = {t: ty for t, (_, ty, _) in NEAR_DDL.items()}
    keys = {t: list(k) for t, (_, _, k) in NEAR_DDL.items() if k}
    nn = {t: frozenset(c for key in k for c in key) for t, (_, _, k) in NEAR_DDL.items() if k}
    for select in sqlglot.parse_one(left, read="duckdb").find_all(sqlglot.exp.Select):
        assert lift_keyed_set_join(select, keys, nn, types) is None
    constraints = {t: TableConstraints(keys=k, not_null=nn[t]) for t, (_, _, k) in NEAR_DDL.items() if k}
    schema = {t: list(ty) for t, ty in types.items()}
    assert not prove(left, right, constraints=constraints, types=types, schema=schema).proven


@pytest.mark.parametrize("sql", [
    LEFT.replace("FROM f) d", "FROM f) d TABLESAMPLE RESERVOIR (1 ROWS)"),
    LEFT + " USING SAMPLE 1 ROWS",
    LEFT.replace("FROM f) d", "FROM f USING SAMPLE 1 ROWS) d"),
])
def test_sampled_shapes_are_declined(sql):
    # a sample of the deduplicated rows is not a sample of the duplicated ones
    assert lift_keyed_set_join(sqlglot.parse_one(sql, read="duckdb"), KEYS, NN, TYPES) is None
