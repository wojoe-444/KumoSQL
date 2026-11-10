"""Which output rows and keys of a query survive the source changes a contract allows.

An incremental model that merges a full re-run on ``uniqueKey`` equals its full refresh exactly when no
key it ever wrote can leave the query's result (a merge never deletes). This module decides that
statically. A contract (kinds of change, and which tables change) makes each source table

* **frozen**: it never changes;
* **growing**: it only gains rows (``insert_*``, ``duplicate``, ``null_key``);
* **keyed**: it gains rows and updates rows in place without changing their declared key
  (``update``, which also keeps the event time, and ``update_touch``, which moves it);
* or anything else (``delete``): no guarantee.

:func:`analyze` reads a query bottom-up and returns, for its output, whether it is frozen, whether it
only gains rows (``grows``), and a set ``stable`` of output columns whose *projection* only gains
values: every combination of those columns' values present before an allowed change is present after
it. A key whose columns are all stable never leaves the result. The rules, each sound on its own:

* a source scan is stable on all columns if it is frozen or growing, and on its key columns if keyed;
* a filter (``WHERE``, ``ON``, ``HAVING``, ``QUALIFY``) keeps the stable set when its value for a row is
  decided by stable columns and frozen tables only, or when it is a positive ``IN`` or ``EXISTS`` over a
  query that only gains matching rows (TRUE stays TRUE); ``NOT``, ``NOT IN``, ``NOT EXISTS`` and
  comparisons with a subquery need frozen tables;
* an inner join unions the stable sets when its condition reads stable columns only; a ``LEFT JOIN``
  always keeps its left side's stable columns (left rows are never dropped) and adds the right side's
  when that side is frozen;
* ``GROUP BY g`` is stable on ``g`` (and anything computed from it) when ``g`` is stable in its input:
  the groups are the input's ``g`` values; a ``HAVING`` must read group columns only, or be a
  monotone condition on a growing input (``COUNT(...) >= n``, ``MAX(x) > n``, ``MIN(x) < n``, ...);
* ``QUALIFY ROW_NUMBER() OVER (PARTITION BY p ...) = 1`` (or ``RANK``/``DENSE_RANK``, ``<= k``) keeps at
  least one row per partition, so it is stable on ``p`` when ``p`` is stable;
* ``DISTINCT`` keeps the stable set; ``UNION [ALL]`` is stable where both branches are; ``INTERSECT``
  and ``EXCEPT`` (the latter only against a frozen query) need growing inputs;
* ``LIMIT``, window values, aggregates, run-dependent functions (``RAND``, the clock) and anything not
  understood are not stable.

Anything outside these rules returns no guarantee, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp

from .incremental import SourceTable

INSERT_KINDS = frozenset({"insert_new", "insert_late", "insert_boundary", "duplicate", "null_key"})
UPDATE_KINDS = frozenset({"update", "update_touch"})

_RUN_DEPENDENT = tuple(
    getattr(exp, name)
    for name in ("Rand", "CurrentTimestamp", "CurrentDate", "CurrentDatetime", "CurrentTime", "Uuid")
    if hasattr(exp, name)
)
_NONDETERMINISTIC_NAMES = {"RAND", "GENERATE_UUID", "SESSION_USER", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_DATETIME", "CURRENT_TIME"}
_RANKING = (exp.RowNumber,) + tuple(getattr(exp, n) for n in ("Rank", "DenseRank") if hasattr(exp, n))


class _Refuse(Exception):
    """The query is outside what the analysis reads; the answer is 'no guarantee'."""


@dataclass(frozen=True)
class Growth:
    """What survives an allowed change, for one query's output columns (by position)."""

    names: tuple[str, ...]
    frozen: bool
    grows: bool
    stable: frozenset[int] | None  # None: no guarantee (the output may even become empty)

    def stable_names(self) -> set[str]:
        return {self.names[i] for i in self.stable or ()}


@dataclass
class _Rel:
    columns: list[tuple[str, str]]  # (qualifier, name), lower case
    frozen: bool
    grows: bool
    stable: frozenset[int] | None

    def all(self) -> frozenset[int]:
        return frozenset(range(len(self.columns)))


