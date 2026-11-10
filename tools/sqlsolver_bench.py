"""Run SQLSolver's published equivalence benchmarks through KumoSQL's provers.

Each benchmark file holds pairs of queries on consecutive lines (SQLSolver's
authors state each pair is equivalent). For every pair this reports whether the
prover proved it, and re-checks every proof by running both queries on random
DuckDB databases that respect the schema's NOT NULL and key constraints.

    python tools/sqlsolver_bench.py            # all suites
    python tools/sqlsolver_bench.py calcite    # one suite

The data in tests/fixtures/sqlsolver comes from https://github.com/SJTU-IPADS/SQLSolver
(Apache-2.0, see LICENSE there).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import json
import hashlib
import os
import platform
import tempfile
from pathlib import Path
import random
import re
import logging
import sys
import time

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sqlsolver"
SUITES = {
    "calcite": ("calcite_pairs.txt", "calcite.schema.sql"),
    "spark": ("spark_pairs.txt", "calcite.schema.sql"),
    "tpch": ("tpch_pairs.txt", "tpch.schema.sql"),
    "tpcc": ("tpcc_pairs.txt", "tpcc.schema.sql"),
}


@dataclass
class Column:
    name: str
    type: str
    not_null: bool = False


@dataclass
class Table:
    name: str
    columns: list[Column]
    primary_key: tuple[str, ...] = ()
    unique: list[tuple[str, ...]] = field(default_factory=list)
    foreign: list[tuple[str, str, str]] = field(default_factory=list)  # (column, parent table, parent column)


def load_schema(path: Path) -> dict[str, Table]:
    tables = {}
    for statement in sqlglot.parse(path.read_text(encoding="utf-8"), read="mysql"):
        if not isinstance(statement, exp.Create):
            continue
        schema = statement.this
        name = schema.this.name.lower()
        table = Table(name, [])
        for item in schema.expressions:
            if isinstance(item, exp.ColumnDef):
                kinds = [k.args.get("kind") for k in item.args.get("constraints") or []]
                constraint_sql = " ".join(k.sql().upper() for k in item.args.get("constraints") or [])
                column = Column(item.name.lower(), item.args["kind"].sql(dialect="mysql").upper())
                column.not_null = "NOT NULL" in constraint_sql or "PRIMARY KEY" in constraint_sql
                for constraint in item.args.get("constraints") or []:
                    reference = constraint.args.get("kind")
                    if isinstance(reference, exp.Reference) and isinstance(reference.this, exp.Schema) and reference.this.expressions:
                        table.foreign.append((column.name, reference.this.this.name.lower(), reference.this.expressions[0].name.lower()))
                if "PRIMARY KEY" in constraint_sql:
                    table.primary_key = (column.name,)
                table.columns.append(column)
            elif isinstance(item, exp.PrimaryKey):
                table.primary_key = tuple(e.name.lower() for e in item.expressions)
                for column in table.columns:
                    if column.name in table.primary_key:
                        column.not_null = True
            elif isinstance(item, exp.ForeignKey) and item.args.get("reference") is not None:
                reference = item.args["reference"]
                if isinstance(reference.this, exp.Schema) and len(item.expressions) == 1 == len(reference.this.expressions):
                    table.foreign.append((item.expressions[0].name.lower(), reference.this.this.name.lower(), reference.this.expressions[0].name.lower()))
            elif isinstance(item, exp.UniqueColumnConstraint):
                table.unique.append(tuple(e.name.lower() for e in item.this.expressions))
        tables[name] = table
    return tables


def load_pairs(path: Path) -> list[tuple[str, str]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return list(zip(lines[0::2], lines[1::2]))


def spark_days(sql: str) -> str:
    """Spark's date_sub(date, days) takes a number of days where MySQL wants an INTERVAL."""

    return re.sub(r"date_(sub|add)\(\s*('[\d-]+')\s*,\s*(\d+)\s*\)", r"date_\1(\2, INTERVAL \3 DAY)", sql, flags=re.I)


