"""``compare_queries``: is my rewrite still the same query?

The fast path of the equivalence prover behind one call: the structural and algebraic proof first (what
``python -m kumosql prove-sql-equivalent`` runs), then, when that does not settle it, the SMT prover with the columns, keys and NOT NULL
columns the project declares, which can also return a database on which the two queries differ. The answer is one of

* ``equivalent``: proven; the assumptions the proof rests on are listed;
* ``not_equivalent``: a counterexample database on which the two queries return different rows;
* ``unknown``: neither; the reason is said, and it is never read as "different" or as "the same".

What the command line adds with ``--conditional``, ``--explain-difference`` and ``--verifier-sql`` is asked for too, with the
same prover calls, and only inside the same time limit:

* when the verdict is not ``equivalent``, the **conditional search** looks for facts the queries do not state (a NOT NULL
  column, a unique key, a foreign key) under which the pair is proven equivalent. Each condition comes with the query that
  counts the rows breaking it, and says ``not checked``: this tool runs no query, so a condition is never read as true and
  the verdict stays what it was;
* when no conditions settle it, the **difference search** asks for a verified predicate P with "the same except on rows
  where P holds";
* the **verifier SQL**, a query that compares the two result bags on real data, comes with every verdict but ``equivalent``;
* when the pair is proven only with the project's declared facts, the **needed-facts search**
  (``constraint_dependence.needed_guarantees``) finds which of them the proof uses, and the answer names each with where it
  was declared (a model's ``assertions``, a declaration, an assertion file, a Malloy or docs file, ``--assume-grain``, the
  schema snapshot) and how it is checked.

Each search starts only with at least :data:`MIN_SEARCH_SECONDS` of the limit left, and one that does not finish says so;
a search that finds nothing adds nothing. The checks run under a time cap (10 seconds by default, 60 at most). A process
is forked for each so that a proof that runs past the cap is stopped for good; where forking is not possible the call
waits for the cap and reports what it has. Nothing here touches a database or the network.
"""

from __future__ import annotations

import multiprocessing
import re
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .mcp_answers import MAX_BYTES, Answer, ToolError, bytes_wanted, clip, render, size, tool
from .mcp_session import Session
from .mcp_tools import expand_refs

DEFAULT_SECONDS = 10
MAX_SECONDS = 60
MAX_SQL = 50_000
SHOWN_ROWS = 3
#: the least time left of the limit that a follow-up search (conditions, difference, needed facts) is started with
MIN_SEARCH_SECONDS = 1.0
#: the keys under which a list item or a field of an answer carries SQL: shown whole or not at all (a query cut short can
#: still run, and count something else)
SQL_KEYS = ("check", "sql", "where")

#: how a declared fact or a condition is known to hold
NOT_CHECKED = "not checked"
CHECKED_BY_USER = "checked by the user"
CHECKED_BY_QUERY = "checked with a query"
ENFORCED = "enforced by BigQuery"
_STATUS_RANK = {ENFORCED: 0, CHECKED_BY_QUERY: 1, CHECKED_BY_USER: 2, NOT_CHECKED: 3}
#: a grain claim's kind (:mod:`kumosql.source_grain`) and how it is checked: an assertion file is Dataform's own query,
#: ``--assume-grain`` is the person who started the server, a declaration, Malloy or docs line is something somebody wrote
_CLAIM_STATUS = {"assume": CHECKED_BY_USER, "config": NOT_CHECKED, "assertion": CHECKED_BY_QUERY, "malloy": NOT_CHECKED, "docs": NOT_CHECKED}
_STATUS_NOTE = {
    CHECKED_BY_QUERY: "Dataform runs the assertion with the pipeline; its last result is not read here",
    ENFORCED: "a REQUIRED column in the schema snapshot; BigQuery rejects NULLs while the column stays REQUIRED",
}


class _Stopped(BaseException):
    """Raised in a check's thread that ran past its cap. A ``BaseException``, so the prover's ``except Exception`` (a crash
    in the solver is a failure to prove) does not swallow it and carry on."""


def _stop(thread: threading.Thread) -> None:
    """Ask ``thread`` to stop: :class:`_Stopped` is raised in it at its next Python instruction (a solver or DuckDB call
    in progress ends first; both run under their own time limits). Without it, a search left running would hold the
    interpreter and make the next question time out."""

    try:
        import ctypes

        ident = ctypes.c_ulong(thread.ident)
        if ctypes.pythonapi.PyThreadState_SetAsyncExc(ident, ctypes.py_object(_Stopped)) > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ident, None)  # more than one thread: take it back
    except Exception:  # noqa: BLE001 - not CPython: the thread runs on, as it did before
        pass


