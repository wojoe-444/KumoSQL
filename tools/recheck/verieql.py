"""Proven pairs of the VeriEQL suites (Literature, Calcite-397, LeetCode), as ``tools/verieql_bench.py`` proves them.

Each pair is read, repaired and proved exactly as the eval's ``decide`` does: the constraints become a
``counterexample.Spec`` (``build_spec``), the queries get the harness repairs (``repaired_pair``), the
``Searcher`` must accept both on DuckDB, and ``prove`` runs with the eval's 3-second z3 timeout inside its
30-second budget (keys and NOT NULL columns first, foreign keys on a second attempt). A pair the prover
proves becomes a :class:`Case` with the DuckDB SQL the ``Searcher`` runs and the declared constraints:
keys, NOT NULL columns, ENUM value lists and foreign keys as tables, and the ``CHECK``-style predicates,
cross-table implications and consecutive-id columns as ``Case.legal`` (a check is violated only when it is
FALSE, as in SQL; ``KUMOSQL_RECHECK_STRICT=1`` also rejects UNKNOWN, as the harness's generator does).
Literature's uninterpreted predicates get the harness's fixed interpretation (a hash of their arguments)
as ``Case.setup`` macros.

The eval counts a pair as proven only when its own 200-database search (seeded by the pair's index)
found nothing first, so ``meta["eval_verdict"]`` says what the eval does with a proven pair:
``equivalent`` (counted), ``different`` (the eval's search refutes it before proving; a false proof the
eval does not count) or ``over-budget`` (proving took longer than the eval's 30 seconds).

Search feature kept here (``engine.py`` is unchanged): a consecutive-id column (LeetCode's ``inc`` and
``consec`` constraints, read by the harness as the ids 1..n in row order) is renumbered 1..n by
``Case.legal`` itself, in place, before the other checks; every database the engine draws or shrinks
passes through ``legal``, so the search only ever runs databases that honour it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import os
from pathlib import Path
import signal
import sys
import time

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import verieql_bench as vb  # noqa: E402
from kumosql import counterexample as cx  # noqa: E402

from recheck import engine  # noqa: E402
from recheck.engine import Case, Column, Table  # noqa: E402

SUITES = ("literature", "calcite", "leetcode")  # smallest first, so the first results come early
_KIND = {"BIGINT": "int", "DOUBLE": "float", "BOOLEAN": "bool", "DATE": "date", "TIME": "time", "VARCHAR": "text"}
PROVE_BUDGET = 30  # seconds: ``decide``'s budget
TIMEOUT_MS = 3000  # ``decide``'s z3 timeout
SEARCH_TRIALS = 200  # ``decide``'s first search
# SQL reads a CHECK as violated only when it is FALSE (a NULL passes); the harness's generator keeps only rows
# where it is TRUE. The search uses SQL's reading (more databases); KUMOSQL_RECHECK_STRICT=1 uses the harness's.
STRICT = os.environ.get("KUMOSQL_RECHECK_STRICT", "0") == "1"


def engine_tables(spec: cx.Spec) -> dict[str, Table]:
    out = {}
    for name, table in spec.tables.items():
        columns = []
        for column in table.columns:
            sql_type = cx._duck_type(column)
            columns.append(Column(
                column.name, _KIND[sql_type], not_null=column.not_null or column.name in table.primary_key,
                sql_type=sql_type, values=tuple(column.values) if column.type == "ENUM" else (),
            ))
        keys = ([tuple(table.primary_key)] if table.primary_key else []) + [tuple(u) for u in table.unique]
        foreign = [((column,), parent, (parent_column,)) for child, column, parent, parent_column in spec.foreign_keys if child == name]
        out[name] = Table(name, columns, keys, foreign)
    return out


def _harness_value(value):
    """A value as the harness's checks see it: dates and times as ISO strings, decimals as floats."""

    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _legal(spec: cx.Spec, tables: dict[str, Table]):
    """``Case.legal``: consecutive ids (renumbered in place), then every CHECK and implication."""

    single: dict[str, list[cx.Check]] = {}
    multi: list[cx.Check] = []
    for check in spec.checks:
        if len(set(check.tables)) == 1:
            single.setdefault(check.tables[0], []).append(check)
        else:
            multi.append(check)
    sequential = {name: [tables[name].index(c) for c in table.sequential] for name, table in spec.tables.items() if table.sequential}
    names = {name: [c.name for c in table.columns] for name, table in spec.tables.items()}
    if not single and not multi and not sequential:
        return None

    def holds(value) -> bool:
        return value is True if STRICT else value is not False

    def rows_of(data, name):
        return [dict(zip(names[name], (_harness_value(v) for v in row))) for row in data[name]]

    def legal(data) -> bool:
        for name, positions in sequential.items():
            if name in data:
                data[name] = [tuple(number if p in positions else v for p, v in enumerate(row)) for number, row in enumerate(data[name], 1)]
        if sequential and not engine.legal(_bare, data):  # renumbering must not break a key or a foreign key
            return False
        for name, checks in single.items():
            if name in data:
                for row in rows_of(data, name):
                    if not all(holds(c.test(row)) for c in checks):
                        return False
        for check in multi:
            if not all(t in data for t in check.tables):
                continue

            def every(i, picked):
                if i == len(check.tables):
                    return holds(check.test(*picked))
                return all(every(i + 1, picked + [row]) for row in rows_of(data, check.tables[i]))

            if not every(0, []):
                return False
        return True

    _bare = Case("", "", "", "", tables)  # the same tables without this callback
    return legal


