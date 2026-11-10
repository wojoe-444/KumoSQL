"""Refactor a pipeline while some tables must stay: PROTECTED, EDITABLE and everything else.

Each model of a Dataform project is in one of three classes, saved like scopes (see
:mod:`kumosql.scopes`; a class is a list of saved scopes plus explicit model names):

* **protected**: the table must keep existing and return the same rows. Its SQL may change.
* **editable**: may be deleted, merged into another model, inlined into its readers or rewritten.
* **frozen** (everything else): read as it is, never modified. A model a frozen model reads is
  *exposed*: it is observable like a protected one, because the frozen model cannot be edited.

``search`` explores moves (drop an unread editable model, inline an editable model into its
readers, merge two editable models that return the same columns) and keeps a state only when
every observable model of the new pipeline is **proved** equal to the original
(:func:`kumosql.pipeline_equivalence.prove_models`; a result that is not proved is rejected, so
unknown never passes). The states kept are reduced to the Pareto front over total sqlfluff
complexity (:func:`kumosql.formatting.complexity`) and model count, both to be minimised.
Proofs carry the prover's assumptions and any saved equivalences they relied on.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
import time
from typing import Callable, Iterable, Mapping

import sqlglot
from sqlglot import exp

from . import scopes as scope_store
from . import state
from kumo_sql_prover.ast_utils import captured_names, is_cte_reference
from .pipeline_equivalence import prove_models
from .prover_schema import ProverSchema, _select_names

SECTION = "refactor"
CLASSES = ("protected", "editable")
MAX_INLINE_CHARS = 200_000
#: Weights on model count against complexity for the descents (0: complexity only, large: model count only).
WEIGHTS = (0.0, 2.0, 5.0, 10.0, 25.0, 1000.0)
SUFFIX = "__after"


# ------------------------------------------------------------ saved classes


@dataclass(frozen=True)
class Selection:
    scopes: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"scopes": list(self.scopes), "models": list(self.models)}


@dataclass(frozen=True)
class Classes:
    protected: Selection = Selection()
    editable: Selection = Selection()

    def to_json(self) -> dict:
        return {"protected": self.protected.to_json(), "editable": self.editable.to_json()}


def _selection(data: object, label: str) -> Selection:
    if data is None:
        return Selection()
    if not isinstance(data, Mapping):
        raise ValueError(f"{label} must be an object with scopes and models")
    out = {}
    for field_name in ("scopes", "models"):
        items = data.get(field_name, [])
        if not isinstance(items, list) or any(not isinstance(i, str) or not i.strip() for i in items):
            raise ValueError(f"{label}.{field_name} must be a list of names")
        if len(items) > 5000:
            raise ValueError(f"{label}.{field_name} is too long")
        out[field_name] = tuple(dict.fromkeys(i.strip() for i in items))
    return Selection(out["scopes"], out["models"])


def parse_classes(data: object) -> Classes:
    if not isinstance(data, Mapping):
        raise ValueError("refactor classes must be an object")
    classes = Classes(_selection(data.get("protected"), "protected"), _selection(data.get("editable"), "editable"))
    known = {scope.name.casefold() for scope in scope_store.list_scopes()}
    for label in CLASSES:
        for name in getattr(classes, label).scopes:
            if name.casefold() not in known:
                raise ValueError(f"{label}: there is no saved scope called {name!r}")
    return classes


def load_classes() -> Classes:
    try:
        return parse_classes(state.get_section(SECTION, {}) or {})
    except ValueError:
        return Classes()  # a scope was deleted since: the model names still apply


def save_classes(data: object) -> Classes:
    classes = parse_classes(data)
    state.set_section(SECTION, classes.to_json())
    return classes


def _matches(pipeline, key: str, selection: Selection, cache: dict) -> bool:
    wanted = {name.casefold() for name in selection.models}
    model = pipeline.models[key]
    if key.casefold() in wanted or model.target.name.casefold() in wanted:
        return True
    for name in selection.scopes:
        scope = scope_store.get_scope(name)
        if scope is None:
            continue
        if "record" not in cache or cache["record"][0] != key:
            cache["record"] = (key, pipeline.model_record(key))
        try:
            if scope.matches(cache["record"][1]):
                return True
        except ValueError:
            continue
    return False


def classify(pipeline, classes: Classes) -> dict[str, str]:
    """``{model key: "protected" | "editable" | "frozen"}``; protected wins, and models that are not queries are frozen."""

    result = {}
    cache: dict = {}
    for key, model in pipeline.models.items():
        if not model.is_query or model.kind == "assertion":
            result[key] = "frozen"
        elif _matches(pipeline, key, classes.protected, cache):
            result[key] = "protected"
        elif _matches(pipeline, key, classes.editable, cache):
            result[key] = "editable"
        else:
            result[key] = "frozen"
    return result


# ------------------------------------------------------------- SQL helpers


def _parse(sql: str) -> exp.Expression:
    """One statement; text with several (a script) is refused, as older sqlglot keeps only the first of them."""

    statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    if len(statements) != 1:
        raise sqlglot.errors.ParseError("expected one statement")
    return statements[0]


def _table_nodes(tree: exp.Expression):
    """The physical table reads of ``tree``: every table node that does not read a WITH table in scope there."""

    for table in tree.find_all(exp.Table):
        if not is_cte_reference(table):
            yield table


class _Reads:
    """Which pipeline models each SQL text reads (cached per text)."""

    def __init__(self, pipeline) -> None:
        self.pipeline = pipeline
        self.cache: dict[str, frozenset[str]] = {}

    def __call__(self, sql: str) -> frozenset[str]:
        if sql not in self.cache:
            found = set()
            try:
                for table in _table_nodes(_parse(sql)):
                    key = self.pipeline.resolve(table)
                    if key in self.pipeline.models:
                        found.add(key)
            except sqlglot.errors.SqlglotError:
                pass
            self.cache[sql] = frozenset(found)
        return self.cache[sql]


def _replace_tables(sql: str, resolve, replace_with: Callable[[exp.Table, str], exp.Expression | None]) -> str:
    tree = _parse(sql)
    for table in list(_table_nodes(tree)):
        key = resolve(table)
        if key is None:
            continue
        new = replace_with(table, key)
        if new is not None:
            table.replace(new)
    return tree.sql(dialect="bigquery")


def _table_for(key: str, alias: str) -> exp.Table:
    parts = key.split(".")
    node = exp.Table(this=exp.to_identifier(parts[-1]))
    if len(parts) > 1:
        node.set("db", exp.to_identifier(parts[-2]))
    if len(parts) > 2:
        node.set("catalog", exp.to_identifier(".".join(parts[:-2])))
    node.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
    return node


def _alias_of(table: exp.Table, key: str) -> str:
    return table.alias or key.split(".")[-1]


class _Captured(ValueError):
    """The move would put a table read where a WITH table of the reader captures its name."""


def _inline_into(reader_sql: str, target: str, body_sql: str, resolve) -> str:
    body = _parse(body_sql)

    def swap(table: exp.Table, key: str):
        if key != target:
            return None
        if captured_names(body, table):
            raise _Captured(target)
        return exp.Subquery(this=body.copy(), alias=exp.TableAlias(this=exp.to_identifier(_alias_of(table, key))))

    return _replace_tables(reader_sql, resolve, swap)


def _redirect(reader_sql: str, old: str, new: str, resolve) -> str:
    def swap(table: exp.Table, key: str):
        if key != old:
            return None
        if captured_names(_table_for(new, "x"), table):
            raise _Captured(new)
        return _table_for(new, _alias_of(table, key))

    return _replace_tables(reader_sql, resolve, swap)


# ------------------------------------------------------------------ costs


class _Complexity:
    def __init__(self) -> None:
        self.cache: dict[str, float] = {}
        self.unscored = 0

    def __call__(self, sql: str) -> float:
        if sql not in self.cache:
            from .formatting import complexity

            try:
                self.cache[sql] = float(complexity(sql).score)
            except Exception:  # noqa: BLE001 - sqlfluff cannot parse it: fall back to a size proxy
                self.unscored += 1
                self.cache[sql] = round(len(sql) / 40, 1)
        return self.cache[sql]


@dataclass(frozen=True)
class RefactorState:
    sql: tuple[tuple[str, str], ...]  # present models and their current SQL, sorted by key
    moves: tuple[str, ...] = ()
    complexity: float = 0.0
    models: int = 0
    assumptions: tuple[str, ...] = ()

    @property
    def sql_map(self) -> dict[str, str]:
        return dict(self.sql)

    def cost(self) -> tuple[float, int]:
        return (self.complexity, self.models)


def _dominates(a: RefactorState, b: RefactorState) -> bool:
    return a.cost()[0] <= b.cost()[0] and a.cost()[1] <= b.cost()[1] and a.cost() != b.cost()


# ---------------------------------------------------------------- the check


def _combined_pipeline(pipeline, candidate: Mapping[str, str], reads: _Reads, observable: Iterable[str] | None = None):
    """The original models plus an ``__after`` copy of every candidate model that changed or reads one that did.

    With ``observable`` the pipeline holds only what those models (and their copies) read, so a check costs
    what it touches, not the size of the project.
    """

    changed = {k for k, sql in candidate.items() if pipeline.models[k].sql != sql}
    affected = set(changed)
    grew = True
    while grew:
        grew = False
        for key, sql in candidate.items():
            if key not in affected and reads(sql) & affected:
                affected.add(key)
                grew = True
    keep: set[str] | None = None
    if observable is not None:
        keep = set()
        todo = [k for k in observable if k in affected]
        seen_after: set[str] = set()
        while todo:
            key = todo.pop()
            if key in seen_after:
                continue
            seen_after.add(key)
            keep.add(key)
            todo.extend(r for r in reads(candidate[key]) if r in candidate and r in affected)
        todo = [*keep]
        while todo:  # originals of every affected model, and everything either version reads
            key = todo.pop()
            for read in [*reads(pipeline.models[key].sql), *(reads(candidate[key]) if key in candidate else ())]:
                if read in pipeline.models and read not in keep:
                    keep.add(read)
                    todo.append(read)
        affected = {k for k in affected if k in keep}
    models = {k: m for k, m in pipeline.models.items() if keep is None or k in keep}
    renamed = {}
    for key in affected:
        original = pipeline.models[key]
        after_key = key + SUFFIX
        renamed[key] = after_key
        target = replace(original.target, name=original.target.name + SUFFIX)
        models[after_key] = replace(original, target=target, sql="", path=None, declared_dependencies=())
    for key in affected:
        sql = candidate[key]
        sql = _replace_tables(
            sql, pipeline.resolve,
            lambda table, found: (
                _after_table(table, models[renamed[found]]) if found in renamed else None
            ),
        )
        models[renamed[key]] = replace(models[renamed[key]], sql=sql)
    combined = type(pipeline)(
        models=models, sources=pipeline.sources, source_schema=pipeline.source_schema,
        default_project=pipeline.default_project, default_dataset=pipeline.default_dataset,
    )
    return combined, renamed


def _after_table(table: exp.Table, model) -> exp.Expression:
    return _table_for(model.target.key, table.alias or table.name)


def check_observable(
    pipeline, candidate: Mapping[str, str], observable: Iterable[str], reads: _Reads,
    *, schema: ProverSchema | None = None, timeout_ms: int = 5000, cache: dict | None = None, declared: list | None = None,
) -> tuple[bool, list[str], str]:
    """``(every observable model proved equal, assumptions, why not)`` for a candidate pipeline.

    ``declared`` are the saved equivalences the prover may use (default: the ones saved in the app).
    """

    observable = list(observable)
    missing = [key for key in observable if key not in candidate]
    if missing:
        return False, [], f"{missing[0]} no longer exists"
    combined, renamed = _combined_pipeline(pipeline, candidate, reads, observable)
    assumptions: list[str] = []
    for key in observable:
        if key not in renamed:
            continue  # neither it nor anything upstream changed
        # a proof depends only on the SQL of the models the observable reads, so it is shared by every
        # candidate that leaves those models as they are
        signature = (key, _closure_signature(candidate, key, reads)) if cache is not None else None
        hit = cache.get(signature) if cache is not None else None
        if hit is None:
            result = prove_models(combined, key, renamed[key], declared=declared, schema=schema, timeout_ms=timeout_ms,
                                  edited_copy=True)
            hit = (result.proven, [*result.assumptions, *result.equivalences], f"{key}: {result.reason}")
            if cache is not None:
                cache[signature] = hit
        if not hit[0]:
            return False, [], hit[2]
        assumptions.extend(a for a in hit[1] if a not in assumptions)
    return True, assumptions, ""


def _closure_signature(candidate: Mapping[str, str], key: str, reads: _Reads) -> tuple:
    seen: dict[str, str] = {}
    todo = [key]
    while todo:
        model = todo.pop()
        if model in seen or model not in candidate:
            continue
        seen[model] = candidate[model]
        todo.extend(reads(candidate[model]))
    return tuple(sorted(seen.items()))


# ------------------------------------------------------------------- moves


@dataclass
class _Setup:
    pipeline: object
    roles: dict[str, str]
    observable: frozenset[str]
    reads: _Reads
    score: _Complexity
    schema: ProverSchema | None
    timeout_ms: int
    cache: dict = field(default_factory=dict)


def _modifiable(setup: _Setup, key: str) -> bool:
    return setup.roles.get(key) in ("protected", "editable")


def _moves(setup: _Setup, current: Mapping[str, str]):
    """``(label, new SQL map)`` for every move that is allowed in ``current``."""

    resolve = setup.pipeline.resolve
    readers: dict[str, set[str]] = {key: set() for key in current}
    for key, sql in current.items():
        for read in setup.reads(sql):
            if read in readers and read != key:
                readers[read].add(key)
    names = {key: tuple(c.lower() for c in _select_names(sql)) for key, sql in current.items()}

    for key in sorted(current):
        if setup.roles.get(key) != "editable" or key in setup.observable:
            continue
        users = readers[key]
        if not users:
            yield f"drop {key}", {k: v for k, v in current.items() if k != key}
            continue
        if all(_modifiable(setup, user) for user in users):
            try:
                changed = {user: _inline_into(current[user], key, current[key], resolve) for user in users}
            except (sqlglot.errors.SqlglotError, _Captured):
                continue
            if all(len(sql) <= MAX_INLINE_CHARS for sql in changed.values()):
                new = {k: changed.get(k, v) for k, v in current.items() if k != key}
                yield f"inline {key} into {', '.join(sorted(users))}", new
    # merge: an editable model that returns the same columns from the same tables as another model
    groups: dict[tuple, list[str]] = {}
    for key in sorted(current):
        if names[key]:
            groups.setdefault((names[key], setup.reads(current[key])), []).append(key)
    for members in groups.values():
        for key in members:
            if setup.roles.get(key) != "editable" or key in setup.observable:
                continue
            for other in members:
                if other == key or _depends_on(setup, current, other, key):
                    continue
                users = readers[key]
                if not all(_modifiable(setup, user) for user in users):
                    continue
                try:
                    changed = {user: _redirect(current[user], key, other, resolve) for user in users}
                except (sqlglot.errors.SqlglotError, _Captured):
                    continue
                new = {k: changed.get(k, v) for k, v in current.items() if k != key}
                yield f"merge {key} into {other}", new
                break  # one target per model is enough: the others are equivalent copies


def _depends_on(setup: _Setup, current: Mapping[str, str], model: str, target: str) -> bool:
    seen, todo = set(), [model]
    while todo:
        key = todo.pop()
        if key == target:
            return True
        if key in seen or key not in current:
            continue
        seen.add(key)
        todo.extend(setup.reads(current[key]))
    return False


def _state(setup: _Setup, sql: Mapping[str, str], moves: tuple[str, ...], assumptions: tuple[str, ...]) -> RefactorState:
    total = sum(setup.score(text) for text in sql.values())
    return RefactorState(tuple(sorted(sql.items())), moves, round(total, 2), len(sql), assumptions)


# ------------------------------------------------------------------ search


@dataclass
class RefactorResult:
    roles: dict[str, str]
    observable: list[str]
    baseline: RefactorState
    front: list[RefactorState]
    tried: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    rejected_moves: list[dict] = field(default_factory=list)
    stopped: str = ""
    seconds: float = 0.0
    unscored: int = 0

    def to_json(self, pipeline=None) -> dict:
        original = {k: m.sql for k, m in pipeline.models.items()} if pipeline is not None else {}

        def item(entry: RefactorState) -> dict:
            now = entry.sql_map
            return {
                "moves": list(entry.moves),
                "complexity": entry.complexity,
                "models": entry.models,
                "deleted": sorted(k for k in self.baseline.sql_map if k not in now),
                "changed": {k: v for k, v in sorted(now.items()) if original.get(k) is not None and original[k] != v},
                "assumptions": list(entry.assumptions),
            }

        counts = {label: sum(1 for r in self.roles.values() if r == label) for label in ("protected", "editable", "frozen")}
        return {
            "classes": counts,
            "observable": self.observable,
            "baseline": {"complexity": self.baseline.complexity, "models": self.baseline.models},
            "front": [item(entry) for entry in self.front],
            "tried": self.tried,
            "rejected": self.rejected,
            "rejected_moves": self.rejected_moves,
            "rejected_reasons": dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])[:5]),
            "stopped": self.stopped,
            "seconds": round(self.seconds, 1),
            "complexity_unscored_models": self.unscored,
            "evidence": "proof: every observable model is proved equal to its original by prove_models",
        }


def search(
    pipeline,
    classes: Classes | None = None,
    *,
    roles: Mapping[str, str] | None = None,
    schema: ProverSchema | None = None,
    timeout_ms: int = 5000,
    max_states: int = 40,
    max_seconds: float = 300.0,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    on_front: Callable[[RefactorResult], None] | None = None,
) -> RefactorResult:
    """Explore proved-safe moves and return the Pareto front over (complexity, model count)."""

    started = time.time()
    roles = dict(roles) if roles is not None else classify(pipeline, classes or load_classes())
    reads = _Reads(pipeline)
    candidate_models = {k: m.sql for k, m in pipeline.models.items() if m.is_query}
    exposed = set()
    for key, role in roles.items():
        if role == "frozen" and key in pipeline.models and pipeline.models[key].is_query:
            exposed |= reads(pipeline.models[key].sql)
    observable = sorted({k for k, r in roles.items() if r == "protected"} | {k for k in exposed if k in candidate_models})
    setup = _Setup(pipeline, roles, frozenset(observable), reads, _Complexity(), schema, timeout_ms)
    baseline = _state(setup, candidate_models, (), ())
    result = RefactorResult(roles, observable, baseline, [baseline])
    verified = 0
    stop = ""

    def out_of_budget() -> str:
        if cancelled is not None and cancelled():
            return "cancel"
        if time.time() - started > max_seconds:
            return "time limit"
        if verified >= max_states:
            return "state limit"
        return ""

    # One greedy descent per weight on model count versus complexity: at each step the cheapest moves
    # under that weight are proved first and the first proved one is taken. Every proved state on every
    # path is a candidate for the Pareto front, and proofs are shared between paths through the cache.
    for weight in WEIGHTS:
        current = baseline
        while not stop:
            stop = out_of_budget()
            if stop:
                break
            options = []
            for label, sql in _moves(setup, current.sql_map):
                child = _state(setup, sql, (*current.moves, label), current.assumptions)
                gain = (current.complexity - child.complexity) + weight * (current.models - child.models)
                if gain > 0:
                    options.append((-gain, label, child))
            options.sort(key=lambda item: (item[0], item[1]))
            taken = None
            for _, label, child in options:
                stop = out_of_budget()
                if stop:
                    break
                result.tried += 1
                ok, assumptions, why = check_observable(
                    pipeline, child.sql_map, observable, reads, schema=schema, timeout_ms=timeout_ms, cache=setup.cache)
                if not ok:
                    result.rejected += 1
                    reason = why.split(":", 1)[-1].strip()[:80]
                    result.reasons[reason] = result.reasons.get(reason, 0) + 1
                    if len(result.rejected_moves) < 20 and not any(m["move"] == label for m in result.rejected_moves):
                        result.rejected_moves.append({"move": label, "why": why[:200]})
                    continue
                verified += 1
                taken = replace(child, assumptions=tuple(dict.fromkeys([*current.assumptions, *assumptions])))
                if progress:
                    progress(f"{label}: proved ({taken.models} models, complexity {taken.complexity})")
                break
            if taken is None:
                break
            current = taken
            if not any(_dominates(other, current) or other.cost() == current.cost() for other in result.front):
                result.front = [e for e in result.front if not _dominates(current, e)] + [current]
                if on_front:
                    result.front.sort(key=lambda e: (e.models, e.complexity))
                    result.seconds = time.time() - started
                    on_front(result)
        if stop:
            break
    result.stopped = stop
    result.front.sort(key=lambda e: (e.models, e.complexity))
    result.seconds = time.time() - started
    result.unscored = setup.score.unscored
    return result


# --------------------------------------------------------------- app and CLI


_JOB: dict = {"state": "idle"}
_JOB_LOCK = threading.Lock()
_CANCEL = threading.Event()


def _snapshot() -> dict:
    with _JOB_LOCK:
        return {k: v for k, v in _JOB.items() if k != "thread"}


def job_status() -> dict:
    """``GET /api/refactor/status``: idle, running (with the front found so far), done, cancelled or error."""

    data = _snapshot()
    if data["state"] == "running":
        data["elapsed"] = round(time.time() - data["started"], 1)
    return data


def cancel_job() -> dict:
    _CANCEL.set()
    return job_status()


def run_loaded(payload: Mapping | None = None) -> dict:
    """``POST /api/refactor/run``: start the search on the project loaded in the app, in the background."""

    from . import live_graph, prover_context

    loaded = live_graph.loaded()
    if not loaded:
        raise ValueError("load a project first")
    config = prover_context.settings()
    if not config["enabled"]:
        raise ValueError("the solver is turned off in Settings")
    payload = payload or {}
    classes = parse_classes(payload["classes"]) if payload.get("classes") else load_classes()
    pipeline = loaded["pipeline"]
    schema = prover_context.current_schema()
    limits = {"max_states": int(payload.get("max_states") or 200), "max_seconds": float(payload.get("max_seconds") or 600)}
    with _JOB_LOCK:
        if _JOB["state"] == "running":
            raise ValueError("a search is already running")
        _CANCEL.clear()
        _JOB.clear()
        _JOB.update(state="running", started=time.time(), line="", partial=None, tried=0)

    def update(**values) -> None:
        with _JOB_LOCK:
            _JOB.update(values)

    def work() -> None:
        try:
            result = search(
                pipeline, classes, schema=schema, timeout_ms=config["timeout_ms"], cancelled=_CANCEL.is_set,
                progress=lambda line: update(line=line),
                on_front=lambda partial: update(partial=partial.to_json(pipeline), tried=partial.tried),
                **limits,
            )
            update(state="cancelled" if result.stopped == "cancel" else "done", result=result.to_json(pipeline))
        except Exception as error:  # noqa: BLE001 - reported to the page, never raised into the server
            update(state="error", error=str(error) or type(error).__name__)

    thread = threading.Thread(target=work, name="refactor-search", daemon=True)
    thread.start()
    return job_status()


def classes_view() -> dict:
    """``GET /api/refactor``: the saved classes, the saved scopes to pick from, and the loaded project's models."""

    from . import live_graph

    loaded = live_graph.loaded()
    models = []
    roles: dict[str, str] = {}
    if loaded:
        pipeline = loaded["pipeline"]
        roles = classify(pipeline, load_classes())
        models = [{"key": k, "kind": m.kind, "role": roles.get(k, "frozen")} for k, m in sorted(pipeline.models.items())]
    return {
        "classes": load_classes().to_json(),
        "scopes": [scope.name for scope in scope_store.list_scopes()],
        "models": models,
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql refactor DIR [--protect KEY] [--editable KEY] ...``"""

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m kumosql refactor",
        description=__doc__.split("\n")[0] + " READ-ONLY: it prints suggestions as JSON and never edits the project folder.")
    parser.add_argument("project", help="Dataform or SQL folder")
    parser.add_argument("--protect", action="append", default=[], metavar="MODEL", help="a table that must keep existing and stay equivalent")
    parser.add_argument("--editable", action="append", default=[], metavar="MODEL", help="a model that may be dropped, merged, inlined or rewritten")
    parser.add_argument("--protect-scope", action="append", default=[], metavar="SCOPE")
    parser.add_argument("--editable-scope", action="append", default=[], metavar="SCOPE")
    parser.add_argument("--save", action="store_true", help="remember the protected/editable choices in the app's own data folder (never the project folder)")
    parser.add_argument("--max-states", type=int, default=40)
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    from .pipeline_loading import load_sqlx_project

    try:
        given = args.protect or args.editable or args.protect_scope or args.editable_scope
        if given:
            data = {"protected": {"models": args.protect, "scopes": args.protect_scope},
                    "editable": {"models": args.editable, "scopes": args.editable_scope}}
            classes = save_classes(data) if args.save else parse_classes(data)
        else:
            classes = load_classes()
        pipeline = load_sqlx_project(args.project)
        from .prover_schema import from_pipeline

        result = search(
            pipeline, classes, schema=from_pipeline(pipeline), timeout_ms=args.timeout_ms,
            max_states=args.max_states, max_seconds=args.max_seconds,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(result.to_json(pipeline), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0
