"""Algebraic normalization for bag-semantics equivalence proofs.

SQLSolver (SIGMOD 2024) reasons about queries as arithmetic over tuple
multiplicities: under bag semantics ``UNION ALL`` is addition, a join is
multiplication, and a filter or projection is linear. Addition distributes
over all three, and aggregates over an addition split into per-branch partial
aggregates combined arithmetically (``COUNT(A + B) = COUNT(A) + COUNT(B)``).

This module applies those identities as sound AST rewrites, so queries that
differ only by where a ``UNION ALL`` sits reach the same normal form. The SMT
prover then compares normal forms instead of raw syntax. Rules:

* **distribute**: a plain select-project-join over inner/cross joins whose
  FROM contains a derived ``UNION ALL`` becomes a ``UNION ALL`` with one copy
  of the select per branch (nested unions are flattened first);
* **split aggregates**: ``COUNT``, ``SUM``, ``MIN`` and ``MAX`` over a derived
  ``UNION ALL`` become the same function over per-branch partial aggregates
  (``COUNT`` is combined with ``SUM``; ``SUM`` of all-NULL partials stays NULL,
  which matches ``SUM`` of no rows). ``AVG``, ``DISTINCT`` aggregates and
  ``HAVING`` on unsplit expressions are left alone.

``UNION DISTINCT``, outer/semi joins, ``LIMIT``, ``ORDER BY`` and window
functions are never touched. Normalization only rewrites; it proves nothing.
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import itertools
import re

import sqlglot
from sqlglot import exp
from . import proof_columns
from .ast_utils import FROM_KEY, UnmodeledConstruct, canonical_negation, check_modeled, distinct_on, drop_case_conflicts, expand_alias_columns, extended_grouping, faithful_sql, free_reads, parenthesize_is_operands, plain_distinct, same_table, select_sources as _sources_of, star_modified, strip_positions, visible_ctes
from .set_operations import positional_sql_pair
from .literal_fold_rules import distribute_over_constant_union, fold_string_literals
from .parse_check import refuse_misread_proofs
from .solver_lock import serialized
from .string_literals import canonical_literals, invalid_literal
from .type_names import invalid_type_name
from .sqlx_fragments import masked_template_problem

from .eager_aggregation import flatten_grouped_join, pull_up_aggregate, unnest_grouped_source
from .eager_sum_readback import read_back_eager_sums
from .fk_rules import drop_fk_join
from .exists_constant_rules import exists_constant_rules
from .decorrelation_rules import decorrelation_step
from .constraint_normalization import keyed_join_to_exists, normalize_key_counts
from .grouping_expansion import collapse_grouping_expansion
from .grouping_sets import expand_grouping_sets, grouping_sets_to_union
from .named_windows import inline_named_windows
from .having_rules import key_having_to_where
from .window_canonical import canonical_windows
from .unread_windows import drop_unread_windows
from .window_rules import window_rules
from .intersection_rules import collapse_counted_intersection, collapse_named_counted_intersection
from .count_case_rules import fold_grouped_count_cases
from .like_rules import drop_subsumed_like
from .row_bound_rules import trim_redundant_row_clauses
from .singleton_aggregate_rules import singleton_count_sum
from .set_identity import expose_set_identity
from .projected_in_rules import normalize_projected_in
from .limit_rules import drops_global_aggregate, limit_rule as _limit_rule, rebinds_grouping_names
from .using_rules import using_to_on_unqualified
from .cast_rules import fold_casts_and_constant_cases
from .int_div_rules import fold_literal_int_div
from .date_ranges import extract_to_ranges
from .dedup_join_rules import drop_unread_outer_join, strip_distinct_sources
from .distinct_rules import distinct_rules
from . import set_aggregates
from .outer_join_flatten import outer_join_rules
from .grouped_outer_joins import grouped_outer_join_rules
from .set_split_rules import split_distinct_select
from .empty_rules import canonical_empty, propagate_empty
from .setop_rules import _sf_flatten as _flatten_projections, merge_same_source, normalize_set_operations, set_operation_to_exists
from .outer_join_exists import move_exists_into_padded_side
from .outer_filters import strengthen_derived_outer_join
from .grouped_sums import drop_grouped_sum_coalesce
from .sum_of_counts import sum_of_grouped_counts
from .lone_source import lift_derived_expressions
from .partition_rules import recombine_partitions
from .distinct_partition_rules import merge_distinct_partitions
from .keyed_rules import drop_keyed_distinct, exists_over_aggregate, remove_keyed_grouping
from .keyed_set_join import lift_keyed_set_join
from .aggregate_rules import rewrite_aggregates
from .null_facts import null_contradiction
from .null_rejecting_joins import left_join_to_inner
from .outer_on_rejection import strengthen_under_outer_on
from .join_rewrites import join_rewrites
from .quantified_rules import rewrite_quantified
from .regroup_arithmetic import regroup_arithmetic
from .tuple_count_rules import regroup_tuple_count
from .union_filter_rules import push_filter_into_set_operation
from .set_operation_types import ASSUMPTION as SET_TYPES_ASSUMPTION, mixed_types, unchecked_types
from .grouped_join_facts import propagate_grouped_join_facts
from .correlated_key_groups import expose_correlated_key_groups
from .lateral_boolean_groups import nullable_lateral_boolean_group
from . import numeric_column_reading, string_number_compare, string_number_literals
from .constant_correlation import propagate_constant_correlations
from .constant_regroup_rules import collapse_constant_regroup
from .bigquery_constructs import rewrite_bigquery_constructs
from .boolean_identities import fold_boolean_identities
from .membership_dedup import relax_membership_dedup
from .smt_equivalence import SmtEquivalenceResult, SmtStatus, prove_equivalent_smt

MAX_BRANCHES = 16
_SPLIT_ALIAS = "kumosql_u"

_COMBINE = {exp.Count: "SUM", exp.Sum: "SUM", exp.Min: "MIN", exp.Max: "MAX"}


def _aligned_branches(source: exp.Subquery) -> list[exp.Select] | None:
    """Branches of a derived UNION ALL, each naming its columns like the first.

    A union's column names come from its first branch, so a copy of the
    enclosing select that keeps only one branch must give that branch's
    columns those names, or ``u.k`` could bind to a different column.
    """

    branches = _union_all_branches(source)
    if branches is None:
        return None
    first = branches[0]
    names = [item.alias_or_name for item in first.expressions]
    if "" in names or len({n.lower() for n in names}) != len(names):
        return None
    aligned = []
    for branch in branches:
        if len(branch.expressions) != len(names) or any(isinstance(i, exp.Star) for i in branch.expressions):
            return None
        copy = branch.copy()
        copy.set(
            "expressions",
            [
                i.copy() if i.alias_or_name == name else exp.alias_((i.this if isinstance(i, exp.Alias) else i).copy(), name)
                for i, name in zip(copy.expressions, names)
            ],
        )
        aligned.append(copy)
    return aligned


def _plain_sources(select: exp.Select) -> list[exp.Subquery] | None:
    """The union-valued derived tables in FROM/JOIN, if every join is inner."""

    sources = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    relations = [from_.this]
    for join in select.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") in {"SEMI", "ANTI", "LEFT", "RIGHT", "FULL"}:
            return None
        if join.args.get("using") is not None or join.args.get("method") or join.args.get("global_"):
            return None
        relations.append(join.this)
    for relation in relations:
        if isinstance(relation, exp.Subquery) and _union_all_branches(relation) is not None:
            if isinstance(relation.this, exp.Union):
                if not relation.alias:
                    return None
                sources.append(relation)
    return sources


def _no_extras(select: exp.Select, *, allow_group: bool) -> bool:
    banned = ["distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with"]
    if not allow_group:
        banned += ["group", "having"]
    if any(select.args.get(k) for k in banned):
        return False
    return not any(select.find_all(exp.Window))


def _global_aggregate(select: exp.Select) -> bool:
    """``select`` aggregates without a GROUP BY: it returns one row even over no input.

    Pruning its outputs must keep one of its aggregates, or it becomes a plain select with a row per input row.
    """

    return not select.args.get("group") and any(
        a.find_ancestor(exp.Select) is select and not isinstance(a.parent, exp.Window) for a in select.find_all(exp.AggFunc)
    )


def _where_false_is_empty(select: exp.Select) -> bool:
    """``select`` has ``WHERE FALSE`` and so no rows: not with an aggregate, which still returns its one row
    over no input, nor with a GROUP BY (an empty grouping set does the same)."""

    where = select.args.get("where")
    return (
        where is not None
        and isinstance(where.this, exp.Boolean)
        and not where.this.this
        and not select.args.get("group")
        and not any(select.find_all(exp.AggFunc))
    )


def _is_agg(node: exp.Expression) -> bool:
    return isinstance(node, tuple(_COMBINE)) and not node.args.get("distinct") and not isinstance(
        node.this, exp.Distinct
    )


def _is_constant(node: exp.Expression) -> bool:
    """A NULL, boolean or number: a grouped select outputs it unchanged whatever its groups hold."""

    return isinstance(node, (exp.Null, exp.Boolean)) or isinstance(node, exp.Literal) and not node.is_string


def _branch_copy(select: exp.Select, source: exp.Subquery, branch: exp.Expression) -> exp.Select:
    copy = select.copy()
    for subquery in copy.find_all(exp.Subquery):
        if subquery.alias == source.alias and subquery.this.sql() == source.this.sql():
            replacement = exp.Subquery(this=branch.copy(), alias=subquery.args.get("alias"))
            subquery.replace(replacement)
            break
    return copy


def _distribute(select: exp.Select) -> exp.Expression | None:
    # aggregates inside a source (a grouped branch) are computed per branch either way
    if not _no_extras(select, allow_group=False) or any(c.find_ancestor(exp.Select) is select for c in select.find_all(exp.AggFunc)):
        return None
    sources = _plain_sources(select)
    if not sources:
        return None
    branch_lists = [_aligned_branches(s) for s in sources]
    if any(branches is None for branches in branch_lists):
        return None
    total = 1
    for branches in branch_lists:
        total *= len(branches)
    if total > MAX_BRANCHES:
        return None
    copies: list[exp.Select] = []
    for combination in itertools.product(*branch_lists):
        copy = select.copy()
        for source, branch in zip(sources, combination):
            copy = _branch_copy(copy, source, branch)
        copies.append(copy)
    result: exp.Expression = copies[0]
    for copy in copies[1:]:
        result = exp.Union(this=result, expression=copy, distinct=False)
    return result


def _split_aggregates(select: exp.Select) -> exp.Expression | None:
    if not _no_extras(select, allow_group=True) or select.args.get("having"):
        return None
    sources = _plain_sources(select)
    if not sources or len(sources) != 1:
        return None
    source = sources[0]
    if source.alias == _SPLIT_ALIAS:
        return None  # already the output of this rule
    from_ = select.args.get("from_") or select.args.get("from")
    if select.args.get("joins") or from_.this is not source:
        return None
    branches = _aligned_branches(source)
    if branches is None or len(branches) > MAX_BRANCHES:
        return None
    group = select.args.get("group")
    keys = list(group.expressions) if group else []
    if any(not isinstance(k, exp.Column) for k in keys):
        return None
    key_sql = {k.sql() for k in keys}

    partial_items: list[exp.Expression] = []
    outer_items: list[exp.Expression] = []
    count = 0
    for item in select.expressions:
        inner = item.this if isinstance(item, exp.Alias) else item
        alias = item.alias if isinstance(item, exp.Alias) else (item.output_name if isinstance(item, exp.Column) else "")
        if isinstance(inner, exp.Column) and inner.sql() in key_sql:
            partial_items.append(exp.alias_(inner.copy(), alias or inner.name))
            outer_items.append(exp.column(alias or inner.name))
        elif _is_agg(inner):
            if any(isinstance(n, exp.Select) for n in inner.walk()) or any(
                _is_agg(n) for n in inner.this.walk() if n is not inner
            ):
                return None
            name = f"kumosql_p{count}"
            count += 1
            partial_items.append(exp.alias_(inner.copy(), name))
            combine = _COMBINE[type(inner)]
            combined = exp.func(combine, exp.column(name))
            outer_items.append(exp.alias_(combined, alias) if alias else combined)
        elif _is_constant(inner):
            outer_items.append(item.copy())  # a constant (a grouping set's missing key) reads the same outside
        else:
            return None
    if not any(_is_agg(i.this if isinstance(i, exp.Alias) else i) for i in select.expressions):
        return None
    projected = {i.this.sql() for i in partial_items if not _is_agg(i.this)}
    for n, key in enumerate(k for k in keys if k.sql() not in projected):
        # a grouping key the select does not show still splits groups: carry it hidden
        partial_items.append(exp.alias_(key.copy(), f"kumosql_g{n}"))

    partials: list[exp.Select] = []
    for branch in branches:
        partial = select.copy()
        partial.set("expressions", [i.copy() for i in partial_items])
        partials.append(_branch_copy(partial, source, branch))
    union: exp.Expression = partials[0]
    for partial in partials[1:]:
        union = exp.Union(this=union, expression=partial, distinct=False)
    outer = exp.select(*outer_items).from_(exp.Subquery(this=union, alias=exp.TableAlias(this=exp.to_identifier(_SPLIT_ALIAS))))
    if keys:
        outer = outer.group_by(*[exp.column(i.alias) for i in partial_items if not _is_agg(i.this)])
    return outer


def _collapse_aggregate(select: exp.Select) -> exp.Expression | None:
    """Drop a redundant regrouping of an already-grouped subquery.

    ``SELECT k, SUM(p) AS n FROM (SELECT k, COUNT(*) AS p FROM t GROUP BY k)
    GROUP BY k`` is the inner query: every group of the outer select holds one
    inner row, so SUM, MIN or MAX of its one value returns that value (SUM of a
    COUNT included). COUNT of it is not: it counts that one row (1, or 0 for NULL). A global inner
    aggregate has exactly one row, so the same holds with no keys at all.
    """

    if not _no_extras(select, allow_group=True) or select.args.get("having"):
        return None
    if select.args.get("where") or select.args.get("joins"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _no_extras(inner, allow_group=True):
        return None
    inner_keys = list(inner.args["group"].expressions) if inner.args.get("group") else []
    if any(not isinstance(k, exp.Column) for k in inner_keys):
        return None
    inner_key_sql = {k.sql() for k in inner_keys}
    key_outputs: dict[str, exp.Expression] = {}
    agg_outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias_or_name
        if not name:
            return None
        if _is_agg(expr):
            agg_outputs[name] = expr
        elif isinstance(expr, exp.Column) and expr.sql() in inner_key_sql:
            key_outputs[name] = expr
        else:
            return None
    outer_keys = list(select.args["group"].expressions) if select.args.get("group") else []
    if any(not isinstance(k, exp.Column) or k.name not in key_outputs for k in outer_keys):
        return None
    if {key_outputs[k.name].sql() for k in outer_keys} != inner_key_sql:
        return None

    items: list[exp.Expression] = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        if (
            isinstance(expr, (exp.Sum, exp.Min, exp.Max))
            and isinstance(expr.this, exp.Distinct)
            and len(expr.this.expressions) == 1
        ):
            # One inner row per group: DISTINCT changes nothing.
            expr = type(expr)(this=expr.this.expressions[0].copy())
        if isinstance(expr, exp.Column) and expr.name in key_outputs:
            items.append(_named(key_outputs[expr.name].copy(), name))
        elif isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and isinstance(expr.this, exp.Column) and expr.this.name in key_outputs:
            items.append(_named(key_outputs[expr.this.name].copy(), name))
        elif _is_agg(expr) and isinstance(expr.this, exp.Column) and expr.this.name in agg_outputs:
            inner_agg = agg_outputs[expr.this.name]
            # SUM of a SUM or COUNT, MIN of a MIN, MAX of a MAX is the one value; COUNT of it is 1 or 0
            ok = isinstance(inner_agg, (exp.Sum, exp.Count)) if isinstance(expr, exp.Sum) else type(expr) is type(inner_agg) and not isinstance(expr, exp.Count)
            if not ok:
                return None
            items.append(_named(inner_agg.copy(), name))
        else:
            return None
    result = inner.copy()
    result.set("expressions", items)
    return result


def _mean_times_count(select: exp.Select) -> exp.Expression | None:
    """Over ``(SELECT .., AVG(x) AS a, COUNT(x) AS n .. GROUP BY ..) AS m``, ``m.a * m.n`` is ``SUM(x)``.

    A group's mean times its count of non-NULL values is its sum (both read NULL when no value is
    present). The sum is added to the derived table and the product replaced by it, so a weighted
    average of per-group averages meets the plain ``SUM``/``COUNT`` form.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or not source.alias:
        return None
    inner = source.this
    if not inner.args.get("group") or not _no_extras(inner, allow_group=True):
        return None
    means: dict[str, exp.Expression] = {}
    counts: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = (item.alias_or_name or "").lower()
        if not name or isinstance(expr.this if hasattr(expr, "this") else None, exp.Distinct) or expr.args.get("distinct"):
            continue
        if isinstance(expr, exp.Avg):
            means[name] = expr.this
        elif isinstance(expr, exp.Count) and not isinstance(expr.this, exp.Star):
            counts[name] = expr.this
    alias = source.alias.lower()

    def column_name(node: exp.Expression) -> str | None:
        if isinstance(node, exp.Column) and (not node.table or node.table.lower() == alias):
            return node.name.lower()
        return None

    new_items: list[exp.Expression] = []
    changed = False
    result = select.copy()
    inner_copy = result.args["from_" if result.args.get("from_") else "from"].this.this
    for product in list(result.find_all(exp.Mul)):
        if product.find_ancestor(exp.Select) is not result:
            continue
        left, right = column_name(product.this), column_name(product.expression)
        for mean, count in ((left, right), (right, left)):
            if mean in means and count in counts and means[mean].sql() == counts[count].sql() and isinstance(means[mean], exp.Column):
                total = f"kq_sum_{len(new_items)}"
                new_items.append(exp.alias_(exp.Sum(this=means[mean].copy()), total))
                product.replace(exp.column(total, table=source.alias))
                changed = True
                break
    if not changed:
        return None
    inner_copy.set("expressions", list(inner_copy.expressions) + new_items)
    return result


def _roll_up_aggregate(select: exp.Select) -> exp.Expression | None:
    """``SELECT SUM(s) FROM (SELECT k, SUM(x) AS s FROM t GROUP BY k)`` is ``SELECT SUM(x) FROM t``.

    A global aggregate over the groups of a grouped select combines their partial results:
    SUM of sums, MIN of minimums and MAX of maximums are the aggregate over all rows (NULL for
    no rows on both sides). ``SUM`` of ``COUNT`` is not, it reads NULL where ``COUNT`` reads 0.
    """

    if select.args.get("group") or select.args.get("where") or select.args.get("joins") or not _no_extras(select, allow_group=False):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not inner.args.get("group") or inner.args.get("having") or not _no_extras(inner, allow_group=True):
        return None
    parts: dict[str, exp.Expression] = {}
    counts: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        if item.alias_or_name and _is_agg(expr) and isinstance(expr, (exp.Sum, exp.Min, exp.Max)):
            parts[item.alias_or_name.lower()] = expr
        elif item.alias_or_name and isinstance(expr, exp.Count) and not isinstance(expr.this, exp.Distinct) and not expr.args.get("distinct"):
            counts[item.alias_or_name.lower()] = expr
    items = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        total = expr.this if isinstance(expr, exp.Coalesce) and isinstance(expr.this, exp.Sum) and len(expr.expressions) == 1 else None
        if (
            total is not None
            and isinstance(expr.expressions[0], exp.Literal)
            and expr.expressions[0].name == "0"
            and isinstance(total.this, exp.Column)
            and (not total.this.table or total.this.table.lower() == (source.alias or "").lower())
            and total.this.name.lower() in counts
        ):
            # COALESCE(SUM(per-group COUNT), 0) is the COUNT over all rows: 0 for no groups, as COUNT reads 0.
            name = item.alias_or_name
            items.append(exp.alias_(counts[total.this.name.lower()].copy(), name) if name else counts[total.this.name.lower()].copy())
            continue
        if not (isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and isinstance(expr.this, exp.Column) and not expr.this.table or isinstance(expr, (exp.Sum, exp.Min, exp.Max)) and isinstance(expr.this, exp.Column) and (expr.this.table or "").lower() == (source.alias or "").lower()):
            return None
        part = parts.get(expr.this.name.lower())
        if part is None or type(part) is not type(expr) or part.args.get("distinct") or isinstance(part.this, exp.Distinct):
            return None
        name = item.alias_or_name
        items.append(exp.alias_(part.copy(), name) if name else part.copy())
    if not items:
        return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("group", None)
    return result


def _named(expr: exp.Expression, name: str) -> exp.Expression:
    return exp.alias_(expr, name) if name else expr


def _regroup_distinct(select: exp.Select) -> exp.Expression | None:
    """Fold a regrouping that finishes a DISTINCT aggregate back into one grouped query.

    ``SELECT k, SUM(p), SUM(x) FROM (SELECT k, x, SUM(c) AS p FROM t GROUP BY k, x)
    GROUP BY k`` is ``SELECT k, SUM(c), SUM(DISTINCT x) FROM t GROUP BY k``: the
    inner query has one row per ``(k, x)``, so SUM or COUNT of ``x`` over those
    rows sees each value once (NULLs are ignored by both), and SUM, MIN or MAX of
    a partial result over the groups is that aggregate over all rows. A count that
    is summed (and possibly wrapped in ``COALESCE(.., 0)``) is the count: the
    groups of the outer query are never empty.
    """

    if not _no_extras(select, allow_group=True) or select.args.get("having"):
        return None
    if select.args.get("where") or select.args.get("joins") or not select.args.get("group"):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    source = from_.this if from_ else None
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _no_extras(inner, allow_group=True) or not inner.args.get("group") or inner.args.get("having"):
        return None
    inner_keys = list(inner.args["group"].expressions)
    if any(not isinstance(k, exp.Column) for k in inner_keys):
        return None
    inner_key_sql = {k.sql() for k in inner_keys}
    key_outputs: dict[str, exp.Expression] = {}
    agg_outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias_or_name
        if not name:
            return None
        if _is_agg(expr):
            agg_outputs[name] = expr
        elif isinstance(expr, exp.Column) and expr.sql() in inner_key_sql:
            key_outputs[name] = expr
        else:
            return None
    outer_keys = list(select.args["group"].expressions)
    if any(not isinstance(k, exp.Column) or k.name not in key_outputs for k in outer_keys):
        return None
    extra = inner_key_sql - {key_outputs[k.name].sql() for k in outer_keys}
    if not extra:
        return None
    extra_names = [n for n, e in key_outputs.items() if e.sql() in extra]
    if len(extra) == 1 and len(extra_names) != 1:
        return None
    # with several extra keys only partial aggregates and keys regroup; a DISTINCT aggregate needs exactly one
    extra_name = extra_names[0] if len(extra) == 1 else None

    items: list[exp.Expression] = []
    for item in select.expressions:
        expr = item.this if isinstance(item, exp.Alias) else item
        name = item.alias or item.output_name
        wrapped = False
        if isinstance(expr, exp.Coalesce) and len(expr.expressions) == 1 and expr.expressions[0].sql() == "0":
            expr, wrapped = expr.this, True
        if _is_constant(expr) and not wrapped:
            items.append(item.copy())
            continue
        if isinstance(expr, exp.Column) and not wrapped:
            if expr.name not in key_outputs or key_outputs[expr.name].sql() in extra:
                return None
            items.append(_named(key_outputs[expr.name].copy(), name))
            continue
        if isinstance(expr, exp.Avg) and isinstance(expr.this, exp.Column) and not wrapped:
            pass  # AVG of the extra key column only (below)
        elif not _is_agg(expr) or not isinstance(expr.this, exp.Column):
            return None
        column = expr.this.name
        if column == extra_name and not wrapped:
            x = key_outputs[extra_name].copy()
            if isinstance(expr, (exp.Sum, exp.Count, exp.Avg)):
                items.append(_named(type(expr)(this=exp.Distinct(expressions=[x])), name))
            elif isinstance(expr, (exp.Min, exp.Max)):
                items.append(_named(type(expr)(this=x), name))
            else:
                return None
        elif column in agg_outputs:
            partial = agg_outputs[column]
            if wrapped:
                if not (isinstance(expr, exp.Sum) and isinstance(partial, exp.Count)):
                    return None
            elif not (
                (isinstance(expr, exp.Sum) and isinstance(partial, (exp.Sum, exp.Count)))
                or (isinstance(expr, (exp.Min, exp.Max)) and type(partial) is type(expr))
            ):
                return None
            items.append(_named(partial.copy(), name))
        else:
            return None
    result = inner.copy()
    result.set("expressions", items)
    result.set("group", exp.Group(expressions=[key_outputs[k.name].copy() for k in outer_keys]))
    return result


def _key_aggregates(select: exp.Select) -> exp.Expression | None:
    """``MIN(k)``, ``MAX(k)`` and ``SUM(DISTINCT k)`` of a group key are the key itself.

    Every row of a group holds the same ``k`` (NULLs form one group and the
    aggregates ignore them, so a group of NULLs gives NULL, the key).
    """

    group = select.args.get("group")
    if not group or any(select.args.get(k) for k in ("having", "qualify")):
        return None
    keys = {k.sql() for k in group.expressions if isinstance(k, exp.Column)}
    changed = False
    for node in list(select.find_all(exp.Sum, exp.Min, exp.Max)):
        if node.find_ancestor(exp.Select) is not select:
            continue
        arg = node.this
        distinct = isinstance(arg, exp.Distinct) and len(arg.expressions) == 1
        column = arg.expressions[0] if distinct else arg
        if not isinstance(column, exp.Column) or column.sql() not in keys:
            continue
        if isinstance(node, exp.Sum) and not distinct:
            continue
        node.replace(column.copy())
        changed = True
    return select if changed else None


_DATE_TEXT = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:\s*(?:[+-]\d{2}(?::?\d{2})?|Z))?\s*$")


def _date_of(node: exp.Expression) -> datetime.date | None:
    """The date a constant date expression denotes (a time-zone suffix does not change a DATE)."""

    if isinstance(node, (exp.TsOrDsToDate, exp.Cast)) and isinstance(node.this, exp.Literal) and node.this.is_string:
        if isinstance(node, exp.Cast) and not node.args["to"].is_type(exp.DataType.Type.DATE):
            return None
        match = _DATE_TEXT.match(node.this.this)
        if match:
            try:
                return datetime.date(*(int(g) for g in match.groups()))
            except ValueError:
                return None
    return None


def _shift(day: datetime.date, count: int, unit: str) -> datetime.date | None:
    if unit == "DAY":
        return day + datetime.timedelta(days=count)
    if unit == "WEEK":
        return day + datetime.timedelta(weeks=count)
    if unit in ("MONTH", "YEAR"):
        months = day.year * 12 + day.month - 1 + count * (12 if unit == "YEAR" else 1)
        year, month = divmod(months, 12)
        if not 1 <= year <= 9999:
            return None
        return datetime.date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))
    return None


def _fold_count_coalesce(tree: exp.Expression) -> exp.Expression:
    """``COALESCE(COUNT(..), 0)`` is the count: a count is never NULL."""

    def step(node: exp.Expression) -> exp.Expression:
        if (
            isinstance(node, exp.Coalesce)
            and isinstance(node.this, exp.Count)
            and len(node.expressions) == 1
            and node.expressions[0].sql() == "0"
        ):
            return node.this
        return node

    return tree.transform(step)


def _fold_dates(tree: exp.Expression) -> exp.Expression:
    """Constant dates become ``CAST('YYYY-MM-DD' AS DATE)``, including ``DATE('..' )`` with a
    time-zone suffix and ``date + INTERVAL n DAY/MONTH/YEAR``, so equal dates compare equal."""

    def literal(day: datetime.date) -> exp.Expression:
        return exp.Cast(this=exp.Literal.string(day.isoformat()), to=exp.DataType.build("DATE"))

    def step(node: exp.Expression) -> exp.Expression:
        day = _date_of(node)
        if day is not None:
            return literal(day)
        sign = 1
        if isinstance(node, (exp.Add, exp.Sub)):
            if isinstance(node, exp.Sub):
                sign = -1
            base, interval = node.this, node.expression
            if isinstance(interval, exp.Interval) and isinstance(interval.this, exp.Literal):
                count, unit = interval.this.this, interval.unit.name.upper() if interval.unit else ""
            else:
                return node
        elif isinstance(node, (exp.DateAdd, exp.DateSub)):
            if isinstance(node, exp.DateSub):
                sign = -1
            base, amount = node.this, node.expression
            if not isinstance(amount, exp.Literal):
                return node
            count, unit = amount.this, (node.args.get("unit").name.upper() if node.args.get("unit") else "")
        else:
            return node
        base_day = _date_of(base)
        if base_day is None or not re.fullmatch(r"-?\d+", str(count).strip()):
            return node
        shifted = _shift(base_day, sign * int(count), unit)
        return literal(shifted) if shifted is not None else node

    return tree.transform(step)


