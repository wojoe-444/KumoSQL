"""Spell two constructs the way they are printed, so a normalized tree and its text are the same query.

``ast_utils.faithful_sql`` prints the normalized tree, parses the text back and refuses the query when the two
differ ("cannot be printed faithfully"). Two forms the normalizer builds print as correct text that parses as
a different (equal) tree, so every query that contained them was declined, even against its own text.
``spell_as_printed`` runs last and rewrites both to the tree their text parses as.

**A NULL-propagating concatenation.** ``algebraic_equivalence._bigquery_sugar`` reads ``a || b`` as
``CONCAT(a, b)`` with no ``coalesce`` flag: the concatenation that is NULL when an operand is NULL, which is what
``||`` means in the dialects below and what BigQuery's ``CONCAT`` means. Nested ones are then flattened to
``CONCAT(a, b, c)``. In PostgreSQL, Redshift and DuckDB the function ``CONCAT`` skips NULL arguments instead, so
sqlglot prints that node back as ``a || b || c``, which parses as nested ``||`` nodes. Each such node becomes
the left-nested chain ``((a || b) || c)``. The value is unchanged:

* ``CONCAT(a, b, ..)`` without ``coalesce`` and the chain are both NULL exactly when some operand is NULL;
* otherwise both are the operands' texts joined in order (string concatenation is associative, which the
  flattening already relied on).

A ``CONCAT`` that skips NULLs (``coalesce`` set: PostgreSQL's own ``CONCAT(a, b)``) is a different function and
is left alone, as is every dialect not listed: Oracle's ``||`` reads NULL as the empty string, and MySQL's is
``OR``. A one-operand ``CONCAT`` is left alone too.

**A grouping set of one column.** ``grouping_sets.expand_grouping_sets`` writes ``ROLLUP (a, b)`` as
``GROUPING SETS ((a, b), (a), ())`` with every set a tuple. A tuple of one expression prints as ``(a)``, which
parses as a parenthesized ``a``. Directly inside ``GROUPING SETS`` the two are the same grouping set, the one
that groups by ``a``, so the one-element tuple becomes the parenthesized expression (any dialect). A tuple
anywhere else (``VALUES (1)``, a row value) is not touched.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

# Dialects whose || is NULL when an operand is NULL and whose CONCAT() skips NULLs (so sqlglot prints the node as ||).
_PIPE_DIALECTS = frozenset({"postgres", "redshift", "duckdb"})
_FLAGS: dict[str, dict | None] = {}
_ATOMS = (exp.Column, exp.Literal, exp.Func, exp.Paren, exp.Null, exp.Boolean, exp.Subquery, exp.Parameter, exp.Placeholder)


def _pipe_flags(dialect: str) -> dict | None:
    """The arguments the dialect's parser puts on a ``||`` node besides its operands (None if it is not a ``||``)."""

    if dialect not in _FLAGS:
        try:
            probe = sqlglot.parse_one("SELECT a || b", read=dialect).expressions[0]
        except Exception:  # noqa: BLE001 - a dialect that cannot read it gets no rewrite
            probe = None
        _FLAGS[dialect] = (
            {k: v for k, v in probe.args.items() if k not in ("this", "expression") and v is not None}
            if type(probe) is exp.DPipe
            else None
        )
    return _FLAGS[dialect]


def _null_propagating_concat(node: exp.Expression) -> bool:
    if type(node) is not exp.Concat or node.args.get("coalesce") or len(node.expressions) < 2:
        return False
    return not any(v for k, v in node.args.items() if k not in ("expressions", "safe", "coalesce"))


def _lone_grouping_set(node: exp.Expression) -> bool:
    return type(node) is exp.Tuple and len(node.expressions) == 1 and isinstance(node.parent, exp.GroupingSets) and node.arg_key == "expressions"


def _operand(node: exp.Expression) -> exp.Expression:
    # the printer adds no parentheses of its own: an operator operand keeps its grouping explicitly
    return node if isinstance(node, _ATOMS) else exp.Paren(this=node)


def spell_as_printed(tree: exp.Expression, dialect: str) -> exp.Expression:
    """``tree`` with NULL-propagating ``CONCAT(a, b, ..)`` as ``a || b || ..`` (PostgreSQL, Redshift, DuckDB) and
    one-column grouping sets as ``(a)``; ``tree`` itself when there is nothing to respell."""

    flags = _pipe_flags(dialect) if dialect in _PIPE_DIALECTS else None
    concats = flags is not None and any(_null_propagating_concat(n) for n in tree.find_all(exp.Concat))
    sets = any(_lone_grouping_set(n) for n in tree.find_all(exp.Tuple))
    if not concats and not sets:
        return tree
    tree = tree.copy()
    if sets:
        for node in list(tree.find_all(exp.Tuple)):
            if _lone_grouping_set(node):
                node.replace(exp.Paren(this=node.expressions[0].copy()))
    if concats:
        for node in reversed(list(tree.find_all(exp.Concat))):  # innermost first
            if not _null_propagating_concat(node):
                continue
            parts = [_operand(part.copy()) for part in node.expressions]
            chain = parts[0]
            for part in parts[1:]:
                chain = exp.DPipe(this=chain, expression=part, **flags)
            if node is tree:
                tree = chain
            else:
                node.replace(chain)
    return tree
