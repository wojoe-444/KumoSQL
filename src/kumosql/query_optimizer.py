"""Proof-gated query rewriting: simpler, cheaper SQL that is proven to return the same rows.

``optimize(sql, catalog)`` applies relational rewrite rules to one query and
returns the rewritten SQL only when KumoSQL's prover proves it equivalent to
the input (bag semantics, same columns in the same order). Anything the prover
cannot establish comes back as "no rewrite": an unproven rewrite is never
returned as safe.

The rules are general relational identities, each sound on its own terms:

* ``unused_cte``: drop a CTE nothing reads.
* ``passthrough``: a CTE or derived table that is only ``SELECT * FROM x`` is
  replaced by ``x``; a query that is only ``SELECT * FROM (q)`` becomes ``q``.
* ``one_row_join``: a cross join with a relation that always has exactly one
  row (an aggregate without ``GROUP BY``), none of whose columns are used
  except in conditions that are always true for it, is removed.
* ``predicates``: conditions that are always true (``x IS NULL OR x IS NOT
  NULL``, ``1 = 1``), repeated conjuncts and repeated ``IN`` list items are
  removed.
* ``merge_filter``: an inner-joined ``(SELECT * FROM t WHERE p) a`` becomes
  ``t AS a`` with ``p`` moved to the outer ``WHERE``.
* ``inline_cte``: a CTE read once is written in place.

Rules only propose; the prover decides.
"""

from __future__ import annotations

from collections import Counter
import contextlib
from dataclasses import dataclass, field
import logging
import signal
import threading
import time

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import distinct_on, extended_grouping, plain_distinct
from kumo_sql_prover.ast_utils import select_sources as _sources, star_modified

logging.getLogger("sqlglot").setLevel(logging.ERROR)


@dataclass
class Catalog:
    """Columns, NOT NULL columns and keys of the tables a query may read."""

    columns: dict[str, list[str]] = field(default_factory=dict)
    types: dict[str, dict[str, str]] = field(default_factory=dict)
    not_null: dict[str, set[str]] = field(default_factory=dict)
    keys: dict[str, list[tuple[str, ...]]] = field(default_factory=dict)

    @classmethod
    def from_schema_profile(cls, profile: dict) -> "Catalog":
        """Read a SQL-RewriteBench ``schema_profile.yaml`` (already parsed)."""

        catalog = cls()
        for table in profile.get("tables") or []:
            name = str(table["name"]).lower()
            cols = sorted(table.get("columns") or [], key=lambda c: c.get("ordinal_position") or 0)
            catalog.columns[name] = [str(c["column_name"]).lower() for c in cols]
            catalog.types[name] = {str(c["column_name"]).lower(): str(c.get("data_type") or "") for c in cols}
            catalog.not_null[name] = {
                str(c["column_name"]).lower() for c in cols if str(c.get("is_nullable")).upper() == "NO"
            }
            key = tuple(str(k).lower() for k in table.get("primary_key") or [])
            catalog.keys[name] = [key] if key else []
        return catalog

    def constraints(self) -> dict:
        from kumo_sql_prover.smt_equivalence import TableConstraints

        return {
            name: TableConstraints(
                not_null=frozenset(self.not_null.get(name, ())),
                keys=tuple(k for k in self.keys.get(name, []) if k),
            )
            for name in self.columns
        }


@dataclass
class Outcome:
    sql: str | None
    reason: str
    steps: tuple[str, ...] = ()
    seconds: float = 0.0


# --------------------------------------------------------------------------- helpers


def _arg(node: exp.Expression, *keys: str):
    for key in keys:
        value = node.args.get(key)
        if value is not None:
            return value
    return None


def _from(select: exp.Expression):
    return _arg(select, "from_", "from")


def _with(query: exp.Expression):
    return _arg(query, "with_", "with")


def _set(node: exp.Expression, key: str, value) -> None:
    """Set ``from``/``with`` under whichever spelling this sqlglot version uses."""

    if key + "_" in node.arg_types:
        key = key + "_"
    node.set(key, value)


_CLAUSES = ("where", "group", "having", "qualify", "order", "limit", "offset", "distinct", "windows", "joins", "with_", "with", "laterals", "pivots", "sample", "into", "locks", "connect", "match", "prewhere", "cluster", "distribute", "sort", "kind", "hint", "operation_modifiers")


def _only(select: exp.Expression, allowed: set[str]) -> bool:
    return all(not select.args.get(k) or k in allowed for k in _CLAUSES)


def _alias(node: exp.Expression) -> str | None:
    alias = node.args.get("alias")
    if isinstance(alias, exp.TableAlias) and alias.this is not None:
        return alias.name.lower()
    return None


def _alias_has_columns(node: exp.Expression) -> bool:
    alias = node.args.get("alias")
    return isinstance(alias, exp.TableAlias) and bool(alias.args.get("columns"))


def _is_star_list(select: exp.Select, source_name: str | None) -> bool:
    exprs = select.expressions
    if len(exprs) != 1:
        return False
    item = exprs[0]
    if star_modified(item):
        return False  # ``* EXCEPT / REPLACE ..`` lists other columns than its source
    if isinstance(item, exp.Star):
        return True
    return (
        isinstance(item, exp.Column)
        and isinstance(item.this, exp.Star)
        and source_name is not None
        and item.table.lower() == source_name
        and not item.args.get("db")
    )


