"""Five shapes that hide a semi-join from the membership rules.

``decorrelation_rules`` already reads ``x IN (SELECT c FROM t WHERE p AND c IN q AND NOT c IN r)`` as
``x IN (SELECT c FROM t WHERE p) AND x IN q AND NOT x IN r``. Queries that restructure a chain of ``IN`` tests
through a CTE, a derived table or a join reach that form only after these exact steps:

**1. A wrapper inside an ``IN`` subquery is merged.** ``x IN (SELECT d.c FROM (SELECT c FROM t WHERE p) AS d
WHERE w)`` reads ``t`` directly: ``SELECT d.c FROM (SELECT c FROM t WHERE p) AS d WHERE w`` is ``SELECT c FROM t
WHERE p AND w`` row for row (the derived table selects plain columns of one table and neither select groups,
deduplicates, limits or aggregates). The wrapper's own ``WHERE`` may hold no subquery, and no name in it may be
captured by the table that moves up. A wrapper with no ``WHERE`` that only picks columns is replaced by its
derived table with those columns, whatever that joins and filters (it still may not group, deduplicate, limit
or aggregate). Only the subquery of an ``IN`` is rewritten, where the membership rules wait for it; an
``EXISTS`` reaches this step through step 2.

**2. ``EXISTS`` tied to the outer row by one equality is an ``IN``.** As a ``WHERE`` conjunct,
``EXISTS (SELECT .. FROM s WHERE s.c = o.k AND rest)`` is TRUE exactly when ``o.k IN (SELECT s.c FROM s WHERE
rest)`` is TRUE: some row of ``s`` passes ``rest`` and equals ``o.k``. Where it is not TRUE the ``IN`` may be
NULL instead of FALSE, which a ``WHERE`` conjunct treats the same, so nothing but a top-level conjunct is
rewritten (never under ``NOT``). ``rest`` may read only ``s``; the step is taken only when ``rest`` holds a
membership test, or ``s`` is a derived table whose own ``WHERE`` holds one, the cases the other rules then open
up. Any other correlated ``EXISTS`` stays: it is the form a semi-join is compared in (a join with a ``SELECT
DISTINCT`` derived table, a ``LEFT SEMI JOIN``), and rewriting one side only would lose those proofs.

**3. A filter with a closed subquery moves into a ``SELECT DISTINCT`` derived table.**
``SELECT .. FROM (SELECT DISTINCT c FROM t WHERE p) AS d WHERE d.c NOT IN (SELECT z FROM u)`` filters on a value
the ``DISTINCT`` keeps, so it drops the same rows before the deduplication as after it. ``algebraic_equivalence``
does this for conditions without subqueries; here the condition may hold subqueries that read nothing outside
themselves (so they are the same test wherever they stand).

**4. A column of a joined ``SELECT DISTINCT`` is read from the base table it equals.** In
``SELECT d.c, t.v FROM t JOIN (SELECT DISTINCT c FROM ..) AS d ON t.k = d.c`` every row that survives has
``t.k = d.c``, so ``d.c`` read anywhere else in that select is ``t.k`` (all joins of the select must be inner
joins, so the equality is a filter on every row). That covers a second derived table compared with the first:
``a.c = b.c AND a.c = t.k`` becomes ``t.k = b.c AND a.c = t.k``. With the derived table read only by the
equality, ``semijoin_rules`` turns the join into ``t.k IN (..)``. Values that compare equal are one value only
within one type (``1 = 1.0``, 2^53 and 2^53 + 1 are equal as ``FLOAT64``, ``0.0 = -0.0``, and MySQL converts a
string compared with a number), so the step is skipped unless both columns are declared with one type that is
not a floating type, or neither is declared; undeclared columns are compared under the prover's standing
same-type assumption, which the SMT step records and already relies on to prove the unnested forms of these
joins. A ``SELECT DISTINCT`` of one table without a subquery is read well by the other rules as it stands, so
there only its comparison with another derived table is re-pointed.

**5. A table joined inside an ``IN`` subquery only to be tested is a nested ``IN``.**
``x IN (SELECT a.c FROM t AS a JOIN t AS b ON a.c = b.c WHERE p(a) AND q(b))`` reads ``b`` only to ask whether a
row with ``q`` and that value exists, and the outer ``IN`` reads only which values the subquery returns, not
how often. So the subquery is ``SELECT a.c FROM t AS a WHERE p(a) AND a.c IN (SELECT b.c FROM t AS b WHERE
q(b))``: the same rows of ``a`` come back, each once instead of once per partner. ``b`` must be tied to the
other tables by exactly one column equality, every other condition on it must read ``b`` alone and hold no
subquery, and the select list must not read it. The step is taken only when that equality is on the one
column the subquery returns, so that the nested test is a test on the value of the outer ``IN`` and the
membership rules can lift it; a partner joined on another column stays a join, the form other queries with
the same join are compared in.
"""

