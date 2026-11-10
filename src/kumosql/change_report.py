"""Semantic change report: behavior, cost and blast radius side by side.

Two project snapshots (base and head) are compared model by model. Each
changed model carries

* ``verification``: the existing evidence label from :func:`verify_rewrite`,
  computed on the raw file text where it exists,
* ``cost``: caller-supplied figures only; ``unavailable`` when none are given,
* ``consumers``: transitive readers taken from the query graph, with
  ``complete`` false whenever the graph has gaps.

The shape matches ``report`` in ``docs/ui-roadmap.md``. Nothing here needs
credentials or network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .graph import build_query_graph
from .overlap_report import MAX_COMPARED_MODELS, OverlapChecker, mark_retiring, unavailable_section
from .resilience import extended_path
from .pipeline import Model, Pipeline, load_compiled_graph, load_sqlx_project
from .rewrite import verify_rewrite
from .table_profile import profile_pipeline
from .scopes import Scope, get_scope

COST_BASES = ("measured", "estimate", "upper_bound")
_PARSE_CODES = {"parse_error", "no_query", "qualify_error", "sqlx_parse_error", "unsupported_ref"}


def _display(model: Model) -> str:
    t = model.target
    return ".".join(p for p in (t.schema, t.name) if p) or model.key or (model.path or "")


def _raw_text(root: Path | None, model: Model) -> str:
    """Raw file text when available, so SQLX block checks apply."""

    if root is not None and model.path:
        try:
            return (extended_path(root) / model.path).read_text(encoding="utf-8")
        except OSError:
            pass
    return model.sql


def _definition_changes(old: Model, new: Model) -> list[str]:
    """Parts of a model other than its query whose change can alter what it materializes.

    A compiled graph carries the operations around the query, the table kind,
    the declared dependencies and the assertions as fields, not as text the
    query comparison sees, so two models with identical queries can still differ.
    """

    changed = []
    if old.kind != new.kind:
        changed.append("kind")
    pre = lambda m: tuple(t.strip() for t in m.operations_sql[: m.pre_operations])  # noqa: E731
    post = lambda m: tuple(t.strip() for t in m.operations_sql[m.pre_operations :])  # noqa: E731
    if pre(old) != pre(new):
        changed.append("pre_operations")
    if post(old) != post(new):
        changed.append("post_operations")
    if {d.key for d in old.declared_dependencies} != {d.key for d in new.declared_dependencies}:
        changed.append("dependencies")
    if (set(old.non_null), set(old.unique_keys)) != (set(new.non_null), set(new.unique_keys)):
        changed.append("constraints")
    return changed


def _unchanged_text_reason(delta, definition: list[str]) -> str:
    parts = []
    if delta:
        parts.append("its resolved output changed: " + _delta_reason(delta))
    if definition:
        parts.append(f"{', '.join(definition)} changed; the effect is not verified")
    return "model text is unchanged but " + "; ".join(parts)


def normalize_cost(entry: Mapping[str, object] | None) -> dict[str, object]:
    """Validate one caller-supplied cost entry; absent input reads as unknown."""

    if not entry:
        return {"basis": "unavailable"}
    basis = entry.get("basis")
    if basis not in COST_BASES:
        raise ValueError(f"cost basis must be one of {', '.join(COST_BASES)}, got {basis!r}")
    out: dict[str, object] = {"basis": basis}
    for key in ("before", "after"):
        value = entry.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"cost {key} must be a number")
            out[key] = value
    if "before" not in out and "after" not in out:
        return {"basis": "unavailable"}
    return out


class _Consumers:
    """Reader closure over one pipeline's query graph."""

    def __init__(self, pipeline: Pipeline):
        graph = build_query_graph(pipeline)
        self.names: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        for key, model in pipeline.models.items():
            if model.identity is not None:
                self.ids[key] = model.identity.stable_key
                self.names[model.identity.stable_key] = _display(model)
        self.readers: dict[str, set[str]] = {}
        self.low: set[str] = set()
        for edge in graph.edges:
            up, down = edge.upstream.stable_key, edge.downstream.stable_key
            self.readers.setdefault(up, set()).add(down)
            if edge.confidence == "low":
                self.low.add(down)
        self.gap = (
            pipeline._analyse().blind
            or any(d.code in _PARSE_CODES for d in pipeline.all_diagnostics())
            or bool(graph.unresolved_observation_count or graph.unattributed_observation_count)
        )

    def of(self, key: str) -> dict[str, object]:
        start = self.ids.get(key)
        seen: set[str] = set()
        stack = [start] if start else []
        while stack:
            for nxt in self.readers.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        complete = start is not None and not self.gap and not (seen & self.low)
        return {"models": sorted(self.names.get(s, s) for s in seen), "complete": complete}


