"""Find existing tables that already provide what a query (or model) computes.

Copied SQL is found by text similarity. This module finds the same *meaning*: it
compares the ``TableProfile`` of the target (a proposed query, or an existing
model) with the profile of every other model, so renamed columns, re-cased
names, views and chains of CTEs do not hide a match. Names, formatting and
shared source columns never produce a match on their own.

Match kinds, from strongest to weakest:

* ``same_meaning`` (``high``): every attribute of the target has the same
  meaning as an attribute of the candidate, the grain keys mean the same, and the
  row scope is identical and comparable.
* ``contains`` (``medium``): same attributes and grain, and the candidate's
  filters are a strict subset of the target's, so the target can filter it.
* ``partial`` (``low``): some attributes match at the same grain, or all do but
  the row scope differs or cannot be compared.
* ``unknown``: the comparison could not be made (grain or lineage unknown on
  either side). The reason is listed; it is never a match.

Every match lists its checks (``lineage``, ``grain``, ``row_scope``), each
``matched``, ``differs`` or ``unknown``. A different grain is not a match. The
result always states how many tables were compared and how many were skipped and
why; "no match" is never reported without those counts. Reports describe
behavior only (no query text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .pipeline import Pipeline
from .scopes import Scope
from .table_profile import COLUMN_MEANING, TableProfile, profile_pipeline, profile_query
from .table_roles import TableRole, infer_roles

__all__ = ["Check", "Match", "MatchRole", "OverlapResult", "find_overlaps"]

_KIND_ORDER = {"same_meaning": 0, "contains": 1, "partial": 2, "unknown": 3}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_COLUMN_MEANING = re.compile(COLUMN_MEANING)


@dataclass(frozen=True)
class Check:
    """One comparison: ``kind`` is lineage, grain or row_scope."""

    kind: str
    outcome: str  # "matched", "differs" or "unknown"
    detail: str = ""

    def to_json(self) -> dict:
        return {"kind": self.kind, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class MatchRole:
    """The candidate's inferred role (see ``infer_roles``)."""

    role: str
    confidence: str

    def to_json(self) -> dict:
        return {"role": self.role, "confidence": self.confidence}


@dataclass(frozen=True)
class Match:
    table: str
    kind: str  # "same_meaning", "contains", "partial" or "unknown"
    confidence: str  # "high", "medium" or "low"
    #: matched column pairs: (target column, candidate column)
    attributes: tuple[tuple[str, str], ...] = ()
    checks: tuple[Check, ...] = ()
    role: MatchRole | None = None
    reason: str = ""

    def to_json(self) -> dict:
        return {
            "table": self.table,
            "kind": self.kind,
            "confidence": self.confidence,
            "attributes": [{"target": t, "candidate": c} for t, c in self.attributes],
            "checks": [c.to_json() for c in self.checks],
            "role": self.role.to_json() if self.role else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OverlapResult:
    """Matches plus the coverage that gives them meaning.

    ``compared`` counts candidates that could be decided (matched or ruled out).
    ``skipped`` maps a reason to the number of candidates that could not be
    decided (``outside_scope``, ``incomplete_lineage``, ``grain_unknown``, ...);
    the undecided in-scope ones also appear in ``matches`` as ``unknown``.
    ``candidates_in_scope`` counts candidates inside the scope, so
    ``compared + sum(skipped.values())`` is every candidate considered.
    """

    target: TableProfile
    matches: tuple[Match, ...] = ()
    compared: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict)
    candidates_in_scope: int = 0

    @property
    def summary(self) -> str:
        skipped = sum(self.skipped.values())
        reasons = ", ".join(f"{reason} {count}" for reason, count in sorted(self.skipped.items()))
        text = f"compared {self.compared} of {self.compared + skipped} tables; {skipped} skipped"
        if reasons:
            text += f": {reasons}"
        found = sum(1 for m in self.matches if m.kind != "unknown")
        if found:
            text += f". {found} existing table{'s' if found != 1 else ''} match"
        else:
            text += ". No match among the compared tables"
        return text

    def to_json(self) -> dict:
        return {
            "summary": self.summary,
            "target": self.target.to_json(),
            "matches": [m.to_json() for m in self.matches],
            "compared": self.compared,
            "skipped": dict(sorted(self.skipped.items())),
            "candidates_in_scope": self.candidates_in_scope,
        }