def _plain_union(node: exp.Expression) -> bool:
    """A bare ``UNION [ALL]`` of its two operands: no LIMIT or OFFSET over the whole union, no ``BY NAME``,
    side, CTE or other clause. Only then is it the sum (or set) of its operands' rows; an ORDER BY alone
    keeps every row."""

    if type(node) is not exp.Union:
        return False
    extra = {key for key, value in node.args.items() if value and key not in ("this", "expression", "distinct")}
    return not extra or extra == {"order"}


def _union_all_branches(node: exp.Expression) -> list[exp.Select] | None:
    """The SELECTs of a (nested) UNION ALL, or ``None`` for any other shape."""

    if isinstance(node, exp.Subquery):
        return _union_all_branches(node.this)
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.Union) and not node.args.get("distinct") and not any(
        node.args.get(k) for k in ("order", "limit", "offset")
    ):
        left, right = _union_all_branches(node.this), _union_all_branches(node.expression)
        return None if left is None or right is None else left + right
    return None


def _prune_union_all(union: exp.Union, used: set[str]) -> bool:
    """Drop, in every branch of a UNION ALL, the columns (by position) nothing reads."""

    branches = _union_all_branches(union)
    if not branches:
        return False
    first = branches[0].expressions
    width = len(first)
    for branch in branches:
        if len(branch.expressions) != width or not _no_extras(branch, allow_group=True) or branch.args.get("distinct"):
            return False
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in branch.expressions):
            return False
    if any(not e.alias_or_name for e in first):
        return False
    names = [e.alias_or_name.lower() for e in first]
    if len(set(names)) != width:
        return False
    keep = [i for i in range(width) if names[i] in used] or [0]
    keep.sort(key=lambda i: names[i])  # the enclosing query reads by name, so the order is free
    if keep == list(range(width)):
        return False
    pruned = [[branch.expressions[i].copy() for i in keep] for branch in branches]
    for branch, items in zip(branches, pruned):
        if _global_aggregate(branch):
            kept = branch.copy()
            kept.set("expressions", [item.copy() for item in items])
            if not _global_aggregate(kept):
                return False  # its only aggregate would go, and its one row would become a row per input row
    for branch, items in zip(branches, pruned):
        branch.set("expressions", items)
    return True


def _null_extended(select: exp.Select, source: exp.Expression) -> bool:
    """Whether ``source`` (the FROM item or a joined item of ``select``) can be NULL-extended by an outer join.

    Its own LEFT or FULL join pads it, and so does every later RIGHT or FULL join, which pads all
    that was joined before it (``t JOIN d ON .. RIGHT JOIN b ON FALSE`` gives rows where ``d`` is
    NULL). A source that is not one of the select's own items, or that carries joins of its own
    (a parenthesized join), counts as padded.
    """

    joins = select.args.get("joins") or []
    sides = [(j.args.get("side") or "").upper() for j in joins]
    kinds = [(j.args.get("kind") or "").upper() for j in joins]
    if source.args.get("joins"):
        return True
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None and from_.this is source:
        later = 0
    else:
        position = next((i for i, j in enumerate(joins) if j.this is source), None)
        if position is None or sides[position] in ("LEFT", "FULL") or kinds[position] in ("LEFT", "FULL"):
            return True
        later = position + 1
    return any(side in ("RIGHT", "FULL") or kind in ("RIGHT", "FULL") for side, kind in zip(sides[later:], kinds[later:]))


_STRICT = (exp.Paren, exp.Neg, exp.Cast, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.IntDiv, exp.Mod, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _null_propagating(node: exp.Expression) -> bool:
    """Whether ``node`` is NULL whenever every column it reads is NULL (constants and IS NULL are not)."""

    if isinstance(node, exp.Column):
        return True
    if isinstance(node, (exp.Paren, exp.Neg, exp.Cast)) and not isinstance(node, exp.TryCast):
        return _null_propagating(node.this)
    if isinstance(node, exp.Binary) and isinstance(node, _STRICT):
        return _null_propagating(node.this) or _null_propagating(node.expression)
    if isinstance(node, exp.Null):
        return True
    # every branch that can be taken gives NULL; a condition that is NULL itself is never taken
    if isinstance(node, exp.Case) and not node.args.get("this"):
        results = [i.args.get("true") for i in node.args.get("ifs") or [] if not _null_propagating(i.this)]
        return all(r is not None and _null_propagating(r) for r in results + [node.args.get("default") or exp.null()])
    if isinstance(node, exp.If):
        taken = [node.args.get("false") or exp.null()] + ([] if _null_propagating(node.this) else [node.args.get("true")])
        return all(r is not None and _null_propagating(r) for r in taken)
    if isinstance(node, exp.Coalesce):
        return all(_null_propagating(a) for a in [node.this, *node.expressions])
    if isinstance(node, exp.Nullif):
        return _null_propagating(node.this)
    return False


def _inline_expression_projection(select: exp.Select, schema: dict[str, list[str]] | None = None) -> exp.Expression | None:
    """``(SELECT f(a) AS x FROM t) AS d`` joined in: read ``t AS d`` and replace ``d.x`` by ``f(d.a)``.

    A derived table that only computes expressions over one table keeps every row, so it can be
    folded into the query that uses it. On the NULL-extended side of an outer join a missing match
    gives ``d.x`` NULL, while ``f(d.a)`` gives ``f(NULL)``; there only expressions that are NULL
    whenever their columns are (columns, arithmetic, comparisons, a ``CASE`` whose reachable
    branches are) are folded, never constants, ``IS NULL`` or a ``CASE`` that can give a constant.
    """

    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    changed = False
    for source in _sources_of(select):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=False) or inner.args.get("where") or inner.args.get("joins"):
            continue
        from_ = inner.args.get("from_") or inner.args.get("from")
        table = from_.this if from_ is not None else None
        if not isinstance(table, exp.Table) or any(
            isinstance(n, (exp.AggFunc, exp.Subquery, exp.Select, exp.Star, exp.Window)) for e in inner.expressions for n in e.walk()
        ):
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        if "" in names or len(set(names)) != len(names):
            continue
        # Plain passthrough projections are handled (and kept) by _inline_projection.
        if all(isinstance(e, exp.Column) and e.alias_or_name.lower() == e.name.lower() for e in inner.expressions):
            continue
        alias = source.alias
        qualifier = table.alias_or_name
        # Columns of the new relation are the table's columns, so every use of ``d.x`` is replaced.
        by_name = {n: (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, inner.expressions)}
        if _null_extended(select, source) and not all(_null_propagating(e) for e in by_name.values()):
            continue
        # the bodies of derived tables name their own sources' columns, not this derived table's
        bodies = [s for s in _sources_of(select) if isinstance(s, exp.Subquery)]
        own = [c for c in select.find_all(exp.Column) if not any(_within(c, body) for body in bodies)]
        uses = [c for c in own if c.table.lower() == alias.lower()]
        if any(c.name.lower() not in by_name for c in uses):
            continue
        unqualified = [c for c in own if not c.table and c.name.lower() in by_name]
        if unqualified:
            continue
        # Alias clashes with another declared name would capture references.
        if sum(1 for n in select.walk() if isinstance(n, (exp.Table, exp.Subquery)) and (n.alias_or_name or "").lower() == alias.lower()) != 1:
            continue
        # the table's other columns become visible where only the derived outputs were
        if _may_capture(select, source, table, set(), schema):
            continue
        for column in uses:
            replacement = by_name[column.name.lower()].copy()
            for inner_column in replacement.find_all(exp.Column):
                if not inner_column.table or inner_column.table.lower() == qualifier.lower():
                    inner_column.set("table", exp.to_identifier(alias))
            bare = isinstance(column.parent, (exp.Func, exp.Alias, exp.Paren, exp.Ordered, exp.Window, exp.Tuple))
            value = exp.Paren(this=replacement) if isinstance(replacement, exp.Binary) and not bare else replacement
            # a select-list item keeps its output name once the column is replaced by a value or a
            # differently named column (``d.x`` becoming ``d.a`` would rename the output to ``a``)
            renamed = not isinstance(replacement, exp.Column) or replacement.name.lower() != column.name.lower()
            column.replace(exp.alias_(value, column.name) if column.parent is select and column.arg_key == "expressions" and renamed else value)
        replaced = exp.Table(this=table.this.copy(), db=table.args.get("db"), catalog=table.args.get("catalog"), alias=exp.TableAlias(this=exp.to_identifier(alias)))
        source.replace(replaced)
        changed = True
    return select if changed else None


_merge_counter = itertools.count()
_OUTER = {"SEMI", "ANTI", "LEFT", "RIGHT", "FULL"}


def _merge_spj_source(select: exp.Select) -> exp.Expression | None:
    """Fold a filtering or joining derived table into the grouped select that reads it.

    ``SELECT SUM(x) FROM (SELECT k, x FROM t WHERE c) AS d GROUP BY k`` is
    ``SELECT SUM(x) FROM t WHERE c GROUP BY k``: the derived table keeps one row per row of its
    own sources, so the aggregate sees the same rows. Only selects that group or aggregate are
    rewritten (the prover flattens the others itself), and only through inner joins.
    """

    from .eager_aggregation import _own_aggregates

    if not (select.args.get("group") or _own_aggregates(select)) or not _no_extras(select, allow_group=True):
        return None
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    select = select.copy()
    items = _sources_of(select)
    for join in select.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") in _OUTER or join.args.get("using") is not None:
            return None
        if join.args.get("method") or join.args.get("global_"):
            return None
    # A subquery outside FROM could read the alias from outside; tests of a lone source are checked per source below.
    tests = [e for e in select.find_all(exp.Exists) if e.find_ancestor(exp.Select) is select]
    subs = [n for n in select.find_all(exp.Subquery) if n not in items and n.find_ancestor(exp.Select) is select]
    outside = [n for n in subs if not any(a in tests for a in _ancestors_of(n, select))]
    if outside or (tests and (len(items) != 1 or any(not isinstance(e.parent, (exp.Where, exp.And)) for e in tests))):
        return None
    own = [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
    for position, source in enumerate(items):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=False):
            continue
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Star, exp.Subquery, exp.Select, exp.Exists)) for e in inner.expressions for n in e.walk()):
            continue
        inner_items = _sources_of(inner)
        if not inner_items or any(not (isinstance(i, (exp.Table, exp.Subquery)) and i.alias_or_name) for i in inner_items):
            continue
        if any(isinstance(i, exp.Table) and (i.args.get("joins") or i.args.get("pivots") or i.args.get("laterals")) for i in inner_items):
            continue
        inner_joins = inner.args.get("joins") or []
        if any(j.args.get("side") or j.args.get("kind") in _OUTER or j.args.get("using") is not None or j.args.get("method") for j in inner_joins):
            continue
        # EXISTS / IN tests of a lone inner table can move up when they name the inner table only by bare columns.
        lone = len(items) == 1 and len(inner_items) == 1 and not inner_joins
        inner_alias = (inner_items[0].alias_or_name or "").lower()
        conds = [inner.args.get("where")] + [j.args.get("on") for j in inner_joins]
        movable = set()
        for cond in conds:
            for n in cond.walk() if cond is not None else []:
                if (
                    lone
                    and isinstance(n, (exp.Exists, exp.In))
                    and all(c.table.lower() != inner_alias for c in n.find_all(exp.Column))
                    and not any(isinstance(t, exp.Subquery) and t.parent is not None and isinstance(t.parent, exp.In) and False for t in n.walk())
                ):
                    movable.add(id(n))
        if any(
            isinstance(n, (exp.Subquery, exp.Exists))
            and n not in inner_items
            and id(n) not in movable
            and not any(id(a) in movable for a in _ancestors_of(n, cond))
            and not (isinstance(n, exp.Subquery) and isinstance(n.parent, exp.In) and id(n.parent) in movable)
            for cond in conds
            if cond is not None
            for n in cond.walk()
        ):
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        inner_aliases = [i.alias_or_name.lower() for i in inner_items]
        if "" in names or len(set(names)) != len(names) or len(set(inner_aliases)) != len(inner_aliases):
            continue
        alias = source.alias.lower()
        uses = [c for c in own if c.table.lower() == alias]
        bare = [c for c in own if not c.table]
        if bare and (len(items) != 1 or select.args.get("order")):
            continue
        if any(c.name.lower() not in names for c in uses + bare):
            continue
        if len(inner_items) > 1 and any(not c.table for c in inner.find_all(exp.Column) if c.find_ancestor(exp.Select) is inner):
            continue
        if any(c.table and c.table.lower() not in inner_aliases for c in inner.find_all(exp.Column) if c.find_ancestor(exp.Select) is inner):
            continue
        mapping = {old: f"kumosql_m{next(_merge_counter)}_{old}" for old in inner_aliases}

        def rename(node: exp.Expression) -> exp.Expression:
            holder = exp.Select(expressions=[node.copy()])
            for column in list(holder.find_all(exp.Column)):
                if column.find_ancestor(exp.Select) is holder:
                    column.set("table", exp.to_identifier(mapping[column.table.lower() or inner_aliases[0]]))
            return holder.expressions[0]

        by_name_pre = {n: (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, inner.expressions)}
        if tests and any(
            c.table.lower() == alias or (not c.table and c.name.lower() in names and not (isinstance(by_name_pre.get(c.name.lower()), exp.Column) and by_name_pre[c.name.lower()].name.lower() == c.name.lower()))
            for e in tests
            for c in e.find_all(exp.Column)
        ):
            continue
        by_name = {n: (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, inner.expressions)}
        # A bare column in the select list keeps its output name once it is replaced.
        for index, item in enumerate(select.expressions):
            if item in uses or item in bare:
                named = exp.alias_(item.copy(), item.name)
                select.expressions[index].replace(named)
        own = [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
        uses = [c for c in own if c.table.lower() == alias]
        bare = [c for c in own if not c.table]
        for column in uses + bare:
            replacement = rename(by_name[column.name.lower()])
            column.replace(exp.Paren(this=replacement) if isinstance(replacement, exp.Binary) else replacement)
        conditions = []
        if inner.args.get("where") is not None:
            conditions.append(rename(inner.args["where"].this))
        for join in inner_joins:
            if join.args.get("on") is not None:
                conditions.append(rename(join.args["on"]))
        new_items = []
        for old, item in zip(inner_aliases, inner_items):
            item = item.copy()
            item.set("alias", exp.TableAlias(this=exp.to_identifier(mapping[old])))
            new_items.append(item)
        all_items: list[exp.Expression] = []
        for i, item in enumerate(items):
            all_items.extend(new_items if i == position else [item.copy()])
        for join in select.args.get("joins") or []:
            if join.args.get("on") is not None:
                conditions.append(join.args["on"].copy())
        if select.args.get("where") is not None:
            conditions.append(select.args["where"].this.copy())
        select.set(FROM_KEY, exp.From(this=all_items[0]))
        select.set("joins", [exp.Join(this=i) for i in all_items[1:]] or None)
        where = _and_all([part for condition in conditions for part in _conjuncts(condition)])
        select.set("where", exp.Where(this=where) if where is not None else None)
        return select
    return None


_decorrelate_counter = itertools.count()
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_SCALAR_NODES = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Paren, exp.Neg, exp.Literal)


def _decorrelate_aggregate(select: exp.Select, schema: dict[str, list[str]] | None) -> exp.Expression | None:
    """``x < (SELECT agg(e) FROM t WHERE t.k = outer.k AND c)`` read as a join with ``GROUP BY k``.

    A correlated scalar aggregate compared in WHERE is the join to one row per key: an outer row
    with no matching rows reads NULL, and a comparison with NULL rejects the row, so the inner
    join drops the same rows. ``COUNT`` is excluded (it reads 0, not NULL, for no rows).
    """

    if not schema or select.args.get("where") is None or any(select.find_all(exp.Window)):
        return None
    schema = {k.lower(): [c.lower() for c in v] for k, v in schema.items()}
    items = _sources_of(select)
    for join in select.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") in _OUTER or join.args.get("using") is not None:
            return None

    def table_columns(source: exp.Expression) -> list[str] | None:
        if isinstance(source, exp.Table) and not source.args.get("joins") and not source.args.get("pivots"):
            return schema.get(".".join(p.name for p in source.parts).lower())
        if isinstance(source, exp.Subquery):
            return _select_names(source.this)
        return None

    outer_known = [(i.alias_or_name.lower(), table_columns(i)) for i in items]
    if any(columns is None or not alias for alias, columns in outer_known):
        return None

    def outer_resolves(column: exp.Column) -> bool:
        if column.table:
            return any(alias == column.table.lower() and column.name.lower() in cols for alias, cols in outer_known)
        return sum(column.name.lower() in cols for _, cols in outer_known) == 1

    conditions = _conjuncts(select.args["where"].this)
    for position, condition in enumerate(conditions):
        if not isinstance(condition, _COMPARISONS):
            continue
        for side in ("this", "expression"):
            subquery = condition.args[side]
            if not isinstance(subquery, exp.Subquery) or not isinstance(subquery.this, exp.Select):
                continue
            built = _decorrelated(subquery.this, table_columns, outer_resolves)
            if built is None:
                continue
            derived, keys, value = built
            alias = f"kqd{next(_decorrelate_counter)}"
            other = condition.args["expression" if side == "this" else "this"]
            if any(isinstance(n, (exp.Subquery, exp.Select)) for n in other.walk()):
                continue
            joined = [exp.EQ(this=outer.copy(), expression=exp.column(f"kqk{i}", table=alias)) for i, (_, outer) in enumerate(keys)]
            comparison = type(condition)(
                this=value_column(alias) if side == "this" else other.copy(),
                expression=other.copy() if side == "this" else value_column(alias),
            )
            result = select.copy()
            new_conditions = [c.copy() for j, c in enumerate(conditions) if j != position] + joined + [comparison]
            result.set("where", exp.Where(this=_and_all(new_conditions)))
            derived_source = exp.Subquery(this=derived, alias=exp.TableAlias(this=exp.to_identifier(alias)))
            result.set("joins", list(result.args.get("joins") or []) + [exp.Join(this=derived_source)])
            return result
    return None


def _decorrelate_select_list(select: exp.Select, schema: dict[str, list[str]] | None) -> exp.Expression | None:
    """``SELECT k, (SELECT SUM(x) FROM t WHERE t.k = outer.k) FROM outer`` reads ``outer LEFT JOIN`` the grouped table.

    A correlated scalar aggregate (not ``COUNT``) with equality correlations is NULL for a row with
    no match and the group's value otherwise, which is what a left join to ``GROUP BY k`` gives.
    """

    if not schema or any(select.find_all(exp.Window)) or select.args.get("group") or select.args.get("having"):
        return None
    if not any(isinstance(n, exp.Subquery) and n.find_ancestor(exp.Select) is select for e in select.expressions for n in e.walk()):
        return None
    from .eager_aggregation import _own_aggregates

    if _own_aggregates(select) or select.args.get("distinct") is not None:
        return None
    schema = {k.lower(): [c.lower() for c in v] for k, v in schema.items()}
    items = _sources_of(select)
    if any(join.args.get("using") is not None for join in select.args.get("joins") or []):
        return None

    def table_columns(source: exp.Expression) -> list[str] | None:
        if isinstance(source, exp.Table) and not source.args.get("joins") and not source.args.get("pivots"):
            return schema.get(".".join(p.name for p in source.parts).lower())
        if isinstance(source, exp.Subquery):
            return _select_names(source.this)
        return None

    outer_known = [(i.alias_or_name.lower(), table_columns(i)) for i in items]
    if any(columns is None or not alias for alias, columns in outer_known):
        return None

    def outer_resolves(column: exp.Column) -> bool:
        if column.table:
            return any(alias == column.table.lower() and column.name.lower() in cols for alias, cols in outer_known)
        return sum(column.name.lower() in cols for _, cols in outer_known) == 1

    for position, item in enumerate(select.expressions):
        for subquery in item.find_all(exp.Subquery):
            if subquery.find_ancestor(exp.Select) is not select or not isinstance(subquery.this, exp.Select):
                continue
            if any(isinstance(a, (exp.Subquery,)) for a in _ancestors_of(subquery, item) if a is not subquery):
                continue
            built = _decorrelated(subquery.this, table_columns, outer_resolves)
            if built is None:
                continue
            derived, keys, _ = built
            alias = f"kqd{next(_decorrelate_counter)}"
            result = select.copy()
            target = result.expressions[position]
            for node in target.find_all(exp.Subquery):
                if node.sql() == subquery.sql():
                    node.replace(value_column(alias))
                    break
            on = _and_all([exp.EQ(this=outer.copy(), expression=exp.column(f"kqk{i}", table=alias)) for i, (_, outer) in enumerate(keys)])
            source = exp.Subquery(this=derived, alias=exp.TableAlias(this=exp.to_identifier(alias)))
            result.set("joins", list(result.args.get("joins") or []) + [exp.Join(this=source, side="LEFT", on=on)])
            return result
    return None


def value_column(alias: str) -> exp.Expression:
    return exp.column("kqv", table=alias)


def _decorrelated(inner: exp.Select, table_columns, outer_resolves):
    """``(derived select, [(inner key, outer column)], value expression)`` for a decorrelatable subquery."""

    if len(inner.expressions) != 1 or not _no_extras(inner, allow_group=False) or inner.args.get("limit"):
        return None
    sources = _sources_of(inner)
    if not sources or any(not isinstance(i, exp.Table) or not i.alias_or_name for i in sources):
        return None
    for join in inner.args.get("joins") or []:
        if join.args.get("side") or join.args.get("kind") in _OUTER or join.args.get("using") is not None or join.args.get("on") is not None:
            return None
    known = [(i.alias_or_name.lower(), table_columns(i)) for i in sources]
    if any(cols is None for _, cols in known) or len({a for a, _ in known}) != len(known):
        return None
    value = inner.expressions[0]
    value = value.this if isinstance(value, exp.Alias) else value
    aggregates = list(value.find_all(exp.AggFunc))
    if not aggregates or any(isinstance(a, exp.Count) for a in aggregates):
        return None
    for node in value.walk():
        if isinstance(node, exp.Column):
            if not any(isinstance(a, exp.AggFunc) for a in _ancestors_of(node, value)):
                return None
        elif not isinstance(node, _SCALAR_NODES + (exp.AggFunc,)) and not isinstance(node.parent, exp.AggFunc) and not _inside_aggregate(node, value):
            return None

    def side(expression: exp.Expression) -> str | None:
        """``inner`` / ``outer`` when every column of the expression is, ``None`` when mixed or empty."""

        kinds = set()
        for column in expression.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                return None
            if column.table:
                kinds.add("inner" if column.table.lower() in {a for a, _ in known} else "outer")
            else:
                hits = [a for a, cols in known if column.name.lower() in cols]
                kinds.add("inner" if len(hits) == 1 else "outer" if not hits else "?")
        if "?" in kinds or len(kinds) != 1:
            return None
        return kinds.pop()

    if any(isinstance(n, (exp.Subquery, exp.Exists)) for n in inner.walk() if n is not inner):
        return None
    local, keys = [], []
    where = inner.args.get("where")
    for condition in _conjuncts(where.this) if where is not None else []:
        columns = list(condition.find_all(exp.Column))
        kinds = {side(c) for c in columns}
        if kinds == {"inner"} or not columns:
            local.append(condition)
            continue
        if isinstance(condition, exp.EQ):
            a, b = side(condition.this), side(condition.expression)
            if (a, b) == ("outer", "inner"):
                condition = exp.EQ(this=condition.expression, expression=condition.this)
                a, b = b, a
            if (a, b) == ("inner", "outer") and isinstance(condition.expression, exp.Column) and outer_resolves(condition.expression):
                keys.append((condition.this, condition.expression))
                continue
        return None
    if not keys:
        return None
    # The value must not read an outer column.
    if any(side(c) != "inner" for c in value.find_all(exp.Column)):
        return None
    derived = exp.Select(
        expressions=[exp.alias_(inner_key.copy(), f"kqk{i}") for i, (inner_key, _) in enumerate(keys)] + [exp.alias_(value.copy(), "kqv")]
    )
    derived.set(FROM_KEY, exp.From(this=sources[0].copy()))
    derived.set("joins", [exp.Join(this=s.copy()) for s in sources[1:]] or None)
    if local:
        derived.set("where", exp.Where(this=_and_all([c.copy() for c in local])))
    derived.set("group", exp.Group(expressions=[k.copy() for k, _ in keys]))
    return derived, keys, value


def _ancestors_of(node: exp.Expression, stop: exp.Expression):
    node = node.parent
    while node is not None:
        yield node
        if node is stop:
            return
        node = node.parent


def _inside_aggregate(node: exp.Expression, root: exp.Expression) -> bool:
    return any(isinstance(a, exp.AggFunc) for a in _ancestors_of(node, root))


def _fold_filter_into_grouping(select: exp.Select) -> exp.Expression | None:
    """``SELECT s FROM (SELECT k, SUM(x) AS s FROM t GROUP BY k) WHERE s > 1`` is ``... HAVING SUM(x) > 1``.

    A select that only filters and projects the output of a grouped derived table reads the groups
    one by one, so its condition belongs in the grouped select's HAVING.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or not isinstance(from_.this, exp.Subquery):
        return None
    # Inside IN / EXISTS the grouped relation stays a derived table: the prover cannot read a HAVING there.
    if select.find_ancestor(exp.In, exp.Exists) is not None:
        return None
    source = from_.this
    inner = source.this
    if not isinstance(inner, exp.Select) or not (inner.args.get("group") or any(c.find_ancestor(exp.Select) is inner for c in inner.find_all(exp.AggFunc))):
        return None
    banned = ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")
    if any(select.args.get(k) for k in banned) or any(inner.args.get(k) for k in ("distinct", "limit", "offset", "qualify", "windows", "order")):
        return None
    if any(n is not source for n in select.find_all(exp.Subquery, exp.Exists)) or any(select.find_all(exp.Window)):
        return None
    if any(c.find_ancestor(exp.Select) is select for c in select.find_all(exp.AggFunc)) or any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
        return None
    alias = (source.alias or "").lower()
    names = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in names or isinstance(item, exp.Star):
            return None
        names[name] = item.this if isinstance(item, exp.Alias) else item
    columns = [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]
    if any(c.table and c.table.lower() != alias for c in columns) or any(c.name.lower() not in names for c in columns):
        return None
    if any(c.find_ancestor(exp.Window) for c in columns):
        return None

    def substitute(node: exp.Expression) -> exp.Expression:
        node = node.copy()
        holder = exp.Select(expressions=[node])
        for column in list(holder.find_all(exp.Column)):
            replacement = names[column.name.lower()].copy()
            if column is node:
                node = replacement
                holder.set("expressions", [node])
            else:
                column.replace(exp.Paren(this=replacement) if isinstance(replacement, exp.Binary) else replacement)
        return holder.expressions[0]

    result = inner.copy()
    outputs = []
    for item in select.expressions:
        expression = item.this if isinstance(item, exp.Alias) else item
        value = substitute(expression)
        name = item.alias_or_name
        outputs.append(exp.alias_(value, name) if name and not (isinstance(value, exp.Column) and value.name == name) else value)
    result.set("expressions", outputs)
    conditions = []
    if inner.args.get("having") is not None:
        conditions.append(inner.args["having"].this.copy())
    moved = [substitute(select.args["where"].this)] if select.args.get("where") is not None else []
    if rebinds_grouping_names(inner, outputs, moved):
        return None
    conditions += moved
    if conditions:
        result.set("having", exp.Having(this=_and_all([c for cond in conditions for c in _conjuncts(cond)])))
    if _global_aggregate(inner) and not _global_aggregate(result):
        return None  # the outer select drops every aggregate: the merged select would lose the one-row result
    return result


def _distinct_over_union_all(select: exp.Select) -> exp.Expression | None:
    """``SELECT DISTINCT a, b FROM (x UNION ALL y)`` is ``x UNION DISTINCT y``."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or not select.args.get("distinct") or select.args["distinct"].args.get("on"):
        return None
    if any(select.args.get(k) for k in ("where", "group", "having", "order", "limit", "offset", "qualify", "windows", "with_", "with")):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Union) or source.this.args.get("distinct"):
        return None
    union = source.this
    if not _plain_union(union):
        return None  # the rewrite keeps only the operands, so a LIMIT over the whole union would be lost
    names = _select_names(union)
    wanted = [e.name.lower() for e in select.expressions if isinstance(e, exp.Column) and not isinstance(e.this, exp.Star)]
    if names is None or wanted != names or len(wanted) != len(select.expressions):
        return None
    if any(c.table and c.table.lower() != (source.alias or "").lower() for e in select.expressions for c in e.find_all(exp.Column)):
        return None
    return exp.Union(this=union.this.copy(), expression=union.expression.copy(), distinct=True)


