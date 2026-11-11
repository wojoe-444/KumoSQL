"""Producers and consumers outside the repository: who builds a source, who reads beyond the project.

A project's lineage ends at a declared source, correctly named, and ``impact`` ends at the last model. But many sources are
built by jobs the repository does not contain (scheduled queries, notebook jobs, legacy query tools) and many outputs are
read by semantic models and BI data sources. A *boundary file* (``python -m kumosql mcp PROJECT --boundary FILE``, YAML or
JSON) names them, with synthetic shape::

    producers:
      - table: warehouse.lookups.region          # a declared source of the project
        by: {kind: scheduled_query, id: "1234", url: "https://example.test/q/1234"}
        sql: external/region.sql                 # a file, or a list of snippets; read as an outside model
      - table: warehouse.lookups.segment
        by: {kind: notebook, id: nb-7}
        sql_unavailable: the notebook code was not exported   # then a trace says so instead of ending silently
    consumers:
      - {name: sales_semantic_model, kind: semantic, path: "semantic/*.malloy"}   # Malloy: read by kumosql.malloy_reader
      - {name: legacy_report, kind: bi, path: "bi/*.lkml"}                        # any other file: scanned as text
      - {name: weekly_dashboard, kind: dashboard, sql: external/dashboard_source.sql}   # parsed: precise column reads

Paths are relative to the file. The file only adds knowledge; nothing it names is ever contacted or written.

* A producer's SQL is parsed with the normal parser and lineage, as a model outside the project: ``trace_column`` goes from a
  declared source into the columns that SQL reads, and on through the SQL of further producers, labelled as outside the
  project. ``describe_table`` says who builds the source.
* A consumer's ``sql`` is parsed and its column reads are exact. A consumer's ``.malloy`` files are read by
  :func:`kumosql.malloy_reader.read_malloy`: each Malloy source's table and the columns it refers to (``reads (Malloy)``).
  Other ``path`` files are scanned as text: a file that names the table (``dataset.table``) and the column as words is
  ``reads (by text)`` with the evidence file. Both are approximations and are labelled so; and a consumer whose files cannot
  be read (no file matched, not text, too large, Malloy the reader could not read) is reported as ``unknown``, never as
  having no reader.
* A file that does not parse, a table the project does not have (the closest names are given) or a table the project
  builds itself is a :class:`BoundaryError` at load time with a bounded, specific message.

Nothing here edits :mod:`kumosql.pipeline` or :mod:`kumosql.impact`: the outside models live in a second
:class:`~kumosql.pipeline.Pipeline` that reads the project's tables as its sources.
"""

from __future__ import annotations

import difflib
import glob
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .mcp_answers import clip
from .pipeline_types import ColumnRef, Model, Target

if TYPE_CHECKING:
    from .mcp_answers import Answer
    from .mcp_session import Session
    from .pipeline import Pipeline

MAX_FILE_BYTES = 2_000_000  # one SQL or scanned file
MAX_SPEC_BYTES = 1_000_000  # the boundary file itself
MAX_FILES = 200  # scanned files per consumer
MAX_PROBLEMS = 5  # problems named in one error
MAX_ITEMS = 200
_PRODUCER_KEYS = ("table", "by", "sql", "sql_unavailable")
_CONSUMER_KEYS = ("name", "kind", "url", "path", "sql")
_BY_KEYS = ("kind", "id", "url")
_CONSUMER_SCHEMA = "_consumers"


class BoundaryError(ValueError):
    """The boundary file cannot be used. The message names what is wrong and where, in a few lines."""


# ------------------------------------------------------------------ the file


@dataclass(frozen=True)
class SqlFile:
    path: str  # as written in the file, relative to it
    text: str


@dataclass(frozen=True)
class Producer:
    table: str
    kind: str
    ident: str
    url: str
    #: the SQL the job runs: one file, or several snippets that each build some columns of the table
    sqls: tuple[SqlFile, ...] = ()
    #: why the SQL cannot be given (a notebook job whose code was not exported); empty when it was given or just left out
    unavailable: str = ""

    @property
    def sql(self) -> SqlFile | None:
        return self.sqls[0] if self.sqls else None

    @property
    def label(self) -> str:
        return f"{self.kind} {self.ident}".strip()

    @property
    def no_sql_reason(self) -> str:
        """Why nothing can be followed through this producer, in one clause."""

        return f"its SQL is not available ({self.unavailable})" if self.unavailable else "the boundary file gives no SQL for it"


