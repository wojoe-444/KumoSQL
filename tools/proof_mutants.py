"""Near-miss probe: does the prover still say "equal" after one side of a pair gets a small mistake?

A rule written with an eval's failing pairs in view proves those pairs. The risk is that it also proves
their neighbours: the same shapes with ``<`` for ``<=``, an inner join for a left join, a missing
``DISTINCT``, one grouping key fewer. Those neighbours are almost never equivalent, so each one the
prover accepts is either a mutant that happens to be equivalent or a false proof.

For every pair of an eval (held-out pairs are never read) this tool builds single-site mutants with
``kumosql.query_mutants`` and proves each mutated pair exactly as the eval proves its own pairs:

* ``right``: the left query against a mutant of the right query (the pair's own rewrite, one mistake in);
* ``left``:  a mutant of the left query against the right query;
* ``self``:  each query against its own mutant.

Every mutated pair the prover accepts goes to the heavy executed search of the proof re-check
(``tools/recheck/engine.py``). A database that separates the two queries makes the record ``differs``:
a false proof, with the witness. ``survived`` records are the equivalent mutants plus whatever the
search could not separate; ``--show`` prints them for reading.

    python tools/proof_mutants.py --list
    python tools/proof_mutants.py qed-calcite --jobs 3 --out proof-mutants
    python tools/proof_mutants.py qed-calcite --pairs testAggregateMerge --per-pair 0 --budget 5000
    python tools/proof_mutants.py --show proof-mutants/qed-calcite.jsonl

Each eval writes ``<out>/<eval>.jsonl``: one ``summary`` record per pair and one record per accepted
mutant. A rerun skips the pairs already written unless ``--fresh``.
"""

from __future__ import annotations

import argparse
from collections import Counter
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
sys.path.insert(0, str(TOOLS.parent / "src"))

MODES = ("right", "left", "self")


class _Timeout(Exception):
    pass


def _alarm(_signum, _frame):
    raise _Timeout


def supported(adapter) -> bool:
    """An adapter can be probed when its items carry both queries as text and ``case`` proves from the item."""

    return bool(getattr(adapter, "mutable", False))


def registry() -> dict:
    import proof_recheck

    return {name: adapter for name, adapter in proof_recheck.adapters().items() if supported(adapter)}


