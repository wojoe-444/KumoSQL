"""Two lookups the agent-facing server adds to ``find_existing`` and ``find_tables``.

* :func:`find_expression` (``find_existing expression=...``): which models compute this SQL expression or CASE ladder,
  grouped into variants with what differs (see :mod:`kumosql.expression_search` for how copies are recognized).
* :func:`link_tables` (``find_tables has_columns=[A, B]``): which tables and views carry both of two keys, by column
  name and by what the columns mean, saying which matched by name only.

Both are read-only and offline. They are called from the tools in :mod:`kumosql.mcp_tools` and :mod:`kumosql.mcp_find`;
neither registers a tool of its own.
"""

from __future__ import annotations

from . import expression_search as search
from .mcp_answers import Answer, ToolError, bytes_wanted, clip, offset_wanted
from .mcp_session import Session

MAX_EXPRESSION = 5000
#: models listed per variant at the default size; a larger ``max_bytes`` lists more
MODELS_SHOWN = 8
OTHER_GROUPS = 3
MAX_KEYS = 4
MEANINGS_BEFORE_AMBIGUOUS = 3


def expression_index(session: Session) -> search.ExpressionIndex:
    """The per-session index of normalized sub-expressions, built from the parsed trees on first use."""

    return session._once("expression_index", lambda: search.build_index(session.pipeline()))


def _plural(count: int, word: str) -> str:
    if count != 1 and word.endswith("copy"):
        word = word[:-1] + "ie"
    return f"{count} {word}{'' if count == 1 else 's'}"


def _refs(group: search.Group, limit: int) -> list[str]:
    refs = list(dict.fromkeys(f"{o.model} ({o.place})" for o in group.copies))
    return refs[:limit] + ([f"+{len(refs) - limit} more"] if len(refs) > limit else [])


def _differs(parts: list[str], limit: int = MAX_KEYS) -> str:
    lead = ""
    if parts and parts[0].startswith("vs variant"):
        lead, parts = parts[0] + ": ", parts[1:]
    shown = parts[:limit] + ([f"+{len(parts) - limit} more differences"] if len(parts) > limit else [])
    return clip(lead + "; ".join(shown), 300)


