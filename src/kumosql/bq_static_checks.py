"""BigQuery-specific static checks for ``check_query``: mistakes BigQuery rejects or silently gets wrong, found from the text alone.

``check_query`` used to answer "No problems found" for ``SELECT x AS new FROM t`` although BigQuery refuses it. These rules close
the gaps that came up again and again in an analyst's drafts. They read only the parsed draft (and, where a rule needs them, the
columns the project declares); they never reach the network.

The single entry point is :func:`static_checks`. It returns ``(findings, gaps)`` in the shape ``check_query`` uses: a finding is
``{check, what, fix}`` and ``check`` is the short rule name below; a gap is ``(what, reason)`` for something a rule could not
decide (it goes under ``unknown``, never passes silently). A rule reports a problem only when it is certain; when it is not, it
says nothing about that spot, or counts it as a gap.

=================== ==========================================================================================
rule                what it finds
=================== ==========================================================================================
``reserved_alias``  a GoogleSQL reserved keyword used unquoted as a column, table, subquery or CTE alias
``subquery``        a correlated subquery BigQuery cannot de-correlate (in a JOIN condition, with ``LIMIT``, or
                    correlated only by an inequality), and an ``IN (SELECT ...)`` inside a JOIN condition
``not_in_null``     ``NOT IN`` over a list holding NULL, or over a subquery column that may hold NULL (no rows)
``limit_reread``    ``LIMIT`` without ``ORDER BY`` in a CTE that is read more than once
``duplicate_column`` two output columns with one name (``t.*`` plus an alias that ``t`` already has)
``case_branch``     a ``CASE`` branch (or ``ELSE``) that can never be reached
``system_time``     ``FOR SYSTEM_TIME AS OF`` on only some references to one table
``cte_shadow``      a CTE named like a column of another table in scope, so the column reads as the CTE's STRUCT
=================== ==========================================================================================

Optional hooks on the session (none is required; a missing one is a named gap, not a pass):

* ``session.column_is_nullable(table_key, column) -> bool | None``: from a schema snapshot; ``None`` or no hook means the
  nullability is unknown, and ``not_in_null`` then reports that it could not decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Callable

from sqlglot import exp

from .mcp_answers import clip

#: GoogleSQL reserved keywords: these cannot be used as an identifier unless enclosed in backticks. Source: the "Reserved
#: keywords" list of the GoogleSQL lexical structure reference (BigQuery docs, and the same page in the open-source ZetaSQL
#: repository, docs/lexical.md), which holds 97 words; GRAPH_TABLE, only in the ZetaSQL copy, is left out so that a word is
#: reported only when it is certain BigQuery rejects it. Keywords that page does not list (DATE, KEY, VALUE, ...) are valid
#: identifiers.
RESERVED_KEYWORDS = frozenset(
    """
    ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BETWEEN BY CASE CAST COLLATE CONTAINS CREATE CROSS CUBE CURRENT DEFAULT
    DEFINE DESC DISTINCT ELSE END ENUM ESCAPE EXCEPT EXCLUDE EXISTS EXTRACT FALSE FETCH FOLLOWING FOR FROM FULL GROUP GROUPING
    GROUPS HASH HAVING IF IGNORE IN INNER INTERSECT INTERVAL INTO IS JOIN LATERAL LEFT LIKE LIMIT LOOKUP MERGE NATURAL NEW NO
    NOT NULL NULLS OF ON OR ORDER OUTER OVER PARTITION PRECEDING PROTO QUALIFY RANGE RECURSIVE RESPECT RIGHT ROLLUP ROWS SELECT
    SET SOME STRUCT TABLESAMPLE THEN TO TREAT TRUE UNBOUNDED UNION UNNEST USING WHEN WHERE WINDOW WITH WITHIN
    """.split()
)

_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
_AGGREGATE = (exp.AggFunc,)


@dataclass
class Context:
    """What a rule may read: the draft, and the project columns/keys of the tables it names."""

    session: object
    tree: exp.Expression
    sql: str
    #: spelling in the draft (lower case) -> project key or None
    tables: dict[str, str | None]
    findings: list[dict]
    gaps: dict[str, list[str]]

    def find(self, check: str, what: str, fix: str) -> None:
        self.findings.append({"check": check, "what": clip(what, 240), "fix": clip(fix, 200)})

    def gap(self, what: str, reason: str) -> None:
        self.gaps.setdefault(what, []).append(reason)


def static_checks(session: object, tree: exp.Expression, sql: str, tables: dict[str, str | None]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Run every rule on the parsed draft: ``(findings, gaps)``. A rule that fails to run is a gap, never a finding."""

    ctx = Context(session, tree, sql, tables, [], {})
    for name, rule in RULES:
        try:
            rule(ctx)
        except Exception as exc:  # noqa: BLE001 - one rule failing must not hide the others
            ctx.gap(f"static check {name}", f"could not run ({type(exc).__name__})")
    gaps = []
    for what, reasons in ctx.gaps.items():
        count = len(reasons)
        gaps.append((f"{count} {what}" if count > 1 else what, reasons[0]))
    return ctx.findings, gaps


