"""Differential soundness fuzzer: typed schemas, integrity constraints and six databases per pair.

Each generated case is a query pair over a small typed schema (nullable INT64 and ASCII STRING columns, an
optional third table, declared keys, NOT NULL columns and foreign keys). The pair is run in DuckDB on six or more
legal databases (seeded random ones, a NULL-heavy one, a duplicate-heavy one and an empty one) and handed to the
public prover, ``kumosql.algebraic_equivalence.prove_equivalent_algebraic``, with the same schema, types and
constraints. A **false proof** is a pair the prover proves while some database separates the results; the count
must be 0. Every database is run with DuckDB's optimizer on and off (``kumosql.duckdb_load.run_unoptimized``, see
#347): a difference counts only when the unoptimized run shows it, and one the optimizer hides (the optimized
results agree, the unoptimized ones do not) counts as well.

Pairs come from three generators, mixed per seed:

``template``
    Rewrite templates with random predicates, expressions, join types and aggregates on both sides: predicate and
    NULL identities, COUNT/SUM/DISTINCT variants, outer-join simplification, ON/WHERE moves, semi- and anti-joins,
    set operations, GROUPING SETS/ROLLUP/CUBE, HAVING, derived-table and window pushdown, scalar subqueries, CTE
    scope and shadowing, QUALIFY/ROW_NUMBER, ordered LIMIT, self-joins on keys and foreign keys. Some instances are
    sound and some are not; the databases decide, and a pair is labelled equivalent only where the template is
    sound for every database (a separated labelled pair is a ``label_error``, a generator bug).
``sol``
    The 24 construct families of Sol's S015 fuzzer (sound identity, wrapper and duplicated-filter mutations, and on
    alternate passes deliberate semantic changes).
``mutant``
    A template query against one of its single-site mutants (``kumosql.query_mutants``).

Every evaluation runs in a long-lived child process with a wall timeout (a hung prover is killed and the child is
restarted); ``--jobs`` children run in parallel. False proofs are reduced (SQL on both sides, then the witness
database's rows) while the prover still proves the pair and the databases still separate it.

    python tools/soundness_fuzz.py --seed 1 --count 400 --jobs 4 --output fuzz-run.json
    python tools/soundness_fuzz.py --replay fuzz-run.json           # re-evaluate the saved false proofs
    python tools/soundness_fuzz.py --seed 7 --count 40 --show        # print each false proof

The oracle and its narrow execution domain (a closed list of SQL node types; casts, LIMIT and ROW_NUMBER only
where both engines agree) come from Sol's S015 deliverable; see ``docs/evals/fuzzing.md``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import threading
import time
import traceback

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

KNOWN_PATH = ROOT / "tests" / "fixtures" / "soundness_fuzz" / "known_false_proofs.json"
HISTORICAL_PATH = ROOT / "tests" / "fixtures" / "soundness_fuzz" / "historical.json"

# Closed execution domain. Unknown syntax and functions are refused, not silently accepted because sqlglot happened
# to serialize them.
SAFE_NODES = set("""Select Column Identifier Table From TableAlias Alias Star Join
Where And Or Not EQ NEQ LT LTE GT GTE Is Paren In Exists Subquery Union Intersect
Except With CTE Group GroupingSets Rollup Cube Tuple Having Distinct Order Ordered Limit
Literal Null Boolean Cast DataType Case If Coalesce Count Sum Min Max Avg Window
RowNumber Qualify Add Sub Mul Neg Any All Between Like NullSafeEQ NullSafeNEQ""".split())


class UnsupportedConversion(ValueError):
    """The execution oracle cannot faithfully model this query."""


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _required(rules: dict) -> set:
    required = set(rules.get("not_null", []))
    for key in rules.get("keys", []):
        required.update(key)  # KumoSQL keys are NOT NULL unique keys
    return required


def fixture_errors(case: dict) -> list[str]:
    """Types, key nullability and uniqueness, NOT NULL and MATCH SIMPLE foreign keys of ``case['tables']``."""

    errors = []
    schema, tables = case["schema"], case["tables"]
    constraints = case.get("constraints", {})
    if set(schema) != set(tables):
        errors.append("schema/table names differ")
    for table, columns in schema.items():
        rules = constraints.get(table, {})
        names = [c[0] for c in columns]
        required = _required(rules)
        if required - set(names):
            errors.append(f"{table}: constraint references unknown column")
        for row in tables.get(table, []):
            if len(row) != len(columns):
                errors.append(f"{table}: row arity mismatch")
                continue
            for (name, kind), value in zip(columns, row):
                if name in required and value is None:
                    errors.append(f"{table}.{name}: NOT NULL/key violation")
                if value is not None:
                    if kind == "INT64" and (type(value) is not int or not -(2**63) <= value < 2**63):
                        errors.append(f"{table}.{name}: outside INT64 domain")
                    elif kind == "STRING" and (type(value) is not str or not value.isascii()):
                        errors.append(f"{table}.{name}: outside ASCII STRING domain")
                    elif kind not in ("INT64", "STRING"):
                        errors.append(f"{table}.{name}: unsupported type {kind}")
        for key in rules.get("keys", []):
            if not set(key) <= set(names):
                errors.append(f"{table}: key references unknown column")
                continue
            values = [tuple(row[names.index(c)] for c in key) for row in tables.get(table, []) if len(row) == len(columns)]
            if len(values) != len(set(values)):
                errors.append(f"{table}: duplicate key {key}")
        for child, parent, parent_cols in rules.get("foreign_keys", []):
            if parent not in schema or not set(child) <= set(names):
                errors.append(f"{table}: FK references unknown table/column")
                continue
            pnames = [c[0] for c in schema[parent]]
            if len(child) != len(parent_cols) or not set(parent_cols) <= set(pnames):
                errors.append(f"{table}: malformed FK")
                continue
            parents = {tuple(row[pnames.index(c)] for c in parent_cols) for row in tables[parent] if len(row) == len(pnames)}
            for row in tables.get(table, []):
                if len(row) != len(names):
                    continue
                value = tuple(row[names.index(c)] for c in child)
                if all(v is not None for v in value) and value not in parents:
                    errors.append(f"{table}: foreign key violation {[child, parent, parent_cols]}")
    return errors


def make_fixture(rng: random.Random, empty: bool = False, keyed_u: bool = False, fk: bool = False):
    """A random schema, primary database and constraints (``t`` keyed on ``id``; ``u`` optionally keyed on ``k``)."""

    schema = {"t": [["id", "INT64"], ["x", "INT64"], ["y", "INT64"], ["s", "STRING"]], "u": [["k", "INT64"], ["v", "STRING"]]}
    # An optional third table varies the schema without making a query reference a missing field.
    if rng.choice([False, True]):
        schema["p"] = [["id", "INT64"], ["extra", rng.choice(["INT64", "STRING"])]]
    not_null_x = rng.choice([False, True])
    domain = [-2, -1, 0, 1, 2] + ([] if not_null_x else [None])
    strings = [None, "", "a", "b", "O'Reilly"]
    count = 0 if empty else rng.randint(2, 6)
    tables = {
        "t": [[i, rng.choice(domain), rng.choice([-1, 0, 1, None]), rng.choice(strings)] for i in range(count)],
        "u": [[rng.choice([None, -1, 0, 1, 2]), rng.choice(strings)] for _ in range(0 if empty else rng.randint(1, 4))],
    }
    constraints = {"t": {"not_null": ["id"] + (["x"] if not_null_x else []), "keys": [["id"]]}}
    if keyed_u:
        tables["u"] = [[k, rng.choice(strings)] for k in rng.sample([-1, 0, 1, 2], 0 if empty else rng.randint(1, 4))]
        constraints["u"] = {"not_null": ["k"], "keys": [["k"]]}
        if fk:
            constraints["t"]["foreign_keys"] = [[["y"], "u", ["k"]]]
            parents = [row[0] for row in tables["u"]]
            for row in tables["t"]:
                row[2] = rng.choice(parents + [None]) if parents else None
    if "p" in schema:
        tables["p"] = [[0, None], [1, 1 if schema["p"][1][1] == "INT64" else "a"]]
        constraints["p"] = {"not_null": ["id"], "keys": [["id"]]}
    return schema, tables, constraints


def additional_fixtures(case: dict, rng: random.Random, randoms: int = 2) -> list[dict]:
    """``randoms`` random databases, then NULL-heavy, duplicate-heavy and empty ones, all respecting the constraints."""

    schema, rules = case["schema"], case["constraints"]

    def build(mode):
        result = {}
        for table, columns in schema.items():
            count = 0 if mode == "empty" else (4 if mode in ("null_heavy", "duplicates") else rng.randint(0, 6))
            required = _required(rules.get(table, {}))
            keyed = {c for key in rules.get(table, {}).get("keys", []) for c in key}
            rows = []
            for i in range(count):
                row = []
                for name, kind in columns:
                    if name in keyed:
                        value = i if mode != "random" or kind != "INT64" else i - 1
                    elif mode == "null_heavy" and name not in required:
                        value = None
                    elif mode == "duplicates":
                        value = 1 if kind == "INT64" else "a"
                    else:
                        values = [-2, -1, 0, 1, 2] if kind == "INT64" else ["", "a", "b", "O'Reilly"]
                        if name not in required:
                            values = values + [None]
                        value = rng.choice(values)
                    row.append(value)
                rows.append(row)
            result[table] = rows
        # MATCH SIMPLE foreign keys, once every parent's key values exist
        for table, constraint in rules.items():
            names = [c[0] for c in schema[table]]
            for child, parent, pcols in constraint.get("foreign_keys", []):
                pnames = [c[0] for c in schema[parent]]
                parents = [tuple(r[pnames.index(c)] for c in pcols) for r in result[parent]]
                for row in result[table]:
                    if not parents:
                        for c in child:
                            row[names.index(c)] = None
                    elif all(row[names.index(c)] is not None for c in child):
                        for c, value in zip(child, rng.choice(parents)):
                            row[names.index(c)] = value
        return result

    modes = [f"random_{i + 1}" for i in range(randoms)] + ["null_heavy", "duplicates", "empty"]
    return [{"name": mode, "tables": build("random" if mode.startswith("random") else mode)} for mode in modes]


# ---------------------------------------------------------------------------
# Oracle: BigQuery SQL in DuckDB, inside a domain where both engines agree
# ---------------------------------------------------------------------------


def _root(node: exp.Expression) -> exp.Expression:
    while node.parent is not None:
        node = node.parent
    return node


def _shadowed(table: exp.Table) -> bool:
    return any(cte.alias_or_name.casefold() == table.name.casefold() for cte in _root(table).find_all(exp.CTE))


def _total_order(select: exp.Select, case: dict, order=None) -> bool:
    """Conservative total-order check: every column of a base table key is ordered."""

    order = select.args.get("order") if order is None else order
    source = select.args.get("from_")
    if not order or not source or not isinstance(source.this, exp.Table):
        return False
    if any(select.args.get(k) for k in ("joins", "group", "distinct")):
        return False
    source = source.this
    if _shadowed(source):
        return False
    ordered = {o.this.name for o in order.expressions if isinstance(o.this, exp.Column) and (not o.this.table or o.this.table == source.alias_or_name)}
    for projection in select.expressions:
        if isinstance(projection, exp.Alias) and projection.alias in ordered:
            column = projection.this
            if not (isinstance(column, exp.Column) and column.name == projection.alias and (not column.table or column.table == source.alias_or_name)):
                return False  # ORDER BY can bind an output alias instead of the key
    return any(set(key) <= ordered for key in case.get("constraints", {}).get(source.name, {}).get("keys", []))


def _direct_integer_column(column: exp.Column, case: dict) -> bool:
    """Resolve only an unshadowed column of a single physical table; refuse other scopes."""

    select = column.find_ancestor(exp.Select)
    source = select.args.get("from_") if select is not None else None
    if not source or not isinstance(source.this, exp.Table) or select.args.get("joins"):
        return False
    table = source.this
    if column.table and column.table.casefold() != table.alias_or_name.casefold():
        return False
    if _shadowed(table):
        return False
    return any(name.casefold() == column.name.casefold() and kind == "INT64" for name, kind in case["schema"].get(table.name, []))


def _large_values(case: dict) -> bool:
    return any(type(v) is int and abs(v) > 10**9 for rows in case["tables"].values() for row in rows for v in row)


def convert_sql(sql: str, case: dict) -> str:
    """DuckDB SQL for ``sql``, or ``UnsupportedConversion`` outside the semantics both engines share."""

    try:
        parsed = sqlglot.parse(sql, read=case.get("dialect", "bigquery"))
    except sqlglot.errors.SqlglotError as exc:
        raise UnsupportedConversion(f"parse failure: {exc}") from exc
    if len(parsed) != 1 or not isinstance(parsed[0], exp.Query):
        raise UnsupportedConversion("exactly one read-only query is required")
    tree = parsed[0]
    large = _large_values(case)
    for node in tree.walk():
        name = type(node).__name__
        if name not in SAFE_NODES:
            raise UnsupportedConversion(f"unsupported semantic conversion: {name}")
        if isinstance(node, exp.DataType) and node.this.value not in ("BIGINT", "INT", "DOUBLE", "FLOAT", "BOOLEAN", "VARCHAR"):
            raise UnsupportedConversion(f"unsupported cast type: {node.this.value}")
        if isinstance(node, exp.Literal):
            if node.is_string and node.this.casefold() in ("nan", "inf", "infinity", "+inf", "-inf"):
                raise UnsupportedConversion("NaN/Infinity semantics differ across engines")
            if not node.is_string:
                try:
                    value = Fraction(node.this)
                except (ValueError, ZeroDivisionError) as exc:
                    raise UnsupportedConversion("unsupported numeric literal") from exc
                if abs(value) >= 2**63:
                    raise UnsupportedConversion("numeric literal outside guarded finite range")
        if isinstance(node, exp.Like) and (node.args.get("escape") or not isinstance(node.expression, exp.Literal) or "\\" in node.expression.this):
            raise UnsupportedConversion("LIKE escapes differ across engines")
        if isinstance(node, exp.Cast):
            target = node.args["to"].this.value
            if target in ("DOUBLE", "FLOAT"):
                if not isinstance(node.this, (exp.Column, exp.Literal, exp.Null)):
                    raise UnsupportedConversion("float cast requires guarded integer input")
                if isinstance(node.this, exp.Literal) and node.this.is_string:
                    raise UnsupportedConversion("string-to-float runtime errors/NaN are unsupported")
                # Name-only lookup is unsafe: q.x can be a STRING CTE output even when a physical table declares x INT64.
                if isinstance(node.this, exp.Column) and not _direct_integer_column(node.this, case):
                    raise UnsupportedConversion("float cast column must resolve to INT64")
            elif not isinstance(node.this, exp.Null):
                raise UnsupportedConversion("potentially failing/narrowing casts are unsupported")
        if large and isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Sum, exp.Avg)):
            # Small fixture values bound exact integer intermediates. Large integers are allowed only for explicit
            # FLOAT64 coercion probes, never in integer arithmetic or aggregation that could overflow.
            if not (isinstance(node, exp.Mul) and isinstance(node.expression, exp.Literal) and node.expression.this.lower() == "1e0"):
                raise UnsupportedConversion("large integer arithmetic overflow domain is unguarded")
        if isinstance(node, exp.Limit):
            amount = node.expression
            if not isinstance(amount, exp.Literal) or amount.is_string or not amount.this.isdigit():
                raise UnsupportedConversion("nonconstant LIMIT")
            if int(amount.this) > 0 and not (isinstance(node.parent, exp.Select) and _total_order(node.parent, case)):
                raise UnsupportedConversion("positive LIMIT requires a declared key total order")
        if node.args.get("offset") is not None:
            raise UnsupportedConversion("OFFSET is unsupported")
        if isinstance(node, exp.RowNumber):
            window = node.find_ancestor(exp.Window)
            select = node.find_ancestor(exp.Select)
            if not window or not select:
                raise UnsupportedConversion("ROW_NUMBER outside guarded window")
            # The original ancestors stay, so a surrounding WITH cannot lend the key of a shadowed table.
            if not window.args.get("order") or not _total_order(select, case, window.args["order"]):
                raise UnsupportedConversion("ROW_NUMBER requires a declared key total order")
        if isinstance(node, exp.Window) and node.args.get("spec") is not None:
            raise UnsupportedConversion("explicit window frames are unsupported")
    # sqlglot reads ``a = b IS NULL`` as ``a = (b IS NULL)``; BigQuery, DuckDB and PostgreSQL read ``(a = b) IS NULL``,
    # and the operand order of other comparison chains differs across engines too. Only parentheses settle them.
    comparisons = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Like)
    for node in tree.find_all(*comparisons):
        for arg in ("this", "expression"):
            operand = node.args.get(arg)
            if isinstance(operand, exp.Not):
                operand = operand.this
            if isinstance(operand, comparisons):
                raise UnsupportedConversion("comparison chain without parentheses reads differently across engines")
    # Operators are written with sqlglot's own precedence, which is not DuckDB's: Is(Not(Is(y, NULL)), NULL) comes
    # out as ``NOT y IS NULL IS NULL``, read by DuckDB as NOT ((y IS NULL) IS NULL). Parenthesizing every operator
    # operand keeps the reading the prover gets.
    operators = (exp.Binary, exp.Not, exp.Between, exp.In, exp.Like, exp.Neg)
    for node in list(tree.find_all(*operators)):
        for arg in ("this", "expression"):
            operand = node.args.get(arg)
            if isinstance(operand, operators) and not isinstance(operand, exp.Paren):
                paren = exp.Paren()
                operand.replace(paren)
                paren.set("this", operand)
    try:
        return tree.sql(dialect="duckdb", unsupported_level=sqlglot.ErrorLevel.RAISE)
    except (sqlglot.errors.SqlglotError, ValueError) as exc:
        raise UnsupportedConversion(f"serialization failure: {exc}") from exc


def normalize_value(value):
    """Exact numbers across int and float; NULL, booleans and strings kept apart."""

    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("number", value, 1)
    if type(value) is float:
        if not math.isfinite(value):
            raise UnsupportedConversion("nonfinite execution result")
        n, d = value.as_integer_ratio()
        return ("number", n, d)
    if type(value) is str:
        return ("string", value)
    raise UnsupportedConversion(f"unsupported result representation: {type(value).__name__}")


def bag(rows) -> Counter:
    return Counter(tuple(normalize_value(v) for v in row) for row in rows)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_fixture(connection, case: dict) -> None:
    """Create and fill the tables; DuckDB enforces the same constraints the prover is given."""

    from kumosql.duckdb_load import insert_rows

    pending = dict(case["schema"])
    loaded: set = set()
    while pending:
        progressed = False
        for table, columns in list(pending.items()):
            rules = case.get("constraints", {}).get(table, {})
            if any(fk[1] not in loaded for fk in rules.get("foreign_keys", [])):
                continue
            required = _required(rules)
            ddl = [f'{_quote(n)} {"BIGINT" if kind == "INT64" else "VARCHAR"}' + (" NOT NULL" if n in required else "") for n, kind in columns]
            for key in rules.get("keys", []):
                ddl.append("UNIQUE (" + ",".join(map(_quote, key)) + ")")
            for child, parent, parent_cols in rules.get("foreign_keys", []):
                ddl.append("FOREIGN KEY (" + ",".join(map(_quote, child)) + ") REFERENCES " + _quote(parent) + " (" + ",".join(map(_quote, parent_cols)) + ")")
            connection.execute(f'CREATE TABLE {_quote(table)} ({",".join(ddl)})')
            insert_rows(connection, _quote(table), case["tables"][table])
            loaded.add(table)
            del pending[table]
            progressed = True
        if not progressed:
            raise UnsupportedConversion("cyclic/unbound fixture foreign keys")


def execute(case: dict, options: dict) -> dict:
    """Run both queries on every database of ``case``, with DuckDB's optimizer on and off.

    A difference counts only when the unoptimized run shows it. One that only the unoptimized run shows counts too
    (``optimizer_masked_differences``): the optimizer made two different queries agree."""

    import duckdb

    from kumosql.duckdb_load import run_unoptimized

    errors = fixture_errors(case)
    if errors:
        return {"execution": "invalid_fixture", "reasons": errors}
    datasets = [{"name": "primary", "tables": case["tables"]}] + case.get("fixture_variants", [])
    observations = []
    sqls: list = []
    converted: dict = {}
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        connection.execute(f"SET memory_limit='{int(options['memory_mb'])}MB'")
        for number, dataset in enumerate(datasets):
            fixture = dict(case, tables=dataset["tables"])
            errors = fixture_errors(fixture)
            if errors:
                return {"execution": "invalid_fixture", "dataset": dataset["name"], "reasons": errors}
            # the guards depend on the database's values (large integers)
            large = _large_values(fixture)
            if large not in converted:
                converted[large] = [convert_sql(case[key], fixture) for key in ("left", "right")]
            sqls = converted[large]
            # one schema per database, so a single connection serves them all
            connection.execute(f"CREATE SCHEMA db{number}")
            connection.execute(f"SET schema = 'db{number}'")
            load_fixture(connection, fixture)
            results = []
            for sql in sqls:
                rows = connection.execute(sql).fetchmany(options["max_result_rows"] + 1)
                if len(rows) > options["max_result_rows"]:
                    return {"execution": "skipped", "reason": "result row limit exceeded"}
                results.append(rows)
            equal = bag(results[0]) == bag(results[1])
            optimizer_only = masked = False
            # Both plans run on every database: the optimizer's wrong rows can make two different queries agree as
            # easily as they can make two equal ones differ, and a false proof must not hide behind the first.
            plain = run_unoptimized(connection, *sqls)
            plain_equal = bag(plain[0]) == bag(plain[1])
            if not equal and plain_equal:
                equal, optimizer_only = True, True  # a DuckDB optimizer disagreement (#347) is not evidence
            elif not plain_equal:
                masked = equal
                equal, results = False, plain
            observations.append({
                "name": dataset["name"], "equal_bags": equal, "optimizer_only_difference": optimizer_only, "optimizer_masked_difference": masked,
                "left_rows": results[0], "right_rows": results[1],
            })
    except UnsupportedConversion as exc:
        return {"execution": "skipped", "reason": str(exc)}
    except duckdb.Error as exc:
        # Binding, typing and runtime errors (a scalar subquery with two rows) never become evidence.
        return {"execution": "execution_error", "reason": str(exc)[:500], "duckdb_sql": sqls}
    finally:
        connection.close()
    equal = all(o["equal_bags"] for o in observations)
    witness = next((o for o in observations if not o["equal_bags"]), None)
    return {
        "execution": "ok",
        "duckdb_sql": sqls,
        "equal_bags": equal,
        "counterexample_dataset": witness["name"] if witness else None,
        "left_rows": witness["left_rows"] if witness else None,
        "right_rows": witness["right_rows"] if witness else None,
        "optimizer_only_differences": sum(o["optimizer_only_difference"] for o in observations),
        "optimizer_masked_differences": sum(o["optimizer_masked_difference"] for o in observations),
    }


def prover_kwargs(case: dict, options: dict) -> dict:
    from kumosql.smt_equivalence import TableConstraints

    kwargs = {"dialect": case.get("dialect", "bigquery"), "timeout_ms": options["solver_timeout_ms"], "search_counterexample": False}
    if case.get("pass_schema", True):
        kwargs["schema"] = {t: [c[0] for c in cols] for t, cols in case["schema"].items()}
        kwargs["types"] = {t: dict(cols) for t, cols in case["schema"].items()}
    if case.get("constraints"):
        kwargs["constraints"] = {
            t: TableConstraints(
                not_null=frozenset(c.get("not_null", [])),
                keys=tuple(tuple(k) for k in c.get("keys", [])),
                foreign_keys=tuple((tuple(a), p, tuple(b)) for a, p, b in c.get("foreign_keys", [])),
            )
            for t, c in case["constraints"].items()
        }
    return kwargs


def classify(verdict: str | None, equal: bool, label) -> str | None:
    if verdict == "proven_equivalent" and not equal:
        return "false_proof"
    if label is True and not equal:
        return "label_error"
    if verdict == "not_equivalent" and label is True:
        return "suspected_false_refutation"
    return None


def worker_evaluate(case: dict, options: dict) -> dict:
    """Execute the pair, then (unless ``options['oracle_only']``) ask the prover; one JSON-safe result."""

    result = execute(case, options)
    if result["execution"] != "ok" or options.get("oracle_only"):
        return result
    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    started = time.monotonic()
    try:
        proof = prove_equivalent_algebraic(case["left"], case["right"], **prover_kwargs(case, options))
        result["prover"] = {"status": proof.status.value, "reason": proof.reason, "assumptions": list(proof.assumptions)}
    except Exception as exc:  # a prover crash is a finding of its own, never a verdict
        result["prover"] = {"status": "checker_error", "reason": f"{type(exc).__name__}: {exc}"[:500]}
    result["prover_seconds"] = round(time.monotonic() - started, 3)
    result["discrepancy"] = classify(result["prover"]["status"], result["equal_bags"], case.get("mutation", {}).get("known_equivalent"))
    return result


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------


def default_options() -> dict:
    return {"query_timeout_seconds": 20.0, "solver_timeout_ms": 2000, "memory_mb": 256, "max_result_rows": 1000, "minimize_checks": 60, "minimize_seconds": 60.0}


class Worker:
    """One long-lived child process answering one JSON request per line; restarted after a timeout or crash."""

    def __init__(self):
        self.process = None
        self._start()

    def _start(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, args=(self.process, self.lines), daemon=True).start()

    @staticmethod
    def _read(process, lines):
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def evaluate(self, case: dict, options: dict, timeout: float) -> dict:
        if timeout <= 0:
            return {"execution": "timeout", "reason": "run deadline exhausted"}
        try:
            self.process.stdin.write(_json({"case": case, "options": options}) + "\n")
            self.process.stdin.flush()
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            self._start()
            return {"execution": "timeout", "reason": "query/prover wall timeout exceeded"}
        except OSError as exc:
            line, error = None, str(exc)
        if line is None:
            self.close()
            self._start()
            return {"execution": "worker_error", "reason": "child process exited"}
        try:
            return json.loads(line)
        except ValueError:
            return {"execution": "worker_error", "reason": "child did not emit strict JSON", "details": line[-2000:]}


class Pool:
    def __init__(self, jobs: int):
        self.workers: queue.Queue = queue.Queue()
        self.all = [Worker() for _ in range(max(1, jobs))]
        for worker in self.all:
            self.workers.put(worker)
        self.executor = ThreadPoolExecutor(max_workers=len(self.all))

    def evaluate(self, case: dict, options: dict, timeout: float | None = None) -> dict:
        worker = self.workers.get()
        try:
            return worker.evaluate(case, options, options["query_timeout_seconds"] if timeout is None else timeout)
        finally:
            self.workers.put(worker)

    def map(self, cases, options: dict, deadline: float):
        def one(case):
            return self.evaluate(case, options, min(options["query_timeout_seconds"], deadline - time.monotonic()))

        return self.executor.map(one, cases)

    def close(self):
        self.executor.shutdown()
        for worker in self.all:
            worker.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _worker_loop() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr  # nothing but answers on the pipe
    import logging

    logging.getLogger("sqlglot").setLevel(logging.CRITICAL)
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            answer = worker_evaluate(payload["case"], payload["options"])
            text = _json(answer)
        except Exception as exc:
            text = _json({"execution": "worker_error", "reason": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:]})
        out.write(text + "\n")
        out.flush()
    return 0


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

INT_COLUMNS = {"t": ["id", "x", "y"], "u": ["k"]}
STRING_COLUMNS = {"t": ["s"], "u": ["v"]}


class Fragments:
    """Random, well-typed SQL pieces over table aliases (``{alias: table}``)."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def choice(self, items):
        return self.rng.choice(list(items))

    def lit(self) -> str:
        return str(self.rng.choice([-1, 0, 1, 2]))

    def icol(self, aliases: dict) -> str:
        alias = self.choice(aliases)
        return f"{alias}.{self.choice(INT_COLUMNS[aliases[alias]])}"

    def scol(self, aliases: dict) -> str:
        alias = self.choice(aliases)
        return f"{alias}.{self.choice(STRING_COLUMNS[aliases[alias]])}"

    def col(self, aliases: dict) -> str:
        return self.icol(aliases) if self.rng.random() < 0.75 else self.scol(aliases)

    def string(self) -> str:
        return "'" + self.choice(["", "a", "b"]) + "'"

    def atom(self, aliases: dict) -> str:
        r = self.rng.randrange(9)
        op = self.choice(["=", "<>", "<", "<=", ">", ">="])
        if r == 0:
            return f"{self.icol(aliases)} {op} {self.lit()}"
        if r == 1:
            return f"{self.icol(aliases)} {op} {self.icol(aliases)}"
        if r == 2:
            return f"{self.col(aliases)} IS {self.choice(['', 'NOT '])}NULL"
        if r == 3:
            items = [self.lit(), self.lit()] + (["NULL"] if self.rng.random() < 0.3 else [])
            return f"{self.icol(aliases)} {self.choice(['', 'NOT '])}IN ({', '.join(items)})"
        if r == 4:
            return f"{self.scol(aliases)} {self.choice(['=', '<>', '<'])} {self.string()}"
        if r == 5:
            lo = self.rng.randint(-2, 1)
            return f"{self.icol(aliases)} BETWEEN {lo} AND {lo + self.rng.randint(0, 2)}"
        if r == 6:
            return f"{self.scol(aliases)} LIKE '{self.choice(['a%', '%', '_', 'O%'])}'"
        if r == 7:
            return f"({self.icol(aliases)} IS {self.choice(['', 'NOT '])}DISTINCT FROM {self.choice([self.icol(aliases), self.lit()])})"
        return f"{self.icol(aliases)} {op} {self.lit()}"

    def pred(self, aliases: dict, depth: int = 2) -> str:
        r = self.rng.random()
        if depth <= 0 or r < 0.55:
            return self.atom(aliases)
        if r < 0.67:
            return f"NOT ({self.pred(aliases, depth - 1)})"
        if r < 0.85:
            return f"({self.pred(aliases, depth - 1)} AND {self.pred(aliases, depth - 1)})"
        return f"({self.pred(aliases, depth - 1)} OR {self.pred(aliases, depth - 1)})"

    def ival(self, aliases: dict) -> str:
        r = self.rng.randrange(6)
        c = self.icol(aliases)
        if r == 0:
            return self.lit()
        if r == 1:
            return f"{c} + {self.lit()}"
        if r == 2:
            return f"COALESCE({c}, {self.lit()})"
        if r == 3:
            return f"CASE WHEN {self.pred(aliases, 1)} THEN {c} ELSE {self.lit()} END"
        if r == 4:
            return f"IF({self.pred(aliases, 1)}, {self.lit()}, {c})"
        return c

    def join(self) -> str:
        return self.choice(["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN"])

    def agg(self, aliases: dict) -> str:
        c = self.icol(aliases)
        return self.choice([f"COUNT(*)", f"COUNT({c})", f"COUNT(DISTINCT {c})", f"SUM({c})", f"MIN({c})", f"MAX({c})", f"MIN({self.scol(aliases)})"])


