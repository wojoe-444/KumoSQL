"""Candidate databases for the executed refutation search, asked of z3.

The executed search (:mod:`kumosql.executed_refutation`) runs both queries on databases built from
their literals. A difference that needs values to meet several constraints at once across joined
tables (a ``t`` row whose ``b`` is the ``a`` of another ``t`` row with a ``u`` partner whose ``c``
is NULL; three rows of one partition with a value below every literal) is rarely hit that way. The
bounded encoding of :mod:`kumosql.bounded_equivalence` asks z3 for such a database directly: at most
N rows per table on which the encoded queries return different bags.

A model is only a candidate. ``find`` hands each one to the caller's ``accept``, which runs both
queries on DuckDB exactly as it runs its own databases (the BigQuery-faithful reading, rows reversed,
declared keys and NOT NULL columns respected), so a gap in the encoding, or in the rewrites below that
only help the encoding, can cost a refutation but never make one. Models are kept to small values
(integers and reals within ``LIMIT``, reals in quarters, printable short strings, dates within BigQuery's
range), the region where the allow-listed constructs read the same in DuckDB and BigQuery.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

import sqlglot
from sqlglot import exp

LIMIT = 10_000  # |value| of a model's integers and reals: no INT64 overflow, no date out of range
BOUNDS = (3, 2)  # rows per table after one: the largest whose encoding stays within WORK
WORK = 400  # ``estimate`` units (well under a second of encoding, mostly)
RLIMIT = 3_000_000  # z3 resource units per solver call (about a second at most)
MODELS = 3  # models asked for when ``accept`` rejects one


def encodable(sql: str) -> str:
    """``sql`` in a form the bounded encoding models (same bag): ``QUALIFY`` as a filter over a derived
    table, and a top-level WITH whose CTEs hold window functions lifted over the main query."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
        tree = tree.transform(_qualify_as_filter)
        clause = tree.args.get("with_")
        if isinstance(tree, exp.Select) and clause is not None and any(True for _ in clause.find_all(exp.Window)):
            body = tree.copy()
            body.set("with_", None)
            lifted = exp.select("*").from_(body.subquery("__kumo_top"))
            lifted.set("with_", clause.copy())
            tree = lifted
        return tree.sql(dialect="bigquery")
    except (sqlglot.errors.SqlglotError, RecursionError):
        return sql


def _qualify_as_filter(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.Select) or node.args.get("qualify") is None:
        return node
    if any(node.args.get(k) for k in ("order", "limit", "offset", "windows")):
        return node
    names = [item.alias_or_name for item in node.expressions]
    if any(isinstance(item, exp.Star) or not name for item, name in zip(node.expressions, names)):
        return node
    if len({name.lower() for name in names}) != len(names):
        return node
    inner = node.copy()
    condition = inner.args["qualify"].this
    with_ = inner.args.get("with_")
    distinct = inner.args.get("distinct")
    for key in ("qualify", "with_", "distinct"):
        inner.set(key, None)
    inner.set("expressions", list(inner.expressions) + [exp.alias_(condition, "__kumo_q")])
    outer = exp.select(*[exp.alias_(exp.column(name, table="__kumo_s"), name) for name in names])
    outer = outer.from_(inner.subquery("__kumo_s")).where(exp.column("__kumo_q", table="__kumo_s"))
    if distinct is not None:  # QUALIFY filters before DISTINCT
        outer.set("distinct", distinct)
    if with_ is not None:
        outer.set("with_", with_)
    return outer


_QUERIES = (exp.Select, exp.Union, exp.Intersect, exp.Except)


def _enclosing_query(node: exp.Expression):
    parent = node.parent
    while parent is not None and not isinstance(parent, _QUERIES):
        parent = parent.parent
    return parent


