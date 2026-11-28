"""Facts about the rows and columns a query returns, each labelled with where it comes from.

``describe_table`` lists them for a model (``facts``) and ``check_query`` for a draft. Four sources, never mixed up:

* ``schema snapshot``: the deployed table's ``is_nullable`` (``--schema-snapshot``). ``NO`` means never NULL. ``YES`` is the
  column's mode; it does not say that NULLs occur.
* ``declared``: the model's Dataform ``assertions`` (``nonNull``, ``uniqueKey``) or a grain stated elsewhere (docs, Malloy,
  ``--assume-grain``, a declaration's ``uniqueKey``). These are claims, checked only when the assertions run, or never.
* ``derived grain``: the grain :mod:`kumosql.table_profile` derives from the query (``describe_table``'s ``grain``).
* ``inferred from the query``: what :func:`kumo_sql_prover.output_properties.infer_properties` proves from the query's
  structure. It finds columns that are never NULL (``COALESCE`` with a constant, ``WHERE x IS NOT NULL`` or a comparison,
  an inner-join key, ``COUNT``), sets of columns no two rows share (``GROUP BY``, ``DISTINCT``,
  ``QUALIFY ROW_NUMBER() ... = 1``), and whether the query returns exactly one row or at most one. It is a proof about the
  query, so it holds for the deployed table only if this query built that table.

A fact that rests on facts of the tables the query reads (a NOT NULL column of the snapshot, a declared key, a derived
grain) names the first of them (``given ...``). When two sources disagree (the snapshot says NULLABLE, the query never
writes NULL), both are shown. Neither replaces the other: the checks keep the snapshot's answer, because it describes the
table that a query actually runs against.

Each model's facts are computed the first time they are asked for and kept in the session. One model costs one parse and
one pass of the prover. It reads only what is already known about the tables above it (the snapshot, declarations, derived
grains and column types), never the inferred facts of other models. A draft also uses the inferred facts of the models it
reads.

Only tables and views get facts from their query. An incremental table keeps the rows of earlier runs, and a model whose
type is computed may be incremental.

This module imports no tool module, so importing it never changes the order of the server's tool list.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

import sqlglot
from sqlglot import exp

from .mcp_answers import Answer, clip

if TYPE_CHECKING:
    from .mcp_session import Session

INFERRED = "inferred from the query"
SNAPSHOT = "schema snapshot"
DECLARED = "declared"
GRAIN = "derived grain"
#: the order sources are named in, most certain first
_ORDER = (INFERRED, SNAPSHOT, DECLARED, GRAIN)

#: model kinds whose stored table is exactly what their query returns
_QUERY_IS_TABLE = frozenset({"table", "view"})
_NOT_THE_TABLE = {
    "incremental": "an incremental table keeps the rows of earlier runs, so what its query guarantees is not what the table holds",
    "unknown": "its type is computed (it may be an incremental table), so what its query guarantees may not be what the table holds",
}
#: lineage reasons that make a model's output column list unreliable (the same rule ``check_query``'s type catalog uses)
_UNRELIABLE_COLUMNS = frozenset({"unexpanded_star", "unparsed_model", "unparsed", "cycle", "qualify_error", "parse_error", "unknown_column"})
NAMES_SHOWN = 8
LINE = 220


@dataclass(frozen=True)
class QueryFacts:
    """What the prover shows about one query's output. Names are output names, spelled as the query spells them."""

    never_null: tuple[str, ...] = ()
    #: sets of columns no two rows share; an empty tuple means the query returns at most one row
    keys: tuple[tuple[str, ...], ...] = ()
    exactly_one_row: bool = False
    #: ``null:<name>`` or ``key:<a,b>`` -> the facts of the tables read that it rests on, as ``(text, source)`` pairs
    given: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    #: why nothing was inferred ("" when the prover read the query)
    unsupported: str = ""
    #: True when the facts are not the table's (an incremental model): ``unsupported`` then says why
    not_the_table: bool = False
    #: per output position, whether the column is never NULL resting on no declared claim (see ``proven``)
    proven_by_position: tuple[bool, ...] = ()

    @property
    def at_most_one_row(self) -> bool:
        return self.exactly_one_row or () in self.keys

    def proven_never_null(self, name: str) -> bool:
        """Never NULL by the query, resting on no declared claim (only on the query and the schema snapshot)."""

        found = {n.casefold(): n for n in self.never_null}.get(name.casefold())
        return found is not None and proven(self.given.get(f"null:{found}", ()))

    def proven_keys(self) -> list[tuple[str, ...]]:
        """The unique column sets that rest on no declared claim."""

        return [unique for unique in self.keys if proven(self.given.get(f"key:{','.join(unique)}", ()))]


