"""A warm start for the agent-facing server: a saved column trace, a background build, and a refresh after edits.

``python -m kumosql mcp PROJECT`` used to pay the whole analysis in front of the first answer, on every start. Three
things change that, all in this module (the analysis itself reuses traces through :mod:`kumosql.trace_memo`):

* **A saved trace.** After an analysis the trace of every model is saved as JSON (a version header, then one entry per
  model keyed by a hash of the model and everything upstream of it) in a cache folder: ``--cache-dir``, else
  ``kumosql/mcp-cache`` in the platform's per-user cache folder, never in the project. The next start replays the entries whose hash still
  matches and traces only the rest. A file that is missing, corrupt, from another KumoSQL or sqlglot version, or in
  another format is ignored and written again. Nothing in it is executed or trusted for more than a hash and names.
* **A background build.** The server answers ``initialize`` at once and builds the index in a thread. While it runs, a
  tool call waits up to ``wait_seconds`` and then answers ``index warming, N% done`` (a bounded answer with the stage
  and when to ask again), never a guess.
* **Saved results and files.** A second file beside it (``<name>.session.json``) keeps what else is costly to rebuild: the
  effect of each Dataform file (:mod:`kumosql.load_memo`), the table profiles, the column types and the expression index
  (:mod:`kumosql.result_memo`), and the size, time and hash of every watched file. A restart on an unchanged project reads
  no file (a file whose size and time match is known by its saved hash), parses none and computes none of those results; after
  an edit only the changed file is read and parsed, and only the models downstream of it are computed again. Same rules as
  the trace file: validated JSON, atomic rename, per-user folder outside the project, anything unreadable ignored.
* **A refresh after edits.** On a tool call (at most once per ``refresh_interval`` seconds; ``0`` checks on every
  call) the project's files are compared by size and modification time, then by content hash. When content changed the
  project is read again in the background and only the models whose own definition or upstream changed are traced
  again; the rest are replayed. The answer waits or says ``index warming`` as above, so an answer never mixes the
  old and the new project.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from . import load_memo, result_memo
from .mcp_answers import Answer, render
from .mcp_session import Session
from .trace_memo import FORMAT, TraceMemo

CACHE_NAME = "kumosql-mcp-trace-cache"
SESSION_NAME = "kumosql-mcp-session-cache"
#: the files a project is made of (a Dataform folder, ``.sql`` files, workflow settings, a compiled graph)
WATCHED_SUFFIXES = (".sqlx", ".sql", ".js", ".json", ".yaml", ".yml")
SKIPPED_FOLDERS = (".git", "node_modules", ".kumosql-cache", "__pycache__")
MAX_WATCHED_FILES = 50_000
MAX_CACHE_BYTES = 512 * 1024 * 1024
DEFAULT_WAIT_SECONDS = 3.0

#: percent of the build spent loading, then tracing (the long part), then profiling and roles
_LOAD, _TRACE_FROM, _TRACE_SPAN, _PROFILES, _ROLES = 2, 5, 85, 92, 96


# ------------------------------------------------------------------ the saved file


def default_cache_dir(root: Path | None = None) -> Path:
    """The platform's per-user cache folder (``~/.cache``, ``~/Library/Caches`` or ``%LOCALAPPDATA%``), then ``kumosql/mcp-cache``.

    It is never inside the project: when the platform folder happens to sit under ``root`` (a cache location
    pointed into the checkout), the system temporary folder is used instead. The file inside is named by a hash
    of the project's path (:func:`cache_file`), so projects do not meet. The folder is created only when a file is saved.
    """

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    folder = base / "kumosql" / "mcp-cache"
    if root is not None and _inside(folder, Path(root)):
        folder = Path(tempfile.gettempdir()) / f"kumosql-mcp-cache-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
    return folder


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to((folder if folder.is_dir() else folder.parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def cache_file(directory: Path, root: Path) -> Path:
    """The file for the project at ``root``: one per project path, so projects do not overwrite each other."""

    name = hashlib.sha256(str(Path(root).resolve()).encode("utf-8", "replace")).hexdigest()[:24]
    return Path(directory) / f"{name}.json"


def _versions() -> dict:
    import sqlglot

    from . import __version__

    return {"kumosql": __version__, "sqlglot": sqlglot.__version__}


def load_cache(path: Path) -> tuple[str, dict] | None:
    """``(fingerprint, entries)`` from a saved file, or ``None`` for one that is missing, corrupt or of another version."""

    try:
        if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        header = data.get("header") if isinstance(data, dict) else None
        if header != {"format": CACHE_NAME, "version": FORMAT, **_versions()}:
            return None
        fingerprint, entries = data.get("fingerprint"), data.get("entries")
        if not isinstance(fingerprint, str) or not isinstance(entries, dict):
            return None
        for key, entry in entries.items():
            if not (isinstance(key, str) and isinstance(entry, dict) and isinstance(entry.get("closure"), str) and isinstance(entry.get("delta"), dict)):
                return None
        return fingerprint, entries
    except (OSError, ValueError, RecursionError, UnicodeError):
        return None


def save_cache(path: Path, fingerprint: str, entries: dict) -> None:
    """Replace the saved file atomically. The folder gets a ``.gitignore`` of ``*``, so it is never committed wherever it sits.

    Safe for several servers on one project at once: each writes its own private temporary file and renames it over the
    saved one, so a reader sees the old file or the new one, never half of either; the last writer wins and no lock exists
    that a killed process could leave behind. Temporary files a killed process left are removed once they are a day old.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("*\n", encoding="utf-8")
        except OSError:
            pass  # another server wrote it at the same moment
    body = {"header": {"format": CACHE_NAME, "version": FORMAT, **_versions()}, "fingerprint": fingerprint, "entries": entries}
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(body, handle, separators=(",", ":"), allow_nan=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    cutoff = time.time() - 86400
    for old in path.parent.glob(".*.tmp"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def session_file(path: Path) -> Path:
    """The file of saved results beside the trace file ``path`` (:func:`cache_file`)."""

    return path.with_name(path.stem + ".session.json")


def _session_header() -> dict:
    return {"format": SESSION_NAME, "loader": load_memo.FORMAT, "results": result_memo.FORMAT, **_versions()}


def load_session(path: Path) -> dict | None:
    """The saved files and results (``{"files", "taken_ns", "loader", "results"}``), or ``None`` for a file that is missing,
    corrupt, from another version or format, or not shaped as :func:`save_session` writes it."""

    try:
        if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("header") != _session_header():
            return None
        files, taken, loader, results = data.get("files"), data.get("taken_ns"), data.get("loader"), data.get("results")
        if not (isinstance(files, dict) and isinstance(taken, int) and not isinstance(taken, bool) and isinstance(loader, dict) and isinstance(results, dict)):
            return None
        shots: dict[str, tuple[int, int, str]] = {}
        for name, item in files.items():
            if not (isinstance(name, str) and isinstance(item, list) and len(item) == 3 and all(isinstance(n, int) and not isinstance(n, bool) for n in item[:2]) and isinstance(item[2], str)):
                return None
            shots[name] = (item[0], item[1], item[2])
        return {"files": shots, "taken_ns": taken, "loader": loader, "results": results}
    except (OSError, ValueError, RecursionError, UnicodeError):
        return None


def save_session(path: Path, files: dict[str, tuple[int, int, str]], taken_ns: int, loader: dict, results: dict) -> None:
    """Replace the saved file atomically (the same way, and in the same folder, as :func:`save_cache`)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("*\n", encoding="utf-8")
        except OSError:
            pass
    body = {"header": _session_header(), "files": {k: list(v) for k, v in files.items()}, "taken_ns": taken_ns, "loader": loader, "results": results}
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(body, handle, separators=(",", ":"), allow_nan=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


# ------------------------------------------------------------------ watching the project's files


def project_files(root: Path, extra: Iterable[Path] = (), skip: Iterable[Path] = ()) -> list[Path]:
    """The files that make up the project at ``root`` (a folder or one compiled graph), plus ``extra`` files, sorted."""

    found: list[Path] = []
    skipped = {Path(p).resolve() for p in skip}
    if root.is_file():
        found.append(root)
    elif root.is_dir():
        for folder, names, files in os.walk(root):
            names[:] = sorted(n for n in names if n not in SKIPPED_FOLDERS and (Path(folder) / n).resolve() not in skipped)
            found.extend(Path(folder) / f for f in sorted(files) if f.endswith(WATCHED_SUFFIXES))
            if len(found) > MAX_WATCHED_FILES:
                break
    found.extend(p for p in extra if p.is_file())
    return found


def snapshot(
    files: Iterable[Path], previous: dict[str, tuple[int, int, str]] | None = None, previous_taken_ns: int | None = None
) -> dict[str, tuple[int, int, str]]:
    """``{path: (mtime_ns, size, sha256)}``. A file whose size and time match ``previous`` is not read again.

    ``previous_taken_ns`` is when ``previous`` was taken (``time.time_ns()``). A file changed within two seconds before
    that moment is read again even when its size and time match: a second edit in the same clock tick would not show.
    ``None`` trusts every match.
    """

    previous = previous or {}
    found: dict[str, tuple[int, int, str]] = {}
    recent = None if previous_taken_ns is None else previous_taken_ns - load_memo.RACY_NS
    for path in files:
        try:
            stat = path.stat()
            before = previous.get(str(path))
            if before is not None and before[:2] == (stat.st_mtime_ns, stat.st_size) and (recent is None or stat.st_mtime_ns <= recent):
                found[str(path)] = before
            else:
                found[str(path)] = (stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            continue  # a file that vanished between the listing and the read counts as removed
    return found


def _contents(shot: dict[str, tuple[int, int, str]]) -> dict[str, str]:
    return {path: sha for path, (_mtime, _size, sha) in shot.items()}


# ------------------------------------------------------------------ the index


class Index:
    """Builds the analysis in the background, answers ``index warming`` until it is ready, and rebuilds after edits.

    ``session`` is the server's :class:`~kumosql.mcp_session.Session`. ``cache`` is the saved file (``None``: save
    nothing). ``watch`` lists the project's files (``None``: edits are not looked for). ``wait_seconds`` is how long
    a tool call waits for a build in progress before answering ``index warming``.
    """

    def __init__(
        self,
        session: Session,
        *,
        cache: Path | None = None,
        watch: Callable[[], list[Path]] | None = None,
        refresh_interval: float = 0.0,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.cache = cache
        self._watch = watch
        self.refresh_interval = max(0.0, refresh_interval)
        self.wait_seconds = max(0.0, wait_seconds)
        self._clock = clock
        self._log = log or (lambda text: print(f"kumosql mcp: {text}", file=sys.stderr))
        self._lock = threading.RLock()
        self._idle = threading.Event()
        self._idle.set()
        self.stage = ""
        self.reason = "starting"
        self.started = clock()
        self.memo: TraceMemo | None = None
        #: what the last finished build did: models traced, models replayed from the saved trace, and how it ended
        self.last: dict = {}
        self._pending: Session | None = None
        self._thread: threading.Thread | None = None
        self._files: dict[str, tuple[int, int, str]] = {}
        self._taken_ns = 0
        self._last_check = float("-inf")
        self._fingerprint: str | None = None
        self._entries: dict = {}
        self.cache_state = "off" if cache is None else "missing"
        if cache is not None:
            saved = load_cache(cache)
            if saved is not None:
                self._fingerprint, self._entries = saved
                self.cache_state = "loaded"
            elif cache.is_file():
                self.cache_state = "ignored"
                self._log("the saved index is unreadable or from another version; it is ignored and will be written again")
        #: saved files and results (the second file, see the module text), what the last build used, and why it was or was not read
        self._saved_files: dict[str, tuple[int, int, str]] = {}
        self._saved_taken_ns = 0
        self._saved_loader: dict = {}
        self._saved_results: dict = {}
        self.load_memo: load_memo.LoadMemo | None = None
        self.results: result_memo.ResultMemo | None = None
        self.results_state = "off" if cache is None else "missing"
        if cache is not None:
            kept = load_session(session_file(cache))
            if kept is not None:
                self._saved_files, self._saved_taken_ns = kept["files"], kept["taken_ns"]
                self._saved_loader, self._saved_results = kept["loader"], kept["results"]
                self.results_state = "loaded"
            elif session_file(cache).is_file():
                self.results_state = "ignored"
                self._log("the saved files and results are unreadable or from another version; they are ignored and will be written again")
        session.index = self
        session.trace_memo_factory = self.new_memo
        session.load_memo_factory = self.new_load_memo
        session.result_memo_factory = self.new_results

    # -- the memo every build uses

    def new_memo(self) -> TraceMemo:
        self.memo = TraceMemo(self._entries, self._fingerprint, on_finish=self._save)
        return self.memo

    def new_load_memo(self) -> load_memo.LoadMemo:
        self.load_memo = load_memo.LoadMemo(self._saved_loader, self._files, self._taken_ns)
        return self.load_memo

    def new_results(self) -> result_memo.ResultMemo:
        self.results = result_memo.ResultMemo(self._saved_results)
        return self.results

    def _save_session(self, target: Session) -> None:
        """Keep what the build used (for the next refresh) and save it, unless nothing changed since the saved file."""

        memo, results = self.load_memo, self.results
        if memo is None or results is None or target._pipeline is None:
            return
        counts = results.counts()
        fresh = bool(memo.parsed or memo.resolved or any(v for k, v in counts.items() if k.endswith("_computed")))
        # a file changed just before the saved snapshot was taken is read again on every start until a newer snapshot is saved
        racy = any(mtime > self._saved_taken_ns - load_memo.RACY_NS for mtime, _size, _sha in self._saved_files.values())
        unchanged = (not fresh and not racy and self.results_state == "loaded" and self._files == self._saved_files
                     and set(memo.entries) == set(self._saved_loader) and all(len(results.entries[n]) == len(self._saved_results.get(n, ())) for n in result_memo.SECTIONS))
        loader, kept = memo.entries, results.to_json()
        if self.cache is None or unchanged:
            self._saved_loader, self._saved_results = loader, kept
            return
        try:
            save_session(session_file(self.cache), dict(self._files), self._taken_ns, loader, kept)
            self.results_state = "saved"
            self._saved_files, self._saved_taken_ns = dict(self._files), self._taken_ns
        except (OSError, ValueError, TypeError) as exc:
            self.results_state = "not saved"
            self._log(f"the saved files and results could not be written ({type(exc).__name__}); the next start reads and analyses again")
        self._saved_loader, self._saved_results = loader, kept

    def _save(self, fingerprint: str, entries: dict) -> None:
        unchanged = fingerprint == self._fingerprint and set(entries) == set(self._entries) and all(entries[k] is self._entries.get(k) for k in entries)
        self._fingerprint, self._entries = fingerprint, entries
        if self.cache is None or unchanged:
            return
        try:
            save_cache(self.cache, fingerprint, entries)
            self.cache_state = "saved"
        except (OSError, ValueError, TypeError) as exc:
            self.cache_state = "not saved"
            self._log(f"the index could not be saved ({type(exc).__name__}); the next start analyses again")

    # -- building

    @property
    def building(self) -> bool:
        return not self._idle.is_set()

    def start(self) -> threading.Thread:
        """Start the first build in a thread (a second start is a no-op while one runs)."""

        with self._lock:
            if self.building and self._thread is not None:
                return self._thread
            self._begin("starting")
            if self._watch is not None:
                taken = time.time_ns()
                self._files = snapshot(self._watch(), self._saved_files, self._saved_taken_ns)
                self._taken_ns = taken
            self._thread = threading.Thread(target=self._build, args=(self.session, False), name="kumosql-mcp-index", daemon=True)
            self._thread.start()
            return self._thread

    def _begin(self, reason: str) -> None:
        self.reason, self.stage, self.started = reason, "loading project", self._clock()
        self._idle.clear()

    def _build(self, target: Session, swap: bool) -> None:
        failed = False
        try:
            target.warm(self._set_stage)
        except Exception as exc:  # noqa: BLE001 - a failed build must never leave the server waiting
            failed = True
            self._log(f"the index build stopped ({type(exc).__name__}); questions compute what they need")
        finally:
            memo = getattr(target._pipeline, "trace_memo", None)
            traced, reused = (len(memo.traced), len(memo.reused)) if memo is not None else (0, 0)
            if not failed:
                self._save_session(target)
            loaded, results = self.load_memo, self.results
            extra = {}
            if loaded is not None:
                extra.update({"files_read": len(loaded.read), "files_parsed": len(loaded.parsed), "files_replayed": len(loaded.replayed),
                              "assets_resolved": len(loaded.resolved)})
            if results is not None:
                extra.update(results.counts())
            with self._lock:
                self.last = {"traced": traced, "reused": reused, "seconds": round(self._clock() - self.started, 2), **extra}
                if swap:
                    self._pending = target
                self.stage = ""
                self._idle.set()
            self._log(f"index ready: {traced} models analysed, {reused} replayed from the saved index")

    def _set_stage(self, stage: str) -> None:
        self.stage = stage

    def wait_ready(self, seconds: float | None = None) -> bool:
        """Block until no build is running (at most ``seconds``); True when ready."""

        return self._idle.wait(seconds)

    def percent(self) -> int:
        if not self.building:
            return 100
        stage, memo = self.stage, self.memo
        if stage == "loading project":
            return _LOAD
        if stage == "tracing columns":
            return _TRACE_FROM + (_TRACE_SPAN * memo.percent()) // 100 if memo is not None else _TRACE_FROM
        if stage == "profiling tables":
            return _PROFILES
        if stage == "inferring roles":
            return _ROLES
        return 0

    def status(self) -> dict:
        """``{"state": "ready"|"warming", "percent", "stage", ...}``: what a build is doing, and what the last one did."""

        building = self.building
        out = {"state": "warming" if building else "ready", "percent": self.percent(), "cache": self.cache_state, "results_cache": self.results_state}
        if building:
            out["stage"] = self.stage
            out["reason"] = self.reason
        out.update({f"last_{k}": v for k, v in self.last.items()})
        return out

    # -- edits

    def _adopt_pending(self) -> None:
        with self._lock:
            fresh, self._pending = self._pending, None
        if fresh is not None:
            self.session.adopt(fresh)

    def check_for_edits(self) -> bool:
        """Compare the project's files with the last build (at most once per ``refresh_interval``); rebuild on a change.

        Returns True when a rebuild was started. Files are compared by size and time first, then by content hash, so
        saving a file without changing it, or touching it, does not rebuild anything.
        """

        if self._watch is None or self.building:
            return False
        now = self._clock()
        if now - self._last_check < self.refresh_interval:
            return False
        self._last_check = now
        taken = time.time_ns()
        shot = snapshot(self._watch(), self._files, self._taken_ns)
        changed = _contents(shot) != _contents(self._files)
        self._files, self._taken_ns = shot, taken
        if not changed:
            return False
        with self._lock:
            if self.building:
                return False
            self._begin("the project changed")
            fresh = Session(
                loader=self.session._loader,
                profiles=self.session.profiles_directory,
                assume_grain=self.session._assume_grain,
                root=self.session.root,
                boundary=self.session._boundary_source,
                docs=self.session._docs,
                malloy=self.session._malloy,
            )
            fresh.trace_memo_factory = self.new_memo
            fresh.load_memo_factory = self.new_load_memo
            fresh.result_memo_factory = self.new_results
            threading.Thread(target=self._build, args=(fresh, True), name="kumosql-mcp-refresh", daemon=True).start()
        return True

    # -- what a tool call does first

    def before_call(self) -> dict | None:
        """``None`` when the index is ready, else the ``index warming`` answer (after waiting up to ``wait_seconds``)."""

        self._adopt_pending()
        self.check_for_edits()
        if self.building and not self._idle.wait(self.wait_seconds):
            return self.warming_answer()
        self._adopt_pending()
        return None

    def warming_answer(self) -> dict:
        percent = self.percent()
        elapsed = max(self._clock() - self.started, 0.0)
        again = 5 if percent < 3 else max(1, min(30, round(elapsed * (100 - percent) / percent)))
        why = "after an edit to the project" if self.reason == "the project changed" else "after the server started"
        answer = Answer(f"index warming, {percent}% done: no answer yet, ask again in about {again} s")
        answer.fields["index"] = {"state": "warming", "percent": percent, "stage": self.stage or "starting", "reason": why, "ask_again_in_seconds": again}
        answer.next(f"Ask the same question again in about {again} seconds. Nothing is wrong: the project is being analysed {why}, and an answer is only given from a finished analysis.")
        return render(answer)
