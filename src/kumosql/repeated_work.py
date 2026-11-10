"""Detect work that repeats inside one query and across assets.

This is detection only. It reuses the exact-duplicate and near-duplicate
SELECT analyses and adds a repeated-scan view (the same table read more than
once). It never states how many times something executes or what it costs:
a repeated CTE body may or may not be reused by the engine, and cost needs
measured data from elsewhere.

Every finding carries ``certainty`` so similarity is never mistaken for identity:

* ``identical_text``: normalized SELECT text is the same and it reads the same
  tables, so the logic is textually identical.
* ``similar``: a near-duplicate cluster; the copies differ.
* ``same_source``: the same table is referenced in several places.

The JSON shape follows ``opportunities[].repeats[] {node, where}`` from the UI
roadmap; ``node`` is the asset (model key) and ``where`` the place inside it.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from sqlglot import exp

from kumo_sql_prover.ast_utils import binding_cte
from .pipeline import _fingerprint, _select_location

if TYPE_CHECKING:
    from .pipeline import Pipeline


@dataclass(frozen=True)
class Repeat:
    node: str
    where: str

    def to_json(self) -> dict[str, str]:
        return {"node": self.node, "where": self.where}


@dataclass(frozen=True)
class RepeatedWork:
    id: str
    kind: str  # "identical_logic", "similar_logic" or "repeated_scan"
    certainty: str  # "identical_text", "similar" or "same_source"
    scope: str  # "within_query", "across_assets" or "both"
    title: str
    repeats: tuple[Repeat, ...]
    tables: tuple[str, ...] = ()
    sql: str | None = None

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(sorted({r.node for r in self.repeats}))

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "certainty": self.certainty,
            "scope": self.scope,
            "title": self.title,
            "occurrences": len(self.repeats),
            "models": list(self.models),
            "tables": list(self.tables),
            "repeats": [r.to_json() for r in self.repeats],
            **({"sql": self.sql} if self.sql is not None else {}),
        }


def find_repeated_work(
    pipeline: "Pipeline",
    *,
    min_nodes: int = 12,
    similarity: float = 0.7,
    min_readers: int = 3,
) -> list[RepeatedWork]:
    """Repeated logic and repeated scans, grouped by kind and ordered by size."""

    parsed = pipeline._analyse().parsed
    signatures = _read_signatures(pipeline, parsed, min_nodes)
    found: list[RepeatedWork] = []
    found.extend(_identical(pipeline, signatures, min_nodes))
    found.extend(_similar(pipeline, min_nodes, similarity))
    found.extend(_repeated_scans(pipeline, parsed, min_readers))
    return found


def repeated_work_report(pipeline: "Pipeline", **options: object) -> dict:
    """JSON-serialisable form: ``{"opportunities": [...]}`` with ``repeats`` per item."""

    return {"opportunities": [w.to_json() for w in find_repeated_work(pipeline, **options)]}  # type: ignore[arg-type]


# ------------------------------------------------------------------- helpers


def _scope(models: Iterable[str]) -> str:
    counts = Counter(models)
    within = any(n > 1 for n in counts.values())
    across = len(counts) > 1
    return "both" if within and across else "across_assets" if across else "within_query"


def _short(text: str, limit: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:limit]


def _cte_definition(table: exp.Table) -> exp.Expression | None:
    """The CTE body a bare table name refers to, searching enclosing scopes (``WITH t AS (SELECT * FROM t)`` reads the table t)."""

    cte = binding_cte(table)
    return cte.this if cte is not None else None


def _reads(pipeline: "Pipeline", select: exp.Expression) -> tuple[str, ...]:
    """Canonical tables a SELECT reads. CTE references are keyed by their body."""

    items: set[str] = set()
    for table in select.find_all(exp.Table):
        definition = _cte_definition(table)
        if definition is not None:
            items.add("cte#" + _fingerprint(definition)[0])
        else:
            items.add(pipeline.resolve(table) or ".".join(p.name for p in table.parts).lower())
    return tuple(sorted(items))


def _read_signatures(
    pipeline: "Pipeline", parsed: dict[str, exp.Expression], min_nodes: int
) -> dict[tuple[str, str, str], list[tuple[str, ...]]]:
    result: dict[tuple[str, str, str], list[tuple[str, ...]]] = defaultdict(list)
    for model, query in parsed.items():
        for select in query.find_all(exp.Select):
            if sum(1 for _ in select.walk()) < min_nodes:
                continue
            fingerprint, _ = _fingerprint(select)
            result[(model, _select_location(select), fingerprint)].append(_reads(pipeline, select))
    return result


def _identical(pipeline, signatures, min_nodes) -> list[RepeatedWork]:
    found = []
    remaining = {key: list(value) for key, value in signatures.items()}
    for group in pipeline.duplicate_selects(min_nodes=min_nodes):
        by_reads: dict[tuple[str, ...], list] = defaultdict(list)
        for occurrence in group.occurrences:
            queue = remaining.get((occurrence.model, occurrence.location, group.fingerprint), [])
            reads = queue.pop(0) if queue else ()
            by_reads[reads].append(occurrence)
        # Identical text over different tables is not the same work.
        for reads, members in by_reads.items():
            if len(members) < 2:
                continue
            found.append(
                RepeatedWork(
                    id=f"rw-identical-{_short(group.fingerprint + '|' + '|'.join(reads))}",
                    kind="identical_logic",
                    certainty="identical_text",
                    scope=_scope(o.model for o in members),
                    title=f"Identical logic appears {len(members)} times",
                    repeats=tuple(Repeat(o.model, o.location) for o in members),
                    tables=tuple(r for r in reads if not r.startswith("cte#")),
                    sql=group.sql,
                )
            )
    return sorted(found, key=lambda w: (-len(w.repeats), w.id))


def _similar(pipeline, min_nodes, similarity) -> list[RepeatedWork]:
    found = []
    for cluster in pipeline.near_duplicate_selects(min_nodes=min_nodes, threshold=similarity):
        repeats = tuple(
            Repeat(model, location)
            for variant in cluster.variants
            for model, location in variant.occurrences
        )
        found.append(
            RepeatedWork(
                id=f"rw-similar-{_short('|'.join(v.fingerprint for v in cluster.variants))}",
                kind="similar_logic",
                certainty="similar",
                scope=_scope(r.node for r in repeats),
                title=f"Similar logic appears in {len(repeats)} places",
                repeats=repeats,
                sql=cluster.representative.sql,
            )
        )
    return sorted(found, key=lambda w: (-len(w.repeats), w.id))


def _owner_select(node: exp.Expression) -> exp.Expression:
    current = node.parent
    while current is not None and not isinstance(current, exp.Select):
        current = current.parent
    return current if current is not None else node


def _repeated_scans(pipeline, parsed, min_readers) -> list[RepeatedWork]:
    refs: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for model, query in parsed.items():
        for table in query.find_all(exp.Table):
            if _cte_definition(table) is not None:
                continue
            target = pipeline.resolve(table) or ".".join(p.name for p in table.parts).lower()
            if not target or target == model:
                continue
            refs[target][model].append(_select_location(_owner_select(table)))
    found = []
    for target, by_model in sorted(refs.items()):
        across = len(by_model) >= min_readers
        multi = {m for m, places in by_model.items() if len(places) > 1}
        if not across and not multi:
            continue
        repeats = tuple(
            Repeat(model, f"scan of {target} ({place})")
            for model, places in sorted(by_model.items())
            for place in places
            if across or model in multi
        )
        found.append(
            RepeatedWork(
                id=f"rw-scan-{_short(target)}",
                kind="repeated_scan",
                certainty="same_source",
                scope=_scope(r.node for r in repeats),
                title=f"{target} is read {len(repeats)} times",
                repeats=repeats,
                tables=(target,),
            )
        )
    return sorted(found, key=lambda w: (-len(w.repeats), w.id))
