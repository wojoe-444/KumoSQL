"""Blast radius of a proposed change: who is affected, and who cannot be checked.

``assess_change`` answers "what breaks if I change this?" for one column or
table. It is a read-only walk over what the pipeline analysis already knows:
the models that read the target (from column consumption, so a column used
only in a filter, join or grouping is found too) and the columns computed from
it (lineage). Readers the analysis cannot read are never dropped from the
answer: they are listed as ``unknown``. Readers outside the pipeline (jobs,
dashboards, scripts that are not parsed) are invisible here, so the result
never claims that a target is safe to remove.

Job history adds the readers no compiled model declares. Given observed reads,
tables that jobs read from the target (or from an affected model) but that no
declared model reads are listed under ``observed``, labeled with when they were
last seen and how confident the edge is. Job history names tables, not
columns, so their column use is unverified: the effect is ``may_break`` or
``may_change``, never ``breaks``. A reader already listed as affected or
unknown is not repeated.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .pipeline import Pipeline

CHANGE_KINDS = ("drop_column", "rename_column", "change_expression", "drop_table")
COLUMN_KINDS = ("drop_column", "rename_column", "change_expression")

SAFE_TO_DELETE_NOTE = (
    "Not claimed. Readers outside the analysed pipeline (query history, dashboards, "
    "unparsed operations) are not visible, so an empty result is not evidence of safety."
)

_READER_REASONS = (
    "parse_error", "qualify_error", "no_query", "insert_target_columns", "unexpanded_star", "unparsed_operation",
    "unresolved_template", "cycle",
)
_VIA_RANK = {"output_column": 0, "condition_only": 1, "model_dependency": 2}


@dataclass(frozen=True)
class AffectedModel:
    """One model that a change reaches.

    ``effect`` is ``breaks`` (the model stops working), ``values_change`` (an
    output column is computed from the changed expression),
    ``behavior_may_change`` (the column is only used in a filter, join,
    grouping or ordering, so rows may change) or ``indirect`` (it reads a model
    that breaks). ``via`` is ``output_column``, ``condition_only`` or
    ``model_dependency``. ``depth`` is 1 for direct readers.
    """

    model: str
    effect: str
    via: str
    depth: int
    columns: tuple[str, ...] = ()
    owned: bool = True


@dataclass(frozen=True)
class UnknownReader:
    """A model whose use of the target cannot be determined, with a reason code."""

    model: str
    reason: str
    owned: bool = True


@dataclass(frozen=True)
class ObservedReader:
    """A table seen reading the target (or an affected model) only in job history.

    ``effect`` is ``may_break`` or ``may_change``: history does not say which
    columns the job used. ``via`` is the table it was seen reading.
    ``confidence`` is that of the observed edge.
    """

    model: str
    effect: str
    via: str
    depth: int
    last_seen: str | None
    confidence: str
    observed_count: int
    source: str = "observed"
    owned: bool = True


@dataclass
class ChangeImpact:
    kind: str
    target: str
    target_known: bool
    affected: list[AffectedModel] = field(default_factory=list)
    unknown: list[UnknownReader] = field(default_factory=list)
    observed: list[ObservedReader] = field(default_factory=list)
    observed_checked: bool = False
    terminal: bool = False
    complete: bool = True
    incomplete_reasons: list[str] = field(default_factory=list)
    scope: str | None = None
    out_of_scope: int = 0
    catalogs: list[str] = field(default_factory=list)
    not_owned: int = 0
    safe_to_delete: str = "unknown"
    safe_to_delete_note: str = SAFE_TO_DELETE_NOTE

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "target_known": self.target_known,
            "affected": [
                {"model": a.model, "effect": a.effect, "via": a.via, "depth": a.depth, "columns": list(a.columns),
                 "owned": a.owned}
                for a in self.affected
            ],
            "unknown": [{"model": u.model, "reason": u.reason, "owned": u.owned} for u in self.unknown],
            "observed": [
                {"model": o.model, "effect": o.effect, "via": o.via, "depth": o.depth,
                 "last_seen": o.last_seen, "confidence": o.confidence,
                 "observed_count": o.observed_count, "source": o.source, "owned": o.owned}
                for o in self.observed
            ],
            "observed_checked": self.observed_checked,
            "terminal": self.terminal,
            "complete": self.complete,
            "incomplete_reasons": self.incomplete_reasons,
            "scope": self.scope,
            "out_of_scope": self.out_of_scope,
            "catalogs": self.catalogs,
            "not_owned": self.not_owned,
            "safe_to_delete": self.safe_to_delete,
            "safe_to_delete_note": self.safe_to_delete_note,
        }


def assess_change(
    pipeline: "Pipeline",
    kind: str,
    target: str,
    column: str | None = None,
    *,
    scope=None,
    observed_reads: Iterable[object] = (),
    owned=None,
) -> ChangeImpact:
    """Blast radius of ``kind`` applied to ``target`` (a table) and ``column``.

    A drop or rename reaches every model that reads the column anywhere, then
    everything that depends on those models. A changed expression reaches the
    columns computed from it, transitively, and models that filter or join on
    them. Without transform kinds for a changed expression, every descendant is
    treated as affected, which over-approximates. Column names match
    case-insensitively. With a ``scope``, only in-scope models are listed and
    the rest are counted in ``out_of_scope``. ``observed_reads`` are job-history
    records (see ``build_query_graph``); readers seen only there are added under
    ``observed``. ``owned`` (:func:`kumosql.catalogs.owned`) says which readers the
    chosen catalogs own: every reader stays listed, flagged ``owned: false`` when
    it is outside them, and ``not_owned`` counts those. Without it every reader is owned.
    """

    if kind not in CHANGE_KINDS:
        raise ValueError(f"unknown change kind {kind!r}; expected one of {', '.join(CHANGE_KINDS)}")
    if kind in COLUMN_KINDS and not column:
        raise ValueError(f"{kind} needs a column")
    if kind == "drop_table":
        column = None

    a = pipeline._analyse()
    table = pipeline.resolve(target) or target
    label = f"{table}.{column}" if column else table
    path: tuple[str, ...] = ()
    if column and "." in column:
        # ``widget.asset.id``: the field of a STRUCT column. A column whose own stored name has a dot (a flattened
        # schema) stays whole unless its first part is also a column.
        known_names = {c.lower() for c in a.outputs.get(table, ())} | {c.lower() for c in pipeline.source_schema.get(table, {})}
        first, *rest = column.split(".")
        if first.lower() in known_names or column.lower() not in known_names:
            column, path = first, tuple(rest)

    codes_by_model: dict[str, set[str]] = {}
    for diagnostic in a.diagnostics:
        codes_by_model.setdefault(diagnostic.model, set()).add(diagnostic.code)

    downstream = pipeline.downstream
    readers_index: dict[str, set[str]] = {}
    for model, refs in a.consumed.items():
        for ref in refs:
            readers_index.setdefault(ref.table, set()).add(model)

    # Tables named in a query also count, so a SELECT * over a table is found
    # even though it consumes no named column.
    from kumo_sql_prover.ast_utils import binding_cte, is_function_table
    from .pipeline import ColumnRef, _table_name_for_schema, reverse_hops, reverse_index
    from .pipeline_types import path_overlap
    from sqlglot import exp

    for model, query in a.parsed.items():
        for node in query.find_all(exp.Table):
            if node.name and not is_function_table(node) and binding_cte(node) is None:
                name = pipeline.resolve(node) or _table_name_for_schema(node)
                if name != model:
                    readers_index.setdefault(name, set()).add(model)

    def readers_of(name: str) -> set[str]:
        return set(downstream.get(name, ())) | readers_index.get(name, set())

    def reader_problem(model: str) -> str | None:
        codes = codes_by_model.get(model, set())
        if "unresolved_template" in codes:
            return "unresolved_template"  # it reads a table named by a template, which might be the target
        if "cycle" in codes and not a.consumed.get(model):
            return "cycle"  # a cycle member analysed before its input's columns were known cannot say which it reads
        if model in a.consumed and "*" not in a.outputs.get(model, ()):
            return None
        return next((c for c in _READER_REASONS if c in codes), "unparsed_model")

    lineage_index = reverse_index(a.reverse_lineage)

    def children(name: str, col: str, fields: tuple[str, ...]) -> set["ColumnRef"]:
        """Columns built from ``col`` (or from its field ``fields``): the same field, one inside it, or the whole column."""

        return reverse_hops(ColumnRef(name, col, fields), a.reverse_lineage, lineage_index, a.records)

    if column and table in a.outputs and "*" not in a.outputs[table]:
        target_known = column.lower() in {c.lower() for c in a.outputs[table]}
    else:
        target_known = table in pipeline.models or table in readers_index or table in pipeline.source_schema
    result = ChangeImpact(kind=kind, target=label, target_known=target_known)

    affected: dict[str, AffectedModel] = {}
    unknown: dict[str, str] = {}
    # Models whose rows (not only some columns' values) may change: everything downstream of them may change.
    rows_changed: dict[str, int] = {}

    def deciding(reader: str, refs: list) -> bool:
        conditions = a.conditions.get(reader)
        if conditions is None:
            return True
        keys = {(c.table, c.column.lower()) for c in conditions}
        return any((r.table, r.column.lower()) in keys for r in refs)

    def note(model: str, effect: str, via: str, depth: int, cols: tuple[str, ...]) -> None:
        old = affected.get(model)
        if old is None:
            affected[model] = AffectedModel(model, effect, via, depth, cols)
            return
        better = _VIA_RANK[old.via] <= _VIA_RANK[via]
        affected[model] = AffectedModel(
            model,
            old.effect if better else effect,
            old.via if better else via,
            min(old.depth, depth),
            tuple(sorted(set(old.columns) | set(cols))),
        )

    def visit(name: str, cols: list[str] | None, depth: int, fields: tuple[str, ...] = ()) -> list[tuple[str, str, tuple[str, ...]]]:
        """Record readers of ``name`` (of the given columns, or of the whole table).

        Returns the columns of those readers that are computed from the target. ``fields`` narrows a column to a struct
        field: a reader of that field, of a field inside it or of the whole column is reached, a reader of a sibling
        field is not.
        """

        reached: list[tuple[str, str, tuple[str, ...]]] = []
        wanted = None if cols is None else {c.lower() for c in cols}
        for reader in sorted(readers_of(name)):
            problem = reader_problem(reader)
            if problem:
                unknown.setdefault(reader, problem)
                continue
            if wanted is None:
                note(reader, "breaks", "model_dependency", depth, ())
                continue
            if name in a.star_branch_tables.get(reader, ()):
                unknown.setdefault(reader, "unexpanded_star")  # a ``SELECT *`` branch may read the column
                continue
            refs = [
                r for r in a.consumed[reader]
                if r.table == name and r.column.lower() in wanted and path_overlap(r.path, fields)
            ]
            if not refs:
                if name in a.template_reads.get(reader, ()):
                    unknown.setdefault(reader, "template_columns")  # a template expression may read it
                elif name in a.script_reads.get(reader, {}):
                    words = a.script_reads[reader][name]
                    if words is None or wanted & words:
                        unknown.setdefault(reader, "script_columns")  # another statement of the script may read it
                continue
            feeds = {c for r in refs for c in children(name, r.column, fields) if c.table == reader}
            if kind == "change_expression":
                effect = "values_change" if feeds else "behavior_may_change"
                if not feeds or deciding(reader, refs):
                    rows_changed[reader] = min(depth, rows_changed.get(reader, depth))
            else:
                effect = "breaks"
            note(reader, effect, "output_column" if feeds else "condition_only", depth,
                 tuple(sorted({r.column for r in refs})))
            reached.extend((c.table, c.column, c.path) for c in feeds)
        return reached

    # A wildcard query (``FROM `d.events_*` ``) reads every table its pattern matches.
    names = [table, *sorted(n for n in readers_index if "*" in n and table in pipeline.wildcard_members(n))]
    if kind == "drop_table":
        for name in names:
            visit(name, None, 1)
    elif kind == "change_expression":
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        queue = deque([(name, column, path, 1) for name in names])
        while queue:
            name, col, fields, depth = queue.popleft()
            if (name, col.lower(), fields) in seen:
                continue
            seen.add((name, col.lower(), fields))
            for next_table, next_column, next_fields in visit(name, [col], depth, fields):
                queue.append((next_table, next_column, next_fields, depth + 1))
        # A model whose rows may change (the column filters, joins, groups or decides a subquery) can change
        # everything downstream of it, whichever of its columns they read.
        pending_rows = deque(sorted(rows_changed.items(), key=lambda item: (item[1], item[0])))
        while pending_rows:
            model, depth = pending_rows.popleft()
            for reader in sorted(downstream.get(model, ())):
                if reader in rows_changed or reader == model:
                    continue
                rows_changed[reader] = depth + 1
                problem = reader_problem(reader)
                if problem:
                    unknown.setdefault(reader, problem)
                    continue
                note(reader, "behavior_may_change", "model_dependency", depth + 1, ())
                pending_rows.append((reader, depth + 1))
    else:
        for name in names:
            visit(name, [column], 1, path)

    if kind != "change_expression":
        # Whatever reads a model that breaks is affected too.
        pending = deque((m.model, m.depth) for m in affected.values())
        seen_models = set(affected)
        while pending:
            model, depth = pending.popleft()
            for reader in sorted(downstream.get(model, ())):
                if reader not in seen_models:
                    seen_models.add(reader)
                    note(reader, "indirect", "model_dependency", depth + 1, ())
                    pending.append((reader, depth + 1))

    # Anything downstream of a reader that could not be read is unknown too.
    pending_unknown = deque(unknown)
    while pending_unknown:
        model = pending_unknown.popleft()
        for reader in sorted(downstream.get(model, ())):
            if reader not in unknown and reader not in affected:
                unknown[reader] = "downstream_of_unknown_reader"
                pending_unknown.append(reader)
    # A model with unknown reads may read the target.
    for model, codes in sorted(codes_by_model.items()):
        if "unknown_reads" in codes and model not in affected:
            unknown.setdefault(model, "unknown_reads")
        elif "unresolved_template" in codes and model not in affected:
            unknown.setdefault(model, "unresolved_template")  # the table it reads is named by a template, so it may be the target

    observed_reads = list(observed_reads)
    observed_found: dict[str, ObservedReader] = {}
    if observed_reads:
        result.observed_checked = True
        from .graph import build_query_graph

        only_observed: dict[str, list] = {}
        for edge in build_query_graph(pipeline, observed_reads).edges:
            if edge.observed and not (edge.declared or edge.parsed):
                only_observed.setdefault(edge.upstream.key, []).append(edge)
        effect = "may_change" if kind == "change_expression" else "may_break"
        queue = deque([(table, 0)] + [(m.model, m.depth) for m in affected.values()])
        visited: set[str] = set()
        while queue:
            name, depth = queue.popleft()
            if name in visited:
                continue
            visited.add(name)
            for edge in sorted(only_observed.get(name, ()), key=lambda e: e.downstream.key):
                reader = edge.downstream.key
                if reader == table or reader in affected or reader in unknown:
                    continue
                found = ObservedReader(reader, effect, name, depth + 1, edge.last_seen,
                                       edge.confidence, edge.observed_count)
                old = observed_found.get(reader)
                if old is None or found.depth < old.depth or (
                    found.depth == old.depth and (found.last_seen or "") > (old.last_seen or "")
                ):
                    observed_found[reader] = found
                queue.append((reader, depth + 1))

    result.terminal = table in pipeline.models and not downstream.get(table)
    keep = pipeline.scope_keys(scope) if scope is not None else None
    if scope is not None:
        result.scope = scope.name

    def in_scope(model: str) -> bool:
        return keep is None or model in keep

    def is_owned(model: str) -> bool:
        return owned is None or model in owned

    result.affected = sorted(
        (replace(m, owned=is_owned(m.model)) for m in affected.values() if in_scope(m.model)),
        key=lambda m: (m.depth, m.model),
    )
    result.unknown = [UnknownReader(m, r, is_owned(m)) for m, r in sorted(unknown.items()) if in_scope(m)]
    result.observed = sorted(
        (replace(o, owned=is_owned(o.model)) for o in observed_found.values() if in_scope(o.model)),
        key=lambda o: (o.depth, o.model),
    )
    if owned is not None:
        result.catalogs = list(getattr(owned, "catalogs", ()))
        result.not_owned = sum(1 for m in {*affected, *unknown, *observed_found} if in_scope(m) and not is_owned(m))
    result.out_of_scope = sum(1 for m in {*affected, *unknown, *observed_found} if not in_scope(m))

    reasons = []
    if not target_known:
        reasons.append("target_not_found")
    if unknown:
        reasons.append("unknown_readers")
    if not pipeline.completeness()["views"].get("impact", True):
        reasons.append("analysis_gaps")
    if result.terminal:
        reasons.append("terminal_output_may_be_read_outside_pipeline")
    result.incomplete_reasons = reasons
    result.complete = not reasons
    return result
