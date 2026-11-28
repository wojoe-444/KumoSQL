"""How each output column of a model is computed, and at what grain: its expression and its aggregation keys.

Column lineage says which source columns a value is built from, and :mod:`kumosql.lineage_filters` which conditions
limit its rows. Neither says what the value is: a flag computed as ``MAX(CASE WHEN UPPER(text) LIKE '%X%' THEN 1 END)``
per ``order_id`` is "built from ``text``". :func:`model_logic` reads, for each output column of one qualified query:

* ``expression``: the SQL that computes the column. A column read from a CTE or derived table of the same query is
  replaced by the expression that CTE computes it with, so ``WITH agg AS (... MAX(x) AS m ... GROUP BY k) SELECT m FROM
  agg`` shows ``MAX(x)``. Table qualifiers are dropped when every column in the expression comes from one table (the
  hop's sources say which), and kept when two tables meet, so two ``id`` columns never read as one.
* ``aggregation_keys``: the grain the value is computed at. ``None`` when no GROUP BY or aggregate computes it, ``()``
  for one aggregate over all rows, otherwise the grouping keys: ``GROUP BY 1`` and ``GROUP BY ALL`` are resolved, and a
  key that is also selected is named by its output column (``day`` for ``DATE(ts) AS day``). ``ROLLUP``, ``CUBE`` and
  ``GROUPING SETS`` are kept as written.
* ``partition_keys``: the ``PARTITION BY`` of the expression's window functions, when they share one.
* ``steps``: values inside the query the expression names instead of inlining them, each with its expression and keys.

Inlining never hides a change of grain. A value computed per group (or after ``DISTINCT``, ``QUALIFY``, ``LIMIT``, a
``UNION DISTINCT``) is inlined only into a query that does not aggregate it again and whose expression reads nothing
else, so ``COALESCE(agg.m, 0)`` becomes ``COALESCE(MAX(x), 0)`` per ``k``, while ``SUM(agg.m)`` and ``agg.m + o.x`` stay
as written and list ``agg.m`` (``MAX(x)`` per ``k``) as a step. A window value is not inlined into an aggregate or
another window. A set operation whose branches compute the column the same way has that expression; otherwise its
branches are steps.

Nothing is guessed: a column that cannot be matched to one projection (an unexpanded ``*``, a name selected twice, a
recursive CTE), a column of a set operation with an unexpanded ``*`` branch, or a query whose scopes cannot be built
has no logic.
Known limits: an aggregate function sqlglot does not know (a user-defined one; BigQuery's ``HLL_COUNT`` and
``KLL_QUANTILES`` sketches are known) is not recognised as one when the query has no ``GROUP BY``, and ``DISTINCT`` on
the column's own query is not a grain here (as for filters, it is not listed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from kumo_sql_prover.ast_utils import field_path

from .lineage_filters import _scope_label, sql_text
from .lineage_soundness import _set_operation_scopes
from .pipeline_types import LogicStep

_MAX_DEPTH = 40
#: a value with more syntax nodes than this is named as a step instead of inlined, so the expression stays readable
_INLINE_NODES = 250
_OWNER = "kumosql_logic_table"


@dataclass(frozen=True)
class ColumnLogic:
    """The logic of one output column (see the module text)."""

    expression: str | None
    aggregation_keys: tuple[str, ...] | None
    partition_keys: tuple[str, ...] | None = None
    steps: tuple[LogicStep, ...] = ()


@dataclass(frozen=True)
class _Part:
    """One column of one scope, as an expression of the scope's inputs with CTE values inlined."""

    node: exp.Expression | None  # a private copy; ``None`` when unknown or when branches differ
    keys: tuple[str, ...] | None  # the aggregation keys the value is computed at
    regrains: bool  # rows were grouped, deduplicated or cut on the way to this value
    windowed: bool  # the inlined value holds a window function
    steps: tuple[LogicStep, ...] = ()
    branches: tuple[tuple[str, "_Part"], ...] = ()  # set operation branches that differ: (scope label, branch)
    size: int = 0


