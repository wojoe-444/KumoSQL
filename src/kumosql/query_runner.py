"""``python -m kumosql q run FILE|-``: run one read-only query and behave the same in PowerShell, cmd and bash.

The stock warehouse CLI trips agents on Windows: multi-line SQL passed as an argument is cut to its first line, a
byte-order mark fails the query, progress banners mix with the results, PowerShell turns stderr into errors, and every
query takes two calls (write the file, run it). This command avoids each of them:

* the SQL comes from a file or from stdin (``-``), never from an argument, so its lines arrive whole; a UTF-8 or UTF-16
  byte-order mark is removed and Windows line ends become ``\\n`` (so the text BigQuery caches on is the same everywhere);
* stdout carries exactly one line: a JSON summary in ASCII, fixed in shape (status, rows, bytes, job id, output path,
  truncation), on success and on failure alike. Nothing prints progress. On failure stderr gets one short line too, but
  the outcome is always readable from the exit code and the stdout summary alone;
* the full result goes to a JSON or CSV file (``-o``; without it, a new file in the temp folder named in the summary);
* every kind of failure has its own exit code (:data:`EXIT_CODES`).

The query must be exactly one read-only ``SELECT`` (``WITH``, ``UNION``, subqueries allowed): KumoSQL only reads, so a
write, DDL, a script or several statements are refused with the SQL-error code before anything reaches the warehouse.
Before the job, a local pre-flight (sqlglot) checks for BigQuery mistakes such as a reserved word used as an alias
(:mod:`kumosql.bq_static_checks`) and, with ``--schema-snapshot``, unknown columns (:mod:`kumosql.googlesql_types`).
On BigQuery the free dry run comes next (:mod:`kumosql.dryrun`): it resolves every table and column and estimates the
bytes; an estimate over the cap is refused, and the real run carries ``maximumBytesBilled`` as the other BigQuery
features do (:mod:`kumosql.scope_queries`). Credentials are found the way every KumoSQL BigQuery feature finds them
(:func:`kumosql.dryrun.access_token`); nothing here asks for, prints or stores them.

``--duckdb PATH`` runs the same path on a local DuckDB file (opened read-only), with the BigQuery SQL translated by
sqlglot, so it can be tested with real execution and no network.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import csv
import hashlib
import http.client
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote, urlencode

from .resilience import replace_file

#: exit codes, also printed by ``--help`` and listed in docs/query-runner.md
EXIT_OK = 0
EXIT_OTHER = 1
EXIT_SQL = 2
EXIT_AUTH = 3
EXIT_NETWORK = 4
EXIT_BYTE_CAP = 5
EXIT_USAGE = 64
EXIT_CODES = {
    EXIT_OK: "success (a --dry-run that planned is a success too)",
    EXIT_OTHER: "other: the results file could not be written, or an unexpected error",
    EXIT_SQL: "SQL error: it does not parse, is not one read-only SELECT, a pre-flight check found an error, "
              "or BigQuery (or DuckDB) rejected it",
    EXIT_AUTH: "auth: no credentials, credentials refused, or access denied",
    EXIT_NETWORK: "network: BigQuery could not be reached, timed out, or is busy (HTTP 5xx, rate or quota limits); "
                  "the same query may work later",
    EXIT_BYTE_CAP: "byte cap: the dry-run estimate is over --max-bytes, or BigQuery refused the run under maximumBytesBilled",
    EXIT_USAGE: "usage: a bad option, a missing or unreadable SQL file, or no billing project",
}

#: BigQuery accepts GoogleSQL query text up to 1,024 KB
MAX_SQL_CHARS = 1_000_000
DEFAULT_MAX_ROWS = 100_000
MAX_ROWS_LIMIT = 1_000_000
DEFAULT_HEAD = 5
MAX_HEAD = 50
DEFAULT_SUMMARY_BYTES = 4_000
MIN_SUMMARY_BYTES = 1_000
MAX_SUMMARY_BYTES = 20_000
DEFAULT_TIMEOUT = 600.0
PAGE_ROWS = 10_000
CELL_CHARS = 80
MESSAGE_CHARS = 300
LIST_ITEMS = 5

#: static rules whose findings BigQuery rejects: they stop the run; the others are reported as warnings
BLOCKING_STATIC = frozenset({"reserved_alias", "subquery"})
#: type-checker findings that certainly fail in BigQuery: they stop the run
BLOCKING_TYPES = frozenset({"unknown_column", "ambiguous_column", "invalid_field_access", "star_except_missing", "set_operation_width"})
#: BigQuery pseudo-columns: never listed in INFORMATION_SCHEMA.COLUMNS, so a snapshot cannot vouch for them
PSEUDO_COLUMNS = frozenset({"_PARTITIONTIME", "_PARTITIONDATE", "_TABLE_SUFFIX", "_FILE_NAME", "_CHANGE_TYPE",
                            "_CHANGE_TIMESTAMP", "_CHANGE_SEQUENCE_NUMBER"})
#: what BigQuery's ``totalBytesProcessedAccuracy`` says about a dry-run estimate, in this command's words
ACCURACY = {"PRECISE": "exact", "UPPER_BOUND": "upper_bound", "LOWER_BOUND": "lower_bound", "UNKNOWN": "unknown"}

_AUTH_REASONS = frozenset({"autherror", "unauthenticated", "unauthorized", "accessdenied", "billingnotenabled", "invaliduser",
                           "termsofservicenotaccepted", "forbidden", "permission_denied", "blocked", "policyviolation"})
_NETWORK_REASONS = frozenset({"backenderror", "internalerror", "ratelimitexceeded", "quotaexceeded", "jobratelimitexceeded",
                              "apilimitexceeded", "jobbackenderror", "jobinternalerror", "tableunavailable", "unavailable",
                              "internal", "resource_exhausted", "deadline_exceeded"})
_CAP_REASONS = frozenset({"bytesbilledlimitexceeded"})


# --------------------------------------------------------------------- errors


class QueryRunError(Exception):
    """A run that cannot go on; ``kind`` and ``exit_code`` say which class of failure it is."""

    kind = "other"
    exit_code = EXIT_OTHER

    def __init__(self, message: str) -> None:
        super().__init__(_clip(" ".join(str(message).split()), MESSAGE_CHARS))


class SqlError(QueryRunError):
    kind, exit_code = "sql", EXIT_SQL


class AuthError(QueryRunError):
    kind, exit_code = "auth", EXIT_AUTH


class NetworkError(QueryRunError):
    kind, exit_code = "network", EXIT_NETWORK


class ByteCapError(QueryRunError):
    kind, exit_code = "byte_cap", EXIT_BYTE_CAP


class UsageError(QueryRunError):
    kind, exit_code = "usage", EXIT_USAGE


class OutputError(QueryRunError):
    kind, exit_code = "output", EXIT_OTHER


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def classify_api_error(status: int | None, reason: str | None, message: str | None) -> QueryRunError:
    """The class of a BigQuery API refusal, from its HTTP status and error reason (see the BigQuery error table)."""

    folded = (reason or "").casefold()
    text = message or (f"BigQuery returned HTTP {status}" if status else "BigQuery refused the request")
    if folded in _CAP_REASONS:
        return ByteCapError(f"BigQuery refused to run it under the byte cap: {text}")
    if status == 401 or folded in _AUTH_REASONS:
        return AuthError(text)
    if folded in _NETWORK_REASONS or (status is not None and (status >= 500 or status in (408, 429))):
        return NetworkError(text)
    if status == 403 and folded != "responsetoolarge":
        return AuthError(text)
    return SqlError(text)


def _api_failure(status: int, payload: object) -> QueryRunError:
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {"message": str(error)}
    first = (error.get("errors") or [{}])[0] if isinstance(error.get("errors"), list) else {}
    return classify_api_error(status, first.get("reason") or error.get("status"), error.get("message") or first.get("message"))


# ------------------------------------------------------------------- the SQL


def decode_sql(data: bytes) -> str:
    """The SQL text of a file or of stdin: a UTF-8 or UTF-16 byte-order mark removed, ``\\r\\n`` read as ``\\n``."""

    try:
        if data.startswith(codecs.BOM_UTF8):
            text = data[len(codecs.BOM_UTF8):].decode("utf-8")
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # what Windows PowerShell 5.1 writes by default
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError("the SQL is not valid UTF-8 or UTF-16 text; save the file as UTF-8") from None
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def read_sql(source: str, stdin: io.BufferedIOBase | None) -> str:
    if source == "-":
        if stdin is None:
            raise UsageError("- reads the SQL from stdin, but there is no stdin")
        if getattr(stdin, "isatty", lambda: False)():
            raise UsageError("- reads the SQL from stdin, but nothing was piped in; pipe the SQL or give a file")
        data = stdin.read()
    else:
        path = Path(source)
        if not path.is_file():
            hint = " (the SQL goes in a file or on stdin, not in an argument)" if re.search(r"\s", source) else ""
            raise UsageError(f"no SQL file {source!r}{hint}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise UsageError(f"could not read the SQL file: {exc.strerror or exc}") from None
    text = decode_sql(data)
    if not text:
        raise UsageError("the SQL is empty")
    if len(text) > MAX_SQL_CHARS:
        raise SqlError(f"the query is longer than {MAX_SQL_CHARS:,} characters, the BigQuery limit")
    return text


# ----------------------------------------------------------------- pre-flight


@dataclass
class Preflight:
    tree: object | None = None
    checked: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_error_text(exc: BaseException) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).splitlines()
    return _clip(text[0] if text else type(exc).__name__, 160)


def _tables(tree) -> list[str]:
    """The tables the query reads (not its CTEs), as spelled, lower case."""

    from sqlglot import exp

    ctes = {cte.alias.casefold() for cte in tree.find_all(exp.CTE)}
    names: list[str] = []
    for table in tree.find_all(exp.Table):
        if not table.name or (not table.args.get("db") and table.name.casefold() in ctes):
            continue
        spelled = ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name)
        if spelled.casefold() not in names:
            names.append(spelled.casefold())
    return names


def preflight(sql: str, *, static: bool = True, snapshot: Path | None = None, need_tree: bool = False,
              result: Preflight | None = None) -> Preflight:
    """Local checks before anything reaches the warehouse. Raises :class:`SqlError` for a query that cannot run.

    A query sqlglot cannot parse is refused when ``need_tree`` (DuckDB has to translate it); on BigQuery the dry run then
    decides whether it is one read-only SELECT (``statementType``) and the local checks are listed as not checked.
    Pass ``result`` to see the checks that had passed when one refuses the query."""

    from sqlglot import exp

    from kumo_sql_prover.ast_utils import parse_statements

    from .sql_validation import validate_readonly_query

    result = result if result is not None else Preflight()
    try:
        statements = [s for s in parse_statements(sql) if not isinstance(s, exp.Semicolon)]
    except Exception as exc:  # noqa: BLE001 - sqlglot's errors vary; the first line says where
        if need_tree:
            raise SqlError(f"the SQL does not parse: {_parse_error_text(exc)}") from None
        result.not_checked.append(f"local checks: sqlglot could not parse the SQL ({_parse_error_text(exc)}); "
                                  "BigQuery's dry run decides whether it is one read-only SELECT")
        return result
    if len(statements) != 1:
        raise SqlError(f"the SQL holds {len(statements)} statements; q run runs exactly one read-only SELECT")
    try:
        validate_readonly_query(sql, max_chars=MAX_SQL_CHARS)
    except ValueError:
        kind = type(statements[0]).__name__.upper()
        raise SqlError(f"refused: KumoSQL only reads, and this is not one read-only SELECT ({kind}); "
                       "writes, DDL and scripts are out of scope") from None
    result.tree = statements[0]
    result.checked.append("read_only")
    if static:
        from .bq_static_checks import static_checks

        found, gaps = static_checks(None, result.tree, sql, {name: None for name in _tables(result.tree)})
        for item in found:
            if item["check"] in BLOCKING_STATIC:
                raise SqlError(f"{item['what']}. Fix: {item['fix']}")
            result.warnings.append(_clip(f"{item['check']}: {item['what']}", 200))
        result.not_checked += [_clip(f"{what}: {why}", 200) for what, why in gaps]
        result.checked.append("static")
    else:
        result.not_checked.append("static checks: skipped (--no-preflight)")
    if snapshot is not None:
        _snapshot_check(result, snapshot)
    return result


def _snapshot_check(result: Preflight, path: Path) -> None:
    from .googlesql_types import Catalog, Column, infer, parse_type
    from .schema_snapshot import SchemaSnapshotError, load_schema_snapshot

    wanted = {name.split(".")[-1] for name in _tables(result.tree)}
    try:
        snapshot = load_schema_snapshot(path, keep=lambda catalog, schema, table: table.casefold() in wanted)
    except (SchemaSnapshotError, OSError) as exc:
        raise UsageError(f"--schema-snapshot: {exc}") from None
    catalog = Catalog()
    for name, columns in snapshot.tables().items():
        catalog.add(name, [Column(c.column, parse_type(c.data_type)) for c in columns])
    missing = [name for name in _tables(result.tree) if snapshot.columns_of(name) is None]
    typed = infer(result.tree, catalog)
    for finding in typed.findings:
        name = str(getattr(finding.node, "name", "") or "")
        if finding.code == "unknown_column" and name.upper() in PSEUDO_COLUMNS:
            result.not_checked.append(f"pseudo-column {name}: a schema snapshot never lists it")
        elif finding.code in BLOCKING_TYPES:
            raise SqlError(f"{finding.message} (schema snapshot)")
        else:
            result.warnings.append(_clip(f"types: {finding.message}", 200))
    if typed.error:
        result.not_checked.append(_clip(f"column names: the type checker could not read the query ({typed.error})", 200))
    if missing:
        shown = ", ".join(missing[:3]) + (", ..." if len(missing) > 3 else "")
        result.not_checked.append(_clip(f"columns of {len(missing)} table(s) not in the schema snapshot: {shown}", 200))
    result.checked.append("schema_snapshot")


# --------------------------------------------------------------------- results


@dataclass
class Plan:
    """What a dry run learned. ``bytes_accuracy`` is one of :data:`ACCURACY`'s values, or ``None`` without an estimate."""

    columns: list[tuple[str, str]]
    estimated_bytes: int | None = None
    bytes_accuracy: str | None = None
    statement_type: str | None = None
    check: str = "dry_run"


