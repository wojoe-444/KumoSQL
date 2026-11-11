"""The first four questions of the agent-facing server: describe a table, trace a column, assess a change, find an existing table.

Each tool is one function registered with :func:`kumosql.mcp_answers.tool`. It takes the :class:`~kumosql.mcp_session.Session`
and the checked arguments and returns an :class:`~kumosql.mcp_answers.Answer`; the server bounds its size. A tool never
writes and never reaches a database. What it cannot determine goes under ``unknown`` with the reason, and the last
thing in an answer is a next step.
"""

from __future__ import annotations

import re
from collections import Counter

from . import boundary
from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_session import Session
from .pipeline_types import ColumnRef

PAGE = 200  # most items one call hands to the size bound

#: plain-language phrases for the reason codes that table profiles, lineage and impact use
REASONS = {
    "nested_field": "reads a field of a computed value or through a path that is not a plain list of field names",
    "unnest_element": "reads an element of an array that the query unnests, which is not a column of any table",
    "ambiguous_column": "the column name could come from more than one table",
    "source_without_grain": "reads a table that is not defined in the project, so which columns identify a row is unknown",
    "unexpanded_star": "SELECT * over a table whose columns are not known",
    "unknown_column": "the column is not among the columns the project knows for that table",
    "unparsed_model": "the model's SQL could not be read",
    "unparsed": "the model's SQL could not be read",
    "cycle": "the model sits in a dependency cycle",
    "incomplete_lineage": "some column meanings are unknown, so tables cannot be compared",
    "grain_unknown": "which columns identify a row is unknown",
    "analysis_gaps": "some models could not be fully analysed, so the list is a lower bound",
    "template_columns": "a template expression may read columns that are not known",
    "unresolved_template": "a template expression could not be resolved",
    "outside_scope": "outside the requested scope",
    "opaque_filter": "a filter could not be put in comparable form",
    "non_deterministic_filter": "a filter depends on the time it runs",
    "input_not_comparable": "a table it reads has a row scope that cannot be compared",
    "grouping_sets": "GROUPING SETS or ROLLUP rows have no single grain",
    "set_returning_select": "the query returns more than one row per input row",
    "unsupported_source": "reads a source of a kind that is not analysed",
    "parse_error": "the SQL could not be parsed",
    "no_query": "the model has no query",
    "unparsed_operation": "an operation statement could not be read",
    "insert_target_columns": "an INSERT without a column list",
    "qualify_error": "column names could not be resolved",
}

_COLUMN_MEANING = re.compile(r"col:(\S+?)(?=[\s(),|]|$)")


def explain(code: object) -> str:
    """A reason code with its plain-language meaning when there is one."""

    text = str(code or "unknown")
    head = text.split(":")[0]
    return f"{head}: {REASONS[head]}" if head in REASONS else text


def short_meaning(text: object) -> str:
    """A column meaning without the project part of its table names, to keep answers small."""

    def trim(match: re.Match) -> str:
        parts = match.group(1).split(".")
        return "col:" + ".".join(parts[1:] if len(parts) >= 4 else parts)

    return _COLUMN_MEANING.sub(trim, str(text))


def need_table(session: Session, text: object) -> str:
    """The key of the table an agent named, or a ``ToolError`` that offers the close ones."""

    key, close = session.resolve(text)
    if key is not None:
        return key
    if close:
        raise ToolError(
            f"no single table matches {clip(text, 80)!r}",
            [f"Did you mean {close[0]}? {len(close) - 1} other close name{'s' if len(close) != 2 else ''} in candidates; any table is accepted as a table name, dataset.table or the full key."]
            if len(close) > 1
            else [f"Use {close[0]}."],
            candidates=close,
        )
    raise ToolError(
        f"no table named {clip(text, 80)!r} in this project",
        ["Check the spelling; a table is a model of the project or a declared source. Names may be given as table, dataset.table or the full key."],
    )


_REF = re.compile(r"""\$\{\s*(?:ref|resolve)\(\s*(["'`])(?P<first>[^"'`]+)\1\s*(?:,\s*(["'`])(?P<second>[^"'`]+)\3\s*)?\)\s*\}""")


