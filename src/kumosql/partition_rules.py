"""Recombine queries that were split into partitions by a filter (TLP-style rewrites).

* ``Q WHERE p UNION ALL Q WHERE NOT p UNION ALL Q WHERE p IS NULL`` is ``Q``. More generally, UNION ALL
  branches that are the same query except for their WHERE filters, where no row can make two of the
  filters TRUE, are one branch filtered by the OR of the filters (a row passes the OR exactly when it
  passes one filter, and it then passes only that one). When the OR is TRUE for every row the filter
  is dropped. Both checks run in three-valued logic over the filters' atoms, so ``p``, ``NOT p`` and
  ``p IS NULL`` cover every row but ``p`` and ``NOT p`` do not.
* ``SELECT SUM(v) FROM (SELECT COUNT(*) AS v FROM s WHERE p UNION ALL ... WHERE NOT p UNION ALL ... WHERE p IS
  NULL)`` is ``SELECT COUNT(*) FROM s``: a global SUM of COUNTs, SUM of SUMs, MIN of MINs or MAX of MAXes over
  a UNION ALL of global aggregates is the inner aggregate over the UNION ALL of its arguments (an empty
  branch adds a 0 count or a NULL that the outer aggregate ignores). This is the reverse of the split into
  per-branch partials that ``algebraic_equivalence._split_aggregates`` normalizes to, so it only fires when
  the branches are one query split by filters that then merge into one branch.

The filter check treats every comparison or other non-boolean expression as an opaque three-valued
atom (the same atom has the same value in every branch because the branches read the same rows), so
it can only miss partitions, never invent one. Branches with aggregates, windows, DISTINCT, LIMIT,
subqueries in the filter or nondeterministic functions are left alone.
"""

from __future__ import annotations

from itertools import combinations

try:
    import z3
except ImportError:  # z3-solver is an optional extra; the rules below are reached only from a prover that has it
    z3 = None
from sqlglot import exp

from .solver_lock import bounded_solver, serialized

_SOLVER_TIMEOUT_MS = 2000
_MAX_BRANCHES = 32
_MAX_PARTITION_SEARCH = 8
_BRANCH_ARGS = {"expressions", "from", "from_", "joins", "where"}
_VOLATILE_TYPES = {"Rand", "Randn", "Uuid", "TableSample", "AnyValue"}
_RECOMBINE = {exp.Count: exp.Sum, exp.Sum: exp.Sum, exp.Min: exp.Min, exp.Max: exp.Max}


@serialized
def recombine_partitions(tree: exp.Expression) -> exp.Expression:
    """Apply both rules everywhere in ``tree``."""
    if z3 is None:  # the solver decides this rewrite; without it the query stays as written
        return tree

    tree = tree.transform(_merge_reaggregation)
    return tree.transform(_merge_partitioned_union)


# -- shapes and branches ----------------------------------------------------------------------------


def _shape(node: exp.Expression) -> str:
    """A structural key for ``node`` that ignores parentheses (the tree already carries precedence)."""

    copy = node.copy()
    for paren in list(copy.find_all(exp.Paren)):
        if paren is copy:
            copy = paren.this
        else:
            paren.replace(paren.this)
    for child in copy.walk():
        child.comments = None
    return repr(copy)


def _volatile(node: exp.Expression) -> bool:
    for child in node.walk():
        if type(child).__name__ in _VOLATILE_TYPES:
            return True
        if isinstance(child, exp.Anonymous) and (child.name or "").upper() in ("RAND", "GENERATE_UUID", "ANY_VALUE"):
            return True
    return False


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Subquery) and not node.alias and not any(
        node.args.get(k) for k in ("order", "limit", "offset", "pivots", "sample")
    ):
        node = node.this
    return node


def _all_union(node: exp.Expression) -> bool:
    return (
        type(node) is exp.Union
        and not node.args.get("distinct")
        and not any(node.args.get(k) for k in ("order", "limit", "offset", "with", "with_", "by_name", "side", "kind", "on"))
    )


