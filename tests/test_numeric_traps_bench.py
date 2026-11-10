"""Numeric traps: BigQuery number and error semantics in the SMT prover (floors, honesty, witnesses)."""

from pathlib import Path
import sys

import pytest

pytest.importorskip("z3")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import numeric_traps_bench as bench  # noqa: E402

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def results():
    return {c.id: (c, bench.decide(c)) for c in bench.load_cases("all")}


def test_the_split_holds_out_a_quarter_and_the_cases_are_unique():
    cases = bench.load_cases("all")
    assert len({c.id for c in cases}) == len(cases)
    assert len(bench.load_cases("held-out")) * 4 in range(len(cases) - 3, len(cases) + 1)
    assert {c.id for c in bench.load_cases("dev")}.isdisjoint(c.id for c in bench.load_cases("held-out"))
    assert {c.label for c in cases} == set(bench.LABELS)


def test_nothing_is_wrong(results):
    assert [i for i, (_, r) in results.items() if r["wrong"]] == []


def test_a_trap_pair_is_never_proven_without_disclosing_the_violated_assumption(results):
    proven = [i for i, (c, r) in results.items() if c.label == "not_equivalent" and r["outcome"] == "proven"]
    assert proven == []


def test_an_assumed_proof_lists_the_assumption_its_case_violates(results):
    for case, result in results.values():
        if result["outcome"] == "assumed":
            named = case.violates + case.discharged
            assert named and any(a.startswith(v) for a in result["assumptions"] for v in named)


def test_sound_pairs_are_still_proved(results):
    equivalent = [r for c, r in results.values() if c.label == "equivalent"]
    assert sum(r["outcome"] == "proven" for r in equivalent) >= 45
    assert not any(r["outcome"] == "refuted" for r in equivalent)


def test_a_float_sum_is_proved_clean_only_over_an_identical_plan_or_an_exact_type(results):
    for case_id in ("float-sum-same-text", "float-sum-grouped-same-text", "float-avg-same-text", "int-sum-with-float-filter-elsewhere"):
        assert results[case_id][1]["outcome"] == "proven", case_id
    for case_id, (case, result) in results.items():
        if case_id.startswith("float-") and "-sum-" in case_id and case.label == "equivalent" and case.violates:
            # another plan over the same rows: any proof lists the order assumption
            assert result["outcome"] in ("assumed", "unknown"), case_id
    for case_id in ("float-sum-pre-aggregated", "float-sum-split", "float-sum-extra-filter"):
        assert results[case_id][1]["outcome"] in ("unknown", "refuted"), case_id


def test_the_literals_of_the_october_audit_are_exact(results):
    for case_id in ("float-literal-underflow", "float-literal-digits", "float-literal-sum-false", "float-literal-sum-true", "int64-literal-sum", "int64-min-literal"):
        assert results[case_id][1]["outcome"] == "proven", case_id
    assert results["float-literal-sum-column"][1]["outcome"] == "refuted"


def test_every_error_case_is_classified_by_its_verdict(results):
    errors = [(i, r) for i, (c, r) in results.items() if c.label in bench.EXPECTED_VERDICT]
    assert len(errors) >= 21
    # the two window pairs are not proven equal (a filter moved across a window is not modelled): unknown, not wrong
    assert sorted(i for i, r in errors if not r["classified"]) == ["window-sum-filter-below", "window-sum-where-to-outer"]


def test_a_rewrite_that_moves_an_operation_ahead_of_its_guard_is_reported(results):
    for case_id in ("overflow-case-guard", "div-if-guard", "int-div-if-guard", "safe-cast-to-cast"):
        assert results[case_id][1]["errors"] == "introduces", case_id
    for case_id in ("slash-to-safe-divide", "slash-to-nullif", "cast-to-safe-cast", "div-if-guard-removed"):
        assert results[case_id][1]["errors"] == "refines", case_id


def test_numeric_scale_and_rounding_pairs_are_decided(results):
    proved = ("numeric-literal-zeros", "numeric-literal-rounding", "numeric-times-int-keeps-scale", "numeric-cast-same-type", "numeric-round-idempotent",
              "numeric-round-nine-noop", "numeric-add-product-twice", "numeric-round-negative-idempotent", "numeric-division-nine-digits", "numeric-bignumeric-roundtrip")
    for case_id in proved:
        assert results[case_id][1]["outcome"] == "proven", case_id
    for case_id in ("numeric-double-round", "numeric-product-casts-wider", "numeric-literal-below-half", "numeric-division-roundtrip", "numeric-mul-distributes"):
        assert results[case_id][1]["outcome"] in ("refuted", "unknown"), case_id
    for case_id in ("numeric-overflow-guard", "numeric-div-guard-dropped"):
        assert results[case_id][1]["errors"] == "introduces", case_id


def test_a_sum_over_a_group_is_compared_group_by_group(results):
    for case_id in ("group-sum-having-to-where", "group-sum-distinct-having-to-where", "group-sum-key-and-aggregate-having", "group-sum-join-having-to-where"):
        assert results[case_id][1]["errors"] == "refines", case_id
    for case_id in ("group-sum-where-to-having", "group-sum-two-keys-where-to-having", "distinct-sum-where-to-having"):
        assert results[case_id][1]["errors"] == "introduces", case_id
        assert results[case_id][1]["outcome"] == "unknown", case_id  # the proof is withheld
    for case_id in ("group-sum-filter-respelled", "sum-commuted-argument", "group-sum-key-order", "global-sum-same-exposure", "group-sum-join-commuted"):
        assert results[case_id][1]["errors"] == "same", case_id


def test_the_held_out_quarter_scores_like_the_rest():
    held_out = bench.load_cases("held-out")
    decided = [bench.decide(c) for c in held_out]
    assert not any(r["wrong"] for r in decided)


@pytest.mark.parametrize("case", [c for c in bench.load_cases("all") if c.executable], ids=lambda c: c.id)
def test_the_label_witness_holds_on_duckdb(case):
    pytest.importorskip("duckdb")
    assert bench.witness_problems(case) == []


def test_the_witness_check_can_fail():
    pytest.importorskip("duckdb")
    case = next(c for c in bench.load_cases("all") if c.executable and c.witness["left"] != "error")
    broken = type(case)(**{**case.__dict__, "witness": {**case.witness, "left": [[999]]}})
    assert bench.witness_problems(broken)
