"""A comparable description of each table: grain, attribute meaning and row scope.

Text similarity finds copied SQL. It misses two tables that answer the same
question with different SQL. A ``TableProfile`` describes what a table *is*,
independent of names and formatting, so profiles of two tables can be compared:

* ``Grain``: the output columns that identify one row (``derived`` from the
  query, ``declared`` by the caller, or ``unknown`` with a reason).
* ``AttributeMeaning``: what each output column contains, as a canonical string
  built from column lineage (``col:<table>.<column>`` for a source column,
  ``agg:SUM(col:...)`` for an aggregate, ``expr:...`` for anything else).
* ``RowScope``: the normalized filter conjuncts that limit which rows are kept.

Nothing is guessed. Whatever cannot be determined is ``unknown`` with a reason,
and a profile is ``complete`` only when all three parts are known.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlglot import exp

from kumo_sql_prover.ast_utils import extended_grouping, is_aggregate, star_modifier
from kumo_sql_prover.output_properties import set_returning_item
from .pipeline import ColumnRef, Model, Pipeline, Target, _parse_script, _table_name_for_schema
from kumo_sql_prover.set_operations import is_by_name

__all__ = [
    "AttributeMeaning",
    "Grain",
    "RowScope",
    "TableProfile",
    "profile_pipeline",
    "profile_query",
]

_QUERY_KEY = "__proposed_query__"

# How a column meaning is written, for the code that searches meanings (filters, aggregates) for the columns they read.
# A table name may hold ``[PLACEHOLDER]`` parts (a template project); an array index such as ``[0]`` ends the name.
SOURCE_COLUMN = r"col:(?:\[[A-Z_][A-Z0-9_]*\]|[^\s()|,\[\]])+"
FIELD_COLUMN = rf"field\({SOURCE_COLUMN}, [A-Za-z0-9_.]+\)"
COLUMN_MEANING = rf"(?:{FIELD_COLUMN}|{SOURCE_COLUMN})"
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FIELD_MEANING = re.compile(rf"field\(({SOURCE_COLUMN}), ([A-Za-z0-9_.]+)\)")
_MAX_DEPTH = 12


# --------------------------------------------------------------------- results


@dataclass(frozen=True)
class Grain:
    """The output columns that identify one row.

    ``keys`` are output column names, sorted case-insensitively; look up what
    each one means in ``TableProfile.attributes``. ``status`` is ``derived``,
    ``declared`` or ``unknown`` (then ``keys`` is empty and ``reason`` says why).
    An empty ``keys`` with a known status means the table has a single row.
    """

    keys: tuple[str, ...] = ()
    status: str = "unknown"
    reason: str | None = None

    @property
    def known(self) -> bool:
        return self.status in ("derived", "declared")

    def to_json(self) -> dict:
        return {"keys": list(self.keys), "status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class AttributeMeaning:
    """What one output column contains, independent of its name.

    ``meaning`` is ``None`` when ``status`` is ``unknown``. Otherwise it starts
    with ``col:`` (a source column reached through passthroughs, renames, views
    and CTEs), ``field(col:..., a.b)`` (a path of field names inside one source
    column), ``agg:`` (an aggregate: function and input meaning), ``window:``,
    ``union(`` , ``const:`` or ``expr:`` (a normalized expression).
    ``equality_only`` marks meanings that can be compared for equality but never
    for similarity. ``sources`` are the ultimate source columns and
    ``transform`` is the strongest step inside the model, from column lineage.
    """

    column: str
    meaning: str | None = None
    status: str = "unknown"
    reason: str | None = None
    sources: tuple[str, ...] = ()
    transform: str | None = None
    equality_only: bool = False

    def to_json(self) -> dict:
        return {
            "column": self.column,
            "meaning": self.meaning,
            "status": self.status,
            "reason": self.reason,
            "sources": list(self.sources),
            "transform": self.transform,
            "equality_only": self.equality_only,
        }


@dataclass(frozen=True)
class RowScope:
    """The normalized filter conjuncts that limit which rows a table holds.

    ``filters`` are sorted and written in terms of column meanings, so they do
    not depend on names, casing or clause order, and they include the filters
    of the tables this one reads. ``comparable`` is false when the scope cannot
    be compared: a ``LIMIT``, sampling, or a filter that could not be normalized
    (kept as ``opaque:`` text).
    """

    filters: tuple[str, ...] = ()
    comparable: bool = True
    reason: str | None = None

    def to_json(self) -> dict:
        return {"filters": list(self.filters), "comparable": self.comparable, "reason": self.reason}


@dataclass(frozen=True)
class TableProfile:
    """Grain, attribute meanings and row scope of one table or query."""

    table: str
    grain: Grain
    attributes: tuple[AttributeMeaning, ...]
    row_scope: RowScope
    complete: bool | None = None

    def __post_init__(self) -> None:
        if self.complete is None:
            object.__setattr__(
                self,
                "complete",
                bool(
                    self.grain.known
                    and self.attributes
                    and all(a.status == "known" for a in self.attributes)
                    and self.row_scope.comparable
                ),
            )

    def attribute(self, column: str) -> AttributeMeaning | None:
        for item in self.attributes:
            if item.column.lower() == column.lower():
                return item
        return None

    def to_json(self) -> dict:
        return {
            "table": self.table,
            "complete": self.complete,
            "grain": self.grain.to_json(),
            "attributes": [a.to_json() for a in self.attributes],
            "row_scope": self.row_scope.to_json(),
        }


# ------------------------------------------------------------------ public API


def profile_pipeline(
    pipeline: Pipeline, *, declared_grain: Mapping[str, Sequence[str]] | None = None
) -> dict[str, TableProfile]:
    """Profile every model of ``pipeline``, keyed by model key. Never raises.

    ``declared_grain`` maps a model (or source table) key to the columns a
    project declares unique, for example from a unique-key assertion.
    """

    remember = getattr(pipeline, "_remembered", None)
    if remember is not None:  # the project is immutable: a report asking per model must not profile every model again each time
        grain = tuple(sorted((key, tuple(columns)) for key, columns in declared_grain.items())) if declared_grain else None
        return dict(remember(("profiles", grain), lambda: _profile_pipeline(pipeline, declared_grain)))
    return _profile_pipeline(pipeline, declared_grain)


def _profile_pipeline(
    pipeline: Pipeline, declared_grain: Mapping[str, Sequence[str]] | None
) -> dict[str, TableProfile]:
    result: dict[str, TableProfile] = {}
    try:
        profiler = _Profiler(pipeline, declared_grain)
        order = [key for key in profiler.order if key in pipeline.models]
    except Exception as exc:  # never raise on odd input
        return {key: _failed(key, exc) for key in getattr(pipeline, "models", {})}
    for key in order:
        result[key] = profiler.profile(key)
    for key in pipeline.models:
        result.setdefault(key, profiler.profile(key))
    return {key: result[key] for key in pipeline.models}


def profile_query(
    pipeline: Pipeline, sql: str, *, declared_grain: Mapping[str, Sequence[str]] | None = None
) -> TableProfile:
    """Profile a query that is not in the pipeline, against the pipeline's tables.

    Tables the query reads resolve to pipeline models and sources, so its
    profile is comparable with theirs. Never raises.
    """

    try:
        key = _QUERY_KEY
        while key in pipeline.models:
            key += "_"
        models = _upstream_models(pipeline, sql)
        models[key] = Model(Target("", "", key), "table", sql if isinstance(sql, str) else "")
        scratch = Pipeline(
            models,
            sources=dict(pipeline.sources),
            source_schema=dict(pipeline.source_schema),
            default_project=pipeline.default_project,
            default_dataset=pipeline.default_dataset,
        )
        profiler = _Profiler(scratch, declared_grain)
        return profiler.profile(key, label="<query>")
    except Exception as exc:
        return _failed("<query>", exc)


def _upstream_models(pipeline: Pipeline, sql: object) -> dict[str, Model]:
    """The models ``sql`` reads, directly or not; every model when that cannot be told.

    A query profile depends only on what sits upstream of it, so the scratch pipeline built for one
    need not re-analyse the rest of the project (seconds per call on a large one).
    """

    try:
        query, analysis = _parse_script(sql if isinstance(sql, str) else "")
        if query is None:
            raise ValueError("no query")
        upstream = pipeline._analyse().upstream
        stack = []
        for table in (*query.find_all(exp.Table), *analysis.all_reads()):
            if (resolved := pipeline.resolve(table)) and resolved in pipeline.models:
                stack.append(resolved)
        keep: set[str] = set()
        while stack:
            key = stack.pop()
            if key not in keep:
                keep.add(key)
                stack.extend(upstream.get(key, ()))
        return {key: model for key, model in pipeline.models.items() if key in keep}
    except Exception:  # noqa: BLE001 - fall back to the whole project
        return dict(pipeline.models)


def _failed(table: str, exc: BaseException) -> TableProfile:
    reason = f"profile_error: {type(exc).__name__}"
    return TableProfile(
        table,
        Grain(reason=reason),
        (),
        RowScope(comparable=False, reason=reason),
    )


# ------------------------------------------------------------------- internals


class _Unresolved(Exception):
    """An expression whose meaning cannot be determined; carries the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Src:
    alias: str
    kind: str  # "model", "table", "query" or "opaque"
    label: str
    key: str = ""
    query: exp.Expression | None = None
    env: dict | None = None
    # The only names an aliased UNNEST makes visible (its element, and its offset column); None when not known.
    names: tuple[str, ...] | None = None


@dataclass
class _Ctx:
    select: exp.Select
    env: dict
    srcs: list[_Src]
    joins: list[exp.Join | None]
    projs: list[tuple[str, exp.Expression]]  # (lower output name, expression); "" for unnamed
    names: tuple[str, ...]  # output names as written; "*" for a star

    @property
    def by_alias(self) -> dict[str, _Src]:
        return {s.alias: s for s in self.srcs if s.alias}


def _unwrap(query: exp.Expression) -> exp.Expression:
    while isinstance(query, exp.Subquery):
        query = query.this
    return query


def _is_star(expr: exp.Expression) -> bool:
    return isinstance(expr, exp.Star) or (isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star))