@dataclass(frozen=True)
class Consumer:
    name: str
    kind: str
    url: str
    patterns: tuple[str, ...]
    sql: SqlFile | None


@dataclass(frozen=True)
class BoundarySpec:
    base: Path
    producers: tuple[Producer, ...]
    consumers: tuple[Consumer, ...]


class _Problems:
    def __init__(self) -> None:
        self.found: list[str] = []

    def add(self, text: str) -> None:
        self.found.append(text)

    def raise_if_any(self, where: str) -> None:
        if not self.found:
            return
        shown = self.found[:MAX_PROBLEMS]
        more = f" (and {len(self.found) - len(shown)} more)" if len(self.found) > len(shown) else ""
        raise BoundaryError(f"{where}: " + "; ".join(shown) + more)


def _text(value: object, what: str, problems: _Problems, *, required: bool = True, limit: int = 300) -> str:
    if value is None or value == "":
        if required:
            problems.add(f"{what} is missing")
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        problems.add(f"{what} must be text")
        return ""
    text = str(value).strip()
    if len(text) > limit:
        problems.add(f"{what} is longer than {limit} characters")
        return ""
    return text


def _relative(value: str, what: str, problems: _Problems) -> bool:
    if value.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", value):
        problems.add(f"{what} must be relative to the boundary file, not absolute")
        return False
    return True


def _keys(entry: dict, allowed: tuple[str, ...], what: str, problems: _Problems) -> None:
    for key in entry:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f"; did you mean {close[0]}?" if close else f"; allowed: {', '.join(allowed)}"
            problems.add(f"{what} has an unknown key {str(key)[:40]!r}{hint}")


