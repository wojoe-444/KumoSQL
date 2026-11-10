"""SMT-backed equivalence proofs for a core subset of BigQuery SQL.

This is a SPES-style prover. Each query is compiled into a normal form over
symbolic table rows and the equivalence conditions are discharged with Z3:

* select-project-join blocks (inner and cross joins, ``WHERE``, derived
  tables and CTEs that can be inlined) are compared under bag semantics by
  searching for a bijection between table occurrences that preserves the
  filter and the projected row;
* ``SELECT DISTINCT`` and ``UNION DISTINCT`` are compared under set semantics
  with containment mappings in both directions, which also proves redundant
  self-join elimination;
* ``GROUP BY`` blocks are compared by proving that both sides form the same
  groups and that their aggregates agree row by row;
* ``UNION ALL`` branches are matched one to one; ``INTERSECT`` and ``EXCEPT``
  (DISTINCT) keep or drop each row by an existence test.

Outer joins are split into one case per matched/unmatched combination (at most
``MAX_OUTER_JOIN_CASES``). ``EXISTS`` and ``IN`` subqueries, correlated or not,
are existence atoms. A derived table that cannot be inlined (one with a
``LIMIT``, or the ``kqw*`` table the normalizer puts window functions in) is an
opaque relation named by its text, under ``LIMIT_SOURCE_ASSUMPTION`` or
``WINDOW_SOURCE_ASSUMPTION``; a top-level ``ORDER BY .. LIMIT`` is proved when
both sides cut alike (``TIE_ASSUMPTION`` unless the ordering covers every
column). Recursive CTEs, a top-level ``LIMIT`` without ``ORDER BY``, ``PIVOT``,
nondeterministic functions and the like yield ``not_proven``.

Values use three-valued logic with explicit NULL flags over an untyped domain
(an exact rational, a string or a boolean), so no schema types are needed.
Every proof assumes ``BASE_ASSUMPTIONS``: no NaN, runtime errors not modeled,
SUM/AVG independent of row order (dropped or narrowed by ``float_sum_order``), result column types not compared; with
``exact_arithmetic`` also that ``+``, ``-`` and ``*`` never round or overflow.
Numeric conversions that the query text makes visible are kept: a CASE, IF,
COALESCE, NULLIF or set operation with a FLOAT64 branch (a FLOAT64 literal or
cast, or arithmetic over one) reads each other non-literal branch as
``CAST(.. AS FLOAT64)``, a comparison (``=``, ``<``, ``IN``, ``BETWEEN``, a
simple ``CASE``) with a FLOAT64 operand reads each other one the same way (a
FLOAT64 literal below 2**53 converts nothing: an integer and its rounding
compare alike with it), and ``x * 1`` is ``x`` only for an integer (or exact
decimal) ``1``; declared column types (``types``) count as visible. Values
from different sources whose types are not known (two undeclared columns) are
compared and combined as they are, under ``MIXED_NUMERIC_ASSUMPTION``. A
decimal or exponent literal is its exact value, which keeps its order against
every FLOAT64 value only with at most 15 significant digits and inside the
normal FLOAT64 range; other literals (``1e-324`` is 0, ``1e400`` overflows)
are not modeled. FLOAT64 arithmetic is never computed on its literals (BigQuery's
``0.1 + 0.2`` is not ``0.3``), except under ``exact_arithmetic``.

When a proof fails the prover looks for a small concrete database on which
the two queries return different rows; if it finds one the result is
``not_equivalent`` with that database attached. A counterexample is only
reported when every function in both queries is modeled exactly, so it never
rests on an uninterpreted function.

``z3-solver`` is an optional dependency (the ``smt`` extra): without it this module still imports and every proof
is ``NOT_PROVEN`` with ``solver_lock.Z3_MISSING`` as the reason.
"""

from __future__ import annotations

from collections import Counter
import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
import argparse
import hashlib
import contextlib
import datetime
import itertools
import json
import re
import sys

import sqlglot
from sqlglot import exp
from .ast_utils import UnmodeledConstruct, canonical_negation, check_modeled, drop_case_conflicts, expand_alias_columns, extended_grouping, faithful_sql, merge_wrapper_tails, plain_distinct, star_modified
from .parse_check import refuse_misread_proofs
from . import proof_columns
from .set_operations import positional_sql_pair
from .smt_args import check_args
from .solver_lock import Z3_MISSING, bound, bounded_solver, serialized
from .string_literals import canonical_literals
from .type_names import invalid_type_name
from .sqlx_fragments import masked_template_problem
from . import numeric_column_reading, smt_errors, smt_group_sums, smt_literal_coercion, smt_numeric, smt_values, string_number_compare, string_number_literals
from .float_sum_order import Ledger

try:  # pragma: no cover - exercised by the import itself
    import z3
except ImportError:  # pragma: no cover
    z3 = None


class SmtStatus(str, Enum):
    """Outcomes of the SMT prover."""

    PROVEN_EQUIVALENT = "proven_equivalent"
    NOT_EQUIVALENT = "not_equivalent"
    NOT_PROVEN = "not_proven"
    # Equivalent on every database that meets ``SmtEquivalenceResult.conditions``; never counted as proven.
    PROVEN_CONDITIONALLY = "proven_conditionally"


@dataclass(frozen=True)
class Counterexample:
    """A database on which the two queries return different result bags."""

    tables: dict[str, list[dict[str, object]]]
    left_rows: list[tuple]
    right_rows: list[tuple]


@dataclass(frozen=True)
class SmtEquivalenceResult:
    status: SmtStatus
    reason: str
    counterexample: Counterexample | None = None
    assumptions: tuple[str, ...] = ()
    # ``kumosql.conditional_equivalence.Condition`` items of a PROVEN_CONDITIONALLY result.
    conditions: tuple = ()
    # ``kumosql.smt_errors.ErrorReport`` of a proof in the BigQuery dialect: what the rewrite does to runtime errors.
    errors: object = None

    @property
    def proven(self) -> bool:
        return self.status is SmtStatus.PROVEN_EQUIVALENT

    @property
    def conditionally_proven(self) -> bool:
        return self.status is SmtStatus.PROVEN_CONDITIONALLY


@dataclass(frozen=True)
class TableConstraints:
    """Declared integrity constraints of one table (names lower-case).

    ``not_null`` lists columns that never hold NULL; each tuple in ``keys`` is a
    set of columns that is unique across rows (a primary key, or a UNIQUE
    constraint whose columns are also NOT NULL). Rows that agree on every column
    of a key are the same row.
    """

    not_null: frozenset = frozenset()
    keys: tuple = ()
    # Each ``(columns, parent_table, parent_columns)``: every row whose columns are all non-NULL has a
    # matching row in the parent. The SMT encoding does not use it; the algebraic normalizer does
    # (``fk_rules``) and counterexamples must respect it.
    foreign_keys: tuple = ()


BASE_ASSUMPTIONS = (
    "FLOAT64 values are never NaN",
    "runtime errors (division by zero, overflow, failed casts) are not modeled",
    "SUM and AVG are treated as independent of row order",
    "result column types are not compared; confirm schemas with a BigQuery dry run",
)
EXACT_ARITHMETIC_ASSUMPTION = "+, - and * are exact (no FLOAT64 rounding or INT64 overflow)"
MIXED_NUMERIC_ASSUMPTION = (
    "values compared, or combined by CASE, IF, COALESCE or a set operation, have the same numeric type (no INT64 to FLOAT64 conversion)"
)

_NONDETERMINISTIC_NAMES = {
    "ANY_VALUE",
    "APPROX_COUNT_DISTINCT",
    "APPROX_QUANTILES",
    "APPROX_TOP_COUNT",
    "APPROX_TOP_SUM",
    "ARRAY_AGG",
    "CURRENT_DATE",
    "CURRENT_DATETIME",
    "CURRENT_TIME",
    "CURRENT_TIMESTAMP",
    "GENERATE_UUID",
    "RAND",
    "SESSION_USER",
    "STRING_AGG",
}
_NONDETERMINISTIC_TYPES = {
    "AnyValue",
    "ApproxDistinct",
    "ApproxQuantile",
    "ArrayAgg",
    "CurrentDate",
    "CurrentDatetime",
    "CurrentTime",
    "CurrentTimestamp",
    "CurrentUser",
    "GroupConcat",
    "MaxBy",
    "MinBy",
    "ArgMax",
    "ArgMin",
    "Rand",
    "TableSample",
    "Uuid",
}
_SUPPORTED_AGGREGATES = {
    exp.Count: "COUNT",
    exp.Sum: "SUM",
    exp.Min: "MIN",
    exp.Max: "MAX",
    exp.Avg: "AVG",
    exp.CountIf: "COUNTIF",
    exp.LogicalAnd: "LOGICAL_AND",
    exp.LogicalOr: "LOGICAL_OR",
    # Bitwise aggregates are left uninterpreted: their value over an empty group differs by engine
    # (NULL in BigQuery, all ones or zero in MySQL), so only equal calls are known to agree.
    # sqlglot 26.0.0 has no classes for them (BIT_AND is an unknown function there).
    **{getattr(exp, cls): name for cls, name in (("BitwiseAndAgg", "BIT_AND"), ("BitwiseOrAgg", "BIT_OR"), ("BitwiseXorAgg", "BIT_XOR")) if hasattr(exp, cls)},
}
# Aggregates whose value depends only on the set of non-NULL values, not on how often each occurs.
_DUPLICATE_INSENSITIVE = ("MIN", "MAX", "LOGICAL_AND", "LOGICAL_OR", "BIT_AND", "BIT_OR")
# Aggregates that are NULL exactly when no argument value is non-NULL (BIT_AND/BIT_OR/BIT_XOR are not:
# MySQL returns a number for an empty group).
_NULL_WHEN_EMPTY = ("SUM", "MIN", "MAX", "LOGICAL_AND", "LOGICAL_OR")
_DATEISH = re.compile(r"^\s*[+-]?\d{1,5}-\d{1,2}-\d{1,2}")
_CANONICAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANONICAL_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_MAX_MAPPINGS = 5000
_MAX_EVAL_COMBINATIONS = 50000
# Select bodies and table reads compiled for one pair: a CTE read twice per level doubles each level, so
# fourteen short lines would expand to 16,384 reads, and the solver timeout does not cover compiling.
_MAX_EXPANSION = 4000


_UNNEST_TABLE = "$unnest"


class _Env(dict):
    """Alias -> source for one query scope; ``outer`` is the enclosing scope of a subquery."""

    outer: "_Env | None" = None


class Unsupported(Exception):
    """The query uses something outside the modeled subset."""


def _lost_null_ordering(body: exp.Expression, text: str) -> str:
    """A suffix naming every NULLS FIRST/LAST of ``body`` when its BigQuery ``text`` lost one, else "".

    sqlglot drops a NULLS placement BigQuery cannot spell there, such as ``ASC NULLS LAST`` in an aggregate's
    window, so ``SUM(v) OVER (ORDER BY t NULLS FIRST)`` and ``.. NULLS LAST`` would print alike.
    """

    before = [bool(o.args.get("nulls_first")) for o in body.find_all(exp.Ordered)]
    if not before:
        return ""
    try:
        after = [bool(o.args.get("nulls_first")) for o in sqlglot.parse_one(text, read="bigquery").find_all(exp.Ordered)]
    except sqlglot.errors.ParseError:
        after = None
    return "" if before == after else " NULLS[" + "".join("F" if b else "L" for b in before) + "]"


def _names_output_alias(column: exp.Column, select: exp.Select) -> bool:
    """A bare column in ``select``'s ORDER BY, GROUP BY, HAVING or QUALIFY that may name one of its output
    aliases rather than a source column (``SELECT y AS v FROM u ORDER BY v`` sorts by ``y``)."""

    node = column
    while node.parent is not None and node.parent is not select:
        node = node.parent
    if node.parent is not select or node.arg_key not in ("order", "group", "having", "qualify"):
        return False
    name = column.name.lower()
    for item in select.expressions:
        if isinstance(item, exp.Alias) and item.alias.lower() == name:
            value = item.this
            if not (isinstance(value, exp.Column) and value.name.lower() == name and not value.table):
                return True
    return False


def _canonical_aliases(body: exp.Expression, schema: dict[str, list[str]] | None = None, reader=None) -> exp.Expression:
    """A copy of ``body`` whose tables and derived tables carry positional aliases (``kq0``, ``kq1``..),
    so two spellings of the same relation get the same identity. A column is renamed through the
    nearest enclosing SELECT that declares its qualifier, so an alias reused in a nested scope is fine.
    With a schema, a bare column of a select over one known table is qualified first; given the independent
    ``reader`` of the statement (``proof_columns``), each such qualifier must name the owner it finds."""

    body = body.copy()
    ctes = {c.alias_or_name.lower() for c in body.find_all(exp.CTE)}
    fresh: dict[int, str] = {}
    for node in body.walk():
        if not isinstance(node, (exp.Table, exp.Subquery)) or isinstance(node.parent, exp.Table):
            continue
        if not node.parent or not isinstance(node.parent, (exp.From, exp.Join)):
            continue
        old = node.alias_or_name
        if not old or old.lower() in ctes or (isinstance(node, exp.Subquery) and not node.alias):
            continue
        fresh[id(node)] = f"kq{len(fresh)}"

    def declared(select: exp.Select) -> list[exp.Expression]:
        from_ = select.args.get("from_") or select.args.get("from")
        return ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]

    def names(node: exp.Expression) -> set[str]:
        alias = node.args.get("alias")
        element = alias.args.get("columns") if isinstance(node, exp.Unnest) and alias is not None else None
        # BigQuery: ``UNNEST(..) AS e`` declares the element ``e``, which hides an outer alias ``e``
        return {(node.alias_or_name or "").lower()} | ({element[0].name.lower()} if element else set())

    renames: list[tuple[exp.Column, str]] = []
    for column in body.find_all(exp.Column):
        qualifier = column.table.lower()
        if not qualifier and schema:
            scope = column.find_ancestor(exp.Select)
            sources = declared(scope) if scope is not None else []
            if len(sources) == 1 and isinstance(sources[0], exp.Table) and not isinstance(column.this, exp.Star) and not _names_output_alias(column, scope):
                key = ".".join(p.name for p in sources[0].parts).lower()
                known = schema.get(key)
                if known is not None and column.name.lower() in [c.lower() for c in known]:
                    if reader is not None:
                        verdict = reader.judge_tagged(column, proof_columns.Claim(source=proof_columns.source_tag(sources[0])), "smt_schema_qualification")
                        if verdict.refused:
                            raise Unsupported(f"independent check of column resolution: {verdict.reason}")
                    qualifier = (sources[0].alias_or_name or "").lower()
                    column.set("table", exp.to_identifier(sources[0].alias_or_name))
        if not qualifier:
            continue
        scope = column.find_ancestor(exp.Select)
        while scope is not None:
            hit = next((n for n in declared(scope) if qualifier in names(n)), None)
            if hit is not None:
                if id(hit) in fresh:
                    renames.append((column, fresh[id(hit)]))
                break
            scope = scope.find_ancestor(exp.Select)
    for column, name in renames:
        column.set("table", exp.to_identifier(name))
    for node in body.walk():
        if id(node) in fresh:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(fresh[id(node)])))
    return body


def _is_ground(term) -> bool:
    """Whether a Z3 term mentions no uninterpreted constant (it has one fixed value)."""

    stack = [term]
    while stack:
        node = stack.pop()
        if z3.is_const(node) and node.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            return False
        stack.extend(node.children())
    return True


# Empty without z3-solver (an optional extra): importing this module must not need it.
_ARITHMETIC = () if z3 is None else (z3.Z3_OP_ADD, z3.Z3_OP_SUB, z3.Z3_OP_MUL, z3.Z3_OP_UMINUS, z3.Z3_OP_ANUM, z3.Z3_OP_AGNUM)


def _uninterpreted_leaves(term) -> list | None:
    """The column constants of ``term`` when it is a column or ``+ - *`` over columns and numbers
    (NULL exactly when one of its columns is), else ``None``."""

    leaves, stack = [], [term]
    while stack:
        node = stack.pop()
        if z3.is_const(node) and node.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            if not any(leaf.eq(node) for leaf in leaves):
                leaves.append(node)
            continue
        kind = node.decl().kind()
        if kind not in _ARITHMETIC and not (kind in (z3.Z3_OP_DT_CONSTRUCTOR, z3.Z3_OP_DT_ACCESSOR) and node.decl().name() in ("Num", "num")):
            return None
        stack.extend(node.children())
    return leaves


def _implied(conjuncts: list, fact, ids: set) -> bool:
    """Whether the conjunction ``conjuncts`` implies ``fact`` (a quick solver check)."""

    if fact.get_id() in ids:
        return True
    solver = bounded_solver(2000)
    solver.add(*conjuncts)
    solver.add(z3.Not(fact))
    return solver.check() == z3.unsat


class _Exhausted(Unsupported):
    """The compilation budget ran out; never retried as an opaque relation."""


class _SetSourceUnresolved(Exception):
    """A DISTINCT derived table was not joined on all of its columns."""


class _Unknown(Exception):
    """The solver timed out."""


# --------------------------------------------------------------------------
# Z3 value domain
# --------------------------------------------------------------------------

_VALUE_SORT = None
_STRING_RANK = None
_ORDER_ROUNDS = 2  # times a counterexample search is told the real order of the strings it chose


def _string_rank():
    """An order-embedding of strings into the reals: ``a < b`` iff ``rank(a) < rank(b)``.

    Z3's own string ordering takes seconds even for ``d < '1994-12-01'``. The
    prover only needs the ordering facts the queries use (see ``order_facts``),
    which hold in the real ordering, so a proof over the abstraction holds there too.
    """

    global _STRING_RANK
    if _STRING_RANK is None:
        _STRING_RANK = z3.Function("string_rank", z3.StringSort(), z3.RealSort())
    return _STRING_RANK


_CONVERTED = None


def _converted_functions():
    """Uninterpreted ``eq`` and ``lt`` over values, for a string compared with a number."""

    global _CONVERTED
    if _CONVERTED is None:
        value = _value_sort()
        _CONVERTED = (z3.Function("converted_eq", value, value, z3.BoolSort()), z3.Function("converted_lt", value, value, z3.BoolSort()))
    return _CONVERTED


def _value_sort():
    global _VALUE_SORT
    if _VALUE_SORT is None:
        value = z3.Datatype("SqlValue")
        value.declare("Num", ("num", z3.RealSort()))
        value.declare("Str", ("str", z3.StringSort()))
        value.declare("Bool", ("bool", z3.BoolSort()))
        _VALUE_SORT = value.create()
    return _VALUE_SORT


@dataclass(frozen=True)
class _Val:
    """A nullable SQL value: ``null`` flag and payload."""

    null: object
    val: object


@dataclass(frozen=True)
class _Pred:
    """A three-valued predicate: ``t`` when TRUE, ``f`` when FALSE."""

    t: object
    f: object


def _canon(v: _Val):
    V = _value_sort()
    return z3.If(v.null, V.Num(0), v.val)


def _null_eq(a: _Val, b: _Val):
    """Row/grouping equality: NULL matches NULL."""

    return z3.And(a.null == b.null, z3.Or(a.null, a.val == b.val))


def _rows_eq(left: list[_Val], right: list[_Val]):
    if len(left) != len(right):
        return z3.BoolVal(False)
    return z3.And(*[_null_eq(a, b) for a, b in zip(left, right)]) if left else z3.BoolVal(True)


def _lt(a, b):
    """A strict total order on values that agrees with SQL within a type."""

    V = _value_sort()

    def rank(v):
        return z3.If(V.is_Num(v), 0, z3.If(V.is_Str(v), 1, 2))

    return z3.If(
        z3.And(V.is_Num(a), V.is_Num(b)),
        V.num(a) < V.num(b),
        z3.If(
            z3.And(V.is_Str(a), V.is_Str(b)),
            _string_rank()(V.str(a)) < _string_rank()(V.str(b)),
            z3.If(
                z3.And(V.is_Bool(a), V.is_Bool(b)),
                z3.And(z3.Not(V.bool(a)), V.bool(b)),
                rank(a) < rank(b),
            ),
        ),
    )


def _rank_injective(a, b):
    """Equal ranks mean equal strings (the ranking is one-to-one), stated for the pair ``a``, ``b``."""

    V, rank = _value_sort(), _string_rank()
    sa, sb = V.str(a), V.str(b)
    return z3.Implies(rank(sa) == rank(sb), sa == sb)


def _pair_lt(a: _Val, b: _Val):
    ca, cb = _canon(a), _canon(b)
    return z3.Or(z3.And(z3.Not(a.null), b.null), z3.And(a.null == b.null, _lt(ca, cb)))


def _box(p: _Pred) -> _Val:
    V = _value_sort()
    return _Val(z3.And(z3.Not(p.t), z3.Not(p.f)), V.Bool(p.t))


def _const_pred(value: bool) -> _Pred:
    return _Pred(z3.BoolVal(value), z3.BoolVal(not value))


# --------------------------------------------------------------------------
# Normal forms
# --------------------------------------------------------------------------


class _Occ:
    """One occurrence of a relation in FROM, with lazily created columns."""

    def __init__(self, table: str, uid: str, columns: list[str] | None, opaque: bool = False):
        self.table = table
        self.uid = uid
        self.columns = columns
        self.opaque = opaque
        self.cols: dict[str, _Val] = {}

    def col(self, name: str) -> _Val:
        if name not in self.cols:
            V = _value_sort()
            self.cols[name] = _Val(
                z3.Bool(f"{self.uid}.{name}#null"), z3.Const(f"{self.uid}.{name}", V)
            )
        return self.cols[name]

    def copy(self, uid: str) -> "_Occ":
        clone = _Occ(self.table, uid, self.columns, self.opaque)
        for name in self.cols:
            clone.col(name)
        return clone


@dataclass
class _State:
    """One way a FROM clause can produce rows (outer joins split into several)."""

    occs: list
    conds: list
    env: "_Env"
    subs: list = field(default_factory=list)

    def cond(self) -> "_Pred":
        result = _const_pred(True)
        for c in self.conds:
            result = _Pred(z3.And(result.t, c.t), z3.Or(result.f, c.f))
        return result


@dataclass
class _Sub:
    """An existence test: ``atom`` is TRUE iff some row of ``occs`` satisfies ``guard``.

    ``atom`` is a free Boolean in the formulas that mention it. Two subqueries
    proved to test the same thing may share an atom; nested tests inside the
    guard are in ``nested``.
    """

    atom: object
    occs: list
    guard: object
    nested: list = field(default_factory=list)
    # (block, [column values]) for a DISTINCT derived table read as a set: the
    # columns are free until the join pins them (see ``resolve_set_sources``).
    setsrc: object = None


@dataclass
class _Spj:
    occs: list[_Occ]
    cond: _Pred
    outputs: list[_Val]
    names: list[str]
    distinct: bool = False
    facts: list = field(default_factory=list)
    subs: list = field(default_factory=list)


@dataclass
class _AggCall:
    func: str
    distinct: bool
    arg: _Val | None
    var: _Val


@dataclass
class _Agg:
    occs: list[_Occ]
    cond: _Pred
    keys: list[_Val]
    aggs: list[_AggCall]
    having: _Pred | None
    outputs: list[_Val]
    names: list[str]
    is_global: bool
    distinct: bool = False
    facts: list = field(default_factory=list)
    subs: list = field(default_factory=list)


@dataclass
class _Union:
    branches: list
    distinct: bool
    column_names: list | None = None

    @property
    def names(self) -> list[str]:
        if self.column_names is not None:
            return self.column_names
        return self.branches[0].names


@dataclass
class _Source:
    occ: _Occ | None = None
    cols: dict[str, _Val] | None = None
    order: list[str] = field(default_factory=list)
    # The unmatched side of an outer join over a table whose columns are not declared: every column is NULL.
    all_null: _Val | None = None
    # An UNNEST named by its element (``UNNEST(..) AS e``): ``e`` is a value, so ``e.f`` and ``e.*`` read its fields.
    value_table: bool = False

    def lookup(self, name: str) -> _Val | None:
        if self.all_null is not None:
            return self.all_null
        if self.cols is not None:
            return self.cols.get(name)
        if self.occ.columns is not None and name not in self.occ.columns:
            return None
        return self.occ.col(name)

    def may_have(self, name: str) -> bool | None:
        """True/False when known, None when the schema is unknown."""

        if self.all_null is not None:
            return None
        if self.cols is not None:
            return name in self.cols
        if self.occ.columns is None:
            return None
        return name in self.occ.columns

    def star(self) -> list[tuple[str, _Val]]:
        if self.all_null is not None:
            raise Unsupported("SELECT * over the unmatched side of an outer join needs a schema")
        if self.cols is not None:
            return [(name, self.cols[name]) for name in self.order]
        if self.occ.columns is None:
            raise Unsupported(f"SELECT * over {self.occ.table} needs a schema")
        return [(name, self.occ.col(name)) for name in self.occ.columns]


def _star_columns(star: exp.Star, columns: list[tuple[str, _Val]]) -> list[tuple[str, _Val | exp.Expression]]:
    """What ``*`` or ``t.*`` lists, given what it would list bare (``columns``), after its EXCEPT, REPLACE and RENAME.

    They apply in that order, the order sqlglot reads them in. sqlglot 30 calls EXCEPT ``except_`` and 26
    ``except``: both are read. A REPLACE value comes back as its expression, for the select to compile in its
    FROM scope. A name the star lacks or lists twice (the query fails, or engines differ on the column it
    means), a RENAME onto a name the star has, and any other modifier (``ILIKE``) are declined.
    """

    modifiers = {key.rstrip("_"): value for key, value in star.args.items() if value}
    if set(modifiers) - {"except", "replace", "rename"}:
        raise Unsupported(f"SELECT * {' '.join(sorted(m.upper() for m in modifiers))}")
    out: list = list(columns)

    def position(name: str) -> int:
        found = [i for i, (n, _) in enumerate(out) if n == name]
        if len(found) != 1:
            raise Unsupported(f"SELECT * modifier naming {name}, which the star lists {len(found)} times")
        return found[0]

    def named(node) -> str:
        if isinstance(node, exp.Column) and not node.table and isinstance(node.this, exp.Identifier):
            return node.name.lower()
        raise Unsupported(f"SELECT * modifier naming {node.sql(dialect='bigquery')}")

    for column in modifiers.get("except", ()):
        del out[position(named(column))]
    for key in ("replace", "rename"):
        if any(not isinstance(a, exp.Alias) or not a.alias for a in modifiers.get(key, ())):
            raise Unsupported(f"SELECT * {key.upper()} of this shape")
    replace = [(position(a.alias.lower()), a.this) for a in modifiers.get("replace", ())]
    rename = [(position(named(a.this)), a.alias.lower()) for a in modifiers.get("rename", ())]
    if len({i for i, _ in replace}) != len(replace) or len({i for i, _ in rename}) != len(rename):
        raise Unsupported("SELECT * modifier naming a column twice")
    # Renames read the names before any of them applies; one onto a name the star has (a swap, say) is declined.
    if len({n for _, n in rename}) != len(rename) or any(out[j][0] == n for i, n in rename for j in range(len(out)) if j != i):
        raise Unsupported("SELECT * RENAME onto a name the star has")
    for i, value in replace:
        out[i] = (out[i][0], value)
    for i, name in rename:
        out[i] = (name, out[i][1])
    return out


# --------------------------------------------------------------------------
# Compiler: sqlglot AST -> normal form
# --------------------------------------------------------------------------


def _from_clause(select: exp.Select):
    return select.args.get("from") or select.args.get("from_")


def _is_limit_zero(node: exp.Expression) -> bool:
    limit = node.args.get("limit")
    value = limit.expression if limit is not None else None
    return isinstance(value, exp.Literal) and not value.is_string and value.this == "0"


def _with_clause(query: exp.Expression):
    return query.args.get("with") or query.args.get("with_")


def _parse_number(text: str) -> Fraction:
    try:
        dec = Decimal(text)
    except InvalidOperation as error:
        raise Unsupported(f"numeric literal {text!r}") from error
    if not dec.is_finite():
        raise Unsupported(f"numeric literal {text!r}")
    if dec and not -400 <= dec.adjusted() <= 400:
        # Outside FLOAT64's range (and Fraction(1e100000000) would build a hundred-million-digit integer).
        raise Unsupported(f"numeric literal {text[:40]!r} is out of range")
    value = Fraction(dec)
    if value.denominator == 1 and "." not in text and "e" not in text.lower():
        if abs(value) > 2**53:
            raise Unsupported(f"integer literal {text} is not exactly representable as FLOAT64")
        return value
    # A decimal with at most 15 significant digits maps to a unique FLOAT64,
    # and rounding preserves its order against every other FLOAT64 value.
    if len(dec.normalize().as_tuple().digits) > 15:
        raise Unsupported(f"numeric literal {text} has more than 15 significant digits")
    # Only in the normal FLOAT64 range: below it a literal rounds to a subnormal with fewer digits or to
    # 0 (1e-324 and 2e-324 are both 0), above it to infinity.
    if value and not _FLOAT64_MIN_NORMAL <= abs(value) <= _FLOAT64_MAX:
        raise Unsupported(f"numeric literal {text} is outside the normal FLOAT64 range")
    return value


_FLOAT64_MIN_NORMAL = Fraction(sys.float_info.min)
_FLOAT64_MAX = Fraction(sys.float_info.max)


_INTEGER_TYPES = {exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT}

# Functions that read the sign of a zero (``IEEE_DIVIDE(1, -0.0)`` is -inf, ``SIGN(-0.0)`` is -0.0).
_SIGN_OBSERVERS = ("IEEE_DIVIDE", "Atan2", "Sign")
_NOT_FLOAT = ("INT64", "NUMERIC", "BIGNUMERIC", "STRING", "BOOL", "NULL", "OTHER")
# Calls that never raise an error in BigQuery, whatever their arguments.
_NEVER_FAILS = frozenset((
    "Upper", "Lower", "Length", "Trim", "Concat", "DPipe", "Like", "ILike", "Greatest", "Least", "Coalesce", "Nullif", "If",
    "StartsWith", "EndsWith", "Contains", "IEEE_DIVIDE", "Atan2", "Sign", "Is", "Not", "Paren", "Case",
))


