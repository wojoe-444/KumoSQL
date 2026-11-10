"""What a schema change does to every model downstream: which break, which change their output schema.

``assess_schema_change`` answers "what if this table gains, loses, renames or retypes a column?" by running the
change through the pipeline: each model that reads the table, directly or through other models, is re-resolved
against the changed schema, in dependency order, and compared with how it resolves today.

* ``breaks``: the model resolved before and does not now (a column it names is gone, a bare column became
  ambiguous, the output has two columns of the same name, a ``UNION`` arm changed width).
* ``output_changes``: the model still works but its output columns differ in name, order or type. This is where
  ``SELECT *`` matters: a star passes an added, removed, renamed or retyped column on to its readers.
* ``unknown``: the model cannot be re-resolved (it did not parse, a table it reads has no known columns, a table is
  named by an unresolved template) and so is anything that reads it. An unknown is never reported as safe.

A model that breaks is assumed to be fixed with the same output it has today, so models after it are judged on their
own. Types come from ``source_schema`` and sqlglot's type inference; a type change is reported where it reaches an
output column, and is not reported as breaking (an incompatible operation on the new type is not detected).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlglot import exp
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

from kumo_sql_prover.ast_utils import binding_cte, star_modifier

if TYPE_CHECKING:
    from .pipeline import Pipeline

SCHEMA_CHANGE_KINDS = ("add_column", "drop_column", "rename_column", "retype_column")

Columns = tuple[tuple[str, str], ...]  # (name, type) in output order


@dataclass(frozen=True)
class ModelEffect:
    model: str
    effect: str  # breaks | output_changes | unknown
    reason: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    retyped: tuple[str, ...] = ()


@dataclass
class SchemaChange:
    kind: str
    target: str
    column: str
    breaks: list[ModelEffect] = field(default_factory=list)
    output_changes: list[ModelEffect] = field(default_factory=list)
    unknown: list[ModelEffect] = field(default_factory=list)
    unaffected: int = 0

    @property
    def complete(self) -> bool:
        return not self.unknown

    def to_json(self) -> dict:
        def row(e: ModelEffect) -> dict:
            return {
                "model": e.model, "effect": e.effect, "reason": e.reason,
                "added": list(e.added), "removed": list(e.removed), "retyped": list(e.retyped),
            }

        return {
            "kind": self.kind, "target": self.target, "column": self.column,
            "breaks": [row(e) for e in self.breaks],
            "output_changes": [row(e) for e in self.output_changes],
            "unknown": [row(e) for e in self.unknown],
            "unaffected": self.unaffected,
            "complete": self.complete,
        }


class _Unresolvable(Exception):
    """The model cannot be resolved against the given schemas (the message is a short reason code)."""


def _canonical(table: exp.Table, pipeline: "Pipeline") -> exp.Table | None:
    key = pipeline.resolve(table)
    if not key:
        return None
    parts = key.split(".")
    return exp.table_(parts[-1], db=parts[-2] if len(parts) > 1 else None, catalog=parts[-3] if len(parts) > 2 else None)


def _resolve(pipeline: "Pipeline", key: str, tables: dict[str, Columns | None]) -> Columns:
    """Output columns of ``key``'s query with its tables' columns as given in ``tables``.

    Raises ``exp.errors.OptimizeError`` (a break) or ``_Unresolvable`` (cannot be judged).
    """

    analysis = pipeline._analyse()
    query = analysis.parsed.get(key)
    if query is None:
        raise _Unresolvable("unparsed_model")
    query = query.copy()
    schema: dict = {}
    for table in list(query.find_all(exp.Table)):
        if binding_cte(table) is not None:
            continue  # a WITH table in scope here; a nested ``WITH t`` does not hide a read of the table t elsewhere
        canonical = _canonical(table, pipeline)
        if canonical is None:
            raise _Unresolvable("unknown_table")
        name = ".".join(part.name for part in canonical.parts)
        columns = tables.get(name)
        if columns is None:
            raise _Unresolvable("unknown_columns")
        alias = table.args.get("alias")
        if alias is not None:
            canonical.set("alias", alias.copy())
        table.replace(canonical)
        node = schema
        for part in [p.name for p in canonical.parts][:-1]:
            node = node.setdefault(part, {})
        node[canonical.name] = {col: typ for col, typ in columns}
    for star in query.find_all(exp.Star):
        named = [c.name.lower() for c in (star_modifier(star, "except") or [])]
        named += [r.alias_or_name.lower() for r in (star_modifier(star, "replace") or [])]
        if not named:
            continue
        scope_select = star.find_ancestor(exp.Select)
        available: set[str] = set()
        for table in scope_select.find_all(exp.Table) if scope_select else ():
            if table.find_ancestor(exp.Select) is not scope_select:
                continue
            parts = [p.name for p in table.parts]
            node = schema
            for part in parts:
                node = node.get(part, {}) if isinstance(node, dict) else {}
            available |= {c.lower() for c in node}
        if scope_select is not None and available and any(n not in available for n in named):
            raise exp.errors.OptimizeError("column in EXCEPT or REPLACE could not be resolved")
    sqlglot_schema = MappingSchema(schema, dialect="bigquery")
    qualified = qualify(
        query, schema=sqlglot_schema, dialect="bigquery", validate_qualify_columns=True, quote_identifiers=False
    )
    for node in qualified.find_all(exp.Union, exp.Intersect, exp.Except):
        widths = {len(arm.selects) for arm in (node.this, node.expression) if hasattr(arm, "selects")}
        if len(widths) > 1:
            raise exp.errors.OptimizeError("set operation arms have different column counts")
    names = [name for name in qualified.named_selects]
    if len({name.lower() for name in names}) != len(names):
        raise exp.errors.OptimizeError("duplicate column names in the output")
    try:
        typed = annotate_types(qualified, schema=sqlglot_schema, dialect="bigquery")
    except Exception:  # noqa: BLE001 - types are best effort; names are what break
        typed = qualified
    return tuple(
        (select.alias_or_name, select.type.sql("bigquery") if getattr(select, "type", None) else "UNKNOWN")
        for select in typed.selects
    )


def _diff(old: Columns, new: Columns) -> ModelEffect | None:
    if old == new:
        return None
    old_names, new_names = [n for n, _ in old], [n for n, _ in new]
    old_types, new_types = dict(old), dict(new)
    added = tuple(n for n in new_names if n not in old_types)
    removed = tuple(n for n in old_names if n not in new_types)
    retyped = tuple(n for n in new_names if n in old_types and old_types[n] != new_types[n])
    reason = "output_columns_changed" if added or removed else ("output_types_changed" if retyped else "output_order_changed")
    return ModelEffect("", "output_changes", reason, added, removed, retyped)


def assess_schema_change(
    pipeline: "Pipeline",
    kind: str,
    table: str,
    column: str,
    *,
    new_name: str | None = None,
    new_type: str | None = None,
) -> SchemaChange:
    """Run a schema change through the pipeline; see the module docstring."""

    if kind not in SCHEMA_CHANGE_KINDS:
        raise ValueError(f"unknown schema change {kind!r}; expected one of {', '.join(SCHEMA_CHANGE_KINDS)}")
    if kind == "rename_column" and not new_name:
        raise ValueError("rename_column needs new_name")
    if kind == "retype_column" and not new_type:
        raise ValueError("retype_column needs new_type")
    analysis = pipeline._analyse()
    key = pipeline.resolve(table) or table
    baseline_cache: dict[str, Columns | None] = {}

    def baseline(name: str) -> Columns | None:
        """Columns of a table today: a declared source, or a model resolved from its own parents."""

        if name in baseline_cache:
            return baseline_cache[name]
        baseline_cache[name] = None  # a cycle resolves to unknown
        result: Columns | None = None
        if name in pipeline.source_schema and name not in pipeline.models:
            result = tuple((c, t.upper()) for c, t in pipeline.source_schema[name].items())
        elif name in pipeline.models:
            try:
                result = _resolve(pipeline, name, _parent_columns(name, baseline))
            except Exception:  # noqa: BLE001 - unknown today means unknown after the change
                result = None
        baseline_cache[name] = result
        return result

    def _parent_columns(model: str, source) -> dict[str, Columns | None]:
        query = analysis.parsed.get(model)
        found: dict[str, Columns | None] = {}
        if query is None:
            return found
        for node in query.find_all(exp.Table):
            canonical = _canonical(node, pipeline)
            if canonical is not None:
                name = ".".join(part.name for part in canonical.parts)
                found[name] = source(name)
        return found

    current = baseline(key)
    changed: Columns | None = None
    if current is not None:
        names = [c for c, _ in current]
        match kind:
            case "add_column":
                if column in names:
                    raise ValueError(f"{key} already has a column {column}")
                changed = current + ((column, (new_type or "STRING").upper()),)
            case "drop_column":
                if column not in names:
                    raise ValueError(f"{key} has no column {column}")
                changed = tuple(item for item in current if item[0] != column)
            case "rename_column":
                if column not in names:
                    raise ValueError(f"{key} has no column {column}")
                changed = tuple((new_name if c == column else c, t) for c, t in current)
            case "retype_column":
                if column not in names:
                    raise ValueError(f"{key} has no column {column}")
                changed = tuple((c, new_type.upper() if c == column else t) for c, t in current)
    result = SchemaChange(kind, key, column)

    downstream = pipeline.downstream
    reach: set[str] = set()
    queue = deque([key])
    while queue:
        for child in downstream.get(queue.popleft(), ()):
            if child not in reach:
                reach.add(child)
                queue.append(child)
    order = [m for m in analysis.order if m in reach]
    order += sorted(m for m in reach if m not in set(order))

    after: dict[str, Columns | None] = {key: changed}
    unknown_models: dict[str, str] = {}
    for model in order:
        parents = {p for p in pipeline.upstream.get(model, ()) if p in after or p == key}
        if not parents & set(after):
            continue
        codes = {d.code for d in analysis.diagnostics if d.model == model}
        if "unresolved_template" in codes:
            unknown_models[model] = "unresolved_template"
            after[model] = None
            continue
        tables = _parent_columns(model, lambda name: after[name] if name in after else baseline(name))
        old = baseline(model)
        if any(columns is None for columns in tables.values()) or old is None or changed is None:
            unknown_models[model] = "unknown_columns" if changed is not None else "target_columns_unknown"
            after[model] = None
            continue
        try:
            new = _resolve(pipeline, model, tables)
        except _Unresolvable as exc:
            unknown_models[model] = str(exc)
            after[model] = None
            continue
        except Exception as exc:  # noqa: BLE001 - OptimizeError and friends: the changed schema does not resolve
            result.breaks.append(ModelEffect(model, "breaks", _reason(exc)))
            after[model] = old  # assume it is fixed with the output it has today
            continue
        effect = _diff(old, new)
        if effect is None:
            continue  # same output: nothing downstream of it sees a change
        result.output_changes.append(
            ModelEffect(model, "output_changes", effect.reason, effect.added, effect.removed, effect.retyped)
        )
        after[model] = new

    # A model that did not parse has no recorded reads, so it is unknown if its text names any table that changed.
    touched = {name.lower().rsplit(".", 1)[-1] for name in after}
    for model, spec in pipeline.models.items():
        if model in reach or model in unknown_models or analysis.parsed.get(model) is not None:
            continue
        text = (spec.sql or "").lower()
        if any(name in text for name in touched):
            unknown_models[model] = "unparsed_model"

    # Anything that reads a model we could not judge is unknown too, even if its own parents look fine.
    pending = deque(unknown_models)
    while pending:
        for child in sorted(downstream.get(pending.popleft(), ())):
            if child not in unknown_models and child not in {e.model for e in result.breaks}:
                unknown_models[child] = "downstream_of_unknown"
                pending.append(child)
    result.output_changes = [e for e in result.output_changes if e.model not in unknown_models]
    result.unknown = [ModelEffect(m, "unknown", r) for m, r in sorted(unknown_models.items())]
    result.breaks.sort(key=lambda e: e.model)
    result.output_changes.sort(key=lambda e: e.model)
    judged = {e.model for e in result.breaks} | {e.model for e in result.output_changes} | set(unknown_models)
    result.unaffected = len(reach - judged)
    return result


def _reason(exc: Exception) -> str:
    text = str(exc).lower()
    if "different column counts" in text:
        return "set_operation_width_changed"
    if "duplicate column" in text:
        return "duplicate_output_column"
    if "could not be resolved" in text or "unknown column" in text:
        return "column_missing_or_ambiguous"
    return "no_longer_resolves"