# ------------------------------------------------------------------------ helpers


def _folded(text: object) -> str:
    return str(text or "").casefold()


def _sources(select: exp.Select) -> list[exp.Expression]:
    """The FROM and JOIN sources of a ``SELECT``, in order."""

    found = []
    source = select.args.get("from_") or select.args.get("from")
    if source is not None and source.this is not None:
        found.append(source.this)
    for join in select.args.get("joins") or []:
        if join.this is not None:
            found.append(join.this)
    return found


def _source_name(source: exp.Expression) -> str:
    return _folded(source.alias_or_name) if hasattr(source, "alias_or_name") else ""


def _bound_names(node: exp.Expression) -> set[str]:
    """Every source name (alias, or table name) bound by a FROM/JOIN anywhere inside ``node``, plus CTE names."""

    names = {_source_name(s) for select in node.find_all(exp.Select) for s in _sources(select)}
    names |= {_folded(cte.alias) for cte in node.find_all(exp.CTE)}
    if isinstance(node, exp.Select):
        names |= {_source_name(s) for s in _sources(node)}
    names.discard("")
    return names


def _ancestor_names(node: exp.Expression) -> set[str]:
    names: set[str] = set()
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            names |= {_source_name(s) for s in _sources(parent)}
        parent = parent.parent
    names.discard("")
    return names


def _spelled(table: exp.Table) -> str:
    return ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name).casefold()


def _is_cte_reference(table: exp.Table) -> bool:
    return not table.args.get("db") and bool(table.name)


def _ctes(tree: exp.Expression) -> dict[str, exp.CTE]:
    return {_folded(cte.alias): cte for cte in tree.find_all(exp.CTE)}


def _output_name(item: exp.Expression) -> str | None:
    if isinstance(item, exp.Alias):
        return item.alias
    if isinstance(item, exp.Column) and not isinstance(item.this, exp.Star):
        return item.name
    return None


def _table_columns(ctx: Context, table: exp.Table, ctes: dict[str, exp.CTE], depth: int) -> list[str] | None:
    """The column names of a table reference: a CTE's own output, or the columns the project declares; ``None`` when unknown."""

    if _is_cte_reference(table) and _folded(table.name) in ctes:
        names = _query_columns(ctx, ctes[_folded(table.name)].this, ctes, depth + 1)
        return None if names is None else [n for n in names if n is not None]
    key = ctx.tables.get(_spelled(table))
    if key is None:
        return None
    pipeline = ctx.session.pipeline()
    if key in pipeline.models:
        columns = list(pipeline.output_columns(key))
    else:
        columns = list((pipeline.source_schema.get(key) or {}))
    return columns or None


def _source_columns(ctx: Context, source: exp.Expression, ctes: dict[str, exp.CTE], depth: int) -> list[str] | None:
    if isinstance(source, exp.Table):
        return _table_columns(ctx, source, ctes, depth)
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Query):
        names = _query_columns(ctx, source.this, ctes, depth + 1)
        return None if names is None else [n for n in names if n is not None]
    return None


def _query_columns(ctx: Context, query: exp.Expression, ctes: dict[str, exp.CTE], depth: int = 0) -> list[str | None] | None:
    """The output names of a query (``None`` entries for unnamed expressions); ``None`` when a star cannot be expanded."""

    if depth > 6:
        return None
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        return _query_columns(ctx, query.this, ctes, depth + 1)
    if not isinstance(query, exp.Select):
        return None
    names: list[str | None] = []
    for item in query.expressions:
        star = item if isinstance(item, exp.Star) else item.this if isinstance(item, exp.Column) and isinstance(item.this, exp.Star) else None
        if star is None:
            names.append(_output_name(item))
            continue
        expanded = _expand_star(ctx, query, item, star, ctes, depth)
        if expanded is None:
            return None
        names += expanded
    return names


def _expand_star(ctx: Context, select: exp.Select, item: exp.Expression, star: exp.Star, ctes: dict[str, exp.CTE], depth: int) -> list[str] | None:
    sources = _sources(select)
    if isinstance(item, exp.Column):
        wanted = _folded(item.table)
        chosen = [s for s in sources if _source_name(s) == wanted]
        if len(chosen) != 1:
            return None
    else:
        chosen = sources
        if any(join.args.get("using") for join in select.args.get("joins") or []):
            return None  # USING merges the joined columns in SELECT *; not modelled
    columns: list[str] = []
    for source in chosen:
        found = _source_columns(ctx, source, ctes, depth)
        if found is None:
            return None
        columns += found
    removed = {_folded(c.name) for c in star.args.get("except_") or [] if getattr(c, "name", None)}
    return [c for c in columns if _folded(c) not in removed]


def _literal_text(node: exp.Expression) -> str:
    return clip(node.sql(dialect="bigquery"), 60)


# --------------------------------------------------------------- 1. reserved aliases


