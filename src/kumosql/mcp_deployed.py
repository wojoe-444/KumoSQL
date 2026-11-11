"""``deployed_status``: is the table built, and does it still look like the repository says?

The repository and the warehouse drift: a model is renamed in the repository and the live table still has the old
column, a model was never built, a partitioning changed and BigQuery refuses to replace the table. Code that is correct
then fails with "column not found" or "cannot replace a table with a different partitioning spec". This module compares
the two sides from what the server already has, **offline and read-only**: the project (what a model's query outputs, its
``partitionBy`` / ``clusterBy`` config) and the schema snapshot (:mod:`kumosql.schema_snapshot`, a file of
``INFORMATION_SCHEMA.COLUMNS`` someone exported). It never queries a warehouse, never builds anything and never writes.

The tool ``deployed_status`` reports for one table:

* **built**: present in the snapshot or not. "Not in the snapshot" is not "not built": the snapshot may be older or may not
  cover the dataset, and the answer says which it can tell (a dataset the snapshot lists no table of is named);
* **columns** only in the repository (the model's query outputs them, the snapshot lacks them) and only in the warehouse
  (the snapshot has them, the model no longer outputs them), with **possible renames** when one column of each side has the
  same type and a close name (never certain), and **type differences** (the type the model's query gives against the
  snapshot's, both known);
* **partitioning and clustering** of the model's config against ``is_partitioning_column`` and
  ``clustering_ordinal_position`` of the snapshot. A snapshot without those columns, or a config that cannot be read,
  puts them under ``unknown`` with the reason; they are never reported as a match;
* **how old** the snapshot is (the export's timestamp column, else the file's modification time), because the deployed
  table may have changed since.

``check_query`` uses :func:`check_draft`: a table the draft reads that is missing from the snapshot, a column the draft uses
that the repository has but the deployed table (snapshot) lacks, and the age of an old snapshot. Without a snapshot it says
that nothing about the deployed tables was checked.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlglot import exp

from .mcp_answers import Answer, clip, tool
from .mcp_session import Session
from .mcp_tools import need_table

#: model kinds that are a stored table with a partitioning and a clustering of their own
_STORED = ("table", "incremental", "unknown")
#: a column list this long is shown in full; more is counted and cut to this many names
SHOWN = 8


# ------------------------------------------------------------------ snapshot age


def snapshot_age(snapshot, now: float | None = None) -> tuple[float | None, str | None]:
    """``(seconds, "snapshot file is 3 days old")`` for a snapshot, or ``(None, None)`` when it carries no time."""

    stamp, what = (snapshot.exported_at, "snapshot export") if snapshot.exported_at is not None else (snapshot.modified_at, "snapshot file")
    if stamp is None:
        return None, None
    seconds = max(0.0, (now if now is not None else time.time()) - stamp)
    if seconds < 3600:
        text = "less than an hour old"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        text = f"{hours} hour{'s' if hours != 1 else ''} old"
    else:
        days = int(seconds // 86400)
        text = f"{days} day{'s' if days != 1 else ''} old"
    return seconds, f"{what} is {text}"


def _old(seconds: float | None) -> bool:
    return seconds is not None and seconds >= 86400


# ------------------------------------------------------------------ the two sides


def _short(session: Session, key: str) -> str:
    """The table name an agent can pass back: the bare name when it finds this table, else ``dataset.table``."""

    bare = key.split(".")[-1]
    return bare if session.resolve(bare)[0] == key else ".".join(key.split(".")[-2:])


def _norm(type_text: str | None) -> str | None:
    """A BigQuery type as one spelling (``INTEGER`` is ``INT64``), or ``None`` when it is not known completely."""

    from .googlesql_types import parse_type

    if not type_text:
        return None
    parsed = parse_type(type_text)
    return parsed.sql() if parsed is not None and parsed.complete else None


def repository_columns(session: Session, key: str) -> tuple[dict[str, str | None] | None, str | None]:
    """``({column: type or None}, None)`` for what the repository says the table has, or ``(None, reason)``.

    A model's columns are what its query outputs (types from :mod:`kumosql.googlesql_types`, reading the tables it reads with
    their snapshot types where the snapshot lists them); a declared source's are its declared schema.
    """

    pipeline = session.pipeline()
    model = pipeline.models.get(key)
    if model is None:
        declared = pipeline.source_schema.get(key) or {}
        if not declared:
            return None, "the project declares no columns for this source (--source-schema gives them)"
        return {name: _norm(str(kind)) if kind else None for name, kind in declared.items()}, None
    from .mcp_preflight import _FAILED_META  # the reasons that make a model's output column list untrustworthy

    outputs = list(pipeline.output_columns(key))
    profile = session.profiles().get(key)
    if not outputs:
        return None, "the model's output columns could not be read from its query"
    if profile is not None and any((a.reason or "").split(":")[0] in _FAILED_META for a in profile.attributes):
        return None, "the model's query could not be read completely (a SELECT * that could not be expanded, or a parse problem)"
    return {name: kind for name, kind in zip(outputs, _model_types(session, key, outputs))}, None


def _model_types(session: Session, key: str, outputs: list[str]) -> list[str | None]:
    from .googlesql_types import Catalog, Column, infer

    pipeline = session.pipeline()
    types = session.column_types()
    catalog = Catalog()
    for parent in sorted(pipeline.upstream.get(key, ())):
        names = list(pipeline.output_columns(parent)) if parent in pipeline.models else list(pipeline.source_schema.get(parent, {}))
        have = {n.casefold() for n in names}
        names += [n for n in types.columns(parent) if n.casefold() not in have]
        if names:
            catalog.add(parent, [Column(name, types.type(parent, name)) for name in names])
    try:
        typed = infer(pipeline.models[key].sql, catalog)
    except Exception:  # noqa: BLE001 - a query the typer cannot read leaves the types unknown, not the answer
        return [None] * len(outputs)
    found = {c.name.casefold(): c.type for c in (typed.columns or ()) if c.name}
    return [found[n.casefold()].sql() if found.get(n.casefold()) is not None and found[n.casefold()].complete else None for n in outputs]


def possible_renames(only_repository: dict[str, str | None], only_warehouse: dict[str, str | None]) -> list[tuple[str, str]]:
    """``[(repository name, warehouse name)]``: one column of each side with the same known type and a close name.

    Only a pair that is the other's single candidate is named, so a table with several new and several old columns
    names none rather than guess. It is a hint to check, never a fact.
    """

    def close(new: str, old: str) -> bool:
        a, b = new.casefold(), old.casefold()
        tokens = {t for t in re.split(r"[_\W]+", a) if len(t) >= 3} & {t for t in re.split(r"[_\W]+", b) if len(t) >= 3}
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6 or bool(tokens)

    pairs = [(n, o) for n, tn in only_repository.items() for o, to in only_warehouse.items() if tn is not None and tn == to and close(n, o)]
    return [(n, o) for n, o in pairs if sum(1 for p in pairs if p[0] == n) == 1 and sum(1 for p in pairs if p[1] == o) == 1]


# ------------------------------------------------------------------ partitioning and clustering


@dataclass
class Layout:
    """What a model's config says about partitioning and clustering, read from its file."""

    partition_by: str | None = None  # the expression as written, ``None`` when the config has none
    cluster_by: tuple[str, ...] = ()
    problem: str | None = None  # why the config could not be read (then both are unknown)


