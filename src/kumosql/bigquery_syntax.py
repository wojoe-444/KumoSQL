"""BigQuery syntax that sqlglot's BigQuery parser does not read, added once for every parse in this package.

``DROP TABLE FUNCTION`` is read as dropping a function of kind ``TABLE FUNCTION``. Pipe ``SET`` and ``DROP`` are read as the
``SELECT * REPLACE`` and ``SELECT * EXCEPT`` they stand for. ``GRAPH_TABLE(...)`` and the ``MODEL m`` argument of an ``ML.``
function sqlglot does not know are kept as their own text.

``FROM dataset.fn(TABLE dataset.input, option => value)`` passes a whole table to a table-valued function. sqlglot stops at
``TABLE`` ("Expecting )"), so the model was reported unparseable and everything it read was lost. Here the argument becomes a
:data:`TABLE_ARGUMENT` call holding the table, so the table is read like any other and the SQL prints back unchanged.

``x LIKE ALL UNNEST(array)`` (and ``LIKE SOME``), an aggregate with a ``WHERE`` filter inside its parentheses (``COUNT(* WHERE c)``),
the ``WITH(a AS 1, a + 1)`` expression and ``t.arr elem WITH OFFSET off`` are read the same way, each into nodes sqlglot already
has or a marker call that prints back as written. ``STRUCT<>()``, which sqlglot reads as the comparison ``STRUCT <> ()``, is refused.

It works the same on pure and compiled sqlglot: a compiled build ignores parser methods assigned after the fact and refuses
subclasses of its expressions, so SQL sqlglot rejects is parsed again with the dialect's tokens rewritten and the marker call is resolved after.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.errors import ParseError
from sqlglot.generator import Generator
from sqlglot.tokens import Token, TokenType

from .string_literals import _bytes_literal, _decode_bytes, _string_end


TABLE_ARGUMENT = "__KUMO_TABLE_ARGUMENT__"


VERBATIM = "__KUMO_VERBATIM__"
# Calls whose arguments are a language of their own, kept as their text: ``GRAPH_TABLE(graph MATCH ... RETURN ...)``.
_VERBATIM_CALLS = ("GRAPH_TABLE",)


WITH_EXPRESSION = "__KUMO_WITH__"
WITH_VARIABLE = "__KUMO_WITH_VARIABLE__"
LIKE_ALL = "__KUMO_LIKE_ALL__"


def _generate(self: Generator, expression: exp.Anonymous) -> str:
    if expression.name == WITH_EXPRESSION and len(expression.expressions) >= 2:
        return f"WITH({', '.join(self.sql(item) for item in expression.expressions)})"
    if expression.name == WITH_VARIABLE and len(expression.expressions) == 2 and expression.expressions[0].is_string:
        return f"{expression.expressions[0].this} AS {self.sql(expression.expressions[1])}"
    if expression.name == TABLE_ARGUMENT and len(expression.expressions) == 1:
        return f"TABLE {self.sql(expression.expressions[0])}"
    if expression.name == VERBATIM and len(expression.expressions) == 1 and expression.expressions[0].is_string:
        return expression.expressions[0].this
    return self.anonymous_sql(expression)


def _all_sql(self: Generator, expression: exp.All) -> str:
    """``ALL UNNEST(array)`` as BigQuery spells ``LIKE ALL UNNEST(array)``, without the parentheses sqlglot adds."""

    if isinstance(expression.this, exp.Unnest):
        return f"ALL {self.sql(expression, 'this')}"
    return Generator.all_sql(self, expression)


def _filter_sql(self: Generator, expression: exp.Filter) -> str:
    """``COUNT(x) FILTER (WHERE c)`` as BigQuery's ``COUNT(x WHERE c)``, the filter it allows inside an aggregate call.

    BigQuery rejects the filter inside a window call and next to ``ORDER BY``, ``LIMIT`` or ``HAVING``; those keep the standard form.
    """

    aggregate = expression.this
    where = expression.expression
    if (
        isinstance(where, exp.Where) and isinstance(aggregate, (exp.AggFunc, exp.IgnoreNulls, exp.RespectNulls))
        and not isinstance(expression.parent, exp.Window) and not aggregate.find(exp.Order, exp.Limit, exp.HavingMax)
    ):
        text = self.sql(aggregate)
        if text.endswith(")"):
            return f"{text[:-1]} WHERE {self.sql(where, 'this')})"
    return Generator.filter_sql(self, expression)


_installed = False


def _open_call(before: list) -> int | None:
    """The index of the ``(`` of the innermost call still open at the end of ``before``."""

    depth = 0
    for index in range(len(before) - 1, -1, -1):
        kind = before[index].token_type
        if kind == TokenType.R_PAREN:
            depth += 1
        elif kind == TokenType.L_PAREN:
            if depth == 0:
                return index
            depth -= 1
    return None


def _ml_call(before: list, index: int | None) -> str | None:
    """The function name when the ``(`` at ``index`` opens ``ML.fn(`` or ``AI.fn(``."""

    if index is None or index < 3 or before[index - 2].token_type != TokenType.DOT:
        return None
    if before[index - 3].text.upper() not in ("ML", "AI"):
        return None
    return before[index - 1].text.upper()


def _in_native_call(before: list) -> bool:
    """Inside an ``ML.fn(...)`` or ``AI.fn(...)`` that sqlglot reads itself (``ML.PREDICT``), with its own ``TABLE`` handling."""

    name = _ml_call(before, _open_call(before))
    return name is not None and name in BigQuery.Parser.FUNCTION_PARSERS


def _mark_table_arguments(tokens: list) -> list:
    """``TABLE name`` as an argument becomes ``__KUMO_TABLE_ARGUMENT__(name)``, which every sqlglot build parses as a call."""

    out: list = []
    i = 0
    count = len(tokens)
    while i < count:
        token = tokens[i]
        follows_open = bool(out) and out[-1].token_type in (TokenType.L_PAREN, TokenType.COMMA)
        if follows_open and _in_native_call(out):
            follows_open = False
        if (
            token.token_type == TokenType.TABLE
            and follows_open
            and i + 1 < count
            and tokens[i + 1].token_type not in (TokenType.COMMA, TokenType.R_PAREN)
        ):
            depth = 0
            end = i + 1
            while end < count:
                kind = tokens[end].token_type
                if kind == TokenType.L_PAREN:
                    depth += 1
                elif kind == TokenType.R_PAREN:
                    if depth == 0:
                        break
                    depth -= 1
                elif kind == TokenType.COMMA and depth == 0:
                    break
                end += 1
            if end < count:
                def made(kind, text, source=token):
                    return Token(kind, text, line=source.line, col=source.col, start=source.start, end=source.end)

                out.append(made(TokenType.VAR, TABLE_ARGUMENT))
                out.append(made(TokenType.L_PAREN, "("))
                out.extend(tokens[i + 1 : end])
                out.append(made(TokenType.R_PAREN, ")"))
                i = end
                continue
        out.append(token)
        i += 1
    return out


def _merge_table_function_kind(tokens: list) -> list:
    """``DROP TABLE FUNCTION`` becomes ``DROP`` and one ``FUNCTION`` token spelled ``TABLE FUNCTION``: sqlglot takes the kind
    of a ``DROP`` from that token's text, so it parses as a dropped function of kind ``TABLE FUNCTION`` and prints back."""

    out: list = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token.token_type == TokenType.TABLE
            and out and out[-1].token_type == TokenType.DROP
            and i + 1 < len(tokens) and tokens[i + 1].token_type == TokenType.FUNCTION
        ):
            end = tokens[i + 1]
            out.append(Token(TokenType.FUNCTION, "TABLE FUNCTION", line=token.line, col=token.col, start=token.start, end=end.end))
            i += 2
            continue
        out.append(token)
        i += 1
    return out


# ``RENAME a AS b`` is left out on purpose: it keeps ``b`` in ``a``'s place, which no ``SELECT`` can say without knowing the
# columns, and a translation that moved the column would let a prover equate queries whose columns are in different orders.
_PIPE_OPERATORS = (TokenType.SET, TokenType.DROP)
_PIPE = getattr(TokenType, "PIPE_GT", None)  # sqlglot 26.0.0 has no pipe syntax at all


def _pipe_items(tokens: list, start: int) -> tuple[list[list], int]:
    """The comma-separated items of a pipe operator whose arguments start at ``start``, and the index after them."""

    items: list[list] = [[]]
    depth = 0
    index = start
    while index < len(tokens):
        kind = tokens[index].token_type
        if depth == 0 and kind in (_PIPE, TokenType.SEMICOLON):
            break
        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1
        elif kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            if depth == 0:
                break
            depth -= 1
        if depth == 0 and kind == TokenType.COMMA:
            items.append([])
        else:
            items[-1].append(tokens[index])
        index += 1
    return [item for item in items if item], index


def _pipe_operator_text(sql: str, operator, items: list[list]) -> str | None:
    """``|> SET c = e`` as ``|> SELECT * REPLACE (e AS c)`` and ``|> DROP c`` as ``|> SELECT * EXCEPT (c)``, which BigQuery
    defines them to mean, or None when an item is not plain."""

    def text(tokens: list) -> str:
        return sql[tokens[0].start : tokens[-1].end + 1]

    if not items:
        return None
    if operator == TokenType.DROP:
        if any(len(item) != 1 for item in items):
            return None
        return f"|> SELECT * EXCEPT ({', '.join(text(item) for item in items)})"
    parts = []
    for item in items:
        if len(item) < 3 or item[1].token_type != TokenType.EQ:
            return None
        parts.append(f"{text(item[2:])} AS {text(item[:1])}")
    return f"|> SELECT * REPLACE ({', '.join(parts)})"


def _rewrite_pipe_operators(sql: str, tokens: list) -> str:
    """Pipe ``SET`` and ``DROP``, which sqlglot does not read, written as the pipe ``SELECT`` that means the same."""

    pieces: list[str] = []
    position = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            _PIPE is not None
            and token.token_type == _PIPE
            and index + 1 < len(tokens)
            and tokens[index + 1].token_type in _PIPE_OPERATORS
        ):
            items, after = _pipe_items(tokens, index + 2)
            replacement = _pipe_operator_text(sql, tokens[index + 1].token_type, items)
            if replacement is not None:
                end = tokens[after - 1].end + 1
                pieces += [sql[position : token.start], replacement]
                position = end
                index = after
                continue
        index += 1
    return "".join(pieces) + sql[position:] if pieces else sql


def _rewrite_verbatim_calls(sql: str, tokens: list) -> str:
    """``GRAPH_TABLE(...)`` becomes ``__KUMO_VERBATIM__('<its text>')``, which prints back as the text it holds.

    The rest of the query is read as usual. The call is an unknown function of its own text, so it reads no table sqlglot
    can see and a prover can only equate it with the same text.
    """

    pieces: list[str] = []
    position = 0
    index = 0
    while index + 1 < len(tokens):
        token = tokens[index]
        if token.text.upper() in _VERBATIM_CALLS and tokens[index + 1].token_type == TokenType.L_PAREN:
            depth = 0
            end = index + 1
            while end < len(tokens):
                kind = tokens[end].token_type
                depth += kind == TokenType.L_PAREN
                depth -= kind == TokenType.R_PAREN
                if depth == 0:
                    break
                end += 1
            if end < len(tokens):
                text = sql[token.start : tokens[end].end + 1]
                pieces += [sql[position : token.start], f"{VERBATIM}({_quoted(text)})"]
                position = tokens[end].end + 1
                index = end + 1
                continue
        index += 1
    return "".join(pieces) + sql[position:] if pieces else sql


def _rewrite_model_arguments(sql: str, tokens: list) -> str:
    """``MODEL name`` as an argument of an ``ML.fn`` sqlglot does not read (``ML.EVALUATE``, ``ML.DETECT_ANOMALIES``) becomes
    ``__KUMO_VERBATIM__('MODEL name')``: it prints back as written and, a model not being a table, is no read."""

    pieces: list[str] = []
    position = 0
    for index, token in enumerate(tokens):
        if token.text.upper() != "MODEL" or index == 0 or index + 1 >= len(tokens):
            continue
        if tokens[index - 1].token_type not in (TokenType.L_PAREN, TokenType.COMMA):
            continue
        name = _ml_call(tokens, _open_call(tokens[:index]))
        if name is None or name in BigQuery.Parser.FUNCTION_PARSERS:
            continue
        end = index + 1
        while end + 1 < len(tokens) and tokens[end + 1].token_type not in (TokenType.COMMA, TokenType.R_PAREN):
            end += 1
        text = sql[token.start : tokens[end].end + 1]
        pieces += [sql[position : token.start], f"{VERBATIM}({_quoted(text)})"]
        position = tokens[end].end + 1
    return "".join(pieces) + sql[position:] if pieces else sql


def _rewrite_raw_bytes(sql: str, tokens: list) -> str:
    """``rb'..'`` and ``br'..'``, which sqlglot reads as a name and a string, written as the ``b'..'`` with the same bytes."""

    pieces: list[str] = []
    position = 0
    for prefix, literal in zip(tokens, tokens[1:]):
        if (
            prefix.token_type == TokenType.VAR and prefix.text.lower() in ("rb", "br")
            and literal.token_type == TokenType.STRING and prefix.end + 1 == literal.start
        ):
            end, _, body = _string_end(sql, literal.start)
            if body is not None and end == literal.end + 1:
                pieces += [sql[position : prefix.start], _bytes_literal(_decode_bytes(body, raw=True))]
                position = end
    return "".join(pieces) + sql[position:] if pieces else sql


def _query_start(tokens: list, index: int) -> int:
    """Index of the first token of the query that holds ``tokens[index]``: just after the parenthesis it sits in, or after
    the statement's start."""

    depth = 0
    for i in range(index - 1, -1, -1):
        kind = tokens[i].token_type
        if kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth += 1
        elif kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            if depth == 0:
                return i + 1
            depth -= 1
        elif kind == TokenType.SEMICOLON and depth == 0:
            return i + 1
    return 0


def _rewrite_pipe_with(sql: str, tokens: list) -> str:
    """``FROM t |> WITH y AS (q) |> ...`` written as ``WITH y AS (q) FROM t |> ...``, which sqlglot reads.

    A pipe ``WITH`` names its queries for the operators after it. Moved to the front, a name would also cover the part
    before it, so the move is made only when nothing before its definition spells the name (later definitions and operators
    may), and only for a query that has no ``WITH`` of its own. The innermost query goes first; the reparse moves the rest.
    """

    if _PIPE is None:
        return sql
    found: dict[int, list[int]] = {}
    for index, token in enumerate(tokens[:-1]):
        if token.token_type == _PIPE and tokens[index + 1].token_type == TokenType.WITH:
            found.setdefault(_query_start(tokens, index), []).append(index)
    for start in sorted(found, reverse=True):
        moved = _move_pipe_with(sql, tokens, start, found[start])
        if moved is not None:
            return moved
    return sql


def _move_pipe_with(sql: str, tokens: list, start: int, pipes: list[int]) -> str | None:
    if tokens[start].token_type not in (TokenType.FROM, TokenType.SELECT):
        return None
    definitions: list[str] = []
    cuts: list[tuple[int, int]] = []
    aliases: dict[str, int] = {}  # name -> index of the token that defines it
    for index in pipes:
        items, after = _pipe_items(tokens, index + 2)
        if not items or items[0][0].text.upper() == "RECURSIVE":
            return None
        for item in items:
            if (
                len(item) < 4 or item[0].token_type not in (TokenType.VAR, TokenType.IDENTIFIER)
                or item[1].token_type != TokenType.ALIAS or item[2].token_type != TokenType.L_PAREN
                or item[-1].token_type != TokenType.R_PAREN or item[0].text.upper() in aliases
            ):
                return None
            aliases[item[0].text.upper()] = tokens.index(item[0], start)
            definitions.append(sql[item[0].start : item[-1].end + 1])
        cuts.append((tokens[index].start, tokens[after - 1].end + 1))
    if any(
        token.token_type in (TokenType.VAR, TokenType.IDENTIFIER) and i < aliases.get(token.text.upper(), -1)
        for i, token in enumerate(tokens[start:], start)
    ):
        return None
    head = tokens[start].start
    kept, position = [], head
    for cut_start, cut_end in cuts:
        kept.append(sql[position:cut_start])
        position = cut_end
    return f"{sql[:head]}WITH {', '.join(definitions)} {''.join(kept)}{sql[position:]}"


def _call_groups(tokens: list) -> list[tuple[int, int]]:
    """(open, close) token indexes of every parenthesis pair, innermost first."""

    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.L_PAREN:
            stack.append(index)
        elif token.token_type == TokenType.R_PAREN and stack:
            pairs.append((stack.pop(), index))
    return pairs


def _top_level(tokens: list, start: int, end: int):
    """(index, token) of the tokens between ``start`` and ``end`` that sit outside every nested bracket."""

    depth = 0
    for index in range(start, end):
        kind = tokens[index].token_type
        if kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth -= 1
        if depth == 0:
            yield index, tokens[index]
        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1


def _apply_edits(sql: str, edits: list[tuple[int, int, str]]) -> str:
    """``edits`` are (start, end, text) character ranges, replaced left to right; overlapping ones are dropped."""

    pieces: list[str] = []
    position = 0
    for start, end, text in sorted(edits):
        if start < position:
            continue
        pieces += [sql[position:start], text]
        position = end
    return "".join(pieces) + sql[position:] if pieces else sql


def _rewrite_like_quantifiers(sql: str, tokens: list) -> str:
    """``x LIKE ALL UNNEST(arr)`` becomes ``x LIKE ANY UNNEST(__KUMO_LIKE_ALL__(arr))``, which every build reads; the marker
    turns the ``ANY`` back into ``ALL`` once parsed. ``LIKE SOME`` is ``LIKE ANY``."""

    edits: list[tuple[int, int, str]] = []
    for index in range(len(tokens) - 2):
        if tokens[index].text.upper() not in ("LIKE", "ILIKE"):
            continue
        quantifier, after = tokens[index + 1], tokens[index + 2]
        word = quantifier.text.upper()
        if word == "SOME":
            edits.append((quantifier.start, quantifier.end + 1, "ANY"))
        elif word == "ALL" and after.text.upper() == "UNNEST" and index + 3 < len(tokens) and tokens[index + 3].token_type == TokenType.L_PAREN:
            close = next((close for opened, close in _call_groups(tokens) if opened == index + 3), None)
            if close is not None:
                edits.append((quantifier.start, quantifier.end + 1, "ANY"))
                edits.append((tokens[index + 3].end + 1, tokens[index + 3].end + 1, f"{LIKE_ALL}("))
                edits.append((tokens[close].start, tokens[close].start, ")"))
    return _apply_edits(sql, edits)


_NOT_AGGREGATE_ARGUMENT = (TokenType.SELECT, TokenType.WITH, TokenType.FROM)


def _rewrite_aggregate_where(sql: str, tokens: list) -> str:
    """``COUNT(* WHERE c)`` becomes ``COUNT(*) FILTER (WHERE c)``, the standard filter sqlglot reads as ``exp.Filter``.

    BigQuery allows the filter after the arguments (and ``IGNORE NULLS``) and rejects it next to ``ORDER BY``, ``LIMIT`` or
    ``HAVING``, inside a window call and in a subquery, so those are left alone.
    """

    matches: list[tuple[int, int, int]] = []
    for opened, closed in _call_groups(tokens):
        where = None
        for index, token in _top_level(tokens, opened + 1, closed):
            kind, word = token.token_type, token.text.upper()
            if kind in _NOT_AGGREGATE_ARGUMENT or word in ("ORDER", "LIMIT", "HAVING", "QUALIFY", "GROUP", "UNION"):
                where = None
                break
            if kind == TokenType.WHERE and where is None:
                where = index
        if where is None or where == opened + 1:
            continue
        following = tokens[closed + 1].text.upper() if closed + 1 < len(tokens) else ""
        if following == "OVER" or opened == 0 or tokens[opened - 1].token_type in (
            TokenType.IN, TokenType.EXISTS, TokenType.ALIAS, TokenType.L_PAREN, TokenType.COMMA
        ):
            continue
        matches.append((opened, where, closed))
    edits = []
    for opened, where, closed in matches:
        if any(opened < o < closed for o, _, _ in matches):
            continue  # the inner filter goes first; the next pass takes this one
        condition = sql[tokens[where].end + 1 : tokens[closed].start].strip()
        edits.append((tokens[where].start, tokens[closed].end + 1, f") FILTER (WHERE {condition})"))
    return _apply_edits(sql, edits)


def _rewrite_with_expressions(sql: str, tokens: list) -> str:
    """``WITH(a AS 1, a + 1)`` becomes ``__KUMO_WITH__(__KUMO_WITH_VARIABLE__('a', 1), a + 1)``: sqlglot has no node for it."""

    edits: list[tuple[int, int, str]] = []
    pairs = {opened: closed for opened, closed in _call_groups(tokens)}
    for index in range(1, len(tokens) - 1):
        token = tokens[index]
        if (
            token.token_type != TokenType.WITH or tokens[index + 1].token_type != TokenType.L_PAREN
            or tokens[index - 1].token_type == TokenType.SEMICOLON or index + 1 not in pairs
        ):
            continue
        opened, closed = index + 1, pairs[index + 1]
        items: list[list] = [[]]
        for position, inner in _top_level(tokens, opened + 1, closed):
            if inner.token_type == TokenType.COMMA:
                items.append([])
            else:
                items[-1].append(position)
        if len(items) < 2 or any(not item for item in items):
            continue
        parts: list[str] = []
        for item in items[:-1]:
            name = tokens[item[0]]
            if (
                len(item) < 3 or name.token_type not in (TokenType.VAR, TokenType.IDENTIFIER)
                or tokens[item[1]].token_type != TokenType.ALIAS
            ):
                parts = []
                break
            label = f"`{name.text}`" if name.token_type == TokenType.IDENTIFIER else name.text
            value = sql[tokens[item[2]].start : tokens[item[-1]].end + 1]
            parts.append(f"{WITH_VARIABLE}({_quoted(label)}, {value})")
        if parts:
            parts.append(sql[tokens[items[-1][0]].start : tokens[items[-1][-1]].end + 1])
            edits.append((token.start, tokens[closed].end + 1, f"{WITH_EXPRESSION}({', '.join(parts)})"))
    # An outer expression holds the text of an inner one, so the inner goes first and the next pass takes the outer.
    return _apply_edits(sql, [e for e in edits if not any(e[0] < o[0] < e[1] for o in edits)])


def _rewrite_unnest_offset(sql: str, tokens: list) -> str:
    """``FROM t, t.arr elem WITH OFFSET off`` becomes ``FROM t, UNNEST(t.arr) elem WITH OFFSET off``, which BigQuery defines
    ``t.arr`` in a FROM clause to mean when it is a path into an earlier table."""

    identifiers = (TokenType.VAR, TokenType.IDENTIFIER)
    edits: list[tuple[int, int, str]] = []
    for index in range(1, len(tokens) - 1):
        if tokens[index].token_type != TokenType.WITH or tokens[index + 1].text.upper() != "OFFSET":
            continue
        end = index - 1
        if end >= 1 and tokens[end].token_type in identifiers and tokens[end - 1].token_type == TokenType.ALIAS:
            end -= 2
        elif end >= 1 and tokens[end].token_type in identifiers and tokens[end - 1].token_type in identifiers:
            end -= 1
        start = end
        while start >= 2 and tokens[start - 1].token_type == TokenType.DOT and tokens[start - 2].token_type in identifiers:
            start -= 2
        if (
            end - start < 2 or start < 1 or tokens[end].token_type not in identifiers or tokens[start].token_type not in identifiers
            or not (tokens[start - 1].token_type in (TokenType.FROM, TokenType.COMMA) or tokens[start - 1].text.upper() == "JOIN")
        ):
            continue
        path = sql[tokens[start].start : tokens[end].end + 1]
        edits.append((tokens[start].start, tokens[end].end + 1, f"UNNEST({path})"))
    return _apply_edits(sql, edits)


def _check_empty_struct(sql: str, tokens: list) -> None:
    """Refuse ``STRUCT<>(...)``: sqlglot reads it as the comparison ``STRUCT <> (...)``, and BigQuery rejects an empty struct type."""

    for token, after in zip(tokens, tokens[1:]):
        if token.token_type == TokenType.STRUCT and after.token_type == TokenType.NEQ and after.start == token.end + 1:
            raise ParseError("STRUCT<>() is an empty struct type, which BigQuery rejects; it is not read as a comparison")


def _quoted(text: str) -> str:
    """``text`` as a BigQuery string literal that reads back as exactly ``text``."""

    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return f"'{escaped}'"


def _table_argument(call: exp.Anonymous) -> exp.Expression:
    arguments = call.expressions
    inner = arguments[0] if len(arguments) == 1 else None
    if isinstance(inner, (exp.Column, exp.Dot)):
        try:
            table = exp.to_table(inner.sql("bigquery"), dialect="bigquery")
        except Exception:
            return call
        return exp.Anonymous(this=TABLE_ARGUMENT, expressions=[table])
    return call


def _resolve_table_arguments(trees):
    for tree in trees:
        if tree is None:
            continue
        for call in list(tree.find_all(exp.Anonymous)):
            if call.name == TABLE_ARGUMENT:
                replacement = _table_argument(call)
                if call.parent is None:
                    tree = replacement
                elif replacement is not call:
                    call.replace(replacement)
        yield tree


def _resolve_markers(trees):
    """Turn the marker calls the rewrites left into the nodes they stand for."""

    for tree in trees:
        if tree is not None:
            tree = _resolve_like_all(tree)
            tree = _resolve_with_expressions(tree)
        yield tree


def _resolve_like_all(tree: exp.Expression) -> exp.Expression:
    for quantifier in list(tree.find_all(exp.Any)):
        unnest = quantifier.this
        if (
            isinstance(unnest, exp.Unnest) and len(unnest.expressions) == 1
            and isinstance(unnest.expressions[0], exp.Anonymous) and unnest.expressions[0].name == LIKE_ALL
            and len(unnest.expressions[0].expressions) == 1
        ):
            unnest.expressions[0].replace(unnest.expressions[0].expressions[0])
            replacement = exp.All(this=unnest)
            if quantifier.parent is None:
                return replacement
            quantifier.replace(replacement)
    return tree


def _resolve_with_expressions(tree: exp.Expression) -> exp.Expression:
    """Inside ``WITH(a AS e, ..., result)`` a bare ``a`` is the variable, not a column, so it becomes ``exp.Var``: nothing then
    counts it as a read of a column ``a``. A name also used inside a subquery of the expression is refused, because
    which ``a`` the subquery means is not something sqlglot can tell."""

    for call in list(tree.find_all(exp.Anonymous)):
        if call.name != WITH_EXPRESSION:
            continue
        *definitions, result = call.expressions
        names: list[str] = []
        for definition in definitions:
            raw = definition.expressions[0].this
            name = raw.strip("`").lower()
            if name in names:
                raise ParseError(f"WITH expression defines {raw} twice")
            names.append(name)
        scopes = [(definition.expressions[1], names[:position]) for position, definition in enumerate(definitions)]
        scopes.append((result, names))
        for scope, visible in scopes:
            for column in list(scope.find_all(exp.Column)):
                if column.table or column.name.lower() not in visible:
                    continue
                if column.find_ancestor(exp.Select, exp.Subquery) is not scope.find_ancestor(exp.Select, exp.Subquery):
                    raise ParseError(f"WITH expression variable {column.name} is used inside a subquery")
                raw = next(d.expressions[0].this for d, n in zip(definitions, names) if n == column.name.lower())
                column.replace(exp.Var(this=raw))
    return tree


class TooDeeplyNested(ParseError):
    """The query nests further than the parser can follow on the interpreter's stack."""


class UnclosedLiteral(ParseError):
    """A string, bytes literal or quoted name that is not triple-quoted runs onto another line, which BigQuery rejects."""


_ONE_LINE = tuple(
    kind for kind in (getattr(TokenType, name, None) for name in ("STRING", "BYTE_STRING", "RAW_STRING", "NATIONAL_STRING", "IDENTIFIER"))
    if kind is not None
)


def _check_literals(sql: str, tokens: list) -> None:
    """Raise ``UnclosedLiteral`` for a quoted token that is not triple-quoted and holds a line break.

    sqlglot reads ``'a<line break>b'`` as the string ``a\\nb``; BigQuery stops with "Unclosed string literal". Reading it
    would let a prover equate an invalid query with a valid one.
    """

    if "\n" not in sql and "\r" not in sql:
        return
    for token in tokens:
        if token.token_type in _ONE_LINE:
            text = sql[token.start : token.end + 1].lstrip("rRbB")  # a command's remaining text is a STRING token too
            if text[:1] in ("'", '"', "`") and ("\n" in text or "\r" in text) and not text.startswith(("'''", '"""')):
                raise UnclosedLiteral(f"Unclosed literal: a quoted string or name that is not triple-quoted runs past the end of line {token.line}")


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    # The parser is not touched: a compiled sqlglot (sqlglotc) dispatches parser methods through a table built when the class
    # is defined, ignores a method assigned afterwards and refuses a subclass. SQL it rejects is parsed again with the
    # tokens rewritten, and the marker call is turned into a table argument after, which works the same on every build.
    def parse_with_table_arguments(self, sql, **opts):
        # Whatever sqlglot reads on its own is left alone; only SQL it rejects is retried with the table arguments marked.
        tokens = self.tokenize(sql)
        try:
            _check_literals(sql, tokens)
            _check_empty_struct(sql, tokens)
            return self.parser(**opts).parse(tokens, sql)  # what the dialect's own parse does
        except UnclosedLiteral:
            raise
        except RecursionError:
            # sqlglot's parser recurses about twenty frames per nesting level, so some fifty nested parentheses end
            # the interpreter's stack. Callers handle text that cannot be read (ParseError); few expect this.
            raise TooDeeplyNested("the query is nested too deeply to read") from None
        except ParseError as error:
            rewritten = sql
            for rewrite in (
                _rewrite_raw_bytes, _rewrite_verbatim_calls, _rewrite_model_arguments, _rewrite_pipe_operators, _rewrite_pipe_with,
                _rewrite_like_quantifiers, _rewrite_aggregate_where, _rewrite_with_expressions, _rewrite_unnest_offset,
            ):
                rewritten = rewrite(rewritten, tokens if rewritten == sql else self.tokenize(rewritten))
            if rewritten != sql:
                try:
                    return list(_resolve_markers(parse_with_table_arguments(self, rewritten, **opts)))
                except (UnclosedLiteral, TooDeeplyNested):
                    raise
                except ParseError:
                    raise error from None
            marked = _mark_table_arguments(_merge_table_function_kind(tokens))
            if len(marked) == len(tokens) and all(a is b for a, b in zip(marked, tokens)):
                raise
            return list(_resolve_markers(_resolve_table_arguments(self.parser(**opts).parse(marked, sql))))

    BigQuery.parse = parse_with_table_arguments
    # Releases differ in how a generator finds its handler (a method named after the class, a per-class table, a cache of both),
    # so every generator that exists gets the handler in its own table and any cache is dropped.
    pending = [Generator]
    while pending:
        generator = pending.pop()
        generator.TRANSFORMS[exp.Anonymous] = _generate
        generator.TRANSFORMS[exp.All] = _all_sql
        pending.extend(generator.__subclasses__())
    from sqlglot import generator as generator_module

    BigQuery.Generator.TRANSFORMS[exp.Filter] = _filter_sql
    getattr(generator_module, "_DISPATCH_CACHE", {}).clear()

install()
