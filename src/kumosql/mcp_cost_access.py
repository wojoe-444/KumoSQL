"""Cost and access for ``check_query`` and ``describe_table``: what will this scan, and can this account read it?

A free BigQuery dry run (:mod:`kumosql.dryrun`) already says how many bytes a query would process. This module adds what an
agent kept finding out the slow way:

* **a byte figure and a readable/denied verdict per source table**, from the dry run: when the whole query plans, every table it
  reads is readable; when BigQuery answers ``Access Denied`` for a table it names that table, and each other source is probed with
  its own ``SELECT *`` dry run (a few at most). Nothing here ever runs a query; dry runs read no data and cost nothing;
* **a denial is remembered** for a while, per account and table, so the next answer says "not readable with the current account
  (cached, N min ago; recheck with refresh=true)" instead of sending the same request again. Byte estimates are remembered the same
  way, per normalized SQL, and a table whose ``SELECT *`` was dry-run has a known full-scan size that ``describe_table`` shows;
* **a query over the byte cap** is answered with the cap, the estimate and the cheapest remedy, never "raise the cap".

The cache keeps the table name, the outcome, the byte count and a timestamp under a non-secret account namespace
(:func:`kumosql.query_context.execution_context`): no credential, token, error text or SQL. It lives in memory. Set
``KUMOSQL_ACCESS_CACHE_FILE`` to ``user`` (the file ``kumosql/access-cache.json`` in the user cache folder) or to a path to also
keep it in a file, so a restart or a second server process knows what the first learned; a path inside the project folder is
refused, because the server never writes into the repository. The file is written atomically after merging what other processes
wrote. ``KUMOSQL_ACCESS_TTL_SECONDS`` (default 3600, ``0`` turns the cache off) changes how long an entry counts, and
``refresh=true`` on ``check_query`` ignores the cache for that call.

Every request here is a dry run, sent only inside a call that passed ``dry_run=true`` and a ``project``, and a dry run job is
created in that project (the one the caller chose to bill), never in a source project, so a read-only upstream project is never
written to.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import sqlglot
from sqlglot import exp

from .mcp_answers import Answer, clip

DEFAULT_TTL_SECONDS = 3600
MAX_PROBES = 3
MAX_ENTRIES = 500
TTL_ENV = "KUMOSQL_ACCESS_TTL_SECONDS"
FILE_ENV = "KUMOSQL_ACCESS_CACHE_FILE"


def user_cache_file() -> Path:
    """``kumosql/access-cache.json`` in the user cache folder (``XDG_CACHE_HOME``, else ``~/.cache``), outside every project."""

    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "kumosql" / "access-cache.json"

NO_CONNECTION = "not checked: no connection (dry_run=true with project=<the BigQuery project to bill> asks BigQuery; it is free)"


# ----------------------------------------------------------------------- helpers


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} bytes"  # pragma: no cover


def sql_hash(sql: str) -> str:
    """A hash of the query that ignores spacing, comments and keyword case, never the spelling of a table or column."""

    try:
        text = sqlglot.parse_one(sql, read="bigquery").sql(dialect="bigquery", comments=False)
    except Exception:  # noqa: BLE001 - the unparsed text still names the query
        text = " ".join(sql.split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _default_identity() -> str:
    from . import query_context

    return query_context.execution_context("")


# ------------------------------------------------------------------------- cache


class AccessCache:
    """Denials, readable tables with their full-scan size, and dry-run estimates, each with the time it was learned."""

    def __init__(
        self,
        ttl: float = DEFAULT_TTL_SECONDS,
        *,
        clock: Callable[[], float] = time.time,
        identity: Callable[[], str] = _default_identity,
        path: Path | None = None,
    ) -> None:
        self.ttl = ttl
        self.clock = clock
        self.identity = identity
        self.path = path
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._seen = None  # (mtime, size) of the file when it was last read
        if path is not None:
            self._merge_file()

    @classmethod
    def from_environment(cls, root: Path | None = None) -> "AccessCache":
        """The cache the environment asks for; a file inside the project folder ``root`` is refused (memory only)."""

        try:
            ttl = float(os.environ.get(TTL_ENV, DEFAULT_TTL_SECONDS))
        except ValueError:
            ttl = DEFAULT_TTL_SECONDS
        wanted = (os.environ.get(FILE_ENV) or "").strip()
        path = None
        if wanted:
            path = user_cache_file() if wanted.lower() == "user" else Path(wanted).expanduser()
            if root is not None and Path(os.path.abspath(path)).is_relative_to(Path(os.path.abspath(root))):
                print(f"kumosql mcp: {FILE_ENV} is inside the project folder; the access cache stays in memory", file=sys.stderr)
                path = None
        return cls(ttl, path=path)

    # ----------------------------------------------------------------- entries

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def _get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            self._sync()
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self.clock() - entry["at"] >= self.ttl:
                del self._entries[key]
                return None
            return entry

    def _put(self, key: str, entry: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries[key] = {**entry, "at": self.clock()}
            if len(self._entries) > MAX_ENTRIES:
                for old in sorted(self._entries, key=lambda k: self._entries[k]["at"])[: len(self._entries) - MAX_ENTRIES]:
                    del self._entries[old]
            self._save()

    def _table_key(self, table: str) -> str:
        return f"t|{self.identity()}|{table.casefold()}"

    def _estimate_key(self, project: str, sql: str) -> str:
        return f"q|{self.identity()}|{project.casefold()}|{sql_hash(sql)}"

    # ------------------------------------------------------------------ tables

    def denial(self, table: str) -> dict | None:
        entry = self._get(self._table_key(table))
        return entry if entry is not None and entry["outcome"] == "denied" else None

    def readable(self, table: str) -> dict | None:
        entry = self._get(self._table_key(table))
        return entry if entry is not None and entry["outcome"] == "readable" else None

    def record_denied(self, table: str) -> None:
        self._put(self._table_key(table), {"outcome": "denied"})

    def record_readable(self, table: str, scan_bytes: int | None = None) -> None:
        known = self.readable(table)
        if scan_bytes is None and known is not None:
            scan_bytes = known.get("bytes")
        self._put(self._table_key(table), {"outcome": "readable", **({"bytes": scan_bytes} if scan_bytes is not None else {})})

    # --------------------------------------------------------------- estimates

    def estimate(self, project: str, sql: str) -> dict | None:
        return self._get(self._estimate_key(project, sql))

    def record_estimate(self, project: str, sql: str, estimated_bytes: int, columns: list[str] | None) -> None:
        self._put(self._estimate_key(project, sql), {"bytes": estimated_bytes, **({"columns": columns} if columns else {})})

    # --------------------------------------------------------------------- age

    def age(self, entry: dict) -> str:
        seconds = max(0, int(self.clock() - entry["at"]))
        if seconds < 60:
            return "under 1 min ago"
        if seconds < 7200:
            return f"{seconds // 60} min ago"
        return f"{seconds // 3600} h ago"

    # -------------------------------------------------------------------- disk

    def _read_file(self) -> dict[str, dict]:
        """The entries in the file that are well formed and not expired; anything else is ignored."""

        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(stored, dict):
            return {}
        now = self.clock()
        kept = {}
        for key, entry in stored.items():
            if (isinstance(key, str) and isinstance(entry, dict) and isinstance(entry.get("at"), (int, float))
                    and not isinstance(entry.get("bytes", 0), bool) and isinstance(entry.get("bytes", 0), int)
                    and now - entry["at"] < self.ttl and entry.get("outcome", "estimate") in ("denied", "readable", "estimate")):
                kept[key] = {name: entry[name] for name in ("outcome", "bytes", "at") if name in entry}
        return kept

    def _stamp(self):
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def _merge_file(self) -> None:
        """Take in what another process wrote: for each key the newer entry wins."""

        self._seen = self._stamp()
        for key, entry in self._read_file().items():
            mine = self._entries.get(key)
            if mine is None or mine["at"] < entry["at"]:
                self._entries[key] = entry

    def _sync(self) -> None:
        if self.path is not None and self._stamp() != self._seen:
            self._merge_file()

    def _save(self) -> None:
        if self.path is None:
            return
        # Only the table name (inside the key), the outcome, a byte count and a time are written: no columns, no SQL, no error text.
        # Merge first so a second server process does not lose what the first learned, then replace the file in one step.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._merge_file()
            kept = {key: {name: entry[name] for name in ("outcome", "bytes", "at") if name in entry} for key, entry in self._entries.items()}
            handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".access-cache-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    json.dump(kept, out)
                os.replace(temp, self.path)
            except OSError:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
            self._seen = self._stamp()
        except OSError:
            pass  # the cache is an optimization; a failed save never fails an answer


def cache_for(session) -> AccessCache:
    """The one cache of this server session (tests replace its clock and identity)."""

    return session._once("cost_access", lambda: AccessCache.from_environment(getattr(session, "root", None)))


# --------------------------------------------------------------------- the tables


def source_tables(draft) -> list[str]:
    """The tables the draft reads that BigQuery can be asked about: ``project.dataset.table``, from the project or spelled in full."""

    found: list[str] = []
    for spelled, key in draft.tables.items():
        table = key or (spelled if len(spelled.split(".")) == 3 else None)
        if table and table not in found:
            found.append(table)
    return found


_TABLE_IN_ERROR = re.compile(r"Table\s+`?([A-Za-z0-9_\-.:$]+)")


def denied_table(result) -> str | None:
    """The table an ``Access Denied`` dry-run error names (``project.dataset.table``), or ``None``: not a denial, or no table named."""

    message = result.error_message or ""
    if result.error_reason != "accessDenied" and not message.startswith("Access Denied"):
        return None
    found = _TABLE_IN_ERROR.search(message)
    if found is None:
        return None
    parts = found.group(1).rstrip(".:").replace(":", ".", 1).split(".")
    return ".".join(parts) if len(parts) == 3 and all(parts) else None


def _is_denied(result) -> bool:
    return result.error_reason == "accessDenied" or (result.error_message or "").startswith("Access Denied")


def _ask(sql: str, project: str):
    """``(result, None)``, or ``(None, why)`` when BigQuery could not be asked (no credentials, no network)."""

    from . import mcp_preflight

    try:
        return mcp_preflight.bigquery_dry_run(sql, project), None
    except Exception as exc:  # noqa: BLE001 - the answer says so instead of failing
        return None, clip(str(exc) or type(exc).__name__, 160)


def _select_star(table: str) -> str:
    from .sql_validation import quote_table_path

    return f"SELECT * FROM {quote_table_path(table)}"


def _is_plain_select_star(draft, table: str) -> bool:
    """Whether the draft is ``SELECT * FROM table`` and nothing else (its estimate is then the table's full-scan size)."""

    tree = draft.tree
    if not isinstance(tree, exp.Select):
        return False
    extra = {name for name, value in tree.args.items() if value and name not in ("expressions", "from", "from_")}
    columns = tree.args.get("expressions") or []
    source = (tree.args.get("from_") or tree.args.get("from"))
    if extra or len(columns) != 1 or not isinstance(columns[0], exp.Star) or columns[0].args.get("except_") or columns[0].args.get("replace"):
        return False
    return source is not None and isinstance(source.this, exp.Table) and len(draft.tables) == 1 and source_tables(draft) == [table]


# -------------------------------------------------------------------------- result


@dataclass
class Assessment:
    findings: list[dict] = field(default_factory=list)
    gaps: list[tuple[str, str]] = field(default_factory=list)
    #: summary of the dry run (``ok``, ``estimated_bytes``, ``byte_cap``, ``columns``, ``cached``), or ``None`` when it did not plan the query
    dry: dict | None = None
    sources: list[dict] = field(default_factory=list)


def _denied_finding(table: str, cache: AccessCache, entry: dict | None) -> dict:
    if entry is not None:
        what = f"{table} is not readable with the current account (cached, {cache.age(entry)}; recheck with refresh=true)"
    else:
        what = f"{table} is not readable with the current account (BigQuery: Access Denied, or the table does not exist in that location)"
    return {
        "check": "access",
        "what": clip(what, 200),
        "fix": "Ask the table's owner for read access, or use a table this account can read; once access is granted, ask again with dry_run=true and refresh=true.",
    }


def _source_line(table: str, cache: AccessCache) -> dict:
    denied = cache.denial(table)
    if denied is not None:
        return {"table": table, "access": "denied", "note": f"cached, {cache.age(denied)}"}
    known = cache.readable(table)
    if known is not None:
        line = {"table": table, "access": "readable"}
        if known.get("bytes") is not None:
            line["scan_bytes"] = known["bytes"]
        return line
    return {"table": table, "access": "unknown"}


def _over_cap_finding(draft, estimated: int, cap: int) -> dict:
    starred = any(not isinstance(star.parent, exp.Count) for star in draft.tree.find_all(exp.Star))
    first = "Name the columns you need instead of *" if starred else "Drop the columns you do not use"
    return {
        "check": "dry_run",
        "what": f"the query would process about {estimated:,} bytes ({human_bytes(estimated)}), over the {cap:,}-byte cap ({human_bytes(cap)})",
        "fix": clip(f"{first} (BigQuery bills the columns it reads), then filter on the table's partition column. LIMIT does not cut bytes. "
                    "The cap is the budget owner's call: do not raise it to get past this.", 230),
    }


# --------------------------------------------------------------------------- assess


def assess(session, draft, sql: str, project: str | None, refresh: bool = False) -> Assessment:
    """The cost and access part of ``check_query``. ``project`` is ``None`` when no dry run was asked for."""

    from . import scope_queries
    from .sql_validation import validate_readonly_query

    cache = cache_for(session)
    out = Assessment()
    sources = source_tables(draft)
    denied = [] if refresh else [(table, entry) for table in sources if (entry := cache.denial(table)) is not None]
    out.findings += [_denied_finding(table, cache, entry) for table, entry in denied]

    if project is None:
        out.sources = [_source_line(table, cache) for table in sources if cache.denial(table) or cache.readable(table)] if not refresh else []
        out.gaps.append(("estimated bytes and source access", NO_CONNECTION))
        return out
    if denied:
        out.sources = [_source_line(table, cache) for table in sources]
        out.gaps.append(("estimated bytes", "not checked: BigQuery was not asked because a source is cached as not readable; refresh=true asks again"))
        return out

    try:
        validate_readonly_query(sql)
    except ValueError as exc:
        out.gaps.append(("BigQuery dry run", f"not checked: {clip(exc, 120)}"))
        return out

    cap = scope_queries.get_settings().max_bytes_billed
    cached = None if refresh else cache.estimate(project, sql)
    if cached is not None:
        out.dry = {"ok": True, "estimated_bytes": cached["bytes"], "byte_cap": cap, "columns": cached.get("columns"), "cached": cache.age(cached)}
        out.sources = [_source_line(table, cache) | ({"access": "readable"} if not cache.readable(table) else {}) for table in sources]
    else:
        result, error = _ask(sql, project)
        if result is None:
            out.gaps.append(("BigQuery dry run", f"not checked: {error}"))
            return out
        if result.ok:
            columns = [clip(f"{f.name} {f.type}", 90) for f in result.schema] if result.schema_observed else None
            out.dry = {"ok": True, "estimated_bytes": result.total_bytes_processed, "byte_cap": cap, "columns": columns}
            if result.total_bytes_processed is not None:
                cache.record_estimate(project, sql, result.total_bytes_processed, columns)
            for table in sources:  # a query that plans reads only tables the account can read
                if len(sources) == 1 and _is_plain_select_star(draft, table):
                    cache.record_readable(table, result.total_bytes_processed)
                else:
                    cache.record_readable(table)
            out.sources = _probe_sizes(cache, sources, project)
        else:
            return _rejected(cache, draft, sources, result, project, out)
    estimated = out.dry["estimated_bytes"]
    if estimated is not None and estimated > cap:
        out.findings.append(_over_cap_finding(draft, estimated, cap))
    if estimated is None:
        out.gaps.append(("estimated bytes", "BigQuery returned no estimate"))
    return out


def _probe_sizes(cache: AccessCache, sources: list[str], project: str) -> list[dict]:
    """After a query planned: learn the full-scan size of up to ``MAX_PROBES`` sources not known yet, each with one free dry run."""

    probes = 0
    for table in sources:
        known = cache.readable(table)
        if known is not None and known.get("bytes") is not None:
            continue
        if probes >= MAX_PROBES:
            break
        try:
            sql = _select_star(table)
        except ValueError:
            continue
        probes += 1
        result, _ = _ask(sql, project)
        if result is None:
            break
        if result.ok and result.total_bytes_processed is not None:
            cache.record_readable(table, result.total_bytes_processed)
    return [_source_line(table, cache) for table in sources]


def _rejected(cache: AccessCache, draft, sources: list[str], result, project: str, out: Assessment) -> Assessment:
    """BigQuery rejected the query: an access error names the unreadable table and the other sources are probed one by one."""

    named = denied_table(result)
    if named is None:
        where = f" (at {result.error_location})" if result.error_location else ""
        out.findings.append({"check": "dry_run", "what": clip(f"BigQuery rejects the query{where}: {result.error_message}", 260),
                             "fix": "Fix what BigQuery reports; check_query without dry_run shows type problems it can see offline."})
        out.dry = {"ok": False}
        return out
    cache.record_denied(named)
    unreadable = [named]
    probes = 0
    for table in sources:
        if table.casefold() == named.casefold() or cache.denial(table) is not None:
            continue
        if probes >= MAX_PROBES:
            break
        try:
            sql = _select_star(table)
        except ValueError:
            continue
        probes += 1
        probed, _ = _ask(sql, project)
        if probed is None:
            break
        if probed.ok:
            cache.record_readable(table, probed.total_bytes_processed)
        elif _is_denied(probed):
            cache.record_denied(table)
    for table in sources:
        if cache.denial(table) is not None and table not in unreadable:
            unreadable.append(table)
    out.findings += [_denied_finding(table, cache, None) for table in unreadable]
    out.sources = [_source_line(table, cache) for table in sources]
    out.dry = {"ok": False}
    return out


# ------------------------------------------------------------------- describe_table


def describe_fields(session, key: str, answer: Answer) -> None:
    """What ``describe_table`` can say about a table's cost and access without asking BigQuery."""

    cache = cache_for(session)
    denied = cache.denial(key)
    if denied is not None:
        answer.fields["access"] = f"not readable with the current account (cached, {cache.age(denied)}; recheck with check_query dry_run=true refresh=true)"
    known = cache.readable(key)
    if known is not None and known.get("bytes") is not None:
        answer.fields["full_scan"] = f"{known['bytes']:,} bytes ({human_bytes(known['bytes'])}), from a dry run, cached {cache.age(known)}"
        return
    answer.unknown("full-scan size", "unknown until a dry run has been made")
    try:
        sql = _select_star(key)
    except ValueError:
        return
    answer.next(f'check_query dry_run=true project=<billing project> sql="{sql}" makes the dry run (free).')
