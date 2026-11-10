"""Simpler equivalent forms of one BigQuery SELECT, for the table minimizer.

``simpler_forms(sql, columns)`` proposes rewrites of one query that score lower
on :func:`kumosql.formatting.complexity` (or equal, with shorter text): derived
tables and CTEs folded into the query that reads them, unmergeable derived
tables turned into CTEs, unused CTEs and joins dropped, and predicates
simplified. ``tidy(sql)`` gives the readable spelling every candidate comes
back in: no redundant quotes, no ``x AS x``, no ``m.t AS t`` and no column
qualifiers where a scope reads one source.

Candidates are equivalent by construction (bag semantics, the same output
names in the same order), but they are only proposals: the caller proves each
one before using it. The passes are sqlglot's optimizer passes (qualify,
pushdown_projections, merge_subqueries, eliminate_joins, eliminate_subqueries,
eliminate_ctes, simplify), :mod:`kumosql.query_optimizer`'s rewrite rules and
two folds of this module's own (a filter over a grouped or DISTINCT derived
table becomes ``HAVING`` or an inner ``WHERE``; ``SELECT * FROM t [WHERE p]``
folds without a column list), behind guards for what those passes get wrong:

* a query with a non-deterministic function (``RAND``, ``CURRENT_TIMESTAMP``,
  ``GENERATE_UUID``...) is not restructured, since folding can copy or move
  the call;
* a derived table or CTE on the NULL-extended side of an outer join that
  computes a value (``COALESCE(v, 0) AS v``, ``1 AS flag``) blocks merging,
  which would evaluate the expression on the NULL-extended rows;
* a column that ``qualify`` cannot attribute to a source blocks merging and
  projection pruning, since a folded source could capture it;
* a ``SELECT *`` body whose columns are unknown blocks merging (sqlglot
  merges it and leaves the outer references dangling);
* a candidate whose output names (case included) or order differ from the
  input's is dropped (``qualify`` lowercases identifiers, so their original
  spelling is restored first), as is one with a column qualifier naming no
  source, or a CTE named like a dataset (``WITH m AS`` changes ``m.t``).
"""

from __future__ import annotations

from collections import Counter
import logging
import re
import time
from typing import Callable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import star_modified

logging.getLogger("sqlglot").setLevel(logging.ERROR)

_DIALECT = "bigquery"
_SET_OPERATIONS = tuple(
    cls for cls in (getattr(exp, "SetOperation", None), exp.Union, exp.Intersect, exp.Except) if cls is not None
)
_VOLATILE_TYPES = tuple(
    cls for cls in (
        getattr(exp, name, None)
        for name in ("Rand", "Randn", "Uuid", "CurrentTimestamp", "CurrentDate", "CurrentTime", "CurrentDatetime", "CurrentUser", "TableSample")
    ) if cls is not None
)
_VOLATILE_NAMES = {
    "RAND", "RANDOM", "GENERATE_UUID", "UUID", "CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_DATETIME",
    "CURRENT_TIME", "SESSION_USER", "NOW",
}
# Unquoted, these read as something other than a column name.
_PSEUDO_KEYWORDS = {
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_DATETIME", "SESSION_USER", "CURRENT_USER",
    "TRUE", "FALSE", "NULL", "UNNEST", "STRUCT", "ARRAY", "INTERVAL", "DATE", "TIME", "TIMESTAMP", "DATETIME",
    "SAFE", "OFFSET", "ORDINAL", "SAFE_OFFSET", "SAFE_ORDINAL", "VALUE", "QUALIFY", "WINDOW",
}
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GENERATED = re.compile(r"_col_\d+\Z")
_BLOCKING_ARGS = ("distinct", "group", "having", "qualify", "limit", "offset", "windows")


# --------------------------------------------------------------------------- small helpers


def _from(select: exp.Expression):
    return select.args.get("from_") or select.args.get("from")


def _sources(select: exp.Select) -> list[exp.Expression]:
    from_ = _from(select)
    out = [from_.this] if from_ is not None and from_.this is not None else []
    out.extend(join.this for join in select.args.get("joins") or [])
    return out


def _is_star(item: exp.Expression) -> bool:
    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))


def _root_select(tree: exp.Expression) -> exp.Select | None:
    node = tree
    while isinstance(node, (exp.Subquery, *_SET_OPERATIONS)):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def _output_names(tree: exp.Expression) -> tuple | None:
    """Output names of the leftmost root SELECT, ``None`` for an unnamed item, a marker per star."""

    select = _root_select(tree)
    if select is None:
        return None
    names: list = [("kind", select.args.get("kind"))]
    for item in select.expressions:
        if _is_star(item):
            star = item.this if isinstance(item, exp.Column) else item
            names.append("*" + star.sql(dialect=_DIALECT))
        else:
            names.append(item.alias_or_name or None)
    return tuple(names)