def spread(mutants: list, most: int) -> list:
    """At most ``most`` mutants, taken in turn from each operator so every kind of mistake is tried."""

    if not most or len(mutants) <= most:
        return list(mutants)
    by_operator: dict[str, list] = {}
    for mutant in mutants:
        by_operator.setdefault(mutant.operator, []).append(mutant)
    queues = list(by_operator.values())
    out = []
    while len(out) < most:
        for queue in queues:
            if queue and len(out) < most:
                # the middle of the query first: the ends are usually the projection and the outer filter
                out.append(queue.pop(len(queue) // 2))
        queues = [q for q in queues if q]
    return out


def mutated_items(item: dict, dialect: str, modes: tuple[str, ...], per_pair: int) -> list[tuple[str, dict]]:
    """``(label, item)`` for every mutated pair of ``item``; the label is ``<mode>:<operator>@<site>``."""

    from kumosql.query_mutants import mutate

    def mutants(sql: str) -> list:
        try:
            return mutate(sql, dialect=dialect)
        except Exception:  # a query sqlglot cannot read or print has no mutants
            return []

    plans = []
    if "right" in modes:
        plans.append(("right", item["left"], mutants(item["right"]), False))
    if "left" in modes:
        plans.append(("left", item["right"], mutants(item["left"]), True))
    if "self" in modes:
        plans.append(("self-left", item["left"], mutants(item["left"]), False))
        if item["right"] != item["left"]:
            plans.append(("self-right", item["right"], mutants(item["right"]), False))
    share = max(1, per_pair // max(1, len(plans))) if per_pair else 0
    out = []
    for mode, fixed, found, mutant_first in plans:
        for mutant in spread(found, share):
            left, right = (mutant.sql, fixed) if mutant_first else (fixed, mutant.sql)
            out.append((f"{mode}:{mutant.operator}@{mutant.site}", dict(item, left=left, right=right)))
    return out


_ADAPTERS: dict | None = None


def _work(job: tuple) -> list[dict]:
    global _ADAPTERS
    name, item, options = job
    if _ADAPTERS is None:
        os.environ.setdefault("KUMOSQL_TIMING", "0")
        logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
        _ADAPTERS = registry()
    from recheck.engine import recheck

    adapter = _ADAPTERS[name]
    start = time.time()
    counts: Counter = Counter()
    records = []
    signal.signal(signal.SIGALRM, _alarm)
    for label, mutated in mutated_items(item, adapter.mutable, options["modes"], options["per_pair"]):
        counts["mutants"] += 1
        # Repeating timer: an exception raised inside a destructor (z3's __del__) is swallowed, so one alarm can be lost.
        signal.setitimer(signal.ITIMER_REAL, int(options["prove_seconds"]), 5)
        try:
            case = adapter.case(mutated)
        except _Timeout:
            counts["prove-timeout"] += 1
            continue
        except Exception:  # a crash on a mutant is a failure to prove
            counts["prove-error"] += 1
            continue
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        if case is None:
            counts["not-proven"] += 1
            continue
        signal.setitimer(signal.ITIMER_REAL, int(options["seconds"] * 2 + 30), 5)
        try:
            record = recheck(case, budget=options["budget"], seconds=options["seconds"], seed=options["seed"], exhaustive_cap=options["exhaustive_cap"])
        except _Timeout:
            record = {"eval": name, "pair": item["pair"], "verdict": "timeout"}
        except Exception as error:
            record = {"eval": name, "pair": item["pair"], "verdict": "search-error", "error": f"{type(error).__name__}: {error}"[:300]}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        record["mutant"] = label
        record["left"], record["right"] = case.left, case.right
        record["source"] = [mutated["left"], mutated["right"]]
        counts["accepted"] += 1
        counts[record["verdict"]] += 1
        records.append(record)
    summary = {"eval": name, "pair": item["pair"], "summary": True, "seconds": round(time.time() - start, 1), **counts}
    return [summary, *records]


def show(path: Path, verdicts: tuple[str, ...]) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("summary") or record.get("verdict") not in verdicts:
            continue
        left, right = record.get("source") or (record.get("left"), record.get("right"))
        print(f"== {record['verdict']}  {record['eval']}  {record['pair']}  {record['mutant']}")
        print(f"   A: {left}\n   B: {right}")
        if record.get("witness"):
            print(f"   witness: {json.dumps(record['witness'], default=str)[:600]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evals", nargs="*", help="eval names (see --list)")
    parser.add_argument("--list", action="store_true", help="list the evals that can be probed and stop")
    parser.add_argument("--show", type=Path, help="print the accepted mutants of a saved <eval>.jsonl and stop")
    parser.add_argument("--verdicts", default="differs,survived,timeout,search-error,unrunnable", help="which records --show prints")
    parser.add_argument("--out", default="proof-mutants", help="folder for <eval>.jsonl")
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--modes", default="right,left,self", help="comma-separated: right, left, self")
    parser.add_argument("--per-pair", type=int, default=24, help="mutated pairs per eval pair, spread over the operators (0: all)")
    parser.add_argument("--budget", type=int, default=600, help="databases per accepted mutant")
    parser.add_argument("--seconds", type=float, default=40.0, help="search seconds per accepted mutant")
    parser.add_argument("--prove-seconds", type=float, default=30.0, help="seconds allowed to prove one mutated pair")
    parser.add_argument("--exhaustive-cap", type=int, default=600)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--pairs", default="", help="comma-separated pair names to run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--every", type=int, default=1, help="run every n-th pair")
    parser.add_argument("--fresh", action="store_true", help="ignore records already written")
    args = parser.parse_args(argv)

    if args.show:
        show(args.show, tuple(args.verdicts.split(",")))
        return 0
    adapters = registry()
    if args.list:
        for name in sorted(adapters):
            print(name)
        return 0
    modes = tuple(args.modes.split(","))
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        parser.error(f"unknown mode {unknown[0]}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    options = {
        "modes": modes, "per_pair": args.per_pair, "budget": args.budget, "seconds": args.seconds, "seed": args.seed,
        "exhaustive_cap": args.exhaustive_cap, "prove_seconds": args.prove_seconds,
    }
    status = 0
    for name in args.evals:
        if name not in adapters:
            parser.error(f"{name} cannot be probed (see --list)")
        items = [i for i in adapters[name].items() if not i.get("held_out")][:: args.every]
        if args.pairs:
            wanted = set(args.pairs.split(","))
            items = [i for i in items if i["pair"] in wanted]
        if args.limit:
            items = items[: args.limit]
        path = out / f"{name}.jsonl"
        done = set()
        if path.exists() and not args.fresh and not args.pairs:
            done = {r["pair"] for r in map(json.loads, filter(str.strip, path.read_text(encoding="utf-8").splitlines())) if r.get("summary")}
        todo = [i for i in items if i["pair"] not in done]
        print(f"{name}: {len(items)} pairs, {len(todo)} to run", flush=True)
        totals: Counter = Counter()
        start = time.time()
        mode = "w" if (args.fresh and not args.pairs) else "a"
        with path.open(mode, encoding="utf-8") as sink, multiprocessing.get_context("fork").Pool(args.jobs, maxtasksperchild=20) as pool:
            for number, records in enumerate(pool.imap_unordered(_work, [(name, item, options) for item in todo]), 1):
                for record in records:
                    sink.write(json.dumps(record, default=str) + "\n")
                    if record.get("summary"):
                        totals.update({k: v for k, v in record.items() if isinstance(v, int) and not isinstance(v, bool)})
                    elif record["verdict"] == "differs":
                        status = 1
                        print(f"  DIFFERS {record['pair']} {record['mutant']}", flush=True)
                sink.flush()
                if number % 25 == 0:
                    print(f"  {number}/{len(todo)} {dict(totals)} {time.time() - start:.0f}s", flush=True)
        print(f"{name}: {dict(totals)} in {time.time() - start:.0f}s", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
