"""Re-aggregated ratios: an ``AVG`` or ``SUM`` of a column that is itself a division.

A ratio such as ``budget_utilization_ratio = SAFE_DIVIDE(actual, budget)`` is right at the grain it was computed at.
Rolled up to a coarser grain, the right number is the ratio of the sums, ``SAFE_DIVIDE(SUM(actual), SUM(budget))``.
An average (or a sum) of the per-row ratios is a different number, and nothing fails: the query runs and the total is
wrong. This module finds that pattern, for ``check_query`` (a draft) and ``project_checks`` (every model):

* **What counts as a ratio.** A division, ``/``, ``SAFE_DIVIDE`` or ``IEEE_DIVIDE``, by something that is not a
  constant, possibly scaled by a constant (``* 100``, ``/ 100``) or wrapped in ``ROUND``, ``TRUNC``, ``CAST``,
  ``SAFE_CAST``, ``COALESCE(..., constant)`` or ``IF(condition, ..., NULL)``. An ``AVG`` is a ratio too
  (``SUM / COUNT``), so an ``AVG`` of an upstream ``AVG`` is found. The division may be written in the aggregate itself,
  in a CTE or subquery, or in any model upstream: the argument is followed through CTEs, subqueries and models
  (passthrough steps by the project's column lineage, the step that computes the value in its parsed query).
* **What is not a finding.** ``MIN``, ``MAX``, ``ANY_VALUE``, ``COUNT`` and every other aggregate; a weighted average
  written right, ``SUM(ratio * weight) / SUM(weight)`` (the argument of that ``SUM`` is a product, not a ratio); a
  division by a constant, a scalar subquery or a window value (one value shared by many rows); a constant numerator
  (``1 / x``); and window aggregates (a moving average of a daily ratio is often meant). Nothing is read from column
  names: a ratio stored in a source table is not recognised.
* **Same grain.** When the aggregating ``SELECT`` reads one table whose grain is known (the table profiles, or a
  stated grain for a source) and groups by every grain column, each group holds one row and the ``AVG`` *is* the ratio.
  That is only a note (``check_query``'s ``notes``), never a finding, and ``project_checks`` leaves it out.
* **Evidence.** A finding is a caution, not an error. It names the ratio column, where it is computed (model and
  expression), and the rewrite ``SUM(numerator) / SUM(denominator)``. The numerator and denominator are found from
  lineage: the canonical meaning of the ratio column (:class:`~kumosql.table_profile.AttributeMeaning`, such as
  ``expr:SAFE_DIVIDE(agg:SUM(col:...), agg:SUM(col:...))``) is split at the division, and each part is matched with
  the meaning of a column of the table the aggregate reads. A part no column carries is ``unknown``, and the fix says
  where the ratio divides it.

Everything is offline: the loaded project only, no warehouse, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .mcp_answers import Answer, clip
from .mcp_session import Session
from .table_profile import SOURCE_COLUMN

CHECK = "ratio"
PROJECT_CHECK = "ratio_reaggregated"
EXAMPLES = 3
MAX_DEPTH = 24
#: the aggregates that re-aggregate a ratio wrongly (an average or a sum of per-row ratios)
AGGREGATES = (exp.Avg, exp.Sum)

_OUTSIDE = "a table outside the project"
_DIVIDE_NAMES = {"IEEE_DIVIDE": "IEEE_DIVIDE"}


def short(key: str) -> str:
    return ".".join(key.split(".")[-2:])


# ------------------------------------------------------------------ what a ratio is


def _constant(node: exp.Expression | None) -> bool:
    """The same value on every row: an expression that reads no column and holds no aggregate (``100``, ``POWER(2, 40)``,
    a query parameter)."""

    if node is None:
        return False
    for item in node.walk():
        if isinstance(item, (exp.Column, exp.AggFunc, exp.Star, exp.Query, exp.Window)):
            return False
        if isinstance(item, exp.Rand):
            return False
    return True


def _shared(node: exp.Expression) -> bool:
    """One value for many rows: a constant, a scalar subquery or a window value. A division by it is a scaling."""

    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast, exp.Nullif)):
        node = node.this
    return _constant(node) or isinstance(node, (exp.Subquery, exp.Query, exp.Window))


def _peel(node: exp.Expression) -> tuple[exp.Expression, list[str]]:
    """``node`` without the wrappers that keep a ratio a ratio, and the constant factors taken off (``["* 100"]``)."""

    scale: list[str] = []
    for _ in range(64):
        if isinstance(node, (exp.Paren, exp.Alias, exp.Cast, exp.TryCast, exp.Round)) and node.this is not None:
            node = node.this
        elif type(node).__name__ == "Trunc" and node.this is not None:
            node = node.this
        elif isinstance(node, exp.Coalesce) and node.this is not None and all(_constant(e) for e in node.expressions):
            node = node.this
        elif isinstance(node, exp.If) and node.args.get("true") is not None and (node.args.get("false") is None or isinstance(node.args.get("false"), exp.Null)):
            node = node.args["true"]
        elif isinstance(node, exp.Mul) and _constant(node.expression) and not _constant(node.this):
            scale.insert(0, f"* {node.expression.sql(dialect='bigquery')}")
            node = node.this
        elif isinstance(node, exp.Mul) and _constant(node.this) and not _constant(node.expression):
            scale.insert(0, f"* {node.this.sql(dialect='bigquery')}")
            node = node.expression
        elif isinstance(node, exp.Div) and _constant(node.expression) and not _constant(node.this):
            scale.insert(0, f"/ {node.expression.sql(dialect='bigquery')}")
            node = node.this
        else:
            break
    return node, scale


def _division(node: exp.Expression) -> tuple[str, exp.Expression, exp.Expression] | None:
    """``(operator, numerator, denominator)`` when ``node`` divides one value by another that varies by row."""

    if isinstance(node, exp.SafeDivide):
        op, numerator, denominator = "SAFE_DIVIDE", node.this, node.expression
    elif isinstance(node, exp.Div):
        op, numerator, denominator = "/", node.this, node.expression
    elif isinstance(node, exp.Anonymous) and str(node.this).upper() in _DIVIDE_NAMES and len(node.expressions) == 2:
        op, (numerator, denominator) = _DIVIDE_NAMES[str(node.this).upper()], node.expressions
    else:
        return None
    if numerator is None or denominator is None or _shared(denominator) or _constant(_peel(numerator)[0]):
        return None
    if op == "/" and isinstance(_unparen(denominator), exp.Nullif):
        op = "SAFE_DIVIDE"  # x / NULLIF(y, 0) is the same guard
    return op, numerator, denominator


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren) and node.this is not None:
        node = node.this
    return node


def _part(node: exp.Expression) -> exp.Expression:
    """A numerator or denominator as it should be summed: without parentheses, casts and a ``NULLIF(x, 0)`` guard."""

    while True:
        if isinstance(node, (exp.Paren, exp.Cast, exp.TryCast)) and node.this is not None:
            node = node.this
        elif isinstance(node, exp.Nullif) and _constant(node.expression):
            node = node.this
        else:
            return node


def _has_aggregate(node: exp.Expression) -> bool:
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, (exp.Window, exp.Query)):
            continue
        if isinstance(item, exp.AggFunc):
            return True
        stack.extend(item.iter_expressions())
    return False


# ------------------------------------------------------------------ where it is computed


@dataclass(eq=False)
class Ratio:
    """A ratio found where it is computed."""

    #: ``ratio`` (a division) or ``average`` (an ``AVG``: a sum over a count)
    kind: str
    #: project key of the model that computes it, or ``""`` for the draft
    model: str
    #: ``""`` (the main query), ``CTE name`` or ``a subquery``
    scope: str
    #: the column it is output as there (``""`` when it is written inside the aggregate itself)
    column: str
    #: the expression as written there
    written: str
    #: ``/``, ``SAFE_DIVIDE`` or ``IEEE_DIVIDE``
    op: str
    numerator: str
    denominator: str
    #: the denominator is a value of each row, not an aggregate: the division may convert a unit
    per_row: bool
    #: the SELECT that computes it
    select: exp.Select | None = None
    #: the numerator or denominator is itself an aggregate there (``SUM(x) / SUM(y)``, or an ``AVG``)
    aggregated: bool = False
    #: the denominator as a node, when it is one column there (to read its meaning)
    denominator_column: exp.Column | None = None


@dataclass
class Trace:
    ratio: Ratio | None = None
    #: why the argument could not be followed to where it is computed ("" when it was)
    unknown: str = ""
    #: constant factors applied to the ratio on the way (``* 100``)
    scale: list[str] = field(default_factory=list)


def _sql(node: exp.Expression) -> str:
    return node.sql(dialect="bigquery")


def _scope(select: exp.Select) -> str:
    node: exp.Expression | None = select
    while node is not None:
        parent = node.parent
        if isinstance(parent, exp.CTE):
            return f"CTE {parent.alias}"
        if isinstance(parent, exp.Subquery):
            return "a subquery"
        node = parent
    return ""


def _unwrap(query: exp.Expression) -> exp.Expression:
    while isinstance(query, (exp.Subquery, exp.Paren)) and query.this is not None:
        query = query.this
    return query


def _sources(select: exp.Select) -> dict[str, exp.Expression]:
    found: dict[str, exp.Expression] = {}
    clause = select.args.get("from_") or select.args.get("from")
    nodes = [clause.this] if clause is not None and clause.this is not None else []
    nodes += [join.this for join in select.args.get("joins") or []]
    for index, node in enumerate(nodes):
        alias = node.alias_or_name if isinstance(node, (exp.Table, exp.Subquery)) else ""
        if alias:
            found[alias.casefold()] = node
        elif isinstance(node, exp.Subquery):
            found[f"#{index}"] = node  # an unaliased subquery: no qualifier can name it, but its columns are read unqualified
    return found


def _ctes(tree: exp.Expression) -> dict[str, list[exp.Expression]]:
    found: dict[str, list[exp.Expression]] = {}
    for cte in tree.find_all(exp.CTE):
        found.setdefault(cte.alias.casefold(), []).append(cte.this)
    return found


def _is_star(projection: exp.Expression) -> bool:
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    return isinstance(projection, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star))


class Walker:
    """Follows an aggregate's argument back to where its value is computed, through CTEs, subqueries and models."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.pipeline = session.pipeline()
        self.parsed = self.pipeline._analyse().parsed
        self.records = {(ref.table, ref.column.casefold()): record for ref, record in session.records().items() if not ref.path}
        self._read: dict[str, object] = {}

    # ------------------------------------------------------------- tables

    def resolve(self, table: exp.Table) -> str | None:
        try:
            key = self.pipeline.resolve(table)
        except Exception:  # noqa: BLE001
            return None
        return key if key in self.session.tables() else None

    def columns_of(self, node: exp.Expression, ctes: dict) -> list[str] | None:
        """The output columns of a source of a query, or ``None`` when they cannot be told."""

        if isinstance(node, exp.Table) and (node.args.get("db") or node.name.casefold() not in ctes):
            key = self.resolve(node)
            if key is None:
                return None
            listed = list(self.pipeline.output_columns(key)) if key in self.pipeline.models else list(self.pipeline.source_schema.get(key, {}))
            return listed if listed and "*" not in listed else None
        if isinstance(node, exp.Table):
            queries = ctes.get(node.name.casefold(), [])
            query = _unwrap(queries[0]) if len(queries) == 1 else None
        else:
            query = _unwrap(node.this) if isinstance(node, exp.Subquery) else None
        if not isinstance(query, exp.Select) or any(_is_star(p) for p in query.expressions):
            return None
        return [p.alias_or_name for p in query.expressions]

    def source_of(self, column: exp.Column, select: exp.Select, ctes: dict) -> tuple[exp.Expression | None, str]:
        """The source of ``select`` that ``column`` reads, or ``(None, reason)``."""

        sources = _sources(select)
        if column.table:
            # a qualifier that names no table of this query is a path into a STRUCT column (or an outer query's
            # column): a stored value, not a computed one, so it is not followed and not reported
            return sources.get(column.table.casefold()), ""
        if len(sources) == 1:
            return next(iter(sources.values())), ""
        if not sources:
            return None, ""
        having = {alias: self.columns_of(node, ctes) for alias, node in sources.items()}
        owners = [alias for alias, names in having.items() if names is not None and column.name.casefold() in {n.casefold() for n in names}]
        if len(owners) == 1 and all(names is not None for names in having.values()):
            return sources[owners[0]], ""
        unsure = [alias for alias, names in having.items() if names is None]
        if not owners and len(unsure) == 1:
            return sources[unsure[0]], ""
        return None, f"which table {column.name} comes from is not known"

    # ------------------------------------------------------------- following

    def argument(self, node: exp.Expression, select: exp.Select, ctes: dict, model: str) -> Trace:
        return self._expression(node, select, ctes, model, "", 0)

    def _expression(self, node: exp.Expression, select: exp.Select, ctes: dict, model: str, column: str, depth: int) -> Trace:
        if depth > MAX_DEPTH:
            return Trace(unknown="too many steps to follow")
        peeled, scale = _peel(node)
        found = _division(peeled)
        if found is not None:
            op, numerator, denominator = found
            bottom = _part(denominator)
            ratio = Ratio("ratio", model, _scope(select), column, clip(_sql(node), 140), op, _sql(_part(numerator)), _sql(bottom),
                          not _has_aggregate(denominator), select, _has_aggregate(numerator) or _has_aggregate(denominator),
                          bottom if isinstance(bottom, exp.Column) else None)
            return Trace(ratio, scale=scale)
        if isinstance(peeled, exp.Avg) and not isinstance(peeled.this, exp.Distinct) and peeled.this is not None:
            inner = _sql(peeled.this)
            ratio = Ratio("average", model, _scope(select), column, clip(_sql(node), 140), "/", f"SUM({inner})", f"COUNT({inner})", False, select, True)
            return Trace(ratio, scale=scale)
        if isinstance(peeled, exp.Column) and not isinstance(peeled.this, exp.Star):
            source, reason = self.source_of(peeled, select, ctes)
            if source is None:
                return Trace(unknown=reason)
            traced = self._source(source, peeled.name, ctes, model, depth + 1)
            return Trace(traced.ratio, traced.unknown, traced.scale + scale)
        return Trace()  # a computation that is not a ratio

    def _source(self, node: exp.Expression, name: str, ctes: dict, model: str, depth: int) -> Trace:
        if isinstance(node, exp.Table):
            folded = node.name.casefold()
            if not node.args.get("db") and folded in ctes:
                queries = ctes[folded]
                if len(queries) != 1:
                    return Trace(unknown="two CTEs share a name")
                return self._query(queries[0], name, ctes, model, depth + 1)
            key = self.resolve(node)
            if key is None:
                return Trace(unknown=_OUTSIDE)
            if key in self.pipeline.models:
                return self._model(key, name, depth + 1)
            return Trace()  # a column of a source table, as stored: not computed in the project
        if isinstance(node, exp.Subquery):
            return self._query(node.this, name, ctes, model, depth + 1)
        return Trace()  # UNNEST and the like: an array element, not a computed column

    def _query(self, query: exp.Expression, name: str, ctes: dict, model: str, depth: int) -> Trace:
        query = _unwrap(query)
        if isinstance(query, exp.SetOperation):
            return Trace(unknown="it comes from a UNION, whose branches are not followed")
        if not isinstance(query, exp.Select):
            return Trace(unknown="it comes from something that is not a SELECT (a template expression, a table function)")
        stars = []
        for projection in query.expressions:
            if _is_star(projection):
                stars.append(projection.this if isinstance(projection, exp.Alias) else projection)
            elif projection.alias_or_name.casefold() == name.casefold():
                inner = projection.this if isinstance(projection, exp.Alias) else projection
                return self._expression(inner, query, ctes, model, projection.alias_or_name, depth + 1)
        if stars:
            sources = _sources(query)
            qualified = {s.table.casefold() for s in stars if isinstance(s, exp.Column) and s.table}
            pool = {alias: node for alias, node in sources.items() if not qualified or alias in qualified}
            if len(pool) == 1:
                return self._source(next(iter(pool.values())), name, ctes, model, depth + 1)
            having = [node for node in pool.values() if name.casefold() in {n.casefold() for n in self.columns_of(node, ctes) or []}]
            if len(having) == 1:
                return self._source(having[0], name, ctes, model, depth + 1)
            return Trace(unknown="SELECT * over several tables")
        return Trace(unknown="the column is not among the query's outputs")

    def _model(self, key: str, column: str, depth: int) -> Trace:
        if depth > MAX_DEPTH:
            return Trace(unknown="too many steps to follow")
        record = self.records.get((key, column.casefold()))
        if record is not None and record.transform in ("passthrough", "renamed") and len(record.sources) == 1:
            (source,) = record.sources
            if source.path:
                return Trace()  # a field inside a STRUCT column
            if source.table in self.pipeline.models:
                return self._model(source.table, source.column, depth + 1)
            return Trace()  # a source column, read as stored
        if record is not None and record.transform == "constant":
            return Trace()
        tree = self.parsed.get(key)
        if tree is None:
            return Trace(unknown=f"{short(key)} could not be parsed")
        return self._query(tree, column, _ctes(tree), key, depth + 1)

    # ------------------------------------------------------------- what the aggregate reads

    def read(self, source: exp.Expression, tree: exp.Expression, ctes: dict) -> "Read":
        """Column meanings and grain of the table (or CTE, subquery) an aggregate reads."""

        if isinstance(source, exp.Table) and (source.args.get("db") or source.name.casefold() not in ctes):
            key = self.resolve(source)
            if key is None:
                return Read(source.alias_or_name or source.name, {}, None)
            if key in self.pipeline.models:
                profile = self.session.profiles().get(key)
                if profile is None:
                    return Read(short(key), {}, None)
                grain = profile.grain.keys if profile.grain.known else None
                return Read(short(key), {a.column: a.meaning for a in profile.attributes}, grain)
            claim = self.session.grain_claim(key)
            meanings = {name: f"col:{key}.{name.lower()}" for name in self.pipeline.source_schema.get(key, {})}
            return Read(short(key), meanings, tuple(claim.keys) if claim is not None else None)
        if isinstance(source, exp.Table):
            label, query = f"CTE {source.name}", _cte_query(tree, source.name, self.columns_of(source, ctes))
        elif isinstance(source, exp.Subquery) and isinstance(_unwrap(source), exp.Query):
            label, query = "the subquery", _with_ctes(tree, _unwrap(source))
        else:
            return Read("its source", {}, None)
        sql = self._qualified(query).sql(dialect="bigquery")
        if sql not in self._read:
            from .table_profile import profile_query

            profile = profile_query(self.pipeline, sql, declared_grain=self.session.declared_grain())
            grain = profile.grain.keys if profile.grain.known else None
            self._read[sql] = Read(label, {a.column: a.meaning for a in profile.attributes}, grain)
        return self._read[sql]


    def _qualified(self, query: exp.Expression) -> exp.Expression:
        """``query`` with every project table written as its full key, the way the profiler resolves it."""

        query = query.copy()
        ctes = _ctes(query)
        for table in list(query.find_all(exp.Table)):
            if not table.name or (not table.args.get("db") and table.name.casefold() in ctes):
                continue
            key = self.resolve(table)
            parts = key.split(".") if key else []
            if len(parts) != 3:
                continue
            alias = table.args.get("alias")
            if alias is None and not isinstance(table.parent, exp.Column):
                alias = exp.TableAlias(this=exp.to_identifier(table.name))  # a column qualified by the table's name still reads it
            table.replace(exp.Table(this=exp.to_identifier(parts[2]), db=exp.to_identifier(parts[1]), catalog=exp.to_identifier(parts[0]), alias=alias))
        return query