def _volatile(tree: exp.Expression) -> bool:
    for node in tree.find_all(exp.Func, *_VOLATILE_TYPES):
        if isinstance(node, _VOLATILE_TYPES):
            return True
        name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
        if str(name).upper() in _VOLATILE_NAMES:
            return True
    return False


def _reserved() -> set[str]:
    try:
        from sqlglot.dialects.bigquery import BigQuery

        words = {str(w).upper() for w in getattr(BigQuery.Generator, "RESERVED_KEYWORDS", ())}
    except Exception:  # noqa: BLE001 - an older sqlglot without the list keeps every quote it can't vouch for
        return set()
    return words | _PSEUDO_KEYWORDS


def _parse(sql: str) -> exp.Expression | None:
    try:
        trees = [t for t in sqlglot.parse(sql, read=_DIALECT) if t is not None]
    except Exception:  # noqa: BLE001 - unparseable input has no candidates
        return None
    return trees[0] if len(trees) == 1 and isinstance(trees[0], exp.Query) else None


def _split_quoted_paths(tree: exp.Expression) -> exp.Expression:
    """```m.t``` is the path ``m.t``: give it its parts so sources resolve and print plainly."""

    for table in list(tree.find_all(exp.Table)):
        ident = table.this
        if table.args.get("db") or not isinstance(ident, exp.Identifier) or "." not in ident.name:
            continue
        parts = ident.name.split(".")
        if len(parts) not in (2, 3) or not all(parts):
            continue
        quoted = [not _PLAIN.match(p) for p in parts]
        table.set("this", exp.to_identifier(parts[-1], quoted=quoted[-1]))
        table.set("db", exp.to_identifier(parts[-2], quoted=quoted[-2]))
        if len(parts) == 3:
            table.set("catalog", exp.to_identifier(parts[0], quoted=quoted[0]))
    return tree


# --------------------------------------------------------------------------- tidy


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren) and not isinstance(node.this, exp.Or):
        return _flatten_and(node.this)
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _tidy_conditions(tree: exp.Expression) -> None:
    holders = [(n, "this") for n in tree.find_all(exp.Where, exp.Having, exp.Qualify)]
    holders += [(j, "on") for j in tree.find_all(exp.Join) if j.args.get("on") is not None]
    for holder, key in holders:
        condition = holder.args.get(key)
        if not isinstance(condition, (exp.And, exp.Paren)):
            continue
        parts = _flatten_and(condition)
        if len(parts) < 2:
            continue
        kept, seen = [], set()
        for part in parts:
            text = part.sql(dialect=_DIALECT)
            if text in seen and not _volatile(part):
                continue
            seen.add(text)
            kept.append(part)
        rebuilt = None
        for part in kept:
            part = part.copy()
            if isinstance(part, exp.Or):
                part = exp.Paren(this=part)
            rebuilt = part if rebuilt is None else exp.And(this=rebuilt, expression=part)
        if rebuilt.sql(dialect=_DIALECT) != condition.sql(dialect=_DIALECT):
            holder.set(key, rebuilt)


def _drop_table_aliases(tree: exp.Expression) -> None:
    """``m.orders AS orders`` reads as ``m.orders``: the implicit alias is the last path part."""

    for select in tree.find_all(exp.Select):
        sources = _sources(select)
        names = Counter(s.alias_or_name.lower() for s in sources)
        for source in sources:
            alias = source.args.get("alias")
            if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier) or alias is None:
                continue
            if alias.args.get("columns") or not alias.name:
                continue
            if alias.name.lower() == source.name.lower() and names[alias.name.lower()] == 1:
                source.set("alias", None)


