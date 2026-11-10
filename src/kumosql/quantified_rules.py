"""Quantified comparisons (``x op ANY/SOME/ALL (subquery)``) and Calcite's expansions of them.

* ``x op ANY (q)`` is TRUE when some row of ``q`` compares TRUE with ``x``, FALSE when every
  row compares FALSE (so an empty ``q`` gives FALSE), and NULL otherwise. ``x op ALL (q)`` is
  the dual. Both are written with ``EXISTS``, which the rest of the normalizer and the
  prover already model: in a WHERE, HAVING or ON condition only the test's TRUE (or, under
  NOT, its FALSE) matters, so it becomes one ``EXISTS``; elsewhere it becomes
  ``CASE WHEN <TRUE test> THEN TRUE WHEN <not-FALSE test> THEN NULL ELSE FALSE END``.
  ``= ANY`` is ``IN`` and ``<> ALL`` is ``NOT IN``.
* Calcite removes such subqueries by joining a one-row global aggregate of the subquery
  (``MIN``/``MAX``, ``COUNT(*)``, ``COUNT(y)``) and, for ``IN``, a grouped indicator
  ``(SELECT y, TRUE FROM q GROUP BY y)`` LEFT JOINed on ``x = y``, then reading them in a
  three-valued ``CASE`` or AND/OR condition. A condition that reads such columns is folded back
  to ``x op ANY (q)``, ``x op ALL (q)`` or ``x IN (q)`` only when a small z3 check proves it
  has the same value (TRUE, FALSE or NULL; or the same TRUE/FALSE where only that matters)
  for every possible aggregate state, so a differently written or wrong expansion is left as is.
  The same check reads Calcite's constant ``c IN (q)``, the first row of ``(y IS NOT NULL, COUNT(*))`` grouped over
  the rows of ``q`` where ``y = c OR y IS NULL``, as ``c IN (q)``.
"""

from __future__ import annotations

import itertools

try:
    import z3
except ImportError:  # z3-solver is an optional extra; the rules below are reached only from a prover that has it
    z3 = None
from sqlglot import exp

from .domain_join_rules import lateral_aggregates
from .solver_lock import bounded_solver, serialized

_OPS = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.EQ: "=", exp.NEQ: "<>"}
_CLASS = {op: cls for cls, op in _OPS.items()}
_NEGATE = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "=": "<>", "<>": "="}
_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "=": "=", "<>": "<>"}
_VALUE_NAME = "kumosql_v"


@serialized
def rewrite_quantified(tree: exp.Expression, schema: dict | None = None, not_null: dict | None = None, keys: dict | None = None) -> exp.Expression:
    """Fold Calcite's expansions back to quantified tests, then write every quantified test with EXISTS."""
    if z3 is None:  # the solver decides this rewrite; without it the query stays as written
        return tree

    tree = fold_expansions(tree, schema, not_null, keys)
    return lower_quantified(tree)


# --------------------------------------------------------------------------------------------
# ANY / ALL as EXISTS


def _mode(node: exp.Expression) -> str:
    """``pos`` when only whether ``node`` is TRUE matters, ``neg`` when only whether it is FALSE does."""

    negated = False
    current = node
    while True:
        parent = current.parent
        if isinstance(parent, (exp.Paren, exp.And, exp.Or)):
            current = parent
            continue
        if isinstance(parent, exp.Not):
            negated = not negated
            current = parent
            continue
        if isinstance(parent, (exp.Where, exp.Having)) and current.arg_key == "this":
            return "neg" if negated else "pos"
        if isinstance(parent, exp.Join) and current.arg_key == "on":
            return "neg" if negated else "pos"
        return "full"


def _query_select(node: exp.Expression) -> exp.Select | None:
    while isinstance(node, exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def _compare(op: str, left: exp.Expression, right: exp.Expression) -> exp.Expression:
    return _CLASS[op](this=left, expression=right)


def _operand(node: exp.Expression) -> exp.Expression:
    node = node.copy()
    return node if isinstance(node, (exp.Column, exp.Literal, exp.Paren, exp.Null, exp.Boolean)) else exp.Paren(this=node)


def _or(*parts: exp.Expression) -> exp.Expression:
    result = parts[0]
    for part in parts[1:]:
        result = exp.Or(this=result, expression=part)
    return result


_VOLATILE = {"random", "rand", "uuid", "gen_random_uuid", "generate_uuid", "newid", "now", "current_timestamp", "clock_timestamp"}


def _volatile(node: exp.Expression) -> bool:
    """Whether evaluating ``node`` twice may give two different values."""

    return any(
        isinstance(n, exp.Rand) or (isinstance(n, exp.Anonymous) and str(n.this).lower() in _VOLATILE)
        for n in node.find_all(exp.Rand, exp.Anonymous)
    )


def _plain_group(group: exp.Expression | None) -> bool:
    """A GROUP BY of plain expressions: each group occurs once (no GROUPING SETS, ROLLUP, CUBE or ALL)."""

    if group is None:
        return True
    if any(group.args.get(k) for k in ("grouping_sets", "rollup", "cube", "all", "totals")):
        return False
    extensions = tuple(getattr(exp, n) for n in ("GroupingSets", "Rollup", "Cube") if hasattr(exp, n))
    return not any(isinstance(e, extensions) for e in group.expressions)


@serialized
def lower_quantified(tree: exp.Expression) -> exp.Expression:
    """Write each ``x op ANY/ALL (SELECT y ...)`` with EXISTS (see the module docstring)."""
    if z3 is None:  # the solver decides this rewrite; without it the query stays as written
        return tree

    # Every identifier is reserved (aliases, table and CTE names, columns), so a generated alias can
    # never shadow a relation the comparison reads.
    taken = {i.name.lower() for i in tree.find_all(exp.Identifier)}
    counter = itertools.count()

    def fresh() -> str:
        while True:
            name = f"kumosql_q{next(counter)}"
            if name not in taken:
                taken.add(name)
                return name

    # NOT (x op ANY q) is x <negated op> ALL q, and the other way round: one spelling for both.
    for node in list(tree.find_all(exp.Any, exp.All)):
        comparison = node.parent
        if type(comparison) not in _OPS or comparison.args.get("expression") is not node:
            continue
        outer = comparison.parent
        while isinstance(outer, exp.Paren):
            outer = outer.parent
        if isinstance(outer, exp.Not):
            dual = (exp.Any if isinstance(node, exp.All) else exp.All)(this=node.this)
            outer.replace(_compare(_NEGATE[_OPS[type(comparison)]], comparison.this, dual))

    # Innermost first, so a copied subquery carries its own nested tests already rewritten.
    for node in list(tree.find_all(exp.Any, exp.All))[::-1]:
        comparison = node.parent
        if type(comparison) not in _OPS or comparison.args.get("expression") is not node:
            continue
        body = _query_select(node.this)
        if body is None or len(body.expressions) != 1 or isinstance(body.expressions[0].unalias(), exp.Star):
            continue
        op = _OPS[type(comparison)]
        quantifier_all = isinstance(node, exp.All)
        x = comparison.this
        if (op, quantifier_all) in (("=", False), ("<>", True)):
            test = exp.In(this=_operand(x), query=exp.Subquery(this=body.copy()))
            replacement = exp.Paren(this=exp.Not(this=test)) if quantifier_all else test
            comparison.replace(replacement)
            continue
        # The output is renamed to one name, so two spellings of a subquery lower alike, unless a clause
        # that may name the output alias (ORDER BY, GROUP BY, HAVING, QUALIFY, WINDOW) would then read
        # something else: there the probe reads the output by its own name.
        inner = body.copy()
        item = inner.expressions[0]
        name = None
        if any(inner.args.get(k) for k in ("order", "group", "having", "qualify", "windows")):
            name = item.args.get("alias") if isinstance(item, exp.Alias) else item.this if isinstance(item, exp.Column) else None
            if not isinstance(name, exp.Identifier) or not name.name:
                name = None
        if name is None:
            if any(c.name.lower() == _VALUE_NAME for c in inner.find_all(exp.Column)):
                continue
            inner.set("expressions", [exp.alias_(item.copy(), _VALUE_NAME)])
            name = exp.to_identifier(_VALUE_NAME)
        if _mode(comparison) == "full" and (_volatile(x) or _volatile(inner)):
            continue  # the full value reads x and the subquery twice

        def probe(condition) -> exp.Exists:
            alias = fresh()
            value = exp.Column(this=name.copy(), table=exp.to_identifier(alias))
            select = exp.Select(expressions=[exp.Literal.number(1)]).from_(
                exp.Subquery(this=inner.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias)))
            )
            select.set("where", exp.Where(this=condition(value)))
            return exp.Exists(this=select)

        def unknown_or(value, compare_op):
            return _or(
                exp.Is(this=_operand(x), expression=exp.Null()),
                exp.Is(this=value.copy(), expression=exp.Null()),
                _compare(compare_op, _operand(x), value),
            )

        if quantifier_all:
            negated = _NEGATE[op]
            true_test = exp.Not(this=probe(lambda v: unknown_or(v, negated)))
            not_false_test = exp.Not(this=probe(lambda v: _compare(negated, _operand(x), v)))
        else:
            true_test = probe(lambda v: _compare(op, _operand(x), v))
            not_false_test = probe(lambda v: unknown_or(v, op))
        mode = _mode(comparison)
        if mode == "pos":
            replacement = true_test
        elif mode == "neg":
            replacement = not_false_test
        else:
            replacement = exp.Case(
                ifs=[exp.If(this=true_test, true=exp.true()), exp.If(this=not_false_test, true=exp.Null())],
                default=exp.false(),
            )
            while isinstance(comparison.parent, exp.Paren):  # a CASE needs no parentheses
                comparison = comparison.parent
        comparison.replace(replacement)
    return tree