@dataclass
class Result:
    columns: list[tuple[str, str]]
    rows: list[list]
    total_rows: int | None
    truncated: bool
    bytes_processed: int | None = None
    bytes_billed: int | None = None
    job_id: str | None = None
    cache_hit: bool | None = None


# ---------------------------------------------------------------- BigQuery


Post = Callable[[str, dict, bytes], tuple[int, dict]]
Get = Callable[[str, dict], tuple[int, dict]]


def _bq_type(raw: dict) -> str:
    kind = str(raw.get("type") or "")
    kind = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL", "RECORD": "STRUCT"}.get(kind, kind)
    return f"ARRAY<{kind}>" if str(raw.get("mode") or "").upper() == "REPEATED" else kind


def _timestamp(value: str) -> str:
    try:
        if re.fullmatch(r"-?\d+", value):  # formatOptions.useInt64Timestamp: microseconds since the epoch
            moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(value))
        else:  # the older form: seconds, as a floating-point number
            moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(Decimal(value) * 1_000_000))
    except (ArithmeticError, ValueError, OverflowError):
        return value
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def bq_value(schema_field: dict, value: object) -> object:
    """A cell of a BigQuery REST response as a JSON value: INT64 a number, BOOL true/false, TIMESTAMP ISO 8601 UTC,
    STRUCT an object, ARRAY a list. NUMERIC, BIGNUMERIC, DATE, DATETIME, TIME, BYTES (base64) and the rest stay text,
    as BigQuery sends them, so no precision is lost; a non-finite FLOAT64 stays ``"NaN"`` or ``"Infinity"``."""

    if value is None:
        return None
    if str(schema_field.get("mode") or "").upper() == "REPEATED":
        element = {**schema_field, "mode": "NULLABLE"}
        return [bq_value(element, item.get("v") if isinstance(item, dict) else item) for item in (value if isinstance(value, list) else [])]
    kind = str(schema_field.get("type") or "").upper()
    if kind in ("RECORD", "STRUCT"):
        cells = value.get("f", []) if isinstance(value, dict) else []
        subs = schema_field.get("fields") or []
        return {str(sub.get("name") or f"f{i}"): bq_value(sub, cells[i].get("v") if i < len(cells) and isinstance(cells[i], dict) else None)
                for i, sub in enumerate(subs)}
    if not isinstance(value, str):
        return value
    if kind in ("INTEGER", "INT64"):
        try:
            return int(value)
        except ValueError:
            return value
    if kind in ("FLOAT", "FLOAT64"):
        try:
            number = float(value)
        except ValueError:
            return value
        return number if math.isfinite(number) else value
    if kind in ("BOOLEAN", "BOOL"):
        return value.casefold() == "true"
    if kind == "TIMESTAMP":
        return _timestamp(value)
    return value