def _cost(node: exp.Expression, rows: int, ctes: Mapping[str, exp.Expression]) -> tuple[int, int]:
    """``(output rows, work)`` of encoding ``node`` over ``rows`` rows per table: symbolic rows built,
    a correlated subquery once per outer row, grouping, DISTINCT and windows quadratic. Deterministic,
    so that whether a bound is tried does not depend on the machine's load."""

    if isinstance(node, exp.Subquery):
        return _cost(node.this, rows, ctes)
    clause = node.args.get("with_")
    if clause is not None:
        ctes = {**ctes, **{cte.alias.lower(): cte.this for cte in clause.expressions}}
    if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        left, left_work = _cost(node.this, rows, ctes)
        right, right_work = _cost(node.expression, rows, ctes)
        out = left + right if isinstance(node, exp.Union) else left
        return out, left_work + right_work + (left + right) ** 2
    if not isinstance(node, exp.Select):
        return 1, 1
    out, work = 1, 0
    clause = node.args.get("from_") or node.args.get("from")
    sources = ([clause.this] if clause is not None else []) + [j.this for j in node.args.get("joins") or []]
    for source in sources:
        if isinstance(source, exp.Table) and not source.db and source.name.lower() in ctes:
            name = source.name.lower()
            size, cost = _cost(ctes[name], rows, {k: v for k, v in ctes.items() if k != name})
        elif isinstance(source, exp.Subquery):
            size, cost = _cost(source.this, rows, ctes)
        else:
            size, cost = rows, rows
        out, work = out * size, work + cost
    work += out
    aggregated = node.args.get("group") is not None
    windows = 0
    for key in ("expressions", "where", "having", "qualify", "group", "order"):
        value = node.args.get(key)
        for part in value if isinstance(value, list) else [value] if value is not None else []:
            for inner in part.find_all(*_QUERIES):
                if _enclosing_query(inner) is node:
                    work += out * _cost(inner, rows, ctes)[1]
            for function in part.find_all(exp.AggFunc, exp.Window):
                if _enclosing_query(function) is not node:
                    continue
                if isinstance(function, exp.Window):
                    windows += 1
                elif function.find_ancestor(exp.Window, *_QUERIES) is node:
                    aggregated = True
    work += out * out * (windows + int(aggregated) + int(node.args.get("distinct") is not None))
    return (out if node.args.get("group") is not None or not aggregated else 1), work


def estimate(left_sql: str, right_sql: str, rows: int) -> int:
    """The work of encoding both queries over ``rows`` rows per table and comparing their bags."""

    try:
        (left, left_work), (right, right_work) = (
            _cost(sqlglot.parse_one(sql, read="bigquery"), rows, {}) for sql in (left_sql, right_sql)
        )
    except (sqlglot.errors.SqlglotError, RecursionError):
        return 1 << 62
    return left_work + right_work + left * right


def _schema(typed: Mapping[str, Mapping[str, str]], required: Mapping[str, frozenset], keys, foreign_keys):
    from .bounded_equivalence import BColumn, BoundedSchema, BTable

    tables = {}
    for key, columns in typed.items():
        needed = required.get(key.lower(), frozenset())
        table = BTable(key, [BColumn(c, t, c.lower() in needed) for c, t in columns.items()], keys=list(keys.get(key.lower(), ())))
        table.foreign_keys = [fk for fk in foreign_keys.get(key.lower(), ()) if fk[1].lower() in {k.lower() for k in typed}]
        tables[key] = table
    return BoundedSchema(tables)


def _small(database) -> list:
    """Constraints keeping every cell of ``database`` in the small, engine-agnostic region."""

    import z3

    out = []
    low, high = date(1900, 1, 1).toordinal(), date(2100, 12, 31).toordinal()
    for slots in database.tables.values():
        for slot in slots:
            for v in slot.vals:
                if v.kind == "int":
                    out.append(z3.And(v.val >= -LIMIT, v.val <= LIMIT))
                elif v.kind == "real":
                    out.append(z3.And(v.val >= -LIMIT, v.val <= LIMIT, z3.IsInt(v.val * 4)))
                elif v.kind == "str":
                    out.append(z3.InRe(v.val, z3.Star(z3.Range(" ", "~"))))
                    out.append(z3.Length(v.val) <= 16)
                elif v.kind == "date":
                    out.append(z3.And(v.val >= low, v.val <= high))
                elif v.kind == "datetime":
                    out.append(z3.And(v.val >= low * 86400, v.val <= high * 86400))
    return out


def _block(database, model):
    """A constraint ruling out ``model``'s database."""

    import z3

    same = []
    for slots in database.tables.values():
        for slot in slots:
            present = model.eval(slot.present, model_completion=True)
            same.append(slot.present == present)
            if z3.is_false(present):
                continue
            for v in slot.vals:
                if v.kind == "unsupported":
                    continue
                null = model.eval(v.null, model_completion=True)
                same.append(v.null == null)
                if z3.is_false(null):
                    same.append(v.val == model.eval(v.val, model_completion=True))
    return z3.Not(z3.And(*same))