# ------------------------------------------------------------------ public API


def find_overlaps(
    pipeline: Pipeline,
    sql: str | None = None,
    *,
    model: str | None = None,
    scope: Scope | None = None,
    roles: Mapping[str, TableRole] | None = None,
    declared_grain: Mapping[str, Sequence[str]] | None = None,
    target_profile: TableProfile | None = None,
) -> OverlapResult:
    """Existing tables that already provide what ``sql`` (or ``model``) computes.

    Give exactly one of ``sql`` (a proposed query) and ``model`` (a model key in
    the pipeline). ``scope`` limits the search to candidates matching a saved
    ``Scope`` (record fields: ``project``, ``dataset``, ``table``, ``model``);
    the others are skipped and counted as ``outside_scope``. ``roles`` are
    attached to matches; when omitted they are inferred. A model is never
    compared with itself. ``target_profile`` is the profile of ``sql`` when the caller already has it
    (profiling a query builds a scratch pipeline, seconds on a large project).
    """

    if (sql is None) == (model is None):
        raise ValueError("give exactly one of sql and model")
    profiles = profile_pipeline(pipeline, declared_grain=declared_grain)
    if sql is not None:
        target_key = None
        target = target_profile or profile_query(pipeline, sql, declared_grain=declared_grain)
    else:
        target_key = _resolve(pipeline, model)
        if target_key not in profiles:
            raise ValueError("the model is not in the pipeline")
        target = profiles[target_key]
    if roles is None:
        try:
            roles = infer_roles(pipeline)
        except Exception:  # noqa: BLE001
            roles = {}

    matches: list[Match] = []
    skipped: dict[str, int] = {}
    compared = 0
    in_scope = 0
    keep = pipeline.scope_keys(scope, profiles) if scope is not None else None
    for key in sorted(pipeline.models):
        if key == target_key:
            continue
        if keep is not None and key not in keep:
            skipped["outside_scope"] = skipped.get("outside_scope", 0) + 1
            continue
        in_scope += 1
        try:
            match = _compare(target, profiles[key], key)
        except Exception as exc:  # noqa: BLE001 - never raise on odd profiles
            match = Match(key, "unknown", "low", reason=f"compare_error: {type(exc).__name__}")
        if match is None:
            compared += 1
            continue
        role = roles.get(key)
        if role is not None:
            match = _with_role(match, MatchRole(role.role, role.confidence))
        if match.kind == "unknown":
            reason = match.reason.split(":")[0]
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            compared += 1
        matches.append(match)
    matches.sort(key=lambda m: (_KIND_ORDER[m.kind], _CONFIDENCE_ORDER[m.confidence], m.table))
    return OverlapResult(target, tuple(matches), compared, skipped, in_scope)


# ------------------------------------------------------------------- internals


def _resolve(pipeline: Pipeline, model: str | None) -> str:
    text = str(model)
    try:
        return pipeline.resolve(text) or text
    except Exception:  # noqa: BLE001
        return text


def _with_role(match: Match, role: MatchRole) -> Match:
    return Match(match.table, match.kind, match.confidence, match.attributes, match.checks, role, match.reason)


def _unknown(table: str, reason: str, pairs: tuple[tuple[str, str], ...] = (), *, lineage: str = "unknown") -> Match:
    """A comparison that could not be made; the reason starts with its category."""

    grain_unknown = reason.startswith("grain_unknown")
    checks = (
        Check("lineage", lineage, reason if lineage == "unknown" else ""),
        Check("grain", "unknown", reason if grain_unknown else ""),
        Check("row_scope", "unknown"),
    )
    return Match(table, "unknown", "low", pairs, checks, None, reason)