def _strip_qualifiers(tree: exp.Expression) -> None:
    """``SELECT t.a FROM m.t`` reads as ``SELECT a FROM m.t`` when ``t`` is the scope's only source."""

    range_names = {s.alias_or_name.lower() for s in tree.find_all(exp.Table, exp.Subquery) if s.alias_or_name}
    range_names |= {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for select in list(tree.find_all(exp.Select)):
        sources = _sources(select)
        if len(sources) != 1 or select.args.get("laterals"):
            continue
        source = sources[0]
        if isinstance(source, exp.Table) and not isinstance(source.this, exp.Identifier):
            continue
        if not isinstance(source, (exp.Table, exp.Subquery)) or not source.alias_or_name:
            continue
        name = source.alias_or_name.lower()
        aliases = {e.alias.lower(): e.this for e in select.expressions if isinstance(e, exp.Alias)}
        for column in list(select.find_all(exp.Column)):
            if column.find_ancestor(exp.Select) is not select or not column.table:
                continue
            if column.args.get("db") or column.args.get("catalog") or column.table.lower() != name:
                continue
            if isinstance(column.this, exp.Star):
                if column.parent is select:
                    column.replace(column.this)
                continue
            cname = column.name.lower()
            if cname in range_names:
                continue  # an unqualified name that is also a range variable reads differently
            node = column
            while node.parent is not select:
                node = node.parent
            if node.arg_key in ("group", "having", "order", "qualify", "windows") and cname in aliases:
                target = aliases[cname]
                if not (isinstance(target, exp.Column) and target.name.lower() == cname and (target.table or name).lower() == name):
                    continue  # the bare name would read the select-list alias
            column.set("table", None)


def _drop_self_aliases(tree: exp.Expression) -> None:
    for alias in list(tree.find_all(exp.Alias)):
        inner = alias.this
        if isinstance(alias.parent, exp.Select) and isinstance(inner, exp.Column) and not isinstance(inner.this, exp.Star) \
                and alias.alias == inner.name:
            alias.replace(inner)


def _unquote(tree: exp.Expression, reserved: set[str]) -> None:
    for ident in tree.find_all(exp.Identifier):
        if ident.quoted and _PLAIN.match(ident.name) and ident.name.upper() not in reserved:
            ident.set("quoted", False)


def _unquote_paths(tree: exp.Expression) -> None:
    """```m.t``` prints as ``m.t`` unless ``m`` also names a CTE or range variable, where they differ."""

    names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    names |= {s.alias_or_name.lower() for s in tree.find_all(exp.Table, exp.Subquery) if s.alias_or_name}
    for table in tree.find_all(exp.Table):
        meta = getattr(table, "_meta", None) or {}
        if meta.get("quoted_table") and table.parts and table.parts[0].name.lower() not in names:
            meta.pop("quoted_table", None)


def _tidy_tree(tree: exp.Expression, unquote: bool = True) -> str:
    tree = _split_quoted_paths(tree)
    for step in (_tidy_conditions, _drop_table_aliases, _strip_qualifiers, _drop_self_aliases):
        try:
            step(tree)
        except Exception:  # noqa: BLE001 - a step that trips leaves the tree as the earlier steps made it
            pass
    if unquote:
        reserved = _reserved()
        if reserved:
            _unquote(tree, reserved)
            _unquote_paths(tree)
    return tree.sql(dialect=_DIALECT)


def _tidy_text(tree: exp.Expression) -> str | None:
    """Tidy ``tree`` (a copy is made) and check the text reads back as the same tree."""

    for unquote in (True, False):
        try:
            text = _tidy_tree(tree.copy(), unquote)
            again = sqlglot.parse_one(text, read=_DIALECT)
        except Exception:  # noqa: BLE001
            continue
        if again is not None and again.sql(dialect=_DIALECT) == text:
            return text
    return None


def tidy(sql: str, columns: Mapping[str, Sequence[str]] | None = None) -> str:
    """Readable form of a query: drop redundant quoting, ``x AS x`` aliases, table aliases equal to the
    table name, and column qualifiers when the scope has a single source. Same semantics, never raises
    (returns the input on failure)."""

    try:
        tree = _parse(sql)
        if tree is None:
            return sql
        text = _tidy_text(tree)
        if text is None or _output_names(sqlglot.parse_one(text, read=_DIALECT)) != _output_names(tree):
            return sql
        return text
    except Exception:  # noqa: BLE001
        return sql


# --------------------------------------------------------------------------- guards


def _schema(columns: Mapping[str, Sequence[str]] | None) -> dict | None:
    """sqlglot's nested schema from ``{"m.orders": [...]}``; one nesting depth only, the most common one."""

    if not columns:
        return None
    by_depth: dict[int, list] = {}
    for name, cols in columns.items():
        parts = [p for p in str(name).split(".")]
        if len(parts) not in (2, 3) or not all(parts) or not cols:
            continue
        by_depth.setdefault(len(parts), []).append((parts, cols))
    if not by_depth:
        return None
    depth = max(by_depth, key=lambda d: len(by_depth[d]))
    schema: dict = {}
    for parts, cols in by_depth[depth]:
        level = schema
        for part in parts[:-1]:
            level = level.setdefault(part, {})
        level[parts[-1]] = {str(c): "UNKNOWN" for c in cols}
    return schema


def _mergeable_body(body: exp.Expression) -> bool:
    if not isinstance(body, exp.Select):
        return False
    if any(body.args.get(arg) for arg in _BLOCKING_ARGS):
        return False
    return not any(e.find(exp.AggFunc, exp.Window) for e in body.expressions)


def _computes_values(source: exp.Expression, ctes: dict[str, exp.Expression], seen: set[int]) -> bool:
    """Whether folding ``source`` into an outer join's NULL-extended side could evaluate an expression there."""

    if isinstance(source, exp.Subquery):
        body = source.this
    elif isinstance(source, exp.Table):
        if source.args.get("db") or source.name.lower() not in ctes:
            return False
        body = ctes[source.name.lower()]
    elif isinstance(source, exp.Unnest):
        return False
    else:
        return True
    if id(body) in seen:
        return False
    seen.add(id(body))
    if not _mergeable_body(body):
        return False  # stays a derived table: its values are computed below the join
    for item in body.expressions:
        inner = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(inner, exp.Column):
            return True
    return any(_computes_values(s, ctes, seen) for s in _sources(body))


def _outer_join_hazard(tree: exp.Expression) -> bool:
    ctes = {cte.alias_or_name.lower(): cte.this for cte in tree.find_all(exp.CTE)}
    for select in tree.find_all(exp.Select):
        joins = select.args.get("joins") or []
        sources = _sources(select)
        if not joins or len(sources) != len(joins) + 1:
            continue
        for index, join in enumerate(joins, 1):
            side = (join.side or "").upper()
            nullable = []
            if side in ("LEFT", "FULL"):
                nullable.append(sources[index])
            if side in ("RIGHT", "FULL"):
                nullable.extend(sources[:index])
            if any(_computes_values(s, ctes, set()) for s in nullable):
                return True
    return False


def _unresolved(tree: exp.Expression) -> bool:
    """A column ``qualify`` left without a source: folding a source in could capture it."""

    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or column.table:
            continue
        select = column.find_ancestor(exp.Select)
        if select is None or _from(select) is None:
            continue
        if column.find_ancestor(exp.Order) is not None and any(
            isinstance(e, exp.Alias) and e.alias.lower() == column.name.lower() for e in select.expressions
        ):
            continue
        return True
    return False


# --------------------------------------------------------------------------- passes


def _call(fn: Callable, tree: exp.Expression, **kwargs) -> exp.Expression | None:
    try:
        return fn(tree.copy(), **kwargs)
    except TypeError:
        if not kwargs:
            return None
        try:
            return fn(tree.copy())
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001 - a pass this sqlglot can't run on this query is skipped
        return None


def _qualify(tree: exp.Expression, schema: dict | None) -> exp.Expression | None:
    try:
        from sqlglot.optimizer.qualify import qualify
    except Exception:  # noqa: BLE001
        return None
    return _call(
        qualify, tree, schema=schema, dialect=_DIALECT, validate_qualify_columns=False,
        quote_identifiers=False, identify=False,
    )


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _substitute(node: exp.Expression, alias: str, mapping: dict[str, exp.Expression]) -> exp.Expression | None:
    """``node`` with each column of ``alias`` replaced by the expression it names, or ``None``."""

    node = node.copy()
    for column in list(node.find_all(exp.Column)):
        if isinstance(column.this, exp.Star) or column.args.get("db") or (column.table and column.table.lower() != alias):
            return None
        target = mapping.get(column.name.lower())
        if target is None:
            return None
        target = target.copy()
        if not isinstance(target, (exp.Column, exp.Literal, exp.Func, exp.Paren)):
            target = exp.Paren(this=target)
        if column is node:
            return target
        column.replace(target)
    return node


def _fold_outer_filter(tree: exp.Expression) -> bool:
    """Fold a filter-and-project query over a grouped or DISTINCT derived table into it.

    ``SELECT k, n FROM (SELECT k, COUNT(*) AS n FROM t GROUP BY k) d WHERE n > 1`` is
    ``SELECT k, COUNT(*) AS n FROM t GROUP BY k HAVING COUNT(*) > 1``; over ``SELECT DISTINCT``
    the filter moves to the inner ``WHERE`` when the outer query keeps every column.
    """

    ctes = Counter(t.name.lower() for t in tree.find_all(exp.Table) if not t.args.get("db"))
    for outer in list(tree.find_all(exp.Select)):
        sources = _sources(outer)
        if len(sources) != 1 or outer.args.get("joins") or outer.args.get("laterals"):
            continue
        if any(outer.args.get(arg) for arg in ("group", "having", "distinct", "qualify", "windows", "order", "limit", "offset", "kind")):
            continue
        own = list(outer.expressions) + ([outer.args["where"]] if outer.args.get("where") else [])
        if any(part.find(exp.Select, exp.Subquery, exp.AggFunc, exp.Window) for part in own):
            continue
        if any(_is_star(e) for e in outer.expressions):
            continue
        source = sources[0]
        cte = None
        if isinstance(source, exp.Subquery):
            body = source.this
        elif isinstance(source, exp.Table) and not source.args.get("db") and ctes[source.name.lower()] == 1:
            found = [c for c in tree.find_all(exp.CTE) if c.alias_or_name.lower() == source.name.lower()]
            if len(found) != 1 or found[0].args.get("alias") is None or found[0].args["alias"].args.get("columns"):
                continue
            cte = found[0]
            if not isinstance(cte.parent, exp.With) or cte.parent.args.get("recursive") or _inside(source, cte):
                continue
            body = cte.this
        else:
            continue
        alias = (source.alias_or_name or "").lower()
        if not isinstance(body, exp.Select) or not alias or body.args.get("kind"):
            continue
        if any(body.args.get(arg) for arg in ("limit", "offset", "qualify", "windows", "order")):
            continue
        if any(_is_star(e) or e.find(exp.Window) for e in body.expressions):
            continue
        distinct = body.args.get("distinct")
        grouped = body.args.get("group") is not None or any(e.find(exp.AggFunc) for e in body.expressions)
        if distinct is not None and (distinct.args.get("on") is not None or grouped):
            continue
        if not grouped and distinct is None:
            continue  # a plain derived table is merge_subqueries' job
        mapping: dict[str, exp.Expression] = {}
        for item in body.expressions:
            name = item.alias_or_name.lower()
            if not name or name in mapping:
                mapping = {}
                break
            mapping[name] = item.this if isinstance(item, exp.Alias) else item
        if not mapping:
            continue
        if distinct is not None:
            # dropping a column from under DISTINCT changes multiplicities
            picked = set()
            for item in outer.expressions:
                inner = item.this if isinstance(item, exp.Alias) else item
                if not isinstance(inner, exp.Column):
                    picked = set()
                    break
                picked.add(inner.name.lower())
            if picked != set(mapping):
                continue
        projections = []
        for item in outer.expressions:
            name = item.alias_or_name
            value = _substitute(item.this if isinstance(item, exp.Alias) else item, alias, mapping)
            if value is None or not name:
                projections = []
                break
            if not (isinstance(value, exp.Column) and value.name == name):
                value = exp.alias_(value, name)
            projections.append(value)
        if not projections:
            continue
        where = outer.args.get("where")
        condition = _substitute(where.this, alias, mapping) if where is not None else None
        if where is not None and condition is None:
            continue
        folded = body.copy()
        folded.set("expressions", projections)
        if condition is not None:
            key, holder_type = ("where", exp.Where) if distinct is not None else ("having", exp.Having)
            existing = folded.args.get(key)
            if existing is not None:
                if isinstance(condition, exp.Or):
                    condition = exp.Paren(this=condition)
                left = existing.this if not isinstance(existing.this, exp.Or) else exp.Paren(this=existing.this)
                condition = exp.And(this=left, expression=condition)
            folded.set(key, holder_type(this=condition))
        if cte is not None:
            parent_with = cte.parent
            cte.pop()
            if not parent_with.expressions:
                parent_with.pop()
        with_ = outer.args.get("with_") or outer.args.get("with")
        if with_ is not None:
            for key in ("with_", "with"):
                if key in folded.arg_types:
                    folded.set(key, with_.copy())
                    break
        outer.replace(folded)
        return True
    return False


def _star_body(body: exp.Expression, allow_where: bool) -> exp.Table | None:
    """The base table of ``SELECT * FROM table [WHERE p]``, or ``None``."""

    if not isinstance(body, exp.Select) or len(body.expressions) != 1:
        return None
    if any(body.args.get(k) for k in ("joins", "laterals", "group", "having", "qualify", "windows", "order", "limit", "offset", "distinct", "kind", "with_", "with", "pivots")):
        return None
    if body.args.get("where") is not None and not allow_where:
        return None
    star = body.expressions[0]
    from_ = _from(body)
    source = from_.this if from_ is not None else None
    if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier) or source.args.get("db") is None:
        return None
    if isinstance(star, exp.Column) and isinstance(star.this, exp.Star) and star.table.lower() == source.alias_or_name.lower():
        star = star.this
    if not isinstance(star, exp.Star) or star_modified(star):
        return None
    alias = source.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return None
    return source