def _cell(value: Any, col_type: str) -> tuple[bool, Any]:
    """``(ok, value)``: the model's value as the dataset holds it for a column of ``col_type``."""

    if value is None:
        return True, None
    if col_type == "INT64":
        return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= LIMIT, value
    if col_type == "FLOAT64":
        return isinstance(value, float) and abs(value) <= LIMIT, value
    if col_type == "NUMERIC":
        number = Decimal(repr(value)) if isinstance(value, float) else None
        return number is not None and abs(number) <= LIMIT and number.as_tuple().exponent >= -9, number
    if col_type == "STRING":
        return isinstance(value, str) and len(value) <= 16 and value.isprintable(), value
    if col_type == "BOOL":
        return isinstance(value, bool), value
    if col_type == "DATE":
        return isinstance(value, date) and not isinstance(value, datetime) and 1900 <= value.year <= 2100, value
    if col_type == "TIMESTAMP":
        return isinstance(value, datetime) and 1900 <= value.year <= 2100, value
    return False, value


def _dataset(data: Mapping[str, list[tuple]], typed: Mapping[str, Mapping[str, str]]):
    from .result_equivalence import SyntheticDataset, SyntheticTable

    tables = {}
    for key, columns in typed.items():
        cols = tuple(columns.items())
        rows = []
        for raw in data.get(key) or []:
            row = []
            for (_, col_type), value in zip(cols, raw):
                ok, cell = _cell(value, col_type)
                if not ok:
                    return None
                row.append(cell)
            rows.append(tuple(row))
        tables[key] = SyntheticTable(cols, tuple(rows))
    return SyntheticDataset(2000, tables)


def isolated_datasets(left_sql: str, right_sql: str, typed, rules, limit: int = 40):
    """Each constant's boundary rows alone in one table (``targeted_data.isolated_boundary_datasets``), once each."""

    import itertools

    from .targeted_data import isolated_boundary_datasets

    seen = set()

    def generate():
        for sql in (left_sql, right_sql):
            try:
                suite = isolated_boundary_datasets(sql, typed, rules)
            except (ValueError, sqlglot.errors.SqlglotError):
                suite = []
            for labeled in suite:
                signature = tuple(sorted((k, t.rows) for k, t in labeled.dataset.tables.items()))
                if signature not in seen:
                    seen.add(signature)
                    yield labeled.dataset

    return itertools.islice(generate(), limit)


def find(
    left_sql: str,
    right_sql: str,
    typed: Mapping[str, Mapping[str, str]],
    rules: Mapping[str, Any],
    foreign_keys: Mapping[str, list],
    accept: Callable[[Any], bool],
):
    """The first z3 model of at most one, then N rows per table that ``accept`` confirms, or ``None``.

    N is the larger of 3 and 2 whose encoding stays within ``WORK`` (see ``estimate``): a database
    of at most N rows per table also covers the smaller ones, and the caller shrinks what it keeps.
    Each solver call gets ``RLIMIT`` z3 resource units; a model ``accept`` rejects is ruled out and
    another one asked for, ``MODELS`` times at most. Every limit counts work, not seconds, so the
    answer does not depend on the machine's load. ``rules`` are the tables' ``DataRules`` (NOT NULL
    columns and keys) by lower-case name and ``foreign_keys`` their ``(columns, parent, parent
    columns)`` triples.
    """

    try:
        import z3
    except ImportError:  # pragma: no cover - z3 is a dependency of the prover
        return None
    from .bounded_equivalence import Compiler, SymbolicDatabase, Unsupported, bag_difference, database_from_model
    from .set_operations import positional_sql_pair

    required = {k: r.not_null for k, r in rules.items()}
    keys = {k: r.keys for k, r in rules.items()}
    schema = _schema(typed, required, keys, foreign_keys)
    try:
        left, right, problem = positional_sql_pair(encodable(left_sql), encodable(right_sql), "bigquery")
        if problem:
            return None
        if estimate(left, right, 1) > WORK:
            return None
        larger = next((n for n in BOUNDS if estimate(left, right, n) <= WORK), None)
        for bound in (1, larger) if larger else (1,):
            database = SymbolicDatabase(schema, bound)
            compiler = Compiler(database, "bigquery")
            encoded = compiler.compile(left), compiler.compile(right)
            solver = z3.SimpleSolver()
            solver.set("rlimit", RLIMIT)
            solver.add(*database.constraints, *compiler.side_conditions, *_small(database))
            solver.add(bag_difference(*encoded))
            for _ in range(MODELS):
                verdict = solver.check()
                if verdict == z3.unknown:
                    return None
                if verdict == z3.unsat:
                    break
                model = solver.model()
                dataset = _dataset(database_from_model(database, model), typed)
                if dataset is not None and accept(dataset):
                    return dataset
                solver.add(_block(database, model))
    except (Unsupported, sqlglot.errors.SqlglotError, RecursionError, z3.Z3Exception):
        return None
    except Exception:  # noqa: BLE001 - an encoding gap only means no candidate
        return None
    return None