def _nonzero_literal(node: exp.Expression) -> bool:
    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Neg):
        node = node.this
    return isinstance(node, exp.Literal) and not node.is_string and not _float_is_zero(node.this)


def _float_is_zero(text: str) -> bool:
    try:
        return float(text) == 0
    except ValueError:
        return True


# Scalar functions that return NULL whenever an argument is NULL, in every dialect.
_STRICT_FUNCTIONS = tuple(
    getattr(exp, name) for name in ("Round", "Abs", "Floor", "Ceil", "Sqrt", "Ln", "Exp", "Upper", "Lower") if hasattr(exp, name)
)

# Dialects whose decimal literals are exact DECIMAL/NUMERIC values (BigQuery's are FLOAT64).
_EXACT_DECIMAL_LITERALS = {"mysql", "postgres", "duckdb"}

# NULLIF(a, b) has the common type of a and b in these dialects, and a's own type in the second set.
_NULLIF_SUPERTYPE = {"bigquery", "postgres"}
_NULLIF_FIRST_TYPE = {"duckdb", "mysql", "sqlite"}


def _literal_arithmetic(node: exp.Expression, dialect: str) -> Fraction | None:
    """The value of ``+``, ``-``, ``*`` and parentheses over numeric literals, when the engine computes it
    exactly: integers anywhere, decimals only where decimal literals are exact."""

    if dialect == "bigquery":
        typed = smt_values.fold_typed(node)
        if typed is None:
            return None
        kind, value = typed
        return value if kind == "float" or abs(value) <= 2**53 else None
    if isinstance(node, exp.Paren):
        return _literal_arithmetic(node.this, dialect)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            value = _parse_number(node.this)
        except Unsupported:
            return None
        # An exponent literal is FLOAT64/DOUBLE even where decimals are exact (NUMERIC only in PostgreSQL).
        if value.denominator != 1 and (dialect not in _EXACT_DECIMAL_LITERALS or ("e" in node.this.lower() and dialect != "postgres")):
            return None
        return value
    if isinstance(node, exp.Neg):
        value = _literal_arithmetic(node.this, dialect)
        return -value if value is not None else None
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
        a, b = _literal_arithmetic(node.this, dialect), _literal_arithmetic(node.expression, dialect)
        if a is None or b is None:
            return None
        value = a + b if isinstance(node, exp.Add) else a - b if isinstance(node, exp.Sub) else a * b
        return value if abs(value) <= 2**53 else None
    return None


def _exact_literals(node: exp.Expression, dialect: str) -> bool:
    """Whether every numeric literal in ``node`` has an exact type: an integer, or a decimal where
    decimal literals are DECIMAL/NUMERIC (never one with an exponent, which is FLOAT64/DOUBLE)."""

    return not any(
        "e" in lit.this.lower() or ("." in lit.this and dialect not in _EXACT_DECIMAL_LITERALS)
        for lit in node.find_all(exp.Literal) if not lit.is_string
    )


def _float64_kind(node: exp.Expression, dialect: str) -> bool | None:
    """Whether ``node`` visibly has type FLOAT64, so that CASE, COALESCE, IF, NULLIF and set operations
    convert their other numeric operands to FLOAT64 (rounding INT64 and NUMERIC values past 2**53):
    True when it does, False when it visibly does not, None when its type is not visible (a column)."""

    if dialect == "sqlite":
        return False  # dynamic typing: branches keep their own values, no common type is imposed
    if isinstance(node, exp.Paren):
        return _float64_kind(node.this, dialect)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return False
        text = node.this.lower()
        if "e" in text:
            return dialect != "postgres"  # an approximate (DOUBLE) literal; NUMERIC in PostgreSQL
        # BigQuery decimal literals are FLOAT64; elsewhere they are exact DECIMAL/NUMERIC.
        return "." in text and dialect == "bigquery"
    if isinstance(node, (exp.Null, exp.Boolean, exp.Predicate, exp.Connector, exp.Not, exp.Count, exp.CountIf)):
        return False
    if isinstance(node, exp.Neg):
        return _float64_kind(node.this, dialect)
    if isinstance(node, exp.Cast):
        to = node.args["to"]
        if to.is_type(exp.DataType.Type.DOUBLE) or (to.is_type(exp.DataType.Type.FLOAT) and dialect == "bigquery"):
            return True
        if to.this in _INTEGER_TYPES or to.is_type(
            exp.DataType.Type.DECIMAL, exp.DataType.Type.BIGDECIMAL, exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR,
            exp.DataType.Type.BOOLEAN, exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME,
        ):
            return False
        return None
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
        kinds = [_float64_kind(node.this, dialect), _float64_kind(node.expression, dialect)]
        if True in kinds:
            return True
        # INT64 / INT64 is FLOAT64 in BigQuery, so a quotient's type is visible only from a FLOAT64 operand.
        return False if kinds == [False, False] and not isinstance(node, exp.Div) else None
    if isinstance(node, (exp.Coalesce, exp.If, exp.Case, exp.Nullif)):
        kinds = [_float64_kind(b, dialect) for b in _branch_values(node, dialect)]
        return True if True in kinds else False if all(k is False for k in kinds) else None
    if isinstance(node, (exp.Sum, exp.Min, exp.Max, exp.Avg)) and isinstance(node.this, exp.Expression):
        return True if _float64_kind(node.this, dialect) else None
    return None


_FLOAT_TYPES = {"FLOAT64", "FLOAT", "FLOAT32", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"}


def _type_is_float(declared: str) -> bool | None:
    """Whether a declared column type is a binary floating type (None when it does not say)."""

    name = re.sub(r"\(.*", "", declared).strip().upper()
    return name in _FLOAT_TYPES if name else None


def _plain_literal(node: exp.Expression, dialect: str) -> bool:
    """A literal (or literal arithmetic folded exactly): its model value stands for it in every numeric type."""

    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, (exp.Null, exp.Boolean, exp.Literal)):
        return True
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return True
    return _literal_arithmetic(node, dialect) is not None


def _literal_value(node: exp.Expression, dialect: str) -> Fraction:
    """The model value of a numeric ``_plain_literal`` (0 for NULL and booleans)."""

    def number(text: str, negate: bool) -> Fraction:
        if dialect != "bigquery":
            value = _parse_number(text)
            return -value if negate else value
        try:
            return smt_values.literal_value(text, negate)  # the FLOAT64 nearest a decimal, not its exact text
        except smt_values.NotModeled as error:
            raise Unsupported(str(error)) from error

    node = node.unnest() if isinstance(node, exp.Paren) else node
    if isinstance(node, exp.Literal) and not node.is_string:
        return number(node.this, False)
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return number(node.this.this, True)
    return _literal_arithmetic(node, dialect) or Fraction(0)


def _branch_values(node: exp.Expression, dialect: str) -> list[exp.Expression]:
    """The expressions whose common type is the type of a CASE, COALESCE, IF or NULLIF."""

    if isinstance(node, exp.Coalesce):
        return [node.this, *node.expressions]
    if isinstance(node, exp.If):
        return [v for v in (node.args.get("true"), node.args.get("false")) if v is not None]
    if isinstance(node, exp.Case):
        return [b.args["true"] for b in node.args.get("ifs") or []] + (
            [node.args["default"]] if node.args.get("default") is not None else []
        )
    # NULLIF: the supertype of both arguments, or the first argument's type
    return [node.this, node.expression] if dialect in _NULLIF_SUPERTYPE else [node.this]


def _output_kinds(node: exp.Expression, dialect: str) -> list | None:
    """``_float64_kind`` of each output column of a query (None for the whole list when not visible)."""

    while isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.SetOperation):
        left, right = _output_kinds(node.this, dialect), _output_kinds(node.expression, dialect)
        if left is None or right is None or len(left) != len(right):
            return None
        return [True if True in pair else False if pair == (False, False) else None for pair in zip(left, right)]
    if isinstance(node, exp.Select):
        if any(isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)) for item in node.expressions):
            return None
        return [_float64_kind(item.this if isinstance(item, exp.Alias) else item, dialect) for item in node.expressions]
    return None


class _AggCtx:
    def __init__(self, compiler: "_Compiler"):
        self.compiler = compiler
        self.calls: list[_AggCall] = []


# Every outer join doubles the cases a select is compiled into; past this many the compiler gives up, not memory.
MAX_OUTER_JOIN_CASES = 256


