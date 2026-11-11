"""``primary_key`` of Malloy sources: which table a source reads and which column identifies its rows.

``source: orders is conn.table('dataset.orders') extend { primary_key: order_id }`` says that ``dataset.orders`` has one row per
``order_id``. This is the whole reader: a source that names a table with ``table('...')`` and a ``primary_key`` at the top
level of its ``extend { }`` (or older ``{ }``) block. Nothing else of Malloy is read (no queries, dimensions or measures), a source
built from another source is skipped, and a file that does not read as expected yields nothing rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_FILE = 1_000_000
MAX_FILES = 500

_SOURCE = re.compile(
    r"\bsource\s*:\s*([A-Za-z_]\w*)\s+is\s+(?:[A-Za-z_]\w*\s*\.\s*)?table\s*\(\s*(['\"])([^'\"]+)\2\s*\)\s*(?:extend\s*)?(\{)?", re.I)
_KEY = re.compile(r"(?<![\w.])primary_key\s*:\s*`?([A-Za-z_]\w*)`?", re.I)


@dataclass(frozen=True)
class MalloyKey:
    source: str
    #: the table as the file writes it, without a connection prefix (``dataset.orders``)
    table: str
    column: str
    file: str


def _blank_comments(text: str) -> str:
    """``text`` with ``//``, ``--`` and ``/* */`` comments blanked (same length), strings kept."""

    out, i, quote = [], 0, ""
    while i < len(text):
        two = text[i:i + 2]
        if quote:
            out.append(text[i])
            if text[i] == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif text[i] == quote:
                quote = ""
        elif text[i] in "'\"`":
            quote = text[i]
            out.append(text[i])
        elif two in ("//", "--"):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out.append(" " * (end - i))
            i = end - 1
        elif two == "/*":
            end = text.find("*/", i + 2)
            end = len(text) if end < 0 else end + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end - 1
        else:
            out.append(text[i])
        i += 1
    return "".join(out)


def _block(text: str, start: int) -> str:
    """The text inside the ``{`` at ``start``, nested blocks blanked, or ``""`` if it never closes."""

    depth, quote, chars = 0, "", []
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
            chars.append(" " if depth > 1 else c)
            continue
        if c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
            chars.append(" " if depth > 1 else "")
            continue
        elif c == "}":
            depth -= 1
            if depth == 0:
                return "".join(chars)
            chars.append(" ")
            continue
        chars.append(" " if depth > 1 else c)
    return ""


def keys_in(text: str, file: str = "") -> list[MalloyKey]:
    """Every source of ``text`` that reads a table and states a ``primary_key``."""

    clean = _blank_comments(text)
    found = []
    for match in _SOURCE.finditer(clean):
        if not match.group(4):
            continue
        body = _block(clean, match.end() - 1)
        keys = _KEY.findall(body)
        if len(keys) == 1:  # two primary_key lines are a mistake, not a claim
            table = match.group(3).split(":", 1)[-1].strip()
            found.append(MalloyKey(match.group(1), table, keys[0], file))
    return found


def read_folder(folder: Path) -> list[MalloyKey]:
    """The keys of every ``.malloy`` file under ``folder`` (at most :data:`MAX_FILES` files of :data:`MAX_FILE` bytes)."""

    found: list[MalloyKey] = []
    try:
        files = sorted(p for p in Path(folder).rglob("*.malloy") if p.is_file() and not p.is_symlink())[:MAX_FILES]
    except OSError:
        return found
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE:
                continue
            found += keys_in(path.read_text(encoding="utf-8"), str(path.relative_to(folder)))
        except (OSError, ValueError):
            continue
    return found
