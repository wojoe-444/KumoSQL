"""Check every incremental model of a Dataform project against its full refresh.

``python -m kumosql incremental-report PROJECT`` finds the ``type: "incremental"``
actions under ``definitions/``, rebuilds each one's source tables from the
columns its query reads, and asks :func:`kumosql.incremental.check_incremental`
about three contracts:

* ``append_only``: sources only ever gain newer rows;
* ``late_and_duplicate``: rows may also arrive late, or be delivered again;
* ``mutable``: rows may also be updated or deleted.

Source schemas are *inferred* (column names and a type guess from the name; an
``id`` column is taken as the source's unique key and a ``*_at``/``ts`` column as
its event time). A verdict is therefore about the model on those assumed
sources, not on the real tables; ``--source-schema`` replaces the guess with
real columns. A ``diverges`` verdict always carries a counterexample that
replays; ``unknown`` means no divergence was found and no proof rule applies.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import binding_cte
from .incremental import (
    IncrementalError,
    SourceTable,
    check_incremental,
    parse_incremental_sqlx,
)
from .resilience import extended_path
from kumo_sql_prover.sqlx import split_sqlx_sections

CONTRACTS: dict[str, tuple[str, ...]] = {
    "append_only": ("insert_new", "empty"),
    "late_and_duplicate": ("insert_new", "insert_late", "duplicate", "empty"),
    "mutable": ("insert_new", "update", "delete", "empty"),
}

_TIME = re.compile(r"(^|_)(ts|timestamp|time|at)$|^(created|updated|modified|loaded|event)(_at|_time|_ts)?$")
_STRINGY = re.compile(r"(^|_)(name|status|type|category|country|email|label|code|currency|channel|region|segment)$")
_DATE = re.compile(r"(^|_)date$")
_BOOL = re.compile(r"^(is|has)_")


def guess_type(column: str) -> str:
    name = column.lower()
    if _TIME.search(name):
        return "TIMESTAMP"
    if _DATE.search(name):
        return "DATE"
    if _BOOL.match(name):
        return "BOOL"
    if _STRINGY.search(name):
        return "STRING"
    return "INT64"


@dataclass(frozen=True)
class ScanRow:
    model: str
    path: str
    contract: str
    outcome: str  # safe | diverges | unknown | unsupported | timeout
    rule: str
    detail: str = ""
    statements: int | None = None  # size of the counterexample


def infer_sources(model, overrides: dict[str, dict[str, str]] | None = None) -> dict[str, SourceTable]:
    """Source tables read by the full query, with the columns it uses."""

    tree = sqlglot.parse_one(model.full_sql, read="bigquery")
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = table.name
        if binding_cte(table) is not None or name.lower() == model.target.lower():
            continue
        aliases[(table.alias or name).lower()] = name
    columns: dict[str, dict[str, str]] = {name: {} for name in aliases.values()}
    single = next(iter(columns)) if len(columns) == 1 else None
    for column in tree.find_all(exp.Column):
        owner = aliases.get(column.table.lower()) if column.table else single
        if owner is not None:
            columns[owner].setdefault(column.name, guess_type(column.name))
    sources: dict[str, SourceTable] = {}
    for name, cols in columns.items():
        if overrides and name in overrides:
            cols = dict(overrides[name])
        if not cols:
            cols = {"id": "INT64"}
        time_column = next((c for c, t in cols.items() if t == "TIMESTAMP"), None)
        key = ("id",) if "id" in cols else ()
        sources[name] = SourceTable(cols, key, time_column)
    return sources


def _incremental_files(root: Path) -> list[Path]:
    root = extended_path(root)
    base = root / "definitions" if (root / "definitions").is_dir() else root
    found = []
    for path in sorted(base.rglob("*.sqlx")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\btype\s*:\s*[\"']incremental[\"']", text):
            found.append(path)
    return found


def _target_name(text: str, path: Path) -> str:
    for kind, body in split_sqlx_sections(text):
        if kind == "block" and body.lstrip().startswith("config"):
            named = re.search(r"\bname\s*:\s*[\"']([^\"']+)[\"']", body)
            if named:
                return named.group(1)
    return path.stem


def scan_project(
    root: str | Path,
    *,
    contracts: dict[str, tuple[str, ...]] | None = None,
    source_schema: dict[str, dict[str, str]] | None = None,
    seeds: int = 20,
    limit: int | None = None,
) -> list[ScanRow]:
    root = extended_path(root)
    contracts = contracts or CONTRACTS
    rows: list[ScanRow] = []
    files = _incremental_files(root)
    for path in files[:limit]:
        relative = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        target = _target_name(text, path)
        try:
            model = parse_incremental_sqlx(text, target)
            sources = infer_sources(model, source_schema)
        except (IncrementalError, sqlglot.errors.SqlglotError) as exc:
            for name in contracts:
                rows.append(ScanRow(target, relative, name, "unsupported", "parse", str(exc)[:120]))
            continue
        for name, kinds in contracts.items():
            verdict = check_incremental(model, sources, kinds, seeds=seeds, time_limit=20)
            rows.append(
                ScanRow(
                    target,
                    relative,
                    name,
                    verdict.outcome,
                    verdict.rule,
                    verdict.detail,
                    verdict.counterexample.size if verdict.counterexample else None,
                )
            )
    return rows


def summarise(rows: list[ScanRow]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        summary.setdefault(row.contract, {}).setdefault(row.outcome, 0)
        summary[row.contract][row.outcome] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check each Dataform incremental model against its full refresh")
    parser.add_argument("root", type=Path, help="Dataform project root")
    parser.add_argument("--source-schema", type=Path, help='JSON of source table columns, e.g. {"events": {"id": "INT64", "ts": "TIMESTAMP"}}')
    parser.add_argument("--seeds", type=int, default=20, help="Random change sequences tried per model and contract")
    parser.add_argument("--limit", type=int, help="Only the first N incremental models")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print("incremental-report: project folder not found", file=sys.stderr)
        return 2
    overrides = json.loads(args.source_schema.read_text(encoding="utf-8")) if args.source_schema else None
    rows = scan_project(args.root, source_schema=overrides, seeds=args.seeds, limit=args.limit)
    summary = summarise(rows)
    if args.json:
        print(json.dumps({"summary": summary, "rows": [r.__dict__ for r in rows]}, indent=1))
        return 0
    models = len({r.model for r in rows})
    print(f"{models} incremental models; source columns and keys are inferred, so read each answer as 'on those assumed sources'.")
    for name, counts in summary.items():
        print(f"  {name}: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    for row in rows:
        if row.outcome == "diverges":
            print(f"  diverges [{row.contract}] {row.model}: {row.detail} ({row.statements} source statements)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
