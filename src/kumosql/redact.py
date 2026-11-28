"""Replace names with stable placeholders so a log can be pasted without revealing them.

The console, ``ui.log`` and the diagnostics bundle all pass text through
:func:`scrub`. Repository URLs, branches, GCP projects, datasets, tables, model
names, file paths, e-mail addresses, the user name and the home folder become
placeholders such as ``repo#1``, ``project#2``, ``model#417``, ``file#88`` and
``user~``. The same value gets the same placeholder for the whole session, so a
sequence of lines can still be followed.

The placeholder-to-real map is written to ``redaction-map.json`` in the local
data folder (the last few sessions are kept). It is never part of the
diagnostics bundle. ``python -m kumosql.ui --lookup model#417`` reads it.

Name redaction is on by default. ``--no-redact`` (or ``KUMOSQL_NO_REDACT=1``)
turns it off for local debugging; credentials are always removed and the
diagnostics bundle is scrubbed regardless.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from .resilience import replace_file

KINDS = ("repo", "branch", "project", "dataset", "table", "model", "file", "email", "name", "host")
MAP_NAME = "redaction-map.json"
KEEP_SESSIONS = 5
MIN_LENGTH = 3
# Names that are also words KumoSQL writes in its own messages; registering them would garble the log.
_COMMON = frozenset((
    "model", "file", "repo", "project", "dataset", "table", "failed", "error", "step", "load", "git", "main",
    "master", "head", "start", "done", "the", "and", "for", "with", "path", "name", "none", "true", "false",
    "stage", "repository", "branch", "query", "graph", "files", "models", "info", "warn", "data", "cache",
))
_KEEP_BRANCHES = frozenset(("main", "master", "develop", "dev", "trunk", "head"))

_SECRET = re.compile(
    r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|ya29\.[A-Za-z0-9._-]{20,}|AIza[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{12,}")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|access[_-]?(?:key|token)|refresh[_-]?token|client[_-]?secret|private[_-]?key|authorization)"
    r"([\"']?\s*[=:]\s*)(?:(?:basic|bearer|token)\s+(?=\S))?(?:\"(?:\\.|[^\"\\])*(?:\"|$)|'(?:\\.|''|[^'\\])*(?:'|$)|(?!<)[^\s,;&'\"]+)")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z0-9 ]* )?PRIVATE KEY-----|$)", re.S)
# SQL and value dumps cannot be made safe by guessing which names are private.
_DATA_PAYLOAD = re.compile(
    r"(?i)\b(?:select\s+(?!projects?\b).*\bfrom\b|insert\s+into\b|update\s+\S+\s+set\b|delete\s+from\b|merge\s+into\b|with\s+\S+\s+as\s*\()"
    r"|\b(?:sql|query|rows?|results|vars|variables|parameters|bindings)[\"']?\s*[=:]\s*[\"'\[({]"
    r"|(?:^|\s)[\[{(]\s*[\"']"
    r"|(?:\b(?:project|dataset|table|model)\s*[=:]\s*[\"'])")
_URL = re.compile(r"""(?i)\b(?:https?|ssh|git|ftp|file)://[^\s'"`<>)\]]+""")
_SCP_URL = re.compile(r"(?<![\w@./-])[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}:[A-Za-z0-9._~/-][^\s'\"`<>)]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_QUOTED_PATH = re.compile(r"""(?P<q>['"])(?P<p>(?:[A-Za-z]:[\\/]|\\\\+|/)[^'"\r\n]*)(?P=q)""")
_POSIX_PATH = re.compile(r"(?<![\w:/.~$#>])(?:~|)/(?:[^\s/'\"`<>:;,()\[\]{}|*?]+/)*[^\s/'\"`<>:;,()\[\]{}|*?]*")
_WIN_PATH = re.compile(r"""(?<![\w])(?:[A-Za-z]:[\\/]+|\\\\+[^\s\\/]+[\\/]+|~[\\/]+)(?:[^\s\\/'"`<>|:*?]+[\\/]+)*[^\s\\/'"`<>|:*?]*""")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# sqlglot appends a copy of the SQL around the error after "Line N, Col: M."; keep the position, drop the SQL.
_SQL_EXCERPT = re.compile(r"(Line \d+, Col: \d+\.?)(?:\s.*)?$", re.S)
_HOST = re.compile(r"(?i)\b(host(?:name)?\s+)([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+)")
_BARE_IDENT = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\.[A-Za-z_]\w*\.[A-Za-z_]\w*\b")
_QUOTED_IDENT = re.compile(r"`([^`\n]{1,300})`")
_TRACE_PREFIX = "  "


def enabled_by_default() -> bool:
    return os.environ.get("KUMOSQL_NO_REDACT", "").strip().lower() not in ("1", "true", "yes")


class Redactor:
    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = enabled_by_default() if enabled is None else bool(enabled)
        self.session = secrets.token_hex(2)
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._lock = threading.RLock()
        self._counts: dict[str, int] = {}
        self._by_value: dict[tuple[str, str], str] = {}  # (kind, value) -> placeholder
        self._known: dict[str, str] = {}  # value -> placeholder, for exact replacement
        self._regex: re.Pattern | None = None
        self._dirty_map = False
        self._last_write = 0.0
        self._home = ""
        self._username = ""
        self._safe_prefixes: list[tuple[str, str, bool]] = []  # (prefix, token, keep the rest)
        self._detect_environment()

    # ---------- what is private ----------

    def _detect_environment(self) -> None:
        try:
            home = str(Path.home())
        except Exception:  # noqa: BLE001 - no home folder (service account)
            home = ""
        self._home = home
        user = os.environ.get("USERNAME") or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not user and home:
            user = re.split(r"[\\/]", home.rstrip("\\/"))[-1]
        self._username = user if len(user) >= MIN_LENGTH and user.lower() not in _COMMON else ""
        package = str(Path(__file__).resolve().parent)
        prefixes = [(package, "kumosql", True)]
        for name in {sys.prefix, sys.base_prefix, getattr(sys, "exec_prefix", "")}:
            if name:
                prefixes.append((name, "python", True))
        self._safe_prefixes = sorted(prefixes, key=lambda item: -len(item[0]))

    def set_data_folder(self, folder: str | Path) -> None:
        """The local data folder (and what lives in it) is KumoSQL's own, so its sub-paths stay readable."""

        text = str(folder)
        with self._lock:
            self._safe_prefixes = [item for item in self._safe_prefixes if item[1] != "data"]
            self._safe_prefixes.append((text, "data", True))
            self._safe_prefixes.sort(key=lambda item: -len(item[0]))

    # ---------- placeholders ----------

    def _placeholder(self, kind: str, number: int, value: str) -> str:
        if kind in ("file",):
            parts = [part for part in re.split(r"[\\/]", value.strip("\\/")) if part]
            suffix = Path(parts[-1]).suffix if parts else ""
            suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) else ""
            return "/".join(["dir"] * max(0, len(parts) - 1) + [f"file#{number}{suffix}"])
        return f"{kind}#{number}"

    def ref(self, kind: str, value: object) -> str:
        """The placeholder for ``value`` (registering it); the real text when redaction is off."""

        text = sanitize_credentials(str(value or ""))
        if not text:
            return text
        if not self.enabled:
            return text
        return self._register(kind, text)

    def _register(self, kind: str, text: str) -> str:
        text = sanitize_credentials(text).strip()
        if not text:
            return text
        with self._lock:
            found = self._by_value.get((kind, text))
            if found:
                return found
            if kind == "branch" and text.lower() in _KEEP_BRANCHES:
                return text
            known = self._known.get(text)
            if known:
                return known
            number = self._counts.get(kind, 0) + 1
            self._counts[kind] = number
            placeholder = self._placeholder(kind, number, text)
            self._by_value[(kind, text)] = placeholder
            if len(text) >= MIN_LENGTH and text.lower() not in _COMMON:
                self._known[text] = placeholder
                self._regex = None
            self._dirty_map = True
            if kind == "repo":  # the short name git reports ("owner/repo.git" -> "repo") is the same repository
                for derived in _repo_names(text):
                    if len(derived) >= MIN_LENGTH and derived.lower() not in _COMMON and derived not in self._known:
                        self._known[derived] = placeholder
                        self._by_value[(kind, derived)] = placeholder
            self._flush_soon()
            return placeholder

    def register(self, kind: str, values: object) -> None:
        """Remember private names so any later mention is replaced. Accepts one value or an iterable."""

        if not self.enabled or values is None:
            return
        items = [values] if isinstance(values, (str, Path)) else list(values)
        for item in items:
            if isinstance(item, (str, Path)) and str(item).strip():
                self._register(kind, str(item))

    # ---------- scrubbing ----------

    def scrub(self, text: object, force: bool = False) -> str:
        value = sanitize_credentials(text if isinstance(text, str) else str(text))
        if not (self.enabled or force):
            return value
        try:
            return self._scrub(value)
        except Exception:  # noqa: BLE001 - a logging failure must never leak or crash; drop the text instead
            return "<log text withheld: redaction failed>"

    def _scrub(self, text: str) -> str:
        text = _ANSI.sub("", text)
        text = _SQL_EXCERPT.sub(lambda m: m.group(1) + " <SQL excerpt withheld>", text)
        text = sanitize_credentials(text)
        if _DATA_PAYLOAD.search(text):
            return "<data payload withheld>"
        text = _URL.sub(lambda m: self._url(m.group(0)), text)
        text = _SCP_URL.sub(lambda m: self._url(m.group(0)), text)
        text = _HOST.sub(lambda m: m.group(1) + self._register("host", m.group(2)), text)
        text = _EMAIL.sub(lambda m: self._register("email", m.group(0)), text)
        text = self._home_forms(text)
        text = _QUOTED_PATH.sub(lambda m: m.group("q") + self._path(m.group("p")) + m.group("q"), text)
        text = _WIN_PATH.sub(lambda m: self._path(m.group(0)), text)
        text = _POSIX_PATH.sub(lambda m: self._path(m.group(0)), text)
        text = _QUOTED_IDENT.sub(lambda m: "`" + self._identifier(m.group(1)) + "`", text)
        text = _BARE_IDENT.sub(lambda m: self._identifier(m.group(0)), text)
        regex = self._compiled()
        if regex is not None:
            text = regex.sub(lambda m: self._known.get(m.group(0), m.group(0)), text)
        if self._username:
            text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(self._username)}(?![A-Za-z0-9_])", "user~", text, flags=re.I)
        return text

    def _compiled(self) -> re.Pattern | None:
        with self._lock:
            if self._regex is None and self._known:
                names = sorted(self._known, key=len, reverse=True)
                self._regex = re.compile(r"(?<![A-Za-z0-9_#])(?:" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_])")
            return self._regex

    def _url(self, url: str) -> str:
        core, tail = _split_tail(url)
        if re.match(r"(?i)https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:/|$)", core):
            return url  # this server's own address
        if core.lower().startswith("file://"):
            return self._path(core[7:]) + tail
        return self._register("repo", core) + tail

    def _home_forms(self, text: str) -> str:
        home = self._home
        if len(home) < 4 or home in ("/", "\\"):
            return text
        for form in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            text = re.sub(re.escape(form) + r"(?=[\\/]|\b|$)", "~", text, flags=re.I if "\\" in form or ":" in form else 0)
        return text

    def _path(self, path: str) -> str:
        core, tail = _split_tail(path)
        normal = core.replace("\\", "/")
        if normal in ("/", "") or re.fullmatch(r"/(?:api|assets|static)(?:/.*)?", normal):
            return path  # a URL route, not a file
        if len([p for p in normal.split("/") if p]) < 2 and not re.match(r"^[A-Za-z]:|^\\\\|^~", core):
            return path  # "/ok" or "and/or" style text
        for prefix, token, keep in self._safe_prefixes:
            base = prefix.replace("\\", "/").rstrip("/")
            if base and (normal == base or normal.startswith(base + "/")):
                rest = normal[len(base):]
                return (f"<{token}>{rest}" if keep else f"<{token}>") + tail
        return self._register("file", core) + tail

    def _identifier(self, ident: str) -> str:
        parts = ident.split(".")
        if not all(re.fullmatch(r"[A-Za-z0-9_\-:]+", part) for part in parts) or len(parts) > 3:
            return ident
        kinds = {1: ("model",), 2: ("dataset", "table"), 3: ("project", "dataset", "table")}[len(parts)]
        return ".".join(self._register(kind, part) for kind, part in zip(kinds, parts))

    # ---------- the private map ----------

    def mapping(self) -> dict[str, str]:
        with self._lock:
            return {placeholder: value for (_, value), placeholder in self._by_value.items()}

    def _flush_soon(self) -> None:
        now = time.monotonic()
        if now - self._last_write >= 2.0:
            self.write_map()

    def write_map(self) -> None:
        """Save the placeholder map (never raises). Called as names appear and at exit."""

        with self._lock:
            if not self.enabled or not self._dirty_map:
                return
            self._last_write = time.monotonic()
            entries = {f"{kind}:{placeholder}": value for (kind, value), placeholder in self._by_value.items()}
        try:
            from . import state

            path = state.data_dir() / MAP_NAME
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
            sessions = existing.get("sessions") if isinstance(existing.get("sessions"), dict) else {}
            # Old sessions may contain URL credentials from releases before this guard.
            for body in sessions.values():
                if isinstance(body, dict) and isinstance(body.get("map"), dict):
                    body["map"] = {key: sanitize_credentials(str(value)) for key, value in body["map"].items()}
            sessions[self.session] = {"started": self.started, "map": entries}
            for stale in sorted(sessions, key=lambda key: sessions[key].get("started", ""))[:-KEEP_SESSIONS]:
                sessions.pop(stale, None)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"note": "Private. Real names behind the placeholders in the log. Never share this file.",
                                       "sessions": sessions}, indent=1), encoding="utf-8")
            replace_file(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self._dirty_map = False
        except Exception:  # noqa: BLE001
            pass


def lookup(placeholder: str, path: Path | None = None) -> list[tuple[str, str, str]]:
    """``[(session, placeholder, real value)]`` for a placeholder (``model#417``), searching saved sessions."""

    from . import state

    try:
        data = json.loads((path or state.data_dir() / MAP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    wanted = placeholder.strip().lower()
    found = []
    for session, body in (data.get("sessions") or {}).items():
        for key, value in (body.get("map") or {}).items():
            token = key.split(":", 1)[-1]
            if token.lower() == wanted or token.lower().split("/")[-1].split(".")[0] == wanted.split("/")[-1].split(".")[0]:
                found.append((session, token, sanitize_credentials(str(value))))
    return found


def _split_tail(text: str) -> tuple[str, str]:
    stripped = text.rstrip(".,;:!?'\"")
    return stripped, text[len(stripped):]


def strip_url_userinfo(url: str) -> str:
    """Drop URL authority userinfo without parsing/re-emitting a possibly malformed URL."""

    def strip(m: re.Match) -> str:
        # An SSH login name (ssh://git@host) is not a secret; only a password after ``:`` or an HTTP(S) userinfo is.
        if m.group(1).lower() in {"ssh://", "git://"} and ":" not in m.group(2):
            return m.group(0)
        return m.group(1)

    return re.sub(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/\s?#]*@)", strip, url)


def sanitize_credentials(text: str) -> str:
    """Remove credential material without retaining it in the private name map."""

    text = strip_url_userinfo(text)
    text = _PRIVATE_KEY.sub("secret~", text)
    text = _SECRET.sub("secret~", text)
    return _ASSIGNED_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}secret~", text)


def _repo_names(url: str) -> list[str]:
    cleaned = re.sub(r"(?:\.git)?[/\\]*$", "", url)
    parts = [p for p in re.split(r"[/\\:@]", cleaned) if p]
    names = []
    if parts:
        names.append(parts[-1])
    if len(parts) >= 2:
        names.append("/".join(parts[-2:]))
    return names


GLOBAL = Redactor()
atexit.register(GLOBAL.write_map)
