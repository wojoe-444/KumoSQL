"""Turn a CTE repeated across Dataform models into one shared model, as a reviewable patch.

``repeated_ctes`` lists the exact-duplicate groups (``Pipeline.duplicate_selects``) whose copies are
WITH tables of ``.sqlx`` models. ``extract_shared_model`` writes the patch for one group: a new
``.sqlx`` file holding the CTE body (refs and all, copied from the first copy's source text) and,
in every model that held a copy, the CTE body replaced by ``SELECT <its columns> FROM ${ref("<new>")}``.
Only the CTE body changes; the rest of each file is kept byte for byte.

The patch is then checked, never trusted from the fingerprint that found the group: the project is
loaded again with the patch applied, and for every edited model the prover compares its query before
the patch with its query after it, the new model inlined in place of the read. Each edited model is
``proven``, ``proven_with_assumptions`` (the prover's assumptions are listed), ``differs`` (the
prover found a database where they differ) or ``unknown``. A model downstream of edited models is
``unchanged`` when every edited model it reads through is proven, since the tables it reads then
hold the same rows. The verdict of the patch is the weakest of its edited models.

A group is offered only when extracting it can be stated as a file edit Dataform will compile the
same way: every copy is a uniquely named WITH table of a ``.sqlx`` file, its body has no Dataform
interpolation other than ``${ref(...)}``/``${resolve(...)}`` with literal names, reads no WITH table
of its model and no table by a one-part name (a BigQuery view cannot), names every output column
once, and has no value that changes from run to run (a clock, RAND, a UUID).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
from pathlib import Path, PurePosixPath
import re
from typing import Callable, Mapping

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import captured_names
from kumo_sql_prover.canonical import free_cte_refs
from kumo_sql_prover.sqlx import _SQLX_BLOCK_RE, _find_balanced_brace, _find_interpolation_end

KINDS = ("view", "table")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_REF_RE = re.compile(
    r"""^\$\{\s*(?:ref|resolve)\(\s*(?:"[^"\\\n]*"|'[^'\\\n]*')(?:\s*,\s*(?:"[^"\\\n]*"|'[^'\\\n]*'))*\s*\)\s*\}$"""
)
_PROJECT_SUFFIXES = (".sqlx", ".sql", ".js")
_CONFIG_FILES = ("workflow_settings.yaml", "workflow_settings.yml", "dataform.json")


class SharedModelError(ValueError):
    """The request cannot be turned into a patch (an unknown group, a bad name, no source files)."""


# ------------------------------------------------------------------ source text


def _skip_quoted(text: str, index: int) -> int:
    """Index just past the string, quoted identifier or triple-quoted string starting at ``index``."""

    quote = text[index]
    if text.startswith(quote * 3, index) and quote != "`":
        end = text.find(quote * 3, index + 3)
        return len(text) if end < 0 else end + 3
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return len(text)


def _tokens(text: str, start: int = 0, end: int | None = None):
    """``(kind, start, end)`` for the SQL tokens of ``text[start:end]`` that matter here.

    Kinds: ``word`` (an identifier or keyword, backticked or not), ``(``, ``)``, ``interp``
    (a ``${...}`` Dataform expression) and ``other``. Comments and whitespace are skipped.
    """

    end = len(text) if end is None else end
    index = start
    while index < end:
        char = text[index]
        nxt = text[index + 1] if index + 1 < end else ""
        if char.isspace():
            index += 1
        elif char == "-" and nxt == "-" or char == "#":
            newline = text.find("\n", index)
            index = end if newline < 0 else newline + 1
        elif char == "/" and nxt == "*":
            close = text.find("*/", index + 2)
            index = end if close < 0 else close + 2
        elif char == "$" and nxt == "{":
            close = _find_interpolation_end(text, index)
            yield "interp", index, close + 1
            index = close + 1
        elif char == "`":
            stop = _skip_quoted(text, index)
            yield "word", index, stop
            index = stop
        elif char in "'\"":
            stop = _skip_quoted(text, index)
            yield "other", index, stop
            index = stop
        elif char.isalnum() or char == "_":
            stop = index
            while stop < end and (text[stop].isalnum() or text[stop] == "_"):
                stop += 1
            yield "word", index, stop
            index = stop
        elif char in "()":
            yield char, index, index + 1
            index += 1
        else:
            yield "other", index, index + 1
            index += 1


def _word(text: str, start: int, end: int) -> str:
    return text[start:end].strip("`").lower()


def _sql_ranges(text: str) -> list[tuple[int, int]]:
    """The SQL sections of a ``.sqlx`` file: everything outside its config, js and operations blocks."""

    ranges: list[tuple[int, int]] = []
    cursor = 0
    while match := _SQLX_BLOCK_RE.search(text, cursor):
        ranges.append((cursor, match.start()))
        cursor = _find_balanced_brace(text, text.find("{", match.start(), match.end())) + 1
    ranges.append((cursor, len(text)))
    return ranges


def cte_bodies(text: str, name: str) -> list[tuple[int, int]]:
    """``(start, end)`` of the body of every WITH table named ``name`` in the SQL of a ``.sqlx`` file.

    ``start`` is just past the opening parenthesis of ``name AS (`` and ``end`` at its closing one.
    """

    wanted = name.lower()
    found: list[tuple[int, int]] = []
    for low, high in _sql_ranges(text):
        tokens = list(_tokens(text, low, high))
        for i in range(len(tokens) - 2):
            (k0, s0, e0), (k1, s1, e1), (k2, s2, _) = tokens[i : i + 3]
            if k0 == "word" and k1 == "word" and k2 == "(" and _word(text, s0, e0) == wanted and _word(text, s1, e1) == "as":
                previous = _word(text, *tokens[i - 1][1:]) if i and tokens[i - 1][0] == "word" else ""
                if previous in ("with", "recursive") or (i and tokens[i - 1][0] == "other" and text[tokens[i - 1][1]] == ","):
                    depth = 0
                    for kind, start, _ in tokens[i + 2 :]:
                        depth += kind == "("
                        depth -= kind == ")"
                        if depth == 0:
                            found.append((s2 + 1, start))
                            break
    return found


def _interpolation_problem(text: str) -> str:
    for kind, start, end in _tokens(text):
        if kind == "interp" and not _REF_RE.match(text[start:end]):
            return f"uses the Dataform expression {text[start:end][:60]}"
    return ""


def _dedent(body: str) -> str:
    lines = body.strip("\n").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(line[cut:] if line.strip() else "" for line in lines).strip("\n")


# ------------------------------------------------------------------- candidates


@dataclass
class Site:
    model: str
    path: str
    cte: str
    columns: tuple[str, ...] = ()
    problem: str = ""

    def to_json(self) -> dict:
        data = {"model": self.model, "path": self.path, "cte": self.cte, "columns": list(self.columns)}
        if self.problem:
            data["problem"] = self.problem
        return data


@dataclass
class RepeatedCte:
    id: str
    sql: str  # the canonical text of the repeated SELECT
    node_count: int
    sites: list[Site] = field(default_factory=list)
    others: list[dict] = field(default_factory=list)  # copies that are not WITH tables; left as they are
    problem: str = ""

    @property
    def extractable(self) -> bool:
        return not self.problem and len(self.sites) >= 2

    def to_json(self) -> dict:
        data = {
            "id": self.id, "sql": self.sql, "node_count": self.node_count,
            "sites": [s.to_json() for s in self.sites], "others": self.others,
            "extractable": self.extractable, "suggested_name": suggested_name(self),
        }
        if self.problem:
            data["problem"] = self.problem
        return data


def suggested_name(group: RepeatedCte) -> str:
    names = sorted({site.cte for site in group.sites})
    return names[0] if names else "shared_model"


def _copy_in(tree: exp.Expression, cte: str) -> list[exp.CTE]:
    return [c for c in tree.find_all(exp.CTE) if c.alias_or_name.lower() == cte.lower()]


def _site_problem(model, text: str | None, cte: str) -> tuple[str, tuple[str, ...]]:
    """Why the copy named ``cte`` in ``model`` cannot be replaced by a file edit (``""`` if it can), and its columns."""

    from .pipeline_equivalence import run_dependent

    if not model.path or not model.path.lower().endswith(".sqlx"):
        return "is not defined in a .sqlx file", ()
    if text is None:
        return "its source file is not available", ()
    try:
        spans = cte_bodies(text, cte)
    except ValueError:
        return "its source file could not be read", ()
    if len(spans) != 1:
        return f"defines {len(spans)} WITH tables named {cte}", ()
    body = text[spans[0][0] : spans[0][1]]
    problem = _interpolation_problem(body)
    if problem:
        return problem, ()
    try:
        tree = sqlglot.parse_one(model.sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return "its query could not be parsed", ()
    copies = _copy_in(tree, cte)
    if len(copies) != 1:
        return f"defines {len(copies)} WITH tables named {cte}", ()
    copy = copies[0]
    if isinstance(copy.parent, exp.With) and copy.parent.args.get("recursive"):
        return "is in a recursive WITH", ()
    query = copy.this
    if not isinstance(query, exp.Select):
        return "is not a single SELECT", ()
    outside = sorted(free_cte_refs(query))
    if outside:
        return f"reads {', '.join(outside)}, defined elsewhere in the model", ()
    one_part = sorted({t.name for t in query.find_all(exp.Table) if t.name and not t.args.get("db")} - _inner_ctes(query))
    if one_part:
        return f"reads {one_part[0]} without a dataset", ()
    volatile = run_dependent(query)
    if volatile:
        return volatile, ()
    names = [p.alias_or_name for p in query.expressions]
    lowered = [n.lower() for n in names]
    if not names or any(not n or n == "*" for n in names) or len(set(lowered)) != len(lowered):
        return "does not name every output column once", ()
    return "", tuple(names)


def _inner_ctes(query: exp.Expression) -> set[str]:
    return {c.alias_or_name for c in query.find_all(exp.CTE)}


def repeated_ctes(pipeline, files: Mapping[str, str] | None, *, min_nodes: int = 12) -> list[RepeatedCte]:
    """Exact-duplicate groups with at least two copies that are WITH tables, largest first."""

    files = files or {}
    groups: list[RepeatedCte] = []
    for group in pipeline.duplicate_selects(min_nodes=min_nodes):
        item = RepeatedCte(id=group.fingerprint, sql=group.sql, node_count=group.node_count)
        for occurrence in group.occurrences:
            model = pipeline.models.get(occurrence.model)
            if not occurrence.location.startswith("cte:") or model is None:
                item.others.append({"model": occurrence.model, "location": occurrence.location})
                continue
            cte = occurrence.location[4:]
            if any(s.model == occurrence.model and s.cte.lower() == cte.lower() for s in item.sites):
                continue
            problem, columns = _site_problem(model, files.get(model.path or ""), cte)
            item.sites.append(Site(occurrence.model, model.path or "", cte, columns, problem))
        if len(item.sites) < 2:
            continue
        bad = next((s for s in item.sites if s.problem), None)
        if bad is not None:
            item.problem = f"{bad.cte} in {bad.model} {bad.problem}"
        else:
            first = {c.lower() for c in item.sites[0].columns}
            if any({c.lower() for c in s.columns} != first for s in item.sites[1:]):
                item.problem = "the copies name their columns differently"
        groups.append(item)
    return groups


# ------------------------------------------------------------------------ patch


@dataclass
class ModelCheck:
    model: str
    role: str  # "edited" or "downstream"
    label: str
    reason: str = ""
    assumptions: tuple[str, ...] = ()
    via: tuple[str, ...] = ()

    def to_json(self) -> dict:
        data = {"model": self.model, "role": self.role, "label": self.label, "reason": self.reason,
                "assumptions": list(self.assumptions)}
        if self.via:
            data["via"] = list(self.via)
        return data


PROVEN = "proven"
PROVEN_WITH_ASSUMPTIONS = "proven_with_assumptions"
UNKNOWN = "unknown"
DIFFERS = "differs"
UNCHANGED = "unchanged"
_RANK = {PROVEN: 0, UNCHANGED: 0, PROVEN_WITH_ASSUMPTIONS: 1, UNKNOWN: 2, DIFFERS: 3}


@dataclass
class SharedModelPatch:
    group: RepeatedCte
    name: str
    kind: str
    new_file: str
    files: dict[str, str]  # path -> new text, for every file the patch writes
    diff: str
    checks: list[ModelCheck]
    diagnostics: list[str]

    @property
    def verdict(self) -> str:
        if self.diagnostics or not self.checks:
            return UNKNOWN
        worst = max((c.label for c in self.checks), key=lambda label: _RANK.get(label, 2))
        return PROVEN if worst == UNCHANGED else worst

    @property
    def assumptions(self) -> list[str]:
        return list(dict.fromkeys(a for c in self.checks for a in c.assumptions))

    def to_json(self) -> dict:
        return {
            "group": self.group.to_json(), "name": self.name, "kind": self.kind, "new_file": self.new_file,
            "changed_files": sorted(self.files), "diff": self.diff, "verdict": self.verdict,
            "assumptions": self.assumptions, "checks": [c.to_json() for c in self.checks],
            "diagnostics": self.diagnostics,
        }


def _model_names(pipeline) -> set[str]:
    return {model.target.name.lower() for model in pipeline.models.values()} | {
        target.name.lower() for target in getattr(pipeline, "sources", {}).values() if hasattr(target, "name")
    }


def _free_name(wanted: str, taken: set[str], paths: set[str], folder: PurePosixPath) -> str:
    candidates = [wanted, f"{wanted}_shared", *(f"{wanted}_shared_{i}" for i in range(2, 100))]
    for name in candidates:
        if name.lower() not in taken and str(folder / f"{name}.sqlx") not in paths:
            return name
    raise SharedModelError(f"no free model name starting with {wanted}")


def _identifier(name: str) -> str:
    return exp.to_identifier(name).sql(dialect="bigquery")


def _unified(path: str, before: str | None, after: str) -> str:
    old = [] if before is None else before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    for lines in (old, new):
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    head = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if before is None else "")
    body = "".join(difflib.unified_diff(old, new, "/dev/null" if before is None else f"a/{path}", f"b/{path}"))
    return head + body


def load_files(files: Mapping[str, str]):
    """A ``Pipeline`` for ``{relative path: text}``, parsed from a temporary folder (no caches, no Dataform API)."""

    from .live_graph import _Checkout, _write_files
    from .pipeline import load_sqlx_project

    with _Checkout() as directory:
        _write_files(dict(files), directory)
        pipeline = load_sqlx_project(directory)
        pipeline.completeness()  # analyse while the folder exists
    return pipeline


def read_project_files(root: str | Path) -> dict[str, str]:
    """The Dataform files of a project folder as ``{relative posix path: text}``."""

    root = Path(root)
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part.startswith(".") or part == "node_modules" for part in path.relative_to(root).parts):
            continue
        if path.name.lower().endswith(_PROJECT_SUFFIXES) or path.name.lower() in _CONFIG_FILES:
            try:
                files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return files


