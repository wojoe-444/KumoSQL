"""``join_profile``: what does joining these two tables on these keys do?

One query answers the questions that took an analyst several rounds by hand: how many rows each side has, whether the keys are
unique on each side (the cardinality class), how many rows find a partner each way, how many keys are null, and, with
``split_by``, where the unmatched rows are. The whole population is counted (no ``LIMIT``, no sample), the tables are read once each
and the answer is the same on every run.

It follows the gate of ``profile_table`` (:mod:`kumosql.mcp_profile`):

* **a DuckDB database inside the project folder** (``duckdb``): free, so it runs at once;
* **a BigQuery table without ``project``**: nothing is sent. The answer is the plan: the SQL and what it would return;
* **with ``project``**: a *dry run*. BigQuery plans the one query (free, no data read) and the answer reports the estimated
  bytes against the byte cap;
* **with ``project`` and ``run=true``**: the query runs under that cap, and the plan is refused up front when its estimate is
  over it.

A completed BigQuery run is saved in the ``join-profiles`` folder beside the saved data profiles, outside the project. ``check_query``
then judges joins of those tables on those keys from it (:mod:`kumosql.mcp_join_fanout`). A run on a local copy is not saved: a copy
need not hold the warehouse table's rows.
"""

from __future__ import annotations

import re
from typing import Sequence

from . import data_profile
from . import join_profile_store as joins
from .data_profile import ByteCapExceeded, ProfileError
from .join_sql import (  # noqa: F401 - the SQL and its reading live in join_sql; re-exported here
    DEFAULT_TOP, MAX_KEYS, SUMMARY, JoinProfile, _int, _rate, build_sql, grouped_cte, join_cte, key_aliases,
    profile_join, read_rows, side_cte, stats_cte, summary_numbers,
)
from .mcp_answers import Answer, ToolError, clip, tool
from .mcp_profile import DATA_NOTE, _local_executor, _physical_name
from .mcp_session import Session

MAX_TOP = 20
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ------------------------------------------------------------------- arguments


def parse_keys(text: str) -> list[tuple[str, str]]:
    """``"a, b=c"`` as ``[("a", "a"), ("b", "c")]``: a column of the same name on both sides, or ``left_column=right_column``."""

    pairs = []
    for item in str(text).split(","):
        if not item.strip():
            continue
        left, _, right = item.partition("=")
        left, right = left.strip().strip("`"), (right or left).strip().strip("`")
        if not _NAME.match(left) or not _NAME.match(right):
            raise ToolError(f"{clip(item.strip(), 40)!r} is not a column name (or left=right pair)", ["Pass plain column names: keys=order_id or keys=customer_id=id, comma separated."])
        pairs.append((left, right))
    if not pairs:
        raise ToolError("keys names no column", ["Pass keys=<column>, or keys=<left column>=<right column>; several separated by commas."])
    if len(pairs) > MAX_KEYS:
        raise ToolError(f"at most {MAX_KEYS} key columns")
    return pairs


def parse_split(text: str | None) -> tuple[str, str] | None:
    """``("left", "order_week")`` for ``split_by=order_week`` or ``left.order_week``; ``right.col`` splits the right side."""

    if not text or not str(text).strip():
        return None
    side, _, column = str(text).strip().rpartition(".")
    side = (side or "left").strip().casefold()
    column = column.strip().strip("`")
    if side not in ("left", "right") or not _NAME.match(column):
        raise ToolError("split_by is one column name, optionally left.column or right.column", ["Example: split_by=order_week (a column of the left table)."])
    return side, column


# ------------------------------------------------------------------- the answer


def _pct(fraction: float | None) -> str:
    return "?" if fraction is None else joins.percent(fraction) if fraction < 0.0001 else f"{fraction:.1%}"


def _keys_text(pairs: Sequence[tuple[str, str]]) -> str:
    return ", ".join(a if a == b else f"{a}={b}" for a, b in pairs)


