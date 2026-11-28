"""``python -m kumosql reconcile``, ``rules`` and ``key-check``: compare two tables by key, re-check the rules, and check a key.

All are READ-ONLY: each call is one read-only query (BigQuery: one job in the billing project,
dry-run first and capped in bytes billed), and the result is printed as JSON. ``--output`` writes the
file you name; nothing else is written. See :mod:`kumosql.reconcile`, :mod:`kumosql.key_check` and docs/reconcile.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import key_check as keys
from . import reconcile as engine
from .reconcile_rules import RuleError, load_rules

#: Exit codes: 0 nothing to report, 1 something to look at, 2 the command could not run, 3 over the byte cap.
OK, FOUND, ERROR, OVER_CAP = 0, 1, 2, 3


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--left", required=True,
                        help="the left side: project.dataset.table (or dataset.table in the billing project), a .sql file, "
                             "or a SELECT/WITH query")
    parser.add_argument("--right", required=True, help="the right side, in the same forms as --left")
    parser.add_argument("--key", required=True,
                        help="the key columns, comma separated; write left_col=right_col when the names differ")
    parser.add_argument("--ignore", help="columns not to compare, comma separated")
    parser.add_argument("--map", help="a .yml/.yaml/.json crosswalk of left to right column names, or `saved` for the "
                                      "saved equivalences (python -m kumosql equivalence list)")
    parser.add_argument("--float-tolerance", type=float, default=engine.DEFAULT_FLOAT_ABSOLUTE, metavar="ABS",
                        help="floating-point columns agree when they differ by at most this much "
                             f"(default {engine.DEFAULT_FLOAT_ABSOLUTE:g}), or by --float-relative, whichever is larger")
    parser.add_argument("--float-relative", type=float, default=engine.DEFAULT_FLOAT_RELATIVE, metavar="REL",
                        help="... or by this fraction of the larger of the two values "
                             f"(default {engine.DEFAULT_FLOAT_RELATIVE:g}); 0 and 0 compare floats exactly")
    parser.add_argument("--tolerance", action="append", metavar="COL=ABS[:REL]",
                        help="one column's own tolerance, replacing the float defaults for it: col=0.01 (absolute), "
                             "col=0.01:1e-6 (absolute and relative) or col=exact; also gives a NUMERIC or integer column "
                             "a tolerance. Comma separated or repeated")
    _engine(parser)


def _engine(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", help="BigQuery billing project (default: the one chosen in Settings)")
    parser.add_argument("--max-bytes", type=int, help="BigQuery byte cap for the query (default: Settings → Scopes)")
    parser.add_argument("--duckdb", help="run on this DuckDB database file (opened read-only) instead of BigQuery, or "
                                         ":memory: to compare queries over files (read_csv_auto, read_parquet)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the query and BigQuery's byte estimate; run nothing")
    parser.add_argument("--sql-only", action="store_true",
                        help="print the query and run nothing (the column lists are still read: free dry runs on BigQuery)")
    parser.add_argument("-o", "--output", type=Path, help="also write the printed text to this file")


def _reconcile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql reconcile",
        description="READ-ONLY. Compare two tables joined on a key in one query: count the keys only on each side, the "
                    "rows whose columns differ (NULL-safe), and sort every differing row under the first known-difference "
                    "rule that explains it (--rules) or as unexplained. Floating-point columns agree within a small "
                    "tolerance (--float-tolerance, --float-relative, --tolerance). Prints JSON. Exit 0 when every row matches or is "
                    "explained, 1 when some are unexplained, 2 on an input error, 3 over the byte cap.",
    )
    _common(parser)
    parser.add_argument("--rules", type=Path, help="known-difference rules (.yml/.yaml/.json), tried in the order listed")
    parser.add_argument("--include-samples", action="store_true",
                        help="also return a few keys of unexplained rows (off by default: key values can be sensitive)")
    parser.add_argument("--sample-limit", type=int, default=engine.DEFAULT_SAMPLE_LIMIT,
                        help=f"keys per kind with --include-samples (default {engine.DEFAULT_SAMPLE_LIMIT}, "
                             f"at most {engine.MAX_SAMPLE_LIMIT})")
    return parser


def _rules_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql rules",
        description="READ-ONLY. Known-difference rules for python -m kumosql reconcile: `check` re-runs them on current "
                    "data and flags a rule that no longer explains any row or explains far more rows than when it was "
                    "verified (exit 1); `list` prints them.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{check,list}")
    check = sub.add_parser("check", help="re-run the rules on current data in one query and flag stale ones",
                           description="READ-ONLY. Re-run every rule in one query. A rule is flagged no_longer_explains "
                                       "when it explains no row, and grew_significantly when it explains more than "
                                       "max(verified_count x growth factor, verified_count + min growth) rows. Exit 0 "
                                       "when every rule is ok, 1 when any is flagged, 2 on an input error, 3 over the "
                                       "byte cap.")
    check.add_argument("--rules", type=Path, required=True, help="the rules file (.yml/.yaml/.json)")
    _common(check)
    check.add_argument("--growth-factor", type=float, default=engine.DEFAULT_GROWTH_FACTOR,
                       help=f"flag a rule that explains this many times its verified_count "
                            f"(default {engine.DEFAULT_GROWTH_FACTOR:g}; a rule's max_growth_ratio overrides it)")
    check.add_argument("--min-growth", type=int, default=engine.DEFAULT_MIN_GROWTH,
                       help=f"and at least this many rows more (default {engine.DEFAULT_MIN_GROWTH})")
    listing = sub.add_parser("list", help="print the rules as JSON", description="READ-ONLY. Print the rules as JSON.")
    listing.add_argument("--rules", type=Path, required=True, help="the rules file (.yml/.yaml/.json)")
    return parser


def _executor(args: argparse.Namespace):
    if args.duckdb:
        import duckdb

        if args.duckdb == ":memory:":
            return engine.DuckDBExecutor(duckdb.connect())
        if not Path(args.duckdb).is_file():
            raise engine.ReconcileError(f"--duckdb {args.duckdb} is not a file")
        return engine.DuckDBExecutor(duckdb.connect(str(args.duckdb), read_only=True))
    return engine.BigQueryExecutor(args.project, args.max_bytes)


def _emit(text: str, output: Path | None) -> None:
    if output:
        output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def _plan_text(plan, executor, dry_run: bool) -> str:
    text = plan.sql + ";\n"
    if not dry_run:
        return text
    if not hasattr(executor, "estimate"):
        return text + "-- DuckDB: nothing was run\n"
    estimate = engine.estimate_bytes(plan, executor)
    size = "unknown" if estimate is None else f"{estimate:,}"
    return text + f"-- estimated bytes processed: {size} (cap: {executor.max_bytes:,}); nothing was run\n"


def _fail(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return OVER_CAP if isinstance(exc, engine.ByteCapExceeded) else ERROR


def _tolerances(args: argparse.Namespace) -> dict:
    return dict(float_tolerance=args.float_tolerance, float_relative=args.float_relative, tolerance=args.tolerance)


def reconcile_main(argv: list[str] | None = None) -> int:
    args = _reconcile_parser().parse_args(argv)
    options = dict(ignore=args.ignore or (), crosswalk=args.map, rules=args.rules,
                   include_samples=args.include_samples, sample_limit=args.sample_limit, **_tolerances(args))
    try:
        executor = _executor(args)
        if args.dry_run or args.sql_only:
            plan = engine.plan_reconcile(args.left, args.right, args.key, executor, **options)
            _emit(_plan_text(plan, executor, args.dry_run and not args.sql_only), args.output)
            return OK
        result = engine.reconcile(args.left, args.right, args.key, executor, **options)
    except (engine.ReconcileError, RuleError) as exc:
        return _fail(exc)
    _emit(json.dumps(result.to_json(), indent=2) + "\n", args.output)
    return OK if result.ok else FOUND


def rules_main(argv: list[str] | None = None) -> int:
    args = _rules_parser().parse_args(argv)
    try:
        if args.command == "list":
            rules = load_rules(args.rules)
            sys.stdout.write(json.dumps({"count": len(rules), "rules": [rule.to_json() for rule in rules]}, indent=2) + "\n")
            return OK
        executor = _executor(args)
        if args.dry_run or args.sql_only:
            plan = engine.plan_reconcile(args.left, args.right, args.key, executor, ignore=args.ignore or (),
                                         crosswalk=args.map, rules=args.rules, standalone=True, **_tolerances(args))
            _emit(_plan_text(plan, executor, args.dry_run and not args.sql_only), args.output)
            return OK
        result = engine.check_rules(args.left, args.right, args.key, executor, args.rules, ignore=args.ignore or (),
                                    crosswalk=args.map, growth_factor=args.growth_factor, min_growth=args.min_growth,
                                    **_tolerances(args))
    except (engine.ReconcileError, RuleError) as exc:
        return _fail(exc)
    _emit(json.dumps(result.to_json(), indent=2) + "\n", args.output)
    return OK if result.ok else FOUND


def _key_check_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql key-check",
        description="READ-ONLY. Is this a key? In one query: how many key values repeat and how many rows they cover, the "
                    "largest groups (sizes; key values only with --include-samples), the NULLs per key column and, with "
                    "--join, how the proposed join fans out and what it leaves unmatched each way. Prints JSON. Exit 0 "
                    "when the key is unique and never NULL (and the join repeats no row), 1 when it is not, 2 on a usage "
                    "or input error, 3 over the byte cap.",
    )
    parser.add_argument("table", help="project.dataset.table (or dataset.table in the billing project), a .sql file, or a "
                                      "SELECT/WITH query")
    parser.add_argument("--key", required=True, help="the proposed key columns of TABLE, comma separated")
    parser.add_argument("--join", help="the other table of a proposed join, in the same forms as TABLE")
    parser.add_argument("--on", help="key_column=other_column pairs for --join, comma separated; a key column with the "
                                     "same name in the other table needs none")
    parser.add_argument("--include-samples", action="store_true",
                        help="also return the key values of the largest repeating groups (off by default: key values "
                             "can be sensitive)")
    parser.add_argument("--top", type=int, default=keys.DEFAULT_TOP,
                        help=f"how many of the largest repeating groups to list (default {keys.DEFAULT_TOP}, "
                             f"at most {keys.MAX_TOP})")
    _engine(parser)
    return parser


def key_check_main(argv: list[str] | None = None) -> int:
    args = _key_check_parser().parse_args(argv)
    options = dict(join=args.join, on=args.on, include_samples=args.include_samples, top=args.top)
    try:
        executor = _executor(args)
        if args.dry_run or args.sql_only:
            plan = keys.plan_key_check(args.table, args.key, executor, **options)
            _emit(_plan_text(plan, executor, args.dry_run and not args.sql_only), args.output)
            return OK
        result = keys.key_check(args.table, args.key, executor, **options)
    except engine.ReconcileError as exc:
        return _fail(exc)
    _emit(json.dumps(result.to_json(), indent=2) + "\n", args.output)
    return OK if result.ok else FOUND


if __name__ == "__main__":
    raise SystemExit(reconcile_main())