T = {"t": "t"}
U = {"u": "u"}
TU = {"t": "t", "u": "u"}


def _pick(f: Fragments, options):
    """One (right side, label) from ``[(sql, label), ...]``; label True means equivalent on every database."""

    return f.choice(options)


def tpl_predicate_where(f, ctx):
    p, q = f.pred(T), f.pred(T)
    left = f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p}"
    right, label = _pick(f, [
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} AND {p}", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE NOT (NOT ({p}))", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE ({p}) IS TRUE", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE ({p}) IS NOT FALSE", None),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE COALESCE({p}, FALSE)", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE COALESCE({p}, TRUE)", None),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE CASE WHEN {p} THEN TRUE ELSE FALSE END", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE CASE WHEN NOT ({p}) THEN FALSE ELSE TRUE END", None),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE ({p} OR {q}) AND ({p} OR NOT ({q}))", None),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} OR ({p} AND {q})", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {q} AND {p}", None),
        (f"SELECT * FROM (SELECT t.id, t.x, t.y, t.s FROM t WHERE {q}) AS d WHERE {p.replace('t.', 'd.')}", None),
    ])
    return "predicate_where", left, right, label


def tpl_predicate_value(f, ctx):
    p = f.pred(T)
    left = f"SELECT t.id, {p} AS b FROM t"
    right, label = _pick(f, [
        (f"SELECT t.id, CASE WHEN {p} THEN TRUE ELSE FALSE END AS b FROM t", None),
        (f"SELECT t.id, NOT (NOT ({p})) AS b FROM t", True),
        (f"SELECT t.id, ({p}) IS TRUE AS b FROM t", None),
        (f"SELECT t.id, COALESCE({p}, FALSE) AS b FROM t", None),
        (f"SELECT t.id, IF({p}, TRUE, IF(NOT ({p}), FALSE, NULL)) AS b FROM t", True),
        (f"SELECT t.id, ({p}) AND ({p}) AS b FROM t", True),
        (f"SELECT t.id, ({p}) OR FALSE AS b FROM t", True),
        (f"SELECT t.id, ({p}) AND TRUE AS b FROM t", True),
        (f"SELECT t.id, ({p}) OR (({p}) IS NULL) AS b FROM t", None),
    ])
    return "predicate_value", left, right, label


