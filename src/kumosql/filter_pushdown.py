"""Propose moving a filter from every reader of a model into the model itself.

A filter is proposed only when it is provably safe to describe as a move:
every consumer of the model applies the same predicate (or one that implies
it) to that model's rows, every consumer was parsed, the consumer set is
complete, and the predicate can be written against the model's own sources.
Anything else is reported as a refusal with the reason, never as a proposal.

Proposals are advice only: ``ready`` is always false here. Verification of
each consumer belongs to a later step. No cost data is invented; the cost
rationale states that it is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Mapping

from sqlglot import exp

from kumo_sql_prover.ast_utils import binding_cte, conjuncts as _flatten, star_modified
from kumo_sql_prover.equivalence import _normalize_predicate
from .graph import ObservedRead, build_query_graph
from .near_duplicates import _filters_safe_downstream
from kumo_sql_prover.smt_equivalence import SmtStatus, prove_equivalent_smt

if TYPE_CHECKING:
    from .pipeline import Pipeline

KIND = "upstream_filter"
_UNPARSED_CODES = {"parse_error", "no_query", "qualify_error"}
_PUSH_TARGET_KINDS = {"table", "view", "sql"}
_NONDETERMINISTIC = (
    exp.Rand,
    exp.CurrentTimestamp,
    exp.CurrentDate,
    exp.CurrentTime,
    exp.CurrentDatetime,
    exp.Uuid,
    exp.Anonymous,
    exp.Placeholder,
    exp.Parameter,
)


@dataclass(frozen=True)
class Refusal:
    model: str
    reason: str
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"model": self.model, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class FilterProposal:
    id: str
    model: str
    predicate: str  # written against the model's own sources
    consumers: tuple[dict[str, str], ...]
    ready: bool = False

    @property
    def title(self) -> str:
        return f"Push `{self.predicate}` into {self.model}"

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": KIND,
            "title": self.title,
            "cost_rationale": (
                "Unknown: no cost data is available. Structurally, every consumer "
                "discards the rows this filter removes."
            ),
            "cost_evidence": "none",
            "consumers": [dict(item) for item in self.consumers],
            "ready": self.ready,
            "edits": [{"model": self.model, "action": "add_where", "predicate": self.predicate}],
        }


@dataclass(frozen=True)
class FilterPushdownResult:
    proposals: tuple[FilterProposal, ...] = ()
    refusals: tuple[Refusal, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "proposals": [item.to_json() for item in self.proposals],
            "refusals": [item.to_json() for item in self.refusals],
        }


class _Refuse(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def find_upstream_filter_proposals(
    pipeline: "Pipeline",
    observed_reads: Iterable[ObservedRead | Mapping[str, object]] = (),
) -> FilterPushdownResult:
    """Propose upstream filters for every model whose consumers all agree."""

    analysis = pipeline._analyse()
    graph = build_query_graph(pipeline, observed_reads)
    key_of = {
        model.identity.stable_key: key
        for key, model in pipeline.models.items()
        if model.identity is not None
    }
    consumers_of: dict[str, set[str | None]] = {}
    for edge in graph.edges:
        upstream = key_of.get(edge.upstream.stable_key)
        if upstream is None:
            continue
        consumers_of.setdefault(upstream, set()).add(key_of.get(edge.downstream.stable_key))
    unparsed = {d.model for d in analysis.diagnostics if d.code in _UNPARSED_CODES}

    proposals: list[FilterProposal] = []
    refusals: list[Refusal] = []
    for key in analysis.order:
        consumers = consumers_of.get(key)
        if not consumers:
            continue
        try:
            found = _propose_for_model(pipeline, analysis, unparsed, key, consumers)
        except _Refuse as refusal:
            refusals.append(Refusal(key, refusal.reason, refusal.detail))
            continue
        proposals.extend(found)
    return FilterPushdownResult(tuple(proposals), tuple(refusals))


def _propose_for_model(pipeline, analysis, unparsed, key, consumers) -> list[FilterProposal]:
    model = pipeline.models[key]
    if analysis.blind:
        raise _Refuse("graph_incomplete", "some model's reads are unknown")
    if None in consumers:
        raise _Refuse("unknown_consumer", "a reader outside the analysed models reads this model")
    if model.kind not in _PUSH_TARGET_KINDS:
        raise _Refuse("unsupported_target", f"a {model.kind} model is not a push target")
    if key in unparsed or key not in analysis.parsed or model.masked_expressions:
        raise _Refuse("target_unparsed", "the model's own query could not be fully analysed")
    order = sorted(consumers)  # type: ignore[type-var]
    per_reader: dict[str, list[list[exp.Expression]]] = {}
    for reader in order:
        reader_model = pipeline.models[reader]
        if reader in unparsed or reader not in analysis.parsed or reader_model.masked_expressions:
            raise _Refuse("consumer_unparsed", f"{reader} could not be fully analysed")
        occurrences = _occurrence_conjuncts(pipeline, analysis.parsed[reader], key)
        if not occurrences:
            raise _Refuse("consumer_unparsed", f"{reader} declares this model but no read was found")
        per_reader[reader] = occurrences

    candidates: dict[str, exp.Expression] = {}
    for occurrences in per_reader.values():
        for conjuncts in occurrences:
            for conjunct in conjuncts:
                candidates.setdefault(_key(conjunct), conjunct)
    if not candidates:
        raise _Refuse("no_common_filter", "no consumer filters this model on its own columns")

    common = [
        (text, node)
        for text, node in candidates.items()
        if all(
            _implied(text, node, conjuncts)
            for occurrences in per_reader.values()
            for conjuncts in occurrences
        )
    ]
    if not common:
        raise _Refuse("no_common_filter", "consumers have different or absent filters")

    select = analysis.parsed[key]
    if not isinstance(select, exp.Select):
        raise _Refuse("unsupported_target", "the model is not a single SELECT")
    if not _filters_safe_downstream(select):
        raise _Refuse("unsafe_target", "the model aggregates, deduplicates, windows or limits rows")
    existing = {_key(c) for c in _flatten(select.args.get("where").this)} if select.args.get("where") else set()

    identity = model.identity
    consumer_rows = tuple(
        {
            "node": pipeline.models[reader].identity.stable_key if pipeline.models[reader].identity else reader,
            "label": "unproven",
            "role": "already_filters",
        }
        for reader in order
    )
    proposals: list[FilterProposal] = []
    skipped: list[str] = []
    accepted: list[tuple[str, exp.Expression]] = []
    for text, node in common:
        if any(_implied(text, node, [prior]) and _implied(t, prior, [node]) for t, prior in accepted):
            continue  # equivalent to a candidate already proposed
        accepted.append((text, node))
        try:
            mapped = _map_to_sources(pipeline, key, select, node)
        except _Refuse as refusal:
            skipped.append(refusal.detail or refusal.reason)
            continue
        if _key(mapped) in existing:
            continue
        proposals.append(
            FilterProposal(
                id=f"{KIND}:{identity.stable_key if identity else key}:{len(proposals) + 1}",
                model=identity.stable_key if identity else key,
                predicate=mapped.sql("bigquery"),
                consumers=consumer_rows,
            )
        )
    if not proposals and skipped:
        raise _Refuse("unmappable_filter", "; ".join(skipped))
    return proposals


def _bare(node: exp.Expression) -> exp.Expression:
    """Copy of a predicate with column qualifiers and case removed."""

    copy = node.copy()
    for column in list(copy.find_all(exp.Column)):
        column.replace(exp.column(column.name.lower()))
    return _normalize_predicate(copy)


def _key(node: exp.Expression) -> str:
    return _bare(node).sql("bigquery")


def _implied(text: str, node: exp.Expression, conjuncts: list[exp.Expression]) -> bool:
    """Whether a reader occurrence applying ``conjuncts`` also applies ``node``."""

    if any(_key(c) == text for c in conjuncts):
        return True
    if not conjuncts:
        return False
    have = exp.and_(*[_bare(c) for c in conjuncts]).sql("bigquery")
    want = _bare(node).sql("bigquery")
    columns = sorted({c.name.lower() for c in _bare(node).find_all(exp.Column)}
                     | {c.name.lower() for k in conjuncts for c in k.find_all(exp.Column)})
    select = ", ".join(columns)
    result = prove_equivalent_smt(
        f"SELECT {select} FROM t WHERE {have}",
        f"SELECT {select} FROM t WHERE ({have}) AND ({want})",
        schema={"t": columns},
    )
    return result.status is SmtStatus.PROVEN_EQUIVALENT


def _occurrence_conjuncts(pipeline, query: exp.Expression, target: str) -> list[list[exp.Expression]]:
    """Filter conjuncts that apply to each read of ``target`` inside ``query``."""

    result: list[list[exp.Expression]] = []
    for table in query.find_all(exp.Table):
        if binding_cte(table) is not None:
            continue  # a WITH table in scope here; a nested ``WITH t`` does not hide a read of the model t elsewhere
        if pipeline.resolve(table) != target:
            continue
        select = table.find_ancestor(exp.Select)
        parent = table.parent
        if select is None or not isinstance(parent, (exp.From, exp.Join)):
            raise _Refuse("unsupported_read", "a consumer reads the model in an unsupported position")
        joins = select.args.get("joins") or []
        for join in joins:
            if (join.side or "").upper() in {"RIGHT", "FULL"}:
                raise _Refuse("unsupported_read", "a consumer reads the model beside a right or full join")
        if isinstance(parent, exp.Join) and (parent.side or "").upper() == "LEFT":
            raise _Refuse("unsupported_read", "a consumer reads the model on the nullable side of an outer join")
        alias = table.alias_or_name
        single = not joins and isinstance(parent, exp.From)
        where = select.args.get("where")
        conjuncts: list[exp.Expression] = []
        for conjunct in _flatten(where.this) if where else []:
            columns = list(conjunct.find_all(exp.Column))
            if not columns or conjunct.find(exp.Subquery, exp.Select):
                continue
            if conjunct.find(*_NONDETERMINISTIC):
                continue
            if all((c.table == alias) or (single and not c.table) for c in columns):
                conjuncts.append(conjunct)
        result.append(conjuncts)
    return result


def _map_to_sources(pipeline, key: str, select: exp.Select, predicate: exp.Expression) -> exp.Expression:
    """Rewrite a predicate on the model's output columns into its source columns."""

    explicit: dict[str, list[exp.Expression | None]] = {}
    stars: list[str | None] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Alias):
            inner = projection.this
            explicit.setdefault(projection.alias.lower(), []).append(
                inner if isinstance(inner, exp.Column) and not isinstance(inner.this, exp.Star) else None
            )
        elif isinstance(projection, exp.Column) and not isinstance(projection.this, exp.Star):
            explicit.setdefault(projection.name.lower(), []).append(projection)
        elif isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            if star_modified(projection):
                raise _Refuse("unmappable_filter", "the model's star projection has EXCEPT, REPLACE, RENAME or ILIKE")
            stars.append(projection.table or None if isinstance(projection, exp.Column) else None)
        else:
            name = projection.alias_or_name.lower()
            if name:
                explicit.setdefault(name, []).append(None)
    outputs = {c.lower() for c in pipeline.output_columns(key)}
    source_count = 1 + len(select.args.get("joins") or [])
    result = predicate.copy()
    for column in list(result.find_all(exp.Column)):
        name = column.name.lower()
        if name in explicit:
            found = explicit[name]
            if len(found) != 1 or found[0] is None:
                raise _Refuse("unmappable_filter", f"column {name} is computed or ambiguous in the model")
            column.replace(found[0].copy())
        elif stars and name in outputs and (source_count == 1 or None not in stars):
            if len(stars) != 1:
                raise _Refuse("unmappable_filter", f"column {name} may come from several star projections")
            column.replace(exp.column(name, table=stars[0]) if stars[0] else exp.column(name))
        else:
            raise _Refuse("unmappable_filter", f"column {name} is not traceable to the model's sources")
    return result


__all__ = [
    "FilterPushdownResult",
    "FilterProposal",
    "Refusal",
    "find_upstream_filter_proposals",
]