def _merge_outer_right_filter(select: exp.Select) -> exp.Expression | None:
    """``a LEFT JOIN (SELECT x, y FROM t WHERE w) AS d ON c`` is ``a LEFT JOIN t AS d ON c AND w``.

    A filter on the null-extended side of a left join belongs in its ON clause: rows of ``a``
    with no surviving match are padded either way. Only a derived table that projects plain
    columns of one table is read this way.
    """

    joins = select.args.get("joins") or []
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    for index, join in enumerate(joins):
        source = join.this
        if (join.args.get("side") or "").upper() != "LEFT" or join.args.get("kind") in ("SEMI", "ANTI") or join.args.get("on") is None:
            continue
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=False) or inner.args.get("joins") or inner.args.get("where") is None:
            continue
        from_ = inner.args.get("from_") or inner.args.get("from")
        table = from_.this if from_ is not None else None
        if not isinstance(table, exp.Table) or table.args.get("joins") or table.args.get("pivots") or table.args.get("laterals"):
            continue
        if not all(isinstance(e, exp.Column) and not isinstance(e.this, exp.Star) for e in inner.expressions):
            continue
        if any(isinstance(n, (exp.Subquery, exp.Exists, exp.Select)) for n in inner.args["where"].walk()):
            continue
        t_name = (table.alias_or_name or "").lower()
        if any(c.table and c.table.lower() != t_name for c in inner.find_all(exp.Column)):
            continue
        alias = source.alias
        condition = inner.args["where"].this.copy()
        for column in condition.find_all(exp.Column):
            column.set("table", exp.to_identifier(alias))
        merged = table.copy()
        merged.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        copy = select.copy()
        target = copy.args["joins"][index]
        target.set("this", merged)
        target.set("on", exp.And(this=exp.Paren(this=target.args["on"]) if isinstance(target.args["on"], exp.Or) else target.args["on"], expression=exp.Paren(this=condition) if isinstance(condition, exp.Or) else condition))
        return copy
    return None


def _flatten_join_source(select: exp.Select) -> exp.Expression | None:
    """``SELECT k, COUNT(x) FROM (SELECT k, x FROM a LEFT JOIN b ON c) AS d GROUP BY k`` reads the join directly.

    A derived table that only projects columns of a join (outer joins included) keeps each row of
    the join, so the grouped select above it can read the join itself.
    """

    from .eager_aggregation import _own_aggregates

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or not (select.args.get("group") or _own_aggregates(select)):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not _no_extras(inner, allow_group=False) or inner.args.get("where") is not None or not inner.args.get("joins"):
        return None
    if not all(isinstance(e, exp.Column) and not isinstance(e.this, exp.Star) for e in inner.expressions):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Exists)) for n in inner.walk() if n is not source.this and n.find_ancestor(exp.Select) is not None and n.find_ancestor(exp.Select) is inner):
        return None
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if len(set(names)) != len(names):
        return None
    inner_sources = _sources_of(inner)
    if any(not (isinstance(i, (exp.Table, exp.Subquery)) and i.alias_or_name) for i in inner_sources):
        return None
    if any(isinstance(i, exp.Table) and (i.args.get("pivots") or i.args.get("laterals") or i.args.get("joins")) for i in inner_sources):
        return None
    if any(j.args.get("using") is not None or j.args.get("method") for j in inner.args["joins"]):
        return None
    alias = source.alias.lower()
    if alias in {i.alias_or_name.lower() for i in inner_sources}:
        return None
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Exists)) and n is not source for n in select.walk() if n.find_ancestor(exp.Select) is select):
        return None
    by_name = {n: e for n, e in zip(names, inner.expressions)}
    copy = select.copy()
    for column in list(copy.find_all(exp.Column)):
        if column.find_ancestor(exp.Select) is not copy:
            continue
        if column.table and column.table.lower() != alias:
            return None
        origin = by_name.get(column.name.lower())
        if origin is None:
            return None
        column.replace(origin.copy() if not isinstance(origin, exp.Alias) else origin.this.copy())
    new_inner = inner.copy()
    copy.set(FROM_KEY, new_inner.args.get("from_") or new_inner.args.get("from"))
    copy.set("joins", new_inner.args.get("joins"))
    return copy


def _qualify_outer_join_columns(tree: exp.Expression, schema: dict[str, list[str]]) -> exp.Expression:
    """Write ``a.x`` for a bare ``x`` in a grouped select over an outer join when one source has ``x``.

    The rewrites that read an outer join as a unit need every column to name its source.
    """

    from .eager_aggregation import _own_aggregates

    schema = {key.lower(): [c.lower() for c in cols] for key, cols in schema.items()}
    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        if not joins:
            continue
        sources = _sources_of(select)
        owners: dict[str, list[str]] = {}
        complete = True
        for source in sources:
            alias = source.alias_or_name
            if isinstance(source, exp.Table):
                parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
                columns = schema.get(".".join(parts).lower())
            elif isinstance(source, exp.Subquery):
                columns = _select_names(source.this)
            else:
                columns = None
            if columns is None or not alias:
                complete = False
                break
            for column in columns:
                owners.setdefault(column, []).append(alias)
        if not complete:
            continue
        outputs = {e.alias.lower() for e in select.expressions if isinstance(e, exp.Alias)}
        for column in select.find_all(exp.Column):
            if column.table or isinstance(column.this, exp.Star) or column.find_ancestor(exp.Select) is not select:
                continue
            name = column.name.lower()
            if name in outputs or len(owners.get(name, [])) != 1:
                continue
            column.set("table", exp.to_identifier(owners[name][0]))
    return tree


def _checked_qualification(tree: exp.Expression, apply, schema: dict[str, list[str]], dialect: str, site: str) -> exp.Expression:
    """Run a pass that writes ``a.x`` for a bare ``x`` and have ``proof_columns`` re-derive each qualifier it added.

    The pass picks the owner from the columns of the sources it can see; the independent reader works from the
    statement's text and the same supplied columns. A qualifier that names a different owner (or a bare name with
    no single owner) declines the query, as an unmodeled construct does.
    """

    try:
        return proof_columns.guarded_qualification(tree, apply, schema, dialect, site)
    except proof_columns.ColumnResolutionRefused as refusal:
        raise UnmodeledConstruct(f"independent check of column resolution: {refusal}") from None


def _column_owners(select: exp.Select, schema: dict[str, list[str]]) -> dict[str, list[str]] | None:
    """Each column name readable in ``select``'s FROM and JOINs, with the sources that have it; None if one is unknown."""

    owners: dict[str, list[str]] = {}
    for source in _sources_of(select):
        alias = source.alias_or_name
        if isinstance(source, exp.Table):
            parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
            columns = schema.get(".".join(parts).lower())
        elif isinstance(source, exp.Subquery):
            columns = _select_names(source.this)
        else:
            columns = None
        if columns is None or not alias:
            return None
        for column in columns:
            owners.setdefault(column, []).append(alias)
    return owners


def _could_expose(source: exp.Expression, name: str, schema: dict[str, list[str]]) -> bool:
    """Whether reading derived table ``source`` as the tables inside it could bring a column ``name`` into scope."""

    if not isinstance(source, exp.Subquery):
        return False
    for table in source.find_all(exp.Table):
        parts = [p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None]
        columns = schema.get(".".join(parts).lower())
        if columns is None or name in columns:
            return True
    return False


def _qualify_correlated_columns(tree: exp.Expression, schema: dict[str, list[str]]) -> exp.Expression:
    """Write ``d.x`` for a bare ``x`` in a subquery that reads ``x`` from an enclosing query's source ``d``.

    Once a derived table inside the subquery is read as its base table, that table's own ``x`` would
    capture the bare name. A column is qualified only when a derived table on the way reads a table with
    a column of that name, every source on the way is known and exactly one enclosing source has it; a
    source on the way also called ``d`` makes the query unmodeled.
    """

    schema = {key.lower(): [c.lower() for c in cols] for key, cols in schema.items()}
    owners_of: dict[int, dict[str, list[str]] | None] = {}

    def owners(select: exp.Select) -> dict[str, list[str]] | None:
        if id(select) not in owners_of:
            owners_of[id(select)] = _column_owners(select, schema)
        return owners_of[id(select)]

    for column in list(tree.find_all(exp.Column)):
        if column.table or isinstance(column.this, exp.Star):
            continue
        name = column.name.lower()
        scope = column.find_ancestor(exp.Select)
        if scope is None or any(isinstance(e, exp.Alias) and e.alias.lower() == name for e in scope.expressions):
            continue
        passed: list[exp.Select] = []
        while scope is not None:
            found = owners(scope)
            if found is None:
                break
            if name in found:
                rivals = [source for p in passed for source in _sources_of(p)]
                rivals += [source for source in _sources_of(scope) if source.alias_or_name not in found[name]]
                if passed and len(found[name]) == 1 and any(_could_expose(source, name, schema) for source in rivals):
                    qualifier = found[name][0]
                    if any(s.alias_or_name.lower() == qualifier.lower() for p in passed for s in _sources_of(p)):
                        raise UnmodeledConstruct(f"a subquery reuses the name {qualifier} of the source its column {name} reads")
                    column.set("table", exp.to_identifier(qualifier))
                break
            if isinstance(scope.parent, exp.Subquery) and isinstance(scope.parent.parent, (exp.From, exp.Join)):
                break  # a derived table sees no enclosing query's columns
            passed.append(scope)
            scope = scope.find_ancestor(exp.Select)
    return tree


_INTEGER_DIGITS = {"TINYINT": 3, "SMALLINT": 5, "INT": 10, "INTEGER": 10, "MEDIUMINT": 8, "BIGINT": 19, "INT64": 19}


def _decimal_shape(type_sql: str) -> tuple[int, int] | None:
    """``(integer digits, scale)`` of an exact numeric type, else ``None``."""

    try:
        parsed = sqlglot.exp.DataType.build(type_sql, dialect="mysql")
    except (sqlglot.errors.SqlglotError, ValueError):
        return None
    return _datatype_shape(parsed)


def _datatype_shape(parsed: exp.DataType, bare: tuple[int, int] = (10, 0)) -> tuple[int, int] | None:
    name = parsed.this.name if isinstance(parsed.this, exp.DataType.Type) else str(parsed.this)
    name = name.upper()
    if name in _INTEGER_DIGITS:
        return _INTEGER_DIGITS[name], 0
    if name in ("DECIMAL", "NUMERIC"):
        sizes = [e.this.this for e in parsed.expressions if isinstance(e, exp.DataTypeParam) and isinstance(e.this, exp.Literal)]
        try:
            precision = int(sizes[0]) if sizes else bare[0] + bare[1]
            scale = int(sizes[1]) if len(sizes) > 1 else 0 if sizes else bare[1]
        except ValueError:
            return None
        return precision - scale, scale
    return None


def _origin_type(select: exp.Select, column: exp.Column, types: dict[str, dict[str, str]], depth: int = 0) -> str | None:
    """Declared type of the table column that ``column`` reads, followed through plain derived tables."""

    if depth > 6:
        return None
    sources = _sources_of(select)
    name = column.name.lower()
    candidates = []
    for source in sources:
        if column.table and (source.alias_or_name or "").lower() != column.table.lower():
            continue
        if isinstance(source, exp.Table):
            parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
            declared = types.get(".".join(parts).lower())
            if declared is None:
                return None if column.table else None
            if name in declared:
                candidates.append(declared[name])
        elif isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
            inner = source.this
            for item in inner.expressions:
                if item.alias_or_name.lower() == name:
                    value = item.this if isinstance(item, exp.Alias) else item
                    if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star):
                        found = _origin_type(inner, value, types, depth + 1)
                        if found is not None:
                            candidates.append(found)
                        else:
                            return None
                    else:
                        return None
        else:
            return None
    return candidates[0] if len(candidates) == 1 else None


def _unbounded_text_cast(to: exp.DataType, dialect: str) -> bool:
    """A cast to a string type with no length (MySQL's ``CAST(x AS CHAR)`` is one), which keeps any string."""

    if to.expressions:
        return False
    kinds = {exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT}
    if dialect == "mysql":
        kinds.add(exp.DataType.Type.CHAR)
    return to.this in kinds


def _fold_identity_casts(select: exp.Select, types: dict[str, dict[str, str]], dialect: str = "bigquery") -> exp.Expression | None:
    """``CAST(x AS DECIMAL(p, s))`` is ``x`` when ``x``'s declared type already fits it.

    A ``DECIMAL(15, 2)`` or ``INT`` column cast to a wider exact type keeps every value, so the
    cast only changes the type the engine reports.
    """

    def input_column(cast):
        value = cast.this
        if isinstance(value, (exp.Sum, exp.Min, exp.Max)):
            value = value.this
        return value if isinstance(value, exp.Column) else None

    casts = [c for c in select.find_all(exp.Cast) if c.find_ancestor(exp.Select) is select and input_column(c) is not None and isinstance(c.args.get("to"), exp.DataType)]
    if not casts:
        return None
    copy = select.copy()
    changed = False
    for cast in [c for c in copy.find_all(exp.Cast) if c.find_ancestor(exp.Select) is copy and input_column(c) is not None and isinstance(c.args.get("to"), exp.DataType)]:
        if isinstance(cast.this, exp.Column) and not isinstance(cast, exp.TryCast) and _unbounded_text_cast(cast.args["to"], dialect):
            declared = _origin_type(copy, cast.this, types)
            if declared and re.match(r"^\s*(VARCHAR|TEXT|STRING)\b", declared, re.I):
                cast.replace(cast.this.copy())
                changed = True
            continue
        target = _datatype_shape(cast.args["to"], (29, 9) if dialect == "bigquery" else (10, 0))
        if target is None or isinstance(cast, exp.TryCast):
            continue
        declared = _origin_type(copy, input_column(cast), types)
        have = _decimal_shape(declared) if declared else None
        if isinstance(cast.this, exp.Sum):
            # SUM of integers stays integral. As elsewhere in this prover,
            # overflow/failed casts and output type differences are excluded.
            # A decimal SUM can overflow its original precision, so do not
            # use the input column's precision to justify a decimal cast.
            if have is None or have[1] != 0 or target[1] != 0:
                continue
        if have is not None and target[1] >= have[1] and target[0] >= have[0]:
            cast.replace(cast.this.copy())
            changed = True
    return copy if changed else None


_EXACT_TYPE = re.compile(r"^\s*(tinyint|smallint|mediumint|int|integer|bigint|int2|int4|int8|int64|numeric|decimal|bignumeric)\b", re.I)


def _shifted_sums(select: exp.Select, types: dict[str, dict[str, str]]) -> exp.Expression | None:
    """``SUM(x + c)`` is ``SUM(x) + c * COUNT(x)`` for a constant ``c`` and an exact-typed column ``x``.

    Rows where ``x`` is NULL count in neither; with no such rows both sides are NULL
    (``NULL + c * 0``). Only declared integer and decimal columns qualify, so no rounding differs.
    """

    def split(arg: exp.Expression):
        node = arg.unnest() if isinstance(arg, exp.Paren) else arg
        if not isinstance(node, (exp.Add, exp.Sub)):
            return None
        left, right = node.this, node.expression
        number = lambda e: isinstance(e, exp.Literal) and not e.is_string and re.fullmatch(r"\d+(\.\d+)?", e.this or "")
        if isinstance(left, exp.Column) and number(right):
            return left, right, isinstance(node, exp.Sub)
        if isinstance(node, exp.Add) and number(left) and isinstance(right, exp.Column):
            return right, left, False
        return None

    if not types:
        return None
    sums = [
        s for s in select.find_all(exp.Sum)
        if s.find_ancestor(exp.Select) is select and not isinstance(s.parent, exp.Window) and not s.args.get("distinct")
        and not isinstance(s.this, exp.Distinct) and split(s.this) is not None
    ]
    if not sums:
        return None
    copy = select.copy()
    changed = False
    for node in [
        s for s in copy.find_all(exp.Sum)
        if s.find_ancestor(exp.Select) is copy and not isinstance(s.parent, exp.Window) and not isinstance(s.this, exp.Distinct)
    ]:
        parts = split(node.this)
        if parts is None:
            continue
        column, constant, minus = parts
        declared = _origin_type(copy, column, types)
        if not declared or not _EXACT_TYPE.match(declared):
            continue
        shift = exp.Mul(this=constant.copy(), expression=exp.Count(this=column.copy()))
        total = exp.Sub(this=exp.Sum(this=column.copy()), expression=shift) if minus else exp.Add(this=exp.Sum(this=column.copy()), expression=shift)
        node.replace(exp.Paren(this=total))
        changed = True
    return copy if changed else None


def _order_grouped_columns(select: exp.Select) -> exp.Expression | None:
    """List a grouped derived table's group keys first, then its aggregates, each in a fixed order.

    The outputs of a derived table are read by name, so their order carries no meaning, but two
    spellings of the same grouped relation must list them alike to be recognized as one relation.
    """

    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    copy = select.copy()
    changed = False
    for source in _sources_of(copy):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if inner.args.get("order") or inner.args.get("limit") or inner.args.get("distinct") or not inner.args.get("group"):
            continue
        names = [e.alias_or_name.lower() for e in inner.expressions]
        if "" in names or len(set(names)) != len(names):
            continue

        def key(item: exp.Expression) -> tuple[int, str]:
            value = item.this if isinstance(item, exp.Alias) else item
            plain = value.copy()
            for column in plain.find_all(exp.Column):
                column.set("table", None)
            return (1 if any(isinstance(n, exp.AggFunc) for n in value.walk()) else 0, plain.sql().lower())

        ordered = sorted(inner.expressions, key=key)
        if [id(e) for e in ordered] != [id(e) for e in inner.expressions]:
            inner.set("expressions", [e.copy() for e in ordered])
            changed = True
    return copy if changed else None


_NEVER_NULL_AGGREGATES = (exp.Sum, exp.Min, exp.Max, exp.Avg)


def _never_null_value(expr: exp.Expression, declared: set[str]) -> bool:
    """Arithmetic over aggregates of NOT NULL columns: a group is never empty, so the value is never NULL."""

    if isinstance(expr, exp.Paren):
        return _never_null_value(expr.this, declared)
    if isinstance(expr, exp.Literal):
        return not expr.is_string
    if isinstance(expr, (exp.Add, exp.Sub, exp.Mul)):
        return _never_null_value(expr.this, declared) and _never_null_value(expr.expression, declared)
    if isinstance(expr, _NEVER_NULL_AGGREGATES):
        return isinstance(expr.this, exp.Column) and expr.this.sql() in declared
    return False


def _drop_derived_null_guard(select: exp.Select, not_null: dict[str, frozenset[str]]) -> exp.Expression | None:
    """``SELECT .. FROM (SELECT k, 0.5 * SUM(q) AS h FROM t GROUP BY k) AS g WHERE h IS NOT NULL`` drops the guard
    when ``q`` is declared NOT NULL: every group has a row, so ``h`` is a number."""

    where = select.args.get("where")
    from_ = select.args.get("from_") or select.args.get("from")
    if not not_null or where is None or from_ is None or select.args.get("joins") or not isinstance(from_.this, exp.Subquery):
        return None
    inner = from_.this.this
    if not isinstance(inner, exp.Select) or not inner.args.get("group") or not inner.expressions:
        return None
    declared = _declared_not_null(inner.expressions[0], {k.lower(): set(v) for k, v in not_null.items()})
    if not declared:
        return None
    outputs = {e.alias_or_name.lower(): (e.this if isinstance(e, exp.Alias) else e) for e in inner.expressions}
    alias = (from_.this.alias or "").lower()
    parts = _conjuncts(where.this)
    kept = []
    for part in parts:
        if (
            isinstance(part, exp.Not)
            and isinstance(part.this, exp.Is)
            and isinstance(part.this.expression, exp.Null)
            and isinstance(part.this.this, exp.Column)
            and (not part.this.this.table or part.this.this.table.lower() == alias)
            and part.this.this.name.lower() in outputs
            and _never_null_value(outputs[part.this.this.name.lower()], declared)
        ):
            continue
        kept.append(part)
    if len(kept) == len(parts):
        return None
    copy = select.copy()
    copy.set("where", exp.Where(this=_and_all([k.copy() for k in kept])) if kept else None)
    return copy


def _local_columns(source: exp.Expression, schema: dict[str, list[str]]) -> list[str] | None:
    if isinstance(source, exp.Table) and not source.args.get("joins") and not source.args.get("pivots"):
        found = schema.get(".".join(p.name for p in source.parts).lower())
        return list(found) if found is not None else None
    if isinstance(source, exp.Subquery):
        return _select_names(source.this)
    return None


def _outer_references(test: exp.Expression, schema: dict[str, list[str]]) -> list[exp.Column] | None:
    """Columns of an EXISTS test that its own sources do not declare (``None`` when that cannot be told)."""

    found: list[exp.Column] = []
    for column in test.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            return None
        scope = column.find_ancestor(exp.Select)
        local = False
        while scope is not None:
            known = []
            for source in _sources_of(scope):
                columns = _local_columns(source, schema)
                if columns is None:
                    return None
                known.append(((source.alias_or_name or "").lower(), [c.lower() for c in columns]))
            if column.table:
                if any(alias == column.table.lower() for alias, _ in known):
                    local = True
                    break
            elif any(column.name.lower() in cols for _, cols in known):
                local = True
                break
            parent = scope.find_ancestor(exp.Select)
            if parent is None or not any(n is test or True for n in [parent]) or not _within(scope, test):
                break
            scope = parent if _within(parent, test) else None
        if not local:
            found.append(column)
    return found


def _within(node: exp.Expression, root: exp.Expression) -> bool:
    return node is root or any(a is root for a in _ancestors_of(node, root))


def _pull_up_exists(select: exp.Select, schema: dict[str, list[str]] | None) -> exp.Expression | None:
    """``FROM (SELECT .. FROM t WHERE EXISTS (..)) AS d JOIN ..`` tests EXISTS in the select that joins ``d``.

    A filter on the rows of a derived table joined by inner joins is a filter of the join. The
    test's references to the derived table's rows are named through its alias.
    """

    if not schema:
        return None
    schema = {k.lower(): [c.lower() for c in v] for k, v in schema.items()}
    joins = select.args.get("joins") or []
    if not joins or any(j.args.get("side") or j.args.get("kind") in _OUTER or j.args.get("using") is not None for j in joins):
        return None
    for position, source in enumerate(_sources_of(select)):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=False) or inner.args.get("where") is None:
            continue
        inner_sources = _sources_of(inner)
        if len(inner_sources) != 1 or not isinstance(inner_sources[0], exp.Table) or not inner_sources[0].alias_or_name:
            continue
        table_columns = _local_columns(inner_sources[0], schema)
        if table_columns is None:
            continue
        outputs = {}
        for item in inner.expressions:
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(value, exp.Column) and not isinstance(value.this, exp.Star) and item.alias_or_name:
                outputs[value.name.lower()] = item.alias_or_name
        parts = _conjuncts(inner.args["where"].this)
        for index, part in enumerate(parts):
            test = part.this if isinstance(part, exp.Not) else part
            if not isinstance(test, exp.Exists):
                continue
            references = _outer_references(test, schema)
            if not references or any(
                (r.table and r.table.lower() != inner_sources[0].alias_or_name.lower())
                or r.name.lower() not in table_columns
                or r.name.lower() not in outputs
                for r in references
            ):
                continue
            moved = part.copy()
            for column in list(moved.find_all(exp.Column)):
                if any(column.name == r.name and column.table == r.table and column.sql() == r.sql() for r in references):
                    column.set("table", exp.to_identifier(source.alias))
                    column.set("this", exp.to_identifier(outputs[column.name.lower()]))
            copy = select.copy()
            target = _sources_of(copy)[position]
            rest = [c.copy() for j, c in enumerate(parts) if j != index]
            target.this.set("where", exp.Where(this=_and_all(rest)) if rest else None)
            where = copy.args.get("where")
            copy.set("where", exp.Where(this=_and_all(([where.this.copy()] if where is not None else []) + [moved])))
            return copy
    return None


def _exists_key(test: exp.Expression, schema: dict[str, list[str]]) -> str:
    from .smt_equivalence import _canonical_aliases

    wrapper = exp.Select(expressions=[exp.Literal.number(1)]).where(test.copy())
    return _canonical_aliases(wrapper, schema).sql().lower()


def _drop_implied_exists(select: exp.Select, schema: dict[str, list[str]] | None) -> exp.Expression | None:
    """A grouped derived table joined on its key need not repeat an EXISTS test the join already makes.

    ``FROM p JOIN (SELECT k, SUM(x) FROM t WHERE EXISTS (.. t.k ..) GROUP BY k) AS g ON p.k = g.k`` with
    ``EXISTS (.. p.k ..)`` among the select's conditions: a group whose key fails the test has no partner
    row, and the test depends on the key only, so the groups that remain are unchanged.
    """

    if not schema:
        return None
    schema = {k.lower(): [c.lower() for c in v] for k, v in schema.items()}
    joins = select.args.get("joins") or []
    if not joins or any(j.args.get("side") or j.args.get("kind") in _OUTER or j.args.get("using") is not None for j in joins):
        return None
    conditions = []
    for join in joins:
        if join.args.get("on") is not None:
            conditions.extend(_conjuncts(join.args["on"]))
    if select.args.get("where") is not None:
        conditions.extend(_conjuncts(select.args["where"].this))
    outer_tests = {_exists_key(c.this if isinstance(c, exp.Not) else c, schema): isinstance(c, exp.Not) for c in conditions if isinstance(c.this if isinstance(c, exp.Not) else c, exp.Exists)}
    if not outer_tests:
        return None
    for position, source in enumerate(_sources_of(select)):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not inner.args.get("group") or inner.args.get("distinct") or inner.args.get("where") is None:
            continue
        group_names = {g.name.lower() for g in inner.args["group"].expressions if isinstance(g, exp.Column)}
        if len(group_names) != len(inner.args["group"].expressions):
            continue
        output_of = {}
        for item in inner.expressions:
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(value, exp.Column) and value.name.lower() in group_names and item.alias_or_name:
                output_of[value.name.lower()] = item.alias_or_name.lower()
        ties: dict[str, exp.Column] = {}
        for condition in conditions:
            if isinstance(condition, exp.EQ):
                for g_side, other in ((condition.this, condition.expression), (condition.expression, condition.this)):
                    if (
                        isinstance(g_side, exp.Column)
                        and g_side.table.lower() == source.alias.lower()
                        and isinstance(other, exp.Column)
                        and other.table
                        and other.table.lower() != source.alias.lower()
                    ):
                        ties.setdefault(g_side.name.lower(), other)
        inner_sources = _sources_of(inner)
        parts = _conjuncts(inner.args["where"].this)
        for index, part in enumerate(parts):
            test = part.this if isinstance(part, exp.Not) else part
            if not isinstance(test, exp.Exists):
                continue
            references = _outer_references(test, schema)
            if not references:
                continue
            substituted = part.copy()
            ok = True
            for column in list(substituted.find_all(exp.Column)):
                if not any(column.sql() == r.sql() for r in references):
                    continue
                name = column.name.lower()
                out = output_of.get(name)
                if name not in group_names or out is None or out not in ties or (column.table and column.table.lower() not in {(s.alias_or_name or "").lower() for s in inner_sources}):
                    ok = False
                    break
                column.replace(ties[out].copy())
            if not ok:
                continue
            probe = substituted.this if isinstance(substituted, exp.Not) else substituted
            if outer_tests.get(_exists_key(probe, schema)) != isinstance(part, exp.Not):
                continue
            copy = select.copy()
            target = _sources_of(copy)[position]
            rest = [c.copy() for j, c in enumerate(parts) if j != index]
            target.this.set("where", exp.Where(this=_and_all(rest)) if rest else None)
            return copy
    return None


