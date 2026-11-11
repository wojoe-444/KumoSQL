"""``primary_key`` of Malloy sources, read with :mod:`kumosql.malloy_reader`: which table a source reads and which column identifies its rows.

``source: orders is conn.table('dataset.orders') extend { primary_key: order_id }`` says that ``dataset.orders`` has one row per
``order_id``. Only a source whose table the reader could determine and that names a ``primary_key`` gives a key; a source it could not
read gives nothing rather than a guess. A source built on another source is skipped: its key may be that of a narrower or filtered view, not of the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .malloy_reader import read_malloy

MAX_FILE = 1_000_000
MAX_FILES = 500


@dataclass(frozen=True)
class MalloyKey:
    source: str
    #: the table as the file writes it, without a connection prefix (``dataset.orders``)
    table: str
    column: str
    file: str


def keys_in(text: str, file: str = "") -> list[MalloyKey]:
    """Every source of ``text`` that reads a table and states a ``primary_key``."""

    return [MalloyKey(item["name"], item["table"], item["primary_key"], file)
            for item in read_malloy(text) if item.get("table") and item.get("primary_key") and not item.get("extends")]


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