def run_capped(function, seconds: float):
    """``(result, reason)``: ``function()``'s result, or ``None`` and why there is none (timeout, crash).

    ``function`` returns something picklable. It runs in a forked child that is killed at the cap; without ``fork`` (Windows)
    it runs in a thread that is told to stop at the cap (:func:`_stop`) and is then left to end on its own."""

    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        context = None
    if context is None:
        box: dict = {}

        def work() -> None:
            try:
                box["result"] = function()
            except _Stopped:
                pass
            except Exception as exc:  # noqa: BLE001
                box["error"] = type(exc).__name__

        thread = threading.Thread(target=work, daemon=True, name="kumosql-mcp-compare")
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            _stop(thread)
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


# ------------------------------------------------------------------ the prover calls (each runs in the child)


def prove_pair(left: str, right: str, *, respect_row_order: bool, facts, timeout_ms: int) -> dict:
    """The verdict for two queries as plain data (runs in the child). Never raises for an unprovable pair.

    ``searchable`` says the SMT prover ran, so the follow-up searches can; ``verifier_sql`` is the bag-comparison query of
    ``prove-sql-equivalent --verifier-sql``; ``conditional`` is set when the SMT prover itself proved the pair only under
    conditions (a value read once per statement, such as ``CURRENT_DATE``)."""

    from kumo_sql_prover.statement_proof import prove_statements

    result = prove_statements(left, right, ignore_row_order=not respect_row_order)
    if result.proven:
        notes = [d.removeprefix("assumption: ") for d in result.diagnostics if d.startswith("assumption:")]
        return {"verdict": "equivalent", "proof": "alone", "how": result.reason, "assumptions": notes}
    out: dict = {"verdict": "unknown", "reason": result.reason, "assumptions": [], "verifier_sql": result.verifier_sql or ""}
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
    out["searchable"] = not str(smt.reason).startswith("parse error")
    if smt.status is SmtStatus.PROVEN_EQUIVALENT:
        return {"verdict": "equivalent", "proof": "with_project_facts", "how": smt.reason,
                "assumptions": [*smt.assumptions, *facts.notes]}
    if smt.status is SmtStatus.PROVEN_CONDITIONALLY:
        out["conditional"] = _conditional(smt)
        out["reason"] = "proven only under the conditions listed, not without them"
        return out
    if smt.status is SmtStatus.NOT_EQUIVALENT and smt.counterexample is not None:
        example = smt.counterexample
        out.update({
            "verdict": "not_equivalent", "how": smt.reason, "assumptions": list(smt.assumptions),
            "tables": {name: [dict(row) for row in rows[:SHOWN_ROWS]] for name, rows in list(example.tables.items())[:4]},
            "table_rows": {name: len(rows) for name, rows in example.tables.items()},
            "left_rows": [list(row) for row in example.left_rows[:SHOWN_ROWS]], "left_count": len(example.left_rows),
            "right_rows": [list(row) for row in example.right_rows[:SHOWN_ROWS]], "right_count": len(example.right_rows),
        })
        out.pop("reason", None)
        return out
    out["reason"] = smt.reason if smt.status is not SmtStatus.NOT_EQUIVALENT else f"{smt.reason} (no database to show)"
    return out


def _conditional(result) -> dict:
    """A ``PROVEN_CONDITIONALLY`` result as plain data: the conditions in one line, and each with its check query."""

    from kumo_sql_prover.conditional_equivalence import describe

    return {
        "when": describe(result.conditions),
        "how": result.reason,
        "assumptions": list(result.assumptions),
        "conditions": [{"text": c.text, "check_sql": c.check_sql} for c in result.conditions],
    }


def find_conditions(left: str, right: str, *, facts, timeout_ms: int, wall_seconds: float) -> dict | None:
    """``prove-sql-equivalent --conditional``, with the project's facts assumed (a declared fact is never a condition).

    ``None`` when no set of conditions proves the pair."""

    from kumo_sql_prover.smt_equivalence import SmtStatus
    from kumo_sql_prover.statement_proof import prove_statements_smt

    result = prove_statements_smt(
        left, right, conditional=True, schema=facts.columns or None, constraints=facts.constraints or None,
        types=facts.types or None, timeout_ms=timeout_ms, conditional_seconds=wall_seconds,
    )
    return _conditional(result) if result.status is SmtStatus.PROVEN_CONDITIONALLY else None


