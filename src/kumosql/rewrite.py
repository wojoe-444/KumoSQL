"""Apply registered rewrite rules and check each output for equivalence.

Every changed output is compared with its input by the conservative prover in
``kumosql.equivalence``. Each result has one evidence label and separate
records for the checks that support it. Only ``unchanged`` and ``proven`` are
trusted for automatic acceptance; a fatal rule failure is labeled ``failed``.

Statements are checked pairwise. For ``CREATE ... AS`` and ``INSERT ...
SELECT`` the statement text around the query must be unchanged and the
queries must be proven equivalent. Other statements must render identically.
Dataform SQLX is checked by requiring config/js/operation blocks to be
identical and treating each ``${...}`` interpolation as an opaque fragment
identified by its text. Only ``ref()``, ``resolve()`` and ``self()`` are
faithful as a name; a changed statement holding any other expression outside
a string literal is unproven until the SQLX is compiled (``sqlx_fragments``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping
from urllib.error import URLError

from sqlglot import exp

from kumo_sql_prover.ast_utils import parse_statements, top_level_query
from .scripts import block_statements, script_skeleton
from .dryrun import Transport, check_rewrite
from .engine import RewriteRule, RuleDiagnostic, RuleOutput, available_rules, get_rule
from kumo_sql_prover.equivalence import prove_equivalent
from kumo_sql_prover.input_validity import invalid_input_reason
from kumo_sql_prover.type_names import invalid_type_name
from kumo_sql_prover.layout_equivalence import created_function_calls, layout_only_change, touching_literal_chunks
from . import prover_context
from kumo_sql_prover.smt_equivalence import SmtStatus, prove_equivalent_smt
from kumo_sql_prover.sqlx import looks_like_sqlx, mask_sqlx_by_content, split_sqlx_sections
from kumo_sql_prover.sqlx_fragments import dynamic_sentinels, holds_dynamic_fragment
from kumo_sql_prover.proof_qualify import QUALIFY_FAMILY
from kumo_sql_prover.proof_format import FORMAT_FAMILY
from kumo_sql_prover.proof_registry import FAMILIES, RULE_FAMILIES
from kumo_sql_prover.proof_steps import RewriteStep, StepCheck, same_tree

# Import built-in rules so they are registered.
from . import cleanup as _cleanup  # noqa: F401
from . import cost_rules as _cost_rules  # noqa: F401
from . import formatting as _formatting  # noqa: F401
from . import inline_ctes as _inline_ctes  # noqa: F401
from kumo_sql_prover import lift_subqueries as _lift_subqueries  # noqa: F401
from . import qualify_columns as _qualify_columns  # noqa: F401


#: Rules whose every changed statement must also pass a checker that shares no code with the rule or the
#: prover (``proof_registry``). The table belongs to this acceptance layer, keyed by rule name, so a rule (or an
#: override passed to ``apply_rule``) cannot opt out. Such a step is accepted only if both the prover and the
#: independent checker accept it.
INDEPENDENT_CHECK_FAMILIES = RULE_FAMILIES
INDEPENDENT_CHECK = "independent_check"


class VerificationStatus(str, Enum):
    """The single evidence label assigned to a rewrite result."""

    UNCHANGED = "unchanged"
    PROVEN = "proven"
    PLANNER_CHECKED = "planner_checked"
    UNPROVEN = "unproven"
    FAILED = "failed"


@dataclass(frozen=True)
class VerificationCheck:
    """One independently reported check supporting an evidence label."""

    kind: str
    outcome: str
    detail: str
    evidence: tuple[tuple[str, object], ...] = ()

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "outcome": self.outcome,
            "detail": self.detail,
        }
        if self.evidence:
            result["evidence"] = {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in self.evidence
            }
        return result


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    reason: str
    details: tuple[str, ...] = ()
    checks: tuple[VerificationCheck, ...] = ()
    #: The independent checker's records, one per changed statement of a safeguarded rule (also in ``checks``).
    proof_checks: tuple[StepCheck, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.status in (VerificationStatus.UNCHANGED, VerificationStatus.PROVEN)

    def to_json(self) -> dict[str, object]:
        """Return the same JSON shape for pipeline and per-rule results."""

        return {
            "status": self.status.value,
            "reason": self.reason,
            "details": list(self.details),
            "checks": [check.to_json() for check in self.checks],
        }


@dataclass(frozen=True)
class RewriteResult:
    """Output of one rule, with its equivalence verification."""

    rule: str
    input_sql: str
    sql: str
    changes: int
    diagnostics: tuple[RuleDiagnostic, ...]
    verification: Verification
    rule_success: bool

    @property
    def success(self) -> bool:
        """The rule ran cleanly and its output is unchanged or proven equivalent."""

        return self.rule_success and self.verification.trusted


@dataclass(frozen=True)
class PipelineResult:
    """Output of several rules applied in sequence."""

    input_sql: str
    sql: str
    steps: tuple[RewriteResult, ...]
    verification: Verification

    @property
    def success(self) -> bool:
        return all(step.rule_success for step in self.steps) and self.verification.trusted


def _statement_shell(statement: exp.Expression) -> tuple[str, exp.Expression | None]:
    """Render a statement with its query replaced by a placeholder."""

    if isinstance(statement, (exp.Select, exp.Union)):
        return "<query>", statement
    if isinstance(statement, (exp.Create, exp.Insert)) and top_level_query(statement) is not None:
        shell = statement.copy()
        query = top_level_query(statement)
        shell.set("expression", exp.Placeholder())
        return shell.sql(dialect="bigquery", comments=False), query
    return statement.sql(dialect="bigquery", comments=False), None


def _order_sql(query: exp.Expression) -> str | None:
    order = query.args.get("order")
    return order.sql(dialect="bigquery", comments=False) if order is not None else None


DEFAULT_SMT_TIMEOUT_MS = 5000


def _predicates_masked(query: exp.Expression) -> str:
    """Render a query with every WHERE, HAVING, QUALIFY and JOIN ON condition blanked."""

    masked = query.copy()
    for node in list(masked.find_all(exp.Where, exp.Having, exp.Qualify)):
        node.set("this", exp.Placeholder())
    for join in list(masked.find_all(exp.Join)):
        if join.args.get("on") is not None:
            join.set("on", exp.Placeholder())
    return masked.sql(dialect="bigquery", comments=False)


def _predicate_only_change(old_sql: str, new_sql: str) -> bool:
    try:
        old = parse_statements(old_sql)[0]
        new = parse_statements(new_sql)[0]
        return _predicates_masked(old) == _predicates_masked(new)
    except Exception:
        return False


def _lossy_types(query: exp.Expression) -> str | None:
    for node in query.find_all(exp.DataType):
        if node.this in (exp.DataType.Type.FLOAT, exp.DataType.Type.UUID):
            return node.this.value
    return None


def _known_columns(statement: exp.Expression) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The columns the loaded project and saved catalog give the physical tables a statement reads.

    The qualification check cannot learn a table's columns from the SQL, so the acceptance layer supplies them
    (catalog data, not decision code): each table the statement names, under its lower-case dotted name.
    """

    try:
        columns = prover_context.current_schema().columns
    except Exception:  # noqa: BLE001 - no schema means no known tables, and the check then refuses plain tables
        return ()
    found: dict[str, tuple[str, ...]] = {}
    for table in statement.find_all(exp.Table):
        parts = [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]
        key = ".".join(parts)
        if key in columns:
            found[key] = tuple(str(column).lower() for column in columns[key])
    return tuple(sorted(found.items()))


