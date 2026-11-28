"""Edit the ``config {}`` blocks of Dataform SQLX files in place: a code mod that keeps every byte it does not change.

A config block is a JavaScript object literal. :func:`find_config` reads it without running it and keeps the source
span of every key and value, so an edit is spliced into the original text. Line endings, indentation, quotes,
comments and the trailing-comma habit of everything the edit does not touch stay as they were, and a file whose edit
would change nothing is left byte-identical. A computed part (a variable, a call, a template literal with ``${...}``,
a spread) can be read around, but an edit that touches one is refused, with the file and the reason.

The edit language is small (the full grammar is in docs/sqlx-config-edits.md)::

    labels.team = "finance" where type in (table, view)
    tags += "finance"; tags -= "old"
    delete bigquery.labels.tmp where name ~ "^stg_" and tags has "pii"

``labels.`` is an explicit alias for ``bigquery.labels.``, where Dataform's documentation puts table labels
(``bigquery: {labels: {...}}``; ``IBigQueryOptions.labels`` in the core reference). Files with ``type: "assertion"``
are never edited unless the where clause names that type, and the inline ``assertions: {...}`` block of a table only
by a path that starts ``assertions``.

``python -m kumosql sqlx-config edit PROJECT COMMAND`` previews a diff; ``--plan FILE`` saves the planned edits with
each file's sha256 and ``--apply`` writes them atomically, re-reading each file to check that only the intended keys
changed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import datetime as _dt
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Callable, Iterator, Sequence

from .js_tokens import Token, strip_comments, tokens

__all__ = [
    "ACTION_TYPES", "LABEL_TYPES", "Command", "CommandError", "ConfigBlock", "ConfigSyntaxError", "EditPlan",
    "FileResult", "apply_plan", "apply_saved_plan", "edit_file", "find_config", "parse_command", "plan_edit",
    "show_project", "strip_comments", "tokens", "main",
]

PLAN_KIND = "kumosql-sqlx-config-plan"
PLAN_VERSION = 1
EXIT_OK, EXIT_USAGE, EXIT_REFUSED = 0, 2, 3
# Dataform's ITableConfig (tables, views and incremental tables) is the only config with bigquery.labels.
LABEL_TYPES = ("table", "view", "incremental")
ACTION_TYPES = ("table", "view", "incremental", "assertion", "operations", "declaration", "test")
_BOM = b"\xef\xbb\xbf"


# ------------------------------------------------------------------------------------------------------------ errors


class ConfigSyntaxError(ValueError):
    """The text is not a JavaScript object literal this reader understands; ``position`` is an offset into it."""

    def __init__(self, message: str, position: int = 0):
        super().__init__(message)
        self.position = position


# ------------------------------------------------------------------------------------------------------------ parsing


@dataclass
class Scalar:
    """A literal: ``value`` is a str, float, bool or None (JavaScript ``null``); ``quote`` is a string's quote."""

    start: int
    end: int
    value: object
    quote: str = ""


@dataclass
class Computed:
    """A value only known by running the project, with a short reason (a variable, a call, a template with ``${}``)."""

    start: int
    end: int
    reason: str


@dataclass
class Container:
    """An object (``kind == "object"``) or array literal; ``start`` is the opening bracket, ``end`` just after the close."""

    kind: str
    start: int
    end: int
    entries: list["Entry"]


@dataclass
class Entry:
    """An object member (``key``) or array item; also a spread or a computed key, which make an object unknowable."""

    kind: str  # "member", "item", "spread", "computed-key"
    start: int
    value: "Scalar | Computed | Container"
    key: str | None = None
    key_quote: str = ""  # "" for a bare name, else the quote the key is written in
    comma: int | None = None  # offset of the comma written after it

    @property
    def end(self) -> int:
        return self.value.end


Node = "Scalar | Computed | Container"
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v"}
_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*", re.A)


def _decode(text: str, token: Token) -> str:
    """The value of a string or template literal token, escapes read as JavaScript reads them."""

    quote, body, out, index = text[token.start], text[token.start + 1 : token.end - 1], [], 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            escaped = body[index + 1]
            if escaped in _ESCAPES:
                out.append(_ESCAPES[escaped])
                index += 2
            elif escaped == "0" and not body[index + 2 : index + 3].isdigit():
                out.append("\0")
                index += 2
            elif escaped == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", body[index + 2 : index + 4]):
                out.append(chr(int(body[index + 2 : index + 4], 16)))
                index += 4
            elif escaped == "u" and (code := re.match(r"\{([0-9a-fA-F]{1,6})\}|([0-9a-fA-F]{4})", body[index + 2 :])):
                out.append(chr(int(code.group(1) or code.group(2), 16)))
                index += 2 + code.end()
            elif escaped == "\r":
                index += 3 if body[index + 2 : index + 3] == "\n" else 2
            elif escaped in "\n\u2028\u2029":
                index += 2
            else:
                out.append(escaped)
                index += 2
        elif char in "\r\n" and quote != "`":
            raise ConfigSyntaxError("a line break inside a string", token.start)
        elif char == "\r" and body[index + 1 : index + 2] == "\n":  # a template literal reads CRLF as LF
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _number(text: str) -> float:
    text = text.replace("_", "").rstrip("n")
    if text[:2].lower() in ("0x", "0b", "0o"):
        return float(int(text, 0))
    return float(text)


def _reason(text: str, found: Sequence[Token]) -> str:
    if len(found) == 1 and found[0].kind == "template":
        return "a template literal with ${...}"
    if all(t.kind == "name" or (t.kind == "punct" and text[t.start] == ".") for t in found):
        return "a variable"
    if any(t.kind == "punct" and text[t.start] == "(" for t in found):
        return "a function call"
    return "an expression"


class _Parser:
    """Recursive descent over significant tokens. ``stop_words`` are names that end a value (``where`` in a command)."""

    def __init__(self, text: str, toks: list[Token], stop_words: frozenset[str] = frozenset()):
        self.text, self.toks, self.stop_words = text, toks, stop_words
        # Each token's punctuation character ("\0" for any other token), so the many bracket checks are one lookup.
        self.punct = [text[t.start] if t.kind == "punct" else "\0" for t in toks] + ["\0"]
        self.count = len(toks)

    def tok(self, index: int) -> Token:
        if index >= self.count:
            raise ConfigSyntaxError("the text ends too early", len(self.text))
        return self.toks[index]

    def is_punct(self, index: int, chars: str) -> bool:
        return self.punct[min(index, self.count)] in chars

    def ends_value(self, index: int) -> bool:
        if index >= self.count or self.punct[index] in ",;}])":
            return True
        token = self.toks[index]
        return token.kind == "name" and self.text[token.start : token.end] in self.stop_words

    def value(self, index: int):
        token, text = self.tok(index), self.text
        node, after, first = None, index + 1, text[token.start]
        if token.kind == "punct" and first in "{[":
            node, after = self.container(index)
        elif token.kind == "string":
            node = Scalar(token.start, token.end, _decode(text, token), first)
        elif token.kind == "template" and not token.computed:
            node = Scalar(token.start, token.end, _decode(text, token), "`")
        elif token.kind == "number":
            node = Scalar(token.start, token.end, _number(text[token.start : token.end]))
        elif token.kind == "name" and text[token.start : token.end] in ("true", "false", "null"):
            node = Scalar(token.start, token.end, {"true": True, "false": False, "null": None}[text[token.start : token.end]])
        elif token.kind == "op" and first == "-" and after < len(self.toks) and self.toks[after].kind == "number":
            number = self.toks[after]
            node, after = Scalar(token.start, number.end, -_number(text[number.start : number.end])), after + 1
        if node is not None and self.ends_value(after):
            return node, after
        return self.expression(index)

    def expression(self, index: int):
        """Everything up to the next ``,`` or closing bracket at this depth, as one computed value."""

        depth, cursor, last, ternaries = 0, index, None, 0
        while cursor < len(self.toks):
            token = self.toks[cursor]
            char = self.text[token.start]
            if token.kind == "punct":
                if char in "{[(":
                    depth += 1
                elif char in "}])":
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and char in ",;":
                    break
                elif depth == 0 and char == "?":
                    ternaries += 1
                elif depth == 0 and char == ":":
                    if not ternaries:  # `a: 1 b: 2`: the next member's key, so a comma is missing
                        raise ConfigSyntaxError("expected ',' before the next key", token.start)
                    ternaries -= 1
            elif depth == 0 and token.kind == "name" and self.text[token.start : token.end] in self.stop_words:
                break
            elif (depth == 0 and last is not None and last.kind in ("string", "template", "number")
                  and token.kind in ("string", "template", "number", "name")
                  and self.text[token.start : token.end] not in ("in", "instanceof")):
                raise ConfigSyntaxError("expected ',' between two values", token.start)
            last, cursor = token, cursor + 1
        if last is None:
            raise ConfigSyntaxError("a value is missing", self.tok(index).start)
        if depth:
            raise ConfigSyntaxError("brackets do not close", self.tok(index).start)
        start = self.toks[index].start
        return Computed(start, last.end, _reason(self.text, self.toks[index:cursor])), cursor

    def matching(self, index: int) -> int:
        depth = 0
        for cursor in range(index, len(self.toks)):
            if self.is_punct(cursor, "{[("):
                depth += 1
            elif self.is_punct(cursor, "}])"):
                depth -= 1
                if depth == 0:
                    return cursor
        raise ConfigSyntaxError("brackets do not close", self.tok(index).start)

    def container(self, index: int):
        opening = self.tok(index)
        kind = "object" if self.text[opening.start] == "{" else "array"
        closer = "}" if kind == "object" else "]"
        entries: list[Entry] = []
        cursor = index + 1
        while True:
            if self.is_punct(cursor, closer):
                return Container(kind, opening.start, self.toks[cursor].end, entries), cursor + 1
            entry, cursor = self.member(cursor) if kind == "object" else self.item(cursor)
            if self.is_punct(cursor, ","):
                entry.comma = self.toks[cursor].start
                cursor += 1
            elif not self.is_punct(cursor, closer):
                raise ConfigSyntaxError(f"expected ',' or '{closer}'", self.tok(cursor).start)
            entries.append(entry)

    def member(self, index: int):
        token, text = self.tok(index), self.text
        if token.kind == "spread":
            value, after = self.value(index + 1)
            return Entry("spread", token.start, Computed(token.start, value.end, "a spread (...)")), after
        if token.kind == "punct" and text[token.start] == "[":
            close = self.matching(index)
            if not self.is_punct(close + 1, ":"):
                raise ConfigSyntaxError("expected ':' after a computed key", self.tok(close + 1).start)
            value, after = self.value(close + 2)
            return Entry("computed-key", token.start, Computed(token.start, value.end, "a computed key")), after
        if token.kind in ("name", "string", "number"):
            if token.kind == "string":
                key, quote = _decode(text, token), text[token.start]
            else:
                key, quote = text[token.start : token.end], ""
            if self.is_punct(index + 1, ":"):
                value, after = self.value(index + 2)
                return Entry("member", token.start, value, key=key, key_quote=quote), after
            if token.kind == "name" and self.is_punct(index + 1, ",}"):  # shorthand {a}: the variable a
                return Entry("member", token.start, Computed(token.start, token.end, "a variable"), key=key), index + 1
            if token.kind == "name" and self.is_punct(index + 1, "("):  # a method
                value, after = self.expression(index)
                return Entry("member", token.start, Computed(token.start, value.end, "a function"), key=key), after
        raise ConfigSyntaxError("expected a key", token.start)

    def item(self, index: int):
        token = self.tok(index)
        if token.kind == "spread":
            value, after = self.value(index + 1)
            return Entry("spread", token.start, Computed(token.start, value.end, "a spread (...)")), after
        if self.is_punct(index, ","):
            raise ConfigSyntaxError("an empty slot in a list", token.start)
        value, after = self.value(index)
        return Entry("item", token.start, value), after


