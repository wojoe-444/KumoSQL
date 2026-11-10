"""``list_tables``: what is in this project, and which tables have this shape?

The whole-project question the per-table tools cannot answer: how many models are there, which ones nobody reads, which read
two or more others, which sit deepest, which live in a folder or carry a tag. One line per table (``name | kind | reads |
read by | depth``) with filters and a sort, so an agent no longer opens every ``.sqlx`` file to count things. It reads
only the loaded project: no BigQuery, no language model, and the same call always gives the same list.

Assertions and output-less operations are not tables to use (as in ``find_tables``) and are left out unless ``kind`` names them.
"""

from __future__ import annotations

from collections import Counter

from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_session import Session

SORTS = ("name", "readers", "reads", "depth")
INTEGERS = ("min_reads", "max_reads", "min_readers", "max_readers", "min_depth")
TAGS_SHOWN = 8


class Row:
    __slots__ = ("key", "kind", "tags", "path", "reads", "sources", "readers", "depth")

    def __init__(self, key: str, kind: str, tags: tuple, path: str | None, reads: int, sources: int, readers: int, depth: int) -> None:
        self.key, self.kind, self.tags, self.path = key, kind, tags, path or ""
        self.reads, self.sources, self.readers, self.depth = reads, sources, readers, depth

    @property
    def name(self) -> str:
        return ".".join(self.key.split(".")[-2:])

    def line(self) -> str:
        sources = f"+{self.sources}src" if self.sources else ""
        return f"{self.name} | {self.kind} | reads {self.reads}{sources} | read by {self.readers} | depth {self.depth}"


def _depths(upstream: dict[str, set[str]], models: set[str]) -> dict[str, int]:
    """Models between each model and a source: 0 for a model that reads no other model, 1 + the deepest model it reads otherwise."""

    depth: dict[str, int] = {}
    stack = set()

    def visit(key: str) -> int:
        if key in depth:
            return depth[key]
        if key in stack:  # a cycle is a project error reported elsewhere; do not loop
            return 0
        stack.add(key)
        value = max((visit(parent) + 1 for parent in upstream.get(key, ()) if parent in models), default=0)
        stack.discard(key)
        depth[key] = value
        return value

    for key in models:
        visit(key)
    return depth


def build_rows(session: Session) -> list[Row]:
    pipeline = session.pipeline()
    downstream = session.downstream()
    models = set(pipeline.models)
    depth = _depths(pipeline.upstream, models)
    rows = []
    for key in (*pipeline.models, *pipeline.sources):
        model = pipeline.models.get(key)
        parents = pipeline.upstream.get(key, ()) if model is not None else ()
        rows.append(
            Row(
                key,
                model.kind if model is not None else "source",
                tuple(model.tags) if model is not None else (),
                model.path if model is not None else None,
                sum(1 for parent in parents if parent in models),
                sum(1 for parent in parents if parent not in models),
                len(downstream.get(key, ())),
                depth.get(key, 0),
            )
        )
    return rows


def _is_not_a_table(session: Session, row: Row) -> bool:
    model = session.pipeline().models.get(row.key)
    return model is not None and (model.kind == "assertion" or (model.kind == "operations" and not model.has_output))


