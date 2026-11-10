"""A heavy executed search for databases that separate two queries a prover called equivalent.

The evals already re-run every proof on a few dozen to a thousand random databases. This search is
much heavier and aims at the corners those miss:

* **exhaustive tiny databases**: every database with up to two rows per table over the columns the
  queries read, each column drawing from NULL and two values (sampled when there are too many);
* **edge databases**: every table empty, each table empty in turn, one all-NULL row, doubled rows;
* **random databases** whose profile varies per database: row counts from 0 to 15 (and around any
  ``LIMIT``, ``OFFSET`` or count threshold the queries name), NULL rates from 0 to 80%, duplicate-heavy
  rows, ties, values next to the queries' literals, boundary numbers (fractions, negatives, 2^31),
  strings that differ only in case, trailing spaces or Unicode case mapping, and dates on month and
  year boundaries.

Every database honours the declared NOT NULL columns, keys (primary and unique) and foreign keys, plus
an optional ``legal`` callback for anything else an eval declares (CHECK constraints). A difference
counts only when it survives three checks: the bags differ after numbers are rounded to 9 significant
digits, DuckDB's unoptimized plan agrees (DuckDB 1.5.6 has optimizer bugs, see
``kumosql.duckdb_load.run_unoptimized``), and each query returns the same bag when every table's rows are
reversed and shuffled (no dependence on row order). The witness is then shrunk row by row.

The optimizer's wrong rows can also make two different queries agree. So every edge database, and every
fifth one after them, on which the optimized results agree is run with the optimizer off as well
(``masked``); results that differ only there end the search with the verdict ``masked``.

The search knows nothing about the prover; an adapter per eval (``tools/recheck/*.py``) turns the eval's
proven pairs into :class:`Case` objects with DuckDB SQL, written exactly as that eval's own harness runs
them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal, InvalidOperation
import itertools
import math
import random
import re
import threading
import time
from typing import Any, Callable, Iterable, Iterator

import sqlglot
from sqlglot import exp

KINDS = ("int", "float", "decimal", "text", "date", "timestamp", "bool", "time")
_DEFAULT_SQL_TYPE = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "decimal": "DECIMAL(18,6)",
    "text": "VARCHAR",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "bool": "BOOLEAN",
    "time": "TIME",
}


@dataclass
class Column:
    name: str
    kind: str = "int"  # one of KINDS: which values the generator draws
    not_null: bool = False
    sql_type: str = ""  # the DuckDB column type; defaults from ``kind``
    values: tuple = ()  # the only values allowed (an ENUM or a CHECK ... IN list)

    def ddl_type(self) -> str:
        return self.sql_type or _DEFAULT_SQL_TYPE[self.kind]


@dataclass
class Table:
    name: str
    columns: list[Column]
    keys: list[tuple[str, ...]] = field(default_factory=list)  # primary key first; non-NULL tuples are distinct
    foreign_keys: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(default_factory=list)  # (columns, parent, parent columns)

    def index(self, name: str) -> int:
        lowered = name.lower()
        for position, column in enumerate(self.columns):
            if column.name.lower() == lowered:
                return position
        raise KeyError(name)


Database = dict  # table name -> list of row tuples


@dataclass
class Case:
    """One proven pair, ready to run on DuckDB."""

    eval: str
    pair: str
    left: str  # DuckDB SQL, exactly as the eval's harness runs it
    right: str
    tables: dict[str, Table]
    legal: Callable[[Database], bool] | None = None  # extra declared constraints (CHECKs, cross-table rules)
    setup: tuple[str, ...] = ()  # statements run once on the connection (collations, settings)
    held_out: bool = False
    source: tuple[str, str] = ("", "")  # the pair as the prover read it
    dialect: str = ""  # the dialect the prover read
    meta: dict = field(default_factory=dict)
    float_digits: int = 9
    mode: str = "bag"  # bag, set, list (row order counts), contained (left a sub-bag of right), contained-set


# --- literals ------------------------------------------------------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


@dataclass
class Pool:
    """The literals of both queries, by kind."""

    ints: set = field(default_factory=set)
    floats: set = field(default_factory=set)
    strings: set = field(default_factory=set)
    dates: set = field(default_factory=set)
    timestamps: set = field(default_factory=set)
    counts: set = field(default_factory=set)  # LIMIT, OFFSET and small thresholds: tables of n - 1 .. n + 2 rows matter
    patterns: set = field(default_factory=set)


def literal_pool(*queries: str, dialect: str = "duckdb") -> Pool:
    pool = Pool()
    for query in queries:
        try:
            tree = sqlglot.parse_one(query, read=dialect)
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            for number in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", query):
                _add_number(pool, number)
            for text in re.findall(r"'((?:[^']|'')*)'", query):
                _add_string(pool, text.replace("''", "'"))
            continue
        for node in tree.walk():
            if isinstance(node, exp.Literal):
                if node.is_string:
                    _add_string(pool, node.this)
                    if isinstance(node.parent, (exp.Like, exp.ILike)) and node.parent.expression is node:
                        pool.patterns.add(node.this)
                else:
                    _add_number(pool, node.this)
            elif isinstance(node, (exp.Limit, exp.Offset)):
                for literal in node.find_all(exp.Literal):
                    if not literal.is_string and literal.this.isdigit():
                        pool.counts.add(int(literal.this))
    for value in list(pool.ints):
        if 1 <= value <= 12:
            pool.counts.add(value)
    return pool


def _add_number(pool: Pool, text: str) -> None:
    try:
        number = Decimal(text)
    except InvalidOperation:
        return
    if not number.is_finite() or abs(number) > 10**12:
        return
    if number == number.to_integral_value() and "." not in text and "e" not in text.lower():
        pool.ints.add(int(number))
    else:
        pool.floats.add(float(number))
        pool.ints.add(int(number))


def _add_string(pool: Pool, text: str) -> None:
    if _DATE.match(text):
        try:
            pool.dates.add(dt.date.fromisoformat(text))
            return
        except ValueError:
            pass
    if _TIMESTAMP.match(text):
        try:
            pool.timestamps.add(dt.datetime.fromisoformat(text.replace("T", " ")))
            return
        except ValueError:
            pass
    if len(text) <= 40:
        pool.strings.add(text)


# --- value domains -------------------------------------------------------------------------------

_INT_BOUNDARY = [0, 1, -1, 2, 2147483647, -2147483648, 1000000000, 10**12, 16777216, 16777217, 2**53, 2**53 + 1, -(2**53) - 1]
_FLOAT_FRACTIONS = [0.5, 1.5, -0.5, 2.25, 0.1, 0.2, 0.3, 2.5, -1.5, 1 / 3]
_FLOAT_BOUNDARY = [0.0, 1e-9, -1e-9, 1e9, 123456789.123456, 0.999999999, 1e15]
_TEXT_EDGE = ["", " ", "a", "A", "a ", " a", "ab", "aB", "b", "B", "aa", "%", "_", "0", "1", "10", "9", "İ", "ß", "ǅ", "ς", "σ", "Σ", "ﬁ", "\u212a", "x'y", "NULL", "a\nb", "é", "e\u0301"]
_DATE_EDGE = ["1970-01-01", "1999-12-31", "2000-01-01", "2019-12-31", "2020-01-01", "2020-01-31", "2020-02-29", "2020-03-01", "2020-12-31"]


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def _flavours(kind: str, pool: Pool) -> dict[str, list]:
    """Named value sets for one kind; a database draws its domain from one or a mix of them."""

    if kind == "int":
        near = sorted({v + d for v in pool.ints for d in (-1, 0, 1)} | {v // 2 for v in pool.ints if abs(v) > 3})
        return {
            "tiny": [0, 1, 2, 3],
            "signed": [-2, -1, 0, 1, 2],
            "literal": near or [0, 1, 2],
            "boundary": _INT_BOUNDARY,
            "count": sorted({c + d for c in pool.counts for d in (-1, 0, 1)}) or [1, 2, 3],
        }
    if kind in ("float", "decimal"):
        near = sorted({float(v) + d for v in pool.ints | pool.floats for d in (-0.5, 0.0, 0.5)})
        out = {
            "tiny": [0.0, 1.0, 2.0],
            "fraction": _FLOAT_FRACTIONS,
            "literal": near or [0.0, 0.5, 1.0],
            "boundary": _FLOAT_BOUNDARY,
            "signed": [-1.0, -0.5, 0.0, 0.5, 1.0],
        }
        if kind == "decimal":
            out = {name: [Decimal(repr(round(v, 6))) for v in values] for name, values in out.items()}
        return out
    if kind == "text":
        derived = set()
        for text in pool.strings:
            derived.update({text, text.upper(), text.lower(), text + " ", text + "x", text[:-1], " " + text})
        for pattern in pool.patterns:
            core = pattern.replace("%", "").replace("_", "")
            derived.update({core, pattern.replace("%", "zz").replace("_", "q"), pattern.replace("%", "").replace("_", "Q"), core.upper(), "q" + core + "q"})
        return {
            "tiny": ["a", "b", "c"],
            "edge": _TEXT_EDGE,
            "literal": sorted(derived) or ["a", "b"],
            "case": ["a", "A", "b", "B", "ab", "AB"],
        }
    if kind in ("date", "timestamp"):
        near = sorted({d + dt.timedelta(days=k) for d in pool.dates | {t.date() for t in pool.timestamps} for k in (-1, 0, 1)})
        near += sorted({d.replace(day=1) for d in pool.dates})
        edge = [_date(t) for t in _DATE_EDGE]
        out = {
            "tiny": [_date("2020-01-01"), _date("2020-01-02"), _date("2020-01-03")],
            "literal": near or [_date("2020-01-01"), _date("2021-01-01")],
            "edge": edge,
        }
        if kind == "timestamp":
            out = {
                name: [dt.datetime.combine(d, dt.time(0)) for d in values] for name, values in out.items()
            }
            out["times"] = [dt.datetime(2020, 1, 1, 0, 0), dt.datetime(2020, 1, 1, 0, 0, 1), dt.datetime(2020, 1, 1, 23, 59, 59), dt.datetime(2020, 1, 2, 12, 0)]
            out["literal"] = out["literal"] + sorted(pool.timestamps) + [t + dt.timedelta(seconds=1) for t in sorted(pool.timestamps)]
        return out
    if kind == "bool":
        return {"tiny": [True, False]}
    if kind == "time":
        return {"tiny": [dt.time(0), dt.time(12), dt.time(23, 59, 59)]}
    raise ValueError(kind)


def _fresh(kind: str, number: int):
    """A value no flavour uses, for redrawing a key that clashes."""

    if kind == "int":
        return 5000 + number
    if kind == "float":
        return 5000.25 + number
    if kind == "decimal":
        return Decimal("5000.25") + number
    if kind == "text":
        return f"k{number}"
    if kind == "date":
        return dt.date(2030, 1, 1) + dt.timedelta(days=number)
    if kind == "timestamp":
        return dt.datetime(2030, 1, 1) + dt.timedelta(hours=number)
    if kind == "time":
        return dt.time(number % 24, (number // 24) % 60, 30)
    return None


_INT_RANGES = {"TINYINT": 2**7, "SMALLINT": 2**15, "INTEGER": 2**31, "INT": 2**31, "INT4": 2**31, "MEDIUMINT": 2**23,
              "UTINYINT": 2**8, "USMALLINT": 2**16, "UINTEGER": 2**32, "UBIGINT": 2**64}


def fits(column: Column, value) -> bool:
    """Whether ``value`` can be stored in ``column``'s DuckDB type."""

    if value is None or isinstance(value, bool):
        return True
    sql_type = column.ddl_type().upper()
    base = sql_type.split("(")[0].strip()
    if isinstance(value, int) and base in _INT_RANGES:
        bound = _INT_RANGES[base]
        return (0 <= value < bound) if base.startswith("U") else (-bound <= value < bound)
    if isinstance(value, int) and base in ("BIGINT", "INT8", "LONG"):
        return -(2**63) <= value < 2**63
    if isinstance(value, (int, float, Decimal)) and base in ("DECIMAL", "NUMERIC") and "(" in sql_type:
        try:
            precision, scale = (int(x) for x in sql_type.split("(")[1].rstrip(")").split(","))
        except ValueError:
            return True
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return abs(number) < Decimal(10) ** (precision - scale) and number == round(number, scale)
    return True


