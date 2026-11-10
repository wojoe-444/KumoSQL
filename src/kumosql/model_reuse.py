"""Answer a query from an existing model, with a replacement query that is proven equivalent.

``rewrite_over_model(query, model, ...)`` asks: can ``query`` be answered by reading the model
(a view or table defined by ``model``) instead of the base tables? It proposes a replacement
over the model (extra filters, a column subset, computed columns, extra joined tables,
aggregate rollups) and returns it only when the algebraic prover proves

    query  ==  replacement   with the model's own SQL substituted for its name.

Proposing is heuristic and may fail; accepting is never heuristic. When no candidate is proven the
answer is ``no_rewrite`` (or ``unsupported`` when the query or the model uses a shape the proposer
does not read); a rewrite is never returned on a guess. Every proof carries the prover's
assumptions.

Shapes the proposer reads (both sides, after merging plain derived tables and a filter over a
grouped derived table): inner joins and filters over base tables, projections of expressions,
literal ``IN`` lists, ``DISTINCT``, ``GROUP BY`` with ``SUM``, ``COUNT``, ``MIN``, ``MAX``, ``AVG``
and distinct aggregates, and ``HAVING``. A model it does not read (a set operation, a join of
derived tables, ...) is still tried as the whole answer and as a column subset; when neither is
proven the answer is unsupported. Outer joins, windows, ``LIMIT`` and subquery predicates are
reported as unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations, product
import re
from typing import Mapping, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.qualify import qualify

from kumo_sql_prover.ast_utils import captured_names, grouping_elements, inside as _inside
from kumo_sql_prover.smt_equivalence import SmtStatus, TableConstraints


@dataclass(frozen=True)
class ModelReuse:
    """The outcome of asking whether a model can answer a query."""

    status: str  # rewritten | no_rewrite | unsupported | timeout | error
    reason: str
    sql: str | None = None  # the replacement query, reading ``model_name``
    strategy: str | None = None
    assumptions: tuple[str, ...] = ()
    model_columns: tuple[str, ...] = ()
    candidates_tried: int = 0
    inlined_sql: str | None = None  # the replacement with the model's definition substituted for its name
    query_sql: str | None = None  # the query as compared (lower-cased, schema prefixes dropped)

    @property
    def rewritten(self) -> bool:
        return self.status == "rewritten"


class _Unsupported(Exception):
    pass


@dataclass
class _Block:
    tables: list[tuple[str, str]]  # (alias, table)
    conjuncts: list[exp.Expression]
    outputs: list[exp.Expression]  # select items, aliases included
    group: list[exp.Expression]
    having: exp.Expression | None
    distinct: bool
    order: list[exp.Expression] = field(default_factory=list)

    @property
    def is_aggregate(self) -> bool:
        return bool(self.group) or any(_has_aggregate(o) for o in self.outputs) or self.having is not None


_AGGREGATES = (exp.Sum, exp.Count, exp.Min, exp.Max, exp.Avg)
_TOP_LEVEL_UNSUPPORTED = (exp.Window, exp.Subquery, exp.Exists, exp.In, exp.Unnest, exp.Lateral, exp.Star)


def _allowed(node: exp.Expression) -> bool:
    """``COUNT(*)`` and an ``IN`` over a list of values are plain expressions; their subquery and star forms are not."""

    if isinstance(node, exp.Star):
        return isinstance(node.parent, exp.Count)
    if isinstance(node, exp.In):
        return not (node.args.get("query") or node.args.get("unnest") or node.args.get("field"))
    return False


def _has_aggregate(node: exp.Expression) -> bool:
    return any(isinstance(n, exp.AggFunc) for n in node.walk())


def _lowercase(tree: exp.Expression) -> exp.Expression:
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.name.lower())
        ident.set("quoted", False)
    return tree


def _name_clash(sqls: Sequence[str], schema: Mapping[str, Sequence[str]], dialect: str, model_name: str) -> str:
    """Why dropping schema prefixes (as :func:`_plain` and :func:`_prepare` do) would make two tables one, or ``""``.

    The schema names tables by their last part, so ``a.t`` and ``b.t`` both become ``t``: that is only sound
    when every spelling of ``t`` across the query and the model is the same table. A WITH table called ``t``
    would capture a read of ``a.t`` once the prefix is gone, and a table called like the model would be
    replaced by the model's definition, so both are refused too.
    """

    known = {t.lower() for t in schema}
    spellings: dict[str, set[tuple[str, ...]]] = {}
    for sql in sqls:
        tree = _lowercase(sqlglot.parse_one(sql, read=dialect))
        for table in tree.find_all(exp.Table):
            if model_name and table.name == model_name.lower():
                return f"the SQL reads a table called {model_name}, the name the model is given"
            prefix = tuple(p.name for p in (table.args.get("catalog"), table.args.get("db")) if p is not None and p.name)
            spellings.setdefault(table.name, set()).add(prefix)
        for cte in tree.find_all(exp.CTE):
            spellings.setdefault(cte.alias_or_name.lower(), set()).add(("<with>",))
    for name, found in sorted(spellings.items()):
        if name in known and len(found) > 1:
            shown = sorted(".".join((*p, name)) if p != ("<with>",) else f"WITH {name}" for p in found)
            return f"{' and '.join(shown)} would be read as the same table {name}"
    return ""


def _plain(sql: str, schema: Mapping[str, Sequence[str]], dialect: str) -> exp.Expression:
    """The query as written, with names lower-cased, unquoted and schema prefixes of known tables dropped."""

    tree = _lowercase(sqlglot.parse_one(sql, read=dialect))
    known = {t.lower() for t in schema}
    for table in tree.find_all(exp.Table):
        if table.args.get("db") and table.name in known:
            table.set("db", None)
            table.set("catalog", None)
    return tree


def _prepare(sql: str, schema: Mapping[str, Sequence[str]], dialect: str) -> exp.Expression:
    tree = _lowercase(sqlglot.parse_one(sql, read=dialect))
    known = {t.lower() for t in schema}
    for table in tree.find_all(exp.Table):
        if table.args.get("db") and table.name in known:
            table.set("db", None)
            table.set("catalog", None)
    columns = {t.lower(): {c.lower(): "unknown" for c in cols} for t, cols in schema.items()}
    try:
        tree = qualify(tree, schema=columns, dialect=dialect, validate_qualify_columns=False, quote_identifiers=False, identify=False)
        tree = merge_subqueries(tree)
    except Exception as error:  # noqa: BLE001 - sqlglot's optimizer raises many types
        raise _Unsupported(f"could not resolve the query: {error}") from error
    return _merge_grouped_derived(tree)


def _merge_grouped_derived(tree: exp.Expression) -> exp.Expression:
    """``SELECT f(c) FROM (SELECT .. GROUP BY ..) d WHERE p(c)`` as one grouped SELECT with ``p`` in HAVING.

    ``merge_subqueries`` leaves grouped derived tables alone. Filtering the rows of a grouped table is
    filtering its groups, so the outer filter becomes a HAVING over the inner expressions, and the outer
    projection reads those expressions directly. Only a plain projection and filter over a single grouped
    derived table is merged; anything else is returned unchanged.
    """

    if not isinstance(tree, exp.Select) or tree.args.get("joins"):
        return tree
    if any(tree.args.get(k) for k in ("group", "having", "distinct", "limit", "offset", "qualify", "windows", "with", "order", "laterals", "pivots")):
        return tree
    source = tree.args.get("from_") or tree.args.get("from")
    derived = source.this if source is not None else None
    if not isinstance(derived, exp.Subquery) or not isinstance(derived.this, exp.Select):
        return tree
    inner = derived.this
    if not (inner.args.get("group") or any(_has_aggregate(e) for e in inner.expressions)):
        return tree
    if any(inner.args.get(k) for k in ("distinct", "limit", "offset", "qualify", "windows", "with", "order")):
        return tree
    if any(isinstance(n, exp.Window) for e in inner.expressions for n in e.walk()):
        return tree
    if any(not isinstance(e, exp.Alias) for e in inner.expressions):
        return tree
    alias = derived.alias_or_name
    defs = {e.alias: e.this for e in inner.expressions}
    if len(defs) != len(inner.expressions):
        return tree
    roots = list(tree.expressions) + ([tree.args["where"].this] if tree.args.get("where") else [])
    if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Star)) for r in roots for n in r.walk()):
        return tree
    for root in roots:
        for column in root.find_all(exp.Column):
            if column.table != alias or column.name not in defs:
                return tree

    def substitute(node: exp.Expression) -> exp.Expression:
        def visit(n):
            if isinstance(n, exp.Column) and n.table == alias:
                return defs[n.name].copy()
            return n

        return node.copy().transform(visit)

    merged = inner.copy()
    merged.set("expressions", [exp.alias_(substitute(_inner(e)), e.alias_or_name) for e in tree.expressions])
    if tree.args.get("where"):
        condition = substitute(tree.args["where"].this)
        having = merged.args.get("having")
        merged.set("having", exp.Having(this=exp.and_(exp.Paren(this=having.this), exp.Paren(this=condition)) if having else condition))
    return merged


def _split_and(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, (exp.Where, exp.Paren)):
        return _split_and(node.this)
    if isinstance(node, exp.And):
        return _split_and(node.left) + _split_and(node.right)
    return [node]


def not_null_columns(constraints: Mapping[str, TableConstraints] | None) -> dict[str, set[str]]:
    """Columns that never hold NULL, by lower-cased table name (declared NOT NULL or part of a key)."""

    found: dict[str, set[str]] = {}
    for table, c in (constraints or {}).items():
        keyed = {col for key in c.keys for col in key}
        found[table.lower()] = {col.lower() for col in c.not_null} | {col.lower() for col in keyed}
    return found


def _count_star_for_not_null(block: "_Block", not_null: Mapping[str, set[str]]) -> None:
    """COUNT(col) of a column that is never NULL is COUNT(*): spell it so, so equal counts get equal keys."""

    by_alias = dict(block.tables)
    roots = [*block.outputs, *block.group, *block.order] + ([block.having] if block.having is not None else [])
    for root in roots:
        for count in root.find_all(exp.Count):
            argument = count.this
            if isinstance(argument, exp.Column) and argument.name in not_null.get(by_alias.get(argument.table, ""), ()):
                count.set("this", exp.Star())


def _block(tree: exp.Expression, not_null: Mapping[str, set[str]] | None = None) -> _Block:
    if not isinstance(tree, exp.Select):
        raise _Unsupported("not a plain SELECT (set operation, CTE or other statement)")
    if tree.args.get("with") or tree.args.get("limit") or tree.args.get("offset") or tree.args.get("qualify") or tree.args.get("windows"):
        raise _Unsupported("WITH, LIMIT, OFFSET or QUALIFY")
    source = tree.args.get("from_") or tree.args.get("from")
    if source is None:
        raise _Unsupported("no FROM")
    tables: list[tuple[str, str]] = []
    conjuncts: list[exp.Expression] = []
    relations = [source.this] + [j for j in tree.args.get("joins") or []]
    for relation in relations:
        join = relation if isinstance(relation, exp.Join) else None
        table = join.this if join is not None else relation
        if join is not None:
            if join.args.get("side") or join.args.get("kind") not in (None, "INNER", "CROSS"):
                raise _Unsupported("outer or special join")
            if join.args.get("using"):
                raise _Unsupported("USING join not resolved")
            conjuncts += _split_and(join.args.get("on"))
        if not isinstance(table, exp.Table) or isinstance(table.this, exp.Func):
            raise _Unsupported("derived table or table function")
        tables.append((table.alias_or_name, table.name))
    conjuncts += _split_and(tree.args.get("where"))
    outputs = list(tree.expressions)
    group = grouping_elements(tree.args.get("group"))
    if tree.args.get("group") and tree.args["group"].args.get("totals") or any(isinstance(g, (exp.Rollup, exp.Cube)) and not g.expressions for g in group):
        raise _Unsupported("WITH ROLLUP, WITH CUBE or WITH TOTALS")
    having = tree.args["having"].this if tree.args.get("having") else None
    parts = [*conjuncts, *outputs, *group] + ([having] if having is not None else [])
    for part in parts:
        for node in part.walk():
            if isinstance(node, _TOP_LEVEL_UNSUPPORTED) and not _allowed(node):
                raise _Unsupported(f"unsupported construct: {type(node).__name__}")
    order = list(tree.args["order"].expressions) if tree.args.get("order") else []
    block = _Block(tables, conjuncts, outputs, group, having, bool(tree.args.get("distinct")), order)
    if not_null:
        _count_star_for_not_null(block, not_null)
    return block


# ---------------------------------------------------------------------------------------------
# canonical keys, so spellings that differ only in operand order compare equal


def _key(node: exp.Expression) -> str:
    node = _canonical(node.copy())
    return node.sql(dialect="postgres")


_FLIP = {exp.LT: exp.GT, exp.LTE: exp.GTE}


def _canonical(node: exp.Expression) -> exp.Expression:
    def visit(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Paren) and not isinstance(n.this, (exp.Binary, exp.Unary)):
            return n.this
        if isinstance(n, exp.Alias):
            return n.this
        if type(n) in _FLIP:
            return _FLIP[type(n)](this=n.expression.copy(), expression=n.this.copy())
        if isinstance(n, (exp.EQ, exp.NEQ, exp.Add, exp.Mul, exp.And, exp.Or)):
            left, right = n.this.sql(), n.expression.sql()
            if right < left:
                return type(n)(this=n.expression.copy(), expression=n.this.copy())
        if isinstance(n, exp.Between):
            low, high = n.args["low"], n.args["high"]
            return exp.And(this=exp.GTE(this=n.this.copy(), expression=low.copy()), expression=exp.LTE(this=n.this.copy(), expression=high.copy()))
        return n

    return node.transform(visit)


def _expand_conjuncts(conjuncts: list[exp.Expression]) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    for c in conjuncts:
        for part in _split_and(_canonical(c.copy())):
            out.append(part)
    return out


# ---------------------------------------------------------------------------------------------


def _output_names(block: _Block) -> list[str]:
    return _names_of(block.outputs)


def _names_of(outputs: list[exp.Expression]) -> list[str]:
    names: list[str] = []
    for index, item in enumerate(outputs):
        base = item.alias_or_name or f"col{index}"
        base = re.sub(r"\W", "_", base.lower()) or f"col{index}"
        name, n = base, 1
        while name in names:
            n += 1
            name = f"{base}_{n}"
        names.append(name)
    return names


def _inner(item: exp.Expression) -> exp.Expression:
    return item.this if isinstance(item, exp.Alias) else item


def _column(table: str, name: str) -> exp.Column:
    return exp.column(name, table=table)


class _Rewriter:
    """Rewrite expressions over base tables into expressions over the model's columns."""

    def __init__(self, model: _Block, names: list[str], model_alias: str, tables_map: dict[str, str], extras: set[str]):
        self.alias = model_alias
        self.extras = extras
        self.available: dict[str, str] = {}
        for item, name in zip(model.outputs, names):
            expression = _inner(item).copy()
            self.available.setdefault(_key(self._rename(expression, tables_map)), name)
        self.constants: dict[str, exp.Expression] = {}
        self.tables_map = tables_map

    @staticmethod
    def _rename(node: exp.Expression, mapping: dict[str, str]) -> exp.Expression:
        def visit(n):
            if isinstance(n, exp.Column) and n.table in mapping:
                return exp.column(n.name, table=mapping[n.table])
            return n

        return node.transform(visit)

    def learn_equalities(self, conjuncts: list[exp.Expression]) -> None:
        """Equalities the model's rows satisfy let a column be read as its partner or constant."""

        changed = True
        while changed:
            changed = False
            for c in conjuncts:
                if not isinstance(c, exp.EQ):
                    continue
                a, b = c.this, c.expression
                for left, right in ((a, b), (b, a)):
                    if isinstance(right, exp.Literal) and isinstance(left, exp.Column):
                        if _key(left) not in self.constants:
                            self.constants[_key(left)] = right.copy()
                            changed = True
                    elif isinstance(left, exp.Column) and isinstance(right, exp.Column):
                        if _key(right) in self.available and _key(left) not in self.available:
                            self.available[_key(left)] = self.available[_key(right)]
                            changed = True

    def rewrite(self, node: exp.Expression) -> exp.Expression | None:
        """``node`` over the model, or None when a needed column is not available."""

        key = _key(node)
        if key in self.available:
            return _column(self.alias, self.available[key])
        if key in self.constants:
            return self.constants[key].copy()
        if isinstance(node, exp.Column):
            return node.copy() if node.table in self.extras else None
        if isinstance(node, exp.Literal) or isinstance(node, exp.Null) or isinstance(node, exp.Boolean):
            return node.copy()
        if isinstance(node, exp.Alias):
            return self.rewrite(node.this)
        copy = node.copy()
        for arg_name, value in list(node.args.items()):
            if isinstance(value, exp.Expression):
                new = self.rewrite(value)
                if new is None:
                    return None
                copy.set(arg_name, new)
            elif isinstance(value, list):
                items = []
                for v in value:
                    if isinstance(v, exp.Expression):
                        new = self.rewrite(v)
                        if new is None:
                            return None
                        items.append(new)
                    else:
                        items.append(v)
                copy.set(arg_name, items)
        return copy