def tpl_null_arithmetic(f, ctx):
    c, d = f.icol(T), f.icol(T)
    n = f.lit()
    pairs = [
        (f"{c} - {c}", "0", None),
        (f"{c} * 0", "0", None),
        (f"{c} + 0", c, True),
        (f"COALESCE({c}, {c})", c, True),
        (f"COALESCE({c}, {n})", c, None),
        (f"CASE WHEN {c} IS NULL THEN {n} ELSE {c} END", f"COALESCE({c}, {n})", True),
        (f"IFNULL({c}, {n})", f"COALESCE({c}, {n})", True),
        (f"{c} = {c}", "TRUE", None),
        (f"{c} = {c}", f"{c} IS NOT NULL OR NULL", True),
        (f"CASE WHEN {c} > {n} THEN 1 WHEN {c} <= {n} THEN 0 END", f"CASE WHEN {c} > {n} THEN 1 ELSE 0 END", None),
        (f"{c} IS DISTINCT FROM {d}", f"{c} <> {d}", None),
        (f"NOT ({c} IS DISTINCT FROM {d})", f"({c} = {d} OR ({c} IS NULL AND {d} IS NULL))", None),
        (f"{c} IN ({n}, {f.lit()})", f"{c} = {n}", None),
        (f"{c} NOT IN ({n}, NULL)", f"{c} <> {n}", None),
        (f"CASE WHEN {f.pred(T, 1)} THEN {c} ELSE {c} END", c, True),
        (f"-(-{c})", c, True),
        (f"{c} + {d}", f"{d} + {c}", True),
        (f"COALESCE({c}, {d}, {n})", f"COALESCE(COALESCE({c}, {d}), {n})", True),
        (f"IF({c} IS NULL, NULL, {c} + 1)", f"{c} + 1", True),
    ]
    left_e, right_e, label = f.choice(pairs)
    if f.rng.random() < 0.5:
        left_e, right_e = right_e, left_e
    where = f" WHERE {f.pred(T)}" if f.rng.random() < 0.4 else ""
    return "null_arithmetic", f"SELECT t.id, {left_e} AS v FROM t{where}", f"SELECT t.id, {right_e} AS v FROM t{where}", label


def tpl_count_variants(f, ctx):
    group = f.choice(["", "t.s", "t.x", "t.y"])
    where = f" WHERE {f.pred(T)}" if f.rng.random() < 0.5 else ""
    gsel = f"{group}, " if group else ""
    gby = f" GROUP BY {group}" if group else ""
    x = f.choice(["t.x", "t.y", "t.id"])
    options = [
        ("COUNT(*)", "COUNT(t.id)", True),
        ("COUNT(*)", f"COUNT({x})", None),
        ("COUNT(*)", "COUNT(1)", True),
        ("COUNT(*)", "SUM(1)", None if not group else True),
        ("COUNT(*)", "COUNT(DISTINCT t.id)", True),
        (f"COUNT({x})", f"SUM(CASE WHEN {x} IS NULL THEN 0 ELSE 1 END)", None if not group else True),
        (f"COUNT({x})", f"COALESCE(SUM(CASE WHEN {x} IS NULL THEN 0 ELSE 1 END), 0)", True),
        (f"COUNT(DISTINCT {x})", f"COUNT({x})", None),
        (f"MIN(DISTINCT {x})", f"MIN({x})", True),
        (f"MAX(DISTINCT {x})", f"MAX({x})", True),
        (f"SUM(DISTINCT {x})", f"SUM({x})", None),
        (f"SUM({x})", f"SUM(COALESCE({x}, 0))", None),
        (f"MAX({x})", f"MAX(COALESCE({x}, -100))", None),
        (f"COUNT(CASE WHEN {f.pred(T, 1)} THEN 1 END)", "COUNT(*)", None),
        (f"MIN({x})", f"-MAX(-{x})", True),
    ]
    a, b, label = f.choice(options)
    if f.rng.random() < 0.3 and not group:
        # a global aggregate that filters its only row
        having = f" HAVING {f.choice(['COUNT(*) > 0', 'COUNT(*) > 1', f'MAX({x}) IS NOT NULL'])}"
        return "count_variants", f"SELECT {a} AS n FROM t{where}{having}", f"SELECT {b} AS n FROM t{where}{having}", label
    return "count_variants", f"SELECT {gsel}{a} AS n FROM t{where}{gby}", f"SELECT {gsel}{b} AS n FROM t{where}{gby}", label