class _Compiler:
    def __init__(self, schema: dict[str, list[str]] | None, exact_arithmetic: bool, dialect: str = "bigquery", types=None):
        self.dialect = dialect
        # Declared column types by table key, and the table of each base-table occurrence.
        self.types = {key.lower(): {c.lower(): t for c, t in cols.items()} for key, cols in (types or {}).items()}
        self.occ_tables: dict[str, str] = {}
        # A comparison, or a CASE/COALESCE/IF/NULLIF or set-operation column, met values whose numeric types are
        # not known to agree: the proof assumes they do (MIXED_NUMERIC_ASSUMPTION).
        self.mixed_numeric = False
        self.schema = {
            key.lower(): [c.lower() for c in cols] for key, cols in (schema or {}).items()
        }
        self.exact = exact_arithmetic
        self.counter = itertools.count()
        self.expansion = 0
        self.uses_uf = False
        self.bounded_sums = False  # a SUM of a plain column can only overflow on a database of large values
        self.functions: dict[tuple[str, int], tuple] = {}
        # Hypotheses every valid query satisfies, e.g. arithmetic operands are
        # numbers in exact mode.
        self.facts: list = []
        # Existence tests (EXISTS, IN) found while compiling the current select.
        self.collector: list = []
        self.ctes: dict = {}
        # Canonical SQL of each opaque derived relation, by identity key.
        self.opaque_bodies: dict[str, tuple[str, int, str]] = {}  # key -> (BigQuery text, arity, text in self.dialect)
        # Opaque derived relations that are sets (DISTINCT, or grouped on exactly their outputs).
        self.opaque_sets: set[str] = set()
        # Read DISTINCT derived tables as sets (existence tests) instead of opaque relations.
        self.semijoin = True
        # Read a DISTINCT derived table in a duplicate-blind select as the rows it deduplicates.
        self.blind_sets = False
        self._blind = False
        self.used_setsrc = False
        self.limit_opaque = False
        self.numeric_differences = False  # ABS(x - y) was read with x and y numbers
        self.window_opaque = False
        # Whether values are ordered (<, MIN, ..): then string ordering facts are needed.
        self.ordered = False
        # Runtime errors (BigQuery dialect): every operation that can fail (``smt_errors.Site``), the guards of the
        # CASE/IF/COALESCE branch being compiled, and the base-table occurrences read (for declared-type facts).
        self.sites: list = []
        self._guard: list = []
        self._safe = 0  # inside SAFE.function(..): its own failures are NULL
        self.occs_seen: list = []
        self.untyped_sources = False  # an UNNEST or opaque source, whose columns have no declared type
        self.big_literals: list[str] = []  # INT64 literals past 2**53, which need an all-INT64 context
        self.float_capable = False  # a literal, cast or function in the queries that may produce a FLOAT64
        # BigQuery types a decimal literal with the operands around it (smt_literal_coercion): the value planned for
        # each literal by the operator being compiled, and one unknown numeric kind per operand of undeclared type.
        self._literal_plans: dict[int, tuple] = {}
        self._typed_literals: dict[int, exp.Literal] = {}
        self.kind_unknowns: dict = {}
        self.sums = Ledger()  # SUM and AVG calls, for the row-order assumption (float_sum_order.py)
        self.string_literals: set[str] = set()
        self.timestamp_literals: set[str] = set()
        # The independent reader of the statement being compiled (``proof_columns``) and the FROM-item number of each
        # source it has seen, to compare a bare column's owner with the one the text names.
        self.column_reader = None
        self.source_tags: dict[int, tuple] = {}

    def fresh(self, prefix: str) -> str:
        return f"{prefix}{next(self.counter)}"

    # ---- queries -------------------------------------------------------

    def compile(self, sql: str) -> _Union:
        try:
            self.column_reader = proof_columns.reader_for(sql, self.schema, self.dialect)
            statements = [expand_alias_columns(check_args(check_modeled(canonical_negation(s))), self.schema) for s in proof_columns.parse_tagged(sql, self.dialect, self.column_reader)]
        except UnmodeledConstruct as error:
            raise Unsupported(str(error)) from error
        except proof_columns.ColumnResolutionRefused as refusal:
            raise Unsupported(f"independent check of column resolution: {refusal}") from None
        if len(statements) != 1:
            raise Unsupported(f"expected one statement, found {len(statements)}")
        statement = statements[0]
        self.float_capable = self.float_capable or smt_values.float_capable(statement)
        self.sums.statement(statement)
        if any(statement.find_all(exp.Pivot)):
            # PIVOT / UNPIVOT reshape columns and rows; they are not modeled, so never claim equivalence.
            raise Unsupported("PIVOT and UNPIVOT are not modeled")
        # Inside a comparison of two opaque bodies (``_unify_opaque``) the kqw* markers are renamed away;
        # a window there still raises Unsupported wherever it would be evaluated, or sits in an opaque body.
        self._check_nondeterminism(statement, allow_windows=_nesting[0] > 0)
        self.window_opaque = any(statement.find_all(exp.Window))
        return self._query(statement, {})

    def _check_nondeterminism(self, node: exp.Expression, allow_windows: bool = False) -> None:
        for sub in node.walk():
            name = type(sub).__name__
            if name in _NONDETERMINISTIC_TYPES:
                raise Unsupported(f"nondeterministic: {sub.sql(dialect='bigquery')}")
            if isinstance(sub, exp.Anonymous) and (sub.name or "").upper() in _NONDETERMINISTIC_NAMES:
                raise Unsupported(f"nondeterministic: {sub.sql(dialect='bigquery')}")
            if isinstance(sub, exp.Window) and not allow_windows:
                # The normalizer isolates window functions in a derived table named kqw*; it is kept whole.
                owner = sub.find_ancestor(exp.Select)
                holder = owner.parent if owner is not None else None
                if not (isinstance(holder, exp.Subquery) and (holder.alias or "").startswith("kqw")):
                    raise Unsupported(f"{name.upper()} is not modeled")

    def _query(self, node: exp.Expression, ctes: dict) -> _Union:
        # The tail of a parenthesized query hangs on the parentheses, not on the query inside.
        node = merge_wrapper_tails(node)
        if node is None:
            raise Unsupported("LIMIT is not modeled")
        if node.args.get("limit") or node.args.get("offset"):
            if _is_limit_zero(node):
                # Nothing survives LIMIT 0: the columns, with no rows.
                bare = node.copy()
                for key in ("limit", "offset", "order"):
                    bare.set(key, None)
                result = self._query(bare, ctes)
                result.column_names = list(result.names)
                result.branches = []
                return result
            raise Unsupported("LIMIT is not modeled")
        with_clause = _with_clause(node)
        if with_clause is not None:
            if with_clause.args.get("recursive"):
                raise Unsupported("recursive CTEs")
            ctes = dict(ctes)
            for cte in with_clause.expressions:
                ctes[cte.alias_or_name.lower()] = (cte.this, dict(ctes))
        if type(node) in (exp.Intersect, exp.Except):
            return self._set_operation(node, ctes)
        if type(node) is exp.Union:
            if node.args.get("by_name") or node.args.get("side") or node.args.get("kind") or node.args.get("on"):
                raise Unsupported("UNION variants other than ALL/DISTINCT")
            distinct = bool(node.args.get("distinct"))
            branches = []
            children = [node.this, node.expression]
            subs = [self._query(self._cut_branch(child), ctes) for child in children]
            self._common_column_types(children, subs)
            for sub in subs:
                if not sub.distinct or distinct:
                    branches.extend(sub.branches)
                elif len(sub.branches) == 1:
                    sub.branches[0].distinct = True
                    branches.append(sub.branches[0])
                else:
                    raise Unsupported("UNION DISTINCT nested inside UNION ALL")
            widths = {len(b.outputs) for b in branches}
            if len(widths) != 1:
                raise Unsupported("UNION branches have different widths")
            return _Union(branches, distinct)
        if isinstance(node, exp.Select):
            result = self._select(node, ctes)
            return result if isinstance(result, _Union) else _Union([result], False)
        raise Unsupported(f"{type(node).__name__} statements")

    def _cut_branch(self, child: exp.Expression) -> exp.Expression:
        """A ``UNION`` branch ending in ``ORDER BY .. LIMIT`` read as a derived table kept whole, as a
        ``FROM`` subquery with a LIMIT is (under ``LIMIT_SOURCE_ASSUMPTION``)."""

        body = child
        while isinstance(body, exp.Subquery) and not any(body.args.get(k) for k in ("limit", "offset", "order")):
            body = body.this
        if not isinstance(body, exp.Select) or not (body.args.get("limit") or body.args.get("offset")):
            return child
        if body.args.get("order") is None or _is_limit_zero(body):
            return child
        names = [e.alias_or_name for e in body.expressions]
        if not names or any(isinstance(e, exp.Star) or not n for e, n in zip(body.expressions, names)) or len({n.lower() for n in names}) != len(names):
            return child
        alias = self.fresh("kq_cut")
        return exp.select(*[exp.column(n, table=alias) for n in names]).from_(exp.Subquery(this=body.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))

    def _common_column_types(self, children: list, subs: list[_Union]) -> None:
        """Convert the outputs of set-operation operands to the common type of each column (``_coerce``)."""

        kinds = [_output_kinds(child, self.dialect) or [] for child in children]
        if self.dialect == "bigquery":
            self._check_literal_columns(children, kinds)
        blocks = [(kind, block) for kind, sub in zip(kinds, subs) for block in sub.branches]
        for j in range(min((len(block.outputs) for _, block in blocks), default=0)):
            entries = []
            for kind, block in blocks:
                v = block.outputs[j]
                literal = z3.is_true(v.null) or (not self.exact and _is_ground(v.val))
                entries.append((kind[j] if j < len(kind) else None, literal, v))
            for (_, block), v in zip(blocks, self._coerce(entries)):
                block.outputs[j] = v

    def _check_literal_columns(self, children: list, kinds: list) -> None:
        """A decimal literal that is a column of a set-operation operand takes the type of that column in the other
        operands (``SELECT n FROM t UNION ALL SELECT 0.1`` holds the NUMERIC 0.1, not a double). The prover reads it
        as a double, so it declines unless every other operand's column is a literal or visibly FLOAT64."""

        C = smt_literal_coercion

        def columns(node):
            while isinstance(node, exp.Subquery):
                node = node.this
            if isinstance(node, exp.SetOperation):
                left, right = columns(node.this), columns(node.expression)
                return None if left is None or right is None else [a + b for a, b in zip(left, right)]
            if isinstance(node, exp.Select) and not any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in node.expressions):
                return [[i.this if isinstance(i, exp.Alias) else i] for i in node.expressions]
            return None

        outputs = [columns(child) for child in children]
        for index, mine in enumerate(outputs):
            for j, items in enumerate(mine or []):
                if not any(C.unstable(item) for item in items):
                    continue
                for other, other_kinds in zip(outputs[:index] + outputs[index + 1:], kinds[:index] + kinds[index + 1:]):
                    floating = j < len(other_kinds) and other_kinds[j] is True
                    literal = other is not None and j < len(other) and all(_plain_literal(item, self.dialect) for item in other[j])
                    if not (floating or literal):
                        raise Unsupported("a decimal literal in a set operation takes the type of the other operand's column")

    def _set_operation(self, node: exp.Expression, ctes: dict) -> _Union:
        """``A INTERSECT B`` and ``A EXCEPT B`` (set semantics) as existence tests.

        Each row of ``A`` is kept (INTERSECT) or dropped (EXCEPT) when some row
        of ``B`` equals it, NULLs comparing equal; the result has no duplicates.
        """

        if not node.args.get("distinct", True) or node.args.get("by_name") or node.args.get("side"):
            raise Unsupported(f"{type(node).__name__.upper()} ALL")
        left = self._query(node.this, ctes)
        right = self._query(node.expression, ctes)
        self._common_column_types([node.this, node.expression], [left, right])
        if len(left.names) != len(right.names) or any(
            len(b.outputs) != len(left.branches[0].outputs) for b in left.branches + right.branches
        ):
            raise Unsupported("set operation branches have different widths")
        if any(not isinstance(b, _Spj) for b in left.branches + right.branches):
            raise Unsupported("set operation over an aggregate")
        blocks = []
        for a in left.branches:
            subs = list(a.subs)
            atoms = []
            for b in right.branches:
                atom = z3.Bool(f"{self.fresh('ex')}#exists")
                guard = z3.And(b.cond.t, _rows_eq(a.outputs, b.outputs))
                subs.append(_Sub(atom, list(b.occs), guard, list(b.subs)))
                atoms.append(atom)
            found = z3.Or(*atoms) if atoms else z3.BoolVal(False)
            keep = found if type(node) is exp.Intersect else z3.Not(found)
            cond = _Pred(z3.And(a.cond.t, keep), z3.Not(z3.And(a.cond.t, keep)))
            blocks.append(
                _Spj(list(a.occs), cond, a.outputs, a.names, distinct=False, facts=a.facts + [f for b in right.branches for f in b.facts], subs=subs)
            )
        return _Union(blocks, True, column_names=list(left.names))

    def _expand(self) -> None:
        self.expansion += 1
        if self.expansion > _MAX_EXPANSION:
            raise _Exhausted(f"the queries expand to more than {_MAX_EXPANSION} selects and table reads")

    def _select(self, node: exp.Select, ctes: dict):
        self._expand()
        for key in ("qualify", "laterals", "pivots", "connect", "match", "prewhere", "windows", "into"):
            if node.args.get(key):
                raise Unsupported(f"{key.upper()} clause")
        distinct_node = node.args.get("distinct")
        if distinct_node is not None and distinct_node.args.get("on"):
            raise Unsupported("DISTINCT ON")
        if node.args.get("kind"):
            raise Unsupported("SELECT AS STRUCT/VALUE")

        facts_start = len(self.facts)
        outer_collector, self.collector = self.collector, []
        outer_ctes, self.ctes = self.ctes, ctes
        try:
            return self._select_body(node, ctes, distinct_node, facts_start)
        finally:
            self.collector = outer_collector
            self.ctes = outer_ctes

    def _scan(self, node: exp.Select, ctes: dict, outer: "_Env | None" = None) -> list[_State]:
        """Sources, joins and WHERE of one select, as one state per outer-join case."""

        outer_blind, self._blind = self._blind, self.blind_sets and _duplicate_blind(node)
        try:
            return self._scan_sources(node, ctes, outer)
        finally:
            self._blind = outer_blind

    def _scan_sources(self, node: exp.Select, ctes: dict, outer: "_Env | None" = None) -> list[_State]:

        first = _State([], [], _Env(), [])
        first.env.outer = outer
        states = [first]
        from_clause = _from_clause(node)
        items = []
        if from_clause is not None:
            items.append((from_clause.this, None))
            for join in node.args.get("joins") or []:
                kind = (join.args.get("kind") or "").upper()
                side = (join.args.get("side") or "").upper()
                if join.args.get("method") or join.args.get("using") or kind not in ("", "INNER", "CROSS", "OUTER"):
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                if side not in ("", "LEFT", "RIGHT", "FULL"):
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                if kind == "OUTER" and not side:
                    raise Unsupported(f"join: {join.sql(dialect='bigquery')}")
                items.append((join.this, join))
        for source_node, join in items:
            side = (join.args.get("side") or "").upper() if join is not None else ""
            if isinstance(source_node, exp.Unnest):
                if side or (join is not None and join.args.get("on") is not None) or len(source_node.expressions) != 1:
                    raise Unsupported("UNNEST outside a cross join")
                alias_node = source_node.args.get("alias")
                columns = alias_node.args.get("columns") if alias_node is not None else None
                # BigQuery names the element ``e`` in ``UNNEST(..) AS e``; it hides an outer alias ``e``
                alias = (alias_node.name if alias_node is not None else "") or (columns[0].name if columns else "")
                alias = (alias or self.fresh("unnest")).lower()
                element = (columns[0].name if columns else alias).lower()
                offset_arg = source_node.args.get("offset")
                offset = ((offset_arg.name if isinstance(offset_arg, exp.Expression) and offset_arg.name else "offset") if offset_arg else "").lower()
                occ = _Occ(_UNNEST_TABLE, self.fresh("u"), ["arr", "value", "offset"])
                self.untyped_sources = True
                cols = {element: occ.col("value")}
                if offset:
                    cols[offset] = occ.col("offset")
                source = _Source(cols=cols, order=list(cols), value_table=alias == element)
                self._tag_source(source, source_node)
                new_states = []
                for st in states:
                    if alias in st.env:
                        raise Unsupported(f"duplicate alias {alias}")
                    saved, self.collector = self.collector, st.subs
                    try:
                        array = self._val(source_node.expressions[0], st.env, None, None)
                    finally:
                        self.collector = saved
                    arr = occ.col("arr")
                    linked = z3.And(z3.Not(array.null), z3.Not(arr.null), array.val == arr.val)
                    joined_env = _Env(st.env)
                    joined_env.outer = st.env.outer
                    joined_env[alias] = source
                    new_states.append(_State(st.occs + [occ], st.conds + [_Pred(linked, z3.Not(linked))], joined_env, list(st.subs)))
                states = new_states
                continue
            b_occs: list[_Occ] = []
            b_conds: list[_Pred] = []
            b_subs: list = []
            saved, self.collector = self.collector, b_subs
            try:
                alias, source = self._source(source_node, ctes, b_occs, b_conds)
            finally:
                self.collector = saved
            self._tag_source(source, source_node)
            on = join.args.get("on") if join is not None else None
            new_states: list[_State] = []
            for st in states:
                if alias in st.env:
                    raise Unsupported(f"duplicate alias {alias}")
                joined_env = _Env(st.env)
                joined_env.outer = st.env.outer
                joined_env[alias] = source
                on_subs: list = []
                saved, self.collector = self.collector, on_subs
                try:
                    c = self._pred(on, joined_env, None, None) if on is not None else _const_pred(True)
                finally:
                    self.collector = saved
                new_states.append(
                    _State(st.occs + b_occs, st.conds + b_conds + ([c] if on is not None else []), joined_env, st.subs + b_subs + on_subs)
                )
                if side in ("LEFT", "FULL"):
                    atom = z3.Bool(f"{self.fresh('ex')}#exists")
                    guard = z3.And(*[x.t for x in b_conds], c.t)
                    anti = _Sub(atom, list(b_occs), guard, b_subs + on_subs)
                    env2 = _Env(st.env)
                    env2.outer = st.env.outer
                    env2[alias] = self._null_source(source)
                    new_states.append(
                        _State(list(st.occs), st.conds + [_Pred(z3.Not(atom), atom)], env2, st.subs + [anti])
                    )
                if side in ("RIGHT", "FULL"):
                    atom = z3.Bool(f"{self.fresh('ex')}#exists")
                    guard = z3.And(*[x.t for x in st.conds], c.t)
                    anti = _Sub(atom, list(st.occs), guard, st.subs + on_subs)
                    env2 = self._null_env(st.env)
                    env2[alias] = source
                    new_states.append(
                        _State(list(b_occs), b_conds + [_Pred(z3.Not(atom), atom)], env2, b_subs + [anti])
                    )
            states = new_states
            if len(states) > MAX_OUTER_JOIN_CASES:
                raise Unsupported(f"more than {MAX_OUTER_JOIN_CASES} outer-join cases in one select")
        if len(states) > 1 and outer is not None:
            raise Unsupported("outer join inside a subquery")
        for st in states:
            if node.args.get("where") is not None:
                saved, self.collector = self.collector, st.subs
                try:
                    st.conds.append(self._pred(node.args["where"].this, st.env, None, None))
                finally:
                    self.collector = saved
        return states

    def _tag_source(self, source: "_Source", node: exp.Expression, of: "_Source | None" = None) -> None:
        """Remember which FROM item of the statement ``source`` stands for (``proof_columns.source_tag``)."""

        if self.column_reader is None:
            return
        number = proof_columns.source_tag(node) if of is None else self.source_tags.get(id(of), (None, None))[1]
        previous = self.source_tags.get(id(source))
        self.source_tags[id(source)] = (source, number if previous is None or previous[1] == number else None)

    def _null_source(self, source: "_Source") -> "_Source":
        result = self._null_source_of(source)
        if result is not source:
            self._tag_source(result, None, of=source)
        return result

    def _null_source_of(self, source: "_Source") -> "_Source":
        V = _value_sort()
        null = _Val(z3.BoolVal(True), V.Num(0))
        if source.all_null is not None:
            return source
        if source.cols is None and source.occ is not None and source.occ.columns is None:
            return _Source(all_null=null)
        names = [name for name, _ in source.star()]
        return _Source(cols={name: null for name in names}, order=list(names))

    def _judge_column(self, col: exp.Column, claim: "proof_columns.Claim") -> None:
        """Have the independent reader confirm the owner the compiler chose for a bare column."""

        if self.column_reader is None:
            return
        verdict = self.column_reader.judge_tagged(col, claim, "smt")
        if verdict.refused:
            raise Unsupported(f"independent check of column resolution: {verdict.reason}")

    def _chosen(self, source: "_Source") -> "proof_columns.Claim":
        entry = self.source_tags.get(id(source))
        return proof_columns.Claim(source=entry[1] if entry is not None and entry[0] is source else None)

    def _null_env(self, env: "_Env") -> "_Env":
        """The scope with every source's columns replaced by NULL (the unmatched side of an outer join)."""

        result = _Env()
        result.outer = env.outer
        for alias, source in env.items():
            result[alias] = self._null_source(source)
        return result

    def _assign_scope(self, start: int, states: list) -> None:
        """The tables of this ``FROM`` are part of the scope of every error site compiled since ``start``."""

        scope = {occ.uid: occ for st in states for occ in st.occs}
        for site in self.sites[start:]:
            site.scope.update(scope)

    def _select_body(self, node: exp.Select, ctes: dict, distinct_node, facts_start: int):
        sites_start = len(self.sites)
        states = self._scan(node, ctes)
        blocks = []
        for st in states:
            saved, self.collector = self.collector, st.subs
            try:
                blocks.append(self._finish_select(node, distinct_node, facts_start, st, len(states) > 1))
            finally:
                self.collector = saved
        self._assign_scope(sites_start, states)
        if len(blocks) == 1:
            return blocks[0]
        union = _Union(blocks, distinct_node is not None)
        for block in blocks:
            block.distinct = False
        return union

    def _finish_select(self, node: exp.Select, distinct_node, facts_start: int, state: _State, split: bool):
        occs, cond, env = state.occs, state.cond(), state.env
        group = node.args.get("group")
        having = node.args.get("having")
        agg_ctx = _AggCtx(self)
        sites_start = len(self.sites)
        names: list[str] = []
        outputs: list[_Val] = []
        aliases: dict[str, exp.Expression] = {}
        for item in node.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                if isinstance(item, exp.Star):
                    sources = list(env.values())
                else:
                    table = item.table.lower()
                    if table not in env or env[table].value_table:
                        raise Unsupported(f"unknown alias {table}")
                    sources = [env[table]]
                # ``t.*`` keeps its EXCEPT/REPLACE/RENAME on the Star under the column.
                star = item if isinstance(item, exp.Star) else item.this
                for name, val in _star_columns(star, [pair for source in sources for pair in source.star()]):
                    if isinstance(val, exp.Expression):
                        aliases[name] = val  # ``* REPLACE (e AS a)`` names ``e`` as ``e AS a`` does
                        val = self._val(val, env, agg_ctx, None)
                    names.append(name)
                    outputs.append(val)
                continue
            expr = item.this if isinstance(item, exp.Alias) else item
            name = item.alias_or_name.lower() if isinstance(item, (exp.Alias, exp.Column)) else ""
            if name:
                aliases[name] = expr
            names.append(name)
            outputs.append(self._val(expr, env, agg_ctx, None))

        is_agg = group is not None or bool(agg_ctx.calls) or having is not None
        if is_agg and split:
            raise Unsupported("aggregate over an outer join")
        if not is_agg:
            return _Spj(
                occs, cond, outputs, names, distinct=distinct_node is not None, facts=self.facts[facts_start:],
                subs=state.subs,
            )

        keys: list[_Val] = []
        if group is not None:
            if extended_grouping(group):
                raise Unsupported("GROUP BY ROLLUP / CUBE / GROUPING SETS / ()")
            for key_expr in group.expressions:
                if isinstance(key_expr, exp.Literal) and not key_expr.is_string:
                    if not key_expr.this.isdigit():
                        raise Unsupported(f"GROUP BY {key_expr.this}")
                    position = int(key_expr.this)
                    if not 1 <= position <= len(outputs):
                        raise Unsupported(f"GROUP BY {position}")
                    keys.append(outputs[position - 1])
                else:
                    keys.append(self._val(key_expr, env, None, aliases))
        having_pred = self._pred(having.this, env, agg_ctx, aliases) if having is not None else None
        block = _Agg(
            occs,
            cond,
            keys,
            agg_ctx.calls,
            having_pred,
            outputs,
            names,
            is_global=group is None,
            distinct=distinct_node is not None,
            facts=self.facts[facts_start:],
            subs=state.subs,
        )
        smt_group_sums.attach(self, sites_start, block)
        return block

    def _source(self, node, ctes, occs, conds) -> tuple[str, _Source]:
        if isinstance(node, exp.Table):
            if node.args.get("joins") or node.args.get("pivots") or node.args.get("laterals"):
                raise Unsupported("table modifiers")
            if isinstance(node.this, (exp.Anonymous, exp.Func)):
                raise Unsupported("table functions")
            alias = node.alias_or_name.lower()
            name = node.name.lower()
            if not node.db and not node.catalog and name in ctes:
                body, cte_env = ctes[name]
                return alias, self._derived(body, cte_env, occs, conds)
            key = self._table_key(node)
            self._expand()
            occ = _Occ(key, self.fresh("r"), self.schema.get(key.lower()))
            self.occ_tables[occ.uid] = key.lower()
            self.occs_seen.append(occ)
            occs.append(occ)
            return alias, _Source(occ=occ)
        if isinstance(node, exp.Subquery):
            alias = node.alias_or_name.lower() or self.fresh("anon")
            return alias, self._derived(node.this, ctes, occs, conds)
        raise Unsupported(f"FROM item {type(node).__name__}")

    @staticmethod
    def _table_key(node: exp.Table) -> str:
        parts = [p.name for p in node.parts]
        return ".".join(parts)

    def _derived(self, body, ctes, occs, conds) -> _Source:
        saved, saved_sites = len(self.facts), len(self.sites)
        try:
            sub = self._query(body, ctes)
        except _Exhausted:
            raise
        except Unsupported:
            del self.facts[saved:]
            dropped = len(self.sites) > saved_sites
            del self.sites[saved_sites:]  # what the failed read recorded is not scoped: the relation kept whole stands for it
            return self._opaque(body, ctes, occs, read=False, dropped=dropped)
        one_row = self._one_row_source(sub, body, ctes)
        if one_row is not None:
            return one_row
        # Joined into a duplicate-blind select, a DISTINCT derived table and the bag it
        # deduplicates give the same set of output rows.
        blind = self._blind and not sub.distinct
        if len(sub.branches) == 1 and isinstance(sub.branches[0], _Spj) and (blind or not sub.branches[0].distinct):
            branch = sub.branches[0]
            if len(set(branch.names)) != len(branch.names) or "" in branch.names:
                raise Unsupported("derived table with unnamed or duplicate columns")
            occs.extend(branch.occs)
            conds.append(branch.cond)
            self.collector.extend(branch.subs)
            return _Source(cols=dict(zip(branch.names, branch.outputs)), order=list(branch.names))
        if self.semijoin and len(sub.branches) == 1 and not sub.distinct:
            branch = sub.branches[0]
            is_set = isinstance(branch, _Spj) and branch.distinct
            is_set = is_set or (isinstance(branch, _Agg) and not branch.aggs and branch.having is None and not branch.is_global)
            names = list(branch.names)
            if is_set and names and len(set(names)) == len(names) and "" not in names:
                return self._set_source(branch, names, conds)
        return self._opaque(body, ctes, occs)

    def _one_row_source(self, sub: _Union, body, ctes) -> _Source | None:
        """A derived global aggregate (no GROUP BY, no HAVING) always has exactly one row.

        Joined into a query it changes no multiplicity, so it is read as one row
        of values that may be anything an aggregate returns, the same values
        wherever the same relation (by its CTE-expanded SQL) appears; COUNT is a
        number of at least zero.
        """

        if len(sub.branches) != 1 or sub.distinct:
            return None
        block = sub.branches[0]
        if not isinstance(block, _Agg) or not block.is_global or block.having is not None or block.subs:
            return None
        names = list(block.names)
        if not names or "" in names or len(set(names)) != len(names):
            return None
        expanded = self._expand_ctes(body.copy(), ctes)
        self._check_nondeterminism(expanded)
        self.uses_uf = True  # the values are free: a model is not a database, so no counterexample
        key = _canonical_aliases(expanded, self.schema, self.column_reader).sql(dialect="bigquery", normalize_functions="upper")
        tag = hashlib.sha1(key.encode()).hexdigest()[:16]
        V = _value_sort()
        counts = {id(call.var): call for call in block.aggs if call.func in ("COUNT", "COUNTIF")}
        cols = {}
        for index, (name, output) in enumerate(zip(names, block.outputs)):
            call = next((c for c in counts.values() if c.var.val.eq(output.val) and c.var.null.eq(output.null)), None)
            val = z3.Const(f"one{tag}.{index}", V)
            if call is not None:
                self.facts.append(z3.And(V.is_Num(val), V.num(val) >= 0))
                cols[name] = _Val(z3.BoolVal(False), val)
            else:
                cols[name] = _Val(z3.Bool(f"one{tag}.{index}#null"), val)
        return _Source(cols=cols, order=names)

    def _set_source(self, branch, names, conds) -> _Source:
        """A DISTINCT (or key-only GROUP BY) derived table as an existence test.

        Its columns are free values, NULL or not; the atom says some row of the
        table has exactly those values (NULL matching NULL, as DISTINCT does).
        Joined on all of its columns, each outer row matches at most one row, so
        the join is that test (``resolve_set_sources``).
        """

        V = _value_sort()
        uid = self.fresh("set")
        # Not ``#null``: ``resolve_set_sources`` reads that suffix as an outer column's NULL flag.
        columns = [_Val(z3.Bool(f"{uid}.{i}#setnull"), z3.Const(f"{uid}.{i}", V)) for i in range(len(names))]
        match = [_null_eq(o, c) for o, c in zip(branch.outputs, columns)]
        atom = z3.Bool(f"{self.fresh('ex')}#exists")
        guard = z3.And(branch.cond.t, *match)
        self.collector.append(_Sub(atom, list(branch.occs), guard, list(branch.subs), setsrc=(branch, columns)))
        conds.append(_Pred(atom, z3.Not(atom)))
        self.used_setsrc = True
        return _Source(cols=dict(zip(names, columns)), order=list(names))

    def _opaque(self, body, ctes, occs, read: bool = True, dropped: bool = False) -> _Source:
        """A derived relation kept whole: identified by its CTE-expanded SQL."""

        body = self._expand_ctes(body.copy(), ctes)
        self._check_nondeterminism(body, allow_windows=True)
        self.window_opaque = self.window_opaque or any(body.find_all(exp.Window))
        if any(isinstance(node, (exp.Limit, exp.Offset)) for node in body.walk()):
            self.limit_opaque = True
        # Kept whole, the relation must not depend on the outer query.
        if any(isinstance(node, (exp.Unnest, exp.Lateral)) for node in body.walk()):
            raise Unsupported("UNNEST or LATERAL inside a derived relation")
        declared = {
            node.alias_or_name.lower()
            for node in body.walk()
            if isinstance(node, (exp.Table, exp.Subquery, exp.CTE))
        }
        for column in body.find_all(exp.Column):
            if column.table and column.table.lower() not in declared:
                raise Unsupported(f"derived relation references outer alias {column.table}")
        inner = body
        while isinstance(inner, exp.Subquery):
            inner = inner.this
        first = inner
        while isinstance(first, exp.Union):
            first = first.this
            while isinstance(first, exp.Subquery):
                first = first.this
        if not isinstance(first, exp.Select):
            raise Unsupported("derived relation shape")
        names = []
        for item in first.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                raise Unsupported("SELECT * inside a derived relation")
            names.append(item.alias_or_name.lower())
        if "" in names or len(set(names)) != len(names):
            raise Unsupported("derived relation with unnamed or duplicate columns")
        canonical = _canonical_aliases(body, self.schema, self.column_reader)
        position = {i: i for i in range(len(names))}
        root = canonical
        while isinstance(root, exp.Subquery):
            root = root.this
        if isinstance(root, exp.Select) and len(root.expressions) == len(names):
            # Columns are identified by position, so two spellings of the output names are one relation,
            # unless the body refers to its own output names (GROUP BY alias, HAVING alias).
            in_select = {id(c) for item in root.expressions for c in item.find_all(exp.Column)}
            outside = {c.name.lower() for c in root.find_all(exp.Column) if not c.table and id(c) not in in_select}
            if not outside & set(names):
                # A windowed relation (matched by its text alone) lists its outputs by their own text, so a
                # permutation of the select list is the same relation, each name reading the column it
                # computes. Others keep their order: two of them proven equal are matched by position.
                values = [(i.this if isinstance(i, exp.Alias) else i) for i in root.expressions]
                order = list(range(len(values)))
                if any(root.find_all(exp.Window)):
                    order.sort(key=lambda n: values[n].sql(dialect="bigquery", normalize_functions="upper"))
                position = {old: new for new, old in enumerate(order)}
                root.set("expressions", [exp.alias_(values[old].copy(), f"c{new}") for new, old in enumerate(order)])
        text = canonical.sql(dialect="bigquery", normalize_functions="upper")
        lost = _lost_null_ordering(canonical, text)
        key = "(" + text + ")" + lost
        occ = _Occ(key, self.fresh("d"), names, opaque=True)
        self.untyped_sources = True
        occs.append(occ)
        if not read and (dropped or any(body.find_all(exp.Sum))):
            smt_group_sums.opaque_site(self, occ, any(body.find_all(exp.Sum)))  # what the compiler did not read can still fail
        if not lost:  # a body whose text lost a NULLS placement is matched by its key alone, never re-proved
            own = self._own_dialect_text(canonical, text)
            if own is not None:
                self.opaque_bodies[key] = (key[1:-1], len(names), own)
        if isinstance(inner, exp.Select) and _selects_a_set(inner):
            self.opaque_sets.add(key)
        return _Source(cols={name: occ.col(f"c{position[i]}") for i, name in enumerate(names)}, order=list(names))

    def _own_dialect_text(self, canonical: exp.Expression, text: str) -> str | None:
        """An opaque body as text the dialect of this proof reads back as the same body (``text`` is its BigQuery
        spelling, which names it), or ``None`` when no spelling does.

        ``_unify_opaque`` proves two bodies equal with the prover itself, and that proof has to read them as the caller's
        dialect does: ``1.00`` is an exact decimal in MySQL and a FLOAT64 in BigQuery. ``ast_utils.faithful_sql`` prints
        the body, parses it back and compares the trees, so a writer that changes the meaning (MySQL's spells
        ``FULL JOIN`` as a union and ``a DIV b`` as a rounding cast) is passed over for one that does not. The compiler's
        own tree can differ in form from anything a parser builds (an alias kept on a subquery node), so when it has no
        faithful spelling the tree BigQuery reads from ``text`` is tried. Either way the result counts only when the
        dialect reads it back to a tree whose BigQuery spelling is ``text`` again: the same body by the name the
        compiler already goes by. A body with no such spelling is matched by its name alone and never re-proved.
        """

        if self.dialect == "bigquery":
            return text
        for reparsed in (False, True):
            try:
                tree = sqlglot.parse_one(text, read="bigquery") if reparsed else canonical
                own = faithful_sql(tree, self.dialect)
                if sqlglot.parse_one(own, read=self.dialect).sql(dialect="bigquery", normalize_functions="upper") == text:
                    return own
            except (UnmodeledConstruct, sqlglot.errors.SqlglotError, RecursionError):
                continue
        return None

    def _expand_ctes(self, body, ctes):
        local = dict(ctes)
        with_clause = _with_clause(body)
        if with_clause is not None:
            for cte in with_clause.expressions:
                local.pop(cte.alias_or_name.lower(), None)
        for table in list(body.find_all(exp.Table)):
            name = table.name.lower()
            if table.db or table.catalog or name not in local:
                continue
            inner_body, inner_env = local[name]
            self._expand()
            expanded = self._expand_ctes(inner_body.copy(), inner_env)
            table.replace(exp.Subquery(this=expanded, alias=exp.TableAlias(this=exp.to_identifier(table.alias_or_name))))
        return body

    # ---- expressions ---------------------------------------------------

    def _column(self, col: exp.Column, env: dict[str, _Source], aliases) -> _Val | exp.Expression:
        if isinstance(col.this, exp.Star):
            raise Unsupported("star in expression")
        if col.args.get("db") or col.args.get("catalog"):
            raise Unsupported(f"column path {col.sql(dialect='bigquery')}")
        name = col.name.lower()
        table = col.table.lower()
        if table:
            scope = env
            while scope is not None and table not in scope:
                scope = getattr(scope, "outer", None)
            if scope is None:
                raise Unsupported(f"unknown alias {table}")
            if scope[table].value_table:
                raise Unsupported(f"field {table}.{name} of an UNNEST element")
            val = scope[table].lookup(name)
            if val is None:
                raise Unsupported(f"{table}.{name} is not a column of {table}")
            return val
        definite = [s for s in env.values() if s.may_have(name) is True]
        maybe = [s for s in env.values() if s.may_have(name) is None]
        outer = getattr(env, "outer", None)
        if not definite and not maybe and outer is not None:
            return self._column(col, outer, aliases)
        if aliases is not None and name in aliases:
            alias_expr = aliases[name]
            same_column = isinstance(alias_expr, exp.Column) and alias_expr.name.lower() == name
            if not same_column:
                if definite or maybe:
                    raise Unsupported(f"{name} is both a SELECT alias and a possible column")
                self._judge_column(col, proof_columns.Claim(alias=True))
                return alias_expr
        if len(definite) == 1 and not maybe:
            self._judge_column(col, self._chosen(definite[0]))
            return definite[0].lookup(name)
        if not definite and len(maybe) == 1 and len(env) == 1:
            self._judge_column(col, self._chosen(maybe[0]))
            return maybe[0].lookup(name)
        raise Unsupported(f"cannot resolve column {name} without a schema")

    def _pred(self, e, env, agg, aliases) -> _Pred:
        if self.dialect == "bigquery":
            self._plan_literals(e, env, agg, aliases)
        if isinstance(e, exp.Paren):
            return self._pred(e.this, env, agg, aliases)
        if isinstance(e, exp.And):
            a, b = self._pred(e.this, env, agg, aliases), self._pred(e.expression, env, agg, aliases)
            return _Pred(z3.And(a.t, b.t), z3.Or(a.f, b.f))
        if isinstance(e, exp.Or):
            a, b = self._pred(e.this, env, agg, aliases), self._pred(e.expression, env, agg, aliases)
            return _Pred(z3.Or(a.t, b.t), z3.And(a.f, b.f))
        if isinstance(e, exp.Not):
            a = self._pred(e.this, env, agg, aliases)
            return _Pred(a.f, a.t)
        if isinstance(e, exp.Boolean):
            return _const_pred(bool(e.this))
        if isinstance(e, exp.Null):
            return _Pred(z3.BoolVal(False), z3.BoolVal(False))
        comparisons = {exp.EQ: "=", exp.NEQ: "<>", exp.LT: "<", exp.LTE: "<=", exp.GT: ">", exp.GTE: ">="}
        for cls, op in comparisons.items():
            if type(e) is cls:
                if self.dialect == "bigquery":
                    kinds = {self._class_of(e.this, env, agg, aliases), self._class_of(e.expression, env, agg, aliases)}
                    if "BOOL" in kinds and kinds & {smt_values.INT64, smt_values.NUMERIC, smt_values.BIGNUMERIC, smt_values.FLOAT64}:
                        raise Unsupported("a number compared with a BOOL (a type error in BigQuery)")
                if string_number_compare.mismatched(e, self.types):
                    return self._converted_compare(op, self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases))
                return self._compare(op, *self._compared_vals([e.this, e.expression], env, agg, aliases))
        if isinstance(e, (exp.NullSafeEQ, exp.NullSafeNEQ)):
            eq = _null_eq(*self._compared_vals([e.this, e.expression], env, agg, aliases))
            eq = eq if isinstance(e, exp.NullSafeEQ) else z3.Not(eq)
            return _Pred(eq, z3.Not(eq))
        if isinstance(e, exp.Is):
            target = e.expression
            if isinstance(target, exp.Null):
                v = self._val(e.this, env, agg, aliases)
                return _Pred(v.null, z3.Not(v.null))
            if isinstance(target, exp.Boolean):
                p = self._pred(e.this, env, agg, aliases)
                hit = p.t if target.this else p.f
                return _Pred(hit, z3.Not(hit))
            raise Unsupported(f"IS {target.sql(dialect='bigquery')}")
        if isinstance(e, exp.In):
            if e.args.get("query") is not None and e.args.get("unnest") is None and e.args.get("field") is None:
                return self._in_subquery(e, env, agg, aliases)
            if e.args.get("query") is not None or e.args.get("unnest") is not None or e.args.get("field") is not None:
                raise Unsupported("IN subquery/UNNEST")
            left, *items = self._compared_vals([e.this, *e.expressions], env, agg, aliases)
            result = _const_pred(False)
            for item in items:
                c = self._compare("=", left, item)
                result = _Pred(z3.Or(result.t, c.t), z3.And(result.f, c.f))
            return result
        if isinstance(e, exp.Between):
            v, low, high = self._compared_vals([e.this, e.args["low"], e.args["high"]], env, agg, aliases)
            lo = self._compare(">=", v, low)
            hi = self._compare("<=", v, high)
            return _Pred(z3.And(lo.t, hi.t), z3.Or(lo.f, hi.f))
        if isinstance(e, exp.Exists):
            atom = self._existence(e.this, env)
            return _Pred(atom, z3.Not(atom))
        if isinstance(e, (exp.Subquery, exp.Select)):
            raise Unsupported("predicate subqueries")
        v = self._val(e, env, agg, aliases)
        V = _value_sort()
        # Only BOOL values are TRUE or FALSE; other types cannot reach here in
        # a valid query, so they are modeled as neither.
        known = z3.And(z3.Not(v.null), V.is_Bool(v.val))
        return _Pred(z3.And(known, V.bool(v.val)), z3.And(known, z3.Not(V.bool(v.val))))

    def _existence(self, query, env, match=None) -> object:
        """A Boolean that is TRUE iff the subquery returns a row satisfying ``match``.

        ``match`` maps the subquery's scope to an extra condition (used by IN).
        Correlated column references resolve through ``env``.
        """

        node = merge_wrapper_tails(query)
        if node is None:
            raise Unsupported("LIMIT inside a predicate subquery")
        if not isinstance(node, exp.Select):
            raise Unsupported(f"{type(node).__name__} inside a predicate subquery")
        for key in ("group", "having", "qualify", "windows", "limit", "offset", "laterals", "pivots", "with_", "with"):
            if node.args.get(key):
                raise Unsupported(f"{key.upper()} inside a predicate subquery")
        if node.args.get("kind"):
            raise Unsupported("subquery shape")
        if any(c.find_ancestor(exp.Select) is node for c in node.find_all(exp.AggFunc)):
            # a global aggregate returns one row even over no input, so it is not an existence test on its rows
            raise Unsupported("aggregate inside a predicate subquery")
        outer_collector, self.collector = self.collector, []
        sites_start = len(self.sites)
        try:
            states = self._scan(node, self.ctes, env)
            if len(states) != 1:
                raise Unsupported("outer join inside a subquery")
            occs, cond, inner = states[0].occs, states[0].cond(), states[0].env
            self.collector.extend(states[0].subs)
            guard = cond.t if match is None else z3.And(cond.t, match(inner, node))
            self._assign_scope(sites_start, states)
            nested = self.collector
        finally:
            self.collector = outer_collector
        atom = z3.Bool(f"{self.fresh('ex')}#exists")
        self.collector.append(_Sub(atom, occs, guard, nested))
        return atom

    def _in_subquery(self, e, env, agg, aliases) -> _Pred:
        """``x IN (SELECT y ...)`` as two existence tests.

        TRUE iff some y equals x; FALSE iff every y compares FALSE with x (so an
        empty subquery gives FALSE and a NULL on either side gives UNKNOWN).
        """

        query = e.args["query"]
        inner_node = merge_wrapper_tails(query)
        if inner_node is None:
            raise Unsupported("LIMIT inside an IN subquery")
        lefts = list(e.this.expressions) if isinstance(e.this, exp.Tuple) else [e.this]
        if not isinstance(inner_node, exp.Select) or len(inner_node.expressions) != len(lefts):
            raise Unsupported("IN subquery shape")
        if any(isinstance(i, exp.Star) for i in inner_node.expressions) or any(
            call.find_ancestor(exp.Select) is inner_node for call in inner_node.find_all(exp.AggFunc)
        ):
            raise Unsupported("IN subquery shape")
        left_values = [self._val(item, env, agg, aliases) for item in lefts]

        def compare(scope, node):
            tests = []
            for left_expr, left, item in zip(lefts, left_values, node.expressions):
                item = item.this if isinstance(item, exp.Alias) else item
                left, right = self._compared([left_expr, item], [left, self._val(item, scope, None, None)])
                tests.append(self._compare("=", left, right))
            # A row value equals another when every pair is equal; it differs when some pair differs.
            return _Pred(z3.And(*[t.t for t in tests]), z3.Or(*[t.f for t in tests]))

        true_atom = self._existence(query, env, lambda scope, node: compare(scope, node).t)
        unknown_or_true = self._existence(query, env, lambda scope, node: z3.Not(compare(scope, node).f))
        return _Pred(true_atom, z3.Not(unknown_or_true))

    def _converted_compare(self, op: str, a: _Val, b: _Val) -> _Pred:
        """A string compared with a number: an engine converts one side, so the result is an unknown function of the two values.

        ``'2' = 2`` is true on MySQL, DuckDB and PostgreSQL and a type error on BigQuery; calling the kinds unequal
        is wrong. ``converted_eq`` and ``converted_lt`` are uninterpreted, so only a pair that compares the same
        values the same way on both sides is proven through them.
        """

        self.uses_uf = True  # a model of the unknown conversion is not a counterexample
        eq, lt = _converted_functions()
        if op == "=":
            r = eq(a.val, b.val)
        elif op == "<>":
            r = z3.Not(eq(a.val, b.val))
        elif op == "<":
            r = lt(a.val, b.val)
        elif op == ">":
            r = lt(b.val, a.val)
        elif op == "<=":
            r = z3.Not(lt(b.val, a.val))
        else:
            r = z3.Not(lt(a.val, b.val))
        known = z3.And(z3.Not(a.null), z3.Not(b.null))
        return _Pred(z3.And(known, r), z3.And(known, z3.Not(r)))

    def _compare(self, op: str, a: _Val, b: _Val) -> _Pred:
        if op not in ("=", "<>"):
            self.ordered = True
            # Equal ranks mean equal strings (the ranking is one-to-one); stated for the compared pair.
            self.facts.append(_rank_injective(a.val, b.val))
        if op == "=":
            r = a.val == b.val
        elif op == "<>":
            r = a.val != b.val
        elif op == "<":
            r = _lt(a.val, b.val)
        elif op == ">":
            r = _lt(b.val, a.val)
        elif op == "<=":
            r = z3.Or(_lt(a.val, b.val), a.val == b.val)
        else:
            r = z3.Or(_lt(b.val, a.val), a.val == b.val)
        known = z3.And(z3.Not(a.null), z3.Not(b.null))
        return _Pred(z3.And(known, r), z3.And(known, z3.Not(r)))

    def _val(self, e, env, agg: _AggCtx | None, aliases) -> _Val:
        V = _value_sort()
        if self.dialect == "bigquery":
            self._plan_literals(e, env, agg, aliases)
        if isinstance(e, exp.Paren):
            return self._val(e.this, env, agg, aliases)
        if isinstance(e, exp.Column):
            resolved = self._column(e, env, aliases)
            if isinstance(resolved, exp.Expression):
                return self._val(resolved, env, agg, None)
            return resolved
        if isinstance(e, exp.Literal):
            return self._literal(e, negate=False)
        if isinstance(e, exp.Exists):
            # EXISTS is never UNKNOWN: a Boolean value built on the existence atom
            atom = self._existence(e.this, env)
            return _Val(z3.BoolVal(False), V.Bool(atom))
        if isinstance(e, exp.Array) and not any(isinstance(n, exp.Column) for n in e.walk()):
            return _Val(z3.BoolVal(False), V.Str(z3.StringVal("\x00array:" + e.sql(dialect="bigquery"))))
        if isinstance(e, exp.Null):
            return _Val(z3.BoolVal(True), V.Num(0))
        if isinstance(e, exp.Boolean):
            return _Val(z3.BoolVal(False), V.Bool(z3.BoolVal(bool(e.this))))
        if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal) and not e.this.is_string:
            return self._literal(e.this, negate=True)
        if isinstance(e, (exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE,
                          exp.Is, exp.In, exp.Between, exp.NullSafeEQ, exp.NullSafeNEQ)):
            return _box(self._pred(e, env, agg, aliases))
        agg_cls = next((cls for cls in _SUPPORTED_AGGREGATES if type(e) is cls), None)
        if agg_cls is not None:
            return self._aggregate(_SUPPORTED_AGGREGATES[agg_cls], e, env, agg)
        if isinstance(e, exp.AggFunc):
            raise Unsupported(f"aggregate {e.sql(dialect='bigquery')}")
        if isinstance(e, exp.Coalesce):
            args = []
            for a in [e.this, *e.expressions]:
                # An argument is evaluated only when every earlier one is NULL (BigQuery stops at the first value).
                with self._guarded(*[prev.null for prev in args]):
                    args.append(self._val(a, env, agg, aliases))
            args = self._common_type([e.this, *e.expressions], args)
            result = args[-1]
            for a in reversed(args[:-1]):
                result = _Val(z3.And(a.null, result.null), z3.If(a.null, result.val, a.val))
            return result
        if isinstance(e, exp.If):
            cond = self._pred(e.this, env, agg, aliases)
            with self._guarded(cond.t):
                then = self._val(e.args["true"], env, agg, aliases)
            other = e.args.get("false")
            if other is not None:
                with self._guarded(z3.Not(cond.t)):
                    other_val = self._val(other, env, agg, aliases)
                then, other = self._common_type([e.args["true"], other], [then, other_val])
            else:
                other = _Val(z3.BoolVal(True), V.Num(0))
            return _Val(z3.If(cond.t, then.null, other.null), z3.If(cond.t, then.val, other.val))
        if isinstance(e, exp.Case):
            operand = e.args.get("this")
            default = e.args.get("default")
            ifs = e.args.get("ifs") or []
            operand_val, whens = None, []
            if operand is not None:
                operand_val, *whens = self._compared_vals([operand, *[b.this for b in ifs]], env, agg, aliases)
            branches = [b.args["true"] for b in ifs] + ([default] if default is not None else [])
            # WHEN conditions run in order, and only the chosen THEN (or ELSE) is evaluated.
            conds = []
            for k, branch in enumerate(ifs):
                with self._guarded(*[z3.Not(c.t) for c in conds]):
                    if operand_val is not None:
                        conds.append(self._compare("=", operand_val, whens[k]))
                    else:
                        conds.append(self._pred(branch.this, env, agg, aliases))
            raw = []
            for k, b in enumerate(branches):
                taken = [z3.Not(c.t) for c in conds[:k]] + ([conds[k].t] if k < len(conds) else [])
                with self._guarded(*taken):
                    raw.append(self._val(b, env, agg, aliases))
            values = self._common_type(branches, raw)
            result = values.pop() if default is not None else _Val(z3.BoolVal(True), V.Num(0))
            for cond, then in reversed(list(zip(conds, values))):
                result = _Val(z3.If(cond.t, then.null, result.null), z3.If(cond.t, then.val, result.val))
            return result
        if isinstance(e, exp.Nullif):
            a, b = self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases)
            eq = self._compare("=", *self._compared([e.this, e.expression], [a, b]))
            if self.dialect in _NULLIF_SUPERTYPE:
                a = self._common_type([e.this, e.expression], [a, b])[0]
            elif self.dialect not in _NULLIF_FIRST_TYPE and _float64_kind(e.expression, self.dialect):
                raise Unsupported(f"NULLIF result type in {self.dialect}")
            return _Val(z3.Or(a.null, eq.t), a.val)
        if self.dialect == "bigquery" and isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            typed = smt_values.fold_typed(e)
            if typed is not None:
                if typed[0] == "int":
                    self._note_integer(typed[1])
                return _Val(z3.BoolVal(False), V.Num(z3.RealVal(f"{typed[1].numerator}/{typed[1].denominator}")))
        if self.dialect == "bigquery" and (scaled := smt_numeric.compile_node(self, e, env, agg, aliases)) is not None:
            return _Val(*scaled)  # NUMERIC and BIGNUMERIC values of known scale: the rounded result (smt_numeric)
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul)) and not self.exact:
            folded = _literal_arithmetic(e, self.dialect)
            if folded is not None:
                return _Val(z3.BoolVal(False), V.Num(z3.RealVal(f"{folded.numerator}/{folded.denominator}")))
            # x * 1 is x for every numeric type (arithmetic operands are numbers, as in exact mode). Not
            # x * 1e0, nor BigQuery's x * 1.0: a FLOAT64 factor converts an INT64 x, rounding it past 2**53.
            for side, other in ((e.this, e.expression), (e.expression, e.this)):
                if isinstance(e, exp.Mul) and _literal_arithmetic(side, self.dialect) == 1 and _exact_literals(side, self.dialect):
                    value = self._val(other, env, agg, aliases)
                    self._numeric(value)
                    return value
        if isinstance(e, exp.Round) and isinstance(e.args.get("decimals"), exp.Literal) and e.args["decimals"].this == "0" and not e.args.get("truncate"):
            # ROUND(x, 0) is ROUND(x).
            return self._val(exp.Round(this=e.this), env, agg, aliases)
        if (
            isinstance(e, (exp.Add, exp.Sub, exp.Mul))
            and self.exact
            and not any(isinstance(side, exp.Interval) for side in (e.this, e.expression))
        ):
            a, b = self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases)
            self._numeric(a)
            self._numeric(b)
            self._arithmetic_site(e, a, b, env, agg, aliases)
            x, y = V.num(a.val), V.num(b.val)
            r = x + y if isinstance(e, exp.Add) else x - y if isinstance(e, exp.Sub) else x * y
            return _Val(z3.Or(a.null, b.null), V.Num(r))
        if isinstance(e, exp.Neg) and self.exact:
            a = self._val(e.this, env, agg, aliases)
            self._numeric(a)
            self._negation_site(e, a, e.this, env, agg, aliases)
            return _Val(a.null, V.Num(-V.num(a.val)))
        if isinstance(e, (exp.Add, exp.Mul)):
            a, b = self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases)
            self._arithmetic_site(e, a, b, env, agg, aliases)
            # Commutative: apply the function to the arguments in a canonical order.
            self.ordered = True
            self.facts.append(_rank_injective(_canon(a), _canon(b)))
            swap = _pair_lt(b, a)
            first = _Val(z3.If(swap, b.null, a.null), z3.If(swap, b.val, a.val))
            second = _Val(z3.If(swap, a.null, b.null), z3.If(swap, a.val, b.val))
            null_fn, val_fn = self._function(type(e).__name__, 2)
            args = self._uf_args([first, second])
            return _Val(z3.Or(a.null, b.null), val_fn(*args))
        if isinstance(e, (exp.Sub, exp.Neg, exp.Div, exp.Mod, exp.DPipe)):
            parts = [e.this] + ([e.expression] if isinstance(e, exp.Binary) else [])
            if isinstance(e, exp.Div) and isinstance(e.expression, exp.Nullif) and e.expression.expression.sql() == "0":
                # x / NULLIF(y, 0) is the quotient with the zero divisor read as NULL (the mean of no values).
                parts = [e.this, e.expression.this]
            vals = [self._val(p, env, agg, aliases) for p in parts]
            if isinstance(e, exp.Sub):
                self._arithmetic_site(e, vals[0], vals[1], env, agg, aliases)
            elif isinstance(e, exp.Neg):
                self._negation_site(e, vals[0], e.this, env, agg, aliases)
            elif isinstance(e, (exp.Div, exp.Mod)):
                divisor = vals[1] if parts == [e.this, e.expression] else self._val(e.expression, env, agg, aliases)
                self._division_site(e, vals[0], divisor, False)
            null_fn, val_fn = self._function(type(e).__name__, len(vals))
            args = self._uf_args(vals)
            nulls = z3.Or(*[v.null for v in vals])
            if isinstance(e, exp.Div):
                nulls = z3.Or(nulls, null_fn(*args))  # SAFE_DIVIDE-like shapes stay possible
            return _Val(nulls, val_fn(*args))
        if isinstance(e, exp.Abs) and self.exact:
            a = self._val(e.this, env, agg, aliases)
            self._numeric(a)
            self._negation_site(e, a, e.this, env, agg, aliases)
            x = V.num(a.val)
            return _Val(a.null, V.Num(z3.If(x < 0, -x, x)))
        if isinstance(e, exp.Abs) and isinstance(e.this.unnest() if isinstance(e.this, exp.Paren) else e.this, exp.Sub):
            return self._abs_difference(e, env, agg, aliases)
        if isinstance(e, exp.SafeDivide):
            # SAFE_DIVIDE is the quotient, and NULL where it would fail (a zero divisor).
            a, b = self._val(e.this, env, agg, aliases), self._val(e.expression, env, agg, aliases)
            null_fn, val_fn = self._function("Div", 2)
            args = self._uf_args([a, b])
            zero = z3.And(V.is_Num(b.val), V.num(b.val) == 0)
            return _Val(z3.Or(a.null, b.null, zero, null_fn(*args)), val_fn(*args))
        if isinstance(e, exp.Cast) and not isinstance(e, exp.TryCast):
            to = e.args["to"]
            inner = e.this.unnest() if isinstance(e.this, exp.Paren) else e.this
            if type(inner) is exp.Cast and inner.args["to"] == to:
                # A cast to the type a value already has is the value: CAST(CAST(x AS T) AS T) is CAST(x AS T).
                return self._val(inner, env, agg, aliases)
            if isinstance(inner, exp.IntDiv) and to.this in _INTEGER_TYPES:
                # Integer division already gives an integer (overflow is not modeled).
                return self._val(inner, env, agg, aliases)
            if (
                isinstance(e.this, exp.Literal)
                and e.this.is_string
                and to.is_type(exp.DataType.Type.DATE)
            ):
                return self._literal(e.this, negate=False)
            v = self._val(e.this, env, agg, aliases)
            source = self._class_of(e.this, env, agg, aliases)
            if source == smt_values.FLOAT64 and smt_values.type_class(to.sql(dialect="bigquery")) == smt_values.STRING and not _nonzero_literal(e.this):
                raise Unsupported("CAST of a FLOAT64 to STRING (the sign of a zero is not modeled)")
            failing = self._cast_sites(e, to, source, v)
            if failing is not None:
                self._site("failed cast", e, z3.And(z3.Not(v.null), failing[0]), under=failing[1])
            _, val_fn = self._function(f"CAST:{to.sql(dialect='bigquery')}", 1)
            return _Val(v.null, val_fn(*self._uf_args([v])))
        if isinstance(e, exp.TryCast):
            # SAFE_CAST is the cast, and NULL where the cast fails.
            to = e.args["to"]
            v = self._val(e.this, env, agg, aliases)
            failing = self._cast_sites(e, to, self._class_of(e.this, env, agg, aliases), v)
            _, val_fn = self._function(f"CAST:{to.sql(dialect='bigquery')}", 1)
            return _Val(v.null if failing is None else z3.Or(v.null, failing[0]), val_fn(*self._uf_args([v])))
        if isinstance(e, (exp.Func, exp.Binary, exp.Unary)):
            return self._generic(e, env, agg, aliases)
        if isinstance(e, exp.Interval) and all(isinstance(n, (exp.Interval, exp.Literal, exp.Var)) for n in e.walk()):
            # A constant interval is one fixed value, named by its text; date arithmetic over it stays
            # uninterpreted, and two spellings of one interval may read as different values, so no
            # counterexample is reported from it.
            self.uses_uf = True
            return _Val(z3.BoolVal(False), z3.Const(f"interval:{e.sql(dialect='bigquery')}", _value_sort()))
        raise Unsupported(f"expression {type(e).__name__}: {e.sql(dialect='bigquery')}")

    def _abs_difference(self, e, env, agg, aliases) -> _Val:
        """``ABS(x - y)`` with ``-`` and ``ABS`` left uninterpreted, plus what holds for every
        number type (IEEE subtraction rounds ``x - y`` to exactly ``-(y - x)``, and is zero only
        when ``x = y``): it is ``ABS(y - x)``, it is ``x - y`` when ``y < x`` and ``y - x`` when
        ``x < y``, it is 0 when ``x = y`` and positive otherwise. ``x`` and ``y`` are taken to be
        numbers (MySQL also subtracts strings, which sort differently), which the proof's
        assumptions record."""

        V = _value_sort()
        diff = e.this.unnest() if isinstance(e.this, exp.Paren) else e.this
        a, b = self._val(diff.this, env, agg, aliases), self._val(diff.expression, env, agg, aliases)
        self._numeric(a)
        self._numeric(b)
        self.numeric_differences = True
        swapped = exp.Sub(this=diff.expression.copy(), expression=diff.this.copy())
        result = self._generic(e, env, agg, aliases)
        mirror = self._generic(exp.Abs(this=swapped), env, agg, aliases)
        forward, backward = self._val(diff, env, agg, aliases), self._val(swapped, env, agg, aliases)
        value = result.val
        self.facts.append(
            z3.Implies(
                z3.And(z3.Not(a.null), z3.Not(b.null)),
                z3.And(
                    z3.Not(result.null),
                    z3.Not(mirror.null),
                    value == mirror.val,
                    z3.Implies(_lt(b.val, a.val), value == forward.val),
                    z3.Implies(_lt(a.val, b.val), value == backward.val),
                    z3.Implies(a.val == b.val, value == V.Num(0)),
                    z3.Implies(a.val != b.val, z3.And(V.is_Num(value), V.num(value) > 0)),
                ),
            )
        )
        return result

    def _declared_float(self, v: _Val) -> bool | None:
        """Whether a base-table column value is declared FLOAT64 (None when it is not a declared column)."""

        if not (z3.is_const(v.val) and v.val.decl().kind() == z3.Z3_OP_UNINTERPRETED):
            return None
        uid, _, name = str(v.val).partition(".")
        declared = self.types.get(self.occ_tables.get(uid, ""), {}).get(name)
        return _type_is_float(declared) if declared else None

    def _type_key(self, v: _Val):
        """Values with equal keys have the same type: one table column, or one term."""

        if z3.is_const(v.val) and v.val.decl().kind() == z3.Z3_OP_UNINTERPRETED:
            uid, _, name = str(v.val).partition(".")
            if uid in self.occ_tables:
                return (self.occ_tables[uid], name)
        return v.val.get_id()

    def _coerce(self, entries: list) -> list[_Val]:
        """Each ``(visible float kind, is literal, value)`` converted to the common type of all of them.

        When one is FLOAT64 (visibly, or by its declared column type) the result is FLOAT64, and a
        value that may be INT64 or NUMERIC is converted, rounding past 2**53: it reads as
        CAST(value AS FLOAT64), an uninterpreted function that is the identity on FLOAT64 values. A
        literal is its own value in either type. Otherwise the values are kept; when two of them come
        from different sources whose types are not known, that is MIXED_NUMERIC_ASSUMPTION."""

        kinds = [self._declared_float(v) if kind is None else kind for kind, _, v in entries]
        if not any(kinds):
            unknown = [self._type_key(v) for k, (_, literal, v) in zip(kinds, entries) if not literal and k is None]
            known = [self._type_key(v) for k, (_, literal, v) in zip(kinds, entries) if not literal and k is False]
            if unknown and len(set(unknown + known)) > 1:
                self.mixed_numeric = True
            return [v for _, _, v in entries]
        result = []
        for kind, (_, literal, v) in zip(kinds, entries):
            if not kind and not literal:
                _, val_fn = self._function("CAST:FLOAT64", 1)
                v = _Val(v.null, val_fn(*self._uf_args([v])))
            result.append(v)
        return result

    def _common_type(self, exprs: list, vals: list[_Val]) -> list[_Val]:
        """``vals`` of the branches ``exprs`` of a CASE, COALESCE, IF or NULLIF, in their common type."""

        # A value that is always NULL (a column of the unmatched side of an outer join) converts like a literal.
        return self._coerce([
            (_float64_kind(e, self.dialect), _plain_literal(e, self.dialect) or z3.is_true(v.null), v) for e, v in zip(exprs, vals)
        ])

    def _compared(self, exprs: list, vals: list[_Val]) -> list[_Val]:
        """``vals`` of the operands ``exprs`` of one comparison (``=``, ``<``, ``IN``, ``BETWEEN``, a simple
        CASE) in the type they are compared in, as ``_common_type`` does for branches: an INT64 operand
        compared with a FLOAT64 one is converted first, so ``a = b AND b = c`` over INT64 ``a``, ``c`` and
        FLOAT64 ``b`` does not give ``a = c``. A FLOAT64 literal below 2**53 in magnitude converts nothing:
        an integer and its FLOAT64 rounding compare alike with it."""

        entries = []
        for position, (e, v) in enumerate(zip(exprs, vals)):
            kind, literal = _float64_kind(e, self.dialect), _plain_literal(e, self.dialect) or z3.is_true(v.null)
            if literal and kind and abs(_literal_value(e, self.dialect)) < 2**53 and self._literal_compares_exactly(e, position, exprs, vals):
                kind = False
            entries.append((kind, literal, v))
        return self._coerce(entries)

    def _literal_compares_exactly(self, e, position: int, exprs: list, vals: list[_Val]) -> bool:
        """Whether the FLOAT64 value ``e`` (a literal, or arithmetic over literals) compares with the other operands
        as the exact number the model holds for it. A literal written in the comparison does: BigQuery converts the
        literal to the type of a NUMERIC or BIGNUMERIC operand, and an INT64 below 2**53 is its own double. A FLOAT64
        that is not a literal there (``0.1 + 0.2``, a literal a subquery returns) makes BigQuery convert the other
        operand instead, and a NUMERIC or BIGNUMERIC value changes when it becomes a double."""

        if self.dialect != "bigquery":
            return True
        leaf = smt_literal_coercion.literal_leaf(e)
        if leaf is not None and self._typed_literals.get(id(leaf[0])) is leaf[0]:
            return True
        for k, (other, v) in enumerate(zip(exprs, vals)):
            if k == position or _plain_literal(other, self.dialect) or z3.is_true(v.null):
                continue
            if self._declared_class(v) not in (smt_values.INT64, smt_values.FLOAT64) and _float64_kind(other, self.dialect) is not True:
                return False
        return True

    def _compared_vals(self, exprs: list, env, agg, aliases) -> list[_Val]:
        return self._compared(exprs, [self._val(e, env, agg, aliases) for e in exprs])

    def _numeric(self, v: _Val) -> None:
        self.facts.append(z3.Implies(z3.Not(v.null), _value_sort().is_Num(v.val)))

    # ---- types and runtime errors (BigQuery) -----------------------------------------------

    def _declared_class(self, v: _Val) -> str | None:
        """INT64, NUMERIC, BIGNUMERIC, FLOAT64 or OTHER for a base-table column value with a declared type."""

        if not (z3.is_const(v.val) and v.val.decl().kind() == z3.Z3_OP_UNINTERPRETED):
            return None
        uid, _, name = str(v.val).partition(".")
        declared = self.types.get(self.occ_tables.get(uid, ""), {}).get(name)
        return smt_values.type_class(declared) if declared else None

    def _class_of(self, e, env, agg, aliases) -> str | None:
        """The type of expression ``e`` where it is visible (a declared column, a literal, a cast, arithmetic over
        those), ``None`` when it is not."""

        S = smt_values
        if isinstance(e, exp.Paren):
            return self._class_of(e.this, env, agg, aliases)
        if isinstance(e, exp.Literal):
            return "STRING" if e.is_string else S.INT64 if S.is_integer_text(e.this) else S.FLOAT64
        if isinstance(e, exp.Null):
            return "NULL"
        if isinstance(e, exp.Boolean):
            return "BOOL"
        if isinstance(e, exp.Column):
            try:
                return self._declared_class(self._val(e, env, agg, aliases))
            except Unsupported:
                return None
        if isinstance(e, exp.Neg):
            return self._class_of(e.this, env, agg, aliases)
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Mod)):
            return S.arithmetic_class(*self._operand_classes([e.this, e.expression], env, agg, aliases))
        if isinstance(e, exp.Div):
            return S.division_class(*self._operand_classes([e.this, e.expression], env, agg, aliases))
        if isinstance(e, (exp.IntDiv, exp.Count, exp.CountIf)):
            return S.INT64
        if isinstance(e, (exp.Abs, exp.Sum, exp.Min, exp.Max)) and isinstance(e.this, exp.Expression):
            inner = e.this.expressions[0] if isinstance(e.this, exp.Distinct) and e.this.expressions else e.this
            return self._class_of(inner, env, agg, aliases)
        if isinstance(e, exp.Avg) and isinstance(e.this, exp.Expression):
            inner = self._class_of(e.this, env, agg, aliases)
            return S.FLOAT64 if inner in (S.INT64, S.FLOAT64) else inner
        if isinstance(e, (exp.Cast, exp.TryCast)):
            return S.type_class(e.args["to"].sql(dialect="bigquery")) or None
        if isinstance(e, (exp.Coalesce, exp.If, exp.Case, exp.Nullif)):
            return S.supertype(self._operand_classes(_branch_values(e, self.dialect), env, agg, aliases))
        return None

    def _operand_classes(self, operands: list, env, agg, aliases) -> list:
        """``_class_of`` each operand of one operator, with a decimal literal typed as BigQuery types it there: NUMERIC
        or BIGNUMERIC when that is the common type of the other operands, FLOAT64 otherwise (``smt_literal_coercion``)."""

        S = smt_values
        literal = [smt_literal_coercion.literal_leaf(o) is not None for o in operands]
        classes = [None if lit else self._class_of(o, env, agg, aliases) for o, lit in zip(operands, literal)]
        if not any(literal):
            return classes
        others = [c for c, lit in zip(classes, literal) if not lit and c != "NULL"]
        if not others:
            mine = S.FLOAT64
        elif any(c is None for c in others):
            mine = None
        else:
            common = others[0] if len(set(others)) == 1 else S.supertype(others)
            mine = common if common in (S.NUMERIC, S.BIGNUMERIC) else S.FLOAT64
        return [mine if lit else c for c, lit in zip(classes, literal)]

    def _site(self, kind: str, e, cond, under=None) -> None:
        """Record an operation that can raise a runtime error where ``cond`` holds, and assume it does not.

        The operation only runs where every enclosing CASE/IF/COALESCE guard holds; ``WHERE`` and ``ON`` do not guard
        it (BigQuery promises no evaluation order). ``under`` is a condition that implies the error for a concrete
        value (used to show a database when ``cond`` mentions an uninterpreted function)."""

        if self.dialect != "bigquery" or self._safe:
            return
        guarded = z3.And(*self._guard, cond) if self._guard else cond
        fire = z3.simplify(guarded)
        if z3.is_false(fire):
            return
        text = e.sql(dialect="bigquery") if isinstance(e, exp.Expression) else str(e)
        if z3.is_true(fire):
            raise Unsupported(f"{kind} on constant operands: {text[:80]}")
        site = smt_errors.Site(kind, text[:120], fire, certain=smt_errors.uninterpreted_free(fire))
        site.under = fire if site.certain else (z3.simplify(z3.And(*self._guard, under)) if under is not None and self._guard else under)
        self.sites.append(site)
        self.facts.append(z3.Not(fire))

    @contextlib.contextmanager
    def _guarded(self, *conditions):
        """Compile an expression that runs only where ``conditions`` hold."""

        self._guard.extend(conditions)
        try:
            yield
        finally:
            if conditions:
                del self._guard[-len(conditions):]

    def _may_fail(self, name: str, vals: list[_Val]):
        """The condition ``fails:name(args)``: an operation that may raise an error for these arguments, as one
        uninterpreted Boolean, equal for equal calls."""

        self.uses_uf = True  # a model of an unknown failing condition is not a counterexample
        V = _value_sort()
        key = ("fails:" + name, len(vals))
        if key not in self.functions:
            self.functions[key] = z3.Function(f"fails:{name}/{len(vals)}", *([z3.BoolSort(), V] * len(vals)), z3.BoolSort()) if vals else z3.Bool(f"fails:{name}")
        function = self.functions[key]
        known = z3.And(*[z3.Not(v.null) for v in vals]) if vals else z3.BoolVal(True)
        return z3.And(known, function(*self._uf_args(vals))) if vals else function

    def _arithmetic_site(self, e, a: _Val, b: _Val, env, agg, aliases) -> None:
        """``a + b``, ``a - b`` and ``a * b`` fail on INT64 overflow (and on FLOAT64 or NUMERIC overflow)."""

        if self.dialect != "bigquery":
            return
        V = _value_sort()
        S = smt_values
        cls = S.arithmetic_class(self._class_of(e.this, env, agg, aliases), self._class_of(e.expression, env, agg, aliases))
        if cls == S.INT64:
            x, y = V.num(a.val), V.num(b.val)
            r = x + y if isinstance(e, exp.Add) else x - y if isinstance(e, exp.Sub) else x * y
            cond = z3.And(z3.Not(a.null), z3.Not(b.null), V.is_Num(a.val), V.is_Num(b.val), z3.Or(r > S.INT64_MAX, r < S.INT64_MIN))
            self._site("INT64 overflow", e, cond)
            return
        kind = "floating point overflow" if cls == S.FLOAT64 else "NUMERIC overflow" if cls in (S.NUMERIC, S.BIGNUMERIC) else "overflow"
        pair = [a, b]
        if isinstance(e, (exp.Add, exp.Mul)):  # commutative: one canonical argument order
            swap = _pair_lt(b, a)
            pair = [_Val(z3.If(swap, b.null, a.null), z3.If(swap, b.val, a.val)), _Val(z3.If(swap, a.null, b.null), z3.If(swap, a.val, b.val))]
        self._site(kind, e, self._may_fail(f"{type(e).__name__}:{cls}", pair))

    def _negation_site(self, e, a: _Val, operand, env, agg, aliases) -> None:
        """``-x`` and ``ABS(x)`` overflow at the smallest INT64."""

        if self.dialect != "bigquery":
            return
        V = _value_sort()
        S = smt_values
        cls = self._class_of(operand, env, agg, aliases)
        if cls == S.INT64:
            cond = z3.And(z3.Not(a.null), V.is_Num(a.val), V.num(a.val) == S.INT64_MIN)
            self._site("INT64 overflow", e, cond)
        elif cls is None:
            self._site("overflow", e, self._may_fail(f"{type(e).__name__}", [a]))

    def _division_site(self, e, a: _Val, divisor: _Val, integer: bool) -> None:
        """``/``, ``DIV`` and ``MOD`` fail on a zero divisor (``DIV`` also on INT64_MIN / -1)."""

        if self.dialect != "bigquery":
            return
        V = _value_sort()
        zero = z3.And(z3.Not(a.null), z3.Not(divisor.null), V.is_Num(divisor.val), V.num(divisor.val) == 0)
        cond = zero
        if integer and isinstance(e, exp.IntDiv):
            cond = z3.Or(zero, z3.And(z3.Not(a.null), z3.Not(divisor.null), V.is_Num(a.val), V.is_Num(divisor.val),
                                      V.num(a.val) == smt_values.INT64_MIN, V.num(divisor.val) == -1))
        self._site("division by zero", e, cond)

    def _function_sites(self, e, name: str, vals: list[_Val], env, agg, aliases) -> None:
        """The failures of a function call: ``DIV`` and ``ABS`` by their rule, any other call that is not known never to
        fail as one unknown failing condition over its arguments."""

        if self.dialect != "bigquery" or name in _NEVER_FAILS or name.startswith("Safe") or name.startswith("SAFE_"):
            return
        if isinstance(e, exp.IntDiv):
            self._division_site(e, vals[0], vals[1], True)
        elif isinstance(e, exp.Abs):
            self._negation_site(e, vals[0], e.this, env, agg, aliases)
        else:
            self._site("function call", e, self._may_fail(name, vals))

    def _cast_sites(self, e, to, source: str | None, v: _Val):
        """The condition under which ``CAST(v AS to)`` fails, or ``None`` when it cannot. A string that is not a
        number (``'x'``) and a FLOAT64 past INT64 are concrete failures."""

        name = re.sub(r"[(<].*", "", to.sql(dialect="bigquery")).strip().upper()
        S = smt_values
        if source is not None and source not in ("NULL",) and S.cast_is_safe(source, name):
            return None
        literal = e.this.unnest() if isinstance(e.this, exp.Paren) else e.this
        if isinstance(literal, exp.Literal) and literal.is_string and S.string_cast_ok(literal.this, name):
            return None
        V = _value_sort()
        under = None
        if source == "STRING" and name in ("INT64", "INT", "BIGINT", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "BOOL", "BOOLEAN", "DATE", "TIMESTAMP"):
            under = z3.And(z3.Not(v.null), V.is_Str(v.val), V.str(v.val) == z3.StringVal("x"))
        elif source == S.FLOAT64 and name in ("INT64", "INT", "BIGINT"):
            under = z3.And(z3.Not(v.null), V.is_Num(v.val), V.num(v.val) >= 2**63)
        return self._may_fail(f"CAST:{name}", [v]), under

    def _literal(self, e: exp.Literal, negate: bool) -> _Val:
        V = _value_sort()
        if e.is_string:
            text = e.this
            self.string_literals.add(text)
            if _CANONICAL_TIMESTAMP.match(text):
                self.timestamp_literals.add(text)  # fixed width, so string order is time order among timestamps
            elif _DATEISH.match(text) and not _CANONICAL_DATE.match(text):
                raise Unsupported(f"date/time-like literal {text!r} (only 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS' are modeled)")
            return _Val(z3.BoolVal(False), V.Str(z3.StringVal(text)))
        planned = self._literal_plans.get(id(e))
        if planned is not None and planned[0] is e:
            self._number(e.this, negate)  # the same range checks as the FLOAT64 reading
            return _Val(z3.BoolVal(False), V.Num(-planned[1] if negate else planned[1]))
        value = self._number(e.this, negate)
        return _Val(z3.BoolVal(False), V.Num(z3.RealVal(f"{value.numerator}/{value.denominator}")))

    # ---- decimal literals next to NUMERIC and BIGNUMERIC operands (BigQuery) -----------------

    def _plan_literals(self, e, env, agg, aliases) -> None:
        """Before the operands of ``e`` are compiled: give each decimal literal among them the value BigQuery gives
        it there (``smt_literal_coercion``). A literal typed with INT64, FLOAT64 or other literals is the nearest
        double; with a NUMERIC or BIGNUMERIC operand it is its decimal text rounded to that type; with an operand of
        undeclared type it is whichever of those the operand's type makes it, one unknown per operand."""

        C = smt_literal_coercion
        if isinstance(e, (exp.Column, exp.Literal, exp.Paren, exp.Neg)):
            return
        typed = C.groups(e)
        if not typed:
            # Not an operator that types its operands together: a literal directly under it is a FLOAT64, whatever an
            # earlier use of the same node object was planned as.
            for child in e.args.values():
                for item in child if isinstance(child, list) else [child]:
                    leaf = C.literal_leaf(item) if isinstance(item, exp.Expression) else None
                    if leaf is not None:
                        self._literal_plans.pop(id(leaf[0]), None)
                        self._typed_literals.pop(id(leaf[0]), None)
            return
        precise = not isinstance(e, exp.Func) or isinstance(e, (exp.Case, exp.If, exp.Coalesce, exp.Nullif, exp.Greatest, exp.Least))
        subquery_side = isinstance(e, exp.In) and any(e.args.get(k) is not None for k in ("query", "unnest", "field"))
        for members in typed:
            leaves = [(member, C.literal_leaf(member)) for member in members]
            if not any(leaf is not None and C.unstable(member) for member, leaf in leaves):
                for _, leaf in leaves:
                    if leaf is not None:
                        self._typed_literals[id(leaf[0])] = leaf[0]
                        self._literal_plans.pop(id(leaf[0]), None)
                continue
            others = [m for m, leaf in leaves if leaf is None and not isinstance(m.unnest() if isinstance(m, exp.Paren) else m, exp.Null)]
            others = [m for m in others if not (isinstance(m, exp.Literal) or (isinstance(m, exp.Neg) and isinstance(m.this, exp.Literal)))]
            if precise:
                kinds = [self._numeric_kind(m, env, agg, aliases) for m in others]
                if subquery_side:
                    kinds.append(self._kind_unknown(("fresh", next(self.counter))))
            elif all(self._numeric_kind(m, env, agg, aliases) in (C.INT64, C.FLOAT64) for m in others):
                kinds = []
            else:
                # Another function: which of its signatures applies is not modelled, so the literal has an unknown
                # reading, the same one in every call written the same way over the same columns.
                kinds = [self._kind_unknown(self._kind_key(e, env, agg, aliases) or ("fresh", next(self.counter)))]
            for member, leaf in leaves:
                if leaf is None:
                    continue
                self._typed_literals[id(leaf[0])] = leaf[0]
                if C.unstable(member):
                    self._literal_plans[id(leaf[0])] = (leaf[0], self._literal_reading(leaf[0], kinds))
                else:
                    self._literal_plans.pop(id(leaf[0]), None)

    def _numeric_kind(self, e, env, agg, aliases):
        """The numeric kind of operand ``e`` (``smt_literal_coercion.INT64`` .. ``FLOAT64``), or a Z3 integer standing
        for it when the type of ``e`` is not declared or not visible."""

        C = smt_literal_coercion
        cls = self._class_of(e, env, agg, aliases)
        if cls in C.KIND:
            return C.KIND[cls]
        if cls is not None:
            return C.FLOAT64  # a string, a BOOL, a date: BigQuery rejects the comparison, any reading will do
        return self._kind_unknown(self._kind_key(e, env, agg, aliases) or ("fresh", next(self.counter)))

    def _kind_unknown(self, key):
        if key not in self.kind_unknowns:
            self.kind_unknowns[key] = z3.Int(f"kind!{len(self.kind_unknowns)}")
        return self.kind_unknowns[key]

    def _kind_key(self, e, env, agg, aliases):
        """A key that two expressions share only when they have the same SQL type: the text of ``e`` with every
        column replaced by the base-table column it reads. ``None`` when a column is not a base-table column (a
        derived value has the type of its own expression) or ``e`` holds a subquery."""

        parts = []
        for node in e.walk():
            if isinstance(node, (exp.Select, exp.Subquery, exp.SetOperation)):
                return None
            if isinstance(node, exp.Column):
                try:
                    v = self._val(node, env, agg, aliases)
                except Unsupported:
                    return None
                key = self._type_key(v)
                if not isinstance(key, tuple):
                    return None
                parts.append(key)
        shape = e.copy()
        for node in list(shape.find_all(exp.Column)):
            if node is shape:
                return ("column", parts[0])
            node.replace(exp.Placeholder())
        return ("expression", shape.sql(dialect="bigquery"), tuple(parts))

    def _literal_reading(self, literal: exp.Literal, kinds: list):
        """The value (a Z3 real) of the unsigned decimal ``literal`` typed with operands of ``kinds``."""

        C = smt_literal_coercion
        try:
            floating, numeric, bignumeric = C.readings(literal.this)
        except smt_values.NotModeled as error:
            raise Unsupported(str(error)) from error

        def real(value):
            if value is None:
                raise Unsupported(f"decimal literal {literal.this[:40]} does not fit the NUMERIC or BIGNUMERIC operand it is compared with")
            return z3.RealVal(f"{value.numerator}/{value.denominator}")

        known = max((k for k in kinds if isinstance(k, int)), default=C.INT64)
        unknown = [k for k in kinds if not isinstance(k, int)]
        if known == C.FLOAT64 or not unknown:
            return real({C.NUMERIC: numeric, C.BIGNUMERIC: bignumeric}.get(known, floating))
        widest = z3.IntVal(known)
        for k in unknown:
            widest = z3.If(k > widest, k, widest)
        return z3.If(widest == C.NUMERIC, real(numeric), z3.If(widest == C.BIGNUMERIC, real(bignumeric), real(floating)))

    def _number(self, text: str, negate: bool) -> Fraction:
        """The value of a numeric literal. In BigQuery an integer is an INT64 and a decimal or exponent literal the
        FLOAT64 nearest its text (``smt_values``); other dialects read decimals exactly."""

        if self.dialect != "bigquery":
            value = _parse_number(text)
            return -value if negate else value
        try:
            value = smt_values.literal_value(text, negate)
        except smt_values.NotModeled as error:
            raise Unsupported(str(error)) from error
        if smt_values.is_integer_text(text):
            self._note_integer(value)
        return value

    def _note_integer(self, value: Fraction) -> None:
        """An INT64 past 2**53 has no exact FLOAT64: it is only modelled in an all-INT64 context (``_integer_context``)."""

        if abs(value) > smt_values.FLOAT_EXACT_INT:
            self.big_literals.append(str(value))

    def typed_facts(self, occs=None) -> list:
        """What the declared type of each base-table column says about its values: INT64 and NUMERIC values are numbers
        in range (INT64: integers; NUMERIC: nine decimal digits), STRING and BOOL values are strings and Booleans.

        ``occs`` limits the facts to those table occurrences: a block's checks must not mention (and so
        give arbitrary values to) the rows of the other query, or a counterexample built from the model
        reads them as free."""

        V = _value_sort()
        S = smt_values
        facts = []
        if self.dialect != "bigquery":
            return facts
        for occ in (self.occs_seen if occs is None else occs):
            declared = self.types.get(self.occ_tables.get(occ.uid, ""), {})
            for name, v in occ.cols.items():
                cls = S.type_class(declared.get(name))
                x = V.num(v.val)
                if cls == S.INT64:
                    typed = z3.And(V.is_Num(v.val), z3.IsInt(x), x >= S.INT64_MIN, x <= S.INT64_MAX)
                elif cls in (S.NUMERIC, S.BIGNUMERIC):
                    typed = smt_numeric.typed_fact(cls, V, v)
                elif cls == S.FLOAT64:
                    typed = V.is_Num(v.val)
                elif cls == S.STRING:
                    typed = V.is_Str(v.val)
                elif cls == S.BOOL:
                    typed = V.is_Bool(v.val)
                else:
                    continue
                facts.append(z3.Implies(z3.Not(v.null), typed))
        return facts

    def integer_context_problem(self) -> str | None:
        """Why an INT64 literal past 2**53 cannot be read as an exact integer: a FLOAT64 value in the queries would
        convert it (rounding it), and a column without a declared integer-compatible type may be one."""

        if self.float_capable:
            return "a FLOAT64 value may meet it"
        if self.untyped_sources:
            return "an UNNEST or derived table has no declared column types"
        for occ in self.occs_seen:
            declared = self.types.get(self.occ_tables.get(occ.uid, ""), {})
            for name in occ.cols:
                if smt_values.type_class(declared.get(name)) not in (smt_values.INT64, smt_values.NUMERIC, smt_values.STRING, smt_values.BOOL, smt_values.OTHER):
                    return f"column {occ.table}.{name} has no declared INT64 type"
        return None

    def order_facts(self) -> list:
        """Facts about the string ordering that the compiled queries may rely on."""

        if not self.ordered or not self.string_literals:
            return []
        rank = _string_rank()
        literals = sorted(self.string_literals)
        facts = [rank(z3.StringVal(a)) < rank(z3.StringVal(b)) for a, b in zip(literals, literals[1:])]
        return facts

    def _function(self, name: str, arity: int):
        self.uses_uf = True
        key = (name, arity)
        if key not in self.functions:
            V = _value_sort()
            domain = [z3.BoolSort(), V] * arity
            self.functions[key] = (
                z3.Function(f"null:{name}/{arity}", *domain, z3.BoolSort()) if arity else z3.Bool(f"null:{name}"),
                z3.Function(f"val:{name}/{arity}", *domain, V) if arity else z3.Const(f"val:{name}", V),
            )
        return self.functions[key]

    @staticmethod
    def _uf_args(vals: list[_Val]) -> list:
        args = []
        for v in vals:
            args.extend([v.null, _canon(v)])
        return args

    def _generic(self, e, env, agg, aliases) -> _Val:
        if type(e).__name__ != "SafeFunc":
            return self._generic_call(e, env, agg, aliases)
        self._safe += 1  # SAFE.function(..) is NULL where the function fails
        try:
            return self._generic_call(e, env, agg, aliases)
        finally:
            self._safe -= 1

    def _generic_call(self, e, env, agg, aliases) -> _Val:
        if isinstance(e, (exp.Lambda, exp.Window)):
            raise Unsupported(type(e).__name__)
        name_parts = [e.name.upper() if isinstance(e, exp.Anonymous) else type(e).__name__]
        vals: list[_Val] = []
        sign_observer = name_parts[0] in _SIGN_OBSERVERS
        # Named arguments in the class's declared order, not the order the node happened to be built in.
        declared = list(type(e).arg_types)
        for key in declared + sorted(k for k in e.args if k not in type(e).arg_types):
            value = e.args.get(key)
            if key.startswith("_") or value is None:
                continue
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, (exp.Var, exp.DataType, exp.Identifier)) or not isinstance(child, exp.Expression):
                    name_parts.append(f"{key}={child.sql(dialect='bigquery') if isinstance(child, exp.Expression) else child}")
                elif isinstance(child, (exp.Subquery, exp.Select, exp.Exists, exp.Lambda, exp.Star)):
                    raise Unsupported(f"{type(child).__name__} inside {type(e).__name__}")
                else:
                    name_parts.append(f"{key}=?")
                    vals.append(self._val(child, env, agg, aliases))
                    if sign_observer and self._class_of(child, env, agg, aliases) not in _NOT_FLOAT:
                        # -0.0 and 0.0 are one value to the prover, but IEEE_DIVIDE(1, -0.0) is -inf.
                        raise Unsupported(f"{name_parts[0]} of a FLOAT64 (the sign of a zero is not modeled)")
        self._function_sites(e, name_parts[0], vals, env, agg, aliases)
        null_fn, val_fn = self._function("|".join(name_parts), len(vals))
        if not vals:
            return _Val(null_fn, val_fn)
        args = self._uf_args(vals)
        if isinstance(e, _STRICT_FUNCTIONS):
            # NULL in, NULL out (and possibly NULL for some non-NULL inputs, such as SQRT(-1) in MySQL).
            return _Val(z3.Or(*[v.null for v in vals], null_fn(*args)), val_fn(*args))
        return _Val(null_fn(*args), val_fn(*args))

    def _aggregate(self, func: str, e, env, agg: _AggCtx | None, from_avg: bool = False) -> _Val:
        if agg is None:
            raise Unsupported(f"aggregate in a non-aggregate position: {e.sql(dialect='bigquery')}")
        if func == "AVG" and e.this is not None and not any(e.args.get(k) for k in ("having_max", "ignore_nulls", "order", "limit", "separator")):
            # AVG(x) is SUM(x) / COUNT(x): NULL when no value is present, and shared with a spelled-out quotient.
            total = self._aggregate("SUM", exp.Sum(this=e.this.copy()), env, agg, from_avg=True)
            count = self._aggregate("COUNT", exp.Count(this=e.this.copy()), env, agg)
            null_fn, val_fn = self._function("Div", 2)
            args = self._uf_args([total, count])
            return _Val(z3.Or(total.null, null_fn(*args)), val_fn(*args))
        V = _value_sort()
        target = e.this
        distinct = False
        # COUNT(a, b, ..) counts the rows where no argument is NULL, and COUNT(DISTINCT a, b, ..) the
        # distinct tuples among them: the argument is the tuple, NULL when any component is, and its
        # value an uninterpreted function of the components (a claim valid for every such function
        # holds for the injective one that a tuple is).
        arguments = None
        if func == "COUNT" and isinstance(target, exp.Distinct) and len(target.expressions) > 1:
            arguments, distinct, target = list(target.expressions), True, None
        elif func == "COUNT" and e.args.get("expressions") and not isinstance(target, exp.Distinct):
            arguments, target = [target, *e.args["expressions"]], None
        if isinstance(target, exp.Distinct):
            if len(target.expressions) != 1:
                raise Unsupported("multi-argument DISTINCT aggregate")
            distinct = True
            target = target.expressions[0]
        if func in ("MIN", "MAX") and e.args.get("expressions"):
            raise Unsupported(f"{func} with several arguments")
        for key in ("having_max", "ignore_nulls", "order", "limit", "separator"):
            if e.args.get(key):
                raise Unsupported(f"aggregate modifier {key}")
        if func in _DUPLICATE_INSENSITIVE:
            distinct = False
        if arguments is not None:
            if any(isinstance(a, exp.Star) for a in arguments):
                raise Unsupported("* among several COUNT arguments")
            vals = [self._val(a, env, None, None) for a in arguments]
            arg = _Val(z3.Or(*[v.null for v in vals]), self._function("Tuple", len(vals))[1](*self._uf_args(vals)))
        elif func == "COUNT" and (target is None or isinstance(target, exp.Star)):
            arg = None
        elif func in ("COUNTIF", "LOGICAL_AND", "LOGICAL_OR"):
            arg = _box(self._pred(target, env, None, None))
        else:
            arg = self._val(target, env, None, None)
        if func == "SUM" and self.dialect == "bigquery":
            try:
                self.sums.observe_sum(e, self._class_of(target, env, None, None))
            except Unsupported:
                self.sums.observe_sum(e, None)
        all_null = arg is not None and func in ("COUNT", "SUM", "MIN", "MAX", "AVG") and z3.is_true(z3.simplify(arg.null))
        if all_null:
            # An aggregate of values that are all NULL: COUNT is 0, the others are NULL. The call is still
            # registered, so the query keeps its aggregate shape (one row for a global aggregate).
            uid = self.fresh("agg")
            agg.calls.append(_AggCall(func, distinct, arg, _Val(z3.BoolVal(False), z3.Const(uid, V))))
            return _Val(z3.BoolVal(False), V.Num(0)) if func == "COUNT" else _Val(z3.BoolVal(True), V.Num(0))
        for call in agg.calls:
            same_arg = (call.arg is None and arg is None) or (
                call.arg is not None and arg is not None
                and call.arg.null.eq(arg.null) and call.arg.val.eq(arg.val)
            )
            if call.func == func and call.distinct == distinct and same_arg:
                return call.var
        uid = self.fresh("agg")
        never_null = func in ("COUNT", "COUNTIF")
        var = _Val(z3.BoolVal(False) if never_null else z3.Bool(f"{uid}#null"), z3.Const(uid, V))
        agg.calls.append(_AggCall(func, distinct, arg, var))
        if func == "SUM" and self.dialect == "bigquery":
            # A sum can overflow (INT64, NUMERIC) whichever order the rows are added in: a site per call, completed with
            # the rows of its group when its select is (``smt_group_sums``). A counterexample database must not hold a
            # sum that can overflow: a plain column of small values cannot, anything else is not known.
            try:
                cls = self._class_of(target, env, None, None)
            except Unsupported:
                cls = None
            smt_group_sums.note(self, e, arg, distinct, cls, maybe=from_avg)
            if isinstance(target, exp.Column):
                self.bounded_sums = True
            else:
                self.uses_uf = True
        return var


