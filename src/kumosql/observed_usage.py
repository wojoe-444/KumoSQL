"""Per-table usage facts read from job history.

Observed job records that carry query text are reduced to how each known table
is read: joined as the lookup side or the driving side, or not joined; on which
columns; and which columns are grouped, aggregated or selected. A fact is kept
per table and per *reader*, never per job, so a scheduled job that ran a
hundred times counts once and an ad hoc query counts once per person.

What may leave this module: counts and column names. Query text, job ids, and
reader identities are used only while a result is being built. They are hashed
into anonymous keys held in local variables and are never stored on a result.

Records that cannot be examined still count. Each is tallied under a reason
(``no_query_text``, ``truncated``, ``parse_error``, ``out_of_scope``,
``invalid_record``, ``no_known_tables``) both overall and, when the record
names the table, per table, so the result always says how many readers it
examined and how many it could not, and why.

Confidence per table: ``low`` on a small sample (fewer than ``MIN_READERS``
examined readers) or when most readers could not be examined, ``medium``
otherwise, ``high`` from ``HIGH_READERS`` examined readers with few
unexamined. These facts are an observed, weaker-than-parsed-models signal:
job history shows how tables were read, not how the pipeline is built.

Nothing here raises for bad input; a bad record is counted, not fatal.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Mapping

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import binding_cte
from .graph import ObservedRead
from .scripts import split_statements

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .scopes import Scope

MIN_READERS = 3
HIGH_READERS = 10
_UNEXAMINED_LOW = 0.5
_UNEXAMINED_HIGH = 0.2

JOINED_AS = ("lookup_side", "driving_side", "not_joined")
REASONS = (
    "out_of_scope",
    "view_definition",
    "invalid_record",
    "no_query_text",
    "truncated",
    "parse_error",
    "no_known_tables",
)
_TEXT_KEYS = ("query", "query_text")
_READER_KEYS = ("reader", "user_email", "user", "principal", "service_account")
_TRUNCATED_KEYS = ("query_truncated", "truncated")
# A plain view is only defined by its job, not read: no data moves, so it is no reader.
_VIEW_DEFINITION = re.compile(r"\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?VIEW|ALTER\s+VIEW)\b", re.IGNORECASE)
_VIEW_STATEMENT_TYPES = {"CREATE_VIEW", "ALTER_VIEW", "DROP_VIEW"}
_SHARD = re.compile(r"(_?\d{8}|_?\*)$")


# ------------------------------------------------------------------ result types


@dataclass(frozen=True)
class TableUsage:
    """How one known table is read, counted by distinct readers."""

    table: str
    confidence: str  # "high", "medium" or "low"
    readers_examined: int
    readers_unexamined: int
    unexamined_reasons: Mapping[str, int]
    joined_as: Mapping[str, int]  # readers per joined_as value
    join_columns: Mapping[str, int]  # column -> readers
    columns_grouped: Mapping[str, int]
    columns_aggregated: Mapping[str, int]
    columns_selected: Mapping[str, int]
    reason: str = ""

    def to_json(self) -> dict:
        return {
            "table": self.table,
            "confidence": self.confidence,
            "reason": self.reason,
            "readers_examined": self.readers_examined,
            "readers_unexamined": self.readers_unexamined,
            "unexamined_reasons": dict(self.unexamined_reasons),
            "joined_as": dict(self.joined_as),
            "join_columns": dict(self.join_columns),
            "columns_grouped": dict(self.columns_grouped),
            "columns_aggregated": dict(self.columns_aggregated),
            "columns_selected": dict(self.columns_selected),
        }


@dataclass(frozen=True)
class UsageResult:
    tables: Mapping[str, TableUsage]
    records_total: int = 0
    records_examined: int = 0
    records_unexamined: Mapping[str, int] = field(default_factory=dict)
    readers_examined: int = 0
    readers_unexamined: int = 0
    temporary_references_ignored: int = 0
    sharded_references_folded: int = 0

    def to_json(self) -> dict:
        return {
            "tables": {name: usage.to_json() for name, usage in sorted(self.tables.items())},
            "records_total": self.records_total,
            "records_examined": self.records_examined,
            "records_unexamined": dict(self.records_unexamined),
            "readers_examined": self.readers_examined,
            "readers_unexamined": self.readers_unexamined,
            "temporary_references_ignored": self.temporary_references_ignored,
            "sharded_references_folded": self.sharded_references_folded,
        }


# ------------------------------------------------------------------ public API


def observed_usage(
    pipeline: "Pipeline",
    records: Iterable[ObservedRead | Mapping[str, object]],
    *,
    scope: "Scope | None" = None,
) -> UsageResult:
    """Usage facts for every known table that job history reads. Never raises."""

    readers: dict[str, _Reader] = {}
    totals: dict[str, int] = defaultdict(int)
    examined_records = 0
    total_records = 0
    temporary = 0
    folded = 0

    for index, raw in enumerate(records):
        total_records += 1
        try:
            fields, scope_record = _fields(raw)
        except Exception:  # noqa: BLE001 - malformed record
            totals["invalid_record"] += 1
            continue
        if scope is not None and not scope.matches(scope_record):
            totals["out_of_scope"] += 1
            continue
        if _defines_view(fields, raw):
            totals["view_definition"] += 1
            continue
        key = _reader_key(fields, index)
        reader = readers.setdefault(key, _Reader())
        refs = _referenced_keys(pipeline, fields["references"])
        outcome = _examine(pipeline, fields)
        if isinstance(outcome, str):
            totals[outcome] += 1
            reader.failed[outcome] += 1
            reader.referenced.update(refs)
            continue
        examined_records += 1
        temporary += outcome.temporary
        folded += outcome.folded
        reader.examined = True
        reader.merge(outcome)
        reader.referenced.update(refs)

    tables = _summarise(readers)
    examined_readers = sum(1 for r in readers.values() if r.examined)
    return UsageResult(
        tables=tables,
        records_total=total_records,
        records_examined=examined_records,
        records_unexamined={r: totals[r] for r in REASONS if totals[r]},
        readers_examined=examined_readers,
        readers_unexamined=len(readers) - examined_readers,
        temporary_references_ignored=temporary,
        sharded_references_folded=folded,
    )


def observed_usage_report(pipeline: "Pipeline", records: Iterable[object], **options: object) -> dict:
    """JSON-serialisable form of :func:`observed_usage`."""

    return observed_usage(pipeline, records, **options).to_json()  # type: ignore[arg-type]


# -------------------------------------------------------------------- internals


@dataclass
class _Outcome:
    """Facts from one examined record: table -> per-fact sets."""

    joined: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    join_cols: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    grouped: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    aggregated: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    selected: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    temporary: int = 0
    folded: int = 0

    def tables(self) -> set[str]:
        return set(self.joined)


@dataclass
class _Reader:
    examined: bool = False
    failed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    referenced: set[str] = field(default_factory=set)
    facts: _Outcome = field(default_factory=_Outcome)

    def merge(self, outcome: _Outcome) -> None:
        for name in ("joined", "join_cols", "grouped", "aggregated", "selected"):
            target = getattr(self.facts, name)
            for table, values in getattr(outcome, name).items():
                target[table] |= values


def _fields(raw: object) -> tuple[dict, dict]:
    if isinstance(raw, ObservedRead):
        record = raw
    elif isinstance(raw, Mapping):
        record = ObservedRead.from_record(raw)
    else:
        raise TypeError("record is not a mapping")
    attributes = dict(record.attributes)
    text = next((attributes.get(k) for k in _TEXT_KEYS if attributes.get(k) not in (None, "")), None)
    truncated = any(bool(attributes.get(k)) for k in _TRUNCATED_KEYS)
    reader = next((attributes.get(k) for k in _READER_KEYS if attributes.get(k) not in (None, "")), None)
    fields = {
        "job_id": record.job_id,
        "destination": record.destination,
        "references": record.referenced_tables,
        "text": text if isinstance(text, str) else None,
        "truncated": truncated,
        "reader": reader,
    }
    scope_record = dict(attributes)
    if isinstance(raw, Mapping) and isinstance(raw.get("attributes"), Mapping):
        scope_record.update(raw["attributes"])
    return fields, scope_record


def _defines_view(fields: dict, raw: object) -> bool:
    """A job that only creates or alters a view (a script with other statements is not)."""

    attributes = raw.attributes if isinstance(raw, ObservedRead) else (raw.get("attributes") or {}) if isinstance(raw, Mapping) else {}
    statement = raw.get("statement_type") if isinstance(raw, Mapping) else None
    statement = statement or (attributes.get("statement_type") if isinstance(attributes, Mapping) else None)
    if isinstance(statement, str) and statement.upper() in _VIEW_STATEMENT_TYPES:
        return True
    text = fields["text"]
    if not text or not _VIEW_DEFINITION.match(text):
        return False
    return ";" not in text.strip().rstrip(";")


def _hash(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8", "replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def _reader_key(fields: dict, index: int) -> str:
    """Anonymous key: the person, else the destination, else the text, else the job."""

    if fields["reader"] is not None:
        return _hash("reader", str(fields["reader"]).casefold())
    if fields["destination"] not in (None, ""):
        return _hash("destination", str(fields["destination"]))
    if fields["text"]:
        return _hash("text", " ".join(fields["text"].split()))
    return _hash("job", fields["job_id"] or f"row:{index}")


def _referenced_keys(pipeline: "Pipeline", references: Iterable[object]) -> set[str]:
    keys: set[str] = set()
    for reference in references:
        try:
            key = pipeline.resolve(reference)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            key = None
        if key is not None:
            keys.add(key)
    return keys


def _examine(pipeline: "Pipeline", fields: dict) -> "_Outcome | str":
    text = fields["text"]
    if not text or not text.strip():
        return "no_query_text"
    if fields["truncated"]:
        return "truncated"
    # A script is cut into its statements first (blocks, strings and comments respected), so one
    # statement that does not parse costs only itself.
    statements = []
    for part in split_statements(text):
        try:
            statements.extend(s for s in sqlglot.parse(part, read="bigquery") if s is not None)
        except Exception:  # noqa: BLE001 - never echo the text
            continue
    if not statements:
        return "parse_error"
    outcome = _Outcome()
    temp_names = {
        t.name.casefold()
        for s in statements
        for c in s.find_all(exp.Create)
        if c.args.get("properties") is not None
        and any(isinstance(p, exp.TemporaryProperty) for p in c.args["properties"].expressions)
        for t in c.find_all(exp.Table)
        if t.parent is c or t.parent is c.this
    }
    try:
        for statement in statements:
            for select in statement.find_all(exp.Select):
                _scan_select(pipeline, select, outcome, temp_names)
    except Exception:  # noqa: BLE001
        return "parse_error"
    if not outcome.tables():
        return "no_known_tables"
    return outcome


def _is_temporary(table: exp.Table, temp_names: set[str]) -> bool:
    if table.db.startswith("_") or table.db.casefold() in {"_session", "_script"}:
        return True
    return not table.db and table.name.casefold() in temp_names


def _resolve_table(pipeline: "Pipeline", table: exp.Table) -> tuple[str | None, bool]:
    """Key for a table node, folding wildcard and date-sharded names to their base."""

    key = pipeline.resolve(table)
    if key is not None:
        return key, False
    base = _SHARD.sub("", table.name)
    if base and base != table.name:
        clone = table.copy()
        clone.set("this", exp.to_identifier(base))
        return pipeline.resolve(clone), True
    return None, False


def _own(select: exp.Select, node: exp.Expression) -> bool:
    return node.find_ancestor(exp.Select) is select


def _scan_select(pipeline: "Pipeline", select: exp.Select, outcome: _Outcome, temp_names: set[str]) -> None:
    aliases: dict[str, str] = {}  # alias -> table key
    nodes: dict[str, exp.Table] = {}
    for table in select.find_all(exp.Table):
        if not _own(select, table) or binding_cte(table) is not None:
            continue
        if _is_temporary(table, temp_names):
            outcome.temporary += 1
            continue
        key, was_sharded = _resolve_table(pipeline, table)
        if key is None:
            continue
        outcome.folded += 1 if was_sharded else 0
        alias = (table.alias_or_name or table.name).casefold()
        aliases[alias] = key
        nodes[alias] = table
    if not aliases:
        return
    lone = next(iter(aliases)) if len(aliases) == 1 else None

    def owner(column: exp.Column) -> str | None:
        if column.table:
            alias = column.table.casefold()
            return aliases.get(alias)
        return aliases[lone] if lone else None

    joins = [j for j in select.args.get("joins") or [] if isinstance(j, exp.Join)]
    from_ = select.args.get("from_") or select.args.get("from")
    from_alias = None
    if from_ is not None and isinstance(from_.this, exp.Table):
        from_alias = (from_.this.alias_or_name or from_.this.name).casefold()

    sides: dict[str, set[str]] = defaultdict(set)
    if joins:
        for join in joins:
            side = str(join.args.get("side") or "").upper()
            if not isinstance(join.this, exp.Table):
                continue
            joined_alias = (join.this.alias_or_name or join.this.name).casefold()
            if joined_alias not in aliases:
                continue
            if side == "RIGHT" and from_alias in aliases:
                sides[aliases[joined_alias]].add("driving_side")
                sides[aliases[from_alias]].add("lookup_side")
            else:
                sides[aliases[joined_alias]].add("lookup_side")
                if from_alias in aliases:
                    sides[aliases[from_alias]].add("driving_side")
    for key in aliases.values():
        outcome.joined[key] |= sides.get(key) or {"not_joined"}

    for join in joins:
        on = join.args.get("on")
        if on is not None:
            for column in on.find_all(exp.Column):
                key = owner(column)
                if key:
                    outcome.join_cols[key].add(column.name.casefold())
        for using in join.args.get("using") or []:
            name = str(using.name).casefold()
            if isinstance(join.this, exp.Table):
                alias = (join.this.alias_or_name or join.this.name).casefold()
                if alias in aliases:
                    outcome.join_cols[aliases[alias]].add(name)
            if from_alias in aliases:
                outcome.join_cols[aliases[from_alias]].add(name)

    projections = list(select.expressions)
    by_alias = {p.alias.casefold(): p for p in projections if isinstance(p, exp.Alias)}

    for projection in projections:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            targets = [aliases[projection.table.casefold()]] if getattr(projection, "table", "") and projection.table.casefold() in aliases else list(aliases.values())
            for key in targets:
                outcome.selected[key].add("*")
            continue
    for column in select.find_all(exp.Column):
        if not _own(select, column) or isinstance(column.this, exp.Star):
            continue
        key = owner(column)
        if not key:
            continue
        agg = column.find_ancestor(exp.AggFunc)
        if agg is not None and _own(select, agg):
            outcome.aggregated[key].add(column.name.casefold())
        elif _in_projection(select, column):
            outcome.selected[key].add(column.name.casefold())

    group = select.args.get("group")
    if group is not None:
        for item in group.expressions:
            targets: list[exp.Expression]
            if isinstance(item, exp.Literal) and item.is_int:
                position = int(item.name) - 1
                targets = [projections[position]] if 0 <= position < len(projections) else []
            elif isinstance(item, exp.Column) and not item.table and item.name.casefold() in by_alias:
                targets = [by_alias[item.name.casefold()]]
            else:
                targets = [item]
            for target in targets:
                for column in target.find_all(exp.Column):
                    key = owner(column)
                    if key:
                        outcome.grouped[key].add(column.name.casefold())


def _in_projection(select: exp.Select, column: exp.Column) -> bool:
    node: exp.Expression | None = column
    while node is not None and node.parent is not select:
        node = node.parent
    return node is not None and any(node is p for p in select.expressions)


def _summarise(readers: Mapping[str, _Reader]) -> dict[str, TableUsage]:
    tables: set[str] = set()
    for reader in readers.values():
        tables |= reader.facts.tables() | reader.referenced
    result: dict[str, TableUsage] = {}
    for table in sorted(tables):
        examined = 0
        reasons: dict[str, int] = defaultdict(int)
        joined = {name: 0 for name in JOINED_AS}
        columns: dict[str, dict[str, int]] = {
            name: defaultdict(int) for name in ("join_cols", "grouped", "aggregated", "selected")
        }
        for reader in readers.values():
            if table in reader.facts.joined:
                examined += 1
                for value in reader.facts.joined[table]:
                    joined[value] += 1
                for name, counter in columns.items():
                    for column in getattr(reader.facts, name).get(table, ()):
                        counter[column] += 1
            elif table in reader.referenced and not reader.examined:
                reason = max(reader.failed, key=lambda r: (-REASONS.index(r), r), default="no_known_tables")
                reasons[reason] += 1
            elif table in reader.referenced:
                reasons["no_known_tables"] += 1  # its examined queries did not read this table
        unexamined = sum(reasons.values())
        confidence, why = _confidence(examined, unexamined)
        result[table] = TableUsage(
            table=table,
            confidence=confidence,
            readers_examined=examined,
            readers_unexamined=unexamined,
            unexamined_reasons=dict(sorted(reasons.items())),
            joined_as=joined,
            join_columns=_ranked(columns["join_cols"]),
            columns_grouped=_ranked(columns["grouped"]),
            columns_aggregated=_ranked(columns["aggregated"]),
            columns_selected=_ranked(columns["selected"]),
            reason=why,
        )
    return result


def _ranked(counter: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _confidence(examined: int, unexamined: int) -> tuple[str, str]:
    total = examined + unexamined
    if examined == 0:
        return "low", "no reader could be examined"
    if examined < MIN_READERS:
        return "low", f"small sample: {examined} reader(s) examined"
    if unexamined / total > _UNEXAMINED_LOW:
        return "low", f"most readers ({unexamined} of {total}) could not be examined"
    if examined >= HIGH_READERS and unexamined / total <= _UNEXAMINED_HIGH:
        return "high", f"{examined} readers examined"
    return "medium", f"{examined} readers examined, {unexamined} could not be"


__all__ = [
    "HIGH_READERS",
    "JOINED_AS",
    "MIN_READERS",
    "REASONS",
    "TableUsage",
    "UsageResult",
    "observed_usage",
    "observed_usage_report",
]