def _default(kind: str):
    return {"int": 0, "float": 0.0, "decimal": Decimal(0), "text": "a", "date": dt.date(2020, 1, 1), "timestamp": dt.datetime(2020, 1, 1), "bool": True, "time": dt.time(0)}[kind]


# --- which tables and columns the queries read ---------------------------------------------------


def _read(case: Case) -> tuple[set[str], set[str], bool]:
    """Tables (by schema name) and lower-case column names the queries mention, and whether a ``*`` reads whole rows."""

    by_lower = {name.lower(): name for name in case.tables}
    tables: set[str] = set()
    columns: set[str] = set()
    star = False
    for query in (case.left, case.right):
        try:
            tree = sqlglot.parse_one(query, read="duckdb")
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            lowered = query.lower()
            tables.update(name for low, name in by_lower.items() if re.search(rf"\b{re.escape(low)}\b", lowered))
            star = True
            continue
        for node in tree.walk():
            if isinstance(node, exp.Table) and node.name.lower() in by_lower:
                tables.add(by_lower[node.name.lower()])
            elif isinstance(node, exp.Column):
                if isinstance(node.this, exp.Star):
                    star = True
                else:
                    columns.add(node.name.lower())
            elif isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
                star = True
            elif isinstance(node, exp.Identifier) and not isinstance(node.parent, (exp.Table, exp.TableAlias)):
                columns.add(node.name.lower())
    return tables, columns, star


