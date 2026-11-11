"""The project loaded into the UI server and the payload ``/api/graph`` serves.

The query graph page reads one JSON shape (see ``docs/ui-roadmap.md``). This
module builds it from a real :class:`~kumosql.pipeline.Pipeline` and keeps the
currently loaded project, its job history and the last change comparison in
memory. With nothing loaded the views answer with an empty state that says what
to load; no sample data is served.

Every payload carries the pipeline's completeness block (issue #28): ``gaps``
lists what could not be analyzed and ``coverage.complete`` is false while any
blocking gap exists, so a partial graph is never presented as complete.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable
import csv
import io
import json
import os
import shutil
import tempfile
import hashlib
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from . import console
from .pipeline import Pipeline, load_sqlx_project
from .resilience import extended_path, is_windows_device_name
from .scripts import expand_script_jobs
from .timing import stage

MAX_FILES = 20_000  # real enterprise Dataform repositories have thousands of files
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CONFIG_FILES = ("workflow_settings.yaml", "workflow_settings.yml", "dataform.json")
_ALLOWED_SUFFIXES = (".sqlx", ".sql", ".js")

MAX_JOB_RECORDS = 200_000
NOT_LOADED = "load a project first"

_LOCK = threading.Lock()
_LOADED: dict | None = None
_JOBS: dict = {"records": (), "label": "", "restored_from": None, "scripts": None}


class ProjectError(ValueError):
    """The supplied project files could not be loaded."""


# Projects already analysed, by content: reloading the same commit reuses the whole analysis.
_PROJECT_CACHE: "OrderedDict[str, Pipeline]" = OrderedDict()
_PROJECT_CACHE_SIZE = 3
_ACTIVITY: dict[int, dict] = {}  # what the server is busy with, for the sidebar
_ACTIVITY_IDS = iter(range(1, 1 << 62))
_CACHE_VERSION = "9"  # 9: models carry logical, disabled, has_output, incremental_sql; project prefixes and suffixes apply; 8: models carry config_reads; comments no longer hold refs; 7: validated JSON model snapshots; never load legacy pickle
_CACHE_KEEP = 12
_SNAPSHOT_KEEP = 3
_ANALYSIS: dict = {}  # id(pipeline) -> {"state", "stage", "started", "finished"}
_GRAPH_SAVED: "OrderedDict[str, dict]" = OrderedDict()  # content key -> the analysis behind the unscoped graph, last computed or saved
_GRAPH_KEEP = 3


def _share_row_filters(rows: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Lineage rows with the filters every column of a model shares moved to one list per model.

    The conditions of a model's query limit the rows of all its columns, so repeating them on each row would
    multiply the payload by the column count. A column keeps only its own (``feeds_value``) filters; its full
    list is the model's shared ones followed by those.
    """

    by_node: dict[str, list[dict]] = {}
    for row in rows:
        by_node.setdefault(row["node"], []).append(row)
    shared: dict[str, list[dict]] = {}
    compact = []
    for node, group in by_node.items():
        known = [row["filters"] for row in group if "filters" in row]
        common = [f for f in known[0] if all(f in other for other in known[1:])] if known else []
        if common:
            shared[node] = common
        for row in group:
            if "filters" in row:
                row = {**row, "filters": [f for f in row["filters"] if f not in common]}
                if not row["filters"]:
                    del row["filters"]
            compact.append(row)
    return compact, shared


@contextmanager
def activity(label: str):
    """Mark ``label`` as running while the block runs; the sidebar shows it."""

    token = next(_ACTIVITY_IDS)
    with _LOCK:
        _ACTIVITY[token] = {"label": label, "started": time.time()}
    try:
        yield
    finally:
        with _LOCK:
            _ACTIVITY.pop(token, None)


def server_status() -> dict:
    """``GET /api/status``: what is running now (never waits on a build) and the latest stage timings."""

    from . import timing

    now = time.time()
    with _LOCK:
        busy = [{"label": item["label"], "elapsed": round(now - item["started"], 1)} for item in _ACTIVITY.values()]
    return {"busy": busy, "analysis": analysis_status(), "progress": timing.current_progress(),
            "timings": timing.recent()[-12:]}


def _cache_file(pipeline: Pipeline):
    key = getattr(pipeline, "content_key", None)
    if not key:
        return None
    from . import state

    try:
        import sqlglot

        from . import __version__, lineage_limits, schema_fetch
        tag = hashlib.sha256(f"{_CACHE_VERSION}|{__version__}|{sqlglot.__version__}|{lineage_limits.cache_tag()}|{schema_fetch.cache_tag()}|{key}".encode()).hexdigest()[:32]
        return state.data_path("analysis-cache", f"{tag}.json")
    except OSError:
        return None


