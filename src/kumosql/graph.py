"""Declared and observed table relationships for pipeline graphs.

The input records are deliberately source-agnostic. A caller can adapt job
history from any provider into :class:`ObservedRead` or its canonical mapping
shape without making graph construction depend on credentials or network I/O.
"""

from __future__ import annotations

import re

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Literal, Mapping

from sqlglot import exp

from kumo_sql_prover.ast_utils import binding_cte, is_function_table
from .identity import IdentityResolution, NodeIdentity, normalize_table_reference
from .scopes import job_record

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .scopes import Scope


Confidence = Literal["high", "medium", "low"]
_SAMPLE_LIMIT = 10
_DECORATOR_SAMPLE_LIMIT = 10


@dataclass(frozen=True)
class ObservedRead:
    """Canonical record for one job that read tables into a destination.

    ``attributes`` carries source-specific scope fields (for example project
    or user) for filtering only; those fields are never serialized.
    """

    job_id: str
    creation_time: datetime | str | None
    destination: object | None
    referenced_tables: tuple[object, ...] = ()
    attributes: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ObservedRead":
        refs = record.get("referenced_tables", record.get("references", ()))
        if refs is None:
            references: tuple[object, ...] = ()
        elif isinstance(refs, (str, Mapping)):
            references = (refs,)
        else:
            try:
                references = tuple(refs)  # type: ignore[arg-type]
            except TypeError as exc:
                raise ValueError("referenced_tables must be a sequence") from exc
        reserved = {
            "job_id", "creation_time", "destination", "referenced_tables", "references"
        }
        attributes = dict(record.get("attributes", {})) if isinstance(record.get("attributes"), Mapping) else {}
        attributes.update({key: value for key, value in record.items() if key not in reserved and key != "attributes"})
        return cls(
            job_id=str(record.get("job_id") or ""),
            creation_time=record.get("creation_time"),
            destination=record.get("destination"),
            referenced_tables=references,
            attributes=attributes,
        )

    def scope_record(self) -> dict[str, object]:
        """Fields a scope rule can use: the source's attributes plus the job's own fields."""

        return {
            **self.attributes,
            "job_id": self.job_id,
            "creation_time": self.creation_time,
            "destination": self.destination,
            "referenced_tables": list(self.referenced_tables),
        }


@dataclass(frozen=True)
class GraphNode:
    identity: NodeIdentity
    node_kind: str
    resolved: bool

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.identity.stable_key,
            "identity": self.identity.to_json(),
            "node_kind": self.node_kind,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class GraphEdge:
    """One aggregated relationship with its evidence and confidence."""

    upstream: NodeIdentity
    downstream: NodeIdentity
    declared: bool = False
    parsed: bool = False
    observed: bool = False
    first_seen: str | None = None
    last_seen: str | None = None
    observed_count: int = 0
    confidence: Confidence = "medium"
    decorator_samples: tuple[str, ...] = ()
    decorator_count: int = 0

    @property
    def source(self) -> str:
        if (self.declared or self.parsed) and self.observed:
            return "both"
        if self.declared or self.parsed:
            return "declared"
        if self.observed:
            return "observed"
        raise ValueError("an edge must have at least one source")

    @property
    def provenance(self) -> tuple[str, ...]:
        return tuple(
            source
            for source, present in (
                ("declared", self.declared),
                ("parsed", self.parsed),
                ("observed", self.observed),
            )
            if present
        )

    def to_json(self) -> dict[str, object]:
        return {
            "upstream_id": self.upstream.stable_key,
            "downstream_id": self.downstream.stable_key,
            "upstream": self.upstream.to_json(),
            "downstream": self.downstream.to_json(),
            "source": self.source,
            "provenance": list(self.provenance),
            "declared": self.declared,
            "parsed": self.parsed,
            "observed": self.observed,
            "confidence": self.confidence,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "observed_count": self.observed_count,
            "decorator_count": self.decorator_count,
            "decorator_samples": list(self.decorator_samples),
        }