def _agg_available(model: _Block, names: list[str], tables_map: dict[str, str]) -> dict[str, tuple[str, exp.Expression]]:
    """The model's aggregate columns by canonical key of the aggregate expression."""

    found: dict[str, tuple[str, exp.Expression]] = {}
    for item, name in zip(model.outputs, names):
        inner = _inner(item)
        if isinstance(inner, exp.AggFunc):
            found.setdefault(_key(_Rewriter._rename(inner.copy(), tables_map)), (name, inner))
    return found


def _mappings(model_tables: list[tuple[str, str]], query_tables: list[tuple[str, str]]):
    """Each way to place the model's tables on distinct query tables of the same name."""

    by_name: dict[str, list[str]] = {}
    for alias, table in query_tables:
        by_name.setdefault(table, []).append(alias)
    groups: dict[str, list[str]] = {}
    for alias, table in model_tables:
        groups.setdefault(table, []).append(alias)
    options = []
    for table, aliases in groups.items():
        candidates = by_name.get(table, [])
        if len(candidates) < len(aliases):
            return
        options.append([list(zip(aliases, perm)) for perm in permutations(candidates, len(aliases))])
    for combo in product(*options):
        yield {q: m for pairs in combo for m, q in pairs}  # query alias -> model alias


def _combine(parts: list[exp.Expression]) -> exp.Expression | None:
    result = None
    for part in parts:
        part = part.copy()
        if isinstance(part, (exp.Or, exp.Xor)) and len(parts) > 1:
            part = exp.Paren(this=part)  # AND binds tighter than OR
        result = part if result is None else exp.And(this=result, expression=part)
    return result


