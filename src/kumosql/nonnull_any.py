"""Two-valued ANY on declared non-NULL columns, including unique grouped expansions."""

try:
    import z3
except ImportError:  # z3-solver is an optional extra; the rules below are reached only from a prover that has it
    z3 = None
from sqlglot import exp

from .quantified_rules import _Encoder, _Unsupported

OPS = {exp.GT: exp.Min, exp.GTE: exp.Min, exp.LT: exp.Max, exp.LTE: exp.Max}


def rewrite_nonnull_any(
    tree: exp.Expression,
    schema: dict | None,
    not_null: dict | None,
    types: dict | None = None,
    assumptions: set[str] | None = None,
) -> exp.Expression:
    if z3 is None:  # the solver decides this rewrite; without it the query stays as written
        return tree
    from .algebraic_equivalence import _qualified_outer_columns, _merge_spj_source
    from .counted_membership import _unstable, _row_phase, GROUP_EQUALITY_ASSUMPTION
    from .mysql_boolean_outputs import boolean_value, INTEGER_TYPES

    if _unstable(tree):
        return tree

    nn = {t.lower(): {c.lower() for c in cs} for t, cs in (not_null or {}).items()}
    type_map = {
        t.lower(): {c.lower(): str(kind).upper() for c, kind in cs.items()}
        for t, cs in (types or {}).items()
    }
    # Recognize the normalizer's three-valued ANY expansion once its direct
    # base-column bindings become visible. Under NOT NULL declarations its
    # second EXISTS is the first EXISTS, so the NULL arm is unreachable.
    from .algebraic_equivalence import _fold_boolean_constants, _fold_constants
    from .smt_equivalence import _canonical_aliases

    for case in list(tree.find_all(exp.Case)):
        branches = case.args.get("ifs") or []
        outer = case.find_ancestor(exp.Select)
        obase = _base(outer)
        if (
            case.this is not None
            or len(branches) != 2
            or obase is None
            or not _row_phase(outer)
            or outer.args.get("joins")
            or outer.args.get("with")
            or outer.args.get("with_")
        ):
            continue
        first, second = branches
        if (
            not isinstance(first.this, exp.Exists)
            or not isinstance(second.this, exp.Exists)
            or not isinstance(first.args.get("true"), exp.Boolean)
            or not first.args["true"].this
            or not isinstance(second.args.get("true"), exp.Null)
            or not isinstance(case.args.get("default"), exp.Boolean)
            or case.args["default"].this
        ):
            continue
        bodies = []
        for original in (first.this.this, second.this.this):
            body = original.copy()
            for _ in range(8):
                replacement = _merge_spj_source(body)
                if replacement is None:
                    break
                body = replacement
            base = _base(body)
            if (
                base is None
                or body.args.get("joins")
                or body.args.get("with")
                or body.args.get("with_")
                or body.find(exp.Subquery, exp.Exists)
                or any(
                    body.args.get(k)
                    for k in (
                        "group",
                        "having",
                        "qualify",
                        "windows",
                        "limit",
                        "offset",
                        "distinct",
                    )
                )
            ):
                break
            for guard in list(body.find_all(exp.Is)):
                column = guard.this
                if not isinstance(column, exp.Column) or not isinstance(
                    guard.expression, exp.Null
                ):
                    continue
                owner = (
                    base
                    if column.table.lower() in ("", base.alias_or_name.lower())
                    else obase
                    if column.table.lower() == obase.alias_or_name.lower()
                    else None
                )
                if _nonnull(column, owner, nn):
                    guard.replace(exp.false())
            def identity(node):
                if not isinstance(node, (exp.And, exp.Or)):
                    return node
                for constant, other in ((node.this, node.expression), (node.expression, node.this)):
                    if isinstance(constant, exp.Boolean):
                        if isinstance(node, exp.Or):
                            return constant.copy() if constant.this else other.copy()
                        return other.copy() if constant.this else constant.copy()
                return node

            for _ in range(8):
                before = body.sql()
                body = body.transform(identity)
                if before == body.sql():
                    break
            bodies.append(_fold_boolean_constants(_fold_constants(body)))
        if len(bodies) != 2:
            continue
        if _canonical_aliases(bodies[0], schema).sql(
            dialect="bigquery"
        ) == _canonical_aliases(bodies[1], schema).sql(dialect="bigquery"):
            case.replace(exp.Exists(this=bodies[0]))
    # The QED spelling introduces several column-only naming layers before
    # the quantified expression. Reuse the existing guarded projection merger
    # to expose those bindings while the ANY still has its source scope.
    if nn and tree.find(exp.Any):
        for _ in range(8):
            changed = False
            for select in list(tree.find_all(exp.Select))[::-1]:
                if select.find(exp.Any) is None or _base(select) is not None or not _row_phase(select):
                    continue
                replacement = _merge_spj_source(select.copy())
                if replacement is not None:
                    if select is tree:
                        tree = replacement
                    else:
                        select.replace(replacement)
                    changed = True
            if not changed:
                break
    # Direct lowering avoids putting a correlated SELECT inside a derived table.
    for node in list(tree.find_all(*OPS)):
        if not isinstance(node.expression, exp.Any) or not isinstance(
            node.this, exp.Column
        ):
            continue
        inner = node.expression.this
        if isinstance(inner, exp.Subquery):
            inner = inner.this
        outer = node.find_ancestor(exp.Select)
        if (
            outer is None
            or not _row_phase(outer)
            or not isinstance(inner, exp.Select)
            or len(inner.expressions) != 1
            or outer.args.get("joins")
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
        base = _base(inner)
        obase = _base(outer)
        y = inner.expressions[0].unalias()
        if not _nonnull(node.this, obase, nn) or not _nonnull(y, base, nn):
            continue
        probe = inner.copy()
        refs = _qualified_outer_columns([node.this], outer, probe, nn)
        if refs is None:
            continue
        value = probe.expressions[0].unalias().copy()
        if not value.table:
            value.set("table", exp.to_identifier(_base(probe).alias_or_name))
        comparison = type(node)(this=refs[0], expression=value)
        where = probe.args.get("where")
        probe.set(
            "where",
            exp.Where(
                this=exp.And(this=where.this, expression=comparison)
                if where
                else comparison
            ),
        )
        probe.set("expressions", [exp.Literal.number(1)])
        node.replace(exp.Exists(this=probe))
    for select in list(tree.find_all(exp.Select))[::-1]:
        joins = select.args.get("joins") or []
        if (
            len(joins) != 1
            or not _row_phase(select)
            or not _base(select)
            or any(
                select.args.get(k)
                for k in ("group", "having", "qualify", "windows", "distinct")
            )
        ):
            continue
        join = joins[0]
        source = join.this
        if (
            not isinstance(source, exp.Subquery)
            or not source.alias
            or join.args.get("using")
            or join.args.get("side") not in ("LEFT", None, "")
            or join.args.get("kind") not in (None, "", "INNER", "CROSS")
        ):
            continue
        body = source.this
        if not isinstance(body, exp.Select):
            continue
        body = _merge_spj_source(body.copy()) or body.copy()
        base = _base(body)
        group = body.args.get("group")
        alias = source.alias.lower()
        if not base or any(
            body.args.get(k)
            for k in (
                "joins",
                "having",
                "distinct",
                "qualify",
                "windows",
                "limit",
                "offset",
                "with",
                "with_",
                "order",
            )
        ):
            continue
        if group is not None and (
            not group.expressions
            or any(
                group.args.get(k)
                for k in ("rollup", "cube", "grouping_sets", "all", "totals")
            )
            or not all(isinstance(g, exp.Column) for g in group.expressions)
        ):
            continue
        outputs = {e.alias_or_name.lower(): e.unalias() for e in body.expressions}
        if len(outputs) != len(body.expressions):
            continue
        mins = {
            n: e
            for n, e in outputs.items()
            if isinstance(e, (exp.Min, exp.Max)) and _nonnull(e.this, base, nn)
        }
        counts = {
            n
            for n, e in outputs.items()
            if isinstance(e, exp.Count)
            and (isinstance(e.this, exp.Star) or _nonnull(e.this, base, nn))
        }
        sentinels = {
            n for n, e in outputs.items() if isinstance(e, exp.Boolean) and e.this
        }
        columns = {n: e for n, e in outputs.items() if isinstance(e, exp.Column)}
        if (
            len(mins) != 1
            or not counts
            or len(mins) + len(counts) + len(sentinels) + len(columns) != len(outputs)
        ):
            continue
        min_name, min_expr = next(iter(mins.items()))
        rows = body.copy()
        rows.set("group", None)
        rows.set("expressions", [exp.Literal.number(1)])
        keys = []
        predicates = []
        for part in _parts(join.args.get("on")):
            if isinstance(part, exp.EQ):
                mine = [
                    c
                    for c in (part.this, part.expression)
                    if isinstance(c, exp.Column)
                    and c.table.lower() == alias
                    and c.name.lower() in columns
                ]
                if len(mine) == 1:
                    own = columns[mine[0].name.lower()]
                    other = part.expression if mine[0] is part.this else part.this
                    if (
                        _nonnull(other, _base(select), nn)
                        or isinstance(other, exp.Column)
                        and other.table.lower() == _base(select).alias_or_name.lower()
                    ):
                        keys.append((own, other))
                        continue
            predicates.append(part)
        if group is not None and {g.sql() for g in group.expressions} != {
            c.sql() for c, _ in keys
        }:
            continue
        if group is None and keys:
            continue
        if group is not None and assumptions is None:
            continue
        padded = join.args.get("side") == "LEFT"
        if padded and predicates:
            continue
        if not padded and group is not None and len(predicates) != 1:
            continue
        targets = (
            [e.unalias() for e in select.expressions]
            if padded or group is None
            else predicates
        )
        # An aggregate source may be removed only when its sole observations are this condition.
        targets = [
            t.this
            if isinstance(t, exp.Cast)
            and (
                t.to.this == exp.DataType.Type.BOOLEAN
                or t.to.this in INTEGER_TYPES
                and boolean_value(t.this)
            )
            else t
            for t in targets
        ]
        for target in targets:
            refs = [c for c in target.find_all(exp.Column) if c.table.lower() == alias]
            if not refs:
                continue
            if any(
                c.name.lower() not in mins | {n: None for n in counts | sentinels}
                for c in refs
            ):
                continue
            comparisons = [
                c
                for c in target.find_all(*OPS)
                if isinstance(c.expression, exp.Column)
                and c.expression.table.lower() == alias
                and c.expression.name.lower() == min_name
                and isinstance(min_expr, OPS[type(c)])
                and _nonnull(c.this, _base(select), nn)
            ]
            if (
                not comparisons
                or len({(type(c), c.this.sql()) for c in comparisons}) != 1
            ):
                continue
            op = type(comparisons[0])
            x = comparisons[0].this
            # MIN/MAX order must be the order of the comparison. In particular,
            # an integer compared to a VARCHAR may coerce the VARCHAR to a
            # number, while MIN(VARCHAR) still orders lexically.
            integer_types = {
                "INT",
                "INTEGER",
                "INT64",
                "BIGINT",
                "SMALLINT",
                "TINYINT",
                "SIGNED",
            }
            if (
                type_map.get(base.name.lower(), {}).get(min_expr.this.name.lower())
                not in integer_types
                or type_map.get(_base(select).name.lower(), {}).get(x.name.lower())
                not in integer_types
            ):
                continue
            # No reference to the removed relation may survive outside the matched expression or its key ON clauses.
            if any(
                c.table.lower() == alias
                and not _inside(c, target)
                and not _inside(c, join.args.get("on"))
                and not _inside(c, source)
                for c in select.find_all(exp.Column)
            ):
                continue
            if any(
                not c.table and c.name.lower() in outputs
                for c in select.find_all(exp.Column)
                if not _inside(c, source)
            ):
                continue
            if any(
                not isinstance(s.parent, exp.Count) for s in select.find_all(exp.Star)
            ):
                continue
            roles = {
                id(c): (
                    "mn"
                    if c.name.lower() == min_name
                    else "c"
                    if c.name.lower() in counts
                    else "i"
                )
                for c in refs
            }

            class Encoder(_Encoder):
                def atom(self, node, kind):
                    key = node.sql(dialect="bigquery")
                    if key in self.atoms:
                        previous, value = self.atoms[key]
                        if previous != kind:
                            raise _Unsupported("atom type changed")
                        return value
                    if kind == "val":
                        value = (
                            z3.BoolVal(False)
                            if key.lower() in self.never_null
                            else z3.Bool(f"na{len(self.atoms)}_null"),
                            z3.Real(f"na{len(self.atoms)}_value"),
                        )
                    else:
                        value = (
                            z3.Bool(f"na{len(self.atoms)}_t"),
                            z3.Bool(f"na{len(self.atoms)}_f"),
                        )
                        self.facts.append(z3.Not(z3.And(*value)))
                    self.atoms[key] = (kind, value)
                    return value

                def agg(self, role):
                    missing = (
                        z3.Bool("any_missing")
                        if group is not None
                        else z3.BoolVal(False)
                    )
                    if role == "c":
                        return missing, z3.Int("any_count")
                    if role == "mn":
                        return z3.Or(missing, z3.Int("any_count") == 0), z3.Real(
                            "any_min"
                        )
                    if role == "i":
                        return z3.Not(missing)
                    return super().agg(role)

                def val(self, node):
                    if (
                        isinstance(node, exp.Column)
                        and id(node) in roles
                        and roles[id(node)] == "i"
                    ):
                        return z3.Bool("any_missing"), z3.RealVal(1)
                    return super().val(node)

            encoder = Encoder(roles, {x.sql(dialect="bigquery").lower()})
            try:
                actual = encoder.pred(target)
                xn, xv = encoder.val(x)
                mn, mv = encoder.agg("mn")
                relation = {
                    exp.GT: xv > mv,
                    exp.GTE: xv >= mv,
                    exp.LT: xv < mv,
                    exp.LTE: xv <= mv,
                }[op]
                expected = z3.And(z3.Not(mn), relation)
                mismatch = (
                    z3.Or(actual[0] != expected, actual[1] != z3.Not(expected))
                    if padded or group is None
                    else actual[0] != expected
                )
                solver = z3.Solver()
                solver.set(timeout=100)
                solver.add(
                    z3.Int("any_count") >= (1 if group is not None else 0),
                    *encoder.facts,
                    mismatch,
                )
                if solver.check() != z3.unsat:
                    continue
            except (_Unsupported, z3.Z3Exception):
                continue
            condition = rows.args.get("where").this if rows.args.get("where") else None
            for own, other in keys + [(min_expr.this, x)]:
                part = (
                    exp.EQ(this=own.copy(), expression=other.copy())
                    if (own, other) in keys
                    else op(this=other.copy(), expression=own.copy())
                )
                condition = (
                    part
                    if condition is None
                    else exp.And(this=condition, expression=part)
                )
            rows.set("where", exp.Where(this=condition))
            replacement = exp.Exists(this=rows)
            if padded or group is None:
                parent = target.parent
                if (
                    isinstance(parent, exp.Cast)
                    and parent.to.this == exp.DataType.Type.BOOLEAN
                ):
                    parent.replace(replacement)
                else:
                    target.replace(replacement)
            else:
                existing = select.args.get("where")
                select.set(
                    "where",
                    exp.Where(
                        this=exp.And(this=existing.this, expression=replacement)
                        if existing
                        else replacement
                    ),
                )
            select.set("joins", None)
            if group is not None:
                assumptions.add(GROUP_EQUALITY_ASSUMPTION)
            break
    return tree


def _base(select):
    if not isinstance(select, exp.Select):
        return None
    f = select.args.get("from_") or select.args.get("from")
    t = f.this if f else None
    return t if isinstance(t, exp.Table) and not t.db and not t.catalog else None


def _nonnull(column, table, nn):
    return (
        isinstance(column, exp.Column)
        and table is not None
        and column.table.lower() in ("", table.alias_or_name.lower())
        and column.name.lower() in nn.get(table.name.lower(), set())
    )


def _parts(node):
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _parts(node.this) + _parts(node.expression)
    return [node]


def _inside(node, root):
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False