def proven(givens) -> bool:
    """Whether a fact rests only on the query, the schema snapshot and other proofs: no declared claim, nothing unlabelled."""

    return all(source not in (DECLARED, "assumed") for _text, source in givens)


# ------------------------------------------------------------------- inference


def tables_read(session: "Session", tree: exp.Expression) -> dict[str, str | None]:
    """Each table the query reads as it spells it (lower case), mapped to the project key, or ``None`` outside the project."""

    pipeline = session.pipeline()
    known = session.tables()
    ctes = {cte.alias.casefold() for cte in tree.find_all(exp.CTE)}
    found: dict[str, str | None] = {}
    for table in tree.find_all(exp.Table):
        if not table.name or (not table.args.get("db") and table.name.casefold() in ctes):
            continue
        spelled = ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name)
        try:
            key = pipeline.resolve(table)
        except Exception:  # noqa: BLE001
            key = None
        found.setdefault(spelled.casefold(), key if key in known else None)
    return found


def columns_of(session: "Session", key: str) -> list[str]:
    """The columns a query that reads ``key`` sees: the snapshot's list, else the model's outputs (when its lineage can be trusted), else the declared ones."""

    pipeline = session.pipeline()
    listed = session.column_types().columns(key)
    if listed:
        return listed
    if key in pipeline.models:
        profile = session.profiles().get(key)
        outputs = list(pipeline.output_columns(key))
        broken = profile is None or any((a.reason or "").split(":")[0] in _UNRELIABLE_COLUMNS for a in profile.attributes)
        return [] if broken else outputs
    return list(pipeline.source_schema.get(key) or {})


def _display(spelled: str) -> str:
    """How the prover names a table in its assumptions: its dataset and name."""

    return ".".join(spelled.split(".")[-2:])


def _inputs(session: "Session", tables: Mapping[str, str | None], with_inferred: bool):
    """``(schema, constraints, labels, missing)`` for the prover: the columns and the known facts of each table read.

    ``labels`` maps the prover's wording of a fact (``ds.t.id is NOT NULL``, ``(id) is unique in ds.t``) to its source.
    ``with_inferred`` (drafts) also passes on the inferred facts of the models read, except a never-NULL fact for a column
    the schema snapshot says is NULLABLE: there the snapshot wins.
    """

    from kumo_sql_prover.smt_equivalence import TableConstraints

    pipeline = session.pipeline()
    types = session.column_types()
    schema: dict[str, list[str]] = {}
    constraints: dict[str, TableConstraints] = {}
    labels: dict[str, str] = {}
    missing: list[str] = []
    bare: dict[str, set[str]] = {}
    for spelled, key in tables.items():
        columns = columns_of(session, key) if key is not None else []
        if not columns:
            missing.append(spelled)
            continue
        folded = [c.casefold() for c in columns]
        schema[spelled] = folded
        bare.setdefault(spelled.split(".")[-1], set()).add(spelled)
        display = _display(spelled)
        model = pipeline.models.get(key)
        own = model_facts(session, key) if with_inferred and model is not None else None
        not_null: dict[str, str] = {}
        for column in folded:
            flag = types.nullable(key, column)
            if flag is False:
                not_null[column] = SNAPSHOT if types.origin.get(key) == "snapshot" else f"inferred from the query of {key.split('.')[-1]}"
            elif own is not None and flag is None and own.proven_never_null(column):
                not_null[column] = f"inferred from the query of {key.split('.')[-1]}"
        for column in (model.non_null if model is not None else ()):
            not_null.setdefault(column.casefold(), DECLARED)
        keys: dict[tuple[str, ...], str] = {}
        profile = session.profiles().get(key)
        if profile is not None and profile.grain.known and profile.grain.keys:
            keys[tuple(k.casefold() for k in profile.grain.keys)] = GRAIN if profile.grain.status == "derived" else DECLARED
        for unique in (model.unique_keys if model is not None else ()):
            keys.setdefault(tuple(k.casefold() for k in unique), DECLARED)
        claim = session.grain_claim(key)
        if claim is not None:
            keys.setdefault(tuple(k.casefold() for k in claim.keys), DECLARED)
        if own is not None and not own.unsupported:
            for unique in own.proven_keys():
                if unique:
                    keys.setdefault(tuple(k.casefold() for k in unique), f"inferred from the query of {key.split('.')[-1]}")
        keys = {k: v for k, v in keys.items() if k and all(c in folded for c in k)}
        not_null = {c: v for c, v in not_null.items() if c in folded}
        if not_null or keys:
            constraints[spelled] = TableConstraints(not_null=frozenset(not_null), keys=tuple(keys))
        for column, source in not_null.items():
            labels[f"{display}.{column} is NOT NULL"] = source
        for unique, source in keys.items():
            labels[f"({', '.join(unique)}) is unique in {display}"] = source
    for name, spellings in bare.items():  # the ROW_NUMBER reading looks a SELECT * source up by its bare name
        if len(spellings) == 1 and name not in schema:
            schema[name] = schema[next(iter(spellings))]
    return schema, constraints, labels, missing


