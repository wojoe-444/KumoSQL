"""Bounded equivalence checking: no counterexample on any database with at most N rows per table.

Both queries are compiled to z3 formulas over a database of ``N`` symbolic rows per table. Every
cell is a symbolic value plus a NULL flag and every row has a presence flag, so the solver
covers every combination of values (and every NULL pattern) up to the bound. Each query
becomes a symbolic relation: a list of rows, each with a presence flag and symbolic cells. The two
relations are compared as bags. ``sat`` is a counterexample; ``unsat`` means "equivalent on every
database of at most N rows per table", which is **not a proof** for larger databases.

This is the approach of VeriEQL (Zhao et al., "VeriEQL: Bounded Equivalence Verification for Complex
SQL Queries with Integrity Constraints", OOPSLA 2024, https://github.com/VeriEQL/VeriEQL): bounded
tables with symbolic tuples, queries as symbolic relations, constraints as extra assertions. The
code here is written from the paper's description and shares none of VeriEQL's source (which is
CC BY-NC-SA 4.0).

Evidence levels
---------------
``bounded`` is its own evidence level, next to an unbounded proof and agreement on executed random
databases. A counterexample is never returned unless it was replayed (both queries executed on
DuckDB over the model's database, difference stable under row shuffles); a model the replay does
not confirm gives ``unknown``, never a verdict.

Assumptions of the encoding (reported with every result): arithmetic is exact (no FLOAT64
rounding or integer overflow), runtime errors are not modeled (division by zero gives NULL), strings
compare case-sensitively by code point, results are compared as bags (row order ignored), and ties in
``ORDER BY`` (``LIMIT``, ``ROW_NUMBER``) are broken by row position.
Anything the encoding does not model raises :class:`Unsupported` and the answer is ``unknown``.
"""

from __future__ import annotations

import datetime as _dt
import random
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Callable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .ast_utils import MAX_EXPANDED_READS, UnmodeledConstruct, distinct_on, drop_case_conflicts, expand_group_by_all, expanded_reads, extended_grouping, is_call, spell_for_duckdb, star_modified
from .duckdb_load import small_database
from .set_operations import positional_sql_pair
from .solver_lock import Z3_MISSING, bounded_solver, serialized
from .type_names import invalid_type_name

try:  # pragma: no cover - exercised through the tests
    import z3
except ImportError:  # pragma: no cover
    z3 = None


class Unsupported(Exception):
    """The query uses something the bounded encoding does not model."""


def _whole(literal: exp.Literal) -> int:
    """The value of a literal used as a position, count or offset; ``1.5`` or ``1e0`` there is not modeled."""

    if not literal.this.isdigit() or len(literal.this) > 18:
        raise Unsupported(f"{literal.this} where a whole number is expected")
    return int(literal.this)


class BoundedStatus(str, Enum):
    BOUNDED_EQUIVALENT = "bounded_equivalent"  # no counterexample within the bound (not a proof)
    DIFFERENT = "different"  # a replayed counterexample
    UNKNOWN = "unknown"  # unsupported, timeout, or an unconfirmed model


ASSUMPTIONS = (
    "bounded: every database with at most N rows per table, not larger ones",
    "arithmetic is exact (no FLOAT64 rounding or INT64 overflow)",
    "runtime errors are not modeled (division by zero gives NULL)",
    "strings compare case-sensitively by code point",
    "results are compared as bags (row order ignored)",
    "ORDER BY ties are broken by row position (a tie-dependent difference is reported only if it survives shuffles)",
)

# --- schema ------------------------------------------------------------------------------------

_INT = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "INT64", "SERIAL", "LONG"}
_REAL = {"FLOAT", "DOUBLE", "FLOAT64", "DECIMAL", "NUMERIC", "NUMBER", "REAL", "BIGNUMERIC", "DEC"}
_STR = {"VARCHAR", "STRING", "TEXT", "CHAR", "ENUM", "CHARACTER", "NVARCHAR", "VARCHAR2"}
_BOOL = {"BOOL", "BOOLEAN"}
_DATE = {"DATE"}
_TIME = {"TIME"}
_DATETIME = {"DATETIME", "TIMESTAMP", "TIMESTAMP_NTZ"}


# Legal values of BigQuery's real types: (decimal digits after the point or None, exclusive bound on |value|).
_REAL_DOMAINS = {
    "NUMERIC": (9, 10**29),
    "BIGNUMERIC": (38, 578960446186580977117854925043439539267),
    "BIGDECIMAL": (38, 578960446186580977117854925043439539267),
    "FLOAT64": (None, 10**308),
}


def _base_type(sql_type: str) -> str:
    return re.split(r"[(\s<]", sql_type.strip().upper(), maxsplit=1)[0]


_DECLARED_DECIMAL = re.compile(r"(NUMERIC|BIGNUMERIC|BIGDECIMAL)\s*\(\s*(\d+)\s*(?:,\s*(\d+))?\s*\)")


def _declared_precision(sql_type: str) -> tuple[int, int] | None:
    """``(precision, scale)`` of a BigQuery-style ``NUMERIC(p, s)`` (scale 0 when omitted), else ``None``.

    ``DECIMAL(p, s)`` keeps its old, unrestricted treatment: constraining it moved bounded verdicts on the QED suite
    (two pairs fell from 3 rows to 2 on a solver timeout), which this change does not set out to do."""

    match = _DECLARED_DECIMAL.fullmatch(sql_type.strip().upper())
    return (int(match.group(2)), int(match.group(3) or 0)) if match else None


def _real_domain(sql_type: str) -> tuple[int | None, int] | None:
    """The legal values of a real column: a declared ``NUMERIC(p, s)`` allows ``s`` decimal digits and
    ``|value| < 10**(p - s)``; a bare type gets its BigQuery default."""

    declared = _declared_precision(sql_type)
    if declared is not None:
        precision, scale = declared
        return scale, 10 ** max(precision - scale, 0)
    return _REAL_DOMAINS.get(_base_type(sql_type))


# the legal encoded values of a DATE (proleptic ordinal) and a DATETIME / TIMESTAMP (seconds from ordinal 0): years 1..9999
_TEMPORAL_DOMAINS = {
    "date": (1, _dt.date.max.toordinal()),
    "datetime": (86400, _dt.date.max.toordinal() * 86400 + 86399),
}


def kind_of(sql_type: str) -> str | None:
    """The encoding kind (int, real, str, bool, date) of a SQL type name; ``None`` if not modeled."""

    base = re.split(r"[(\s<]", sql_type.strip().upper(), maxsplit=1)[0]
    if base in _INT:
        return "int"
    if base in _REAL:
        return "real"
    if base in _STR:
        return "str"
    if base in _BOOL:
        return "bool"
    if base in _DATE:
        return "date"
    if base in _TIME:
        return "time"
    if base in _DATETIME:
        return "datetime"
    return None


@dataclass
class BColumn:
    name: str
    type: str
    not_null: bool = False
    values: tuple = ()  # allowed values of an ENUM column


@dataclass
class BTable:
    name: str
    columns: list[BColumn]
    keys: list[tuple[str, ...]] = field(default_factory=list)
    # (child columns, parent table, parent columns): a row whose columns are all non-NULL has a parent row
    foreign_keys: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(default_factory=list)

    def column(self, name: str) -> BColumn:
        for column in self.columns:
            if column.name.lower() == name.lower():
                return column
        raise KeyError(name)


@dataclass
class BoundedSchema:
    tables: dict[str, BTable]
    # extra constraints: each gets the symbolic database and returns z3 booleans that must hold
    extra: list[Callable[["SymbolicDatabase"], list]] = field(default_factory=list)

    def lookup(self, table: exp.Table) -> BTable | None:
        """The table a reference names: by its full dotted name, else by a name that ends the same way."""

        full = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
        for key, value in self.tables.items():
            if key.lower() == full:
                return value
        suffixed = [v for k, v in self.tables.items() if k.lower().endswith("." + full)]
        if len(suffixed) == 1:
            return suffixed[0]
        if suffixed:
            return None  # a spelling shared by two tables names neither
        plain = [v for k, v in self.tables.items() if k.lower() == table.name.lower()]
        return plain[0] if len(plain) == 1 and not (table.catalog or table.db) else None


def schema_from_prover(schema: Mapping[str, Sequence[str]], constraints=None, types=None) -> BoundedSchema:
    """A :class:`BoundedSchema` from the prover's schema, ``TableConstraints`` and column types."""

    # Tables whose names differ only in case and disagree are dropped, as in the SMT and algebraic provers
    # (otherwise both reach the DuckDB replay, which rejects the second as a duplicate table).
    schema = drop_case_conflicts(dict(schema))
    constraints = drop_case_conflicts(dict(constraints or {}))
    types = drop_case_conflicts(dict(types or {}))
    tables: dict[str, BTable] = {}
    for name, columns in schema.items():
        if name.lower() in {t.lower() for t in tables}:
            continue  # an entry that agrees with an earlier one up to case: one table, not a duplicate
        constraint = constraints.get(name)
        column_types = {k.lower(): v for k, v in (types.get(name) or {}).items()}
        not_null = {c.lower() for c in (constraint.not_null if constraint else ())}
        # a key's columns are NOT NULL (``TableConstraints.keys``: a primary key, or UNIQUE over NOT NULL columns)
        not_null |= {c.lower() for key in (constraint.keys if constraint else ()) for c in key}
        table = BTable(
            name,
            [BColumn(c, column_types.get(c.lower(), "UNKNOWN"), c.lower() in not_null) for c in columns],
            keys=[tuple(k) for k in (constraint.keys if constraint else ())],
        )
        for cols, parent, pcols in (constraint.foreign_keys if constraint else ()):
            table.foreign_keys.append((tuple(cols), parent, tuple(pcols)))
        tables[name] = table
    return BoundedSchema(tables)


def schema_from_bigquery(tables) -> BoundedSchema:
    """A bounded schema from saved BigQuery metadata: ``(project, dataset, table, {"schema": [...], "constraints": ...})``.

    Column types, REQUIRED columns, the primary key and foreign keys become facts; a column whose type or
    mode the encoding does not model (BYTES, JSON, STRUCT, REPEATED) fails only if a query reads it.
    """

    out: dict[str, BTable] = {}
    for project, dataset, name, data in tables:
        fields = [f for f in (data.get("schema") or []) if isinstance(f, Mapping) and f.get("name")]
        if not fields:
            continue
        key = f"{project}.{dataset}.{name}".lower()
        columns = []
        for field_ in fields:
            kind = str(field_.get("type", "")).upper()
            repeated = str(field_.get("mode", "")).upper() == "REPEATED"
            columns.append(BColumn(field_["name"].lower(), "UNSUPPORTED" if repeated else kind, str(field_.get("mode", "")).upper() == "REQUIRED"))
        table = BTable(key, columns)
        constraints = data.get("constraints")
        if isinstance(constraints, Mapping):
            primary = [c.lower() for c in (constraints.get("primaryKey") or {}).get("columns") or []]
            names = {c.name for c in columns}
            if primary and all(c in names for c in primary):
                table.keys.append(tuple(primary))
                for c in primary:
                    table.column(c).not_null = True
            for fk in constraints.get("foreignKeys") or []:
                ref = fk.get("referencedTable") or {}
                refs = fk.get("columnReferences") or []
                if ref.get("tableId") and refs:
                    parent = ".".join(str(ref[k]) for k in ("projectId", "datasetId", "tableId") if ref.get(k)).lower()
                    table.foreign_keys.append((tuple(r["referencingColumn"].lower() for r in refs), parent, tuple(r["referencedColumn"].lower() for r in refs)))
        out[key] = table
    for table in out.values():  # keep only keys whose parent table is known and whose columns exist
        table.foreign_keys = [
            fk for fk in table.foreign_keys
            if fk[1] in out and all(c in {x.name for x in table.columns} for c in fk[0]) and all(c in {x.name for x in out[fk[1]].columns} for c in fk[2])
        ]
    return BoundedSchema(out)


# --- symbolic values ---------------------------------------------------------------------------

_SORTS: dict = {}


def _sort(kind: str):
    if kind in ("int", "date", "time", "datetime"):
        return z3.IntSort()
    if kind == "real":
        return z3.RealSort()
    if kind == "str":
        return z3.StringSort()
    if kind == "bool":
        return z3.BoolSort()
    raise Unsupported(f"type {kind}")


def _default(kind: str):
    return {
        "int": z3.IntVal(0),
        "date": z3.IntVal(0),
        "time": z3.IntVal(0),
        "datetime": z3.IntVal(0),
        "real": z3.RealVal(0),
        "str": z3.StringVal(""),
        "bool": z3.BoolVal(False),
        "null": z3.IntVal(0),
    }[kind]


@dataclass(frozen=True)
class V:
    """A symbolic SQL value: ``val`` is meaningful only when ``null`` is false."""

    kind: str  # int, real, str, bool, date or null (the NULL literal)
    val: object
    null: object
    lit: object = None  # the Python constant, when the value is a literal