def _number_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class BigQueryRunner:
    """Dry run and run in the billing ``project`` under the byte cap. ``post``, ``get`` and ``token`` default to the
    transports and credentials of the other BigQuery features; tests pass fakes, so nothing here needs the network."""

    backend = "bigquery"

    def __init__(self, project: str, *, location: str | None, max_bytes: int, timeout: float = DEFAULT_TIMEOUT,
                 post: Post | None = None, get: Get | None = None, token: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        from . import scope_queries

        self.project, self.location, self.max_bytes, self.timeout = project, location or None, max_bytes, timeout
        self._post, self._get = post or scope_queries._post, get or scope_queries._get
        self._token_source, self._token = token, None
        self._clock = clock
        self._api = scope_queries._API

    def _bearer(self) -> str:
        if self._token is None:
            if self._token_source is not None:
                self._token = self._token_source()
            else:
                from .dryrun import access_token

                try:
                    with warnings.catch_warnings():  # google-auth warns on stderr about quota projects
                        warnings.simplefilter("ignore")
                        self._token = access_token()
                except RuntimeError as exc:
                    raise AuthError(str(exc)) from None
        return self._token

    def _call(self, send: Callable[[], tuple[int, dict]]) -> dict:
        try:
            status, payload = send()
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise NetworkError(f"could not reach BigQuery: {getattr(exc, 'reason', None) or exc}") from None
        if status >= 400 or (isinstance(payload, dict) and "error" in payload):
            raise _api_failure(status, payload)
        return payload if isinstance(payload, dict) else {}

    def plan(self, sql: str) -> Plan:
        from . import dryrun

        token = self._bearer()
        try:
            result = dryrun.dry_run(sql, self.project, location=self.location, token=token,
                                    transport=lambda url, headers, body: self._post(url, headers, body))
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise NetworkError(f"could not reach BigQuery: {getattr(exc, 'reason', None) or exc}") from None
        if not result.ok:
            raise classify_api_error(result.http_status, result.error_reason, result.error_message)
        accuracy = ACCURACY.get(str(result.bytes_accuracy or "").upper(), "unknown") if result.total_bytes_processed is not None else None
        columns = [(f.name, f"ARRAY<{f.type}>" if f.mode == "REPEATED" else f.type) for f in result.schema]
        return Plan(columns, result.total_bytes_processed, accuracy, result.statement_type)

    def run(self, sql: str, max_rows: int) -> Result:
        headers = {"Authorization": f"Bearer {self._bearer()}", "Content-Type": "application/json"}
        deadline = self._clock() + self.timeout
        wait_ms = lambda: str(int(max(1.0, min(10.0, deadline - self._clock())) * 1000))  # noqa: E731
        body = {
            "query": sql, "useLegacySql": False, "useQueryCache": True, "maximumBytesBilled": str(self.max_bytes),
            "maxResults": min(PAGE_ROWS, max_rows), "timeoutMs": int(wait_ms()), "labels": {"kumosql": "q_run"},
            "formatOptions": {"useInt64Timestamp": True},
        }
        if self.location:
            body["location"] = self.location
        base = f"{self._api}/projects/{quote(self.project, safe='')}"
        payload = self._call(lambda: self._post(f"{base}/queries", headers, json.dumps(body).encode("utf-8")))
        job = payload.get("jobReference") or {}
        job_id = job.get("jobId") or None
        where = {"location": job["location"]} if job.get("location") else ({"location": self.location} if self.location else {})

        def fetch(params: dict) -> dict:
            if not job_id:
                raise NetworkError("BigQuery returned no job id to read the results from")
            query = urlencode({**where, "formatOptions.useInt64Timestamp": "true", **params})
            return self._call(lambda: self._get(f"{base}/queries/{quote(job_id, safe='')}?{query}", headers))

        while not payload.get("jobComplete", True):
            if self._clock() > deadline:
                raise NetworkError(f"the query did not finish within {self.timeout:g} seconds; "
                                   f"it may still be running in BigQuery as job {job_id}")
            payload = fetch({"timeoutMs": wait_ms(), "maxResults": str(min(PAGE_ROWS, max_rows))})
        fields = (payload.get("schema") or {}).get("fields") or []
        columns = [(str(f.get("name", "")), _bq_type(f)) for f in fields]
        total = _number_or_none(payload.get("totalRows"))
        processed = _number_or_none(payload.get("totalBytesProcessed"))
        billed = _number_or_none(payload.get("totalBytesBilled"))
        cache_hit = payload.get("cacheHit")
        rows: list[list] = []
        while True:
            for row in payload.get("rows") or []:
                if len(rows) >= max_rows:
                    break
                cells = row.get("f", []) if isinstance(row, dict) else []
                rows.append([bq_value(f, cells[i].get("v") if i < len(cells) and isinstance(cells[i], dict) else None)
                             for i, f in enumerate(fields)])
            token = payload.get("pageToken")
            if not token or len(rows) >= max_rows:
                break
            payload = fetch({"pageToken": token, "maxResults": str(min(PAGE_ROWS, max_rows - len(rows)))})
        truncated = (total is not None and total > len(rows)) or (total is None and bool(payload.get("pageToken")))
        return Result(columns, rows, total, truncated, processed, billed, job_id,
                      bool(cache_hit) if cache_hit is not None else None)


# ------------------------------------------------------------------ DuckDB


def duck_value(value: object) -> object:
    """A DuckDB value as a JSON value, written the way the BigQuery backend writes the same type."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return value.isoformat()
    if isinstance(value, (date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [duck_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): duck_value(item) for key, item in value.items()}
    return str(value)


class DuckDBRunner:
    """Run the BigQuery SQL, translated by sqlglot, on a DuckDB database file opened read-only (``:memory:`` for none).

    Results can differ from BigQuery's where the two engines differ; the backend is for local copies and tests."""

    backend = "duckdb"
    max_bytes = None

    def __init__(self, path: str) -> None:
        try:
            import duckdb
        except ImportError:
            raise UsageError("--duckdb needs the duckdb package: pip install 'kumosql[execution]'") from None
        self._duckdb = duckdb
        if path != ":memory:" and not Path(path).is_file():
            raise UsageError(f"--duckdb: {path} is not a file")
        try:
            self.connection = duckdb.connect() if path == ":memory:" else duckdb.connect(path, read_only=True)
        except duckdb.Error as exc:
            raise UsageError(f"--duckdb: could not open the database: {str(exc).splitlines()[0]}") from None
        try:
            self.connection.execute("SET TimeZone = 'UTC'")
        except duckdb.Error:
            pass  # without the ICU extension there is no time zone setting, and no TIMESTAMPTZ to show

    @staticmethod
    def translate(tree) -> str:
        return tree.sql(dialect="duckdb")

    def _sql_error(self, exc: BaseException) -> SqlError:
        lines = str(exc).splitlines()
        return SqlError(f"DuckDB rejected the query: {lines[0] if lines else type(exc).__name__}")

    def plan(self, sql: str) -> Plan:
        try:
            cursor = self.connection.execute(f"DESCRIBE {sql}")
            columns = [(str(row[0]), str(row[1])) for row in cursor.fetchall()]
        except self._duckdb.Error as exc:
            raise self._sql_error(exc) from None
        return Plan(columns, None, None, "SELECT", check="duckdb_bind")

    def run(self, sql: str, max_rows: int) -> Result:
        try:
            cursor = self.connection.execute(sql)
            columns = [(str(item[0]), str(item[1])) for item in cursor.description or []]
            raw = cursor.fetchmany(max_rows)
            extra = 0
            while chunk := cursor.fetchmany(PAGE_ROWS):  # count the rest, cheaply and locally, to say how many there are
                extra += len(chunk)
        except self._duckdb.Error as exc:
            raise self._sql_error(exc) from None
        rows = [[duck_value(value) for value in row] for row in raw]
        return Result(columns, rows, len(rows) + extra, extra > 0)


# --------------------------------------------------------------- writing


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def write_results(path: Path, fmt: str, result: Result, meta: dict) -> None:
    """Write the result to ``path`` (JSON: one object, a row per line; CSV: a header row), replacing it atomically."""

    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="") as out:
            if fmt == "csv":
                writer = csv.writer(out)
                writer.writerow([name for name, _ in result.columns])
                for row in result.rows:
                    writer.writerow([_csv_cell(value) for value in row])
            else:
                head = {"columns": [{"name": n, "type": t} for n, t in result.columns], **meta}
                out.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "rows": [')
                for number, row in enumerate(result.rows):
                    out.write(("\n" if number == 0 else ",\n") + json.dumps(row, ensure_ascii=False, allow_nan=False))
                out.write("\n]}\n")
        replace_file(temp, path)
    except (OSError, ValueError, TypeError) as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise OutputError(f"could not write the results file: {getattr(exc, 'strerror', None) or exc}") from None


def default_output(sql: str, fmt: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / "kumosql-results" / f"q-{stamp}-{os.getpid()}-{digest}.{fmt}"


# ------------------------------------------------------------- summarizing


def _key(value: object) -> object:
    return value if isinstance(value, (str, int, float, bool)) else json.dumps(value, sort_keys=True)


def column_stats(name: str, type_name: str, values: Sequence[object]) -> dict:
    """Compact statistics of one result column over the rows in the file (the shape of :mod:`kumosql.data_profile`)."""

    from .data_profile import _number, classify

    kind = classify(type_name)
    present = [v for v in values if v is not None]
    entry: dict = {"name": name, "type": type_name, "nulls": len(values) - len(present)}
    if kind == "other" or not present:
        return entry
    entry["distinct"] = len({_key(v) for v in present})
    if kind == "numeric":
        numbers = [n for n in (_number(v) for v in present) if n is not None]
        if numbers:
            entry.update(min=min(numbers), max=max(numbers), mean=round(sum(numbers) / len(numbers), 6))
    elif kind == "temporal":
        texts = [str(v) for v in present]
        entry.update(min=min(texts), max=max(texts))
    elif kind == "string":
        lengths = [len(str(v)) for v in present]
        entry.update(min_length=min(lengths), max_length=max(lengths))
    if kind in ("string", "boolean") and entry["distinct"] < len(present):
        counts = Counter(_key(v) for v in present)
        top = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[:3]
        entry["top"] = [{"value": _cell(value), "count": count} for value, count in top]
    return entry


def _cell(value: object) -> object:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        return _clip(value, CELL_CHARS)
    return value


def dump(summary: dict) -> str:
    """The stdout line: ASCII JSON, so it reads the same in every console code page."""

    return json.dumps(summary, ensure_ascii=True, allow_nan=False)


def fit(summary: dict, cap: int) -> str:
    """``summary`` as one line of at most ``cap`` bytes: head rows go first, then top values, then column statistics,
    then the lists of warnings. What was left out is counted in ``summary_omitted``."""

    text = dump(summary)
    if len(text) <= cap:
        return text
    summary["summary_truncated"] = True
    omitted = summary.setdefault("summary_omitted", {"head_rows": 0, "columns": 0, "notes": 0})
    head = summary.get("head")
    while head and head["rows"] and len(dump(summary)) > cap:
        head["rows"].pop()
        omitted["head_rows"] += 1
    if head is not None and not head["rows"] and len(dump(summary)) > cap:
        summary["head"] = head = None  # column names without rows say nothing column_stats does not
    stats = summary.get("column_stats")
    if stats and len(dump(summary)) > cap:
        for entry in stats:
            entry.pop("top", None)
    while stats and len(dump(summary)) > cap:
        stats.pop()
        omitted["columns"] += 1
    if head is not None and len(dump(summary)) > cap:
        summary["head"] = None
    for key in ("warnings", "not_checked", "checked"):
        while summary.get(key) and len(dump(summary)) > cap:
            summary[key].pop()
            omitted["notes"] += 1
    if len(dump(summary)) > cap and summary.get("output"):
        summary["output"] = _clip(summary["output"], 200)
    return dump(summary)


# --------------------------------------------------------------------- CLI


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]i?B|B)?\s*$", re.I)
_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
          "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def parse_bytes(text: str) -> int:
    match = _SIZE.match(str(text))
    if not match:
        raise argparse.ArgumentTypeError(f"not a byte count: {text!r} (examples: 1000000000, 10GB, 1TiB)")
    return int(float(match.group(1)) * _UNITS[(match.group(2) or "B").upper()])


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse exits 2, which is the SQL-error code here
        raise UsageError(message)