def tpl_distinct(f, ctx):
    cols = f.choice([["t.id", "t.x"], ["t.x", "t.s"], ["t.x"], ["t.id"], ["t.s", "t.y"]])
    where = f" WHERE {f.pred(T)}" if f.rng.random() < 0.5 else ""
    sel = ", ".join(cols)
    left = f"SELECT DISTINCT {sel} FROM t{where}"
    joined = f"SELECT DISTINCT {', '.join(cols)} FROM t {f.choice(['JOIN', 'LEFT JOIN'])} u ON t.x = u.k{where}"
    right, label = _pick(f, [
        (f"SELECT {sel} FROM t{where}", True if "t.id" in cols else None),
        (f"SELECT {sel} FROM t{where} GROUP BY {sel}", True),
        (f"SELECT DISTINCT * FROM (SELECT {sel} FROM t{where}) AS d", True),
        (f"SELECT {sel} FROM t{where} UNION DISTINCT SELECT {sel} FROM t{where}", True),
        (joined, None),
        (f"SELECT {sel} FROM t{where} INTERSECT DISTINCT SELECT {sel} FROM t", True),
        (f"SELECT {sel} FROM t{where} EXCEPT DISTINCT SELECT {sel} FROM t WHERE FALSE", True),
    ])
    return "distinct", left, right, label


def tpl_group_key(f, ctx):
    x = f.choice(["t.x", "t.y"])
    pairs = [
        ("COUNT(*)", "1", True),
        (f"SUM({x})", x, True),
        (f"MIN({x})", x, True),
        (f"COUNT({x})", f"CASE WHEN {x} IS NULL THEN 0 ELSE 1 END", True),
        (f"COUNT({x})", "1", None),
        (f"COUNT(DISTINCT {x})", f"IF({x} IS NULL, 0, 1)", True),
        (f"MAX(t.s)", "t.s", True),
    ]
    a, b, label = f.choice(pairs)
    if f.rng.random() < 0.3:
        return "group_key", f"SELECT t.x, {a} AS n FROM t GROUP BY t.x, t.s", f"SELECT t.x, {a} AS n FROM t GROUP BY t.x", None
    return "group_key", f"SELECT t.id, {a} AS n FROM t GROUP BY t.id", f"SELECT t.id, {b} AS n FROM t", label


def _join_on(f) -> str:
    return f.choice([
        "t.x = u.k",
        "t.y = u.k",
        f"t.x = u.k AND {f.pred(U, 1)}",
        f"t.x = u.k AND {f.pred(T, 1)}",
        "TRUE",
        "FALSE",
        "t.x <= u.k",
        "t.s = u.v",
        f"t.x = u.k OR {f.pred(U, 1)}",
    ])


def tpl_join_type(f, ctx):
    on = _join_on(f)
    j1, j2 = f.join(), f.join()
    where = f.choice([f.pred(U, 1), f.pred(T, 1), f.pred(TU, 1), "u.k IS NOT NULL", "t.id IS NOT NULL", "u.k IS NULL", "t.id IS NULL", f"u.k = {f.lit()}"])
    sel = f.choice(["t.id, t.x, u.k", "t.x, u.v", "u.k, t.s", "t.id, u.k, u.v"])
    left = f"SELECT {sel} FROM t {j1} u ON {on} WHERE {where}"
    right = f"SELECT {sel} FROM t {j2} u ON {on} WHERE {where}"
    return "join_type", left, right, True if j1 == j2 else None


def tpl_on_where(f, ctx):
    j = f.join()
    a = f.choice(["t.x = u.k", "t.y = u.k", "t.s = u.v"])
    side = f.choice(["u", "t", "tu"])
    q = f.pred({"u": U, "t": T, "tu": TU}[side], 1)
    sel = f.choice(["t.id, t.x, u.k", "t.id, u.v", "u.k, t.s"])
    left = f"SELECT {sel} FROM t {j} u ON {a} AND {q}"
    options = [
        (f"SELECT {sel} FROM t {j} u ON {a} WHERE {q}", True if j == "JOIN" else None),
        (f"SELECT {sel} FROM t {j} u ON {q} AND {a}", True),
    ]
    if side == "u":
        # filtering the right input first is sound for inner and left joins
        options.append((f"SELECT {sel} FROM t {j} (SELECT * FROM u WHERE {q}) AS u ON {a}", True if j in ("JOIN", "LEFT JOIN") else None))
    if side == "t":
        options.append((f"SELECT {sel} FROM (SELECT * FROM t WHERE {q}) AS t {j} u ON {a}", True if j in ("JOIN", "RIGHT JOIN") else None))
    right, label = _pick(f, options)
    return "on_where", left, right, label


def tpl_join_commute(f, ctx):
    on = _join_on(f)
    j = f.join()
    mirror = {"JOIN": "JOIN", "LEFT JOIN": "RIGHT JOIN", "RIGHT JOIN": "LEFT JOIN", "FULL JOIN": "FULL JOIN"}
    sel = f.choice(["t.id, t.x, u.k", "t.x, u.v", "u.k, t.s"])
    left = f"SELECT {sel} FROM t {j} u ON {on}"
    right, label = _pick(f, [
        (f"SELECT {sel} FROM u {mirror[j]} t ON {on}", True),
        (f"SELECT {sel} FROM u {j} t ON {on}", True if j in ("JOIN", "FULL JOIN") else None),
        (f"SELECT {sel} FROM t, u WHERE {on}", True if j == "JOIN" else None),
        (f"SELECT {sel} FROM t CROSS JOIN u WHERE {on}", True if j == "JOIN" else None),
    ])
    return "join_commute", left, right, label


def tpl_self_join(f, ctx):
    sel = "a.id, a.x, b.s"
    on = f.choice(["a.id = b.id", "a.x = b.x", "a.id = b.id AND a.x = b.x", "a.id = b.id AND b.y IS NOT NULL"])
    j = f.choice(["JOIN", "LEFT JOIN"])
    left = f"SELECT {sel} FROM t AS a {j} t AS b ON {on}"
    right = "SELECT t.id, t.x, t.s FROM t"
    label = True if on == "a.id = b.id" else None
    if f.rng.random() < 0.3:
        left = f"SELECT a.id, a.x FROM t AS a WHERE EXISTS (SELECT 1 FROM t AS b WHERE {on})"
        right = "SELECT t.id, t.x FROM t" + (" WHERE t.x IS NOT NULL" if "a.x" in on else "")
        label = None
    return "self_join", left, right, label


def tpl_semijoin(f, ctx):
    q = f.pred(U, 1) if f.rng.random() < 0.5 else "TRUE"
    c = f.choice(["t.x", "t.y"])
    left = f"SELECT t.id, t.x FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = {c} AND {q})"
    right, label = _pick(f, [
        (f"SELECT t.id, t.x FROM t WHERE {c} IN (SELECT u.k FROM u WHERE {q})", True),
        (f"SELECT t.id, t.x FROM t JOIN u ON u.k = {c} WHERE {q}", None),
        (f"SELECT t.id, t.x FROM t JOIN (SELECT DISTINCT u.k FROM u WHERE {q}) AS d ON d.k = {c}", True),
        (f"SELECT DISTINCT t.id, t.x FROM t JOIN u ON u.k = {c} WHERE {q}", True),
        (f"SELECT t.id, t.x FROM t WHERE {c} = ANY (SELECT u.k FROM u WHERE {q})", True),
        (f"SELECT t.id, t.x FROM t WHERE (SELECT COUNT(*) FROM u WHERE u.k = {c} AND {q}) > 0", True),
        (f"SELECT t.id, t.x FROM t WHERE EXISTS (SELECT u.k FROM u WHERE u.k = {c} AND {q} GROUP BY u.k)", True),
        (f"SELECT t.id, t.x FROM t WHERE EXISTS (SELECT COUNT(*) FROM u WHERE u.k = {c} AND {q})", None),
    ])
    if "ANY" in right:
        right = right.replace(f"{c} = ANY (", f"{c} IN (")
    return "semijoin", left, right, label


def tpl_antijoin(f, ctx):
    q = f.pred(U, 1) if f.rng.random() < 0.5 else "TRUE"
    c = f.choice(["t.x", "t.y"])
    left = f"SELECT t.id, t.x FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.k = {c} AND {q})"
    right, label = _pick(f, [
        (f"SELECT t.id, t.x FROM t WHERE {c} NOT IN (SELECT u.k FROM u WHERE {q})", None),
        (f"SELECT t.id, t.x FROM t WHERE NOT ({c} IN (SELECT u.k FROM u WHERE {q}))", None),
        (f"SELECT t.id, t.x FROM t LEFT JOIN (SELECT DISTINCT u.k FROM u WHERE {q}) AS d ON d.k = {c} WHERE d.k IS NULL", True),
        (f"SELECT t.id, t.x FROM t LEFT JOIN u ON u.k = {c} AND {q} WHERE u.k IS NULL", True),
        (f"SELECT t.id, t.x FROM t LEFT JOIN u ON u.k = {c} WHERE u.k IS NULL AND {q}", None),
        (f"SELECT t.id, t.x FROM t WHERE (SELECT COUNT(*) FROM u WHERE u.k = {c} AND {q}) = 0", True),
        (f"SELECT t.id, t.x FROM t EXCEPT DISTINCT SELECT t.id, t.x FROM t JOIN u ON u.k = {c} WHERE {q}", True),
    ])
    return "antijoin", left, right, label


def tpl_membership_value(f, ctx):
    q = f.pred(U, 1) if f.rng.random() < 0.5 else "TRUE"
    c = f.choice(["t.x", "t.y", f"t.x + {f.lit()}"])
    neg = f.choice(["", "NOT "])
    left = f"SELECT t.id, {c} {neg}IN (SELECT u.k FROM u WHERE {q}) AS m FROM t"
    right, label = _pick(f, [
        (f"SELECT t.id, {neg}EXISTS (SELECT 1 FROM u WHERE u.k = {c} AND {q}) AS m FROM t", None),
        (f"SELECT t.id, {neg}({c} IN (SELECT u.k FROM u WHERE {q})) AS m FROM t", True),
        (f"SELECT t.id, {neg}({c} IN (SELECT DISTINCT u.k FROM u WHERE {q})) AS m FROM t", True),
        (f"SELECT t.id, {neg}({c} IN (SELECT u.k FROM u WHERE {q} AND u.k IS NOT NULL)) AS m FROM t", None),
        (f"SELECT t.id, {neg}({c} IN (SELECT u.k FROM u WHERE {q} UNION ALL SELECT u.k FROM u WHERE FALSE)) AS m FROM t", True),
        (f"SELECT t.id, {neg}({c} IN (SELECT u.k FROM u WHERE {q} LIMIT 0)) AS m FROM t", None),
    ])
    return "membership_value", left, right, label


def _branch(f, table: str, alias: str = "c") -> str:
    if table == "t":
        return f"SELECT {f.choice(['t.x', 't.y', 't.id'])} AS {alias} FROM t WHERE {f.pred(T, 1)}"
    return f"SELECT u.k AS {alias} FROM u WHERE {f.pred(U, 1)}"