def find_difference(left: str, right: str, *, timeout_ms: int) -> dict | None:
    """``prove-sql-equivalent --explain-difference``: a verified predicate P ("the same except when P"), or ``None``."""

    from kumo_sql_prover.difference_surface import except_when

    return except_when(left, right, timeout_ms=timeout_ms)


def find_needed(left: str, right: str, *, facts, timeout_ms: int) -> dict:
    """Which declared facts the proof of the pair needs (``needed_guarantees``, proving with ``prove_statements_smt``).

    Only the facts filed under the table names the queries write are offered: the prover looks a table's facts up by the
    name as written, and the same facts filed under the table's other spellings would be tried once per spelling."""

    from kumo_sql_prover.constraint_dependence import needed_guarantees
    from kumo_sql_prover.statement_proof import prove_statements_smt

    report = needed_guarantees(
        left, right, schema=facts.columns or None, constraints=written_constraints(facts.constraints, left, right),
        prove=prove_statements_smt, types=facts.types or None, timeout_ms=timeout_ms,
    )
    return {
        "status": report.status,
        "needed": [_guarantee(g) for g in report.needed],
        "assumptions": list(report.result.assumptions) if report.result is not None else [],
        "proofs_run": report.proofs_run,
    }


def _guarantee(g) -> dict:
    return {"kind": g.kind, "table": g.table, "columns": list(g.columns), "parent": g.parent, "parent_columns": list(g.parent_columns)}


def written_tables(*queries: str) -> set[str] | None:
    """Lower-case table names as the queries write them (``p.ds.t``, ``ds.t`` or ``t``), or ``None`` when one does not parse."""

    import sqlglot
    from sqlglot import exp

    names: set[str] = set()
    try:
        for sql in queries:
            for tree in sqlglot.parse(sql, read="bigquery"):
                if tree is not None:
                    names.update(".".join(part.name for part in table.parts).lower() for table in tree.find_all(exp.Table))
    except Exception:  # noqa: BLE001 - without the names every fact is offered, which costs time, never soundness
        return None
    return names


def written_constraints(constraints: Mapping, left: str, right: str) -> dict:
    """``constraints`` kept for the table names the queries write and the parents of their foreign keys."""

    names = written_tables(left, right)
    if names is None:
        return dict(constraints or {})
    kept = {name: facts for name, facts in (constraints or {}).items() if name in names}
    for facts in list(kept.values()):
        for _columns, parent, _parent_columns in getattr(facts, "foreign_keys", ()):
            if parent.lower() in constraints:
                kept[parent.lower()] = constraints[parent.lower()]
    return kept


# ------------------------------------------------------------------ the project's facts, with where each came from


@dataclass
class ProjectFacts:
    """The facts handed to the prover (a :class:`kumosql.prover_schema.ProverSchema`), and where each came from."""

    schema: object
    #: every spelling of a table (``p.ds.t``, ``ds.t``, ``t``) -> the one table it names
    owners: dict[str, str] = field(default_factory=dict)
    #: ``(table, kind, columns)`` -> ``{origin: status}``; ``kind`` is ``not_null``, ``unique`` or ``foreign_key``
    origins: dict[tuple, dict[str, str]] = field(default_factory=dict)

    def describe(self, g: Mapping) -> dict:
        """One needed fact for the answer: the fact in words about the table it names, where it came from, its status."""

        table = self.owners.get(g["table"].lower(), g["table"])
        parent = self.owners.get(g["parent"].lower(), g["parent"])
        columns = tuple(c.lower() for c in g["columns"])
        if g["kind"] == "not_null":
            text = f"{table}.{columns[0]} is NOT NULL"
        elif g["kind"] == "unique":
            text = f"({', '.join(columns)}) is unique in {table}"
        else:
            text = f"{table}({', '.join(columns)}) references {parent}({', '.join(g['parent_columns'])})"
        found = self.origins.get((table, g["kind"], columns))
        if found is None and g["kind"] == "unique":
            found = next((v for (t, k, c), v in self.origins.items() if t == table and k == "unique" and set(c) == set(columns)), None)
        found = found or {"declared in the project": NOT_CHECKED}
        status = min(found.values(), key=lambda s: _STATUS_RANK[s])
        item = {"fact": text, "from": clip("; ".join(found), 160), "status": status}
        if status in (NOT_CHECKED, CHECKED_BY_USER):  # an assertion is its own check, and BigQuery enforces a REQUIRED column
            item["check"] = _check_sql(g["kind"], table, columns, parent, g["parent_columns"])
        return item


