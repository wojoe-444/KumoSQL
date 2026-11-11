"""Reuse what reading one Dataform file produced, so a restart or an edit does not read and parse every file again.

``load_sqlx_project`` reads each asset in two steps: a **first step** that depends only on the file's text and a few
project settings (it splits the file into its config block and SQL, works out which table the action writes, and notes the
column docs and unique keys a declaration states), and a **second step** that resolves ``ref()`` calls against the names
every file declared. Each step records what it does as a list of *events* (a diagnostic, a model, a table name it
declared, ...) instead of changing the project directly, and the loader then applies the events in order. That makes the
effect of one file a plain list that can be kept:

* the first step of a file is kept under the file's content hash and ``ctx1`` (a hash of the settings it reads);
* the second step is kept under the same hash and ``ctx2`` (``ctx1`` plus every declared name, the renamed targets and the
  JavaScript flags), so it is replayed only while nothing it resolves against moved.

A file whose size and modification time still match the last snapshot (:func:`kumosql.mcp_cache.snapshot`) is not read at all:
its saved hash is used. A file that differs is read once and parsed once; every other file replays. The replay and a cold
load run the same event code, so a project loaded either way is equal (``tests/test_load_memo.py`` compares them).

Entries are saved by :mod:`kumosql.mcp_cache` as JSON and decoded strictly here: an entry that does not decode is a miss.
JavaScript files are always read again (their exports and declarations feed every other file).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .pipeline_types import Model, PipelineDiagnostic, Target

#: bump when what a load step produces, or how an entry is saved, changes: entries of another format are never read
FORMAT = 1

#: the memo the running load uses (set around the call by the agent-facing server's session; ``None``: nothing is reused)
CURRENT: contextvars.ContextVar["LoadMemo | None"] = contextvars.ContextVar("kumosql_load_memo", default=None)

#: a file changed within this many seconds before the snapshot cannot be told from an unchanged one by size and time alone
RACY_NS = 2_000_000_000


class MemoError(ValueError):
    """A saved entry that is not what :func:`encode_events` writes."""


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def contexts(version: str, database: str, dataset: str, assertion_dataset: str, naming: Any, include_modules: Iterable[str]) -> str:
    """``ctx1``: everything but the file's text that the first step reads."""

    return digest(f"format={FORMAT}", f"kumosql={version}", database, dataset, assertion_dataset,
                  json.dumps(asdict(naming), sort_keys=True), json.dumps(sorted(include_modules)))


def second_context(ctx1: str, known: Mapping[str, list], renamed: Mapping[Target, tuple], flags: tuple) -> str:
    """``ctx2``: ``ctx1`` plus the names every file declared (what a ``ref()`` resolves against) and the loader's flags."""

    names = sorted((name, sorted((t.database, t.schema, t.name) for t in targets)) for name, targets in known.items())
    moved = sorted(((t.database, t.schema, t.name), list(logical)) for t, logical in renamed.items())
    return digest(ctx1, json.dumps(names), json.dumps(moved), json.dumps(list(flags)))


# ------------------------------------------------------------------ saving events as plain data


def _target(t: Target) -> list:
    return [t.database, t.schema, t.name]


def _untarget(value: object) -> Target:
    if not (isinstance(value, list) and len(value) == 3 and all(isinstance(v, str) for v in value)):
        raise MemoError("target")
    return Target(*value)


def _strs(value: object) -> tuple[str, ...]:
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise MemoError("string list")
    return tuple(value)


def _model(m: Model) -> dict:
    d = asdict(m)
    d["target"] = _target(m.target)
    d["declared_dependencies"] = [_target(t) for t in m.declared_dependencies]
    for name in ("masked_expressions", "tags", "non_null", "operations_sql", "config_reads", "config_reads_unread", "logical", "incremental_sql"):
        d[name] = list(d[name])
    d["unique_keys"] = [list(k) for k in m.unique_keys]
    return d


