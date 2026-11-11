"""What the project says about its columns in words: descriptions and declared attributes, read without running anything.

A column can carry four declared facts, all plain text:

* ``description``: what the column holds (Dataform ``columns: {col: "text"}`` or ``columns: {col: {description: "text"}}``);
* ``timezone``: the zone its time values are in (``UTC``, ``America/New_York``);
* ``unit``: what one step of the value is (``seconds``, ``USD``);
* ``scale``: the range the value lives on (``0-100``, ``0-1``).

They come from the ``columns`` block of a Dataform config (``.sqlx`` files and declarations), from the same block written in
JavaScript (``declare({..., columns: {...}})``, ``publish("x").columns({...})``, ``publish("x").config({columns: {...}})``), from
``actionDescriptor`` of a compiled graph, and from a sidecar file (``--column-attributes FILE``, JSON or YAML)::

    {"orders.created_at": {"timezone": "UTC"}, "products.margin_pct": {"scale": "0-100", "description": "Margin as a percentage"}}

The loaders keep them in ``Pipeline.column_docs`` as ``{table key: {column: {attribute: text}}}``. Sidecar values replace
the config's for the same attribute: the person starting the server knows more than a comment in a file. Nothing here
decides what a declaration means for a query. A value the reader cannot evaluate without running code (a template string with a
variable, a call, a variable) is never guessed: it is kept in ``Pipeline.column_doc_unread`` with the reason and the agent
answers list it under ``unknown``; :mod:`kumosql.column_meaning` carries it along lineage and
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


Unread = tuple[str, str, str]  # (column, attribute, reason); column "" is the whole block, attribute "meaning" is any of them

_ESCAPE_RE = re.compile(r"\\[^'\"`\\]")


def _why(expression: str) -> str:
    """Why a JavaScript expression is not a plain string, in words (never the expression itself)."""

    from .pipeline_loading import _blank_strings

    text = expression.strip()
    blanked = _blank_strings(text)
    if text.startswith("`") and "${" in text:
        return "a template string with a variable (${...})"
    if re.fullmatch(r"[A-Za-z_$][\w$]*(\s*\.\s*[A-Za-z_$][\w$]*)*", text):
        return "a variable or property reference"
    if "(" in blanked:
        return "a function call"
    if "?" in blanked:
        return "a conditional expression"
    if "+" in blanked:
        return "a string built with +"
    if text.startswith("..."):
        return "a spread (...) of another object"
    if text and text[0] in "'\"`" and _ESCAPE_RE.search(text):
        return "a string with an escape sequence (not decoded)"
    return "not a plain string literal"


def _text(expression: str) -> tuple[str | None, str]:
    """``(text, reason)`` of a JavaScript string literal; ``(None, reason)`` when it is not one the reader may evaluate."""

    from . import js_literals

    value, end = js_literals.parse_literal(expression.strip())
    if not isinstance(value, str) or expression.strip()[end:].strip():
        return None, _why(expression)
    if _ESCAPE_RE.search(expression):
        return None, "a string with an escape sequence (not decoded)"
    return value, ""


_PAIR_RE = re.compile(r"""^(?:([A-Za-z_$][\w$]*)|'([^']*)'|"([^"]*)")\s*(?::\s*(.*))?$""", re.S)


def _pairs(object_text: str) -> tuple[list[tuple[str, str | None]], str]:
    """``[(key, value text)]`` of an object literal written out in a file, and the reason when it is not one.

    A shorthand member (``{ id }``) has ``None`` for its value. A spread or a computed key is returned under the key ``"..."``."""

    from . import js_literals
    from .pipeline_loading import _split_top_level

    text = js_literals.strip_comments(object_text).strip()
    if not (text.startswith("{") and text.endswith("}")):
        return [], _why(text)
    found: list[tuple[str, str | None]] = []
    for part in _split_top_level(text[1:-1]):
        pair = _PAIR_RE.match(part)
        if pair is None:
            found.append(("...", part))
        else:
            found.append((next(g for g in pair.groups()[:3] if g is not None), pair.group(4)))
    return found, ""


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


