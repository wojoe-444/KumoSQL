"""Behaviour-preservation eval for BigQuery / GoogleSQL queries.

For each case the KumoSQL rewrite pipeline runs, and when it changes the SQL both versions are
executed in DuckDB (after sqlglot's BigQuery -> DuckDB transpile) and their results compared. Three
scores are kept apart, as in the scoreboard page:

* correctness: rewrites KumoSQL accepts (proven, or unchanged) whose results differ. Must be 0.
* coverage: handled (rewritten and result-identical) / declined (left unchanged or unproven) /
  unsupported (does not parse) / error (a crash in KumoSQL).
* performance: seconds per case.

``--googlesql DIR`` reads GoogleSQL compliance ``.test`` files (Apache-2.0, google/googlesql,
formerly ZetaSQL); only the SQL text is used, never the expected results. DuckDB is a differential
oracle here, so the same query is run before and after; a query DuckDB cannot run counts as
"not executable" and gives no verdict.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from kumosql import rewrite

import logging

logging.getLogger("sqlglot").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
GOOGLESQL_CORPUS = ROOT / "tests" / "fixtures" / "googlesql" / "queries.json.gz"
EDGE_CASES = ROOT / "tests" / "fixtures" / "bq_edge" / "cases.json"

PIPELINES = {
    "canonical": rewrite.canonical_rule_order(),
    "semantic": tuple(r for r in rewrite.canonical_rule_order() if r != "format_sql"),
    "lift": ("lift_subqueries",) + tuple(r for r in rewrite.canonical_rule_order() if r != "format_sql"),
}


# --- corpus extraction -------------------------------------------------------------------------

def parse_test_file(text: str):
    """Yield (name, sql, expects_error) for each case in a GoogleSQL compliance .test file."""

    for block in text.split("\n==\n"):
        lines = block.strip("\n").split("\n")
        name, i = None, 0
        while i < len(lines) and (lines[i].startswith("[") or not lines[i].strip()):
            m = re.match(r"\[name=(.+?)\]", lines[i])
            if m:
                name = m.group(1)
            i += 1
        body = "\n".join(lines[i:])
        sql, _, expected = body.partition("\n--\n")
        sql = sql.strip()
        if name and sql and re.match(r"(?is)\s*(select|with|\()", sql):
            yield name, sql, expected.lstrip().startswith("ERROR")


def extract(directory: Path):
    seen, out = set(), []
    for path in sorted(directory.glob("*.test")):
        for name, sql, errors in parse_test_file(path.read_text(encoding="utf-8", errors="replace")):
            if errors or "@" in sql or sql in seen:
                continue
            seen.add(sql)
            out.append({"id": f"{path.stem}/{name}", "sql": sql})
    return out


HELDOUT_CASES = EDGE_CASES.with_name("heldout.json")


def load_edge(path: Path = EDGE_CASES):
    setup = json.loads(EDGE_CASES.read_text(encoding="utf-8"))["setup"]
    data = json.loads(path.read_text(encoding="utf-8"))
    return [dict(case, setup=setup) for case in data["cases"]]


def load_googlesql():
    with gzip.open(GOOGLESQL_CORPUS, "rt", encoding="utf-8") as f:
        return json.load(f)


# --- execution ---------------------------------------------------------------------------------

def _norm(value):
    if isinstance(value, float):
        return "nan" if value != value else round(value, 6)
    if isinstance(value, (list, tuple)):
        return tuple(_norm(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _norm(v)) for k, v in value.items()))
    return value


def run_duckdb(sql: str, setup: list[str] | None = None):
    """Execute BigQuery SQL in DuckDB; return ("ok", rows) or ("error", message)."""

    try:
        statements = sqlglot.transpile(sql, read="bigquery", write="duckdb")
        from kumosql.duckdb_load import small_database
        con = small_database()
        # One thread: with several, DuckDB may build ARRAY(SELECT .. UNION ALL ..) in a different
        # order on each run, and the same query would look like it changed behaviour.
        con.execute("SET threads TO 1")
        for stmt in setup or []:
            con.execute(stmt)
        rows = con.execute(statements[0]).fetchall()
    except Exception as exc:  # noqa: BLE001
        return "error", f"{type(exc).__name__}: {str(exc)[:100]}"
    return "ok", rows


def ordered(sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return False
    return bool(tree.args.get("order")) if tree is not None else False


def _deep_sorted(value):
    if isinstance(value, tuple):
        return tuple(sorted((_deep_sorted(v) for v in value), key=repr))
    return value


def unordered_aggregates(sql: str) -> set[str]:
    """Aggregates whose element order BigQuery leaves unspecified (no ORDER BY inside the call)."""

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except Exception:  # noqa: BLE001
        return set()
    kinds = (exp.ArrayConcatAgg, exp.ArrayAgg, exp.GroupConcat)
    return {
        type(node).__name__
        for node in tree.find_all(*kinds)
        if not isinstance(node.this, exp.Order)
    }


def same_results(before, after, keep_order: bool, unordered_elements: bool = False) -> bool:
    a = [_norm(r) for r in before]
    b = [_norm(r) for r in after]
    if unordered_elements:
        # ARRAY_AGG / ARRAY_CONCAT_AGG without ORDER BY: the element order is not part of the contract,
        # so lists compare as multisets (DuckDB may also order them differently from run to run).
        a = [_deep_sorted(r) for r in a]
        b = [_deep_sorted(r) for r in b]
    if keep_order:
        return a == b
    key = lambda r: repr(r)  # noqa: E731
    return sorted(a, key=key) == sorted(b, key=key)


# --- one case ----------------------------------------------------------------------------------

def evaluate(sql: str, rules, setup: list[str] | None = None) -> dict:
    """Return {"class": ..., "detail": ...}.

    Classes: unsupported, error, declined, handled, not_executable, WRONG.
    """

    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
        if tree is None or isinstance(tree, exp.Command):
            return {"class": "unsupported", "detail": "parse"}
    except Exception as exc:  # noqa: BLE001
        return {"class": "unsupported", "detail": f"parse: {type(exc).__name__}"}
    try:
        result = rewrite.apply_rules(rules, sql)
    except Exception as exc:  # noqa: BLE001
        return {"class": "error", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
    after = result.sql
    if after.strip() == sql.strip():
        return {"class": "declined", "detail": "unchanged", "trusted": True}
    try:
        if sqlglot.parse_one(after, read="bigquery") == tree:
            return {"class": "declined", "detail": "layout only", "trusted": True}
    except Exception:  # noqa: BLE001
        pass
    trusted = result.verification.trusted
    s1, r1 = run_duckdb(sql, setup)
    if s1 != "ok":
        return {"class": "not_executable", "detail": r1, "trusted": trusted, "after": after}
    s2, r2 = run_duckdb(after, setup)
    keep = ordered(sql)
    loose = unordered_aggregates(sql)
    if "GroupConcat" in loose:
        # STRING_AGG without ORDER BY concatenates in an unspecified order; the result text cannot be compared.
        return {"class": "not_executable", "detail": "order-unspecified STRING_AGG", "trusted": trusted, "after": after}
    if s2 != "ok" or not same_results(r1, r2, keep, bool(loose)):
        detail = r2 if s2 != "ok" else "results differ"
        return {"class": "WRONG" if trusted else "declined", "detail": detail, "trusted": trusted, "after": after,
                "unproven_but_wrong": not trusted}
    return {"class": "handled" if trusted else "declined", "detail": "", "trusted": trusted, "after": after}


def summarize(results: list[dict]) -> dict:
    c = Counter(r["class"] for r in results)
    wrong = c.get("WRONG", 0)
    return {
        "cases": len(results),
        "wrong": wrong,
        "handled": c.get("handled", 0),
        "declined": c.get("declined", 0),
        "unsupported": c.get("unsupported", 0),
        "error": c.get("error", 0),
        "not_executable": c.get("not_executable", 0),
        "unproven_changes_that_differ": sum(1 for r in results if r.get("unproven_but_wrong")),
        "changed": c.get("handled", 0) + wrong + sum(1 for r in results if r.get("after") and r["class"] == "declined" and r.get("detail") != "layout only"),
        "rewritten_and_checked": c.get("handled", 0) + wrong,
    }


def run_corpus(cases, pipeline: str):
    rules = PIPELINES[pipeline]
    out = []
    t0 = time.perf_counter()
    for case in cases:
        r = evaluate(case["sql"], rules, case.get("setup"))
        r["id"] = case["id"]
        out.append(r)
    return out, time.perf_counter() - t0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--googlesql", type=Path, help="directory of GoogleSQL compliance .test files: write the corpus")
    ap.add_argument("--corpus", choices=["googlesql", "edge", "heldout", "all"], default="all")
    ap.add_argument("--pipeline", choices=sorted(PIPELINES), default="semantic")
    ap.add_argument("--details", action="store_true", help="list not-executable and unsupported cases")
    ap.add_argument("--failures", action="store_true")
    args = ap.parse_args(argv)
    if args.googlesql:
        cases = extract(args.googlesql)
        GOOGLESQL_CORPUS.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(GOOGLESQL_CORPUS, "wt", encoding="utf-8") as f:
            json.dump(cases, f)
        print(f"wrote {len(cases)} queries to {GOOGLESQL_CORPUS}")
        return 0
    bad = 0
    for name, loader in (("googlesql", load_googlesql), ("edge", load_edge), ("heldout", lambda: load_edge(HELDOUT_CASES))):
        if args.corpus not in (name, "all") or not {"googlesql": GOOGLESQL_CORPUS, "edge": EDGE_CASES, "heldout": HELDOUT_CASES}[name].exists():
            continue
        results, secs = run_corpus(loader(), args.pipeline)
        s = summarize(results)
        print(f"{name}/{args.pipeline}: {s}  {secs:.1f}s")
        if args.failures:
            for r in results:
                if r["class"] in ("WRONG", "error") or (args.details and r["class"] in ("not_executable", "unsupported")):
                    print("  ", r["class"], r["id"], r["detail"])
        bad += s["wrong"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