def classify(
    sources: dict[str, SourceTable], kinds: Iterable[str], tables: Iterable[str] | None = None
) -> dict[str, tuple[str, frozenset[str] | None]]:
    """Each source's state under the contract and its stable columns (lower case)."""

    kinds = frozenset(kinds) - {"empty"}
    changing = {t.lower() for t in tables} if tables else {t.lower() for t in sources}
    out: dict[str, tuple[str, frozenset[str] | None]] = {}
    for name, table in sources.items():
        columns = frozenset(c.lower() for c in table.columns)
        if name.lower() not in changing or not kinds:
            out[name.lower()] = ("frozen", columns)
        elif kinds <= INSERT_KINDS:
            out[name.lower()] = ("growing", columns)
        elif kinds <= INSERT_KINDS | UPDATE_KINDS and table.key:
            # ``update`` changes the other columns of a row and keeps its key and event time;
            # ``update_touch`` also moves the event time.
            kept = {k.lower() for k in table.key}
            if "update_touch" not in kinds and table.time_column:
                kept.add(table.time_column.lower())
            out[name.lower()] = ("keyed", frozenset(kept))
        else:
            out[name.lower()] = ("changing", None)
    return out


class _Scope:
    """The FROM clause of one SELECT: its items, and how the combined rows survive changes."""

    def __init__(self, outer: "_Scope | None", ctes: dict[str, _Rel] | None = None):
        self.items: list[tuple[str, _Rel]] = []  # (alias, relation)
        self.ctes = ctes or {}
        self.frozen = True
        self.grows = True
        self.stable: set[tuple[int, int]] | None = set()  # (item, position)
        self.outer = outer

    def resolve(self, column: exp.Column) -> tuple["_Scope", int, int] | None:
        """(scope, item, position) of a column reference, searching outer scopes; None when unknown."""

        name = column.name.lower()
        qualifier = column.table.lower() if column.table else ""
        found = []
        for i, (alias, rel) in enumerate(self.items):
            if qualifier and alias != qualifier:
                continue
            found += [(i, p) for p, (_, n) in enumerate(rel.columns) if n == name]
        if len(found) == 1:
            return (self, *found[0])
        if found:
            raise _Refuse(f"ambiguous column {column.sql()}")
        if qualifier and any(alias == qualifier for alias, _ in self.items):
            raise _Refuse(f"unknown column {column.sql()}")
        return self.outer.resolve(column) if self.outer is not None else None

    def is_stable(self, item: int, position: int) -> bool:
        return self.stable is not None and (item, position) in self.stable