def _independent_checks(
    left: list[exp.Expression],
    right: list[exp.Expression],
    rule: str,
    family: str,
    section_index: int,
    step_checks: list[StepCheck],
) -> list[str]:
    """Run the independent checker on every changed statement pair; the problems it found."""

    registered = FAMILIES[family]
    problems: list[str] = []
    for index, (old, new) in enumerate(zip(left, right)):
        if same_tree(old, new):
            continue
        step = RewriteStep(
            rule, family, index, old.sql(dialect="bigquery"), new.sql(dialect="bigquery"),
            registered.assumptions, section_index,
            _known_columns(old) if family == QUALIFY_FAMILY else (),
        )
        check = registered.check(step, old.copy(), new.copy())
        step_checks.append(check)
        if not check.accepted:
            problems.append(f"statement {index}: the independent {registered.label} check refused the change: {check.reason}")
    return problems


def _layout_check(
    before: str,
    after: str,
    rule: str,
    family: str,
    section_index: int,
    step_checks: list[StepCheck],
) -> list[str]:
    """Run the independent layout checker on the whole before and after text; the problems it found.

    A formatting step is not a change of any statement's tree, so it is checked from the two texts, before any
    layout shortcut can accept it.
    """

    registered = FAMILIES[family]
    step = RewriteStep(rule, family, 0, before, after, registered.assumptions, section_index)
    check = registered.check(step, exp.Placeholder(), exp.Placeholder())
    step_checks.append(check)
    if check.accepted:
        return []
    return [f"the independent {registered.label} check refused the change: {check.reason}"]


