"""Safely lift relational BigQuery subqueries into named CTEs.

The supported transformation is deliberately narrow: subqueries used as a
FROM or JOIN relation are moved into the statement's top-level WITH clause.
Scalar, EXISTS, and correlated predicate subqueries are not rewritten because
turning those into CTEs changes cardinality or correlation semantics. Neither
is a FROM/JOIN subquery that reads a relation of the enclosing query (a
correlated or lateral derived table, checked in every branch of a set
operation) or a name defined by a WITH clause nested around it: a top-level
CTE sees neither.

This module is the ``lift_subqueries`` rule in the rewrite-rule registry; the
``lift_subqueries()`` function is kept as the stable public entry point.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from .ast_utils import (
    free_reads,
    identifier_name,
    nearest_parent_cte,
    nearest_root_cte,
    parse_statements,
    select_sources,
    set_with_clause,
    top_level_query,
    with_clause,
)
from .rule_driver import FATAL_DIAGNOSTIC_CODES, RewriteRule, RuleDiagnostic
from .sqlx import looks_like_sqlx, mask_sqlx_interpolations, split_sqlx_sections

# Backwards-compatible names for code that imported the private helpers.
from .ast_utils import with_clause as _with_clause  # noqa: F401

LiftDiagnostic = RuleDiagnostic


@dataclass(frozen=True)
class LiftResult:
    """Result of transforming one SQL workbook."""

    sql: str
    statements: int
    transformed_statements: int
    lifted_subqueries: int
    remaining_inline_subqueries: int
    diagnostics: tuple[LiftDiagnostic, ...]

    @property
    def success(self) -> bool:
        """Whether every parsed relational subquery was lifted."""

        return (
            not any(diagnostic.code in FATAL_DIAGNOSTIC_CODES for diagnostic in self.diagnostics)
            and self.remaining_inline_subqueries == 0
        )

    @property
    def recovered(self) -> bool:
        """Whether strict parsing failed and sqlglot's recovery mode supplied the statements.

        ``success`` does not look at this: recovery is kept for valid BigQuery that sqlglot cannot
        parse strictly. Recovery also accepts truncated or trailing-garbage input (``WHERE 1 =``),
        so a gate that must not credit broken SQL checks this as well as ``success``.
        """

        return any(diagnostic.code == "recovered_parse" for diagnostic in self.diagnostics)


def _is_relation_subquery(node: exp.Expression) -> bool:
    """A FROM or JOIN relation that is a query. ``FROM (t)`` and ``FROM (VALUES ...)`` hold no query, and a CTE
    body must be one (``WITH l AS (t) ...`` is not SQL), so they are not lifted."""

    return isinstance(node, exp.Subquery) and isinstance(node.parent, (exp.From, exp.Join)) and isinstance(node.this, exp.Query)


def _relation_names(select: exp.Select) -> set[str]:
    """Names a column can use to qualify a FROM, JOIN or LATERAL relation of ``select``."""

    names: set[str] = set()
    for source in [*select_sources(select), *(select.args.get("laterals") or [])]:
        names.add((source.alias_or_name or "").lower())
        if isinstance(source, exp.Table) and source.name:
            names.add(source.name.lower())
    names.discard("")
    return names


def _correlated(subquery: exp.Subquery) -> bool:
    """Whether the subquery reads a relation of an enclosing query (a correlated or lateral derived table).

    Every qualified column inside it, in every branch of a set operation and in nested predicate subqueries, must
    name a relation defined inside the subquery; one that names an outer relation instead would lose its source
    in a top-level CTE. Unqualified columns cannot be resolved without a schema and are not checked.
    """

    outer: set[str] = set()
    node = subquery.parent
    while node is not None:
        if isinstance(node, exp.Select):
            outer |= _relation_names(node)
        node = node.parent
    if not outer:
        return False
    for column in subquery.this.find_all(exp.Column):
        parts = {part.lower() for part in (column.text("catalog"), column.text("db"), column.table) if part}
        if not parts and _bare_column_without_relation(column, subquery):
            return True
        if not parts & outer:
            continue
        inner: set[str] = set()
        node = column.parent
        while node is not None and node is not subquery:
            if isinstance(node, exp.Select):
                inner |= _relation_names(node)
            node = node.parent
        if not parts & inner:
            return True
    return False


def _bare_column_without_relation(column: exp.Column, subquery: exp.Subquery) -> bool:
    """Whether an unqualified column sits in a SELECT with nothing to read it from, so it can only come from around.

    ``(SELECT MAX(s.v) FROM (SELECT a AS v) AS s)``: the inner SELECT has no FROM, so ``a`` is a column of an
    enclosing query. A name that the SELECT itself defines (an output alias used by GROUP BY, HAVING or ORDER BY)
    is not one.
    """

    if not _has_scope_above(subquery):
        return False
    node = column.parent
    while node is not None and node is not subquery:
        if isinstance(node, exp.Select):
            if select_sources(node) or node.args.get("laterals"):
                return False
            return column.name.lower() not in {name.lower() for name in node.named_selects if name}
        node = node.parent
    return False


def _has_scope_above(subquery: exp.Subquery) -> bool:
    """Whether a query encloses the one whose FROM holds ``subquery`` (the only place an outer column can come from)."""

    holder = subquery.find_ancestor(exp.Select)
    return holder is not None and holder.find_ancestor(exp.Select) is not None or bool(
        holder is not None and holder.args.get("laterals"))


def _captured(subquery: exp.Subquery, query: exp.Expression) -> bool:
    """Whether the subquery reads a name that a WITH clause nested between it and ``query`` defines.

    Lifted to the top level, that name would mean a different relation (or none).
    """

    nested: set[str] = set()
    node = subquery.parent
    while node is not None and node is not query:
        clause = with_clause(node)
        if isinstance(clause, exp.With):
            nested |= {cte.alias_or_name.lower() for cte in clause.expressions}
        node = node.parent
    return bool(nested and free_reads(subquery.this) & nested)


def _liftable(subquery: exp.Subquery, query: exp.Expression) -> bool:
    """Whether the subquery can move to ``query``'s top-level WITH without changing what it reads.

    The prover's normalization (``rewrite_pipe_syntax=True``) asks the same question: its independent check
    (``proof_lift``) refuses a lifted body that reads a relation of the query around it or a name a nested
    WITH defines, so such a subquery stays where it is.
    """

    return not _correlated(subquery) and not _captured(subquery, query)


def _relation_subqueries(node: exp.Expression) -> list[exp.Subquery]:
    """Return inline FROM/JOIN subqueries in deterministic tree order."""

    return [candidate for candidate in node.walk() if _is_relation_subquery(candidate)]


def _lift_scope(subquery: exp.Subquery, statement: exp.Expression) -> exp.Expression:
    """The query whose WITH clause would receive the subquery (see ``_transform_statement``)."""

    query = top_level_query(statement)
    if query is not None:
        return query
    outermost: exp.Expression = statement
    node = subquery.parent
    while node is not None:
        if isinstance(node, (exp.Select, exp.Union)):
            outermost = node
        if node is statement:
            break
        node = node.parent
    return outermost


def _liftable_subqueries(statement: exp.Expression) -> list[exp.Subquery]:
    """The statement's relation subqueries that can move to a top-level WITH unchanged in meaning."""

    return [s for s in _relation_subqueries(statement) if _liftable(s, _lift_scope(s, statement))]