def _leaves(node: exp.Expression, out: list[exp.Expression]) -> None:
    node = _unwrap(node)
    if _all_union(node):
        _leaves(node.this, out)
        _leaves(node.expression, out)
    else:
        out.append(node)


def _union_all(leaves: list[exp.Expression]) -> exp.Expression:
    def operand(leaf: exp.Expression) -> exp.Expression:
        return leaf if isinstance(leaf, exp.Select) else exp.Subquery(this=leaf)

    result = operand(leaves[0])
    for leaf in leaves[1:]:
        result = exp.Union(this=result, expression=operand(leaf), distinct=False)
    return result


def _filterable(select: exp.Expression) -> bool:
    """A plain select-project-join branch: its WHERE can move between branches."""

    if not isinstance(select, exp.Select) or any(v for k, v in select.args.items() if k not in _BRANCH_ARGS):
        return False
    if any(isinstance(n, (exp.AggFunc, exp.Window)) for item in select.expressions for n in item.walk()):
        return False
    if _volatile(select):
        return False
    where = select.args.get("where")
    return where is None or not any(
        isinstance(n, (exp.Subquery, exp.Exists, exp.AggFunc, exp.Window, exp.Anonymous)) for n in where.walk()
    )


# -- three-valued filter logic ----------------------------------------------------------------------


def _constant(node: exp.Expression | None) -> bool:
    while isinstance(node, (exp.Paren, exp.Neg)):
        node = node.this
    return isinstance(node, exp.Literal)


def _strict(node: exp.Expression) -> bool:
    """Columns and non-NULL literals under + - * only: NULL exactly when one of the columns is NULL."""

    while isinstance(node, (exp.Paren, exp.Neg)):
        node = node.this
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        return _strict(node.left) and _strict(node.right)
    return isinstance(node, (exp.Column, exp.Literal)) and not isinstance(node.this, exp.Star)


class _Logic:
    """Encodes filters as (is TRUE, is FALSE) pairs of z3 formulas; neither means NULL."""

    def __init__(self) -> None:
        self.atoms: dict[str, tuple] = {}
        self.facts: list = []

    def _atom(self, node: exp.Expression, two_valued: bool = False) -> tuple:
        key = _shape(node)
        if key not in self.atoms:
            index = len(self.atoms)
            true, false = z3.Bool(f"t{index}"), z3.Bool(f"f{index}")
            self.facts.append(z3.Not(z3.And(true, false)))
            if two_valued:
                self.facts.append(z3.Or(true, false))
            self.atoms[key] = (true, false)
            self._nullness(node, true, false)
        return self.atoms[key]

    def _nullness(self, node: exp.Expression, true, false) -> None:
        """A comparison of strict operands is NULL exactly when one of their columns is NULL."""

        if isinstance(node, (exp.EQ, exp.LT, exp.LTE)):
            operands = [node.left, node.right]
        elif isinstance(node, exp.In) and not node.args.get("query") and not node.args.get("unnest") and node.expressions:
            if not all(_constant(item) for item in node.expressions):
                return
            operands = [node.this]
        elif isinstance(node, exp.Between) and _constant(node.args.get("low")) and _constant(node.args.get("high")):
            operands = [node.this]
        else:
            return
        if not all(_strict(operand) for operand in operands):
            return
        columns = [c for operand in operands for c in operand.find_all(exp.Column)]
        nulls = []
        for column in columns:
            column_true, column_false = self._atom(column)
            nulls.append(z3.And(z3.Not(column_true), z3.Not(column_false)))
        self.facts.append(z3.Or(true, false) == z3.Not(z3.Or(*nulls)) if nulls else z3.Or(true, false))

    def encode(self, node: exp.Expression | None) -> tuple:
        if node is None:
            return z3.BoolVal(True), z3.BoolVal(False)
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.Boolean):
            return z3.BoolVal(bool(node.this)), z3.BoolVal(not node.this)
        if isinstance(node, exp.Null):
            return z3.BoolVal(False), z3.BoolVal(False)
        if isinstance(node, exp.Not):
            true, false = self.encode(node.this)
            return false, true
        if isinstance(node, exp.And):
            (t1, f1), (t2, f2) = self.encode(node.left), self.encode(node.right)
            return z3.And(t1, t2), z3.Or(f1, f2)
        if isinstance(node, exp.Or):
            (t1, f1), (t2, f2) = self.encode(node.left), self.encode(node.right)
            return z3.Or(t1, t2), z3.And(f1, f2)
        if isinstance(node, exp.Is):
            target, test = node.this, node.expression
            while isinstance(target, exp.Paren):
                target = target.this
            if isinstance(test, exp.Null):
                if isinstance(target, (exp.Predicate, exp.Connector, exp.Not, exp.Boolean, exp.Column)):
                    true, false = self.encode(target)
                    return z3.And(z3.Not(true), z3.Not(false)), z3.Or(true, false)
                return self._atom(node, two_valued=True)
            if isinstance(test, exp.Boolean):
                true, false = self.encode(target)
                hit = true if test.this else false
                return hit, z3.Not(hit)
            return self._atom(node)
        if isinstance(node, exp.NEQ):
            true, false = self.encode(exp.EQ(this=node.left.copy(), expression=node.right.copy()))
            return false, true
        if isinstance(node, exp.GT):
            return self.encode(exp.LT(this=node.right.copy(), expression=node.left.copy()))
        if isinstance(node, exp.GTE):
            return self.encode(exp.LTE(this=node.right.copy(), expression=node.left.copy()))
        if isinstance(node, exp.EQ):
            left, right = sorted((node.left, node.right), key=_shape)
            return self._atom(exp.EQ(this=left.copy(), expression=right.copy()))
        return self._atom(node)

    def unsat(self, formula) -> bool:
        solver = bounded_solver(_SOLVER_TIMEOUT_MS)
        solver.add(*self.facts)
        solver.add(formula)
        return solver.check() == z3.unsat


