"""``rows_filtered``: where are rows dropped on the way to this table?

The question behind "why don't my example rows show up?". ``trace_column`` says where a value comes from; this tool lists, in
path order (the table first, then each model upstream), the conditions that can remove rows on the way: every ``WHERE``,
``QUALIFY`` and ``HAVING``, the ``ON`` of every inner join and the ``EXISTS`` / ``IN (subquery)`` conditions (semi-joins), each with
its model, clause, kind and whether it is written on the column itself or on a column joined to it. Conditions come from the
lineage records the other tools use (:mod:`kumosql.lineage_filters`); nothing here parses a query a second time.

With a ``predicate`` such as ``status = 'archived'`` it also says which conditions remove the rows that satisfy it:

* ``can_remove``: proven, by the equivalence prover, that no row satisfying the predicate passes the condition;
* ``may_remove``: the condition can drop such rows and it cannot be told whether it does (it reads another table, another column,
  an aggregate or window, or the proof did not decide);
* conditions the predicate provably passes are only counted (``keeps``);
* a condition that cannot be read at all, or depends on the time it runs, is ``unknown`` with the reason, never left out.

A condition is compared with the predicate only when every column it names is a plain table of the model's own query that
the lineage resolves exactly and, for the columns the predicate names, a chain of unchanged copies leads from the table's column
to it. Anything else is an opaque value to the prover, which can only make an answer less certain, never more.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from .filter_pushdown import _NONDETERMINISTIC
from .lineage_filters import EXCLUDES, LIMITS, MATCHES, VALUE
from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_session import Session
from .mcp_tools import PAGE, _file, group_unknown, need_column, need_table
from .pipeline_types import ColumnFilter, ColumnRef

MAX_TRACES = 300  # joined columns followed to the filters of the tables they come from
MAX_PROOFS = 40  # conditions compared with a predicate by the prover in one answer
CLAUSE_CLIP = 120
SEMI = "semi_join"
ANTI = "anti_join"
INNER = "inner_join"

_PASSING = ("passthrough", "renamed")


def _short(key: str) -> str:
    return ".".join(key.split(".")[-2:])


@dataclass
class Entry:
    """One condition on the way to the table, with where it sits."""

    model: str
    found: ColumnFilter
    seq: int
    on: str = "joined"
    kind: str = ""
    node: exp.Expression | None = field(default=None, repr=False)
    parse_error: str = ""
    result: str = ""
    why: str = ""
    #: the model is switched off (``disabled: true``): Dataform never runs it
    inactive: bool = False
    #: the condition is written inside ``when(incremental(), ...)``: it applies on incremental runs only
    incremental: bool = False

    def row(self, with_result: bool) -> dict:
        item: dict = {"model": _short(self.model), "kind": self.kind, "clause": clip(self.found.condition, CLAUSE_CLIP), "on": self.on}
        if self.found.scope != "main":
            item["in"] = clip(self.found.scope, 40)
        if self.incremental:
            item["incremental_only"] = True
        if self.inactive:
            item["inactive_model"] = True
        if with_result:
            item["result"] = self.result
            if self.why:
                item["why"] = self.why
        return item


# ------------------------------------------------------------------ the predicate


def _parse_predicate(session: Session, key: str, text: str) -> tuple[exp.Expression, dict[str, tuple[str, ColumnRef]]]:
    """The predicate with each column renamed to its lower-case table column, and ``{variable: (column name, ref)}``."""

    try:
        node = parse_one(text, read="bigquery")
    except SqlglotError:
        raise ToolError("the predicate is not valid SQL", ["Write one condition on the table's columns, such as status = 'archived' or order_date = DATE '2026-01-05'."]) from None
    if isinstance(node, exp.Alias) or not isinstance(node, exp.Condition) or node.find(exp.Select, exp.Subquery, exp.Window, exp.AggFunc, exp.Star):
        raise ToolError("the predicate must be one simple condition", ["Use comparisons, IN, BETWEEN, IS NULL, AND, OR and NOT on columns and literals; no subquery, aggregate or window."])
    if node.find(*_NONDETERMINISTIC):
        raise ToolError("the predicate depends on the time it runs", ["Write the date or value out, such as order_date = DATE '2026-01-05'."])
    columns = list(node.find_all(exp.Column))
    if not columns:
        raise ToolError("the predicate names no column", ["Name a column of the table, such as status = 'archived'."])
    variables: dict[str, tuple[str, ColumnRef]] = {}
    short = key.split(".")[-1].casefold()
    for column in columns:
        written = column.name
        if column.table and column.table.casefold() != short and column.table.casefold() != key.casefold():
            written = f"{column.table}.{column.name}"
        name, path = need_column(session, key, written)
        if path:
            raise ToolError("the predicate cannot name a field inside a STRUCT column", [f"Ask with column={'.'.join((name, *path))} for the conditions that reach it."])
        variables[name.casefold()] = (name, ColumnRef(key, name))
        column.replace(exp.column(name.casefold()))
    return node, variables


def _same_value(records: dict, start: ColumnRef) -> set[ColumnRef]:
    """``start`` and every column it is an unchanged copy of, back to the sources."""

    found = {start}
    stack = [start]
    while stack:
        record = records.get(stack.pop())
        if record is None or record.status != "traced" or record.transform not in _PASSING or len(record.sources) != 1:
            continue
        (source,) = record.sources
        if source not in found:
            found.add(source)
            stack.append(source)
    return found


class _Compare:
    """Compares each condition with the predicate; at most ``MAX_PROOFS`` conditions go to the prover."""

    def __init__(self, session: Session, predicate: exp.Expression, variables: dict[str, tuple[str, ColumnRef]]) -> None:
        self.pipeline = session.pipeline()
        analysis = self.pipeline._analyse()
        self.parsed = getattr(analysis, "parsed", {}) or {}
        self.predicate = predicate
        self.same: dict[ColumnRef, str] = {}
        records = session.records()
        for var, (_, ref) in variables.items():
            for copy in _same_value(records, ref):
                self.same.setdefault(copy, var)
        self.proofs = 0
        self.skipped = 0
        self.prover_missing = False

    # A condition's column is read as a column of a table only when the lineage can follow it exactly: through plain
    # copies in CTEs and derived tables to one table that the model's query reads once.
    def _select(self, model: str, scope: str) -> exp.Select | None:
        query = self.parsed.get(model)
        if scope == "main":
            return query if isinstance(query, exp.Select) else None
        if query is not None:
            for cte in query.find_all(exp.CTE):
                if cte.alias_or_name == scope and isinstance(cte.this, exp.Select):
                    return cte.this
        return None

    def _read_once(self, model: str, table: str) -> bool:
        from kumo_sql_prover.ast_utils import binding_cte

        query = self.parsed.get(model)
        if query is None:
            return False
        reads = 0
        for node in query.find_all(exp.Table):
            if binding_cte(node) is None and self._resolve(node) == table:
                reads += 1
        return reads == 1

    def _resolve(self, table: exp.Table) -> str | None:
        try:
            return self.pipeline.resolve(table)
        except Exception:  # noqa: BLE001
            return None

    def _origin(self, model: str, select: exp.Select, qualifier: str, name: str, depth: int = 0) -> tuple[str, str] | None:
        from kumo_sql_prover.ast_utils import binding_cte

        if depth > 20:
            return None
        origin = select.args.get("from_") or select.args.get("from")
        sources = ([origin.this] if origin is not None else []) + [join.this for join in select.args.get("joins") or ()]
        # the parsed query may leave a column unqualified; with one source there is no doubt where it comes from
        found = [source for source in sources if source.alias_or_name.casefold() == qualifier.casefold()] if qualifier else sources
        if len(found) != 1:
            return None
        source = found[0]
        body = None
        if isinstance(source, exp.Table):
            cte = binding_cte(source)
            if cte is None:
                table = self._resolve(source)
                return (table, name) if table and self._read_once(model, table) else None
            body = cte.this
        elif isinstance(source, exp.Subquery):
            body = source.this
        if not isinstance(body, exp.Select):
            return None
        projections = [p for p in body.expressions if p.alias_or_name.casefold() == name.casefold()]
        if len(projections) != 1:
            return None
        inner = projections[0].this if isinstance(projections[0], exp.Alias) else projections[0]
        if not isinstance(inner, exp.Column) or isinstance(inner.this, exp.Star):
            return None
        return self._origin(model, body, inner.table, inner.name, depth + 1)

    def _translate(self, entry: Entry) -> tuple[exp.Expression, set[str]]:
        node = entry.node.copy()
        select = self._select(entry.model, entry.found.scope) if entry.found.columns_complete else None
        columns = list(node.find_all(exp.Column))
        refs: dict[tuple[str, str], ColumnRef | None] = {}
        for column in columns:
            token = (column.table.casefold(), column.name.casefold())
            if token in refs:
                continue
            origin = self._origin(entry.model, select, column.table, column.name) if select is not None else None
            refs[token] = None
            if origin is not None:
                refs[token] = next((r for r in entry.found.columns if r.table == origin[0] and r.column.casefold() == origin[1].casefold() and not r.path), None)
        # two names that end at the same table column are two row sets (a self join): neither is read as that column
        taken = [ref for ref in refs.values() if ref is not None]
        mapped: set[str] = set()
        opaque: dict[tuple, str] = {}
        for column in columns:
            token = (column.table.casefold(), column.name.casefold())
            ref = refs[token]
            if ref is not None and taken.count(ref) > 1:
                ref = None
            var = self.same.get(ref) if ref is not None else None
            if var is not None:
                mapped.add(var)
            else:
                var = opaque.setdefault(token if ref is None else (ref,), f"zz_other_{len(opaque)}")
            column.replace(exp.column(var))
        return node, mapped

    def decide(self, entry: Entry) -> tuple[str, str]:
        """``(result, why)``: can_remove, may_remove, keeps or unknown."""

        if entry.node is None:
            if entry.incremental:
                return "may_remove", "applies on incremental runs only"
            if entry.found.condition.startswith("USING ("):
                return "may_remove", "a join on a key of another table"
            return "unknown", entry.parse_error or "unparsed"
        if entry.node.find(*_NONDETERMINISTIC):
            return "unknown", "non_deterministic_filter"
        if entry.kind in (SEMI, ANTI):
            return "may_remove", "depends on the rows of another table"
        if entry.node.find(exp.Window, exp.AggFunc):
            return "may_remove", "depends on the other rows of its group"
        node, mapped = self._translate(entry)
        if not mapped:
            return "may_remove", "reads other columns"
        if self.proofs >= MAX_PROOFS:
            self.skipped += 1
            return "may_remove", "not compared (limit reached)"
        self.proofs += 1
        return self._prove(node)

    def _prove(self, condition: exp.Expression) -> tuple[str, str]:
        from kumo_sql_prover.smt_equivalence import SmtStatus, prove_equivalent_smt

        names = sorted({c.name for c in condition.find_all(exp.Column)} | {c.name for c in self.predicate.find_all(exp.Column)})
        select = ", ".join(names)
        wanted, test = self.predicate.sql("bigquery"), condition.sql("bigquery")
        try:
            never = prove_equivalent_smt(f"SELECT {select} FROM t WHERE ({wanted}) AND ({test})", f"SELECT {select} FROM t WHERE FALSE", schema={"t": names})
            if never.status is SmtStatus.PROVEN_EQUIVALENT:
                return "can_remove", ""
            if "z3-solver is not installed" in never.reason:
                self.prover_missing = True
                return "may_remove", "the prover is not installed"
            kept = prove_equivalent_smt(f"SELECT {select} FROM t WHERE {wanted}", f"SELECT {select} FROM t WHERE ({wanted}) AND ({test})", schema={"t": names})
        except Exception:  # noqa: BLE001 - one condition the prover cannot read must not end the answer
            return "may_remove", "the prover could not read it"
        if kept.status is SmtStatus.PROVEN_EQUIVALENT:
            return "keeps", ""
        return "may_remove", "not provable either way"


# ------------------------------------------------------------------ gathering


def _kind(found: ColumnFilter, node: exp.Expression | None) -> str:
    if found.kind in ("where", "join") and node is not None:
        sub = node.find(exp.Exists) or next((n for n in node.find_all(exp.In) if n.args.get("query")), None)
        if sub is not None:
            negated = isinstance(sub.parent, exp.Not)
            return ANTI if negated else SEMI
    return INNER if found.kind == "join" else found.kind


def _gather(session: Session, refs: list[ColumnRef]) -> dict:
    """Every condition on the way to ``refs``, those of the tables joined to them included."""

    pipeline = session.pipeline()
    traces = {}
    covered: set[ColumnRef] = set(refs)
    found: dict[tuple[str, ColumnFilter], int] = {}
    unknown_models: set[str] = set()
    unknown_steps: dict[ColumnRef, str] = {}
    sources: set[ColumnRef] = set()
    queue = list(refs)
    primary: set[ColumnRef] = set(refs)
    limit_hit = False
    index = 0
    while index < len(queue):
        ref = queue[index]
        index += 1
        trace = pipeline.trace_column(ref)
        traces[ref] = trace
        covered |= trace.upstream
        if index <= len(refs):
            primary |= trace.upstream
            sources |= trace.sources
        unknown_models |= set(trace.filters_unknown)
        unknown_steps.update(dict(trace.unknown))
        for model, item in trace.filters:
            found.setdefault((model, item), len(found))
            if item.effect != LIMITS:
                continue
            for column in item.columns:
                if column not in covered and column not in queue:
                    if len(queue) >= MAX_TRACES:
                        limit_hit = True
                    else:
                        queue.append(column)
    return {"filters": found, "primary": primary, "unknown_models": unknown_models, "unknown_steps": unknown_steps, "sources": sources, "limit_hit": limit_hit}


def _entries(session: Session, gathered: dict) -> tuple[list[Entry], dict[str, int]]:
    order = session.order()
    models = session.pipeline().models
    entries: list[Entry] = []
    skipped = {"outer_join_on": 0, "scalar_subquery": 0, "except": 0}
    for (model, item), seq in gathered["filters"].items():
        if item.effect == MATCHES:
            skipped["outer_join_on"] += 1
            continue
        if item.effect == VALUE:
            skipped["scalar_subquery"] += 1
            continue
        if item.effect == EXCLUDES:
            skipped["except"] += 1
            continue
        entry = Entry(model, item, seq, inactive=bool(getattr(models.get(model), "disabled", False)), incremental="incremental(" in item.condition)
        try:
            entry.node = parse_one(item.condition, read="bigquery")
        except SqlglotError:
            entry.parse_error = "a Dataform expression or syntax the condition reader does not take" if "${" in item.condition else "the condition could not be read"
        entry.kind = _kind(item, entry.node)
        if any(column in gathered["primary"] for column in item.columns):
            entry.on = "column"
        elif not item.columns and item.columns_complete:
            entry.on = "constant"
        elif not item.columns:
            entry.on = "unknown"
        entries.append(entry)
    entries.sort(key=lambda e: (-order.get(e.model, 0), e.seq))
    return entries, {name: count for name, count in skipped.items() if count}


# ------------------------------------------------------------------ the tool


@tool(
    "rows_filtered",
    "Where are rows dropped",
    "Why don't my example rows show up? Lists, from the table upstream towards the sources, the conditions that can remove rows "
    "on the way to a column: WHERE, QUALIFY, HAVING, inner joins and EXISTS / IN (subquery), each with its model, clause, kind "
    "and whether it is on the column itself or on a column joined to it. With a predicate (status = 'archived') it says which "
    "conditions can_remove the rows that satisfy it (proven), which may_remove them (cannot tell) and which could not be "
    "checked, with the reason. Reads only the loaded project.",
    {
        "table": {"type": "string", "description": "A table of the project: full key, dataset.table or the table name."},
        "column": {"type": "string", "description": "An output column of the table whose rows are wanted (a STRUCT field may follow a dot)."},
        "predicate": {
            "type": "string",
            "description": "One simple condition on the table's columns for the rows you expect, such as status = 'archived' or order_date = DATE '2026-01-05'.",
        },
    },
    required=("table",),
)
def rows_filtered(session: Session, args: dict) -> Answer:
    pipeline = session.pipeline()
    key = need_table(session, args["table"])
    column, text = args.get("column"), args.get("predicate")
    if not column and not text:
        raise ToolError("pass a column or a predicate", [f"rows_filtered table={key} column=<a column> lists the conditions that reach it; predicate=\"status = 'archived'\" checks rows you expect."])
    if key not in pipeline.models:
        answer = Answer(f"{_short(key)} is a source: the project does not build it, so no condition of the project sits upstream of it.")
        answer.fields["table"] = key
        answer.unknown("which rows the source holds", "the project does not define it; profile_table shows the values it holds")
        answer.next(f"impact table={key} lists the models that read it, then ask rows_filtered of one of them.")
        return answer

    refs: list[ColumnRef] = []
    if column:
        name, path = need_column(session, key, column)
        refs.append(ColumnRef(key, name, path))
    predicate = variables = None
    if text:
        predicate, variables = _parse_predicate(session, key, text)
        for _, ref in variables.values():
            if ref not in refs:
                refs.append(ref)
    label = _short(key) + "." + ".".join(refs[0].dotted.split(".")) if column else _short(key)

    gathered = _gather(session, refs)
    entries, skipped = _entries(session, gathered)
    compare = _Compare(session, predicate, variables) if predicate is not None else None
    if compare is not None:
        for entry in entries:
            entry.result, entry.why = compare.decide(entry)
    return _answer(session, key, label, str(text) if text else None, refs, gathered, entries, skipped, compare, offset_wanted(args))


def _answer(session, key, label, text, refs, gathered, entries, skipped, compare, offset) -> Answer:
    models = {e.model for e in entries}
    counts: dict = {"conditions": len(entries), "models": len(models), "on_column": sum(e.on == "column" for e in entries)}
    shown = entries
    if compare is not None:
        tally = {name: sum(e.result == name for e in entries) for name in ("can_remove", "may_remove", "keeps", "unknown")}
        counts.update(tally)
        rank = {"can_remove": 0, "may_remove": 1, "unknown": 2, "keeps": 3}
        shown = sorted((e for e in entries if e.result in ("can_remove", "may_remove")), key=lambda e: rank[e.result])
    else:
        counts["joined"] = sum(e.on == "joined" for e in entries)
    if skipped:
        counts["not_listed"] = skipped
    path_models = sorted({ref.table for ref in gathered["primary"] if ref.table in session.pipeline().models})
    inactive = [m for m in path_models if session.pipeline().models[m].disabled]
    incremental = [m for m in path_models if session.pipeline().models[m].kind == "incremental"]
    if any(e.inactive for e in entries):
        counts["in_inactive_models"] = sum(e.inactive for e in entries)
    summary = _summary(label, text, entries, counts, compare is not None)
    nearest = next((e for e in entries if e.result == "can_remove"), None) if compare is not None else None
    if nearest is not None:
        summary += f" First removed at {_short(nearest.model)}."
    answer = Answer(summary)
    answer.fields["table"] = key
    if compare is not None:
        first = next((e for e in entries if e.result == "can_remove"), None)
        if first is not None:
            answer.fields["first_removed_at"] = f"{_short(first.model)} ({first.kind.replace('_', ' ')}{', in an inactive model' if first.inactive else ''})"
        else:
            answer.fields["first_removed_at"] = "none proven"
    answer.counts = counts
    answer.add_list("filters", [e.row(compare is not None) for e in shown][offset:][:PAGE], total=len(shown), offset=offset, primary=True)

    group_unknown(answer, [(_short(model), "conditions could not be worked out") for model in sorted(gathered["unknown_models"])], "model whose row filters")
    group_unknown(answer, [(str(ref), reason.split(":")[0]) for ref, reason in sorted(gathered["unknown_steps"].items(), key=lambda p: str(p[0]))], "column on the path")
    if compare is not None:
        group_unknown(answer, [(f"{_short(e.model)}: {clip(e.found.condition, 40)}", e.why) for e in entries if e.result == "unknown"], "condition")
        if compare.skipped:
            answer.unknown(f"{compare.skipped} condition{'s' if compare.skipped != 1 else ''} compared by reading only", f"the prover compares at most {MAX_PROOFS} conditions per answer; they are listed as may_remove")
    elif any(e.on == "unknown" for e in entries):
        answer.unknown("how some conditions relate to the column", "their columns could not be worked out, so on is unknown for them")
    if inactive:
        names = ", ".join(_short(m) for m in inactive[:3]) + (", ..." if len(inactive) > 3 else "")
        answer.unknown(f"{len(inactive)} inactive model{'s' if len(inactive) != 1 else ''} on the path: {names}", "disabled: true, so Dataform never runs them: their tables may be missing or stale, and their conditions may not describe the rows that exist")
    if incremental:
        answer.unknown(f"{len(incremental)} incremental model{'s' if len(incremental) != 1 else ''} on the path", "they keep rows from earlier runs, so a condition changed since then does not describe the rows already stored; a condition marked incremental_only applies to incremental runs only")
    if gathered["limit_hit"]:
        answer.unknown("conditions of joined tables", f"more than {MAX_TRACES} joined columns; the tables beyond that were not followed")
    if gathered["sources"]:
        count = len({s.table for s in gathered["sources"]})
        answer.unknown(f"rows held by {count} source table{'s' if count != 1 else ''}", "the project does not build them, so what they hold is not visible; profile_table shows their values")

    _next_steps(session, answer, key, refs, text, entries, shown, compare)
    return answer


def _summary(label: str, text: str | None, entries: list[Entry], counts: dict, with_predicate: bool) -> str:
    models = f"{counts['models']} model{'s' if counts['models'] != 1 else ''}"
    if not with_predicate:
        n = len(entries)
        if not n:
            return f"{label}: no condition that removes rows was found upstream (in the models that could be analysed)."
        return (f"{label}: {n} condition{'s' if n != 1 else ''} in {models} can remove rows "
                f"({counts['on_column']} on the column itself, {counts['joined']} on a joined column).")
    return (f"For rows with {clip(text, 60)}: {counts['can_remove']} condition{'s' if counts['can_remove'] != 1 else ''} remove them, "
            f"{counts['may_remove']} may, {counts['keeps']} keep them, {counts['unknown']} could not be checked ({len(entries)} in {models}).")


def _next_steps(session, answer, key, refs, text, entries, shown, compare) -> None:
    column = refs[0].dotted if refs and refs[0].table == key else None
    if compare is None and entries and column:
        answer.next(f"rows_filtered table={_short(key)} column={column} predicate=\"<the rows you expect>\" says which of them remove those rows.")
    first = next((e for e in shown), None)
    if first is not None:
        path = _file(session, first.model)
        if compare is None or first.result == "can_remove":
            answer.next(f"Open {path} for the {first.kind.replace('_', ' ')}: {clip(first.found.condition, 60)}." if path else f"describe_table table={_short(first.model)} shows its filters.")
        elif path:
            answer.next(f"Open {path}: nothing was proven to remove those rows, so the first condition to check is {clip(first.found.condition, 50)}.")
    elif entries and first is None:
        path = _file(session, entries[0].model)
        answer.next(f"Open {path} to read the {len(entries)} condition{'s' if len(entries) != 1 else ''} the predicate could not be checked against." if path else f"describe_table table={_short(entries[0].model)} shows its filters.")
    if not entries:
        answer.next("If rows are still missing, the cause is in the data: profile_table shows what a table holds.")