def project_facts(session: Session) -> ProjectFacts:
    """What the prover may assume about the project's tables, as :func:`kumosql.prover_schema.from_pipeline` collects it
    (the models' ``assertions`` and the source column lists), plus the grain claims the session reads (declarations,
    assertion files, Malloy and docs files, ``--assume-grain``) and the REQUIRED columns of the schema snapshot."""

    from .prover_schema import _Builder, _select_names

    pipeline = session.pipeline()
    builder = _Builder()
    origins: dict[tuple, dict[str, str]] = {}

    def record(table: str, kind: str, columns, origin: str, status: str) -> None:
        origins.setdefault((table.strip("`").lower(), kind, tuple(c.lower() for c in columns)), {}).setdefault(origin, status)

    # the same facts as ``from_pipeline(pipeline, [])``, in the same order
    for key, columns in (getattr(pipeline, "source_schema", None) or {}).items():
        if isinstance(columns, Mapping) and columns:
            builder.add(str(key), list(columns), source="dataform")
    for key, model in getattr(pipeline, "models", {}).items():
        name = model.target.key or key
        columns = _select_names(model.sql) if model.is_query and "${" not in model.sql else []
        builder.add(name, columns, model.non_null, model.unique_keys, source="dataform" if (model.non_null or model.unique_keys or columns) else "")
        short = key.split(".")[-1]
        for column in model.non_null:
            record(name, "not_null", (column,), f"nonNull assertion of model {short}", CHECKED_BY_QUERY)
        for unique in model.unique_keys:
            record(name, "unique", unique, f"uniqueKey assertion of model {short}", CHECKED_BY_QUERY)
    # what the project, its docs and the person who started the server state about a table's key
    try:
        claims = session.grain_claims()
    except Exception:  # noqa: BLE001 - claims are extra evidence; without them the proof has the facts above
        claims = {}
    for claim in claims.values():
        name = claim.table
        if claim.in_project and claim.table in pipeline.models:
            name = pipeline.models[claim.table].target.key or claim.table
        builder.add(name, keys=[claim.keys], source="claims")
        record(name, "unique", claim.keys, claim.origin, _CLAIM_STATUS.get(claim.kind, NOT_CHECKED))
    # REQUIRED columns of the schema snapshot, for the project's tables
    snapshot = _snapshot(session)
    if snapshot is not None:
        for table, columns in snapshot.tables().items():
            key, _close = session.resolve(table)
            if key is None:
                continue
            name = pipeline.models[key].target.key or key if key in pipeline.models else key
            required = [c.column for c in columns if c.is_nullable is False]
            builder.add(name, [c.column for c in columns], required, source="snapshot")
            for column in required:
                record(name, "not_null", (column,), f"schema snapshot: {table}.{column} is REQUIRED", ENFORCED)
    return ProjectFacts(builder.build(), _owners(builder), origins)


def _snapshot(session: Session):
    try:
        return session.schema_snapshot()
    except Exception:  # noqa: BLE001
        return None


def _owners(builder) -> dict[str, str]:
    """Each spelling of a table that names one table only, as ``_Builder.build`` registers them."""

    names = set(builder.columns) | {n for n, v in builder.not_null.items() if v} | {n for n, v in builder.keys.items() if v} | {n for n, v in builder.foreign_keys.items() if v}
    by_suffix: dict[str, set[str]] = {}
    for name in names:
        parts = name.split(".")
        for start in range(len(parts)):
            by_suffix.setdefault(".".join(parts[start:]), set()).add(name)
    return {spelling: next(iter(owners)) for spelling, owners in by_suffix.items() if len(owners) == 1}


def _quoted(name: str) -> str:
    from sqlglot import exp

    return ".".join(exp.to_identifier(part, quoted=True).sql(dialect="bigquery") for part in name.split("."))


