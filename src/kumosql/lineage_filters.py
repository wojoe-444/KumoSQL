"""The conditions that limit which rows reach each output column of a model.

Column lineage says which source columns a value is built from. It does not say that the rows were
filtered on the way: a ``COALESCE`` built in a CTE keeps its sources, and the ``WHERE week = ...`` of
the query that reads the CTE is invisible. ``model_filters`` lists those conditions for one qualified
query: every ``WHERE``, ``JOIN ... ON`` / ``USING``, ``HAVING`` and ``QUALIFY`` in the select that makes
the column and in every CTE, derived table and set-operation branch it reads (including inputs that are
only joined in), each with the scope it was written in and the source columns it reads.

How a condition affects the output rows is its ``effect``:

* ``limits_rows``: rows that fail it do not reach the output (a ``WHERE``, an inner join's ``ON``, a filter
  inside an input that is inner-joined or is the preserved side of an outer join).
* ``matches_only``: it decides which rows pair up, not whether a row survives (the ``ON`` of an outer
  join, and any filter inside the input that an outer join may null-extend). Rows without a match stay.
* ``excludes_rows``: filters inside the right side of ``EXCEPT``, which decides which rows are removed.
* ``feeds_value``: filters of a scalar subquery in the select list, which limit the rows that one
  column's value is computed from, not the model's own rows.

Nothing here is guessed: a condition that cannot be attributed to source columns is listed without
them and flagged ``columns_complete=False``; a model whose scopes cannot be built has no filters
(``None``), which callers report as unknown rather than as "no filters".
"""

from __future__ import annotations

import re
from typing import Callable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from .lineage_soundness import _Resolver, _set_operation_scopes
from .pipeline_types import ColumnFilter, ColumnRef

LIMITS = "limits_rows"
MATCHES = "matches_only"
EXCLUDES = "excludes_rows"
VALUE = "feeds_value"

_TOKEN = re.compile(r"__sqlx_token_(\d+)__")
_MAX_DEPTH = 40


def model_filters(
    qualified: exp.Expression,
    owner: Callable[[exp.Table], str],
    masked: tuple[str, ...] = (),
) -> tuple[tuple[ColumnFilter, ...], dict[str, tuple[ColumnFilter, ...]]] | None:
    """``(row filters, per-column value filters)`` of a qualified query, or ``None`` when its scopes cannot be built.

    Row filters apply to every output column. Value filters (``feeds_value``) belong to the one column
    whose select-list expression holds the scalar subquery they come from.
    """

    try:
        root = build_scope(qualified)
    except Exception:
        return None
    if root is None:
        return None
    walker = _Walker(_Resolver(owner, ColumnRef), masked)
    try:
        for inner in root.traverse():
            for column in inner.columns:
                walker.home[id(column)] = inner
        walker.rows(root, "main", LIMITS, 0)
        values = {name: walker.value_filters(root, name) for name in qualified.named_selects}
    except _Unsupported:
        return None
    return _dedupe(walker.found), {name: found for name, found in values.items() if found}


class _Unsupported(Exception):
    pass


