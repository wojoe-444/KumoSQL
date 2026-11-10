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

Before the move this script read ``src/kumosql``. Since the move it reads the installed ``kumo_sql_prover`` package
(``--source`` and ``--package`` choose another tree): the same closure is that package, and ``--check`` then also
fails on any import of ``kumosql`` inside it, the one direction an import must never take. ``--plan`` and ``--paths``
describe the move itself and need the pre-split tree (the history keeps it: ``git show 7011437d:src/kumosql``).
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

#: The modules the prover is made of: the eleven named by the roadmap (step 8), plus two benchmark-support leaves and the parser extension.
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
    # Teaches sqlglot's BigQuery parser syntax it lacks, once, when imported; every parse the prover makes relies on
    # it, so the prover's own package imports it first (as ``kumosql/__init__`` always did).
    "bigquery_syntax",
    # The verdict cache of the benchmark runs hashes the prover's own source; it lives with the prover, whose runners
    # call it. No prover module imports it, so it is a root.
    "verdict_cache",
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


#: Feature pages of ``docs/`` that describe the prover (each has a ``docs_simple/`` companion that moves with it).
#: Eval pages (``docs/evals/``) follow their results files.
PROVER_DOCS = (
    "bigquery-on-duckdb.md",
    "conditional-equivalence.md",
    "constraint-rewrites.md",
    "difference-explanations.md",
    "output-properties.md",
    "parser-checks.md",
    "proof-safeguards.md",
    "provers.md",
    "singleton-and-set-identity.md",
    "ties.md",
)


#: Generic files both repositories need: the scoreboard and eval-diff tools with their tests and docs, the docs
#: checks, and the results-file format. They are copied (the history comes along), not moved.
COPIED_FILES = (
    "tools/scoreboard.py",
    "tools/eval_diff.py",
    "tests/test_scoreboard.py",
    "tests/test_eval_diff.py",
    "tests/test_docs.py",
    "tools/docs_index.py",
    "tests/test_canaries.py",
    "docs/evals/eval-diff.md",
    "docs_simple/evals/eval-diff.md",
    "benchmarks/README.md",
    "docs_simple/benchmarks/README.md",
)


#: Tests that exist on both sides (listed in ``COPIED_FILES``): the runners they load stay in KumoSQL as copies.
COPIED_TESTS = frozenset(Path(f).name for f in COPIED_FILES if f.startswith("tests/"))


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


def locate() -> tuple[str, Path]:
    """The prover's package name and source folder: this repository's own copy before the split, the installed one after."""

    own = ROOT / "src" / "kumosql"
    if (own / "equivalence.py").exists():
        return "kumosql", own
    import importlib.util

    spec = importlib.util.find_spec("kumo_sql_prover")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("kumo_sql_prover is not installed: pip install -e .")
    return "kumo_sql_prover", Path(next(iter(spec.submodule_search_locations)))


