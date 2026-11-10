"""``python -m kumosql COMMAND ...``: every console command without a launcher.

Locked-down machines may block the ``.exe`` launchers pip creates, and the
Scripts folder is often not on ``PATH``. This runs the same entry points
through ``python`` itself, for example ``python -m kumosql ui`` for
``kumosql-ui``. A command can be given with or without its ``kumosql-`` prefix.
"""

from __future__ import annotations

import importlib
import sys

# Keep in step with [project.scripts] in pyproject.toml (tests/test_main_module.py checks).
COMMANDS = {
    "lift-subqueries": "kumosql.cli:main",
    "prove-sql-equivalent": "kumosql.cli:prove_main",
    "rewrite-sql": "kumosql.cli:rewrite_main",
    "kumosql-pipeline-report": "kumosql.cli:pipeline_main",
    "kumosql-dry-run": "kumosql.cli:dry_run_main",
    "prove-sql-smt": "kumosql.smt_equivalence:main",
    "prove-sql-sqlsolver": "kumosql.sqlsolver_backend:main",
    "prove-tables": "kumosql.pipeline_equivalence:main",
    "consolidate-tables": "kumosql.consolidate:main",
    "kumosql-equivalence": "kumosql.pipeline_equivalence:equivalence_main",
    "kumosql-refactor": "kumosql.refactor:main",
    "minimize-tables": "kumosql.table_minimizer:main",
    "reduce-project": "kumosql.project_reduction:main",
    "kumosql-shared-model": "kumosql.shared_models:main",
    "kumosql-profile-table": "kumosql.data_profile_cli:main",
    "kumosql-profile-mcp": "kumosql.profile_mcp:main",
    "kumosql-mcp": "kumosql.mcp_server:main",
    "kumosql-compare-outputs": "kumosql.cli:compare_outputs_main",
    "kumosql-ui": "kumosql.ui:main",
    "kumosql-scopes": "kumosql.cli:scopes_main",
    "kumosql-change-report": "kumosql.change_report:change_report_main",
    "kumosql-ci-check": "kumosql.ci_check:main",
    "kumosql-evidence-summary": "kumosql.cli:evidence_summary_main",
    "kumosql-workflow-configs": "kumosql.workflow_configs:main",
    "kumosql-smoke": "kumosql.smoke:main",
    "kumosql-incremental-report": "kumosql.incremental_scan:main",
}


def resolve(name: str) -> str | None:
    """The entry point for ``name``, accepting the short form without ``kumosql-``."""

    if name in COMMANDS:
        return COMMANDS[name]
    return COMMANDS.get(f"kumosql-{name}")


def usage() -> str:
    lines = ["usage: python -m kumosql COMMAND [ARGS...]", "", "commands:"]
    lines += [f"  {name}" for name in COMMANDS]
    lines += ["", "Run `python -m kumosql COMMAND --help` for a command's options. --help only prints text; it never does any work.",
              "Most commands only read and print (consolidate-tables, refactor, minimize-tables, prove-*). A command writes",
              "a file only for an option you give it: -o/--output/--patch/--csv write the file you name; reduce-project",
              "--write is the only option that edits a project folder.",
              "The kumosql- prefix is optional: `python -m kumosql ui` runs kumosql-ui."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(usage())
        return 0
    target = resolve(args[0])
    if target is None:
        print(f"kumosql: unknown command {args[0]!r}\n\n{usage()}", file=sys.stderr)
        return 2
    module, _, attribute = target.partition(":")
    function = getattr(importlib.import_module(module), attribute)
    sys.argv = [f"python -m kumosql {args[0]}", *args[1:]]
    result = function()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