def _reserved_aliases(ctx: Context) -> None:
    """A reserved keyword as an alias or CTE name must be in backticks; sqlglot accepts them, BigQuery does not."""

    candidates: list[tuple[exp.Identifier, str]] = []
    for select in ctx.tree.find_all(exp.Select):
        for item in select.expressions:
            if isinstance(item, exp.Alias) and isinstance(item.args.get("alias"), exp.Identifier):
                candidates.append((item.args["alias"], "column alias"))
    for alias in ctx.tree.find_all(exp.TableAlias):
        kind = "CTE name" if isinstance(alias.parent, exp.CTE) else "table alias"
        if isinstance(alias.this, exp.Identifier):
            candidates.append((alias.this, kind))
        candidates += [(c, "column alias") for c in alias.args.get("columns") or [] if isinstance(c, exp.Identifier)]
    for unnest in ctx.tree.find_all(exp.Unnest):
        if isinstance(unnest.args.get("offset"), exp.Identifier):
            candidates.append((unnest.args["offset"], "offset alias"))
    unverified = 0
    seen: set[tuple[int, int]] = set()
    for identifier, kind in candidates:
        if identifier.name.upper() not in RESERVED_KEYWORDS or identifier.args.get("quoted"):
            continue
        meta = identifier.meta or {}
        start, end = meta.get("start"), meta.get("end")
        if start is None or end is None or ctx.sql[start : end + 1].casefold() != identifier.name.casefold():
            unverified += 1  # the position does not match the source text: not certain the word is unquoted
            continue
        if (start, end) in seen:
            continue
        seen.add((start, end))
        word = identifier.name
        ctx.find("reserved_alias", f"{word.upper()} is a reserved GoogleSQL keyword and is used unquoted as a {kind}; BigQuery rejects it",
                 f"Quote it with backticks (`{word}`) or rename it (for example {word.lower()}_value).")
    if unverified:
        ctx.gap("reserved-word aliases not verified", "their position in the text could not be matched, so it is not certain they are unquoted")


# ---------------------------------------------------------------------- 2. subqueries


def _expression_subqueries(tree: exp.Expression) -> list[tuple[str, exp.Expression, exp.Expression]]:
    """``(kind, node, query)`` of every subquery used as an expression: scalar, ``IN``, ``EXISTS`` (derived tables are not)."""

    found = []
    for node in tree.find_all(exp.Subquery, exp.Exists):
        if isinstance(node, exp.Exists):
            if isinstance(node.this, exp.Query):
                found.append(("exists", node, node.this))
            elif isinstance(node.this, exp.Subquery) and isinstance(node.this.this, exp.Query):
                found.append(("exists", node, node.this.this))
            continue
        parent = node.parent
        if isinstance(parent, exp.In) and node.arg_key == "query":
            found.append(("in", node, node.this))
        elif isinstance(parent, (exp.From, exp.Join, exp.Lateral, exp.Table, exp.CTE, exp.Subquery, exp.Unnest)):
            continue  # a derived table, not an expression
        elif isinstance(node.this, exp.Query):
            found.append(("scalar", node, node.this))
    return found


def _correlation(query: exp.Expression, node: exp.Expression) -> list[exp.Column]:
    """Columns inside ``query`` qualified by a name that only an enclosing query binds (an outer reference)."""

    inner = _bound_names(query)
    outer = _ancestor_names(node)
    return [c for c in query.find_all(exp.Column) if c.table and _folded(c.table) not in inner and _folded(c.table) in outer]


def _reads_a_table(query: exp.Expression) -> bool:
    return any(isinstance(t, exp.Table) and t.name for t in query.find_all(exp.Table))


def _inside_join_condition(node: exp.Expression) -> bool:
    """Whether ``node`` sits in the ON condition of a join (looking up to the nearest enclosing SELECT)."""

    child, parent = node, node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.Join) and child.arg_key == "on":
            return True
        child, parent = parent, parent.parent
    return False


def _subqueries(ctx: Context) -> None:
    unclassified = 0
    for kind, node, query in _expression_subqueries(ctx.tree):
        in_join = _inside_join_condition(node)
        label = clip(query.sql(dialect="bigquery"), 70)
        if kind == "in" and in_join:
            ctx.find("subquery", f"IN (SELECT ...) inside a JOIN condition is rejected by BigQuery: ({label})",
                     "Join to the subquery instead: put it in a CTE or derived table and JOIN it on equality, or move the IN test to WHERE.")
            continue
        outer_refs = _correlation(query, node)
        if not outer_refs or not _reads_a_table(query):
            continue
        reason = None
        if in_join:
            reason = "it sits in a JOIN condition"
        elif kind == "scalar" and isinstance(query, exp.Select) and query.args.get("limit") is not None:
            reason = "a scalar subquery with LIMIT cannot be turned into a join"
        elif kind == "scalar" and _only_inequality_correlation(query, node):
            reason = "it is correlated only by an inequality (<, <=, >, >=, <>)"
        if reason is None:
            unclassified += 1
            continue
        ctx.find("subquery", f"correlated subquery reads other tables and BigQuery cannot de-correlate it: {reason} ({label})",
                 "Rewrite it as a JOIN to a pre-aggregated CTE (GROUP BY the correlation key), or use a window function such as ROW_NUMBER().")
    if unclassified:
        ctx.gap("correlated subqueries not classified", "BigQuery decides whether it can turn them into a join; only the shapes above are certain, so a dry run is the check for these")


