"""Carry what changed in KumoSQL since the last sync into the prover repository: one command.

    python tools/prover_resync.py FROM TO /path/to/Kumo_SQL_Prover            # show what would be carried
    python tools/prover_resync.py FROM TO /path/to/Kumo_SQL_Prover --apply    # apply it as a patch there
    python tools/prover_resync.py TO TO /path/to/Kumo_SQL_Prover --verify     # compare every moved file at TO with the prover tree

``FROM`` is the KumoSQL commit the prover was last synced to (the prover's last release notes name it), ``TO`` the
commit to sync to. Run it from a KumoSQL checkout; ``TO`` should be what the checkout's ``origin/master`` holds.

A changed path is *the prover's* when it is a module of ``kumosql._prover_aliases.MOVED`` or when the prover checkout has a
file at the same path (a test, a runner, a results file or a docs page). A file added since ``FROM`` that neither tree
has is listed as new, with a guess: a test that imports only moved modules looks prover-only; say so with ``--also PATH``.
The patch has ``kumosql.<moved name>`` and ``from kumosql import <public name>`` rewritten to the package's new name, as the
extraction did. Files KumoSQL deleted are deleted there. A hunk that does not apply is left in a ``.rej`` file: a deliberate
difference of the prover repository (the pytest ``--quick`` rules, the README, the argparse of the runners) needs a hand edit.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEW = "kumo_sql_prover"
PROVER_DIRS = ("tests/", "tools/", "benchmarks/results/", "docs/", "docs_simple/")


def moved_names() -> tuple[frozenset[str], frozenset[str]]:
    """The top-level names that moved, and the public names the prover package re-exports."""

    sys.path.insert(0, str(ROOT / "src"))
    from kumosql import _prover_aliases

    import kumo_sql_prover

    return frozenset(_prover_aliases.MOVED), frozenset(getattr(kumo_sql_prover, "__all__", ()))


def renamer(names: frozenset[str], exports: frozenset[str]):
    alternatives = "|".join(sorted(names | exports, key=len, reverse=True))
    dotted = re.compile(rf"\bkumosql\.({alternatives})\b")
    from_import = re.compile(r"\bfrom kumosql import\b")
    bare = re.compile(r"^(\s*)import kumosql\b(?!\.)", re.MULTILINE)
    source = re.compile(rf"\bsrc/kumosql/(?=(?:{alternatives})(?:\.py|/|\b))")

    def rename(text: str) -> str:
        text = dotted.sub(rf"{NEW}.\1", text)
        text = from_import.sub(f"from {NEW} import", text)
        text = bare.sub(rf"\1import {NEW}", text)
        return source.sub(f"src/{NEW}/", text)

    return rename


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check).stdout


def target(path: str, names: frozenset[str]) -> str | None:
    """Where a KumoSQL path lives in the prover repository, or None when it is not the prover's."""

    if path.startswith("src/kumosql/"):
        rest = path[len("src/kumosql/"):]
        return f"src/{NEW}/{rest}" if rest.split("/")[0].removesuffix(".py") in names else None
    return path if path.startswith(PROVER_DIRS) else None


def looks_prover_only(path: str, names: frozenset[str], exports: frozenset[str], ref: str) -> bool:
    if not (path.startswith("tests/test_") and path.endswith(".py")):
        return False
    text = git("show", f"{ref}:{path}", check=False)
    used = set(re.findall(r"\bkumosql\.(\w+)", text)) | set(re.findall(r"from kumosql import \(?\s*([\w, \n]+)", text))
    used = {u.strip() for chunk in used for u in chunk.split(",") if u.strip()}
    return bool(used) and all(u in names or u in exports for u in used)


def plan(old: str, new: str, prover: Path, names, exports, also: set[str]):
    carried, new_files, skipped = [], [], []
    for line in git("diff", "--name-status", "-M", old, new).splitlines():
        status, *paths = line.split("\t")
        path = paths[-1]
        mapped = target(path, names)
        if mapped is not None and (path.startswith("src/") or (prover / mapped).exists() or status.startswith("D") or path in also):
            carried.append((status[0], path, mapped))
        elif mapped is not None and status.startswith("A"):
            new_files.append((path, looks_prover_only(path, names, exports, new)))
        else:
            skipped.append(path)
    return carried, new_files, skipped