def _wrap_outer_join_aggregate(select: exp.Select) -> exp.Expression | None:
    """An aggregate over an outer join reads the join as one derived relation.

    ``SELECT k, COUNT(*) FROM a LEFT JOIN b ON c GROUP BY k`` becomes
    ``SELECT j.c0, COUNT(*) FROM (SELECT a.k AS c0 FROM a LEFT JOIN b ON c) AS j GROUP BY j.c0``:
    the same rows, with the join stated once and its columns named by position, so two spellings of
    the same join (different aliases, expressions folded in) have the same text.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    joins = select.args.get("joins") or []
    if from_ is None or not joins or not any((j.args.get("side") or "").upper() in ("LEFT", "RIGHT", "FULL") for j in joins):
        return None
    from .eager_aggregation import _own_aggregates

    if not (select.args.get("group") or _own_aggregates(select)) or not _no_extras(select, allow_group=True):
        return None
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    sources = _sources_of(select)
    aliases = [(s.alias_or_name or "").lower() for s in sources]
    if "" in aliases or len(set(aliases)) != len(aliases):
        return None
    # A subquery in the SELECT, WHERE or HAVING could read the join's columns; leave those shapes alone.
    if any(
        not isinstance(n.parent, (exp.From, exp.Join))
        for n in select.find_all(exp.Subquery, exp.Exists)
        if n.find_ancestor(exp.Join) is None
    ):
        return None
    columns = [
        c for c in select.find_all(exp.Column)
        if not isinstance(c.this, exp.Star) and c.find_ancestor(exp.Join) is None and c.find_ancestor(exp.Select) is select
    ]
    if any(c.table.lower() not in aliases for c in columns):
        return None
    spelled = {(s.alias_or_name or "").lower(): s.alias_or_name for s in sources}
    keys = [(c.table.lower(), c.name.lower()) for c in columns]
    positions: dict[tuple[str, str], int] = {}
    for key in keys:
        positions.setdefault(key, len(positions))
    if not positions:
        return None
    inner = exp.Select(
        expressions=[
            exp.alias_(exp.column(name, table=spelled[table]), f"c{index}") for (table, name), index in positions.items()
        ]
    )
    inner.set(FROM_KEY, from_.copy())
    inner.set("joins", [j.copy() for j in joins])
    # An output that is a bare column keeps its name once the column is renamed to kqj.cN.
    select.set(
        "expressions",
        [exp.alias_(e, e.name, copy=False) if isinstance(e, exp.Column) and not isinstance(e.this, exp.Star) else e for e in select.expressions],
    )
    for column, key in zip(columns, keys):
        column.set("table", exp.to_identifier("kqj"))
        column.set("this", exp.to_identifier(f"c{positions[key]}"))
    select.set("joins", None)
    select.set(FROM_KEY, exp.From(this=exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier("kqj")))))
    return select


def _reads_of(select: exp.Select, alias: str, *, inside: bool = False) -> set[str]:
    """The names ``select`` may read from its source ``alias``.

    Columns inside derived-table bodies belong to those bodies' own sources, so they are left out unless ``inside``.
    """

    bodies = [] if inside else [s for s in _sources_of(select) if isinstance(s, exp.Subquery)]
    return {
        c.name.lower()
        for c in select.find_all(exp.Column)
        if (not c.table or c.table.lower() == alias.lower()) and not any(_within(c, body) for body in bodies)
    }


def _prune_derived(select: exp.Select) -> exp.Expression | None:
    """Drop the columns of a derived table that the enclosing query never reads.

    A grouped or plain derived table keeps its rows when an output is removed,
    so ``SELECT 1 FROM (SELECT k, COUNT(*) FROM t GROUP BY k) AS d`` is the same
    as ``SELECT 1 FROM (SELECT k FROM t GROUP BY k) AS d``. Not applied under
    ``DISTINCT`` (removing a column changes which rows collapse) or with a star,
    nor where it would remove the last aggregate of a select without GROUP BY.
    """

    if any(
        isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count)
        for star in select.find_all(exp.Star)
    ):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    changed = False
    for source in [from_.this] + [j.this for j in select.args.get("joins") or []]:
        if isinstance(source, exp.Subquery) and source.alias and isinstance(source.this, exp.Union):
            # a grouped branch keeps its key columns visible: the regrouping rules match on them
            grouped = any(b.args.get("group") for b in source.this.find_all(exp.Select))
            used = _reads_of(select, source.alias, inside=grouped)
            if _prune_union_all(source.this, used):
                changed = True
            continue
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if not _no_extras(inner, allow_group=True):
            continue
        if inner.args.get("distinct") or any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in inner.expressions):
            continue
        used = {
            c.name.lower()
            for c in select.find_all(exp.Column)
            if not c.table or c.table.lower() == source.alias.lower()
        }
        keep = [e for e in inner.expressions if e.alias_or_name and e.alias_or_name.lower() in used]
        if not keep:
            keep = inner.expressions[:1]
        if _global_aggregate(inner):
            # keep an aggregate output, or the one-row aggregate becomes a row per input row
            pruned = inner.copy()
            pruned.set("expressions", [e.copy() for e in keep])
            if not _global_aggregate(pruned):
                witness = next((e for e in inner.expressions if any(a.find_ancestor(exp.Select) is inner for a in e.find_all(exp.AggFunc))), None)
                if witness is None:
                    continue
                keep = [e for e in inner.expressions if e is witness or any(e is k for k in keep)]
        if len(keep) == len(inner.expressions):
            continue
        inner.set("expressions", [e.copy() for e in keep])
        changed = True
    return select if changed else None


def _declared_names(select: exp.Select) -> set[str]:
    """The names ``select``'s FROM and JOIN items declare, lower-case (an UNNEST's element name included)."""

    names = set()
    for source in _sources_of(select):
        if source.alias_or_name:
            names.add(source.alias_or_name.lower())
        alias = source.args.get("alias")
        if isinstance(source, exp.Unnest) and alias is not None and alias.args.get("columns"):
            names.add(alias.args["columns"][0].name.lower())  # BigQuery: the element is a range variable
    return names


def _may_capture(outer: exp.Select, node: exp.Subquery, table: exp.Table, exposed: set[str], schema: dict[str, list[str]] | None) -> bool:
    """Whether reading ``node`` as its base ``table`` could change what a column in ``outer``'s scope names.

    The table may have columns ``node`` does not expose. A bare column that is not one of ``exposed``
    (it named an outer query's column, or another source's) could then bind to such a column, and so
    could the head of a path ``s.f`` that names no visible alias, unless ``schema`` lists the table's
    columns and none is that name. A qualifier that names a visible alias is read as that alias, as the
    provers read it. Sibling derived tables cannot see ``node`` and are not searched.
    """

    known = {k.lower(): v for k, v in (schema or {}).items()}.get(".".join(p.name for p in table.parts).lower())
    hidden = None if known is None else {c.lower() for c in known} - exposed
    siblings = [s for s in _sources_of(outer) if isinstance(s, exp.Subquery) and s is not node]
    for column in outer.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or _within(column, node) or any(_within(column, s) for s in siblings):
            continue
        lead = column.parts[0].name.lower()
        if lead in exposed or (hidden is not None and lead not in hidden):
            continue
        if not column.table:
            return True
        scope = column.find_ancestor(exp.Select)
        while scope is not None and lead not in _declared_names(scope):
            scope = scope.find_ancestor(exp.Select)
        if scope is None:
            return True
    return False


def _inline_projection(node: exp.Subquery, schema: dict[str, list[str]] | None = None) -> exp.Expression | None:
    """``(SELECT a, b FROM t) AS u`` is ``t AS u`` when every column is passed through.

    Only the columns of ``u`` the enclosing query uses matter, so dropping the
    unused ones is invisible unless the enclosing query uses ``*``.
    """

    inner = node.this
    if not isinstance(node.parent, (exp.From, exp.Join)) or not node.alias:
        return None
    if not isinstance(inner, exp.Select) or not _no_extras(inner, allow_group=False):
        return None
    if inner.args.get("where") or inner.args.get("joins") or any(inner.find_all(exp.AggFunc)):
        return None
    from_ = inner.args.get("from_") or inner.args.get("from")
    table = from_.this if from_ else None
    if not isinstance(table, exp.Table):
        return None
    if any(not isinstance(e, exp.Column) or e.name == "*" or e.table not in {"", table.alias_or_name} for e in inner.expressions):
        return None
    outer = node.find_ancestor(exp.Select)
    if outer is None or any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in outer.find_all(exp.Star)):
        return None
    if _may_capture(outer, node, table, {e.name.lower() for e in inner.expressions}, schema):
        return None
    return exp.Table(this=table.this.copy(), db=table.args.get("db"), catalog=table.args.get("catalog"), alias=node.args.get("alias"))


def _canonical_branch(branch: exp.Select) -> exp.Select | None:
    """Positional table aliases and output names for a plain select over tables."""

    if not _no_extras(branch, allow_group=True):
        return None
    from_ = branch.args.get("from_") or branch.args.get("from")
    if from_ is None:
        return None
    sources = [from_.this] + [j.this for j in branch.args.get("joins") or []]
    if any(not isinstance(src, exp.Table) for src in sources):
        return None
    if any(isinstance(n, (exp.Subquery, exp.Select)) for n in branch.walk() if n is not branch):
        return None
    copy = branch.copy()
    from_ = copy.args.get("from_") or copy.args.get("from")
    tables = [from_.this] + [j.this for j in copy.args.get("joins") or []]
    renames = {}
    for index, table in enumerate(tables):
        renames[table.alias_or_name.lower()] = f"t{index}"
    new_tables = []
    for column in copy.find_all(exp.Column):
        if column.table:
            if column.table.lower() not in renames:
                return None
            new_tables.append((column, renames[column.table.lower()]))
        elif len(tables) == 1:
            new_tables.append((column, "t0"))
    for column, name in new_tables:
        column.set("table", exp.to_identifier(name))
    for index, table in enumerate(tables):
        table.set("alias", exp.TableAlias(this=exp.to_identifier(f"t{index}")))
    items = []
    for index, item in enumerate(copy.expressions):
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        items.append(exp.alias_((item.this if isinstance(item, exp.Alias) else item).copy(), f"c{index}"))
    copy.set("expressions", items)
    return copy


def _canonicalize_union_source(node: exp.Subquery) -> exp.Expression | None:
    """Make a derived UNION ALL comparable by text: same shape, same SQL.

    The SMT prover identifies a multi-branch derived table by its SQL, so two
    unions that differ only in aliases or branch order would otherwise never
    match. Output columns become c0, c1, ... (the enclosing select is updated),
    table aliases become t0, t1, ..., and branches are sorted.
    """

    if not isinstance(node.this, exp.Union) or not node.alias or node.alias == _SPLIT_ALIAS + "_done":
        return None
    branches = _union_all_branches(node)
    outer = node.find_ancestor(exp.Select)
    if not branches or len(branches) < 2 or outer is None:
        return None
    first_names = [item.alias_or_name for item in branches[0].expressions]
    if "" in first_names or len(set(first_names)) != len(first_names):
        return None
    canonical = [_canonical_branch(b) for b in branches]
    if any(c is None for c in canonical):
        return None
    if any(len(b.expressions) != len(first_names) for b in branches):
        return None
    # columns are listed by what the branches compute, not by name: an output named after its
    # expression in one query and after its column in the other is then the same column
    def text(branch: exp.Select, index: int) -> str:
        return branch.expressions[index].this.sql(dialect="bigquery")

    order = sorted(range(len(first_names)), key=lambda i: sorted(text(b, i) for b in canonical))
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in outer.find_all(exp.Star)):
        order = list(range(len(first_names)))  # a star reads the columns in their order
    for branch in canonical:
        values = [item.this for item in branch.expressions]
        branch.set("expressions", [exp.alias_(values[old], f"c{new}") for new, old in enumerate(order)])
    alias = node.alias.lower()
    renames = {first_names[old].lower(): f"c{new}" for new, old in enumerate(order)}
    from_ = outer.args.get("from_") or outer.args.get("from")
    sole_source = from_ is not None and from_.this is node and not outer.args.get("joins")
    targets = []
    for column in outer.find_all(exp.Column):
        if column.table:
            if column.table.lower() == alias:
                if column.name.lower() not in renames:
                    return None
                targets.append(column)
        elif sole_source and column.name.lower() in renames:
            targets.append(column)
        elif column.name.lower() in renames:
            return None  # ambiguous unqualified use: leave it alone
    for column in targets:
        column.set("this", exp.to_identifier(renames[column.name.lower()]))
    canonical.sort(key=lambda b: b.sql(dialect="bigquery"))
    union: exp.Expression = canonical[0]
    for branch in canonical[1:]:
        union = exp.Union(this=union, expression=branch, distinct=False)
    return exp.Subquery(this=union, alias=node.args.get("alias"))


_VALUES_COUNTER = itertools.count()


def _values_to_union(tree: exp.Expression) -> exp.Expression:
    """``(VALUES (1, 2), (3, 4)) AS t(a, b)`` is ``SELECT 1 AS a, 2 AS b UNION ALL SELECT 3, 4``.

    A constant relation is a sum of one-row relations. Columns without declared names are called
    ``expr$0``, ``expr$1``, .. as Calcite does; a query that reads any other default name stays unresolved
    and is not proven.
    """

    def step(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Values) or not isinstance(node.parent, (exp.From, exp.Join)):
            return node
        alias = node.args.get("alias")
        names = [c.name for c in alias.columns] if alias is not None else []
        rows = node.expressions
        if not names and rows and isinstance(rows[0], exp.Tuple):
            names = [f"expr${i}" for i in range(len(rows[0].expressions))]
        if not names or not rows or any(not isinstance(r, exp.Tuple) or len(r.expressions) != len(names) for r in rows):
            return node
        selects = []
        for index, row in enumerate(rows):
            items = [
                exp.alias_(value.copy(), name) if index == 0 else value.copy()
                for value, name in zip(row.expressions, names)
            ]
            selects.append(exp.select(*items))
        body: exp.Expression = selects[0]
        for select in selects[1:]:
            body = exp.Union(this=body, expression=select, distinct=False)
        name = alias.name if alias is not None and alias.name else f"kqv{next(_VALUES_COUNTER)}"
        return exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(name)))

    return tree.transform(step)


_EXACT_LIMIT = 2**53


def _int_value(node: exp.Expression) -> int | None:
    if isinstance(node, exp.Paren):
        return _int_value(node.this)
    if isinstance(node, exp.Literal) and not node.is_string and node.this.lstrip("-").isdigit():
        return int(node.this)
    if isinstance(node, exp.Neg):
        inner = _int_value(node.this)
        return None if inner is None else -inner
    return None


def _fold_constants(tree: exp.Expression) -> exp.Expression:
    """Evaluate integer arithmetic on literals, e.g. ``10 / 2`` to ``5``.

    Decimal literals lose trailing zeros (``1.00`` is ``1.0``; the point stays, since BigQuery reads
    ``1.0`` as FLOAT64 and ``x * 1.0`` rounds an INT64 ``x``). Only exact results are folded (a division must divide evenly, nothing may
    leave the range where FLOAT64 and INT64 agree), so INT64 and FLOAT64
    readings of the expression coincide.
    """

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"\d+\.\d*0", node.name):
            # 1.00 and 0.20 name the same numbers as 1.0 and 0.2.
            text = node.name.rstrip("0")
            return exp.Literal.number(text + "0" if text.endswith(".") else text)
        if isinstance(node, exp.Case) and node.this is None and len(node.args.get("ifs") or []) == 1:
            branch = node.args["ifs"][0]
            if isinstance(branch.this, exp.Boolean):
                if branch.this.this:
                    return branch.args["true"].copy()
                return node.args["default"].copy() if node.args.get("default") is not None else exp.Null()
        if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)):
            a, b = _int_value(node.this), _int_value(node.expression)
            if a is None or b is None:
                return node
            holds = {exp.EQ: a == b, exp.NEQ: a != b, exp.LT: a < b, exp.LTE: a <= b, exp.GT: a > b, exp.GTE: a >= b}[type(node)]
            return exp.true() if holds else exp.false()
        if not isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            return node
        a, b = _int_value(node.this), _int_value(node.expression)
        if a is None or b is None:
            return node
        if isinstance(node, exp.Add):
            value = a + b
        elif isinstance(node, exp.Sub):
            value = a - b
        elif isinstance(node, exp.Mul):
            value = a * b
        elif b != 0 and a % b == 0:
            value = a // b
        else:
            return node
        if abs(value) >= _EXACT_LIMIT:
            return node
        return exp.Neg(this=exp.Literal.number(-value)) if value < 0 else exp.Literal.number(value)

    return tree.transform(step)


def _is_empty_select(node: exp.Expression) -> bool:
    """A SELECT whose WHERE is literally FALSE (and has no aggregate) or that has LIMIT 0 returns no rows."""

    while isinstance(node, exp.Subquery):
        node = node.this
    if not isinstance(node, exp.Select) or node.args.get("group") or node.args.get("having"):
        return False
    if _where_false_is_empty(node):
        return True
    limit = node.args.get("limit")
    return limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.name == "0"


def _fold_trivia(tree: exp.Expression) -> exp.Expression:
    """Small exact identities: ``(SELECT 1)`` is ``1``, ``IN``/``EXISTS`` over an empty subquery
    is FALSE, and ``agg(x) FILTER (WHERE c)`` is ``agg(CASE WHEN c THEN x END)``."""

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Subquery) and isinstance(node.parent, exp.Binary):
            inner = node.this
            if (
                isinstance(inner, exp.Select)
                and len(inner.expressions) == 1
                and not any(inner.args.get(k) for k in ("from_", "from", "where", "group", "having", "joins", "limit", "distinct"))
                and isinstance(inner.expressions[0], (exp.Literal, exp.Boolean))
            ):
                return inner.expressions[0].copy()
        if isinstance(node, exp.Select):
            group = node.args.get("group")
            if (
                group is not None
                and len(group.expressions) == 1
                and isinstance(group.expressions[0], exp.Boolean)
                and not node.args.get("having")
                and not node.args.get("distinct")
                and not any(node.find_all(exp.AggFunc))
                and node.expressions
                and all(isinstance(item.unalias(), exp.Literal) for item in node.expressions)
                and not any(isinstance(item.unalias(), exp.Star) for item in node.expressions)
            ):
                # One group holding every row: a constant row exists iff the source has a row.
                node.set("group", None)
                node.set("distinct", exp.Distinct())
                return node
        if isinstance(node, (exp.Upper, exp.Lower)) and type(node.this) is type(node):
            # Repeating a case map changes nothing; across maps the inner one can matter
            # (LOWER(UPPER('ς')) is 'σ', UPPER(LOWER('İ')) is 'I'), so those are kept.
            return node.this.copy()
        if isinstance(node, exp.Anonymous) and node.name.upper() == "POSITIVE" and len(node.expressions) == 1:
            return node.expressions[0].copy()
        if isinstance(node, exp.Concat) and any(isinstance(e, exp.Concat) for e in node.expressions):
            flat = []
            for item in node.expressions:
                if isinstance(item, exp.Concat) and item.args.get("safe") == node.args.get("safe") and item.args.get("coalesce") == node.args.get("coalesce"):
                    flat.extend(i.copy() for i in item.expressions)
                else:
                    flat.append(item.copy())
            rebuilt = node.copy()
            rebuilt.set("expressions", flat)
            return rebuilt
        for kind, part in ((exp.Year, "YEAR"), (exp.Month, "MONTH"), (exp.Day, "DAY")):
            if isinstance(node, kind):
                argument = node.this
                if isinstance(argument, exp.TsOrDsToDate):
                    argument = argument.this
                return exp.Extract(this=exp.var(part), expression=argument.copy())
        if isinstance(node, exp.In) and isinstance(node.args.get("query"), exp.Expression) and _is_empty_select(node.args["query"]):
            return exp.false()
        if isinstance(node, exp.Exists) and _is_empty_select(node.this):
            return exp.false()
        if isinstance(node, exp.Filter) and isinstance(node.this, (exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg)):
            condition = node.expression.this if isinstance(node.expression, exp.Where) else None
            aggregate = node.this
            if condition is None:
                return node
            argument = aggregate.this
            distinct = isinstance(argument, exp.Distinct)
            if distinct:
                if len(argument.expressions) != 1:
                    return node
                argument = argument.expressions[0]
            if isinstance(argument, exp.Star):
                argument = exp.Literal.number(1)
            guarded = exp.Case(ifs=[exp.If(this=condition.copy(), true=argument.copy())])
            if distinct:
                guarded = exp.Distinct(expressions=[guarded])
            rebuilt = aggregate.copy()
            rebuilt.set("this", guarded)
            return rebuilt
        return node

    return tree.transform(step)


def _select_names(select: exp.Expression) -> list[str] | None:
    first = select
    while isinstance(first, exp.Union):
        first = first.this
    while isinstance(first, exp.Subquery):
        first = first.this
    if not isinstance(first, exp.Select):
        return None
    names = [item.alias_or_name.lower() for item in first.expressions]
    if "" in names or len(set(names)) != len(names) or any(isinstance(i, exp.Star) for i in first.expressions):
        return None
    return names


def _expand_stars(tree: exp.Expression, schema: dict[str, list[str]]) -> exp.Expression:
    """Replace ``*`` and ``t.*`` with explicit columns when every source's columns are known."""

    schema = {key.lower(): [c.lower() for c in cols] for key, cols in schema.items()}

    def columns_of(source: exp.Expression) -> list[str] | None:
        if isinstance(source, exp.Table):
            parts = [p.name for p in (source.args.get("catalog"), source.args.get("db"), source.this) if p is not None]
            return schema.get(".".join(parts).lower())
        if isinstance(source, exp.Subquery):
            return _select_names(source.this)
        return None

    for select in list(tree.find_all(exp.Select))[::-1]:  # innermost first
        if not any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in select.expressions):
            continue
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None or any(j.args.get("using") is not None or _natural(j) for j in select.args.get("joins") or []):
            continue  # a USING or NATURAL join lists its merged columns once, first (_using_to_on)
        sources = [from_.this] + [j.this for j in select.args.get("joins") or []]
        known = []
        for source in sources:
            columns = columns_of(source)
            # A lone derived table may go without an alias; its columns are then written bare.
            if columns is None or (not source.alias_or_name and len(sources) != 1):
                known = None
                break
            known.append((source.alias_or_name or "", columns))
        if known is None:
            continue
        items = []
        ok = True

        def expand(star: exp.Star, columns: list[tuple[str, str]]) -> list[exp.Expression] | None:
            """``(qualifier, column)`` pairs as items, honoring EXCEPT and REPLACE; ``None`` if not understood."""

            if star.args.get("rename") or star.args.get("ilike"):
                return None
            skipped = {c.name.lower() for c in star.args.get("except_") or star.args.get("except") or []}
            replaced = {}
            for r in star.args.get("replace") or star.args.get("replace_") or []:
                if not isinstance(r, exp.Alias):
                    return None
                replaced[r.alias.lower()] = r.this
            produced = []
            for qualifier, column in columns:
                if column in skipped:
                    continue
                if column in replaced:
                    produced.append(exp.alias_(replaced[column].copy(), column))
                else:
                    produced.append(exp.column(column, table=qualifier or None))
            return produced

        for item in select.expressions:
            if isinstance(item, exp.Star):
                produced = expand(item, [(a, c) for a, cols in known for c in cols])
                if produced is None:
                    ok = False
                    break
                items.extend(produced)
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                match = [cols for a, cols in known if a.lower() == item.table.lower()]
                if len(match) != 1:
                    ok = False
                    break
                produced = expand(item.this, [(item.table, c) for c in match[0]])
                if produced is None:
                    ok = False
                    break
                items.extend(produced)
            else:
                items.append(item)
        if ok:
            select.set("expressions", items)
    return tree


_REJECTING = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike)


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """The parts of an AND chain; a parenthesized part that is not itself an AND keeps its parentheses."""

    inner = node
    while isinstance(inner, exp.Paren):
        inner = inner.this
    if isinstance(inner, exp.And):
        return _conjuncts(inner.this) + _conjuncts(inner.expression)
    return [node if isinstance(inner, (exp.Or, exp.Xor)) else inner]


def _and_all(parts: list[exp.Expression]) -> exp.Expression | None:
    # sqlglot prints trees without adding parentheses, so an OR operand must carry its own.
    result = None
    for part in parts:
        if isinstance(part, exp.Or):
            part = exp.Paren(this=part)
        result = part if result is None else exp.And(this=result, expression=part)
    return result


def _guard_of(part: exp.Expression, columns: set[str]) -> bool:
    return (
        isinstance(part, exp.Not)
        and isinstance(part.this, exp.Is)
        and isinstance(part.this.expression, exp.Null)
        and part.this.this.sql() in columns
    )


def _declared_not_null(holder: exp.Expression, not_null: dict[str, set[str]]) -> set[str]:
    """SQL of the columns of ``holder``'s select that are NOT NULL by declaration and always present."""

    select = holder.find_ancestor(exp.Select)
    if select is None or any(
        j.args.get("side") or j.args.get("kind") in _OUTER for j in select.args.get("joins") or []
    ):
        return set()
    found: set[str] = set()
    for source in _sources_of(select):
        if isinstance(source, exp.Table) and not source.db and not source.catalog:
            for name in not_null.get(source.name.lower(), ()):
                for column in (exp.column(name, table=source.alias_or_name), exp.column(name)):
                    found.add(column.sql())
    return found


def _aggregate_guard(part: exp.Expression, declared: set[str]) -> bool:
    if not (isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null)):
        return False
    return _never_null_value(part.this.this, declared) and any(part.this.this.find_all(exp.AggFunc))


def _fold_null_guards(tree: exp.Expression, not_null: dict[str, frozenset[str]] | None = None) -> exp.Expression:
    """Drop ``x IS NOT NULL`` from a condition that already compares ``x``.

    In WHERE, ON and HAVING a comparison with NULL rejects the row, so another conjunct
    ``x > 0`` makes the guard redundant, and so does a NOT NULL declaration for a column of a
    table that no outer join can null-extend. Writing the condition with or without it must read alike.
    """

    not_null = {k.lower(): {c.lower() for c in v} for k, v in (not_null or {}).items()}

    for holder in list(tree.find_all(exp.Where, exp.Having)) + [j.args["on"].parent for j in tree.find_all(exp.Join) if j.args.get("on") is not None]:
        condition = holder.this if isinstance(holder, (exp.Where, exp.Having)) else holder.args["on"]
        parts = _conjuncts(condition)
        rejected = {
            side.sql()
            for part in parts
            if isinstance(part, _REJECTING)
            for side in (part.this, part.expression)
            if isinstance(side, exp.Column)
        }
        declared = _declared_not_null(holder, not_null) if not_null else set()
        kept = [part for part in parts if not _guard_of(part, rejected | declared)]
        if isinstance(holder, exp.Having) and holder.parent is not None and holder.parent.args.get("group") and not extended_grouping(holder.parent.args["group"]):
            # A group is never empty, so MIN/MAX/SUM/AVG of a NOT NULL column is never NULL (the empty
            # grouping set of a ROLLUP is one group even over no input, so not there).
            kept = [part for part in kept if not _aggregate_guard(part, declared)]
        if not kept and isinstance(holder, (exp.Where, exp.Having)) and len(parts) > 0 and holder.parent is not None:
            holder.pop()
            continue
        if not kept or (len(kept) == len(parts) and not any(isinstance(p, exp.Paren) for p in condition.find_all(exp.Paren) if isinstance(p.parent, exp.And) or p is condition)):
            continue
        rebuilt = _and_all(kept)
        if isinstance(holder, (exp.Where, exp.Having)):
            holder.set("this", rebuilt)
        else:
            holder.set("on", rebuilt)
    return tree


