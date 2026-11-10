"""Check two queries against many small random databases that respect a declared schema.

A proof is only as good as the model behind it, so the evals under ``tools/`` re-run every proven
claim here. Databases are tiny and deliberately nasty: values come from a small domain seeded with
the numeric and string literals the queries mention (and their neighbours), so filters split the
rows, columns hold NULLs unless declared NOT NULL, rows repeat, keys stay unique, and some
databases are empty. Everything is deterministic from the seed.

    spec = Schema([Table("emps", [Column("empid", "int", not_null=True), ...], keys=[("empid",)])])
    witness = find_difference(spec, sql_a, sql_b, mode="bag")   # a Witness, or None when none found

``mode`` is ``"bag"`` (multiset equality), ``"set"`` (equality of the distinct rows), ``"subbag"``
(every row of A occurs in B at least as often) or ``"subset"`` (every row of A occurs in B).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import random
import re
from typing import Iterable, Sequence

import sqlglot

from kumo_sql_prover.ast_utils import spell_for_duckdb
from kumo_sql_prover.duckdb_load import run_unoptimized, small_database
from kumo_sql_prover.string_literals import canonical_literals

_DUCK_TYPES = {"int": "BIGINT", "float": "DOUBLE", "text": "VARCHAR", "date": "DATE", "bool": "BOOLEAN"}


@dataclass(frozen=True)
class Column:
    name: str
    type: str = "int"  # int | float | text | date | bool
    not_null: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    columns: Sequence[Column]
    keys: Sequence[Sequence[str]] = ()  # unique column sets (primary or unique keys; their columns are never NULL)


@dataclass(frozen=True)
class Schema:
    tables: Sequence[Table]

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    @property
    def columns(self) -> dict[str, list[str]]:
        return {t.name: [c.name for c in t.columns] for t in self.tables}

    def not_null(self, table: Table) -> set[str]:
        keyed = {c for key in table.keys for c in key}
        return {c.name for c in table.columns if c.not_null} | keyed


def prover_constraints(schema: Schema) -> dict:
    """The schema's NOT NULL columns and keys in the form the prover reads."""

    from kumo_sql_prover.smt_equivalence import TableConstraints

    return {t.name: TableConstraints(not_null=frozenset(schema.not_null(t)), keys=tuple(tuple(k) for k in t.keys)) for t in schema.tables}


@dataclass(frozen=True)
class Witness:
    """A database on which the queries disagree, with both results."""

    seed: int
    tables: dict[str, list[tuple]]
    only_left: tuple
    only_right: tuple


class CheckError(Exception):
    """A query could not be run (it is rejected by the engine); never evidence either way."""


def _numbers(sqls: Iterable[str]) -> list[float]:
    found: set[float] = set()
    for sql in sqls:
        for text in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", re.sub(r"'(?:[^']|'')*'", "", sql)):
            found.add(float(text))
    return sorted(found)


def _strings(sqls: Iterable[str]) -> list[str]:
    found: list[str] = []
    for sql in sqls:
        for text in re.findall(r"'((?:[^']|'')*)'", sql):
            if text not in found:
                found.append(text.replace("''", "'"))
    return found


