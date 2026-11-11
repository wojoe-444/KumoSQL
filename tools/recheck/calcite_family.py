"""Proven pairs of the evals that share ``tools/sqlsolver_bench.py``'s harness.

SQLSolver (Calcite, Spark, TPC-H, TPC-C), QED, mined Calcite, R-Bot, Cosette and SPES. Each pair is
proved exactly as its eval proves it, and a proven pair is turned into DuckDB SQL exactly as that
eval's ``differ`` check runs it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import sqlsolver_bench as sb  # noqa: E402

from recheck.engine import Case, Column, Table  # noqa: E402

FIXTURES = TOOLS.parent / "tests" / "fixtures"
PROVER_ENV = "KUMO_SQL_PROVER_CHECKOUT"  # the name tools/proof_admission.py reads its --prover default from
_KIND = {"VARCHAR": "text", "DOUBLE": "float", "DATE": "date", "BIGINT": "int"}


def engine_tables(tables: dict) -> dict[str, Table]:
    out = {}
    for name, table in tables.items():
        columns = [Column(c.name, _KIND[sb._duck_type(c)], not_null=c.not_null, sql_type=sb._duck_type(c)) for c in table.columns]
        keys = ([tuple(table.primary_key)] if table.primary_key else []) + [tuple(u) for u in table.unique]
        foreign = [((column,), parent, (parent_column,)) for column, parent, parent_column in table.foreign if parent in tables]
        out[name] = Table(table.name, columns, keys, foreign)
    return out


def duckdb_pair(left: str, right: str, constants: bool) -> tuple[str, str]:
    """The DuckDB SQL ``sqlsolver_bench.differ`` runs for a pair."""

    left, right = sb.spark_days(left), sb.spark_days(right)
    if constants:
        left, right = sb.name_values(left), sb.name_values(right)
    left_sql, right_sql = sb.to_dialect(left, "duckdb"), sb.to_dialect(right, "duckdb")
    if constants:
        left_sql, right_sql = sb.constant_groupings(left_sql), sb.constant_groupings(right_sql)
    return left_sql, right_sql


def _schema(ddl: str) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "schema.sql"
        path.write_text(ddl, encoding="utf-8")
        return sb.load_schema(path)


_SCHEMAS: dict[str, dict] = {}


def _tables_for(ddl: str) -> dict:
    if ddl not in _SCHEMAS:
        _SCHEMAS[ddl] = _schema(ddl)
    return _SCHEMAS[ddl]


def _proved(prove, *args) -> bool:
    try:
        result = prove(*args)
    except Exception:  # a crash is a failure to prove, never a proof
        return False
    return bool(result if isinstance(result, bool) else result.proven)


def converted_fixtures(environ=os.environ) -> Path:
    """Where QED's and mined Calcite's pairs are read: the named prover checkout, else this repository's copy.

    Their converters live in the prover repository, and a pull request there can regenerate the fixtures, so its
    ledger lists pairs this repository's copy does not hold yet. Read from the copy, those proofs would never be
    checked, and a later run would report every one of them lost and drop it from the ledger. A named checkout is
    never second-guessed: when it lacks the fixtures, reading them fails."""

    named = environ.get(PROVER_ENV)
    return Path(named).expanduser() / "tests" / "fixtures" if named else FIXTURES


class Adapter:
    """``items()`` lists every pair (picklable); ``case(item)`` proves it as the eval does and returns a Case, or None."""

    name = ""
    # The dialect ``tools/proof_mutants.py`` reads the queries in, when ``items()`` carries both as ``left`` and
    # ``right`` text and ``case`` proves from the item alone; empty when the pairs cannot be mutated that way.
    mutable = ""

    def items(self) -> list[dict]:
        raise NotImplementedError

    def case(self, item: dict) -> Case | None:
        raise NotImplementedError


class SqlSolver(Adapter):
    mutable = "mysql"

    def __init__(self, suite: str):
        self.suite = suite
        self.name = f"sqlsolver-{suite}"

    def items(self) -> list[dict]:
        pairs_file, schema_file = sb.SUITES[self.suite]
        excluded = sb.must_not_prove(self.suite)
        return [
            {"pair": str(index), "left": left, "right": right, "schema": schema_file, "excluded": index in excluded}
            for index, (left, right) in enumerate(sb.load_pairs(sb.FIXTURES / pairs_file))
        ]

    def case(self, item: dict) -> Case | None:
        tables = _tables_for((sb.FIXTURES / item["schema"]).read_text(encoding="utf-8"))
        constants = self.suite in sb.CONSTANT_GROUPING
        left, right = item["left"], item["right"]
        if constants:
            left, right = sb.calcite_operators(left), sb.calcite_operators(right)
        args = (left, right, tables, True) if constants else (left, right, tables)
        if not _proved(sb.default_prove, *args):
            return None
        left_sql, right_sql = duckdb_pair(left, right, constants)
        return Case(self.name, item["pair"], left_sql, right_sql, engine_tables(tables), source=(left, right), dialect="mysql",
                    meta={"must_not_prove": item["excluded"]})


class Qed(Adapter):
    mutable = "mysql"

    name = "qed-calcite"

    def items(self) -> list[dict]:
        rows = [json.loads(line) for line in (converted_fixtures() / "qed" / "qed_calcite_pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        return [{"pair": r["name"], "left": r["sql_a"], "right": r["sql_b"], "ddl": r["ddl"]} for r in rows]

    def case(self, item: dict) -> Case | None:
        tables = _tables_for(item["ddl"])
        if not _proved(sb.default_prove, item["left"], item["right"], tables, True):
            return None
        left_sql, right_sql = duckdb_pair(item["left"], item["right"], True)
        return Case(self.name, item["pair"], left_sql, right_sql, engine_tables(tables), source=(item["left"], item["right"]), dialect="mysql")


class Mined(Adapter):
    mutable = "mysql"

    name = "calcite-mined"

    def items(self) -> list[dict]:
        folder = converted_fixtures() / "calcite_mined"
        schemas = json.loads((folder / "schemas.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (folder / "pairs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        return [{"pair": r["name"], "left": r["sql_a"], "right": r["sql_b"], "ddl": schemas[r["schema_id"]]["ddl"], "held_out": bool(r.get("new"))} for r in rows]

    def case(self, item: dict) -> Case | None:
        tables = _tables_for(item["ddl"])
        if not _proved(sb.default_prove, item["left"], item["right"], tables):
            return None
        left_sql, right_sql = duckdb_pair(item["left"], item["right"], False)
        return Case(self.name, item["pair"], left_sql, right_sql, engine_tables(tables), source=(item["left"], item["right"]), dialect="mysql",
                    held_out=item["held_out"])


class RBot(Adapter):
    mutable = "mysql"

    name = "rbot-calcite"

    def items(self) -> list[dict]:
        import rbot_bench

        return [{"pair": name, "left": left, "right": right} for name, left, right in rbot_bench.load_pairs()]

    def case(self, item: dict) -> Case | None:
        import rbot_bench

        tables = _tables_for((FIXTURES / "rbot" / "create_tables.sql").read_text(encoding="utf-8"))
        try:
            left, right = rbot_bench.normalise(item["left"]), rbot_bench.normalise(item["right"])
        except Exception:
            return None
        if not _proved(sb.default_prove, left, right, tables, True):
            return None
        left_sql, right_sql = duckdb_pair(left, right, True)
        return Case(self.name, item["pair"], left_sql, right_sql, engine_tables(tables), source=(left, right), dialect="mysql")


class CosetteSuite(Adapter):
    mutable = "mysql"

    def __init__(self, suite: str):
        self.suite = suite
        self.name = "cosette" if suite == "cosette" else "spes-only"

    def items(self) -> list[dict]:
        import cosette_bench

        return [
            {"pair": r["name"], "left": r["sql_a"], "right": r["sql_b"], "ddl": r["ddl"], "label": r.get("label", "equivalent")}
            for r in cosette_bench.load(self.suite)
        ]

    def case(self, item: dict) -> Case | None:
        import cosette_bench

        tables = _tables_for(item["ddl"])
        left, right = cosette_bench.repaired(item["left"], item["right"], tables)
        constants = self.suite == "spes"
        args = (left, right, tables, constants) if constants else (left, right, tables)
        if not _proved(sb.prove_result, *args):
            return None
        left_sql, right_sql = duckdb_pair(left, right, constants)
        # a hidden ``__`` column stands for an uninterpreted predicate: random values need not be a function of the row
        return Case(self.name, item["pair"], left_sql, right_sql, engine_tables(tables), source=(left, right), dialect="mysql",
                    meta={"label": item["label"], "uninterpreted": "__" in item["ddl"]})


ADAPTERS = {
    a.name: a
    for a in [
        SqlSolver("calcite"), SqlSolver("spark"), SqlSolver("tpch"), SqlSolver("tpcc"),
        Qed(), Mined(), RBot(), CosetteSuite("cosette"), CosetteSuite("spes"),
    ]
}
