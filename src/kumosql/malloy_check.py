"""``check-malloy`` and the ``check_malloy`` tool: confront a Malloy model with what KumoSQL already holds about its tables.

Malloy's aggregates are fan-out safe only if every source's ``primary_key`` really identifies a row of its table and every
``join_one`` really matches at most one row. A wrong key, or a ``join_one`` that fans out, gives numbers that are silently
wrong. This check reads ``.malloy`` files with :mod:`kumosql.malloy_reader` and reports two kinds of finding:

* **Structural**, from the files alone: a join ``with`` a source that has no ``primary_key``; sources that join (or extend)
  each other in a cycle, a source joining itself included; a source, dimension, measure, view or join name defined twice in
  one source or one file; a source used before (or without) being defined; and a bare name that must be a measure
  (``aggregate: x``) or a view (``nest: x``, ``view: v is x + {...}``, ``run: s -> x``) and is not one.
* **Against the evidence KumoSQL holds** (``--project``, ``--schema-snapshot``, saved profiles), never the warehouse: a
  ``primary_key`` that is not the grain of the table it names, a ``join_one`` whose key repeats in the joined table, a
  column the model reads that its table does not have, and a measure that sums or averages a ratio column.

Every finding says how sure it is, in its ``status`` and the first words of ``why``:

* ``contradicted by <evidence>``: the evidence shows the model is wrong here (a structural problem is contradicted by the
  model text itself). The CLI exits 1 when there is one.
* ``unconfirmed: ...``: nothing KumoSQL holds decides it; ``next`` names the opt-in command that would measure it.
* ``confirmed by <evidence>``: the evidence agrees.

A key is never called wrong without evidence. Grain evidence is, in order: the table's grain from ``table_profile``
(derived from a model's query, or declared by its ``uniqueKey``), the grain the project states for a table it does not
build (:mod:`kumosql.source_grain`: a declaration's ``assertions.uniqueKey``, a duplicate-key assertion, docs,
``--assume-grain``; never a Malloy ``primary_key``, which is the claim being checked), and the key measurements of saved
data profiles and saved ``join_profile`` runs (:mod:`kumosql.join_profile_store`). A measurement is a count on one day and
is reported with its date. ``key-check`` prints its result and saves none, so the saved measurements are the only measured
evidence; an unconfirmed key's ``next`` names ``key-check`` to answer it at once and the command whose saved result counts. A source with a ``where:`` filter keeps a subset of its table, so whole-table evidence of repeats does not
contradict its key (it stays unconfirmed).

Ratio columns: the ratio re-aggregation detector (``kumosql.ratio_check``) is being built separately. :func:`ratio_verdict`
is the hook that will call it; until it exists, a column counts as a ratio when the project computes it with a division
(lineage), when ``--column-attributes`` declares a ``scale``, when the Malloy model defines it with ``/``, or (weakest)
when its name says so.

Limits: the reader is an approximation, not the Malloy compiler (see :mod:`kumosql.malloy_reader`); what it cannot read is
listed under ``unknown``. Only ``.malloy`` files are read: no ``.malloynb`` notebooks, no queries' results.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import malloy_reader as reader
from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool

MAX_FILE = 1_000_000
MAX_FILES = 200

CONTRADICTED, UNCONFIRMED, CONFIRMED = "contradicted", "unconfirmed", "confirmed"
STATUSES = (CONTRADICTED, UNCONFIRMED, CONFIRMED)

#: check code -> what it means
CHECKS = {
    "join_without_key": "a join `with` a source that has no primary_key",
    "join_many_with": "a join_many written with `with`, which Malloy rejects",
    "join_cycle": "sources that join or extend each other in a cycle",
    "duplicate_name": "a source or field name defined twice",
    "undefined_source": "a source used before or without being defined",
    "undefined_field": "a name used as a measure or view that is not one",
    "primary_key": "whether a primary_key is the grain of its table",
    "join_fan_out": "whether a join_one's key is unique in the joined table",
    "missing_column": "whether the columns a source reads exist in its table",
    "ratio_measure": "a measure that sums or averages a ratio column",
}

#: words of Malloy expressions the reader does not already know are never columns
_MORE_WORDS = {"case", "end", "distinct", "then", "when", "else", "is_not_null", "not"}
_RATIO_NAME = re.compile(r"(?:^|_)(?:pct|percent|percentage|ratio|rate|share)(?:$|_)", re.IGNORECASE)


class MalloyInputError(ValueError):
    """A path that cannot be checked (missing, not a ``.malloy`` file or folder, too large)."""


# ------------------------------------------------------------------------------------------- reading the model


@dataclass
class Field:
    kind: str
    name: str | None
    line: int
    tokens: list


@dataclass
class Join:
    name: str
    kind: str  # one, many, cross
    #: ``with``, ``on`` or ``None`` (a cross join, or a join with no condition)
    mode: str | None
    #: the source joined by name, or ``None`` for an inline ``table('...')``
    source: str | None
    table: str | None
    line: int
    condition: list
    #: why the reader could not tell what is joined, when it could not
    unread: str | None = None


@dataclass
class Source:
    name: str
    file: str
    line: int
    #: ``("table", path)``, ``("source", name)`` or ``("unknown", reason)``
    base: tuple[str, str]
    fields: list[Field] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    #: the table it reads and its key, its own or those of the source it extends
    table: str | None = None
    primary_key: str | None = None
    parent: "Source | None" = None

    @property
    def at(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def own_key(self) -> str | None:
        for item in self.fields:
            if item.kind == "primary_key":
                word = next((t.text for t in item.tokens if t.kind == "word"), None)
                if word:
                    return word
        return None

    def chain(self) -> list["Source"]:
        """This source and the sources it extends, nearest first (a cycle stops the walk)."""

        found, seen, here = [], set(), self
        while here is not None and id(here) not in seen:
            found.append(here)
            seen.add(id(here))
            here = here.parent
        return found

    @property
    def filtered(self) -> bool:
        """Whether a ``where:`` in this source or one it extends keeps only some of the table's rows."""

        return any(item.kind == "where" for source in self.chain() for item in source.fields)

    def all_joins(self) -> dict[str, Join]:
        found: dict[str, Join] = {}
        for source in reversed(self.chain()):
            found.update({j.name: j for j in source.joins if j.name})
        return found

    def field_of(self, name: str) -> Field | None:
        for source in self.chain():
            for item in source.fields:
                if item.name == name and item.kind in ("dimension", "measure", "view"):
                    return item
        return None


@dataclass
class ModelFile:
    name: str
    text: str
    sources: list[Source] = field(default_factory=list)
    #: every ``source:`` statement in order, as ``(name, line)``, repeats included
    statements: list[tuple[str, int]] = field(default_factory=list)
    queries: set[str] = field(default_factory=set)
    #: ``(source name, view name or None, line)`` of each ``run:`` / ``query:`` head
    runs: list[tuple[str, str | None, int]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)
    #: the files this one imports that were read, by import text
    imported: dict[str, "ModelFile"] = field(default_factory=dict)


def _scan(name: str, text: str) -> ModelFile:
    """The statements of one file: sources with their fields and joins, query names, ``run:`` heads and imports."""

    model = ModelFile(name, text)
    tokens = reader._tokens(text, [])
    depths = reader._depths(tokens)
    if depths is None:
        return model
    starts = [
        i for i, t in enumerate(tokens)
        if depths[i] == 0 and t.kind == "word" and t.text in reader._STATEMENTS
        and (t.text == "import" or (i + 1 < len(tokens) and tokens[i + 1].is_(":"))) and not (i and tokens[i - 1].is_("."))
    ]
    for n, start in enumerate(starts):
        statement = tokens[start : starts[n + 1] if n + 1 < len(starts) else len(tokens)]
        keyword = statement[0].text
        if keyword == "import":
            model.imports.extend(t.text for t in statement if t.kind == "string")
        elif keyword in ("source", "explore"):
            source = _scan_source(name, statement[2:], statement[0].line)
            if source is not None:
                model.statements.append((source.name, source.line))
                model.sources.append(source)
        elif keyword in ("query", "run"):
            body = statement[2:]
            if keyword == "query" and len(body) >= 2 and body[0].kind == "word" and body[1].is_("is"):
                model.queries.add(body[0].text)
                body = body[2:]
            head = _run_head(body)
            if head is not None:
                model.runs.append((*head, statement[0].line))
    return model


def _run_head(body: list) -> tuple[str, str | None] | None:
    """``(source, view)`` of ``name -> view`` or ``name -> {...}`` (view ``None``), else ``None``."""

    if len(body) >= 3 and body[0].kind == "word" and body[1].is_("-") and body[2].is_(">"):
        view = body[3].text if len(body) >= 4 and body[3].kind == "word" and not (len(body) > 4 and body[4].is_("+")) else None
        return body[0].text, view
    return None


