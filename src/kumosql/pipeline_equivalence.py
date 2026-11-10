"""Prove two tables of a Dataform pipeline equivalent, layer by layer.

Comparing ``E`` (A -> B -> C -> D -> E) with ``E2`` (A -> G -> H -> I -> E2) does not
need the pipelines flattened first. Models are visited from the sources upward; each
model's SQL is read with every table already shown equivalent to another replaced by
that table (saved equivalences and layer lemmas alike), and it is compared only with
models that now read the same tables. Each match is a small solver call and becomes a
lemma for the layers above, so a saved equivalence low in the pipeline ripples up through
every layer that builds on it, and a layer shared by several models is proved once.

When layer matching cannot connect the two (the pipelines are cut at different places) the
fallback is the baseline: inline every upstream model as a derived table and prove the
flat queries, within a size limit. Either way a verdict of equivalent needs every step
proven; anything else is unknown, never wrong.

A model is read as its query only when its stored rows are what that query returns: an incremental
model, one with pre or post operations, a script, or one whose values change from run to run (a clock,
RAND, a UUID) is never matched with another model nor inlined into a reader; it equals only itself.
Two models with the same SQL are matched without the solver only when nothing in it depends on row
order or ties (LIMIT, ARRAY_AGG, ROW_NUMBER, ...); otherwise the solver decides, and the assumptions it
needs (such as ties in a window's ORDER BY resolving alike) are listed with the result. An upstream query is inlined only where no WITH table of the reader shares the name of
a table it reads, which would otherwise capture the read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import sqlglot
from sqlglot import exp

from . import equivalences as saved
from kumo_sql_prover.ast_utils import binding_cte, captured_names
from kumo_sql_prover.algebraic_equivalence import prove_equivalent_algebraic
from kumo_sql_prover.conditional_equivalence import conditions_json
from kumo_sql_prover.equivalence import _has_row_selection_nondeterminism, _nondeterminism_reasons, _value_nondeterminism_sites
from .prover_schema import DECLARED_FACTS_NOTE, ProverSchema, _select_names
from kumo_sql_prover.smt_equivalence import SmtStatus

MAX_INLINE_CHARS = 400_000
MAX_SOLVER_CALLS = 300


@dataclass
class PipelineResult:
    status: str  # "equivalent" or "unknown"
    reason: str
    method: str = ""  # "same", "layers" or "inlined"
    lemmas: list[str] = field(default_factory=list)
    equivalences: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    bounded: dict | None = None  # the bounded check of the two flat queries, when nothing was proven
    conditions: list[dict] = field(default_factory=list)  # of a "conditional" verdict: the facts it holds under

    @property
    def proven(self) -> bool:
        return self.status == "equivalent"

    def to_json(self) -> dict:
        data = {"status": self.status, "reason": self.reason, "method": self.method,
                "lemmas": self.lemmas, "equivalences": self.equivalences, "assumptions": self.assumptions}
        if self.bounded is not None:
            data["bounded"] = self.bounded
        if self.conditions:
            data["conditions"] = self.conditions
        return data


def _opaque(model) -> bool:
    return (not model.is_query) or "__sqlx_token_" in model.sql or "${" in model.sql or not _one_query(model.sql)


@lru_cache(maxsize=4096)
def _one_query(sql: str) -> bool:
    """The text is a single query. A script's result is its last statement and its DML changes tables, so
    reading only its first SELECT (what ``parse_one`` returns) would compare the wrong rows."""

    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except sqlglot.errors.SqlglotError:
        return True  # the callers report the parse error themselves
    return len(statements) == 1 and isinstance(statements[0], exp.Query)


def stored_rows_differ(model) -> str:
    """Why the table a model writes may hold other rows than its query returns when read, or ``""``."""

    if model.kind == "incremental":
        return "is incremental, so its rows depend on its run history"
    if model.kind == "unknown":
        return "has a config type that cannot be read without running the project, so it may be incremental"
    if model.operations_sql:
        return "runs pre or post operations, which can change its rows"
    return ""


def run_dependent(tree: exp.Expression) -> str:
    """A value that changes from run to run even on the same input (a clock, RAND, a UUID), or ``""``."""

    sites = _value_nondeterminism_sites(tree)
    if sites:
        return f"value nondeterminism: {sites[0]}"
    found = next((n for n in tree.walk() if type(n).__name__ in _RUN_DEPENDENT_TYPES), None)
    return f"value nondeterminism: {found.sql(dialect='bigquery')}" if found is not None else ""


_RUN_DEPENDENT_TYPES = {"CurrentDatetime", "CurrentUser", "Randn", "Uuid", "Rand", "CurrentDate", "CurrentTime", "CurrentTimestamp"}


def nondeterministic(tree: exp.Expression) -> str:
    """Why two evaluations of the query can return different rows (a clock, RAND, an unordered LIMIT, ...), or ``""``."""

    reasons = _nondeterminism_reasons(tree)
    if reasons:
        return reasons[0]
    if _has_row_selection_nondeterminism(tree):
        return "LIMIT or OFFSET may select different rows on each run"
    return ""


def _tables(tree: exp.Expression) -> set[str]:
    found = set()
    for table in tree.find_all(exp.Table):
        parts = saved._parts(table)
        if parts and binding_cte(table) is None:
            found.add(".".join(parts))
    return found


def _resolve(pipeline, name: str) -> str:
    name = name.strip("`").lower()
    for key in pipeline.models:
        if key.lower() == name:
            return key
    matches = [key for key in pipeline.models if key.lower().endswith("." + name)]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"{name} is not a model of the loaded project")


def _lemma_record(rep: str, member: str, representative_names: list[str], member_names: list[str]) -> saved.Equivalence:
    return saved.Equivalence(
        rep.lower(), member.lower(), tuple(zip(representative_names, member_names)), True
    )


def prove_models(
    pipeline,
    first: str,
    second: str,
    *,
    declared: list[saved.Equivalence] | None = None,
    schema: ProverSchema | None = None,
    timeout_ms: int = 5000,
    bounded_check=None,
    conditional: bool = False,
    edited_copy: bool = False,
) -> PipelineResult:
    """Prove two models of ``pipeline`` return the same rows (columns compared by position).

    ``bounded_check(left_sql, right_sql)`` (optional) runs when nothing was proven; its JSON lands in
    ``PipelineResult.bounded`` as its own evidence level. ``edited_copy`` says ``second`` is ``first`` with
    its SQL edited, run on the same schedule with the same operations (the refactor check), so an
    incremental or operations model may be compared with it; otherwise such a model equals only itself.
    With ``conditional`` a pair that cannot be proven outright is tried under facts taken from the queries; the verdict is then
    ``"conditional"`` and ``conditions`` lists the minimal facts it needs (never counted as ``proven``).
    """

    a, b = _resolve(pipeline, first), _resolve(pipeline, second)
    declared = saved.load() if declared is None else list(declared)
    facts = schema or ProverSchema()
    columns = facts.columns or None
    calls = [0]

    def prove(left: str, right: str, names: bool = False):
        calls[0] += 1
        return prove_equivalent_algebraic(
            left, right, schema=columns, constraints=facts.constraints or None, types=facts.types or None,
            compare_names=names, timeout_ms=timeout_ms,
        )

    if a == b:
        return PipelineResult("equivalent", "the same table", "same")

    for key in (a, b):
        if _opaque(pipeline.models[key]):
            return PipelineResult("unknown", f"{key} could not be read as a plain query")
    if not edited_copy:
        for key in (a, b):
            why = stored_rows_differ(pipeline.models[key])
            if why:
                return PipelineResult("unknown", f"{key} {why}")

    # Upstream of both targets, in pipeline order (sources first).
    upstream = pipeline.upstream
    needed: set[str] = set()
    todo = [a, b]
    while todo:
        key = todo.pop()
        if key in needed or key not in pipeline.models:
            continue
        needed.add(key)
        todo.extend(upstream.get(key, ()))
    order = [key for key in pipeline.topological_order() if key in needed]

    lemmas: list[saved.Equivalence] = []
    used_declared: list[saved.Equivalence] = []
    processed: dict[str, tuple[str, set[str]]] = {}  # model -> (SQL after substitution, tables it reads)

    def substituted(key: str):
        tree = sqlglot.parse_one(pipeline.models[key].sql, read="bigquery")
        tree, used_d = saved.rewrite_tree(tree, declared, columns)
        tree, used_l = saved.rewrite_tree(tree, lemmas, columns)
        for item in used_d:
            if item not in used_declared:
                used_declared.append(item)
        return tree.sql(dialect="bigquery"), _tables(tree), used_l

    notes: list[str] = []
    lemma_assumptions: list[str] = []
    for key in order:
        if key in (a, b) or _opaque(pipeline.models[key]) or stored_rows_differ(pipeline.models[key]):
            continue
        try:
            sql, reads, _ = substituted(key)
            parsed = sqlglot.parse_one(sql, read="bigquery")
            if run_dependent(parsed):
                continue  # two separate runs of it differ, even when another model has the same SQL
            # the same text is the same rows only when nothing depends on row order or ties; else ask the solver
            shortcut = not nondeterministic(parsed)
        except sqlglot.errors.SqlglotError:
            continue
        names = _select_names(sql)
        if names:
            for other, (other_sql, other_reads) in processed.items():
                if other_reads != reads or calls[0] >= MAX_SOLVER_CALLS:
                    continue
                other_names = _select_names(other_sql)
                if not other_names or len(other_names) != len(names):
                    continue
                matched = shortcut and sql == other_sql
                if not matched:
                    proof = prove(sql, other_sql)
                    matched = proof.proven
                    if matched:
                        lemma_assumptions.extend(proof.assumptions)
                if matched:
                    lemmas.append(_lemma_record(other, key, other_names, names))
                    notes.append(f"{key} ≡ {other}")
                    break
            else:
                processed[key] = (sql, reads)
        # a model with unknown columns stays out of the lemma search

    try:
        left_sql, _, _ = substituted(a)
        right_sql, _, _ = substituted(b)
    except sqlglot.errors.SqlglotError as error:
        return PipelineResult("unknown", f"parse error: {error}")

    def finish(method: str, result) -> PipelineResult:
        extra = [DECLARED_FACTS_NOTE] if facts.notes else []
        if method == "layers":
            extra.extend(lemma_assumptions)
        if used_declared:
            extra.append("declared equivalences hold in the data: " + "; ".join(i.label for i in used_declared))
        return PipelineResult(
            "equivalent", result.reason, method, notes, [i.label for i in used_declared],
            list(dict.fromkeys([*extra, *result.assumptions])),
        )

    result = prove(left_sql, right_sql)
    if result.status is SmtStatus.PROVEN_EQUIVALENT:
        return finish("layers", result)

    flat = _inlined(pipeline, a, declared, columns), _inlined(pipeline, b, declared, columns)
    if flat[0] and flat[1]:
        direct = prove(flat[0][0], flat[1][0])
        if direct.status is SmtStatus.PROVEN_EQUIVALENT:
            used_declared[:] = [i for i in {*(flat[0][1]), *(flat[1][1])}]
            return finish("inlined", direct)
    if conditional:
        for method, pair in (("layers", (left_sql, right_sql)), ("inlined", (flat[0][0], flat[1][0]) if flat[0] and flat[1] else None)):
            if pair is None:
                continue
            attempt = prove_equivalent_algebraic(
                *pair, schema=columns, constraints=facts.constraints or None, types=facts.types or None,
                compare_names=False, timeout_ms=timeout_ms, conditional=True,
            )
            if attempt.status is SmtStatus.PROVEN_CONDITIONALLY:
                extra = [DECLARED_FACTS_NOTE] if facts.notes else []
                if used_declared and method == "layers":
                    extra.append("declared equivalences hold in the data: " + "; ".join(i.label for i in used_declared))
                return PipelineResult(
                    "conditional", attempt.reason, method, notes, [i.label for i in used_declared] if method == "layers" else [],
                    list(dict.fromkeys([*extra, *attempt.assumptions])), conditions=conditions_json(attempt),
                )
    # The layer attempt compares a model with its rewritten copy, which read different tables, so its reason says
    # little; when the flat queries were compared too, theirs is the reason the proof failed.
    why = direct.reason if flat[0] and flat[1] else result.reason
    outcome = PipelineResult("unknown", why, "", notes)
    if bounded_check is not None:
        pair = (flat[0][0], flat[1][0]) if flat[0] and flat[1] else (left_sql, right_sql)
        outcome.bounded = bounded_check(*pair)
    return outcome


def _inlined(pipeline, key: str, declared, columns, limit: int = MAX_INLINE_CHARS):
    """``(sql, declarations used)`` with every upstream model inlined as a derived table, or None."""

    used: list[saved.Equivalence] = []

    def expand(model_key: str, trail: tuple[str, ...]) -> exp.Expression | None:
        model = pipeline.models[model_key]
        if _opaque(model) or model_key in trail:
            return None
        tree = sqlglot.parse_one(model.sql, read="bigquery")
        if trail and (stored_rows_differ(model) or run_dependent(tree)):
            return None  # its stored rows are not what a fresh run of its query returns
        for table in list(tree.find_all(exp.Table)):
            parts = saved._parts(table)
            if len(parts) < 2:
                continue
            name = ".".join(parts)
            target = next((k for k in pipeline.models if k.lower() == name), None)
            if target is None:
                continue
            body = expand(target, trail + (model_key,))
            if body is None or captured_names(body, table):
                return None
            alias = table.alias or table.name
            table.replace(exp.Subquery(this=body, alias=exp.TableAlias(this=exp.to_identifier(alias))))
            if len(tree.sql()) > limit:
                return None
        tree, hits = saved.rewrite_tree(tree, declared, columns)
        used.extend(h for h in hits if h not in used)
        return tree

    try:
        tree = expand(key, ())
    except sqlglot.errors.SqlglotError:
        return None
    if tree is None:
        return None
    sql = tree.sql(dialect="bigquery")
    return (sql, used) if len(sql) <= limit else None


def prove_loaded(left: object, right: object) -> dict:
    """``POST /api/prove-tables``: compare two models of the project loaded in the app."""

    from . import live_graph, prover_context

    if not isinstance(left, str) or not isinstance(right, str) or not left.strip() or not right.strip():
        raise ValueError("name the two tables to compare")
    loaded = live_graph.loaded()
    if not loaded:
        raise ValueError("load a project first")
    config = prover_context.settings()
    if not config["enabled"]:
        raise ValueError("the solver is turned off in Settings")
    return prove_models(
        loaded["pipeline"], left, right,
        schema=prover_context.current_schema(), timeout_ms=config["timeout_ms"],
        bounded_check=prover_context.bounded, conditional=True,
    ).to_json()


def prove_queries(left: object, right: object, explain: bool = False) -> dict:
    """``POST /api/prove-queries``: prove two pasted queries return the same rows.

    ``explain`` (the request's ``explain`` field, off by default) adds ``except_when``
    (``{sql, atoms, tables, exact}``, see ``difference_explanation``) to a refuted pair when a verified predicate exists.
    """

    from . import prover_context
    from kumo_sql_prover.result_equivalence import DataRules
    from kumo_sql_prover.smt_equivalence import SmtStatus

    if not isinstance(left, str) or not isinstance(right, str) or not left.strip() or not right.strip():
        raise ValueError("paste both queries")
    config = prover_context.settings()
    if not config["enabled"]:
        raise ValueError("the solver is turned off in Settings")
    result = prover_context.prove(left, right, search_counterexample=True, conditional=True)
    data = {"status": result.status.value, "reason": result.reason, "assumptions": list(result.assumptions)}
    if result.status is SmtStatus.PROVEN_CONDITIONALLY:
        data["conditions"] = conditions_json(result)
    if result.status not in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
        bounded = prover_context.bounded(left, right)
        if bounded is not None:
            data["bounded"] = bounded
    if result.status in (SmtStatus.NOT_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY) and result.counterexample is not None:
        example = result.counterexample
        data["counterexample"] = {
            "tables": {name: [dict(row) for row in rows] for name, rows in example.tables.items()},
            "left_rows": [list(row) for row in example.left_rows],
            "right_rows": [list(row) for row in example.right_rows],
        }
    if result.status not in (SmtStatus.PROVEN_EQUIVALENT, SmtStatus.PROVEN_CONDITIONALLY):
        from kumo_sql_prover.refute import counterexample_from_search

        facts = prover_context.current_schema()
        # a declared key's columns are NOT NULL (TableConstraints), and every foreign key holds
        rules = {
            name: DataRules(not_null=frozenset(c.not_null).union(*map(frozenset, c.keys)), keys=tuple(c.keys))
            for name, c in facts.constraints.items()
        }
        foreign_keys = [
            (name.lower(), tuple(map(str.lower, columns)), parent.lower().split(".")[-1], tuple(map(str.lower, parent_columns)))
            for name, c in facts.constraints.items()
            for columns, parent, parent_columns in c.foreign_keys
        ]
        found = counterexample_from_search(left, right, facts.columns, rules, facts.types, foreign_keys=foreign_keys)
        size = lambda c: sum(len(r) for r in c["tables"].values())
        if found is not None and ("counterexample" not in data or size(found) < size(data["counterexample"])):
            data["status"] = "not_equivalent"
            data["reason"] = "the queries return different rows on a small database"
            data["counterexample"] = found
    if explain and data["status"] == SmtStatus.NOT_EQUIVALENT.value:
        from kumo_sql_prover.difference_surface import except_when

        found_when = except_when(left, right, timeout_ms=config["timeout_ms"])
        if found_when is not None:
            data["except_when"] = found_when
    return data


def main(argv: list[str] | None = None) -> int:
    """``python -m kumosql prove-tables LEFT RIGHT --project DIR`` and ``equivalence list|add|remove``."""

    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m kumosql prove-tables", description=__doc__.split("\n")[0])
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--project", required=True, metavar="DIR", help="Dataform or SQL folder")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    args = parser.parse_args(argv)
    from .pipeline_loading import load_sqlx_project

    try:
        result = prove_models(load_sqlx_project(args.project), args.left, args.right, timeout_ms=args.timeout_ms)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(result.to_json(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result.proven else 1


def equivalence_main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m kumosql equivalence", description="Saved column equivalences between tables")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add", help="LEFT RIGHT [LEFTCOL=RIGHTCOL ...] [--whole]")
    add.add_argument("left")
    add.add_argument("right")
    add.add_argument("columns", nargs="*", help="pairs written LEFTCOL=RIGHTCOL")
    add.add_argument("--whole", action="store_true", help="the two tables hold the same rows (other columns keep their names)")
    remove = sub.add_parser("remove")
    remove.add_argument("right")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            json.dump([i.to_json() for i in saved.load()], sys.stdout, indent=2)
            sys.stdout.write("\n")
        elif args.action == "add":
            pairs = [pair.split("=", 1) for pair in args.columns]
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError("write column pairs as LEFTCOL=RIGHTCOL")
            item = saved.add({"left": args.left, "right": args.right, "columns": pairs, "whole": args.whole})
            print(f"saved {item.label}")
        else:
            print("removed" if saved.remove(args.right) else "nothing to remove")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0
