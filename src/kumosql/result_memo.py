"""Reuse the session results that are costly to rebuild on every start: table profiles, column types and the expression index.

:mod:`kumosql.trace_memo` replays the column trace of a model whose own definition and everything upstream of it are
unchanged. The results here are keyed the same way, so they are replayed for exactly those models and computed again for
an edited model and the models downstream of it:

* **profiles** (``Session.profiles()``: grain, row scope, column meanings): per model, keyed by the model's closure key (its
  definition, everything upstream, the project-wide facts) and by the grain declarations the profile was made with;
* **column types** (``Session.column_types()``): per model, keyed by the closure key, the schema snapshot's rows and the
  names of the tables typed before it (a name two tables share is found as neither, so what came earlier matters);
* **the expression index** (``find_existing expression=...``): per model, keyed by a hash of the model's own SQL, which
  :func:`kumosql.expression_search.build_index` already supports.

What is *not* kept, and why: the downstream map, the unused-column list and the dependency order come from the finished
analysis in well under a millisecond per hundred models, so keeping them would add a way to be stale for no gain; roles
are whole-project and cost a few milliseconds.

Everything is plain JSON decoded strictly: an entry that does not decode, or whose key does not match, is a miss and the
result is computed. Nothing is replayed when the trace memo cannot be (a project with writes between models, a dependency
cycle, a trace cut short by a time limit): those models have no closure key.

Counters (per instance) say what was computed and what was replayed, so a test or the server can show it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Mapping

from .googlesql_types import GType, StructField
from .table_profile import AttributeMeaning, Grain, RowScope, TableProfile

#: bump when what a profile, a type or an index part contains changes: saved entries of another format are never read
FORMAT = 1

SECTIONS = ("profiles", "types", "expression")


class MemoError(ValueError):
    """A saved entry that is not what the encoders here write."""


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


# ------------------------------------------------------------------ profiles


def _strings(value: object) -> tuple[str, ...]:
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise MemoError("string list")
    return tuple(value)


def _optional(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise MemoError("text")
    return value


def decode_profile(value: object) -> TableProfile:
    """The profile saved by ``TableProfile.to_json``, validated whole; raises :class:`MemoError` on anything else."""

    try:
        if not isinstance(value, dict):
            raise MemoError("profile")
        grain, scope, attributes = value["grain"], value["row_scope"], value["attributes"]
        if not (isinstance(grain, dict) and isinstance(scope, dict) and isinstance(attributes, list) and isinstance(value["table"], str)):
            raise MemoError("profile parts")
        if not isinstance(grain["status"], str) or not isinstance(scope["comparable"], bool) or not isinstance(value["complete"], bool):
            raise MemoError("profile flags")
        items = []
        for a in attributes:
            if not (isinstance(a, dict) and isinstance(a["column"], str) and isinstance(a["status"], str) and isinstance(a["equality_only"], bool)):
                raise MemoError("attribute")
            items.append(AttributeMeaning(a["column"], _optional(a["meaning"]), a["status"], _optional(a["reason"]),
                                          _strings(a["sources"]), _optional(a["transform"]), a["equality_only"]))
        return TableProfile(
            value["table"],
            Grain(_strings(grain["keys"]), grain["status"], _optional(grain["reason"])),
            tuple(items),
            RowScope(_strings(scope["filters"]), scope["comparable"], _optional(scope["reason"])),
            value["complete"],
        )
    except (KeyError, TypeError) as exc:
        raise MemoError(f"profile: {type(exc).__name__}") from exc


def profile_is_cacheable(profile: TableProfile) -> bool:
    """A profile that records an exception (``profile_error``) is computed again next time, never saved."""

    texts = [profile.grain.reason, profile.row_scope.reason, *(a.reason for a in profile.attributes)]
    return not any(t and t.startswith("profile_error") for t in texts)


# ------------------------------------------------------------------ column types


def encode_type(t: GType | None) -> Any:
    if t is None:
        return None
    return [t.kind, encode_type(t.element), [[f.name, encode_type(f.type)] for f in t.fields]]


def decode_type(value: object, depth: int = 0) -> GType | None:
    if value is None:
        return None
    if depth > 40 or not (isinstance(value, list) and len(value) == 3 and isinstance(value[0], str) and isinstance(value[2], list)):
        raise MemoError("type")
    fields = []
    for item in value[2]:
        if not (isinstance(item, list) and len(item) == 2 and (item[0] is None or isinstance(item[0], str))):
            raise MemoError("struct field")
        fields.append(StructField(item[0], decode_type(item[1], depth + 1)))
    return GType(value[0], decode_type(value[1], depth + 1), tuple(fields))


def encode_columns(columns: list[tuple[str, GType | None, bool | None]] | None) -> dict:
    """One model's typed columns ``(name, type, null flag)``, or ``None`` for a model that could not be typed."""

    if columns is None:
        return {"cols": None}
    return {"cols": [[name, encode_type(kind), flag] for name, kind, flag in columns]}