class Analyzer:
    def __init__(self, sources: dict[str, SourceTable], kinds: Iterable[str], tables: Iterable[str] | None, target: str = ""):
        self.sources = {k.lower(): v for k, v in sources.items()}
        self.states = classify(sources, kinds, tables)
        self.target = target.lower()

    # -- queries -----------------------------------------------------------

    def query(self, node: exp.Expression, ctes: dict[str, _Rel], outer: _Scope | None = None) -> _Rel:
        if isinstance(node, exp.Subquery):
            if any(v for k, v in node.args.items() if k not in ("this", "alias")):
                raise _Refuse("subquery modifiers")
            return self.query(node.this, ctes, outer)
        if isinstance(node, exp.Paren):
            return self.query(node.this, ctes, outer)
        with_ = node.args.get("with_") or node.args.get("with")
        if with_ is not None:
            if with_.args.get("recursive"):
                raise _Refuse("recursive WITH")
            ctes = dict(ctes)
            for cte in with_.expressions:
                ctes[cte.alias_or_name.lower()] = self._rename(self.query(cte.this, ctes, None), cte.alias_or_name, cte)
        if isinstance(node, exp.Select):
            return self.select(node, ctes, outer)
        if isinstance(node, exp.SetOperation):
            return self.set_operation(node, ctes, outer)
        raise _Refuse(f"{type(node).__name__} query")

    def _rename(self, rel: _Rel, alias: str, node: exp.Expression) -> _Rel:
        alias = alias.lower()
        columns = [(alias, n) for _, n in rel.columns]
        names = node.args.get("alias")
        listed = names.args.get("columns") if isinstance(names, exp.TableAlias) else None
        if listed:
            if len(listed) != len(columns):
                raise _Refuse("column alias list")
            columns = [(alias, c.name.lower()) for c in listed]
        return _Rel(columns, rel.frozen, rel.grows, rel.stable)

    def set_operation(self, node: exp.SetOperation, ctes: dict[str, _Rel], outer: _Scope | None) -> _Rel:
        if any(node.args.get(k) for k in ("limit", "offset")):
            left = self.query(node.left, ctes, outer)
            right = self.query(node.right, ctes, outer)
            if left.frozen and right.frozen:
                return _Rel(left.columns, True, True, left.all())
            raise _Refuse("LIMIT")
        left = self.query(node.left, ctes, outer)
        right = self.query(node.right, ctes, outer)
        if len(left.columns) != len(right.columns):
            raise _Refuse("set operation arity")
        frozen = left.frozen and right.frozen
        if frozen:
            return _Rel(left.columns, True, True, left.all())
        if isinstance(node, exp.Union):
            grows = left.grows and right.grows
            stable = None if left.stable is None or right.stable is None else left.stable & right.stable
            return _Rel(left.columns, False, grows, stable)
        if isinstance(node, exp.Intersect):
            grows = left.grows and right.grows
            return _Rel(left.columns, False, grows, left.all() if grows else None)
        if isinstance(node, exp.Except):
            grows = left.grows and right.frozen
            return _Rel(left.columns, False, grows, left.all() if grows else None)
        raise _Refuse("set operation")

    def table(self, node: exp.Table, ctes: dict[str, _Rel]) -> _Rel:
        if any(v for k, v in node.args.items() if k not in ("this", "db", "catalog", "alias")):
            raise _Refuse("table modifiers (snapshot, sample, ...)")
        name = node.name.lower()
        alias = (node.alias or node.name).lower()
        if not node.args.get("db") and name in ctes:
            rel = ctes[name]
            return _Rel([(alias, n) for _, n in rel.columns], rel.frozen, rel.grows, rel.stable)
        if name == self.target:
            raise _Refuse("the query reads the table it builds")
        if name not in self.sources:
            raise _Refuse(f"unknown table {name}")
        state, stable_columns = self.states[name]
        columns = [(alias, c.lower()) for c in self.sources[name].columns]
        if state in ("frozen", "growing"):
            return _Rel(columns, state == "frozen", True, frozenset(range(len(columns))))
        if state == "keyed":
            return _Rel(columns, False, False, frozenset(i for i, (_, c) in enumerate(columns) if c in stable_columns))
        return _Rel(columns, False, False, None)

    def from_item(self, node: exp.Expression, ctes: dict[str, _Rel], outer: _Scope | None) -> tuple[str, _Rel]:
        if isinstance(node, exp.Table):
            return (node.alias or node.name).lower(), self.table(node, ctes)
        if isinstance(node, exp.Subquery):
            alias = node.alias or ""  # an unnamed derived table: its columns are read unqualified
            inner = exp.Subquery(this=node.this)
            return alias.lower(), self._rename(self.query(inner, ctes, None), alias, node)
        raise _Refuse(f"FROM {type(node).__name__}")

    def scope(self, select: exp.Select, ctes: dict[str, _Rel], outer: _Scope | None) -> _Scope:
        scope = _Scope(outer, ctes)
        source = select.args.get("from_") or select.args.get("from")
        if source is None:
            return scope  # SELECT without FROM: one constant row
        alias, rel = self.from_item(source.this, ctes, outer)
        self._add(scope, alias, rel)
        scope.frozen, scope.grows = rel.frozen, rel.grows
        scope.stable = None if rel.stable is None else {(0, p) for p in rel.stable}
        for join in select.args.get("joins") or []:
            self.join(scope, join, ctes, outer)
        return scope

    def _add(self, scope: _Scope, alias: str, rel: _Rel) -> int:
        if any(a == alias for a, _ in scope.items):
            raise _Refuse(f"repeated alias {alias}")
        scope.items.append((alias, rel))
        return len(scope.items) - 1

    def join(self, scope: _Scope, join: exp.Join, ctes: dict[str, _Rel], outer: _Scope | None) -> None:
        if join.args.get("method") or (join.kind or "").upper() not in ("", "INNER", "OUTER", "CROSS"):
            raise _Refuse(f"{join.kind} join")
        alias, rel = self.from_item(join.this, ctes, outer)
        before_items = len(scope.items)
        index = self._add(scope, alias, rel)
        condition = join.args.get("on")
        using = join.args.get("using")
        if using:
            parts = []
            for column in using:
                name = column.name if isinstance(column, (exp.Identifier, exp.Column)) else str(column)
                left = [a for a, r in scope.items[:before_items] if any(n == name.lower() for _, n in r.columns)]
                if len(left) != 1:
                    raise _Refuse("USING column")
                parts.append(exp.EQ(this=exp.column(name, table=left[0]), expression=exp.column(name, table=alias)))
            condition = exp.and_(*parts) if parts else None
        side = (join.side or "").upper()
        everything = {(i, p) for i, (_, r) in enumerate(scope.items) for p in range(len(r.columns))}
        right = {(index, p) for p in rel.stable or ()}
        left_stable = scope.stable
        scope.frozen = scope.frozen and rel.frozen
        if side == "":
            condition_ok = condition is None or self.positive(condition, scope, everything)
            scope.grows = scope.grows and rel.grows and condition_ok
            if left_stable is None or rel.stable is None:
                scope.stable = None
            else:
                both = left_stable | right
                scope.stable = both if condition is None or self.positive(condition, scope, both) else None
        elif side == "LEFT":
            condition_ok = condition is None or self.decided(condition, scope, everything)
            scope.grows = scope.grows and rel.frozen and condition_ok
            if left_stable is not None and rel.frozen:
                with_right = left_stable | {(index, p) for p in range(len(rel.columns))}
                if condition is None or self.decided(condition, scope, with_right):
                    left_stable = with_right
            scope.stable = left_stable
        else:
            raise _Refuse(f"{side} JOIN")
        if scope.frozen:
            scope.grows, scope.stable = True, everything

    def select(self, select: exp.Select, ctes: dict[str, _Rel], outer: _Scope | None) -> _Rel:
        for key in ("laterals", "pivots", "into", "sample", "settings", "connect", "match", "prewhere", "windows"):
            if select.args.get(key):
                raise _Refuse(key)
        if select.args.get("kind"):
            raise _Refuse("SELECT AS STRUCT/VALUE")
        distinct = select.args.get("distinct")
        if distinct is not None and distinct.args.get("on"):
            raise _Refuse("DISTINCT ON")
        scope = self.scope(select, ctes, outer)
        everything = {(i, p) for i, (_, r) in enumerate(scope.items) for p in range(len(r.columns))}
        projections = self._projections(select, scope)
        names = [name for name, _ in projections]
        if scope.frozen and self._fixed(select, scope, everything):
            # every input is fixed and every outer column read is stable: the output never changes
            return _Rel([("", n) for n in names], True, True, frozenset(range(len(names))))
        scope.frozen = False
        where = select.args.get("where")
        if where is not None:
            scope.grows = scope.grows and self.positive(where.this, scope, everything)
            if scope.stable is not None and not self.positive(where.this, scope, scope.stable):
                scope.stable = None
        if scope.grows and scope.stable is not None:
            scope.stable = everything
        group = select.args.get("group")
        qualify = select.args.get("qualify")
        if group is not None or any(_has_aggregate(e) for _, e in projections) or select.args.get("having") is not None:
            stable = None if qualify is not None else self._grouped(select, scope, projections)
            grows = False
        else:
            stable = None
            if scope.stable is not None:
                stable = frozenset(i for i, (_, e) in enumerate(projections) if self._stable_value(e, scope))
            grows = scope.grows and stable is not None and len(stable) == len(projections)
            if qualify is not None:
                stable, grows = self._qualify(qualify.this, scope, projections, stable), False
        if select.args.get("limit") or select.args.get("offset"):
            return _Rel([("", n) for n in names], False, False, None)
        return _Rel([("", n) for n in names], False, grows and stable is not None, stable)

    def _fixed(self, select: exp.Select, scope: _Scope, everything: set) -> bool:
        """With frozen inputs: no run-dependent value, and every outer column and subquery read is fixed."""

        if _run_dependent(select):
            return False
        clauses = [*select.expressions, *(select.args.get(k) for k in ("where", "group", "having", "qualify"))]
        clauses += [j.args.get("on") for j in select.args.get("joins") or []]
        for clause in (c for c in clauses if c is not None):
            for column in _own_columns(clause):
                found = scope.resolve(column) if not isinstance(column.this, exp.Star) else (scope, 0, 0)
                if found is None:
                    return False
                owner, item, position = found
                if owner is not scope and not owner.is_stable(item, position):
                    return False
            for sub in _own_subqueries(clause):
                if not self._subquery(sub, scope, everything).frozen:
                    return False
        return True

    def _projections(self, select: exp.Select, scope: _Scope) -> list[tuple[str, exp.Expression]]:
        out: list[tuple[str, exp.Expression]] = []
        for e in select.expressions:
            star = e if isinstance(e, exp.Star) else (e.this if isinstance(e, exp.Column) and isinstance(e.this, exp.Star) else None)
            if star is not None:
                if star.args.get("replace") or star.args.get("rename") or star.args.get("ilike"):
                    raise _Refuse("SELECT * REPLACE")
                # sqlglot 30 names the EXCEPT list "except_", older releases "except"
                excluded = {c.name.lower() for c in star.args.get("except_") or star.args.get("except") or []}
                qualifier = e.table.lower() if isinstance(e, exp.Column) and e.table else ""
                for alias, rel in scope.items:
                    if qualifier and alias != qualifier:
                        continue
                    for _, name in rel.columns:
                        if name not in excluded:
                            out.append((name, exp.column(name, table=alias)))
                continue
            name = e.alias_or_name.lower() if isinstance(e, (exp.Alias, exp.Column)) else ""
            out.append((name, e.unalias() if isinstance(e, exp.Alias) else e))
        return out

    # -- values and predicates --------------------------------------------

    def _columns_in(self, node: exp.Expression, scope: _Scope) -> Iterable[tuple[_Scope, int, int]]:
        """Column references of ``node`` outside its subqueries, resolved."""

        for column in _own_columns(node):
            found = scope.resolve(column)
            if found is None:
                raise _Refuse(f"unresolved column {column.sql()}")
            yield found

    def _stable_value(self, node: exp.Expression, scope: _Scope, stable: set | frozenset | None = None) -> bool:
        """``node``'s value for a row is fixed by that row's stable columns (and frozen tables)."""

        stable = scope.stable if stable is None else stable
        if _run_dependent(node) or any(isinstance(n, (exp.Window, exp.AggFunc)) for n in _own_nodes(node)):
            return False
        for owner, item, position in self._columns_in(node, scope):
            if owner is scope:
                if stable is None or (item, position) not in stable:
                    return False
            elif not owner.is_stable(item, position):
                return False
        for sub in _own_subqueries(node):
            if not self._subquery(sub, scope, stable).frozen:
                return False
        return True

    def _subquery(self, node: exp.Expression, scope: _Scope, stable) -> _Rel:
        """Analyze a subquery whose outer references read ``scope`` with ``stable`` as its stable set."""

        view = _Scope(scope.outer, scope.ctes)
        view.items, view.frozen, view.grows = scope.items, scope.frozen, scope.grows
        view.stable = set(stable) if stable is not None else None
        query = node.this if isinstance(node, exp.Subquery) else node
        return self.query(query, scope.ctes, view)

    def decided(self, node: exp.Expression, scope: _Scope, stable) -> bool:
        """A condition whose value for a row is fixed by stable columns and frozen tables."""

        return self._stable_value(node, scope, stable)

    def positive(self, node: exp.Expression, scope: _Scope, stable) -> bool:
        """A condition that, once TRUE for a row whose stable columns persist, stays TRUE."""

        if isinstance(node, exp.Paren):
            return self.positive(node.this, scope, stable)
        if isinstance(node, (exp.And, exp.Or)):
            return self.positive(node.left, scope, stable) and self.positive(node.right, scope, stable)
        if isinstance(node, exp.In) and node.args.get("query") is not None and not node.args.get("unnest"):
            if not self._stable_value(node.this, scope, stable):
                return False
            rel = self._subquery(node.args["query"], scope, stable)
            return rel.frozen or (rel.stable is not None and len(rel.columns) == 1 and 0 in rel.stable)
        if isinstance(node, exp.Exists):
            rel = self._subquery(node.this, scope, stable)
            return rel.frozen or rel.stable is not None
        return self.decided(node, scope, stable)

    # -- aggregation and QUALIFY --------------------------------------------

    def _grouped(self, select: exp.Select, scope: _Scope, projections) -> frozenset[int] | None:
        group = select.args.get("group")
        if group is not None and any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "all", "totals")):
            raise _Refuse("grouping sets")
        keys: list[exp.Expression] = []
        for e in group.expressions if group is not None else []:
            if isinstance(e, exp.Literal) and not e.is_string:
                position = int(e.this) - 1
                if not 0 <= position < len(projections):
                    raise _Refuse("GROUP BY position")
                e = projections[position][1]
            elif isinstance(e, exp.Column) and not e.table:
                # BigQuery lets GROUP BY name an output alias
                named = [p for n, p in projections if n == e.name.lower()]
                if named and scope.resolve(e) is None:
                    e = named[0]
                elif named and named[0].sql() != e.sql() and not (isinstance(named[0], exp.Column) and scope.resolve(named[0]) == scope.resolve(e)):
                    raise _Refuse("GROUP BY name is both a column and an output alias")
            keys.append(e)
        if scope.stable is None or not all(self._stable_value(k, scope) for k in keys):
            return None
        stable = frozenset(i for i, (_, e) in enumerate(projections) if _determined_by(e, keys, scope) and not _run_dependent(e))
        having = select.args.get("having")
        if having is not None and not self._having(having.this, scope, keys):
            return None
        return stable

    def _having(self, node: exp.Expression, scope: _Scope, keys: list[exp.Expression]) -> bool:
        if isinstance(node, exp.Paren):
            return self._having(node.this, scope, keys)
        if isinstance(node, (exp.And, exp.Or)):
            return self._having(node.left, scope, keys) and self._having(node.right, scope, keys)
        if _determined_by(node, keys, scope) and not _run_dependent(node):
            return True
        return scope.grows and _monotone_aggregate_condition(node, self, scope)

    def _qualify(self, node: exp.Expression, scope: _Scope, projections, stable) -> frozenset[int] | None:
        windows = {n: e for n, e in projections if isinstance(e, exp.Window)}
        if isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, (exp.EQ, exp.LTE, exp.LT, exp.GTE, exp.GT)):
            window, bound = node.this, node.expression
            flipped = isinstance(bound, exp.Window) or (isinstance(bound, exp.Column) and bound.name.lower() in windows)
            if flipped:
                window, bound = bound, window
            if isinstance(window, exp.Column) and not window.table and window.name.lower() in windows:
                window = windows[window.name.lower()]
            op = type(node)
            if flipped:
                op = {exp.LTE: exp.GTE, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.GT: exp.LT}.get(op, op)
            keeps_first = isinstance(bound, exp.Literal) and not bound.is_string and (
                (op is exp.EQ and bound.this == "1") or (op is exp.LTE and float(bound.this) >= 1) or (op is exp.LT and float(bound.this) > 1)
            )
            if keeps_first and isinstance(window, exp.Window) and isinstance(window.this, _RANKING):
                partition = list(window.args.get("partition_by") or [])
                if scope.stable is None or not all(self._stable_value(p, scope) for p in partition):
                    return None
                return frozenset(i for i, (_, e) in enumerate(projections) if _determined_by(e, partition, scope) and not _run_dependent(e))
        # any other QUALIFY must be decided by stable, non-window columns
        if any(isinstance(n, exp.Window) for n in node.walk()):
            return None
        return stable if self.decided(node, scope, scope.stable) else None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _own_nodes(node: exp.Expression) -> Iterable[exp.Expression]:
    """``node`` and its descendants, not entering subqueries."""

    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in current.iter_expressions():
            if isinstance(child, (exp.Subquery, exp.Select, exp.SetOperation)):
                continue
            stack.append(child)


