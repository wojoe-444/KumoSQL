"""Minimize a set of tables while some must stay: the simplest SQL that keeps every protected table.

``minimize_tables(tables, protected, sources=...)`` takes any number of table definitions (name to
one SELECT; a name a query reads that is not a table is a source) and the names of the
**protected** tables. It returns a new set of tables in which

* every protected table still exists under the same name, with the same output columns in the
  same order, and is **proved** to return the same rows as before
  (:func:`kumosql.pipeline_equivalence.prove_models`; agreement on test data never counts), and
* the total complexity is as low as the search could make it.

Complexity is the repo's sqlfluff measure summed over the tables plus one per table
(:func:`pipeline_score`; see ``docs/table-minimization.md``). Unprotected tables may be dropped,
folded into their readers, merged into an equal table, pruned of columns no reader uses, or
rewritten. Each step is kept only when every protected table of the whole new set is proved equal
to the original; anything unknown is rejected, so the worst outcome is the input unchanged.

The search is greedy with compound moves: folding a table into its readers is scored after the
readers are simplified (:mod:`kumosql.sql_simplify`), because a fold on its own raises the score.
A second start folds every unprotected table into its readers at once, and the cheaper proved
result wins. Proofs are cached per protected table and the SQL it depends on.

``minimize_case(case)`` is the entry point for the table-minimization eval harness
(``case = {id, dialect, sources, tables, protected}``, returns ``{name: SQL}``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import time
from typing import Callable, Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import is_cte_reference, set_with_clause, with_clause
from .minimizer_identity import ambiguous, fresh_catalogs, internal_name, single_query
from .pipeline import Pipeline
from .pipeline_types import Model, Target
from .prover_schema import ProverSchema, _Builder, _select_names
from .refactor import SUFFIX, _inline_into, _Reads, _redirect, _table_for, _table_nodes, check_observable

MAX_SQL_CHARS = 200_000


class MinimizationError(ValueError):
    """The input cannot be minimized as given (unknown protected table, a cycle, a name clash)."""


# ------------------------------------------------------------------ score


def _structural(sql: str, unscored: list[int] | None = None) -> float:
    from .formatting import complexity

    try:
        return float(complexity(sql).score)
    except Exception:  # noqa: BLE001 - sqlfluff cannot parse it: fall back to a size proxy
        if unscored is not None:
            unscored[0] += 1
        return round(len(sql) / 40, 1)


def pipeline_score(tables: Mapping[str, str]) -> float:
    """Total complexity of a set of tables: the sqlfluff structural score of each, plus one per table."""

    return round(sum(_structural(sql) for sql in tables.values()) + len(tables), 2)


# ------------------------------------------------------------------ names


def _parts(name: str, fold: bool = False) -> tuple[str, ...]:
    """The parts of a table name as written. Case is kept (BigQuery names are case-sensitive); ``fold`` lower-cases
    them for dialects whose unquoted names are not."""

    parts = tuple(p.strip("`").strip() for p in name.strip().strip("`").split("."))
    if not all(parts) or len(parts) > 3:
        raise MinimizationError(f"{name!r} is not a table name")
    return tuple(p.lower() for p in parts) if fold else parts


def _table_parts(table: exp.Table, fold: bool = False) -> tuple[str, ...]:
    parts = tuple(p for p in (table.catalog, table.db, table.name) if p)
    return tuple(p.lower() for p in parts) if fold else parts


def _set_parts(table: exp.Table, parts: Sequence[str]) -> None:
    table.set("this", exp.to_identifier(parts[-1]))
    table.set("db", exp.to_identifier(parts[-2]) if len(parts) > 1 else None)
    table.set("catalog", exp.to_identifier(parts[-3]) if len(parts) > 2 else None)


def _rename_tables(tree: exp.Expression, mapping: Mapping[tuple[str, ...], Sequence[str]], fold: bool = False) -> exp.Expression:
    for table in list(tree.find_all(exp.Table)):
        parts = _table_parts(table, fold)
        if not parts or is_cte_reference(table):
            continue
        new = mapping.get(parts)
        if new is not None:
            _set_parts(table, new)
    return tree


# ------------------------------------------------------------------ result


@dataclass
class ProtectedProof:
    """How one protected table is known to be unchanged."""

    table: str
    status: str  # "unchanged" (its SQL and everything it reads are as given) or "proved"
    reason: str = ""
    assumptions: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"table": self.table, "status": self.status, "reason": self.reason, "assumptions": self.assumptions}


@dataclass
class TableMinimization:
    tables: dict[str, str]  # the new set: name -> SQL, in the input dialect
    original: dict[str, str]
    protected: list[str]
    proofs: dict[str, ProtectedProof]
    score: float
    original_score: float
    moves: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)  # shared tables factored out of repeated queries
    tried: int = 0
    rejected: int = 0
    rejected_moves: list[dict] = field(default_factory=list)
    stopped: str = ""
    seconds: float = 0.0
    unscored: int = 0

    @property
    def improved(self) -> bool:
        return self.score < self.original_score

    def to_json(self) -> dict:
        return {
            "tables": self.tables,
            "protected": self.protected,
            "proofs": {k: v.to_json() for k, v in self.proofs.items()},
            "score": self.score,
            "original_score": self.original_score,
            "moves": self.moves,
            "removed": self.removed,
            "changed": self.changed,
            "added": self.added,
            "tried": self.tried,
            "rejected": self.rejected,
            "rejected_moves": self.rejected_moves,
            "stopped": self.stopped,
            "seconds": round(self.seconds, 2),
            "complexity_unscored_tables": self.unscored,
            "evidence": "proof: every changed protected table is proved equal to its original by prove_models",
        }


# ------------------------------------------------------------------ setup


def _source_facts(spec: object) -> tuple[dict[str, str], list[str], list[str]]:
    """``(columns with types, key, not_null)`` from ``{"columns": {...}, "key": [...], "not_null": [...]}``,
    a ``{column: type}`` mapping or a list of column names."""

    if spec is None:
        return {}, [], []
    if isinstance(spec, Mapping) and "columns" in spec:
        columns = spec["columns"]
        if isinstance(columns, Mapping):
            typed = {str(c): str(t) for c, t in columns.items()}
        else:
            typed = {str(c): "" for c in columns}
        return typed, [str(c) for c in spec.get("key") or ()], [str(c) for c in spec.get("not_null") or ()]
    if isinstance(spec, Mapping):
        return {str(c): str(t) for c, t in spec.items()}, [], []
    if isinstance(spec, (list, tuple)):
        return {str(c): "" for c in spec}, [], []
    raise MinimizationError("a source is described by its columns")


@dataclass
class _Setup:
    pipeline: Pipeline
    original: dict[str, str]  # internal key -> internal SQL
    protected: list[str]
    opaque: frozenset[str]  # tables that could not be read: never rewritten or folded
    pinned: frozenset[str]  # tables an unreadable table reads: kept and proved like protected ones
    fixed: frozenset[str]  # unreadable or non-deterministic tables: never folded, merged, pruned or rewritten
    reads: _Reads
    source_columns: dict[str, list[str]]
    builder_facts: list[tuple]  # (key, columns, not_null, keys, types) for sources
    timeout_ms: int
    unscored: list[int] = field(default_factory=lambda: [0])
    scores: dict[str, float] = field(default_factory=dict)
    names_cache: dict[tuple, tuple[str, ...]] = field(default_factory=dict)
    back: dict = field(default_factory=dict)  # internal parts -> the name as the input spelled it
    catalogs: tuple[str, str] = ("kumo_min", "kumo_min1")  # internal catalogs of bare and two-part names
    fold: bool = False  # the dialect's unquoted names are case-insensitive
    ambiguous: frozenset[str] = frozenset()  # names that differ from another only by case
    touching: frozenset[str] = frozenset()  # tables that are one of those, or read one
    protected_opaque: tuple[str, ...] = ()  # protected tables that are not one query: returned as given
    proof_cache: dict = field(default_factory=dict)
    schema_cache: dict = field(default_factory=dict)
    forms_cache: dict = field(default_factory=dict)
    all_reads: dict = field(default_factory=dict)
    # surviving tables that must stay proved equal to the original on the columns they keep
    checked: frozenset[str] = frozenset()
    keep_columns: dict[str, frozenset[str]] = field(default_factory=dict)  # never pruned
    factor: bool = False
    new_tables: dict = field(default_factory=dict)  # body fingerprint -> internal key of a factored table
    new_names: dict = field(default_factory=dict)  # internal key of a factored table -> the name it is given

    def score(self, sql: str) -> float:
        if sql not in self.scores:
            self.scores[sql] = _structural(sql, self.unscored)
        return self.scores[sql]

    def cost(self, state: Mapping[str, str]) -> tuple[float, int]:
        return round(sum(self.score(s) for s in state.values()) + len(state), 2), sum(len(s) for s in state.values())


def _with_incremental(fixed, incremental):
    """``fixed`` plus the incremental tables: a table that builds on its own earlier rows cannot be read as its SELECT."""

    names = [str(n) for n in (incremental or ())]
    if not names:
        return fixed
    if isinstance(fixed, Mapping):
        return {**{name: None for name in names}, **fixed}
    return [*(fixed or ()), *names]


def _prepare(tables, protected, sources, dialect, timeout_ms, fixed=None, checked=(), keep_columns=None, extra_texts=()):
    if not isinstance(tables, Mapping) or not tables:
        raise MinimizationError("give at least one table")
    fold = dialect != "bigquery"
    names: dict[tuple[str, ...], str] = {}  # user parts -> user spelling
    for name in tables:
        parts = _parts(str(name), fold)
        if parts in names:
            raise MinimizationError(f"{name!r} is given twice")
        names[parts] = str(name)
    source_specs: dict[tuple[str, ...], object] = {}
    for name, spec in (sources or {}).items():
        parts = _parts(str(name), fold)
        if parts in names:
            raise MinimizationError(f"{name!r} is both a source and a table")
        source_specs[parts] = spec

    # internal names live under catalogs that occur nowhere in the input, so no real table can take their place
    catalogs = fresh_catalogs([*map(str, tables), *map(str, tables.values()), *map(str, sources or ()), *extra_texts])
    mapping: dict[tuple[str, ...], tuple[str, str, str]] = {}
    back: dict[tuple[str, ...], tuple[str, ...]] = {}
    for parts in [*names, *source_specs]:
        internal = internal_name(parts, catalogs)
        mapping[parts] = internal
        back[internal] = parts
    fixed_specs: dict[tuple[str, ...], object] = {}
    for name in (fixed or ()):
        parts = _parts(str(name), fold)
        if parts not in names:
            raise MinimizationError(f"fixed table {name!r} is not one of the tables")
        fixed_specs[parts] = fixed.get(name) if isinstance(fixed, Mapping) else None
    # a source named only in the SQL (no columns given) still gets an internal name
    parsed: dict[tuple[str, ...], exp.Expression | None] = {}
    for parts, user in names.items():
        if parts in fixed_specs:  # read as it is, never rewritten, like a table that is not a query
            parsed[parts] = None
            continue
        # a script (more than one statement) is not read as its first SELECT: it stays opaque
        tree = single_query(tables[user], dialect)
        if tree is not None and dialect != "bigquery":
            tree = single_query(tree.sql(dialect="bigquery"), "bigquery")
        parsed[parts] = tree
        if tree is None:
            continue
        for table in tree.find_all(exp.Table):
            ref = _table_parts(table, fold)
            if not ref or is_cte_reference(table) or ref in mapping:
                continue
            internal = internal_name(ref, catalogs)
            mapping[ref] = internal
            back[internal] = ref
    if len(set(mapping.values())) != len(mapping):  # cannot happen with fresh catalogs; never prove on merged tables
        raise MinimizationError("two names map to the same table")

    protected_keys = []
    for name in protected:
        parts = _parts(str(name), fold)
        if parts not in names:
            raise MinimizationError(f"protected table {name!r} is not one of the tables")
        key = ".".join(mapping[parts])
        if key not in protected_keys:
            protected_keys.append(key)

    models: dict[str, Model] = {}
    original: dict[str, str] = {}
    opaque = set()
    for parts, user in names.items():
        internal = mapping[parts]
        key = ".".join(internal)
        tree = parsed[parts]
        if tree is None:
            sql = tables[user]
            opaque.add(key)
            kind = "operation"  # not a query: read as it is, never rewritten
        else:
            sql = _rename_tables(tree, mapping, fold).sql(dialect="bigquery")
            kind = "table"
        models[key] = Model(Target(*internal), kind, sql)
        if tree is not None:
            original[key] = sql
    source_targets = {}
    source_schema = {}
    facts = []
    source_columns = {}
    for parts, spec in source_specs.items():
        internal = mapping[parts]
        key = ".".join(internal)
        typed, key_cols, not_null = _source_facts(spec)
        source_targets[key] = Target(*internal)
        if typed:
            source_schema[key] = typed
            source_columns[key] = [c.lower() for c in typed]
        facts.append((key, list(typed), [*not_null, *key_cols], [key_cols] if key_cols else [],
                      {c: t for c, t in typed.items() if t}))
    for internal, parts in back.items():
        key = ".".join(internal)
        if key not in models and key not in source_targets:
            source_targets[key] = Target(*internal)
    for parts, spec in fixed_specs.items():  # columns of a fixed table let the prover expand a star over it
        if spec:
            typed, _key_cols, _not_null = _source_facts(spec)
            key = ".".join(mapping[parts])
            source_columns[key] = [c.lower() for c in typed]
    pipeline = Pipeline(models=models, sources=source_targets, source_schema=source_schema)
    upstream = pipeline.upstream
    done: set[str] = set()
    for start in models:  # depth-first walk; a table met again on its own path is a cycle
        stack = [(start, iter(sorted(upstream.get(start, ()))))]
        path = {start}
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                path.discard(node)
                done.add(node)
            elif child in path:
                raise MinimizationError("the tables read each other in a cycle")
            elif child in models and child not in done:
                path.add(child)
                stack.append((child, iter(sorted(upstream.get(child, ())))))
    protected_opaque = tuple(k for k in protected_keys if k in opaque)
    protected_keys = [k for k in protected_keys if k not in opaque]  # an unreadable table is returned as given
    # an unreadable table's text is not renamed, so any table whose name it mentions counts as read
    words = {w.lower() for key in opaque for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", models[key].sql)}
    mentioned = {key for key in original if key.split(".")[-1].lower() in words}
    # names that differ from another only by case are different tables here (BigQuery's default), but would be one in
    # a dataset set to case-insensitive names: such a table, and every table that reads one, is kept as written
    clash = {".".join(parts) for parts in ambiguous(back)}
    touching = {key for key, sql in original.items() if key in clash or _physical_keys(pipeline, sql) & clash}
    pinned = frozenset(r for r in mentioned | touching | {r for key in opaque for r in upstream.get(key, ())}
                       if r in original and r not in protected_keys)
    fixed = frozenset(opaque) | touching | {key for key, sql in original.items() if _volatile(sql)}
    setup = _Setup(pipeline, original, protected_keys, frozenset(opaque), pinned, fixed, _Reads(pipeline), source_columns,
                   facts, timeout_ms)
    setup.back = back
    setup.catalogs = catalogs
    setup.fold = fold
    setup.ambiguous = frozenset(clash)
    setup.touching = frozenset(touching)
    setup.protected_opaque = protected_opaque
    for name in checked or ():
        parts = _parts(str(name), fold)
        if parts not in names:
            raise MinimizationError(f"checked table {name!r} is not one of the tables")
        key = ".".join(mapping[parts])
        if key in original and key not in protected_keys and key not in pinned:
            setup.checked = setup.checked | {key}
    for name, cols in (keep_columns or {}).items():
        parts = _parts(str(name), fold)
        if parts in names:
            setup.keep_columns[".".join(mapping[parts])] = frozenset(str(c).lower() for c in cols)
    return setup, names, mapping, back


def _physical_keys(pipeline: Pipeline, sql: str) -> set[str]:
    """Keys of every table and source a query reads (WITH tables aside)."""

    from .refactor import _table_nodes

    found = set()
    try:
        for table in _table_nodes(sqlglot.parse_one(sql, read="bigquery")):
            key = pipeline.resolve(table)
            if key:
                found.add(key)
    except sqlglot.errors.SqlglotError:
        pass
    return found


_VOLATILE_NODES = tuple(
    getattr(exp, name) for name in (
        "Rand", "Randn", "Uuid", "CurrentTimestamp", "CurrentDate", "CurrentDatetime", "CurrentTime", "CurrentUser",
        "AnyValue", "ArrayAgg", "GroupConcat", "Limit", "TableSample",
    ) if hasattr(exp, name)
)
_VOLATILE_WORDS = re.compile(
    r"\b(GENERATE_UUID|SESSION_USER|_TABLE_SUFFIX|SYSTEM_TIME|ANY_VALUE|ARRAY_AGG|STRING_AGG|LIMIT|RAND|CURRENT_\w+)\b",
    re.IGNORECASE,
)


def _volatile(sql: str) -> bool:
    """Whether a query can return different rows when evaluated twice or in another place: random and
    time functions, a representative or unordered aggregate, LIMIT, sampling, wildcard suffixes or time travel."""

    if _VOLATILE_WORDS.search(sql):
        return True
    try:
        return any(True for _ in sqlglot.parse_one(sql, read="bigquery").find_all(*_VOLATILE_NODES))
    except sqlglot.errors.SqlglotError:
        return True


# ------------------------------------------------------------------ columns and proofs


def _output_names(sql: str, columns: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """Output column names of a query, expanding stars from known columns; ``()`` when unknown."""

    names = _select_names(sql)
    if names:
        return tuple(n.lower() for n in names)
    try:
        from sqlglot.optimizer.qualify import qualify

        nested: dict = {}
        for key, cols in columns.items():
            parts = key.split(".")
            if len(parts) != 3 or not cols:
                continue
            nested.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = {c: "STRING" for c in cols}
        tree = qualify(sqlglot.parse_one(sql, read="bigquery"), schema=nested, dialect="bigquery",
                       validate_qualify_columns=False, quote_identifiers=False)
        while isinstance(tree, exp.Subquery):
            tree = tree.this
        while isinstance(tree, exp.SetOperation):
            tree = tree.this
        if not isinstance(tree, exp.Select):
            return ()
        out = []
        for item in tree.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                return ()
            if not item.alias_or_name:
                return ()
            out.append(item.alias_or_name.lower())
        return tuple(out)
    except Exception:  # noqa: BLE001 - unknown names are reported as unknown
        return ()


def _state_columns(setup: _Setup, state: Mapping[str, str]) -> dict[str, list[str]]:
    """Columns of every source and table of ``state`` (tables in pipeline order)."""

    columns: dict[str, list[str]] = dict(setup.source_columns)
    pending = dict(state)
    for _ in range(len(pending) + 1):
        progressed = False
        for key in list(pending):
            reads = setup.reads(pending[key])
            if any(r in pending and r != key for r in reads):
                continue
            sql = pending.pop(key)
            signature = (sql, tuple((r, tuple(columns.get(r, ()))) for r in sorted(reads)))
            if signature not in setup.names_cache:
                setup.names_cache[signature] = _output_names(sql, columns)
            names = setup.names_cache[signature]
            if names:
                columns[key] = list(names)
            progressed = True
        if not pending or not progressed:
            break
    return columns


def _schema(setup: _Setup, columns: Mapping[str, Sequence[str]]) -> ProverSchema:
    signature = tuple(sorted((k, tuple(v)) for k, v in columns.items()))
    if signature not in setup.schema_cache:
        builder = _Builder()
        for key, cols, not_null, keys, types in setup.builder_facts:
            builder.add(key, cols, not_null, keys, source="minimize", types=types or None)
        for key, cols in columns.items():
            builder.add(key, cols)
        setup.schema_cache[signature] = builder.build()
    return setup.schema_cache[signature]


def _check(setup: _Setup, state: Mapping[str, str], keys: Iterable[str] | None = None) -> tuple[bool, list[str], str]:
    """Every protected table (or ``keys``) of ``state`` keeps its output names and is proved equal."""

    if keys is None:
        keys = [*setup.protected, *sorted(setup.pinned), *sorted(k for k in setup.checked if k in state)]
    keys = list(keys)
    if any(len(sql) > MAX_SQL_CHARS for sql in state.values()):
        return False, [], "the SQL grew too large"
    for key in keys:
        if key not in state:
            return False, [], f"{key} no longer exists"
    missing = _dangling(setup, state, keys)
    if missing:
        return False, [], f"{keys[0] if len(keys) == 1 else 'a protected table'} reads {missing}, which no longer exists"
    before = _state_columns(setup, setup.original)
    after = _state_columns(setup, state)
    for key in keys:
        if state[key] == setup.original[key] and _closure(setup, state, key) == _closure(setup, setup.original, key):
            continue
        if not after.get(key) or after.get(key) != before.get(key):
            if key in setup.checked and _kept_in_order(after.get(key), before.get(key)):
                continue  # a checked table may lose columns; it is compared on the ones it keeps
            return False, [], f"{key}: output columns are not known to be the same"
    for key, sql in state.items():
        if sql != setup.original.get(key):
            captured = _captured(setup, sql)
            if captured:
                return False, [], f"{key}: a WITH table named {captured} would hide the table of that name"
    known = None
    assumptions: list[str] = []
    for key in keys:
        if state[key] == setup.original[key] and _closure(setup, state, key) == _closure(setup, setup.original, key):
            continue
        try:
            full = [_expanded(setup, sqls, key, set()) for sqls in (setup.original, state)]
        except (sqlglot.errors.SqlglotError, RecursionError) as error:
            return False, [], f"{key}: {error}"
        if any(_repeated_ctes(tree) for tree in full):
            # inlined tables would put two WITH tables of one name in one query, where one can capture the
            # other's readers; such a proof is not trusted
            return False, [], f"{key}: two WITH tables share a name once the tables are inlined"
        narrowed = tuple(after[key]) if after.get(key) != before.get(key) else None
        ok, used, why = _prove_frontier(setup, state, key, before, narrowed)
        if not ok and narrowed is None:
            # the whole pipeline prover: layer lemmas, then everything inlined; it sees the original
            # tables and the new ones (``__after`` copies) side by side
            if known is None:
                known = {**before, **{k + SUFFIX: cols for k, cols in after.items() if k in state}}
            ok, used, why = check_observable(
                setup.pipeline, state, [key], setup.reads, schema=_schema(setup, known), timeout_ms=setup.timeout_ms,
                cache=setup.proof_cache, declared=[],
            )
        if not ok:
            return False, [], why
        assumptions.extend(a for a in used if a not in assumptions)
    return True, assumptions, ""


def _kept_in_order(kept, original) -> bool:
    """Whether ``kept`` is ``original`` with some columns left out, in the same order."""

    if not kept or not original:
        return False
    remaining = iter(original)
    return all(any(name == other for other in remaining) for name in kept)


def _repeated_ctes(tree: exp.Expression) -> bool:
    names = [c.alias_or_name.lower() for c in tree.find_all(exp.CTE)]
    return len(names) != len(set(names))


def _captured(setup: _Setup, sql: str) -> str:
    """The name of a table that a WITH table of ``sql`` would capture once names are written as given, or ``""``."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return ""
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    if not ctes:
        return ""
    for table in tree.find_all(exp.Table):
        parts = _table_parts(table)
        given = setup.back.get(parts)
        if given is not None and len(given) == 1 and given[0].lower() in ctes:
            return given[0]
    return ""


