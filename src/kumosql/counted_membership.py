"""Eliminate redundant count guards after membership has become EXISTS."""

try:
    import z3
except ImportError:  # z3-solver is an optional extra; the rules below are reached only from a prover that has it
    z3 = None
from sqlglot import exp


def _row_phase(select):
    """Declared base NOT NULL facts need a row phase, not an aggregate output."""
    ancestor = select
    while ancestor is not None:
        if isinstance(ancestor, exp.Select) and (
            any(ancestor.args.get(k) for k in ("group", "having", "qualify", "windows"))
            or any(n.find_ancestor(exp.Select) is ancestor for n in ancestor.find_all(exp.AggFunc, exp.Window))
        ):
            return False
        ancestor = ancestor.parent
    return True

from .quantified_rules import _Encoder, _Unsupported

GROUP_EQUALITY_ASSUMPTION = "equalities joining grouped keys use the same equality relation as GROUP BY on those keys (including collations and coercions)"


def _unstable(tree):
    from .quantified_rules import _volatile

    return _volatile(tree) or any(
        isinstance(n, exp.Anonymous)
        or isinstance(n, exp.Func)
        and n.sql_name().upper().startswith("CURRENT_")
        for n in tree.walk()
    )


def drop_equal_count_guard(
    select: exp.Select, not_null: dict | None, assumptions: set[str] | None = None
) -> exp.Expression | None:
    """Drop a unique LEFT grouped-count source when WHERE no longer needs its count."""
    if assumptions is None or _unstable(select):
        return None
    nn = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
    where = select.args.get("where")
    if where is None or any(
        select.args.get(k) for k in ("group", "having", "qualify", "windows")
    ):
        return None
    for join in select.args.get("joins") or []:
        source = join.this
        if (
            join.args.get("side") != "LEFT"
            or join.args.get("kind")
            or join.args.get("using")
            or not isinstance(source, exp.Subquery)
            or not source.alias
        ):
            continue
        body = source.this
        if not isinstance(body, exp.Select) or any(
            body.args.get(k)
            for k in (
                "joins",
                "having",
                "qualify",
                "distinct",
                "order",
                "limit",
                "offset",
                "windows",
                "with",
                "with_",
            )
        ):
            continue
        group = body.args.get("group")
        from_ = body.args.get("from_") or body.args.get("from")
        base = from_.this if from_ else None
        if (
            group is None
            or any(
                group.args.get(k)
                for k in ("rollup", "cube", "grouping_sets", "all", "totals")
            )
            or not group.expressions
            or not isinstance(base, exp.Table)
            or base.db
            or base.catalog
        ):
            continue
        if not all(
            isinstance(e, exp.Column)
            and e.table.lower() in ("", base.alias_or_name.lower())
            for e in group.expressions
        ):
            continue
        outputs = {e.alias_or_name.lower(): e.unalias() for e in body.expressions}
        if len(outputs) != len(body.expressions):
            continue
        counts = {
            name
            for name, e in outputs.items()
            if isinstance(e, exp.Count)
            and (
                isinstance(e.this, exp.Star)
                or isinstance(e.this, exp.Column)
                and e.this.table.lower() in ("", base.alias_or_name.lower())
                and e.this.name.lower() in nn.get(base.name.lower(), set())
            )
        }
        if not counts or any(
            not isinstance(e, (exp.Column, exp.Count))
            or isinstance(e, exp.Count)
            and name not in counts
            for name, e in outputs.items()
        ):
            continue
        alias = source.alias.lower()
        on = join.args.get("on")
        parts = []

        def split(n):
            if isinstance(n, exp.And):
                split(n.this)
                split(n.expression)
            else:
                parts.append(n)

        if on is None:
            continue
        split(on)
        equated = set()
        valid = True
        for part in parts:
            if not isinstance(part, exp.EQ):
                valid = False
                break
            mine = [
                c
                for c in (part.this, part.expression)
                if isinstance(c, exp.Column) and c.table.lower() == alias
            ]
            if len(mine) != 1:
                valid = False
                break
            column = outputs.get(mine[0].name.lower())
            other = part.expression if mine[0] is part.this else part.this
            if (
                not isinstance(column, exp.Column)
                or any(
                    not c.table or c.table.lower() == alias
                    for c in other.find_all(exp.Column)
                )
                or not isinstance(other, exp.Column)
            ):
                valid = False
                break
            equated.add(column.name.lower())
        if not valid or not {e.name.lower() for e in group.expressions} <= equated:
            continue
        # Reject unqualified references and stars: these may observe this source.
        if any(
            not c.table and c.name.lower() in outputs and not _inside(c, source)
            for c in select.find_all(exp.Column)
        ) or any(
            not isinstance(s.parent, exp.Count) for s in select.find_all(exp.Star)
        ):
            continue
        uses = [
            c
            for c in select.find_all(exp.Column)
            if c.table.lower() == alias
            and c.find_ancestor(exp.Select) is select
            and c.find_ancestor(exp.Join) is not join
        ]
        if not uses or any(
            c.name.lower() not in counts or not _inside(c, where) for c in uses
        ):
            continue
        # A deeper query might read the same alias; conservative refusal avoids capture.
        if any(
            c.table.lower() == alias
            and c.find_ancestor(exp.Select) is not select
            and not _inside(c, source)
            for c in select.find_all(exp.Column)
        ):
            continue
        candidates = []
        for exists in where.find_all(exp.Exists):
            candidates.extend([exists.copy(), exp.Not(this=exists.copy())])

        class CountEncoder(_Encoder):
            def atom(self, node, kind):
                # Preserve literal/namespace case. The generic expansion
                # encoder's lowercased keys are unsuitable for arbitrary SQL.
                key = node.sql(dialect="bigquery")
                if key in self.atoms:
                    previous, value = self.atoms[key]
                    if previous != kind:
                        raise _Unsupported("atom type changed")
                    return value
                if kind == "val":
                    value = (
                        z3.Bool(f"cg{len(self.atoms)}_null"),
                        z3.Real(f"cg{len(self.atoms)}_value"),
                    )
                else:
                    value = (
                        z3.Bool(f"cg{len(self.atoms)}_t"),
                        z3.Bool(f"cg{len(self.atoms)}_f"),
                    )
                    self.facts.append(z3.Not(z3.And(*value)))
                    if isinstance(node, exp.Exists):
                        self.facts.append(z3.Or(*value))
                self.atoms[key] = (kind, value)
                return value

            def val(self, node):
                if isinstance(node, exp.Column) and id(node) in self.refs:
                    return z3.Bool("count_missing"), z3.Real("positive_count")
                return super().val(node)

        encoder = CountEncoder({id(c): "c" for c in uses}, set())
        try:
            original = encoder.pred(where.this)[0]
            for candidate in candidates:
                proposed = encoder.pred(candidate)[0]
                solver = z3.Solver()
                solver.set(timeout=100)
                solver.add(z3.Real("positive_count") >= 1, *encoder.facts)
                # EXISTS is two-valued. All other atoms remain arbitrary SQL 3VL.
                solver.add(original != proposed)
                if solver.check() == z3.unsat:
                    assumptions.add(GROUP_EQUALITY_ASSUMPTION)
                    copy = select.copy()
                    copy.set("where", exp.Where(this=candidate))
                    copy.set(
                        "joins",
                        [j.copy() for j in select.args["joins"] if j is not join]
                        or None,
                    )
                    return copy
        except (_Unsupported, z3.Z3Exception):
            continue
    return None