def _true():
    return z3.BoolVal(True)


def _false():
    return z3.BoolVal(False)


NULL = None  # set lazily (needs z3 context)


def null_value(kind: str = "null") -> V:
    return V(kind, _default(kind), _true())


def const(value, kind: str) -> V:
    if kind == "int":
        return V("int", z3.IntVal(value), _false(), value)
    if kind == "real":
        return V("real", z3.RealVal(Fraction(value).limit_denominator(10**12) if not isinstance(value, Fraction) else value), _false(), value)
    if kind == "str":
        return V("str", z3.StringVal(value), _false(), value)
    if kind == "bool":
        return V("bool", z3.BoolVal(value), _false(), value)
    if kind == "date":
        return V("date", z3.IntVal(value.toordinal()), _false(), value)
    if kind == "time":
        return V("time", z3.IntVal(value.hour * 3600 + value.minute * 60 + value.second), _false(), value)
    if kind == "datetime":
        return V("datetime", z3.IntVal(_seconds(value)), _false(), value)
    raise Unsupported(kind)


def _seconds(value: _dt.datetime) -> int:
    return (value.toordinal() * 86400) + value.hour * 3600 + value.minute * 60 + value.second


def truth(v: V):
    """When a value counts as true in WHERE / ON / HAVING (NULL does not)."""

    if v.kind == "null":
        return _false()
    if v.kind == "bool":
        return z3.And(v.val, z3.Not(v.null))
    if v.kind in ("int", "real"):
        return z3.And(v.val != 0, z3.Not(v.null))
    raise Unsupported(f"{v.kind} used as a condition")


def falsity(v: V):
    if v.kind == "null":
        return _false()
    if v.kind == "bool":
        return z3.And(z3.Not(v.val), z3.Not(v.null))
    if v.kind in ("int", "real"):
        return z3.And(v.val == 0, z3.Not(v.null))
    raise Unsupported(f"{v.kind} used as a condition")


def from_tf(t, f) -> V:
    return V("bool", t, z3.Not(z3.Or(t, f)))


def to_kind(v: V, kind: str) -> V:
    if v.kind == kind:
        return v
    if v.kind == "null":
        return null_value(kind)
    if kind == "real" and v.kind == "int":
        return V("real", z3.ToReal(v.val), v.null, v.lit)
    if kind in ("int", "real") and v.kind == "bool":
        number = z3.If(v.val, z3.IntVal(1), z3.IntVal(0))
        return V(kind, z3.ToReal(number) if kind == "real" else number, v.null)
    if kind in ("date", "time", "datetime") and v.kind == "str" and isinstance(v.lit, str):
        try:
            if kind == "date":
                return const(_dt.date.fromisoformat(v.lit), "date")
            if kind == "time":
                return const(_dt.time.fromisoformat(v.lit if len(v.lit) > 5 else v.lit + ":00"), "time")
            return const(_dt.datetime.fromisoformat(v.lit), "datetime")
        except ValueError:
            raise Unsupported("unparseable date or time literal")
    raise Unsupported(f"cannot treat {v.kind} as {kind}")


def unify(a: V, b: V) -> tuple[V, V]:
    if a.kind == b.kind:
        if a.kind == "null":
            return a, b
        return a, b
    if a.kind == "null":
        return null_value(b.kind), b
    if b.kind == "null":
        return a, null_value(a.kind)
    kinds = {a.kind, b.kind}
    if kinds <= {"int", "real", "bool"}:
        target = "real" if "real" in kinds else "int"
        return to_kind(a, target), to_kind(b, target)
    for temporal in ("date", "time", "datetime"):
        if kinds == {temporal, "str"}:
            return to_kind(a, temporal), to_kind(b, temporal)
    raise Unsupported(f"cannot compare {a.kind} with {b.kind}")


def same(a: V, b: V):
    """Null-safe equality (NULLs equal each other): row identity for DISTINCT, GROUP BY and bags."""

    a, b = unify(a, b)
    if a.kind == "null":
        return _true()
    return z3.Or(z3.And(a.null, b.null), z3.And(z3.Not(a.null), z3.Not(b.null), a.val == b.val))


def _ordered(v: V) -> V:
    return to_kind(v, "int") if v.kind == "bool" else v


def compare(op: str, a: V, b: V) -> V:
    a, b = unify(a, b)
    a, b = _ordered(a), _ordered(b)
    if a.kind == "null":
        return null_value("bool")
    result = {
        "eq": lambda: a.val == b.val,
        "neq": lambda: a.val != b.val,
        "lt": lambda: a.val < b.val,
        "lte": lambda: a.val <= b.val,
        "gt": lambda: a.val > b.val,
        "gte": lambda: a.val >= b.val,
    }[op]()
    return V("bool", result, z3.Or(a.null, b.null))


def logical_and(values: Sequence[V]) -> V:
    t = z3.And(*[truth(v) for v in values]) if values else _true()
    f = z3.Or(*[falsity(v) for v in values]) if values else _false()
    return from_tf(t, f)


def logical_or(values: Sequence[V]) -> V:
    t = z3.Or(*[truth(v) for v in values]) if values else _false()
    f = z3.And(*[falsity(v) for v in values]) if values else _true()
    return from_tf(t, f)


def logical_not(v: V) -> V:
    return from_tf(falsity(v), truth(v))


def _lift(op: Callable, a: V, b: V, kind: str) -> V:
    return V(kind, op(a.val, b.val), z3.Or(a.null, b.null))


def arithmetic(op: str, a: V, b: V) -> V:
    a, b = unify(a, b)
    if a.kind == "null":
        return null_value("int")
    if a.kind not in ("int", "real"):
        raise Unsupported(f"arithmetic on {a.kind}")
    if op == "add":
        return _lift(lambda x, y: x + y, a, b, a.kind)
    if op == "sub":
        return _lift(lambda x, y: x - y, a, b, a.kind)
    if op == "mul":
        return _lift(lambda x, y: x * y, a, b, a.kind)
    zero = b.val == 0
    if op == "div":
        a, b = to_kind(a, "real"), to_kind(b, "real")
        return V("real", z3.If(b.val == 0, z3.RealVal(0), a.val / z3.If(b.val == 0, z3.RealVal(1), b.val)), z3.Or(a.null, b.null, b.val == 0))
    if a.kind != "int":
        raise Unsupported(f"{op} on non-integers")
    safe = z3.If(zero, z3.IntVal(1), b.val)
    magnitude = z3.If(a.val >= 0, a.val, -a.val) / z3.If(safe >= 0, safe, -safe)
    if op == "intdiv":  # truncates toward zero
        negative = (a.val >= 0) != (safe >= 0)
        return V("int", z3.If(negative, -magnitude, magnitude), z3.Or(a.null, b.null, zero))
    if op == "mod":  # the sign follows the dividend
        remainder = z3.If(a.val >= 0, a.val, -a.val) % z3.If(safe >= 0, safe, -safe)
        return V("int", z3.If(a.val >= 0, remainder, -remainder), z3.Or(a.null, b.null, zero))
    raise Unsupported(op)


def civil(ordinal):
    """``(year, month, day)`` of a proleptic Gregorian date given as Python's ordinal (1 is 0001-01-01), as z3 integers."""

    z = ordinal - 719163 + 719468
    era = z / 146097
    doe = z - era * 146097
    yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365
    doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
    mp = (5 * doy + 2) / 153
    day = doy - (153 * mp + 2) / 5 + 1
    month = z3.If(mp < 10, mp + 3, mp - 9)
    year = yoe + era * 400 + z3.If(month <= 2, 1, 0)
    return year, month, day


# --- relations ---------------------------------------------------------------------------------


@dataclass
class Row:
    present: object
    vals: list[V]
    keys: list | None = None  # ORDER BY keys: (V, descending)


@dataclass
class Rel:
    cols: list[tuple[str | None, str]]  # (qualifier, name), lower case
    kinds: list[str]
    rows: list[Row]
    hidden: frozenset = frozenset()  # column positions merged away by USING / NATURAL (still reachable by qualifier)
    first: tuple = ()  # positions a star lists first (the USING columns)


class Scope:
    def __init__(self, cols, parent=None, aliases=None, group=None, alias_first=False, hidden=(), window=None, index=None, picker=None, group_keys=None):
        self.picker = picker  # position in ``cols`` -> the value of an ungrouped column (an arbitrary member of the group)
        self.group_keys = group_keys  # SQL text of each GROUP BY expression -> its value
        self.window = window  # WindowCtx of the select being projected
        self.index = index  # position of this row in the window's rows
        self.cols = cols  # list of (qualifier, name, V)
        self.hidden = frozenset(hidden)  # ids of values reachable only through a qualifier
        self.parent = parent
        self.aliases = aliases or {}  # name -> V or thunk
        self.group = group
        self.alias_first = alias_first
        self._resolved: dict[str, V] = {}

    def lookup(self, table: str | None, name: str) -> V:
        if self.alias_first and table is None and name in self.aliases:
            return self._alias(name)
        found = [(i, v) for i, (q, n, v) in enumerate(self.cols) if n == name and (table is None or q == table) and (table is not None or id(v) not in self.hidden)]
        if len(found) > 1 and not all(f[1] is found[0][1] for f in found):
            raise Unsupported(f"ambiguous column {name}")
        if found:
            position, value = found[0]
            return self.picker(position) if self.picker is not None else value
        if table is None and name in self.aliases:
            return self._alias(name)
        if self.parent is not None:
            return self.parent.lookup(table, name)
        raise Unsupported(f"unknown column {table + '.' if table else ''}{name}")

    def qualifier_values(self, name: str) -> list[V] | None:
        """The cells of the row bound to table alias ``name`` (a row passed to an uninterpreted predicate)."""

        found = [v for (q, n, v) in self.cols if q == name]
        if found:
            return found
        return self.parent.qualifier_values(name) if self.parent is not None else None

    def _alias(self, name: str) -> V:
        if name not in self._resolved:
            entry = self.aliases[name]
            self._resolved[name] = entry() if callable(entry) else entry
        return self._resolved[name]


@dataclass
class WindowCtx:
    rows: list  # the source rows
    scopes: list  # one scope per source row
    cache: dict = field(default_factory=dict)


@dataclass
class Group:
    members: list  # z3 booleans: row j belongs to the current group
    scopes: list[Scope]
    cache: dict = field(default_factory=dict)


AGGREGATES = (exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg)


class SymbolicDatabase:
    """``rows`` symbolic rows per table plus the constraints every database must satisfy."""

    def __init__(self, schema: BoundedSchema, rows: int | Mapping[str, int], *, restrict: Sequence[str] = ()):
        self.schema = schema
        self.rows = rows
        self.restrict = tuple(restrict)  # extra restrictions on models (see ``_confirmed``)
        self.tables: dict[str, list[Row]] = {}
        self.constraints: list = []
        self.predicates: dict = {}  # uninterpreted predicates (VeriEQL's symbolic predicates)
        self.supported: dict[str, set[str]] = {}
        for name, table in schema.tables.items():
            count = rows if isinstance(rows, int) else rows.get(name, 2)
            slots = []
            for slot in range(count):
                present = z3.Bool(f"{name}#{slot}#p")
                vals = []
                for column in table.columns:
                    kind = kind_of(column.type)
                    label = f"{name}#{slot}#{column.name}"
                    if kind is None:
                        vals.append(V("unsupported", z3.IntVal(0), _false(), column.type))
                        continue
                    value = z3.Const(label, _sort(kind))
                    null = _false() if column.not_null else z3.Bool(label + "#null")
                    vals.append(V(kind, value, null))
                    if column.values:
                        self.constraints.append(z3.Or(z3.Not(present), null, *[value == _const_of(v, kind) for v in column.values]) if kind == "str" else _true())
                    if "printable" in self.restrict and kind == "str":
                        self.constraints.append(z3.InRe(value, z3.Star(z3.Range(" ", "~"))))
                    if "decimal" in self.restrict and kind == "real":
                        self.constraints.append(z3.IsInt(value * 4))
                    span = _TEMPORAL_DOMAINS.get(kind)
                    if span is not None:  # keep models inside the range a decoded value can show (no clamping)
                        low, high = span
                        self.constraints.append(z3.Or(z3.Not(present), null, z3.And(value >= low, value <= high)))
                    domain = _real_domain(column.type) if kind == "real" else None
                    if domain is not None:
                        digits, bound = domain
                        legal = [value < z3.RealVal(bound), value > -z3.RealVal(bound)]
                        if digits is not None:
                            legal.append(z3.IsInt(value * 10**digits))
                        self.constraints.append(z3.Or(z3.Not(present), null, z3.And(*legal)))
                slots.append(Row(present, vals))
            self.tables[name] = slots
            for left, right in zip(slots, slots[1:]):  # rows fill from the front: fewer symmetric models
                self.constraints.append(z3.Implies(right.present, left.present))
        for name, table in schema.tables.items():
            slots = self.tables[name]
            index = {c.name.lower(): i for i, c in enumerate(table.columns)}
            for key in table.keys:
                positions = [index[c.lower()] for c in key]
                for i, a in enumerate(slots):
                    for b in slots[i + 1 :]:
                        collide = [z3.Not(a.vals[p].null) for p in positions] + [z3.Not(b.vals[p].null) for p in positions]
                        collide += [a.vals[p].val == b.vals[p].val for p in positions]
                        self.constraints.append(z3.Not(z3.And(a.present, b.present, *collide)))
            for cols, parent, pcols in table.foreign_keys:
                parent_table = schema.tables.get(parent) or next((t for k, t in schema.tables.items() if k.lower() == parent.lower()), None)
                if parent_table is None:
                    continue
                parent_slots = self.tables[parent_table.name]
                pindex = {c.name.lower(): i for i, c in enumerate(parent_table.columns)}
                for child in slots:
                    needs = [child.present] + [z3.Not(child.vals[index[c.lower()]].null) for c in cols]
                    options = []
                    for candidate in parent_slots:
                        # a NULL parent value matches nothing, whatever value hides behind it
                        options.append(z3.And(candidate.present, *[
                            z3.And(
                                z3.Not(candidate.vals[pindex[pc.lower()]].null),
                                candidate.vals[pindex[pc.lower()]].val == child.vals[index[c.lower()]].val,
                            )
                            for c, pc in zip(cols, pcols)
                        ]))
                    self.constraints.append(z3.Implies(z3.And(*needs), z3.Or(*options) if options else _false()))
        for extra in schema.extra:
            self.constraints.extend(extra(self))

    def table(self, name: str) -> tuple[BTable, list[Row]]:
        for key, table in self.schema.tables.items():
            if key == name:
                return table, self.tables[key]
        raise KeyError(name)

    def table_for(self, node: exp.Table) -> tuple[BTable, list[Row]]:
        found = self.schema.lookup(node)
        if found is None:
            raise Unsupported(f"table {node.name} is not in the schema")
        return found, self.tables[found.name]