def _shared(setup: _Setup, state: Mapping[str, str]) -> set[str]:
    """Tables whose SQL, and the SQL of everything they read, is the same in ``state`` as in the input."""

    same: dict[str, bool] = {}

    def check(key: str, trail: frozenset[str]) -> bool:
        if key not in same:
            if key in trail or state.get(key) != setup.original.get(key):
                return False
            same[key] = all(check(r, trail | {key}) for r in setup.reads(state[key]) if r in setup.original)
        return same[key]

    return {key for key in state if key in setup.original and check(key, frozenset())}


def _expanded(setup: _Setup, sqls: Mapping[str, str], key: str, stop: set[str], trail: tuple = ()) -> exp.Expression:
    """``key``'s query with every table it reads inlined as a derived table, down to sources and ``stop``.

    When two WITH tables then share a name, each gets a name of its own (``_unique_ctes``), so that tables
    that use the same WITH names (``source``, ``renamed``) can sit in one query.
    """

    tree = sqlglot.parse_one(sqls[key], read="bigquery")
    for table in list(_table_nodes(tree)):
        found = setup.pipeline.resolve(table)
        if found is None or found in stop or found not in sqls or found in trail:
            continue
        body = _expanded(setup, sqls, found, stop, (*trail, key))
        table.replace(exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(table.alias or table.name))))
    if not trail and _repeated_ctes(tree):
        counter = [0]
        for clause in reversed(list(tree.find_all(exp.With))):  # the innermost first
            if clause.parent is not None:
                _unique_ctes(clause.parent, counter)
    return tree