def _inside(node, root):
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def nonnull_not_in(tree: exp.Expression, not_null: dict | None) -> exp.Expression:
    """Non-NULL scalar NOT IN is a negated equality EXISTS, including empty input."""
    from .algebraic_equivalence import _qualified_outer_columns

    nn = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
    for node in list(tree.find_all(exp.In)):
        if not isinstance(node.parent, exp.Not) or not isinstance(
            node.this, exp.Column
        ):
            continue
        query = node.args.get("query")
        inner = query.this if isinstance(query, exp.Subquery) else None
        outer = node.find_ancestor(exp.Select)
        if (
            not isinstance(inner, exp.Select)
            or outer is None
            or not _row_phase(outer)
            or len(inner.expressions) != 1
            or node.args.get("expressions")
        ):
            continue
        if any(
            inner.args.get(k)
            for k in (
                "joins",
                "group",
                "having",
                "distinct",
                "limit",
                "offset",
                "qualify",
                "windows",
                "with",
                "with_",
            )
        ) or any(inner.find_all(exp.AggFunc, exp.Window, exp.Subquery, exp.Exists)):
            continue
        if outer.args.get("joins"):
            continue
        f = inner.args.get("from_") or inner.args.get("from")
        t = f.this if f else None
        of = outer.args.get("from_") or outer.args.get("from")
        ot = of.this if of else None
        value = inner.expressions[0].unalias()
        if (
            not isinstance(t, exp.Table)
            or t.db
            or t.catalog
            or not isinstance(ot, exp.Table)
            or ot.db
            or ot.catalog
        ):
            continue
        if (
            not isinstance(value, exp.Column)
            or value.table.lower() not in ("", t.alias_or_name.lower())
            or value.name.lower() not in nn.get(t.name.lower(), set())
        ):
            continue
        if node.this.table.lower() not in (
            "",
            ot.alias_or_name.lower(),
        ) or node.this.name.lower() not in nn.get(ot.name.lower(), set()):
            continue
        probe = inner.copy()
        refs = _qualified_outer_columns([node.this], outer, probe, nn)
        if refs is None:
            continue
        match = exp.EQ(this=probe.expressions[0].unalias().copy(), expression=refs[0])
        where = probe.args.get("where")
        probe.set(
            "where",
            exp.Where(
                this=exp.And(this=where.this, expression=match) if where else match
            ),
        )
        probe.set("expressions", [exp.Literal.number(1)])
        node.replace(exp.Exists(this=probe))
    return tree


