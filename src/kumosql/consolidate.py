"""Fold a chain of intermediate tables into the table that ends it, and prove nothing changed.

``consolidate_tables(pipeline, ["A", "B", "C"], "D")`` rewrites ``D`` so it reads straight from what
``A``, ``B`` and ``C`` read: each folded table becomes a ``WITH`` table of ``D``'s own query, named after
the table it replaces and ordered sources first, and every reference to it (from ``D`` or from another
folded table) points at that name. A table shared by two folded readers (``A`` feeding ``B`` and ``C``)
is written once.

The result carries the new SQL of ``D`` and a verdict. ``equivalent`` means :func:`kumosql.refactor.check_observable`
(the proof the Refactor page uses, built on :func:`kumosql.pipeline_equivalence.prove_models`) proved the new ``D``
returns the same rows as the original ``D`` read through the folded tables. Anything else is ``unknown``: the
SQL is still returned so it can be read, but nothing is claimed about it.

A proof that rests on a LIMIT cutting the same rows each time (``LIMIT_SOURCE_ASSUMPTION``, ``TIE_ASSUMPTION``) is not
returned as ``equivalent``: folding turns a stored table into a recomputed query, and when its physical order or tie
order differs, the LIMIT can pick other rows. Pass ``stable_selection=True`` to state that contract yourself.

A folded table that something outside the set still reads is never folded: the call raises
:class:`ConsolidationError` naming that reader, because dropping the table would break it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import set_with_clause, with_clause
from .pipeline_equivalence import _opaque, _resolve
from .prover_schema import ProverSchema
from .refactor import _Reads, _alias_of, _parse, _replace_tables, check_observable
from kumo_sql_prover.smt_equivalence import LIMIT_SOURCE_ASSUMPTION, TIE_ASSUMPTION

SELECTION_ASSUMPTIONS = (LIMIT_SOURCE_ASSUMPTION, TIE_ASSUMPTION)

FOLDABLE = ("table", "view", "sql")


HELP_SUMMARY = (
    "READ-ONLY PREVIEW. Shows what one table would look like with a chain of intermediate tables folded into it,\n"
    "and whether that was proved to return the same rows. It prints JSON to the screen and changes none of your files."
)
HELP_EPILOG = (
    "This command never writes, moves, renames or deletes any of your files, never touches the project folder, and\n"
    "has no option that does. Copy the printed \"sql\" into the target model yourself if you want it.\n"
    "(Like every KumoSQL command that analyzes a project, it appends timing lines to KumoSQL's own diagnostic log,\n"
    "ui.log, in KumoSQL's data folder; that is not part of your project.)\n"
    "\n"
    "Exit code: 0 proved equal, 1 not proved (unknown), 2 refused or bad input.\n"
    "Folds whose proof needs a LIMIT to cut the same rows each time are reported as unknown unless you pass\n"
    "--stable-selection (your statement that the LIMIT picks the same rows from a stored or a recomputed table).\n"
    "Example: python -m kumosql consolidate-tables path/to/project D A B C"
)


class ConsolidationError(ValueError):
    """The tables cannot be folded; ``readers`` maps a folded table to the models outside the set that read it."""

    def __init__(self, message: str, readers: dict[str, list[str]] | None = None) -> None:
        super().__init__(message)
        self.readers = readers or {}


@dataclass
class ConsolidationResult:
    status: str  # "equivalent" or "unknown"
    reason: str
    target: str
    folded: list[str]  # the tables folded away, sources first
    sql: str  # the new SQL of the target
    original_sql: str
    assumptions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def proven(self) -> bool:
        return self.status == "equivalent"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "target": self.target,
            "folded": self.folded,
            "sql": self.sql,
            "original_sql": self.original_sql,
            "assumptions": self.assumptions,
            "notes": self.notes,
        }


def _names(pipeline, tables: Iterable[str], target: str) -> tuple[list[str], str]:
    target_key = _resolve(pipeline, target)
    members: list[str] = []
    for name in tables:
        key = _resolve(pipeline, name)
        if key != target_key and key not in members:
            members.append(key)
    if not members:
        raise ConsolidationError("name at least one table to fold into the target")
    return members, target_key


def _check_models(pipeline, members: list[str], target: str) -> None:
    for key in (*members, target):
        model = pipeline.models[key]
        if model.kind not in FOLDABLE:
            raise ConsolidationError(f"{key} is a {model.kind}, and only tables and views can be folded")
        if _opaque(model):
            raise ConsolidationError(f"{key} could not be read as a plain query")
        if model.operations_sql:
            raise ConsolidationError(f"{key} runs pre or post operations, which folding would drop")
    readers = pipeline.downstream
    outside = {
        key: sorted(r for r in readers.get(key, ()) if r != target and r not in members) for key in members
    }
    outside = {key: found for key, found in outside.items() if found}
    if outside:
        key, found = next(iter(outside.items()))
        more = f" (and {sum(map(len, outside.values())) - 1} more reads elsewhere)" if sum(map(len, outside.values())) > 1 else ""
        raise ConsolidationError(f"{key} is still read by {found[0]}, which is not part of the fold{more}", outside)
    for key in members:
        if not readers.get(key):
            raise ConsolidationError(f"nothing reads {key}, so there is nothing to fold it into")


def _taken_names(trees: Iterable[exp.Expression]) -> set[str]:
    taken: set[str] = set()
    for tree in trees:
        for cte in tree.find_all(exp.CTE):
            taken.add(cte.alias_or_name.lower())
        for table in tree.find_all(exp.Table):
            if not table.db:
                taken.add(table.name.lower())
    return taken


def _unique(base: str, taken: set[str]) -> str:
    name, count = base, 1
    while name.lower() in taken:
        count += 1
        name = f"{base}_{count}"
    taken.add(name.lower())
    return name


def fold_sql(pipeline, members: list[str], target: str) -> str:
    """The SQL of ``target`` with every table of ``members`` (in pipeline order) turned into a ``WITH`` table."""

    sources = {key: pipeline.models[key].sql for key in (*members, target)}
    trees = {key: _parse(sql) for key, sql in sources.items()}
    for key, tree in trees.items():
        if not isinstance(tree, exp.Query):
            raise ConsolidationError(f"{key} is not a single SELECT")
        clause = with_clause(tree)
        if clause is not None and clause.args.get("recursive"):
            raise ConsolidationError(f"{key} uses a recursive WITH")
    taken = _taken_names(trees.values())
    names = {key: _unique(key.split(".")[-1], taken) for key in members}

    def swap(table: exp.Table, key: str):
        if key not in names:
            return None
        alias = _alias_of(table, key)
        node = exp.Table(this=exp.to_identifier(names[key]))
        if alias != names[key]:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        return node

    def rewritten(key: str) -> exp.Expression:
        return _parse(_replace_tables(sources[key], pipeline.resolve, swap))

    final = rewritten(target)
    existing = with_clause(final)
    ctes = []
    for key in members:
        ctes.append(exp.CTE(this=rewritten(key), alias=exp.TableAlias(this=exp.to_identifier(names[key]))))
    own = list(existing.expressions) if existing is not None else []
    set_with_clause(final, exp.With(expressions=[*ctes, *own]))
    return final.sql(dialect="bigquery", pretty=True)


def consolidate_tables(
    pipeline,
    tables: Iterable[str],
    target: str,
    *,
    schema: ProverSchema | None = None,
    timeout_ms: int = 5000,
    stable_selection: bool = False,
) -> ConsolidationResult:
    """Fold ``tables`` into ``target`` and prove the new ``target`` returns the rows the old one did.

    Raises :class:`ConsolidationError` when the fold is not allowed (a folded table is read from outside the
    set, a model is not a plain table or view, a name is unknown). Otherwise returns the new SQL with status
    ``equivalent`` (proved) or ``unknown`` (not proved; no claim is made). A proof that assumes a LIMIT picks the
    same rows whether its input is stored or recomputed is ``unknown`` unless ``stable_selection`` is true.
    """

    members, target_key = _names(pipeline, tables, target)
    _check_models(pipeline, members, target_key)
    members = [key for key in pipeline.topological_order() if key in members]  # sources first
    try:
        sql = fold_sql(pipeline, members, target_key)
        _parse(sql)
    except sqlglot.errors.SqlglotError as error:
        raise ConsolidationError(f"could not rewrite {target_key}: {error}") from error
    original = pipeline.models[target_key].sql
    notes = []
    for key in members:
        model = pipeline.models[key]
        if model.non_null or model.unique_keys:
            notes.append(f"{key} declared assertions that are dropped with it")

    candidate = {k: m.sql for k, m in pipeline.models.items() if m.is_query and k not in members}
    candidate[target_key] = sql
    ok, assumptions, why = check_observable(
        pipeline, candidate, [target_key], _Reads(pipeline), schema=schema, timeout_ms=timeout_ms,
    )
    if ok:
        unstable = [a for a in assumptions if a in SELECTION_ASSUMPTIONS]
        if unstable and not stable_selection:
            reason = (
                f"the proof assumes that {unstable[0]}; folding replaces a stored table with a recomputed query, "
                "so a LIMIT can pick other rows when the physical or tie order differs "
                "(state a stable-selection contract to accept this)"
            )
            return ConsolidationResult("unknown", reason, target_key, members, sql, original, [], notes)
        if unstable:
            notes.append("accepted on your stable-selection contract: a LIMIT picks the same rows from a stored or a recomputed table")
        return ConsolidationResult("equivalent", "proved equal to the original by the pipeline prover", target_key,
                                   members, sql, original, assumptions, notes)
    hidden = _star_without_columns(pipeline, [*members, target_key], schema)
    if hidden:
        why = f"{why}; SELECT * reads {', '.join(hidden)}, whose columns are not declared (declare them or load the BigQuery catalog)"
    return ConsolidationResult("unknown", why or "not proved", target_key, members, sql, original, [], notes)


def _star_without_columns(pipeline, keys: list[str], schema: ProverSchema | None) -> list[str]:
    """Tables read by a ``SELECT *`` in the given models whose columns the prover does not know."""

    known = {name.lower() for name in (schema.columns if schema else {})}
    found: list[str] = []
    for key in keys:
        try:
            tree = _parse(pipeline.models[key].sql)
        except sqlglot.errors.SqlglotError:
            continue
        for select in tree.find_all(exp.Select):
            if not any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in select.expressions):
                continue
            for table in select.find_all(exp.Table):
                name = pipeline.resolve(table) or table.name
                if name.lower() not in known and name not in found:
                    found.append(name)
    return found


def consolidate_loaded(tables: object, target: object, stable_selection: object = False) -> dict:
    """``POST /api/consolidate-tables``: fold tables of the project loaded in the app into a target."""

    from . import live_graph, prover_context

    if (
        not isinstance(tables, list) or not tables or any(not isinstance(t, str) or not t.strip() for t in tables)
        or not isinstance(target, str) or not target.strip()
    ):
        raise ValueError("name the tables to fold and the target")
    loaded = live_graph.loaded()
    if not loaded:
        raise ValueError("load a project first")
    config = prover_context.settings()
    if not config["enabled"]:
        raise ValueError("the solver is turned off in Settings")
    return consolidate_tables(
        loaded["pipeline"], tables, target, schema=prover_context.current_schema(), timeout_ms=config["timeout_ms"],
        stable_selection=stable_selection is True,
    ).to_json()


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql consolidate-tables DIR TARGET TABLE [TABLE ...]``"""

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m kumosql consolidate-tables", description=HELP_SUMMARY, epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project", help="Dataform or SQL folder (read, never modified)")
    parser.add_argument("target", help="the table that keeps existing and absorbs the others")
    parser.add_argument("tables", nargs="+", metavar="TABLE", help="intermediate tables to fold into the target")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--stable-selection", action="store_true",
                        help="accept proofs that assume a LIMIT picks the same rows from a stored or a recomputed table")
    args = parser.parse_args(argv)
    from .pipeline_loading import load_sqlx_project
    from .prover_schema import from_pipeline

    try:
        pipeline = load_sqlx_project(args.project)
        result = consolidate_tables(
            pipeline, args.tables, args.target, schema=from_pipeline(pipeline), timeout_ms=args.timeout_ms,
            stable_selection=args.stable_selection,
        )
    except ConsolidationError as error:
        json.dump({"status": "refused", "reason": str(error), "readers": error.readers}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        print(f"error: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(result.to_json(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result.proven else 1
