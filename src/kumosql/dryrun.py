"""BigQuery dry runs for query planning and output-schema comparison.

A dry run asks BigQuery to plan a query without executing it. It costs
nothing and reads no data, but it resolves every table and column, checks
types, and returns the output schema and an estimate of bytes processed.
:func:`check_rewrite` reports whether both queries planned with matching
schemas. This does not show that their results are equal.

Authentication, in order of preference:

1. an explicit ``token`` argument;
2. ``BQ_ACCESS_TOKEN`` (an OAuth access token);
3. a service account key in ``GOOGLE_APPLICATION_CREDENTIALS_JSON`` (the key
   file's contents) or ``GOOGLE_APPLICATION_CREDENTIALS`` (a path), which
   needs the optional ``google-auth`` dependency (``pip install
   kumosql[bigquery]``).
4. Application Default Credentials, including credentials created by
   ``gcloud auth application-default login`` (also needs ``google-auth``).

Tests pass a fake ``transport`` so nothing here needs network access.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Callable
import urllib.error
import urllib.request

from sqlglot import exp

from kumo_sql_prover.ast_utils import parse_statements
from .sql_validation import quote_table_path

Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict]]

_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
_TYPE_ALIASES = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
    "RECORD": "STRUCT",
}


@dataclass(frozen=True)
class Field:
    """One output column; ``fields`` holds STRUCT members."""

    name: str
    type: str
    mode: str = "NULLABLE"
    fields: tuple["Field", ...] = ()

    @classmethod
    def from_api(cls, raw: dict) -> "Field":
        return cls(
            name=raw.get("name", ""),
            type=_TYPE_ALIASES.get(raw.get("type", ""), raw.get("type", "")),
            mode=raw.get("mode", "NULLABLE") or "NULLABLE",
            fields=tuple(cls.from_api(item) for item in raw.get("fields", [])),
        )

    def describe(self) -> str:
        inner = f"<{', '.join(f.describe() for f in self.fields)}>" if self.fields else ""
        mode = "" if self.mode == "NULLABLE" else f" {self.mode}"
        return f"{self.name} {self.type}{inner}{mode}"


@dataclass(frozen=True)
class DryRunResult:
    ok: bool
    schema: tuple[Field, ...] = ()
    total_bytes_processed: int | None = None
    referenced_tables: tuple[str, ...] = ()
    error_reason: str | None = None
    error_message: str | None = None
    error_location: str | None = None
    # False when a planned response carried no output schema: then ``schema`` is empty because
    # nothing was observed, not because the query has no columns.
    schema_observed: bool = True


@dataclass(frozen=True)
class RewriteCheck:
    planned_same_schema: bool | None
    reason: str
    original: DryRunResult | None
    rewritten: DryRunResult | None
    schema_differences: tuple[str, ...] = ()

    @property
    def outcome(self) -> str:
        if self.planned_same_schema is None:
            return "not_run"
        return "passed" if self.planned_same_schema else "failed"

    @property
    def ok(self) -> bool:
        """Compatibility alias; prefer ``planned_same_schema``."""

        return self.planned_same_schema is True

    @property
    def original_planned(self) -> bool | None:
        return None if self.original is None else self.original.ok

    @property
    def rewritten_planned(self) -> bool | None:
        return None if self.rewritten is None else self.rewritten.ok

    @property
    def schema_matches(self) -> bool | None:
        if self.original_planned is not True or self.rewritten_planned is not True:
            return None
        if self.original is None or self.rewritten is None or not (self.original.schema_observed and self.rewritten.schema_observed):
            return None  # never "matches" unless both schemas were actually observed
        return not self.schema_differences

    @property
    def estimated_bytes_delta(self) -> int | None:
        if self.original is None or self.rewritten is None:
            return None
        before = self.original.total_bytes_processed
        after = self.rewritten.total_bytes_processed
        return None if before is None or after is None else after - before

    @property
    def bytes_delta(self) -> int | None:
        """Compatibility alias; the value is an estimate, not a result metric."""

        return self.estimated_bytes_delta


def _urllib_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"error": {"message": payload.decode("utf-8", "replace")}}


def access_token(scope: str = _SCOPE) -> str:
    """Find credentials in the environment and return an OAuth access token for ``scope``."""

    token = os.environ.get("BQ_ACCESS_TOKEN")
    if token:
        return token
    raw_key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw_key and not key_path:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "no BigQuery credentials found; Google Cloud credentials need google-auth: "
                "pip install 'kumosql[bigquery]'"
            ) from exc
        try:
            credentials, _ = google.auth.default(scopes=[scope])
            if not credentials.valid:
                credentials.refresh(Request())
        except Exception:
            raise RuntimeError(
                "no BigQuery credentials: set BQ_ACCESS_TOKEN, configure a service account, "
                "or run 'gcloud auth application-default login'"
            ) from None
        return credentials.token
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "service account credentials need google-auth: pip install 'kumosql[bigquery]'"
        ) from exc
    try:
        if raw_key:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(raw_key), scopes=[scope]
            )
        else:
            credentials = service_account.Credentials.from_service_account_file(key_path, scopes=[scope])
        if not credentials.valid:
            credentials.refresh(Request())
    except Exception:
        raise RuntimeError("could not load or refresh service account credentials") from None
    return credentials.token


def dry_run(
    sql: str,
    project: str,
    *,
    location: str | None = None,
    token: str | None = None,
    transport: Transport | None = None,
) -> DryRunResult:
    """Dry-run one GoogleSQL query in ``project`` (the project billed, not read)."""

    body: dict = {
        "configuration": {
            "dryRun": True,
            "query": {"query": sql, "useLegacySql": False},
        }
    }
    if location:
        body["jobReference"] = {"location": location}
    headers = {
        "Authorization": f"Bearer {token or access_token()}",
        "Content-Type": "application/json",
    }
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/jobs"
    status, payload = (transport or _urllib_transport)(url, headers, json.dumps(body).encode("utf-8"))

    if status >= 400 or "error" in payload:
        error = payload.get("error", {})
        first = (error.get("errors") or [{}])[0]
        return DryRunResult(
            ok=False,
            error_reason=first.get("reason") or error.get("status"),
            error_message=error.get("message") or first.get("message") or f"HTTP {status}",
            error_location=first.get("location"),
        )

    statistics = payload.get("statistics", {})
    query_stats = statistics.get("query", {})
    schema_object = query_stats.get("schema")
    # Observed means the response carried a fields list; `{}` or `{"schema": {}}` observed nothing.
    observed = isinstance(schema_object, dict) and isinstance(schema_object.get("fields"), list)
    schema = schema_object["fields"] if observed else []
    tables = tuple(
        ".".join(filter(None, (t.get("projectId"), t.get("datasetId"), t.get("tableId"))))
        for t in query_stats.get("referencedTables", [])
    )
    processed = statistics.get("totalBytesProcessed") or query_stats.get("totalBytesProcessed")
    return DryRunResult(
        ok=True,
        schema=tuple(Field.from_api(item) for item in schema),
        total_bytes_processed=int(processed) if processed is not None else None,
        referenced_tables=tables,
        schema_observed=observed,
    )


def schema_differences(
    before: tuple[Field, ...], after: tuple[Field, ...], *, prefix: str = ""
) -> tuple[str, ...]:
    """Describe how two output schemas differ; empty means identical.

    Column names compare case-insensitively (as BigQuery does) but order,
    type, mode and nested STRUCT members must all match.
    """

    differences: list[str] = []
    if [f.name.lower() for f in before] != [f.name.lower() for f in after]:
        differences.append(
            f"{prefix or 'columns'}: "
            f"[{', '.join(f.name for f in before)}] != [{', '.join(f.name for f in after)}]"
        )
        return tuple(differences)
    for left, right in zip(before, after):
        path = f"{prefix}{left.name}"
        if (left.type, left.mode) != (right.type, right.mode):
            differences.append(f"{path}: {left.describe()} != {right.describe()}")
        elif left.fields or right.fields:
            differences.extend(schema_differences(left.fields, right.fields, prefix=f"{path}."))
    return tuple(differences)


def check_rewrite(
    original_sql: str,
    rewritten_sql: str,
    project: str,
    *,
    location: str | None = None,
    token: str | None = None,
    transport: Transport | None = None,
) -> RewriteCheck:
    """Dry-run both queries and compare their output schemas.

    A matching plan and schema do not establish that the queries return the
    same rows. Byte counts are estimates and are reported separately.
    """

    try:
        original_statements = parse_statements(original_sql)
        rewritten_statements = parse_statements(rewritten_sql)
    except Exception:
        return RewriteCheck(
            None,
            "planner check was not run: both inputs must be single SELECT statements",
            None,
            None,
        )
    if (
        len(original_statements) != 1
        or len(rewritten_statements) != 1
        or not isinstance(original_statements[0], exp.Query)
        or not isinstance(rewritten_statements[0], exp.Query)
    ):
        return RewriteCheck(
            None,
            "planner check was not run: both inputs must be single SELECT statements",
            None,
            None,
        )

    original = dry_run(original_sql, project, location=location, token=token, transport=transport)
    rewritten = dry_run(rewritten_sql, project, location=location, token=token, transport=transport)
    if not original.ok:
        return RewriteCheck(
            False,
            f"original SQL could not be planned: {original.error_message}",
            original,
            rewritten,
        )
    if not rewritten.ok:
        return RewriteCheck(
            False,
            f"rewritten SQL could not be planned: {rewritten.error_message}",
            original,
            rewritten,
        )
    unobserved = [name for name, result in (("original", original), ("rewritten", rewritten)) if not result.schema_observed]
    if unobserved:
        return RewriteCheck(
            None,
            f"both SQL statements planned, but the dry run returned no output schema for the {' and '.join(unobserved)} SQL;"
            " schemas were not compared",
            original,
            rewritten,
        )
    differences = schema_differences(original.schema, rewritten.schema)
    if differences:
        return RewriteCheck(
            False,
            "both SQL statements planned, but their output schemas differ",
            original,
            rewritten,
            differences,
        )
    return RewriteCheck(
        True,
        "both SQL statements planned and their output schemas match; results were not compared",
        original,
        rewritten,
    )


def fetch_table_schemas(
    tables: list[str],
    project: str,
    *,
    location: str | None = None,
    token: str | None = None,
    transport: Transport | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Read top-level column names and types for tables via free dry runs.

    Returns ``(schemas, errors)``; ``schemas`` can be passed straight to the
    pipeline loaders as ``source_schema``.
    """

    schemas: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    # Validate the whole batch before a valid first table can acquire credentials
    # or build a request while a later path contains SQL punctuation.
    quoted = [quote_table_path(table) for table in tables]
    for table, reference in zip(tables, quoted):
        result = dry_run(
            f"SELECT * FROM {reference}",
            project,
            location=location,
            token=token,
            transport=transport,
        )
        if result.ok and result.schema_observed:
            schemas[table] = {field.name: field.type for field in result.schema}
        elif result.ok:
            errors[table] = "the dry run returned no schema"
        else:
            errors[table] = result.error_message or "dry run failed"
    return schemas, errors
