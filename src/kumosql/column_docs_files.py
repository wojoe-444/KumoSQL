"""Column descriptions from Markdown documentation kept next to a project (``--docs DIR``), read-only.

Teams often document their tables in Markdown: a heading that names the table or view, then a table of its columns::

    ## orders_summary

    | Column | Type | Description |
    | --- | --- | --- |
    | `created_at` | TIMESTAMP | When the order was placed (UTC). |

This reads those tables and keeps each description with the file it came from. Rules, so a doc never passes for more than it says:

* A table matches a table of the project only by name: a heading (or backticked name in one, or the file name when the page has
  no matching heading) that names exactly one table of the project. Anything else is not used and counted.
* A description is the lowest-priority text: the config's ``columns`` block and the sidecar file replace it.
* A mention of a time zone inside a description (``UTC``, ``local time``, ``America/New_York``) is kept as a hint, shown as
  "docs mention UTC (not declared)". It is never a declared zone: nothing checks queries against it.
* Two files describing the same column: the first (in sorted path order) is kept, and the conflict is noted.

The folder is only read. Symbolic links, huge files and folders such as ``node_modules`` are skipped; nothing is written.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_FILES = 2000
MAX_BYTES = 1_000_000
SKIP = frozenset({"node_modules", ".git", ".venv", "venv", "__pycache__"})

_NAME_HEADERS = frozenset({"column", "columns", "field", "name", "column name", "field name"})
_TEXT_HEADERS = frozenset({"description", "meaning", "notes", "note", "comment", "definition", "details"})
_ZONE = re.compile(r"\b(UTC|GMT)\b|\blocal time\b|\b(?:America|Europe|Asia|Australia|Africa|Pacific|Atlantic)/[A-Za-z_]+(?:/[A-Za-z_]+)?", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def zone_hint(text: str) -> str:
    """The first time zone a description mentions (``UTC``, ``local time``, an IANA name), or ``""``."""

    match = _ZONE.search(text)
    if not match:
        return ""
    found = match.group(0)
    return found.upper() if found.casefold() in ("utc", "gmt") else found


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [" ".join(cell.replace("\\|", "|").split()) for cell in re.split(r"(?<!\\)\|", text)]


def _plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return " ".join(text.replace("**", "").replace("__", "").replace("`", "").split())


def parse_page(text: str, stem: str = "") -> list[tuple[list[str], str, str]]:
    """``(table name candidates, column, description)`` for every row of every column table of one Markdown page.

    The candidates are the names in the nearest headings above the table (nearest first), then the file's own name.
    """

    lines = text.splitlines()
    headings: list[str] = []  # headings above the current line, nearest last
    rows: list[tuple[list[str], str, str]] = []
    index, fence = 0, False
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence:
            match = _HEADING.match(line)
            if match:
                headings.append(match.group(1))
            elif "|" in line and index + 1 < len(lines) and _SEPARATOR.match(lines[index + 1]):
                header = [c.casefold().strip("` *") for c in _cells(line)]
                name_at = next((i for i, c in enumerate(header) if c in _NAME_HEADERS), None)
                text_at = next((i for i, c in enumerate(header) if c in _TEXT_HEADERS), None)
                index += 2
                body = []
                while index < len(lines) and "|" in lines[index] and lines[index].strip():
                    body.append(_cells(lines[index]))
                    index += 1
                if name_at is not None and text_at is not None:
                    names = _candidates(headings[-3:][::-1]) + ([stem] if stem else [])
                    for cells in body:
                        if len(cells) > max(name_at, text_at):
                            column = _plain(cells[name_at])
                            description = _plain(cells[text_at])
                            if re.fullmatch(r"[A-Za-z_][\w]*", column) and description:
                                rows.append((names, column, description))
                continue
        index += 1
    return rows


def _candidates(headings: list[str]) -> list[str]:
    """Names a heading may give a table: its backticked spans, else its words that look like a table name."""

    found: list[str] = []
    for heading in headings:
        spans = re.findall(r"`([^`]+)`", heading)
        words = re.findall(r"[A-Za-z_][\w.\-]*", heading) if not spans else []
        for item in spans or words:
            if item not in found:
                found.append(item)
    return found


def markdown_files(folders) -> list[Path]:
    """The Markdown files ``apply_docs`` reads under ``folders`` (a folder or a single file each), in sorted path order."""

    found: list[Path] = []
    for folder in folders:
        folder = Path(folder)
        if folder.is_file():
            found.append(folder)
        elif folder.is_dir():
            found.extend(sorted(p for p in folder.rglob("*.md") if not SKIP.intersection(p.relative_to(folder).parts))[:MAX_FILES])
    return found


def apply_docs(pipeline, folders) -> list[str]:
    """Read the Markdown pages under ``folders`` into ``pipeline.column_doc_files`` and return notes about what was not used.

    ``column_doc_files`` is ``{table key: {column: {"description": text, "file": label, "hint": zone mention or ""}}}``.
    """

    from .column_docs import _resolve
    from .pipeline_types import PipelineDiagnostic
    from .resilience import read_text_or_reason

    known = set(pipeline.models) | set(pipeline.sources)
    store: dict = pipeline.__dict__.setdefault("column_doc_files", {})
    unmatched: set[str] = set()
    conflicts: set[str] = set()
    pages = 0
    missing: list[str] = []
    for folder in folders:
        folder = Path(folder)
        if not folder.exists():
            missing.append(str(folder.name) or "the docs folder")
            continue
        for path in markdown_files([folder]):
            try:
                if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            text, _reason = read_text_or_reason(path)
            if text is None:
                continue
            pages += 1
            label = path.name if folder.is_file() else path.relative_to(folder).as_posix()
            for names, column, description in parse_page(text, path.stem):
                key = next((k for k in (_resolve(pipeline, n, known) for n in names) if k), None)
                if key is None:
                    unmatched.add(names[0] if names else label)
                    continue
                outputs = list(pipeline.output_columns(key)) if key in pipeline.models else list(pipeline.source_schema.get(key, {}))
                by_name = {c.casefold(): c for c in outputs}
                if outputs and column.casefold() not in by_name:
                    unmatched.add(f"{key.split('.')[-1]}.{column}")
                    continue
                name = by_name.get(column.casefold(), column)
                entry = store.setdefault(key, {})
                if name in entry:
                    if entry[name]["file"] != label:
                        conflicts.add(f"{key.split('.')[-1]}.{name}")
                    continue
                entry[name] = {"description": description[:400], "file": label, "hint": zone_hint(description)}
    notes = [f"docs folder not found: {name}" for name in missing]
    if unmatched:
        shown = ", ".join(sorted(unmatched)[:3]) + (", ..." if len(unmatched) > 3 else "")
        notes.append(f"{len(unmatched)} documented table or column name{'s' if len(unmatched) != 1 else ''} matched nothing in the project, so their descriptions are not used: {shown}")
    if conflicts:
        shown = ", ".join(sorted(conflicts)[:3]) + (", ..." if len(conflicts) > 3 else "")
        notes.append(f"{len(conflicts)} column{'s' if len(conflicts) != 1 else ''} described in more than one docs file; the first file in path order is used: {shown}")
    for note in notes:
        pipeline.diagnostics.append(PipelineDiagnostic("", "column_docs_unmatched", note))
    return notes


def documented(pipeline, key: str, column: str) -> dict | None:
    """The description a docs page gave for this exact column (any case), or ``None``."""

    for name, entry in (getattr(pipeline, "column_doc_files", None) or {}).get(key, {}).items():
        if name.casefold() == column.casefold():
            return entry
    return None