_UNKNOWN = _Part(None, None, True, False)


class _Unsupported(Exception):
    pass


def model_logic(
    qualified: exp.Expression,
    names: tuple[str, ...] | list[str],
    owner: Callable[[exp.Table], str],
    masked: tuple[str, ...] = (),
) -> dict[str, ColumnLogic]:
    """The logic of each output column of a qualified query that can be worked out (see the module text)."""

    try:
        root = build_scope(qualified)
    except Exception:
        return {}
    if root is None:
        return {}
    reader = _Reader(owner, masked)
    reader.recursive = [
        cte.this
        for cte in qualified.find_all(exp.CTE)
        if isinstance(cte.parent, exp.With) and cte.parent.args.get("recursive")
    ]
    found: dict[str, ColumnLogic] = {}
    for name in names:
        if name == "*":
            continue
        try:
            logic = reader.logic(reader.part(root, name, "main", 0), name)
        except (_Unsupported, RecursionError):
            logic = None
        if logic is not None:
            found[name] = logic
    return found


class _Reader:
    def __init__(self, owner: Callable[[exp.Table], str], masked: tuple[str, ...]) -> None:
        self.owner = owner
        self.masked = masked
        self.memo: dict[tuple[int, str], _Part] = {}
        self._keys: dict[int, tuple[str, ...] | None] = {}
        self._own: dict[int, set[int]] = {}
        #: bodies of the CTEs of a ``WITH RECURSIVE``: a row of one may come from any number of rounds, so nothing in them is inlined
        self.recursive: list[exp.Expression] = []

    # ------------------------------------------------------------------ results

    def logic(self, part: _Part, name: str) -> ColumnLogic | None:
        """The result for an output column. Its part belongs to the main scope, which no other part reads or copies,
        so it is tidied in place."""

        if part.node is not None:
            node = _tidy(part.node)
            partition = _partition_keys(node, self)
            return ColumnLogic(self.text(node), part.keys, partition, part.steps)
        if not part.branches:
            return None
        steps: list[LogicStep] = []
        for label, branch in part.branches:
            steps.extend(self.branch_steps(name, label, branch))
        keys = {branch.keys for _, branch in part.branches}
        return ColumnLogic(None, keys.pop() if len(keys) == 1 else None, None, _unique(steps))

    def branch_steps(self, reference: str, label: str, branch: _Part) -> list[LogicStep]:
        text = None if branch.node is None else self.text(_tidy(branch.node.copy()))
        return [LogicStep(reference, label, text, branch.keys), *branch.steps]

    def text(self, node: exp.Expression) -> str:
        return sql_text(node, self.masked, copy=False, unnamed=True)

    # ------------------------------------------------------------------ parts

    def part(self, scope: Scope, name: str, label: str, depth: int) -> _Part:
        if depth > _MAX_DEPTH:
            raise _Unsupported
        key = (id(scope), name.casefold())
        if key in self.memo:
            return self.memo[key]
        self.memo[key] = _UNKNOWN  # a recursive CTE reads itself: unknown
        result = self._part(scope, name, label, depth)
        self.memo[key] = result
        return result

    def _part(self, scope: Scope, name: str, label: str, depth: int) -> _Part:
        expression = scope.expression
        if self.recursive and any(_inside(expression, body) for body in self.recursive):
            return _UNKNOWN
        if isinstance(expression, exp.SetOperation):
            return self._set_operation(scope, expression, name, label, depth)
        if not isinstance(expression, exp.Select):
            return _UNKNOWN
        matches = [p for p in expression.expressions if p.alias_or_name.casefold() == name.casefold()]
        if len(matches) != 1:
            return _UNKNOWN
        projection = matches[0]
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if _is_star(projection):
            return _UNKNOWN

        own_keys = self.keys(scope)
        own = self.own_columns(scope)
        copy = inner.copy()
        twins: dict[int, exp.Expression] = {}
        columns: list[exp.Column] = []
        any_window = False
        for original, twin in zip(inner.dfs(), copy.dfs()):
            twins[id(original)] = twin
            if isinstance(original, exp.Column):
                if id(original) in own:
                    columns.append(original)
            elif isinstance(original, exp.Window):
                any_window = True
        own_window = any_window and _has_window(inner)  # not one inside a nested query
        size = len(twins)
        children: list[tuple[exp.Column, Scope, str, _Part]] = []
        tables: list[tuple[exp.Column, exp.Table]] = []
        unnested: list[exp.Column] = []
        others = 0
        for column in columns:
            source = scope.sources.get(column.table) if column.table else None
            if isinstance(source, Scope) and isinstance(source.expression, (exp.Select, exp.SetOperation)):
                child_label = _scope_label(source, column.table)
                children.append((column, source, child_label, self.part(source, column.name, child_label, depth + 1)))
            elif isinstance(source, exp.Table):
                tables.append((column, source))
            else:
                others += 1  # UNNEST, an outer column, a name the qualifier could not place: shown as written
                if isinstance(source, Scope) and isinstance(source.expression, exp.Unnest):
                    unnested.append(column)  # ``UNNEST(tags) AS tag`` is read as ``tag``, not by the qualifier's ``_0``

        # A regrained value goes into a non-aggregating expression only when it is all the expression reads.
        regrained = [entry for entry in children if entry[3].regrains]
        lone_source = (
            not tables and not others and len({id(source) for _, source, _, _ in children}) == 1
            and len({part.keys for _, _, _, part in children}) == 1
        )
        inline: set[int] = set()
        for column, _source, _label, part in children:
            if part.node is None or part.size > _INLINE_NODES or field_path(column):
                continue
            if own_keys is not None or own_window:
                if part.regrains or part.windowed:
                    continue
            elif part.regrains and not lone_source:
                continue
            inline.add(id(column))

        steps: list[LogicStep] = []
        inlined_keys: set = set()
        windowed = own_window
        for column, _source, child_label, part in children:
            twin = twins[id(column)]
            if id(column) in inline:
                replacement = part.node.copy()
                if twin is copy:
                    copy = replacement
                else:
                    twin.replace(exp.Paren(this=replacement) if _compound(replacement) else replacement)
                size += part.size
                steps.extend(part.steps)
                windowed = windowed or part.windowed
                if part.regrains:
                    inlined_keys.add(part.keys)
                continue
            reference = sql_text(column, self.masked, copy=False)  # a column: nothing in it to tidy
            if part.node is not None:
                steps.extend(self.branch_steps(reference, child_label, part))
            for branch_label, branch in part.branches:
                steps.extend(self.branch_steps(reference, branch_label, branch))
        for column, table in tables:
            twins[id(column)].meta[_OWNER] = self.owner(table)
        for column in unnested:
            twins[id(column)].set("table", None)

        if own_keys is not None:
            keys = own_keys
        elif inlined_keys:
            keys = inlined_keys.pop() if len(inlined_keys) == 1 else None
        else:
            keys = None
        regrains = own_keys is not None or _regrains(expression) or bool(regrained)
        return _Part(copy, keys, regrains, windowed, _unique(steps), (), size)

    def _set_operation(self, scope: Scope, expression: exp.SetOperation, name: str, label: str, depth: int) -> _Part:
        names = [n.casefold() for n in expression.named_selects]
        if name.casefold() not in names:
            return _UNKNOWN
        index = names.index(name.casefold())
        branches: list[tuple[str, _Part]] = []
        for number, branch in enumerate(_set_operation_scopes(scope), start=1):
            if isinstance(branch.expression, exp.Select) and any(_is_star(p) for p in branch.expression.expressions):
                return _UNKNOWN  # a ``*`` left unexpanded: positions after it cannot be matched
            branch_names = branch.expression.named_selects
            if index >= len(branch_names):
                return _UNKNOWN
            branch_label = f"{label} branch {number}"
            part = self.part(branch, branch_names[index], branch_label, depth + 1)
            branches.extend(part.branches or [(branch_label, part)])
        if not branches:
            return _UNKNOWN
        regrains = _regrains(expression) or any(part.regrains for _, part in branches)
        if not (isinstance(expression, exp.Union) and expression.args.get("distinct") is False):
            regrains = True  # UNION DISTINCT, INTERSECT and EXCEPT compare whole rows
        windowed = any(part.windowed for _, part in branches)
        first = branches[0][1]
        if first.node is not None and all(part.node is not None for _, part in branches):
            texts = {self.text(_tidy(part.node.copy())) for _, part in branches}
            if len(texts) == 1 and len({part.keys for _, part in branches}) == 1:
                steps = _unique([step for _, part in branches for step in part.steps])
                return _Part(first.node, first.keys, regrains, windowed, steps, (), first.size)
        return _Part(None, None, regrains, windowed, (), tuple(branches))

    # ------------------------------------------------------------------ scopes

    def own_columns(self, scope: Scope) -> set[int]:
        found = self._own.get(id(scope))
        if found is None:
            found = self._own[id(scope)] = {id(column) for column in scope.columns}
        return found

    def keys(self, scope: Scope) -> tuple[str, ...] | None:
        """The aggregation keys of a select: ``None`` when it does not aggregate, ``()`` for one row."""

        if id(scope) not in self._keys:
            self._keys[id(scope)] = self._group_keys(scope)
        return self._keys[id(scope)]

    def _group_keys(self, scope: Scope) -> tuple[str, ...] | None:
        select = scope.expression
        group = select.args.get("group")
        projections = list(select.expressions)
        if group is None:
            if select.args.get("having") is not None or any(_aggregates(p) for p in projections):
                return ()
            return None
        single = len(scope.sources) == 1
        found: list[str] = []
        if group.args.get("all"):
            for projection in projections:
                inner = projection.this if isinstance(projection, exp.Alias) else projection
                if _aggregates(inner) or _has_window(inner) or inner.find(exp.Column) is None:
                    continue
                found.append(projection.alias_or_name)
        inners = [(p.this if isinstance(p, exp.Alias) else p, p.alias_or_name) for p in projections]
        selected = {}  # a selected column, by qualifier and name
        for inner, name in inners:
            if isinstance(inner, exp.Column):
                selected.setdefault((inner.table.casefold(), inner.name.casefold(), field_path(inner)), name)
        shown: dict[str, str] | None = None
        aliases = {name.casefold(): name for _, name in inners}
        for key in group.args.get("expressions") or ():
            if isinstance(key, exp.Literal) and key.is_int and 0 < int(key.this) <= len(projections):
                found.append(projections[int(key.this) - 1].alias_or_name)  # GROUP BY 1
                continue
            if isinstance(key, exp.Column):
                if not key.table and key.name.casefold() in aliases:
                    found.append(aliases[key.name.casefold()])  # an alias the qualifier left as written
                    continue
                alias = selected.get((key.table.casefold(), key.name.casefold(), field_path(key)))
            else:
                if shown is None:
                    shown = {}
                    for inner, name in inners:
                        if not isinstance(inner, exp.Column):
                            shown.setdefault(inner.sql(dialect="bigquery"), name)
                alias = shown.get(key.sql(dialect="bigquery"))
            if alias is not None:
                found.append(alias)
                continue
            if single and any(column.table for column in key.find_all(exp.Column)):
                key = key.copy()
                for column in key.find_all(exp.Column):
                    column.set("table", None)
                found.append(self.text(key))
            else:
                found.append(sql_text(key, self.masked))
        return tuple(dict.fromkeys(found))