class _Contracts:
    """Each model's resolved output contract in one pipeline: columns, one-hop column lineage and tables read.

    Two snapshots can hold identical model text yet resolve differently, because a supplied source schema
    changed under a ``SELECT *``. The text comparison cannot see that; this one can.
    """

    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self.reads = pipeline.table_reads()
        self.lineage: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
        for ref, record in pipeline.explain_lineage().items():
            sources = tuple(sorted({str(s) for s in record.sources}))
            self.lineage.setdefault(ref.table, {})[ref.column] = (record.status, sources)

    def of(self, key: str) -> dict[str, object]:
        return {
            "columns": list(self.pipeline.output_columns(key)),
            "lineage": self.lineage.get(key, {}),
            "reads": sorted(self.reads.get(key, ())),
        }


def contract_delta(old: Mapping[str, object], new: Mapping[str, object]) -> dict[str, object]:
    """What differs between two models' resolved contracts; empty when nothing does."""

    delta: dict[str, object] = {}
    old_cols, new_cols = list(old["columns"]), list(new["columns"])
    if old_cols != new_cols:
        delta["columns_added"] = [c for c in new_cols if c not in old_cols]
        delta["columns_removed"] = [c for c in old_cols if c not in new_cols]
        if not delta["columns_added"] and not delta["columns_removed"]:
            delta["columns_reordered"] = True
    old_lin, new_lin = old["lineage"], new["lineage"]
    moved = sorted(c for c in set(old_lin) & set(new_lin) if old_lin[c] != new_lin[c])
    if moved:
        delta["lineage_changed"] = moved
    old_reads, new_reads = set(old["reads"]), set(new["reads"])
    if old_reads != new_reads:
        delta["reads_added"], delta["reads_removed"] = sorted(new_reads - old_reads), sorted(old_reads - new_reads)
    # Columns that appeared or vanished carry their lineage with them; the column lists already say so.
    return delta


def _delta_reason(delta: Mapping[str, object]) -> str:
    parts = []
    for key, text in (
        ("columns_added", "output columns added"), ("columns_removed", "output columns removed"),
        ("lineage_changed", "column lineage changed for"), ("reads_added", "now reads"), ("reads_removed", "no longer reads"),
    ):
        if delta.get(key):
            parts.append(f"{text} {', '.join(map(str, delta[key]))}")
    if delta.get("columns_reordered"):
        parts.append("output column order changed")
    return "; ".join(parts)


def load_snapshot(root: Path, source_schema=None) -> tuple[Pipeline, Path | None]:
    """Load a project folder or compiled graph file; also return the raw-text root."""

    if root.is_file():
        return load_compiled_graph(root, source_schema=source_schema), None
    return load_sqlx_project(root, source_schema=source_schema), root