def find_expression(session: Session, args: dict) -> Answer:
    text = str(args["expression"])
    if len(text) > MAX_EXPRESSION:
        raise ToolError(f"the expression is too long ({MAX_EXPRESSION} characters at most)", ["Search one CASE or one function call at a time."])
    index = expression_index(session)
    try:
        query = search.prepare(index, text)
    except search.ExpressionError as exc:
        raise ToolError(str(exc), ["Pass one SQL expression, such as a CASE ... END ladder or a function call, written with the column names of the project."]) from exc
    result = search.search(index, query)
    limit = MODELS_SHOWN if bytes_wanted(args) <= 2000 else MODELS_SHOWN + (bytes_wanted(args) - 2000) // 60
    offset = offset_wanted(args)

    variants = result.variants
    copies = sum(len(g.copies) for g in variants)
    models = len({o.model for g in variants for o in g.copies})
    others = result.others
    answer = Answer("")
    answer.counts = {"copies": copies, "models": models, "variants": len(variants), "models_searched": index.models_searched}
    if others:
        answer.counts["partial_or_related_copies"] = sum(len(g.copies) for g in others)
    if index.unread:
        answer.counts["unreadable_models"] = len(index.unread)

    items = []
    for number, group in enumerate(variants, 1):
        item: dict = {"variant": number, "copies": len(group.copies)}
        if group.same_as_yours:
            item["same_as_yours"] = True
        item["models"] = _refs(group, limit)
        if group.differs:
            item["differs"] = _differs(group.differs)
        items.append(item)
    answer.add_list("variants", items[offset:], total=len(items), offset=offset, primary=True)

    extra = []
    for group in others[:OTHER_GROUPS]:
        item = {"kind": group.relation, "copies": len(group.copies), "models": _refs(group, min(limit, 4))}
        item["differs"] = _differs(_against_yours(group))
        extra.append(item)
    answer.add_list("partial", extra, total=len(others), keep=0)

    if variants:
        answer.summary = f"{_plural(copies, 'copy')}, {_plural(len(variants), 'variant')} in {_plural(models, 'model')}"
        if len(variants) > 1:
            first = next((g for g in variants[1:] if g.differs), variants[-1])
            answer.summary += ": " + clip(_differs(first.differs[1:] or first.differs, 2), 160)
        elif variants[0].same_as_yours:
            answer.summary += ": all identical to yours after normalization"
        answer.summary += "."
        if not any(g.same_as_yours for g in variants):
            answer.fields["yours"] = "no copy is identical to the expression you gave"
    elif others:
        answer.summary = f"No model computes this expression in full; {_plural(sum(len(g.copies) for g in others), 'related copy')} found (partial, extended or overlapping)."
    else:
        answer.summary = f"No model computes this expression (searched {_plural(index.models_searched, 'model')})."

    if index.unread:
        pairs = [(key, reason) for key, reason in index.unread.items()]
        from .mcp_tools import group_unknown

        group_unknown(answer, pairs, "model")
    if index.scripts:
        answer.unknown(
            _plural(len(index.scripts), "model"),
            "their pre or post operations or incremental branch are not searched; only the main query of each model is",
        )
    if index.not_normalized:
        answer.unknown(_plural(index.not_normalized, "expression"), "the normalizer could not follow them, so they match only when written the same")
    answer.unknown("columns under another name", "qualifiers are ignored, but a copy that reads the same value under a different column name is not found")

    if variants and len(variants) > 1:
        answer.next("Pick the variant to keep, then impact table=<model> to see what changes when the others adopt it.")
    elif variants:
        answer.next(f"describe_table table={variants[0].copies[0].model} shows where the expression sits; move it to one shared model if it is copied.")
    elif others:
        answer.next("Open the models under partial: they hold some of the branches. Add the missing ones or reuse one of them.")
    else:
        answer.next("Search a smaller piece of it (one CASE or one function call), or use find_tables for a table that holds the result.")
    if query.nested_case and variants:
        answer.next("To check just the CASE inside it, search that CASE alone.")
    return answer


def _against_yours(group: search.Group) -> list[str]:
    return [{"partial": "sub-ladder of yours", "extends": "yours plus more", "overlap": "overlaps yours"}[group.relation], *group.differs]


# ------------------------------------------------------------------ link tables


