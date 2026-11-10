"""BigQuery scripts: break them into statements and follow what flows between them.

A script is several statements run together, with variables, temporary tables,
control flow and stored procedures between them. Everything that reads SQL
(job history, Dataform operations, procedure bodies, pasted queries) goes
through this module so a script is understood the same way everywhere.

Reading happens in three steps:

1. :func:`split_script` cuts the text into statements with a small lexer that
   knows strings (raw, triple-quoted), comments, quoted names and nested
   ``BEGIN ... END``, ``IF``, ``LOOP``, ``WHILE``, ``FOR``, ``REPEAT`` and
   ``CASE`` blocks. Semicolons inside any of those never split. The lexer does
   not depend on sqlglot, whose tokenizer treats ``BEGIN`` as a command and
   swallows the rest.
2. Each statement is *kept* (it reads or writes tables: queries,
   ``CREATE TABLE/VIEW ... AS``, ``INSERT``, ``MERGE``, ``UPDATE``, ``DELETE``, table DDL,
   ``CALL`` of a known procedure), *ignored* (``DECLARE``/``SET`` of scalars,
   ``ASSERT``, transactions, ``LOAD DATA``, definitions outside tables, control-flow shells) or
   *unknown* (it might read tables and could not be understood: dynamic
   ``EXECUTE IMMEDIATE``, an unknown procedure, text that does not parse).
   Unknown is never guessed.
3. Kept statements are followed in order. A temporary table remembers what it
   was built from, so a table written from a temporary table traces to the real
   sources; a script variable remembers the tables its value came from; a
   statement inside an ``IF``, loop or exception handler is a *possible* edge.
   A temporary table that one query defines is also spliced into the final query
   as a common table expression so column lineage reaches the real sources.

Nothing here echoes SQL text; :meth:`ScriptAnalysis.report` carries counts,
kinds and line numbers only.
"""

from __future__ import annotations

import bisect
import itertools
import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, NamedTuple

import sqlglot
from sqlglot import ErrorLevel, exp

from kumo_sql_prover.ast_utils import binding_cte, quiet_parser, set_with_clause, with_clause

MAX_PROCEDURE_DEPTH = 8
MAX_NESTED_SQL_DEPTH = 3
MAX_LOOP_PASSES = 12  # trial runs of a loop body before its variables are called unsettled
MAX_LOOP_NESTING = 4
KEPT, IGNORED, UNKNOWN = "kept", "ignored", "unknown"

# ----------------------------------------------------------------------------- lexer


class Tok(NamedTuple):
    kind: str  # w word, s string, q quoted name, n number, p punctuation, ; semicolon
    text: str
    start: int
    end: int

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == "w" else ""


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"\d[\w.]*")
_STRING_PREFIXES = {"r", "b", "rb", "br"}


def lex(text: str) -> list[Tok]:
    """Tokens of BigQuery SQL with comments dropped. Unterminated strings run to the end of the text."""

    tokens: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "#" or text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            end = _string_end(text, i, raw=False)
            tokens.append(Tok("s", text[i:end], i, end))
            i = end
        elif c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            end = min(j + 1, n)
            tokens.append(Tok("q", text[i:end], i, end))
            i = end
        elif c == ";":
            tokens.append(Tok(";", c, i, i + 1))
            i += 1
        elif c.isalpha() or c == "_":
            m = _WORD.match(text, i)
            end = m.end()
            if text[i:end].lower() in _STRING_PREFIXES and end < n and text[end] in "'\"":
                stop = _string_end(text, end, raw=True)
                tokens.append(Tok("s", text[i:stop], i, stop))
                i = stop
            else:
                tokens.append(Tok("w", text[i:end], i, end))
                i = end
        elif c.isdigit():
            m = _NUMBER.match(text, i)
            tokens.append(Tok("n", m.group(), i, m.end()))
            i = m.end()
        else:
            tokens.append(Tok("p", c, i, i + 1))
            i += 1
    return tokens


def _string_end(text: str, start: int, *, raw: bool) -> int:
    quote = text[start]
    triple = text.startswith(quote * 3, start)
    closer = quote * 3 if triple else quote
    i = start + len(closer)
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
        elif text.startswith(closer, i):
            return i + len(closer)
        elif not triple and text[i] == "\n":
            return i  # an unterminated one-line string stops at the line end
        else:
            i += 1
    return n


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "`": "`", "?": "?"}


def string_value(token: str) -> str | None:
    """The value of a string literal token, or ``None`` for bytes or an escape it does not decode."""

    prefix = ""
    body = token
    while body and body[0] not in "'\"":
        prefix += body[0]
        body = body[1:]
    if "b" in prefix.lower():
        return None
    triple = body[:3] in ("'''", '"""')
    inner = body[3:-3] if triple else body[1:-1]
    if "r" in prefix.lower() or "\\" not in inner:
        return inner
    out = []
    i = 0
    while i < len(inner):
        if inner[i] == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt not in _ESCAPES:
                return None
            out.append(_ESCAPES[nxt])
            i += 2
        else:
            out.append(inner[i])
            i += 1
    return "".join(out)


# ------------------------------------------------------------------------ statement tree


@dataclass
class Node:
    """One statement or control structure of a script."""

    kind: str  # stmt, begin, if, while, loop, for, repeat, case, procedure
    start: int
    end: int
    header: str = ""  # a statement's text; a control structure's condition text
    branches: list[list["Node"]] = field(default_factory=list)
    name: str = ""  # a procedure's name, a FOR loop's variable
    query: str = ""  # a FOR loop's query
    params: str = ""  # a procedure's parameter list
    conditions: list[str] = field(default_factory=list)  # every condition that picks a path: IF/ELSEIF, CASE and WHEN, WHILE, UNTIL


_CONTROL_STARTS = {"IF", "WHILE", "LOOP", "FOR", "REPEAT", "CASE"}
_BLOCK_ENDS = {"IF", "LOOP", "WHILE", "FOR", "REPEAT", "CASE"}


class _Splitter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks = lex(text)
        self.i = 0
        self.n = len(self.toks)

    # -- helpers
    def peek(self, offset: int = 0) -> Tok | None:
        j = self.i + offset
        return self.toks[j] if j < self.n else None

    def word(self, offset: int = 0) -> str:
        t = self.peek(offset)
        return t.up if t is not None else ""

    def skip_to_semicolon(self) -> None:
        while self.i < self.n and self.toks[self.i].kind != ";":
            self.i += 1
        if self.i < self.n:
            self.i += 1

    def scan_until(self, stop: set[str], *, then: str = "") -> tuple[int, int]:
        """Token range up to a keyword in ``stop`` at parenthesis and CASE depth zero; leaves ``i`` on it."""

        start = self.i
        depth = case = 0
        while self.i < self.n:
            t = self.toks[self.i]
            if t.kind == "p" and t.text == "(":
                depth += 1
            elif t.kind == "p" and t.text == ")":
                depth = max(depth - 1, 0)
            elif t.kind == "w":
                if t.up == "CASE":
                    case += 1
                elif t.up == "END" and case:
                    case -= 1
                elif depth == 0 and case == 0 and t.up in stop:
                    break
            elif t.kind == ";" and depth == 0:
                break
            self.i += 1
        return start, self.i

    def text_of(self, a: int, b: int) -> str:
        if a >= b or a >= self.n:
            return ""
        return self.text[self.toks[a].start : self.toks[b - 1].end]

    # -- grammar
    def parse(self) -> list[Node]:
        return self.block(set())

    def block(self, terminators: set[str]) -> list[Node]:
        nodes: list[Node] = []
        while self.i < self.n:
            t = self.toks[self.i]
            if t.kind == ";":
                self.i += 1
                continue
            if t.kind == "w" and t.up in terminators:
                break
            node = self.statement(bool(terminators))
            if node is not None:
                nodes.append(node)
        return nodes

    def statement(self, inside_block: bool) -> Node | None:
        # an optional ``label:`` before a block
        if self.peek(1) is not None and self.toks[self.i].kind == "w" and self.peek(1).text == ":" and self.word(2) in {"BEGIN", "LOOP", "WHILE", "FOR", "REPEAT"}:
            self.i += 2
        first = self.word()
        begin = self.toks[self.i].start
        if first == "BEGIN" and self.word(1) not in {"TRANSACTION", ""} and self.peek(1).kind != ";":
            return self.begin_block(begin)
        if first == "IF" and self.peek(1) is not None and self.peek(1).text != "(" or first == "IF" and self.control_if_call_is_statement():
            return self.if_block(begin)
        if first == "WHILE":
            return self.simple_loop(begin, "while", {"DO"})
        if first == "LOOP":
            return self.simple_loop(begin, "loop", set())
        if first == "REPEAT":
            return self.repeat_block(begin)
        if first == "FOR" and self.word(2) == "IN":
            return self.for_block(begin)
        if first == "CASE":
            return self.case_block(begin)
        if first == "CREATE":
            node = self.maybe_procedure(begin)
            if node is not None:
                return node
        return self.simple(begin, inside_block)

    def control_if_call_is_statement(self) -> bool:
        # ``IF(cond, a, b)`` opens no statement of its own; an IF statement is ``IF cond THEN``
        j = self.i
        depth = 0
        while j < self.n:
            t = self.toks[j]
            if t.kind == "p" and t.text == "(":
                depth += 1
            elif t.kind == "p" and t.text == ")":
                depth -= 1
            elif t.kind == ";":
                return False
            elif t.kind == "w" and t.up == "THEN" and depth == 0:
                return True
            j += 1
        return False

    def end_statement(self, kind: str) -> None:
        """Consume ``END`` plus its optional keyword and label up to the semicolon."""

        if self.word() == "END":
            self.i += 1
        self.skip_to_semicolon()

    def begin_block(self, begin: int) -> Node:
        self.i += 1
        body = self.block({"END", "EXCEPTION"})
        node = Node("begin", begin, begin, branches=[body])
        if self.word() == "EXCEPTION":
            self.i += 1
            if self.word() == "WHEN":
                self.scan_until({"THEN"})
                self.i += 1
            node.branches.append(self.block({"END"}))
        self.end_statement("begin")
        node.end = self.toks[self.i - 1].end if self.i else begin
        return node

    def if_block(self, begin: int) -> Node:
        self.i += 1
        a, b = self.scan_until({"THEN"})
        node = Node("if", begin, begin, header=self.text_of(a, b))
        node.conditions.append(node.header)
        self.i += 1  # THEN
        node.branches.append(self.block({"ELSEIF", "ELSE", "END"}))
        while self.word() in {"ELSEIF", "ELSE"}:
            if self.word() == "ELSEIF":
                self.i += 1
                node.conditions.append(self.text_of(*self.scan_until({"THEN"})))
            self.i += 1
            node.branches.append(self.block({"ELSEIF", "ELSE", "END"}))
        self.end_statement("if")
        node.end = self.toks[self.i - 1].end
        return node

    def simple_loop(self, begin: int, kind: str, header_stop: set[str]) -> Node:
        self.i += 1
        node = Node(kind, begin, begin)
        if header_stop:
            a, b = self.scan_until(header_stop)
            node.header = self.text_of(a, b)
            node.conditions.append(node.header)
            self.i += 1
        node.branches.append(self.block({"END"}))
        self.end_statement(kind)
        node.end = self.toks[self.i - 1].end
        return node

    def repeat_block(self, begin: int) -> Node:
        self.i += 1
        node = Node("repeat", begin, begin)
        node.branches.append(self.block({"UNTIL", "END"}))
        if self.word() == "UNTIL":
            self.i += 1
            a, b = self.scan_until({"END"})
            node.header = self.text_of(a, b)
            node.conditions.append(node.header)
        self.end_statement("repeat")
        node.end = self.toks[self.i - 1].end
        return node

    def for_block(self, begin: int) -> Node:
        variable = self.toks[self.i + 1].text
        self.i += 3  # FOR var IN
        a, b = self.scan_until({"DO"})
        query = self.text_of(a, b).strip()
        if query.startswith("(") and query.endswith(")"):
            query = query[1:-1]
        node = Node("for", begin, begin, name=variable, query=query)
        self.i += 1
        node.branches.append(self.block({"END"}))
        self.end_statement("for")
        node.end = self.toks[self.i - 1].end
        return node

    def case_block(self, begin: int) -> Node:
        self.i += 1
        node = Node("case", begin, begin, header=self.text_of(*self.scan_until({"WHEN"})))
        if node.header.strip():
            node.conditions.append(node.header)  # ``CASE expr WHEN ...``: the value every WHEN is compared with
        while self.word() in {"WHEN", "ELSE"}:
            if self.word() == "WHEN":
                self.i += 1
                node.conditions.append(self.text_of(*self.scan_until({"THEN"})))
            self.i += 1
            node.branches.append(self.block({"WHEN", "ELSE", "END"}))
        self.end_statement("case")
        node.end = self.toks[self.i - 1].end
        return node

    def maybe_procedure(self, begin: int) -> Node | None:
        j = self.i + 1
        while j < self.n and self.toks[j].up in {"OR", "REPLACE", "TEMP", "TEMPORARY"}:
            j += 1
        if not (j < self.n and self.toks[j].up == "PROCEDURE"):
            return None
        j += 1
        if self.word(j - self.i) == "IF":  # IF NOT EXISTS
            j += 3
        name_parts = []
        while j < self.n and self.toks[j].kind in {"w", "q"} or (j < self.n and self.toks[j].text == "."):
            name_parts.append(self.toks[j].text)
            j += 1
        # parameter list, then OPTIONS(...) / LANGUAGE ..., then BEGIN
        k = j
        depth = 0
        params_start = j
        while k < self.n:
            t = self.toks[k]
            if t.kind == "p" and t.text == "(":
                depth += 1
            elif t.kind == "p" and t.text == ")":
                depth -= 1
            elif t.kind == ";" and depth == 0:
                return None  # no body here (a language other than SQL)
            elif t.kind == "w" and t.up == "BEGIN" and depth == 0:
                break
            k += 1
        else:
            return None
        node = Node(
            "procedure",
            begin,
            begin,
            name="".join(name_parts).replace("`", ""),
            params=self.text_of(params_start, k),
        )
        self.i = k + 1
        node.branches.append(self.block({"END", "EXCEPTION"}))
        if self.word() == "EXCEPTION":
            self.i += 1
            if self.word() == "WHEN":
                self.scan_until({"THEN"})
                self.i += 1
            node.branches.append(self.block({"END"}))
        self.end_statement("procedure")
        node.end = self.toks[self.i - 1].end
        return node

    def simple(self, begin: int, inside_block: bool) -> Node | None:
        start = self.i
        depth = case = 0
        while self.i < self.n:
            t = self.toks[self.i]
            if t.kind == ";":
                break
            if t.kind == "p":
                depth += t.text == "("
                depth -= t.text == ")" and depth > 0
            elif t.kind == "w":
                if t.up == "CASE":
                    case += 1
                elif t.up == "END":
                    if case:
                        case -= 1
                    elif inside_block and depth == 0:
                        break  # the block's END closes a last statement written without its semicolon
            self.i += 1
        stop = self.i
        if self.i < self.n and self.toks[self.i].kind == ";":
            self.i += 1
        if stop == start:
            if self.i == start:  # a stray END or keyword at the top level: step over it
                self.i += 1
            return None
        return Node("stmt", self.toks[start].start, self.toks[stop - 1].end, header=self.text_of(start, stop))


