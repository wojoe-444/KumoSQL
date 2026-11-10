"""``python -m kumosql COMMAND ...``: every console command without a launcher.

Locked-down machines may block the ``.exe`` launchers pip creates, and the
Scripts folder is often not on ``PATH``. This runs the same entry points
through ``python`` itself, for example ``python -m kumosql ui`` for
``kumosql-ui``. A command can be given with or without its ``kumosql-`` prefix.
"""

from __future__ import annotations

import importlib
import sys

def _declared_commands() -> dict[str, str]:
    """The console commands, read from the one place they are declared: ``[project.scripts]`` in pyproject.toml.

    A source checkout reads that file, so a command added to it works at once, even when an older editable install
    left stale metadata. An installed package reads its own installed entry points. Nothing here is a second list."""

    import tomllib
    from importlib import metadata
    from pathlib import Path

    toml = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(toml.read_text(encoding="utf-8")).get("project", {})
        if project.get("name") == "kumosql" and project.get("scripts"):
            return dict(project["scripts"])
    except (OSError, ValueError):
        pass
    return {
        entry.name: entry.value
        for entry in metadata.entry_points(group="console_scripts")
        if entry.dist is not None and entry.dist.name.lower() == "kumosql"
    }


COMMANDS = _declared_commands()


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