def _ancestors(case: Case, tables: set[str]) -> list[str]:
    """``tables`` and every table they reach by a foreign key, parents first."""

    found = set(tables)
    todo = list(tables)
    while todo:
        name = todo.pop()
        for _, parent, _ in case.tables[name].foreign_keys:
            if parent in case.tables and parent not in found:
                found.add(parent)
                todo.append(parent)
    order: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in order or name in visiting:
            return
        visiting.add(name)
        for _, parent, _ in case.tables[name].foreign_keys:
            if parent in found and parent != name:
                visit(parent)
        visiting.discard(name)
        order.append(name)

    for name in sorted(found):
        visit(name)
    return order


# --- legality ------------------------------------------------------------------------------------


def legal(case: Case, data: Database) -> bool:
    """Whether ``data`` meets every declared NOT NULL, key, value list and foreign key, and ``case.legal``."""

    for name, rows in data.items():
        table = case.tables[name]
        for position, column in enumerate(table.columns):
            for row in rows:
                if row[position] is None:
                    if column.not_null:
                        return False
                elif column.values and row[position] not in column.values:
                    return False
        for key in table.keys:
            positions = [table.index(k) for k in key]
            seen = set()
            for row in rows:
                value = tuple(row[p] for p in positions)
                if None in value:
                    continue
                if value in seen:
                    return False
                seen.add(value)
        for columns, parent, parent_columns in table.foreign_keys:
            if parent not in data:
                continue
            ptable = case.tables[parent]
            ppos = [ptable.index(c) for c in parent_columns]
            present = {tuple(r[p] for p in ppos) for r in data[parent]}
            cpos = [table.index(c) for c in columns]
            for row in rows:
                value = tuple(row[p] for p in cpos)
                if None not in value and value not in present:
                    return False
    if case.legal is not None:
        try:
            return bool(case.legal(data))
        except Exception:
            return False
    return True


# --- random databases ----------------------------------------------------------------------------


@dataclass
class Profile:
    """How one random database is drawn."""

    rows: dict[str, int]
    null_rate: float
    duplicates: str  # none, some, heavy
    flavour: dict[str, str]  # kind -> flavour name, or "mix"
    shared: float  # chance a column uses its kind's shared domain
    label: str = "random"


