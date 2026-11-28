"""Read-only BigQuery project, dataset, table, and schema browsing."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

from . import console
from .dryrun import access_token
from .state import data_dir, get_section, set_section
from .resilience import replace_file

_API = "https://bigquery.googleapis.com/bigquery/v2"


class CatalogError(RuntimeError):
    """A BigQuery catalog request failed with a user-facing message."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# Catalog listings and schemas are metadata calls: free, but each is a network
# round trip and the first also mints an OAuth token. Answers are kept in memory
# and in a small file so reopening the page or revisiting an item is instant.
_TOKEN_SECONDS = 1800
_lock = threading.Lock()
_memory: dict[str, dict] = {}
_token: tuple[str, float] | None = None
_disk_loaded = False


def ttl_seconds() -> float:
    """Catalog answers live as long as the query cache lifetime setting (default 48 hours)."""
    return query_cache_seconds()


def _cache_path() -> Path:
    return data_dir() / "catalog-cache.json"


def _load_disk() -> None:
    global _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    try:
        stored = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(stored, dict):
        for key, entry in stored.items():
            if isinstance(entry, dict) and "at" in entry and "data" in entry:
                _memory.setdefault(key, entry)


def _save_disk() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".catalog-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(_memory, out)
        replace_file(temp, path)
    except OSError:
        pass  # the cache is an optimization; never fail a request over it


_batching = 0
_dirty = False


def _changed() -> None:
    """Save the cache now, or once the enclosing :func:`batched_saves` block ends. Call with ``_lock`` held."""

    global _dirty
    if _batching:
        _dirty = True
    else:
        _save_disk()


@contextmanager
def batched_saves() -> Iterator[None]:
    """Write the cache file once at the end instead of after every answer (it is rewritten whole each time)."""

    global _batching, _dirty
    with _lock:
        _batching += 1
    try:
        yield
    finally:
        with _lock:
            _batching -= 1
            if not _batching and _dirty:
                _dirty = False
                _save_disk()


def clear_cache() -> None:
    global _disk_loaded
    with _lock:
        _memory.clear()
        _disk_loaded = True
        try:
            _cache_path().unlink()
        except OSError:
            pass


def forget_prefix(prefix: str) -> int:
    """Drop saved answers whose key starts with ``prefix`` (memory and disk); returns how many."""

    _load_disk()
    with _lock:
        keys = [key for key in _memory if isinstance(key, str) and key.startswith(prefix)]
        for key in keys:
            del _memory[key]
        if keys:
            _save_disk()
    return len(keys)


_refreshing: set[str] = set()


def _refresh_in_background(key: str, fetch) -> None:
    """Re-fetch ``key`` on a worker thread; a failure leaves the saved copy in place."""

    def work() -> None:
        parts = key.split("\x1f") if isinstance(key, str) else [str(item) for item in key]
        for kind, value in zip(("project", "dataset", "table"), parts[1:4]):
            console.register(kind, value)
        try:
            with console.task("BigQuery catalog refresh", warn_after=30):
                data = fetch()
                with _lock:
                    _memory[key] = {"at": time.time(), "data": data}
                    _save_disk()
        except (CatalogError, RuntimeError, OSError):
            pass  # task() already logged the reason
        finally:
            with _lock:
                _refreshing.discard(key)

    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)
    threading.Thread(target=work, daemon=True).start()


def peek(key: str) -> dict | None:
    """The saved answer for ``key`` in ``cached`` form, or ``None``; never calls BigQuery or any other service."""
    with _lock:
        _load_disk()
        entry = _memory.get(key)
    if entry is None:
        return None
    return {"data": entry["data"], "fetchedAt": entry["at"], "cached": True,
            "stale": False, "refreshing": time.time() - entry["at"] >= ttl_seconds()}


def saved_tables() -> list[tuple[str, str, str, dict]]:
    """``(project, dataset, table, metadata)`` for every table whose schema is saved; calls nothing."""

    with _lock:
        _load_disk()
        entries = list(_memory.items())
    found = []
    for key, entry in entries:
        parts = key.split("\x1f") if isinstance(key, str) else []
        if len(parts) == 4 and parts[0] == "table" and isinstance(entry.get("data"), dict):
            found.append((parts[1], parts[2], parts[3], entry["data"]))
    return found