def keys_argument(pairs: Sequence[tuple[str, str]]) -> str:
    return ",".join(a if a == b else f"{a}={b}" for a, b in pairs)


def _short(name: str) -> str:
    return name.split(".")[-1]


def _quote(value: object) -> str:
    from .mcp_profile import _quoted

    return "(null)" if value is None else _quoted(value)


def _result_answer(result: JoinProfile, mode: str, source: str, saved: str | None) -> Answer:
    left, right = result.sides["left"], result.sides["right"]
    klass = result.cardinality
    names = f"{_short(result.left)} to {_short(result.right)}"
    answer = Answer(f"{names} on {_keys_text(result.keys)}: {klass}; {_pct(left['match_rate'])} of {_short(result.left)} rows and {_pct(right['match_rate'])} of "
                    f"{_short(result.right)} rows find a partner; an inner join returns {result.join_rows:,} rows.")
    answer.fields.update({"mode": mode, "source": source, "cardinality": klass, "join_rows": result.join_rows})
    for label, table, side in (("left", result.left, left), ("right", result.right, right)):
        answer.fields[label] = {"table": clip(table, 80), "rows": side["rows"], "null_key_rate": side["null_key_rate"], "distinct_keys": side["distinct_keys"],
                                "duplicate_rows": side["duplicate_rows"], "max_rows_per_key": side["max_rows_per_key"], "match_rate": side["match_rate"]}
    if result.estimated_bytes is not None or result.bytes_billed is not None:
        answer.fields["scan"] = {"estimated_bytes": result.estimated_bytes, "billed_bytes": result.bytes_billed}
    if saved:
        answer.fields["saved"] = saved
    if result.split_by:
        total = left["rows"] - left["matched_rows"] if result.split_by[0] == "left" else right["rows"] - right["matched_rows"]
        answer.fields["split_by"] = f"{result.split_by[0]}.{result.split_by[1]}"
        answer.fields["unmatched_rows"] = total
        answer.fields["note"] = DATA_NOTE
        lines = [f"{_quote(item['value'])}: {item['unmatched']:,} of {item['rows']:,} unmatched ({_pct(_rate(item['unmatched'], item['rows']))})" for item in result.unmatched_by]
        answer.add_list("unmatched_by", lines, primary=True)
    if klass in ("N:M", "1:N"):
        answer.unknown("which key values repeat", f"{_short(result.right)} has {right['duplicate_rows']:,} repeated key rows (up to {right['max_rows_per_key']:,} per key): a join repeats each matching {_short(result.left)} row")
    if klass == "unknown":
        answer.unknown("the cardinality class", "a side has no non-null key values")
    if klass in ("N:1", "N:M") and left["duplicate_rows"]:
        answer.unknown("whether the left key is a grain", f"{_short(result.left)} repeats its key on {left['duplicate_rows']:,} rows")
    if saved:
        answer.next("check_query now judges joins of these tables on these keys from this result (no extra call).")
    elif mode == "profiled":
        answer.next("This local run was not saved: a local copy need not hold the warehouse table's rows. Run it on BigQuery (project=..., run=true) to have check_query use it.")
    if klass in ("N:M", "1:N"):
        answer.next(f"Reduce {_short(result.right)} to one row per ({_keys_text(result.keys)}) before joining, or aggregate after the join knowing the rows repeat.")
    elif result.split_by is None and (left["match_rate"] or 1) < 1:
        answer.next(f"Add split_by=<a column of {_short(result.left)}> to see where its unmatched rows are.")
    return answer


# --------------------------------------------------------------------- the tool


def _physical(session: Session, text: str) -> str:
    """``project.dataset.table`` for what the agent wrote: a project table, a full name, or ``dataset.table`` read in the default project."""

    value = text.strip().strip("`")
    key, _ = session.resolve(value)
    if key is None and re.fullmatch(r"[\w-]+\.[\w-]+", value) and session.pipeline().default_project:
        return f"{session.pipeline().default_project}.{value}"
    return _physical_name(session, value)[0]


