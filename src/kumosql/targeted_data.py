"""Databases built to tell a query from a faulty variant of itself.

Random rows rarely land on the one value where ``<`` and ``<=`` part, give a join
partner to every row, or leave a table empty. This module reads the query and
builds databases that do, in the spirit of XData (Chandra et al., "Data
generation for testing and grading SQL queries", VLDB J. 2015); no XData code
or data is used:

* for each comparison, ``BETWEEN``, ``IN`` and ``LIKE`` against a constant, rows
  just below, at and just above the constant;
* join and grouping columns draw from small pools shared across equated
  columns, so joins match and groups hold several rows;
* each referenced table empty in turn, every row NULL, every row doubled, one
  value everywhere (all rows join, one group);
* ordinary random rows drawn from the same pools.

Every dataset respects the declared NOT NULL columns and keys (``DataRules``).
``database_suite`` is the fixed list of databases a query is checked on; its
labels say which scenario each one is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import random
import re
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from .result_equivalence import (
    _DOMAINS,
    DataRules,
    Schema,
    SyntheticDataset,
    SyntheticTable,
    _normalize_type,
    generate_synthetic_dataset,
    respect_rules,
)

_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


@dataclass(frozen=True)
class LabeledDataset:
    label: str
    dataset: SyntheticDataset

    @property
    def rows(self) -> int:
        return sum(len(t.rows) for t in self.dataset.tables.values())


def _coerce(text: str, col_type: str) -> Any:
    try:
        if col_type == "INT64":
            return int(float(text))
        if col_type == "FLOAT64":
            return float(text)
        if col_type == "NUMERIC":
            return Decimal(text)
        if col_type == "STRING":
            return text
        if col_type == "DATE":
            return date.fromisoformat(text[:10])
        if col_type == "TIMESTAMP":
            return datetime.fromisoformat(text.replace("T", " ")[:19])
        if col_type == "BOOL":
            return text.lower() in ("true", "1")
    except (ValueError, ArithmeticError):
        return None
    return None


def _neighbours(value: Any, col_type: str) -> list[Any]:
    """The constant and the nearest values on each side of it."""

    if value is None:
        return []
    if col_type == "INT64":
        return [value - 1, value, value + 1]
    if col_type == "FLOAT64":
        return [value - 0.5, value, value + 0.5]
    if col_type == "NUMERIC":
        return [value - Decimal("0.5"), value, value + Decimal("0.5")]
    if col_type == "STRING" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)  # a date kept in a text column: neighbours are dates too
        return [(day - timedelta(days=1)).isoformat(), value, (day + timedelta(days=1)).isoformat()]
    if col_type == "STRING" and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        moment = datetime.fromisoformat(value)
        return [(moment + timedelta(hours=h)).isoformat(sep=" ") for h in (-1, 0, 1)]
    if col_type == "STRING":
        return [value, value + "x", value[:-1] if value else "x", value.upper() if value.lower() == value else value.lower()]
    if col_type == "DATE":
        return [value - timedelta(days=1), value, value + timedelta(days=1)]
    if col_type == "TIMESTAMP":
        return [value - timedelta(hours=1), value, value + timedelta(hours=1)]
    return [True, False]


def _passing(values: list[Any], col_type: str, op) -> Any:
    """A value among ``values`` (a constant and its neighbours) for which ``column op constant`` holds."""

    if col_type == "BOOL":
        return None
    low, mid, high = (values + [values[-1]] * 3)[:3] if col_type != "STRING" or len(values) == 3 else (values[2], values[0], values[1])
    return {exp.EQ: mid, exp.GTE: mid, exp.LTE: mid, exp.GT: high, exp.LT: low, exp.NEQ: high}.get(op, mid)


def _like_values(pattern: str) -> list[str]:
    core = re.sub(r"[%_]", "", pattern)
    return [core, re.sub(r"[%_]", "z", pattern), re.sub(r"[%_]", "", pattern) + "q", "q" + core]


class _Facts:
    """What a query says about its columns, by lower-case column name."""

    def __init__(self, sql: str, schema: Schema, dialect: str):
        self.types: dict[str, str] = {}
        for columns in schema.values():
            for name, bq_type in columns.items():
                self.types.setdefault(name.lower(), _normalize_type(bq_type))
        self.constants: dict[str, list[list[Any]]] = {}  # column -> groups of boundary values
        self.parent: dict[str, str] = {}
        self.nullable_tests: set[str] = set()
        self.satisfy: dict[str, Any] = {}  # column -> a value that passes a predicate on it
        self._pools: dict[str, list[Any]] = {}
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.SqlglotError:
            return
        for node in tree.find_all(*_COMPARISONS):
            self._comparison(node)
        for node in tree.find_all(exp.Between):
            for bound, op in ((node.args.get("low"), exp.GTE), (node.args.get("high"), exp.LTE)):
                self._constant_against(bound, node.this, op)
        for node in tree.find_all(exp.In):
            for item in node.expressions:
                self._constant_against(item, node.this, exp.EQ)
        for node in tree.find_all(exp.Like, exp.ILike):
            pattern = node.expression
            if isinstance(pattern, exp.Literal) and pattern.is_string:
                for column in node.this.find_all(exp.Column):
                    self.constants.setdefault(column.name.lower(), []).append(_like_values(pattern.name))
                    if isinstance(node.this, exp.Column):
                        self.satisfy.setdefault(column.name.lower(), _like_values(pattern.name)[0])
        for node in tree.find_all(exp.Is):
            for column in node.this.find_all(exp.Column):
                self.nullable_tests.add(column.name.lower())

    def _find(self, name: str) -> str:
        while self.parent.get(name, name) != name:
            name = self.parent[name]
        return name

    def _union(self, a: str, b: str) -> None:
        self.parent[self._find(a)] = self._find(b)

    def _comparison(self, node: exp.Expression) -> None:
        left, right = node.this, node.expression
        left_cols, right_cols = list(left.find_all(exp.Column)), list(right.find_all(exp.Column))
        if isinstance(node, exp.EQ) and isinstance(left, exp.Column) and isinstance(right, exp.Column):
            self._union(left.name.lower(), right.name.lower())
            return
        if left_cols and not right_cols:
            self._constant_against(right, left, type(node))
        elif right_cols and not left_cols:
            flipped = {exp.LT: exp.GT, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.GTE: exp.LTE}.get(type(node), type(node))
            self._constant_against(left, right, flipped)

    def _constant_against(self, constant: exp.Expression | None, side: exp.Expression, op=None) -> None:
        if constant is None:
            return
        literals = [lit for lit in constant.find_all(exp.Literal)] or ([constant] if isinstance(constant, exp.Literal) else [])
        if not literals:
            return
        for column in side.find_all(exp.Column):
            name = column.name.lower()
            col_type = self.types.get(name)
            if col_type is None:
                continue
            value = _coerce(literals[0].name, col_type)
            values = _neighbours(value, col_type)
            if values:
                self.constants.setdefault(name, []).append(values)
                if isinstance(side, exp.Column):
                    passing = _passing(values, col_type, op)
                    if passing is not None:
                        self.satisfy.setdefault(name, passing)

    def pool(self, name: str) -> list[Any]:
        """Values for column ``name``: boundary values of every constant it meets and its join partners."""

        cached = self._pools.get(name)
        if cached is not None:
            return cached
        col_type = self.types[name]
        root = self._find(name)
        values: list[Any] = []
        for member in self.types:
            if self._find(member) == root:
                for group in self.constants.get(member, []):
                    values.extend(group)
        out: list[Any] = []
        for value in values + list(_DOMAINS[col_type][:3]):
            if value is not None and value not in out:
                out.append(value)
        self._pools[name] = out
        return out

    def is_joined(self, name: str) -> bool:
        root = self._find(name)
        return sum(1 for n in self.types if self._find(n) == root) > 1

    def predicate_groups(self) -> list[tuple[str, list[Any]]]:
        return [(name, group) for name in sorted(self.constants) for group in self.constants[name]]


def _columns(schema: Schema, key: str) -> tuple[tuple[str, str], ...]:
    return tuple((name, _normalize_type(t)) for name, t in schema[key].items())


def _referenced(sql: str, schema: Schema, dialect: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return sorted(schema)
    lowered = {key.lower(): key for key in schema}
    names = []
    for table in tree.find_all(exp.Table):
        key = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
        if key in lowered and lowered[key] not in names:
            names.append(lowered[key])
    return sorted(names) or sorted(schema)


def _build_rows(
    key: str,
    columns: tuple[tuple[str, str], ...],
    facts: _Facts,
    rules: Mapping[str, DataRules] | None,
    rng: random.Random,
    count: int,
    *,
    null_rate: float,
    fixed_rows: list[Mapping[str, Any]] | None = None,
    single_value: bool = False,
    pool_bias: float = 0.9,
) -> list[tuple]:
    table_rules = rules.get(key.lower()) if rules else None
    required = table_rules.not_null if table_rules else frozenset()
    key_columns = {c for k in (table_rules.keys if table_rules else ()) for c in k}
    rows = []
    offset = rng.randrange(8)
    if fixed_rows is not None:
        count = len(fixed_rows)
    for index in range(count):
        fixed = fixed_rows[index] if fixed_rows is not None else None
        row = []
        for name, col_type in columns:
            lowered = name.lower()
            if fixed and lowered in fixed:
                value = fixed[lowered]
            else:
                pool = facts.pool(lowered)
                if single_value:
                    value = pool[0]
                elif lowered in key_columns:
                    value = pool[(index + offset) % len(pool)]
                elif rng.random() < pool_bias:
                    value = rng.choice(pool)
                else:
                    value = rng.choice(_DOMAINS[col_type])
                if lowered not in required and lowered not in key_columns and rng.random() < null_rate:
                    value = None
            row.append(value)
        rows.append(tuple(row))
    return respect_rules(columns, rows, table_rules)


def _dataset(
    seed: int,
    schema: Schema,
    names: list[str],
    facts: _Facts,
    rules: Mapping[str, DataRules] | None,
    rng: random.Random,
    *,
    sizes: Mapping[str, int] | None = None,
    default_size: tuple[int, int] = (1, 4),
    null_rate: float = 0.1,
    overrides: Mapping[str, list[tuple]] | None = None,
    double: bool = False,
    single_value: bool = False,
) -> SyntheticDataset:
    tables: dict[str, SyntheticTable] = {}
    for key in sorted(schema):
        columns = _columns(schema, key)
        if key not in names:
            tables[key] = SyntheticTable(columns, ())
            continue
        if overrides and key in overrides:
            rows = overrides[key]
        else:
            count = sizes[key] if sizes and key in sizes else rng.randint(*default_size)
            rows = _build_rows(key, columns, facts, rules, rng, count, null_rate=null_rate, single_value=single_value)
        table_rules = rules.get(key.lower()) if rules else None
        if double and rows and not (table_rules and table_rules.keys):
            rows = list(rows) + list(rows)
        tables[key] = SyntheticTable(columns, tuple(rows))
    return SyntheticDataset(seed=seed, tables=tables)


def edge_datasets(
    schema: Schema, rules: Mapping[str, DataRules] | None = None
) -> list[LabeledDataset]:
    """Query-independent corner cases: no rows, one row, every value NULL."""

    facts = _Facts("", schema, "bigquery")
    names = sorted(schema)
    rng = random.Random(0)
    out = [
        LabeledDataset("empty", _dataset(0, schema, [], facts, rules, rng)),
        LabeledDataset("single_row", _dataset(1, schema, names, facts, rules, rng, sizes={n: 1 for n in names}, null_rate=0.0)),
    ]
    nulls: dict[str, list[tuple]] = {}
    for key in names:
        columns = _columns(schema, key)
        table_rules = rules.get(key.lower()) if rules else None
        required = (table_rules.not_null if table_rules else frozenset()) | {
            c for k in (table_rules.keys if table_rules else ()) for c in k
        }
        row = tuple(
            facts.pool(n.lower())[0] if n.lower() in required else None for n, _ in columns
        )
        nulls[key] = [row]
    out.append(LabeledDataset("all_null", _dataset(2, schema, names, facts, rules, rng, overrides=nulls)))
    return out


def targeted_datasets(
    sql: str,
    schema: Schema,
    rules: Mapping[str, DataRules] | None = None,
    *,
    dialect: str = "bigquery",
    random_count: int = 12,
) -> list[LabeledDataset]:
    """Databases designed around ``sql``'s predicates, joins and groups."""

    facts = _Facts(sql, schema, dialect)
    names = _referenced(sql, schema, dialect)
    rng = random.Random(1)
    out: list[LabeledDataset] = []
    seed = 100

    def add(label: str, **kwargs) -> None:
        nonlocal seed
        seed += 1
        out.append(LabeledDataset(label, _dataset(seed, schema, names, facts, rules, rng, **kwargs)))

    for key in names:
        add(f"empty:{key}", overrides={key: []})
    # NULL in the matching columns of every table at once: where INTERSECT and a join part ways.
    null_overrides = {}
    for key in names:
        columns = _columns(schema, key)
        table_rules = rules.get(key.lower()) if rules else None
        pinned = (table_rules.not_null if table_rules else frozenset()) | {
            c for k in (table_rules.keys if table_rules else ()) for c in k
        }
        shared = {n.lower() for n, _ in columns if facts.is_joined(n.lower())} or {n.lower() for n, _ in columns}
        null_row = tuple(None if n.lower() in shared and n.lower() not in pinned else facts.pool(n.lower())[0] for n, _ in columns)
        match_row = tuple(facts.pool(n.lower())[0] for n, _ in columns)
        null_overrides[key] = respect_rules(columns, [null_row, match_row, null_row], table_rules)
    add("null_keys", overrides=null_overrides)
    add("one_value", single_value=True, default_size=(3, 3), null_rate=0.0)
    add("doubled", double=True, null_rate=0.0)
    add("small_groups", default_size=(4, 6), null_rate=0.0)
    add("sparse_nulls", default_size=(3, 5), null_rate=0.4)
    # Rows on both sides of every constant a predicate compares against.
    for index, (column, values) in enumerate(facts.predicate_groups()):
        holders = [k for k in names if any(c.lower() == column for c in schema[k])]
        overrides = {}
        for key in holders:
            columns = _columns(schema, key)
            others = {c: v for c, v in facts.satisfy.items() if c != column and any(n.lower() == c for n, _ in columns)}
            fixed = [{**others, column: value} for value in values]
            if _nullable(rules, key, column):
                fixed.append({**others, column: None})
            rows = _build_rows(key, columns, facts, rules, rng, 0, null_rate=0.0, fixed_rows=fixed)
            overrides[key] = rows
        add(f"boundary:{column}:{index}", overrides=overrides, null_rate=0.0)
    for index in range(random_count):
        add(f"targeted_random:{index}", default_size=(1, 5), null_rate=0.12)
    return out