def build_change_report(
    base: Pipeline,
    head: Pipeline,
    *,
    base_root: Path | None = None,
    head_root: Path | None = None,
    costs: Mapping[str, Mapping[str, object]] | None = None,
    title: str = "Change report",
    base_label: str = "base",
    head_label: str = "head",
    generated_at: str | None = None,
    scope: Scope | None = None,
    owned: tuple | None = None,
    overlaps: bool = True,
    explain_differences: bool = False,
) -> dict[str, object]:
    """Compare two pipelines and return the documented ``report`` payload.

    ``costs`` maps a model's ``schema.name`` to ``{basis, before?, after?}``.
    Models are matched by target key, so a rename shows as a removal plus an
    addition. Per-model failures become diagnostics, not a failed report.

    Each added or modified model also carries ``overlaps``: the existing tables
    in ``scope`` that already provide the same attributes, with match kind,
    checks and role, plus a coverage summary (see ``overlap_report``). It is
    advisory context; a comparison that fails is listed as unavailable inside
    that model's entry and changes nothing else. ``overlaps=False`` omits it.

    ``explain_differences=True`` (off by default; it runs a search per unproven model) adds ``except_when``
    (``{sql, atoms, tables, exact}``) to a modified query model whose rewrite is unproven when a verified
    predicate P exists such that the two versions are equivalent except on rows where P holds.
    """

    costs = costs or {}
    diagnostics: list[dict[str, str]] = []
    changes: list[dict[str, object]] = []
    cache: dict[str, _Consumers] = {}
    contracts: dict[str, _Contracts] = {}
    compare: list[tuple[dict[str, object], str]] = []

    def consumers(side: str) -> _Consumers:
        if side not in cache:
            cache[side] = _Consumers(head if side == "head" else base)
        return cache[side]

    def contract(side: str, pipeline: Pipeline, key: str) -> dict[str, object]:
        if side not in contracts:
            contracts[side] = _Contracts(pipeline)
        return contracts[side].of(key)

    for key in sorted(set(base.models) | set(head.models)):
        old, new = base.models.get(key), head.models.get(key)
        name = _display(new or old)
        kind = "modified"
        delta: dict[str, object] = {}
        try:
            if old and new:
                before, after = _raw_text(base_root, old), _raw_text(head_root, new)
                # Identical text is not identical output: a changed source schema reshapes a SELECT *, and
                # so its lineage, with no change to any file. Compare what each snapshot resolves the model to.
                delta = contract_delta(contract("base", base, key), contract("head", head, key))
                definition = _definition_changes(old, new)
                if before == after and not delta and not definition:
                    continue
                if before == after:
                    verification = {
                        "label": "unproven",
                        "reason": _unchanged_text_reason(delta, definition),
                        "checks": [],
                    }
                elif old.is_query and new.is_query:
                    v = verify_rewrite(before, after)
                    verification = {
                        "label": v.status.value,
                        "reason": v.reason,
                        "checks": [c.to_json() for c in v.checks],
                    }
                    if definition and v.status.value == "proven":
                        verification["label"] = "unproven"
                        verification["reason"] = (
                            f"query rewrite proven, but {', '.join(definition)} changed; the effect is not verified"
                        )
                else:
                    verification = {"label": "unproven", "reason": "not analyzed: not a query model", "checks": []}
                if delta and before != after and verification["label"] in ("proven", "planner_checked"):
                    # The proof compares the two texts over one schema; here the schema the texts resolve against moved too.
                    verification = {
                        **verification, "label": "unproven",
                        "reason": "resolved output changed between the snapshots: " + _delta_reason(delta),
                    }
                readers = consumers("head").of(key)
            elif new:
                kind = "added"
                verification = {"label": "unproven", "reason": "new model; no baseline to compare", "checks": []}
                readers = consumers("head").of(key)
            else:
                kind = "removed"
                verification = {"label": "unproven", "reason": "model removed; its readers may break", "checks": []}
                readers = consumers("base").of(key)
            cost = normalize_cost(costs.get(name))
        except Exception as exc:  # one model must not sink the report
            diagnostics.append({"asset": name, "message": f"{type(exc).__name__}: {exc}"})
            verification = {"label": "failed", "reason": "analysis of this model failed", "checks": []}
            readers = {"models": [], "complete": False}
            cost = {"basis": "unavailable"}
            delta = {}
        change = {"model": name, "kind": kind, "verification": verification, "cost": cost, "consumers": readers}
        if delta:
            change["contract"] = delta
        if explain_differences and kind == "modified" and verification["label"] == "unproven" and old.is_query and new.is_query and before != after:
            from kumo_sql_prover.difference_surface import except_when

            found = except_when(before, after)
            if found is not None:
                change["except_when"] = found
        if owned is not None:  # (base, head) catalogs.owned(): a removed model is owned by what owned it before
            change["owned"] = key in owned[0 if kind == "removed" else 1]
        changes.append(change)
        if kind in ("added", "modified"):
            compare.append((change, key))

    if overlaps:
        _attach_overlaps(compare, base, head, scope)

    for side, pipe in (("base", base), ("head", head)):
        for d in pipe.all_diagnostics():
            if d.code in _PARSE_CODES:
                diagnostics.append({"asset": d.model, "message": f"{side}: {d.message}"})
    return {
        "title": title,
        "base": base_label,
        "head": head_label,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "changes": changes,
        "diagnostics": diagnostics,
    }