def _read_sql(base: Path, value: object, what: str, problems: _Problems) -> SqlFile | None:
    rel = _text(value, f"{what} sql", problems, required=False)
    if not rel or not _relative(rel, f"{what} sql", problems):
        return None
    path = base / rel
    try:
        if not path.is_file():
            problems.add(f"{what} sql {rel} is not a file")
            return None
        if path.stat().st_size > MAX_FILE_BYTES:
            problems.add(f"{what} sql {rel} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
            return None
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        problems.add(f"{what} sql {rel} is not UTF-8 text")
        return None
    except OSError as exc:
        problems.add(f"{what} sql {rel} cannot be read ({type(exc).__name__})")
        return None
    if not text.strip():
        problems.add(f"{what} sql {rel} is empty")
        return None
    from .pipeline import _parse_failure

    failure = _parse_failure(text)
    if failure:
        problems.add(f"{what} sql {rel} does not parse: {failure[:120]}")
        return None
    return SqlFile(rel, text)


def read_file(path: Path) -> BoundarySpec:
    """The producers and consumers a boundary file names, with their SQL read and parsed.

    Raises :class:`BoundaryError` for a file that is missing, not YAML or JSON, shaped wrongly, or whose SQL cannot be read
    or parsed. Table names are checked against a project later (:func:`build`).
    """

    path = Path(path)
    where = f"boundary file {path.name}"
    try:
        if not path.is_file():
            raise BoundaryError(f"{where} not found")
        if path.stat().st_size > MAX_SPEC_BYTES:
            raise BoundaryError(f"{where} is larger than {MAX_SPEC_BYTES // 1_000_000} MB")
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise BoundaryError(f"{where} is not UTF-8 text") from exc
    except OSError as exc:
        raise BoundaryError(f"{where} cannot be read ({type(exc).__name__})") from exc
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            import yaml

            data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - json and yaml raise their own families
        reason = str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__
        raise BoundaryError(f"{where} is not valid YAML or JSON: {reason}") from exc
    if not isinstance(data, dict):
        raise BoundaryError(f"{where} must be a mapping with `producers` and/or `consumers` lists")

    problems = _Problems()
    _keys(data, ("producers", "consumers"), where, problems)
    base = path.resolve().parent
    producers: list[Producer] = []
    consumers: list[Consumer] = []
    for index, entry in enumerate(_entries(data, "producers", where, problems), 1):
        label = f"producers[{index}]"
        if not isinstance(entry, dict):
            problems.add(f"{label} must be a mapping")
            continue
        _keys(entry, _PRODUCER_KEYS, label, problems)
        table = _text(entry.get("table"), f"{label} table", problems)
        by = entry.get("by")
        if not isinstance(by, dict):
            problems.add(f"{label} by must be a mapping with a kind, an id and a url")
            by = {}
        _keys(by, _BY_KEYS, f"{label} by", problems)
        kind = _text(by.get("kind"), f"{label} by.kind", problems)
        ident = _text(by.get("id"), f"{label} by.id", problems, required=False)
        url = _text(by.get("url"), f"{label} by.url", problems, required=False, limit=500)
        written = entry.get("sql")
        written = [] if written is None else written if isinstance(written, list) else [written]
        if not written and entry.get("sql") is not None:
            problems.add(f"{label} sql must be a file or a list of files")
        sqls = tuple(found for found in (_read_sql(base, item, label, problems) for item in written) if found is not None)
        unavailable = _text(entry.get("sql_unavailable"), f"{label} sql_unavailable", problems, required=False, limit=160)
        if unavailable and written:
            problems.add(f"{label} gives both sql and sql_unavailable")
        if table and kind:
            producers.append(Producer(table, kind, ident, url, sqls, unavailable))
    for index, entry in enumerate(_entries(data, "consumers", where, problems), 1):
        label = f"consumers[{index}]"
        if not isinstance(entry, dict):
            problems.add(f"{label} must be a mapping")
            continue
        _keys(entry, _CONSUMER_KEYS, label, problems)
        name = _text(entry.get("name"), f"{label} name", problems)
        kind = _text(entry.get("kind"), f"{label} kind", problems, required=False) or "other"
        url = _text(entry.get("url"), f"{label} url", problems, required=False, limit=500)
        patterns = entry.get("path")
        patterns = [patterns] if isinstance(patterns, str) else patterns
        listed: list[str] = []
        if patterns is not None:
            if not isinstance(patterns, list) or not all(isinstance(p, str) and p.strip() for p in patterns):
                problems.add(f"{label} path must be a path or glob, or a list of them")
            else:
                listed = [p.strip() for p in patterns if _relative(p.strip(), f"{label} path", problems)]
        sql = _read_sql(base, entry.get("sql"), label, problems) if entry.get("sql") is not None else None
        if name and not listed and entry.get("sql") is None:
            problems.add(f"{label} ({name}) needs a path (files scanned as text) or a sql file")
        if name:
            consumers.append(Consumer(name, kind, url, tuple(listed), sql))
    seen: set[str] = set()
    for consumer in consumers:
        if consumer.name.casefold() in seen:
            problems.add(f"consumer {consumer.name!r} is listed twice")
        seen.add(consumer.name.casefold())
    if not producers and not consumers and not problems.found:
        problems.add("it names no producers and no consumers")
    problems.raise_if_any(where)
    return BoundarySpec(base, tuple(producers), tuple(consumers))


def _entries(data: dict, key: str, where: str, problems: _Problems) -> list:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        problems.add(f"{where}: `{key}` must be a list")
        return []
    return value


# ------------------------------------------------------------------ against a project


def build(spec: BoundarySpec, session: "Session") -> "Boundary":
    """The spec checked against the project: every producer table is a source the project reads and does not build."""

    pipeline = session.pipeline()
    problems = _Problems()
    by_key: dict[str, Producer] = {}
    for producer in spec.producers:
        key, close = session.resolve(producer.table)
        if key is None:
            hint = f" (close: {', '.join(close[:3])})" if close else ""
            problems.add(f"producer table {producer.table!r} is not a table of the project{hint}")
        elif key in pipeline.models:
            problems.add(f"producer table {producer.table!r} is built by the project (model {key}); an outside producer must be a source")
        elif key in by_key:
            problems.add(f"table {key} has two producers")
        else:
            by_key[key] = producer
    problems.raise_if_any("boundary file")
    return Boundary(spec, session, by_key)


@dataclass
class Readers:
    """The outside readers of one change: one item per reader, and what could not be decided."""

    items: list[dict] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)
    #: set when the boundary file could not be applied at all
    problem: str | None = None