def _own_columns(node: exp.Expression) -> list[exp.Column]:
    return [n for n in _own_nodes(node) if isinstance(n, exp.Column) and not isinstance(n.this, exp.Star)]


def _own_subqueries(node: exp.Expression) -> list[exp.Expression]:
    found = []
    stack = [node]
    while stack:
        current = stack.pop()
        for child in current.iter_expressions():
            if isinstance(child, (exp.Subquery, exp.Select, exp.SetOperation)):
                found.append(child)
            else:
                stack.append(child)
    return found


def _run_dependent(node: exp.Expression) -> bool:
    for n in node.walk():
        if isinstance(n, _RUN_DEPENDENT):
            return True
        if isinstance(n, exp.Anonymous) and str(n.this).upper() in _NONDETERMINISTIC_NAMES:
            return True
    return False


def _has_aggregate(node: exp.Expression) -> bool:
    return any(isinstance(n, exp.AggFunc) and n.find_ancestor(exp.Window) is None for n in _own_nodes(node)) and not isinstance(
        node, exp.Window
    )


def _determined_by(node: exp.Expression, keys: list[exp.Expression], scope: "_Scope") -> bool:
    """``node`` is computed from the ``keys`` expressions alone (no other column, aggregate or window)."""

    wanted = {k.sql() for k in keys}
    positions = set()
    for k in keys:
        if isinstance(k, exp.Column):
            try:
                found = scope.resolve(k)
            except _Refuse:
                found = None
            if found is not None and found[0] is scope:
                positions.add(found[1:])

    def covered(n: exp.Expression) -> bool:
        if n.sql() in wanted:
            return True
        if isinstance(n, exp.Column):
            try:
                found = scope.resolve(n)
            except _Refuse:
                return False
            return found is not None and found[0] is scope and found[1:] in positions
        if isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select)):
            return False
        return all(covered(c) for c in n.iter_expressions())

    return covered(node)