def columns_block(block_text: str) -> tuple[dict[str, dict[str, str]], list[Unread]]:
    """Read ``{col: "text", col: {description: "text", unit: "USD"}}`` written out in a file, one entry at a time.

    Entries that are plain strings are read; an entry (or attribute) that is computed is returned in the second list with
    the reason, so one variable never hides the other columns and is never guessed."""

    docs: dict[str, dict[str, str]] = {}
    unread: list[Unread] = []
    pairs, reason = _pairs(block_text)
    if reason:
        return docs, [("", "meaning", reason)]
    for name, value in pairs:
        if name == "...":
            unread.append(("", "meaning", _why(value or "")))
        elif value is None:
            unread.append((name, "meaning", "a variable (shorthand member)"))
        elif value.strip().startswith("{"):
            attributes, inner = {}, _pairs(value)[0]
            for attribute, expression in inner:
                if attribute == "...":
                    unread.append((name, "meaning", _why(expression or "")))
                elif attribute in ATTRIBUTES:
                    text, why = _text(expression or attribute)
                    if text is None:
                        unread.append((name, attribute, why))
                    elif cleaned := _clean(text):
                        attributes[attribute] = cleaned
            if attributes:
                docs[name] = attributes
        else:
            text, why = _text(value)
            if text is None:
                unread.append((name, "description", why))
            elif cleaned := _clean(text):
                docs[name] = {"description": cleaned}
    return docs, unread


def _columns(block: object) -> dict[str, dict[str, str]]:
    if not isinstance(block, dict):
        return {}
    return {str(name): entry for name, value in block.items() if (entry := _entry(value))}


def config_columns(config: str) -> tuple[dict[str, dict[str, str]], list[Unread]]:
    """The ``columns`` block of a Dataform config (a ``.sqlx`` config block or a JavaScript config object): what could be read
    as ``{column: {attribute: text}}`` and what could not, as ``(column, attribute, reason)``."""

    from .pipeline_loading import _blank_strings, _top_level, _value_at

    # _top_level blanks nested braces to spaces, so the value starts at the first non-space of the config itself.
    blanked = _blank_strings(_top_level(config))
    match = re.search(r"(?<![\w$.])columns\s*:", blanked)
    if not match:
        if re.search(r"[{,]\s*columns\s*(?=[,}])", blanked):
            return {}, [("", "meaning", "a variable (shorthand member)")]
        return {}, []
    start = max(config.find("{"), 0) + match.end()
    while start < len(config) and config[start].isspace():
        start += 1
    return columns_block(_value_at(config, start))


def config_column_docs(config: str) -> dict[str, dict[str, str]]:
    """The ``columns`` block of a Dataform config block as ``{column: {attribute: text}}``; empty when absent or computed."""

    return config_columns(config)[0]


def merge_reads(
    first: tuple[dict[str, dict[str, str]], list[Unread]], second: tuple[dict[str, dict[str, str]], list[Unread]]
) -> tuple[dict[str, dict[str, str]], list[Unread]]:
    """Two readings of the same action's columns (a config object and a chained ``.columns()``): what they agree on is kept;
    an attribute the two texts give differently is dropped and reported, because which one wins is not decided here."""

    docs = {column: dict(attributes) for column, attributes in first[0].items()}
    unread = list(first[1]) + list(second[1])
    for column, attributes in second[0].items():
        have = docs.setdefault(column, {})
        for attribute, text in attributes.items():
            if have.get(attribute, text) != text:
                del have[attribute]
                unread.append((column, attribute, "declared twice with different text"))
            else:
                have[attribute] = text
        if not have:
            del docs[column]
    return docs, unread