def _semi_joins_to_exists(tree: exp.Expression) -> exp.Expression:
    """``a LEFT SEMI JOIN b ON c`` keeps the rows of ``a`` for which some row of ``b`` satisfies ``c``:
    ``a WHERE EXISTS (SELECT 1 FROM b WHERE c)`` (``LEFT ANTI JOIN`` is ``NOT EXISTS``)."""

    for select in list(tree.find_all(exp.Select))[::-1]:
        joins = select.args.get("joins") or []
        kept, tests = [], []
        for join in joins:
            if join.args.get("side") == "LEFT" and join.args.get("kind") in ("SEMI", "ANTI") and join.args.get("on") is not None:
                source = join.this.copy()
                probe = exp.Select(expressions=[exp.Literal.number(1)]).from_(source)
                probe.set("where", exp.Where(this=join.args["on"].copy()))
                test = exp.Exists(this=probe)
                tests.append(test if join.args.get("kind") == "SEMI" else exp.Not(this=test))
            else:
                kept.append(join)
        if not tests:
            continue
        select.set("joins", kept or None)
        where = select.args.get("where")
        parts = ([where.this] if where is not None else []) + tests
        select.set("where", exp.Where(this=_and_all(parts)))
    return tree


_HAVING_COUNTER = itertools.count()


def _grouped_in_to_derived(tree: exp.Expression) -> exp.Expression:
    """``x IN (SELECT k FROM t GROUP BY k HAVING f(agg))`` reads the groups as a derived relation.

    ``IN (SELECT g.k FROM (SELECT k, agg AS a FROM t GROUP BY k) AS g WHERE f(a))``: the aggregate
    values live in the derived table, and the HAVING becomes an ordinary condition on them.
    """

    for node in list(tree.find_all(exp.In)):
        query = node.args.get("query")
        inner = query.this if isinstance(query, exp.Subquery) else query
        if not isinstance(inner, exp.Select) or not inner.args.get("group") or inner.args.get("having") is None:
            continue
        if any(inner.args.get(k) for k in ("distinct", "limit", "offset", "qualify", "order", "with_", "with")) or any(inner.find_all(exp.Window)):
            continue
        group = inner.args["group"]
        if extended_grouping(group):
            continue
        keys = [k.sql() for k in group.expressions]
        outputs = [(i.this if isinstance(i, exp.Alias) else i) for i in inner.expressions]
        if not outputs or any(o.sql() not in keys or any(o.find_all(exp.AggFunc)) for o in outputs):
            continue
        having = inner.args["having"].this.copy()
        aggregates: dict[str, str] = {}
        ok = True
        for call in list(having.find_all(exp.AggFunc)):
            if any(a is not call for a in call.find_all(exp.AggFunc)) or any(isinstance(n, (exp.Subquery, exp.Window)) for n in call.walk()):
                ok = False
                break
        if not ok or any(isinstance(n, (exp.Subquery, exp.Exists)) for n in having.walk()):
            continue
        counter = next(_HAVING_COUNTER)
        key_names = {k: f"kqk{counter}_{i}" for i, k in enumerate(keys)}
        alias = f"kqg{counter}"

        def lift(piece: exp.Expression) -> exp.Expression:
            if isinstance(piece, exp.AggFunc):
                name = aggregates.setdefault(piece.sql(), f"kqa{counter}_{len(aggregates)}")
                return exp.column(name, table=alias)
            if piece.sql() in key_names:
                return exp.column(key_names[piece.sql()], table=alias)
            return piece

        calls = {call.sql(): call.copy() for call in having.find_all(exp.AggFunc)}
        lifted = having.transform(lift)
        if any(isinstance(c, exp.Column) and c.table != alias for c in lifted.find_all(exp.Column)):
            continue  # HAVING reads something that is neither a key nor an aggregate
        derived = exp.Select(
            expressions=[exp.alias_(k.copy(), key_names[k.sql()]) for k in group.expressions]
            + [exp.alias_(calls[sql].copy(), name) for sql, name in aggregates.items()]
        )
        derived.set(FROM_KEY, (inner.args.get("from_") or inner.args.get("from")).copy())
        if inner.args.get("joins"):
            derived.set("joins", [j.copy() for j in inner.args["joins"]])
        if inner.args.get("where") is not None:
            derived.set("where", inner.args["where"].copy())
        derived.set("group", group.copy())
        outer = exp.Select(expressions=[exp.column(key_names[o.sql()], table=alias) for o in outputs])
        outer = outer.from_(exp.Subquery(this=derived, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        outer.set("where", exp.Where(this=lifted))
        node.set("query", exp.Subquery(this=outer))
    return tree


_WINDOW_COUNTER = itertools.count()


def _wrap_window_select(select: exp.Select) -> exp.Select | None:
    """A grouped select with window functions, kept whole as a derived table named ``kqw*``.

    Its windows run over its groups, so they cannot be split from the grouping the way
    ``_isolate_windows`` splits them from a plain FROM/WHERE. Wrapped whole, the prover keeps it
    opaque (``_opaque``): two queries agree on it when it reads alike on both sides. Only plain
    selects with distinct, named outputs qualify (no ORDER BY, LIMIT, DISTINCT or set operation).
    """

    if any(select.args.get(k) for k in ("order", "limit", "offset", "distinct", "qualify")) or not isinstance(
        select.parent, (exp.Subquery, exp.CTE, exp.From, exp.Join, type(None))
    ):
        return None
    names = []
    for item in select.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        names.append(item.alias_or_name)
    if any(not n for n in names) or len({n.lower() for n in names}) != len(names):
        return None
    alias = f"kqw{next(_WINDOW_COUNTER)}"
    outer = exp.Select(expressions=[exp.alias_(exp.column(n, table=alias), n) for n in names])
    outer.set(FROM_KEY, exp.From(this=exp.Subquery(this=select.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias)))))
    return outer


def _isolate_windows(tree: exp.Expression) -> exp.Expression:
    """Compute a select's window functions in a derived table over its FROM and WHERE.

    ``SELECT a, ROW_NUMBER() OVER (...) AS n FROM t WHERE c QUALIFY n = 1`` becomes
    ``SELECT d.a, d.n FROM (SELECT a AS c0, ROW_NUMBER() OVER (...) AS w0 FROM t WHERE c) AS d WHERE d.w0 = 1``:
    a window sees the rows left after WHERE, so the derived table is the window's input, and the
    outer select is plain. The prover keeps that derived table whole (see ``_opaque``), so two queries
    agree when their window computations read alike.
    """

    for select in list(tree.find_all(exp.Select))[::-1]:
        windows = [w for w in select.find_all(exp.Window) if w.find_ancestor(exp.Select) is select]
        if not windows or select.args.get("windows"):
            continue
        parent = select.parent
        if isinstance(parent, exp.Subquery) and (parent.alias or "").startswith("kqw"):
            continue
        if select.args.get("group") or select.args.get("having"):
            wrapped = _wrap_window_select(select)
            if wrapped is not None:
                if select is tree:
                    tree = wrapped
                else:
                    select.replace(wrapped)
            continue
        own_calls = [c for c in select.find_all(exp.AggFunc) if c.find_ancestor(exp.Select) is select and c.find_ancestor(exp.Window) is None]
        if own_calls or select.args.get("distinct") and select.args["distinct"].args.get("on"):
            continue
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
            continue
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None:
            continue
        qualify = select.args.get("qualify")
        aliases = {i.alias.lower() for i in select.expressions if isinstance(i, exp.Alias)}
        outer_parts = list(select.expressions) + ([qualify.this] if qualify is not None else []) + [
            o for o in (select.args.get("order").expressions if select.args.get("order") else [])
        ]

        def outside_window(column: exp.Column) -> bool:
            return column.find_ancestor(exp.Window) is None and column.find_ancestor(exp.Select) is select

        columns: dict[str, exp.Column] = {}
        for part in outer_parts:
            for column in part.find_all(exp.Column):
                if not outside_window(column) or (not column.table and column.name.lower() in aliases):
                    continue
                columns.setdefault(column.sql(), column)
        calls = {w.sql(): w for w in windows}
        counter = next(_WINDOW_COUNTER)
        alias = f"kqw{counter}"
        column_names = {sql: f"kqc{i}" for i, sql in enumerate(sorted(columns))}
        window_names = {sql: f"kqv{i}" for i, sql in enumerate(sorted(calls))}

        def swap(piece: exp.Expression) -> exp.Expression:
            if isinstance(piece, exp.Window) and piece.sql() in window_names:
                return exp.column(window_names[piece.sql()], table=alias)
            if isinstance(piece, exp.Column) and piece.sql() in column_names and piece.find_ancestor(exp.Window) is None:
                return exp.column(column_names[piece.sql()], table=alias)
            return piece

        inner = exp.Select(
            expressions=[exp.alias_(columns[sql].copy(), name) for sql, name in sorted(column_names.items())]
            + [exp.alias_(calls[sql].copy(), name) for sql, name in sorted(window_names.items())]
        )
        inner.set(FROM_KEY, from_.copy())
        if select.args.get("joins"):
            inner.set("joins", [j.copy() for j in select.args["joins"]])
        if select.args.get("where") is not None:
            inner.set("where", select.args["where"].copy())
        outer_items = [item.transform(swap) for item in select.expressions]
        # An output named by the source column keeps its name after the swap.
        for old, new in zip(select.expressions, outer_items):
            if isinstance(old, exp.Column) and not isinstance(new, exp.Alias):
                outer_items[outer_items.index(new)] = exp.alias_(new, old.name)
        outer = exp.Select(expressions=outer_items)
        outer.set(FROM_KEY, exp.From(this=exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias)))))
        if qualify is not None:
            named = {
                i.alias.lower(): outer_items[n].this.copy()
                for n, i in enumerate(select.expressions)
                if isinstance(i, exp.Alias) and isinstance(outer_items[n], exp.Alias)
            }
            lifted = qualify.this.transform(swap)
            lifted = lifted.transform(
                lambda c: named[c.name.lower()].copy() if isinstance(c, exp.Column) and not c.table and c.name.lower() in named else c
            )
            outer.set("where", exp.Where(this=lifted))
        if select.args.get("distinct"):
            outer.set("distinct", select.args["distinct"].copy())
        if select.args.get("order"):
            outer.set("order", select.args["order"].transform(swap))
        for key in ("limit", "offset"):
            if select.args.get(key):
                outer.set(key, select.args[key].copy())
        if select is tree:
            tree = outer
        else:
            select.replace(outer)
    return tree


def _peel_star_wrappers(tree: exp.Expression) -> exp.Expression:
    """``SELECT * FROM (q) a`` with no other clause is ``q`` itself, at the root of the statement.

    The wrapper returns exactly ``q``'s rows and columns, so a query that ends in
    ``ORDER BY .. LIMIT`` inside such wrappers (CTE chains of ``SELECT *``) is
    read with its limit at the top.
    """

    while isinstance(tree, exp.Select):
        from_ = tree.args.get("from_") or tree.args.get("from")
        if from_ is None or not isinstance(from_.this, exp.Subquery) or not isinstance(from_.this.this, exp.Query):
            return tree
        source = from_.this
        if source.args.get("lateral") or (source.args.get("alias") is not None and source.args["alias"].args.get("columns")):
            return tree
        if any(tree.args.get(k) for k in ("joins", "where", "group", "having", "qualify", "order", "limit", "offset", "distinct", "windows", "with_", "with", "laterals", "pivots")):
            return tree
        if len(tree.expressions) != 1:
            return tree
        item = tree.expressions[0]
        alias = source.alias_or_name.lower()
        if not (isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star) and item.table.lower() == alias and alias)):
            return tree
        if star_modified(item):
            return tree  # ``* EXCEPT / REPLACE ..`` lists other columns than ``q``
        tree = source.this.copy()
    return tree


def _declaring_cte(table: exp.Table, name: str, stop: exp.Expression) -> exp.CTE | None:
    """The CTE a reference to ``name`` at ``table`` reads, searching the WITH clauses out to ``stop``.

    The nearest WITH that defines the name wins, so an inner ``WITH q`` hides an outer one. Inside a
    CTE's own body only the CTEs before it are visible (all of them in a ``WITH RECURSIVE``).
    """

    child, parent = table, table.parent
    while parent is not None:
        if isinstance(parent, exp.CTE) and isinstance(parent.parent, exp.With):
            with_ = parent.parent
            for cte in with_.expressions:
                if cte is parent and not with_.args.get("recursive"):
                    break
                if cte.alias_or_name.lower() == name:
                    return cte
            # the WITH's own names were just searched; its owner must not search them again
            child, parent = with_, with_.parent
            if parent is None:
                return None
        with_ = parent.args.get("with_") or parent.args.get("with")
        if isinstance(with_, exp.With) and child is not with_:
            for cte in with_.expressions:
                if cte.alias_or_name.lower() == name:
                    return cte
        if parent is stop:
            return None
        child, parent = parent, parent.parent
    return None


def _inline_ctes(tree: exp.Expression) -> exp.Expression:
    """Replace each reference to a WITH table by the table's query as a derived table."""

    for with_ in list(tree.find_all(exp.With)):
        if with_.args.get("recursive"):
            continue
        owner = with_.parent
        if owner is None:
            continue
        bodies: dict[str, exp.Expression] = {}
        for cte in with_.expressions:
            name = cte.alias_or_name.lower()
            if cte.args.get("alias") is not None and cte.args["alias"].args.get("columns"):
                bodies = {}
                break
            bodies[name] = cte.this
        else:
            for cte in with_.expressions:
                name = cte.alias_or_name.lower()
                body = bodies[name]
                uses = [
                    t
                    for t in owner.find_all(exp.Table)
                    if not t.db and not t.catalog and t.name.lower() == name and _declaring_cte(t, name, owner) is cte
                ]
                # The body's own name is the real table there (``WITH t AS (SELECT * FROM t)``); copies placed now
                # are not revisited for it. Earlier tables of this WITH are already replaced inside the body.
                reads = free_reads(body) - {name} if uses else set()
                for table in uses:
                    if reads & visible_ctes(table, owner):
                        # A WITH nested around the use (or a later table of this WITH) defines a name the body
                        # reads as something else, which inlining would capture.
                        raise UnmodeledConstruct(f"WITH table {name} reads a name that a WITH around its use redefines")
                    derived = exp.Subquery(this=body.copy(), alias=exp.TableAlias(this=exp.to_identifier(table.alias or table.name)))
                    table.replace(derived)
            owner.set("with_", None)
            owner.set("with", None)
    return tree


def _natural(join: exp.Join) -> bool:
    return str(join.args.get("method") or "").upper() == "NATURAL"


def _using_to_on(tree: exp.Expression, schema: dict[str, list[str]] | None) -> exp.Expression:
    """``a JOIN b USING (k)`` is ``a JOIN b ON a.k = b.k`` with the merged column ``k`` read from ``a``
    (from ``b`` for a RIGHT JOIN, ``COALESCE(a.k, b.k)`` for FULL). ``SELECT *`` lists the merged columns first.
    ``NATURAL JOIN`` is ``USING`` over the column names both sides share (a cross join when they share none)."""

    if not schema:
        return tree
    schema = {k.lower(): [c.lower() for c in v] for k, v in schema.items()}

    def columns_of(source: exp.Expression) -> list[str] | None:
        if isinstance(source, exp.Table):
            return schema.get(".".join(p.name for p in source.parts).lower())
        if isinstance(source, exp.Subquery):
            return _select_names(source.this)
        return None

    for select in list(tree.find_all(exp.Select))[::-1]:
        joins = select.args.get("joins") or []
        if not any(j.args.get("using") is not None or _natural(j) for j in joins):
            continue
        from_ = select.args.get("from_") or select.args.get("from")
        sources = [from_.this] + [j.this for j in joins]
        for position, src in enumerate(sources):
            if isinstance(src, exp.Subquery) and not src.alias:
                src.set("alias", exp.TableAlias(this=exp.to_identifier(f"kumosql_using_{position}")))
        known = [columns_of(src) for src in sources]
        aliases = [(src.alias_or_name or "").lower() for src in sources]
        if from_ is None or any(k is None for k in known) or "" in aliases or len(set(aliases)) != len(aliases):
            continue
        stars = [i for i in select.expressions if isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star))]
        if stars and (len(stars) > 1 or not isinstance(stars[0], exp.Star) or star_modified(stars[0])):
            continue
        # The columns of the running result, as (name, expression) pairs, to expand a bare star.
        running = [(c, exp.column(c, table=exp.to_identifier(aliases[0]))) for c in known[0]]
        merged: dict[str, exp.Expression] = {}
        ok = True
        new_joins = []
        for index, join in enumerate(joins, start=1):
            using = join.args.get("using")
            side = join.args.get("side")
            if _natural(join) and using is None and join.args.get("on") is None:
                # NATURAL JOIN is USING over every column name both sides have.
                have = {c for c, _ in running}
                using = [exp.to_identifier(c) for c in known[index] if c in have]
                join = join.copy()
                join.set("method", None)
                if not using:
                    new_join = join.copy()
                    if side:
                        new_join.set("on", exp.true())
                    running += [(c, exp.column(c, table=exp.to_identifier(aliases[index]))) for c in known[index]]
                    new_joins.append(new_join)
                    continue
            if using is None:
                running += [(c, exp.column(c, table=exp.to_identifier(aliases[index]))) for c in known[index]]
                new_joins.append(join)
                continue
            if join.args.get("kind") in ("SEMI", "ANTI") or join.args.get("method") or join.args.get("on") is not None:
                ok = False
                break
            conditions = []
            first = []
            for ident in using:
                name = ident.name.lower()
                left = [expr for c, expr in running if c == name]
                if len(left) != 1 or name not in known[index]:
                    ok = False
                    break
                right = exp.column(name, table=exp.to_identifier(aliases[index]))
                conditions.append(exp.EQ(this=left[0].copy(), expression=right.copy()))
                value = {"RIGHT": right, "FULL": exp.Coalesce(this=left[0].copy(), expressions=[right.copy()])}.get(side, left[0])
                merged[name] = value
                first.append((name, value))
            if not ok:
                break
            names = {n for n, _ in first}
            running = first + [(c, e) for c, e in running if c not in names] + [
                (c, exp.column(c, table=exp.to_identifier(aliases[index]))) for c in known[index] if c not in names
            ]
            new_join = join.copy()
            new_join.set("using", None)
            new_join.set("on", _and_all(conditions))
            new_joins.append(new_join)
        if not ok:
            continue
        # Unqualified references to a merged column read the merged value (own scope only).
        for column in list(select.find_all(exp.Column)):
            if column.table or isinstance(column.this, exp.Star) or column.find_ancestor(exp.Select) is not select:
                continue
            if column.name.lower() in merged:
                column.replace(merged[column.name.lower()].copy())
        if stars:
            select.set(
                "expressions",
                [e.copy() if isinstance(e, exp.Column) and e.name.lower() == n else exp.alias_(e.copy(), n) for n, e in running],
            )
        select.set("joins", new_joins)
    return tree


def _drop_global_null_filter(select: exp.Select) -> exp.Expression | None:
    """``SELECT MIN(x), COUNT(*) FROM t WHERE x IS NOT NULL`` is ``SELECT MIN(x), COUNT(x) FROM t``.

    A global aggregate over one column ignores its NULLs already, so the guard only matters to
    ``COUNT(*)``, which becomes ``COUNT(x)``. With a ``GROUP BY`` a group of NULLs would vanish, so
    this holds only without one.
    """

    where = select.args.get("where")
    if where is None or select.args.get("group") or select.args.get("having") or not _no_extras(select, allow_group=False):
        return None
    parts = _conjuncts(where.this)
    for index, part in enumerate(parts):
        if not (isinstance(part, exp.Not) and isinstance(part.this, exp.Is) and isinstance(part.this.expression, exp.Null)):
            continue
        target = part.this.this
        if not isinstance(target, exp.Column) or isinstance(target.this, exp.Star):
            continue
        copy = select.copy()
        ok = bool(copy.expressions)
        for item in copy.expressions:
            expr = item.this if isinstance(item, exp.Alias) else item
            if isinstance(expr, exp.Count) and isinstance(expr.this, exp.Star) and not expr.args.get("distinct"):
                expr.set("this", target.copy())
            elif isinstance(expr, (exp.Sum, exp.Min, exp.Max, exp.Avg, exp.Count)) and isinstance(expr.this, exp.Column) and expr.this.sql() == target.sql():
                continue
            else:
                ok = False
                break
        if not ok:
            continue
        rest = [c for j, c in enumerate(parts) if j != index]
        copy.set("where", exp.Where(this=_and_all([c.copy() for c in rest])) if rest else None)
        return copy
    return None