def _check_sql(kind: str, table: str, columns, parent: str = "", parent_columns=()) -> str:
    """The query that counts the rows breaking a declared fact (0 means it holds), like the conditional search's checks.

    A uniqueness check groups NULLs together, as Dataform's ``uniqueKey`` assertion does, so it is the stricter one."""

    cols = ", ".join(_quoted(c) for c in columns)
    if kind == "not_null":
        return f"SELECT COUNT(*) AS violations FROM {_quoted(table)} WHERE {_quoted(columns[0])} IS NULL"
    if kind == "unique":
        return f"SELECT COUNT(*) AS violations FROM (SELECT {cols} FROM {_quoted(table)} GROUP BY {cols} HAVING COUNT(*) > 1) AS duplicated"
    present = " AND ".join(f"c.{_quoted(c)} IS NOT NULL" for c in columns)
    match = " AND ".join(f"p.{_quoted(pc)} = c.{_quoted(c)}" for c, pc in zip(columns, parent_columns))
    return f"SELECT COUNT(*) AS violations FROM {_quoted(table)} AS c WHERE {present} AND NOT EXISTS (SELECT 1 FROM {_quoted(parent)} AS p WHERE {match})"


def _facts(session: Session):
    return lambda: project_facts(session)


# ------------------------------------------------------------------ the tool


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: object) -> str:
    """A prover message without the terminal colour codes a parse error carries."""

    return _ANSI.sub("", str(text))


def _value(value: object) -> object:
    """A value from a counterexample, clipped; it is made-up data, but quoted like any data."""

    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return clip(value, 40)


def keep_sql_whole(answer: Answer, max_bytes: int) -> None:
    """SQL is shown whole or not at all. Where the size bound would clip a query in a list item or a field (its last
    resort, after cutting lists), the query is taken out and its place says which ``max_bytes`` shows it."""

    out = render(answer, max_bytes=max_bytes)
    everything = None
    pairs = [pair for name, listing in answer.lists.items() for pair in zip(listing.items, out.get(name) or [])]
    pairs += [(value, out.get(name)) for name, value in answer.fields.items()]
    for item, sent in pairs:
        if not (isinstance(item, dict) and isinstance(sent, dict)):
            continue
        for key in SQL_KEYS:
            if key in item and sent.get(key) != item[key]:
                if everything is None:
                    everything = min(MAX_BYTES, size(render(answer, max_bytes=MAX_BYTES)) + 100)
                del item[key]
                item[f"{key}_not_shown"] = (f"cut by the size bound; ask again with max_bytes={everything} to see it" if everything < MAX_BYTES
                                            else "longer than an answer holds")


def _search(function: Callable[[float], object], deadline: float) -> tuple[object, str]:
    """``(result, how)`` of one follow-up search given the time left until ``deadline``: ``how`` is ``done``, ``no_time``,
    ``timeout`` (with what was left) or ``crash:Name``."""

    left = deadline - time.monotonic()
    if left < MIN_SEARCH_SECONDS:
        return None, "no_time"
    result, failure = run_capped(lambda: function(left), left)
    if failure == "timeout":
        return None, f"timeout:{left:.0f}"
    return result, failure or "done"


def _searched(how: str, found: bool) -> str:
    if how == "done":
        return "found" if found else "none found"
    if how == "no_time":
        return f"not run: less than {MIN_SEARCH_SECONDS:.0f} s of the time limit was left"
    if how.startswith("timeout:"):
        return f"did not finish in the {how.split(':')[1]} s left of the time limit"
    return f"stopped with an error ({how.split(':')[-1]})"


@tool(
    "compare_queries",
    "Compare two queries",
    "Is my rewrite still the same query? Runs the equivalence prover's fast path on two SELECT queries (written against the "
    "project's tables) and answers equivalent (proven, with its assumptions; a proof that needs the project's declared keys "
    "or NOT NULL columns names each fact it needs and where it was declared), not equivalent (with a small database on "
    "which they differ) or unknown (with the reason). Unknown is not a claim either way. When the answer is not equivalent, "
    "the rest of the time limit looks for conditions that would make the queries equivalent (each with a query that checks "
    "it; never assumed true) and for a predicate they differ only under, and the answer adds a query that compares the two "
    "results on real data. Rows are compared as bags unless respect_row_order is set. Stops at time_limit seconds.",
    {
        "left": {"type": "string", "description": "The original query (Dataform ${ref(\"table\")} is read as that table)."},
        "right": {"type": "string", "description": "The rewritten query."},
        "time_limit": {"type": "integer", "description": f"Seconds the check and its follow-up searches may take together (default {DEFAULT_SECONDS}, from 1 to {MAX_SECONDS})."},
        "respect_row_order": {"type": "boolean", "description": "Also require the same ORDER BY on both (default false: rows are compared as a bag; the follow-up searches then do not run)."},
    },
    required=("left", "right"),
)
def compare_queries(session: Session, args: dict) -> Answer:
    answer = _compare(session, args)
    keep_sql_whole(answer, bytes_wanted(args))
    return answer