def product_imports(source: Path) -> dict[str, list[int]]:
    """Files of ``source`` that import ``kumosql``, with the line numbers (the prover must not)."""

    found: dict[str, list[int]] = {}
    for path in sorted(source.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            if any(name == "kumosql" or name.startswith("kumosql.") for name in names):
                found.setdefault(path.relative_to(source).as_posix(), []).append(node.lineno)
    return found


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
    imports_product = product_imports(source) if package != "kumosql" else {}
    return {
        "modules": sorted(inside),
        "lines": sum(lines_of(modules[m]) for m in inside),
        "shared": shared,
        "prover_only": sorted(inside - set(shared)),
        "product_importers": sorted({m for found in importers.values() for m in found}),
        "forbidden": {m: chain_to(graph, ROOTS, m) for m in forbidden},
        "imports_kumosql": imports_product,
        "product_modules": len(modules) - len(inside),
        "product_lines": sum(lines_of(path) for name, path in modules.items() if name not in inside),
    }


# ---------------------------------------------------------------------------------------------------------------
# What else moves: tests, benchmark runners, results files, docs pages.
# ---------------------------------------------------------------------------------------------------------------

_PACKAGE_REF = re.compile(r"""\bkumosql(?:\.([A-Za-z_][A-Za-z_0-9]*))?""")


def package_exports(source: Path, modules: dict[str, Path]) -> dict[str, str]:
    """Name -> module for what the package's ``__init__`` re-exports (``kumosql.prove_equivalent``)."""

    init = source / "__init__.py"
    exports: dict[str, str] = {}
    if not init.exists():
        return exports
    for node in ast.parse(init.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in modules:
            for alias in node.names:
                exports[alias.asname or alias.name] = node.module
    return exports


def referenced_modules(
    text: str, modules: dict[str, Path], package: str = "kumosql", exports: dict[str, str] | None = None
) -> set[str]:
    """Package modules a file names: ``from kumosql import a, b``, dotted strings (``"kumosql.a.f"``) and the
    names ``kumosql/__init__`` re-exports (``kumosql.format_sql`` counts as ``formatting``)."""

    exports = exports or {}
    found: set[str] = set()
    for match in re.finditer(rf"\bfrom\s+{package}\s+import\s+\(?([^\n)]*(?:\n[^\n)]*)*?)\)?(?:\n|$)", text):
        for name in re.split(r"[,\s]+", match.group(1)):
            name = name.strip("()")
            if name in modules:
                found.add(name)
            elif name in exports:
                found.add(exports[name])
    for match in re.finditer(rf"\b{package}\.([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*)", text):
        dotted = match.group(1).split(".")
        for end in range(len(dotted), 0, -1):
            candidate = ".".join(dotted[:end])
            if candidate in modules:
                found.add(candidate)
                break
        else:
            if dotted[0] in exports:
                found.add(exports[dotted[0]])
    return found


def tool_imports(text: str, tools: set[str]) -> set[str]:
    """Other ``tools/`` modules a file imports."""

    found = set()
    for match in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z_0-9]*)", text, re.MULTILINE):
        if match.group(1) in tools:
            found.add(match.group(1))
    for match in re.finditer(r"^\s*from\s+tools\s+import\s+\(?([^\n)]*)", text, re.MULTILINE):
        for name in re.split(r"[,\s]+", match.group(1)):
            if name in tools:
                found.add(name)
    for match in re.finditer(r"\btools\.([A-Za-z_][A-Za-z_0-9]*)", text):
        if match.group(1) in tools:
            found.add(match.group(1))
    # tests also load a runner by path (``TOOLS / "qed_bench.py"``) or name (``importlib.import_module("qed_bench")``)
    for match in re.finditer(r"""["']([A-Za-z_][A-Za-z_0-9]*)(?:\.py)?["']|tools/([A-Za-z_][A-Za-z_0-9]*)\.py""", text):
        name = match.group(1) or match.group(2)
        if name in tools:
            found.add(name)
    return found