def _lift_limit_derived(select: exp.Select) -> exp.Expression | None:
    """``SELECT id FROM (SELECT id, x FROM t ORDER BY x LIMIT 5)`` is ``SELECT id FROM t ORDER BY x LIMIT 5``.

    A select that only lists columns of a derived table keeps its rows, so the derived table's
    ``ORDER BY .. LIMIT`` can sit at the top, which the prover reads as a limit query.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or any(
        select.args.get(k) for k in ("where", "group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")
    ):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select) or select.find_ancestor(exp.Select) is not None:
        return None
    inner = source.this
    if inner.args.get("limit") is None or inner.args.get("order") is None or any(inner.args.get(k) for k in ("distinct", "qualify", "with_", "with")):
        return None
    if any(isinstance(e, exp.Star) for e in inner.expressions):
        return None
    alias = (source.alias or "").lower()
    by_name = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in by_name:
            return None
        by_name[name] = item
    items = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or (column.table and column.table.lower() != alias):
            return None
        origin = by_name.get(column.name.lower())
        if origin is None:
            return None
        value = origin.this if isinstance(origin, exp.Alias) else origin
        name = item.alias_or_name
        items.append(exp.alias_(value.copy(), name) if name else value.copy())
    kept = {item.alias_or_name.lower() for item in items}
    aliased = {n for n, item in by_name.items() if isinstance(item, exp.Alias)}
    if any(c.name.lower() in aliased and not c.table and c.name.lower() not in kept for c in inner.args["order"].find_all(exp.Column)):
        return None
    if rebinds_grouping_names(inner, items, clauses=("group", "having", "order")):
        return None
    result = inner.copy()
    result.set("expressions", items)
    if drops_global_aggregate(inner, result):
        return None
    return result


_INDICATOR_COUNTER = itertools.count()


def _left_join_indicator_to_exists(tree: exp.Expression, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression:
    """``a LEFT JOIN (SELECT k, 1 AS i FROM t WHERE c) AS d ON a.x = d.k WHERE d.i IS NOT NULL OR p`` is
    ``a WHERE EXISTS (SELECT 1 FROM t WHERE c AND a.x = t.k) OR p``.

    The join is a plain existence test when it matches at most one row of ``d`` (the equated columns
    cover a key of ``t``, or every column of ``d``'s ``GROUP BY``) and only the indicator column of ``d``
    is read, always as ``IS NOT NULL``. Then no row of ``a`` is duplicated and the indicator is non-NULL
    exactly when a row of ``t`` matches.
    """

    key_sets = {t.lower(): [frozenset(c.lower() for c in k) for k in ks if k] for t, ks in (keys or {}).items()}

    for select in list(tree.find_all(exp.Select))[::-1]:
        _apply_indicator_joins(select, key_sets)
    return tree


def _apply_indicator_joins(select: exp.Select, key_sets: dict[str, list[frozenset[str]]]) -> bool:
    fired, progress = False, True
    while progress:
        progress = False
        for join in list(select.args.get("joins") or []):
            if _indicator_join(select, join, key_sets):
                fired = progress = True
                break
    return fired


def _indicator_join_above(select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression | None:
    """``_left_join_indicator_to_exists`` once the select is normalized, also reading the indicator
    a few projections above the join (the derived tables are flattened into one select first)."""

    key_sets = {t.lower(): [frozenset(c.lower() for c in k) for k in ks if k] for t, ks in (keys or {}).items()}
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Subquery) or select.args.get("joins"):
        return None
    flat = _flatten_projections(select.copy())
    if flat is None or not flat.args.get("joins") or not _apply_indicator_joins(flat, key_sets):
        return None
    return flat


def _indicator_join(select: exp.Select, join: exp.Join, key_sets: dict[str, list[frozenset[str]]]) -> bool:
    source = join.this
    inner_join = not join.args.get("side") and join.args.get("kind") in (None, "", "INNER") and join.args.get("using") is None
    if not (inner_join or (join.args.get("side") == "LEFT" and not join.args.get("kind"))) or join.args.get("on") is None or not isinstance(source, exp.Subquery):
        return False
    inner, alias = source.this, source.alias
    if isinstance(inner, exp.Select) and isinstance((inner.args.get("from_") or inner.args.get("from") or exp.From()).this, exp.Subquery):
        inner = _flatten_projections(inner.copy()) or inner  # read through projection-only derived tables
    if not alias or not isinstance(inner, exp.Select) or inner.args.get("joins") or not isinstance(inner.args.get("from_") or inner.args.get("from"), exp.From):
        return False
    constant_distinct = plain_distinct(inner) and all(isinstance(e.this if isinstance(e, exp.Alias) else e, (exp.Literal, exp.Boolean)) for e in inner.expressions)
    if any(inner.args.get(k) for k in ("having", "limit", "offset", "qualify", "order", "with_", "with")) or (inner.args.get("distinct") and not constant_distinct) or any(inner.find_all(exp.Window, exp.Subquery, exp.AggFunc)):
        return False
    table = (inner.args.get("from_") or inner.args.get("from")).this
    if not isinstance(table, exp.Table) or table.args.get("joins"):
        return False
    base = table.name.lower()
    inner_alias = table.alias_or_name.lower()
    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        if not isinstance(item, exp.Alias) and not isinstance(item, exp.Column):
            return False
        outputs[item.alias_or_name.lower()] = item.this if isinstance(item, exp.Alias) else item
    indicators = {n for n, e in outputs.items() if isinstance(e, exp.Literal) or isinstance(e, exp.Boolean) and e.this}
    group = inner.args.get("group")
    group_columns = set() if constant_distinct and group is None else None
    if group is not None:
        if extended_grouping(group):
            return False
        group_columns = set()
        for e in group.expressions:
            if isinstance(e, exp.Column):
                group_columns.add(e.name.lower())
            elif not isinstance(e, (exp.Literal, exp.Boolean)):
                return False
    elif group_columns is None and any(not isinstance(e, (exp.Column, exp.Literal, exp.Boolean)) for e in outputs.values()):
        return False
    alias = alias.lower()
    if any((later.args.get("side") or "").upper() in ("RIGHT", "FULL") for later in select.args["joins"][select.args["joins"].index(join) + 1 :]):
        return False  # a later outer join NULL-pads the joined relation, and its indicator with it, but not an EXISTS test
    equated: dict[str, list[exp.Expression]] = {}
    for part in ([] if isinstance(join.args["on"], exp.Boolean) and join.args["on"].this else _conjuncts(join.args["on"])):
        if not isinstance(part, exp.EQ):
            return False
        sides = (part.this, part.expression)
        mine = [x for x in sides if isinstance(x, exp.Column) and x.table.lower() == alias]
        if len(mine) != 1:
            return False
        other = sides[1] if mine[0] is sides[0] else sides[0]
        if any(c.table.lower() in ("", alias) for c in other.find_all(exp.Column)) or any(isinstance(n, exp.Subquery) for n in other.walk()):
            return False
        column = outputs.get(mine[0].name.lower())
        if not isinstance(column, exp.Column) or column.table.lower() not in ("", inner_alias):
            return False
        equated.setdefault(column.name.lower(), []).append(other)  # every equality is a condition of the match
    if group_columns is not None:
        if not group_columns <= set(equated):
            return False
    elif not any(k <= set(equated) for k in key_sets.get(base, [])):
        return False
    # every other reference to the joined relation must be "indicator IS NOT NULL"
    uses = [c for c in select.find_all(exp.Column) if c.table.lower() == alias and c.find_ancestor(exp.Join) is not join]
    if any(not c.table and c.name.lower() in outputs for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select):
        return False  # an unqualified column could be one of the joined relation's
    if any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star) if star.find_ancestor(exp.Select) is select):
        return False  # SELECT * lists the joined relation's columns
    if inner_join and group is None:
        return False  # a keyed table or plain select joined for matching only: left as a join
    tests = []
    if inner_join:
        # an inner join to a set that is unique on the joined columns and read nowhere is an existence test
        if uses:
            return False
    else:
        for use in uses:
            parent = use.parent
            if use.name.lower() not in indicators or not isinstance(parent, exp.Is) or not isinstance(parent.expression, exp.Null):
                return False
            tests.append((parent.parent, False) if isinstance(parent.parent, exp.Not) else (parent, True))
        if not tests:
            return False
    fresh = f"kqj{next(_INDICATOR_COUNTER)}"
    fresh_table = table.copy()
    fresh_table.set("alias", exp.TableAlias(this=exp.to_identifier(fresh)))
    probe = exp.Select(expressions=[exp.Literal.number(1)]).from_(fresh_table)
    conditions = []
    if inner.args.get("where") is not None:
        own = inner.args["where"].this.copy()
        for column in own.find_all(exp.Column):
            if column.table.lower() in ("", inner_alias):
                column.set("table", exp.to_identifier(fresh))
        conditions.append(own)
    for name, others in equated.items():
        conditions.extend(exp.EQ(this=exp.column(name, table=fresh), expression=other.copy()) for other in others)
    where = _and_all(conditions)
    if where is not None:
        probe.set("where", exp.Where(this=where))
    for test, negate in tests:
        found = exp.Exists(this=probe.copy())
        test.replace(exp.Not(this=found) if negate else found)
    if inner_join:
        found = exp.Exists(this=probe.copy())
        existing = select.args.get("where")
        select.set("where", exp.Where(this=exp.And(this=existing.this, expression=found) if existing is not None else found))
    select.set("joins", [j for j in select.args["joins"] if j is not join] or None)
    return True


def _full_join_to_one_sided(select: exp.Select) -> exp.Expression | None:
    """``a FULL JOIN b ON c WHERE b.x > 1`` is ``a RIGHT JOIN b ON c WHERE b.x > 1``.

    The rows of ``a`` with no match carry NULL for every column of ``b``, and a comparison of such a
    column with a value rejects them; a test on ``a`` likewise leaves ``a LEFT JOIN b``. Tests on
    both sides leave an inner join. Only a single join with qualified columns is read this way.
    """

    joins = select.args.get("joins") or []
    where = select.args.get("where")
    from_ = select.args.get("from_") or select.args.get("from")
    if len(joins) != 1 or where is None or from_ is None or (joins[0].args.get("side") or "").upper() != "FULL" or joins[0].args.get("kind"):
        return None
    left, right = (from_.this.alias_or_name or "").lower(), (joins[0].this.alias_or_name or "").lower()
    if not left or not right or left == right:
        return None
    rejects = set()
    for part in _conjuncts(where.this):
        if not isinstance(part, _REJECTING) or not all(isinstance(side, (exp.Column, exp.Literal)) for side in (part.this, part.expression)):
            continue
        tables = {c.table.lower() for c in part.find_all(exp.Column)}
        if len(tables) == 1 and tables <= {left, right} and "" not in tables:
            rejects |= tables
    if not rejects:
        return None
    copy = select.copy()
    join = copy.args["joins"][0]
    if rejects == {left, right}:
        join.set("side", None)
    else:
        join.set("side", "RIGHT" if rejects == {right} else "LEFT")
    return copy


def _drop_unused_left_join(select: exp.Select, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression | None:
    """``a LEFT JOIN (SELECT k FROM t WHERE c GROUP BY k) AS d ON a.x = d.k`` is ``a`` when ``d`` is never read.

    A left join keeps every row of ``a`` once as long as it matches at most one row: the equated
    columns cover every ``GROUP BY`` column of ``d``, or a declared key of the table behind it.
    """

    joins = select.args.get("joins") or []
    key_sets = {t.lower(): [frozenset(c.lower() for c in k) for k in ks if k] for t, ks in (keys or {}).items()}
    for index, join in enumerate(joins):
        source = join.this
        if (join.args.get("side") or "").upper() != "LEFT" or join.args.get("kind") or join.args.get("on") is None:
            continue
        alias = (source.alias_or_name or "").lower()
        if not alias:
            continue
        if any(c.table.lower() == alias for c in select.find_all(exp.Column) if c.find_ancestor(exp.Join) is not join):
            continue
        if any(not c.table for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select and c.find_ancestor(exp.Join) is not join) or any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
            continue
        equated: set[str] = set()
        ok = True
        for part in _conjuncts(join.args["on"]):
            sides = (part.this, part.expression) if isinstance(part, exp.EQ) else ()
            mine = [x for x in sides if isinstance(x, exp.Column) and x.table.lower() == alias]
            if len(mine) != 1 or any(c.table.lower() in ("", alias) for x in sides if x is not mine[0] for c in x.find_all(exp.Column)):
                ok = False
                break
            equated.add(mine[0].name.lower())
        if not ok:
            continue
        if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
            inner = source.this
            group = inner.args.get("group")
            if group is None or extended_grouping(group) or inner.args.get("having") is not None or inner.args.get("limit") is not None:
                continue
            outputs = {e.alias_or_name.lower(): (e.this if isinstance(e, exp.Alias) else e) for e in inner.expressions}
            mapped = {outputs[name].sql() for name in equated if name in outputs and isinstance(outputs[name], exp.Column)}
            wanted = {e.sql() for e in group.expressions if not isinstance(e, (exp.Literal, exp.Boolean))}
            if not wanted or not wanted <= mapped:
                continue
        elif isinstance(source, exp.Table):
            if not any(k <= equated for k in key_sets.get(source.name.lower(), [])):
                continue
        else:
            continue
        copy = select.copy()
        remaining = [j for i, j in enumerate(copy.args["joins"]) if i != index]
        copy.set("joins", remaining or None)
        return copy
    return None


def _derived_key_outputs(inner: exp.Select) -> dict[str, exp.Expression] | None:
    """Output name -> value for the outputs of a DISTINCT or GROUP BY derived select that a filter commutes with.

    A filter on a value the select deduplicates or groups by can run before it: DISTINCT outputs, or
    the non-aggregate outputs that are GROUP BY keys (a HAVING drops whole groups, as such a filter
    does, so the two commute). ``None`` when the select is anything else.
    """

    if inner.args.get("having") is not None and inner.args.get("group") is None:
        return None
    if any(inner.args.get(k) for k in ("limit", "offset", "qualify", "windows", "with_", "with", "order")) or any(inner.find_all(exp.Window)):
        return None
    if any(isinstance(e, exp.Star) for e in inner.expressions):
        return None
    group = inner.args.get("group")
    if distinct_on(inner):
        return None  # one row per ON key, which a filter on another output would pick differently
    if (group is not None) == plain_distinct(inner):  # exactly one of DISTINCT, GROUP BY
        return None
    if extended_grouping(group):
        return None
    keys = {e.sql() for e in group.expressions} if group is not None else None
    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        value = item.this if isinstance(item, exp.Alias) else item
        if not name or name in outputs or any(isinstance(n, (exp.Subquery, exp.Window)) for n in value.walk()):
            return None
        if any(True for _ in value.find_all(exp.AggFunc)):
            continue
        if keys is None or value.sql() in keys:
            outputs[name] = value
    return outputs


def _push_filter_into_derived(select: exp.Select) -> exp.Expression | None:
    """``SELECT * FROM (SELECT DISTINCT k FROM t) AS d WHERE d.k > 1`` is ``.. FROM (SELECT DISTINCT k FROM t WHERE k > 1) AS d``.

    A condition on values that a derived DISTINCT or GROUP BY keeps unchanged filters the same rows
    before or after it, so it moves inside.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    where = select.args.get("where")
    if from_ is None or where is None or select.args.get("joins"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    outputs = _derived_key_outputs(inner)
    if not outputs:
        return None
    inner_from = inner.args.get("from_") or inner.args.get("from")
    if inner_from is None:
        return None
    alias = source.alias.lower()
    moved, kept = [], []
    for part in _conjuncts(where.this):
        columns = list(part.find_all(exp.Column))
        movable = (
            columns
            and not any(isinstance(n, (exp.Subquery, exp.Exists, exp.Window, exp.AggFunc)) for n in part.walk())
            and all(c.name.lower() in outputs and c.table.lower() in ("", alias) for c in columns)
        )
        (moved if movable else kept).append(part)
    if not moved:
        return None
    copy = select.copy()
    new_inner = copy.args.get("from_", copy.args.get("from")).this.this
    pushed = []
    for part in moved:
        part = part.copy()
        holder = exp.Select(expressions=[part])
        for column in list(holder.find_all(exp.Column)):
            column.replace(outputs[column.name.lower()].copy())
        pushed.append(holder.expressions[0])
    existing = new_inner.args.get("where")
    conditions = ([existing.this] if existing is not None else []) + pushed
    new_inner.set("where", exp.Where(this=_and_all(conditions)))
    copy.set("where", exp.Where(this=_and_all(kept)) if kept else None)
    return copy


def _unwrap_distinct_projection(select: exp.Select) -> exp.Expression | None:
    """``SELECT d.a, d.b FROM (SELECT DISTINCT a, b FROM t) AS d`` is the derived select: it lists every output once."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or any(
        select.args.get(k) for k in ("where", "group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")
    ):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if not inner.args.get("distinct") or any(inner.args.get(k) for k in ("group", "limit", "offset", "qualify", "order", "with_", "with")) or any(inner.find_all(exp.Window)):
        return None
    if any(isinstance(e, exp.Star) for e in inner.expressions):
        return None
    names = [e.alias_or_name.lower() for e in inner.expressions]
    if "" in names or len(set(names)) != len(names):
        return None
    alias = source.alias.lower()
    picked = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or (column.table and column.table.lower() != alias):
            return None
        picked.append(column.name.lower())
    if sorted(picked) != sorted(names):
        return None
    by_name = dict(zip(names, inner.expressions))
    new = inner.copy()
    items = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        origin = by_name[column.name.lower()]
        value = origin.this if isinstance(origin, exp.Alias) else origin
        items.append(exp.alias_(value.copy(), item.alias_or_name))
    new.set("expressions", items)
    return new


_DUPLICATE_INSENSITIVE = (exp.Min, exp.Max)


def _drop_redundant_distinct_source(select: exp.Select) -> exp.Expression | None:
    """``SELECT COUNT(DISTINCT x) FROM (SELECT x FROM t GROUP BY x) AS d`` is ``.. FROM (SELECT x FROM t) AS d``.

    Aggregates that ignore repeated values (MIN, MAX, and DISTINCT ones) see the same set of values with
    or without the derived table's deduplication, and so do the groups of an outer ``GROUP BY``.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or select.args.get("having") is not None:
        return None
    # An outer DISTINCT without aggregates or grouping ignores repeated rows of the source too.
    distinct = select.args.get("distinct")
    if distinct is not None and (distinct_on(select) or select.args.get("group")):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in ("having", "limit", "offset", "qualify", "order", "with_", "with")) or any(inner.find_all(exp.Window)):
        return None
    if distinct_on(inner):
        return None  # DISTINCT ON keeps one row per key, not every distinct row: it removes values, not only repeats
    group = inner.args.get("group")
    if (group is None) == (not plain_distinct(inner)):
        return None
    if extended_grouping(group):
        return None
    if any(isinstance(e, exp.Star) for e in inner.expressions) or any(c.find_ancestor(exp.Select) is inner for c in inner.find_all(exp.AggFunc)):
        return None
    if group is not None:
        values = [(e.this if isinstance(e, exp.Alias) else e).sql() for e in inner.expressions]
        if sorted(values) != sorted(g.sql() for g in group.expressions):
            return None
    aggregates = [a for a in select.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is select]
    if distinct is not None:
        if aggregates or any(select.find_all(exp.Window)) or any(n is not source for n in select.find_all(exp.Subquery, exp.Exists)):
            return None
        copy = select.copy()
        new_inner = copy.args.get("from_", copy.args.get("from")).this.this
        new_inner.set("distinct", None)
        new_inner.set("group", None)
        return copy
    if not aggregates:
        return None
    for call in aggregates:
        insensitive = isinstance(call, _DUPLICATE_INSENSITIVE) or isinstance(call.this, exp.Distinct) or bool(call.args.get("distinct"))
        if not insensitive or isinstance(call, exp.Window):
            return None
    outside = [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select and c.find_ancestor(exp.AggFunc) is None]
    keys = {g.sql() for g in select.args["group"].expressions} if select.args.get("group") else set()
    if select.args.get("group") is not None and any(g.find_ancestor(exp.AggFunc) for g in select.args["group"].expressions):
        return None
    for expression in select.expressions:
        value = expression.this if isinstance(expression, exp.Alias) else expression
        if not any(True for _ in value.find_all(exp.AggFunc)) and value.sql() not in keys and any(True for _ in value.find_all(exp.Column)):
            return None
    del outside
    copy = select.copy()
    new_inner = copy.args.get("from_", copy.args.get("from")).this.this
    new_inner.set("distinct", None)
    new_inner.set("group", None)
    return copy


def _name_derived_columns(tree: exp.Expression) -> exp.Expression:
    """A derived table's unnamed column (``MIN(x)``) gets a generated name, so ``SELECT *`` can list it.

    Output names are not compared and no query reads the engine's own spelling of such a name.
    """

    for source in list(tree.find_all(exp.Subquery)):
        inner = source.this
        if not source.alias or not isinstance(inner, exp.Select) or not isinstance(source.parent, (exp.From, exp.Join)):
            continue
        for index, item in enumerate(inner.expressions):
            if not isinstance(item, (exp.Alias, exp.Column, exp.Star)):
                item.replace(exp.alias_(item.copy(), f"kqc{index}"))
    return tree


def _lateral_joins(tree: exp.Expression) -> exp.Expression:
    """Read ``JOIN LATERAL (subquery) AS d`` as the plain join or scalar subquery it stands for.

    * A global-aggregate subquery (no ``GROUP BY``) always returns one row, so ``d.m`` is the scalar
      subquery ``(SELECT agg ..)`` wherever it is read; the join is dropped (``LEFT`` or ``INNER``).
    * A plain filter-and-project subquery under ``INNER JOIN LATERAL`` is a join whose ``ON`` is the
      correlated part of its ``WHERE``.
    """

    for select in list(tree.find_all(exp.Select))[::-1]:
        for join in list(select.args.get("joins") or []):
            lateral = join.this
            if not isinstance(lateral, exp.Lateral) or lateral.args.get("view") or not isinstance(lateral.this, exp.Subquery):
                continue
            body = lateral.this.this
            alias = lateral.alias
            kind = (join.args.get("kind") or "").upper()
            side = (join.args.get("side") or "").upper()
            on = join.args.get("on")
            # ON FALSE keeps no pair (an inner join is empty, a LEFT one pads with NULLs); only TRUE is the plain lateral read
            if not isinstance(body, exp.Select) or not alias or on is not None and not (isinstance(on, exp.Boolean) and on.this):
                continue
            if any(body.args.get(k) for k in ("distinct", "limit", "offset", "qualify", "windows", "with_", "with", "having", "order")) or any(body.find_all(exp.Window)):
                continue
            if any(isinstance(e, exp.Star) for e in body.expressions):
                continue
            names = [e.alias_or_name for e in body.expressions]
            if "" in names or len({n.lower() for n in names}) != len(names):
                continue
            inner_from = body.args.get("from_") or body.args.get("from")
            aggregated = body.args.get("group") is None and any(c.find_ancestor(exp.Select) is body for c in body.find_all(exp.AggFunc))
            if aggregated and (side == "LEFT" or kind == "INNER" or not kind and not side) and len(names) >= 1:
                values = {n.lower(): (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, body.expressions)}
                uses = [c for c in select.find_all(exp.Column) if c.table.lower() == alias.lower()]
                if not uses or any(c.name.lower() not in values for c in uses):
                    continue
                if any(c.find_ancestor(exp.Join) is join for c in uses):
                    continue
                # the one row comes from the aggregates: a used output without one, (SELECT 7 FROM u), has a row per row of u
                if not all(any(a.find_ancestor(exp.Select) is body for a in values[c.name.lower()].find_all(exp.AggFunc)) for c in uses):
                    continue
                for column in uses:
                    probe = body.copy()
                    probe.set("expressions", [values[column.name.lower()].copy()])
                    column.replace(exp.Subquery(this=probe))
                select.set("joins", [j for j in select.args["joins"] if j is not join] or None)
                continue
            if aggregated or body.args.get("group") is not None or kind not in ("INNER", "") or side:
                continue
            # filter-and-project subquery: its correlated conjuncts become the ON clause
            if any(isinstance(n, (exp.Subquery, exp.Exists)) for n in body.walk() if n is not body and not isinstance(n, exp.Select)):
                continue
            outer_names = {
                (t.alias_or_name or "").lower()
                for t in list(select.find_all(exp.Table)) + list(select.find_all(exp.Subquery))
                if t.find_ancestor(exp.Select) is select and t.alias_or_name
            }
            inner_names = {(t.alias_or_name or "").lower() for t in body.find_all(exp.Table) if t.find_ancestor(exp.Select) is body}
            outputs = {n.lower(): (e.this if isinstance(e, exp.Alias) else e) for n, e in zip(names, body.expressions)}
            where = body.args.get("where")
            parts = _conjuncts(where.this) if where is not None else []
            correlated, local = [], []
            for part in parts:
                touches = {c.table.lower() for c in part.find_all(exp.Column)}
                (correlated if touches & (outer_names - inner_names) else local).append(part)
            if any(c.table.lower() in (outer_names - inner_names) for e in body.expressions for c in e.find_all(exp.Column)):
                continue
            if any(c.table.lower() in (outer_names - inner_names) for part in local for c in part.find_all(exp.Column)):
                continue
            if not inner_from or (body.args.get("group") is not None):
                if correlated:
                    continue
            conditions, ok = [], True
            for part in correlated:
                part = part.copy()
                for column in list(part.find_all(exp.Column)):
                    if column.table.lower() in (outer_names - inner_names):
                        continue
                    match = next((n for n, v in outputs.items() if isinstance(v, exp.Column) and v.name.lower() == column.name.lower() and (not v.table or v.table.lower() == column.table.lower() or not column.table)), None)
                    if match is None:
                        ok = False
                        break
                    column.replace(exp.column(match, table=alias))
                if not ok:
                    break
                conditions.append(part)
            if not ok:
                continue
            flat = body.copy()
            flat.set("where", exp.Where(this=_and_all([p.copy() for p in local])) if local else None)
            derived = exp.Subquery(this=flat, alias=exp.TableAlias(this=exp.to_identifier(alias)))
            on = _and_all(conditions) if conditions else exp.true()
            join.set("this", derived)
            join.set("on", on)
            join.set("kind", "INNER")
    return tree


def _probe_and_nth_value(tree: exp.Expression) -> exp.Expression:
    """Two exact spellings: ``(SELECT 1 FROM x LIMIT 1) IS NOT NULL`` is ``EXISTS (SELECT 1 FROM x)``
    (``IS NULL`` is ``NOT EXISTS``), and ``NTH_VALUE(x, 1)`` is ``FIRST_VALUE(x)``."""

    def probe(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.NthValue):
            offset = node.args.get("offset")
            if isinstance(offset, exp.Literal) and not offset.is_string and offset.this == "1" and node.args.get("from_first") is not False:
                return exp.FirstValue(this=node.this.copy())
        test = node
        negate = False
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
            test, negate = node.this, True
        elif not (isinstance(node, exp.Is) and isinstance(node.expression, exp.Null)):
            return node
        subject = test.this
        body = subject.this if isinstance(subject, exp.Subquery) else None
        if not isinstance(body, exp.Select) or len(body.expressions) != 1:
            return node
        limit = body.args.get("limit")
        value = body.expressions[0]
        if limit is None or not isinstance(limit.expression, exp.Literal) or limit.expression.this != "1" or body.args.get("offset"):
            return node
        if not (isinstance(value, exp.Literal) and not value.is_string) or any(body.args.get(k) for k in ("group", "having", "distinct", "qualify", "windows", "with_", "with")):
            return node
        if any(body.find_all(exp.AggFunc, exp.Window)):
            return node
        flat = body.copy()
        flat.set("limit", None)
        flat.set("order", None)
        found = exp.Exists(this=flat)
        return found if negate else exp.Not(this=found)

    return tree.transform(probe)


def _except_of_same_table_filters(tree: exp.Expression, schema: dict[str, list[str]] | None, dialect: str = "bigquery") -> exp.Expression:
    """``SELECT * FROM t WHERE a EXCEPT SELECT * FROM t WHERE b`` is ``SELECT DISTINCT * FROM t WHERE a AND NOT COALESCE(b, FALSE)``.

    Both sides list every column of the same table, so a row is in the second side exactly when ``b`` holds
    for it (equal rows agree on any condition over their columns). Same in every qualifier: ``s1.t`` is not ``s2.t``.
    """

    if not schema:
        return tree
    columns = {t.lower(): [c.lower() for c in cs] for t, cs in schema.items()}

    def single(select: exp.Expression):
        if not isinstance(select, exp.Select) or any(select.args.get(k) for k in ("joins", "group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")):
            return None
        from_ = select.args.get("from_") or select.args.get("from")
        table = from_.this if from_ is not None else None
        if not isinstance(table, exp.Table) or table.args.get("joins") or table.name.lower() not in columns:
            return None
        where = select.args.get("where")
        if where is not None and any(isinstance(n, (exp.Subquery, exp.Exists, exp.Window, exp.AggFunc, exp.Rand)) for n in where.walk()):
            return None
        names = []
        for item in select.expressions:
            if not isinstance(item, exp.Column) or item.table.lower() not in ("", (table.alias_or_name or "").lower()):
                return None
            names.append(item.name.lower())
        if names != columns[table.name.lower()]:
            return None
        return table, where

    def step(node: exp.Expression) -> exp.Expression:
        if type(node) is not exp.Except or not node.args.get("distinct", True):
            return node
        left, right = single(node.this), single(node.expression)
        if left is None or right is None or not same_table(left[0], right[0], dialect):
            return node
        (a_table, a_where), (b_table, b_where) = left, right
        a_alias, b_alias = (a_table.alias_or_name or "").lower(), (b_table.alias_or_name or "").lower()
        condition = None
        if b_where is not None:
            test = b_where.this.copy()
            for column in test.find_all(exp.Column):
                if column.table.lower() in ("", b_alias):
                    column.set("table", exp.to_identifier(a_table.alias_or_name))
            condition = exp.Not(this=exp.Coalesce(this=exp.Paren(this=test), expressions=[exp.false()]))
        else:
            return node
        merged = node.this.copy()
        parts = ([a_where.this.copy()] if a_where is not None else []) + [condition]
        merged.set("where", exp.Where(this=_and_all(parts)))
        merged.set("distinct", exp.Distinct())
        return merged

    return tree.transform(step)


def _push_distinct_into_sources(select: exp.Select, schema: dict[str, list[str]] | None, keys: dict[str, list[tuple[str, ...]]] | None) -> exp.Expression | None:
    """``SELECT e.k FROM e JOIN d ON e.k = d.k GROUP BY e.k`` reads ``e`` as ``(SELECT k FROM e GROUP BY k)``.

    A select that only keeps distinct values cannot tell repeated rows of a source apart, so a source
    is replaced by its distinct projection onto the columns the select reads, unless a declared key
    is among them (then it has no repeats). The outer ``GROUP BY`` stays.
    """

    if not schema:
        return None
    group = select.args.get("group")
    distinct = select.args.get("distinct")
    if (group is None) == (not distinct) or not select.args.get("joins"):
        return None
    if distinct_on(select) or extended_grouping(group):
        return None
    if any(select.args.get(k) for k in ("having", "limit", "offset", "qualify", "windows", "with_", "with", "order")):
        return None
    if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Exists, exp.Star)) for n in select.walk() if n is not select and not isinstance(n, (exp.Table,))):
        return None
    for join in select.args["joins"]:
        if join.args.get("side") or join.args.get("kind") not in (None, "", "INNER", "CROSS"):
            return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        return None
    sources = [from_.this] + [j.this for j in select.args["joins"]]
    if any(not isinstance(src, exp.Table) for src in sources):
        return None
    columns = {t.lower(): [c.lower() for c in cs] for t, cs in schema.items()}
    key_sets = {t.lower(): [frozenset(c.lower() for c in k) for k in ks if k] for t, ks in (keys or {}).items()}
    aliases = [(src.alias_or_name or "").lower() for src in sources]
    if len(set(aliases)) != len(aliases) or "" in aliases:
        return None
    where = select.args.get("where")
    own: dict[str, list[exp.Expression]] = {a: [] for a in aliases}
    rest = []
    for part in (_conjuncts(where.this) if where is not None else []):
        tables = {c.table.lower() for c in part.find_all(exp.Column)}
        if len(tables) == 1 and next(iter(tables)) in own:
            own[next(iter(tables))].append(part)
        else:
            rest.append(part)

    def used_by(alias: str, skip_own: bool) -> set[str] | None:
        names: set[str] = set()
        for column in select.find_all(exp.Column):
            if not column.table or column.table.lower() not in aliases:
                return None
            if skip_own and any(column is c for part in own[alias] for c in part.find_all(exp.Column)):
                continue
            if column.table.lower() == alias:
                names.add(column.name.lower())
        return names

    used: dict[str, set[str]] = {}
    for alias in aliases:
        names = used_by(alias, True)
        if names is None:
            return None
        used[alias] = names
    chosen = []
    for src, alias in zip(sources, aliases):
        table = src.name.lower()
        if table not in columns or not used[alias] or not used[alias] <= set(columns[table]):
            continue
        if any(k <= used[alias] for k in key_sets.get(table, [])) or used[alias] == set(columns[table]):
            continue
        chosen.append((src, alias))
    if not chosen:
        return None
    copy = select.copy()
    new_sources = [copy.args.get("from_", copy.args.get("from")).this] + [j.this for j in copy.args["joins"]]
    removed: list[exp.Expression] = []
    for (src, alias), target in zip([(s_, a_) for s_, a_ in zip(sources, aliases)], new_sources):
        if (src, alias) not in chosen:
            continue
        names = sorted(used[alias])
        inner_table = src.copy()
        inner_table.set("alias", exp.TableAlias(this=exp.to_identifier(f"kqs{next(_VALUES_COUNTER)}")))
        inner = exp.Select(expressions=[exp.alias_(exp.column(n, table=inner_table.alias), n) for n in names]).from_(inner_table)
        inner.set("distinct", exp.Distinct())
        moved = []
        for part in own[alias]:
            part = part.copy()
            for column in part.find_all(exp.Column):
                column.set("table", exp.to_identifier(inner_table.alias))
            moved.append(part)
        if moved:
            inner.set("where", exp.Where(this=_and_all(moved)))
        target.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        removed.extend(own[alias])
    leftover = [p for p in _conjuncts(copy.args["where"].this) if not any(p.sql() == r.sql() for r in removed)] if copy.args.get("where") is not None else []
    copy.set("where", exp.Where(this=_and_all(leftover)) if leftover else None)
    return copy


def _qualified_outer_columns(columns: list[exp.Column], outer: exp.Select, probe: exp.Select, declared: dict[str, set[str]]) -> list[exp.Column] | None:
    """``columns`` of ``outer`` qualified so that they still read ``outer`` once moved into ``probe``'s WHERE.

    Inside the probe an unqualified ``deptno`` would read the probe's own ``deptno``, and ``e.deptno``
    would read a probe source also called ``e``. Each column is qualified by the outer source that
    declares it, and a probe source of that name gets a fresh alias. None when that cannot be done safely.
    """

    sources = _sources_of(outer)
    refs = []
    for column in columns:
        qualifier = column.table
        if not qualifier:
            owners = [
                s for s in sources
                if isinstance(s, exp.Table) and column.name.lower() in declared.get(s.name.lower(), set())
            ]
            if len(owners) != 1:
                return None
            qualifier = owners[0].alias_or_name
        refs.append(exp.column(column.name, table=qualifier))
    for qualifier in {r.table.lower() for r in refs}:
        clashing = [s for s in _sources_of(probe) if s.alias_or_name.lower() == qualifier]
        for source in clashing:
            if not isinstance(source, exp.Table):
                return None
            own = [c for c in probe.find_all(exp.Column) if c.table.lower() == qualifier]
            if any(c.find_ancestor(exp.Select) is not probe for c in own):
                return None  # a nested query reads that name too
            fresh = f"kq_probe{next(_INDICATOR_COUNTER)}"
            source.set("alias", exp.TableAlias(this=exp.to_identifier(fresh)))
            for c in own:
                c.set("table", exp.to_identifier(fresh))
    return refs


