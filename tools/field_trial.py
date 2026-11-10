"""Field trial harness (roadmap step 7): analyst tasks done with and without the agent-facing server.

This is a periodic *report*, never a gate: an agent is in the loop, so nothing here runs in the test suite beyond the
unit tests of the harness itself. The harness has four parts.

``tasks``    the analyst tasks on the public Dataform project in the fixtures, each with a prompt and an expected answer.
``relay``    holds the agent-facing server in memory behind a unix socket, so each ``ask`` is one warm call.
``ask``      one tool call to that relay (what the "with the server" agent runs from a shell).
``score``    compares an agent's answer with the expected one (exact set match, else precision and recall).

The expected answers of the model-graph tasks come from an independent reading of the ``ref("...")`` calls in the
``.sqlx`` files, not from KumoSQL. The expected answers of the column tasks come from KumoSQL's own analysis (said so in
the task), so they measure agreement with it, not independent truth.

    python tools/field_trial.py tasks [--json]
    python tools/field_trial.py relay /tmp/ft.sock &
    python tools/field_trial.py ask /tmp/ft.sock describe_table table=csa_5_01_summary_daily
    python tools/field_trial.py score T01 '["a", "b"]'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
PROJECT = ROOT / "tests" / "fixtures" / "bq_corpora" / "security-analytics"
SOURCE = "_AllLogs"


# ------------------------------------------------------------------ the project, read without KumoSQL


def read_graph(project: Path = PROJECT) -> dict[str, dict]:
    """``{model: {"file": relative path, "reads": {names}, "unique_key": [...] | None}}`` from the .sqlx text alone."""

    graph = {}
    for path in sorted((project / "definitions").rglob("*.sqlx")):
        text = path.read_text(encoding="utf-8")
        config = text.split("config", 1)[1].split("\n}", 1)[0] if "config" in text else ""
        named = re.search(r'name:\s*"([^"]+)"', config)
        name = named.group(1) if named else path.stem
        key = re.search(r"uniqueKey:\s*\[([^\]]*)\]", config)
        graph[name] = {
            "file": str(path.relative_to(project)),
            "reads": set(re.findall(r'ref\("([^"]+)"\)', text)),
            "unique_key": re.findall(r'"([^"]+)"', key.group(1)) if key else None,
        }
    return graph


def upstream(graph: dict, name: str) -> set[str]:
    seen, pending = set(), list(graph[name]["reads"])
    while pending:
        item = pending.pop()
        if item not in seen:
            seen.add(item)
            pending.extend(graph.get(item, {}).get("reads", ()))
    return seen


def downstream(graph: dict, name: str) -> set[str]:
    seen, pending = set(), [name]
    while pending:
        item = pending.pop()
        for model, info in graph.items():
            if item in info["reads"] and model not in seen:
                seen.add(model)
                pending.append(model)
    return seen


# ------------------------------------------------------------------ the tasks


def build_tasks() -> list[dict]:
    graph = read_graph()
    readers_of = lambda x: sorted(m for m, i in graph.items() if x in i["reads"])  # noqa: E731
    models = sorted(graph)
    read_by = {m: readers_of(m) for m in models}
    popular = max((m for m in models if m != SOURCE), key=lambda m: (len(read_by[m]), m))
    chain = max(models, key=lambda m: (len(upstream(graph, m) - {SOURCE}), m))
    two = sorted(m for m, i in graph.items() if len(i["reads"] & (set(models) - {SOURCE})) >= 2)
    keyed = next(m for m in models if graph[m]["unique_key"] and len(graph[m]["unique_key"]) >= 3)
    alerts_reading_models = sorted(m for m in models if m.endswith("_alert") and graph[m]["reads"] & (set(models) - {SOURCE}))
    return [
        {"id": "T01", "kind": "graph", "prompt": f"Which models read the model `{popular}` directly? Answer with a JSON list of model names.",
         "expected": read_by[popular], "answer_type": "set"},
        {"id": "T02", "kind": "graph", "prompt": f"Which models does `{chain}` depend on, directly or through other models, other than the raw log source `{SOURCE}`? Answer with a JSON list of model names.",
         "expected": sorted(upstream(graph, chain) - {SOURCE}), "answer_type": "set"},
        {"id": "T03", "kind": "graph", "prompt": f"How many models read `{SOURCE}` directly? Answer with a single integer.",
         "expected": len(read_by[SOURCE]), "answer_type": "number"},
        {"id": "T04", "kind": "graph", "prompt": "Which models read two or more other models of the project (not counting the raw log source)? Answer with a JSON list of model names.",
         "expected": two, "answer_type": "set"},
        {"id": "T05", "kind": "graph", "prompt": "How many models does no other model read? Answer with a single integer.",
         "expected": sum(1 for m in models if m != SOURCE and not read_by[m]), "answer_type": "number"},
        {"id": "T06", "kind": "graph", "prompt": f"If the model `{popular}` were deleted, which models would stop working (everything downstream of it, directly or indirectly)? Answer with a JSON list of model names.",
         "expected": sorted(downstream(graph, popular)), "answer_type": "set"},
        {"id": "T07", "kind": "graph", "prompt": f"What columns identify a row of `{keyed}` (its declared unique key)? Answer with a JSON list of column names.",
         "expected": graph[keyed]["unique_key"], "answer_type": "set"},
        {"id": "T08", "kind": "graph", "prompt": "Among the models whose name ends in `_alert`, which ones read at least one other model of the project (not only the raw log source)? Answer with a JSON list of model names.",
         "expected": alerts_reading_models, "answer_type": "set"},
        {"id": "T09", "kind": "column", "reference": "KumoSQL's own analysis (agreement with it, not independent truth)",
         "prompt": f"Which models would change values or break if the first output column of `{popular}` were dropped? Answer with a JSON list of model names.",
         "expected": None, "answer_type": "set"},
        {"id": "T10", "kind": "column", "reference": "KumoSQL's own analysis (agreement with it, not independent truth)",
         "prompt": f"Which source columns does the last output column of `{chain}` ultimately come from? Answer with a JSON list of `table.column` names.",
         "expected": None, "answer_type": "set"},
    ]


def fill_column_answers(tasks: list[dict]) -> None:
    """Fill the two column tasks from the server (the reference named in the task). Needs the project analysed."""

    from kumosql import mcp_server
    from kumosql.mcp_session import Session
    from kumosql.pipeline_loading import load_sqlx_project

    session = Session(load_sqlx_project(PROJECT), root=PROJECT)
    session.records()
    server = mcp_server.AgentServer(session)

    def ask(name, **arguments):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]
        return json.loads(result["content"][0]["text"])

    by_id = {t["id"]: t for t in tasks}
    popular = re.search(r"`([^`]+)`", by_id["T09"]["prompt"]).group(1)
    first = ask("describe_table", table=popular)["columns"][0].split(" = ", 1)[0]
    impact = ask("impact", table=popular, column=first, change="drop_column", max_bytes=20000)
    names = {item["model"] for item in impact.get("affected", [])}
    by_id["T09"]["expected"] = sorted(n.rsplit(".", 1)[-1] for n in names if n)
    chain = re.search(r"`([^`]+)`", by_id["T10"]["prompt"]).group(1)
    card = ask("describe_table", table=chain, max_bytes=20000)
    last = card["columns"][-1].split(" = ", 1)[0]
    trace = ask("trace_column", table=chain, column=last, max_bytes=20000)
    by_id["T10"]["expected"] = sorted(trace.get("ultimate_sources", trace.get("sources", [])))


# ------------------------------------------------------------------ scoring


def score(task: dict, answer) -> dict:
    """``{"exact": bool, "precision": float, "recall": float}`` for an agent's answer to a task."""

    expected = task["expected"]
    if task["answer_type"] == "number":
        try:
            ok = int(str(answer).strip()) == expected
        except ValueError:
            ok = False
        return {"exact": ok, "precision": float(ok), "recall": float(ok)}
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except ValueError:
            answer = []
    got, want = {str(x).strip().lower() for x in answer}, {str(x).strip().lower() for x in expected}
    hit = len(got & want)
    return {"exact": got == want, "precision": hit / len(got) if got else float(not want), "recall": hit / len(want) if want else 1.0}


