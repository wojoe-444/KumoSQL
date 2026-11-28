"""Views the project reads but does not define: name where lineage stops, and optionally read their definitions.

A project often reads a BigQuery view it does not define (a declaration, or a table named in an old job's SQL). Its
columns come from tables the project cannot see, so a trace that reaches it cannot go on. When KumoSQL knows the table
is a view, ``Pipeline.trace_column`` stops there with status ``unknown`` and reason ``opaque_view`` (the hop's ``model``
is the view), instead of calling its columns sources. It knows a table is a view from:

* ``Pipeline.table_types``, which :func:`attach` fills and a caller may set;
* the saved BigQuery catalog (the UI's table browser, or an earlier schema lookup saved the table's type). Reading it
  calls nothing.

When it does not know, nothing changes: a column of a table of unknown type is a source, as before. Nothing is guessed.

**Reading the definitions** (:func:`attach`) is off by default and follows the same opt-in as the schema lookup
(:mod:`kumosql.schema_fetch`): ``KUMOSQL_SCHEMA_FETCH=1``, the Settings checkbox, or ``--fetch-schema``. It uses the same
credentials. For every table the project's SQL reads and does not define, it asks BigQuery's free ``tables.get``
metadata call (saved in the catalog cache, like the schema lookup), which gives a view's ``view.query``. A view with a
definition becomes a model of kind ``view`` with ``fetched_from`` (``tables.get``) and ``fetched_at`` (when it was
read), so the trace continues through it, and through the views it reads in turn (a few levels deep). A view
BigQuery will not describe is a gap, ``view_definition_unreadable`` (``view p.d.v: definition not readable
(permission)``), never a guess. ``INFORMATION_SCHEMA.VIEWS`` is a billed query (at least 10 MB per dataset): it is used
only with an explicit ``billed=True`` (``--fetch-views-billed``), only for views the free call could not define, one
query per dataset, dry-run first and under the byte cap in Settings. Credentials are never printed or stored.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .pipeline_types import Model, PipelineDiagnostic, Target

if TYPE_CHECKING:
    from .pipeline import Pipeline

VIEW_TYPES = frozenset({"VIEW", "MATERIALIZED_VIEW"})
MAX_VIEWS = 200  # definitions read per load; the rest stay opaque
MAX_DEPTH = 4  # views read through views
WORKERS = 8
MAX_SECONDS = 60.0  # per load, like the schema lookup; tables not described by then stay as they are
FREE_SOURCE = "tables.get"
BILLED_SOURCE = "INFORMATION_SCHEMA.VIEWS"
_NAME = re.compile(r"^[A-Za-z0-9_\-$]+$")


# ------------------------------------------------------------------ knowing a table is a view


def _catalog_types() -> dict[str, str]:
    """``{"project.dataset.table" (case folded): type}`` from the saved catalog: table entries and dataset listings."""

    from . import bigquery_catalog

    types: dict[str, str] = {}
    try:
        tables = bigquery_catalog.saved_tables()
        listings = bigquery_catalog.saved_listings()
    except Exception:  # noqa: BLE001 - an unreadable catalog knows nothing
        return types
    for project, dataset, table, data in tables:
        if isinstance(data, dict) and data.get("type"):
            types[f"{project}.{dataset}.{table}".casefold()] = str(data["type"]).upper()
    for project, dataset, items in listings:
        for item in items:
            if item.get("id") and item.get("type"):
                types.setdefault(f"{project}.{dataset}.{item['id']}".casefold(), str(item["type"]).upper())
    return types


def _full_name(pipeline: "Pipeline", key: str) -> str:
    parts = [part for part in key.replace("`", "").split(".") if part]
    if len(parts) == 2 and pipeline.default_project:
        parts = [pipeline.default_project, *parts]
    return ".".join(parts)


def table_type(pipeline: "Pipeline", key: str) -> str | None:
    """What BigQuery says the table is (``VIEW``, ``TABLE``, ...), when KumoSQL knows; ``None`` otherwise. Calls nothing."""

    known = getattr(pipeline, "table_types", None) or {}
    for name in (key, _full_name(pipeline, key)):
        found = known.get(name) or next((kind for other, kind in known.items() if other.casefold() == name.casefold()), None)
        if found:
            return str(found).upper()
    catalog = pipeline._remembered(("catalog_table_types",), _catalog_types)
    return catalog.get(_full_name(pipeline, key).casefold())


def is_opaque_view(pipeline: "Pipeline", key: str) -> bool:
    """Whether ``key`` is a view the pipeline has no definition for, so a trace that reaches it must stop there."""

    return key not in pipeline.models and table_type(pipeline, key) in VIEW_TYPES


# ------------------------------------------------------------------ reading definitions


def _enabled(fetch: bool | None) -> bool:
    from . import schema_fetch

    return schema_fetch.enabled() if fetch is None else bool(fetch)


def _read_tables(pipeline: "Pipeline", models: dict[str, Model]) -> dict[str, str]:
    """``{key: spelling}`` for every table the models' SQL reads that no model defines (declared sources and tables outside)."""

    from .scripts import token_reads

    found: dict[str, str] = {}
    for model in models.values():
        for text in (model.sql, *model.scripts):
            if not text or not text.strip():
                continue
            try:
                reads, _writes = token_reads(text)
            except Exception:  # noqa: BLE001 - a statement the token reader cannot read adds nothing
                continue
            for table in reads:
                if not table.name or not table.db:
                    continue  # a temporary table, a CTE or a name with no dataset: not a table BigQuery can describe
                spelled = ".".join(part for part in (table.catalog, table.db, table.name) if part)
                key = pipeline.resolve(spelled) or spelled
                if key not in models:
                    found.setdefault(key, spelled)
    return found


