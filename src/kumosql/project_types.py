"""Column types for every table of a project: from a schema snapshot, from declarations and carried through the models.

A Dataform project stores no column types, so a query written against one has nothing to compare. This module gives
each table of the project (``Session.column_types()``) a type per column:

1. a **source** (a declared table) takes the types of the schema snapshot (:mod:`kumosql.schema_snapshot`), else the
   types its declaration gives (``--source-schema``);
2. a **model** takes the snapshot's types when the snapshot lists the table (the deployed table is what a query runs
   against), and otherwise the types :mod:`kumosql.googlesql_types` infers from the model's own query, reading the
   tables above it. Models are typed in dependency order, once, and a model's types feed the models that read it;
3. a column whose type cannot be fixed gets ``None``, for that column only, with a reason (:meth:`ProjectTypes.reason`).

Snapshot tables are joined to project tables by their names, case-insensitively: the full ``project.dataset.table``,
or the trailing parts when either side lacks the project. A name that fits several tables (or none) is never guessed;
it is listed in ``unmatched`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .schema_snapshot import SchemaSnapshot

if TYPE_CHECKING:
    from .googlesql_types import GType
    from .mcp_session import Session

#: model kinds whose query result is the table (an assertion or an operations file has no column types to carry)
_TYPED_KINDS = frozenset({"table", "view", "incremental", "unknown"})


@dataclass
class ProjectTypes:
    """``types[key][column.lower()]`` is the :class:`GType` of a column, or ``None`` when it is not known."""

    types: dict[str, dict[str, "GType | None"]] = field(default_factory=dict)
    #: column names as spelled, in order, for tables whose column list the snapshot gave
    snapshot_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: where each table's types came from: ``snapshot``, ``declared`` or ``derived``
    origin: dict[str, str] = field(default_factory=dict)
    #: snapshot tables that match no table of the project (or several), as written in the snapshot
    unmatched: list[str] = field(default_factory=list)
    #: whether a snapshot was given (and could be read)
    has_snapshot: bool = False
    snapshot_problem: str | None = None
    #: snapshot tables by project key, for tables outside the project's own definitions
    snapshot: SchemaSnapshot | None = None

    def type(self, key: str, column: str) -> "GType | None":
        return self.types.get(key, {}).get(column.casefold())

    def columns(self, key: str) -> list[str]:
        """The columns this module knows of ``key`` (snapshot spelling), or ``[]``."""

        return list(self.snapshot_columns.get(key, ()))

    def known(self, key: str, column: str) -> bool:
        return self.type(key, column) is not None

    def reason(self, key: str, column: str, is_model: bool) -> str:
        """Why the type of ``key.column`` is unknown, in one phrase that names the way to supply it."""

        hint = "give --schema-snapshot FILE"
        if self.snapshot_problem:
            return f"the schema snapshot could not be read ({self.snapshot_problem})"
        if not self.has_snapshot:
            return ("no schema snapshot was given and the model's query does not fix it; " if is_model else "the project declares no type for it; ") + hint
        if self.origin.get(key) == "snapshot":
            return "the schema snapshot lists the table but not this column; refresh the snapshot"
        return ("the schema snapshot has no rows for the table and the model's query does not fix it" if is_model
                else "the schema snapshot has no rows for the table") + "; refresh the snapshot if it should"


def build(session: "Session") -> ProjectTypes:
    """Type every table of the loaded project (see the module docstring); never raises for a model it cannot type."""

    from .googlesql_types import Catalog, Column, infer, parse_type

    pipeline = session.pipeline()
    result = ProjectTypes()
    snapshot = session.schema_snapshot()
    result.snapshot_problem = session.snapshot_error
    result.has_snapshot = snapshot is not None
    result.snapshot = snapshot
    matched = _match(snapshot, list(session.tables()), result) if snapshot is not None else {}
    catalog = Catalog()

    def record(key: str, origin: str, columns: list[tuple[str, "GType | None"]]) -> None:
        by_name: dict[str, "GType | None"] = {}
        for name, kind in columns:
            folded = name.casefold()
            by_name[folded] = None if folded in by_name else kind  # a name twice cannot be told apart
        result.types[key] = by_name
        result.origin[key] = origin
        catalog.add(key, [Column(name, by_name[name.casefold()]) for name, _ in columns])

    def from_snapshot(key: str) -> bool:
        rows = matched.get(key)
        if rows is None:
            return False
        record(key, "snapshot", [(c.column, parse_type(c.data_type)) for c in rows])
        result.snapshot_columns[key] = tuple(c.column for c in rows)
        return True

    for key in pipeline.sources:
        if key in pipeline.models or from_snapshot(key):
            continue
        declared = pipeline.source_schema.get(key) or {}
        if declared:
            record(key, "declared", [(name, parse_type(str(kind)) if kind else None) for name, kind in declared.items()])
    for key in pipeline.topological_order():
        model = pipeline.models.get(key)
        if model is None or from_snapshot(key):
            continue
        if model.kind not in _TYPED_KINDS or not model.sql.strip():
            continue
        try:
            typed = infer(model.sql, catalog)
        except Exception:  # noqa: BLE001 - a model the typer cannot read stays untyped; the rest is unaffected
            continue
        if typed.columns is None:
            continue
        named = [(c.name, c.type) for c in typed.columns if c.name]
        if named:
            record(key, "derived", named)
    return result


def _match(snapshot: SchemaSnapshot, keys: list[str], result: ProjectTypes) -> dict:
    """``{project key: snapshot columns}`` joined by name; ambiguous or unknown snapshot tables go to ``result.unmatched``."""

    parts = {key: tuple(key.casefold().split(".")) for key in keys}
    by_tail: dict[str, list[str]] = {}
    for key, key_parts in parts.items():
        by_tail.setdefault(key_parts[-1], []).append(key)
    found: dict[str, tuple] = {}
    for name, columns in snapshot.tables().items():
        wanted = tuple(name.split("."))
        owners = []
        for key in by_tail.get(wanted[-1], ()):
            depth = min(len(wanted), len(parts[key]))
            if parts[key][-depth:] == wanted[-depth:]:
                owners.append(key)
        if len(owners) == 1 and owners[0] not in found:
            found[owners[0]] = columns
        else:
            result.unmatched.append(columns[0].table_name)
    return found