def _int_in(low: int, high: int, what: str) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{what} must be a whole number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{what} must be from {low:,} to {high:,}")
        return value

    return parse


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--timeout must be a number of seconds") from None
    if not 0 < value <= 6 * 3600:
        raise argparse.ArgumentTypeError("--timeout must be above 0 and at most 21600 seconds (BigQuery's 6-hour limit)")
    return value


def _parser(prog: str) -> argparse.ArgumentParser:
    codes = "\n".join(f"  {code:<3} {text}" for code, text in EXIT_CODES.items())
    parser = _Parser(
        prog=prog, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run one read-only BigQuery query the same way in PowerShell, cmd and bash. READ-ONLY: anything "
                    "but one SELECT is refused.",
        epilog=f"exit codes:\n{codes}\n\nExample: python -m kumosql q run query.sql -o result.csv --project my-billing-project",
    )
    commands = parser.add_subparsers(dest="command", metavar="run", required=True)
    run = commands.add_parser(
        "run", formatter_class=argparse.RawDescriptionHelpFormatter,
        help="run a query from a file or stdin",
        description="Run one read-only SELECT from FILE, or from stdin with -. stdout gets one line of JSON (the summary) "
                    "and nothing else; the full result goes to the -o file. A UTF-8 or UTF-16 byte-order mark is removed. "
                    "Before any job: a read-only check and BigQuery-specific static checks (a reserved word as an alias, ...), "
                    "unknown columns with --schema-snapshot, then BigQuery's free dry run and the byte cap. READ-ONLY: writes, "
                    "DDL, scripts and several statements are refused.",
        epilog=f"exit codes:\n{codes}\n\n"
               "Examples:\n"
               "  python -m kumosql q run query.sql -o result.json --project my-billing-project\n"
               "  python -m kumosql q run - --dry-run < query.sql\n"
               "  python -m kumosql q run query.sql --summarize --duckdb local.duckdb",
    )
    run.add_argument("source", metavar="FILE|-", help="the SQL file, or - to read the SQL from stdin")
    run.add_argument("-o", "--output", type=Path, help="write the full result here (default: a new file in the temp folder; "
                     "the summary names it)")
    run.add_argument("--format", choices=("json", "csv"), help="results file format (default: csv for an -o ending in .csv, else json)")
    run.add_argument("--dry-run", action="store_true", help="plan only: report the bytes BigQuery estimates and whether the figure "
                     "is exact or an upper bound; nothing runs and no file is written")
    run.add_argument("--project", help="BigQuery billing project the job runs in (default: the one chosen in Settings)")
    run.add_argument("--location", help="BigQuery location such as US or EU (default: the one in Settings, else BigQuery's)")
    run.add_argument("--max-bytes", type=parse_bytes, help="byte cap: refuse a query whose dry-run estimate is larger, and pass it "
                     "as maximumBytesBilled (default: Settings, Scopes; 1 GiB). Accepts 10GB, 1TiB")
    run.add_argument("--max-rows", type=_int_in(1, MAX_ROWS_LIMIT, "--max-rows"), default=DEFAULT_MAX_ROWS,
                     help=f"rows written to the results file; more are counted, not fetched (default {DEFAULT_MAX_ROWS:,}, "
                          f"at most {MAX_ROWS_LIMIT:,})")
    run.add_argument("--timeout", type=_seconds, default=DEFAULT_TIMEOUT, help=f"seconds to wait for the job (default {DEFAULT_TIMEOUT:g})")
    run.add_argument("--duckdb", metavar="PATH", help="run on this DuckDB database file instead (opened read-only; :memory: for "
                     "none); the BigQuery SQL is translated by sqlglot")
    run.add_argument("--schema-snapshot", type=Path, metavar="FILE", help="column list in INFORMATION_SCHEMA.COLUMNS shape (CSV or "
                     "JSON lines): unknown and ambiguous columns are caught before any request")
    run.add_argument("--no-preflight", action="store_true", help="skip the static checks (the read-only check always runs)")
    run.add_argument("--summarize", action="store_true", help="add column statistics and the first rows to the summary, "
                     "within --summary-bytes")
    run.add_argument("--head", type=_int_in(0, MAX_HEAD, "--head"), default=DEFAULT_HEAD,
                     help=f"rows shown with --summarize (default {DEFAULT_HEAD}, at most {MAX_HEAD})")
    run.add_argument("--summary-bytes", type=_int_in(MIN_SUMMARY_BYTES, MAX_SUMMARY_BYTES, "--summary-bytes"),
                     default=DEFAULT_SUMMARY_BYTES, help=f"hard cap on the stdout summary (default {DEFAULT_SUMMARY_BYTES:,} bytes)")
    return parser