@dataclass(frozen=True)
class GraphResult:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    unresolved_observation_count: int = 0
    unresolved_observation_samples: tuple[dict[str, object], ...] = ()
    unattributed_observation_count: int = 0
    unattributed_observation_samples: tuple[dict[str, object], ...] = ()
    filtered_observation_count: int = 0
    invalid_timestamp_count: int = 0
    observation_scope_applied: bool = False

    def to_json(self) -> dict[str, object]:
        diagnostics: list[dict[str, object]] = []
        for code, count in (
            ("unresolved_observation", self.unresolved_observation_count),
            ("unattributed_observation", self.unattributed_observation_count),
            ("invalid_observation_time", self.invalid_timestamp_count),
        ):
            if count:
                diagnostics.append(
                    {
                        "code": code,
                        "count": count,
                        "scope_applied": self.observation_scope_applied,
                        "model_scope_applied": False,
                    }
                )
        return {
            "nodes": [node.to_json() for node in self.nodes],
            "edges": [edge.to_json() for edge in self.edges],
            "unresolved_observations": {
                "count": self.unresolved_observation_count,
                "samples": list(self.unresolved_observation_samples),
                "scope_applied": self.observation_scope_applied,
                "model_scope_applied": False,
            },
            "unattributed_observations": {
                "count": self.unattributed_observation_count,
                "samples": list(self.unattributed_observation_samples),
                "scope_applied": self.observation_scope_applied,
                "model_scope_applied": False,
            },
            "filtered_observation_count": self.filtered_observation_count,
            "scope_applied": {"observations": self.observation_scope_applied, "models": False},
            "diagnostics": diagnostics,
        }


def edge_confidence(
    *, declared: bool, parsed: bool, observed: bool, parse_incomplete: bool = False
) -> Confidence:
    """Map available edge evidence to a reusable categorical confidence."""

    if declared and observed:
        return "high"
    if parsed and not declared and not observed and parse_incomplete:
        return "low"
    return "medium"


@dataclass
class _EdgeEvidence:
    upstream: NodeIdentity
    downstream: NodeIdentity
    declared: bool = False
    parsed: bool = False
    observed: bool = False
    parse_incomplete: bool = False
    job_ids: set[str] = field(default_factory=set)
    times: list[datetime] = field(default_factory=list)
    decorators: set[str] = field(default_factory=set)

    def freeze(self) -> GraphEdge:
        return GraphEdge(
            upstream=self.upstream,
            downstream=self.downstream,
            declared=self.declared,
            parsed=self.parsed,
            observed=self.observed,
            first_seen=_format_time(min(self.times)) if self.times else None,
            last_seen=_format_time(max(self.times)) if self.times else None,
            observed_count=len(self.job_ids),
            confidence=edge_confidence(
                declared=self.declared,
                parsed=self.parsed,
                observed=self.observed,
                parse_incomplete=self.parse_incomplete,
            ),
            decorator_samples=tuple(sorted(self.decorators)[:_DECORATOR_SAMPLE_LIMIT]),
            decorator_count=len(self.decorators),
        )


_TEMPLATE_TOKEN = re.compile(r"__sqlx_token_\d+__")