def _unique_ctes(query: exp.Expression, counter: list[int]) -> None:
    """Rename ``query``'s WITH tables ``<name>__kumo<n>``, references included, in place.

    Only the plain case is renamed: not recursive, each name once, and no WITH inside that defines one of
    the names again. A WITH table is seen by the WITH tables after it and by the main query (with their
    subqueries); in its own body and the ones before it, the name is a table of the pipeline. Anything else
    is left as it is, and the repeated-name check still refuses it.
    """

    clause = with_clause(query)
    if clause is None or clause.args.get("recursive"):
        return
    ctes = list(clause.expressions)
    names = [c.alias_or_name.lower() for c in ctes]
    if len(names) != len(set(names)):
        return
    for inner in query.find_all(exp.With):
        if inner is not clause and {c.alias_or_name.lower() for c in inner.expressions} & set(names):
            return
    renamed: dict[str, str] = {}
    for name in names:
        counter[0] += 1
        renamed[name] = f"{name}__kumo{counter[0]}"

    def visible(node: exp.Expression, seen: Mapping[str, str]) -> None:
        for table in list(node.find_all(exp.Table)):
            name = table.name.lower()
            if table.db or table.catalog or name not in seen:
                continue
            if not table.alias:  # columns qualified by the WITH name keep naming it
                table.set("alias", exp.TableAlias(this=exp.to_identifier(table.name)))
            table.set("this", exp.to_identifier(seen[name]))

    for index, cte in enumerate(ctes):
        visible(cte.this, {n: renamed[n] for n in names[:index]})
    set_with_clause(query, None)
    visible(query, renamed)
    for index, cte in enumerate(ctes):
        cte.set("alias", exp.TableAlias(this=exp.to_identifier(renamed[names[index]])))
    set_with_clause(query, clause)


