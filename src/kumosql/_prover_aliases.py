"""Old import paths of the modules that moved to the ``kumo_sql_prover`` package.

The equivalence prover lives in its own distribution (``kumo-sql-prover``, import name ``kumo_sql_prover``). KumoSQL's
own modules import it by that name. Code written against earlier KumoSQL, and this repository's tests, import the same
modules as ``kumosql.<name>`` (``from kumosql.equivalence import prove_equivalent``,
``monkeypatch.setattr("kumosql.smt_equivalence.f", ...)``). Rather than keep a re-export file for each of the 132
moved modules, which would copy names and leave private ones behind, ``import kumosql.<name>`` resolves to the very same
module object as ``import kumo_sql_prover.<name>``: one copy, one set of module globals, so patching either spelling
patches the module. ``tests/test_prover_aliases.py`` pins the list to what the installed package contains.

The finder only answers for names in ``MOVED``; every other ``kumosql.*`` import is untouched. It does not support
``python -m kumosql.<moved name>`` (use ``python -m kumo_sql_prover.<name>``); the CLI commands go through
``python -m kumosql <command>``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys

#: Top-level modules and packages of ``kumo_sql_prover`` (everything but its ``__init__``).
MOVED = frozenset(
    {
        "aggregate_rules",
        "algebraic_equivalence",
        "ast_utils",
        "bigquery_constructs",
        "bigquery_on_duckdb",
        "bigquery_syntax",
        "boolean_identities",
        "bounded_equivalence",
        "bounded_refutation",
        "canonical",
        "canonical_rules",
        "case_arm_rules",
        "cast_rules",
        "clause_alias_reads",
        "closed_sources",
        "conditional_equivalence",
        "constant_correlation",
        "constant_regroup_rules",
        "constraint_dependence",
        "constraint_normalization",
        "correlated_key_groups",
        "count_case_rules",
        "counted_membership",
        "counterexample",
        "date_ranges",
        "decorrelation_rules",
        "dedup_join_rules",
        "difference_explanation",
        "difference_surface",
        "disjoint_union",
        "distinct_partition_rules",
        "distinct_rules",
        "distinct_safety",
        "domain_join_rules",
        "duckdb_load",
        "eager_aggregation",
        "eager_sum_readback",
        "empty_rules",
        "equivalence",
        "executed_refutation",
        "exists_constant_rules",
        "fk_rules",
        "float_sum_order",
        "grouped_join_facts",
        "grouped_outer_joins",
        "grouped_self_membership",
        "grouped_sums",
        "grouping_expansion",
        "grouping_sets",
        "having_count_rules",
        "having_rules",
        "in_null_candidates",
        "input_validity",
        "int_div_rules",
        "intersection_rules",
        "join_rewrites",
        "keyed_rules",
        "keyed_set_join",
        "lateral_boolean_groups",
        "layout_equivalence",
        "lift_subqueries",
        "like_rules",
        "limit_rules",
        "listed_output_names",
        "literal_fold_rules",
        "lone_source",
        "membership_dedup",
        "minimize",
        "mysql_boolean_outputs",
        "mysql_exists_casts",
        "named_windows",
        "nonnull_any",
        "null_facts",
        "null_rejecting_joins",
        "numeric_column_reading",
        "one_row_tails",
        "outer_filters",
        "outer_join_exists",
        "outer_join_flatten",
        "outer_on_rejection",
        "output_properties",
        "parse_check",
        "partition_rules",
        "passthrough_sources",
        "printed_spelling",
        "projected_in_rules",
        "proof_columns",
        "proof_ctes",
        "proof_format",
        "proof_lift",
        "proof_qualify",
        "proof_registry",
        "proof_steps",
        "proof_syntax",
        "quantified_rules",
        "query_mutants",
        "refute",
        "regroup_arithmetic",
        "result_equivalence",
        "row_bound_rules",
        "rule_driver",
        "scalar_subqueries",
        "semijoin_rules",
        "semijoin_shapes",
        "set_aggregates",
        "set_identity",
        "set_operation_types",
        "set_operations",
        "set_split_rules",
        "setop_rules",
        "shape_fallback",
        "single_source_columns",
        "singleton_aggregate_rules",
        "smt_args",
        "smt_equivalence",
        "smt_errors",
        "smt_group_sums",
        "smt_literal_coercion",
        "smt_numeric",
        "smt_values",
        "solver_lock",
        "sqlsolver_backend",
        "sqlx",
        "sqlx_fragments",
        "statement_proof",
        "string_literals",
        "string_number_compare",
        "string_number_literals",
        "string_spelling_rules",
        "struct_fields",
        "structural_identity",
        "sum_of_counts",
        "tailed_grouping_sets",
        "tailed_outer_aggregates",
        "targeted_data",
        "tie_determinism",
        "tie_witness",
        "tuple_count_rules",
        "type_names",
        "uexpr",
        "union_filter_rules",
        "unmatched_outer_join",
        "unnest_literals",
        "unread_windows",
        "using_rules",
        "verdict_cache",
        "volatile_calls",
        "window_canonical",
        "window_rules",
        "wrapped_join_filters",
    }
)

_PREFIX = "kumosql."
_TARGET = "kumo_sql_prover."


class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return None  # a throwaway module; exec_module swaps in the real one

    def exec_module(self, module):
        real = importlib.import_module(_TARGET + module.__name__[len(_PREFIX) :])
        sys.modules[module.__name__] = real


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_PREFIX) or fullname[len(_PREFIX) :].split(".")[0] not in MOVED:
            return None
        return importlib.machinery.ModuleSpec(fullname, _Loader())


#: Module names that `kumosql/__init__` also binds to a function (``from .lift_subqueries import lift_subqueries``). They are
#: registered up front, because importing a submodule later would rebind the package attribute to the module.
SHADOWED = ("lift_subqueries",)


def install() -> None:
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
    for name in SHADOWED:
        sys.modules.setdefault(_PREFIX + name, importlib.import_module(_TARGET + name))
