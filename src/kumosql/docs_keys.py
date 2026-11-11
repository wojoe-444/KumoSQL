"""Primary keys that Markdown docs state for a table or view, read conservatively.

Two shapes are read, and only inside a section whose heading (or, before any heading, whose file name) names one table:

* a line ``Primary key: a, b`` (also ``Primary keys``, ``Unique key``, bold or backticked, with ``:`` or ``-``);
* a column table whose header has a ``Primary key`` (or ``PK``) column, where the rows marked ``yes``, ``x``, ``true``, ``pk`` or a check
  mark are the key, in table order.

Anything else is ignored: prose that mentions a key, a list item that is not plain column names, a section naming no table or
two. The caller turns the table text into a project table and keeps where the key was found (file and section).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import relative_posix

MAX_FILE = 1_000_000
MAX_FILES = 500
_LINE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?\s*(?:primary|unique)\s+keys?\s*(?:\*\*|__)?\s*[:\-–]\s*(?:\*\*|__)?\s*(.+?)\s*$", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,4}\s+(.+?)\s*#*\s*$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MARK = {"yes", "y", "x", "true", "pk", "primary", "✓", "✔", "✅", "primary key"}
_PK_HEADER = {"primary key", "pk", "primary"}


@dataclass(frozen=True)
class DocKey:
    #: the heading text (or file stem) that names the table; the caller resolves it
    table_text: str
    columns: tuple[str, ...]
    file: str
    section: str


def _plain(text: str) -> str:
    return re.sub(r"[`*]", "", text).strip()


def _columns(text: str) -> tuple[str, ...] | None:
    parts = [p for p in re.split(r"\s*(?:,|\band\b|\+|&)\s*", text.strip().rstrip(".")) if p.strip()]
    names = tuple(_plain(p) for p in parts)
    return names if names and all(_NAME.match(n) for n in names) else None


def _cells(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith("|") or line.count("|") < 2:
        return None
    return [c.strip() for c in line.strip("|").split("|")]


def keys_in(text: str, stem: str = "", file: str = "") -> list[DocKey]:
    """The keys stated in ``text`` (one Markdown file); ``stem`` names the table before the first heading."""

    found: list[DocKey] = []
    section = stem
    lines = text.splitlines()
    i = 0
    in_fence = False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            heading = _HEADING.match(line)
            if heading:
                section = _plain(heading.group(1))
            elif section and (m := _LINE.match(line)) and (cols := _columns(m.group(1))):
                found.append(DocKey(section, cols, file, section))
            elif section and (head := _cells(line)) and i + 1 < len(lines) and (rule := _cells(lines[i + 1])) and all(re.fullmatch(r":?-{2,}:?", c) for c in rule if c):
                marker = next((n for n, c in enumerate(head) if _plain(c).casefold() in _PK_HEADER), None)
                name = next((n for n, c in enumerate(head) if _plain(c).casefold() in ("column", "name", "field", "column name")), 0)
                j, chosen = i + 2, []
                while j < len(lines) and (row := _cells(lines[j])) is not None:
                    if marker is not None and len(row) > max(marker, name) and _plain(row[marker]).casefold() in _MARK and _NAME.match(_plain(row[name])):
                        chosen.append(_plain(row[name]))
                    j += 1
                if chosen:
                    found.append(DocKey(section, tuple(dict.fromkeys(chosen)), file, section))
                i = j - 1
        i += 1
    return found


def read_folder(folder: Path) -> list[DocKey]:
    """The keys of every ``.md`` file under ``folder`` (at most :data:`MAX_FILES` files of :data:`MAX_FILE` bytes)."""

    found: list[DocKey] = []
    try:
        files = sorted(p for p in Path(folder).rglob("*.md") if p.is_file() and not p.is_symlink())[:MAX_FILES]
    except OSError:
        return found
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE:
                continue
            found += keys_in(path.read_text(encoding="utf-8"), path.stem, relative_posix(path, folder))
        except (OSError, ValueError):
            continue
    return found
