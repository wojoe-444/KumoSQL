"""The equivalence solver as the app uses it: settings, the facts it may assume, one entry point.

Rewrite verification asks ``prove`` when the structural prover cannot establish
equivalence. ``prove`` runs the algebraic prover (rewrites plus the SMT prover) with
the columns, NOT NULL columns and keys of the loaded Dataform project and the
saved BigQuery catalog, so a rewrite that depends on a key is provable once
the key is declared. The solver is on by default; ``enabled`` and the time
limit per solver check live in the ``prover`` section of the saved settings.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading

from . import state
from .prover_schema import ProverSchema, from_pipeline
from kumo_sql_prover.smt_equivalence import SmtEquivalenceResult, SmtStatus, z3

DEFAULT_TIMEOUT_MS = 5000
MIN_TIMEOUT_MS = 500
MAX_TIMEOUT_MS = 60000
DEFAULT_BOUNDED_ROWS = 3  # rows per table in the bounded check; 0 turns it off
MAX_BOUNDED_ROWS = 6

_LOCK = threading.Lock()
_CACHE: dict = {}


def settings() -> dict:
    """``{"enabled": bool, "timeout_ms": int, "bounded_rows": int}`` with defaults filled in."""

    saved = state.get_section("prover", {}) or {}
    enabled = saved.get("enabled", True)
    timeout = saved.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        timeout = DEFAULT_TIMEOUT_MS
    rows = saved.get("bounded_rows", DEFAULT_BOUNDED_ROWS)
    if not isinstance(rows, int) or isinstance(rows, bool):
        rows = DEFAULT_BOUNDED_ROWS
    return {
        "enabled": enabled if isinstance(enabled, bool) else True,
        "timeout_ms": min(max(timeout, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS),
        "bounded_rows": min(max(rows, 0), MAX_BOUNDED_ROWS),
    }


def save_settings(enabled: object = None, timeout_ms: object = None, bounded_rows: object = None) -> dict:
    """Update the solver settings; refuses values of the wrong type or range."""

    current = settings()
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        current["enabled"] = enabled
    if timeout_ms is not None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not (
            MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS
        ):
            raise ValueError(f"timeout_ms must be a whole number from {MIN_TIMEOUT_MS} to {MAX_TIMEOUT_MS}")
        current["timeout_ms"] = timeout_ms
    if bounded_rows is not None:
        if not isinstance(bounded_rows, int) or isinstance(bounded_rows, bool) or not (0 <= bounded_rows <= MAX_BOUNDED_ROWS):
            raise ValueError(f"bounded_rows must be a whole number from 0 to {MAX_BOUNDED_ROWS}")
        current["bounded_rows"] = bounded_rows
    state.set_section("prover", current)
    return current


_COLUMNS: ContextVar[dict[str, list[str]] | None] = ContextVar("kumosql_prover_columns", default=None)


@contextmanager
def use_columns(columns: dict[str, list[str]]):
    """Make ``current_schema()`` report exactly these columns (table name to column names) inside the block.

    For callers that hold their own table definitions, and tests: the rule that qualifies columns and the
    proof that checks it then read the same tables.
    """

    token = _COLUMNS.set({k.lower(): [c.lower() for c in v] for k, v in columns.items()})
    try:
        yield
    finally:
        _COLUMNS.reset(token)


def current_schema() -> ProverSchema:
    """Facts from the loaded project and the saved BigQuery catalog (nothing is fetched)."""

    override = _COLUMNS.get()
    if override is not None:
        return ProverSchema(columns=dict(override), table_count=len(override), sources={"given"})

    from . import bigquery_catalog, live_graph

    loaded = live_graph.loaded()
    pipeline = loaded["pipeline"] if loaded else None
    tables = bigquery_catalog.saved_tables()
    signature = (id(pipeline), len(tables))
    with _LOCK:
        if _CACHE.get("signature") == signature:
            return _CACHE["schema"]
    schema = from_pipeline(pipeline, tables) if pipeline is not None else from_pipeline(_Empty(), tables)
    with _LOCK:
        _CACHE.update(signature=signature, schema=schema, pipeline=pipeline)
    return schema


class _Empty:
    models: dict = {}
    source_schema: dict = {}


def status() -> dict:
    """What the solver can use right now, for the status line and Settings."""

    config = settings()
    schema = current_schema() if config["enabled"] else ProverSchema()
    return {
        **config,
        "available": z3 is not None,
        **schema.to_json(),
    }


def prove(old_sql: str, new_sql: str, *, timeout_ms: int | None = None, schema: ProverSchema | None = None, equivalences_enabled: bool = True, search_counterexample: bool = False, conditional: bool = False) -> SmtEquivalenceResult:
    """Prove two queries return the same rows, using the project's declared facts.

    ``search_counterexample`` also runs an unproven pair on databases built for it
    (needs declared column types) and returns any database that tells them apart.
    ``conditional`` retries an unproven pair under facts taken from the queries and returns
    ``PROVEN_CONDITIONALLY`` with the minimal conditions when they settle it.
    """

    from . import equivalences
    from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic

    facts = schema if schema is not None else current_schema()
    declared = equivalences.load() if equivalences_enabled else []
    used: list = []
    if declared:
        try:
            rewritten_old, used_old = equivalences.rewrite_sql(old_sql, declared, facts.columns or None)
            rewritten_new, used_new = equivalences.rewrite_sql(new_sql, declared, facts.columns or None)
            old_sql, new_sql = rewritten_old, rewritten_new
            used = [*used_old, *(i for i in used_new if i not in used_old)]
        except Exception:  # noqa: BLE001 - an unreadable query is the prover's to report
            used = []
    result = prove_equivalent_algebraic(
        old_sql,
        new_sql,
        schema=facts.columns or None,
        constraints=facts.constraints or None,
        types=facts.types or None,
        timeout_ms=timeout_ms if timeout_ms is not None else settings()["timeout_ms"],
        search_counterexample=search_counterexample,
        conditional=conditional,
    )
    if (facts.notes or used) and result.status in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
        wanted = [*facts.notes]
        if used:
            wanted.append("declared equivalences hold in the data: " + "; ".join(i.label for i in used))
        extra = tuple(n for n in wanted if n not in result.assumptions)
        if extra:
            import dataclasses

            result = dataclasses.replace(result, assumptions=tuple(result.assumptions) + extra)
    return result


_BOUNDED: dict = {}


def bounded_schema():
    """The saved BigQuery catalog as a schema for the bounded check (column types, REQUIRED, keys)."""

    from . import bigquery_catalog
    from kumo_sql_prover.bounded_equivalence import schema_from_bigquery

    tables = list(bigquery_catalog.saved_tables())
    signature = (len(tables), tuple(sorted(f"{p}.{d}.{t}" for p, d, t, _ in tables)))
    with _LOCK:
        if _BOUNDED.get("signature") == signature:
            return _BOUNDED["schema"]
    schema = schema_from_bigquery(tables)
    with _LOCK:
        _BOUNDED.update(signature=signature, schema=schema)
    return schema


def bounded(old_sql: str, new_sql: str, *, schema=None, timeout_ms: int | None = None) -> dict | None:
    """The bounded check of two queries as JSON: ``None`` when it is off or cannot run.

    ``status`` is ``bounded_equivalent`` (no counterexample within ``bound`` rows per table; not a proof),
    ``different`` (a counterexample, replayed on DuckDB before it is reported) or ``unknown``.
    """

    from kumo_sql_prover import bounded_equivalence as be

    rows = settings()["bounded_rows"]
    if rows <= 0 or z3 is None:
        return None
    try:
        import duckdb  # noqa: F401  - the replay needs it
    except ImportError:
        return None
    facts = schema if schema is not None else bounded_schema()
    if not facts.tables:
        return None
    limit = timeout_ms if timeout_ms is not None else settings()["timeout_ms"]
    result = be.check_bounded(old_sql, new_sql, facts, rows=rows, dialect="bigquery", timeout_ms=limit, budget_s=max(limit / 1000 * 3, 5))
    return bounded_json(result, facts)


def bounded_json(result, facts) -> dict:
    data = {"status": result.status.value, "label": result.label, "bound": result.bound, "reason": result.reason}
    if result.counterexample is not None:
        tables = {}
        for name, rows in result.counterexample.items():
            if not rows:
                continue
            columns = [c.name for c in facts.tables[name].columns]
            tables[name] = [{c: (None if v is None else str(v) if not isinstance(v, (int, float, bool)) else v) for c, v in zip(columns, row)} for row in rows]
        data["counterexample"] = {"tables": tables}
    return data