def _source_name(source: exp.Expression) -> str | None:
    if isinstance(source, exp.Table):
        return _alias(source) or source.name.lower()
    return _alias(source)


def _passthrough_source(query: exp.Expression) -> exp.Expression | None:
    """``x`` when ``query`` is exactly ``SELECT * FROM x`` (nothing else)."""

    if not isinstance(query, exp.Select) or not _only(query, set()):
        return None
    from_ = _from(query)
    if from_ is None:
        return None
    source = from_.this
    if not isinstance(source, (exp.Table, exp.Subquery)) or _alias_has_columns(source):
        return None
    if isinstance(source, exp.Table) and (source.args.get("db") or not isinstance(source.this, exp.Identifier)):
        return None
    if isinstance(source, exp.Subquery) and (source.args.get("lateral") or not isinstance(source.this, exp.Query)):
        return None
    if not _is_star_list(query, _source_name(source)):
        return None
    return source


def _cte_name_counts(tree: exp.Expression) -> Counter:
    return Counter(cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE))


def _cte_refs(scope: exp.Expression, name: str, cte: exp.CTE) -> list[exp.Table]:
    """Table references to ``name`` inside ``scope`` (outside ``cte``'s own body)."""

    refs = []
    for table in scope.find_all(exp.Table):
        if table.args.get("db") or table.args.get("catalog") or table.name.lower() != name:
            continue
        if not isinstance(table.parent, (exp.From, exp.Join)):
            return [table, table]  # used in an unusual position: treat as many references
        node = table
        inside = False
        while node is not None and node is not scope:
            if node is cte:
                inside = True
                break
            node = node.parent
        if not inside:
            refs.append(table)
    return refs


def _with_scope(with_: exp.With) -> exp.Expression:
    return with_.parent


def _replace_table(table: exp.Table, replacement: exp.Expression, alias: str) -> None:
    replacement = replacement.copy()
    replacement.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    table.replace(replacement)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _and_all(items: list[exp.Expression]) -> exp.Expression | None:
    if not items:
        return None
    out = items[0]
    for item in items[1:]:
        out = exp.and_(out, item, copy=False)
    return out


def _set_where(select: exp.Select, conjuncts: list[exp.Expression]) -> None:
    condition = _and_all([c.copy() for c in conjuncts])
    select.set("where", exp.Where(this=condition) if condition is not None else None)


_VOLATILE = {"RANDOM", "RAND", "NOW", "CLOCK_TIMESTAMP", "NEXTVAL", "SETSEED", "GEN_RANDOM_UUID", "UUID", "TIMEOFDAY", "STATEMENT_TIMESTAMP", "TXID_CURRENT"}


def _volatile(node: exp.Expression) -> bool:
    for f in node.find_all(exp.Func):
        name = (f.sql_name() if not isinstance(f, exp.Anonymous) else f.name).upper()
        if name in _VOLATILE or isinstance(f, (exp.Rand, exp.CurrentTimestamp)):
            return True
    return False


# --------------------------------------------------------------------------- rules


def rule_unused_cte(tree: exp.Expression) -> bool:
    counts = _cte_name_counts(tree)
    changed = False
    for with_ in list(tree.find_all(exp.With)):
        if with_.args.get("recursive"):
            continue
        scope = _with_scope(with_)
        for cte in list(with_.expressions):
            name = cte.alias_or_name.lower()
            if counts[name] != 1 or _volatile(cte.this):
                continue
            if not _cte_refs(scope, name, cte):
                cte.pop()
                changed = True
        if not with_.expressions:
            with_.pop()
    return changed


def rule_passthrough_cte(tree: exp.Expression) -> bool:
    counts = _cte_name_counts(tree)
    for with_ in list(tree.find_all(exp.With)):
        if with_.args.get("recursive"):
            continue
        scope = _with_scope(with_)
        for cte in with_.expressions:
            name = cte.alias_or_name.lower()
            source = _passthrough_source(cte.this)
            if source is None or counts[name] != 1 or _alias_has_columns(cte):
                continue
            if isinstance(source, exp.Table) and counts[source.name.lower()] > 1:
                continue
            refs = _cte_refs(scope, name, cte)
            if any(_alias_has_columns(r) for r in refs) or len(set(map(id, refs))) != len(refs):
                continue
            for ref in refs:
                bare = source.copy()
                bare.set("alias", None)
                _replace_table(ref, bare, _alias(ref) or name)
            cte.pop()
            if not with_.expressions:
                with_.pop()
            return True
    return False


def rule_passthrough_derived(tree: exp.Expression) -> bool:
    for sub in list(tree.find_all(exp.Subquery)):
        if not isinstance(sub.parent, (exp.From, exp.Join)) or sub.args.get("lateral") or _alias_has_columns(sub):
            continue
        source = _passthrough_source(sub.this)
        if source is None:
            continue
        alias = _alias(sub)
        bare = source.copy()
        if alias is None:
            alias = _source_name(source)
            if alias is None:
                continue
        bare.set("alias", None)
        _replace_table(sub, bare, alias)
        return True
    return False


def _merge_with(outer: exp.With | None, inner: exp.With | None) -> exp.With | None:
    if outer is None:
        return inner
    if inner is None:
        return outer
    if outer.args.get("recursive") or inner.args.get("recursive"):
        return None
    names = {c.alias_or_name.lower() for c in outer.expressions}
    if any(c.alias_or_name.lower() in names for c in inner.expressions):
        return None
    return exp.With(expressions=[c.copy() for c in outer.expressions] + [c.copy() for c in inner.expressions])


