"""A counterexample is a database the tables can hold: every value fits the declared type of its column.

``_Prover._nice_model`` asks the solver for a readable model once a difference is found. Its preference "every value
is a number" ignored declared column types, and the value sort has none of its own, so a counterexample could hold
the number 1 in a STRING column or the string ``'A'`` in an INT64 key. The pair really differs in those cases, but the
database shown cannot be loaded into the tables it describes. Found on 2026-11-10 while comparing 600 generated
pairs before and after another fix to the same function (``tests/test_counterexample_time_limit.py``).
"""

import pytest

pytest.importorskip("z3")

from kumosql import prove_equivalent_smt
from kumosql.smt_equivalence import SmtStatus, TableConstraints

T_SCHEMA = {"t": ["id", "x", "y", "s"]}
T_TYPES = {"t": {"id": "INT64", "x": "INT64", "y": "INT64", "s": "STRING"}}
T_KEYS = {"t": TableConstraints(not_null=frozenset({"id"}), keys=(("id",),))}
TYPED_PAIRS = {
    "join-against-its-negation": ("SELECT a.id, a.x, b.s FROM t AS a JOIN t AS b ON a.x = b.x", "SELECT a.id, a.x, b.s FROM t AS a JOIN t AS b ON NOT (a.x = b.x)"),
    "extra-grouping-column": ("SELECT t.x, MAX(t.s) AS n FROM t GROUP BY t.x", "SELECT t.x, MAX(t.s) AS n FROM t GROUP BY t.x, t.s"),
    "sum-with-extra-grouping-column": ("SELECT x, SUM(y) AS n FROM t GROUP BY x, s", "SELECT x, SUM(y) AS n FROM t GROUP BY x"),
    "string-filter": ("SELECT id FROM t WHERE s = 'a'", "SELECT id FROM t WHERE s <> 'b'"),
    "number-filter": ("SELECT s FROM t WHERE x > 1", "SELECT s FROM t WHERE x >= 1"),
}
DUCKDB_TYPES = {"INT64": "BIGINT", "STRING": "VARCHAR"}


@pytest.mark.parametrize("name", sorted(TYPED_PAIRS))
def test_a_counterexample_fits_the_declared_column_types_and_separates_the_pair(name):
    """Loaded into DuckDB tables of the declared types, as is: an ill-typed value would not even insert."""

    duckdb = pytest.importorskip("duckdb")
    left, right = TYPED_PAIRS[name]
    result = prove_equivalent_smt(left, right, schema=T_SCHEMA, types=T_TYPES, constraints=T_KEYS)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
    if result.counterexample is None:
        return  # unknown is allowed; a database that cannot exist is not
    connection = duckdb.connect(":memory:")
    try:
        for table, columns in T_TYPES.items():
            connection.execute(f"CREATE TABLE {table} (" + ", ".join(f"{c} {DUCKDB_TYPES[t]}" for c, t in columns.items()) + ")")
            for row in result.counterexample.tables.get(table, []):
                for column, value in row.items():
                    if value is not None:
                        assert isinstance(value, str) == (columns[column] == "STRING"), (table, column, value)
                        assert columns[column] != "INT64" or (isinstance(value, int) and not isinstance(value, bool)), (table, column, value)
                connection.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
        rows = [sorted(map(repr, connection.execute(sql).fetchall())) for sql in (left, right)]
    finally:
        connection.close()
    assert rows[0] != rows[1]


@pytest.mark.parametrize("name", ["join-against-its-negation", "number-filter", "string-filter"])
def test_typed_pairs_are_still_refuted(name):
    """Fitting the types must not cost the refutation: the first of these is the pair that showed ``s = 1``."""

    left, right = TYPED_PAIRS[name]
    result = prove_equivalent_smt(left, right, schema=T_SCHEMA, types=T_TYPES, constraints=T_KEYS)
    assert result.status is SmtStatus.NOT_EQUIVALENT and result.counterexample is not None


def test_without_declared_types_the_preference_for_numbers_stands():
    result = prove_equivalent_smt("SELECT id FROM t WHERE x > 1", "SELECT id FROM t", schema=T_SCHEMA)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    assert all(not isinstance(value, str) for row in result.counterexample.tables["t"] for value in row.values())
