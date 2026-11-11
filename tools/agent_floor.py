"""The product's own floor: standard analyst questions asked of the agent-facing server, with bounded answers.

``python tools/agent_floor.py [--write-results] [--json]``

About twenty questions an analyst asks about a Dataform project ("what is this table", "where does this column come from",
"what breaks if I drop it", "does one like it exist") are asked of ``python -m kumosql mcp`` in process, on the largest public
Dataform project in the fixtures (``tests/fixtures/bq_corpora/security-analytics``, 106 models). Each question is one *task*:
the calls an agent would make to answer it. For every task the floor records the number of calls, the bytes of every answer,
the latency of each call after the one-time project load, and whether each number is inside its bound.

The floor is deterministic and runs no language model. It fails (``main`` returns 1, the test fails) when

* an answer is larger than its bound (``ANSWER_BOUND``, the server's default of 2,000 bytes),
* a task needs more calls than its limit (``MAX_CALLS``),
* a call is slower than ``LATENCY_BOUND_MS`` after the load (a generous bound; the median is recorded), or
* a tool the task needs answers with an error.

A task that needs a tool this checkout does not have yet is reported as ``waiting`` and does not fail, so the floor can
list the questions for tools that arrive later. The unbounded alternatives are measured on the same project for scale: the
bytes of every ``.sqlx`` file and of the ``pipeline-report`` JSON.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PROJECT = ROOT / "tests" / "fixtures" / "bq_corpora" / "security-analytics"
ANSWER_BOUND = 2000  # bytes of one answer; the server's default bound
MAX_CALLS = 3  # calls per task
LATENCY_BOUND_MS = 1000.0  # one call after the load; a regression guard, not a benchmark of the machine
RESULT_NAME = "agent-floor"


class Asker:
    """Calls the server's tools in process and records each call."""

    def __init__(self, server) -> None:
        self.server = server
        self.tools = {tool["name"] for tool in server.handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})["result"]["tools"]}
        self.calls: list[dict] = []

    def __call__(self, name: str, **arguments) -> dict:
        started = time.perf_counter()
        result = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]
        milliseconds = (time.perf_counter() - started) * 1000
        text = result["content"][0]["text"]
        self.calls.append({"tool": name, "bytes": len(text.encode("utf-8")), "ms": round(milliseconds, 1), "error": bool(result["isError"])})
        return json.loads(text)


def load_server():
    from kumosql import mcp_server
    from kumosql.mcp_session import Session
    from kumosql.pipeline_loading import load_sqlx_project

    started = time.perf_counter()
    session = Session(load_sqlx_project(PROJECT), root=PROJECT)
    session.records()
    return mcp_server.AgentServer(session), time.perf_counter() - started


def choose(server) -> dict[str, object]:
    """The tables and columns the questions are about, picked by fixed rules from the project, so the same questions are asked every run."""

    ask = Asker(server)
    cards = {}
    for key in sorted(server.session.pipeline().models):
        cards[key] = ask("describe_table", table=key)
    names = sorted(cards)

    def count(key, field):
        return cards[key]["counts"].get(field, 0)

    def column_names(key):
        return [line.split(" = ", 1)[0] for line in cards[key].get("columns", [])]

    most_read = max(names, key=lambda k: (count(k, "read_by"), k))
    mid_read = max((k for k in names if 0 < count(k, "read_by") < count(most_read, "read_by")), key=lambda k: (count(k, "read_by"), k), default=most_read)
    leaf = next(k for k in names if count(k, "read_by") == 0 and cards[k]["kind"] in ("table", "view", "incremental"))
    incremental = next((k for k in names if cards[k]["kind"] == "incremental" and column_names(k)), most_read)
    widest = max(names, key=lambda k: (count(k, "columns"), k))
    with_nested = next((k for k in names if count(k, "meaning_unknown") and column_names(k)), most_read)
    reads_most = max(names, key=lambda k: (count(k, "reads"), k))
    from kumosql.mcp_expression import expression_index

    index = expression_index(server.session)
    copied = max(
        (group for group in index.exact.values() if group[0].text and "${" not in group[0].text),
        key=lambda group: (len({o.model for o in group}), len(group[0].text), group[0].text),
    )
    widest_columns = column_names(widest)
    return {
        "copied_expression": copied[0].text, "link_columns": [widest_columns[0], widest_columns[1]],
        "most_read": most_read, "mid_read": mid_read, "leaf": leaf, "incremental": incremental, "widest": widest,
        "nested": with_nested, "reads_most": reads_most,
        "most_read_column": column_names(most_read)[0], "mid_read_column": column_names(mid_read)[0], "leaf_column": column_names(leaf)[0],
        "widest_column": column_names(widest)[-1], "nested_column": column_names(with_nested)[-1], "incremental_column": column_names(incremental)[0],
        "second_column": (column_names(most_read) + column_names(most_read))[1],
        "draft": f"SELECT {column_names(most_read)[0]} FROM `{most_read}`",
    }