def plan(source: Path, package: str, repo: Path) -> dict:
    modules, graph = import_graph(source, package)
    prover = closure(graph, ROOTS)
    exports = package_exports(source, modules)
    tools_dir = repo / "tools"
    tests_dir = repo / "tests"
    tools = {p.stem: p for p in tools_dir.glob("*.py")} | {p.name: p for p in tools_dir.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    tool_refs = {}
    for name, path in tools.items():
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files)
        tool_refs[name] = (referenced_modules(text, modules, package, exports), tool_imports(text, set(tools) - {name}))
    test_files = sorted(tests_dir.glob("test_*.py"))
    test_refs = {}
    for path in test_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        test_refs[path.name] = (referenced_modules(text, modules, package, exports), tool_imports(text, set(tools)))

    # A tool belongs to the prover when everything it names is the prover or another such tool. Start from every
    # tool and drop the ones that name a product module or a dropped tool, until nothing changes (tools import
    # tools). What is left and is reached from a tool that names the prover (or names it itself) moves; a helper
    # with no prover reference that the prover tools do not import (a corpus fetcher) stays.
    stays = {"eval_diff", "run_tests", "test_history", "test_sqlglot_matrix", "prover_split", "xdist_scheduler"}
    copied = {"scoreboard"}  # generic, used by both repositories: copied, never moved
    candidates = set(tools) - stays
    changed = True
    while changed:
        changed = False
        for name in sorted(candidates):
            refs, other = tool_refs[name]
            if not refs <= prover or not other <= candidates:
                candidates.discard(name)
                changed = True
    moved_tools = {name for name in candidates if tool_refs[name][0] and name not in copied}
    stack = list(moved_tools)
    while stack:
        for name in tool_refs[stack.pop()][1]:
            if name in candidates and name not in moved_tools and name not in copied:
                moved_tools.add(name)
                stack.append(name)
    moved_tests = {
        name
        for name, (refs, used) in test_refs.items()
        if refs <= prover and used <= moved_tools and (refs or used) and name not in COPIED_TESTS
    }
    # A moved runner that something staying still imports (a corpus fetcher, ``bench_common``) stays too; it is
    # copied to the prover rather than moved.
    shared_tools: set[str] = set()
    wanted = {
        used
        for name in set(tools) - moved_tools
        for used in tool_refs[name][1]
    } | {used for name, (_, used) in test_refs.items() if name not in moved_tests for used in used}
    stack = [name for name in wanted if name in moved_tools]
    while stack:
        name = stack.pop()
        if name in shared_tools:
            continue
        shared_tools.add(name)
        stack.extend(dep for dep in tool_refs[name][1] if dep in moved_tools)

    results = {}
    for path in sorted((repo / "benchmarks" / "results").glob("*.json")):
        command = json.loads(path.read_text(encoding="utf-8")).get("command", "")
        used = re.findall(r"tools/([A-Za-z_0-9]+)\.py", command)
        results[path.name] = bool(used) and all(tool in moved_tools for tool in used)
    moved_results = {name for name, moves in results.items() if moves}

    # An eval page moves when every results file it names moves; one that names some of each is copied to both.
    pages: dict[str, tuple[set[str], set[str]]] = {}
    index = (repo / "docs" / "evals" / "README.md").read_text(encoding="utf-8")
    for line in index.splitlines():
        link = re.match(r"\| \[[^\]]*\]\(([\w.-]+\.md)\)", line)
        if link:
            named = {f"{n}.json" for n in re.findall(r"`([a-z0-9_.-]+)`", line.split("|")[-2]) if f"{n}.json" in results}
            pages[link.group(1)] = ({n for n in named if n in moved_results}, {n for n in named if n not in moved_results})
    eval_pages_moved = sorted(page for page, (go, stay) in pages.items() if go and not stay)
    eval_pages_shared = sorted(page for page, (go, stay) in pages.items() if go and stay)

    moved_text = "\n".join(
        [(tests_dir / name).read_text(encoding="utf-8", errors="replace") for name in sorted(moved_tests)]
        + [f.read_text(encoding="utf-8", errors="replace") for name in sorted(moved_tools) for f in ([tools[name]] if tools[name].is_file() else sorted(tools[name].rglob("*.py")))]
    )
    staying_text = "\n".join(
        [(tests_dir / name).read_text(encoding="utf-8", errors="replace") for name in sorted(set(test_refs) - moved_tests)]
        + [f.read_text(encoding="utf-8", errors="replace") for name in sorted(set(tools) - moved_tools) for f in ([tools[name]] if tools[name].is_file() else sorted(tools[name].rglob("*.py")))]
    )

    def mentioned(text: str, entry: Path) -> bool:
        return bool(re.search(r"[/\"']" + re.escape(entry.name) + r"[/\"'.]", text) or re.search(r"[/\"']" + re.escape(entry.stem) + r"[/\"'.]", text))

    assets: dict[str, str] = {}
    for base in (repo / "tests" / "fixtures", repo / "benchmarks"):
        for entry in sorted(base.iterdir()):
            if entry.name == "results" or entry.name == "README.md":
                continue
            if mentioned(moved_text, entry):
                assets[str(entry.relative_to(repo))] = "both" if mentioned(staying_text, entry) else "prover"
    return {
        "results": sorted(moved_results),
        "results_staying": sorted(set(results) - moved_results),
        "docs": sorted(PROVER_DOCS),
        "eval_pages": eval_pages_moved,
        "eval_pages_shared": eval_pages_shared,
        "assets": assets,
        "tools": sorted(moved_tools),
        "tools_shared": sorted(shared_tools),
        "tests": sorted(moved_tests),
        "tests_staying": sorted(set(test_refs) - moved_tests),
        "tool_blockers": {
            name: sorted((refs - prover) | (other - moved_tools))
            for name, (refs, other) in tool_refs.items()
            if name not in moved_tools and (refs & prover or other & moved_tools) and ((refs - prover) or (other - moved_tools))
        },
    }


