"""The search for a readable counterexample stays inside the solver's time limit.

``_Prover._nice_model`` asked for a model with whole-number values by ``push``-ing extra facts onto the solver that had
just answered. ``push`` moves Z3 to its incremental core, which neither ``timeout`` nor ``rlimit`` covers, and with
some ninety string constants among the assertions it never returned: ``prove_equivalent_smt(..., timeout_ms=1000)``
on ``b IN (<90 strings>)`` against no filter ran until it was killed (86 literals took a tenth of a second). Found by
fuzzing on 2026-11-10 with z3 4.16 and 5.4.
"""

import subprocess
import sys
from pathlib import Path

import pytest

z3 = pytest.importorskip("z3")

from kumosql import prove_equivalent_smt
from kumosql.smt_equivalence import SmtStatus

SRC = Path(__file__).resolve().parent.parent / "src"
SCHEMA = {"t": ["a", "b"]}
TYPES = {"t": {"a": "INT64", "b": "STRING"}}


def _filtered(literals: int) -> str:
    return "SELECT a FROM t WHERE b IN (%s)" % ", ".join(f"'s{i}'" for i in range(literals))


@pytest.mark.parametrize("literals", [90, 200])
def test_many_string_constants_do_not_outlive_the_time_limit(literals):
    """In its own interpreter: the old code cannot be interrupted from Python once Z3 stops returning."""

    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from kumosql import prove_equivalent_smt\n"
        "result = prove_equivalent_smt(%r, 'SELECT a FROM t', schema=%r, types=%r, timeout_ms=1000)\n"
        "print(result.status.value, result.counterexample is not None)\n"
    ) % (str(SRC), _filtered(literals), SCHEMA, TYPES)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.split() == ["not_equivalent", "True"]


def test_the_preference_search_never_pushes_onto_a_checked_solver(monkeypatch):
    def push(self):
        raise AssertionError("push on a checked solver leaves the time limit behind")

    monkeypatch.setattr(z3.Solver, "push", push)
    result = prove_equivalent_smt(_filtered(3), "SELECT a FROM t", schema=SCHEMA, types=TYPES)
    assert result.status is SmtStatus.NOT_EQUIVALENT and result.counterexample is not None


def test_a_counterexample_still_prefers_whole_numbers():
    """What the preference is for: a column the queries only compare with numbers gets a number, and a whole one."""

    result = prove_equivalent_smt("SELECT a FROM t WHERE a > 1.5", "SELECT a FROM t", schema=SCHEMA)
    assert result.status is SmtStatus.NOT_EQUIVALENT
    values = [row["a"] for row in result.counterexample.tables["t"] if row["a"] is not None]
    assert values and all(isinstance(value, int) or float(value).is_integer() for value in values), values
