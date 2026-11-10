"""Re-check every pair an eval counts as proven with a much heavier executed search.

The evals re-run each proof on a few dozen to a thousand random databases. This tool proves each pair
exactly as its eval does and, for every proven pair, runs ``tools/recheck/engine.py``: exhaustive tiny
databases, edge databases (empty tables, all-NULL rows, doubled rows) and thousands of random ones with
NULL-heavy and duplicate-heavy data, ties, boundary numbers and literal neighbours, all respecting the
eval's declared keys, NOT NULL columns and foreign keys. A difference counts only if DuckDB's
unoptimized plan agrees and neither query depends on row order.

    python tools/proof_recheck.py --list
    python tools/proof_recheck.py qed-calcite --jobs 4 --out proof-recheck
    python tools/proof_recheck.py qed-calcite --pairs testAggregateMerge --budget 20000
    python tools/proof_recheck.py qed-calcite --since old-run --out new-run    # only what the old run did not settle

Each eval writes ``<out>/<eval>.jsonl``, one record per pair (``verdict`` is ``survived``, ``differs``,
``masked``, ``not-proven``, ``unrunnable``, ``timeout`` or ``search-error``); a rerun skips the pairs already
written unless ``--fresh``. ``masked`` is a pair whose results agree with DuckDB's optimizer on and
differ with it off: one plan is wrong, so the pair needs a reading. Adapters live in ``tools/recheck/*.py`` (an ``ADAPTERS`` dict per module).
"""

from __future__ import annotations

import argparse
from collections import Counter
import importlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))


def adapters() -> dict:
    found = {}
    for path in sorted((TOOLS / "recheck").glob("*.py")):
        if path.stem in ("__init__", "engine"):
            continue
        try:
            module = importlib.import_module(f"recheck.{path.stem}")
        except Exception as error:  # one broken adapter must not stop the others
            print(f"warning: tools/recheck/{path.name} does not import: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        found.update(getattr(module, "ADAPTERS", {}))
    return found


_ADAPTERS: dict | None = None


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout


def _work(job: tuple) -> dict:
    global _ADAPTERS
    name, item, options = job
    if _ADAPTERS is None:
        os.environ.setdefault("KUMOSQL_TIMING", "0")
        logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
        _ADAPTERS = adapters()
    from recheck.engine import recheck

    adapter = _ADAPTERS[name]
    start = time.time()
    signal.signal(signal.SIGALRM, _alarm)
    # Repeating timer: an exception raised inside a destructor (z3's __del__) is swallowed, so one alarm can be lost.
    signal.setitimer(signal.ITIMER_REAL, int(options["seconds"] * 2 + options["prove_seconds"]), 5)
    try:
        case = adapter.case(item)
        if case is None:
            return {"eval": name, "pair": item["pair"], "verdict": "not-proven", "seconds": round(time.time() - start, 2)}
        prove_seconds = time.time() - start
        record = recheck(case, budget=options["budget"], seconds=options["seconds"], seed=options["seed"], exhaustive_cap=options["exhaustive_cap"])
        record["prove_seconds"] = round(prove_seconds, 2)
        record["left"], record["right"] = case.left, case.right
        if case.source != ("", ""):
            record["source"] = list(case.source)
        if case.meta:
            record["meta"] = case.meta
        return record
    except _Timeout:
        return {"eval": name, "pair": item["pair"], "verdict": "timeout", "seconds": round(time.time() - start, 2)}
    except Exception as error:
        return {"eval": name, "pair": item["pair"], "verdict": "search-error", "error": f"{type(error).__name__}: {error}"[:400]}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evals", nargs="*", help="eval names (see --list)")
    parser.add_argument("--list", action="store_true", help="list the evals and stop")
    parser.add_argument("--out", default="proof-recheck", help="folder for <eval>.jsonl (default ./proof-recheck)")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--budget", type=int, default=3000, help="databases per proven pair")
    parser.add_argument("--seconds", type=float, default=180.0, help="search seconds per proven pair")
    parser.add_argument("--prove-seconds", type=float, default=300.0, help="extra seconds allowed for proving")
    parser.add_argument("--exhaustive-cap", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--pairs", default="", help="comma-separated pair names to run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--every", type=int, default=1, help="run every n-th pair")
    parser.add_argument("--fresh", action="store_true", help="ignore records already written")
    parser.add_argument(
        "--since",
        default="",
        help="folder of an earlier run: skip pairs it already re-checked (survived or differs) and run only the rest "
        "(pairs it did not prove then, timeouts, unrunnable ones and new pairs)",
    )
    args = parser.parse_args(argv)

    registry = adapters()
    if args.list:
        for name in sorted(registry):
            print(name)
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    options = {"budget": args.budget, "seconds": args.seconds, "seed": args.seed, "exhaustive_cap": args.exhaustive_cap, "prove_seconds": args.prove_seconds}
    status = 0
    for name in args.evals:
        adapter = registry[name]
        items = adapter.items()[:: args.every]
        if args.pairs:
            wanted = set(args.pairs.split(","))
            items = [i for i in items if i["pair"] in wanted]
        if args.limit:
            items = items[: args.limit]
        path = out / f"{name}.jsonl"
        done = set()
        if path.exists() and not args.fresh and not args.pairs:
            done = {json.loads(line)["pair"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
        if args.since:
            earlier = Path(args.since) / f"{name}.jsonl"
            if earlier.exists():
                done |= {
                    record["pair"]
                    for record in map(json.loads, filter(str.strip, earlier.read_text(encoding="utf-8").splitlines()))
                    if record["verdict"] in ("survived", "differs")
                }
        todo = [i for i in items if i["pair"] not in done]
        print(f"{name}: {len(items)} pairs, {len(todo)} to run", flush=True)
        counts: Counter = Counter()
        start = time.time()
        mode = "w" if (args.fresh and not args.pairs) else "a"
        with path.open(mode, encoding="utf-8") as sink, multiprocessing.get_context("fork").Pool(args.jobs, maxtasksperchild=40) as pool:
            for number, record in enumerate(pool.imap_unordered(_work, [(name, item, options) for item in todo]), 1):
                sink.write(json.dumps(record, default=str) + "\n")
                sink.flush()
                counts[record["verdict"]] += 1
                if record["verdict"] in ("differs", "masked"):
                    status = 1
                    print(f"  {record['verdict'].upper()} {record['pair']}", flush=True)
                if number % 25 == 0:
                    print(f"  {number}/{len(todo)} {dict(counts)} {time.time() - start:.0f}s", flush=True)
        print(f"{name}: {dict(counts)} in {time.time() - start:.0f}s", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