def tpl_setop(f, ctx):
    a, b = _branch(f, "t"), _branch(f, f.choice(["t", "u"]))
    op = f.choice(["UNION ALL", "UNION DISTINCT", "INTERSECT DISTINCT", "EXCEPT DISTINCT"])
    w = f"d.c {f.choice(['>', '=', '<>', '<='])} {f.lit()}" if f.rng.random() < 0.8 else "d.c IS NULL"
    left = f"SELECT d.c FROM ({a} {op} {b}) AS d WHERE {w}"

    def pushed(branch):
        return f"SELECT * FROM ({branch}) AS d WHERE {w}"

    right, label = _pick(f, [
        (f"{pushed(a)} {op} {pushed(b)}", True),
        (f"{pushed(a)} {op} {b}", True if op in ("INTERSECT DISTINCT", "EXCEPT DISTINCT") else None),
        (f"{a} {op} {pushed(b)}", True if op == "INTERSECT DISTINCT" else None),
        (f"SELECT d.c FROM ({b} {op} {a}) AS d WHERE {w}", True if op != "EXCEPT DISTINCT" else None),
        (f"SELECT d.c FROM ({a} {f.choice(['UNION ALL', 'UNION DISTINCT', 'INTERSECT DISTINCT', 'EXCEPT DISTINCT'])} {b}) AS d WHERE {w}", None),
        (f"SELECT DISTINCT d.c FROM ({a}) AS d WHERE {w} AND d.c IN (SELECT e.c FROM ({b}) AS e)", True if op == "INTERSECT DISTINCT" and "IS NULL" not in w else None),
        (f"SELECT DISTINCT d.c FROM ({a}) AS d WHERE {w} AND d.c NOT IN (SELECT e.c FROM ({b}) AS e)", None),
        (f"SELECT DISTINCT d.c FROM ({a}) AS d WHERE {w} AND NOT EXISTS (SELECT 1 FROM ({b}) AS e WHERE e.c IS NOT DISTINCT FROM d.c)", True if op == "EXCEPT DISTINCT" else None),
        (f"SELECT d.c FROM ({a} {op} {b} LIMIT 0) AS d WHERE {w}" if op != "UNION ALL" else f"SELECT d.c FROM ({a} {op} ({b} LIMIT 0)) AS d WHERE {w}", None),
    ])
    return "set_operations", left, right, label


def tpl_setop_aggregate(f, ctx):
    a, b = _branch(f, "t"), _branch(f, "u")
    agg = f.choice(["COUNT(*)", "COUNT(d.c)", "SUM(d.c)", "MAX(d.c)", "MIN(d.c)"])
    left = f"SELECT {agg} AS n FROM ({a} UNION ALL {b}) AS d"
    split = agg.replace("d.c", "e.c")
    one = f"(SELECT {split} FROM ({a}) AS e)"
    two = f"(SELECT {split} FROM ({b}) AS e)"
    right, label = _pick(f, [
        (f"SELECT {one} + {two} AS n", True if agg.startswith("COUNT") else None),
        (f"SELECT COALESCE({one}, 0) + COALESCE({two}, 0) AS n", True if agg.startswith("COUNT") else None),
        (f"SELECT {agg.replace('d.c', 'g.n')} AS n FROM (SELECT {split} AS n FROM ({a}) AS e UNION ALL SELECT {split} AS n FROM ({b}) AS e) AS g", True if agg.startswith(("MAX", "MIN")) else None),
        (f"SELECT {agg} AS n FROM ({b} UNION ALL {a}) AS d", True),
        (f"SELECT {agg} AS n FROM ({a} UNION DISTINCT {b}) AS d", True if agg.startswith(("MAX", "MIN")) else None),
    ])
    return "setop_aggregate", left, right, label


def tpl_grouping_sets(f, ctx):
    where = f" WHERE {f.pred(T)}" if f.rng.random() < 0.4 else ""
    a, b = f.choice([("t.x", "t.s"), ("t.s", "t.y"), ("t.x", "t.y")])
    agg = f.choice(["COUNT(*)", "SUM(t.id)", "MAX(t.id)", "COUNT(t.x)"])
    left = f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY ROLLUP({a}, {b})"
    right, label = _pick(f, [
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY GROUPING SETS(({a}, {b}), ({a}), ())", True),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY GROUPING SETS(({a}, {b}), ({a}))", None),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY GROUPING SETS(({a}, {b}), ({b}), ())", None),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY {a}, {b} UNION ALL SELECT {a}, NULL, {agg} AS n FROM t{where} GROUP BY {a} UNION ALL SELECT NULL, NULL, {agg} AS n FROM t{where}", True),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY {a}, {b} UNION ALL SELECT {a}, NULL, {agg} AS n FROM t{where} GROUP BY {a} UNION ALL SELECT NULL, NULL, {agg} AS n FROM t{where} GROUP BY ()", True),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY CUBE({a}, {b})", None),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY GROUPING SETS(({a}, {b}), ({a}, {b}), ({a}), ())", None),
        (f"SELECT {a}, {b}, {agg} AS n FROM t{where} GROUP BY {a}, ROLLUP({b}) UNION ALL SELECT NULL, NULL, {agg} AS n FROM t{where}", True),
    ])
    if f.rng.random() < 0.3:
        left = f"SELECT {a}, {agg} AS n FROM t{where} GROUP BY GROUPING SETS(({a}), ())"
        right, label = _pick(f, [
            (f"SELECT {a}, {agg} AS n FROM t{where} GROUP BY {a} UNION ALL SELECT NULL, {agg} AS n FROM t{where}", True),
            (f"SELECT {a}, {agg} AS n FROM t{where} GROUP BY ROLLUP({a})", True),
            (f"SELECT {a}, {agg} AS n FROM t{where} GROUP BY {a}", None),
            (f"SELECT {a}, {agg} AS n FROM t{where} GROUP BY GROUPING SETS(({a}), ({a}))", None),
        ])
    return "grouping_sets", left, right, label


def tpl_having(f, ctx):
    g = f.choice(["t.x", "t.s", "t.y"])
    agg = f.choice(["COUNT(*)", "SUM(t.id)", "MAX(t.y)"])
    hp_key = f.pred({"t": "t"}, 1)
    on_key = f.rng.random() < 0.5
    if on_key:
        hp = f.choice([f"{g} IS NOT NULL", f"{g} IS NULL"] + ([f"{g} > {f.lit()}"] if g != "t.s" else [f"{g} = 'a'"]))
    else:
        hp = f"{agg} {f.choice(['>', '<=', '='])} {f.lit()}"
    left = f"SELECT {g}, {agg} AS n FROM t GROUP BY {g} HAVING {hp}"
    right, label = _pick(f, ([(f"SELECT {g}, {agg} AS n FROM t WHERE {hp} GROUP BY {g}", True)] if on_key else []) + [
        (f"SELECT d.g, d.n FROM (SELECT {g} AS g, {agg} AS n FROM t GROUP BY {g}) AS d WHERE {hp.replace(agg, 'd.n').replace(g, 'd.g')}", True),
        (f"SELECT {g}, {agg} AS n FROM t WHERE {hp_key} GROUP BY {g} HAVING {hp}", None),
    ])
    if f.rng.random() < 0.25:
        hp = f.choice(["COUNT(*) > 0", "COUNT(*) = 0", "MAX(t.x) IS NULL", "SUM(t.x) > 0"])
        left = f"SELECT {agg} AS n FROM t HAVING {hp}"
        right, label = _pick(f, [(f"SELECT {agg} AS n FROM t", None), (f"SELECT {agg} AS n FROM t WHERE TRUE HAVING {hp}", True), (f"SELECT {agg} AS n FROM t GROUP BY () HAVING {hp}", True)])
    return "having", left, right, label


def tpl_derived_pushdown(f, ctx):
    kind = f.rng.randrange(3)
    if kind == 0:
        inner = "SELECT t.x, COUNT(*) AS n, SUM(t.y) AS m FROM t GROUP BY t.x"
        w = f.choice([f"d.x > {f.lit()}", "d.x IS NULL", f"d.n > {f.rng.randint(0, 2)}", "d.m IS NULL", f"d.x = {f.lit()} OR d.n > 1"])
        keyed = "d.n" not in w and "d.m" not in w
        right, label = _pick(f, ([(f"SELECT t.x, COUNT(*) AS n, SUM(t.y) AS m FROM t WHERE {w.replace('d.', 't.')} GROUP BY t.x", True)] if keyed else []) + [
            (f"SELECT t.x, COUNT(*) AS n, SUM(t.y) AS m FROM t GROUP BY t.x HAVING {w.replace('d.x', 't.x').replace('d.n', 'COUNT(*)').replace('d.m', 'SUM(t.y)')}", True),
        ])
    elif kind == 1:
        part = f.choice(["t.s", "t.x"])
        inner = f"SELECT t.id, t.x, t.s, COUNT(*) OVER (PARTITION BY {part}) AS c, SUM(t.y) OVER (PARTITION BY {part}) AS m FROM t"
        w = f.choice([f"d.x > {f.lit()}", "d.s IS NULL", "d.c > 1", f"d.{part[2:]} IS NOT NULL", f"d.s = 'a'"])
        right, label = _pick(f, ([] if "d.c" in w else [
            (f"SELECT t.id, t.x, t.s, COUNT(*) OVER (PARTITION BY {part}) AS c, SUM(t.y) OVER (PARTITION BY {part}) AS m FROM t WHERE {w.replace('d.', 't.')}", True if f"d.{part[2:]}" in w else None)]) + [
            (f"SELECT t.id, t.x, t.s, g.c, g.m FROM t JOIN (SELECT {part} AS p, COUNT(*) AS c, SUM(t.y) AS m FROM t GROUP BY {part}) AS g ON g.p = {part} WHERE {w.replace('d.c', 'g.c').replace('d.', 't.')}", None),
            (f"SELECT t.id, t.x, t.s, g.c, g.m FROM t JOIN (SELECT {part} AS p, COUNT(*) AS c, SUM(t.y) AS m FROM t GROUP BY {part}) AS g ON g.p IS NOT DISTINCT FROM {part} WHERE {w.replace('d.c', 'g.c').replace('d.', 't.')}", True),
        ])
    else:
        inner = f"SELECT t.id, t.x + {f.lit()} AS x, t.s FROM t WHERE {f.pred(T, 1)}"
        w = f"d.x {f.choice(['>', '=', '<'])} {f.lit()}"
        right, label = f"SELECT * FROM ({inner}) AS d WHERE TRUE AND {w}", True
    return "derived_pushdown", f"SELECT * FROM ({inner}) AS d WHERE {w}", right, label


def tpl_scalar_subquery(f, ctx):
    c = f.choice(["t.x", "t.y"])
    agg = f.choice(["COUNT(*)", "COUNT(u.v)", "MAX(u.v)", "SUM(u.k)", "MIN(u.k)"])
    q = f.pred(U, 1) if f.rng.random() < 0.4 else "TRUE"
    left = f"SELECT t.id, (SELECT {agg} FROM u WHERE u.k = {c} AND {q}) AS n FROM t"
    grouped = f"(SELECT u.k, {agg} AS n FROM u WHERE {q} GROUP BY u.k)"
    count = agg.startswith("COUNT")
    right, label = _pick(f, [
        (f"SELECT t.id, d.n FROM t LEFT JOIN {grouped} AS d ON d.k = {c}", True if not count else None),
        (f"SELECT t.id, COALESCE(d.n, {0 if agg != 'MAX(u.v)' else repr('')}) AS n FROM t LEFT JOIN {grouped} AS d ON d.k = {c}", True if count else None),
        (f"SELECT t.id, d.n FROM t JOIN {grouped} AS d ON d.k = {c}", None),
        (f"SELECT t.id, (SELECT {agg} FROM u WHERE {c} = u.k AND {q}) AS n FROM t", True),
    ])
    if f.rng.random() < 0.3:
        agg2 = f.choice(["MAX(u.k)", "COUNT(*)", "MIN(u.v)"])
        left = f"SELECT t.id, (SELECT {agg2} FROM u WHERE {q}) AS n FROM t"
        right, label = _pick(f, [
            (f"SELECT t.id, d.n FROM t CROSS JOIN (SELECT {agg2} AS n FROM u WHERE {q}) AS d", True),
            (f"SELECT t.id, d.n FROM t LEFT JOIN (SELECT {agg2} AS n FROM u WHERE {q}) AS d ON TRUE", True),
            (f"SELECT t.id, d.n FROM t JOIN (SELECT {agg2} AS n FROM u WHERE {q} GROUP BY u.k) AS d ON TRUE", None),
        ])
    return "scalar_subquery", left, right, label