def count_inline_subqueries(sql: str) -> int:
    """Count the FROM/JOIN subqueries ``lift_subqueries`` would lift in parseable BigQuery SQL.

    Correlated derived tables, and ones reading a name a nested WITH defines, are not counted:
    they stay in place. Raises ``sqlglot.ParseError`` for invalid or unsupported input instead of
    silently claiming that the input is transformed.
    """

    if looks_like_sqlx(sql):
        total = 0
        for kind, section in split_sqlx_sections(sql):
            if kind == "block" or not section.strip():
                continue
            masked, _ = mask_sqlx_interpolations(section)
            total += sum(len(_liftable_subqueries(s)) for s in parse_statements(masked))
        return total
    return sum(len(_liftable_subqueries(statement)) for statement in parse_statements(sql))


def _used_relation_names(query: exp.Expression) -> set[str]:
    """Every relation name visible anywhere in the statement, lower-cased.

    A lifted CTE named like a table the query already reads would shadow that table
    (a WITH name hides a same-named unqualified table, and BigQuery matches CTE names
    case-insensitively), silently changing the result, so tables count as well as CTEs.
    """

    names = {
        alias.lower()
        for cte in query.root().find_all(exp.CTE)
        if (alias := identifier_name(cte.args.get("alias")))
    }
    names.update(table.name.lower() for table in query.root().find_all(exp.Table) if table.name)
    # An alias or column spelled like a generated name counts too: an unaliased derived table becomes a relation
    # named like its CTE, and the independent check (``proof_lift``) accepts a lifted name only if it occurs
    # nowhere else in the statement. (``inline_single_use_ctes`` leaves a lifted name behind as an alias.)
    names.update(identifier.name.lower() for identifier in query.root().find_all(exp.Identifier) if identifier.name)
    return names


