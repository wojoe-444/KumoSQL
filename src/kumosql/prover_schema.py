"""What the equivalence prover may assume about your tables.

The SMT prover proves more when it knows a table's columns, which columns are
never NULL and which sets of columns are unique (a join of a table to itself on
its key is the table, ``IS NOT NULL`` on a NOT NULL column is true). This module
collects those facts from two places and hands them over as ``schema`` and
``constraints`` for ``prove_equivalent_smt`` / ``prove_equivalent_algebraic``:

* BigQuery table metadata (the saved catalog): column names, ``REQUIRED``
  columns (never NULL) and a declared primary key;
* Dataform: the ``assertions`` of each action's config (``nonNull``,
  ``uniqueKey``, ``uniqueKeys``), plus the output columns written in the action's
  SELECT and the column lists of declared source tables.

A fact is used only when it was declared. BigQuery does not enforce primary
keys and Dataform assertions are checked only when they run, so the schema
carries a note the prover's result lists as an assumption. A table known under
several spellings (``project.dataset.table``, ``dataset.table``, ``table``) is
registered under each, except that a bare name shared by two tables is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

from kumo_sql_prover.smt_equivalence import TableConstraints

DECLARED_FACTS_NOTE = (
    "declared keys and NOT NULL columns hold in the data "
    "(BigQuery does not enforce primary keys; Dataform assertions are checked when they run)"
)


@dataclass
class ProverSchema:
    """Columns and constraints per table spelling, ready for the prover."""

    columns: dict[str, list[str]] = field(default_factory=dict)
    constraints: dict[str, TableConstraints] = field(default_factory=dict)
    # Declared column types per table spelling, for casts that change nothing.
    types: dict[str, dict[str, str]] = field(default_factory=dict)
    # Distinct tables with columns / with at least one declared fact.
    table_count: int = 0
    constrained_count: int = 0
    # Where the facts came from, for display ("bigquery", "dataform").
    sources: set[str] = field(default_factory=set)

    @property
    def notes(self) -> tuple[str, ...]:
        return (DECLARED_FACTS_NOTE,) if self.constrained_count else ()

    def __bool__(self) -> bool:
        return bool(self.columns)

    def to_json(self) -> dict:
        return {
            "tables": self.table_count,
            "constrained": self.constrained_count,
            "sources": sorted(self.sources),
        }


class _Builder:
    def __init__(self) -> None:
        # canonical full name -> merged facts
        self.columns: dict[str, list[str]] = {}
        self.not_null: dict[str, set[str]] = {}
        self.keys: dict[str, list[tuple[str, ...]]] = {}
        self.foreign_keys: dict[str, list[tuple]] = {}
        self.types: dict[str, dict[str, str]] = {}
        self.sources: set[str] = set()

    def add(
        self,
        name: str,
        columns: Iterable[str] = (),
        not_null: Iterable[str] = (),
        keys: Iterable[Iterable[str]] = (),
        source: str = "",
        types: Mapping[str, str] | None = None,
        foreign_keys: Iterable[tuple] = (),
    ) -> None:
        name = name.strip("`").lower()
        if not name:
            return
        listed = [c.lower() for c in columns]
        if listed and name not in self.columns:
            self.columns[name] = list(dict.fromkeys(listed))
        self.not_null.setdefault(name, set()).update(c.lower() for c in not_null)
        for key in keys:
            key = tuple(c.lower() for c in key)
            if key and key not in self.keys.setdefault(name, []):
                self.keys[name].append(key)
        for cols, parent, parent_cols in foreign_keys:
            fk = (tuple(c.lower() for c in cols), parent.strip("`").lower(), tuple(c.lower() for c in parent_cols))
            if fk[0] and len(fk[0]) == len(fk[2]) and fk not in self.foreign_keys.setdefault(name, []):
                self.foreign_keys[name].append(fk)
        if types:
            self.types.setdefault(name, {}).update({c.lower(): t for c, t in types.items()})
        if source:
            self.sources.add(source)

    def build(self) -> ProverSchema:
        by_suffix: dict[str, set[str]] = {}
        names = set(self.columns) | {n for n, v in self.not_null.items() if v} | {n for n, v in self.keys.items() if v} | {n for n, v in self.foreign_keys.items() if v}
        for name in names:
            parts = name.split(".")
            for start in range(len(parts)):
                by_suffix.setdefault(".".join(parts[start:]), set()).add(name)
        schema = ProverSchema(sources=set(self.sources))
        for spelling, owners in by_suffix.items():
            if len(owners) != 1:
                continue  # a spelling shared by two tables says nothing about either
            owner = next(iter(owners))
            if owner in self.columns:
                schema.columns[spelling] = list(self.columns[owner])
            if self.types.get(owner):
                schema.types[spelling] = dict(self.types[owner])
            facts = TableConstraints(
                not_null=frozenset(self.not_null.get(owner, ())),
                keys=tuple(self.keys.get(owner, ())),
                foreign_keys=tuple(self.foreign_keys.get(owner, ())),
            )
            if facts.not_null or facts.keys or facts.foreign_keys:
                schema.constraints[spelling] = facts
        schema.table_count = len(self.columns)
        schema.constrained_count = len(
            {n for n in names if self.not_null.get(n) or self.keys.get(n)}
        )
        return schema


def _add_bigquery(builder: _Builder, project: str, dataset: str, table: str, data: Mapping) -> None:
    fields = [f for f in (data.get("schema") or []) if isinstance(f, Mapping) and f.get("name")]
    not_null = [f["name"] for f in fields if str(f.get("mode", "NULLABLE")).upper() == "REQUIRED"]
    keys = []
    constraints = data.get("constraints")
    if isinstance(constraints, Mapping):
        primary = (constraints.get("primaryKey") or {}).get("columns") or []
        if primary:
            keys.append(primary)
            not_null = not_null + list(primary)
    foreign = []
    if isinstance(constraints, Mapping):
        for fk in constraints.get("foreignKeys") or []:
            ref = fk.get("referencedTable") or {}
            refs = fk.get("columnReferences") or []
            if ref.get("tableId") and refs:
                parent = ".".join(str(ref[k]) for k in ("projectId", "datasetId", "tableId") if ref.get(k))
                foreign.append(([r["referencingColumn"] for r in refs], parent, [r["referencedColumn"] for r in refs]))
    exact = {"INTEGER": "BIGINT", "INT64": "BIGINT", "NUMERIC": "DECIMAL(38, 9)", "DECIMAL": "DECIMAL(38, 9)"}
    types = {f["name"]: exact[str(f.get("type", "")).upper()] for f in fields if str(f.get("type", "")).upper() in exact}
    builder.add(
        f"{project}.{dataset}.{table}", [f["name"] for f in fields], not_null, keys, source="bigquery", types=types, foreign_keys=foreign
    )


def _select_names(sql: str) -> list[str]:
    """Output column names of the model's top-level SELECT, or ``[]`` when a star or unnamed column hides them."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return []
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        while isinstance(tree, (exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
            tree = tree.this
    if not isinstance(tree, exp.Select):
        return []
    names = []
    for item in tree.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return []
        name = item.alias_or_name
        if not name:
            return []
        names.append(name)
    return names if len(set(n.lower() for n in names)) == len(names) else []


def from_bigquery(tables: Iterable[tuple[str, str, str, Mapping]]) -> ProverSchema:
    """Facts from BigQuery table metadata: ``(project, dataset, table, {"schema": [...], "constraints": ...})``."""

    builder = _Builder()
    for project, dataset, table, data in tables:
        _add_bigquery(builder, project, dataset, table, data)
    return builder.build()


def from_pipeline(pipeline, bigquery: Iterable[tuple[str, str, str, Mapping]] = ()) -> ProverSchema:
    """Facts from a loaded Dataform project, merged with BigQuery metadata when given."""

    builder = _Builder()
    for project, dataset, table, data in bigquery:
        _add_bigquery(builder, project, dataset, table, data)
    for key, columns in (getattr(pipeline, "source_schema", None) or {}).items():
        if isinstance(columns, Mapping) and columns:
            builder.add(str(key), list(columns), source="dataform")
    for key, model in getattr(pipeline, "models", {}).items():
        columns = _select_names(model.sql) if model.is_query and "${" not in model.sql else []
        builder.add(
            model.target.key or key,
            columns,
            model.non_null,
            model.unique_keys,
            source="dataform" if (model.non_null or model.unique_keys or columns) else "",
        )
    return builder.build()
