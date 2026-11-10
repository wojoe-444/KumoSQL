"""Saved column equivalences between tables, and how they enter an equivalence proof.

A declaration says "``left`` and ``right`` hold the same rows, and ``left.x`` is
``right.y``": ``{"left": "p.d.a", "right": "p.d.b", "columns": [["x", "y"]], "whole": false}``.

* ``whole`` false: the listed columns of the two tables hold the same rows as a bag
  (``SELECT x FROM a`` equals ``SELECT y FROM b``). It applies only to queries that read
  nothing else from ``right``.
* ``whole`` true: the tables are the same bag of rows; columns not listed share their name.

The declaration is an assumption about the data, never inferred and never written to
BigQuery. It enters a proof by rewriting each reference to ``right`` into
``(SELECT left.x AS y, ... FROM left)``, so the solver sees one relation and every
existing rule (keys, joins, aggregates) applies. A column the rewrite does not provide
makes the query fail to compile, which leaves the result unknown rather than wrong.
Declarations are stored in the data folder (``equivalences.json``).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from . import state
from kumo_sql_prover.ast_utils import UnmodeledConstruct, faithful_sql, table_parts as _parts, visible_ctes

MAX_DECLARATIONS = 2000
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Equivalence:
    left: str
    right: str
    columns: tuple[tuple[str, str], ...] = ()
    whole: bool = False

    def to_json(self) -> dict:
        return {"left": self.left, "right": self.right,
                "columns": [list(pair) for pair in self.columns], "whole": self.whole}

    @property
    def label(self) -> str:
        pairs = ", ".join(f"{a} = {b}" for a, b in self.columns)
        return f"{self.left} ≡ {self.right}" + (f" ({pairs})" if pairs else "")


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip().strip("`"):
        raise ValueError(f"{what} must be a table name")
    return value.strip().strip("`").lower()


def parse(item: object) -> Equivalence:
    if not isinstance(item, dict):
        raise ValueError("an equivalence must be an object")
    left, right = _name(item.get("left"), "left"), _name(item.get("right"), "right")
    if left == right:
        raise ValueError("a table is already equivalent to itself")
    raw = item.get("columns") or []
    if not isinstance(raw, list):
        raise ValueError("columns must be a list of [left column, right column]")
    pairs = []
    for pair in raw:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise ValueError("columns must be a list of [left column, right column]")
        a, b = _name(pair[0], "column"), _name(pair[1], "column")
        if any(a == p[0] or b == p[1] for p in pairs):
            raise ValueError(f"column {a} or {b} is listed twice")
        pairs.append((a, b))
    whole = item.get("whole", False)
    if not isinstance(whole, bool):
        raise ValueError("whole must be true or false")
    if not pairs and not whole:
        raise ValueError("list the columns that are equivalent, or declare the whole table")
    return Equivalence(left, right, tuple(pairs), whole)


def _file():
    return state.data_path("equivalences.json")


def load() -> list[Equivalence]:
    try:
        raw = json.loads(_file().read_text(encoding="utf-8"))
        return [parse(item) for item in raw]
    except (OSError, ValueError):
        return []


def _save(items: list[Equivalence]) -> None:
    path = _file()
    handle, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump([item.to_json() for item in items], out, indent=1)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _reaches(items: list[Equivalence], start: str, goal: str) -> bool:
    """Whether ``goal`` is reached from ``start`` following right -> left rewrites."""

    edges: dict[str, set[str]] = {}
    for item in items:
        edges.setdefault(item.right, set()).add(item.left)
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node == goal:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(edges.get(node, ()))
    return False


def add(item: object) -> Equivalence:
    """Save a declaration; one per ``right`` table, and no cycles (a table cannot be defined through itself)."""

    new = parse(item)
    with _LOCK:
        items = load()
        if any(old.right == new.right for old in items):
            raise ValueError(f"{new.right} already has an equivalence; remove it first")
        if _reaches(items, new.left, new.right):
            raise ValueError(f"{new.left} is already defined through {new.right}")
        if len(items) >= MAX_DECLARATIONS:
            raise ValueError("too many equivalences")
        items.append(new)
        _save(items)
    return new


def remove(right: object) -> bool:
    name = _name(right, "right")
    with _LOCK:
        items = load()
        kept = [item for item in items if item.right != name]
        if len(kept) == len(items):
            return False
        _save(kept)
    return True


def _matches(parts: list[str], key: str) -> bool:
    wanted = key.split(".")
    return bool(parts) and parts == wanted[-len(parts):]


def _read_columns(table: exp.Table, alias: str) -> list[str] | None:
    """Columns the enclosing SELECT reads from ``alias``; ``None`` when a star hides them."""

    select = table.find_ancestor(exp.Select)
    if select is None:
        return None
    sources = 1 + len(select.args.get("joins") or [])
    names: list[str] = []
    for column in select.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            if not column.table or column.table.lower() == alias.lower():
                return None
            continue
        if column.table.lower() == alias.lower() or (not column.table and sources == 1):
            if column.name.lower() not in names:
                names.append(column.name.lower())
    if any(isinstance(i, exp.Star) for i in select.expressions):
        return None
    return names


def replacement(item: Equivalence, alias: str, left_columns: list[str] | None = None) -> exp.Subquery:
    """``(SELECT left.x AS y, ... FROM left) AS alias`` for a reference to ``item.right``."""

    source = exp.to_table(item.left, dialect="bigquery")
    source.set("alias", exp.TableAlias(this=exp.to_identifier("kumosql_eq")))
    mapped = dict(item.columns)
    items: list[exp.Expression] = [
        exp.alias_(exp.column(a, table="kumosql_eq"), b) for a, b in item.columns
    ]
    if item.whole:
        if left_columns:
            renamed = set(mapped.values())
            items += [exp.column(c, table="kumosql_eq") for c in left_columns if c not in mapped and c not in renamed]
        else:
            skipped = f" EXCEPT ({', '.join(a for a in mapped)})" if mapped else ""
            star = sqlglot.parse_one(f"SELECT kumosql_eq.*{skipped}", read="bigquery").expressions[0]
            items.insert(0, star)
    select = exp.Select(expressions=items).from_(source)
    return exp.Subquery(this=select, alias=exp.TableAlias(this=exp.to_identifier(alias)))


def rewrite_tree(tree: exp.Expression, items: list[Equivalence], columns: dict[str, list[str]] | None = None):
    """Replace references to each ``right`` table in a parsed query; returns the tree and the declarations used."""

    if not items:
        return tree, []
    used: list[Equivalence] = []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for _ in range(len(items) + 1):  # a rewrite can expose another table to rewrite
        changed = False
        for table in list(tree.find_all(exp.Table)):
            parts = _parts(table)
            if not parts or (len(parts) == 1 and parts[0] in ctes):
                continue
            hits = [item for item in items if _matches(parts, item.right)]
            if len(hits) != 1:
                continue  # an ambiguous spelling says nothing certain
            item = hits[0]
            if "." not in item.left and item.left.lower() in visible_ctes(table):
                continue  # a WITH table of that name would capture the replacement's read
            alias = table.alias or table.name
            left_columns = None
            if columns:
                left_columns = columns.get(item.left) or next(
                    (cols for key, cols in columns.items() if _matches(key.split("."), item.left)), None
                )
            if item.whole and not left_columns:
                read = _read_columns(table, alias)
                left_columns = None if read is None else read
            table.replace(replacement(item, alias, left_columns))
            if item not in used:
                used.append(item)
            changed = True
        if not changed:
            break
    return tree, used


def rewrite_sql(sql: str, items: list[Equivalence] | None = None, columns: dict[str, list[str]] | None = None,
                dialect: str = "bigquery") -> tuple[str, list[Equivalence]]:
    """``(sql, used)``: the query with saved equivalences applied (unchanged when none matches)."""

    items = load() if items is None else items
    if not items:
        return sql, []
    tree = sqlglot.parse_one(sql, read=dialect)
    tree, used = rewrite_tree(tree, items, columns)
    if not used:
        return sql, used
    try:
        return faithful_sql(tree, dialect), used
    except UnmodeledConstruct:
        return sql, []  # the rewritten query cannot be written down faithfully, so it is not used