def cached_result(pipeline: Pipeline, name: str, compute, persist: bool = True):
    """``compute()`` once per project, also saved by content (commit) in the data folder.

    The saved copy is plain JSON, so reloading the same commit after a restart skips
    the slow search. A missing, unreadable or stale file is just recomputed.
    """

    def load_or_compute():
        path = _cache_file(pipeline) if persist else None
        stored: dict = {}
        if path is not None and path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                if name in stored:
                    with stage("analysis cache hit", result=name):
                        return stored[name]
            except (OSError, ValueError):
                stored = {}
        value = compute()
        if path is not None:
            try:
                stored[name] = value
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(stored), encoding="utf-8")
                os.replace(temp, path)
                _prune_cache(path.parent)
            except (OSError, TypeError, ValueError):
                pass  # the cache is an optimisation; never fail an analysis over it
        return value

    return pipeline._remembered(("cached", name), load_or_compute)


def _prune_cache(folder) -> None:
    files = sorted((f for f in folder.glob("*.json")), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[_CACHE_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def _snapshot_file(key: str):
    """Where the parsed project for ``key`` is saved in the data folder (None when it cannot be)."""

    from . import state

    try:
        import sqlglot

        from . import __version__, lineage_limits, schema_fetch
        tag = hashlib.sha256(f"{_CACHE_VERSION}|{__version__}|{sqlglot.__version__}|{lineage_limits.cache_tag()}|{schema_fetch.cache_tag()}|{key}".encode()).hexdigest()[:32]
        return state.data_path("parse-cache", f"{tag}.json")
    except OSError:
        return None


def _load_snapshot(key: str):
    path = _snapshot_file(key)
    if path is None or not path.is_file():
        return None
    try:
        from .storage import pipeline_from_snapshot

        with stage("parse cache hit", file=path.name):
            pipeline = pipeline_from_snapshot(json.loads(path.read_text(encoding="utf-8")), key)
    except Exception:  # noqa: BLE001 - a damaged or older snapshot is just a miss
        return None
    pipeline.content_key = key
    return pipeline


def _save_snapshot(pipeline: Pipeline, key: str) -> None:
    path = _snapshot_file(key)
    if path is None:
        return
    try:
        from .storage import pipeline_snapshot, atomic_json

        atomic_json(path, pipeline_snapshot(pipeline, key))
        files = sorted(path.parent.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[_SNAPSHOT_KEEP:]:
            old.unlink(missing_ok=True)
        for legacy in path.parent.glob("*.pkl"):  # executable-format caches from earlier versions are never read
            legacy.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - the snapshot is an optimisation; never fail a load over it
        pass


def restore_snapshot(key: object, label: str, remote: dict | None = None) -> bool:
    """Show saved model metadata without git or SQLX parsing; SQL analysis is rebuilt."""

    if not isinstance(key, str) or not key:
        return False
    with _LOCK:
        cached = _PROJECT_CACHE.get(key)
    pipeline = cached or _load_snapshot(key)
    if pipeline is None:
        return False
    with _LOCK:  # the reload that follows finds the same commit here instead of reading the file again
        _PROJECT_CACHE[key] = pipeline
        while len(_PROJECT_CACHE) > _PROJECT_CACHE_SIZE:
            _PROJECT_CACHE.popitem(last=False)
    set_project(pipeline, label, remote=remote)
    return True


def _content_key(files: dict) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode("utf-8", "replace") + b"\0" + files[path].encode("utf-8", "replace") + b"\0")
    return digest.hexdigest()


def _warm(pipeline: Pipeline, state: dict) -> None:
    """Run the slow searches (identical and similar SELECTs) in the background.

    The graph does not need them; the Cost and Change reports pages do, and show
    progress until they are done. Results are kept on the pipeline and, by commit,
    on disk.
    """

    try:
        with activity("Analyzing repeated work"):
            state["stage"] = "repeated work"
            from .repeated_work import repeated_work_report

            cached_result(pipeline, "repeated_work", lambda: repeated_work_report(pipeline))
            state["stage"] = "shared logic"
            from .live_insights import shared_logic_proposals

            shared_logic_proposals(pipeline, _JOBS["records"])
        state["state"] = "done"
    except Exception as exc:  # noqa: BLE001 - the pages fall back to computing it themselves
        state.update(state="failed", error=str(exc))
        from . import console

        console.error(f"background analysis failed at stage '{state.get('stage') or 'start'}' (the pages compute it themselves)", exc,
                      code="KS-ANALYSIS")
    state["finished"] = time.time()
    state["event"].set()


def start_background_analysis(pipeline: Pipeline) -> None:
    """Start (once per project) the slow analysis a project needs for its other pages."""

    with _LOCK:
        if id(pipeline) in _ANALYSIS:
            return
        done = pipeline.has_cached("repeated_work") and pipeline.has_cached("shared_logic")
        state = {"state": "done" if done else "running", "stage": "", "started": time.time(), "pipeline": pipeline,
                 "event": threading.Event()}
        if done:
            state["finished"] = state["started"]
            state["event"].set()
        _ANALYSIS[id(pipeline)] = state
        for stale in [key for key, item in _ANALYSIS.items() if item["pipeline"] is not _LOADED_PIPELINE()]:
            if stale != id(pipeline):
                _ANALYSIS.pop(stale, None)
    if not done:
        threading.Thread(target=_warm, args=(pipeline, state), name="kumosql-analysis", daemon=True).start()


def _LOADED_PIPELINE():
    return _LOADED["pipeline"] if _LOADED else None


def analysis_status(pipeline: Pipeline | None = None) -> dict:
    """``{"state": "running"|"done"|"failed"|"idle", "stage", "elapsed"}`` for the loaded project's slow analysis."""

    with _LOCK:
        target = pipeline or _LOADED_PIPELINE()
        state = _ANALYSIS.get(id(target)) if target is not None else None
        if state is None or state["pipeline"] is not target:
            return {"state": "idle", "stage": "", "elapsed": 0}
        end = state.get("finished") or time.time()
        return {"state": state["state"], "stage": state["stage"], "elapsed": round(end - state["started"], 1)}


def pending_payload(current: dict, scope_name: str | None = None) -> dict | None:
    """What a page that needs the slow analysis answers while it is still running, else ``None``."""

    with _LOCK:
        state = _ANALYSIS.get(id(current["pipeline"]))
    if state is not None:
        state["event"].wait(0.5)  # small projects finish at once; only a slow analysis shows progress
    status = analysis_status(current["pipeline"])
    if status["state"] != "running":
        return None
    return {"pending": status, "source": source_info(), "scope": None,
            "message": "Looking for repeated work in your models. This runs in the background; the query graph is already available."}


def register_names(pipeline: Pipeline, remote: dict | None = None) -> None:
    """Tell the log redactor which names this project contains, so later log lines show placeholders."""

    from . import console

    try:
        projects, datasets, names, files = set(), set(), set(), set()
        for model in pipeline.models.values():
            projects.add(model.target.database)
            datasets.add(model.target.schema)
            names.add(model.target.name)
            if model.path:
                files.add(model.path)
        for target in pipeline.sources.values():
            projects.add(target.database)
            datasets.add(target.schema)
            names.add(target.name)
        projects.add(pipeline.default_project)
        datasets.add(pipeline.default_dataset)
        if remote:
            console.register("repo", remote.get("url"))
            console.register("branch", [remote.get("branch"), remote.get("actual")])
        console.register("project", sorted(p for p in projects if p))
        console.register("dataset", sorted(d for d in datasets if d))
        console.register("model", sorted(n for n in names if n))
        console.register("file", sorted(files))
    except Exception:  # noqa: BLE001 - naming is a logging nicety
        pass


def set_project(
    pipeline: Pipeline, label: str, observed_reads: Iterable[object] | None = None, remote: dict | None = None
) -> None:
    """Make ``pipeline`` the project the views show.

    ``observed_reads`` are job-history records; when given they replace the
    loaded job history, otherwise the history already loaded is kept. ``remote``
    (``{"url", "branch"}``) says which git branch the project came from, which
    is what a change report compares against.
    """

    global _LOADED
    register_names(pipeline, remote)
    with _LOCK:
        _LOADED = {"pipeline": pipeline, "label": label, "remote": remote, "report": None}
        if observed_reads is not None:
            records = tuple(observed_reads)
            # Records passed in code replace the history for this run only; the saved file is left alone.
            _JOBS.update(records=records, label="provided records" if records else "", restored_from=str(_jobs_file()))
    start_background_analysis(pipeline)


def forget_project() -> None:
    """Drop the loaded project, the in-memory parse cache and the saved analyses; job history stays."""

    global _LOADED
    with _LOCK:
        _LOADED = None
        _PROJECT_CACHE.clear()
        _GRAPH_SAVED.clear()
    from . import state

    for name in ("analysis-cache", "parse-cache", "graph-cache"):
        folder = state.data_dir() / name
        if folder.is_dir():
            for item in folder.glob("*"):
                try:
                    item.unlink()
                except OSError:
                    pass


def clear_project() -> None:
    """Forget the project and its job history."""

    global _LOADED
    with _LOCK:
        _LOADED = None
        _JOBS.update(records=(), label="")
        _forget_saved_jobs()


# Loaded job history is saved in the data folder, so it survives a restart and
# applies to whichever repository is loaded; "Remove job history" deletes it.
def _jobs_file():
    from . import state

    return state.data_dir() / "job-history" / "jobs.jsonl"


def _restore_jobs() -> None:
    """Read the saved job history once per data folder (call with ``_LOCK`` held)."""

    path = _jobs_file()
    if _JOBS["restored_from"] == str(path):
        return
    _JOBS["restored_from"] = str(path)
    if _JOBS["records"] or not path.is_file():
        return
    try:
        with path.open(encoding="utf-8") as handle:
            label = json.loads(handle.readline() or "{}").get("label", "")
            records = tuple(json.loads(line) for line in handle if line.strip())
    except (OSError, ValueError, AttributeError) as exc:
        console.error(f"could not read the saved job history in {path}", exc, trace=False)
        return
    _JOBS.update(records=records, label=str(label or "saved job history"))


def _save_jobs(records: list[dict], label: str) -> None:
    path = _jobs_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".tmp")
        with partial.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"label": label}) + "\n")
            for record in records:
                handle.write(json.dumps(record, default=str) + "\n")
        partial.replace(path)
    except OSError as exc:
        console.error(f"could not save the job history to {path}; it is kept until KumoSQL stops", exc, trace=False)
    _JOBS["restored_from"] = str(path)


