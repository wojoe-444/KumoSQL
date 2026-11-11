"""Admission check: a proof is counted in an eval's score only after it has been through the heavy checks.

An eval's results file (``benchmarks/results/<eval>.json``) holds a count of proven pairs. Nothing used to
say *which* pairs, so a rule that newly proved a pair raised the count with no more evidence than the
eval's own short random run. Per eval, a ledger lists the proofs that were admitted to the count
(``benchmarks/admitted/<eval>.txt``), and this tool admits a new proof only after:

1. the heavy executed search of the proof re-check (``tools/recheck/engine.py``: exhaustive tiny
   databases, edge databases and random ones) finds no database on which the two queries differ, and
2. the near-miss probe (``tools/proof_mutants.py``) finds no single-site mutant of the pair that the
   prover also calls equivalent and a database separates (held-out pairs are never mutated).

Since the prover moved to its own repository (wojoe-444/Kumo_SQL_Prover), the results files and the ledgers
live there, next to each other, and that repository's gate compares them (``tests/test_proof_ledger.py``).
This tool, which needs the executed-search engine, stays here and works on a checkout of that repository:
``--prover PATH``, or the environment variable ``KUMO_SQL_PROVER_CHECKOUT``. There is no default location:
without one of the two the command stops, it never compares against nothing.

    python tools/proof_admission.py status --prover ../Kumo_SQL_Prover            # ledger size against each results file
    python tools/proof_admission.py check --prover ../Kumo_SQL_Prover             # every results file counts what its ledger admits
    python tools/proof_admission.py admit qed-calcite --prover ../Kumo_SQL_Prover # prove every pair, check the new proofs, update the ledger
    python tools/proof_admission.py admit qed-calcite --prover ../Kumo_SQL_Prover --dry-run   # report unadmitted and lost proofs, write nothing
    python tools/proof_admission.py admit singh --prover ../Kumo_SQL_Prover --baseline        # start a ledger from today's proofs, unchecked

``admit`` proves with the prover installed in this environment, so that has to be the checkout it writes to
(``pip install -e <checkout>``); it refuses otherwise. It exits 1 when a new proof is shown false (the
witness is printed; the pair is not added) and drops ledger lines whose pair is no longer proven or whose
text changed. A ledger line is ``<digest> <how> <pair>``: the digest covers the pair exactly as the eval
reads it, and ``how`` is ``baseline`` (counted before the ledger existed), ``checked`` (both checks ran),
``searched`` (only the executed search: a held-out pair, or an eval whose pairs cannot be mutated) or
``unrunnable`` (DuckDB cannot run the pair, so no executed check exists; the proof stands on the prover alone).
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import sys

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "src"))

PROVER_ENV = "KUMO_SQL_PROVER_CHECKOUT"
HOWS = ("baseline", "checked", "searched", "unrunnable")
_HEADER = "# Proofs of {name} admitted to its count. Written by KumoSQL's tools/proof_admission.py (docs/proof-admission.md); do not edit by hand.\n"


class NoProverCheckout(Exception):
    """The ledgers and results files cannot be located; the message says what to pass."""


def prover_checkout(path: str | os.PathLike | None = None, environ=os.environ) -> Path:
    """The prover checkout that holds the results files and the ledgers: ``path``, else ``$KUMO_SQL_PROVER_CHECKOUT``.

    Raises :class:`NoProverCheckout` when neither is given or the folder is not such a checkout. Nothing is guessed:
    a check that quietly looked in the wrong place would compare the ledgers against nothing and pass."""

    chosen = path or environ.get(PROVER_ENV)
    if not chosen:
        raise NoProverCheckout(
            f"the results files and ledgers live in the prover repository (wojoe-444/Kumo_SQL_Prover); pass --prover <checkout> or set {PROVER_ENV}"
        )
    root = Path(chosen).expanduser().resolve()
    if not (root / "benchmarks" / "results").is_dir() or not (root / "src" / "kumo_sql_prover").is_dir():
        raise NoProverCheckout(f"{root} is not a checkout of the prover repository (no benchmarks/results or src/kumo_sql_prover)")
    return root


def installed_prover_is(root: Path) -> tuple[bool, str]:
    """Whether the ``kumo_sql_prover`` this environment imports is the one in ``root``, and where it is imported from."""

    import importlib.util

    spec = importlib.util.find_spec("kumo_sql_prover")
    origin = Path(spec.origin).resolve() if spec and spec.origin else None
    return origin is not None and (root / "src") in origin.parents, str(origin)


def digest(name: str, item: dict) -> str:
    """Twelve hex digits over the eval's name and the pair exactly as its adapter lists it."""

    return hashlib.sha256(json.dumps([name, item], sort_keys=True, default=str).encode()).hexdigest()[:12]


