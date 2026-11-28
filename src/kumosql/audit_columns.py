"""Propose audit columns (load timestamps) from two snapshots or comparisons; never apply them.

A column such as ``_loaded_at``, ``ingested_at`` or ``etl_updated_at`` is rewritten every time a
table is rebuilt, so every comparison of two builds reports it as different even when the data is
the same. KumoSQL's comparisons take such columns by name: ``ignore_columns`` in
:func:`kumosql.fingerprint.plan_output_comparison` (``--ignore-column`` on ``compare-outputs``) and
``--ignore`` on ``reconcile``. This module proposes the names. It never adds them to a comparison:
every proposal says ``"applied": false`` and leaves the next run to the person who reads it.

The rule is ported from KingyoOrchestrator's fingerprint audit heuristic. Between two snapshots
of the same rows, a timestamp-like column is proposed when it changed on every row while every
other column stayed equal. One pair of snapshots gives a ``low`` confidence proposal; ``high``
needs two consecutive pairs (three snapshots, or two comparison runs) that both propose it
(:func:`confirm`).

How much the evidence shows depends on where it comes from:

* ``rows`` (:func:`propose_from_rows`): two snapshots keyed by row. The rule is checked row by row.
* ``checksums`` (:func:`propose_from_comparison`): a ``compare-outputs`` comparison, or two
  fingerprint snapshots joined by :func:`kumosql.fingerprint.join_snapshots`. Only per-column
  checksums are known, so "changed on every row" cannot be checked: the column's checksum changed,
  the row counts are equal and every other column's checksum matched.
* ``counts`` (:func:`propose_from_reconcile`): a ``reconcile`` run. The column differs on nearly
  every key on both sides (``share``, 95% by default), and the other compared columns' differences,
  added up, touch at most the remaining 5% of those keys.

"Timestamp-like" is the column's type when it is known (``TIMESTAMP``, ``DATETIME``, or a ``DATE``
whose name says it is a load date) and its name otherwise (``*_at``, ``*_ts``, ``*_time``,
``loaded``...). A column of any other known type is never proposed.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

__all__ = [
    "AuditCandidate", "DEFAULT_SHARE", "confirm", "propose_from_comparison", "propose_from_reconcile",
    "propose_from_rows", "propose_from_snapshots", "rows_by_key", "suggestion", "timestamp_like",
]

Rows = Mapping[Hashable, Mapping[str, object]]

DEFAULT_SHARE = 0.95
MIN_ROWS = 2
ROW = "*"  # the whole-row checksum's column name in fingerprint results (kumosql.fingerprint.ROW)

_TIME_NAME = re.compile(r"(^|_)(at|ts|time|timestamp|datetime|dttm)$|^(created|updated|modified|loaded|ingested|inserted|refreshed|synced)$")
_LOAD_WORD = re.compile(r"(^_)|(^|_)(etl|elt|dw|dwh|load|loaded|ingest|ingested|ingestion|insert|inserted|refresh|refreshed|"
                        r"processed|sync|synced|batch|audit|extracted|sdc|dbt|airbyte|fivetran)(_|$)")
_TIME_TYPES = ("TIMESTAMP", "DATETIME")
_SURFACES = ("python", "compare-outputs", "reconcile")


def timestamp_like(name: str, type_: str = "", values: Iterable[object] = ()) -> bool:
    """Whether a column can be a load timestamp: by its type when known, else its values, else its name."""

    kind = (type_ or "").strip().upper()
    if kind:
        if kind.startswith(_TIME_TYPES):
            return True
        return kind == "DATE" and bool(_LOAD_WORD.search(name.lower()))
    seen = [value for value in values if value is not None]
    if seen:
        return all(isinstance(value, (dt.datetime, dt.date)) for value in seen)
    return bool(_TIME_NAME.search(name.lower()))


def suggestion(columns: Sequence[str], surface: str = "python") -> str:
    """How to skip ``columns`` on one surface. Text for a person to apply; nothing calls it."""

    if surface not in _SURFACES:
        raise ValueError(f"surface must be one of {', '.join(_SURFACES)}")
    if surface == "compare-outputs":
        return " ".join(f"--ignore-column {column}" for column in columns)
    if surface == "reconcile":
        return "--ignore " + ",".join(columns)
    return "ignore_columns=[" + ", ".join(f'"{column}"' for column in columns) + "]"


@dataclass(frozen=True)
class AuditCandidate:
    """One proposed audit column. ``applied`` is always False: a proposal changes no comparison."""

    column: str
    confidence: str  # "low": one pair of snapshots; "high": two consecutive pairs agree
    evidence: str  # "rows", "checksums" or "counts"
    reason: str
    pairs: int = 1
    model: str | None = None

    @property
    def applied(self) -> bool:
        return False

    def to_json(self, surface: str = "python") -> dict:
        row = {
            "column": self.column,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "pairs": self.pairs,
            "reason": self.reason,
            "suggested": suggestion([self.column], surface),
            "applied": False,
        }
        if self.model is not None:
            row = {"model": self.model, **row}
        return row


def rows_by_key(rows: Iterable[Mapping[str, object]], keys: Sequence[str]) -> dict[tuple, dict[str, object]]:
    """``{key tuple: row}`` for :func:`propose_from_rows`; a repeated key raises ``ValueError``."""

    out: dict[tuple, dict[str, object]] = {}
    for row in rows:
        key = tuple(row[name] for name in keys)
        if key in out:
            raise ValueError(f"key {key!r} repeats: snapshots must have one row per key")
        out[key] = {name: value for name, value in row.items() if name not in keys}
    return out


def propose_from_rows(previous: Rows, current: Rows, column_types: Mapping[str, str] | None = None, *,
                      min_rows: int = MIN_ROWS) -> tuple[AuditCandidate, ...]:
    """Columns that changed on every row between two snapshots while every other column stayed equal.

    ``previous`` and ``current`` map a row key to the row (:func:`rows_by_key` builds them). Nothing is
    proposed unless both snapshots have the same keys, at least ``min_rows`` of them, and the same
    columns; a column with a NULL in either snapshot, or not timestamp-like, is never proposed; and if any
    other column changed on any row, nothing is proposed, because the differences are then not only audit
    noise. Every proposal has ``low`` confidence: it rests on one pair (see :func:`confirm`).
    """

    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        raise ValueError("snapshots must map a row key to the row")
    if set(previous) != set(current) or len(previous) < min_rows:
        return ()
    keys = list(previous)
    columns = set(previous[keys[0]])
    for rows in (previous, current):
        if any(not isinstance(row, Mapping) or set(row) != columns for row in rows.values()):
            return ()
    types = {name.casefold(): kind for name, kind in (column_types or {}).items()}
    changed_everywhere, changed_somewhere = [], []
    for column in sorted(columns):
        flags = [previous[key][column] != current[key][column] for key in keys]
        if all(flags):
            changed_everywhere.append(column)
        elif any(flags):
            changed_somewhere.append(column)
    if changed_somewhere or not changed_everywhere:
        return ()
    found = []
    for column in changed_everywhere:
        values = [rows[key][column] for rows in (previous, current) for key in keys]
        if any(value is None for value in values) or not timestamp_like(column, types.get(column.casefold(), ""), values):
            return ()  # a non-timestamp column changed on every row: this is a real difference, not a load time
        found.append(AuditCandidate(column, "low", "rows", f"changed on every one of {len(keys):,} rows while every "
                                    "other column stayed equal (one pair of snapshots)"))
    return tuple(found)


def propose_from_snapshots(snapshots: Sequence[Rows], column_types: Mapping[str, str] | None = None, *,
                           min_rows: int = MIN_ROWS) -> tuple[AuditCandidate, ...]:
    """:func:`propose_from_rows` on the last pair of a series of snapshots, confirmed by the pair before it.

    With three or more snapshots the last two pairs share a snapshot, so they are consecutive by
    construction, and a column both propose is ``high`` confidence.
    """

    if len(snapshots) < 2:
        return ()
    latest = propose_from_rows(snapshots[-2], snapshots[-1], column_types, min_rows=min_rows)
    if len(snapshots) == 2:
        return latest
    earlier = propose_from_rows(snapshots[-3], snapshots[-2], column_types, min_rows=min_rows)
    return confirm(earlier, latest)


def _confirmed(earlier: Iterable[AuditCandidate], latest: Iterable[AuditCandidate]) -> tuple[AuditCandidate, ...]:
    before = {candidate.column.casefold() for candidate in earlier}
    out = []
    for candidate in latest:
        if candidate.column.casefold() in before:
            out.append(replace(candidate, confidence="high", pairs=candidate.pairs + 1,
                               reason=candidate.reason + "; the earlier pair proposed it too"))
        else:
            out.append(candidate)
    return tuple(out)


def confirm(earlier, latest):
    """``latest`` proposals, ``high`` confidence where ``earlier`` (the previous pair) proposed the same column.

    Takes two tuples of candidates, or two ``{model: candidates}`` mappings as
    :func:`propose_from_comparison` returns them. A proposal only the latest pair makes stays ``low``.
    """

    if isinstance(latest, Mapping):
        earlier = earlier if isinstance(earlier, Mapping) else {}
        return {model: _confirmed(earlier.get(model, ()), found) for model, found in latest.items()}
    return _confirmed(earlier or (), latest)


def _int(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _true(value) -> bool:
    return value.strip().lower() == "true" if isinstance(value, str) else bool(value)


def propose_from_comparison(rows: Iterable[Mapping], *, min_rows: int = MIN_ROWS) -> dict[str, tuple[AuditCandidate, ...]]:
    """Proposals per model from the rows of a ``compare-outputs`` comparison (or joined fingerprint snapshots).

    A model qualifies when both sides have the same number of rows (at least ``min_rows``), at least one
    shared column's checksum differs, and every column whose checksum differs is timestamp-like by name
    (the results carry no types). A model with any other differing column gets no proposal. Checksums
    cannot show that a column changed on every row, so the evidence is ``checksums`` and the confidence
    ``low`` until a second comparison agrees (:func:`confirm`).
    """

    by_model: dict[str, list[Mapping]] = {}
    for row in rows:
        by_model.setdefault(str(row["model"]), []).append(row)
    out: dict[str, tuple[AuditCandidate, ...]] = {}
    for model, model_rows in sorted(by_model.items()):
        whole = next((row for row in model_rows if row["column_name"] == ROW), None)
        if whole is None or whole["before_checksum"] is None or whole["after_checksum"] is None:
            continue
        before_rows, after_rows = _int(whole["before_rows"]), _int(whole["after_rows"])
        if before_rows is None or before_rows != after_rows or before_rows < min_rows:
            continue
        shared = [row for row in model_rows if row["column_name"] != ROW
                  and row["before_checksum"] is not None and row["after_checksum"] is not None]
        differing = [str(row["column_name"]) for row in shared if not _true(row["matches"])]
        if not differing or not all(timestamp_like(column) for column in differing):
            continue
        out[model] = tuple(
            AuditCandidate(column, "low", "checksums",
                           f"its checksum changed while every other column's matched, on {before_rows:,} rows on each "
                           "side (one comparison; checksums cannot show that every row changed)", model=model)
            for column in differing
        )
    return out


def propose_from_reconcile(compared: Sequence[tuple[str, str]], column_mismatches: Mapping[str, int], keys_both: int, *,
                           share: float = DEFAULT_SHARE, min_rows: int = MIN_ROWS) -> list[dict]:
    """``--ignore`` proposals from one reconcile run, as JSON rows for its result (never applied).

    ``compared`` lists each compared column's left name and type, ``column_mismatches`` how many keys on
    both sides differ in it, ``keys_both`` how many keys are on both sides. A timestamp-like column is
    proposed when it differs on at least ``share`` of those keys and every other compared column's
    differences, added up, touch at most ``1 - share`` of them (so at least ``share`` of the keys
    differ in nothing else). The confidence is ``low``: one comparison.
    """

    if not 0.5 <= share <= 1:
        raise ValueError("share must be between 0.5 and 1")
    if keys_both < min_rows:
        return []
    counts = {name: int(column_mismatches.get(name, 0) or 0) for name, _ in compared}
    candidates = [name for name, kind in compared
                  if counts[name] >= share * keys_both and timestamp_like(name, kind)]
    if not candidates:
        return []
    others = sum(n for name, n in counts.items() if name not in candidates)
    if others > (1 - share) * keys_both:
        return []
    rows = []
    for name in candidates:
        reason = (f"differs on {counts[name]:,} of the {keys_both:,} keys on both sides while the other compared columns "
                  f"differ on {others:,} at most (one comparison). A known-difference rule would explain whole rows, "
                  "hiding any other difference on them, so --ignore is the narrower fix")
        rows.append(AuditCandidate(name, "low", "counts", reason).to_json("reconcile"))
    return rows