def expand_refs(session: Session, sql: str) -> tuple[str, list[str]]:
    """A draft with each Dataform ``${ref("table")}`` / ``${ref("dataset", "table")}`` read as that table of the project.

    An agent drafting inside a Dataform project writes ``ref()``; the checks read plain SQL. The reference becomes the
    table's ``dataset.table`` name (the last two parts of its key, the bare name for a one-part key). Returns the SQL and
    one ``ref(...) = dataset.table`` note per distinct reference; a reference to no table, or to several, is a
    ``ToolError`` that names the close ones, like every other tool. Other ``${...}`` expressions stay as written.
    """

    notes: dict[str, str] = {}

    def swap(match: re.Match) -> str:
        first, second = match.group("first").strip(), (match.group("second") or "").strip()
        written = f"{first}.{second}" if second else first
        key = need_table(session, written)
        name = ".".join(key.split(".")[-2:])
        notes.setdefault(f"ref({written})", name)
        return name

    expanded = _REF.sub(swap, sql) if "${" in sql else sql
    return expanded, [f"{ref} = {name}" for ref, name in notes.items()]


def leftover_expressions(sql: str, limit: int = 3) -> list[str]:
    """The first ``${...}`` expressions still in ``sql``, clipped (what ``ref()`` expansion could not read)."""

    found = re.findall(r"\$\{[^}]*\}?", sql)
    return [clip(item, 40) for item in dict.fromkeys(found)][:limit]


def need_column(session: Session, key: str, text: str) -> tuple[str, tuple[str, ...]]:
    """The output column (and struct field path) an agent named, matched in any case."""

    pipeline = session.pipeline()
    known = list(pipeline.output_columns(key)) or list(pipeline.source_schema.get(key, {}))
    by_name = {name.casefold(): name for name in known}
    name = text.strip().strip("`")
    if name.casefold() in by_name:
        return by_name[name.casefold()], ()
    head, _, rest = name.partition(".")
    if rest and head.casefold() in by_name:
        return by_name[head.casefold()], tuple(rest.split("."))
    if not known:
        raise ToolError(f"the columns of {key} are not known", ["The project does not define this table's columns; pass a source schema with --source-schema."])
    close = [column for column in known if name.casefold() in column.casefold()][:8] or known[:8]
    raise ToolError(
        f"{key} has no column {clip(name, 60)!r}",
        [f"Its columns include: {', '.join(close)} ({len(known)} in all)."],
        columns=len(known),
    )