# --------------------------------------------------------------------------------------------
# Column lineage through derived tables


def _alias_of(node: exp.Expression) -> str:
    return (node.alias_or_name or "").lower()


def _one_row(node: exp.Expression) -> bool:
    """A derived global aggregate (or a projection of one) always has exactly one row."""

    if isinstance(node, exp.Lateral):
        node = node.this
    select = _query_select(node)
    if select is None or any(select.args.get(k) for k in ("group", "having", "qualify", "limit", "offset", "joins", "laterals", "windows")):
        return False
    if _global_aggregate(select):
        return True
    from_ = select.args.get("from_") or select.args.get("from")
    return from_ is not None and select.args.get("where") is None and isinstance(from_.this, exp.Subquery) and _one_row(from_.this)


def _global_aggregate(select: exp.Select) -> bool:
    if any(select.args.get(k) for k in ("group", "having", "qualify", "limit", "offset", "windows")):
        return False
    return any(
        agg.find_ancestor(exp.Select) is select and agg.find_ancestor(exp.Window) is None
        for item in select.expressions
        for agg in item.find_all(exp.AggFunc)
    )


def _true(node: exp.Expression | None) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    return isinstance(node, exp.Boolean) and bool(node.this)


def _source_map(select: exp.Select) -> dict[str, tuple[exp.Expression, bool, exp.Join | None]] | None:
    """alias -> (source, may be NULL-extended, join) for the FROM and JOIN items of ``select``."""

    from_ = select.args.get("from_") or select.args.get("from")
    joins = select.args.get("joins") or []
    if select.args.get("laterals"):
        return None
    entries = ([(from_.this, None)] if from_ is not None else []) + [(j.this, j) for j in joins]
    nullable = [False] * len(entries)
    for index, (source, join) in enumerate(entries):
        if join is None:
            continue
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if kind in ("SEMI", "ANTI") or join.args.get("using"):
            return None
        if side == "LEFT":
            if not (_true(join.args.get("on")) and _one_row(source)):
                nullable[index] = True
        elif side == "RIGHT":
            for earlier in range(index):
                nullable[earlier] = True
        elif side == "FULL":
            for earlier in range(index + 1):
                nullable[earlier] = True
    found: dict[str, tuple] = {}
    for (source, join), null in zip(entries, nullable):
        name = _alias_of(source)
        if not name or name in found:
            return None
        found[name] = (source, null, join)
    return found


def _outputs(select: exp.Select, name: str) -> exp.Expression | None:
    hits = [e for e in select.expressions if e.alias_or_name.lower() == name]
    return hits[0] if len(hits) == 1 else None


class _Lineage:
    """What a column of a select reads: a base column or an expression, and the derived tables on the way."""

    __slots__ = ("expr", "select", "table", "column", "nullable", "hops")

    def __init__(self, expr, select, table, column, nullable, hops):
        self.expr, self.select, self.table, self.column, self.nullable, self.hops = expr, select, table, column, nullable, hops


def _table_columns(schema: dict | None, table: exp.Table) -> set[str] | None:
    if not schema or table.args.get("db") or table.args.get("catalog"):
        return None
    for name, columns in schema.items():
        if name.lower() == table.name.lower():
            return {c.lower() for c in columns}
    return None


def _local_source(column: exp.Column, select: exp.Select, schema: dict | None):
    """The source of ``select`` that an unqualified ``column`` reads, if that is certain."""

    sources = _source_map(select)
    if sources is None:
        return None
    name = column.name.lower()
    providers = []
    for alias, (source, _null, _join) in sources.items():
        if isinstance(source, exp.Table):
            columns = _table_columns(schema, source)
            if columns is None:
                return None
            if name in columns:
                providers.append(alias)
        else:
            body = _query_select(source.this if isinstance(source, exp.Lateral) else source)
            if body is None:
                return None
            if _outputs(body, name) is not None:
                providers.append(alias)
    return providers[0] if len(providers) == 1 else None


def _resolve(column: exp.Column, select: exp.Select, schema: dict | None, nullable: bool = False, hops: tuple = ()) -> _Lineage | None:
    sources = _source_map(select)
    if sources is None:
        return None
    alias = column.table.lower() if column.table else _local_source(column, select, schema)
    if not alias or alias not in sources:
        return None
    source, null, _join = sources[alias]
    nullable = nullable or null
    if isinstance(source, exp.Table):
        return _Lineage(None, select, source, column.name.lower(), nullable, hops)
    body = _query_select(source.this if isinstance(source, exp.Lateral) else source)
    if body is None:
        return None
    item = _outputs(body, column.name.lower())
    if item is None:
        return None
    expr = item.unalias()
    hops = hops + ((select, source),)
    if isinstance(expr, exp.Column):
        return _resolve(expr, body, schema, nullable, hops)
    return _Lineage(expr, body, None, None, nullable, hops)


def _never_null(expr: exp.Expression, select: exp.Select, schema: dict | None, not_null: dict) -> bool:
    while isinstance(expr, exp.Paren):
        expr = expr.this
    if isinstance(expr, exp.Literal):
        return True
    if isinstance(expr, exp.Boolean):
        return True
    if not isinstance(expr, exp.Column):
        return False
    lineage = _resolve(expr, select, schema)
    if lineage is None or lineage.nullable:
        return False
    if lineage.table is not None:
        if lineage.table.args.get("db") or lineage.table.args.get("catalog"):
            return False
        return lineage.column in not_null.get(lineage.table.name.lower(), set())
    return _never_null(lineage.expr, lineage.select, schema, not_null)


def _scope_columns(select: exp.Select) -> list[exp.Column]:
    """Columns that ``select`` itself reads (not those of its derived tables or subqueries)."""

    return [c for c in select.find_all(exp.Column) if c.find_ancestor(exp.Select) is select]


def _binding(column: exp.Column, schema: dict | None) -> exp.Select | None:
    """The select whose FROM/JOIN item ``column`` reads."""

    select = column.find_ancestor(exp.Select)
    while select is not None:
        sources = _source_map(select)
        if sources is None:
            return None
        if column.table:
            if column.table.lower() in sources:
                return select
        elif _local_source(column, select, schema) is not None:
            return select
        select = select.find_ancestor(exp.Select)
    return None