@dataclass
class _Candidate:
    sql: str
    strategy: str


def _candidates(query: _Block, model: _Block, names: list[str], model_name: str):
    """Yield replacement queries over ``model_name``; none is trusted until proven."""

    if model.is_aggregate and not query.is_aggregate:
        return
    for mapping in _mappings(model.tables, query.tables):  # query alias -> model alias
        yield from _candidates_for(query, model, names, model_name, mapping)


def _conjunct_keys(node: exp.Expression | None) -> list[exp.Expression]:
    return _expand_conjuncts(_split_and(node))


def _candidates_for(query: _Block, model: _Block, names: list[str], model_name: str, mapping: dict[str, str]):
    model_in_query = {m: q for q, m in mapping.items()}  # model alias -> query alias
    extras = {alias for alias, _ in query.tables} - set(mapping)
    model_conj = [_Rewriter._rename(c.copy(), model_in_query) for c in _expand_conjuncts(model.conjuncts)]
    model_keys = {_key(c) for c in model_conj}
    query_conj = _expand_conjuncts(query.conjuncts)
    residual_all = [c for c in query_conj if _key(c) not in model_keys]

    model_renamed = _Block(
        [(model_in_query.get(a, a), t) for a, t in model.tables],
        model_conj,
        [_Rewriter._rename(o.copy(), model_in_query) for o in model.outputs],
        [_Rewriter._rename(g.copy(), model_in_query) for g in model.group],
        _Rewriter._rename(model.having.copy(), model_in_query) if model.having is not None else None,
        model.distinct,
    )
    alias = model_name
    rewriter = _Rewriter(model_renamed, names, alias, {}, extras)
    rewriter.learn_equalities(model_conj)

    def with_filters(select: exp.Select, residuals: list[exp.Expression]) -> exp.Select | None:
        rewritten = []
        for r in residuals:
            new = rewriter.rewrite(r)
            if new is None:
                return None
            rewritten.append(new)
        if rewritten:
            select.set("where", exp.Where(this=_combine(rewritten)))
        return select

    def build_from(select: exp.Select) -> exp.Select:
        select = select.from_(exp.to_table(model_name))
        for a, t in query.tables:
            if a in extras:
                select = select.join(exp.Table(this=exp.to_identifier(t), alias=exp.TableAlias(this=exp.to_identifier(a))), join_type="cross")
        return select

    def ordered(select: exp.Select, rewrite) -> bool:
        """Carry the query's ORDER BY over; False when a sort key cannot be read from the model."""

        if not query.order:
            return True
        keys = []
        for item in query.order:
            new = rewrite(item.this)
            if new is None:
                return False
            copy = item.copy()
            copy.set("this", new)
            keys.append(copy)
        select.set("order", exp.Order(expressions=keys))
        return True

    # Variants of the residual filter: all of it, or only the part that can be read from the model.
    readable = [r for r in residual_all if rewriter.rewrite(r) is not None]
    residual_options = [("filter", residual_all)]
    if len(readable) != len(residual_all):
        residual_options.append(("filter-implied-dropped", readable))

    if not model.is_aggregate:
        # model is select-project-join: rewrite the whole query over it
        for label, residuals in residual_options:
            select = exp.Select()
            ok = True
            new_outputs = []
            for item in query.outputs:
                new = rewriter.rewrite(item)
                if new is None:
                    ok = False
                    break
                alias_name = item.alias if isinstance(item, exp.Alias) else None
                new_outputs.append(exp.alias_(new, alias_name) if alias_name else new)
            if not ok:
                continue
            select.set("expressions", new_outputs)
            if query.distinct:
                select.set("distinct", exp.Distinct())
            built = with_filters(build_from(select), residuals)
            if built is None:
                continue
            if query.group:
                groups = [rewriter.rewrite(g) for g in query.group]
                if any(g is None for g in groups):
                    continue
                built.set("group", exp.Group(expressions=groups))
            if query.having is not None:
                having = rewriter.rewrite(query.having)
                if having is None:
                    continue
                built.set("having", exp.Having(this=having))
            if not ordered(built, rewriter.rewrite):
                continue
            yield _Candidate(built.sql(dialect="postgres"), f"spj-{label}")
        return

    # model is an aggregate; the query must read it at the same or a coarser grain.
    model_group_keys = {_key(g) for g in model_renamed.group}
    model_aggs = _agg_available(model_renamed, names, {})
    model_having = [_key(h) for h in _conjunct_keys(model_renamed.having)]
    # a model key the query fixes to a constant does not split the query's groups any further
    fixed = {
        _key(side)
        for c in query_conj
        if isinstance(c, exp.EQ)
        for side, other in ((c.this, c.expression), (c.expression, c.this))
        if isinstance(side, exp.Column) and isinstance(other, exp.Literal)
    }
    for label, residuals in residual_options:
        query_group_keys = {_key(g) for g in query.group}
        same_grain = not extras and (
            query_group_keys == model_group_keys
            or (bool(query.group) and query_group_keys <= model_group_keys <= query_group_keys | fixed)
        )
        query_having = _conjunct_keys(query.having)
        if model_having:
            # the model dropped groups: the query must drop at least those, and nothing finer can be rebuilt
            if not same_grain or not set(model_having) <= {_key(h) for h in query_having}:
                continue
            query_having = [h for h in query_having if _key(h) not in set(model_having)]
        regroup: list = []

        def agg_rewrite(node, same_grain=same_grain, regroup=regroup):
            return _agg_rewrite(node, rewriter, model_aggs, same_grain, alias, no_group=_has_empty_grouping(query.group), regroup=regroup)

        select = exp.Select()
        built_outputs: list[exp.Expression] = []
        ok = True
        for item in query.outputs:
            new = agg_rewrite(_inner(item))
            if new is None:
                ok = False
                break
            alias_name = item.alias if isinstance(item, exp.Alias) else None
            built_outputs.append(exp.alias_(new, alias_name) if alias_name else new)
        if not ok:
            continue
        select.set("expressions", built_outputs)
        if query.distinct:
            select.set("distinct", exp.Distinct())
        built = with_filters(build_from(select), residuals)
        if built is None:
            continue
        if not same_grain:
            groups = [rewriter.rewrite(g) for g in query.group]
            if any(g is None for g in groups):
                continue
            if groups:
                built.set("group", exp.Group(expressions=groups))
        having = None
        if query_having:
            parts = [agg_rewrite(h) for h in query_having]
            if any(p is None for p in parts):
                continue
            having = _combine(parts)
        if having is not None:
            if same_grain:
                where = built.args.get("where")
                built.set("where", exp.Where(this=_combine([w for w in [where.this if where else None, having] if w is not None])))
            else:
                built.set("having", exp.Having(this=having))
        if not ordered(built, agg_rewrite):
            continue
        if regroup and not _regroup_by_model_keys(built, model_renamed.group, rewriter):
            continue
        yield _Candidate(built.sql(dialect="postgres"), "aggregate-same-grain" if same_grain else "aggregate-rollup")