from __future__ import annotations

from sqlglot import exp

from .ast_utils import FROM_KEY, plain_distinct, visible_ctes
from .decorrelation_rules import _and_all, _closed, _conjuncts, _sources

_SPJ = frozenset({"expressions", "from_", "from", "where"})
_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
_VOLATILE = (exp.Rand, exp.Randn, exp.Randstr, exp.Uuid, exp.Anonymous)
_FLOATING = ("FLOAT", "DOUBLE", "REAL")


def _only(select: exp.Select, allowed: frozenset) -> bool:
    return not any(value for key, value in select.args.items() if key not in allowed)


def _plain_table(source: exp.Expression) -> bool:
    if type(source) is not exp.Table or not isinstance(source.this, exp.Identifier):
        return False
    if any(value for key, value in source.args.items() if key not in _TABLE_ARGS):
        return False
    alias = source.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return False
    return bool(source.args.get("db")) or source.name.lower() not in visible_ctes(source)


def _derived(source: exp.Expression) -> exp.Select | None:
    """The select of a plainly aliased derived table."""

    if type(source) is not exp.Subquery or not source.alias or type(source.this) is not exp.Select:
        return None
    if any(value for key, value in source.args.items() if key not in ("this", "alias")) or source.args["alias"].args.get("columns"):
        return None
    return source.this


def _plain_column(node: exp.Expression) -> exp.Column | None:
    while isinstance(node, exp.Paren):
        node = node.this
    if type(node) is not exp.Column or isinstance(node.this, exp.Star) or node.args.get("db") or node.args.get("catalog"):
        return None
    return node


def _column_outputs(select: exp.Select, names: set[str]) -> dict[str, exp.Column] | None:
    """Output name -> the plain column it selects (of a source in ``names``), or ``None`` if any output is not one."""

    outputs: dict[str, exp.Column] = {}
    for item in select.expressions:
        column = _plain_column(item.unalias())
        name = item.alias_or_name.lower()
        if column is None or not name or name in outputs or column.table.lower() not in names | {""}:
            return None
        outputs[name] = column
    return outputs


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def _is_in_body(select: exp.Select) -> bool:
    parent = select.parent
    return type(parent) is exp.Subquery and not parent.alias and type(parent.parent) is exp.In and parent.parent.args.get("query") is parent


def _project(select: exp.Select, alias: str, inner: exp.Select) -> exp.Select | None:
    """``SELECT d.c, .. FROM (inner) AS d`` as ``inner`` with only those outputs: the same rows, column for column."""

    outputs: dict[str, exp.Expression] = {}
    for item in inner.expressions:
        name = item.alias_or_name.lower()
        if not name or name in outputs or any(isinstance(n, (exp.Star, exp.AggFunc, exp.Window)) for n in item.walk()):
            return None
        outputs[name] = item.unalias()
    chosen = []
    for item in select.expressions:
        column = _plain_column(item.unalias())
        if column is None or column.table.lower() not in ("", alias) or column.name.lower() not in outputs or not item.alias_or_name:
            return None
        chosen.append(exp.alias_(outputs.pop(column.name.lower()).copy(), item.alias_or_name))  # each output once
    result = inner.copy()
    result.set("expressions", chosen)
    return result