def _forget_saved_jobs() -> None:
    path = _jobs_file()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        console.error(f"could not delete the saved job history {path}", exc, trace=False)
    _JOBS["restored_from"] = str(path)


def loaded() -> dict | None:
    """The loaded project (with its job history as ``observed_reads``), or ``None``."""

    with _LOCK:
        if _LOADED is None:
            return None
        _restore_jobs()
        return {**_LOADED, "observed_reads": _JOBS["records"], "jobs_label": _JOBS["label"]}


def set_report(report: dict | None) -> None:
    """Keep the latest change comparison with the project it was made for."""

    with _LOCK:
        if _LOADED is not None:
            _LOADED["report"] = report


def parse_job_history(text: object, filename: object = "", *, expand: bool = True) -> list[dict]:
    """Records from an exported job history: a JSON array, JSON lines or CSV.

    With ``expand`` (the default) script jobs are reduced to the real tables they read and wrote.

    Exports name the destination ``destination_table``; the graph reads
    ``destination``, so it is filled in. CSV cells holding JSON are decoded.
    """

    if not isinstance(text, str) or not text.strip():
        raise ProjectError("the job history file is empty")
    stripped = text.strip()
    name = filename.lower() if isinstance(filename, str) else ""
    try:
        if name.endswith(".csv") or (not stripped.startswith(("[", "{"))):
            records = list(csv.DictReader(io.StringIO(stripped)))
        elif stripped.startswith("["):
            records = json.loads(stripped)
        else:
            records = [json.loads(line) for line in stripped.splitlines() if line.strip()]
    except (ValueError, csv.Error) as exc:
        raise ProjectError(f"could not read the job history: {exc}") from exc
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise ProjectError("job history must be a list of job records")
    if len(records) > MAX_JOB_RECORDS:
        raise ProjectError(f"job history is limited to {MAX_JOB_RECORDS} jobs")
    out = []
    for row in records:
        row = dict(row)
        for key in ("referenced_tables", "destination_table", "destination", "labels"):
            value = row.get(key)
            if isinstance(value, str) and value.lstrip().startswith(("[", "{")):
                try:
                    row[key] = json.loads(value)
                except ValueError:
                    pass
        if not row.get("destination") and row.get("destination_table"):
            row["destination"] = row["destination_table"]
        out.append(row)
    if not out:
        raise ProjectError("the job history file has no jobs")
    if expand:
        out, _summary = expand_script_jobs(out)
    return out


