"""Scheduled-job SQL loaded beside a Dataform project, joined to it by the tables the jobs write.

During a move from scheduled BigQuery SQL jobs to Dataform, the new models usually reach the old tables through
declarations. Lineage inside the project stops at those declarations. This module loads the old jobs into the same
:class:`~kumosql.pipeline.Pipeline`, one model per step that writes a table, so ``trace_column`` on a Dataform column
continues into the job statement that wrote the declared table, and on through the jobs before it. Each hop that lies in a
job carries the job's id and the step (``TraceHop.job_id`` and ``TraceHop.step``).

Two inputs are read (``python -m kumosql pipeline-report PROJECT --job-sql PATH``; ``mcp`` and the UI's ``--project``
take the same option):

* **A job folder**: one SQL script per job and a manifest, ``jobs.yaml`` (or ``jobs.yml`` / ``jobs.json``)::

      defaults:                  # optional: what unqualified table names in the scripts mean
        project: shop-legacy
        dataset: sandbox
      jobs:
        - job_id: daily_orders
          script: daily_orders.sql
          steps:                 # optional: read from the script's own statements when left out
            - step: 1
              destination: sandbox.orders_clean
            - step: 2
              destination: orders_daily
              statement: 4       # optional: which statement of the script (1-based, counting every statement)
        - job_id: weekly_returns
          script: weekly_returns.sql
          destination: sandbox.returns_weekly   # a script that ends in a plain SELECT: where that SELECT writes

  ``project`` and ``dataset`` may also be set per job. Paths are relative to the manifest.
* **A job-history export** (``INFORMATION_SCHEMA.JOBS`` rows as JSON, JSON lines or CSV, read by
  :func:`kumosql.costs.load_jobs`): query text, destination table, statement type, ``parent_job_id``. The child jobs of
  a script are grouped under the script's job, in the order they ran. Runs of the same scheduled query (the same text,
  literal values aside, and the same destination) become one writer: the latest run's text is read and its job id is the
  writer's id. Dry runs and failed jobs are left out.

A step's own SQL is the statement that writes the table plus what it needs from earlier in the script: the temporary
tables it reads (followed through further temporary tables) and the ``DECLARE`` / ``SET`` statements. Statements that
write other real tables become their own steps; a later step reads them by table name, like any model. Unqualified table
names are qualified with the job's project and dataset first, so a bare ``orders`` in an old job never resolves to a new
Dataform model of the same name.

Joining: a step's destination is matched to the project's tables with partial names allowed on either side
(``sandbox.orders_daily`` finds the declaration ``shop-legacy.sandbox.orders_daily``). A declared table written by
exactly one loaded step resolves to that step. Gaps in :meth:`Pipeline.completeness`:

* ``job_writer_ambiguous``: more than one loaded writer (two jobs, two statements of one job, or a job and a Dataform
  model) writes the table; the writers are named and lineage stops at the table.
* ``job_writer_missing``: a declared table in a dataset the loaded jobs write (the shared sandbox) that no loaded job
  writes. Declarations in other datasets (raw sources) are not gaps.
* ``job_destination_ambiguous``: a step's destination matches several tables of the project.

Nothing here contacts BigQuery or writes anything.
"""

from __future__ import annotations

import csv
import difflib
import functools
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

from sqlglot import exp

from .paths import posix_path
from .pipeline_types import Model, PipelineDiagnostic, Target

if TYPE_CHECKING:
    from .pipeline import Pipeline

MANIFEST_NAMES = ("jobs.yaml", "jobs.yml", "jobs.json")
MAX_MANIFEST_BYTES = 1_000_000
MAX_SCRIPT_BYTES = 2_000_000
MAX_JOBS = 5_000
MAX_PROBLEMS = 5
_JOB_KEYS = ("job_id", "script", "project", "dataset", "destination", "steps")
_STEP_KEYS = ("step", "destination", "statement")
_TOP_KEYS = ("defaults", "jobs")
_DEFAULT_KEYS = ("project", "dataset")
#: statement kinds (``kumosql.scripts``) that write values into a table's columns; DELETE, TRUNCATE and DROP do not
_VALUE_WRITES = {"create_table", "create_view", "insert", "merge", "update", "clone"}
_CONTEXT = {"declare", "set"}
_OPAQUE = {"call", "execute", "other"}


class JobSqlError(ValueError):
    """The job folder or job history cannot be used; the message names what is wrong and where, in a few lines."""


# ------------------------------------------------------------------ what is loaded


@dataclass(frozen=True)
class ManifestStep:
    """One step a manifest names: its number, the table it writes, and optionally which statement it is."""

    step: int
    destination: str
    statement: int | None = None


