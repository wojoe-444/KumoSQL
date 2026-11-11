"""Value types shared by the pipeline modules."""

from __future__ import annotations

from dataclasses import dataclass

from .identity import NodeIdentity
from .paths import posix_path


@dataclass(frozen=True, order=True)
class Target:
    """A BigQuery table identity; empty parts are unknown."""

    database: str = ""
    schema: str = ""
    name: str = ""

    @property
    def key(self) -> str:
        return ".".join(part for part in (self.database, self.schema, self.name) if part)

    def sql(self) -> str:
        return f"`{self.key}`"


@dataclass(frozen=True, order=True)
class ColumnRef:
    """A column of a table or model, optionally narrowed to a field inside a STRUCT column.

    ``column`` is always the root column the table stores. ``path`` names the struct fields read below it, outermost
    first: ``widget.asset.id`` is ``ColumnRef(t, "widget", ("asset", "id"))``. An empty path is the whole column, which is
    also the coarse answer when the field a query reads cannot be resolved.
    """

    table: str
    column: str
    path: tuple[str, ...] = ()

    @property
    def root(self) -> "ColumnRef":
        """The whole root column, with no field path."""

        return ColumnRef(self.table, self.column) if self.path else self

    @property
    def dotted(self) -> str:
        """The column with its field path, ``widget.asset.id``."""

        return ".".join((self.column, *self.path))

    def overlaps(self, other: "ColumnRef") -> bool:
        """Whether the two read the same stored value: same column, and one field path is a prefix of the other.

        ``a.b`` overlaps ``a.b.c`` and ``a`` as a whole, never ``a.d``. Names compare case-insensitively.
        """

        return (
            self.table == other.table
            and self.column.lower() == other.column.lower()
            and path_overlap(self.path, other.path)
        )

    def __str__(self) -> str:
        return f"{self.table}.{self.dotted}"


