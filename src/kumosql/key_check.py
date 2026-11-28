"""``key-check``: is this a key? Duplicate groups, NULLs and, for a proposed join, its fan-out, in one read-only query.

    python -m kumosql key-check proj.ds.orders --key order_id
    python -m kumosql key-check proj.ds.orders --key customer_id --join proj.ds.customers --on customer_id=id

It is the command-line face of ``join_profile`` (:mod:`kumosql.mcp_join_profile`) plus duplicate groups: the same SQL
(:mod:`kumosql.join_sql`) groups each side by its key once (``lg``, ``rg``) and reads every number from those groups, so each table is read once.
Key-check adds the repeating key values and how many rows they cover, the largest groups, the NULLs per key column and,
with ``--join``, how far the join fans out.

Inputs are read like ``reconcile``'s (:func:`kumosql.reconcile.resolve_input`): a table, a ``.sql`` file or a
``SELECT``/``WITH`` query, and the executors are reconcile's: BigQuery reads the column lists with free dry runs and runs
the one query in the billing project under the byte cap, after a dry run when asked. Key values are table data: the
largest groups are listed by size only, and their key values only with ``include_samples``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import join_sql as joins
from .reconcile import (
    ByteCapExceeded, Column, Input, ReconcileError, _int, _keys, _listed, _names, _readonly, json_rows, refuse_complex_key,
    resolve_input,
)

__all__ = ["DEFAULT_TOP", "MAX_KEYS", "MAX_TOP", "ByteCapExceeded", "KeyCheckPlan", "KeyCheckResult", "ReconcileError",
           "build_key_check_sql", "key_check", "plan_key_check"]

DEFAULT_TOP = 5
MAX_TOP = 100
MAX_KEYS = joins.MAX_KEYS
_NARROW = "Shorten the TABLE or --join SQL"

#: added to the table's ``ls``: key values that repeat, the rows they cover (NULL keys excluded; they never join)
_GROUPS = ("SUM(CASE WHEN ok AND n > 1 THEN 1 ELSE 0 END) AS g", "SUM(CASE WHEN ok AND n > 1 THEN n ELSE 0 END) AS gr")
#: added to ``j``: matched keys with more than one row on the other side, the table rows they hold, the most other rows per key
_FAN_OUT = ("SUM(CASE WHEN b.n > 1 THEN 1 ELSE 0 END) AS fk", "SUM(CASE WHEN b.n > 1 THEN a.n ELSE 0 END) AS fr",
            "MAX(b.n) AS fx")


@dataclass
class KeyCheckPlan:
    """Everything resolved before the query runs, and the query itself (``sql``)."""

    table: Input
    dialect: str
    keys: list[str]
    other: Input | None = None
    on: list[tuple[str, str]] = field(default_factory=list)
    include_samples: bool = False
    top: int = DEFAULT_TOP
    notes: list[str] = field(default_factory=list)
    sql: str = ""


def _find(columns: Sequence[Column], name: str, where: str) -> Column:
    found = {column.name.casefold(): column for column in columns}.get(name.casefold())
    if found is None:
        raise ReconcileError(f"{where} has no column {name!r}; it has {_listed(columns)}")
    refuse_complex_key(found)
    return found


def _on_pairs(on: object) -> list[tuple[str, str]]:
    pairs = []
    for item in _names(on):
        left, sep, right = item.partition("=")
        left, right = left.strip().strip("`"), right.strip().strip("`")
        if not left or not sep or not right:
            raise ReconcileError(f"--on {item!r}: write key_column=other_column, comma separated")
        pairs.append((left, right))
    return pairs


def _top_sql(count: int, names: Sequence[str], dialect: str, top: int, samples: bool) -> str:
    """``tg``: the sizes of the largest repeating groups and, with samples, their key values, in the same order."""

    order = ", ".join(["n DESC", *joins.key_aliases(count)])
    parts = [json_rows("ok AND n > 1", [("n", "n")], order, top, dialect, "tn")]
    if samples:
        parts.append(json_rows("ok AND n > 1", list(zip(names, joins.key_aliases(count))), order, top, dialect, "tk"))
    return f"tg AS (SELECT {', '.join(parts)} FROM lg)"


def _build_sql(plan: KeyCheckPlan) -> str:
    count, d = len(plan.keys), plan.dialect
    nulls = tuple(f"SUM(CASE WHEN k{i} IS NULL THEN n ELSE 0 END) AS z{i}" for i in range(count))
    parts = [joins.side_cte("l", f"{plan.table.source} AS kumo_src", plan.keys, d), joins.grouped_cte("l", count),
             joins.stats_cte("l", (*_GROUPS, *nulls))]
    numbers = ["ls.r AS lr", "ls.nu AS ln", "ls.k AS lk", "ls.d AS ld", "ls.x AS lx", "ls.g AS lgk", "ls.gr AS lgr",
               *(f"ls.z{i} AS z{i}" for i in range(count)), "tg.tn AS tn"]
    if plan.include_samples:
        numbers.append("tg.tk AS tk")
    sources = ["ls", "tg"]
    if plan.other is not None:
        parts += [joins.side_cte("r", f"{plan.other.source} AS kumo_src", [b for _, b in plan.on], d),
                  joins.grouped_cte("r", count), joins.stats_cte("r", _GROUPS), joins.join_cte(count, _FAN_OUT)]
        numbers = [item for item in joins.summary_numbers() if not item.startswith("ls.")] + numbers
        numbers += ["rs.g AS rgk", "j.fk AS fk", "j.fr AS fr", "j.fx AS fx"]
        sources += ["rs", "j"]
    parts.append(_top_sql(count, plan.keys, d, plan.top, plan.include_samples))
    return "WITH " + ",\n".join(parts) + f"\nSELECT 'summary' AS kind, {', '.join(numbers)} FROM {', '.join(sources)}"


def plan_key_check(table: str, keys: Iterable[str] | str, executor, *, join: str | None = None, on: object = None,
                   include_samples: bool = False, top: int = DEFAULT_TOP) -> KeyCheckPlan:
    """Resolve the table, its key and the proposed join, and write the one query. Raises :class:`ReconcileError`."""

    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= MAX_TOP:
        raise ReconcileError(f"--top must be a whole number from 1 to {MAX_TOP}")
    names = _names(keys)
    if not names:
        raise ReconcileError("--key needs at least one column (a,b)")
    if any("=" in name for name in names):
        raise ReconcileError("--key names the table's own columns; map them to the other table's with --on a=x")
    if len(names) > MAX_KEYS:
        raise ReconcileError(f"--key takes at most {MAX_KEYS} columns")
    pairs = _on_pairs(on)
    if pairs and join is None:
        raise ReconcileError("--on maps the key to the columns of the --join table; give --join too")
    tin = resolve_input(table, executor, "table", flag="TABLE")
    tcols = executor.columns(tin.source)
    key_cols = [_find(tcols, name, "the table") for name in names]
    if len({column.name.casefold() for column in key_cols}) != len(key_cols):
        raise ReconcileError("--key names a column twice")
    notes = [f"the key column {column.name} is floating-point: a join on it matches only exactly equal values"
             for column in key_cols if column.is_float]
    plan = KeyCheckPlan(tin, executor.dialect, [column.name for column in key_cols], include_samples=include_samples,
                        top=top, notes=notes)
    if join is not None:
        oin = resolve_input(join, executor, "join")
        ocols = executor.columns(oin.source)
        given = {}
        for left, right in pairs:
            if left.casefold() not in {name.casefold() for name in plan.keys}:
                raise ReconcileError(f"--on {left}={right}: {left} is not one of the --key columns ({', '.join(plan.keys)})")
            if left.casefold() in given:
                raise ReconcileError(f"--on maps {left} twice")
            given[left.casefold()] = right
        for column in key_cols:
            other = _find(ocols, given.get(column.name.casefold(), column.name), "the --join table")
            if other.is_float and not column.is_float:
                plan.notes.append(f"the --join column {other.name} is floating-point: the join matches only exactly "
                                  "equal values")
            plan.on.append((column.name, other.name))
        plan.other = oin
    plan.sql = _build_sql(plan)
    _readonly(plan.sql, "the key-check query", _NARROW)
    return plan


def build_key_check_sql(table: str, keys: Iterable[str] | str, executor, **options) -> str:
    """The one read-only query :func:`key_check` would run (nothing runs; BigQuery reads the column lists with dry runs)."""

    return plan_key_check(table, keys, executor, **options).sql


# -------------------------------------------------------------------- results


@dataclass
class KeyCheckResult:
    table: str
    dialect: str
    key: list[str]
    rows: int
    distinct_keys: int
    duplicates: dict
    nulls: dict
    join: dict | None = None
    samples_included: bool = False
    estimated_bytes: int | None = None
    bytes_billed: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def unique(self) -> bool:
        """No non-NULL key value repeats."""

        return self.duplicates["groups"] == 0

    @property
    def problems(self) -> list[str]:
        found = []
        if self.duplicates["groups"]:
            found.append("duplicate_keys")
        if self.nulls["rows"]:
            found.append("null_keys")
        if self.join is not None and self.join["fan_out"]["keys_with_several_rows"]:
            found.append("join_fans_out")
        return found

    @property
    def ok(self) -> bool:
        """The key is unique and never NULL, and a proposed join repeats no row of the table."""

        return not self.problems

    def to_json(self) -> dict:
        return {
            "table": self.table, "dialect": self.dialect, "key": list(self.key), "rows": self.rows,
            "distinct_keys": self.distinct_keys, "unique": self.unique,
            "duplicates": dict(self.duplicates), "nulls": dict(self.nulls), "join": self.join,
            "ok": self.ok, "problems": self.problems, "samples_included": self.samples_included,
            "estimated_bytes": self.estimated_bytes, "bytes_billed": self.bytes_billed, "notes": list(self.notes),
        }


def _groups(row: dict, plan: KeyCheckPlan) -> list[dict]:
    sizes = [_int(item.get("n")) if isinstance(item, dict) else 0 for item in _keys(row.get("tn"))]
    if not plan.include_samples:
        return [{"rows": size} for size in sizes]
    values = _keys(row.get("tk"))
    return [{"rows": size, "key": value if isinstance(value, dict) else None}
            for size, value in zip(sizes, values + [None] * (len(sizes) - len(values)))]


def _join(row: dict, plan: KeyCheckPlan) -> dict:
    profile = joins.read_rows([row], plan.table.spec, plan.other.spec, plan.on, plan.dialect, None)
    mine, other = profile.sides["left"], profile.sides["right"]
    unmatched = mine["rows"] - mine["matched_rows"]
    left_join = profile.join_rows + unmatched
    return {
        "other": plan.other.spec, "on": {a: b for a, b in plan.on}, "cardinality": profile.cardinality,
        "other_rows": other["rows"], "other_null_key_rows": other["null_key_rows"],
        "other_distinct_keys": other["distinct_keys"], "other_duplicate_groups": _int(row.get("rgk")),
        "other_max_rows_per_key": other["max_rows_per_key"], "matched_keys": profile.matched_keys,
        "unmatched": {"rows": unmatched, "keys": mine["distinct_keys"] - profile.matched_keys,
                      "other_rows": other["rows"] - other["matched_rows"],
                      "other_keys": other["distinct_keys"] - profile.matched_keys},
        "fan_out": {"max_rows_per_key": _int(row.get("fx")), "keys_with_several_rows": _int(row.get("fk")),
                    "rows_multiplied": _int(row.get("fr")), "inner_join_rows": profile.join_rows,
                    "left_join_rows": left_join, "added_rows": left_join - mine["rows"]},
    }


def _result(plan: KeyCheckPlan, row: dict, executor) -> KeyCheckResult:
    n = lambda key: _int(row.get(key))  # noqa: E731
    rows, null_rows = n("lr"), n("ln")
    duplicates = {"groups": n("lgk"), "rows": n("lgr"), "extra_rows": n("ld"), "max_rows_per_key": n("lx"),
                  "largest_groups": _groups(row, plan)}
    nulls = {"rows": null_rows, "by_column": {name: n(f"z{i}") for i, name in enumerate(plan.keys)}}
    notes = list(plan.notes)
    if null_rows:
        notes.append(f"{null_rows:,} row(s) have a NULL in a key column: NULL never equals anything, so they never join "
                     "and are not counted as duplicates")
    join = _join(row, plan) if plan.other is not None else None
    if join is not None:
        if join["other_null_key_rows"]:
            notes.append(f"{join['other_null_key_rows']:,} row(s) of the --join table have a NULL in a key column and "
                         "never join")
        if join["fan_out"]["keys_with_several_rows"]:
            notes.append(f"{join['fan_out']['keys_with_several_rows']:,} matched key value(s) have more than one row in "
                         f"the --join table: a join repeats {join['fan_out']['rows_multiplied']:,} row(s) of the table "
                         f"and adds {join['fan_out']['added_rows']:,}")
    return KeyCheckResult(
        table=plan.table.spec, dialect=plan.dialect, key=list(plan.keys), rows=rows, distinct_keys=n("lk"),
        duplicates=duplicates, nulls=nulls, join=join, samples_included=plan.include_samples,
        estimated_bytes=executor.estimated_bytes, bytes_billed=executor.bytes_billed, notes=notes,
    )


def key_check(table: str, keys: Iterable[str] | str, executor, *, join: str | None = None, on: object = None,
              include_samples: bool = False, top: int = DEFAULT_TOP) -> KeyCheckResult:
    """Check ``keys`` on ``table`` and, with ``join``, the proposed join ``on`` (``"a=x,b=y"``; a key column whose name
    is the same in the other table needs no entry), in one read-only query.

    ``table`` and ``join`` are table names, ``.sql`` files or inline ``SELECT``/``WITH`` queries. ``top`` is how many of
    the largest repeating groups to list (by size; with ``include_samples`` also by key value). Raises
    :class:`~kumosql.reconcile.ReconcileError`, and :class:`~kumosql.reconcile.ByteCapExceeded` when a BigQuery dry run
    is over the cap."""

    plan = plan_key_check(table, keys, executor, join=join, on=on, include_samples=include_samples, top=top)
    _readonly(plan.sql, "the key-check query", _NARROW)
    rows = executor.run(plan.sql)
    return _result(plan, rows[0] if rows else {}, executor)