def _next_name(used: set[str], counter: list[int]) -> str:
    while True:
        counter[0] += 1
        candidate = f"__lifted_subquery_{counter[0]:03d}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def _replace_relation_subquery(subquery: exp.Subquery, name: str) -> None:
    """Replace a relation subquery while preserving its source alias."""

    replacement = exp.to_table(name)
    if subquery.alias:
        alias = exp.TableAlias(this=exp.to_identifier(subquery.alias))
        columns = subquery.args["alias"].args.get("columns")
        if columns:  # `(...) AS t (a, b)` names the relation's columns; the lifted reference must keep them
            alias.set("columns", [column.copy() for column in columns])
        replacement.set("alias", alias)
    # PIVOT / UNPIVOT / TABLESAMPLE attach to the subquery in the tree; they belong to the relation
    # and must move onto the new table reference, or the lifted query silently loses them.
    for key in ("pivots", "sample", "laterals"):
        if subquery.args.get(key):
            replacement.set(key, subquery.args[key])
    subquery.replace(replacement)


def _make_cte(name: str, body: exp.Expression) -> exp.CTE:
    return exp.CTE(
        this=body,
        alias=exp.TableAlias(this=exp.to_identifier(name)),
    )


@dataclass(frozen=True)
class _PendingCte:
    cte: exp.CTE
    parent_cte: exp.CTE | None


def _tree_depth(node: exp.Expression) -> int:
    depth = 0
    parent = node.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _lift_query(query: exp.Expression, counter: list[int] | None = None) -> int:
    """Lift relation subqueries from a SELECT/UNION query, recursively."""

    used = _used_relation_names(query)
    counter = counter if counter is not None else [0]
    lifted = 0
    current = with_clause(query)
    original_ctes = list(current.expressions) if current else []
    pending: list[_PendingCte] = []

    # Work from the deepest currently visible relation subquery. Processing
    # one at a time is intentional: nested relations are added to this
    # statement's WITH clause, never to a nested WITH clause inside another
    # lifted CTE. That gives every lifted relation one top-level name and
    # keeps dependency order (inner before outer).
    while True:
        candidates = [s for s in _relation_subqueries(query) if _liftable(s, query)]
        if not candidates:
            break

        subquery = max(candidates, key=_tree_depth)
        if not subquery.parent:
            break
        body = subquery.this.copy()
        name = _next_name(used, counter)
        pending.append(
            _PendingCte(
                cte=_make_cte(name, body),
                parent_cte=(
                    nearest_root_cte(subquery, original_ctes)
                    if current
                    else nearest_parent_cte(subquery)
                ),
            )
        )
        _replace_relation_subquery(subquery, name)
        lifted += 1

    if pending:
        if current:
            ordered: list[exp.CTE] = []
            for original_cte in original_ctes:
                ordered.extend(
                    item.cte for item in pending if item.parent_cte is original_cte
                )
                ordered.append(original_cte)
            ordered.extend(item.cte for item in pending if item.parent_cte is None)
            current.set("expressions", ordered)
        else:
            set_with_clause(query, exp.With(expressions=[item.cte for item in pending]))

    return lifted


