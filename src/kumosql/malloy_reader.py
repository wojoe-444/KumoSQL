"""A small reader for Malloy semantic models: which warehouse table each source reads and which columns it references.

``read_malloy(text)`` lists the *sources* of one ``.malloy`` file::

    source: sales is duckdb.table('ds.orders') extend {
      primary_key: order_id
      join_one: customers with customer_id
      dimension: week is order_week
      measure: revenue is amount.sum()
      where: status = 'paid'
    }
    source: big_sales is sales extend { where: amount > 100 }

Each source is a dict: ``name``, ``table`` (the string inside ``table('...')``, a ``connection:`` prefix removed; for a source
that extends another it is the table of the one it extends; ``None`` when it cannot be known), ``extends`` (the source it is
built on, or ``None``), ``primary_key`` (the column named by ``primary_key:``, as written), ``columns`` (the sorted names
the source's definitions, filters, keys and joins refer to, including those of the sources it extends) and ``joins`` (one dict
per join: ``name``, ``kind``, ``table`` or ``source`` it joins, and the ``columns`` read from it).

The reader is an approximation, not a Malloy compiler. It does not know which names are columns: ``columns`` holds every name an
expression uses that the source does not define itself (a name defined by a dimension or measure is left out unless its own
definition reads a column of the same name), so a caller intersects it with the columns of the table it knows. Names after a
dot (``amount.sum()``, ``customers.name``), function calls, keywords and strings are not columns. Joined tables' columns come from
``joins``.

Whatever the reader cannot read is never skipped silently. The returned list has an ``unknown`` attribute: one
``{"what": ..., "reason": ...}`` per problem (a source built from ``sql(...)`` or a query, a parent source that is not in the
file, a string or brace that never closes, an inline ``table()`` query). A source whose table could not be determined has
``table: None``. ``imports`` lists the files the text imports. The reader never raises on any text.
"""

from __future__ import annotations

import re
from typing import Mapping