def _lift(expr: exp.Expression, hops: tuple, depth: int) -> exp.Expression | None:
    """Rewrite ``expr``, written in the scope of ``hops[depth - 1]``'s derived body, in the scope of ``hops[0][0]``.

    Each column must be passed through unchanged by every derived table on the way up.
    """

    expr = expr.copy()
    for level in range(depth - 1, -1, -1):
        outer, source = hops[level]
        body = _query_select(source.this if isinstance(source, exp.Lateral) else source)
        alias = _alias_of(source)
        for column in list(expr.find_all(exp.Column)) if not isinstance(expr, exp.Column) else [expr]:
            match = [
                e for e in body.expressions
                if isinstance(e.unalias(), exp.Column) and e.unalias().sql().lower() == column.sql().lower()
            ]
            if not match:
                return None
            lifted = exp.column(match[0].alias_or_name, table=alias)
            if column is expr:
                expr = lifted
            else:
                column.replace(lifted)
    return expr


# --------------------------------------------------------------------------------------------
# Folding Calcite's expansions


class _Group:
    """The one-row aggregate (``agg``) or grouped indicator (``ind``) a set of columns reads."""

    def __init__(self, kind: str, select: exp.Select, hops: tuple):
        self.kind, self.select, self.hops = kind, select, hops
        self.y: exp.Expression | None = None
        self.x: exp.Expression | None = None  # indicator: the outer value, lifted to the reading select
        self.key: str | None = None  # canonical SQL of SELECT y FROM <rows>


def _aggregate_role(agg: exp.Expression) -> tuple[str, exp.Expression | None] | None:
    if isinstance(agg, exp.Count):
        arg = agg.this
        if isinstance(arg, exp.Star):
            return "c", None
        if isinstance(arg, exp.Distinct):
            if len(arg.expressions) != 1:
                return None
            return "d", arg.expressions[0]
        return "ck", arg
    if isinstance(agg, exp.Min):
        return "mn", agg.this
    if isinstance(agg, exp.Max):
        return "mx", agg.this
    return None


def _rows_select(select: exp.Select, y: exp.Expression, hops: tuple, schema: dict | None, drop_group: bool) -> exp.Select | None:
    """``SELECT y FROM <select's rows>`` written in the scope of ``hops[0][0]`` (correlations lifted)."""

    copy = select.copy()
    pairs = list(zip(select.walk(), copy.walk()))
    y_copy = next((d for o, d in pairs if o is y), None)
    if y_copy is None:
        return None
    # Columns that read a select outside ``select`` are correlations; lift them up the hops.
    selects_on_path = [outer for outer, _ in hops]
    replacements = []
    for original, duplicate in pairs:
        if not isinstance(original, exp.Column):
            continue
        bound = _binding(original, schema)
        if bound is None:
            return None
        if _inside(bound, select):
            continue
        if bound not in selects_on_path:
            return None
        depth = selects_on_path.index(bound)
        lifted = _lift(original, hops, depth) if depth else original.copy()
        if lifted is None:
            return None
        replacements.append((duplicate, lifted))
    for duplicate, lifted in replacements:
        if duplicate is y_copy:
            y_copy = lifted
        duplicate.replace(lifted)
    copy.set("expressions", [exp.alias_(y_copy.copy(), _VALUE_NAME)])
    if drop_group:
        copy.set("group", None)
    return copy