def tpl_cte_scope(f, ctx):
    k = f.rng.randint(-1, 1)
    p = f.pred(T, 1)
    kind = f.rng.randrange(4)
    if kind == 0:
        left = f"WITH q AS (SELECT t.id, t.x FROM t WHERE {p}) SELECT q.id, q.x FROM q"
        right, label = _pick(f, [
            (f"SELECT q.id, q.x FROM (SELECT t.id, t.x FROM t WHERE {p}) AS q", True),
            (f"SELECT t.id, t.x FROM t WHERE {p}", True),
            (f"WITH q AS (SELECT t.id, t.x FROM t) SELECT q.id, q.x FROM q", None),
        ])
    elif kind == 1:
        left = f"WITH q AS (SELECT {k} AS x) SELECT d.x FROM (WITH q AS (SELECT {k + 1} AS x) SELECT q.x FROM q) AS d"
        right, label = _pick(f, [
            (f"SELECT d.x FROM (SELECT q.x FROM (SELECT {k + 1} AS x) AS q) AS d", True),
            (f"SELECT d.x FROM (SELECT q.x FROM (SELECT {k} AS x) AS q) AS d", None),
            (f"WITH q AS (SELECT {k} AS x) SELECT d.x FROM (SELECT q.x FROM q) AS d", None),
        ])
    elif kind == 2:
        # a CTE named like a base table hides it
        left = f"WITH t AS (SELECT u.k AS id, u.k AS x, u.k AS y, u.v AS s FROM u) SELECT t.id, t.x FROM t WHERE {p}"
        right, label = _pick(f, [
            (f"SELECT d.id, d.x FROM (SELECT u.k AS id, u.k AS x, u.k AS y, u.v AS s FROM u) AS d WHERE {p.replace('t.', 'd.')}", True),
            (f"SELECT t.id, t.x FROM t WHERE {p}", None),
        ])
    else:
        left = f"WITH a AS (SELECT t.x AS c FROM t), b AS (SELECT a.c FROM a WHERE a.c > {k}) SELECT b.c FROM b"
        right, label = _pick(f, [
            (f"SELECT t.x AS c FROM t WHERE t.x > {k}", True),
            (f"WITH b AS (SELECT t.x AS c FROM t), a AS (SELECT b.c FROM b WHERE b.c > {k}) SELECT a.c FROM a", True),
            (f"WITH a AS (SELECT t.x AS c FROM t WHERE t.x > {k}), b AS (SELECT a.c FROM a) SELECT b.c FROM b", True),
            (f"WITH a AS (SELECT t.x AS c FROM t), b AS (SELECT a.c FROM a WHERE a.c > {k}) SELECT a.c FROM a", None),
        ])
    return "cte_scope", left, right, label


def tpl_qualify(f, ctx):
    part = f.choice(["t.s", "t.x"])
    n = f.choice([1, 1, 2])
    left = f"SELECT t.id, t.x FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY t.id) <= {n}"
    right, label = _pick(f, [
        (f"SELECT t.id, t.x FROM t WHERE t.id IN (SELECT MIN(b.id) FROM t AS b GROUP BY b.{part[2:]})", True if n == 1 else None),
        (f"SELECT t.id, t.x FROM t WHERE NOT EXISTS (SELECT 1 FROM t AS b WHERE b.{part[2:]} = {part} AND b.id < t.id)", None),
        (f"SELECT t.id, t.x FROM t WHERE (SELECT COUNT(*) FROM t AS b WHERE b.{part[2:]} IS NOT DISTINCT FROM {part} AND b.id < t.id) < {n}", True),
        (f"SELECT d.id, d.x FROM (SELECT t.id, t.x, ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY t.id) AS rn FROM t) AS d WHERE d.rn <= {n}", None),
        (f"SELECT t.id, t.x FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY t.id) = {n}", True if n == 1 else None),
    ])
    return "qualify", left, right, label


def tpl_limit(f, ctx):
    n = f.rng.randint(0, 3)
    p = f.pred(T, 1)
    left = f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} ORDER BY t.id LIMIT {n}"
    right, label = _pick(f, [
        (f"SELECT * FROM (SELECT t.id, t.x, t.y, t.s FROM t ORDER BY t.id LIMIT {n}) AS d WHERE {p.replace('t.', 'd.')}", None),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} ORDER BY t.id LIMIT {n + 1}", None),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} ORDER BY t.id DESC LIMIT {n}", None if n else True),
        (f"SELECT * FROM (SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} ORDER BY t.id LIMIT {n}) AS d", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p} AND t.id IN (SELECT b.id FROM t AS b WHERE {p.replace('t.', 'b.')} ORDER BY b.id LIMIT {n})", True),
        (f"SELECT t.id, t.x, t.y, t.s FROM t WHERE {p}" + ("" if n else " AND FALSE"), None if n else True),
    ])
    if f.rng.random() < 0.3:
        left = f"SELECT t.x AS c FROM t UNION ALL SELECT u.k AS c FROM u LIMIT 0"
        right, label = _pick(f, [("SELECT t.x AS c FROM t WHERE FALSE", True), ("SELECT t.x AS c FROM t", None), ("SELECT t.x AS c FROM t UNION ALL (SELECT u.k AS c FROM u LIMIT 0)", None)])
    return "limit", left, right, label


def tpl_foreign_key(f, ctx):
    if not ctx.get("fk"):
        return tpl_join_type(f, ctx)
    sel = f.choice(["t.id, t.y", "t.id, t.x, t.s"])
    left = f"SELECT {sel} FROM t JOIN u ON t.y = u.k"
    right, label = _pick(f, [
        (f"SELECT {sel} FROM t WHERE t.y IS NOT NULL", True),
        (f"SELECT {sel} FROM t", None),
        (f"SELECT {sel} FROM t LEFT JOIN u ON t.y = u.k", None),
        (f"SELECT {sel} FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k = t.y)", True),
        (f"SELECT {sel} FROM t WHERE t.y IN (SELECT u.k FROM u)", True),
    ])
    if f.rng.random() < 0.4:
        left = f"SELECT {sel} FROM t LEFT JOIN u ON t.y = u.k"
        right, label = _pick(f, [(f"SELECT {sel} FROM t", True), (f"SELECT {sel} FROM t WHERE t.y IS NOT NULL", None)])
    return "foreign_key", left, right, label


def tpl_string(f, ctx):
    c = f.choice(["t.s"])
    pairs = [
        (f"{c} LIKE '%'", f"{c} IS NOT NULL", True),
        (f"{c} LIKE 'a%'", f"{c} = 'a'", None),
        (f"{c} LIKE 'a'", f"{c} = 'a'", True),
        (f"{c} LIKE '_'", f"{c} IN ('a', 'b')", None),
        (f"{c} = ''", f"{c} IS NULL", None),
        (f"{c} < 'b'", f"{c} IN ('', 'a')", None),
        (f"COALESCE({c}, '') = ''", f"({c} = '' OR {c} IS NULL)", True),
    ]
    a, b, label = f.choice(pairs)
    return "string", f"SELECT t.id FROM t WHERE {a}", f"SELECT t.id FROM t WHERE {b}", label


def tpl_global_aggregate(f, ctx):
    agg = f.choice(["COUNT(*)", "MAX(t.x)", "SUM(t.y)", "COUNT(DISTINCT t.s)"])
    p = f.pred(T, 1)
    kind = f.rng.randrange(4)
    if kind == 0:
        left = f"SELECT 1 AS c FROM (SELECT {agg} AS m FROM t WHERE {p}) AS d"
        right, label = _pick(f, [("SELECT 1 AS c", True), (f"SELECT 1 AS c FROM t WHERE {p}", None), (f"SELECT 1 AS c FROM (SELECT {agg} AS m FROM t) AS d", True)])
    elif kind == 1:
        c = f.lit()
        left = f"SELECT d.c FROM (SELECT {agg} AS m, {c} AS c FROM t WHERE {p}) AS d"
        right, label = _pick(f, [(f"SELECT d.c FROM (SELECT {c} AS c FROM t WHERE {p}) AS d", None), (f"SELECT {c} AS c", True)])
    elif kind == 2:
        v, q = f.lit(), f.pred(U, 1)
        left = f"SELECT t.id, {v} IN (SELECT COUNT(*) FROM u WHERE {q}) AS e FROM t"
        right, label = _pick(f, [("SELECT t.id, FALSE AS e FROM t", None), (f"SELECT t.id, {v} = (SELECT COUNT(*) FROM u WHERE {q}) AS e FROM t", True)])
    else:
        q = f.pred(U, 1)
        left = f"SELECT t.id FROM t WHERE EXISTS (SELECT {agg.replace('t.', 'u.').replace('u.x', 'u.k').replace('u.y', 'u.k').replace('u.s', 'u.v')} FROM u WHERE {q})"
        right, label = _pick(f, [("SELECT t.id FROM t", True), (f"SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE {q})", None),
                                 (f"SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u WHERE {q} HAVING COUNT(*) > 0)", None)])
        if "HAVING" in right:
            right, label = f"SELECT t.id FROM t WHERE EXISTS (SELECT 1 FROM u WHERE {q})", True
            left = f"SELECT t.id FROM t WHERE EXISTS (SELECT COUNT(*) FROM u WHERE {q} HAVING COUNT(*) > 0)"
    return "global_aggregate", left, right, label


def tpl_aggregate_arithmetic(f, ctx):
    group = f.choice(["", "t.s"])
    gsel, gby = (f"{group}, ", f" GROUP BY {group}") if group else ("", "")
    n = f.choice([2, -1, 3])
    pairs = [
        ("SUM(t.x + t.y)", "SUM(t.x) + SUM(t.y)", None),
        (f"SUM(t.x * {n})", f"{n} * SUM(t.x)", True),
        ("SUM(t.x + 1)", "SUM(t.x) + COUNT(t.x)", True),
        ("SUM(t.x + 1)", "SUM(t.x) + COUNT(*)", None),
        ("COUNT(t.x + t.y)", "COUNT(t.x)", None),
        ("MAX(t.x + 1)", "MAX(t.x) + 1", True),
        ("MIN(-t.x)", "-MAX(t.x)", True),
        ("SUM(COALESCE(t.x, 0))", "COALESCE(SUM(t.x), 0)", None if not group else True),
        ("SUM(CASE WHEN t.y > 0 THEN t.x ELSE 0 END)", "SUM(CASE WHEN t.y > 0 THEN t.x END)", None),
        ("COUNT(DISTINCT t.x + 0)", "COUNT(DISTINCT t.x)", True),
    ]
    a, b, label = f.choice(pairs)
    where = f" WHERE {f.pred(T, 1)}" if f.rng.random() < 0.4 else ""
    return "aggregate_arithmetic", f"SELECT {gsel}{a} AS n FROM t{where}{gby}", f"SELECT {gsel}{b} AS n FROM t{where}{gby}", label


def tpl_scope_capture(f, ctx):
    kind = f.rng.randrange(3)
    if kind == 0:
        # an unqualified name binds to the nearest scope that has it
        left = "SELECT t.id, t.x FROM t WHERE EXISTS (SELECT 1 FROM t AS b WHERE id = t.x)"
        right, label = _pick(f, [("SELECT t.id, t.x FROM t WHERE EXISTS (SELECT 1 FROM t AS b WHERE b.id = t.x)", True),
                                 ("SELECT t.id, t.x FROM t WHERE EXISTS (SELECT 1 FROM t AS b WHERE t.id = t.x)", None),
                                 ("SELECT t.id, t.x FROM t WHERE t.x IN (SELECT b.id FROM t AS b)", True)])
    elif kind == 1:
        # derived-table aliases that swap column names
        w = f"d.y {f.choice(['>', '=', '<>'])} {f.lit()}"
        left = f"SELECT d.x FROM (SELECT t.y AS x, t.x AS y FROM t) AS d WHERE {w}"
        right, label = _pick(f, [(f"SELECT t.y AS x FROM t WHERE {w.replace('d.y', 't.x')}", True), (f"SELECT t.y AS x FROM t WHERE {w.replace('d.y', 't.y')}", None),
                                 (f"SELECT t.x FROM t WHERE {w.replace('d.y', 't.x')}", None)])
    else:
        c = f.choice(["x", "y"])
        left = f"SELECT t.id, (SELECT MAX(k) FROM u WHERE k > {c}) AS m FROM t"
        right, label = _pick(f, [(f"SELECT t.id, (SELECT MAX(u.k) FROM u WHERE u.k > t.{c}) AS m FROM t", True),
                                 (f"SELECT t.id, (SELECT MAX(u.k) FROM u) AS m FROM t", None)])
    return "scope_capture", left, right, label


TEMPLATES = [
    tpl_predicate_where, tpl_predicate_value, tpl_null_arithmetic, tpl_count_variants, tpl_distinct, tpl_group_key,
    tpl_join_type, tpl_on_where, tpl_join_commute, tpl_self_join, tpl_semijoin, tpl_antijoin, tpl_membership_value,
    tpl_setop, tpl_setop_aggregate, tpl_grouping_sets, tpl_having, tpl_derived_pushdown, tpl_scalar_subquery,
    tpl_cte_scope, tpl_qualify, tpl_limit, tpl_foreign_key, tpl_string, tpl_global_aggregate, tpl_aggregate_arithmetic,
    tpl_scope_capture,
]

# Sol's S015 families: a fixed query each, sound mutations on one pass and deliberate changes on the next.
SOL_FAMILIES = ["aggregate", "distinct", "grouping", "grouping_sets", "rollup", "inner_join", "outer_join", "derived_subquery",
                "correlated_subquery", "membership", "quantified_membership", "set_operations", "cte_scope", "float_coercion",
                "case_coalesce", "window_qualify", "limit", "constraint_fk", "constraint_key", "scalar_subquery", "right_join",
                "full_join", "join_true", "join_false"]