def _domains(sqls: Sequence[str]) -> dict[str, list]:
    numbers = _numbers(sqls)
    ints = {0, 1, 2, 3}
    floats = {0.0, 1.0, 2.5}
    for number in numbers:
        for delta in (-1, 0, 1):
            ints.add(int(number) + delta)
        for delta in (-0.5, 0, 0.5):
            floats.add(number + delta)
    # keep domains small so equal values (joins, ties, duplicate groups) are common
    ints_sorted = sorted(ints)
    if len(ints_sorted) > 14:
        ints_sorted = ints_sorted[:3] + sorted(ints_sorted[3:])[:: max(1, len(ints_sorted) // 12)]
    texts = ["a", "b", "c", *_strings(sqls)][:8]
    return {
        "int": ints_sorted,
        "float": sorted(floats)[:14],
        "text": texts,
        "date": ["2020-01-01", "2020-06-15", "2021-01-01", "2021-12-31"],
        "bool": [True, False],
    }


def _row(table: Table, rng: random.Random, domains: dict[str, list], not_null: set[str], null_rate: float) -> list:
    row = []
    for column in table.columns:
        value = rng.choice(domains[column.type])
        if column.name not in not_null and rng.random() < null_rate:
            value = None
        row.append(value)
    return row


def random_tables(schema: Schema, seed: int, domains: dict[str, list], rows: int = 6, null_rate: float = 0.2) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    out: dict[str, list[tuple]] = {}
    for table in schema.tables:
        names = [c.name for c in table.columns]
        not_null = schema.not_null(table)
        count = 0 if seed % 9 == 0 else rng.randint(1, rows)
        taken: set[tuple] = set()
        result: list[tuple] = []
        for _ in range(count):
            row = _row(table, rng, domains, not_null, null_rate)
            clash = False
            for index, key in enumerate(table.keys):
                signature = (index, tuple(row[names.index(k)] for k in key))
                if signature in taken:
                    clash = True
            if clash:
                continue
            for index, key in enumerate(table.keys):
                taken.add((index, tuple(row[names.index(k)] for k in key)))
            result.append(tuple(row))
            if not table.keys and rng.random() < 0.3:
                result.append(tuple(row))  # duplicate rows exist when nothing forbids them
        out[table.name] = result
    return out


def _connect(schema: Schema, dialect: str = "postgres"):
    import duckdb

    db = small_database()
    if dialect == "bigquery":
        from kumo_sql_prover.bigquery_on_duckdb import configure

        configure(db)
    for table in schema.tables:
        not_null = schema.not_null(table)
        columns = ", ".join(f'"{c.name}" {_DUCK_TYPES[c.type]}{" NOT NULL" if c.name in not_null else ""}' for c in table.columns)
        db.execute(f'CREATE TABLE "{table.name}" ({columns})')
    return db


def _floor_to_unit(node):
    """``FLOOR(ts TO unit)`` (Calcite) is ``DATE_TRUNC('unit', ts)``; DuckDB has no TO form."""

    if isinstance(node, sqlglot.exp.Floor) and node.args.get("to") is not None:
        return sqlglot.exp.Anonymous(this="DATE_TRUNC", expressions=[sqlglot.exp.Literal.string(node.args["to"].name.lower()), node.this.copy()])
    return node


_RESERVED: set[str] | None = None


def _duck_reserved() -> set[str]:
    """Words DuckDB will not take as a bare name (``at`` is a common alias in JOB queries)."""

    global _RESERVED
    if _RESERVED is None:
        import duckdb

        rows = duckdb.sql("SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category IN ('reserved', 'type_function', 'column_name')").fetchall()
        _RESERVED = {r[0].lower() for r in rows}
    return _RESERVED


def _quote_reserved(node):
    if isinstance(node, sqlglot.exp.Identifier) and not node.args.get("quoted") and node.name.lower() in _duck_reserved():
        return sqlglot.exp.to_identifier(node.name, quoted=True)
    return node


def _duck(sql: str, dialect: str) -> str:
    if dialect == "bigquery":
        sql = canonical_literals(sql)
    try:
        tree = sqlglot.parse_one(sql, read=dialect).transform(_floor_to_unit).transform(_quote_reserved)
        if dialect == "bigquery":
            from kumo_sql_prover.bigquery_on_duckdb import faithful

            tree = faithful(tree)
        return spell_for_duckdb(tree).sql(dialect="duckdb")
    except sqlglot.errors.SqlglotError as error:
        raise CheckError(f"cannot translate: {error}") from error


def _norm(value):
    if isinstance(value, float):
        if value != value:  # NaN compares unequal to itself, which would make every NaN a "difference"
            return "nan"
        return round(value, 9)
    if hasattr(value, "as_tuple"):  # Decimal
        return round(float(value), 9)
    return value


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _load(db, schema: Schema, tables: dict[str, list[tuple]]) -> None:
    for table in schema.tables:
        db.execute(f'DELETE FROM "{table.name}"')
        rows = tables.get(table.name) or []
        if rows:
            values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)
            db.execute(f'INSERT INTO "{table.name}" VALUES {values}')


def _reader(dialect: str):
    """BigQuery-dialect rows as BigQuery returns them (other dialects as DuckDB returns them)."""

    if dialect != "bigquery":
        return lambda rows: rows
    from kumo_sql_prover.bigquery_on_duckdb import bigquery_rows

    return bigquery_rows


def _bigquery_failure(error: BaseException) -> bool:
    from kumo_sql_prover.bigquery_on_duckdb import is_bigquery_failure

    return is_bigquery_failure(error)


def run_all(schema: Schema, queries: Sequence[str], seeds: Iterable[int], *, dialect: str = "postgres", modes: Sequence[str] = ("bag",)) -> dict[str, Witness | None]:
    """Run the pair of ``queries`` ([a, b]) on every seed; per mode, the first database where it fails, else None."""

    import duckdb

    a_sql, b_sql = (_duck(q, dialect) for q in queries)
    domains = _domains(list(queries))
    db = _connect(schema, dialect)
    read = _reader(dialect)
    found: dict[str, Witness | None] = {m: None for m in modes}
    # A database already run gives the same rows again (every ninth seed is the all-empty one), and two queries
    # that translate to the same DuckDB text give the same rows: run each only once.
    seen: set[str] = set()
    for seed in seeds:
        tables = random_tables(schema, seed, domains)
        content = repr(tables)
        if content in seen:
            continue
        seen.add(content)
        _load(db, schema, tables)
        try:
            a = Counter(tuple(_norm(v) for v in row) for row in read(db.execute(a_sql).fetchall()))
            b = a if b_sql == a_sql else Counter(tuple(_norm(v) for v in row) for row in read(db.execute(b_sql).fetchall()))
        except Exception as error:
            if dialect == "bigquery" and _bigquery_failure(error):
                continue  # BigQuery fails on this database: it shows nothing either way
            if isinstance(error, duckdb.Error):
                raise CheckError(str(error)) from error
            raise
        if a != b:
            try:
                plain = [Counter(tuple(_norm(v) for v in row) for row in read(rows)) for rows in run_unoptimized(db, a_sql, b_sql)]
            except Exception:
                plain = None
            if plain != [a, b]:
                continue  # DuckDB's optimizer disagrees with its unoptimized plan: not evidence
        for mode in modes:
            if found[mode] is not None:
                continue
            x, y = (Counter(set(a)), Counter(set(b))) if mode in ("set", "subset") else (a, b)
            only_left, only_right = x - y, y - x
            differs = bool(only_left) if mode in ("subbag", "subset") else bool(only_left or only_right)
            if differs:
                found[mode] = Witness(seed, tables, tuple(only_left.elements()), tuple(only_right.elements()))
        if all(w is not None for w in found.values()):
            break
    return found


def find_difference(schema: Schema, left: str, right: str, *, mode: str = "bag", trials: int = 200, dialect: str = "postgres") -> Witness | None:
    """A database where ``left`` and ``right`` disagree under ``mode``, or None after ``trials`` databases."""

    return run_all(schema, [left, right], range(trials), dialect=dialect, modes=(mode,))[mode]


def replay(schema: Schema, witness: Witness, left: str, right: str, *, mode: str = "subbag", dialect: str = "postgres") -> bool:
    """Re-run both queries on the witness database alone: does it separate them under ``mode``?"""

    db = _connect(schema, dialect)
    _load(db, schema, witness.tables)
    left_sql, right_sql = _duck(left, dialect), _duck(right, dialect)
    read = _reader(dialect)
    try:
        a = Counter(tuple(_norm(v) for v in row) for row in read(db.execute(left_sql).fetchall()))
        b = Counter(tuple(_norm(v) for v in row) for row in read(db.execute(right_sql).fetchall()))
        plain = [Counter(tuple(_norm(v) for v in row) for row in read(rows)) for rows in run_unoptimized(db, left_sql, right_sql)]
    except Exception as error:
        if dialect == "bigquery" and _bigquery_failure(error):
            return False  # BigQuery fails on the witness database: it separates nothing
        raise
    if plain != [a, b]:
        return False  # DuckDB's optimizer disagrees with its unoptimized plan: not evidence
    if mode in ("set", "subset"):
        a, b = Counter(set(a)), Counter(set(b))
    return bool(a - b) if mode in ("subbag", "subset") else bool((a - b) or (b - a))
