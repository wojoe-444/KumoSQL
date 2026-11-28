"""Tableau field references: ``[Sales]``, ``[federated.0a1].[sum:Sales:qk]``, ``[Parameters].[Parameter 1]``.

A name is written in square brackets with ``]`` doubled inside it. A reference is one bracketed name, or two joined by a
dot (data source, then field or field instance). In a formula, brackets inside string literals and comments are not
references, so :func:`formula_refs` skips them.
"""

from __future__ import annotations

import re
from typing import Iterator, NamedTuple

__all__ = ["Ref", "bracket", "split_ref", "formula_refs", "qualified_refs", "instance_field"]


class Ref(NamedTuple):
    start: int  # offset of the first "[" in the text it was found in
    end: int  # offset just after the last "]"
    parts: tuple[str, ...]  # the unescaped names, one or two


def bracket(name: str) -> str:
    """``Sales [USD]`` -> ``[Sales [USD]]]``."""

    return "[" + name.replace("]", "]]") + "]"


_NAME = re.compile(r"\[(?!\[)((?:[^\]]|\]\])*)\]")


def _read_name(text: str, i: int) -> tuple[str, int] | None:
    """Read ``[name]`` starting at ``text[i] == "["``; return the name and the offset after it, or ``None``."""

    match = _NAME.match(text, i)
    if match is None:
        return None
    return match.group(1).replace("]]", "]"), match.end()


def _read_ref(text: str, i: int) -> Ref | None:
    first = _read_name(text, i)
    if first is None:
        return None
    name, j = first
    if text.startswith(".[", j):
        second = _read_name(text, j + 1)
        if second is not None:
            return Ref(i, second[1], (name, second[0]))
    return Ref(i, j, (name,))


def split_ref(text: str) -> tuple[str, ...] | None:
    """``"[a].[b]"`` -> ``("a", "b")``; ``"[a]"`` -> ``("a",)``; ``None`` unless the whole text is one reference."""

    text = text.strip()
    if not text.startswith("["):
        return None
    ref = _read_ref(text, 0)
    if ref is None or ref.end != len(text):
        return None
    return ref.parts


def formula_refs(formula: str) -> Iterator[Ref]:
    """The bracketed references of a Tableau formula, outside string literals and comments."""

    i = 0
    n = len(formula)
    while i < n:
        ch = formula[i]
        if ch in "\"'":
            j = i + 1
            while j < n:
                if formula[j] == ch:
                    if formula.startswith(ch * 2, j):
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if formula.startswith("//", i):
            newline = formula.find("\n", i)
            i = n if newline < 0 else newline + 1
            continue
        if formula.startswith("/*", i):
            close = formula.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch == "[":
            ref = _read_ref(formula, i)
            if ref is not None:
                yield ref
                i = ref.end
                continue
        i += 1


def qualified_refs(text: str) -> Iterator[Ref]:
    """Every ``[a].[b]`` in free text (shelves, attribute values, tooltip runs); single names are not reported."""

    i = text.find("[")
    while 0 <= i < len(text):
        ref = _read_ref(text, i)
        if ref is not None and len(ref.parts) == 2:
            yield ref
            i = text.find("[", ref.end)
        else:
            i = text.find("[", i + 1)


# A field instance is the field with how a sheet uses it: "sum:Sales:qk" (summed, quantitative), "none:Region:nk",
# "usr:Calculation_1:qk" (a calculated field as written), "pcto:sum:Sales:qk" (a table calculation over a sum),
# "yr:Order Date:ok". Instance names are looked up in the sheet's <column-instance> elements first; this pattern is the
# fallback for an instance the sheet does not declare.
_INSTANCE = re.compile(r"((?:[a-z][a-z0-9]*:)+)(.+?):([a-z]{2})(?::\d+)?")


def instance_field(name: str) -> str:
    """The field an instance name refers to: ``sum:Sales:qk`` -> ``Sales``; a plain field name is returned as it is."""

    match = _INSTANCE.fullmatch(name)
    return match.group(2) if match else name