# BigQuery has no statement-level WITH for these: ``WITH s AS (...) UPDATE ...`` is a syntax error
# ("Unexpected keyword UPDATE"), and likewise for DELETE and MERGE.
_NO_STATEMENT_WITH = (exp.Update, exp.Delete, exp.Merge)


def _transform_statement(statement: exp.Expression) -> int:
    query = None if isinstance(statement, _NO_STATEMENT_WITH) else top_level_query(statement)
    if query is not None:
        return _lift_query(query)

    # BigQuery scripting statements such as SET can contain a query inside a
    # scalar expression, and UPDATE, DELETE and MERGE cannot start with WITH.
    # There is no statement-level WITH slot for those, so lift within the
    # innermost query scope instead (for example, SET x = (WITH ... SELECT ...)
    # or DELETE ... WHERE id IN (WITH ... SELECT ...)). A subquery directly in
    # UPDATE ... FROM has no such scope and is reported as remaining.
    lifted = 0
    query_nodes = [
        node
        for node in statement.walk()
        if isinstance(node, (exp.Select, exp.Union)) and _relation_subqueries(node)
    ]
    for query_node in query_nodes:
        lifted += _lift_query(query_node)
    return lifted


class LiftSubqueriesRule(RewriteRule):
    """Promote every FROM/JOIN subquery into a uniquely named top-level CTE."""

    name = "lift_subqueries"
    summary = "Lift FROM/JOIN subqueries into named top-level CTEs"

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        before = len(_liftable_subqueries(statement))
        lifted = _transform_statement(statement)
        after = len(_liftable_subqueries(statement))
        kept = len(_relation_subqueries(statement)) - after
        diagnostics: list[RuleDiagnostic] = []
        if before and after:
            diagnostics.append(
                RuleDiagnostic(
                    index,
                    "inline_subqueries_remaining",
                    f"{after} relational subquery/subqueries remain after transformation",
                )
            )
        if kept:
            diagnostics.append(
                RuleDiagnostic(
                    index,
                    "correlated_subquery_kept",
                    f"{kept} subquery/subqueries left in place: they read a relation of the enclosing query "
                    "or a name defined by a nested WITH, which a top-level CTE cannot see",
                )
            )
        return lifted, diagnostics

    def count_remaining(self, statements: list[exp.Expression]) -> int:
        return sum(len(_liftable_subqueries(statement)) for statement in statements)


_RULE = LiftSubqueriesRule()


class _AnalysisLiftRule(LiftSubqueriesRule):
    rewrite_pipe_syntax = True
    analysis_only = True


_ANALYSIS_RULE = _AnalysisLiftRule()


def lift_subqueries(sql: str, *, rewrite_pipe_syntax: bool = False) -> LiftResult:
    """Lift relational subqueries in BigQuery SQL or Dataform SQLX.

    Pipe syntax is left as written unless ``rewrite_pipe_syntax`` asks for sqlglot's standard-SQL
    translation of it, which the prover normalizes but a user should not be shown.
    """

    output = (_ANALYSIS_RULE if rewrite_pipe_syntax else _RULE).apply(sql)
    return LiftResult(
        sql=output.sql,
        statements=output.statements,
        transformed_statements=output.changed_statements,
        lifted_subqueries=output.changes,
        remaining_inline_subqueries=output.remaining,
        diagnostics=output.diagnostics,
    )


__all__ = [
    "LiftDiagnostic",
    "LiftResult",
    "LiftSubqueriesRule",
    "count_inline_subqueries",
    "lift_subqueries",
]
