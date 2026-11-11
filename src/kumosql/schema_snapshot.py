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

The file is **streamed**, one row at a time, and a row of a table the caller does not want is dropped on the spot
(``keep``, see below), so a warehouse-wide export of hundreds of thousands of rows costs the memory of the tables the
project uses, and about a second per 100,000 rows. A JSON *array* is parsed whole (use JSON lines for big files). Nothing is
written anywhere: the loader keeps no cache file.

    load_schema_snapshot("columns.csv", keep=lambda catalog, schema, table: table in project_tables)
    snapshot.ignored    # rows dropped because ``keep`` said no (never stored)
    snapshot.skipped    # rows dropped because they were unusable or repeated
    snapshot.columns_of("sales.orders")[0].is_nullable   # True, False, or None when the file has no is_nullable
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

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
    #: rows of tables the caller did not want (``keep`` said no); they are counted and never stored
    ignored: int = 0
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


#: ``keep(catalog, schema, table)``: whether the rows of this table are wanted (parts the file lacks are ``""``)
Keep = Callable[[str, str, str], bool]


def load_schema_snapshot(path: str | Path, keep: Keep | None = None) -> SchemaSnapshot:
    """Read a snapshot file (CSV, JSON lines or one JSON array), streaming it; raises :class:`SchemaSnapshotError` when it
    cannot be read. ``keep`` drops the rows of unwanted tables as they are read."""

    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SchemaSnapshotError(f"schema snapshot {path.name}: cannot read it ({type(exc).__name__}: {exc.strerror or exc})") from exc
    if size > MAX_BYTES:
        raise SchemaSnapshotError(f"schema snapshot {path.name} is {size:,} bytes, over the {MAX_BYTES:,}-byte limit")
    suffix = path.suffix.lower()
    fmt = "jsonl" if suffix in (".jsonl", ".ndjson", ".json") else "csv" if suffix in (".csv", ".tsv") else None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return _read(handle, fmt, path.name, keep, None)
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaSnapshotError(f"schema snapshot {path.name}: cannot read it as UTF-8 text ({type(exc).__name__})") from exc


def check_schema_snapshot(path: str | Path, rows: int = 200) -> None:
    """Fail fast on a file that is not a snapshot: read its header and the first ``rows`` rows, keep nothing. Raises
    :class:`SchemaSnapshotError`; a server calls it at start so a wrong file is not a silent gap in every answer."""

    path = Path(path)
    suffix = path.suffix.lower()
    fmt = "jsonl" if suffix in (".jsonl", ".ndjson", ".json") else "csv" if suffix in (".csv", ".tsv") else None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            _read(handle, fmt, path.name, lambda *_: False, rows)
    except (OSError, UnicodeDecodeError) as exc:
        raise SchemaSnapshotError(f"schema snapshot {path.name}: cannot read it ({type(exc).__name__})") from exc


def parse_schema_snapshot(text: str, fmt: str | None = None, source: str = "schema snapshot", keep: Keep | None = None) -> SchemaSnapshot:
    """Parse snapshot text; ``fmt`` is ``"csv"`` or ``"jsonl"`` (decided from the first character when ``None``)."""

    return _read(io.StringIO(text, newline=""), fmt, source, keep, None)


def _accepted() -> str:
    return ("expected INFORMATION_SCHEMA.COLUMNS columns: table_catalog, table_schema, table_name, column_name, data_type[, is_nullable] "
            "(catalog/schema/table/column/data_type also work)")


def _read(handle, fmt: str | None, source: str, keep: Keep | None, limit: int | None) -> SchemaSnapshot:
    first = handle.readline()
    while first and not first.strip():
        first = handle.readline()
    if not first:
        raise SchemaSnapshotError(f"{source}: the file is empty; {_accepted()}")
    lead = first.lstrip()[0]
    if fmt is None or (fmt == "jsonl") != (lead in "{["):
        fmt = "jsonl" if lead in "{[" else "csv"
    lines: Iterable[str] = _chain(first, handle)
    rows = _json_rows(lines, source, lead) if fmt == "jsonl" else _csv_rows(lines, first, source)
    return _collect(rows, source, keep, limit)


def _chain(first: str, handle) -> Iterator[str]:
    yield first
    yield from handle