def _verify_sql(
    before: str,
    after: str,
    smt_checks: list[VerificationCheck] | None = None,
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
    dynamic: frozenset[str] = frozenset(),
    independent: tuple[str, str, int, list[StepCheck]] | None = None,
) -> tuple[bool, list[str]]:
    if independent is not None and independent[1] == FORMAT_FAMILY:
        layout_problems = _layout_check(before, after, *independent)
        if layout_problems:
            return False, layout_problems
        independent = None  # the statements are compared by the shortcuts and the prover below
    if layout_only_change(before, after):
        if before != after and any(sentinel in before or sentinel in after for sentinel in dynamic):
            # Layout in the template is not layout in the compiled SQL: an expansion can end in a line
            # comment, so moving a newline next to it changes which tokens the comment swallows.
            return False, [
                "the layout changed in a statement holding a Dataform expression other than ref() or self(), "
                "whose expansion can be a comment; compile the SQLX to prove a change to this statement"
            ]
        # Only whitespace and the case of reserved words and built-in calls changed: proven for any
        # statement, including ones sqlglot cannot parse or keeps as an opaque command.
        return True, []
    if touching_literal_chunks(before) or touching_literal_chunks(after):
        return False, ["two string literals touch ('a''b'); GoogleSQL needs whitespace or a comment between them"]
    if any(before.count(sentinel) != after.count(sentinel) for sentinel in dynamic):
        # A Dataform expression dropped or added with a comment. sqlglot 26 drops a comment at the end of a
        # BigQuery statement from the parse tree, so the statements below would compare equal without it.
        return False, [
            "a Dataform expression other than ref() or self() was dropped, added or repeated; "
            "compile the SQLX to prove a change to this statement"
        ]
    unknown_type = invalid_type_name(before) or invalid_type_name(after)
    if unknown_type:
        # The BigQuery printer writes FLOAT as FLOAT64, INT32 as INT64 and VARCHAR as STRING, so both sides of a
        # comparison would read the same; BigQuery rejects the name and there is nothing to preserve.
        return False, [f"the query would not run on BigQuery: {unknown_type}"]
    calls = created_function_calls(before), created_function_calls(after)
    if calls[0] is None or calls[0] != calls[1]:
        # sqlglot prints `abs(1)` and `ABS(1)` alike, but a script can create both as temporary functions.
        return False, ["a call to a function the script creates changed (function names are case sensitive)"]
    try:
        left = block_statements(before)
        right = block_statements(after)
        left = parse_statements(before) if left is None else left
        right = parse_statements(after) if right is None else right
    except Exception as exc:
        return False, [f"strict parse failed: {exc}"]
    if len(left) != len(right):
        return False, [f"statement count changed from {len(left)} to {len(right)}"]
    if block_statements(before) is not None or block_statements(after) is not None:
        # In a script only the queries are proven; the control flow, conditions and declarations around them must not change.
        if script_skeleton(before) != script_skeleton(after):
            return False, ["script structure changed: control flow, conditions or declarations outside the queries differ"]

    problems: list[str] = []
    if independent is not None:
        problems.extend(_independent_checks(left, right, *independent))
    for index, (old, new) in enumerate(zip(left, right)):
        old_shell, old_query = _statement_shell(old)
        new_shell, new_query = _statement_shell(new)
        if old_shell != new_shell:
            problems.append(f"statement {index}: text outside the query changed")
            continue
        if old_query is None:
            continue
        if dynamic and old_query.sql(dialect="bigquery") != new_query.sql(dialect="bigquery") and (
            holds_dynamic_fragment(old, dynamic) or holds_dynamic_fragment(new, dynamic)
        ):
            # Masked, ${when(...)} or ${"x OR y"} reads as one name, but it can expand to several
            # operators, a clause or nothing, and can name a CTE the masked text never mentions.
            problems.append(
                f"statement {index}: it holds a Dataform expression other than ref() or self(), "
                "which can expand to any SQL; compile the SQLX to prove a change to this statement"
            )
            continue
        lossy = _lossy_types(old_query)
        if lossy:
            # The BigQuery generator prints FLOAT (a 32-bit float in GoogleSQL) as FLOAT64 and UUID as STRING,
            # on both sides of the comparison, so a rewrite would pass the check while changing the type.
            problems.append(f"statement {index}: type {lossy} is not rendered faithfully for BigQuery")
            continue
        old_sql = old_query.sql(dialect="bigquery")
        new_sql = new_query.sql(dialect="bigquery")
        if old_sql == new_sql:
            continue
        invalid = invalid_input_reason(old_query) or invalid_input_reason(new_query)
        if invalid:
            # Nothing to preserve: BigQuery would reject the statement, so no prover's agreement is evidence.
            problems.append(f"statement {index}: the query would not run on BigQuery: {invalid}")
            continue
        # The prover compares result bags; a rewrite must also keep ordering.
        if _order_sql(old_query) != _order_sql(new_query):
            problems.append(f"statement {index}: final ORDER BY changed")
            continue
        proof = prove_equivalent(old_sql, new_sql)
        if not proof.proven:
            detail = "; ".join(proof.diagnostics)
            message = f"statement {index}: {proof.reason}" + (f" ({detail})" if detail else "")
            # The solver (algebraic rewrites plus SMT, using the project's declared keys and
            # NOT NULL columns) tries any change; with it off, only a change confined to predicates.
            solver_on = prover_context.settings()["enabled"]
            if solver_on or _predicate_only_change(old_sql, new_sql):
                if solver_on:
                    smt = prover_context.prove(
                        old_sql,
                        new_sql,
                        timeout_ms=None if smt_timeout_ms == DEFAULT_SMT_TIMEOUT_MS else smt_timeout_ms,
                    )
                else:
                    smt = prove_equivalent_smt(old_sql, new_sql, timeout_ms=smt_timeout_ms)
                if smt.status is SmtStatus.PROVEN_EQUIVALENT:
                    if smt_checks is not None:
                        smt_checks.append(
                            VerificationCheck(
                                "smt_proof",
                                "passed",
                                f"statement {index}: "
                                + ("change proven equivalent by the solver" if solver_on else "predicate-only change proven equivalent by SMT"),
                                (("assumptions", tuple(smt.assumptions)),),
                            )
                        )
                    continue
                label = (
                    "SMT found a counterexample"
                    if smt.status is SmtStatus.NOT_EQUIVALENT
                    else "SMT could not prove it"
                )
                message += f"; {label}: {smt.reason}"
                if smt_checks is not None:
                    smt_checks.append(
                        VerificationCheck(
                            "smt_proof",
                            "refuted" if smt.status is SmtStatus.NOT_EQUIVALENT else "not_proven",
                            f"statement {index}: {smt.reason}",
                        )
                    )
            problems.append(message)
    return not problems, problems