def _only_inequality_correlation(query: exp.Expression, node: exp.Expression) -> bool:
    """True when every comparison that mixes an inner and an outer column is an inequality and there is at least one."""

    inner, outer = _bound_names(query), _ancestor_names(node)
    mixed = []
    for comparison in query.find_all(*_COMPARISONS):
        sides = [c for side in (comparison.left, comparison.right) for c in ([side] if isinstance(side, exp.Column) else [])]
        tables = {_folded(c.table) for c in sides if c.table}
        if any(t in outer and t not in inner for t in tables):
            mixed.append(comparison)
    if not mixed:
        return False
    # an outer column used any other way (a function, a BETWEEN) is not judged
    judged = {id(c) for comparison in mixed for c in comparison.find_all(exp.Column)}
    for column in _correlation(query, node):
        if id(column) not in judged:
            return False
    return all(not isinstance(c, exp.EQ) for c in mixed)


# --------------------------------------------------------------------- 3. NOT IN


def _not_in_nodes(tree: exp.Expression):
    for node in tree.find_all(exp.Not):
        if isinstance(node.this, exp.In):
            yield node.this


def _where_not_null_columns(query: exp.Select) -> set[tuple[str, str]]:
    """Columns the subquery's top-level WHERE keeps non-NULL (``col IS NOT NULL`` as an AND-ed condition)."""

    found: set[tuple[str, str]] = set()
    where = query.args.get("where")
    stack = [where.this] if where is not None else []
    while stack:
        node = stack.pop()
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            stack += [node.left, node.right]
        elif isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.this, exp.Column) and isinstance(node.this.expression, exp.Null):
            column = node.this.this
            found.add((_folded(column.table), _folded(column.name)))
    return found


def _not_in(ctx: Context) -> None:
    undecided = 0
    hook: Callable[[str, str], bool | None] | None = getattr(ctx.session, "column_is_nullable", None)
    for node in _not_in_nodes(ctx.tree):
        subject = clip(node.this.sql(dialect="bigquery"), 40) if node.this is not None else "the value"
        listed = node.args.get("expressions")
        if listed:
            if any(isinstance(item, exp.Null) for item in listed):
                ctx.find("not_in_null", f"{subject} NOT IN (..., NULL) never returns a row: the comparison with NULL is never true",
                         "Remove NULL from the list, or test NULL separately (x IS NULL).")
            continue
        query = node.args.get("query")
        query = query.this if isinstance(query, exp.Subquery) else query
        if not isinstance(query, exp.Select) or len(query.expressions) != 1:
            if isinstance(query, exp.Query):
                undecided += 1
            continue
        column = query.expressions[0]
        while isinstance(column, exp.Alias):
            column = column.this
        if isinstance(column, exp.Literal) or isinstance(column, exp.Count):
            continue  # a literal and COUNT are never NULL
        verdict = _subquery_column_nullable(ctx, query, column, hook)
        if verdict is False:
            continue
        if verdict is None:
            undecided += 1
            continue
        ctx.find("not_in_null", f"{subject} NOT IN (SELECT {clip(column.sql(dialect='bigquery'), 40)} ...): the column is nullable, and one NULL makes the NOT IN return no rows",
                 f"Use NOT EXISTS (SELECT 1 ... WHERE ... = {subject}), or add {clip(column.sql(dialect='bigquery'), 40)} IS NOT NULL to the subquery.")
    if undecided:
        ctx.gap("NOT IN subqueries not checked", "nullability unknown: a NOT IN returns no rows when the subquery holds a NULL, and the project has no schema snapshot saying whether the column can be NULL")


def _subquery_column_nullable(ctx: Context, query: exp.Select, column: exp.Expression, hook) -> bool | None:
    """Whether the subquery's output column can be NULL: ``True``/``False``, or ``None`` when it is not known."""

    if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
        return None
    if (_folded(column.table), _folded(column.name)) in _where_not_null_columns(query) or (("", _folded(column.name)) in _where_not_null_columns(query)):
        return False
    if hook is None:
        return None
    sources = _sources(query)
    if column.table:
        chosen = [s for s in sources if _source_name(s) == _folded(column.table)]
    else:
        chosen = sources
    if len(chosen) != 1 or not isinstance(chosen[0], exp.Table):
        return None
    key = ctx.tables.get(_spelled(chosen[0]))
    if key is None:
        return None
    try:
        answer = hook(key, column.name)
    except Exception:  # noqa: BLE001 - a broken hook is "unknown", never a finding
        return None
    return answer if isinstance(answer, bool) else None


# --------------------------------------------------------------------- 4. LIMIT