Prove = Callable[[str, str], object]


def _default_prove(pipeline) -> Prove:
    from . import prover_context
    from .prover_schema import from_pipeline

    schema = from_pipeline(pipeline)
    return lambda old, new: prover_context.prove(old, new, schema=schema, equivalences_enabled=False)


def _parts(table: exp.Table) -> tuple[str, ...]:
    return tuple(p for part in table.parts for p in part.name.lower().split("."))


def _inline(after_sql: str, shared_key: str, shared_sql: str) -> tuple[str | None, str]:
    """``after_sql`` with every read of the shared model replaced by its query, or ``(None, why)``."""

    try:
        tree = sqlglot.parse_one(after_sql, read="bigquery")
        body = sqlglot.parse_one(shared_sql, read="bigquery")
    except sqlglot.errors.SqlglotError as error:
        return None, f"the query could not be parsed: {error}"
    wanted = tuple(shared_key.lower().split("."))
    reads = [t for t in tree.find_all(exp.Table) if _parts(t) == wanted]
    if not reads:
        return None, "the edited query does not read the new model"
    for table in reads:
        if captured_names(body, table):
            return None, "a WITH table of the model has the name of a table the new model reads"
        alias = table.alias or table.name
        table.replace(exp.Subquery(this=body.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))
    return tree.sql(dialect="bigquery"), ""