_STATEMENTS = {"source", "query", "run", "import", "explore"}
_BLOCK_KEYWORDS = {
    "primary_key", "dimension", "measure", "join_one", "join_many", "join_cross", "where", "view", "rename", "accept", "except",
    "timezone", "declare", "query", "index",
}
#: words an expression uses that are never a column
_WORDS = {
    "is", "extend", "include", "refine", "on", "with", "and", "or", "not", "true", "false", "null", "pick", "when", "else", "then", "for",
    "to", "in", "like", "desc", "asc", "ungrouped", "all", "exclude", "now", "from", "number", "string", "date", "timestamp", "boolean",
    "year", "quarter", "month", "week", "day", "hour", "minute", "second", "private", "public", "internal", "table", "sql", "duckdb",
    "bigquery", "postgres", "snowflake", "mysql", "trino", "presto", "motherduck", "by", "that", "this", "is_null",
}
_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*|--[^\n]*|\#[^\n]*|/\*.*?\*/)
  | (?P<tstring>'''.*?'''|\"\"\".*?\"\"\")
  | (?P<string>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")
  | (?P<quoted>`[^`\n]*`)
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<punct>.)
    """,
    re.VERBOSE | re.DOTALL,
)


class MalloySources(list):
    """The sources of a file (a list of dicts) with what could not be read in ``unknown`` and the file's ``imports``."""

    def __init__(self) -> None:
        super().__init__()
        self.unknown: list[dict] = []
        self.imports: list[str] = []


class _Token:
    __slots__ = ("kind", "text", "line")

    def __init__(self, kind: str, text: str, line: int) -> None:
        self.kind, self.text, self.line = kind, text, line

    def is_(self, text: str) -> bool:
        return self.kind in ("punct", "word") and self.text == text


def _tokens(text: str, unknown: list[dict]) -> list[_Token]:
    found: list[_Token] = []
    pos, line = 0, 1
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:  # cannot happen: punct matches any character
            break
        kind = match.lastgroup or "punct"
        chunk = match.group()
        if kind in ("ws", "comment", "number"):
            pass
        elif kind == "punct" and chunk in "'\"":
            unknown.append({"what": f"line {line}", "reason": "a string that never closes; the rest of the file was not read"})
            break
        elif kind == "punct" and chunk == "/" and text.startswith("/*", pos):
            unknown.append({"what": f"line {line}", "reason": "a comment that never closes; the rest of the file was not read"})
            break
        elif kind == "tstring":
            found.append(_Token("string", chunk[3:-3], line))
        elif kind == "string":
            found.append(_Token("string", chunk[1:-1], line))
        elif kind == "quoted":
            found.append(_Token("word", chunk[1:-1], line))
        else:
            found.append(_Token(kind, chunk, line))
        line += chunk.count("\n")
        pos = match.end()
    return found


_OPEN, _CLOSE = "([{", ")]}"


def _depths(tokens: list[_Token]) -> list[int] | None:
    """The nesting depth before each token, or ``None`` when the brackets do not balance."""

    depth = 0
    out = []
    for token in tokens:
        if token.kind == "punct" and token.text in _CLOSE:
            depth -= 1
            if depth < 0:
                return None
        out.append(depth)
        if token.kind == "punct" and token.text in _OPEN:
            depth += 1
    return out if depth == 0 else None


def read_malloy(text: str, known: Mapping[str, dict] | None = None) -> MalloySources:
    """The sources of one Malloy text. ``known`` holds sources of other files (by name) that this text may extend."""

    result = MalloySources()
    try:
        _read(str(text), known or {}, result)
    except Exception as exc:  # noqa: BLE001 - an odd file must be reported, not raised
        result.unknown.append({"what": "file", "reason": f"the reader stopped on this file ({type(exc).__name__})"})
    return result


def _read(text: str, known: Mapping[str, dict], result: MalloySources) -> None:
    tokens = _tokens(text, result.unknown)
    depths = _depths(tokens)
    if depths is None:
        result.unknown.append({"what": "file", "reason": "brackets or braces that do not balance; its sources were not read"})
        return
    # top-level statements: ``keyword :`` (or ``import``) at depth 0
    starts = [
        i for i, t in enumerate(tokens)
        if depths[i] == 0 and t.kind == "word" and t.text in _STATEMENTS and (t.text == "import" or (i + 1 < len(tokens) and tokens[i + 1].is_(":")))
        and not (i and tokens[i - 1].is_("."))
    ]
    raw: dict[str, dict] = {}
    order: list[str] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(tokens)
        statement = tokens[start:end]
        keyword = statement[0].text
        if keyword == "import":
            result.imports.extend(t.text for t in statement if t.kind == "string")
        elif keyword == "source" or keyword == "explore":
            _read_source(statement[2:], statement[0].line, raw, order, result)
        elif any(t.is_("table") and i + 1 < len(statement) and statement[i + 1].is_("(") for i, t in enumerate(statement)):
            result.unknown.append({"what": f"line {statement[0].line}", "reason": f"a {keyword}: over an inline table() is not analysed"})
    resolved: dict[str, dict] = {}
    for name in order:
        _resolve(name, raw, known, resolved, result, set())
    for name in order:
        result.append(resolved[name])


def _read_source(tokens: list[_Token], line: int, raw: dict, order: list, result: MalloySources) -> None:
    """``name is <base> [extend { ... }]`` (a statement may define several sources)."""

    if len(tokens) < 3 or tokens[0].kind != "word" or not tokens[1].is_("is"):
        result.unknown.append({"what": f"line {line}", "reason": "a source statement that is not `name is ...`"})
        return
    name = tokens[0].text
    depths = _depths(tokens) or [0] * len(tokens)
    brace = next((i for i, t in enumerate(tokens) if depths[i] == 0 and t.is_("{")), None)
    head = tokens[2 : brace if brace is not None else len(tokens)]
    head = [t for t in head if not (t.kind == "word" and t.text in ("extend", "include", "refine")) and not t.is_("+")]
    entry = {"name": name, "table": None, "extends": None, "primary_key": None, "columns": set(), "joins": [], "defined": set(), "unknown": [], "line": line}
    base = _base(head)
    if base[0] == "table":
        entry["table"] = base[1]
    elif base[0] == "source":
        entry["extends"] = base[1]
    else:
        reason = base[1]
        entry["unknown"].append(reason)
        result.unknown.append({"what": name, "reason": reason})
    if brace is not None:
        _read_block(entry, tokens[brace:], result)
    if name in raw:
        result.unknown.append({"what": name, "reason": "defined twice in the file; the later definition was used"})
    else:
        order.append(name)
    raw[name] = entry


def _base(head: list[_Token]) -> tuple[str, str]:
    """``("table", path)``, ``("source", name)`` or ``("unknown", reason)`` for what a source is built from."""

    for i, token in enumerate(head):
        if token.is_("table") and i + 1 < len(head) and head[i + 1].is_("(") and i + 2 < len(head) and head[i + 2].kind == "string":
            path = head[i + 2].text.strip()
            if path:
                return "table", path.rsplit(":", 1)[-1].strip()
    words = [t.text for t in head if t.kind == "word"]
    if any(t.is_("sql") or t.is_("from") for t in head) or any(t.is_("-") or t.is_(">") for t in head):
        return "unknown", "a source built from sql(...) or a query: the tables it reads are not extracted"
    if head and all(t.kind == "word" or t.is_(".") for t in head) and words:
        return "source", words[-1] if len(words) == 1 else ".".join(words)
    return "unknown", "a source whose definition the reader does not recognise"


def _read_block(entry: dict, block: list[_Token], result: MalloySources) -> None:
    """The statements inside ``extend { ... }``: keys, dimensions, measures, joins, filters, views."""

    inner = block[1:-1] if block and block[-1].is_("}") else block[1:]
    depths = _depths(inner) or [0] * len(inner)
    starts = [
        i for i, t in enumerate(inner)
        if depths[i] == 0 and t.kind == "word" and t.text in _BLOCK_KEYWORDS and i + 1 < len(inner) and inner[i + 1].is_(":")
    ]
    statements = [(inner[s].text, inner[s + 2 : (starts[n + 1] if n + 1 < len(starts) else len(inner))]) for n, s in enumerate(starts)]
    # first pass: the names this source defines, so a use of one is not taken for a column
    definitions: list[tuple[str, str | None, list[_Token]]] = []
    for keyword, body in statements:
        for name, rhs in _split(keyword, body):
            definitions.append((keyword, name, rhs))
            if name and keyword not in ("join_one", "join_many", "join_cross", "where", "primary_key"):
                entry["defined"].add(name)
    joins: dict[str, dict] = {}
    for keyword, name, rhs in definitions:
        if keyword == "primary_key":
            first = next((t.text for t in rhs if t.kind == "word"), None)
            if first:
                entry["primary_key"] = first
                entry["columns"].add(first)
        elif keyword.startswith("join_"):
            joins[name or ""] = _read_join(entry, keyword, name, rhs)
        else:
            words = _references(rhs)
            entry["columns"].update(words)
            if name and name in words:
                entry.setdefault("self_read", set()).add(name)
    for join in joins.values():
        entry["joins"].append(join)
    for keyword, name, rhs in definitions:
        if keyword.startswith("join_") and name:
            entry["defined"].add(name)
    # a name the source defines is not a column, unless its own definition reads a column of that name (``amount is amount``)
    entry["columns"] = {c for c in entry["columns"] if c not in entry["defined"] or c in entry.get("self_read", ())}


def _split(keyword: str, body: list[_Token]) -> list[tuple[str | None, list[_Token]]]:
    """A statement's definitions as ``(name, right-hand side tokens)``; a statement with no ``name is`` is one definition with no name."""

    depths = _depths(body) or [0] * len(body)
    marks = [i for i, t in enumerate(body) if depths[i] == 0 and t.kind == "word" and i + 1 < len(body) and body[i + 1].is_("is") and not (i and body[i - 1].is_("."))]
    if not marks and keyword.startswith("join_"):
        # ``join_one: customers with customer_id`` (a source joined by name), possibly several one to a line
        names = [i for i, t in enumerate(body) if depths[i] == 0 and t.kind == "word" and (i == 0 or _starts_line(body, i))]
        marks = names
        if marks:
            return [(body[m].text, body[m + 1 : (marks[k + 1] if k + 1 < len(marks) else len(body))]) for k, m in enumerate(marks)]
    if not marks:
        return [(None, body)]
    return [(body[m].text, body[m + 2 : (marks[k + 1] if k + 1 < len(marks) else len(body))]) for k, m in enumerate(marks)]


def _starts_line(tokens: list[_Token], i: int) -> bool:
    """Whether token ``i`` begins a new line and the previous token does not continue an expression."""

    if i == 0:
        return True
    prev = tokens[i - 1]
    if tokens[i].line == prev.line:
        return False
    return not (prev.kind == "punct" and prev.text in ",=<>+-*/(&|!") and not (prev.kind == "word" and prev.text in ("on", "with", "and", "or", "not", "is"))


def _references(tokens: list[_Token]) -> list[str]:
    """Names an expression reads: words that are not keywords, not after a dot or ``::``, not called as functions, not labels."""

    found = []
    for i, token in enumerate(tokens):
        if token.kind != "word" or token.text.lower() in _WORDS:
            continue
        before = tokens[i - 1] if i else None
        after = tokens[i + 1] if i + 1 < len(tokens) else None
        if before is not None and before.is_("."):
            continue
        if before is not None and before.is_(":") and i >= 2 and tokens[i - 2].is_(":"):
            continue
        if after is not None and (after.is_("(") or after.is_("is")):
            continue
        if after is not None and after.is_(":") and not (i + 2 < len(tokens) and tokens[i + 2].is_(":")):
            continue  # a label such as ``group_by:``; ``x::date`` reads x
        found.append(token.text)
    return found


def _read_join(entry: dict, keyword: str, name: str | None, rhs: list[_Token]) -> dict:
    join: dict = {"name": name or "", "kind": keyword.removeprefix("join_"), "table": None, "source": None, "columns": []}
    cut = next((i for i, t in enumerate(rhs) if t.is_("on") or t.is_("with")), len(rhs))
    base, condition = rhs[:cut], rhs[cut + 1 :]
    kind, value = _base(base) if base else ("source", name or "")
    if kind == "table":
        join["table"] = value
    elif kind == "source":
        join["source"] = value if base else (name or "")
    else:
        entry["unknown"].append(value)
    joined: set[str] = set()
    for i, token in enumerate(condition):
        if token.kind == "word" and name and token.text == name and i + 2 < len(condition) and condition[i + 1].is_(".") and condition[i + 2].kind == "word":
            joined.add(condition[i + 2].text)
    join["columns"] = sorted(joined)
    own = [w for w in _references(condition) if w != name]
    entry["columns"].update(own)
    return join


def _resolve(name: str, raw: dict, known: Mapping[str, dict], done: dict, result: MalloySources, trail: set[str]) -> dict:
    if name in done:
        return done[name]
    entry = raw[name]
    parent_name = entry["extends"]
    columns = set(entry["columns"])
    defines = set(entry["defined"])
    joins = list(entry["joins"])
    table, primary, reasons = entry["table"], entry["primary_key"], list(entry["unknown"])
    if parent_name:
        parent = None
        if parent_name in raw and parent_name not in trail and parent_name != name:
            parent = _resolve(parent_name, raw, known, done, result, trail | {name})
        elif parent_name in known:
            parent = known[parent_name]
        if parent is None:
            reason = f"extends {parent_name}, which is not defined in the file" if parent_name not in raw else f"extends {parent_name} in a cycle"
            result.unknown.append({"what": name, "reason": reason})
            reasons.append(reason)
        else:
            table = table or parent.get("table")
            primary = primary or parent.get("primary_key")
            inherited = set(parent.get("defines", ()))
            columns = {c for c in columns if c not in inherited or c in entry.get("self_read", ())} | set(parent.get("columns", ()))
            defines |= inherited
            joins = [*parent.get("joins", ()), *joins]
            if parent.get("table") is None:
                reasons.append(f"the table of {parent_name} is unknown")
    # a joined source's key and table come from the file when it defines the source
    out_joins = []
    for join in joins:
        join = dict(join)
        target = join.get("source")
        if target and target in raw and target != name:
            other = _resolve(target, raw, known, done, result, trail | {name})
            join["table"] = join["table"] or other.get("table")
        elif target and target in known:
            join["table"] = join["table"] or known[target].get("table")
        elif target and not join.get("table"):
            result.unknown.append({"what": name, "reason": f"joins {target}, which is not defined in the file, so its table is unknown"})
        out_joins.append(join)
    done[name] = {
        "name": name, "table": table, "extends": parent_name, "primary_key": primary,
        "columns": sorted(columns, key=str.casefold), "joins": out_joins, "unknown": reasons, "defines": sorted(defines),
    }
    return done[name]