def _plan_answer(left: str, right: str, pairs, split, top: int, sql: list[str]) -> Answer:
    answer = Answer(f"Planned only: one BigQuery query would profile joining {_short(left)} to {_short(right)} on {_keys_text(pairs)}; nothing was sent.")
    returns = "per side: rows, null-key rate, repeated key rows, match rate; cardinality class; inner join rows"
    if split:
        returns += f"; top {top} unmatched by {split[0]}.{split[1]}"
    answer.fields.update({"mode": "plan_only", "left": left, "right": right, "returns": returns})
    answer.add_list("sql", sql, primary=True)
    answer.unknown("row counts and match rates", "nothing was run; the estimated scan needs a BigQuery dry run (free, reads no data): pass project=<the project to bill>")
    answer.next(f"join_profile left={left} right={right} keys={keys_argument(pairs)} project=<your project> reports the estimated bytes against the byte cap; add run=true to run it.")
    return answer


def _refusal(reason: str, total: int | None, cap: int) -> Answer:
    answer = Answer(f"Not run: {clip(reason, 200)}.")
    answer.fields.update({"mode": "refused", "scan": {"estimated_bytes": total, "byte_cap": cap}})
    answer.unknown("the join profile", "refused before anything ran: it is over the byte cap")
    answer.next("Raise the cap in Settings → Scopes, or profile a smaller pair of tables (a view that selects only the key columns).")
    return answer


def _not_run(reason: str) -> Answer:
    answer = Answer("BigQuery was not reached; nothing was profiled.")
    answer.fields["mode"] = "not_run"
    answer.unknown("BigQuery dry run", reason)
    answer.next("Check the credentials (BQ_ACCESS_TOKEN or application default credentials), the project, the table names and the key columns, then ask again; plan-only answers need no credentials (leave project out).")
    return answer


def _save(session: Session, result: JoinProfile) -> str | None:
    folder = session.profile_folder_outside_project()
    if folder is None:
        return None
    try:
        path = joins.save_join(result.to_json(), session.profiles_directory)
    except ProfileError:
        return None
    return f"join-profiles/{path.stem}.json in the profile folder"