# --------------------------------------------------------------------------
# Prover
# --------------------------------------------------------------------------


def _subst(term, pairs):
    """``z3.substitute(term, *pairs)``: the same call into Z3, without z3py's per-pair checks (which
    took most of a proof's time; Z3 itself still rejects a pair of different sorts)."""

    if not pairs:
        return term
    if not isinstance(term, z3.ExprRef):
        return z3.substitute(term, *pairs)
    count = len(pairs)
    old, new = (z3.Ast * count)(), (z3.Ast * count)()
    for i, (x, y) in enumerate(pairs):
        old[i], new[i] = x.as_ast(), y.as_ast()
    return z3.z3._to_expr_ref(z3.Z3_substitute(term.ctx.ref(), term.as_ast(), count, old, new), term.ctx)


def _subst_val(v: _Val, pairs) -> _Val:
    return _Val(_subst(v.null, pairs), _subst(v.val, pairs))


def _with_atoms(block, pairs):
    """``block`` with its existence atoms replaced (condition and HAVING only)."""

    if not pairs:
        return block
    changes = {"cond": _Pred(_subst(block.cond.t, pairs), _subst(block.cond.f, pairs))}
    if isinstance(block, _Agg) and block.having is not None:
        changes["having"] = _Pred(_subst(block.having.t, pairs), _subst(block.having.f, pairs))
    return dataclasses.replace(block, **changes)