#: how a consumer's files fail to be read, as plain reasons (one line each, so many consumers group)
_FILE_REASONS = {
    "none": "no file matches the path",
    "not_text": "a file is not text",
    "big": f"a file is larger than {MAX_FILE_BYTES // 1_000_000} MB",
    "unreadable": "a file cannot be read",
    "many": f"a pattern matches more than {MAX_FILES} files, so only the first {MAX_FILES} were scanned",
}
_MALLOY_REASON = "a Malloy file has parts the reader could not read, so it was also matched as text"
_NO_TABLE_REASON = "a Malloy source's table could not be determined"


class Boundary:
    def __init__(self, spec: BoundarySpec, session: "Session", producers: dict[str, Producer]) -> None:
        self.spec = spec
        self.session = session
        self.producers = producers
        self._lock = threading.RLock()
        self._outside_pipeline: "Pipeline | None" = None
        self._outside_error: str | None = None
        self._files: dict[tuple, tuple[str | None, str]] = {}
        self._malloy: dict[tuple, dict] = {}
        #: outside model key -> the producer it belongs to; a producer's key is its table, extra snippets get their own
        self._snippets: dict[str, list[str]] = {}
        self._owner: dict[str, Producer] = {}
        self._consumer_of: dict[str, Consumer] = {}

    # ---------------------------------------------------------- names

    @staticmethod
    def built_by(producer: Producer) -> str:
        return f"{producer.label} ({producer.url})" if producer.url else producer.label

    # ---------------------------------------------------------- the outside models

    def outside(self) -> "Pipeline | None":
        """A pipeline of the producers' and consumers' SQL as models; the project's tables are its sources. ``None`` when there is none."""

        with self._lock:
            if self._outside_pipeline is None and self._outside_error is None:
                try:
                    self._outside_pipeline = self._build_outside()
                except Exception as exc:  # noqa: BLE001 - a bad outside file must not take the server down
                    self._outside_error = f"the outside SQL could not be analysed ({type(exc).__name__})"
            return self._outside_pipeline

    def _build_outside(self) -> "Pipeline | None":
        from .pipeline import Pipeline

        main = self.session.pipeline()
        models: dict[str, Model] = {}
        for number, (key, producer) in enumerate(self.producers.items()):
            keys = []
            for part, sql in enumerate(producer.sqls):
                if part == 0:
                    model_key, target = key, main.sources[key]
                else:
                    target = Target("", "_producers", f"p{number}_{part}")
                    model_key = target.key
                models[model_key] = Model(target, "table", sql.text, path=sql.path)
                self._owner[model_key] = producer
                keys.append(model_key)
            if keys:
                self._snippets[key] = keys
        for index, consumer in enumerate(self.spec.consumers):
            if consumer.sql is not None:
                target = Target("", _CONSUMER_SCHEMA, f"c{index}")
                models[target.key] = Model(target, "table", consumer.sql.text, path=consumer.sql.path)
                self._consumer_of[target.key] = consumer
        if not models:
            return None
        sources = {key: model.target for key, model in main.models.items()}
        sources.update({key: target for key, target in main.sources.items() if key not in models})
        schema = {key: dict(columns) for key, columns in main.source_schema.items() if key not in models}
        for key in main.models:
            schema.setdefault(key, {name: "STRING" for name in main.output_columns(key)})
        return Pipeline(
            models, sources=sources, source_schema=schema,
            default_project=main.default_project, default_dataset=main.default_dataset, default_location=main.default_location,
        )

    def warm(self) -> None:
        """Analyse the outside SQL now, so the first question is not the one that pays for many files."""

        outside = self.outside()
        if outside is not None:
            outside.explain_lineage()

    # ---------------------------------------------------------- trace

    def trace(self, starts: Iterable[ColumnRef]) -> tuple[list[dict], list[tuple[str, str]]]:
        """Follow columns past the declared sources into the producers' SQL: one item per source column built outside.

        Each item names the builder and the columns its SQL reads (through further producers when it reads their tables).
        A producer without SQL, or an outside step that cannot be followed, is an unknown with its reason.
        """

        main = self.session.pipeline()
        outside = self.outside()
        queue: deque[ColumnRef] = deque(starts)
        seen: set[tuple[str, str]] = set()
        items: list[dict] = []
        unknown: list[tuple[str, str]] = []
        if outside is None and self._outside_error:
            unknown.append(("the producers' SQL", self._outside_error))
        while queue and len(items) < MAX_ITEMS:
            ref = queue.popleft()
            marker = (ref.table, ref.column.casefold())
            if marker in seen:
                continue
            seen.add(marker)
            producer = self.producers.get(ref.table)
            if producer is not None:
                self._trace_producer(producer, ref, outside, items, unknown, queue)
            elif ref.table in main.models:
                # an outside job reads a table the project builds: keep following it inside the project
                try:
                    inner = main.trace_column(ref)
                except Exception:  # noqa: BLE001
                    continue
                queue.extend(inner.sources)
        return items, unknown

    def _trace_producer(self, producer: Producer, ref: ColumnRef, outside, items: list, unknown: list, queue: deque) -> None:
        item: dict = {"source": str(ref), "built_by": self.built_by(producer)}
        keys = self._snippets.get(ref.table, []) if outside is not None else []
        if not keys:
            if producer.unavailable:
                item["sql"] = clip(f"not available: {producer.unavailable}", 100)
            unknown.append((str(ref), f"built outside the project by {producer.label}; {producer.no_sql_reason}"))
            items.append(item)
            return
        item["file"] = producer.sql.path
        reads: set[str] = set()
        through: set[str] = set()
        matched = False
        for key in keys:
            name = next((c for c in outside.output_columns(key) if c.casefold() == ref.column.casefold()), None)
            if name is None:
                continue
            matched = True
            found = outside.trace_column(ColumnRef(key, name, ref.path))
            reads.update(str(source) for source in found.sources)
            through.update(up.table for up in found.upstream if up.table in self.producers and up.table != ref.table)
            for step, reason in found.unknown:
                unknown.append((str(step), reason.split(":")[0]))
            queue.extend(found.sources)
        if not matched:
            unknown.append((str(ref), f"the SQL given for {producer.label} does not output this column"))
        ordered = sorted(reads)
        item["reads"] = ordered[:6] + ([f"+{len(ordered) - 6}"] if len(ordered) > 6 else [])
        if through:
            item["through"] = sorted(through)[:4]
        if len(keys) > 1:
            item["sql_snippets"] = len(keys)
        items.append(item)

    # ---------------------------------------------------------- reads of files

    def _scan_files(self, consumer: Consumer) -> tuple[list[tuple[str, str]], list[str]]:
        """The text of a consumer's files and the codes of what could not be read: ``([(file, text)], [code])``."""

        texts: list[tuple[str, str]] = []
        problems: list[str] = []
        base = str(self.spec.base)
        for pattern in consumer.patterns:
            try:
                names = sorted(name for name in glob.glob(pattern, root_dir=base, recursive=True) if (self.spec.base / name).is_file())
            except (OSError, ValueError):
                names = []
            if not names:
                problems.append("none")
                continue
            if len(names) > MAX_FILES:
                problems.append("many")
                names = names[:MAX_FILES]
            for name in names:
                text, code = self._read_cached(name)
                if text is None:
                    problems.append(code)
                else:
                    texts.append((name, text))
        return texts, problems

    def _read_cached(self, name: str) -> tuple[str | None, str]:
        path = self.spec.base / name
        try:
            stat = path.stat()
            signature = (name, stat.st_mtime_ns, stat.st_size)
            with self._lock:
                cached = self._files.get(signature)
            if cached is not None:
                return cached
            if stat.st_size > MAX_FILE_BYTES:
                result: tuple[str | None, str] = (None, "big")
            else:
                data = path.read_bytes()
                if b"\x00" in data[:4096]:
                    result = (None, "not_text")
                else:
                    try:
                        result = (data.decode("utf-8-sig"), "")
                    except UnicodeError:
                        result = (None, "not_text")
        except OSError:
            return None, "unreadable"
        with self._lock:
            self._files[signature] = result
        return result

    def _malloy_sources(self, consumer: Consumer, texts: list[tuple[str, str]]) -> tuple[list[dict], dict[str, str]]:
        """The Malloy sources of a consumer's ``.malloy`` files, each with its file and the project table it reads.

        Returns ``(sources, files_with_unread_parts)``; a source extending one defined in another file of the consumer is resolved.
        """

        files = [(name, text) for name, text in texts if name.lower().endswith(".malloy")]
        signature = (consumer.name, tuple((name, len(text), hash(text)) for name, text in files))
        with self._lock:
            cached = self._malloy.get(signature)
        if cached is not None:
            return cached["sources"], cached["unread"]
        from .malloy_reader import read_malloy

        first = {name: read_malloy(text) for name, text in files}
        known = {source["name"]: source for read in first.values() for source in read}
        sources: list[dict] = []
        unread: dict[str, str] = {}
        for name, text in files:
            read = first[name]
            if any(source["extends"] and source["table"] is None and source["extends"] in known for source in read):
                read = read_malloy(text, known)
            if read.unknown:
                unread[name] = read.unknown[0]["reason"]
            for source in read:
                tables = {}
                if source["table"]:
                    tables[""] = source["table"]
                for join in source["joins"]:
                    if join["table"]:
                        tables[join["name"]] = join["table"]
                keyed = {where: self.session.resolve(table)[0] for where, table in tables.items()}
                sources.append({**source, "file": name, "keys": keyed})
        value = {"sources": sources, "unread": unread}
        with self._lock:
            self._malloy[signature] = value
        return sources, unread

    # ---------------------------------------------------------- readers

    def readers(self, change: str, key: str, column: str | None) -> Readers:
        """Who outside the project reads ``key`` (and ``column``): parsed SQL and Malloy sources, and other files by text."""

        pipeline = self.session.pipeline()
        found = Readers()
        outside = self.outside()
        precise: set[str] = set()
        if outside is None and self._outside_error:
            found.unknown.append(("outside SQL", self._outside_error))
        if outside is not None:
            self._sql_readers(outside, change, key, column, found, precise)
        root_column = column.split(".")[0] if column else None
        matcher = _TextMatcher(pipeline, key, root_column)
        for consumer in self.spec.consumers:
            if not consumer.patterns:
                continue
            texts, problems = self._scan_files(consumer)
            for code in dict.fromkeys(problems):
                found.unknown.append((consumer.name, _FILE_REASONS[code]))
            malloy, unread = self._malloy_sources(consumer, texts)
            hits = _malloy_hits(malloy, key, root_column)
            if any(source["table"] is None for source in malloy):
                found.unknown.append((consumer.name, _NO_TABLE_REASON))
            if unread:
                found.unknown.append((consumer.name, _MALLOY_REASON))
            evidence: list[str] = []
            unnamed: list[str] = []
            for name, text in texts:
                if name.lower().endswith(".malloy") and name not in unread:
                    continue  # read as Malloy
                verdict = matcher.check(text)
                if verdict == "reads":
                    evidence.append(name)
                elif verdict == "column_only":
                    unnamed.append(name)
            if consumer.name in precise:
                continue
            if hits:
                files = list(dict.fromkeys(hit["file"] for hit in hits))
                item = {"name": consumer.name, "kind": consumer.kind, "how": "reads (Malloy)",
                        "sources": list(dict.fromkeys(hit["name"] for hit in hits))[:3], "evidence": files[:2]}
                if root_column:
                    item["columns"] = [root_column]
                found.items.append(item)
            elif evidence:
                item = {"name": consumer.name, "kind": consumer.kind, "how": "reads (by text)", "evidence": evidence[:2]}
                if len(evidence) > 2:
                    item["evidence_files"] = len(evidence)
                if root_column:
                    item["columns"] = [root_column]
                found.items.append(item)
            elif unnamed:
                found.unknown.append((consumer.name, f"a file names the column {root_column} but not the table, so a text match cannot tell"))
        found.items.sort(key=lambda item: (item["how"] != "reads", item["how"] != "reads (Malloy)", item["name"].casefold()))
        return found

    def _sql_readers(self, outside, change: str, key: str, column: str | None, found: Readers, precise: set[str]) -> None:
        result = outside.assess_change(change, key, column)
        for affected in sorted(result.affected, key=lambda a: (a.depth, a.model)):
            item: dict = {"how": "reads", "effect": affected.effect}
            consumer = self._consumer_of.get(affected.model)
            producer = self._owner.get(affected.model)
            if consumer is not None:
                item = {"name": consumer.name, "kind": consumer.kind, **item, "file": consumer.sql.path}
                precise.add(consumer.name)
            elif producer is not None:
                item = {"name": producer.table, "kind": f"table built by {producer.kind}", **item, "built_by": producer.label}
                feeds = sorted(self.session.downstream().get(producer.table, ()))
                if feeds:
                    item["feeds_project_models"] = feeds[:3] + ([f"+{len(feeds) - 3}"] if len(feeds) > 3 else [])
                if any(i.get("name") == producer.table for i in found.items):
                    continue
            else:
                continue
            if affected.depth > 1:
                item["depth"] = affected.depth
            if affected.columns:
                item["columns"] = list(affected.columns[:4]) + ([f"+{len(affected.columns) - 4}"] if len(affected.columns) > 4 else [])
            found.items.append(item)
        for reader in result.unknown:
            consumer = self._consumer_of.get(reader.model)
            producer = self._owner.get(reader.model)
            name = consumer.name if consumer else producer.table if producer else reader.model
            found.unknown.append((name, reader.reason))
        # "terminal output" is true of every outside model, and each unknown reader is listed by itself
        gaps = [r for r in result.incomplete_reasons if r not in ("terminal_output_may_be_read_outside_pipeline", "unknown_readers")]
        if gaps:
            found.unknown.append(("the outside readers", ", ".join(gaps)))