def _select_list_in_to_exists(tree: exp.Expression, not_null: dict[str, frozenset[str]] | None) -> exp.Expression:
    """``SELECT x IN (SELECT y FROM t WHERE c)`` is ``EXISTS (SELECT 1 FROM t WHERE c AND y = x)`` when neither side is NULL.

    Declared NOT NULL columns on both sides make the test two-valued (never UNKNOWN), and
    ``CASE WHEN EXISTS (..) THEN TRUE ELSE FALSE END`` is the ``EXISTS`` itself.
    """

    declared = {k.lower(): {c.lower() for c in v} for k, v in (not_null or {}).items()}

    def case_of_exists(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Case) and node.this is None and len(node.args.get("ifs") or []) == 1:
            branch = node.args["ifs"][0]
            default = node.args.get("default")
            if isinstance(branch.this, exp.Exists) and isinstance(branch.args.get("true"), exp.Boolean) and branch.args["true"].this and isinstance(default, exp.Boolean) and not default.this:
                return branch.this
        return node

    tree = tree.transform(case_of_exists)
    if not declared:
        return tree
    for node in list(tree.find_all(exp.In)):
        query = node.args.get("query")
        inner = query.this if isinstance(query, exp.Subquery) else None
        if isinstance(inner, exp.Select) and isinstance((inner.args.get("from_") or inner.args.get("from") or exp.From()).this, exp.Subquery):
            inner = _flatten_projections(inner.copy()) or inner  # read through projection-only derived tables
        lefts = node.this.expressions if isinstance(node.this, exp.Tuple) else [node.this]
        if not isinstance(inner, exp.Select) or node.args.get("expressions") or len(inner.expressions) != len(lefts) or node.args.get("unnest"):
            continue
        if any(inner.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "windows", "with_", "with")) or any(inner.find_all(exp.Window, exp.AggFunc)):
            continue
        walker, in_list = node.parent, True
        while walker is not None and not isinstance(walker, exp.Select):
            if isinstance(walker, (exp.Where, exp.Having, exp.Join, exp.Group, exp.Order)):
                in_list = False
                break
            walker = walker.parent
        if not in_list or walker is None or not all(isinstance(left, exp.Column) for left in lefts):
            continue
        if _global_aggregate(walker):
            continue  # a bare column of an aggregate without GROUP BY reads NULL over no rows (SQLite, MySQL)
        outer_known = _declared_not_null(node, declared)
        inner_known = _declared_not_null(inner.expressions[0], declared)
        values = [(item.this if isinstance(item, exp.Alias) else item) for item in inner.expressions]
        if not all(left.sql() in outer_known for left in lefts) or not all(isinstance(v, exp.Column) and v.sql() in inner_known for v in values):
            continue
        probe = inner.copy()
        outer_refs = _qualified_outer_columns(lefts, walker, probe, declared)
        if outer_refs is None:
            continue
        values = [(item.this if isinstance(item, exp.Alias) else item) for item in probe.expressions]
        matches = [exp.EQ(this=v.copy(), expression=ref) for v, ref in zip(values, outer_refs)]
        probe.set("expressions", [exp.Literal.number(1)])
        where = probe.args.get("where")
        probe.set("where", exp.Where(this=_and_all(([where.this] if where is not None else []) + matches)))
        node.replace(exp.Exists(this=probe))
    return tree.transform(case_of_exists)


def _inline_constant_columns(select: exp.Select) -> exp.Expression | None:
    """A derived select's constant output (``TRUE AS g``) is read as that constant by the select over it.

    Every row of the derived table carries the same value, so ``d.g`` is the literal wherever it is read.
    """

    if any(j.args.get("side") or j.args.get("kind") in _OUTER for j in select.args.get("joins") or []):
        return None  # an outer join can null-extend the derived table: its constants are then NULL
    changed = False
    for source in _sources_of(select):
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        constants = {}
        for item in inner.expressions:
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(item, exp.Alias) and isinstance(value, (exp.Boolean, exp.Null)) or (isinstance(item, exp.Alias) and isinstance(value, exp.Literal) and not value.is_string):
                constants[item.alias.lower()] = value
        if not constants:
            continue
        alias = source.alias.lower()
        sources = [x for x in _sources_of(select) if (x.alias_or_name or "")]
        for column in list(select.find_all(exp.Column)):
            if column.find_ancestor(exp.Select) is not select and not any(a.find_ancestor(exp.Select) is select for a in [column]):
                pass
            name = column.name.lower()
            if name not in constants:
                continue
            if column.table.lower() == alias or (not column.table and len(sources) == 1):
                # a reference in a nested subquery could be shadowed; stay on this select's own scope
                if column.find_ancestor(exp.Select) is not select:
                    continue
                value = constants[name].copy()
                if isinstance(column.parent, exp.Group):
                    # A constant key never splits a group, and a literal there would read as a column
                    # ordinal: drop it, keeping GROUP BY TRUE when nothing else is grouped.
                    group = column.parent
                    column.pop()
                    if not group.expressions:
                        group.set("expressions", [exp.true()])
                else:
                    column.replace(exp.alias_(value, column.name) if column.parent is select else value)
                changed = True
    return select if changed else None


def _constant_counts(select: exp.Select) -> exp.Expression | None:
    """``COUNT(NULL)`` counts nothing: 0, in a select that has another aggregate or a ``GROUP BY``.

    (Alone in a global aggregate it is the one row the select returns, so it stays.)
    """

    calls = [c for c in select.find_all(exp.AggFunc) if c.find_ancestor(exp.Select) is select]
    zero = [c for c in calls if isinstance(c, exp.Count) and isinstance(c.this, exp.Null) and not c.args.get("distinct")]
    if not zero or (len(calls) == len(zero) and select.args.get("group") is None):
        return None
    for call in zero:
        call.replace(exp.Literal.number(0))
    return select


def _inline_constant_source(select: exp.Select) -> exp.Expression | None:
    """``FROM (SELECT 10 AS x, 1 AS y) AS t`` is one row of constants: read ``t.x`` as ``10`` and drop the source."""

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    joins = select.args.get("joins") or []
    if any(j.args.get("side") or j.args.get("kind") in _OUTER for j in joins):
        return None  # an outer join can null-extend the constant row
    candidates = [("from", from_.this, None)] + [("join", j.this, j) for j in joins]
    for position, source, join in candidates:
        if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
            continue
        inner = source.this
        if inner.args.get("from_") or inner.args.get("from") or any(inner.args.get(k) for k in ("where", "group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "joins", "with_", "with")):
            continue
        values: dict[str, exp.Expression] = {}
        for item in inner.expressions:
            value = item.this if isinstance(item, exp.Alias) else item
            name = item.alias_or_name.lower() if isinstance(item, exp.Alias) else ""
            if not name or name in values or any(True for _ in value.find_all(exp.Column, exp.Subquery, exp.Star, exp.AggFunc, exp.Window, exp.Anonymous)) or isinstance(value, exp.Func) and not isinstance(value, (exp.Cast,)):
                values = {}
                break
            values[name] = value
        if not values or len(values) != len(inner.expressions):
            continue
        if join is not None and (join.args.get("side") or join.args.get("kind") not in (None, "", "INNER", "CROSS")):
            continue
        if position == "from" and joins and (joins[0].args.get("side") or joins[0].args.get("kind") not in (None, "", "INNER", "CROSS") or not isinstance(joins[0].this, (exp.Table, exp.Subquery))):
            continue
        alias = source.alias.lower()
        others = [x for x in _sources_of(select) if x is not source]
        copy = select.copy()
        scope = copy
        for column in list(scope.find_all(exp.Column)):
            if column.find_ancestor(exp.Select) is not scope:
                continue
            if column.name.lower() in values and (column.table.lower() == alias or (not column.table and not others)):
                value = values[column.name.lower()].copy()
                column.replace(exp.alias_(value, column.name) if column.parent is scope else value)
        # any remaining mention of the alias (in a nested subquery) keeps the source
        if any(c.table.lower() == alias for c in copy.find_all(exp.Column)):
            continue
        new_joins = list(copy.args.get("joins") or [])
        if join is not None:
            index = joins.index(join)
            on = new_joins[index].args.get("on")
            del new_joins[index]
            extra = [on] if on is not None else []
        elif new_joins:
            first = new_joins.pop(0)
            copy.set(FROM_KEY, exp.From(this=first.this))
            on = first.args.get("on")
            extra = [on] if on is not None else []
        else:
            copy.set(FROM_KEY, None)
            extra = []
        copy.set("joins", new_joins or None)
        if extra:
            existing = copy.args.get("where")
            copy.set("where", exp.Where(this=_and_all(([existing.this] if existing is not None else []) + extra)))
        return copy
    return None


def _provably_empty(node: exp.Expression) -> bool:
    """A select with ``WHERE FALSE`` (and no aggregate, which would still return a row) or a set operation of such."""

    while isinstance(node, exp.Subquery) and not node.args.get("limit") and not node.args.get("order"):
        node = node.this
    if isinstance(node, exp.Select):
        return _where_false_is_empty(node)
    if isinstance(node, exp.Union):
        return _provably_empty(node.this) and _provably_empty(node.expression)
    if isinstance(node, exp.Intersect):
        return _provably_empty(node.this) or _provably_empty(node.expression)
    if isinstance(node, exp.Except):
        return _provably_empty(node.this)
    return False


def _fold_empty_set_operands(tree: exp.Expression) -> exp.Expression:
    """``a INTERSECT <empty>`` and ``<empty> EXCEPT a`` have no rows: ``SELECT NULL, .. WHERE FALSE`` with ``a``'s width."""

    def first_select(node: exp.Expression):
        while isinstance(node, (exp.Subquery, exp.Union, exp.Intersect, exp.Except)):
            node = node.this
        return node if isinstance(node, exp.Select) else None

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.Intersect, exp.Except)) and _provably_empty(node):
            first = first_select(node)
            if first is None or any(isinstance(e, exp.Star) for e in first.expressions):
                return node
            items = [exp.alias_(exp.Null(), e.alias_or_name) if e.alias_or_name else exp.Null() for e in first.expressions]
            return exp.Select(expressions=items, where=exp.Where(this=exp.false()))
        return node

    return tree.transform(step)


def _drop_group_in_membership_tests(tree: exp.Expression) -> exp.Expression:
    """``x IN (SELECT k FROM t GROUP BY k)`` is ``x IN (SELECT k FROM t)``, and the same for ``EXISTS``.

    A membership or existence test does not see repeated rows. Only a ``GROUP BY`` of exactly the selected
    columns with no aggregate and no ``HAVING`` is dropped (a select with a ``GROUP BY`` always has at
    least one row per group, unlike a global aggregate, so the rows tested are the same set).
    """

    for node in list(tree.find_all(exp.In, exp.Exists)):
        query = node.args.get("query") if isinstance(node, exp.In) else node.this
        inner = query.this if isinstance(query, exp.Subquery) else query
        if not isinstance(inner, exp.Select):
            continue
        group = inner.args.get("group")
        if group is None or inner.args.get("having") is not None or extended_grouping(group):
            continue  # (the empty grouping set gives a row even over no input)
        if any(inner.args.get(k) for k in ("limit", "offset", "qualify", "windows", "distinct", "with_", "with")) or any(inner.find_all(exp.AggFunc, exp.Window)):
            continue
        outputs = {(e.this if isinstance(e, exp.Alias) else e).sql() for e in inner.expressions}
        keys = {g.sql() for g in group.expressions}
        if isinstance(node, exp.In) and outputs != keys:
            continue
        if isinstance(node, exp.Exists) and not outputs <= keys and not all(isinstance(e, (exp.Literal, exp.Star)) or (e.this if isinstance(e, exp.Alias) else e).sql() in keys for e in inner.expressions):
            continue
        inner.set("group", None)
    return tree


def _is_null_test(node: exp.Expression) -> bool:
    """``x IS NULL`` or ``x IS NOT NULL``: a Boolean that is never NULL."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Not):
        node = node.this
    return isinstance(node, exp.Is) and isinstance(node.expression, exp.Null)


def _fold_boolean_constants(tree: exp.Expression) -> exp.Expression:
    """Exact Boolean identities on never-NULL tests: ``(x IS NULL) IS NULL`` is FALSE, ``TRUE OR y`` is TRUE,
    ``FALSE AND y`` is FALSE, and ``CAST(x IS NULL AS INTEGER)`` (0 or 1) compared with a number outside
    ``{0, 1}`` is decided."""

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.Cast, exp.Timestamp)) and isinstance(node.this, exp.Null):
            return exp.Null()  # a typed NULL is NULL (output types are not compared)
        if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null) and _is_null_test(node.this):
            return exp.false()
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null) and _is_null_test(node.this.this):
            return exp.true()
        if isinstance(node, exp.Or) and any(isinstance(side, exp.Boolean) and side.this for side in (node.this, node.expression)):
            return exp.true()
        if isinstance(node, exp.And) and any(isinstance(side, exp.Boolean) and not side.this for side in (node.this, node.expression)):
            return exp.false()
        if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)):
            cast, number = node.this, _int_value(node.expression)
            flipped = False
            if number is None:
                cast, number, flipped = node.expression, _int_value(node.this), True
            while isinstance(cast, exp.Paren):
                cast = cast.this
            if number is not None and isinstance(cast, exp.Cast) and _is_null_test(cast.this) and cast.to.is_type(*exp.DataType.INTEGER_TYPES):
                if number in (0, 1):
                    return node
                kind = type(node)
                if flipped:
                    kind = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}.get(kind, kind)
                above = number > 1  # every value (0 or 1) is below the number; otherwise above it
                holds = {exp.GT: not above, exp.GTE: not above, exp.LT: above, exp.LTE: above, exp.EQ: False, exp.NEQ: True}[kind]
                return exp.true() if holds else exp.false()
        return node

    return tree.transform(step)


def _drop_empty_null_extended_side(select: exp.Select) -> exp.Expression | None:
    """``a LEFT JOIN (SELECT .. WHERE FALSE) AS e ON c`` is ``a`` with NULL for every column of ``e``
    (and ``RIGHT JOIN`` the other way round): an empty side never matches."""

    joins = select.args.get("joins") or []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or len(joins) != 1 or any(isinstance(star, exp.Star) and not isinstance(star.parent, exp.Count) for star in select.find_all(exp.Star)):
        return None
    join = joins[0]
    side = (join.args.get("side") or "").upper()
    if side not in ("LEFT", "RIGHT") or join.args.get("kind") or join.args.get("on") is None:
        return None
    empty, kept = (join.this, from_.this) if side == "LEFT" else (from_.this, join.this)
    if not isinstance(empty, exp.Subquery) or not empty.alias or not _provably_empty(empty):
        return None
    alias = empty.alias.lower()
    copy = select.copy()
    for column in list(copy.find_all(exp.Column)):
        if column.table.lower() == alias:
            if column.find_ancestor(exp.Select) is not copy:
                return None
            column.replace(exp.alias_(exp.Null(), column.name) if column.parent is copy else exp.Null())
    if not isinstance(kept, (exp.Table, exp.Subquery)):
        return None
    copy.set(FROM_KEY, exp.From(this=copy.args.get("from_", copy.args.get("from")).this if side == "LEFT" else copy.args["joins"][0].this))
    copy.set("joins", None)
    return copy


def _drop_exists_witnessed_by_join(select: exp.Select, dialect: str = "bigquery") -> exp.Expression | None:
    """``WHERE EXISTS (SELECT 1 FROM d WHERE d.k = o.x)`` is implied by a joined ``d AS s`` with ``s.k = o.x``.

    The row of ``s`` is a witness for every row the select returns, so the test is TRUE and can go. Only
    inner joins (nothing is null-extended), and only a test that is a plain conjunction of such equalities.
    The joined table must be the probed one in every qualifier: ``s1.d`` is no witness for ``s2.d``.
    """

    joins = select.args.get("joins") or []
    where = select.args.get("where")
    from_ = select.args.get("from_") or select.args.get("from")
    if where is None or from_ is None or not joins or any(j.args.get("side") or j.args.get("kind") in _OUTER or j.args.get("using") is not None for j in joins):
        return None
    sources = _sources_of(select)
    if any(not isinstance(src, (exp.Table, exp.Subquery)) for src in sources):
        return None
    conditions = list(_conjuncts(where.this))
    for join in joins:
        if join.args.get("on") is not None:
            conditions.extend(_conjuncts(join.args["on"]))
    equalities = set()
    for condition in conditions:
        if isinstance(condition, exp.EQ) and isinstance(condition.this, exp.Column) and isinstance(condition.expression, exp.Column):
            a, b = condition.this, condition.expression
            equalities.add((a.table.lower(), a.name.lower(), b.table.lower(), b.name.lower()))
            equalities.add((b.table.lower(), b.name.lower(), a.table.lower(), a.name.lower()))
    kept = []
    dropped = False
    for part in _conjuncts(where.this):
        if not isinstance(part, exp.Exists) or not isinstance(part.this, exp.Select):
            kept.append(part)
            continue
        probe = part.this
        table = (probe.args.get("from_") or probe.args.get("from"))
        table = table.this if table is not None else None
        if (
            not isinstance(table, exp.Table)
            or probe.args.get("joins")
            or any(probe.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "windows", "with_", "with"))
            or any(probe.find_all(exp.AggFunc, exp.Window, exp.Subquery))
            or probe.args.get("where") is None
        ):
            kept.append(part)
            continue
        pairs = []
        for cond in _conjuncts(probe.args["where"].this):
            if not isinstance(cond, exp.EQ) or not isinstance(cond.this, exp.Column) or not isinstance(cond.expression, exp.Column):
                pairs = None
                break
            inner_alias = (table.alias_or_name or "").lower()
            a, b = cond.this, cond.expression
            if a.table.lower() == inner_alias and b.table.lower() != inner_alias and b.table:
                pairs.append((a.name.lower(), b.table.lower(), b.name.lower()))
            elif b.table.lower() == inner_alias and a.table.lower() != inner_alias and a.table:
                pairs.append((b.name.lower(), a.table.lower(), a.name.lower()))
            else:
                pairs = None
                break
        witnessed = False
        if pairs:
            for src in sources:
                if isinstance(src, exp.Table) and same_table(src, table, dialect) and src.alias_or_name:
                    alias = src.alias_or_name.lower()
                    if all((alias, col, o_table, o_col) in equalities for col, o_table, o_col in pairs):
                        witnessed = True
                        break
        if witnessed:
            dropped = True
        else:
            kept.append(part)
    if not dropped:
        return None
    copy = select.copy()
    copy.set("where", exp.Where(this=_and_all([k.copy() for k in kept])) if kept else None)
    return copy


def _single_row_source(select: exp.Select) -> exp.Expression | None:
    """A select over ``(SELECT .. LIMIT 1)`` sees at most one row, so grouping or ``DISTINCT`` in an aggregate does nothing.

    ``SELECT k FROM (.. LIMIT 1) AS d GROUP BY k`` is ``SELECT k FROM (.. LIMIT 1) AS d``, and
    ``SUM(DISTINCT k)`` is ``SUM(k)``. A global aggregate keeps its row either way, so it is left alone.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins"):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    limit = source.this.args.get("limit")
    if limit is None or not isinstance(limit.expression, exp.Literal) or limit.expression.this != "1" or source.this.args.get("offset"):
        return None
    changed = False
    copy = select.copy()
    for call in list(copy.find_all(exp.AggFunc)):
        if call.find_ancestor(exp.Select) is copy and isinstance(call.this, exp.Distinct) and len(call.this.expressions) == 1:
            call.set("this", call.this.expressions[0].copy())
            changed = True
    group = copy.args.get("group")
    if group is not None and not extended_grouping(group) and copy.args.get("having") is None:
        has_aggregate = any(c.find_ancestor(exp.Select) is copy for c in copy.find_all(exp.AggFunc))
        keys = {g.sql() for g in group.expressions}
        outputs = {(e.this if isinstance(e, exp.Alias) else e).sql() for e in copy.expressions}
        if not has_aggregate and outputs <= keys:
            copy.set("group", None)
            changed = True
    return copy if changed else None


def _group_by_to_distinct(select: exp.Select) -> exp.Expression | None:
    """``SELECT a, b FROM (x UNION ALL y) GROUP BY a, b`` (no aggregate, no HAVING) is ``SELECT DISTINCT a, b FROM ..``."""

    group = select.args.get("group")
    if group is None or select.args.get("having") or select.args.get("distinct") or any(
        select.args.get(k) for k in ("qualify", "windows", "with_", "with", "limit", "offset", "order")
    ):
        return None
    if extended_grouping(group):
        return None
    if any(c.find_ancestor(exp.Select) is select for c in select.find_all(exp.AggFunc)) or any(select.find_all(exp.Window)):
        return None
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
        return None
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or not isinstance(from_.this, exp.Subquery) or not isinstance(from_.this.this, exp.Union):
        return None  # only worth it over a derived union, where DISTINCT reads as UNION
    keys = {g.sql() for g in group.expressions}
    values = {(e.this if isinstance(e, exp.Alias) else e).sql() for e in select.expressions}
    if keys != values or any(isinstance(g, exp.Literal) and not g.is_string for g in group.expressions):
        return None
    copy = select.copy()
    copy.set("group", None)
    copy.set("distinct", exp.Distinct())
    return copy


def _parenthesize_boolean(tree: exp.Expression) -> exp.Expression:
    """Wrap an ``OR`` under ``AND`` (or ``NOT``) in parentheses; sqlglot prints trees without adding them.

    Whatever rule built the tree, the printed text then reads back with the same precedence.
    """

    tree = tree.copy()
    for node in list(tree.find_all(exp.Or, exp.And)):
        if isinstance(node, exp.Or) and isinstance(node.parent, (exp.And, exp.Not)):
            node.replace(exp.Paren(this=node.copy()))
        elif isinstance(node, exp.And) and isinstance(node.parent, exp.Not):
            node.replace(exp.Paren(this=node.copy()))
    return tree


def _parenthesize_set_operations(tree: exp.Expression) -> exp.Expression:
    """Keep the shape of nested set operations through the text the prover re-reads.

    ``a INTERSECT (b UNION ALL c)`` prints without parentheses, and read back it is
    ``(a INTERSECT b) UNION ALL c``. An operand needs them unless it binds tighter (an INTERSECT under a
    UNION or EXCEPT), is the left operand of the same operator, or is a UNION on the right of a UNION
    of the same kind.
    """

    for node in list(tree.find_all(exp.SetOperation)):
        for side in ("this", "expression"):
            child = node.args.get(side)
            if isinstance(child, exp.Select) and any(child.args.get(k) for k in ("order", "limit", "offset")):
                child.replace(exp.Subquery(this=child.copy()))  # unwrapped, its ORDER BY .. LIMIT reads as the whole operation's
                continue
            if not isinstance(child, exp.SetOperation):
                continue
            same = type(child) is type(node) and bool(child.args.get("distinct")) == bool(node.args.get("distinct"))
            if isinstance(child, exp.Intersect) and not isinstance(node, exp.Intersect):
                continue
            if same and (side == "this" or isinstance(node, (exp.Union, exp.Intersect))):
                continue
            child.replace(exp.Subquery(this=child.copy()))
    return tree


def _flatten_unions(tree: exp.Expression) -> exp.Expression:
    """``(A UNION B) UNION C`` is ``A UNION B UNION C`` (a UNION over any union, a UNION ALL over a UNION ALL),
    and a select that lists every column of a derived union is that union."""

    def step(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Union) or type(node) is not exp.Union:
            return node
        changed = False
        for side in ("this", "expression"):
            operand = node.args[side]
            while isinstance(operand, exp.Subquery) and not operand.alias and not operand.args.get("order") and not operand.args.get("limit"):
                operand = operand.this
            if isinstance(operand, exp.Select) and not operand.args.get("joins") and not operand.args.get("where") and not any(
                operand.args.get(k) for k in ("group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")
            ):
                from_ = operand.args.get("from_") or operand.args.get("from")
                source = from_.this if from_ is not None else None
                if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Union) and type(source.this) is exp.Union:
                    names = _select_names(source.this)
                    wanted = [
                        e.name.lower() for e in operand.expressions
                        if isinstance(e, exp.Column) and not isinstance(e.this, exp.Star)
                        and (not e.table or e.table.lower() == (source.alias or "").lower())
                    ]
                    if names is not None and wanted == names and len(wanted) == len(operand.expressions):
                        operand = source.this
            if isinstance(operand, exp.Union) and type(operand) is exp.Union and not operand.args.get("order") and not operand.args.get("limit"):
                if node.args.get("distinct") or not operand.args.get("distinct"):
                    if operand is not node.args[side]:
                        pass
                    node.set(side, operand.copy())
                    changed = True
            elif operand is not node.args[side] and isinstance(operand, exp.Select):
                pass
        if changed:
            # Drop the parentheses a nested union left behind: the operand now is the union itself.
            for side in ("this", "expression"):
                operand = node.args[side]
                if isinstance(operand, exp.Subquery) and isinstance(operand.this, exp.Union) and not operand.alias:
                    node.set(side, operand.this)
        return node

    return tree.transform(step)


def _unwrap_projection(select: exp.Select) -> exp.Expression | None:
    """``SELECT d.a, d.b FROM (SELECT a, b, c FROM t GROUP BY ..) AS d`` is the derived select with those outputs.

    A select that only lists columns of its one derived table keeps every row of it.
    """

    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or select.args.get("joins") or any(
        select.args.get(k) for k in ("where", "group", "having", "distinct", "order", "limit", "offset", "qualify", "windows", "with_", "with")
    ):
        return None
    source = from_.this
    if not isinstance(source, exp.Subquery) or not source.alias or not isinstance(source.this, exp.Select):
        return None
    inner = source.this
    if any(inner.args.get(k) for k in ("distinct", "limit", "offset", "qualify", "windows", "with_", "with")) or any(inner.find_all(exp.Window)):
        return None
    if not (inner.args.get("group") or any(c.find_ancestor(exp.Select) is inner for c in inner.find_all(exp.AggFunc))):
        return None
    if any(isinstance(e, exp.Star) for e in inner.expressions):
        return None
    by_name = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in by_name:
            return None
        by_name[name] = item
    alias = source.alias.lower()
    items = []
    for item in select.expressions:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star) or (column.table and column.table.lower() != alias):
            return None
        origin = by_name.get(column.name.lower())
        if origin is None:
            return None
        value = origin.this if isinstance(origin, exp.Alias) else origin
        name = item.alias_or_name
        items.append(exp.alias_(value.copy(), name) if name else value.copy())
    # An ORDER BY of the inner select would read output aliases that may be gone; groups have none here.
    if inner.args.get("order") or rebinds_grouping_names(inner, items):
        return None
    result = inner.copy()
    result.set("expressions", items)
    if _global_aggregate(inner) and not _global_aggregate(result):
        return None  # the outer select drops every aggregate: unwrapping would lose the one-row result
    return result


def _in_over_union(tree: exp.Expression) -> exp.Expression:
    """``x IN (SELECT a FROM p UNION ALL SELECT b FROM q)`` is ``x IN (SELECT a FROM p) OR x IN (SELECT b FROM q)``.

    A match in either branch is a match in the union and an unknown stays unknown, so the three-valued
    result agrees (and so does ``NOT IN`` as the negation). Every union in the tree must be bare: a
    ``LIMIT`` over the whole union (or ``BY NAME``) changes which rows it has, and the split would lose it.
    """

    def step(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.In) or node.args.get("expressions") or node.args.get("unnest"):
            return node
        query = node.args.get("query")
        if not isinstance(query, exp.Subquery) or type(query.this) is not exp.Union or any(v for k, v in query.args.items() if k != "this"):
            return node
        branches, stack = [], [query.this]
        while stack:
            part = stack.pop()
            if _plain_union(part):
                stack.extend([part.expression, part.this])
            elif isinstance(part, exp.Select):
                branches.append(part)
            else:
                return node
        if len(branches) < 2 or any(isinstance(n, (exp.Subquery, exp.Select)) for n in node.this.walk()):
            return node
        tests = [exp.In(this=node.this.copy(), query=exp.Subquery(this=b.copy())) for b in branches]
        result = tests[0]
        for test in tests[1:]:
            result = exp.Or(this=result, expression=test)
        return exp.Paren(this=result)

    return tree.transform(step)


# sqlglot 26 has no EndsWith node; ENDS_WITH there parses as a plain function call.
_PREFIX_SUFFIX_TESTS = tuple(t for t in (exp.StartsWith, getattr(exp, "EndsWith", None)) if t is not None)