def _split_and(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, (exp.Where, exp.Having, exp.Qualify)):
        node = node.this
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _split_and(node.this) + _split_and(node.expression)
    return [node]


def _canon_pass(node: exp.Expression) -> exp.Expression:
    if isinstance(node, (exp.EQ, exp.NEQ)):
        left, right = node.this, node.expression
        if left.sql() > right.sql():
            return type(node)(this=right.copy(), expression=left.copy())
    elif isinstance(node, exp.LT):
        return exp.GT(this=node.expression.copy(), expression=node.this.copy())
    elif isinstance(node, exp.LTE):
        return exp.GTE(this=node.expression.copy(), expression=node.this.copy())
    elif isinstance(node, (exp.And, exp.Or)):
        kind = type(node)
        operands: list[exp.Expression] = []
        stack = [node]
        while stack:
            item = stack.pop()
            if isinstance(item, kind):
                stack.extend([item.expression, item.this])
            else:
                operands.append(item)
        ordered = sorted(operands, key=lambda n: n.sql())
        if [o.sql() for o in ordered] != [o.sql() for o in operands]:
            result = ordered[0].copy()
            for item in ordered[1:]:
                result = kind(this=result, expression=item.copy())
            return result
    elif isinstance(node, exp.In) and node.args.get("expressions") and not node.args.get("query"):
        items = node.args["expressions"]
        ordered = sorted(items, key=lambda n: n.sql())
        if [o.sql() for o in ordered] != [o.sql() for o in items]:
            clone = node.copy()
            clone.set("expressions", [o.copy() for o in ordered])
            return clone
    return node


def _join_field_paths(expr: exp.Expression) -> exp.Expression:
    """``a.b.c.d.e`` parses as a column ``a.b.c.d`` with ``.e`` after it; make it one column with the whole path.

    sqlglot keeps four name parts in a column and puts the rest in ``Dot`` nodes. The path is kept in the column's
    ``meta["parts"]`` so every spelling of one path reads the same.
    """

    def join(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Dot):
            return node
        names: list[str] = []
        leaf: exp.Expression = node
        while isinstance(leaf, exp.Dot) and isinstance(leaf.expression, exp.Identifier):
            names.append(leaf.expression.name)
            leaf = leaf.this
        if not names or not isinstance(leaf, exp.Column) or isinstance(leaf.this, exp.Star):
            return node
        column = leaf.copy()
        column.meta["parts"] = [part.name for part in leaf.parts] + names[::-1]
        return column

    return expr.transform(join)