def _malloy_hits(sources: list[dict], key: str, column: str | None) -> list[dict]:
    """The Malloy sources that read ``key`` (and ``column``): by their own table, or through a join."""

    hits = []
    for source in sources:
        wanted = column.casefold() if column else None
        if source["keys"].get("") == key and (wanted is None or wanted in {c.casefold() for c in source["columns"]}):
            hits.append(source)
            continue
        for join in source["joins"]:
            if source["keys"].get(join["name"]) == key and (wanted is None or wanted in {c.casefold() for c in join["columns"]}):
                hits.append(source)
                break
    return hits


class _TextMatcher:
    """Whether a text names a table (as ``dataset.table``) and one of its columns (as a word), case-insensitively."""

    def __init__(self, pipeline: "Pipeline", key: str, column: str | None) -> None:
        parts = key.split(".")
        spellings = {".".join(parts[-2:]), key} if len(parts) >= 2 else {key}
        alternatives = []
        for spelling in sorted(spellings, key=len, reverse=True):
            alternatives.append(r"`?\.`?".join(re.escape(part) for part in spelling.split(".")))
        self.table = re.compile(r"(?<![\w])(?:" + "|".join(f"(?:{a})" for a in alternatives) + r")(?![\w])", re.IGNORECASE)
        self.column = re.compile(r"(?<![\w])" + re.escape(column) + r"(?![\w])", re.IGNORECASE) if column else None

    def check(self, text: str) -> str:
        """``reads`` (table and column, or the table for a whole-table question), ``column_only`` or ``no``."""

        names_table = self.table.search(text) is not None
        if self.column is None:
            return "reads" if names_table else "no"
        names_column = self.column.search(text) is not None
        if names_table and names_column:
            return "reads"
        return "column_only" if names_column else "no"


