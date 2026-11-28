"""Warn when sqlglot reads a query differently from BigQuery.

Every answer the server gives about SQL (lineage, types, grain, fan-out, the inferred facts) is built on sqlglot's parse.
When sqlglot groups operators differently from BigQuery, drops a ``NOT`` or reads code as a comment, those answers describe
a query nobody wrote. :func:`kumo_sql_prover.parse_check.check_query` reads the text again with an independent tokenizer and
GoogleSQL's precedence table and names the operation where the two readings differ. Before this module it only guarded
proofs; here it also warns ``check_query`` (a ``parse`` finding) and ``describe_table`` (``parse_warning``).

Two readings are compared: the independent one, and sqlglot's reading of its own printed SQL (a round trip). Answers that
print SQL and read it again, such as the fan-out check of a subquery, need the second.

``unchecked`` means the independent reader does not know a construct in the query (it does not read every BigQuery construct).
It is never a pass. The check is a static text check and never reaches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .mcp_answers import Answer, clip

if TYPE_CHECKING:
    from .mcp_session import Session

FIX = "Add parentheses around the operation it names, so sqlglot and BigQuery read it the same way."
#: ``check_query``'s ``unknown`` entry for a draft the check could not compare (kept short: unknowns are the last thing cut)
GAP = "parse check (sqlglot against BigQuery)"


@dataclass(frozen=True)
class Reading:
    """``status`` is ``agree``, ``disagree`` or ``unchecked``. ``reason`` names the first disagreement, ``note`` why it was unchecked."""

    status: str
    reason: str = ""
    #: how many more disagreements the check found
    more: int = 0
    note: str = ""

    @property
    def misread(self) -> bool:
        return self.status == "disagree"


def read(sql: str) -> Reading:
    """Compare sqlglot's reading of ``sql`` with BigQuery's (``kumo_sql_prover.parse_check``); never raises."""

    from kumo_sql_prover.parse_check import check_query, round_trip

    try:
        checked = check_query(sql, "bigquery")
    except Exception as exc:  # noqa: BLE001 - a failing check is "unchecked", never a pass or a warning
        return Reading("unchecked", note=f"the parse check stopped ({type(exc).__name__})")
    if checked.disagrees:
        return Reading("disagree", checked.reasons[0], len(checked.reasons) - 1)
    try:
        regrouped = round_trip(sql, "bigquery")
    except Exception:  # noqa: BLE001
        regrouped = None
    if regrouped:
        return Reading("disagree", regrouped)
    if checked.status == "unchecked":
        return Reading("unchecked", note=checked.note)
    return Reading("agree")


def model_reading(session: "Session", key: str) -> Reading | None:
    """The reading of model ``key``'s SQL, computed once per session; ``None`` for a table without SQL."""

    model = session.pipeline().models.get(key)
    if model is None or not model.sql.strip():
        return None
    return session._once(f"parse_check:{key}", lambda: read(model.sql))


def _reason(found: Reading) -> str:
    return found.reason + (f" (and {found.more} more)" if found.more else "")


def describe(answer: Answer, session: "Session", key: str) -> bool:
    """``describe_table``'s ``parse_warning`` for model ``key``; ``True`` when sqlglot misreads its SQL."""

    found = model_reading(session, key)
    if found is None or not found.misread:
        return False
    model = session.pipeline().models[key]
    templated = f" Its {len(model.masked_expressions)} template expressions were read as placeholders." if model.masked_expressions else ""
    answer.fields["parse_warning"] = clip(
        f"sqlglot reads this model's SQL differently from BigQuery: {_reason(found)}. The grain, column meanings, lineage, "
        f"types and facts here are built on sqlglot's reading and may be wrong. {FIX}{templated}", 420)
    return True


def check_draft(sql: str) -> tuple[list[dict], list[tuple[str, str]], bool]:
    """``check_query``'s parse check: ``(findings, gaps, ran)``. A misread is a ``parse`` finding; an unchecked draft is a gap."""

    found = read(sql)
    if found.misread:
        return [{
            "check": "parse",
            "what": clip(f"sqlglot reads the draft differently from BigQuery: {_reason(found)}. The other checks read sqlglot's version, "
                         "so their findings may be wrong or missing", 300),
            "fix": FIX,
        }], [], True
    if found.status == "unchecked":
        return [], [(GAP, clip(f"its reader does not know a construct here ({found.note})", 110))], False
    return [], [], True