def rule_unwrap_root(tree: exp.Expression) -> exp.Expression | None:
    """The root ``SELECT * FROM x`` becomes ``x``'s query (a new tree), or ``None``."""

    source = _passthrough_source(_strip_with(tree))
    if source is None:
        return None
    outer_with = _with(tree)
    if isinstance(source, exp.Subquery):
        body = source.this.copy()
        merged = _merge_with(outer_with.copy() if outer_with else None, _with(body))
        if merged is None and (outer_with or _with(body)):
            return None
        _set(body, "with", merged)
        return body
    if outer_with is None:
        return None
    name = source.name.lower()
    counts = _cte_name_counts(tree)
    target = next((c for c in outer_with.expressions if c.alias_or_name.lower() == name), None)
    if target is None or counts[name] != 1 or _alias_has_columns(target) or _alias_has_columns(source):
        return None
    if len(_cte_refs(tree, name, target)) != 1:
        return None
    body = target.this.copy()
    rest = exp.With(expressions=[c.copy() for c in outer_with.expressions if c is not target]) if len(outer_with.expressions) > 1 else None
    merged = _merge_with(rest, _with(body))
    if merged is None and (rest or _with(body)):
        return None
    _set(body, "with", merged)
    return body


def _strip_with(query: exp.Expression) -> exp.Expression:
    if _with(query) is None:
        return query
    copy = query.copy()
    _set(copy, "with", None)
    return copy


_COUNTS = (exp.Count,)


def _one_row_relation(source: exp.Expression, tree: exp.Expression) -> dict[str, str] | None:
    """Column name -> kind (``count`` or ``other``) when ``source`` always has exactly one row."""

    body = None
    if isinstance(source, exp.Subquery) and not source.args.get("lateral"):
        body = source.this
    elif isinstance(source, exp.Table) and not source.args.get("db"):
        name = source.name.lower()
        if _cte_name_counts(tree)[name] != 1:
            return None
        cte = next((c for c in tree.find_all(exp.CTE) if c.alias_or_name.lower() == name), None)
        if cte is None or _alias_has_columns(cte):
            return None
        body = cte.this
    if not isinstance(body, exp.Select) or _alias_has_columns(source):
        return None
    if not _only(body, {"where", "joins", "with_", "with"}) or _volatile(body):
        return None
    columns: dict[str, str] = {}
    for item in body.expressions:
        inner = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(inner, exp.AggFunc) or inner.find(exp.Window):
            return None
        columns[item.alias_or_name.lower()] = "count" if isinstance(inner, _COUNTS) else "other"
    return columns


def _always_true_for(conjunct: exp.Expression, alias: str, columns: dict[str, str]) -> bool:
    """``alias.c >= 0`` (or ``> -k``, ``IS NOT NULL``) for a COUNT column ``c``."""

    node = conjunct.unnest() if isinstance(conjunct, exp.Paren) else conjunct

    def count_col(e):
        return (
            isinstance(e, exp.Column)
            and e.table.lower() == alias
            and columns.get(e.name.lower()) == "count"
        )

    def number(e):
        if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal) and not e.this.is_string:
            return -float(e.this.this)
        if isinstance(e, exp.Literal) and not e.is_string:
            return float(e.this)
        return None

    if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and count_col(node.this.this) and isinstance(node.this.expression, exp.Null):
        return True
    if isinstance(node, exp.Is) and node.args.get("negate") and count_col(node.this) and isinstance(node.expression, exp.Null):
        return True
    pairs = {exp.GTE: lambda v: v <= 0, exp.GT: lambda v: v < 0}
    flipped = {exp.LTE: lambda v: v <= 0, exp.LT: lambda v: v < 0}
    for kind, ok in pairs.items():
        if isinstance(node, kind) and count_col(node.this):
            v = number(node.expression)
            return v is not None and ok(v)
    for kind, ok in flipped.items():
        if isinstance(node, kind) and count_col(node.expression):
            v = number(node.this)
            return v is not None and ok(v)
    return False


def rule_one_row_join(tree: exp.Expression) -> bool:
    for select in list(tree.find_all(exp.Select)):
        joins = select.args.get("joins") or []
        from_ = _from(select)
        if from_ is None or not joins:
            continue
        items = [(from_, from_.this)] + [(j, j.this) for j in joins]
        if any(isinstance(j, exp.Join) and (j.side or j.args.get("on") or j.args.get("using") or (j.kind or "").upper() not in ("", "CROSS", "INNER")) for j, _ in items[1:]):
            continue
        if any(isinstance(e, exp.Star) for e in select.expressions):
            continue
        for holder, source in items:
            alias = _source_name(source)
            if alias is None or sum(_source_name(s) == alias for _, s in items) != 1:
                continue
            columns = _one_row_relation(source, tree)
            if columns is None:
                continue
            where = select.args.get("where")
            conjuncts = _conjuncts(where.this) if where else []
            kept = [c for c in conjuncts if not _always_true_for(c, alias, columns)]
            # Any other mention of the alias (select list, other clauses, subqueries) blocks removal.
            dropped_ids = {id(n) for c in conjuncts if c not in kept for n in c.walk()}
            used = False
            for col in select.find_all(exp.Column):
                if id(col) in dropped_ids:
                    continue
                if col.table.lower() == alias or (not col.table and col.name.lower() in columns):
                    used = True
                    break
            if used:
                continue
            if holder is from_:
                first = joins[0]
                from_.set("this", first.this.copy())
                first.pop()
            else:
                holder.pop()
            _set_where(select, kept)
            return True
    return False