def _verify_sqlx(
    before: str,
    after: str,
    smt_checks: list[VerificationCheck] | None = None,
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
    independent: tuple[str, str, list[StepCheck]] | None = None,
) -> tuple[bool, list[str]]:
    try:
        left = split_sqlx_sections(before)
        right = split_sqlx_sections(after)
    except Exception as exc:
        return False, [f"SQLX could not be split: {exc}"]
    if [kind for kind, _ in left] != [kind for kind, _ in right]:
        return False, ["SQLX section layout changed"]

    problems: list[str] = []
    for section, ((kind, old), (_, new)) in enumerate(zip(left, right)):
        if kind == "block":
            if old != new:
                problems.append("a SQLX config/js/operations block changed")
            continue
        if old == new or (not old.strip() and not new.strip()):
            continue
        try:
            ok, section_problems = _verify_sql(
                mask_sqlx_by_content(old),
                mask_sqlx_by_content(new),
                smt_checks,
                smt_timeout_ms,
                dynamic_sentinels(old) | dynamic_sentinels(new),
                None if independent is None else (independent[0], independent[1], section, independent[2]),
            )
        except Exception as exc:
            ok, section_problems = False, [f"SQLX interpolations could not be masked: {exc}"]
        if not ok:
            problems.extend(section_problems)
    return not problems, problems


def verify_rewrite(
    before: str,
    after: str,
    *,
    planner_check: VerificationCheck | None = None,
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
) -> Verification:
    """Check a rewrite and return one label with the evidence supporting it.

    A caller may attach a planner check when one has run. Planner evidence is
    reported separately from the structural proof; it can yield
    ``planner_checked`` when no proof is available, but that label is not
    trusted for automatic acceptance.
    """

    return _verify_rewrite(
        before, after, planner_check=planner_check, smt_timeout_ms=smt_timeout_ms
    )


def _step_check_record(check: StepCheck) -> VerificationCheck:
    step = check.step
    evidence: tuple[tuple[str, object], ...] = (
        ("family", step.family),
        ("statement_index", step.statement_index),
        ("assumptions", step.assumptions),
        ("cases_checked", check.cases_checked),
    )
    if step.section_index >= 0:
        evidence += (("section_index", step.section_index),)
    if check.counterexample:
        evidence += (("counterexample", tuple(f"{label} = {'NULL' if value is None else str(value).upper()}" for label, value in check.counterexample)),)
    return VerificationCheck(
        INDEPENDENT_CHECK,
        "passed" if check.accepted else "failed",
        f"statement {step.statement_index}: {check.reason}",
        evidence,
    )