def sol_case(rng: random.Random, index: int) -> dict:
    """Sol's S015 family ``index % 24`` (the pass, ``index // 24``, picks sound or deliberate changes)."""

    family = SOL_FAMILIES[index % len(SOL_FAMILIES)]
    schema, tables, constraints = make_fixture(rng, empty=index % 11 == 0)
    k = rng.randint(-1, 1)
    pass_ = index // len(SOL_FAMILIES)
    queries = {
        "aggregate": "SELECT COUNT(*) AS n, SUM(x) AS s, COUNT(DISTINCT s) AS d FROM t",
        "distinct": "SELECT DISTINCT x, s FROM t",
        "grouping": "SELECT x, COUNT(*) AS n, SUM(y) AS s FROM t GROUP BY x",
        "grouping_sets": "SELECT x, s, COUNT(*) AS n FROM t GROUP BY GROUPING SETS ((x,s),(x),())",
        "rollup": "SELECT x,s,COUNT(*) AS n FROM t GROUP BY ROLLUP(x,s)",
        "inner_join": "SELECT t.x, u.k, t.s FROM t JOIN u ON t.x=u.k",
        "outer_join": "SELECT t.x, u.k, t.s FROM t LEFT JOIN u ON t.x=u.k",
        "derived_subquery": "SELECT d.x,d.s FROM (SELECT x,s FROM t WHERE y IS NOT NULL) d",
        "correlated_subquery": "SELECT x,s FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.k=t.x)",
        "membership": "SELECT x, x IN (SELECT k FROM u) AS member, x NOT IN (SELECT k FROM u) AS absent FROM t",
        "quantified_membership": "SELECT x, x = ANY (SELECT k FROM u) AS member, x <> ALL (SELECT k FROM u) AS absent FROM t",
        "set_operations": rng.choice([
            "SELECT x AS v FROM t UNION ALL SELECT k AS v FROM u",
            "SELECT x AS v FROM t UNION DISTINCT SELECT k AS v FROM u",
            "SELECT x AS v FROM t INTERSECT DISTINCT SELECT k AS v FROM u",
            "SELECT x AS v FROM t EXCEPT DISTINCT SELECT k AS v FROM u"]),
        "cte_scope": f"WITH q AS (SELECT {k} AS x) SELECT d.x FROM (WITH q AS (SELECT {k + 1} AS x) SELECT x FROM q) d",
        "float_coercion": "SELECT x * 1e0 AS v FROM t",
        "case_coalesce": rng.choice([
            "SELECT COALESCE(x,0) AS v, CASE WHEN s IS NULL THEN '' ELSE s END AS s FROM t",
            "SELECT COALESCE(x, CAST(1 AS FLOAT64)) AS v FROM t WHERE x IS NOT NULL",
            "SELECT CASE WHEN x IS NOT NULL THEN x ELSE CAST(1 AS FLOAT64) END AS v FROM t WHERE x IS NOT NULL"]),
        "window_qualify": "SELECT id,x,ROW_NUMBER() OVER (PARTITION BY s ORDER BY id) AS rn FROM t QUALIFY rn=1",
        "limit": "SELECT id,x FROM t ORDER BY id LIMIT 1",
        "constraint_fk": "SELECT t.id,t.y FROM t WHERE t.y IS NULL OR EXISTS (SELECT 1 FROM u WHERE u.k=t.y)",
        "constraint_key": "SELECT id FROM t GROUP BY id",
        "scalar_subquery": "SELECT id,(SELECT COUNT(*) FROM u WHERE u.k=t.x) AS n FROM t",
        "right_join": "SELECT t.x,u.k,t.s FROM t RIGHT JOIN u ON t.x=u.k",
        "full_join": "SELECT t.x,u.k,t.s FROM t FULL JOIN u ON t.x=u.k",
        "join_true": f"SELECT t.x,u.k FROM t {['LEFT', 'RIGHT', 'FULL'][pass_ % 3]} JOIN u ON TRUE",
        "join_false": f"SELECT t.x,u.k FROM t {['LEFT', 'RIGHT', 'FULL'][pass_ % 3]} JOIN u ON FALSE",
    }
    left = queries[family]
    dialect = "duckdb" if family == "quantified_membership" else "bigquery"
    unequal = pass_ % 2 == 1
    transformation = rng.choice(["identity", "wrapper", "duplicate_filter"])
    known = True
    if family == "constraint_fk":
        tables["u"] = [[0, None], [1, "a"]]
        constraints["u"] = {"not_null": ["k"], "keys": [["k"]]}
        constraints["t"]["foreign_keys"] = [[["y"], "u", ["k"]]]
        for row in tables["t"]:
            if row[2] is not None:
                row[2] = rng.choice([0, 1])
    if family == "float_coercion":
        tables["t"] = [[0, 9007199254740993, None, None], [1, 9007199254740993, 1, "a"], [2, None, 0, "b"]]
        constraints["t"]["not_null"] = ["id"]
    if family == "case_coalesce" and "FLOAT64" in left:
        tables["t"] = [[0, 9007199254740993, None, None], [1, None, 0, "a"]]
        constraints["t"]["not_null"] = ["id"]
    if unequal and family in ("aggregate", "grouping", "cte_scope", "float_coercion", "case_coalesce", "limit"):
        known = None
        if family == "aggregate":
            left, right, transformation = "SELECT d.c FROM (SELECT COUNT(*) AS n,7 AS c FROM t) d", "SELECT 7 AS c FROM t", "remove_last_aggregate"
            if len(tables["t"]) < 2:
                tables["t"] = [[0, 1, 0, "a"], [1, 1, 1, "b"]]
        elif family == "grouping":
            left, right, transformation = "SELECT x, SUM(y) AS n FROM t GROUP BY x,s", "SELECT x, SUM(y) AS n FROM t GROUP BY x", "remove_group_key"
            tables["t"] = [[0, 1, 1, "a"], [1, 1, 2, "b"]]
        elif family == "cte_scope":
            right, transformation = f"WITH q AS (SELECT {k} AS x) SELECT d.x FROM (SELECT x FROM q) d", "rename_shadowed_scope"
        elif family == "float_coercion":
            right, transformation = "SELECT x AS v FROM t", "remove_float_coercion"
        elif family == "case_coalesce" and "FLOAT64" in left:
            right, transformation = "SELECT x AS v FROM t WHERE x IS NOT NULL", "remove_case_coalesce_float_widening"
        elif family == "case_coalesce":
            right, transformation = "SELECT x AS v, s FROM t", "remove_null_handling"
            constraints["t"]["not_null"] = ["id"]
            tables["t"] = [[0, None, None, None], [1, 1, 0, "a"]]
        else:
            left = "SELECT x AS v FROM t UNION ALL SELECT k AS v FROM u LIMIT 0"
            right, transformation = "SELECT x AS v FROM t UNION ALL SELECT k AS v FROM u", "remove_set_limit"
            tables["t"] = [[0, 1, 0, "a"], [1, 2, 0, "b"]]
    else:
        if transformation == "identity":
            right = left
        elif transformation == "wrapper":
            right = f"SELECT * FROM ({left}) AS wrapped"
        else:
            # p AND p on a fresh outer filter is valid even with NULL: AND is idempotent in three-valued logic.
            left = f"SELECT * FROM ({left}) AS wrapped WHERE 1=1"
            right = left + " AND 1=1"
        if family == "constraint_fk":
            right, transformation = "SELECT t.id,t.y FROM t", "drop_fk_witness_filter"
        elif family == "constraint_key":
            right, transformation = "SELECT id FROM t", "drop_keyed_grouping"
    return {"family": f"sol:{family}", "left": left, "right": right, "schema": schema, "tables": tables, "constraints": constraints, "dialect": dialect, "mutation": {"name": transformation, "known_equivalent": known}}


def template_case(rng: random.Random, template=None) -> dict:
    keyed = rng.random() < 0.4
    fk = keyed and rng.random() < 0.5
    schema, tables, constraints = make_fixture(rng, empty=rng.random() < 0.05, keyed_u=keyed, fk=fk)
    f = Fragments(rng)
    template = template or rng.choice(TEMPLATES)
    family, left, right, label = template(f, {"fk": fk, "keyed_u": keyed})
    if rng.random() < 0.5:
        left, right = right, left
    return {"family": family, "left": left, "right": right, "schema": schema, "tables": tables, "constraints": constraints, "dialect": "bigquery", "mutation": {"name": template.__name__[4:], "known_equivalent": label}}


def mutant_case(rng: random.Random) -> dict | None:
    from kumosql.query_mutants import mutate

    case = template_case(rng)
    try:
        mutants = mutate(case["left"])
    except Exception:
        return None
    if not mutants:
        return None
    mutant = rng.choice(mutants)
    case.update(family=f"mutant:{case['family']}", right=mutant.sql, mutation={"name": mutant.operator, "known_equivalent": None})
    return case


def generated_cases(seed: int, count: int, generators=("template", "sol", "mutant"), randoms: int = 2):
    """``count`` cases; the seed decides the SQL, schema, constraints and every database."""

    rng = random.Random(seed)
    weights = {"template": 6, "sol": 2, "mutant": 2}
    kinds = [g for g in generators]
    sol_index = 0
    index = 0
    while index < count:
        kind = rng.choices(kinds, [weights[k] for k in kinds])[0]
        if kind == "sol":
            case = sol_case(rng, sol_index)
            sol_index += 1
        elif kind == "mutant":
            case = mutant_case(rng)
            if case is None:
                continue
        else:
            case = template_case(rng)
        case["id"] = f"s{seed}-{index:05d}"
        case["fixture_variants"] = additional_fixtures(case, rng, randoms)
        index += 1
        yield case


def historical_cases() -> list[dict]:
    """Published false proofs (Sol's S009 audit) with their witness databases; none may be proved again."""

    cases = []
    for item in json.loads(HISTORICAL_PATH.read_text(encoding="utf-8")):
        cases.append({
            "id": item["id"], "family": f"historical:{item['family']}", "left": item["left"], "right": item["right"],
            "dialect": "bigquery", "pass_schema": False, "schema": {"t": [["x", "INT64"], ["y", "INT64"]], "u": [["k", "INT64"]]},
            "tables": item["tables"], "constraints": {}, "mutation": {"name": item["family"], "known_equivalent": None},
        })
    return cases


# ---------------------------------------------------------------------------
# Reduction
# ---------------------------------------------------------------------------


def _sql_edits(sql: str, dialect: str):
    """Smaller variants of ``sql``, one edit each (largest removals first)."""

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return
    count = len(list(tree.walk()))
    for site in range(count):
        node = list(tree.walk())[site]
        variants = []
        if isinstance(node, (exp.And, exp.Or, exp.Union, exp.Intersect, exp.Except)):
            variants += [lambda n: n.this, lambda n: n.expression]
        if isinstance(node, (exp.Not, exp.Paren, exp.Neg)):
            variants.append(lambda n: n.this)
        if isinstance(node, (exp.Coalesce, exp.If, exp.Case)):
            variants.append(lambda n: n.this if not isinstance(n, exp.Case) else n.args["ifs"][0].args["true"])
            if isinstance(node, exp.Coalesce):
                variants.append(lambda n: n.expressions[0])
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul)):
            variants += [lambda n: n.this, lambda n: n.expression]
        if isinstance(node, exp.Literal) and not node.is_string and node.this not in ("0", "1"):
            variants.append(lambda n: exp.Literal.number(0))
        if isinstance(node, (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Is, exp.In, exp.Exists, exp.Between, exp.Like, exp.NullSafeEQ, exp.NullSafeNEQ)) and isinstance(node.parent, (exp.Where, exp.Having, exp.Join, exp.And, exp.Or, exp.Not, exp.Paren, exp.Qualify)):
            variants += [lambda n: exp.true(), lambda n: exp.false()]
        for make in variants:
            copy_ = tree.copy()
            target = list(copy_.walk())[site]
            try:
                replacement = make(target)
            except (IndexError, KeyError, AttributeError, TypeError):
                continue
            if replacement is None or not isinstance(replacement, exp.Expression):
                continue
            replacement = replacement.copy()
            if target is copy_:
                yield replacement.sql(dialect=dialect)
            else:
                target.replace(replacement)
                yield copy_.sql(dialect=dialect)
        if isinstance(node, exp.Select):
            for arg in ("where", "having", "qualify", "order", "limit", "distinct", "group", "with_"):
                if node.args.get(arg) is not None:
                    copy_ = tree.copy()
                    list(copy_.walk())[site].set(arg, None)
                    yield copy_.sql(dialect=dialect)
            for index in range(len(node.expressions)):
                if len(node.expressions) > 1:
                    copy_ = tree.copy()
                    target = list(copy_.walk())[site]
                    target.set("expressions", [e for i, e in enumerate(target.expressions) if i != index])
                    yield copy_.sql(dialect=dialect)
            for index in range(len(node.args.get("joins") or [])):
                copy_ = tree.copy()
                target = list(copy_.walk())[site]
                target.set("joins", [j for i, j in enumerate(target.args["joins"]) if i != index] or None)
                yield copy_.sql(dialect=dialect)
        if isinstance(node, exp.With):
            for index in range(len(node.expressions)):
                copy_ = tree.copy()
                target = list(copy_.walk())[site]
                rest = [c for i, c in enumerate(target.expressions) if i != index]
                if rest:
                    target.set("expressions", rest)
                else:
                    target.pop()
                yield copy_.sql(dialect=dialect)