def compiled_columns(item: object) -> tuple[dict[str, dict[str, str]], list[Unread]]:
    """The columns of one entry of a compiled graph and what could not be read.

    ``actionDescriptor.columns`` is a list of ``{path, description}`` (a mapping in the config's own form is read too, as is a
    ``columns`` at the entry's top level). A path longer than one name describes a field inside a STRUCT column: not read."""

    found: dict[str, dict[str, str]] = {}
    unread: list[Unread] = []
    descriptor = item.get("actionDescriptor") if isinstance(item, dict) else None
    blocks = [descriptor.get("columns") if isinstance(descriptor, dict) else None, item.get("columns") if isinstance(item, dict) else None]
    for block in blocks:
        if isinstance(block, dict):
            for name, value in block.items():
                if entry := _entry(value):
                    found.setdefault(str(name), {}).update(entry)
                elif not isinstance(value, (str, dict)) and value is not None:
                    unread.append((str(name), "description", "the declared value is not text"))
            continue
        for entry in block if isinstance(block, list) else ():
            path = entry.get("path") if isinstance(entry, dict) else None
            if not (isinstance(path, list) and path and all(isinstance(part, str) for part in path)):
                continue
            attributes = _entry(entry)
            if len(path) > 1:
                unread.append((path[0], "description", "a description for a field inside the column is not read"))
            elif attributes:
                found.setdefault(path[0], {}).update(attributes)
            elif any(entry.get(a) is not None and not isinstance(entry.get(a), str) for a in ATTRIBUTES):
                unread.append((path[0], "description", "the declared value is not text"))
    return found, unread


def compiled_column_docs(item: object) -> dict[str, dict[str, str]]:
    """The same from one entry of a compiled graph: ``actionDescriptor.columns`` is a list of ``{path, description}``."""

    return compiled_columns(item)[0]


def call_columns(kind: str, config: str, chain: list[tuple[str, str]]) -> tuple[dict[str, dict[str, str]], list[Unread]]:
    """The columns a JavaScript ``declare``, ``publish`` or ``operate`` call declares: in its config object and in the calls
    chained on it (``.columns({...})`` and ``.config({columns: {...}})``; the other chained calls say nothing about columns).

    ``config`` is the call's config object text (``"{}"`` when it has none), ``chain`` the chained ``(method, argument text)``.
    The readings are merged; a computed config (a variable) is reported, never assumed to hold no columns."""

    from .pipeline_loading import _JS_IDENT_RE

    config = config.strip()
    if config.startswith("{"):
        read = config_columns(config)
    elif config and (kind == "declare" or _JS_IDENT_RE.match(config)):
        read = ({}, [("", "meaning", "the config is not written out here (a variable or a call)")])
    else:
        read = ({}, [])
    for method, argument in chain:
        if method == "columns":
            read = merge_reads(read, columns_block(argument))
        elif method == "config":
            read = merge_reads(read, config_columns(argument) if argument.startswith("{") else ({}, [("", "meaning", _why(argument))]))
    return read


class ColumnBook:
    """What the loaders collect while reading a project: the declared facts, where each came from and what could not be read.

    ``docs`` is ``Pipeline.column_docs``; ``sources`` is ``{table key: {column: {attribute: label}}}`` for facts whose origin is
    not the plain ``columns config`` of a ``.sqlx`` file (a JavaScript file, a compiled graph); ``unread`` is
    ``{table key: [(column, attribute, reason, where)]}`` for what is written but cannot be evaluated without running code.
    """

    def __init__(self) -> None:
        self.docs: ColumnDocs = {}
        self.sources: dict[str, dict[str, dict[str, str]]] = {}
        self.unread: dict[str, list[tuple[str, str, str, str]]] = {}

    def keep(self, key: str, read: tuple[dict[str, dict[str, str]], list[Unread]], where: str, label: str = "") -> None:
        """Remember one reading for table ``key`` (it replaces an earlier one). ``where`` is the file; ``label`` the origin shown
        to the agent when it is not the default ``columns config``."""

        found, unread = read
        if not key:
            return
        self.docs.pop(key, None)
        self.sources.pop(key, None)
        self.unread.pop(key, None)
        if found:
            self.docs[key] = found
            if label:
                self.sources[key] = {column: {attribute: label for attribute in attributes} for column, attributes in found.items()}
        if unread:
            self.unread[key] = [(column, attribute, reason, where) for column, attribute, reason in unread]


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