def _unmodel(d: object) -> Model:
    if not isinstance(d, dict):
        raise MemoError("model")
    try:
        path = d["path"]
        if not (isinstance(d["kind"], str) and isinstance(d["sql"], str) and (path is None or isinstance(path, str))):
            raise MemoError("model text")
        if not (isinstance(d["pre_operations"], int) and isinstance(d["disabled"], bool) and isinstance(d["has_output"], bool)):
            raise MemoError("model flags")
        keys = d["unique_keys"]
        if not (isinstance(keys, list) and all(isinstance(k, list) for k in keys)):
            raise MemoError("unique keys")
        deps = d["declared_dependencies"]
        if not isinstance(deps, list):
            raise MemoError("dependencies")
        return Model(
            _untarget(d["target"]), d["kind"], d["sql"], path, tuple(_untarget(t) for t in deps),
            _strs(d["masked_expressions"]), _strs(d["tags"]), _strs(d["non_null"]), tuple(_strs(k) for k in keys),
            _strs(d["operations_sql"]), d["pre_operations"], _strs(d["config_reads"]), _strs(d["config_reads_unread"]),
            _strs(d["logical"]), d["disabled"], d["has_output"], _strs(d["incremental_sql"]),
        )
    except (KeyError, TypeError) as exc:
        raise MemoError(f"model: {type(exc).__name__}") from exc


def _docs(found: Mapping) -> dict:
    return {column: dict(attributes) for column, attributes in found.items()}


def _undocs(value: object) -> dict:
    if not isinstance(value, dict):
        raise MemoError("docs")
    for column, attributes in value.items():
        if not (isinstance(column, str) and isinstance(attributes, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in attributes.items())):
            raise MemoError("docs entry")
    return value


def _unread(value: object) -> list[tuple[str, str, str]]:
    if not (isinstance(value, list) and all(isinstance(u, list) and len(u) == 3 and all(isinstance(x, str) for x in u) for u in value)):
        raise MemoError("unread")
    return [tuple(u) for u in value]


def encode_events(events: list[tuple]) -> list:
    out: list = []
    for event in events:
        op = event[0]
        if op == "diag":
            out.append(["diag", event[1].model, event[1].code, event[1].message])
        elif op == "model":
            out.append(["model", _model(event[1])])
        elif op == "known":
            out.append(["known", event[1], _target(event[2])])
        elif op == "renamed":
            out.append(["renamed", _target(event[1]), list(event[2])])
        elif op == "computed":
            out.append(["computed"])
        elif op == "source":
            out.append(["source", _target(event[1])])
        elif op == "docs":
            out.append(["docs", event[1], _docs(event[2]), [list(u) for u in event[3]], event[4]])
        elif op == "source_keys":
            out.append(["source_keys", event[1], [list(k) for k in event[2]]])
        elif op == "read_output":
            out.append(["read_output", event[1], _target(event[2])])
        else:
            raise MemoError(f"event {op}")
    return out


def decode_events(value: object) -> list[tuple]:
    if not isinstance(value, list):
        raise MemoError("events")
    out: list[tuple] = []
    try:
        for item in value:
            if not (isinstance(item, list) and item and isinstance(item[0], str)):
                raise MemoError("event")
            op, rest = item[0], item[1:]
            if op == "diag" and len(rest) == 3 and all(isinstance(r, str) for r in rest):
                out.append(("diag", PipelineDiagnostic(*rest)))
            elif op == "model" and len(rest) == 1:
                out.append(("model", _unmodel(rest[0])))
            elif op == "known" and len(rest) == 2 and isinstance(rest[0], str):
                out.append(("known", rest[0], _untarget(rest[1])))
            elif op == "renamed" and len(rest) == 2:
                out.append(("renamed", _untarget(rest[0]), _strs(rest[1])))
            elif op == "computed" and not rest:
                out.append(("computed",))
            elif op == "source" and len(rest) == 1:
                out.append(("source", _untarget(rest[0])))
            elif op == "docs" and len(rest) == 4 and isinstance(rest[0], str) and isinstance(rest[3], str):
                out.append(("docs", rest[0], _undocs(rest[1]), _unread(rest[2]), rest[3]))
            elif op == "source_keys" and len(rest) == 2 and isinstance(rest[0], str) and isinstance(rest[1], list):
                out.append(("source_keys", rest[0], tuple(_strs(k) for k in rest[1])))
            elif op == "read_output" and len(rest) == 2 and isinstance(rest[0], str):
                out.append(("read_output", rest[0], _untarget(rest[1])))
            else:
                raise MemoError("event shape")
    except (TypeError, ValueError) as exc:
        raise MemoError(f"events: {type(exc).__name__}") from exc
    return out


def encode_asset(asset: tuple | None) -> list | None:
    """The first step's result ``(relative, sections, config, kind, target)`` as JSON data (``None`` for a declaration or an error)."""

    if asset is None:
        return None
    relative, sections, config, kind, target = asset
    return [relative, [[k, s] for k, s in sections], config, kind, _target(target)]


