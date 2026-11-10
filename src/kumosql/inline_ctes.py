"""Inline root CTEs that are referenced exactly once.

``WITH a AS (body) SELECT ... FROM a AS x`` becomes
``SELECT ... FROM (body) AS x``. A non-recursive BigQuery CTE is not
materialized, so a CTE with a single reference is evaluated once either way.

The rule is conservative and skips a CTE when any of these hold:

- the WITH clause is ``RECURSIVE`` or the query contains a nested WITH scope
  (a nested scope could shadow the name);
- a CTE name is read before its definition (an earlier CTE's body, or its own), where it is a table, not the
  CTE; inlining one CTE would move that read out of the body that makes it a table;
- the CTE declares column aliases (``WITH a(x) AS ...``);
- the name is referenced zero or several times, compared case-insensitively,
  or the one reference differs from the CTE name in case;
- the reference is not a plain FROM/JOIN relation (for example ``MERGE ...
  USING a``) or carries anything beyond an alias, such as ``FOR SYSTEM_TIME``;
- a bare ``UNNEST`` argument matches a relation name, which older sqlglot
  releases produce when parsing a repeated relation (see
  ``ambiguous_unnest_names``).
"""

from __future__ import annotations

from sqlglot import exp

from kumo_sql_prover.ast_utils import (
    ambiguous_unnest_names,
    cte_dependency_errors,
    cte_alias_name,
    has_nested_with,
    is_cte_reference_candidate,
    set_with_clause,
    top_level_query,
    with_clause,
)
from .engine import RewriteRule, RuleDiagnostic, register_rule


def _plain_relation_reference(table: exp.Table) -> bool:
    if not isinstance(table.parent, (exp.From, exp.Join)):
        return False
    extra = {key for key, value in table.args.items() if value is not None and value != []}
    if not extra <= {"this", "alias"}:
        return False
    alias = table.args.get("alias")
    return alias is None or not alias.args.get("columns")


def _inlinable(cte: exp.CTE, references: list[exp.Table]) -> exp.Table | None:
    name = cte_alias_name(cte)
    alias = cte.args.get("alias")
    if not name or alias is None or alias.args.get("columns"):
        return None
    if cte.args.get("materialized") is not None:
        return None
    if len(references) != 1:
        return None
    reference = references[0]
    if reference.name != name or not _plain_relation_reference(reference):
        return None
    # A non-recursive CTE cannot see its own name; a reference inside its
    # body is to some other object and must not be replaced.
    parent = reference.parent
    while parent is not None:
        if parent is cte:
            return None
        parent = parent.parent
    return reference


def _inline_once(query: exp.Expression, clause: exp.With) -> bool:
    ctes = list(clause.expressions)
    names = {name.lower() for cte in ctes if (name := cte_alias_name(cte))}
    references: dict[str, list[exp.Table]] = {name: [] for name in names}
    for table in query.find_all(exp.Table):
        key = table.name.lower()
        if key in references and is_cte_reference_candidate(table):
            references[key].append(table)

    for cte in ctes:
        name = cte_alias_name(cte)
        if not name:
            continue
        reference = _inlinable(cte, references[name.lower()])
        if reference is None:
            continue
        subquery = exp.Subquery(
            this=cte.this.copy(),
            alias=exp.TableAlias(this=exp.to_identifier(reference.alias or reference.name)),
        )
        reference.replace(subquery)
        cte.pop()
        return True
    return False


@register_rule
class InlineSingleUseCtesRule(RewriteRule):
    """Replace each root CTE that is referenced once with an inline subquery."""

    name = "inline_single_use_ctes"
    summary = "Inline root CTEs that are referenced exactly once"
    keep_sqlx_expressions = True

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        query = top_level_query(statement)
        if query is None:
            return 0, []
        clause = with_clause(query)
        if not clause or clause.args.get("recursive") or has_nested_with(query):
            return 0, []
        if ambiguous_unnest_names(query) or cte_dependency_errors(statement):
            return 0, []
        if not all(isinstance(cte.this, exp.Query) for cte in clause.expressions):
            return 0, []  # a data-modifying CTE (PostgreSQL) runs once whether or not it is read

        inlined = 0
        while _inline_once(query, clause):
            inlined += 1
        if inlined and not clause.expressions:
            set_with_clause(query, None)
        return inlined, []