def _base_summary(backend: str | None) -> dict:
    return {
        "status": "error", "exit_code": EXIT_OTHER, "error": None, "backend": backend,
        "rows": None, "total_rows": None, "truncated": False, "columns": None,
        "bytes_estimated": None, "bytes_accuracy": None, "within_cap": None, "max_bytes_billed": None,
        "bytes_processed": None, "bytes_billed": None, "cache_hit": None, "job_id": None,
        "output": None, "format": None, "checked": [], "not_checked": [], "warnings": [],
    }


def _runner(args, bigquery: Callable[..., BigQueryRunner] | None):
    if args.duckdb:
        return DuckDBRunner(args.duckdb)
    from . import query_context, scope_queries

    project = (args.project or "").strip()
    if not project:
        try:
            project = scope_queries._need_project(None)
        except scope_queries.QueryError:
            raise UsageError("no billing project: pass --project ID (or choose one in Settings, BigQuery projects)") from None
    if args.max_bytes is None:
        cap = scope_queries.get_settings().max_bytes_billed
    elif not scope_queries.MIN_BYTES_BILLED <= args.max_bytes <= scope_queries.MAX_BYTES_BILLED:
        raise UsageError(f"--max-bytes must be from {scope_queries.MIN_BYTES_BILLED:,} (10 MB, BigQuery's minimum bill) "
                         f"to {scope_queries.MAX_BYTES_BILLED:,} (1 TiB)")
    else:
        cap = args.max_bytes
    location = args.location if args.location is not None else query_context.execution_location()
    return (bigquery or BigQueryRunner)(project, location=location, max_bytes=cap, timeout=args.timeout)


