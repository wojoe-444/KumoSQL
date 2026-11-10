"""Equivalent under stated conditions: the fourth verdict next to equivalent, different and unknown.

Two queries are often equal only because of a fact the queries themselves never state: ``id`` is a
primary key, ``customer_id`` is never NULL, every order has a customer. When the provers cannot decide
a pair outright, :func:`add_conditions` asks whether such facts would settle it. It collects *candidate
conditions* from the queries (every column they read may be NOT NULL; the columns they join, group,
order and project may be unique; two joined tables may be linked by a foreign key), assumes them all,
and runs the prover again. If that proves the pair, it removes conditions (chunks first) while the proof
survives, until every single condition has been tried and removing it loses the proof. The reported set is
therefore minimal *for the prover*: it is a set the prover can prove the pair under and cannot shrink by one
condition. A prover that fell silent without a condition does not show the condition is semantically needed.

The result is a real proof under exactly the listed conditions, together with everything the prover
already assumes about declared keys and NOT NULL columns. Each condition carries a SQL check (a query
that counts the rows breaking it, zero when the fact holds) so a team can decide whether the condition
is an acceptable cost and then verify it against the warehouse.

Soundness rules:

* the verdict needs a proof by the same prover, with the conditions added as ordinary declared
  constraints; nothing weaker than a proof is ever reported;
* a pair that is refuted on a database that satisfies the conditions stays refuted: the unconditional
  counterexample must visibly break one of the reported conditions, else the verdict is withheld;
* the declared facts stay assumed and are never listed as conditions; only candidates are.

Not in the catalog: that a table is non-empty (the provers have no way to assume it).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

import sqlglot
from sqlglot import exp

from .smt_equivalence import SmtEquivalenceResult, SmtStatus, TableConstraints

MAX_CANDIDATES = 40
DEFAULT_WALL_SECONDS = 30.0

KINDS = ("not_null", "unique", "foreign_key")


@dataclass(frozen=True)
class Condition:
    """One fact the pair is equivalent under: a NOT NULL column, a unique key or a foreign key."""

    kind: str
    table: str  # lower-case, as the queries spell it: how the provers look the table up
    columns: tuple[str, ...]
    parent: str = ""
    parent_columns: tuple[str, ...] = ()
    text: str = ""
    check_sql: str = ""

    @property
    def key(self) -> tuple:
        return (self.kind, self.table, self.columns, self.parent, self.parent_columns)

    def to_json(self) -> dict:
        data = {"kind": self.kind, "table": self.table, "columns": list(self.columns), "text": self.text, "check_sql": self.check_sql}
        if self.kind == "foreign_key":
            data.update(parent=self.parent, parent_columns=list(self.parent_columns))
        return data


# ---- candidate conditions ---------------------------------------------------------------------


class _Occurrence:
    """One table read by one select under one alias."""

    def __init__(self, select: exp.Select, alias: str, table: exp.Table):
        self.select, self.alias, self.table = select, alias, table
        parts = [p.name for p in table.parts]
        self.parts = parts
        self.key = ".".join(parts).lower()

    @property
    def ident(self) -> tuple:
        return (id(self.select), self.alias)


def _from_sources(select: exp.Select) -> list[exp.Expression]:
    source = select.args.get("from_") or select.args.get("from")
    found = [source.this] if source is not None and source.this is not None else []
    found += [j.this for j in select.args.get("joins") or [] if j.this is not None]
    return found


def _sources(select: exp.Select, ctes: set[str]) -> tuple[dict[str, _Occurrence], int]:
    """``({alias: occurrence of a base table}, number of sources in all)`` of one select."""

    tables: dict[str, _Occurrence] = {}
    sources = _from_sources(select)
    for source in sources:
        if isinstance(source, exp.Table) and not (not source.db and source.name.lower() in ctes):
            alias = source.alias_or_name.lower()
            tables[alias] = _Occurrence(select, alias, source)
    return tables, len(sources)


def _columns_of(schema: Mapping[str, list[str]] | None, key: str) -> set[str] | None:
    if not schema:
        return None
    lowered = {k.lower(): v for k, v in schema.items()}
    if key in lowered:
        return {c.lower() for c in lowered[key]}
    matches = [v for k, v in lowered.items() if key.endswith("." + k) or k.endswith("." + key)]
    return {c.lower() for c in matches[0]} if len(matches) == 1 else None


class _Reader:
    """Which base table and occurrence each column of one query belongs to."""

    def __init__(self, tree: exp.Expression, schema: Mapping[str, list[str]] | None):
        self.tree = tree
        self.schema = schema
        self.ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        self._scopes: dict[int, tuple[dict[str, _Occurrence], int]] = {}

    def scope(self, select: exp.Select):
        if id(select) not in self._scopes:
            self._scopes[id(select)] = _sources(select, self.ctes)
        return self._scopes[id(select)]

    def owner(self, column: exp.Column) -> _Occurrence | None:
        selects = []
        node = column.parent
        while node is not None:
            if isinstance(node, exp.Select):
                selects.append(node)
            node = node.parent
        if not selects:
            return None
        if column.table:
            alias = column.table.lower()
            for select in selects:
                tables, _ = self.scope(select)
                if alias in tables:
                    return tables[alias]
                if any(s.alias_or_name.lower() == alias for s in _from_sources(select) if not isinstance(s, exp.Table) or s.alias):
                    return None  # a derived table or CTE reference by that alias
            return None
        tables, total = self.scope(selects[0])
        if len(tables) == 1 and total == 1:
            return next(iter(tables.values()))
        if self.schema and tables and total == len(tables):
            found = [o for o in tables.values() if column.name.lower() in (_columns_of(self.schema, o.key) or ())]
            if len(found) == 1:
                return found[0]
        return None


def _quote(name: str, dialect: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _table_sql(parts: list[str], dialect: str) -> str:
    return ".".join(_quote(p, dialect) for p in parts)


def _make(kind: str, occ: _Occurrence, columns: Iterable[str], dialect: str, parent: _Occurrence | None = None, parent_columns: Iterable[str] = ()) -> Condition:
    columns = tuple(columns)
    shown = ".".join(occ.parts)
    if kind == "not_null":
        column = _quote(columns[0], dialect)
        return Condition(
            kind, occ.key, columns, text=f"{shown}.{columns[0]} is NOT NULL",
            check_sql=f"SELECT COUNT(*) AS violations FROM {_table_sql(occ.parts, dialect)} WHERE {column} IS NULL",
        )
    if kind == "unique":
        cols = ", ".join(_quote(c, dialect) for c in columns)
        present = " AND ".join(f"{_quote(c, dialect)} IS NOT NULL" for c in columns)
        return Condition(
            kind, occ.key, columns, text=f"({', '.join(columns)}) is unique in {shown}",
            check_sql=(
                f"SELECT COUNT(*) AS violations FROM (SELECT {cols} FROM {_table_sql(occ.parts, dialect)} "
                f"WHERE {present} GROUP BY {cols} HAVING COUNT(*) > 1) AS duplicated"
            ),
        )
    assert parent is not None
    parent_columns = tuple(parent_columns)
    present = " AND ".join(f"c.{_quote(c, dialect)} IS NOT NULL" for c in columns)
    match = " AND ".join(f"p.{_quote(pc, dialect)} = c.{_quote(c, dialect)}" for c, pc in zip(columns, parent_columns))
    return Condition(
        kind, occ.key, columns, parent.key, parent_columns,
        text=f"{shown}({', '.join(columns)}) references {'.'.join(parent.parts)}({', '.join(parent_columns)})",
        check_sql=(
            f"SELECT COUNT(*) AS violations FROM {_table_sql(occ.parts, dialect)} AS c WHERE {present} "
            f"AND NOT EXISTS (SELECT 1 FROM {_table_sql(parent.parts, dialect)} AS p WHERE {match})"
        ),
    )


def _declared(constraints: Mapping[str, TableConstraints] | None, condition: Condition) -> bool:
    facts = {k.lower(): v for k, v in (constraints or {}).items()}.get(condition.table)
    if facts is None:
        return False
    if condition.kind == "not_null":
        return condition.columns[0] in facts.not_null
    if condition.kind == "unique":
        return any(set(key) <= set(condition.columns) for key in facts.keys)
    return any(
        tuple(fk[0]) == condition.columns and fk[1].lower() == condition.parent and tuple(fk[2]) == condition.parent_columns
        for fk in facts.foreign_keys
    )


def candidate_conditions(
    left_sql: str,
    right_sql: str,
    *,
    schema: Mapping[str, list[str]] | None = None,
    constraints: Mapping[str, TableConstraints] | None = None,
    dialect: str = "bigquery",
) -> list[Condition]:
    """Facts that could settle the pair, taken from the queries' own joins, groupings, orderings and columns.

    Most useful first; at most ``MAX_CANDIDATES``. Facts already declared are left out.
    """

    # Pools run from the facts a team will least like to assume to the ones it will most like: the search
    # drops conditions from the front first, so what is left reads as the cheapest set that proves the pair.
    pools: dict[str, dict[tuple, Condition]] = {name: {} for name in ("shape_unique", "join_unique", "foreign_key", "join_not_null", "not_null")}

    def known(table: str, columns: Iterable[str]) -> bool:
        listed = _columns_of(schema, table)
        return listed is None or set(columns) <= listed

    def add(pool: str, condition: Condition) -> None:
        if condition.kind == "foreign_key" and not known(condition.parent, condition.parent_columns):
            return
        if known(condition.table, condition.columns) and not _declared(constraints, condition):
            pools[pool].setdefault(condition.key, condition)

    for sql in (left_sql, right_sql):
        try:
            tree = sqlglot.parse_one(sql, read=dialect)
        except (sqlglot.errors.SqlglotError, RecursionError):
            continue
        reader = _Reader(tree, schema)
        pairs: dict[tuple, tuple[_Occurrence, _Occurrence, list[tuple[str, str]]]] = {}

        def relate(a: exp.Column, b: exp.Column) -> None:
            first, second = reader.owner(a), reader.owner(b)
            if first is None or second is None or first.ident == second.ident:
                return
            slot = pairs.setdefault((first.ident, second.ident), (first, second, []))
            slot[2].append((a.name.lower(), b.name.lower()))

        for node in tree.find_all(exp.EQ):
            if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Column):
                relate(node.this, node.expression)
        for node in tree.find_all(exp.NEQ):
            # a.id <> b.id tells two rows apart: the column is the one thing that is meant to differ per row
            for side in (node.this, node.expression):
                if isinstance(side, exp.Column) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Column):
                    occ = reader.owner(side)
                    if occ is not None:
                        add("shape_unique", _make("unique", occ, [side.name.lower()], dialect))
        for node in tree.find_all(exp.In):
            query = node.args.get("query")
            inner = query.this if isinstance(query, exp.Subquery) else query
            if isinstance(node.this, exp.Column) and isinstance(inner, exp.Select) and len(inner.expressions) == 1:
                projected = inner.expressions[0]
                projected = projected.this if isinstance(projected, exp.Alias) else projected
                if isinstance(projected, exp.Column):
                    relate(node.this, projected)
        for first, second, joined in pairs.values():
            for occ, other in ((first, second), (second, first)):
                index = 0 if occ is first else 1
                columns = tuple(dict.fromkeys(pair[index] for pair in joined))
                other_columns = tuple(dict.fromkeys(pair[1 - index] for pair in joined))
                if len(columns) != len(other_columns):
                    columns = other_columns = ()
                if not columns:
                    continue
                for group in dict.fromkeys([*[(c,) for c in columns], columns]):
                    add("join_unique", _make("unique", occ, sorted(group), dialect))
                if occ.key != other.key:
                    aligned = dict(zip(columns, other_columns))
                    add("foreign_key", _make("foreign_key", occ, columns, dialect, other, [aligned[c] for c in columns]))
                for column in columns:
                    add("join_not_null", _make("not_null", occ, [column], dialect))

        def single_table(items: Iterable[exp.Expression]):
            """Columns of ``items`` grouped by the occurrence they come from (only plain columns count)."""

            by: dict[tuple, tuple[_Occurrence, list[str]]] = {}
            for item in items:
                item = item.this if isinstance(item, (exp.Alias, exp.Ordered)) else item
                if not isinstance(item, exp.Column):
                    continue
                occ = reader.owner(item)
                if occ is not None:
                    by.setdefault(occ.ident, (occ, []))[1].append(item.name.lower())
            return by.values()

        def unique_of(items: Iterable[exp.Expression]) -> None:
            for occ, names in single_table(items):
                names = sorted(dict.fromkeys(names))
                for group in dict.fromkeys([*[(n,) for n in names], tuple(names)]):
                    add("shape_unique", _make("unique", occ, group, dialect))

        for select in tree.find_all(exp.Select):
            group = select.args.get("group")
            if group is not None:
                unique_of(group.expressions)
                # grouping by k and counting distinct v per group: one row per (k, v)
                arguments = [
                    column for call in select.find_all(exp.AggFunc) if call.find_ancestor(exp.Select) is select
                    for column in call.find_all(exp.Column)
                ]
                if arguments:
                    unique_of([*group.expressions, *arguments])
            order = select.args.get("order")
            if order is not None:
                unique_of(order.expressions)
            _, total = reader.scope(select)
            if total == 1:
                unique_of(select.expressions)
        for order in tree.find_all(exp.Order):
            unique_of(order.expressions)
        for column in tree.find_all(exp.Column):
            occ = reader.owner(column)
            if occ is not None:
                add("not_null", _make("not_null", occ, [column.name.lower()], dialect))

    ordered: list[Condition] = []
    for pool in pools.values():
        for condition in pool.values():
            if condition.key not in {c.key for c in ordered}:
                ordered.append(condition)
    return ordered[:MAX_CANDIDATES]


# ---- running the prover under conditions -------------------------------------------------------


def with_conditions(constraints: Mapping[str, TableConstraints] | None, conditions: Iterable[Condition]) -> dict[str, TableConstraints]:
    """``constraints`` with the conditions added as declared facts (keyed by the queries' own table spelling)."""

    merged = {k.lower(): v for k, v in (constraints or {}).items()}
    for condition in conditions:
        facts = merged.get(condition.table, TableConstraints())
        not_null, keys, fks = set(facts.not_null), list(facts.keys), list(facts.foreign_keys)
        if condition.kind == "not_null":
            not_null.add(condition.columns[0])
        elif condition.kind == "unique":
            if tuple(condition.columns) not in [tuple(k) for k in keys]:
                keys.append(tuple(condition.columns))
        else:
            fk = (tuple(condition.columns), condition.parent, tuple(condition.parent_columns))
            if fk not in fks:
                fks.append(fk)
        merged[condition.table] = TableConstraints(not_null=frozenset(not_null), keys=tuple(keys), foreign_keys=tuple(fks))
    return merged


def _row_value(row: Mapping, column: str):
    for key, value in row.items():
        if str(key).lower() == column:
            return True, value
    return False, None


def _rows_of(tables: Mapping[str, list], name: str) -> list[Mapping] | None:
    """The rows of the counterexample table the query spells ``name`` (suffix match), or ``None``."""

    parts = name.lower().split(".")
    for table, rows in tables.items():
        other = str(table).lower().split(".")
        short = min(len(parts), len(other))
        if parts[-short:] == other[-short:]:
            return list(rows)
    return None


def broken_by(condition: Condition, tables: Mapping[str, list]) -> bool:
    """Whether ``tables`` visibly violates the condition (missing columns are read as unconstrained)."""

    rows = _rows_of(tables, condition.table)
    if condition.kind == "not_null":
        return any(seen and value is None for seen, value in (_row_value(r, condition.columns[0]) for r in rows or []))
    if condition.kind == "unique":
        seen_keys: set = set()
        for row in rows or []:
            values = [_row_value(row, c) for c in condition.columns]
            if not all(seen for seen, _ in values) or any(v is None for _, v in values):
                continue
            key = tuple(v for _, v in values)
            if key in seen_keys:
                return True
            seen_keys.add(key)
        return False
    parents = _rows_of(tables, condition.parent) or []
    have = {
        tuple(v for _, v in vals)
        for vals in ([_row_value(r, c) for c in condition.parent_columns] for r in parents)
        if all(seen for seen, _ in vals)
    }
    for row in rows or []:
        values = [_row_value(row, c) for c in condition.columns]
        if not all(seen for seen, _ in values) or any(v is None for _, v in values):
            continue
        if tuple(v for _, v in values) not in have:
            return True
    return False


def minimal_conditions(candidates: list[Condition], proves: Callable[[list[Condition]], bool], deadline: float | None = None) -> tuple[list[Condition], bool]:
    """A subset of ``candidates`` that still proves, from which the prover let no single condition be dropped.

    Chunks are dropped first (delta debugging), so a handful of provers calls handle dozens of
    candidates. The last condition is tried too: the result may be empty when the prover proves without any.
    The flag is ``False`` when the deadline cut the search short (still a proof, not minimal).
    """

    keep = list(candidates)
    parts = 2
    while keep:
        if deadline is not None and time.monotonic() > deadline:
            return keep, False
        size = max(1, -(-len(keep) // parts))
        chunks = [keep[i:i + size] for i in range(0, len(keep), size)]
        for chunk in chunks:
            trial = [c for c in keep if c not in chunk]
            if proves(trial):
                keep = trial
                parts = max(parts - 1, 2)
                break
            if deadline is not None and time.monotonic() > deadline:
                return keep, False
        else:
            if parts >= len(keep):
                break
            parts = min(len(keep), parts * 2)
    return keep, True


def always_empty(left_sql: str, prove: Callable[..., SmtEquivalenceResult], constraints: Mapping[str, TableConstraints]) -> bool:
    """Whether the prover shows, under ``constraints``, that the query returns no rows on any database.

    That is the signature of facts nothing satisfies, or of facts that empty the query: either way an
    equivalence proved under them says nothing.
    """

    try:
        counted = prove(constraints, (f"SELECT COUNT(*) AS n FROM ({left_sql.strip().rstrip(';')}) AS q", "SELECT 0 AS n"))
    except Exception:  # noqa: BLE001 - a control that cannot run finds nothing
        return False
    return counted.status is SmtStatus.PROVEN_EQUIVALENT


_TEXT_TYPES = {"VARCHAR": "STRING", "CHAR": "STRING", "TEXT": "STRING"}  # SQL names the synthetic databases do not know


def _independence(
    left_sql: str,
    right_sql: str,
    constraints: Mapping[str, TableConstraints],
    *,
    schema: Mapping[str, list[str]] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    dialect: str = "bigquery",
) -> str:
    """``"independent"`` when each query returns one and the same result on every test database that meets ``constraints``,
    ``"dependent"`` when they do not, ``"not_run"`` when no test database ran on both queries (the engine rejects them) and
    ``"error"`` when the check itself failed.

    Conditions that make both queries constant (a unique key under ``HAVING COUNT(*) > 1``, a self join on a
    key that asks for two different rows) prove the pair for the wrong reason: the proof is real, but the
    queries no longer ask anything of the data.
    """

    from .refute import ExecutionError, infer_schema, repair_foreign_keys
    from .result_equivalence import DataRules, DatasetRunner, _normalize_type
    from .targeted_data import database_suite

    def short(name: str) -> str:
        return name.lower().split(".")[-1]

    def known_types(declared: Mapping[str, Mapping[str, str]] | None) -> dict[str, dict[str, str]]:
        # A declared type the synthetic databases cannot build (TIME, ENUM) is left out and the column's type is inferred;
        # without this one such column would stop the check from running at all, and a check that cannot run fails closed.
        usable: dict[str, dict[str, str]] = {}
        for table, columns in (declared or {}).items():
            for column, kind in columns.items():
                try:
                    usable.setdefault(table, {})[column] = _normalize_type(_TEXT_TYPES.get(kind.strip().upper(), kind))
                except ValueError:
                    continue
        return usable

    try:
        shaped = infer_schema([left_sql, right_sql], schema, known_types(types), dialect)
        rules = {
            short(name): DataRules(not_null=frozenset(c.not_null), keys=tuple(tuple(k) for k in c.keys))
            for name, c in constraints.items() if short(name) in shaped
        }
        foreign = [
            (short(name), fk[0][0], short(fk[1]), fk[2][0])
            for name, c in constraints.items() if short(name) in shaped
            for fk in c.foreign_keys if len(fk[0]) == 1 and short(fk[1]) in shaped
        ]
        seen: dict[str, set] = {"left": set(), "right": set()}
        ran = 0
        with DatasetRunner(shaped, dialect) as runner:
            for sql in (left_sql, right_sql):
                for labeled in database_suite(sql, shaped, rules, dialect=dialect, random_seeds=range(1, 4)):
                    dataset = repair_foreign_keys(labeled.dataset, foreign, rules)
                    try:
                        a = runner.run(left_sql, dataset, timeout=2.0)
                        b = runner.run(right_sql, dataset, timeout=2.0)
                    except ExecutionError:
                        continue
                    ran += 1
                    seen["left"].add(tuple(sorted(repr(r) for r in a.rows)))
                    seen["right"].add(tuple(sorted(repr(r) for r in b.rows)))
    except Exception:  # noqa: BLE001 - reported as "error"; the caller decides what a failed check means
        return "error"
    if ran == 0:
        return "not_run"
    return "independent" if len(seen["left"]) <= 1 and len(seen["right"]) <= 1 else "dependent"


def data_independent(left_sql: str, right_sql: str, constraints: Mapping[str, TableConstraints], **options) -> bool | None:
    """Whether each query returns one and the same result on every test database that meets ``constraints``; ``None`` when
    the queries could not be run to find out (see :func:`_independence`)."""

    return {"independent": True, "dependent": False}.get(_independence(left_sql, right_sql, constraints, **options))


def jointly_satisfiable(conditions: Iterable[Condition], constraints: Mapping[str, TableConstraints] | None = None) -> bool:
    """Whether one non-empty database meets every condition and every declared fact at once.

    Builds it: two rows per table whose columns hold the row's number (distinct, so keys are unique; never
    NULL; a child column then equals a parent column's value), and reads each fact back off it. A set that
    no database satisfies would let a prover "prove" anything.
    """

    from .constraint_dependence import guarantees_of

    facts = list(conditions) + [
        Condition(g.kind, g.table.lower(), g.columns, g.parent.lower(), g.parent_columns) for g in guarantees_of(constraints or {})
    ]
    columns: dict[str, set[str]] = {}
    for fact in facts:
        if fact.kind == "foreign_key" and len(fact.columns) != len(fact.parent_columns):
            return False
        columns.setdefault(fact.table, set()).update(fact.columns)
        if fact.kind == "foreign_key":
            columns.setdefault(fact.parent, set()).update(fact.parent_columns)
    database = {table: [{c: row for c in cols} for row in (1, 2)] for table, cols in columns.items()}
    return not any(broken_by(fact, database) for fact in facts)


MAX_ALTERNATIVES = 6


def add_conditions(
    left_sql: str,
    right_sql: str,
    result: SmtEquivalenceResult,
    prove: Callable[..., SmtEquivalenceResult],
    *,
    schema: Mapping[str, list[str]] | None = None,
    constraints: Mapping[str, TableConstraints] | None = None,
    types: Mapping[str, Mapping[str, str]] | None = None,
    dialect: str = "bigquery",
    wall_seconds: float = DEFAULT_WALL_SECONDS,
) -> SmtEquivalenceResult:
    """``result`` unchanged, or a ``PROVEN_CONDITIONALLY`` result when conditions settle a pair it did not prove.

    ``prove(constraints, pair=None)`` runs the prover on the pair (or on another pair, names ignored) with the given
    constraints. A set of conditions is passed over for another set when no database meets it, when the prover then
    shows the query is always empty (a prover given contradictory facts proves that and everything else), or when it
    makes both queries return one result on every test database (see :func:`data_independent`; when that check
    fails, the conditions are not cleared, and when the engine rejects the queries the verdict says it was not checked).
    """

    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return result
    candidates = candidate_conditions(left_sql, right_sql, schema=schema, constraints=constraints, dialect=dialect)
    if not candidates:
        return result
    deadline = time.monotonic() + wall_seconds

    proofs: dict[frozenset, SmtEquivalenceResult] = {}

    def attempt(chosen: Iterable[Condition]) -> SmtEquivalenceResult:
        chosen = list(chosen)
        key = frozenset(c.key for c in chosen)
        if key not in proofs:
            proofs[key] = prove(with_conditions(constraints, chosen))
        return proofs[key]

    def proves(chosen: list[Condition]) -> bool:
        return attempt(chosen).status is SmtStatus.PROVEN_EQUIVALENT

    # Foreign keys come second: their rules can rewrite one side out of the shape the other side's proof needs, so a
    # pair the other facts prove is never lost to them.
    without_keys = [c for c in candidates if c.kind != "foreign_key"]
    for universe in dict.fromkeys([tuple(without_keys), tuple(candidates)]):
        if universe and proves(list(universe)):
            candidates = list(universe)
            break
    else:
        return result
    pool, blocked = candidates, []
    needed: list[Condition] = []
    minimal = True
    unchecked = False
    for _ in range(MAX_ALTERNATIVES):
        if time.monotonic() > deadline:
            return result
        if blocked and not proves(pool):
            break
        chosen, minimal = minimal_conditions(pool, proves, deadline)
        if not chosen:
            return result
        merged = with_conditions(constraints, chosen)

        def vacuous() -> bool:
            nonlocal unchecked
            unchecked = False
            same = _independence(left_sql, right_sql, merged, schema=schema, types=types, dialect=dialect)
            # A check that failed does not clear the conditions. Queries the engine rejects outright (a MySQL-only GROUP BY)
            # leave the guard with nothing to run: the verdict stands, and says so.
            unchecked = same == "not_run"
            if not (always_empty(left_sql, prove, merged) or same in ("independent", "error")):
                return False
            # A query written to be empty or constant (WHERE 1 = 0, SELECT 0) is what the other side is meant to equal.
            declared = {k.lower(): v for k, v in (constraints or {}).items()}
            return not (
                always_empty(left_sql, prove, declared) or always_empty(right_sql, prove, declared)
                or data_independent(left_sql, left_sql, declared, schema=schema, types=types, dialect=dialect) is True
                or data_independent(right_sql, right_sql, declared, schema=schema, types=types, dialect=dialect) is True
            )

        constant = not jointly_satisfiable(chosen, constraints) or vacuous()
        if not constant:
            needed = chosen
            break
        # The conditions make both queries constant: look for a set that avoids one of them.
        blocked = [*blocked, chosen[0]]
        pool = [c for c in candidates if c not in blocked]
    if not needed:
        return result
    if result.counterexample is not None and not any(broken_by(c, result.counterexample.tables) for c in needed):
        return result  # refuted on a database that meets every condition: nothing is conditional about it
    final = attempt(needed)  # the proof the verdict stands on: its own assumptions, not those of the full set
    if final.status is not SmtStatus.PROVEN_EQUIVALENT:
        return result
    reason = "equivalent whenever: " + describe(needed)
    if not minimal:
        reason += " (the search for fewer conditions ran out of time)"
    if unchecked:
        reason += " (not checked for conditions that make both queries constant: the queries cannot run on test databases)"
    return SmtEquivalenceResult(
        SmtStatus.PROVEN_CONDITIONALLY,
        reason,
        counterexample=result.counterexample,
        assumptions=tuple(final.assumptions),
        conditions=tuple(needed),
    )


def describe(conditions: Iterable[Condition]) -> str:
    """The conditions in one line; a unique key whose columns are all NOT NULL reads as a primary key."""

    conditions = list(conditions)
    not_null = {(c.table, c.columns[0]) for c in conditions if c.kind == "not_null"}
    merged = {
        c.key for c in conditions
        if c.kind == "unique" and all((c.table, column) in not_null for column in c.columns)
    }
    covered = {(c.table, column) for c in conditions if c.key in merged for column in c.columns}
    parts = []
    for c in conditions:
        if c.key in merged:
            parts.append(f"{c.table}({', '.join(c.columns)}) is a primary key (unique, never NULL)")
        elif c.kind == "not_null" and (c.table, c.columns[0]) in covered:
            continue
        else:
            parts.append(c.text)
    return "; ".join(parts)


def conditions_json(result) -> list[dict]:
    return [c.to_json() for c in getattr(result, "conditions", ())]
