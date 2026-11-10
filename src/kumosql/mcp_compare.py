"""``compare_queries``: is my rewrite still the same query?

The fast path of the equivalence prover behind one call: the structural and algebraic proof first (what
``python -m kumosql prove-sql-equivalent`` runs), then, when that does not settle it, the SMT prover with the columns, keys and NOT NULL
columns the project declares, which can also return a database on which the two queries differ. The answer is one of

* ``equivalent``: proven; the assumptions the proof rests on are listed;
* ``not_equivalent``: a counterexample database on which the two queries return different rows;
* ``unknown``: neither; the reason is said, and it is never read as "different" or as "the same".

The check runs under a time cap (10 seconds by default, 60 at most). A process is forked for it so that a proof that runs
past the cap is stopped for good; where forking is not possible the call waits for the cap and reports ``unknown``.
Nothing here touches a database or the network.
"""

from __future__ import annotations

import multiprocessing
import re
import threading
import time
import warnings

from .mcp_answers import Answer, ToolError, clip, tool
from .mcp_session import Session

DEFAULT_SECONDS = 10
MAX_SECONDS = 60
MAX_SQL = 50_000
SHOWN_ROWS = 3


def run_capped(function, seconds: float):
    """``(result, reason)``: ``function()``'s result, or ``None`` and why there is none (timeout, crash).

    ``function`` returns something picklable. It runs in a forked child that is killed at the cap; without ``fork`` it runs
    in a thread that is abandoned at the cap."""

    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        context = None
    if context is None:
        box: dict = {}

        def work() -> None:
            try:
                box["result"] = function()
            except Exception as exc:  # noqa: BLE001
                box["error"] = type(exc).__name__

        thread = threading.Thread(target=work, daemon=True, name="kumosql-mcp-compare")
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            return None, "timeout"
        return box.get("result"), (f"crash:{box['error']}" if "error" in box else None)

    receiver, sender = context.Pipe(duplex=False)

    def child() -> None:
        try:
            sender.send(("ok", function()))
        except BaseException as exc:  # noqa: BLE001 - the parent reports it; nothing may escape the child
            try:
                sender.send(("error", type(exc).__name__))
            except Exception:  # noqa: BLE001
                pass
        finally:
            sender.close()

    process = context.Process(target=child, daemon=True, name="kumosql-mcp-compare")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "fork in a multi-threaded process": the child touches none of the parent's locks
        process.start()
    sender.close()
    try:
        if receiver.poll(seconds):
            try:
                kind, value = receiver.recv()
            except (EOFError, OSError):
                return None, "crash:ProcessEnded"
            return (value, None) if kind == "ok" else (None, f"crash:{value}")
        return None, "timeout"
    finally:
        if process.is_alive():
            process.kill()
        process.join(2)
        receiver.close()


def prove_pair(left: str, right: str, *, respect_row_order: bool, facts, timeout_ms: int) -> dict:
    """The verdict for two queries as plain data (runs in the child). Never raises for an unprovable pair."""

    from kumo_sql_prover.statement_proof import prove_statements

    result = prove_statements(left, right, ignore_row_order=not respect_row_order)
    if result.proven:
        notes = [d.removeprefix("assumption: ") for d in result.diagnostics if d.startswith("assumption:")]
        return {"verdict": "equivalent", "proof": "alone", "how": result.reason, "assumptions": notes}
    out: dict = {"verdict": "unknown", "reason": result.reason, "assumptions": []}
    if respect_row_order:
        out["reason"] += "; the SMT prover does not check row order, so it was not tried"
        return out
    try:
        from kumo_sql_prover.smt_equivalence import SmtStatus, z3
    except Exception:  # noqa: BLE001
        z3 = None
    if z3 is None:
        out["reason"] += "; the SMT prover is not installed"
        return out
    from kumo_sql_prover.statement_proof import prove_statements_smt

    smt = prove_statements_smt(
        left, right, schema=facts.columns or None, constraints=facts.constraints or None, types=facts.types or None,
        timeout_ms=timeout_ms, search_counterexample=True,
    )
    if smt.status is SmtStatus.PROVEN_EQUIVALENT:
        return {"verdict": "equivalent", "proof": "with_project_facts", "how": smt.reason,
                "assumptions": [*smt.assumptions, *facts.notes]}
    if smt.status is SmtStatus.NOT_EQUIVALENT and smt.counterexample is not None:
        example = smt.counterexample
        return {
            "verdict": "not_equivalent", "how": smt.reason, "assumptions": list(smt.assumptions),
            "tables": {name: [dict(row) for row in rows[:SHOWN_ROWS]] for name, rows in list(example.tables.items())[:4]},
            "table_rows": {name: len(rows) for name, rows in example.tables.items()},
            "left_rows": [list(row) for row in example.left_rows[:SHOWN_ROWS]], "left_count": len(example.left_rows),
            "right_rows": [list(row) for row in example.right_rows[:SHOWN_ROWS]], "right_count": len(example.right_rows),
        }
    out["reason"] = smt.reason if smt.status is not SmtStatus.NOT_EQUIVALENT else f"{smt.reason} (no database to show)"
    return out


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: object) -> str:
    """A prover message without the terminal colour codes a parse error carries."""

    return _ANSI.sub("", str(text))