class _Walker:
    def __init__(self, resolver: _Resolver, masked: tuple[str, ...]) -> None:
        self.resolver = resolver
        self.masked = masked
        self.found: list[ColumnFilter] = []
        # The scope each column belongs to, so a column inside a predicate subquery resolves in that subquery.
        self.home: dict[int, Scope] = {}
        self._visiting: set[tuple[int, str]] = set()

    # ------------------------------------------------------------------ rows

    def rows(self, scope: Scope, label: str, effect: str, depth: int) -> None:
        """Every condition that limits the rows ``scope`` returns, into ``self.found``."""

        if depth > _MAX_DEPTH:
            raise _Unsupported
        key = (id(scope.expression), effect)
        if key in self._visiting:
            return
        self._visiting.add(key)
        try:
            self._rows(scope, label, effect, depth)
        finally:
            self._visiting.discard(key)

    def _rows(self, scope: Scope, label: str, effect: str, depth: int) -> None:
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            branches = _set_operation_scopes(scope)
            for index, branch in enumerate(branches):
                branch_effect = effect
                if isinstance(expression, exp.Except) and index > 0 and effect == LIMITS:
                    branch_effect = EXCLUDES
                self.rows(branch, f"{label} branch {index + 1}", branch_effect, depth + 1)
            return
        if not isinstance(expression, exp.Select):
            return
        sources = _source_effects(expression, effect)
        where = expression.args.get("where")
        if where is not None:
            self.add("where", where.this, scope, label, effect)
        for join in expression.args.get("joins") or ():
            on = join.args.get("on")
            if on is not None:
                self.add("join", on, scope, label, sources.join_effect(join))
            using = join.args.get("using")
            if using:
                keys = ", ".join(key.name for key in using)
                self.found.append(
                    ColumnFilter("join", f"USING ({keys})", label, (), sources.join_effect(join), False)
                )
        having = expression.args.get("having")
        if having is not None:
            self.add("having", having.this, scope, label, effect)
        qualify = expression.args.get("qualify")
        if qualify is not None:
            self.add("qualify", qualify.this, scope, label, effect)
        for alias, source_effect in sources.by_alias.items():
            source = scope.sources.get(alias)
            if isinstance(source, Scope):
                self.rows(source, _scope_label(source, alias), source_effect, depth + 1)

    # ----------------------------------------------------------------- value

    def value_filters(self, scope: Scope, name: str, depth: int = 0, seen: frozenset = frozenset()) -> tuple[ColumnFilter, ...]:
        """Conditions of scalar subqueries the output column ``name`` of ``scope`` is computed with."""

        if depth > _MAX_DEPTH:
            raise _Unsupported
        marker = (id(scope.expression), name.casefold())
        if marker in seen:
            return ()
        seen = seen | {marker}
        expression = scope.expression
        found: list[ColumnFilter] = []
        if isinstance(expression, exp.SetOperation):
            names = [n.casefold() for n in expression.named_selects]
            if name.casefold() not in names:
                return ()
            index = names.index(name.casefold())
            for branch in _set_operation_scopes(scope):
                branch_names = branch.expression.named_selects
                if index < len(branch_names):
                    found.extend(self.value_filters(branch, branch_names[index], depth + 1, seen))
            return tuple(found)
        if not isinstance(expression, exp.Select):
            return ()
        matches = [p for p in expression.expressions if p.alias_or_name.casefold() == name.casefold()]
        if len(matches) != 1:
            return ()
        projection = matches[0]
        own = {id(c) for c in scope.columns}
        for column in projection.find_all(exp.Column):
            if id(column) not in own or not column.table:
                continue
            source = _source_of(scope, column)
            if isinstance(source, Scope):
                found.extend(self.value_filters(source, column.name, depth + 1, seen))
        for child in scope.subquery_scopes:
            if not _inside(child.expression, projection):
                continue
            if _is_predicate_subquery(child.expression, projection):
                continue  # an EXISTS / IN in the select list: its condition is part of the value, not a row filter
            saved = self.found
            self.found = []
            try:
                self.rows(child, "subquery", VALUE, depth + 1)
                found.extend(self.found)
            finally:
                self.found = saved
        return tuple(found)

    # ----------------------------------------------------------------- shared

    def add(self, kind: str, condition: exp.Expression, scope: Scope, label: str, effect: str) -> None:
        refs: set[ColumnRef] = set()
        complete = True
        for column in condition.find_all(exp.Column):
            home = self.home.get(id(column))
            if home is None:
                complete = False
                continue
            try:
                resolved = self.resolver.column(home, column)
            except Exception:
                resolved = None
            if resolved is None:
                complete = False
            else:
                refs |= resolved
        self.found.append(
            ColumnFilter(kind, self.text(condition), label, tuple(sorted(refs, key=str)), effect, complete)
        )

    def text(self, condition: exp.Expression) -> str:
        return sql_text(condition, self.masked)


def sql_text(node: exp.Expression, masked: tuple[str, ...] = (), *, copy: bool = True, unnamed: bool = False) -> str:
    """``node`` as BigQuery SQL the way it was written: ``x IS NOT NULL``, no aliases the qualifier added, ``${...}`` restored.

    ``copy=False`` tidies ``node`` in place (for a tree the caller has already copied). ``unnamed`` also drops the
    ``_col_0`` names the qualifier gives the unnamed projection of a scalar subquery, which no query refers to.
    """

    if copy:
        node = node.copy()
    for negated in list(node.find_all(exp.Not)):
        test = negated.this
        if isinstance(test, exp.Is) and isinstance(test.expression, exp.Null):
            # sqlglot prints ``x IS NOT NULL`` as ``NOT x IS NULL``; say it the way it was written.
            replacement = exp.Is(this=test.this.copy(), expression=exp.Not(this=exp.Null()))
            if negated is node:
                node = replacement
            else:
                negated.replace(replacement)
    for alias in list(node.find_all(exp.Alias)):
        if isinstance(alias.parent, exp.Select) and (_qualify_alias(alias) or (unnamed and _scalar_unnamed(alias))):
            alias.replace(alias.this.copy())  # an alias the qualifier added (``b.id AS id``, ``1 AS `1```)
    sql = node.sql(dialect="bigquery")
    if not masked:
        return sql
    return _TOKEN.sub(lambda m: _restore(m, masked), sql)


