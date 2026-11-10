"""The heavy re-check search behind tools/proof_recheck.py (tools/recheck/engine.py)."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from recheck import engine  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402


def _emp_dept() -> dict[str, Table]:
    dept = Table("dept", [Column("deptno", "int", not_null=True), Column("name", "text")], keys=[("deptno",)])
    emp = Table(
        "emp",
        [Column("empno", "int", not_null=True), Column("deptno", "int", not_null=True), Column("sal", "int")],
        keys=[("empno",)],
        foreign_keys=[(("deptno",), "dept", ("deptno",))],
    )
    return {"dept": dept, "emp": emp}


def test_finds_a_duplicate_sensitive_difference():
    tables = {"t": Table("t", [Column("x", "int")])}
    case = Case("test", "dup", "SELECT DISTINCT COUNT(*) FROM (SELECT DISTINCT x FROM t) d", "SELECT DISTINCT COUNT(*) FROM t", tables)
    record = engine.recheck(case, budget=500)
    assert record["verdict"] == "differs"
    left, right = record["witness"]["left"], record["witness"]["right"]
    assert left != right


def test_an_equivalent_pair_survives():
    case = Case("test", "same", "SELECT name FROM dept WHERE deptno > 1", "SELECT name FROM dept WHERE NOT deptno <= 1", _emp_dept())
    record = engine.recheck(case, budget=400)
    assert record["verdict"] == "survived" and record["dbs"] == 400


def test_databases_respect_keys_not_null_and_foreign_keys():
    case = Case("test", "legal", "SELECT e.sal FROM emp e JOIN dept d ON e.deptno = d.deptno", "SELECT sal FROM emp", _emp_dept())
    generator = engine.Generator(case, random.Random(3))
    databases = [generator.database(generator.random_profile()) for _ in range(200)]
    databases += [d for _, d in generator.edge_databases()]
    databases += list(generator.exhaustive(300, engine._exhaustive_values(generator, 0)))
    databases = [d for d in databases if d is not None]
    assert len(databases) > 150
    for data in databases:
        assert engine.legal(case, data)
        keys = {row[0] for row in data.get("dept", [])}
        assert all(row[1] in keys for row in data.get("emp", []))
        assert len({row[0] for row in data.get("emp", [])}) == len(data.get("emp", []))
    # the FK makes the join keep every employee: the pair is equivalent under the declarations
    assert engine.recheck(case, budget=300)["verdict"] == "survived"


def test_without_the_foreign_key_the_join_can_drop_rows():
    tables = _emp_dept()
    tables["emp"].foreign_keys = []
    case = Case("test", "no-fk", "SELECT e.sal FROM emp e JOIN dept d ON e.deptno = d.deptno", "SELECT sal FROM emp", tables)
    assert engine.recheck(case, budget=300)["verdict"] == "differs"


@pytest.mark.parametrize(
    "mode, left, right, verdict",
    [
        ("contained", "SELECT x FROM t WHERE y > 1", "SELECT x FROM t", "survived"),
        ("contained", "SELECT x FROM t", "SELECT x FROM t WHERE y > 1", "differs"),
        ("set", "SELECT DISTINCT x FROM t", "SELECT x FROM t", "survived"),
        ("list", "SELECT x FROM t ORDER BY x", "SELECT x FROM t ORDER BY x DESC", "differs"),
    ],
)
def test_comparison_modes(mode, left, right, verdict):
    tables = {"t": Table("t", [Column("x", "int"), Column("y", "int")])}
    assert engine.recheck(Case("test", mode, left, right, tables, mode=mode), budget=300)["verdict"] == verdict


def test_row_order_dependence_is_not_a_difference():
    tables = {"t": Table("t", [Column("x", "int"), Column("y", "int")])}
    case = Case("test", "limit", "SELECT y FROM t LIMIT 1", "SELECT y FROM t ORDER BY x LIMIT 1", tables)
    record = engine.recheck(case, budget=300)
    assert record["verdict"] == "survived"  # the first row of an unordered table is the engine's choice
    assert record.get("notes", {}).get("nondeterministic", 0) > 0


def test_values_fit_the_declared_type():
    column = Column("x", "int", sql_type="INTEGER")
    assert engine.fits(column, 2147483647) and not engine.fits(column, 2147483648)
    assert not engine.fits(Column("d", "decimal", sql_type="DECIMAL(4,2)"), 123.5)


def test_integer_beside_a_double_is_float_noise():
    # DuckDB's AVG is DOUBLE: over one row, 2^53 + 1 comes back as 2^53 where an exact decimal (MySQL) keeps it
    tables = {"t": Table("t", [Column("k", "int", not_null=True), Column("x", "int", sql_type="BIGINT")], keys=[("k",)])}
    case = Case("test", "avg", "SELECT k, x FROM t", "SELECT k, AVG(x) FROM t GROUP BY k", tables)
    record = engine.recheck(case, budget=1500)
    assert record["verdict"] == "survived"
    assert record.get("notes", {}).get("float-noise", 0) > 0


def test_integer_difference_beside_no_float_still_differs():
    tables = {"t": Table("t", [Column("k", "int", not_null=True), Column("x", "int", sql_type="BIGINT")], keys=[("k",)])}
    case = Case("test", "plus", "SELECT k, x FROM t", "SELECT k, x + 1 FROM t", tables)
    assert engine.recheck(case, budget=300)["verdict"] == "differs"


def test_a_difference_the_optimizer_hides_is_reported_as_masked(monkeypatch):
    """Optimized results agree; with the optimizer off the right query returns other rows on every database."""

    tables = {"t": Table("t", [Column("x", "int")])}
    case = Case("test", "masked", "SELECT COUNT(*) FROM t", "SELECT COUNT(*) FROM t", tables)
    monkeypatch.setattr(engine.Runner, "unoptimized", lambda self: (self.run(self.case.left), [(-1,)]))
    record = engine.recheck(case, budget=50)
    assert record["verdict"] == "masked"
    assert record["witness"]["right"] == [[-1]] or record["witness"]["right"] == [(-1,)]


def test_an_unoptimized_run_that_agrees_changes_nothing(monkeypatch):
    calls = []
    original = engine.Runner.unoptimized

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(engine.Runner, "unoptimized", counting)
    case = Case("test", "same", "SELECT name FROM dept WHERE deptno > 1", "SELECT name FROM dept WHERE NOT deptno <= 1", _emp_dept())
    record = engine.recheck(case, budget=200)
    assert record["verdict"] == "survived"
    assert 200 // engine.UNOPTIMIZED_EVERY <= len(calls) < 200  # every edge database and a fifth of the rest


def test_masked_ignores_float_noise_and_row_order():
    import random as _random

    tables = {"t": Table("t", [Column("x", "int")])}
    case = Case("test", "noise", "SELECT 1", "SELECT 1", tables)
    runner = engine.Runner(case)
    try:
        runner.load({"t": [(1,), (2,)]})
        runner.unoptimized = lambda: ([(0.1 + 0.2,)], [(0.3,)])
        assert engine.masked(runner, {"t": [(1,), (2,)]}, _random.Random(0)) is None
        answers = iter([([(1,)], [(2,)]), ([(2,)], [(1,)])])
        runner.unoptimized = lambda: next(answers)  # a different answer once the rows are stored in another order
        assert engine.masked(runner, {"t": [(1,), (2,)]}, _random.Random(0)) is None
    finally:
        runner.close()