def _regroup_by_model_keys(built: exp.Select, model_group: list[exp.Expression], rewriter: _Rewriter) -> bool:
    """Group a same-grain replacement by the model's keys (one model row per group, so the rows are
    unchanged) and by every other model column it reads outside an aggregate; False when a key is not
    an output of the model."""

    keys = [rewriter.rewrite(g) for g in model_group]
    if not keys or any(k is None for k in keys):
        return False
    seen = {k.sql() for k in keys}
    roots = list(built.expressions) + ([built.args["order"]] if built.args.get("order") else [])
    for root in roots:
        for column in root.find_all(exp.Column):
            if column.find_ancestor(exp.AggFunc) is None and column.sql() not in seen:
                seen.add(column.sql())
                keys.append(column.copy())
    built.set("group", exp.Group(expressions=keys))
    return True


def _has_empty_grouping(group: list[exp.Expression]) -> bool:
    """Whether the grouping has an empty grouping set (no GROUP BY, or one ROLLUP, CUBE or GROUPING SETS
    can produce ``()``): that group exists even over no rows, where a sum of counts is NULL, not 0."""

    def can_be_empty(g: exp.Expression) -> bool:
        if isinstance(g, (exp.Cube, exp.Rollup)):
            return True
        if isinstance(g, exp.GroupingSets):
            return any(isinstance(s, (exp.Tuple, exp.Paren)) and not (s.expressions if isinstance(s, exp.Tuple) else s.this) for s in g.expressions)
        return False

    return all(can_be_empty(g) for g in group)