def tasks(c: dict) -> list[tuple[str, tuple[str, ...], callable]]:
    """(question, tools needed, steps): ``steps(ask)`` makes the calls an agent makes to answer the question."""

    def table(key):
        return key

    return [
        ("What is the most-read table?", ("describe_table",), lambda ask: ask("describe_table", table=c["most_read"])),
        ("What is a table nothing reads?", ("describe_table",), lambda ask: ask("describe_table", table=c["leaf"])),
        ("What is an incremental table?", ("describe_table",), lambda ask: ask("describe_table", table=c["incremental"])),
        ("What is the widest table?", ("describe_table",), lambda ask: ask("describe_table", table=c["widest"])),
        ("What is the table that reads the most others?", ("describe_table",), lambda ask: ask("describe_table", table=c["reads_most"])),
        ("What does a table with unknown column meanings say about them?", ("describe_table",), lambda ask: ask("describe_table", table=c["nested"])),
        ("Where does a column of the most-read table come from?", ("trace_column",), lambda ask: ask("trace_column", table=c["most_read"], column=c["most_read_column"])),
        ("Where does a column of a middle table come from?", ("trace_column",), lambda ask: ask("trace_column", table=c["mid_read"], column=c["mid_read_column"])),
        ("Where does the last column of the widest table come from?", ("trace_column",), lambda ask: ask("trace_column", table=c["widest"], column=c["widest_column"])),
        ("Where does a column that reads a nested field come from?", ("trace_column",), lambda ask: ask("trace_column", table=c["nested"], column=c["nested_column"])),
        ("Where does a column of an incremental table come from?", ("trace_column",), lambda ask: ask("trace_column", table=c["incremental"], column=c["incremental_column"])),
        ("What breaks if I drop a column of the most-read table?", ("impact",), lambda ask: ask("impact", table=c["most_read"], column=c["most_read_column"], change="drop_column")),
        ("What breaks if I rename a column of a middle table?", ("impact",), lambda ask: ask("impact", table=c["mid_read"], column=c["mid_read_column"], change="rename_column")),
        ("What breaks if I change how a column is computed?", ("impact",), lambda ask: ask("impact", table=c["mid_read"], column=c["mid_read_column"], change="change_expression")),
        ("What breaks if I drop the most-read table?", ("impact",), lambda ask: ask("impact", table=c["most_read"], change="drop_table")),
        ("Is a column nothing reads used anywhere?", ("impact",), lambda ask: ask("impact", table=c["leaf"], column=c["leaf_column"])),
        ("Does a table like the most-read one already exist?", ("find_existing",), lambda ask: ask("find_existing", table=c["most_read"])),
        ("Does a table for this draft query already exist?", ("find_existing",), lambda ask: ask("find_existing", sql=c["draft"])),
        ("Is a computation that appears in several models copied, and do the copies differ?", ("find_existing",), lambda ask: ask("find_existing", expression=c["copied_expression"])),
        ("Is there a table or view that carries two given columns together?", ("find_tables",), lambda ask: ask("find_tables", has_columns=c["link_columns"])),
        ("Is there a table that already holds the most-read table's subject?", ("find_tables",), lambda ask: ask("find_tables", need=c["most_read"].replace("_", " "))),
        ("Is there a table with a given column?", ("find_tables",), lambda ask: ask("find_tables", need=c["most_read"].replace("_", " "), column=c["most_read_column"])),
        ("What is in this project?", ("list_tables",), lambda ask: ask("list_tables")),
        ("Which tables does nothing read?", ("list_tables",), lambda ask: ask("list_tables", max_readers=0, kind="view")),
        ("Which tables read two or more other models, and which sit deepest?", ("list_tables",),
         lambda ask: (ask("list_tables", min_reads=2, sort="reads"), ask("list_tables", sort="depth", max_bytes=1000))),
        ("Does the repository itself have problems (a name declared and built, a disabled model still read)?", ("project_checks",), lambda ask: ask("project_checks")),
        ("Can I plan a profile of a table without touching BigQuery?", ("profile_table",), lambda ask: ask("profile_table", table=c["most_read"])),
        ("Is my draft query free of certain problems before I run it?", ("check_query",), lambda ask: ask("check_query", sql=c["draft"])),
        ("Is my SELECT * from a wide table free of certain problems?", ("check_query",), lambda ask: ask("check_query", sql=f"SELECT * FROM `{c['widest']}`")),
        ("Does my draft use a word BigQuery reserves as an alias?", ("check_query",),
         lambda ask: ask("check_query", sql=f"SELECT {c['most_read_column']} AS new FROM `{c['most_read']}`")),
        ("Is my rewrite of a draft query equivalent to the original?", ("compare_queries",),
         lambda ask: ask("compare_queries", left=c["draft"], right=f"SELECT * FROM (SELECT {c['draft'][len('SELECT '):]}) AS t")),
        ("Can my messy draft be tidied, and what exactly changes?", ("clean_query",),
         lambda ask: ask("clean_query", sql="WITH a AS (SELECT * FROM t WHERE 1=1 AND x > 0), unused AS (SELECT 1) SELECT (a.x), a.y FROM a WHERE (TRUE)")),
        ("Can the most-read table's own file be tidied?", ("clean_query",), lambda ask: ask("clean_query", table=c["most_read"])),
        ("Can I delete a table nothing reads? (describe it, then ask what breaks)", ("describe_table", "impact"),
         lambda ask: (ask("describe_table", table=c["leaf"]), ask("impact", table=c["leaf"], change="drop_table"))),
        ("Orient me in an unfamiliar table: what is it, where does a column come from, what reads it?", ("describe_table", "trace_column", "impact"),
         lambda ask: (ask("describe_table", table=c["mid_read"]), ask("trace_column", table=c["mid_read"], column=c["mid_read_column"]), ask("impact", table=c["mid_read"]))),
    ]


