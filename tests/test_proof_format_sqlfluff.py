"""The layout check against sqlfluff's own layout fixes (development rules only; held-out rules are not read).

Moved out of ``test_proof_format.py`` because it loads the sqlfluff fixture benchmark script, which marks a test
file as an eval file (``tests/conftest.py``).
"""

from __future__ import annotations

import pytest
import importlib.util
from pathlib import Path
import sys

import sqlglot

from kumosql.proof_format import FORMAT_ASSUMPTIONS, FORMAT_FAMILY, check_format_transition
from kumosql.proof_steps import RewriteStep

pytestmark = pytest.mark.eval

TREE = sqlglot.parse_one("SELECT 1", read="bigquery")


def check(before: str, after: str):
    step = RewriteStep("format_sql", FORMAT_FAMILY, 0, before, after, FORMAT_ASSUMPTIONS)
    return check_format_transition(step, TREE, TREE)


def _bench():
    spec = importlib.util.spec_from_file_location("sqlfluff_fixtures_bench_proof_format", Path(__file__).resolve().parent.parent / "tools" / "sqlfluff_fixtures_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sqlfluff_fixtures_bench_proof_format"] = module
    spec.loader.exec_module(module)
    return module


def test_sqlfluffs_own_layout_fixes_are_never_accepted_when_they_change_more_than_layout():
    bench = _bench()
    accepted = refused = 0
    for case in bench.split(bench.layout_cases(bench.load_cases()), "dev"):
        if case.dialect not in ("ansi", "bigquery") or "{{" in case.fail or "{%" in case.fail:
            continue
        result = check(case.fail, case.fix)
        try:
            changed = bench.layout_differences(case)
        except Exception:  # noqa: BLE001 - a fixture sqlglot cannot read has no reference answer
            continue
        if result.accepted:
            accepted += 1
            assert not changed, (case.id, changed)
            assert not bench.renames_identifiers(case), case.id
        else:
            refused += 1
    assert accepted >= 100, (accepted, refused)
