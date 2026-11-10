"""The package works without its optional extras: ``z3-solver`` (``smt``) and ``duckdb`` (``execution``).

Only sqlglot and sqlfluff are required. ``import kumosql`` used to crash without z3 (a module-level ``z3.Z3_OP_ADD``
in ``smt_equivalence``, and four rule modules that imported z3 unguarded), so a plain install could not even print
the command list. Each check below runs in its own interpreter with the extra made unimportable.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

PRELUDE = """
import sys
for name in {blocked!r}:
    sys.modules[name] = None  # ``import name`` now raises ImportError, as on an install without the extra
sys.path.insert(0, {src!r})
"""

IMPORT_EVERYTHING = PRELUDE + """
import importlib, json, pkgutil
import kumosql
failed = {{}}
for info in pkgutil.walk_packages(kumosql.__path__, "kumosql."):
    try:
        importlib.import_module(info.name)
    except BaseException as error:
        failed[info.name] = f"{{type(error).__name__}}: {{error}}"
print(json.dumps(failed))
"""

ENTRY_POINTS = PRELUDE + """
import contextlib, io, json, os, tempfile
import kumosql
from kumosql import bounded_equivalence, check_result_equivalence, prove_equivalent, prove_equivalent_smt, smt_equivalence, sqlsolver_backend
from kumosql.algebraic_equivalence import normalize, prove_equivalent_algebraic

SAME = ("SELECT a FROM t WHERE a > 1 AND b = 2", "SELECT a FROM t WHERE b = 2 AND a > 1")
PARTITIONS = "SELECT a FROM t WHERE a = 1 UNION ALL SELECT a FROM t WHERE a <> 1 OR a IS NULL"
QUANTIFIED = "SELECT a FROM t WHERE a > ALL (SELECT b FROM u) AND a NOT IN (SELECT b FROM u)"
schema = {{"t": ["a", "b"], "u": ["b"]}}
types = {{"t": {{"a": "INT64", "b": "INT64"}}, "u": {{"b": "INT64"}}}}
out = {{}}

def record(name, call):
    try:
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = call()
        status = getattr(result, "status", None)
        out[name] = {{"status": getattr(status, "value", None) or str(status), "reason": str(getattr(result, "reason", "") or getattr(result, "detail", "")), "returned": result if isinstance(result, (int, str)) else None, "printed": printed.getvalue()}}
    except BaseException as error:
        out[name] = {{"raised": f"{{type(error).__name__}}: {{error}}"}}

record("structural", lambda: prove_equivalent("SELECT a FROM t", "SELECT a FROM t"))
record("smt", lambda: prove_equivalent_smt(*SAME, schema=schema))
record("algebraic", lambda: prove_equivalent_algebraic(*SAME, schema=schema))
record("algebraic partitions", lambda: prove_equivalent_algebraic(PARTITIONS, "SELECT a FROM t", schema=schema))
record("algebraic quantified", lambda: prove_equivalent_algebraic(QUANTIFIED, QUANTIFIED, schema=schema, dialect="mysql"))
record("normalize partitions", lambda: normalize(PARTITIONS, schema=schema))
record("normalize quantified", lambda: normalize(QUANTIFIED, schema=schema, dialect="mysql"))
record("bounded", lambda: bounded_equivalence.check_bounded(*SAME, bounded_equivalence.schema_from_prover(schema, types=types)))
record("result", lambda: check_result_equivalence(*SAME, types))
folder = tempfile.mkdtemp()
paths = []
for index, sql in enumerate(SAME):
    paths.append(os.path.join(folder, f"{{index}}.sql"))
    with open(paths[-1], "w", encoding="utf-8") as handle:
        handle.write(sql)
with open(os.path.join(folder, "schema.json"), "w", encoding="utf-8") as handle:
    json.dump(schema, handle)
record("smt command", lambda: smt_equivalence.main([*paths, "--schema", os.path.join(folder, "schema.json")]))
record("sqlsolver command", lambda: sqlsolver_backend.main([*paths, "--schema", os.path.join(folder, "schema.json"), "--backend", "algebraic"]))
print(json.dumps(out))
"""


def run(script: str, blocked: tuple[str, ...]) -> dict:
    done = subprocess.run([sys.executable, "-c", script.format(blocked=blocked, src=str(SRC))], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("blocked", [("z3",), ("duckdb",), ("z3", "duckdb")], ids=lambda b: "without-" + "-and-".join(b))
def test_every_module_imports_without_the_optional_extras(blocked):
    assert run(IMPORT_EVERYTHING, blocked) == {}


def test_the_command_list_prints_without_any_extra():
    script = PRELUDE.format(blocked=("z3", "duckdb"), src=str(SRC)) + "import runpy\nsys.argv = ['kumosql', '--help']\nrunpy.run_module('kumosql', run_name='__main__')\n"
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "usage" in done.stdout.lower()


def test_without_z3_every_prover_declines_with_one_reason_and_never_raises():
    out = run(ENTRY_POINTS, ("z3",))
    assert not {name: entry["raised"] for name, entry in out.items() if "raised" in entry}
    missing = "z3-solver is not installed (pip install z3-solver)"
    for name in ("smt", "algebraic", "algebraic partitions", "algebraic quantified"):
        assert out[name]["status"] == "not_proven" and out[name]["reason"] == missing, (name, out[name])
    assert out["bounded"]["status"] == "unknown" and out["bounded"]["reason"] == missing
    assert out["structural"]["status"] == "proven_equivalent"  # the structural prover needs no solver
    assert out["result"]["status"] == "equivalent"  # nor does running both queries
    for name in ("smt command", "sqlsolver command"):
        assert out[name]["returned"] == 1 and missing in out[name]["printed"], (name, out[name])


def test_without_z3_the_rules_that_ask_the_solver_leave_the_query_as_written():
    """``normalize`` is called on its own too; a rule that cannot ask the solver must not fail halfway through."""

    out = run(ENTRY_POINTS, ("z3",))
    assert "raised" not in out["normalize partitions"] and "raised" not in out["normalize quantified"]


def test_without_duckdb_proofs_still_work_and_the_executed_checks_say_what_is_missing():
    pytest.importorskip("z3")
    out = run(ENTRY_POINTS, ("duckdb",))
    assert not {name: entry["raised"] for name, entry in out.items() if "raised" in entry}
    for name in ("smt", "algebraic", "algebraic partitions"):
        assert out[name]["status"] == "proven_equivalent", (name, out[name])
    assert out["bounded"]["status"] == "bounded_equivalent", out["bounded"]  # nothing to replay: no difference within the bound
    assert out["result"]["status"] == "error" and "duckdb is required" in out["result"]["reason"] and "kumosql[" not in out["result"]["reason"]
    assert out["smt command"]["returned"] == 0