def _key_meanings(profile: TableProfile) -> frozenset[str] | None:
    """The meanings of the grain key columns; None when one is unknown."""

    meanings = []
    for key in profile.grain.keys:
        attr = profile.attribute(key)
        if attr is None or attr.status != "known" or attr.meaning is None:
            return None
        meanings.append(attr.meaning)
    return frozenset(meanings)


def _compare(target: TableProfile, candidate: TableProfile, key: str) -> Match | None:
    """The match of ``candidate`` for ``target``; None when it is ruled out."""

    if not target.grain.known:
        return _unknown(key, "grain_unknown: the target's grain is unknown")
    if not target.attributes or any(a.status != "known" for a in target.attributes):
        return _unknown(key, "incomplete_lineage: the target's lineage is incomplete")
    t_keys = _key_meanings(target)
    if t_keys is None:
        return _unknown(key, "grain_unknown: a target grain column has unknown meaning")

    by_meaning: dict[str, str] = {}
    for attr in sorted(candidate.attributes, key=lambda a: a.column.lower()):
        if attr.status == "known" and attr.meaning is not None:
            by_meaning.setdefault(attr.meaning, attr.column)
    pairs = tuple((a.column, by_meaning[a.meaning]) for a in target.attributes if a.meaning in by_meaning)
    hidden = not candidate.attributes or any(a.status != "known" for a in candidate.attributes)
    all_attrs = len(pairs) == len(target.attributes)
    if not pairs:
        if hidden:
            return _unknown(key, "incomplete_lineage: the table's lineage is incomplete")
        return None
    if hidden and not all_attrs:
        return _unknown(key, "incomplete_lineage: the table's lineage is incomplete", pairs)
    if not candidate.grain.known:
        return _unknown(key, "grain_unknown: the table's grain is unknown", pairs, lineage="matched")
    c_keys = _key_meanings(candidate)
    if c_keys is None:
        return _unknown(key, "grain_unknown: a grain column has unknown meaning", pairs, lineage="matched")
    if c_keys != t_keys:
        return None  # a different grain is not the same table, however many attributes it shares

    lineage = Check("lineage", "matched", f"{len(pairs)} of {len(target.attributes)} attributes have the same meaning")
    grain = Check("grain", "matched", "grain keys have the same meaning")
    scope, contained = _scope_check(target, candidate, by_meaning)
    checks = (lineage, grain, scope)
    if all_attrs and scope.outcome == "matched":
        return Match(key, "same_meaning", "high", pairs, checks, None, "same attributes, grain and row scope")
    if all_attrs and contained:
        return Match(
            key, "contains", "medium", pairs, checks, None,
            "same attributes and grain; the table has a wider row scope that the target can filter",
        )  # fmt: skip
    if all_attrs:
        return Match(key, "partial", "low", pairs, checks, None, "same attributes and grain; row scope differs or cannot be compared")
    return Match(key, "partial", "low", pairs, checks, None, "only some attributes match")


def _scope_check(target: TableProfile, candidate: TableProfile, by_meaning: dict[str, str]) -> tuple[Check, bool]:
    """The row-scope check, and whether the candidate strictly contains the target's rows."""

    t, c = target.row_scope, candidate.row_scope
    if not (t.comparable and c.comparable):
        who = "target" if not t.comparable else "table"
        return Check("row_scope", "unknown", f"the {who}'s row scope cannot be compared"), False
    if t.filters == c.filters:
        return Check("row_scope", "matched", "same filters"), False
    if set(c.filters) < set(t.filters):
        needed = {m for f in set(t.filters) - set(c.filters) for m in _COLUMN_MEANING.findall(f)}
        if needed <= set(by_meaning):
            return Check("row_scope", "differs", "the table's filters are a subset of the target's"), True
        return Check("row_scope", "differs", "the target filters on a column the table does not provide"), False
    return Check("row_scope", "differs", "the filters differ"), False