def _subst_sub(sub: _Sub, pairs) -> _Sub:
    return _Sub(
        sub.atom,
        sub.occs,
        _subst(sub.guard, pairs),
        [_subst_sub(n, pairs) for n in sub.nested],
        sub.setsrc,
    )


def _pinned_guard(sub: _Sub, pairs, ids) -> object:
    """The guard of a set-source test once its columns are pinned; the pinned outer columns are known non-NULL."""

    known = [
        z3.Not(z3.Bool(leaf.decl().name() + "#null"))
        for _, pin in pairs
        for leaf in (_uninterpreted_leaves(pin) or [])
    ]
    return z3.And(_subst(sub.guard, pairs), *known)


def _atom_copy_pairs(subs: list, tag: str) -> list:
    """Fresh atoms for a copy of a block: an existence test depends on the row, so a
    second row cannot share the first row's atoms."""

    return [(sub.atom, z3.Bool(f"{sub.atom}{tag}")) for sub in subs]


def _occ_pairs(src: _Occ, dst: _Occ) -> list:
    pairs = []
    for name, v in list(src.cols.items()):
        target = dst.col(name)
        pairs.append((v.null, target.null))
        pairs.append((v.val, target.val))
    return pairs


class _CaseSearch:
    """The answers of one ``set_contained_by_cases`` search, so that no check is made twice.

    ``targets`` holds ``(block, parent)`` pairs. A case of a target whose tables and tests are
    those of ``parent`` only adds a filter to it, so it holds no row ``parent`` lacks: ``parent`` is
    the target's index, or ``("joined", index)`` for the target with the tests its own condition
    requires made joins (``joined`` holds those blocks; every case of the target makes the same
    joins), else ``None``. ``known`` maps ``(case key, target index or parent)`` to
    ``(contained, decided)``, decided meaning no check timed out. ``kept`` holds every split, so
    no id in a key is reused while the search runs.
    """

    def __init__(self, targets: list):
        self.targets = targets
        self.joined: dict = {}
        self.known: dict = {}
        self.kept: list = []

    @staticmethod
    def key(parts: frozenset, block) -> tuple:
        """A case by the disjuncts it adds (z3 ids), its tables and its tests. Two cases with one
        key differ only in the order of their conjuncts (``a AND p AND q`` split in either order,
        or ``a AND p AND p`` and ``a AND p`` when no test became a join), so they have the same
        rows and the same answers."""

        return parts, _CaseSearch.shape(block)

    @staticmethod
    def shape(block) -> tuple:
        return tuple(map(id, block.occs)), tuple(map(id, block.subs))

    def answer(self, key, index: int, block, probe=None):
        """Whether target ``index`` contains the case, when that is already known, else ``None``
        (a refutation in which a check timed out is asked again, as before).

        A target ``b AND q`` that only adds a filter to ``b`` needs ``b``'s condition and ``q``
        under the same mappings, so a model refuting ``b`` refutes it too. ``probe(b)`` checks a
        case against a joined target that is not itself a target (only to skip its cases).
        """

        contained, decided = self.known.get((key, index), (None, False))
        if contained or decided:
            return contained
        parent = self.targets[index][1]
        if parent is None or not isinstance(block, _Spj):
            return None
        if (key, parent) not in self.known and probe is not None and parent in self.joined:
            self.known[key, parent] = probe(self.joined[parent])
        return False if self.known.get((key, parent)) == (False, True) else None

    def outside(self, key, block) -> bool:
        """No target contains the case, as far as is already known."""

        return all(self.answer(key, i, block) is False for i in range(len(self.targets)))