def group_unknown(answer: Answer, pairs: list[tuple[str, str]], what: str) -> None:
    """Add one unknown per reason, naming up to three examples, so a hundred of the same reason cost one line."""

    by_reason: dict[str, list[str]] = {}
    for item, reason in pairs:
        by_reason.setdefault(reason, []).append(item)
    for reason, items in sorted(by_reason.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        examples = ", ".join(items[:3]) + (", ..." if len(items) > 3 else "")
        answer.unknown(f"{len(items)} {what}{'s' if len(items) != 1 else ''}: {examples}", explain(reason))


def _project_check(session: Session, key: str) -> str | None:
    """One line from the static project checks about ``key`` (declared and built, disabled), or ``None``."""

    from . import project_checks  # imported here so the tool list keeps the order of TOOL_MODULES

    notes = project_checks.table_notes(session, key)
    return clip("; ".join(notes[:2]), 260) if notes else None


def _file(session: Session, key: str) -> str | None:
    model = session.pipeline().models.get(key)
    return model.path if model is not None else None


def _filter_text(found) -> str:
    where = f" [{found.scope}]" if found.scope != "main" else ""
    effect = "" if found.effect == "limits_rows" else f" ({found.effect})"
    return clip(f"{found.kind}{where}{effect}: {found.condition}", 150)


# ------------------------------------------------------------------ describe_table


@tool(
    "describe_table",
    "Describe a table",
    "What is this table? Its grain (what one row is), the rows it keeps, what each column holds, the tables it reads and "
    "who reads it, and whether a saved data profile exists. Use it before reading any SQL.",
    {"table": {"type": "string", "description": "A table of the project: full key, dataset.table or the table name."}},
    required=("table",),
)
def describe_table(session: Session, args: dict) -> Answer:
    pipeline = session.pipeline()
    key = need_table(session, args["table"])
    readers = sorted(session.downstream().get(key, ()))
    if key not in pipeline.models:
        return _describe_source(session, key, readers, offset_wanted(args))
    model = pipeline.models[key]
    profile = session.profiles()[key]
    role = session.roles().get(key)
    reads = sorted(pipeline.upstream.get(key, ()))
    outputs = list(pipeline.output_columns(key))

    meanings = {a.column.casefold(): a for a in profile.attributes}
    columns = []
    unknown_columns = []
    for name in outputs or [a.column for a in profile.attributes]:
        attribute = meanings.get(name.casefold())
        if attribute is not None and attribute.status == "known":
            columns.append(clip(f"{name} = {short_meaning(attribute.meaning)}", 110))
        else:
            reason = (attribute.reason if attribute is not None else None) or "unknown"
            columns.append(f"{name} = ? ({reason})")
            unknown_columns.append((name, reason))

    own_filters, filters_unknown = _own_filters(session, key, outputs)
    unused = list(session.dead_columns().get(key, ()))
    grain = profile.grain

    answer = Answer("")
    answer.fields["table"] = key
    answer.fields["kind"] = model.kind
    if model.path:
        answer.fields["file"] = model.path
    if model.tags:
        answer.fields["tags"] = list(model.tags)
    if role is not None and role.role != "unknown":
        answer.fields["role"] = f"{role.role} ({role.confidence} confidence)"
    answer.fields["grain"] = list(grain.keys) if grain.known else None
    if grain.known and not grain.keys:
        answer.fields["grain"] = "single row"
    answer.fields["grain_status"] = grain.status
    if (origin := session.grain_origin(key, grain)) is not None:
        answer.fields["grain_origin"] = origin
    answer.fields["rows_comparable"] = profile.row_scope.comparable
    saved = session.saved_profile(key)
    if saved is not None:
        answer.fields["data_profile"] = {
            "rows": saved["row_count"], "columns": saved["columns"], "generated": saved["generated_at"],
            "read": f"kumosql://data-profile/{saved['name']}/summary.md",
        }
    else:
        answer.fields["data_profile"] = "none saved"
    if (note := _project_check(session, key)) is not None:
        answer.fields["project_check"] = note
    answer.counts = {
        "columns": len(columns), "meaning_unknown": len(unknown_columns), "reads": len(reads), "read_by": len(readers),
        "own_filters": len(own_filters), "unused_columns": len(unused),
    }
    answer.add_list("columns", columns[offset_wanted(args):][:PAGE], total=len(columns), offset=offset_wanted(args), primary=True)
    answer.add_list("own_filters", own_filters[:PAGE], total=len(own_filters))
    answer.add_list("reads", reads[:PAGE], total=len(reads))
    answer.add_list("read_by", readers[:PAGE], total=len(readers))
    answer.add_list("unused_columns", unused[:PAGE], total=len(unused))

    if not grain.known:
        answer.unknown("grain", explain(grain.reason))
    if unknown_columns:
        group_unknown(answer, [(name, reason) for name, reason in unknown_columns], "column meaning")
    if not profile.row_scope.comparable:
        answer.unknown("rows kept", explain(profile.row_scope.reason))
    if filters_unknown:
        answer.unknown("row filters", "the conditions of some columns could not be worked out")
    if saved is None:
        answer.unknown("value statistics", "no saved data profile; `python -m kumosql profile-table` reads the table to make one")

    answer.summary = _card_summary(key, model.kind, grain, len(columns), len(unknown_columns), len(reads), len(readers))
    if unknown_columns:
        answer.next(f"trace_column table={key} column={unknown_columns[0][0]} shows how far its source can be followed.")
    if readers:
        answer.next(f"impact table={key} lists what a change to it breaks.")
    elif not unknown_columns:
        answer.next("Nothing in the project reads it: it is an output. impact table=... column=... lists a column's readers.")
    if model.path:
        answer.next(f"Open {model.path} for its SQL.")
    boundary.annotate_table(session, key, answer)
    return answer


def _card_summary(key: str, kind: str, grain, columns: int, unknown: int, reads: int, readers: int) -> str:
    name = key.split(".")[-1]
    if grain.known and grain.keys:
        shape = f"one row per {', '.join(grain.keys)}"
    elif grain.known:
        shape = "a single row"
    else:
        shape = "grain unknown"
    meaning = "all column meanings known" if not unknown else f"{unknown} of {columns} column meanings unknown"
    return f"{name} ({kind}): {shape}; {columns} columns, {meaning}; reads {reads}, read by {readers}."


def _own_filters(session: Session, key: str, outputs: list[str]) -> tuple[list[str], bool]:
    """The conditions in the model's own query, each once, however many columns they limit."""

    records = session.records()
    seen: dict[tuple[str, str, str], str] = {}
    unknown = False
    for name in outputs:
        record = records.get(ColumnRef(key, name))
        if record is None:
            continue
        if record.filters is None:
            unknown = True
            continue
        for found in record.filters:
            seen.setdefault((found.kind, found.scope, found.condition), _filter_text(found))
    return list(seen.values()), unknown


def _describe_source(session: Session, key: str, readers: list[str], offset: int) -> Answer:
    pipeline = session.pipeline()
    columns = [f"{name} {kind}" for name, kind in pipeline.source_schema.get(key, {}).items()]
    answer = Answer(f"{key.split('.')[-1]} is a source: the project reads it but does not build it; {len(columns) or 'no'} columns known, read by {len(readers)}.")
    answer.fields.update({"table": key, "kind": "source"})
    if (note := _project_check(session, key)) is not None:
        answer.fields["project_check"] = note
    answer.counts = {"columns": len(columns), "read_by": len(readers)}
    answer.add_list("columns", columns[offset:][:PAGE], total=len(columns), offset=offset, primary=True)
    answer.add_list("read_by", readers[:PAGE], total=len(readers))
    from . import join_profile_store as joins

    claim = session.grain_claim(key)
    near = None if claim is not None else joins.best_candidate(session.key_observations(), [key])
    if claim is not None:
        answer.fields.update({"grain": list(claim.keys), "grain_status": "declared", "grain_origin": claim.origin})
        answer.unknown("row filters", "the project does not define this table, so they are not known")
    elif near is not None:
        answer.fields["grain_assumed"] = clip(f"{'unique' if near.unique else 'near-key'} on ({', '.join(near.columns)}): {near.counts()}; from the saved {near.source.replace('_', ' ')} of {near.generated_at[:10]}, an assumption, not a declared grain", 220)
        answer.unknown("grain", "no key is declared for this table; the saved measurement above is the nearest, not certain")
        answer.unknown("row filters", "the project does not define this table, so they are not known")
    else:
        answer.unknown("grain and row filters", "the project does not define this table, so they are not known")
    if not columns:
        answer.unknown("columns", "no source schema was given (--source-schema)")
    if readers:
        answer.next(f"impact table={key} lists what a change to it breaks.")
    boundary.annotate_table(session, key, answer)
    return answer


# ------------------------------------------------------------------ trace_column


@tool(
    "trace_column",
    "Trace a column to its sources",
    "Where does this value come from, and which rows reach it? Follows a model's output column back through every model to "
    "the source columns, lists the filters and joins that limit its rows once per model, and says which steps could not be followed.",
    {
        "table": {"type": "string", "description": "A table of the project: full key, dataset.table or the table name."},
        "column": {"type": "string", "description": "An output column of the table; a field inside a STRUCT column may follow a dot (payload.user.id)."},
    },
    required=("table", "column"),
)
def trace_column(session: Session, args: dict) -> Answer:
    pipeline = session.pipeline()
    key = need_table(session, args["table"])
    name, path = need_column(session, key, args["column"])
    ref = ColumnRef(key, name, path)
    if key not in pipeline.models:
        return _source_column(session, ref)
    trace = pipeline.trace_column(ref)
    record = session.records().get(ColumnRef(key, name))
    offset = offset_wanted(args)

    order = session.order()
    tables = sorted({up.table for up in trace.upstream if up.table in pipeline.models}, key=lambda t: (-order.get(t, 0), t))
    sources = sorted(str(ref_) for ref_ in trace.sources)
    hop = sorted(str(s) for s in record.sources) if record is not None else []
    groups = _filter_groups(trace)
    touching = _touching_count(trace)
    filters_total = sum(len(g["filters"]) + g.get("more", 0) for g in groups)

    is_model = key in pipeline.models
    kind = record.transform if record is not None else ("source column" if not is_model else "unknown")
    answer = Answer(_trace_summary(ref, kind, len(sources), len(tables), filters_total, trace))
    answer.fields["column"] = str(ref)
    answer.fields["computed_by"] = kind
    answer.fields["complete"] = trace.complete
    if hop and hop != sources:
        answer.add_list("from", hop[:PAGE], total=len(hop))
    answer.counts = {
        "source_columns": len(sources), "models_passed": len(tables), "upstream_columns": len(trace.upstream),
        "filters": filters_total, **({"filters_on_column": touching} if filters_total else {}), "read_by_columns": len(pipeline.downstream_columns(ref)),
    }
    answer.add_list("sources", sources[offset:][:PAGE], total=len(sources), offset=offset, primary=True)
    answer.add_list(
        "filters_by_model", groups[:PAGE], total=len(groups),
        unit=lambda group: (len(group["filters"]), len(group["filters"]) + group.get("more", 0)), unit_name="conditions",
    )
    answer.add_list("via_models", tables[:PAGE], total=len(tables))

    if record is not None and record.status == "unknown":
        answer.unknown(f"how {ref} is built", explain(record.reason))
    group_unknown(answer, [(str(item), reason.split(":")[0]) for item, reason in trace.unknown if item != ref], "column on the path")
    if trace.filters_unknown:
        group_unknown(answer, [(table, "conditions could not be worked out") for table in trace.filters_unknown], "model whose row filters")

    first_unknown = next((item for item, _ in trace.unknown if item.table in pipeline.models), None)
    if first_unknown is not None:
        answer.next(f"describe_table table={first_unknown.table} shows why it could not be followed.")
    if filters_total > 3:
        answer.next(f"rows_filtered table={key} column={name} lists every condition in path order; add predicate=... to check rows you expect.")
    if pipeline.downstream_columns(ref):
        answer.next(f"impact table={key} column={name} lists the models that read it.")
    if _file(session, key):
        answer.next(f"Open {_file(session, key)} where {name} is computed.")
    boundary.annotate_trace(session, trace.sources, answer, source_tables={ref_.table for ref_ in trace.sources})
    return answer


def _source_column(session: Session, ref: ColumnRef) -> Answer:
    readers = sorted({column.table for column in session.pipeline().downstream_columns(ref)})
    answer = Answer(f"{ref.table.split('.')[-1]}.{ref.dotted} is a column of a source: the project does not build it, so nothing is upstream; {len(readers)} model{'s' if len(readers) != 1 else ''} compute values from it.")
    answer.fields["column"] = str(ref)
    answer.fields["computed_by"] = "source column"
    answer.counts = {"read_by_models": len(readers)}
    answer.add_list("read_by", readers[:PAGE], total=len(readers), primary=True)
    answer.next(f"impact table={ref.table} column={ref.dotted} lists what depends on it.")
    boundary.annotate_trace(session, [ref], answer)
    return answer


def _trace_summary(ref: ColumnRef, kind: str, sources: int, models: int, filters: int, trace) -> str:
    where = f"{sources} source column{'s' if sources != 1 else ''} through {models} model{'s' if models != 1 else ''}"
    if not sources and trace.complete:
        where = "no source column (literals only)" + (f", through {models} model{'s' if models != 1 else ''}" if models else "")
    text = f"{ref.table.split('.')[-1]}.{ref.dotted} ({kind}) comes from {where}; {filters} row condition{'s' if filters != 1 else ''} limit it."
    if trace.complete:
        return text
    return f"{text} The trace is incomplete: {len(trace.unknown)} step{'s' if len(trace.unknown) != 1 else ''} could not be followed, so the sources are a lower bound."


def _filter_groups(trace) -> list[dict]:
    """The conditions that limit the rows reaching the column, grouped under the model that wrote them.

    Conditions that read the column itself (or a column it is built from) come first, in their model and among the models,
    so that a cut for size loses the conditions on other columns before them.
    """

    mine = set(trace.upstream) | {trace.column}
    grouped: dict[str, dict[str, bool]] = {}
    for model, found in trace.filters:
        texts = grouped.setdefault(model, {})
        text = _filter_text(found)
        texts[text] = texts.get(text, False) or any(column in mine for column in found.columns)
    groups = []
    for model, texts in grouped.items():
        ordered = sorted(texts, key=lambda text: not texts[text])
        shown = ordered[:3]
        group: dict = {"model": model, "filters": shown}
        if len(ordered) > len(shown):
            group["more"] = len(ordered) - len(shown)
        groups.append((not any(texts.values()), group))
    groups.sort(key=lambda pair: pair[0])
    return [group for _, group in groups]


def _touching_count(trace) -> int:
    """How many distinct conditions read the traced column or a column it is built from."""

    mine = set(trace.upstream) | {trace.column}
    return len({(model, _filter_text(found)) for model, found in trace.filters if any(column in mine for column in found.columns)})


# ------------------------------------------------------------------ impact

_EFFECTS = {"breaks": 0, "values_change": 1, "behavior_may_change": 2, "indirect": 3}


@tool(
    "impact",
    "What breaks if I change this",
    "What breaks if a column or table is dropped, renamed or its expression changed? Lists the models that break or change, "
    "nearest first, the readers that could not be analysed, and the files to open. It never says a deletion is safe: "
    "readers outside the project (query history, dashboards) are not visible.",
    {
        "table": {"type": "string", "description": "A table of the project: full key, dataset.table or the table name."},
        "column": {"type": "string", "description": "A column of the table (optional). Leave out to ask about dropping the whole table."},
        "change": {
            "type": "string",
            "enum": ["drop_column", "rename_column", "change_expression", "drop_table"],
            "description": "The change (default: drop_column when a column is given, otherwise drop_table).",
        },
    },
    required=("table",),
)
def impact(session: Session, args: dict) -> Answer:
    pipeline = session.pipeline()
    key = need_table(session, args["table"])
    column = args.get("column")
    change = args.get("change") or ("drop_column" if column else "drop_table")
    if change != "drop_table" and not column:
        raise ToolError(f"{change} needs a column", [f"Pass column, or ask about dropping the whole table: impact table={key}."])
    if change == "drop_table" and column:
        raise ToolError("drop_table takes no column", [f"Leave out column, or use change=drop_column for {key}.{column}."])
    if column:
        name, path = need_column(session, key, column)
        column = ".".join((name, *path))
    result = pipeline.assess_change(change, key, column)
    offset = offset_wanted(args)
    outside = boundary.impact_readers(session, change, key, column)  # readers named in --boundary, or None

    affected = sorted(result.affected, key=lambda a: (a.depth, _EFFECTS.get(a.effect, 9), a.model))
    items = []
    for a in affected:
        item: dict = {"model": a.model, "effect": a.effect, "via": a.via, "depth": a.depth}
        if a.columns:
            item["columns"] = list(a.columns[:4]) + (["+%d" % (len(a.columns) - 4)] if len(a.columns) > 4 else [])
        items.append(item)
    effects = Counter(a.effect for a in affected)
    label = f"{key.split('.')[-1]}.{column}" if column else key.split(".")[-1]

    answer = Answer(_impact_summary(change, label, effects, len(result.unknown), result))
    answer.fields["change"] = change
    answer.fields["target"] = f"{key}.{column}" if column else key
    answer.fields["safe_to_delete"] = "unknown (readers outside the project, such as query history and dashboards, are not visible)"
    if not result.target_known:
        answer.fields["target_known"] = False
    answer.counts = {"affected": len(affected), **{effect: effects[effect] for effect in _EFFECTS if effects[effect]},
                     "unknown_readers": len(result.unknown), "max_depth": max((a.depth for a in affected), default=0)}
    # with a boundary file, `offset` pages whichever list is longer: the outside readers when they outnumber the inside ones
    page_outside = outside is not None and len(outside.items) > len(items)
    inner = 0 if page_outside else offset
    answer.add_list("affected", items[inner:][:PAGE], total=len(items), offset=inner, primary=not page_outside)

    group_unknown(answer, [(u.model, u.reason) for u in result.unknown], "reader")
    if not result.complete:
        reasons = ", ".join(result.incomplete_reasons) or "analysis_gaps"
        answer.unknown("the whole list", explain(reasons.split(",")[0].strip()))
    if not result.target_known:
        answer.unknown(f"{label}", "the project has no such table or column, so nothing reads it that the analysis can see")

    files = []
    for a in affected:
        path = _file(session, a.model)
        if path and path not in files and a.effect in ("breaks", "values_change", "behavior_may_change"):
            files.append(path)
    if files:
        answer.next("Open " + ", ".join(files[:3]) + (f" (+{len(files) - 3} more)" if len(files) > 3 else "") + ".")
    if column and affected:
        answer.next(f"trace_column table={affected[0].model} column=<its column> shows how it reads {column}.")
    if result.unknown:
        answer.next(f"describe_table table={result.unknown[0].model} shows why that reader could not be analysed.")
    if not affected and not result.unknown:
        answer.next("No reader inside the project. Check query history and dashboards before deleting.")
    boundary.annotate_impact(answer, outside, offset=offset if page_outside else 0, primary=page_outside)
    return answer


def _impact_summary(change: str, label: str, effects: Counter, unknown: int, result) -> str:
    verb = {"drop_column": "Dropping", "rename_column": "Renaming", "change_expression": "Changing the expression of", "drop_table": "Dropping"}[change]
    if not effects:
        tail = "no model reads it" + ("; it is a final output" if result.terminal else "")
    else:
        tail = ", ".join(f"{count} {effect.replace('_', ' ')}" for effect, count in sorted(effects.items(), key=lambda p: _EFFECTS.get(p[0], 9)))
    return f"{verb} {label}: {tail}" + (f"; {unknown} reader{'s' if unknown != 1 else ''} could not be analysed" if unknown else "") + "."


# ------------------------------------------------------------------ find_existing


@tool(
    "find_existing",
    "Find an existing table",
    "Does a table for this already exist, or one I can roll up? Compares what a table of the project (or a proposed query) "
    "means, at which grain and over which rows, with every other table. When nothing can be compared it says so in a few "
    "lines and names what blocks the comparison. With expression=... it instead finds every model that computes a SQL "
    "expression or CASE ladder, as copies grouped into variants with what differs.",
    {
        "table": {"type": "string", "description": "A table of the project to look for duplicates and roll-up sources of."},
        "sql": {"type": "string", "description": "Or a proposed SELECT query, written against the project's tables."},
        "expression": {
            "type": "string",
            "description": "Or one SQL expression or CASE ladder: which models compute it, grouped into variants with what differs "
            "(alias, qualifier, spacing and, where order cannot matter, branch order are ignored).",
        },
    },
    required=(),
)
def find_existing(session: Session, args: dict) -> Answer:
    from .overlap import find_overlaps
    from .rollups import find_rollups

    pipeline = session.pipeline()
    table, sql = args.get("table"), args.get("sql")
    if args.get("expression"):
        if table or sql:
            raise ToolError("give exactly one of table, sql and expression", ["expression: a SQL expression or CASE ladder to look for in every model."])
        from .mcp_expression import find_expression

        return find_expression(session, args)
    if bool(table) == bool(sql):
        raise ToolError("give exactly one of table, sql and expression", ["table: a table of the project; sql: a proposed query; expression: a SQL expression or CASE ladder."])
    if sql and len(sql) > 50_000:
        raise ToolError("the query is too long (50,000 characters at most)")
    roles = session.roles()
    if table:
        key = need_table(session, table)
        if key not in pipeline.models:
            raise ToolError(f"{key} is a source the project does not build", ["Pass a model of the project, or a proposed query with sql."])
        declared = session.declared_grain()
        overlaps = find_overlaps(pipeline, model=key, roles=roles, declared_grain=declared)
        rollups = find_rollups(pipeline, model=key, roles=roles, declared_grain=declared)
        subject = key.split(".")[-1]
    else:
        declared = session.declared_grain()
        overlaps = find_overlaps(pipeline, sql, roles=roles, declared_grain=declared)
        rollups = find_rollups(pipeline, sql, roles=roles, declared_grain=declared)
        subject = "the proposed query"
    offset = offset_wanted(args)

    target = overlaps.target
    skipped = dict(sorted(overlaps.skipped.items(), key=lambda p: (-p[1], p[0])))
    considered = overlaps.compared + sum(skipped.values())
    found = [m for m in overlaps.matches if m.kind != "unknown"]
    derivable = [r for r in rollups.rollups if r.derivability != "unknown"]
    blockers = _target_blockers(target)

    answer = Answer("")
    answer.counts = {"tables_considered": considered, "compared": overlaps.compared, "skipped": sum(skipped.values())}
    if skipped:
        answer.counts["skipped_by_reason"] = skipped
    if not found and not derivable and overlaps.compared == 0:
        answer.summary = f"Cannot tell whether a table like {subject} exists: compared 0 of {considered} tables."
        answer.fields["verdict"] = "cannot_tell"
        if blockers:
            answer.fields["blocked_by"] = f"{subject} itself: its grain or column meanings are unknown, so nothing can be compared with it"
            for what, reason in blockers:
                answer.unknown(what, explain(reason))
            answer.next(f"describe_table table={table} lists which columns are unknown." if table else "Write the query over tables whose columns are known, or name a model with find_existing table=.")
        else:
            answer.fields["blocked_by"] = "the other tables: their grain or column meanings are unknown"
            for reason, count in skipped.items():
                answer.unknown(f"{count} tables", explain(reason))
            answer.next("describe_table on a candidate table shows which of its columns are unknown.")
        return answer

    if found:
        answer.summary = f"{len(found)} existing table{'s' if len(found) != 1 else ''} may provide {subject}; {overlaps.compared} compared, {sum(skipped.values())} could not be."
        answer.fields["verdict"] = "found"
    elif derivable:
        answer.summary = f"No table matches {subject} as it is, but {len({r.table for r in derivable})} finer-grain table(s) could be rolled up; {overlaps.compared} compared."
        answer.fields["verdict"] = "rollup_possible"
    else:
        answer.summary = f"No match among the {overlaps.compared} tables compared; {sum(skipped.values())} could not be compared."
        answer.fields["verdict"] = "none_found"
    kinds = Counter(m.kind for m in found)
    answer.counts.update({kind: kinds[kind] for kind in ("same_meaning", "contains", "partial") if kinds[kind]})
    items = [
        {"table": m.table, "kind": m.kind, "confidence": m.confidence, "columns": f"{len(m.attributes)} of {len(target.attributes)}", "why": clip(m.reason, 100)}
        for m in found
    ]
    answer.add_list("matches", items[offset:][:PAGE], total=len(items), offset=offset, primary=True)
    grouped: dict[tuple[str, str], list] = {}
    for r in derivable:
        grouped.setdefault((r.table, r.derivability), []).append(r)
    roll_items = []
    for (table_key, derivability), group in grouped.items():
        entry: dict = {"table": table_key, "derivability": derivability, "attributes": [r.attribute for r in group][:4]}
        conditions = sorted({c for r in group for c in r.conditions})
        if conditions:
            entry["conditions"] = [clip(c, 100) for c in conditions[:2]]
        roll_items.append(entry)
    order = {"derivable_exact": 0, "derivable_with_conditions": 1, "not_derivable": 2}
    roll_items.sort(key=lambda e: (order.get(e["derivability"], 3), e["table"]))
    answer.counts["rollup_tables"] = len(roll_items)
    answer.add_list("rollups", roll_items[:PAGE], total=len(roll_items))
    if sum(skipped.values()):
        answer.unknown(f"{sum(skipped.values())} of {considered} tables", ", ".join(f"{explain(r)} ({n})" for r, n in list(skipped.items())[:2]))
    if found:
        answer.next(f"describe_table table={found[0].table} to compare it with {subject}.")
    elif derivable:
        answer.next(f"describe_table table={roll_items[0]['table']} to see its grain and columns.")
    else:
        answer.next("No existing table found among those compared; build it.")
    return answer


def _target_blockers(target) -> list[tuple[str, str]]:
    """What about the thing being looked for makes every comparison impossible."""

    problems = []
    if not target.grain.known:
        problems.append(("grain", target.grain.reason or "grain_unknown"))
    unknown = [a for a in target.attributes if a.status != "known"]
    if not target.attributes:
        problems.append(("column meanings", "none could be determined; check that the query reads the project's tables by their full names"))
    if unknown:
        reasons = Counter(a.reason or "unknown" for a in unknown)
        reason = ", ".join(f"{code} {count}" for code, count in reasons.most_common(2))
        problems.append((f"{len(unknown)} of {len(target.attributes)} column meanings", explain(reasons.most_common(1)[0][0]) + (f"; all causes: {reason}" if len(reasons) > 1 else "")))
    if not target.row_scope.comparable:
        problems.append(("rows kept", target.row_scope.reason or "not comparable"))
    return problems