def _config_layout(session: Session, key: str) -> Layout:
    from . import pipeline_loading as loading

    model = session.pipeline().models[key]
    root = session.root
    if root is None or not model.path:
        return Layout(problem="the session has no project folder to read the model's config from")
    path = Path(model.path)
    if path.suffix.lower() == ".js":
        return Layout(problem="the model is a .js file; its config is not read")
    try:
        full = (root / path).resolve()
        full.relative_to(root.resolve())  # a file outside the project is never read
        text = full.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return Layout(problem="the model's file could not be read inside the project folder")
    try:
        sections = loading._split_sqlx_sections(text)
    except ValueError:
        return Layout(problem="the model's file could not be split into config and query")
    config = next((body for kind, body in sections if kind == "block" and body.lstrip().startswith("config")), "")

    def value_of(name: str) -> str | None:
        match = re.search(rf"(?<![\w$.])['\"`]?{name}['\"`]?\s*:\s*", loading._blank_strings(config))
        return loading._value_at(config, match.end()).strip() if match else None

    layout = Layout()
    raw = value_of("partitionBy")
    if raw is not None:
        quoted = re.fullmatch(r"""(['"`])((?:\\.|(?!\1).)*)\1""", raw, re.S)
        if not quoted or "${" in quoted.group(2):
            return Layout(problem="partitionBy is computed (a variable or a call), so its value is not known without running the project")
        layout.partition_by = quoted.group(2).strip()
    raw = value_of("clusterBy")
    if raw is not None:
        literals = list(re.finditer(r"""(['"`])((?:\\.|(?!\1).)*)\1""", raw, re.S))
        leftover = re.sub(r"[\s,\[\]]", "", loading._ANY_STRING_RE.sub("", raw))
        if not literals or leftover or any("${" in m.group(2) for m in literals):
            return Layout(problem="clusterBy is computed (a variable or a call), so its value is not known without running the project")
        layout.cluster_by = tuple(m.group(2).strip() for m in literals)
    return layout


