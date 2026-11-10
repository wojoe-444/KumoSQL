"""Differential soundness fuzzer (tools/soundness_fuzz.py): its oracle, its generators, and a seeded smoke run."""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("z3")
duckdb = pytest.importorskip("duckdb")

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
import soundness_fuzz as fuzz  # noqa: E402

pytestmark = pytest.mark.eval

SMOKE_SEED = 3
SMOKE_COUNT = 120


def basic_case(left="SELECT x FROM t", right="SELECT x FROM t", label=True):
    return {
        "id": "test", "family": "test", "left": left, "right": right,
        "schema": {"t": [["id", "INT64"], ["x", "INT64"], ["s", "STRING"]]},
        "tables": {"t": [[0, 1, "a"], [1, None, None], [2, 1, "b"]]},
        "constraints": {"t": {"keys": [["id"]]}},
        "mutation": {"name": "identity" if label else "deliberate_change", "known_equivalent": label},
    }


def rows(case, sql):
    with duckdb.connect(":memory:") as connection:
        fuzz.load_fixture(connection, case)
        return connection.execute(fuzz.convert_sql(sql, case)).fetchall()


def test_generation_is_seeded_and_every_database_is_legal():
    first = list(fuzz.generated_cases(15, 80))
    assert first == list(fuzz.generated_cases(15, 80))
    assert first != list(fuzz.generated_cases(16, 80))
    assert {len(case["schema"]) for case in first} == {2, 3}
    for case in first:
        assert not fuzz.fixture_errors(case), case["id"]
        assert [v["name"] for v in case["fixture_variants"]] == ["random_1", "random_2", "null_heavy", "duplicates", "empty"]
        for variant in case["fixture_variants"]:
            assert not fuzz.fixture_errors(dict(case, tables=variant["tables"])), (case["id"], variant["name"])
        assert all(not data for data in case["fixture_variants"][-1]["tables"].values())


def test_sol_families_cover_every_outer_join_probe():
    cases = [fuzz.sol_case(fuzz.random.Random(1), i) for i in range(3 * len(fuzz.SOL_FAMILIES))]
    assert {c["family"] for c in cases} == {f"sol:{f}" for f in fuzz.SOL_FAMILIES}
    for condition, family in (("TRUE", "sol:join_true"), ("FALSE", "sol:join_false")):
        queries = [c["left"] for c in cases if c["family"] == family]
        for side in ("LEFT", "RIGHT", "FULL"):
            assert any(f"{side} JOIN u ON {condition}" in sql for sql in queries)


def test_sol_sound_mutations_agree_and_deliberate_changes_differ():
    rng = fuzz.random.Random(15)
    changed = {}
    for index in range(2 * len(fuzz.SOL_FAMILIES)):
        case = fuzz.sol_case(rng, index)
        case["fixture_variants"] = fuzz.additional_fixtures(case, rng)
        observed = fuzz.execute(case, fuzz.default_options())
        assert observed["execution"] == "ok", (case["family"], observed)
        if case["mutation"]["known_equivalent"]:
            assert observed["equal_bags"], case["family"]
        else:
            changed[case["mutation"]["name"]] = observed["equal_bags"]
    assert set(changed) >= {"remove_last_aggregate", "remove_group_key", "rename_shadowed_scope", "remove_set_limit", "remove_float_coercion"}
    assert not any(changed.values())


def test_every_template_runs_and_pairs_labelled_equivalent_agree():
    """A pair a template calls equivalent must agree on every database; otherwise the generator is wrong."""

    rng = fuzz.random.Random(5)
    for template in fuzz.TEMPLATES:
        ran = 0
        for _ in range(10):
            case = fuzz.template_case(rng, template)
            case["fixture_variants"] = fuzz.additional_fixtures(case, rng)
            observed = fuzz.execute(case, fuzz.default_options())
            assert observed["execution"] in ("ok", "skipped"), (template.__name__, case["left"], case["right"], observed)
            if observed["execution"] == "ok":
                ran += 1
                if case["mutation"]["known_equivalent"]:
                    assert observed["equal_bags"], (case["left"], case["right"], observed)
        assert ran, template.__name__