# ------------------------------------------------------------------ the answers


def _state(session: "Session") -> tuple["Boundary | None", str | None]:
    return session.boundary(), session.boundary_error


def annotate_table(session: "Session", key: str, answer: "Answer") -> None:
    """``describe_table``: who builds the source, and who outside the project reads the table."""

    boundary, problem = _state(session)
    if problem:
        answer.unknown("builders and readers outside the project", f"the boundary file was not applied: {problem}")
        return
    if boundary is None:
        return
    pipeline = session.pipeline()
    producer = boundary.producers.get(key)
    if producer is not None:
        answer.fields["built_outside_the_project"] = boundary.built_by(producer)
        outside = boundary.outside()
        if not producer.sqls or outside is None:
            answer.unknown("what the outside job reads", producer.no_sql_reason)
        else:
            table_reads = outside.table_reads()
            reads = sorted({table for part in boundary._snippets.get(key, [key]) for table in table_reads.get(part, ())})
            answer.add_list("outside_reads", reads[:MAX_ITEMS], total=len(reads))
    elif key in pipeline.sources and key not in pipeline.models:
        answer.unknown("who builds it", "the boundary file does not name a producer for this source")
    found = boundary.readers("drop_table", key, None)
    names = [item["name"] for item in found.items]
    answer.counts["read_by_outside_project"] = len(names)
    answer.add_list("read_by_outside_project", names[:MAX_ITEMS], total=len(names))
    _unknowns(answer, found.unknown)
    if names:
        answer.next(f"impact table={key} column=<column> says which outside readers use a column.")