def _partition_columns(expression: str) -> set[str] | None:
    """The columns a ``partitionBy`` expression reads (``DATE(placed_at)`` reads ``placed_at``), or ``None`` when it cannot be parsed."""

    import sqlglot

    try:
        tree = sqlglot.parse_one(expression, read="bigquery")
    except Exception:  # noqa: BLE001
        return None
    found = {c.name.casefold() for c in tree.find_all(exp.Column)}
    return found or None


def _layout_findings(session: Session, key: str, snapshot_rows: tuple) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """``(compared lines, unknown (what, reason), differences)`` for partitioning and clustering of a stored table."""

    snapshot = session.schema_snapshot()
    layout = _config_layout(session, key)
    lines: list[str] = []
    unknown: list[tuple[str, str]] = []
    differs: list[str] = []
    if layout.problem:
        return lines, [("partitioning and clustering of the model's config", layout.problem)], differs
    # partitioning
    if not snapshot.has_partitioning or all(c.is_partitioning is None for c in snapshot_rows):
        unknown.append(("partitioning of the deployed table", "the snapshot has no is_partitioning_column column (export it from INFORMATION_SCHEMA.COLUMNS)"))
    else:
        warehouse = sorted(c.column for c in snapshot_rows if c.is_partitioning)
        wanted = _partition_columns(layout.partition_by) if layout.partition_by else set()
        if wanted is None:
            unknown.append(("partitioning compared", f"the partitionBy expression {clip(layout.partition_by, 60)!r} could not be read"))
        else:
            same = wanted == {w.casefold() for w in warehouse}
            repository = layout.partition_by or "none"
            lines.append(f"partitioning: repository {clip(repository, 50)}, snapshot {', '.join(warehouse) or 'none'}: {'same column' if same else 'DIFFERENT'}")
            if not same:
                differs.append("partitioning")
    # clustering
    if not snapshot.has_clustering:
        unknown.append(("clustering of the deployed table", "the snapshot has no clustering_ordinal_position column (export it from INFORMATION_SCHEMA.COLUMNS)"))
    else:
        warehouse = [c.column for c in sorted((c for c in snapshot_rows if c.clustering_position is not None), key=lambda c: c.clustering_position)]
        same = [c.casefold() for c in layout.cluster_by] == [w.casefold() for w in warehouse]
        lines.append(f"clustering: repository {', '.join(layout.cluster_by) or 'none'}, snapshot {', '.join(warehouse) or 'none'}: {'same' if same else 'DIFFERENT'}")
        if not same:
            differs.append("clustering")
    return lines, unknown, differs


# ------------------------------------------------------------------ the snapshot side


def _snapshot_state(session: Session, key: str):
    """``(rows, why)``: the snapshot's columns of this table, or ``None`` and the reason there are none to compare."""

    if session.schema_snapshot() is None:
        if session.snapshot_error:
            return None, f"the schema snapshot could not be read ({clip(session.snapshot_error, 100)})"
        return None, "no schema snapshot was given (start the server with --schema-snapshot FILE)"
    types = session.column_types()
    rows = types.snapshot_rows.get(key)
    if rows is not None:
        return rows, None
    tail = key.split(".")[-1].casefold()
    if any(name.split(".")[-1].casefold() == tail for name in types.unmatched):
        return None, "ambiguous: the snapshot has a table with this name that cannot be told apart from another table of the project"
    return None, "absent"


