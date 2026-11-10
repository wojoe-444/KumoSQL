"""A counterexample orders its strings as strings are ordered.

The provers order strings by an uninterpreted rank (``smt_equivalence._string_rank``) and state only the order of the
queries' own string literals. That is enough for a proof, but a model of a difference may rank every other string as
it likes, and the counterexample search evaluated both queries through that model. So ``WHERE s < ''`` against
``WHERE 1 = 0`` was reported NOT_EQUIVALENT with ``s = 'A'`` (no string is below the empty one: the pair is equal on
every STRING column), and ``WHERE s < 'b' AND s > 'a'`` was "shown" to return a row for ``s = ''``. Found 2026-11-10.

Every counterexample below is loaded into DuckDB tables of the declared types and both queries are run on it.
"""

import pytest

pytest.importorskip("z3")

from kumosql import prove_equivalent_smt
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import SmtStatus, _strings_between

SCHEMA = {"t": ["x", "s", "d"], "u": ["y", "s2"]}
TYPES = {"t": {"x": "INT64", "s": "STRING", "d": "DATE"}, "u": {"y": "INT64", "s2": "STRING"}}
DUCKDB_TYPES = {"INT64": "BIGINT", "STRING": "VARCHAR", "DATE": "DATE"}
NOTHING = "SELECT x FROM t WHERE 1 = 0"
# name -> (left, right, whether a counterexample is required: these pairs differ and the search finds a real one)
PAIRS = {
    "below-the-empty-string": ("SELECT x FROM t WHERE s < ''", NOTHING, False),
    "below-a-literal": ("SELECT x FROM t WHERE s < 'a'", NOTHING, True),
    "two-upper-bounds": ("SELECT x FROM t WHERE s < 'b'", "SELECT x FROM t WHERE s < 'a'", True),
    "between-two-literals": ("SELECT x FROM t WHERE s < 'b' AND s > 'a'", NOTHING, True),
    "between-a-literal-and-its-extension": ("SELECT x FROM t WHERE s > 'a' AND s < 'aa'", NOTHING, True),
    "between-a-literal-and-itself-with-a-space": ("SELECT x FROM t WHERE s < 'a ' AND s > 'a'", NOTHING, False),
    "strict-and-not-strict": ("SELECT x FROM t WHERE s >= 'b'", "SELECT x FROM t WHERE s > 'b'", True),
    "equal-or-below": ("SELECT x FROM t WHERE s = 'a'", "SELECT x FROM t WHERE s < 'b'", True),
    "range-and-list": ("SELECT x FROM t WHERE s BETWEEN 'a' AND 'c'", "SELECT x FROM t WHERE s IN ('a', 'b', 'c')", True),
    "date-ranges": ("SELECT x FROM t WHERE d >= '1994-01-01' AND d < '1995-01-01'", "SELECT x FROM t WHERE d >= '1994-01-01' AND d < '1994-06-01'", True),
    "two-columns-strict-and-not": ("SELECT x FROM t JOIN u ON s < s2", "SELECT x FROM t JOIN u ON s <= s2", True),
    "two-columns-below-and-above": ("SELECT x FROM t JOIN u ON s < s2", "SELECT x FROM t JOIN u ON s > s2", True),
}
PROVERS = {"smt": prove_equivalent_smt, "algebraic": prove_equivalent_algebraic}


def _run_both(counterexample, left, right):
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(":memory:")
    try:
        for table, columns in TYPES.items():
            connection.execute(f"CREATE TABLE {table} (" + ", ".join(f"{c} {DUCKDB_TYPES[t]}" for c, t in columns.items()) + ")")
            for row in counterexample.tables.get(table, []):
                connection.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))
        return [sorted(map(repr, connection.execute(sql).fetchall())) for sql in (left, right)]
    finally:
        connection.close()


@pytest.mark.parametrize("prover", sorted(PROVERS))
@pytest.mark.parametrize("name", sorted(PAIRS))
def test_a_counterexample_with_ordered_strings_separates_the_pair_in_duckdb(name, prover):
    left, right, required = PAIRS[name]
    result = PROVERS[prover](left, right, schema=SCHEMA, types=TYPES)
    assert result.status is not SmtStatus.PROVEN_EQUIVALENT
    if result.counterexample is None:
        assert not required, result.reason
        return  # unknown is allowed; rows that no engine returns are not
    assert result.status is SmtStatus.NOT_EQUIVALENT
    left_rows, right_rows = _run_both(result.counterexample, left, right)
    assert left_rows != right_rows, result.counterexample.tables


@pytest.mark.parametrize("prover", sorted(PROVERS))
def test_nothing_is_below_the_empty_string(prover):
    """The pair is equal on every STRING column, so NOT_EQUIVALENT is a wrong answer, not a weak one."""

    result = PROVERS[prover]("SELECT x FROM t WHERE s < ''", NOTHING, schema=SCHEMA, types=TYPES)
    assert result.status is SmtStatus.NOT_PROVEN and result.counterexample is None


def test_columns_without_declared_types_get_ordered_strings_too():
    result = prove_equivalent_smt("SELECT x FROM t WHERE s < 'b' AND s > 'a'", NOTHING, schema=SCHEMA)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    (row,) = result.counterexample.tables["t"]
    assert isinstance(row["s"], str) and "a" < row["s"] < "b"


def test_the_strings_offered_between_literals_are_between_them():
    literals = ["", "1994-06-01", "1995-01-01", "a", "a ", "aa", "b"]
    offered = _strings_between(literals)
    assert "1994-06-02" in offered  # a date between two dates: the column may be a DATE
    bounds = [None, *literals, None]
    for text in offered:
        assert text not in literals
        assert any((low is None or low < text) and (high is None or text < high) for low, high in zip(bounds, bounds[1:]))
    assert not [text for text in offered if text < ""]  # nothing is below the empty string
    assert _strings_between([]) == ["a", "b"]
