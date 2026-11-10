"""Exact decoding of GoogleSQL string literals, before sqlglot reads the query.

sqlglot's BigQuery reader keeps most backslash escapes of a string literal undecoded (``"\\x41"`` stays four
characters), so the evaluator rewrites the source text first: every string literal (plain, triple-quoted or raw)
becomes a single-quoted literal holding the decoded value, spelt with only the escapes ``\\\\ \\' \\n \\r \\t``, which
sqlglot reads exactly. Bytes literals are rewritten by :mod:`kumosql.string_literals`.

GoogleSQL string escapes: ``\\a \\b \\f \\n \\r \\t \\v \\\\ \\? \\" \\' \\```, ``\\ooo`` (three octal digits), ``\\xhh``
(two hex digits), ``\\uhhhh`` and ``\\Uhhhhhhhh``. In a STRING the octal and ``\\x`` escapes name the Unicode code point
of that number (``"\\xE2"`` is one character, U+00E2), not a byte. Any other escape is an error, as is a code
point that is a surrogate or beyond U+10FFFF.
"""

from __future__ import annotations

from kumo_sql_prover.string_literals import _bytes_literal, _decode_bytes, _invalid, _string_end
from .errors import AnalysisError, Unsupported

_SIMPLE = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "?": "?", '"': '"',
           "'": "'", "`": "`"}
_OUT = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_HEX = "0123456789abcdefABCDEF"


def _code_point(number: int) -> str:
    if 0xD800 <= number <= 0xDFFF or number > 0x10FFFF:
        raise AnalysisError("Illegal escape sequence: Unicode code point out of range")
    return chr(number)


def decode_string(body: str) -> str:
    """The value of a string literal body with escapes (raises :class:`AnalysisError` for an illegal escape)."""

    out = []
    i, size = 0, len(body)
    while i < size:
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        code = body[i + 1 : i + 2]
        if code in _SIMPLE and code != "":
            out.append(_SIMPLE[code])
            i += 2
        elif code in ("x", "X"):
            digits = body[i + 2 : i + 4]
            if len(digits) != 2 or any(d not in _HEX for d in digits):
                raise AnalysisError("Illegal escape sequence: \\x needs two hex digits")
            out.append(chr(int(digits, 16)))
            i += 4
        elif code in ("u", "U"):
            width = 4 if code == "u" else 8
            digits = body[i + 2 : i + 2 + width]
            if len(digits) != width or any(d not in _HEX for d in digits):
                raise AnalysisError(f"Illegal escape sequence: \\{code} needs {width} hex digits")
            out.append(_code_point(int(digits, 16)))
            i += 2 + width
        elif code != "" and code in "0123":
            digits = body[i + 1 : i + 4]
            if len(digits) != 3 or any(d not in "01234567" for d in digits):
                raise AnalysisError("Illegal escape sequence: octal escapes have three digits")
            out.append(chr(int(digits, 8)))
            i += 4
        elif code in "\r\n" and code != "":
            raise Unsupported("backslash before a line break in a string literal")
        else:
            raise AnalysisError(f"Illegal escape sequence: \\{code}")
    return "".join(out)


def _spell(value: str) -> str:
    return "'" + "".join(_OUT.get(c, c) for c in value) + "'"


def decode_literals(sql: str) -> str:
    """``sql`` with every string literal replaced by a single-quoted literal of the same value (see the module docstring)."""

    if "\\" not in sql and "``" not in sql:
        return sql
    out = []
    i, size = 0, len(sql)
    while i < size:
        char = sql[i]
        if sql.startswith("--", i) or char == "#":
            end = sql.find("\n", i)
            end = size if end < 0 else end
            out.append(sql[i:end])
            i = end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = size if end < 0 else end + 2
            out.append(sql[i:end])
            i = end
        elif char == "`":
            j = i + 1
            while j < size and sql[j] != "`":
                j += 2 if sql[j] == "\\" else 1
            j = min(j + 1, size)
            out.append(sql[i:j])
            if sql.startswith("`", j):
                out.append(" ")  # `a``b` is two names, not one name containing a backtick
            i = j
        elif char.isalnum() or char == "_":
            j = i
            while j < size and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            prefix = word.lower()
            if prefix in ("r", "b", "rb", "br") and j < size and sql[j] in "'\"":
                end, quote, body = _string_end(sql, j)
                if _invalid(quote, body):
                    out.append(sql[i:end])
                elif prefix == "r":
                    out.append(_spell(body))
                else:
                    value = _decode_bytes(body, raw=len(prefix) == 2)
                    out.append(sql[i:end] if value is None else _bytes_literal(value))
                i = end
            else:
                out.append(word)
                i = j
        elif char in "'\"":
            end, quote, body = _string_end(sql, i)
            out.append(sql[i:end] if _invalid(quote, body) else _spell(decode_string(body)))
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)
