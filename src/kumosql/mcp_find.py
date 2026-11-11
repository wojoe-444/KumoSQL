"""``find_tables``: which table should I use?

A search over what the loaded project knows about each table: its name, the names and meanings of its columns, its tags,
its kind and role, and, when they exist, its saved data profile and the saved BigQuery catalog entry. The ranking is plain
word matching with fixed weights, so the same question always gives the same answer; no language model runs and nothing
reaches BigQuery. An answer names a handful of candidates (five by default), why each one matched, and what about it is
unknown, because a table whose meanings are not tracked can only be matched by its names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_session import Session
from .mcp_tools import short_meaning

CANDIDATES = 5
_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOP = frozenset(
    "a an and any are as at be by can data do find for from get give has have how i in into is it its me my need needs of "
    "on one or per show some table tables that the their this to use want what when where which who with".split()
)
#: structure words of a column meaning (``agg:sum(col:orders.amount)``), not content
_STRUCTURE = frozenset("col agg expr const window union case when then else end null and or not sum count min max avg cast if coalesce".split())
KINDS = ("table", "view", "incremental", "source", "fact", "dimension", "bridge")

#: what a match is worth, by where the word was found (a word counts once, at its best place)
WEIGHTS = {"name": 4.0, "column": 2.0, "tag": 2.0, "dataset": 1.0, "meaning": 1.0, "saved": 1.0, "catalog": 1.0}
PREFIX = 0.5  # a word that only starts the other (cust / customer) counts for half
LABELS = {"name": "name", "column": "columns", "tag": "tag", "dataset": "dataset", "meaning": "meaning of", "saved": "saved profile columns", "catalog": "saved catalog"}


def words(text: object) -> list[str]:
    """The lower-case, singular content words of ``text``; underscores, dots and camelCase split words."""

    found = []
    for raw in _WORD.findall(_CAMEL.sub(" ", str(text or ""))):
        word = _singular(raw.lower())
        if word and word not in _STOP:
            found.append(word)
    return found


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


@dataclass
class Entry:
    key: str
    kind: str
    role: str | None
    grain: tuple[str, ...] | str | None
    grain_reason: str | None
    readers: int
    unknown_meanings: int
    columns: tuple[str, ...]
    #: place -> {word: original text that gave it}
    found: dict[str, dict[str, str]] = field(default_factory=dict)


def _add(entry: Entry, place: str, text: object) -> None:
    """Remember the words of ``text`` under ``place``; a name, column or tag is shown whole, other places show the word."""

    for word in words(text):
        entry.found.setdefault(place, {}).setdefault(word, str(text) if place in ("name", "column", "tag") else word)


def build_index(session: Session) -> list[Entry]:
    """One entry per model and declared source of the project, with the words each place gives it."""

    pipeline = session.pipeline()
    profiles, roles, downstream = session.profiles(), session.roles(), session.downstream()
    catalog = _saved_catalog()
    entries = []
    for key in (*pipeline.models, *pipeline.sources):
        model = pipeline.models.get(key)
        if model is not None and (model.kind == "assertion" or (model.kind == "operations" and not model.has_output)):
            continue
        profile = profiles.get(key)
        outputs = list(pipeline.output_columns(key)) if model is not None else list(pipeline.source_schema.get(key, {}))
        grain: tuple[str, ...] | str | None = None
        grain_reason = None
        if profile is not None:
            grain = profile.grain.keys if profile.grain.known and profile.grain.keys else ("single row" if profile.grain.known else None)
            grain_reason = None if profile.grain.known else profile.grain.reason
        unknown = sum(1 for a in profile.attributes if a.status != "known") if profile is not None else 0
        entry = Entry(key, model.kind if model is not None else "source", None, grain, grain_reason, len(downstream.get(key, ())), unknown, tuple(outputs))
        role = roles.get(key)
        if role is not None and role.role != "unknown":
            entry.role = role.role
        parts = key.split(".")
        _add(entry, "name", parts[-1])
        _add(entry, "dataset", ".".join(parts[:-1]))
        for column in outputs:
            _add(entry, "column", column)
        for tag in (model.tags if model is not None else ()):
            _add(entry, "tag", tag)
        if profile is not None:
            for attribute in profile.attributes:
                if attribute.status == "known":
                    text = " ".join(w for w in words(_meaning_text(attribute.meaning)) if w not in _STRUCTURE)
                    _add(entry, "meaning", text)
        saved = session.saved_profile(key)
        if saved is not None:
            for column in _saved_columns(session, saved):
                _add(entry, "saved", column)
        for text in catalog.get(key.casefold(), ()):
            _add(entry, "catalog", text)
        entries.append(entry)
    return entries


def _meaning_text(meaning: object) -> str:
    return short_meaning(meaning).replace("col:", " ").replace("agg:", " ").replace("expr:", " ")


def _saved_columns(session: Session, row: dict) -> list[str]:
    from . import data_profile_store as store

    try:
        profile = store.load(row["name"], session.profiles_directory)
    except Exception:  # noqa: BLE001 - an unreadable saved profile is just not searched
        return []
    return [column.name for column in profile.columns]


def _saved_catalog() -> dict[str, list[str]]:
    """Names and descriptions of the tables in the saved BigQuery catalog (read from disk; nothing is fetched)."""

    try:
        from . import bigquery_catalog

        saved = bigquery_catalog.saved_tables()
    except Exception:  # noqa: BLE001
        return {}
    found: dict[str, list[str]] = {}
    for project, dataset, table, data in saved:
        texts = [str(data.get("description") or "")]
        for column in data.get("schema") or []:
            if isinstance(column, dict):
                texts += [str(column.get("name") or ""), str(column.get("description") or "")]
        found[f"{project}.{dataset}.{table}".casefold()] = [t for t in texts if t]
    return found


def _match(word: str, available: dict[str, str]) -> tuple[float, str | None]:
    """0 when ``word`` is not among ``available``; 1 for the same word; PREFIX for a shared start of four or more letters."""

    if word in available:
        return 1.0, available[word]
    if len(word) >= 4:
        for other, text in sorted(available.items()):
            if len(other) >= 4 and (other.startswith(word) or word.startswith(other)):
                return PREFIX, text
    return 0.0, None


def score(entry: Entry, need: list[str], column: str | None) -> tuple[float, dict[str, list[str]], int]:
    """Points, the words found per place, and how many of the ``need`` words matched anywhere."""

    points = 0.0
    hits: dict[str, list[str]] = {}
    matched = 0
    for word in need:
        best = (0.0, "", "")
        for place, weight in WEIGHTS.items():
            strength, text = _match(word, entry.found.get(place, {}))
            if strength and strength * weight > best[0]:
                best = (strength * weight, place, text or "")
        if best[0]:
            points += best[0]
            matched += 1
            shown = hits.setdefault(best[1], [])
            if best[2] not in shown:
                shown.append(best[2])
    if column:
        wanted = column.strip().strip("`").casefold()
        names = {name.casefold(): name for name in entry.columns}
        if wanted in names:
            points += 6.0
            hits.setdefault("column", [])
            if names[wanted] not in hits["column"]:
                hits["column"].append(names[wanted])
        else:
            close = [name for fold, name in sorted(names.items()) if len(wanted) >= 3 and (wanted in fold or fold in wanted)]
            if close:
                points += 2.0
                hits.setdefault("column", []).extend(n for n in close[:2] if n not in hits.get("column", []))
    return points, hits, matched


def _why(hits: dict[str, list[str]]) -> str:
    parts = []
    for place in WEIGHTS:
        if place in hits:
            names = ", ".join(clip(h, 40) for h in hits[place][:3])
            parts.append(f"{LABELS[place]} {names}")
    return clip("; ".join(parts), 150)


def _grain_text(entry: Entry) -> str:
    if isinstance(entry.grain, tuple):
        return "one row per " + ", ".join(entry.grain)
    if entry.grain == "single row":
        return "a single row"
    return "grain unknown"


@tool(
    "find_tables",
    "Find the table to use",
    "Which table should I use? Describe what you need in a few words (and a column name or a kind of table if you know one). "
    "Returns a handful of candidate tables of the project, why each one matched (its name, columns, meanings, tags, saved "
    "profile), its grain and readers, and what is unknown about it. A fixed word-matching rule, no language model, no BigQuery.",
    {
        "need": {"type": "string", "description": "What you are looking for, in plain words, such as 'daily revenue by customer'."},
        "column": {"type": "string", "description": "A column the table should have (optional); an exact name counts most."},
        "kind": {"type": "string", "enum": list(KINDS), "description": "Only tables of this kind or role (optional)."},
        "has_columns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Instead of need: two to four keys the table must carry together (a link table), such as [\"customer_id\", \"order_id\"]. "
            "Matches column names and column meanings, so a renamed key counts; say which matched by name only. Views count. table.column pins a key.",
        },
    },
    required=(),
)
def find_tables(session: Session, args: dict) -> Answer:
    if args.get("has_columns") is not None:
        from .mcp_expression import link_tables

        return link_tables(session, args)
    if not args.get("need"):
        raise ToolError("missing argument 'need'", ["Pass need: what you are looking for, in plain words; or has_columns=[A, B] for a table that carries both keys."])
    need_text = str(args["need"])
    if len(need_text) > 500:
        raise ToolError("the need is too long (500 characters at most)", ["Name the main things you need in a few words."])
    column = args.get("column")
    need = list(dict.fromkeys(words(need_text)))
    if not need and not column:
        raise ToolError("the need has no searchable words", ["Describe it with names of things, such as 'orders by customer', or pass column."])
    kind = args.get("kind")

    entries = session._once("find_tables_index", lambda: build_index(session))
    ranked = []
    for entry in entries:
        if kind and kind not in (entry.kind, entry.role):
            continue
        points, hits, matched = score(entry, need, column)
        if points > 0:
            known = 0.1 if isinstance(entry.grain, tuple) or entry.grain == "single row" else 0.0
            ranked.append((-(points + known), -entry.readers, entry.key, points, hits, matched, entry))
    ranked.sort(key=lambda item: item[:3])
    offset = offset_wanted(args)

    items = []
    for _, _, key, points, hits, matched, entry in ranked[offset:][:CANDIDATES]:
        item: dict = {"table": key, "kind": entry.kind, "score": round(points, 1), "why": _why(hits), "grain": _grain_text(entry), "readers": entry.readers}
        if len(need) > 1:
            item["words"] = f"{matched} of {len(need)}"
        if entry.role:
            item["role"] = entry.role
        gaps = []
        if entry.grain is None:
            gaps.append("grain unknown")
        if entry.unknown_meanings:
            gaps.append(f"{entry.unknown_meanings} column meaning{'s' if entry.unknown_meanings != 1 else ''} unknown")
        if gaps:
            item["unknown"] = "; ".join(gaps)
        items.append(item)

    searched = len(entries)
    label = clip(need_text, 60)
    answer = Answer("")
    answer.counts = {"matches": len(ranked), "tables_searched": searched}
    answer.fields["searched_for"] = " ".join(need) + (f" + column {clip(column, 40)}" if column else "")
    answer.add_list("candidates", items, total=len(ranked), offset=offset, primary=True)
    if not ranked:
        answer.summary = f"No table of the project matches {label!r} (searched {searched}{f', kind {kind}' if kind else ''})."
        answer.unknown("whether a table exists for this", "only names, columns, tags and tracked meanings are searched; the words may be spelled differently in the project")
        answer.next("Try other words, or a column name with column=...; find_existing sql=... compares a proposed query with every table.")
        return answer
    top = ranked[0]
    answer.summary = f"{len(ranked)} table{'s' if len(ranked) != 1 else ''} match {label!r}; best: {top[2].split('.')[-1]} ({_why(top[4]) or 'matched'})."
    blind = sum(1 for e in entries if e.unknown_meanings or e.grain is None)
    if blind:
        answer.unknown(f"{blind} of {searched} tables", "their grain or some column meanings are not tracked, so they can only be matched by name; a better table may be among them")
    if not any(isinstance(item_[6].grain, tuple) or item_[6].grain == "single row" for item_ in ranked[offset:][:CANDIDATES]):
        answer.unknown("which columns identify a row in these candidates", "their grain is unknown")
    answer.next(f"describe_table table={top[2]} shows its grain, columns and who reads it.")
    if len(ranked) > 1:
        answer.next("Compare candidates with describe_table before joining them; ranking is by word match, not by quality or freshness.")
    return answer