def _merge_test_wrapper(select: exp.Select) -> exp.Select | None:
    """Step 1."""

    if not _is_in_body(select) or not _only(select, _SPJ):
        return None
    sources = _sources(select)
    inner = _derived(sources[0]) if len(sources) == 1 else None
    if inner is None:
        return None
    if select.args.get("where") is None and _only(inner, _SPJ | {"joins"}):
        projected = _project(select, sources[0].alias.lower(), inner)
        if projected is not None:
            return projected
    if not _only(inner, _SPJ):
        return None
    inner_sources = _sources(inner)
    if len(inner_sources) != 1 or not _plain_table(inner_sources[0]):
        return None
    alias, moved = sources[0].alias.lower(), inner_sources[0].alias_or_name.lower()
    outputs = _column_outputs(inner, {moved})
    if outputs is None or alias == moved:
        return None
    where = select.args.get("where")
    read = list(select.expressions) + ([where.this] if where is not None else [])
    if any(isinstance(n, (exp.Select, exp.Subquery, exp.AggFunc, exp.Window, exp.Star)) for part in read for n in part.walk()):
        return None
    result = exp.Select(expressions=[item.copy() for item in select.expressions])
    result.set(FROM_KEY, (inner.args.get("from_") or inner.args.get("from")).copy())
    conditions = _conjuncts(inner.args["where"].this.copy()) if inner.args.get("where") is not None else []
    if where is not None:
        result.set("where", exp.Where(this=where.this.copy()))
    for index, item in enumerate(result.expressions):
        if isinstance(item, exp.Column):  # keep the output name once the column is replaced
            result.expressions[index].replace(exp.alias_(item.copy(), item.name))
    for column in [c for c in result.find_all(exp.Column)]:
        qualifier, name = column.table.lower(), column.name.lower()
        if qualifier == alias or (not qualifier and name in outputs):
            if name not in outputs or column.args.get("db"):
                return None
            column.replace(outputs[name].copy())
        elif not qualifier or qualifier == moved:
            return None  # a bare name of unknown owner, or a name the moved table would capture
    kept = result.args.get("where")
    conditions += _conjuncts(kept.this) if kept is not None else []
    merged = _and_all(conditions)
    result.set("where", exp.Where(this=merged) if merged is not None else None)
    return result


def _provided(source: exp.Expression, declared: dict[str, set[str]]) -> set[str] | None:
    """The column names ``source`` is known to expose."""

    if _plain_table(source):
        full = ".".join(part.name for part in source.parts).lower()
        return declared.get(full) or declared.get(source.name.lower())
    inner = _derived(source)
    if inner is None:
        return None
    names = set()
    for item in inner.expressions:
        shown = item.unalias()
        if isinstance(shown, exp.Star) or (isinstance(shown, exp.Column) and isinstance(shown.this, exp.Star)) or not item.alias_or_name:
            return None
        names.add(item.alias_or_name.lower())
    return names