def _compare(session: Session, args: dict) -> Answer:
    left, right = args["left"], args["right"]
    for name, sql in (("left", left), ("right", right)):
        if len(sql) > MAX_SQL:
            raise ToolError(f"{name} is too long ({MAX_SQL:,} characters at most)")
    left, left_refs = expand_refs(session, left)
    right, right_refs = expand_refs(session, right)
    refs = list(dict.fromkeys(left_refs + right_refs))
    seconds = args.get("time_limit", DEFAULT_SECONDS)
    if not 1 <= seconds <= MAX_SECONDS:
        raise ToolError(f"time_limit must be from 1 to {MAX_SECONDS} seconds", [f"Pass time_limit between 1 and {MAX_SECONDS}."])
    ordered = bool(args.get("respect_row_order", False))
    project = session._once("compare_facts", _facts(session))
    facts = project.schema
    _warm_prover()

    began = time.monotonic()
    deadline = began + seconds
    plain_ms = max(500, int(seconds * 1000 * 0.8))
    out, failure = run_capped(lambda: prove_pair(left, right, respect_row_order=ordered, facts=facts, timeout_ms=plain_ms), seconds)
    follow = _follow_up(out, left, right, facts, deadline, plain_ms) if out is not None else {}
    took = time.monotonic() - began

    answer = Answer("")
    answer.fields["compared_as"] = "ordered rows (ORDER BY must match)" if ordered else "bags of rows (row order ignored)"
    if refs:
        answer.fields["refs_read_as"] = refs
    answer.counts = {"seconds": round(took, 1), "time_limit": seconds}
    if out is None:
        answer.summary = f"Unknown: the check {'did not finish in %d s' % seconds if failure == 'timeout' else 'failed (' + str(failure).split(':')[-1] + ')'}."
        answer.fields["verdict"] = "unknown"
        answer.unknown("whether the queries are equivalent", "no result within the time cap" if failure == "timeout" else "the prover stopped with an error")
        answer.next(f"Pass a larger time_limit (at most {MAX_SECONDS}) or compare smaller pieces; unknown does not mean different.")
        return answer

    verdict = out["verdict"]
    answer.fields["verdict"] = verdict
    if verdict == "equivalent":
        _equivalent(answer, out, follow, project, left, right)
        return answer
    conditional = out.get("conditional") or follow.get("conditional")
    if verdict == "not_equivalent":
        answer.summary = f"Not equivalent: on a small database the left query returns {out['left_count']} row(s) and the right {out['right_count']}."
        answer.fields["counterexample"] = {
            "tables": {name: [{k: _value(v) for k, v in row.items()} for row in rows] for name, rows in out["tables"].items()},
            "table_rows": out["table_rows"],
            "left_returns": [[_value(v) for v in row] for row in out["left_rows"]],
            "right_returns": [[_value(v) for v in row] for row in out["right_rows"]],
        }
    else:
        answer.summary = "Unknown: the prover could neither prove the queries equivalent nor find a difference."
        reason = _plain(out["reason"])
        answer.unknown("whether the queries are equivalent", clip(reason, 220))
    difference = follow.get("difference")
    if conditional:
        _conditions(answer, conditional, verdict, project, left, right)
    elif difference:
        exact = bool(difference.get("exact"))
        where = difference["sql"]  # SQL: never clipped (see keep_sql_whole); a long one is named, not quoted, in the summary
        named = where if len(where) <= 200 else "the predicate under differs_only_when"
        answer.summary = (f"The same except on rows where {named}: they differ exactly there (verified). " if exact
                          else f"Every difference is on rows where {named} (verified; they may also agree on some of those rows). ") + answer.summary
        answer.fields["differs_only_when"] = {"where": where, "exact": exact, "tables": list(difference.get("tables", []))[:4]}
    if out.get("assumptions") and not conditional:
        answer.add_list("assumptions", [clip(a, 140) for a in dict.fromkeys(out["assumptions"])][:6])
    if follow.get("searched") and not conditional:
        answer.fields["searched"] = follow["searched"]
    _verifier(answer, out.get("verifier_sql") or "")
    if conditional:
        pass  # its next step is to check the conditions
    elif verdict == "not_equivalent":
        answer.next("The rows above are made-up values showing one difference; fix the rewrite where that case differs, then ask again.")
    elif _plain(out["reason"]).startswith("parse error"):
        answer.next("Fix the SQL so that it parses (one SELECT statement, written against the project's tables), then ask again.")
    else:
        answer.next("This does not mean they differ. Simplify the rewrite in smaller steps, or compare the two result sets on real data.")
    if any(str(v).startswith("did not finish") for v in follow.get("searched", {}).values()):
        answer.next(f"A follow-up search ran out of time: ask again with a larger time_limit (at most {MAX_SECONDS}) to let it finish.")
    return answer