def _is_call(text: str) -> bool:
    """Whether ``text`` is one function call, ``FN(...)``, so it needs no parentheses when inlined."""

    match = re.match(r"[A-Z][A-Z_0-9]*\(", text)
    if match is None:
        return False
    depth = 0
    for index in range(match.end() - 1, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return index == len(text) - 1
    return False


def _meaning_node(meaning: str) -> exp.Expression:
    """The node that stands for a column's meaning inside a larger expression.

    A composite ``expr:`` meaning is parenthesised, so ``SUM(margin)`` over a view
    column reads the same as ``SUM(price - cost)`` written out in place.
    """

    if meaning.startswith("expr:"):
        inner = meaning[len("expr:"):]
        return exp.Var(this=inner) if _is_call(inner) else exp.Paren(this=exp.Var(this=inner))
    return exp.Var(this=meaning)


_REAGGREGATE = {exp.Sum: ("SUM", "COUNT"), exp.Min: ("MIN",), exp.Max: ("MAX",)}


def _collapse_nested(node: exp.Expression) -> str | None:
    """``SUM(SUM(x))`` is ``SUM(x)`` (likewise MIN, MAX and ``SUM(COUNT(x))``).

    Only when the inner aggregate is a plain, non-distinct one: the same rows are
    combined either way. Any filter between the two stages is a row-scope filter.
    """

    inner = node.this
    if not isinstance(inner, exp.Var) or not inner.name.startswith("agg:"):
        return None
    for kind, allowed in _REAGGREGATE.items():
        if type(node) is kind:
            match = re.match(r"agg:([A-Z_0-9]+)\((?!DISTINCT )", inner.name)
            if match and match.group(1) in allowed:
                return inner.name
    return None


def _canon_sql(node: exp.Expression) -> str:
    current = node.copy()
    text = current.sql(dialect="bigquery", normalize_functions="upper")
    for _ in range(6):
        current = current.transform(_canon_pass)
        again = current.sql(dialect="bigquery", normalize_functions="upper")
        if again == text:
            break
        text = again
    return text


_NONDETERMINISTIC = tuple(
    getattr(exp, name)
    for name in ("Rand", "CurrentDate", "CurrentTimestamp", "CurrentTime", "CurrentDatetime", "Uuid")
    if hasattr(exp, name)
)
_NONDETERMINISTIC_NAMES = {"GENERATE_UUID", "RAND", "SESSION_USER", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_DATETIME", "CURRENT_TIME"}


def _has_aggregate(expr: exp.Expression) -> bool:
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, (exp.Window, exp.Query)):
            continue
        if is_aggregate(node):
            return True
        stack.extend(node.iter_expressions())
    return False


class _Profiler:
    def __init__(self, pipeline: Pipeline, declared: Mapping[str, Sequence[str]] | None):
        self.p = pipeline
        self.analysis = pipeline._analyse()
        self.order = list(self.analysis.order)
        self.declared: dict[str, tuple[str, ...]] = {}
        try:
            for key, cols in (declared or {}).items():
                if isinstance(cols, str):
                    cols = [cols]
                self.declared[str(key).lower()] = tuple(str(c) for c in cols)
        except Exception:
            self.declared = {}
        self._ctxs: dict[int, _Ctx] = {}
        self._grains: dict[Any, Grain] = {}
        self._scopes: dict[Any, RowScope] = {}
        self._attrs: dict[tuple[str, str], AttributeMeaning] = {}
        self._traces: dict[ColumnRef, Any] = {}
        self._active: set[Any] = set()

    # ------------------------------------------------------------- profiles

    def profile(self, key: str, label: str | None = None) -> TableProfile:
        try:
            grain = self._model_grain(key)
        except Exception as exc:
            grain = Grain(reason=f"profile_error: {type(exc).__name__}")
        try:
            scope = self._model_scope(key)
        except Exception as exc:
            scope = RowScope(comparable=False, reason=f"profile_error: {type(exc).__name__}")
        attributes: list[AttributeMeaning] = []
        try:
            for column in self.analysis.outputs.get(key, ()):
                attributes.append(self._attr(key, column))
        except Exception as exc:
            attributes.append(AttributeMeaning("*", reason=f"profile_error: {type(exc).__name__}"))
        return TableProfile(label or key, grain, tuple(attributes), scope)

    # ------------------------------------------------------------- contexts

    def _extend_env(self, query: exp.Expression, env: dict) -> dict:
        ctes = list(getattr(query, "ctes", None) or [])
        if not ctes:
            return env
        extended = dict(env)
        for cte in ctes:
            extended[cte.alias_or_name.lower()] = (cte.this, extended)
        return extended

    def _make_src(self, node: exp.Expression, env: dict) -> _Src:
        alias = (node.alias_or_name or "").lower() if hasattr(node, "alias_or_name") else ""
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            name = node.name.lower()
            if not node.db and not node.catalog and name in env:
                query, defined_in = env[name]
                return _Src(alias, "query", name, query=query, env=defined_in)
            key = self.p.resolve(node) or _table_name_for_schema(node)
            kind = "model" if key in self.p.models else "table"
            return _Src(alias, kind, key, key=key)
        if isinstance(node, exp.Subquery):
            return _Src(alias, "query", alias or "subquery", query=node.this, env=env)
        if isinstance(node, exp.Unnest):
            table_alias = node.args.get("alias")
            columns = [c.name.lower() for c in (table_alias.columns if table_alias is not None else [])]
            offset = node.args.get("offset")
            if columns and offset is not None and offset is not False:
                columns.append(offset.name.lower() if hasattr(offset, "name") and offset.name else "offset")
            # Behind an alias the elements of an array of STRUCTs are reached only through the alias.
            return _Src(alias, "opaque", "unnest", names=tuple(columns) if columns else None)
        return _Src(alias, "opaque", alias or type(node).__name__.lower())

    def _ctx(self, select: exp.Select, env: dict) -> _Ctx:
        cached = self._ctxs.get(id(select))
        if cached is not None:
            return cached
        env = self._extend_env(select, env)
        srcs: list[_Src] = []
        joins: list[exp.Join | None] = []
        source = select.args.get("from_") or select.args.get("from")
        if source is not None and source.this is not None:
            srcs.append(self._make_src(source.this, env))
            joins.append(None)
        for join in select.args.get("joins") or []:
            srcs.append(self._make_src(join.this, env))
            joins.append(join)
        projs: list[tuple[str, exp.Expression]] = []
        names: list[str] = []
        for item in select.expressions:
            inner = item.this if isinstance(item, exp.Alias) else item
            if _is_star(inner):
                projs.append(("", inner))
                names.append("*")
            else:
                name = item.alias_or_name or ""
                projs.append((name.lower(), inner))
                names.append(name)
        ctx = _Ctx(select, env, srcs, joins, projs, tuple(names))
        self._ctxs[id(select)] = ctx
        return ctx

    def _output_names(self, query: exp.Expression, env: dict) -> tuple[str, ...]:
        query = _unwrap(query)
        if isinstance(query, exp.SetOperation):
            return self._output_names(query.this, self._extend_env(query, env))
        if isinstance(query, exp.Select):
            return self._ctx(query, env).names
        return ("*",)

    @staticmethod
    def _branches(query: exp.Expression) -> list[exp.Expression]:
        query = _unwrap(query)
        if isinstance(query, exp.SetOperation):
            return _Profiler._branches(query.this) + _Profiler._branches(query.expression)
        return [query]

    # -------------------------------------------------------------- meaning

    def _ref_meaning(self, ref: ColumnRef) -> AttributeMeaning:
        if ref.table in self.p.models:
            attr = self._attr(ref.table, ref.column)
            if not ref.path or attr.status != "known" or attr.meaning is None:
                return attr
            try:
                return AttributeMeaning(ref.column, self._field(attr.meaning, list(ref.path)), "known")
            except _Unresolved as exc:
                return AttributeMeaning(ref.column, reason=exc.reason)
        base = f"col:{ref.table}.{ref.column.lower()}"
        try:
            return AttributeMeaning(ref.column, self._field(base, list(ref.path)), "known")
        except _Unresolved as exc:
            return AttributeMeaning(ref.column, reason=exc.reason)

    def _trace(self, ref: ColumnRef):
        if ref not in self._traces:
            self._traces[ref] = self.p.trace_column(ref)
        return self._traces[ref]

    def _attr(self, key: str, column: str) -> AttributeMeaning:
        cache_key = (key, column.lower())
        cached = self._attrs.get(cache_key)
        if cached is not None:
            return cached
        guard = ("attr",) + cache_key
        if guard in self._active:
            return AttributeMeaning(column, reason="cycle")
        self._active.add(guard)
        try:
            try:
                result = self._compute_attr(key, column)
            except _Unresolved as exc:
                result = AttributeMeaning(column, reason=exc.reason)
            except RecursionError:
                result = AttributeMeaning(column, reason="too_deep")
            except Exception as exc:
                result = AttributeMeaning(column, reason=f"profile_error: {type(exc).__name__}")
        finally:
            self._active.discard(guard)
        self._attrs[cache_key] = result
        return result

    def _compute_attr(self, key: str, column: str) -> AttributeMeaning:
        if column == "*":
            return AttributeMeaning(column, reason="unexpanded_star")
        ref = ColumnRef(key, column)
        record = self.analysis.records.get(ref)
        if record is None:
            reason = self.analysis.untraced_reason(ref, self.p.models) or "unknown_column"
            return AttributeMeaning(column, reason=reason)
        if record.status == "unknown":
            return AttributeMeaning(column, reason=record.reason or "unknown", transform=record.transform)
        trace = self._trace(ref)
        if not trace.complete:
            reason = sorted({r for _, r in trace.unknown})[0]
            return AttributeMeaning(column, reason=reason, transform=record.transform)
        sources = tuple(sorted(str(s) for s in trace.sources))
        meaning: str | None = None
        problem: str | None = None
        if record.transform in ("passthrough", "renamed") and len(record.sources) == 1:
            upstream = self._ref_meaning(next(iter(record.sources)))
            if upstream.status == "known":
                meaning = upstream.meaning
            else:
                problem = upstream.reason
        if meaning is None and problem is None:
            try:
                meaning = self._model_column_meaning(key, column)
            except _Unresolved as exc:
                problem = exc.reason
        if meaning is None:
            return AttributeMeaning(column, reason=problem or "unresolved", sources=sources, transform=record.transform)
        equality_only = meaning.startswith(("expr:", "window:", "union(", "field("))
        return AttributeMeaning(column, meaning, "known", None, sources, record.transform, equality_only)

    def _model_column_meaning(self, key: str, column: str) -> str:
        query = self.analysis.parsed.get(key)
        if query is None:
            raise _Unresolved("unparsed")
        return self._query_out(query, {}, column.lower())

    def _query_out(self, query: exp.Expression, env: dict, name: str) -> str:
        query = _unwrap(query)
        if isinstance(query, exp.Select):
            return self._select_out(self._ctx(query, env), name)
        if isinstance(query, exp.SetOperation):
            if is_by_name(query):
                raise _Unresolved("by_name_set_operation")  # columns pair by name here, not position
            env = self._extend_env(query, env)
            branches = self._branches(query)
            first = self._output_names(branches[0], env)
            if "*" in first:
                raise _Unresolved("unexpanded_star")
            lowered = [n.lower() for n in first]
            if name not in lowered:
                raise _Unresolved("unknown_column")
            position = lowered.index(name)
            found: set[str] = set()
            for branch in branches:
                names = self._output_names(branch, env)
                if "*" in names or position >= len(names):
                    raise _Unresolved("unexpanded_star")
                found.add(self._query_out(branch, env, names[position].lower()))
            if len(found) == 1:
                return next(iter(found))
            return "union(" + "|".join(sorted(found)) + ")"
        raise _Unresolved("unsupported_query")

    def _select_out(self, ctx: _Ctx, name: str) -> str:
        for outname, expr in ctx.projs:
            if outname == name and not _is_star(expr):
                return self._render_meaning(expr, ctx)
        stars = [expr for _, expr in ctx.projs if _is_star(expr)]
        for star in stars:
            qualifier = star.table.lower() if isinstance(star, exp.Column) else ""
            # ``t.*`` keeps its modifiers on the Star under the column, as ``*`` does on itself.
            if star_modifier(star, "rename") or star_modifier(star, "ilike"):
                raise _Unresolved("unexpanded_star")
            if name in {c.name.lower() for c in star_modifier(star, "except") or []}:
                continue
            replaced = {a.alias.lower(): a.this for a in star_modifier(star, "replace") or [] if isinstance(a, exp.Alias)}
            if name in replaced:
                return self._render_meaning(replaced[name], ctx)
            if qualifier:
                src = ctx.by_alias.get(qualifier)
                if src is None:
                    raise _Unresolved("unknown_qualifier")
                return self._src_col(src, name)
            if len(ctx.srcs) == 1:
                return self._src_col(ctx.srcs[0], name)
            return self._locate(ctx, name)
        raise _Unresolved("unknown_column")

    def _src_has(self, src: _Src, name: str) -> bool | None:
        try:
            if src.kind == "model":
                outs = self.analysis.outputs.get(src.key)
                if outs and "*" not in outs:
                    return name in {o.lower() for o in outs}
            elif src.kind == "table":
                schema = self.p.source_schema.get(src.key)
                if schema:
                    return name in {c.lower() for c in schema}
            elif src.kind == "query":
                names = self._output_names(src.query, src.env or {})
                if "*" not in names:
                    return name in {n.lower() for n in names}
            elif src.names is not None:
                return name in src.names
        except Exception:
            return None
        return None

    def _locate(self, ctx: _Ctx, name: str) -> str:
        """The meaning of an unqualified column in a query with several sources."""

        verdicts = [(src, self._src_has(src, name)) for src in ctx.srcs]
        having = [src for src, has in verdicts if has]
        unsure = [src for src, has in verdicts if has is None]
        if len(having) == 1 and not unsure:
            return self._src_col(having[0], name)
        if not having and len(unsure) == 1:  # every other source lacks it, and a valid query reads it from somewhere
            return self._src_col(unsure[0], name)
        raise _Unresolved("ambiguous_column")

    def _src_col(self, src: _Src, name: str) -> str:
        if src.kind == "model":
            attr = self._attr(src.key, name)
            if attr.status != "known" or attr.meaning is None:
                raise _Unresolved(attr.reason or "unknown")
            return attr.meaning
        if src.kind == "table":
            return f"col:{src.key}.{name}"
        if src.kind == "query":
            guard = ("src", id(src.query), name)
            if guard in self._active:
                raise _Unresolved("recursive_cte")
            self._active.add(guard)
            try:
                return self._query_out(src.query, src.env or {}, name)
            finally:
                self._active.discard(guard)
        if src.names and name in src.names:
            raise _Unresolved("unnest_element")  # one value per array element: not a column of any table
        raise _Unresolved("unsupported_source")

    def _col_meaning(self, col: exp.Column, ctx: _Ctx, allow_alias: bool, depth: int) -> str:
        if depth > _MAX_DEPTH:
            raise _Unresolved("too_deep")
        if isinstance(col.this, exp.Star):
            raise _Unresolved("star_expression")
        parts = list(col.meta.get("parts") or [part.name for part in col.parts])  # as written: a JSON field name is case-sensitive
        if len(parts) > 1:
            return self._qualified_meaning(parts, ctx, allow_alias, depth)
        name = parts[0].lower()
        if allow_alias:
            for outname, expr in ctx.projs:
                plain = isinstance(expr, exp.Column) and expr.name.lower() == name and not expr.table
                if outname == name and not _is_star(expr) and not plain:
                    return self._render_meaning(expr, ctx, depth=depth + 1)
        if len(ctx.srcs) == 1:
            return self._src_col(ctx.srcs[0], name)
        if not ctx.srcs:
            raise _Unresolved("unknown_column")
        return self._locate(ctx, name)

    def _qualified_meaning(self, parts: list[str], ctx: _Ctx, allow_alias: bool, depth: int) -> str:
        """``alias.column``, ``alias.column.field...``, ``column.field...`` and ``dataset.table.column...``.

        The first part names a table alias when one matches (BigQuery gives the alias precedence), a table by its
        qualified name when one does, and otherwise a column of the only (or the one known) source, so what follows
        it is a path of field names inside that column.
        """

        names = [part.lower() for part in parts]
        first = names[0]
        src = ctx.by_alias.get(first)
        if src is not None:
            return self._field(self._src_col(src, names[1]), parts[2:])
        qualified = self._by_qualified_name(names, ctx)
        if qualified is not None:
            src, used = qualified
            if self._src_has(src, first):
                raise _Unresolved("ambiguous_column")  # a column of that name would read the same way
            return self._field(self._src_col(src, names[used]), parts[used + 1 :])
        if not ctx.srcs:
            raise _Unresolved("unknown_qualifier")
        if any(src.names and first in src.names for src in ctx.srcs):
            raise _Unresolved("unnest_element")
        base = self._col_meaning(exp.column(first), ctx, allow_alias, depth + 1)  # a STRUCT or JSON column
        return self._field(base, parts[1:])

    @staticmethod
    def _by_qualified_name(parts: list[str], ctx: _Ctx) -> tuple[_Src, int] | None:
        """The source a ``dataset.table.column`` or ``project.dataset.table.column`` reference names, and how many parts name it."""

        if len(parts) < 3:
            return None
        found = []
        for src in ctx.srcs:
            if src.kind not in ("model", "table") or not src.alias:
                continue
            table = src.key.lower().split(".")
            for used in (2, 3):
                if len(parts) > used and table[-used:] == parts[:used] and src.alias == table[-1]:
                    found.append((src, used))
        return found[0] if len(found) == 1 else None

    def _field(self, base: str, path: Sequence[str]) -> str:
        """``field(<base column>, a.b.c)``: the value at a path of field names inside one source column.

        Two such meanings are equal only when both read the same path of the same source column, so a different
        path, or the same path in another column or table, never compares equal. The base must itself be a source
        column (or a field of one); a field of a computed value stays unknown. Field names keep the case they were
        written in, because a JSON field name is case-sensitive; they are folded to lower case only when the source
        schema says the column is a STRUCT, whose field names are not.
        """

        if not path:
            return base
        if not all(_FIELD_NAME.fullmatch(name) for name in path):
            raise _Unresolved("nested_field")
        found = _FIELD_MEANING.fullmatch(base)
        column, head = (found.group(1), [found.group(2)]) if found is not None else (base, [])
        if not column.startswith("col:"):
            raise _Unresolved("nested_field")
        names = [*head, *path]
        if self._is_struct(column):
            names = [name.lower() for name in names]
        return f"field({column}, {'.'.join(names)})"

    def _is_struct(self, column: str) -> bool:
        """Whether the source schema types ``col:<table>.<column>`` as a STRUCT (RECORD)."""

        for table, columns in self.p.source_schema.items():
            prefix = f"col:{table}."
            if column.startswith(prefix):
                kind = {name.lower(): str(typ) for name, typ in columns.items()}.get(column[len(prefix) :].lower(), "")
                return kind.upper().startswith(("STRUCT", "RECORD"))
        return False

    def _render(self, expr: exp.Expression, ctx: _Ctx, allow_alias: bool = False, depth: int = 0) -> tuple[str, str]:
        """(kind, text) with columns replaced by their meanings; raises when unknown."""

        expr = _join_field_paths(expr.copy())
        while isinstance(expr, exp.Paren):
            expr = expr.this
        for node in expr.walk():
            if isinstance(node, exp.Query):
                raise _Unresolved("subquery_expression")
            if isinstance(node, _NONDETERMINISTIC) or (
                isinstance(node, exp.Anonymous) and str(node.this).upper() in _NONDETERMINISTIC_NAMES
            ):
                raise _Unresolved("non_deterministic")
        replaced = 0
        columns = list(expr.find_all(exp.Column))
        if isinstance(expr, exp.Column):
            return "col", self._col_meaning(expr, ctx, allow_alias, depth)
        for column in columns:
            column.replace(_meaning_node(self._col_meaning(column, ctx, allow_alias, depth)))
            replaced += 1

        wrapped = 0

        def wrap(node: exp.Expression) -> exp.Expression:
            nonlocal wrapped
            if isinstance(node, exp.Window):
                wrapped += 1
                return exp.Var(this="window:" + _canon_sql(node))
            if is_aggregate(node):
                wrapped += 1
                collapsed = _collapse_nested(node)
                if collapsed is not None:
                    return exp.Var(this=collapsed)
                if isinstance(node.this, exp.Paren) and isinstance(node.this.this, exp.Var):
                    node = node.copy()
                    node.set("this", node.this.this)
                return exp.Var(this="agg:" + _canon_sql(node))
            return node

        expr = expr.transform(wrap)
        if isinstance(expr, exp.Var) and wrapped:
            return ("window" if expr.name.startswith("window:") else "agg"), expr.name
        text = _canon_sql(expr)
        return ("expr" if replaced or wrapped else "const"), text

    def _render_meaning(self, expr: exp.Expression, ctx: _Ctx, depth: int = 0) -> str:
        kind, text = self._render(expr, ctx, depth=depth)
        if kind in ("col", "agg", "window"):
            return text
        return f"{kind}:{text}"

    def _filter_text(self, conjunct: exp.Expression, ctx: _Ctx) -> str:
        kind, text = self._render(conjunct, ctx, allow_alias=True)
        return text if kind in ("col", "agg", "window", "expr") else f"const:{text}"

    # ---------------------------------------------------------------- grain

    def _model_grain(self, key: str) -> Grain:
        declared = self.declared.get(key.lower())
        if declared is not None:
            return Grain(tuple(sorted(declared, key=str.lower)), "declared")
        cache_key = ("model", key)
        if cache_key in self._grains:
            return self._grains[cache_key]
        guard = ("grain",) + (key,)
        if guard in self._active:
            return Grain(reason="cycle")
        self._active.add(guard)
        try:
            model = self.p.models.get(key)
            if model is None or not model.is_query:
                result = Grain(reason="not_a_query")
            else:
                query = self.analysis.parsed.get(key)
                result = Grain(reason="unparsed") if query is None else self._query_grain(query, {})
        except RecursionError:
            result = Grain(reason="too_deep")
        except Exception as exc:
            result = Grain(reason=f"profile_error: {type(exc).__name__}")
        finally:
            self._active.discard(guard)
        if not result.known:
            result = self._asserted_grain(key) or result
        self._grains[cache_key] = result
        return result

    def _asserted_grain(self, key: str) -> Grain | None:
        """The project's own ``assertions: {uniqueKey}`` of a model, used only where the query does not show its grain.

        The columns must be output columns of the model. A grain derived from the query wins over it, so declaring a
        key never changes a grain that was already known; with several keys the shortest one stands.
        """

        model = self.p.models.get(key)
        outputs = {name.lower(): name for name in self.analysis.outputs.get(key, ()) if name != "*"}
        if model is None or not outputs:
            return None
        usable = [k for k in model.unique_keys if k and all(c.lower() in outputs for c in k)]
        if not usable:
            return None
        best = min(usable, key=lambda k: (len(set(c.lower() for c in k)), sorted(c.lower() for c in k)))
        return Grain(_sorted_keys([outputs[c.lower()] for c in dict.fromkeys(best)]), "declared")

    def _src_grain(self, src: _Src) -> Grain:
        if src.kind == "model":
            return self._model_grain(src.key)
        if src.kind == "table":
            declared = self.declared.get(src.key.lower())
            if declared is not None:
                return Grain(tuple(sorted(declared, key=str.lower)), "declared")
            return Grain(reason="wildcard_table" if "*" in src.key else "source_without_grain")
        if src.kind == "query":
            return self._query_grain(src.query, src.env or {})
        return Grain(reason="unsupported_source")

    def _query_grain(self, query: exp.Expression, env: dict) -> Grain:
        query = _unwrap(query)
        cache_key = ("query", id(query))
        if cache_key in self._grains:
            return self._grains[cache_key]
        if cache_key in self._active:
            return Grain(reason="recursive_cte")
        self._active.add(cache_key)
        try:
            env = self._extend_env(query, env)
            if isinstance(query, exp.Select):
                result = self._select_grain(self._ctx(query, env))
            elif isinstance(query, exp.SetOperation):
                names = self._output_names(query, env)
                if is_by_name(query):
                    result = Grain(reason="by_name_set_operation")
                elif not query.args.get("distinct"):
                    result = Grain(reason="union_mixed_grain")
                elif "*" in names:
                    result = Grain(reason="unexpanded_star")
                else:
                    result = Grain(_sorted_keys(names), "derived")
            else:
                result = Grain(reason="unsupported_query")
        finally:
            self._active.discard(cache_key)
        self._grains[cache_key] = result
        return result

    def _select_grain(self, ctx: _Ctx) -> Grain:
        select = ctx.select
        if any(set_returning_item(e) for _, e in ctx.projs):
            return Grain(reason="set_returning_select")
        # DISTINCT ON keeps one row per ON value; it does not make whole output rows unique.
        distinct = select.args.get("distinct") is not None and not select.args["distinct"].args.get("on")
        group = select.args.get("group")
        if group is not None:
            if extended_grouping(group):
                return Grain(reason="grouping_sets")
            if group.args.get("all"):
                exprs = [e for _, e in ctx.projs if not _has_aggregate(e) and not _is_star(e)]
            else:
                exprs = list(group.expressions)
            keys = self._map_out(exprs, ctx, group_by=True)
            if keys is not None:
                return Grain(_sorted_keys(keys), "derived")
            if distinct and "*" not in ctx.names:
                return Grain(_sorted_keys(ctx.names), "derived")
            return Grain(reason="grain_not_in_output")
        if any(_has_aggregate(e) for _, e in ctx.projs):
            return Grain((), "derived")
        partition = self._qualify_partition(ctx)
        if partition is not None:
            keys = self._map_out(partition, ctx)
            if keys is not None:
                return Grain(_sorted_keys(keys), "derived")
        if distinct:
            if "*" in ctx.names:
                return Grain(reason="unexpanded_star")
            return Grain(_sorted_keys(ctx.names), "derived")
        return self._passthrough_grain(ctx)

    def _passthrough_grain(self, ctx: _Ctx) -> Grain:
        if not ctx.srcs:
            return Grain((), "derived")
        base = ctx.srcs[0]
        base_grain = self._effective_grain(base, ctx)
        if not base_grain.known:
            return Grain(reason=base_grain.reason or "input_grain_unknown")
        for index in range(1, len(ctx.srcs)):
            problem = self._fan_out(ctx, index)
            if problem:
                return Grain(reason=problem)
        keys = self._carry_keys(base, base_grain.keys, ctx)
        if keys is None:
            return Grain(reason="grain_not_in_output")
        return Grain(_sorted_keys(keys), "derived")

    def _effective_grain(self, src: _Src, ctx: _Ctx) -> Grain:
        """A source's grain, tightened when this select keeps only its ROW_NUMBER() = 1 rows."""

        if src.kind == "query":
            inner = _unwrap(src.query)
            if isinstance(inner, exp.Select):
                inner_ctx = self._ctx(inner, src.env or {})
                for conjunct in _split_and(ctx.select.args.get("where")):
                    column = _row_one_column(conjunct)
                    if column is None or (column.table and column.table.lower() != src.alias):
                        continue
                    if not column.table and len(ctx.srcs) > 1 and self._src_has(src, column.name.lower()) is not True:
                        continue
                    for outname, expr in inner_ctx.projs:
                        if outname == column.name.lower() and _is_row_number(expr):
                            keys = self._map_out(list(expr.args.get("partition_by") or []), inner_ctx)
                            if keys is not None:
                                return Grain(_sorted_keys(keys), "derived")
        return self._src_grain(src)

    def _qualify_partition(self, ctx: _Ctx) -> list[exp.Expression] | None:
        for conjunct in _split_and(ctx.select.args.get("qualify")):
            target = _row_one_target(conjunct)
            if target is None:
                continue
            if isinstance(target, exp.Column) and not target.table:
                for outname, expr in ctx.projs:
                    if outname == target.name.lower() and _is_row_number(expr):
                        return list(expr.args.get("partition_by") or [])
            if _is_row_number(target):
                return list(target.args.get("partition_by") or [])
        return None

    def _expr_key(self, expr: exp.Expression, ctx: _Ctx) -> str:
        expr = expr.copy()
        single = ctx.srcs[0].alias if len(ctx.srcs) == 1 else None
        if isinstance(expr, exp.Column):
            return f"{(expr.table.lower() or single or '?')}.{expr.name.lower()}"
        for column in list(expr.find_all(exp.Column)):
            qualifier = column.table.lower() or single or "?"
            column.replace(exp.Var(this=f"{qualifier}.{column.name.lower()}"))
        return _canon_sql(expr)

    def _map_out(self, exprs: Sequence[exp.Expression], ctx: _Ctx, group_by: bool = False) -> list[str] | None:
        """Output column names for each expression, or None when one is not selected."""

        out: list[str] = []
        for expr in exprs:
            while isinstance(expr, exp.Paren):
                expr = expr.this
            found: str | None = None
            if group_by and isinstance(expr, exp.Literal) and not expr.is_string:
                try:
                    position = int(expr.name) - 1
                except ValueError:
                    return None
                if 0 <= position < len(ctx.projs) and not _is_star(ctx.projs[position][1]):
                    found = ctx.names[position] or None
                if found is None:
                    return None
                out.append(found)
                continue
            if group_by and isinstance(expr, exp.Column) and not expr.table:
                for index, (outname, proj) in enumerate(ctx.projs):
                    plain = isinstance(proj, exp.Column) and proj.name.lower() == outname
                    if outname == expr.name.lower() and not plain and not _is_star(proj):
                        found = ctx.names[index]
                        break
            if found is None:
                key = self._expr_key(expr, ctx)
                for index, (outname, proj) in enumerate(ctx.projs):
                    if not _is_star(proj) and outname and self._expr_key(proj, ctx) == key:
                        found = ctx.names[index]
                        break
            if found is None and isinstance(expr, exp.Column):
                found = self._star_covers(expr, ctx)
            if found is None:
                return None
            out.append(found)
        return out

    def _star_covers(self, column: exp.Column, ctx: _Ctx) -> str | None:
        name = column.name.lower()
        for _, proj in ctx.projs:
            if not _is_star(proj):
                continue
            qualifier = proj.table.lower() if isinstance(proj, exp.Column) else ""
            if qualifier:
                if column.table.lower() != qualifier:
                    continue
            elif len(ctx.srcs) != 1 and column.table.lower() not in ctx.by_alias:
                continue
            if star_modifier(proj, "rename") or star_modifier(proj, "ilike"):
                continue
            if name in {c.name.lower() for c in star_modifier(proj, "except") or []}:
                continue
            if name in {a.alias.lower() for a in star_modifier(proj, "replace") or [] if isinstance(a, exp.Alias)}:
                continue
            return column.name
        return None

    def _carry_keys(self, src: _Src, keys: Sequence[str], ctx: _Ctx) -> list[str] | None:
        out: list[str] = []
        for key in keys:
            column = exp.column(key, table=src.alias or None)
            found: str | None = None
            for index, (outname, proj) in enumerate(ctx.projs):
                if isinstance(proj, exp.Column) and not _is_star(proj) and proj.name.lower() == key.lower():
                    qualifier = proj.table.lower()
                    if qualifier == src.alias or (not qualifier and len(ctx.srcs) == 1):
                        found = ctx.names[index]
                        break
            if found is None:
                found = self._star_covers(column, ctx)
            if found is None:
                return None
            out.append(found)
        return out

    def _fan_out(self, ctx: _Ctx, index: int) -> str | None:
        src, join = ctx.srcs[index], ctx.joins[index]
        label = src.label or src.alias
        if join is None or src.kind == "opaque":
            return f"fan_out_join: {label} is not a plain join"
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        on, using = join.args.get("on"), join.args.get("using")
        if side in ("RIGHT", "FULL") or kind == "CROSS" or join.args.get("method") or not (on or using):
            return f"fan_out_join: {label} join can multiply rows"
        grain = self._effective_grain(src, ctx)
        if not grain.known:
            return f"fan_out_join: {label} has no known grain"
        pinned: set[str] = {i.name.lower() for i in using or [] if hasattr(i, "name")}
        for conjunct in _split_and(on):
            if not isinstance(conjunct, exp.EQ):
                continue
            for mine, other in ((conjunct.this, conjunct.expression), (conjunct.expression, conjunct.this)):
                if not (isinstance(mine, exp.Column) and mine.table.lower() == src.alias and src.alias):
                    continue
                others = list(other.find_all(exp.Column))
                if all(c.table and c.table.lower() != src.alias for c in others):
                    pinned.add(mine.name.lower())
        if not {k.lower() for k in grain.keys} <= pinned:
            return f"fan_out_join: {label} is not joined on its grain"
        return None

    # ---------------------------------------------------------------- scope

    def _model_scope(self, key: str) -> RowScope:
        cache_key = ("model", key)
        if cache_key in self._scopes:
            return self._scopes[cache_key]
        guard = ("scope", key)
        if guard in self._active:
            return RowScope(comparable=False, reason="cycle")
        self._active.add(guard)
        try:
            model = self.p.models.get(key)
            query = self.analysis.parsed.get(key)
            if model is None or not model.is_query:
                result = RowScope(comparable=False, reason="not_a_query")
            elif query is None:
                result = RowScope(comparable=False, reason="unparsed")
            else:
                result = self._query_scope(query, {})
                if model.masked_expressions and result.comparable:
                    result = RowScope(result.filters, False, "masked_expression")
        except RecursionError:
            result = RowScope(comparable=False, reason="too_deep")
        except Exception as exc:
            result = RowScope(comparable=False, reason=f"profile_error: {type(exc).__name__}")
        finally:
            self._active.discard(guard)
        self._scopes[cache_key] = result
        return result

    def _src_scope(self, src: _Src) -> RowScope:
        if src.kind == "model":
            return self._model_scope(src.key)
        if src.kind == "query":
            return self._query_scope(src.query, src.env or {})
        if src.kind == "opaque":
            return RowScope(comparable=False, reason="unsupported_source")
        return RowScope()

    def _query_scope(self, query: exp.Expression, env: dict) -> RowScope:
        query = _unwrap(query)
        cache_key = ("query", id(query))
        if cache_key in self._scopes:
            return self._scopes[cache_key]
        if cache_key in self._active:
            return RowScope(comparable=False, reason="recursive_cte")
        self._active.add(cache_key)
        try:
            env = self._extend_env(query, env)
            if isinstance(query, exp.Select):
                result = self._select_scope(self._ctx(query, env))
            elif isinstance(query, exp.SetOperation):
                result = self._set_scope(query, env)
            else:
                result = RowScope(comparable=False, reason="unsupported_query")
        finally:
            self._active.discard(cache_key)
        self._scopes[cache_key] = result
        return result

    def _set_scope(self, query: exp.Expression, env: dict) -> RowScope:
        scopes = [self._query_scope(b, env) for b in self._branches(query)]
        comparable = all(s.comparable for s in scopes)
        reason = next((s.reason for s in scopes if not s.comparable), None)
        texts = {" AND ".join(s.filters) for s in scopes}
        if len(texts) == 1:
            filters = scopes[0].filters
        else:
            filters = ("any_of(" + " | ".join(sorted(f"[{t}]" for t in texts)) + ")",)
        return RowScope(filters, comparable, reason)

    def _select_scope(self, ctx: _Ctx) -> RowScope:
        select = ctx.select
        filters: set[str] = set()
        problems: list[str] = []

        def take(node: exp.Expression | None) -> None:
            for conjunct in _split_and(node):
                try:
                    filters.add(self._filter_text(conjunct, ctx))
                except _Unresolved as exc:
                    filters.add("opaque:" + conjunct.sql(dialect="bigquery"))
                    problems.append("opaque_filter" if exc.reason != "non_deterministic" else "non_deterministic_filter")

        take(select.args.get("where"))
        take(select.args.get("having"))
        take(select.args.get("qualify"))
        for index, src in enumerate(ctx.srcs):
            join = ctx.joins[index]
            side = (join.args.get("side") or "").upper() if join is not None else ""
            if side != "LEFT":
                inherited = self._src_scope(src)
                filters.update(inherited.filters)
                if not inherited.comparable:
                    problems.append(inherited.reason or "input_not_comparable")
            if join is not None and side != "LEFT":
                self._join_filter(ctx, index, join, filters, problems)
        if select.find(exp.Limit, exp.Offset, exp.Fetch):
            problems.append("limit")
        if select.find(exp.TableSample):
            problems.append("sampling")
        return RowScope(tuple(sorted(filters)), not problems, problems[0] if problems else None)

    def _join_filter(self, ctx: _Ctx, index: int, join: exp.Join, filters: set[str], problems: list[str]) -> None:
        on, using = join.args.get("on"), join.args.get("using")
        try:
            if on is not None:
                filters.add("join:" + self._filter_text(on, ctx))
            elif using:
                prior = ctx.srcs[:index]
                if len(prior) != 1:
                    raise _Unresolved("ambiguous_column")
                parts = []
                for ident in using:
                    name = ident.name.lower()
                    parts.append(
                        _canon_sql(
                            exp.EQ(
                                this=exp.Var(this=self._src_col(prior[0], name)),
                                expression=exp.Var(this=self._src_col(ctx.srcs[index], name)),
                            )
                        )
                    )
                filters.add("join:" + " AND ".join(sorted(parts)))
        except _Unresolved:
            filters.add("opaque:" + join.sql(dialect="bigquery"))
            problems.append("opaque_filter")


def _sorted_keys(names: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(names), key=str.lower))


def _is_row_number(expr: exp.Expression) -> bool:
    return isinstance(expr, exp.Window) and isinstance(expr.this, exp.RowNumber)


def _row_one_target(conjunct: exp.Expression) -> exp.Expression | None:
    """The expression compared to 1 in ``x = 1``, ``x <= 1`` or ``x < 2``."""

    if isinstance(conjunct, (exp.EQ, exp.LTE, exp.LT)) and isinstance(conjunct.expression, exp.Literal):
        limit = conjunct.expression
        if not limit.is_string and limit.name == ("2" if isinstance(conjunct, exp.LT) else "1"):
            return conjunct.this
    return None


def _row_one_column(conjunct: exp.Expression) -> exp.Column | None:
    target = _row_one_target(conjunct)
    return target if isinstance(target, exp.Column) else None
