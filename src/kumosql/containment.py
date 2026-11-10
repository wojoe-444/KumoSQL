"""Decide whether every result of one query is also a result of another (query containment).

``check_containment(q1, q2, semantics=...)`` answers "is ``q1`` contained in ``q2``" on every
database satisfying the declared schema:

* ``semantics="set"``: every row ``q1`` returns, ``q2`` also returns (row multiplicities are ignored).
* ``semantics="bag"``: every row ``q1`` returns, ``q2`` returns at least as many times.

The two are different questions: ``SELECT x FROM t`` is set-contained in ``SELECT DISTINCT x FROM t``
and not bag-contained in it; the other direction holds in both. Answers are

* ``contained``: proven (the method is named; the algebraic prover did the proving);
* ``not_contained``: a concrete small database on which ``q1`` returns a row that ``q2`` does not
  return as often is attached (found by random search over databases that respect the schema, then
  re-run, so it is a checked fact, not a claim);
* ``unknown``: neither; the reason says why. ``unsupported`` marks shapes the decision procedures do
  not read, ``timeout`` an exhausted prover budget.

Proof methods, each reducing containment to an equivalence the prover can discharge:

* equal: ``q1`` and ``q2`` are equivalent as bags, which implies both containments;
* pre-filter: the filters of ``q1`` that ``q2`` lacks are added to ``q2`` (only on columns that
  survive any grouping) and the result is proven equivalent to ``q1``; a filtered copy of ``q2`` is
  contained in ``q2`` in every semantics, so equality with it proves ``q1`` is contained. Not when
  ``q2`` is a global aggregate or has ``ROLLUP``/``CUBE``/``GROUPING SETS``/``GROUP BY ()`` (its
  grand-total row exists, with other values, even when the filter drops every row) or ``DISTINCT ON``
  (the filter changes which row of a key is kept);
* post-filter: ``q1`` is rewritten as a filter over the rows of ``q2`` (the model-reuse rewriter
  restricted to an identity projection);
* distinct collapse: ``DISTINCT`` in ``q1`` only removes duplicates, so for bags it is dropped when
  ``q2`` does not have it;
* union (set semantics): ``q1 UNION q2`` is proven equivalent to ``q2 UNION q2``;
* set operations (any semantics, each step valid for bags): ``A EXCEPT B`` is within ``A``,
  ``A INTERSECT B`` within ``A`` and within ``B``, ``A UNION B`` within ``A UNION ALL B``, and a
  ``UNION ALL`` is within another when each branch is contained in its own, different branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import sqlglot
from sqlglot import exp

from . import model_reuse as mr
from kumo_sql_prover.ast_utils import distinct_on, extended_grouping
from .random_check import CheckError, Schema, Witness, find_difference
from kumo_sql_prover.smt_equivalence import SmtStatus, TableConstraints


@dataclass(frozen=True)
class Containment:
    status: str  # contained | not_contained | unknown | unsupported | timeout | error
    semantics: str
    reason: str
    method: str | None = None
    witness: Witness | None = None
    assumptions: tuple[str, ...] = ()

    @property
    def contained(self) -> bool:
        return self.status == "contained"


def _prove(a: str, b: str, *, schema, constraints, types, timeout_ms, dialect, exact_arithmetic=False):
    from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic

    try:
        return prove_equivalent_algebraic(
            a,
            b,
            schema=schema,
            constraints=dict(constraints) if constraints else None,
            types={t: dict(c) for t, c in types.items()} if types else None,
            timeout_ms=timeout_ms,
            dialect=dialect,
            compare_names=False,
            exact_arithmetic=exact_arithmetic,
        )
    except Exception:  # noqa: BLE001 - a prover failure is never a proof
        return None


def _prefilter(q1: mr._Block, q2: mr._Block, tree2: exp.Expression, schema: Mapping[str, Sequence[str]], dialect: str):
    """Yield copies of ``q2`` restricted by the conjuncts ``q1`` has and ``q2`` lacks."""

    if len(q1.tables) != len(q2.tables):
        return
    group = tree2.args.get("group") if isinstance(tree2, exp.Select) else None
    if distinct_on(tree2) or (q2.is_aggregate and (not q2.group or extended_grouping(group))):
        return  # a filter before these is not a filter of their rows (see the module docstring)
    for mapping in mr._mappings(q2.tables, q1.tables):  # q1 alias -> q2 alias
        if len(mapping) != len(q1.tables):
            continue
        q1_conj = [mr._Rewriter._rename(c.copy(), mapping) for c in mr._expand_conjuncts(q1.conjuncts)]
        have = {mr._key(c) for c in mr._expand_conjuncts(q2.conjuncts)}
        extra = [c for c in q1_conj if mr._key(c) not in have]
        if q2.is_aggregate:
            groups = {mr._key(g) for g in q2.group}
            if not all(_over_groups(c, groups) for c in extra):
                continue
        if not extra:
            continue
        restricted = tree2.copy()
        combined = exp.Paren(this=mr._combine(extra))
        where = restricted.args.get("where")
        restricted.set("where", exp.Where(this=exp.And(this=exp.Paren(this=where.this.copy()), expression=combined) if where else combined))
        yield restricted.sql(dialect="postgres")


def _over_groups(node: exp.Expression, groups: set[str]) -> bool:
    """Whether a conjunct reads only grouping expressions (so it filters whole groups)."""

    if mr._key(node) in groups:
        return True
    if isinstance(node, exp.Column):
        return False
    return all(_over_groups(child, groups) for child in node.iter_expressions())


def _strip_distinct(tree: exp.Expression) -> exp.Expression:
    tree = tree.copy()
    if isinstance(tree, exp.Select) and tree.args.get("distinct"):
        tree.set("distinct", None)
    return tree


def check_containment(
    q1_sql: str,
    q2_sql: str,
    *,
    schema: Mapping[str, Sequence[str]],
    constraints: Mapping[str, TableConstraints] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    semantics: str = "set",
    dialect: str = "postgres",
    timeout_ms: int = 5000,
    database: Schema | None = None,
    trials: int = 200,
) -> Containment:
    """Is ``q1`` contained in ``q2`` under ``semantics`` ("set" or "bag")?

    ``database`` (a ``random_check.Schema``) enables the search for a counterexample database;
    without it a failed proof is only ``unknown``.
    """

    if semantics not in ("set", "bag"):
        raise ValueError("semantics must be 'set' or 'bag'")
    base = {t.lower(): [c.lower() for c in cols] for t, cols in schema.items()}
    try:
        clash = mr._name_clash((q1_sql, q2_sql), base, dialect, "")
    except sqlglot.errors.SqlglotError:
        clash = ""  # the parse error surfaces below as it always did
    if clash:
        return Containment("unsupported", semantics, clash)
    prove = lambda a, b: _prove(a, b, schema=base, constraints=constraints, types=types, timeout_ms=timeout_ms, dialect=dialect)  # noqa: E731

    def proven(a: str, b: str, method: str) -> Containment | None:
        result = prove(a, b)
        if result is not None and result.status is SmtStatus.PROVEN_EQUIVALENT:
            return Containment("contained", semantics, "proven by reducing containment to an equivalence", method, None, tuple(result.assumptions))
        return None

    unsupported = ""

    def prove_contained(a_sql: str, b_sql: str, depth: int = 0) -> Containment | None:
        """A proof that ``a_sql`` is contained in ``b_sql``, or None (never a refutation)."""

        nonlocal unsupported
        if depth == 0:
            done = proven(a_sql, b_sql, "equal")
            if done:
                return done
        try:
            tree1, tree2 = mr._prepare(a_sql, base, dialect), mr._prepare(b_sql, base, dialect)
        except mr._Unsupported as error:
            unsupported = unsupported or str(error)
            return None
        except sqlglot.errors.SqlglotError:
            if depth == 0:
                raise
            return None
        if depth < 3 and (isinstance(tree1, exp.SetOperation) or isinstance(tree2, exp.SetOperation)):
            done = _set_operations(tree1, tree2, lambda a, b: prove_contained(a, b, depth + 1))
            if done:
                return done
        if depth > 0:
            done = proven(a_sql, b_sql, "equal")
            if done:
                return done
        try:
            nn = mr.not_null_columns(constraints)
            b1, b2 = mr._block(tree1, nn), mr._block(tree2, nn)
        except mr._Unsupported as error:
            unsupported = unsupported or str(error)
            return None
        # distinct collapse (bags): DISTINCT can only remove rows from q1
        sql1 = a_sql
        if semantics == "bag" and b1.distinct and not b2.distinct:
            sql1 = _strip_distinct(tree1).sql(dialect="postgres")
            done = proven(sql1, b_sql, "distinct-collapse+equal")
            if done:
                return done
        if semantics == "bag" and b2.distinct and not b1.distinct:
            return None
        for restricted in _prefilter(b1, b2, tree2, base, dialect):
            done = proven(sql1, restricted, "pre-filter")
            if done:
                return done
        # post-filter: q1 as a filter over q2's own rows
        width = len(b2.outputs)
        if len(b1.outputs) == width:
            reuse = _post_filter(a_sql, b_sql, base, constraints, types, dialect, timeout_ms, width)
            if reuse is not None:
                return Containment("contained", semantics, "q1 is a filter over the rows of q2", "post-filter", None, reuse.assumptions)
        return None

    try:
        done = prove_contained(q1_sql, q2_sql)
    except sqlglot.errors.SqlglotError as error:
        return Containment("unsupported", semantics, f"parse error: {error}")
    if done:
        return done
    if semantics == "set":
        done = proven(f"({q1_sql}) UNION ({q2_sql})", f"({q2_sql}) UNION ({q2_sql})", "union")
        if done:
            return done

    if database is not None:
        mode = "subset" if semantics == "set" else "subbag"
        try:
            witness = find_difference(database, q1_sql, q2_sql, mode=mode, trials=trials, dialect=dialect)
        except CheckError as error:
            return Containment("unknown", semantics, f"no proof, and no search was possible: {error}")
        if witness is not None:
            return Containment("not_contained", semantics, "a database where q1 returns a row that q2 does not", "random-search", witness)
    if unsupported:
        return Containment("unsupported", semantics, unsupported)
    return Containment("unknown", semantics, "no proof was found" + ("" if database is not None else " and no counterexample search was requested"))


def _branches(tree: exp.Expression) -> list[exp.Expression]:
    """The operands of a chain of ``UNION ALL`` (a single query is a chain of one)."""

    if isinstance(tree, exp.Union) and not tree.args.get("distinct") and not _set_op_modifiers(tree):
        return _branches(_unwrap(tree.left)) + _branches(_unwrap(tree.right))
    return [tree]


def _unwrap(tree: exp.Expression) -> exp.Expression:
    while isinstance(tree, exp.Subquery) and not tree.alias and not _set_op_modifiers(tree):
        tree = tree.this
    return tree


def _set_op_modifiers(tree: exp.Expression) -> bool:
    return any(tree.args.get(k) for k in ("order", "limit", "offset", "with"))


def _set_operations(tree1: exp.Expression, tree2: exp.Expression, contained) -> Containment | None:
    """Containment through set operations, each step valid for bags (and so for sets).

    * ``A EXCEPT [ALL] B`` is within ``A``, and ``A INTERSECT [ALL] B`` within both ``A`` and ``B``;
    * ``A UNION B`` is the distinct rows of ``A UNION ALL B``, which are within it;
    * a ``UNION ALL`` of branches is within another when each branch is within its own, different branch
      of the other (multiplicities add up branch by branch).
    """

    sql = lambda t: t.sql(dialect="postgres")  # noqa: E731
    tree1, tree2 = _unwrap(tree1), _unwrap(tree2)
    if isinstance(tree1, exp.SetOperation) and not _set_op_modifiers(tree1):
        left, right = _unwrap(tree1.left), _unwrap(tree1.right)
        if isinstance(tree1, exp.Except):
            done = contained(sql(left), sql(tree2))
            if done:
                return _via(done, "except-within-left")
            return None
        if isinstance(tree1, exp.Intersect):
            for side in (left, right):
                done = contained(sql(side), sql(tree2))
                if done:
                    return _via(done, "intersect-within-operand")
            return None
        if isinstance(tree1, exp.Union) and tree1.args.get("distinct"):
            done = contained(sql(exp.union(left.copy(), right.copy(), distinct=False)), sql(tree2))
            if done:
                return _via(done, "union-within-union-all")
            return None
    if not (isinstance(tree1, exp.Union) or isinstance(tree2, exp.Union)):
        return None
    ours, theirs = _branches(tree1), _branches(tree2)
    if len(theirs) < 2 or len(ours) > len(theirs):
        return None
    pairs: dict[tuple[int, int], Containment | None] = {}

    def holds(i: int, j: int) -> Containment | None:
        if (i, j) not in pairs:
            pairs[(i, j)] = contained(sql(ours[i]), sql(theirs[j]))
        return pairs[(i, j)]

    def assign(i: int, used: frozenset[int], found: tuple[Containment, ...]):
        if i == len(ours):
            return found
        for j in range(len(theirs)):
            if j not in used:
                done = holds(i, j)
                if done:
                    result = assign(i + 1, used | {j}, found + (done,))
                    if result is not None:
                        return result
        return None

    found = assign(0, frozenset(), ())
    if found is None:
        return None
    assumptions = tuple(dict.fromkeys(a for f in found for a in f.assumptions))
    methods = "+".join(sorted({f.method or "" for f in found}))
    return Containment("contained", found[0].semantics, "each branch is contained in its own branch of q2", f"union-all-branchwise({methods})", None, assumptions)


def _via(done: Containment, step: str) -> Containment:
    return Containment("contained", done.semantics, done.reason, f"{step}+{done.method}", None, done.assumptions)


def _post_filter(q1_sql, q2_sql, schema, constraints, types, dialect, timeout_ms, width):
    reuse = mr.rewrite_over_model(
        q1_sql,
        q2_sql,
        schema=schema,
        constraints=constraints,
        types=types,
        model_name="mv0",
        dialect=dialect,
        timeout_ms=timeout_ms,
        identity=True,
    )
    return reuse if reuse.rewritten else None