def _fold_star_sources(tree: exp.Expression) -> bool:
    """``(SELECT * FROM m.t [WHERE p]) AS d`` is ``m.t AS d`` (with ``p`` moved out), and so is a CTE of that form.

    Needs no column list, so it folds what ``merge_subqueries`` cannot when a table's columns are unknown.
    """

    for cte in list(tree.find_all(exp.CTE)):
        with_ = cte.parent
        if not isinstance(with_, exp.With) or with_.args.get("recursive") or cte.args["alias"].args.get("columns"):
            continue
        source = _star_body(cte.this, allow_where=False)
        name = cte.alias_or_name.lower()
        if source is None or source.parts[0].name.lower() == name:
            continue
        refs = [t for t in tree.find_all(exp.Table) if not t.args.get("db") and t.name.lower() == name and not _inside(t, cte)]
        if not refs or sum(1 for c in tree.find_all(exp.CTE) if c.alias_or_name.lower() == name) != 1:
            continue
        for ref in refs:
            replacement = source.copy()
            replacement.set("alias", exp.TableAlias(this=exp.to_identifier(ref.alias_or_name)))
            ref.replace(replacement)
        cte.pop()
        if not with_.expressions:
            with_.pop()
        return True
    for sub in list(tree.find_all(exp.Subquery)):
        holder = sub.parent
        outer = holder.parent if holder is not None else None
        if not isinstance(holder, (exp.From, exp.Join)) or not isinstance(outer, exp.Select) or sub.args.get("lateral"):
            continue
        alias = sub.args.get("alias")
        if alias is None or not alias.name or alias.args.get("columns"):
            continue
        single = isinstance(holder, exp.From) and not outer.args.get("joins") and not outer.args.get("laterals")
        source = _star_body(sub.this, allow_where=single)
        if source is None:
            continue
        where = sub.this.args.get("where")
        moved = None
        if where is not None:
            inner_name = source.alias_or_name.lower()
            moved = where.this.copy()
            if moved.find(exp.Select, exp.Subquery) or _volatile(moved):
                continue
            ok = True
            for column in moved.find_all(exp.Column):
                if column.args.get("db") or (column.table and column.table.lower() != inner_name):
                    ok = False
                    break
                if column.table:
                    column.set("table", exp.to_identifier(alias.name))
            if not ok:
                continue
        replacement = source.copy()
        replacement.set("alias", exp.TableAlias(this=exp.to_identifier(alias.name)))
        sub.replace(replacement)
        if moved is not None:
            existing = outer.args.get("where")
            if isinstance(moved, exp.Or):
                moved = exp.Paren(this=moved)
            if existing is not None:
                left = existing.this if not isinstance(existing.this, exp.Or) else exp.Paren(this=existing.this)
                moved = exp.And(this=left, expression=moved)
            outer.set("where", exp.Where(this=moved))
        return True
    return False