def _triple(pipeline: "Pipeline", name: str) -> tuple[str, str, str] | None:
    from . import schema_fetch

    triple = schema_fetch._parts(name, pipeline.default_project)
    if triple is None or "*" in triple[2] or triple[1].startswith("_") or "INFORMATION_SCHEMA" in (part.upper() for part in triple):
        return None
    return triple


def _now(at: float | None) -> str:
    moment = datetime.fromtimestamp(at, timezone.utc) if at else datetime.now(timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _describe(triple: tuple[str, str, str]) -> tuple[dict | None, float | None, str | None]:
    """``(metadata, fetched at, None)`` from ``tables.get`` (through the catalog cache), or ``(None, None, reason)``."""

    from . import bigquery_catalog, schema_fetch

    key = schema_fetch._key(*triple)

    def fetch():
        return bigquery_catalog.get_table(*triple)

    try:
        entry = bigquery_catalog.cached(key, fetch)
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, dict) and str(data.get("type", "")).upper() in VIEW_TYPES and "view_query" not in data:
            entry = bigquery_catalog.cached(key, fetch, refresh=True)  # saved before definitions were asked for
            data = entry.get("data")
    except bigquery_catalog.CatalogError as exc:
        return None, None, "permission" if exc.status == 403 else "not found" if exc.status == 404 else "error"
    except Exception:  # noqa: BLE001 - one table failing must not stop the others
        return None, None, "error"
    return (data if isinstance(data, dict) else None), entry.get("fetchedAt"), None


def _listed_type(triple: tuple[str, str, str]) -> str | None:
    """The table's type from its dataset's listing (``tables.list``, free), when ``tables.get`` would not say."""

    from . import bigquery_catalog

    project, dataset, table = triple
    try:
        listing = bigquery_catalog.cached(bigquery_catalog.tables_key(project, dataset), lambda: bigquery_catalog.list_tables(project, dataset))
    except Exception:  # noqa: BLE001 - the listing is a second chance, not a requirement
        return None
    for item in listing.get("data") or ():
        if isinstance(item, dict) and str(item.get("id", "")) == table:
            return str(item.get("type") or "").upper() or None
    return None


def _run_billed(sql: str, project: str, max_bytes: int):
    """Run one read-only query under the byte cap (dry run first); returns the ``data_sources.Run``. Tests replace this."""

    from . import data_sources

    source = data_sources.Source("view-definitions", "view definitions", "bigquery_sql", sql)
    return data_sources.RUNNERS["bigquery_sql"](source, project, max_bytes)