def _check(prove: Prove, model: str, before_sql: str, after_sql: str) -> ModelCheck:
    from kumo_sql_prover.smt_equivalence import SmtStatus

    if "__sqlx_token_" in before_sql or "__sqlx_token_" in after_sql:
        return ModelCheck(model, "edited", UNKNOWN, "the model uses Dataform expressions the prover cannot read")
    try:
        result = prove(before_sql, after_sql)
    except Exception as error:  # noqa: BLE001 - a crash is never a proof
        return ModelCheck(model, "edited", UNKNOWN, f"the prover could not run: {error}")
    status = getattr(result, "status", None)
    assumptions = tuple(getattr(result, "assumptions", ()) or ())
    reason = getattr(result, "reason", "") or ""
    if status is SmtStatus.PROVEN_EQUIVALENT:
        return ModelCheck(model, "edited", PROVEN_WITH_ASSUMPTIONS if assumptions else PROVEN, reason, assumptions)
    if status is SmtStatus.NOT_EQUIVALENT:
        return ModelCheck(model, "edited", DIFFERS, reason)
    return ModelCheck(model, "edited", UNKNOWN, reason)


def extract_shared_model(
    files: Mapping[str, str],
    group_id: str,
    *,
    name: str | None = None,
    kind: str = "view",
    pipeline=None,
    prove: Prove | None = None,
    min_nodes: int = 12,
) -> SharedModelPatch:
    """The patch that moves the repeated CTE ``group_id`` into a new model, checked by the prover.

    ``files`` is the project as ``{relative path: text}``; ``pipeline`` is it loaded (loaded here
    when not given). ``prove(old_sql, new_sql)`` returns a prover result (default: the project's
    prover with the facts the project declares).
    """

    if kind not in KINDS:
        raise SharedModelError(f"kind must be one of {', '.join(KINDS)}")
    if name is not None and not _NAME_RE.match(name):
        raise SharedModelError("the model name must be letters, digits and underscores, starting with a letter or underscore")
    before = pipeline if pipeline is not None else load_files(files)
    group = next((g for g in repeated_ctes(before, files, min_nodes=min_nodes) if g.id == group_id), None)
    if group is None:
        raise SharedModelError("no repeated WITH table with that id in this project")
    if not group.extractable:
        raise SharedModelError(group.problem or "this group cannot be extracted")

    first = group.sites[0]
    folder = PurePosixPath(first.path).parent
    taken = _model_names(before)
    wanted = name or suggested_name(group)
    if name is not None and name.lower() in taken:
        raise SharedModelError(f"a table named {name} already exists in the project")
    name = _free_name(wanted, taken, set(files), folder)
    new_file = str(folder / f"{name}.sqlx")

    start, end = cte_bodies(files[first.path], first.cte)[0]
    shared_body = _dedent(files[first.path][start:end])
    new_text = f'config {{\n  type: "{kind}"\n}}\n\n{shared_body}\n'

    by_path: dict[str, list[Site]] = {}
    for site in group.sites:
        by_path.setdefault(site.path, []).append(site)
    changed: dict[str, str] = {}
    for path, sites in by_path.items():
        text = files[path]
        edits = []
        for site in sites:
            (low, high), = cte_bodies(text, site.cte)
            line_start = text.rfind("\n", 0, low) + 1
            indent = re.match(r"[ \t]*", text[line_start:]).group(0)
            columns = ", ".join(_identifier(c) for c in site.columns)
            edits.append((low, high, f'\n{indent}  SELECT {columns}\n{indent}  FROM ${{ref("{name}")}}\n{indent}'))
        for low, high, replacement in sorted(edits, reverse=True):
            text = text[:low] + replacement + text[high:]
        changed[path] = text
    changed[new_file] = new_text

    diff = "".join(_unified(path, files.get(path), changed[path]) for path in sorted(changed))
    patched = {**files, **changed}
    diagnostics: list[str] = []
    try:
        after = load_files(patched)
    except Exception as error:  # noqa: BLE001 - the patch must load before anything is checked
        diagnostics.append(f"the patched project could not be loaded: {error}")
        return SharedModelPatch(group, name, kind, new_file, changed, diff, [], diagnostics)

    shared_key = next((k for k, m in after.models.items() if m.path == new_file), None)
    if shared_key is None:
        diagnostics.append(f"{new_file} did not load as a model")
    before_codes = {(d.model, d.code) for d in before.all_diagnostics()}
    for diagnostic in after.all_diagnostics():
        if (diagnostic.model, diagnostic.code) not in before_codes and diagnostic.code in ("parse_error", "no_query", "duplicate_model"):
            diagnostics.append(f"{diagnostic.model}: {diagnostic.message}")
    if diagnostics:
        return SharedModelPatch(group, name, kind, new_file, changed, diff, [], diagnostics)

    prove = prove or _default_prove(before)
    shared_sql = after.models[shared_key].sql
    checks: list[ModelCheck] = []
    edited = sorted({site.model for site in group.sites})
    for key in edited:
        if key not in after.models:
            checks.append(ModelCheck(key, "edited", UNKNOWN, "the model is missing after the patch"))
            continue
        inlined, why = _inline(after.models[key].sql, shared_key, shared_sql)
        if inlined is None:
            checks.append(ModelCheck(key, "edited", UNKNOWN, why))
            continue
        checks.append(_check(prove, key, before.models[key].sql, inlined))

    status = {c.model: c for c in checks}
    downstream = before.downstream
    via: dict[str, set[str]] = {}
    for origin in edited:
        seen, todo = {origin}, [origin]
        while todo:
            for child in sorted(downstream.get(todo.pop(), ())):
                if child not in seen:
                    seen.add(child)
                    todo.append(child)
                    via.setdefault(child, set()).add(origin)
    for node in sorted(via):
        if node in status:
            continue
        origins = tuple(sorted(via[node]))
        proven = all(status[o].label in (PROVEN, PROVEN_WITH_ASSUMPTIONS) for o in origins)
        inherited = tuple(dict.fromkeys(a for o in origins for a in status[o].assumptions))
        checks.append(ModelCheck(
            node, "downstream", UNCHANGED if proven else UNKNOWN,
            "every edited model it reads returns the same rows" if proven else "an edited model it reads was not proven",
            inherited if proven else (), origins,
        ))
    return SharedModelPatch(group, name, kind, new_file, changed, diff, checks, diagnostics)


