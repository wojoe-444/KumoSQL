"""Refute an equivalence by running both queries on databases built for them.

The SMT prover returns a counterexample only when its own model of the queries
yields one, and it yields none for outer joins, duplicate-producing joins,
``NOT IN`` with NULLs or set operations of different shapes, so such pairs stay
"unknown" although a three-row database tells them apart. This module searches
for that database: the corner-case, targeted and random databases of
:mod:`kumosql.targeted_data` built around *both* queries, each respecting the
declared NOT NULL columns, keys and foreign keys, run on DuckDB.

A database on which the two result bags differ is a refutation that needs no
trust in the prover: it is returned as a :class:`Counterexample` that anyone can
replay. To keep it a refutation of the BigQuery queries and not of DuckDB's
reading of them, the search only runs when

* every column type is declared (no guessing that an untyped column is an
  integer),
* both queries use only constructs whose DuckDB translation evaluates as
  BigQuery does (a fixed allow-list: joins, set operations, subqueries,
  comparisons, arithmetic, ``CASE``/``IF``/``COALESCE``, the plain aggregates and
  aggregate windows; no ``LIMIT``, string or date functions, ``LIKE``, arrays,
  ``ROW_NUMBER`` or anything nondeterministic),
* the DuckDB run follows BigQuery's semantics (:mod:`kumosql.bigquery_on_duckdb`:
  a zero divisor fails the run as it does in BigQuery, NULL sorts first, and so
  on), and
* the difference survives rounding floats to 6 digits and both sides return the
  same bag with every table's rows reversed (no dependence on row order).

Finding nothing proves nothing. ``search_counterexample`` is opt-in through
``prove_equivalent_algebraic(..., search_counterexample=True)``.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

import sqlglot
from sqlglot import exp

from .smt_equivalence import Counterexample, TableConstraints
from .type_names import invalid_type_name

ASSUMPTION = "the counterexample was found by running both queries on DuckDB with the declared column types"

_ALLOWED_NAMES = (
    # query structure
    "Select", "From", "Join", "Where", "Group", "Having", "Qualify", "Order", "Ordered", "Union", "Intersect",
    "Except", "Subquery", "CTE", "With", "TableAlias", "Table", "Column", "Identifier", "Star", "Alias", "Distinct",
    "Values", "Tuple", "Paren",
    # values and predicates
    "Literal", "Null", "Boolean", "DataType", "And", "Or", "Not", "EQ", "NEQ", "LT", "LTE", "GT", "GTE", "Is", "In",
    "Between", "Exists", "Any", "All", "NullSafeEQ", "NullSafeNEQ",
    # arithmetic and conditionals
    "Add", "Sub", "Mul", "Neg", "Div", "SafeDivide", "Abs", "Case", "If", "Coalesce", "Nullif", "Cast", "TryCast",
    # aggregates and aggregate windows
    "Count", "Sum", "Min", "Max", "Avg", "CountIf", "LogicalAnd", "LogicalOr", "Window", "WindowSpec",
    # checked further in _refusal: field access to a STRUCT, day arithmetic on dates, CROSS JOIN UNNEST
    "Struct", "PropertyEQ", "DateAdd", "DateSub", "DateDiff", "Interval", "Var", "Unnest", "Array",
)
_ALLOWED = tuple(cls for cls in (getattr(exp, name, None) for name in _ALLOWED_NAMES) if cls is not None)
_CAST_TARGETS = {
    exp.DataType.Type.DATE,
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.INT,
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT,
    exp.DataType.Type.BOOLEAN,
}
_SET_OPERATIONS = (exp.Union, exp.Intersect, exp.Except)

_DATASET_LIMIT = 140
_RANDOM_SEEDS = range(1, 13)


def _day_unit(node: exp.Expression) -> bool:
    unit = node.args.get("unit")
    if isinstance(node.expression, exp.Interval):
        unit = node.expression.args.get("unit")
    return unit is not None and unit.name.upper() == "DAY"


def _struct_refusal(tree: exp.Expression) -> str | None:
    from .bigquery_on_duckdb import struct_refusal

    return struct_refusal(tree)


def _refusal(tree: exp.Expression) -> str | None:
    """Why ``tree`` is outside what the search runs, or ``None``."""

    for node in tree.walk():
        if not isinstance(node, _ALLOWED):
            return type(node).__name__
        if isinstance(node, (exp.Cast, exp.TryCast)) and node.to.this not in _CAST_TARGETS:
            return f"CAST to {node.to.sql('bigquery')}"
        if isinstance(node, (exp.Cast, exp.TryCast)) and node.to.this == exp.DataType.Type.DATE and not (
            isinstance(node.this, exp.Literal) and node.this.is_string
        ):
            return "CAST to DATE"
        if isinstance(node, (exp.DateAdd, exp.DateSub, exp.DateDiff)) and not _day_unit(node):
            return "date arithmetic other than days"
        if isinstance(node, (exp.Interval, exp.Var)) and not isinstance(node.parent, (exp.DateAdd, exp.DateSub, exp.DateDiff, exp.Interval)):
            return type(node).__name__
        if isinstance(node, exp.Unnest) and not (
            isinstance(node.parent, exp.Join)
            and not node.parent.args.get("side")
            and not node.parent.args.get("on")
            and not node.args.get("offset")
            and len(node.expressions) == 1
            and isinstance(node.expressions[0], exp.Array)
        ):
            return "UNNEST"
        if isinstance(node, exp.Array) and not isinstance(node.parent, exp.Unnest):
            return "ARRAY"
        if isinstance(node, _SET_OPERATIONS) and (node.args.get("by_name") or node.args.get("side") or node.args.get("kind")):
            return "set operation by name"
        if isinstance(node, exp.Window) and not isinstance(
            node.this, (exp.Count, exp.Sum, exp.Min, exp.Max, exp.Avg, exp.CountIf)
        ):
            return "window function"
        if isinstance(node, exp.Select) and (node.args.get("limit") or node.args.get("offset")):
            return "LIMIT"
        if isinstance(node, _SET_OPERATIONS) and (node.args.get("limit") or node.args.get("offset")):
            return "LIMIT"
        if isinstance(node, exp.Join) and (node.args.get("using") or node.args.get("method")):
            return "join form"
    return _struct_refusal(tree)


_INTEGER_CASTS = {exp.DataType.Type.BIGINT, exp.DataType.Type.INT}


def _faithful(tree: exp.Expression) -> exp.Expression:
    """``tree`` with the DuckDB reading pinned to BigQuery's where the rest of it (a zero divisor
    fails, NULL order, ...: :mod:`kumosql.bigquery_on_duckdb`, applied by the runner) leaves a gap.

    ``DATE_ADD``/``DATE_SUB`` are cast back to ``DATE`` (DuckDB's date plus interval is a timestamp)
    and fail outside BigQuery's years 1 to 9999 (DuckDB's dates go far beyond). ``CAST(x AS INT64)``
    rounds first: BigQuery rounds a FLOAT64 half away from zero, DuckDB's cast half to even (so 2.5 is
    3 and 2); ``ROUND`` refuses a STRING or BOOL operand, which only costs a refutation (the run
    fails). Nested forms are guarded from the inside out.
    """

    def guard(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.DateAdd, exp.DateSub)):
            day = exp.Cast(this=node, to=exp.DataType.build("DATE"))
            low = exp.LT(this=day.copy(), expression=exp.cast(exp.Literal.string("0001-01-01"), "DATE"))
            high = exp.GT(this=day.copy(), expression=exp.cast(exp.Literal.string("9999-12-31"), "DATE"))
            error = exp.Anonymous(this="ERROR", expressions=[exp.Literal.string("date out of range")])
            return exp.If(this=exp.Or(this=low, expression=high), true=error, false=day)
        if isinstance(node, (exp.Cast, exp.TryCast)) and node.to.this in _INTEGER_CASTS:
            operand = node.this
            if isinstance(operand, (exp.Predicate, exp.Connector, exp.Not, exp.Boolean, exp.Null)) or (
                isinstance(operand, exp.Literal) and operand.is_int
            ):
                return node
            return node.__class__(**{**node.args, "this": exp.Round(this=operand)})
        return node

    tree = tree.copy()
    targets = (exp.DateAdd, exp.DateSub, exp.Cast, exp.TryCast)
    for node in reversed([n for n in tree.walk() if isinstance(n, targets)]):  # breadth-first, reversed: inner ones first
        candidate = node.copy()
        replacement = guard(candidate)
        if replacement is candidate:
            continue
        if node.parent is not None:
            node.replace(replacement)
        else:
            tree = replacement
    return tree


def _tables_read(tree: exp.Expression) -> set[str]:
    ctes = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        key = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
        if key and not (key in ctes and not table.db):
            names.add(key)
    return names


def _typed_schema(
    read: set[str], schema: Mapping[str, list[str]], types: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, str]] | None:
    from .result_equivalence import _normalize_type

    by_lower = {k.lower(): k for k in schema}
    types_lower = {k.lower(): {c.lower(): t for c, t in v.items()} for k, v in types.items()}
    typed: dict[str, dict[str, str]] = {}
    for name in sorted(read):
        key = by_lower.get(name)
        declared = types_lower.get(name)
        if key is None or declared is None:
            return None
        columns: dict[str, str] = {}
        for column in schema[key]:
            raw = declared.get(column.lower())
            if raw is None:
                return None
            try:
                columns[column] = _normalize_type(raw)
            except ValueError:
                return None
        typed[key] = columns
    return typed


def _rules(typed: Mapping[str, Mapping[str, str]], constraints: Mapping[str, TableConstraints]):
    from .result_equivalence import DataRules

    by_lower = {k.lower(): v for k, v in constraints.items()}
    rules = {}
    for key in typed:
        declared = by_lower.get(key.lower())
        if declared is None:
            continue
        keys = tuple(tuple(c.lower() for c in k) for k in declared.keys)
        not_null = frozenset(c.lower() for c in declared.not_null) | {c for k in keys for c in k}
        rules[key.lower()] = DataRules(not_null=frozenset(not_null), keys=keys)
    return rules


def _legal(dataset, typed, constraints: Mapping[str, TableConstraints]) -> bool:
    """Whether ``dataset`` keeps every declared NOT NULL, key and foreign key."""

    by_lower = {k.lower(): v for k, v in constraints.items()}
    tables = {k.lower(): t for k, t in dataset.tables.items()}
    for name, table in tables.items():
        declared = by_lower.get(name)
        if declared is None:
            continue
        index = {c.lower(): i for i, (c, _) in enumerate(table.columns)}
        keys = [tuple(c.lower() for c in k) for k in declared.keys]
        required = {c.lower() for c in declared.not_null} | {c for k in keys for c in k}
        if any(c not in index for c in required):
            return False
        for row in table.rows:
            if any(row[index[c]] is None for c in required):
                return False
        for key in keys:
            seen = [tuple(row[index[c]] for c in key) for row in table.rows]
            if len(set(seen)) != len(seen):
                return False
        for columns, parent, parent_columns in declared.foreign_keys:
            parent_table = tables.get(parent.lower())
            if parent_table is None:
                return False
            pindex = {c.lower(): i for i, (c, _) in enumerate(parent_table.columns)}
            if any(c.lower() not in index for c in columns) or any(c.lower() not in pindex for c in parent_columns):
                return False
            present = {tuple(r[pindex[c.lower()]] for c in parent_columns) for r in parent_table.rows}
            for row in table.rows:
                values = tuple(row[index[c.lower()]] for c in columns)
                if None not in values and values not in present:
                    return False
    return True


def _datasets(left: str, right: str, typed, rules):
    from .result_equivalence import generate_synthetic_dataset, query_constants
    from .targeted_data import edge_datasets, targeted_datasets

    yield from (d.dataset for d in edge_datasets(typed, rules))
    for sql in (left, right):
        try:
            suite = targeted_datasets(sql, typed, rules, random_count=6)
        except (ValueError, sqlglot.errors.SqlglotError):
            suite = []
        yield from (d.dataset for d in suite)
    extras = query_constants(left, right)
    yield from _narrow_datasets(typed, rules, extras)
    integers = _query_integers(left, right)
    if set(integers) - set(extras.get("INT64", ())):
        # ``extras`` leaves out the integers the default domain already holds, so the narrow pool above can be
        # just 0 and 1; a batch over every integer of the queries and its neighbours, before the random ones
        yield from _narrow_datasets(typed, rules, {"INT64": integers}, count=40)
    for seed in _RANDOM_SEEDS:
        yield generate_synthetic_dataset(typed, seed=seed, rows_per_table=5, null_rate=0.2, rules=rules, extra_values=extras)


def _query_integers(*sqls: str) -> tuple[int, ...]:
    found: list[int] = []
    for sql in sqls:
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except sqlglot.errors.SqlglotError:
            continue
        for node in tree.find_all(exp.Literal):
            if not node.is_string and node.name.isascii() and node.name.isdigit():
                found.append(-int(node.name) if isinstance(node.parent, exp.Neg) else int(node.name))
    return tuple(dict.fromkeys(found))


def _narrow_datasets(typed, rules, extras, count: int = 60):
    """Two to four rows per table over a few values per column (the queries' integers and their
    neighbours), often one value for a whole column: groups that share a key, join partners and
    duplicates are common, which is where HAVING, COUNT(col) and join rewrites part ways."""

    import random

    from .result_equivalence import _DOMAINS, SyntheticDataset, SyntheticTable, respect_rules

    ints = sorted({v + d for v in extras.get("INT64", ()) for d in (-1, 0, 1)} | {0, 1})[:8]
    rng = random.Random(17)
    for index in range(count):
        tables = {}
        for key, columns in typed.items():
            cols = tuple(columns.items())
            pools = []
            for _, col_type in cols:
                pool = ints if col_type == "INT64" else list(_DOMAINS[col_type][:3])
                pools.append([rng.choice(pool)] if rng.random() < 0.4 else pool)
            rows = [
                tuple(None if rng.random() < 0.25 else rng.choice(pool) for pool in pools)
                for _ in range(0 if rng.random() < 0.15 else rng.randint(1, 4))
            ]
            if rows and rng.random() < 0.3:
                rows.append(rng.choice(rows))
            rows = respect_rules(cols, rows, rules.get(key.lower()))
            tables[key] = SyntheticTable(cols, tuple(rows))
        yield SyntheticDataset(1000 + index, tables)


def _reversed(dataset):
    from .result_equivalence import SyntheticDataset, SyntheticTable

    return SyntheticDataset(
        dataset.seed, {k: SyntheticTable(t.columns, tuple(reversed(t.rows))) for k, t in dataset.tables.items()}
    )


def _with_rows(dataset, rows: Mapping[str, list]):
    from .result_equivalence import SyntheticDataset, SyntheticTable

    return SyntheticDataset(dataset.seed, {k: SyntheticTable(t.columns, tuple(rows[k])) for k, t in dataset.tables.items()})


class _Search:
    def __init__(self, runner, left: str, right: str):
        self.runner = runner
        self.left = left
        self.right = right

    def outputs(self, dataset):
        from .result_equivalence import ExecutionError

        try:
            return self.runner.run(self.left, dataset, timeout=5), self.runner.run(self.right, dataset, timeout=5)
        except ExecutionError:
            return None

    def differs(self, dataset) -> bool:
        """The bags differ on ``dataset``, robustly (6-digit floats, row order irrelevant)."""

        from .result_equivalence import compare_outputs

        first = self.outputs(dataset)
        if first is None:
            return False
        a, b = first
        if compare_outputs(a, b, check_column_names=False, float_digits=6)[0]:
            return False
        again = self.outputs(_reversed(dataset))
        if again is None:
            return False
        for before, after in zip(first, again):
            if not compare_outputs(before, after, check_column_names=False, float_digits=12)[0]:
                return False  # depends on row order: not a refutation
        return True

    def shrink(self, dataset, legal, deadline: float):
        """Drop rows, whole tables first, while the pair still differs and the database stays legal."""

        rows = {k: list(t.rows) for k, t in dataset.tables.items()}

        def ok(trial) -> bool:
            candidate = _with_rows(dataset, trial)
            return legal(candidate) and self.differs(candidate)

        for key in sorted(rows):
            if rows[key] and time.monotonic() < deadline:
                trial = {**rows, key: []}
                if ok(trial):
                    rows = trial
        changed = True
        while changed and time.monotonic() < deadline:
            changed = False
            for key in sorted(rows):
                for i in range(len(rows[key])):
                    trial = {**rows, key: rows[key][:i] + rows[key][i + 1 :]}
                    if ok(trial):
                        rows = trial
                        changed = True
                        break
                if changed:
                    break
        return _with_rows(dataset, rows)


def _export(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def search_counterexample(
    left_sql: str,
    right_sql: str,
    *,
    schema: Mapping[str, list[str]] | None,
    types: Mapping[str, Mapping[str, str]] | None,
    constraints: Mapping[str, TableConstraints] | None = None,
    dialect: str = "bigquery",
    time_limit: float = 3.0,
) -> Counterexample | None:
    """A database on which the two queries return different bags, or ``None``."""

    if dialect != "bigquery" or not schema or not types:
        return None
    if invalid_type_name(left_sql) or invalid_type_name(right_sql):
        return None  # BigQuery rejects the query, and DuckDB would run it anyway
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return None
    try:
        trees = [sqlglot.parse_one(sql, read="bigquery") for sql in (left_sql, right_sql)]
    except sqlglot.errors.SqlglotError:
        return None
    if any(_refusal(tree) for tree in trees):
        return None
    read = _tables_read(trees[0]) | _tables_read(trees[1])
    typed = _typed_schema(read, schema, types)
    if not typed:
        return None
    constraints = constraints or {}
    rules = _rules(typed, constraints)
    guarded = [_faithful(tree).sql(dialect="bigquery") for tree in trees]

    from .result_equivalence import DatasetRunner, ExecutionError

    def legal(dataset) -> bool:
        return _legal(dataset, typed, constraints)

    deadline = time.monotonic() + time_limit
    try:
        runner = DatasetRunner(typed)
    except Exception:  # noqa: BLE001 - no engine means no search
        return None
    with runner:
        try:
            runner.prepare(guarded[0])
            runner.prepare(guarded[1])
        except ExecutionError:
            return None
        search = _Search(runner, *guarded)

        def accept(dataset) -> bool:
            return legal(dataset) and search.differs(dataset)

        found = _first_difference(left_sql, right_sql, typed, rules, constraints, accept, deadline)
        if found is None:
            return None
        small = search.shrink(found, legal, time.monotonic() + max(1.0, time_limit / 2))
        # The queries as written must differ on it too, so that a plain DuckDB replay shows it.
        try:
            if not _Search(runner, left_sql, right_sql).differs(small):
                return None
        except ExecutionError:
            return None
        outputs = search.outputs(small)
        if outputs is None or not _unoptimized_agrees(typed, guarded, small, outputs):
            return None
        a, b = outputs
        tables = {
            key: [{name: _export(v) for (name, _), v in zip(table.columns, row)} for row in table.rows]
            for key, table in small.tables.items()
        }
        return Counterexample(
            tables=tables,
            left_rows=sorted((tuple(_export(v) for v in r) for r in a.rows), key=repr),
            right_rows=sorted((tuple(_export(v) for v in r) for r in b.rows), key=repr),
        )


def _first_difference(left_sql, right_sql, typed, rules, constraints, accept, deadline):
    """The first database ``accept`` confirms: the ones built from the literals, then each boundary row
    alone in its table, then (``bounded_refutation``) the z3 candidates; ``None`` if there is none."""

    import itertools

    from . import bounded_refutation

    built = itertools.islice(_datasets(left_sql, right_sql, typed, rules), _DATASET_LIMIT)
    for dataset in itertools.chain(built, bounded_refutation.isolated_datasets(left_sql, right_sql, typed, rules)):
        if time.monotonic() > deadline:
            break  # the z3 candidates are bounded by work, not by this clock
        if accept(dataset):
            return dataset
    foreign_keys = {k.lower(): list(v.foreign_keys) for k, v in constraints.items() if k.lower() in {t.lower() for t in typed}}
    return bounded_refutation.find(left_sql, right_sql, typed, rules, foreign_keys, accept)


def _unoptimized_agrees(typed, queries, dataset, outputs) -> bool:
    """Both queries return the same rows with DuckDB's optimizer off (DuckDB 1.5 returns wrong rows
    for some correlated subqueries with it on; see ``duckdb_load.run_unoptimized``)."""

    from .result_equivalence import DatasetRunner, ExecutionError, compare_outputs

    try:
        with DatasetRunner(typed, settings=("PRAGMA disable_optimizer",)) as plain:
            for sql, output in zip(queries, outputs):
                again = plain.run(sql, dataset, timeout=5)
                if not compare_outputs(output, again, check_column_names=False, float_digits=12)[0]:
                    return False
    except ExecutionError:
        return False
    return True
