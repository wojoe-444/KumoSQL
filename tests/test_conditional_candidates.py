"""``tools/conditional_candidates.py``: the conditional verdict with a schema's constraints removed.

One VeriEQL Literature pair (the pinned file is downloaded on first use; the test skips when it cannot be fetched) and
one SQLSolver Calcite pair, plus the comparison of a proof's conditions with what the schema declares.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_tools = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_tools))
_spec = importlib.util.spec_from_file_location("conditional_candidates", _tools / "conditional_candidates.py")
candidates = importlib.util.module_from_spec(_spec)
sys.modules["conditional_candidates"] = candidates
_spec.loader.exec_module(candidates)


def _condition(kind, table, columns, **extra):
    return {"kind": kind, "table": table, "columns": columns, **extra}


def test_a_declared_key_covers_its_supersets_and_its_not_null():
    declared = ["PRIMARY KEY DEPT(DEPTNO)", "NOT NULL DEPT(NAME)", "FOREIGN KEY EMP(DEPTNO) REFERENCES DEPT(DEPTNO)"]
    conditions = [
        _condition("unique", "dept", ["deptno", "name"]),
        _condition("not_null", "dept", ["deptno"]),
        _condition("not_null", "dept", ["name"]),
        _condition("foreign_key", "emp", ["deptno"], parent="dept", parent_columns=["deptno"]),
    ]
    assert candidates._beyond_schema(conditions, declared) == []


def test_a_condition_the_schema_does_not_declare_is_reported():
    declared = ["PRIMARY KEY R2(A)"]
    conditions = [_condition("unique", "r2", ["a"]), _condition("not_null", "r2", ["b"]), _condition("unique", "r2", ["b"])]
    assert candidates._beyond_schema(conditions, declared) == ["NOT NULL r2(b)", "UNIQUE r2(b)"]


def test_a_literature_pair_names_the_not_null_the_schema_never_stated():
    veri = pytest.importorskip("verieql_bench")
    try:
        cases = veri.load_cases("literature")
    except OSError as error:
        pytest.skip(f"benchmark data not available: {error}")
    case = next(c for c in cases if "Y.B = Z.B" in c["pair"][0])  # a self join of R2, whose primary key is A
    with_schema = candidates.bench.decide_verieql(case, declared=True)
    bare = candidates.bench.decide_verieql(case, declared=False)
    assert bare.kind == "conditional" and bare.minimal
    found = {(c["kind"], tuple(c["columns"])) for c in bare.conditions}
    assert found == {("unique", ("a",)), ("not_null", ("b",))}
    assert with_schema.kind != "wrong"


def test_a_calcite_pair_is_proved_with_no_constraint_and_removing_them_changes_nothing_else():
    sol = pytest.importorskip("sqlsolver_bench")
    left = "SELECT EMP.EMPNO FROM EMP AS EMP WHERE EMP.SAL > 1 AND 1 = 1"
    right = "SELECT EMP.EMPNO FROM EMP AS EMP WHERE EMP.SAL > 1"
    outcome = candidates.decide_sqlsolver((0, left, right))
    assert outcome.kind == "equivalent"
    assert sol.load_schema(sol.FIXTURES / "calcite.schema.sql")["emp"].primary_key  # the schema itself still declares its key


def test_a_calcite_except_all_pair_needs_a_unique_key_the_schema_declares_a_subset_of():
    sol = pytest.importorskip("sqlsolver_bench")
    pairs = sol.load_pairs(sol.FIXTURES / "calcite_pairs.txt")
    index = 29  # the EXCEPT ALL / GROUP BY pair over DEPT
    outcome = candidates.decide_sqlsolver((index, *pairs[index]))
    assert outcome.kind == "conditional" and outcome.minimal
    assert {(c["kind"], tuple(c["columns"])) for c in outcome.conditions} == {("unique", ("deptno", "name")), ("not_null", ("deptno",)), ("not_null", ("name",))}
    declared = ["PRIMARY KEY DEPT(DEPTNO)", "NOT NULL DEPT(DEPTNO)", "NOT NULL DEPT(NAME)"]
    assert candidates._beyond_schema(outcome.conditions, declared) == []