def _verify_rewrite(
    before: str,
    after: str,
    *,
    planner_check: VerificationCheck | None = None,
    rewrite_succeeded: bool = True,
    failure_details: tuple[str, ...] = (),
    smt_timeout_ms: int = DEFAULT_SMT_TIMEOUT_MS,
    rule: str | None = None,
) -> Verification:
    if planner_check is not None and planner_check.kind != "planner":
        raise ValueError("planner_check must have kind='planner'")

    checks: list[VerificationCheck] = []
    if before == after:
        checks.append(
            VerificationCheck("change_detection", "passed", "Output is identical to input.")
        )
        if planner_check is not None:
            checks.append(planner_check)
        if not rewrite_succeeded:
            details = failure_details or ("The rewrite rule did not complete successfully.",)
            checks.append(VerificationCheck("rewrite", "failed", "; ".join(details)))
            return Verification(
                VerificationStatus.FAILED,
                "the rewrite rule did not complete successfully",
                details,
                tuple(checks),
            )
        if planner_check is not None and planner_check.outcome == "failed":
            return Verification(
                VerificationStatus.UNPROVEN,
                "the planner check failed",
                (planner_check.detail,),
                tuple(checks),
            )
        return Verification(
            VerificationStatus.UNCHANGED,
            "output is identical to input",
            checks=tuple(checks),
        )

    smt_checks: list[VerificationCheck] = []
    family = INDEPENDENT_CHECK_FAMILIES.get(rule) if rule else None
    step_checks: list[StepCheck] = []
    if looks_like_sqlx(before) or looks_like_sqlx(after):
        independent = None if family is None else (rule, family, step_checks)
        ok, problems = _verify_sqlx(before, after, smt_checks, smt_timeout_ms, independent)
    else:
        independent = None if family is None else (rule, family, -1, step_checks)
        ok, problems = _verify_sql(before, after, smt_checks, smt_timeout_ms, independent=independent)
    proof_checks = tuple(step_checks)

    proof_detail = (
        "Every changed statement was proven equivalent."
        if ok
        else "; ".join(problems) or "The equivalence prover could not establish equivalence."
    )
    checks.append(
        VerificationCheck("equivalence_proof", "passed" if ok else "not_proven", proof_detail)
    )
    checks.extend(smt_checks)
    checks.extend(_step_check_record(check) for check in proof_checks)
    checks.append(
        planner_check
        or VerificationCheck("planner", "not_run", "No planner check was run.")
    )

    if not rewrite_succeeded:
        details = failure_details or ("The rewrite rule did not complete successfully.",)
        checks.append(VerificationCheck("rewrite", "failed", "; ".join(details)))
        return Verification(
            VerificationStatus.FAILED,
            "the rewrite rule did not complete successfully",
            details,
            tuple(checks),
            proof_checks,
        )

    if ok and (planner_check is None or planner_check.outcome in ("passed", "not_run")):
        return Verification(
            VerificationStatus.PROVEN,
            "every changed statement was proven equivalent",
            checks=tuple(checks),
            proof_checks=proof_checks,
        )
    if planner_check is not None and planner_check.outcome == "passed":
        return Verification(
            VerificationStatus.PLANNER_CHECKED,
            "the planner check passed, but equivalence could not be proven",
            tuple(problems),
            tuple(checks),
            proof_checks,
        )
    if planner_check is not None and planner_check.outcome == "failed":
        return Verification(
            VerificationStatus.UNPROVEN,
            "the planner check failed",
            (planner_check.detail,),
            tuple(checks),
            proof_checks,
        )
    return Verification(
        VerificationStatus.UNPROVEN,
        "equivalence could not be proven for every changed statement",
        tuple(problems),
        tuple(checks),
    )


RECOVERED_PARSE_CHECK = "recovered_parse"


def _without_unchanged_trust(verification: Verification, recovered: bool) -> Verification:
    """Withdraw the ``unchanged`` label from a result whose input only parsed in recovery mode.

    Recovery keeps valid BigQuery that sqlglot cannot parse strictly, but it also accepts truncated or
    trailing-garbage input (``WHERE 1 =``). A rule that never changed such an input has not shown it is
    SQL, so "the output is identical to the input" must not read as trusted.
    """

    if not recovered or verification.status is not VerificationStatus.UNCHANGED:
        return verification
    detail = "Strict BigQuery parsing failed and the statements were read in sqlglot recovery mode."
    return Verification(
        VerificationStatus.UNPROVEN,
        "the output is identical to the input, but the input only parsed in recovery mode and may not be valid SQL",
        (detail,),
        verification.checks + (VerificationCheck(RECOVERED_PARSE_CHECK, "failed", detail),),
        verification.proof_checks,
    )