def _prove_frontier(setup: _Setup, state: Mapping[str, str], key: str, before,
                    narrowed: tuple[str, ...] | None = None) -> tuple[bool, list[str], str]:
    """Prove ``key`` equal by inlining both versions only down to the tables both sides still share.

    ``narrowed`` names the columns a checked table kept: the original is compared on those columns only.
    """

    shared = _shared(setup, state)
    outcome = (False, [], f"{key}: not proved")
    # first stop at the tables both sides still share; then inline everything down to the sources,
    # which finds a table that was merged into an equal one
    for stop in (shared, set()) if shared else (set(),):
        outcome = _prove_expanded(setup, state, key, before, stop, narrowed)
        if outcome[0]:
            break
    return outcome


def _projected(tree: exp.Expression, columns: Sequence[str]) -> exp.Expression:
    """``SELECT columns FROM (tree)``: the bag of rows of ``tree`` on those columns, in that order."""

    return exp.select(*(exp.column(exp.to_identifier(c)) for c in columns)).from_(
        exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier("kumo_kept"))))


def _prove_expanded(setup: _Setup, state: Mapping[str, str], key: str, before, stop: set[str],
                    narrowed: tuple[str, ...] | None = None) -> tuple[bool, list[str], str]:
    from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic
    from kumo_sql_prover.smt_equivalence import SmtStatus

    shared = stop
    try:
        original = _expanded(setup, setup.original, key, shared)
        if narrowed is not None:
            original = _projected(original, narrowed)
        left = original.sql(dialect="bigquery")
        right = _expanded(setup, state, key, shared).sql(dialect="bigquery")
    except (sqlglot.errors.SqlglotError, RecursionError) as error:
        return False, [], f"{key}: {error}"
    if max(len(left), len(right)) > MAX_SQL_CHARS:
        return False, [], f"{key}: the SQL grew too large"
    signature = ("frontier", left, right)
    if signature not in setup.proof_cache:
        columns = {k: v for k, v in before.items() if k in shared or k in setup.source_columns}
        facts = _schema(setup, columns)
        try:
            result = prove_equivalent_algebraic(
                left, right, schema=facts.columns or None, constraints=facts.constraints or None,
                types=facts.types or None, timeout_ms=setup.timeout_ms,
            )
            proven = result.status is SmtStatus.PROVEN_EQUIVALENT
            setup.proof_cache[signature] = (proven, list(dict.fromkeys([*facts.notes, *result.assumptions])) if proven else [],
                                            f"{key}: {result.reason}")
        except Exception as error:  # noqa: BLE001 - a prover failure is "not proved", never a crash
            setup.proof_cache[signature] = (False, [], f"{key}: {type(error).__name__}")
    return setup.proof_cache[signature]


def _closure(setup: _Setup, state: Mapping[str, str], key: str) -> tuple:
    seen: dict[str, str] = {}
    todo = [key]
    while todo:
        model = todo.pop()
        if model in seen or model not in state:
            continue
        seen[model] = state[model]
        todo.extend(setup.reads(state[model]))
    return tuple(sorted(seen.items()))


# ------------------------------------------------------------------ moves


def _simplified(setup: _Setup, sql: str, columns: Mapping[str, Sequence[str]]) -> list[str]:
    try:
        from .sql_simplify import simpler_forms
    except ImportError:  # pragma: no cover - the simplifier ships with this module
        return []
    reads = _all_reads(setup, sql)
    signature = (sql, tuple((r, tuple(columns.get(r, ()))) for r in sorted(reads)))
    if signature not in setup.forms_cache:
        try:
            setup.forms_cache[signature] = list(simpler_forms(sql, {r: columns[r] for r in reads if r in columns}))
        except Exception:  # noqa: BLE001 - a simplifier failure only means fewer candidates
            setup.forms_cache[signature] = []
    return setup.forms_cache[signature]


def _all_reads(setup: _Setup, sql: str) -> frozenset[str]:
    """Tables and sources a query reads (``_Reads`` keeps tables only)."""

    if sql not in setup.all_reads:
        found = set()
        try:
            for table in _table_nodes(sqlglot.parse_one(sql, read="bigquery")):
                key = setup.pipeline.resolve(table)
                if key:
                    found.add(key)
        except sqlglot.errors.SqlglotError:
            pass
        setup.all_reads[sql] = frozenset(found)
    return setup.all_reads[sql]