def _fold_star_chain(tree: exp.Expression) -> exp.Expression:
    root = exp.Subquery(this=tree)
    for _ in range(16):
        if not _fold_star_sources(root.this):
            break
    return root.this


def _fold_outer_filters(tree: exp.Expression) -> exp.Expression:
    root = exp.Subquery(this=tree)  # so the root query can be replaced in place
    for _ in range(8):
        if not _fold_outer_filter(root.this):
            break
    return root.this


def _optimizer_forms(tree: exp.Expression, schema: dict | None, volatile: bool) -> list[exp.Expression]:
    """sqlglot optimizer chains over one tree: folded, folded then CTE form, each simplified."""

    try:
        from sqlglot.optimizer.eliminate_ctes import eliminate_ctes
        from sqlglot.optimizer.eliminate_joins import eliminate_joins
        from sqlglot.optimizer.eliminate_subqueries import eliminate_subqueries
        from sqlglot.optimizer.merge_subqueries import merge_subqueries
        from sqlglot.optimizer.pushdown_projections import pushdown_projections
        from sqlglot.optimizer.simplify import simplify
    except Exception:  # noqa: BLE001
        return []

    qualified = _qualify(tree, schema)
    if qualified is None:
        return []

    def chain(node, *passes):
        for fn in passes:
            nxt = _call(fn, node)
            if nxt is not None:
                node = nxt
        return node

    out: list[exp.Expression] = []
    if volatile:
        # nothing moves: only an unused CTE goes
        out.append(chain(qualified, eliminate_ctes))
        return out
    try:
        qualified = _fold_star_chain(qualified.copy())
    except Exception:  # noqa: BLE001
        pass
    unresolved = _unresolved(qualified)
    # sqlglot merges a ``SELECT *`` body whose columns it does not know and leaves the outer references dangling
    inner_star = any(
        _is_star(e) for sel in qualified.find_all(exp.Select) if sel is not _root_select(qualified) for e in sel.expressions
    )
    can_merge = not unresolved and not inner_star and not _outer_join_hazard(qualified)
    folded = qualified
    if not unresolved:
        folded = chain(folded, pushdown_projections)
    if can_merge:
        folded = chain(folded, merge_subqueries)
    folded = chain(folded, eliminate_joins, eliminate_ctes)
    out.append(folded)
    try:
        having = _fold_outer_filters(folded.copy())
    except Exception:  # noqa: BLE001
        having = None
    if having is not None and having.sql(dialect=_DIALECT) != folded.sql(dialect=_DIALECT):
        having = chain(having, merge_subqueries, eliminate_ctes) if can_merge else having
        out.append(having)
        out.append(chain(having, eliminate_subqueries, eliminate_ctes))
    as_ctes = chain(folded, eliminate_subqueries)
    if can_merge:
        as_ctes = chain(as_ctes, merge_subqueries)
    out.append(chain(as_ctes, eliminate_ctes))
    for node in list(out):
        simple = _call(simplify, node, dialect=_DIALECT)
        if simple is not None:
            out.append(simple)
    return out