def infer(session: "Session", sql: str, tables: Mapping[str, str | None], spelling: Mapping[str, str] | None = None, *, with_inferred: bool = False) -> QueryFacts:
    """The prover's facts about ``sql``, which reads ``tables`` (``tables_read``); ``spelling`` restores the case of output names."""

    from kumo_sql_prover.output_properties import infer_properties

    from .incremental_merge import dedup_abstraction

    schema, constraints, labels, missing = _inputs(session, tables, with_inferred)
    if missing:
        return QueryFacts(unsupported=f"it reads {missing[0]}, whose columns are not known (a schema snapshot or --source-schema gives them)")
    try:
        props = infer_properties(sql, constraints, schema)
    except Exception as exc:  # noqa: BLE001 - a failing analysis is "not inferred", never a wrong fact
        return QueryFacts(unsupported=f"the prover stopped ({type(exc).__name__})")
    if props.unsupported:
        return QueryFacts(unsupported=_plain(props.unsupported))
    names = [c.name for c in props.columns]
    counts = Counter(names)
    usable = {n for n in names if counts[n] == 1 and "\x00" not in n}
    spelled = {k.casefold(): v for k, v in (spelling or {}).items()}
    position = {name: i for i, name in enumerate(names)}

    def say(name: str) -> str:
        return spelled.get(name, name)

    def in_order(columns) -> tuple[str, ...]:
        """A key's columns in output order, as the query spells them."""

        return tuple(say(c) for c in sorted(columns, key=lambda c: position.get(c, len(position))))

    def given(assumptions) -> tuple[tuple[str, str], ...]:
        return tuple((_given_text(a), labels.get(a, "assumed")) for a in assumptions)

    found: dict[str, tuple] = {}
    never_null = []
    for column in props.columns:
        if column.non_null and column.name in usable:
            never_null.append(say(column.name))
            found[f"null:{say(column.name)}"] = given(column.assumptions)
    by_position = tuple(column.non_null and proven(given(column.assumptions)) for column in props.columns)
    keys: dict[tuple[str, ...], tuple] = {(): ()} if props.exactly_one_row else {}
    for unique in props.keys:
        if all(c in usable for c in unique.columns):
            keys.setdefault(in_order(unique.columns), given(unique.assumptions))
    try:
        grouped = dedup_abstraction(sql, schema)  # QUALIFY ROW_NUMBER() OVER (PARTITION BY p ...) = 1 read as GROUP BY p
        other = infer_properties(grouped, constraints, schema) if grouped else None
    except Exception:  # noqa: BLE001
        other = None
    if other is not None and not other.unsupported and [c.name for c in other.columns] == names:
        for unique in other.keys:
            if all(c in usable for c in unique.columns):
                keys.setdefault(in_order(unique.columns), given(unique.assumptions))
    kept = _smallest(list(keys))
    for unique in kept:
        found[f"key:{','.join(unique)}"] = keys[unique]
    return QueryFacts(tuple(never_null), tuple(kept), props.exactly_one_row, found, proven_by_position=by_position)