def path_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Whether one struct field path is a prefix of the other (an empty path is the whole column)."""

    shorter = min(len(left), len(right))
    return [part.lower() for part in left[:shorter]] == [part.lower() for part in right[:shorter]]


@dataclass(frozen=True)
class ColumnLineage:
    """How one model output column is built, one hop back.

    ``status`` is ``traced`` (built from ``sources``), ``constant`` (checked
    to read no column at all, such as a literal or ``COUNT(*)``) or
    ``unknown`` (could not be traced; ``reason`` says why and ``sources``
    holds only what was resolved before the trace gave up). ``transform`` is
    ``passthrough``, ``renamed``, ``expression``, ``aggregate``, ``window``,
    ``union``, ``constant`` or ``unknown``.
    """

    column: "ColumnRef"
    sources: frozenset["ColumnRef"]
    status: str
    transform: str
    reason: str | None = None
    # The conditions that limit which rows reach this column (``None``: could not be worked out).
    filters: tuple["ColumnFilter", ...] | None = None


@dataclass(frozen=True)
class ColumnFilter:
    """One condition that limits which rows reach an output column (see ``kumosql.lineage_filters``).

    ``kind`` is ``where``, ``join`` (an ``ON`` or ``USING``), ``having`` or ``qualify``. ``scope`` names the
    query it was written in: ``main``, a CTE, ``subquery <alias>``, with ``branch N`` for a set operation's
    branches. ``columns`` are the source columns it reads. ``effect`` says how it acts on the output rows:
    ``limits_rows``, ``matches_only`` (decides which rows pair up in an outer join, rows without a match
    stay), ``excludes_rows`` (inside the right side of ``EXCEPT``) or ``feeds_value`` (inside a scalar
    subquery that one column is computed with).
    """

    kind: str
    condition: str
    scope: str
    columns: tuple["ColumnRef", ...]
    effect: str = "limits_rows"
    columns_complete: bool = True

    def to_json(self) -> dict:
        row: dict = {
            "kind": self.kind,
            "condition": self.condition,
            "scope": self.scope,
            "effect": self.effect,
            "columns": [_column_row(ref) for ref in self.columns],
        }
        if not self.columns_complete:
            row["columns_complete"] = False
        return row


def _column_row(ref: "ColumnRef") -> dict:
    row = {"node": ref.table, "column": ref.column}
    if ref.path:
        row["field"] = ".".join(ref.path)
    return row


@dataclass(frozen=True)
class ColumnTrace:
    """Everything upstream of one column, with untraceable parts kept apart."""

    column: "ColumnRef"
    upstream: frozenset["ColumnRef"]
    # Columns the trace ended on that are not produced by any pipeline model.
    sources: frozenset["ColumnRef"]
    # Columns on the way whose own inputs could not be traced, with the reason.
    unknown: tuple[tuple["ColumnRef", str], ...]
    # The conditions that limit the rows reaching ``column``, from its own model and every model upstream of it,
    # each with the model it was written in. ``filters_unknown`` lists models whose conditions could not be worked out.
    filters: tuple[tuple[str, "ColumnFilter"], ...] = ()
    filters_unknown: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unknown


@dataclass
class Model:
    """One pipeline node: a table, view, incremental table, assertion or operation.

    ``kind`` is ``unknown`` when the config's ``type`` is computed (a project variable, a call): the node could be an
    incremental table, so nothing that depends on its stored rows is concluded from its query alone.
    """

    target: Target
    kind: str
    sql: str
    path: str | None = None
    declared_dependencies: tuple[Target, ...] = ()
    # Dataform expressions masked out of ``sql``; identifiers in them count as reads.
    masked_expressions: tuple[str, ...] = ()
    # Dataform tags from the config block; workflow configurations select actions by them.
    tags: tuple[str, ...] = ()
    # Dataform ``assertions`` of the config block: columns that are never NULL and
    # sets of columns that are unique. Declared by the project, checked when its assertions run.
    non_null: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()
    # Dataform ``pre_operations`` and ``post_operations`` statements, with refs resolved: scripts that run around the query.
    operations_sql: tuple[str, ...] = ()
    # How many leading entries of ``operations_sql`` run before the query; the rest run after it.
    pre_operations: int = 0
    # Lower-case words of the config expressions that read this table's own columns (built-in assertions, partitioning,
    # clustering, ``uniqueKey``, ``updatePartitionFilter``): an output column named here is used even when no model reads it.
    config_reads: tuple[str, ...] = ()
    # Config keys that read columns but whose value could not be read without running the project (a variable, a call).
    config_reads_unread: tuple[str, ...] = ()
    # database, schema and name as the config wrote them, before the project's prefix and suffix settings; empty when
    # no setting changed the target. A ``ref()`` names an action by these.
    logical: tuple[str, ...] = ()
    # ``disabled: true``: Dataform compiles the action (it stays in the graph) but does not run it.
    disabled: bool = False
    # An operations action that ``hasOutput``: it defines a table other actions can ``ref()``.
    has_output: bool = False
    # Compiled incremental tables: the query that runs after the first run, and its pre and post operations. The loaded
    # ``sql`` is the full-refresh query; these read tables and columns too.
    incremental_sql: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.path:
            self.path = posix_path(self.path)  # shown to agents and in diffs: forward slashes on every platform

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def scripts(self) -> tuple[str, ...]:
        """Every statement besides ``sql`` that reads tables: pre and post operations and the incremental branch."""

        return (*self.operations_sql, *self.incremental_sql)

    @property
    def identity(self) -> NodeIdentity | None:
        """Identity of the output table, falling back to the asset path."""

        if self.target.key:
            return NodeIdentity.for_target(self.target.database, self.target.schema, self.target.name)
        return self.asset_identity

    @property
    def asset_identity(self) -> NodeIdentity | None:
        return NodeIdentity.for_asset(self.path) if self.path else None

    @property
    def is_query(self) -> bool:
        return self.kind in {"table", "view", "incremental", "assertion", "sql", "unknown"}


@dataclass(frozen=True)
class PipelineDiagnostic:
    model: str
    code: str
    message: str


@dataclass(frozen=True)
class DuplicateOccurrence:
    model: str
    location: str  # "query", "cte:<name>" or "subquery:<alias>"


@dataclass(frozen=True)
class DuplicateGroup:
    fingerprint: str
    node_count: int
    sql: str
    occurrences: tuple[DuplicateOccurrence, ...]