def _is_tautology(node: exp.Expression) -> bool:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Boolean) and node.this is True:
        return True
    if isinstance(node, exp.EQ) and isinstance(node.this, exp.Literal) and isinstance(node.expression, exp.Literal):
        return node.this.this == node.expression.this and node.this.is_string == node.expression.is_string
    if isinstance(node, exp.Or):
        a, b = (x.unnest() if isinstance(x, exp.Paren) else x for x in (node.this, node.expression))

        def null_test(e):
            if isinstance(e, exp.Is) and isinstance(e.expression, exp.Null):
                return e.this, bool(e.args.get("negate"))
            if isinstance(e, exp.Not) and isinstance(e.this, exp.Is) and isinstance(e.this.expression, exp.Null):
                return e.this.this, True
            return None

        ta, tb = null_test(a), null_test(b)
        if ta and tb and ta[1] != tb[1] and ta[0] == tb[0] and not _volatile(ta[0]):
            return True
        return _is_tautology(a) or _is_tautology(b)
    return False


def rule_predicates(tree: exp.Expression) -> bool:
    changed = False
    for node in list(tree.find_all(exp.Where, exp.Having)):
        conjuncts = _conjuncts(node.this)
        kept: list[exp.Expression] = []
        seen: set[str] = set()
        for c in conjuncts:
            key = c.sql(dialect="postgres")
            if _is_tautology(c) or (key in seen and not _volatile(c)):
                continue
            seen.add(key)
            kept.append(c)
        if len(kept) != len(conjuncts):
            changed = True
            if kept:
                node.set("this", _and_all([k.copy() for k in kept]))
            else:
                node.pop()
    for in_ in list(tree.find_all(exp.In)):
        values = in_.expressions
        if not values or in_.args.get("query"):
            continue
        keys = [v.sql(dialect="postgres") for v in values]
        if len(set(keys)) == len(keys) or not all(isinstance(v, exp.Literal) for v in values):
            continue
        unique, seen_keys = [], set()
        for v, k in zip(values, keys):
            if k not in seen_keys:
                seen_keys.add(k)
                unique.append(v.copy())
        in_.set("expressions", unique)
        changed = True
    return changed


def _resolve_into(pred: exp.Expression, inner_name: str, new_alias: str, columns: list[str] | None) -> exp.Expression | None:
    """Copy ``pred`` with its columns qualified by ``new_alias``; ``None`` if a column is not the source's."""

    pred = pred.copy()
    if pred.find(exp.Subquery, exp.Select):
        return None
    for col in list(pred.find_all(exp.Column)):
        if isinstance(col.this, exp.Star):
            return None
        if col.table and col.table.lower() != inner_name:
            return None
        if columns is not None and col.name.lower() not in columns:
            return None
        col.set("table", exp.to_identifier(new_alias))
    return pred


def rule_merge_filter(tree: exp.Expression, catalog: Catalog) -> bool:
    for sub in list(tree.find_all(exp.Subquery)):
        parent = sub.parent
        if not isinstance(parent, (exp.From, exp.Join)) or sub.args.get("lateral") or _alias_has_columns(sub):
            continue
        if isinstance(parent, exp.Join) and (parent.side or (parent.kind or "").upper() not in ("", "INNER", "CROSS")):
            continue
        outer = parent.parent
        if not isinstance(outer, exp.Select):
            continue
        body = sub.this
        if not isinstance(body, exp.Select) or not _only(body, {"where"}):
            continue
        from_ = _from(body)
        if from_ is None or not isinstance(from_.this, exp.Table) or from_.this.args.get("db") or _alias_has_columns(from_.this):
            continue
        source = from_.this
        if not _is_star_list(body, _source_name(source)) or _volatile(body):
            continue
        alias = _alias(sub)
        if alias is None:
            continue
        table_name = source.name.lower()
        is_cte = _cte_name_counts(tree)[table_name] > 0
        if is_cte and _cte_name_counts(tree)[table_name] != 1:
            continue
        columns = None if is_cte else catalog.columns.get(table_name)
        if not is_cte and columns is None:
            continue
        where = body.args.get("where")
        moved = []
        for c in _conjuncts(where.this) if where else []:
            m = _resolve_into(c, _source_name(source), alias, columns)
            if m is None:
                break
            moved.append(m)
        else:
            if any(_source_name(s) == alias for s in _sources(outer) if s is not sub):
                continue
            bare = source.copy()
            bare.set("alias", None)
            _replace_table(sub, bare, alias)
            if moved:
                outer_where = outer.args.get("where")
                _set_where(outer, (_conjuncts(outer_where.this) if outer_where else []) + moved)
            return True
    return False


def rule_inline_cte(tree: exp.Expression) -> bool:
    counts = _cte_name_counts(tree)
    for with_ in list(tree.find_all(exp.With)):
        if with_.args.get("recursive"):
            continue
        scope = _with_scope(with_)
        for cte in with_.expressions:
            name = cte.alias_or_name.lower()
            if counts[name] != 1 or _alias_has_columns(cte) or _volatile(cte.this):
                continue
            refs = _cte_refs(scope, name, cte)
            if len(refs) != 1 or len(set(map(id, refs))) != 1 or _alias_has_columns(refs[0]):
                continue
            ref = refs[0]
            # A later CTE body referencing this one keeps its scope; only inline into the main query or later CTEs.
            body = cte.this.copy()
            _replace_table(ref, exp.Subquery(this=body), _alias(ref) or name)
            cte.pop()
            if not with_.expressions:
                with_.pop()
            return True
    return False