def _limit_reread(ctx: Context) -> None:
    ctes = list(ctx.tree.find_all(exp.CTE))
    names = [_folded(c.alias) for c in ctes]
    repeated = {n for n in names if names.count(n) > 1}
    if repeated:
        ctx.gap("CTEs with a repeated name not checked for LIMIT", "the same CTE name is defined more than once, so reads cannot be told apart")
    for cte in ctes:
        name = _folded(cte.alias)
        body = cte.this
        with_ = cte.parent
        if name in repeated or (with_ is not None and with_.args.get("recursive")):
            continue
        if not isinstance(body, (exp.Select, exp.SetOperation)) or body.args.get("limit") is None or body.args.get("order") is not None:
            continue
        limit = body.args["limit"].expression
        if isinstance(limit, exp.Literal) and limit.name == "0":
            continue
        if isinstance(body, exp.Select) and _single_row(body):
            continue
        reads = sum(1 for t in ctx.tree.find_all(exp.Table) if _is_cte_reference(t) and _folded(t.name) == name and not _within(t, cte))
        if reads > 1:
            ctx.find("limit_reread", f"CTE {cte.alias} has LIMIT without ORDER BY and is read {reads} times; each read may return different rows",
                     f"Add an ORDER BY on a unique key inside {cte.alias}, or materialize it once (a temp table) and read that.")


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _single_row(select: exp.Select) -> bool:
    """A SELECT with aggregates and no GROUP BY, or no FROM, returns at most one row: its LIMIT cannot pick among rows."""

    if select.args.get("from_") is None and select.args.get("from") is None:
        return True
    return select.args.get("group") is None and any(select.find_all(*_AGGREGATE)) and not any(select.find_all(exp.Window))


# ---------------------------------------------------------- 5. duplicate output columns


def _query_bodies(tree: exp.Expression):
    """Selects whose columns are exposed by name: the query itself, CTE bodies and derived tables."""

    def body(node):
        while isinstance(node, exp.Subquery):
            node = node.this
        return node

    roots = [tree]
    roots += [cte.this for cte in tree.find_all(exp.CTE)]
    roots += [s.this for s in tree.find_all(exp.Subquery) if isinstance(s.parent, (exp.From, exp.Join))]
    for root in roots:
        node = body(root)
        if isinstance(node, exp.SetOperation):
            node = body(node.this)
            while isinstance(node, exp.SetOperation):
                node = body(node.this)
        if isinstance(node, exp.Select):
            yield node


def _duplicate_columns(ctx: Context) -> None:
    ctes = _ctes(ctx.tree)
    unknown = 0
    for select in _query_bodies(ctx.tree):
        has_star = any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in select.expressions)
        if not has_star or len(select.expressions) < 2:
            continue
        starred: dict[str, str] = {}  # folded name -> the star that outputs it
        explicit: list[str] = []
        clash = None
        ok = True
        for item in select.expressions:
            star = item if isinstance(item, exp.Star) else item.this if isinstance(item, exp.Column) and isinstance(item.this, exp.Star) else None
            if star is None:
                name = _output_name(item)
                if name is not None:
                    explicit.append(name)
                continue
            columns = _expand_star(ctx, select, item, star, ctes, 0)
            if columns is None:
                ok = False
                continue
            label = f"{item.table}.*" if isinstance(item, exp.Column) else "*"
            for column in columns:
                starred.setdefault(_folded(column), label)
        for name in explicit:
            if _folded(name) in starred and clash is None:
                clash = (name, starred[_folded(name)])
        if clash:
            name, label = clash
            ctx.find("duplicate_column", f"{label} already outputs a column named {name}, and another output is also named {name}; BigQuery renames one to {name}_1",
                     f"Give the explicit column another alias, or remove {name} from the star with {label} EXCEPT ({name}).")
        elif not ok:
            unknown += 1
    if unknown:
        ctx.gap("star expansions with unknown columns", "the columns of the starred table are not declared by the project, so a clash with an explicit alias cannot be ruled out")


# ---------------------------------------------------------------------- 6. CASE


_Interval = tuple  # (low, low_closed, high, high_closed); None bounds are unbounded


@dataclass(frozen=True)
class Domain:
    """The values of one column for which a condition is TRUE (a NULL comparison is never TRUE)."""

    nulls: bool = False
    #: every non-NULL value (``IS NOT NULL``)
    nonnull: bool = False
    intervals: tuple = ()
    points: frozenset = frozenset()

    def empty(self) -> bool:
        return not (self.nulls or self.nonnull or self.intervals or self.points)


def _merge(intervals: list) -> tuple:
    items = sorted((i for i in intervals if not _interval_empty(i)), key=lambda i: (i[0] is not None, i[0] if i[0] is not None else 0, not i[1]))
    merged: list = []
    for low, low_closed, high, high_closed in items:
        if merged:
            plow, plc, phigh, phc = merged[-1]
            touches = phigh is None or low is None or low < phigh or (low == phigh and (phc or low_closed))
            if touches:
                if phigh is not None and (high is None or high > phigh or (high == phigh and high_closed and not phc)):
                    merged[-1] = (plow, plc, high, high_closed)
                continue
        merged.append((low, low_closed, high, high_closed))
    return tuple(merged)


