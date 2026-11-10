"""Compute what belongs to the equivalence prover, so the split into its own package can be re-checked.

    python tools/prover_split.py                  # the import closure of the prover roots, with line counts
    python tools/prover_split.py --list           # one module per line, marked shared (also used by the product) or not
    python tools/prover_split.py --check          # exit 1 if the closure pulls in a product-only module
    python tools/prover_split.py --plan           # the tests, tools, results files and docs pages that move with it
    python tools/prover_split.py --json           # everything above as JSON

The prover is the import closure of ``ROOTS`` over ``src/kumosql`` (lazy imports inside functions count; the
walk is on the syntax tree, so nothing is executed). ``FORBIDDEN`` names the product modules the closure must
never reach: the check is how a regression of the first step of the split (``equivalence`` importing
``lift_subqueries``, which imported the rule registry and, through it, BigQuery scripting) is caught.

Before the move this script reads ``src/kumosql``; after it, the same closure is the content of the
``kumo_sql_prover`` package, and ``--package`` points the script at that source tree instead (the check then
also fails on any import of ``kumosql``).
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent

#: The modules the prover is made of: the eleven named by the roadmap (step 8), plus two benchmark-support leaves.
#: Their import closure is the prover.
ROOTS = (
    "equivalence",
    "smt_equivalence",
    "algebraic_equivalence",
    "bounded_equivalence",
    "result_equivalence",
    "sqlsolver_backend",
    "tie_determinism",
    "tie_witness",
    "uexpr",
    "proof_registry",
    "proof_format",
    # Benchmark support the prover's own evals import: the Singh-Bedathur canonical form and the query mutants
    # of the soundness fuzzers. Each is a leaf that imports nothing outside the closure above.
    "canonical_rules",
    "query_mutants",
)

#: Product modules the prover must not import, directly or through a chain. ``engine`` is the registry of the
#: product's rewrite rules, ``scripts`` is BigQuery scripting; the rest is the pipeline, UI and CLI side.
FORBIDDEN = (
    "engine",
    "scripts",
    "pipeline",
    "pipeline_loading",
    "pipeline_equivalence",
    "cli",
    "ui",
    "rewrite",
    "cleanup",
    "formatting",
    "inline_ctes",
    "qualify_columns",
    "cost_rules",
    "live_graph",
    "prover_context",
    "prover_schema",
    "equivalences",
    "filter_pushdown",
    "incremental_monotone",
    "model_reuse",
    "containment",
    "consolidate",
    "lineage_filters",
)


def module_index(source: Path, package: str) -> dict[str, Path]:
    """Dotted module name (relative to the package) -> file, for every module under ``source``."""

    modules: dict[str, Path] = {}
    for path in sorted(source.rglob("*.py")):
        parts = list(path.relative_to(source).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    modules.pop("", None)
    return modules


def imports_of(name: str, path: Path, modules: dict[str, Path], package: str) -> tuple[set[str], set[str]]:
    """The package modules ``path`` imports (anywhere in the file), and the other ``package`` names it uses."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    own_package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        bases: list[tuple[str, list[str]]] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(package + "."):
                    found.add(alias.name[len(package) + 1 :])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and (node.module == package or node.module.startswith(package + ".")):
                    bases.append((node.module[len(package) + 1 :], [a.name for a in node.names]))
            else:
                parts = own_package.split(".") if own_package else []
                if node.level > 1:
                    parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
                bases.append((base, [a.name for a in node.names]))
        for base, names in bases:
            for imported in names:
                candidate = f"{base}.{imported}".strip(".")
                if candidate in modules:
                    found.add(candidate)
                elif base in modules:
                    found.add(base)
    found.discard("")
    # Importing ``a.b`` runs ``a/__init__``; count the parent packages that are modules of ours.
    for module in list(found):
        parent = module
        while "." in parent:
            parent = parent.rpartition(".")[0]
            if parent in modules:
                found.add(parent)
    found.discard(name)
    return found, set()


def import_graph(source: Path, package: str) -> tuple[dict[str, Path], dict[str, set[str]]]:
    modules = module_index(source, package)
    graph = {name: imports_of(name, path, modules, package)[0] for name, path in modules.items()}
    return modules, graph