def decode_columns(value: object) -> list[tuple[str, GType | None, bool | None]] | None:
    if not (isinstance(value, dict) and "cols" in value):
        raise MemoError("columns")
    cols = value["cols"]
    if cols is None:
        return None
    if not isinstance(cols, list):
        raise MemoError("column list")
    out = []
    for item in cols:
        if not (isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) and (item[2] is None or isinstance(item[2], bool))):
            raise MemoError("column")
        out.append((item[0], decode_type(item[1]), item[2]))
    return out


# ------------------------------------------------------------------ the expression index


def valid_part(part: object) -> bool:
    """Whether a saved expression-index part has exactly the shape :func:`kumosql.expression_search.model_part` writes."""

    try:
        if not (isinstance(part, dict) and isinstance(part["hash"], str) and isinstance(part["not_normalized"], int) and not isinstance(part["not_normalized"], bool)):
            return False
        if not (isinstance(part["aliases"], list) and all(isinstance(a, str) for a in part["aliases"]) and isinstance(part["rows"], list)):
            return False
        for row in part["rows"]:
            if not (isinstance(row, list) and len(row) == 5 and isinstance(row[0], str) and isinstance(row[1], str) and isinstance(row[2], str)
                    and (row[3] is None or isinstance(row[3], str))):
                return False
            ladder = row[4]
            if ladder is None:
                continue
            if not (isinstance(ladder, dict) and isinstance(ladder.get("b"), list) and isinstance(ladder.get("d"), str)
                    and (ladder.get("s") is None or isinstance(ladder.get("s"), str)) and isinstance(ladder.get("g"), str)):
                return False
            if not all(isinstance(b, list) and len(b) == 3 and all(isinstance(x, str) for x in b) for b in ladder["b"]):
                return False
        return True
    except (KeyError, TypeError):
        return False


# ------------------------------------------------------------------ the memo


class ResultMemo:
    """Saved results of the last build, the results of this one, and counters of what was computed or replayed.

    ``saved`` is ``{section: {token: payload}}`` (a section or entry of another shape is dropped). A token is a hash of
    everything the result depends on, so a lookup that finds it is a hit. ``entries`` collects what this build used or
    computed: it is what :mod:`kumosql.mcp_cache` saves, so entries no model uses any more are not carried on.
    """

    def __init__(self, saved: Mapping[str, Any] | None = None) -> None:
        self.saved: dict[str, dict] = {name: {} for name in SECTIONS}
        for name in SECTIONS:
            found = (saved or {}).get(name)
            if isinstance(found, dict):
                self.saved[name] = {k: v for k, v in found.items() if isinstance(k, str)}
        self.entries: dict[str, dict] = {name: {} for name in SECTIONS}
        self.computed: dict[str, list[str]] = {name: [] for name in SECTIONS}
        self.replayed: dict[str, list[str]] = {name: [] for name in SECTIONS}
        self._lock = threading.Lock()

    def token(self, pipeline: Any, key: str, *context: str) -> str | None:
        """The token of ``key``'s result under ``context``, or ``None`` when the model has no closure key (nothing is reused)."""

        trace = getattr(pipeline, "trace_memo", None)
        closures = getattr(trace, "closures", None)
        closure = closures.get(key) if closures else None
        if closure is None:
            return None
        return digest(f"format={FORMAT}", key, closure, *context)

    def lookup(self, section: str, token: str | None) -> Any | None:
        if token is None:
            return None
        return self.saved[section].get(token)

    def replayed_one(self, section: str, token: str, key: str) -> None:
        with self._lock:
            self.replayed[section].append(key)
            self.entries[section][token] = self.saved[section][token]

    def computed_one(self, section: str, token: str | None, key: str, payload: Any | None) -> None:
        with self._lock:
            self.computed[section].append(key)
            if token is not None and payload is not None:
                self.entries[section][token] = payload

    def counts(self) -> dict:
        return {f"{name}_{kind}": len(getattr(self, kind)[name]) for name in SECTIONS for kind in ("computed", "replayed")}

    def to_json(self) -> dict:
        return {name: self.entries[name] for name in SECTIONS}


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)