def parse_script(text: str) -> list[Node]:
    return _Splitter(text).parse()


def column_words(text: str) -> frozenset[str] | None:
    """Every name the text could use as a column (lower-cased), or ``None`` when it selects ``*`` (any column)."""

    words: set[str] = set()
    toks = lex(text)
    for i, tok in enumerate(toks):
        if tok.kind == "w":
            words.add(tok.text.casefold())
        elif tok.kind == "q":
            words.update(part.casefold() for part in tok.text.strip("`").split(".") if part)
        elif tok.kind == "p" and tok.text == "*":
            before = toks[i - 1] if i else None
            after = toks[i + 1] if i + 1 < len(toks) else None
            if (
                before is not None
                and (before.up in {"SELECT", "DISTINCT", "ALL", "STRUCT", "VALUE"} or before.text in {",", "."})
                or after is not None and after.up in {"EXCEPT", "REPLACE", "FROM"}
            ):
                return None
    return frozenset(words)


# ------------------------------------------------------------------------- flat splitting


@dataclass(frozen=True)
class ScriptPart:
    """A statement of a script, in order, with where it sits."""

    text: str
    line: int
    conditional: bool = False
    in_procedure: str = ""


def _line_index(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def split_script(text: str) -> list[ScriptPart]:
    """The statements of ``text`` in order. Blocks and control flow are opened, not returned as statements."""

    breaks = _line_index(text)
    parts: list[ScriptPart] = []

    def walk(nodes: list[Node], conditional: bool, procedure: str) -> None:
        for node in nodes:
            if node.kind == "stmt":
                parts.append(ScriptPart(node.header, bisect.bisect_left(breaks, node.start) + 1, conditional, procedure))
                continue
            inner = procedure or (node.name if node.kind == "procedure" else "")
            for index, branch in enumerate(node.branches):
                gated = conditional or node.kind in {"if", "while", "for", "repeat", "case", "loop"} or (node.kind in {"begin", "procedure"} and index > 0)
                walk(branch, gated, inner)

    walk(parse_script(text), False, "")
    return parts


def leaf_statements(text: str) -> list[Node]:
    """The statement nodes of ``text`` in order, with their offsets in ``text``; blocks are opened."""

    return [node for node in _walk_nodes(parse_script(text)) if node.kind == "stmt"]


def has_blocks(text: str) -> bool:
    """Whether ``text`` has ``BEGIN ... END``, ``IF``, a loop or a procedure around its statements."""

    return any(node.kind != "stmt" for node in parse_script(text))


_REWRITEABLE = {"SELECT", "WITH", "FROM", "CREATE", "INSERT", "MERGE", "UPDATE", "DELETE", "("}


def is_rewriteable(statement: str) -> bool:
    """A statement whose query text a rewrite rule may change (not a declaration, assignment or call)."""

    words = _first_words(statement, 1)
    return bool(words) and words[0] in _REWRITEABLE


_BLOCK_START = re.compile(r"\s*(?:BEGIN|IF|LOOP|WHILE|REPEAT|FOR|CASE)\b", re.IGNORECASE)
_BLOCK_WORDS = re.compile(r"\b(?:BEGIN|LOOP|WHILE|REPEAT|THEN|DO)\b", re.IGNORECASE)


def block_statements(text: str) -> list[exp.Expression] | None:
    """The parsed statements a rewrite may change, in order, when ``text`` has blocks; ``None`` for plain SQL.

    Raises what sqlglot raises for a statement that does not parse."""

    if not _BLOCK_WORDS.search(text) or not has_blocks(text):
        return None
    statements: list[exp.Expression] = []
    for node in leaf_statements(text):
        if is_rewriteable(node.header):
            with quiet_parser():
                statements.extend(s for s in sqlglot.parse(node.header, read="bigquery", error_level=ErrorLevel.RAISE) if s is not None)
    return statements


def script_skeleton(text: str) -> list[str]:
    """What a rewrite must leave alone in a script: its tokens with each rewriteable statement replaced by one placeholder.

    Control flow, conditions, ``DECLARE``/``SET`` expressions, ``CALL`` arguments, procedure headers and the order of everything
    stay in the skeleton. Layout and comments do not count (the tokens are compared, not the text), keywords and names are
    compared case-insensitively and strings exactly.
    """

    ranges = [(node.start, node.end) for node in leaf_statements(text) if is_rewriteable(node.header)]
    skeleton: list[str] = []
    inside = -1
    for token in lex(text):
        index = next((k for k, (start, end) in enumerate(ranges) if start <= token.start < end), -1)
        if index >= 0:
            if index != inside:
                skeleton.append("<statement>")
            inside = index
            continue
        inside = -1
        skeleton.append(token.text.upper() if token.kind == "w" else token.text)
    return skeleton


def split_statements(text: str) -> list[str]:
    """Just the statement texts, in order (see :func:`split_script`)."""

    return [part.text for part in split_script(text)]


# ----------------------------------------------------------------------- classification

_QUERY_STARTS = {"SELECT", "WITH", "FROM", "TABLE"}
_DDL = {"DROP", "ALTER", "GRANT", "REVOKE", "UNDROP", "TRUNCATE", "COMMENT"}
_FLOW = {"RAISE", "RETURN", "BREAK", "LEAVE", "CONTINUE", "ITERATE"}
_TRANSACTION = {"BEGIN", "COMMIT", "ROLLBACK", "START"}


def _code_start(text: str) -> int:
    """Index of the first character after the leading whitespace and comments."""

    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text[i] == "#" or text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def _first_words(text: str, count: int = 8) -> list[str]:
    # Only the start is lexed, measured from the first code: a long header comment must not hide the statement's kind.
    start = _code_start(text)
    words: list[str] = []
    for tok in lex(text[start : start + 600]):
        if tok.kind == "w":
            words.append(tok.up)
        elif tok.text == "(" and not words:
            words.append("(")
        else:
            words.append("")
        if len(words) >= count:
            break
    return words


_READ_WORDS = {"FROM", "JOIN", "USING"}
_WRITE_WORDS = {"INTO", "UPDATE", "MERGE"}
_NOT_TABLES = {
    "SELECT", "UNNEST", "LATERAL", "ONLY", "WHERE", "SET", "VALUES", "ON", "AS", "WITH", "GENERATE_ARRAY", "UNION", "ALL", "DISTINCT",
    "TABLE", "MODEL", "DELETE", "INSERT", "UPDATE", "MERGE", "INTO", "FROM", "JOIN", "USING", "SELECT", "NOT", "EXISTS", "IF", "NULL",
    "TRUE", "FALSE", "CASE", "WHEN", "THEN", "ELSE", "END", "OR", "AND", "REPLACE", "TEMP", "TEMPORARY", "VIEW", "MATERIALIZED", "EXTERNAL",
}
_CLAUSE_END = {"WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "INTERSECT", "EXCEPT", "WINDOW", "QUALIFY", "ON", "USING", "SET", "WHEN", "JOIN",
               "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS", "NATURAL", "SELECT", "VALUES", "OPTIONS", "PARTITION", "CLUSTER"}


def _name_at(tokens: list[Tok], i: int) -> tuple[exp.Table | None, int]:
    """The table named at ``tokens[i]`` (a quoted path or ``a.b.c``) and the index after it; ``None`` for a call or a subquery."""

    if i >= len(tokens):
        return None, i
    token = tokens[i]
    if token.kind == "q":
        text_ = token.text
        j = i + 1
    elif token.kind == "w" and token.up not in _NOT_TABLES:
        parts = [token.text]
        j = i + 1
        while j + 1 < len(tokens) and tokens[j].text == "." and tokens[j + 1].kind in {"w", "q"}:
            parts.append(tokens[j + 1].text)
            j += 2
        text_ = ".".join(parts)
    else:
        return None, (i + 1 if token.kind == "w" and i + 1 < len(tokens) and tokens[i + 1].text == "(" else i)
    if j < len(tokens) and tokens[j].text == "(":
        return None, j  # a function call such as UNNEST(x) or fn(TABLE t)
    try:
        table = exp.to_table(text_, dialect="bigquery")
    except Exception:  # noqa: BLE001
        return None, j
    return (table, j) if isinstance(table, exp.Table) and table.name else (None, j)


# Functions whose arguments are separated by ``FROM``: that ``FROM`` names no table.
_FROM_ARGUMENT_CALLS = {"EXTRACT", "TRIM", "SUBSTRING", "SUBSTR", "OVERLAY", "POSITION"}


def _enclosing_call(tokens: list[Tok], index: int) -> str:
    """The word before the innermost parenthesis still open at ``tokens[index]``; empty when there is none."""

    depth = 0
    for k in range(index - 1, -1, -1):
        if tokens[k].text == ")":
            depth += 1
        elif tokens[k].text == "(":
            if depth == 0:
                return tokens[k - 1].up if k else ""
            depth -= 1
    return ""


def token_reads(text: str) -> tuple[list[exp.Table], list[exp.Table]]:
    """``(tables read, tables written)`` found in the token stream of a statement that could not be parsed.

    A name after ``FROM``, ``JOIN``, ``USING`` or ``TABLE`` is read (``FROM a, b`` reads both), one after ``INTO``, ``UPDATE``,
    ``MERGE`` or ``CREATE ... TABLE|VIEW`` is written, and a ``CREATE`` also reads what it ``LIKE``s or ``CLONE``s. A name is a
    CTE, not a table, only where its WITH is in scope: after its body up to the parenthesis that closes the WITH's query
    (from the WITH on for ``WITH RECURSIVE``). Only the tables are found, never columns.
    """

    tokens = [t for t in lex(text) if t.kind != ";"]
    creates = bool(tokens) and tokens[0].up == "CREATE"
    reads: list[exp.Table] = []
    writes: list[exp.Table] = []

    def call_end(start: int) -> int:
        depth = 0
        for k in range(start, len(tokens)):
            if tokens[k].text == "(":
                depth += 1
            elif tokens[k].text == ")":
                depth -= 1
                if depth == 0:
                    return k + 1
        return len(tokens)

    # Parenthesis depth of each token; a ``(`` and its ``)`` have the depth of what surrounds them.
    levels: list[int] = []
    level = 0
    for token in tokens:
        if token.text == ")":
            level -= 1
        levels.append(level)
        if token.text == "(":
            level += 1

    def cte_body(k: int) -> int | None:
        """Where the body of a WITH table named at ``tokens[k]`` opens: ``name AS (``, ``name (a, b) AS (``,
        ``name AS [NOT] MATERIALIZED (``."""

        j = k + 1
        if j < len(tokens) and tokens[j].text == "(" and tokens[k - 1].text.upper() in {"WITH", ",", "RECURSIVE"}:
            j = call_end(j)  # a column list
        if j >= len(tokens) or tokens[j].up != "AS":
            return None
        j += 1
        if j < len(tokens) and tokens[j].up == "NOT":
            j += 1
        if j < len(tokens) and tokens[j].up == "MATERIALIZED":
            j += 1
        return j if j < len(tokens) and tokens[j].text == "(" else None

    ctes: dict[str, list[tuple[int, int]]] = {}  # name -> token ranges where it names a CTE
    for k in range(1, len(tokens)):
        if tokens[k].kind != "w" or (body := cte_body(k)) is None:
            continue
        start = call_end(body)  # a WITH table does not see itself
        w = k - 1
        while w >= 0 and levels[w] >= levels[k] and not (levels[w] == levels[k] and tokens[w].up == "WITH"):
            w -= 1
        if w >= 0 and levels[w] == levels[k] and tokens[w].up == "WITH" and w + 1 < len(tokens) and tokens[w + 1].up == "RECURSIVE":
            start = w
        end = next((i for i in range(start, len(tokens)) if levels[i] < levels[k]), len(tokens))
        ctes.setdefault(tokens[k].text.casefold(), []).append((start, end))

    def is_cte(table: exp.Table, at: int) -> bool:
        return not table.db and not table.catalog and any(s <= at < e for s, e in ctes.get(table.name.casefold(), ()))

    i = 0
    while i < len(tokens):
        word = tokens[i].up
        previous = tokens[i - 1].up if i else ""
        kind = None
        if word == "FROM" and (previous == "DISTINCT" or _enclosing_call(tokens, i) in _FROM_ARGUMENT_CALLS):
            pass  # ``EXTRACT(DATE FROM ts)``, ``TRIM(BOTH 'x' FROM s)``, ``a IS DISTINCT FROM b``: not a table
        elif word == "FROM":
            kind = "write" if previous == "DELETE" else "read"
        elif word in {"JOIN", "USING"}:
            kind = "read"
        elif word in {"INTO", "UPDATE", "MERGE", "INSERT"}:
            kind = "write"
        elif word == "TABLE":
            kind = "write" if previous in {"TEMP", "TEMPORARY", "REPLACE", "CREATE", "EXTERNAL", "SNAPSHOT", "DROP", "ALTER", "TRUNCATE"} else "read"
        elif word == "VIEW" and creates:
            kind = "write"
        elif word in {"LIKE", "CLONE"} and creates:
            kind = "read"
        elif word == "DELETE" and i + 1 < len(tokens) and tokens[i + 1].up != "FROM":
            kind = "write"
        if kind is None:
            i += 1
            continue
        j = i + 1
        while j < len(tokens) and tokens[j].up in {"INTO", "IF", "NOT", "EXISTS"}:  # MERGE INTO, IF [NOT] EXISTS
            j += 1
        scan_to = first = j
        while True:
            table, after = _name_at(tokens, j)
            if table is not None and not is_cte(table, j):
                (reads if kind == "read" else writes).append(table)
            if j == first:
                scan_to = max(scan_to, after)  # keep scanning inside a subquery or call argument list
            following = call_end(after) if table is None and after < len(tokens) and tokens[after].text == "(" else after
            if kind != "read" or word != "FROM":
                break
            # an alias, then a comma list (``FROM a, b``); stop at anything else
            if following < len(tokens) and tokens[following].up == "AS":
                following += 2
            elif following < len(tokens) and tokens[following].kind == "w" and tokens[following].up not in _CLAUSE_END:
                following += 1
            if following < len(tokens) and tokens[following].text == ",":
                j = following + 1
                continue
            break
        i = max(i + 1, scan_to)
    return reads, writes


def _parse_error_line(text: str) -> str:
    """Where sqlglot stopped ("Invalid expression / Unexpected token. Line 1, Col: 37."), without any SQL text; empty when it parses."""

    try:
        with quiet_parser():
            sqlglot.parse(text, read="bigquery")
    except Exception as exc:  # noqa: BLE001
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return re.sub(r"\s+", " ", first)[:160]
    return ""


def _parse_one(sql: str) -> exp.Expression | None:
    try:
        with quiet_parser():
            parsed = sqlglot.parse(sql, read="bigquery")
    except Exception:  # noqa: BLE001 - sqlglot raises several error types
        return None
    parsed = [p for p in parsed if p is not None]
    return parsed[0] if len(parsed) == 1 else None


def _query_of(statement: exp.Expression) -> exp.Expression | None:
    """The query a statement reads from, without any ``WITH`` that the statement itself carries."""

    if isinstance(statement, exp.Query):
        return statement
    if isinstance(statement, (exp.Create, exp.Insert)):
        node = statement.expression
        while isinstance(node, exp.Subquery):
            node = node.this
        return node if isinstance(node, exp.Query) else None
    return None


def _target_column(node: exp.Expression, names: set[str]) -> str | None:
    """The target column a ``SET`` or ``INSERT`` writes: ``t.v`` and ``v`` are ``v``, ``t.a.b`` is ``a`` (the struct column)."""

    parts = [part.name for part in getattr(node, "parts", ()) if getattr(part, "name", "")]
    if not parts:
        return None
    if len(parts) > 1 and parts[0].casefold() in names:
        parts = parts[1:]
    return parts[0]


def _values_of(node: exp.Expression | None) -> list[exp.Expression] | None:
    if isinstance(node, exp.Tuple):
        return list(node.expressions)
    if isinstance(node, exp.Paren):
        return [node.this]
    return None if node is None or node is False else [node]


def _qualified_with(node: exp.Expression, alias: str) -> exp.Expression:
    """``node`` with its bare column names (outside any subquery) qualified by ``alias``: a clause that cannot see the target
    reads its columns from the source, and a join to the target would make a bare name ambiguous."""

    copy = node.copy()
    for column in list(copy.find_all(exp.Column)):
        if column.table or isinstance(column.this, exp.Star):
            continue
        if column.find_ancestor(exp.Select, exp.Subquery) is not None:
            continue
        column.set("table", exp.to_identifier(alias))
    return copy


def _only_deletes(merge: exp.Merge) -> bool:
    """A MERGE whose clauses all delete: it writes no column, so there is nothing to trace."""

    whens = merge.args.get("whens")
    clauses = list(whens.expressions) if isinstance(whens, exp.Whens) else list(whens or [])
    return bool(clauses) and all(isinstance(when.args.get("then"), exp.Delete) or str(when.args.get("then")).upper() == "DELETE" for when in clauses)


def merge_query(merge: exp.Merge) -> exp.Query | None:
    """What flows into the target's columns, as a query the lineage code can trace; ``None`` when the MERGE is not understood.

    Each ``WHEN`` clause becomes one arm of a ``UNION ALL`` that selects the values it assigns (``SET c = e`` and
    ``INSERT (c) VALUES (e)``; every column the clause does not touch is ``NULL``), from the ``USING`` source joined to the target
    on the ``ON`` condition, filtered by the clause's own ``AND`` condition. The ``ON`` and condition columns are therefore
    read, as are the columns of any subquery in them. ``INSERT ROW`` takes every column of the source; a ``DELETE`` clause
    assigns nothing but still reads its condition. A clause of a shape that is not understood makes the whole thing ``None``:
    columns are then unknown, never guessed.
    """

    try:
        return _merge_query(merge)
    except Exception:  # sqlglot raises many types; an unreadable MERGE is unknown, never a crash
        return None


def insert_values_query(insert: exp.Insert) -> exp.Query | None:
    """``INSERT INTO t (a, b) VALUES (...), (...)`` as the query ``SELECT ... AS a, ... AS b UNION ALL ...``; ``None`` when not understood.

    The values are constants (or expressions over a scalar subquery, whose tables are then read like any other), so the model's
    columns are the insert list and a constant has no source column. Without a column list the names are unknown, and so is the
    result.
    """

    try:
        schema, values = insert.this, insert.expression
        if not isinstance(schema, exp.Schema) or not isinstance(values, exp.Values) or not schema.expressions:
            return None
        names = [item.name for item in schema.expressions if isinstance(item, (exp.Identifier, exp.Column))]
        if len(names) != len(schema.expressions) or not all(names):
            return None
        selects: list[exp.Query] = []
        for row in values.expressions:
            items = list(row.expressions) if isinstance(row, exp.Tuple) else [row]
            if len(items) != len(names) or any(isinstance(item, exp.Var) and item.name.upper() == "DEFAULT" for item in items):
                return None
            selects.append(exp.select(*[exp.alias_(item.copy(), name, quoted=False) for item, name in zip(items, names)]))
        if not selects:
            return None
        query: exp.Query = selects[0]
        for nxt in selects[1:]:
            query = exp.Union(this=query, expression=nxt, distinct=False)
        return query
    except Exception:  # an unreadable INSERT is unknown, never a crash
        return None


def _merge_query(merge: exp.Merge) -> exp.Query | None:
    target, using, on = merge.this, merge.args.get("using"), merge.args.get("on")
    whens = merge.args.get("whens")
    clauses = list(whens.expressions) if isinstance(whens, exp.Whens) else list(whens or [])
    if not isinstance(target, exp.Table) or using is None or on is None or not clauses:
        return None
    names = {n.casefold() for n in (target.alias, target.name) if n}
    referable = bool(using.alias) or isinstance(using, exp.Table)  # ``s.*`` needs something to name
    arms: list[tuple[str, list[tuple[str, exp.Expression]], bool, bool, exp.Expression | None]] = []
    columns: dict[str, str] = {}
    for when in clauses:
        then, matched, by_source, condition = when.args.get("then"), bool(when.args.get("matched")), bool(when.args.get("source")), when.args.get("condition")
        if isinstance(then, exp.Update):
            pairs = []
            for assignment in then.expressions:
                column = _target_column(assignment.this, names) if isinstance(assignment, exp.EQ) else None
                if column is None:
                    return None
                pairs.append((column, assignment.expression))
                columns.setdefault(column.casefold(), column)
            arms.append(("set", pairs, matched, by_source, condition))
        elif isinstance(then, exp.Insert):
            if isinstance(then.this, exp.Var) and then.this.name.upper() == "ROW":
                arms.append(("row", [], matched, by_source, condition))
                continue
            targets = _values_of(then.this) if then.this is not None else None
            values = _values_of(then.expression)
            if not targets or values is None or len(targets) != len(values):
                return None  # ``INSERT VALUES (...)`` without column names: positions need the target's schema
            pairs = []
            for column_node, value in zip(targets, values):
                column = _target_column(column_node, names)
                if column is None:
                    return None
                pairs.append((column, value))
                columns.setdefault(column.casefold(), column)
            arms.append(("set", pairs, matched, by_source, condition))
        elif isinstance(then, exp.Var) and then.name.upper() == "DELETE":
            arms.append(("delete", [], matched, by_source, condition))
        else:
            return None
    has_row = any(kind == "row" for kind, *_ in arms)
    if not columns and not has_row:
        return None  # only deletes: nothing is written, there are no columns to trace
    ordered = list(columns.values())
    selects: list[exp.Select] = []
    source_name = using.alias or (using.name if isinstance(using, exp.Table) else "")
    for kind, pairs, matched, by_source, condition in arms:
        sees_target = matched or by_source
        # a clause that cannot see the target names the source's columns, and a bare name must stay the source's
        fix = (lambda node: node.copy()) if sees_target or not source_name else (lambda node, alias=source_name: _qualified_with(node, alias))
        if kind == "row" or (kind == "delete" and not ordered):
            star = exp.Column(this=exp.Star(), table=exp.to_identifier(using.alias)) if using.alias else (
                exp.Column(this=exp.Star(), table=exp.to_identifier(using.name)) if isinstance(using, exp.Table) else exp.Star()
            )
            projections: list[exp.Expression] = [star]
        elif not has_row:
            given = {column.casefold(): fix(value) for column, value in pairs}
            projections = [exp.alias_(given[c.casefold()] if c.casefold() in given else exp.Null(), c, quoted=False) for c in ordered]
        else:  # INSERT ROW beside explicit columns: arms keep only their own columns and are lined up by name
            projections = [exp.alias_(fix(value), column, quoted=False) for column, value in pairs] or [exp.alias_(exp.Null(), ordered[0], quoted=False)]
        select = exp.Select(expressions=projections)
        if by_source:
            select = select.from_(target.copy())
            select = select.join(using.copy(), on=on.copy(), join_type="left")
        elif kind in {"row"} and not referable:
            select = select.from_(using.copy())
        else:
            select = select.from_(using.copy())
            select = select.join(target.copy(), on=on.copy(), join_type=None if matched else "left")
        if condition is not None:
            select = select.where(fix(condition))
        selects.append(select)
    query: exp.Query = selects[0] if len(selects) == 1 else exp.union(*selects, distinct=False)
    if has_row and len(selects) > 1 and ordered:
        for node in query.find_all(exp.SetOperation):
            node.set("by_name", True)
    clause = with_clause(merge)
    if clause is not None:
        set_with_clause(query, clause.copy())
    return query


def _table_ref(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _norm(name: str) -> str:
    return name.replace("`", "").casefold()


def _clean(table: exp.Table) -> exp.Table:
    """The table without its alias, joins or sampling: just the name."""

    copy = table.copy()
    for key in ("alias", "joins", "laterals", "pivots", "sample"):
        if copy.args.get(key):
            copy.set(key, None)
    return copy


def _is_temp_name(table: exp.Table) -> bool:
    return not table.catalog and (not table.db or table.db.casefold() == "_session")


# ----------------------------------------------------------------------------- analysis


@dataclass
class Statement:
    """What was decided about one statement. Carries no SQL text."""

    index: int
    line: int
    kind: str
    disposition: str
    reason: str = ""
    conditional: bool = False
    nested: str = ""  # "procedure" or "execute_immediate" when it was read from inside one
    traced: bool = False  # its columns are part of the output query's column lineage
    definition: bool = False  # defines a routine: not a step of the data flow, never counted as skipped
    complete: bool = False  # writes no column (DELETE, a delete-only MERGE): nothing is left to trace
    degraded: bool = False  # could not be parsed; its table reads and writes come from its tokens, its columns are unknown
    error: str = ""  # where sqlglot stopped (line and column, no SQL text)
    reads: dict = field(default_factory=dict, repr=False, compare=False)  # real tables it reads directly

    def to_json(self) -> dict:
        row = {"line": self.line, "kind": self.kind, "disposition": self.disposition}
        if self.reason:
            row["reason"] = self.reason
        if self.conditional:
            row["conditional"] = True
        if self.nested:
            row["nested"] = self.nested
        if self.definition:
            row["definition"] = True
        if self.degraded:
            row["degraded"] = True
        return row


@dataclass
class ScriptWrite:
    """A table a script writes and the real tables that feed it."""

    table: exp.Table
    sources: tuple[exp.Table, ...]
    kind: str
    conditional: bool = False
    via_variable: bool = False


@dataclass(eq=False)
class _Temp:
    """One version of a temporary table: what it was built from and, when known, the query that builds it."""

    name: str
    alias: str
    sources: dict[str, exp.Table] = field(default_factory=dict)
    query: exp.Query | None = None
    columns: tuple[str, ...] | None = None
    deps: tuple["_Temp", ...] = ()
    opaque_reason: str = ""
    sequence: int = 0
    statements: list["Statement"] = field(default_factory=list)  # the statements whose columns this version's query carries
    empty: bool = False  # created with a schema and no rows yet


@dataclass
class _Variable:
    sources: dict[str, exp.Table] = field(default_factory=dict)


@dataclass
class Procedure:
    name: str
    params: tuple[str, ...]
    body: list[Node]
    text: str  # the script text the body's offsets point into
    modes: tuple[str, ...] = ()  # IN, OUT or INOUT per parameter


@dataclass
class _Output:
    statement: Statement
    node: exp.Expression  # the statement with temporary tables renamed to their versions
    query: exp.Query
    temps: tuple[_Temp, ...]  # versions the query reads directly
    sources: dict[str, exp.Table]
    target: exp.Table | None
    conditional: bool
    top_level: bool


class ScriptAnalysis:
    """The result of :func:`analyse_script`."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.statements: list[Statement] = []
        self.writes: list[ScriptWrite] = []
        self.reads: dict[str, exp.Table] = {}
        self.variable_reads: dict[str, exp.Table] = {}
        self.definition_reads: dict[str, exp.Table] = {}  # tables the bodies of routine definitions read
        # Tables read outside the traced output query (by other statements, conditions, variables and routine bodies):
        # column lineage does not see which of their columns are read.
        self.side_reads: dict[str, exp.Table] = {}
        self.procedures: dict[str, Procedure] = {}
        self.procedure_reports: list[dict] = []
        self.opaque_temps: dict[str, str] = {}
        self._outputs: list[_Output] = []
        self._final: _Output | None = None
        self._ctes: list[tuple[str, exp.Query]] = []
        self.temp_names: set[str] = set()

    # -- results
    @property
    def final(self) -> exp.Expression | None:
        """The statement whose columns define the script's output: its last unconditional query."""

        return self._final.node if self._final is not None else None

    @property
    def final_query(self) -> exp.Query | None:
        return self._final.query if self._final is not None else None

    @property
    def final_target(self) -> exp.Table | None:
        return self._final.target if self._final is not None else None

    def with_temp_ctes(self, query: exp.Query) -> exp.Query:
        """``query`` (the final one, already carrying its own CTEs) with the temporary tables it reads defined in front of it."""

        if not self._ctes:
            return query
        clause = with_clause(query)
        ours = [exp.CTE(this=body.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))) for alias, body in self._ctes]
        if clause is None:
            clause = exp.With(expressions=ours)
        else:
            clause = clause.copy()
            clause.set("expressions", [*ours, *clause.expressions])
        set_with_clause(query, clause)
        return query

    def counts(self) -> dict[str, int]:
        out = {KEPT: 0, IGNORED: 0, UNKNOWN: 0}
        for s in self.statements:
            if not s.nested:
                out[s.disposition] += 1
        return out

    @property
    def unknown(self) -> list[Statement]:
        return [s for s in self.statements if s.disposition == UNKNOWN]

    @property
    def considered(self) -> int:
        """Statements that matter for lineage: kept or unknown. Ignored ones are thrown away on purpose."""

        return sum(1 for s in self.statements if s.disposition != IGNORED and not s.nested and not s.definition)

    @property
    def traced(self) -> int:
        return sum(1 for s in self.statements if s.traced and not s.nested)

    @property
    def untraced_kept(self) -> int:
        """Kept statements whose columns are not part of the traced output (their tables are still dependencies)."""

        return sum(1 for s in self.statements if s.disposition == KEPT and not s.traced and not s.nested and not s.definition and not s.complete)

    def untraced_kinds(self) -> dict[str, int]:
        """Kinds of the statements that matter but whose columns are not traced (kept without a trace, or unknown)."""

        kinds: dict[str, int] = {}
        for s in self.statements:
            if s.nested or s.definition or s.traced or s.complete or s.disposition == IGNORED:
                continue
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        return dict(sorted(kinds.items()))

    def all_reads(self) -> list[exp.Table]:
        merged = dict(self.reads)
        merged.update(self.variable_reads)
        return list(merged.values())

    def report(self) -> dict:
        """Counts and kinds only: no SQL text, names or paths."""

        by_kind: dict[str, dict[str, int]] = {KEPT: {}, IGNORED: {}, UNKNOWN: {}}
        for s in self.statements:
            if s.nested:
                continue
            by_kind[s.disposition][s.kind] = by_kind[s.disposition].get(s.kind, 0) + 1
        for s in self.unknown:
            if s.nested:  # unknown inside a CALL or EXECUTE IMMEDIATE still counts as unknown
                by_kind[UNKNOWN][s.kind] = by_kind[UNKNOWN].get(s.kind, 0) + 1
        out = {
            "statements": sum(self.counts().values()),
            "kept": self.counts()[KEPT],
            "ignored": self.counts()[IGNORED],
            "unknown": len(self.unknown),
            "by_kind": {k: dict(sorted(v.items())) for k, v in by_kind.items() if v},
            "conditional": sum(1 for s in self.statements if s.conditional and s.disposition == KEPT),
            "expanded": sum(1 for s in self.statements if s.nested),
            "traced": self.traced,
            "temporary_tables": len(self.temp_names),
            "variable_dependencies": len(self.variable_reads),
            "writes": len(self.writes),
            "detail": [s.to_json() for s in self.statements if s.disposition != IGNORED or not s.nested][:200],
        }
        if self.opaque_temps:
            out["temporary_tables_untraced"] = len(self.opaque_temps)
        if self.procedure_reports:
            out["procedures"] = self.procedure_reports
        return out

    def summary(self) -> str:
        """A one-line description of what was kept, ignored and unknown, for a diagnostic."""

        c = self.counts()
        kinds = self.report()["by_kind"]
        parts = [f"{c[KEPT]} kept", f"{c[IGNORED]} ignored"]
        if self.unknown:
            detail = ", ".join(f"{kind} x{count}" for kind, count in kinds.get(UNKNOWN, {}).items())
            nested = sum(1 for s in self.unknown if s.nested)
            parts.append(f"{len(self.unknown)} unknown ({detail}{f'; {nested} nested' if nested else ''})")
        else:
            parts.append("0 unknown")
        extra = []
        if self.temp_names:
            extra.append(f"{len(self.temp_names)} temporary table(s) followed")
        if self.variable_reads:
            extra.append(f"{len(self.variable_reads)} table(s) read through variables or conditions")
        if any(s.conditional and s.disposition == KEPT for s in self.statements):
            extra.append("statements in branches count as possible edges")
        text = f"script of {sum(c.values())} statements: " + ", ".join(parts)
        return text + ("; " + "; ".join(extra) if extra else "")