def _interval_empty(i: _Interval) -> bool:
    low, low_closed, high, high_closed = i
    if low is None or high is None:
        return False
    return low > high or (low == high and not (low_closed and high_closed))


def _intersect_interval(a: _Interval, b: _Interval) -> _Interval:
    if a[0] is None or (b[0] is not None and b[0] > a[0]):
        low, lc = b[0], b[1]
    elif b[0] is None or a[0] > b[0]:
        low, lc = a[0], a[1]
    else:
        low, lc = a[0], a[1] and b[1]
    if a[2] is None or (b[2] is not None and b[2] < a[2]):
        high, hc = b[2], b[3]
    elif b[2] is None or a[2] < b[2]:
        high, hc = a[2], a[3]
    else:
        high, hc = a[2], a[3] and b[3]
    return (low, lc, high, hc)


def _interval_inside(a: _Interval, b: _Interval) -> bool:
    low_ok = b[0] is None or (a[0] is not None and (a[0] > b[0] or (a[0] == b[0] and (b[1] or not a[1]))))
    high_ok = b[2] is None or (a[2] is not None and (a[2] < b[2] or (a[2] == b[2] and (b[3] or not a[3]))))
    return low_ok and high_ok


def _union(a: Domain, b: Domain) -> Domain:
    return Domain(a.nulls or b.nulls, a.nonnull or b.nonnull, _merge(list(a.intervals) + list(b.intervals)), a.points | b.points)


def _intersection(a: Domain, b: Domain) -> Domain:
    if a.nonnull and not (b.intervals or b.points or b.nonnull or b.nulls):
        return Domain()
    nulls = a.nulls and b.nulls
    if a.nonnull:
        return Domain(nulls, b.nonnull, b.intervals, b.points)
    if b.nonnull:
        return Domain(nulls, False, a.intervals, a.points)
    intervals = _merge([_intersect_interval(x, y) for x in a.intervals for y in b.intervals])
    return Domain(nulls, False, intervals, a.points & b.points)


def _subset(a: Domain, b: Domain) -> bool:
    """Whether every value that makes ``a`` TRUE also makes ``b`` TRUE."""

    if a.nulls and not b.nulls:
        return False
    if b.nonnull:
        return True
    if a.nonnull:
        return False
    if not a.points <= b.points:
        return False
    return all(any(_interval_inside(i, j) for j in b.intervals) for i in a.intervals)


def _number(node: exp.Expression) -> Fraction | None:
    sign = 1
    while isinstance(node, (exp.Paren, exp.Neg)):
        if isinstance(node, exp.Neg):
            sign = -sign
        node = node.this
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return sign * Fraction(Decimal(node.name))
        except (InvalidOperation, ValueError):
            return None
    return None


def _value(node: exp.Expression):
    """A literal as a comparable value: ``("n", Fraction)``, ``("s", str)``, ``("b", bool)``, or ``None`` when it is not one."""

    number = _number(node)
    if number is not None:
        return ("n", number)
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        return ("s", node.name)
    if isinstance(node, exp.Boolean):
        return ("b", bool(node.this))
    return None


def _point(value) -> Domain:
    if value[0] == "n":
        return Domain(intervals=((value[1], True, value[1], True),))
    return Domain(points=frozenset({value}))


_FLIP = {exp.LT: exp.GT, exp.LTE: exp.GTE, exp.GT: exp.LT, exp.GTE: exp.LTE, exp.EQ: exp.EQ, exp.NEQ: exp.NEQ}


def _compare_constants(op, left, right) -> bool | None:
    if left[0] != right[0]:
        return None
    if left[0] != "n" and op not in (exp.EQ, exp.NEQ):
        return None
    a, b = left[1], right[1]
    return {exp.EQ: a == b, exp.NEQ: a != b, exp.LT: a < b if left[0] == "n" else None, exp.LTE: a <= b if left[0] == "n" else None,
            exp.GT: a > b if left[0] == "n" else None, exp.GTE: a >= b if left[0] == "n" else None}[op]


@dataclass(frozen=True)
class Cond:
    """What is known about a condition: a constant, or the values of one column that make it TRUE.

    ``exact`` is False when the true set is only known to lie inside ``domain`` (an AND with a part not understood).
    """

    kind: str  # "const" or "column"
    value: bool | None = None
    column: tuple[str, str] | None = None
    domain: Domain | None = None
    exact: bool = True


def _column_key(node: exp.Expression):
    return (_folded(node.table), _folded(node.name)) if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star) else None