def fold_nonnull_membership_case(
    select: exp.Select, not_null: dict | None, schema: dict | None
) -> exp.Expression | None:
    """The standard counted IN CASE becomes its EXISTS when both values are non-NULL."""
    from .smt_equivalence import _canonical_aliases

    if _unstable(select) or not _row_phase(select):
        return None

    nn = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
    from_ = select.args.get("from_") or select.args.get("from")
    base = from_.this if from_ else None
    if (
        not isinstance(base, exp.Table)
        or base.db
        or base.catalog
        or select.args.get("joins")
    ):
        return None

    def count_rows(node):
        if not isinstance(node, exp.Subquery) or not isinstance(node.this, exp.Select):
            return None
        q = node.this
        if len(q.expressions) != 1 or any(
            q.args.get(k)
            for k in (
                "joins",
                "group",
                "having",
                "distinct",
                "limit",
                "offset",
                "qualify",
                "windows",
                "with",
                "with_",
            )
        ):
            return None
        c = q.expressions[0].unalias()
        f = q.args.get("from_") or q.args.get("from")
        t = f.this if f else None
        if (
            not isinstance(c, exp.Count)
            or not isinstance(t, exp.Table)
            or t.db
            or t.catalog
        ):
            return None
        if not isinstance(c.this, exp.Star) and not (
            isinstance(c.this, exp.Column)
            and c.this.table.lower() in ("", t.alias_or_name.lower())
            and c.this.name.lower() in nn.get(t.name.lower(), set())
        ):
            return None
        rows = q.copy()
        rows.set("expressions", [exp.Literal.number(1)])
        return rows

    for item in select.expressions:
        case = item.unalias()
        branches = case.args.get("ifs") or [] if isinstance(case, exp.Case) else []
        if (
            not isinstance(case, exp.Case)
            or case.this is not None
            or len(branches) != 4
            or not isinstance(case.args.get("default"), exp.Boolean)
            or case.args["default"].this
        ):
            continue
        zero, null, hit, missing = branches
        if (
            not isinstance(zero.this, exp.EQ)
            or not isinstance(zero.this.expression, exp.Literal)
            or zero.this.expression.this != "0"
            or not isinstance(zero.args["true"], exp.Boolean)
            or zero.args["true"].this
        ):
            continue
        if (
            not isinstance(null.this, exp.Is)
            or not isinstance(null.this.expression, exp.Null)
            or not isinstance(null.this.this, exp.Column)
            or null.this.this.table.lower() not in ("", base.alias_or_name.lower())
            or null.this.this.name.lower() not in nn.get(base.name.lower(), set())
            or not isinstance(null.args["true"], exp.Null)
        ):
            continue
        if (
            not isinstance(hit.this, exp.Exists)
            or not isinstance(hit.args["true"], exp.Boolean)
            or not hit.args["true"].this
            or not isinstance(missing.this, exp.LT)
            or not isinstance(missing.args["true"], exp.Null)
        ):
            continue
        counts = [
            count_rows(q)
            for q in (zero.this.this, missing.this.this, missing.this.expression)
        ]
        if any(q is None for q in counts):
            continue
        canonical = [
            _canonical_aliases(q, schema).sql(dialect="bigquery") for q in counts
        ]
        if len(set(canonical)) != 1:
            continue
        # EXISTS must read a subset of the counted rows. Remove its correlation
        # equality only; the remaining FROM/WHERE must be exactly the count's.
        probe = hit.this.this.copy()
        if not isinstance(probe, exp.Select):
            continue
        where = probe.args.get("where")
        if where is None:
            continue
        parts = []

        def split(n):
            if isinstance(n, exp.And):
                split(n.this)
                split(n.expression)
            else:
                parts.append(n)

        split(where.this)
        matching = []
        for p in parts:
            if isinstance(p, exp.EQ) and any(
                isinstance(c, exp.Column)
                and c.table.lower() == base.alias_or_name.lower()
                for c in (p.this, p.expression)
            ):
                matching.append(p)
        if not matching:
            continue
        kept = [p for p in parts if p not in matching]
        condition = None
        for p in kept:
            condition = (
                p if condition is None else exp.And(this=condition, expression=p)
            )
        probe.set("where", exp.Where(this=condition) if condition is not None else None)
        probe.set("expressions", [exp.Literal.number(1)])
        if _canonical_aliases(probe, schema).sql(dialect="bigquery") != canonical[0]:
            continue
        copy = select.copy()
        replacement = copy.expressions[select.expressions.index(item)]
        if isinstance(replacement, exp.Alias):
            replacement.set("this", hit.this.copy())
        else:
            replacement.replace(hit.this.copy())
        return copy
    return None


def rewrite_counted_membership(
    tree: exp.Expression,
    schema: dict | None,
    not_null: dict | None,
    assumptions: set[str] | None = None,
) -> exp.Expression:
    if z3 is None:  # the solver decides this rewrite; without it the query stays as written
        return tree
    tree = nonnull_not_in(tree, not_null)
    for select in list(tree.find_all(exp.Select))[::-1]:
        replacement = drop_equal_count_guard(
            select, not_null, assumptions
        ) or fold_nonnull_membership_case(select, not_null, schema)
        if replacement is not None:
            if select is tree:
                tree = replacement
            else:
                select.replace(replacement)
    return tree