def _rule_forms(sql: str, columns: Mapping[str, Sequence[str]] | None, volatile: bool) -> list[exp.Expression]:
    """KumoSQL's own relational rewrite rules (``kumosql.query_optimizer``)."""

    if volatile:
        return []
    try:
        from .query_optimizer import Catalog, rewrite_candidates

        catalog = Catalog(columns={str(k).lower(): [str(c).lower() for c in v] for k, v in (columns or {}).items()})
        found = rewrite_candidates(sql, catalog, dialect=_DIALECT)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for candidate in found:
        tree = _parse(candidate.sql)
        if tree is not None:
            out.append(tree)
    return out


# --------------------------------------------------------------------------- finishing a candidate


def _case_map(tree: exp.Expression) -> dict[str, str]:
    spelling: dict[str, str] = {}
    for ident in tree.find_all(exp.Identifier):
        if isinstance(ident.parent, exp.Table) and ident.parent.args.get("db") is not None:
            continue
        spelling.setdefault(ident.name.lower(), ident.name)
    return spelling


def _restore_case(tree: exp.Expression, spelling: dict[str, str]) -> None:
    for ident in tree.find_all(exp.Identifier):
        if isinstance(ident.parent, exp.Table) and ident.parent.args.get("db") is not None:
            continue
        original = spelling.get(ident.name.lower())
        if original is not None and original != ident.name:
            ident.set("this", original)


