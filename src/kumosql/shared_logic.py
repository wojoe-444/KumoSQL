"""Proposals to extract logic repeated across models into one shared model.

A proposal is a report only. Nothing here edits a model. ``ready`` is true only
when the refactor was applied to every copy (``refactor_sql``) and the prover
showed each edited model unchanged, so every consumer reads exactly what it
read before; the readers downstream of an edited model inherit that result.
Anything the prover cannot show, and any proposal whose reader list is
incomplete, stays ``unknown`` and not ready.

The consumer list of a proposal holds every model that would be edited (the
models containing a copy) and, through the query graph, every model that reads
one of them, directly or transitively. When the graph cannot be trusted to show
every reader, the proposal says so in ``consumers_complete`` and
``incomplete_reasons`` instead of implying the list is exhaustive.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace as dataclass_replace
import hashlib
from typing import TYPE_CHECKING, Iterable, Mapping

import sqlglot
from sqlglot import exp

from kumo_sql_prover.canonical import _declared, free_cte_refs
from .graph import GraphResult, ObservedRead, build_query_graph
from .pipeline_duplicates import _fingerprint, _select_location
from kumo_sql_prover.ast_utils import select_sources
from kumo_sql_prover.scalar_subqueries import _select_names

if TYPE_CHECKING:
    from .near_duplicates import NearDuplicateCluster
    from .pipeline import Pipeline

UNKNOWN = "unknown"
# Cluster kinds that have no plain shared SELECT to extract.
_NOT_EXTRACTABLE = {"literal_parameters", "mixed"}


@dataclass(frozen=True)
class Consumer:
    node: str
    role: str  # "edit_site" (contains a copy) or "downstream" (reads an edit site)
    label: str = UNKNOWN  # verification result; unknown until something verifies it
    via: tuple[str, ...] = ()  # edit sites this consumer reads, directly or not

    def to_json(self) -> dict:
        data = {"node": self.node, "label": self.label, "role": self.role}
        if self.via:
            data["via"] = list(self.via)
        return data


@dataclass(frozen=True)
class SharedLogicProposal:
    id: str
    title: str
    origin: str  # "exact_duplicate" or the near-duplicate cluster kind
    shared_sql: str
    sites: tuple[tuple[str, str], ...]  # (model, location) of every copy to replace
    consumers: tuple[Consumer, ...]
    incomplete_reasons: tuple[str, ...] = ()
    cost_rationale: str = UNKNOWN
    observed_reads_included: bool = False
    # Per-site filters the copy would still apply after the extraction.
    residual_filters: Mapping[tuple[str, str], tuple[str, ...]] | None = None
    # Fingerprint of the copy at each site, to find it again inside its model.
    site_fingerprints: Mapping[tuple[str, str], str] | None = None
    # Verification label per model, filled by ``verify_proposal``; empty means unverified.
    verification: Mapping[str, str] | None = None
    unready_reasons: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return bool(self.verification) and not self.unready_reasons

    @property
    def consumers_complete(self) -> bool:
        return not self.incomplete_reasons

    def _consumer_json(self, consumer: Consumer) -> dict:
        data = consumer.to_json()
        if self.verification and consumer.node in self.verification:
            data["label"] = self.verification[consumer.node]
        return data

    def to_json(self) -> dict:
        data = {
            "id": self.id,
            "kind": "shared_logic",
            "title": self.title,
            "origin": self.origin,
            "cost_rationale": self.cost_rationale,
            "consumers": [self._consumer_json(c) for c in self.consumers],
            "consumers_complete": self.consumers_complete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "observed_reads_included": self.observed_reads_included,
            "shared_sql": self.shared_sql,
            "sites": [{"node": m, "location": loc} for m, loc in self.sites],
            "ready": self.ready,
        }
        if self.unready_reasons:
            data["not_ready_reasons"] = list(self.unready_reasons)
        if self.residual_filters:
            data["residual_filters"] = [
                {"node": m, "location": loc, "filters": list(f)}
                for (m, loc), f in self.residual_filters.items()
            ]
        return data


def propose_shared_logic(
    pipeline: "Pipeline",
    observed_reads: Iterable[ObservedRead | Mapping[str, object]] = (),
    *,
    min_nodes: int = 12,
    threshold: float = 0.7,
    verify: bool = True,
    verify_limit: int = 200,
) -> list[SharedLogicProposal]:
    """Propose extracting repeated SELECTs, exact and near, into shared models.

    With ``verify`` each proposal (up to ``verify_limit`` of them, largest first) is applied to
    its copies and checked with :func:`verify_proposal`; the rest stay unverified.
    """

    observed = tuple(observed_reads)
    graph = build_query_graph(pipeline, observed)
    readers = _reader_map(pipeline, graph)
    gaps = _gap_reasons(pipeline, graph)

    proposals: list[SharedLogicProposal] = []
    for group in pipeline.duplicate_selects(min_nodes=min_nodes):
        proposals.append(_from_group(group, readers, gaps, bool(observed)))
    for cluster in pipeline.near_duplicate_selects(min_nodes=min_nodes, threshold=threshold):
        proposal = _from_cluster(cluster, readers, gaps, bool(observed))
        if proposal is not None:
            proposals.append(proposal)
    if verify:
        proposals = [
            verify_proposal(pipeline, p) if i < verify_limit else p for i, p in enumerate(proposals)
        ]
    return proposals


def proposals_json(proposals: Iterable[SharedLogicProposal]) -> dict:
    return {"proposals": [p.to_json() for p in proposals]}


# ------------------------------------------------------------- verification

SHARED_ALIAS = "_shared"


def _find_copy(tree: exp.Expression, location: str, fingerprint: str) -> exp.Expression | None:
    for select in tree.find_all(exp.Select):
        if _select_location(select) == location and _fingerprint(select)[0] == fingerprint:
            return select
    return None


def refactor_sql(
    proposal: SharedLogicProposal, model_sql: str, site: tuple[str, str], *, shared_model: str | None = None
) -> str | None:
    """``model_sql`` with the copy at ``site`` replaced by a read of the shared SELECT.

    The shared SELECT is inlined as a derived table, so the result stands for the model as it would
    read the new shared model; with ``shared_model`` the copy reads that table instead. The copy keeps its own output columns and applies its residual
    filters. ``None`` when the copy cannot be replaced safely (a star or unnamed projection,
    the copy is not found, or the shared SELECT does not parse).
    """

    fingerprint = (proposal.site_fingerprints or {}).get(site)
    if fingerprint is None or not proposal.shared_sql:
        return None
    try:
        tree = sqlglot.parse_one(model_sql, read="bigquery")
        shared = sqlglot.parse_one(proposal.shared_sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return None
    if tree is None or not isinstance(shared, exp.Select):
        return None
    copy = _find_copy(tree, site[1], fingerprint)
    if copy is None:
        return None
    names = [projection.alias_or_name for projection in copy.expressions]
    if not names or any(not name or name == "*" for name in names):
        return None
    shared_columns = {projection.alias_or_name.lower() for projection in shared.expressions}
    if not {name.lower() for name in names} <= shared_columns:
        return None
    alias = exp.TableAlias(this=exp.to_identifier(SHARED_ALIAS))
    source = (
        exp.Table(this=exp.to_identifier(shared_model), alias=alias)
        if shared_model
        else exp.Subquery(this=shared, alias=alias)
    )
    replacement = exp.select(*[exp.column(name) for name in names]).from_(source)
    for text in (proposal.residual_filters or {}).get(site, ()):
        try:
            replacement = replacement.where(sqlglot.parse_one(text, read="bigquery"))
        except sqlglot.errors.SqlglotError:
            return None
    if copy is tree:
        return replacement.sql(dialect="bigquery")
    copy.replace(replacement)
    return tree.sql(dialect="bigquery")


def _outside_reads(proposal: SharedLogicProposal, model_sql: str | None, sites) -> list[str]:
    """CTEs the copies read that their model defines outside them; a shared model could not see these."""

    if not model_sql:
        return []
    try:
        tree = sqlglot.parse_one(model_sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return []
    names: set[str] = set()
    for site in sites:
        fingerprint = (proposal.site_fingerprints or {}).get(site)
        copy = _find_copy(tree, site[1], fingerprint) if tree is not None and fingerprint else None
        if copy is not None:
            names |= set(free_cte_refs(copy))
    return sorted(names)


def _identical_copies(proposal: SharedLogicProposal, model_sql: str, sites) -> bool:
    """Whether every copy in the model is the shared SELECT itself and reads it back column for column.

    Equal fingerprints mean equal canonical text, and every canonicalization step keeps the meaning,
    so the copy and the shared SELECT are one query; selecting its own columns from it changes nothing.
    """

    try:
        tree = sqlglot.parse_one(model_sql, read="bigquery")
        shared = sqlglot.parse_one(proposal.shared_sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return False
    if tree is None or not isinstance(shared, exp.Select):
        return False
    shared_names = [p.alias_or_name.lower() for p in shared.expressions]
    shared_fingerprint = _fingerprint(shared)[0]
    for site in sites:
        fingerprint = (proposal.site_fingerprints or {}).get(site)
        copy = _find_copy(tree, site[1], fingerprint) if fingerprint else None
        if copy is None or fingerprint != shared_fingerprint:
            return False  # the SELECT to share is not what the copy is
        names = [p.alias_or_name.lower() for p in copy.expressions]
        if names != shared_names or len(set(names)) != len(names) or "" in names or "*" in names:
            return False
    return True


_NOT_PLAIN = ("distinct", "group", "having", "qualify", "limit", "offset", "joins_lateral")


def _merges_back(proposal: SharedLogicProposal, model_sql: str, sites) -> bool:
    """Whether reading the shared SELECT and filtering it is the copy itself, by view merging.

    For a shared SELECT with no DISTINCT, grouping, window or limit, ``SELECT cols FROM (shared) WHERE r``
    is ``shared`` with the columns replaced by the shared expressions of those names and ``r`` added to
    its WHERE. When that merged SELECT has the copy's canonical text, the copy and the refactor are one query.
    """

    try:
        tree = sqlglot.parse_one(model_sql, read="bigquery")
        shared = sqlglot.parse_one(proposal.shared_sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return False
    if tree is None or not isinstance(shared, exp.Select):
        return False
    if any(shared.args.get(key) for key in _NOT_PLAIN) or shared.find(exp.Window) or shared.find(exp.AggFunc):
        return False
    by_name: dict[str, exp.Expression] = {}
    for projection in shared.expressions:
        name = projection.alias_or_name.lower()
        if not name or name == "*" or name in by_name:
            return False
        by_name[name] = projection.unalias() if isinstance(projection, exp.Alias) else projection
    for site in sites:
        fingerprint = (proposal.site_fingerprints or {}).get(site)
        copy = _find_copy(tree, site[1], fingerprint) if fingerprint else None
        if copy is None:
            return False
        names = [p.alias_or_name.lower() for p in copy.expressions]
        if not names or any(n not in by_name for n in names) or len(set(names)) != len(names):
            return False
        merged = shared.copy()
        projections = []
        for projection, name in zip(copy.expressions, names):
            expression = by_name[name].copy()
            projections.append(expression if isinstance(expression, exp.Column) and expression.name.lower() == name
                               else exp.alias_(expression, projection.alias_or_name, copy=False))
        merged.set("expressions", projections)
        for text in (proposal.residual_filters or {}).get(site, ()):
            try:
                condition = sqlglot.parse_one(text, read="bigquery")
            except sqlglot.errors.SqlglotError:
                return False
            for column in list(condition.find_all(exp.Column)):
                target = by_name.get(column.name.lower())
                if target is None or column.table:
                    return False
                column.replace(target.copy())
            merged = merged.where(condition, copy=False)
        if _fingerprint(merged)[0] != _fingerprint(copy)[0]:
            return False
    return True


def _known_columns(pipeline: "Pipeline | None"):
    """A lookup from a table in SQL to its lower-case column names, or ``None`` where they are not known."""

    if pipeline is None:
        return lambda table: None

    def lookup(table: exp.Table):
        try:
            key = pipeline._resolver.resolve(table) or ".".join(p.name for p in table.parts)
            columns = {k.lower(): v for k, v in pipeline.source_schema.items()}.get(key.lower())
            if columns is None:
                columns = pipeline._analyse().outputs.get(key)
        except Exception:  # noqa: BLE001  # unknown columns make the caller careful, never wrong
            return None
        return None if columns is None else {c.lower() for c in columns}

    return lookup


def _binding(column: exp.Column, scopes: list[exp.Select], shared: exp.Expression, columns_of) -> str:
    """Where an unqualified column of the shared SELECT comes from: ``bound``, ``free`` or ``unknown``.

    ``bound``: a source inside the SELECT provides it. ``free``: every source on the way is known and none does,
    so it can only come from the query around the copy. ``unknown``: some source's columns are not known.
    """

    name = column.name.lower()
    ctes = {c.alias_or_name.lower(): c.this for c in shared.find_all(exp.CTE)}
    for scope in scopes:
        unknown = False
        for source in select_sources(scope):
            if isinstance(source, exp.Table):
                cte = None if source.args.get("db") else ctes.get(source.name.lower())
                names = _select_names(cte) if cte is not None else columns_of(source)
            elif isinstance(source, exp.Subquery):
                names = _select_names(source.this)
            else:
                names = None
            if names is None:
                unknown = True
            elif name in names:
                return "bound"
        if unknown:
            return "unknown"
        if name in {(item.alias or "").lower() for item in scope.expressions}:
            return "bound"  # an output alias used in GROUP BY, HAVING or ORDER BY
    return "free"


def _not_shareable(proposal: SharedLogicProposal, pipeline: "Pipeline | None" = None) -> str:
    """Why one shared table cannot stand in for the copies whatever their text, or ``""``.

    Equal canonical text says the copies are the same code, not that one materialized result serves them all:
    a clock or RAND is evaluated again by every build, a Dataform expression the loader masked may differ
    between models, and a SELECT that reads a column of the query around it has no rows on its own.
    """

    from .pipeline_equivalence import run_dependent

    text = proposal.shared_sql or ""
    if "__sqlx_" in text or "${" in text:
        return "the shared SELECT holds a Dataform expression that was not resolved"
    try:
        shared = sqlglot.parse_one(text, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return "the shared SELECT does not parse"
    if shared is None:
        return "the shared SELECT does not parse"
    why = run_dependent(shared)
    if why:
        return f"the shared SELECT computes {why.split(': ', 1)[-1]}, which each build evaluates again"
    # A scalar subquery sits in an expression of the query around it, so it may read that query's columns.
    nested = any(
        not location.startswith(("query", "cte:", "subquery:")) or location == "subquery:<anonymous>"
        for _, location in proposal.sites
    )
    columns_of = _known_columns(pipeline)
    for column in shared.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        qualifier = column.table.lower() if column.table else ""
        scopes = [s for s in [column.find_ancestor(exp.Select)] if s is not None]
        while scopes[-1:] and scopes[-1].find_ancestor(exp.Select) is not None:
            scopes.append(scopes[-1].find_ancestor(exp.Select))
        declared = {name for scope in scopes for name in _declared(scope)}
        if qualifier and qualifier not in declared:
            return f"the shared SELECT reads {column.sql(dialect='bigquery')} from the query around it"
        if not qualifier:
            where = _binding(column, scopes, shared, columns_of)
            if where == "free":
                return f"the shared SELECT reads {column.name} from the query around it"
            if where == "unknown" and nested:
                return "a copy sits inside an expression and reads a column it does not qualify, which may belong to the query around it"
    return ""


def verify_proposal(pipeline: "Pipeline", proposal: SharedLogicProposal) -> SharedLogicProposal:
    """Apply the refactor to every copy and label each consumer by what the prover shows.

    An edited model is ``proven`` when the prover shows it returns the same rows after the refactor.
    A reader downstream of edited models is ``unchanged`` when all of them are proven (it reads
    exactly the rows it read before). Everything else is ``unknown``. The proposal is ready only
    with every consumer proven or unchanged and a complete reader list.
    """

    from .proposal_readiness import ConsumerResult, assess_proposal, verify_consumer

    results: dict[str, ConsumerResult] = {}
    by_model: dict[str, list[tuple[str, str]]] = {}
    for site in proposal.sites:
        by_model.setdefault(site[0], []).append(site)
    blocked = _not_shareable(proposal, pipeline)
    for model_key, sites in by_model.items():
        if blocked:
            results[model_key] = ConsumerResult(model_key, "unknown", blocked)
            continue
        model = pipeline.models.get(model_key)
        before = getattr(model, "sql", None) if model is not None else None
        outside = _outside_reads(proposal, before, sites)
        if outside:
            results[model_key] = ConsumerResult(
                model_key, "unknown", f"the copy reads {', '.join(outside)}, defined in the model outside the copy"
            )
            continue
        after = before
        for site in sites:  # a model may hold several copies; apply them one after another
            after = refactor_sql(proposal, after, site) if after else None
        if after and proposal.origin == "exact_duplicate" and _identical_copies(proposal, before, sites):
            results[model_key] = ConsumerResult(
                model_key, "proven", "every copy is the shared SELECT itself once aliases and clause order are ignored"
            )
            continue
        if after and before and _merges_back(proposal, before, sites):
            results[model_key] = ConsumerResult(
                model_key, "proven", "merging the shared SELECT back into each copy gives the copy itself"
            )
            continue
        results[model_key] = verify_consumer(before, after, node=model_key)
    for consumer in proposal.consumers:
        if consumer.role != "downstream":
            continue
        proven = all(results.get(origin) and results[origin].label in ("proven", "unchanged") for origin in consumer.via)
        results[consumer.node] = ConsumerResult(
            consumer.node,
            "unchanged" if proven else "unknown",
            "every model it reads through returns the same rows" if proven else "an edited model it reads was not proven unchanged",
        )
    assessed = assess_proposal(proposal.to_json(), results)
    labels = {c["node"]: c["label"] for c in assessed["consumers"]}
    reasons = tuple(assessed["not_ready_reasons"])
    return dataclass_replace(proposal, verification=labels, unready_reasons=reasons)


# ----------------------------------------------------------------- internals


def _from_group(group, readers, gaps, observed) -> SharedLogicProposal:
    sites = tuple((o.model, o.location) for o in group.occurrences)
    models = {m for m, _ in sites}
    return SharedLogicProposal(
        id=f"shared-{group.fingerprint}",
        title=f"Extract a SELECT repeated in {len(sites)} places into one shared model",
        origin="exact_duplicate",
        shared_sql=group.sql,
        sites=sites,
        consumers=_consumers(models, readers),
        incomplete_reasons=gaps,
        observed_reads_included=observed,
        site_fingerprints={site: group.fingerprint for site in sites},
    )


def _from_cluster(cluster: "NearDuplicateCluster", readers, gaps, observed):
    if cluster.kind in _NOT_EXTRACTABLE or not cluster.shared_sql:
        return None
    sites = tuple(occ for v in cluster.variants for occ in v.occurrences)
    models = {m for m, _ in sites}
    residual = {
        occ: v.residual_filters
        for v in cluster.variants
        if v.residual_filters
        for occ in v.occurrences
    }
    digest = hashlib.sha1(
        "|".join(sorted(v.fingerprint for v in cluster.variants)).encode()
    ).hexdigest()[:16]
    return SharedLogicProposal(
        id=f"shared-{digest}",
        title=f"Extract a near-identical SELECT repeated in {len(sites)} places into one shared model",
        origin=cluster.kind,
        shared_sql=cluster.shared_sql,
        sites=sites,
        consumers=_consumers(models, readers),
        incomplete_reasons=gaps,
        observed_reads_included=observed,
        residual_filters=residual or None,
        site_fingerprints={occ: v.fingerprint for v in cluster.variants for occ in v.occurrences},
    )


def _reader_map(pipeline: "Pipeline", graph: GraphResult) -> dict[str, set[str]]:
    """Direct readers per node, named by model key where the node is a model."""

    name_of: dict[str, str] = {}
    for key, model in pipeline.models.items():
        identity = model.identity
        if identity is not None:
            name_of[identity.stable_key] = key

    def name(identity) -> str:
        return name_of.get(identity.stable_key, identity.key or identity.stable_key)

    readers: dict[str, set[str]] = {}
    for edge in graph.edges:
        readers.setdefault(name(edge.upstream), set()).add(name(edge.downstream))
    # Edges the pipeline itself knows about stay visible even if the graph
    # could not place them.
    for parent, children in pipeline.downstream.items():
        readers.setdefault(parent, set()).update(children)
    return readers


def _consumers(edit_models: set[str], readers: dict[str, set[str]]) -> tuple[Consumer, ...]:
    via: dict[str, set[str]] = {}
    for start in sorted(edit_models):
        seen = {start}
        queue = deque([start])
        while queue:  # cycle-safe breadth-first walk
            for child in sorted(readers.get(queue.popleft(), ())):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
                    via.setdefault(child, set()).add(start)
    consumers = [Consumer(m, "edit_site") for m in sorted(edit_models)]
    consumers += [
        Consumer(node, "downstream", via=tuple(sorted(origins)))
        for node, origins in sorted(via.items())
        if node not in edit_models
    ]
    return tuple(consumers)


def _gap_reasons(pipeline: "Pipeline", graph: GraphResult) -> tuple[str, ...]:
    reasons: list[str] = []
    for diag in pipeline.all_diagnostics():
        if diag.code in ("parse_error", "no_query"):
            reasons.append(f"{diag.model} could not be parsed, so its reads are unknown")
    for model in pipeline.models.values():
        if not model.is_query and not model.declared_dependencies and model.sql.strip():
            reasons.append(f"{model.key or model.path} is not a query and declares no dependencies")
    if graph.unresolved_observation_count:
        reasons.append(
            f"{graph.unresolved_observation_count} observed reads referenced tables the graph could not identify"
        )
    if graph.unattributed_observation_count:
        reasons.append(
            f"{graph.unattributed_observation_count} observed reads had no destination to attribute them to"
        )
    return tuple(dict.fromkeys(reasons))