class Generator:
    def __init__(self, case: Case, rng: random.Random):
        self.case = case
        self.rng = rng
        read, self.columns, self.star = _read(case)
        self.read = read
        self.order = _ancestors(case, read)
        self.pool = literal_pool(case.left, case.right)
        self.kinds = sorted({c.kind for name in self.order for c in case.tables[name].columns})
        self.flavours = {kind: _flavours(kind, self.pool) for kind in self.kinds}

    # profile choice
    def _row_count(self) -> int:
        rng = self.rng
        roll = rng.random()
        if roll < 0.07:
            return 0
        if roll < 0.75:
            return rng.choice([1, 2, 2, 3, 3, 4, 5, 6])
        if roll < 0.88 and self.pool.counts:
            n = rng.choice(sorted(self.pool.counts))
            return max(0, min(20, n + rng.choice([-1, 0, 1, 2])))
        return rng.randint(7, 15)

    def random_profile(self) -> Profile:
        rng = self.rng
        flavour = {}
        for kind in self.kinds:
            names = list(self.flavours[kind])
            weights = [3 if n in ("tiny", "literal") else 1 for n in names]
            flavour[kind] = "mix" if rng.random() < 0.2 else rng.choices(names, weights)[0]
        return Profile(
            rows={name: self._row_count() for name in self.order},
            null_rate=rng.choice([0.0, 0.0, 0.1, 0.25, 0.25, 0.5, 0.8]),
            duplicates=rng.choice(["none", "none", "some", "some", "heavy"]),
            flavour=flavour,
            shared=rng.choice([0.6, 0.85, 1.0]),
        )

    def _domain(self, kind: str, flavour: str) -> list:
        sets = self.flavours[kind]
        if flavour == "mix" or flavour not in sets:
            values = sorted({v for vs in sets.values() for v in vs}, key=repr)
        else:
            values = sets[flavour]
        size = self.rng.choice([1, 2, 2, 3, 3, 4, 6])
        return self.rng.sample(values, min(size, len(values)))

    def database(self, profile: Profile) -> Database | None:
        rng, case = self.rng, self.case
        shared = {kind: self._domain(kind, profile.flavour.get(kind, "tiny")) for kind in self.kinds}
        data: Database = {}
        fresh = itertools.count(1)
        for name in self.order:
            table = case.tables[name]
            domains = []
            for column in table.columns:
                if column.values:
                    domain = list(column.values)
                elif rng.random() < profile.shared:
                    domain = shared[column.kind]
                else:
                    flavour = rng.choice(list(self.flavours[column.kind]) + ["mix"])
                    domain = self._domain(column.kind, flavour)
                domain = [v for v in domain if fits(column, v)] or [_default(column.kind)]
                domains.append(domain)
            key_positions = [[table.index(k) for k in key] for key in table.keys]
            key_columns = {p for key in key_positions for p in key}
            fk_positions = []
            for columns, parent, parent_columns in table.foreign_keys:
                if parent not in data and parent != name:
                    continue
                fk_positions.append(([table.index(c) for c in columns], parent, [case.tables[parent].index(c) for c in parent_columns]))
            rows: list[tuple] = []
            seen = [set() for _ in key_positions]
            for _ in range(profile.rows.get(name, 2)):
                for attempt in range(8):
                    if rows and profile.duplicates != "none" and rng.random() < (0.45 if profile.duplicates == "some" else 0.85):
                        row = list(rng.choice(rows))
                        for p in key_columns:
                            row[p] = rng.choice(domains[p]) if attempt == 0 else _fresh(table.columns[p].kind, next(fresh))
                    else:
                        row = []
                        for position, column in enumerate(table.columns):
                            if not column.not_null and rng.random() < profile.null_rate:
                                row.append(None)
                            elif attempt > 0 and position in key_columns and not column.values:
                                row.append(_fresh(column.kind, next(fresh)) if rng.random() < 0.5 else rng.choice(domains[position]))
                            else:
                                row.append(rng.choice(domains[position]))
                    ok = True
                    for positions, parent, parent_positions in fk_positions:
                        parents = rows if parent == name else data.get(parent, [])
                        candidates = [tuple(r[p] for p in parent_positions) for r in parents]
                        candidates = [c for c in candidates if None not in c]
                        nullable = all(not table.columns[p].not_null for p in positions)
                        if parent == name:
                            candidates.append(tuple(row[p] for p in parent_positions))
                        if candidates and (not nullable or rng.random() < 0.85):
                            chosen = rng.choice(candidates)
                            for p, v in zip(positions, chosen):
                                row[p] = v
                        elif nullable:
                            for p in positions:
                                row[p] = None
                        else:
                            ok = False
                    if ok:
                        for positions, parent, parent_positions in fk_positions:  # a key redraw may have touched a foreign key
                            value = tuple(row[p] for p in positions)
                            parents = rows + [tuple(row)] if parent == name else data.get(parent, [])
                            if None not in value and value not in {tuple(r[p] for p in parent_positions) for r in parents}:
                                ok = False
                    if ok:
                        for index, positions in enumerate(key_positions):
                            value = tuple(row[p] for p in positions)
                            if None not in value and value in seen[index]:
                                ok = False
                    if ok:
                        for column, value in zip(table.columns, row):
                            if value is None and column.not_null:
                                ok = False
                    if ok:
                        for index, positions in enumerate(key_positions):
                            value = tuple(row[p] for p in positions)
                            if None not in value:
                                seen[index].add(value)
                        rows.append(tuple(row))
                        break
            data[name] = rows
        return data if legal(case, data) else None

    # edge databases
    def edge_databases(self) -> Iterator[tuple[str, Database]]:
        base = dict(null_rate=0.0, duplicates="none", flavour={k: "tiny" for k in self.kinds}, shared=1.0)
        yield "all empty", {name: [] for name in self.order}
        for name in self.order:
            for rows in (1, 2, 3):
                data = self.database(Profile(rows={n: (0 if n == name else rows) for n in self.order}, **base))
                if data is not None:
                    yield f"{name} empty", data
        for rows in (1, 2):
            for null_rate in (0.0, 1.0, 0.5):
                for duplicates in ("none", "heavy"):
                    for flavour in ("tiny", "literal"):
                        profile = Profile(
                            rows={n: rows for n in self.order}, null_rate=null_rate, duplicates=duplicates,
                            flavour={k: flavour for k in self.kinds}, shared=1.0, label="edge",
                        )
                        for _ in range(3):
                            data = self.database(profile)
                            if data is not None:
                                yield f"edge rows={rows} nulls={null_rate} dup={duplicates} {flavour}", data

    # exhaustive tiny databases
    def _row_options(self, name: str, values: dict[str, list], data: Database) -> list[tuple]:
        """Every row of ``name`` the exhaustive search may use, given the rows already chosen for its parents."""

        case = self.case
        table = case.tables[name]
        key_columns = {k.lower() for key in table.keys for k in key}
        parent_values: dict[int, list] = {}
        for columns, parent, parent_columns in table.foreign_keys:
            if len(columns) == 1 and parent in data and parent != name:
                ptable = case.tables[parent]
                position = ptable.index(parent_columns[0])
                parent_values[table.index(columns[0])] = sorted({r[position] for r in data[parent] if r[position] is not None}, key=repr)
        choices = []
        for position, column in enumerate(table.columns):
            used = self.star or column.name.lower() in self.columns
            if position in parent_values:
                options = list(parent_values[position])
            elif column.values:
                options = list(column.values)[:3]
            elif used:
                options = [v for v in values.get(column.kind, []) if fits(column, v)] or [_default(column.kind)]
            else:
                options = ["__fill__"] if column.name.lower() in key_columns else [_default(column.kind)]
            if not column.not_null and (used or position in parent_values):
                options = [None] + options
            choices.append(options)
        return list(itertools.product(*choices))

    def _fill(self, name: str, rows: tuple) -> list[tuple]:
        table = self.case.tables[name]
        counter = itertools.count(1)
        return [tuple(_fresh(c.kind, next(counter)) if v == "__fill__" else v for c, v in zip(table.columns, row)) for row in rows]

    def exhaustive(self, cap: int, values: dict[str, list]) -> Iterator[Database]:
        """Every database (or ``cap`` random ones) with 0-2 rows per read table (0-3 when that is still few);
        each read column draws from NULL and ``values[kind]``, a foreign key from its parent's chosen values."""

        rng = self.rng
        enumerated = [n for n in self.order if n in self.read]
        if not enumerated:
            return
        estimate = {}
        for most in (3, 2):
            total = 1
            for name in enumerated:
                r = len(self._row_options(name, values, {}))
                total *= sum(math.comb(r + k - 1, k) for k in range(most + 1))
            estimate[most] = total
        most = 3 if estimate[3] <= cap else 2

        def complete(data: Database) -> Database | None:
            out = dict(data)
            self._complete(out)
            return out if legal(self.case, out) else None

        if estimate[most] <= cap:
            def walk(index: int, data: Database) -> Iterator[Database]:
                if index == len(enumerated):
                    yield data
                    return
                name = enumerated[index]
                for rows in _multisets(self._row_options(name, values, data), most):
                    filled = self._fill(name, rows)
                    if legal(self.case, {name: filled}):
                        yield from walk(index + 1, {**data, name: filled})

            for data in walk(0, {}):
                done = complete(data)
                if done is not None:
                    yield done
            return
        seen = set()
        for _ in range(cap):
            data: Database = {}
            for name in enumerated:
                options = self._row_options(name, values, data)
                data[name] = self._fill(name, _random_multiset(options, most, rng)) if options else []
            signature = repr(sorted(data.items()))
            if signature in seen:
                continue
            seen.add(signature)
            done = complete(data)
            if done is not None:
                yield done

    def _complete(self, data: Database) -> bool:
        """Add rows to unread parent tables so every foreign key of the read tables finds its parent."""

        case = self.case
        for name in reversed(self.order):
            if name in data:
                continue
            table = case.tables[name]
            needed: set[tuple] = set()
            for child, rows in data.items():
                for columns, parent, parent_columns in case.tables[child].foreign_keys:
                    if parent != name:
                        continue
                    positions = [case.tables[child].index(c) for c in columns]
                    for row in rows:
                        value = tuple(row[p] for p in positions)
                        if None not in value:
                            needed.add((tuple(c.lower() for c in parent_columns), value))
            out = []
            counter = itertools.count(100)
            for columns, value in sorted(needed, key=repr):
                row = [_default(c.kind) if c.not_null or c.name.lower() in {k.lower() for key in table.keys for k in key} else None for c in table.columns]
                for position, column in enumerate(table.columns):
                    if any(column.name.lower() in key for key in [tuple(k.lower() for k in key) for key in table.keys]):
                        row[position] = _fresh(column.kind, next(counter))
                for column_name, v in zip(columns, value):
                    row[table.index(column_name)] = v
                out.append(tuple(row))
            data[name] = out
        return True