@dataclass
class TableFunction:
    """``CREATE TABLE FUNCTION name(params) AS (query)``: kept so a call can be read as the query it stands for."""

    name: str
    params: tuple[tuple[str, bool, tuple[str, ...]], ...]  # (name, is a table parameter, the columns it declares)
    body: exp.Query


_TABLE_FUNCTION = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+FUNCTION\b", re.I)


def _table_function(statement: str) -> TableFunction | None:
    """The definition in one ``CREATE TABLE FUNCTION`` statement, or ``None`` when it is not a plain SQL one."""

    tokens = lex(statement)
    start = next((k for k, t in enumerate(tokens) if t.up == "FUNCTION"), None)
    if start is None:
        return None
    j = start + 1
    while j < len(tokens) and tokens[j].up in {"IF", "NOT", "EXISTS"}:
        j += 1
    parts: list[str] = []
    while j < len(tokens) and tokens[j].kind in {"w", "q"}:
        parts.append(tokens[j].text.strip("`"))
        j += 1
        if j < len(tokens) and tokens[j].text == "." and j + 1 < len(tokens):
            j += 1
            continue
        break
    if not parts or j >= len(tokens) or tokens[j].text != "(":
        return None
    name = ".".join(parts)
    depth, close = 0, None
    for k in range(j, len(tokens)):
        if tokens[k].text == "(":
            depth += 1
        elif tokens[k].text == ")":
            depth -= 1
            if depth == 0:
                close = k
                break
    if close is None:
        return None
    params: list[tuple[str, bool, tuple[str, ...]]] = []
    current: list[Tok] = []
    depth = 0
    for tok in [*tokens[j + 1 : close], Tok(",", ",", 0, 0)]:
        if tok.text in {"(", "<"}:
            depth += 1
        elif tok.text in {")", ">"}:
            depth -= 1
        if tok.text == "," and depth == 0:
            if current and current[0].kind in {"w", "q"}:
                rest = current[1:]
                is_table = bool(rest) and (rest[0].up == "TABLE" or (rest[0].up == "ANY" and len(rest) > 1 and rest[1].up == "TABLE"))
                columns: list[str] = []
                level = 0
                expecting = False
                for part in rest:
                    if part.text == "<":
                        level += 1
                        expecting = level == 1
                    elif part.text == ">":
                        level -= 1
                    elif part.text == "," and level == 1:
                        expecting = True
                    elif expecting and level == 1 and part.kind in {"w", "q"}:
                        columns.append(part.text.strip("`"))
                        expecting = False
                params.append((current[0].text.strip("`"), is_table, tuple(columns)))
            current = []
            continue
        current.append(tok)
    as_at = next((k for k in range(close + 1, len(tokens)) if tokens[k].up == "AS"), None)
    if as_at is None or as_at + 1 >= len(tokens):
        return None
    first = tokens[as_at + 1]
    body_text = statement[first.start :]
    if first.text == "(":
        end = tokens[-1].end
        inner = statement[first.end : end]
        inner = inner.rstrip()
        if inner.endswith(")"):
            body_text = inner[:-1]
    body = _parse_one(body_text)
    if body is None or not isinstance(body, exp.Query):
        return None
    return TableFunction(name, tuple(params), body)