def _inside(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _canonical(select: exp.Select) -> str:
    """Text that two writings of the same rows share: renaming layers removed, aliases canonical."""

    from .smt_equivalence import _canonical_aliases

    return _canonical_aliases(_collapse(select.copy())).sql(dialect="bigquery")


_EXTRAS = ("where", "group", "having", "qualify", "distinct", "limit", "offset", "order", "joins", "laterals", "windows", "with", "with_")


def _renaming(select: exp.Select | None) -> bool:
    """A select that only lists columns of its one source."""

    if select is None or any(select.args.get(k) for k in _EXTRAS):
        return False
    from_ = select.args.get("from_") or select.args.get("from")
    return from_ is not None and all(isinstance(e.unalias(), exp.Column) for e in select.expressions)


def _collapse(select: exp.Select) -> exp.Select:
    # An outer renaming of a derived select is that select with its outputs renamed.
    while _renaming(select):
        source = (select.args.get("from_") or select.args.get("from")).this
        inner = _query_select(source) if isinstance(source, exp.Subquery) else None
        alias = _alias_of(source)
        if inner is None or any(e.unalias().table.lower() != alias for e in select.expressions):
            break
        items = []
        for item in select.expressions:
            match = _outputs(inner, item.unalias().name.lower())
            if match is None:
                return select
            items.append(exp.alias_(match.unalias().copy(), item.alias_or_name))
        select = inner.copy()
        select.set("expressions", items)
    # A derived renaming of a table is the table, read by the original column names.
    for sub in list(select.find_all(exp.Subquery)):
        inner = sub.this if isinstance(sub.this, exp.Select) else None
        if not _renaming(inner) or not isinstance(sub.parent, (exp.From, exp.Join)):
            continue
        table = (inner.args.get("from_") or inner.args.get("from")).this
        if not isinstance(table, exp.Table) or table.alias or not sub.alias:
            continue
        outer = sub.find_ancestor(exp.Select)
        alias = _alias_of(sub)
        names = {e.alias_or_name.lower(): e.unalias().name for e in inner.expressions}
        readers = [c for c in outer.find_all(exp.Column) if not _inside(c, sub) and (c.table.lower() == alias or not c.table)]
        # Only qualified reads in the outer select itself are renamed: a reader in a deeper select, or an
        # unqualified one, would keep the old output name and could bind to another column.
        if any(not c.table or c.find_ancestor(exp.Select) is not outer for c in readers):
            continue
        if any(c.name.lower() not in names for c in readers):
            continue
        for column in readers:
            column.set("this", exp.to_identifier(names[column.name.lower()].lower()))
        renamed = table.copy()  # keeps TABLESAMPLE, FOR SYSTEM_TIME and every other table argument
        renamed.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        sub.replace(renamed)
    return select


def _path_null(hops: tuple, upto: int) -> bool:
    """Whether a derived table among the first ``upto`` hops may be NULL-extended."""

    for outer, source in hops[:upto]:
        sources = _source_map(outer)
        if sources is None:
            return True
        entry = sources.get(_alias_of(source))
        if entry is None or entry[1]:
            return True
    return False


def _classify(column: exp.Column, select: exp.Select, schema, not_null, groups: dict) -> tuple | None:
    """``(group, role)`` when ``column`` reads Calcite's one-row aggregate or IN indicator."""

    lineage = _resolve(column, select, schema)
    if lineage is None or lineage.expr is None or not lineage.hops:
        return None
    expr, body, hops = lineage.expr, lineage.select, lineage.hops
    if any(_query_select(source.this if isinstance(source, exp.Lateral) else source).args.get("limit") for _, source in hops):
        return _probe(lineage, schema, groups)
    over_aggregates = not isinstance(expr, exp.AggFunc) and _reads_aggregates(expr, body)
    if (isinstance(expr, exp.AggFunc) or over_aggregates) and expr.parent is not None and _global_aggregate(body):
        if lineage.nullable or body.args.get("distinct"):
            return None
        role = ("expr", expr) if over_aggregates else _aggregate_role(expr)
        aggregates = [a for i in body.expressions for a in i.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is body]
        if role is None or not all(_aggregate_role(a) for a in aggregates) or not all(_aggregate_item(i.unalias(), body) for i in body.expressions):
            return None
        group = groups.get(id(body))
        if group is None:
            group = groups[id(body)] = _Group("agg", body, hops)
            args = [a for agg in aggregates for r, a in [_aggregate_role(agg)] if a is not None]
            if len({a.sql() for a in args}) > 1:
                group.key = None
                group.y = None
            else:
                arg = args[0] if args else None
                group.y = arg
                if arg is not None:
                    rows = _rows_select(body, arg, hops, schema, drop_group=False)
                    group.key = _canonical(rows) if rows is not None else None
                    group.rows = rows
        if group.y is None or group.key is None:
            return None
        return group, role if over_aggregates else role[0]
    if _true(expr) and isinstance(expr, exp.Boolean):
        # TRUE read through a projection of a one-row aggregate is always TRUE.
        if not lineage.nullable and len(hops) >= 1 and _one_row(hops[-1][1]):
            return None, "true"
        # Calcite's IN indicator: (SELECT y AS k, TRUE AS i FROM rows GROUP BY y) LEFT JOINed ON x = k.
        return _indicator(body, hops, schema, not_null, groups)
    return None


def _outside_columns(expr: exp.Expression, body: exp.Select) -> list[exp.Column]:
    """The columns of ``expr`` outside its aggregates."""

    return [c for c in expr.find_all(exp.Column) if not any(a.find_ancestor(exp.Select) is body for a in _ancestors(c, expr) if isinstance(a, exp.AggFunc))]


def _ancestors(node: exp.Expression, root: exp.Expression):
    node = node.parent
    while node is not None:
        yield node
        if node is root:
            return
        node = node.parent


def _correlated(column: exp.Column, body: exp.Select) -> bool:
    """A qualified column naming no source of ``body``: a value of an outer row."""

    sources = _source_map(body)
    return bool(column.table) and sources is not None and column.table.lower() not in sources


def _aggregate_item(expr: exp.Expression, body: exp.Select) -> bool:
    """An item of a one-row aggregate the encoder can read: aggregates, constants and outer values."""

    if any(n.find_ancestor(exp.Select) is body for n in expr.find_all(exp.Window, exp.Subquery, exp.Star) if not isinstance(n.parent, exp.Count)):
        return False
    return all(_correlated(c, body) for c in _outside_columns(expr, body))


def _reads_aggregates(expr: exp.Expression, body: exp.Select) -> bool:
    """An expression over ``body``'s aggregates (Calcite's NULL-when-empty and COALESCE wrappers)."""

    if isinstance(expr, (exp.Column, exp.Boolean)):
        return False
    return any(a.find_ancestor(exp.Select) is body for a in expr.find_all(exp.AggFunc)) and _aggregate_item(expr, body)


def _indicator(body: exp.Select, hops: tuple, schema, not_null, groups: dict) -> tuple | None:
    # The indicator need not be grouped: each row of the reading select then pairs with every
    # match, but whether a match exists, which is all the condition reads, is the same.
    group_by = body.args.get("group")
    if not _plain_group(group_by):
        return None
    if len(body.expressions) != 2 or any(body.args.get(k) for k in ("having", "qualify", "limit", "offset", "windows")):
        return None
    # Constants in the GROUP BY (Calcite writes ``GROUP BY y, TRUE``) do not split groups.
    grouped = [g for g in group_by.expressions if not isinstance(g, (exp.Boolean, exp.Literal))] if group_by is not None else None
    if grouped is not None and len(grouped) != 1:
        return None
    if any(a.find_ancestor(exp.Select) is body for e in body.expressions for a in e.find_all(exp.AggFunc, exp.Window)):
        return None
    keys = [e for e in body.expressions if not isinstance(e.unalias(), exp.Boolean)]
    if len(keys) != 1 or (grouped is not None and keys[0].unalias().sql() != grouped[0].sql()):
        return None
    y = keys[0].unalias()
    key_name = keys[0].alias_or_name.lower()
    found = _indicator_join(hops, key_name)
    if found is None:
        return None
    x, depth = found
    group = groups.get(id(body))
    if group is None:
        group = groups[id(body)] = _Group("ind", body, hops)
        rows = _rows_select(body, y, hops, schema, drop_group=True)
        group.key = _canonical(rows) if rows is not None else None
        group.rows = rows
        group.y = y
        group.x = _lift(x, hops, depth) if depth else x.copy()
        group.x_null = None
    if group.key is None or group.x is None:
        return None
    return group, "i"


def _indicator_join(hops: tuple, key_name: str) -> tuple[exp.Expression, int] | None:
    """``(x, depth)`` when the indicator is matched on ``x = key``: in the ON of a LEFT JOIN, or in the
    WHERE of a LEFT JOIN LATERAL ... ON TRUE that reads only the indicator. ``x`` is written in the scope of
    ``hops[depth][0]``.
    """

    outer, source = hops[-1]
    sources = _source_map(outer)
    if sources is None:
        return None
    entry = sources.get(_alias_of(source))
    if entry is None:
        return None
    alias = _alias_of(source)
    if entry[2] is not None:
        if (entry[2].args.get("side") or "").upper() != "LEFT" or _path_null(hops, len(hops) - 1):
            return None
        on, depth = entry[2].args.get("on"), len(hops) - 1
    else:
        # SELECT a.k, a.i FROM (indicator) AS a WHERE x = a.k, LEFT JOINed LATERAL ON TRUE.
        if len(hops) < 2 or len(sources) != 1 or any(outer.args.get(k) for k in _ROW_CHANGING + ("joins",)):
            return None
        if any(not isinstance(e.unalias(), exp.Column) for e in outer.expressions):
            return None
        reader, lateral = hops[-2]
        if not isinstance(lateral, exp.Lateral) or _query_select(lateral.this) is not outer:
            return None
        lateral_entry = (_source_map(reader) or {}).get(_alias_of(lateral))
        join = lateral_entry[2] if lateral_entry else None
        if join is None or (join.args.get("side") or "").upper() != "LEFT" or not _true(join.args.get("on")) or _path_null(hops, len(hops) - 2):
            return None
        where = outer.args.get("where")
        on, depth = (where.this if where is not None else None), len(hops) - 2
    on = _unparen(on)
    if not isinstance(on, exp.EQ):
        return None

    def is_key(side):
        return isinstance(side, exp.Column) and side.table.lower() == alias and side.name.lower() == key_name

    if is_key(on.this):
        x = on.expression
    elif is_key(on.expression):
        x = on.this
    else:
        return None
    if any(c.table.lower() == alias for c in x.find_all(exp.Column)) or any(not c.table for c in x.find_all(exp.Column)):
        return None
    return x, depth


def _unparen(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _limit_one(select: exp.Select) -> bool:
    limit = select.args.get("limit")
    value = limit.expression if isinstance(limit, exp.Limit) else None
    return not select.args.get("offset") and isinstance(value, exp.Literal) and not value.is_string and value.this == "1"


_ROW_CHANGING = ("group", "having", "distinct", "qualify", "windows", "limit", "offset", "order", "laterals")


def _keeps_rows(select: exp.Select) -> bool:
    """A plain projection/filter/join: no grouping, DISTINCT, LIMIT or aggregate of its own."""

    if any(select.args.get(k) for k in _ROW_CHANGING):
        return False
    return not any(a.find_ancestor(exp.Select) is select for e in select.expressions for a in e.find_all(exp.AggFunc, exp.Window))


def _probe(lineage: _Lineage, schema, groups: dict) -> tuple | None:
    """Calcite's ``c IN (q)`` for a constant ``c``: the first row of
    ``SELECT y IS NOT NULL AS g, COUNT(*) AS n FROM q WHERE c = y OR y IS NULL GROUP BY g ORDER BY g IS NULL DESC, g DESC LIMIT 1``
    LEFT JOINed ON TRUE. ``g`` is TRUE when some ``y = c``, FALSE when there is none but a NULL ``y``,
    and both columns are NULL when there is neither.
    """

    hops = lineage.hops
    index = next(i for i, (_, source) in enumerate(hops) if _query_select(source.this if isinstance(source, exp.Lateral) else source).args.get("limit"))
    outer, source = hops[index]
    if index + 1 >= len(hops) or _path_null(hops, index):
        return None
    top = _query_select(source.this if isinstance(source, exp.Lateral) else source)
    sources = _source_map(outer)
    entry = sources.get(_alias_of(source)) if sources else None
    join = entry[2] if entry else None
    if join is None or (join.args.get("side") or "").upper() != "LEFT" or not _true(join.args.get("on")):
        return None
    if not _limit_one(top) or any(top.args.get(k) for k in ("where", "group", "having", "joins", "laterals", "distinct", "qualify", "windows")):
        return None
    if hops[index + 1][0] is not top:
        return None
    counted_source = hops[index + 1][1]
    counted = _query_select(counted_source)
    alias = _alias_of(counted_source)
    group = counted.args.get("group") if counted is not None else None
    if not _plain_group(group):
        return None
    if group is None or len(group.expressions) != 1 or any(counted.args.get(k) for k in ("where", "having", "joins", "laterals", "distinct", "qualify", "windows", "order", "limit", "offset")):
        return None
    key = group.expressions[0]
    if not isinstance(key, exp.Column) or len(counted.expressions) not in (1, 2):
        return None
    g_items = [e for e in counted.expressions if isinstance(e.unalias(), exp.Column) and e.unalias().sql() == key.sql()]
    n_items = [e for e in counted.expressions if isinstance(e.unalias(), exp.Count) and isinstance(e.unalias().this, exp.Star)]
    if len(g_items) != 1 or len(g_items) + len(n_items) != len(counted.expressions):
        return None
    g_name = g_items[0].alias_or_name.lower()
    if any(not isinstance(e.unalias(), exp.Column) or e.unalias().table.lower() != alias for e in top.expressions):
        return None
    order = top.args.get("order")
    keys = [o for o in order.expressions] if order is not None else []

    def reads_g(node):
        node = _unparen(node)
        return isinstance(node, exp.Column) and node.table.lower() == alias and node.name.lower() == g_name

    # TRUE first; g is never NULL, so a leading ``g IS NULL DESC`` changes nothing.
    if not keys or not all(k.args.get("desc") for k in keys) or not reads_g(keys[-1].this):
        return None
    first = _unparen(keys[0].this)
    if len(keys) > 2 or (len(keys) == 2 and not (isinstance(first, exp.Is) and isinstance(first.expression, exp.Null) and reads_g(first.this))):
        return None
    # The marked rows: SELECT y IS NOT NULL AS g FROM rows, under renaming layers.
    marking = _resolve(key, counted, schema)
    if marking is None or marking.expr is None or marking.nullable or not marking.hops:
        return None
    mark, marked = _unparen(marking.expr), marking.select
    if not (isinstance(mark, exp.Not) and isinstance(_unparen(mark.this), exp.Is) and isinstance(_unparen(mark.this).expression, exp.Null)):
        return None
    y = _unparen(_unparen(mark.this).this)
    if not isinstance(y, exp.Column) or not all(_keeps_rows(o) for o, _ in marking.hops[1:]) or not _keeps_rows(marked):
        return None
    marked_hops = hops[: index + 2] + marking.hops
    # Which column is read: the count, or the mark.
    if lineage.select is counted and isinstance(lineage.expr, exp.Count):
        role = "pn"
    elif lineage.select is marked and lineage.expr is marking.expr and len(hops) == len(marked_hops) and all(
        a[1] is b[1] for a, b in zip(hops, marked_hops)
    ):
        role = "pg"
    else:
        return None
    found = groups.get(id(top))
    if found is None:
        found = groups[id(top)] = _Group("probe", marked, marked_hops)
        found.key = None
        filtered = _probe_filter(marked, y, schema)
        rows = _rows_select(marked, y, marked_hops, schema, drop_group=False) if filtered is not None else None
        if rows is not None:
            conjunct, constant = filtered
            matches = [
                n for w in rows.find_all(exp.Where) for n in (w.this.flatten() if isinstance(w.this, exp.And) else [w.this])
                if n.sql() == conjunct.sql()
            ]
            if len(matches) == 1:
                _drop_conjunct(matches[0])
                found.rows = rows
                found.key = _canonical(rows)
                found.x = constant.copy()
                found.y = y
    if found.key is None:
        return None
    return found, role


def _probe_filter(select: exp.Select, y: exp.Column, schema) -> tuple[exp.Expression, exp.Expression] | None:
    """The ``c = y OR y IS NULL`` filter (``c`` a non-NULL literal) on the rows that ``y`` reads, and ``c``."""

    column = y
    while True:
        if not _keeps_rows(select):
            return None
        where = select.args.get("where")
        for part in (where.this.flatten() if isinstance(where.this, exp.And) else [where.this]) if where is not None else []:
            node = _unparen(part)
            if not isinstance(node, exp.Or):
                continue
            left, right = _unparen(node.this), _unparen(node.expression)
            if isinstance(left, exp.Is):
                left, right = right, left
            if not (isinstance(left, exp.EQ) and isinstance(right, exp.Is) and isinstance(right.expression, exp.Null)):
                continue
            if _unparen(right.this).sql() != column.sql():
                continue
            sides = [_unparen(left.this), _unparen(left.expression)]
            constants = [s for s in sides if isinstance(s, exp.Literal)]
            if len(constants) == 1 and any(s.sql() == column.sql() for s in sides):
                return part, constants[0]
        sources = _source_map(select)
        if sources is None:
            return None
        alias = column.table.lower() if column.table else _local_source(column, select, schema)
        source = sources.get(alias, (None,))[0] if alias else None
        body = _query_select(source) if isinstance(source, exp.Subquery) else None
        item = _outputs(body, column.name.lower()) if body is not None else None
        if item is None or not isinstance(item.unalias(), exp.Column):
            return None
        select, column = body, item.unalias()


def _drop_conjunct(node: exp.Expression) -> None:
    where = node
    while not isinstance(where, exp.Where):
        where = where.parent
    rest = [p for p in (where.this.flatten() if isinstance(where.this, exp.And) else [where.this]) if p is not node]
    if rest:
        where.set("this", exp.and_(*rest, copy=False))
    else:
        where.pop()


_BOOLEAN_SHAPES = (exp.And, exp.Or, exp.Not, exp.Is, exp.Boolean, exp.In, exp.Exists, exp.Between, exp.NullSafeEQ, exp.NullSafeNEQ) + tuple(_OPS)


def _boolean_shaped(node: exp.Expression) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, _BOOLEAN_SHAPES):
        return True
    if isinstance(node, exp.Cast):
        return node.to.this == exp.DataType.Type.BOOLEAN
    if isinstance(node, exp.Case):
        results = [i.args.get("true") for i in node.args.get("ifs") or []] + [node.args.get("default")]
        return node.this is None and all(r is None or isinstance(r, exp.Null) or _boolean_shaped(r) for r in results) and any(
            r is not None and not isinstance(r, exp.Null) for r in results
        )
    return False


class _Unsupported(Exception):
    pass


class _Encoder:
    """Three-valued z3 encoding of a condition over Calcite's aggregate and indicator columns."""

    def __init__(self, refs: dict[int, tuple], never_null: set[str]):
        self.refs = refs
        self.never_null = never_null
        self.atoms: dict[str, tuple] = {}
        self.facts: list = []
        self.state: dict[str, object] = {}

    # aggregate state of the subquery's rows
    def agg(self, role: str):
        if role not in self.state:
            if role in ("c", "ck", "d"):
                self.state[role] = (z3.BoolVal(False), z3.Int(f"q_{role}"))
            elif role == "i":
                self.state[role] = z3.Bool("q_hit")
            else:
                self.state[role] = (z3.Bool(f"q_{role}_null"), z3.Real(f"q_{role}"))
        return self.state[role]

    def present(self):
        """A probe's first group exists: some ``y`` equals the constant, or some ``y`` is NULL."""

        _, c = self.agg("c")
        _, ck = self.agg("ck")
        return z3.Or(self.agg("i"), ck < c)

    def atom(self, node: exp.Expression, kind: str):
        key = _key(node)
        if key in self.atoms:
            if self.atoms[key][0] != kind:
                raise _Unsupported("atom read as a value and as a condition")
            return self.atoms[key][1]
        if kind == "val":
            value = (z3.BoolVal(False) if key in self.never_null else z3.Bool(f"a{len(self.atoms)}_null"), z3.Real(f"a{len(self.atoms)}"))
        else:
            value = (z3.Bool(f"a{len(self.atoms)}_t"), z3.Bool(f"a{len(self.atoms)}_f"))
            self.facts.append(z3.Not(z3.And(*value)))
        self.atoms[key] = (kind, value)
        return value

    def has_ref(self, node: exp.Expression) -> bool:
        return any(id(c) in self.refs for c in node.find_all(exp.Column, exp.AggFunc))

    def pred(self, node: exp.Expression):
        if isinstance(node, exp.Paren):
            return self.pred(node.this)
        if isinstance(node, exp.And):
            a, b = self.pred(node.this), self.pred(node.expression)
            return z3.And(a[0], b[0]), z3.Or(a[1], b[1])
        if isinstance(node, exp.Or):
            a, b = self.pred(node.this), self.pred(node.expression)
            return z3.Or(a[0], b[0]), z3.And(a[1], b[1])
        if isinstance(node, exp.Not):
            a = self.pred(node.this)
            return a[1], a[0]
        if isinstance(node, exp.Boolean):
            return (z3.BoolVal(True), z3.BoolVal(False)) if node.this else (z3.BoolVal(False), z3.BoolVal(True))
        if isinstance(node, exp.Null):
            return z3.BoolVal(False), z3.BoolVal(False)
        if type(node) in _OPS:
            op = _OPS[type(node)]
            if _boolean_shaped(node.this) or _boolean_shaped(node.expression):
                if op not in ("=", "<>"):
                    raise _Unsupported("ordered comparison of conditions")
                a, b = self.pred(node.this), self.pred(node.expression)
                same = z3.Or(z3.And(a[0], b[0]), z3.And(a[1], b[1]))
                differ = z3.Or(z3.And(a[0], b[1]), z3.And(a[1], b[0]))
                return (same, differ) if op == "=" else (differ, same)
            return _compare_values(op, self.val(node.this), self.val(node.expression))
        if isinstance(node, exp.Is):
            target = node.expression
            if isinstance(target, exp.Null):
                if _boolean_shaped(node.this):
                    a = self.pred(node.this)
                    null = z3.And(z3.Not(a[0]), z3.Not(a[1]))
                else:
                    null = self.val(node.this)[0]
                return null, z3.Not(null)
            if isinstance(target, exp.Boolean):
                a = self.pred(node.this)
                hit = a[0] if target.this else a[1]
                return hit, z3.Not(hit)
            raise _Unsupported("IS")
        if isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.BOOLEAN:
            if isinstance(node.this, exp.Null):
                return z3.BoolVal(False), z3.BoolVal(False)
            if _boolean_shaped(node.this):
                return self.pred(node.this)
            raise _Unsupported("cast to BOOLEAN")
        if isinstance(node, exp.Case):
            return self._case(node, self.pred, lambda a: z3.And(z3.Not(a[0]), z3.Not(a[1])))
        if isinstance(node, exp.Column) and id(node) in self.refs:
            role = self.refs[id(node)]
            if isinstance(role, tuple):
                return self.pred(role[1])
            if role == "true":
                return z3.BoolVal(True), z3.BoolVal(False)
            if role == "i":
                return self.agg("i"), z3.BoolVal(False)
            if role == "pg":
                hit = self.agg("i")
                return hit, z3.And(self.present(), z3.Not(hit))
            raise _Unsupported("aggregate read as a condition")
        if self.has_ref(node):
            raise _Unsupported(type(node).__name__)
        return self.atom(node, "pred")

    def val(self, node: exp.Expression):
        if isinstance(node, exp.Paren):
            return self.val(node.this)
        if isinstance(node, exp.Null):
            return z3.BoolVal(True), z3.RealVal(0)
        if isinstance(node, exp.Literal) and not node.is_string:
            try:
                return z3.BoolVal(False), z3.RealVal(node.this)
            except z3.Z3Exception as error:
                raise _Unsupported("number") from error
        if isinstance(node, (exp.Column, exp.AggFunc)) and id(node) in self.refs:
            role = self.refs[id(node)]
            if isinstance(role, tuple):
                if _boolean_shaped(role[1]):
                    hit, miss = self.pred(role[1])
                    return z3.And(z3.Not(hit), z3.Not(miss)), z3.If(hit, z3.RealVal(1), z3.RealVal(0))
                return self.val(role[1])
            if role in ("c", "ck", "d"):
                null, count = self.agg(role)
                return null, z3.ToReal(count)
            if role in ("mn", "mx"):
                return self.agg(role)
            if role == "i":
                return z3.Not(self.agg("i")), z3.RealVal(1)
            if role == "pg":
                return z3.Not(self.present()), z3.RealVal(1)
            if role == "pn":
                count = z3.Int("q_pn")
                self.facts.append(count >= 1)
                return z3.Not(self.present()), z3.ToReal(count)
            return z3.BoolVal(False), z3.RealVal(1)
        if isinstance(node, exp.Case) and not _boolean_shaped(node):
            return self._case(node, self.val, lambda a: a[0])
        if self.has_ref(node) or _boolean_shaped(node):
            raise _Unsupported(type(node).__name__)
        return self.atom(node, "val")

    def _case(self, node: exp.Case, encode, is_null):
        if node.this is not None:
            raise _Unsupported("CASE operand")
        branches = [(self.pred(i.this), encode(i.args["true"])) for i in node.args.get("ifs") or []]
        default = node.args.get("default")
        result = encode(default) if default is not None else encode(exp.Null())
        for condition, value in reversed(branches):
            result = tuple(z3.If(condition[0], v, r) for v, r in zip(value, result))
        return result


def _key(node: exp.Expression) -> str:
    while isinstance(node, exp.Paren):
        node = node.this
    return node.sql(dialect="bigquery").lower()


def _compare_values(op: str, a, b):
    known = z3.And(z3.Not(a[0]), z3.Not(b[0]))
    relation = {
        "=": a[1] == b[1], "<>": a[1] != b[1], "<": a[1] < b[1], "<=": a[1] <= b[1], ">": a[1] > b[1], ">=": a[1] >= b[1],
    }[op]
    return z3.And(known, relation), z3.And(known, z3.Not(relation))


def _state_facts(encoder: _Encoder, y_never_null: bool, y_unique: bool = False) -> list:
    _, c = encoder.agg("c")
    _, ck = encoder.agg("ck")
    _, d = encoder.agg("d")
    mn, mx, hit = encoder.agg("mn"), encoder.agg("mx"), encoder.agg("i")
    facts = [
        c >= 0, ck >= 0, ck <= c,
        mn[0] == (ck == 0), mx[0] == (ck == 0),
        z3.Implies(ck >= 1, mn[1] <= mx[1]),
        (d == 0) == (ck == 0), z3.Implies(ck >= 1, z3.And(d >= 1, d <= ck)),
        z3.Implies(ck >= 1, (d == 1) == (mn[1] == mx[1])),
        z3.Implies(hit, ck >= 1),
    ]
    if y_never_null:
        facts.append(ck == c)
    if y_unique:
        facts.append(d == ck)
    return facts


def _quantified_semantics(encoder: _Encoder, x, op: str, quantifier_all: bool):
    _, c = encoder.agg("c")
    _, ck = encoder.agg("ck")
    mn, mx = encoder.agg("mn"), encoder.agg("mx")
    _, d = encoder.agg("d")
    present = z3.And(z3.Not(x[0]), ck >= 1)
    if op in ("<>", "="):
        # Some y differs from x when there are two distinct values, or the only one is not x.
        differs = z3.And(present, z3.Or(d >= 2, x[1] != mx[1]))
        if not quantifier_all:
            return differs, z3.Not(z3.And(c >= 1, z3.Or(differs, x[0], ck < c)))
        return z3.And(z3.Not(differs), z3.Or(c == 0, z3.And(z3.Not(x[0]), ck == c))), differs
    if not quantifier_all:
        bound = mn if op in (">", ">=") else mx
        witness = z3.And(present, _compare_values(op, x, bound)[0])
        not_false = z3.And(c >= 1, z3.Or(witness, x[0], ck < c))
        return witness, z3.Not(not_false)
    # x op ALL fails when some y makes the comparison FALSE.
    bound = mx if op in (">", ">=") else mn
    violated = z3.And(present, _compare_values(_FLIP[_NEGATE[op]], bound, x)[0])
    holds = z3.And(z3.Not(violated), z3.Or(c == 0, z3.And(z3.Not(x[0]), ck == c)))
    return holds, violated


def _in_semantics(encoder: _Encoder, x):
    _, c = encoder.agg("c")
    _, ck = encoder.agg("ck")
    hit = encoder.agg("i")
    false = z3.And(z3.Not(hit), z3.Or(c == 0, z3.And(z3.Not(x[0]), ck == c)))
    return hit, false


def _same(encoder: _Encoder, condition, semantics, mode: str, extra: list, witnesses: list | None = None) -> bool:
    """Whether ``condition`` and ``semantics`` agree in every state ``extra`` allows.

    ``witnesses`` keeps the models of earlier checks that came out different (the variables are named the same
    in every encoding): most candidate readings of a condition are told apart by a state an earlier check found,
    and evaluating that state is much cheaper than a solver call. A model that satisfies every assertion proves
    them satisfiable, so the answer is the one the solver would give.
    """

    assertions = [*encoder.facts, *extra]
    if mode == "pos":
        assertions.append(condition[0] != semantics[0])
    elif mode == "neg":
        assertions.append(condition[1] != semantics[1])
    else:
        assertions.append(z3.Or(condition[0] != semantics[0], condition[1] != semantics[1]))
    if witnesses and _witnessed(z3.And(*assertions), witnesses):
        return False
    solver = bounded_solver(2000)
    solver.add(*assertions)
    verdict = solver.check()
    if verdict == z3.sat and witnesses is not None:
        witnesses.insert(0, solver.model())
        del witnesses[_WITNESSES:]
    return verdict == z3.unsat


_WITNESSES = 8  # models _same keeps per folded query


def _witnessed(goal, witnesses: list) -> bool:
    for model in witnesses:
        try:
            if z3.is_true(model.eval(goal, model_completion=True)):
                return True
        except z3.Z3Exception:
            continue
    return False


@serialized
def fold_expansions(tree: exp.Expression, schema: dict | None = None, not_null: dict | None = None, keys: dict | None = None) -> exp.Expression:
    """Read Calcite's aggregate/indicator expansions as ``x op ANY/ALL (q)`` or ``x IN (q)`` (see the module docstring)."""
    if z3 is None:  # the solver decides this rewrite; without it the query stays as written
        return tree

    not_null = {k.lower(): {c.lower() for c in v} for k, v in (not_null or {}).items()}
    # Decorrelated aggregates joined on their correlation key become one-row LATERAL aggregates;
    # a rewrite that no fold uses is undone below.
    rewrites = []
    for select in list(tree.find_all(exp.Select)):
        if any(isinstance(j.this, exp.Subquery) for j in select.args.get("joins") or []):
            snapshot = select.copy()
            made = lateral_aggregates(select, not_null)
            if made:
                rewrites.append((select, snapshot, made))
    tree = _fold_all(tree, schema, not_null, keys)
    restored = False
    for select, snapshot, made in rewrites:
        if any(_inside(lateral, tree) for lateral in made):
            for key in list(select.args):
                select.set(key, None)
            for key, value in snapshot.args.items():
                select.set(key, value)
            restored = True
    if restored:
        tree = _fold_all(tree, schema, not_null, keys)
    return tree


def _fold_all(tree: exp.Expression, schema, not_null, keys) -> exp.Expression:
    if not any(
        isinstance(s.parent, (exp.Subquery, exp.Lateral))
        and (_global_aggregate(s) or _limit_one(s) or (s.args.get("group") and any(isinstance(e.unalias(), exp.Boolean) for e in s.expressions)))
        for s in tree.find_all(exp.Select)
    ):
        return tree
    folded = False
    witnesses: list = []
    for select in list(tree.find_all(exp.Select)):
        if not _inside(select, tree):
            continue
        try:
            folded = _fold_select(select, schema, not_null, keys, witnesses) or folded
        except (_Unsupported, z3.Z3Exception, RecursionError):
            continue
    if folded:
        tree = _drop_unread_joins(tree, schema, keys)
    return tree


def _prune_one_row_outputs(tree: exp.Expression) -> bool:
    """Drop column outputs of derived tables that nothing reads, so unread one-row joins show as unread."""

    pruned = False
    for select in list(tree.find_all(exp.Select)):
        sources = _source_map(select)
        if not sources or any(not isinstance(s.parent, exp.Count) for s in select.find_all(exp.Star)):
            continue
        for alias, (source, _null, _join) in sources.items():
            if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
                continue
            body = source.this
            inner = _source_map(body)
            if not inner or any(body.args.get(k) for k in ("order", "limit", "offset", "qualify")):
                continue
            one_row = {a for a, (s, _n, j) in inner.items() if j is not None and _one_row(s)}
            for item in list(body.expressions):
                value = item.unalias()
                name = item.alias_or_name.lower()
                if isinstance(value, exp.Cast) and isinstance(value.this, exp.Column):
                    value = value.this
                if not isinstance(value, exp.Column) or len(body.expressions) == 1:
                    continue
                if body.args.get("distinct") and value.table.lower() not in one_row:
                    continue  # a DISTINCT compares whole rows
                read = any(
                    (c.table.lower() == alias if c.table else True) and c.name.lower() == name
                    for c in select.find_all(exp.Column)
                    if not _inside(c, source)
                ) or any(
                    not c.table and c.name.lower() == name
                    for c in body.find_all(exp.Column)
                    if c.find_ancestor(exp.Select) is body
                )
                if not read:
                    item.pop()
                    pruned = True
    return pruned


def _at_most_one_match(join: exp.Join, select: exp.Select, schema, keys: dict) -> bool:
    """Whether each row meets at most one row of a LEFT JOINed derived table through its ON equalities.

    True when the equated outputs cover the derived select's GROUP BY, or read (through filters and
    renamings only) a declared key of one table. A LATERAL table ``SELECT ... FROM (d) AS a WHERE
    <equalities>`` joined ON TRUE is read the same way, with the WHERE equalities on ``d``.
    """

    source = join.this
    on = join.args.get("on")
    if isinstance(source, exp.Lateral):
        scope = _query_select(source.this)
        if scope is None or not _true(on):
            return False
        if _limit_one(scope):
            return True
        if not _keeps_rows(scope) or scope.args.get("joins"):
            return False
        from_ = scope.args.get("from_") or scope.args.get("from")
        inner = from_.this if from_ is not None else None
        body = _query_select(inner) if isinstance(inner, exp.Subquery) else None
        where = scope.args.get("where")
        on = where.this if where is not None else None
        if on is None:
            return False
    else:
        scope = select
        inner = source
        body = _query_select(source) if isinstance(source, exp.Subquery) else None
    if body is None:
        return False
    if _limit_one(body):
        return True
    alias = _alias_of(inner)
    equated = []
    for part in on.flatten() if isinstance(on, exp.And) else [on]:
        while isinstance(part, exp.Paren):
            part = part.this
        if not isinstance(part, exp.EQ):
            return False
        mine = [side for side in (part.this, part.expression) if isinstance(side, exp.Column) and side.table.lower() == alias]
        other = [side for side in (part.this, part.expression) if side not in mine]
        if len(mine) != 1 or any(c.table.lower() in ("", alias) for o in other for c in o.find_all(exp.Column)):
            return False
        equated.append(mine[0])
    group = body.args.get("group")
    if group is not None and not body.args.get("having") and _plain_group(group):
        outputs = {e.alias_or_name.lower(): e.unalias().sql() for e in body.expressions}
        grouped = {e.sql() for e in group.expressions if not isinstance(e, (exp.Boolean, exp.Literal))}
        if grouped <= {outputs.get(c.name.lower()) for c in equated}:
            return True
    found = [_filtered_base(column, scope, schema) for column in equated]
    if not found or any(f is None for f in found) or len({id(t) for t, _ in found}) != 1:
        return False
    return _is_key(found[0][0], {c for _, c in found}, keys)


def _filtered_base(column: exp.Column, select: exp.Select, schema) -> tuple[exp.Table, str] | None:
    """The base table and column that ``column`` reads through filters and renamings only (one row per table row)."""

    lineage = _resolve(column, select, schema)
    if lineage is None or lineage.table is None:
        return None
    for _outer, hop in lineage.hops:
        inner = _query_select(hop) if isinstance(hop, exp.Subquery) else None
        if inner is None or any(inner.args.get(k) for k in ("group", "having", "joins", "laterals", "distinct", "qualify", "windows")):
            return None
        if any(a.find_ancestor(exp.Select) is inner for e in inner.expressions for a in e.find_all(exp.AggFunc, exp.Window)):
            return None
    return lineage.table, lineage.column


def _is_key(table: exp.Table, columns: set[str], keys: dict | None) -> bool:
    if table.args.get("db") or table.args.get("catalog"):
        return False
    declared = [{c.lower() for c in k} for name, ks in (keys or {}).items() if name.lower() == table.name.lower() for k in ks]
    return any(k and k <= columns for k in declared)


def _drop_unread_joins(tree: exp.Expression, schema, keys) -> exp.Expression:
    """Joins the folding left unread: a one-row aggregate, or a LEFT JOIN meeting at most one row, changes no row."""

    changed = True
    while changed:
        changed = _prune_one_row_outputs(tree)
        for select in list(tree.find_all(exp.Select)):
            for join in list(select.args.get("joins") or []):
                source = join.this
                side = (join.args.get("side") or "").upper()
                if side not in ("", "LEFT") or (join.args.get("kind") or "").upper() in ("SEMI", "ANTI") or join.args.get("using"):
                    continue
                on = join.args.get("on")
                one_row = not (on is not None and not _true(on)) and not (side == "LEFT" and on is None) and _one_row(source)
                if not one_row and not (side == "LEFT" and on is not None and _at_most_one_match(join, select, schema, keys)):
                    continue
                alias = _alias_of(source)
                body = _query_select(source.this if isinstance(source, exp.Lateral) else source)
                names = {e.alias_or_name.lower() for e in body.expressions}
                read = any(
                    (c.table.lower() == alias if c.table else c.name.lower() in names)
                    for c in select.find_all(exp.Column)
                    if not _inside(c, source) and not _inside(c, join.args.get("on") or source)
                )
                if not read and alias:
                    join.pop()
                    changed = True
            if changed:
                break
    return tree


def _fold_select(select: exp.Select, schema, not_null, keys, witnesses: list | None = None) -> bool:
    groups: dict = {}
    refs: dict[int, tuple] = {}
    for column in _scope_columns(select):
        found = _classify(column, select, schema, not_null, groups)
        if found is not None:
            refs[id(column)] = found
    if not any(group is not None for group, _ in refs.values()):
        return False
    roots = [e for e in select.expressions] + [
        select.args[k].this for k in ("where", "having") if select.args.get(k) is not None
    ] + [j.args["on"] for j in select.args.get("joins") or [] if j.args.get("on") is not None]
    candidates = []
    for root in roots:
        for node in root.walk():
            if _boolean_shaped(node) and not isinstance(node, exp.Paren):
                inside = [c for c in node.find_all(exp.Column) if id(c) in refs and refs[id(c)][0] is not None]
                if inside:
                    candidates.append(node)
    done: list[exp.Expression] = []
    for node in candidates:  # an enclosing condition comes before the ones inside it
        if any(_inside(node, d) for d in done):
            continue
        replacement = _fold_condition(node, select, refs, schema, not_null, keys, witnesses)
        if replacement is not None:
            node.replace(replacement)
            done.append(node)
    return bool(done)


def _fold_condition(node: exp.Expression, select: exp.Select, refs: dict, schema, not_null, keys, witnesses: list | None = None) -> exp.Expression | None:
    columns = [c for c in node.find_all(exp.Column) if id(c) in refs]
    groups = {id(refs[id(c)][0]): refs[id(c)][0] for c in columns if refs[id(c)][0] is not None}
    aggregates = [g for g in groups.values() if g.kind == "agg"]
    indicators = [g for g in groups.values() if g.kind == "ind"]
    probes = [g for g in groups.values() if g.kind == "probe"]
    if len(aggregates) > 1 or len(indicators) > 1 or len(probes) > 1 or (probes and len(groups) > 1):
        return None
    if indicators and aggregates and indicators[0].key != aggregates[0].key:
        return None
    rows_group = indicators[0] if indicators else probes[0] if probes else aggregates[0]
    roles = {id(c): refs[id(c)][1] for c in columns}
    for role in list(roles.values()):
        if isinstance(role, tuple):
            body = role[1].find_ancestor(exp.Select)
            for agg in role[1].find_all(exp.AggFunc):
                if agg.find_ancestor(exp.Select) is body:
                    roles[id(agg)] = _aggregate_role(agg)[0]
    y_select = rows_group.select
    y_never_null = _never_null(rows_group.y, y_select, schema, not_null)
    base = _filtered_base(rows_group.y, y_select, schema) if isinstance(rows_group.y, exp.Column) else None
    y_unique = base is not None and _is_key(base[0], {base[1]}, keys)
    mode = _mode(node)

    def encoder_for(x: exp.Expression | None):
        never = set()
        if x is not None and _never_null(x, select, schema, not_null):
            never.add(_key(x))
        encoder = _Encoder(roles, never)
        return encoder, encoder.pred(node)

    if probes:
        x = probes[0].x
        encoder, condition = encoder_for(x)
        x_value = encoder.val(x)
        if _same(encoder, condition, _in_semantics(encoder, x_value), mode, _state_facts(encoder, y_never_null, y_unique), witnesses):
            return exp.In(this=_operand(x), query=exp.Subquery(this=rows_group.rows.copy()))
        return None
    if indicators:
        x = indicators[0].x
        encoder, condition = encoder_for(x)
        x_value = encoder.val(x)
        facts = _state_facts(encoder, y_never_null, y_unique) + [z3.Implies(encoder.agg("i"), z3.Not(x_value[0]))]
        if _same(encoder, condition, _in_semantics(encoder, x_value), mode, facts, witnesses):
            return exp.In(this=_operand(x), query=exp.Subquery(this=rows_group.rows.copy()))
        return None
    # x op ANY/ALL: x is what the subquery's MIN or MAX is compared with.
    operands = []
    for column in columns:
        if roles[id(column)] not in ("mn", "mx"):
            continue
        comparison = column.parent
        while isinstance(comparison, exp.Paren):
            comparison = comparison.parent
        if type(comparison) not in _OPS:
            continue
        other = comparison.expression if _inside(column, comparison.this) else comparison.this
        if not any(id(c) in refs for c in other.find_all(exp.Column)) and _key(other) not in {_key(o) for o in operands}:
            operands.append(other)
    for x in operands:
        # one encoding serves every reading: its variables are named by role and atom, so it is the same each time
        encoder, condition = encoder_for(x)
        x_value = encoder.val(x)
        facts = _state_facts(encoder, y_never_null, y_unique)
        for quantifier_all in (False, True):
            for op in (">", ">=", "<", "<=", "=" if quantifier_all else "<>"):
                semantics = _quantified_semantics(encoder, x_value, op, quantifier_all)
                if _same(encoder, condition, semantics, mode, facts, witnesses):
                    quantifier = exp.All if quantifier_all else exp.Any
                    return _compare(op, _operand(x), quantifier(this=exp.Subquery(this=rows_group.rows.copy())))
    return None
