"""Command-line interface for the SQL transformer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from kumo_sql_prover.lift_subqueries import lift_subqueries
from kumo_sql_prover.statement_proof import prove_statements
from .rewrite import apply_rules, attach_planner_check, available_rules, check_idempotence
from kumo_sql_prover.sqlx import looks_like_sqlx
from .synthetic_check import attach_synthetic_check
from .evidence_summary import DEFAULT_MIN_CHANGED, summarize_evidence
from .dryrun import check_rewrite, dry_run
from .fingerprint import Location, compare_snapshots, plan_output_comparison, summarize_comparison
from .scopes import Scope, delete_scope, discover_fields, get_scope, list_scopes, parse_scope, save_scope
from .coverage import Thresholds, sample_impact_reports, score_verdicts
from .pipeline import load_compiled_graph, load_sqlx_project
from .resilience import PipelineLoadError, parse_json_or_raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lift BigQuery FROM/JOIN subqueries into CTEs")
    parser.add_argument("input", type=Path, help="Input SQL file")
    parser.add_argument("-o", "--output", type=Path, help="Output SQL file; stdout if omitted")
    parser.add_argument("--report", action="store_true", help="Print transformation summary to stderr")
    args = parser.parse_args(argv)

    result = lift_subqueries(args.input.read_text(encoding="utf-8"))
    if args.report:
        print(
            f"statements={result.statements} transformed={result.transformed_statements} "
            f"lifted={result.lifted_subqueries} remaining={result.remaining_inline_subqueries} "
            f"diagnostics={len(result.diagnostics)}",
            file=sys.stderr,
        )
    if args.report or not result.success:
        for diagnostic in result.diagnostics:
            print(f"diagnostic: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)

    if not result.success:
        return 2

    if args.output:
        args.output.write_text(result.sql + ("\n" if result.sql else ""), encoding="utf-8")
    else:
        sys.stdout.write(result.sql)
        if result.sql:
            sys.stdout.write("\n")

    return 0


def prove_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conservatively prove two BigQuery queries equivalent")
    parser.add_argument("left", type=Path, help="Left SQL file")
    parser.add_argument("right", type=Path, help="Right SQL file")
    parser.add_argument(
        "--respect-row-order",
        action="store_true",
        help="Also require the same ORDER BY on both queries (rows tied on its keys may still come back in either order)",
    )
    parser.add_argument("--verifier-sql", type=Path, help="Write generated SQL that compares the result bags (not their order)")
    parser.add_argument(
        "--conditional",
        action="store_true",
        help="When the pair is not proven, look for facts (NOT NULL, unique keys, foreign keys) that would make it equivalent; exit 3",
    )
    parser.add_argument("--schema", type=Path, help="JSON file mapping table names to column lists (for --conditional)")
    parser.add_argument(
        "--explain-difference",
        action="store_true",
        help="When the pair is not proven, print a verified predicate P such that the queries are equivalent except when P holds",
    )
    args = parser.parse_args(argv)

    left_sql = args.left.read_text(encoding="utf-8")
    right_sql = args.right.read_text(encoding="utf-8")
    result = prove_statements(left_sql, right_sql, ignore_row_order=not args.respect_row_order)
    if args.conditional and not result.proven and not args.respect_row_order:
        from kumo_sql_prover.smt_equivalence import SmtStatus
        from kumo_sql_prover.statement_proof import prove_statements_smt

        schema = json.loads(args.schema.read_text(encoding="utf-8")) if args.schema else None
        conditional = prove_statements_smt(left_sql, right_sql, conditional=True, schema=schema)
        if conditional.status is SmtStatus.PROVEN_CONDITIONALLY:
            print(conditional.status.value)
            print(conditional.reason)
            for condition in conditional.conditions:
                print(f"condition: {condition.text}")
                print(f"check: {condition.check_sql}")
            for assumption in conditional.assumptions:
                print(f"diagnostic: assumption: {assumption}")
            return 3
    print(result.status.value)
    print(result.reason)
    for diagnostic in result.diagnostics:
        print(f"diagnostic: {diagnostic}")
    if args.explain_difference and not result.proven and not args.respect_row_order:
        from kumo_sql_prover.difference_surface import describe, except_when

        found = except_when(left_sql, right_sql)
        if found is not None:
            for line in describe(found):
                print(line)
    if args.verifier_sql and result.verifier_sql:
        args.verifier_sql.write_text(result.verifier_sql + "\n", encoding="utf-8")
    return 0 if result.proven else 2


def evidence_summary_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the anonymized share of changed outputs with useful evidence"
    )
    parser.add_argument("paths", nargs="+", type=Path, help=".sql or .sqlx files, or directories searched recursively")
    parser.add_argument(
        "--rule", action="append", choices=sorted(available_rules()), required=True,
        help="Rule to apply to every file; repeat to apply several in order",
    )
    parser.add_argument(
        "--min-changed", type=int, default=DEFAULT_MIN_CHANGED,
        help="Withhold percentages below this many changed outputs",
    )
    args = parser.parse_args(argv)

    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix in (".sql", ".sqlx")))
        else:
            files.append(path)
    if not files:
        print("error: no .sql or .sqlx files found", file=sys.stderr)
        return 2
    results = [apply_rules(args.rule, f.read_text(encoding="utf-8")) for f in files]
    # Only the aggregate is printed: no file names, SQL text or reasons.
    print(json.dumps(summarize_evidence(results, min_changed=args.min_changed).to_json(), indent=2))
    return 0


def rewrite_main(argv: list[str] | None = None) -> int:
    rules = available_rules()
    parser = argparse.ArgumentParser(
        description="Apply registered rewrite rules and verify each output for equivalence",
        epilog="Rules: " + "; ".join(f"{name}: {rule.summary}" for name, rule in rules.items()),
    )
    parser.add_argument("input", type=Path, help="Input SQL or SQLX file")
    parser.add_argument(
        "-r",
        "--rule",
        action="append",
        choices=sorted(rules),
        required=True,
        help="Rule to apply; repeat to apply several in order",
    )
    parser.add_argument("-o", "--output", type=Path, help="Output file; stdout if omitted")
    parser.add_argument(
        "--allow-unproven",
        action="store_true",
        help="Exit 0 after writing output when it is unproven or only planner checked",
    )
    parser.add_argument(
        "--check-idempotence",
        action="store_true",
        help="Also run the rules on their own output and exit 4 if it changes again",
    )
    parser.add_argument(
        "--planner-project",
        help="Opt in to a BigQuery planning and output-schema check (credentials required)",
    )
    parser.add_argument("--planner-location", help="BigQuery job location for the opt-in planner check")
    parser.add_argument(
        "--planner-compiled-original",
        type=Path,
        help="Compiled SQL for the original SQLX input",
    )
    parser.add_argument(
        "--planner-compiled-rewritten",
        type=Path,
        help="Compiled SQL for the rewritten SQLX output",
    )
    parser.add_argument(
        "--synthetic-check",
        action="store_true",
        help="Opt in to a local DuckDB comparison on synthetic data (needs --synthetic-schema); "
        "agreement is evidence, not proof",
    )
    parser.add_argument(
        "--synthetic-schema",
        type=Path,
        help='JSON mapping of source tables to columns, e.g. {"p.d.t": {"id": "INT64"}}',
    )
    parser.add_argument(
        "--synthetic-seeds",
        type=int,
        default=8,
        help="Number of synthetic datasets (seeds 0..N-1) for --synthetic-check",
    )
    args = parser.parse_args(argv)

    if args.synthetic_check and not args.synthetic_schema:
        parser.error("--synthetic-check requires --synthetic-schema")
    if (args.synthetic_schema or args.synthetic_seeds != 8) and not args.synthetic_check:
        parser.error("--synthetic-schema and --synthetic-seeds require --synthetic-check")
    if args.synthetic_seeds < 1:
        parser.error("--synthetic-seeds must be at least 1")
    if (args.planner_location or args.planner_compiled_original or args.planner_compiled_rewritten) and not args.planner_project:
        parser.error("--planner-location and compiled SQL inputs require --planner-project")
    if bool(args.planner_compiled_original) != bool(args.planner_compiled_rewritten):
        parser.error("provide both --planner-compiled-original and --planner-compiled-rewritten")

    result = apply_rules(args.rule, args.input.read_text(encoding="utf-8"))
    if args.planner_project:
        result = attach_planner_check(
            result,
            args.planner_project,
            location=args.planner_location,
            compiled_original_sql=(
                args.planner_compiled_original.read_text(encoding="utf-8")
                if args.planner_compiled_original
                else None
            ),
            compiled_rewritten_sql=(
                args.planner_compiled_rewritten.read_text(encoding="utf-8")
                if args.planner_compiled_rewritten
                else None
            ),
        )
    if args.synthetic_check:
        try:
            synthetic_schema = json.loads(args.synthetic_schema.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: cannot read --synthetic-schema: {exc}", file=sys.stderr)
            return 2
        result = attach_synthetic_check(
            result, synthetic_schema, seeds=range(args.synthetic_seeds)
        )

    for step in result.steps:
        print(
            f"{step.rule}: changes={step.changes} "
            f"verification={step.verification.status.value}",
            file=sys.stderr,
        )
        for diagnostic in step.diagnostics:
            print(f"  diagnostic: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        for detail in step.verification.details:
            print(f"  detail: {detail}", file=sys.stderr)
    print(f"verification={result.verification.status.value}", file=sys.stderr)
    for check in result.verification.checks:
        print(
            f"  check {check.kind}={check.outcome}: {check.detail}",
            file=sys.stderr,
        )
        for key, value in check.evidence:
            if key == "scope":
                continue
            if key == "estimated_bytes_delta":
                if value is not None:
                    print(f"    estimated bytes delta: {value} bytes (estimate)", file=sys.stderr)
            elif key == "schema_differences":
                for difference in value:
                    print(f"    schema difference: {difference}", file=sys.stderr)
            elif check.kind == "synthetic_results" and key in ("seeds", "seeds_checked"):
                print(f"    {key}={list(value)}", file=sys.stderr)
            else:
                print(f"    {key}={value}", file=sys.stderr)

    if not all(step.rule_success for step in result.steps):
        return 2

    if args.output:
        args.output.write_text(result.sql + ("\n" if result.sql else ""), encoding="utf-8")
    else:
        sys.stdout.write(result.sql)
        if result.sql:
            sys.stdout.write("\n")

    if args.check_idempotence:
        again = check_idempotence(args.rule, args.input.read_text(encoding="utf-8"))
        if again.idempotent:
            print("idempotence=passed", file=sys.stderr)
        else:
            print(
                "idempotence=failed: running the rules on their own output changed it again"
                + (f" ({', '.join(again.rules_that_changed)})" if again.rules_that_changed else ""),
                file=sys.stderr,
            )
            return 4

    if not result.verification.trusted:
        if args.allow_unproven:
            print(
                "warning: output is not proven equivalent; accepted by --allow-unproven",
                file=sys.stderr,
            )
            return 0
        return 3
    return 0


def pipeline_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse a whole Dataform or SQL pipeline: graph, dead columns, duplicate logic"
    )
    parser.add_argument("root", type=Path, nargs="?", help="Dataform project root, folder of .sql files, or compiled graph JSON")
    parser.add_argument("--git", metavar="URL", help="Instead of a folder: clone this repository with the local git CLI (https://, ssh://, git@host:path)")
    parser.add_argument("--branch", help="With --git: branch to load")
    parser.add_argument("--refresh", action="store_true", help="With --git: fetch the latest commit instead of reusing the cached clone")
    parser.add_argument(
        "--source-schema",
        type=Path,
        help='JSON mapping of source tables to columns, e.g. {"p.d.t": {"id": "INT64"}}',
    )
    parser.add_argument(
        "--fetch-schema",
        action="store_true",
        help="Look up columns of tables the project does not define in BigQuery (off by default: nothing reaches the network otherwise)",
    )
    parser.add_argument("--min-nodes", type=int, default=12, help="Smallest SELECT subtree to report as a duplicate")
    parser.add_argument(
        "--similarity",
        type=float,
        default=0.7,
        help="Smallest tree similarity (0-1) for SELECTs to be reported as near-duplicates",
    )
    parser.add_argument("--scope", help="Limit the report to a saved scope (see python -m kumosql scopes)")
    parser.add_argument(
        "--assess",
        choices=("drop_column", "rename_column", "change_expression", "drop_table"),
        help="Instead of the report, list the models a change would affect (needs --target)",
    )
    parser.add_argument(
        "--observed-reads",
        type=Path,
        help="With --assess: JSON list of job-history records (job_id, creation_time, destination, referenced_tables); "
        "readers seen only there are listed under observed",
    )
    parser.add_argument(
        "--target",
        help="Table for drop_table, or table.column for the column changes, e.g. proj.dataset.table.column",
    )
    parser.add_argument(
        "--verdicts",
        type=Path,
        help='JSON of reviewer verdicts by sample id, e.g. {"ab12": {"correct": 4, "false_positive": 1, "missed": 0}}',
    )
    parser.add_argument(
        "--sample-impact", type=int, metavar="N", help="Write a review sheet of N sampled impact reports and exit"
    )
    parser.add_argument("--seed", default="kumosql", help="Seed for --sample-impact")
    parser.add_argument(
        "--min-coverage",
        type=float,
        help="Exit 3 if assets analyzed or statements matched fall below this ratio (0-1), or analysis is incomplete",
    )
    for flag, what in (
        ("min-columns-traced", "output columns traced"),
        ("min-accuracy", "sampled impact accuracy"),
        ("min-precision", "sampled precision"),
        ("min-recall", "sampled recall"),
    ):
        parser.add_argument(f"--{flag}", type=float, help=f"Release gate: minimum ratio (0-1) of {what}; exit 3 below it")
    parser.add_argument("--min-sample-size", type=int, help="Release gate: minimum number of reviewed impact reports")
    parser.add_argument(
        "--require-complete", action="store_true", help="Release gate: fail while any blocking gap exists"
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
        help='JSON of release thresholds, e.g. {"min_columns_traced": 0.95, "require_complete": true}; flags override it',
    )
    parser.add_argument(
        "--boundary",
        type=Path,
        metavar="FILE",
        help="YAML or JSON file naming the jobs outside the repository that build sources (producers) and the semantic models and "
        "dashboards that read outputs (consumers), as for python -m kumosql mcp. Adds a boundary section to the report (or to the --assess "
        "result): producer SQL as models outside the project and the readers outside the project. Nothing it names is contacted.",
    )
    parser.add_argument("-o", "--output", type=Path, help="Write the JSON report here; stdout if omitted")
    args = parser.parse_args(argv)
    if args.fetch_schema:
        from . import schema_fetch

        os.environ[schema_fetch.ENV] = "1"
    if args.assess and not args.target:
        parser.error("--assess needs --target")
    if bool(args.root) == bool(args.git):
        parser.error("give either a project folder or --git URL")
    if (args.branch or args.refresh) and not args.git:
        parser.error("--branch and --refresh need --git")
    if args.git:
        from .git_repo import GitRepoError, sync, parse_remote, parse_branch

        try:
            args.root = sync(parse_remote(args.git), parse_branch(args.branch), args.refresh)
        except GitRepoError as exc:
            parser.error(str(exc))

    boundary_spec = None
    if args.boundary:
        from .boundary import BoundaryError, read_file

        try:
            boundary_spec = read_file(args.boundary)  # the same checks, and messages, as python -m kumosql mcp
        except BoundaryError as exc:
            parser.error(f"--boundary: {exc}")

    scope = None
    if args.scope:
        scope = get_scope(args.scope)
        if scope is None:
            parser.error(f"no saved scope named {args.scope!r}")
    try:
        pipeline = _load_pipeline(args.root, args.source_schema)
    except PipelineLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    boundary = boundary_session = None
    if boundary_spec is not None:
        from . import boundary_report
        from .boundary import BoundaryError

        try:
            boundary, boundary_session = boundary_report.load(pipeline, boundary_spec)
        except BoundaryError as exc:  # a producer table the project does not have, or builds itself
            parser.error(f"--boundary: {exc}")
    if args.assess:
        table, column = args.target, None
        if args.assess != "drop_table":
            table, _, column = args.target.rpartition(".")
            if not table or not column:
                parser.error("--target must be table.column for column changes")
        observed_reads = []
        if args.observed_reads:
            try:
                observed_reads = json.loads(args.observed_reads.read_text(encoding="utf-8"))
                if not isinstance(observed_reads, list):
                    raise ValueError("expected a JSON list")
            except (OSError, ValueError) as exc:
                parser.error(f"could not read --observed-reads: {exc}")
        try:
            data = pipeline.assess_change(
                args.assess, table, column, scope=scope, observed_reads=observed_reads
            ).to_json()
        except ValueError as exc:
            parser.error(str(exc))
        if boundary is not None:
            data["boundary"] = boundary_report.assess_section(
                boundary, boundary_session, args.assess, table, column, file=args.boundary
            )
        if args.fetch_schema:
            from . import schema_fetch

            print(schema_fetch.summary(pipeline._analyse().schema_lookup), file=sys.stderr)
        text = json.dumps(data, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0
    verdicts = None
    if args.verdicts:
        try:
            verdicts = json.loads(args.verdicts.read_text(encoding="utf-8"))
            score_verdicts(verdicts)
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            parser.error(f"unreadable verdicts file: {exc}")
    thresholds = _cli_thresholds(parser, args)
    try:
        data = pipeline.report(
            min_nodes=args.min_nodes, similarity=args.similarity, scope=scope, verdicts=verdicts, thresholds=thresholds
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.sample_impact is not None:
        sheet = json.dumps(sample_impact_reports(data, args.sample_impact, args.seed), indent=2)
        if args.output:
            args.output.write_text(sheet + "\n", encoding="utf-8")
        else:
            print(sheet)
        return 0
    if boundary is not None:
        data["boundary"] = boundary_report.report_section(
            boundary,
            boundary_session,
            keep=None if scope is None else pipeline.scope_keys(scope),
            file=args.boundary,
        )
    if scope is not None and not data["models"]:
        print(f"warning: scope {scope.name!r} matches no models", file=sys.stderr)
    if args.fetch_schema:
        from . import schema_fetch

        print(schema_fetch.summary(data.get("schema_lookup") or {}), file=sys.stderr)
    summary = data.get("diagnostic_summary", {})
    if summary.get("assets_not_analyzed"):
        count = summary["assets_not_analyzed"]
        print(
            f"warning: {count} asset{'s' if count != 1 else ''} could not be analyzed; "
            "see diagnostics in the report",
            file=sys.stderr,
        )
    report = json.dumps(data, indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    if thresholds is not None:
        gate = (data.get("coverage") or {}).get("gate")
        if gate is None or not gate["passed"]:
            failed = ", ".join(gate["failed"]) if gate else "coverage could not be computed"
            print(f"error: release gate failed ({failed})", file=sys.stderr)
            return 3
    return 0


def _cli_thresholds(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Thresholds | None:
    """Thresholds from ``--thresholds`` and the gate flags; ``None`` when no gate was asked for.

    ``--min-coverage R`` is shorthand for minimum assets analyzed and
    statements matched of R, plus a complete analysis.
    """

    values: dict = {}
    if args.thresholds:
        try:
            loaded = Thresholds.from_json(json.loads(args.thresholds.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            parser.error(f"unreadable thresholds file: {exc}")
        values = {k: v for k, v in loaded.to_json().items() if v is not None and v is not False}
    if args.min_coverage is not None:
        values.update(
            min_assets_analyzed=args.min_coverage, min_statements_matched=args.min_coverage, require_complete=True
        )
    for flag in ("min_columns_traced", "min_accuracy", "min_precision", "min_recall", "min_sample_size"):
        if getattr(args, flag) is not None:
            values[flag] = getattr(args, flag)
    if args.require_complete:
        values["require_complete"] = True
    if not values:
        return None
    try:
        return Thresholds.from_json(values)
    except ValueError as exc:
        parser.error(str(exc))


def scopes_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage saved scopes: named rules over models, job history and table profiles"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show saved scopes")
    add = commands.add_parser(
        "add",
        help="Create or replace a scope",
        description="Give the scope a rule with --rule/--rule-file (any conditions, nested AND/OR/NOT), "
        "or a simple 'field in values' filter with --field.",
    )
    add.add_argument("name", help='Scope name, e.g. "My Team"')
    add.add_argument(
        "--field",
        action="append",
        nargs="+",
        metavar=("FIELD", "VALUE"),
        help="A field and its values, e.g. --field author ana@co.com bo@co.com (end a value with * for a prefix)",
    )
    add.add_argument(
        "--rule",
        help='Rule as JSON, e.g. \'{"field": "submitter", "op": "in", "value": ["ana@co.com"]}\'; '
        'groups are {"all": [...]}, {"any": [...]} and {"not": rule}',
    )
    add.add_argument("--rule-file", type=Path, help="Read the rule JSON from a file")
    add.add_argument(
        "--applies-to",
        nargs="+",
        metavar="DOMAIN",
        help="Data domains the scope applies to: models, jobs, bigquery (default: the ones its fields fit)",
    )
    remove = commands.add_parser("remove", help="Delete a scope")
    remove.add_argument("name")
    refresh = commands.add_parser(
        "refresh",
        help="Run or refresh the SQL query conditions of a scope (all scopes if none is named)",
        description="Runs each 'is returned by SQL query' condition now, ignoring the kept result. "
        "Queries are capped by the byte limit in Settings; --check only dry-runs them (free).",
    )
    refresh.add_argument("name", nargs="?", help="Scope name; every scope with query conditions if omitted")
    refresh.add_argument("--check", action="store_true", help="Dry-run only: validate and estimate, run nothing")
    fields = commands.add_parser("fields", help="List the fields a rule can use, found in your data")
    fields.add_argument("--root", type=Path, help="Dataform project (or compiled graph) to read model and profile fields from")
    fields.add_argument("--source-schema", type=Path, help="Source schema JSON, as for python -m kumosql pipeline-report")
    fields.add_argument("--observed-reads", type=Path, help="Job-history JSON list to read job fields from")
    args = parser.parse_args(argv)

    if args.command == "list":
        for scope in list_scopes():
            print(json.dumps(scope.to_json()))
    elif args.command == "refresh":
        from . import scope_queries

        chosen = [get_scope(args.name)] if args.name else list_scopes()
        if args.name and chosen[0] is None:
            parser.error(f"no saved scope named {args.name!r}")
        failed = False
        for scope in chosen:
            try:
                nodes = scope_queries.query_nodes(scope.expanded_rule())
            except ValueError as exc:
                parser.error(str(exc))
            for node in nodes:
                label = f"{scope.name}: {node['field']}"
                try:
                    if args.check:
                        plan = scope_queries.dry_run(node["query"], node.get("column"))
                        print(f"{label}: ok, returns {', '.join(plan['columns'])}; about {plan['estimated_bytes']} bytes")
                    else:
                        result = scope_queries.result_for(node["query"], node.get("column"), refresh=True)
                        note = f" (re-run failed, kept the older copy: {result.error})" if result.error else ""
                        print(f"{label}: {len(result.values)} values{note}")
                except ValueError as exc:
                    failed = True
                    print(f"{label}: {exc}", file=sys.stderr)
        return 1 if failed else 0
    elif args.command == "fields":
        pipeline = None
        if args.root:
            try:
                pipeline = _load_pipeline(args.root, args.source_schema)
            except PipelineLoadError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        reads: list = []
        if args.observed_reads:
            try:
                reads = json.loads(args.observed_reads.read_text(encoding="utf-8"))
                if not isinstance(reads, list):
                    raise ValueError("expected a JSON list")
            except (OSError, ValueError) as exc:
                parser.error(f"could not read --observed-reads: {exc}")
        profiles = None
        if pipeline is not None:
            from .table_profile import profile_pipeline

            profiles = profile_pipeline(pipeline)
        for info in discover_fields(pipeline, reads, profiles):
            examples = f"  e.g. {', '.join(info.examples[:3])}" if info.examples else ""
            print(f"{info.name}\t{info.source}\t{info.kind}{examples}")
    elif args.command == "add":
        given = [bool(args.field), bool(args.rule), bool(args.rule_file)]
        if sum(given) != 1:
            parser.error("give exactly one of --field, --rule or --rule-file")
        try:
            if args.field:
                filters: dict[str, list[str]] = {}
                for field, *values in args.field:
                    filters.setdefault(field, []).extend(values)
                data = {"name": args.name, "fields": filters}
            else:
                text = args.rule if args.rule else args.rule_file.read_text(encoding="utf-8")
                data = {"name": args.name, "rule": json.loads(text)}
            if args.applies_to:
                data["applies_to"] = args.applies_to
            save_scope(parse_scope(data))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"could not read the rule: {exc}")
        except ValueError as exc:
            parser.error(str(exc))
    elif not delete_scope(args.name):
        parser.error(f"no saved scope named {args.name!r}")
    return 0


def dry_run_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check BigQuery query planning and output schemas without comparing query results"
    )
    parser.add_argument("sql", type=Path, help="SQL file (the original, when --rewritten is given)")
    parser.add_argument("--rewritten", type=Path, help="Rewritten SQL file to compare against")
    parser.add_argument("--project", required=True, help="Project to run the dry run in")
    parser.add_argument("--location", help="Job location, e.g. US or EU")
    args = parser.parse_args(argv)

    sql = args.sql.read_text(encoding="utf-8")
    if args.rewritten:
        rewritten_sql = args.rewritten.read_text(encoding="utf-8")
        if looks_like_sqlx(sql) or looks_like_sqlx(rewritten_sql):
            print("planner_check=not_run: compile both SQLX inputs before planning")
            print("scope=planning and output-schema comparison only; results were not compared")
            return 2
        check = check_rewrite(
            sql, rewritten_sql, args.project, location=args.location
        )
        print(f"planner_check={check.outcome}")
        print(check.reason)
        print(f"original_planned={check.original_planned}")
        print(f"rewritten_planned={check.rewritten_planned}")
        print(f"schema_matches={'unknown' if check.schema_matches is None else check.schema_matches}")
        print("scope=planning and output-schema comparison only; results were not compared")
        for difference in check.schema_differences:
            print(f"schema: {difference}")
        if check.estimated_bytes_delta is not None:
            print(f"estimated_bytes_delta={check.estimated_bytes_delta} (estimate)")
        return 0 if check.planned_same_schema else 2

    if looks_like_sqlx(sql):
        print("query_plan=not_run: compile SQLX before planning")
        return 2
    result = dry_run(sql, args.project, location=args.location)
    if not result.ok:
        print(f"query_plan=failed: {result.error_message}")
        return 2
    print("query_plan=passed")
    print("scope=planning only; query results were not compared")
    print(f"estimated_bytes={result.total_bytes_processed} (estimate)")
    if not result.schema_observed:
        print("schema=unknown: the dry run returned no output schema")
    for field in result.schema:
        print(field.describe())
    return 0


def _load_pipeline(root: Path, source_schema_path: Path | None):
    source_schema = (
        parse_json_or_raise(source_schema_path, "source schema file") if source_schema_path else None
    )
    if source_schema is not None and not isinstance(source_schema, dict):
        raise PipelineLoadError("source schema file must be a JSON object")
    if root.is_file():
        return load_compiled_graph(root, source_schema=source_schema)
    return load_sqlx_project(root, source_schema=source_schema)


def compare_outputs_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate BigQuery SQL comparing every model's output before and after a refactor"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def planned(name: str, help_text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("root", type=Path, help="Pipeline before the refactor (project, folder or graph JSON)")
        command.add_argument("--after-root", type=Path, help="Pipeline after the refactor, if its code differs")
        command.add_argument("--source-schema", type=Path, help="JSON mapping of source tables to columns")
        for side in ("before", "after"):
            command.add_argument(f"--{side}-project", help=f"Project of the {side} tables")
            command.add_argument(f"--{side}-dataset", help=f"Dataset of the {side} tables")
            command.add_argument(f"--{side}-dataset-suffix", default="", help="Appended to each dataset")
            command.add_argument(f"--{side}-table-prefix", default="", help="Prepended to each table name")
            command.add_argument(f"--{side}-table-suffix", default="", help="Appended to each table name")
        command.add_argument("--models", nargs="+", help="Only these models")
        command.add_argument("--ignore-column", action="append", default=[], help="Skip this column everywhere")
        command.add_argument(
            "--normalize",
            action="append",
            default=[],
            metavar="COLUMN=TEMPLATE",
            help='SQL applied to a column on both sides, e.g. "amount=ROUND({col}, 6)"',
        )
        command.add_argument(
            "--where", action="append", default=[], metavar="MODEL=PREDICATE", help="Filter a model's scans"
        )
        return command

    planned("compare", "one query comparing every model's fingerprints")
    fingerprint = planned("fingerprint", "one query fingerprinting one side, to save and compare later")
    fingerprint.add_argument("--side", choices=["before", "after"], default="before")
    drilldown = planned("drilldown", "the rows that differ for one model")
    drilldown.add_argument("--model", required=True)
    drilldown.add_argument("--keys", help="Comma-separated key columns")
    drilldown.add_argument("--columns", help="Comma-separated columns to compare (default: all)")
    drilldown.add_argument("--limit", type=int, default=100)
    summarize = commands.add_parser(
        "summarize", help="summarise query results exported as JSON (e.g. bq query --format=json)"
    )
    summarize.add_argument("results", type=Path, help="Results of the compare query, or the before fingerprints")
    summarize.add_argument("after", type=Path, nargs="?", help="The after fingerprints, when comparing snapshots")
    args = parser.parse_args(argv)

    if args.command == "summarize":
        first = json.loads(args.results.read_text(encoding="utf-8"))
        if args.after:
            diffs = compare_snapshots(first, json.loads(args.after.read_text(encoding="utf-8")))
        else:
            diffs = summarize_comparison(first)
        if not diffs:
            print("error: the results hold no comparison rows, so nothing was compared", file=sys.stderr)
            return 2
        for diff in diffs:
            print(f"{diff.model}: {diff.status}" + (f" ({diff.note})" if diff.note else ""))
        return 0 if all(diff.matches for diff in diffs) else 1

    def location(side: str) -> Location:
        return Location(
            getattr(args, f"{side}_project"),
            getattr(args, f"{side}_dataset"),
            getattr(args, f"{side}_dataset_suffix"),
            getattr(args, f"{side}_table_prefix"),
            getattr(args, f"{side}_table_suffix"),
        )

    def pairs(values: list[str], flag: str) -> dict[str, str]:
        result = {}
        for value in values:
            name, sep, rest = value.partition("=")
            if not sep:
                parser.error(f"{flag} expects NAME=VALUE, got {value!r}")
            result[name] = rest
        return result

    try:
        before = _load_pipeline(args.root, args.source_schema)
        after = _load_pipeline(args.after_root, args.source_schema) if args.after_root else None
    except PipelineLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        plan = plan_output_comparison(
            before,
            after,
            before_location=location("before"),
            after_location=location("after"),
            models=args.models,
            normalize=pairs(args.normalize, "--normalize"),
            ignore_columns=args.ignore_column,
            where=pairs(args.where, "--where"),
        )
        if args.command == "compare":
            sql = plan.compare_sql()
        elif args.command == "fingerprint":
            sql = plan.fingerprint_sql(args.side)
        else:
            sql = plan.drilldown_sql(
                args.model,
                keys=_split(args.keys),
                columns=_split(args.columns) if args.columns else None,
                limit=args.limit,
            )
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for diagnostic in plan.diagnostics:
        print(f"diagnostic: {diagnostic.model}: {diagnostic.code}: {diagnostic.message}", file=sys.stderr)
    print(sql)
    return 0


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