_TVF_COUNTER = itertools.count(1)


def _inline_table_function(definition: TableFunction, call: exp.Func) -> exp.Query | None:
    """The function's query for one call: table arguments become CTEs the body reads, scalar arguments replace the parameters.

    ``None`` when the arguments do not fit the parameters (then the call stays an opaque relation, never guessed).
    """

    try:
        body = definition.body.copy()
        arguments = list(call.expressions)
        bound: dict[str, exp.Expression] = {}
        position = 0
        for argument in arguments:
            if isinstance(argument, exp.Kwarg):
                bound[argument.this.name.casefold()] = argument.expression
            else:
                if position >= len(definition.params):
                    return None
                bound[definition.params[position][0].casefold()] = argument
                position += 1
        declared = {col.casefold() for _n, is_table, cols in definition.params if is_table for col in cols}
        ctes: list[exp.CTE] = []
        for name, is_table, _columns in definition.params:
            argument = bound.get(name.casefold())
            if argument is None:
                return None
            if is_table:
                if isinstance(argument, exp.Anonymous) and argument.name == "__KUMO_TABLE_ARGUMENT__" and len(argument.expressions) == 1:
                    argument = argument.expressions[0]
                if isinstance(argument, exp.Table):
                    source: exp.Query = exp.select("*").from_(argument.copy())
                elif isinstance(argument, (exp.Subquery, exp.Paren)) and isinstance(argument.this, exp.Query):
                    source = argument.this.copy()
                elif isinstance(argument, exp.Column) and not argument.table:
                    source = exp.select("*").from_(exp.to_table(argument.name))
                else:
                    return None
                alias = f"__tvf_{next(_TVF_COUNTER)}_{name}"
                for table in body.find_all(exp.Table):
                    if not table.db and not table.catalog and table.name.casefold() == name.casefold():
                        table.set("this", exp.to_identifier(alias))
                        if not table.args.get("alias"):
                            table.set("alias", exp.TableAlias(this=exp.to_identifier(name)))  # ``t.k`` in the body still resolves
                ctes.append(exp.CTE(this=source, alias=exp.TableAlias(this=exp.to_identifier(alias))))
            elif name.casefold() not in declared:
                for column in list(body.find_all(exp.Column)):
                    if not column.table and column.name.casefold() == name.casefold():
                        column.replace(exp.paren(argument.copy()) if not isinstance(argument, (exp.Literal, exp.Column)) else argument.copy())
        clause = with_clause(body)
        if clause is None:
            clause = exp.With(expressions=ctes)
        else:
            clause = clause.copy()
            clause.set("expressions", [*ctes, *clause.expressions])
        if ctes or clause.expressions:
            set_with_clause(body, clause)
        return body
    except Exception:  # noqa: BLE001 - an unreadable call is an opaque relation
        return None


def _routine_name(statement: str) -> str:
    """The bare lower-cased name of the routine a ``CREATE FUNCTION`` statement defines."""

    match = re.search(r"\bFUNCTION\b\s*(?:IF\s+NOT\s+EXISTS\s+)?((?:`[^`]+`|[\w$]+)(?:\s*\.\s*(?:`[^`]+`|[\w$]+))*)\s*\(", statement, re.I)
    return match.group(1).replace("`", "").split(".")[-1].strip().casefold() if match else ""


def collect_table_functions(texts: Iterable[str]) -> dict[str, TableFunction]:
    """Table functions defined in ``texts``, by lower-cased name as written and by bare name when that is unambiguous."""

    found: dict[str, TableFunction] = {}
    bare: dict[str, list[TableFunction]] = {}
    for text in texts:
        if not _TABLE_FUNCTION.search(text):
            continue
        try:
            nodes = parse_script(text)
        except Exception:  # noqa: BLE001
            continue
        for node in _walk_nodes(nodes):
            if node.kind == "stmt" and _TABLE_FUNCTION.match(node.header):
                function = _table_function(node.header)
                if function is not None:
                    found[_norm(function.name)] = function
                    bare.setdefault(_norm(function.name).split(".")[-1], []).append(function)
    for name, functions in bare.items():
        if len(functions) == 1 and name not in found:
            found[name] = functions[0]
    return found


def collect_procedures(texts: Iterable[str]) -> dict[str, Procedure]:
    """Procedures defined in ``texts``, by lower-cased name as written and by bare name when that is unambiguous."""

    found: dict[str, Procedure] = {}
    bare: dict[str, list[Procedure]] = {}
    for text in texts:
        if "procedure" not in text.lower():
            continue
        try:
            nodes = parse_script(text)
        except Exception:  # noqa: BLE001
            continue
        for node in _walk_nodes(nodes):
            if node.kind == "procedure" and node.name:
                params = _parameters(node.params)
                body = node.branches[0] if node.branches else []
                proc = Procedure(node.name, tuple(n for n, _m in params), body, text, tuple(m for _n, m in params))
                found[_norm(node.name)] = proc
                bare.setdefault(_norm(node.name).split(".")[-1], []).append(proc)
    for name, procs in bare.items():
        if len(procs) == 1 and name not in found:
            found[name] = procs[0]
    return found


