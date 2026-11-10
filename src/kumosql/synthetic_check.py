"""Opt-in synthetic-data evidence for rewrite results.

``attach_synthetic_check`` runs the execution harness in
:mod:`kumosql.result_equivalence` on a changed result and records one
``synthetic_results`` check on its verification. It mirrors
``attach_planner_check``: ordinary rewriting never executes anything.

Agreement on synthetic data is evidence, not proof. A passing check therefore
never changes the evidence label and never makes a result trusted. A concrete
counterexample does the opposite: it records a failed check and demotes the
result to ``unproven``, because a query pair with a known disagreeing input
must not be accepted automatically. The check's detail and evidence describe
the counterexample by seed, counts and synthetic rows only; they never contain
query text or column names, and the evidence summary reads only the check's
kind and outcome.
"""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from typing import Iterable

from .evidence_summary import SYNTHETIC_KIND
from kumo_sql_prover.result_equivalence import (
    ResultEquivalence,
    ResultEquivalenceStatus,
    Schema,
    check_result_equivalence,
)
from .rewrite import (
    PipelineResult,
    RewriteResult,
    Verification,
    VerificationCheck,
    VerificationStatus,
)

DEFAULT_SEEDS = tuple(range(8))
_SAMPLE_ROWS = 3


def _evidence(
    seeds: tuple[int, ...],
    rows_per_table: int,
    null_rate: float,
    *,
    seeds_checked: tuple[int, ...] = (),
    failing_seed: int | None = None,
    extra: tuple[tuple[str, object], ...] = (),
) -> tuple[tuple[str, object], ...]:
    engine = "duckdb"
    if importlib.util.find_spec("duckdb") is not None:
        import duckdb

        engine = f"duckdb {duckdb.__version__}"
    return (
        ("scope", "end_to_end"),
        ("engine", engine),
        ("seeds", seeds),
        ("seeds_checked", seeds_checked),
        ("failing_seed", failing_seed),
        ("rows_per_table", rows_per_table),
        ("null_rate", null_rate),
    ) + extra


def _with_check(verification: Verification, check: VerificationCheck) -> Verification:
    """Replace any earlier synthetic check; downgrade only on a counterexample."""

    checks = tuple(c for c in verification.checks if c.kind != SYNTHETIC_KIND) + (check,)
    if check.outcome == "failed" and verification.status in (
        VerificationStatus.PROVEN,
        VerificationStatus.PLANNER_CHECKED,
        VerificationStatus.UNPROVEN,
    ):
        return Verification(
            VerificationStatus.UNPROVEN,
            "a synthetic-data counterexample shows the results differ",
            (check.detail,),
            checks,
        )
    # passed / inconclusive / not_run: the label and trust are left exactly as is.
    return replace(verification, checks=checks)


def _sample(rows) -> list[str]:
    return [repr(row) for row in rows[:_SAMPLE_ROWS]]


def _record(
    outcome: ResultEquivalence,
    seeds: tuple[int, ...],
    rows_per_table: int,
    null_rate: float,
) -> VerificationCheck:
    status = outcome.status
    common = dict(seeds_checked=outcome.seeds_checked, failing_seed=outcome.failing_seed)
    if status is ResultEquivalenceStatus.EQUIVALENT:
        return VerificationCheck(
            SYNTHETIC_KIND,
            "passed",
            f"Results matched on {len(outcome.seeds_checked)} synthetic datasets; "
            "agreement is evidence, not proof of equivalence.",
            _evidence(seeds, rows_per_table, null_rate, **common),
        )
    if status is ResultEquivalenceStatus.DIFFERENT:
        left = outcome.left_output
        right = outcome.right_output
        extra = (
            ("difference", outcome.reason),
            ("rows_only_in_original", len(outcome.only_left)),
            ("rows_only_in_rewritten", len(outcome.only_right)),
            ("sample_rows_only_in_original", _sample(outcome.only_left)),
            ("sample_rows_only_in_rewritten", _sample(outcome.only_right)),
            ("original_column_count", len(left.columns) if left else None),
            ("rewritten_column_count", len(right.columns) if right else None),
        )
        return VerificationCheck(
            SYNTHETIC_KIND,
            "failed",
            f"Counterexample on synthetic data (seed {outcome.failing_seed}): {outcome.reason}; "
            f"{len(outcome.only_left)} rows only in the original, "
            f"{len(outcome.only_right)} only in the rewritten output. "
            f"Reproduce with seed {outcome.failing_seed}.",
            _evidence(seeds, rows_per_table, null_rate, extra=extra, **common),
        )
    if status is ResultEquivalenceStatus.INCONCLUSIVE:
        # The harness reason names a side and seed but no query text.
        return VerificationCheck(
            SYNTHETIC_KIND,
            "inconclusive",
            outcome.reason,
            _evidence(seeds, rows_per_table, null_rate, **common),
        )
    # ERROR: the harness message can quote statements, so report only the side.
    side = "original" if outcome.reason.startswith("left") else "rewritten"
    return VerificationCheck(
        SYNTHETIC_KIND,
        "not_run",
        f"The {side} query could not be executed locally on synthetic data "
        f"(seed {outcome.failing_seed}); no comparison was made.",
        _evidence(seeds, rows_per_table, null_rate, **common),
    )