def filter_repo_paths(source: Path, package: str, repo: Path, new_package: str = "kumo_sql_prover") -> list[str]:
    """Lines for ``git filter-repo --paths-from-file``: every path that moves, with the package renamed.

    Running it on a full clone keeps the history of each moved file at its new path::

        python tools/prover_split.py --paths > paths.txt
        git filter-repo --paths-from-file paths.txt
    """

    modules, graph = import_graph(source, package)
    inside = closure(graph, ROOTS)
    found = plan(source, package, repo)
    lines: list[str] = []
    seen_dirs: set[str] = set()
    base = source.relative_to(repo).as_posix()
    for module in sorted(inside):
        path = modules[module]
        top = module.split(".")[0]
        if (source / top).is_dir():  # a package (``uexpr``): its whole folder
            if top not in seen_dirs:
                seen_dirs.add(top)
                lines.append(f"{base}/{top}==>src/{new_package}/{top}")
            continue
        lines.append(f"{path.relative_to(repo).as_posix()}==>src/{new_package}/{path.name}")
    lines += [f"tests/{name}" for name in found["tests"]] + ["tests/sqlglot_support.py"]
    tools_dir = repo / "tools"
    for name in sorted(set(found["tools"])):
        lines.append(f"tools/{name}.py" if (tools_dir / f"{name}.py").exists() else f"tools/{name}")
    lines += [f"benchmarks/results/{name}" for name in found["results"]]
    for page in found["docs"]:
        lines += [f"docs/{page}", f"docs_simple/{page}"]
    for page in found["eval_pages"] + found["eval_pages_shared"]:
        lines += [f"docs/evals/{page}", f"docs_simple/evals/{page}"]
    lines += sorted(found["assets"])
    lines += [f"{path}" for path in COPIED_FILES]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print every module of the closure")
    parser.add_argument("--check", action="store_true", help="exit 1 when the closure reaches a product-only module")
    parser.add_argument("--plan", action="store_true", help="also list the tests and tools that move with it")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--paths", action="store_true", help="print the paths that move, as a git filter-repo paths file")
    parser.add_argument("--source", type=Path, default=None, help="package source directory (default: where the prover is)")
    parser.add_argument("--package", default=None, help="package name used in imports (default: where the prover is)")
    args = parser.parse_args(argv)
    located_package, located_source = locate()
    args.source = args.source or located_source
    args.package = args.package or located_package
    if (args.plan or args.paths) and args.package != "kumosql":
        raise SystemExit("--plan and --paths describe the move and need the pre-split tree (src/kumosql with the prover in it)")

    if args.paths:
        print("\n".join(filter_repo_paths(args.source, args.package, ROOT)))
        return 0
    result = analyse(args.source, args.package)
    if args.plan:
        result["plan"] = plan(args.source, args.package, ROOT)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if args.package == "kumosql":
            print(
                f"prover closure: {len(result['modules'])} modules, {result['lines']:,} lines "
                f"({len(result['shared'])} also imported by {len(result['product_importers'])} product modules); "
                f"product: {result['product_modules']} modules, {result['product_lines']:,} lines"
            )
        else:
            print(
                f"prover closure: {len(result['modules'])} modules, {result['lines']:,} lines of the {args.package} package "
                f"({result['product_modules']} further modules are outside the roots' closure)"
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
    for file, numbers in result["imports_kumosql"].items():
        print(f"forbidden: {file} imports kumosql (line {', '.join(map(str, numbers))})", file=sys.stderr)
    if args.check and (result["forbidden"] or result["imports_kumosql"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