def _result(rule_name: str, sql: str, output: RuleOutput) -> RewriteResult:
    failure_details = tuple(
        f"{diagnostic.code}: {diagnostic.message}" for diagnostic in output.diagnostics
    )
    verification = _without_unchanged_trust(
        _verify_rewrite(
            sql,
            output.sql,
            rewrite_succeeded=output.success,
            failure_details=failure_details,
            rule=rule_name,
        ),
        any(diagnostic.code == "recovered_parse" for diagnostic in output.diagnostics),
    )
    return RewriteResult(
        rule=rule_name,
        input_sql=sql,
        sql=output.sql,
        changes=output.changes,
        diagnostics=output.diagnostics,
        verification=verification,
        rule_success=output.success,
    )


def apply_rule(
    name: str, sql: str, *, overrides: Mapping[str, RewriteRule] | None = None
) -> RewriteResult:
    """Apply one registered rule and verify its output.

    ``overrides`` swaps in a differently configured instance for a rule name.
    """

    rule = overrides[name] if overrides and name in overrides else get_rule(name)
    return _result(name, sql, rule.apply(sql))


def apply_rules(
    names: Iterable[str], sql: str, *, overrides: Mapping[str, RewriteRule] | None = None
) -> PipelineResult:
    """Apply rules in order, verifying every step and the end-to-end result."""

    steps: list[RewriteResult] = []
    current = sql
    for name in names:
        step = apply_rule(name, current, overrides=overrides)
        steps.append(step)
        current = step.sql

    step_checks = tuple(
        VerificationCheck(
            check.kind,
            check.outcome,
            f"{step.rule}: {check.detail}",
            (
                tuple(
                    (key, value)
                    for key, value in check.evidence
                    if key not in ("scope", "rule")
                )
                + (("scope", "step"), ("rule", step.rule))
                if check.kind == "planner"
                else check.evidence
            ),
        )
        for step in steps
        for check in step.verification.checks
    )
    rule_succeeded = all(step.rule_success for step in steps)
    failure_details = tuple(
        f"{step.rule}: {diagnostic.code}: {diagnostic.message}"
        for step in steps
        if not step.rule_success
        for diagnostic in step.diagnostics
    ) or tuple(
        f"{step.rule}: {step.verification.reason}"
        for step in steps
        if not step.rule_success
    )

    # A safeguarded rule's step must be accepted on its own: a failed independent check anywhere, or any
    # untrusted change by a rule in INDEPENDENT_CHECK_FAMILIES, cannot be rescued later in the pipeline.
    refused = tuple(
        f"{step.rule}: {check.detail}"
        for step in steps
        for check in step.verification.checks
        if check.kind == INDEPENDENT_CHECK and check.outcome == "failed"
    ) or tuple(
        f"{step.rule}: {step.verification.reason}"
        for step in steps
        if step.rule in INDEPENDENT_CHECK_FAMILIES and step.rule_success and not step.verification.trusted
    )
    proof_checks = tuple(check for step in steps for check in step.verification.proof_checks)
    if not rule_succeeded:
        base = _verify_rewrite(
            sql,
            current,
            rewrite_succeeded=False,
            failure_details=failure_details,
        )
        verification = Verification(
            base.status,
            base.reason,
            base.details,
            step_checks + base.checks,
            proof_checks,
        )
    elif refused:
        # Neither a later step that undoes the change nor an end-to-end proof (by the same prover the checker
        # guards) can rescue a safeguarded step that was not accepted.
        verification = Verification(
            VerificationStatus.UNPROVEN,
            "a safeguarded step was not accepted, so the pipeline is not trusted even if later steps undo it",
            refused,
            step_checks,
            proof_checks,
        )
    elif current == sql:
        base = verify_rewrite(sql, current)
        planner_failure = next(
            (
                check
                for check in step_checks
                if check.kind == "planner" and check.outcome == "failed"
            ),
            None,
        )
        if planner_failure is not None:
            verification = Verification(
                VerificationStatus.UNPROVEN,
                "the planner check failed",
                (planner_failure.detail,),
                step_checks + base.checks,
            )
        else:
            verification = Verification(
                base.status,
                base.reason,
                base.details,
                step_checks + base.checks,
            )
    elif all(step.verification.trusted for step in steps):
        verification = Verification(
            VerificationStatus.PROVEN,
            "every step was unchanged or proven equivalent",
            checks=step_checks,
            proof_checks=proof_checks,
        )
    else:
        # A chain with an unproven step can still be proven directly.
        base = verify_rewrite(sql, current)
        planner_failure = next(
            (
                check
                for check in step_checks
                if check.kind == "planner" and check.outcome == "failed"
            ),
            None,
        )
        if planner_failure is not None:
            verification = Verification(
                VerificationStatus.UNPROVEN,
                "the planner check failed",
                (planner_failure.detail,),
                step_checks + base.checks,
            )
        elif base.trusted:
            verification = Verification(
                base.status,
                base.reason,
                base.details,
                step_checks + base.checks,
            )
        else:
            verification = Verification(
                base.status,
                base.reason,
                base.details,
                step_checks + base.checks,
            )
    verification = _without_unchanged_trust(
        verification,
        any(diagnostic.code == "recovered_parse" for step in steps for diagnostic in step.diagnostics),
    )
    return PipelineResult(sql, current, tuple(steps), verification)