def rule_drop_inner_order(tree: exp.Expression) -> bool:
    """ORDER BY without LIMIT inside a derived table or CTE orders nothing anyone can see."""

    for node in list(tree.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except)):
        if node is tree or not node.args.get("order") or node.args.get("limit") or node.args.get("offset"):
            continue
        holder = node.parent
        if not isinstance(holder, (exp.Subquery, exp.CTE)):
            continue
        if distinct_on(node):
            continue  # DISTINCT ON keeps the first row in this order (a set operation's DISTINCT is a bool)
        node.set("order", None)
        return True
    return False


def _has_agg_or_window(node: exp.Expression) -> bool:
    return any(isinstance(n, (exp.AggFunc, exp.Window)) for n in node.walk())


def rule_merge_projection(tree: exp.Expression) -> bool:
    """``SELECT f(x.c) FROM (SELECT e AS c FROM ... WHERE p) x`` becomes ``SELECT f(e) FROM ... WHERE p``."""

    for sub in list(tree.find_all(exp.Subquery)):
        parent = sub.parent
        if not isinstance(parent, exp.From) or sub.args.get("lateral") or _alias_has_columns(sub):
            continue
        outer = parent.parent
        if not isinstance(outer, exp.Select) or outer.args.get("joins") or outer.args.get("laterals"):
            continue
        body = sub.this
        if not isinstance(body, exp.Select) or not _only(body, {"where", "joins"}) or _from(body) is None:
            continue
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in body.expressions):
            continue
        if _has_agg_or_window(body) or _volatile(body) or any(e.find(exp.Subquery, exp.Select) for e in body.expressions):
            continue
        mapping: dict[str, exp.Expression] = {}
        for item in body.expressions:
            name = item.alias_or_name.lower()
            if not name or name in mapping:
                mapping = {}
                break
            mapping[name] = item.this if isinstance(item, exp.Alias) else item
        if not mapping:
            continue
        alias = _alias(sub)
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in outer.expressions):
            continue
        if any(n is not body and isinstance(n, exp.Select) and not _inside(n, body) for n in outer.walk() if n is not outer):
            continue
        output_names = {e.alias_or_name.lower() for e in outer.expressions if isinstance(e, exp.Alias)}
        columns = [c for c in outer.find_all(exp.Column) if not _inside(c, body)]
        ok = True
        uses: Counter = Counter()
        for col in columns:
            name = col.name.lower()
            if col.table and col.table.lower() != alias:
                ok = False
                break
            if name not in mapping:
                if col.table or name not in output_names:
                    ok = False
                    break
                continue
            if not col.table and name in output_names and not isinstance(mapping[name], exp.Column):
                ok = False  # an output alias and an input column share the name
                break
            uses[name] += 1
        if not ok or any(uses[n] > 1 and not isinstance(mapping[n], (exp.Column, exp.Literal)) for n in uses):
            continue
        for index, item in enumerate(list(outer.expressions)):
            if isinstance(item, exp.Column) and item.name.lower() in mapping:
                target = mapping[item.name.lower()]
                if not (isinstance(target, exp.Column) and target.name.lower() == item.name.lower()):
                    item.replace(exp.alias_(item.copy(), item.name, quoted=item.this.args.get("quoted")))
        for col in [c for c in outer.find_all(exp.Column) if not _inside(c, body)]:
            name = col.name.lower()
            if name not in mapping or (col.table and col.table.lower() != alias):
                continue
            if not col.table and name in output_names and isinstance(col.parent, exp.Ordered):
                continue
            target = mapping[name].copy()
            col.replace(target if isinstance(target, (exp.Column, exp.Literal)) else exp.Paren(this=target))
        inner_where = body.args.get("where")
        outer_where = outer.args.get("where")
        _set(outer, "from", _from(body).copy())
        outer.set("joins", [j.copy() for j in body.args.get("joins") or []] or None)
        _set_where(outer, (_conjuncts(inner_where.this) if inner_where else []) + (_conjuncts(outer_where.this) if outer_where else []))
        return True
    return False


def _inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def rule_shared_sums(tree: exp.Expression) -> bool:
    """``SUM(x + 1), SUM(x + 2), ...`` share one ``SUM(x)`` and one ``COUNT(x)``.

    Applied only where one select list sums the same column with two or more
    different constants, so the rewrite replaces many aggregates by two.
    """

    def split(node: exp.Sum):
        arg = node.this.unnest() if isinstance(node.this, exp.Paren) else node.this
        if isinstance(arg, (exp.Add, exp.Sub)) and isinstance(arg.this, exp.Column) and isinstance(arg.expression, exp.Literal) and not arg.expression.is_string:
            return arg.this, arg.expression, isinstance(arg, exp.Sub)
        return None

    for select in tree.find_all(exp.Select):
        sums = [
            s for s in select.find_all(exp.Sum)
            if s.find_ancestor(exp.Select) is select and not isinstance(s.parent, exp.Window) and not isinstance(s.this, exp.Distinct) and split(s)
        ]
        by_column = Counter(split(s)[0].sql() for s in sums)
        targets = [s for s in sums if by_column[split(s)[0].sql()] >= 2]
        if not targets:
            continue
        for node in targets:
            column, constant, minus = split(node)
            shift = exp.Mul(this=constant.copy(), expression=exp.Count(this=column.copy()))
            total = (exp.Sub if minus else exp.Add)(this=exp.Sum(this=column.copy()), expression=shift)
            # A bare SUM(...) keeps its values; only the engine-assigned column name ("sum") can change.
            node.replace(exp.Paren(this=total))
        return True
    return False