def _smallest(keys: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """The keys no smaller key is part of, shortest first (a superset of a unique set says nothing more)."""

    keys = sorted(set(keys), key=lambda k: (len(k), [c.casefold() for c in k]))
    kept: list[tuple[str, ...]] = []
    for unique in keys:
        if not any({c.casefold() for c in other} <= {c.casefold() for c in unique} for other in kept):
            kept.append(unique)
    return kept[:4]


_NOT_NULL = re.compile(r"^(?P<column>.+) is NOT NULL$")
_UNIQUE = re.compile(r"^\((?P<columns>[^)]*)\) is unique in (?P<table>.+)$")


def _given_text(assumption: str) -> str:
    if match := _NOT_NULL.match(assumption):
        return f"{match.group('column')} NOT NULL"
    if match := _UNIQUE.match(assumption):
        return f"{match.group('table')} unique on ({match.group('columns')})"
    return assumption


def _plain(reason: str) -> str:
    """The prover's reason for not reading a query, in words."""

    if (match := re.match(r"columns of (.+) are not known", reason)) or (match := re.match(r"table (.+) is not in the schema", reason)):
        return f"it reads {match.group(1)}, whose columns are not known"
    return f"the prover does not read this query shape ({clip(reason, 90)})"


def model_facts(session: "Session", key: str) -> QueryFacts | None:
    """The inferred facts of model ``key``'s query, computed once per session; ``None`` for a table the project does not build."""

    return session._once(f"inferred_facts:{key}", lambda: _model_facts(session, key))


def _model_facts(session: "Session", key: str) -> QueryFacts | None:
    pipeline = session.pipeline()
    model = pipeline.models.get(key)
    if model is None or model.kind in ("assertion", "operations", "declaration"):
        return None
    if model.kind not in _QUERY_IS_TABLE:
        reason = _NOT_THE_TABLE.get(model.kind, f"a {model.kind} model's table is not its query's result")
        return QueryFacts(unsupported=reason, not_the_table=True)
    if not model.sql.strip():
        return QueryFacts(unsupported="the model has no query")
    try:
        tree = sqlglot.parse_one(model.sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return QueryFacts(unsupported="its SQL could not be read")
    if not isinstance(tree, exp.Query):
        return QueryFacts(unsupported="its SQL is not one query")
    spelling = {name.casefold(): name for name in pipeline.output_columns(key)}
    return infer(session, model.sql, tables_read(session, tree), spelling)


def never_null(session: "Session", key: str, column: str) -> bool:
    """Whether model ``key``'s query never writes NULL to ``column``, resting on nothing declared (``Session.column_is_nullable``)."""

    facts = model_facts(session, key)
    return facts is not None and not facts.unsupported and facts.proven_never_null(column)


def query_never_null(session: "Session", sql: str, tables: Mapping[str, str | None]) -> tuple[bool, ...] | None:
    """For each output column of ``sql`` (a subquery of a draft), whether the prover shows it is never NULL without a declared claim.

    ``None`` when the prover cannot read it (a correlated subquery, a table with unknown columns, ...). Only names in
    ``tables`` (the draft's own tables) are read: any other name is a CTE of the draft, which the subquery text alone does
    not define, so it is never looked up as a project table of the same name.
    """

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return None
    reads = tables_read(session, tree)
    if any(spelled not in tables for spelled in reads):
        return None
    facts = infer(session, sql, {spelled: tables[spelled] for spelled in reads}, with_inferred=True)
    return None if facts.unsupported else facts.proven_by_position


def unique_key(session: "Session", key: str) -> tuple[str, ...] | None:
    """The smallest set of columns model ``key``'s query proves unique, or ``None`` (``()`` means at most one row).

    The fan-out check of ``check_query`` uses it when the model's grain is unknown.
    """

    facts = model_facts(session, key)
    keys = facts.proven_keys() if facts is not None and not facts.unsupported else []
    return keys[0] if keys else None


# --------------------------------------------------------------------- answers


def _names(names: list[str]) -> str:
    shown = ", ".join(names[:NAMES_SHOWN])
    return shown + (f", ... (+{len(names) - NAMES_SHOWN} more)" if len(names) > NAMES_SHOWN else "")


def _sources(found: set[str]) -> str:
    ordered = [s for s in _ORDER if s in found] + sorted(s for s in found if s not in _ORDER)
    return ", ".join(ordered)


def _given(givens: list[tuple[str, str]]) -> str:
    unique = list(dict.fromkeys(givens))
    if not unique:
        return ""
    text, source = unique[0]
    more = f" and {len(unique) - 1} more" if len(unique) > 1 else ""
    return f"; given {text} [{source}]{more}"


def fact_lines(facts: QueryFacts | None, *, grain=None, snapshot_nullable: Mapping[str, bool] | None = None,
               other_null: Mapping[str, set[str]] | None = None, other_keys: Mapping[tuple[str, ...], set[str]] | None = None) -> list[str]:
    """The facts as short lines, most useful first: row count, unique column sets, disagreements, never-NULL columns.

    ``snapshot_nullable`` is the snapshot's ``is_nullable`` by column; ``other_null`` and ``other_keys`` hold the facts other
    sources give (column -> sources, key -> sources). ``grain`` is the profile's grain; it is shown only where a key of
    another source equals it, so it is not repeated on its own (``describe_table`` reports it already).
    """

    inferred = facts if facts is not None and not facts.unsupported else None
    snapshot_nullable = snapshot_nullable or {}
    lines: list[str] = []

    # rows and keys
    keys: dict[tuple[str, ...], set[str]] = {}
    givens: dict[tuple[str, ...], list] = {}
    for unique in (inferred.keys if inferred else ()):
        keys.setdefault(unique, set()).add(INFERRED)
        givens.setdefault(unique, []).extend((inferred.given or {}).get(f"key:{','.join(unique)}", ()))
    for unique, sources in (other_keys or {}).items():
        match = next((k for k in keys if {c.casefold() for c in k} == {c.casefold() for c in unique}), unique)
        keys.setdefault(match, set()).update(sources)
    if grain is not None and grain.known:
        label = GRAIN if grain.status == "derived" else DECLARED
        for unique in keys:
            if {c.casefold() for c in unique} == {c.casefold() for c in grain.keys}:
                keys[unique].add(label)
    if () in keys:
        what = "exactly one row" if inferred is not None and inferred.exactly_one_row else "at most one row"
        lines.append(clip(f"{what} ({_sources(keys.pop(()))}{_given(givens.get((), []))})", LINE))
    by_sources: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    grain_keys = {c.casefold() for c in grain.keys} if grain is not None and grain.known else None
    for unique, sources in keys.items():
        if not sources or (sources <= {GRAIN, DECLARED} and {c.casefold() for c in unique} == grain_keys):
            continue  # the grain alone: describe_table's grain says it already
        by_sources.setdefault(tuple(sorted(sources)), []).append(unique)
    for sources, group in by_sources.items():
        rests = [g for unique in group for g in givens.get(unique, [])]
        shown = "; ".join(f"({', '.join(u)})" for u in group)
        lines.append(clip(f"unique: {shown} ({_sources(set(sources))}{_given(rests)})", LINE))

    # never NULL, and where the snapshot disagrees
    columns: dict[str, set[str]] = {}
    null_givens: dict[str, list] = {}
    for name in (inferred.never_null if inferred else ()):
        columns.setdefault(name, set()).add(INFERRED)
        null_givens[name] = list((inferred.given or {}).get(f"null:{name}", ()))
    for name, sources in (other_null or {}).items():
        match = next((c for c in columns if c.casefold() == name.casefold()), name)
        columns.setdefault(match, set()).update(sources)
    conflict: dict[tuple[str, ...], list[str]] = {}
    plain: dict[tuple[str, ...], list[str]] = {}
    for name, sources in columns.items():
        if snapshot_nullable.get(name.casefold()) is True:
            conflict.setdefault(tuple(sorted(sources - {SNAPSHOT})), []).append(name)
        else:
            plain.setdefault(tuple(sorted(sources)), []).append(name)
    for sources, names in conflict.items():
        lines.append(clip(f"NULLABLE in the schema snapshot, but never NULL ({_sources(set(sources))}): {_names(names)}", LINE))
    for sources, names in sorted(plain.items(), key=lambda pair: [_ORDER.index(s) if s in _ORDER else 9 for s in pair[0]]):
        rests = [g for name in names for g in null_givens.get(name, [])]
        lines.append(clip(f"never NULL: {_names(names)} ({_sources(set(sources))}{_given(rests)})", LINE))
    return lines


def describe(answer: Answer, session: "Session", key: str, outputs: list[str], grain) -> None:
    """``describe_table``'s ``facts`` for model ``key``, and the grain's unknown sharpened by a key its query proves."""

    pipeline = session.pipeline()
    model = pipeline.models[key]
    facts = model_facts(session, key)
    types = session.column_types()
    have = {c.casefold(): c for c in outputs}
    snapshot_nullable: dict[str, bool] = {}
    other_null: dict[str, set[str]] = {}
    if types.origin.get(key) == "snapshot":
        for column in types.columns(key):
            flag = types.nullable(key, column)
            if flag is not None:
                snapshot_nullable[column.casefold()] = flag
            if flag is False:
                other_null.setdefault(have.get(column.casefold(), column), set()).add(SNAPSHOT)
    elif facts is not None and not facts.not_the_table:
        for column in outputs:  # the type checker's never-NULL (a literal, COUNT) is a fact of the query too
            if types.nullable(key, column) is False:
                other_null.setdefault(column, set()).add(INFERRED)
    for column in model.non_null:
        other_null.setdefault(have.get(column.casefold(), column), set()).add(DECLARED)
    other_keys = {tuple(unique): {DECLARED} for unique in model.unique_keys}
    lines = fact_lines(facts, grain=grain, snapshot_nullable=snapshot_nullable, other_null=other_null, other_keys=other_keys)
    answer.add_list("facts", lines, total=len(lines))  # a normal list: the cutter trims the longest list (the columns) first
    # Nothing inferred adds nothing: a missing fact already reads "not known", and an ``unknown`` entry is the last thing an
    # answer cuts, so one on most models would push out the lists that say more. ``model_facts(...).unsupported`` keeps why.
    proven = facts.proven_keys() if facts is not None and not facts.unsupported else []
    if proven and not grain.known:
        listing = answer.lists.get("unknown")
        for item in (listing.items if listing is not None else []):
            if item["what"] == "grain":
                item["reason"] += f"; its query does prove {'at most one row' if not proven[0] else '(' + ', '.join(proven[0]) + ') unique'} (see facts)"


def draft(session: "Session", sql: str, tree: exp.Expression, tables: Mapping[str, str | None]) -> list[str]:
    """``check_query``'s ``facts``: what the prover shows about the draft's output, and the draft's input columns whose sources
    disagree. ``tables`` is the draft's tables as ``tables_read`` gives them. Nothing inferred gives no line (see ``describe``)."""

    spelling = {e.alias_or_name.casefold(): e.alias_or_name for e in tree.selects} if isinstance(tree, exp.Select) else {}
    facts = infer(session, sql, tables, spelling, with_inferred=True)
    return fact_lines(facts) + _input_disagreements(session, tree, tables)


def _input_disagreements(session: "Session", tree: exp.Expression, tables: Mapping[str, str | None], limit: int = 2) -> list[str]:
    """Columns the draft uses that the schema snapshot calls NULLABLE and their model's query never writes NULL to."""

    types = session.column_types()
    used = {column.name.casefold() for column in tree.find_all(exp.Column) if column.name}
    lines: list[str] = []
    for key in sorted({k for k in tables.values() if k}):
        if types.origin.get(key) != "snapshot":
            continue
        facts = model_facts(session, key)
        if facts is None or facts.unsupported:
            continue
        both = [name for name in facts.never_null if name.casefold() in used and types.nullable(key, name) is True]
        if both:
            lines.append(clip(f"{key.split('.')[-1]}: {_names(both)} NULLABLE in the schema snapshot, but its model's query never writes NULL ({INFERRED}); the checks used the snapshot", LINE))
    return lines[:limit]