def _csv_rows(lines: Iterable[str], first: str, source: str) -> Iterator[dict]:
    delimiter = "\t" if first.count("\t") > first.count(",") else ","
    try:
        reader = csv.reader(lines, delimiter=delimiter)
        header = next(reader)
        names = [str(h).strip().casefold() for h in header]
        yield {"__header__": names}
        for row in reader:
            yield {"__row__": row}
    except csv.Error as exc:
        raise SchemaSnapshotError(f"{source}: not valid CSV ({exc})") from exc


def _json_rows(lines: Iterable[str], source: str, lead: str) -> Iterator[dict]:
    if lead == "[":
        text = "".join(lines)
        try:
            items = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaSnapshotError(f"{source}: not valid JSON ({exc.msg} at line {exc.lineno})") from exc
        if not isinstance(items, list):
            raise SchemaSnapshotError(f"{source}: expected a JSON array of objects; {_accepted()}")
    else:
        items = _json_lines(lines, source)
    for item in items:
        if not isinstance(item, dict):
            raise SchemaSnapshotError(f"{source}: every JSON line must be an object with the column fields; {_accepted()}")
        yield {"__object__": item}


def _json_lines(lines: Iterable[str], source: str) -> Iterator[object]:
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise SchemaSnapshotError(f"{source}: line {number} is not valid JSON ({exc.msg})") from exc


def _text(value) -> str:
    return "" if value is None else str(value).strip().strip("`")


def _nullable(value) -> bool | None:
    if isinstance(value, bool):
        return value
    folded = _text(value).casefold()
    return True if folded in ("yes", "true", "1", "nullable") else False if folded in ("no", "false", "0", "required") else None


def _positions(names: Iterable[str], source: str) -> dict[str, str]:
    """``{field: the file's name for it}`` from the names the file has; raises when a required field is missing."""

    present = set(names)
    found = {}
    for name, aliases in _FIELDS.items():
        for alias in aliases:
            if alias in present:
                found[name] = alias
                break
    missing = [name for name in _REQUIRED if name not in found]
    if missing:
        raise SchemaSnapshotError(f"{source}: no column for {', '.join(missing)}; {_accepted()}")
    return found


def _collect(rows: Iterator[dict], source: str, keep: Keep | None, limit: int | None) -> SchemaSnapshot:
    columns: list[str] | None = None  # CSV: the header; JSON: the first object's keys
    index: dict[str, int] = {}
    mapping: dict[str, str] = {}
    seen: set[tuple[str, str, str, str]] = set()
    wanted: dict[tuple[str, str, str], bool] = {}
    kept: list[tuple[int, int, SnapshotColumn]] = []
    skipped = ignored = count = 0
    for count, row in enumerate(rows):
        if "__header__" in row:
            columns = row["__header__"]
            mapping = _positions(columns, source)
            index = {field_name: columns.index(alias) for field_name, alias in mapping.items()}
            continue
        if "__row__" in row:
            values = row["__row__"]
            get = lambda name, values=values: values[index[name]] if name in index and index[name] < len(values) else None  # noqa: E731
        else:
            item = {str(k).strip().casefold(): v for k, v in row["__object__"].items()}
            if not mapping:
                mapping = _positions(item, source)
            get = lambda name, item=item: item.get(mapping.get(name, ""))  # noqa: E731
        if limit is not None and count > limit:
            break
        catalog, schema, table = _text(get("catalog")), _text(get("schema")), _text(get("table"))
        name = (catalog, schema, table)
        if name not in wanted:
            wanted[name] = bool(table) and (keep is None or bool(keep(catalog, schema, table)))
        if not wanted[name]:
            if table:
                ignored += 1
            else:
                skipped += 1
            continue
        column, data_type = _text(get("column")), _text(get("data_type"))
        key = (catalog.casefold(), schema.casefold(), table.casefold(), column.casefold())
        if not column or not data_type or key in seen:
            skipped += 1
            continue
        seen.add(key)
        position = _text(get("ordinal_position"))
        kept.append((int(position) if position.isdigit() else len(kept), len(kept), SnapshotColumn(catalog, schema, table, column, data_type, _nullable(get("is_nullable")))))
    if not mapping:
        raise SchemaSnapshotError(f"{source}: it has a header but no rows; {_accepted()}")
    if not kept and keep is None and limit is None:
        raise SchemaSnapshotError(f"{source}: no row has a table, a column and a data type; {_accepted()}")
    kept.sort(key=lambda item: (item[2].table_name.casefold(), item[0], item[1]))
    return SchemaSnapshot(tuple(item[2] for item in kept), skipped, ignored, source)