def to_dialect(sql: str, dialect: str) -> str:
    # Spark writes date('1994-01-01 +08'); the engines under test read the date part.
    sql = re.sub(r"date\(\s*'(\d{4}-\d{2}-\d{2})\s*[+-]\d{2}(?::?\d{2})?'\s*\)", r"date('\1')", sql, flags=re.I)
    sql = spark_days(sql)
    sql = re.sub(r"(?<=[\w$])\$|\$(?=\w)", "_S_", sql)  # DuckDB rejects $ in bare names (EXPR$0, $f1)
    return sqlglot.transpile(sql, read="mysql", write=dialect)[0]


def _duck_type(column: Column) -> str:
    base = column.type.split("(")[0]
    if base in {"VARCHAR", "CHAR", "TEXT"}:
        return "VARCHAR"
    if base in {"DECIMAL", "DOUBLE", "FLOAT", "NUMERIC"}:
        return "DOUBLE"
    if base in {"DATE", "TIMESTAMP", "DATETIME"}:
        return "DATE"
    return "BIGINT"


# Around the dates the TPC-H pairs filter on, so range predicates split the rows.
DATES = ["1993-12-31", "1994-01-01", "1994-09-01", "1994-12-15", "1995-03-21", "1996-06-30", "1997-01-01"]


def new_database(tables: dict[str, Table]):
    """An empty DuckDB database holding the schema."""

    import duckdb

    from kumosql.duckdb_load import small_database
    db = small_database()
    for table in tables.values():
        columns = ", ".join(f'"{c.name}" {_duck_type(c)}' for c in table.columns)
        db.execute(f'CREATE TABLE "{table.name}" ({columns})')
    return db


def random_rows(table: Table, rng: random.Random, numbers: list | None = None) -> list[list]:
    """A few rows with small value domains so joins and ties are common.

    With ``numbers`` (a few values near the queries' literals, shared by every table of the database),
    numeric columns draw from them, and each non-key column from one or two values about half the time,
    so rows sharing a join key or a whole row repeat often.
    """

    rows, keys = [], set()
    domains = {}
    key_columns = {c for key in ([table.primary_key] if table.primary_key else []) + list(table.unique) for c in key}
    for column in table.columns:
        kind = _duck_type(column)
        domain = {"VARCHAR": ["a", "b", "c"], "DATE": DATES}.get(kind, numbers or [0, 1, 2, 3])
        if numbers is not None and column.name not in key_columns and rng.random() < 0.5:
            domain = rng.sample(domain, min(len(domain), rng.choice([1, 2])))
        domains[column.name] = domain
    for _ in range(rng.choice([0, 0, 1, 2, 3, 4] if numbers is None else [1, 2, 3, 4, 5])):
        row = []
        for column in table.columns:
            value = rng.choice(domains[column.name])
            if not column.not_null and rng.random() < 0.25:
                value = None
            row.append(value)
        key_sets = ([table.primary_key] if table.primary_key else []) + list(table.unique)
        clash = False
        for index, key in enumerate(key_sets):
            value = (index, tuple(row[[c.name for c in table.columns].index(k)] for k in key))
            if None not in value[1] and value in keys:
                clash = True
        if clash:
            continue
        for index, key in enumerate(key_sets):
            keys.add((index, tuple(row[[c.name for c in table.columns].index(k)] for k in key)))
        rows.append(row)
    return rows


def referenced_tables(*queries: str) -> set[str]:
    names = set()
    for query in queries:
        for table in sqlglot.parse_one(query, read="mysql").find_all(exp.Table):
            names.add(table.name.lower())
    return names


# Calcite reads a literal in GROUP BY as a constant; BigQuery, MySQL and DuckDB read it as a column ordinal.
CONSTANT_GROUPING = {"calcite"}


def constant_groupings(sql: str) -> str:
    """Wrap each bare number in a GROUP BY in a cast so DuckDB groups by the constant, not an ordinal."""

    tree = sqlglot.parse_one(sql, read="duckdb")
    for group in tree.find_all(exp.Group):
        group.set(
            "expressions",
            [exp.cast(e, "int") if isinstance(e, exp.Literal) and not e.is_string else e for e in group.expressions],
        )
    return tree.sql(dialect="duckdb")


