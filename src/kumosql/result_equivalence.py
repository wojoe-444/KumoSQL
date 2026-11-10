"""Execution-based equivalence checks for BigQuery SQL rewrites.

This module complements the static prover in :mod:`kumosql.equivalence`.
Instead of comparing ASTs, it runs the original and the rewritten SQL against
the same deterministic synthetic tables in a local DuckDB engine and compares
the result sets. Agreement on synthetic data is evidence, not a proof; a
disagreement is a concrete counterexample.

Each run is isolated: every side of every seed gets its own in-memory DuckDB
connection, physical source tables are loaded fresh from the synthetic
dataset, and tables written by a script (``CREATE TABLE``, ``INSERT``,
``UPDATE``, ``DELETE``, ``TRUNCATE``) are renamed to run-unique local names so
two runs can never observe each other's output. A script that ends by
modifying a table is compared by that table's rows, never by its
affected-row count.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import math
import random
import re
import threading
from typing import Any, Iterable, Mapping

import sqlglot
from sqlglot import exp

from .ast_utils import spell_for_duckdb
from .duckdb_load import small_database
from .sqlx import looks_like_sqlx, split_sqlx_sections

Schema = Mapping[str, Mapping[str, str]]
"""Table name (as written in SQL, e.g. ``p.d.customers``) -> column -> BigQuery type."""

Row = tuple[Any, ...]


class ResultEquivalenceStatus(str, Enum):
    """Possible outcomes of an execution-based comparison."""

    EQUIVALENT = "equivalent"
    DIFFERENT = "different"
    ERROR = "error"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class QueryOutput:
    """Column names and rows produced by one execution."""

    columns: tuple[str, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class ResultEquivalence:
    """The outcome of comparing two queries over one or more synthetic datasets."""

    status: ResultEquivalenceStatus
    reason: str
    seeds_checked: tuple[int, ...] = ()
    failing_seed: int | None = None
    left_output: QueryOutput | None = None
    right_output: QueryOutput | None = None
    only_left: tuple[Row, ...] = ()
    only_right: tuple[Row, ...] = ()
    left_duckdb_sql: tuple[str, ...] = ()
    right_duckdb_sql: tuple[str, ...] = ()
    float_digits: int | None = 12
    """The float policy the rows were compared under: significant digits, or ``None`` for exact."""

    @property
    def equivalent(self) -> bool:
        return self.status is ResultEquivalenceStatus.EQUIVALENT

    @property
    def float_comparison(self) -> str:
        return float_policy(self.float_digits)

    def describe(self, max_rows: int = 10) -> str:
        """Render a human-readable report, including any counterexample."""

        lines = [f"{self.status.value}: {self.reason}", f"floats compared: {self.float_comparison}"]
        if self.failing_seed is not None:
            lines.append(f"failing seed: {self.failing_seed}")
        if self.left_output is not None and self.right_output is not None:
            lines.append(f"left columns:  {self.left_output.columns}")
            lines.append(f"right columns: {self.right_output.columns}")
        for label, rows in (("only in left", self.only_left), ("only in right", self.only_right)):
            if rows:
                lines.append(f"{label} ({len(rows)} rows):")
                lines.extend(f"  {row!r}" for row in rows[:max_rows])
        for label, statements in (("left DuckDB SQL", self.left_duckdb_sql), ("right DuckDB SQL", self.right_duckdb_sql)):
            if statements and self.status is not ResultEquivalenceStatus.EQUIVALENT:
                lines.append(f"{label}:")
                lines.extend(f"  {statement}" for statement in statements)
        return "\n".join(lines)


class ExecutionError(RuntimeError):
    """Raised when SQL cannot be translated or executed locally."""


class QueryTimeout(ExecutionError):
    """A query ran past its time limit and was interrupted."""


class BigQueryWouldFail(ExecutionError):
    """BigQuery fails on this database (see :mod:`kumosql.bigquery_on_duckdb`): try the next one."""


def _execution_error(message: str, exc: BaseException) -> ExecutionError:
    from .bigquery_on_duckdb import is_bigquery_failure

    return (BigQueryWouldFail if is_bigquery_failure(exc) else ExecutionError)(f"{message}: {exc}")


def _bigquery_rows(rows, dialect: str) -> tuple:
    """BigQuery-dialect results read as BigQuery returns them (:func:`kumosql.bigquery_on_duckdb.bigquery_rows`)."""

    if dialect != "bigquery":
        return rows
    from .bigquery_on_duckdb import UnfaithfulOutput, bigquery_rows

    try:
        return tuple(bigquery_rows(rows))
    except UnfaithfulOutput as exc:
        raise BigQueryWouldFail(str(exc)) from exc


# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

# Deliberately small domains: joins find matches, GROUP BY produces multi-row
# groups, and duplicate rows appear, which is where rewrites usually break.
_DOMAINS: dict[str, tuple[Any, ...]] = {
    "INT64": (-1, 0, 1, 2, 3, 4, 5, 7),
    "FLOAT64": (-2.5, 0.0, 0.5, 1.0, 1.25, 3.75, 10.0),
    "NUMERIC": tuple(Decimal(v) for v in ("-1.50", "0", "0.25", "1", "2.75", "100")),
    "STRING": ("", "a", "A", "b", "b ", "café", "x_y", "long value"),
    "BOOL": (True, False),
    "DATE": tuple(date(2024, 1, 1) + timedelta(days=d) for d in (0, 1, 30, 31, 59, 365)),
    "TIMESTAMP": tuple(
        datetime(2024, 1, 1) + timedelta(hours=h) for h in (0, 1, 23, 24, 24 * 31, 24 * 366)
    ),
}

_TYPE_ALIASES = {
    "INT": "INT64",
    "INTEGER": "INT64",
    "BIGINT": "INT64",
    "FLOAT": "FLOAT64",
    "DOUBLE": "FLOAT64",
    "DECIMAL": "NUMERIC",
    "BOOLEAN": "BOOL",
    "DATETIME": "TIMESTAMP",
}

_DUCKDB_TYPES = {
    "INT64": "BIGINT",
    "FLOAT64": "DOUBLE",
    "NUMERIC": "DECIMAL(38, 9)",
    "STRING": "VARCHAR",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
}


def _normalize_type(bq_type: str) -> str:
    upper = bq_type.strip().upper()
    upper = _TYPE_ALIASES.get(upper, upper)
    if upper not in _DOMAINS:
        raise ValueError(f"unsupported synthetic column type: {bq_type!r}")
    return upper


@dataclass(frozen=True)
class SyntheticTable:
    """Column (name, BigQuery type) pairs and the rows generated for them."""

    columns: tuple[tuple[str, str], ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class SyntheticDataset:
    """Deterministic tables keyed by their SQL name (e.g. ``p.d.customers``)."""

    seed: int
    tables: Mapping[str, SyntheticTable] = field(default_factory=dict)


@dataclass(frozen=True)
class DataRules:
    """Declared facts a generated database must respect (names lower-case).

    ``not_null`` columns never hold NULL; each tuple in ``keys`` is a set of
    columns whose non-NULL combinations are unique across rows.
    """

    not_null: frozenset = frozenset()
    keys: tuple = ()


def respect_rules(
    columns: tuple[tuple[str, str], ...], rows: Iterable[Row], rules: DataRules | None
) -> list[Row]:
    """Drop rows that break ``rules`` (a NULL in a NOT NULL column, a repeated key)."""

    if rules is None:
        return list(rows)
    names = [name.lower() for name, _ in columns]
    required = [i for i, n in enumerate(names) if n in rules.not_null]
    keys = [[names.index(c) for c in key if c in names] for key in rules.keys]
    seen: list[set] = [set() for _ in keys]
    kept: list[Row] = []
    for row in rows:
        if any(row[i] is None for i in required):
            continue
        marks = [tuple(row[i] for i in key) for key in keys]
        if any(None not in mark and mark in seen[n] for n, mark in enumerate(marks)):
            continue
        for n, mark in enumerate(marks):
            seen[n].add(mark)
        kept.append(row)
    return kept


_MAX_CONSTANTS_PER_TYPE = 24


def query_constants(*sqls: str) -> dict[str, tuple[Any, ...]]:
    """Values worth generating for each column type, read from the queries' own constants.

    A filter such as ``it.info = 'top 250 rank'`` or ``d_year = 1998`` never matches values
    drawn from a small fixed domain, so both sides of a comparison return nothing and agree
    trivially. Adding the constants (and a string matching each ``LIKE`` pattern) makes such
    filters select rows.
    """

    found: dict[str, list[Any]] = {t: [] for t in ("INT64", "FLOAT64", "NUMERIC", "STRING", "DATE")}
    for sql in sqls:
        try:
            statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
        except sqlglot.errors.SqlglotError:
            continue
        for statement in statements:
            for node in statement.walk():
                if isinstance(node, (exp.Like, exp.ILike)) and isinstance(node.expression, exp.Literal):
                    pattern = node.expression.name
                    found["STRING"].append(pattern.replace("%", "").replace("_", "x"))
                    continue
                if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
                    if node.to.this == exp.DataType.Type.DATE:
                        try:
                            found["DATE"].append(date.fromisoformat(node.this.name[:10]))
                        except ValueError:
                            pass
                    continue
                if not isinstance(node, exp.Literal) or isinstance(node.parent, (exp.Like, exp.ILike, exp.Cast)):
                    continue
                if node.is_string:
                    found["STRING"].append(node.name)
                    try:
                        found["DATE"].append(date.fromisoformat(node.name))
                    except ValueError:
                        pass
                    continue
                text = node.name
                try:
                    if re.fullmatch(r"-?\d+", text):
                        found["INT64"].append(int(text))
                    else:
                        found["FLOAT64"].append(float(text))
                        found["NUMERIC"].append(Decimal(text))
                except (ValueError, ArithmeticError):
                    pass
    out = {}
    for type_name, values in found.items():
        unique = [v for v in dict.fromkeys(values) if v not in _DOMAINS[type_name]]
        if unique:
            out[type_name] = tuple(unique[:_MAX_CONSTANTS_PER_TYPE])
    return out


def _draw(rng: random.Random, col_type: str, extras: Mapping[str, tuple[Any, ...]]) -> Any:
    # Half the values come from the queries' constants when there are any, so that rows
    # matching several filters at once are common, not a rare coincidence.
    if extras.get(col_type) and rng.random() < 0.5:
        return rng.choice(extras[col_type])
    return rng.choice(_DOMAINS[col_type])


def generate_synthetic_dataset(
    schema: Schema,
    *,
    seed: int,
    rows_per_table: int = 25,
    null_rate: float = 0.15,
    rules: Mapping[str, DataRules] | None = None,
    extra_values: Mapping[str, Iterable[Any]] | None = None,
) -> SyntheticDataset:
    """Generate reproducible synthetic rows for every table in ``schema``.

    Seed 0 is always an empty dataset, which catches rewrites that differ only
    when an input is empty (for example aggregates without ``GROUP BY``).
    On every other seed each value is NULL with probability ``null_rate`` (so a
    small table can have none) and each non-empty table gets one exact duplicate
    row, so bag semantics are exercised. With ``rules`` (declared
    NOT NULL columns and keys, by lower-case table name) rows that break a rule
    are dropped, and a table with a key gets no exact duplicate row.
    """

    rng = random.Random(seed)
    extras = {
        name: tuple(v for v in (extra_values or {}).get(name, ()) if v not in values)
        for name, values in _DOMAINS.items()
    }
    tables: dict[str, SyntheticTable] = {}
    for table_name in sorted(schema):
        columns = tuple((name, _normalize_type(t)) for name, t in schema[table_name].items())
        if not columns:
            raise ValueError(f"table {table_name!r} has no columns")
        count = 0 if seed == 0 else rng.randint(1, rows_per_table)
        rows: list[Row] = []
        for _ in range(count):
            rows.append(
                tuple(
                    None if rng.random() < null_rate else _draw(rng, col_type, extras)
                    for _, col_type in columns
                )
            )
        table_rules = rules.get(table_name.lower()) if rules is not None else None
        rows = respect_rules(columns, rows, table_rules)
        if rows and not (table_rules and table_rules.keys):
            rows.append(rng.choice(rows))
            rng.shuffle(rows)
        tables[table_name] = SyntheticTable(columns=columns, rows=tuple(rows))
    return SyntheticDataset(seed=seed, tables=tables)


# ---------------------------------------------------------------------------
# SQL preparation
# ---------------------------------------------------------------------------

_REF_RE = re.compile(
    r"""\$\{\s*ref\(\s*(?P<args>(?:"[^"]*"|'[^']*')(?:\s*,\s*(?:"[^"]*"|'[^']*'))*)\s*\)\s*\}"""
)
_INTERPOLATION_RE = re.compile(r"\$\{")


def sqlx_to_sql(sqlx: str) -> str:
    """Reduce Dataform SQLX to executable SQL.

    ``config``/``js``/``pre_operations``/``post_operations`` blocks are
    dropped and ``${ref("name")}`` / ``${ref("schema", "name")}`` become
    backtick-quoted table names. Any other interpolation fails closed, because
    guessing its value could hide a real difference.
    """

    sql = "".join(text for kind, text in split_sqlx_sections(sqlx) if kind == "sql")

    def replace(match: re.Match[str]) -> str:
        parts = [part.strip()[1:-1] for part in re.findall(r'"[^"]*"|\'[^\']*\'', match.group("args"))]
        return "`" + ".".join(parts) + "`"

    sql = _REF_RE.sub(replace, sql)
    if _INTERPOLATION_RE.search(sql):
        raise ExecutionError("SQLX contains interpolations other than ref(); cannot execute locally")
    return sql


def _table_key(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _local_name(key: str) -> str:
    return "src__" + re.sub(r"[^0-9A-Za-z_]", "_", key.replace(".", "__"))


def _cte_names(statement: exp.Expression) -> set[str]:
    names: set[str] = set()
    for cte in statement.find_all(exp.CTE):
        if cte.alias:
            names.add(cte.alias)
    return names


_MODIFYING = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.TruncateTable)
_MODIFYING_COMMANDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}


def _modifies_table(statement: exp.Expression) -> bool:
    """Whether ``statement`` changes the rows of a table that may already exist.

    DuckDB answers such a statement with its affected-row count, which says
    nothing about the rows written, so the written table is compared instead.
    """

    if isinstance(statement, exp.Command):
        return str(statement.this).upper() in _MODIFYING_COMMANDS
    return isinstance(statement, _MODIFYING)


def _write_target(statement: exp.Expression) -> exp.Table | None:
    if isinstance(statement, exp.Delete) and not isinstance(statement.this, exp.Table):
        # BigQuery's ``DELETE t WHERE ...`` (FROM is optional) parses with the
        # table under ``tables``; move it to ``this`` so DuckDB gets DELETE FROM.
        tables = statement.args.get("tables") or []
        if len(tables) == 1 and isinstance(tables[0], exp.Table):
            statement.set("tables", None)
            statement.set("this", tables[0])
    if isinstance(statement, exp.TruncateTable):
        target = statement.expressions[0] if len(statement.expressions) == 1 else None
    elif isinstance(statement, (exp.Create, *_MODIFYING)):
        target = statement.this
    else:
        return None
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _unsupported_write(statement: exp.Expression, target: exp.Table | None, dialect: str) -> str | None:
    """Why a table-modifying statement cannot be compared by its written table, or ``None``."""

    if target is None:
        return "cannot tell which table it modifies"
    if isinstance(statement, exp.Merge) or (
        isinstance(statement, exp.Update) and (statement.args.get("from_") or statement.args.get("from"))
    ):
        return (
            "MERGE and UPDATE ... FROM: BigQuery fails when a target row matches several "
            "source rows, DuckDB silently uses one of them"
        )
    if dialect == "bigquery" and isinstance(statement, (exp.Update, exp.Delete)) and not statement.args.get("where"):
        return "BigQuery requires a WHERE clause on UPDATE and DELETE"
    return None


def _schema_lookup(schema: Schema) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for key in schema:
        lookup[key.lower()] = key
    return lookup


def prepare_statements(
    sql: str,
    schema: Schema,
    *,
    run_tag: str,
    dialect: str = "bigquery",
) -> tuple[list[str], str | None]:
    """Translate BigQuery SQL (or SQLX) to DuckDB statements for one isolated run.

    Returns the DuckDB statements and the local name of the table the script
    wrote last (``None`` when the final statement is a query). Physical tables
    must appear in ``schema``; unknown tables fail closed instead of being
    silently treated as empty.
    """

    if looks_like_sqlx(sql):
        sql = sqlx_to_sql(sql)
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise ExecutionError(f"{dialect} parse failed: {exc}") from exc
    if not statements:
        raise ExecutionError("no SQL statements to execute")

    lookup = _schema_lookup(schema)
    targets: dict[str, str] = {}
    duckdb_sql: list[str] = []
    last_target: str | None = None
    for index, statement in enumerate(statements):
        ctes = _cte_names(statement)
        target = _write_target(statement)
        modifies = _modifies_table(statement)
        if modifies:
            unsupported = _unsupported_write(statement, target, dialect)
            if unsupported:
                raise ExecutionError(f"statement {index + 1} is not supported: {unsupported}")
        target_local: str | None = None
        if target is not None:
            target_key = _table_key(target).lower()
            target_local = targets.get(target_key)
            if target_local is None:
                target_local = f"__eqv_{run_tag}_target_{len(targets) + 1:03d}"
                if modifies:
                    # INSERT, UPDATE, DELETE and TRUNCATE change existing rows,
                    # so the renamed target starts as a copy of the source table
                    # it stands in for, and is what the script returns.
                    if target_key not in lookup:
                        raise ExecutionError(
                            f"statement {index + 1} writes to {_table_key(target)!r}, "
                            "which is neither created by the script nor in the synthetic schema"
                        )
                    duckdb_sql.append(
                        f'CREATE TABLE "{target_local}" AS SELECT * FROM "{_local_name(lookup[target_key])}"'
                    )
        elif isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            last_target = None
        for table in list(statement.find_all(exp.Table)):
            if table is target:
                continue
            key = _table_key(table)
            if not key:
                continue
            if not table.db and not table.catalog and key in ctes:
                continue
            lowered = key.lower()
            if lowered in targets:
                local = targets[lowered]
            elif lowered in lookup:
                local = _local_name(lookup[lowered])
            else:
                raise ExecutionError(
                    f"statement {index + 1} references table {key!r}, which is not in the synthetic schema"
                )
            if not table.alias and table.name and not isinstance(table.this, exp.Func):
                # Columns may be qualified by the table's own name (``t.a``);
                # keep that name usable after the table is renamed.
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            table.set("catalog", None)
            table.set("db", None)
            table.set("this", exp.to_identifier(local))
        if target is not None:
            # Register the target only after resolving reads, so
            # ``CREATE TABLE t AS SELECT ... FROM t`` reads the source table.
            targets[target_key] = target_local
            last_target = target_local
            if isinstance(statement, (exp.Update, exp.Delete)) and not target.alias:
                # Columns may be qualified by the written table's name (``t.a``).
                target.set("alias", exp.TableAlias(this=exp.to_identifier(target.name)))
            target.set("catalog", None)
            target.set("db", None)
            target.set("this", exp.to_identifier(target_local))
        try:
            if dialect == "bigquery":
                from .bigquery_on_duckdb import faithful

                statement = faithful(statement)
            duckdb_sql.append(spell_for_duckdb(statement).sql(dialect="duckdb"))
        except sqlglot.errors.SqlglotError as exc:
            raise ExecutionError(f"cannot translate statement {index + 1} to DuckDB: {exc}") from exc
    return duckdb_sql, last_target


# ---------------------------------------------------------------------------
# Execution and comparison
# ---------------------------------------------------------------------------


def _connect(dialect: str = "bigquery"):
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ExecutionError(
            "duckdb is required for result equivalence (pip install duckdb)"
        ) from exc
    connection = small_database()
    if dialect == "bigquery":
        from .bigquery_on_duckdb import configure

        configure(connection)
    return connection


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return repr(value) if not isinstance(value, Decimal) else f"'{value}'"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TypeError(f"cannot render synthetic value {value!r}")


def _load_dataset(connection, dataset: SyntheticDataset) -> None:
    for key, table in dataset.tables.items():
        local = _local_name(key)
        column_sql = ", ".join(f'"{name}" {_DUCKDB_TYPES[t]}' for name, t in table.columns)
        connection.execute(f'CREATE TABLE "{local}" ({column_sql})')
        if table.rows:
            # One multi-row INSERT of literals: executemany costs a round trip
            # per row, and bound parameters make DuckDB probe for optional
            # Python modules on every call.
            values_sql = ", ".join(
                "(" + ", ".join(_sql_literal(value) for value in row) + ")" for row in table.rows
            )
            connection.execute(f'INSERT INTO "{local}" VALUES {values_sql}')


def execute_on_dataset(
    sql: str,
    schema: Schema,
    dataset: SyntheticDataset,
    *,
    run_tag: str = "run",
) -> tuple[QueryOutput, list[str]]:
    """Run ``sql`` in a fresh DuckDB connection loaded with ``dataset``."""

    statements, last_target = prepare_statements(sql, schema, run_tag=run_tag)
    connection = _connect()
    try:
        _load_dataset(connection, dataset)
        cursor = None
        for statement in statements:
            try:
                cursor = connection.execute(statement)
            except Exception as exc:
                raise _execution_error(f"DuckDB failed on {statement!r}", exc) from exc
        if last_target is not None:
            cursor = connection.execute(f'SELECT * FROM "{last_target}"')
        if cursor is None or cursor.description is None:
            raise ExecutionError("script produced neither a query result nor a written table")
        try:
            columns = tuple(column[0] for column in cursor.description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
        except Exception as exc:
            raise _execution_error("DuckDB failed while fetching results", exc) from exc
    finally:
        connection.close()
    rows = _bigquery_rows(rows, "bigquery")
    return QueryOutput(columns=columns, rows=rows), statements


class DatasetRunner:
    """Run many single-statement queries over many datasets on one DuckDB connection.

    ``execute_on_dataset`` opens a fresh connection per run; scoring thousands
    of query variants over dozens of databases needs the tables created once and
    only their rows swapped. A query must be one SELECT over physical tables in
    ``schema`` (scripts and writes still go through ``execute_on_dataset``).
    """

    def __init__(self, schema: Schema, dialect: str = "bigquery", settings: Iterable[str] = ()):
        self.schema = schema
        self.dialect = dialect
        self._connection = _connect(dialect)
        for statement in settings:
            self._connection.execute(statement)
        self._loaded: SyntheticDataset | None = None
        columns_by_table = {key: tuple((n, _normalize_type(t)) for n, t in cols.items()) for key, cols in schema.items()}
        for key, columns in columns_by_table.items():
            column_sql = ", ".join(f'"{name}" {_DUCKDB_TYPES[t]}' for name, t in columns)
            self._connection.execute(f'CREATE TABLE "{_local_name(key)}" ({column_sql})')
        self._contents = {_local_name(key): "" for key in columns_by_table}  # each table's rows as literal text
        self._prepared: dict[str, str] = {}

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DatasetRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def prepare(self, sql: str) -> str:
        """The DuckDB text of ``sql`` (cached); raises ``ExecutionError`` if it is not one query."""

        cached = self._prepared.get(sql)
        if cached is None:
            statements, last_target = prepare_statements(sql, self.schema, run_tag="runner", dialect=self.dialect)
            if len(statements) != 1 or last_target is not None:
                raise ExecutionError("a dataset runner takes exactly one query")
            cached = self._prepared[sql] = statements[0]
        return cached

    def load(self, dataset: SyntheticDataset) -> None:
        if dataset is self._loaded:
            return
        for key, table in dataset.tables.items():
            local = _local_name(key)
            values_sql = ", ".join("(" + ", ".join(_sql_literal(value) for value in row) + ")" for row in table.rows)
            # Datasets of one suite often share a table's rows; a table already holding them is left as it is
            current = self._contents.pop(local, None)
            if values_sql == current:
                self._contents[local] = values_sql
                continue
            if current != "":
                self._connection.execute(f'DELETE FROM "{local}"')
            if table.rows:
                self._connection.execute(f'INSERT INTO "{local}" VALUES {values_sql}')
            self._contents[local] = values_sql
        self._loaded = dataset

    def run(self, sql: str, dataset: SyntheticDataset, *, timeout: float | None = None) -> QueryOutput:
        text = self.prepare(sql)
        self.load(dataset)
        timer = None
        if timeout is not None:
            timer = threading.Timer(timeout, self._connection.interrupt)
            timer.start()
        try:
            cursor = self._connection.execute(text)
            columns = tuple(column[0] for column in cursor.description)
            rows = tuple(tuple(row) for row in cursor.fetchall())
        except Exception as exc:
            if timer is not None and not timer.is_alive():
                raise QueryTimeout(f"query ran longer than {timeout} s") from exc
            raise _execution_error(f"DuckDB failed on {text!r}", exc) from exc
        finally:
            if timer is not None:
                timer.cancel()
        return QueryOutput(columns=columns, rows=_bigquery_rows(rows, self.dialect))


def _normalize_value(value: Any, float_digits: int | None, booleans_are_integers: bool = False) -> Any:
    """Encode one result value as a hashable key that keeps its kind.

    Booleans, NaN, arrays and structs get tagged tuples, so ``TRUE`` never equals
    ``1``, NaN never equals the string or array ``"NaN"``, and a struct never
    equals an array of pairs; every non-scalar encoding is such a tuple. Numbers
    stay untagged so ``int``, ``float`` and ``Decimal`` still compare by value:
    DuckDB's result types legitimately differ from BigQuery's (and between two
    equivalent queries), e.g. a ``NUMERIC`` column comes back as ``Decimal`` but
    dividing it gives a ``float``. ``float_digits`` rounds floats to that many significant digits;
    ``None`` compares them exactly. ``booleans_are_integers`` reads ``TRUE`` as ``1`` for corpora written in
    a dialect with no boolean type (MySQL, Calcite), where the same predicate is a boolean in one query and a
    0/1 integer in the other.
    """

    if isinstance(value, bool):
        return int(value) if booleans_are_integers else ("bool", value)
    if isinstance(value, float):
        if math.isnan(value):
            return ("nan",)
        if value == 0:
            return 0.0
        return value if float_digits is None else float(f"{value:.{float_digits}g}")
    if isinstance(value, Decimal):
        return value.normalize() if value == value else ("nan",)
    if isinstance(value, (list, tuple)):
        return ("array", *(_normalize_value(v, float_digits, booleans_are_integers) for v in value))
    if isinstance(value, dict):
        return ("struct", *sorted((k, _normalize_value(v, float_digits, booleans_are_integers)) for k, v in value.items()))
    return value


def _sort_key(row: Row) -> tuple:
    return tuple((value is None, type(value).__name__, repr(value)) for value in row)


def float_policy(float_digits: int | None) -> str:
    """Name the float comparison policy, as recorded on results."""

    return "exact" if float_digits is None else f"{float_digits} significant digits"


def compare_outputs(
    left: QueryOutput,
    right: QueryOutput,
    *,
    ignore_row_order: bool = True,
    check_column_names: bool = True,
    float_digits: int | None = 12,
    booleans_are_integers: bool = False,
) -> tuple[bool, str, tuple[Row, ...], tuple[Row, ...]]:
    """Compare two outputs; returns (equal, reason, only_left, only_right).

    Rows are compared as multisets of type-tagged values (see :func:`_normalize_value`): numbers
    compare by value (``1`` and ``1.0`` are equal, and floats are rounded to ``float_digits``
    significant digits first, or compared exactly with ``None``), but a boolean, NaN, array or
    struct keeps its kind. ``only_left``/``only_right`` hold the rows as the engine returned them.
    """

    if len(left.columns) != len(right.columns):
        return False, f"column counts differ ({len(left.columns)} vs {len(right.columns)})", (), ()
    if check_column_names and [c.lower() for c in left.columns] != [c.lower() for c in right.columns]:
        return False, "column names differ", (), ()

    def keyed(rows: tuple[Row, ...]) -> tuple[list[Row], dict[Row, list[Row]]]:
        keys: list[Row] = []
        originals: dict[Row, list[Row]] = {}
        for row in rows:
            key = tuple(_normalize_value(v, float_digits, booleans_are_integers) for v in row)
            keys.append(key)
            originals.setdefault(key, []).append(row)
        return keys, originals

    def surplus(counts: Counter, originals: dict[Row, list[Row]]) -> tuple[Row, ...]:
        return tuple(
            row
            for key in sorted(counts, key=_sort_key)
            for row in originals[key][: counts[key]]
        )

    left_rows, left_originals = keyed(left.rows)
    right_rows, right_originals = keyed(right.rows)

    left_counts = Counter(left_rows)
    right_counts = Counter(right_rows)
    only_left = surplus(left_counts - right_counts, left_originals)
    only_right = surplus(right_counts - left_counts, right_originals)
    if only_left or only_right:
        return False, "result multisets differ", only_left, only_right
    if not ignore_row_order and left_rows != right_rows:
        return False, "same rows but in a different order", (), ()
    return True, "results match", (), ()


def check_result_equivalence(
    left_sql: str,
    right_sql: str,
    schema: Schema,
    *,
    seeds: Iterable[int] = range(8),
    rows_per_table: int = 25,
    null_rate: float = 0.15,
    ignore_row_order: bool = True,
    check_column_names: bool = True,
    float_digits: int | None = 12,
    use_query_constants: bool = True,
    targeted: bool = False,
) -> ResultEquivalence:
    """Run both queries over several synthetic datasets and compare results.

    With ``targeted`` the random databases are followed by the targeted suite built
    around the left query (:func:`kumosql.targeted_data.database_suite`: corner cases,
    rows at each comparison's boundary, each table empty in turn); it only adds
    databases, so it can only turn "equivalent" into "different".

    With ``use_query_constants`` the generated values include the queries' own constants
    (see :func:`query_constants`), so their filters match some rows.

    Stops at the first seed that yields a counterexample or an execution
    error. An error on either side is never reported as equivalence. Each side
    is executed twice per seed; a side that differs from itself makes the
    result ``INCONCLUSIVE`` instead of equivalent or different.
    """

    extras = query_constants(left_sql, right_sql) if use_query_constants else None
    checked: list[int] = []
    left_sql_out: list[str] = []
    right_sql_out: list[str] = []
    def _datasets():
        for seed in seeds:
            yield seed, generate_synthetic_dataset(
                schema, seed=seed, rows_per_table=rows_per_table, null_rate=null_rate, extra_values=extras
            )
        if targeted:
            from .targeted_data import database_suite

            try:
                suite = database_suite(left_sql, schema, random_seeds=())
            except (ValueError, sqlglot.errors.SqlglotError):
                suite = []
            for labeled in suite:
                yield labeled.dataset.seed, labeled.dataset

    skipped = 0
    for seed, dataset in _datasets():
        try:
            left_output, left_sql_out = execute_on_dataset(
                left_sql, schema, dataset, run_tag=f"left_{seed}"
            )
        except BigQueryWouldFail:
            skipped += 1  # BigQuery fails on this database: it shows nothing either way
            continue
        except ExecutionError as exc:
            return ResultEquivalence(
                ResultEquivalenceStatus.ERROR, f"left side failed: {exc}", tuple(checked), seed,
                float_digits=float_digits,
            )
        try:
            right_output, right_sql_out = execute_on_dataset(
                right_sql, schema, dataset, run_tag=f"right_{seed}"
            )
        except BigQueryWouldFail:
            skipped += 1
            continue
        except ExecutionError as exc:
            return ResultEquivalence(
                ResultEquivalenceStatus.ERROR,
                f"right side failed: {exc}",
                tuple(checked),
                seed,
                left_output=left_output,
                left_duckdb_sql=tuple(left_sql_out),
                float_digits=float_digits,
            )
        # A side that disagrees with itself on identical input cannot support
        # either an equivalence or a counterexample claim.
        for side, side_sql, first_output in (
            ("left", left_sql, left_output),
            ("right", right_sql, right_output),
        ):
            try:
                repeat_output, _ = execute_on_dataset(
                    side_sql, schema, dataset, run_tag=f"{side}_{seed}_repeat"
                )
            except ExecutionError as exc:
                return ResultEquivalence(
                    ResultEquivalenceStatus.ERROR,
                    f"{side} side failed on repeat run: {exc}",
                    tuple(checked),
                    seed,
                    float_digits=float_digits,
                )
            stable, _, _, _ = compare_outputs(
                first_output,
                repeat_output,
                ignore_row_order=ignore_row_order,
                check_column_names=check_column_names,
                float_digits=float_digits,
            )
            if not stable:
                return ResultEquivalence(
                    ResultEquivalenceStatus.INCONCLUSIVE,
                    f"{side} side returned different results on two runs over the same "
                    f"synthetic data (seed {seed}); it is nondeterministic, so the "
                    "comparison proves nothing either way",
                    tuple(checked),
                    seed,
                    left_duckdb_sql=tuple(left_sql_out),
                    right_duckdb_sql=tuple(right_sql_out),
                    float_digits=float_digits,
                )
        checked.append(seed)
        equal, reason, only_left, only_right = compare_outputs(
            left_output,
            right_output,
            ignore_row_order=ignore_row_order,
            check_column_names=check_column_names,
            float_digits=float_digits,
        )
        if not equal:
            from .refute import order_dependence

            try:
                free = order_dependence(left_sql) or order_dependence(right_sql)
            except sqlglot.errors.SqlglotError:
                free = None
            if free:
                return ResultEquivalence(
                    ResultEquivalenceStatus.INCONCLUSIVE,
                    f"the results differ, but a query may pick rows freely ({free}), so the "
                    "difference may be a different choice rather than a different answer",
                    tuple(checked),
                    seed,
                    left_output,
                    right_output,
                    only_left,
                    only_right,
                    tuple(left_sql_out),
                    tuple(right_sql_out),
                )
            return ResultEquivalence(
                ResultEquivalenceStatus.DIFFERENT,
                reason,
                tuple(checked),
                seed,
                left_output,
                right_output,
                only_left,
                only_right,
                tuple(left_sql_out),
                tuple(right_sql_out),
                float_digits=float_digits,
            )
    if not checked and skipped:
        return ResultEquivalence(
            ResultEquivalenceStatus.ERROR, "BigQuery fails on every synthetic dataset", (), None
        )
    if not checked:
        raise ValueError("at least one seed is required")
    return ResultEquivalence(
        ResultEquivalenceStatus.EQUIVALENT,
        f"results match on {len(checked)} synthetic datasets",
        tuple(checked),
        left_duckdb_sql=tuple(left_sql_out),
        right_duckdb_sql=tuple(right_sql_out),
        float_digits=float_digits,
    )


def assert_result_equivalent(left_sql: str, right_sql: str, schema: Schema, **kwargs: Any) -> ResultEquivalence:
    """Pytest-friendly wrapper that raises ``AssertionError`` with a counterexample."""

    result = check_result_equivalence(left_sql, right_sql, schema, **kwargs)
    if not result.equivalent:
        raise AssertionError(result.describe())
    return result