def annotate_trace(session: "Session", starts: Iterable[ColumnRef], answer: "Answer", *, source_tables: Iterable[str] = ()) -> None:
    """``trace_column``: continue past the declared sources into the producers' SQL, labelled as outside the project."""

    boundary, problem = _state(session)
    if problem:
        answer.unknown("where the sources come from", f"the boundary file was not applied: {problem}")
        return
    if boundary is None:
        return
    items, unknown = boundary.trace(starts)
    if items:
        answer.counts["built_outside_the_project"] = len(items)
        answer.add_list("outside_the_project", items[:MAX_ITEMS], total=len(items))
        answer.fields["outside_note"] = "outside_the_project: sources built by jobs outside the repository, followed through the SQL the boundary file gives"
    from .mcp_tools import group_unknown

    group_unknown(answer, unknown, "outside column")
    unnamed = sorted({table for table in source_tables if table not in boundary.producers})
    if unnamed:
        answer.unknown(f"{len(unnamed)} source table{'s' if len(unnamed) != 1 else ''}: {', '.join(unnamed[:3])}{', ...' if len(unnamed) > 3 else ''}", "the boundary file names no producer for them")
    first = next((i["file"] for i in items if "file" in i), None)
    if first:
        answer.next(f"Open {first} to see how the outside job builds it.")