def _significant(text: str, start: int = 0, *, closing: bool = True) -> list[Token]:
    """The tokens from ``start`` without whitespace and comments: up to the bracket that closes the first one, or to the
    end. A string, template literal or comment that never ends is a syntax error here."""

    out: list[Token] = []
    depth = 0
    for token in tokens(text, start):
        if token.unterminated:
            raise ConfigSyntaxError("a string, template literal or comment never ends", token.start)
        if token.kind in ("space", "comment"):
            continue
        out.append(token)
        if closing and token.kind == "punct":
            char = text[token.start]
            if char in "{[(":
                depth += 1
            elif char in "}])":
                depth -= 1
                if depth <= 0:
                    return out
    if closing:
        raise ConfigSyntaxError("the block never closes", start)
    return out


def read_object(text: str, brace: int) -> Container:
    """The object literal whose ``{`` is at ``brace``, with absolute source spans."""

    toks = _significant(text, brace)
    node, after = _Parser(text, toks).value(0)
    if not isinstance(node, Container) or node.kind != "object" or after != len(toks):
        raise ConfigSyntaxError("the config block is not an object literal", brace)
    return node


@dataclass
class ConfigBlock:
    """The ``config {...}`` block of one SQLX text: ``root`` spans from its ``{`` to just after its ``}``."""

    text: str
    keyword: int
    root: Container

    @property
    def start(self) -> int:
        return self.root.start

    @property
    def end(self) -> int:
        return self.root.end

    def member(self, key: str) -> Entry | None:
        found = [e for e in self.root.entries if e.kind == "member" and e.key == key]
        return found[-1] if found else None


def find_config(text: str) -> ConfigBlock | None:
    """The config block of a SQLX text, found as the loader finds it (the first block that starts ``config``), or None.

    Raises :class:`ConfigSyntaxError` when the blocks cannot be split, when there are two config blocks, or when the
    block is not an object literal this reader understands.
    """

    from kumo_sql_prover.sqlx import split_sqlx_sections

    try:
        sections = split_sqlx_sections(text)
    except ValueError as exc:
        raise ConfigSyntaxError(f"the file's blocks could not be split ({exc})") from exc
    offset, found = 0, []
    for kind, body in sections:
        if kind == "block" and body.lstrip().startswith("config"):
            found.append((offset, body))
        offset += len(body)
    if not found:
        return None
    if len(found) > 1:
        raise ConfigSyntaxError("the file has two config blocks", found[1][0])
    start, body = found[0]
    keyword = start + len(body) - len(body.lstrip())
    root = read_object(text, text.index("{", keyword))
    if root.end != start + len(body):
        raise ConfigSyntaxError("the end of the config block is unclear (its braces read differently as JavaScript)", root.start)
    return ConfigBlock(text, keyword, root)


# ------------------------------------------------------------------------------------------------------------ values