def _agg_rewrite(node: exp.Expression, rewriter: _Rewriter, model_aggs: dict[str, tuple[str, exp.Expression]], same_grain: bool, alias: str, no_group: bool, regroup: list | None = None):
    """Rewrite an expression of a grouped query, re-aggregating the model's partial aggregates.

    At the model's own grain an aggregate the model lacks can still be computed over the model's rows
    when they are grouped again by the model's keys; such aggregates are appended to ``regroup``.
    """

    regroup = [] if regroup is None else regroup

    def lookup(agg: exp.Expression):
        found = model_aggs.get(_key(agg))
        return _column(alias, found[0]) if found else None

    def visit(n: exp.Expression):
        if isinstance(n, exp.AggFunc):
            return aggregate(n)
        if isinstance(n, exp.Alias):
            return visit(n.this)
        key = _key(n)
        if key in rewriter.available:
            return _column(alias, rewriter.available[key])
        if isinstance(n, exp.Column):
            return None
        if isinstance(n, (exp.Literal, exp.Null, exp.Boolean)):
            return n.copy()
        copy = n.copy()
        for arg_name, value in list(n.args.items()):
            if isinstance(value, exp.Expression):
                new = visit(value)
                if new is None:
                    return None
                copy.set(arg_name, new)
            elif isinstance(value, list):
                items = []
                for v in value:
                    new = visit(v) if isinstance(v, exp.Expression) else v
                    if new is None:
                        return None
                    items.append(new)
                copy.set(arg_name, items)
        return copy

    def aggregate(agg: exp.AggFunc):
        distinct = isinstance(agg.this, exp.Distinct)
        if distinct:
            if same_grain:
                direct = lookup(agg)
                if direct is not None:
                    return direct
            if isinstance(agg, (exp.Count, exp.Sum)):
                arguments = [rewriter.rewrite(e) for e in agg.this.expressions]
                if any(a is None for a in arguments):
                    return None
                if same_grain:
                    # each row of the model is one group: aggregate it again over the model's own keys
                    regroup.append(agg)
                return type(agg)(this=exp.Distinct(expressions=arguments))
            return None
        direct = lookup(agg)
        if same_grain:
            return direct
        if direct is None or isinstance(agg, exp.Avg):
            if isinstance(agg, exp.Avg):
                total = lookup(exp.Sum(this=agg.this.copy()))
                count = lookup(exp.Count(this=agg.this.copy()))
                if count is None:
                    count = lookup(exp.Count(this=exp.Star()))
                if count is None:
                    return None
                if total is None:
                    # a weighted average: the sum is each group's mean times its count of values
                    mean = lookup(exp.Avg(this=agg.this.copy()))
                    if mean is None:
                        return None
                    total = exp.Mul(this=mean, expression=count.copy())
                return exp.Div(
                    this=exp.Sum(this=total),
                    expression=exp.Nullif(this=exp.Sum(this=count), expression=exp.Literal.number(0)),
                    typed=True,
                )
            if isinstance(agg, exp.Min) or isinstance(agg, exp.Max):
                argument = rewriter.rewrite(agg.this)
                return type(agg)(this=argument) if argument is not None else None
            return None
        if isinstance(agg, exp.Count):
            total = exp.Sum(this=direct)
            return exp.Coalesce(this=total, expressions=[exp.Literal.number(0)]) if no_group else total
        if isinstance(agg, (exp.Sum, exp.Min, exp.Max)):
            return type(agg)(this=direct)
        return None

    return visit(node)