def build_query_graph(
    pipeline: "Pipeline",
    observed_reads: Iterable[ObservedRead | Mapping[str, object]] = (),
    *,
    scope: "Scope | None" = None,
) -> GraphResult:
    """Build source-agnostic declared, parsed, and observed graph edges.

    ``scope`` may be a saved scope exposing ``matches(mapping)``. It is applied
    to each input row before aggregation. Observation identifiers are used only
    internally to de-duplicate repeated records and are never serialized.
    """

    nodes: dict[str, GraphNode] = {}
    evidence: dict[tuple[str, str], _EdgeEvidence] = {}

    def add_node(identity: NodeIdentity, node_kind: str, resolved: bool) -> None:
        key = identity.stable_key
        previous = nodes.get(key)
        if previous is None or (resolved and not previous.resolved):
            nodes[key] = GraphNode(identity, node_kind, resolved)

    for key, model in pipeline.models.items():
        if model.identity is not None:
            add_node(model.identity, model.kind, True)
    for target in pipeline.sources.values():
        identity = NodeIdentity.for_target(target.database, target.schema, target.name)
        add_node(identity, "source", True)

    analysis = pipeline._analyse()
    parse_incomplete = {
        diagnostic.model
        for diagnostic in analysis.diagnostics
        if diagnostic.code in {"parse_error", "no_query", "qualify_error", "unparsed_operation"}
    }

    def identity_for_key(pipeline: "Pipeline", key: str) -> tuple[NodeIdentity, str]:
        model = pipeline.models.get(key)
        if model is not None and model.identity is not None:
            return model.identity, model.kind
        target = pipeline.sources.get(key)
        if target is not None:
            return NodeIdentity.for_target(target.database, target.schema, target.name), "source"
        identity = normalize_table_reference(key) or NodeIdentity.unresolved(key)
        return identity, "external"

    def resolve_static(reference: object) -> tuple[NodeIdentity, str, bool]:
        resolution = pipeline.resolve_reference(reference, use_defaults=True)
        if resolution.matched:
            return resolution.identity, resolution.node_kind, True

        normalized = normalize_table_reference(
            reference,
            default_project=pipeline.default_project,
            default_dataset=pipeline.default_dataset,
        )
        # Preserve legacy resolution for partial SQL names, while a full
        # qualification remains authoritative for graph provenance.
        if normalized is not None and len(normalized.parts) < 3:
            key = pipeline.resolve(reference)
            if key:
                identity, kind = identity_for_key(pipeline, key)
                return identity, kind, True
        return resolution.identity, "external", False

    def get_evidence(
        upstream: NodeIdentity,
        downstream: NodeIdentity,
        upstream_kind: str,
        downstream_kind: str,
        *,
        upstream_resolved: bool = True,
        downstream_resolved: bool = True,
    ) -> _EdgeEvidence:
        add_node(upstream, upstream_kind, upstream_resolved)
        add_node(downstream, downstream_kind, downstream_resolved)
        edge_key = upstream.stable_key, downstream.stable_key
        item = evidence.get(edge_key)
        if item is None:
            item = _EdgeEvidence(upstream, downstream)
            evidence[edge_key] = item
        return item

    # Preserve compiled dependency declarations separately from references
    # parsed out of SQL, even though the legacy `upstream` set combines them.
    for downstream_key, model in pipeline.models.items():
        downstream = model.identity or NodeIdentity.unresolved(downstream_key)
        for dependency in model.declared_dependencies:
            upstream, kind, resolved = resolve_static(dependency.key)
            if upstream.stable_key == downstream.stable_key:
                continue
            get_evidence(
                upstream,
                downstream,
                kind,
                model.kind,
                upstream_resolved=resolved,
            ).declared = True

        query = analysis.parsed.get(downstream_key)
        extra_tables = analysis.script_tables.get(downstream_key, ())
        if query is None and not extra_tables:
            continue
        for table in (*(query.find_all(exp.Table) if query is not None else ()), *extra_tables):
            if is_function_table(table):
                continue  # a table function call is not a table; the tables it is given are their own nodes
            if binding_cte(table) is not None:
                continue  # a WITH table in scope at this reference, not a node
            if _TEMPLATE_TOKEN.fullmatch(table.name):
                continue  # a ${...} that could not be resolved: the model carries an unresolved_template gap, not a node
            upstream, kind, resolved = resolve_static(table)
            if upstream.stable_key == downstream.stable_key:
                continue
            item = get_evidence(
                upstream,
                downstream,
                kind,
                model.kind,
                upstream_resolved=resolved,
            )
            item.parsed = True
            item.parse_incomplete = downstream_key in parse_incomplete
            if upstream.kind == "wildcard":
                # A wildcard query reads every known table its pattern matches.
                for member in pipeline.wildcard_members(table):
                    member_identity, member_kind = identity_for_key(pipeline, member)
                    if member_identity.stable_key == downstream.stable_key:
                        continue
                    shard = get_evidence(member_identity, downstream, member_kind, model.kind)
                    shard.parsed = True
                    shard.parse_incomplete = downstream_key in parse_incomplete

    # A table another model's script writes (a MERGE or INSERT into a declared source or into another model) is fed by
    # what that script reads.
    for written_key, feeders in sorted(analysis.written_into.items()):
        downstream, downstream_kind = identity_for_key(pipeline, written_key)
        for feeder_key in sorted(feeders):
            upstream, upstream_kind = identity_for_key(pipeline, feeder_key)
            if upstream.stable_key == downstream.stable_key:
                continue
            get_evidence(upstream, downstream, upstream_kind, downstream_kind).parsed = True

    unresolved_count = 0
    unresolved_samples: list[dict[str, object]] = []
    unattributed_count = 0
    unattributed_samples: list[dict[str, object]] = []
    filtered_count = 0
    invalid_timestamp_count = 0

    if scope is not None:
        observed_reads = list(observed_reads)
        seen_fields: set[str] = set()
        for raw_record in observed_reads:
            seen_fields.update(job_record(raw_record))
        if seen_fields:
            scope.require_fields(seen_fields, "job-history records")

    for row_index, raw_record in enumerate(observed_reads):
        if isinstance(raw_record, ObservedRead):
            record = raw_record
            scope_record = record.scope_record()
        elif isinstance(raw_record, Mapping):
            scope_record = dict(raw_record)
            if isinstance(raw_record.get("attributes"), Mapping):
                scope_record.update(raw_record["attributes"])
            if scope is not None and not scope.matches(scope_record):
                filtered_count += 1
                continue
            try:
                record = ObservedRead.from_record(raw_record)
            except ValueError:
                unattributed_count += 1
                _append_sample(
                    unattributed_samples,
                    {"reason": "invalid_observation_record"},
                )
                continue
            scope_record = record.scope_record()
        else:
            unattributed_count += 1
            _append_sample(unattributed_samples, {"reason": "invalid_observation_record"})
            continue

        if scope is not None and isinstance(raw_record, ObservedRead) and not scope.matches(scope_record):
            filtered_count += 1
            continue

        timestamp, parsed_time = _parse_time(record.creation_time)
        if record.creation_time is not None and parsed_time is None:
            invalid_timestamp_count += 1

        destination_resolution = (
            pipeline.resolve_reference(record.destination)
            if record.destination not in (None, "")
            else None
        )
        reference_resolutions = [
            pipeline.resolve_reference(reference) for reference in record.referenced_tables
        ]

        if destination_resolution is None:
            unattributed_count += 1
            _append_sample(
                unattributed_samples,
                {"reason": "missing_destination", "creation_time": timestamp},
            )
            for resolution in reference_resolutions:
                if resolution.diagnostic_code:
                    unresolved_count += 1
                    _append_sample(unresolved_samples, _unresolved_sample("reference", resolution))
            continue

        destination = destination_resolution.identity
        add_node(destination, destination_resolution.node_kind, destination_resolution.matched)
        if destination_resolution.diagnostic_code:
            unresolved_count += 1
            _append_sample(
                unresolved_samples,
                _unresolved_sample("destination", destination_resolution),
            )

        if not reference_resolutions:
            unattributed_count += 1
            _append_sample(
                unattributed_samples,
                {
                    "reason": "no_references",
                    "creation_time": timestamp,
                    "destination_id": destination.stable_key,
                },
            )
            continue

        job_key = record.job_id or f"row:{row_index}"
        row_edges: set[tuple[str, str]] = set()
        for resolution in reference_resolutions:
            upstream = resolution.identity
            add_node(upstream, resolution.node_kind, resolution.matched)
            if resolution.diagnostic_code:
                unresolved_count += 1
                _append_sample(unresolved_samples, _unresolved_sample("reference", resolution, destination))
            edge_key = upstream.stable_key, destination.stable_key
            if edge_key in row_edges:
                continue
            row_edges.add(edge_key)
            item = get_evidence(
                upstream,
                destination,
                resolution.node_kind,
                destination_resolution.node_kind,
                upstream_resolved=resolution.matched,
                downstream_resolved=destination_resolution.matched,
            )
            item.observed = True
            item.job_ids.add(job_key)
            if resolution.decorator:
                item.decorators.add(resolution.decorator)
            if parsed_time is not None:
                item.times.append(parsed_time)

    edges = tuple(
        evidence[key].freeze()
        for key in sorted(evidence)
    )
    return GraphResult(
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        edges=edges,
        unresolved_observation_count=unresolved_count,
        unresolved_observation_samples=tuple(unresolved_samples),
        unattributed_observation_count=unattributed_count,
        unattributed_observation_samples=tuple(unattributed_samples),
        filtered_observation_count=filtered_count,
        invalid_timestamp_count=invalid_timestamp_count,
        observation_scope_applied=scope is not None,
    )


def _append_sample(samples: list[dict[str, object]], sample: dict[str, object]) -> None:
    if len(samples) < _SAMPLE_LIMIT:
        samples.append(sample)


def _unresolved_sample(
    role: str,
    resolution: IdentityResolution,
    destination: NodeIdentity | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "role": role,
        "identity": resolution.identity.to_json(),
        "status": resolution.status,
        "diagnostic": resolution.diagnostic_code,
    }
    if destination is not None:
        item["downstream_id"] = destination.stable_key
    if resolution.decorator:
        item["decorator"] = resolution.decorator
    return item


def _parse_time(value: datetime | str | None) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return _format_time(parsed), parsed


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphResult",
    "ObservedRead",
    "build_query_graph",
    "edge_confidence",
]
