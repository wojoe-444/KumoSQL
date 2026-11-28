"""User-defined data sources: a saved query whose result rows scopes and tag rules can use.

A data source is a name, a type (today only ``bigquery_sql``), the SQL, and an optional cache
lifetime in hours (the global BigQuery query cache lifetime when unset). Populating it runs the
single read-only query in the billing project chosen in Settings under the byte cap of :mod:`kumosql.scope_queries`
(dry run first, ``maximumBytesBilled`` on the real run) and keeps the rows in the data folder
(``data-sources/<id>.json``) until the lifetime is over. An expired or edited source is re-run the next time
it is populated. Expired or other-context rows are deleted on access/load;
a failed refresh can use unexpired same-query, same-context rows, flagged stale.

Each saved source is an "applies to" domain of scopes (``source:<id>``, see
:func:`kumosql.scopes.all_domains`), and its result columns are rule fields, like ``user_email``
for job history. A source with a ``full_name`` column (``project.dataset.name``) is also joined to
BigQuery and Dataform objects, so tag rules can use its other columns.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import console, query_context, scope_queries, state
from .resilience import replace_file

SECTION = "data_sources"
PREFIX = "source:"
MAX_SOURCES = 50
MAX_ROWS = 100_000
MAX_NAME = 80
MAX_CACHE_HOURS = 24 * 365
#: Source types: key, then the name shown in Settings. New types register here and in :data:`RUNNERS`.
TYPES = {"bigquery_sql": "BigQuery SQL"}
JOIN_FIELD = "full_name"
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
_lock = threading.Lock()


class DataSourceError(ValueError):
    """A data source could not be saved or populated; the message says what to change."""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    type: str
    query: str
    cache_hours: float | None = None

    @property
    def domain(self) -> str:
        return PREFIX + self.id

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "type": self.type, "query": self.query, "cache_hours": self.cache_hours}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:60] or "source"


def parse_source(data: object) -> Source:
    """Validate one JSON-shaped source (``id`` optional); ``ValueError`` when malformed."""

    if not isinstance(data, Mapping):
        raise ValueError("a data source must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
        raise ValueError(f"a data source needs a name of up to {MAX_NAME} characters")
    kind = data.get("type", "bigquery_sql")
    if kind not in TYPES:
        raise ValueError(f"unknown data source type {kind!r}. Choose from: {', '.join(TYPES)}")
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"data source {name.strip()!r} needs a query")
    if len(query) > scope_queries.MAX_QUERY_CHARS:
        raise ValueError(f"the query can have at most {scope_queries.MAX_QUERY_CHARS:,} characters")
    scope_queries.validate_query(query)
    hours = data.get("cache_hours")
    if hours is not None:
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 <= hours <= MAX_CACHE_HOURS:
            raise ValueError(f"cache_hours must be a number of hours from 0 to {MAX_CACHE_HOURS}")
        hours = float(hours)
    ident = data.get("id")
    if ident is not None and (not isinstance(ident, str) or not _ID.match(ident)):
        raise ValueError("a data source id uses lowercase letters, digits, - and _")
    return Source(ident or _slug(name), name.strip(), kind, query.strip(), hours)


# ------------------------------------------------------------------- storage


_SKIPPED_WARNED: set[str] = set()


def list_sources() -> list[Source]:
    stored = state.get_section(SECTION, [])
    found: list[Source] = []
    for item in stored if isinstance(stored, list) else []:
        try:
            source = parse_source(item)
        except ValueError as exc:
            # Never drop a saved source without a trace: a script or an unparseable query is not run, but the user can fix it.
            if str(exc) not in _SKIPPED_WARNED:
                _SKIPPED_WARNED.add(str(exc))
                console.warn(f"saved data source skipped: {exc}")
            continue
        if source.id not in {s.id for s in found}:
            found.append(source)
    return found


def get_source(ident: str) -> Source | None:
    return next((s for s in list_sources() if s.id == ident), None)


def source_for_domain(domain: str) -> Source | None:
    return get_source(domain[len(PREFIX):]) if domain.startswith(PREFIX) else None


def save_sources(items: object) -> list[Source]:
    """Replace the saved sources. A new source gets an id from its name; a removed one loses its rows and
    its place in the "applies to" list of scopes (a scope that would apply to nothing blocks the removal)."""

    if not isinstance(items, list):
        raise ValueError("data sources must be a list")
    if len(items) > MAX_SOURCES:
        raise ValueError(f"at most {MAX_SOURCES} data sources can be saved")
    parsed = [parse_source(item) for item in items]
    taken: set[str] = set()
    sources: list[Source] = []
    for source in parsed:
        ident, number = source.id, 1
        while ident in taken:
            number += 1
            ident = f"{source.id}-{number}"
        taken.add(ident)
        sources.append(Source(ident, source.name, source.type, source.query, source.cache_hours))
    names = [s.name.casefold() for s in sources]
    if len(set(names)) != len(names):
        raise ValueError("data source names must be unique")
    kept = {s.id for s in sources}
    removed = [s for s in list_sources() if s.id not in kept]
    if removed:
        from . import scopes

        scopes.drop_domains([s.domain for s in removed])
    state.set_section(SECTION, [s.to_json() for s in sources])
    for source in removed:
        _delete_rows(source.id)
    return sources


# ---------------------------------------------------------------------- rows


@dataclass
class Table:
    columns: list[str]
    rows: list[list[str | None]]
    fetched_at: float
    expires_at: float
    stale: bool = False
    error: str | None = None
    estimated_bytes: int | None = None
    bytes_billed: int | None = None

    def records(self) -> list[dict[str, str | None]]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def status(self) -> dict:
        return {
            "rows": len(self.rows), "columns": self.columns, "fetched_at": self.fetched_at,
            "expires_at": self.expires_at, "expired": time.time() >= self.expires_at, "stale": self.stale,
            "error": self.error, "bytes_billed": self.bytes_billed,
        }


def _rows_path(ident: str) -> Path:
    return state.data_path("data-sources", f"{ident}.json")


def _delete_rows(ident: str) -> None:
    try:
        _rows_path(ident).unlink()
    except OSError:
        pass


def _query_hash(source: Source) -> str:
    return hashlib.sha256(f"{source.type}\x1f{source.query}".encode("utf-8")).hexdigest()


def cache_seconds(source: Source) -> float:
    return source.cache_hours * 3600 if source.cache_hours is not None else scope_queries.cache_seconds()


def _prune_rows(context: str) -> None:
    """Remove legacy, expired and other-context rows, including inactive sources."""
    now = time.time()
    sources = {source.id: source for source in list_sources()}
    for path in (state.data_dir() / "data-sources").glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            source = sources.get(path.stem)
            valid = (isinstance(entry, dict) and entry.get("context") == context
                     and isinstance(entry.get("at"), (int, float))
                     and isinstance(entry.get("expires_at"), (int, float))
                     and now < entry["expires_at"]
                     and (source is None or (entry.get("query") == _query_hash(source)
                          and now < entry["at"] + cache_seconds(source))))
            if not valid:
                path.unlink()
        except (OSError, ValueError, TypeError):
            try:
                path.unlink()
            except OSError:
                pass


def _stored(source: Source, context: str | None = None) -> dict | None:
    scope_queries.validate_query(source.query)
    context = context or query_context.execution_context(scope_queries.billing_project())
    _prune_rows(context)
    try:
        entry = json.loads(_rows_path(source.id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(entry, dict) and isinstance(entry.get("columns"), list) and isinstance(entry.get("rows"), list)
    if (ok and isinstance(entry.get("at"), (int, float)) and entry.get("context") == context
            and entry.get("query") == _query_hash(source)
            and time.time() < entry["at"] + cache_seconds(source)):
        return entry
    _delete_rows(source.id)
    return None


def _table(source: Source, entry: dict, *, stale: bool = False, error: str | None = None) -> Table:
    at = float(entry["at"])
    return Table(
        [str(c) for c in entry["columns"]], entry["rows"], at, at + cache_seconds(source), stale, error,
        entry.get("estimated"), entry.get("billed"),
    )


def peek(source: Source) -> Table | None:
    """The unexpired rows of the current source and context, without running anything."""

    entry = _stored(source)
    if entry is None:
        return None
    return _table(source, entry, stale=entry.get("query") != _query_hash(source))


def _save_rows(source: Source, entry: dict) -> None:
    path = _rows_path(source.id)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".source-", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(entry, out)
    replace_file(temp, path)


@dataclass
class Run:
    columns: list[str]
    rows: list[list[str | None]]
    estimated_bytes: int | None = None
    bytes_billed: int | None = None


def _cell(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _bq_runner(source: Source, project: str, max_bytes: int) -> Run:
    """Dry-run, then run the source's query in ``project`` under a byte cap; every column comes back."""

    from urllib.parse import quote, urlencode

    from .dryrun import access_token

    scope_queries.validate_query(source.query)
    plan = scope_queries.dry_run(source.query, None, project=project, max_bytes=max_bytes)
    headers = {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"}
    body = {
        "query": source.query, "useLegacySql": False, "useQueryCache": True, "maximumBytesBilled": str(max_bytes),
        "maxResults": 10000, "timeoutMs": 30000, "labels": {"kumosql": "data_source"},
    }
    if location := query_context.execution_location():
        body["location"] = location
    base = f"{scope_queries._API}/projects/{quote(project, safe='')}"
    status, payload = scope_queries._post(f"{base}/queries", headers, json.dumps(body).encode("utf-8"))
    if status >= 400 or "error" in payload:
        raise scope_queries._api_error(status, payload)
    job = payload.get("jobReference", {})
    where = {"location": job["location"]} if job.get("location") else {}
    deadline = time.time() + scope_queries._POLL_SECONDS

    def fetch(params: dict) -> dict:
        status, page = scope_queries._get(
            f"{base}/queries/{quote(job.get('jobId', ''), safe='')}?{urlencode({**where, **params})}", headers)
        if status >= 400 or "error" in page:
            raise scope_queries._api_error(status, page)
        return page

    while not payload.get("jobComplete", True):
        if time.time() > deadline:
            raise DataSourceError("the query did not finish in time; try again or make it cheaper")
        payload = fetch({"timeoutMs": "30000"})
    columns = [item.get("name", "") for item in payload.get("schema", {}).get("fields", [])] or plan["columns"]
    rows: list[list[str | None]] = []
    while True:
        for row in payload.get("rows", []):
            cells = row.get("f", [])
            rows.append([_cell(cells[i].get("v")) if i < len(cells) else None for i in range(len(columns))])
            if len(rows) > MAX_ROWS:
                raise DataSourceError(f"the query returns more than {MAX_ROWS:,} rows; narrow it")
        token = payload.get("pageToken")
        if not token:
            break
        payload = fetch({"pageToken": token, "maxResults": "10000"})
    billed = payload.get("totalBytesBilled")
    return Run(columns, rows, plan["estimated_bytes"], int(billed) if billed is not None else None)


#: Runners by source type: ``(source, billing project, byte cap) -> Run``. Tests replace entries.
RUNNERS: dict[str, Callable[[Source, str, int], Run]] = {"bigquery_sql": _bq_runner}


def populate(source: Source, *, refresh: bool = False) -> Table:
    """The rows of ``source``: the saved ones while fresh, else the query is run now.

    ``refresh`` runs it regardless of the timer. A failed refresh can return
    unexpired rows for the same query/context, flagged stale; expired rows are deleted.
    """

    scope_queries.validate_query(source.query)
    try:
        project = scope_queries._need_project(None)
    except scope_queries.QueryError as exc:
        raise DataSourceError(str(exc)) from None
    context = query_context.execution_context(project)
    with _lock:
        entry = _stored(source, context)
        now = time.time()
        current = entry is not None and entry.get("query") == _query_hash(source)
        if entry is not None and current and not refresh and now - entry["at"] < cache_seconds(source):
            return _table(source, entry)
        try:
            run = RUNNERS[source.type](source, project, scope_queries.get_settings().max_bytes_billed)
        except scope_queries.QueryError as exc:
            message = str(exc)
        except DataSourceError as exc:
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - credentials, network and library errors read the same to the user
            message = f"could not run the query: {exc}"
        else:
            if not run.columns:
                raise DataSourceError("the query returns no columns")
            fresh = {
                "at": time.time(), "query": _query_hash(source), "columns": run.columns, "rows": run.rows,
                "estimated": run.estimated_bytes, "billed": run.bytes_billed,
                "context": context, "expires_at": time.time() + cache_seconds(source),
            }
            try:
                if (cache_seconds(source) > 0
                        and query_context.execution_context(scope_queries.billing_project()) == context):
                    _save_rows(source, fresh)
            except OSError as exc:
                raise DataSourceError(f"could not save the rows to the data folder: {exc}") from exc
            return _table(source, fresh)
        current_context = query_context.execution_context(scope_queries.billing_project())
        _prune_rows(current_context)
        if entry is not None and current_context == context and time.time() < entry["at"] + cache_seconds(source):
            return _table(source, entry, stale=True, error=message)
        raise DataSourceError(message)


# ----------------------------------------------------- use by scopes and tags


def columns_of(source: Source) -> list[str]:
    entry = _stored(source)
    return [str(c) for c in entry["columns"]] if entry else []


def domains() -> dict[str, str]:
    """``source:<id>`` -> name, for every saved source."""

    return {s.domain: s.name for s in list_sources()}


def field_columns() -> dict[str, list[str]]:
    """Result columns by ``source:<id>`` of every populated source."""

    return {s.domain: cols for s in list_sources() if (cols := columns_of(s))}


def records(source: Source) -> list[dict[str, str | None]]:
    """The saved rows of ``source`` as flat records for rules (nothing is run)."""

    table = peek(source)
    return table.records() if table else []


def join_index() -> list[tuple[str, dict[str, list[dict[str, str | None]]]]]:
    """Rows of every populated source that has a ``full_name`` column, by lower-cased full name.

    Build it once and pass it to :func:`extra_fields` when joining many objects."""

    joins: list[tuple[str, dict[str, list[dict[str, str | None]]]]] = []
    for source in list_sources():
        columns = columns_of(source)
        key = next((c for c in columns if c.casefold() == JOIN_FIELD), None)
        if key is None:
            continue
        by_name: dict[str, list[dict[str, str | None]]] = {}
        for row in records(source):
            if row.get(key):
                by_name.setdefault(str(row[key]).casefold().replace("`", ""), []).append(row)
        joins.append((key, by_name))
    return joins


def extra_fields(index: list, full_name: str, record: Mapping) -> dict[str, object]:
    """The source columns that join to the object ``full_name`` and that ``record`` does not already have.

    A column with several matching rows is a list (a rule matches when any element does)."""

    extra: dict[str, list] = {}
    for key, by_name in index:
        for row in by_name.get(full_name.casefold().replace("`", ""), ()):
            for column, value in row.items():
                if column == key or column in record or value is None:
                    continue
                seen = extra.setdefault(column, [])
                if value not in seen:
                    seen.append(value)
    return {column: values[0] if len(values) == 1 else values for column, values in extra.items()}


def enrich_objects(objects: Mapping[str, Mapping]) -> Mapping[str, Mapping]:
    """Objects joined, by ``full_name``, to the rows of every populated source that has such a column.

    The source's other columns become fields of the matching objects; a field an object already has is
    kept. Nothing changes when no source has a ``full_name`` column.
    """

    index = join_index()
    if not index:
        return objects
    enriched: dict[str, Mapping] = {}
    for name, record in objects.items():
        extra = extra_fields(index, str(record.get(JOIN_FIELD) or name), record)
        enriched[name] = {**record, **extra} if extra else record
    return enriched


def joined_columns() -> list[str]:
    """Every column of the sources joined to objects by ``full_name`` (the join column included)."""

    found: list[str] = []
    for source in list_sources():
        columns = columns_of(source)
        if any(c.casefold() == JOIN_FIELD for c in columns):
            found.extend(c for c in columns if c not in found)
    return found


def tag_fields() -> list[str]:
    """Columns tag rules can use because their source is joined to objects."""

    found: list[str] = []
    for source in list_sources():
        columns = columns_of(source)
        if any(c.casefold() == JOIN_FIELD for c in columns):
            found.extend(c for c in columns if c.casefold() != JOIN_FIELD and c not in found)
    return found


def describe(sources: Iterable[Source] | None = None) -> list[dict]:
    """Each source with the state of its saved rows, for Settings."""

    items = []
    for source in list_sources() if sources is None else sources:
        table = peek(source)
        items.append({**source.to_json(), "domain": source.domain, "status": table.status() if table else None})
    return items