def _const_of(value, kind: str):
    return const(value, kind).val


MAX_ROWS = 600  # relation size cap: joins multiply


class Compiler:
    """Compile SELECT statements into symbolic relations over a :class:`SymbolicDatabase`."""

    def __init__(self, database: SymbolicDatabase, dialect: str = "bigquery", *, nulls_first: bool | None = None, group_constants: bool = False):
        self.db = database
        self.group_constants = group_constants  # Calcite reads a number in GROUP BY as a constant, not an ordinal
        self.dialect = dialect
        # where NULL sorts under ASC: MySQL and BigQuery put it first, DuckDB and PostgreSQL last
        self.nulls_first = nulls_first if nulls_first is not None else dialect in ("mysql", "bigquery", "spark", "hive")
        self.side_conditions: list = []
        self._picks = 0
        self._ctes: list[dict] = [{}]

    # -- entry points --------------------------------------------------------------------------

    def compile(self, sql: str) -> Rel:
        tree = sqlglot.parse_one(sql, read=self.dialect)
        try:
            tree = expand_group_by_all(tree)
        except UnmodeledConstruct as error:
            raise Unsupported(str(error)) from None
        if expanded_reads(tree) > MAX_EXPANDED_READS:
            raise Unsupported(f"the query reads more than {MAX_EXPANDED_READS} tables once its CTEs are expanded")
        return self.query(tree, None)

    def query(self, node: exp.Expression, outer: Scope | None) -> Rel:
        if isinstance(node, exp.Subquery):
            inner = self.query(node.this, outer)
            return inner
        if isinstance(node, exp.Select):
            return self._with(node, lambda: self.select(node, outer))
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            return self._with(node, lambda: self.set_operation(node, outer))
        raise Unsupported(f"statement {type(node).__name__}")

    def _with(self, node, run):
        clause = node.args.get("with_") or node.args.get("with")
        if clause is None:
            return run()
        if clause.args.get("recursive"):
            raise Unsupported("recursive CTE")
        frame = dict(self._ctes[-1])
        self._ctes.append(frame)
        try:
            for cte in clause.expressions:
                columns = [c.name.lower() for c in cte.args["alias"].args.get("columns") or []]
                frame[cte.alias.lower()] = (cte.this, columns, dict(frame))
            return run()
        finally:
            self._ctes.pop()

    # -- set operations ------------------------------------------------------------------------

    def set_operation(self, node, outer: Scope | None) -> Rel:
        # Branches are matched by position. BY NAME / CORRESPONDING (made positional by
        # ``positional_sql_pair`` before compiling), an outer or inner mode, and a LIMIT or
        # OFFSET of the operation itself are not modeled; ORDER BY alone keeps the bag.
        modifiers = [k for k, v in node.args.items() if v and k not in ("this", "expression", "distinct", "with_", "with", "order")]
        if modifiers:
            raise Unsupported(f"set operation with {', '.join(sorted(modifiers))}")
        left = self.query(node.this, outer)
        right = self.query(node.expression, outer)
        if len(left.cols) != len(right.cols):
            raise Unsupported("set operation column counts differ")
        kinds = []
        for a, b in zip(left.kinds, right.kinds):
            kinds.append(unify(null_value(a) if a != "null" else null_value(), null_value(b) if b != "null" else null_value())[0].kind)
        left_rows = [Row(r.present, [to_kind(v, k) for v, k in zip(r.vals, kinds)]) for r in left.rows]
        right_rows = [Row(r.present, [to_kind(v, k) for v, k in zip(r.vals, kinds)]) for r in right.rows]
        distinct = bool(node.args.get("distinct", True))
        cols = [(None, n) for _, n in left.cols]
        if isinstance(node, exp.Union):
            rows = left_rows + right_rows
            rel = Rel(cols, kinds, rows)
            return self.dedupe(rel) if distinct else rel
        matches = lambda row, rows: [z3.And(o.present, *[same(a, b) for a, b in zip(row.vals, o.vals)]) for o in rows]
        out = []
        if distinct:
            first_left = self.dedupe(Rel(cols, kinds, left_rows))
            for row in first_left.rows:
                found = z3.Or(*matches(row, right_rows)) if right_rows else _false()
                keep = found if isinstance(node, exp.Intersect) else z3.Not(found)
                out.append(Row(z3.And(row.present, keep), row.vals))
        else:
            for i, row in enumerate(left_rows):
                earlier = [z3.If(z3.And(left_rows[k].present, *[same(a, b) for a, b in zip(row.vals, left_rows[k].vals)]), 1, 0) for k in range(i)]
                ordinal = z3.Sum(*earlier) + 1 if earlier else z3.IntVal(1)
                count = z3.Sum(*[z3.If(m, 1, 0) for m in matches(row, right_rows)]) if right_rows else z3.IntVal(0)
                keep = ordinal <= count if isinstance(node, exp.Intersect) else ordinal > count
                out.append(Row(z3.And(row.present, keep), row.vals))
        return Rel(cols, kinds, out)

    def dedupe(self, rel: Rel) -> Rel:
        out = []
        for i, row in enumerate(rel.rows):
            clash = [z3.And(rel.rows[k].present, *[same(a, b) for a, b in zip(row.vals, rel.rows[k].vals)]) for k in range(i)]
            out.append(Row(z3.And(row.present, z3.Not(z3.Or(*clash))) if clash else row.present, row.vals, row.keys))
        return Rel(rel.cols, rel.kinds, out)

    # -- SELECT --------------------------------------------------------------------------------

    def select(self, node: exp.Select, outer: Scope | None) -> Rel:
        if node.args.get("qualify") or node.args.get("windows"):
            raise Unsupported("window clause")
        if distinct_on(node):
            raise Unsupported("DISTINCT ON")
        source = self.from_clause(node, outer)
        where = node.args.get("where")
        rows = source.rows
        if where is not None:
            filtered = []
            for row in rows:
                scope = self._scope(source, row, outer)
                filtered.append(Row(z3.And(row.present, truth(self.expr(where.this, scope))), row.vals))
            rows = filtered
        source = Rel(source.cols, source.kinds, rows, source.hidden, source.first)
        items = list(node.expressions)
        aggregated = self._aggregated(node)
        if self._windowed(node) and (aggregated or node.args.get("group") is not None):
            raise Unsupported("window function over a grouped query")
        if node.args.get("group") is not None or aggregated:
            out = self.grouped(node, source, items, outer)
        else:
            out = self.projected(node, source, items, outer)
        if node.args.get("distinct") is not None:
            out = self.dedupe(out)
        return self.limited(node, out)

    def _windowed(self, node) -> bool:
        return any(True for _ in node.find_all(exp.Window))

    def _aggregated(self, node: exp.Select) -> bool:
        targets = list(node.expressions) + [node.args[k] for k in ("having",) if node.args.get(k) is not None]
        order = node.args.get("order")
        if order is not None:
            targets.append(order)
        for target in targets:
            for found in target.find_all(*AGGREGATES, exp.GroupConcat, exp.AnyValue, exp.ArrayAgg):
                if found.find_ancestor(exp.Select) is node and found.find_ancestor(exp.Window) is None:
                    return True
        return False

    def _columns(self, rel: Rel, row: Row):
        return [(q, n, v) for (q, n), v in zip(rel.cols, row.vals)]

    def _scope(self, rel: Rel, row: Row, outer) -> Scope:
        return Scope(self._columns(rel, row), outer, hidden=[id(row.vals[i]) for i in rel.hidden])

    def from_clause(self, node: exp.Select, outer: Scope | None) -> Rel:
        clause = node.args.get("from_") or node.args.get("from")
        if clause is None:
            if node.args.get("joins"):
                raise Unsupported("joins without FROM")
            return Rel([], [], [Row(_true(), [])])
        rel = self.table_factor(clause.this, outer)
        for join in node.args.get("joins") or []:
            rel = self.join(rel, join, outer)
        return rel

    def table_factor(self, node, outer: Scope | None) -> Rel:
        if isinstance(node, exp.Table):
            alias = node.alias.lower() if node.alias else node.name.lower()
            name = node.name.lower()
            if not node.db and not node.catalog and name in self._ctes[-1]:
                select, columns, frame = self._ctes[-1][name]
                self._ctes.append(frame)
                try:
                    inner = self.query(select, None)
                finally:
                    self._ctes.pop()
                return self._renamed(inner, alias, columns or None, node)
            if isinstance(node.this, exp.Func) or node.args.get("joins") is not None and False:
                raise Unsupported("table function")
            table, slots = self.db.table_for(node)
            cols = [(alias, c.name.lower()) for c in table.columns]
            kinds = [kind_of(c.type) or "unsupported" for c in table.columns]
            return Rel(cols, kinds, [Row(r.present, list(r.vals)) for r in slots])
        if isinstance(node, exp.Subquery):
            inner = self.query(node.this, outer)
            alias = node.alias.lower() if node.alias else None
            columns = [c.name.lower() for c in node.args["alias"].args.get("columns") or []] if node.args.get("alias") else None
            return self._renamed(inner, alias, columns or None, node)
        if isinstance(node, exp.Paren):
            return self.table_factor(node.this, outer)
        if isinstance(node, exp.Values):
            return self._values(node, outer)
        raise Unsupported(f"FROM item {type(node).__name__}")

    def _values(self, node: exp.Values, outer: Scope | None) -> Rel:
        rows = []
        for row in node.expressions:
            cells = row.expressions if isinstance(row, exp.Tuple) else [row]
            rows.append(Row(_true(), [self.expr(c, Scope([], outer)) for c in cells]))
        if not rows:
            raise Unsupported("empty VALUES")
        alias = node.args.get("alias")
        width = len(rows[0].vals)
        names = [c.name.lower() for c in alias.args.get("columns") or []] if alias is not None else []
        if len(names) != width:
            names = [f"col{i}" for i in range(width)]
        rel = self._finish([(None, n) for n in names], rows)
        qualifier = alias.name.lower() if alias is not None and alias.name else None
        return Rel([(qualifier, n) for _, n in rel.cols], rel.kinds, rel.rows)

    def _renamed(self, inner: Rel, alias: str | None, columns, node) -> Rel:
        if node.args.get("pivots") or node.args.get("sample"):
            raise Unsupported("pivot or sample")
        names = columns or [n for _, n in inner.cols]
        return Rel([(alias, n) for n in names], list(inner.kinds), [Row(r.present, r.vals) for r in inner.rows])

    def _lateral(self, left: Rel, join: exp.Join, outer: Scope | None) -> Rel:
        """``JOIN LATERAL (subquery)``: the subquery is compiled once per left row, seeing that row's columns."""

        lateral = join.this
        side = (join.side or "").upper()
        if lateral.args.get("view") or lateral.args.get("outer") or side not in ("", "LEFT") or (join.kind or "").upper() not in ("", "CROSS", "INNER"):
            raise Unsupported("this LATERAL form")
        subquery = lateral.this
        if not isinstance(subquery, (exp.Subquery, exp.Select)):
            raise Unsupported("LATERAL over a function")
        alias = lateral.args.get("alias")
        qualifier = alias.name.lower() if alias is not None and alias.name else None
        renamed = [c.name.lower() for c in alias.args.get("columns") or []] if alias is not None else []
        on = join.args.get("on")
        blocks, cols, kinds = [], None, None
        rows: list[Row] = []
        for a in left.rows:
            scope = self._scope(left, a, outer)
            inner = self.query(subquery, scope)
            names = renamed or [n for _, n in inner.cols]
            cols = left.cols + [(qualifier, n) for n in names]
            kinds = left.kinds + list(inner.kinds)
            matched = []
            for b in inner.rows:
                joined = a.vals + b.vals
                condition = _true()
                if on is not None:
                    condition = truth(self.expr(on, Scope([(q, n, v) for (q, n), v in zip(cols, joined)], outer, hidden=[id(joined[i]) for i in left.hidden])))
                rows.append(Row(z3.And(a.present, b.present, condition), joined))
                matched.append(z3.And(b.present, condition))
            if side == "LEFT":
                blanks = [null_value(k) for k in inner.kinds]
                rows.append(Row(z3.And(a.present, z3.Not(z3.Or(*matched)) if matched else _true()), a.vals + blanks))
        if len(rows) > MAX_ROWS:
            raise Unsupported("join too large for the bound")
        if cols is None:
            raise Unsupported("empty left side")
        return Rel(cols, kinds, rows, left.hidden, left.first)

    def join(self, left: Rel, join: exp.Join, outer: Scope | None) -> Rel:
        if isinstance(join.this, exp.Lateral):
            return self._lateral(left, join, outer)
        right = self.table_factor(join.this, outer)
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        if kind in ("ANTI", "SEMI"):
            return self._semi_join(left, right, join, kind, outer)
        natural = bool(join.args.get("method")) or kind == "NATURAL"
        on = join.args.get("on")
        using = [u.name.lower() for u in join.args.get("using") or []]
        if natural:
            visible_right = {n for i, (q, n) in enumerate(right.cols) if i not in right.hidden}
            using = [n for i, (q, n) in enumerate(left.cols) if i not in left.hidden and n in visible_right]
        cols = left.cols + right.cols
        kinds = left.kinds + right.kinds
        if len(left.rows) * len(right.rows) > MAX_ROWS:
            raise Unsupported("join too large for the bound")
        pairs = [(self._pick(left, name), self._pick(right, name)) for name in using]
        conditions = []
        for a in left.rows:
            line = []
            for b in right.rows:
                if on is not None:
                    scope = Scope([(q, n, v) for (q, n), v in zip(cols, a.vals + b.vals)], outer,
                                  hidden=[id(v) for i, v in enumerate(a.vals + b.vals) if i in left.hidden or i - len(left.cols) in right.hidden])
                    line.append(truth(self.expr(on, scope)))
                elif using:
                    line.append(z3.And(*[truth(compare("eq", a.vals[i], b.vals[j])) for i, j in pairs]))
                else:
                    line.append(_true())
            conditions.append(line)
        rows = []
        for i, a in enumerate(left.rows):
            for j, b in enumerate(right.rows):
                rows.append(Row(z3.And(a.present, b.present, conditions[i][j]), a.vals + b.vals))
        if side in ("LEFT", "FULL"):
            blanks = [null_value(k) for k in right.kinds]
            for i, a in enumerate(left.rows):
                matched = z3.Or(*[z3.And(b.present, conditions[i][j]) for j, b in enumerate(right.rows)])
                rows.append(Row(z3.And(a.present, z3.Not(matched)), a.vals + blanks))
        if side in ("RIGHT", "FULL"):
            blanks = [null_value(k) for k in left.kinds]
            for j, b in enumerate(right.rows):
                matched = z3.Or(*[z3.And(a.present, conditions[i][j]) for i, a in enumerate(left.rows)])
                rows.append(Row(z3.And(b.present, z3.Not(matched)), blanks + b.vals))
        hidden = set(left.hidden) | {len(left.cols) + i for i in right.hidden}
        first = list(left.first) + [len(left.cols) + i for i in right.first]
        if using:
            offset = len(left.cols)
            for i, j in pairs:
                if side == "RIGHT":
                    hidden.add(i)
                    first.insert(len(using) and 0, offset + j) if offset + j not in first else None
                else:
                    hidden.add(offset + j)
                    if i not in first:
                        first.append(i)
                if side == "FULL":
                    hidden.discard(i)  # the merged column is the left one, holding COALESCE(left, right)
                    rows = [
                        Row(r.present, [
                            self._choose([(z3.Not(r.vals[i].null), r.vals[i])], r.vals[offset + j]) if k == i else v
                            for k, v in enumerate(r.vals)
                        ])
                        for r in rows
                    ]
        return Rel(cols, kinds, rows, frozenset(hidden), tuple(first))

    def _semi_join(self, left: Rel, right: Rel, join: exp.Join, kind: str, outer: Scope | None) -> Rel:
        """``LEFT SEMI JOIN`` keeps the left rows with a match, ``LEFT ANTI JOIN`` those without; only left columns remain."""

        if (join.side or "").upper() not in ("LEFT", "") or join.args.get("using") or join.args.get("on") is None:
            raise Unsupported(f"{kind} join form")
        if len(left.rows) * len(right.rows) > MAX_ROWS:
            raise Unsupported("join too large for the bound")
        cols = left.cols + right.cols
        rows = []
        for a in left.rows:
            matches = []
            for b in right.rows:
                scope = Scope([(q, n, v) for (q, n), v in zip(cols, a.vals + b.vals)], outer)
                matches.append(z3.And(b.present, truth(self.expr(join.args["on"], scope))))
            found = z3.Or(*matches) if matches else _false()
            rows.append(Row(z3.And(a.present, found if kind == "SEMI" else z3.Not(found)), a.vals))
        return Rel(left.cols, left.kinds, rows, left.hidden, left.first)

    def _pick(self, rel: Rel, name: str) -> int:
        matches = [i for i, (q, n) in enumerate(rel.cols) if n == name.lower() and i not in rel.hidden]
        if len(matches) != 1:
            raise Unsupported(f"USING column {name}")
        return matches[0]

    # -- projection ----------------------------------------------------------------------------

    def _output_names(self, items, source: Rel) -> list[tuple[str | None, str]]:
        names = []
        for position, item in enumerate(items):
            if star_modified(item):
                raise Unsupported("SELECT * with EXCEPT, REPLACE, RENAME or ILIKE")  # not expanded below
            if isinstance(item, exp.Star):
                names.extend(source.cols[i] for i in self._star_order(source))
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                table = item.table.lower()
                names.extend((q, n) for q, n in source.cols if q == table)
            else:
                inner = item
                while isinstance(inner, exp.Paren):
                    inner = inner.this
                names.append((None, (item.alias or inner.alias_or_name or f"_col{position}").lower()))
        return names

    def _expand(self, items, source: Rel, row: Row, scope_for) -> list:
        """The projected cells of one source row (stars expanded)."""

        cells = []
        for position, item in enumerate(items):
            if isinstance(item, exp.Star):
                cells.extend(row.vals[i] for i in self._star_order(source))
            elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                table = item.table.lower()
                cells.extend(v for (q, _), v in zip(source.cols, row.vals) if q == table)
            else:
                cells.append(self.expr(item.this if isinstance(item, exp.Alias) else item, scope_for))
        return cells

    def _star_order(self, rel: Rel) -> list[int]:
        rest = [i for i in range(len(rel.cols)) if i not in rel.hidden and i not in rel.first]
        return list(rel.first) + rest

    def projected(self, node, source: Rel, items, outer: Scope | None) -> Rel:
        names = self._output_names(items, source)
        order = node.args.get("order")
        out_rows: list[Row] = []
        scopes = [self._scope(source, row, outer) for row in source.rows]
        context = WindowCtx(source.rows, scopes)
        for index, (row, scope) in enumerate(zip(source.rows, scopes)):
            scope.window, scope.index = context, index
            scope.aliases = {
                item.alias.lower(): (lambda e=item.this, s=scope: self.expr(e, s)) for item in items if isinstance(item, exp.Alias)
            }
            cells = self._expand(items, source, row, scope)
            keys = self._sort_keys(order, items, cells, scope, outer) if order is not None else None
            out_rows.append(Row(row.present, cells, keys))
        return self._finish(names, out_rows)

    def _finish(self, names, rows: list[Row]) -> Rel:
        kinds = []
        for index in range(len(names)):
            kind = "null"
            for row in rows:
                current = row.vals[index].kind
                if kind == "null" or current == "null":
                    kind = current if kind == "null" else kind
                elif kind != current:
                    kind = unify(null_value(kind), null_value(current))[0].kind if {kind, current} != {"int", "real"} else "real"
            kinds.append(kind)
        fixed = [Row(r.present, [to_kind(v, k) if k != "null" else v for v, k in zip(r.vals, kinds)], r.keys) for r in rows]
        return Rel(names, kinds, fixed)

    def _sort_keys(self, order, items, cells, scope: Scope, outer) -> list:
        keys = []
        for item in order.expressions:
            target = item.this
            descending = bool(item.args.get("desc"))
            if isinstance(target, exp.Literal) and not target.is_string:
                position = _whole(target) - 1
                if not 0 <= position < len(cells):
                    raise Unsupported("ORDER BY position")
                value = cells[position]
            else:
                ordering = Scope(scope.cols, scope.parent, {}, scope.group, alias_first=True, hidden=scope.hidden, window=scope.window, index=scope.index, picker=scope.picker, group_keys=scope.group_keys)
                projected = [i for i in items if isinstance(i, exp.Alias)]
                position = 0
                for item_, cell in zip(items, cells):
                    if isinstance(item_, exp.Alias):
                        ordering.aliases[item_.alias.lower()] = cell
                value = self.expr(target, ordering)
            keys.append((value, descending, item.args.get("nulls_first")))
        return keys

    # -- grouping ------------------------------------------------------------------------------

    def grouped(self, node, source: Rel, items, outer: Scope | None) -> Rel:
        if any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in items):
            raise Unsupported("star in a grouped query")
        names = self._output_names(items, source)
        group = node.args.get("group")
        having = node.args.get("having")
        order = node.args.get("order")
        scopes = [self._scope(source, row, outer) for row in source.rows]
        alias_nodes = {i.alias.lower(): i.this for i in items if isinstance(i, exp.Alias)}
        key_nodes = []
        if group is not None:
            if extended_grouping(group):
                raise Unsupported("grouping sets")
            for key in group.expressions:
                if isinstance(key, exp.Literal) and not key.is_string and not self.group_constants:
                    position = _whole(key) - 1
                    if not 0 <= position < len(items):
                        raise Unsupported("GROUP BY position")
                    chosen = items[position]
                    key = chosen.this if isinstance(chosen, exp.Alias) else chosen
                key_nodes.append(key)
        for scope in scopes:
            scope.aliases = {k: (lambda e=e, s=scope: self.expr(e, s)) for k, e in alias_nodes.items()}
        keys = []
        for scope in scopes:
            keys.append([self._group_key(k, scope) for k in key_nodes])
        key_sql = [k.sql() for k in key_nodes]
        out_rows: list[Row] = []
        if key_nodes:
            for i, row in enumerate(source.rows):
                members = [z3.And(source.rows[j].present, *[same(a, b) for a, b in zip(keys[i], keys[j])]) for j in range(len(source.rows))]
                earlier = [members[j] for j in range(i)]
                exists = z3.And(row.present, z3.Not(z3.Or(*earlier))) if earlier else row.present
                scope = Scope(scopes[i].cols, outer, None, Group(members, scopes), hidden=scopes[i].hidden,
                              picker=self._picker(scopes, i, members, keys[i]), group_keys=dict(zip(key_sql, keys[i])) or None)
                scope.aliases = {k: (lambda e=e, s=scope: self.expr(e, s)) for k, e in alias_nodes.items()}
                out_rows.append(self._group_row(items, scope, having, order, outer, exists))
        else:
            members = [r.present for r in source.rows]
            # without GROUP BY one row always comes out, built from the (possibly empty) group
            blank = [(q, n, null_value(k)) for (q, n), k in zip(source.cols, source.kinds)]
            scope = Scope(blank, outer, {k: (lambda e=e: self.expr(e, scope)) for k, e in alias_nodes.items()}, Group(members, scopes),
                          picker=self._picker(scopes, None, members, []))
            out_rows.append(self._group_row(items, scope, having, order, outer, _true()))
        return self._finish(names, out_rows)

    def _picker(self, scopes: list[Scope], rep: int | None, members: list, keys: list[V]):
        """The value of a column that is neither grouped nor aggregated.

        SQL engines return it from an arbitrary row of the group, so the encoding picks a row freely
        (a fresh variable constrained to a member): two queries are equivalent only if they agree
        whatever the pick, which holds when the column is determined by the grouping and fails otherwise.
        """

        cache: dict[int, V] = {}

        def pick(position: int) -> V:
            if position in cache:
                return cache[position]
            own = scopes[rep].cols[position][2] if rep is not None else None
            if own is not None and any(own is key for key in keys):
                cache[position] = own
                return own
            if own is not None and own.kind == "unsupported":
                raise Unsupported(f"column type {own.lit}")
            cells = [s.cols[position][2] for s in scopes]
            kind = next((c.kind for c in cells if c.kind != "null"), "null")
            if kind == "unsupported":
                raise Unsupported("column type")
            chosen = z3.Int(f"pick#{len(self.db.predicates)}#{self._picks}")
            self._picks += 1
            self.side_conditions.append(z3.Implies(z3.Or(*members) if members else _false(), z3.Or(*[z3.And(chosen == k, m) for k, m in enumerate(members)])))
            value, null = _default(kind), _true()
            for k in reversed(range(len(cells))):
                cell = to_kind(cells[k], kind) if kind != "null" else cells[k]
                value = z3.If(chosen == k, cell.val, value)
                null = z3.If(chosen == k, cell.null, null)
            cache[position] = V(kind, value, null)
            return cache[position]

        return pick

    def _group_key(self, node, scope: Scope) -> V:
        if isinstance(node, exp.Column) and not node.table:
            try:
                return scope.lookup(None, node.name.lower())
            except Unsupported:
                pass
        return self.expr(node, scope)

    def _group_row(self, items, scope: Scope, having, order, outer, exists) -> Row:
        present = exists
        if having is not None:
            present = z3.And(present, truth(self.expr(having.this, scope)))
        cells = [self.expr(i.this if isinstance(i, exp.Alias) else i, scope) for i in items]
        keys = self._sort_keys(order, items, cells, scope, outer) if order is not None else None
        return Row(present, cells, keys)

    # -- ORDER BY / LIMIT ----------------------------------------------------------------------

    def limited(self, node, rel: Rel) -> Rel:
        limit = node.args.get("limit")
        offset = node.args.get("offset")
        if limit is None and offset is None:
            return rel
        count = None
        if limit is not None:
            target = limit.expression
            if not (isinstance(target, exp.Literal) and not target.is_string):
                raise Unsupported("LIMIT must be a literal")
            count = _whole(target)
        skip = 0
        if offset is not None:
            target = offset.expression if hasattr(offset, "expression") else offset
            if not (isinstance(target, exp.Literal) and not target.is_string):
                raise Unsupported("OFFSET must be a literal")
            skip = _whole(target)
        if node.args.get("order") is None:
            raise Unsupported("LIMIT without ORDER BY")
        rows = rel.rows
        before = [[None] * len(rows) for _ in rows]  # before[j][i]: row j sorts strictly before row i
        for i, a in enumerate(rows):
            for j, b in enumerate(rows):
                if i != j:
                    before[j][i] = self._precedes(b, a)
        # ties are broken by row position, the same way for both queries
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                tied = z3.Not(z3.Or(before[j][i], before[i][j]))
                before[i][j] = z3.Or(before[i][j], tied)
        out = []
        for i, a in enumerate(rows):
            rank = z3.Sum(*[z3.If(z3.And(b.present, before[j][i]), 1, 0) for j, b in enumerate(rows) if j != i]) if len(rows) > 1 else z3.IntVal(0)
            keep = rank >= skip
            if count is not None:
                keep = z3.And(keep, rank < skip + count)
            out.append(Row(z3.And(a.present, keep), a.vals))
        return Rel(rel.cols, rel.kinds, out)

    def _precedes(self, b: Row, a: Row):
        """Does row ``b`` sort strictly before row ``a`` under the ORDER BY keys."""

        clauses = []
        equal_so_far = []
        for (kb, desc, explicit), (ka, _, _) in zip(b.keys, a.keys):
            kb, ka = unify(kb, ka)
            kb, ka = _ordered(kb), _ordered(ka)
            if kb.kind == "null":
                continue
            if explicit is None:
                # the default places NULL as the smallest value (first under ASC, last under DESC) or the largest
                null_before = z3.BoolVal(not desc if self.nulls_first else desc)
            else:
                null_before = z3.BoolVal(explicit)
            ordered = (ka.val < kb.val) if desc else (kb.val < ka.val)
            less = z3.If(
                z3.And(kb.null, ka.null),
                _false(),
                z3.If(kb.null, null_before, z3.If(ka.null, z3.Not(null_before), ordered)),
            )
            clauses.append(z3.And(*equal_so_far, less))
            equal_so_far.append(same(kb, ka))
        return z3.Or(*clauses) if clauses else _false()

    # -- expressions ---------------------------------------------------------------------------

    def expr(self, node: exp.Expression, scope: Scope) -> V:
        if scope.group_keys and not isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
            known = scope.group_keys.get(node.sql())
            if known is not None:
                return known
        method = getattr(self, "_e_" + type(node).__name__, None)
        if method is not None:
            return method(node, scope)
        if isinstance(node, exp.Binary) and type(node) in _COMPARES:
            if isinstance(node.expression, (exp.Any, exp.All)):
                return self._quantified(_COMPARES[type(node)], self.expr(node.this, scope), node.expression, scope)
            return compare(_COMPARES[type(node)], self.expr(node.this, scope), self.expr(node.expression, scope))
        raise Unsupported(f"expression {type(node).__name__}")

    def _quantified(self, op: str, left: V, quantifier, scope: Scope) -> V:
        subquery = quantifier.this
        if not isinstance(subquery, (exp.Subquery, exp.Select)):
            raise Unsupported("ANY or ALL over a list")
        rel = self.query(subquery, scope)
        if len(rel.cols) != 1:
            raise Unsupported("ANY or ALL over several columns")
        results = [(row.present, compare(op, left, row.vals[0])) for row in rel.rows]
        if isinstance(quantifier, exp.Any):
            t = z3.Or(*[z3.And(p, truth(r)) for p, r in results]) if results else _false()
            f = z3.And(*[z3.Or(z3.Not(p), falsity(r)) for p, r in results]) if results else _true()
        else:
            t = z3.And(*[z3.Or(z3.Not(p), truth(r)) for p, r in results]) if results else _true()
            f = z3.Or(*[z3.And(p, falsity(r)) for p, r in results]) if results else _false()
        return from_tf(t, f)

    def _e_Paren(self, node, scope):
        return self.expr(node.this, scope)

    def _e_Column(self, node, scope):
        if isinstance(node.this, exp.Star):
            raise Unsupported("star in an expression")
        value = scope.lookup(node.table.lower() or None, node.name.lower())
        if value.kind == "unsupported":
            raise Unsupported(f"column type {value.lit}")
        return value

    def _e_Literal(self, node, scope):
        if node.is_string:
            return const(node.this, "str")
        text = node.this
        if re.fullmatch(r"\d+", text):
            if len(text) > 400:
                raise Unsupported("integer literal out of range")
            return const(int(text), "int")
        try:
            value = Decimal(text)
        except ArithmeticError:  # sqlglot tokenizes ``1e`` as a number
            raise Unsupported(f"numeric literal {text[:20]!r}") from None
        if value.is_finite() and value and not -400 <= value.adjusted() <= 400:
            # Out of FLOAT64's range; Fraction(Decimal('1e100000000')) would build a hundred-million-digit integer.
            raise Unsupported("numeric literal out of range")
        return const(Fraction(value), "real")

    def _e_Boolean(self, node, scope):
        return const(bool(node.this), "bool")

    def _e_Null(self, node, scope):
        return null_value()

    def _e_Neg(self, node, scope):
        inner = self.expr(node.this, scope)
        if inner.kind not in ("int", "real"):
            raise Unsupported("negating a non-number")
        return V(inner.kind, -inner.val, inner.null)

    def _e_Add(self, node, scope):
        return self._dated(node, scope, "add")

    def _e_Sub(self, node, scope):
        return self._dated(node, scope, "sub")

    def _dated(self, node, scope, op):
        left, right = node.this, node.expression
        if isinstance(right, exp.Interval):
            return self._shift(self.expr(left, scope), right, op, scope)
        if isinstance(left, exp.Interval) and op == "add":
            return self._shift(self.expr(right, scope), left, op, scope)
        a, b = self.expr(left, scope), self.expr(right, scope)
        if a.kind == "date" and b.kind == "date" and op == "sub":
            return V("int", a.val - b.val, z3.Or(a.null, b.null))
        return arithmetic(op, a, b)

    def _shift(self, base: V, interval: exp.Interval, op: str, scope) -> V:
        unit = interval.args.get("unit")
        name = (unit.name if unit is not None else "DAY").upper()
        if name != "DAY" or base.kind != "date":
            raise Unsupported("interval arithmetic other than days on dates")
        amount = self.expr(interval.this, scope)
        if amount.kind == "str" and isinstance(amount.lit, str):
            try:
                amount = const(int(amount.lit), "int")
            except ValueError:
                raise Unsupported("interval literal")
        if amount.kind != "int":
            raise Unsupported("interval amount")
        return V("date", base.val + amount.val if op == "add" else base.val - amount.val, z3.Or(base.null, amount.null))

    def _e_DateAdd(self, node, scope):
        return self._date_function(node, scope, "add")

    def _e_DateSub(self, node, scope):
        return self._date_function(node, scope, "sub")

    def _date_function(self, node, scope, op):
        unit = node.args.get("unit")
        if unit is not None and unit.name.upper() != "DAY":
            raise Unsupported("date arithmetic other than days")
        base = self.expr(node.this, scope)
        amount = self.expr(node.expression, scope)
        if base.kind == "str":
            base = to_kind(base, "date")
        if base.kind != "date" or amount.kind != "int":
            raise Unsupported("date arithmetic")
        return V("date", base.val + amount.val if op == "add" else base.val - amount.val, z3.Or(base.null, amount.null))

    def _e_DateDiff(self, node, scope):
        unit = node.args.get("unit")
        if unit is not None and unit.name.upper() != "DAY":
            raise Unsupported("DATEDIFF other than days")
        a, b = self.expr(node.this, scope), self.expr(node.expression, scope)
        a, b = (to_kind(a, "date") if a.kind == "str" else a), (to_kind(b, "date") if b.kind == "str" else b)
        if a.kind != "date" or b.kind != "date":
            raise Unsupported("DATEDIFF arguments")
        return V("int", a.val - b.val, z3.Or(a.null, b.null))

    def _e_Mul(self, node, scope):
        return arithmetic("mul", self.expr(node.this, scope), self.expr(node.expression, scope))

    def _e_Div(self, node, scope):
        return arithmetic("div", self.expr(node.this, scope), self.expr(node.expression, scope))

    def _e_IntDiv(self, node, scope):
        return arithmetic("intdiv", self.expr(node.this, scope), self.expr(node.expression, scope))

    def _e_Mod(self, node, scope):
        return arithmetic("mod", self.expr(node.this, scope), self.expr(node.expression, scope))

    def _e_NullSafeEQ(self, node, scope):
        return V("bool", same(self.expr(node.this, scope), self.expr(node.expression, scope)), _false())

    def _e_NullSafeNEQ(self, node, scope):
        return V("bool", z3.Not(same(self.expr(node.this, scope), self.expr(node.expression, scope))), _false())

    def _e_And(self, node, scope):
        return logical_and([self.expr(node.this, scope), self.expr(node.expression, scope)])

    def _e_Or(self, node, scope):
        return logical_or([self.expr(node.this, scope), self.expr(node.expression, scope)])

    def _e_Not(self, node, scope):
        return logical_not(self.expr(node.this, scope))

    def _e_Is(self, node, scope):
        value = self.expr(node.this, scope)
        target = node.expression
        if isinstance(target, exp.Null):
            return V("bool", value.null, _false())
        if isinstance(target, exp.Boolean):
            return V("bool", truth(value) if target.this else falsity(value), _false())
        raise Unsupported("IS with this operand")

    def _e_Between(self, node, scope):
        value = self.expr(node.this, scope)
        low, high = self.expr(node.args["low"], scope), self.expr(node.args["high"], scope)
        return logical_and([compare("gte", value, low), compare("lte", value, high)])

    def _e_In(self, node, scope):
        if isinstance(node.this, exp.Tuple) or any(isinstance(e, exp.Tuple) for e in node.expressions):
            return self._tuple_in(node, scope)
        value = self.expr(node.this, scope)
        query = node.args.get("query")
        if query is not None:
            rel = self.query(query, scope)
            if len(rel.cols) != 1:
                raise Unsupported("IN subquery with several columns")
            candidates = [(r.present, r.vals[0]) for r in rel.rows]
        else:
            if node.args.get("unnest"):
                raise Unsupported("IN UNNEST")
            candidates = [(_true(), self.expr(e, scope)) for e in node.expressions]
        matches = []
        unknown = []
        for present, other in candidates:
            result = compare("eq", value, other)
            matches.append(z3.And(present, truth(result)))
            unknown.append(z3.And(present, z3.Not(truth(result)), z3.Not(falsity(result))))
        t = z3.Or(*matches) if matches else _false()
        f = z3.Not(z3.Or(t, *unknown)) if unknown else z3.Not(t)
        return from_tf(t, f)

    def _tuple_in(self, node, scope):
        probe = [self.expr(e, scope) for e in (node.this.expressions if isinstance(node.this, exp.Tuple) else [node.this])]
        query = node.args.get("query")
        if query is not None:
            rel = self.query(query, scope)
            candidates = [(r.present, r.vals) for r in rel.rows]
        else:
            candidates = [(_true(), [self.expr(x, scope) for x in (e.expressions if isinstance(e, exp.Tuple) else [e])]) for e in node.expressions]
        matches, unknown = [], []
        for present, other in candidates:
            if len(other) != len(probe):
                raise Unsupported("tuple IN with different widths")
            result = logical_and([compare("eq", a, b) for a, b in zip(probe, other)])
            matches.append(z3.And(present, truth(result)))
            unknown.append(z3.And(present, z3.Not(truth(result)), z3.Not(falsity(result))))
        t = z3.Or(*matches) if matches else _false()
        return from_tf(t, z3.Not(z3.Or(t, *unknown)) if unknown else z3.Not(t))

    def _e_Exists(self, node, scope):
        rel = self.query(node.this, scope)
        return V("bool", z3.Or(*[r.present for r in rel.rows]), _false())

    def _e_Subquery(self, node, scope):
        rel = self.query(node.this, scope)
        if len(rel.cols) != 1:
            raise Unsupported("scalar subquery with several columns")
        kind = rel.kinds[0]
        value, null = _default(kind), _true()
        for row in reversed(rel.rows):
            value = z3.If(row.present, row.vals[0].val, value)
            null = z3.If(row.present, row.vals[0].null, null)
        return V(kind, value, null)

    def _e_Select(self, node, scope):
        return self._e_Subquery(exp.Subquery(this=node), scope)

    def _e_Case(self, node, scope):
        subject = self.expr(node.this, scope) if node.this is not None else None
        branches = []
        for branch in node.args["ifs"]:
            if subject is not None:
                condition = truth(compare("eq", subject, self.expr(branch.this, scope)))
            else:
                condition = truth(self.expr(branch.this, scope))
            branches.append((condition, self.expr(branch.args["true"], scope)))
        fallback = self.expr(node.args["default"], scope) if node.args.get("default") is not None else null_value()
        return self._choose(branches, fallback)

    def _e_If(self, node, scope):
        condition = truth(self.expr(node.this, scope))
        fallback = self.expr(node.args["false"], scope) if node.args.get("false") is not None else null_value()
        return self._choose([(condition, self.expr(node.args["true"], scope))], fallback)

    def _choose(self, branches, fallback: V) -> V:
        values = [v for _, v in branches] + [fallback]
        kind = "null"
        for v in values:
            if v.kind == "null":
                continue
            if kind == "null":
                kind = v.kind
            elif kind != v.kind:
                kind = unify(null_value(kind), null_value(v.kind))[0].kind if {kind, v.kind} != {"int", "real"} else "real"
        result = to_kind(fallback, kind) if kind != "null" else fallback
        for condition, value in reversed(branches):
            value = to_kind(value, kind) if kind != "null" else value
            result = V(kind, z3.If(condition, value.val, result.val), z3.If(condition, value.null, result.null))
        return result

    def _e_Coalesce(self, node, scope):
        parts = [self.expr(node.this, scope)] + [self.expr(e, scope) for e in node.expressions]
        branches = [(z3.Not(p.null), p) for p in parts[:-1]]
        return self._choose(branches, parts[-1])

    def _e_Nullif(self, node, scope):
        a, b = self.expr(node.this, scope), self.expr(node.expression, scope)
        return self._choose([(truth(compare("eq", a, b)), null_value(a.kind))], a)

    def _e_Abs(self, node, scope):
        v = self.expr(node.this, scope)
        if v.kind not in ("int", "real"):
            raise Unsupported("ABS of a non-number")
        return V(v.kind, z3.If(v.val >= 0, v.val, -v.val), v.null)

    def _e_Greatest(self, node, scope):
        return self._extreme(node, scope, True)

    def _e_Least(self, node, scope):
        return self._extreme(node, scope, False)

    def _extreme(self, node, scope, largest: bool) -> V:
        parts = [self.expr(node.this, scope)] + [self.expr(e, scope) for e in node.expressions]
        result = parts[0]
        for part in parts[1:]:
            a, b = unify(result, part)
            take = (a.val >= b.val) if largest else (a.val <= b.val)
            result = V(a.kind, z3.If(take, a.val, b.val), z3.Or(a.null, b.null))
        return result

    def _day(self, v: V) -> V:
        """The date of a DATE, TIMESTAMP, or a date-looking string literal."""

        if v.kind == "date":
            return v
        if v.kind == "datetime":
            return V("date", v.val / 86400, v.null)
        if v.kind == "str" and isinstance(v.lit, str):
            return to_kind(v, "date")
        raise Unsupported(f"a date part of a {v.kind}")

    def _part(self, v: V, part: str) -> V:
        year, month, day = civil(self._day(v).val)
        value = {"YEAR": year, "MONTH": month, "DAY": day, "QUARTER": (month + 2) / 3}.get(part)
        if value is None:
            raise Unsupported(f"date part {part}")
        return V("int", value, v.null)

    def _e_TsOrDsToDate(self, node, scope):
        return self._day(self.expr(node.this, scope))

    def _e_Year(self, node, scope):
        return self._part(self.expr(node.this, scope), "YEAR")

    def _e_Month(self, node, scope):
        return self._part(self.expr(node.this, scope), "MONTH")

    def _e_Day(self, node, scope):
        return self._part(self.expr(node.this, scope), "DAY")

    def _e_Quarter(self, node, scope):
        return self._part(self.expr(node.this, scope), "QUARTER")

    def _e_Extract(self, node, scope):
        part = node.this.name.upper() if hasattr(node.this, "name") else str(node.this).upper()
        return self._part(self.expr(node.expression, scope), part)

    def _e_Round(self, node, scope):
        v = self.expr(node.this, scope)
        digits = node.args.get("decimals")
        places = 0
        if digits is not None:
            if not (isinstance(digits, exp.Literal) and not digits.is_string):
                raise Unsupported("ROUND with a computed precision")
            places = _whole(digits)
        if v.kind == "int" and places >= 0:
            return v
        if v.kind not in ("int", "real") or places < 0:
            raise Unsupported("ROUND of this operand")
        x = to_kind(v, "real").val
        factor = z3.RealVal(10**places)
        scaled = x * factor
        half = z3.RealVal(Fraction(1, 2))
        rounded = z3.If(scaled >= 0, z3.ToReal(z3.ToInt(scaled + half)), -z3.ToReal(z3.ToInt(-scaled + half)))
        return V("real", rounded / factor, v.null)

    def _e_Substring(self, node, scope):
        value = self.expr(node.this, scope)
        start, length = node.args.get("start"), node.args.get("length")
        if value.kind != "str" or not (isinstance(start, exp.Literal) and not start.is_string and _whole(start) >= 1):
            raise Unsupported("SUBSTRING with this form")
        offset = _whole(start) - 1
        if length is None:
            size = z3.If(z3.Length(value.val) - offset > 0, z3.Length(value.val) - offset, z3.IntVal(0))
        elif isinstance(length, exp.Literal) and not length.is_string and _whole(length) >= 0:
            size = z3.IntVal(_whole(length))
        else:
            raise Unsupported("SUBSTRING with a computed length")
        return V("str", z3.SubString(value.val, offset, size), value.null)

    def _e_Length(self, node, scope):
        v = self.expr(node.this, scope)
        if v.kind != "str":
            raise Unsupported("LENGTH of a non-string")
        return V("int", z3.Length(v.val), v.null)

    def _e_Concat(self, node, scope):
        parts = [self.expr(e, scope) for e in node.expressions]
        if any(p.kind != "str" for p in parts):
            raise Unsupported("CONCAT of non-strings")
        return V("str", z3.Concat(*[p.val for p in parts]) if len(parts) > 1 else parts[0].val, z3.Or(*[p.null for p in parts]))

    def _e_Cast(self, node, scope):
        v = self.expr(node.this, scope)
        target = node.args["to"].this
        wanted = None
        name = target.name if hasattr(target, "name") else str(target)
        name = str(node.args["to"].sql(dialect=self.dialect)).upper()
        wanted = kind_of(name) or ("int" if name.startswith("SIGNED") or name.startswith("UNSIGNED") else None)
        if wanted is None or (wanted == v.kind) or (wanted == "real" and v.kind == "int") or (wanted in ("date", "time", "datetime") and v.kind == "str" and isinstance(v.lit, str)):
            if wanted is None:
                raise Unsupported(f"CAST to {name}")
            return to_kind(v, wanted)
        if v.kind == "null":
            return null_value(wanted)
        raise Unsupported(f"CAST from {v.kind} to {wanted}")

    def _e_Like(self, node, scope):
        value = self.expr(node.this, scope)
        pattern = self.expr(node.expression, scope)
        if value.kind != "str" or pattern.kind != "str" or not isinstance(pattern.lit, str) or node.args.get("escape"):
            raise Unsupported("LIKE with a non-literal pattern")
        regex = _like_regex(pattern.lit)
        return V("bool", z3.InRe(value.val, regex), value.null)

    def _e_Window(self, node, scope):
        context = scope.window
        if context is None:
            raise Unsupported("window function outside a select list")
        spec = node.args.get("spec")
        if spec is not None and (spec.args.get("kind") or spec.args.get("start") or spec.args.get("end")):
            raise Unsupported("explicit window frame")
        function = node.this
        index = scope.index
        partition = node.args.get("partition_by") or []
        order = node.args.get("order")
        key = id(node)
        if key not in context.cache:
            keyed = []
            for s in context.scopes:
                parts = [self.expr(p, s) for p in partition]
                sorts = [(self.expr(o.this, s), bool(o.args.get("desc")), o.args.get("nulls_first")) for o in order.expressions] if order is not None else []
                keyed.append((parts, sorts))
            context.cache[key] = keyed
        keyed = context.cache[key]
        peers = [
            z3.And(row.present, *[same(x, y) for x, y in zip(keyed[index][0], keyed[j][0])])
            for j, row in enumerate(context.rows)
        ]
        before = [self._precedes(Row(None, [], keyed[j][1]), Row(None, [], keyed[index][1])) if order is not None else _false() for j in range(len(context.rows))]
        if isinstance(function, exp.RowNumber) or is_call(function, "Rank") or is_call(function, "DenseRank"):
            if order is None:
                raise Unsupported("ranking function without ORDER BY")
            if is_call(function, "DenseRank"):
                total = []
                for j in range(len(context.rows)):
                    first = z3.Not(z3.Or(*[
                        z3.And(peers[k], before[k], *[same(x, y) for (x, _), (y, _) in zip(keyed[k][1], keyed[j][1])])
                        for k in range(j)
                    ])) if j else _true()
                    total.append(z3.If(z3.And(peers[j], before[j], first), 1, 0))
                return V("int", 1 + z3.Sum(*total), _false())
            if isinstance(function, exp.RowNumber):  # ties are broken by row position
                before = [
                    z3.Or(before[j], z3.Not(z3.Or(before[j], self._precedes(Row(None, [], keyed[index][1]), Row(None, [], keyed[j][1]))))) if j < index else before[j]
                    for j in range(len(context.rows))
                ]
            counted = z3.Sum(*[z3.If(z3.And(peers[j], before[j]), 1, 0) for j in range(len(context.rows)) if j != index]) if len(context.rows) > 1 else z3.IntVal(0)
            return V("int", 1 + counted, _false())
        if isinstance(function, AGGREGATES):
            frame = []
            for j in range(len(context.rows)):
                if order is None:
                    frame.append(peers[j])
                else:  # the default frame: from the partition start to the last row tied with this one
                    follows = self._precedes(Row(None, [], keyed[index][1]), Row(None, [], keyed[j][1]))
                    frame.append(z3.And(peers[j], z3.Not(follows)))
            return self._aggregate_over(function, Group(frame, context.scopes))
        if isinstance(function, (exp.Lag, exp.Lead, exp.FirstValue)):
            if order is None:
                raise Unsupported("LAG, LEAD or FIRST_VALUE without ORDER BY")
            count = len(context.rows)
            ahead = [[self._precedes(Row(None, [], keyed[k][1]), Row(None, [], keyed[j][1])) if k != j else _false() for j in range(count)] for k in range(count)]
            for k in range(count):  # ties are broken by row position
                for j in range(k + 1, count):
                    ahead[k][j] = z3.Or(ahead[k][j], z3.Not(z3.Or(ahead[k][j], ahead[j][k])))
            rank = [z3.Sum(*[z3.If(z3.And(peers[k], ahead[k][j]), 1, 0) for k in range(count)]) for j in range(count)]
            if isinstance(function, exp.FirstValue):
                target = z3.IntVal(0)
                fallback = null_value()
            else:
                offset_node = function.args.get("offset")
                amount = 1
                if offset_node is not None:
                    if not (isinstance(offset_node, exp.Literal) and not offset_node.is_string):
                        raise Unsupported("LAG or LEAD with a computed offset")
                    amount = _whole(offset_node)
                target = rank[index] + (amount if isinstance(function, exp.Lead) else -amount)
                default = function.args.get("default")
                fallback = self.expr(default, scope) if default is not None else null_value()
            argument = function.this
            values = [self.expr(argument, s) for s in context.scopes]
            kinds = {v.kind for v in values} - {"null"} | ({fallback.kind} - {"null"})
            kind = next(iter(kinds)) if len(kinds) == 1 else ("real" if kinds == {"int", "real"} else None)
            if kind is None:
                raise Unsupported("LAG or LEAD over mixed types")
            result = to_kind(fallback, kind)
            for j in reversed(range(count)):
                value = to_kind(values[j], kind)
                hit = z3.And(peers[j], rank[j] == target)
                result = V(kind, z3.If(hit, value.val, result.val), z3.If(hit, value.null, result.null))
            return result
        raise Unsupported(f"window function {type(function).__name__}")

    def _e_Anonymous(self, node, scope):
        name = str(node.this).upper()
        arguments: list[V] = []
        for argument in node.expressions:
            if isinstance(argument, exp.Column) and not argument.table:
                row = scope.qualifier_values(argument.name.lower())
                if row is not None:
                    arguments.extend(row)
                    continue
            arguments.append(self.expr(argument, scope))
        if any(a.kind in ("null", "unsupported") for a in arguments):
            raise Unsupported(f"function {name} over a NULL literal")
        sorts = []
        for a in arguments:
            sorts.extend([_sort(a.kind), z3.BoolSort()])
        key = (name, tuple(str(x) for x in sorts))
        function = self.db.predicates.get(key)
        if function is None:
            function = self.db.predicates[key] = z3.Function(f"predicate_{name}_{len(self.db.predicates)}", *sorts, z3.BoolSort())
        call = []
        for a in arguments:
            call.extend([a.val, a.null])
        return V("bool", function(*call), _false())

    def _e_Count(self, node, scope):
        return self._aggregate(node, scope)

    _e_Sum = _e_Min = _e_Max = _e_Avg = _e_Count

    def _aggregate(self, node, scope: Scope) -> V:
        group = scope.group
        if group is None:
            raise Unsupported("aggregate outside a grouped query")
        return self._aggregate_over(node, group)

    def _aggregate_over(self, node, group: "Group") -> V:
        argument = node.this
        distinct = False
        tuple_arguments = None
        if isinstance(argument, exp.Distinct):
            distinct = True
            if len(argument.expressions) != 1:
                if not isinstance(node, exp.Count):
                    raise Unsupported("DISTINCT over several columns")
                tuple_arguments = argument.expressions
            else:
                argument = argument.expressions[0]
        if tuple_arguments is not None:  # COUNT(DISTINCT a, b) counts the tuples without a NULL
            rows = [[self.expr(a, s) for a in tuple_arguments] for s in group.scopes]
            counted = [z3.And(m, *[z3.Not(v.null) for v in row]) for m, row in zip(group.members, rows)]
            first = [
                z3.And(c, z3.Not(z3.Or(*[z3.And(counted[k], *[same(x, y) for x, y in zip(rows[k], row)]) for k in range(j)]))) if j else c
                for j, (c, row) in enumerate(zip(counted, rows))
            ]
            return V("int", z3.Sum(*[z3.If(f, 1, 0) for f in first]) if first else z3.IntVal(0), _false())
        if isinstance(node, exp.Count) and isinstance(argument, exp.Star):
            return V("int", z3.Sum(*[z3.If(m, 1, 0) for m in group.members]) if group.members else z3.IntVal(0), _false())
        cache_key = id(argument)
        if cache_key not in group.cache:
            group.cache[cache_key] = [self.expr(argument, s) for s in group.scopes]
        values = group.cache[cache_key]
        present = [z3.And(m, z3.Not(v.null)) for m, v in zip(group.members, values)]
        if distinct:
            present = [
                z3.And(p, z3.Not(z3.Or(*[z3.And(present[k], same(values[k], v)) for k in range(i)]))) if i else p
                for i, (p, v) in enumerate(zip(present, values))
            ]
        counted = z3.Sum(*[z3.If(p, 1, 0) for p in present]) if present else z3.IntVal(0)
        if isinstance(node, exp.Count):
            return V("int", counted, _false())
        if isinstance(node, (exp.Sum, exp.Avg)):
            values = [to_kind(v, "int") if v.kind == "bool" else v for v in values]
        kinds = {v.kind for v in values} - {"null"}
        if not kinds:
            return null_value()
        kind = "real" if "real" in kinds else next(iter(kinds)) if len(kinds) == 1 else None
        if kind is None:
            raise Unsupported("aggregate over mixed types")
        values = [to_kind(v, kind) for v in values]
        none = z3.Not(z3.Or(*present)) if present else _true()
        if isinstance(node, (exp.Sum, exp.Avg)):
            if kind not in ("int", "real"):
                raise Unsupported("SUM or AVG of a non-number")
            total = z3.Sum(*[z3.If(p, v.val, z3.IntVal(0) if kind == "int" else z3.RealVal(0)) for p, v in zip(present, values)]) if present else _default(kind)
            if isinstance(node, exp.Sum):
                return V(kind, total, none)
            real_total = z3.ToReal(total) if kind == "int" else total
            return V("real", real_total / z3.If(counted == 0, z3.RealVal(1), z3.ToReal(counted)), none)
        # MIN / MAX
        ordered = [_ordered(v) for v in values]
        kind = ordered[0].kind
        best, have = _default(kind), _false()
        for p, v in zip(present, ordered):
            better = (v.val < best) if isinstance(node, exp.Min) else (v.val > best)
            take = z3.And(p, z3.Or(z3.Not(have), better))
            best = z3.If(take, v.val, best)
            have = z3.Or(have, p)
        return V(kind, best, z3.Not(have))