def name_values(sql: str) -> str:
    """Calcite calls the columns of an unnamed VALUES ``EXPR$0``, ``EXPR$1``, ..; DuckDB calls them ``col0``, .."""

    tree = sqlglot.parse_one(sql, read="mysql")
    for number, values in enumerate(tree.find_all(exp.Values)):
        if values.parent is not None and isinstance(values.parent, (exp.From, exp.Join)) and values.expressions and isinstance(values.expressions[0], exp.Tuple):
            alias = values.args.get("alias")
            if alias is not None and alias.columns:
                continue
            width = len(values.expressions[0].expressions)
            name = alias.name if alias is not None and alias.name else f"kqv{number}"
            values.set("alias", exp.TableAlias(this=exp.to_identifier(name), columns=[exp.to_identifier(f"EXPR${i}") for i in range(width)]))
    return tree.sql(dialect="mysql")


def _bag(rows) -> Counter:
    """Rows as a bag; numbers compared as floats rounded to 6 places (DuckDB returns DECIMAL as Decimal, DOUBLE as float)."""

    return Counter(tuple(round(float(v), 6) if isinstance(v, (float, Decimal)) else v for v in row) for row in rows)


def calcite_operators(sql: str) -> str:
    """Calcite's ``||`` concatenates strings; read as MySQL it would be OR (see ``tools/bench_sql_repairs.py``)."""

    if str(Path(__file__).resolve().parent) not in sys.path:  # loaded by file path (the tests), not run as a script
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bench_sql_repairs import pipes_as_concat

    try:
        return pipes_as_concat(sql)
    except (sqlglot.errors.SqlglotError, ValueError):
        return sql


def _random_check_cache(left_sql, right_sql, used, samples, db):
    """Optional local evidence for the sample executions only, never for a proof.

    Callers opt in only for a fresh connection from new_database(). Missing,
    corrupt or unwritable evidence falls back to executing the checks.
    """
    import duckdb
    from kumosql import duckdb_load

    folder = os.environ.get("KUMOSQL_EVAL_CACHE", "").strip()
    if folder.lower() in ("", "off", "0", "false"):
        return None, False
    if any(None in sample for sample in samples):
        return None, False  # A row without a literal representation has no reusable key.
    try:
        root = Path(__file__).resolve().parent
        sources = [Path(__file__), Path(duckdb_load.__file__), root / "bench_sql_repairs.py"]
        context = {
            "format": 1,
            "queries": [left_sql, right_sql],
            "tables": [asdict(t) for t in used],
            "samples": samples,
            "sources": [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources],
            "sqlglot": sqlglot.__version__,
            "compiled": any(Path(sqlglot.__file__).parent.rglob("*.so")) or any(Path(sqlglot.__file__).parent.rglob("*.pyd")),
            "duckdb": duckdb.__version__,
            "python": platform.python_version(),
            "platform": [platform.system(), platform.machine()],
            "settings": db.execute("SELECT name, value FROM duckdb_settings() ORDER BY name").fetchall(),
            "ddl": db.execute("SELECT schema_name, table_name, sql FROM duckdb_tables() ORDER BY schema_name, table_name").fetchall(),
        }
        key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        path = Path(folder) / f"{key}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            hit = record == {"key": key, "random_agreement": True} and 0 <= time.time() - path.stat().st_mtime < 7 * 86400
        except (OSError, ValueError):
            hit = False
        return path, hit
    except (OSError, duckdb.Error):
        return None, False