def _exists_to_in(select: exp.Select, schema: dict | None) -> exp.Select | None:
    """Step 2."""

    where = select.args.get("where")
    if where is None or not schema:
        return None
    declared = {str(t).lower(): {str(c).lower() for c in columns} for t, columns in schema.items()}
    own_names = [(s.alias_or_name or "").lower() for s in _sources(select)]
    parts = _conjuncts(where.this)
    for index, part in enumerate(parts):
        if type(part) is not exp.Exists:
            continue
        body = part.this
        if type(body) is exp.Subquery and not body.alias:
            body = body.this
        if type(body) is not exp.Select or not _only(body, _SPJ) or body.args.get("where") is None:
            continue
        if any(isinstance(n, (exp.AggFunc, exp.Window)) for item in body.expressions for n in item.walk()):
            continue
        sources = _sources(body)
        if len(sources) != 1:
            continue
        source, local = sources[0], (sources[0].alias_or_name or "").lower()
        provided = _provided(source, declared)
        if not local or not provided:
            continue

        def local_column(column: exp.Column) -> bool:
            if column.table:
                return column.table.lower() == local and column.name.lower() in provided
            return column.name.lower() in provided

        link, rest = None, []
        for test in _conjuncts(body.args["where"].this):
            sides = [_plain_column(test.this), _plain_column(test.expression)] if type(test) is exp.EQ else [None, None]
            found = None
            if None not in sides:
                for mine, other in (sides, sides[::-1]):
                    qualifier = other.table.lower()
                    if local_column(mine) and qualifier and qualifier != local and own_names.count(qualifier) == 1:
                        found = (mine, other)
                        break
            if found is not None and link is None:
                link = found
            else:
                rest.append(test)
        if link is None:
            continue
        closed = True
        for test in rest:
            for node in test.walk():
                if isinstance(node, exp.Column) and _own(node, body) and not local_column(node):
                    closed = False
                if isinstance(node, (exp.AggFunc, exp.Window, *_VOLATILE)) and _own(node, body):
                    closed = False
            for nested in test.find_all(exp.Select):
                if nested.find_ancestor(exp.Select) is body and not _closed(nested, schema):
                    closed = False
        # Taken only where it opens a membership test to the other rules: one in ``rest``, or one in the WHERE of a
        # derived source. A plain correlated EXISTS (and one over a derived table that only filters) is the form
        # semi-joins are compared in, so rewriting it on one side would hide that the two sides are the same.
        inner_where = _derived(source).args.get("where") if _derived(source) is not None else None
        opens = any(isinstance(n, (exp.Exists, exp.Subquery)) for test in rest for n in test.walk()) or (
            inner_where is not None and any(isinstance(n, (exp.Exists, exp.Subquery)) for n in inner_where.walk())
        )
        if not closed or not opens:
            continue
        new_body = exp.Select(expressions=[link[0].copy()])
        new_body.set(FROM_KEY, (body.args.get("from_") or body.args.get("from")).copy())
        remaining = _and_all([test.copy() for test in rest])
        if remaining is not None:
            new_body.set("where", exp.Where(this=remaining))
        membership = exp.In(this=link[1].copy(), query=exp.Subquery(this=new_body))
        select.set("where", exp.Where(this=_and_all([p if i != index else membership for i, p in enumerate(parts)])))
        return select
    return None


def _push_closed_filter(select: exp.Select, schema: dict | None) -> exp.Select | None:
    """Step 3."""

    where = select.args.get("where")
    sources = _sources(select)
    if where is None or len(sources) != 1 or not schema:
        return None
    inner = _derived(sources[0])
    if inner is None or not plain_distinct(inner) or not _only(inner, _SPJ | {"distinct", "joins"}):
        return None
    names = {(s.alias_or_name or "").lower() for s in _sources(inner)}
    outputs = _column_outputs(inner, names)
    if outputs is None or "" in names or any(not c.table for c in outputs.values()) and len(names) != 1:
        return None
    alias = sources[0].alias.lower()
    moved, kept = [], []
    for part in _conjuncts(where.this):
        own = [c for c in part.find_all(exp.Column) if _own(c, select)]
        nested = [n for n in part.find_all(exp.Select) if n.find_ancestor(exp.Select) is select]
        movable = (
            bool(own)
            and bool(nested)
            and all(c.name.lower() in outputs and c.table.lower() in ("", alias) and not isinstance(c.this, exp.Star) for c in own)
            and all(_closed(n, schema) for n in nested)
            and not any(isinstance(n, (exp.AggFunc, exp.Window, *_VOLATILE)) and _own(n, select) for n in part.walk())
        )
        (moved if movable else kept).append(part)
    if not moved:
        return None
    pushed = []
    for part in moved:
        holder = exp.Paren(this=part.copy())
        for column in [c for c in holder.find_all(exp.Column) if c.find_ancestor(exp.Select) is None]:
            column.replace(outputs[column.name.lower()].copy())
        pushed.append(holder.this)
    existing = inner.args.get("where")
    inner.set("where", exp.Where(this=_and_all((_conjuncts(existing.this) if existing is not None else []) + pushed)))
    remaining = _and_all(kept)
    select.set("where", exp.Where(this=remaining) if remaining is not None else None)
    return select


