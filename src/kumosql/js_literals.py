"""Read plain data out of Dataform JavaScript without running it.

Dataform projects often keep their source tables as a literal list in ``includes/`` and ``require`` it from a
file that calls ``declare`` in a loop. This reads such literals (strings, arrays, objects) and the exports of
each module, and says nothing when anything is computed.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .js_tokens import strip_comments as _strip_comments

_MISSING = object()
_STRING_RE = re.compile(r"""'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)"|`((?:\\.|[^`\\$])*)`""", re.S)
_KEY_RE = re.compile(r"""([A-Za-z_$][\w$]*)|'([^']*)'|"([^"]*)" """.strip())


def strip_comments(text: str) -> str:
    """``text`` without ``//`` and ``/* */`` comments (string contents are left alone).

    The shared tokenizer of :mod:`kumosql.js_tokens`: a template literal holding ``${...}`` is kept whole too.
    """

    return _strip_comments(text)


def parse_literal(text: str, index: int = 0):
    """Parse one JS literal at ``index``: returns ``(value, end)`` or ``(_MISSING, index)``.

    Values are ``str``, ``list`` and ``dict``; numbers and booleans become ``None`` placeholders that keep
    their place in a list.
    """

    length = len(text)

    def skip(i: int) -> int:
        while i < length and text[i].isspace():
            i += 1
        return i

    index = skip(index)
    if index >= length:
        return _MISSING, index
    char = text[index]
    if char in "'\"`":
        match = _STRING_RE.match(text, index)
        if not match:
            return _MISSING, index
        value = next(group for group in match.groups() if group is not None)
        return re.sub(r"\\(.)", r"\1", value), match.end()
    if char == "[":
        items, i = [], skip(index + 1)
        while i < length and text[i] != "]":
            value, i = parse_literal(text, i)
            if value is _MISSING:
                return _MISSING, index
            items.append(value)
            i = skip(i)
            if i < length and text[i] == ",":
                i = skip(i + 1)
            elif i < length and text[i] != "]":
                return _MISSING, index
        return (items, i + 1) if i < length else (_MISSING, index)
    if char == "{":
        result, i = {}, skip(index + 1)
        while i < length and text[i] != "}":
            key = _KEY_RE.match(text, i)
            if not key:
                return _MISSING, index
            i = skip(key.end())
            if i >= length or text[i] != ":":
                return _MISSING, index
            value, i = parse_literal(text, i + 1)
            if value is _MISSING:
                return _MISSING, index
            result[next(group for group in key.groups() if group is not None)] = value
            i = skip(i)
            if i < length and text[i] == ",":
                i = skip(i + 1)
            elif i < length and text[i] != "}":
                return _MISSING, index
        return (result, i + 1) if i < length else (_MISSING, index)
    number = re.match(r"-?\d[\d_.]*|true\b|false\b|null\b", text[index:])
    if number:
        return None, index + number.end()
    return _MISSING, index


def parse_complete(text: str, index: int = 0):
    """Like :func:`parse_literal`, but the literal must end its expression (``[...].map(f)`` is not a literal)."""

    value, end = parse_literal(text, index)
    if value is _MISSING:
        return value, end
    rest = text[end:end + 40].lstrip(" \t")
    if rest and rest[0] not in ";\n\r,})":
        return _MISSING, index
    return value, end


def _literal_after(text: str, pattern: str):
    value = _MISSING
    count = 0
    for match in re.finditer(pattern, text):
        parsed, _end = parse_literal(text, match.end())
        count += 1
        value = parsed
    return value if count == 1 else _MISSING  # defined twice: not knowable


def module_exports(text: str) -> dict[str, object]:
    """What a module offers a ``require``: ``{"": whole export, "name": member}`` for literals only.

    Understands ``module.exports = <literal>``, ``module.exports = { a, b: <literal> }`` with ``a`` a literal
    ``const``, and ``exports.name = <literal>``.
    """

    text = strip_comments(text)
    constants: dict[str, object] = {}
    for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*", text):
        value, _ = parse_complete(text, match.end())
        if value is not _MISSING:
            constants[match.group(1)] = _MISSING if match.group(1) in constants else value
    exports: dict[str, object] = {}
    whole = re.search(r"\bmodule\.exports\s*=\s*", text)
    if whole:
        value, _ = parse_complete(text, whole.end())
        if value is not _MISSING:
            exports[""] = value
            if isinstance(value, dict):
                exports.update(value)
        else:
            brace = re.match(r"\{", text[whole.end():].lstrip())
            if brace:
                start = whole.end() + len(text[whole.end():]) - len(text[whole.end():].lstrip())
                body = _shorthand_members(text, start)
                for name, key in body:
                    if key in constants and constants[key] is not _MISSING:
                        exports[name] = constants[key]
    for match in re.finditer(r"\bexports\.([A-Za-z_$][\w$]*)\s*=\s*", text):
        value, _ = parse_complete(text, match.end())
        if value is not _MISSING:
            exports[match.group(1)] = value
        else:
            ident = re.match(r"([A-Za-z_$][\w$]*)\s*[;\n]", text[match.end():])
            if ident and constants.get(ident.group(1), _MISSING) is not _MISSING:
                exports[match.group(1)] = constants[ident.group(1)]
    return exports