def _best(setup: _Setup, sql: str, columns) -> list[str]:
    """``sql`` and its simpler forms, cheapest first."""

    forms = [sql, *(_simplified(setup, sql, columns))]
    forms = list(dict.fromkeys(forms))
    return sorted(forms, key=lambda s: (setup.score(s), len(s)))


def _readers(setup: _Setup, state: Mapping[str, str]) -> dict[str, set[str]]:
    readers: dict[str, set[str]] = {key: set() for key in state}
    for key, sql in state.items():
        for read in setup.reads(sql):
            if read in readers and read != key:
                readers[read].add(key)
    return readers


def _used_columns(sql: str, table_key: str, resolve) -> set[str] | None:
    """Column names a reader may take from ``table_key`` (every column name it mentions), ``None`` for a star."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return None
    aliases = set()
    for table in _table_nodes(tree):
        if resolve(table) == table_key:
            aliases.add((table.alias or table.name).lower())
    for star in tree.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Column):
            if (parent.table or "").lower() in aliases:
                return None
        elif isinstance(parent, exp.Select):
            return None  # a bare star: be safe whatever the FROM is
    return {c.name.lower() for c in tree.find_all(exp.Column) if c.name}


def _prune(sql: str, keep: set[str]) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(tree, exp.Select) or tree.args.get("distinct"):
        return None
    items = tree.expressions
    if any(isinstance(i, exp.Star) or (isinstance(i, exp.Column) and isinstance(i.this, exp.Star)) for i in items):
        return None
    kept = [i for i in items if (i.alias_or_name or "").lower() in keep]
    if not kept or len(kept) == len(items):
        return None
    tree.set("expressions", kept)
    return tree.sql(dialect="bigquery")


def _moves(setup: _Setup, state: Mapping[str, str], columns: Mapping[str, Sequence[str]]):
    """``(label, new state)`` for every move allowed in ``state``, cheapest variants first."""

    resolve = setup.pipeline.resolve
    readers = _readers(setup, state)
    protected = set(setup.protected) | setup.pinned
    editable = [k for k in sorted(state) if k not in protected]

    for key in editable:
        users = readers[key]
        if not users:
            yield f"drop {key}", {k: v for k, v in state.items() if k != key}
            continue
        if key in setup.fixed or any(u in setup.fixed for u in users):
            continue
        try:
            raw = {u: _inline_into(state[u], key, state[key], resolve) for u in users}
        except sqlglot.errors.SqlglotError:
            continue
        rest = {k: v for k, v in state.items() if k != key}
        best = {u: _best(setup, sql, columns) for u, sql in raw.items()}
        simple = {**rest, **{u: forms[0] for u, forms in best.items()}}
        yield f"fold {key} into {', '.join(sorted(users))}", simple
        plain = {**rest, **raw}
        if plain != simple:
            yield f"fold {key} into {', '.join(sorted(users))} (unsimplified)", plain

    # merge an unprotected table into another that returns the same columns from the same tables
    groups: dict[tuple, list[str]] = {}
    for key in sorted(state):
        names = tuple(columns.get(key, ()))
        if names and key not in setup.fixed:
            groups.setdefault((names, setup.reads(state[key])), []).append(key)
    for members in groups.values():
        for key in members:
            if key in protected:
                continue
            others = sorted(members, key=lambda k: (k not in protected, state[k] != state[key], k))  # exact copies first
            for other in others:
                if other == key or _depends_on(setup, state, other, key):
                    continue
                users = readers[key]
                if any(u in setup.fixed for u in users):
                    break
                try:
                    changed = {u: _redirect(state[u], key, other, resolve) for u in users}
                except sqlglot.errors.SqlglotError:
                    break
                yield f"merge {key} into {other}", {**{k: v for k, v in state.items() if k != key}, **changed}
                break

    # columns no reader uses
    for key in editable:
        users = readers[key]
        if not users or key in setup.fixed:
            continue
        used: set[str] = set()
        for user in users:
            found = _used_columns(state[user], key, resolve)
            if found is None:
                used = set()
                break
            used |= found
        if not used:
            continue
        used |= setup.keep_columns.get(key, frozenset())
        pruned = _prune(state[key], used)
        if pruned is not None:
            forms = _best(setup, pruned, columns)
            yield f"prune unused columns of {key}", {**state, key: forms[0]}

    # simpler SQL for one table
    for key in sorted(state):
        if key in setup.fixed:
            continue
        for form in _simplified(setup, state[key], columns)[:2]:
            yield f"simplify {key}", {**state, key: form}

    if setup.factor:
        yield from _factor_moves(setup, state, columns)


# ------------------------------------------------------------------ shared tables


_GENERIC_ALIAS = re.compile(r"^(?:[a-z]{1,3}\d*|t\w{0,2}|sub\w*|sq\w*|dt\w*|tmp\w*|temp\w*|base\d*|inner\w*|q\d*|x\d*)$")


@dataclass
class _Occurrence:
    key: str  # the table it is in
    kind: str  # "derived", "cte" or "table"
    name: str  # derived-table alias or CTE name ("" for a whole table)
    sql: str  # the repeated query


def _fingerprint(body: exp.Expression) -> str:
    from kumo_sql_prover.canonical import canonical_copy

    try:
        text = canonical_copy(body).sql(dialect="bigquery", normalize=True, comments=False)
    except Exception:  # noqa: BLE001 - a shape the canonicalizer cannot follow keeps its own text
        text = body.sql(dialect="bigquery", normalize=True, comments=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _nested_columns(columns: Mapping[str, Sequence[str]], keys: Iterable[str]) -> dict | None:
    nested: dict = {}
    for key in keys:
        cols = columns.get(key)
        parts = key.split(".")
        if not cols or len(parts) != 3:
            return None
        nested.setdefault(parts[0], {}).setdefault(parts[1], {})[parts[2]] = {c: "STRING" for c in cols}
    return nested


def _standalone(setup: _Setup, body: exp.Expression, outer_ctes: set[str], columns) -> tuple[str, tuple[str, ...], str] | None:
    """``(sql, output names, fingerprint)`` when ``body`` can be its own table: it reads only tables (no WITH
    table of the query around it, no column of an outer query), has named outputs and gives the same rows
    wherever it runs. The fingerprint is taken with every column qualified, so ``p.amount`` over
    ``payments AS p`` and ``amount`` over ``payments`` match."""

    if not isinstance(body, exp.Query):
        return None
    sql = body.sql(dialect="bigquery")
    if _volatile(sql):
        return None
    own = {c.alias_or_name.lower() for c in body.find_all(exp.CTE)}
    if any(isinstance(w, exp.With) and w.args.get("recursive") for w in body.find_all(exp.With)):
        return None
    reads = set()
    for table in body.find_all(exp.Table):
        parts = _table_parts(table)
        if not parts:
            return None
        if len(parts) == 1 and parts[0] in own:
            continue
        if len(parts) == 1 and parts[0] in outer_ctes:
            return None  # reads a WITH table of the query around it
        found = setup.pipeline.resolve(table)
        if found is None:
            return None
        reads.add(found)
    names = tuple(n.lower() for n in (_select_names(sql) or ()))
    if not names or len(set(names)) != len(names):
        return None
    schema = _nested_columns(columns, reads)
    if schema is None and reads:
        return None
    try:
        from sqlglot.optimizer.qualify import qualify

        qualified = qualify(sqlglot.parse_one(sql, read="bigquery"), schema=schema or {}, dialect="bigquery",
                            validate_qualify_columns=True, quote_identifiers=False)
    except Exception:  # noqa: BLE001 - a column the body cannot resolve on its own belongs to an outer query
        return None
    return sql, names, _fingerprint(qualified) + "|" + ",".join(names)


def _occurrences(setup: _Setup, key: str, sql: str, columns) -> list[tuple[str, _Occurrence]]:
    """``(fingerprint, occurrence)`` for every derived table, WITH table and the whole query of one table."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return []
    found = []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    whole = _standalone(setup, tree, set(), columns)
    if whole is not None:
        found.append((whole[2], _Occurrence(key, "table", "", whole[0])))
    for node in tree.find_all(exp.Subquery):
        if not isinstance(node.parent, (exp.From, exp.Join)) or not node.alias:
            continue
        alias = node.args.get("alias")
        if alias is not None and alias.args.get("columns"):
            continue
        done = _standalone(setup, node.this, ctes, columns)
        if done is not None:
            found.append((done[2], _Occurrence(key, "derived", node.alias.lower(), done[0])))
    clause = with_clause(tree)
    if clause is not None and not clause.args.get("recursive"):
        for cte in clause.expressions:
            done = _standalone(setup, cte.this, ctes, columns)
            if done is not None:
                found.append((done[2], _Occurrence(key, "cte", cte.alias_or_name.lower(), done[0])))
    return found


