"""What saved profiles say about whether a set of columns identifies a row, and the saved join profiles themselves.

A *key observation* is a measured fact about one table and one set of columns: how many rows it has, how many have a null in
the columns, and how many rows repeat an earlier row's values (``duplicate_rows``). Two kinds of saved file give them:

* a saved data profile (``python -m kumosql profile-table``, or ``profile_table`` with ``run=true``): every top-level
  column has a row count, a non-null count and a distinct count, so each single column is an observation. Only an exact,
  whole-table profile counts: a sample, a row filter or approximate distinct counts say nothing about the table;
* a saved join profile (``join_profile``, see :mod:`kumosql.mcp_join_profile`): one query measured the key columns of both
  tables, so each side is an observation for the columns the join used (several columns at once).

An observation is a measurement on one day, not a constraint. Whoever uses one says so and gives the date and the counts.
A column with no duplicates is *unique in the data profiled*; a column with a few is a *near-key* (at most
:data:`NEAR_KEY_MAX` of the rows). Join profiles are kept in the ``join-profiles`` folder inside the saved-profile folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import data_profile_store as store
from .data_profile import DataProfile, ProfileError

FOLDER = "join-profiles"
VERSION = 1
#: a column that repeats at most this share of the rows is offered as a near-key
NEAR_KEY_MAX = 0.01
MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class KeyObservation:
    table: str
    columns: tuple[str, ...]
    rows: int
    null_rows: int
    duplicate_rows: int
    generated_at: str
    #: ``join_profile`` or ``profile_table``
    source: str
    #: the saved file this was read from (without ``.json``)
    name: str

    @property
    def unique(self) -> bool:
        return self.duplicate_rows == 0

    @property
    def duplicate_rate(self) -> float:
        return self.duplicate_rows / self.rows if self.rows else 0.0

    @property
    def null_rate(self) -> float:
        return self.null_rows / self.rows if self.rows else 0.0

    @property
    def near_key(self) -> bool:
        return 0 < self.duplicate_rate <= NEAR_KEY_MAX

    def counts(self) -> str:
        """``3 duplicate rows in 5,000 (0.06%), 0% null`` (the facts, no verdict)."""

        return f"{self.duplicate_rows:,} duplicate row{'s' if self.duplicate_rows != 1 else ''} in {self.rows:,} ({percent(self.duplicate_rate)}), {percent(self.null_rate)} null"

    def verdict(self) -> str:
        """``unique`` or ``near-key: 3 duplicate rows in ...`` or ``not a key: ...``."""

        if self.unique:
            return f"unique ({self.counts()})"
        return f"{'near-key' if self.near_key else 'not a key'}: {self.counts()}"

    def to_json(self) -> dict:
        return {"table": self.table, "columns": list(self.columns), "rows": self.rows, "null_rows": self.null_rows,
                "duplicate_rows": self.duplicate_rows, "generated_at": self.generated_at, "source": self.source, "name": self.name}


def percent(fraction: float) -> str:
    if fraction <= 0:
        return "0%"
    if fraction < 0.0001:
        return "under 0.01%"
    return f"{fraction:.2%}" if fraction < 0.1 else f"{fraction:.0%}"


# ------------------------------------------------------------- from a data profile


def exact_whole_table(profile: DataProfile) -> bool:
    """Whether ``profile`` counted every row exactly (a sample, a row filter or approximate distinct counts prove nothing)."""

    return profile.sample_percent is None and not profile.row_filter and not profile.approximate_distinct


def observations_from_profile(profile: DataProfile, name: str = "") -> list[KeyObservation]:
    """One observation per top-level column of an exact, whole-table profile (none otherwise)."""

    if not exact_whole_table(profile) or profile.row_count <= 0:
        return []
    found = []
    for column in profile.columns:
        if column.unit != "rows" or column.kind in ("other", "array") or column.distinct is None or any(c in column.name for c in ".[]"):
            continue
        found.append(KeyObservation(profile.table, (column.name,), profile.row_count, column.null_count, max(column.non_null - column.distinct, 0),
                                    profile.generated_at, "profile_table", name))
    return found


def key_candidates(profile: DataProfile, limit: int = 5) -> list[KeyObservation]:
    """The columns of an exact profile that are unique or repeat at most :data:`NEAR_KEY_MAX` of the rows, fewest repeats first."""

    usable = [o for o in observations_from_profile(profile) if o.rows - o.null_rows > 1 and o.duplicate_rate <= NEAR_KEY_MAX and o.null_rows < o.rows]
    return sorted(usable, key=lambda o: (o.duplicate_rate, o.null_rate, o.columns))[:limit]


# ------------------------------------------------------------- join profiles


def _directory(directory: Path | None) -> Path:
    return store._directory(directory) / FOLDER


def join_name(left: str, right: str, keys: Sequence[Sequence[str]]) -> str:
    """A file-safe name for one pair of tables and keys: the same question always lands in the same file."""

    spec = json.dumps([left.casefold(), right.casefold(), [[a.casefold(), b.casefold()] for a, b in keys]])
    stem = f"{store.default_name(left.split('.')[-1])}--{store.default_name(right.split('.')[-1])}"[:90].strip("._-")
    return f"{stem}-{hashlib.sha1(spec.encode()).hexdigest()[:10]}"


def save_join(record: dict, directory: Path | None = None) -> Path:
    """Write a join profile record (replacing the one for the same tables and keys) and return the file."""

    name = join_name(record["left"], record["right"], record["keys"])
    folder = _directory(directory)
    path = folder / f"{store.check_name(name)}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=folder, prefix=".join-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump({"version": VERSION, **record}, out, indent=2)
            out.write("\n")
        os.replace(temp, path)
    except OSError as exc:
        raise ProfileError(f"could not save the join profile: {exc}") from None
    return path


def load_joins(directory: Path | None = None) -> list[dict]:
    """Every readable saved join profile, newest first. Unreadable files are left out."""

    rows = []
    try:
        files = sorted(_directory(directory).glob("*.json"))
    except OSError:
        return []
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("version") == VERSION and isinstance(data.get("sides"), dict):
            data["name"] = path.stem
            rows.append(data)
    return sorted(rows, key=lambda d: (str(d.get("generated_at", "")), d["name"]), reverse=True)


def observations_from_join(record: dict) -> list[KeyObservation]:
    found = []
    try:
        for side, table, columns in (("left", record["left"], [k[0] for k in record["keys"]]), ("right", record["right"], [k[1] for k in record["keys"]])):
            numbers = record["sides"][side]
            found.append(KeyObservation(str(table), tuple(str(c) for c in columns), int(numbers["rows"]), int(numbers["null_key_rows"]),
                                        int(numbers["duplicate_rows"]), str(record["generated_at"]), "join_profile", str(record.get("name", ""))))
    except (KeyError, TypeError, ValueError):
        return []
    return found


# ------------------------------------------------------------- all of them


def signature(directory: Path | None = None) -> tuple:
    """Changes when a saved profile or join profile is added, replaced or removed."""

    entries = []
    for folder in (store._directory(directory), _directory(directory)):
        try:
            entries.append(tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(folder.glob("*.json"))))
        except OSError:
            entries.append(())
    return tuple(entries)


def load_observations(directory: Path | None = None) -> list[KeyObservation]:
    """Every key observation in the saved profiles and join profiles, newest first."""

    found: list[KeyObservation] = []
    for record in load_joins(directory):
        found += observations_from_join(record)
    for row in store.list_profiles(directory):
        try:
            found += observations_from_profile(store.load(row["name"], directory), row["name"])
        except ProfileError:
            continue
    return sorted(found, key=lambda o: (o.generated_at, o.name), reverse=True)


# ------------------------------------------------------------- looking one up


def _parts(name: str) -> list[str]:
    return [p for p in str(name).replace("`", "").casefold().split(".") if p]


def same_table(saved: str, wanted: str) -> bool:
    """Whether two spellings name one table: they agree on every part the shorter one has (at least dataset.table), or both have one part."""

    a, b = _parts(saved), _parts(wanted)
    n = min(len(a), len(b))
    return bool(n) and (n >= 2 or len(a) == len(b)) and a[-n:] == b[-n:]


@dataclass(frozen=True)
class Verdict:
    #: ``unique`` (no repeats on these columns, or on a subset of them), ``repeats`` (measured on exactly these columns),
    #: ``wider`` (measured only on a longer key that contains them), or ``none``
    kind: str
    observation: KeyObservation | None = None


def lookup(observations: Iterable[KeyObservation], names: Sequence[str], columns: Iterable[str]) -> Verdict:
    """What the saved measurements say about ``columns`` of the table spelled any of ``names``.

    Unique on a subset of ``columns`` means unique on all of them. A measurement on exactly ``columns`` gives the repeats
    directly. A measurement on a longer key that contains ``columns`` says nothing about ``columns`` alone.
    """

    wanted = {c.casefold() for c in columns}
    here = [o for o in observations if any(same_table(o.table, n) for n in names)]  # newest first
    exact = next((o for o in here if {c.casefold() for c in o.columns} == wanted), None)
    subset = next((o for o in here if o.unique and o.rows > 0 and {c.casefold() for c in o.columns} < wanted), None)
    decisive = [o for o in (exact, subset) if o is not None]
    if decisive:
        newest = max(decisive, key=lambda o: o.generated_at)  # when two disagree the data changed between them: the later one stands
        return Verdict("unique" if newest.unique else "repeats", newest)
    wider = next((o for o in here if wanted < {c.casefold() for c in o.columns}), None)
    return Verdict("wider", wider) if wider is not None else Verdict("none")


def best_candidate(observations: Iterable[KeyObservation], names: Sequence[str]) -> KeyObservation | None:
    """The saved measurement of ``names`` that is closest to a key: unique, else a near-key with the fewest repeats (newest on ties), else ``None``."""

    usable = [o for o in observations if any(same_table(o.table, n) for n in names) and o.rows - o.null_rows > 1 and o.null_rows < o.rows and o.duplicate_rate <= NEAR_KEY_MAX]
    if not usable:
        return None
    return min(usable, key=lambda o: (o.duplicate_rate, len(o.columns), o.null_rate, -len(o.generated_at), o.columns))