def _warm_prover() -> None:
    """Import the prover here, before the clock starts: the time limit is for proving, and a forked check then starts
    with the modules loaded instead of importing them again inside its cap."""

    try:
        from kumo_sql_prover import conditional_equivalence, constraint_dependence, difference_surface, statement_proof  # noqa: F401
    except Exception:  # noqa: BLE001 - the check imports what it needs itself and reports what fails
        pass


def _follow_up(out: dict, left: str, right: str, facts, deadline: float, plain_ms: int) -> dict:
    """The follow-up searches the plain verdict calls for, in what is left of the time limit (see the module docstring)."""

    found: dict = {}
    if out["verdict"] == "equivalent":
        if out.get("proof") == "with_project_facts":
            needed, how = _search(lambda s: find_needed(left, right, facts=facts, timeout_ms=max(300, min(plain_ms, int(s * 400)))), deadline)
            found["needed"], found["needed_how"] = needed, how
        return found
    if not out.get("searchable"):
        return found  # row order, no SMT prover or SQL that does not parse: the searches cannot run either
    searched: dict[str, str] = {}
    if out.get("conditional"):
        searched["conditions"] = "found"
    else:
        conditional, how = _search(
            lambda s: find_conditions(left, right, facts=facts, timeout_ms=max(300, min(plain_ms, int(s * 400))), wall_seconds=max(0.5, s * 0.5)),
            deadline,
        )
        searched["conditions"] = _searched(how, conditional is not None)
        found["conditional"] = conditional
    if searched["conditions"] == "found":
        searched["difference"] = "not run: conditions settle the pair"
    else:
        difference, how = _search(lambda s: find_difference(left, right, timeout_ms=max(300, int(s * 800))), deadline)
        searched["difference"] = _searched(how, difference is not None)
        found["difference"] = difference
    found["searched"] = searched
    return found


def _equivalent(answer: Answer, out: dict, follow: dict, project: ProjectFacts, left: str, right: str) -> None:
    """An ``equivalent`` answer; one that rests on declared facts names the facts the proof needs."""

    answer.fields["proof"] = "unconditional" if out["proof"] == "alone" else "given declared project facts"
    answer.fields["how"] = clip(out["how"], 160)
    assumptions = list(out.get("assumptions") or [])
    if out["proof"] == "alone":
        answer.summary = "Equivalent (proven)."
    else:
        needed, how = follow.get("needed"), follow.get("needed_how", "no_time")
        if how == "done" and needed is not None and needed["status"] == "proven":
            rests = [project.describe(g) for g in needed["needed"]]
            assumptions = list(needed["assumptions"]) + ([project.schema.notes[0]] if rests and project.schema.notes else [])
            if rests:
                listed = "; ".join(f"{item['fact']} ({clip(item['from'], 60)}, {item['status']})" for item in rests)
                answer.summary = clip(f"Equivalent, given {len(rests)} declared fact{'s' if len(rests) > 1 else ''}: safe only while "
                                      f"{'it holds' if len(rests) == 1 else 'they hold'}: {listed}.", 400)
                answer.add_list("rests_on", rests, keep=1)
                notes = {_STATUS_NOTE[item["status"]] for item in rests if item["status"] in _STATUS_NOTE}
                if notes:
                    answer.fields["status_note"] = "; ".join(sorted(notes))
            else:
                answer.summary = "Equivalent (proven, given the project's declared columns; it needs no declared key or NOT NULL fact)."
        else:
            answer.summary = "Equivalent (proven, given the project's declared columns, keys and NOT NULL columns)."
            offered = _offered(project, left, right)
            if offered:
                answer.add_list("may_rest_on", offered, keep=1)
            reason = {"no_time": "the proof used the time limit, so the search did not start"}.get(how) or (
                _searched(how, False) if how != "done" else "the facts could not be narrowed down (the proof did not go through with the queried tables' facts alone)")
            answer.unknown("which declared facts the proof needs", reason + ("; it may rest on any fact under may_rest_on" if offered else ""))
    if assumptions:
        answer.add_list("assumptions", [clip(a, 140) for a in dict.fromkeys(assumptions)][:6])
    if "rests_on" in answer.lists and any(item["status"] in (NOT_CHECKED, CHECKED_BY_USER) for item in answer.lists["rests_on"].items):
        answer.next("Run the check query of each fact not checked by a query: 0 means it holds today. If a fact can stop holding, the rewrite can stop being equivalent.")
    answer.next("Equivalence covers the rows only: confirm column types and names with check_query dry_run=true project=... before replacing the original.")


