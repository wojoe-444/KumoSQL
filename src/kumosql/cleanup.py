"""Cleanup rules: trivial predicates, redundant parentheses, and CTE tidying.

Each rule is conservative and is checked by the equivalence prover, which
normalizes the same constructs independently (see ``equivalence.py``):

- ``remove_trivial_predicates``: ``WHERE 1 = 1``, ``AND TRUE``, ``OR FALSE``
  and other literal-only comparisons. Only three-valued-logic identities are
  used (``p AND TRUE = p``, ``p OR FALSE = p``), so NULL behaviour is kept.
  Annihilators such as ``p AND FALSE`` are not applied, because they would
  drop ``p`` and any error it raises. UPDATE, DELETE, and MERGE statements are
  left unchanged because DML rewrites are not currently proven.
- ``remove_redundant_parentheses``: parentheses that cannot change how an
  expression parses.
- ``deduplicate_ctes``: root CTEs with identical, deterministic bodies.
- ``remove_unused_ctes``: root CTEs that nothing references.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.tokens import TokenType

from kumo_sql_prover.ast_utils import (
    ambiguous_unnest_names,
    cte_alias_name,
    cte_dependency_errors,
    has_nested_with,
    inside as _inside,
    is_cte_reference_candidate,
    set_with_clause,
    top_level_query,
    with_clause,
)
from .engine import RewriteRule, RuleDiagnostic, register_rule
from kumo_sql_prover.equivalence import _literal_compare, _nondeterminism_reasons


# ---------------------------------------------------------------------------
# Trivial predicates


_COMPARISONS_FOLDED = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _truth(node: exp.Expression) -> bool | None:
    """Truth value of a predicate built only from literals, or None."""

    node = _unparen(node)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Not):
        inner = _truth(node.this)
        return None if inner is None else not inner
    if not isinstance(node, _COMPARISONS_FOLDED):
        return None
    left, right = _unparen(node.this), _unparen(node.expression)
    folded = _literal_compare(node, left, right)
    if folded is not None:
        return folded
    # Two string literals are only known equal when their text is identical
    # and has no escapes; anything else is left alone.
    if (
        isinstance(node, (exp.EQ, exp.NEQ))
        and isinstance(left, exp.Literal)
        and isinstance(right, exp.Literal)
        and left.is_string
        and right.is_string
        and left.this == right.this
        and "\\" not in left.this
    ):
        return isinstance(node, exp.EQ)
    return None


def simplify_predicate(node: exp.Expression) -> tuple[exp.Expression, int]:
    """Drop TRUE from AND chains and FALSE from OR chains, folding literals.

    The walk only descends through parentheses, AND, OR and NOT, so it stays
    in boolean context. Returns the new node and the number of changes.
    """

    if isinstance(node, exp.Paren):
        inner, changes = simplify_predicate(node.this)
        # ``(TRUE)`` folds away, and so does a group that simplification
        # reduced to one operand, as in ``(x OR FALSE)``.
        if isinstance(inner, exp.Boolean) or (changes and _self_delimited(inner)):
            return inner, changes + 1
        node.set("this", inner)
        return node, changes
    if isinstance(node, (exp.And, exp.Or)):
        identity = isinstance(node, exp.And)
        left, left_changes = simplify_predicate(node.this)
        right, right_changes = simplify_predicate(node.expression)
        changes = left_changes + right_changes
        if _truth(left) is identity:
            return right, changes + 1
        if _truth(right) is identity:
            return left, changes + 1
        node.set("this", left)
        node.set("expression", right)
        return node, changes
    if isinstance(node, exp.Not):
        inner, changes = simplify_predicate(node.this)
        node.set("this", inner)
        truth = _truth(node)
        if truth is not None:
            return exp.Boolean(this=truth), changes + 1
        return node, changes
    truth = _truth(node)
    if truth is not None and not isinstance(node, exp.Boolean):
        return exp.Boolean(this=truth), 1
    return node, 0


@register_rule
class RemoveTrivialPredicatesRule(RewriteRule):
    """Remove ``WHERE 1 = 1``, ``AND TRUE`` and similar no-op predicates."""

    name = "remove_trivial_predicates"
    summary = "Remove always-true filters such as WHERE 1 = 1 and AND TRUE"
    keep_sqlx_expressions = True

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        # The verifier does not prove UPDATE, DELETE, or MERGE statements.
        # Keep their predicates intact instead of returning a changed DML
        # statement that cannot be shown equivalent (or dropping a required
        # clause such as UPDATE ... WHERE TRUE).
        if isinstance(statement, (exp.Update, exp.Delete, exp.Merge)):
            return 0, []

        changes = 0
        for clause_type in (exp.Where, exp.Having, exp.Qualify):
            for clause in list(statement.find_all(clause_type)):
                condition, count = simplify_predicate(clause.this)
                changes += count
                clause.set("this", condition)
                if _truth(condition) is not True or isinstance(clause.parent, exp.Filter):
                    continue  # FILTER (WHERE TRUE) needs its condition to stay valid SQL
                # HAVING without GROUP BY can make a query an aggregate, so
                # only drop HAVING TRUE when there is a GROUP BY.
                owner = clause.parent
                if clause_type is exp.Having and not (owner and owner.args.get("group")):
                    continue
                clause.pop()
                changes += 1
        for join in list(statement.find_all(exp.Join)):
            on = join.args.get("on")
            if on is None:
                continue
            # ON TRUE itself is kept: an INNER JOIN needs a condition.
            condition, count = simplify_predicate(on)
            changes += count
            join.set("on", condition)
        return changes, []


# ---------------------------------------------------------------------------
# Redundant parentheses


_COMPARISONS = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.ILike,
    exp.Is,
    exp.In,
    exp.Between,
)

_ATOMS = (exp.Column, exp.Literal, exp.Boolean, exp.Null, exp.Paren, exp.Star)


def _self_delimited(node: exp.Expression) -> bool:
    """An expression whose rendering cannot be split by a neighbouring operator."""

    if isinstance(node, _ATOMS):
        return True
    if isinstance(node, (exp.Func, exp.Case, exp.Cast)):
        return _call_syntax(node.sql(dialect="bigquery"))
    return False


def _call_syntax(text: str) -> bool:
    """Whether text is ``NAME(...)`` or ``CASE ... END`` as a single unit.

    Some sqlglot function nodes render as operators (``x IN UNNEST(a)``), so
    the rendering is checked rather than the node type.
    """

    try:
        tokens = BigQuery().tokenize(text)
    except Exception:
        return False
    if len(tokens) >= 2 and tokens[0].token_type is TokenType.CASE:
        depth = 0
        for position, token in enumerate(tokens):
            if token.token_type is TokenType.CASE:
                depth += 1
            elif token.token_type is TokenType.END:
                depth -= 1
                if depth == 0:
                    return position == len(tokens) - 1
        return False
    if len(tokens) < 3 or tokens[1].token_type is not TokenType.L_PAREN:
        return False
    depth = 0
    for position, token in enumerate(tokens[1:], start=1):
        if token.token_type is TokenType.L_PAREN:
            depth += 1
        elif token.token_type is TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return position == len(tokens) - 1
    return False


def _redundant(paren: exp.Paren) -> bool:
    inner = paren.this
    parent = paren.parent
    if parent is None or inner is None or isinstance(inner, (exp.Query, exp.Subquery)):
        return False
    # BigQuery names an unaliased projection after its expression.
    if isinstance(parent, (exp.Select, exp.Union)) and paren.arg_key == "expressions":
        return False
    # ``(t).x`` and ``t.x`` parse differently.
    if isinstance(parent, (exp.Dot, exp.Bracket)) and not isinstance(inner, exp.Paren):
        return False
    if isinstance(inner, exp.Paren) or isinstance(parent, exp.Paren):
        return True
    if isinstance(parent, (exp.Where, exp.Having, exp.Qualify)):
        return True
    if isinstance(parent, exp.Join) and paren.arg_key == "on":
        return True
    if isinstance(parent, (exp.And, exp.Or)):
        # Same connector: associativity. Comparisons and NOT bind tighter
        # than AND and OR. ``(a AND b) OR c`` is kept for readability.
        if type(inner) is type(parent):
            return True
        if isinstance(inner, _COMPARISONS + (exp.Not,)):
            return True
    if isinstance(parent, exp.Alias) and paren.arg_key == "this":
        return True
    return _self_delimited(inner)


@register_rule
class RemoveRedundantParenthesesRule(RewriteRule):
    """Remove parentheses that cannot change how an expression parses."""

    name = "remove_redundant_parentheses"
    summary = "Remove parentheses that do not change how an expression parses"
    keep_sqlx_expressions = True

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        changes = 0
        # Innermost first, so ``((a))`` collapses fully in one pass.
        for paren in reversed(list(statement.find_all(exp.Paren))):
            if _redundant(paren):
                paren.replace(paren.this)
                changes += 1
        return changes, []


# ---------------------------------------------------------------------------
# CTE tidying


def _root_ctes(statement: exp.Expression) -> tuple[exp.Expression, exp.With] | None:
    """The query and its root WITH clause, if CTE rewrites are safe on it."""

    query = top_level_query(statement)
    if query is None:
        return None
    clause = with_clause(query)
    if not clause or clause.args.get("recursive") or has_nested_with(query):
        return None
    if ambiguous_unnest_names(query) or cte_dependency_errors(statement):
        return None
    if not all(isinstance(cte.this, exp.Query) for cte in clause.expressions):
        return None  # a data-modifying CTE (PostgreSQL) runs even when nothing reads it
    names = [cte_alias_name(cte) for cte in clause.expressions]
    if any(name is None for name in names):
        return None
    if any(cte.args["alias"].args.get("columns") for cte in clause.expressions):
        return None
    lowered = {name.lower() for name in names}
    if len(lowered) != len(names):
        return None
    exact = set(names)
    for table in query.find_all(exp.Table):
        if (
            is_cte_reference_candidate(table)
            and table.name.lower() in lowered
            and table.name not in exact
        ):
            return None  # a reference differing only in case
    return query, clause


def _references(query: exp.Expression, name: str) -> list[exp.Table]:
    return [
        table
        for table in query.find_all(exp.Table)
        if is_cte_reference_candidate(table) and table.name == name
    ]


def _body_key(cte: exp.CTE, names: set[str], defined_before: set[str]) -> str | None:
    """Text identifying a CTE body, or None if merging it would be unsafe."""

    body = cte.this
    if _nondeterminism_reasons(body):
        return None
    if any(type(node).__name__ in {"Limit", "Offset"} for node in body.walk()):
        return None
    for table in body.find_all(exp.Table):
        # A name read by the body must mean the same relation for both copies.
        if is_cte_reference_candidate(table) and table.name in names:
            if table.name not in defined_before:
                return None
    return body.sql(dialect="bigquery", comments=False)


def _range_variable_collision(query: exp.Expression, old: str, new: str) -> bool:
    """Whether renaming references to ``old`` would repeat a FROM name.

    ``FROM a JOIN b`` would become ``FROM a JOIN a AS b``. A one-part name
    that repeats a range variable in the same FROM clause can be read as a
    correlated array path (sqlglot 26 parses it as ``UNNEST(a)``), so such a
    merge is skipped.
    """

    for table in _references(query, old):
        select = table.find_ancestor(exp.Select)
        if select is None:
            continue
        sources = [select.args.get("from") or select.args.get("from_")]
        sources += [join for join in select.args.get("joins") or []]
        for source in sources:
            if source is None or source.this is table:
                continue
            if (source.this.alias_or_name or "").lower() == new.lower():
                return True
    return False


@register_rule
class DeduplicateCtesRule(RewriteRule):
    """Point references to a duplicate CTE at the first identical one."""

    name = "deduplicate_ctes"
    summary = "Merge root CTEs whose bodies are identical"
    keep_sqlx_expressions = True

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        found = _root_ctes(statement)
        if found is None:
            return 0, []
        query, clause = found
        merged = 0
        while True:
            ctes = list(clause.expressions)
            names = {cte_alias_name(cte) for cte in ctes}
            seen: dict[str, exp.CTE] = {}
            defined: set[str] = set()
            duplicate: tuple[exp.CTE, exp.CTE] | None = None
            for cte in ctes:
                key = _body_key(cte, names, defined)
                if key is not None:
                    first = seen.get(key)
                    if first is None:
                        seen[key] = cte
                    elif not _range_variable_collision(
                        query, cte_alias_name(cte), cte_alias_name(first)
                    ):
                        duplicate = (first, cte)
                        break
                defined.add(cte_alias_name(cte))
            if duplicate is None:
                break
            keep, drop = duplicate
            keep_name, drop_name = cte_alias_name(keep), cte_alias_name(drop)
            for table in _references(query, drop_name):
                if not table.alias:
                    # Keep the range-variable name so qualifiers still match.
                    table.set("alias", exp.TableAlias(this=exp.to_identifier(drop_name)))
                table.set("this", exp.to_identifier(keep_name))
            drop.pop()
            merged += 1
        return merged, []


@register_rule
class RemoveUnusedCtesRule(RewriteRule):
    """Remove root CTEs that no query or other CTE references."""

    name = "remove_unused_ctes"
    summary = "Remove root CTEs that are never referenced"
    keep_sqlx_expressions = True

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        found = _root_ctes(statement)
        if found is None:
            return 0, []
        query, clause = found
        removed = 0
        while True:
            unused = [
                cte
                for cte in clause.expressions
                if not any(
                    ref for ref in _references(query, cte_alias_name(cte))
                    if not _inside(ref, cte)
                )
                and not _named_as_value(query, cte_alias_name(cte), cte)
            ]
            if not unused:
                break
            for cte in unused:
                cte.pop()
                removed += 1
        if removed and not clause.expressions:
            set_with_clause(query, None)
        return removed, []


def _named_as_value(query: exp.Expression, name: str, cte: exp.Expression) -> bool:
    """A bare identifier spelled like the CTE may name it (DuckDB passes tables to functions)."""

    return any(
        column.name == name and not column.table and not _inside(column, cte)
        for column in query.find_all(exp.Column)
    )