class Opaque:
    """A computed part of a value, compared by its source text (whitespace collapsed)."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = " ".join(text.split())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Opaque) and other.text == self.text

    def __hash__(self) -> int:
        return hash(("opaque", self.text))

    def __repr__(self) -> str:
        return f"<computed {self.text}>"


def _tree(node, text: str):
    """A Python value for a node: str, float, bool, None, list, dict; computed parts become :class:`Opaque`.

    An object's earlier members with a repeated key and its spreads are kept under tuple keys, so a change to one is
    still seen by the check that compares two trees.
    """

    if isinstance(node, Scalar):
        return node.value
    if isinstance(node, Computed):
        return Opaque(text[node.start : node.end])
    if node.kind == "array":
        return [_tree(e.value, text) for e in node.entries]
    out: dict = {}
    seen: Counter = Counter()
    for number, entry in enumerate(node.entries):
        if entry.kind == "member":
            if entry.key in out:
                out[("shadowed", entry.key, seen[entry.key])] = out.pop(entry.key)
            seen[entry.key] += 1
            out[entry.key] = _tree(entry.value, text)
        else:
            out[(entry.kind, sum(1 for e in node.entries[:number] if e.kind == entry.kind))] = _tree(entry.value, text)
    return out


def _canon(value):
    """A hashable form of a tree that tells ``true`` from ``1`` and ignores member order."""

    if isinstance(value, bool):
        return ("bool", value)
    if value is None:
        return ("null",)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, Opaque):
        return ("computed", value.text)
    if isinstance(value, list):
        return ("list", tuple(_canon(v) for v in value))
    if isinstance(value, dict):
        return ("object", frozenset((k, _canon(v)) for k, v in value.items()))
    raise TypeError(f"not a config value: {value!r}")


def _has_opaque(value) -> bool:
    if isinstance(value, Opaque):
        return True
    if isinstance(value, list):
        return any(_has_opaque(v) for v in value)
    if isinstance(value, dict):
        return any(not isinstance(k, str) or _has_opaque(v) for k, v in value.items())
    return False


def _has_computed(node) -> bool:
    if isinstance(node, Computed):
        return True
    if isinstance(node, Container):
        return any(e.kind != "member" and e.kind != "item" or _has_computed(e.value) for e in node.entries)
    return False


def _excerpt(text: str, node) -> str:
    snippet = " ".join(text[node.start : node.end].split())
    return snippet if len(snippet) <= 60 else snippet[:57] + "..."


def _describe(node, text: str) -> str:
    if isinstance(node, Computed):
        return f"computed ({node.reason}: {_excerpt(text, node)})"
    if isinstance(node, Container):
        return "an object" if node.kind == "object" else "a list"
    value = node.value
    if isinstance(value, bool) or value is None:
        return "true or false" if isinstance(value, bool) else "null"
    return "a number" if isinstance(value, float) else "a string"


def _show_value(value) -> str:
    """A compact rendering for messages and ``show``."""

    if isinstance(value, Opaque):
        return f"<computed: {value.text}>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_show_value(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = [f"{k}: {_show_value(v)}" if isinstance(k, str) else _show_value(v) for k, v in value.items()]
        return "{" + ", ".join(parts) + "}"
    return repr(value)


# ------------------------------------------------------------------------------------------------------------ commands


class CommandError(ValueError):
    """A command that does not parse, with the offset where it went wrong."""

    def __init__(self, message: str, position: int = 0):
        super().__init__(message)
        self.position = position


@dataclass(frozen=True)
class PathRef:
    """A path into the config block as written, and the keys it resolves to.

    ``labels.x`` resolves to ``bigquery.labels.x`` (``alias``); ``config.x`` is ``x`` with no alias; in a where clause
    a bare ``type``, ``name`` or ``file`` is the action's type (a missing one is ``table``), its name (else the file
    name) and the file's path from the project folder (``special``).
    """

    written: str
    keys: tuple[str, ...]
    alias: bool = False
    special: str = ""

    @property
    def dotted(self) -> str:
        return ".".join(k if _IDENTIFIER_RE.fullmatch(k) else json.dumps(k) for k in self.keys)


@dataclass(frozen=True)
class Edit:
    op: str  # "set", "add" (+=), "remove" (-=), "delete"
    path: PathRef
    value: object = None
    written: str = ""


@dataclass(frozen=True)
class Condition:
    op: str  # "and", "or", "not", or a comparison: "in", "=", "!=", "~", "has", "like"
    parts: tuple = ()
    field: PathRef | None = None
    operands: tuple = ()
    pattern: re.Pattern | None = None


@dataclass(frozen=True)
class Command:
    """A parsed command: edits (empty for ``show``), the paths ``show`` prints, and the where clause."""

    text: str
    edits: tuple[Edit, ...] = ()
    where: Condition | None = None
    paths: tuple[PathRef, ...] = ()

    @property
    def names_assertions(self) -> bool:
        """Whether the where clause names the ``assertion`` type, which lets assertion files be edited."""

        def names(condition: Condition | None) -> bool:
            if condition is None:
                return False
            if condition.op in ("and", "or", "not"):
                return any(names(part) for part in condition.parts)
            return bool(condition.field and condition.field.special == "type" and "assertion" in condition.operands)

        return names(self.where)

    @property
    def uses_label_alias(self) -> bool:
        return any(edit.path.alias for edit in self.edits)


def _path_ref(segments: list[str], quoted_first: bool, written: str, *, where: bool) -> PathRef:
    if where and len(segments) == 1 and not quoted_first and segments[0] in ("type", "name", "file"):
        return PathRef(written, tuple(segments), special=segments[0])
    if segments[0] == "config" and not quoted_first:
        if len(segments) == 1:
            raise CommandError("a path names a key inside the config block, such as config.labels")
        return PathRef(written, tuple(segments[1:]))
    if segments[0] == "labels" and not quoted_first:
        return PathRef(written, ("bigquery", "labels", *segments[1:]), alias=True)
    return PathRef(written, tuple(segments))


# Names that end a value in a command: ``labels.x = "y" where ...``, ``name = "a" and ...``.
_VALUE_STOPS = frozenset({"where", "and", "or"})


class _CommandParser:
    def __init__(self, text: str):
        self.text = text
        try:
            self.toks = _significant(text, closing=False)
        except ConfigSyntaxError as exc:
            raise CommandError(str(exc), exc.position) from exc
        self.index = 0

    def peek(self, offset: int = 0) -> Token | None:
        index = self.index + offset
        return self.toks[index] if index < len(self.toks) else None

    def word(self, offset: int = 0) -> str:
        token = self.peek(offset)
        return self.text[token.start : token.end] if token and token.kind == "name" else ""

    def char(self, offset: int = 0) -> str:
        token = self.peek(offset)
        return self.text[token.start] if token and token.kind in ("punct", "op") else ""

    def where_at(self) -> int:
        token = self.peek()
        return token.start if token else len(self.text)

    def fail(self, message: str) -> CommandError:
        return CommandError(message, self.where_at())

    def path(self, *, where: bool = False) -> PathRef:
        start = self.where_at()
        segments, quoted_first = [], False
        while True:
            token = self.peek()
            if token is None:
                raise self.fail("expected a key")
            if token.kind == "name":
                segments.append(self.text[token.start : token.end])
            elif token.kind == "string":
                segments.append(_decode(self.text, token))
                quoted_first = quoted_first or len(segments) == 1
            else:
                raise self.fail("expected a key (a name, or a quoted key such as \"cost-center\")")
            self.index += 1
            if self.char() == "." and self.peek().kind == "punct":
                self.index += 1
                continue
            break
        end = self.toks[self.index - 1].end
        return _path_ref(segments, quoted_first, self.text[start:end], where=where)

    def literal(self) -> object:
        token = self.peek()
        if token is None:
            raise self.fail("a value is missing")
        parser = _Parser(self.text, self.toks, _VALUE_STOPS)
        word = self.word()
        if token.kind == "name" and word not in ("true", "false", "null") and parser.ends_value(self.index + 1):
            self.index += 1
            return word  # a bare word is a string: there are no variables in a command
        try:
            node, after = parser.value(self.index)
        except ConfigSyntaxError as exc:
            raise CommandError(str(exc), exc.position) from exc
        if _has_computed(node):
            raise CommandError("a value must be written out: a string, number, true, false, null, a list or an object",
                               token.start)
        self.index = after
        return _tree(node, self.text)

    def edit(self) -> Edit:
        start = self.where_at()
        if self.word() == "delete" and self.char(1) not in ("=", "+", "-", "."):
            self.index += 1
            path = self.path()
            return Edit("delete", path, written=self.text[start : self.toks[self.index - 1].end])
        path = self.path()
        first, second = self.peek(), self.peek(1)
        joined = bool(first and second and first.end == second.start and self.char(1) == "=")
        if self.char() == "=":
            op, self.index = "set", self.index + 1
        elif self.char() in ("+", "-") and joined:
            op, self.index = ("add" if self.char() == "+" else "remove"), self.index + 2
        else:
            raise self.fail(f"expected =, += or -= after {path.written}")
        value = self.literal()
        if op in ("add", "remove") and not isinstance(value, (str, float, bool)):
            raise CommandError("+= and -= take one string or number", start)
        return Edit(op, path, value, self.text[start : self.toks[self.index - 1].end])

    def operand(self) -> object:
        token = self.peek()
        if token is None:
            raise self.fail("a value is missing")
        if token.kind == "name" and self.word() not in ("true", "false", "null"):
            self.index += 1
            return self.word(-1)
        if token.kind not in ("string", "number", "name", "template") and not (token.kind == "op" and self.char() == "-"):
            raise self.fail("expected a value")
        return self.literal()

    def condition(self) -> Condition:
        parts = [self.conjunction()]
        while self.word() == "or":
            self.index += 1
            parts.append(self.conjunction())
        return parts[0] if len(parts) == 1 else Condition("or", tuple(parts))

    def conjunction(self) -> Condition:
        parts = [self.negation()]
        while self.word() == "and":
            self.index += 1
            parts.append(self.negation())
        return parts[0] if len(parts) == 1 else Condition("and", tuple(parts))

    def negation(self) -> Condition:
        if self.word() == "not":
            self.index += 1
            return Condition("not", (self.negation(),))
        if self.char() == "(":
            self.index += 1
            inner = self.condition()
            if self.char() != ")":
                raise self.fail("expected ')'")
            self.index += 1
            return inner
        return self.comparison()

    def comparison(self) -> Condition:
        target = self.path(where=True)
        negate = False
        if self.word() == "not" and self.word(1) == "in":
            negate, self.index = True, self.index + 1
        word, char = self.word(), self.char()
        if word == "in":
            self.index += 1
            if self.char() != "(":
                raise self.fail("expected '(' after in")
            self.index += 1
            operands = [self.operand()]
            while self.char() == ",":
                self.index += 1
                operands.append(self.operand())
            if self.char() != ")":
                raise self.fail("expected ',' or ')'")
            self.index += 1
            found = Condition("in", field=target, operands=tuple(operands))
        elif char == "=" or (char == "!" and self.char(1) == "="):
            self.index += 1 if char == "=" else 2
            found = Condition("=" if char == "=" else "!=", field=target, operands=(self.operand(),))
        elif char == "~" or word == "like":
            self.index += 1
            token = self.peek()
            if token is None or token.kind != "string":
                raise self.fail(f"{'~' if char == '~' else 'like'} takes a quoted pattern")
            raw = self.text[token.start + 1 : token.end - 1]
            self.index += 1
            pattern = _glob_pattern(raw) if word == "like" else raw
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise CommandError(f"not a regular expression: {exc}", token.start) from exc
            found = Condition("~" if char == "~" else "like", field=target, operands=(raw,), pattern=compiled)
        elif word == "has":
            self.index += 1
            found = Condition("has", field=target, operands=(self.operand(),))
        else:
            raise self.fail(f"expected in, =, !=, ~, like or has after {target.written}")
        if target.special == "type":
            unknown = [o for o in found.operands if o not in ACTION_TYPES]
            if unknown:
                raise CommandError(f"{unknown[0]!r} is not a Dataform action type ({', '.join(ACTION_TYPES)})",
                                   self.where_at())
        return Condition("not", (found,)) if negate else found

    def end(self) -> None:
        if self.peek() is not None:
            raise self.fail(f"unexpected {self.text[self.peek().start : self.peek().end]!r}")


def parse_command(text: str, *, show: bool = False) -> Command:
    """Parse an edit command (``PATH = VALUE``, ``PATH += V``, ``PATH -= V``, ``delete PATH``; several joined by ``;``)
    or, with ``show``, a list of paths; either may end with ``where CONDITION``. Raises :class:`CommandError`."""

    parser = _CommandParser(text)
    edits: list[Edit] = []
    paths: list[PathRef] = []
    if show:
        while parser.peek() is not None and parser.word() != "where":
            paths.append(parser.path(where=True))
            if parser.char() == ",":
                parser.index += 1
    else:
        if parser.peek() is None or parser.word() == "where":
            raise parser.fail("an edit is missing: PATH = VALUE, PATH += VALUE, PATH -= VALUE or delete PATH")
        edits.append(parser.edit())
        while parser.char() == ";":
            parser.index += 1
            if parser.peek() is None or parser.word() == "where":
                break
            edits.append(parser.edit())
    where = None
    if parser.word() == "where":
        parser.index += 1
        where = parser.condition()
    parser.end()
    for edit in edits:
        if not edit.path.keys:
            raise CommandError("a path names a key inside the config block")
    return Command(text.strip(), tuple(edits), where, tuple(paths))


def _glob_pattern(glob: str) -> str:
    """A regular expression for a path glob: ``**`` crosses folders, ``*`` and ``?`` do not."""

    out, index = [], 0
    while index < len(glob):
        char = glob[index]
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[" and (close := glob.find("]", index + 1)) > index + 1:
            body = glob[index + 1 : close]
            negate = body[0] == "!" and len(body) > 1
            inner = (body[1:] if negate else body).replace("\\", "\\\\").replace("^", "\\^")
            out.append("[" + ("^" if negate else "") + inner + "]")  # a range such as [0-4] stays a range
            index = close + 1
        else:
            out.append(re.escape(char))
            index += 1
    return "\\A" + "".join(out) + "\\Z"


# ------------------------------------------------------------------------------------------------------------ filters

_MISSING = object()


@dataclass
class _Facts:
    """What a where clause can ask of one file."""

    path: str
    text: str
    block: ConfigBlock | None

    def action_type(self) -> tuple[str | None, str]:
        """``(type, "")``, or ``(None, reason)`` when it is computed. A missing type is a table, as the loader reads it."""

        if self.block is None:
            return "table", ""
        entry = self.block.member("type")
        if entry is None:
            return "table", ""
        if isinstance(entry.value, Scalar) and isinstance(entry.value.value, str):
            return entry.value.value, ""
        return None, f"type is {_describe(entry.value, self.text)}"

    def top_level_labels(self) -> bool:
        return self.block is not None and self.block.member("labels") is not None

    def read(self, ref: PathRef):
        """``(value, "")``, ``(_MISSING, "")`` or ``(None, reason)`` for an unknown value."""

        if ref.special == "file":
            return self.path, ""
        if ref.special == "type":
            kind, reason = self.action_type()
            return (kind, "") if kind is not None else (None, reason)
        if ref.special == "name":
            value, reason = self._lookup(("name",))
            if value is _MISSING:
                return Path(self.path).stem, ""
            return (value, reason) if isinstance(value, str) or reason else (None, "name is not a string")
        if ref.alias and self.top_level_labels():
            return None, "the block has a top-level labels member, so labels. is ambiguous here (write bigquery.labels or config.labels)"
        return self._lookup(ref.keys)

    def _lookup(self, keys: tuple[str, ...]):
        if self.block is None:
            return _MISSING, ""
        node = self.block.root
        for depth, key in enumerate(keys):
            where = ".".join(keys[:depth]) or "the config block"
            if isinstance(node, Computed):
                return None, f"{where} is {_describe(node, self.text)}"
            if not isinstance(node, Container) or node.kind != "object":
                return _MISSING, ""
            entries = [e for e in node.entries if e.kind == "member" and e.key == key]
            others = [e for e in node.entries if e.kind not in ("member", "item")]
            if others and (not entries or others[-1].start > entries[-1].start):
                return None, f"{where} holds {others[-1].value.reason}, which may set {key}"
            if not entries:
                return _MISSING, ""
            node = entries[-1].value
        value = _tree(node, self.text)
        if isinstance(value, Opaque):
            return None, f"{'.'.join(keys)} is {_describe(node, self.text)}"
        return value, ""


def _same(left, right) -> bool:
    try:
        return _canon(left) == _canon(right)
    except TypeError:
        return False


def _evaluate(condition: Condition, facts: _Facts) -> tuple[bool | None, str]:
    """Three-valued: ``(True | False | None, reason for None)``."""

    if condition.op in ("and", "or"):
        results = [_evaluate(part, facts) for part in condition.parts]
        decisive = condition.op == "or"
        if any(value is decisive for value, _ in results):
            return decisive, ""
        unknown = [reason for value, reason in results if value is None]
        return (None, unknown[0]) if unknown else (not decisive, "")
    if condition.op == "not":
        value, reason = _evaluate(condition.parts[0], facts)
        return (None if value is None else not value), reason
    value, reason = facts.read(condition.field)
    if value is None and reason:
        return None, reason
    return _compare(condition, value)


def _compare(condition: Condition, value) -> tuple[bool | None, str]:
    op, operands = condition.op, condition.operands
    unknown = (None, f"{condition.field.written} holds a computed part")
    if value is _MISSING:
        return op == "!=", ""
    if op in ("=", "!="):
        if _has_opaque(value):
            return unknown
        return _same(value, operands[0]) == (op == "="), ""
    items = value if isinstance(value, list) else [value]
    if op == "has" and isinstance(value, dict):
        if operands[0] in value:
            return True, ""
        return (None, unknown[1]) if any(not isinstance(k, str) for k in value) else (False, "")
    if op in ("has", "in"):
        wanted = operands if op == "in" else operands[:1]
        if any(any(_same(item, w) for w in wanted) for item in items if not isinstance(item, Opaque)):
            return True, ""
        return unknown if any(isinstance(item, Opaque) for item in items) else (False, "")
    # ~ and like: a string, or any string in a list
    if any(isinstance(item, str) and condition.pattern.search(item) for item in items):
        return True, ""
    return unknown if any(isinstance(item, Opaque) for item in items) else (False, "")


# ------------------------------------------------------------------------------------------------------------ edits


class Refused(Exception):
    """An edit that cannot be made safely; the message says why."""


@dataclass
class _Style:
    """How the block is written, so new text matches it."""

    unit: str = "  "
    quote: str = '"'
    trailing_comma: bool = False
    brace_space: bool = True
    key_quote: str = ""
    newline: str = "\n"


def _line_start(text: str, position: int) -> int:
    return text.rfind("\n", 0, position) + 1


def _line_indent(text: str, position: int) -> str:
    start = _line_start(text, position)
    return re.match(r"[ \t]*", text[start:]).group(0)


def _own_line_indent(text: str, position: int) -> str | None:
    """The whitespace before ``position`` when nothing else precedes it on its line."""

    prefix = text[_line_start(text, position) : position]
    return prefix if prefix.strip(" \t") == "" else None


def _newline_after(text: str, start: int, stop: int) -> int | None:
    """The offset just after the first line break between ``start`` and ``stop`` that is not inside a comment."""

    for token in tokens(text, start):
        if token.start >= stop:
            return None
        if token.kind == "space":
            found = text.find("\n", token.start, min(token.end, stop))
            if found >= 0:
                return found + 1
        elif token.kind != "comment":
            return None
    return None


def _containers(node) -> Iterator[Container]:
    if isinstance(node, Container):
        yield node
        for entry in node.entries:
            yield from _containers(entry.value)


def _anchor(container: Container) -> int:
    last = container.entries[-1]
    return last.comma + 1 if last.comma is not None else last.end


def _multiline(text: str, container: Container) -> bool:
    start = _anchor(container) if container.entries else container.start + 1
    return _newline_after(text, start, container.end - 1) is not None


def _detect_style(text: str, block: ConfigBlock) -> _Style:
    containers = list(_containers(block.root))
    style = _Style()
    piece = text[block.start : block.end]
    crlf = piece.count("\r\n")
    lf = piece.count("\n") - crlf
    if not crlf and not lf:
        crlf = text.count("\r\n")
        lf = text.count("\n") - crlf
    style.newline = "\r\n" if crlf > lf else "\n"
    units: Counter = Counter()
    quotes: Counter = Counter()
    commas: Counter = Counter()
    spaces: Counter = Counter()
    keys: list[str] = []
    for container in containers:
        opening = _line_indent(text, container.start)
        for entry in container.entries:
            indent = _own_line_indent(text, entry.start)
            if indent is not None and indent.startswith(opening) and len(indent) > len(opening):
                units[indent[len(opening) :]] += 1
            if isinstance(entry.value, Scalar) and isinstance(entry.value.value, str):
                quotes[entry.value.quote] += 1
            if entry.kind == "member":
                keys.append(entry.key_quote)
        if container.entries and _multiline(text, container):
            commas[container.entries[-1].comma is not None] += 1
        elif container.entries and container.kind == "object":
            spaces[text[container.start + 1 : container.start + 2] in (" ", "\t")] += 1
    if units:
        style.unit = sorted(units.items(), key=lambda item: (-item[1], len(item[0])))[0][0]
    if quotes:
        style.quote = sorted(quotes.items(), key=lambda item: (-item[1], item[0] != '"'))[0][0]
    style.trailing_comma = commas[True] > commas[False]
    if spaces:
        style.brace_space = spaces[True] >= spaces[False]
    if keys and all(keys):
        style.key_quote = Counter(keys).most_common(1)[0][0]
    return style


def _container_quote(container: Container, style: _Style) -> str:
    found = Counter(e.value.quote for e in container.entries
                    if isinstance(e.value, Scalar) and isinstance(e.value.value, str))
    return sorted(found.items(), key=lambda item: (-item[1], item[0] != style.quote))[0][0] if found else style.quote


def _container_key_quote(container: Container, style: _Style) -> str:
    members = [e for e in container.entries if e.kind == "member"]
    if members:
        return members[-1].key_quote if all(e.key_quote for e in members) else ""
    return style.key_quote


def js_string(value: str, quote: str) -> str:
    """``value`` as a JavaScript string literal in ``quote``."""

    body = value.replace("\\", "\\\\").replace(quote, "\\" + quote)
    body = body.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if quote == "`":
        body = body.replace("${", "\\${")
    return quote + body + quote


def _render_key(key: str, key_quote: str, quote: str) -> str:
    if key_quote:
        return js_string(key, key_quote)
    if _IDENTIFIER_RE.fullmatch(key):
        return key
    return js_string(key, quote if quote != "`" else '"')


def _render(value, style: _Style, quote: str, key_quote: str, indent: str, multiline: bool, newline: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return _show_value(value)
    if isinstance(value, str):
        return js_string(value, quote)
    if isinstance(value, list):
        return "[" + ", ".join(_render(v, style, quote, key_quote, indent, False, newline) for v in value) + "]"
    if not value:
        return "{}"
    if multiline:
        inner = indent + style.unit
        parts = [inner + _render_key(k, key_quote, quote) + ": " + _render(v, style, quote, key_quote, inner, True, newline)
                 for k, v in value.items()]
        return "{" + newline + ("," + newline).join(parts) + ("," if style.trailing_comma else "") + newline + indent + "}"
    pad = " " if style.brace_space else ""
    parts = [_render_key(k, key_quote, quote) + ": " + _render(v, style, quote, key_quote, indent, False, newline)
             for k, v in value.items()]
    return "{" + pad + ", ".join(parts) + pad + "}"


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(edits, key=lambda edit: edit[0], reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _insert_entry(text: str, container: Container, render: Callable[[str, bool, str], str], style: _Style) -> str:
    """``text`` with one entry added at the end of ``container``, laid out like its neighbours.

    ``render(indent, multiline, newline)`` gives the entry's text. In a container written over several lines the
    entry gets its own line after the last one, with that line's indentation and line ending, and the container's
    trailing-comma habit; in a one-line container it follows the last entry after ``, ``.
    """

    close = container.end - 1
    if container.entries:
        last = container.entries[-1]
        anchor = _anchor(container)
        after = _newline_after(text, anchor, close)
        if after is not None:
            newline = "\r\n" if text[after - 2 : after] == "\r\n" else "\n"
            indent = next((found for e in reversed(container.entries)
                           if (found := _own_line_indent(text, e.start)) is not None),
                          _line_indent(text, container.start) + style.unit)
            trailing = last.comma is not None
            edits = [(after, after, indent + render(indent, True, newline) + ("," if trailing else "") + newline)]
            if not trailing:
                edits.append((last.end, last.end, ","))
            return _splice(text, edits)
        if last.comma is not None:
            return _splice(text, [(last.comma + 1, last.comma + 1, " " + render("", False, style.newline) + ",")])
        return _splice(text, [(last.end, last.end, ", " + render("", False, style.newline))])
    inner = text[container.start + 1 : close]
    after = _newline_after(text, container.start + 1, close)
    if after is not None:
        newline = "\r\n" if text[after - 2 : after] == "\r\n" else "\n"
        indent = _line_indent(text, container.start) + style.unit
        body = indent + render(indent, True, newline) + ("," if style.trailing_comma else "") + newline
        return _splice(text, [(after, after, body)])  # the closing bracket keeps its own line
    pad = " " if container.kind == "object" and style.brace_space else ""
    if inner.strip(" \t") == "":
        return _splice(text, [(container.start + 1, close, pad + render("", False, style.newline) + pad)])
    return _splice(text, [(container.start + 1, container.start + 1, pad + render("", False, style.newline) + ",")])


def _rest_of_line(text: str, position: int) -> int | None:
    """The offset after the line break that ends this line when only blanks or a ``//`` comment follow ``position``."""

    index = position
    while index < len(text) and text[index] in " \t":
        index += 1
    if text.startswith("//", index):
        found = text.find("\n", index)
        return len(text) if found < 0 else found + 1
    if text.startswith("\r\n", index):
        return index + 2
    if index >= len(text) or text[index] == "\n":
        return min(index + 1, len(text))
    return None