def _walk_nodes(nodes: list[Node]) -> Iterator[Node]:
    for node in nodes:
        yield node
        for branch in node.branches:
            yield from _walk_nodes(branch)


def _parameters(params: str) -> list[tuple[str, str]]:
    """``(name, mode)`` of each parameter of a procedure: the mode is ``IN`` (the default), ``OUT`` or ``INOUT``."""

    inner = params.strip()
    if inner.startswith("("):
        inner = inner[1:]
    found = []
    depth = 0
    expecting = True
    mode = "IN"
    for tok in lex(inner):
        if tok.kind == "p" and tok.text == "(":
            depth += 1
        elif tok.kind == "p" and tok.text == ")":
            if depth == 0:
                break
            depth -= 1
        elif tok.kind == "p" and tok.text == "," and depth == 0:
            expecting = True
            mode = "IN"
        elif expecting and depth == 0 and tok.kind in {"w", "q"}:
            if tok.kind == "w" and tok.up in {"IN", "OUT", "INOUT"}:
                mode = tok.up
                continue
            found.append((tok.text.strip("`"), mode))
            expecting = False
    return found


def _parameter_names(params: str) -> list[str]:
    return [name for name, _mode in _parameters(params)]


def analyse_script(
    text: str, *, procedures: Mapping[str, Procedure] | None = None, functions: Mapping[str, TableFunction] | None = None
) -> ScriptAnalysis:
    """Split ``text`` and follow what each statement reads and writes. Never raises."""

    analysis = ScriptAnalysis(text)
    try:
        _Run(analysis, procedures or {}, functions or {}).run(text)
    except RecursionError:  # pathological nesting: report what was read so far as unknown
        analysis.statements.append(Statement(len(analysis.statements), 0, "script", UNKNOWN, "nested too deeply to read"))
    return analysis