_COMPARES = {
    exp.EQ: "eq",
    exp.NEQ: "neq",
    exp.LT: "lt",
    exp.LTE: "lte",
    exp.GT: "gt",
    exp.GTE: "gte",
}


def _like_regex(pattern: str):
    any_char = z3.AllChar(z3.ReSort(z3.StringSort()))
    parts = []
    literal = ""
    for char in pattern:
        if char in "%_":
            if literal:
                parts.append(z3.Re(literal))
                literal = ""
            parts.append(z3.Star(any_char) if char == "%" else any_char)
        else:
            literal += char
    if literal:
        parts.append(z3.Re(literal))
    if not parts:
        return z3.Re("")
    return parts[0] if len(parts) == 1 else z3.Concat(*parts)


# --- comparing two relations -------------------------------------------------------------------


def bag_difference(left: Rel, right: Rel):
    """A formula that holds when the two relations are different bags of rows."""

    if len(left.cols) != len(right.cols):
        raise Unsupported("different column counts")
    kinds = []
    for a, b in zip(left.kinds, right.kinds):
        if a == b:
            kinds.append(a)
        elif a == "null":
            kinds.append(b)
        elif b == "null":
            kinds.append(a)
        elif {a, b} <= {"int", "real", "bool"}:
            kinds.append("real" if "real" in (a, b) else "int")
        else:
            raise Unsupported(f"column types differ ({a} vs {b})")
    L = [Row(r.present, [to_kind(v, k) for v, k in zip(r.vals, kinds)]) for r in left.rows]
    R = [Row(r.present, [to_kind(v, k) for v, k in zip(r.vals, kinds)]) for r in right.rows]

    def equal(a: Row, b: Row):
        return z3.And(*[same(x, y) for x, y in zip(a.vals, b.vals)]) if a.vals else _true()

    def count(rows, probe, cache):
        return z3.Sum(*[z3.If(z3.And(r.present, cache(r, probe)), 1, 0) for r in rows]) if rows else z3.IntVal(0)

    pairs: dict = {}

    def eq(a: Row, b: Row):
        key = (id(a), id(b))
        if key not in pairs:
            pairs[key] = pairs[(id(b), id(a))] = equal(a, b)
        return pairs[key]

    clauses = []
    for row in L:
        clauses.append(z3.And(row.present, count(L, row, eq) != count(R, row, eq)))
    for row in R:
        clauses.append(z3.And(row.present, count(R, row, eq) != count(L, row, eq)))
    return z3.Or(*clauses) if clauses else _false()