def impact_readers(session: "Session", change: str, key: str, column: str | None) -> Readers | None:
    """The outside readers of a change for ``impact``; ``None`` when no boundary file was given."""

    boundary, problem = _state(session)
    if problem:
        return Readers(problem=problem)
    if boundary is None:
        return None
    return boundary.readers(change, key, column)


def annotate_impact(answer: "Answer", found: Readers | None, *, offset: int = 0, primary: bool = False) -> None:
    """``impact``: the producers and consumers outside the project that read the target, grouped and paged.

    ``primary`` makes ``readers_outside_project`` the list ``offset`` pages (the caller chooses it when there are more outside
    readers than readers inside the project).
    """

    if found is None:
        return
    if found.problem:
        answer.unknown("readers outside the project", f"the boundary file was not applied: {found.problem}")
        return
    answer.fields["safe_to_delete"] = "unknown (only the outside readers named in the boundary file were checked; query history and unlisted dashboards are not visible)"
    items = found.items
    answer.counts["outside_readers"] = len(items)
    kinds: dict[str, int] = {}
    for item in items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    if len(kinds) > 1:
        top = sorted(kinds.items(), key=lambda pair: (-pair[1], pair[0]))
        shown = dict(top[:4])
        if len(top) > 4:
            shown["other kinds"] = sum(n for _, n in top[4:])
        answer.counts["outside_by_kind"] = shown
    answer.add_list("readers_outside_project", items[offset:][:MAX_ITEMS], total=len(items), offset=offset, primary=primary)
    if any(item["how"].startswith("reads (") for item in items):
        answer.fields["outside_note"] = "reads (by text): a file names the table and the column, matched as text; reads (Malloy): a Malloy source reads the table and refers to the column, read without compiling; both can be wrong either way"
    if items:
        answer.summary = answer.summary.rstrip(".") + f"; {len(items)} reader{'s' if len(items) != 1 else ''} outside the project."
    _unknowns(answer, found.unknown)
    first = items[0] if items else None
    if first is not None:
        where = first.get("file") or (first.get("evidence") or [None])[0]
        answer.next(f"Open {where} to see how {first['name']} uses it." if where else f"{first['name']} reads it outside the project.")
    elif not found.unknown:
        answer.next("No outside reader named in the boundary file reads it; readers it does not name are still invisible.")


def _unknowns(answer: "Answer", pairs: list[tuple[str, str]]) -> None:
    from .mcp_tools import group_unknown

    group_unknown(answer, pairs, "outside reader")