def read_ledger(name: str, folder: Path) -> dict[str, tuple[str, str]]:
    """``pair -> (digest, how)``; empty when the eval has no ledger."""

    path = folder / f"{name}.txt"
    entries: dict[str, tuple[str, str]] = {}
    if not path.is_file():
        return entries
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(" ", 2)
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{12}", parts[0]) or parts[1] not in HOWS or parts[2] in entries:
            raise ValueError(f"{path.name}:{number}: not a ledger line: {line[:80]!r}")
        entries[parts[2]] = (parts[0], parts[1])
    return entries


def write_ledger(name: str, entries: dict[str, tuple[str, str]], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lines = [f"{entries[pair][0]} {entries[pair][1]} {pair}\n" for pair in sorted(entries)]
    (folder / f"{name}.txt").write_text(_HEADER.format(name=name) + "".join(lines), encoding="utf-8")


def ledgers(folder: Path) -> list[str]:
    return sorted(path.stem for path in folder.glob("*.txt")) if folder.is_dir() else []


def counted(name: str, results: Path) -> int | None:
    """The number of proofs the eval's results file counts: ``coverage.proven``, else the number that opens ``score``."""

    path = results / f"{name}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    proven = (data.get("coverage") or {}).get("proven")
    if isinstance(proven, int) and not isinstance(proven, bool):
        return proven
    match = re.match(r"\s*([\d,]+)", str(data.get("score", "")))
    return int(match.group(1).replace(",", "")) if match else None


def check(folder: Path, results: Path) -> list[str]:
    """One line per ledger that does not match its results file (the prover repository's gate runs the same
    comparison in ``tools/proof_ledger.py``).

    More counted than admitted: a proof was counted without the checks. Fewer: the ledger still lists a proof the
    eval lost, which would let a new, unchecked proof take its place without the count moving. A ledger with no
    results file, or with one that gives no count, has nothing to be compared with and is reported, never skipped."""

    problems = []
    for name in ledgers(folder):
        admitted = len(read_ledger(name, folder))
        if not (results / f"{name}.json").is_file():
            problems.append(f"{name}: benchmarks/admitted/{name}.txt has no results file benchmarks/results/{name}.json to be compared with")
            continue
        count = counted(name, results)
        if count is None:
            problems.append(f"{name}: benchmarks/results/{name}.json gives no proven count (coverage.proven, or a number that opens score)")
        elif count != admitted:
            problems.append(
                f"{name}: benchmarks/results/{name}.json counts {count} proofs and {admitted} are admitted; "
                f"run `python tools/proof_admission.py admit {name} --prover <checkout>` and commit benchmarks/admitted/{name}.txt"
            )
    return problems


# --- admit ---------------------------------------------------------------------------------------

_ADAPTERS: dict | None = None


def _adapters() -> dict:
    global _ADAPTERS
    if _ADAPTERS is None:
        import logging

        import proof_recheck

        os.environ.setdefault("KUMOSQL_TIMING", "0")
        logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
        _ADAPTERS = proof_recheck.adapters()
    return _ADAPTERS


def _prove(job: tuple) -> tuple[str, bool]:
    """Whether the eval proves the pair (a crash or a timeout is a failure to prove)."""

    import signal

    import proof_recheck

    name, item, options = job
    signal.signal(signal.SIGALRM, proof_recheck._alarm)
    signal.setitimer(signal.ITIMER_REAL, int(options["prove_seconds"]), 5)
    try:
        adapter = _adapters()[name]
        counted = getattr(adapter, "counted", None)  # an eval that scores a proven pair as something else says so
        return item["pair"], bool(counted(item)) if counted else adapter.case(item) is not None
    except Exception:
        return item["pair"], False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def examine(name: str, item: dict, options: dict, adapter=None, search=None, probe=None) -> dict:
    """Run both checks on one pair: ``{"pair", "verdict", "how"?, "why"?, "witness"?}``.

    ``verdict`` is ``admitted``, ``not-proven``, ``differs`` (a false proof) or ``unsettled`` (a check timed out or
    crashed; the pair is not admitted and can be tried again with a larger budget)."""

    import proof_mutants
    import proof_recheck

    adapter = adapter or _adapters()[name]
    search = search or proof_recheck._work
    probe = probe or proof_mutants._work
    record = search((name, item, {k: options[k] for k in ("budget", "seconds", "prove_seconds", "seed", "exhaustive_cap")}))
    verdict = record.get("verdict")
    out = {"pair": item["pair"]}
    if verdict == "not-proven":
        return {**out, "verdict": "not-proven"}
    if verdict == "differs":
        return {**out, "verdict": "differs", "why": "a database separates the two queries", "witness": record.get("witness")}
    if verdict == "unrunnable":
        return {**out, "verdict": "admitted", "how": "unrunnable"}
    if verdict != "survived":
        return {**out, "verdict": "unsettled", "why": f"executed search: {verdict} {record.get('error', '')}".strip()}
    if (record.get("meta") or {}).get("eval_verdict", "equivalent") != "equivalent":
        return {**out, "verdict": "not-proven"}  # proved here, but the eval itself does not count it (its own budget)
    if not getattr(adapter, "mutable", "") or item.get("held_out") or not options["per_pair"]:
        return {**out, "verdict": "admitted", "how": "searched"}
    mutant_options = {
        "modes": options["modes"], "per_pair": options["per_pair"], "budget": options["mutant_budget"], "seconds": options["mutant_seconds"],
        "seed": options["seed"], "exhaustive_cap": options["exhaustive_cap"], "prove_seconds": options["mutant_prove_seconds"],
    }
    for mutant in probe((name, item, mutant_options)):
        if not mutant.get("summary") and mutant.get("verdict") == "differs":
            return {
                **out, "verdict": "differs", "why": f"the prover also accepts the near miss {mutant.get('mutant')}, which a database separates",
                "witness": mutant.get("witness"), "source": mutant.get("source"),
            }
    return {**out, "verdict": "admitted", "how": "checked"}


def _examine(job: tuple) -> dict:
    return examine(*job)


def admit(name: str, items: list[dict], options: dict, folder: Path, pool_map=None, prove=_prove, examine_job=_examine) -> dict:
    """Bring the eval's ledger up to date; returns ``{"admitted", "kept", "lost", "differs", "unsettled", "unadmitted"}``.

    ``pool_map(function, jobs)`` runs the jobs (a process pool's ``imap_unordered``; plain ``map`` in tests)."""

    pool_map = pool_map or map
    ledger = read_ledger(name, folder)
    digests = {item["pair"]: digest(name, item) for item in items}
    known = [item for item in items if ledger.get(item["pair"], ("", ""))[0] == digests[item["pair"]]]
    fresh = [item for item in items if ledger.get(item["pair"], ("", ""))[0] != digests[item["pair"]]]
    report: dict = {"admitted": [], "kept": [], "lost": [], "differs": [], "unsettled": [], "unadmitted": []}
    entries: dict[str, tuple[str, str]] = {}
    for pair, proven in pool_map(prove, [(name, item, options) for item in known]):
        if proven:
            entries[pair] = ledger[pair]
            report["kept"].append(pair)
    report["lost"] = sorted(set(ledger) - set(entries))
    if options.get("baseline") or options.get("dry_run"):
        for pair, proven in pool_map(prove, [(name, item, options) for item in fresh]):
            if proven and options.get("baseline"):
                entries[pair] = (digests[pair], "baseline")
                report["admitted"].append(pair)
            elif proven:
                report["unadmitted"].append(pair)
    else:
        for result in pool_map(examine_job, [(name, item, options) for item in fresh]):
            pair = result["pair"]
            if result["verdict"] == "admitted":
                entries[pair] = (digests[pair], result["how"])
                report["admitted"].append(pair)
            elif result["verdict"] in ("differs", "unsettled"):
                report[result["verdict"]].append(result)
    for key in ("admitted", "kept", "unadmitted"):
        report[key].sort()
    if not options.get("dry_run"):
        write_ledger(name, entries, folder)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument(
        "--prover", metavar="CHECKOUT",
        help=f"checkout of the prover repository, which holds benchmarks/results and benchmarks/admitted (default: ${PROVER_ENV})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", parents=[where], help="ledger size against each results file")
    commands.add_parser("check", parents=[where], help="fail when a results file and its ledger disagree on the number of proofs")
    admitting = commands.add_parser("admit", parents=[where], help="prove every pair of an eval and admit its new proofs")
    admitting.add_argument(
        "--any-prover", action="store_true",
        help="admit even though the prover installed here is not the checkout (the ledger then describes the installed one)",
    )
    admitting.add_argument("evals", nargs="+", help="eval names (python tools/proof_recheck.py --list)")
    admitting.add_argument("--baseline", action="store_true", help="admit today's proofs without the checks (starting a ledger)")
    admitting.add_argument("--dry-run", action="store_true", help="report unadmitted and lost proofs; write nothing")
    admitting.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    admitting.add_argument("--budget", type=int, default=3000, help="databases per new proof")
    admitting.add_argument("--seconds", type=float, default=120.0, help="search seconds per new proof")
    admitting.add_argument("--prove-seconds", type=float, default=300.0, help="seconds allowed to prove one pair")
    admitting.add_argument("--per-pair", type=int, default=24, help="near-miss mutants per new proof (0: none)")
    admitting.add_argument("--modes", default="right,left,self")
    admitting.add_argument("--mutant-budget", type=int, default=600, help="databases per accepted near miss")
    admitting.add_argument("--mutant-seconds", type=float, default=40.0)
    admitting.add_argument("--mutant-prove-seconds", type=float, default=30.0)
    admitting.add_argument("--exhaustive-cap", type=int, default=1500)
    admitting.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        root = prover_checkout(args.prover)
    except NoProverCheckout as error:
        print(f"proof_admission: {error}", file=sys.stderr)
        return 2
    folder, results = root / "benchmarks" / "admitted", root / "benchmarks" / "results"
    if args.command == "status":
        for name in ledgers(folder):
            hows = Counter(how for _, how in read_ledger(name, folder).values())
            count = counted(name, results)
            print(
                f"{name}: {sum(hows.values())} admitted ({', '.join(f'{n} {how}' for how, n in sorted(hows.items()))}); "
                f"results file {'counts ' + str(count) if count is not None else 'MISSING OR WITHOUT A COUNT'}"
            )
        return 0
    if args.command == "check":
        problems = check(folder, results)
        for problem in problems:
            print(problem)
        if not problems:
            print(f"{len(ledgers(folder))} ledgers match their results files in {root}")
        return 1 if problems else 0

    same, origin = installed_prover_is(root)
    if not same and not args.any_prover:
        print(
            f"proof_admission: the prover installed here ({origin}) is not the checkout {root}, so the proofs would be of other code "
            f"than the ledger is committed with; run `pip install -e {root}` first, or pass --any-prover",
            file=sys.stderr,
        )
        return 2

    adapters = _adapters()
    options = {**vars(args), "modes": tuple(args.modes.split(","))}
    status = 0
    for name in args.evals:
        if name not in adapters:
            parser.error(f"unknown eval {name} (python tools/proof_recheck.py --list)")
        if args.baseline and read_ledger(name, folder):
            parser.error(f"{name} already has a ledger; --baseline only starts one")
        os.environ[PROVER_ENV] = str(root)  # adapters that read pairs from the prover checkout read this one
        items = adapters[name].items()
        with multiprocessing.get_context("fork").Pool(args.jobs, maxtasksperchild=20) as pool:
            report = admit(name, items, options, folder, pool_map=pool.imap_unordered)
        print(
            f"{name}: {len(report['kept'])} kept, {len(report['admitted'])} admitted, {len(report['lost'])} lost, "
            f"{len(report['unadmitted'])} unadmitted, {len(report['differs'])} false, {len(report['unsettled'])} unsettled",
            flush=True,
        )
        for pair in report["lost"]:
            print(f"  lost: {pair}")
        for pair in report["unadmitted"]:
            print(f"  not admitted yet: {pair}")
        for result in report["unsettled"]:
            print(f"  unsettled: {result['pair']}: {result['why']}")
        for result in report["differs"]:
            print(f"  FALSE PROOF: {result['pair']}: {result['why']}")
            if result.get("source"):
                print(f"    A: {result['source'][0]}\n    B: {result['source'][1]}")
            if result.get("witness"):
                print(f"    witness: {json.dumps(result['witness'], default=str)[:800]}")
        if report["differs"] or report["unsettled"] or report["unadmitted"]:
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