def _is_identity(select: exp.Expression, model_name: str, names: list[str]) -> bool:
    """Whether the replacement returns the model's columns unchanged, in order (a pure row filter)."""

    if not isinstance(select, exp.Select) or select.args.get("group") or select.args.get("having") or select.args.get("distinct") or select.args.get("joins"):
        return False
    outputs = [e.this if isinstance(e, exp.Alias) else e for e in select.expressions]
    return len(outputs) == len(names) and all(isinstance(o, exp.Column) and o.table == model_name and o.name == n for o, n in zip(outputs, names))


def _model_outputs(model_sql: str, schema: Mapping[str, Sequence[str]], dialect: str) -> tuple[_Block, list[str]]:
    block = _block(_prepare(model_sql, schema, dialect))
    return block, _output_names(block)


def _named_model_sql(block: _Block, names: list[str], prepared: exp.Expression, plain: exp.Expression) -> str:
    """The model's SQL with every output given the name the replacement uses.

    The model as written is used when it can be renamed in place (no ``*``, no ``USING``); otherwise
    its resolved form is.
    """

    simple = (
        isinstance(plain, exp.Select)
        and len(plain.expressions) == len(names)
        and not any(isinstance(n, exp.Star) for e in plain.expressions for n in e.walk())
        and not any(j.args.get("using") for j in plain.args.get("joins") or [])
    )
    tree = (plain if simple else prepared).copy()
    if simple:
        tree.set("expressions", [exp.alias_(_inner(item).copy(), name) for item, name in zip(plain.expressions, names)])
    else:
        tree.set("expressions", [exp.alias_(_inner(item).copy(), name) for item, name in zip(block.outputs, names)])
    return tree.sql(dialect="postgres")


def _propose(query_tree: exp.Expression, model: _Block, names: list[str], model_name: str, not_null):
    """Candidates for one SELECT (prepared on its own); a shape the proposer does not read yields none."""

    try:
        block = _block(query_tree, not_null)
        yield from _candidates(block, model, names, model_name)
    except _Unsupported:
        return


def _sites(plain: exp.Expression) -> list[exp.Expression]:
    """Nested SELECTs that could be answered from the model on their own: branches of set operations,
    derived tables, subqueries and CTE bodies (outermost first)."""

    found: list[exp.Expression] = []
    for node in plain.find_all(exp.Select):
        if node is plain:
            continue
        found.append(node)
    return found


def _outputs_named(select: exp.Select) -> bool:
    return all(isinstance(e, exp.Alias) or isinstance(e, exp.Column) for e in select.expressions)


def _leftmost_select(tree: exp.Expression) -> exp.Expression:
    """The SELECT whose output names a set operation takes."""

    while isinstance(tree, (exp.SetOperation, exp.Subquery)):
        tree = tree.this if isinstance(tree, exp.Subquery) else tree.left
    return tree


def _opaque_model(model_tree: exp.Expression, reason: str) -> tuple[list[str], str]:
    """Output names and named SQL for a model the proposer does not read (a set operation, a join of
    derived tables, ...), so whole-model and projection candidates can still be proven against it."""

    if any(model_tree.args.get(k) for k in ("limit", "offset", "with")):
        raise _Unsupported(reason)
    tree = model_tree.copy()
    first = _leftmost_select(tree)
    if not isinstance(first, exp.Select) or any(isinstance(n, exp.Star) for e in first.expressions for n in e.walk() if not isinstance(n.parent, exp.Count)):
        raise _Unsupported(reason)
    names = _names_of(first.expressions)
    first.set("expressions", [exp.alias_(_inner(item).copy(), name) for item, name in zip(first.expressions, names)])
    return names, tree.sql(dialect="postgres")


def _lineage_keys(select: exp.Select) -> list[str]:
    """A key per output: its expression, with each column of a derived table replaced by that table's own
    expression for it (tagged with the derived table's alias)."""

    sources = [select.args.get("from_") or select.args.get("from")] + list(select.args.get("joins") or [])
    derived = {s.this.alias_or_name: s.this.this for s in sources if s is not None and isinstance(s.this, exp.Subquery) and isinstance(s.this.this, exp.Select)}
    keys = []
    for item in select.expressions:
        node = _inner(item).copy()
        if isinstance(node, exp.Column) and node.table in derived:
            node = exp.Paren(this=node)  # so a bare column can be replaced in place
        for column in list(node.find_all(exp.Column)):
            inner = derived.get(column.table)
            if inner is None:
                continue
            match = [e for e in inner.expressions if e.alias_or_name == column.name]
            if len(match) != 1:
                return []
            column.replace(exp.Anonymous(this="from_derived", expressions=[exp.Literal.string(column.table), _inner(match[0]).copy()]))
        keys.append(_key(node))
    return keys


