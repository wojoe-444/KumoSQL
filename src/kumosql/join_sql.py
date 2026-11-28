"""The SQL behind ``join_profile`` (:mod:`kumosql.mcp_join_profile`) and ``key-check`` (:mod:`kumosql.key_check`).

One query groups each side of a join by its key once (``lg``, ``rg``) and reads every number from those two small
results: rows, rows with a NULL key, distinct keys, repeated rows, the most rows per key, and what an inner join
returns. ``join_profile`` reads it as a :class:`JoinProfile`; ``key-check`` adds its own numbers to the same groups.

This module registers no MCP tool, so the command line can import it without changing the agent server's tool order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from .data_profile import ProfileError, _ident, _text_type

MAX_KEYS = 6
DEFAULT_TOP = 5
#: the order of the numbers in the summary row of the query
SUMMARY = ("lr", "ln", "lk", "ld", "lx", "rr", "rn", "rk", "rd", "rx", "mk", "lm", "rm", "jr")


# ------------------------------------------------------------------------ the SQL


def key_aliases(count: int) -> list[str]:
    """``k0``, ``k1``, ...: the key columns of either side, renamed so both sides line up."""

    return [f"k{i}" for i in range(count)]


def side_cte(prefix: str, table_sql: str, columns: Sequence[str], dialect: str, extra: str = "") -> str:
    """``l AS (SELECT key AS k0, ... FROM table)``: one side, reduced to its key columns (and ``extra``)."""

    q = lambda name: _ident(name, dialect)  # noqa: E731
    return f"{prefix} AS (SELECT {', '.join(f'{q(c)} AS {k}' for c, k in zip(columns, key_aliases(len(columns))))}{extra} FROM {table_sql})"


def grouped_cte(prefix: str, count: int) -> str:
    """``lg``: one row per key value with its row count ``n``; ``ok`` is false for a key with a NULL in it."""

    ks = key_aliases(count)
    ok = " AND ".join(f"{k} IS NOT NULL" for k in ks)
    group = ", ".join(ks)
    return f"{prefix}g AS (SELECT {group}, COUNT(*) AS n, ({ok}) AS ok FROM {prefix} GROUP BY {group})"


def stats_cte(prefix: str, extra: Sequence[str] = ()) -> str:
    """``ls``: rows, rows with a NULL key, distinct keys, repeated rows and the most rows per key, read from ``lg``."""

    more = "".join(f", {item}" for item in extra)
    return (f"{prefix}s AS (SELECT SUM(n) AS r, SUM(CASE WHEN ok THEN 0 ELSE n END) AS nu, SUM(CASE WHEN ok THEN 1 ELSE 0 END) AS k, "
            f"SUM(CASE WHEN ok THEN n - 1 ELSE 0 END) AS d, MAX(CASE WHEN ok THEN n ELSE 0 END) AS x{more} FROM {prefix}g)")


def join_cte(count: int, extra: Sequence[str] = ()) -> str:
    """``j``: the key values both sides have (``a`` is the left group, ``b`` the right), and the rows an inner join returns."""

    on = " AND ".join(f"a.{k} = b.{k}" for k in key_aliases(count))
    more = "".join(f", {item}" for item in extra)
    return f"j AS (SELECT COUNT(*) AS mk, SUM(a.n) AS lm, SUM(b.n) AS rm, SUM(a.n * b.n) AS jr{more} FROM lg a JOIN rg b ON {on})"


def summary_numbers() -> list[str]:
    """The select list of the summary row, named as :data:`SUMMARY`."""

    numbers = [f"ls.{c} AS l{n}" for c, n in (("r", "r"), ("nu", "n"), ("k", "k"), ("d", "d"), ("x", "x"))]
    numbers += [f"rs.{c} AS r{n}" for c, n in (("r", "r"), ("nu", "n"), ("k", "k"), ("d", "d"), ("x", "x"))]
    return numbers + [f"j.{c} AS {c}" for c in ("mk", "lm", "rm", "jr")]


def build_sql(left_sql: str, right_sql: str, pairs: Sequence[tuple[str, str]], dialect: str, split: tuple[str, str] | None = None, top: int = DEFAULT_TOP,
              *, lines: bool = False) -> str | list[str]:
    """The one query (``lines=True``: its pieces, one common table expression each, for an answer that may have to cut it).

    Each side is grouped by its key once; everything else reads those two small results.
    """

    q = lambda name: _ident(name, dialect)  # noqa: E731
    ks = key_aliases(len(pairs))

    def side(prefix: str, table_sql: str, columns: Sequence[str]) -> str:
        extra = f", {q(split[1])} AS s" if split and split[0] == {"l": "left", "r": "right"}[prefix] else ""
        return side_cte(prefix, table_sql, columns, dialect, extra)

    parts = [side("l", left_sql, [p[0] for p in pairs]), side("r", right_sql, [p[1] for p in pairs]), grouped_cte("l", len(ks)), grouped_cte("r", len(ks)),
             stats_cte("l"), stats_cte("r"), join_cte(len(ks))]
    final = f"SELECT 'summary' AS kind, '' AS v, {', '.join(summary_numbers())} FROM ls, rs, j"
    if split:
        mine, other = ("l", "rg") if split[0] == "left" else ("r", "lg")
        sel_on = " AND ".join(f"x.{k} = o.{k}" for k in ks)
        parts.append(f"sp AS (SELECT CAST(x.s AS {_text_type(dialect)}) AS v, COUNT(*) AS t, SUM(CASE WHEN o.n IS NULL THEN 1 ELSE 0 END) AS u "
                     f"FROM {mine} x LEFT JOIN {other} o ON {sel_on} GROUP BY x.s ORDER BY u DESC, v LIMIT {int(top)})")
        final += f" UNION ALL SELECT 'split', v, t, u{', NULL' * (len(SUMMARY) - 2)} FROM sp"
    if lines:
        return ["WITH " + parts[0] + ","] + [part + "," for part in parts[1:-1]] + [parts[-1], final]
    return "WITH " + ", ".join(parts) + " " + final


# --------------------------------------------------------------------- the result


@dataclass
class JoinProfile:
    left: str
    right: str
    keys: list[tuple[str, str]]
    dialect: str
    generated_at: str
    sides: dict
    matched_keys: int
    join_rows: int
    split_by: tuple[str, str] | None = None
    unmatched_by: list[dict] = field(default_factory=list)
    estimated_bytes: int | None = None
    bytes_billed: int | None = None

    @property
    def cardinality(self) -> str:
        """``1:1``, ``1:N`` (a left row can match several right rows), ``N:1`` (several left rows share a key; the right is unique), ``N:M`` or ``unknown``.

        Judged on whole tables: a side is unique when no non-null key value repeats on it.
        """

        left, right = self.sides["left"], self.sides["right"]
        if not left["distinct_keys"] or not right["distinct_keys"]:
            return "unknown"
        return {(True, True): "1:1", (True, False): "1:N", (False, True): "N:1", (False, False): "N:M"}[(left["duplicate_rows"] == 0, right["duplicate_rows"] == 0)]

    def to_json(self) -> dict:
        return {"left": self.left, "right": self.right, "keys": [list(k) for k in self.keys], "dialect": self.dialect, "generated_at": self.generated_at,
                "sides": self.sides, "matched_keys": self.matched_keys, "join_rows": self.join_rows, "cardinality": self.cardinality,
                "split_by": list(self.split_by) if self.split_by else None, "unmatched_by": self.unmatched_by,
                "estimated_bytes": self.estimated_bytes, "bytes_billed": self.bytes_billed}


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 6) if whole else None


def read_rows(rows: list[dict], left: str, right: str, pairs: Sequence[tuple[str, str]], dialect: str, split: tuple[str, str] | None,
              top: int = DEFAULT_TOP) -> JoinProfile:
    """The result rows of :func:`build_sql` as a :class:`JoinProfile`."""

    summary = next((row for row in rows if row.get("kind") == "summary"), None)
    if summary is None:
        raise ProfileError("the join profile query returned no summary row")
    n = {name: _int(summary.get(name)) for name in SUMMARY}
    sides = {}
    for side, p in (("left", "l"), ("right", "r")):
        rows_, matched = n[p + "r"], n["lm" if side == "left" else "rm"]
        sides[side] = {"rows": rows_, "null_key_rows": n[p + "n"], "distinct_keys": n[p + "k"], "duplicate_rows": n[p + "d"], "max_rows_per_key": n[p + "x"],
                       "matched_rows": matched, "match_rate": _rate(matched, rows_), "null_key_rate": _rate(n[p + "n"], rows_)}
    found = sorted(({"value": row.get("v"), "rows": _int(row.get("lr")), "unmatched": _int(row.get("ln"))} for row in rows if row.get("kind") == "split"),
                   key=lambda item: (-item["unmatched"], str(item["value"])))
    found = [item for item in found if item["unmatched"] > 0][:top]
    return JoinProfile(left, right, list(pairs), dialect, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), sides, n["mk"], n["jr"], split, found)


def profile_join(left: str, right: str, pairs: Sequence[tuple[str, str]], executor, split: tuple[str, str] | None = None, top: int = DEFAULT_TOP) -> JoinProfile:
    """Run the one query with ``executor`` (a :class:`kumosql.data_profile.Executor`) and read it. Raises :class:`ProfileError`."""

    sql = build_sql(executor.table_sql(left), executor.table_sql(right), pairs, executor.dialect, split, top)
    result = read_rows(executor.run(sql), left, right, pairs, executor.dialect, split, top)
    result.estimated_bytes, result.bytes_billed = executor.estimated_bytes, executor.bytes_billed
    return result