def _declared_type(column: exp.Column, source: exp.Expression, types: dict) -> str | None:
    if _plain_table(source):
        full = ".".join(part.name for part in source.parts).lower()
        kinds = types.get(full) or types.get(source.name.lower()) or {}
        return str(kinds.get(column.name.lower(), "")).upper() or None
    inner = _derived(source)
    if inner is None:
        return None
    for item in inner.expressions:
        if item.alias_or_name.lower() == column.name.lower():
            shown = _plain_column(item.unalias())
            inner_sources = _sources(inner)
            if shown is not None and len(inner_sources) == 1:
                return _declared_type(shown, inner_sources[0], types)
    return None


def _inner_conjuncts(select: exp.Select) -> list[exp.Expression] | None:
    """The ``WHERE`` and ``ON`` conjuncts of a select whose joins are all plain inner joins, else ``None``."""

    parts = _conjuncts(select.args["where"].this) if select.args.get("where") is not None else []
    for join in select.args.get("joins") or []:
        if join.side or (join.kind or "").upper() not in ("", "INNER", "CROSS") or any(
            value for key, value in join.args.items() if key not in ("this", "on", "kind")
        ):
            return None
        parts += _conjuncts(join.args.get("on"))
    return parts


def _link(test: exp.Expression, names: set[str]) -> list[exp.Column] | None:
    """The two columns of ``a.x = b.y`` between two different sources in ``names``."""

    if type(test) is not exp.EQ:
        return None
    sides = [_plain_column(test.this), _plain_column(test.expression)]
    if None in sides or any(side.table.lower() not in names for side in sides) or sides[0].table.lower() == sides[1].table.lower():
        return None
    return sides


def _under(node: exp.Expression, root: exp.Expression) -> bool:
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _between_derived(test: exp.Expression, by_name: dict[str, exp.Expression]) -> bool:
    sides = _link(test, set(by_name))
    return sides is not None and all(_derived(by_name[side.table.lower()]) is not None for side in sides)


def _read_base_column(select: exp.Select, types: dict | None) -> exp.Select | None:
    """Step 4."""

    sources = _sources(select)
    names = [(s.alias_or_name or "").lower() for s in sources]
    parts = _inner_conjuncts(select)
    if parts is None or len(sources) < 2 or "" in names or len(set(names)) != len(names):
        return None
    by_name = dict(zip(names, sources))
    for part in parts:
        sides = _link(part, set(names))
        if sides is None:
            continue
        for mine, base in (sides, sides[::-1]):
            inner = _derived(by_name[mine.table.lower()])
            if inner is None or not plain_distinct(inner) or not _plain_table(by_name[base.table.lower()]):
                continue
            kinds = [_declared_type(c, by_name[c.table.lower()], types or {}) for c in (mine, base)]
            if kinds[0] != kinds[1] or any(f in k for k in kinds if k for f in _FLOATING):
                continue
            reads = [
                c
                for c in select.find_all(exp.Column)
                if _own(c, select)
                and c.table.lower() == mine.table.lower()
                and c.name.lower() == mine.name.lower()
                and not isinstance(c.this, exp.Star)
                and not _under(c, part)
            ]
            if not inner.args.get("joins") and inner.find(exp.Subquery, exp.Exists) is None:
                # a plain SELECT DISTINCT of one table is read well by the other rules: only its comparison with another derived table moves
                reads = [c for c in reads if any(c.parent is p for p in parts) and _between_derived(c.parent, by_name)]
            joins = select.args.get("joins") or []
            early = [j for j in joins[: max(names.index(base.table.lower()) - 1, 0)] if j.args.get("on") is not None]
            if not reads or any(_under(c, j.args["on"]) for c in reads for j in early):
                continue  # nothing to re-point, or an ON clause would read the base table before it is joined
            for column in reads:
                item = column.parent is select and column.arg_key == "expressions"
                column.replace(exp.alias_(base.copy(), column.name) if item else base.copy())
            return select
    return None