def _projection_by_lineage(query_tree: exp.Expression, model_tree: exp.Expression, names: list[str], model_name: str) -> list[_Candidate]:
    """The query as a column subset of a model it otherwise equals (matched by where each output comes from)."""

    if not (isinstance(query_tree, exp.Select) and isinstance(model_tree, exp.Select)):
        return []
    query_keys, model_keys = _lineage_keys(query_tree), _lineage_keys(model_tree)
    if not query_keys or len(model_keys) != len(names):
        return []
    by_key: dict[str, str] = {}
    for key, name in zip(model_keys, names):
        by_key.setdefault(key, name)
    if not all(k in by_key for k in query_keys):
        return []
    outputs = [exp.alias_(_column(model_name, by_key[k]), item.alias_or_name) if item.alias_or_name else _column(model_name, by_key[k]) for k, item in zip(query_keys, query_tree.expressions)]
    select = exp.Select(expressions=outputs).from_(exp.to_table(model_name))
    if query_tree.args.get("distinct"):
        select.set("distinct", exp.Distinct())
    return [_Candidate(select.sql(dialect="postgres"), "projection")]


def rewrite_over_model(
    query_sql: str,
    model_sql: str,
    *,
    schema: Mapping[str, Sequence[str]],
    constraints: Mapping[str, TableConstraints] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    model_name: str = "mv0",
    dialect: str = "postgres",
    timeout_ms: int = 5000,
    exact_arithmetic: bool = False,
    identity: bool = False,
) -> ModelReuse:
    """Return a verified replacement for ``query_sql`` that reads ``model_name``, or say why not."""

    from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic

    try:
        clash = _name_clash((query_sql, model_sql), schema, dialect, model_name)
    except sqlglot.errors.SqlglotError as error:
        return ModelReuse("unsupported", f"parse error: {error}")
    if clash:
        return ModelReuse("unsupported", clash)
    nn = not_null_columns(constraints)
    opaque = ""  # why the proposer cannot read the model, when it cannot
    model: _Block | None
    try:
        model_tree = _prepare(model_sql, schema, dialect)
        try:
            model = _block(model_tree, nn)
            names = _output_names(model)
        except _Unsupported as error:
            # a model the proposer does not read can still answer the query as a whole or as a projection
            opaque = f"model: {error}"
            model = None
            names, opaque_sql = _opaque_model(model_tree, opaque)
    except _Unsupported as error:
        return ModelReuse("unsupported", opaque or f"model: {error}")
    except sqlglot.errors.SqlglotError as error:
        return ModelReuse("unsupported", f"model parse error: {error}")
    try:
        plain_model, plain_query = _plain(model_sql, schema, dialect), _plain(query_sql, schema, dialect)
    except sqlglot.errors.SqlglotError as error:
        return ModelReuse("unsupported", f"parse error: {error}")
    if model is None:
        named_model = opaque_sql
    else:
        # the model's own ORDER BY never changes its rows
        if model.order:
            model.order = []
        named_model = _named_model_sql(model, names, model_tree, plain_model)
    plain_query_sql = plain_query.sql(dialect="postgres")
    base_schema = {t.lower(): [c.lower() for c in cols] for t, cols in schema.items()}

    # Candidate replacements, from the whole query down to nested pieces.
    def whole() -> list[_Candidate]:
        out = []
        try:
            query = _block(_prepare(query_sql, schema, dialect), nn)
        except _Unsupported as error:
            whole.reason = str(error)  # type: ignore[attr-defined]
            return out
        except sqlglot.errors.SqlglotError as error:
            whole.reason = f"parse error: {error}"  # type: ignore[attr-defined]
            return out
        out.extend(_candidates(query, model, names, model_name))
        return out

    whole.reason = ""  # type: ignore[attr-defined]
    candidates: list[_Candidate] = []
    exact = "SELECT " + ", ".join(f"{model_name}.{n}" for n in names) + f" FROM {model_name}"
    # A whole-view replacement cannot change its output width. Avoid expensive
    # proof search for an impossible candidate; unresolved stars keep the old path.
    explicit_width = (len(plain_query.expressions) if isinstance(plain_query, exp.Select)
                      and not any(e.is_star for e in plain_query.expressions) else None)
    if explicit_width is None or explicit_width == len(names):
        candidates.append(_Candidate(exact, "same-as-model"))
    if model is None:
        if isinstance(model_tree, exp.SetOperation):
            # the prover reads a set operation under SELECT * as the set operation itself
            candidates.append(_Candidate(f"SELECT * FROM {model_name}", "same-as-model"))
        try:
            candidates.extend(_projection_by_lineage(_prepare(query_sql, schema, dialect), model_tree, names, model_name))
        except (_Unsupported, sqlglot.errors.SqlglotError):
            pass
    else:
        try:
            candidates.extend(whole())
        except _Unsupported as error:
            return ModelReuse("unsupported", str(error), model_columns=tuple(names))
    if identity:
        candidates = [c for c in candidates if _is_identity(sqlglot.parse_one(c.sql, read="postgres"), model_name, names)]
    elif model is not None:
        # nested pieces: replace a sub-select by an answer from the model, keep the rest of the query
        pieces: list[tuple[exp.Select, str]] = []
        for site in _sites(plain_query):
            try:
                prepared = _prepare(site.sql(dialect="postgres"), schema, dialect="postgres")
            except (_Unsupported, sqlglot.errors.SqlglotError):
                continue
            for candidate in _propose(prepared, model, names, model_name, nn):
                if candidate.strategy.startswith(("spj", "aggregate")):
                    pieces.append((site, candidate.sql))
                    break
        if pieces:
            candidates.extend(_with_pieces(plain_query, pieces))

    tried = 0
    for candidate in candidates:
        replacement = sqlglot.parse_one(candidate.sql, read="postgres")
        tried += 1
        if _captures_model_reads(replacement, model_name, named_model):
            continue  # the model's definition would read the replacement's WITH table instead of its own source
        replacement_sql = _inline(replacement, model_name, named_model)
        try:
            result = prove_equivalent_algebraic(
                plain_query_sql,
                replacement_sql,
                schema=base_schema,
                constraints=dict(constraints) if constraints else None,
                types={t: dict(c) for t, c in types.items()} if types else None,
                timeout_ms=timeout_ms,
                dialect=dialect,
                compare_names=False,
                exact_arithmetic=exact_arithmetic,
            )
        except Exception:  # noqa: BLE001 - a prover crash is never a proof
            continue
        if result.status is SmtStatus.PROVEN_EQUIVALENT:
            assumptions = tuple(result.assumptions)
            if query_has_order(plain_query):
                assumptions += (ORDER_ASSUMPTION,)
            return ModelReuse("rewritten", "proven equivalent to the query with the model's definition substituted", candidate.sql, candidate.strategy, assumptions, tuple(names), tried, replacement_sql, plain_query_sql)
    if opaque:
        return ModelReuse("unsupported", opaque, model_columns=tuple(names), candidates_tried=tried)
    if tried <= 1 and whole.reason:
        return ModelReuse("unsupported", whole.reason, model_columns=tuple(names), candidates_tried=tried)
    return ModelReuse("no_rewrite", f"{tried} candidate replacement(s) were not proven equivalent", model_columns=tuple(names), candidates_tried=tried)


