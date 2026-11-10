"""Wiring canary for the prover dependency: about 14 real cases per benchmark family, in seconds.

The prover's own benchmarks (SQLSolver, QED, R-Bot, Cosette, SPES, VeriEQL, Singh and Bedathur, SQL-IQ, bounded
verification, ...) run in `wojoe-444/Kumo_SQL_Prover`, on a schedule. What stays here is the smallest check that
KumoSQL still reaches the pinned prover the way its own code does: the same pairs the SQLSolver suites prove, fed
through the entry points KumoSQL imports, plus the pairs those suites must never prove. A floor that drops is the
prover repository's to report; a failure here means the wiring (the pin, the import paths, the sqlglot parser
extension) broke.
"""

import json
from pathlib import Path

import pytest

from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.smt_equivalence import TableConstraints

CANARY = json.loads((Path(__file__).resolve().parent / "fixtures" / "prover_canary.json").read_text(encoding="utf-8"))


def _prove(case, tables):
    schema = {name: [c[0] for c in table["columns"]] for name, table in tables.items()}
    constraints = {
        name: TableConstraints(
            not_null=frozenset(c[0] for c in table["columns"] if c[2]),
            keys=tuple(tuple(k) for k in ([table["primary_key"]] if table["primary_key"] else []) + table["unique"]),
        )
        for name, table in tables.items()
    }
    types = {name: {c[0]: c[1] for c in table["columns"]} for name, table in tables.items()}
    return prove_equivalent_algebraic(
        case["left"], case["right"], schema=schema, constraints=constraints, types=types, compare_names=False,
        dialect="mysql", exact_arithmetic=True, group_by_constants=case["constants"],
    )


CASES = [(family, case) for family, data in CANARY["families"].items() for case in data["proven"]]


@pytest.mark.parametrize("family,case", CASES, ids=[f"{family}-{case['index']}" for family, case in CASES])
def test_a_pair_the_suite_proves_is_still_proven(family, case):
    result = _prove(case, CANARY["families"][family]["tables"])
    assert result.proven, f"{family} pair {case['index']}: {result.status} {getattr(result, 'reason', '')}"


@pytest.mark.parametrize("family,case", [(f, c) for f, d in CANARY["families"].items() for c in d.get("must_not_prove", [])])
def test_a_pair_that_must_stay_unproven_is_not_proven(family, case):
    result = _prove(case, CANARY["families"][family]["tables"])
    assert not result.proven, f"{family} pair {case['index']} was proved: {case['reason']}"