# --------------------------------------------------------------------------- app


def _loaded_files():
    from . import live_graph

    current = live_graph.loaded()
    if not current:
        raise SharedModelError("load a project first")
    files = getattr(current["pipeline"], "source_files", None)
    if not files:
        raise SharedModelError("reload the project to edit its files")
    return current["pipeline"], files


def repeated_payload() -> dict:
    """``GET /api/shared-models``: the repeated WITH tables of the loaded project."""

    from . import live_graph

    current = live_graph.loaded()
    if not current:
        return {"loaded": False, "groups": []}
    pipeline = current["pipeline"]
    files = getattr(pipeline, "source_files", None) or {}
    groups = repeated_ctes(pipeline, files)
    groups.sort(key=lambda g: (not g.extractable, -len(g.sites), -g.node_count))
    return {"loaded": True, "label": current["label"], "files_available": bool(files),
            "groups": [g.to_json() for g in groups]}


def patch_payload(body: object) -> dict:
    """``POST /api/shared-models/patch`` ``{id, name?, kind?}``: the patch for one group and its checks."""

    from . import prover_context

    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise SharedModelError("choose a repeated WITH table")
    name = body.get("name") or None
    kind = body.get("kind") or "view"
    if name is not None and not isinstance(name, str):
        raise SharedModelError("the model name must be text")
    if not isinstance(kind, str):
        raise SharedModelError("kind must be text")
    pipeline, files = _loaded_files()
    config = prover_context.settings()
    if not config["enabled"]:
        raise SharedModelError("the solver is turned off in Settings")
    schema = prover_context.current_schema()
    prove = lambda old, new: prover_context.prove(old, new, schema=schema)  # noqa: E731
    before = load_files(files)
    return extract_shared_model(files, body["id"], name=name, kind=kind, pipeline=before, prove=prove).to_json()


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql shared-model DIR [GROUP] [--name N] [--kind view|table] [--patch FILE]``."""

    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="kumosql shared-model",
        description="List CTEs repeated across Dataform models, or write the patch that moves one into a shared model. "
        "It never edits the project folder: --patch only saves a patch file for you to apply with git apply.",
    )
    parser.add_argument("project", help="Dataform project folder")
    parser.add_argument("group", nargs="?", help="id of the repeated CTE to extract (omit to list them)")
    parser.add_argument("--name", help="name of the new model (default: the CTE name)")
    parser.add_argument("--kind", choices=KINDS, default="view", help="type of the new model (default: view)")
    parser.add_argument("--patch", help="also write the patch to this file (apply with git apply)")
    args = parser.parse_args(argv)

    files = read_project_files(args.project)
    pipeline = load_files(files)
    if not args.group:
        print(json.dumps({"groups": [g.to_json() for g in repeated_ctes(pipeline, files)]}, indent=2))
        return 0
    try:
        patch = extract_shared_model(files, args.group, name=args.name, kind=args.kind, pipeline=pipeline)
    except SharedModelError as error:
        parser.error(str(error))
    if args.patch:
        Path(args.patch).write_text(patch.diff, encoding="utf-8")
    print(json.dumps(patch.to_json(), indent=2))
    return 0 if patch.verdict in (PROVEN, PROVEN_WITH_ASSUMPTIONS) else 1