def rule_constant_group_keys(tree: exp.Expression) -> bool:
    """A GROUP BY item that names a constant select item groups nothing."""

    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if group is None or len(group.expressions) < 2:
            continue
        keep = []
        for item in group.expressions:
            target = item
            if isinstance(item, exp.Literal) and not item.is_string and item.this.isdigit():
                index = int(item.this) - 1
                if 0 <= index < len(select.expressions):
                    target = select.expressions[index]
                    target = target.this if isinstance(target, exp.Alias) else target
                else:
                    keep.append(item)
                    continue
                if isinstance(target, exp.Literal):
                    continue
            keep.append(item)
        if 0 < len(keep) < len(group.expressions):
            group.set("expressions", keep)
            return True
    return False


# --------------------------------------------------------------------------- search


RULES = (
    ("unused_cte", lambda t, c: rule_unused_cte(t)),
    ("passthrough", lambda t, c: rule_passthrough_cte(t) or rule_passthrough_derived(t)),
    ("one_row_join", lambda t, c: rule_one_row_join(t)),
    ("predicates", lambda t, c: rule_predicates(t)),
    ("merge_filter", rule_merge_filter),
    ("merge_projection", lambda t, c: rule_merge_projection(t)),
    ("inner_order", lambda t, c: rule_drop_inner_order(t)),
    ("shared_sums", lambda t, c: rule_shared_sums(t)),
    ("constant_group_keys", lambda t, c: rule_constant_group_keys(t)),
)


def _apply(tree: exp.Expression, catalog: Catalog, rules, steps: list[str], snapshots: list[str], dialect: str, limit: int = 200) -> exp.Expression:
    """Apply ``rules`` to a fixpoint, recording each step's name and resulting SQL."""

    for _ in range(limit):
        for name, rule in rules:
            if rule(tree, catalog):
                steps.append(name)
                break
        else:
            unwrapped = rule_unwrap_root(tree)
            if unwrapped is None:
                return tree
            tree = unwrapped
            steps.append("passthrough")
        snapshots.append(tree.sql(dialect=dialect, pretty=True))
    return tree


@dataclass
class Candidate:
    sql: str
    steps: tuple[str, ...]
    snapshots: tuple[str, ...]  # the statement after each step; the last is ``sql``


def rewrite_candidates(sql: str, catalog: Catalog, dialect: str = "postgres") -> list[Candidate]:
    """Distinct rewritten statements, most rewritten first; may be empty."""

    tree = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(tree, exp.Query):
        return []
    out: list[Candidate] = []
    seen = {_key(tree.sql(dialect=dialect))}
    for extra in (True, False):
        steps: list[str] = []
        snapshots: list[str] = []
        rules = RULES + ((("inline_cte", lambda t, c: rule_inline_cte(t)),) if extra else ())
        result = _apply(tree.copy(), catalog, rules, steps, snapshots, dialect)
        text = result.sql(dialect=dialect, pretty=True)
        if steps and _key(text) not in seen:
            seen.add(_key(text))
            out.append(Candidate(text, tuple(steps), tuple(snapshots)))
    return out


def _key(sql: str) -> str:
    return " ".join(sql.lower().split())


def expand_stars(sql: str, catalog: Catalog, dialect: str = "postgres") -> str | None:
    """``sql`` with every ``*`` expanded and columns qualified from the catalog, or ``None``."""

    from sqlglot.optimizer.qualify import qualify

    schema = {t: {c: (catalog.types.get(t, {}).get(c) or "text") for c in cols} for t, cols in catalog.columns.items()}
    try:
        tree = qualify(
            sqlglot.parse_one(sql, read=dialect),
            schema=schema,
            dialect=dialect,
            expand_stars=True,
            validate_qualify_columns=False,
            identify=False,
            quote_identifiers=False,
        )
    except Exception:  # noqa: BLE001 - an unreadable query just keeps its stars
        return None
    if any(isinstance(n, exp.Star) for n in tree.walk() if not isinstance(n.parent, exp.Count)):
        return None
    return tree.sql(dialect=dialect)


class _OutOfTime(Exception):
    pass