def _is_star(projection: exp.Expression) -> bool:
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    return isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star))


def _regrains(query: exp.Expression) -> bool:
    """A query that groups, deduplicates or cuts its rows (its aggregation is checked by the caller)."""

    return any(query.args.get(arg) for arg in ("group", "having", "distinct", "qualify", "limit", "offset"))


def _in_scope(node: exp.Expression):
    """``node`` and what is under it, without nested queries."""

    return node.walk(prune=lambda n: n is not node and isinstance(n, (exp.Query, exp.Subquery)))


def _aggregates(node: exp.Expression) -> bool:
    """An aggregate function of this query level: not one inside a window's function or a nested query."""

    for item in node.walk(prune=lambda n: n is not node and isinstance(n, (exp.Query, exp.Subquery, exp.Window))):
        if isinstance(item, exp.AggFunc) or _sketch_aggregate(item):
            return True
        if isinstance(item, exp.Window):
            for part in (*(item.args.get("partition_by") or ()), item.args.get("order")):
                if part is not None and _aggregates(part):
                    return True
    return False


#: BigQuery's sketch functions that aggregate (``HLL_COUNT.MERGE``, ``KLL_QUANTILES.INIT_INT64``); sqlglot reads them as
#: a dotted call it does not know. ``HLL_COUNT.EXTRACT`` and ``KLL_QUANTILES.EXTRACT_*`` are scalar.
_SKETCHES = {"HLL_COUNT", "KLL_QUANTILES"}


