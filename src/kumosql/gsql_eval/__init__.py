"""Pure-Python GoogleSQL / BigQuery reference evaluator.

``evaluate(sql, database)`` interprets a query's sqlglot BigQuery tree against in-memory tables and returns the
rows BigQuery returns, or raises :class:`Unsupported` (never an approximation), :class:`AnalysisError` (BigQuery
rejects the query) or :class:`EvalError` (BigQuery fails while running it on this data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from . import types as T
from . import values as V
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import Ctx, Env
from . import expressions as _expressions  # noqa: F401  (registers the expression handlers)

__all__ = ["AnalysisError", "Database", "EvalError", "Result", "Table", "Unsupported", "evaluate"]


@dataclass
class Table:
    """``columns`` is ``[(name, Type)]``; ``rows`` are tuples of payloads (see ``values.py``)."""

    columns: list
    rows: list


class Database:
    def __init__(self, tables: dict[str, Table] | None = None):
        self._tables = {k.lower(): v for k, v in (tables or {}).items()}

    def add(self, name: str, table: Table) -> None:
        self._tables[name.lower()] = table

    def table(self, name: str) -> Table | None:
        return self._tables.get(name.lower())


@dataclass
class Result:
    columns: list  # [(name or None, Type)]
    rows: list
    ordered: bool  # the rows' order is part of the answer (ORDER BY / known order)
    deterministic: bool  # no part of the result depends on an undetermined choice
    inexact: bool  # float arithmetic whose last bits may differ between engines
    value_table: bool = False
    reasons: list = field(default_factory=list)


def _strict_set_operations(text: str) -> bool:
    """Whether ``text`` has ``STRICT CORRESPONDING`` set operations (sqlglot reads them exactly as ``BY NAME``: it drops the
    keyword) and no ``BY NAME``, so that a set operation parsed without a mode is a STRICT one.

    Works on tokens, so a STRICT in a string, a comment or a quoted name does not count. A bare STRICT anywhere else (or
    one combined with INNER/LEFT/FULL) is refused: the parse could not tell us what it meant.
    """

    from sqlglot.dialects.bigquery import BigQuery
    from sqlglot.tokens import TokenType

    try:
        tokens = BigQuery().tokenize(text)
    except Exception:  # the parser reports what is wrong with the text
        return False
    setop = (TokenType.UNION, TokenType.INTERSECT, TokenType.EXCEPT)
    quantifier = (TokenType.ALL, TokenType.DISTINCT)
    strict = by_name = False
    for i, token in enumerate(tokens):
        word = token.text.upper()
        if token.token_type is not TokenType.VAR:
            continue
        after_op = i >= 2 and tokens[i - 1].token_type in quantifier and tokens[i - 2].token_type in setop
        if word == "STRICT":
            followed = i + 1 < len(tokens) and tokens[i + 1].token_type is TokenType.VAR and tokens[i + 1].text.upper() == "CORRESPONDING"
            if not (after_op and followed):
                raise Unsupported("STRICT outside STRICT CORRESPONDING (sqlglot drops the keyword)")
            if i >= 3 and tokens[i - 3].text.upper() in ("INNER", "LEFT", "FULL", "OUTER"):
                raise Unsupported("STRICT CORRESPONDING combined with a join-style mode")
            strict = True
        elif word == "BY" and after_op and i + 1 < len(tokens) and tokens[i + 1].text.upper() == "NAME":
            by_name = True
    return strict and not by_name


def evaluate(sql_or_tree: Any, database: Database | None = None, time_zone: str = "UTC", params: dict | None = None,
             mode: str = "bigquery") -> Result:
    from .compiler import Compiler, EmptyScope

    literals_decoded = False
    strict_certain = False
    if isinstance(sql_or_tree, str):
        from kumo_sql_prover.string_literals import invalid_literal
        from . import text_guards
        from .literals import decode_literals

        text = sql_or_tree
        if invalid_literal(text):
            raise AnalysisError("Invalid string literal")
        text_guards.check(text)
        strict_certain = _strict_set_operations(text)
        try:
            tree = sqlglot.parse_one(decode_literals(text), read="bigquery")
        except sqlglot.errors.ParseError as error:
            raise Unsupported(f"sqlglot cannot parse the query: {str(error)[:120]}") from None
        literals_decoded = True
        if isinstance(tree, exp.Block):  # `SELECT 1;  -- comment`: the statement and a Semicolon carrying the comment
            statements = [e for e in tree.expressions if not isinstance(e, exp.Semicolon)]
            if len(statements) != 1:
                raise Unsupported("several statements")
            tree = statements[0]
    else:
        tree = sql_or_tree
        for lit in tree.find_all(exp.Literal):
            if "\\" in str(lit.this):
                raise Unsupported("backslash in a string literal on the tree path")
    database = database or Database()
    tz = V.zone(time_zone)
    ctx = Ctx(tz, params, mode, literals_decoded=literals_decoded)
    compiler = Compiler(database, tz, params or {}, mode, literals_decoded, strict_certain)
    plan = compiler.query(tree, EmptyScope(), {})
    rows = plan.run(Env((), None, ctx, {}))
    return Result(plan.columns, rows, plan.ordered, not ctx.nondeterministic, ctx.inexact, plan.value_table, list(ctx.reasons))