@contextlib.contextmanager
def _time_limit(seconds: float):
    """Stop the enclosed Python code after ``seconds`` of wall time (main thread only)."""

    if seconds <= 0 or threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield
        return

    def expire(signum, frame):
        raise _OutOfTime()

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def prove(original: str, rewritten: str, catalog: Catalog, *, dialect: str = "postgres", timeout_ms: int = 10000, wall_s: float = 60.0):
    """The prover's verdict, trying the statements as written and then with stars expanded.

    Each attempt is stopped after ``wall_s`` seconds and then counts as not proven.
    """

    from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic
    from kumo_sql_prover.smt_equivalence import SmtEquivalenceResult, SmtStatus

    def attempt(a: str, b: str):
        try:
            with _time_limit(wall_s):
                return prove_equivalent_algebraic(
                    a,
                    b,
                    schema=catalog.columns or None,
                    constraints=catalog.constraints() or None,
                    types=catalog.types or None,
                    dialect=dialect,
                    timeout_ms=timeout_ms,
                )
        except _OutOfTime:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"timeout: no proof within {wall_s:.0f} s")
        except Exception as error:  # noqa: BLE001 - a crash is a failure to prove, never a proof
            if isinstance(error.__context__, _OutOfTime) or "_OutOfTime" in str(error):
                return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"timeout: no proof within {wall_s:.0f} s")
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"error: {type(error).__name__}: {str(error)[:120]}")

    result = attempt(original, rewritten)
    if result.proven:
        return result
    a, b = expand_stars(original, catalog, dialect), expand_stars(rewritten, catalog, dialect)
    if a is not None and b is not None:
        expanded = attempt(a, b)
        if expanded.proven:
            return expanded
    return result


def scs_like(sql: str, dialect: str = "postgres") -> int:
    """A rough size measure used to rank proven candidates: AST nodes."""

    return sum(1 for _ in sqlglot.parse_one(sql, read=dialect).walk())


def _prove_chain(sql: str, candidate: Candidate, catalog: Catalog, dialect: str, timeout_ms: int, deadline: float):
    """``(proven SQL, steps, reasons)``: the whole rewrite if proven, else the longest proven chain of steps."""

    def wall() -> float:
        return max(0.0, min(60.0, deadline - time.perf_counter()))

    result = prove(sql, candidate.sql, catalog, dialect=dialect, timeout_ms=timeout_ms, wall_s=wall())
    if result.proven:
        return candidate.sql, candidate.steps, []
    reasons = [f"whole: {result.status.value}: {result.reason[:160]}"]
    current, done = sql, 0
    for index, (step, text) in enumerate(zip(candidate.steps, candidate.snapshots)):
        if _key(text) == _key(current):
            done = index + 1
            continue
        if wall() <= 0.5:
            reasons.append(f"step {index + 1} {step}: out of time")
            break
        result = prove(current, text, catalog, dialect=dialect, timeout_ms=timeout_ms, wall_s=wall())
        if not result.proven:
            reasons.append(f"step {index + 1} {step}: {result.status.value}: {result.reason[:160]}")
            break
        current, done = text, index + 1
    if done == 0 or _key(current) == _key(sql):
        return None, (), reasons
    return current, candidate.steps[:done], reasons


def _deletion_edits(tree: exp.Expression):
    """``(name, index, apply)`` for each single deletion that might keep the query's meaning.

    ``apply(node)`` edits the node at ``index`` in ``list(copy.walk())`` of a copy of ``tree``.
    Every edit is only a proposal: it is kept when the prover proves it.
    """

    nodes = list(tree.walk())
    for index, node in enumerate(nodes):
        if isinstance(node, exp.Select):
            distinct = node.args.get("distinct")
            if plain_distinct(node):
                yield "drop_distinct", index, lambda n: n.set("distinct", None)
            group = node.args.get("group")
            if group is not None and len(group.expressions) > 1 and not extended_grouping(group):
                for k in range(len(group.expressions)):
                    yield "drop_group_key", index, (lambda k: lambda n: n.args["group"].set("expressions", [e.copy() for i, e in enumerate(n.args["group"].expressions) if i != k]))(k)
            if group is not None and not node.args.get("having") and not any(_has_agg_or_window(e) for e in node.expressions):
                yield "drop_group_by", index, lambda n: n.set("group", None)
                if distinct is None:
                    yield "group_by_to_distinct", index, lambda n: (n.set("group", None), n.set("distinct", exp.Distinct()))
            for j, join in enumerate(node.args.get("joins") or []):
                alias = _source_name(join.this)
                if alias is None or join.args.get("using") or (join.side or "").upper() in ("RIGHT", "FULL"):
                    continue
                if any(isinstance(e, exp.Star) for e in node.expressions):
                    continue
                used = any(
                    c.table.lower() == alias and not _inside(c, join)
                    for c in node.find_all(exp.Column)
                )
                if not used:
                    yield "drop_join", index, (lambda j: lambda n: n.args["joins"][j].pop())(j)
        elif isinstance(node, (exp.Where, exp.Having)):
            parts = _conjuncts(node.this)
            if len(parts) == 1:
                yield "drop_predicate", index, lambda n: n.pop()
            else:
                for k in range(len(parts)):
                    yield "drop_predicate", index, (lambda k: lambda n: n.set("this", _and_all([c.copy() for i, c in enumerate(_conjuncts(n.this)) if i != k])))(k)
        elif isinstance(node, exp.Join) and node.args.get("on") is not None:
            parts = _conjuncts(node.args["on"])
            if len(parts) > 1:
                for k in range(len(parts)):
                    yield "drop_predicate", index, (lambda k: lambda n: n.set("on", _and_all([c.copy() for i, c in enumerate(_conjuncts(n.args["on"])) if i != k])))(k)