ORDER_ASSUMPTION = "the rows are compared as a bag; ORDER BY keys are carried over but the order itself is not proven"


def query_has_order(tree: exp.Expression) -> bool:
    return isinstance(tree, exp.Query) and bool(tree.args.get("order"))


def _captures_model_reads(replacement: exp.Expression, model_name: str, named_model: str) -> bool:
    """Where ``replacement`` reads the model, a WITH table in scope shares the name of a table the model reads."""

    if not any(replacement.find_all(exp.CTE)):
        return False
    model = sqlglot.parse_one(named_model, read="postgres")
    return any(
        captured_names(model, table)
        for table in replacement.find_all(exp.Table)
        if table.name == model_name and not table.args.get("db")
    )


def _inline(replacement: exp.Expression, model_name: str, named_model: str) -> str:
    """The replacement with the model's definition substituted for its name."""

    tree = replacement.copy()
    for table in list(tree.find_all(exp.Table)):
        if table.name == model_name and not table.args.get("db"):
            alias = table.alias or model_name
            table.replace(exp.Subquery(this=sqlglot.parse_one(named_model, read="postgres"), alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree.sql(dialect="postgres")


def _with_pieces(plain_query: exp.Expression, pieces: list[tuple[exp.Select, str]]):
    """The query with all replaceable pieces swapped, then each swap alone."""

    def swapped(chosen: list[tuple[exp.Select, str]]) -> _Candidate | None:
        tree = plain_query.copy()
        # map each chosen site to its copy by position in a walk of the copy
        original = list(plain_query.find_all(exp.Select))
        clone = list(tree.find_all(exp.Select))
        index = {id(node): i for i, node in enumerate(original)}
        for site, sql in chosen:
            target = clone[index[id(site)]]
            replacement = sqlglot.parse_one(sql, read="postgres")
            target.replace(replacement)
        return _Candidate(tree.sql(dialect="postgres"), "partial:" + "+".join(sorted({"nested"})))

    out = []
    # outermost sites first; skip a site whose ancestor is already chosen
    chosen: list[tuple[exp.Select, str]] = []
    for site, sql in pieces:
        if any(site is not other and _inside(site, other) for other, _ in chosen):
            continue
        chosen.append((site, sql))
    candidate = swapped(chosen)
    if candidate:
        out.append(candidate)
    if len(chosen) > 1:
        for one in chosen:
            candidate = swapped([one])
            if candidate:
                out.append(candidate)
    return out


@dataclass(frozen=True)
class ReplacementCheck:
    """The verdict on a replacement someone else proposed (a person, a tool, a language model)."""

    status: str  # proven | refuted | unknown | unsupported
    reason: str
    witness: object | None = None  # a random_check.Witness when refuted
    assumptions: tuple[str, ...] = ()


def check_replacement(
    query_sql: str,
    model_sql: str,
    replacement_sql: str,
    *,
    schema: Mapping[str, Sequence[str]],
    constraints: Mapping[str, TableConstraints] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    model_name: str = "mv0",
    dialect: str = "postgres",
    timeout_ms: int = 5000,
    database=None,
    trials: int = 200,
) -> ReplacementCheck:
    """Is ``replacement_sql`` (reading ``model_name``) equal to ``query_sql`` once the model is substituted?

    ``proven`` carries the prover's assumptions. With ``database`` (a ``random_check.Schema``) a replacement that
    fails to prove is searched for a counterexample database and is ``refuted`` when one exists.
    """

    from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic

    try:
        model_tree = _prepare(model_sql, schema, dialect)
        model = _block(model_tree, not_null_columns(constraints))
        names = _output_names(model)
        plain_model, plain_query = _plain(model_sql, schema, dialect), _plain(query_sql, schema, dialect)
    except _Unsupported as error:
        return ReplacementCheck("unsupported", f"model: {error}")
    except sqlglot.errors.SqlglotError as error:
        return ReplacementCheck("unsupported", f"parse error: {error}")
    named_model = _named_model_sql(model, names, model_tree, plain_model)
    try:
        inlined = _inline(sqlglot.parse_one(replacement_sql, read=dialect), model_name, named_model)
    except sqlglot.errors.SqlglotError as error:
        return ReplacementCheck("unsupported", f"parse error in the replacement: {error}")
    plain_query_sql = plain_query.sql(dialect="postgres")
    base = {t.lower(): [c.lower() for c in cols] for t, cols in schema.items()}
    try:
        result = prove_equivalent_algebraic(
            plain_query_sql,
            inlined,
            schema=base,
            constraints=dict(constraints) if constraints else None,
            types={t: dict(c) for t, c in types.items()} if types else None,
            timeout_ms=timeout_ms,
            dialect="postgres",
            compare_names=False,
        )
    except Exception as error:  # noqa: BLE001
        result = None
        reason = f"prover error: {type(error).__name__}"
    else:
        reason = result.reason
    if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
        return ReplacementCheck("proven", reason, None, tuple(result.assumptions))
    if database is not None:
        from .random_check import CheckError, find_difference

        try:
            witness = find_difference(database, plain_query_sql, inlined, mode="bag", trials=trials)
        except CheckError as error:
            return ReplacementCheck("unknown", f"not proven, and the search could not run: {error}")
        if witness is not None:
            return ReplacementCheck("refuted", "a database on which the two return different rows", witness)
    return ReplacementCheck("unknown", reason)