def unbounded_alternatives() -> dict[str, int]:
    """What the same project costs without the server: every .sqlx file, and the pipeline-report JSON."""

    from kumosql.pipeline_loading import load_sqlx_project

    sqlx = sum(path.stat().st_size for path in PROJECT.rglob("*.sqlx"))
    pipeline = load_sqlx_project(PROJECT)
    report = len(json.dumps(pipeline.report(), default=str).encode("utf-8"))
    return {"all_sqlx_files_bytes": sqlx, "pipeline_report_json_bytes": report}


def run() -> dict:
    server, load_seconds = load_server()
    chosen = choose(server)
    rows = []
    for question, needed, steps in tasks(chosen):
        ask = Asker(server)
        if not set(needed) <= ask.tools:
            rows.append({"question": question, "status": "waiting", "needs": sorted(set(needed) - ask.tools)})
            continue
        steps(ask)
        calls = ask.calls
        problems = []
        if len(calls) > MAX_CALLS:
            problems.append(f"{len(calls)} calls, limit {MAX_CALLS}")
        problems += [f"{call['tool']} answered {call['bytes']} bytes, bound {ANSWER_BOUND}" for call in calls if call["bytes"] > ANSWER_BOUND]
        problems += [f"{call['tool']} took {call['ms']:.0f} ms, bound {LATENCY_BOUND_MS:.0f}" for call in calls if call["ms"] > LATENCY_BOUND_MS]
        problems += [f"{call['tool']} answered with an error" for call in calls if call["error"]]
        rows.append({
            "question": question, "status": "ok" if not problems else "FAILED", "calls": len(calls), "tools": [c["tool"] for c in calls],
            "bytes": sum(c["bytes"] for c in calls), "largest_answer": max(c["bytes"] for c in calls),
            "latency_ms": round(sum(c["ms"] for c in calls), 1), "problems": problems,
        })
    measured = [r for r in rows if r["status"] != "waiting"]
    summary = {
        "questions": len(rows), "measured": len(measured), "waiting": len(rows) - len(measured),
        "within_bounds": sum(r["status"] == "ok" for r in measured),
        "largest_answer_bytes": max((r["largest_answer"] for r in measured), default=0),
        "median_task_bytes": int(statistics.median(r["bytes"] for r in measured)) if measured else 0,
        "most_calls": max((r["calls"] for r in measured), default=0),
        "median_latency_ms": round(statistics.median(r["latency_ms"] for r in measured), 1) if measured else 0.0,
        "load_seconds": round(load_seconds, 1),
        "bounds": {"answer_bytes": ANSWER_BOUND, "calls": MAX_CALLS, "latency_ms": LATENCY_BOUND_MS},
        **unbounded_alternatives(),
    }
    return {"summary": summary, "tasks": rows}