def decode_asset(value: object) -> tuple | None:
    if value is None:
        return None
    try:
        relative, sections, config, kind, target = value
        if not (isinstance(relative, str) and isinstance(config, str) and isinstance(kind, str) and isinstance(sections, list)):
            raise MemoError("asset")
        pairs = []
        for pair in sections:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
                raise MemoError("section")
            pairs.append((pair[0], pair[1]))
        return relative, pairs, config, kind, _untarget(target)
    except (TypeError, ValueError) as exc:
        raise MemoError(f"asset: {type(exc).__name__}") from exc


# ------------------------------------------------------------------ the memo


class LoadMemo:
    """Saved effects of the files of the last load, the effects of this one, and counts of what was read, parsed or replayed.

    ``saved`` maps a file's memo key (its path below the project, or ``path#name`` for an asset a JavaScript file
    publishes) to ``{"sha", "c1", "s1": {"events", "asset"}, "c2", "s2"}``. ``files`` is the last snapshot,
    ``{absolute path: (mtime_ns, size, sha256 of the bytes)}``, taken at ``taken_ns``: a file whose size and time still
    match, and whose time is older than ``taken_ns`` by more than :data:`RACY_NS`, is known by its saved hash without reading.
    All counters are per instance, so concurrent loads (and tests) share nothing.
    """

    def __init__(self, saved: Mapping[str, Any] | None = None, files: Mapping[str, tuple[int, int, str]] | None = None, taken_ns: int = 0) -> None:
        self.saved = dict(saved or {})
        self.files = files if files is not None else {}
        self.taken_ns = taken_ns
        self._lock = threading.Lock()
        #: what this load produced, by memo key, for saving
        self.entries: dict[str, dict] = {}
        #: files read from disk by the load, files whose first step ran (parsed), first steps replayed
        self.read: list[str] = []
        self.parsed: list[str] = []
        self.replayed: list[str] = []
        #: assets whose second step (``ref()`` resolution) ran, and ones replayed
        self.resolved: list[str] = []
        self.resolved_replayed: list[str] = []
        self.finished = False

    # -- which files need reading

    def trusted_sha(self, path: Path) -> str | None:
        """The saved hash of ``path`` when its size and time match the snapshot and are not too recent to trust, else ``None``."""

        known = self.files.get(os.fspath(path))
        if known is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if (stat.st_mtime_ns, stat.st_size) != known[:2] or stat.st_mtime_ns > self.taken_ns - RACY_NS:
            return None
        return known[2]

    def note_read(self, key: str) -> None:
        with self._lock:
            self.read.append(key)

    # -- steps

    def first(self, key: str, sha: str, ctx1: str) -> tuple[list[tuple], tuple | None] | None:
        """The saved first step of ``key`` for this content and settings as ``(events, asset)``, or ``None`` (a miss, or undecodable)."""

        entry = self.saved.get(key)
        if isinstance(entry, dict) and entry.get("sha") == sha and entry.get("c1") == ctx1 and isinstance(entry.get("s1"), dict):
            try:
                found = decode_events(entry["s1"].get("events")), decode_asset(entry["s1"].get("asset"))
            except MemoError:
                return None
            with self._lock:
                self.replayed.append(key)
                self.entries[key] = {"sha": sha, "c1": ctx1, "s1": entry["s1"]}
            return found
        return None

    def store_first(self, key: str, sha: str | None, ctx1: str, events: list[tuple], asset: tuple | None) -> None:
        with self._lock:
            self.parsed.append(key)
            if sha is not None:
                self.entries[key] = {"sha": sha, "c1": ctx1, "s1": {"events": encode_events(events), "asset": encode_asset(asset)}}

    def second(self, key: str, sha: str, ctx2: str) -> list[tuple] | None:
        entry = self.saved.get(key)
        if isinstance(entry, dict) and entry.get("sha") == sha and entry.get("c2") == ctx2 and "s2" in entry:
            try:
                events = decode_events(entry["s2"])
            except MemoError:
                return None
            with self._lock:
                self.resolved_replayed.append(key)
                if key in self.entries:
                    self.entries[key].update({"c2": ctx2, "s2": entry["s2"]})
            return events
        return None

    def store_second(self, key: str, sha: str | None, ctx2: str, events: list[tuple]) -> None:
        with self._lock:
            self.resolved.append(key)
            if sha is not None and key in self.entries:
                self.entries[key].update({"c2": ctx2, "s2": encode_events(events)})