def load_job_history(text: object, filename: object = "") -> int:
    """Load a job-history export for the loaded project; returns the number of jobs."""

    raw = parse_job_history(text, filename, expand=False)
    records, scripts = expand_script_jobs(raw)
    label = str(filename or "job history")[:120]
    with _LOCK:
        if _LOADED is None:
            raise ProjectError("load a project before its job history")
        _JOBS.update(records=tuple(records), label=label, scripts=scripts)
        _LOADED["report"] = None
        _save_jobs(records, label)
    return len(raw)


def clear_job_history() -> None:
    with _LOCK:
        _JOBS.update(records=(), label="", scripts=None)
        _forget_saved_jobs()


def source_info() -> dict:
    """The ``source`` block every view carries: what is loaded and from where."""

    current = loaded()
    if current is None:
        return {"kind": "none", "label": "", "jobs": None}
    count = len(current["observed_reads"])
    jobs = {"label": current["jobs_label"], "count": count} if count else None
    if jobs and _JOBS.get("scripts") and _JOBS["scripts"].get("scripts"):
        jobs["scripts"] = _JOBS["scripts"]
    return {
        "kind": "project", "label": current["label"],
        "git": bool(current.get("remote")),
        "jobs": jobs,
    }


def empty_payload(needs: str, message: str, scope_name: str | None = None) -> dict:
    """What a view answers when it has nothing to show: what to load, never made-up numbers."""

    return {
        "empty": True, "needs": needs, "message": message, "source": source_info(),
        "scope": {"name": scope_name, "rule": None, "applied_to": [],
                  "note": "Load a project to apply this scope."} if scope_name else None,
    }