def _offered(project: ProjectFacts, left: str, right: str) -> list[dict]:
    """Every declared fact on the tables the queries write: what a proof given the project's facts may rest on."""

    from kumo_sql_prover.constraint_dependence import guarantees_of

    try:
        facts = guarantees_of(written_constraints(project.schema.constraints, left, right))
    except Exception:  # noqa: BLE001
        return []
    described = [project.describe(_guarantee(g)) for g in facts]
    return list({item["fact"]: item for item in described}.values())


def _conditions(answer: Answer, conditional: dict, verdict: str, project: ProjectFacts, left: str, right: str) -> None:
    """The conditional verdict: equivalent only if every condition holds. The verdict field keeps the plain verdict.

    The proof also assumed the project's declared facts on the queried tables (a declared fact is never a condition), so
    they are listed with where each came from: they were offered to the proof, not narrowed down to the ones it used."""

    count = len(conditional["conditions"])
    without = "they differ (the database below breaks one)" if verdict == "not_equivalent" else "not proven either way"
    declared = _offered(project, left, right)
    also = f" The proof also assumes {len(declared)} declared fact{'s' if len(declared) > 1 else ''} (assumes_declared)." if declared else ""
    plain = f" {answer.summary}" if verdict == "not_equivalent" else ""
    answer.summary = clip(f"Equivalent only if {conditional['when']} ({'not checked' if count == 1 else f'{count} conditions, none checked'}); "
                          f"without {'it' if count == 1 else 'them'}, {without}.", 400) + also + plain
    answer.fields["if_conditions_hold"] = "equivalent"  # proven under the conditions; ``verdict`` is the answer without them
    items = []
    for condition in conditional["conditions"]:
        item = {"condition": condition["text"], "status": NOT_CHECKED}
        if condition["check_sql"]:
            item["check"] = condition["check_sql"]
        else:  # a condition on how the queries run (in the same statement or session), not on the data
            item["no_check"] = "no query can check it: it is about how the queries are run, not the data"
        items.append(item)
    answer.add_list("conditions", items, keep=1)
    if declared:
        answer.add_list("assumes_declared", declared)
    # the prover's reason repeats the conditions; what it adds is a caveat ("the search for fewer conditions ran out of time")
    caveat = conditional["how"].removeprefix("equivalent whenever: ").removeprefix(conditional["when"]).strip(" ;")
    if caveat:
        answer.fields["prover_note"] = clip(caveat.strip("()"), 160)
    assumptions = list(conditional.get("assumptions") or [])
    if declared:
        from .prover_schema import DECLARED_FACTS_NOTE

        assumptions.append(DECLARED_FACTS_NOTE)
    if assumptions:
        answer.add_list("assumptions", [clip(a, 140) for a in dict.fromkeys(assumptions)][:6])
    answer.next("Run each check on the warehouse (0 violations: it holds today). To rely on it, declare it (assertions: uniqueKey or nonNull) and ask again.")


def _verifier(answer: Answer, sql: str) -> None:
    """The bag-comparison query of ``prove-sql-equivalent --verifier-sql``, as a one-item list.

    It is the first thing the size bound cuts, and the bound then says which ``max_bytes`` shows everything; a query too
    long for any answer is not shown, and the answer says how to write it to a file."""

    if not sql:
        return
    if size(sql) > MAX_BYTES // 2:
        answer.fields["verifier_not_shown"] = (f"{len(sql):,} characters, longer than an answer holds; write it with "
                                               "prove-sql-equivalent LEFT.sql RIGHT.sql --verifier-sql OUT.sql")
        return
    answer.add_list("verifier", [{"sql": sql, "returns": "equivalent = true when both return the same rows (as bags) on the data it runs on: evidence, not a proof"}])