def _scan_source(file: str, tokens: list, line: int) -> Source | None:
    if not tokens or tokens[0].kind != "word":
        return None  # the reader reports it
    if len(tokens) < 3 or not tokens[1].is_("is"):
        return Source(tokens[0].text, file, line, ("unknown", "a source statement that is not `name is ...`"))
    depths = reader._depths(tokens) or [0] * len(tokens)
    brace = next((i for i, t in enumerate(tokens) if depths[i] == 0 and t.is_("{")), None)
    head = tokens[2 : brace if brace is not None else len(tokens)]
    head = [t for t in head if not (t.kind == "word" and t.text in ("extend", "include", "refine")) and not t.is_("+")]
    source = Source(tokens[0].text, file, line, reader._base(head))
    if brace is None:
        return source
    block = tokens[brace:]
    inner = block[1:-1] if block[-1].is_("}") else block[1:]
    inner_depths = reader._depths(inner) or [0] * len(inner)
    starts = [
        i for i, t in enumerate(inner)
        if inner_depths[i] == 0 and t.kind == "word" and t.text in reader._BLOCK_KEYWORDS and i + 1 < len(inner) and inner[i + 1].is_(":")
    ]
    for n, s in enumerate(starts):
        keyword, body = inner[s].text, inner[s + 2 : starts[n + 1] if n + 1 < len(starts) else len(inner)]
        for name, rhs in _split(keyword, body):
            where = rhs[0].line if rhs else inner[s].line
            item = Field(keyword, name, where, rhs)
            source.fields.append(item)
            if keyword.startswith("join_"):
                source.joins.append(_scan_join(keyword, name, rhs, where))
    return source


def _split(keyword: str, body: list) -> list[tuple[str | None, list]]:
    """The definitions of one statement, as :func:`kumosql.malloy_reader._split` reads them, except that a ``name is`` in the
    middle of a line (``nn is customer_id is not null``) is a comparison, not a new definition."""

    if keyword.startswith("join_"):
        return reader._split(keyword, body)
    depths = reader._depths(body) or [0] * len(body)
    marks = [
        i for i, t in enumerate(body)
        if depths[i] == 0 and t.kind == "word" and i + 1 < len(body) and body[i + 1].is_("is") and not (i and body[i - 1].is_("."))
        and (i == 0 or body[i - 1].is_(",") or reader._starts_line(body, i))
    ]
    if not marks:
        return [(None, body)]
    return [(body[m].text, body[m + 2 : (marks[k + 1] if k + 1 < len(marks) else len(body))]) for k, m in enumerate(marks)]


def _scan_join(keyword: str, name: str | None, rhs: list, line: int) -> Join:
    cut = next((i for i, t in enumerate(rhs) if t.is_("on") or t.is_("with")), len(rhs))
    base, mode, condition = rhs[:cut], (rhs[cut].text if cut < len(rhs) else None), rhs[cut + 1 :]
    join = Join(name or "", keyword.removeprefix("join_"), mode, None, None, line, condition)
    if not base:
        join.source = name or None
        return join
    kind, value = reader._base(base)
    if kind == "table":
        join.table = value
    elif kind == "source":
        join.source = value
    else:
        join.unread = value
    return join


def read_model(files: Sequence[tuple[str, str]], load_import: Callable[[str, str], tuple[str, str] | None] | None = None) -> list[ModelFile]:
    """Scan and resolve every file. ``load_import(importing file, import text)`` returns ``(name, text)`` of an imported file, or ``None``."""

    by_name: dict[str, ModelFile] = {}
    order: list[ModelFile] = []

    def get(name: str, text: str) -> ModelFile:
        if name not in by_name:
            by_name[name] = _scan(name, text)
        return by_name[name]

    for name, text in files:
        order.append(get(name, text))
    # follow imports (read once each; an import of a file that is not read is an unknown of the importing file)
    pending = list(order)
    while pending:
        model = pending.pop()
        for spec in model.imports:
            if spec in model.imported:
                continue
            found = load_import(model.name, spec) if load_import is not None else None
            if found is None:
                model.unknown.append({"what": f"import {reader_clip(spec)}", "reason": "the imported file was not read, so references to its sources are not checked"})
                continue
            fresh = found[0] not in by_name
            model.imported[spec] = get(*found)
            if fresh:
                pending.append(model.imported[spec])
    # what the reader could not read (sql() sources, unbalanced braces, ...); names defined twice, in a cycle or nowhere
    # are findings of their own (duplicate_name, join_cycle, undefined_source)
    for model in order:
        result = reader.read_malloy(model.text)
        model.unknown.extend(u for u in result.unknown if not u["reason"].startswith(("defined twice", "extends ", "joins ")))
    for model in by_name.values():
        visible = _visible(model)
        for source in model.sources:
            if source.base[0] == "source":
                parent = visible.get(source.base[1])
                source.parent = parent if parent is not source else None
    for model in by_name.values():
        for source in model.sources:
            chain = source.chain()
            root = chain[-1]
            source.table = root.base[1] if root.base[0] == "table" and root.parent is None else None
            source.primary_key = next((s.own_key for s in chain if s.own_key), None)
    return order


