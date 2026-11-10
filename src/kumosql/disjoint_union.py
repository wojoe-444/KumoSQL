"""A ``UNION`` whose branches cannot repeat a row is a ``UNION ALL``.

``SELECT 'low' AS bin, COUNT(*) FROM t WHERE x < 5 UNION SELECT 'high', COUNT(*) FROM t WHERE x >= 5`` removes
duplicates from two rows that can never be equal: each branch returns one row, and the rows differ in their
first column. ``UNION`` and ``UNION ALL`` then return the same bag.

A ``UNION [DISTINCT]`` is read as ``UNION ALL`` when both hold for all of its branches (the selects under it,
through nested ``UNION``/``UNION ALL`` nodes):

* **no branch repeats a row**: it returns at most one row (an aggregate query with no ``GROUP BY``, or a select
  with no ``FROM``). A ``SELECT DISTINCT`` branch of many rows would qualify as well, but is left alone: the
  prover compares a ``UNION`` of such branches with a ``UNION`` of branches without ``DISTINCT`` as sets, and
  marking only one of the two ``ALL`` would hide that they are the same;
* **no two branches share a row**: at one column position every branch selects a literal, and the literals are
  certainly different values. Integers are compared as numbers. Strings are compared with case, spaces and
  punctuation ignored and must be plain ASCII with a letter or digit left, so two tags that some collation
  could call equal (``'A'`` and ``'a '`` in MySQL) never count as different. A mix of string and integer tags is
  not read.

A nested ``UNION`` only removes rows of its own branches, so its result still has no repeated row and no row of
another branch. ``INTERSECT``, ``EXCEPT``, ``BY NAME``/``CORRESPONDING`` set operations, a union with its own
``ORDER BY``/``LIMIT``, and branches with a star (the positions are not known) are left alone.
"""

from __future__ import annotations

import re

from sqlglot import exp

from .ast_utils import plain_distinct, select_sources

_UNION_ARGS = frozenset({"this", "expression", "distinct"})
_BANNED = ("qualify", "kind", "into", "laterals", "pivots", "connect", "sample", "with_", "with")
# PostgreSQL returns a row per element of a set-returning function in the select list; sqlglot classes only some as UDTF.
_ROW_MAKERS = (exp.UDTF, exp.GenerateSeries, exp.ExplodingGenerateSeries, exp.Anonymous)


def _unwrap_branch(node: exp.Expression) -> exp.Expression | None:
    while type(node) is exp.Subquery:
        if node.alias or any(value for key, value in node.args.items() if key not in ("this", "alias")):
            return None
        node = node.this
    return node


def _branches(node: exp.Expression) -> list[exp.Select] | None:
    """The selects under a union, through nested unions; ``None`` if anything else is under it."""

    node = _unwrap_branch(node)
    if type(node) is exp.Select:
        return [node]
    if type(node) is exp.Union and not any(value for key, value in node.args.items() if key not in _UNION_ARGS):
        left, right = _branches(node.this), _branches(node.expression)
        return None if left is None or right is None else left + right
    return None


def _own_aggregate(select: exp.Select) -> bool:
    for item in select.expressions:
        for node in item.walk(prune=lambda n: isinstance(n, (exp.Select, exp.Subquery, exp.Window))):
            if isinstance(node, exp.AggFunc):
                return True
    return False


def _one_row_per_input(select: exp.Select) -> bool:
    """No select-list function that returns several rows (or an unknown one, which might)."""

    return not any(isinstance(node, _ROW_MAKERS) for item in select.expressions for node in item.walk())


def _never_repeats(select: exp.Select) -> bool:
    if any(select.args.get(key) for key in _BANNED):
        return False
    if select.args.get("distinct") is not None and not plain_distinct(select):
        return False
    if select.args.get("group") is not None or not _one_row_per_input(select):
        return False
    return not select_sources(select) or _own_aggregate(select)


def _tag(item: exp.Expression) -> tuple[str, object] | None:
    node = item.unalias()
    while isinstance(node, exp.Paren):
        node = node.this
    if type(node) is not exp.Literal:
        return None
    if not node.is_string:
        return ("int", int(node.this)) if node.this.isdigit() and len(node.this) <= 15 else None
    text = node.this
    if not text.isascii() or "\\" in text:
        return None  # an escape sequence may spell another character
    folded = re.sub(r"[^a-z0-9]", "", text.lower())
    return ("str", folded) if folded else None


def _told_apart(branches: list[exp.Select]) -> bool:
    width = len(branches[0].expressions)
    for branch in branches:
        if len(branch.expressions) != width:
            return False
        for item in branch.expressions:
            shown = item.unalias()
            if isinstance(shown, exp.Star) or (isinstance(shown, exp.Column) and isinstance(shown.this, exp.Star)):
                return False
    for position in range(width):
        tags = [_tag(branch.expressions[position]) for branch in branches]
        if None in tags or len({kind for kind, _ in tags}) != 1:
            continue
        if len(set(tags)) == len(tags):
            return True
    return False


def separate_disjoint_unions(tree: exp.Expression) -> exp.Expression:
    """``tree`` with every such ``UNION`` marked ``ALL`` (in place; a pre-pass of ``normalize``)."""

    for union in list(tree.find_all(exp.Union)):
        if type(union) is not exp.Union or not union.args.get("distinct"):
            continue
        if any(value for key, value in union.args.items() if key not in _UNION_ARGS):
            continue
        branches = _branches(union)
        if branches is None or len(branches) < 2:
            continue
        if all(_never_repeats(branch) for branch in branches) and _told_apart(branches):
            union.set("distinct", False)
    return tree