def _has_comment(text: str, start: int, end: int) -> bool:
    return any(token.kind == "comment" for token in tokens(text[start:end]))


def _remove_entry(text: str, container: Container, index: int) -> str:
    """``text`` without entry ``index`` of ``container``: the entry's line when it has one to itself (with a comment
    at its end), else the entry and one comma; the comma before a removed last entry goes too, keeping the habit."""

    entries = container.entries
    entry = entries[index]
    end = entry.comma + 1 if entry.comma is not None else entry.end
    if len(entries) == 1:
        inner_start, inner_end = container.start + 1, container.end - 1
        if not _has_comment(text, inner_start, entry.start) and not _has_comment(text, end, inner_end):
            return _splice(text, [(inner_start, inner_end, "")])
        return _splice(text, [(entry.start, end, "")])
    last = index == len(entries) - 1
    line_end = _rest_of_line(text, end)
    if _own_line_indent(text, entry.start) is not None and line_end is not None:
        edits = [(_line_start(text, entry.start), line_end, "")]
        if last and entry.comma is None and entries[index - 1].comma is not None:
            comma = entries[index - 1].comma
            edits.append((comma, comma + 1, ""))
        return _splice(text, edits)
    if not last:
        stop = end
        while stop < len(text) and text[stop] in " \t":
            stop += 1
        return _splice(text, [(entry.start, stop, "")])
    previous = entries[index - 1]
    if entry.comma is not None:
        return _splice(text, [(previous.comma + 1, end, "")])
    return _splice(text, [(previous.comma, entry.end, "")])