def cached(key: str, fetch, refresh: bool = False) -> dict:
    """Return ``{"data", "fetchedAt", "cached", "stale", "refreshing"}`` for ``key``.

    Saved answers are served instantly. One older than the cache lifetime is
    still returned at once and re-fetched in the background (``refreshing``);
    ``refresh`` fetches now instead. If BigQuery fails on a forced refresh and
    an older copy exists, that copy is returned marked ``stale``.
    """
    with _lock:
        _load_disk()
        entry = _memory.get(key)
        busy = key in _refreshing
    now = time.time()
    if entry and not refresh:
        expired = now - entry["at"] >= ttl_seconds()
        if expired:
            _refresh_in_background(key, fetch)
        return {"data": entry["data"], "fetchedAt": entry["at"], "cached": True,
                "stale": False, "refreshing": expired or busy}
    try:
        data = fetch()
    except (CatalogError, RuntimeError):
        if entry:
            return {"data": entry["data"], "fetchedAt": entry["at"], "cached": True,
                    "stale": True, "refreshing": False}
        raise
    with _lock:
        _memory[key] = {"at": now, "data": data}
        _changed()
    return {"data": data, "fetchedAt": now, "cached": False, "stale": False, "refreshing": False}


def forget_table(project: str, dataset: str, table: str) -> None:
    """Drop ``table`` from the cached table list of its dataset (it turned out to be inaccessible)."""
    key = "\x1f".join(("tables", project, dataset))
    with _lock:
        entry = _memory.get(key)
        if entry:
            entry["data"] = [item for item in entry["data"] if item.get("id") != table]
            _save_disk()


_token_lock = threading.Lock()
token_refreshes = 0  # times a new access token was fetched in this process (a count for the log, never the token)


def _token_cached() -> str:
    """One access token for every request, refreshed once when it ages out (safe from many threads)."""

    global _token, token_refreshes
    if os.environ.get("BQ_ACCESS_TOKEN"):
        return access_token()
    current = _token
    if current and time.time() < current[1]:
        return current[0]
    with _token_lock:  # concurrent callers wait for one refresh instead of each doing their own
        if _token and time.time() < _token[1]:
            return _token[0]
        value = access_token()
        token_refreshes += 1
        _token = (value, time.time() + _TOKEN_SECONDS)
        return value


def _note_path(path: str) -> None:
    """Register the project, dataset and table in an API path so the log shows placeholders for them."""

    kinds = {"projects": "project", "datasets": "dataset", "tables": "table"}
    parts = path.split("/")
    for index, part in enumerate(parts[:-1]):
        if part in kinds:
            console.register(kinds[part], unquote(parts[index + 1]))


def _get(path: str, params: dict[str, str] | None = None) -> dict:
    _note_path(path)
    started = time.monotonic()
    try:
        result = _get_once(path, params)
    except CatalogError as exc:
        # A 403 or 404 is how a project the user cannot use is found out, so only other failures are warnings.
        denied = exc.status in (403, 404)
        console.say(f"BigQuery request GET failed: HTTP {exc.status or 'none'} after {time.monotonic() - started:.1f}s",
                    console=not denied, level="INFO" if denied else "WARN")
        raise
    console.say(f"BigQuery request GET: ok in {time.monotonic() - started:.1f}s", console=False)
    return result


def _get_once(path: str, params: dict[str, str] | None = None) -> dict:
    token = _token_cached()
    url = f"{_API}/{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            payload = {}
        error = payload.get("error", {})
        message = error.get("message") or f"BigQuery returned HTTP {exc.code}"
        raise CatalogError(message, exc.code) from exc
    except URLError as exc:
        raise CatalogError(f"Could not connect to BigQuery: {exc.reason}") from exc


def _list(path: str, key: str, params: dict[str, str] | None = None) -> list[dict]:
    items: list[dict] = []
    query = {"maxResults": "1000", **(params or {})}
    while True:
        payload = _get(path, query)
        items.extend(payload.get(key, []))
        token = payload.get("nextPageToken")
        if not token:
            return items
        query["pageToken"] = token


