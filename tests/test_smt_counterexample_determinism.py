"""Candidate models must not depend on earlier imports or shared-context term ids (#472)."""

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path[:0] = ['src', 'tools']
    import z3
    import constraint_rewrite_bench as bench
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic
    from kumosql.constraint_dependence import constraints_with, guarantees_of
    from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

    fixture = bench.load_cases('cases.json')
    case = next(c for c in fixture['cases'] if c['id'] == 'h-lookup-join-elimination')
    schema = fixture['schemas'][case['schema']]
    columns, declared = bench.schema_parts(schema)
    facts = [g for g in guarantees_of(declared)
             if not (g.kind == 'not_null' and g.table == 'users' and g.columns == ('plan',))]
    constraints = constraints_with(facts)
    db = bench.connection(schema)
    expected = None
    for attempt in range(8):
        # Keep unrelated terms alive so the default context's ids differ on each call.
        unrelated = [z3.Int(f'unrelated_{attempt}_{i}') + i for i in range(attempt * 13)]
        for prove in (prove_equivalent_smt, prove_equivalent_algebraic):
            result = prove(case['original'], case['rewritten'], schema=columns, constraints=constraints)
            assert result.status is SmtStatus.NOT_EQUIVALENT, (attempt, prove, result)
            assert result.counterexample is not None
            assert bench.counterexample_ok(schema, db, case, result, facts)
            assert any(row['plan'] is None for row in result.counterexample.tables['users'])
            expected = expected or result.counterexample
            assert result.counterexample == expected
    # Restoring NOT NULL still proves the rewrite: candidate search changes no proof facts.
    assert prove_equivalent_algebraic(case['original'], case['rewritten'],
                                     schema=columns, constraints=declared).proven
    db.close()
    """
)


@pytest.mark.parametrize(
    "imports",
    ["", "import hashlib, dataclasses\n", "import runpy\nrunpy.run_path('tools/constraint_rewrite_bench.py')\n"],
    ids=["direct", "unrelated-imports", "runpy"],
)
def test_repeated_lookup_join_counterexample_is_stable(imports):
    done = subprocess.run(
        [sys.executable, "-c", imports + SCRIPT], cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_isolated_candidate_search_keeps_the_solver_limits(monkeypatch):
    """A fresh Z3 solver has no timeout or work cap of its own, so the isolated one must be given both (#472)."""
    import z3
    from kumosql.smt_equivalence import _Prover
    from kumosql.solver_lock import WORK_PER_MS

    prover = _Prover(250)
    solver = z3.Solver()
    solver.add(z3.Int("x") > 0)
    assert solver.check() == z3.sat

    limits = []
    real_set = z3.Solver.set

    def set_(self, *args, **kwargs):
        if args[:1] in (("timeout",), ("rlimit",)):
            limits.append((self.ctx is not solver.ctx, args[0], args[1]))
        return real_set(self, *args, **kwargs)

    monkeypatch.setattr(z3.Solver, "set", set_)
    prover._counterexample(solver.assertions(), solver.model(), [])
    assert (True, "timeout", 250) in limits
    assert (True, "rlimit", 250 * WORK_PER_MS) in limits