def _locate(block: ConfigBlock, keys: tuple[str, ...]) -> tuple[Container, Entry | None, int]:
    """``(object, member or None, depth)``: the member at ``keys``, or the deepest object on the way and how many keys
    it covers. Refuses a path through a computed value, a non-object, a spread or a key written twice."""

    text, node = block.text, block.root
    for depth, key in enumerate(keys):
        where = ".".join(keys[:depth]) or "the config block"
        if isinstance(node, Computed):
            raise Refused(f"{where} is {_describe(node, text)}")
        if not isinstance(node, Container) or node.kind != "object":
            raise Refused(f"{where} is {_describe(node, text)}, not an object")
        special = [e for e in node.entries if e.kind in ("spread", "computed-key")]
        if special:
            raise Refused(f"{where} holds {special[0].value.reason} ({_excerpt(text, special[0].value)}), which may set any key")
        found = [e for e in node.entries if e.kind == "member" and e.key == key]
        if len(found) > 1:
            raise Refused(f"{'.'.join(keys[: depth + 1])} is written twice in one object")
        if not found:
            return node, None, depth
        if depth == len(keys) - 1:
            return node, found[0], depth
        node = found[0].value
    raise AssertionError("unreachable")


def _apply_one(text: str, block: ConfigBlock, edit: Edit) -> tuple[str, str]:
    """``(new text, what changed)``; the text is unchanged (and the note empty) when the edit is a no-op."""

    style = _detect_style(text, block)
    keys, dotted = edit.path.keys, edit.path.dotted
    container, entry, depth = _locate(block, keys)
    quote = _container_quote(container, style)
    key_quote = _container_key_quote(container, style)

    def insert(value) -> str:
        nested = value
        for key in reversed(keys[depth + 1 :]):
            nested = {key: nested}

        def render(indent: str, multiline: bool, newline: str) -> str:
            return (_render_key(keys[depth], key_quote, quote) + ": "
                    + _render(nested, style, quote, style.key_quote, indent, multiline, newline))

        return _insert_entry(text, container, render, style)

    if edit.op == "delete":
        if entry is None:
            return text, ""
        if _has_computed(entry.value):
            raise Refused(f"{dotted} is {_describe(entry.value, text)}")
        return _remove_entry(text, container, container.entries.index(entry)), f"{dotted}: deleted"
    if entry is None:
        if edit.op == "remove":
            return text, ""
        value = edit.value if edit.op == "set" else [edit.value]
        return insert(value), f"{dotted}: added {_show_value(value)}"
    old = entry.value
    if _has_computed(old):
        if edit.op in ("add", "remove") and isinstance(old, Container):
            raise Refused(f"{dotted} holds a computed entry ({_excerpt(text, old)}), so whether "
                          f"{_show_value(edit.value)} is in it is unknown")
        raise Refused(f"{dotted} is {_describe(old, text)}")
    current = _tree(old, text)
    if edit.op == "set":
        if _same(current, edit.value):
            return text, ""
        multiline = isinstance(old, Container) and old.entries and _multiline(text, old)
        indent = _line_indent(text, entry.start)
        new_quote = old.quote if isinstance(old, Scalar) and isinstance(old.value, str) and old.quote else quote
        rendered = _render(edit.value, style, new_quote, style.key_quote, indent, bool(multiline), style.newline)
        return _splice(text, [(old.start, old.end, rendered)]), f"{dotted}: {_show_value(current)} -> {_show_value(edit.value)}"
    if isinstance(old, Scalar):
        if isinstance(old.value, (str, float)) and not isinstance(old.value, bool):
            if edit.op == "add":
                if _same(current, edit.value):
                    return text, ""
                item_quote = old.quote or quote
                rendered = "[" + text[old.start : old.end] + ", " + _render(edit.value, style, item_quote, "", "", False, "\n") + "]"
                return _splice(text, [(old.start, old.end, rendered)]), f"{dotted}: added {_show_value(edit.value)}"
            if _same(current, edit.value):
                return _splice(text, [(old.start, old.end, "[]")]), f"{dotted}: removed {_show_value(edit.value)}"
            return text, ""
        raise Refused(f"{dotted} is {_describe(old, text)}, not a list")
    if old.kind != "array":
        raise Refused(f"{dotted} is an object, not a list")
    matches = [i for i, e in enumerate(old.entries) if _same(_tree(e.value, text), edit.value)]
    if edit.op == "add":
        if matches:
            return text, ""
        item_quote = _container_quote(old, style)
        new = _insert_entry(text, old, lambda indent, multiline, newline: _render(
            edit.value, style, item_quote, "", indent, False, newline), style)
        return new, f"{dotted}: added {_show_value(edit.value)}"
    if not matches:
        return text, ""
    return _remove_entry(text, old, matches[0]), f"{dotted}: removed {_show_value(edit.value)}"