@dataclass(frozen=True)
class JobScript:
    """One job: its script and what unqualified names in it mean."""

    job_id: str
    text: str
    path: str | None = None
    project: str = ""
    dataset: str = ""
    #: where a final plain SELECT writes (a scheduled query's destination table)
    destination: str = ""
    #: ``None``: the steps are read from the script's own statements
    steps: tuple[ManifestStep, ...] | None = None
    origin: str = "folder"
    runs: int = 1
    last_run: str | None = None


@dataclass(frozen=True)
class JobStep:
    """One statement of a job that writes a table, ready to be a model."""

    job_id: str
    step: int
    statement: int
    destination: tuple[str, ...]
    sql: str
    kind: str
    path: str | None
    origin: str
    runs: int = 1

    @property
    def label(self) -> str:
        return f"job {self.job_id} step {self.step}"


@dataclass
class JobSet:
    """The jobs read from one ``--job-sql`` input."""

    source: str
    scripts: list[JobScript] = field(default_factory=list)
    #: counts of what reading left out (dry runs, failed jobs, rows without query text, runs folded into one writer)
    summary: dict[str, int] = field(default_factory=dict)
    #: the files this input was read from (the manifest and its scripts, or the history file), for a watcher
    paths: list[Path] = field(default_factory=list)


class _Problems:
    def __init__(self) -> None:
        self.found: list[str] = []

    def add(self, text: str) -> None:
        self.found.append(text)

    def raise_if_any(self, where: str) -> None:
        if not self.found:
            return
        shown = self.found[:MAX_PROBLEMS]
        more = f" (and {len(self.found) - len(shown)} more)" if len(self.found) > len(shown) else ""
        raise JobSqlError(f"{where}: " + "; ".join(shown) + more)


# ------------------------------------------------------------------ reading inputs


def load_job_sql(path: str | Path) -> JobSet:
    """Read a job folder (a folder with ``jobs.yaml``, or the manifest itself) or a job-history export.

    A ``.json`` file is a manifest when it is a mapping with ``jobs`` and a job history when it is a list. Raises
    :class:`JobSqlError` with a bounded message for anything that cannot be used.
    """

    path = Path(path)
    if path.is_dir():
        for name in MANIFEST_NAMES:
            if (path / name).is_file():
                return read_manifest(path / name)
        raise JobSqlError(f"job folder {path.name}: no manifest ({', '.join(MANIFEST_NAMES)}) was found in it")
    if not path.is_file():
        raise JobSqlError(f"job SQL input {path.name} was not found")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return read_manifest(path)
    if suffix == ".json":
        try:
            data = json.loads(_read_text(path, f"job SQL input {path.name}", MAX_MANIFEST_BYTES))
        except (JobSqlError, ValueError):
            data = None  # too large for a manifest, or JSON lines: a job history
        return read_manifest(path) if isinstance(data, dict) and "jobs" in data else read_job_history(path)
    if suffix in (".jsonl", ".ndjson", ".csv"):
        return read_job_history(path)
    raise JobSqlError(f"job SQL input {path.name}: give a job folder, a jobs.yaml manifest or a job-history export (.json, .jsonl, .csv)")


def _read_text(path: Path, what: str, limit: int) -> str:
    try:
        if path.stat().st_size > limit:
            raise JobSqlError(f"{what} is larger than {limit // 1_000_000} MB")
        return path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise JobSqlError(f"{what} is not UTF-8 text") from exc
    except OSError as exc:
        raise JobSqlError(f"{what} cannot be read ({type(exc).__name__})") from exc


def _text(value: object, what: str, problems: _Problems, *, required: bool = True, limit: int = 300) -> str:
    if value is None or value == "":
        if required:
            problems.add(f"{what} is missing")
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        problems.add(f"{what} must be text")
        return ""
    text = str(value).strip()
    if len(text) > limit:
        problems.add(f"{what} is longer than {limit} characters")
        return ""
    return text


def _number(value: object, what: str, problems: _Problems) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        problems.add(f"{what} must be a whole number from 1")
        return None
    return value


def _keys(entry: dict, allowed: tuple[str, ...], what: str, problems: _Problems) -> None:
    for key in entry:
        if key not in allowed:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f"; did you mean {close[0]}?" if close else f"; allowed: {', '.join(allowed)}"
            problems.add(f"{what} has an unknown key {str(key)[:40]!r}{hint}")


_TABLE_NAME = re.compile(r"^`?[A-Za-z0-9_\-:$*]+(?:`?\.`?[A-Za-z0-9_\-$*]+){0,2}`?$")


def _table_text(value: str, what: str, problems: _Problems) -> str:
    if value and not _TABLE_NAME.match(value):
        problems.add(f"{what} {value[:80]!r} is not a table name (table, dataset.table or project.dataset.table)")
        return ""
    return value