def _grouping_violations(tree: exp.Expression) -> int:
    """References PostgreSQL would reject after a deletion: ungrouped columns, DISTINCT orderings.

    A deletion can be proven equivalent yet not run: dropping a ``GROUP BY`` key
    that another key determines leaves its column ungrouped in ``SELECT``, which
    PostgreSQL accepts only when a grouped primary key determines it. Edits that
    raise this count are skipped. Counted conservatively, without such keys.
    """

    count = 0
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        order = select.args.get("order")
        outputs = [e.unalias() for e in select.expressions]
        aliases = {e.alias_or_name.lower() for e in select.expressions if e.alias_or_name}
        if select.args.get("distinct") is not None and order is not None:
            listed = {o.sql() for o in outputs}
            for item in order.expressions:
                key = item.this
                if key.sql() not in listed and not (isinstance(key, exp.Column) and not key.table and key.name.lower() in aliases):
                    count += 1
        if group is None or not group.expressions or any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets")):
            continue
        grouped = {g.sql() for g in group.expressions}
        roots = list(outputs) + ([select.args["having"].this] if select.args.get("having") else [])
        roots += [o.this for o in order.expressions] if order is not None else []
        for root in roots:
            for column in root.find_all(exp.Column):
                if column.find_ancestor(exp.Select) is not select or isinstance(column.this, exp.Star):
                    continue
                if not column.table and column.name.lower() in aliases and root not in outputs:
                    continue
                node, covered = column, False
                while node is not None:
                    if node.sql() in grouped or isinstance(node, (exp.AggFunc, exp.Window)):
                        covered = True
                        break
                    if node is root:
                        break
                    node = node.parent
                count += not covered
    return count


def search_deletions(
    sql: str,
    catalog: Catalog,
    *,
    dialect: str = "postgres",
    budget_s: float = 20.0,
    timeout_ms: int = 5000,
    max_size: int = 4000,
    cost=None,
):
    """Greedily delete DISTINCTs, GROUP BYs, joins and predicates the prover shows are redundant.

    Returns ``(sql, steps)``; each kept deletion is proven equivalent to the query before it.
    With ``cost`` (an estimate such as EXPLAIN's, ``None`` when the engine rejects
    the query), a deletion is also kept only if it does not raise the estimate.
    """

    deadline = time.perf_counter() + budget_s
    current, steps = sql, []
    while time.perf_counter() < deadline:
        tree = sqlglot.parse_one(current, read=dialect)
        if sum(1 for _ in tree.walk()) > max_size:
            break
        progressed = False
        violations = _grouping_violations(tree)
        current_cost = cost(current) if cost else None
        for name, index, apply in _deletion_edits(tree):
            if time.perf_counter() >= deadline:
                break
            copy = tree.copy()
            try:
                apply(list(copy.walk())[index])
                text = copy.sql(dialect=dialect, pretty=True)
                sqlglot.parse_one(text, read=dialect)
            except Exception:  # noqa: BLE001 - an edit that does not produce SQL is skipped
                continue
            if _key(text) == _key(current) or _grouping_violations(copy) > violations:
                continue
            remaining = deadline - time.perf_counter()
            if remaining <= 0.5:
                break
            if prove(current, text, catalog, dialect=dialect, timeout_ms=timeout_ms, wall_s=remaining).proven:
                if cost and current_cost is not None:
                    new_cost = cost(text)
                    if new_cost is None or new_cost > current_cost * 1.02:
                        continue
                current, progressed = text, True
                steps.append(name)
                break
        if not progressed:
            break
    return current, steps


def optimize(
    sql: str,
    catalog: Catalog,
    *,
    dialect: str = "postgres",
    cost=None,
    timeout_ms: int = 10000,
    deletions: bool = True,
    deletion_budget_s: float = 20.0,
    budget_s: float = 120.0,
) -> Outcome:
    """The best proven rewrite of ``sql``, or ``Outcome(None, reason)``.

    Each candidate is proven whole; failing that, its steps are proven one after
    another and the longest proven prefix is kept (equivalence is transitive).
    """

    started = time.perf_counter()
    try:
        candidates = rewrite_candidates(sql, catalog, dialect)
    except (sqlglot.errors.SqlglotError, RecursionError) as error:
        return Outcome(None, f"not rewritten: {type(error).__name__}", seconds=time.perf_counter() - started)
    if not candidates and not deletions:
        return Outcome(None, "no rule applies", seconds=time.perf_counter() - started)
    base_cost = cost(sql) if cost else None
    reasons = []
    proven = []
    for candidate in candidates:
        text, steps, why = _prove_chain(sql, candidate, catalog, dialect, timeout_ms, started + budget_s)
        reasons += why
        if text is None:
            continue
        if cost and base_cost is not None:
            new_cost = cost(text)
            if new_cost is None or new_cost > base_cost * 1.02:
                reasons.append(f"proven but estimated cost {new_cost} > {base_cost}")
                continue
        proven.append((scs_like(text, dialect), text, steps))
    if deletions:
        base = proven[0][1] if proven else sql
        if proven:
            proven.sort(key=lambda p: p[0])
            base = proven[0][1]
        text, extra = search_deletions(base, catalog, dialect=dialect, budget_s=deletion_budget_s, cost=cost)
        if extra:
            prior = proven[0][2] if proven else ()
            proven = [(scs_like(text, dialect), text, tuple(prior) + tuple(extra))]
    if not proven:
        return Outcome(None, "; ".join(reasons) or "not proven", seconds=time.perf_counter() - started)
    proven.sort(key=lambda p: p[0])
    _, text, steps = proven[0]
    return Outcome(text, "proven equivalent", steps, seconds=time.perf_counter() - started)


def has_top_level_order(sql: str, dialect: str = "postgres") -> bool:
    """Whether the statement's outermost query has ORDER BY (so row order is observable)."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return False
    return tree.args.get("order") is not None