def _sketch_aggregate(node: exp.Expression) -> bool:
    if not isinstance(node, exp.Dot) or not isinstance(node.expression, exp.Anonymous):
        return False
    family = node.this.name.upper() if isinstance(node.this, (exp.Var, exp.Identifier, exp.Column)) else ""
    return family in _SKETCHES and node.expression.name.upper().startswith(("INIT", "MERGE"))


def _has_window(node: exp.Expression) -> bool:
    return any(isinstance(item, exp.Window) for item in _in_scope(node))


def _inside(node: exp.Expression, container: exp.Expression) -> bool:
    while node is not None:
        if node is container:
            return True
        node = node.parent
    return False


def _compound(node: exp.Expression) -> bool:
    """An expression that needs parentheses where a column stood (``a + b`` in ``x * 2``)."""

    return isinstance(node, (exp.Binary, exp.Unary, exp.Predicate)) and not isinstance(node, exp.Paren)


def _tidy(node: exp.Expression) -> exp.Expression:
    """Drop table qualifiers when every column the expression reads comes from one table.

    Inlined values keep the qualifiers of the query they were written in, so two of them can use one alias for two
    tables (``r.id`` and ``r.id``); those are named by their table instead.
    """

    columns = [column for column in _in_scope(node) if isinstance(column, exp.Column) and column.table]
    owners = {column.meta.get(_OWNER) for column in columns}
    if len(owners) == 1 and None not in owners:
        for column in columns:
            column.set("table", None)
        return node
    spelled: dict[str, set] = {}
    for column in columns:
        spelled.setdefault(column.table, set()).add(column.meta.get(_OWNER))
    if all(len(found) == 1 for found in spelled.values()):
        return node
    known = sorted(owner for owner in owners if owner is not None)
    names = {owner: owner.split(".")[-1] for owner in known}
    if len(set(names.values())) != len(names):
        names = {owner: owner for owner in known}
    for column in columns:
        owner = column.meta.get(_OWNER)
        if owner is not None:
            column.set("table", exp.to_identifier(names[owner]))
    return node


def _partition_keys(node: exp.Expression, reader: _Reader) -> tuple[str, ...] | None:
    windows = [item for item in _in_scope(node) if isinstance(item, exp.Window)]
    if not windows or any(window.args.get("alias") for window in windows):
        return None  # no window, or ``OVER w``: a named window whose PARTITION BY is written elsewhere
    found = {
        tuple(sql_text(key, reader.masked) for key in window.args.get("partition_by") or ()) for window in windows
    }
    return found.pop() if len(found) == 1 else None


def _unique(steps) -> tuple[LogicStep, ...]:
    return tuple(dict.fromkeys(steps))
