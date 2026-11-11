"""What the project says about its columns in words: descriptions and declared attributes, read without running anything.

A column can carry four declared facts, all plain text:

* ``description``: what the column holds (Dataform ``columns: {col: "text"}`` or ``columns: {col: {description: "text"}}``);
* ``timezone``: the zone its time values are in (``UTC``, ``America/New_York``);
* ``unit``: what one step of the value is (``seconds``, ``USD``);
* ``scale``: the range the value lives on (``0-100``, ``0-1``).

They come from the ``columns`` block of a Dataform config (sqlx files and declarations, and ``actionDescriptor`` of a compiled
graph) and from a sidecar file (``--column-attributes FILE``, JSON or YAML)::

    {"orders.created_at": {"timezone": "UTC"}, "products.margin_pct": {"scale": "0-100", "description": "Margin as a percentage"}}

The loaders keep them in ``Pipeline.column_docs`` as ``{table key: {column: {attribute: text}}}``. Sidecar values replace
the config's for the same attribute: the person starting the server knows more than a comment in a file. Nothing here
decides what a declaration means for a query; :mod:`kumosql.column_meaning` carries it along lineage and
:mod:`kumosql.column_lint` checks drafts against it.
"""

from __future__ import annotations

import re
from pathlib import Path

ATTRIBUTES = ("description", "timezone", "unit", "scale")
MAX_VALUE = 400  # a declared value is clipped to this many characters

ColumnDocs = dict[str, dict[str, dict[str, str]]]


def _clean(value: object) -> str | None:
    """A declared value as one line of text, or ``None`` when it is not text (a number, a list, a computed value)."""

    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_VALUE] if text else None


def _entry(value: object) -> dict[str, str]:
    """``"text"`` is a description; an object contributes the attributes it names (others are ignored)."""

    if isinstance(value, str):
        text = _clean(value)
        return {"description": text} if text else {}
    if isinstance(value, dict):
        found = {}
        for name in ATTRIBUTES:
            text = _clean(value.get(name))
            if text:
                found[name] = text
        return found
    return {}


def _columns(block: object) -> dict[str, dict[str, str]]:
    if not isinstance(block, dict):
        return {}
    return {str(name): entry for name, value in block.items() if (entry := _entry(value))}


def config_column_docs(config: str) -> dict[str, dict[str, str]]:
    """The ``columns`` block of a Dataform config block as ``{column: {attribute: text}}``; empty when absent or computed."""

    from . import js_literals
    from .pipeline_loading import _blank_strings, _top_level, _value_at

    # _top_level blanks nested braces to spaces, so the value starts at the first non-space of the config itself.
    match = re.search(r"\bcolumns\s*:", _blank_strings(_top_level(config)))
    if not match:
        return {}
    start = max(config.find("{"), 0) + match.end()
    while start < len(config) and config[start].isspace():
        start += 1
    text = _value_at(config, start)
    value, _end = js_literals.parse_literal(js_literals.strip_comments(text))
    return _columns(value) if isinstance(value, dict) else {}


def compiled_column_docs(item: object) -> dict[str, dict[str, str]]:
    """The same from one entry of a compiled graph: ``actionDescriptor.columns`` is a list of ``{path, description}``."""

    descriptor = item.get("actionDescriptor") if isinstance(item, dict) else None
    entries = descriptor.get("columns") if isinstance(descriptor, dict) else None
    found: dict[str, dict[str, str]] = {}
    for entry in entries if isinstance(entries, list) else ():
        path = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(path, list) and len(path) == 1 and isinstance(path[0], str) and (attributes := _entry(entry)):
            found[path[0]] = attributes
    return found


# ------------------------------------------------------------------ the sidecar file


def read_attributes_file(path: Path) -> dict[str, dict[str, str]]:
    """``{"table.column": {attribute: text}}`` from a JSON or YAML file; raises ``ValueError`` (a short message) when it is malformed."""

    from .resilience import read_text_or_reason

    text, reason = read_text_or_reason(path)
    if text is None:
        raise ValueError(f"the column attributes file could not be read: {reason}")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ValueError("a YAML column attributes file needs PyYAML (pip install pyyaml); JSON needs nothing") from None
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError:
            raise ValueError("the column attributes file is not valid YAML") from None
    else:
        import json

        try:
            raw = json.loads(text)
        except ValueError:
            raise ValueError("the column attributes file is not valid JSON") from None
    return check_attributes(raw)


def check_attributes(raw: object) -> dict[str, dict[str, str]]:
    """The sidecar's content, checked: an object of ``"table.column"`` to an object of attributes with text values."""

    if not isinstance(raw, dict):
        raise ValueError('the column attributes file must be an object such as {"orders.created_at": {"timezone": "UTC"}}')
    found: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or "." not in key.strip("`"):
            raise ValueError(f"{str(key)[:60]!r} must name a column as table.column")
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{key[:60]!r} must map attributes ({', '.join(ATTRIBUTES)}) to text")
        attributes = {}
        for name, item in value.items():
            if name not in ATTRIBUTES:
                raise ValueError(f"{key[:60]!r}: unknown attribute {str(name)[:30]!r} (use {', '.join(ATTRIBUTES)})")
            text = _clean(item)
            if text is None:
                raise ValueError(f"{key[:60]!r}: {name} must be text")
            attributes[name] = text
        found[key.strip().strip("`")] = attributes
    return found


def apply_attributes(pipeline, declared: dict[str, dict[str, str]]) -> list[str]:
    """Merge sidecar declarations into ``pipeline.column_docs`` (they replace the config's per attribute).

    A declaration for a table the project does not know, or for a column a model does not output, cannot apply: it is
    left out and named in the returned notes (and as a pipeline diagnostic), because a declaration that does nothing must
    not pass for knowledge.
    """

    from .pipeline_types import PipelineDiagnostic

    docs: ColumnDocs = pipeline.__dict__.setdefault("column_docs", {})
    notes: list[str] = []
    known = set(pipeline.models) | set(pipeline.sources)
    for text, attributes in declared.items():
        table_text, _dot, column = text.rpartition(".")
        key = _resolve(pipeline, table_text, known)
        if key is None:
            notes.append(f"{text}: no table of the project matches {table_text!r}, or several do")
            continue
        outputs = [c for c in pipeline.output_columns(key)] if key in pipeline.models else list(pipeline.source_schema.get(key, {}))
        by_name = {c.casefold(): c for c in outputs}
        if outputs and column.casefold() not in by_name:
            notes.append(f"{text}: {key.split('.')[-1]} has no column {column!r}")
            continue
        name = by_name.get(column.casefold(), column)
        docs.setdefault(key, {}).setdefault(name, {}).update(attributes)
        labels = pipeline.__dict__.setdefault("column_doc_sources", {}).setdefault(key, {}).setdefault(name, {})
        labels.update({attribute: "column attributes file" for attribute in attributes})
    for note in notes:
        pipeline.diagnostics.append(PipelineDiagnostic("", "column_attributes_unmatched", note))
    return notes


def _resolve(pipeline, text: str, known: set[str]) -> str | None:
    name = text.strip().strip("`")
    if name in known:
        return name
    try:
        resolved = pipeline.resolve(name)
    except Exception:  # noqa: BLE001
        resolved = None
    if resolved in known:
        return resolved
    parts = len(name.casefold().split("."))
    tails = [key for key in known if ".".join(key.casefold().split(".")[-parts:]) == name.casefold()]
    return tails[0] if len(tails) == 1 else None