def read_manifest(path: str | Path) -> JobSet:
    """The jobs a manifest names, each script read and checked: its steps must be found in its statements."""

    path = Path(path)
    where = f"job manifest {path.name}"
    raw = _read_text(path, where, MAX_MANIFEST_BYTES)
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            import yaml

            data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - json and yaml raise their own families
        reason = str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__
        raise JobSqlError(f"{where} is not valid YAML or JSON: {reason}") from exc
    if not isinstance(data, dict):
        raise JobSqlError(f"{where} must be a mapping with a `jobs` list")
    problems = _Problems()
    _keys(data, _TOP_KEYS, where, problems)
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        problems.add("defaults must be a mapping with project and dataset")
        defaults = {}
    _keys(defaults, _DEFAULT_KEYS, "defaults", problems)
    default_project = _text(defaults.get("project"), "defaults.project", problems, required=False)
    default_dataset = _text(defaults.get("dataset"), "defaults.dataset", problems, required=False)
    entries = data.get("jobs")
    if not isinstance(entries, list) or not entries:
        problems.add("`jobs` must be a non-empty list")
        entries = []
    if len(entries) > MAX_JOBS:
        problems.add(f"it lists more than {MAX_JOBS} jobs")
        entries = entries[:MAX_JOBS]
    base = path.resolve().parent
    scripts: list[JobScript] = []
    files: list[Path] = [path]
    seen: set[str] = set()
    for index, entry in enumerate(entries, 1):
        label = f"jobs[{index}]"
        if not isinstance(entry, dict):
            problems.add(f"{label} must be a mapping")
            continue
        _keys(entry, _JOB_KEYS, label, problems)
        job_id = _text(entry.get("job_id"), f"{label} job_id", problems)
        if job_id:
            label = f"job {job_id}"
            if job_id.casefold() in seen:
                problems.add(f"{label} is listed twice")
            seen.add(job_id.casefold())
        relative = _text(entry.get("script"), f"{label} script", problems)
        project = _text(entry.get("project"), f"{label} project", problems, required=False) or default_project
        dataset = _text(entry.get("dataset"), f"{label} dataset", problems, required=False) or default_dataset
        destination = _table_text(_text(entry.get("destination"), f"{label} destination", problems, required=False), f"{label} destination", problems)
        steps = _manifest_steps(entry.get("steps"), label, problems)
        text = None
        if relative:
            if relative.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", relative):
                problems.add(f"{label} script must be relative to the manifest, not absolute")
            else:
                script_path = base / relative
                if not script_path.is_file():
                    problems.add(f"{label} script {relative} is not a file")
                else:
                    try:
                        text = _read_text(script_path, f"{label} script {relative}", MAX_SCRIPT_BYTES)
                    except JobSqlError as exc:
                        problems.add(str(exc))
                    else:
                        files.append(script_path)
                        if not text.strip():
                            problems.add(f"{label} script {relative} is empty")
                            text = None
        if job_id and text is not None:
            script = JobScript(job_id, text, posix_path(relative), project, dataset, destination, steps, "folder")
            for problem in _step_problems(script):
                problems.add(f"{label}: {problem}")
            scripts.append(script)
    problems.raise_if_any(where)
    return JobSet(f"{path.parent.name}/{path.name}", scripts, paths=files)