def attach_synthetic_check(
    result: RewriteResult | PipelineResult,
    schema: Schema,
    *,
    seeds: Iterable[int] = DEFAULT_SEEDS,
    rows_per_table: int = 25,
    null_rate: float = 0.15,
) -> RewriteResult | PipelineResult:
    """Opt in to synthetic-data evidence for a changed result.

    Runs the original and the rewritten SQL over seeded synthetic tables built
    from ``schema`` (table name as written in the SQL -> column -> BigQuery
    type) and records a ``synthetic_results`` check:

    * ``passed``: the outputs matched on every seed. Agreement is not proof: the
      label and ``trusted`` are unchanged.
    * ``failed``: a counterexample was found. The check describes it by seed and
      counts, and the result drops to ``unproven``.
    * ``inconclusive``: a query returned different results on two runs over
      identical data (for example ``RAND()``), so the comparison proves nothing.
    * ``not_run``: nothing was compared: duckdb is not installed, the result is
      unchanged or a rule failed, or a query could not be translated or executed.

    Every outcome carries the seeds and generation parameters so it can be
    reproduced. Ordinary rewriting stays offline; only this call executes SQL,
    and only inside an in-memory DuckDB.
    """

    seed_list = tuple(seeds)
    if not seed_list:
        raise ValueError("at least one seed is required")
    verification = result.verification
    rule_failed = verification.status is VerificationStatus.FAILED or (
        isinstance(result, RewriteResult) and not result.rule_success
    ) or (
        isinstance(result, PipelineResult)
        and any(not step.rule_success for step in result.steps)
    )

    def finish(outcome: str, detail: str, **kwargs) -> RewriteResult | PipelineResult:
        check = VerificationCheck(
            SYNTHETIC_KIND,
            outcome,
            detail,
            _evidence(seed_list, rows_per_table, null_rate, **kwargs),
        )
        return replace(result, verification=_with_check(verification, check))

    if rule_failed:
        return finish("not_run", "Synthetic check was skipped because the rewrite failed.")
    if result.input_sql == result.sql:
        return finish("not_run", "Synthetic check was skipped because the output did not change.")
    if importlib.util.find_spec("duckdb") is None:
        return finish(
            "not_run", "Synthetic check was not run: duckdb is not installed (install kumosql[execution])."
        )
    try:
        outcome = check_result_equivalence(
            result.input_sql,
            result.sql,
            schema,
            seeds=seed_list,
            rows_per_table=rows_per_table,
            null_rate=null_rate,
            targeted=True,
        )
    except ValueError:
        return finish("not_run", "Synthetic check was not run: the synthetic schema is not usable.")
    return replace(
        result,
        verification=_with_check(
            verification, _record(outcome, seed_list, rows_per_table, null_rate)
        ),
    )


__all__ = ["DEFAULT_SEEDS", "SYNTHETIC_KIND", "attach_synthetic_check"]