def _attach_overlaps(
    compare: list[tuple[dict[str, object], str]], base: Pipeline, head: Pipeline, scope: Scope | None
) -> None:
    """Add the "already done elsewhere" section to each added or modified change.

    Nothing here raises: a failed comparison becomes an ``unavailable`` section
    on that change alone.
    """

    if not compare:
        return
    try:
        changed = {key: str(change["kind"]) for change, key in compare}
        removed = set(base.models) - set(head.models)
        # What a changed model computes is already profiled in head; profiling its SQL again against base re-analyses its upstream per model.
        head_profiles = profile_pipeline(head) if removed else {}
        checker = OverlapChecker(head, scope=scope, name_of=lambda k: _display(head.models[k]) if k in head.models else k)
        base_checker = None
        if removed:
            base_checker = OverlapChecker(
                base, scope=scope, name_of=lambda k: _display(base.models[k]) if k in base.models else k
            )
    except Exception as exc:  # noqa: BLE001
        for change, _ in compare:
            change["overlaps"] = unavailable_section(f"compare_error: {type(exc).__name__}")
        return
    for index, (change, key) in enumerate(compare):
        if index >= MAX_COMPARED_MODELS:
            change["overlaps"] = unavailable_section(
                f"limit_exceeded: only the first {MAX_COMPARED_MODELS} changed models are compared"
            )
            continue
        section = checker.section(key, changed=changed)
        if base_checker is not None and section.get("status") == "ok":
            try:
                extra = base_checker.retired_matches(head.models[key].sql, removed, head_profiles.get(key))
                section = mark_retiring(section, extra, "retired in this change")
            except Exception:  # noqa: BLE001
                pass
        change["overlaps"] = section


def change_report_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report behavior, cost and consumer changes between two project snapshots"
    )
    parser.add_argument("base", type=Path, help="Base project root or compiled graph JSON")
    parser.add_argument("head", type=Path, help="Head project root or compiled graph JSON")
    parser.add_argument(
        "--cost", type=Path,
        help='JSON mapping "schema.name" to {"basis", "before", "after"}; omitted means unknown',
    )
    parser.add_argument(
        "--base-source-schema", type=Path, metavar="FILE",
        help="Source schema JSON for the base snapshot, as for pipeline-report (changes under SELECT * are reported)",
    )
    parser.add_argument("--head-source-schema", type=Path, metavar="FILE", help="Source schema JSON for the head snapshot")
    parser.add_argument("--scope", metavar="NAME", help="Saved scope that limits which existing tables are compared")
    parser.add_argument("--no-overlaps", action="store_true", help="Skip the already-done-elsewhere section")
    parser.add_argument(
        "--explain-differences", action="store_true",
        help='For each unproven query rewrite, add the verified predicate P when the versions are "equivalent except when P"',
    )
    parser.add_argument("--title", default="Change report")
    parser.add_argument("-o", "--output", type=Path, help="Write JSON here; stdout if omitted")
    args = parser.parse_args(argv)
    try:
        costs = json.loads(args.cost.read_text(encoding="utf-8")) if args.cost else {}
        for entry in costs.values():
            normalize_cost(entry)
        scope = None
        if args.scope:
            scope = get_scope(args.scope)
            if scope is None:
                raise ValueError(f"no saved scope named {args.scope!r}")
        schemas = []
        for path in (args.base_source_schema, args.head_source_schema):
            schema = json.loads(path.read_text(encoding="utf-8")) if path else None
            if schema is not None and not isinstance(schema, dict):
                raise ValueError("source schema file must be a JSON object")
            schemas.append(schema)
        base, base_root = load_snapshot(args.base, schemas[0])
        head, head_root = load_snapshot(args.head, schemas[1])
    except (OSError, ValueError, AttributeError) as exc:
        parser.error(str(exc))
    report = build_change_report(
        base, head, base_root=base_root, head_root=head_root, costs=costs,
        title=args.title, base_label=str(args.base), head_label=str(args.head),
        scope=scope, overlaps=not args.no_overlaps,
        explain_differences=args.explain_differences,
    )
    text = json.dumps({"report": report}, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(change_report_main())