def isolated_boundary_datasets(
    sql: str,
    schema: Schema,
    rules: Mapping[str, DataRules] | None = None,
    *,
    dialect: str = "bigquery",
) -> list[LabeledDataset]:
    """Each constant's boundary rows in one table at a time, every other referenced table empty.

    Facts are kept by column name, so ``targeted_datasets`` puts a constant's boundary rows into
    every table with a column of that name: for ``t.a >= 3 AND NOT EXISTS (SELECT 1 FROM u WHERE
    u.a = t.a)`` the ``u`` partner is always there and the row at the boundary never survives. Here
    it stands alone. Only columns held by two or more referenced tables get these databases.
    """

    facts = _Facts(sql, schema, dialect)
    names = _referenced(sql, schema, dialect)
    rng = random.Random(2)
    out: list[LabeledDataset] = []
    for index, (column, values) in enumerate(facts.predicate_groups()):
        holders = [k for k in names if any(c.lower() == column for c in schema[k])]
        if len(holders) < 2:
            continue
        for key in holders:
            columns = _columns(schema, key)
            others = {c: v for c, v in facts.satisfy.items() if c != column and any(n.lower() == c for n, _ in columns)}
            fixed = [{**others, column: value} for value in values]
            if _nullable(rules, key, column):
                fixed.append({**others, column: None})
            rows = _build_rows(key, columns, facts, rules, rng, 0, null_rate=0.0, fixed_rows=fixed)
            overrides = {k: (rows if k == key else []) for k in names}
            dataset = _dataset(300 + len(out), schema, names, facts, rules, rng, overrides=overrides)
            out.append(LabeledDataset(f"isolated:{key}:{column}:{index}", dataset))
    return out


def _nullable(rules: Mapping[str, DataRules] | None, key: str, column: str) -> bool:
    table_rules = rules.get(key.lower()) if rules else None
    if table_rules is None:
        return True
    return column not in table_rules.not_null and not any(column in k for k in table_rules.keys)


def random_datasets(
    schema: Schema,
    rules: Mapping[str, DataRules] | None = None,
    seeds=range(8),
) -> list[LabeledDataset]:
    """The engine's ordinary random databases (seed 0 is empty), respecting ``rules``."""

    return [
        LabeledDataset(f"random:{seed}", generate_synthetic_dataset(schema, seed=seed, rules=rules))
        for seed in seeds
    ]


def database_suite(
    sql: str,
    schema: Schema,
    rules: Mapping[str, DataRules] | None = None,
    *,
    dialect: str = "bigquery",
    random_seeds=range(1, 5),
    random_count: int = 12,
) -> list[LabeledDataset]:
    """The databases a query pair is checked on: corner cases, targeted and random ones."""

    return (
        edge_datasets(schema, rules)
        + targeted_datasets(sql, schema, rules, dialect=dialect, random_count=random_count)
        + random_datasets(schema, rules, random_seeds)
    )