def closure(graph: dict[str, set[str]], roots: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    stack = [root for root in roots if root in graph]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(graph.get(module, ()))
    return seen


def lines_of(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def chain_to(graph: dict[str, set[str]], roots: tuple[str, ...], target: str) -> list[str]:
    """One import path from a root to ``target`` (shortest first), to show how a forbidden module got in."""

    previous: dict[str, str | None] = {root: None for root in roots if root in graph}
    queue = list(previous)
    for module in queue:
        if module == target:
            break
        for dependency in sorted(graph.get(module, ())):
            if dependency not in previous:
                previous[dependency] = module
                queue.append(dependency)
    path: list[str] = []
    node: str | None = target if target in previous else None
    while node is not None:
        path.append(node)
        node = previous[node]
    return path[::-1]


def analyse(source: Path, package: str) -> dict:
    modules, graph = import_graph(source, package)
    inside = closure(graph, ROOTS)
    importers: dict[str, set[str]] = defaultdict(set)
    for module, dependencies in graph.items():
        if module not in inside:
            for dependency in dependencies & inside:
                importers[dependency].add(module)
    shared = sorted(importers)
    forbidden = [m for m in FORBIDDEN if m in inside]
    return {
        "modules": sorted(inside),
        "lines": sum(lines_of(modules[m]) for m in inside),
        "shared": shared,
        "prover_only": sorted(inside - set(shared)),
        "product_importers": sorted({m for found in importers.values() for m in found}),
        "forbidden": {m: chain_to(graph, ROOTS, m) for m in forbidden},
        "product_modules": len(modules) - len(inside),
        "product_lines": sum(lines_of(path) for name, path in modules.items() if name not in inside),
    }


# ---------------------------------------------------------------------------------------------------------------
# What else moves: tests, benchmark runners, results files, docs pages.
# ---------------------------------------------------------------------------------------------------------------

_PACKAGE_REF = re.compile(r"""\bkumosql(?:\.([A-Za-z_][A-Za-z_0-9]*))?""")


def referenced_modules(text: str, modules: dict[str, Path], package: str = "kumosql") -> set[str]:
    """Package modules a file names: imports (``from kumosql import a, b``) and dotted strings (``"kumosql.a.f"``)."""

    found: set[str] = set()
    for match in re.finditer(rf"\bfrom\s+{package}\s+import\s+\(?([^\n)]*(?:\n[^\n)]*)*?)\)?(?:\n|$)", text):
        for name in re.split(r"[,\s]+", match.group(1)):
            name = name.strip("()")
            if name in modules:
                found.add(name)
    for match in re.finditer(rf"\b{package}\.([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)", text):
        dotted = match.group(1).split(".")
        for end in range(len(dotted), 0, -1):
            candidate = ".".join(dotted[:end])
            if candidate in modules:
                found.add(candidate)
                break
    return found


def tool_imports(text: str, tools: set[str]) -> set[str]:
    """Other ``tools/`` modules a file imports."""

    found = set()
    for match in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z_0-9]*)", text, re.MULTILINE):
        if match.group(1) in tools:
            found.add(match.group(1))
    return found


def plan(source: Path, package: str, repo: Path) -> dict:
    modules, graph = import_graph(source, package)
    prover = closure(graph, ROOTS)
    tools_dir = repo / "tools"
    tests_dir = repo / "tests"
    tools = {p.stem: p for p in tools_dir.glob("*.py")} | {p.name: p for p in tools_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    tool_refs = {}
    for name, path in tools.items():
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files)
        tool_refs[name] = (referenced_modules(text, modules, package), tool_imports(text, set(tools) - {name}))
    test_files = sorted(tests_dir.glob("test_*.py"))
    test_refs = {}
    for path in test_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        test_refs[path.name] = (referenced_modules(text, modules, package), tool_imports(text, set(tools)))

    # A tool belongs to the prover when everything it names is the prover or another such tool. Start from every
    # tool and drop the ones that name a product module or a dropped tool, until nothing changes (tools import
    # tools). What is left and is reached from a tool that names the prover (or names it itself) moves; a helper
    # with no prover reference that the prover tools do not import (a corpus fetcher) stays.
    stays = {"eval_diff", "run_tests", "scoreboard", "test_history", "prover_split", "xdist_scheduler"}
    candidates = set(tools) - stays
    changed = True
    while changed:
        changed = False
        for name in sorted(candidates):
            refs, other = tool_refs[name]
            if not refs <= prover or not other <= candidates:
                candidates.discard(name)
                changed = True
    moved_tools = {name for name in candidates if tool_refs[name][0]}
    stack = list(moved_tools)
    while stack:
        for name in tool_refs[stack.pop()][1]:
            if name in candidates and name not in moved_tools:
                moved_tools.add(name)
                stack.append(name)
    moved_tests = {
        name
        for name, (refs, used) in test_refs.items()
        if refs <= prover and used <= moved_tools and (refs or used)
    }
    return {
        "tools": sorted(moved_tools),
        "tests": sorted(moved_tests),
        "tests_staying": sorted(set(test_refs) - moved_tests),
        "tool_blockers": {
            name: sorted((refs - prover) | (other - moved_tools))
            for name, (refs, other) in tool_refs.items()
            if name not in moved_tools and (refs & prover or other & moved_tools) and ((refs - prover) or (other - moved_tools))
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print every module of the closure")
    parser.add_argument("--check", action="store_true", help="exit 1 when the closure reaches a product-only module")
    parser.add_argument("--plan", action="store_true", help="also list the tests and tools that move with it")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--source", type=Path, default=ROOT / "src" / "kumosql", help="package source directory")
    parser.add_argument("--package", default="kumosql", help="package name used in imports")
    args = parser.parse_args(argv)

    result = analyse(args.source, args.package)
    if args.plan:
        result["plan"] = plan(args.source, args.package, ROOT)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"prover closure: {len(result['modules'])} modules, {result['lines']:,} lines "
            f"({len(result['shared'])} also imported by {len(result['product_importers'])} product modules); "
            f"product: {result['product_modules']} modules, {result['product_lines']:,} lines"
        )
        if args.list:
            shared = set(result["shared"])
            for module in result["modules"]:
                print(f"{module}{'  (shared)' if module in shared else ''}")
        if args.plan:
            p = result["plan"]
            print(f"moves: {len(p['tests'])} test files, {len(p['tools'])} tools; stays: {len(p['tests_staying'])} test files")
    for module, chain in result["forbidden"].items():
        print(f"forbidden: {module} is reached through {' -> '.join(chain)}", file=sys.stderr)
    if args.check and result["forbidden"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