def reader_clip(text: str, limit: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _visible(model: ModelFile, trail: frozenset = frozenset()) -> dict[str, Source]:
    """The sources a file can use: those of the files it imports, then its own (the last definition of a name)."""

    found: dict[str, Source] = {}
    for other in model.imported.values():
        if other.name not in trail and other is not model:
            found.update(_visible(other, trail | {model.name}))
    for source in model.sources:
        found[source.name] = source
    return found


def _imported_names(model: ModelFile) -> dict[str, str]:
    """``{source name: file}`` of the sources a file imports."""

    names: dict[str, str] = {}
    for other in model.imported.values():
        if other is model:
            continue
        for name, source in _visible(other, frozenset({model.name})).items():
            names.setdefault(name, source.file)
    return names


# ------------------------------------------------------------------------------------------------- findings


@dataclass
class Finding:
    check: str
    status: str
    #: the source the finding is about
    source: str
    #: ``file:line``
    at: str
    what: str
    #: starts with the status words: ``contradicted by ...``, ``unconfirmed: ...``, ``confirmed by ...``
    why: str
    next: str | None = None

    def to_json(self) -> dict:
        out = {"check": self.check, "status": self.status, "source": self.source, "at": self.at, "what": self.what, "why": self.why}
        if self.next:
            out["next"] = self.next
        return out


@dataclass
class Report:
    files: list[str]
    sources: int
    joins: int
    measures: int
    findings: list[Finding]
    unknown: list[dict]
    #: what evidence was available: project, schema snapshot, saved measurements
    evidence: dict

    def counts(self) -> dict[str, int]:
        return {status: sum(1 for f in self.findings if f.status == status) for status in STATUSES}

    @property
    def contradicted(self) -> bool:
        return any(f.status == CONTRADICTED for f in self.findings)

    def to_json(self) -> dict:
        return {
            "checked": {"files": self.files, "sources": self.sources, "joins": self.joins, "measures": self.measures},
            "evidence": self.evidence,
            "counts": self.counts(),
            "findings": [f.to_json() for f in self.findings],
            "unknown": self.unknown,
        }

    def to_text(self) -> str:
        counts = self.counts()
        head = (f"check-malloy: {counts[CONTRADICTED]} contradicted, {counts[UNCONFIRMED]} unconfirmed, {counts[CONFIRMED]} confirmed "
                f"({self.sources} source{'s' if self.sources != 1 else ''} in {len(self.files)} file{'s' if len(self.files) != 1 else ''})")
        lines = [head, "evidence: " + _evidence_words(self.evidence)]
        for item in self.findings:
            lines.append(f"{item.status.upper():<12} {item.check:<17} {item.at}  {item.source}: {item.what}")
            lines.append(f"{'':<13}{item.why}")
            if item.next:
                lines.append(f"{'':<13}next: {item.next}")
        for entry in self.unknown:
            lines.append(f"unknown      {entry['what']}: {entry['reason']}")
        return "\n".join(lines)


def _evidence_words(evidence: dict) -> str:
    parts = [f"project {evidence['project']}" if evidence.get("project") else "no project (--project)"]
    parts.append("schema snapshot" if evidence.get("schema_snapshot") else "no schema snapshot (--schema-snapshot)")
    parts.append(f"{evidence.get('saved_measurements', 0)} saved key measurement{'s' if evidence.get('saved_measurements', 0) != 1 else ''}")
    return ", ".join(parts)


def _cols(columns: Iterable[str]) -> str:
    return ", ".join(columns)


# --------------------------------------------------------------------------------------------- the evidence


class Evidence:
    """What KumoSQL already holds about the tables a model reads. It never queries a warehouse.

    ``session`` is a :class:`~kumosql.mcp_session.Session` over the project (or ``None``), ``snapshot`` a
    :class:`~kumosql.schema_snapshot.SchemaSnapshot` (or ``None``) and ``observations`` the saved key measurements.
    ``assumed`` is ``--assume-grain`` when there is no project (with one, the session applies it).
    """

    def __init__(self, session=None, snapshot=None, observations: Sequence = (), assumed: dict | None = None, surface: str = "cli") -> None:
        self.session = _without_malloy_claims(session) if session is not None and session.configured else None
        self.pipeline = self.session.pipeline() if self.session is not None else None
        if self.pipeline is None:
            self.session = None
        self.snapshot = snapshot
        self.observations = list(observations)
        self.assumed = dict(assumed or {})
        self.surface = surface
        self._key_memo: dict[str, str | None] = {}

    def describe(self) -> dict:
        return {
            "project": (str(self.session.root) if self.session is not None and self.session.root else ("loaded" if self.session is not None else None)),
            "schema_snapshot": self.snapshot is not None,
            "saved_measurements": len(self.observations),
        }

    # ------------------------------------------------------------------ names

    def key(self, table: str) -> str | None:
        """The project's key for a table as the model writes it (``dataset.table``, any case), or ``None``."""

        if self.session is None:
            return None
        if table not in self._key_memo:
            key, _close = self.session.resolve(table)
            self._key_memo[table] = key
        return self._key_memo[table]

    def names(self, table: str) -> list[str]:
        key = self.key(table)
        return [n for n in dict.fromkeys((key, table)) if n]

    def label(self, table: str) -> str:
        key = self.key(table)
        return ".".join(key.split(".")[-2:]) if key else table

    # ------------------------------------------------------------------ grain

    def grains(self, table: str) -> list[tuple[tuple[str, ...], str]]:
        """``(keys, origin in words)`` for each grain the project shows or states for ``table``; never a Malloy key."""

        from . import join_profile_store as joins

        found: list[tuple[tuple[str, ...], str]] = []
        key = self.key(table)
        if self.session is not None and key is not None:
            pipeline = self.pipeline
            if key in pipeline.models:
                profile = self.session.profiles().get(key)
                grain = profile.grain if profile is not None else None
                if grain is not None and grain.known:
                    name, keys = key.split(".")[-1], _cols(grain.keys) or "a single row"
                    if grain.status == "derived":
                        found.append((tuple(grain.keys), f"the query of model {name}, whose grain is ({keys})"))
                    else:
                        found.append((tuple(grain.keys), f"{self.session.grain_origin(key, grain) or 'a declared grain'} ({keys})"))
            else:
                claim = self.session.grain_claim(key)
                if claim is not None:
                    found.append((tuple(claim.keys), f"{claim.origin} ({_cols(claim.keys)})"))
        elif self.session is not None:
            for claim in self.session.grain_claims().values():
                if not claim.in_project and joins.same_table(claim.table, table):
                    found.append((tuple(claim.keys), f"{claim.origin} ({_cols(claim.keys)})"))
                    break
        else:
            for name, columns in self.assumed.items():
                if joins.same_table(name, table):
                    found.append((tuple(columns), f"--assume-grain ({_cols(columns)})"))
                    break
        return found

    def measured(self, table: str, columns: Sequence[str]):
        """The saved measurement that decides whether ``columns`` repeat in ``table`` (:func:`kumosql.join_profile_store.lookup`)."""

        from . import join_profile_store as joins

        return joins.lookup(self.observations, self.names(table), columns)

    # ------------------------------------------------------------------ columns

    def columns(self, table: str) -> list[tuple[set[str], str]]:
        """``(casefolded column names, where the list comes from)`` for each complete column list of ``table``."""

        found: list[tuple[set[str], str]] = []
        key = self.key(table)
        if self.session is not None and key is not None:
            pipeline = self.pipeline
            if key in pipeline.models:
                outputs = pipeline.output_columns(key)
                if outputs and "*" not in outputs:
                    found.append(({c.casefold() for c in outputs}, f"the output columns of model {key.split('.')[-1]}"))
            else:
                declared = pipeline.source_schema.get(key) or {}
                if declared:
                    found.append(({c.casefold() for c in declared}, f"the source schema of {self.label(table)}"))
        if self.snapshot is not None:
            columns = self.snapshot.columns_of(table) or (self.snapshot.columns_of(key) if key else None)
            if columns:
                found.append(({c.column.casefold() for c in columns}, "the schema snapshot"))
        return found

    # ------------------------------------------------------------------ ratios

    def ratio(self, table: str, column: str) -> tuple[str, str] | None:
        """``(strength, words)`` when the project shows ``table.column`` is a ratio: ``aggregates`` (a ratio of aggregates),
        ``row`` (a division of row values) or ``declared`` (a declared scale); ``None`` when it shows nothing."""

        key = self.key(table)
        if self.session is None or key is None:
            return None
        pipeline = self.pipeline
        if key in pipeline.models:
            profile = self.session.profiles().get(key)
            meaning = profile.attribute(column) if profile is not None else None
            text = meaning.meaning if meaning is not None and meaning.status == "known" else None
            if text and text.startswith("expr:") and (" / " in text or "SAFE_DIVIDE(" in text.upper() or "DIV(" in text.upper()):
                from .mcp_tools import short_meaning

                parts = re.split(r" / |SAFE_DIVIDE\(|DIV\(", text[5:], flags=re.IGNORECASE)
                strength = "aggregates" if len(parts) >= 2 and all(p.strip(" ,()").startswith("agg:") for p in parts[:2]) else "row"
                return strength, f"the lineage of model {key.split('.')[-1]}: {column} is {reader_clip(short_meaning(text[5:]), 90)}"
        try:
            from . import column_meaning

            scale = column_meaning.declared(pipeline, key, column).get("scale")
        except Exception:  # noqa: BLE001 - declared attributes are optional evidence
            scale = None
        if scale:
            return "declared", f"--column-attributes declares {column} on the scale {scale}"
        return None

    # ------------------------------------------------------------------ commands to measure

    def measure_key(self, table: str, columns: Sequence[str], left: str | None = None, pairs: Sequence[tuple[str, str]] = ()) -> str:
        """The opt-in command that would measure whether ``columns`` repeat in ``table`` (it reaches BigQuery only when run)."""

        key = self.key(table)
        full = key or (table if table.count(".") >= 2 else f"PROJECT.{table}")
        names = ",".join(columns)
        keys = ",".join(a if a == b else f"{a}={b}" for a, b in pairs)
        join_call = f"join_profile left={left or 'BASE_TABLE'} right={self.label(table)} keys={keys} project=BILLING_PROJECT run=true"
        if self.surface == "mcp":
            if len(columns) == 1 or not pairs:
                return f"profile_table table={self.label(table)} columns={names} project=BILLING_PROJECT (a dry run; add run=true to measure, then check_malloy again)"
            return join_call
        # key-check answers at once but saves nothing; profile-table and join_profile save what the next check-malloy reads
        saved = (f"python -m kumosql profile-table {full} --columns {names}" if len(columns) == 1 or not pairs
                 else f"{join_call} on python -m kumosql mcp")
        return f"python -m kumosql key-check {full} --key {names} --dry-run, then without --dry-run (it saves nothing; {saved} saves a measurement the next check-malloy reads)"


def _without_malloy_claims(session):
    """The session, or a sibling over the same project without ``--malloy`` folders: a Malloy key must not confirm itself."""

    if not getattr(session, "_malloy", ()):
        return session
    from .mcp_session import Session

    def sibling():
        return Session(session.pipeline(), profiles=session.profiles_directory, assume_grain=session._assume_grain, root=session.root,
                       schema_snapshot=session.schema_snapshot(), docs=session._docs)

    return session._once("malloy_check_evidence", sibling)


# ------------------------------------------------------------------------------------------------- the checks


def check(files: Sequence[tuple[str, str]], evidence: Evidence | None = None, load_import: Callable[[str, str], tuple[str, str] | None] | None = None) -> Report:
    """Check the Malloy files ``(name, text)`` against ``evidence`` (none: structural checks and unconfirmed keys only)."""

    evidence = evidence or Evidence()
    models = read_model(files, load_import)
    findings: list[Finding] = []
    unknown: list[dict] = []
    for model in models:
        unknown.extend({"what": f"{model.name}: {u['what']}", "reason": u["reason"]} for u in model.unknown)
        findings += _structural(model)
    checker = _Evidence(evidence, findings, unknown)
    for model in models:
        visible = _visible(model)
        for source in _last_definitions(model):
            checker.source(source, visible)
    rank = {status: i for i, status in enumerate(STATUSES)}

    def place(f: Finding) -> tuple:
        file, _, line = f.at.rpartition(":")
        return rank[f.status], list(CHECKS).index(f.check), file, int(line) if line.isdigit() else 0

    findings.sort(key=place)
    sources = sum(len(_last_definitions(m)) for m in models)
    joins = sum(len(s.joins) for m in models for s in m.sources)
    measures = sum(1 for m in models for s in m.sources for f in s.fields if f.kind == "measure")
    return Report([m.name for m in models], sources, joins, measures, findings, _dedupe(unknown), evidence.describe())


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        mark = (item["what"], item["reason"])
        if mark not in seen:
            seen.add(mark)
            out.append(item)
    return out


def _last_definitions(model: ModelFile) -> list[Source]:
    last = {source.name: source for source in model.sources}
    return [source for source in model.sources if last[source.name] is source]


# ------------------------------------------------------------------ structural


def _structural(model: ModelFile) -> list[Finding]:
    found: list[Finding] = []
    text = "contradicted by the model text"
    imported = _imported_names(model)
    unread_imports = any(spec not in model.imported for spec in model.imports)

    # a source name defined twice in the file, or both here and in an imported file
    first: dict[str, int] = {}
    for name, line in model.statements:
        if name in first:
            found.append(Finding("duplicate_name", CONTRADICTED, name, f"{model.name}:{line}", f"source {name} is defined again",
                                 f"{text}: {name} is already defined on line {first[name]}; Malloy rejects a second definition", "Rename one of the sources."))
        elif name in imported:
            found.append(Finding("duplicate_name", CONTRADICTED, name, f"{model.name}:{line}", f"source {name} is also imported",
                                 f"{text}: {name} is already defined in {imported[name]}, which this file imports", "Rename the source, or use the imported one."))
        first.setdefault(name, line)

    # names used before or without being defined: what a source extends or joins, and the head of run:/query:
    defined_at = {name: line for name, line in reversed(model.statements)}  # the first definition
    edges: dict[str, set[str]] = {}
    later: list[tuple[Source, str, str, int]] = []
    for source in model.sources:
        uses = []
        if source.base[0] == "source":
            uses.append(("built on", source.base[1], source.line))
        uses += [(f"join_{j.kind} {j.name} of", j.source, j.line) for j in source.joins if j.source]
        for how, target, line in uses:
            if target in defined_at:
                edges.setdefault(source.name, set()).add(target)
                if target == source.name or defined_at[target] >= source.line:
                    later.append((source, how, target, line))
                continue
            if target in imported or target in model.queries or unread_imports:
                continue  # an unread import may define it (listed under unknown)
            found.append(Finding("undefined_source", CONTRADICTED, source.name, f"{model.name}:{line}", f"{how} {target}",
                                 f"{text}: no source named {target} is defined in this file or the files it imports",
                                 f"Define {target} before {source.name}, import the file that defines it, or fix the name."))
    for head, view, line in model.runs:
        if head in defined_at and defined_at[head] > line:
            found.append(Finding("undefined_source", CONTRADICTED, head, f"{model.name}:{line}", f"run on {head} before it is defined",
                                 f"{text}: {head} is defined only later, on line {defined_at[head]}; Malloy reads a file from the top", f"Move the run below {head}."))
        elif head not in defined_at and head not in imported and head not in model.queries and not unread_imports:
            found.append(Finding("undefined_source", CONTRADICTED, head, f"{model.name}:{line}", f"run on {head}",
                                 f"{text}: no source or query named {head} is defined in this file or the files it imports", "Fix the source name."))
    group: dict[str, int] = {}
    for n, cycle in enumerate(_cycles(edges)):
        group.update({name: n for name in cycle})
        name = min(cycle, key=lambda c: defined_at.get(c, 0))
        ring = " -> ".join([*cycle, cycle[0]]) if len(cycle) > 1 else f"{name} -> {name}"
        what = f"{name} joins or extends itself" if len(cycle) == 1 else f"cycle {ring}"
        found.append(Finding("join_cycle", CONTRADICTED, name, f"{model.name}:{defined_at.get(name, 0)}", what,
                             f"{text}: a source can only use sources defined before it, so {ring} can never all be defined",
                             "Break the cycle: join from one side only, or build the second source on a source defined earlier."))
    for source, how, target, line in later:
        if target != source.name and not (source.name in group and group.get(target) == group[source.name]):
            found.append(Finding("undefined_source", CONTRADICTED, source.name, f"{model.name}:{line}", f"{how} {target}, before {target} is defined",
                                 f"{text}: {target} is defined only later, on line {defined_at[target]}; Malloy reads a file from the top",
                                 f"Move {target} above {source.name}."))

    visible = _visible(model)

    def usable(name: str, line: int) -> bool:
        return name in imported or (name in defined_at and defined_at[name] < line)

    for source in model.sources:
        found += _source_structure(model, source, visible, usable)
    for head, view, line in model.runs:
        target = visible.get(head)
        if target is not None and view and view not in _views(target) and not _unreadable(target):
            found.append(Finding("undefined_field", CONTRADICTED, head, f"{model.name}:{line}", f"run: {head} -> {view}",
                                 f"{text}: {head} has no view named {view}", f"Name one of the views of {head}, or define {view}."))
    return found


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """The strongly connected components with a cycle (two or more members, or one that points to itself), members in order."""

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    found: list[list[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on.add(node)
        for nxt in sorted(edges.get(node, ())):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            group = []
            while True:
                item = stack.pop()
                on.discard(item)
                group.append(item)
                if item == node:
                    break
            if len(group) > 1 or node in edges.get(node, ()):
                found.append(sorted(group))

    for node in sorted(edges):
        if node not in index:
            visit(node)
    return found


def _views(source: Source) -> set[str]:
    return {f.name for s in source.chain() for f in s.fields if f.kind == "view" and f.name}


def _measures(source: Source) -> set[str]:
    return {f.name for s in source.chain() for f in s.fields if f.kind == "measure" and f.name}


def _unreadable(source: Source) -> bool:
    """Whether some of what the source inherits could not be read (its fields are then not all known)."""

    return any(s.base[0] == "unknown" for s in source.chain()) or (source.chain()[-1].base[0] == "source")


def _source_structure(model: ModelFile, source: Source, visible: dict[str, Source], usable: Callable[[str, int], bool]) -> list[Finding]:
    found: list[Finding] = []
    text = "contradicted by the model text"
    # a name defined twice in one source (dimension, measure, view and join names share one namespace)
    seen: dict[str, tuple[str, int]] = {}
    for item in source.fields:
        if not item.name or item.kind not in _NAMED:
            continue
        if item.name in seen:
            kind, line = seen[item.name]
            found.append(Finding("duplicate_name", CONTRADICTED, source.name, f"{model.name}:{item.line}", f"{item.kind} {item.name} is defined twice",
                                 f"{text}: {source.name} already has a {kind} named {item.name} (line {line}); Malloy rejects a redefinition",
                                 "Rename one of them (a join takes an alias: join_one: other_name is source with key)."))
        else:
            seen[item.name] = (item.kind, item.line)
    # a name the source it extends already has, unless except: (or accept: without it) removes it first
    parent = source.parent
    if parent is not None and not _unreadable(parent):
        inherited = _available(source, _field_names(parent))
        for name, (kind, line) in seen.items():
            if name in inherited:
                found.append(Finding("duplicate_name", CONTRADICTED, source.name, f"{model.name}:{line}", f"{kind} {name} redefines a field of {parent.name}",
                                     f"{text}: {parent.name} ({parent.at}), which {source.name} extends, already defines {name}; Malloy rejects a redefinition",
                                     f"Rename it, or remove the inherited one first with except: {name}."))
    # a join with ``with`` needs the joined source's primary_key; join_many cannot use ``with`` at all
    for join in source.joins:
        if join.mode != "with":
            continue
        if join.kind == "many":
            found.append(Finding("join_many_with", CONTRADICTED, source.name, f"{model.name}:{join.line}", f"join_many {join.name} with {_with_text(join)}",
                                 f"{text}: `with` matches one row by the joined source's primary_key, which a join_many cannot use",
                                 f"Write join_many: {join.name} on <this column> = {join.name}.<its column>, or use join_one if it matches one row."))
            continue
        if join.table is not None:
            found.append(Finding("join_without_key", CONTRADICTED, source.name, f"{model.name}:{join.line}", f"join_{join.kind} {join.name} with ... over table('{join.table}')",
                                 f"{text}: a table joined inline has no primary_key, and `with` matches the key to the joined source's primary_key",
                                 "Define a source for the table with a primary_key, or write the join with `on`."))
            continue
        target = visible.get(join.source or "")
        if target is None or target is source or _unreadable(target) or not usable(join.source or "", join.line):
            continue
        if not target.primary_key:
            found.append(Finding("join_without_key", CONTRADICTED, source.name, f"{model.name}:{join.line}", f"join_{join.kind} {join.name} with {_with_text(join)}",
                                 f"{text}: {target.name} ({target.at}) has no primary_key, which `with` needs",
                                 f"Add primary_key to {target.name}, or write the join with `on`."))
    # a bare name in aggregate: must be a measure, and in nest: or `view: v is x + {...}` a view
    measures, views = _measures(source), _views(source)
    if not _unreadable(source):
        for item in source.fields:
            if item.kind != "view":
                continue
            for label, names, kind, known in (("aggregate", _label_words(item.tokens, "aggregate"), "measure", measures),
                                              ("nest", _label_words(item.tokens, "nest"), "view", views)):
                for word, line in names:
                    if word not in known:
                        found.append(Finding("undefined_field", CONTRADICTED, source.name, f"{model.name}:{line}", f"{label}: {word} in view {item.name}",
                                             f"{text}: {source.name} has no {kind} named {word}", f"Fix the name, or define the {kind} {word}."))
            base = _view_base(item.tokens)
            if base and base not in views:
                found.append(Finding("undefined_field", CONTRADICTED, source.name, f"{model.name}:{item.line}", f"view {item.name} is {base} + ...",
                                     f"{text}: {source.name} has no view named {base}", f"Fix the name, or define the view {base}."))
    return found


#: statements whose name is a field of the source (they share one namespace)
_NAMED = ("dimension", "measure", "view", "join_one", "join_many", "join_cross")


def _words(source: Source, kind: str) -> set[str] | None:
    """The names listed by ``accept:`` or ``except:`` in this source, or ``None`` when it has no such statement."""

    items = [item for item in source.fields if item.kind == kind]
    if not items:
        return None
    return {t.text for item in items for t in item.tokens if t.kind == "word"}


def _available(source: Source, names: set[str]) -> set[str]:
    """Which of the inherited ``names`` (fields or columns) ``source`` still has after its own ``accept:`` and ``except:``."""

    accepted, excepted = _words(source, "accept"), _words(source, "except") or set()
    return {n for n in names if n not in excepted and (accepted is None or n in accepted)}


def _field_names(source: Source) -> set[str]:
    """The fields a source has: its own and those it inherits (after ``accept:`` / ``except:``), never its table's columns."""

    chain = source.chain()
    names: set[str] = set()
    for s in reversed(chain):
        names = _available(s, names) if s is not chain[-1] else names
        names |= {item.name for item in s.fields if item.name and item.kind in _NAMED}
        names |= {item.name for item in s.fields if item.name and item.kind == "rename"}
    return names


def _with_text(join: Join) -> str:
    return " ".join(t.text for t in join.condition[:3]) or "?"


def _label_words(tokens: list, label: str) -> list[tuple[str, int]]:
    """The items of ``label:`` (at any depth of a view) that are one bare name, as ``(name, line)``."""

    depths = reader._depths(tokens) or [0] * len(tokens)
    found = []
    for i, token in enumerate(tokens):
        if not (token.kind == "word" and token.text == label and i + 1 < len(tokens) and tokens[i + 1].is_(":") and not (i + 2 < len(tokens) and tokens[i + 2].is_(":"))):
            continue
        level, item = depths[i], []
        j = i + 2
        while j <= len(tokens):
            end = j == len(tokens) or depths[j] < level or (depths[j] == level and tokens[j].kind == "word" and j + 1 < len(tokens) and tokens[j + 1].is_(":") and not (j + 2 < len(tokens) and tokens[j + 2].is_(":")))
            split = not end and depths[j] == level and (tokens[j].is_(",") or (item and reader._starts_line(tokens, j)))
            if end or split:
                if len(item) == 1 and item[0].kind == "word" and item[0].text.lower() not in reader._WORDS:
                    found.append((item[0].text, item[0].line))
                item = []
                if end:
                    break
                if tokens[j].is_(","):
                    j += 1
                    continue
            item.append(tokens[j])
            j += 1
    return found


def _view_base(tokens: list) -> str | None:
    """``x`` of ``view: v is x + {...}`` or ``view: v is x`` (the view a view refines), else ``None``."""

    if tokens and tokens[0].kind == "word" and (len(tokens) == 1 or tokens[1].is_("+")) and tokens[0].text.lower() not in reader._WORDS:
        return tokens[0].text
    return None


# ------------------------------------------------------------------ against the evidence


class _Evidence:
    def __init__(self, evidence: Evidence, findings: list[Finding], unknown: list[dict]) -> None:
        self.ev, self.findings, self.unknown = evidence, findings, unknown

    def add(self, *args, **kwargs) -> None:
        self.findings.append(Finding(*args, **kwargs))

    def source(self, source: Source, visible: dict[str, Source]) -> None:
        if source.table is None:
            root = source.chain()[-1]
            if root.base[0] == "unknown":
                why = root.base[1]
            elif root.base[0] == "source":
                why = f"it is built on {root.base[1]}, which is a query, is not defined, or is in a cycle"
            else:
                why = "its table could not be read"
            self.unknown.append({"what": f"{source.at} {source.name}", "reason": f"{why}, so its key and columns are not checked"})
            return
        self._primary_key(source)
        joins = source.all_joins()
        for join in source.joins:
            if join.kind == "one":
                self._join_one(source, join, visible)
        self._columns(source, visible, joins)
        self._ratios(source, visible, joins)

    # -- the key

    def _judge(self, table: str, columns: Sequence[str], filtered: bool) -> tuple[str, str, list[str]]:
        """``(status, why, notes)`` for whether ``columns`` identify a row of ``table``."""

        wanted = {c.casefold() for c in columns}
        label = self.ev.label(table)
        against: list[str] = []
        agree: list[str] = []
        notes: list[str] = []
        for keys, origin in self.ev.grains(table):
            grain = {k.casefold() for k in keys}
            if not grain:
                agree.append(f"{origin}: the table has a single row")
            elif grain == wanted:
                agree.append(origin)
            elif grain < wanted:
                agree.append(f"{origin}, which ({_cols(columns)}) contains")
            elif wanted < grain:
                extra = _cols(k for k in keys if k.casefold() not in wanted)
                against.append(f"{origin}: one row per ({_cols(keys)}), so {_cols(columns)} repeats wherever one {_cols(columns)} has several {extra}")
            else:
                notes.append(f"the grain of {label} is ({_cols(keys)}) by {origin}; ({_cols(columns)}) is another set of columns that nothing shows unique")
        verdict = self.ev.measured(table, columns)
        measured_unique = verdict.kind == "unique"
        if verdict.kind in ("unique", "repeats"):
            o = verdict.observation
            said = f"the saved {'join profile' if o.source == 'join_profile' else 'data profile'} of {o.generated_at[:10]}"
            if verdict.kind == "unique":
                agree.append(f"{said}: no repeats on ({_cols(o.columns)}) in {o.rows:,} rows (a measurement, assumed to still hold)")
            else:
                against.append(f"{said}: ({_cols(o.columns)}) has {o.counts()}")
            if o.null_rows:
                notes.append(f"{o.null_rows:,} rows have a null key in {said}")
        elif verdict.kind == "wider":
            o = verdict.observation
            notes.append(f"measured only on the longer key ({_cols(o.columns)}) on {o.generated_at[:10]}, which says nothing about ({_cols(columns)}) alone")
        if against and filtered:
            notes.insert(0, f"whole-table evidence ({against[0]}) does not decide a source that keeps only some rows (where:)")
            against = []
        if against and measured_unique and not any(a.startswith("the saved ") for a in against):
            # the grain allows repeats, the data had none when it was measured: neither alone decides
            return UNCONFIRMED, f"unconfirmed: {agree[-1]}, but {against[0]}", notes
        if against:
            why = f"contradicted by {against[0]}"
            if agree:
                why += f"; though {agree[0]}"
            return CONTRADICTED, why, notes
        if agree:
            return CONFIRMED, f"confirmed by {agree[0]}", notes
        if notes:
            return UNCONFIRMED, f"unconfirmed: {notes[0]}", notes[1:]
        return UNCONFIRMED, f"unconfirmed: no grain evidence for {label} (no derived or declared grain, no saved measurement of ({_cols(columns)}))", notes

    def _primary_key(self, source: Source) -> None:
        own = source.own_key
        if not own:
            return  # no key, or one inherited from the source it extends (checked there)
        inherited = source.parent.primary_key if source.parent is not None else None
        if inherited and inherited.casefold() == own.casefold():
            return
        status, why, notes = self._judge(source.table, [own], source.filtered)
        what = f"primary_key {own} of {self.ev.label(source.table)}"
        if source.parent is not None:
            what += f" (built on {source.parent.name})"
        line = next((f.line for f in source.fields if f.kind == "primary_key"), source.line)
        fix = None
        if status == CONTRADICTED:
            fix = f"Use the table's real key as primary_key, or reduce {self.ev.label(source.table)} to one row per {own} before Malloy reads it; until then measures that cross a join can be wrong."
        elif status == UNCONFIRMED:
            fix = self.ev.measure_key(source.table, [own])
        self.add("primary_key", status, source.name, f"{source.file}:{line}", what, _with_notes(why, notes), fix)

    # -- join_one

    def _join_one(self, source: Source, join: Join, visible: dict[str, Source]) -> None:
        target = visible.get(join.source or "") if join.source else None
        table = join.table or (target.table if target is not None else None)
        if table is None:
            if join.unread or target is not None:
                self.unknown.append({"what": f"{source.file}:{join.line} join_one {join.name}", "reason": "the joined table is not known, so whether the join fans out is not checked"})
            return
        if join.mode == "with":
            if target is None or not target.primary_key:
                return  # reported as join_without_key (or the target is undefined)
            columns, pairs = [target.primary_key], [(_with_text(join), target.primary_key)]
            filtered = target.filtered
        elif join.mode == "on":
            pairs, extra = _equalities(join)
            if pairs is None:
                self.add("join_fan_out", UNCONFIRMED, source.name, f"{source.file}:{join.line}", f"join_one {join.name} on ...",
                         f"unconfirmed: the condition is not only column equalities ({extra}), so no key measurement decides it",
                         "Check the join by hand, or write it as equalities of columns.")
                return
            columns = [b for _, b in pairs]
            filtered = target.filtered if target is not None else False
            if extra:
                filtered = True  # a condition that also pins a constant keeps only some joined rows
        else:
            return
        status, why, notes = self._judge(table, columns, filtered)
        what = f"join_one {join.name} to {self.ev.label(table)} on ({_cols(columns)})"
        fix = None
        if status == CONTRADICTED:
            why += f"; Malloy expects at most one {join.name} row per {source.name} row, so {source.name}'s measures count repeated rows"
            fix = f"Use join_many for {join.name}, or join a source that has one row per ({_cols(columns)})."
        elif status == UNCONFIRMED:
            fix = self.ev.measure_key(table, columns, left=self.ev.label(source.table), pairs=pairs)
        self.add("join_fan_out", status, source.name, f"{source.file}:{join.line}", what, _with_notes(why, notes), fix)

    # -- columns

    def _columns(self, source: Source, visible: dict[str, Source], joins: dict[str, Join]) -> None:
        local = _local_names(source)
        # a name that must be a measure or a view is reported as undefined_field, never as a missing column
        for item in source.fields:
            if item.kind == "view":
                local |= {w for w, _ in _label_words(item.tokens, "aggregate") + _label_words(item.tokens, "nest")}
                local |= {_view_base(item.tokens)} - {None}
        folded = {n.casefold() for n in local}
        own: dict[str, int] = {}
        through: dict[tuple[str, str], tuple[str, int]] = {}  # (table, column) -> (join name, line)
        for item in source.fields:
            if item.kind in ("join_one", "join_many", "join_cross"):
                tokens = _condition_tokens(source, item)
            elif item.kind in ("rename", "accept", "except", "declare", "timezone"):
                tokens = item.tokens if item.kind in ("rename", "accept", "except") else []
            else:
                tokens = item.tokens
            for word in _refs(tokens):
                if word.casefold() not in folded:
                    own.setdefault(word, item.line)
            for join_name, column, line in _paths(tokens, joins):
                join = joins[join_name]
                target = visible.get(join.source or "") if join.source else None
                if target is not None and column in _local_names(target):
                    continue
                table = join.table or (target.table if target is not None else None)
                if table is not None and not (target is not None and _unreadable(target) and target.table is None):
                    through.setdefault((table, column), (join_name, line))
        self._fields_named_like_columns(source)
        groups: dict[str, dict[str, int]] = {}
        if own:
            groups[source.table] = dict(own)
        for (table, column), (_name, line) in through.items():
            groups.setdefault(table, {}).setdefault(column, line)
        for table, names in groups.items():
            lists = self.ev.columns(table)
            label = self.ev.label(table)
            line = min(names.values())
            at = f"{source.file}:{line}"
            if not lists:
                self.add("missing_column", UNCONFIRMED, source.name, at, f"{len(names)} column{'s' if len(names) != 1 else ''} of {label} not checked",
                         f"unconfirmed: no column list for {label}", "Pass --schema-snapshot (INFORMATION_SCHEMA.COLUMNS exported once) or a --project that builds or declares the table with its columns."
                         if self.ev.surface == "cli" else "Start the server with --schema-snapshot, or declare the table's columns with --source-schema.")
                continue
            missing: dict[str, list[str]] = {}
            for name in names:
                for columns, origin in lists:
                    if name.casefold() not in columns:
                        missing.setdefault(name, []).append(origin)
            if missing:
                gone = sorted(missing, key=lambda n: names[n])
                origins = sorted({o for n in gone for o in missing[n]})
                present = [o for _c, o in lists if o not in origins]
                why = f"contradicted by {' and '.join(origins)}: {reader_clip(_cols(gone), 120)} not among the columns of {label}"
                if present:
                    why += f" (though {present[0]} has {'them' if len(gone) > 1 else 'it'})"
                self.add("missing_column", CONTRADICTED, source.name, f"{source.file}:{names[gone[0]]}",
                         f"{len(gone)} of {len(names)} column{'s' if len(names) != 1 else ''} read from {label} missing", why,
                         "Fix the name, define it as a dimension, or update the snapshot if the table changed.")
            else:
                self.add("missing_column", CONFIRMED, source.name, at, f"{len(names)} column{'s' if len(names) != 1 else ''} read from {label}",
                         f"confirmed by {' and '.join(o for _c, o in lists)}")

    def _fields_named_like_columns(self, source: Source) -> None:
        """A field of the source with the name of a column its table still has: Malloy rejects it as a redefinition."""

        lists = self.ev.columns(source.table)
        if not lists:
            return
        for item in source.fields:
            if not item.name or item.kind not in _NAMED:
                continue
            hits = [origin for columns, origin in lists if item.name.casefold() in _columns_left(source, columns)]
            if hits:
                self.add("duplicate_name", CONTRADICTED, source.name, f"{source.file}:{item.line}", f"{item.kind} {item.name} has the name of a column",
                         f"contradicted by {hits[0]}: {self.ev.label(source.table)} has a column {item.name}, and Malloy rejects a field that redefines a column",
                         f"Rename the {item.kind}, or remove the column first with except: {item.name}.")

    # -- ratio measures

    def _ratios(self, source: Source, visible: dict[str, Source], joins: dict[str, Join]) -> None:
        for item in source.fields:
            if item.kind != "measure":
                continue
            for function, path in _aggregates(item.tokens):
                self._ratio(source, item, function, path, visible, joins)

    def _ratio(self, source: Source, item: Field, function: str, path: list[str], visible: dict[str, Source], joins: dict[str, Join]) -> None:
        owner, column, table = source, path[-1], source.table
        if len(path) == 2:
            join = joins.get(path[0])
            if join is None:
                return
            owner = visible.get(join.source or "") if join.source else None
            table = join.table or (owner.table if owner is not None else None)
        elif len(path) != 1:
            return
        evidence = None
        defined = owner.field_of(column) if owner is not None else None
        if defined is not None:
            if defined.kind != "dimension":
                return
            if not _divides(defined.tokens):
                return
            evidence = ("model", f"the model defines {column} as {reader_clip(_text(defined.tokens), 60)}")
        verdict = ratio_verdict(self.ev, table, column, function) if table is not None and defined is None else None
        what = f"measure {item.name} is {column}.{function}()" if function != "avg" else f"measure {item.name} averages {column}"
        at = f"{source.file}:{item.line}"
        if verdict is not None:
            self.add("ratio_measure", verdict[0], source.name, at, what, verdict[1], verdict[2] if len(verdict) > 2 else None)
            return
        if evidence is None and table is not None:
            evidence = self.ev.ratio(table, column)
        if evidence is None and _RATIO_NAME.search(column):
            evidence = ("name", f"the name {column} suggests a ratio")
        if evidence is None:
            return
        strength, words = evidence
        fix = "Sum the numerator and the denominator and divide: measure: x is a.sum() / b.sum()."
        if function == "sum" and strength in ("aggregates", "row", "declared", "model"):
            self.add("ratio_measure", CONTRADICTED, source.name, at, what,
                     f"contradicted by {words}: a sum of ratios is not a ratio of anything", fix)
        elif function == "avg" and strength == "aggregates":
            self.add("ratio_measure", CONTRADICTED, source.name, at, what,
                     f"contradicted by {words}: each row's ratio is already an aggregate, and avg() weighs every row the same, so it is not the ratio over the rows it covers", fix)
        else:
            effect = "if it is one, a sum of ratios is not a ratio" if function == "sum" else "avg() of a ratio is right only if every row should count the same"
            self.add("ratio_measure", UNCONFIRMED, source.name, at, what, f"unconfirmed: {words}; {effect} ({_detector_state()})", fix)


def _with_notes(why: str, notes: list[str]) -> str:
    return why + "".join(f"; {note}" for note in notes[:1])


# ------------------------------------------------------------------ the ratio detector hook


def _detector_state() -> str:
    try:
        from . import ratio_check  # type: ignore[attr-defined]  # noqa: F401
    except ImportError:
        return "the ratio re-aggregation detector, kumosql.ratio_check, is not in this version"
    return "the ratio re-aggregation detector gave no verdict"


def ratio_verdict(evidence: Evidence, table: str, column: str, function: str) -> tuple | None:
    """HOOK for the ratio re-aggregation detector, ``kumosql.ratio_check``, which is being built on another branch.

    When that module exists and offers ``malloy_verdict(session, table_key, column, function)`` returning ``None`` (no
    opinion) or ``(status, why[, next])`` with a status of :data:`STATUSES`, its verdict is used for a measure that applies
    ``function`` (``sum`` or ``avg``) to ``table.column``. Until then this returns ``None`` and the fallback in
    ``_Evidence._ratio`` decides from lineage, declared attributes and names.
    """

    try:
        from . import ratio_check  # type: ignore[attr-defined]
    except ImportError:
        return None
    judge = getattr(ratio_check, "malloy_verdict", None)
    if judge is None or evidence.session is None:
        return None
    key = evidence.key(table)
    if key is None:
        return None
    try:
        verdict = judge(evidence.session, key, column, function)
    except Exception:  # noqa: BLE001 - a detector failure must not stop the check
        return None
    if verdict is None or not isinstance(verdict, tuple) or verdict[0] not in STATUSES:
        return None
    return verdict


# ------------------------------------------------------------------ token helpers


def _refs(tokens: list) -> list[str]:
    """Names an expression reads (see :func:`kumosql.malloy_reader._references`), without regular expressions ``r'...'`` and Malloy words."""

    kept = [t for i, t in enumerate(tokens) if not (t.kind == "word" and t.text == "r" and i + 1 < len(tokens) and tokens[i + 1].kind == "string")]
    return [w for w in reader._references(kept) if w.lower() not in _MORE_WORDS]


def _columns_left(source: Source, columns: set[str]) -> set[str]:
    """The casefolded table ``columns`` a source still has after the ``accept:`` and ``except:`` of its chain."""

    left = set(columns)
    for s in reversed(source.chain()):
        accepted, excepted = _words(s, "accept"), _words(s, "except") or set()
        left = {c for c in left if c not in {w.casefold() for w in excepted} and (accepted is None or c in {w.casefold() for w in accepted})}
    return left


def _local_names(source: Source) -> set[str]:
    """Every name a source (or one it extends) defines: fields, joins, views and the names a view defines with ``is``."""

    names: set[str] = set()
    for s in source.chain():
        for item in s.fields:
            if item.name:
                names.add(item.name)
            names.update(t.text for i, t in enumerate(item.tokens) if t.kind == "word" and i + 1 < len(item.tokens) and item.tokens[i + 1].is_("is"))
        names.update(j.name for j in s.joins if j.name)
    return names


def _condition_tokens(source: Source, item: Field) -> list:
    join = next((j for j in source.joins if j.line == item.line and j.name == (item.name or "")), None)
    return join.condition if join is not None else []


def _paths(tokens: list, joins: dict[str, Join]) -> list[tuple[str, str, int]]:
    """``(join name, field, line)`` for each ``join.field`` read through a join of the source (not ``join.count()``)."""

    found = []
    for i in range(len(tokens) - 2):
        a, dot, b = tokens[i], tokens[i + 1], tokens[i + 2]
        if a.kind != "word" or a.text not in joins or not dot.is_(".") or b.kind != "word":
            continue
        if i and tokens[i - 1].is_("."):
            continue
        if i + 3 < len(tokens) and tokens[i + 3].is_("("):
            continue  # a method of the join: customers.count(), customers.sum(...)
        found.append((a.text, b.text, b.line))
    return found


def _equalities(join: Join) -> tuple[list[tuple[str, str]] | None, str]:
    """The ``(this side, joined side)`` column pairs of an ``on`` condition and a note about any other conjunct, or ``(None, why)``."""

    depths = reader._depths(join.condition) or [0] * len(join.condition)
    conjuncts: list[list] = [[]]
    for token, depth in zip(join.condition, depths):
        if depth == 0 and token.kind == "word" and token.text.lower() == "and":
            conjuncts.append([])
        else:
            conjuncts[-1].append(token)
    pairs, constants = [], []
    for part in conjuncts:
        words = [t for t in part if not t.is_("(") and not t.is_(")")]
        equal = [i for i, t in enumerate(words) if t.is_("=")]
        if len(equal) != 1:
            return None, reader_clip(_text(part), 60)
        left, right = _path(words[: equal[0]]), _path(words[equal[0] + 1 :])
        mine = [side for side in (left, right) if side is not None and len(side) == 2 and side[0] == join.name]
        other = [side for side in (left, right) if side is not None and not (len(side) == 2 and side[0] == join.name)]
        if len(mine) == 1 and len(other) == 1:
            pairs.append((".".join(other[0]), mine[0][1]))
        elif len(mine) == 1 and (left is None or right is None):
            constants.append(reader_clip(_text(part), 40))
        else:
            return None, reader_clip(_text(part), 60)
    if not pairs:
        return None, "no column equality"
    return pairs, ", ".join(constants)


def _path(tokens: list) -> list[str] | None:
    """``["a", "b"]`` for tokens ``a . b`` (any number of parts), ``None`` for anything else."""

    if not tokens or len(tokens) % 2 == 0:
        return None
    parts = []
    for i, token in enumerate(tokens):
        if i % 2 == 0:
            if token.kind != "word":
                return None
            parts.append(token.text)
        elif not token.is_("."):
            return None
    return parts


def _aggregates(tokens: list) -> list[tuple[str, list[str]]]:
    """``(sum|avg, path)`` for ``path.sum()``, ``path.avg()``, ``sum(path)`` and ``avg(path)`` in a measure."""

    found = []
    for i, token in enumerate(tokens):
        if token.kind != "word" or token.text.lower() not in ("sum", "avg") or i + 1 >= len(tokens) or not tokens[i + 1].is_("("):
            continue
        function = token.text.lower()
        if i >= 2 and tokens[i - 1].is_(".") and i + 2 < len(tokens) and tokens[i + 2].is_(")"):
            j, parts = i - 2, []
            while j >= 0 and tokens[j].kind == "word":
                parts.insert(0, tokens[j].text)
                if j >= 1 and tokens[j - 1].is_("."):
                    j -= 2
                else:
                    break
            if parts:
                found.append((function, parts))
            continue
        close = next((k for k in range(i + 2, len(tokens)) if tokens[k].is_(")")), None)
        if close is not None:
            inside = _path(tokens[i + 2 : close])
            if inside:
                found.append((function, inside))
    return found


def _divides(tokens: list) -> bool:
    depths = reader._depths(tokens) or [0] * len(tokens)
    return any(t.is_("/") and d == min(depths) for t, d in zip(tokens, depths))


def _text(tokens: list) -> str:
    out = ""
    for token in tokens:
        text = f"'{token.text}'" if token.kind == "string" else token.text
        if out and (token.kind == "word" or token.kind == "string") and (out[-1].isalnum() or out[-1] in "_')"):
            out += " "
        elif out and token.kind == "punct" and token.text in "=<>+-*/" and not out.endswith(" "):
            out += " "
        out += text
        if token.kind == "punct" and token.text in "=<>+-*/,":
            out += " "
    return " ".join(out.split())


# ------------------------------------------------------------------------------------------------- files


def read_files(path: Path) -> list[tuple[str, str]]:
    """``(name, text)`` of a ``.malloy`` file, or of every ``.malloy`` file under a folder (names relative to it, POSIX)."""

    path = Path(path)
    if path.is_file():
        if path.suffix.lower() != ".malloy":
            raise MalloyInputError(f"{path} is not a .malloy file")
        return [(path.name, _read(path))]
    if not path.is_dir():
        raise MalloyInputError(f"no such file or folder: {path}")
    files = sorted(p for p in path.rglob("*.malloy") if p.is_file() and not p.is_symlink())
    if not files:
        raise MalloyInputError(f"no .malloy files under {path}")
    if len(files) > MAX_FILES:
        raise MalloyInputError(f"{len(files)} .malloy files under {path}; at most {MAX_FILES} are checked at once (pass a narrower folder)")
    return [(p.relative_to(path).as_posix(), _read(p)) for p in files]


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE:
            raise MalloyInputError(f"{path.name} is over {MAX_FILE // 1_000_000} MB")
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise MalloyInputError(f"cannot read {path.name}: {exc}") from None


def import_loader(root: Path) -> Callable[[str, str], tuple[str, str] | None]:
    """Reads an imported file relative to the importing one, only inside ``root`` (a folder) and only ``.malloy`` files."""

    root = Path(root).resolve()

    def load(importing: str, spec: str) -> tuple[str, str] | None:
        if re.match(r"^[a-z][a-z0-9+.-]*:", spec, re.IGNORECASE):
            return None  # a URL
        target = (root / Path(importing).parent / spec).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() != ".malloy" or not target.is_file():
            return None
        try:
            return target.relative_to(root).as_posix(), _read(target)
        except MalloyInputError:
            return None

    return load


def check_path(path: Path, evidence: Evidence | None = None) -> Report:
    path = Path(path)
    files = read_files(path)
    return check(files, evidence, import_loader(path if path.is_dir() else path.parent))


# ------------------------------------------------------------------------------------------------- the CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql check-malloy",
        description="READ-ONLY and offline. Check a Malloy model (a .malloy file or a folder of them): structural problems "
                    "(join `with` a source without primary_key, join cycles, duplicate names, undefined sources and fields), "
                    "and, against what KumoSQL holds (the project, a schema snapshot, saved profiles), primary keys that are "
                    "not the grain of their table, join_one joins that fan out, missing columns and sums or averages of ratio "
                    "columns. Each finding is contradicted, unconfirmed or confirmed, with the evidence. No warehouse is "
                    "queried. Exit 0 when nothing is contradicted, 1 when something is, 2 when the input cannot be read.",
    )
    parser.add_argument("path", type=Path, help="a .malloy file or a folder of .malloy files")
    parser.add_argument("--project", type=Path, help="Dataform project folder, folder of .sql files, or compiled graph JSON whose grain and columns are the evidence")
    parser.add_argument("--source-schema", type=Path, help='with --project: JSON mapping of source tables to columns, e.g. {"p.d.t": {"id": "INT64"}}')
    parser.add_argument("--schema-snapshot", type=Path, metavar="FILE", help="column lists of the warehouse tables (INFORMATION_SCHEMA.COLUMNS shape, CSV or JSON lines)")
    parser.add_argument("--profiles", type=Path, help="folder of saved data profiles and join profiles (default: data-profiles in the KumoSQL data folder)")
    parser.add_argument("--assume-grain", action="append", metavar="TABLE=COLUMN[,COLUMN]", help="declare the grain of a table (repeatable); shown as declared, never derived")
    parser.add_argument("--docs", type=Path, action="append", metavar="DIR", help="with --project: folder of Markdown docs whose 'Primary key:' lines state a table's grain (repeatable)")
    parser.add_argument("--column-attributes", type=Path, metavar="FILE", help="with --project: declared column facts; a declared scale marks a ratio column")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if (args.source_schema or args.docs or args.column_attributes) and not args.project:
        parser.error("--source-schema, --docs and --column-attributes need --project")

    from . import join_profile_store as joins
    from .mcp_session import Session, load_project, parse_assumed_grain
    from .resilience import PipelineLoadError
    from .schema_snapshot import SchemaSnapshotError, load_schema_snapshot

    try:
        assumed = parse_assumed_grain(args.assume_grain)
    except ValueError as exc:
        parser.error(f"--assume-grain: {exc}")
    try:
        files = read_files(args.path)
    except MalloyInputError as exc:
        print(f"check-malloy: {exc}", file=sys.stderr)
        return 2
    snapshot = None
    if args.schema_snapshot:
        try:
            snapshot = load_schema_snapshot(args.schema_snapshot)
        except SchemaSnapshotError as exc:
            print(f"check-malloy: --schema-snapshot: {exc}", file=sys.stderr)
            return 2
    session = None
    if args.project:
        try:
            pipeline = load_project(args.project, args.source_schema, args.column_attributes, args.docs or ())
        except PipelineLoadError as exc:
            print(f"check-malloy: --project: {exc}", file=sys.stderr)
            return 2
        root = args.project.parent if args.project.is_file() else args.project
        session = Session(pipeline, profiles=args.profiles, assume_grain=assumed, root=root, schema_snapshot=snapshot, docs=args.docs or ())
    observations = joins.load_observations(args.profiles)
    evidence = Evidence(session, snapshot, observations, assumed=None if session is not None else assumed, surface="cli")
    path = args.path
    report = check(files, evidence, import_loader(path if path.is_dir() else path.parent))
    print(json.dumps(report.to_json(), indent=2) if args.json else report.to_text())
    return 1 if report.contradicted else 0


# ------------------------------------------------------------------------------------------------- the tool


def _tool_files(session, args: dict) -> tuple[list[tuple[str, str]], Callable | None, str]:
    """The files a call names: ``text``, a ``path`` inside the project or a ``--malloy`` folder, or every ``--malloy`` folder."""

    if args.get("text"):
        return [("(text)", str(args["text"]))], None, "the text given"
    folders = [Path(f).resolve() for f in getattr(session, "_malloy", ())]
    if args.get("path"):
        roots = [Path(session.root).resolve() if session.root else Path.cwd().resolve(), *folders]
        given = Path(args["path"])
        for target in ([given.resolve()] if given.is_absolute() else [(root / given).resolve() for root in roots]):
            if target.exists() and any(target.is_relative_to(root) for root in roots):
                try:
                    files = read_files(target)
                except MalloyInputError as exc:
                    raise ToolError(str(exc), ["Pass a .malloy file or a folder of them, relative to the project folder."]) from None
                return files, import_loader(target if target.is_dir() else target.parent), clip(args["path"], 60)
        raise ToolError("no such .malloy file or folder inside the project folder or a --malloy folder",
                        ["Pass the path relative to the project folder, or text=... with the Malloy source."])
    if not folders:
        raise ToolError("no Malloy model to check", ["Pass path=... (a .malloy file or folder inside the project) or text=... (the Malloy source), "
                                                     "or start the server with --malloy DIR."])
    files: list[tuple[str, str]] = []
    loaders: dict[str, Callable] = {}
    for n, folder in enumerate(folders):
        prefix = f"{folder.name}/" if len(folders) == 1 else f"{n + 1}:{folder.name}/"
        try:
            files += [(prefix + name, text) for name, text in read_files(folder)]
        except MalloyInputError:
            continue
        loaders[prefix] = import_loader(folder)

    def load(importing: str, spec: str) -> tuple[str, str] | None:
        prefix = next((p for p in loaders if importing.startswith(p)), None)
        found = loaders[prefix](importing[len(prefix):], spec) if prefix is not None else None
        return (prefix + found[0], found[1]) if found is not None else None

    if not files:
        raise ToolError("the --malloy folders hold no .malloy files", ["Pass path=... or text=..."])
    return files, load, "the --malloy folders"


@tool(
    "check_malloy",
    "Check a Malloy model against the project",
    "Is this Malloy model's primary_key really the grain of its table, and does each join_one match at most one row? Structural "
    "checks (join `with` a source without primary_key, join cycles, duplicate names, undefined sources and fields) plus checks "
    "against what the server holds: the table's derived or declared grain, saved data profiles and join profiles, the schema "
    "snapshot and the project's columns, and sums or averages of ratio columns. Each finding is contradicted, unconfirmed or "
    "confirmed, with its evidence; an unconfirmed key names the profile_table or join_profile call that would measure it. "
    "Reads no warehouse. Default: the server's --malloy folders.",
    {
        "path": {"type": "string", "description": "A .malloy file or folder, relative to the project folder (or a --malloy folder)."},
        "text": {"type": "string", "description": "Malloy source to check instead of a file (imports are not followed)."},
        "status": {"type": "string", "enum": list(STATUSES), "description": "Only findings with this status."},
    },
)
def check_malloy(session, args: dict) -> Answer:
    files, loader, label = _tool_files(session, args)
    # the server's snapshot keeps only the project's tables, so a warehouse table outside the project has no column list here
    evidence = Evidence(session, session.schema_snapshot(), session.key_observations(), surface="mcp")
    report = check(files, evidence, loader)
    counts = report.counts()
    wanted = args.get("status")
    rows = []
    for item in report.findings:
        if wanted and item.status != wanted:
            continue
        row = item.to_json()
        row["why"] = clip(row["why"], 240)
        if "next" in row:
            row["next"] = clip(row["next"], 180)
        rows.append(row)
    offset = offset_wanted(args)
    answer = Answer("")
    files_word = f"{len(report.files)} file{'s' if len(report.files) != 1 else ''}"
    answer.fields["checked"] = f"{report.sources} sources, {report.joins} joins, {report.measures} measures in {files_word} ({label})"
    answer.counts = dict(counts)
    answer.add_list("findings", rows[offset:], total=len(rows), offset=offset, primary=True, keep=1)
    for entry in report.unknown[:4]:
        answer.unknown(clip(entry["what"], 80), clip(entry["reason"], 140))
    if len(report.unknown) > 4:
        answer.unknown(f"{len(report.unknown) - 4} more parts of the model", "not read; python -m kumosql check-malloy PATH lists them all")
    if not report.evidence["schema_snapshot"]:
        answer.unknown("which columns the warehouse tables have", "no schema snapshot: columns are checked only against the project's models and declared schemas")
    first = next((f for f in report.findings if f.status == (CONTRADICTED if counts[CONTRADICTED] else UNCONFIRMED) and f.next), None)
    if counts[CONTRADICTED]:
        answer.summary = f"{counts[CONTRADICTED]} contradicted, {counts[UNCONFIRMED]} unconfirmed, {counts[CONFIRMED]} confirmed: the evidence shows problems in the model."
    elif counts[UNCONFIRMED]:
        answer.summary = f"Nothing contradicted; {counts[UNCONFIRMED]} unconfirmed and {counts[CONFIRMED]} confirmed. Unconfirmed is not a pass: the evidence is missing."
    else:
        answer.summary = f"Nothing contradicted or unconfirmed: {counts[CONFIRMED]} checks confirmed by the evidence the server holds."
    if first is not None:
        answer.next(clip(first.next, 200))
    else:
        answer.next("Check again after the model or the tables change; a saved measurement is a count on one day.")
    return answer