def _multisets(rows: list, most: int) -> Iterator[tuple]:
    for size in range(most + 1):
        yield from itertools.combinations_with_replacement(rows, size)


def _random_multiset(rows: list, most: int, rng: random.Random) -> tuple:
    size = rng.randint(0, most)
    return tuple(rng.choice(rows) for _ in range(size))


# --- running and comparing -----------------------------------------------------------------------


def normalize(value: Any, digits: int = 9) -> Any:
    """One spelling per value: numbers as decimals rounded to ``digits`` significant digits, TRUE as 1,
    a timestamp at midnight as its date, nested values element-wise."""

    if value is None:
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return Decimal(format(value, f".{digits}g"))
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return Decimal(format(value, f".{digits}g"))
    if isinstance(value, dt.datetime):
        if value.tzinfo is None and value.time() == dt.time(0):
            return value.date()
        return value
    if isinstance(value, (list, tuple)):
        return tuple(normalize(v, digits) for v in value)
    if isinstance(value, dict):
        return tuple((k, normalize(v, digits)) for k, v in value.items())
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def bag(rows: Iterable[tuple], digits: int = 9) -> Counter:
    return Counter(tuple(normalize(v, digits) for v in row) for row in rows)


def view(rows: list[tuple], mode: str, digits: int):
    """What ``mode`` compares of a result: its bag, its set or its row sequence."""

    if mode == "list":
        return [tuple(normalize(v, digits) for v in row) for row in rows]
    counted = bag(rows, digits)
    return set(counted) if mode in ("set", "contained-set") else counted


def _float_columns(*row_sets: Iterable[tuple]) -> set[int]:
    """Positions holding a float in either result: an exact integer there may be the same number as a double."""

    return {i for rows in row_sets for row in rows for i, value in enumerate(row) if isinstance(value, float)}


