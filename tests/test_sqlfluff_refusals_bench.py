"""sqlfluff's refusal cases: KumoSQL's structural rules must decline or prove (see tools/sqlfluff_fixtures_bench.py).

The 212 cases are the structure-rule (ST) and CV12 fixtures with no ``fix_str``: queries sqlfluff leaves alone, often
because a rewrite would change the meaning (a correlated derived table, a data-modifying CTE, Jinja). They are stored
in benchmarks/sqlfluff_rule_cases/refusal-cases.json (MIT, licence next to it), so no download is needed. ``FLOORS``
only goes up; every case that is refused, caught or unsupported is a regression case that must never become wrong.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqlfluff_fixtures_bench.py"
_spec = importlib.util.spec_from_file_location("sqlfluff_fixtures_bench_refusals", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sqlfluff_fixtures_bench_refusals"] = bench
_spec.loader.exec_module(bench)

# measured 2026-10-03 over all 212 cases: 190 declined or proved (139 declined, 51 proven)
FLOORS = {"correct": 188, "declined": 137}

# A change KumoSQL's verification refused although it alters behaviour: a rule bug that never reaches a user as a
# trusted output. PL/SQL is read in sqlglot's recovery mode, which loses the ``INTO`` targets and the ``FROM cte1``
# that uses the CTE, so ``remove_unused_ctes`` drops the CTE and prints a garbled statement (marked unproven).
KNOWN_CAUGHT = {"ST03/test_pass_oracle_select_into_record_fields"}

# Lifted forms that differ from their input because the lift moved a subquery out of its scope (DuckDB: binder
# error), which the structural prover used to prove. Its lift now leaves such a subquery in place and the
# independent check of the lift (``proof_lift``) refuses a body that reads the query around it.
PROVER_LIFT_OUT_OF_SCOPE = [
    # a correlated derived table: `a` is out of scope in the CTE
    (
        "SELECT * FROM a, (SELECT * FROM b WHERE b.x = a.x) AS s",
        "WITH l AS (SELECT * FROM b WHERE b.x = a.x) SELECT * FROM a CROSS JOIN l AS s",
    ),
    # the derived table reads `c` from the WITH around it; at the top level `c` is the base table (c = {5}: 1 vs 5)
    (
        "SELECT * FROM (WITH c AS (SELECT 1 AS x) SELECT * FROM (SELECT x FROM c) AS d) AS e",
        "WITH l1 AS (SELECT x FROM c), l2 AS (WITH c AS (SELECT 1 AS x) SELECT * FROM l1 AS d) SELECT * FROM l2 AS e",
    ),
]


def test_data_is_the_pinned_version():
    cases = bench.load_refusals()
    assert len(cases) == 212 and len({c.id for c in cases}) == 212
    assert {c.id.split("/")[0][:2] for c in cases} == {"ST", "CV"}
    assert sum(c.kind == "pass" for c in cases) == 151
    ids = {c.id for c in cases}
    assert "ST05/correlated_subquery_in_later_set_expression_branch" in ids  # sqlfluff PR 8169, in the pinned commit
    assert set(bench.REFUSAL_HAZARDS) <= ids
    held = [c for c in cases if c.held_out]
    assert len(held) == 39


def test_independent_checks_see_scope_side_effects_and_templates():
    parse = lambda sql: bench.parse_all(sql, "bigquery")  # noqa: E731
    lateral = parse("SELECT * FROM a, (SELECT * FROM b WHERE b.x = a.x) AS s")
    lifted = parse("WITH l AS (SELECT * FROM b WHERE b.x = a.x) SELECT * FROM a, l AS s")
    assert not bench.unbound_references(lateral)  # a derived table may name a relation before it
    assert bench.unbound_references(lifted) == {"a": 1}  # a CTE body sees nothing of the query using it
    branch = parse("SELECT * FROM a JOIN (SELECT x FROM b UNION ALL SELECT c.x FROM c WHERE c.x = a.x) AS s ON TRUE")
    assert not bench.unbound_references(branch)
    dml = bench.parse_all("WITH d AS (DELETE FROM t) SELECT 1", "postgres")
    assert bench.data_modifications(dml) == 1
    assert bench.rewrite_harms("SELECT {{ x }} FROM t", "SELECT STRUCT(STRUCT(x)) FROM t", "ansi") == ["template"]


def test_refusal_cases():
    cases = bench.load_refusals()
    result = bench.run_refusals(cases, workers=2)
    verdicts = result["verdicts"]
    wrong = {k: [r for r in rs if r["outcome"] == "wrong"] for k, (o, rs) in verdicts.items() if o == "wrong"}
    assert wrong == {}, wrong
    outcomes = {o: sum(v[0] == o for v in verdicts.values()) for o in bench.REFUSAL_OUTCOMES}
    assert outcomes["declined"] + outcomes["proven"] >= FLOORS["correct"], outcomes
    assert outcomes["declined"] >= FLOORS["declined"], outcomes
    caught = {k for k, (o, _) in verdicts.items() if o == "caught"}
    assert caught <= KNOWN_CAUGHT, caught - KNOWN_CAUGHT
    # the hazards sqlfluff names: a correlated derived table (in any set-operation branch) and a data-modifying
    # CTE are left alone; templated SQL is left exactly as written
    for case_id in ("ST05/issue_3572_correlated_subquery_1", "ST05/issue_3572_correlated_subquery_2",
                    "ST05/issue_3572_correlated_subquery_3", "ST05/correlated_subquery_in_later_set_expression_branch",
                    "ST03/test_fail_postgres_dml_ctes_not_flagged"):
        assert verdicts[case_id][0] == "declined", verdicts[case_id]
    from kumosql.engine import has_template_tags

    templated = [c.id for c in cases if has_template_tags(c.sql)]
    assert len(templated) == 7
    assert all(verdicts[i][0] == "unsupported" for i in templated), {i: verdicts[i] for i in templated}


@pytest.mark.parametrize("before,after", PROVER_LIFT_OUT_OF_SCOPE)
def test_prover_does_not_prove_a_lift_out_of_scope(before, after):
    from kumosql.rewrite import verify_rewrite

    assert verify_rewrite(before, after).status.value not in ("proven", "unchanged")