def _classify(node: exp.Expression) -> Cond | None:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Boolean):
        return Cond("const", bool(node.this))
    if isinstance(node, exp.Null):
        return Cond("const", None)
    if isinstance(node, _COMPARISONS):
        left, right = node.left, node.right
        lv, rv = _value(left), _value(right)
        if lv is not None and rv is not None:
            result = _compare_constants(type(node), lv, rv)
            return None if result is None else Cond("const", result)
        if _column_key(right) is not None and lv is not None:
            left, right, lv, rv = right, left, rv, lv
            op = _FLIP[type(node)]
        else:
            op = type(node)
        key = _column_key(left)
        if key is None or rv is None or op is exp.NEQ:
            return None
        if rv[0] != "n" and op is not exp.EQ:
            return None
        if op is exp.EQ:
            return Cond("column", column=key, domain=_point(rv))
        n = rv[1]
        interval = {exp.LT: (None, False, n, False), exp.LTE: (None, False, n, True), exp.GT: (n, False, None, False), exp.GTE: (n, True, None, False)}[op]
        return Cond("column", column=key, domain=Domain(intervals=(interval,)))
    if isinstance(node, exp.Between):
        key, low, high = _column_key(node.this), _number(node.args.get("low")), _number(node.args.get("high"))
        if key is None or low is None or high is None:
            return None
        return Cond("column", column=key, domain=Domain(intervals=_merge([(low, True, high, True)])))
    if isinstance(node, exp.In) and not node.args.get("query") and node.args.get("expressions") and not node.args.get("unnest"):
        key = _column_key(node.this)
        values = [_value(v) for v in node.expressions]
        if key is None or any(v is None for v in values):
            return None
        domain = Domain()
        for value in values:
            domain = _union(domain, _point(value))
        return Cond("column", column=key, domain=domain)
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        key = _column_key(node.this)
        return None if key is None else Cond("column", column=key, domain=Domain(nulls=True))
    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
        key = _column_key(node.this.this)
        return None if key is None else Cond("column", column=key, domain=Domain(nonnull=True))
    if isinstance(node, exp.Or):
        parts = [_classify(p) for p in _flatten(node, exp.Or)]
        if any(p is None or not p.exact for p in parts):
            return None
        if all(p.kind == "const" for p in parts):
            values = [p.value for p in parts]
            return Cond("const", True if True in values else None if None in values else False)
        columns = {p.column for p in parts if p.kind == "column"}
        if len(columns) != 1 or any(p.kind == "const" and p.value is not False and p.value is not None for p in parts):
            return None
        domain = Domain()
        for p in parts:
            if p.kind == "column":
                domain = _union(domain, p.domain)
        return Cond("column", column=next(iter(columns)), domain=domain)
    if isinstance(node, exp.And):
        parts = [_classify(p) for p in _flatten(node, exp.And)]
        known = [p for p in parts if p is not None]
        if any(p.kind == "const" and p.value is not True for p in known):
            return Cond("const", False)  # a constant FALSE or NULL conjunct: never TRUE
        by_column: dict[tuple[str, str], Domain] = {}
        for p in known:
            if p.kind == "column":
                previous = by_column.get(p.column)
                by_column[p.column] = p.domain if previous is None else _intersection(previous, p.domain)
        if any(d.empty() for d in by_column.values()):
            return Cond("const", False)
        exact = len(known) == len(parts) and all(p.exact for p in known)
        if len(by_column) == 1:
            column, domain = next(iter(by_column.items()))
            return Cond("column", column=column, domain=domain, exact=exact)
        if not by_column and exact:
            return Cond("const", True)
        return None
    return None


def _flatten(node: exp.Expression, kind: type) -> list[exp.Expression]:
    out = []
    stack = [node]
    while stack:
        item = stack.pop(0)
        while isinstance(item, exp.Paren):
            item = item.this
        if isinstance(item, kind):
            stack = [item.left, item.right] + stack
        else:
            out.append(item)
    return out