def _save_random_agreement(path):
    if path is None:
        return
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"key": path.stem, "random_agreement": True}, handle)
        os.replace(temporary, path)
    except OSError:
        pass  # An unavailable cache cannot change an eval verdict.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def differ(left: str, right: str, tables: dict[str, Table], db, trials: int = 60, seed: int = 11, constants: bool = False, *, cache_random: bool = False):
    """A database on which the queries differ as bags, else ``None``; ``False`` if DuckDB rejects them."""

    import duckdb

    from kumosql.duckdb_load import TableLoader, rows_key, run_unoptimized

    rng = random.Random(seed)
    left, right = spark_days(left), spark_days(right)
    if constants:
        left, right = name_values(left), name_values(right)
    try:
        left_sql, right_sql = to_dialect(left, "duckdb"), to_dialect(right, "duckdb")
        if constants:
            left_sql, right_sql = constant_groupings(left_sql), constant_groupings(right_sql)
        used = [tables[n] for n in sorted(referenced_tables(left, right)) if n in tables]
    except sqlglot.errors.SqlglotError:
        return False
    numbers = {0, 1, 2, 3}
    for literal in [lit for sql in (left, right) for lit in sqlglot.parse_one(sql, read="mysql").find_all(exp.Literal)]:
        if not literal.is_string and re.fullmatch(r"-?\d+", literal.this) and abs(int(literal.this)) < 10**6:
            numbers.update({int(literal.this) - 1, int(literal.this), int(literal.this) + 1})
    samples = []
    # Plain databases first, then skewed ones whose numbers sit next to the queries' literals
    for trial in range(2 * trials):
        # every table of a skewed database draws its numbers from the same few, so join keys meet
        shared = rng.sample(sorted(numbers), min(len(numbers), rng.choice([2, 3, 4]))) if trial >= trials else None
        database = {f'"{table.name}"': random_rows(table, rng, shared if trial >= trials else None) for table in used}
        key = rows_key(database)
        samples.append((database, key))
    cache_path, hit = _random_check_cache(left_sql, right_sql, used, [key for _, key in samples], db) if cache_random else (None, False)
    if hit:
        logging.getLogger(__name__).info("Reusing sample execution evidence: %s", cache_path.stem)
        return targeted_differ(left_sql, right_sql, used)
    loader = TableLoader(db)
    agreed: set[tuple] = set()  # databases the queries were already run on without a difference
    for database, key in samples:
        if key in agreed:
            continue  # small random databases repeat (empty tables, one-row tables); the answer would too
        try:
            loader.load(database, key)
            a = _bag(db.execute(left_sql).fetchall())
            b = _bag(db.execute(right_sql).fetchall())
            # DuckDB's optimizer disagreeing with its unoptimized plan is not evidence
            found = a != b and [_bag(rows) for rows in run_unoptimized(db, left_sql, right_sql)] == [a, b]
        except duckdb.Error:
            return False
        if found:
            return (left_sql, right_sql, a, b)
        if None not in key:
            agreed.add(key)
    _save_random_agreement(cache_path)
    return targeted_differ(left_sql, right_sql, used)


_TARGETED_TYPES = {"VARCHAR": "STRING", "DOUBLE": "FLOAT64", "DATE": "DATE", "BIGINT": "INT64"}


def targeted_differ(left_sql: str, right_sql: str, used: list[Table]):
    """The targeted suite (kumosql.refute) as a last look; same result shape as ``differ``."""

    import os

    if os.environ.get("KUMOSQL_TARGETED", "1") == "0":
        return None
    from kumosql.refute import find_targeted_difference
    from kumosql.result_equivalence import DataRules

    names = {t.name for t in used}
    schema = {t.name: {c.name: _TARGETED_TYPES[_duck_type(c)] for c in t.columns} for t in used}
    rules = {
        t.name: DataRules(
            frozenset(c.name for c in t.columns if c.not_null),
            tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
        )
        for t in used
    }
    foreign = [(t.name, c, p, pc) for t in used for c, p, pc in t.foreign if p in names]
    found = find_targeted_difference(left_sql, right_sql, schema, rules, foreign_keys=foreign, dialect="duckdb", budget=20.0, booleans_are_integers=True)
    if found is None:
        return None
    return (left_sql, right_sql, Counter(found.left.rows), Counter(found.right.rows))


@dataclass
class SuiteResult:
    name: str
    total: int = 0
    proved: int = 0
    wrong: list = field(default_factory=list)
    unchecked: int = 0
    seconds: float = 0.0
    unproved: list = field(default_factory=list)
    excluded: list = field(default_factory=list)  # pairs that must stay unproven (see tests/fixtures/sqlsolver/not_provable.json)

    @property
    def scored(self) -> int:
        """Pairs a prover should prove: all of them except the ones that must stay unproven."""

        return self.total - len(self.excluded)


