"""``--boundary FILE`` for the command line: what a producers-and-consumers file adds to ``pipeline-report``.

The file, its validation and the analysis are those of the agent-facing server (:mod:`kumosql.boundary`); this module only
loads them for a project that was loaded by the command line and shapes the result as JSON keys. Nothing is read or written
beyond the boundary file and the files it names; no query is sent.

Without ``--boundary`` the report and the ``--assess`` result are exactly what they were. With it they gain one top-level key,
``boundary``:

* the report: ``external_models`` (one per producer, its SQL read as a model outside the project, with the columns it builds
  and what each reads), ``readers_outside_project`` (per project table, who outside the project reads it) and ``unknown``;
* ``--assess``: ``readers_outside_project`` for the target and ``unknown``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .boundary import MAX_ITEMS, Boundary, BoundarySpec, build
from .mcp_session import Session

if TYPE_CHECKING:
    from .pipeline import Pipeline

NOTE = (
    "Only what the boundary file names: a job, dashboard or semantic model it does not list is invisible, so nothing here says a "
    "table is unused or safe to delete. 'reads (by text)' and 'reads (Malloy)' are approximations, labelled as such."
)


def load(pipeline: "Pipeline", spec: BoundarySpec) -> tuple[Boundary, Session]:
    """The spec checked against the loaded project (every producer table must be a source the project reads and does not build)."""

    session = Session(pipeline)
    return build(spec, session), session


def _unique(pairs: list[tuple[str, str]]) -> list[dict]:
    seen: dict[tuple[str, str], None] = {}
    for what, reason in pairs:
        seen.setdefault((what, reason), None)
    items = [{"what": what, "reason": reason} for what, reason in seen]
    return items[:MAX_ITEMS]


def _producer(boundary: Boundary, key: str) -> dict:
    producer = boundary.producers[key]
    outside = boundary.outside()
    snippets = boundary._snippets.get(key, []) if outside is not None else []
    built_by: dict = {"kind": producer.kind}
    if producer.ident:
        built_by["id"] = producer.ident
    if producer.url:
        built_by["url"] = producer.url
    item: dict = {"table": key, "built_by": built_by}
    if not snippets:
        item["status"] = "sql_not_available" if producer.unavailable else "no_sql"
        item["unknown"] = f"nothing can be followed through it: {producer.no_sql_reason}"
        return item
    item["status"] = "sql_parsed"
    item["file"] = producer.sql.path
    if len(snippets) > 1:
        item["sql_snippets"] = len(snippets)
    table_reads = outside.table_reads()
    item["reads"] = sorted({table for part in snippets for table in table_reads.get(part, ())})
    records = outside.explain_lineage()
    columns = []
    for ref, record in records.items():
        if ref.table not in snippets:
            continue
        column: dict = {"column": ref.column, "status": record.status, "transform": record.transform,
                        "sources": sorted(str(source) for source in record.sources)}
        if record.reason:
            column["reason"] = record.reason.split(":")[0]
        columns.append(column)
    item["columns"] = columns
    return item


def report_section(boundary: Boundary, session: Session, *, keep: set[str] | None = None, file: Path | None = None) -> dict:
    """The ``boundary`` key of the report. ``keep`` limits the readers listed to those project tables (a ``--scope``)."""

    pipeline = session.pipeline()
    section: dict = {"note": NOTE}
    if file is not None:
        section["file"] = file.name
    boundary.outside()  # the producers' and consumers' SQL, analysed once
    section["external_models"] = [_producer(boundary, key) for key in sorted(boundary.producers)]
    tables = sorted(key for key in (*pipeline.models, *pipeline.sources) if keep is None or key in keep)
    unknown: list[tuple[str, str]] = []
    readers: list[dict] = []
    by_consumer: dict[str, int] = {}
    for key in tables:
        found = boundary.readers("drop_table", key, None)
        unknown.extend(found.unknown)
        if found.items:
            readers.append({"table": key, "readers": found.items})
            for item in found.items:
                by_consumer[item["name"]] = by_consumer.get(item["name"], 0) + 1
    section["readers_outside_project"] = readers
    section["counts"] = {
        "producers": len(boundary.producers),
        "consumers": len(boundary.spec.consumers),
        "tables_read_outside_project": len(readers),
        "outside_readers": len(by_consumer),
    }
    if keep is not None:
        section["scope_note"] = "readers are listed for the tables in the scope only; the producers are all listed"
    section["unknown"] = _unique(unknown)
    return section


def assess_section(boundary: Boundary, session: Session, change: str, table: str, column: str | None, *, file: Path | None = None) -> dict:
    """The ``boundary`` key of an ``--assess`` result: who outside the project reads the target."""

    key, _close = session.resolve(table)
    section: dict = {"note": NOTE}
    if file is not None:
        section["file"] = file.name
    if key is None:
        section["readers_outside_project"] = []
        section["unknown"] = [{"what": table, "reason": "the table is not a table of the project, so outside readers were not looked up"}]
        return section
    found = boundary.readers(change, key, column)
    section["readers_outside_project"] = found.items
    section["counts"] = {"outside_readers": len(found.items)}
    section["unknown"] = _unique(found.unknown)
    return section
