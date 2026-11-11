"""Reuse the column trace of models whose inputs did not change (the slow part of ``Pipeline`` analysis).

Tracing every output column of every model is nearly all of the time analysis takes. What one model's trace
produces depends only on that model's own definition, on the schemas of the tables it reads (so on the
definitions of everything upstream of it) and on a few project-wide facts (the set of table names, declared
source schemas, procedures and table functions, the lineage time limits, the sqlglot and KumoSQL versions).
:class:`TraceMemo` hashes exactly those into a *closure key* per model. A model whose key matches a saved one
is not traced again: its saved result is replayed into the analysis at the place the trace would have put it,
so the analysis equals a cold one (same records, same order, same diagnostics).

Only plain data is saved (names, strings, lists; see :func:`encode_delta`), as JSON the caller stores. A saved
entry that does not decode is a miss. The analysis falls back to tracing, never to a guess, when

* a model writes into another model's table (``MERGE`` / ``INSERT`` operations) or has an ``INSERT`` with no
  column list: those couple models in ways the key does not cover, so nothing is reused for the project;
* a model reads one that is not traced before it (a dependency cycle);
* a trace hit a time limit (``lineage_skipped``): that model and everything downstream of it is traced again.

``Pipeline.trace_memo`` is ``None`` unless a caller (the agent-facing server) sets it, and then nothing changes.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from .pipeline_types import ColumnFilter, ColumnLineage, ColumnRef, PipelineDiagnostic

#: bump when what a trace produces, or how it is saved, changes: saved entries of another version are never read
FORMAT = 1


class MemoError(ValueError):
    """A saved entry that is not what :func:`encode_delta` writes."""


# ------------------------------------------------------------------ saving a trace as plain data


def _ref(ref: ColumnRef) -> list:
    return [ref.table, ref.column, list(ref.path)]


def _refs(refs: Any) -> list:
    return [_ref(r) for r in sorted(refs)]


def _unref(value: object) -> ColumnRef:
    if not (isinstance(value, list) and len(value) == 3 and isinstance(value[0], str) and isinstance(value[1], str)):
        raise MemoError("column reference")
    path = value[2]
    if not (isinstance(path, list) and all(isinstance(p, str) for p in path)):
        raise MemoError("column path")
    return ColumnRef(value[0], value[1], tuple(path))


def _unrefs(value: object) -> frozenset[ColumnRef]:
    if not isinstance(value, list):
        raise MemoError("column list")
    return frozenset(_unref(v) for v in value)


def _strings(value: object) -> list[str]:
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise MemoError("string list")
    return value


def _filters(filters: tuple[ColumnFilter, ...] | None) -> list | None:
    if filters is None:
        return None
    return [[f.kind, f.condition, f.scope, [_ref(r) for r in f.columns], f.effect, f.columns_complete] for f in filters]


def _unfilters(value: object) -> tuple[ColumnFilter, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise MemoError("filters")
    found = []
    for item in value:
        if not (isinstance(item, list) and len(item) == 6 and all(isinstance(item[i], str) for i in (0, 1, 2, 4)) and isinstance(item[5], bool)):
            raise MemoError("filter")
        if not isinstance(item[3], list):
            raise MemoError("filter columns")
        found.append(ColumnFilter(item[0], item[1], item[2], tuple(_unref(r) for r in item[3]), item[4], item[5]))
    return tuple(found)


def encode_delta(delta: Mapping[str, Any]) -> dict:
    """One model's trace as JSON-ready data (the inverse of :func:`decode_delta`)."""

    return {
        "records": [
            [_ref(r.column), _refs(r.sources), r.status, r.transform, r.reason, _filters(r.filters)] for r in delta["records"]
        ],
        "direct": [[_ref(ref), _refs(sources)] for ref, sources in delta["direct"]],
        "outputs": None if delta["outputs"] is None else list(delta["outputs"]),
        "schema": delta["schema"],
        "consumed": None if delta["consumed"] is None else _refs(delta["consumed"]),
        "conditions": None if delta["conditions"] is None else _refs(delta["conditions"]),
        "star": None if delta["star"] is None else sorted(delta["star"]),
        "template": None if delta["template"] is None else sorted(delta["template"]),
        "opaque": sorted(delta["opaque"]),
        "diagnostics": [[d.model, d.code, d.message] for d in delta["diagnostics"]],
    }


