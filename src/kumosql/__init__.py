"""KumoSQL."""

from . import _prover_aliases

_prover_aliases.install()  # kumosql.<moved module> is kumo_sql_prover.<moved module>; must run before any import below

from kumo_sql_prover import bigquery_syntax as _bigquery_syntax  # noqa: F401  (teaches sqlglot TABLE arguments of table-valued functions)
from kumo_sql_prover.lift_subqueries import (
    LiftDiagnostic,
    LiftResult,
    count_inline_subqueries,
    lift_subqueries,
)
from .engine import (
    RewriteRule,
    RuleDiagnostic,
    RuleOutput,
    available_rules,
    get_rule,
    register_rule,
)
from .inline_ctes import InlineSingleUseCtesRule
from .cleanup import (
    DeduplicateCtesRule,
    RemoveRedundantParenthesesRule,
    RemoveTrivialPredicatesRule,
    RemoveUnusedCtesRule,
)
from .formatting import (
    Complexity,
    FormatPreferences,
    FormatSqlRule,
    complexity,
    format_sql,
)
from .scopes import Scope, UnknownFieldError, discover_fields, get_scope, list_scopes, save_scope
from .evidence_summary import EvidenceSummary, summarize_evidence
from .rewrite import (
    PipelineResult,
    RewriteResult,
    Verification,
    VerificationCheck,
    VerificationStatus,
    apply_rule,
    apply_rules,
    attach_planner_check,
    canonical_rule_order,
    check_idempotence,
    verify_rewrite,
)
from kumo_sql_prover.equivalence import (
    EquivalenceResult,
    EquivalenceStatus,
    build_bag_verifier_sql,
    prove_equivalent,
)
from kumo_sql_prover.smt_equivalence import (
    Counterexample,
    SmtEquivalenceResult,
    SmtStatus,
    prove_equivalent_smt,
)
from .synthetic_check import attach_synthetic_check
from kumo_sql_prover.result_equivalence import (
    ResultEquivalence,
    ResultEquivalenceStatus,
    assert_result_equivalent,
    check_result_equivalence,
    generate_synthetic_dataset,
)
from .pipeline import (
    ColumnFilter,
    ColumnLineage,
    ColumnRef,
    ColumnTrace,
    Pipeline,
    Target,
    load_compiled_graph,
    load_sqlx_project,
)
from .impact import AffectedModel, ChangeImpact, UnknownReader, assess_change
from .identity import IdentityResolution, NodeIdentity, normalize_table_reference
from .graph import (
    GraphEdge,
    GraphNode,
    GraphResult,
    ObservedRead,
    build_query_graph,
    edge_confidence,
)
from .repeated_work import RepeatedWork, find_repeated_work, repeated_work_report
from .observed_usage import TableUsage, UsageResult, observed_usage, observed_usage_report
from .table_roles import RoleSignal, TableRole, infer_roles, table_roles_report
from .near_duplicates import (
    ClauseDifference,
    NearDuplicateCluster,
    NearDuplicateVariant,
    QueryParameter,
    find_near_duplicates,
)
from .dryrun import (
    DryRunResult,
    RewriteCheck,
    check_rewrite,
    dry_run,
    fetch_table_schemas,
)
from .fingerprint import (
    ComparisonPlan,
    Location,
    ModelDiff,
    TableComparison,
    compare_snapshots,
    compare_tables_sql,
    diff_rows_sql,
    plan_output_comparison,
    summarize_comparison,
    table_fingerprint_sql,
)
from .table_profile import (
    AttributeMeaning,
    Grain,
    RowScope,
    TableProfile,
    profile_pipeline,
    profile_query,
)
from .overlap import Check, Match, MatchRole, OverlapResult, find_overlaps
from .overlap_report import OverlapChecker
from .rollups import GrainMapping, Rollup, RollupResult, find_rollups

__all__ = [
    "Check",
    "Match",
    "MatchRole",
    "OverlapResult",
    "find_overlaps",
    "OverlapChecker",
    "GrainMapping",
    "Rollup",
    "RollupResult",
    "find_rollups",
    "AttributeMeaning",
    "Grain",
    "RowScope",
    "TableProfile",
    "profile_pipeline",
    "profile_query",
    "EvidenceSummary",
    "summarize_evidence",
    "RepeatedWork",
    "find_repeated_work",
    "repeated_work_report",
    "RoleSignal",
    "TableRole",
    "infer_roles",
    "observed_usage",
    "observed_usage_report",
    "TableUsage",
    "UsageResult",
    "table_roles_report",
    "LiftDiagnostic",
    "LiftResult",
    "count_inline_subqueries",
    "lift_subqueries",
    "EquivalenceResult",
    "EquivalenceStatus",
    "build_bag_verifier_sql",
    "prove_equivalent",
    "Counterexample",
    "SmtEquivalenceResult",
    "SmtStatus",
    "prove_equivalent_smt",
    "ResultEquivalence",
    "ResultEquivalenceStatus",
    "assert_result_equivalent",
    "check_result_equivalence",
    "generate_synthetic_dataset",
    "Complexity",
    "FormatPreferences",
    "FormatSqlRule",
    "Scope",
    "UnknownFieldError",
    "discover_fields",
    "complexity",
    "format_sql",
    "get_scope",
    "list_scopes",
    "save_scope",
    "InlineSingleUseCtesRule",
    "DeduplicateCtesRule",
    "RemoveRedundantParenthesesRule",
    "RemoveTrivialPredicatesRule",
    "RemoveUnusedCtesRule",
    "PipelineResult",
    "RewriteResult",
    "RewriteRule",
    "RuleDiagnostic",
    "RuleOutput",
    "Verification",
    "VerificationCheck",
    "VerificationStatus",
    "apply_rule",
    "apply_rules",
    "attach_planner_check",
    "attach_synthetic_check",
    "available_rules",
    "canonical_rule_order",
    "check_idempotence",
    "get_rule",
    "register_rule",
    "verify_rewrite",
    "ColumnFilter",
    "ColumnLineage",
    "ColumnRef",
    "ColumnTrace",
    "IdentityResolution",
    "NodeIdentity",
    "Pipeline",
    "AffectedModel",
    "ChangeImpact",
    "UnknownReader",
    "assess_change",
    "Target",
    "normalize_table_reference",
    "GraphEdge",
    "GraphNode",
    "GraphResult",
    "ObservedRead",
    "build_query_graph",
    "edge_confidence",
    "load_compiled_graph",
    "load_sqlx_project",
    "ClauseDifference",
    "NearDuplicateCluster",
    "NearDuplicateVariant",
    "QueryParameter",
    "find_near_duplicates",
    "DryRunResult",
    "RewriteCheck",
    "check_rewrite",
    "dry_run",
    "fetch_table_schemas",
    "ComparisonPlan",
    "Location",
    "ModelDiff",
    "TableComparison",
    "compare_snapshots",
    "compare_tables_sql",
    "diff_rows_sql",
    "plan_output_comparison",
    "summarize_comparison",
    "table_fingerprint_sql",
]

__version__ = "0.1.0"