def _manifest_steps(value: object, label: str, problems: _Problems) -> tuple[ManifestStep, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        problems.add(f"{label} steps must be a non-empty list (or left out, to read them from the script)")
        return None
    steps: list[ManifestStep] = []
    numbers: set[int] = set()
    for position, item in enumerate(value, 1):
        what = f"{label} steps[{position}]"
        if not isinstance(item, dict):
            problems.add(f"{what} must be a mapping with step and destination")
            continue
        _keys(item, _STEP_KEYS, what, problems)
        number = _number(item.get("step"), f"{what} step", problems) if "step" in item else position
        statement = _number(item.get("statement"), f"{what} statement", problems)
        destination = _table_text(_text(item.get("destination"), f"{what} destination", problems, required=statement is None), f"{what} destination", problems)
        if number is None:
            continue
        if number in numbers:
            problems.add(f"{label} step {number} is listed twice")
        numbers.add(number)
        steps.append(ManifestStep(number, destination, statement))
    return tuple(steps)


def read_job_history(path: str | Path) -> JobSet:
    """Job-history rows (query text plus destination table) as jobs: scripts grouped, repeated runs folded into one writer."""

    from .costs import _is_temporary, load_jobs

    path = Path(path)
    where = f"job history {path.name}"
    try:
        jobs = load_jobs(path)
    except (OSError, UnicodeError) as exc:
        raise JobSqlError(f"{where} cannot be read ({type(exc).__name__})") from exc
    except (ValueError, csv.Error, TypeError, AttributeError) as exc:
        raise JobSqlError(f"{where} is not a job-history export (JSON, JSON lines or CSV): {str(exc).splitlines()[0][:120] if str(exc) else type(exc).__name__}") from exc
    summary = {"rows": len(jobs), "dry_runs": 0, "failed": 0, "no_query_text": 0, "scripts": 0, "runs_folded": 0}
    usable = []
    for job in jobs:
        if job.dry_run:
            summary["dry_runs"] += 1
        elif job.error_result:
            summary["failed"] += 1  # a failed run wrote nothing
        else:
            usable.append(job)
    children: dict[str, list] = {}
    for job in usable:
        if job.parent_job_id:
            children.setdefault(job.parent_job_id, []).append(job)
    runs: list[tuple[str, str, str, str, str | None]] = []  # (job id, text, project, destination, creation time)
    parents = {job.job_id: job for job in usable if job.job_id in children}
    for parent_id, kids in children.items():
        parent = parents.get(parent_id)
        ordered = sorted(kids, key=lambda job: (job.creation_time or "", job.job_id))
        text = parent.query if parent is not None and parent.query.strip() else ";\n".join(
            job.query.strip().rstrip(";") for job in ordered if job.query.strip()
        )
        if not text.strip():
            summary["no_query_text"] += 1
            continue
        summary["scripts"] += 1
        times = [job.creation_time for job in (parent, *kids) if job is not None and job.creation_time]
        project = (parent.project if parent is not None else "") or next((job.project for job in kids if job.project), "")
        runs.append((parent_id, text, project, "", max(times) if times else None))
    for job in usable:
        if job.parent_job_id or job.job_id in children:
            continue
        if not job.query.strip():
            summary["no_query_text"] += 1
            continue
        destination = ""
        if job.destination_table and not _is_temporary(job.destination_table):
            from .identity import normalize_table_reference

            identity = normalize_table_reference(job.destination_table)
            destination = ".".join(identity.parts) if identity is not None and identity.kind == "table" else ""
        if job.statement_type == "SCRIPT":
            summary["scripts"] += 1
        runs.append((job.job_id or f"row{len(runs) + 1}", job.query, job.project, destination, job.creation_time))
    groups: dict[tuple[str, str], list[tuple[str, str, str, str, str | None]]] = {}
    for run in runs:
        groups.setdefault((_fingerprint(run[1]), run[3].casefold()), []).append(run)
    scripts: list[JobScript] = []
    for members in groups.values():
        latest = max(members, key=lambda run: (run[4] or "", run[0]))
        summary["runs_folded"] += len(members) - 1
        job_id, text, project, destination, when = latest
        scripts.append(JobScript(job_id, text, None, project, "", destination, None, "history", len(members), when))
    scripts.sort(key=lambda script: script.job_id)
    return JobSet(path.name, scripts, summary, [path])


def _fingerprint(text: str) -> str:
    """The query text with literal values, layout and letter case set aside: runs of one scheduled query share it."""

    from .scripts import lex

    tokens = ["?" if token.kind in ("s", "n") else token.text.upper() for token in lex(text)]
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()[:20]


# ------------------------------------------------------------------ the statements of a script


@dataclass
class _Leaf:
    number: int
    start: int
    end: int
    text: str
    conditional: bool
    in_procedure: bool
    kind: str = ""
    #: real tables it writes values into, as spelled (parts)
    writes: tuple[tuple[str, ...], ...] = ()
    #: real tables it changes without writing values (DELETE, TRUNCATE, DROP, a CREATE with only a schema)
    touches: tuple[tuple[str, ...], ...] = ()
    temp_writes: frozenset[str] = frozenset()
    temp_reads: frozenset[str] = frozenset()
    query: bool = False


@dataclass
class _Block:
    """A control structure (``BEGIN``, ``IF``, a loop, ``CASE``) and what is inside it."""

    start: int
    end: int
    parent: int | None
    #: numbers of the statements inside it, at any depth
    leaves: set[int] = field(default_factory=set)
    #: where its conditions (and a FOR loop's query) are written: ``(start, end)`` in the script
    conditions: list[tuple[int, int]] = field(default_factory=list)
    temp_reads: frozenset[str] = frozenset()
    #: it holds a procedure definition, which a CALL elsewhere may need
    defines: bool = False


@dataclass
class _Script:
    leaves: list[_Leaf]
    blocks: list[_Block]
    temps: frozenset[str]


def _is_temp_table(table: exp.Table, temps: frozenset[str]) -> bool:
    if table.catalog:
        return False
    if table.db:
        return table.db.casefold() == "_session"
    return table.name.casefold() in temps


def _parts_of(table: exp.Table) -> tuple[str, ...]:
    return tuple(part for part in (table.catalog, table.db, table.name) if part)


def _leaves(text: str) -> _Script:
    """Every statement of the script, numbered in order and classified (a procedure's own statements are marked), and
    the blocks around them."""

    from .scripts import analyse_script, parse_script, token_reads

    leaves: list[_Leaf] = []
    blocks: list[_Block] = []
    condition_texts: dict[int, list[str]] = {}

    def walk(nodes, conditional: bool, in_procedure: bool, inside: list[int]) -> None:
        for node in nodes:
            if node.kind == "stmt":
                leaves.append(_Leaf(len(leaves) + 1, node.start, node.end, node.header, conditional, in_procedure))
                for index in inside:
                    blocks[index].leaves.add(len(leaves))
                continue
            here = inside
            if node.kind == "procedure":
                for index in inside:
                    blocks[index].defines = True
            elif not in_procedure:
                block = _Block(node.start, node.end, inside[-1] if inside else None)
                written = [*node.conditions, *([node.query] if node.query else [])]
                search = node.start
                for condition in written:
                    found = text.find(condition, search, node.end) if condition else -1
                    if found >= 0:
                        block.conditions.append((found, found + len(condition)))
                        search = found + len(condition)
                blocks.append(block)
                condition_texts[len(blocks) - 1] = written
                here = [*inside, len(blocks) - 1]
            for index, branch in enumerate(node.branches):
                gated = conditional or node.kind in {"if", "while", "for", "repeat", "case", "loop"} or (
                    node.kind in {"begin", "procedure"} and index > 0
                )
                walk(branch, gated, in_procedure or node.kind == "procedure", here)

    walk(parse_script(text), False, False, [])
    whole = analyse_script(text)
    temps = frozenset(name.casefold() for name in whole.temp_names)
    for index, written in condition_texts.items():
        names: set[str] = set()
        for condition in written:
            try:
                reads, _writes = token_reads(f"SELECT {condition}")
            except Exception:  # noqa: BLE001 - a condition that cannot be read adds nothing
                continue
            names.update(t.name.casefold() for t in reads if _is_temp_table(t, temps))
        blocks[index].temp_reads = frozenset(names)
    for leaf in leaves:
        if leaf.in_procedure:
            leaf.kind = "procedure"
            continue
        alone = analyse_script(leaf.text)
        first = alone.statements[0] if alone.statements else None
        leaf.kind = first.kind if first is not None else "other"
        temp_writes = {name.casefold() for name in alone.temp_names}
        writes: list[tuple[str, ...]] = []
        touches: list[tuple[str, ...]] = []
        for write in alone.writes:
            if _is_temp_table(write.table, temps):
                temp_writes.add(write.table.name.casefold())
                continue
            parts = _parts_of(write.table)
            values = leaf.kind in _VALUE_WRITES and not (first is not None and first.complete) and not (
                leaf.kind == "create_table" and first is not None and first.reason == "no source query"
            )
            (writes if values else touches).append(parts)
        leaf.writes = tuple(dict.fromkeys(writes))
        leaf.touches = tuple(dict.fromkeys(touches))
        leaf.temp_writes = frozenset(temp_writes)
        reads = [*alone.all_reads(), *alone.side_reads.values()]
        leaf.temp_reads = frozenset(t.name.casefold() for t in reads if _is_temp_table(t, temps))
        leaf.query = leaf.kind == "select" and not writes and not touches
    return _Script(leaves, blocks, temps)


def _same_table(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Whether two spellings name the same table: the shorter one is the end of the longer one (case aside)."""

    short, long = sorted((tuple(p.casefold() for p in left), tuple(p.casefold() for p in right)), key=len)
    return bool(short) and long[len(long) - len(short):] == short


def _qualified(parts: Sequence[str], project: str, dataset: str) -> tuple[str, ...]:
    parts = tuple(part.strip("`") for part in parts if part)
    if len(parts) == 1 and len(parts[0].split(".")) > 1:
        parts = tuple(parts[0].split("."))
    if len(parts) == 2 and ":" in parts[0]:  # legacy ``project:dataset.table``
        project_part, dataset_part = parts[0].split(":", 1)
        parts = (project_part, dataset_part, parts[1])
    if len(parts) == 1 and dataset:
        parts = (dataset, parts[0])
    if len(parts) == 2 and project:
        parts = (project, *parts)
    return parts


def _split_name(name: str) -> tuple[str, ...]:
    return tuple(part for part in name.replace("`", "").split(".") if part)


def _step_problems(script: JobScript) -> list[str]:
    """Why the script's steps cannot be found, for a manifest (an empty list when they all can)."""

    _steps, problems = script_steps(script)
    return problems


def script_steps(script: JobScript) -> tuple[list[JobStep], list[str]]:
    """The steps of one job, each with its own SQL; and problems (a step that names no statement, a script with none)."""

    steps, problems = _script_steps(script)
    return list(steps), list(problems)


@functools.lru_cache(maxsize=4096)
def _script_steps(script: JobScript) -> tuple[tuple[JobStep, ...], tuple[str, ...]]:
    # A pure function of the script: checking a manifest and joining it read each script once, not twice.
    parsed = _leaves(script.text)
    leaves = parsed.leaves
    top = [leaf for leaf in leaves if not leaf.in_procedure]
    problems: list[str] = []
    chosen: list[tuple[int, _Leaf, tuple[str, ...]]] = []  # (step number, statement, destination as written)
    final_query = next((leaf for leaf in reversed(top) if not leaf.conditional), None)
    final_query = final_query if final_query is not None and final_query.query else None
    if script.steps is None:
        number = 0
        for leaf in top:
            for written in leaf.writes:
                number += 1
                chosen.append((number, leaf, written))
        if script.destination and final_query is not None:
            chosen.append((number + 1, final_query, _split_name(script.destination)))
        if not chosen:
            problems.append(
                "the script writes no table; give `destination` (where its final SELECT writes) or list its `steps`"
                if final_query is not None
                else "the script writes no table, so it has no step to load"
            )
    else:
        for item in script.steps:
            destination = _split_name(item.destination) if item.destination else ()
            wanted = _qualified(destination, script.project, script.dataset) if destination else ()
            if item.statement is not None:
                if item.statement > len(leaves):
                    problems.append(f"step {item.step}: statement {item.statement} does not exist (the script has {len(leaves)})")
                    continue
                leaf = leaves[item.statement - 1]
                if leaf.in_procedure:
                    problems.append(f"step {item.step}: statement {item.statement} is inside a procedure definition")
                    continue
                if leaf.writes:
                    match = next((w for w in leaf.writes if not wanted or _same_table(_qualified(w, script.project, script.dataset), wanted)), None)
                    if match is None:
                        problems.append(f"step {item.step}: statement {item.statement} writes {'.'.join(leaf.writes[0])}, not {item.destination}")
                        continue
                    chosen.append((item.step, leaf, destination or match))
                elif leaf.query and destination:
                    chosen.append((item.step, leaf, destination))
                elif leaf.query:
                    problems.append(f"step {item.step}: statement {item.statement} is a SELECT; give the destination it writes")
                else:
                    problems.append(f"step {item.step}: statement {item.statement} writes no table")
                continue
            matches = [leaf for leaf in top for w in leaf.writes if _same_table(_qualified(w, script.project, script.dataset), wanted)]
            if len(matches) == 1:
                chosen.append((item.step, matches[0], destination))
            elif len(matches) > 1:
                numbers = ", ".join(str(leaf.number) for leaf in matches[:6])
                problems.append(f"step {item.step}: {len(matches)} statements write {item.destination} (statements {numbers}); give `statement`")
            elif final_query is not None and not any(final_query is leaf for _, leaf, _d in chosen):
                chosen.append((item.step, final_query, destination))
            else:
                problems.append(f"step {item.step}: no statement of the script writes {item.destination}")
    steps: list[JobStep] = []
    for number, leaf, destination in chosen:
        parts = _qualified(destination, script.project, script.dataset)
        if len(parts) > 3 or not parts:
            problems.append(f"step {number}: {'.'.join(destination)} is not a table name")
            continue
        sql = _step_sql(script, parsed, leaf)
        kind = "view" if leaf.kind == "create_view" else "sql"
        steps.append(JobStep(script.job_id, number, leaf.number, parts, sql, kind, script.path, script.origin, script.runs))
    return tuple(steps), tuple(problems)


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _step_sql(script: JobScript, parsed: _Script, step: _Leaf) -> str:
    """The script with every statement the step does not need blanked out (lines kept), and its table names qualified.

    Kept before the step: ``DECLARE`` / ``SET``, the statements that build the temporary tables it reads (followed
    through further temporary tables and through the conditions of the blocks that stay), procedure definitions (as
    written: they run only when called), and statements that cannot be read (``EXECUTE IMMEDIATE``, an unknown
    ``CALL``), which might build what it reads. Everything after it is blanked, and so is a block left with nothing
    kept inside it.
    """

    leaves, blocks = parsed.leaves, parsed.blocks
    before = [leaf for leaf in leaves if leaf.number < step.number and not leaf.in_procedure]
    keep: set[int] = {step.number} | {
        leaf.number for leaf in before if leaf.kind in _CONTEXT or (leaf.kind in _OPAQUE and not leaf.writes)
    }
    needed = set(step.temp_reads)
    kept_blocks: set[int] = set()
    changed = True
    while changed:
        changed = False
        for leaf in before:
            if leaf.number not in keep and leaf.temp_writes & needed and not leaf.writes:
                keep.add(leaf.number)
                needed |= leaf.temp_reads
                changed = True
        for index, block in enumerate(blocks):
            if index not in kept_blocks and (block.leaves & keep or block.defines):
                kept_blocks.add(index)
                if block.temp_reads - needed:
                    needed |= block.temp_reads  # a condition that stays reads these temporary tables too
                    changed = True
    edits: list[tuple[int, int, str]] = []
    blanked: list[tuple[int, int]] = []
    for index, block in enumerate(blocks):
        if index in kept_blocks or (block.parent is not None and block.parent not in kept_blocks):
            continue  # kept, or inside a block that is blanked whole
        blanked.append((block.start, block.end))
        edits.append((block.start, block.end, _blank(script.text[block.start:block.end])))

    def inside_blanked(position: int) -> bool:
        return any(start <= position < end for start, end in blanked)

    for index in kept_blocks:
        for start, end in blocks[index].conditions:
            qualified = _qualify_statement(script.text[start:end], script.project, script.dataset, parsed.temps)
            edits.append((start, end, qualified))
    for leaf in leaves:
        if leaf.in_procedure or inside_blanked(leaf.start):
            continue
        original = script.text[leaf.start:leaf.end]
        if leaf.number in keep:
            edits.append((leaf.start, leaf.end, _qualify_statement(original, script.project, script.dataset, parsed.temps)))
        else:
            edits.append((leaf.start, leaf.end, _blank(original)))
    pieces: list[str] = []
    position = 0
    for start, end, replacement in sorted(edits):
        if start < position:
            continue  # inside a span already replaced
        pieces.append(script.text[position:start])
        pieces.append(replacement)
        position = end
    pieces.append(script.text[position:])
    return "".join(pieces)


def _qualify_statement(statement: str, project: str, dataset: str, temps: frozenset[str]) -> str:
    """``statement`` with partial table names completed by the job's project and dataset; as written when nothing changes.

    Temporary tables, CTEs and table functions are left alone. The rewritten text keeps the statement's line count, so a
    location in a message still points at the right line of the script. A block's condition or a FOR loop's query is
    qualified the same way.
    """

    if not (project or dataset):
        return statement
    import sqlglot

    from kumo_sql_prover.ast_utils import binding_cte, is_function_table, quiet_parser

    try:
        with quiet_parser():
            tree = sqlglot.parse_one(statement, read="bigquery")
    except Exception:  # noqa: BLE001 - a statement sqlglot cannot read stays as written
        return statement
    if tree is None:
        return statement
    changed = False
    for table in list(tree.find_all(exp.Table)):
        if not table.name or is_function_table(table) or binding_cte(table) is not None:
            continue
        if _is_temp_table(table, temps) or "INFORMATION_SCHEMA" in (table.db or "").upper() or table.name.upper() == "INFORMATION_SCHEMA":
            continue
        if not table.db and dataset:
            table.set("db", exp.to_identifier(dataset))
            changed = True
        if table.db and not table.catalog and project:
            table.set("catalog", exp.to_identifier(project))
            changed = True
    if not changed:
        return statement
    try:
        rewritten = tree.sql(dialect="bigquery")
        with quiet_parser():
            sqlglot.parse_one(rewritten, read="bigquery")
    except Exception:  # noqa: BLE001 - keep what the job wrote rather than a text that might not parse
        return statement
    return rewritten + "\n" * statement.count("\n")


# ------------------------------------------------------------------ joining the jobs to a project


def _dataset_of(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part.casefold() for part in parts[:-1])


def _same_dataset(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return False
    short, long = sorted((left, right), key=len)
    return long[len(long) - len(short):] == short


def with_jobs(pipeline: "Pipeline", jobs: Iterable[JobSet]) -> "Pipeline":
    """``pipeline`` with every loaded job step that writes a table added as a model, and the writer gaps as diagnostics.

    A declared table one step writes becomes that step's model, so lineage continues through it. A table with more than
    one loaded writer, or a declaration in a dataset the jobs write that no loaded job writes, is a gap (see the module).
    Returns a new pipeline; ``pipeline`` is not changed.
    """

    from .pipeline_loading import _TargetResolver

    sets = list(jobs)
    steps: list[JobStep] = []
    diagnostics: list[PipelineDiagnostic] = []
    for job_set in sets:
        for script in job_set.scripts:
            found, problems = script_steps(script)
            steps.extend(found)
            for problem in problems:
                diagnostics.append(PipelineDiagnostic(f"job {script.job_id}", "job_sql_unread", f"{problem}; nothing of it was loaded"))
    if not steps and not diagnostics:
        return pipeline
    project_keys = [*(model.key for model in pipeline.models.values() if model.key), *pipeline.sources]
    resolver = _TargetResolver(project_keys)
    writers: dict[str, list[JobStep]] = {}
    for step in steps:
        name = ".".join(step.destination)
        key = resolver.resolve(name)
        if key is None and resolver.is_ambiguous(name):
            close = sorted(k for k in project_keys if _same_table(_split_name(k), step.destination))[:4]
            diagnostics.append(PipelineDiagnostic(
                name, "job_destination_ambiguous",
                f"{step.label} writes {name}, which matches {len(close)} tables of the project ({', '.join(close)}); "
                "it was not joined to any (qualify the destination in the manifest, or set the job's project and dataset)"))
            continue
        writers.setdefault(key or name, []).append(step)
    # A partial destination no project table has, and a qualified one that ends the same way, are one table.
    full = _TargetResolver([key for key in writers if key not in pipeline.models and key not in pipeline.sources and key.count(".") == 2])
    for key in [k for k in writers if k.count(".") < 2 and k not in pipeline.models and k not in pipeline.sources]:
        other = full.resolve(key)
        if other is not None and other != key:
            writers[other].extend(writers.pop(key))
    models = dict(pipeline.models)
    added = 0
    for key, found in sorted(writers.items()):
        if key in pipeline.models:
            names = "; ".join(step.label for step in found[:4])
            diagnostics.append(PipelineDiagnostic(
                key, "job_writer_ambiguous",
                f"written by the project's own model and by {len(found)} loaded job step(s) ({names}); the model was kept"))
            continue
        if len(found) > 1:
            names = "; ".join(step.label for step in found[:6])
            more = f" and {len(found) - 6} more" if len(found) > 6 else ""
            diagnostics.append(PipelineDiagnostic(
                key, "job_writer_ambiguous",
                f"written by {len(found)} loaded writers ({names}{more}); lineage stops at this table until only one is loaded"))
            continue
        step = found[0]
        parts = _split_name(key)
        target = pipeline.sources.get(key) or Target(*(("",) * (3 - len(parts)) + parts))
        models[key] = Model(target, step.kind, step.sql, path=step.path, job_id=step.job_id, job_step=step.step)
        added += 1
    written = [_dataset_of(_split_name(key)) for key in writers]
    for key, target in sorted(pipeline.sources.items()):
        if key in writers or key in pipeline.models:
            continue
        dataset = _dataset_of(_split_name(key))
        if any(_same_dataset(dataset, other) for other in written):
            diagnostics.append(PipelineDiagnostic(
                key, "job_writer_missing",
                f"declared in {'.'.join(_split_name(key)[:-1])}, which the loaded jobs write, but no loaded job writes it; "
                "lineage stops at this table (load the job that builds it)"))
    note = f"loaded {added} job step(s) from {len(sets)} input(s): " + ", ".join(job_set.source for job_set in sets)
    diagnostics.append(PipelineDiagnostic("", "job_sql_loaded", note))
    return replace(pipeline, models=models, diagnostics=[*pipeline.diagnostics, *diagnostics])


def load_into(pipeline: "Pipeline", paths: Sequence[str | Path]) -> "Pipeline":
    """Read every ``--job-sql`` input and join them to ``pipeline`` (raises :class:`JobSqlError` for an unusable input)."""

    if not paths:
        return pipeline
    return with_jobs(pipeline, [load_job_sql(path) for path in paths])


def job_files(paths: Sequence[str | Path]) -> list[Path]:
    """The files the ``--job-sql`` inputs are read from (the manifest, its scripts, the history file), for a file watcher.

    Only lists them: a watcher calls this often, so no script is read and nothing is checked.
    """

    found: list[Path] = []
    for path in map(Path, paths):
        manifest = next((path / name for name in MANIFEST_NAMES if (path / name).is_file()), None) if path.is_dir() else path
        if manifest is None:
            continue
        found.append(manifest)
        if manifest.suffix.lower() not in (".yaml", ".yml", ".json") or not manifest.is_file():
            continue
        try:
            raw = manifest.read_text(encoding="utf-8-sig")
            if manifest.suffix.lower() == ".json":
                data = json.loads(raw)
            else:
                import yaml

                data = yaml.safe_load(raw)
        except Exception:  # noqa: BLE001 - a job history (JSON lines), or a manifest being edited: the file itself is watched
            continue
        entries = data.get("jobs") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else ():
            script = entry.get("script") if isinstance(entry, dict) else None
            if isinstance(script, str) and script.strip():
                found.append(manifest.parent / script.strip())
    return found


__all__ = [
    "JobScript",
    "JobSet",
    "JobSqlError",
    "JobStep",
    "ManifestStep",
    "job_files",
    "load_into",
    "load_job_sql",
    "read_job_history",
    "read_manifest",
    "script_steps",
    "with_jobs",
]