def test_exact_bags_keep_duplicates_null_and_large_integers():
    assert fuzz.bag([(1,), (None,)]) == fuzz.bag([(None,), (1.0,)])
    assert fuzz.bag([(1,), (1,)]) != fuzz.bag([(1,)])
    assert fuzz.bag([(None,)]) != fuzz.bag([(False,)])
    assert fuzz.bag([(True,)]) != fuzz.bag([(1,)])
    assert fuzz.bag([(9007199254740993,)]) != fuzz.bag([(float(9007199254740993),)])
    assert fuzz.bag([(9007199254740992,)]) == fuzz.bag([(float(9007199254740992),)])
    with pytest.raises(fuzz.UnsupportedConversion):
        fuzz.bag([(float("nan"),)])


@pytest.mark.parametrize("change,reason", [
    (lambda c: c["tables"]["t"][0].__setitem__(0, None), "NOT NULL/key"),
    (lambda c: c["tables"]["t"][1].__setitem__(0, 0), "duplicate key"),
    (lambda c: c["tables"]["t"][0].__setitem__(1, 2**63), "outside INT64"),
    (lambda c: c["tables"]["t"][0].__setitem__(2, "é"), "outside ASCII"),
    (lambda c: c["tables"]["t"][0].append(1), "arity"),
])
def test_invalid_databases_are_refused(change, reason):
    case = basic_case()
    change(case)
    assert any(reason in problem for problem in fuzz.fixture_errors(case))
    assert fuzz.worker_evaluate(case, fuzz.default_options())["execution"] == "invalid_fixture"


def test_foreign_keys_are_match_simple_against_actual_parent_values():
    case = basic_case()
    case["schema"]["p"] = [["id", "INT64"]]
    case["tables"]["p"] = [[1]]
    case["constraints"]["p"] = {"keys": [["id"]]}
    case["constraints"]["t"]["foreign_keys"] = [[["x"], "p", ["id"]]]
    assert not fuzz.fixture_errors(case)  # a NULL child is allowed
    case["tables"]["p"] = []
    assert any("foreign key violation" in e for e in fuzz.fixture_errors(case))


@pytest.mark.parametrize("sql", [
    "SELECT x FROM t LIMIT 1",
    "SELECT x AS id,s FROM t ORDER BY id LIMIT 1",
    "WITH t AS (SELECT 1 AS id UNION ALL SELECT 1 AS id) SELECT id FROM t ORDER BY id LIMIT 1",
    "WITH t AS (SELECT 1 AS id UNION ALL SELECT 1 AS id) SELECT * FROM (SELECT ROW_NUMBER() OVER (ORDER BY id) AS rn FROM t) d",
    "SELECT ROW_NUMBER() OVER () FROM t ORDER BY id",
    "SELECT CAST(s AS FLOAT64)=CAST(s AS FLOAT64) FROM t",
    "WITH q AS (SELECT s AS x FROM t) SELECT CAST(x AS FLOAT64)=CAST(x AS FLOAT64) FROM q",
    "SELECT CAST(x AS FLOAT64) FROM (SELECT s AS x FROM t) d",
    "SELECT CAST('NaN' AS FLOAT64)=CAST('NaN' AS FLOAT64)",
    "SELECT SAFE_DIVIDE(1e308,1e-308)",
    "SELECT DATE_TRUNC(DATE '2024-01-03',WEEK)",
    "SELECT REGEXP_EXTRACT('abc','z')",
    "SELECT STRUCT(1 AS a)=STRUCT(1 AS b)",
    "SELECT NUMERIC '0.123456789'",
    "SELECT RAND()",
    "SELECT 9223372036854775809",
    "SELECT x / 2 FROM t",
    "SELECT x FROM t ORDER BY id LIMIT 1 OFFSET 1",
    "SELECT SUM(x) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM t",
    "DELETE FROM t",
])
def test_unsupported_or_nondeterministic_queries_are_refused(sql):
    with pytest.raises(fuzz.UnsupportedConversion):
        fuzz.convert_sql(sql, basic_case())


