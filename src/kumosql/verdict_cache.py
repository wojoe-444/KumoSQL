"""A cache of proof verdicts for the benchmark runs, keyed by the query pair and the prover's own source.

A benchmark floor re-proves thousands of pairs on every run although nothing the prover reads has changed. This cache
remembers the *yes or no* of a proof so a run re-proves only when the prover changed. It is opt-in: nothing is read
or written unless ``KUMOSQL_VERDICT_CACHE`` names a folder (``python tools/run_tests.py --verdict-cache PATH`` sets it).

What makes a hit safe:

* the key holds a hash of the source of every module the provers import (``prover_fingerprint``), so any edit to a
  prover module, a helper it uses or this file is a miss for every pair. ``bigquery_syntax`` is a root although only
  the package's ``__init__`` imports it: it patches the parser every proof reads with;
* a runner puts a hash of its own source in the payload (``source_digest``): how it builds the proof call (dialect,
  constraints, rewriting of the text) decides the verdict as much as the prover does;
* the key also holds the query texts, every option that changes a verdict, the SQLGlot, DuckDB, z3 and Python versions;
* only a verdict summary is stored (``proven`` and the status name), as JSON, never a pickled result object, so a damaged
  or planted file can at worst fail to parse, which is a miss. A caller that needs a counterexample or the reasons does not
  use this cache.

Missing, corrupt or unwritable cache files fall back to proving. The cache never makes a verdict *more* favourable than
the prover gave when the file was written: it replays that answer for the identical source and inputs.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import tempfile
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path

ENV = "KUMOSQL_VERDICT_CACHE"
FORMAT = 1

# The modules the provers start from; the fingerprint covers everything they import (docs/roadmap.md, step 8).
# ``bigquery_syntax`` patches sqlglot's BigQuery parser when the package is imported, so no prover module names it.
PROVER_ROOTS = (
    "bigquery_syntax",
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
    "verdict_cache",
)

_PACKAGE = Path(__file__).resolve().parent


def folder() -> Path | None:
    """The cache folder, or None when the cache is off."""

    value = os.environ.get(ENV, "").strip()
    if value.lower() in ("", "off", "0", "false"):
        return None
    return Path(value)


def _imports(path: Path, known: set[str], package: Path = _PACKAGE) -> set[str]:
    """Names of the package's top-level modules that ``path`` imports, at any depth (function-level imports count).

    ``path`` may sit in a subpackage: ``from ..ast_utils import x`` in ``uexpr/translate.py`` names ``ast_utils``, and
    ``from .ir import y`` there names ``uexpr`` itself. Absolute imports count when they name this package, whatever
    it is called (``kumosql`` in the product, another name where the prover is packaged on its own).
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside = path.resolve().parent.relative_to(package.resolve()).parts  # the subpackages ``path`` sits in
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return set()
    found: set[str] = set()

    def named(parts: list[str], names: list[str]) -> None:
        """An import of ``parts`` (relative to the package root; empty for the package itself) binding ``names``."""

        if parts:
            found.add(parts[0])
        else:
            found.update(names)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module.split(".") if node.module else []
            names = [alias.name for alias in node.names]
            if node.level:
                up = node.level - 1
                if up <= len(inside):
                    named([*inside[: len(inside) - up], *module], names)
            elif module and module[0] == package.name:
                named(module[1:], names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == package.name and len(parts) > 1:
                    found.add(parts[1])
    return {name for name in found if name in known}


def prover_modules(roots: Iterable[str] = PROVER_ROOTS, package: Path = _PACKAGE) -> list[str]:
    """The roots and every module they import, directly or not, sorted."""

    known = {p.stem for p in package.glob("*.py")} | {p.name for p in package.iterdir() if (p / "__init__.py").is_file()}
    known.discard("__init__")
    seen: set[str] = set()
    pending = [name for name in roots if name in known]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for path in _files(name, package):  # every file of a subpackage: each can import a module the others do not
            pending.extend(_imports(path, known, package) - seen)
    return sorted(seen)


def _files(name: str, package: Path) -> list[Path]:
    single = package / f"{name}.py"
    return [single] if single.is_file() else sorted((package / name).rglob("*.py"))


@lru_cache(maxsize=1)
def prover_fingerprint() -> str:
    """A hash of the source of every prover module and what they import."""

    digest = hashlib.sha256()
    for name in prover_modules():
        for path in _files(name, _PACKAGE):
            digest.update(str(path.relative_to(_PACKAGE)).encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_digest(*paths: str | Path) -> str:
    """A hash of the given source files, for a runner to put in its payload (an unreadable file hashes as missing)."""

    digest = hashlib.sha256()
    for path in paths:
        try:
            digest.update(hashlib.sha256(Path(path).read_bytes()).digest())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _environment() -> dict:
    import sqlglot

    versions = {"sqlglot": sqlglot.__version__, "python": platform.python_version()}
    for module in ("duckdb", "z3"):
        try:
            imported = __import__(module)
            versions[module] = getattr(imported, "__version__", None) or getattr(imported, "get_version_string", lambda: "")()
        except ImportError:
            versions[module] = None
    versions["compiled"] = any(Path(sqlglot.__file__).parent.rglob("*.so")) or any(Path(sqlglot.__file__).parent.rglob("*.pyd"))
    return versions


def key(kind: str, payload: object) -> str:
    """The cache key for one question: ``kind`` names the question, ``payload`` is every input that can change the answer."""

    context = {"format": FORMAT, "kind": kind, "payload": payload, "prover": prover_fingerprint(), "env": _environment()}
    return hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()


def lookup(cache_key: str) -> dict | None:
    root = folder()
    if root is None:
        return None
    try:
        record = json.loads((root / f"{cache_key[:2]}" / f"{cache_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("key") != cache_key or not isinstance(record.get("proven"), bool):
        return None
    return record


def store(cache_key: str, proven: bool, status: str = "") -> None:
    root = folder()
    if root is None:
        return
    target = root / cache_key[:2] / f"{cache_key}.json"
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"key": cache_key, "proven": proven, "status": status}, handle)
        os.replace(temporary, target)
    except OSError:
        pass  # an unavailable cache cannot change a verdict
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def cached_verdict(kind: str, payload: object, prove: Callable[[], bool]) -> bool:
    """``prove()`` answered from the cache when the same question was answered by the same prover source."""

    if folder() is None:
        return prove()
    cache_key = key(kind, payload)
    hit = lookup(cache_key)
    if hit is not None:
        return hit["proven"]
    verdict = bool(prove())
    store(cache_key, verdict)
    return verdict
