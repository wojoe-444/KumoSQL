"""The local data folder: where KumoSQL keeps all its files, such as settings, caches, logs and repository clones.

It is a saved setting, chosen by the user, because the default location can be
unusable for git: the Microsoft Store build of Python redirects writes under
AppData into a private folder that ``git.exe`` cannot see. Choosing a folder
checks that it exists, is writable, and that git can see what Python writes there.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, fields
from pathlib import Path

from . import state

SECTION = "storage"
SNAPSHOT_VERSION = 5  # 5: models carry fetched_from and fetched_at (kumosql.view_definitions); 4: models carry job_id and job_step (kumosql.job_sql); 3: models carry logical, disabled, has_output, incremental_sql and the pipeline a default_location; 2: models carry config_reads and config_reads_unread


def atomic_json(path: Path, value: object) -> None:
    """Replace a JSON file using a new private temporary file, never a shared .tmp name."""

    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, allow_nan=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _object(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid snapshot fields")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("invalid snapshot strings")
    return value


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError("invalid snapshot string map")
    return value


def pipeline_snapshot(pipeline, key: str) -> dict:
    """Only model data is persisted: no ASTs, classes, locks or executable objects."""

    return {"version": SNAPSHOT_VERSION, "content_key": key,
            "models": {k: asdict(v) for k, v in pipeline.models.items()},
            "sources": {k: asdict(v) for k, v in pipeline.sources.items()},
            "source_schema": pipeline.source_schema,
            "diagnostics": [asdict(v) for v in pipeline.diagnostics],
            "default_project": pipeline.default_project, "default_dataset": pipeline.default_dataset,
            "default_location": pipeline.default_location,
            "source_files": getattr(pipeline, "source_files", {})}


def pipeline_from_snapshot(value: object, key: str):
    """Validate the entire versioned schema before constructing a Pipeline."""

    from .pipeline import Pipeline, Model, Target, PipelineDiagnostic
    from .live_graph import MAX_FILES, MAX_TOTAL_BYTES, _safe_path

    data = _object(value, {"version", "content_key", "models", "sources", "source_schema", "diagnostics",
                           "default_project", "default_dataset", "default_location", "source_files"})
    if type(data["version"]) is not int or data["version"] != SNAPSHOT_VERSION or data["content_key"] != key:
        raise ValueError("stale snapshot")
    if not all(isinstance(data[k], str) for k in ("content_key", "default_project", "default_dataset", "default_location")):
        raise ValueError("invalid snapshot defaults")

    def target(value):
        obj = _object(value, {"database", "schema", "name"})
        _string_map(obj)
        return Target(**obj)

    models = {}
    for name, raw in _mapping(data["models"]).items():
        obj = _object(raw, {f.name for f in fields(Model)}).copy()
        if not all(isinstance(obj[k], str) for k in ("kind", "sql")) or not (obj["path"] is None or isinstance(obj["path"], str)):
            raise ValueError("invalid snapshot model")
        if obj["path"] is not None:
            # Models loaded on Windows retain native separators; validate both forms.
            _safe_path(obj["path"].replace("\\", "/"))
        obj["target"] = target(obj["target"])
        if not isinstance(obj["declared_dependencies"], list):
            raise ValueError("invalid snapshot dependencies")
        obj["declared_dependencies"] = tuple(target(t) for t in obj["declared_dependencies"])
        for k in ("masked_expressions", "tags", "non_null", "operations_sql", "config_reads", "config_reads_unread", "logical", "incremental_sql"):
            obj[k] = tuple(_strings(obj[k]))
        if not all(isinstance(obj[k], bool) for k in ("disabled", "has_output")):
            raise ValueError("invalid snapshot model flags")
        if not (obj["job_id"] is None or isinstance(obj["job_id"], str)) or not (obj["job_step"] is None or type(obj["job_step"]) is int):
            raise ValueError("invalid snapshot model job")
        if not all(isinstance(obj[k], str) for k in ("fetched_from", "fetched_at")):
            raise ValueError("invalid snapshot model origin")
        if not isinstance(obj["unique_keys"], list):
            raise ValueError("invalid snapshot unique keys")
        obj["unique_keys"] = tuple(tuple(_strings(v)) for v in obj["unique_keys"])
        model = Model(**obj)
        if model.key != name:
            raise ValueError("invalid snapshot model identity")
        models[name] = model
    sources = {name: target(raw) for name, raw in _mapping(data["sources"]).items()}
    if any(name != t.key for name, t in sources.items()):
        raise ValueError("invalid snapshot source identity")
    schema = {name: _string_map(raw) for name, raw in _mapping(data["source_schema"]).items()}
    if not isinstance(data["diagnostics"], list):
        raise ValueError("invalid snapshot diagnostics")
    diagnostics = []
    for raw in data["diagnostics"]:
        obj = _string_map(_object(raw, {"model", "code", "message"}))
        diagnostics.append(PipelineDiagnostic(**obj))
    files = _string_map(data["source_files"])
    if len(files) > MAX_FILES or sum(len(v.encode("utf-8")) for v in files.values()) > MAX_TOTAL_BYTES:
        raise ValueError("snapshot project is too large")
    for name in files:
        _safe_path(name)
    pipeline = Pipeline(models, sources, schema, diagnostics, data["default_project"], data["default_dataset"], data["default_location"])
    pipeline.source_files = files
    return pipeline


def _mapping(value: object) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError("invalid snapshot mapping")
    return value


class StorageError(ValueError):
    """The chosen folder cannot be used."""


def saved_folder() -> Path | None:
    return state.chosen_folder()


def configured() -> bool:
    """True when the user chose a folder (or an environment override pins one)."""

    return bool(os.environ.get("KUMOSQL_GIT_CACHE")) or saved_folder() is not None


def suggested() -> str:
    return str(Path.home() / "KumoSQL")


def describe() -> dict:
    folder = saved_folder()
    return {"folder": str(folder) if folder else None, "suggested": suggested(),
            "configured": configured(), "pointer": str(state.pointer_path()), "files": str(state.data_dir()), "override": os.environ.get("KUMOSQL_GIT_CACHE") or None}


def _probe(folder: Path) -> None:
    """Prove git and Python see the same folder: Python writes a file, git reads it and inits a repo."""

    from .git_repo import GitRepoError, _run

    probe = Path(tempfile.mkdtemp(prefix=".kumosql-probe-", dir=folder))
    try:
        marker = probe / "marker.txt"
        marker.write_text("kumosql", encoding="utf-8")
        _run(["init", "-q"], cwd=probe)
        if not (probe / ".git").is_dir():
            raise StorageError(
                "git ran but its files are not visible to Python in this folder. "
                "Choose a folder outside AppData, such as one in your home folder.")
        _run(["hash-object", "marker.txt"], cwd=probe)
    except GitRepoError as exc:
        raise StorageError(
            f"git cannot use this folder ({exc}). Choose a folder outside AppData, such as one in your home folder.") from exc
    finally:
        from .git_repo import _rmtree

        try:
            _rmtree(probe)
        except OSError:
            pass


def validate(value: object) -> Path:
    """Expand, create if needed and check a folder; returns its absolute path."""

    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\0" in value:
        raise StorageError("Enter a folder path")
    folder = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    if not folder.is_absolute():
        raise StorageError("Enter a full path, such as C:\\Users\\you\\KumoSQL or /home/you/KumoSQL")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Could not create {folder}: {exc}") from exc
    if not os.access(folder, os.W_OK):
        raise StorageError(f"{folder} is not writable")
    _probe(folder)
    return folder.resolve()


def save(value: object) -> dict:
    """Validate and save the folder, moving the settings and caches already kept elsewhere into it.

    Only a tiny pointer file stays in the default location. Clones are not moved (they are
    fetched again on their next load). The result's ``migrated`` says what moved.
    """

    folder = validate(value)
    with state._MIGRATE_LOCK:
        previous = state.data_dir()
        report = state.migrate(previous, folder) if previous.resolve() != folder else {"moved": [], "merged": [], "kept": []}
        state.set_pointer(folder)
    result = describe()
    result["migrated"] = {**report, "from": str(previous)} if any(report.values()) else None
    if previous.resolve() != folder:
        result["previous"] = str(previous)
    return result