def _billed_definitions(views: dict[tuple[str, str, str], str], max_bytes: int | None) -> tuple[dict, dict[tuple[str, str], str]]:
    """Definitions from ``INFORMATION_SCHEMA.VIEWS``, one query per dataset: ``({triple: sql}, {(project, dataset): reason})``."""

    from . import bigquery_catalog, scope_queries

    cap = max_bytes if max_bytes is not None else scope_queries.get_settings().max_bytes_billed
    by_dataset: dict[tuple[str, str], list[str]] = {}
    for project, dataset, table in views:
        if _NAME.match(table) and _NAME.match(dataset) and _NAME.match(project.replace(".", "").replace(":", "")):
            by_dataset.setdefault((project, dataset), []).append(table)
    found: dict[tuple[str, str, str], str] = {}
    failed: dict[tuple[str, str], str] = {}
    for (project, dataset), tables in sorted(by_dataset.items()):
        names = ", ".join(f"'{name}'" for name in sorted(set(tables)))
        sql = (
            f"SELECT table_name, view_definition, use_standard_sql FROM `{project}.{dataset}.INFORMATION_SCHEMA.VIEWS` "
            f"WHERE table_name IN ({names})"
        )
        try:
            run = _run_billed(sql, bigquery_catalog.billing_project() or project, cap)
        except Exception as exc:  # noqa: BLE001 - a refused query, the byte cap, no billing project: the views stay opaque
            text = str(exc).lower()
            failed[(project, dataset)] = "permission" if "403" in text or "denied" in text or "permission" in text else "query failed"
            continue
        columns = [str(name).lower() for name in run.columns]
        for row in run.rows:
            record = dict(zip(columns, row))
            name, definition = record.get("table_name"), record.get("view_definition")
            standard = str(record.get("use_standard_sql", "YES")).upper() in ("YES", "TRUE", "1", "NONE")
            if name and definition and standard:
                found[(project, dataset, str(name))] = str(definition)
    return found, failed