def test_guarded_window_limit_and_integer_float_cast_are_allowed():
    case = basic_case()
    for sql in ("SELECT id,x FROM t ORDER BY id LIMIT 1", "SELECT ROW_NUMBER() OVER (ORDER BY id) FROM t", "SELECT CAST(a.x AS FLOAT64) FROM t a",
                "SELECT COUNT(*) OVER (PARTITION BY s) FROM t", "SELECT x FROM t WHERE s LIKE 'a%' AND x IS DISTINCT FROM 1"):
        assert fuzz.convert_sql(sql, case)


def test_operator_operands_keep_sqlglots_reading_in_duckdb():
    # sqlglot writes Is(Not(Is(x, NULL)), NULL) as NOT x IS NULL IS NULL, which DuckDB reads as NOT ((x IS NULL) IS NULL)
    case = basic_case()
    assert rows(case, "SELECT (x IS NOT NULL) IS NULL AS b FROM t") == [(False,), (False,), (False,)]
    assert rows(case, "SELECT NOT (x = 1) IS NULL AS b FROM t") == [(True,), (False,), (True,)]


def test_unparenthesized_comparison_chains_are_refused():
    # sqlglot reads t.x = 1 IS NULL as t.x = (1 IS NULL); the engines read (t.x = 1) IS NULL
    case = basic_case()
    for sql in ("SELECT x = 1 IS NULL AS b FROM t", "SELECT x <> 1 IS NOT NULL AS b FROM t", "SELECT x = NOT x IS NULL AS b FROM t"):
        with pytest.raises(fuzz.UnsupportedConversion, match="comparison chain"):
            fuzz.convert_sql(sql, case)
    assert rows(case, "SELECT (x = 1) IS NULL AS b FROM t") == [(False,), (True,), (False,)]


def test_large_integer_arithmetic_is_refused_but_float_coercion_is_measured():
    case = basic_case("SELECT x*1e0 AS v FROM t", "SELECT x AS v FROM t", None)
    case["tables"]["t"] = [[0, 9007199254740993, None]]
    assert fuzz.bag(rows(case, case["left"])) != fuzz.bag(rows(case, case["right"]))
    with pytest.raises(fuzz.UnsupportedConversion, match="overflow"):
        fuzz.convert_sql("SELECT SUM(x) FROM t", case)


def test_prover_gets_the_schema_types_constraints_and_timeout():
    case = basic_case()
    case["constraints"]["t"]["foreign_keys"] = []
    kwargs = fuzz.prover_kwargs(case, dict(fuzz.default_options(), solver_timeout_ms=321))
    assert kwargs["timeout_ms"] == 321
    assert kwargs["search_counterexample"] is False
    assert kwargs["types"] == {"t": {"id": "INT64", "x": "INT64", "s": "STRING"}}
    assert kwargs["constraints"]["t"].keys == (("id",),)


def test_classification():
    assert fuzz.classify("proven_equivalent", False, None) == "false_proof"
    assert fuzz.classify("proven_equivalent", True, None) is None
    assert fuzz.classify("not_proven", False, True) == "label_error"
    assert fuzz.classify("not_equivalent", True, True) == "suspected_false_refutation"
    assert fuzz.classify("not_equivalent", False, None) is None


def test_errors_and_row_caps_are_never_evidence():
    for left in ("SELECT missing_column FROM t", "SELECT (SELECT id FROM t) AS x"):
        observed = fuzz.worker_evaluate(basic_case(left), fuzz.default_options())
        assert observed["execution"] == "execution_error"
        assert "discrepancy" not in observed
    observed = fuzz.worker_evaluate(basic_case(), dict(fuzz.default_options(), max_result_rows=1))
    assert observed == {"execution": "skipped", "reason": "result row limit exceeded"}


def test_a_difference_counts_only_when_the_unoptimized_run_agrees(monkeypatch):
    import kumosql.duckdb_load as duckdb_load

    case = basic_case("SELECT x FROM t", "SELECT DISTINCT x FROM t", None)
    assert fuzz.execute(case, fuzz.default_options())["equal_bags"] is False
    monkeypatch.setattr(duckdb_load, "run_unoptimized", lambda db, *queries: [[(1,)], [(1,)]])
    observed = fuzz.execute(case, fuzz.default_options())
    assert observed["equal_bags"] is True
    assert observed["optimizer_only_differences"] == 1