def can_browse(project: str) -> bool:
    """Whether the credentials may list datasets in ``project``.

    Only a definite denial (403 or 404, which includes BigQuery being disabled
    there) hides a project; a network or server error keeps it.
    """
    try:
        _get(f"projects/{quote(project, safe='')}/datasets", {"maxResults": "1", "all": "true"})
    except CatalogError as exc:
        return exc.status not in (403, 404)
    return True


def list_projects() -> list[dict[str, str]]:
    """List every project the credentials hold some role on.

    This can be long (it is not the console's starred list) and is not probed
    for BigQuery access, so it is only requested while choosing projects.
    ``can_browse`` checks the ones actually chosen.
    """
    return [
        {
            "id": item.get("id", ""),
            "name": item.get("friendlyName") or item.get("name") or item.get("id", ""),
        }
        for item in _list("projects", "projects")
    ]


_PROJECT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9.:_-]{0,99}$")
MAX_SELECTED = 100


def selected_projects() -> list[str]:
    """The project ids chosen for the BigQuery tab (empty until the user picks some)."""
    stored = get_section("bigquery", {})
    items = stored.get("projects", []) if isinstance(stored, dict) else []
    chosen = [item for item in items if isinstance(item, str) and _PROJECT_ID.match(item)]
    console.register("project", chosen)
    return chosen


def select_projects(projects: list) -> list[str]:
    """Validate and save the chosen projects; refuses ids BigQuery denies access to."""
    if not isinstance(projects, list) or not all(isinstance(item, str) for item in projects):
        raise ValueError("projects must be a list of project ids")
    ids = list(dict.fromkeys(item.strip() for item in projects if item.strip()))
    if len(ids) > MAX_SELECTED:
        raise ValueError(f"choose at most {MAX_SELECTED} projects")
    bad = [item for item in ids if not _PROJECT_ID.match(item)]
    if bad:
        raise ValueError(f"not a project id: {bad[0]}")
    previous = set(selected_projects())
    new = [item for item in ids if item not in previous]
    if new:
        with ThreadPoolExecutor(max_workers=min(8, len(new))) as pool:
            allowed = list(pool.map(can_browse, new))
        denied = [item for item, ok in zip(new, allowed) if not ok]
        if denied:
            raise ValueError(f"No access to BigQuery in {', '.join(denied)}")
    _save({"projects": ids})
    return ids


# --- Settings shared with other features (scope rules that run SQL, for example) ---

DEFAULT_QUERY_CACHE_HOURS = 48
MAX_QUERY_CACHE_HOURS = 24 * 365


class BillingProjectRequired(RuntimeError):
    """Raised by anything that runs a query before a billing project is chosen."""


def _stored() -> dict:
    stored = get_section("bigquery", {})
    return dict(stored) if isinstance(stored, dict) else {}


def _save(changes: dict) -> None:
    set_section("bigquery", {**_stored(), **changes})


def billing_project() -> str:
    """The project query jobs run and bill in, or "" when none has been chosen."""
    value = _stored().get("billingProject", "")
    if isinstance(value, str) and _PROJECT_ID.match(value):
        console.register("project", value)
        return value
    return ""


def require_billing_project() -> str:
    """Return the billing project, or raise ``BillingProjectRequired`` with a user-facing message."""
    project = billing_project()
    if not project:
        raise BillingProjectRequired(
            "A billing project is needed to run queries. Choose one under BigQuery projects in Settings."
        )
    return project