def _bigquery_sugar(tree: exp.Expression) -> exp.Expression:
    """``COUNTIF(c)`` is ``COUNT(CASE WHEN c THEN 1 END)``; ``SAFE_DIVIDE(a, b)`` is ``IF(b = 0, NULL, a / b)``;
    ``STARTS_WITH(x, 'p')`` is ``x LIKE 'p%'`` (and ``ENDS_WITH`` ``'%p'``) for a pattern without wildcards."""

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.DPipe):
            return exp.Concat(expressions=[node.this.copy(), node.expression.copy()])
        if isinstance(node, (exp.Lower, exp.Upper)) and isinstance(node.this, exp.Trim) and not any(node.this.args.get(k) for k in ("expression", "position", "collation")):
            return exp.Trim(this=type(node)(this=node.this.this.copy()))
        if isinstance(node, exp.CountIf):
            return exp.Count(this=exp.Case(ifs=[exp.If(this=node.this.copy(), true=exp.Literal.number(1))]))
        if isinstance(node, _PREFIX_SUFFIX_TESTS):
            pattern = node.expression
            if isinstance(pattern, exp.Literal) and pattern.is_string and not set(pattern.name) & set("%_\\"):
                text = pattern.name + "%" if isinstance(node, exp.StartsWith) else "%" + pattern.name
                return exp.Like(this=node.this.copy(), expression=exp.Literal.string(text))
        if isinstance(node, exp.SafeDivide):
            zero = exp.EQ(this=node.expression.copy(), expression=exp.Literal.number(0))
            return exp.Case(
                ifs=[exp.If(this=zero, true=exp.Null())],
                default=exp.Div(this=node.this.copy(), expression=node.expression.copy()),
            )
        return node

    for _ in range(3):
        before = tree.sql()
        tree = tree.transform(step)
        if tree.sql() == before:
            break
    return tree


def _lowercase_columns(tree: exp.Expression) -> exp.Expression:
    """Column names are case-insensitive: ``t.EMPNO`` and ``t.empno`` are the same column.

    A name that is also an output alias somewhere keeps its spelling, because aliases are
    compared as written when output names matter.
    """

    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    aliases |= {t.alias.lower() for t in tree.find_all(exp.TableAlias) if t.name}
    for column in tree.find_all(exp.Column):
        identifier = column.this
        if isinstance(identifier, exp.Identifier) and identifier.name.lower() not in aliases and identifier.name != identifier.name.lower():
            column.set("this", exp.Identifier(this=identifier.name.lower(), quoted=identifier.args.get("quoted")))
    return tree


def _is_number(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string


def _resolve_ordinals(tree: exp.Expression, group_by: bool) -> exp.Expression:
    """Spell ``GROUP BY 2`` and ``ORDER BY 2`` of a select as the second output's expression.

    Rewrites substitute constants into these clauses (a derived ``2 AS c`` read in ``ORDER BY d.c``), and
    a number printed there would be read back as a position. With every position spelled out first, a
    number left in them at the end is a constant (see ``_constant_keys``). A position that cannot be
    spelled out (a star, an aggregate or window in ``GROUP BY``) is declined. ``group_by=False`` leaves
    ``GROUP BY`` alone (Calcite's constants, already dropped).
    """

    for select in list(tree.find_all(exp.Select)):
        clauses = [(select.args.get("order"), False)] + ([(select.args.get("group"), True)] if group_by else [])
        for clause, grouping in clauses:
            if clause is None or not any(_is_number(k.this if isinstance(k, exp.Ordered) else k) for k in clause.expressions):
                continue
            outputs = select.expressions
            for key in list(clause.expressions):
                value = key.this if isinstance(key, exp.Ordered) else key
                if not _is_number(value) or not re.fullmatch(r"\d+", value.this):
                    continue
                position = int(value.this)
                if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in outputs):
                    raise UnmodeledConstruct("a column position next to a star select list is not modeled")
                if not 1 <= position <= len(outputs):
                    raise UnmodeledConstruct(f"column position {position} is out of range")
                target = outputs[position - 1]
                target = target.this if isinstance(target, exp.Alias) else target
                if grouping and any(isinstance(n, (exp.AggFunc, exp.Window)) for n in target.walk()):
                    raise UnmodeledConstruct("GROUP BY position of an aggregate")
                value.replace(target.copy())
    return tree


def _constant_keys(tree: exp.Expression) -> exp.Expression:
    """A number left in a select's ``GROUP BY`` or ``ORDER BY`` is a constant: write it ``TRUE``.

    Printed as a number it would be read as a column position; a constant key never splits a group
    or changes an order, and ``GROUP BY TRUE`` keeps a grouping of constants a grouping.
    """

    for select in list(tree.find_all(exp.Select)):
        for clause in (select.args.get("group"), select.args.get("order")):
            if clause is None:
                continue
            keys = [k for k in clause.expressions if not _is_number(k.this if isinstance(k, exp.Ordered) else k)]
            if keys and clause is select.args.get("order"):
                clause.set("expressions", keys)  # a constant sort key ahead of others orders nothing
                continue
            for key in list(clause.expressions):
                value = key.this if isinstance(key, exp.Ordered) else key
                if _is_number(value):
                    value.replace(exp.true())
    return tree


def _drop_constant_groupings(tree: exp.Expression) -> exp.Expression:
    """Calcite reads ``GROUP BY 4, x`` with 4 as a constant: it never splits a group, so it is dropped.

    A grouping made only of constants keeps one group when the input has rows and none when it has
    none, which is ``GROUP BY TRUE`` (dropping it altogether would make a global aggregate, one row
    on empty input).
    """

    for select in list(tree.find_all(exp.Select)):
        group = select.args.get("group")
        if not group or extended_grouping(group):
            continue  # ROLLUP (4) is two grouping sets, one of them empty
        items = group.expressions

        def constant(node: exp.Expression) -> bool:
            return not any(node.find_all(exp.Column, exp.Subquery, exp.Anonymous, exp.Rand, exp.Window)) and not any(
                isinstance(n, exp.Func) and not isinstance(n, (exp.Cast, exp.Coalesce)) for n in node.walk()
            )

        kept = [e for e in items if not constant(e)]
        if len(kept) == len(items):
            continue
        group.set("expressions", kept or [exp.true()])
    return tree


def _derived_output_names(query: exp.Expression) -> list[str] | None:
    """The output names of a query (its first branch for a set operation), ``None`` with a star."""

    while isinstance(query, (exp.Subquery, exp.SetOperation)):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    names = []
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        names.append(item.alias_or_name.lower())
    return names


def _keeps_names(before: list[str], after: list[str] | None) -> bool:
    """Whether a rewrite kept every output name (an unnamed output may gain a name)."""

    if after is None:
        return True  # a star lists the same sources' columns
    return len(before) == len(after) and all(not old or old == new for old, new in zip(before, after))


def limit_rule(select: exp.Select, types: dict[str, dict[str, str]] | None = None, dialect: str = "bigquery") -> exp.Expression | None:
    """:func:`kumosql.limit_rules.limit_rule`, except on a ``DISTINCT ON`` select.

    Those rules drop an ORDER BY whose order no reader sees, but under DISTINCT ON the ORDER BY picks the
    row kept for each key, so it changes the rows themselves.
    """

    return None if distinct_on(select) else _limit_rule(select, types, dialect)


@serialized
def normalize(
    sql: str,
    *,
    schema: dict[str, list[str]] | None = None,
    dialect: str = "bigquery",
    not_null: dict[str, frozenset[str]] | None = None,
    keys: dict[str, list[tuple[str, ...]]] | None = None,
    types: dict[str, dict[str, str]] | None = None,
    group_by_constants: bool = False,
    keyed_distinct: int = 0,
    foreign_keys: dict[str, list[tuple]] | None = None,
    _assumptions: set[str] | None = None,
) -> str:
    """Rewrite ``sql`` with the bag-semantics identities above (``dialect`` in and out).

    ``group_by_constants`` reads a literal in ``GROUP BY`` as a constant, as Calcite does, instead of a
    column ordinal (see ``_drop_constant_groupings``). ``keyed_distinct`` drops a ``DISTINCT`` that outputs a
    NOT NULL key (``keyed_rules.drop_keyed_distinct``), and at 2 also strips duplicate removal from derived tables
    under a duplicate-blind select (``dedup_join_rules.strip_distinct_sources``); these are later attempts, since applying them on one side
    can hide a match the first attempt finds.
    """

    tree = expand_alias_columns(check_modeled(canonical_negation(strip_positions(sqlglot.parse_one(sql, read=dialect)))), schema)
    if group_by_constants:
        tree = _drop_constant_groupings(tree)
    tree = _resolve_ordinals(tree, group_by=not group_by_constants)
    tree = _lowercase_columns(tree)
    tree = _inline_ctes(tree)
    tree = rewrite_bigquery_constructs(tree, schema, types, dialect)  # pre-pass: UNNEST of literals, STRUCT fields
    tree = inline_named_windows(tree)
    from .nonnull_any import rewrite_nonnull_any

    tree = rewrite_quantified(tree, schema, not_null, keys)
    tree = _peel_star_wrappers(tree)
    tree = trim_redundant_row_clauses(tree)
    tree = _bigquery_sugar(tree)
    tree = using_to_on_unqualified(_using_to_on(tree, schema))
    tree = drop_subsumed_like(tree)
    tree = _semi_joins_to_exists(tree)
    tree = exists_over_aggregate(tree)
    tree = fold_grouped_count_cases(tree, not_null)
    tree = _left_join_indicator_to_exists(tree, keys)
    tree = _grouped_in_to_derived(tree)
    tree = _in_over_union(tree)
    tree = expand_grouping_sets(tree)
    for select in list(tree.find_all(exp.Select))[::-1]:
        replacement = collapse_grouping_expansion(select) or collapse_counted_intersection(select)
        if replacement is not None:
            if select is tree:
                tree = replacement
            else:
                select.replace(replacement)
    tree = grouping_sets_to_union(tree)
    tree = _drop_group_in_membership_tests(tree)
    tree = _fold_dates(extract_to_ranges(tree))
    tree = _fold_boolean_constants(_fold_constants(tree))
    tree = _fold_trivia(tree)
    tree = _fold_null_guards(tree, not_null)
    tree = _select_list_in_to_exists(tree, not_null)
    tree = _values_to_union(tree)
    tree = _name_derived_columns(tree)
    if schema:
        tree = _using_to_on(_expand_stars(tree, schema), schema)  # USING over a derived table read once its stars are known
    tree = _except_of_same_table_filters(tree, schema, dialect)
    tree = _probe_and_nth_value(tree)
    tree = _name_derived_columns(_lateral_joins(tree))
    if schema:
        tree = _expand_stars(tree, schema)
    tree = _isolate_windows(canonical_windows(drop_unread_windows(tree), types))
    if schema:
        # name each bare column's source before any rewrite reads a derived table as its base table, whose
        # other columns would otherwise capture (or make ambiguous) a bare column of another source
        tree = _checked_qualification(
            tree, lambda t: _qualify_correlated_columns(_qualify_outer_join_columns(t, schema), schema), schema, dialect, "algebraic_qualification"
        )
    tree = merge_distinct_partitions(tree)

    types_map = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in (types or {}).items()}

    def qualify(select: exp.Select) -> exp.Expression | None:
        if not schema:
            return None
        copy = select.copy()
        _checked_qualification(copy, lambda t: _qualify_outer_join_columns(t, schema), schema, dialect, "algebraic_select_qualification")
        return copy if copy.sql() != select.sql() else None

    def step(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Subquery):
            return _inline_projection(node, schema) or node
        if isinstance(node, exp.SetOperation):
            return merge_same_source(node) or set_operation_to_exists(node) or node
        if isinstance(node, exp.Select):
            # a derived table's output names are how the query around it reads it, so a rule that renames
            # them would silently redirect those reads (to another column of the same name, say)
            names = _derived_output_names(node) if isinstance(node.parent, (exp.Subquery, exp.CTE)) else None
            snapshot = node.copy() if names is not None else None  # rules rewrite in place
            quantified = rewrite_nonnull_any(node.copy(), schema, not_null, types, _assumptions)
            if quantified.sql() != node.sql() and (names is None or _keeps_names(names, _derived_output_names(quantified))):
                return quantified
            projected = normalize_projected_in(node, not_null)
            if projected is not None and (names is None or _keeps_names(names, _derived_output_names(projected))):
                return projected
            singleton = singleton_count_sum(node, keys, types_map, _assumptions)
            if singleton is not None and (names is None or _keeps_names(names, _derived_output_names(singleton))):
                return singleton
            constrained = lift_keyed_set_join(node, keys, not_null, types_map) or normalize_key_counts(node, keys) or keyed_join_to_exists(node, keys, not_null)
            if constrained is not None:
                if names is None or _keeps_names(names, _derived_output_names(constrained)):
                    return constrained
                fresh = snapshot.copy()
                if node.parent is not None:
                    node.replace(fresh)
                node = fresh
            for rule in (null_contradiction, lambda sel: nullable_lateral_boolean_group(sel, schema, not_null), distribute_over_constant_union, collapse_named_counted_intersection, lambda sel: distinct_rules(sel, schema), _mean_times_count, _single_row_source, lambda sel: _drop_exists_witnessed_by_join(sel, dialect), _drop_empty_null_extended_side, _inline_constant_source, _constant_counts, _inline_constant_columns, lambda sel: _push_distinct_into_sources(sel, schema, keys), _push_filter_into_derived, push_filter_into_set_operation, _unwrap_distinct_projection, _drop_redundant_distinct_source, left_join_to_inner, lambda sel: join_rewrites(sel, schema, not_null, foreign_keys), strengthen_under_outer_on, _full_join_to_one_sided, lambda sel: _drop_unused_left_join(sel, keys), lambda sel: decorrelation_step(sel, not_null, schema, dialect), lambda sel: _decorrelate_aggregate(sel, schema), lambda sel: _decorrelate_select_list(sel, schema), lambda sel: _inline_expression_projection(sel, schema), _inline_expression_projection, _prune_derived, _distinct_over_union_all, _merge_spj_source, _fold_filter_into_grouping, _merge_outer_right_filter, lambda sel: move_exists_into_padded_side(sel, schema), lambda sel: _drop_derived_null_guard(sel, not_null or {}), lambda sel: _pull_up_exists(sel, schema), lambda sel: _drop_implied_exists(sel, schema), _flatten_join_source, lambda sel: outer_join_rules(sel, keys), qualify, _order_grouped_columns, fold_literal_int_div, lambda sel: _fold_identity_casts(sel, types_map, dialect), lambda sel: fold_casts_and_constant_cases(sel, types_map, dialect), lambda sel: _shifted_sums(sel, types_map), _wrap_outer_join_aggregate, lambda sel: limit_rule(sel, types_map, dialect), _lift_limit_derived, _group_by_to_distinct, _unwrap_projection, key_having_to_where, window_rules, _collapse_aggregate, _drop_global_null_filter, _roll_up_aggregate, _regroup_distinct, lambda sel: regroup_arithmetic(sel, (_collapse_aggregate, _roll_up_aggregate, _regroup_distinct)), _split_aggregates, _distribute, unnest_grouped_source, flatten_grouped_join, lambda sel: pull_up_aggregate(sel, keys), lambda sel: read_back_eager_sums(sel, not_null), _key_aggregates, rewrite_aggregates, lambda sel: remove_keyed_grouping(sel, keys, not_null), lambda sel: drop_fk_join(sel, keys, not_null, foreign_keys), drop_unread_outer_join, strengthen_derived_outer_join, lambda sel: drop_grouped_sum_coalesce(sel, not_null or {}), lift_derived_expressions, lambda sel: _indicator_join_above(sel, keys), split_distinct_select, grouped_outer_join_rules, lambda sel: propagate_constant_correlations(sel, types_map), lambda sel: propagate_grouped_join_facts(sel, types_map), lambda sel: expose_correlated_key_groups(sel, keys, not_null or {}, types_map), lambda sel: exists_constant_rules(sel, schema, not_null, foreign_keys, types_map), collapse_constant_regroup, regroup_tuple_count, sum_of_grouped_counts,
                    fold_boolean_identities,
                    relax_membership_dedup,
            ):
                rewritten = rule(node)
                if rewritten is not None:
                    if names is not None and not _keeps_names(names, _derived_output_names(rewritten)):
                        fresh = snapshot.copy()
                        if node.parent is not None:
                            node.replace(fresh)
                        node = fresh
                        continue
                    return rewritten
            if keyed_distinct:
                return drop_keyed_distinct(node, keys, not_null) or (keyed_distinct >= 2 and strip_distinct_sources(node)) or node
        return node

    for _ in range(16):
        before = tree.sql(dialect="bigquery")
        tree = _select_list_in_to_exists(_fold_null_guards(_fold_count_coalesce(_fold_empty_set_operands(_flatten_unions(normalize_set_operations(_fold_boolean_constants(fold_string_literals(_fold_constants(propagate_empty(recombine_partitions(tree)).transform(step)))))))), not_null), not_null)
        from .passthrough_sources import remove_passthrough_sources

        tree = remove_passthrough_sources(tree, schema)
        from .counted_membership import rewrite_counted_membership

        tree = rewrite_counted_membership(tree, schema, not_null, _assumptions)
        if tree.sql(dialect="bigquery") == before:
            break
    for subquery in list(tree.find_all(exp.Subquery)):
        if subquery.find_ancestor(exp.Select) is not None:
            replacement = _canonicalize_union_source(subquery)
            if replacement is not None:
                subquery.replace(replacement)
    tree = expose_set_identity(tree, schema)
    return faithful_sql(parenthesize_is_operands(_parenthesize_boolean(_parenthesize_set_operations(_constant_keys(canonical_empty(tree))))), dialect)


@refuse_misread_proofs
@serialized
def prove_equivalent_algebraic(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Normalize both queries algebraically, then run the SMT prover on the result.

    On MySQL a proof that needs different strings compared with an untyped column to differ must also hold with the strings
    read as the numbers they convert to (``kumosql.numeric_column_reading``).
    See ``_prove_equivalent_algebraic_checked`` for the rest.
    """

    return numeric_column_reading.checked(
        _prove_equivalent_algebraic_checked, left_sql, right_sql, kwargs, lambda reason: SmtEquivalenceResult(SmtStatus.NOT_PROVEN, reason)
    )


def _prove_equivalent_algebraic_checked(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Normalize both queries algebraically, then run the SMT prover on the result.

    With ``search_counterexample=True`` an unproven pair the solver cannot refute is
    run on databases built for it (:mod:`kumosql.executed_refutation`); a database
    on which the results differ comes back as a ``NOT_EQUIVALENT`` counterexample.

    With ``conditional=True`` a pair that is not proven is retried under facts taken from the queries
    (NOT NULL columns, unique keys, foreign keys); a proof that needs some of them comes back as
    ``PROVEN_CONDITIONALLY`` with the minimal ``conditions`` (:mod:`kumosql.conditional_equivalence`).
    A proof over a set operation whose branch types are not all known carries
    ``set_operation_types.ASSUMPTION``; one whose branches have different known types is declined.
    Text that cannot be tokenized or nests too deeply is ``NOT_PROVEN``, never an exception.
    """

    dialect = kwargs.get("dialect", "bigquery")
    try:
        left_sql, right_sql = (string_number_literals.normalize(sql, dialect, kwargs.get("types")) for sql in (left_sql, right_sql))
        if dialect == "bigquery" and (invalid_literal(left_sql) or invalid_literal(right_sql)):
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "unsupported: a single-quoted literal holds a line break (not valid GoogleSQL)")
        unknown_type = dialect == "bigquery" and (invalid_type_name(left_sql) or invalid_type_name(right_sql))
        if unknown_type:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: BigQuery would reject the query: {unknown_type}")
        masked = masked_template_problem(left_sql, right_sql, dialect=dialect or "bigquery")
        if masked:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, masked)
        result = _prove_equivalent_algebraic(left_sql, right_sql, **kwargs)
        if result.proven and (unchecked_types(left_sql, kwargs.get("types"), dialect) or unchecked_types(right_sql, kwargs.get("types"), dialect)):
            result = dataclasses.replace(result, assumptions=tuple(dict.fromkeys(tuple(result.assumptions) + (SET_TYPES_ASSUMPTION,))))
    except sqlglot.errors.SqlglotError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {str(error)[:200]}")
    except RecursionError:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "unsupported: the query nests too deeply")
    return result


def _prove_equivalent_algebraic(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    search = kwargs.pop("search_counterexample", False)
    for name in ("schema", "types", "constraints"):
        if kwargs.get(name):
            kwargs[name] = drop_case_conflicts(kwargs[name])
    conditional = kwargs.pop("conditional", False)
    wall = kwargs.pop("conditional_seconds", None)
    result = _prove_algebraic_levels(left_sql, right_sql, search, **kwargs)
    if not conditional or result.status is SmtStatus.PROVEN_EQUIVALENT:
        return result
    from . import conditional_equivalence

    def prove(constraints, pair=None):
        if pair is not None:
            return _prove_algebraic_levels(*pair, False, **{**kwargs, "constraints": constraints, "compare_names": False})
        return _prove_algebraic_levels(left_sql, right_sql, False, **{**kwargs, "constraints": constraints})

    options = {} if wall is None else {"wall_seconds": wall}
    return conditional_equivalence.add_conditions(
        left_sql, right_sql, result, prove,
        schema=kwargs.get("schema"), constraints=kwargs.get("constraints"), types=kwargs.get("types"), dialect=kwargs.get("dialect", "bigquery"), **options,
    )


def _prove_algebraic_levels(left_sql: str, right_sql: str, search: bool, **kwargs) -> SmtEquivalenceResult:
    if kwargs.get("dialect", "bigquery") == "bigquery":
        left_sql, right_sql = canonical_literals(left_sql), canonical_literals(right_sql)
    original = (left_sql, right_sql)
    left_sql, right_sql, problem = positional_sql_pair(left_sql, right_sql, kwargs.get("dialect", "bigquery"))
    if problem:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: BY NAME set operation ({problem})")
    mixed = mixed_types(left_sql, kwargs.get("types"), kwargs.get("dialect", "bigquery")) or mixed_types(
        right_sql, kwargs.get("types"), kwargs.get("dialect", "bigquery")
    )
    if mixed:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {mixed} (the rules do not model the conversion)")
    compared = string_number_compare.problem(
        original[0], kwargs.get("dialect", "bigquery"), kwargs.get("types"), plain_ok=True
    ) or string_number_compare.problem(original[1], kwargs.get("dialect", "bigquery"), kwargs.get("types"), plain_ok=True)
    if compared:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {compared}")
    normalization_cache = {}
    result = _prove_algebraic(left_sql, right_sql, 0, _normalization_cache=normalization_cache, **kwargs)
    for level in (1, 2):
        if result.proven or (level == 1 and not (kwargs.get("constraints") or {})):
            continue
        retry = _prove_algebraic(left_sql, right_sql, level, _normalization_cache=normalization_cache, **kwargs)
        if retry.proven:
            return retry
    if result.status is SmtStatus.NOT_PROVEN:
        from .semijoin_rules import semijoin_reading

        dialect = kwargs.get("dialect", "bigquery")
        semi = semijoin_reading(left_sql, dialect), semijoin_reading(right_sql, dialect)
        if semi != (None, None):
            retry = _prove_algebraic(semi[0] or left_sql, semi[1] or right_sql, False, **kwargs)
            if retry.proven:
                return retry
    if search and result.status is SmtStatus.NOT_PROVEN:
        from . import executed_refutation

        counterexample = executed_refutation.search_counterexample(
            *original,
            schema=kwargs.get("schema"),
            types=kwargs.get("types"),
            constraints=kwargs.get("constraints"),
            dialect=kwargs.get("dialect", "bigquery"),
        )
        if counterexample is not None:
            return SmtEquivalenceResult(
                SmtStatus.NOT_EQUIVALENT,
                "the queries return different rows on the attached database (found by running both)",
                counterexample=counterexample,
                assumptions=(executed_refutation.ASSUMPTION,),
            )
    return result


def _prove_algebraic(left_sql: str, right_sql: str, keyed_distinct: int, *, _normalization_cache=None, **kwargs) -> SmtEquivalenceResult:
    dialect = kwargs.get("dialect", "bigquery")
    types = kwargs.get("types")
    constants = kwargs.get("group_by_constants", False)
    normalization_assumptions: set[str] = set()
    kwargs = {k: v for k, v in kwargs.items() if k not in ("types", "group_by_constants")}
    try:
        not_null = {t: c.not_null for t, c in (kwargs.get("constraints") or {}).items()}
        keys = {t.lower(): [tuple(k) for k in c.keys] for t, c in (kwargs.get("constraints") or {}).items()}
        fks = {t.lower(): list(c.foreign_keys) for t, c in (kwargs.get("constraints") or {}).items() if c.foreign_keys}
        # This cache belongs to one pair and one immutable proof context. Never share
        # it with recursive or conditional proofs, whose constraints may differ.
        cache = {} if _normalization_cache is None else _normalization_cache

        def normalized(sql, level):
            key = (sql, level)
            if key not in cache:
                assumptions = set()
                text = normalize(sql, schema=kwargs.get("schema"), dialect=dialect,
                                 not_null=not_null, keys=keys, types=types,
                                 group_by_constants=constants, keyed_distinct=level,
                                 foreign_keys=fks, _assumptions=assumptions)
                cache[key] = (text, frozenset(assumptions))
            text, assumptions = cache[key]
            normalization_assumptions.update(assumptions)
            return text

        left, right = normalized(left_sql, keyed_distinct), normalized(right_sql, keyed_distinct)
        if keyed_distinct and (left, right) == (normalized(left_sql, 0), normalized(right_sql, 0)):
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "no keyed DISTINCT to drop", assumptions=tuple(sorted(normalization_assumptions)))
    except sqlglot.errors.SqlglotError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {error}", assumptions=tuple(sorted(normalization_assumptions)))
    from . import scalar_subqueries

    replaced = False
    report: dict = {}
    try:
        inner = {k: v for k, v in kwargs.items() if k != "compare_names"}

        def same(a: str, b: str) -> bool:
            proof = prove_equivalent_algebraic(a, b, compare_names=False, types=types, **inner)
            if proof.proven:
                normalization_assumptions.update(proof.assumptions)
            return proof.proven

        def single_row(sql: str) -> bool:
            from .output_properties import infer_properties

            try:
                return infer_properties(sql, kwargs.get("constraints"), kwargs.get("schema"), dialect=dialect).at_most_one_row
            except Exception:  # noqa: BLE001 - not vouching keeps the assumption
                return False

        left, right, replaced = scalar_subqueries.unify(
            left, right, dialect=dialect, schema=kwargs.get("schema"), prove=same, single_row=single_row, report=report
        )
    except sqlglot.errors.SqlglotError:
        replaced = False
    result = prove_equivalent_smt(left, right, types=types, **kwargs)
    if normalization_assumptions:
        result = dataclasses.replace(result, assumptions=tuple(dict.fromkeys((*result.assumptions, *sorted(normalization_assumptions)))))
    if result.status is SmtStatus.NOT_PROVEN and not replaced:
        reduced = set_aggregates.reduce(left, right, dialect, kwargs.get("compare_names", True))
        if reduced:
            inner = {k: v for k, v in kwargs.items() if k != "compare_names"}
            proofs = []
            for value_left, value_right in reduced:
                proof = prove_equivalent_algebraic(value_left, value_right, compare_names=False, types=types, group_by_constants=constants, **inner)
                if not proof.proven:
                    break
                proofs.append(proof)
            else:
                assumptions = tuple(dict.fromkeys((*sorted(normalization_assumptions), *(a for proof in proofs for a in proof.assumptions))))
                return SmtEquivalenceResult(
                    SmtStatus.PROVEN_EQUIVALENT, "the aggregates read the same set of values on both sides", assumptions=assumptions
                )
    if result.status is SmtStatus.NOT_PROVEN and not replaced:
        from .structural_identity import same_scoped_query
        if same_scoped_query(left, right, schema=kwargs.get("schema"), dialect=dialect,
                             compare_names=kwargs.get("compare_names", True)):
            return dataclasses.replace(result, status=SmtStatus.PROVEN_EQUIVALENT,
                                       reason="identical scoped queries after algebraic normalization")
    if replaced and result.proven and report.get("unproven", 1):
        result = dataclasses.replace(
            result, assumptions=tuple(result.assumptions) + (scalar_subqueries.ASSUMPTION,)
        )
    return result