# ------------------------------------------------------------------ the relay and the ask


def serve(path: str, project: Path) -> None:
    from kumosql import mcp_server
    from kumosql.mcp_session import Session
    from kumosql.pipeline_loading import load_sqlx_project

    session = Session(load_sqlx_project(project), root=project)
    session.records()
    server = mcp_server.AgentServer(session)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                reply = server.handle(json.loads(line))
                self.wfile.write((json.dumps(reply) + "\n").encode("utf-8"))
                self.wfile.flush()

    if os.path.exists(path):
        os.unlink(path)
    with socketserver.ThreadingUnixStreamServer(path, Handler) as relay:
        print(f"relay ready on {path}", flush=True)
        relay.serve_forever()


def ask_once(path: str, tool: str, arguments: dict) -> str:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(path)
        client.sendall((json.dumps(message) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            data += client.recv(65536)
    return json.loads(data)["result"]["content"][0]["text"]


def parse_arguments(items: list[str]) -> dict:
    out = {}
    for item in items:
        key, _, value = item.partition("=")
        out[key] = int(value) if value.isdigit() else {"true": True, "false": False}.get(value, value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("tasks")
    t.add_argument("--json", action="store_true")
    r = sub.add_parser("relay")
    r.add_argument("socket")
    r.add_argument("--project", default=str(PROJECT))
    a = sub.add_parser("ask")
    a.add_argument("socket")
    a.add_argument("tool")
    a.add_argument("arguments", nargs="*")
    s = sub.add_parser("score")
    s.add_argument("task")
    s.add_argument("answer")
    args = parser.parse_args(argv)
    if args.command == "tasks":
        tasks = build_tasks()
        fill_column_answers(tasks)
        for task in tasks:
            print(json.dumps(task) if args.json else f"{task['id']} [{task['kind']}] {task['prompt']}\n      expected: {task['expected']}")
    elif args.command == "relay":
        serve(args.socket, Path(args.project))
    elif args.command == "ask":
        print(ask_once(args.socket, args.tool, parse_arguments(args.arguments)))
    else:
        tasks = build_tasks()
        fill_column_answers(tasks)
        task = next(t for t in tasks if t["id"] == args.task)
        print(json.dumps(score(task, args.answer)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
