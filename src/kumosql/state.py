"""Persistent local state shared by the UI, the CLI and the Python API.

State is one JSON file in the data directory: the local data folder the user
chose in Settings (recorded by a tiny pointer file in the platform's per-user
location), else that per-user location (or ``$KUMOSQL_HOME`` when set). UI
preferences, saved scopes and formatting preferences survive restarts and are
shared by every entry point.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import threading
from contextlib import contextmanager

from .resilience import read_bytes_shared, replace_file

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

STATE_FILENAME = "state.json"
_LOCK = threading.Lock()


POINTER_FILENAME = "location.json"
_MIGRATE_LOCK = threading.RLock()
_NOT_MOVED = {".state.lock", POINTER_FILENAME, "git-cache"}  # clones are rebuilt, not moved


def default_dir() -> Path:
    """The fixed per-user location. Once a local data folder is chosen it holds only the pointer to it."""

    override = os.environ.get("KUMOSQL_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "kumosql"


def pointer_path() -> Path:
    return default_dir() / POINTER_FILENAME


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def chosen_folder() -> Path | None:
    """The local data folder the user chose, or ``None``.

    It is recorded in a tiny pointer file in :func:`default_dir`. A folder chosen by an
    earlier version (saved inside ``state.json``) is moved over to the pointer, with all
    the other files, the first time it is seen.
    """

    pointer = _read_json(pointer_path()).get("folder")
    if isinstance(pointer, str) and pointer:
        return Path(pointer)
    legacy = _read_json(default_dir() / STATE_FILENAME).get("storage")
    folder = legacy.get("folder") if isinstance(legacy, dict) else None
    if not (isinstance(folder, str) and folder):
        return None
    with _MIGRATE_LOCK:
        try:
            migrate(default_dir(), Path(folder))
            set_pointer(Path(folder))
        except OSError:
            return None  # cannot reach the old folder now; keep using the default location
    return Path(folder)


def set_pointer(folder: Path) -> None:
    pointer = pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"folder": str(folder)}, indent=2), encoding="utf-8")


def data_dir() -> Path:
    """The directory KumoSQL keeps all its local files in: the chosen folder, else the default."""

    return chosen_folder() or default_dir()


def data_path(*parts: str) -> Path:
    """A path inside the data directory, with its parent folder created.

    Every file KumoSQL keeps (settings, caches, logs, a feature's own store) belongs under
    this, so choosing a local data folder in Settings moves it all together: for example
    ``state.data_path("tags", "rules.json")``.
    """

    if not parts:
        return data_dir()
    path = data_dir().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def migrate(source: Path, destination: Path) -> dict:
    """Move everything KumoSQL keeps from ``source`` to ``destination``; nothing is lost.

    ``state.json`` is merged (a section already in the destination wins); any other file
    already in the destination is kept and the source copy is left where it is. Clones in
    ``git-cache`` are not moved: they are fetched again on the next load. Returns
    ``{"moved": [...], "merged": [...], "kept": [...]}`` (names only).
    """

    report: dict[str, list[str]] = {"moved": [], "merged": [], "kept": []}
    if source.resolve() == destination.resolve() or not source.is_dir():
        return report
    destination.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        name = item.name
        if name in _NOT_MOVED or name.startswith(".state-") or name.startswith(".kumosql-probe"):
            continue
        target = destination / name
        if name == STATE_FILENAME and target.exists():
            theirs, mine = _read_json(target), _read_json(item)
            added = [key for key in mine if key not in theirs and key != "storage"]
            theirs.update({key: mine[key] for key in added})
            theirs.pop("storage", None)
            target.write_text(json.dumps(theirs, indent=2, sort_keys=True), encoding="utf-8")
            item.unlink()
            report["merged"].append(name)
        elif target.exists():
            report["kept"].append(name)
        elif item.is_dir():
            shutil.copytree(item, target)
            shutil.rmtree(item, ignore_errors=True)
            report["moved"].append(name)
        else:
            shutil.copy2(item, target)
            item.unlink()
            report["moved"].append(name)
    moved_state = destination / STATE_FILENAME
    if moved_state.exists():  # the folder setting now lives in the pointer file
        data = _read_json(moved_state)
        if data.pop("storage", None) is not None:
            moved_state.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return report


def state_path() -> Path:
    return data_dir() / STATE_FILENAME


def load_state() -> dict:
    """Read all saved state; a missing or unreadable file yields empty state."""

    try:
        data = json.loads(read_bytes_shared(state_path()).decode("utf-8"))  # never blocks a writer on Windows
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)
        replace_file(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@contextmanager
def _file_lock():
    """Serialise writers across processes (UI server and CLI) with an OS file lock."""

    path = data_dir() / ".state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def get_section(name: str, default=None):
    """Return one top-level section of the saved state."""

    with _LOCK:
        return load_state().get(name, default)


def set_section(name: str, value) -> None:
    """Replace one top-level section, leaving the others untouched."""

    with _LOCK, _file_lock():
        state = load_state()
        state[name] = value
        _write(state)
