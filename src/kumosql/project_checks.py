"""``project_checks``: what is wrong with the repository itself, before any warehouse is asked.

Checks that need only the loaded project (no BigQuery, no schema snapshot, no data):

* ``declared_and_built``: a name declared as a source (``declare``, ``type: "declaration"``) that the project also builds
  as a model. Dataform rejects the duplicate action name, and the warehouse table may come from either definition.
* ``disabled_read_by_enabled``: a model with ``disabled: true`` that an enabled model (or assertion) reads. Dataform compiles
  a disabled action but never runs it, so its table may never have been built, or is as old as the last time it was enabled.
* ``declared_in_several_datasets``: one table name declared in two or more datasets. A one-argument ``ref("name")`` to it is
  ambiguous; Dataform needs the dataset too.
* ``defined_twice``: two files that publish the same table (the loader kept the later one).

Identical declarations repeated are one table (the loader folds them), so they are not a finding. The same findings are one
line in ``describe_table`` for the table concerned and a note in ``check_query`` when the draft reads such a table.

What this cannot see is the warehouse: whether a table exists there, what its partitioning is, how old it is. Every answer
says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_session import Session

EXAMPLES = 3

#: check code -> (what it means, how to fix it)
CHECKS = {
    "declared_and_built": (
        "name is declared as a source and also built as a model",
        "Keep one: delete the declaration if the project builds the table, or delete the model if something outside the project writes it.",
    ),
    "disabled_read_by_enabled": (
        "disabled model is read by an enabled model",
        "Enable the model (remove disabled: true), or stop reading it; until then readers see a table that may never have been built.",
    ),
    "declared_in_several_datasets": (
        "table name is declared in several datasets",
        "Refer to it with the dataset too, ref(\"dataset\", \"name\"); a one-argument ref(\"name\") is ambiguous.",
    ),
    "defined_twice": (
        "two files publish the same table",
        "Give one of the files another name or schema; only one definition is kept.",
    ),
}


@dataclass(frozen=True)
class Finding:
    check: str
    #: the table the finding is about (a project key), or "" when it is about a name
    table: str
    #: one example line, as shown to the agent
    line: str
    #: other tables the finding concerns (the readers of a disabled model, the datasets of a declaration)
    related: tuple[str, ...] = ()


def short(key: str) -> str:
    return ".".join(key.split(".")[-2:])


def _builds_a_table(model) -> bool:
    return model.kind not in ("assertion",) and not (model.kind == "operations" and not model.has_output)


def findings(session: Session) -> list[Finding]:
    """Every static finding of the loaded project, in a fixed order (computed once per session)."""

    return session._once("project_findings", lambda: _find(session))


def _find(session: Session) -> list[Finding]:
    pipeline = session.pipeline()
    found: list[Finding] = []

    for key in sorted(pipeline.sources):
        model = pipeline.models.get(key)
        if model is not None and _builds_a_table(model):
            where = f" ({model.path})" if model.path else ""
            found.append(Finding("declared_and_built", key, f"{short(key)} is declared as a source and built as a {model.kind}{where}"))

    downstream = session.downstream()
    for key in sorted(pipeline.models):
        model = pipeline.models[key]
        if not model.disabled or not _builds_a_table(model):
            continue
        readers = sorted(r for r in downstream.get(key, ()) if r != key and r in pipeline.models and not pipeline.models[r].disabled)
        if readers:
            names = ", ".join(short(r) for r in readers[:EXAMPLES]) + (", ..." if len(readers) > EXAMPLES else "")
            found.append(Finding("disabled_read_by_enabled", key, f"{short(key)} is disabled and read by {len(readers)} enabled: {names}", tuple(readers)))

    by_name: dict[str, list[str]] = {}
    for key, target in pipeline.sources.items():
        by_name.setdefault(target.name, []).append(key)
    for name in sorted(by_name):
        keys = sorted(by_name[name])
        if len(keys) > 1 and name:
            datasets = ", ".join(sorted({k.split(".")[-2] for k in keys if k.count(".") >= 1}))
            found.append(Finding("declared_in_several_datasets", "", f"{name} is declared in {len(keys)} places ({clip(datasets, 80)})", tuple(keys)))

    for item in pipeline.diagnostics:
        if item.code == "duplicate_model":
            found.append(Finding("defined_twice", "", clip(f"{item.model} {item.message}", 150)))
    return found


# ------------------------------------------------------------ one table, one draft


def table_notes(session: Session, key: str) -> list[str]:
    """What the static checks say about table ``key``, one short phrase each (for ``describe_table``)."""

    notes: list[str] = []
    pipeline = session.pipeline()
    model = pipeline.models.get(key)
    for item in findings(session):
        if item.check == "declared_and_built" and item.table == key:
            notes.append("declared as a source and also built as a model: Dataform rejects the duplicate name, and the warehouse table may come from either")
        elif item.check == "disabled_read_by_enabled" and item.table == key:
            notes.append(f"disabled (disabled: true), so Dataform never runs it, yet {len(item.related)} enabled model{'s' if len(item.related) != 1 else ''} read it: the table may never have been built or may be stale")
        elif item.check == "disabled_read_by_enabled" and key in item.related:
            notes.append(f"reads the disabled model {short(item.table)}, which Dataform never runs: its table may be missing or stale")
        elif item.check == "declared_in_several_datasets" and key in item.related:
            notes.append(f"{model.target.name if model else key.split('.')[-1]} is declared in {len(item.related)} datasets: a one-argument ref() to it is ambiguous")
    return notes


def draft_notes(session: Session, keys: set[str]) -> list[tuple[str, str]]:
    """``(what, reason)`` gaps for ``check_query``: the tables of a draft that the repository alone cannot vouch for."""

    pipeline = session.pipeline()
    items = findings(session)
    groups: dict[str, list[str]] = {"disabled": [], "disabled_upstream": [], "both": [], "source": []}
    for key in sorted(keys):
        model = pipeline.models.get(key)
        if model is None:
            if key in pipeline.sources:
                groups["source"].append(key)
            continue
        if model.disabled:
            groups["disabled"].append(key)
        if any(i.check == "declared_and_built" and i.table == key for i in items):
            groups["both"].append(key)
        if any(i.check == "disabled_read_by_enabled" and key in i.related for i in items):
            groups["disabled_upstream"].append(key)

    def names(group: list[str]) -> str:
        return ", ".join(short(k) for k in group[:EXAMPLES]) + (", ..." if len(group) > EXAMPLES else "")

    gaps: list[tuple[str, str]] = []
    if groups["disabled"]:
        gaps.append((
            f"{len(groups['disabled'])} disabled model{'s' if len(groups['disabled']) != 1 else ''} read: {names(groups['disabled'])}",
            "disabled: true means Dataform never runs it, so the table may never have been built or may be stale; its columns here come from the repository, not the warehouse"))
    if groups["disabled_upstream"]:
        gaps.append((
            f"{len(groups['disabled_upstream'])} table{'s' if len(groups['disabled_upstream']) != 1 else ''} read from a disabled model: {names(groups['disabled_upstream'])}",
            "a model they read is never run by Dataform, so they may be built from an old or missing table"))
    if groups["both"]:
        gaps.append((
            f"{len(groups['both'])} table{'s' if len(groups['both']) != 1 else ''} both declared as a source and built as a model: {names(groups['both'])}",
            "Dataform rejects the duplicate name; which definition made the warehouse table is not known"))
    if groups["source"]:
        gaps.append((
            f"{len(groups['source'])} declared source{'s' if len(groups['source']) != 1 else ''} the project does not build: {names(groups['source'])}",
            "whether the table exists in the warehouse is not checked; its columns are only as good as --source-schema"))
    return gaps


# ------------------------------------------------------------------------- tool


@tool(
    "project_checks",
    "Check the project for repository problems",
    "What is wrong with the repository itself, before any warehouse is asked? Static checks on the loaded project: a name both "
    "declared as a source and built as a model, a disabled model that enabled models read, a table name declared in several "
    "datasets, two files that publish one table. Counts per check with three examples and the fix. It never reads the warehouse, "
    "so whether tables exist or are stale there is not checked.",
    {"check": {"type": "string", "enum": list(CHECKS), "description": "Only this check's findings (the examples then list as many as fit)."}},
)
def project_checks(session: Session, args: dict) -> Answer:
    wanted = args.get("check")
    items = findings(session)
    counts = {code: sum(1 for i in items if i.check == code) for code in CHECKS}
    shown = [code for code in CHECKS if counts[code] and (not wanted or code == wanted)]
    offset = offset_wanted(args)

    rows = []
    for code in shown:
        about = [i for i in items if i.check == code]
        limit = 20 if wanted else EXAMPLES
        rows.append({"check": code, "count": len(about), "what": CHECKS[code][0], "examples": [clip(i.line, 150) for i in about[:limit]], "fix": CHECKS[code][1]})
    total = sum(counts.values())

    answer = Answer("")
    answer.fields["checked"] = list(CHECKS)
    answer.counts = {"findings": total, **{code: n for code, n in counts.items() if n}}
    answer.add_list("findings", rows[offset:], total=len(rows), offset=offset, primary=True, keep=1)
    from . import column_meaning

    column_meaning.zone_unknown(answer, session)  # TIMESTAMP/DATETIME columns with no declared zone: unknown, not a finding
    answer.unknown("whether the deployed tables match the repository", "the warehouse is not read: a table that was never built, is older than its model or has a different partitioning is not visible here")
    if total:
        parts = ", ".join(f"{n} {CHECKS[code][0]}" for code, n in counts.items() if n)
        answer.summary = f"{total} problem{'s' if total != 1 else ''} in the repository, found without the warehouse: {parts}."
        if wanted and not counts[wanted]:
            answer.summary = f"No {wanted} problem found; the other checks found {total}."
        first = next(code for code in CHECKS if counts[code])
        answer.next(f"Fix the {first} group first ({CHECKS[first][1]}); describe_table table=... shows the line for one table.")
    else:
        answer.summary = f"No repository problems found by the {len(CHECKS)} static checks that ran. This says nothing about the warehouse."
        answer.next("check_query on a draft reads the same facts for the tables it uses; a schema snapshot or dry run is needed to compare with the warehouse.")
    if wanted and wanted not in CHECKS:
        raise ToolError(f"unknown check {clip(wanted, 40)!r}", [f"Checks: {', '.join(CHECKS)}."])
    return answer