def execute(args, summary: dict, stdin, bigquery=None) -> None:
    """Fill ``summary`` for one parsed ``run`` command; raises :class:`QueryRunError` for a run that cannot go on."""

    from . import scope_queries

    sql = read_sql(args.source, stdin)
    runner = _runner(args, bigquery)
    summary["backend"] = runner.backend
    summary["max_bytes_billed"] = runner.max_bytes
    fmt = args.format or ("csv" if args.output is not None and args.output.suffix.lower() == ".csv" else "json")
    checks = Preflight()
    summary["checked"], summary["not_checked"], summary["warnings"] = checks.checked, checks.not_checked, checks.warnings
    preflight(sql, static=not args.no_preflight, snapshot=args.schema_snapshot, need_tree=runner.backend == "duckdb", result=checks)
    text = runner.translate(checks.tree) if runner.backend == "duckdb" else sql
    plan = runner.plan(text)
    summary["checked"].append(plan.check)
    if runner.backend == "bigquery" and plan.statement_type and plan.statement_type.upper() != "SELECT":
        raise SqlError(f"refused: BigQuery reads this as a {plan.statement_type} statement; KumoSQL only reads (one SELECT)")
    if runner.backend == "bigquery" and checks.tree is None and (plan.statement_type or "").upper() != "SELECT":
        raise SqlError("refused: sqlglot could not parse the SQL and the dry run did not confirm it is one read-only SELECT")
    summary["columns"] = len(plan.columns)
    summary["bytes_estimated"], summary["bytes_accuracy"] = plan.estimated_bytes, plan.bytes_accuracy
    if runner.max_bytes is not None and plan.estimated_bytes is not None:
        summary["within_cap"] = plan.estimated_bytes <= runner.max_bytes
    if args.summarize:
        summary["column_stats"] = [{"name": name, "type": type_name} for name, type_name in plan.columns]
    if args.dry_run:
        summary["status"], summary["exit_code"] = "dry_run", EXIT_OK
        return
    if summary["within_cap"] is False:
        bound = {"upper_bound": "at most", "exact": "exactly"}.get(plan.bytes_accuracy or "", "about")
        raise ByteCapError(f"the dry run estimates {bound} {scope_queries._size(plan.estimated_bytes)}, over the "
                           f"{scope_queries._size(runner.max_bytes)} cap; narrow the query or raise --max-bytes")
    output = (args.output or default_output(sql, fmt)).expanduser().resolve()
    try:  # before the job, so a folder that cannot be made never costs a query
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"could not create the folder for the results file: {exc.strerror or exc}") from None
    result = runner.run(text, args.max_rows)
    meta = {"row_count": len(result.rows), "total_rows": result.total_rows, "truncated": result.truncated,
            "max_rows": args.max_rows, "backend": runner.backend, "job_id": result.job_id,
            "bytes_processed": result.bytes_processed, "bytes_billed": result.bytes_billed}
    write_results(output, fmt, result, meta)
    summary.update(
        status="ok", exit_code=EXIT_OK, rows=len(result.rows), total_rows=result.total_rows, truncated=result.truncated,
        columns=len(result.columns), bytes_processed=result.bytes_processed, bytes_billed=result.bytes_billed,
        cache_hit=result.cache_hit, job_id=result.job_id, output=str(output), format=fmt,
    )
    if args.summarize:
        summary["column_stats"] = [column_stats(name, type_name, [row[i] for row in result.rows])
                                   for i, (name, type_name) in enumerate(result.columns)]
        summary["head"] = {"columns": [name for name, _ in result.columns],
                           "rows": [[_cell(v) for v in row] for row in result.rows[: args.head]]}
        if result.truncated:
            summary["not_checked"].append(f"column_stats describe the {len(result.rows):,} rows in the file, not every row")