@tool(
    "join_profile",
    "Profile a join of two tables",
    "What does joining these two tables on these keys do? One query returns the row counts, the cardinality class (1:1, 1:N, N:1, N:M), "
    "the share of rows that find a partner each way, the null-key share and, with split_by, where the unmatched rows are. The whole "
    "population is counted. Like profile_table it is only planned unless you pass project: with project (and no run) BigQuery's free "
    "dry run reports the estimated scan against the byte cap; with project and run=true it runs under that cap and the result is "
    "saved outside the project, so check_query judges later joins of these tables from it. A DuckDB database inside the project "
    "folder is free and runs at once.",
    {
        "left": {"type": "string", "description": "The left table: a table of the project, project.dataset.table, or dataset.table (read in the project's default project)."},
        "right": {"type": "string", "description": "The right table, written the same way."},
        "keys": {"type": "string", "description": "Join columns, comma separated: a name used on both sides, or left_column=right_column (customer_id=id)."},
        "split_by": {"type": "string", "description": "A column to count unmatched rows by (the left table's unless written right.column). Values are table data."},
        "top": {"type": "integer", "description": f"How many split_by values to show (1 to {MAX_TOP}, default {DEFAULT_TOP})."},
        "project": {"type": "string", "description": "The BigQuery project to bill for the dry run or the profile. Never guessed."},
        "run": {"type": "boolean", "description": "Actually run the query on BigQuery (needs project). Default false: dry run only."},
        "duckdb": {"type": "string", "description": "Run on tables of this DuckDB database file inside the project folder instead (free)."},
    },
    required=("left", "right", "keys"),
    open_world=True,
)
def join_profile(session: Session, args: dict) -> Answer:
    pairs = parse_keys(args["keys"])
    split = parse_split(args.get("split_by"))
    top = args.get("top", DEFAULT_TOP)
    if not 1 <= top <= MAX_TOP:
        raise ToolError(f"top must be from 1 to {MAX_TOP}")
    project = (args.get("project") or "").strip()
    run = bool(args.get("run", False))
    local = bool(args.get("duckdb"))
    if run and not project and not local:
        raise ToolError("run=true needs an explicit project", ["Pass project=<the BigQuery project to bill>; it is not guessed. Without run, a dry run reports the cost first."])
    if local and (project or run):
        raise ToolError("project and run are for BigQuery tables; a DuckDB database is free and runs at once", ["Leave project and run out."])

    if local:
        left, right = str(args["left"]).strip(), str(args["right"]).strip()
        executor, _table, label = _local_executor(session, {"duckdb": args["duckdb"], "table": left})
        try:
            result = profile_join(left, right, pairs, executor, split, top)
        except ProfileError as exc:
            raise ToolError(f"could not profile the join: {clip(exc, 200)}", ["Check the table names and key columns."]) from None
        finally:
            executor.connection.close()
        return _result_answer(result, "profiled", "local, free", None)

    left, right = _physical(session, str(args["left"])), _physical(session, str(args["right"]))
    if not project:
        for name in (left, right):
            if not re.fullmatch(r"[\w\[\]-]+(\.[\w\[\]-]+){0,2}", name):
                raise ToolError("a table name is letters, digits, dashes, underscores and dots", ["Name BigQuery tables as project.dataset.table."])
        answer = _plan_answer(left, right, pairs, split, top, build_sql(f"`{left}`", f"`{right}`", pairs, "bigquery", split, top, lines=True))
        if any(name.count(".") != 2 for name in (left, right)):
            answer.unknown("full table names", "the project does not give a default project for a name written without one; BigQuery needs project.dataset.table to plan or run")
        return answer
    try:
        executor = data_profile.BigQueryExecutor(project)
        sql = build_sql(executor.table_sql(left), executor.table_sql(right), pairs, "bigquery", split, top)
        estimate = executor.estimate(sql)
    except ByteCapExceeded as exc:
        return _refusal(str(exc), None, getattr(executor, "max_bytes", 0))
    except ProfileError as exc:
        return _not_run(f"not checked: {clip(exc, 220)}")
    except Exception as exc:  # noqa: BLE001 - no credentials, no network: say so
        return _not_run(f"not checked: {clip(str(exc) or type(exc).__name__, 220)}")
    cap = executor.max_bytes
    if estimate is not None and estimate > cap:
        return _refusal(f"the query would process about {estimate:,} bytes, over the {cap:,}-byte cap", estimate, cap)
    if not run:
        about = f"about {estimate:,} bytes" if estimate is not None else "an unknown number of bytes"
        answer = Answer(f"Dry run: profiling the join of {_short(left)} and {_short(right)} would process {about} in one query (cap {cap:,}); nothing was run.")
        answer.fields.update({"mode": "dry_run", "left": left, "right": right, "scan": {"estimated_bytes": estimate, "byte_cap": cap}})
        if estimate is None:
            answer.unknown("estimate", "BigQuery gave no estimate for the query")
        answer.next(f"join_profile left={left} right={right} keys={keys_argument(pairs)} project={project} run=true runs it and saves the result.")
        return answer
    try:
        result = profile_join(left, right, pairs, executor, split, top)
    except ByteCapExceeded as exc:
        return _refusal(str(exc), estimate, cap)
    except ProfileError as exc:
        return _not_run(f"not run: {clip(exc, 220)}")
    except Exception as exc:  # noqa: BLE001
        return _not_run(f"not run: {clip(str(exc) or type(exc).__name__, 220)}")
    return _result_answer(result, "profiled", "BigQuery, run under the byte cap", _save(session, result))