def _monotone_aggregate_condition(node: exp.Expression, analyzer: Analyzer, scope: _Scope) -> bool:
    """A HAVING atom that, over a group that only gains rows, stays TRUE once TRUE."""

    if not isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return False
    left, right = node.this, node.expression
    if isinstance(left, exp.Literal) and not isinstance(right, exp.Literal):
        flip = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}[type(node)]
        left, right, op = right, left, flip
    else:
        op = type(node)
    if not (isinstance(right, exp.Literal) and not right.is_string):
        return False
    if any(_run_dependent(a) for a in (left,)) or _own_subqueries(left):
        return False
    growing_up = isinstance(left, (exp.Count, exp.Max)) or (hasattr(exp, "CountIf") and isinstance(left, exp.CountIf))
    if growing_up and op in (exp.GT, exp.GTE):
        return True
    return isinstance(left, exp.Min) and op in (exp.LT, exp.LTE)


def analyze(
    query: str | exp.Expression,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    tables: Iterable[str] | None = None,
    *,
    target: str = "",
    dialect: str = "bigquery",
) -> Growth | None:
    """What of ``query``'s output survives every change the contract allows; None when not understood."""

    try:
        tree = sqlglot.parse_one(query, read=dialect) if isinstance(query, str) else query
        rel = Analyzer(sources, kinds, tables, target).query(tree, {}, None)
    except (_Refuse, sqlglot.errors.SqlglotError, ValueError):
        return None
    return Growth(tuple(n for _, n in rel.columns), rel.frozen, rel.grows, rel.stable)