def _join_to_membership(select: exp.Select, schema: dict | None) -> exp.Select | None:
    """Step 5."""

    if not _is_in_body(select) or not schema:
        return None
    if not _only(select, _SPJ | {"joins", "distinct"}) or (select.args.get("distinct") is not None and not plain_distinct(select)):
        return None
    sources = _sources(select)
    names = [(s.alias_or_name or "").lower() for s in sources]
    parts = _inner_conjuncts(select)
    if parts is None or len(sources) < 2 or "" in names or len(set(names)) != len(names) or not all(_plain_table(s) for s in sources):
        return None
    shown = [c for item in select.expressions for c in item.find_all(exp.Column)]
    if any(isinstance(n, (exp.Select, exp.Subquery, exp.AggFunc, exp.Window, exp.Star, *_VOLATILE)) for item in select.expressions for n in item.walk()):
        return None
    if any(c.table.lower() not in names for c in shown):
        return None
    only = _plain_column(select.expressions[0].unalias()) if len(select.expressions) == 1 else None
    reads: list[set[str] | None] = []  # per conjunct: the sources it reads, None when it cannot be placed
    for part in parts:
        nested = [n for n in part.find_all(exp.Select)]
        own = [c for c in part.find_all(exp.Column) if _own(c, select)]
        if any(c.table.lower() not in names for c in own) or any(isinstance(n, (exp.AggFunc, exp.Window, *_VOLATILE)) for n in part.walk()):
            reads.append(None)
        elif nested and not all(_closed(n, schema) for n in nested if n.find_ancestor(exp.Select) is select):
            reads.append(None)
        else:
            reads.append({c.table.lower() for c in own})
    if None in reads:
        return None
    for index in range(len(sources) - 1, -1, -1):
        name = names[index]
        if any(c.table.lower() == name for c in shown):
            continue
        touching = [i for i, read in enumerate(reads) if name in read]
        links = [i for i in touching if reads[i] != {name}]
        if len(links) != 1 or _link(parts[links[0]], set(names)) is None:
            continue
        local = [i for i in touching if i not in links]
        if any(isinstance(n, (exp.Select, exp.Subquery)) for i in local for n in parts[i].walk()):
            continue
        sides = _link(parts[links[0]], set(names))
        mine, other = sides if sides[0].table.lower() == name else sides[::-1]
        if only is None or (other.table.lower(), other.name.lower()) != (only.table.lower(), only.name.lower()):
            continue  # the nested test would not be one on the value the outer IN compares
        body = exp.Select(expressions=[mine.copy()])
        body.set(FROM_KEY, exp.From(this=sources[index].copy()))
        condition = _and_all([parts[i].copy() for i in local])
        if condition is not None:
            body.set("where", exp.Where(this=condition))
        membership = exp.In(this=other.copy(), query=exp.Subquery(this=body))
        kept = [parts[i].copy() for i in range(len(parts)) if i not in touching] + [membership]
        remaining = [s.copy() for i, s in enumerate(sources) if i != index]
        result = exp.Select(expressions=[item.copy() for item in select.expressions])
        if select.args.get("distinct") is not None:
            result.set("distinct", select.args["distinct"].copy())
        result.set(FROM_KEY, exp.From(this=remaining[0]))
        if remaining[1:]:
            result.set("joins", [exp.Join(this=source) for source in remaining[1:]])
        result.set("where", exp.Where(this=_and_all(kept)))
        return result
    return None


def rewrite_semijoin_shapes(select: exp.Select, schema: dict | None, types: dict | None = None) -> exp.Expression | None:
    """``select`` after one of the five steps (in place, or its replacement), else ``None``."""

    return (
        _merge_test_wrapper(select)
        or _exists_to_in(select, schema)
        or _push_closed_filter(select, schema)
        or _read_base_column(select, types)
        or _join_to_membership(select, schema)
    )