@tool(
    "list_tables",
    "List the tables of the project",
    "What is in this project, and which tables have this shape? One line per table, 'name | kind | reads N (+n src: sources) | read by N | "
    "depth N (models above it)',  with counts, filters and a sort: the tables nobody reads (max_readers=0), the ones that read two or more "
    "models (min_reads=2), the deepest (sort=depth), everything under a folder, with a tag or with a word in the name. With no "
    "filter it is the overview. Reads only the loaded project.",
    {
        "kind": {"type": "string", "description": "Only this kind: table, view, incremental, source, or one the answer lists (assertion and operations are listed only when asked for)."},
        "tag": {"type": "string", "description": "Only tables with this tag (any case)."},
        "folder": {"type": "string", "description": "Only models whose file path contains this text, such as 'definitions/alerts'."},
        "name_contains": {"type": "string", "description": "Only tables whose name contains this text (any case)."},
        "min_reads": {"type": "integer", "description": "Only tables that read at least this many other models of the project (sources are not counted)."},
        "max_reads": {"type": "integer", "description": "Only tables that read at most this many other models."},
        "min_readers": {"type": "integer", "description": "Only tables that at least this many models read."},
        "max_readers": {"type": "integer", "description": "Only tables that at most this many models read; 0 lists the tables nothing reads."},
        "min_depth": {"type": "integer", "description": "Only tables at least this many models above a source (0: reads no other model)."},
        "count_only": {"type": "boolean", "description": "Answer with the counts only, no lines (a few hundred bytes): for 'how many' questions. Default false."},
        "sort": {"type": "string", "enum": list(SORTS), "description": "name (default), or readers, reads, depth: largest first, ties by name."},
    },
)
def list_tables(session: Session, args: dict) -> Answer:
    rows = session._once("list_tables_rows", lambda: build_rows(session))
    kind = str(args.get("kind") or "").strip().casefold()
    tag = str(args.get("tag") or "").strip().casefold()
    folder = str(args.get("folder") or "").strip().casefold()
    word = str(args.get("name_contains") or "").strip().casefold()
    limits = {name: args[name] for name in INTEGERS if name in args}
    sort = args.get("sort", "name")

    kinds = Counter(row.kind for row in rows if not _is_not_a_table(session, row))
    if kind and kind not in {row.kind.casefold() for row in rows}:
        raise ToolError(f"no table of kind {clip(kind, 40)!r} in this project", [f"Kinds here: {', '.join(sorted({row.kind for row in rows}))}."])

    def keep(row: Row) -> bool:
        if kind:
            if row.kind.casefold() != kind:
                return False
        elif _is_not_a_table(session, row):
            return False
        if tag and tag not in {t.casefold() for t in row.tags}:
            return False
        if folder and folder not in row.path.casefold():
            return False
        if word and word not in row.name.casefold():
            return False
        return (
            row.reads >= limits.get("min_reads", 0)
            and row.reads <= limits.get("max_reads", row.reads)
            and row.readers >= limits.get("min_readers", 0)
            and row.readers <= limits.get("max_readers", row.readers)
            and row.depth >= limits.get("min_depth", 0)
        )

    matched = [row for row in rows if keep(row)]
    if sort == "name":
        matched.sort(key=lambda row: row.name.casefold())
    else:
        attribute = {"readers": "readers", "reads": "reads", "depth": "depth"}[sort]
        matched.sort(key=lambda row: (-getattr(row, attribute), row.name.casefold()))
    offset = offset_wanted(args)
    filtered = bool(kind or tag or folder or word or limits)

    answer = Answer("")
    models = sum(count for name, count in kinds.items() if name != "source")
    answer.counts = {"matches": len(matched), "models": models, "sources": kinds.get("source", 0), "unread_models": sum(1 for r in rows if r.kind != "source" and not r.readers and not _is_not_a_table(session, r))}
    if len(matched) == models + kinds.get("source", 0):
        answer.counts["by_kind"] = dict(sorted(kinds.items(), key=lambda pair: (-pair[1], pair[0])))
    if not filtered:
        tag_counts = Counter(t for row in matched for t in row.tags)
        if tag_counts:
            answer.fields["tags"] = [f"{name} {count}" for name, count in sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:TAGS_SHOWN]]
    if not args.get("count_only"):
        answer.add_list("tables", [row.line() for row in matched[offset:]][:200], total=len(matched), offset=offset, primary=True)
    sort_note = "" if sort == "name" else f", largest {sort} first"
    if filtered:
        answer.summary = f"{len(matched)} of {models + kinds.get('source', 0)} tables match{sort_note}."
    else:
        answer.summary = f"{models} models and {kinds.get('source', 0)} source{'s' if kinds.get('source', 0) != 1 else ''}; {answer.counts['unread_models']} models nothing reads{sort_note}."
    if not matched:
        answer.next("Loosen a filter; with none, list_tables is the whole overview.")
        return answer
    if args.get("count_only"):
        answer.next("Leave count_only out to see the lines.")
        return answer
    answer.next(f"describe_table table={matched[0].name} says what a table is; impact table=... says what breaks if it changes.")
    if not filtered:
        answer.next("Narrow it: max_readers=0 (nothing reads them), min_reads=2, sort=depth, folder=..., tag=...")
    return answer