def _shorthand_members(text: str, start: int) -> list[tuple[str, str]]:
    """``{ a, b: c }`` after ``module.exports =``: ``[(exported name, local name)]``."""

    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    members = []
    for part in text[start + 1:i].split(","):
        part = part.strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", part):
            members.append((part, part))
        elif re.fullmatch(r"[A-Za-z_$][\w$]*\s*:\s*[A-Za-z_$][\w$]*", part):
            name, local = (side.strip() for side in part.split(":"))
            members.append((name, local))
    return members


def resolve_module(spec: str, from_file: str, modules: dict[str, dict[str, object]]) -> dict[str, object] | None:
    """The exports of ``require(spec)`` from the file ``from_file`` (project-relative), or None."""

    base = PurePosixPath(from_file).parent
    candidates = []
    if spec.startswith("."):
        candidates.append(base / spec)
    else:
        candidates += [PurePosixPath(spec), base / spec]
    for candidate in candidates:
        parts: list[str] = []
        for part in candidate.parts:
            if part == "..":
                if parts:
                    parts.pop()
            elif part != ".":
                parts.append(part)
        key = "/".join(parts)
        key = key[:-3] if key.endswith(".js") else key
        for option in (key, key + "/index"):
            if option in modules:
                return modules[option]
    return None


def required_bindings(text: str, from_file: str, modules: dict[str, dict[str, object]]) -> dict[str, object]:
    """Names this file binds from ``require``: ``const x = require("m")`` and ``const { a, b } = require("m")``."""

    text = strip_comments(text)
    bound: dict[str, object] = {}
    seen: dict[str, int] = {}
    for match in re.finditer(r"\b(?:const|let|var)\s+(?:(\w+)|\{([^}]*)\})\s*=\s*require\(\s*(['\"`])([^'\"`]+)\3\s*\)(\.\w+)?", text):
        exported = resolve_module(match.group(4), from_file, modules)
        if exported is None:
            continue
        member = match.group(5)[1:] if match.group(5) else ""
        if match.group(1):
            if member:
                value = exported.get(member, _MISSING)
            else:  # the whole module: its default export, else an object of its named exports
                value = exported[""] if "" in exported else {k: v for k, v in exported.items() if k}
            names = {match.group(1): value}
        else:
            names = {}
            for item in match.group(2).split(","):
                pair = [side.strip() for side in item.split(":")]
                if pair and pair[0]:
                    names[pair[-1]] = exported.get(pair[0], _MISSING)
        for name, value in names.items():
            seen[name] = seen.get(name, 0) + 1
            if value is not _MISSING:
                bound[name] = value
    return {name: value for name, value in bound.items() if seen[name] == 1}


def local_constants(text: str) -> dict[str, object]:
    """Literal ``const`` values defined once in this file."""

    text = strip_comments(text)
    found: dict[str, object] = {}
    counts: dict[str, int] = {}
    for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*", text):
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        value, _ = parse_complete(text, match.end())
        if value is not _MISSING:
            found[match.group(1)] = value
    return {name: value for name, value in found.items() if counts[name] == 1}


def source_items(expression: str, scope: dict[str, object]):
    """The items a loop ranges over when ``expression`` is a literal, a name in ``scope`` or ``name.member``."""

    expression = expression.strip()
    value, end = parse_literal(expression, 0)
    if value is _MISSING:
        parts = expression.split(".")
        if not all(re.fullmatch(r"[A-Za-z_$][\w$]*", part) for part in parts) or parts[0] not in scope:
            return _MISSING
        value = scope[parts[0]]
        for member in parts[1:]:
            value = value.get(member, _MISSING) if isinstance(value, dict) else _MISSING
    elif expression[end:].strip():
        return _MISSING
    if isinstance(value, dict):
        return _MISSING
    return value if isinstance(value, list) else _MISSING


def substitute(config: str, variable: str, item) -> str | None:
    """``config`` with the loop variable replaced by this item's literals; None if a member is missing."""

    def quote(value: object) -> str | None:
        return None if not isinstance(value, str) else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    failed = False

    def member(match: re.Match[str]) -> str:
        nonlocal failed
        key = match.group(1) or match.group(2) or match.group(3)
        if isinstance(item, dict) and key not in item:
            return "undefined"
        quoted = quote(item.get(key)) if isinstance(item, dict) else None
        if quoted is None:
            failed = True
            return match.group(0)
        return quoted

    out = re.sub(rf"\b{re.escape(variable)}(?:\.([A-Za-z_$][\w$]*)|\[\s*'([^']*)'\s*\]|\[\s*\"([^\"]*)\"\s*\])", member, config)
    if isinstance(item, str):
        quoted_item = quote(item)
        out = re.sub(rf"(?<=[{{,])(\s*)({re.escape(variable)})(\s*)(?=[,}}])", lambda m: f"{m.group(1)}{variable}: {quoted_item}{m.group(3)}", out)
        out = re.sub(rf"(?<![\w$.\"'])({re.escape(variable)})(?![\w$])(?!\s*:)", lambda m: quoted_item or m.group(0), out)
    if failed or re.search(rf"(?<![\w$.\"'])\b{re.escape(variable)}\b(?!\s*:)", out):
        return None
    return out