def _macros(predicates: dict[str, int]) -> tuple[str, ...]:
    """The harness's fixed interpretation of each uninterpreted predicate (``Searcher.__init__``)."""

    out = []
    for name, arity in predicates.items():
        parameters = [f"p{i}" for i in range(arity)]
        hashed = ", ".join([*parameters, cx._literal(name.lower())])
        out.append(f"CREATE MACRO {name}({', '.join(parameters)}) AS (hash({hashed}) % 2 = 0)")
    return tuple(out)


class _Budget(Exception):
    pass


def _raise_budget(_signum, _frame):
    raise _Budget


def _prove(case: dict, spec: cx.Spec, pair: tuple[str, str]):
    """``vb.prove`` under the eval's budget; the runner's own alarm is put back afterwards."""

    previous = signal.signal(signal.SIGALRM, _raise_budget)
    outer = signal.alarm(PROVE_BUDGET)
    start = time.time()
    try:
        result = vb.prove(case, spec, TIMEOUT_MS, pair)
        return bool(result.proven), getattr(result, "reason", "")
    except _Budget:
        return False, "budget"
    except Exception as error:  # a crash is a failure to prove, never a proof
        return False, f"crash: {type(error).__name__}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if outer:
            signal.alarm(max(1, int(outer - (time.time() - start))))


class Adapter:
    name = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


class VeriEQL(Adapter):
    name = "verieql"

    def __init__(self, suites: tuple[str, ...] = SUITES):
        self.suites = suites

    def items(self) -> list[dict]:
        out = []
        for suite in self.suites:
            for case in vb.load_cases(suite):
                out.append({"pair": f"{suite}:{case['index']}", "case": case})
        return out

    def case(self, item: dict) -> Case | None:
        case = item["case"]
        try:
            spec = vb.build_spec(case)
        except Exception:  # unreadable constraints: the eval says unknown
            return None
        left, right, predicates = vb.repaired_pair(case, spec)
        try:
            searcher = cx.Searcher(spec, left, right, predicates=predicates)
        except Exception:
            return None
        if not searcher.runs():
            return None
        start = time.time()
        proven, reason = _prove(case, spec, (left, right))
        prove_seconds = time.time() - start
        if not proven:
            return None
        start = time.time()
        try:
            found = searcher.search(SEARCH_TRIALS, seed=case["index"]) is not None
        except Exception:
            found = False
        search_seconds = time.time() - start
        verdict = "different" if found else "over-budget" if prove_seconds + search_seconds > PROVE_BUDGET else "equivalent"
        tables = engine_tables(spec)
        meta = {
            "suite": case["suite"], "index": case["index"], "eval_verdict": verdict, "reason": str(reason)[:120],
            "prove_seconds": round(prove_seconds, 2), "search_seconds": round(search_seconds, 2),
        }
        if case.get("adapted"):
            meta["adapted"] = True
        if predicates:
            meta["uninterpreted"] = sorted(predicates)
        if case["suite"] == "leetcode":
            meta["search_tuning_sample"] = case["index"] % 24 == 0  # the eval's search settings were tuned on every 24th pair
        return Case(
            self.name, item["pair"], searcher.left_sql, searcher.right_sql, tables, legal=_legal(spec, tables),
            setup=_macros(predicates), source=(left, right), dialect="mysql", meta=meta,
        )


class VeriEQLSuite(VeriEQL):
    """One suite under the name of its results file (``benchmarks/results/verieql-<suite>-proof.json``), so that the
    admission ledger (``tools/proof_admission.py``) can hold the proofs that row counts."""

    def __init__(self, suite: str):
        super().__init__((suite,))
        self.name = f"verieql-{suite}-proof"

    def counted(self, item: dict) -> bool:
        """Whether the eval counts the pair as proven: the prover proves it, and the eval's own search finds no
        difference first (a pair it refutes is scored as different, whatever the prover says) within its budget."""

        case = self.case(item)
        return case is not None and case.meta.get("eval_verdict") == "equivalent"


# LeetCode has no suite adapter: its row is measured in hours and has no ledger.
ADAPTERS = {a.name: a for a in [VeriEQL(), VeriEQLSuite("literature"), VeriEQLSuite("calcite")]}