def _dataset_covered(session: Session, key: str) -> bool | None:
    """Whether the snapshot lists any table of this table's dataset (``None`` when the key has no dataset)."""

    parts = key.casefold().split(".")
    if len(parts) < 2:
        return None
    dataset = parts[-2]
    return any(len(name.split(".")) >= 2 and name.split(".")[-2] == dataset for name in session.schema_snapshot().tables())


# ------------------------------------------------------------------ the tool


@tool(
    "deployed_status",
    "Is the table built, and does it match the repository?",
    "Is this table built in the warehouse, and does it still match what the repository says? Compared offline from the schema snapshot "
    "(--schema-snapshot): built or not in the snapshot, columns only in the repository or only in the warehouse (with possible renames, "
    "never certain), type differences, and partitioning and clustering against the snapshot when it has those columns (else unknown, "
    "with the reason). Says how old the snapshot is. It never queries a warehouse, never builds anything and never writes.",
    {"table": {"type": "string", "description": "A table of the project: full key, dataset.table or the table name."}},
    required=("table",),
)
def deployed_status(session: Session, args: dict) -> Answer:
    pipeline = session.pipeline()
    key = need_table(session, args["table"])
    name = _short(session, key)
    model = pipeline.models.get(key)
    answer = Answer("")
    answer.fields["table"] = key
    rows, why = _snapshot_state(session, key)
    if rows is None and why != "absent" and not why.startswith("ambiguous"):
        answer.summary = f"Cannot compare {name} with the warehouse: {why}."
        answer.fields["deployed"] = "unknown"
        answer.unknown("whether the table is built and matches", why)
        answer.next("Export INFORMATION_SCHEMA.COLUMNS (a free metadata query, run by someone with warehouse access) and start the server with --schema-snapshot FILE; this tool never queries a warehouse.")
        return answer
    snapshot = session.schema_snapshot()
    seconds, age = snapshot_age(snapshot)
    if age:
        answer.fields["snapshot"] = age + ("; the deployed table may have changed since" if _old(seconds) else "")
    else:
        answer.unknown("how old the snapshot is", "the file has no timestamp column and its modification time is not known")
    if model is not None and model.disabled:
        answer.unknown("whether Dataform builds it", "the model is disabled: true, so Dataform never runs it")
    if rows is None:
        answer.fields["deployed"] = "unknown" if why.startswith("ambiguous") else "not_in_snapshot"
        if why.startswith("ambiguous"):
            answer.summary = f"Cannot tell whether {name} is built: {why}."
            answer.unknown("which snapshot table is this one", "give the snapshot its project and dataset columns (table_catalog, table_schema)")
            answer.next("Re-export the snapshot with table_catalog and table_schema, then ask again.")
            return answer
        covered = _dataset_covered(session, key)
        answer.summary = f"{name} is not in the schema snapshot: it may not be built, or it was built after the export."
        if covered is False:
            answer.unknown("whether the snapshot covers this dataset", f"it lists no table of dataset {key.split('.')[-2] if '.' in key else '?'}; it may leave the dataset out")
        else:
            answer.unknown("whether it is built", "absent from the snapshot; a table built after the export would be missing too")
        answer.next(f"If {name} should exist, build it (a Dataform run by someone with access; this tool builds nothing) or refresh the snapshot if it was built since, then ask again.")
        return answer

    repository, reason = repository_columns(session, key)
    answer.fields["deployed"] = "in_snapshot"
    warehouse = {c.column: _norm(c.data_type) for c in rows}
    if repository is None:
        answer.summary = f"{name} is in the schema snapshot ({len(warehouse)} columns); its columns were not compared with the repository."
        answer.counts = {"warehouse_columns": len(warehouse)}
        answer.unknown("columns against the repository", reason)
        answer.next("Check the model's output columns with describe_table, then ask again.")
        return answer

    repo_by = {n.casefold(): n for n in repository}
    wh_by = {n.casefold(): n for n in warehouse}
    only_repo = {repo_by[f]: repository[repo_by[f]] for f in repo_by if f not in wh_by}
    only_wh = {wh_by[f]: warehouse[wh_by[f]] for f in wh_by if f not in repo_by}
    both = [f for f in repo_by if f in wh_by]
    diffs = []
    untyped = 0
    for folded in both:
        left, right = repository[repo_by[folded]], warehouse[wh_by[folded]]
        if left is None or right is None:
            untyped += 1
        elif left != right:
            diffs.append({"column": repo_by[folded], "repository": left, "snapshot": right})
    renames = possible_renames(only_repo, only_wh)

    lines, layout_unknown, layout_diffs = ([], [], [])
    if model is not None and model.kind in _STORED:
        lines, layout_unknown, layout_diffs = _layout_findings(session, key, rows)
    answer.counts = {
        "repository_columns": len(repository), "warehouse_columns": len(warehouse), "in_both": len(both),
        "only_in_repository": len(only_repo), "only_in_warehouse": len(only_wh), "possible_renames": len(renames),
        "type_differences": len(diffs), "layout_differences": len(layout_diffs),
    }
    answer.add_list("only_in_repository", [f"{n} {t or '?'}" for n, t in only_repo.items()][:SHOWN], total=len(only_repo), keep=1)
    answer.add_list("only_in_warehouse", [f"{n} {t or '?'}" for n, t in only_wh.items()][:SHOWN], total=len(only_wh), keep=1)
    answer.add_list("possible_renames", [clip(f"{o} (warehouse) may now be {n} (repository), both {only_repo[n]}; possible rename, not certain", 150) for n, o in renames][:SHOWN], total=len(renames), keep=1)
    answer.add_list("type_differences", diffs[:SHOWN], total=len(diffs), keep=1)
    answer.add_list("partitioning_and_clustering", lines, total=len(lines), keep=1)
    for what, why_ in layout_unknown:
        answer.unknown(what, why_)
    if untyped:
        answer.unknown(f"types of {untyped} column{'s' if untyped != 1 else ''}", "the model's query does not fix them or the snapshot's type is not one KumoSQL reads, so they were not compared")
    if model is None:
        answer.unknown("partitioning and clustering", "this is a declared source; its config is not in the project")

    differs = bool(only_repo or only_wh or diffs or layout_diffs)
    parts = [f"{len(only_repo)} only in the repository" if only_repo else "", f"{len(only_wh)} only in the warehouse" if only_wh else "",
             f"{len(renames)} possible rename{'s' if len(renames) != 1 else ''}" if renames else "",
             f"{len(diffs)} type difference{'s' if len(diffs) != 1 else ''}" if diffs else "", " and ".join(layout_diffs) + " differ" if layout_diffs else ""]
    detail = ", ".join(p for p in parts if p)
    answer.summary = (f"{name} is built (in the snapshot) and differs from the repository: {detail}." if differs
                      else f"{name} is built and its columns{', partitioning and clustering' if lines else ''} match the repository as far as the snapshot shows.")
    if only_repo or diffs:
        answer.next(f"Build {name} (a Dataform run by someone with access; this tool builds nothing) so the table gets the repository's columns, or refresh the snapshot if it was built since; then ask again.")
    if only_wh and not only_repo:
        answer.next(f"The warehouse table has columns the model no longer outputs ({next(iter(only_wh))}, ...): code that reads them is reading a stale column; rebuild {name} to drop them.")
    if "partitioning" in layout_diffs or "clustering" in layout_diffs:
        answer.next("BigQuery refuses to replace a table whose partitioning differs (\"cannot replace a table with a different partitioning spec\"): the table's owner has to drop it, or the config goes back to the deployed spec.")
    if not differs:
        answer.next("Nothing differs in what the snapshot shows" + ("; it is old, so refresh it before relying on this." if _old(seconds) else "; refresh the snapshot if the table was rebuilt since."))
    return answer


