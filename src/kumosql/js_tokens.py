"""JavaScript tokens without running JavaScript: the one tokenizer for Dataform config blocks and ``includes/`` modules.

:func:`tokens` yields every token with its source span (whitespace and comments included), which is what an edit that
splices text needs (:mod:`kumosql.sqlx_config`). :func:`strip_comments` blanks comments and keeps strings and template
literals whole; the loader (``pipeline_loading``) and :mod:`kumosql.js_literals` read config text and modules through it.
Template literals nest as JavaScript nests them: ``${...}`` is code, so a brace, backtick or comment marker inside one
does not end the literal.
"""

from __future__ import annotations

import re
from typing import Iterator, NamedTuple

__all__ = ["Token", "tokens", "strip_comments"]

class Token(NamedTuple):
    """One JavaScript token: ``kind`` is space, comment, string, template, number, name, spread, punct or op."""

    kind: str
    start: int
    end: int
    computed: bool = False  # a template literal with ${...}
    unterminated: bool = False  # a string, template literal or block comment that never ends


_COMMENT = r"//[^\n]*|/\*.*?\*/|/\*.*"
_STRING = r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""
_TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    rf"|(?P<comment>{_COMMENT})"
    rf"|(?P<string>{_STRING})"
    r"|(?P<open>['\"`])"
    r"|(?P<number>(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?)"
    r"|(?P<name>[^\W\d][\w$]*|\$[\w$]*)"
    r"|(?P<spread>\.\.\.)"
    r"|(?P<punct>[{}\[\](),:;?.])"
    r"|(?P<op>.)",
    re.S,
)
# Code with no string, comment, template or brace in it, read in one step by the fast scanner below.
_PLAIN_RE = re.compile(r"[^'\"`/{}]+|/(?![/*])")
_COMMENT_RE = re.compile(_COMMENT, re.S)
_STRING_RE = re.compile(_STRING, re.S)


def tokens(text: str, start: int = 0) -> Iterator[Token]:
    """The JavaScript tokens of ``text`` from ``start``, whitespace and comments included.

    Lenient: a string or comment that never ends runs to the end of the text and is marked ``unterminated``; a
    template literal is one token, and ``computed`` when it holds ``${...}`` (nested templates and braces included).
    """

    index, length = start, len(text)
    while index < length:
        match = _TOKEN_RE.match(text, index)
        kind, end = match.lastgroup, match.end()
        if kind == "open":
            if text[index] == "`":
                end, computed, closed = _template_end(text, index)
                yield Token("template", index, end, computed=computed, unterminated=not closed)
            else:
                end = length
                yield Token("string", index, end, unterminated=True)
        elif kind == "comment":
            closed = not text.startswith("/*", index) or (end - index >= 4 and text.endswith("*/", 0, end))
            yield Token("comment", index, end, unterminated=not closed)
        else:
            yield Token(kind, index, end)
        index = end


def _spans(text: str, start: int = 0) -> Iterator[tuple[str, int, int]]:
    """``(kind, start, end)`` of each comment, string, template literal and brace from ``start``; plain code skipped."""

    index, length = start, len(text)
    while index < length:
        plain = _PLAIN_RE.match(text, index)
        if plain:
            index = plain.end()
            continue
        char = text[index]
        if char in "{}":
            yield char, index, index + 1
            index += 1
        elif char == "/":
            end = _COMMENT_RE.match(text, index).end()
            yield "comment", index, end
            index = end
        elif char == "`":
            end = _template_end(text, index)[0]
            yield "template", index, end
            index = end
        else:
            match = _STRING_RE.match(text, index)
            end = match.end() if match else length
            yield "string", index, end
            index = end


def _template_end(text: str, start: int) -> tuple[int, bool, bool]:
    """``(end, has ${...}, closed)`` of the template literal whose backtick is at ``start``."""

    index, length, computed = start + 1, len(text), False
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
        elif char == "`":
            return index + 1, computed, True
        elif text.startswith("${", index):
            computed = True
            index = _code_end(text, index + 2)
        else:
            index += 1
    return length, computed, False


def _code_end(text: str, start: int) -> int:
    """The offset just after the ``}`` that closes the code starting at ``start`` (the inside of a ``${``)."""

    depth = 0
    for kind, _start, end in _spans(text, start):
        if kind == "{":
            depth += 1
        elif kind == "}":
            if depth == 0:
                return end
            depth -= 1
    return len(text)


def strip_comments(text: str) -> str:
    """``text`` with each ``//`` and ``/* */`` comment outside a string replaced by one space; strings and template
    literals (with what their ``${...}`` holds) are kept whole. The loader reads config blocks through this."""

    pieces, cursor = [], 0
    for kind, start, end in _spans(text):
        if kind == "comment":
            pieces.append(text[cursor:start])
            pieces.append(" ")
            cursor = end
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)