class _Prover:
    def __init__(self, timeout_ms: int, constraints: dict[str, TableConstraints] | None = None, types: dict | None = None):
        self.timeout_ms = timeout_ms
        self.constraints = {k.lower(): v for k, v in (constraints or {}).items()}
        # Declared column types, lower-cased: a counterexample has to be a database those tables can hold.
        self.types = {str(table).lower(): {str(c).lower(): t for c, t in columns.items()} for table, columns in (types or {}).items()}
        # Whether the queries order values (<, MIN, ..), and their string literals: see ``real_string_order``.
        self.ordered = False
        self.string_literals: set[str] = set()
        # UNNEST is read as a table of (array, element, offset) rows with one row per array and offset.
        self.constraints[_UNNEST_TABLE] = TableConstraints(keys=(("arr", "offset"),))
        self.unknown = False
        self.wall_clock = False  # a check stopped on the wall clock, not the work cap: the verdict can vary by machine
        self.opaque_sets: set[str] = set()
        self.candidates: list[tuple[object, object, list[_Occ], list[_Val], list[str | None]]] = []
        self.kind_unknowns: list = []  # the compiler's unknown numeric kinds (decimal literals next to undeclared types)

    def declared_kind(self, table: str, column: str) -> str | None:
        """``"int"``, ``"number"`` or ``"string"`` for a column whose declared type fixes what a value in it is, else ``None``."""

        cls = smt_values.type_class(self.types.get(str(table).lower(), {}).get(str(column).lower()))
        if cls == smt_values.INT64:
            return "int"
        if cls in (smt_values.NUMERIC, smt_values.BIGNUMERIC, smt_values.FLOAT64):
            return "number"
        return "string" if cls == smt_values.STRING else None

    def _order_chain(self, strings) -> list:
        """The real order of ``strings`` as facts about ``string_rank``, one per neighbouring pair."""

        rank = _string_rank()
        ordered = sorted(set(strings) | self.string_literals)
        return [rank(z3.StringVal(a)) < rank(z3.StringVal(b)) for a, b in zip(ordered, ordered[1:])]

    def real_string_order(self, model, strings) -> bool:
        """Whether ``model`` orders ``strings``, the queries' literals and every string it ranks itself as strings
        are really ordered (by code point, as BigQuery and DuckDB compare them).

        The proof orders strings by an uninterpreted rank (``_string_rank``), and a model may rank them any way the
        queries' facts allow: ``'A'`` below ``''``, say. A difference found on such a model is no difference between
        the queries: evaluated on it, ``WHERE s < ''`` keeps a row that no engine returns."""

        if not self.ordered:
            return True
        chain = self._order_chain(list(strings) + _ranked_strings(model))
        return all(z3.is_true(model.eval(fact, model_completion=True)) for fact in chain)

    def _ordered_domain(self, values, kinds) -> list:
        """Facts that keep every string cell among strings whose order is stated: the queries' literals and a few
        strings from each gap between them. The last resort of ``_counterexample``: a solver left to choose strings
        rarely chooses one between ``'a'`` and ``'b'``."""

        V, rank = _value_sort(), _string_rank()
        domain = sorted(self.string_literals | set(_strings_between(sorted(self.string_literals))))
        facts = [rank(z3.StringVal(a)) < rank(z3.StringVal(b)) for a, b in zip(domain, domain[1:])]
        for v, kind in zip(values, list(kinds) + [None] * len(values)):
            if kind in ("string", None):
                facts.append(z3.Or(v.null, z3.Not(V.is_Str(v.val)), *[V.str(v.val) == z3.StringVal(text) for text in domain]))
        return facts

    def well_typed(self, db: dict[str, list[dict]]) -> bool:
        """Whether every value sits in a column that can hold it: no string in a number column, no number in a
        STRING column, no fraction in an INT64 column. The value sort has no column types, so a model can break all three."""

        for table, rows in db.items():
            for row in rows:
                for column, value in row.items():
                    kind = self.declared_kind(table, column) if value is not None else None
                    if kind is None:
                        continue
                    number = isinstance(value, (Fraction, int, float)) and not isinstance(value, bool)
                    if (kind == "string") != isinstance(value, str) or (kind != "string" and not number):
                        return False
                    if kind == "int" and Fraction(value).denominator != 1:
                        return False
        return True

    @staticmethod
    def _typing(occs: list[_Occ]):
        """Every non-NULL value in one table column has the same type."""

        V = _value_sort()
        by_column: dict[tuple[str, str], list[_Val]] = {}
        for occ in occs:
            for name, v in occ.cols.items():
                by_column.setdefault((occ.table, name), []).append(v)
        facts = []
        for vals in by_column.values():
            for a, b in zip(vals, vals[1:]):
                facts.append(
                    z3.Implies(
                        z3.And(z3.Not(a.null), z3.Not(b.null)),
                        z3.And(V.is_Num(a.val) == V.is_Num(b.val), V.is_Str(a.val) == V.is_Str(b.val)),
                    )
                )
        return facts

    def _constraint_facts(self, occs: list[_Occ]):
        """NOT NULL columns, and rows with equal keys being the same row."""

        facts = []
        if not self.constraints:
            return facts
        for occ in occs:
            constraint = self.constraints.get(occ.table.lower())
            if constraint is None:
                continue
            for name in list(occ.cols):
                if name in constraint.not_null:
                    facts.append(z3.Not(occ.cols[name].null))
        for i, first in enumerate(occs):
            for second in occs[i + 1:]:
                constraint = self.constraints.get(first.table.lower())
                if constraint is None or first.table != second.table or first.opaque or second.opaque:
                    continue
                for key in constraint.keys:
                    same_key = z3.And(
                        *[
                            z3.And(z3.Not(first.col(k).null), z3.Not(second.col(k).null), _null_eq(first.col(k), second.col(k)))
                            for k in key
                        ]
                    )
                    names = sorted(set(first.cols) | set(second.cols))
                    same_row = z3.And(*[_null_eq(first.col(n), second.col(n)) for n in names])
                    facts.append(z3.Implies(same_key, same_row))
        return facts

    @staticmethod
    def _group_member_facts(occs: list[_Occ]):
        """Facts linking a derived ``GROUP BY`` table to rows of its source in the same scope.

        A row ``s`` of table ``t`` whose key columns equal those of a row ``d`` of
        ``SELECT k, MAX(v), COUNT(*) .. FROM t GROUP BY k`` is a member of ``d``'s group: a non-NULL
        ``s.v`` makes ``MAX(v)`` non-NULL and at least ``s.v`` (``MIN`` at most), ``COUNT(v)`` at
        least one and ``SUM(v)`` non-NULL. Every group has a row, so ``COUNT(*)`` is at least one.
        Only columns the queries already read are used.
        """

        V = _value_sort()
        facts = []
        for d in occs:
            shape = _group_shape(d.table) if d.opaque else None
            if shape is None:
                continue
            table, keys, outputs = shape
            for index, (kind, column) in enumerate(outputs):
                value = d.cols.get(f"c{index}")
                if value is not None and kind in ("count", "count*"):
                    facts.append(z3.And(V.is_Num(value.val), z3.IsInt(V.num(value.val)), V.num(value.val) >= (1 if kind == "count*" else 0)))
            for s in occs:
                if s.opaque or s.table != table:
                    continue
                pairs = [(s.cols.get(k), d.cols.get(f"c{i}")) for k, i in keys]
                if any(a is None or b is None for a, b in pairs):
                    continue
                member = z3.And(*[_null_eq(a, b) for a, b in pairs])
                for index, (kind, column) in enumerate(outputs):
                    value, row = d.cols.get(f"c{index}"), s.cols.get(column) if column else None
                    if value is None or row is None:
                        continue
                    present = z3.And(member, z3.Not(row.null))
                    if kind == "max":
                        facts.append(z3.Implies(present, z3.And(z3.Not(value.null), z3.Not(_lt(value.val, row.val)))))
                    elif kind == "min":
                        facts.append(z3.Implies(present, z3.And(z3.Not(value.null), z3.Not(_lt(row.val, value.val)))))
                    elif kind == "sum":
                        facts.append(z3.Implies(present, z3.Not(value.null)))
                    elif kind == "count":
                        facts.append(z3.Implies(present, V.num(value.val) >= 1))
        return facts

    def valid(self, formula, occs: list[_Occ], facts=()) -> bool:
        solver = bounded_solver(self.timeout_ms)
        solver.add(*self._typing(occs))
        solver.add(*self._constraint_facts(occs))
        solver.add(*self._group_member_facts(occs))
        solver.add(*facts)
        solver.add(z3.Not(formula))
        result = solver.check()
        if result == z3.unsat:
            return True
        if result == z3.sat:
            self._candidate(solver, occs)
        else:
            self.unknown = True
            self.wall_clock = self.wall_clock or solver.reason_unknown() == "timeout"
        return False

    def _candidate(self, solver, occs):
        # Internal proof attempts often discard their candidates or prove the pair.
        # Only extract a model if refutation search actually consumes this candidate.
        # Keep the asserted terms and the plain model, not the solver: a solver holds its whole search state,
        # and a prover that collects many candidates would hold all of it until the search ends.
        # Snapshot the values now: occurrences can acquire more columns later.
        values = [v for occ in occs for v in occ.cols.values()]
        kinds = [self.declared_kind(occ.table, name) for occ in occs for name in occ.cols]
        self.candidates.append((solver.assertions(), solver.model(), list(occs), values, kinds))

    def _counterexample(self, assertions, base, values, kinds=()):
        """A model of a satisfiable check, preferring an integral one.

        Extract candidates in a fresh context: the shared context's term ids can
        change which unconstrained NULL flags Z3 picks across identical calls.
        The proof check above is unchanged, and every candidate is still checked
        against the constraints and both queries before it is returned.
        """

        context = z3.Context()
        isolated = bound(z3.Solver(ctx=context), self.timeout_ms)  # a fresh solver has no limits of its own
        isolated.add(*[assertion.translate(context) for assertion in assertions])
        if isolated.check() != z3.sat:
            return base  # A timeout in the extra search must not lose a satisfiable model.
        model = (self._nice_model(isolated, values, kinds) or isolated.model()).translate(base.ctx)
        for attempt in range(_ORDER_ROUNDS + 1 if self.ordered else 0):
            # ``_find_counterexample`` drops a candidate whose strings the model ranks out of their real order. Tell
            # the solver the real order of the strings it chose and ask again; it may then choose others, so after a
            # few rounds give it a small set of strings to choose from whose order is stated in full.
            strings = [cell for cell in (_cell(model, v) for v in values) if isinstance(cell, str)] + _ranked_strings(model)
            if self.real_string_order(model, strings):
                break
            facts = self._order_chain(strings) if attempt < _ORDER_ROUNDS else self._ordered_domain(values, kinds)
            # A fresh solver each round, for the reason ``_nice_model`` gives.
            retry = bound(z3.Solver(ctx=context), self.timeout_ms)
            retry.add(*isolated.assertions())
            retry.add(*[fact.translate(context) for fact in facts])
            if retry.check() != z3.sat:
                continue  # a round the solver could not finish, or a set of strings too small: the next may do
            isolated = retry if attempt < _ORDER_ROUNDS else isolated
            model = (self._nice_model(retry, values, kinds) or retry.model()).translate(base.ctx)
        return model

    def _nice_model(self, solver, values, kinds=()):
        """A model whose values fit their columns. A column with a declared type gets a value of that type (the
        value sort itself has none, so the solver would as soon put a number in a STRING column). For the others,
        prefer integer-valued numeric counterexamples (they fit INT64 and FLOAT64), then any numbers, so a column
        the queries only compare with numbers is not given a string."""

        V = _value_sort()
        kinds = list(kinds) + [None] * (len(values) - len(kinds))
        declared = []
        for v, kind in zip(values, kinds):
            if kind == "string":
                declared.append(z3.Implies(z3.Not(v.null), V.is_Str(v.val)))
            elif kind is not None:
                fits = z3.And(V.is_Num(v.val), z3.IsInt(V.num(v.val))) if kind == "int" else V.is_Num(v.val)
                declared.append(z3.Implies(z3.Not(v.null), fits))
        free = [v for v, kind in zip(values, kinds) if kind is None]
        integral = [z3.Implies(V.is_Num(v.val), z3.IsInt(V.num(v.val))) for v in values]
        numeric = [z3.Implies(z3.Not(v.null), V.is_Num(v.val)) for v in free]
        assertions = solver.assertions()
        preferences = [integral + numeric, numeric, integral] + ([[]] if declared else [])
        for extra in [declared + preference for preference in preferences]:
            # A fresh solver per preference, never push/pop on the one already checked: ``push`` moves Z3 to its
            # incremental core, which no time limit or work cap covers, and with some ninety string constants in the
            # assertions it does not return (z3 4.16 and 5.4). A fresh ``check`` is bounded like every other.
            trial = bound(z3.Solver(ctx=solver.ctx), self.timeout_ms)
            trial.add(*assertions)
            trial.add(*[fact.translate(solver.ctx) for fact in extra])
            if trial.check() == z3.sat:
                return trial.model()
        return None

    def witness(self, pred, occs: list[_Occ], facts=()) -> None:
        solver = bounded_solver(self.timeout_ms)
        solver.add(*self._typing(occs))
        solver.add(*self._constraint_facts(occs))
        solver.add(*facts)
        solver.add(pred)
        if solver.check() == z3.sat:
            self._candidate(solver, occs)

    def unify_subs(self, a_subs: list, b_subs: list, base_pairs: list, scope: list, facts):
        """Group equivalent existence tests so equal tests share one atom.

        Returns substitutions for ``a``'s atoms and for ``b``'s atoms (after the
        outer variables of ``b`` are mapped by ``base_pairs``). Tests with no
        equivalent partner keep their own free atom, which can only make a
        proof fail, never succeed.
        """

        moved = [_subst_sub(sb, base_pairs) for sb in b_subs]
        entries = [(sub, "a") for sub in a_subs] + [(sub, "b") for sub in moved]
        reps: list[tuple[_Sub, object]] = []
        a_pairs, b_pairs = [], []
        for sub, side in entries:
            rep = None
            for known, atom in reps:
                if self._sub_implies(known, sub, scope, facts) and self._sub_implies(sub, known, scope, facts):
                    rep = atom
                    break
            if rep is None:
                reps.append((sub, sub.atom))
                rep = sub.atom
            if not rep.eq(sub.atom):
                (a_pairs if side == "a" else b_pairs).append((sub.atom, rep))
        # ``b`` atoms keep their identity in ``b_subs``; moved copies share atoms.
        return a_pairs, b_pairs

    def _sub_implies(self, sa: _Sub, sb: _Sub, scope: list, facts) -> bool:
        """Every row satisfying ``sa`` forces some row satisfying ``sb`` (same outer variables)."""

        for mapping in self._homomorphisms(sb.occs, sa.occs):
            pairs = self._pairs(mapping)
            a_nested, b_nested = self.unify_subs(sa.nested, sb.nested, pairs, scope + sa.occs, facts)
            guard_a = _subst(sa.guard, a_nested)
            target = _subst(sb.guard, pairs + b_nested)
            saved = len(self.candidates)
            if self.valid(z3.Implies(guard_a, target), scope + sa.occs + sb.occs, facts):
                return True
            del self.candidates[saved:]
        return False

    def unsatisfiable(self, pred, occs: list[_Occ], facts=()) -> bool:
        solver = bounded_solver(self.timeout_ms)
        solver.add(*self._typing(occs))
        solver.add(*self._constraint_facts(occs))
        solver.add(*facts)
        solver.add(pred)
        return solver.check() == z3.unsat

    def merge_key_occurrences(self, block):
        """Fold occurrences of one table that a key forces onto the same row.

        If the block's condition makes two occurrences agree on a key, each
        row of the table pairs with itself exactly once, so the second
        occurrence adds nothing: the multiplicity is unchanged.
        """

        if not self.constraints or any(o.opaque for o in block.occs):
            return block
        changed = True
        while changed:
            changed = False
            for i, first in enumerate(block.occs):
                for second in block.occs[i + 1:]:
                    constraint = self.constraints.get(first.table.lower())
                    if constraint is None or first.table != second.table:
                        continue
                    for key in constraint.keys:
                        same_key = z3.And(
                            *[
                                z3.And(z3.Not(first.col(k).null), z3.Not(second.col(k).null), _null_eq(first.col(k), second.col(k)))
                                for k in key
                            ]
                        )
                        implied = z3.Implies(block.cond.t, same_key)
                        saved = len(self.candidates)
                        if self.valid(implied, block.occs, block.facts):
                            self._merge(block, first, second)
                            changed = True
                            break
                        del self.candidates[saved:]
                    if changed:
                        break
                if changed:
                    break
        return block

    def inline_unique_subs(self, block, require_unique: bool = True):
        """Turn a required existence test into a join (when it has at most one witness).

        With ``require_unique=False`` (valid only where duplicate rows do not
        matter, i.e. under DISTINCT) any required existence test becomes a join.

        ``EXISTS (SELECT .. FROM u WHERE u.id = t.id)`` with ``u.id`` a key has
        at most one matching row per outer row, so it multiplies the block's
        rows by exactly 1 where it holds: the same as joining ``u`` in.
        """

        if (require_unique and not self.constraints) or any(o.opaque for o in block.occs):
            return block
        for sub in list(block.subs):
            if any(o.opaque for o in sub.occs) or not sub.occs:
                continue
            saved = len(self.candidates)
            required = self.valid(z3.Implies(block.cond.t, sub.atom), block.occs, block.facts)
            del self.candidates[saved:]
            if not required or (require_unique and not self._single_witness(sub, block)):
                continue
            guard = sub.guard
            block.occs = block.occs + sub.occs
            block.cond = _Pred(
                z3.And(_subst(block.cond.t, [(sub.atom, z3.BoolVal(True))]), guard),
                z3.Not(z3.And(_subst(block.cond.t, [(sub.atom, z3.BoolVal(True))]), guard)),
            )
            block.subs = [x for x in block.subs if x is not sub] + sub.nested
        return block

    def _single_witness(self, sub: _Sub, block) -> bool:
        """Two rows satisfying the guard for the same outer row share a key in every table."""

        copies = [o.copy(o.uid + "'") for o in sub.occs]
        copy_pairs = _atom_copy_pairs(sub.nested, "'")
        for o, c in zip(sub.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        clauses = []
        for o, c in zip(sub.occs, copies):
            constraint = self.constraints.get(o.table.lower())
            if constraint is None or not constraint.keys:
                return False
            clauses.append(
                z3.Or(
                    *[
                        z3.And(*[z3.And(z3.Not(o.col(k).null), _null_eq(o.col(k), c.col(k))) for k in key])
                        for key in constraint.keys
                    ]
                )
            )
        both = z3.And(sub.guard, _subst(sub.guard, copy_pairs))
        saved = len(self.candidates)
        ok = self.valid(z3.Implies(both, z3.And(*clauses)), block.occs + sub.occs + copies, block.facts)
        del self.candidates[saved:]
        return ok

    def _merge(self, block, keep: _Occ, drop: _Occ) -> None:
        pairs = _occ_pairs(drop, keep)
        block.occs = [o for o in block.occs if o is not drop]
        block.cond = _Pred(_subst(block.cond.t, pairs), _subst(block.cond.f, pairs))
        block.outputs = [_subst_val(v, pairs) for v in block.outputs]
        block.facts = [_subst(f, pairs) for f in block.facts]
        block.subs = [_subst_sub(sub, pairs) for sub in block.subs]
        if isinstance(block, _Agg):
            block.keys = [_subst_val(k, pairs) for k in block.keys]
            for call in block.aggs:
                if call.arg is not None:
                    call.arg = _subst_val(call.arg, pairs)
            if block.having is not None:
                block.having = _Pred(_subst(block.having.t, pairs), _subst(block.having.f, pairs))

    def legal_database(self, db: dict[str, list[dict]]) -> dict[str, list[dict]] | None:
        """Respect declared constraints: ``None`` if the database cannot satisfy them."""

        if not self.constraints:
            return db
        legal = {}
        for table, rows in db.items():
            constraint = self.constraints.get(table.lower())
            if constraint is None:
                legal[table] = rows
                continue
            for row in rows:
                if any(row.get(c, 1) is None for c in constraint.not_null):
                    return None
            if constraint.keys:
                unique = list({tuple(sorted(r.items(), key=lambda kv: kv[0])): r for r in rows}.values())
                for key in constraint.keys:
                    seen = set()
                    for row in unique:
                        value = tuple(row.get(k) for k in key)
                        if any(v is None for v in value):
                            continue
                        if value in seen:
                            return None
                        seen.add(value)
                rows = unique
            legal[table] = rows
        for table, rows in legal.items():
            constraint = self.constraints.get(table.lower())
            for columns, parent, parent_columns in (constraint.foreign_keys if constraint else ()):
                parents = {tuple(r.get(c) for c in parent_columns) for r in legal.get(parent, [])}
                for row in rows:
                    value = tuple(row.get(c) for c in columns)
                    if None not in value and value not in parents:
                        return None
        return legal

    # ---- mappings ------------------------------------------------------

    @staticmethod
    def _bijections(occs1: list[_Occ], occs2: list[_Occ]):
        tables1 = Counter(o.table for o in occs1)
        if tables1 != Counter(o.table for o in occs2):
            return
        sides = [
            ([o for o in occs1 if o.table == table], [o for o in occs2 if o.table == table]) for table in sorted(tables1)
        ]

        def mappings(index: int):
            # itertools.product order over each table's permutations, built lazily: eight occurrences of
            # one table have 40,320 permutations, of which at most _MAX_MAPPINGS are ever read.
            if index == len(sides):
                yield []
                return
            left, right = sides[index]
            for perm in itertools.permutations(left):
                head = list(zip(right, perm))
                for rest in mappings(index + 1):
                    yield head + rest

        yield from itertools.islice(mappings(0), _MAX_MAPPINGS)

    @staticmethod
    def _homomorphisms(src: list[_Occ], dst: list[_Occ]):
        """Maps from src occurrences to dst occurrences of the same table."""

        choices = []
        for occ in src:
            options = [d for d in dst if d.table == occ.table]
            if not options:
                return
            choices.append([(occ, d) for d in options])
        for count, combo in enumerate(itertools.product(*choices)):
            if count >= _MAX_MAPPINGS:
                return
            yield list(combo)

    @staticmethod
    def _pairs(mapping) -> list:
        pairs = []
        for src, dst in mapping:
            pairs.extend(_occ_pairs(src, dst))
        return pairs

    # ---- block comparisons ---------------------------------------------

    def spj_bag_equal(self, a: _Spj, b: _Spj) -> bool:
        for mapping in self._bijections(a.occs, b.occs):
            pairs = self._pairs(mapping)
            facts = a.facts + [_subst(f, pairs) for f in b.facts]
            a_atoms, b_atoms = self.unify_subs(a.subs, b.subs, pairs, a.occs, facts)
            cond_a = _subst(a.cond.t, a_atoms)
            pairs = pairs + b_atoms
            cond_b = _subst(b.cond.t, pairs)
            outs_b = [_subst_val(v, pairs) for v in b.outputs]
            if not self.valid(cond_a == cond_b, a.occs, facts):
                continue
            if self.valid(z3.Implies(cond_a, _rows_eq(a.outputs, outs_b)), a.occs, facts):
                return True
        return False

    def spj_set_contained(self, a: _Spj, b: _Spj) -> bool:
        """Every row of ``a`` is a row of ``b`` (set semantics)."""

        for mapping in self._homomorphisms(b.occs, a.occs):
            pairs = self._pairs(mapping)
            facts = a.facts + [_subst(f, pairs) for f in b.facts]
            a_atoms, b_atoms = self.unify_subs(a.subs, b.subs, pairs, a.occs, facts)
            cond_a = _subst(a.cond.t, a_atoms)
            pairs = pairs + b_atoms
            cond_b = _subst(b.cond.t, pairs)
            outs_b = [_subst_val(v, pairs) for v in b.outputs]
            if self.valid(z3.Implies(cond_a, z3.And(cond_b, _rows_eq(a.outputs, outs_b))), a.occs, facts):
                return True
        return False

    def agg_equal(self, a: _Agg, b: _Agg) -> bool:
        if a.is_global != b.is_global or len(a.outputs) != len(b.outputs):
            return False
        for mapping in self._bijections(a.occs, b.occs):
            if self._agg_equal_under(a, b, mapping):
                return True
        return False

    def _agg_equal_under(self, a: _Agg, b: _Agg, mapping) -> bool:
        pairs = self._pairs(mapping)
        facts = a.facts + [_subst(f, pairs) for f in b.facts]
        a_atoms, b_atoms = self.unify_subs(a.subs, b.subs, pairs, a.occs, facts)
        a = _with_atoms(a, a_atoms)
        pairs = pairs + b_atoms
        if not self.valid(a.cond.t == _subst(b.cond.t, pairs), a.occs, facts):
            return False
        keys_b = [_subst_val(k, pairs) for k in b.keys]
        if not a.is_global:
            copies = [o.copy(o.uid + "'") for o in a.occs]
            copy_pairs = _atom_copy_pairs(a.subs, "'")
            for o, c in zip(a.occs, copies):
                copy_pairs.extend(_occ_pairs(o, c))

            def key_eq(keys):
                other = [_subst_val(k, copy_pairs) for k in keys]
                return _rows_eq(keys, other)

            both = z3.And(a.cond.t, _subst(a.cond.t, copy_pairs))
            same_groups = z3.Implies(both, key_eq(a.keys) == key_eq(keys_b))
            copy_facts = facts + [_subst(f, copy_pairs) for f in facts]
            if not self.valid(same_groups, a.occs + copies, copy_facts):
                return False
        # Merge aggregates of ``a`` that always agree, then map ``b`` onto them.
        own_pairs = []
        for later, call in enumerate(a.aggs):
            for earlier in a.aggs[:later]:
                if self._same_aggregate(a, earlier, call.func, call.distinct, call.arg, facts):
                    own_pairs.extend([(call.var.null, earlier.var.null), (call.var.val, earlier.var.val)])
                    break
        own_pairs = [p for p in own_pairs if not p[0].eq(p[1])]
        agg_pairs = []
        for call_b in b.aggs:
            arg_b = _subst_val(call_b.arg, pairs) if call_b.arg is not None else None
            first = None
            for call_a in a.aggs:
                if self._same_aggregate(a, call_a, call_b.func, call_b.distinct, arg_b, facts):
                    if first is None:
                        first = call_a
                        agg_pairs.extend([(call_b.var.null, call_a.var.null), (call_b.var.val, call_a.var.val)])
                    else:
                        # another call of ``a`` that agrees with the same call of ``b`` (COUNT(x) and COUNT(y) over NOT NULL columns)
                        own_pairs.extend([(call_a.var.null, first.var.null), (call_a.var.val, first.var.val)])
        own_pairs = [p for p in own_pairs if not p[0].eq(p[1])]
        all_pairs = pairs + [p for p in agg_pairs if not p[0].eq(p[1])]
        # Aggregate values are left free: the claim must hold for any group, with the facts every
        # group satisfies (counts are whole numbers, a group has a row, ...).
        calls = [dataclasses.replace(c, arg=_subst_val(c.arg, own_pairs) if c.arg is not None else None, var=_subst_val(c.var, own_pairs)) for c in a.aggs]
        calls += [
            dataclasses.replace(
                c,
                arg=_subst_val(_subst_val(c.arg, pairs), own_pairs) if c.arg is not None else None,
                var=_subst_val(_subst_val(c.var, all_pairs), own_pairs),
            )
            for c in b.aggs
        ]
        facts = facts + self._aggregate_facts(a, calls, facts, None if a.is_global else (copies, copy_pairs, keys_b))
        guard = z3.BoolVal(True) if a.is_global else a.cond.t
        outs_a = [_subst_val(v, own_pairs) for v in a.outputs]
        outs_b = [_subst_val(v, own_pairs) for v in (_subst_val(v, all_pairs) for v in b.outputs)]
        if not self.valid(z3.Implies(guard, _rows_eq(outs_a, outs_b)), a.occs, facts):
            return False
        having_a = _subst(a.having.t, own_pairs) if a.having is not None else z3.BoolVal(True)
        having_b = _subst(_subst(b.having.t, all_pairs), own_pairs) if b.having is not None else z3.BoolVal(True)
        return self.valid(z3.Implies(guard, having_a == having_b), a.occs, facts)

    def _aggregate_facts(self, a: _Agg, calls: list, facts, grouping) -> list:
        """What every group of ``a`` satisfies about the aggregate values ``calls``.

        * COUNT and COUNTIF are whole numbers of at least zero, and a group has at least one row.
        * The group's representative row (the row variables of ``a``) is one of its members: a non-NULL
          argument there makes MIN/MAX/SUM non-NULL and COUNT at least one.
        * An argument that is the same on every row of a group (a function of the group key) is its
          own MIN/MAX/SUM(DISTINCT)/BIT_AND/BIT_OR; COUNT of it is 0 when it is NULL and COUNT(*)
          otherwise, COUNT(DISTINCT) of it is 0 or 1.
        * SUM/MIN/MAX of x is NULL exactly when COUNT of an argument NULL on the same rows is 0.

        ``grouping`` is ``(copies, copy_pairs, keys_b)`` for a grouped ``a`` (a second row of the
        same group), ``None`` for a global aggregate.
        """

        V = _value_sort()
        out: list = []
        cond = a.cond.t
        seen: set = set()
        unique = []
        for c in calls:
            ident = (c.func, c.distinct, c.var.null.get_id(), c.var.val.get_id())
            if ident not in seen:
                seen.add(ident)
                unique.append(c)
        counts = [c for c in unique if c.func == "COUNT"]
        star = next((c for c in counts if c.arg is None), None)
        for c in unique:
            if c.func in ("COUNT", "COUNTIF"):
                out.append(z3.And(V.is_Num(c.var.val), z3.IsInt(V.num(c.var.val)), V.num(c.var.val) >= 0))
        if grouping is not None:
            copies, copy_pairs, _ = grouping
            if star is None:
                uid = self._fresh_count()
                star = _AggCall("COUNT", False, None, _Val(z3.BoolVal(False), z3.Const(uid, V)))
                out.append(z3.And(V.is_Num(star.var.val), z3.IsInt(V.num(star.var.val))))
            out.append(V.num(star.var.val) >= 1)
            copy_facts = list(facts) + [_subst(f, copy_pairs) for f in facts]
            both = z3.And(cond, _subst(cond, copy_pairs), _rows_eq(a.keys, [_subst_val(k, copy_pairs) for k in a.keys]))
            for c in unique:
                if c.arg is None:
                    continue
                present = z3.And(cond, z3.Not(c.arg.null))
                if c.func in _NULL_WHEN_EMPTY or c.func in ("BIT_AND", "BIT_OR", "BIT_XOR"):
                    out.append(z3.Implies(present, z3.Not(c.var.null)))
                elif c.func == "COUNT":
                    out.append(z3.Implies(present, V.num(c.var.val) >= 1))
                if c.func == "COUNTIF" or c.func == "BIT_XOR" or (c.func == "SUM" and not c.distinct):
                    continue
                if not any(c.arg.null.eq(k.null) and c.arg.val.eq(k.val) for k in a.keys):
                    saved = len(self.candidates)
                    constant = self.valid(
                        z3.Implies(both, _null_eq(c.arg, _subst_val(c.arg, copy_pairs))), a.occs + copies, copy_facts
                    )
                    del self.candidates[saved:]
                    if not constant:
                        continue
                if c.func == "COUNT" and c.distinct:
                    value = z3.If(c.arg.null, V.Num(0), V.Num(1))
                    out.append(z3.Implies(cond, c.var.val == value))
                elif c.func == "COUNT":
                    out.append(z3.Implies(cond, c.var.val == z3.If(c.arg.null, V.Num(0), star.var.val)))
                elif c.func in _NULL_WHEN_EMPTY:
                    out.append(z3.Implies(cond, _null_eq(c.var, c.arg)))
                else:
                    # BIT_AND/BIT_OR of a group of NULLs is not NULL in MySQL: only the non-NULL case is known.
                    out.append(z3.Implies(present, _null_eq(c.var, c.arg)))
        for c in unique:
            if c.func not in _NULL_WHEN_EMPTY or c.arg is None:
                continue
            for n in counts:
                if n.arg is None:
                    same = z3.Implies(cond, z3.Not(c.arg.null))
                elif n.arg.null.eq(c.arg.null):
                    same = None
                else:
                    same = z3.Implies(cond, n.arg.null == c.arg.null)
                if same is not None:
                    saved = len(self.candidates)
                    ok = self.valid(same, a.occs, facts)
                    del self.candidates[saved:]
                    if not ok:
                        continue
                out.append(c.var.null == (V.num(n.var.val) == 0))
                break
        return out

    def _fresh_count(self) -> str:
        self.count_vars = getattr(self, "count_vars", 0) + 1
        return f"count*#{self.count_vars}"

    def _same_aggregate(self, a: _Agg, call_a: _AggCall, func: str, distinct: bool, arg: _Val | None, facts) -> bool:
        """Whether ``call_a`` and the other call agree on every group of ``a``."""

        if call_a.func != func or call_a.distinct != distinct:
            return False
        if (call_a.arg is None) != (arg is None):
            # COUNT(x) is COUNT(*) when x is never NULL.
            present = call_a.arg if call_a.arg is not None else arg
            return not distinct and self.valid(z3.Implies(a.cond.t, z3.Not(present.null)), a.occs, facts)
        if arg is None:
            return True
        if self.valid(z3.Implies(a.cond.t, _null_eq(call_a.arg, arg)), a.occs, facts):
            return True
        if func == "COUNT" and not distinct:
            # COUNT only sees whether its argument is NULL: two arguments that are NULL on the same rows
            # give the same count (COUNT(CASE WHEN p THEN 'x' END) is COUNT(CASE WHEN p THEN 1 END)).
            return self.valid(z3.Implies(a.cond.t, call_a.arg.null == arg.null), a.occs, facts)
        return False

    def set_source_facts(self, block) -> list:
        """``NOT NULL`` for each column of the block's DISTINCT derived tables that the table never
        returns as NULL (a NOT NULL column, or one its WHERE filters), so ``d.x IS NULL`` is FALSE."""

        facts, stack = [], list(block.subs)
        while stack:
            sub = stack.pop()
            stack.extend(sub.nested)
            if not sub.setsrc:
                continue
            inner, columns = sub.setsrc
            for output, column in zip(inner.outputs, columns):
                saved = len(self.candidates)
                if self.valid(z3.Implies(inner.cond.t, z3.Not(output.null)), inner.occs, inner.facts):
                    facts.append(z3.Not(column.null))
                del self.candidates[saved:]
        return facts

    def resolve_set_sources(self, block):
        """Pin the columns of DISTINCT derived tables to the outer columns they are joined on.

        Each such table is, per outer row, either matched by exactly one row or
        not at all, so the join is an existence test. Raises ``_SetSourceUnresolved``
        when some column is not pinned to a column that cannot be NULL.
        """

        def walk(subs):
            for sub in subs:
                yield sub
                yield from walk(sub.nested)

        if not any(sub.setsrc for sub in walk(block.subs)):
            return block
        if not isinstance(block, (_Spj, _Agg)) or any(o.opaque for o in block.occs):
            raise _SetSourceUnresolved
        conjuncts, stack = [], [block.cond.t]
        while stack:
            term = stack.pop()
            if z3.is_and(term):
                stack.extend(term.children())
            else:
                conjuncts.append(term)
        ids = {c.get_id() for c in conjuncts}
        pairs = []
        for sub in block.subs:
            if not sub.setsrc:
                continue
            inner, columns = sub.setsrc
            if sub.atom.get_id() not in ids or any(x.setsrc for x in walk(sub.nested)):
                raise _SetSourceUnresolved
            distinct = self.as_distinct_spj(inner)
            if not (isinstance(distinct, _Spj) and distinct.distinct):
                raise _SetSourceUnresolved
            for position, column in enumerate(columns):
                produced = inner.outputs[position] if position < len(inner.outputs) else None
                if produced is not None and _is_ground(produced.val) and _is_ground(produced.null):
                    # a constant column is pinned by what it is
                    pairs.extend([(column.val, produced.val), (column.null, produced.null)])
                    continue
                if not _implied(conjuncts, z3.Not(column.null), ids):
                    raise _SetSourceUnresolved  # only a non-NULL column can be pinned by an equality
                pairs.append((column.null, z3.BoolVal(False)))
                for term in conjuncts:
                    if not z3.is_eq(term):
                        continue
                    left, right = term.children()
                    if right.eq(column.val):
                        left, right = right, left
                    if not left.eq(column.val):
                        continue
                    leaves = _uninterpreted_leaves(right)
                    if not leaves or any(leaf.eq(column.val) or leaf.sort() == z3.BoolSort() for leaf in leaves):
                        continue
                    # an arithmetic expression over outer columns that the condition requires to be non-NULL
                    if all(_implied(conjuncts, z3.Not(z3.Bool(leaf.decl().name() + "#null")), ids) for leaf in leaves):
                        pairs.append((column.val, right))
                        break
                else:
                    raise _SetSourceUnresolved
        # The test already requires the pinned columns to be non-NULL, so the
        # condition need not repeat it (the existence atom is a free Boolean).
        known = [
            (z3.Not(z3.Bool(leaf.decl().name() + "#null")), z3.BoolVal(True))
            for _, pin in pairs
            for leaf in (_uninterpreted_leaves(pin) or [])
        ]
        changes = {
            "cond": _Pred(_subst(_subst(block.cond.t, pairs), known), _subst(block.cond.f, pairs)),
            "outputs": [_subst_val(v, pairs) for v in block.outputs],
            "subs": [
                dataclasses.replace(_subst_sub(sub, pairs), setsrc=None, guard=_pinned_guard(sub, pairs, ids))
                if sub.setsrc
                else _subst_sub(sub, pairs)
                for sub in block.subs
            ],
        }
        if isinstance(block, _Agg):
            changes["keys"] = [_subst_val(k, pairs) for k in block.keys]
            changes["aggs"] = [
                _AggCall(c.func, c.distinct, _subst_val(c.arg, pairs) if c.arg is not None else None, c.var)
                for c in block.aggs
            ]
            if block.having is not None:
                changes["having"] = _Pred(_subst(block.having.t, pairs), _subst(block.having.f, pairs))
        return dataclasses.replace(block, **changes)

    def sub_consequences(self, block):
        """Facts ``atom => condition on outer columns`` implied by each existence test.

        A witness row satisfies the guard, so a guard condition over columns that
        the guard equates to outer columns holds of those outer columns. The atom
        is otherwise free, so without this a required test would not tell the
        prover that, e.g., ``d.x = e.x AND e.x > 7`` forces ``d.x > 7``.
        """

        if not block.subs or any(o.opaque for o in block.occs):
            return block
        extra = []
        for sub in block.subs:
            if any(o.opaque for o in sub.occs):
                continue
            inner_ids = set()
            for occ in sub.occs:
                for v in occ.cols.values():
                    inner_ids.update((v.null.get_id(), v.val.get_id()))
            conjuncts, stack = [], [sub.guard]
            while stack:
                term = stack.pop()
                if z3.is_and(term):
                    stack.extend(term.children())
                else:
                    conjuncts.append(term)

            def consts(term):
                found, todo = set(), [term]
                while todo:
                    t = todo.pop()
                    if z3.is_const(t) and t.decl().kind() == z3.Z3_OP_UNINTERPRETED:
                        found.add(t.get_id())
                    todo.extend(t.children())
                return found

            ids = {c.get_id() for c in conjuncts}
            pairs = []
            for occ in sub.occs:
                for v in occ.cols.values():
                    for term in conjuncts:
                        if not z3.is_eq(term):
                            continue
                        left, right = term.children()
                        if right.eq(v.val):
                            left, right = right, left
                        if not left.eq(v.val) or consts(right) & inner_ids:
                            continue
                        # ``right`` is an outer term; the pair is known non-NULL when both sides' flags are.
                        outer_null = z3.Bool(right.decl().name() + "#null") if z3.is_const(right) else None
                        if (
                            outer_null is not None
                            and z3.Not(v.null).get_id() in ids
                            and z3.Not(outer_null).get_id() in ids
                        ):
                            pairs.append((v.val, right))
                            pairs.append((v.null, outer_null))
                            break
            for term in conjuncts:
                used = consts(term)
                if not used:
                    continue
                moved = _subst(term, pairs)
                if consts(moved) & inner_ids:
                    continue
                extra.append(z3.Implies(sub.atom, moved))
        if extra:
            block.facts = block.facts + extra
        return block

    def self_witness_facts(self, block):
        """Facts ``guard(o) => atom``: a row the block reads can witness an existence test itself.

        An occurrence ``o`` of the block is a row of its table, so when ``o`` satisfies the guard of
        a test over that same table (one occurrence, no nested tests), the test holds. This lets
        ``l.user_id = 1 AND l.page_id NOT IN (SELECT page_id FROM likes WHERE user_id = 1)`` be seen
        to be never TRUE: ``l`` itself is in the subquery.
        """

        if not block.subs or any(o.opaque for o in block.occs):
            return block
        extra = []
        for sub in block.subs:
            if len(sub.occs) != 1 or sub.nested or sub.setsrc or sub.occs[0].opaque:
                continue
            inner = sub.occs[0]
            for occ in block.occs:
                if occ.table != inner.table or occ.columns != inner.columns:  # BigQuery: ds.T is not ds.t
                    continue
                extra.append(z3.Implies(_subst(sub.guard, _occ_pairs(inner, occ)), sub.atom))
        if extra:
            block.facts = block.facts + extra
        return block

    def merge_equivalent_subs(self, block):
        """Two existence tests of one block that agree under its plain conditions share one atom.

        ``o.k = l.k AND EXISTS(.. b.k = o.k) AND EXISTS(.. b.k = l.k)`` tests the same thing twice: the
        conditions that mention no existence atom make the two guards equal, so both atoms mean the same.
        """

        subs = [sub for sub in block.subs if sub.setsrc is None]
        if len(block.subs) < 2 or len(subs) < 2:
            return block
        atoms = set()

        def collect(items):
            for sub in items:
                atoms.add(sub.atom.get_id())
                collect(sub.nested)

        collect(block.subs)

        def mentions_atom(term) -> bool:
            stack = [term]
            while stack:
                t = stack.pop()
                if z3.is_const(t) and t.get_id() in atoms:
                    return True
                stack.extend(t.children())
            return False

        conjuncts, todo = [], [block.cond.t]
        while todo:
            term = todo.pop()
            if z3.is_and(term):
                todo.extend(term.children())
            else:
                conjuncts.append(term)
        plain = [c for c in conjuncts if not mentions_atom(c)]
        facts = block.facts + plain
        pairs, dropped = [], set()
        for i, first in enumerate(subs):
            if id(first) in dropped:
                continue
            for second in subs[i + 1:]:
                if id(second) in dropped:
                    continue
                if self._sub_implies(first, second, block.occs, facts) and self._sub_implies(second, first, block.occs, facts):
                    pairs.append((second.atom, first.atom))
                    dropped.add(id(second))
        if not pairs:
            return block
        block = _with_atoms(block, pairs)
        block.subs = [sub for sub in block.subs if id(sub) not in dropped]
        return block

    def drop_never_null_having(self, block):
        """``HAVING MIN(x) IS NOT NULL`` holds for every group when ``x`` is never NULL in the group's rows."""

        if not isinstance(block, _Agg) or block.is_global or block.having is None or block.subs:
            return block
        never_null = {
            c.var.null.get_id()
            for c in block.aggs
            if c.func in ("MIN", "MAX", "SUM", "AVG") and not c.distinct and c.arg is not None and c.var.null.decl().kind() == z3.Z3_OP_UNINTERPRETED
            and self.valid(z3.Implies(block.cond.t, z3.Not(c.arg.null)), block.occs, block.facts)
        }
        if not never_null:
            return block
        top = block.having.t
        conjuncts = list(top.children()) if z3.is_and(top) else [top]
        kept = [c for c in conjuncts if not (z3.is_not(c) and c.arg(0).get_id() in never_null)]
        if len(kept) == len(conjuncts):
            return block
        if kept:
            rest = z3.And(*kept) if len(kept) > 1 else kept[0]
            block.having = _Pred(rest, z3.Not(rest))
        else:
            block.having = None
        return block

    def push_having(self, block):
        """``HAVING`` on group keys only is a ``WHERE`` filter (every row of a group agrees on it)."""

        if (
            not isinstance(block, _Agg)
            or block.is_global
            or block.having is None
            or block.subs
            or any(o.opaque for o in block.occs)
        ):
            return block
        agg_terms = {t.get_id() for c in block.aggs for t in (c.var.null, c.var.val) if z3.is_const(t)}

        def has_aggregate(root) -> bool:
            stack = [root]
            while stack:
                term = stack.pop()
                if z3.is_const(term) and term.decl().kind() == z3.Z3_OP_UNINTERPRETED and term.get_id() in agg_terms:
                    return True
                stack.extend(term.children())
            return False

        top = block.having.t
        conjuncts = list(top.children()) if z3.is_and(top) else [top]
        candidates = [c for c in conjuncts if not has_aggregate(c)]
        if not candidates:
            return block
        copies = [o.copy(o.uid + "'") for o in block.occs]
        copy_pairs = []
        for o, c in zip(block.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        keys_same = _rows_eq(block.keys, [_subst_val(k, copy_pairs) for k in block.keys])
        both = z3.And(block.cond.t, _subst(block.cond.t, copy_pairs), keys_same)
        facts = block.facts + [_subst(f, copy_pairs) for f in block.facts]
        pushed, kept = [], [c for c in conjuncts if has_aggregate(c)]

        def invariant(term) -> bool:
            saved = len(self.candidates)
            ok = self.valid(z3.Implies(both, term == _subst(term, copy_pairs)), block.occs + copies, facts)
            del self.candidates[saved:]
            return ok

        together = z3.And(*candidates) if len(candidates) > 1 else candidates[0]
        if invariant(together):
            pushed = list(candidates)  # conjuncts can depend on each other (a NULL test guards a comparison)
        else:
            for conjunct in candidates:
                (pushed if invariant(conjunct) else kept).append(conjunct)
        if not pushed:
            return block
        t = z3.And(block.cond.t, *pushed)
        block.cond = _Pred(t, z3.Not(t))
        if kept:
            rest = z3.And(*kept) if len(kept) > 1 else kept[0]
            block.having = _Pred(rest, z3.Not(rest))
        else:
            block.having = None
        return block

    def as_distinct_spj(self, block):
        """``GROUP BY`` with no aggregates is ``SELECT DISTINCT`` when the
        output determines the group key."""

        if not isinstance(block, _Agg) or block.aggs or block.having is not None or block.is_global:
            return block
        copies = [o.copy(o.uid + "'") for o in block.occs]
        copy_pairs = _atom_copy_pairs(block.subs, "'")
        for o, c in zip(block.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        other_out = [_subst_val(v, copy_pairs) for v in block.outputs]
        other_keys = [_subst_val(v, copy_pairs) for v in block.keys]
        formula = z3.Implies(
            z3.And(block.cond.t, _subst(block.cond.t, copy_pairs), _rows_eq(block.outputs, other_out)),
            _rows_eq(block.keys, other_keys),
        )
        saved = len(self.candidates)
        facts = block.facts + [_subst(f, copy_pairs) for f in block.facts]
        if self.valid(formula, block.occs + copies, facts):
            return _Spj(
                block.occs, block.cond, block.outputs, block.names, distinct=True, facts=block.facts, subs=block.subs
            )
        del self.candidates[saved:]
        return block

    def branch_bag_equal(self, a, b) -> bool:
        if isinstance(a, _Spj) and isinstance(b, _Spj):
            if a.distinct != b.distinct:
                return False
            if a.distinct:
                return self.spj_set_contained(a, b) and self.spj_set_contained(b, a)
            return self.spj_bag_equal(a, b)
        if isinstance(a, _Agg) and isinstance(b, _Agg):
            return a.distinct == b.distinct and self.agg_equal(a, b)
        return False

    def branch_unique(self, a) -> bool:
        """No two rows of this select are equal: equal outputs force the same row of every table.

        Needs a declared key for every table occurrence. Two copies of the block are checked: if both
        satisfy the condition and agree on the outputs, each pair of occurrences agrees on a key.
        """

        if not isinstance(a, _Spj) or a.distinct or not a.occs or any(o.opaque and o.table not in self.opaque_sets for o in a.occs):
            return False
        if not self.constraints and not any(o.opaque for o in a.occs):
            return False
        copies = [o.copy(o.uid + "'") for o in a.occs]
        pairs: list = _atom_copy_pairs(a.subs, "'")
        for o, c in zip(a.occs, copies):
            pairs.extend(_occ_pairs(o, c))
        same = []
        for o, c in zip(a.occs, copies):
            if o.opaque:
                names = [f"c{i}" for i in range(len(o.columns or []))]
                same.append(_rows_eq([o.col(n) for n in names], [c.col(n) for n in names]))
                continue
            constraint = (self.constraints or {}).get(o.table.lower())
            if constraint is None or not constraint.keys:
                return False
            same.append(
                z3.Or(
                    *[
                        z3.And(*[z3.And(z3.Not(o.col(k).null), z3.Not(c.col(k).null), _null_eq(o.col(k), c.col(k))) for k in key])
                        for key in constraint.keys
                    ]
                )
            )
        other_cond = _subst(a.cond.t, pairs)
        other_outs = [_subst_val(v, pairs) for v in a.outputs]
        claim = z3.Implies(z3.And(a.cond.t, other_cond, _rows_eq(a.outputs, other_outs)), z3.And(*same))
        facts = a.facts + [_subst(f, pairs) for f in a.facts]
        saved = len(self.candidates)
        result = self.valid(claim, a.occs + copies, facts)
        del self.candidates[saved:]
        return result

    def branch_set_contained(self, a, b) -> bool:
        if isinstance(a, _Spj) and isinstance(b, _Spj):
            return self.spj_set_contained(a, b)
        # Read as sets, a SELECT DISTINCT is the GROUP BY of its outputs.
        a, b = _spj_as_groups(a), _spj_as_groups(b)
        if isinstance(a, _Agg) and isinstance(b, _Agg):
            return self.agg_equal(a, b)
        return False

    def set_contained_by_cases(self, a, targets) -> bool:
        """Every row of ``a`` is a row of some block in ``targets`` (set semantics).

        When no single target contains ``a``, a disjunction ``p OR q`` among the
        conjuncts of a filter splits that block into ``a AND p`` and ``a AND q``
        (a row passes the filter only if one disjunct is TRUE, so the cases cover
        every row). Each case of ``a`` may land in a different target, and a case
        of a target is part of that target. Up to two disjunctions of ``a`` are split.

        No check is made twice (see ``_CaseSearch``), and a case of a target that only adds a
        filter is not tried for a case its target was refuted for.
        """

        search = _CaseSearch([(b, None) for b in targets])
        key = _CaseSearch.key(frozenset(), a)
        if self._in_some_target(a, key, search):
            return True
        for i, b in enumerate(targets):
            joined = None
            for split in self._disjunctive_cases(b):
                for _, case in split:
                    shape = _CaseSearch.shape(case)
                    if shape != _CaseSearch.shape(b) and joined is None:
                        joined = self._quietly(lambda: self.inline_unique_subs(dataclasses.replace(b), require_unique=False))[0]
                        search.joined["joined", i] = joined
                    parent = i if shape == _CaseSearch.shape(b) else ("joined", i) if shape == _CaseSearch.shape(joined) else None
                    search.targets.append((case, parent))
        return self._contained_by_cases(a, key, search, 2)

    def _contained_by_cases(self, a, key, search: "_CaseSearch", depth: int) -> bool:
        if self._in_some_target(a, key, search):
            return True
        if depth <= 0:
            return False
        for split in self._disjunctive_cases(a):
            search.kept.append(split)
            cases = [(_CaseSearch.key(key[0] | {part.get_id()}, case), case) for part, case in split]
            if depth == 1 and any(search.outside(k, case) for k, case in cases):
                continue  # a case no target holds: this split cannot cover ``a``
            if all(self._contained_by_cases(case, k, search, depth - 1) for k, case in cases):
                return True
        return False

    def _in_some_target(self, a, key, search: "_CaseSearch") -> bool:
        def probe(joined):
            return self._quietly(lambda: self.branch_set_contained(a, joined))

        for index, (b, _) in enumerate(search.targets):
            contained = search.answer(key, index, a, probe)
            if contained is None:
                outer, self.unknown = self.unknown, False
                try:
                    contained = self.branch_set_contained(a, b)
                    search.known[key, index] = (contained, not self.unknown)
                finally:
                    self.unknown = outer or self.unknown
            if contained:
                return True
        return False

    def _quietly(self, check) -> tuple:
        """``(check(), whether no check timed out)`` for work the case search adds only to skip
        checks: it leaves no candidate model and no timeout behind."""

        saved, outer = len(self.candidates), self.unknown
        self.unknown = False
        try:
            return check(), not self.unknown
        finally:
            del self.candidates[saved:]
            self.unknown = outer

    def _disjunctive_cases(self, block):
        """For each disjunction among the conjuncts of a block's filter, the block split by its
        disjuncts, as ``(disjunct, case)`` pairs (one disjunction at a time, when asked for)."""

        if not isinstance(block, _Spj) or any(o.opaque for o in block.occs):
            return
        disjunctions, stack = [], [block.cond.t]
        while stack:
            term = stack.pop()
            if z3.is_and(term):
                stack.extend(term.children())
            elif z3.is_or(term) and 2 <= term.num_args() <= 4:
                disjunctions.append(term.children())
            elif z3.is_distinct(term) and term.num_args() == 2 and term.arg(0).sort() == _value_sort():
                # x <> y is x < y OR y < x where the order is total on the two values.
                x, y = term.children()
                parts = [_lt(x, y), _lt(y, x)]
                saved = len(self.candidates)
                if self.valid(z3.Implies(block.cond.t, z3.Or(*parts)), block.occs, block.facts):
                    disjunctions.append(parts)
                del self.candidates[saved:]
        # Duplicates do not matter under set semantics, so a test a case now requires becomes a join.
        for parts in disjunctions:
            yield [
                (part, self.inline_unique_subs(dataclasses.replace(block, cond=_Pred(z3.And(block.cond.t, part), block.cond.f)), require_unique=False))
                for part in parts
            ]

    def groups_unique(self, block) -> bool:
        """No two groups of a ``GROUP BY`` give the same row: equal outputs force equal group keys,
        whatever the two groups' aggregate values are."""

        if not isinstance(block, _Agg) or block.is_global or block.distinct:
            return False
        copies = [o.copy(o.uid + "''") for o in block.occs]
        copy_pairs = _atom_copy_pairs(block.subs, "''")
        for o, c in zip(block.occs, copies):
            copy_pairs.extend(_occ_pairs(o, c))
        for call in block.aggs:
            for term in (call.var.null, call.var.val):
                if z3.is_const(term) and term.decl().kind() == z3.Z3_OP_UNINTERPRETED:
                    copy_pairs.append((term, z3.Const(f"{term.decl().name()}''", term.sort())))
        other_out = [_subst_val(v, copy_pairs) for v in block.outputs]
        other_keys = [_subst_val(v, copy_pairs) for v in block.keys]
        formula = z3.Implies(
            z3.And(block.cond.t, _subst(block.cond.t, copy_pairs), _rows_eq(block.outputs, other_out)),
            _rows_eq(block.keys, other_keys),
        )
        facts = block.facts + [_subst(f, copy_pairs) for f in block.facts]
        saved = len(self.candidates)
        result = self.valid(formula, block.occs + copies, facts)
        del self.candidates[saved:]
        return result


_CONST_PAIRS_NULL = ("COUNT", "COUNTIF")


def _aggregate_of_nothing(call: _AggCall) -> _Val:
    """The value of an aggregate over an empty group."""

    V = _value_sort()
    return _Val(z3.BoolVal(call.func not in _CONST_PAIRS_NULL), V.Num(0))


def _prune(prover: "_Prover", union: _Union) -> None:
    """Drop blocks that cannot return a row; a global aggregate over nothing is one constant row."""

    kept = []
    for block in union.branches:
        if block.occs and any(o.opaque for o in block.occs):
            # A literal WHERE FALSE needs no model of the opaque source (a global aggregate still returns its one row).
            if (isinstance(block, _Agg) and block.is_global) or not z3.is_false(z3.simplify(block.cond.t)):
                kept.append(block)
            continue
        if not prover.unsatisfiable(block.cond.t, block.occs, block.facts + prover.set_source_facts(block)):
            kept.append(block)
            continue
        if isinstance(block, _Agg) and block.is_global and block.having is None and all(c.func in _NULL_WHEN_EMPTY + _CONST_PAIRS_NULL for c in block.aggs):
            pairs = []
            for call in block.aggs:
                value = _aggregate_of_nothing(call)
                pairs.extend([(call.var.null, value.null), (call.var.val, value.val)])
            outputs = [_subst_val(v, pairs) for v in block.outputs]
            kept.append(_Spj([], _const_pred(True), outputs, block.names, distinct=block.distinct, facts=[]))
        elif isinstance(block, _Agg) and block.is_global:
            kept.append(block)  # HAVING over the empty group: leave to the general path
    union.branches = kept


def _spj_as_groups(block):
    """A ``SELECT DISTINCT`` as the ``GROUP BY`` of its outputs (no aggregates), to compare with a grouped block."""

    if not isinstance(block, _Spj) or not block.distinct:
        return block
    return _Agg(
        block.occs, block.cond, list(block.outputs), [], None, block.outputs, block.names, is_global=False,
        distinct=True, facts=block.facts, subs=block.subs,
    )


def _constant_global(block):
    """A global aggregate whose outputs do not depend on any aggregate (``SELECT COUNT(NULL) FROM t``)
    returns exactly one constant row, like a SELECT without FROM."""

    if (
        isinstance(block, _Agg)
        and block.is_global
        and block.having is None
        and not block.subs
        and all(_is_ground(v.val) and _is_ground(v.null) for v in block.outputs)
    ):
        return _Spj([], _const_pred(True), block.outputs, block.names, distinct=block.distinct, facts=[])
    return block


def _selects_a_set(select: exp.Select) -> bool:
    """A DISTINCT select, or one grouped on exactly its (aggregate-free) outputs, never repeats a row.

    Not a ``DISTINCT ON (k)`` select: it keeps one row per ``k``, and two of them can be equal.
    """

    if plain_distinct(select) and not select.args.get("group"):
        return not any(select.find_all(exp.Window))
    group = select.args.get("group")
    if group is None or extended_grouping(group):
        return False
    outputs = [(i.this if isinstance(i, exp.Alias) else i) for i in select.expressions]
    if any(isinstance(n, (exp.AggFunc, exp.Window)) for o in outputs for n in o.walk()):
        return False
    return {o.sql() for o in outputs} == {g.sql() for g in group.expressions}


def _duplicate_blind(select: exp.Select) -> bool:
    """A plain ``SELECT DISTINCT`` (no GROUP BY, aggregates or windows of its own): its rows are a set
    that depends only on the set of rows each source has."""

    distinct = select.args.get("distinct")
    if distinct is None or distinct.args.get("on") or select.args.get("group") or select.args.get("having"):
        return False
    return not any(n.find_ancestor(exp.Select) is select for n in select.find_all(exp.AggFunc, exp.Window))


def _repeat_blind(block) -> bool:
    """Repeating a row of the block's input leaves its distinct rows alone: no aggregate counts it.

    DISTINCT dedups a block's output rows, not what its aggregates read: ``SELECT DISTINCT COUNT(*)``
    still sees every repeat.
    """

    if not isinstance(block, _Agg):
        return True
    return all(c.distinct or c.func in _DUPLICATE_INSENSITIVE for c in block.aggs)


def _is_set(u: _Union) -> bool:
    return u.distinct or (len(u.branches) == 1 and u.branches[0].distinct)


def _prove(prover: _Prover, left: _Union, right: _Union) -> tuple[bool, str]:
    left.column_names, right.column_names = list(left.names), list(right.names)
    for union in (left, right):
        _prune(prover, union)
        branches = []
        for b in union.branches:
            b = _constant_global(b)
            b = prover.resolve_set_sources(b)
            b = prover.drop_never_null_having(b)
            b = prover.push_having(b)
            b = prover.as_distinct_spj(b)
            b = prover.inline_unique_subs(b)
            b = prover.merge_equivalent_subs(b)
            b = prover.sub_consequences(b)
            b = prover.self_witness_facts(b)
            if (union.distinct or b.distinct) and _repeat_blind(b):
                b = prover.inline_unique_subs(b, require_unique=False)
            branches.append(prover.merge_key_occurrences(b))
        union.branches = branches
    if not left.branches and not right.branches:
        return True, "both queries always return no rows"
    if not left.branches or not right.branches:
        return False, "only one query can return rows"
    for keep, other in ((left, right), (right, left)):
        # A side whose rows are provably distinct (by keys) is as good as a DISTINCT one.
        if _is_set(keep) and not _is_set(other) and len(other.branches) == 1 and (
            prover.branch_unique(other.branches[0]) or prover.groups_unique(other.branches[0])
        ):
            other.branches = [dataclasses.replace(other.branches[0], distinct=True)]
    if _is_set(left) and _is_set(right):
        for a in left.branches:
            if not prover.set_contained_by_cases(a, right.branches):
                return False, "a left branch has rows the right side may lack"
        for b in right.branches:
            if not prover.set_contained_by_cases(b, left.branches):
                return False, "a right branch has rows the left side may lack"
        return True, "set semantics: each side is contained in the other"
    if _is_set(left) != _is_set(right) and not (left.distinct or right.distinct):
        # One side removes duplicates, the other keeps them.
        return False, "only one side removes duplicate rows"
    if left.distinct or right.distinct or len(left.branches) != len(right.branches):
        return False, "UNION shapes differ"

    def match(i: int, used: frozenset) -> bool:
        if i == len(left.branches):
            return True
        for j, b in enumerate(right.branches):
            if j not in used and prover.branch_bag_equal(left.branches[i], b) and match(i + 1, used | {j}):
                return True
        return False

    if match(0, frozenset()):
        return True, "bag semantics: matching blocks are equivalent"
    return False, "no row-preserving mapping between the queries was found"


# --------------------------------------------------------------------------
# Concrete evaluation (counterexample checking)
# --------------------------------------------------------------------------


def _py(model, term):
    V = _value_sort()
    value = model.eval(term, model_completion=True)
    if z3.is_true(model.eval(V.is_Num(value), model_completion=True)):
        num = model.eval(V.num(value), model_completion=True)
        return Fraction(num.as_fraction()) if z3.is_rational_value(num) else Fraction(num.as_decimal(20).rstrip("?"))
    if z3.is_true(model.eval(V.is_Str(value), model_completion=True)):
        return model.eval(V.str(value), model_completion=True).as_string()
    return z3.is_true(model.eval(V.bool(value), model_completion=True))


def _z3_value(value):
    V = _value_sort()
    if isinstance(value, bool):
        return V.Bool(z3.BoolVal(value))
    if isinstance(value, Fraction):
        return V.Num(z3.RealVal(f"{value.numerator}/{value.denominator}"))
    return V.Str(z3.StringVal(value))


def _ranked_strings(model) -> list[str]:
    """The strings ``model`` gives a rank of their own; every other string has its default rank."""

    found, pending = [], []
    with contextlib.suppress(z3.Z3Exception):  # a model that never ranked a string has no interpretation to read
        interpretation = model.get_interp(_string_rank())
        if interpretation is not None:
            pending = [interpretation.entry(i).arg_value(0) for i in range(interpretation.num_entries())]
            pending.append(interpretation.else_value())
    while pending:
        term = pending.pop()
        if z3.is_string_value(term):
            found.append(term.as_string())
        elif z3.is_app(term):
            pending.extend(term.children())
    return found


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _strings_between(literals: list[str], per_gap: int = 2) -> list[str]:
    """Up to ``per_gap`` strings from each gap of the sorted ``literals``, and from below the first and above the
    last. Between two dates, dates: the column may be a DATE."""

    found: list[str] = []
    bounds = [None, *literals, None]
    for low, high in zip(bounds, bounds[1:]):
        candidates = ["a", "b", "A", "0", " ", ""] if low is None else [low + suffix for suffix in ("a", "0", "A", " ")]
        dates = [datetime.date.fromisoformat(text) for text in (low, high) if text is not None and _ISO_DATE.match(text) and _is_date(text)]
        if dates and len(dates) == sum(text is not None for text in (low, high)):
            day = datetime.timedelta(days=1)
            near = [dates[0] + day, dates[0] + 2 * day] if low is not None else [dates[0] - day, dates[0] - 2 * day]
            candidates = [date.isoformat() for date in near if datetime.date.min + 3 * day < date < datetime.date.max - 3 * day] + candidates
        fits = [text for text in candidates if (low is None or low < text) and (high is None or text < high)]
        found.extend(fits[:per_gap])
    return found


def _is_date(text: str) -> bool:
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _cell(model, v: _Val, kind: str | None = None):
    if z3.is_true(model.eval(v.null, model_completion=True)):
        return None
    if not _decided(model, v.val):
        # The model never constrained this value, so z3 would complete it with
        # an arbitrary constructor, often a string; a number loads into any column but a declared STRING one.
        return "" if kind == "string" else Fraction(0)
    return _py(model, v.val)


def _decided(model, term) -> bool:
    """Whether the model itself, not its completion, fixes ``term``'s value."""

    value = model.eval(term, model_completion=False)
    return not (z3.is_const(value) and value.decl().kind() == z3.Z3_OP_UNINTERPRETED)


class _NoCandidate(Exception):
    pass


def _eval_union(u: _Union, db: dict, model) -> Counter:
    total: Counter = Counter()
    for branch in u.branches:
        total.update(_eval_block(branch, db, model))
    if u.distinct:
        total = Counter(set(total))
    return total


def _combinations(occs: list[_Occ], db: dict):
    tables = [db.get(o.table, []) for o in occs]
    size = 1
    for rows in tables:
        size *= len(rows)
    if size > _MAX_EVAL_COMBINATIONS:
        raise _NoCandidate()
    for combo in itertools.product(*tables):
        pairs = []
        for occ, row in zip(occs, combo):
            for name, v in occ.cols.items():
                cell = row.get(name)
                if cell is None:
                    pairs.append((v.null, z3.BoolVal(True)))
                    pairs.append((v.val, _value_sort().Num(0)))
                else:
                    pairs.append((v.null, z3.BoolVal(False)))
                    pairs.append((v.val, _z3_value(cell)))
        yield pairs


def _eval_block(block, db: dict, model) -> Counter:
    rows: Counter = Counter()
    if isinstance(block, _Spj):
        for pairs in _combinations(block.occs, db):
            if z3.is_true(model.eval(_subst(block.cond.t, pairs), model_completion=True)):
                rows[tuple(_cell(model, _subst_val(v, pairs)) for v in block.outputs)] += 1
        return Counter(set(rows)) if block.distinct else rows

    groups: dict[tuple, list] = {}
    for pairs in _combinations(block.occs, db):
        if z3.is_true(model.eval(_subst(block.cond.t, pairs), model_completion=True)):
            key = tuple(_hashable(_cell(model, _subst_val(k, pairs))) for k in block.keys)
            groups.setdefault(key, []).append(pairs)
    if block.is_global and not groups:
        groups[()] = []
    for members in groups.values():
        agg_pairs = []
        for call in block.aggs:
            if call.arg is None:
                values = [0] * len(members)
            else:
                values = [_cell(model, _subst_val(call.arg, p)) for p in members]
            result = _aggregate(call, values)
            if not z3.is_false(call.var.null):
                agg_pairs.append((call.var.null, z3.BoolVal(result is None)))
            value = _value_sort().Num(0) if result is None else _z3_value(
                Fraction(result) if isinstance(result, int) and not isinstance(result, bool) else result
            )
            agg_pairs.append((call.var.val, value))
        all_pairs = (members[0] if members else []) + agg_pairs
        if block.having is not None and not z3.is_true(
            model.eval(_subst(block.having.t, all_pairs), model_completion=True)
        ):
            continue
        rows[tuple(_cell(model, _subst_val(v, all_pairs)) for v in block.outputs)] += 1
    return Counter(set(rows)) if block.distinct else rows


def _hashable(value):
    return ("null",) if value is None else (type(value).__name__, value)


def _aggregate(call: _AggCall, values: list):
    if call.func == "COUNT":
        if call.arg is None:
            return len(values)
        present = [v for v in values if v is not None]
        return len(set(map(_hashable, present))) if call.distinct else len(present)
    if call.func == "COUNTIF":
        return sum(1 for v in values if v is True)
    present = [v for v in values if v is not None]
    if call.distinct:
        present = list({_hashable(v): v for v in present}.values())
    if not present:
        return None
    kinds = {type(v) for v in present}
    if len(kinds) != 1:
        raise _NoCandidate()
    kind = kinds.pop()
    if call.func in ("SUM", "AVG"):
        if kind is not Fraction:
            raise _NoCandidate()
        total = sum(present, Fraction(0))
        return total if call.func == "SUM" else total / len(present)
    if call.func == "MIN":
        return min(present)
    if call.func == "MAX":
        return max(present)
    if call.func in ("LOGICAL_AND", "LOGICAL_OR"):
        if kind is not bool:
            raise _NoCandidate()
        return all(present) if call.func == "LOGICAL_AND" else any(present)
    raise _NoCandidate()


def _export(value):
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else float(value)
    return value


def _block_occs(block) -> list:
    """The table occurrences of ``block`` and of the existence tests nested in it."""

    found = list(block.occs)
    stack = list(block.subs)
    while stack:
        sub = stack.pop()
        found.extend(sub.occs)
        stack.extend(sub.nested)
    return found


def _model_int(model, term) -> int | None:
    value = model.eval(term, model_completion=True)
    return value.as_long() if z3.is_int_value(value) else None


def _find_counterexample(prover: _Prover, left: _Union, right: _Union) -> Counterexample | None:
    blocks = list(left.branches) + list(right.branches)
    all_occs = [o for b in blocks for o in b.occs]
    if any(o.opaque or o.table == _UNNEST_TABLE for o in all_occs) or any(b.subs for b in blocks):
        return None
    for block in blocks:
        prover.witness(block.cond.t, block.occs, block.facts)
    empty = z3.Solver()
    empty.check()
    for assertions, base, occs, values, kinds in [(None, empty.model(), [], [], [])] + prover.candidates:
        pins = [kind == smt_literal_coercion.FLOAT64 for kind in prover.kind_unknowns]
        model = prover._counterexample(list(assertions) + pins if pins else assertions, base, values, kinds) if occs else base
        if any(_model_int(model, kind) in (smt_literal_coercion.NUMERIC, smt_literal_coercion.BIGNUMERIC) for kind in prover.kind_unknowns):
            continue  # this model reads an undeclared column as NUMERIC or BIGNUMERIC; the database is shown as plain numbers
        db: dict[str, list[dict]] = {occ.table: [] for occ in all_occs}
        for occ in occs:
            db.setdefault(occ.table, []).append({name: _cell(model, v, prover.declared_kind(occ.table, name)) for name, v in occ.cols.items()})
        # A column only the other query reads is still a column of the row: give it the model's
        # value (never NULL by default) rather than leaving it to read as NULL.
        other_columns: dict[str, dict] = {}
        for occ in all_occs:
            for name, v in occ.cols.items():
                other_columns.setdefault(occ.table, {}).setdefault(name, v)
        for table, rows in db.items():
            for row in rows:
                for name, v in other_columns.get(table, {}).items():
                    if name not in row:
                        row[name] = _cell(model, v, prover.declared_kind(table, name))
        db = prover.legal_database(db)
        if db is None or not prover.well_typed(db):
            continue  # not a database these tables can hold
        if not prover.real_string_order(model, [cell for rows in db.values() for row in rows for cell in row.values() if isinstance(cell, str)]):
            continue  # the model ranks these strings in an order they do not have
        try:
            left_rows = _eval_union(left, db, model)
            right_rows = _eval_union(right, db, model)
        except _NoCandidate:
            continue
        if left_rows != right_rows:
            tables = {
                table: [{k: _export(v) for k, v in row.items()} for row in rows] for table, rows in db.items()
            }
            return Counterexample(
                tables=tables,
                left_rows=sorted((tuple(_export(v) for v in r) for r in left_rows.elements()), key=repr),
                right_rows=sorted((tuple(_export(v) for v in r) for r in right_rows.elements()), key=repr),
            )
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


_GROUP_SHAPES: dict[str, object] = {}


def _group_shape(key: str):
    """``(table, [(key column, output index)], [(kind, column)])`` for an opaque relation that is one
    ``GROUP BY`` of plain columns over one table with no filter, or ``None``. Kinds: key, max, min,
    sum, count, count* (other outputs are ``(None, None)``)."""

    if key in _GROUP_SHAPES:
        return _GROUP_SHAPES[key]
    shape = None
    try:
        tree = sqlglot.parse_one(key[1:-1], read="bigquery") if key.startswith("(") and key.endswith(")") else None
    except sqlglot.errors.SqlglotError:
        tree = None
    from_ = _from_clause(tree) if isinstance(tree, exp.Select) else None
    group = tree.args.get("group") if from_ is not None else None
    if (
        group is not None
        and isinstance(from_.this, exp.Table)
        and not from_.this.args.get("joins")
        and not any(tree.args.get(k) for k in ("joins", "where", "having", "distinct", "limit", "offset", "qualify", "laterals", "pivots", "with", "with_"))
        and not extended_grouping(group)
        and all(isinstance(g, exp.Column) for g in group.expressions)
    ):
        group_names = {g.name.lower() for g in group.expressions}
        keys, outputs = [], []
        for index, item in enumerate(tree.expressions):
            value = item.this if isinstance(item, exp.Alias) else item
            target = value.this if isinstance(value, exp.AggFunc) else None
            if isinstance(target, exp.Distinct):
                target = target.expressions[0] if len(target.expressions) == 1 and not isinstance(value, exp.Count) else None
            column = target.name.lower() if isinstance(target, exp.Column) else None
            if isinstance(value, exp.Column) and value.name.lower() in group_names:
                keys.append((value.name.lower(), index))
                outputs.append(("key", value.name.lower()))
            elif isinstance(value, exp.Max) and column and not value.args.get("expressions"):
                outputs.append(("max", column))
            elif isinstance(value, exp.Min) and column and not value.args.get("expressions"):
                outputs.append(("min", column))
            elif isinstance(value, exp.Sum) and column:
                outputs.append(("sum", column))
            elif isinstance(value, exp.Count) and isinstance(value.this, exp.Star):
                outputs.append(("count*", None))
            elif isinstance(value, exp.Count) and isinstance(value.this, exp.Column):
                outputs.append(("count", value.this.name.lower()))
            else:
                outputs.append((None, None))
        if {k for k, _ in keys} == group_names:
            shape = (_Compiler._table_key(from_.this), keys, outputs)
    _GROUP_SHAPES[key] = shape
    return shape


_OPAQUE_PROOFS = 6
_nesting = [0]


def _all_occs(unions) -> list[_Occ]:
    found: list[_Occ] = []

    def from_subs(subs):
        for sub in subs:
            found.extend(sub.occs)
            from_subs(getattr(sub, "nested", None) or [])

    for union in unions:
        for branch in union.branches:
            found.extend(branch.occs)
            from_subs(branch.subs)
    return found


def _unify_opaque(compiler: "_Compiler", unions, **settings) -> None:
    """Give two differently written derived relations one identity when they are provably equal.

    A derived table kept whole is identified by its text, so a subquery written with another join
    order, extra IS NOT NULL guards or nested pass-through layers would read as a different relation.
    The bodies are compared by the prover itself; each proven pair shares one table name.
    """

    bodies = compiler.opaque_bodies
    if len(bodies) < 2 or _nesting[0] >= 2:
        return

    def tables(sql: str) -> frozenset:
        tree = sqlglot.parse_one(sql, read="bigquery")
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        return frozenset(t.name.lower() for t in tree.find_all(exp.Table)) - ctes

    representatives: list[str] = []
    mapping: dict[str, str] = {}
    budget = _OPAQUE_PROOFS
    for key, (sql, arity, own) in bodies.items():
        for rep in representatives:
            rep_sql, rep_arity, rep_own = bodies[rep]
            if rep_arity != arity or budget <= 0 or tables(rep_sql) != tables(sql):
                continue
            budget -= 1
            _nesting[0] += 1
            try:
                # The LIMIT-aware entry point: a body may end in ORDER BY .. LIMIT on both sides. The bodies are read
                # in the dialect of the proof they belong to (``_own_dialect_text``), never as BigQuery by default.
                result = prove_equivalent_smt(rep_own, own, compare_names=False, dialect=compiler.dialect, **settings)
            finally:
                _nesting[0] -= 1
            if result.status is SmtStatus.PROVEN_EQUIVALENT:
                mapping[key] = rep
                break
        else:
            representatives.append(key)
    if mapping:
        for occ in _all_occs(unions):
            if occ.opaque and occ.table in mapping:
                occ.table = mapping[occ.table]


def _prove_core(
    left_sql: str,
    right_sql: str,
    *,
    schema: dict[str, list[str]] | None = None,
    exact_arithmetic: bool = False,
    timeout_ms: int = 5000,
    constraints: dict[str, TableConstraints] | None = None,
    compare_names: bool = True,
    dialect: str = "bigquery",
    types: dict[str, dict[str, str]] | None = None,
) -> SmtEquivalenceResult:
    """Prove two BigQuery queries return the same result bag, or refute them.

    ``constraints`` maps table names to ``TableConstraints`` (NOT NULL columns and
    keys) the proof may rely on. ``compare_names=False`` ignores output column
    names, comparing only the rows. ``dialect`` is the sqlglot dialect of the input.

    ``schema`` optionally maps table names (as written in the query, e.g.
    ``"project.dataset.table"``) to column lists; it enables ``SELECT *`` and
    unqualified columns in joins. ``exact_arithmetic`` models ``+``, ``-``
    and ``*`` as exact arithmetic, which is only right for INT64/NUMERIC.
    ``types`` maps table names to declared column types, used for numeric conversions.
    """

    assumptions = BASE_ASSUMPTIONS + ((EXACT_ARITHMETIC_ASSUMPTION,) if exact_arithmetic else ())
    compared = string_number_compare.problem(left_sql, dialect, types, plain_ok=True) or string_number_compare.problem(right_sql, dialect, types, plain_ok=True)
    if compared:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {compared}", assumptions=assumptions)
    if z3 is None:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, Z3_MISSING)
    used = [False]

    def attempt(semijoin: bool, blind: bool = False) -> SmtEquivalenceResult:
        compiler = _Compiler(schema, exact_arithmetic, dialect, types)
        compiler.semijoin = semijoin
        compiler.blind_sets = blind
        try:
            left = compiler.compile(left_sql)
            left_sites = list(compiler.sites)
            right = compiler.compile(right_sql)
            right_sites = compiler.sites[len(left_sites):]
        except Unsupported as error:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {error}", assumptions=assumptions)
        except sqlglot.errors.ParseError as error:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {error}", assumptions=assumptions)
        _unify_opaque(
            compiler,
            (left, right),
            schema=schema,
            exact_arithmetic=exact_arithmetic,
            timeout_ms=timeout_ms,
            constraints=constraints,
            types=types,
        )
        assumed = assumptions + ((LIMIT_SOURCE_ASSUMPTION,) if compiler.limit_opaque else ()) + (
            (WINDOW_SOURCE_ASSUMPTION,) if compiler.window_opaque else ()
        ) + ((NUMERIC_DIFFERENCE_ASSUMPTION,) if compiler.numeric_differences else ()) + (
            (MIXED_NUMERIC_ASSUMPTION,) if compiler.mixed_numeric else ()
        )
        if compiler.timestamp_literals and any(_CANONICAL_DATE.match(t) for t in compiler.string_literals):
            # 'YYYY-MM-DD' sorts before 'YYYY-MM-DD 00:00:00' as text but is the same instant: not modeled together
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "unsupported: dates and timestamps compared in one proof", assumptions=assumed)
        if compiler.big_literals:
            problem = compiler.integer_context_problem()
            if problem:
                return SmtEquivalenceResult(
                    SmtStatus.NOT_PROVEN, f"unsupported: integer literal {compiler.big_literals[0]} is past 2**53 and {problem}", assumptions=assumed
                )
        typed = compiler.typed_facts()
        nothing_floating = dialect == "bigquery" and compiler.integer_context_problem() is None
        if nothing_floating:
            # Every column is declared INT64, NUMERIC, STRING, BOOL or another non-floating type and no expression can produce a
            # FLOAT64: there is no NaN, a SUM is exact in any order, and + - * are exact (an overflow is an error, reported below).
            assumed = tuple(a for a in assumed if a not in (BASE_ASSUMPTIONS[0], BASE_ASSUMPTIONS[2], EXACT_ARITHMETIC_ASSUMPTION))
        elif dialect == "bigquery":
            assumed = compiler.sums.settle(assumed, BASE_ASSUMPTIONS[2])  # an exact SUM, or the same plan on both sides
        order = compiler.order_facts()
        for union in (left, right):
            for block in union.branches:
                extra = order + compiler.typed_facts(_block_occs(block))
                if extra:
                    block.facts = block.facts + extra

        if len(left.names) != len(right.names):
            return SmtEquivalenceResult(
                SmtStatus.NOT_PROVEN,
                f"different column counts ({len(left.names)} vs {len(right.names)})",
                assumptions=assumed,
            )
        for position, (a, b) in enumerate(zip(left.names, right.names), start=1):
            if compare_names and a != b:
                return SmtEquivalenceResult(
                    SmtStatus.NOT_PROVEN,
                    f"column {position} is named {a or '(unnamed)'} on the left and {b or '(unnamed)'} on the right",
                    assumptions=assumed,
                )

        prover = _Prover(timeout_ms, constraints, compiler.types)
        prover.ordered, prover.string_literals = compiler.ordered, compiler.string_literals
        prover.opaque_sets = compiler.opaque_sets
        prover.kind_unknowns = list(compiler.kind_unknowns.values())
        used[0] = compiler.used_setsrc
        try:
            proven, reason = _prove(prover, left, right)
        except _SetSourceUnresolved:
            proven, reason = False, "a derived table is not joined on all of its columns"
        if proven:
            report = smt_errors.compare(prover, left_sites, right_sites, typed) if dialect == "bigquery" else None
            if report is not None and report.verdict == smt_errors.INTRODUCES:
                # Same rows wherever both succeed, but the rewrite fails on a database where the original returns rows.
                return SmtEquivalenceResult(
                    SmtStatus.NOT_PROVEN, report.detail, assumptions=_error_assumptions(assumed, report), errors=report
                )
            return SmtEquivalenceResult(SmtStatus.PROVEN_EQUIVALENT, reason, assumptions=_error_assumptions(assumed, report), errors=report)
        if not compiler.uses_uf:
            if compiler.kind_unknowns:
                # A proof holds whatever the undeclared types are. A counterexample is shown as plain numbers, so it is
                # sought with every undeclared operand read as FLOAT64, where a decimal literal is the nearest double.
                pins = [kind == smt_literal_coercion.FLOAT64 for kind in compiler.kind_unknowns.values()]
                for union in (left, right):
                    for block in union.branches:
                        block.facts = list(block.facts) + pins
            counterexample = _find_counterexample(prover, left, right)
            if counterexample is not None and compiler.bounded_sums and not _small_database(counterexample):
                counterexample = None
            if counterexample is not None:
                return SmtEquivalenceResult(
                    SmtStatus.NOT_EQUIVALENT,
                    "the queries differ on the attached database",
                    counterexample=counterexample,
                    assumptions=assumed,
                )
        if prover.wall_clock:
            reason += " (the solver timed out on some checks)"
        elif prover.unknown:
            reason += " (the solver ran out of its work budget on some checks)"
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, reason, assumptions=assumed)

    result = attempt(True)
    if result.status is not SmtStatus.NOT_PROVEN or not used[0]:
        return result
    # Under DISTINCT the derived table can also be read as a plain join (only a proof is taken from it).
    blind = attempt(True, blind=True)
    if blind.proven:
        return blind
    # A DISTINCT derived table read as a set can fail to match what the opaque
    # reading would; try that reading too before giving up.
    fallback = attempt(False)
    return fallback if fallback.status is not SmtStatus.NOT_PROVEN else result


