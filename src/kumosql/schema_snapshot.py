"""A schema snapshot: the column names and types of tables, read from a file produced outside the server.

The file has the shape of BigQuery's ``INFORMATION_SCHEMA.COLUMNS``, one row per column::

    table_catalog,table_schema,table_name,column_name,data_type,is_nullable
    my-project,sales,orders,order_id,INT64,NO
    my-project,sales,orders,placed_at,TIMESTAMP,YES

Someone with warehouse access exports it once (a free metadata query) and refreshes it when tables change; KumoSQL
only reads the file and never fetches a schema over the network. CSV (with a header row) and JSON lines (one object per
line, or one JSON array of objects) are read. Header names are not case sensitive, and both spellings are accepted:

================  ===============================  =========================================================
Field             Accepted names                   Notes
================  ===============================  =========================================================
catalog           ``table_catalog``, ``catalog``,  optional (the project); blank means unknown
                  ``project``
schema            ``table_schema``, ``schema``,    optional (the dataset)
                  ``dataset``
table             ``table_name``, ``table``        required
column            ``column_name``, ``column``      required
data_type         ``data_type``, ``type``          required, BigQuery type text such as ``INT64`` or
                                                   ``STRUCT<a STRING>``
is_nullable       ``is_nullable``, ``nullable``    optional: ``YES``/``NO``/``true``/``false``
ordinal_position  ``ordinal_position``             optional: columns are listed in this order
================  ===============================  =========================================================

Use::

    snapshot = load_schema_snapshot("columns.csv")      # raises SchemaSnapshotError when it cannot be read
    snapshot.tables()                                   # {"proj.sales.orders": (SnapshotColumn, ...)}, lower case keys
    snapshot.columns_of("sales.orders")                 # the columns of the one table that name finds, or None
    snapshot.types_of("sales.orders")                   # {"order_id": "INT64", ...}, the type text as written

A row without a table, column or type is skipped and counted in ``skipped``; a second row for the same column of a
table is skipped too (``INFORMATION_SCHEMA.COLUMNS`` has none). The loader never guesses a type and never contacts a
warehouse. ``Session.schema_snapshot()`` gives the snapshot a server was started with.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

#: a snapshot file larger than this is refused, not read (a column list of any real warehouse is far smaller)
MAX_BYTES = 200_000_000

_FIELDS = {
    "catalog": ("table_catalog", "catalog", "project", "project_id", "table_project"),
    "schema": ("table_schema", "schema", "dataset", "dataset_id", "table_dataset"),
    "table": ("table_name", "table", "table_id"),
    "column": ("column_name", "column"),
    "data_type": ("data_type", "type"),
    "is_nullable": ("is_nullable", "nullable"),
    "ordinal_position": ("ordinal_position", "position"),
}
_REQUIRED = ("table", "column", "data_type")


class SchemaSnapshotError(ValueError):
    """The snapshot file cannot be read as a column list; the message says what is wrong and what is accepted."""


@dataclass(frozen=True)
class SnapshotColumn:
    catalog: str
    schema: str
    table: str
    column: str
    data_type: str
    is_nullable: bool | None = None

    @property
    def table_name(self) -> str:
        """``catalog.schema.table`` with the parts the snapshot lacks left out."""

        return ".".join(part for part in (self.catalog, self.schema, self.table) if part)


@dataclass
class SchemaSnapshot:
    """The rows of a snapshot, grouped by table."""

    columns: tuple[SnapshotColumn, ...] = ()
    #: rows that were not used: missing table, column or type, or a repeated column
    skipped: int = 0
    #: where it was read from, for messages
    source: str = "schema snapshot"
    _tables: dict[str, tuple[SnapshotColumn, ...]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        grouped: dict[str, list[SnapshotColumn]] = {}
        for column in self.columns:
            grouped.setdefault(column.table_name.casefold(), []).append(column)
        self._tables = {name: tuple(columns) for name, columns in grouped.items()}

    def __len__(self) -> int:
        return len(self._tables)

    def tables(self) -> dict[str, tuple[SnapshotColumn, ...]]:
        """Every table by its lower-case name (``catalog.schema.table``, parts the file lacks left out)."""

        return dict(self._tables)

    def columns_of(self, name: str) -> tuple[SnapshotColumn, ...] | None:
        """The columns of the table spelled ``name`` (any case, backticks allowed): the exact full name, else the one
        table whose name ends with it. ``None`` when there is none or the spelling is ambiguous."""

        key = name.replace("`", "").strip().casefold()
        if key in self._tables:
            return self._tables[key]
        owners = [full for full in self._tables if full.endswith("." + key)]
        return self._tables[owners[0]] if len(owners) == 1 else None

    def types_of(self, name: str) -> dict[str, str] | None:
        """``{column: data_type}`` of the table ``name`` finds (see :meth:`columns_of`), in column order."""

        columns = self.columns_of(name)
        return {c.column: c.data_type for c in columns} if columns is not None else None


def load_schema_snapshot(path: str | Path) -> SchemaSnapshot:
    """Read a snapshot file (CSV, JSON lines or one JSON array); raises :class:`SchemaSnapshotError` when it cannot."""

    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SchemaSnapshotError(f"schema snapshot {path.name}: cannot read it ({type(exc).__name__}: {exc.strerror or exc})") from exc
    if size > MAX_BYTES:
        raise SchemaSnapshotError(f"schema snapshot {path.name} is {size:,} bytes, over the {MAX_BYTES:,}-byte limit")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaSnapshotError(f"schema snapshot {path.name}: cannot read it as UTF-8 text ({type(exc).__name__})") from exc
    suffix = path.suffix.lower()
    fmt = "jsonl" if suffix in (".jsonl", ".ndjson", ".json") else "csv" if suffix in (".csv", ".tsv") else None
    return parse_schema_snapshot(text, fmt, source=path.name)


def parse_schema_snapshot(text: str, fmt: str | None = None, source: str = "schema snapshot") -> SchemaSnapshot:
    """Parse snapshot text; ``fmt`` is ``"csv"`` or ``"jsonl"`` (decided from the first character when ``None``)."""

    stripped = text.lstrip()
    if not stripped:
        raise SchemaSnapshotError(f"{source}: the file is empty; {_accepted()}")
    if fmt is None or (fmt == "jsonl") != (stripped[0] in "{["):
        fmt = "jsonl" if stripped[0] in "{[" else "csv"
    rows = _json_rows(text, source) if fmt == "jsonl" else _csv_rows(text, source)
    return _snapshot(rows, source)


def _accepted() -> str:
    return ("expected INFORMATION_SCHEMA.COLUMNS columns: table_catalog, table_schema, table_name, column_name, data_type[, is_nullable] "
            "(catalog/schema/table/column/data_type also work)")


def _csv_rows(text: str, source: str) -> list[dict]:
    first = text.lstrip().splitlines()[0]
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    try:
        return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    except csv.Error as exc:
        raise SchemaSnapshotError(f"{source}: not valid CSV ({exc})") from exc


def _json_rows(text: str, source: str) -> list[dict]:
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SchemaSnapshotError(f"{source}: not valid JSON ({exc.msg} at line {exc.lineno})") from exc
        items = data
    else:
        items = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SchemaSnapshotError(f"{source}: line {number} is not valid JSON ({exc.msg})") from exc
    if not all(isinstance(item, dict) for item in items):
        raise SchemaSnapshotError(f"{source}: every JSON line must be an object with the column fields; {_accepted()}")
    return items


def _lookup(row: dict, name: str):
    folded = {str(key).strip().casefold(): value for key, value in row.items() if key is not None}
    for alias in _FIELDS[name]:
        if alias in folded:
            return folded[alias]
    return None


def _text(value) -> str:
    return "" if value is None else str(value).strip().strip("`")


def _nullable(value) -> bool | None:
    if isinstance(value, bool):
        return value
    folded = _text(value).casefold()
    return True if folded in ("yes", "true", "1", "nullable") else False if folded in ("no", "false", "0", "required") else None


def _snapshot(rows: list[dict], source: str) -> SchemaSnapshot:
    if not rows:
        raise SchemaSnapshotError(f"{source}: it has a header but no rows; {_accepted()}")
    present = {str(key).strip().casefold() for key in rows[0] if key is not None}
    missing = [name for name in _REQUIRED if not any(alias in present for alias in _FIELDS[name])]
    if missing:
        raise SchemaSnapshotError(f"{source}: no column for {', '.join(missing)}; {_accepted()}")
    seen: set[tuple[str, str]] = set()
    kept: list[tuple[int, int, SnapshotColumn]] = []
    skipped = 0
    for index, row in enumerate(rows):
        column = SnapshotColumn(
            _text(_lookup(row, "catalog")), _text(_lookup(row, "schema")), _text(_lookup(row, "table")),
            _text(_lookup(row, "column")), _text(_lookup(row, "data_type")), _nullable(_lookup(row, "is_nullable")),
        )
        position = _text(_lookup(row, "ordinal_position"))
        key = (column.table_name.casefold(), column.column.casefold())
        if not (column.table and column.column and column.data_type) or key in seen:
            skipped += 1
            continue
        seen.add(key)
        kept.append((int(position) if position.isdigit() else index, index, column))
    if not kept:
        raise SchemaSnapshotError(f"{source}: no row has a table, a column and a data type; {_accepted()}")
    kept.sort(key=lambda item: (item[2].table_name.casefold(), item[0], item[1]))
    return SchemaSnapshot(tuple(item[2] for item in kept), skipped, source)