def decode_delta(value: object) -> dict:
    """The trace saved by :func:`encode_delta`, validated whole; raises :class:`MemoError` on anything else."""

    if not isinstance(value, dict):
        raise MemoError("entry")
    try:
        records = []
        for row in value["records"]:
            ref, sources, status, transform, reason, filters = row
            if not (isinstance(status, str) and isinstance(transform, str) and (reason is None or isinstance(reason, str))):
                raise MemoError("record")
            records.append(ColumnLineage(_unref(ref), _unrefs(sources), status, transform, reason, _unfilters(filters)))
        direct = [(_unref(ref), _unrefs(sources)) for ref, sources in value["direct"]]
        outputs = value["outputs"]
        if outputs is not None:
            outputs = tuple(_strings(outputs))
        schema = value["schema"]
        if schema is not None and not (isinstance(schema, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in schema.items())):
            raise MemoError("schema")
        diagnostics = []
        for model, code, message in value["diagnostics"]:
            if not all(isinstance(x, str) for x in (model, code, message)):
                raise MemoError("diagnostic")
            diagnostics.append(PipelineDiagnostic(model, code, message))
        return {
            "records": records,
            "direct": direct,
            "outputs": outputs,
            "schema": schema,
            "consumed": None if value["consumed"] is None else _unrefs(value["consumed"]),
            "conditions": None if value["conditions"] is None else _unrefs(value["conditions"]),
            "star": None if value["star"] is None else set(_strings(value["star"])),
            "template": None if value["template"] is None else set(_strings(value["template"])),
            "opaque": set(_strings(value["opaque"])),
            "diagnostics": diagnostics,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoError(f"entry: {type(exc).__name__}") from exc


# ------------------------------------------------------------------ keys


def _digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def definition_digest(model: Any) -> str:
    """A hash of everything the project says about one model (its SQL, config, target, path)."""

    return _digest(json.dumps(asdict(model), sort_keys=True, default=str))


def project_fingerprint(pipeline: Any, schema: Mapping[str, Any], special_texts: list[str]) -> str:
    """A hash of the project-wide facts every model's trace depends on (see the module text).

    ``schema`` is the table schema known before any model is traced; ``special_texts`` are the models' texts that
    define procedures or table functions.
    """

    import sqlglot

    from . import __version__, lineage_limits, schema_fetch

    return _digest(
        f"format={FORMAT}",
        f"kumosql={__version__}",
        f"sqlglot={sqlglot.__version__}",
        f"limits={lineage_limits.cache_tag()}",
        f"fetch={schema_fetch.cache_tag()}",
        json.dumps(sorted(pipeline.models), ensure_ascii=False),
        json.dumps({key: asdict(target) for key, target in sorted(pipeline.sources.items())}, sort_keys=True),
        json.dumps([pipeline.default_project, pipeline.default_dataset, pipeline.default_location]),
        json.dumps(schema, sort_keys=True, default=str),
        json.dumps(sorted(special_texts), ensure_ascii=False),
    )


# ------------------------------------------------------------------ the memo


@dataclass
class TraceState:
    """The containers of one analysis run that tracing a model fills (``Pipeline`` hands them over)."""

    pipeline: Any
    order: list[str]
    upstream: dict[str, set[str]]
    records: dict
    direct: dict
    outputs: dict
    schema: dict
    consumed: dict
    conditions: dict
    star_branch_tables: dict
    template_reads: dict
    diagnostics: list
    opaque_readers_of: set
    sqlglot_schema: Any
    spellings: dict
    add_table: Callable[[Any, str, dict], None]


class TraceMemo:
    """Saved traces from an earlier run, the traces of this one, and counts of what was traced or reused.

    ``previous`` maps a model key to ``{"closure": key, "delta": saved trace}`` from the run saved under
    ``previous_fingerprint``. ``on_finish(fingerprint, entries)`` is called when a run that traced every
    model it could has finished, with the entries to save. All counters are per instance, so concurrent
    analyses (and tests) do not share any state.
    """

    def __init__(
        self,
        previous: Mapping[str, Any] | None = None,
        previous_fingerprint: str | None = None,
        on_finish: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.previous = dict(previous or {})
        self.previous_fingerprint = previous_fingerprint
        self.on_finish = on_finish
        self._lock = threading.Lock()
        #: models traced in the run (a cache miss), and models whose saved trace was replayed
        self.traced: list[str] = []
        self.reused: list[str] = []
        #: ``"idle"`` until the trace starts, ``"tracing"`` while it runs, ``"done"``, or ``"off"`` when nothing may be reused
        self.state = "idle"
        self.total = 0
        self.done = 0
        self.entries: dict[str, dict] = {}

    def percent(self) -> int:
        """How much of the trace is done, 0 to 100 (100 only when finished)."""

        if self.state == "done":
            return 100
        return min(99, (100 * self.done) // self.total) if self.total else 0

    def start(self, state: TraceState, special_texts: list[str], eligible: bool) -> "_Replay":
        """Begin a run. ``eligible`` is False when the project has writes between models (nothing is reused or saved)."""

        fingerprint = project_fingerprint(state.pipeline, state.schema, special_texts)
        with self._lock:
            self.traced, self.reused, self.entries, self.done = [], [], {}, 0
            self.total = len(state.order)
            reuse = eligible and fingerprint == self.previous_fingerprint
            self.state = "tracing" if eligible else "off"
        return _Replay(self, state, fingerprint, reuse, eligible)


class _Replay:
    """What the analysis loop calls: ``enter(key)`` before tracing a model (True: replayed, skip it) and ``leave()`` after the last."""

    def __init__(self, memo: TraceMemo, state: TraceState, fingerprint: str, reuse: bool, eligible: bool) -> None:
        self.memo, self.state, self.fingerprint, self.reuse, self.eligible = memo, state, fingerprint, reuse, eligible
        self.closure: dict[str, str | None] = {}
        self.pending: tuple | None = None

    # -- keys

    def _closure_of(self, key: str) -> str | None:
        pipeline = self.state.pipeline
        parts = [definition_digest(pipeline.models[key])]
        for parent in sorted(self.state.upstream.get(key, ())):
            if parent == key:
                continue
            if parent in pipeline.models:
                if parent not in self.closure or self.closure[parent] is None:
                    return None  # not traced before this model (a cycle), or itself not reusable
                parts.append(f"{parent}={self.closure[parent]}")
            else:
                parts.append(f"{parent}=source")
        return _digest(*parts)

    # -- the loop's calls

    def enter(self, key: str, traceable: bool) -> bool:
        self._flush()
        state = self.state
        closure = self._closure_of(key) if self.eligible else None
        self.closure[key] = closure
        if not traceable:
            return False
        entry = self.memo.previous.get(key) if self.reuse and closure is not None else None
        if entry is not None and entry.get("closure") == closure:
            try:
                delta = decode_delta(entry.get("delta"))
            except MemoError:
                delta = None
            if delta is not None:
                self._apply(key, delta)
                with self.memo._lock:
                    self.memo.reused.append(key)
                    self.memo.done += 1
                self.memo.entries[key] = entry
                return True
        with self.memo._lock:
            self.memo.traced.append(key)
        self.pending = (key, closure, len(state.diagnostics), set(state.opaque_readers_of), state.schema.get(key))
        return False

    def leave(self) -> None:
        self._flush()
        with self.memo._lock:
            self.memo.state = "done" if self.eligible else "off"
            self.memo.done = self.memo.total
        if self.eligible and self.memo.on_finish is not None:
            self.memo.on_finish(self.fingerprint, dict(self.memo.entries))

    # -- capturing and replaying one model

    def _flush(self) -> None:
        """Record what tracing the model entered last produced (called when the next model starts)."""

        if self.pending is None:
            return
        key, closure, diagnostics_before, opaque_before, schema_before = self.pending
        self.pending = None
        state = self.state
        with self.memo._lock:
            self.memo.done += 1
        new_diagnostics = state.diagnostics[diagnostics_before:]
        if any(d.code == "lineage_skipped" for d in new_diagnostics):
            self.closure[key] = None  # cut short by a time limit: the result is not a function of the inputs
            return
        if closure is None:
            return
        names = list(dict.fromkeys(ColumnRef(key, name) for name in (*state.outputs.get(key, ()), "*")))
        schema = state.schema.get(key)
        delta = {
            "records": [state.records[ref] for ref in names if ref in state.records],
            "direct": [(ref, state.direct[ref]) for ref in names if ref in state.direct],
            "outputs": state.outputs.get(key),
            "schema": dict(schema) if schema is not None and schema is not schema_before else None,
            "consumed": state.consumed.get(key),
            "conditions": state.conditions.get(key),
            "star": state.star_branch_tables.get(key),
            "template": state.template_reads.get(key),
            "opaque": state.opaque_readers_of - opaque_before,
            "diagnostics": new_diagnostics,
        }
        self.memo.entries[key] = {"closure": closure, "delta": encode_delta(delta)}

    def _apply(self, key: str, delta: dict) -> None:
        state = self.state
        for record in delta["records"]:
            state.records[record.column] = record
        for ref, sources in delta["direct"]:
            state.direct[ref] = sources
        if delta["outputs"] is not None:
            state.outputs[key] = delta["outputs"]
        if delta["schema"] is not None:
            state.schema[key] = delta["schema"]
            state.add_table(state.sqlglot_schema, key, delta["schema"])
            for spelled in sorted(state.spellings.get(key, ())):
                state.add_table(state.sqlglot_schema, spelled, delta["schema"])
        if delta["consumed"] is not None:
            state.consumed[key] = delta["consumed"]
        if delta["conditions"] is not None:
            state.conditions[key] = delta["conditions"]
        if delta["star"] is not None:
            state.star_branch_tables[key].update(delta["star"])
        if delta["template"] is not None:
            state.template_reads[key] = set(delta["template"])
        state.opaque_readers_of.update(delta["opaque"])
        state.diagnostics.extend(delta["diagnostics"])