_SMALL_VALUE = 2**40
_SMALL_ROWS = 2**20


def _small_database(counterexample: Counterexample) -> bool:
    """Whether no sum of column values over this database can leave INT64, NUMERIC or FLOAT64 range."""

    rows = 0
    for table in counterexample.tables.values():
        rows += len(table)
        for row in table:
            for value in row.values():
                if isinstance(value, (int, float, Fraction)) and not isinstance(value, bool) and abs(value) > _SMALL_VALUE:
                    return False
    return rows <= _SMALL_ROWS


ERROR_SAME_ASSUMPTION = (
    "both queries can raise the same runtime errors (BigQuery picks the evaluation order); the proof covers databases where neither does"
)
ERROR_REFINES_ASSUMPTION = (
    "the rewrite raises no runtime error the original cannot; the original may fail where the rewrite returns rows"
)
ERROR_INTRODUCES_ASSUMPTION = "the rewrite can raise a runtime error on a database where the original returns rows"


def _error_assumptions(assumed: tuple, report) -> tuple:
    """``assumed`` with the "runtime errors are not modeled" label replaced by what the error comparison showed."""

    if report is None or BASE_ASSUMPTIONS[1] not in assumed:
        return assumed
    index = assumed.index(BASE_ASSUMPTIONS[1])
    if report.verdict == smt_errors.NONE:
        return assumed[:index] + assumed[index + 1:]
    replacement = {
        smt_errors.SAME: (ERROR_SAME_ASSUMPTION,),
        smt_errors.REFINES: (ERROR_REFINES_ASSUMPTION,),
        smt_errors.INTRODUCES: (BASE_ASSUMPTIONS[1], ERROR_INTRODUCES_ASSUMPTION),
    }.get(report.verdict, (BASE_ASSUMPTIONS[1],))
    return assumed[:index] + replacement + assumed[index + 1:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove two BigQuery queries equivalent with Z3.")
    parser.add_argument("left", help="path to the left SQL file")
    parser.add_argument("right", help="path to the right SQL file")
    parser.add_argument("--schema", help="JSON file mapping table names to column lists")
    parser.add_argument("--exact-arithmetic", action="store_true", help="model + - * exactly (INT64/NUMERIC)")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument(
        "--conditional",
        action="store_true",
        help="when the pair is not proven, look for facts (NOT NULL, unique keys, foreign keys) that would make it equivalent; exit 3",
    )
    parser.add_argument(
        "--explain-difference",
        action="store_true",
        help="when the pair is refuted, add except_when: a verified predicate P such that the queries are equivalent except when P holds",
    )
    args = parser.parse_args(argv)
    with open(args.left, encoding="utf-8") as handle:
        left = handle.read()
    with open(args.right, encoding="utf-8") as handle:
        right = handle.read()
    schema = None
    if args.schema:
        with open(args.schema, encoding="utf-8") as handle:
            schema = json.load(handle)
    from .statement_proof import prove_statements_smt

    result = prove_statements_smt(
        left, right, schema=schema, exact_arithmetic=args.exact_arithmetic, timeout_ms=args.timeout_ms,
        conditional=args.conditional,
    )
    payload = {
        "status": result.status.value,
        "reason": result.reason,
        "assumptions": list(result.assumptions),
    }
    if result.conditions:
        payload["conditions"] = [c.to_json() for c in result.conditions]
    if result.counterexample is not None:
        payload["counterexample"] = {
            "tables": result.counterexample.tables,
            "left_rows": [list(r) for r in result.counterexample.left_rows],
            "right_rows": [list(r) for r in result.counterexample.right_rows],
        }
    if args.explain_difference and result.status is SmtStatus.NOT_EQUIVALENT:
        from .difference_surface import except_when

        options: dict = {"timeout_ms": args.timeout_ms}
        if schema is not None:
            options["schema"] = schema
        found = except_when(left, right, **options)
        if found is not None:
            payload["except_when"] = found
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    if result.conditionally_proven:
        return 3
    return 0 if result.proven else 1


WINDOW_SOURCE_ASSUMPTION = "window functions over the same input with the same text give the same values (ties in ORDER BY resolve alike)"
NUMERIC_DIFFERENCE_ASSUMPTION = "x and y in ABS(x - y) are numbers"
LIMIT_SOURCE_ASSUMPTION = "a LIMIT subquery with the same text returns the same rows each time"
TIE_ASSUMPTION = "rows tied on the ORDER BY are cut by LIMIT the same way for equal inputs"


def _split_limit(sql: str, dialect: str):
    """``(core_sql, spec)`` for a query ending in ORDER BY .. LIMIT, ``(sql, None)`` without a limit.

    ``spec`` is ``(limit, offset, ordering, covers_all, visible)`` (``limit`` ``None`` for ``OFFSET`` alone) where ``ordering`` lists
    ``(output position, descending, nulls first)`` and ``visible`` counts the output columns (order keys that are
    not outputs become extra columns of the core); ``None`` as the core means the
    shape is not handled (``spec`` then says why).
    """

    try:
        tree = check_modeled(canonical_negation(sqlglot.parse_one(sql, read=dialect)))
    except UnmodeledConstruct:
        return sql, None
    root = merge_wrapper_tails(tree)
    if root is None:
        return None, "LIMIT or OFFSET tails stacked in parentheses"
    if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        return sql, None
    limit, offset, order = root.args.get("limit"), root.args.get("offset"), root.args.get("order")
    if (limit is None and offset is None) or _is_limit_zero(root):
        return sql, None
    if order is None:
        return None, "LIMIT or OFFSET without ORDER BY picks arbitrary rows"
    # ORDER BY .. OFFSET m without LIMIT keeps every row after the first m: an unbounded limit.
    if limit is not None and (not isinstance(limit, exp.Limit) or not isinstance(limit.expression, exp.Literal) or limit.expression.is_string or not limit.expression.this.isdigit()):
        return None, "LIMIT is not a constant"
    if offset is not None and (not isinstance(offset.expression, exp.Literal) or offset.expression.is_string or not offset.expression.this.isdigit()):
        return None, "OFFSET is not a constant"
    first = root
    while isinstance(first, (exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        first = first.this
    if not isinstance(first, exp.Select) or any(isinstance(e, exp.Star) for e in first.expressions):
        return None, "ORDER BY with a star select list"
    outputs = [(e.alias_or_name.lower(), (e.this if isinstance(e, exp.Alias) else e).sql()) for e in first.expressions]
    ordering = []
    hidden: list[exp.Expression] = []  # order keys that are not output columns, read as extra columns
    for item in order.expressions:
        if not isinstance(item, exp.Ordered):
            return None, "ORDER BY item"
        key = item.this
        position = None
        if isinstance(key, exp.Literal) and not key.is_string and not key.this.isdigit():
            return None, "ORDER BY a number that is not a position"
        if isinstance(key, exp.Literal) and not key.is_string and 1 <= int(key.this) <= len(outputs):
            position = int(key.this) - 1
        else:
            text = key.sql()
            matches = [
                i
                for i, (name, expr) in enumerate(outputs)
                if (isinstance(key, exp.Column) and not key.table and key.name.lower() == name) or expr == text
            ]
            if len(matches) == 1 or (matches and len({outputs[i][1] for i in matches}) == 1):
                position = matches[0]
        if position is None and root is first and not first.args.get("distinct") and not any(key.find_all(exp.Subquery, exp.Window)):
            hidden.append(key.copy())
            position = len(outputs) + len(hidden) - 1
        if position is None:
            return None, "ORDER BY on an expression that is not an output column"
        if position < len(outputs):
            # Output columns with the same expression hold the same values: name the first one.
            position = next(i for i, (_, expr) in enumerate(outputs) if expr == outputs[position][1])
        desc = bool(item.args.get("desc"))
        nulls_first = item.args.get("nulls_first")
        ordering.append((position, desc, (not desc) if nulls_first is None else bool(nulls_first)))
    covers = {p for p, _, _ in ordering} >= set(range(len(outputs)))
    # A tail that sat on the parentheses was moved onto ``root``; the core is then the merged query.
    innermost = tree
    while isinstance(innermost, exp.Subquery):
        innermost = innermost.this
    core = root.copy() if root is not innermost else tree.copy()
    stripped = core
    while isinstance(stripped, exp.Subquery):
        stripped = stripped.this
    for key in ("limit", "offset", "order"):
        stripped.set(key, None)
    for index, key in enumerate(hidden):
        stripped.set("expressions", list(stripped.expressions) + [exp.alias_(key, f"kq_ord{index}")])
    spec = (
        int(limit.expression.this) if limit is not None else None,
        int(offset.expression.this) if offset is not None else 0,
        tuple(ordering),
        covers,
        len(outputs),
    )
    return faithful_sql(core, dialect), spec


def _check_options(kwargs: dict) -> None:
    """Raise ``TypeError``/``ValueError`` for an option a caller passed wrongly (a bug, not a property of the SQL).

    ``timeout_ms=None`` means the default and a float is rounded down to whole milliseconds; schema, types and
    constraints entries whose names differ only in case and disagree are dropped (:func:`drop_case_conflicts`), in place.
    """

    timeout = kwargs.get("timeout_ms")
    if timeout is None:
        kwargs.pop("timeout_ms", None)
    elif isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout < 2**32:
        raise ValueError(f"timeout_ms must be a positive number of milliseconds, not {timeout!r}")
    else:
        kwargs["timeout_ms"] = int(timeout)
    for name in ("schema", "types", "constraints"):
        value = kwargs.get(name)
        if value is not None and not isinstance(value, dict):
            raise TypeError(f"{name} must map table names to their entries, not {type(value).__name__}")
    schema = kwargs.get("schema") or {}
    for table, columns in schema.items():
        # A column list, or a mapping whose keys are the column names (column -> type).
        if not isinstance(table, str) or isinstance(columns, (str, bytes)) or not all(isinstance(c, str) for c in columns):
            raise TypeError(f"schema entry {table!r} must list column names")
    for table, columns in (kwargs.get("types") or {}).items():
        if not isinstance(table, str) or not isinstance(columns, dict):
            raise TypeError(f"types entry {table!r} must map column names to type names")
    for name in ("schema", "types", "constraints"):
        if kwargs.get(name):
            kwargs[name] = drop_case_conflicts(kwargs[name])


@refuse_misread_proofs
@serialized
def prove_equivalent_smt(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Prove two BigQuery queries return the same result bag, or refute them.

    On MySQL a proof that needs different strings compared with an untyped column to differ must also hold with the strings
    read as the numbers they convert to (``kumosql.numeric_column_reading``).
    See ``_prove_equivalent_smt`` for the rest.
    """

    return numeric_column_reading.checked(
        _prove_equivalent_smt, left_sql, right_sql, kwargs, lambda reason: SmtEquivalenceResult(SmtStatus.NOT_PROVEN, reason)
    )


def _prove_equivalent_smt(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Prove two BigQuery queries return the same result bag, or refute them.

    Input the prover cannot read (untokenizable text, nesting too deep for the compiler) is
    ``not_proven``, never an exception; an option passed wrongly raises ``TypeError``/``ValueError``.
    With ``conditional=True`` a pair that is not proven is retried under facts taken from the
    queries (NOT NULL columns, unique keys, foreign keys); a proof that needs some of them comes back
    as ``PROVEN_CONDITIONALLY`` with the minimal ``conditions`` (see ``kumosql.conditional_equivalence``).
    See ``_prove_with_limit`` for the rest.
    """

    conditional = kwargs.pop("conditional", False)
    wall = kwargs.pop("conditional_seconds", None)
    _check_options(kwargs)
    if kwargs.get("dialect", "bigquery") == "bigquery":
        unknown_type = invalid_type_name(left_sql) or invalid_type_name(right_sql)
        if unknown_type:
            return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: BigQuery would reject the query: {unknown_type}")
    left_sql, right_sql = (string_number_literals.normalize(sql, kwargs.get("dialect", "bigquery"), kwargs.get("types")) for sql in (left_sql, right_sql))
    result = _prove_with_limit(left_sql, right_sql, **kwargs)
    if not conditional or result.status is SmtStatus.PROVEN_EQUIVALENT:
        return result
    from . import conditional_equivalence

    def prove(constraints, pair=None):
        if pair is not None:
            return _prove_with_limit(*pair, **{**kwargs, "constraints": constraints, "compare_names": False})
        return _prove_with_limit(left_sql, right_sql, **{**kwargs, "constraints": constraints})

    options = {} if wall is None else {"wall_seconds": wall}
    return conditional_equivalence.add_conditions(
        left_sql, right_sql, result, prove,
        schema=kwargs.get("schema"), constraints=kwargs.get("constraints"), types=kwargs.get("types"), dialect=kwargs.get("dialect", "bigquery"), **options,
    )


def _prove_with_limit(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    """Prove two BigQuery queries return the same result bag, or refute them.

    A query ending in ``ORDER BY .. LIMIT n [OFFSET m]`` is handled when both sides
    have the same limit, offset and ordering (by output column) and the queries
    without them are equivalent: equal inputs give equal first rows, up to rows
    tied on the ordering, which is recorded in the result's assumptions unless the
    ordering covers every output column.

    See ``_prove_core`` for the options.
    """

    try:
        return _prove_smt(left_sql, right_sql, **kwargs)
    except sqlglot.errors.SqlglotError as error:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"parse error: {str(error)[:200]}")
    except RecursionError:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "unsupported: the query nests too deeply")


def _prove_smt(left_sql: str, right_sql: str, **kwargs) -> SmtEquivalenceResult:
    dialect = kwargs.get("dialect", "bigquery")
    masked = masked_template_problem(left_sql, right_sql, dialect=dialect or "bigquery")
    if masked:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, masked)
    if dialect == "bigquery":
        left_sql, right_sql = canonical_literals(left_sql), canonical_literals(right_sql)
    left_sql, right_sql, problem = positional_sql_pair(left_sql, right_sql, dialect)
    if problem:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: BY NAME set operation ({problem})")
    try:
        left_core, left_spec = _split_limit(left_sql, dialect)
        right_core, right_spec = _split_limit(right_sql, dialect)
    except sqlglot.errors.SqlglotError:
        return _prove_core(left_sql, right_sql, **kwargs)
    if left_spec is None and right_spec is None and left_core is not None and right_core is not None:
        return _prove_core(left_sql, right_sql, **kwargs)
    if left_core is None or right_core is None:
        reason = left_spec if left_core is None else right_spec
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"unsupported: {reason}")
    if left_spec is None or right_spec is None:
        # The limited query may sit inside the other one as a derived table: compare with it wrapped.
        wrapped = [
            sql if spec is None else f"SELECT * FROM ({sql}) AS kq_limited"
            for sql, spec in ((left_sql, left_spec), (right_sql, right_spec))
        ]
        result = _prove_core(wrapped[0], wrapped[1], **kwargs)
        if result.proven:
            return result
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "only one query has a LIMIT")
    if left_spec[:3] != right_spec[:3]:
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "the queries differ in LIMIT, OFFSET or ORDER BY")
    if left_spec[4] != right_spec[4]:
        # An order key that is not an output is an extra column of the core: it must not stand in for an output.
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, f"different column counts ({left_spec[4]} vs {right_spec[4]})")
    result = _prove_core(left_core, right_core, **kwargs)
    if result.status is SmtStatus.NOT_EQUIVALENT:
        # The rows before the cut differ, but the first rows may still agree.
        return SmtEquivalenceResult(SmtStatus.NOT_PROVEN, "the queries differ before the LIMIT", assumptions=result.assumptions)
    if result.proven and not left_spec[3]:
        result = dataclasses.replace(result, assumptions=tuple(result.assumptions) + (TIE_ASSUMPTION,))
    return result