class _Run:
    def __init__(self, analysis: ScriptAnalysis, external: Mapping[str, Procedure], functions: Mapping[str, TableFunction] = ()) -> None:
        self.a = analysis
        self.external = dict(external)
        self.functions = dict(functions)
        self.function_reads: dict[str, dict[str, exp.Table]] = {}  # routine defined in this script -> the tables its body reads
        self.temps: dict[str, _Temp] = {}
        self.versions: dict[str, int] = {}
        self.variables: dict[str, _Variable] = {}
        self.sequence = 0
        self.used_names: set[str] = set()
        self.call_stack: list[str] = []
        self.breaks: list[int] = []
        self.nested = ""
        self.anchor_line = 0  # the line of the CALL or EXECUTE IMMEDIATE that statements read from inside it are reported at
        self._depth = 0
        self.control: list[dict[str, exp.Table]] = []  # tables the conditions around the current statement read
        self.scopes: list[dict[str, _Variable | None]] = [{}]  # per open block: each name it declared -> the binding it hid
        self.transactions: list[tuple[dict[str, _Temp], dict[int, tuple[dict, list]]]] = []  # temporary tables at each BEGIN TRANSACTION
        self.parameters: dict[str, dict[str, exp.Table]] | None = None  # EXECUTE IMMEDIATE ... USING: what each @name or ? holds
        self.default_dataset = ""  # SET @@dataset_id
        self.default_project = ""  # SET @@dataset_project_id
        self._loops = 0

    # -- driving
    def run(self, text: str) -> None:
        self.a.procedures = {}
        if ";" not in text:
            # one statement, the common case: no need to cut it apart
            nodes = [Node("stmt", 0, len(text), header=text.strip())] if text.strip() else []
            if _BLOCK_START.match(text):
                nodes = parse_script(text)
        else:
            nodes = parse_script(text)
            for proc in collect_procedures([text]).values():
                self.a.procedures[_norm(proc.name)] = proc
            if _TABLE_FUNCTION.search(text):
                self.functions = {**collect_table_functions([text]), **self.functions}
        self.breaks = _line_index(text)
        self.used_names = {m.casefold() for m in re.findall(r"\b[A-Za-z_]\w*\b", text)} if ";" in text else set()
        self.nodes(nodes, text, conditional=False, top_level=True)
        self.choose_final()

    def line_of(self, text: str, offset: int) -> int:
        if text is self.a.text:
            return bisect.bisect_left(self.breaks, offset) + 1
        return bisect.bisect_left(_line_index(text), offset) + 1

    def nodes(self, nodes: list[Node], text: str, *, conditional: bool, top_level: bool) -> None:
        for node in nodes:
            self.node(node, text, conditional=conditional, top_level=top_level)

    def node(self, node: Node, text: str, *, conditional: bool, top_level: bool) -> None:
        line = self.line_of(text, node.start)
        if node.kind == "stmt":
            self.statement(node.header, line, text, conditional=conditional, top_level=top_level)
            return
        if node.kind == "procedure":
            self.record(Statement(len(self.a.statements), line, "create_procedure", KEPT, "definition; read when called", conditional), nested=False)
            self.describe_procedure(node, text)
            return
        looping = node.kind in {"while", "loop", "for", "repeat"}
        statement = self.record(Statement(len(self.a.statements), line, node.kind if node.kind != "begin" else "block", IGNORED, "control flow", conditional))
        if looping:
            self.settle_loop(node, text, top_level, statement)
        self.control_flow(node, text, conditional=conditional, top_level=top_level, statement=statement)

    def control_flow(self, node: Node, text: str, *, conditional: bool, top_level: bool, statement: Statement | None) -> None:
        """Run the branches of a block once: the tables its conditions read feed everything inside it, and names it declares end with it."""

        looping = node.kind in {"while", "loop", "for", "repeat"}
        line = self.line_of(text, node.start)
        guard: dict[str, exp.Table] = {}
        for condition in node.conditions:
            sources, ok = self.value_sources(condition)
            if not ok and statement is not None:
                statement.disposition, statement.reason = UNKNOWN, "condition could not be read"
            guard.update(sources)
        hidden: dict[str, _Variable | None] = {}
        if node.kind == "for":
            hidden[node.name.casefold()] = self.variables.get(node.name.casefold())
            guard.update(self.bind_loop_variable(node, line))
        self.note_reads(guard)
        self.control.append(guard)
        self.scopes.append(hidden)
        try:
            for index, branch in enumerate(node.branches):
                gated = conditional or node.kind in {"if", "case"} or looping or index > 0
                self.nodes(branch, text, conditional=gated, top_level=top_level)
        finally:
            self.control.pop()
            self.close_scope(self.scopes.pop())

    def close_scope(self, hidden: dict[str, "_Variable | None"]) -> None:
        for name, previous in hidden.items():
            if previous is None:
                self.variables.pop(name, None)
            else:
                self.variables[name] = previous

    def note_reads(self, sources: dict[str, exp.Table]) -> None:
        """Tables read to decide something (a condition, a variable's value): direct reads, or reads through a variable."""

        for key, table in sources.items():
            if key not in self.a.reads:
                self.a.variable_reads.setdefault(key, table)
            self.a.side_reads.setdefault(key, table)

    def guarded(self) -> dict[str, exp.Table]:
        """The tables the conditions around the current statement read: whether it runs depends on them."""

        found: dict[str, exp.Table] = {}
        for guard in self.control:
            found.update(guard)
        return found

    # -- loops
    def settle_loop(self, node: Node, text: str, top_level: bool, statement: Statement) -> None:
        """Bring the variables to what any iteration can start with, by running the body until they stop changing.

        A value set late in one iteration is read early in the next. Each trial run is thrown away; only the variables
        are kept, so the real run below sees every source a variable can hold at any point of the loop."""

        if self._loops >= MAX_LOOP_NESTING:
            statement.disposition, statement.reason = UNKNOWN, "loops nested too deeply to follow"
            return
        for _ in range(MAX_LOOP_PASSES):
            before = self.variable_state()
            saved = self.save_state()
            self._loops += 1
            try:
                self.control_flow(node, text, conditional=True, top_level=False, statement=None)
            finally:
                self._loops -= 1
                self.restore_state(saved)
            if self.variable_state() == before:
                return
        statement.disposition, statement.reason = UNKNOWN, "loop variables did not settle"

    def variable_state(self) -> dict[str, frozenset[str]]:
        return {name: frozenset(var.sources) for name, var in self.variables.items()}

    def save_state(self) -> tuple:
        """Everything a trial run of a loop body changes, except the variables."""

        analysis = self.a
        scratch = ScriptAnalysis(analysis.text)
        scratch.procedures = analysis.procedures
        self.a = scratch
        return (
            analysis,
            self.temp_snapshot(),
            dict(self.versions),
            self.sequence,
            dict(self.function_reads),
            list(self.transactions),
            (self.default_dataset, self.default_project),
        )

    def restore_state(self, saved: tuple) -> None:
        self.a, temps, versions, self.sequence, self.function_reads, self.transactions, defaults = saved
        self.restore_temps(temps)
        self.versions = versions
        self.default_dataset, self.default_project = defaults

    def temp_snapshot(self) -> tuple[dict[str, _Temp], dict[int, tuple[dict, list]]]:
        """The current version of each temporary table, and what can still change inside each version (a DELETE adds to it)."""

        return dict(self.temps), {id(t): (dict(t.sources), list(t.statements)) for t in self.temps.values()}

    def restore_temps(self, snapshot: tuple[dict[str, _Temp], dict[int, tuple[dict, list]]]) -> None:
        temps, inside = snapshot
        for temp in temps.values():
            sources, statements = inside[id(temp)]
            temp.sources, temp.statements = dict(sources), list(statements)
        self.temps = dict(temps)

    def record(self, statement: Statement, *, nested: bool | None = None) -> Statement:
        if self.nested:
            statement.nested = self.nested
            statement.line = self.anchor_line or statement.line
        self.a.statements.append(statement)
        return statement

    # -- procedures
    def describe_procedure(self, node: Node, text: str) -> None:
        """What a procedure reads and writes, read once from its definition."""

        sub = ScriptAnalysis(text)
        runner = _Run(sub, self.external)
        runner.breaks = _line_index(text)
        runner.used_names = self.used_names
        for name in _parameter_names(node.params):
            runner.variables[name.casefold()] = _Variable()
        runner.nodes(node.branches[0] if node.branches else [], text, conditional=False, top_level=False)
        runner.nested = ""
        self.a.procedure_reports.append(
            {
                "line": self.line_of(text, node.start),
                "statements": len(sub.statements),
                "reads": len(sub.reads),
                "writes": len(sub.writes),
                "unknown": len(sub.unknown),
            }
        )

    def known_procedure(self, name: str) -> Procedure | None:
        key = _norm(name)
        for registry in (self.a.procedures, self.external):
            if key in registry:
                return registry[key]
            bare = key.split(".")[-1]
            hits = {id(p): p for k, p in registry.items() if k == bare or k.endswith("." + bare)}
            if len(hits) == 1:
                return next(iter(hits.values()))
        return None

    # -- variables
    def bind_loop_variable(self, node: Node, line: int) -> dict[str, exp.Table]:
        """The FOR loop's row variable holds what its query reads; the same tables decide how often the body runs."""

        sources, ok = self.value_sources(node.query)
        self.variables[node.name.casefold()] = _Variable(dict(sources))
        if not ok:
            self.record(Statement(len(self.a.statements), line, "for_query", UNKNOWN, "loop query could not be read"))
        return sources

    def value_sources(self, expression: str) -> tuple[dict[str, exp.Table], bool]:
        """Tables an expression's value comes from: what it reads, directly or through the variables it names."""

        sources, ok = self.sources_of_text(expression)
        for key, table in self.variable_sources_of_text(expression):
            sources.setdefault(key, table)
        return sources, ok

    def sources_of_text(self, expression: str) -> tuple[dict[str, exp.Table], bool]:
        """Real tables read by an expression or query text, through temporary tables and variables. ``ok`` is False if it did not parse."""

        tree = _parse_one(expression) if re.match(r"\s*(SELECT|WITH|\()", expression, re.I) else _parse_one("SELECT " + expression)
        if tree is None:
            ok = not re.search(r"\b(from|join)\b", expression, re.I)
            return {}, ok
        found, _temps, _unresolved = self.reads_of(tree)
        return found, True

    def declare_variables(self, names: list[str], sources: dict[str, exp.Table]) -> None:
        scope = self.scopes[-1]
        for name in names:
            key = name.casefold()
            if key not in scope:
                scope[key] = self.variables.get(key)
            self.variables[key] = _Variable({**sources, **self.guarded()})

    def assign(self, name: str, sources: dict[str, exp.Table], conditional: bool) -> None:
        """A variable takes a new value; on a path that may not run, it may also keep its old one."""

        key = name.casefold()
        sources = {**sources, **self.guarded()}
        existing = self.variables.get(key)
        if existing is not None and conditional:
            self.variables[key] = _Variable({**existing.sources, **sources})
        else:
            self.variables[key] = _Variable(dict(sources))

    def variable_sources(self, tree: exp.Expression) -> dict[str, exp.Table]:
        found: dict[str, exp.Table] = {}
        if self.parameters is not None:
            for parameter in tree.find_all(exp.Parameter, exp.Placeholder):
                name = parameter.name.casefold() if isinstance(parameter, exp.Parameter) else ""
                if name.startswith("@"):
                    continue  # a system variable
                found.update(self.parameters.get(name) or self.parameters.get("", {}))
        if not self.variables:
            return found
        for column in tree.find_all(exp.Column):
            name = (column.table or column.name).casefold() if column.table else column.name.casefold()
            var = self.variables.get(name)
            if var is not None:
                found.update(var.sources)
        return found

    # -- reading tables out of a syntax tree
    def reads_of(
        self, tree: exp.Expression, skip: Iterable[exp.Table | None] = ()
    ) -> tuple[dict[str, exp.Table], list[_Temp], list[str]]:
        """``(real tables, temporary tables read directly, unqualified names that no temporary table has yet)``.

        ``skip`` are table nodes of the statement that are written, not read."""

        skipped = {id(t) for t in skip if t is not None}
        real: dict[str, exp.Table] = {}
        temps: list[_Temp] = []
        late: list[str] = []
        for table in tree.find_all(exp.Table):
            if id(table) in skipped or not table.name or not isinstance(table.this, exp.Identifier):
                continue
            if binding_cte(table) is not None:
                continue  # a WITH table in scope here (a nested ``WITH t`` does not hide a read of the table t elsewhere)
            if _is_temp_name(table) and table.name.casefold() in self.temps:
                version = self.temps[table.name.casefold()]
                if version not in temps:
                    temps.append(version)
                real.update(version.sources)
                continue
            if _is_temp_name(table) and not table.db:
                late.append(table.name.casefold())
            real[_norm(_table_ref(table))] = _clean(table)
        if self.function_reads:
            for call in tree.find_all(exp.Anonymous):
                real.update(self.function_reads.get(call.name.casefold().split(".")[-1], {}))  # a call reads what its body reads
        return real, temps, late

    # -- statements
    def statement(self, text_: str, line: int, text: str, *, conditional: bool, top_level: bool) -> None:
        words = _first_words(text_)
        first = words[0] if words else ""
        index = len(self.a.statements)

        def done(kind: str, disposition: str, reason: str = "") -> Statement:
            return self.record(Statement(index, line, kind, disposition, reason, conditional))

        if first in _QUERY_STARTS or first == "(":
            return self.query_statement(text_, line, conditional, top_level, kind="select")
        if first == "CREATE":
            return self.create_statement(text_, line, words, conditional, top_level)
        if first == "INSERT":
            return self.dml_statement(text_, line, "insert", conditional)
        if first == "MERGE":
            return self.dml_statement(text_, line, "merge", conditional)
        if first == "UPDATE":
            return self.dml_statement(text_, line, "update", conditional)
        if first == "DELETE":
            return self.dml_statement(text_, line, "delete", conditional)
        if first == "TRUNCATE":
            return self.ddl_statement(text_, line, "truncate", conditional)
        if first in _DDL:
            if first in {"ALTER", "DROP"}:
                return self.ddl_statement(text_, line, first.lower(), conditional)
            return done(first.lower(), IGNORED, "definition change, no data flow")
        if first == "DECLARE":
            return self.declare_statement(text_, line, conditional)
        if first == "SET":
            return self.set_statement(text_, line, conditional)
        if first == "ASSERT":
            return done("assert", IGNORED, "a check, not data flow")
        if first == "LOAD":
            return done("load_data", IGNORED, "loads files")
        if first == "EXPORT":
            return self.export_statement(text_, line, conditional)
        if first in _TRANSACTION or (first == "BEGIN" and len(words) > 1):
            self.transaction(first, conditional)
            return done("transaction", IGNORED, "transaction control")
        if first in _FLOW:
            return done("flow", IGNORED, "control flow")
        if first == "CALL":
            return self.call_statement(text_, line, conditional, top_level)
        if first == "EXECUTE":
            return self.execute_statement(text_, line, conditional, top_level)
        return self.degrade(done("other", UNKNOWN, "statement not recognised"), text_)

    def transaction(self, first: str, conditional: bool) -> None:
        """BEGIN keeps the temporary tables as they are; ROLLBACK brings them back, dropping what the transaction changed."""

        if first in {"BEGIN", "START"}:
            self.transactions.append(self.temp_snapshot())
            return
        if not self.transactions:
            return
        snapshot = self.transactions.pop()
        if first != "ROLLBACK" or conditional:
            return  # committed, or rolled back on a path that may not run: the changes may stay
        kept = {id(s) for temp in snapshot[0].values() for s in snapshot[1][id(temp)][1]}
        for temp in self.temps.values():
            for statement in temp.statements:
                if id(statement) not in kept:
                    statement.complete = True  # undone: nothing it wrote is left to trace
        self.restore_temps(snapshot)

    def ddl_statement(self, text_: str, line: int, kind: str, conditional: bool) -> Statement:
        """Table DDL writes its targets; a rename also reads the original table."""

        tree = self.parse(text_)
        valid = {"alter": exp.Alter, "drop": exp.Drop, "truncate": exp.TruncateTable}[kind]
        index = len(self.a.statements)
        if not isinstance(tree, valid):
            return self.record(Statement(index, line, kind, UNKNOWN, "could not be parsed", conditional))
        if kind != "truncate" and str(tree.args.get("kind") or "").upper() not in {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
            return self.record(Statement(index, line, kind, IGNORED, "definition change outside tables", conditional))
        targets = tree.expressions if kind == "truncate" else tree.args.get("tables") or [tree.this]
        if not targets or any(not isinstance(table, exp.Table) or not table.name for table in targets):
            return self.record(Statement(index, line, kind, UNKNOWN, "target could not be read", conditional))
        statement = self.record(Statement(index, line, kind, KEPT, "table definition or rows changed", conditional))
        statement.complete = True  # no output column values are assigned
        for target in targets:
            temporary = _is_temp_name(target) and target.name.casefold() in self.temps
            if temporary:
                if kind == "drop" and not conditional:
                    self.temps.pop(target.name.casefold(), None)
                continue
            rename = next((a for a in tree.args.get("actions") or [] if isinstance(a, exp.AlterRename)), None)
            sources = {}
            if rename is not None:
                destination = rename.this
                if not isinstance(destination, exp.Table) or not destination.name:
                    statement.disposition = UNKNOWN
                    statement.reason = "rename target could not be read"
                    continue
                sources = {_norm(_table_ref(target)): _clean(target)}
                self.a.reads.update(sources)
                statement.reads.update(sources)
                destination = destination.copy()
                for part in ("db", "catalog"):
                    if not destination.args.get(part) and target.args.get(part):
                        destination.set(part, target.args[part].copy())
                target = destination
            self.write(target, sources, kind, conditional)
        return statement

    # -- queries and DDL
    def parse(self, text_: str) -> exp.Expression | None:
        """The statement as a tree, with every call of a table function this project defines replaced by its query."""

        tree = _parse_one(text_)
        if tree is not None and (self.default_dataset or self.default_project):
            self.qualify_defaults(tree, text_)
        return self.expand_functions(tree) if tree is not None and self.functions else tree

    def qualify_defaults(self, tree: exp.Expression, text_: str) -> None:
        """After ``SET @@dataset_id``, an unqualified name means a table of that dataset (not a temporary table or CTE)."""

        temp_target = None
        if isinstance(tree, exp.Create) and re.match(r"\s*CREATE\s+(OR\s+REPLACE\s+)?TEMP(ORARY)?\b", text_, re.I):
            temp_target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        for table in tree.find_all(exp.Table):
            if table is temp_target or not table.name or not isinstance(table.this, exp.Identifier) or table.catalog:
                continue
            if not table.db:
                if binding_cte(table) is not None or table.name.casefold() in self.temps or not self.default_dataset:
                    continue
                table.set("db", exp.to_identifier(self.default_dataset))
            elif table.db.casefold() == "_session":
                continue
            if self.default_project:
                table.set("catalog", exp.to_identifier(self.default_project))

    def expand_functions(self, tree: exp.Expression) -> exp.Expression:
        for table in list(tree.find_all(exp.Table)):
            call = table.this
            if not isinstance(call, exp.Func):
                continue
            name = ".".join(part for part in (table.catalog, table.db, call.name) if part)
            definition = self.functions.get(_norm(name)) or self.functions.get(_norm(name).split(".")[-1])
            if definition is None:
                continue
            query = _inline_table_function(definition, call)
            if query is None:
                continue
            replacement = exp.Subquery(this=query, alias=table.args.get("alias"))
            for key in ("joins", "laterals"):
                if table.args.get(key):
                    replacement.set(key, table.args.get(key))
            if table is tree:
                return replacement
            table.replace(replacement)
        return tree

    def rewritten(self, tree: exp.Expression, temps: list[_Temp]) -> exp.Expression:
        """A copy of ``tree`` reading each temporary table under the name of the version that is current."""

        if not temps:
            return tree
        copy = tree.copy()
        by_name = {t.name.casefold(): t for t in temps}
        for table in list(copy.find_all(exp.Table)):
            if binding_cte(table) is not None:
                continue
            version = by_name.get(table.name.casefold()) if _is_temp_name(table) else None
            if version is not None and (version.alias != table.name or table.db):
                table.set("this", exp.to_identifier(version.alias))
                table.set("db", None)  # ``_SESSION.t`` is the temporary table ``t``
        return copy

    def degrade(self, statement: Statement, text_: str) -> Statement:
        """A statement that did not parse: its tables still become graph edges (from its tokens), its columns stay unknown."""

        statement.error = _parse_error_line(text_)
        reads, writes = token_reads(text_)
        if not reads and not writes:
            return statement
        statement.degraded = True
        sources: dict[str, exp.Table] = {}
        for table in reads:
            key = _norm(_table_ref(table))
            if _is_temp_name(table) and table.name.casefold() in self.temps:
                sources.update(self.temps[table.name.casefold()].sources)
                continue
            sources[key] = _clean(table)
        for key, table in sources.items():
            self.a.reads.setdefault(key, table)
        statement.reads.update(sources)
        for table in writes:
            if _is_temp_name(table):
                continue
            self.write(table, {k: v for k, v in sources.items() if k != _norm(_table_ref(table))}, "opaque", statement.conditional)
        return statement

    def query_statement(self, text_: str, line: int, conditional: bool, top_level: bool, *, kind: str) -> Statement:
        index = len(self.a.statements)
        tree = self.parse(text_)
        for cls, dml_kind in ((exp.Insert, "insert"), (exp.Update, "update"), (exp.Delete, "delete"), (exp.Merge, "merge")):
            if isinstance(tree, cls):
                return self.dml_statement(text_, line, dml_kind, conditional)
        query = _query_of(tree) if tree is not None else None
        if tree is None or query is None:
            return self.degrade(self.record(Statement(index, line, kind, UNKNOWN, "could not be parsed", conditional)), text_)
        statement = self.record(Statement(index, line, kind, KEPT, "", conditional))
        self.after_query(statement, tree, query, None, conditional, top_level)
        return statement

    def after_query(
        self,
        statement: Statement,
        tree: exp.Expression,
        query: exp.Query,
        target: exp.Table | None,
        conditional: bool,
        top_level: bool,
    ) -> "_Output":
        sources, temps, _late = self.reads_of(tree, [target])
        via = self.variable_sources(tree)
        for key, table in sources.items():
            self.a.reads.setdefault(key, table)
        statement.reads.update(sources)
        for key, table in via.items():
            if key not in sources:
                self.a.variable_reads.setdefault(key, table)
            self.a.side_reads.setdefault(key, table)
        merged = {**sources, **via}
        node = self.rewritten(tree, temps)
        out_query = _query_of(node)
        output = _Output(
            statement, node, out_query if out_query is not None else query, tuple(temps), merged, target, conditional, top_level and not self.nested
        )
        self.a._outputs.append(output)
        return output

    def create_statement(self, text_: str, line: int, words: list[str], conditional: bool, top_level: bool) -> Statement:
        index = len(self.a.statements)
        head = " ".join(w for w in words[:6] if w)
        if re.match(r"CREATE (OR REPLACE )?(TEMP |TEMPORARY )?(AGGREGATE )?(TABLE )?(FUNCTION|PROCEDURE)", head):
            if _TABLE_FUNCTION.match(text_):
                definition = _table_function(text_)
                body = definition.body if definition is not None else None
                tree = body
                params = {name.casefold() for name, is_table, _cols in definition.params if is_table} if definition is not None else set()
            else:
                tree = _parse_one(text_) if "FUNCTION" in head else None
                body = tree.args.get("expression") if isinstance(tree, exp.Create) else None  # not the routine's own name
                params = set()
            found = {k: v for k, v in self.reads_of(body)[0].items() if k not in params} if body is not None else {}
            if not found:
                statement = Statement(index, line, "create_function", IGNORED, "defines a routine; read when called", conditional)
                statement.definition = True
                return self.record(statement)
            for key, table in found.items():
                self.a.reads.setdefault(key, table)
                self.a.definition_reads.setdefault(key, table)
                self.a.side_reads.setdefault(key, table)
            routine = _routine_name(text_)
            if routine and found:
                self.function_reads[routine] = found
            statement = Statement(index, line, "create_function", KEPT, "reads only: the tables its body reads when called", conditional)
            statement.definition = True
            return self.record(statement)
        if not re.match(r"CREATE (OR REPLACE )?(TEMP |TEMPORARY )?(EXTERNAL |MATERIALIZED |SNAPSHOT )?(TABLE|VIEW|MODEL)", head):
            return self.record(Statement(index, line, "ddl", IGNORED, "definition change, no data flow", conditional))
        tree = self.parse(text_)
        target = None
        if isinstance(tree, exp.Create):
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if not isinstance(target, exp.Table) or not target.name:
            return self.degrade(self.record(Statement(index, line, "create_table", UNKNOWN, "could not be parsed", conditional)), text_)
        words_set = set(words[:6])
        properties = tree.args.get("properties")
        temp = bool(words_set & {"TEMP", "TEMPORARY"}) or (
            bool(properties) and any(isinstance(p, exp.TemporaryProperty) for p in properties.expressions)
        )
        temp = temp and _is_temp_name(target)  # ``CREATE TEMP TABLE _SESSION.t`` is temporary too
        kind = "create_view" if "VIEW" in words_set else "create_model" if "MODEL" in words_set else "create_table"
        columns = None
        if isinstance(tree.this, exp.Schema):
            columns = tuple(
                (item.this if isinstance(item, exp.ColumnDef) else item).name
                for item in tree.this.expressions
                if getattr(item.this if isinstance(item, exp.ColumnDef) else item, "name", "")
            ) or None
        query = _query_of(tree)
        clone = tree.args.get("clone")
        like = tree.find(exp.LikeProperty)
        copied = clone.this if isinstance(clone, exp.Clone) else like.this if like is not None else None
        if query is None and isinstance(copied, exp.Table):
            statement = self.record(Statement(index, line, "clone" if clone is not None else "like", KEPT, "", conditional))
            sources, _temps, _late = self.reads_of(copied)
            self.write_or_temp(target, sources, "clone", conditional, temp, columns, None, statement)
            return statement
        if query is None:
            statement = self.record(Statement(index, line, kind, KEPT, "no source query", conditional))
            if temp:
                self.define_temp(target, {}, None, columns, conditional, [statement])
            else:
                self.write(target, {}, kind, conditional)
            return statement
        statement = self.record(Statement(index, line, kind, KEPT, "", conditional))
        output = self.after_query(statement, tree, query, target, conditional, top_level and not temp)
        self.write_or_temp(target, output.sources, kind, conditional, temp, columns, output, statement)
        return statement

    def write_or_temp(self, target, sources, kind, conditional, temp, columns, output, statement) -> None:
        if temp:
            self.define_temp(target, sources, output, columns, conditional, [statement])
            return
        for key, table in sources.items():
            self.a.reads.setdefault(key, table)
        if output is None:
            statement.reads.update(sources)
        self.write(target, sources, kind, conditional)

    def write(self, target: exp.Table, sources: dict[str, exp.Table], kind: str, conditional: bool) -> None:
        """A real table is written: from its sources, and from whatever decides whether the statement runs at all."""

        merged = {**sources, **{k: t for k, t in self.guarded().items() if k != _norm(_table_ref(target))}}
        self.a.writes.append(ScriptWrite(_clean(target), tuple(merged.values()), kind, conditional))

    # -- temporary tables
    def define_temp(
        self,
        target: exp.Table,
        sources: dict[str, exp.Table],
        output: "_Output | None",
        columns: tuple[str, ...] | None,
        conditional: bool,
        statements: list[Statement],
    ) -> None:
        name = target.name
        key = name.casefold()
        self.a.temp_names.add(key)
        self.sequence += 1
        version = self.versions.get(key, 0) + 1
        self.versions[key] = version
        alias = name if version == 1 and key not in self.used_elsewhere() else f"{name}__v{version}"
        temp = _Temp(name, alias, {**sources, **self.guarded()}, sequence=self.sequence, statements=list(statements))
        previous = self.temps.get(key)
        if output is not None:
            query = output.query
            temp.deps = tuple(output.temps)
            if columns:
                query = _alias_to(query, columns)
                temp.columns = columns
            else:
                temp.columns = _output_names(query)
            temp.query = query.copy() if query is not None else None
            if temp.query is None:
                temp.opaque_reason = "its columns do not line up with the declared ones"
        else:
            temp.columns = columns
            temp.empty = True
        if previous is not None and conditional:
            temp.sources = {**previous.sources, **temp.sources}
            temp.query = None
            temp.empty = False
            temp.opaque_reason = "defined on more than one path"
            temp.statements = [*previous.statements, *temp.statements]
        self.temps[key] = temp

    def used_elsewhere(self) -> set[str]:
        """Names a version alias must not take: any word of the script that is not a temporary table's own name."""

        return self.used_names - {t.casefold() for t in self.a.temp_names}

    def temp_dml(
        self,
        target: exp.Table,
        kind: str,
        output: "_Output | None",
        insert_columns: tuple[str, ...] | None,
        sources: dict[str, exp.Table],
        statement: Statement,
    ) -> None:
        """INSERT, UPDATE, MERGE or DELETE on a temporary table that exists."""

        key = target.name.casefold()
        previous = self.temps[key]
        sources = {**sources, **self.guarded()}
        if kind == "delete":  # removes rows: the columns still come from the same query
            previous.sources.update({k: t for k, t in sources.items() if k not in previous.sources})
            previous.statements.append(statement)
            return
        # Later statements see the changed table; statements that already read it keep the version they read.
        self.sequence += 1
        version = self.versions.get(key, 0) + 1
        self.versions[key] = version
        temp = _Temp(
            previous.name,
            f"{previous.name}__v{version}",
            {**previous.sources, **{k: t for k, t in sources.items() if k not in previous.sources}},
            previous.query,
            previous.columns,
            previous.deps,
            previous.opaque_reason,
            self.sequence,
            [*previous.statements, statement],
            previous.empty,
        )
        self.temps[key] = temp
        if kind == "insert" and output is not None and temp.columns and not temp.opaque_reason:
            arm = _arm(output.query, temp.columns, insert_columns)
            if arm is not None:
                if temp.empty or temp.query is None:
                    temp.query = arm.copy()
                else:
                    temp.query = exp.union(exp.Subquery(this=temp.query.copy()), exp.Subquery(this=arm.copy()), distinct=False)
                temp.empty = False
                temp.deps = tuple(dict.fromkeys([*previous.deps, *output.temps]))
                return
        temp.query = None
        temp.empty = False
        temp.opaque_reason = f"changed by {kind} in a way it could not follow"

    def dml_statement(self, text_: str, line: int, kind: str, conditional: bool) -> Statement:
        index = len(self.a.statements)
        tree = self.parse(text_)
        valid = {"insert": exp.Insert, "merge": exp.Merge, "update": exp.Update, "delete": exp.Delete}[kind]
        if not isinstance(tree, valid):
            return self.degrade(self.record(Statement(index, line, kind, UNKNOWN, "could not be parsed", conditional)), text_)
        target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
        if not isinstance(target, exp.Table) and kind == "delete":
            # BigQuery permits DELETE without FROM; sqlglot stores that target in tables.
            targets = tree.args.get("tables") or []
            if len(targets) == 1:
                target = targets[0]
        if not isinstance(target, exp.Table) or not target.name:
            return self.record(Statement(index, line, kind, UNKNOWN, "target could not be read", conditional))
        statement = self.record(Statement(index, line, kind, KEPT, "", conditional))
        statement.complete = kind == "delete" or (kind == "merge" and _only_deletes(tree))
        into_temp = _is_temp_name(target) and target.name.casefold() in self.temps
        query = _query_of(tree) if kind == "insert" else None
        output = None
        if query is not None:
            output = self.after_query(statement, tree, query, target, conditional, not into_temp)
            sources = dict(output.sources)
        else:
            sources, temps, _late = self.reads_of(tree, [target])
            via = self.variable_sources(tree)
            for key, table in sources.items():
                self.a.reads.setdefault(key, table)
            statement.reads.update(sources)
            for key, table in via.items():
                if key not in sources:
                    self.a.variable_reads.setdefault(key, table)
                self.a.side_reads.setdefault(key, table)
            sources = {**sources, **via}
            if kind in ("merge", "insert") and not into_temp:
                node = self.rewritten(tree, temps)
                if kind == "merge":
                    built = merge_query(node) if isinstance(node, exp.Merge) else None
                else:
                    built = insert_values_query(node) if isinstance(node, exp.Insert) else None
                if built is not None:
                    self.a._outputs.append(_Output(statement, node, built, tuple(temps), sources, target, conditional, not self.nested))
        if into_temp:
            insert_columns = None
            if kind == "insert" and isinstance(tree.this, exp.Schema):
                insert_columns = tuple(c.name for c in tree.this.expressions)
            self.temp_dml(target, kind, output, insert_columns, sources, statement)
            return statement
        sources.pop(_norm(_table_ref(target)), None)
        self.write(target, sources, kind, conditional)
        return statement

    # -- variables
    def declare_statement(self, text_: str, line: int, conditional: bool) -> Statement:
        toks = lex(text_)
        names: list[str] = []
        j = 1
        while j < len(toks) and toks[j].kind in {"w", "p"} and not (toks[j].kind == "w" and toks[j].up in {"DEFAULT"}):
            if toks[j].kind == "w":
                names.append(toks[j].text)
                j += 1
                if j < len(toks) and toks[j].text == ",":
                    j += 1
                    continue
                break
            j += 1
        default = next((k for k, t in enumerate(toks) if t.kind == "w" and t.up == "DEFAULT"), None)
        statement = self.record(Statement(len(self.a.statements), line, "declare", IGNORED, "scalar variable", conditional))
        if default is not None:
            expression = text_[toks[default + 1].start :] if default + 1 < len(toks) else ""
            sources, ok = self.value_sources(expression)  # a default can name a variable declared before it
            self.mark_unreadable(statement, ok)
            self.declare_variables(names, sources)
        else:
            self.declare_variables(names, {})
        return statement

    def mark_unreadable(self, statement: Statement, ok: bool) -> None:
        if not ok:
            statement.disposition, statement.reason = UNKNOWN, "value could not be read"

    def set_statement(self, text_: str, line: int, conditional: bool) -> Statement:
        statement = self.record(Statement(len(self.a.statements), line, "set", IGNORED, "scalar variable", conditional))
        match = re.match(r"\s*SET\s+(\([^)]*\)|[\w.@`]+)\s*=\s*(.*)$", text_, re.I | re.S)
        if not match:
            return statement
        target, expression = match.group(1), match.group(2)
        names = [n.strip().strip("()") for n in target.split(",")] if target.startswith("(") else [target]
        sources, ok = self.value_sources(expression)
        self.mark_unreadable(statement, ok)
        for name in names:
            name = name.split(".")[0].strip().casefold()
            if name.startswith("@@"):
                self.set_system_variable(statement, name, expression, conditional)
                continue
            self.assign(name, sources, conditional)
        return statement

    def set_system_variable(self, statement: Statement, name: str, expression: str, conditional: bool) -> None:
        """``SET @@dataset_id`` and ``@@dataset_project_id`` move where later unqualified table names point."""

        if name not in {"@@dataset_id", "@@dataset_project_id"}:
            return
        toks = lex(expression)
        value = string_value(toks[0].text) if len(toks) == 1 and toks[0].kind == "s" else None
        if value is None or conditional or self.nested or not re.fullmatch(r"[\w\-]+", value):
            statement.disposition, statement.reason = UNKNOWN, "changes the default dataset to a value that is not known here"
            return
        if name == "@@dataset_id":
            self.default_dataset = value
        else:
            self.default_project = value

    def variable_sources_of_text(self, expression: str) -> list[tuple[str, exp.Table]]:
        tree = _parse_one(expression) if re.match(r"\s*(SELECT|WITH)\b", expression, re.I) else _parse_one("SELECT " + expression)
        if tree is None:
            return []
        return list(self.variable_sources(tree).items())

    # -- other statements
    def export_statement(self, text_: str, line: int, conditional: bool) -> Statement:
        index = len(self.a.statements)
        match = re.search(r"\bAS\b\s*(.*)$", text_, re.I | re.S)
        tree = _parse_one(match.group(1)) if match else None
        query = _query_of(tree) if tree is not None else None
        if query is None:
            return self.degrade(self.record(Statement(index, line, "export_data", UNKNOWN, "query could not be read", conditional)), text_)
        statement = self.record(Statement(index, line, "export_data", KEPT, "reads only: nothing is written to a table", conditional))
        sources, _temps, _late = self.reads_of(tree)
        for key, table in sources.items():
            self.a.reads.setdefault(key, table)
        statement.reads.update(sources)
        return statement

    def call_statement(self, text_: str, line: int, conditional: bool, top_level: bool) -> Statement:
        index = len(self.a.statements)
        match = re.match(r"\s*CALL\s+((?:`[^`]+`|[\w.\-]+)+)\s*\((.*)\)\s*$", text_, re.I | re.S)
        if not match:
            return self.record(Statement(index, line, "call", UNKNOWN, "could not be read", conditional))
        name = match.group(1).replace("`", "")
        procedure = self.known_procedure(name)
        if procedure is None:
            return self.record(Statement(index, line, "call", UNKNOWN, "procedure is not defined in the project", conditional))
        if _norm(procedure.name) in self.call_stack or len(self.call_stack) >= MAX_PROCEDURE_DEPTH:
            return self.record(Statement(index, line, "call", UNKNOWN, "recursive or too deeply nested", conditional))
        statement = self.record(Statement(index, line, "call", KEPT, "expanded from its definition", conditional))
        arguments = _split_arguments(match.group(2))
        modes = procedure.modes or ("IN",) * len(procedure.params)
        # The body runs in its own scope: parameters hide the caller's names, and nothing it sets leaks back except
        # through OUT and INOUT parameters.
        caller, caller_scopes = self.variables, self.scopes
        self.variables = {name: _Variable(dict(var.sources)) for name, var in caller.items()}
        self.scopes = [{}]
        for position, param in enumerate(procedure.params):
            sources: dict[str, exp.Table] = {}
            if position < len(arguments) and modes[position] != "OUT":
                sources, _ok = self.value_sources(arguments[position])
            self.variables[param.casefold()] = _Variable({**sources, **self.guarded()})
        self.call_stack.append(_norm(procedure.name))
        previous_nested, self.nested = self.nested, self.nested or "procedure"
        previous_anchor, self.anchor_line = self.anchor_line, self.anchor_line or line
        returned: dict[str, dict[str, exp.Table]] = {}
        try:
            before_unknown = len(self.a.unknown)
            self.nodes_in_text(procedure.body, procedure.text, conditional=conditional)
            if len(self.a.unknown) > before_unknown:
                statement.reason = "expanded from its definition; some of it could not be read"
            for position, param in enumerate(procedure.params):
                if modes[position] in {"OUT", "INOUT"} and position < len(arguments):
                    var = self.variables.get(param.casefold())
                    returned[arguments[position]] = dict(var.sources) if var is not None else {}
        finally:
            self.nested, self.anchor_line = previous_nested, previous_anchor
            self.call_stack.pop()
            self.variables, self.scopes = caller, caller_scopes
        for argument, sources in returned.items():
            name = argument.strip().strip("`")
            if re.fullmatch(r"\w+", name):
                self.assign(name, sources, conditional)
            else:
                statement.disposition, statement.reason = UNKNOWN, "an OUT argument is not a variable"
        return statement

    def nodes_in_text(self, nodes: list[Node], text: str, *, conditional: bool) -> None:
        saved = self.breaks
        self.breaks = _line_index(text)
        try:
            self.nodes(nodes, text, conditional=conditional, top_level=False)
        finally:
            self.breaks = saved

    def execute_statement(self, text_: str, line: int, conditional: bool, top_level: bool) -> Statement:
        index = len(self.a.statements)
        toks = lex(text_)
        if len(toks) < 3 or toks[1].up != "IMMEDIATE":
            return self.record(Statement(index, line, "execute", UNKNOWN, "statement not recognised", conditional))
        body: list[Tok] = []
        starts: list[tuple[str, int, int]] = []  # INTO / USING: where the keyword ends and the clause starts
        depth = 0
        for t in toks[2:]:
            if t.kind == "p" and t.text in "([":
                depth += 1
            elif t.kind == "p" and t.text in ")]":
                depth -= 1
            if depth == 0 and t.kind == "w" and t.up in {"INTO", "USING"}:
                starts.append((t.up, t.start, t.end))
            elif not starts:
                body.append(t)
        clauses = {
            word: text_[end : starts[i + 1][1] if i + 1 < len(starts) else len(text_)].strip()
            for i, (word, _start, end) in enumerate(starts)
        }
        inner = _literal_text(body)
        if inner is None:
            for variable in _split_arguments(clauses.get("INTO", "")):
                if variable:
                    self.assign(variable.strip("`"), {}, conditional=True)  # what dynamic text puts in it is unknown
            return self.record(Statement(index, line, "execute_immediate", UNKNOWN, "dynamic SQL text", conditional))
        if self._depth >= MAX_NESTED_SQL_DEPTH:
            return self.record(Statement(index, line, "execute_immediate", UNKNOWN, "nested too deeply to read", conditional))
        statement = self.record(Statement(index, line, "execute_immediate", KEPT, "literal text, read as a script", conditional))
        # USING binds values to the text's @name and ? parameters; the text cannot see script variables otherwise.
        parameters: dict[str, dict[str, exp.Table]] = {"": {}}
        for argument in _split_arguments(clauses.get("USING", "")):
            if not argument:
                continue
            named = re.match(r"(.*?)\s+AS\s+`?(\w+)`?\s*$", argument, re.I | re.S)
            value, name = (named.group(1), named.group(2)) if named else (argument, "")
            sources, ok = self.value_sources(value)
            if not ok:
                statement.disposition, statement.reason = UNKNOWN, "a USING value could not be read"
            key = (name or (value.strip() if re.fullmatch(r"\w+", value.strip()) else "")).casefold()
            if key:
                parameters[key] = {**parameters.get(key, {}), **sources}
            parameters[""].update(sources)
        self._depth += 1
        previous_nested, self.nested = self.nested, self.nested or "execute_immediate"
        previous_anchor, self.anchor_line = self.anchor_line, self.anchor_line or line
        previous_parameters, self.parameters = self.parameters, parameters
        into: dict[str, exp.Table] = {}
        try:
            self.nodes_in_text(parse_script(inner), inner, conditional=conditional)
            if clauses.get("INTO"):
                into, ok = self.value_sources(inner)
                if not ok:
                    statement.disposition, statement.reason = UNKNOWN, "the query read INTO variables could not be read"
        finally:
            self.nested, self.anchor_line = previous_nested, previous_anchor
            self.parameters = previous_parameters
            self._depth -= 1
        for variable in _split_arguments(clauses.get("INTO", "")):
            if variable:
                self.assign(variable.strip("`"), into, conditional)
        return statement

    # -- output
    def choose_final(self) -> None:
        try:
            self.pick_final()
        finally:
            for statement in self.a.statements:
                if not statement.traced:
                    for key, table in statement.reads.items():
                        self.a.side_reads.setdefault(key, table)

    def pick_final(self) -> None:
        outputs = [o for o in self.a._outputs if o.top_level and o.statement.disposition == KEPT]
        unconditional = [o for o in outputs if not o.conditional]
        final = (unconditional or outputs or [None])[-1]
        if final is None:
            return
        self.a._final = final
        final.statement.traced = True
        needed: list[_Temp] = []

        def collect(temp: _Temp) -> None:
            if temp in needed:
                return
            for dep in temp.deps:
                collect(dep)
            needed.append(temp)

        for temp in final.temps:
            collect(temp)
        ctes: list[tuple[str, exp.Query]] = []
        for temp in sorted(needed, key=lambda t: t.sequence):
            if temp.query is None:
                self.a.opaque_temps[temp.alias.casefold()] = temp.opaque_reason or "no query"
                continue
            ctes.append((temp.alias, temp.query))
            for statement in temp.statements:
                statement.traced = True
        self.a._ctes = ctes


def _literal_text(tokens: list[Tok]) -> str | None:
    """The text a constant string expression stands for (literals joined by ``||`` or ``CONCAT``), else ``None``."""

    position = 0

    def expression() -> str | None:
        nonlocal position
        value = term()
        while value is not None and position + 1 < len(tokens) and tokens[position].text == "|" and tokens[position + 1].text == "|":
            position += 2
            more = term()
            value = None if more is None else value + more
        return value

    def term() -> str | None:
        nonlocal position
        if position >= len(tokens):
            return None
        t = tokens[position]
        if t.kind == "s":
            position += 1
            return string_value(t.text)
        if t.kind == "p" and t.text == "(":
            position += 1
            value = expression()
            if value is None or position >= len(tokens) or tokens[position].text != ")":
                return None
            position += 1
            return value
        if t.kind == "w" and t.up == "CONCAT" and position + 1 < len(tokens) and tokens[position + 1].text == "(":
            position += 2
            parts: list[str] = []
            while True:
                piece = expression()
                if piece is None:
                    return None
                parts.append(piece)
                if position < len(tokens) and tokens[position].text == ",":
                    position += 1
                    continue
                break
            if position >= len(tokens) or tokens[position].text != ")":
                return None
            position += 1
            return "".join(parts)
        return None

    value = expression()
    return value if value is not None and position == len(tokens) else None


def _output_names(query: exp.Query | None) -> tuple[str, ...] | None:
    if query is None:
        return None
    first = query
    while isinstance(first, exp.SetOperation):
        first = first.left
    first = first.unnest() if isinstance(first, exp.Subquery) else first
    if not isinstance(first, exp.Select):
        return None
    if any(p.is_star or isinstance(p.unalias(), exp.Star) for p in first.expressions):
        return None
    names = tuple(p.alias_or_name for p in first.expressions)
    return names if all(names) else None


def _alias_to(query: exp.Query, columns: tuple[str, ...]) -> exp.Query | None:
    first = query.copy()
    head = first
    while isinstance(head, exp.SetOperation):
        head = head.left
    head = head.unnest() if isinstance(head, exp.Subquery) else head
    if not isinstance(head, exp.Select) or len(head.expressions) != len(columns):
        return None
    if any(p.is_star or isinstance(p.unalias(), exp.Star) for p in head.expressions):
        return None
    head.set("expressions", [exp.alias_(p.unalias(), name, quoted=False) for p, name in zip(head.expressions, columns)])
    return first


def _arm(query: exp.Query, columns: tuple[str, ...], insert_columns: tuple[str, ...] | None) -> exp.Query | None:
    """``query`` named by position to fit ``columns``, or ``None`` when its columns cannot be matched safely."""

    if insert_columns is None:
        return _alias_to(query, columns)
    if not isinstance(query, exp.Select):
        return None
    if len(query.expressions) != len(insert_columns) or any(p.is_star or isinstance(p.unalias(), exp.Star) for p in query.expressions):
        return None
    given = {name.casefold(): projection.unalias() for name, projection in zip(insert_columns, query.expressions)}
    if not set(given) <= {c.casefold() for c in columns}:
        return None
    copy = query.copy()
    copy.set(
        "expressions",
        [exp.alias_(given[c.casefold()].copy() if c.casefold() in given else exp.Null(), c, quoted=False) for c in columns],
    )
    return copy


def _split_arguments(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    start = 0
    toks = lex(text)
    for t in toks:
        if t.kind == "p" and t.text in "([{":
            depth += 1
        elif t.kind == "p" and t.text in ")]}":
            depth -= 1
        elif t.kind == "p" and t.text == "," and depth == 0:
            args.append(text[start : t.start])
            start = t.end
    if toks:
        args.append(text[start:])
    return [a.strip() for a in args]


# ---------------------------------------------------------------------------------- jobs


_TEMP_DATASET = re.compile(r"^_")


def _parts(reference: object) -> tuple[str, str, str]:
    if isinstance(reference, Mapping):
        project = reference.get("projectId") or reference.get("project_id") or reference.get("project") or ""
        dataset = reference.get("datasetId") or reference.get("dataset_id") or reference.get("dataset") or ""
        table = reference.get("tableId") or reference.get("table_id") or reference.get("table") or ""
        return str(project), str(dataset), str(table)
    parts = [p for p in str(reference or "").replace("`", "").split(".") if p]
    parts = [""] * (3 - len(parts)) + parts[-3:]
    return parts[0], parts[1], parts[2]


def is_temporary_reference(reference: object) -> bool:
    """A table in a script's hidden dataset (``_script...``, ``_session``, anonymous query results)."""

    _project, dataset, table = _parts(reference)
    return bool(dataset and _TEMP_DATASET.match(dataset)) or (not dataset and bool(table))


_DML_JOB_TYPES = frozenset({"MERGE", "INSERT", "UPDATE", "DELETE"})


def expand_script_jobs(records: Iterable[Mapping[str, object]]) -> tuple[list[dict], dict]:
    """Job-history records with script jobs reduced to the real tables they read and wrote.

    Each statement of a BigQuery script is its own job (``parent_job_id`` names the script). Statements
    write to temporary tables that later statements read; a record that reads or writes one is replaced
    by what the temporary table was built from, so a final table traces to the real sources. A script's
    parent job (``statement_type`` ``SCRIPT``) with children is dropped, since they carry the facts. A
    script job that has query text but no children is read with :func:`analyse_script`, one record per
    table it writes. Everything else passes through untouched.

    Returns ``(records, summary)``; the summary has counts only.
    """

    rows = [dict(r) for r in records if isinstance(r, Mapping)]
    summary = {"scripts": 0, "child_jobs": 0, "temporary_tables_followed": 0, "parents_dropped": 0, "scripts_read_from_text": 0, "records_added": 0, "unreadable_scripts": 0}
    by_parent: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        parent = row.get("parent_job_id")
        if parent:
            by_parent.setdefault(str(parent), []).append(index)
    drop: set[int] = set()
    out: dict[int, dict] = {}
    for parent, indices in by_parent.items():
        summary["scripts"] += 1
        summary["child_jobs"] += len(indices)
        ordered = sorted(indices, key=lambda i: (str(rows[i].get("creation_time") or ""), i))
        built: dict[str, set[str]] = {}
        for i in ordered:
            row = rows[i]
            refs = row.get("referenced_tables") or row.get("references") or []
            if isinstance(refs, (str, Mapping)):
                refs = [refs]
            expanded: list[object] = []
            seen: set[str] = set()
            for ref in refs:
                key = ".".join(_parts(ref))
                if is_temporary_reference(ref):
                    for source in sorted(built.get(key, ())):
                        if source not in seen:
                            seen.add(source)
                            expanded.append(source)
                elif key not in seen:
                    seen.add(key)
                    expanded.append(ref)
            destination = row.get("destination") or row.get("destination_table")
            if destination and is_temporary_reference(destination):
                built.setdefault(".".join(_parts(destination)), set()).update(".".join(_parts(r)) for r in expanded)
                drop.add(i)
                summary["temporary_tables_followed"] += 1
                continue
            row["referenced_tables"] = expanded
            row.pop("references", None)
            out[i] = row
    parents_with_children = set(by_parent)
    result: list[dict] = []
    for index, row in enumerate(rows):
        if index in drop:
            continue
        job_id = str(row.get("job_id") or "")
        if job_id and job_id in parents_with_children and str(row.get("statement_type") or "").upper() == "SCRIPT":
            summary["parents_dropped"] += 1
            continue
        row = out.get(index, row)
        text = row.get("query") or row.get("query_text")
        has_edges = bool(row.get("referenced_tables") or row.get("references")) and bool(row.get("destination") or row.get("destination_table"))
        statement_type = str(row.get("statement_type") or "").upper()
        if (
            isinstance(text, str)
            and not has_edges
            and not row.get("parent_job_id")
            and statement_type in {"SCRIPT", *_DML_JOB_TYPES}
            and not row.get("query_truncated")
            and job_id not in parents_with_children
        ):
            analysis = analyse_script(text)
            if statement_type in _DML_JOB_TYPES:
                # A MERGE, INSERT, UPDATE or DELETE job names no destination table: the statement text does.
                writes = [w for w in analysis.writes if w.table.name]
                summary["dml_read_from_text"] = summary.get("dml_read_from_text", 0) + 1
                if len(writes) != 1:
                    summary["unreadable_scripts"] += 1
                    result.append(row)
                    continue
                added = dict(row)
                added["destination"] = added["destination_table"] = _table_ref(writes[0].table)
                added["referenced_tables"] = [_table_ref(t) for t in writes[0].sources]
                result.append(added)
                summary["records_added"] += 1
                continue
            summary["scripts_read_from_text"] += 1
            writes = [w for w in analysis.writes if w.table.name]
            if not writes:
                summary["unreadable_scripts"] += 1
                result.append(row)
                continue
            for number, write in enumerate(writes, 1):
                added = dict(row)
                added["job_id"] = f"{job_id}#{number}" if job_id else f"script#{number}"
                added["destination"] = _table_ref(write.table)
                added["destination_table"] = added["destination"]
                added["referenced_tables"] = [_table_ref(t) for t in write.sources]
                added["statement_type"] = "SCRIPT"
                result.append(added)
                summary["records_added"] += 1
            continue
        result.append(row)
    return result, summary