def _value(value: object) -> object:
    """A value from a counterexample, clipped; it is made-up data, but quoted like any data."""

    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return clip(value, 40)


@tool(
    "compare_queries",
    "Compare two queries",
    "Is my rewrite still the same query? Runs the equivalence prover's fast path on two SELECT queries (written against the "
    "project's tables) and answers equivalent (proven, with its assumptions), not equivalent (with a small database on which "
    "they differ) or unknown (with the reason). Unknown is not a claim either way. Rows are compared as bags unless "
    "respect_row_order is set. Stops at time_limit seconds.",
    {
        "left": {"type": "string", "description": "The original query."},
        "right": {"type": "string", "description": "The rewritten query."},
        "time_limit": {"type": "integer", "description": f"Seconds the check may take (default {DEFAULT_SECONDS}, from 1 to {MAX_SECONDS})."},
        "respect_row_order": {"type": "boolean", "description": "Also require the same ORDER BY on both (default false: rows are compared as a bag)."},
    },
    required=("left", "right"),
)
def compare_queries(session: Session, args: dict) -> Answer:
    left, right = args["left"], args["right"]
    for name, sql in (("left", left), ("right", right)):
        if len(sql) > MAX_SQL:
            raise ToolError(f"{name} is too long ({MAX_SQL:,} characters at most)")
    seconds = args.get("time_limit", DEFAULT_SECONDS)
    if not 1 <= seconds <= MAX_SECONDS:
        raise ToolError(f"time_limit must be from 1 to {MAX_SECONDS} seconds", [f"Pass time_limit between 1 and {MAX_SECONDS}."])
    ordered = bool(args.get("respect_row_order", False))
    facts = session._once("prover_facts", _facts(session))

    began = time.monotonic()
    out, failure = run_capped(
        lambda: prove_pair(left, right, respect_row_order=ordered, facts=facts, timeout_ms=max(500, int(seconds * 1000 * 0.8))), seconds
    )
    took = time.monotonic() - began

    answer = Answer("")
    answer.fields["compared_as"] = "ordered rows (ORDER BY must match)" if ordered else "bags of rows (row order ignored)"
    answer.counts = {"seconds": round(took, 1), "time_limit": seconds}
    if out is None:
        answer.summary = f"Unknown: the check {'did not finish in %d s' % seconds if failure == 'timeout' else 'failed (' + str(failure).split(':')[-1] + ')'}."
        answer.fields["verdict"] = "unknown"
        answer.unknown("whether the queries are equivalent", "no result within the time cap" if failure == "timeout" else "the prover stopped with an error")
        answer.next(f"Pass a larger time_limit (at most {MAX_SECONDS}) or compare smaller pieces; unknown does not mean different.")
        return answer

    verdict = out["verdict"]
    answer.fields["verdict"] = verdict
    if out.get("assumptions"):
        answer.add_list("assumptions", [clip(a, 140) for a in dict.fromkeys(out["assumptions"])][:6])
    if verdict == "equivalent":
        basis = "" if out["proof"] == "alone" else ", given the project's declared columns, keys and NOT NULL columns"
        answer.summary = f"Equivalent (proven{basis})."
        answer.fields["proof"] = "unconditional" if out["proof"] == "alone" else "given declared project facts"
        answer.fields["how"] = clip(out["how"], 160)
        answer.next("Equivalence covers the rows only: confirm column types and names with check_query dry_run=true project=... before replacing the original.")
    elif verdict == "not_equivalent":
        answer.summary = f"Not equivalent: on a small database the left query returns {out['left_count']} row(s) and the right {out['right_count']}."
        answer.fields["counterexample"] = {
            "tables": {name: [{k: _value(v) for k, v in row.items()} for row in rows] for name, rows in out["tables"].items()},
            "table_rows": out["table_rows"],
            "left_returns": [[_value(v) for v in row] for row in out["left_rows"]],
            "right_returns": [[_value(v) for v in row] for row in out["right_rows"]],
        }
        answer.next("The rows above are made-up values showing one difference; fix the rewrite where that case differs, then ask again.")
    else:
        answer.summary = "Unknown: the prover could neither prove the queries equivalent nor find a difference."
        reason = _plain(out["reason"])
        answer.unknown("whether the queries are equivalent", clip(reason, 220))
        if reason.startswith("parse error"):
            answer.next("Fix the SQL so that it parses (one SELECT statement, written against the project's tables), then ask again.")
        else:
            answer.next("This does not mean they differ. Simplify the rewrite in smaller steps, or compare the two result sets on real data.")
    return answer


def _facts(session: Session):
    def build():
        from .prover_schema import from_pipeline

        return from_pipeline(session.pipeline(), [])

    return build