def _expected(tree: dict, edit: Edit) -> None:
    """Apply ``edit`` to a parsed tree, as the text edit should have."""

    keys = edit.path.keys
    node = tree
    for key in keys[:-1]:
        if key not in node:
            if edit.op in ("remove", "delete"):
                return
            node[key] = {}
        node = node[key]
    last = keys[-1]
    if edit.op == "set":
        node[last] = edit.value
    elif edit.op == "delete":
        node.pop(last, None)
    elif edit.op == "add":
        current = node.get(last, _MISSING)
        if current is _MISSING:
            node[last] = [edit.value]
        elif isinstance(current, list):
            if not any(_same(item, edit.value) for item in current):
                current.append(edit.value)
        elif not _same(current, edit.value):
            node[last] = [current, edit.value]
    else:
        current = node.get(last, _MISSING)
        if isinstance(current, list):
            node[last] = [item for item in current if not _same(item, edit.value)]
        elif current is not _MISSING and _same(current, edit.value):
            node[last] = []


def _apply_edits(text: str, block: ConfigBlock, command: Command) -> tuple[str, list[str]]:
    notes: list[str] = []
    for edit in command.edits:
        while True:
            new, note = _apply_one(text, block, edit)
            if note:
                notes.append(note)
            if new != text:
                text, block = new, find_config(new)
            if edit.op != "remove" or not note:
                break  # -= removes every copy: run it again until it finds none
    return text, notes