def query_cache_hours() -> float:
    """How long results of queries KumoSQL runs are reused (default 48 hours)."""
    value = _stored().get("queryCacheHours", DEFAULT_QUERY_CACHE_HOURS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= MAX_QUERY_CACHE_HOURS:
        return DEFAULT_QUERY_CACHE_HOURS
    return value


def query_cache_seconds() -> float:
    return query_cache_hours() * 3600


def bigquery_settings() -> dict:
    return {
        "projects": selected_projects(),
        "billingProject": billing_project(),
        "queryCacheHours": query_cache_hours(),
    }


def save_settings(billing: object = None, cache_hours: object = None) -> dict:
    """Save the billing project and query cache lifetime; ``None`` leaves a value unchanged.

    A billing project is checked with a free dry run, which also proves the
    credentials may create jobs there. An empty string clears it.
    """
    changes: dict = {}
    if billing is not None:
        if not isinstance(billing, str):
            raise ValueError("billingProject must be a project id")
        billing = billing.strip()
        if billing:
            if not _PROJECT_ID.match(billing):
                raise ValueError(f"not a project id: {billing}")
            if billing != billing_project():
                from .dryrun import dry_run

                try:
                    check = dry_run("SELECT 1", billing)
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from exc
                if not check.ok:
                    raise ValueError(
                        f"Cannot run queries in {billing}: {check.error_message or check.error_reason}"
                    )
        changes["billingProject"] = billing
    if cache_hours is not None:
        if isinstance(cache_hours, bool) or not isinstance(cache_hours, (int, float)) \
                or not 0 <= cache_hours <= MAX_QUERY_CACHE_HOURS:
            raise ValueError(f"query cache hours must be a number from 0 to {MAX_QUERY_CACHE_HOURS}")
        changes["queryCacheHours"] = cache_hours
    if changes:
        _save(changes)
    return bigquery_settings()


def _can_list_tables(project: str, dataset: str) -> bool:
    """Whether the credentials may list tables in ``dataset`` (same rule as projects)."""
    try:
        _get(
            f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}/tables",
            {"maxResults": "1"},
        )
    except CatalogError as exc:
        return exc.status not in (403, 404)
    return True


def list_datasets(project: str) -> list[dict[str, str]]:
    """List datasets the credentials can browse.

    Names starting with an underscore are hidden anonymous datasets that hold
    cached query results (the BigQuery console hides them too); datasets whose
    table listing BigQuery denies are dropped.
    """
    datasets = [
        {"id": item.get("datasetReference", {}).get("datasetId", ""),
         "location": item.get("location", "")}
        for item in _list(f"projects/{quote(project, safe='')}/datasets", "datasets", {"all": "true"})
    ]
    datasets = [item for item in datasets if not item["id"].startswith("_")]
    if not datasets:
        return datasets
    with ThreadPoolExecutor(max_workers=min(8, len(datasets))) as pool:
        allowed = list(pool.map(lambda item: _can_list_tables(project, item["id"]), datasets))
    return [item for item, ok in zip(datasets, allowed) if ok]


def datasets_key(project: str) -> str:
    return f"datasets\x1fbrowsable\x1f{project}"


def tables_key(project: str, dataset: str) -> str:
    return f"tables\x1f{project}\x1f{dataset}"


#: BigQuery's routine types, as shown in the explorer and used by tag rules (``type``).
ROUTINE_TYPES = {
    "SCALAR_FUNCTION": "UDF", "TABLE_VALUED_FUNCTION": "TABLE_FUNCTION",
    "AGGREGATE_FUNCTION": "AGGREGATE_FUNCTION", "PROCEDURE": "PROCEDURE",
}


def is_routine(object_type: str) -> bool:
    return object_type in ROUTINE_TYPES.values()


def list_tables(project: str, dataset: str) -> list[dict[str, str]]:
    """Everything inside the dataset: tables, views and materialized views, then functions and procedures.

    Routines come from a second call; if BigQuery refuses it, the tables are still returned.
    """
    base = f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}"
    items = [
        {"id": item.get("tableReference", {}).get("tableId", ""),
         "type": item.get("type", "TABLE")}
        for item in _list(f"{base}/tables", "tables")
    ]
    try:
        routines = _list(f"{base}/routines", "routines")
    except CatalogError:
        routines = []
    items.extend(
        {"id": item.get("routineReference", {}).get("routineId", ""),
         "type": ROUTINE_TYPES.get(item.get("routineType", ""), "UDF")}
        for item in routines
    )
    return items


def get_table(project: str, dataset: str, table: str) -> dict:
    """Return table metadata with its BigQuery schema."""
    # tables.get reads metadata only, never rows, so a table that requires a partition filter is no problem.
    payload = _get(
        f"projects/{quote(project, safe='')}/datasets/{quote(dataset, safe='')}/tables/{quote(table, safe='')}",
        {"fields": "tableReference,type,numRows,schema,tableConstraints"},
    )
    reference = payload.get("tableReference", {})
    return {
        "id": reference.get("tableId", table),
        "type": payload.get("type", "TABLE"),
        "numRows": payload.get("numRows"),
        "schema": payload.get("schema", {}).get("fields", []),
        "constraints": payload.get("tableConstraints"),
    }