def _replace_occurrences(sql: str, fingerprint: str, target: str, setup: _Setup, columns) -> str | None:
    """``sql`` with every derived table and WITH table of that fingerprint read from table ``target``."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    changed = False
    for node in list(tree.find_all(exp.Subquery)):
        if not isinstance(node.parent, (exp.From, exp.Join)) or not node.alias:
            continue
        done = _standalone(setup, node.this, ctes, columns)
        if done is None or done[2] != fingerprint:
            continue
        node.replace(_table_for(target, node.alias))
        changed = True
    clause = with_clause(tree)
    if clause is not None and not clause.args.get("recursive"):
        for cte in list(clause.expressions):
            done = _standalone(setup, cte.this, ctes, columns)
            if done is None or done[2] != fingerprint:
                continue
            name = cte.alias_or_name.lower()
            if sum(1 for c in tree.find_all(exp.CTE) if c.alias_or_name.lower() == name) != 1:
                return None  # another WITH table of that name: references cannot be told apart
            cte.pop()
            for table in list(tree.find_all(exp.Table)):
                if not table.db and table.name.lower() == name:
                    table.replace(_table_for(target, table.alias or table.name))
            changed = True
        if not clause.expressions:
            set_with_clause(tree, None)
    return tree.sql(dialect="bigquery") if changed else None


def _fresh_name(setup: _Setup, hints: Sequence[str], reader: str, state: Mapping[str, str]) -> str:
    """A table name for a factored query: the first descriptive WITH-table name or alias of its occurrences,
    else ``<reader>_shared``; never the name of a table, a source or any other word of the SQL (a WITH name
    of an occurrence aside, since the move removes that WITH table)."""

    tables = {parts[-1] for parts in setup.back.values()} | {k.split(".")[-1] for k in setup.pipeline.models}
    words = set()
    for sql in state.values():
        words |= {name.lower() for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)}
    base = ""
    for hint in hints:
        hint = re.sub(r"[^a-z0-9_]", "_", hint.lower()).strip("_")
        if hint and not _GENERIC_ALIAS.match(hint) and hint not in tables:
            base = hint
            break
    if not base:
        base = f"{reader}_shared"
    taken = tables | (words - {base})
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_shared" if n == 2 else f"{base}_{n - 1}"
    return name


def _new_table(setup: _Setup, fingerprint: str, occurrences: Sequence[_Occurrence], state: Mapping[str, str]) -> str:
    """The internal key of the table a repeated query is factored into (one per fingerprint)."""

    if fingerprint in setup.new_tables:
        return setup.new_tables[fingerprint]
    occurrence = occurrences[0]
    reader_parts = setup.back.get(tuple(occurrence.key.split(".")), tuple(occurrence.key.split(".")))
    hints = [o.name for o in occurrences if o.kind == "cte"] + [o.name for o in occurrences if o.kind == "derived"]
    name = _fresh_name(setup, hints, reader_parts[-1], state)
    parts = (*reader_parts[:-1], name)
    internal = internal_name(parts, setup.catalogs)
    key = ".".join(internal)
    setup.back[internal] = parts
    models = dict(setup.pipeline.models)
    models[key] = Model(Target(*internal), "table", occurrence.sql)
    setup.pipeline = Pipeline(models=models, sources=setup.pipeline.sources, source_schema=setup.pipeline.source_schema)
    setup.reads.pipeline = setup.pipeline  # cached reads of earlier SQL stay right: none of it names the new table
    setup.new_tables[fingerprint] = key
    setup.new_names[key] = parts
    return key


def _factor_moves(setup: _Setup, state: Mapping[str, str], columns):
    """Repeated queries read from one table: an existing table with that query, or a new shared one."""

    groups: dict[str, list[_Occurrence]] = {}
    for key in sorted(state):
        if key in setup.fixed:
            continue
        for fingerprint, occurrence in _occurrences(setup, key, state[key], columns):
            groups.setdefault(fingerprint, []).append(occurrence)
    protected = set(setup.protected) | setup.pinned
    for fingerprint, members in sorted(groups.items()):
        inner = [m for m in members if m.kind != "table"]
        tables = sorted((m.key for m in members if m.kind == "table"), key=lambda k: (k not in protected, k))
        if not inner or (not tables and len(inner) < 2):
            continue
        if tables:
            target = tables[0]
            label = f"reuse {target} in"
        else:
            target = _new_table(setup, fingerprint, inner, state)
            label = f"factor {target} out of"
        readers = sorted({m.key for m in inner if m.key != target})
        if not readers or any(_depends_on(setup, state, target, reader) for reader in readers):
            continue
        changed = {}
        try:
            for reader in readers:
                new = _replace_occurrences(state[reader], fingerprint, target, setup, columns)
                if new is None:
                    break
                changed[reader] = new
        except sqlglot.errors.SqlglotError:
            continue
        if len(changed) != len(readers):
            continue
        base = dict(state)
        if not tables:
            base[target] = inner[0].sql
        raw = {**base, **changed}
        yield f"{label} {', '.join(readers)}", raw
        after = _state_columns(setup, raw)
        simple = {**raw, **{reader: _best(setup, sql, after)[0] for reader, sql in changed.items()}}
        if simple != raw:
            yield f"{label} {', '.join(readers)} (simplified)", simple


def _depends_on(setup: _Setup, state: Mapping[str, str], model: str, target: str) -> bool:
    seen, todo = set(), [model]
    while todo:
        key = todo.pop()
        if key == target:
            return True
        if key in seen or key not in state:
            continue
        seen.add(key)
        todo.extend(setup.reads(state[key]))
    return False


def _fold_all(setup: _Setup, state: Mapping[str, str]) -> tuple[str, dict[str, str]] | None:
    """Every unprotected table that is read folded into its readers, sources first; unread ones dropped."""

    protected = set(setup.protected) | setup.pinned
    current = dict(state)
    order = [k for k in setup.pipeline.topological_order() if k in current]
    for key in order:
        if key in protected or key in setup.fixed:
            continue
        users = _readers(setup, current)[key]
        if any(u in setup.fixed for u in users):
            continue
        try:
            for user in users:
                current[user] = _inline_into(current[user], key, current[key], setup.pipeline.resolve)
        except sqlglot.errors.SqlglotError:
            return None
        del current[key]
    columns = _state_columns(setup, current)
    for key in list(current):
        if key not in setup.fixed:
            current[key] = _best(setup, current[key], columns)[0]
    return ("fold every unprotected table into its readers", current) if current != dict(state) else None


# ------------------------------------------------------------------ search


def minimize_tables(
    tables: Mapping[str, str],
    protected: Iterable[str],
    *,
    sources: Mapping[str, object] | None = None,
    dialect: str = "bigquery",
    timeout_ms: int = 5000,
    max_seconds: float = 120.0,
    max_steps: int = 200,
    progress: Callable[[str], None] | None = None,
    fixed: Iterable[str] | Mapping[str, object] = (),
    incremental: Iterable[str] = (),
    checked: Iterable[str] = (),
    keep_columns: Mapping[str, Iterable[str]] | None = None,
    factor: bool = False,
    lower_score_only: bool = False,
) -> TableMinimization:
    """The lowest-complexity set of tables found that keeps every protected table, with its proofs.

    ``tables`` maps a table name to one SELECT (in ``dialect``); names a query reads that are not tables
    are sources. ``sources`` optionally describes them: ``{name: {"columns": {col: type}, "key": [...],
    "not_null": [...]}}`` (or ``{col: type}``, or a list of columns); keys and NOT NULL columns are facts
    the prover may assume. Raises :class:`MinimizationError` on input it cannot use.

    ``fixed`` tables are kept exactly as given, like a table that is not a query, and every table they
    read is kept and proved unchanged (a mapping gives their columns). ``incremental`` names tables whose
    rows depend on earlier runs (a SELECT alone cannot say so, nor give a ``uniqueKey``, an
    ``updatePartitionFilter`` or a schedule): they are fixed, so they are never folded, merged, pruned or
    rewritten. A ``checked`` table may be dropped
    or folded away, but while it exists it must stay proved equal to the original on the columns it
    keeps. ``keep_columns`` are columns of a table that are never pruned. With ``factor``, a query
    repeated as a derived table or WITH table in several tables may be moved into a table of its own
    (an existing one with that query, or a new one, listed in ``added``). With ``lower_score_only`` a
    step must lower the complexity score; shorter text at the same score does not count.
    """

    started = time.time()
    protected = list(protected)
    fixed = _with_incremental(fixed, incremental)
    setup, names, mapping, back = _prepare(tables, protected, sources, dialect, timeout_ms, fixed, checked, keep_columns)
    setup.factor = factor
    start = dict(setup.original)
    result_state = start
    tried = rejected = steps = 0
    rejected_moves: list[dict] = []
    moves_taken: list[str] = []
    stop = ""

    def out_of_budget() -> str:
        if time.time() - started > max_seconds:
            return "time limit"
        if steps >= max_steps:
            return "step limit"
        return ""

    def attempt(label: str, candidate: dict[str, str]) -> bool:
        nonlocal tried, rejected
        tried += 1
        ok, _, why = _check(setup, candidate)
        if not ok:
            rejected += 1
            if len(rejected_moves) < 20 and not any(m["move"] == label for m in rejected_moves):
                rejected_moves.append({"move": label, "why": why[:200]})
            return False
        return True

    def descend(state: dict[str, str], path: list[str]) -> tuple[dict[str, str], list[str]]:
        nonlocal steps, stop
        while True:
            stop = stop or out_of_budget()
            if stop:
                return state, path
            current = setup.cost(state)
            columns = _state_columns(setup, state)
            options = []
            seen = set()
            for label, candidate in _moves(setup, state, columns):
                signature = tuple(sorted(candidate.items()))
                if signature in seen:
                    continue
                seen.add(signature)
                cost = setup.cost(candidate)
                if cost[0] < current[0] if lower_score_only else cost < current:
                    options.append((cost, label, candidate))
            options.sort(key=lambda item: (item[0], item[1]))
            for cost, label, candidate in options:
                stop = stop or out_of_budget()
                if stop:
                    return state, path
                if attempt(label, candidate):
                    steps += 1
                    if progress:
                        progress(f"{_label(label, back, names)}: proved (score {cost[0]})")
                    state, path = candidate, [*path, label]
                    break
            else:
                return state, path

    best_state, best_path = descend(start, [])
    if not stop:
        folded = _fold_all(setup, start)
        if folded is not None and attempt(folded[0], folded[1]):
            other_state, other_path = descend(folded[1], [folded[0]])
            better = setup.cost(other_state)[0] < setup.cost(best_state)[0] if lower_score_only else \
                setup.cost(other_state) < setup.cost(best_state)
            if better:
                best_state, best_path = other_state, other_path
    result_state, moves_taken = best_state, best_path

    # the proofs for the answer, one per protected table (cached from the search)
    proofs = _answer_proofs(setup, result_state, start, back, names)
    if proofs is None:  # cannot happen for a state the search accepted; never return an unproved answer
        result_state, moves_taken = start, []
        proofs = _answer_proofs(setup, start, start, back, names) or {}
    out = _render(setup, result_state, start, tables, names, back, dialect)
    if result_state is not start and not _holds_together(setup, result_state, out, names, back, dialect):
        # the rendered answer must stand on the input's own names: every table it reads is one the input named
        # and one it still has; if not, the input is the answer
        result_state, moves_taken = start, []
        proofs = _answer_proofs(setup, start, start, back, names) or {}
        out = _render(setup, start, start, tables, names, back, dialect)
    original_score = pipeline_score(_render(setup, start, start, tables, names, back, dialect))
    removed = sorted(names[back[tuple(k.split("."))]] for k in setup.original if k not in result_state)
    changed = sorted(names[back[tuple(k.split("."))]] for k in result_state
                     if k in setup.original and result_state[k] != setup.original[k])
    added = sorted(_user_name(k, back, names) for k in result_state if k not in setup.original)
    return TableMinimization(
        tables=out, original=dict(tables), protected=[names[_parts(p, setup.fold)] for p in dict.fromkeys(protected)],
        proofs=proofs, score=pipeline_score(out), original_score=original_score, moves=[_label(m, back, names) for m in moves_taken],
        removed=removed, changed=changed, added=added, tried=tried, rejected=rejected,
        rejected_moves=[{**m, "move": _label(m["move"], back, names), "why": _label(m["why"], back, names)} for m in rejected_moves],
        stopped=stop, seconds=time.time() - started, unscored=setup.unscored[0],
    )


def _answer_proofs(setup: _Setup, state, start, back, names) -> dict[str, ProtectedProof] | None:
    """One proof per protected table of ``state``, or ``None`` when one of them does not hold."""

    proofs: dict[str, ProtectedProof] = {}
    for key in setup.protected_opaque:
        user = _user_name(key, back, names)
        proofs[user] = ProtectedProof(user, "unchanged", "not a single query: returned exactly as given")
    for key in setup.protected:
        user = _user_name(key, back, names)
        if state[key] == setup.original[key] and _closure(setup, state, key) == _closure(setup, start, key):
            proofs[user] = ProtectedProof(user, "unchanged", "its SQL and every table it reads are as given")
            continue
        ok, assumptions, why = _check(setup, state, [key])
        if not ok:
            return None
        proofs[user] = ProtectedProof(user, "proved", "proved equal to the original by the pipeline prover",
                                      [*assumptions, *_case_assumptions(setup)])
    return proofs


def _case_assumptions(setup: _Setup) -> list[str]:
    if not setup.ambiguous:
        return []
    shown = ", ".join(sorted(".".join(setup.back[tuple(k.split("."))]) for k in setup.ambiguous))
    return [f"table names are case-sensitive; names that differ only by case ({shown}) are kept as written"]


def _dangling(setup: _Setup, state: Mapping[str, str], keys: Iterable[str]) -> str:
    """A table that ``keys`` (and what they read) read but ``state`` no longer has, or ``""``."""

    todo, seen = list(keys), set()
    while todo:
        key = todo.pop()
        if key in seen or key not in state:
            continue
        seen.add(key)
        for read in setup.reads(state[key]):
            if read not in state and read not in setup.opaque:
                return read
            todo.append(read)
    return ""


def _holds_together(setup: _Setup, state: Mapping[str, str], out: Mapping[str, str], names, back, dialect: str) -> bool:
    """Whether the rendered tables read only tables the input named, none of which the answer removed."""

    known = set(back.values())
    gone = {back[tuple(k.split("."))] for k in setup.original if k not in state}
    kept_as_given = {names[back[tuple(k.split("."))]] for k in setup.opaque}
    for user, sql in out.items():
        tree = None if user in kept_as_given else single_query(sql, dialect)
        if tree is None:
            continue  # a table that is not one query, or is kept as written, is returned as given
        from .refactor import _table_nodes

        for table in _table_nodes(tree):
            parts = _table_parts(table, setup.fold)
            if parts and (parts not in known or parts in gone):
                return False
    return True


def _user_name(key: str, back, names) -> str:
    parts = back[tuple(key.split("."))]
    return names.get(parts, ".".join(parts))


def _label(text: str, back, names) -> str:
    for internal, parts in sorted(back.items(), key=lambda kv: -len(".".join(kv[0]))):
        text = text.replace(".".join(internal), names.get(parts, ".".join(parts)))
    return text


def _render(setup: _Setup, state, start, tables, names, back, dialect) -> dict[str, str]:
    out = {}
    for key, sql in state.items():
        parts = back[tuple(key.split("."))]
        user = names.get(parts, ".".join(parts))
        if key in setup.original and sql == setup.original[key]:
            out[user] = tables[user]
            continue
        tree = _rename_tables(sqlglot.parse_one(sql, read="bigquery"), back)
        text = tree.sql(dialect="bigquery")
        try:
            from .sql_simplify import tidy

            text = tidy(text)
        except Exception:  # noqa: BLE001 - the untidied text is still right
            pass
        if dialect != "bigquery":
            text = sqlglot.transpile(text, read="bigquery", write=dialect)[0]
        out[user] = text
    for key in setup.opaque:  # tables that could not be read stay exactly as given
        user = names[back[tuple(key.split("."))]]
        out.setdefault(user, tables[user])
    ordered = {name: out[name] for name in tables if name in out}
    ordered.update({name: out[name] for name in sorted(out) if name not in ordered})  # factored tables last
    return ordered


def verify_tables(
    original: Mapping[str, str],
    candidate: Mapping[str, str],
    protected: Iterable[str],
    *,
    sources: Mapping[str, object] | None = None,
    dialect: str = "bigquery",
    timeout_ms: int = 5000,
    fixed: Iterable[str] | Mapping[str, object] = (),
    incremental: Iterable[str] = (),
    checked: Iterable[str] = (),
) -> dict[str, ProtectedProof]:
    """Check a proposed set of tables against the original: one result per protected table.

    Status ``unchanged`` (its SQL and everything it reads are as given), ``proved`` (the prover proved it
    returns the same rows, with the same output columns), ``missing`` or ``unknown`` (not proved; the reason
    says why). ``candidate`` may add tables of its own. Raises :class:`MinimizationError` on unusable input.

    ``fixed`` and ``incremental`` tables (as in :func:`minimize_tables`) must be in the candidate exactly as given, and every
    table one of them (or an unreadable table) reads gets a result too, since it is read as given. Each
    ``checked`` table the candidate still has gets a result: proved equal on the columns it keeps.
    """

    protected = list(protected)
    fixed = _with_incremental(fixed, incremental)
    fold = dialect != "bigquery"
    added = {name: sql for name, sql in candidate.items()
             if _parts(str(name), fold) not in {_parts(str(n), fold) for n in original}}
    checked = [c for c in checked if c in candidate and c not in protected]
    setup, names, mapping, back = _prepare({**original, **added}, protected, sources, dialect, timeout_ms, fixed, checked,
                                           extra_texts=candidate.values())
    by_parts = {_parts(str(name), fold): sql for name, sql in candidate.items()}
    for key in setup.opaque:  # read as given on both sides: it must not have changed
        parts = back[tuple(key.split("."))]
        user = names[parts]
        if user in original and by_parts.get(parts) != original[user]:
            why = f"{user} is kept as written but the candidate changed or removed it"
            return {_user_name(k, back, names): ProtectedProof(_user_name(k, back, names), "unknown", why)
                    for k in [*setup.protected_opaque, *setup.protected, *sorted(setup.checked)]}
    state: dict[str, str] = {}
    unreadable: set[str] = set()  # candidate tables that are not one query (a script, another statement, bad SQL)
    for parts, user in names.items():
        if parts not in by_parts:
            continue
        key = ".".join(mapping[parts])
        if key in setup.opaque:
            continue
        sql = by_parts[parts]
        if user in original and sql == original[user] and key in setup.original:
            state[key] = setup.original[key]
            continue
        tree = single_query(sql, dialect)
        if tree is not None and dialect != "bigquery":
            tree = single_query(tree.sql(dialect="bigquery"), "bigquery")
        if tree is None:
            state[key] = sql  # unreadable: nothing that depends on it can be proved
            unreadable.add(key)
            continue
        for table in tree.find_all(exp.Table):
            ref = _table_parts(table, fold)
            if ref and ref not in mapping and not is_cte_reference(table):
                mapping[ref] = internal_name(ref, setup.catalogs)
        state[key] = _rename_tables(tree, mapping, fold).sql(dialect="bigquery")
    results: dict[str, ProtectedProof] = {}
    for key in setup.protected_opaque:  # not a query: only the text as given can stand for it (checked above)
        user = _user_name(key, back, names)
        results[user] = ProtectedProof(user, "unchanged", "not a single query: the candidate has it exactly as given")
    keys = [*setup.protected, *sorted(setup.pinned - set(setup.protected)), *sorted(k for k in setup.checked if k in state)]
    for key in keys:
        user = _user_name(key, back, names)
        if key not in state:
            results[user] = ProtectedProof(user, "missing", "the table is not in the candidate")
            continue
        blocked = _reaches(setup, state, key, unreadable)
        if blocked:
            results[user] = ProtectedProof(user, "unknown", f"{_label(blocked, back, names)} is not a single query")
            continue
        if key in setup.touching and any(state.get(m) != setup.original[m] for m in setup.ambiguous if m in setup.original):
            why = "table names that differ only by case may be one table, and one of them was changed"
            results[user] = ProtectedProof(user, "unknown", why)
            continue
        if state[key] == setup.original[key] and _closure(setup, state, key) == _closure(setup, setup.original, key):
            results[user] = ProtectedProof(user, "unchanged", "its SQL and every table it reads are as given")
            continue
        ok, assumptions, why = _check(setup, state, [key])
        results[user] = (ProtectedProof(user, "proved", "proved equal to the original", [*assumptions, *_case_assumptions(setup)])
                         if ok else ProtectedProof(user, "unknown", _label(why, back, names)))
    return results


def _reaches(setup: _Setup, state: Mapping[str, str], key: str, targets: set[str]) -> str:
    """The first of ``targets`` that ``key`` is, or reads through other tables of ``state``, else ``""``."""

    todo, seen = [key], set()
    while todo:
        node = todo.pop()
        if node in seen or node not in state:
            continue
        seen.add(node)
        if node in targets:
            return node
        todo.extend(sorted(setup.reads(state[node])))
    return ""


def minimize_case(case: Mapping) -> dict[str, str]:
    """Eval harness entry point: ``{id, dialect, sources, tables, protected}`` to the new ``{name: SQL}``."""

    return minimize_tables(
        case["tables"], case["protected"], sources=case.get("sources"), dialect=case.get("dialect") or "bigquery",
        max_seconds=float(case.get("max_seconds") or 60.0), incremental=case.get("incremental") or (),
    ).tables


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql minimize-tables CASE.json [--max-seconds N]``"""

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m kumosql minimize-tables",
        description=__doc__.split("\n")[0] + " READ-ONLY: it prints the result and writes no file.")
    parser.add_argument("case", help="JSON file with tables, protected and (optionally) sources, dialect and incremental (tables kept as written); - reads stdin")
    parser.add_argument("--max-seconds", type=float, default=120.0)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.read() if args.case == "-" else open(args.case, encoding="utf-8").read()
        case = json.loads(text)
        result = minimize_tables(
            case["tables"], case["protected"], sources=case.get("sources"), dialect=case.get("dialect") or "bigquery",
            timeout_ms=args.timeout_ms, max_seconds=args.max_seconds, incremental=case.get("incremental") or (),
            progress=lambda line: print(line, file=sys.stderr),
        )
    except (OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(result.to_json(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