def _planner_check_evidence(
    *,
    original_planned: bool | None = None,
    rewritten_planned: bool | None = None,
    schema_matches: bool | None = None,
    schema_differences: tuple[str, ...] = (),
    estimated_bytes_delta: int | None = None,
    compiled_sql_used: bool = False,
    scope: str = "end_to_end",
) -> tuple[tuple[str, object], ...]:
    return (
        ("scope", scope),
        ("original_planned", original_planned),
        ("rewritten_planned", rewritten_planned),
        ("schema_matches", schema_matches),
        ("schema_differences", schema_differences),
        ("estimated_bytes_delta", estimated_bytes_delta),
        ("results_compared", False),
        ("compiled_sql_used", compiled_sql_used),
    )


def _replace_end_to_end_planner_check(
    verification: Verification, planner_check: VerificationCheck
) -> Verification:
    checks = tuple(
        check
        for check in verification.checks
        if not (
            check.kind == "planner"
            and (
                (check.outcome == "not_run" and check.detail == "No planner check was run.")
                or dict(check.evidence).get("scope") == "end_to_end"
            )
        )
    ) + (planner_check,)

    if verification.status is VerificationStatus.FAILED:
        return replace(verification, checks=checks)
    if planner_check.outcome == "failed":
        return Verification(
            VerificationStatus.UNPROVEN,
            "the planner check failed; query result equality was not established",
            (planner_check.detail,),
            checks,
        )
    if (
        planner_check.outcome == "passed"
        and verification.status is VerificationStatus.UNPROVEN
    ):
        return Verification(
            VerificationStatus.PLANNER_CHECKED,
            "both queries planned with matching schemas; query result equality was not established",
            verification.details,
            checks,
        )
    return replace(verification, checks=checks)


def attach_planner_check(
    result: RewriteResult | PipelineResult,
    project: str,
    *,
    location: str | None = None,
    token: str | None = None,
    transport: Transport | None = None,
    compiled_original_sql: str | None = None,
    compiled_rewritten_sql: str | None = None,
) -> RewriteResult | PipelineResult:
    """Opt in to planner evidence for a changed result.

    Planner checks compare whether both queries plan and whether their output
    schemas match. They do not compare rows and never establish equivalence.
    This function is the only rewrite-result entry point that invokes the
    BigQuery dry-run transport; ordinary rewriting remains offline.

    SQLX sources need both compiled SQL strings. Only a single SELECT query
    on each side is planned; multi-statement scripts and DML/DDL are reported
    as not planned.
    """

    if not project:
        raise ValueError("project is required to run a planner check")

    before = result.input_sql
    after = result.sql
    verification = result.verification
    rule_failed = verification.status is VerificationStatus.FAILED or (
        isinstance(result, RewriteResult) and not result.rule_success
    ) or (
        isinstance(result, PipelineResult)
        and any(not step.rule_success for step in result.steps)
    )

    def not_run(detail: str, *, compiled: bool = False) -> RewriteResult | PipelineResult:
        check = VerificationCheck(
            "planner",
            "not_run",
            detail,
            _planner_check_evidence(compiled_sql_used=compiled),
        )
        return replace(
            result,
            verification=_replace_end_to_end_planner_check(verification, check),
        )

    if rule_failed:
        return not_run("Planner check was skipped because the rewrite failed.")
    if before == after:
        return not_run("Planner check was skipped because the output did not change.")

    compiled_supplied = compiled_original_sql is not None or compiled_rewritten_sql is not None
    is_sqlx = looks_like_sqlx(before) or looks_like_sqlx(after)
    if compiled_supplied and not is_sqlx:
        return not_run(
            "Planner check was skipped; compiled SQL overrides are only accepted for SQLX."
        )
    if compiled_supplied and (
        compiled_original_sql is None or compiled_rewritten_sql is None
    ):
        return not_run(
            "Planner check was skipped; provide both compiled SQL inputs for SQLX.",
        )
    if is_sqlx and not compiled_supplied:
        return not_run(
            "Planner check was skipped because SQLX must be compiled first; provide both compiled SQL inputs."
        )

    planner_before = compiled_original_sql if compiled_supplied else before
    planner_after = compiled_rewritten_sql if compiled_supplied else after
    assert planner_before is not None and planner_after is not None
    if looks_like_sqlx(planner_before) or looks_like_sqlx(planner_after):
        return not_run(
            "Planner check was skipped because the supplied SQLX inputs are not compiled SQL.",
            compiled=compiled_supplied,
        )

    try:
        check = check_rewrite(
            planner_before,
            planner_after,
            project,
            location=location,
            token=token,
            transport=transport,
        )
    except (RuntimeError, OSError, URLError) as exc:
        not_run_check = VerificationCheck(
            "planner",
            "not_run",
            f"Planner check was not run: {exc}",
            _planner_check_evidence(compiled_sql_used=compiled_supplied),
        )
        return replace(
            result,
            verification=_replace_end_to_end_planner_check(verification, not_run_check),
        )

    outcome = check.outcome
    detail = f"{check.reason}; query result equality was not checked"
    planner_record = VerificationCheck(
        "planner",
        outcome,
        detail,
        _planner_check_evidence(
            original_planned=check.original_planned,
            rewritten_planned=check.rewritten_planned,
            schema_matches=check.schema_matches,
            schema_differences=check.schema_differences,
            estimated_bytes_delta=check.estimated_bytes_delta,
            compiled_sql_used=compiled_supplied,
        ),
    )
    return replace(
        result,
        verification=_replace_end_to_end_planner_check(verification, planner_record),
    )