def _drop_generated_aliases(tree: exp.Expression, sql: str) -> None:
    """``qualify`` names unnamed select items ``_col_0``; drop the names nothing reads."""

    read = {c.name.lower() for c in tree.find_all(exp.Column)}
    for alias in list(tree.find_all(exp.Alias)):
        name = alias.alias
        if isinstance(alias.parent, exp.Select) and _GENERATED.match(name) and name not in sql and name.lower() not in read:
            alias.replace(alias.this)


def _restore_root_names(tree: exp.Expression, reference: tuple) -> None:
    """Undo ``qualify``'s ``_col_0`` names and give each output its original spelling."""

    select = _root_select(tree)
    if select is None or len(select.expressions) != len(reference) - 1:
        return
    for item, name in zip(list(select.expressions), reference[1:]):
        if isinstance(item, exp.Alias) and name is None and _GENERATED.match(item.alias):
            item.replace(item.this)
        elif name and not name.startswith("*") and item.alias_or_name and item.alias_or_name != name \
                and item.alias_or_name.lower() == name.lower():
            if isinstance(item, exp.Alias):
                item.set("alias", exp.to_identifier(name))
            else:
                item.replace(exp.alias_(item.copy(), name))


def _collapse_stars(tree: exp.Expression, columns: Mapping[str, Sequence[str]] | None) -> None:
    """A select list naming every column of its only base table, in order, is that table's ``*``."""

    if not columns:
        return
    known = {str(k).lower(): [str(c) for c in v] for k, v in columns.items()}
    for select in tree.find_all(exp.Select):
        sources = _sources(select)
        if len(sources) != 1 or not isinstance(sources[0], exp.Table) or select.args.get("distinct"):
            continue
        table = sources[0]
        name = ".".join(p.name for p in table.parts).lower() if hasattr(table, "parts") else ""
        wanted = known.get(name)
        if not wanted or len(wanted) != len(select.expressions):
            continue
        ref = table.alias_or_name.lower()
        ok = True
        for item, col in zip(select.expressions, wanted):
            if isinstance(item, exp.Alias) and item.alias == col:
                item = item.this
            if not isinstance(item, exp.Column) or item.name != col or (item.table and item.table.lower() != ref):
                ok = False
                break
        if ok:
            select.set("expressions", [exp.Star()])


def _reference_names(tree: exp.Expression, schema: dict | None, spelling: dict[str, str], sql: str) -> set:
    """The output names a candidate may have: the input's, or the input's with its stars expanded."""

    names = _output_names(tree)
    accepted = {names}
    if names is not None and any(isinstance(n, str) and n.startswith("*") for n in names[1:]):
        qualified = _qualify(tree, schema)
        if qualified is not None:
            _restore_case(qualified, spelling)
            expanded = _output_names(qualified)
            if expanded is not None and not any(
                isinstance(n, str) and (n.startswith("*") or (_GENERATED.match(n) and n not in sql)) for n in expanded[1:]
            ):
                accepted.add(expanded)
    return accepted


def _score(sql: str):
    from .formatting import complexity

    try:
        return complexity(sql).score
    except Exception:  # noqa: BLE001 - sqlfluff can't read it
        return None


def simpler_forms(sql: str, columns: Mapping[str, Sequence[str]] | None = None, *, limit: int = 8) -> list[str]:
    """Candidate rewrites of one BigQuery SELECT that score lower (or equal with shorter text) on
    kumosql.formatting.complexity."""

    try:
        return _simpler_forms(sql, columns, limit)
    except Exception:  # noqa: BLE001 - a proposal generator never fails its caller
        return []


def _estimate(tree: exp.Expression) -> float:
    """A cheap stand-in for :func:`kumosql.formatting.complexity`, to pick which candidates to score."""

    def nesting(select: exp.Select) -> int:
        level, node = 1, select.parent
        while node is not None and not isinstance(node, exp.CTE):
            level += isinstance(node, exp.Select)
            node = node.parent
        return level

    selects = list(tree.find_all(exp.Select))
    nested = sum(1 for sel in selects if nesting(sel) > 1)
    depth = max((nesting(sel) for sel in selects), default=1)
    return (
        2 * len(list(tree.find_all(exp.Join)))
        + len(list(tree.find_all(exp.CTE)))
        + 3 * nested
        + 2 * len(list(tree.find_all(*_SET_OPERATIONS)))
        + len(list(tree.find_all(exp.Case)))
        + 2 * len(list(tree.find_all(exp.Window)))
        + 0.5 * len(list(tree.find_all(exp.And, exp.Or)))
        + 2 * (depth - 1)
    )