def test_worker_timeout_restarts_the_child():
    worker = fuzz.Worker()
    try:
        assert worker.evaluate(basic_case(), fuzz.default_options(), 0.001)["execution"] == "timeout"
        assert worker.evaluate(basic_case(), fuzz.default_options(), 0)["execution"] == "timeout"
        observed = worker.evaluate(basic_case(), fuzz.default_options(), 60)
        assert observed["execution"] == "ok"
        assert observed["prover"]["status"] == "proven_equivalent"
    finally:
        worker.close()


def test_reduction_keeps_the_false_proof_and_shrinks_sql_and_rows():
    case = basic_case("SELECT t.x, t.s FROM t WHERE t.x IS NOT NULL AND t.s <> 'z'", "SELECT DISTINCT t.x, t.s FROM t WHERE t.x IS NOT NULL", None)
    case["tables"]["t"] = [[0, 1, "a"], [1, 1, "a"], [2, 2, "b"], [3, None, None]]
    checks = []

    def evaluate(candidate, options):
        # stand-in prover: "proves" any pair whose right side keeps DISTINCT
        checks.append(candidate)
        observed = fuzz.execute(candidate, options)
        if observed["execution"] == "ok" and not options.get("oracle_only"):
            proved = "DISTINCT" in candidate["right"]
            observed["prover"] = {"status": "proven_equivalent" if proved else "not_proven"}
            observed["discrepancy"] = fuzz.classify(observed["prover"]["status"], observed["equal_bags"], None)
        return observed

    observed = evaluate(case, fuzz.default_options())
    assert observed["discrepancy"] == "false_proof"
    reduced = fuzz.minimize_false_proof(case, observed, evaluate, dict(fuzz.default_options(), minimize_checks=40))
    assert reduced["reduced_sql_chars"] < reduced["original_sql_chars"]
    assert "DISTINCT" in reduced["right"]
    assert sum(len(r) for r in reduced["tables"].values()) == 2  # the two duplicate rows
    final = evaluate(dict(case, left=reduced["left"], right=reduced["right"], tables=reduced["tables"]), fuzz.default_options())
    assert final["discrepancy"] == "false_proof"


def test_known_false_proofs_name_their_cause_and_owner():
    for entry in fuzz.load_known():
        assert entry["cause"] and entry["owner"]
        for side in ("left", "right"):
            fuzz.sqlglot.parse_one(entry[side], read="bigquery")


def test_published_false_proofs_stay_unproved():
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    for case in fuzz.historical_cases():
        observed = fuzz.execute(case, fuzz.default_options())
        assert observed["execution"] == "ok" and not observed["equal_bags"], case["id"]
        result = prove_equivalent_algebraic(case["left"], case["right"], dialect="bigquery")
        assert not result.proven, case["id"]


def test_seeded_smoke_run_finds_no_new_false_proof():
    """A short fixed-seed run; long runs are ``python tools/soundness_fuzz.py --count N`` (docs/evals/fuzzing.md)."""

    report = fuzz.run(SMOKE_SEED, SMOKE_COUNT, jobs=2, minimize=False, run_timeout_seconds=600)
    summary = report["summary"]
    new = [f"{r['case']['left']}  vs  {r['case']['right']}" for r in report["findings"] if r["observation"].get("discrepancy") == "false_proof" and not r.get("known")]
    assert not new, "new false proofs (fix them, or record them in tests/fixtures/soundness_fuzz/known_false_proofs.json):\n" + "\n".join(new)
    assert "label_error" not in summary["discrepancies"], json.dumps(report["findings"], indent=1)[:4000]
    assert summary["completed"] == summary["planned"]
    assert summary["execution"].get("ok", 0) >= 0.9 * summary["planned"]
    assert not summary["execution"].get("worker_error")
    assert summary["prover_status"].get("proven_equivalent", 0) >= 0.3 * summary["planned"]