def _meanings(session: Session) -> dict[str, dict[str, str]]:
    """Per model, the meaning of each column the project could determine (``col:`` and expression meanings, not constants)."""

    def compute() -> dict[str, dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        for key, profile in session.profiles().items():
            found[key] = {
                a.column.lower(): a.meaning
                for a in profile.attributes
                if a.status == "known" and a.meaning and a.meaning.startswith(("col:", "field(", "expr:"))
            }
        for key, schema in session.pipeline().source_schema.items():  # a source column means itself
            found[key] = {column.lower(): f"col:{key}.{column}" for column in schema}
        return found

    return session._once("link_meanings", compute)


def _anchor(session: Session, wanted: str, meanings: dict[str, dict[str, str]]) -> tuple[set[str], str | None]:
    """The meanings a key stands for, and a reason when they could not be pinned down.

    ``table.column`` pins the key to that table's column; a bare name collects the meanings of every column of that name.
    """

    name = wanted.strip().strip("`")
    table_part, _, column = name.rpartition(".")
    if table_part:
        key, _close = session.resolve(table_part)
        if key is not None:
            if key in meanings and column.lower() in meanings[key]:
                return {meanings[key][column.lower()]}, None
            return set(), f"{table_part} has no column {column} with a known meaning"
    found = {m[name.lower()] for m in meanings.values() if name.lower() in m}
    if len(found) > MEANINGS_BEFORE_AMBIGUOUS:
        return set(), f"{name} means {len(found)} different things in the project; pin it with table.column"
    return found, None


def link_tables(session: Session, args: dict) -> Answer:
    from .mcp_find import _grain_text, build_index

    wanted = args["has_columns"]
    if not isinstance(wanted, list) or not 2 <= len(wanted) <= 4 or not all(isinstance(w, str) and w.strip() for w in wanted):
        raise ToolError("has_columns takes two to four column names", ["Pass has_columns=[\"customer_id\", \"order_id\"]; use table.column to pin a key to one table's column."])
    for other in ("need", "column"):
        if args.get(other):
            raise ToolError(f"has_columns is a lookup of its own; do not combine it with {other}", ["Ask again with has_columns (and kind) only."])
    kind = args.get("kind")
    meanings = _meanings(session)
    entries = session._once("find_tables_index", lambda: build_index(session))
    anchors = [_anchor(session, w, meanings) for w in wanted]
    labels = [w.strip().strip("`") for w in wanted]
    names = [label.rpartition(".")[2].lower() for label in labels]

    ranked = []
    for entry in entries:
        if kind and kind not in (entry.kind, entry.role):
            continue
        have = {c.lower(): c for c in entry.columns}
        table_meanings = meanings.get(entry.key, {})
        how: list[tuple[str, str]] = []
        for name, (anchor, _why) in zip(names, anchors):
            by_meaning = sorted(c for c, m in table_meanings.items() if m in anchor)
            named = have.get(name)
            if named and by_meaning and name in by_meaning:
                how.append((named, "name and meaning"))
            elif by_meaning:
                how.append((have[by_meaning[0]], "meaning only: the column is renamed"))
            elif named and anchor and name in table_meanings and table_meanings[name] not in anchor:
                how.append((named, "name only: its meaning differs"))
            elif named:
                how.append((named, "name only"))
            else:
                break
        else:
            strength = sum({"name and meaning": 0, "meaning only: the column is renamed": 1}.get(h, 3 if "differs" in h else 2) for _, h in how)
            ranked.append((strength, len(entry.columns), -entry.readers, entry.key, entry, how))
    ranked.sort(key=lambda item: item[:4])
    offset = offset_wanted(args)

    items = []
    for _s, _n, _r, key, entry, how in ranked[offset:][:5]:
        item: dict = {"table": key, "kind": entry.kind, "columns": len(entry.columns), "grain": _grain_text(entry), "readers": entry.readers}
        if entry.role:
            item["role"] = entry.role
        item["matched"] = [f"{label}: {column} ({why})" for label, (column, why) in zip(labels, how)]
        items.append(item)
    answer = Answer("")
    answer.counts = {"tables_with_all_keys": len(ranked), "tables_searched": len(entries)}
    answer.fields["keys"] = labels
    answer.add_list("candidates", items, total=len(ranked), offset=offset, primary=True)
    shown = ", ".join(labels)
    if ranked:
        views = sum(1 for r in ranked if r[4].kind == "view")
        answer.summary = f"{_plural(len(ranked), 'table')} carry {shown}" + (f" ({views} of them {'is a view' if views == 1 else 'are views'})" if views else "") + f"; best: {ranked[0][3].split('.')[-1]}."
        weak = sum(1 for r in ranked if any(h.startswith("name only") for _, h in r[5]))
        if weak:
            answer.unknown(_plural(weak, "table"), "matched at least one key by column name only: the project does not show the column means the same value there")
    else:
        answer.summary = f"No table or view carries all of {shown} (searched {len(entries)})."
        answer.unknown("whether a link exists", "only column names and tracked column meanings are searched; the keys may sit in a table whose columns are not tracked")
    for label, (anchor, why) in zip(labels, anchors):
        if why:
            answer.unknown(label, why)
        elif not anchor:
            answer.unknown(label, "no column of this name has a known meaning, so only column names were matched")
    if ranked:
        answer.next(f"describe_table table={ranked[0][3]} shows its grain and who reads it; a link table has one row per pair of keys.")
    else:
        answer.next("Search each key alone with find_tables column=..., then join the tables that carry them.")
    return answer