# ------------------------------------------------------------------ check_query


@dataclass
class DraftNotes:
    """What ``check_query`` adds about the deployed tables of a draft."""

    findings: list[dict] = field(default_factory=list)
    gaps: list[tuple[str, str]] = field(default_factory=list)
    next: list[str] = field(default_factory=list)


def _used_columns(session: Session, draft, known: dict[str, set[str]]) -> dict[str, set[str]]:
    """``{table key: lower-case columns the draft names}`` for the project tables it reads; a column it cannot place is left out."""

    tree = draft.tree
    owners: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        spelled = ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name).casefold()
        key = draft.tables.get(spelled)
        if key:
            owners[(table.alias_or_name or "").casefold()] = key
            owners[spelled] = key
    aliases = {a.alias.casefold() for a in tree.find_all(exp.Alias) if a.alias}
    used: dict[str, set[str]] = {}
    for column in tree.find_all(exp.Column):
        name = column.name.casefold()
        if column.table:
            key = owners.get(column.table.casefold())
        else:
            candidates = [k for k, names in known.items() if name in names]
            key = candidates[0] if len(candidates) == 1 and name not in aliases else None
        if key:
            used.setdefault(key, set()).add(name)
    return used


def check_draft(session: Session, draft) -> DraftNotes:
    """Findings, unknowns and next steps about whether the tables a draft reads are deployed as the repository says."""

    notes = DraftNotes()
    pipeline = session.pipeline()
    keys = sorted(k for k in draft.keys() if k in pipeline.sources or (k in pipeline.models and pipeline.models[k].kind in (*_STORED, "view")))
    if not keys:
        return notes
    snapshot = session.schema_snapshot()
    if snapshot is None:
        why = f"the schema snapshot could not be read ({clip(session.snapshot_error, 100)})" if session.snapshot_error else "no schema snapshot was given (--schema-snapshot FILE)"
        notes.gaps.append(("whether the tables it reads are built and current", why))
        return notes
    types = session.column_types()
    seconds, age = snapshot_age(snapshot)
    missing = [k for k in keys if k not in types.snapshot_rows]
    if missing:
        names = [_short(session, k) for k in missing]
        shown = ", ".join(names[:3]) + (", ..." if len(names) > 3 else "")
        covered = [_dataset_covered(session, k) for k in missing]
        extra = "; the snapshot lists no table of some of their datasets, so it may not cover them" if any(c is False for c in covered) else ""
        what = f"{names[0]} is not in the schema snapshot" if len(names) == 1 else f"{len(names)} tables are not in the schema snapshot: {shown}"
        notes.gaps.append((what, clip(f"{'it' if len(names) == 1 else 'they'} may not be built (or the snapshot is older){extra}; deployed_status table={names[0]}", 200)))
        notes.next.append(f"{names[0]} is not in the schema snapshot, so a query on it may fail: deployed_status table={names[0]} says whether it is built.")
    known: dict[str, set[str]] = {}
    outputs: dict[str, dict[str, str]] = {}
    for key in keys:
        if key in pipeline.models and key in types.snapshot_rows:
            repository, _ = repository_columns(session, key)
            if repository:
                outputs[key] = {n.casefold(): n for n in repository}
                known[key] = set(outputs[key]) | {c.casefold() for c in types.snapshot_columns.get(key, ())}
    if known:
        used = _used_columns(session, draft, known)
        for key, columns in outputs.items():
            deployed = {c.casefold() for c in types.snapshot_columns.get(key, ())}
            absent = sorted(outputs[key][c] for c in used.get(key, ()) if c in outputs[key] and c not in deployed)
            if absent:
                name = _short(session, key)
                what = f"{name}: {', '.join(absent[:3])}{', ...' if len(absent) > 3 else ''} used by the draft {'is' if len(absent) == 1 else 'are'} in the repository but not in the deployed table (snapshot{', ' + age if age else ''})"
                notes.findings.append({
                    "check": "deployed", "what": clip(what, 260),
                    "fix": clip(f"Build {name} so the table has {'it' if len(absent) == 1 else 'them'}, or refresh the snapshot if it was built since; deployed_status table={name} compares both.", 220)})
    if _old(seconds):
        notes.gaps.append((age, "tables may have changed since then; the column and table checks above are as old as the snapshot"))
    return notes