def _merge_group(selects: list[exp.Select]) -> tuple[list[list[int]], _Logic, list[tuple]]:
    """Clusters of two or more branch indexes whose filters are pairwise disjoint, in branch order.

    Complete partitions (filters whose OR is always TRUE) are taken first, largest first, so a
    duplicated or substituted partition is left beside the unfiltered query instead of being merged
    into an OR the prover cannot refute; the remaining disjoint branches are then merged greedily.
    """

    logic = _Logic()
    encoded = [logic.encode(s.args["where"].this if s.args.get("where") else None) for s in selects]
    count = len(encoded)
    disjoint = {
        (i, j): logic.unsat(z3.And(encoded[i][0], encoded[j][0])) for i in range(count) for j in range(i + 1, count)
    }
    free = list(range(count))
    clusters: list[list[int]] = []
    if count <= _MAX_PARTITION_SEARCH:
        found = True
        while found and len(free) > 1:
            found = False
            for size in range(len(free), 1, -1):
                for subset in combinations(free, size):
                    if all(disjoint[pair] for pair in combinations(subset, 2)) and logic.unsat(
                        z3.Not(z3.Or(*[encoded[i][0] for i in subset]))
                    ):
                        clusters.append(list(subset))
                        free = [i for i in free if i not in subset]
                        found = True
                        break
                if found:
                    break
    greedy: list[list[int]] = []
    for index in free:
        for cluster in greedy:
            if all(disjoint[min(index, other), max(index, other)] for other in cluster):
                cluster.append(index)
                break
        else:
            greedy.append([index])
    clusters += [c for c in greedy if len(c) > 1]
    return sorted(clusters), logic, encoded