def _paired_edits(left: str, right: str, dialect: str):
    """The same removal (a conjunct, a set-operation branch, WHERE, HAVING, ...) made on both queries."""

    from kumosql.minimize import _edits

    try:
        lt, rt = sqlglot.parse_one(left, read=dialect), sqlglot.parse_one(right, read=dialect)
    except sqlglot.errors.SqlglotError:
        return
    partners: dict = {}
    for key, edited in _edits(rt):
        partners.setdefault(key, edited)
    for key, edited in _edits(lt):
        if key in partners:
            yield edited.sql(dialect=dialect), partners[key].sql(dialect=dialect)


def minimize_false_proof(case: dict, observed: dict, evaluate, options: dict) -> dict:
    """Shrink both SQL sides, then the witness database, while the pair stays a false proof.

    ``evaluate(case, options)`` is the pool's evaluation. SQL edits need the prover again; row deletions only the
    oracle (the prover never sees data).
    """

    end = time.monotonic() + options["minimize_seconds"]
    checks = 0
    current = copy.deepcopy(case)
    seen = {(case["left"], case["right"])}
    dialect = case.get("dialect", "bigquery")

    def still(candidate, oracle_only=False):
        nonlocal checks
        checks += 1
        outcome = evaluate(candidate, dict(options, oracle_only=oracle_only))
        if outcome.get("execution") != "ok" or outcome.get("equal_bags"):
            return None
        if not oracle_only and outcome.get("discrepancy") != "false_proof":
            return None
        return outcome

    last = observed
    progress = True
    while progress and checks < options["minimize_checks"] and time.monotonic() < end:
        progress = False
        # the same edit on both sides first (a proof rarely survives an edit to one side only)
        for pair in _paired_edits(current["left"], current["right"], dialect):
            if checks >= options["minimize_checks"] or time.monotonic() >= end:
                break
            if pair in seen or len(pair[0]) + len(pair[1]) >= len(current["left"]) + len(current["right"]):
                continue
            seen.add(pair)
            candidate = dict(current, left=pair[0], right=pair[1])
            outcome = still(candidate)
            if outcome:
                current, last, progress = candidate, outcome, True
                break
        if progress:
            continue
        for side in ("left", "right"):
            for sql in _sql_edits(current[side], dialect):
                if checks >= options["minimize_checks"] or time.monotonic() >= end:
                    break
                if len(sql) >= len(current[side]):
                    continue
                pair = (sql, current["right"]) if side == "left" else (current["left"], sql)
                if pair in seen:
                    continue
                seen.add(pair)
                candidate = dict(current, **{side: sql})
                outcome = still(candidate)
                if outcome:
                    current, last, progress = candidate, outcome, True
                    break
    # the witness database alone, then fewer rows
    witness = last.get("counterexample_dataset")
    datasets = {"primary": current["tables"], **{v["name"]: v["tables"] for v in current.get("fixture_variants", [])}}
    if witness in datasets:
        current = dict(current, tables=copy.deepcopy(datasets[witness]), fixture_variants=[])
        for table in list(current["tables"]):
            index = 0
            while index < len(current["tables"][table]) and checks < options["minimize_checks"] * 2 and time.monotonic() < end + 30:
                tables = copy.deepcopy(current["tables"])
                del tables[table][index]
                candidate = dict(current, tables=tables)
                if still(candidate, oracle_only=True):
                    current = candidate
                else:
                    index += 1
    return {"left": current["left"], "right": current["right"], "tables": current["tables"], "checks": checks,
            "original_sql_chars": len(case["left"]) + len(case["right"]), "reduced_sql_chars": len(current["left"]) + len(current["right"])}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def load_known() -> list[dict]:
    if not KNOWN_PATH.exists():
        return []
    return json.loads(KNOWN_PATH.read_text(encoding="utf-8"))


def known_entry(record: dict, known: list[dict]):
    pair = {record["case"]["left"], record["case"]["right"]}
    reduced = record.get("minimized") or {}
    text = record["case"]["left"] + "\n" + record["case"]["right"]
    for entry in known:
        sides = {entry["left"], entry["right"]}
        if sides == pair or (reduced and sides == {reduced.get("left"), reduced.get("right")}):
            return entry
        # ``match``: fragments that, all present, mark another instance of the same root cause
        if entry.get("match") and record["case"]["family"] in entry.get("families", [record["case"]["family"]]) and all(m in text for m in entry["match"]):
            return entry
    return None


def _compact(record: dict) -> dict:
    o = record["observation"]
    return {"id": record["case"]["id"], "family": record["case"]["family"], "mutation": record["case"]["mutation"]["name"],
            "label": record["case"]["mutation"].get("known_equivalent"), "execution": o.get("execution"),
            "prover": o.get("prover", {}).get("status"), "equal_bags": o.get("equal_bags"), "reason": o.get("reason")}


def run(seed: int = 1, count: int = 100, *, jobs: int = 2, options: dict | None = None, include_historical: bool = True,
        minimize: bool = True, run_timeout_seconds: float = 3600.0, generators=("template", "sol", "mutant"), randoms: int = 2,
        cases=None) -> dict:
    options = options or default_options()
    deadline = time.monotonic() + run_timeout_seconds
    planned = list(historical_cases()) if include_historical else []
    planned += list(cases) if cases is not None else list(generated_cases(seed, count, generators, randoms))
    known = load_known()
    records = []
    with Pool(jobs) as pool:
        for case, observed in zip(planned, pool.map(planned, options, deadline)):
            records.append({"case": case, "observation": observed})
        if minimize:
            def evaluate(candidate, opts):
                return pool.evaluate(candidate, opts)

            interesting = [r for r in records if r["observation"].get("discrepancy") == "false_proof"]

            def reduce(record):
                if time.monotonic() >= deadline:
                    return None
                return minimize_false_proof(record["case"], record["observation"], evaluate, options)

            for record, reduced in zip(interesting, pool.executor.map(reduce, interesting)):
                if reduced:
                    record["minimized"] = reduced
    for record in records:
        if record["observation"].get("discrepancy") == "false_proof":
            entry = known_entry(record, known)
            record["known"] = entry.get("cause") if entry else None
    summary = {
        "planned": len(planned),
        "completed": sum(r["observation"].get("reason") != "run deadline exhausted" for r in records),
        "execution": dict(sorted(Counter(r["observation"].get("execution") for r in records).items())),
        "prover_status": dict(sorted(Counter(r["observation"].get("prover", {}).get("status", "not_run") for r in records).items())),
        "discrepancies": dict(sorted(Counter(r["observation"]["discrepancy"] for r in records if r["observation"].get("discrepancy")).items())),
        "new_false_proofs": sum(1 for r in records if r["observation"].get("discrepancy") == "false_proof" and not r.get("known")),
        "families": dict(sorted(Counter(r["case"]["family"].split(":")[0] if r["case"]["family"].startswith("sol") else r["case"]["family"] for r in records).items())),
        "proved_by_family": dict(sorted(Counter(r["case"]["family"] for r in records if r["observation"].get("prover", {}).get("status") == "proven_equivalent").items())),
    }
    groups: dict = {}
    for record in records:
        if record["observation"].get("discrepancy") != "false_proof":
            continue
        key = (record["case"]["family"], record["case"]["mutation"]["name"])
        reduced = record.get("minimized") or {"left": record["case"]["left"], "right": record["case"]["right"], "tables": record["case"]["tables"]}
        group = groups.setdefault(key, {"family": key[0], "mutation": key[1], "count": 0, "known": record.get("known"), "ids": [], "example": reduced})
        group["count"] += 1
        group["ids"].append(record["case"]["id"])
        group["known"] = group["known"] if group["known"] == record.get("known") else None
        if len(reduced["left"]) + len(reduced["right"]) < len(group["example"]["left"]) + len(group["example"]["right"]):
            group["example"] = reduced
    summary["false_proof_groups"] = sorted(groups.values(), key=lambda g: (g["known"] is not None, -g["count"]))
    keep = [r for r in records if r["observation"].get("discrepancy") or r["observation"].get("execution") in ("worker_error", "timeout")]
    return {"format_version": 2, "seed": seed, "count": count, "generators": list(generators), "limits": dict(options, run_timeout_seconds=run_timeout_seconds),
            "oracle": "exact finite row bags; differences confirmed with DuckDB's optimizer off", "summary": summary,
            "findings": keep, "records": [_compact(r) for r in records]}


def _show(report: dict) -> None:
    for group in report["summary"].get("false_proof_groups", []):
        print(f"## {group['count']} false proof(s) from {group['family']} / {group['mutation']}" + (f" (known: {group['known']})" if group["known"] else " (NEW)"))
        print("   left: ", group["example"]["left"])
        print("   right:", group["example"]["right"])
        print("   rows: ", _json(group["example"]["tables"]))
    for record in report["findings"]:
        if record["observation"].get("discrepancy") == "false_proof":
            continue
        o = record["observation"]
        kind = o.get("discrepancy") or o.get("execution")
        reduced = record.get("minimized") or {}
        print(f"== {record['case']['id']} {record['case']['family']} {kind}" + (f" (known: {record['known']})" if record.get("known") else ""))
        print("   left: ", reduced.get("left", record["case"]["left"]))
        print("   right:", reduced.get("right", record["case"]["right"]))
        if reduced:
            print("   rows: ", _json(reduced["tables"]))
        elif o.get("counterexample_dataset"):
            print("   witness:", o["counterexample_dataset"], "left", o.get("left_rows"), "right", o.get("right_rows"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--count", type=int, default=100, help="generated pairs (the published historical pairs run first)")
    parser.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--generators", default="template,sol,mutant")
    parser.add_argument("--random-databases", type=int, default=2, help="random databases per pair besides the NULL-heavy, duplicate-heavy and empty ones")
    parser.add_argument("--query-timeout", type=float, default=20.0, metavar="SECONDS")
    parser.add_argument("--solver-timeout-ms", type=int, default=2000)
    parser.add_argument("--run-timeout", type=float, default=3600.0, metavar="SECONDS")
    parser.add_argument("--memory-mb", type=int, default=256)
    parser.add_argument("--max-result-rows", type=int, default=1000)
    parser.add_argument("--minimize-checks", type=int, default=60)
    parser.add_argument("--minimize-seconds", type=float, default=60.0)
    parser.add_argument("--no-minimize", action="store_true")
    parser.add_argument("--no-historical", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replay", type=Path, help="re-evaluate the findings of a saved report")
    parser.add_argument("--show", action="store_true", help="print every finding")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        return _worker_loop()
    options = {"query_timeout_seconds": args.query_timeout, "solver_timeout_ms": args.solver_timeout_ms, "memory_mb": args.memory_mb,
               "max_result_rows": args.max_result_rows, "minimize_checks": args.minimize_checks, "minimize_seconds": args.minimize_seconds}
    cases = None
    if args.replay:
        saved = json.loads(args.replay.read_text(encoding="utf-8"))
        cases = [r["case"] for r in saved.get("findings", saved.get("records", [])) if "case" in r]
    report = run(args.seed, args.count, jobs=args.jobs, options=options, include_historical=not args.no_historical and not args.replay,
                 minimize=not args.no_minimize, run_timeout_seconds=args.run_timeout, generators=tuple(args.generators.split(",")),
                 randoms=args.random_databases, cases=cases)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    if args.show:
        _show(report)
    print(_json({k: v for k, v in report["summary"].items() if k not in ("families", "proved_by_family", "false_proof_groups")}))
    return 1 if report["summary"]["new_false_proofs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