@dataclass
class Read:
    """The table an aggregate reads: how to name it, its columns' meanings and its grain (``None`` when unknown)."""

    label: str
    meanings: dict[str, str | None]
    grain: tuple[str, ...] | None


def _with_key(tree: exp.Expression) -> str:
    return "with_" if "with_" in tree.arg_types else "with"


def _cte_query(tree: exp.Expression, name: str, columns: list[str] | None) -> exp.Expression:
    """``WITH <the CTEs of tree> SELECT <its columns> FROM name``: the CTE as a query of its own."""

    wrapped = exp.select(*([exp.column(c) for c in columns] if columns else ["*"])).from_(exp.to_identifier(name))
    found = tree.args.get(_with_key(tree))
    if found is not None:
        wrapped.set(_with_key(wrapped), found.copy())
    return wrapped


def _with_ctes(tree: exp.Expression, query: exp.Expression) -> exp.Expression:
    query = query.copy()
    found = tree.args.get(_with_key(tree))
    if found is not None and query.args.get(_with_key(query)) is None and _with_key(query) in query.arg_types:
        query.set(_with_key(query), found.copy())
    return query


# ------------------------------------------------------------------ numerator and denominator from meanings

_START = re.compile(r"col:|field\(|agg:|window:|union\(")
_COL = re.compile(SOURCE_COLUMN)
_CALL = re.compile(r"[A-Za-z_][A-Za-z_0-9.]*\(")
_OVER = re.compile(r"\s*(?:(?:IGNORE|RESPECT) NULLS\s*)?OVER\s*\(")
_PLACEHOLDER = re.compile(r"\b__kumo_m(\d+)\b")


