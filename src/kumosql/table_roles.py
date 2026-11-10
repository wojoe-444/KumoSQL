"""Infer whether each table is a dimension, a fact, a bridge or unknown.

The role comes from evidence, never from names alone. Each table gets a list of
signals, each with a source (``kind``), a direction (``points_to``) and a
strength, so the reader can see what the role rests on:

* ``parsed_usage``: how parsed models read the table. Joined as the lookup side
  on a key to add descriptive columns points to dimension; aggregated on its own
  columns points to fact; joined from two directions on keys with nothing else
  taken from it points to bridge. Readers that could not be parsed are counted as
  unexamined; they never vote.
* ``schema_shape``: for sources, the declared column types (descriptive columns
  plus one key vs. several numeric measures plus several keys); for models, the
  grain hints in the query (GROUP BY or DISTINCT keys) and aggregate columns.
* ``size``: relative row counts against the tables joined with it. It only ever
  corroborates or weakens other evidence and never decides a role alone.
* ``declared``: a role set by hand. It always wins.

Rules: a declared role wins (high confidence). Two independent signals that
agree with none against give ``high``; a single signal gives at most ``medium``;
strong signals that point in different directions, or weak ones that do, give
``unknown`` with the conflicting signals listed. A weak signal against the
chosen role lowers confidence one step. A column name ending in ``_id`` is used
only as one weak hint that a column is a key.

Nothing here raises: bad input becomes ``unknown`` with a ``reason``. Reports
describe behavior only and never include query text.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

from sqlglot import exp

from kumo_sql_prover.ast_utils import binding_cte

if TYPE_CHECKING:
    from .pipeline import Pipeline

ROLES = ("dimension", "fact", "bridge", "unknown")
_DIRECTIONAL = ("dimension", "fact", "bridge")
_NUMERIC = {
    "INT64", "INTEGER", "INT", "SMALLINT", "BIGINT", "TINYINT", "BYTEINT",
    "FLOAT64", "FLOAT", "DOUBLE", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL",
}  # fmt: skip
_DESCRIPTIVE = {"STRING", "BOOL", "BOOLEAN", "DATE", "DATETIME", "TIMESTAMP", "TIME", "BYTES"}
_WIDE_TOTAL = 12


@dataclass(frozen=True)
class RoleSignal:
    kind: str  # "parsed_usage", "schema_shape", "size" or "declared"
    points_to: str  # "dimension", "fact", "bridge" or "none"
    strength: str  # "strong" or "weak"
    detail: str
    available: bool = True

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "points_to": self.points_to,
            "strength": self.strength,
            "detail": self.detail,
            "available": self.available,
        }


@dataclass(frozen=True)
class TableRole:
    table: str
    role: str  # "dimension", "fact", "bridge" or "unknown"
    confidence: str  # "high", "medium" or "low"
    signals: tuple[RoleSignal, ...] = ()
    readers_examined: int = 0
    readers_unexamined: int = 0
    reason: str = ""

    def to_json(self) -> dict:
        return {
            "table": self.table,
            "role": self.role,
            "confidence": self.confidence,
            "reason": self.reason,
            "readers_examined": self.readers_examined,
            "readers_unexamined": self.readers_unexamined,
            "signals": [s.to_json() for s in self.signals],
        }


# ------------------------------------------------------------------ public API


def infer_roles(
    pipeline: "Pipeline",
    *,
    row_counts: Mapping[str, int] | None = None,
    declared: Mapping[str, str] | None = None,
) -> dict[str, TableRole]:
    """A role for every model and declared source. Never raises."""

    try:
        tables = sorted({*pipeline.models, *pipeline.sources})
    except Exception:  # noqa: BLE001 - malformed pipeline object
        return {}
    try:
        facts = _collect(pipeline)
        problem = ""
    except Exception as error:  # noqa: BLE001
        facts = _Facts()
        problem = f"analysis failed ({type(error).__name__})"
    counts = _clean_counts(pipeline, row_counts)
    declared_map = _clean_declared(pipeline, declared)
    result: dict[str, TableRole] = {}
    for table in tables:
        try:
            result[table] = _infer_one(pipeline, facts, table, counts, declared_map, problem)
        except Exception as error:  # noqa: BLE001
            result[table] = TableRole(table, "unknown", "low", reason=f"inference failed ({type(error).__name__})")
    return result


def table_roles_report(pipeline: "Pipeline", **options: object) -> dict:
    """JSON-serialisable form: ``{"tables": {name: role}}``."""

    roles = infer_roles(pipeline, **options)  # type: ignore[arg-type]
    return {"tables": {name: role.to_json() for name, role in roles.items()}}


# ------------------------------------------------------------------ input clean


def _key_of(pipeline: "Pipeline", name: object) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return pipeline.resolve(name) or name.strip().lower()
    except Exception:  # noqa: BLE001
        return name.strip().lower()


def _clean_counts(pipeline: "Pipeline", row_counts: object) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(row_counts, Mapping):
        return out
    for name, count in row_counts.items():
        key = _key_of(pipeline, name)
        if key is None or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            continue
        out[key] = count
    return out


def _clean_declared(pipeline: "Pipeline", declared: object) -> dict[str, str | None]:
    """Declared roles by table; ``None`` marks an invalid role value."""

    out: dict[str, str | None] = {}
    if not isinstance(declared, Mapping):
        return out
    for name, role in declared.items():
        key = _key_of(pipeline, name)
        if key is None:
            continue
        value = role.strip().lower() if isinstance(role, str) else None
        out[key] = value if value in _DIRECTIONAL else None
    return out


# ---------------------------------------------------------------- parsed usage


@dataclass
class _Usage:
    lookup_dim: set[str] = field(default_factory=set)  # readers: lookup adding attributes
    range_join: set[str] = field(default_factory=set)  # readers: joined with a range condition
    aggregated: set[str] = field(default_factory=set)  # readers: aggregated columns
    driving: set[str] = field(default_factory=set)  # readers: driving side with lookups
    bridge: set[str] = field(default_factory=set)  # readers: keys only, two directions
    join_keys: set[str] = field(default_factory=set)  # column names joined on
    peers: set[str] = field(default_factory=set)  # tables joined with this one


@dataclass
class _Facts:
    usage: dict[str, _Usage] = field(default_factory=lambda: defaultdict(_Usage))
    readers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    parsed: dict = field(default_factory=dict)
    records: dict = field(default_factory=dict)


def _collect(pipeline: "Pipeline") -> _Facts:
    analysis = pipeline._analyse()
    facts = _Facts(parsed=analysis.parsed, records=analysis.records)
    for reader, reads in analysis.upstream.items():
        for table in reads:
            if table != reader:
                facts.readers[table].add(reader)
    for model, query in analysis.parsed.items():
        try:
            for select in query.find_all(exp.Select):
                _scan_select(pipeline, facts, model, select)
        except Exception:  # noqa: BLE001 - a reader that cannot be walked gives no votes
            continue
    return facts


def _own(select: exp.Select, node: exp.Expression) -> bool:
    return node.find_ancestor(exp.Select) is select


def _scan_select(pipeline: "Pipeline", facts: _Facts, model: str, select: exp.Select) -> None:
    aliases: dict[str, tuple[str, exp.Table]] = {}  # alias -> (table key, node)
    for table in select.find_all(exp.Table):
        if not _own(select, table) or binding_cte(table) is not None:
            continue
        key = pipeline.resolve(table)
        if key is None:
            continue
        alias = (table.alias_or_name or table.name).lower()
        aliases[alias] = (key, table)
    if not aliases:
        return
    lone = next(iter(aliases)) if len(aliases) == 1 else None

    def alias_of(column: exp.Column) -> str | None:
        qualifier = column.table.lower() if column.table else None
        if qualifier:
            return qualifier if qualifier in aliases else None
        return lone

    joins = [j for j in select.args.get("joins") or [] if isinstance(j, exp.Join)]
    join_aliases: dict[int, set[str]] = {}
    join_columns: dict[int, list[exp.Column]] = {}
    for index, join in enumerate(joins):
        columns = [c for c in (join.args.get("on").find_all(exp.Column) if join.args.get("on") else [])]
        join_columns[index] = columns
        join_aliases[index] = {a for a in map(alias_of, columns) if a}
    lookup_side = {
        (j.this.alias_or_name or j.this.name).lower()
        for j in joins
        if isinstance(j.this, exp.Table) and (j.this.alias_or_name or j.this.name).lower() in aliases
    }
    keys_used: dict[str, set[str]] = defaultdict(set)
    for index, columns in join_columns.items():
        for column in columns:
            alias = alias_of(column)
            if alias:
                keys_used[alias].add(column.name.lower())
    for join in joins:
        for using in join.args.get("using") or []:
            if isinstance(join.this, exp.Table):
                alias = (join.this.alias_or_name or join.this.name).lower()
                if alias in aliases:
                    keys_used[alias].add(str(using.name).lower())

    taken: dict[str, set[str]] = defaultdict(set)
    aggregated: dict[str, set[str]] = defaultdict(set)
    for column in select.find_all(exp.Column):
        if not _own(select, column):
            continue
        alias = alias_of(column)
        if alias is None:
            continue
        join = column.find_ancestor(exp.Join)
        if join is not None and _own(select, join) and column.find_ancestor(exp.Where) is None:
            on = join.args.get("on")
            if on is not None and any(c is column for c in on.find_all(exp.Column)):
                continue
        agg = column.find_ancestor(exp.AggFunc)
        if agg is not None and _own(select, agg) and _sole_owner(agg, alias, alias_of):
            aggregated[alias].add(column.name.lower())
        else:
            taken[alias].add(column.name.lower())

    all_tables = {key for key, _ in aliases.values()}
    for alias, (key, _node) in aliases.items():
        usage = facts.usage[key]
        usage.join_keys.update(keys_used.get(alias, ()))
        if joins:
            usage.peers.update(all_tables - {key})
        own_keys = keys_used.get(alias, set())
        measures = aggregated[alias] - own_keys
        attributes = taken[alias] - own_keys
        if measures:
            usage.aggregated.add(model)
        counterparts = set()
        for present in join_aliases.values():
            if alias in present:
                counterparts |= present - {alias}
        if len(own_keys) >= 2 and len(counterparts) >= 2 and not attributes and not measures:
            usage.bridge.add(model)
        elif alias in lookup_side and own_keys and attributes and not measures:
            usage.lookup_dim.add(model)
            if _has_range_join(joins, alias, aliases):
                usage.range_join.add(model)
        elif joins and alias not in lookup_side and not measures:
            usage.driving.add(model)


def _sole_owner(agg: exp.Expression, alias: str, alias_of) -> bool:
    """Whether every column inside an aggregate belongs to ``alias``.

    ``SUM(item.price - product.cost)`` measures the item, not the product: the
    product only supplied an attribute to it.
    """

    return all(alias_of(c) == alias for c in agg.find_all(exp.Column))


def _has_range_join(joins: list[exp.Join], alias: str, aliases: dict) -> bool:
    for join in joins:
        on = join.args.get("on")
        if on is None:
            continue
        for node in on.find_all((exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
            if any((c.table or "").lower() == alias for c in node.find_all(exp.Column)):
                return True
    return False


def _usage_signals(facts: _Facts, table: str, examined: int) -> list[RoleSignal]:
    if examined == 0:
        return [RoleSignal("parsed_usage", "none", "weak", "no reader could be examined", False)]
    usage = facts.usage.get(table)
    if usage is None:
        return [RoleSignal("parsed_usage", "none", "weak", f"{examined} reader(s) examined; none uses it in a telling way")]

    def strength(readers: set[str]) -> str:
        return "strong" if len(readers) >= 2 else "weak"

    signals: list[RoleSignal] = []
    if usage.lookup_dim:
        note = "; matched on a validity range, so the key alone may not be unique" if usage.range_join else ""
        signals.append(
            RoleSignal(
                "parsed_usage", "dimension", strength(usage.lookup_dim),
                f"joined as the lookup side on a key to add columns in {len(usage.lookup_dim)} reader(s){note}",
            )
        )  # fmt: skip
    if usage.aggregated:
        signals.append(
            RoleSignal(
                "parsed_usage", "fact", strength(usage.aggregated),
                f"its own columns are summed, counted or otherwise aggregated in {len(usage.aggregated)} reader(s)",
            )
        )  # fmt: skip
    if usage.bridge:
        signals.append(
            RoleSignal(
                "parsed_usage", "bridge", strength(usage.bridge),
                f"only keys are used, matched against two or more other tables in {len(usage.bridge)} reader(s)",
            )
        )  # fmt: skip
    if usage.driving:
        signals.append(
            RoleSignal(
                "parsed_usage", "fact", "weak",
                f"driving side of joins to other tables in {len(usage.driving)} reader(s)",
            )
        )  # fmt: skip
    return signals or [
        RoleSignal("parsed_usage", "none", "weak", f"{examined} reader(s) examined; none uses it in a telling way")
    ]


# ----------------------------------------------------------------------- shape


def _is_key_hint(name: str) -> bool:
    lowered = name.lower()
    return lowered == "id" or lowered.endswith("_id")


def _base_type(value: object) -> str:
    text = str(value).strip().upper()
    return re.split(r"[<(\s]", text, maxsplit=1)[0]


def _source_columns(pipeline: "Pipeline", table: str) -> dict[str, str] | None:
    schema = pipeline.source_schema
    if not isinstance(schema, Mapping):
        return None
    for candidate in (table, table.lower()):
        value = schema.get(candidate)
        if isinstance(value, Mapping) and value:
            return {str(k): v for k, v in value.items()}
    return None


def _shape_signal(pipeline: "Pipeline", facts: _Facts, table: str) -> RoleSignal:
    usage = facts.usage.get(table)
    joined = usage.join_keys if usage else set()
    if table in pipeline.models:
        return _model_shape(pipeline, facts, table, joined)
    columns = _source_columns(pipeline, table)
    if columns is None:
        return RoleSignal("schema_shape", "none", "weak", "no column types known for this source", False)
    keys, measures, descriptive = [], [], []
    for name, kind in columns.items():
        base = _base_type(kind)
        if name.lower() in joined or _is_key_hint(name):
            keys.append(name)
        elif base in _NUMERIC:
            measures.append(name)
        elif base in _DESCRIPTIVE:
            descriptive.append(name)
    total = len(columns)
    summary = f"{len(keys)} key-like, {len(measures)} numeric measure, {len(descriptive)} descriptive column(s)"
    if total >= _WIDE_TOTAL and len(measures) >= 3 and len(descriptive) >= 3:
        return RoleSignal("schema_shape", "none", "weak", f"wide table mixing measures and descriptive columns ({summary})")
    confirmed = [k for k in keys if k.lower() in joined]
    if len(keys) >= 2 and len(measures) + len(descriptive) <= 1:
        strength = "strong" if len(confirmed) >= 2 else "weak"
        return RoleSignal("schema_shape", "bridge", strength, f"two or more keys and almost nothing else ({summary})")
    if len(measures) >= 2 and len(keys) >= 2:
        return RoleSignal("schema_shape", "fact", "strong", f"several numeric measures plus several keys ({summary})")
    if len(measures) >= 2 and len(keys) == 1:
        return RoleSignal("schema_shape", "fact", "weak", f"several numeric measures and one key ({summary})")
    if keys and len(descriptive) >= 2 and len(measures) <= 1:
        strong = len(keys) == 1 and not measures
        dates = sum(1 for n in descriptive if _base_type(columns[n]) in {"DATE", "DATETIME", "TIMESTAMP"})
        note = "; several date columns, so the key alone may not be unique" if dates >= 2 else ""
        return RoleSignal(
            "schema_shape", "dimension", "strong" if strong else "weak",
            f"mostly descriptive columns with a key ({summary}){note}",
        )  # fmt: skip
    return RoleSignal("schema_shape", "none", "weak", f"no clear shape ({summary})")


def _group_names(query: exp.Expression, projections: list[exp.Expression]) -> set[str]:
    group = query.args.get("group")
    names: set[str] = set()
    if group is None:
        return names
    for item in group.expressions:
        if isinstance(item, exp.Literal) and item.is_int:
            index = int(item.name) - 1
            if 0 <= index < len(projections):
                names.add(projections[index].alias_or_name.lower())
        else:
            names.update(c.name.lower() for c in item.find_all(exp.Column))
            if isinstance(item, exp.Identifier):
                names.add(item.name.lower())
    return names


def _model_shape(pipeline: "Pipeline", facts: _Facts, model: str, joined: set[str]) -> RoleSignal:
    from .pipeline import ColumnRef

    query = facts.parsed.get(model)
    if query is None:
        return RoleSignal("schema_shape", "none", "weak", "model could not be parsed", False)
    if not isinstance(query, exp.Select):
        return RoleSignal("schema_shape", "none", "weak", "no grain hints (set operation or script)")
    projections = list(query.expressions)
    outputs = [p.alias_or_name.lower() for p in projections if p.alias_or_name]
    keys = _group_names(query, projections) & set(outputs)
    distinct = query.args.get("distinct") is not None
    measures = []
    for name in outputs:
        record = facts.records.get(ColumnRef(model, name))
        if record is not None and record.transform == "aggregate":
            measures.append(name)
    if keys and measures:
        strength = "strong" if len(keys) >= 2 else "weak"
        return RoleSignal(
            "schema_shape", "fact", strength,
            f"grouped by {len(keys)} key column(s) with {len(measures)} aggregate column(s)",
        )  # fmt: skip
    if (keys or distinct) and not measures and outputs:
        grain = keys or set(outputs)
        if len(grain) >= 2 and all(_is_key_hint(n) or n in joined for n in grain) and len(grain) == len(outputs):
            return RoleSignal("schema_shape", "bridge", "weak", f"distinct combinations of {len(grain)} key-like columns only")
        return RoleSignal(
            "schema_shape", "dimension", "weak",
            f"one row per distinct key ({len(grain)} grain column(s)) with no aggregate columns",
        )  # fmt: skip
    return RoleSignal("schema_shape", "none", "weak", "no grain hints (no GROUP BY or DISTINCT keys)")


# ------------------------------------------------------------------------ size


def _size_signal(facts: _Facts, table: str, counts: dict[str, int]) -> RoleSignal:
    if table not in counts:
        return RoleSignal("size", "none", "weak", "no row count for this table", False)
    usage = facts.usage.get(table)
    peers = [counts[p] for p in (usage.peers if usage else ()) if p in counts]
    if not peers:
        return RoleSignal("size", "none", "weak", "no row counts for the tables it is joined with", False)
    mine, biggest = counts[table], max(peers)
    if biggest > 0 and mine * 10 <= biggest:
        return RoleSignal("size", "dimension", "weak", "at most a tenth of the size of the tables it is joined with")
    if mine >= biggest * 10 and mine > 0:
        return RoleSignal("size", "fact", "weak", "at least ten times the size of the tables it is joined with")
    return RoleSignal("size", "none", "weak", "similar in size to the tables it is joined with")


# ------------------------------------------------------------------- decision


def _infer_one(
    pipeline: "Pipeline", facts: _Facts, table: str, counts: dict[str, int],
    declared: dict[str, str | None], problem: str,
) -> TableRole:  # fmt: skip
    if table in declared:
        role = declared[table]
        if role is None:
            signal = RoleSignal("declared", "none", "strong", "declared role is not one of dimension, fact, bridge")
            return TableRole(table, "unknown", "low", (signal,), reason="declared role is invalid")
        signal = RoleSignal("declared", role, "strong", "role set by hand")
        return TableRole(table, role, "high", (signal,), *_reader_counts(facts, table), reason="declared")
    examined, unexamined = _reader_counts(facts, table)
    if problem:
        return TableRole(table, "unknown", "low", (), examined, unexamined, reason=problem)
    signals: list[RoleSignal] = [RoleSignal("declared", "none", "strong", "no declared role", False)]
    signals += _usage_signals(facts, table, examined)
    signals.append(_shape_signal(pipeline, facts, table))
    signals.append(_size_signal(facts, table, counts))
    return _decide(table, tuple(signals), examined, unexamined)


def _reader_counts(facts: _Facts, table: str) -> tuple[int, int]:
    readers = facts.readers.get(table, set())
    examined = sum(1 for r in readers if r in facts.parsed)
    return examined, len(readers) - examined


def _decide(table: str, signals: tuple[RoleSignal, ...], examined: int, unexamined: int) -> TableRole:
    votes = [s for s in signals if s.available and s.points_to in _DIRECTIONAL]
    deciders = [s for s in votes if s.kind != "size"]
    strong = [s for s in deciders if s.strength == "strong"]
    deciders = strong or deciders
    directions = {s.points_to for s in deciders}
    unread = f"; {unexamined} reader(s) could not be examined" if unexamined else ""
    if not deciders:
        why = "size alone does not decide a role" if votes else "no usable evidence"
        return TableRole(table, "unknown", "low", signals, examined, unexamined, reason=why + unread)
    if len(directions) > 1:
        return TableRole(
            table, "unknown", "low", signals, examined, unexamined,
            reason="conflicting evidence: " + " vs ".join(sorted(directions)) + unread,
        )  # fmt: skip
    role = next(iter(directions))
    kinds = {s.kind for s in votes if s.points_to == role}
    opposing = [s for s in votes if s.points_to != role]
    if strong and len(kinds) >= 2:
        level = 2
    elif strong or len(kinds) >= 2:
        level = 1
    else:
        level = 0
    if opposing:
        level = max(0, level - 1)
    confidence = ("low", "medium", "high")[level]
    reason = f"{len(kinds)} signal kind(s) agree" + (f", {len(opposing)} weak signal(s) differ" if opposing else "")
    return TableRole(table, role, confidence, signals, examined, unexamined, reason=reason + unread)