@dataclass(frozen=True)
class IdempotenceCheck:
    """Whether re-running a rule list on its own output changes it again."""

    rules: tuple[str, ...]
    idempotent: bool
    sql: str
    rerun_sql: str
    rules_that_changed: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "kind": "idempotence",
            "outcome": "passed" if self.idempotent else "failed",
            "rules": list(self.rules),
            "rules_that_changed": list(self.rules_that_changed),
        }


def check_idempotence(
    names: Iterable[str],
    sql: str,
    *,
    overrides: Mapping[str, RewriteRule] | None = None,
) -> IdempotenceCheck:
    """Apply ``names`` to ``sql``, then again to the output, and compare the text.

    The comparison is on exact text so formatting oscillation is caught. Running
    the rules twice doubles the work, so this is opt in (``python -m kumosql rewrite-sql
    --check-idempotence``) rather than part of ``apply_rules``.
    """

    rules = tuple(names)
    first = apply_rules(rules, sql, overrides=overrides)
    second = apply_rules(rules, first.sql, overrides=overrides)
    changed = tuple(step.rule for step in second.steps if step.changes or step.sql != step.input_sql)
    return IdempotenceCheck(
        rules=rules,
        idempotent=second.sql == first.sql and not changed,
        sql=first.sql,
        rerun_sql=second.sql,
        rules_that_changed=changed,
    )


def canonical_rule_order() -> tuple[str, ...]:
    """The rules that can run together, in an order whose output is a fixed point.

    ``lift_subqueries`` and ``inline_single_use_ctes`` are inverses, so a
    pipeline holding both undoes and redoes its own work on every run; the
    canonical order keeps ``inline_single_use_ctes`` and leaves the lifter to
    be run on its own, and so does ``qualify_columns``, which makes SQL longer
    and is a style choice. Rules that re-render a statement discard the layout
    ``format_sql`` produced, so ``format_sql`` goes last.
    """

    names = [n for n, rule in available_rules().items() if n not in ("lift_subqueries", "format_sql") and not rule.opt_in]
    # Removing unused and duplicate CTEs can leave another CTE with a single
    # reader, so inlining has to come after both or one pass is not enough.
    if "inline_single_use_ctes" in names:
        names.remove("inline_single_use_ctes")
        after = max((names.index(n) for n in ("deduplicate_ctes", "remove_unused_ctes") if n in names), default=-1)
        names.insert(after + 1, "inline_single_use_ctes")
    if "format_sql" in available_rules():
        names.append("format_sql")
    return tuple(names)


__all__ = [
    "IdempotenceCheck",
    "PipelineResult",
    "RewriteResult",
    "Verification",
    "VerificationCheck",
    "VerificationStatus",
    "apply_rule",
    "apply_rules",
    "attach_planner_check",
    "available_rules",
    "canonical_rule_order",
    "check_idempotence",
    "verify_rewrite",
]
