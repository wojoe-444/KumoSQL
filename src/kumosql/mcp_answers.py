"""Bounded answers and the tool registry of the agent-facing server (``python -m kumosql mcp``).

An agent pays for every byte it reads, so an answer is small by construction. A tool builds an
:class:`Answer`: a one-sentence summary, a few core fields, counts, lists, the things it could not
see (each with a reason) and the next step. :func:`render` turns it into one JSON object that fits
``max_bytes`` (about 2 KB by default): when it does not fit, the longest lists are cut and the
answer says how many items it showed, how many exist, and which argument asks for the rest.

Adding a tool is one function in a module imported by :mod:`kumosql.mcp_server`::

    @tool("find_tables", "Which table should I use?", "...", {"need": {"type": "string"}}, required=("need",))
    def find_tables(session, args):
        answer = Answer("...")
        return answer

Every tool also accepts ``max_bytes`` (the size bound) and ``offset`` (where the first list starts),
so each answer can be widened without a new tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_BYTES = 2000
MIN_BYTES = 1000
MAX_BYTES = 20000
MAX_CLIP = 160

COMMON_PROPERTIES = {
    "max_bytes": {
        "type": "integer",
        "description": f"Size bound of the answer in bytes (default {DEFAULT_BYTES}, from {MIN_BYTES} to {MAX_BYTES}). Raise it to see more of a cut list.",
    },
    "offset": {"type": "integer", "description": "Skip this many items of the answer's main list (paging)."},
}


class ToolError(Exception):
    """A question the tool cannot answer as asked. The answer says why and what to do instead."""

    def __init__(self, message: str, next_steps: list[str] | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.message, self.next_steps, self.extra = message, list(next_steps or []), extra


@dataclass
class Listing:
    """A list inside an answer. ``items`` is the page shown from ``offset``; ``total`` counts all of them."""

    items: list
    total: int | None = None
    offset: int = 0
    #: the list that ``offset`` pages
    primary: bool = False
    #: the fewest items kept when the answer must shrink (explicit unknowns and next steps keep one)
    keep: int = 0
    #: ``item -> (shown, all)``: how many underlying things one item holds, so a cut can say how many of them it hid
    unit: Callable[[Any], tuple[int, int]] | None = None
    unit_name: str = "items"

    def __post_init__(self) -> None:
        if self.total is None:
            self.total = self.offset + len(self.items)


@dataclass
class Answer:
    summary: str
    fields: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    lists: dict[str, Listing] = field(default_factory=dict)

    def add_list(self, name: str, items: list, *, total: int | None = None, offset: int = 0, primary: bool = False, keep: int = 0, unit: Callable[[Any], tuple[int, int]] | None = None, unit_name: str = "items") -> None:
        if items or total:
            self.lists[name] = Listing(list(items), total, offset, primary, keep, unit, unit_name)

    def unknown(self, what: str, reason: str) -> None:
        """Something the tool could not determine. It stays in the answer, with its reason."""

        entry = {"what": what, "reason": reason}
        listing = self.lists.setdefault("unknown", Listing([], 0, keep=1))
        listing.items.append(entry)
        listing.total = len(listing.items)

    def next(self, step: str) -> None:
        listing = self.lists.setdefault("next", Listing([], 0, keep=1))
        listing.items.append(step)
        listing.total = len(listing.items)


def size(value: object) -> int:
    """Bytes of ``value`` as the compact JSON the server sends."""

    return len(dump(value).encode("utf-8"))


def dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def clip(text: object, limit: int = MAX_CLIP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(limit - 3, 1)] + "..."


def render(answer: Answer, *, max_bytes: int = DEFAULT_BYTES) -> dict:
    """The answer as one JSON-ready dict of at most about ``max_bytes`` bytes, saying what it left out."""

    shown = {name: len(listing.items) for name, listing in answer.lists.items()}

    def build(with_more: bool = True) -> dict:
        out: dict[str, Any] = {"answer": answer.summary}
        out.update(answer.fields)
        if answer.counts:
            out["counts"] = answer.counts
        for name, listing in answer.lists.items():
            if name not in ("unknown", "next"):
                _put(out, name, listing, shown[name])
        for name in ("unknown", "next"):
            if name in answer.lists:
                _put(out, name, answer.lists[name], shown[name])
        if with_more:
            more = _more(answer, shown, full, max_bytes)
            if more:
                out["more"] = more
        return out

    full = 0
    full = size(build(with_more=False))
    while size(build()) > max_bytes:
        # cut the plain lists first; unknowns and next steps go only when nothing else is left to cut
        candidates = [name for name, listing in answer.lists.items() if shown[name] > 0 and not listing.keep]
        candidates = candidates or [name for name, listing in answer.lists.items() if shown[name] > listing.keep]
        if not candidates:
            break
        name = max(candidates, key=lambda n: (size(answer.lists[n].items[: shown[n]]), n))
        shown[name] -= 1
    out = build()
    if size(out) > max_bytes:
        out = _clip_strings(out, 100)
    if size(out) > max_bytes:
        out = {key: value for key, value in out.items() if key in ("answer", "counts", "unknown", "next", "more") or key.endswith("_shown")}
        out = _clip_strings(out, 60)
    return out


def _put(out: dict, name: str, listing: Listing, count: int) -> None:
    if not listing.items and not listing.total:
        return
    if count:
        out[name] = listing.items[:count]
    complete = count == len(listing.items) and listing.total == listing.offset + len(listing.items) and not listing.offset
    if not complete:
        first = listing.offset + 1 if count else listing.offset
        text = f"{first}-{listing.offset + count} of {listing.total}" if count else f"0 of {listing.total}"
        if listing.unit is not None and count < len(listing.items):
            seen = sum(listing.unit(item)[0] for item in listing.items[:count])
            every = sum(listing.unit(item)[1] for item in listing.items)
            text += f" ({seen} of {every} {listing.unit_name})"
        out[f"{name}_shown"] = text


def _more(answer: Answer, shown: dict[str, int], full: int, max_bytes: int) -> str:
    parts = []
    for name, listing in answer.lists.items():
        if listing.primary and listing.offset + shown[name] < listing.total and (shown[name] or listing.offset):
            parts.append(f"offset={listing.offset + shown[name]} for the next {name}")
    cut = any(shown[name] < len(listing.items) for name, listing in answer.lists.items())
    if cut:
        parts.append(f"max_bytes={min(MAX_BYTES, max(full + 100, max_bytes + 1))} for everything on this page")
    if not parts:
        return ""
    return "Ask again with " + ", or ".join(parts) + "."


def _clip_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, list):
        return [_clip_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _clip_strings(item, limit) for key, item in value.items()}
    return value


# ------------------------------------------------------------------ the registry


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    properties: dict[str, dict]
    required: tuple[str, ...]
    run: Callable[[Any, dict], Answer]
    #: True for a tool that can reach BigQuery when asked to (it never does so by default)
    open_world: bool = False

    def definition(self) -> dict:
        """The ``tools/list`` entry: read-only; closed-world unless the tool can reach BigQuery on request."""

        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": {**self.properties, **COMMON_PROPERTIES},
                "required": list(self.required),
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": self.open_world},
        }

    def check(self, arguments: object) -> dict:
        """The arguments, or a ``ToolError`` that names the mistake."""

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be an object")
        allowed = {**self.properties, **COMMON_PROPERTIES}
        for name in arguments:
            if name not in allowed:
                raise ToolError(f"unknown argument {clip(name, 40)!r}", [f"Arguments of {self.name}: {', '.join(allowed)}."])
        for name in self.required:
            if arguments.get(name) in (None, ""):
                raise ToolError(f"missing argument {name!r}", [f"Pass {name}: {self.properties[name].get('description', '')}".strip()])
        for name, value in arguments.items():
            spec = allowed[name]
            kind = spec.get("type")
            if kind == "string" and not isinstance(value, str):
                raise ToolError(f"argument {name!r} must be a string")
            if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                raise ToolError(f"argument {name!r} must be an integer")
            if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ToolError(f"argument {name!r} must be a number")
            if kind == "boolean" and not isinstance(value, bool):
                raise ToolError(f"argument {name!r} must be true or false")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"argument {name!r} must be one of: {', '.join(spec['enum'])}")
        return arguments


REGISTRY: dict[str, Tool] = {}


def tool(name: str, title: str, description: str, properties: dict[str, dict] | None = None, required: tuple[str, ...] = (), *, open_world: bool = False):
    """Register ``function(session, arguments) -> Answer`` as the tool ``name`` (``open_world``: it can reach BigQuery when asked)."""

    def register(function: Callable[[Any, dict], Answer]) -> Callable[[Any, dict], Answer]:
        if name in REGISTRY:
            raise ValueError(f"tool {name!r} is registered twice")
        REGISTRY[name] = Tool(name, title, description, dict(properties or {}), tuple(required), function, open_world)
        return function

    return register


def bytes_wanted(arguments: dict) -> int:
    value = arguments.get("max_bytes", DEFAULT_BYTES)
    return max(MIN_BYTES, min(MAX_BYTES, value))


def offset_wanted(arguments: dict) -> int:
    return max(0, arguments.get("offset", 0))


def call(tool_: Tool, session: Any, arguments: object) -> tuple[dict, bool]:
    """Run one tool call: the bounded answer, and whether it is an error."""

    max_bytes = DEFAULT_BYTES
    try:
        checked = tool_.check(arguments)
        max_bytes = bytes_wanted(checked)
        return render(tool_.run(session, checked), max_bytes=max_bytes), False
    except ToolError as exc:
        body: dict[str, Any] = {"error": exc.message, **exc.extra}
        if exc.next_steps:
            body["next"] = exc.next_steps
        return _clip_strings(body, 300), True
    except Exception as exc:  # noqa: BLE001 - a tool must not take the server down
        return {"error": f"{tool_.name} failed: {type(exc).__name__}", "next": ["Try a narrower question; the server is still running."]}, True