def results_row(result: dict) -> dict:
    s = result["summary"]
    return {
        "suite": "Agent-facing server: analyst questions",
        "order": 900,
        "size": s["measured"],
        "score": f"{s['within_bounds']}/{s['measured']} within bounds, largest answer {s['largest_answer_bytes']:,} B",
        "metric": (
            f"Standard analyst questions asked of the agent-facing server on the {PROJECT.name} project (106 models), each inside "
            f"{ANSWER_BOUND:,} bytes per answer, {MAX_CALLS} calls per task and {LATENCY_BOUND_MS:.0f} ms per call after the load. "
            f"Median task {s['median_task_bytes']:,} B in total, against {s['all_sqlx_files_bytes']:,} B for every .sqlx file and "
            f"{s['pipeline_report_json_bytes']:,} B for the pipeline-report JSON."
        ),
        "evidence": "executed",
        "correctness": "Measures size, calls and latency, not whether an answer is right; the correctness tests are in tests/test_mcp_server.py. An answer that cannot decide says so (unknown with a reason) and still counts as inside its bound.",
        "coverage": {},
        "held_out": "none",
        "docs": "docs/evals/agent-floor.md",
        "command": "python tools/agent_floor.py --write-results",
        "date": __import__("datetime").date.today().isoformat(),
        "caveats": "One public project with no source schemas and no job history. In-process calls (no stdio transfer time). No language model is involved, so the turn and token counts of a real agent are not measured here (see docs/field-trial.md). The BigQuery dry-run and run paths of check_query and profile_table are not part of this floor. Questions for a tool the server does not have are listed as waiting, not scored.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write-results", action="store_true", help="write benchmarks/results/agent-floor.json and regenerate the README scoreboard")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args(argv)
    from bench_common import quiet, write_results

    quiet()
    result = run()
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        s = result["summary"]
        print(f"agent floor: {s['within_bounds']}/{s['measured']} within bounds ({s['waiting']} waiting for a tool), largest answer {s['largest_answer_bytes']} B, median task {s['median_task_bytes']} B, most calls {s['most_calls']}, median latency {s['median_latency_ms']} ms, load {s['load_seconds']} s")
        for row in result["tasks"]:
            print(f"  {row['status']:8} {row.get('calls', '-')!s:>2} calls {row.get('bytes', '-')!s:>6} B  {row['question']}" + "".join(f"\n           {p}" for p in row.get("problems", [])))
    if args.write_results:
        write_results(RESULT_NAME, results_row(result))
    return 0 if result["summary"]["within_bounds"] == result["summary"]["measured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
