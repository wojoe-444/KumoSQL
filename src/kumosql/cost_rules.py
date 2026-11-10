"""Catalog of cost rewrite rules.

A cost rule changes the work a query does, not just how it reads. Each rule
ships on its own with three things: the conditions under which it is safe, what
evidence a reviewer needs, and a measured outcome that stays unknown until the
saving is measured on real jobs. Rules not yet built are listed as
``not_implemented`` so the catalog shows the intended scope without claiming it.

``rule_catalog()`` returns the ``rules[]`` shape documented in
``docs/ui-roadmap.md``: ``id``, ``name``, ``state``, ``safe_when``,
``requires`` and ``outcome``, plus ``measured_outcome`` (null until measured).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from kumo_sql_prover.distinct_safety import distinct_is_redundant
from .engine import RewriteRule, RuleDiagnostic, register_rule

SHIPPED = "shipped"
NOT_IMPLEMENTED = "not_implemented"

# What a person must have before accepting the rewrite.
REQUIRES_PROVEN = "proven"
REQUIRES_REVIEW = "review"

NOT_MEASURED = "Not yet measured"


@dataclass(frozen=True)
class MeasuredOutcome:
    """A measured before/after for one rule. Absent until a measurement exists."""

    basis: str  # "measured", "estimate" or "upper_bound"
    before: float
    after: float
    source: str

    def to_json(self) -> dict:
        return {
            "basis": self.basis,
            "before": self.before,
            "after": self.after,
            "source": self.source,
        }


@dataclass(frozen=True)
class CostRuleSpec:
    id: str
    name: str
    state: str
    safe_when: str
    requires: str
    measured: MeasuredOutcome | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "safe_when": self.safe_when,
            "requires": self.requires,
            "outcome": (
                f"Measured: {self.measured.before:g} to {self.measured.after:g}"
                if self.measured
                else NOT_MEASURED
            ),
            "measured_outcome": self.measured.to_json() if self.measured else None,
        }


CATALOG: tuple[CostRuleSpec, ...] = (
    CostRuleSpec(
        id="remove_redundant_distinct",
        name="Remove redundant DISTINCT",
        state=SHIPPED,
        safe_when=(
            "SELECT DISTINCT over a plain GROUP BY on columns, with every "
            "grouping column projected unchanged; no ROLLUP, CUBE, GROUPING "
            "SETS, expression or ordinal keys."
        ),
        requires=REQUIRES_PROVEN,
    ),
    CostRuleSpec(
        id="drop_unused_cte_columns",
        name="Drop unused CTE columns",
        state=NOT_IMPLEMENTED,
        safe_when="The CTE has one reference and the column is never read downstream.",
        requires=REQUIRES_PROVEN,
    ),
    CostRuleSpec(
        id="partition_filter_pushdown",
        name="Partition filter pushdown",
        state=NOT_IMPLEMENTED,
        safe_when="The filter is on the partition column and is not moved past a boundary that changes rows.",
        requires=REQUIRES_REVIEW,
    ),
    CostRuleSpec(
        id="select_star_to_columns",
        name="Replace SELECT * with used columns",
        state=NOT_IMPLEMENTED,
        safe_when="The table schema is known and no consumer depends on the dropped columns.",
        requires=REQUIRES_REVIEW,
    ),
)


def rule_catalog() -> list[dict]:
    """JSON-ready ``rules[]`` for the cost view."""

    return [spec.to_json() for spec in CATALOG]


@register_rule
class RemoveRedundantDistinctRule(RewriteRule):
    """Remove DISTINCT where a GROUP BY on the projected keys already dedupes."""

    name = "remove_redundant_distinct"
    summary = "Remove DISTINCT when grouping columns are all projected"

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        # DML rewrites are not proven, so they are left alone.
        if isinstance(statement, (exp.Update, exp.Delete, exp.Merge)):
            return 0, []
        changes = 0
        for select in list(statement.find_all(exp.Select)):
            if distinct_is_redundant(select):
                select.set("distinct", None)
                changes += 1
        return changes, []