def _case_findings(case: exp.Case) -> tuple[str, str] | None:
    """``(what, fix)`` for the first branch of a CASE that can never be reached, or ``None`` when every branch can."""

    subject = case.this
    key = _column_key(subject) if subject is not None else None
    branches: list[tuple[exp.Expression, Cond | None, str]] = []
    for position, branch in enumerate(case.args.get("ifs") or [], 1):
        condition = branch.this
        text = clip(condition.sql(dialect="bigquery"), 50)
        if subject is not None:
            when = _value(condition)
            if isinstance(condition, exp.Null):
                cond = Cond("const", None)
            elif key is not None and when is not None:
                cond = Cond("column", column=key, domain=_point(when))
            else:
                cond = None
            text = f"WHEN {text}"
        else:
            cond = _classify(condition)
        branches.append((branch, cond, text))
    earlier: list[tuple[int, Cond, str]] = []
    always_true: tuple[int, str] | None = None
    for position, (_branch, cond, text) in enumerate(branches, 1):
        if cond is not None:
            reason = None
            if always_true is not None:
                reason = f"branch {always_true[0]} ({always_true[1]}) is always true"
            elif cond.kind == "const" and cond.value is not True:
                reason = "its condition is " + ("NULL, which is never true" if cond.value is None else "always false")
            elif cond.kind == "column" and cond.domain.empty():
                reason = "its condition can never be true"
            elif cond.kind == "column":
                for index, other, other_text in earlier:
                    if other.column == cond.column and other.exact and _subset(cond.domain, other.domain):
                        reason = f"every row it matches is already taken by branch {index} ({other_text})"
                        break
            if reason is not None:
                return (f"CASE branch {position} ({text}) can never be reached: {reason}",
                        "Remove the branch, or fix its condition; check the order of the branches (the first true one wins).")
            if cond.kind == "const" and cond.value is True and cond.exact:
                always_true = (position, text)
            elif cond.kind == "column":
                earlier.append((position, cond, text))
        elif always_true is not None:
            return (f"CASE branch {position} ({text}) can never be reached: branch {always_true[0]} ({always_true[1]}) is always true",
                    "Remove the branch, or fix its condition; check the order of the branches (the first true one wins).")
    if case.args.get("default") is not None:
        if always_true is not None:
            return (f"CASE ELSE can never be reached: branch {always_true[0]} ({always_true[1]}) is always true",
                    "Remove the ELSE, or fix the branch before it.")
        nulls = [(i, c) for i, c, _t in earlier if c.exact and c.domain.nulls and not (c.domain.nonnull or c.domain.intervals or c.domain.points)]
        nonnull = [(i, c) for i, c, _t in earlier if c.exact and c.domain.nonnull and not (c.domain.nulls or c.domain.intervals or c.domain.points)]
        for _i, a in nulls:
            if any(b.column == a.column for _j, b in nonnull):
                return ("CASE ELSE can never be reached: the branches IS NULL and IS NOT NULL on the same column cover every row",
                        "Remove the ELSE, or drop one of the two branches.")
    return None


def _case_branches(ctx: Context) -> None:
    for case in ctx.tree.find_all(exp.Case):
        found = _case_findings(case)
        if found:
            ctx.find("case_branch", found[0], found[1])


# ----------------------------------------------------------- 7. FOR SYSTEM_TIME AS OF


def _system_time(ctx: Context) -> None:
    ctes = _ctes(ctx.tree)
    groups: dict[str, list[exp.Table]] = {}
    for table in ctx.tree.find_all(exp.Table):
        if not table.name or (_is_cte_reference(table) and _folded(table.name) in ctes):
            continue
        spelled = _spelled(table)
        key = ctx.tables.get(spelled) or spelled
        groups.setdefault(key, []).append(table)
    for key, tables in groups.items():
        timed = [t for t in tables if t.args.get("version") is not None]
        if timed and len(timed) < len(tables):
            name = tables[0].name
            ctx.find("system_time", f"{name} is read {len(tables)} times but FOR SYSTEM_TIME AS OF is on only {len(timed)} of them; the others read the current data",
                     f"Put the same FOR SYSTEM_TIME AS OF on every reference to {name}, or read it once in a CTE with the clause and use the CTE.")


# ------------------------------------------------------------------ 8. CTE shadows column


def _cte_shadow(ctx: Context) -> None:
    ctes = _ctes(ctx.tree)
    if not ctes:
        return
    unknown = 0
    reported: set[tuple[str, str]] = set()
    for select in ctx.tree.find_all(exp.Select):
        sources = _sources(select)
        unaliased = [s for s in sources if isinstance(s, exp.Table) and _is_cte_reference(s) and _folded(s.name) in ctes and not s.args.get("alias")]
        if not unaliased:
            continue
        bare = {}
        for column in select.find_all(exp.Column):
            if not column.table and not isinstance(column.this, exp.Star) and _owner_select(column) is select:
                bare.setdefault(_folded(column.name), column)
        for cte_table in unaliased:
            name = _folded(cte_table.name)
            if name not in bare:
                continue
            owners = []
            undecided = False
            for source in sources:
                if source is cte_table:
                    continue
                columns = _source_columns(ctx, source, ctes, 0)
                if columns is None:
                    undecided = True
                elif name in {_folded(c) for c in columns}:
                    owners.append(source)
            if owners and (name, _source_name(owners[0])) not in reported:
                reported.add((name, _source_name(owners[0])))
                ctx.find("cte_shadow", f"{cte_table.name} is both a CTE read in FROM and a column of {_source_name(owners[0])}; the unqualified {bare[name].name} reads the CTE's row (a STRUCT), not the column",
                         f"Qualify the column ({_source_name(owners[0])}.{bare[name].name}), alias the CTE in FROM, or rename the CTE.")
            elif not owners and undecided:
                unknown += 1
    if unknown:
        ctx.gap("CTE names that may shadow a column", "a table read next to the CTE has columns the project does not declare, so a column of the same name cannot be ruled out")


def _owner_select(node: exp.Expression) -> exp.Expression | None:
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


RULES: tuple[tuple[str, Callable[[Context], None]], ...] = (
    ("reserved_alias", _reserved_aliases),
    ("subquery", _subqueries),
    ("not_in_null", _not_in),
    ("limit_reread", _limit_reread),
    ("duplicate_column", _duplicate_columns),
    ("case_branch", _case_branches),
    ("system_time", _system_time),
    ("cte_shadow", _cte_shadow),
)