def must_not_prove(name: str) -> dict[int, str]:
    """Pairs of a suite that hold only if tie-breaking is fixed (LIMIT without ORDER BY) or only on some strings
    (a case map of another one) and so must never be proved as written, with the reason. They stay in the suite as a guard and leave the score's denominator."""

    path = FIXTURES / "not_provable.json"
    return {int(index): reason for index, reason in json.loads(path.read_text(encoding="utf-8")).get(name, {}).items()}


def run_suite(name: str, prove, limit: int | None = None, trials: int = 60) -> SuiteResult:
    pairs_file, schema_file = SUITES[name]
    tables = load_schema(FIXTURES / schema_file)
    pairs = load_pairs(FIXTURES / pairs_file)[:limit]
    result = SuiteResult(name, total=len(pairs))
    result.excluded = sorted(index for index in must_not_prove(name) if index < len(pairs))
    start = time.time()
    db = new_database(tables)
    for index, (left, right) in enumerate(pairs):
        if name in CONSTANT_GROUPING:
            left, right = calcite_operators(left), calcite_operators(right)
        try:
            proof = prove(left, right, tables, name in CONSTANT_GROUPING) if name in CONSTANT_GROUPING else prove(left, right, tables)
        except Exception as error:  # a crash is a failure to prove, never a proof
            proof = False
            result.unproved.append((index, f"crash: {type(error).__name__}: {error}"))
            continue
        if not proof:
            result.unproved.append((index, "not proven"))
            continue
        if index in result.excluded:
            result.wrong.append((index, "proved, but the pair must stay unproven"))
            continue
        result.proved += 1
        counter = differ(left, right, tables, db, trials, constants=name in CONSTANT_GROUPING, cache_random=True)
        if counter is False:
            result.unchecked += 1
        elif counter is not None:
            result.wrong.append((index, counter))
    result.seconds = time.time() - start
    return result


def default_prove(left: str, right: str, tables: dict[str, Table], constants: bool = False) -> bool:
    """Whether the prover proves the pair; answered from the verdict cache (``KUMOSQL_VERDICT_CACHE``) when the prover's source is unchanged."""

    from kumosql import verdict_cache

    if verdict_cache.folder() is None:
        return prove_result(left, right, tables, constants).proven
    # This file is part of the question: ``prove_result`` picks the dialect, the constraints and the options of the proof.
    payload = {"left": left, "right": right, "tables": [asdict(t) for t in sorted(tables.values(), key=lambda t: t.name)], "constants": constants, "runner": verdict_cache.source_digest(__file__)}
    return verdict_cache.cached_verdict("sqlsolver_bench.default_prove", payload, lambda: prove_result(left, right, tables, constants).proven)


def prove_result(left: str, right: str, tables: dict[str, Table], constants: bool = False):
    """The prover's full result (status, reason and any counterexample) for one pair."""

    from kumosql.algebraic_equivalence import prove_equivalent_algebraic

    from kumosql.smt_equivalence import TableConstraints

    schema = {t.name: [c.name for c in t.columns] for t in tables.values()}
    constraints = {
        t.name: TableConstraints(
            not_null=frozenset(c.name for c in t.columns if c.not_null),
            keys=tuple(k for k in ([t.primary_key] if t.primary_key else []) + list(t.unique)),
        )
        for t in tables.values()
    }
    return prove_equivalent_algebraic(
        spark_days(left), spark_days(right), schema=schema, constraints=constraints, types={t.name: {c.name: c.type for c in t.columns} for t in tables.values()}, compare_names=False, dialect="mysql", exact_arithmetic=True, group_by_constants=constants
    )


def main(argv: list[str] | None = None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or list(SUITES)
    print(f"{'suite':8} {'pairs':>6} {'scored':>7} {'proved':>7} {'unknown':>8} {'wrong':>6} {'unchecked':>10} {'sec':>6}")
    bad = 0
    for name in names:
        r = run_suite(name, default_prove)
        print(f"{r.name:8} {r.total:6} {r.scored:7} {r.proved:7} {r.scored - r.proved:8} {len(r.wrong):6} {r.unchecked:10} {r.seconds:6.1f}")
        bad += len(r.wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