def _safe_path(path: object) -> PurePosixPath:
    if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path or "\0" in path or ":" in path:
        raise ProjectError("invalid file path")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if posix.is_absolute() or windows.drive or windows.root or any(
        p in ("", ".", "..") or p.endswith((".", " ")) or is_windows_device_name(p)
        for p in path.split("/")
    ):
        raise ProjectError("invalid file path")
    name = posix.name.lower()
    if not (name.endswith(_ALLOWED_SUFFIXES) or name in _CONFIG_FILES):
        raise ProjectError("only .sqlx, .sql, .js and Dataform config files are accepted")
    return posix


def _write_files(files: object, directory: str) -> None:
    """Write ``{relative path: text}`` under ``directory`` after the same checks every load uses."""

    if not isinstance(files, dict) or not files:
        raise ProjectError("files must be a non-empty object of path to text")
    if len(files) > MAX_FILES:
        raise ProjectError(f"too many files (limit {MAX_FILES})")
    total = 0
    root = Path(directory).resolve()
    for path, text in files.items():
        relative = _safe_path(path)
        if not isinstance(text, str):
            raise ProjectError("file contents must be text")
        total += len(text.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise ProjectError("project is too large")
        target = root.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(root):
            raise ProjectError("invalid file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(root):
            raise ProjectError("invalid file path")
        target.write_text(text, encoding="utf-8")


class _Checkout:
    """A temporary folder holding project files; deleted on exit.

    On Windows the path carries the extended-length prefix, because repository
    paths can exceed the 260-character limit.
    """

    def __enter__(self) -> str:
        self._created = tempfile.mkdtemp(prefix="kumosql-project-")
        return str(extended_path(Path(self._created).resolve()))

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self._created, ignore_errors=True)


def _compiled_targets(repo_url: str | None):
    """The loader's fallback to Dataform's compilation for a repository, or None without a remote."""

    if not repo_url:
        return None
    from . import workflow_configs

    return lambda: workflow_configs.compiled_targets(repo_url)


def _awaits_compilation(pipeline: Pipeline, repo_url: str | None) -> bool:
    """Whether the Dataform compilation could not be read when this project was parsed and may be now."""

    return bool(repo_url) and any(d.code == "compiled_graph_unavailable" for d in pipeline.diagnostics)


def pipeline_from_files(files: object, repo_url: str | None = None):
    """Load a ``Pipeline`` from ``{relative path: text}``."""

    key = _content_key(files) if isinstance(files, dict) and all(isinstance(v, str) for v in files.values()) else None
    with _LOCK:
        cached = _PROJECT_CACHE.get(key) if key else None
        if cached is not None:
            _PROJECT_CACHE.move_to_end(key)
    if cached is not None and _awaits_compilation(cached, repo_url):
        cached = None
    if cached is not None:
        with stage("project cache hit", files=len(files)):
            return cached
    if key:
        saved = _load_snapshot(key)
        if saved is not None and not _awaits_compilation(saved, repo_url):
            with _LOCK:
                _PROJECT_CACHE[key] = saved
                while len(_PROJECT_CACHE) > _PROJECT_CACHE_SIZE:
                    _PROJECT_CACHE.popitem(last=False)
            return saved
    with activity("Parsing project"), _Checkout() as directory:
        with stage("write files", files=len(files) if isinstance(files, dict) else 0):
            _write_files(files, directory)
        try:
            with stage("parse project"):
                pipeline = load_sqlx_project(directory, compiled_targets=_compiled_targets(repo_url))
            pipeline.completeness()  # analyse now: the folder is deleted on exit
            pipeline.source_files = dict(files)  # the text a patch edits (kumosql.shared_models)
        except Exception as exc:  # loader errors are user-facing
            from . import console

            console.error(f"project parse: reading {len(files) if isinstance(files, dict) else 0} files failed", exc, code="KS-GRAPH-BUILD")
            detail = str(exc) or "project could not be loaded"
            raise ProjectError(detail if isinstance(exc, (ValueError, OSError)) else f"{type(exc).__name__}: {detail}") from exc
    if key:
        # The saved copy is always written, so a restart shows the project at once. A load that could not reach Dataform
        # stays out of the in-memory cache and is parsed again by the next load (see _awaits_compilation).
        pipeline.content_key = key
        _save_snapshot(pipeline, key)
    if key and not _awaits_compilation(pipeline, repo_url):
        with _LOCK:
            _PROJECT_CACHE[key] = pipeline
            while len(_PROJECT_CACHE) > _PROJECT_CACHE_SIZE:
                _PROJECT_CACHE.popitem(last=False)
    return pipeline


def load_files(files: object, label: str, remote: dict | None = None) -> Pipeline:
    """Load a project from ``{relative path: text}`` and make it current."""

    if not isinstance(label, str) or not label.strip():
        label = "uploaded project"
    pipeline = pipeline_from_files(files, (remote or {}).get("url"))
    set_project(pipeline, label.strip()[:200], remote=remote)
    return pipeline


def _edge_source(edge: dict) -> str:
    if edge["observed"] and (edge["declared"] or edge["parsed"]):
        return "both"
    if edge["declared"]:
        return "declared"
    return "observed" if edge["observed"] else "parsed"


def _split(key: str) -> tuple[str, str]:
    parts = key.split(".")
    return (".".join(parts[-3:-1]) if len(parts) > 1 else ""), parts[-1]


def _plan(scope_name: str | None, observed_reads: Iterable[object]):
    """The saved scope named ``scope_name`` and where it applies; ``ValueError`` for an unknown name."""

    from . import scopes as scope_store

    if not scope_name:
        return None
    chosen = scope_store.get_scope(scope_name)
    if chosen is None:
        raise ValueError(f"no saved scope named {scope_name!r}")
    return scope_store.plan_scope(chosen, observed_reads)


def graph_payload(
    pipeline: Pipeline, label: str = "", observed_reads: Iterable[object] = (), scope_name: str | None = None
) -> dict:
    """The ``/api/graph`` payload for a real pipeline. No ``preview`` flag.

    With ``scope_name`` (a saved scope) the graph is limited to the models it
    matches and the job history it matches; ``scope`` in the payload says which.
    """

    observed_reads = list(observed_reads)
    plan = _plan(scope_name, observed_reads)
    inputs = _graph_inputs(pipeline, observed_reads, plan)
    return _graph_from_inputs(pipeline, inputs, plan)


def _graph_inputs(pipeline: Pipeline, observed_reads: list, plan) -> dict:
    """The slow part of the graph payload: the pipeline analysis (graph, completeness, coverage, lineage, columns).

    Plain JSON, so the unscoped copy without job history can be saved by commit and shown after a restart while
    the project is analysed again (see :func:`_saved_graph`).
    """

    with stage("graph report", models=len(pipeline.models)):
        # The duplicate searches are the slowest part and the graph does not use them.
        report = pipeline.report(
            observed_reads=observed_reads,
            scope=plan.models if plan else None,
            observed_scope=plan.jobs if plan else None,
            include_duplicates=False,
        )
    with stage("lineage"):
        lineage = pipeline.lineage_report()
    if plan and plan.models is not None:
        keep = pipeline.scope_keys(plan.models)
        lineage = [row for row in lineage if row["node"] in keep]
    return {"graph": report.get("graph") or {"nodes": [], "edges": []}, "completeness": report["completeness"],
            "coverage": dict(report["coverage"] or {}), "lineage": lineage,
            "outputs": {key: list(pipeline.output_columns(key)) for key in pipeline.models}}


def _graph_from_inputs(pipeline: Pipeline, inputs: dict, plan) -> dict:
    """The ``/api/graph`` payload from :func:`_graph_inputs`; what depends on settings is read live."""

    graph, completeness, lineage = inputs["graph"], inputs["completeness"], inputs["lineage"]
    lineage, model_filters = _share_row_filters(lineage)

    columns: dict[str, list[str]] = {}

    def add_column(node: str, column: str) -> None:
        seen = columns.setdefault(node, [])
        if column not in seen:
            seen.append(column)

    for key, names in inputs["outputs"].items():
        for column in names:
            add_column(key, column)
    for row in lineage:
        add_column(row["node"], row["column"])
        for source in row["sources"]:
            add_column(source["node"], source["column"])

    from . import catalogs

    have = catalogs.owned(pipeline)
    gap_assets = {gap["asset"] for gap in completeness["gaps"] if gap["blocking"]}
    nodes = []
    for item in graph["nodes"]:
        key = item["identity"]["key"]
        dataset, name = _split(key)
        model = pipeline.models.get(key)
        if model is not None:
            kind = "view" if model.kind == "view" else "model"
        elif item["resolved"]:
            kind = "observed"
        else:
            kind = "source"
        entry = {
            "id": key, "dataset": dataset, "name": name, "kind": kind,
            "source": "declared" if model is not None else "parsed",
            "columns": columns.get(key, []),
            "owned": key in have,
        }
        if key in gap_assets:
            entry["note"] = "Could not be fully analyzed; its reads are unknown."
        nodes.append(entry)
    known = {node["id"] for node in nodes}
    nodes.extend(
        {"id": key, "dataset": _split(key)[0], "name": _split(key)[1], "kind": "unmatched",
         "source": "declared", "columns": [], "note": "Could not be analyzed."}
        for key in sorted(gap_assets - known) if key in pipeline.models
    )

    edges = [
        {
            "from": edge["upstream"]["key"], "to": edge["downstream"]["key"],
            "source": _edge_source(edge),
            "confidence": edge["confidence"], "last_seen": edge["last_seen"],
            "observed_count": edge["observed_count"],
        }
        for edge in graph["edges"]
    ]
    coverage = dict(inputs["coverage"])
    coverage.setdefault("complete", completeness["complete"])
    return {
        "source": source_info(),
        "scope": plan.to_json() if plan else None,
        "catalogs": have.catalogs,
        "window": coverage.get("window") or {"start": None, "end": None},
        "coverage": coverage,
        "nodes": nodes,
        "edges": edges,
        "column_lineage": lineage,
        "model_filters": model_filters,
        "gaps": [
            {"asset": gap["asset"], "kind": gap["kind"], "message": gap["message"],
             "blocking": gap["blocking"]}
            for gap in completeness["gaps"]
        ],
        "completeness": {
            "complete": completeness["complete"],
            "views": completeness["views"],
            "assets_not_analyzed": completeness["assets_not_analyzed"],
        },
    }


def _graph_cache_file(key: str):
    """Where the analysis behind the unscoped graph of ``key`` is saved (None when it cannot be)."""

    from . import state

    try:
        import sqlglot

        from . import __version__, lineage_limits, schema_fetch
        tag = hashlib.sha256(f"{_CACHE_VERSION}|{__version__}|{sqlglot.__version__}|{lineage_limits.cache_tag()}|{schema_fetch.cache_tag()}|graph|{key}".encode()).hexdigest()[:32]
        return state.data_path("graph-cache", f"{tag}.json")
    except OSError:
        return None


def _remember_graph(key: str, inputs: dict) -> None:
    with _LOCK:
        _GRAPH_SAVED[key] = inputs
        _GRAPH_SAVED.move_to_end(key)
        while len(_GRAPH_SAVED) > _GRAPH_KEEP:
            _GRAPH_SAVED.popitem(last=False)


def _save_graph(key: str, inputs: dict) -> None:
    path = _graph_cache_file(key)
    if path is None:
        return
    try:
        from .storage import atomic_json

        atomic_json(path, {"content_key": key, "inputs": inputs})
        for old in sorted(path.parent.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[_GRAPH_KEEP:]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - a saved copy is an optimisation; never fail a graph over it
        pass


def _saved_graph(pipeline: Pipeline) -> dict | None:
    """The analysis behind the unscoped graph as last computed for this commit, while ``pipeline`` has not been analysed.

    The saved parse restores the models only; analysing them again (reading every model and tracing every column)
    takes minutes on a large project, so after a restart the graph is shown from this copy while the analysis runs
    in the background (:func:`_refresh_graph`). ``None`` once the pipeline has been analysed, and when nothing was saved.
    """

    key = getattr(pipeline, "content_key", None)
    if not key or getattr(pipeline, "_graph_state", "") in ("fresh", "failed"):
        return None
    with _LOCK:
        inputs = _GRAPH_SAVED.get(key)
    if inputs is not None:
        return inputs
    inputs = _load_saved_graph(key)
    if inputs is not None:
        _remember_graph(key, inputs)
    return inputs


def _load_saved_graph(key: str) -> dict | None:
    path = _graph_cache_file(key)
    if path is None or not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        inputs = saved["inputs"]
        if saved["content_key"] != key or not {"graph", "completeness", "coverage", "lineage", "outputs"} <= set(inputs):
            return None
    except Exception:  # noqa: BLE001 - a damaged or older copy is just a miss
        return None
    return inputs


def _analyse_graph(pipeline: Pipeline) -> dict:
    """Analyse the unscoped graph of ``pipeline`` now, and keep the result by commit."""

    inputs = _graph_inputs(pipeline, [], None)
    pipeline._graph_state = "fresh"
    key = getattr(pipeline, "content_key", None)
    if key:
        _remember_graph(key, inputs)
        _save_graph(key, inputs)
    return inputs


def _refresh_graph(pipeline: Pipeline) -> None:
    """Analyse ``pipeline`` in a background thread (once), while the saved copy is shown."""

    with _LOCK:
        if getattr(pipeline, "_graph_state", "") == "refreshing":
            return
        pipeline._graph_state = "refreshing"

    def run() -> None:
        try:
            with activity("Building graph"), stage("graph refresh"):
                _analyse_graph(pipeline)
        except Exception as exc:  # noqa: BLE001 - the next request analyses it itself and reports the error
            pipeline._graph_state = "failed"
            console.error("graph refresh failed (the next request analyses the project itself)", exc, code="KS-GRAPH-BUILD")

    threading.Thread(target=run, name="kumosql-graph-refresh", daemon=True).start()


def graph_or_empty(scope_name: str | None = None) -> dict:
    """The graph of the loaded project, or the empty state when nothing is loaded."""

    current = loaded()
    if current is None:
        return empty_payload(
            "project", "Load a Dataform project to see its query graph.", scope_name)
    pipeline = current["pipeline"]
    saved = _saved_graph(pipeline) if not scope_name and not current["observed_reads"] else None
    if saved is not None:
        _refresh_graph(pipeline)
        with stage("graph payload from the saved copy"):
            payload = _graph_from_inputs(pipeline, saved, None)
    elif not scope_name and not current["observed_reads"]:
        with activity("Building graph"), stage("graph payload"):
            payload = _graph_from_inputs(pipeline, _analyse_graph(pipeline), None)
    else:
        with activity("Building graph"), stage("graph payload"):
            payload = graph_payload(pipeline, current["label"], current["observed_reads"], scope_name)
    models = pipeline.models
    for node in payload.get("nodes", []):  # Dataform ``tags`` from each action's config block
        node["dataform_tags"] = sorted({tag for tag in getattr(models.get(node["id"]), "tags", ()) or () if isinstance(tag, str)})
    try:  # production schedules come from saved Dataform data only; never block or fail the graph
        from .workflow_configs import annotate

        annotate(payload, pipeline, current.get("remote"))
    except Exception:  # noqa: BLE001
        pass
    return payload


_PAGE_CHANGES = {"drop": "drop_column", "rename": "rename_column", "expression": "change_expression"}


def impact_payload(node: str, column: str, change: str, scope_name: str | None = None) -> dict:
    """The ``/api/impact`` payload: the server's blast radius of one column change.

    ``change`` is ``drop``, ``rename`` or ``expression``. A loaded project is
    assessed by ``Pipeline.assess_change`` together with its job history;
    ``ValueError`` when nothing is loaded.
    """

    kind = _PAGE_CHANGES.get(change)
    if kind is None:
        raise ValueError("change must be drop, rename or expression")
    current = loaded()
    if current is None:
        raise ValueError(NOT_LOADED)
    reads = list(current.get("observed_reads", ()))
    plan = _plan(scope_name, reads)
    if plan and plan.jobs is not None:
        from .scopes import job_record

        reads = [row for row in reads if plan.jobs.matches(job_record(row))]
    from . import catalogs

    result = current["pipeline"].assess_change(
        kind, node, column, scope=plan.models if plan else None, observed_reads=reads,
        owned=catalogs.owned(current["pipeline"]),
    )
    return {**result.to_json(), "source": source_info(),
            "scope_plan": plan.to_json() if plan else None}


def overlaps_payload(node: str, scope: str | None = None) -> dict:
    """The ``/api/overlaps`` payload: tables that already provide the same attributes as ``node``.

    A loaded project is compared with ``OverlapChecker`` (optionally limited to
    the saved scope named ``scope``); ``ValueError`` when nothing is loaded. A comparison that fails is returned
    as ``status: "unavailable"``, never as an error, so the page keeps working.
    Raises ``ValueError`` for an unknown scope or a node that is not a model.
    """

    from . import scopes as scope_store
    from .overlap_report import OverlapChecker

    current = loaded()
    if current is None:
        raise ValueError(NOT_LOADED)
    plan = _plan(scope, current.get("observed_reads", ()))
    chosen = plan.models if plan else None
    pipeline = current["pipeline"]
    if node not in pipeline.models:
        raise ValueError("node is not a model in the loaded project")
    section = OverlapChecker(pipeline, scope=chosen).section(node)
    return {**section, "node": node, "scope": scope or None, "scope_plan": plan.to_json() if plan else None,
            "source": source_info()}