def _skip_quoted(text: str, start: int) -> int:
    quote = text[start]
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)


def _close(text: str, start: int) -> int | None:
    """The index after the parenthesis that closes the one at ``start``."""

    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            index = _skip_quoted(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _token_end(text: str, index: int, kind: str) -> int | None:
    if kind == "col:":
        found = _COL.match(text, index)
        return found.end() if found else None
    if kind in ("field(", "union("):
        return _close(text, index + len(kind) - 1)
    call = _CALL.match(text, index + len(kind))
    if call is None:
        return None
    end = _close(text, call.end() - 1)
    if end is None or kind == "agg:":
        return end
    over = _OVER.match(text, end)
    return _close(text, over.end() - 1) if over else None


def meaning_tree(text: str) -> tuple[exp.Expression, list[str]] | None:
    """A meaning (``col:...``, ``agg:...``, an expression of those) parsed as SQL, each meaning token a placeholder column."""

    out: list[str] = []
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            end = _skip_quoted(text, index)
            out.append(text[index:end])
            index = end
            continue
        start = _START.match(text, index)
        if start is not None and (index == 0 or not (text[index - 1].isalnum() or text[index - 1] == "_")):
            end = _token_end(text, index, start.group(0))
            if end is None:
                return None
            tokens.append(text[index:end])
            out.append(f"__kumo_m{len(tokens) - 1}")
            index = end
            continue
        out.append(char)
        index += 1
    try:
        tree = sqlglot.parse_one("".join(out), read="bigquery")
    except Exception:  # noqa: BLE001 - a meaning that does not read back is simply not used
        return None
    return (tree, tokens) if tree is not None else None


def meaning_text(node: exp.Expression, tokens: list[str]) -> str:
    text = _unparen(node).sql(dialect="bigquery", normalize_functions="upper")
    return _PLACEHOLDER.sub(lambda m: tokens[int(m.group(1))] if int(m.group(1)) < len(tokens) else m.group(0), text)


def normal_meaning(meaning: str | None) -> str | None:
    """One spelling of a meaning, so a part of a ratio compares with a column's meaning."""

    if not meaning:
        return None
    text = meaning[len("expr:"):] if meaning.startswith("expr:") else meaning
    parsed = meaning_tree(text)
    if parsed is None:
        return " ".join(text.split())
    return meaning_text(parsed[0], parsed[1])


def ratio_parts(meaning: str | None) -> tuple[str, str] | None:
    """The numerator and denominator meanings of a ratio column's meaning, or ``None`` when it is not one."""

    if not meaning:
        return None
    if meaning.startswith("agg:AVG(") and not meaning.startswith("agg:AVG(DISTINCT ") and _close(meaning, len("agg:AVG")) == len(meaning):
        inner = meaning[len("agg:AVG("):-1]
        return f"agg:SUM({inner})", f"agg:COUNT({inner})"
    text = meaning[len("expr:"):] if meaning.startswith("expr:") else meaning
    parsed = meaning_tree(text)
    if parsed is None:
        return None
    tree, tokens = parsed
    found = _division(_peel(tree)[0])
    if found is None:
        return None
    _, numerator, denominator = found
    return meaning_text(_part(numerator), tokens), meaning_text(_part(denominator), tokens)


def _carrier(meaning: str, read: Read) -> str | None:
    """The column of ``read`` whose meaning is ``meaning``."""

    target = normal_meaning(meaning)
    for name, other in read.meanings.items():
        if other and normal_meaning(other) == target:
            return name
    return None


# ------------------------------------------------------------------ one aggregate


@dataclass
class Site:
    """One ``AVG`` or ``SUM`` of a ratio, judged."""

    aggregate: str
    #: ``caution`` (a finding) or ``note`` (the grain does not change)
    level: str
    ratio: Ratio
    what: str
    fix: str
    numerator: str
    denominator: str
    #: the rewrite, or ``""`` when a part is unknown
    rewrite: str
    same_hop: bool
    #: the aggregate as shown, and what the finding says about the ratio after the verb (to word several aggregates as one)
    subject: str = ""
    claim: str = ""


def _why(kind: str, names: list[str]) -> str:
    if kind == "average":
        return "an average of averages weights every group alike, whatever its rows" if "AVG" in names else "a sum of averages is not a total"
    return "an average of per-row ratios is not the ratio of the totals" if "AVG" in names else "a sum of ratios is not a ratio of the totals"


def _rewrite(op: str, numerator: str, denominator: str, scale: list[str], summed: bool = True) -> str:
    """``SAFE_DIVIDE(SUM(n), SUM(d)) * 100``; with ``summed=False`` the parts are already aggregates and are kept as they are."""

    top, bottom = (f"SUM({numerator})", f"SUM({denominator})") if summed else (numerator, denominator)
    text = f"{top} / {bottom}" if op == "/" else f"{op}({top}, {bottom})"
    return text + "".join(f" {factor}" for factor in scale)


def _place(ratio: Ratio) -> str:
    if not ratio.model:
        return f"the draft's {ratio.scope}" if ratio.scope else "the draft"
    return short(ratio.model) + (f" ({ratio.scope})" if ratio.scope else "")


def _group_names(select: exp.Select, alias: str) -> set[str] | None:
    """The source columns ``select`` groups by (folded), or ``None`` when a grouping item is not a plain column."""

    group = select.args.get("group")
    if group is None:
        return set()
    projections = [p.this if isinstance(p, exp.Alias) else p for p in select.expressions]
    aliased = {p.alias_or_name.casefold(): (p.this if isinstance(p, exp.Alias) else p) for p in select.expressions}
    if group.args.get("all"):
        items = [p for p in projections if not _has_aggregate(p)]
    else:
        items = list(group.expressions)
    names: set[str] = set()
    for item in items:
        if isinstance(item, exp.Literal) and not item.is_string and item.name.isdigit() and 0 < int(item.name) <= len(projections):
            item = projections[int(item.name) - 1]
        elif isinstance(item, exp.Column) and not item.table and item.name.casefold() in aliased and isinstance(aliased[item.name.casefold()], exp.Column):
            item = aliased[item.name.casefold()]
        item = _unparen(item)
        if not isinstance(item, exp.Column) or isinstance(item.this, exp.Star):
            return None
        if item.table and item.table.casefold() != alias.casefold():
            continue
        names.add(item.name.casefold())
    return names


def _same_grain(walker: Walker, select: exp.Select, tree: exp.Expression, ctes: dict) -> Read | None:
    """The one table ``select`` reads when each of its groups holds one row of it, else ``None``."""

    sources = _sources(select)
    if len(sources) != 1 or select.args.get("joins"):
        return None
    source = next(iter(sources.values()))
    read = walker.read(source, tree, ctes)
    if read.grain is None:
        return None
    names = _group_names(select, source.alias_or_name)
    if names is None:
        return None
    return read if {key.casefold() for key in read.grain} <= names else None


def _meaning_of(read: Read | None, name: str) -> str | None:
    if read is None:
        return None
    if name in read.meanings:
        return read.meanings[name]
    return next((m for n, m in read.meanings.items() if n.casefold() == name.casefold()), None)


def judge(walker: Walker, aggregate: exp.Expression, tree: exp.Expression, ctes: dict, model: str) -> Site | Trace | None:
    """A :class:`Site` for an ``AVG`` or ``SUM`` of a ratio, the :class:`Trace` when the argument could not be followed,
    or ``None`` when it is not a ratio."""

    argument = aggregate.this
    if isinstance(argument, exp.Distinct):
        argument = argument.expressions[0] if len(argument.expressions) == 1 else None
    select = aggregate.find_ancestor(exp.Select)
    if argument is None or select is None:
        return None
    trace = walker.argument(argument, select, ctes, model)
    if trace.ratio is None:
        return trace if trace.unknown else None
    ratio = trace.ratio
    name = type(aggregate).__name__.upper()
    shown = clip(_sql(aggregate), 70)
    same_hop = ratio.select is select
    written = _rewrite(ratio.op, ratio.numerator, ratio.denominator, trace.scale, summed=not ratio.aggregated)

    if same_hop:
        numerator, denominator, rewrite = ratio.numerator, ratio.denominator, written
        bottom = ratio.denominator_column
        source = walker.source_of(bottom, select, ctes)[0] if bottom is not None else None
        meaning = _meaning_of(walker.read(source, tree, ctes), bottom.name) if source is not None else None
        conversion = ratio.per_row if meaning is None else not meaning.startswith("agg:")
        holder = ""
    else:
        peeled = _peel(argument)[0]
        source = walker.source_of(peeled, select, ctes)[0] if isinstance(peeled, exp.Column) else None
        read = walker.read(source, tree, ctes) if source is not None else None
        parts = ratio_parts(_meaning_of(read, peeled.name)) if read is not None else None
        carriers = tuple(_carrier(part, read) for part in parts) if parts else (None, None)
        holder = read.label if read is not None else "the table it reads"
        numerator, denominator = (c or "unknown" for c in carriers)
        qualifier = f"{peeled.table}." if peeled.table else ""
        rewrite = _rewrite(ratio.op, qualifier + carriers[0], qualifier + carriers[1], trace.scale) if all(carriers) else ""
        conversion = ratio.per_row if parts is None else not parts[1].startswith("agg:")

    computed = f"an average, {ratio.written}" if ratio.kind == "average" else ratio.written
    if same_hop:
        claim = "a per-row ratio"
    else:
        same_name = isinstance(peeled, exp.Column) and ratio.column.casefold() == peeled.name.casefold()
        claim = f"a ratio: {_place(ratio)} computes {'it' if same_name or not ratio.column else ratio.column} as {computed}"
    what = f"{shown} {'averages' if name == 'AVG' else 'adds up'} {claim}; {_why(ratio.kind, [name])}."

    grain = _same_grain(walker, select, tree, ctes)
    if grain is not None:
        note = (f"{shown} reads {grain.label}, which has one row per ({', '.join(grain.grain)}), and groups by all of them: each group "
                f"holds one row, so the {name} is the ratio itself. A coarser grouping needs {rewrite or written}.")
        return Site(name, "note", ratio, clip(note, 300), "", numerator, denominator, rewrite, same_hop, shown, claim)

    if rewrite:
        fix = f"Divide the sums instead: {rewrite}."
    else:
        missing = " and ".join(part for part, found in (("numerator", numerator), ("denominator", denominator)) if found == "unknown")
        fix = (f"Divide the sums instead: {written}, where {_place(ratio)} computes the ratio, grouped at the new grain; "
               f"lineage finds no column of {holder} holding the {missing}.")
    if conversion and ratio.kind == "ratio":
        fix += f" (If the division converts a unit, such as an exchange rate, the {name} is right.)"
    return Site(name, "caution", ratio, clip(what, 300), clip(fix, 300), numerator, denominator, rewrite, same_hop, shown, claim)


def _aggregates(tree: exp.Expression):
    for node in tree.find_all(*AGGREGATES):
        if isinstance(node.parent, exp.Window):
            continue  # a moving average of a ratio is often meant
        yield node


# ------------------------------------------------------------------ check_query


def check_draft(session: Session, tree: exp.Expression, answer: Answer) -> tuple[list[dict], list[tuple[str, str]]]:
    """``(findings, not checked)`` for a draft; a same-grain re-aggregation is added to ``answer`` as a note."""

    try:
        return _check_draft(session, tree, answer)
    except Exception as exc:  # noqa: BLE001 - a check must not take the others down
        return [], [("re-aggregated ratios", f"not checked: the check failed ({type(exc).__name__})")]


def _check_draft(session: Session, tree: exp.Expression, answer: Answer) -> tuple[list[dict], list[tuple[str, str]]]:
    walker = Walker(session)
    ctes = _ctes(tree)
    notes: list[str] = []
    unknown: dict[str, list[str]] = {}
    #: one finding per ratio: several aggregates of the same ratio are said once
    groups: dict[tuple, list[Site]] = {}
    for aggregate in _aggregates(tree):
        judged = judge(walker, aggregate, tree, ctes, "")
        if isinstance(judged, Trace):
            if judged.unknown != _OUTSIDE:  # "N tables not in the project" already says it
                unknown.setdefault(judged.unknown, []).append(clip(_sql(aggregate), 50))
            continue
        if judged is None:
            continue
        if judged.level == "note":
            if judged.what not in notes:
                notes.append(judged.what)
            continue
        ratio = judged.ratio
        key = (ratio.model, ratio.scope, ratio.column, ratio.written, id(ratio.select) if judged.same_hop else 0, judged.fix)
        sites = groups.setdefault(key, [])
        if all(s.subject != judged.subject for s in sites):
            sites.append(judged)
    findings = [_finding(sites) for sites in groups.values()]
    if notes:
        answer.add_list("notes", notes)
    gaps = []
    for reason, items in unknown.items():
        items = list(dict.fromkeys(items))
        names = ", ".join(items[:EXAMPLES]) + (", ..." if len(items) > EXAMPLES else "")
        gaps.append((f"whether {len(items)} AVG/SUM argument{'s are' if len(items) != 1 else ' is'} a ratio: {names}", clip(reason, 160)))
    return findings, gaps


def _finding(sites: list[Site]) -> dict:
    """One ``check_query`` finding for the aggregates of one ratio."""

    first = sites[0]
    what = first.what
    if len(sites) > 1:
        names = [s.aggregate for s in sites]
        subjects = ", ".join(s.subject for s in sites[:2]) + (f" and {len(sites) - 2} more" if len(sites) > 2 else "")
        what = clip(f"{subjects} re-aggregate {first.claim}; {_why(first.ratio.kind, names)}.", 300)
    return {"check": CHECK, "level": "caution", "what": what, "fix": first.fix,
            "numerator": clip(first.numerator, 60), "denominator": clip(first.denominator, 60)}


# ------------------------------------------------------------------ project_checks


def project_findings(session: Session) -> list:
    """Models that average or sum a ratio computed upstream (in another model, a CTE or a subquery), as
    :class:`~kumosql.project_checks.Finding` items; computed once per session. Same-grain cases are left out."""

    return session._once("ratio_findings", lambda: _project_findings(session))


def _project_findings(session: Session) -> list:
    from .project_checks import Finding

    walker = Walker(session)
    pipeline = walker.pipeline
    found = []
    for key in sorted(pipeline.models):
        model = pipeline.models[key]
        tree = walker.parsed.get(key)
        if tree is None or model.kind == "assertion":
            continue
        ctes = _ctes(tree)
        seen: set[str] = set()
        for aggregate in _aggregates(tree):
            try:
                judged = judge(walker, aggregate, tree, ctes, key)
            except Exception:  # noqa: BLE001 - one odd query must not stop the check
                continue
            if not isinstance(judged, Site) or judged.level != "caution" or judged.same_hop:
                continue
            ratio = judged.ratio
            origin = short(ratio.model) if ratio.model != key else (ratio.scope or "its own query")
            fix = f"should be {judged.rewrite}" if judged.rewrite else "re-aggregates a ratio whose parts it does not have"
            line = f"{short(key)}: {clip(_sql(aggregate), 60)} {fix}; the ratio is from {origin}"
            if line in seen:
                continue
            seen.add(line)
            related = (ratio.model,) if ratio.model and ratio.model != key else ()
            found.append(Finding(PROJECT_CHECK, key, clip(line, 200), related))
    return found