def _line_ends(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "crlf" if crlf and not lf else "lf" if lf and not crlf else "mixed" if crlf else "none"


@dataclass(frozen=True)
class _Expectation:
    """What an edited text must be: the same text around the block, and a block that reads back as ``canon``."""

    prefix: str
    suffix: str
    canon: object
    line_ends: str


def _expectation(before: str, block: ConfigBlock, command: Command) -> _Expectation:
    expected = _tree(block.root, before)
    for edit in command.edits:
        _expected(expected, edit)
    return _Expectation(before[: block.start], before[block.end :], _canon(expected),
                        _line_ends(before[block.start : block.end]))


def _verify(after: str, expect: _Expectation) -> str:
    """Why ``after`` is not exactly the intended result (empty when it is): it is read again from scratch."""

    try:
        block = find_config(after)
    except ConfigSyntaxError as exc:
        return f"the edited block does not read back ({exc})"
    if block is None:
        return "the edited file has no config block"
    if after[: block.start] != expect.prefix or after[block.end :] != expect.suffix:
        return "text outside the config block changed"
    if _canon(_tree(block.root, after)) != expect.canon:
        return "the block read back does not hold exactly the intended change"
    ends = _line_ends(after[block.start : block.end])
    if expect.line_ends in ("crlf", "lf") and ends not in (expect.line_ends, "none"):
        return f"the block's line ends changed from {expect.line_ends} to {ends}"
    return ""


# ------------------------------------------------------------------------------------------------------------ files


@dataclass
class FileResult:
    """What an edit does to one file.

    ``status`` is ``changed``, ``unchanged`` (the edit is a no-op), ``skipped`` (``reason`` says why: an assertion
    file, or labels on a type that has none), ``refused`` (``reason`` names the computed part or the problem) or
    ``unmatched`` (the where clause rules it out).
    """

    path: str
    status: str
    reason: str = ""
    category: str = ""
    action_type: str | None = None
    notes: list[str] = field(default_factory=list)
    sha256: str = ""
    sha256_after: str = ""
    line_ends: str = ""
    bom: bool = False
    inline_assertions: bool = False
    diff: str = ""
    before: bytes = field(default=b"", repr=False)
    after: bytes = field(default=b"", repr=False)
    expect: _Expectation | None = field(default=None, repr=False)

    def to_json(self) -> dict:
        out = {"path": self.path, "status": self.status}
        for key in ("reason", "category", "action_type", "sha256", "sha256_after", "line_ends", "diff"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.notes:
            out["edits"] = self.notes
        if self.bom:
            out["bom"] = True
        return out


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _diff(path: str, before: str, after: str, context: int = 3) -> str:
    def lines(text: str) -> list[str]:
        return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]

    return "\n".join(difflib.unified_diff(lines(before), lines(after), f"a/{path}", f"b/{path}", n=context, lineterm=""))


def edit_file(data: bytes, path: str, command: Command, *, context: int = 3) -> FileResult:
    """Plan ``command`` on one file's bytes (``path`` is its POSIX path from the project folder). Writes nothing."""

    result = FileResult(path, "refused", sha256=_sha(data), before=data)
    result.bom = data.startswith(_BOM)
    try:
        text = data[len(_BOM) if result.bom else 0 :].decode("utf-8")
    except UnicodeDecodeError:
        result.reason, result.category = "not UTF-8 text (this tool edits UTF-8 files only)", "unreadable"
        return result
    result.line_ends = _line_ends(text)
    try:
        block = find_config(text)
    except ConfigSyntaxError as exc:
        line = text.count("\n", 0, exc.position) + 1
        result.reason, result.category = f"the config block could not be read: {exc} (line {line})", "unreadable"
        return result
    facts = _Facts(path, text, block)
    kind, kind_reason = facts.action_type()
    result.action_type = kind
    named = command.names_assertions
    if kind == "assertion" and not named:
        result.status, result.category = "skipped", "assertion"
        result.reason = "an assertion file (name the type, as in where type in (assertion), to edit it)"
        return result
    if command.where is not None:
        matched, why = _evaluate(command.where, facts)
        if matched is False:
            result.status = "unmatched"
            return result
        if matched is None:
            result.reason, result.category = f"the where clause cannot be decided: {why}", "computed"
            return result
    if kind is None and not named:
        result.reason, result.category = f"{kind_reason}, so it may be an assertion", "computed"
        return result
    if block is None:
        result.reason, result.category = "no config block (this tool edits existing blocks only)", "no-config"
        return result
    result.inline_assertions = block.member("assertions") is not None
    if command.uses_label_alias:
        if kind is None:
            result.reason, result.category = f"{kind_reason}, so whether labels apply is unknown", "computed"
            return result
        if kind not in LABEL_TYPES:
            result.status, result.category = "skipped", "labels-type"
            result.reason = f"labels apply to {', '.join(LABEL_TYPES)} actions, not {kind} (write bigquery.labels to set them anyway)"
            return result
        if facts.top_level_labels():
            result.reason, result.category = ("the block has a top-level labels member, so labels. is ambiguous here "
                                              "(write bigquery.labels or config.labels)"), "ambiguous"
            return result
    try:
        new_text, notes = _apply_edits(text, block, command)
    except Refused as exc:
        result.reason, result.category = str(exc), "computed"
        return result
    except ConfigSyntaxError as exc:  # an edit that leaves an unreadable block is a bug: never write it
        result.reason, result.category = f"internal check failed: {exc}", "check"
        return result
    if new_text == text:
        result.status, result.category = "unchanged", "no-op"
        return result
    result.expect = _expectation(text, block, command)
    problem = _verify(new_text, result.expect)
    if problem:
        result.reason, result.category = f"internal check failed: {problem}; nothing will be written", "check"
        return result
    result.status, result.notes = "changed", notes
    result.after = (_BOM if result.bom else b"") + new_text.encode("utf-8")
    result.sha256_after = _sha(result.after)
    result.diff = _diff(path, text, new_text, context)
    return result


# ------------------------------------------------------------------------------------------------------------ projects


def _project_files(root: Path, globs: Sequence[str] = ()) -> tuple[list[tuple[str, Path]], list[str]]:
    """``([(posix path from the root, path to read)], problems)`` for the project's ``.sqlx`` files, chosen as the loader
    chooses them: under ``definitions/`` when there is one, without ``node_modules``, ``.git`` or a nested project."""

    from .resilience import extended_path, find_assets

    base = extended_path(root)
    definitions = base / "definitions"
    search = definitions if definitions.is_dir() and not definitions.is_symlink() else base
    problems: list[str] = []
    nested: dict[tuple[str, ...], bool] = {}

    def inside_other_project(parts: tuple[str, ...]) -> bool:
        if not parts:
            return False
        if parts not in nested:
            here = base.joinpath(*parts)
            nested[parts] = any((here / marker).exists() for marker in (".git", "workflow_settings.yaml", "dataform.json")) \
                or inside_other_project(parts[:-1])
        return nested[parts]

    def on_error(path: Path, reason: str) -> None:
        try:
            shown = path.relative_to(base).as_posix()
        except ValueError:
            shown = str(path)
        problems.append(f"{shown}: {reason}")

    patterns = [re.compile(_glob_pattern(glob.replace("\\", "/"))) for glob in globs]
    found = []
    for path in find_assets(search, (".sqlx",), on_error):
        parts = path.relative_to(base).parts
        if "node_modules" in parts or ".git" in parts or inside_other_project(parts[:-1]):
            continue
        relative = "/".join(parts)
        if patterns and not any(pattern.match(relative) for pattern in patterns):
            continue
        found.append((relative, path))
    return found, problems


@dataclass
class EditPlan:
    """The planned result of one command on a project: one :class:`FileResult` per ``.sqlx`` file read."""

    root: Path
    command: Command
    globs: tuple[str, ...]
    results: list[FileResult]
    problems: list[str] = field(default_factory=list)

    @property
    def changed(self) -> list[FileResult]:
        return [r for r in self.results if r.status == "changed"]

    @property
    def refused(self) -> list[FileResult]:
        return [r for r in self.results if r.status == "refused"]

    def summary(self) -> dict:
        counts = Counter(r.status for r in self.results)
        skipped = Counter(r.category for r in self.results if r.status == "skipped")
        return {
            "files": len(self.results),
            "matched": len(self.results) - counts["unmatched"] - skipped["assertion"],
            "changed": counts["changed"],
            "unchanged": counts["unchanged"],
            "skipped_assertion": skipped["assertion"],
            "skipped_labels_type": skipped["labels-type"],
            "refused": counts["refused"],
            "unmatched": counts["unmatched"],
            "inline_assertions_kept": sum(1 for r in self.changed if r.inline_assertions
                                          and not any(e.path.keys[0] == "assertions" for e in self.command.edits)),
            "unlisted": len(self.problems),
        }

    def to_json(self) -> dict:
        from . import __version__

        return {
            "kind": PLAN_KIND,
            "version": PLAN_VERSION,
            "kumosql_version": __version__,
            "created": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "project": str(self.root),
            "command": self.command.text,
            "files_globs": list(self.globs),
            "summary": self.summary(),
            "files": [r.to_json() for r in self.changed],
            "refused": [{"path": r.path, "reason": r.reason} for r in self.refused],
            "skipped": [{"path": r.path, "reason": r.reason} for r in self.results if r.status == "skipped"],
            "unlisted": self.problems,
        }


def plan_edit(root: Path | str, command: Command | str, files: Sequence[str] = (), *, context: int = 3) -> EditPlan:
    """Plan ``command`` on every ``.sqlx`` file of the project at ``root`` (optionally only those matching ``files``,
    path globs from the project folder). Reads only; nothing is written."""

    root = Path(root).resolve()
    if isinstance(command, str):
        command = parse_command(command)
    found, problems = _project_files(root, files)
    results = []
    for relative, path in found:
        try:
            data = path.read_bytes()
        except OSError as exc:
            results.append(FileResult(relative, "refused", reason=f"could not be read ({exc.strerror or exc})", category="unreadable"))
            continue
        results.append(edit_file(data, relative, command, context=context))
    return EditPlan(root, command, tuple(files), results, problems)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to a temporary file beside ``path`` and move it over ``path`` in one step, keeping its mode."""

    path = Path(path)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent.resolve()))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@dataclass
class Written:
    path: str
    ok: bool
    reason: str = ""


def _write_checked(path: Path, result: FileResult) -> Written:
    """Write one planned file if it still holds what was planned, then read it back and check it again: the same
    bytes, and a config block that reads back as the old one with exactly the intended change."""

    try:
        current = path.read_bytes()
    except OSError as exc:
        return Written(result.path, False, f"could not be read ({exc.strerror or exc})")
    if _sha(current) != result.sha256:
        return Written(result.path, False, "changed since it was planned; nothing was written (plan again)")
    try:
        _write_atomic(path, result.after)
        written = path.read_bytes()
    except OSError as exc:
        return Written(result.path, False, f"could not be written ({exc.strerror or exc})")
    if written != result.after:
        return Written(result.path, False, "the file read back differs from what was written")
    try:
        problem = _verify(written[len(_BOM) if result.bom else 0 :].decode("utf-8"), result.expect)
    except UnicodeDecodeError as exc:
        problem = str(exc)
    if problem:
        return Written(result.path, False, f"written, but the check after writing failed: {problem}")
    return Written(result.path, True)


def apply_plan(plan: EditPlan) -> list[Written]:
    """Write every changed file of ``plan`` atomically; a file that changed since it was planned is refused."""

    from .resilience import extended_path

    base = extended_path(plan.root)
    return [_write_checked(base / result.path, result) for result in plan.changed]


def load_plan(path: Path | str) -> dict:
    saved = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(saved, dict) or saved.get("kind") != PLAN_KIND:
        raise ValueError(f"{path} is not a sqlx-config plan")
    if saved.get("version") != PLAN_VERSION:
        raise ValueError(f"{path} is a version {saved.get('version')} plan; this KumoSQL reads version {PLAN_VERSION}")
    return saved


def apply_saved_plan(root: Path | str, saved: dict) -> tuple[list[Written], Command]:
    """Apply a plan saved by ``--plan``: each listed file must still have the sha256 it had when planned, and the
    command must still give the planned bytes; any other file is refused, and files not in the plan are not touched."""

    from .resilience import extended_path

    command = parse_command(saved["command"])
    base = extended_path(Path(root).resolve())
    out = []
    for entry in saved.get("files", []):
        relative = entry["path"]
        if relative.startswith("/") or ".." in relative.split("/"):
            out.append(Written(relative, False, "not a path inside the project"))
            continue
        path = base / relative
        try:
            data = path.read_bytes()
        except OSError as exc:
            out.append(Written(relative, False, f"could not be read ({exc.strerror or exc})"))
            continue
        if _sha(data) != entry.get("sha256"):
            out.append(Written(relative, False, "changed since the plan was made (its sha256 differs); nothing was written"))
            continue
        result = edit_file(data, relative, command)
        if result.status != "changed" or result.sha256_after != entry.get("sha256_after"):
            out.append(Written(relative, False, "the command no longer gives the planned result; nothing was written"))
            continue
        out.append(_write_checked(path, result))
    return out, command


@dataclass
class ShowRow:
    path: str
    action_type: str | None
    values: list[tuple[str, str]]
    status: str = "matched"  # or "unknown" (the where clause cannot be decided) or "unreadable"
    reason: str = ""


def show_project(root: Path | str, command: Command | str, files: Sequence[str] = ()) -> tuple[list[ShowRow], int, list[str]]:
    """``(rows, files read, problems)``: the files the where clause selects, with the values of the command's paths
    (by default type, name, tags and labels). Assertion files are listed too: showing changes nothing."""

    root = Path(root).resolve()
    if isinstance(command, str):
        command = parse_command(command, show=True)
    paths = command.paths or tuple(_path_ref([name], False, name, where=True) for name in ("type", "name")) + (
        _path_ref(["tags"], False, "tags", where=False), _path_ref(["labels"], False, "labels", where=False))
    found, problems = _project_files(root, files)
    rows = []
    for relative, path in found:
        try:
            data = path.read_bytes()
            text = data.decode("utf-8-sig")
            block = find_config(text)
        except (OSError, UnicodeDecodeError, ConfigSyntaxError) as exc:
            rows.append(ShowRow(relative, None, [], "unreadable", str(exc)))
            continue
        facts = _Facts(relative, text, block)
        if command.where is not None:
            matched, why = _evaluate(command.where, facts)
            if matched is False:
                continue
            if matched is None:
                rows.append(ShowRow(relative, facts.action_type()[0], [], "unknown", why))
                continue
        values = []
        for ref in paths:
            value, reason = facts.read(ref)
            shown = "-" if value is _MISSING else f"<unknown: {reason}>" if reason else _show_value(value)
            values.append((ref.written, shown))
        rows.append(ShowRow(relative, facts.action_type()[0], values))
    return rows, len(found), problems


# ------------------------------------------------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql sqlx-config",
        description="Read and edit the config {} blocks of a Dataform project's .sqlx files as JavaScript, keeping "
                    "every byte an edit does not change. show only prints. edit is a PREVIEW BY DEFAULT: it prints a "
                    "diff and a summary; only --apply writes files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python -m kumosql sqlx-config show PROJECT 'labels.team, tags where type in (table, view)'\n"
               "  python -m kumosql sqlx-config edit PROJECT 'labels.team = \"finance\" where type in (table, view)'\n"
               "  python -m kumosql sqlx-config edit PROJECT 'tags += \"finance\"' --plan plan.json   (preview, saves a plan)\n"
               "  python -m kumosql sqlx-config edit PROJECT --apply --plan plan.json              (writes the plan)\n"
               "\nlabels.KEY means bigquery.labels.KEY. Assertion files are never edited unless the where clause names\n"
               "the type (where type in (assertion)). See docs/sqlx-config-edits.md for the language.")
    commands = parser.add_subparsers(dest="action", metavar="COMMAND")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("project", type=Path, help="the Dataform project folder (its definitions/ folder is read)")
    common.add_argument("selector", nargs="?", default=None, help="the command, in one quoted argument")
    common.add_argument("--command-file", metavar="FILE",
                        help="read the command from FILE ('-' for standard input) instead, so no shell can change its quotes")
    common.add_argument("--files", action="append", default=[], metavar="GLOB",
                        help="only files whose path from the project folder matches GLOB (** crosses folders; repeatable)")
    common.add_argument("--json", action="store_true", help="print JSON")
    show = commands.add_parser("show", parents=[common], help="print config values of the files a where clause selects",
                               description="Print the files a where clause selects, with the values of the paths named "
                                           "before it (default: type, name, tags, labels). Only prints.")
    show.add_argument("--limit", type=int, default=100, help="print at most this many files (0 for all; default 100)")
    edit = commands.add_parser("edit", parents=[common], help="change config blocks; previews a diff unless --apply",
                               description="Plan an edit on every .sqlx file the command selects, check each result by "
                                           "reading it back, and print a unified diff and a summary. PREVIEW BY DEFAULT: "
                                           "only --apply writes files (atomically, each re-read and checked). --plan FILE "
                                           "saves the plan with each file's sha256; --apply --plan FILE writes exactly "
                                           "that plan and refuses any file that changed since. Exit 3 when a file is "
                                           "refused.")
    edit.add_argument("--plan", type=Path, metavar="FILE",
                      help="without --apply: save the plan here; with --apply: write the changes this saved plan lists")
    edit.add_argument("--apply", action="store_true", help="write the changes into the project's files")
    edit.add_argument("--stat", action="store_true", help="one line per changed file instead of the full diff")
    edit.add_argument("--context", type=int, default=3, help="lines of context in the diff (default 3)")
    return parser


def _read_command(args) -> str | None:
    if args.command_file:
        if args.command_file == "-":
            return sys.stdin.read().lstrip("\ufeff")
        return Path(args.command_file).read_text(encoding="utf-8-sig")
    return args.selector


def _print_command_error(text: str, error: CommandError) -> None:
    print(f"sqlx-config: {error}", file=sys.stderr)
    if text and "\n" not in text:
        print(f"  {text}\n  {' ' * min(error.position, len(text))}^", file=sys.stderr)


def _summary_lines(plan: EditPlan, written: list[Written] | None) -> list[str]:
    s = plan.summary()
    where = "definitions/" if (plan.root / "definitions").is_dir() else "the project folder"
    lines = [f"command: {plan.command.text}",
             f"  files read          {s['files']:>6}  (.sqlx under {where}"
             + (f", matching {', '.join(plan.globs)}" if plan.globs else "") + ")",
             f"  matched             {s['matched']:>6}",
             f"  changed             {s['changed']:>6}",
             f"  already as asked    {s['unchanged']:>6}  (no-op: left byte-identical)",
             f"  skipped: assertion  {s['skipped_assertion']:>6}  (assertion files are edited only when the where clause names type assertion)"]
    if s["skipped_labels_type"]:
        lines.append(f"  skipped: no labels  {s['skipped_labels_type']:>6}  (labels apply to {', '.join(LABEL_TYPES)} only)")
    lines.append(f"  refused             {s['refused']:>6}")
    for result in plan.refused[:50]:
        lines.append(f"    {result.path}: {result.reason}")
    if len(plan.refused) > 50:
        lines.append(f"    ... {len(plan.refused) - 50} more (see --json or the plan file)")
    if s["inline_assertions_kept"]:
        lines.append(f"  inline assertions blocks in changed files left as they were: {s['inline_assertions_kept']}")
    for problem in plan.problems:
        lines.append(f"  not read: {problem}")
    if written is not None:
        done = [w for w in written if w.ok]
        lines.append(f"  written             {len(done):>6}")
        for failure in (w for w in written if not w.ok):
            lines.append(f"    NOT WRITTEN {failure.path}: {failure.reason}")
    return lines


def _edit_main(args) -> int:
    text = _read_command(args)
    root = args.project.resolve()
    if not root.is_dir():
        print(f"sqlx-config: {args.project} is not a folder", file=sys.stderr)
        return EXIT_USAGE
    if args.apply and args.plan is not None:
        try:
            saved = load_plan(args.plan)
        except (OSError, ValueError) as exc:
            print(f"sqlx-config: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if Path(saved.get("project", "")).resolve() != root:
            print(f"sqlx-config: the plan was made for {saved.get('project')}, not {root}", file=sys.stderr)
            return EXIT_USAGE
        if text is not None and text.strip() != saved["command"]:
            print("sqlx-config: the command differs from the plan's; leave it out to apply the plan", file=sys.stderr)
            return EXIT_USAGE
        try:
            written, _command = apply_saved_plan(root, saved)
        except CommandError as exc:
            _print_command_error(saved["command"], exc)
            return EXIT_USAGE
        if args.json:
            print(json.dumps({"applied": [w.__dict__ for w in written]}, indent=2))
        else:
            print(f"applied the plan {args.plan}: {saved['command']}")
            for item in written:
                print(f"  {'written    ' if item.ok else 'NOT WRITTEN'} {item.path}" + ("" if item.ok else f": {item.reason}"))
            print(f"  {sum(w.ok for w in written)} of {len(written)} planned files written")
        return EXIT_OK if all(w.ok for w in written) else EXIT_REFUSED
    if not text:
        print("sqlx-config: give the command (for example 'labels.team = \"finance\" where type in (table, view)')",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        command = parse_command(text)
    except CommandError as exc:
        _print_command_error(text, exc)
        return EXIT_USAGE
    plan = plan_edit(root, command, args.files, context=args.context)
    written = apply_plan(plan) if args.apply else None
    if args.plan is not None:
        try:
            _write_atomic(args.plan, json.dumps(plan.to_json(), indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError as exc:
            print(f"sqlx-config: the plan could not be saved in {args.plan} ({exc.strerror or exc})", file=sys.stderr)
            return EXIT_USAGE
    if args.json:
        out = plan.to_json()
        if written is not None:
            out["applied"] = [w.__dict__ for w in written]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for result in plan.changed:
            if args.stat:
                added = sum(1 for line in result.diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
                removed = sum(1 for line in result.diff.splitlines() if line.startswith("-") and not line.startswith("---"))
                print(f"{result.path}  +{added} -{removed}  {'; '.join(result.notes)}")
            else:
                print(result.diff)
        print("\n".join(_summary_lines(plan, written)))
        if written is None:
            saved = f" The plan is saved in {args.plan}; --apply --plan {args.plan} writes it." if args.plan else ""
            print(f"PREVIEW: nothing was written.{saved} Run again with --apply to write.")
    failed = written is not None and not all(w.ok for w in written)
    return EXIT_REFUSED if plan.refused or failed else EXIT_OK


def _show_main(args) -> int:
    text = _read_command(args) or ""
    root = args.project.resolve()
    if not root.is_dir():
        print(f"sqlx-config: {args.project} is not a folder", file=sys.stderr)
        return EXIT_USAGE
    try:
        command = parse_command(text, show=True)
    except CommandError as exc:
        _print_command_error(text, exc)
        return EXIT_USAGE
    rows, read, problems = show_project(root, command, args.files)
    if args.json:
        print(json.dumps({"files_read": read, "rows": [row.__dict__ for row in rows], "not_read": problems},
                         indent=2, ensure_ascii=False))
        return EXIT_OK
    limit = args.limit if args.limit > 0 else len(rows)
    for row in rows[:limit]:
        if row.status != "matched":
            print(f"{row.path}  {row.status}: {row.reason}")
            continue
        print(f"{row.path}  " + "  ".join(f"{name}={value}" for name, value in row.values))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more (--limit 0 prints all)")
    unknown = sum(1 for row in rows if row.status != "matched")
    print(f"{len(rows) - unknown} of {read} files selected" + (f", {unknown} undecided or unreadable" if unknown else "")
          + "".join(f"\nnot read: {problem}" for problem in problems))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.action == "show":
        return _show_main(args)
    if args.action == "edit":
        return _edit_main(args)
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