# --- model extraction and replay ---------------------------------------------------------------


def _model_value(model, v: V, kind: str):
    if z3.is_true(model.eval(v.null, model_completion=True)):
        return None
    value = model.eval(v.val, model_completion=True)
    if kind in ("int",):
        return value.as_long()
    if kind == "date":
        return _dt.date.fromordinal(value.as_long())  # the domain constraints keep it in range
    if kind == "time":
        seconds = value.as_long() % 86400
        return _dt.time(seconds // 3600, seconds % 3600 // 60, seconds % 60)
    if kind == "datetime":
        day, seconds = divmod(value.as_long(), 86400)
        return _dt.datetime.combine(_dt.date.fromordinal(day), _dt.time(seconds // 3600, seconds % 3600 // 60, seconds % 60))
    if kind == "bool":
        return z3.is_true(value)
    if kind == "real":
        fraction = Fraction(value.as_fraction())
        denominator = fraction.denominator
        while denominator % 2 == 0:
            denominator //= 2
        while denominator % 5 == 0:
            denominator //= 5
        if denominator == 1:
            return float(fraction)
        return float(fraction)
    if kind == "str":
        return _decode(value.as_string())
    raise Unsupported(kind)


def _decode(text: str) -> str:
    return re.sub(r"\\u\{([0-9a-fA-F]+)\}", lambda m: chr(int(m.group(1), 16)), text)


def database_from_model(database: SymbolicDatabase, model) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for name, slots in database.tables.items():
        table = database.schema.tables[name]
        rows = []
        for slot in slots:
            if z3.is_true(model.eval(slot.present, model_completion=True)):
                rows.append(tuple(
                    _model_value(model, v, kind_of(c.type) or "int") if v.kind != "unsupported" else None
                    for v, c in zip(slot.vals, table.columns)
                ))
        out[name] = rows
    return out


class DuckDBReplay:
    """Execute both queries on DuckDB over a concrete database: the judge of every counterexample.

    A counterexample counts only when the two result bags differ and keep differing after the rows
    of every table are shuffled (so a tie in ``LIMIT``, or MySQL's arbitrary pick of an ungrouped
    column, never produces one).
    """

    def __init__(self, schema: BoundedSchema, left: str, right: str, dialect: str = "bigquery", shuffles: int = 3, translate: Callable[[str], str] | None = None):
        import duckdb

        from . import counterexample as cx

        self.schema = schema
        self.shuffles = shuffles
        self.duckdb = duckdb
        self.db = small_database()
        self.cx = cx
        self.bigquery = dialect == "bigquery"
        if self.bigquery:
            from .bigquery_on_duckdb import configure

            configure(self.db)
        self.names = {}
        for name, table in schema.tables.items():
            if not table.columns:
                raise Unsupported(f"table {name!r} has no columns")
            columns = ", ".join(f'"{c.name}" {_duck_type(c, self.bigquery)}' for c in table.columns)
            self.db.execute(f'CREATE TABLE "{name}" ({columns})')
        self.left = translate(left) if translate is not None else self._translate(left, dialect)
        self.right = translate(right) if translate is not None else self._translate(right, dialect)

    def _translate(self, sql: str, dialect: str) -> str:
        tree = sqlglot.parse_one(sql, read=dialect)
        if dialect == "mysql":
            tree = self.cx.relax_grouping(self.cx._date_functions(tree))
        for table in tree.find_all(exp.Table):
            found = self.schema.lookup(table)
            if found is not None:
                if not table.alias:
                    table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
                table.set("catalog", None)
                table.set("db", None)
                table.set("this", exp.to_identifier(found.name))
        if dialect == "bigquery":
            from .bigquery_on_duckdb import faithful

            tree = faithful(tree)
        return spell_for_duckdb(tree).sql(dialect="duckdb")

    def _run(self, data: dict[str, list[tuple]]):
        for name, table in self.schema.tables.items():
            self.db.execute(f'DELETE FROM "{name}"')
            rows = data.get(name) or []
            if rows:
                marks = ", ".join("?" * len(table.columns))
                self.db.executemany(f'INSERT INTO "{name}" VALUES ({marks})', rows)
        left, right = self.db.execute(self.left).fetchall(), self.db.execute(self.right).fetchall()
        if self.bigquery:
            from .bigquery_on_duckdb import UnfaithfulOutput, bigquery_rows

            try:
                return bigquery_rows(left), bigquery_rows(right)
            except UnfaithfulOutput as error:
                raise self.duckdb.InvalidInputException(str(error)) from error
        return left, right

    def differ(self, data: dict[str, list[tuple]]) -> bool | None:
        """``True``: the bags differ on every shuffle; ``False``: they agree; ``None``: could not run."""

        try:
            a, b = self._run(data)
            expected = (self.cx._bag(a), self.cx._bag(b))
            if expected[0] == expected[1]:
                return False
            for shuffled in _reorders(data, random.Random(0), self.shuffles):
                a2, b2 = self._run(shuffled)
                if (self.cx._bag(a2), self.cx._bag(b2)) != expected:
                    return None
            return True
        except self.duckdb.Error:
            return None


class SQLiteReplay:
    """The same judge on SQLite (standard library), for queries written in SQLite's dialect.

    Both queries run exactly as written over the model's database; the shuffle and bag rules are DuckDBReplay's.
    """

    def __init__(self, schema: BoundedSchema, left: str, right: str, shuffles: int = 3):
        import sqlite3

        from . import counterexample as cx

        self.schema = schema
        self.shuffles = shuffles
        self.sqlite3 = sqlite3
        self.cx = cx
        self.db = sqlite3.connect(":memory:")
        for name, table in schema.tables.items():
            columns = ", ".join(f'"{c.name}" {_sqlite_type(c)}' for c in table.columns)
            self.db.execute(f'CREATE TABLE "{name}" ({columns})')
        self.left, self.right = left, right

    def _run(self, data: dict[str, list[tuple]]):
        for name, table in self.schema.tables.items():
            self.db.execute(f'DELETE FROM "{name}"')
            rows = data.get(name) or []
            if rows:
                marks = ", ".join("?" * len(table.columns))
                self.db.executemany(f'INSERT INTO "{name}" VALUES ({marks})', rows)
        return self.db.execute(self.left).fetchall(), self.db.execute(self.right).fetchall()

    def differ(self, data: dict[str, list[tuple]]) -> bool | None:
        try:
            a, b = self._run(data)
            expected = (self.cx._bag(a), self.cx._bag(b))
            if expected[0] == expected[1]:
                return False
            for shuffled in _reorders(data, random.Random(0), self.shuffles):
                a2, b2 = self._run(shuffled)
                if (self.cx._bag(a2), self.cx._bag(b2)) != expected:
                    return None
            return True
        except self.sqlite3.Error:
            return None


def _reorders(data: dict[str, list[tuple]], rng: random.Random, shuffles: int):
    """``data`` reversed, then rotated by a row, then ``shuffles`` random shuffles (random shuffles
    alone keep a two-row table in order once in ``2**shuffles``)."""

    yield {n: list(reversed(rows)) for n, rows in data.items()}
    yield {n: list(rows[1:]) + list(rows[:1]) for n, rows in data.items()}
    for _ in range(shuffles):
        yield {n: rng.sample(rows, len(rows)) for n, rows in data.items()}


def _sqlite_type(column: BColumn) -> str:
    kind = kind_of(column.type)
    return {"int": "INTEGER", "real": "REAL", "str": "TEXT", "bool": "INTEGER", "date": "TEXT", "time": "TEXT", "datetime": "TEXT", None: "INTEGER"}[kind]


def _duck_type(column: BColumn, bigquery: bool = False) -> str:
    kind = kind_of(column.type)
    if bigquery and _base_type(column.type) == "NUMERIC":
        declared = _declared_precision(column.type)
        # BigQuery's NUMERIC, or the declared NUMERIC(p, s); a value it cannot hold fails to load
        return f"DECIMAL({declared[0]}, {declared[1]})" if declared else "DECIMAL(38, 9)"
    return {"int": "BIGINT", "real": "DOUBLE", "str": "VARCHAR", "bool": "BOOLEAN", "date": "DATE", "time": "TIME", "datetime": "TIMESTAMP", None: "BIGINT"}[kind]


# --- driver ------------------------------------------------------------------------------------


@dataclass
class BoundedResult:
    status: BoundedStatus
    reason: str
    bound: int = 0  # rows per table covered (largest bound whose check finished without a counterexample)
    counterexample: dict[str, list[tuple]] | None = None
    seconds: float = 0.0
    assumptions: tuple[str, ...] = ASSUMPTIONS

    @property
    def bounded_equivalent(self) -> bool:
        return self.status is BoundedStatus.BOUNDED_EQUIVALENT

    @property
    def label(self) -> str:
        """The evidence label, for example ``bounded, 3 rows``."""

        return f"bounded, {self.bound} {'row' if self.bound == 1 else 'rows'}" if self.bounded_equivalent else self.status.value


def _no_replay(data) -> None:
    return None


def _check_bounded(
    left_sql: str,
    right_sql: str,
    schema: BoundedSchema,
    *,
    rows: int = 3,
    start: int = 1,
    dialect: str = "bigquery",
    timeout_ms: int = 10_000,
    budget_s: float | None = None,
    replay: DuckDBReplay | Callable | None = None,
    nulls_first: bool | None = None,
    prepare: Callable[[str], str] | None = None,
    translate: Callable[[str], str] | None = None,
    group_constants: bool = False,
) -> BoundedResult:
    """Check two queries on every database of at most ``rows`` rows per table.

    Bounds ``start`` .. ``rows`` are tried in turn, so a counterexample is found at its smallest
    size and a timeout still reports the largest bound that finished. ``bounded_equivalent`` is not a
    proof: it says no counterexample exists within ``bound`` rows per table.
    """

    if z3 is None:
        return BoundedResult(BoundedStatus.UNKNOWN, Z3_MISSING)
    began = time.time()
    if prepare is not None:
        left_sql, right_sql = prepare(left_sql), prepare(right_sql)
    # The compiler matches set-operation branches by position: align BY NAME ones first (the
    # replay then runs the same positional queries the encoding compiled).
    left_sql, right_sql, problem = positional_sql_pair(left_sql, right_sql, dialect)
    if problem:
        return BoundedResult(BoundedStatus.UNKNOWN, f"unsupported: {problem}", 0, None, time.time() - began)
    done = 0
    unconfirmed = 0
    restrictions: list[str] = []
    try:
        if replay is None:
            from .bigquery_on_duckdb import Unfaithful

            try:
                replay = DuckDBReplay(schema, left_sql, right_sql, dialect, translate=translate)
            except (Unfaithful, ImportError):
                # DuckDB cannot run these as BigQuery does, or is not installed (the ``execution`` extra): no
                # counterexample is confirmed, and a bound with no difference is still reported.
                replay = _no_replay
        for bound in range(start, rows + 1):
            outcome = None
            for attempt in range(3):
                database = SymbolicDatabase(schema, bound, restrict=restrictions)
                compiler = Compiler(database, dialect, nulls_first=nulls_first, group_constants=group_constants)
                left = compiler.compile(left_sql)
                right = compiler.compile(right_sql)
                remaining = timeout_ms if budget_s is None else int(max(1, min(timeout_ms, (budget_s - (time.time() - began)) * 1000)))
                solver = bounded_solver(remaining)
                solver.add(*database.constraints, *compiler.side_conditions)
                solver.add(bag_difference(left, right))
                verdict = solver.check()
                if verdict == z3.unsat:
                    outcome = "same"
                    break
                if verdict != z3.sat:
                    outcome = "timeout"
                    break
                data = database_from_model(database, solver.model())
                confirmed = replay.differ(data) if hasattr(replay, "differ") else replay(data)
                if confirmed:
                    return BoundedResult(BoundedStatus.DIFFERENT, f"counterexample with at most {bound} rows per table", bound, data, time.time() - began)
                unconfirmed += 1
                # an unconfirmed model may come from an unprintable string or an inexact real: retry restricted
                if "printable" not in restrictions:
                    restrictions.append("printable")
                elif "decimal" not in restrictions:
                    restrictions.append("decimal")
                else:
                    outcome = "unconfirmed"
                    break
            if outcome == "same":
                done = bound
                continue
            reason = {"timeout": "solver timeout", "unconfirmed": "a model the replay did not confirm"}.get(outcome, "unresolved")
            if done >= 1:  # the smaller bounds finished: that much is established
                return BoundedResult(BoundedStatus.BOUNDED_EQUIVALENT, f"no counterexample with at most {done} rows per table ({reason} at {bound})", done, None, time.time() - began)
            return BoundedResult(BoundedStatus.UNKNOWN, f"{reason} at {bound} rows", 0, None, time.time() - began)
        return BoundedResult(BoundedStatus.BOUNDED_EQUIVALENT, f"no counterexample with at most {done} rows per table", done, None, time.time() - began)
    except Unsupported as error:
        return BoundedResult(BoundedStatus.UNKNOWN, f"unsupported: {error}", 0, None, time.time() - began)
    except sqlglot.errors.SqlglotError as error:
        return BoundedResult(BoundedStatus.UNKNOWN, f"parse error: {str(error)[:80]}", 0, None, time.time() - began)
    except RecursionError:
        return BoundedResult(BoundedStatus.UNKNOWN, "query too deeply nested", 0, None, time.time() - began)


@serialized
def evaluate(sql: str, schema: BoundedSchema, data: dict[str, list[tuple]], dialect: str = "bigquery", nulls_first: bool | None = None):
    """The rows the *encoding* gives ``sql`` on a concrete database (for testing it against DuckDB)."""

    database = SymbolicDatabase(schema, max([len(r) for r in data.values()] + [1]))
    compiler = Compiler(database, dialect, nulls_first=nulls_first)
    rel = compiler.compile(sql)
    solver = bounded_solver(20_000)
    for name, slots in database.tables.items():
        table = schema.tables[name]
        rows = data.get(name) or []
        for index, slot in enumerate(slots):
            if index < len(rows):
                solver.add(slot.present)
                for v, c, cell in zip(slot.vals, table.columns, rows[index]):
                    kind = kind_of(c.type)
                    if cell is None:
                        solver.add(v.null)
                    else:
                        solver.add(z3.Not(v.null))
                        solver.add(v.val == _cell(cell, kind))
            else:
                solver.add(z3.Not(slot.present))
    if compiler._picks:
        raise Unsupported("an ungrouped column has no single value")
    solver.add(*compiler.side_conditions)
    verdict = solver.check()
    if verdict == z3.unknown:
        raise Unsupported("solver timeout")
    if verdict != z3.sat:
        raise Unsupported("the concrete database violates a side condition")
    model = solver.model()
    out = []
    for row in rel.rows:
        if z3.is_true(model.eval(row.present, model_completion=True)):
            out.append(tuple(_model_value(model, v, k) if k != "null" else None for v, k in zip(row.vals, rel.kinds)))
    return out


def _cell(value, kind: str):
    if kind == "time":
        value = _dt.time.fromisoformat(value if len(value) > 5 else value + ":00") if isinstance(value, str) else value
        return z3.IntVal(value.hour * 3600 + value.minute * 60 + value.second)
    if kind == "datetime":
        value = _dt.datetime.fromisoformat(value) if isinstance(value, str) else value
        return z3.IntVal(_seconds(value))
    if kind == "date":
        return z3.IntVal((_dt.date.fromisoformat(value) if isinstance(value, str) else value).toordinal())
    if kind == "real":
        return z3.RealVal(Fraction(str(value)))
    if kind == "bool":
        return z3.BoolVal(bool(value))
    if kind == "str":
        return z3.StringVal(value)
    return z3.IntVal(value)


@serialized
def check_bounded(left_sql: str, right_sql: str, schema: BoundedSchema, **kwargs) -> BoundedResult:
    """See :func:`_check_bounded`. For SQLite only a counterexample is offered: the encoding's LIKE (case-sensitive)
    and ``/`` (exact) differ from SQLite's, so "no counterexample" would not carry over."""

    if kwargs.get("dialect", "bigquery") == "bigquery":
        unknown_type = invalid_type_name(left_sql) or invalid_type_name(right_sql)
        if unknown_type:
            # The compiler reads FLOAT, INT32 and VARCHAR as FLOAT64, INT64 and STRING, so a bound "checked" for
            # such a query would be a claim about a query BigQuery rejects.
            return BoundedResult(BoundedStatus.UNKNOWN, f"unsupported: BigQuery would reject the query: {unknown_type}")
    result = _check_bounded(left_sql, right_sql, schema, **kwargs)
    if kwargs.get("dialect") == "sqlite" and result.bounded_equivalent:
        return BoundedResult(BoundedStatus.UNKNOWN, "SQLite: no equivalence claim (its LIKE and integer division differ from the encoding)", result.bound, None, result.seconds)
    return result
