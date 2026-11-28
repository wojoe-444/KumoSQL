"""Keep failures local: one unreadable asset becomes a diagnostic, not a lost report.

Messages built here describe *what kind* of failure happened. They never echo
file contents, decoder byte dumps or exception text that may embed either.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from pathlib import Path
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

# Diagnostic codes that mean "this asset was not (fully) analyzed", as opposed
# to informational notes such as tables outside the pipeline.
ASSET_FAILURE_CODES = frozenset(
    {
        "read_error",
        "unreadable_directory",
        "settings_unreadable",
        "invalid_entry",
        "duplicate_model",
        "sqlx_parse_error",
        "unsupported_ref",
        "dynamic_config",
        "parse_error",
        "no_query",
        "qualify_error",
        "lineage_error",
        "lineage_skipped",
        "unexpanded_star",
        "section_failed",
        "cycle",
        "unknown_reads",
        "unresolved_template",
        "skipped_statements",
        "unparsed_operation",
        "ambiguous_reference",
        "insert_target_columns",
        "template_columns",
        # Scheduled-job SQL loaded beside the project (kumosql.job_sql).
        "job_sql_unread",
        "job_writer_ambiguous",
        "job_writer_missing",
        "job_destination_ambiguous",
        # A view the project reads whose definition BigQuery would not give (kumosql.view_definitions).
        "view_definition_unreadable",
    }
)

# Views that a gap makes unreliable. A result in one of these views is only
# complete when no blocking gap lists it.
VIEWS = ("graph", "lineage", "impact", "dead_columns")

_EFFECTS = {
    "cycle": ("graph", "impact"),
    "external_tables": ("lineage",),
    "unresolved_template": ("graph", "lineage", "impact", "dead_columns"),
    "lineage_skipped": ("lineage", "impact", "dead_columns"),
    "insert_target_columns": ("lineage", "impact", "dead_columns"),
    "template_columns": ("impact", "dead_columns"),
    # A computed type changes what a model's stored rows are, not what it reads: only proofs depend on it.
    "dynamic_config": (),
    # A declared table whose writer is not settled: the trace stops there, and a change to the jobs is not followed.
    "job_writer_ambiguous": ("lineage", "impact"),
    "job_writer_missing": ("lineage", "impact"),
    "job_destination_ambiguous": ("lineage", "impact"),
    "view_definition_unreadable": ("lineage", "impact"),
}

# Kinds shared with the graph page's gaps table; other codes keep their own name.
_GAP_KINDS = {
    "read_error": "inaccessible",
    "unreadable_directory": "inaccessible",
    "settings_unreadable": "inaccessible",
    "invalid_entry": "inaccessible",
    "parse_error": "parse_error",
    "no_query": "parse_error",
    "sqlx_parse_error": "parse_error",
    "unsupported_ref": "parse_error",
    "dynamic_config": "unmatched_reference",
    "qualify_error": "parse_error",
    "lineage_error": "parse_error",
    "lineage_skipped": "lineage_skipped",
    "ambiguous_reference": "unmatched_reference",
    "external_tables": "unmatched_reference",
    "unknown_reads": "unattributed_reads",
    "unresolved_template": "unmatched_reference",
    "job_sql_unread": "parse_error",
    "job_destination_ambiguous": "unmatched_reference",
}


class PipelineLoadError(ValueError):
    """The whole input is unusable (missing root, invalid graph JSON)."""


def describe_os_error(exc: BaseException) -> str:
    """A content-free description of an I/O or decoding failure."""

    if isinstance(exc, UnicodeError):
        return "file is not valid UTF-8 text"
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return "permission denied"
    if isinstance(exc, FileNotFoundError):
        return "file not found or a broken link"
    if isinstance(exc, IsADirectoryError):
        return "path is a directory, not a file"
    if isinstance(exc, OSError):
        return "file could not be read"
    return f"unexpected {type(exc).__name__}"


def _extended_text(text: str) -> str:
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def extended_path(path: str | Path) -> Path:
    """``path`` made absolute, with Windows' extended-length prefix so paths over 260 characters work.

    No administrator setting is needed. Elsewhere the path is returned unchanged. Applied to a folder
    root, every path derived from it (``/``, ``rglob``, ``os.walk``) keeps the prefix.
    """

    path = Path(path)
    if os.name != "nt":
        return path
    return Path(_extended_text(os.path.abspath(path)))


def decode_text(data: bytes) -> str:
    """UTF-8 (a byte-order mark is dropped, as it would hide a leading config block), else Windows-1252-style latin-1.

    Both the git reader and the local-folder reader use this, so a file loads the same either way.
    """

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # only comments and strings differ; keep the model rather than dropping it


def read_text_or_reason(path: Path) -> tuple[str | None, str | None]:
    """Return ``(text, None)`` or ``(None, reason)``; never raises for I/O errors."""

    try:
        # Refuse even in-root file links, including settings files read outside find_assets.
        if path.is_symlink():
            return None, "symbolic links are not read"
        return decode_text(path.read_bytes()), None
    except (OSError, UnicodeError) as exc:
        return None, describe_os_error(exc)


def find_assets(
    root: Path, suffixes: Iterable[str], on_error: Callable[[Path, str], None]
) -> list[Path]:
    """Sorted files under ``root`` with one of ``suffixes``.

    Unlistable directories are reported through ``on_error`` and skipped.
    """

    wanted = tuple(suffixes)
    found: list[Path] = []
    # The root itself may be a link the user chose (``--project ~/link``); only links below it are pruned.
    def walk_error(exc: OSError) -> None:
        on_error(Path(exc.filename) if exc.filename else root, describe_os_error(exc))

    resolved_root = root.resolve()
    for directory, dirs, files in os.walk(root, onerror=walk_error):
        # Explicitly prune junctions and other directory links as well as symlinks.
        for name in list(dirs):
            path = Path(directory) / name
            try:
                if _linked_directory(path) or not path.resolve().is_relative_to(resolved_root):
                    dirs.remove(name)
                    on_error(path, "linked directories are not read")
            except OSError as exc:
                dirs.remove(name)
                on_error(path, describe_os_error(exc))
        for name in files:
            if not name.endswith(wanted):
                continue
            path = Path(directory) / name
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(resolved_root):
                    on_error(path, "symbolic links are not read")
                    continue
            except OSError as exc:
                on_error(path, describe_os_error(exc))
                continue
            found.append(path)
    return sorted(found)


_WINDOWS_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[0-9\u00b9\u00b2\u00b3])")


def is_windows_device_name(part: str) -> bool:
    """A path component Windows treats as a device (``CON``, ``NUL.sqlx``, ``COM1``); ``is_reserved`` is deprecated."""

    return bool(_WINDOWS_DEVICE.fullmatch(part.split(".", 1)[0].rstrip(" ")))


def unsafe_checkout_path(path: str) -> bool:
    """True for a repository path that cannot be written safely into a checkout on any supported system."""

    return (
        not path or ":" in path or "\\" in path or "\0" in path or path.startswith("/")
        or any(p in ("", ".", "..") or p.endswith((".", " ")) or is_windows_device_name(p) for p in path.split("/"))
    )


def _linked_directory(path: Path) -> bool:
    """Recognize Windows junctions even on Python 3.11, before Path.is_junction existed."""

    if path.is_symlink():
        return True
    tag = getattr(path.stat(follow_symlinks=False), "st_reparse_tag", 0)
    return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)


def parse_json_or_raise(path: Path, what: str) -> object:
    """Parse a whole-input JSON file; failures raise one content-free error."""

    text, reason = read_text_or_reason(path)
    if text is None:
        raise PipelineLoadError(f"{what} could not be read: {reason}")
    try:
        return json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", None)
        where = f" (line {line})" if line else ""
        raise PipelineLoadError(f"{what} is not valid JSON{where}") from None


def guarded(default: T, action: Callable[[], T]) -> tuple[T, str | None]:
    """Run ``action``; on any exception return ``default`` and the exception class name."""

    try:
        return action(), None
    except Exception as exc:  # noqa: BLE001 - isolation boundary by design
        return default, type(exc).__name__


def diagnostic_entry(model: str, code: str, message: str) -> dict:
    """JSON shape for the UI: ``asset`` and ``message`` plus the stable ``model``/``code`` keys."""

    return {
        "asset": model,
        "model": model,
        "code": code,
        "message": message,
        "analysis_incomplete": code in ASSET_FAILURE_CODES,
        "severity": "error" if code in ASSET_FAILURE_CODES else "info",
        "effect": list(_EFFECTS.get(code, VIEWS)),
    }


def summarize(entries: list[dict]) -> dict:
    incomplete = [e for e in entries if e.get("analysis_incomplete")]
    return {
        "count": len(entries),
        "assets_not_analyzed": len({e["asset"] for e in incomplete}),
        "analysis_incomplete": bool(incomplete),
    }


def build_completeness(entries: list[dict], extra_gaps: Iterable[dict] = ()) -> dict:
    """Summarize what the analysis could not see, and which views that affects.

    ``entries`` are :func:`diagnostic_entry` dicts. ``extra_gaps`` are gaps from
    other sources (job history); each needs ``asset``, ``kind``, ``message``.
    ``complete`` is false when any blocking gap exists. ``views`` says, per view,
    whether its results can be treated as complete. Informational gaps (tables
    outside the pipeline) are listed but do not block.
    """

    gaps: list[dict] = []
    for entry in entries:
        blocking = bool(entry.get("analysis_incomplete"))
        if not blocking and entry["code"] != "external_tables":
            continue
        gaps.append(
            {
                "asset": entry["asset"],
                "kind": _GAP_KINDS.get(entry["code"], entry["code"]),
                "code": entry["code"],
                "message": entry["message"],
                "blocking": blocking,
                "effect": entry.get("effect", list(VIEWS)),
                "origin": "static",
            }
        )
    for gap in extra_gaps:
        gaps.append({"code": gap["kind"], "blocking": True, "effect": list(VIEWS), "origin": "observed", **gap})
    blocking_gaps = [g for g in gaps if g["blocking"]]
    by_code: dict[str, int] = {}
    for gap in gaps:
        by_code[gap["code"]] = by_code.get(gap["code"], 0) + 1
    return {
        "complete": not blocking_gaps,
        "assets_not_analyzed": len({g["asset"] for g in blocking_gaps if g["origin"] == "static"}),
        "views": {view: not any(view in g["effect"] for g in blocking_gaps) for view in VIEWS},
        "by_code": dict(sorted(by_code.items())),
        "gaps": gaps,
    }
