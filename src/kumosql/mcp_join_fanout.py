"""How ``check_query`` judges a join to a table whose grain the project's queries do not show.

A warehouse table has no query to read a grain from. In order, the draft's join is judged from

1. a **stated grain** (:mod:`kumosql.source_grain`): a declaration's ``uniqueKey``, an assertion file, Malloy ``primary_key``, a
   docs file, ``--assume-grain``. It is used like any grain and the answer lists where it came from;
2. a **saved measurement** (:mod:`kumosql.join_profile_store`): a saved data profile (single columns) or a saved ``join_profile``
   of exactly these tables and keys. No repeats on the join columns: no finding, and the answer says it assumed the measurement
   still holds. Repeats: a finding with the counts and the date, and ``near-key`` when they are few;
3. otherwise **not decided**, and the reason names ``join_profile`` with the arguments to run.

An observation is a measurement on one day, never a constraint, and says so wherever it is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from . import join_profile_store as joins
from .mcp_answers import clip


@dataclass
class Outcome:
    finding: dict | None = None
    gap: tuple[str, str] | None = None
    assumption: str | None = None
    pointer: str | None = None


def stated_grain(session, key: str | None, spelled: str):
    """The :class:`~kumosql.source_grain.GrainClaim` for a joined table, or ``None``. ``key`` is its project key, ``spelled`` how the draft wrote it."""

    claims = session.grain_claims()
    if key is not None:
        return claims.get(key)
    for claim in claims.values():
        if not claim.in_project and joins.same_table(claim.table, spelled):
            return claim
    return None


def _spelled(target: exp.Table) -> str:
    return ".".join(p.name for p in (target.args.get("catalog"), target.args.get("db"), target.this) if p is not None and p.name)


def _aliases(select: exp.Select) -> dict[str, exp.Table]:
    tables = {}
    source = select.args.get("from_") or select.args.get("from")
    for item in [source.this if source is not None else None, *[j.this for j in select.args.get("joins") or []]]:
        if isinstance(item, exp.Table) and item.name:
            tables[item.alias_or_name.casefold()] = item
    return tables


def _pairs(select: exp.Select, join: exp.Join, alias: str, equalities) -> tuple[exp.Table | None, list[tuple[str, str]]]:
    """The table on the other side and the ``(its column, this table's column)`` pairs a join condition equates, or ``(None, [])``."""

    folded = alias.casefold()
    aliases = _aliases(select)
    pairs, others = [], set()
    for column in join.args.get("using") or []:
        if getattr(column, "name", None):
            pairs.append((column.name, column.name))
    for equal in equalities:
        for mine, other in ((equal.left, equal.right), (equal.right, equal.left)):
            if isinstance(mine, exp.Column) and isinstance(other, exp.Column) and (mine.table or "").casefold() == folded and (other.table or "").casefold() not in ("", folded):
                pairs.append((other.name, mine.name))
                others.add(other.table.casefold())
    if join.args.get("using"):
        source = select.args.get("from_") or select.args.get("from")
        if source is not None and isinstance(source.this, exp.Table):
            others.add(source.this.alias_or_name.casefold())
    left = aliases.get(next(iter(others))) if len(others) == 1 else None
    return left, list(dict.fromkeys(pairs))


def _name(session, table: exp.Table, key: str | None) -> str:
    return key or _spelled(table)


def judge(session, draft, select: exp.Select, join: exp.Join, target: exp.Expression, label: str, aggregate: str | None, reason: str) -> Outcome:
    """The outcome for a join whose target has no grain from the project: ``reason`` is why, if nothing else decides."""

    from .mcp_preflight import _equalities, _joined_columns

    what = f"whether the JOIN to {label} multiplies rows"
    if not isinstance(target, exp.Table) or not target.name or (not target.args.get("db") and target.name.casefold() in {c.alias.casefold() for c in draft.tree.find_all(exp.CTE)}):
        return Outcome(gap=(what, reason))
    has_condition = join.args.get("on") is not None or bool(join.args.get("using"))
    if not has_condition or str(join.args.get("kind") or "").upper() == "CROSS":
        return Outcome(gap=(what, reason))
    spelled = _spelled(target)
    key = draft.tables.get(spelled.casefold())
    names = [n for n in (key, spelled) if n]
    joined = _joined_columns(join, target.alias_or_name)
    left, pairs = _pairs(select, join, target.alias_or_name, _equalities(join.args.get("on")))
    if not joined or not pairs:
        return Outcome(gap=(what, f"{reason}; the join condition is not a column-to-column equality, which join_profile needs"))
    right_columns = {b.casefold() for _, b in pairs}
    verdict = joins.lookup(session.key_observations(), names, joined)
    cols = ", ".join(sorted(joined))
    if verdict.kind == "unique":
        o = verdict.observation
        return Outcome(assumption=clip(f"{label}: no repeats on ({', '.join(o.columns)}) in the saved {_source(o)} of {o.generated_at[:10]} ({o.duplicate_rows} duplicate rows in {o.rows:,}); assumed to hold now", 200))
    if verdict.kind == "repeats":
        o = verdict.observation
        kind = f"is a near-key on ({cols})" if o.near_key else f"repeats on ({cols})"
        said = (f"JOIN to {label} on ({cols}) can match several rows: {label} {kind}, {o.counts()} (saved {_source(o)} of {o.generated_at[:10]})")
        if o.near_key:
            said += "; a near-key, so a few rows repeat, an assumption that the profile still holds"
        if aggregate:
            said += f"; the {aggregate} in this query would count the repeated rows more than once"
        fix = (f"Reduce {label} to one row per ({cols}) first (GROUP BY, or QUALIFY ROW_NUMBER() OVER (PARTITION BY {cols} ORDER BY ...) = 1), "
               "or accept the repeated rows; join_profile measures the join itself.")
        return Outcome(finding={"check": "fan_out", "what": clip(said, 300), "fix": clip(fix, 220)})
    pointer = _call(session, left, key_names=_name(session, target, key), pairs=pairs, left_key=_left_key(draft, left))
    if joined != right_columns:  # the join also pins a column to a constant; a repeat on the equated columns alone may not apply
        again = joins.lookup(session.key_observations(), names, right_columns)
        if again.kind == "repeats":
            o = again.observation
            return Outcome(gap=(what, clip(f"{label} repeats on ({', '.join(sorted(right_columns))}) ({o.counts()}, {_source(o)} of {o.generated_at[:10]}); the extra condition in the join may remove them, which no saved measurement decides", 230)))
    note = ""
    if verdict.kind == "wider":
        o = verdict.observation
        note = f"; measured only on the longer key ({', '.join(o.columns)}) in {_source(o)} of {o.generated_at[:10]}"
    return Outcome(gap=(what, clip(f"{reason}{note}. Run: {pointer}", 300)), pointer=pointer)


def _source(o: joins.KeyObservation) -> str:
    return "join profile" if o.source == "join_profile" else "data profile"


def _left_key(draft, table: exp.Table | None) -> str | None:
    if table is None:
        return None
    return draft.tables.get(_spelled(table).casefold()) or _spelled(table)


def _call(session, left, key_names: str, pairs, left_key: str | None) -> str:
    from .mcp_join_profile import keys_argument

    return f"join_profile left={left_key or '<the table on the other side>'} right={key_names} keys={keys_argument(pairs)}"