def patch_text(old: str, new: str, carried, rename) -> str:
    out = []
    for status, path, mapped in carried:
        diff = git("diff", old, new, "--", path)
        lines = []
        for line in diff.split("\n"):
            if line.startswith(("diff --git", "--- ", "+++ ")):
                line = line.replace(path, mapped)
            elif line[:1] in ("+", "-", " "):
                line = rename(line)
            lines.append(line)
        out.append("\n".join(lines))
    return "\n".join(out)


#: Files that differ on purpose in the prover repository (the pytest gate rules, runner options, renamed tests and notes).
EXPECTED_DIFFERENCES = frozenset({
    "tests/conftest.py", "tests/order.json", "tests/test_main_module.py", "tests/test_optional_dependencies.py",
    "tests/test_negated_predicates.py", "tests/test_bigquery_on_duckdb.py", "tests/fixtures/optimizer_bugs/README.md",
    "tools/bench_common.py", "tools/cosette_bench.py", "tools/eval_diff.py", "tools/logos_bench.py", "tools/parse_check_sweep.py",
    "tools/quite_bench.py", "tools/rule_fuzz_gen.py", "tools/scoreboard.py", "tools/sqlsolver_bench.py",
})


def verify(ref: str, prover: Path, names, exports) -> list[str]:
    rename = renamer(names, exports)
    different = []
    for path in git("ls-tree", "-r", "--name-only", ref).split():
        mapped = target(path, names)
        if mapped is None or path.startswith(("docs/", "docs_simple/")) or not (prover / mapped).exists() or path.endswith("__init__.py"):
            continue  # docs are adapted by hand to the other repository
        if (prover / mapped).read_text(encoding="utf-8") != rename(git("show", f"{ref}:{path}")):
            different.append(path)
    return [path for path in different if path not in EXPECTED_DIFFERENCES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("old", metavar="FROM")
    parser.add_argument("new", metavar="TO")
    parser.add_argument("prover", type=Path, metavar="PROVER_CHECKOUT")
    parser.add_argument("--apply", action="store_true", help="apply the patch in the prover checkout")
    parser.add_argument("--verify", action="store_true", help="list moved files at TO that differ from the prover checkout (after renaming), except the known deliberate differences")
    parser.add_argument("--also", action="append", default=[], metavar="PATH", help="carry a new file that neither tree has yet")
    args = parser.parse_args(argv)
    names, exports = moved_names()
    if args.verify:
        different = verify(args.new, args.prover, names, exports)
        print("\n".join(different) if different else "every moved file at TO matches the prover checkout")
        return 1 if different else 0
    carried, new_files, skipped = plan(args.old, args.new, args.prover, names, exports, set(args.also))
    for status, path, mapped in carried:
        print(f"{status}  {path} -> {mapped}")
    for path, guess in new_files:
        print(f"new {path}: {'looks prover-only (use --also)' if guess else 'stays in KumoSQL unless you say otherwise'}")
    print(f"{len(carried)} carried, {len(new_files)} new, {len(skipped)} left in KumoSQL")
    if not args.apply:
        return 0
    patch = patch_text(args.old, args.new, [c for c in carried if c[0] != "D"], renamer(names, exports))
    for status, _, mapped in carried:
        if status == "D" and (args.prover / mapped).exists():
            (args.prover / mapped).unlink()
    done = subprocess.run(["patch", "-p1", "--no-backup-if-mismatch", "-d", str(args.prover)], input=patch, text=True, capture_output=True)
    print(done.stdout.strip())
    rejects = [str(p.relative_to(args.prover)) for p in args.prover.rglob("*.rej")]
    if rejects:
        print("hunks that did not apply (edit by hand, then delete the .rej files):", *rejects, sep="\n  ")
    return 1 if done.returncode or rejects else 0


if __name__ == "__main__":
    raise SystemExit(main())