def contract_constraints(
    sources: dict[str, SourceTable], kinds: Iterable[str], tables: Iterable[str] | None = None, *, exact_copies: bool = False
) -> dict:
    """``TableConstraints`` for every source that hold in every state the contract reaches.

    A declared key is unique and non-NULL at the start and stays so under inserts of new keys, updates
    (which keep the key) and deletes. ``duplicate`` re-delivers a row, so the key is no longer unique;
    with ``exact_copies`` it is kept anyway, because rows that agree on it are still equal, which is
    all a tie check needs. ``null_key`` breaks both facts. Other columns get no NOT NULL fact: the
    contract does not say they are never NULL.
    """

    from kumo_sql_prover.smt_equivalence import TableConstraints

    kinds = frozenset(kinds)
    changing = {t.lower() for t in tables} if tables else {t.lower() for t in sources}
    out = {}
    for name, table in sources.items():
        key = tuple(k.lower() for k in table.key)
        changes = kinds if name.lower() in changing else frozenset()
        if not key or "null_key" in changes:
            out[name] = TableConstraints()
        elif "duplicate" in changes and not exact_copies:
            out[name] = TableConstraints(not_null=frozenset(key))
        else:
            out[name] = TableConstraints(not_null=frozenset(key), keys=(key,))
    return out


def source_schema(sources: dict[str, SourceTable]) -> dict[str, list[str]]:
    return {name: [c.lower() for c in table.columns] for name, table in sources.items()}