def attach(
    pipeline: "Pipeline", *, fetch: bool | None = None, billed: bool = False, max_bytes: int | None = None
) -> "Pipeline":
    """``pipeline`` with the definitions of the views it reads as models, when the schema-lookup opt-in is on.

    Off (the default unless ``KUMOSQL_SCHEMA_FETCH=1``, the Settings checkbox or ``--fetch-schema`` turned it on, or
    ``fetch=True``): returns ``pipeline`` unchanged and calls nothing; a view known from the saved catalog still stops a
    trace as ``opaque_view``. On: every table the SQL reads that no model defines is described with ``tables.get``;
    views with a definition become models (and their own reads are described in turn, up to ``MAX_DEPTH`` levels and
    ``MAX_VIEWS`` views), every type learnt is kept in ``table_types``, and a view or table BigQuery would not describe
    is a ``view_definition_unreadable`` gap. ``billed`` also asks ``INFORMATION_SCHEMA.VIEWS`` for the views the free
    call could not define (a billed query per dataset, under ``max_bytes`` or the cap in Settings).
    """

    if not _enabled(fetch):
        return pipeline
    from . import schema_fetch
    from .job_sql import _qualify_statement

    diagnostics: list[PipelineDiagnostic] = []
    if not schema_fetch._credentials_ok():
        diagnostics.append(PipelineDiagnostic(
            "", "view_fetch_skipped", "view definitions were not read: no BigQuery credentials were found, so views stay opaque"))
        return replace(pipeline, diagnostics=[*pipeline.diagnostics, *diagnostics])
    models = dict(pipeline.models)
    types = dict(pipeline.table_types)
    asked: set[tuple[str, str, str]] = set()
    unread: dict[tuple[str, str, str], tuple[str, str]] = {}  # triple -> (key, reason) for views the free call did not define
    added = 0
    deadline = time.monotonic() + MAX_SECONDS
    pending = _read_tables(pipeline, models)
    for _depth in range(MAX_DEPTH):
        todo: list[tuple[str, tuple[str, str, str]]] = []
        for key, spelled in sorted(pending.items()):
            triple = _triple(pipeline, key if key in pipeline.sources else spelled)
            if triple is None or triple in asked or len(asked) + len(todo) >= MAX_VIEWS * 5:
                continue
            todo.append((key, triple))
        if not todo:
            break
        asked.update(triple for _key, triple in todo)
        def describe(item):
            if time.monotonic() > deadline:
                return None, None, "out of time"  # not asked: it stays as it is, with no gap claimed
            return _describe(item[1])

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            answers = list(pool.map(describe, todo))
        new_views: dict[str, Model] = {}
        for (key, triple), (data, fetched_at, reason) in zip(todo, answers):
            name = ".".join(triple)
            if data is None:
                listed = _listed_type(triple) if reason == "permission" else None
                if listed:
                    types[key] = types[name] = listed
                if listed in VIEW_TYPES:
                    unread[triple] = (key, reason or "error")
                elif reason in ("permission", "not found"):
                    diagnostics.append(PipelineDiagnostic(
                        key, "view_definition_unreadable",
                        f"{name}: metadata not readable ({reason}), so whether it is a view, and what it reads, is unknown"))
                continue
            kind = str(data.get("type") or "TABLE").upper()
            types[key] = types[name] = kind
            if kind not in VIEW_TYPES:
                continue
            query = data.get("view_query")
            if not query or data.get("view_legacy_sql"):
                why = "it is legacy SQL, which is not read" if data.get("view_legacy_sql") else "BigQuery gave no definition"
                diagnostics.append(PipelineDiagnostic(key, "view_definition_unreadable", f"view {name}: definition not readable ({why})"))
                continue
            if added + len(new_views) >= MAX_VIEWS:
                diagnostics.append(PipelineDiagnostic(key, "view_definition_unreadable", f"view {name}: not read, more than {MAX_VIEWS} views"))
                continue
            view = _view_model(pipeline, key, triple, query, FREE_SOURCE, _now(fetched_at), _qualify_statement)
            new_views[view.key] = view
        models.update(new_views)
        added += len(new_views)
        pending = {key: spelled for key, spelled in _read_tables(pipeline, new_views).items() if key not in models}
        if not new_views:
            break
    if unread and billed:
        found, failed = _billed_definitions({triple: key for triple, (key, _reason) in unread.items()}, max_bytes)
        for triple, query in found.items():
            view = _view_model(pipeline, unread[triple][0], triple, query, BILLED_SOURCE, _now(time.time()), _qualify_statement)
            models[view.key] = view
            added += 1
        for triple, (key, reason) in unread.items():
            if triple not in found:
                again = failed.get(triple[:2])
                unread[triple] = (key, f"{reason}; {BILLED_SOURCE}: {again}" if again else f"{reason}; not in {BILLED_SOURCE}")
        unread = {triple: value for triple, value in unread.items() if triple not in found}
    for triple, (key, reason) in sorted(unread.items()):
        hint = "" if billed else f" (with --fetch-views-billed, {BILLED_SOURCE} is asked too)"
        diagnostics.append(PipelineDiagnostic(
            key, "view_definition_unreadable", f"view {'.'.join(triple)}: definition not readable ({reason}){hint}"))
    if added:
        diagnostics.append(PipelineDiagnostic("", "view_definition_fetched", f"read the definitions of {added} view(s) from BigQuery"))
    return replace(pipeline, models=models, table_types=types, diagnostics=[*pipeline.diagnostics, *diagnostics])


def _view_model(pipeline: "Pipeline", key: str, triple: tuple[str, str, str], query: str, source: str, at: str, qualify) -> Model:
    project, dataset, table = triple
    target = pipeline.sources.get(key) or Target(project, dataset, table)
    # A view's partial names mean the view's own project (BigQuery views name their datasets).
    sql = qualify(query, project, "", frozenset())
    return Model(target, "view", sql, fetched_from=source, fetched_at=at)


__all__ = ["VIEW_TYPES", "attach", "is_opaque_view", "table_type"]