def _as_floats(rows: list[tuple], columns: set[int]) -> list[tuple]:
    """``rows`` with the integers in ``columns`` read as doubles, as an engine that averages in DOUBLE returns
    them (2^53 + 1 comes back as 2^53). Only the float-noise check uses this; the main comparison keeps integers
    exact."""

    if not columns:
        return rows
    return [
        tuple(float(v) if i in columns and isinstance(v, int) and not isinstance(v, bool) else v for i, v in enumerate(row))
        for row in rows
    ]


def agree(mode: str, left, right) -> bool:
    """Whether two views (see ``view``) satisfy ``mode``."""

    if mode == "contained":
        return all(right.get(row, 0) >= count for row, count in left.items())
    if mode == "contained-set":
        return left <= right
    return left == right


class QueryError(Exception):
    pass


def quoted(name: str) -> str:
    """A table name as DuckDB reads it; a dotted name is a catalog and schema path."""

    return ".".join('"' + part.replace('"', '""') + '"' for part in name.split("."))


class Runner:
    """One DuckDB connection holding the case's tables."""

    def __init__(self, case: Case, query_seconds: float = 10.0):
        import duckdb

        self.duckdb = duckdb
        self.case = case
        self.query_seconds = query_seconds
        self.db = duckdb.connect(":memory:")
        self.db.execute("SET threads = 1")
        for statement in case.setup:
            self.db.execute(statement)
        made: set[str] = set()
        for table in case.tables.values():
            parts = table.name.split(".")
            if len(parts) == 3 and parts[0] not in made:
                self.db.execute(f"ATTACH ':memory:' AS {quoted(parts[0])}")
                made.add(parts[0])
            if len(parts) >= 2 and ".".join(parts[:-1]) not in made:
                self.db.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted('.'.join(parts[:-1]))}")
                made.add(".".join(parts[:-1]))
            columns = ", ".join(f'"{c.name}" {c.ddl_type()}' for c in table.columns)
            self.db.execute(f"CREATE TABLE {quoted(table.name)} ({columns})")
        self.loaded: set[str] = set()

    def close(self) -> None:
        self.db.close()

    def load(self, data: Database) -> None:
        from kumosql.duckdb_load import insert_rows

        for name in self.loaded | set(data):
            self.db.execute(f"DELETE FROM {quoted(name)}")
        self.loaded = set()
        for name, rows in data.items():
            self.loaded.add(name)
            try:
                insert_rows(self.db, quoted(name), rows)
            except self.duckdb.Error as error:
                raise QueryError(f"load: {str(error).splitlines()[0][:200]}") from None

    def run(self, sql: str) -> list[tuple]:
        timer = threading.Timer(self.query_seconds, self.db.interrupt)
        timer.start()
        try:
            return self.db.execute(sql).fetchall()
        except self.duckdb.Error as error:
            raise QueryError(f"{type(error).__name__}: {str(error).splitlines()[0][:300]}") from None
        finally:
            timer.cancel()

    def unoptimized(self) -> tuple[list[tuple], list[tuple]]:
        from kumosql.duckdb_load import run_unoptimized

        timer = threading.Timer(self.query_seconds * 2, self.db.interrupt)
        timer.start()
        try:
            left, right = run_unoptimized(self.db, self.case.left, self.case.right)
        except self.duckdb.Error as error:
            raise QueryError(f"{type(error).__name__}: {str(error).splitlines()[0][:300]}") from None
        finally:
            timer.cancel()
        return left, right


@dataclass
class Outcome:
    kind: str  # same, differs, error (one side only), both-error
    left: list | None = None
    right: list | None = None
    error: str = ""


def compare(runner: Runner, data: Database) -> Outcome:
    case = runner.case
    try:
        runner.load(data)
    except QueryError as error:
        return Outcome("load-error", error=str(error))
    errors = []
    results = []
    for sql in (case.left, case.right):
        try:
            results.append(runner.run(sql))
            errors.append("")
        except QueryError as error:
            results.append(None)
            errors.append(str(error))
    if results[0] is None and results[1] is None:
        return Outcome("both-error", error=errors[0] + " | " + errors[1])
    if results[0] is None or results[1] is None:
        return Outcome("error", results[0], results[1], error=errors[0] or errors[1])
    if agree(case.mode, view(results[0], case.mode, case.float_digits), view(results[1], case.mode, case.float_digits)):
        return Outcome("same", results[0], results[1])
    return Outcome("differs", results[0], results[1])


def _shuffled(data: Database, rng: random.Random, reverse: bool) -> Database:
    out = {}
    for name, rows in data.items():
        rows = list(rows)
        if reverse:
            rows.reverse()
        else:
            rng.shuffle(rows)
        out[name] = rows
    return out


def confirm(runner: Runner, data: Database, outcome: Outcome, rng: random.Random) -> str:
    """``differs`` if the difference is real, else why not: ``optimizer``, ``nondeterministic``, ``float-noise``."""

    case = runner.case
    mode, digits = case.mode, case.float_digits
    left, right = view(outcome.left, mode, digits), view(outcome.right, mode, digits)
    floats = _float_columns(outcome.left, outcome.right)
    if agree(mode, view(_as_floats(outcome.left, floats), mode, 6), view(_as_floats(outcome.right, floats), mode, 6)):
        return "float-noise"
    runner.load(data)
    try:
        u_left, u_right = runner.unoptimized()
    except QueryError:
        return "optimizer"
    if view(u_left, mode, digits) != left or view(u_right, mode, digits) != right:
        return "optimizer"
    for order in _orders(data, rng):
        again = compare(runner, order)
        if again.kind != "differs" or view(again.left, mode, digits) != left or view(again.right, mode, digits) != right:
            return "nondeterministic"
    from .ties import tie_dependent  # imported here: ties reads the engine's own helpers

    if tie_dependent(runner, data):  # an engine's pick among tied rows under LIMIT, stable under reordering
        return "nondeterministic"
    return "differs"