def _merge_partitioned_union(node: exp.Expression) -> exp.Expression:
    if not _all_union(node) or (_all_union(node.parent) if node.parent is not None else False):
        return node
    leaves: list[exp.Expression] = []
    _leaves(node, leaves)
    if len(leaves) > _MAX_BRANCHES:
        return node
    groups: dict[str, list[int]] = {}
    for index, leaf in enumerate(leaves):
        if _filterable(leaf):
            rest = leaf.copy()
            rest.set("where", None)
            groups.setdefault(_shape(rest), []).append(index)
    replaced: dict[int, exp.Expression | None] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        clusters, logic, encoded = _merge_group([leaves[i] for i in members])
        for cluster in clusters:
            merged = leaves[members[cluster[0]]].copy()
            if logic.unsat(z3.Not(z3.Or(*[encoded[i][0] for i in cluster]))):
                merged.set("where", None)
            else:
                condition = exp.or_(*[exp.Paren(this=leaves[members[i]].args["where"].this.copy()) for i in cluster])
                merged.set("where", exp.Where(this=condition))
            replaced[members[cluster[0]]] = merged
            for i in cluster[1:]:
                replaced[members[i]] = None
    if not replaced:
        return node
    kept = [replaced[i] if i in replaced else leaf.copy() for i, leaf in enumerate(leaves)]
    return _union_all([leaf for leaf in kept if leaf is not None])


# -- re-aggregation over UNION ALL ------------------------------------------------------------------


def _global_aggregate_branch(select: exp.Expression) -> list[exp.AggFunc] | None:
    """The aggregate of each output of a global aggregate whose outputs are plain COUNT/SUM/MIN/MAX calls."""

    if not isinstance(select, exp.Select) or any(v for k, v in select.args.items() if k not in _BRANCH_ARGS):
        return None
    if _volatile(select):
        return None
    calls = []
    for item in select.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        if type(value) not in _RECOMBINE or value.args.get("expressions") or isinstance(value.this, exp.Distinct):
            return None
        if any(isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery)) for n in value.this.walk()):
            return None
        if isinstance(value.this, exp.Star) and not isinstance(value, exp.Count):
            return None
        calls.append(value)
    return calls


def _merge_reaggregation(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.Select) or any(v for k, v in node.args.items() if k not in ("expressions", "from", "from_")):
        return node
    source = node.args.get("from_") or node.args.get("from")
    derived = source.this if source is not None else None
    if not isinstance(derived, exp.Subquery) or any(derived.args.get(k) for k in ("order", "limit", "offset", "pivots", "sample")):
        return node
    union = _unwrap(derived.this)
    if not _all_union(union):
        return node
    leaves: list[exp.Expression] = []
    _leaves(union, leaves)
    if len(leaves) > _MAX_BRANCHES:
        return node
    calls = [_global_aggregate_branch(leaf) for leaf in leaves]
    if any(c is None for c in calls) or len({len(c) for c in calls}) != 1:
        return node
    names = [item.alias_or_name.lower() for item in leaves[0].expressions]
    if len(set(names)) != len(names):
        return node
    alias = (derived.alias or "").lower()
    outputs = []
    for item in node.expressions:
        value = item.this if isinstance(item, exp.Alias) else item
        column = value.this if type(value) in (exp.Sum, exp.Min, exp.Max) and not value.args.get("expressions") else None
        if not isinstance(column, exp.Column) or column.table.lower() not in ("", alias) or column.name.lower() not in names:
            return node
        position = names.index(column.name.lower())
        inner = {type(branch[position]) for branch in calls}
        if len(inner) != 1 or _RECOMBINE[inner.pop()] is not type(value):
            return node
        outputs.append((item, position))
    used = sorted({position for _, position in outputs})
    branches = []
    for leaf, branch_calls in zip(leaves, calls):
        branch = leaf.copy()
        branch.set(
            "expressions",
            [
                exp.alias_(exp.Literal.number(1) if isinstance(branch_calls[p].this, exp.Star) else branch_calls[p].this.copy(), f"_p{p}")
                for p in used
            ],
        )
        branches.append(branch)
    # Only when the arguments are one query split by a filter: otherwise the aggregate over a UNION ALL is
    # left split into per-branch partials, the form the other aggregate rules normalize to.
    merged = _merge_partitioned_union(_union_all(branches))
    if not isinstance(merged, exp.Select):
        return node
    name = derived.alias or "_parts"
    projections = []
    for item, position in outputs:
        call = type(calls[0][position])(this=exp.column(f"_p{position}", table=name))
        projections.append(exp.alias_(call, item.alias) if isinstance(item, exp.Alias) else call)
    return exp.select(*projections).from_(exp.Subquery(this=merged, alias=exp.TableAlias(this=exp.to_identifier(name))))