def _write(stream, text: str) -> None:
    data = (text + "\n").encode("ascii", errors="backslashreplace")
    target = getattr(stream, "buffer", stream)
    try:
        target.write(data)
    except TypeError:  # a text-only stream
        stream.write(data.decode("ascii"))
    stream.flush()


def run(argv: Sequence[str] | None = None, *, stdin=None, stdout=None, stderr=None, bigquery=None, prog: str | None = None) -> int:
    """The command, with its streams and BigQuery runner injectable for tests. Returns the exit code."""

    stdin = getattr(sys.stdin, "buffer", sys.stdin) if stdin is None else stdin
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    parser = _parser(prog or "python -m kumosql q")
    summary = _base_summary(None)
    summarize, cap = False, DEFAULT_SUMMARY_BYTES
    try:
        with warnings.catch_warnings():  # nothing but the summary reaches the console
            warnings.simplefilter("ignore")
            try:
                args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
            except SystemExit as stop:  # --help printed its text
                return int(stop.code or 0)
            summarize, cap = args.summarize, args.summary_bytes
            if summarize:
                summary.update(column_stats=[], head=None, summary_truncated=False)
            execute(args, summary, stdin, bigquery)
    except QueryRunError as exc:
        summary.update(status="error", exit_code=exc.exit_code, error={"kind": exc.kind, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - an unexpected failure still gets the fixed-shape summary and its own code
        error = QueryRunError(f"unexpected error: {type(exc).__name__}: {exc}")
        summary.update(status="error", exit_code=EXIT_OTHER, error={"kind": "internal", "message": str(error)})
    for key in ("warnings", "not_checked"):
        if len(summary[key]) > LIST_ITEMS:
            extra = len(summary[key]) - LIST_ITEMS
            summary[key] = summary[key][:LIST_ITEMS] + [f"... and {extra} more"]
    _write(stdout, fit(summary, cap))
    if summary["error"]:
        _write(stderr, f"q: {summary['error']['kind']} error (exit {summary['exit_code']}): {summary['error']['message']}")
    return summary["exit_code"]


def main(argv: Sequence[str] | None = None) -> int:
    prog = sys.argv[0] if sys.argv and str(sys.argv[0]).startswith("python -m kumosql") else "python -m kumosql q"
    return run(argv, prog=prog)


if __name__ == "__main__":
    raise SystemExit(main())
