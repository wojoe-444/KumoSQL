"""Saved column equivalences between tables, how they enter an equivalence proof, and the crosswalk they belong to.

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

The same store is a crosswalk between two versions of a pipeline. Every entry has a ``status``:

* ``equal`` (the default, and every entry of a file written before statuses existed): the
  declaration above. Only these enter a proof.
* ``differs``: the listed column pairs (or, with no columns, the two tables) correspond, but
  their values are known to differ, for example a timezone or a rounding.
* ``missing``: a column (``[["x", null]]`` or ``[[null, "y"]]``) or a table (``right`` or
  ``left`` null, no columns) has no counterpart on the other side.

``explanation`` names the known-difference rule behind a ``differs`` or ``missing`` entry,
``evidence`` is free text (a query or a link) and ``verified_at`` the date (``YYYY-MM-DD``)
someone checked the entry. ``differs`` and ``missing`` entries are kept apart, in
``crosswalk.json``, so that nothing reading ``equivalences.json`` (a KumoSQL from before
statuses included) can take a recorded difference for an equality, and ``rewrite_tree`` skips
any entry that is not ``equal``. ``load`` returns the declarations a proof may use,
``load_all`` every entry, and ``lookup`` the entries that name a table or a column.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from . import state
from kumo_sql_prover.ast_utils import UnmodeledConstruct, faithful_sql, table_parts as _parts, visible_ctes

MAX_DECLARATIONS = 2000
STATUSES = ("equal", "differs", "missing")
MAX_EXPLANATION = 200
MAX_EVIDENCE = 4000
EQUAL_FILE = "equivalences.json"  # what a proof may assume: equal declarations only
CROSSWALK_FILE = "crosswalk.json"  # differs and missing records, never read by a proof
_LOCK = threading.Lock()
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Equivalence:
    """One saved entry: ``status`` ``equal`` is a proof assumption, ``differs`` and ``missing`` are records only.

    ``left`` or ``right`` is ``None`` only in a ``missing`` entry for a table with no counterpart, and one column of
    a ``missing`` entry's pair is ``None``: the side that has no counterpart.
    """

    left: str | None
    right: str | None
    columns: tuple[tuple[str | None, str | None], ...] = ()
    whole: bool = False
    status: str = "equal"
    explanation: str | None = None
    evidence: str | None = None
    verified_at: str | None = None

    def to_json(self) -> dict:
        return {"left": self.left, "right": self.right,
                "columns": [list(pair) for pair in self.columns], "whole": self.whole,
                "status": self.status, "explanation": self.explanation, "evidence": self.evidence,
                "verified_at": self.verified_at}

    @property
    def lonely(self) -> bool:
        """A ``missing`` entry for a whole table: one side is ``None``."""

        return self.left is None or self.right is None

    @property
    def label(self) -> str:
        if self.status == "missing":
            if self.lonely:
                return f"{self.left or self.right} has no counterpart"
            return "; ".join(
                f"{self.left}.{a} has no counterpart in {self.right}" if b is None
                else f"{self.right}.{b} has no counterpart in {self.left}"
                for a, b in self.columns
            )
        if self.status == "differs":
            pairs = ", ".join(f"{a} vs {b}" for a, b in self.columns)
            text = f"{self.left} differs from {self.right}" + (f" ({pairs})" if pairs else "")
            return text + (f" [{self.explanation}]" if self.explanation else "")
        pairs = ", ".join(f"{a} = {b}" for a, b in self.columns)
        return f"{self.left} ≡ {self.right}" + (f" ({pairs})" if pairs else "")


def _absent(value: object) -> bool:
    """No counterpart: ``None``, an empty string or ``-``."""

    return value is None or (isinstance(value, str) and value.strip().strip("`").strip() in ("", "-"))


def _name(value: object, what: str) -> str:
    if not isinstance(value, str) or _absent(value):
        raise ValueError(f"{what} must be a table name")
    return value.strip().strip("`").lower()


def _column(value: object) -> str:
    if not isinstance(value, str) or _absent(value):
        raise ValueError("a column must be a name")
    return value.strip().strip("`").lower()


def _text(value: object, what: str, limit: int, one_line: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    if one_line and ("\n" in value or "\r" in value):
        raise ValueError(f"{what} must be one line")
    return value


def _date(value: object) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value.isoformat()
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value.strip()):
        raise ValueError("verified_at must be a date written YYYY-MM-DD")
    try:
        return datetime.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ValueError(f"verified_at {value.strip()} is not a calendar date") from None


def _status(value: object) -> str:
    if value is None:
        return "equal"
    if not isinstance(value, str) or value.strip().lower() not in STATUSES:
        raise ValueError("status must be one of " + ", ".join(STATUSES))
    return value.strip().lower()


def parse(item: object) -> Equivalence:
    if not isinstance(item, dict):
        raise ValueError("an equivalence must be an object")
    status = _status(item.get("status"))
    raw_left, raw_right = item.get("left"), item.get("right")
    lonely = status == "missing" and (_absent(raw_left) or _absent(raw_right))
    if lonely and _absent(raw_left) and _absent(raw_right):
        raise ValueError("name the table that has no counterpart")
    left = None if lonely and _absent(raw_left) else _name(raw_left, "left")
    right = None if lonely and _absent(raw_right) else _name(raw_right, "right")
    if left == right:
        raise ValueError("a table is already equivalent to itself")
    raw = item.get("columns") or []
    if not isinstance(raw, list):
        raise ValueError("columns must be a list of [left column, right column]")
    pairs: list[tuple[str | None, str | None]] = []
    for pair in raw:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise ValueError("columns must be a list of [left column, right column]")
        if status == "missing":
            if lonely:
                raise ValueError("a table with no counterpart takes no columns")
            if _absent(pair[0]) == _absent(pair[1]):
                raise ValueError("a missing column has a counterpart on one side only: write LEFTCOL= or =RIGHTCOL")
            a = None if _absent(pair[0]) else _column(pair[0])
            b = None if _absent(pair[1]) else _column(pair[1])
        else:
            a, b = _column(pair[0]), _column(pair[1])
        if any((a is not None and a == p[0]) or (b is not None and b == p[1]) for p in pairs):
            raise ValueError(f"column {a or b} is listed twice")
        pairs.append((a, b))
    whole = item.get("whole", False)
    if not isinstance(whole, bool):
        raise ValueError("whole must be true or false")
    explanation = _text(item.get("explanation"), "explanation", MAX_EXPLANATION, one_line=True)
    if status == "equal":
        if not pairs and not whole:
            raise ValueError("list the columns that are equivalent, or declare the whole table")
        if explanation:
            raise ValueError("an explanation names a known difference, so it needs status differs or missing")
    elif whole:
        raise ValueError("whole applies only to an equal declaration")
    elif status == "missing" and not pairs and not lonely:
        raise ValueError("list the columns that have no counterpart, or name only the table that has none")
    return Equivalence(
        left, right, tuple(pairs), whole, status, explanation,
        _text(item.get("evidence"), "evidence", MAX_EVIDENCE, one_line=False),
        _date(item.get("verified_at")),
    )


def _file(name: str = EQUAL_FILE):
    return state.data_path(name)


def _read(name: str) -> list[Equivalence]:
    try:
        raw = json.loads(_file(name).read_text(encoding="utf-8"))
        return [parse(item) for item in raw]
    except (OSError, TypeError, ValueError):
        return []


def load() -> list[Equivalence]:
    """The ``equal`` declarations: the only entries a proof may assume."""

    return [item for item in _read(EQUAL_FILE) if item.status == "equal"]


def _records() -> list[Equivalence]:
    return [item for item in _read(CROSSWALK_FILE) if item.status != "equal"]


def load_all() -> list[Equivalence]:
    """Every saved entry: the ``equal`` declarations, then the ``differs`` and ``missing`` records."""

    return load() + _records()


def _save(name: str, items: list[Equivalence]) -> None:
    path = _file(name)
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
        if item.status == "equal":
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


def _oriented(old: Equivalence, new: Equivalence) -> tuple[tuple[str | None, str | None], ...] | None:
    """``old``'s column pairs written as ``new``'s left and right, or ``None`` when they name other tables."""

    if (old.left, old.right) == (new.left, new.right):
        return old.columns
    if (old.left, old.right) == (new.right, new.left):
        return tuple((b, a) for a, b in old.columns)
    return None


def _conflict(items: list[Equivalence], new: Equivalence) -> str | None:
    """Why ``new`` repeats or contradicts a saved entry, or ``None``.

    A table with no counterpart appears in no other entry; a whole-table ``equal`` declaration (the same rows, every
    column) leaves no column of that pair to differ or be missing; and a column has one entry per pair of tables.
    """

    for old in items:
        if old.lonely or new.lonely:
            shared = ({old.left, old.right} & {new.left, new.right}) - {None}
            if shared and old.lonely and new.lonely:
                return f"{new.label} is already saved; remove it first"
            if shared:
                lonely = old if old.lonely else new
                return f"{min(shared)} is in another entry, which contradicts {lonely.label}; remove one first"
            continue
        pairs = _oriented(old, new)
        if pairs is None:
            continue
        if not old.columns and not new.columns and old.status == new.status:
            return f"{new.label} is already saved; remove it first"
        if old.status != new.status and "equal" in (old.status, new.status):
            equal, other = (old, new) if old.status == "equal" else (new, old)
            if equal.whole:
                return f"{equal.label} says every column holds the same rows, which contradicts {other.label}; remove one first"
        lefts = {a for a, _ in pairs if a is not None}
        rights = {b for _, b in pairs if b is not None}
        for a, b in new.columns:
            if (a is not None and a in lefts) or (b is not None and b in rights):
                return f"column {a or b} already has a {old.status} entry ({old.label}); remove it first"
    return None


def add(item: object) -> Equivalence:
    """Save an entry.

    An ``equal`` declaration is one per ``right`` table, with no cycles (a table cannot be defined through itself).
    An entry that repeats or contradicts a saved one is refused (see ``_conflict``).
    """

    new = parse(item)
    with _LOCK:
        equal, records = load(), _records()
        if new.status == "equal":
            if any(old.right == new.right for old in equal):
                raise ValueError(f"{new.right} already has an equivalence; remove it first")
            if _reaches(equal, new.left, new.right):
                raise ValueError(f"{new.left} is already defined through {new.right}")
        why = _conflict(equal + records, new)
        if why:
            raise ValueError(why)
        if len(equal) + len(records) >= MAX_DECLARATIONS:
            raise ValueError("too many equivalences")
        if new.status == "equal":
            _save(EQUAL_FILE, [*equal, new])
        else:
            _save(CROSSWALK_FILE, [*records, new])
    return new


def remove(right: object, left: object = None, status: object = "equal") -> bool:
    """Remove the entries for ``right``; for a table with no counterpart, ``right`` is the table it names.

    ``left`` keeps only the entries from that table. ``status`` (``equal`` by default, which is what the declarations
    page shows) keeps only that status; ``None`` removes every status.
    """

    name = _name(right, "right")
    other = None if left is None else _name(left, "left")
    wanted = None if status is None else _status(status)

    def hit(item: Equivalence) -> bool:
        if wanted is not None and item.status != wanted:
            return False
        if item.right == name:
            return other is None or item.left == other
        return item.right is None and item.left == name and other is None

    removed = False
    with _LOCK:
        for file, items in ((EQUAL_FILE, load()), (CROSSWALK_FILE, _records())):
            kept = [item for item in items if not hit(item)]
            if len(kept) != len(items):
                _save(file, kept)
                removed = True
    return removed


def names_table(query: str, name: str | None) -> bool:
    """``query`` and ``name`` are the same table: equal, or one is the last dotted parts of the other.

    ``orders`` and ``raw.orders`` both name ``proj.raw.orders``.
    """

    if name is None:
        return False
    a, b = query.lower().split("."), name.lower().split(".")
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return bool(short) and long[-len(short):] == short


def _column_matches(item: Equivalence, side: str, column: str, implied: bool) -> list[dict]:
    index = 0 if side == "left" else 1
    found = [{"left_column": a, "right_column": b, "implied": False}
             for a, b in item.columns if (a, b)[index] == column]
    if found or not item.whole or not implied:
        return found
    listed = {name for pair in item.columns for name in pair if name is not None}
    # a whole-table declaration: a column it does not list keeps its name on the other side
    return [] if column in listed else [{"left_column": column, "right_column": column, "implied": True}]


def lookup(table: str | None = None, column: str | None = None, *, status: str | None = None,
           items: list[Equivalence] | None = None) -> list[dict]:
    """The saved entries that name ``table`` (on either side, by full name or its last parts) and ``column``.

    Each result is the entry's JSON plus ``label``; ``matched_on``, the side ``table`` names (``left``, ``right`` or
    ``both``; ``None`` without ``table``); ``counterpart``, the table on the other side (``None`` when the table has
    none, or without ``table``); and ``column_matches``, one ``{left_column, right_column, implied}`` per pair naming
    ``column`` (a ``None`` column has no counterpart). ``implied`` marks a column a whole-table declaration gives the
    same name on both sides without listing it; that needs ``table``, since without one every whole-table declaration
    would claim every name. ``status`` keeps only entries of that status. ``items`` defaults to every saved entry;
    results keep their saved order.
    """

    wanted = None if status is None else _status(status)
    table_name = None if table is None else _name(table, "table")
    column_name = None if column is None else _column(column)
    items = load_all() if items is None else items
    found: list[dict] = []
    for item in items:
        if wanted is not None and item.status != wanted:
            continue
        sides = ["left", "right"] if table_name is None else [
            side for side, name in (("left", item.left), ("right", item.right)) if names_table(table_name, name)
        ]
        if not sides:
            continue
        matches: list[dict] = []
        if column_name is not None:
            for side in sides:
                matches += [m for m in _column_matches(item, side, column_name, table_name is not None) if m not in matches]
            if not matches:
                continue
        entry = item.to_json()
        entry["label"] = item.label
        if table_name is None or len(sides) == 2:
            entry["matched_on"], entry["counterpart"] = (None if table_name is None else "both"), None
        else:
            entry["matched_on"] = sides[0]
            entry["counterpart"] = item.right if sides[0] == "left" else item.left
        entry["column_matches"] = matches
        found.append(entry)
    return found


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

    if item.status != "equal":
        raise ValueError(f"only an equal declaration rewrites a query, not {item.label}")
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
    """Replace references to each ``right`` table in a parsed query; returns the tree and the declarations used.

    Only ``equal`` declarations apply: a ``differs`` or ``missing`` entry says the tables do not hold the same values.
    """

    items = [item for item in items if item.status == "equal"]
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

    items = [item for item in (load() if items is None else items) if item.status == "equal"]
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
