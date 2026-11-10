"""Reduce a Dataform project around the outputs you keep.

``reduce_project(root, keep)`` loads a Dataform project (or a folder of ``.sql`` files) and the actions whose
output must stay: the **kept outputs**, which keep their name and their exact output (the same columns in the
same order, proved to return the same rows). It returns the smallest project it can prove still produces
them, as a patch on the project's files:

* actions no kept output needs are dropped (an operation is dropped only when everything it writes is known
  and nothing that stays reads it);
* single-use intermediates are folded into their readers, equal tables merged, columns nobody reads pruned, a
  query repeated in several places moved into one shared table, and each remaining query simplified
  (:func:`kumosql.table_minimizer.minimize_tables`, with factoring on);
* every step is kept only when each kept output, and each surviving table that carries assertions, is proved
  equal to the original; a step the prover cannot prove is rejected, so the worst outcome is the input with
  only the unneeded actions removed.

What is never rewritten: incremental tables (their rows depend on earlier runs), operations, models with pre
or post operations, and models whose ``${...}`` expressions depend on the file they are written in
(``self()``, ``when(incremental(), ...)``, js-block constants). They stay exactly as written, and every table
they read is kept and proved unchanged. ``${dataform.projectConfig.vars.X}`` and constants from ``includes/``
mean the same in every file, so models that use only those can be rewritten and moved.

Assertions: one that reads only kept outputs and actions kept as written stays as it is. Others are dropped
and listed, unless ``keep_assertions`` (then each must keep returning the same rows) or they are named in
``keep``. An assertion another action lists in ``dependencies`` is kept the same way. A table with
``assertions`` in its config that survives must stay proved equal on the columns it keeps; when it is folded
away, its config assertions are listed as dropped, unless an action waits for them in ``dependencies`` (then
the table stays).

The patch keeps every ``config``, ``js`` and ``pre_operations``/``post_operations`` block of a changed file
byte for byte and rewrites only its SQL, with ``ref()`` written as the project already writes it. Dropped
actions are deleted; a shared table is a new file next to its first reader, with that reader's ``schema``,
``database`` and the tags of every reader. Before it is returned, the patched project is loaded again and
every kept output re-proved against the original.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tempfile
import time
from typing import Callable, Iterable, Mapping, Sequence

import sqlglot
from sqlglot import exp

from .pipeline import Pipeline, load_sqlx_project
from .pipeline_loading import _REF_RE, _config_dependencies, _config_value, _parse_ref_args, _top_level
from kumo_sql_prover.sqlx import split_sqlx_sections
from .table_minimizer import MinimizationError, _structural, minimize_tables, verify_tables

_QUERY_KINDS = {"table", "view", "sql"}
_TOKEN = re.compile(r"__sqlx_token_(\d+)__")
_GLOBAL = re.compile(r"__kumo_x_[0-9a-f]{12}__")
_CONTEXT = {"ctx", "self", "ref", "resolve", "name", "schema", "database", "when", "incremental", "dataform"}
_VARS = re.compile(r"^\$\{\s*dataform\.projectConfig\.vars\.[A-Za-z_$][\w$]*\s*\}$")
_PATH = re.compile(r"^\$\{\s*(?P<root>[A-Za-z_$][\w$]*)(?:\s*\.\s*[A-Za-z_$][\w$]*)+\s*\}$")
_JS_LOCAL = re.compile(r"\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Script statements whose writes the script reader records; anything else leaves what it writes unknown.
_KNOWN_STATEMENTS = {
    "select", "create_table", "create_view", "clone", "like", "insert", "merge", "delete", "update", "truncate",
    "load_data", "drop", "declare", "set", "export_data", "create_function", "create_procedure", "ddl", "block",
    "if", "begin", "end", "while", "loop", "repeat", "for", "return", "leave", "continue", "break",
}


class ReductionError(ValueError):
    """The project cannot be reduced as asked (an unknown or ambiguous kept output, a declaration kept)."""


# ------------------------------------------------------------------ patch


@dataclass
class FileChange:
    """One file of the patch: ``add``, ``modify`` or ``delete``, with its text before and after."""

    path: str
    action: str
    before: str = ""
    after: str = ""

    def diff(self) -> str:
        """A unified diff of this file that ``git apply`` accepts."""

        header = f"diff --git a/{self.path} b/{self.path}\n"
        if self.action == "add":
            header += "new file mode 100644\n"
        elif self.action == "delete":
            header += "deleted file mode 100644\n"
        before = "/dev/null" if self.action == "add" else f"a/{self.path}"
        after = "/dev/null" if self.action == "delete" else f"b/{self.path}"
        lines = []
        for line in difflib.unified_diff(self.before.splitlines(keepends=True), self.after.splitlines(keepends=True),
                                         before, after):
            lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return header + "".join(lines)

    def to_json(self) -> dict:
        return {"path": self.path, "action": self.action, "before": self.before, "after": self.after, "diff": self.diff()}


# ------------------------------------------------------------------ result


@dataclass
class ProjectReduction:
    root: str
    keep: list[str]  # kept outputs, by model key
    files: list[FileChange]
    proofs: dict[str, dict]  # kept output -> {status: unchanged | proved, reason, assumptions}
    removed: list[dict] = field(default_factory=list)  # {model, path, kind, why}
    changed: list[str] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)  # {model, path}
    fixed: list[dict] = field(default_factory=list)  # {model, why}: kept exactly as written
    checks: dict[str, dict] = field(default_factory=dict)  # surviving tables with assertions, proved unchanged
    dropped_assertions: list[dict] = field(default_factory=list)
    moves: list[str] = field(default_factory=list)
    rejected_moves: list[dict] = field(default_factory=list)
    tried: int = 0  # steps the search tried to prove
    rejected: int = 0  # steps it could not prove
    score_before: float = 0.0
    score_after: float = 0.0
    actions_before: int = 0
    actions_after: int = 0
    verified: bool = False
    stopped: str = ""
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def improved(self) -> bool:
        return self.score_after < self.score_before

    def patch(self) -> str:
        """Every file change as one unified diff."""

        return "".join(change.diff() for change in self.files)

    def apply(self, root: str | Path) -> None:
        """Write the reduced project into ``root`` (a copy of the original project)."""

        _apply(Path(root), self.files)

    def check_list(self) -> list[dict]:
        """One result per kept output and per surviving table with assertions, in the Shared models shape:
        ``{model, role, label, reason, assumptions}`` with ``role`` ``kept`` or ``checked``."""

        out = []
        for role, results in (("kept", self.proofs), ("checked", self.checks)):
            for model, proof in results.items():
                status, assumptions = proof.get("status"), list(proof.get("assumptions") or [])
                label = ("unchanged" if status == "unchanged" else "unknown" if status != "proved"
                         else "proven_with_assumptions" if assumptions else "proven")
                out.append({"model": model, "role": role, "label": label, "reason": proof.get("reason", ""),
                            "assumptions": assumptions})
        return out

    def verdict(self) -> str:
        """The weakest result (``proven``, ``proven_with_assumptions`` or ``unknown``), as Shared models words it."""

        labels = {c["label"] for c in self.check_list()}
        if not self.verified or not labels or "unknown" in labels:
            return "unknown"
        return "proven_with_assumptions" if "proven_with_assumptions" in labels else "proven"

    def to_json(self) -> dict:
        checks = self.check_list()
        assumptions: list[str] = []
        for check in checks:
            assumptions.extend(a for a in check["assumptions"] if a not in assumptions)
        return {
            "keep": self.keep,
            "verified": self.verified,
            "verdict": self.verdict(),
            "checks": checks,
            "assumptions": assumptions,
            "diagnostics": [] if self.verified else list(self.notes),
            "score": {"before": self.score_before, "after": self.score_after},
            "actions": {"before": self.actions_before, "after": self.actions_after},
            "removed": self.removed,
            "changed": self.changed,
            "added": self.added,
            "fixed": self.fixed,
            "dropped_assertions": self.dropped_assertions,
            "moves": self.moves,
            "rejected_moves": self.rejected_moves,
            "tried": self.tried,
            "rejected": self.rejected,
            "files": [{"path": f.path, "action": f.action} for f in self.files],
            "changed_files": [f.path for f in self.files],
            "diff": self.patch(),
            "stopped": self.stopped,
            "seconds": round(self.seconds, 2),
            "notes": self.notes,
            "evidence": "proof: every kept output of the patched project is re-proved equal to the original",
        }


def _apply(root: Path, files: Iterable[FileChange]) -> None:
    for change in files:
        path = root / change.path
        if change.action == "delete":
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(change.after)


# ------------------------------------------------------------------ the project


@dataclass
class _Project:
    root: Path
    pipeline: Pipeline
    texts: dict[str, str]  # model key -> file text
    sql: dict[str, str]  # model key -> SQL with every movable ${...} as a project-wide token
    tokens: dict[str, str]  # token -> the ${...} text it stands for
    movable: dict[str, bool]
    reads: dict[str, set[str]]  # model key -> models it reads or depends on
    ref_text: dict[str, str]  # target key -> how the project writes a ref() to it
    includes: set[str]
    declarations: dict[str, str]  # declaration target key -> its .sqlx path
    js_words: set[str]  # words of the project's .js files: actions they may name
    include_words: set[str] = field(default_factory=set)  # words of includes/*.js: declarations they may name


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _config_block(text: str) -> str:
    try:
        for kind, piece in split_sqlx_sections(text):
            if kind == "block" and piece.lstrip().startswith("config"):
                return piece
    except ValueError:
        pass
    return ""


def _js_locals(text: str) -> set[str]:
    try:
        blocks = [piece for kind, piece in split_sqlx_sections(text) if kind == "block" and piece.lstrip().startswith("js")]
    except ValueError:
        return set()
    return {name for block in blocks for name in _JS_LOCAL.findall(block)}


def _token(expression: str) -> str:
    normal = re.sub(r"\s+", "", expression)
    return f"__kumo_x_{hashlib.sha1(normal.encode('utf-8')).hexdigest()[:12]}__"


def _movable(expression: str, includes: set[str], local: set[str]) -> bool:
    """Whether a ``${...}`` means the same in every file: a project variable or an ``includes/`` constant."""

    if _VARS.match(expression):
        return True
    match = _PATH.match(expression)
    if not match or "(" in expression:
        return False
    root = match.group("root")
    return root in includes and root not in local and root not in _CONTEXT


def _load(root: Path, pipeline: Pipeline | None) -> _Project:
    if pipeline is None:
        pipeline = load_sqlx_project(root)
    includes = {p.stem for p in (root / "includes").glob("*.js")} if (root / "includes").is_dir() else set()
    js_words: set[str] = set()
    for path in (root / "definitions").rglob("*.js") if (root / "definitions").is_dir() else ():
        if "node_modules" in path.parts or ".git" in path.parts:
            continue
        try:
            js_words |= {w.lower() for w in _WORD.findall(_read(path))}
        except (OSError, UnicodeError):
            continue
    include_words: set[str] = set()
    for path in (root / "includes").rglob("*.js") if (root / "includes").is_dir() else ():
        try:
            include_words |= {w.lower() for w in _WORD.findall(_read(path))}
        except (OSError, UnicodeError):
            continue
    texts, sql, tokens, movable, reads = {}, {}, {}, {}, {}
    upstream = pipeline.upstream
    for key, model in pipeline.models.items():
        text = ""
        if model.path:
            try:
                text = _read(root / model.path)
            except (OSError, UnicodeError):
                text = ""
        texts[key] = text
        local = _js_locals(text)
        body = model.sql
        ok = True
        for index, expression in enumerate(model.masked_expressions):
            placeholder = f"__sqlx_token_{index:03d}__"
            if placeholder not in body:
                ok = False
                continue
            token = _token(expression)
            tokens[token] = expression
            body = body.replace(placeholder, token)
            ok = ok and _movable(expression, includes, local)
        if _TOKEN.search(body):
            ok = False
        sql[key] = body
        movable[key] = ok
        reads[key] = {r for r in upstream.get(key, ()) if r in pipeline.models and r != key}
        reads[key] |= {d.key for d in model.declared_dependencies if d.key in pipeline.models and d.key != key}
    known: dict[str, list] = {}
    logical = {model.target: model.logical for model in pipeline.models.values() if model.logical}
    for model in pipeline.models.values():
        known.setdefault(model.logical[2] if model.logical else model.target.name, []).append(model.target)
    for target in pipeline.sources.values():
        known.setdefault(target.name, []).append(target)
    default = type(next(iter(pipeline.models.values())).target)(pipeline.default_project, pipeline.default_dataset, "") \
        if pipeline.models else None
    ref_text: dict[str, str] = {}
    counts: dict[str, dict[str, int]] = {}
    for key, text in texts.items():
        for match in _REF_RE.finditer(text):
            try:
                target = _parse_ref_args(match.group("args"), default, known, logical=logical)
            except ValueError:
                continue
            spelled = counts.setdefault(target.key, {})
            spelled[match.group(0)] = spelled.get(match.group(0), 0) + 1
    for target, spellings in counts.items():
        ref_text[target] = max(spellings.items(), key=lambda item: (item[1], -len(item[0])))[0]
    declarations: dict[str, str] = {}
    search = root / "definitions" if (root / "definitions").is_dir() else root
    for path in search.rglob("*.sqlx"):
        try:
            text = _read(path)
        except (OSError, UnicodeError):
            continue
        config = _config_block(text)
        if _config_value(config, "type") != "declaration":
            continue
        name = _config_value(config, "name") or path.stem
        schema = _config_value(config, "schema") or pipeline.default_dataset
        database = _config_value(config, "database") or pipeline.default_project
        key = ".".join(p for p in (database, schema, name) if p)
        declarations[key] = str(path.relative_to(root)).replace("\\", "/")
    return _Project(root, pipeline, texts, sql, tokens, movable, reads, ref_text, includes, declarations, js_words,
                    include_words)


def _resolve_keep(pipeline: Pipeline, names: Iterable[str]) -> list[str]:
    keys = []
    for name in names:
        spelled = str(name).strip().strip("`")
        if not spelled:
            continue
        if spelled in pipeline.models:
            found = [spelled]
        else:
            lowered = spelled.lower().replace("\\", "/")
            found = [k for k, m in pipeline.models.items()
                     if k.lower() == lowered or k.lower().endswith("." + lowered)
                     or (m.path and m.path.replace("\\", "/").lower() == lowered)]
        if not found:
            if any(k.lower().endswith(spelled.lower()) for k in pipeline.sources):
                raise ReductionError(f"{spelled!r} is a declaration, not an action of the project")
            raise ReductionError(f"{spelled!r} is not an action of the project")
        if len(found) > 1:
            raise ReductionError(f"{spelled!r} names several actions: {', '.join(sorted(found))}")
        if found[0] not in keys:
            keys.append(found[0])
    if not keys:
        raise ReductionError("name at least one output to keep")
    return keys


def _parses(sql: str) -> bool:
    try:
        return isinstance(sqlglot.parse_one(sql, read="bigquery"), exp.Query)
    except sqlglot.errors.SqlglotError:
        return False


def _value_tokens(sql: str) -> bool:
    """Whether a ``${...}`` expression stands for a value (in a string) rather than a name.

    The prover would read the token as one more string constant, different from every other: then
    ``status = "${vars.paid}" AND status = 'paid'`` looks contradictory, although it is not when the
    variable is ``paid``. As a table name a token is sound: two names are proved for any two tables.
    """

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.SqlglotError:
        return bool(_GLOBAL.search(sql))
    return any(isinstance(node, exp.Literal) and _GLOBAL.search(str(node.this)) for node in tree.walk())


def _fixed_reason(project: _Project, key: str) -> str:
    model = project.pipeline.models[key]
    if model.kind == "incremental":
        return "incremental table: its rows depend on earlier runs"
    if model.kind == "operations":
        return "operations script"
    if model.kind not in _QUERY_KINDS and model.kind != "assertion":
        return f"{model.kind} action"
    if model.operations_sql:
        return "has pre or post operations"
    if not project.movable[key]:
        return "uses Dataform expressions that depend on the file they are written in"
    if not _parses(project.sql[key]):
        return "not a single query"
    if _value_tokens(project.sql[key]):
        return "uses a project variable or constant as a value, which the prover cannot read"
    return ""


# ------------------------------------------------------------------ what is needed


@dataclass
class _Effects:
    writes: set[str]  # tables written, as model keys or lowercased names
    routines: set[str]  # routines defined, by last name part


_LOAD = re.compile(r"\bLOAD\s+DATA\s+(?:OVERWRITE|INTO)\s+(?:TEMP(?:ORARY)?\s+TABLE\s+)?([`\w.\-]+)", re.IGNORECASE)
_DROP = re.compile(r"\b(?:DROP|TRUNCATE)\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|EXTERNAL\s+TABLE|SNAPSHOT\s+TABLE)?\s*(?:IF\s+EXISTS\s+)?([`\w.\-]+)", re.IGNORECASE)
_ROUTINE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:AGGREGATE\s+)?(?:TABLE\s+)?(?:FUNCTION|PROCEDURE)\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\w.\-]+)", re.IGNORECASE)


def _name_key(project: _Project, name: str) -> str:
    name = name.strip("`").strip('"')
    parts = [p.strip("`").strip('"') for p in name.split(".")]
    table = exp.Table(this=exp.to_identifier(parts[-1]))
    if len(parts) > 1:
        table.set("db", exp.to_identifier(parts[-2]))
    if len(parts) > 2:
        table.set("catalog", exp.to_identifier(parts[-3]))
    return project.pipeline.resolve(table) or ".".join(parts).lower()


def _effects(project: _Project, key: str) -> _Effects | None:
    """What an operations script (or a model's pre and post operations) writes and defines; ``None`` when unknown."""

    from .scripts import UNKNOWN, analyse_script

    model = project.pipeline.models[key]
    texts = [model.sql] if model.kind == "operations" else list(model.operations_sql)
    effects = _Effects(set(), set())
    for text in texts:
        analysis = analyse_script(text)
        if any(s.disposition == UNKNOWN for s in analysis.statements):
            return None
        if any(s.kind not in _KNOWN_STATEMENTS for s in analysis.statements):
            return None
        for write in analysis.writes:
            effects.writes.add(_name_key(project, write.table.sql(dialect="bigquery")))
        for pattern in (_LOAD, _DROP):
            for match in pattern.finditer(text):
                effects.writes.add(_name_key(project, match.group(1)))
        for match in _ROUTINE.finditer(text):
            effects.routines.add(match.group(1).strip("`").split(".")[-1].lower())
    if _GLOBAL.search(" ".join(effects.writes)) or any("__sqlx" in w for w in effects.writes):
        return None  # a table named by a ${...} expression
    effects.writes.discard(key)
    return effects


def _needed(project: _Project, keep: Sequence[str]) -> tuple[set[str], dict[str, str]]:
    """Every action the kept outputs need, and why each operation that is not read was kept."""

    pipeline = project.pipeline
    table_reads = pipeline.table_reads()
    side: dict[str, _Effects | None] = {}
    for key, model in pipeline.models.items():
        if model.kind == "operations" or model.operations_sql:
            side[key] = _effects(project, key)
    needed = set(keep)
    why: dict[str, str] = {}
    while True:
        stack = list(needed)
        while stack:
            key = stack.pop()
            for read in project.reads.get(key, ()):
                if read not in needed:
                    needed.add(read)
                    stack.append(read)
        names = set(needed)
        for key in needed:
            names |= {str(r).lower() for r in table_reads.get(key, ())}
        words = set()
        for key in needed:
            words |= {w.lower() for w in _WORD.findall(project.sql[key])}
        grew = False
        for key, effects in side.items():
            if key in needed:
                continue
            if effects is None:
                why[key] = "what it writes is not known"
            elif effects.writes & names:
                why[key] = f"writes {sorted(effects.writes & names)[0]}, which a kept action reads"
            elif effects.routines & words:
                why[key] = f"defines {sorted(effects.routines & words)[0]}, which a kept action calls"
            else:
                continue
            needed.add(key)
            grew = True
        if not grew:
            return needed, why


def _dependency_entries(project: _Project, key: str) -> list[tuple[str, set[str]]]:
    """Each entry of ``key``'s config ``dependencies`` as written, with the actions it may name."""

    config = _config_block(project.texts.get(key, ""))
    if not config:
        return []
    by_name: dict[str, list[str]] = {}
    for other, model in project.pipeline.models.items():
        by_name.setdefault(model.target.name.lower(), []).append(other)
    out = []
    for entry in _config_dependencies(config):
        out.append((entry, set(by_name.get(_entry_name(entry), []))))  # ambiguous: every candidate
    return out


def _entry_name(entry: str) -> str:
    name = _config_value(entry, "name") if entry.lstrip().startswith("{") else entry.strip("'\"`")
    return (name or "").lower()


def _asserted_by_name(project: _Project, name: str, among: Iterable[str]) -> set[str]:
    """Tables among ``among`` whose config assertions Dataform names ``name`` (``<schema>_<table>_assertions_...``)."""

    out = set()
    for key in among:
        model = project.pipeline.models[key]
        if re.search(r"\bassertions\s*:", _config_block(project.texts.get(key, ""))) and re.fullmatch(
                rf"(?:\w*_)?{re.escape(model.target.name.lower())}_assertions_\w+", name):
            out.add(key)
    return out


def _config_dependency_keys(project: _Project, key: str) -> set[str]:
    return {k for _entry, keys in _dependency_entries(project, key) for k in keys}


def _inherited_dependencies(project: _Project, key: str, folded: set[str], final: Mapping[str, str]) -> list[str]:
    """``dependencies`` entries of the tables folded into ``key``: it must keep waiting for what they waited for."""

    own = _dependency_entries(project, key)
    have = {k for _entry, keys in own for k in keys}
    have_names = {_entry_name(entry) for entry, _keys in own}
    entries: list[str] = []
    seen: set[str] = set()
    stack = [r for r in project.reads.get(key, ()) if r in folded]
    while stack:
        table = stack.pop()
        if table in seen:
            continue
        seen.add(table)
        for entry, targets in _dependency_entries(project, table):
            if entry in entries or _entry_name(entry) in have_names:
                continue
            if targets and targets <= set(final) and not targets & have and key not in targets:
                entries.append(entry)
            elif not targets and not _asserted_by_name(project, _entry_name(entry), folded):
                entries.append(entry)  # a name the project defines elsewhere (JavaScript): it compiled before
        stack.extend(r for r in project.reads.get(table, ()) if r in folded)
    return entries


# ------------------------------------------------------------------ writing SQL back


def _ref_for(project: _Project, key: str, names: Mapping[str, list[str]], added: Iterable[str] = ()) -> str | None:
    """How a changed query names table ``key``: the project's own ref() text, or ``None`` to keep the name."""

    if key in project.ref_text:
        return project.ref_text[key]
    model = project.pipeline.models.get(key)
    if model is None and key not in project.pipeline.sources and key not in project.declarations and key not in added:
        return None
    if model is not None and model.path and not model.path.endswith(".sqlx"):
        return None
    name = key.split(".")[-1]
    if len(names.get(name.lower(), [])) <= 1:
        return '${ref("%s")}' % name
    parts = key.split(".")
    return '${ref("%s", "%s")}' % (parts[-2], name) if len(parts) > 1 else '${ref("%s")}' % name


def _restore_tokens(text: str, tokens: Mapping[str, str]) -> str:
    def swap(match: re.Match[str]) -> str:
        return tokens.get(match.group(0), match.group(0))

    text = re.sub(r"`(__kumo_x_[0-9a-f]{12}__)`", lambda m: tokens.get(m.group(1), m.group(0)), text)
    return _GLOBAL.sub(swap, text)


def _written_sql(project: _Project, sql: str, resolve: Callable[[exp.Table], str | None],
                 names: Mapping[str, list[str]], added: Iterable[str] = ()) -> str:
    """A query of the reduced project as it goes into a file: ref() to actions, ``${...}`` restored."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    markers: dict[str, str] = {}
    for table in list(tree.find_all(exp.Table)):
        if not table.db and table.name.lower() in ctes:
            continue
        key = resolve(table)
        ref = _ref_for(project, key, names, added) if key is not None else None
        if ref is None:
            parts = [p for p in (table.catalog, table.db, table.name) if p]
            if not any(_GLOBAL.search(p) for p in parts):
                continue
            ref = "`" + ".".join(parts) + "`"  # a name built from ${...}: one quoted path, as written
        marker = f"kumoref{len(markers)}x"
        markers[marker] = ref
        alias = table.args.get("alias")
        replacement = exp.Table(this=exp.to_identifier(marker))
        if alias is not None:
            replacement.set("alias", alias)
        table.replace(replacement)
    text = tree.sql(dialect="bigquery", pretty=True)
    for marker, ref in markers.items():
        text = re.sub(rf"`?\b{marker}\b`?", lambda _m, ref=ref: ref, text)
    return _restore_tokens(text, project.tokens)


_LEADING = re.compile(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)


def _with_body(text: str, sql: str) -> str:
    """``text`` with its SQL replaced by ``sql``: config, js and operation blocks stay byte for byte, and so do
    comments before the query (a licence header, a note above the SELECT)."""

    try:
        pieces = split_sqlx_sections(text)
    except ValueError:
        pieces = [("sql", text)]
    blocks = any(kind == "block" for kind, _ in pieces)
    out: list[str] = []
    placed = False
    for kind, piece in pieces:
        lead = _LEADING.match(piece).group(0)
        if kind == "block" or len(lead) == len(piece):  # a block, or only whitespace and comments
            out.append(piece)
        elif not placed:
            out.append(lead + sql.rstrip() + "\n")
            placed = True
        else:
            out.append(lead)
    if not placed:
        out.append(("\n\n" if blocks else "") + sql.rstrip() + "\n")
    return "".join(out)


def _add_dependencies(block: str, entries: Sequence[str]) -> str:
    """A config block with ``entries`` added to its ``dependencies`` (created when it has none)."""

    top = _top_level(block)
    match = re.search(r"(?<![\w$.])dependencies\s*:\s*", top)
    added = ", ".join(entries)
    if match is None:
        opening = block.index("{")
        return block[:opening + 1] + f"\n  dependencies: [{added}]," + block[opening + 1:]
    start = match.end()
    if block[start] == "[":
        depth, index, quote = 0, start, ""
        while index < len(block):
            char = block[index]
            if quote:
                if char == "\\":
                    index += 1
                elif char == quote:
                    quote = ""
            elif char in "'\"`":
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        inner = block[start + 1:index].rstrip()
        separator = "" if not inner.strip() else (" " if inner.endswith(",") else ", ")
        return block[:start + 1] + inner + separator + added + block[index:]
    end = start
    while end < len(top) and top[end] not in ",\n}":
        end += 1
    return block[:start] + "[" + block[start:end].strip() + ", " + added + "]" + block[end:]


def _raw_config_value(config: str, key: str) -> str | None:
    """The source text of a top-level config value (a string literal or an expression), or ``None``."""

    top = _top_level(config)
    match = re.search(rf"(?<![\w$.]){key}\s*:\s*", top)
    if not match:
        return None
    end = match.end()
    while end < len(top) and top[end] not in ",\n}":
        end += 1
    value = config[match.end():end].strip()
    return value or None


def _new_file(project: _Project, name: str, sql: str, readers: Sequence[str], table_type: str) -> tuple[str, str]:
    """``(path, text)`` for a shared table factored out of ``readers``."""

    first = project.pipeline.models[readers[0]]
    directory = posixpath.dirname((first.path or "definitions/x.sqlx").replace("\\", "/"))
    suffix = ".sqlx" if (first.path or ".sqlx").endswith(".sqlx") else ".sql"
    path = posixpath.join(directory, name + suffix)
    n = 2
    while (project.root / path).exists():
        path = posixpath.join(directory, f"{name}_{n}{suffix}")
        n += 1
    if suffix == ".sql":
        return path, sql.rstrip() + "\n"
    config = _config_block(project.texts.get(readers[0], ""))
    lines = [f'  type: "{table_type}"']
    for key in ("database", "schema"):
        value = _raw_config_value(config, key) if config else None
        if value:
            lines.append(f"  {key}: {value}")
    tags = []
    for reader in readers:
        for tag in project.pipeline.models[reader].tags:
            if tag not in tags:
                tags.append(tag)
    if tags:
        lines.append("  tags: [" + ", ".join('"%s"' % t.replace('"', '\\"') for t in tags) + "]")
    return path, "config {\n" + ",\n".join(lines) + "\n}\n\n" + sql.rstrip() + "\n"


# ------------------------------------------------------------------ scoring and checking


def project_score(pipeline: Pipeline) -> float:
    """Complexity of a whole project: the sqlfluff structural score of every action's SQL, plus one per action."""

    return round(sum(_structural(m.sql) for m in pipeline.models.values()) + len(pipeline.models), 2)


def _sources(project: _Project, extra: Mapping[str, object] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in project.pipeline.sources:
        columns = project.pipeline.source_schema.get(key)
        out[key] = {"columns": dict(columns)} if columns else None
    for key, columns in (project.pipeline.source_schema or {}).items():
        if key not in project.pipeline.models:
            out.setdefault(key, {"columns": dict(columns)})
    for key, spec in (extra or {}).items():
        out[key] = spec
    return {k: v for k, v in out.items() if v is not None and k not in project.pipeline.models}


def _columns_of(project: _Project, key: str) -> list[str] | None:
    try:
        columns = list(project.pipeline.output_columns(key))
    except Exception:  # noqa: BLE001 - columns are optional evidence
        return None
    return columns or None


def _reloaded_tables(project: _Project, keys: Iterable[str]) -> dict[str, str]:
    return {key: project.sql[key] for key in keys if key in project.sql}


# ------------------------------------------------------------------ the reduction


def reduce_project(
    root: str | Path,
    keep: Iterable[str],
    *,
    pipeline: Pipeline | None = None,
    keep_assertions: bool = False,
    strict: bool = False,
    rewrite: bool = True,
    factor: bool = True,
    new_table_type: str = "view",
    source_columns: Mapping[str, object] | None = None,
    timeout_ms: int = 5000,
    max_seconds: float = 300.0,
    progress: Callable[[str], None] | None = None,
) -> ProjectReduction:
    """The smallest project found that still produces every kept output, as a proved patch.

    ``keep`` names actions by key (``project.dataset.name``), ``dataset.name``, name or file path. With
    ``strict``, every table that survives (not only those with assertions) must stay proved equal to the
    original on the columns it keeps. With ``rewrite=False`` only what the kept outputs do not need is
    deleted; no query is changed. ``source_columns`` adds columns (and keys) of declared sources, in the
    format :func:`kumosql.table_minimizer.minimize_tables` takes. Raises :class:`ReductionError` on a kept
    output that is unknown, ambiguous or a declaration.
    """

    started = time.time()
    root = Path(root)
    project = _load(root, pipeline)
    models = project.pipeline.models
    kept = _resolve_keep(project.pipeline, keep)
    say = progress or (lambda _line: None)

    needed, operation_why = _needed(project, kept)
    fixed_why = {key: _fixed_reason(project, key) for key in needed}
    fixed_why = {k: v for k, v in fixed_why.items() if v}

    # assertions: one another kept action waits for (config dependencies) is needed already
    dropped_assertions: list[dict] = []
    protected: list[str] = [k for k in kept if k not in fixed_why]
    stable = set(kept) | {k for k in needed if k in fixed_why}
    assertions_kept: dict[str, str] = {}
    for key, model in models.items():
        if model.kind != "assertion" or key in kept:
            continue
        reads = project.reads.get(key, set())
        if key in needed:
            assertions_kept[key] = "unchanged" if key in fixed_why else "protected"
        elif reads and reads <= needed and (keep_assertions or reads <= stable):
            assertions_kept[key] = "protected" if keep_assertions and not reads <= stable else "unchanged"
            needed.add(key)
        else:
            gone = sorted(reads - needed)
            changing = sorted(reads - stable)
            why = (f"reads {gone[0]}, which is no longer in the project" if gone
                   else f"reads {changing[0]}, which the reduction may rewrite" if changing
                   else "reads nothing the kept outputs need")
            dropped_assertions.append({"model": key, "path": models[key].path, "why": why})
            needed.discard(key)
    for key, how in assertions_kept.items():
        reason = _fixed_reason(project, key)
        if reason:
            fixed_why[key] = reason
        elif how == "protected" and key not in protected:
            protected.append(key)
        elif how == "unchanged":
            fixed_why[key] = "assertion over actions that stay as they are"

    # actions read from JavaScript stay as they are
    js_named = {k for k in needed if models[k].target.name.lower() in project.js_words}
    for key in js_named:
        if key not in protected and key not in fixed_why:
            protected.append(key)

    # surviving actions keep the actions their config lists in dependencies, and the tables whose config
    # assertions they wait for
    for key in sorted(needed):
        for entry, targets in _dependency_entries(project, key):
            targets = targets or _asserted_by_name(project, _entry_name(entry), needed)
            for dep in sorted(targets):
                if dep in needed and dep not in protected and dep not in fixed_why:
                    protected.append(dep)

    # what the minimizer works on
    tables: dict[str, str] = {}
    fixed: dict[str, list[str] | None] = {}
    checked: list[str] = []
    keep_columns: dict[str, list[str]] = {}
    for key in sorted(needed):
        tables[key] = project.sql[key]
        if key in fixed_why:
            fixed[key] = _columns_of(project, key)
            continue
        config = _config_block(project.texts.get(key, ""))
        columns = _columns_of(project, key) or []
        if config:
            words = {w.lower() for w in _WORD.findall(config)}
            named = [c for c in columns if c.lower() in words]
            if named:
                keep_columns[key] = named
            if re.search(r"\bassertions\s*:", config):
                checked.append(key)
        if strict and key not in protected:
            checked.append(key)
    sources = _sources(project, source_columns)
    say(f"{len(needed)} of {len(models)} actions are needed; {len(fixed)} stay as written")

    minimized = None
    notes: list[str] = []
    if rewrite:
        try:
            minimized = minimize_tables(
                tables, protected, sources=sources, fixed=fixed, checked=checked, keep_columns=keep_columns,
                factor=factor, timeout_ms=timeout_ms, max_seconds=max(1.0, max_seconds - (time.time() - started)),
                progress=progress, lower_score_only=True,
            )
        except MinimizationError as error:
            notes.append(f"queries were not rewritten: {error}")

    result = _build(project, kept, needed, protected, fixed_why, operation_why, dropped_assertions, minimized,
                    new_table_type, notes)
    _verify(project, result, tables, protected, fixed, checked, sources, timeout_ms)
    if not result.verified and minimized is not None and minimized.moves:
        notes.append("the rewritten queries did not re-prove after writing them back; only unneeded actions were removed")
        result = _build(project, kept, needed, protected, fixed_why, operation_why, dropped_assertions, None,
                        new_table_type, notes)
        _verify(project, result, tables, protected, fixed, checked, sources, timeout_ms)
    result.seconds = time.time() - started
    return result


def _removal_reasons(moves: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for move in moves:
        match = re.match(r"(fold|merge|drop) (\S+)(?: into (.+?))?(?: \(unsimplified\))?$", move)
        if not match:
            continue
        verb, table, into = match.groups()
        if verb == "fold":
            out[table] = f"folded into {into}"
        elif verb == "merge":
            out[table] = f"merged into {into}, which returns the same rows"
        else:
            out[table] = "no longer read"
        if move.startswith("fold every unprotected table"):
            pass
    return out


def _build(project: _Project, kept, needed, protected, fixed_why, operation_why, dropped_assertions,
           minimized, table_type: str, notes: list[str]) -> ProjectReduction:
    models = project.pipeline.models
    final: dict[str, str] = {k: project.sql[k] for k in needed}
    added: dict[str, str] = {}
    moves: list[str] = []
    rejected: list[dict] = []
    stopped = ""
    tried = rejected_count = 0
    if minimized is not None:
        final = {k: v for k, v in minimized.tables.items() if k in needed}
        added = {k: v for k, v in minimized.tables.items() if k in minimized.added}
        moves, rejected, stopped = minimized.moves, minimized.rejected_moves, minimized.stopped
        tried, rejected_count = minimized.tried, minimized.rejected
    reasons = _removal_reasons(moves)
    removed: list[dict] = []
    for key, model in models.items():
        if key in final:
            continue
        if key in needed:
            why = reasons.get(key) or "folded into its readers"
        elif any(a["model"] == key for a in dropped_assertions):
            continue
        else:
            why = "not needed by any kept output"
        removed.append({"model": key, "path": model.path, "kind": model.kind, "why": why})

    resolve_keys = {**{k: k for k in models}, **{k: k for k in added}}
    names: dict[str, list[str]] = {}
    for key in [*models, *project.pipeline.sources, *added]:
        names.setdefault(key.split(".")[-1].lower(), []).append(key)

    def resolve(table: exp.Table) -> str | None:
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        key = ".".join(parts)
        lowered = {k.lower(): k for k in resolve_keys}
        if key.lower() in lowered:
            return lowered[key.lower()]
        return project.pipeline.resolve(table)

    files: list[FileChange] = []
    changed: list[str] = []
    folded = {k for k in needed if k not in final}
    for key, sql in final.items():
        if sql == project.sql[key]:
            continue
        model = models[key]
        text = project.texts.get(key, "")
        new_text = _with_body(text, _written_sql(project, sql, resolve, names, added))
        inherited = _inherited_dependencies(project, key, folded, final)
        config = _config_block(new_text)
        if inherited and config:
            new_text = new_text.replace(config, _add_dependencies(config, inherited), 1)
        files.append(FileChange(model.path, "modify", text, new_text))
        changed.append(key)
    added_out: list[dict] = []
    for key, sql in added.items():
        readers = sorted(k for k, s in final.items() if key.split(".")[-1].lower() in s.lower())
        readers = [r for r in readers if models[r].path] or [k for k in final if models[k].path][:1]
        path, text = _new_file(project, key.split(".")[-1], _written_sql(project, sql, resolve, names, added), readers, table_type)
        files.append(FileChange(path, "add", "", text))
        added_out.append({"model": key, "path": path, "readers": readers})
    for entry in list(removed):
        if re.search(r"\bassertions\s*:", _config_block(project.texts.get(entry["model"], ""))):
            why = ("its table is not needed by any kept output" if entry["why"].startswith("not needed")
                   else f"its table was {entry['why']}")
            dropped_assertions = [*dropped_assertions,
                                  {"model": entry["model"], "path": None, "config": True, "why": f"config assertions: {why}"}]
    for entry in removed + dropped_assertions:
        if entry.get("path"):
            files.append(FileChange(entry["path"], "delete", project.texts.get(entry["model"], ""), ""))
    # a declaration stays while anything that stays may name it: its SQL, its file as written (with the
    # ${...} expressions), or JavaScript
    surviving_text = " ".join([*final.values(), *added.values(), *(project.texts.get(k, "") for k in final)]).lower()
    for key, path in sorted(project.declarations.items()):
        name = key.split(".")[-1].lower()
        if name in project.js_words or name in project.include_words or re.search(rf"(?<!\w){re.escape(name)}(?!\w)", surviving_text):
            continue
        try:
            before = _read(project.root / path)
        except (OSError, UnicodeError):
            continue
        files.append(FileChange(path, "delete", before, ""))
        removed.append({"model": key, "path": path, "kind": "declaration", "why": "no action that stays reads it"})
    files.sort(key=lambda f: (f.path, f.action))
    fixed = [{"model": k, "why": f"kept: {operation_why[k]}" if k in operation_why else v} for k, v in sorted(fixed_why.items())]
    fixed += [{"model": k, "why": f"kept: {v}"} for k, v in sorted(operation_why.items()) if k not in fixed_why]
    proofs = {}
    if minimized is not None:
        for name, proof in minimized.proofs.items():
            proofs[name] = proof.to_json()
    for key in kept:
        proofs.setdefault(key, {"table": key, "status": "unchanged", "reason": "its SQL and every table it reads are as given",
                                "assumptions": []})
    return ProjectReduction(
        root=str(project.root), keep=list(kept), files=files, proofs={k: proofs[k] for k in kept if k in proofs},
        removed=sorted(removed, key=lambda r: r["model"]), changed=sorted(changed), added=added_out, fixed=fixed,
        dropped_assertions=dropped_assertions, moves=moves, rejected_moves=rejected, tried=tried, rejected=rejected_count,
        score_before=project_score(project.pipeline), actions_before=len(models), stopped=stopped, notes=list(notes),
    )


def _copy_project(source: Path, target: Path) -> None:
    for item in source.iterdir():
        if item.name in {".git", "node_modules"}:
            continue
        if item.is_dir():
            shutil.copytree(item, target / item.name, ignore=shutil.ignore_patterns(".git", "node_modules"))
        else:
            shutil.copy2(item, target / item.name)


def _verify(project: _Project, result: ProjectReduction, tables, protected, fixed, checked, sources, timeout_ms) -> None:
    """Apply the patch to a copy, load it again and re-prove every kept output against the original."""

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project"
        copy.mkdir()
        _copy_project(project.root, copy)
        _apply(copy, result.files)
        try:
            reloaded = _load(copy, None)
        except Exception as error:  # noqa: BLE001 - a patched project that does not load is not verified
            result.notes.append(f"the patched project does not load: {type(error).__name__}: {error}")
            result.verified = False
            return
    before_codes = {(d.code, d.model) for d in project.pipeline.diagnostics}
    new_codes = [d for d in reloaded.pipeline.diagnostics
                 if (d.code, d.model) not in before_codes and d.code not in {"duplicate_model"}]
    result.score_after = project_score(reloaded.pipeline)
    result.actions_after = len(reloaded.pipeline.models)
    if new_codes:
        result.notes.append(f"the patched project has new load problems: {new_codes[0].code} in {new_codes[0].model}")
        result.verified = False
        return
    missing = [k for k in result.keep if k not in reloaded.pipeline.models]
    if missing:
        result.notes.append(f"kept outputs missing from the patched project: {missing}")
        result.verified = False
        return
    after = {k: reloaded.sql[k] for k in reloaded.pipeline.models}
    after_fixed = {k: fixed.get(k) for k in fixed if k in after}
    try:
        verdicts = verify_tables(tables, after, protected, sources=sources, timeout_ms=timeout_ms, fixed=after_fixed,
                                 checked=[c for c in checked if c in after])
    except MinimizationError as error:
        result.notes.append(f"the patched project could not be checked: {error}")
        result.verified = False
        return
    ok = True
    for name in result.keep:
        verdict = verdicts.get(name)
        if verdict is None:
            if name in fixed and after.get(name) == tables.get(name):
                continue
            ok = False
            continue
        if verdict.status not in ("unchanged", "proved"):
            ok = False
            result.notes.append(f"{name} did not re-prove: {verdict.reason}"[:300])
        else:
            result.proofs[name] = verdict.to_json()
    for name in checked:
        if name in after and name in tables and name not in protected:
            verdict = verdicts.get(name)
            if verdict is None or verdict.status not in ("unchanged", "proved"):
                ok = False
                result.notes.append(f"{name} has assertions and did not re-prove")
            else:
                result.checks[name] = verdict.to_json()
    for name, verdict in verdicts.items():  # tables that actions kept as written read
        if name not in result.keep and name not in checked and verdict.status not in ("unchanged", "proved"):
            ok = False
            result.notes.append(f"{name}, which an action kept as written reads, did not re-prove: {verdict.reason}"[:300])
    result.verified = ok


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql reduce-project DIR --keep NAME [--keep NAME ...]``"""

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m kumosql reduce-project",
        description=__doc__.split("\n")[0] + " PREVIEW BY DEFAULT: it prints the result and changes no file. "
        "Only --write edits the project folder, and only when the reduction verified.")
    parser.add_argument("project", help="Dataform project folder (or a folder of .sql files)")
    parser.add_argument("--keep", action="append", default=[], help="an output to keep: name, dataset.name or file path")
    parser.add_argument("--keep-assertions", action="store_true", help="keep every assertion over needed tables, proved unchanged")
    parser.add_argument("--strict", action="store_true", help="every surviving table must stay proved equal")
    parser.add_argument("--drop-only", action="store_true", help="only delete what the kept outputs do not need")
    parser.add_argument("--no-factor", action="store_true", help="never move repeated queries into a shared table")
    parser.add_argument("--table-type", default="view", choices=("view", "table"), help="type of a new shared table")
    parser.add_argument("--max-seconds", type=float, default=300.0)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--patch", help="write the unified diff here (- for stdout)")
    parser.add_argument("--write", action="store_true", help="the only option that edits the project folder: apply the patch (without it, nothing is written)")
    args = parser.parse_args(argv)
    try:
        result = reduce_project(
            args.project, args.keep, keep_assertions=args.keep_assertions, strict=args.strict, rewrite=not args.drop_only,
            factor=not args.no_factor,
            new_table_type=args.table_type, max_seconds=args.max_seconds, timeout_ms=args.timeout_ms,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.patch == "-":
        sys.stdout.write(result.patch())
    else:
        if args.patch:
            Path(args.patch).write_text(result.patch(), encoding="utf-8")
        payload = result.to_json()
        payload.pop("diff")
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    if args.write:
        if not result.verified:
            print("error: the reduction did not verify; nothing was written", file=sys.stderr)
            return 1
        result.apply(args.project)
    return 0 if result.verified else 1