_GENERATED_ALIAS = re.compile(r"_col_\d+")


def _qualify_alias(alias: exp.Alias) -> bool:
    inner = alias.this
    if isinstance(inner, exp.Column):
        return inner.name == alias.alias
    return inner.sql(dialect="bigquery").strip("`") == alias.alias


def _scalar_unnamed(alias: exp.Alias) -> bool:
    """``MAX(x) AS _col_0`` in the one-column select of a scalar subquery: a name nothing reads."""

    select = alias.parent
    return (
        bool(_GENERATED_ALIAS.fullmatch(alias.alias))
        and isinstance(select, exp.Select)
        and len(select.expressions) == 1
        and isinstance(select.parent, exp.Subquery)
        and not isinstance(select.parent.parent, (exp.From, exp.Join))
    )


def _restore(match: re.Match, masked: tuple[str, ...]) -> str:
    index = int(match.group(1))
    if index < len(masked):
        text = masked[index]
        return text if text.startswith("${") else "${" + text + "}"
    return match.group(0)


class _Effects:
    """How each input of a select combines, from the order and sides of its joins."""

    def __init__(self) -> None:
        self.by_alias: dict[str, str] = {}
        self._join: dict[int, str] = {}

    def join_effect(self, join: exp.Join) -> str:
        return self._join.get(id(join), LIMITS)


def _source_effects(select: exp.Select, inherited: str) -> _Effects:
    """Per input alias, whether its filters limit the select's rows, and per join, whether its ON does."""

    result = _Effects()
    first = select.args.get("from_") or select.args.get("from")
    base = _alias(first.this) if first is not None else None
    if base:
        result.by_alias[base] = inherited
    for join in select.args.get("joins") or ():
        alias = _alias(join.this)
        side = (join.side or "").upper()
        if side == "LEFT":
            effect = MATCHES if inherited == LIMITS else inherited
            on_effect = effect
        elif side == "RIGHT":
            effect = inherited
            on_effect = MATCHES if inherited == LIMITS else inherited
            for earlier in result.by_alias:
                if result.by_alias[earlier] == LIMITS:
                    result.by_alias[earlier] = MATCHES if inherited == LIMITS else inherited
        elif side == "FULL":
            effect = on_effect = MATCHES if inherited == LIMITS else inherited
            for earlier in result.by_alias:
                if result.by_alias[earlier] == LIMITS:
                    result.by_alias[earlier] = effect
        else:
            effect = on_effect = inherited
        result._join[id(join)] = on_effect
        if alias:
            result.by_alias[alias] = effect
    return result


def _alias(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    return node.alias_or_name or None


def _scope_label(scope: Scope, alias: str) -> str:
    parent = scope.expression.parent
    while parent is not None and not isinstance(parent, (exp.CTE, exp.Subquery, exp.Select)):
        parent = parent.parent
    if isinstance(parent, exp.CTE):
        return parent.alias_or_name
    return f"subquery {alias}" if alias else "subquery"


def _source_of(scope: Scope, column: exp.Column):
    current: Scope | None = scope
    while current is not None and column.table not in current.sources:
        current = current.parent
    return current.sources[column.table] if current is not None else None


def _inside(node: exp.Expression, container: exp.Expression) -> bool:
    parent = node
    while parent is not None:
        if parent is container:
            return True
        parent = parent.parent
    return False


def _is_predicate_subquery(node: exp.Expression, projection: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and parent is not projection:
        if isinstance(parent, exp.Exists):
            return True
        parent = parent.parent
    return False


def _dedupe(filters: list[ColumnFilter]) -> tuple[ColumnFilter, ...]:
    best: dict[tuple, ColumnFilter] = {}
    order = {LIMITS: 0, EXCLUDES: 1, MATCHES: 2, VALUE: 3}
    for item in filters:
        key = (item.kind, item.condition, item.scope)
        held = best.get(key)
        if held is None or order[item.effect] < order[held.effect]:
            best[key] = item
    return tuple(best.values())