def _dangling(tree: exp.Expression) -> set[str]:
    """Qualifiers of columns that name no source of an enclosing SELECT (struct fields look like this too)."""

    out = set()
    for column in tree.find_all(exp.Column):
        if not column.table or column.args.get("db"):
            continue
        name = column.table.lower()
        node, found = column.parent, False
        while node is not None and not found:
            if isinstance(node, exp.Select):
                found = any(s.alias_or_name.lower() == name for s in _sources(node) + list(node.args.get("laterals") or []))
            node = node.parent
        if not found:
            out.add(name)
    return out


def _shadowed_paths(tree: exp.Expression) -> bool:
    """A CTE named like a dataset changes what ``dataset.table`` reads."""

    datasets = {t.parts[0].name.lower() for t in tree.find_all(exp.Table) if t.args.get("db") is not None}
    return any(c.alias_or_name.lower() in datasets for c in tree.find_all(exp.CTE))


_FINISHED = 6  # candidates tidied and checked, the most promising by estimate
_SCORED = 3  # sqlfluff parses cost ~30 ms each: score only the most promising candidates


def _simpler_forms(sql: str, columns, limit: int) -> list[str]:
    original = _parse(sql)
    if original is None or limit <= 0:
        return []
    original = _split_quoted_paths(original)
    schema = _schema(columns)
    volatile = _volatile(original)
    spelling = _case_map(original)
    reference = _output_names(original)
    if reference is None:
        return []
    accepted = _reference_names(original, schema, spelling, sql)
    had_star = any(_is_star(n) and not isinstance(n.parent, exp.Count) for n in original.find_all(exp.Star, exp.Column))
    shadowed = _shadowed_paths(original)
    dangling = _dangling(original)

    trees: list[exp.Expression] = []
    seen_trees: set[str] = {original.sql(dialect=_DIALECT)}

    def add(found: list[exp.Expression]) -> list[exp.Expression]:
        fresh = []
        for node in found:
            key = node.sql(dialect=_DIALECT)
            if key not in seen_trees:
                seen_trees.add(key)
                fresh.append(node)
        trees.extend(fresh)
        return fresh

    started = time.perf_counter()
    passes = add(_optimizer_forms(original, schema, volatile))
    rules = add(_rule_forms(sql, columns, volatile))
    # chain once: the sqlglot passes after the rules, and the rules after the best pass result
    chained = 2 if time.perf_counter() - started < 0.15 else 0  # a long query skips the second round
    for node in sorted(rules, key=_estimate)[:chained]:
        add(_optimizer_forms(node, schema, volatile))
    for node in sorted(passes, key=_estimate)[: min(chained, 1)]:
        add(_rule_forms(node.sql(dialect=_DIALECT), columns, volatile))

    found: list[tuple[float, int, int, str]] = []
    seen_texts = {sql}
    shapes: set = set()
    ranked = sorted(range(len(trees)), key=lambda i: (_estimate(trees[i]), i))[:_FINISHED]
    for node in [trees[i] for i in sorted(ranked)]:
        try:
            node = node.copy()
            _restore_case(node, spelling)
            _restore_root_names(node, reference)
            _drop_generated_aliases(node, sql)
            if had_star:
                _collapse_stars(node, columns)
            text = _tidy_text(node)
        except Exception:  # noqa: BLE001
            continue
        if text is None or text in seen_texts:
            continue
        seen_texts.add(text)
        parsed = _parse(text)
        if parsed is None or _output_names(parsed) not in accepted:
            continue
        if (not volatile and _volatile(parsed)) or (not shadowed and _shadowed_paths(parsed)) or not _dangling(parsed) <= dangling:
            continue
        estimate = _estimate(parsed)
        shape = (estimate, tuple(sorted(text.split())))
        if shape in shapes:
            continue  # the same query with its conjuncts or equality sides in another order
        shapes.add(shape)
        found.append((estimate, len(text), len(found), text))
    if not found:
        return []

    scoring = time.perf_counter()
    base_score = _score(sql)
    # each sqlfluff parse costs about what the input's did: score fewer candidates of a long query
    budget = max(1, min(_SCORED, int(0.2 / max(time.perf_counter() - scoring, 0.02))))
    base_length = len(tidy(sql, columns))
    if base_score is None:
        base_score = _score(tidy(sql, columns))
    scored = []
    for _, length, order, text in sorted(found)[:budget]:
        score = _score(text)
        if score is None:
            continue
        if base_score is not None and (score > base_score or (score == base_score and length >= base_length)):
            continue
        scored.append((score, length, order, text))
    scored.sort()  # ties keep generation order: the less rewritten form first
    return [text for *_, text in scored[:limit]]