# How often a database on which the optimized results agree is also run with the optimizer off (every edge
# database is). The unoptimized run doubles a database's cost, so the random ones are sampled.
UNOPTIMIZED_EVERY = 5


def masked(runner: Runner, data: Database, rng: random.Random) -> tuple[list, list] | None:
    """The unoptimized results of the loaded database when they differ although the optimized ones agree.

    DuckDB's optimizer can return wrong rows, and wrong rows can make two different queries agree as easily as they
    make two equal ones differ. ``None`` when the unoptimized results agree too, differ only by float noise, or
    change when the tables' rows are stored in another order (a pick among tied rows is not evidence)."""

    case = runner.case
    mode, digits = case.mode, case.float_digits
    try:
        left, right = runner.unoptimized()
        if agree(mode, view(left, mode, digits), view(right, mode, digits)):
            return None
        floats = _float_columns(left, right)
        if agree(mode, view(_as_floats(left, floats), mode, 6), view(_as_floats(right, floats), mode, 6)):
            return None
        for order in _orders(data, rng):
            runner.load(order)
            again = runner.unoptimized()
            if view(again[0], mode, digits) != view(left, mode, digits) or view(again[1], mode, digits) != view(right, mode, digits):
                return None
        from .ties import tie_dependent

        if tie_dependent(runner, data):
            return None
    except QueryError:
        return None
    return left, right


def _orders(data: Database, rng: random.Random) -> Iterator[Database]:
    """The same database with each table's rows reversed, rotated so each row comes first, and shuffled."""

    yield _shuffled(data, rng, True)
    longest = max((len(rows) for rows in data.values()), default=0)
    for shift in range(1, min(longest, 6)):
        yield {name: list(rows[shift % len(rows):]) + list(rows[: shift % len(rows)]) if rows else [] for name, rows in data.items()}
    for _ in range(3):
        yield _shuffled(data, rng, False)


def shrink(runner: Runner, data: Database, rng: random.Random, seconds: float = 20.0) -> Database:
    """Drop rows one at a time while the queries still differ (confirmed) and the database stays legal."""

    case = runner.case
    deadline = time.time() + seconds
    current = {name: list(rows) for name, rows in data.items()}
    changed = True
    while changed and time.time() < deadline:
        changed = False
        for name in sorted(current, key=lambda n: -len(current[n])):
            index = 0
            while index < len(current[name]) and time.time() < deadline:
                trial = {n: list(rows) for n, rows in current.items()}
                del trial[name][index]
                if legal(case, trial):
                    outcome = compare(runner, trial)
                    if outcome.kind == "differs" and confirm(runner, trial, outcome, rng) == "differs":
                        current = trial
                        changed = True
                        continue
                index += 1
    # simpler values: NULL out a nullable cell, when the difference survives
    for name, rows in current.items():
        table = case.tables[name]
        for r in range(len(rows)):
            for position, column in enumerate(table.columns):
                if time.time() > deadline or rows[r][position] is None or column.not_null:
                    continue
                trial = {n: list(rs) for n, rs in current.items()}
                row = list(trial[name][r])
                row[position] = None
                trial[name][r] = tuple(row)
                if legal(case, trial):
                    outcome = compare(runner, trial)
                    if outcome.kind == "differs" and confirm(runner, trial, outcome, rng) == "differs":
                        current = trial
                        rows = current[name]
    return {name: rows for name, rows in current.items()}


# --- the search ----------------------------------------------------------------------------------

_PICKS = {"ANY_VALUE", "ANYVALUE", "FIRST", "LAST", "ARBITRARY", "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE"}
_ORDERED_AGGREGATES = {"ARRAY_AGG", "ARRAYAGG", "STRING_AGG", "GROUP_CONCAT", "GROUPCONCAT", "LIST", "LISTAGG"}
_RANKS = {"ROW_NUMBER", "ROWNUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD", "PERCENT_RANK", "CUME_DIST"}


def order_sensitive(*queries: str) -> list[str]:
    """Constructs whose result may depend on row order or on the engine's choice (a difference found
    with them needs a second look, even after the row-order checks)."""

    found: set[str] = set()
    for query in queries:
        try:
            tree = sqlglot.parse_one(query, read="duckdb")
        except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
            found.add("unparsed")
            continue
        for node in tree.walk():
            name = (node.sql_name() if isinstance(node, exp.Func) else "").upper()
            if isinstance(node, exp.Anonymous):
                name = str(node.this).upper()
            if name in _PICKS:
                found.add(name.lower())
            elif name in _ORDERED_AGGREGATES and not node.find(exp.Order):
                found.add(f"unordered {name.lower()}")
            elif name in _RANKS:
                found.add(name.lower())
            elif isinstance(node, (exp.Limit, exp.Offset, exp.Fetch)):
                found.add("limit")
            elif isinstance(node, exp.Distinct) and node.args.get("on"):
                found.add("distinct on")
            elif name in ("RAND", "RANDOM", "UUID", "GEN_RANDOM_UUID", "NOW", "CURRENT_TIMESTAMP", "CURRENT_DATE"):
                found.add("volatile")
    return sorted(found)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def _rows(rows: list | None, most: int = 30) -> list:
    if rows is None:
        return []
    return [[_json_value(v) for v in row] for row in rows[:most]]


def database_json(data: Database) -> dict:
    return {name: _rows(rows, 1000) for name, rows in data.items()}


def _anchors(generator: Generator, kind: str) -> list:
    pool = generator.pool
    if kind == "int":
        return sorted(pool.ints)
    if kind in ("float", "decimal"):
        values = sorted({float(v) for v in pool.ints | pool.floats})
        return [Decimal(repr(v)) for v in values] if kind == "decimal" else values
    if kind == "text":
        found = set(pool.strings)
        for pattern in pool.patterns:
            found.add(pattern.replace("%", "").replace("_", "q"))
        return sorted(found)
    if kind == "date":
        return sorted(pool.dates)
    if kind == "timestamp":
        return sorted(pool.timestamps) + [dt.datetime.combine(d, dt.time(0)) for d in sorted(pool.dates)]
    return []


def _neighbour(kind: str, anchor, rng: random.Random):
    if kind == "int":
        return anchor + rng.choice([-1, 1])
    if kind == "float":
        return anchor + rng.choice([-0.5, 0.5])
    if kind == "decimal":
        return anchor + rng.choice([Decimal("-0.5"), Decimal("0.5")])
    if kind == "text":
        return rng.choice(["a", anchor + "x", anchor.swapcase() if anchor.swapcase() != anchor else anchor + " ", ""])
    if kind == "date":
        return anchor + dt.timedelta(days=rng.choice([-1, 1]))
    if kind == "timestamp":
        return anchor + rng.choice([dt.timedelta(seconds=1), dt.timedelta(days=-1)])
    return anchor


def _exhaustive_values(generator: Generator, variant: int) -> dict[str, list]:
    """Two values per kind: small generic ones (variant 0), or a literal of the queries and its neighbour."""

    rng = generator.rng
    out = {}
    for kind in generator.kinds:
        tiny = generator.flavours[kind]["tiny"]
        anchors = _anchors(generator, kind)
        if variant == 0 or not anchors:
            out[kind] = tiny[:2] if variant == 0 else tiny[1:3] or tiny[:2]
        else:
            anchor = rng.choice(anchors)
            out[kind] = sorted({anchor, _neighbour(kind, anchor, rng)}, key=repr)
    return out


def recheck(case: Case, *, budget: int = 3000, seconds: float = 240.0, seed: int = 0, exhaustive_cap: int = 1500) -> dict:
    """Search for a database separating ``case.left`` and ``case.right``; a JSON-ready record."""

    start = time.time()
    rng = random.Random(f"{seed}:{case.eval}:{case.pair}")
    record: dict[str, Any] = {"eval": case.eval, "pair": case.pair, "held_out": case.held_out, "verdict": "survived", "dbs": 0}
    sensitive = order_sensitive(case.left, case.right)
    if sensitive:
        record["order_sensitive"] = sensitive
    try:
        runner = Runner(case)
    except Exception as error:  # a schema DuckDB cannot create
        record.update(verdict="unrunnable", error=f"schema: {error}"[:400])
        return record
    try:
        generator = Generator(case, rng)
        notes: Counter = Counter()
        first_error: dict[str, str] = {}
        both_error = 0

        def databases() -> Iterator[tuple[str, Database]]:
            yield from generator.edge_databases()
            yield from (("exhaustive tiny", d) for d in generator.exhaustive(exhaustive_cap, _exhaustive_values(generator, 0)))
            for variant in (1, 2):
                yield from (("exhaustive literal", d) for d in generator.exhaustive(exhaustive_cap // 3, _exhaustive_values(generator, variant)))
            while True:
                data = generator.database(generator.random_profile())
                if data is not None:
                    yield "random", data

        for label, data in databases():
            if record["dbs"] >= budget or time.time() - start > seconds:
                break
            record["dbs"] += 1
            outcome = compare(runner, data)
            if outcome.kind == "same":
                if label != "random" and not label.startswith("exhaustive") or record["dbs"] % UNOPTIMIZED_EVERY == 0:
                    hidden = masked(runner, data, rng)
                    if hidden is not None:
                        # Not ``differs``: the two plans disagree about the pair, so one of them is wrong. It is not
                        # ``survived`` either; the pair needs a reading (docs/proof-recheck.md).
                        record.update(
                            verdict="masked", profile=label,
                            witness={"database": database_json(data), "left": _rows(hidden[0]), "right": _rows(hidden[1])},
                        )
                        break
                continue
            if outcome.kind == "load-error":
                notes["load-error"] += 1
                first_error.setdefault("load", outcome.error)
                continue
            if outcome.kind == "both-error":
                both_error += 1
                first_error.setdefault("both", outcome.error)
                if both_error >= 25 and both_error == record["dbs"]:
                    record.update(verdict="unrunnable", error=outcome.error[:400])
                    return record
                continue
            if outcome.kind == "error":
                notes["one-side-error"] += 1
                if "one-side" not in first_error:
                    first_error["one-side"] = outcome.error
                    record["error_witness"] = {"database": database_json(data), "left": _rows(outcome.left), "right": _rows(outcome.right), "error": outcome.error[:400]}
                continue
            verdict = confirm(runner, data, outcome, rng)
            if verdict != "differs":
                notes[verdict] += 1
                if verdict not in first_error:
                    first_error[verdict] = label
                    record.setdefault("unconfirmed", {})[verdict] = {"database": database_json(data), "left": _rows(outcome.left), "right": _rows(outcome.right)}
                continue
            small = shrink(runner, data, rng)
            final = compare(runner, small)
            if final.kind != "differs" or confirm(runner, small, final, rng) != "differs":
                small, final = data, outcome
            record.update(
                verdict="differs",
                profile=label,
                witness={"database": database_json(small), "left": _rows(final.left), "right": _rows(final.right)},
            )
            break
        if record["verdict"] == "survived" and both_error and both_error == record["dbs"]:
            record.update(verdict="unrunnable", error=first_error.get("both", "")[:400])
        if notes:
            record["notes"] = dict(notes)
        if first_error:
            record["first"] = {k: v[:300] for k, v in first_error.items()}
    except Exception as error:  # a bug in the search must not read as a survived proof
        record.update(verdict="search-error", error=f"{type(error).__name__}: {error}"[:400])
    finally:
        runner.close()
        record["seconds"] = round(time.time() - start, 2)
    return record
