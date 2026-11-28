"""``python -m kumosql workbook show|find|tables|patch FILE ...``: Tableau workbooks as text, and patches checked by re-parsing.

READ-ONLY unless --apply or --output is given: ``show``, ``find`` and ``tables`` only print, and ``patch`` prints the
verified change as a diff. ``patch ... --apply`` writes the workbook file you name (atomically, after checking it did not
change since it was read); ``--output PATH`` writes a patched copy instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (DEFAULT_MAX_CHARS, WorkbookError, find_field, find_table, open_workbook, render_find, render_text,
               tables_read)
from . import patch as P

EXIT_REFUSED = 3
EXIT_ERROR = 2
EXIT_NOT_FOUND = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql workbook",
        description="Tableau workbooks (.twb, .twbx) as compact text, one-call questions about fields and tables, and "
                    "patches that are checked by parsing the result again. READ-ONLY unless --apply or --output is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python -m kumosql workbook show sales.twb\n"
               "  python -m kumosql workbook find sales.twb \"Profit Ratio\"\n"
               "  python -m kumosql workbook tables sales.twbx --json\n"
               "  python -m kumosql workbook patch sales.twb rename-field \"Profit Ratio\" \"Margin %\"          (preview)\n"
               "  python -m kumosql workbook patch sales.twb rename-field \"Profit Ratio\" \"Margin %\" --apply  (writes)\n")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    show = commands.add_parser("show", help="the workbook as text: data sources, calculated fields, sheets, dashboards",
                               description="Print the workbook as compact text (or JSON): data sources with their "
                                           "connections, tables and custom SQL, calculated fields with their formulas in "
                                           "captions, parameters, worksheets with the fields and filters they use, "
                                           "dashboards, and anything that does not add up. Only prints.")
    show.add_argument("file", type=Path, help="a .twb or .twbx file")
    show.add_argument("--json", action="store_true", help="print the structured view as JSON (not capped)")
    show.add_argument("--sheet", help="only this worksheet")
    show.add_argument("--datasource", help="only this data source (caption or internal name) and the sheets that use it")
    show.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                      help=f"cut the text at this many characters and say what was cut (default {DEFAULT_MAX_CHARS:,})")
    show.add_argument("--sql-lines", type=int, default=30, help="lines of each custom SQL query to show (default 30)")

    find = commands.add_parser("find", help="which sheets use a field, and its formula; or who reads a warehouse table",
                               description="Answer \"which sheets use field X, and what is its formula?\" in one call: the "
                                           "formula (in captions and as stored), the fields it reads, the calculated fields "
                                           "that read it, the sheets that use it directly or through other calculated "
                                           "fields, the filters on it and the dashboards. With --table, the data sources, "
                                           "sheets and dashboards that read a warehouse table. Only prints; exits 1 when "
                                           "nothing matches.")
    find.add_argument("file", type=Path, help="a .twb or .twbx file")
    find.add_argument("field", nargs="?", help="a field or parameter: its caption or internal name, brackets optional")
    find.add_argument("--table", help="a warehouse table instead: table, dataset.table or project.dataset.table")
    find.add_argument("--datasource", help="look in this data source only")
    find.add_argument("--json", action="store_true", help="print JSON")

    tables = commands.add_parser("tables", help="the warehouse tables each data source reads (for lineage)",
                                 description="List the warehouse tables each data source reads: its connection tables and "
                                             "the tables named in its custom SQL, parsed with sqlglot (BigQuery dialect). "
                                             "Custom SQL that does not parse is listed as unknown, never guessed. Only prints.")
    tables.add_argument("file", type=Path, help="a .twb or .twbx file")
    tables.add_argument("--json", action="store_true", help="print JSON")

    patch = commands.add_parser("patch", help="change a workbook; previews a verified diff unless --apply or --output",
                                description="Plan a change, apply it in memory, parse the result again, check that the XML "
                                            "and the structured view changed exactly as intended, and show the diff. "
                                            "PREVIEW BY DEFAULT: only --apply (edit the file in place) or --output PATH "
                                            "(write a copy) writes anything. A refused patch exits 3 and writes nothing.")
    patch.add_argument("file", type=Path, help="a .twb or .twbx file")
    common = argparse.ArgumentParser(add_help=False)
    where = common.add_mutually_exclusive_group()
    where.add_argument("--apply", action="store_true", help="write the change into FILE (a .twbx keeps every other member byte for byte)")
    where.add_argument("--output", "-o", type=Path, help="write the patched workbook here instead (same suffix as FILE)")
    common.add_argument("--json", action="store_true", help="print the result as JSON")
    common.add_argument("--max-diff-lines", type=int, default=200, help="XML diff lines to print (default 200)")
    kinds = patch.add_subparsers(dest="kind", metavar="PATCH")

    rename = kinds.add_parser("rename-field", parents=[common], help="rename a field or parameter everywhere it is shown",
                              description="Rename a field or parameter: its caption in the data source and in every "
                                          "sheet's copy. Formulas, shelves and filters refer to fields by internal name, "
                                          "so they follow without being edited; the diff shows each one that now reads "
                                          "the new name. Refused when the new name is already taken in that data source.")
    rename.add_argument("field", help="the field's caption or internal name")
    rename.add_argument("new_name", help="the new caption")
    rename.add_argument("--datasource", help="the data source, when the name is in more than one")

    swap = kinds.add_parser("swap-datasource", parents=[common], help="point a data source at another project, dataset, table or column",
                            description="Point a data source at other warehouse tables: move it to another project or "
                                        "dataset (connection tables and the tables its custom SQL names move with it), "
                                        "rename tables, or point fields at renamed warehouse columns. Custom SQL is edited "
                                        "only where a table moves, and re-parsed with sqlglot to check it is the same query "
                                        "with the names mapped. BigQuery connections only.")
    swap.add_argument("datasource", help="the data source's caption or internal name")
    swap.add_argument("--project", help="the new project")
    swap.add_argument("--dataset", help="the new dataset")
    swap.add_argument("--billing-project", help="the new billing project (where queries run)")
    swap.add_argument("--table", action="append", metavar="OLD=NEW", help="rename a table (repeatable): orders=orders_v2")
    swap.add_argument("--column", action="append", metavar="OLD=NEW",
                      help="point the field reading warehouse column OLD at NEW (repeatable)")

    color = kinds.add_parser("set-color", parents=[common], help="set a sheet's mark color or a member's palette color",
                             description="Set the fixed mark color of a sheet (--sheet), or the palette color of one value "
                                         "of a dimension (--field with --member). Only a color the workbook already sets "
                                         "is changed; a sheet or member without one is refused.")
    color.add_argument("--color", required=True, help="#rrggbb")
    color.add_argument("--sheet", help="the worksheet whose mark color to set")
    color.add_argument("--field", help="the dimension whose member color to set")
    color.add_argument("--member", help="the member (value) of --field, as shown, without quotes")
    color.add_argument("--datasource", help="the data source of --field, when its name is in more than one")

    fmt = kinds.add_parser("set-format", parents=[common], help="set a field's number format",
                           description="Set a field's default number format (in the data source and every sheet's copy), "
                                       "or with --sheet the format that sheet already gives it. Tableau format strings: "
                                       "p0.0% (percent), c\"$\"#,##0 (currency), n#,##0.00 (number).")
    fmt.add_argument("field", help="the field's caption or internal name")
    fmt.add_argument("format", help="the Tableau format string")
    fmt.add_argument("--sheet", help="change the format this sheet gives the field instead of the default")
    fmt.add_argument("--datasource", help="the data source, when the name is in more than one")
    return parser


def _print(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _plan(args):
    if args.kind == "rename-field":
        return P.rename_field(args.field, args.new_name, datasource=args.datasource)
    if args.kind == "swap-datasource":
        return P.swap_datasource(args.datasource, project=args.project, dataset=args.dataset,
                                 billing_project=args.billing_project, tables=args.table, columns=args.column)
    if args.kind == "set-color":
        return P.set_color(args.color, sheet=args.sheet, field_name=args.field, member=args.member, datasource=args.datasource)
    return P.set_format(args.format, args.field, sheet=args.sheet, datasource=args.datasource)


def _patch(args, book) -> int:
    try:
        result = P.run(book, _plan(args))
    except P.PatchRefused as exc:
        if args.json:
            _print(json.dumps({"patch": args.kind, "file": book.label, "refused": str(exc), "written": None}, indent=2))
        else:
            print(f"REFUSED ({args.kind}): {exc}\nNothing was written.", file=sys.stderr)
        return EXIT_REFUSED
    target = args.file if args.apply else args.output
    if target is not None and Path(target).suffix.lower() != args.file.suffix.lower():
        print(f"--output must have the same suffix as {args.file.name} ({args.file.suffix})", file=sys.stderr)
        return EXIT_ERROR
    written = None
    if target is not None:
        try:
            P.write_result(result, Path(target))
        except (P.PatchRefused, OSError) as exc:
            print(f"REFUSED ({args.kind}): {exc}", file=sys.stderr)
            return EXIT_REFUSED
        written = str(target)
    if args.json:
        out = result.as_dict(args.max_diff_lines)
        out["written"] = written
        _print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    lines = [f"PATCH {result.kind}: {result.description}", "", "verified:"]
    lines += [f"  - {check}" for check in result.checks]
    for warning in result.warnings:
        lines.append(f"  ! {warning}")
    limit = args.max_diff_lines
    lines += ["", f"changes in the structured view ({len(result.changes)}):"]
    lines += [f"  {change}" for change in result.changes[:limit]]
    if len(result.changes) > limit:
        lines.append(f"  ... {len(result.changes) - limit} more changes (raise --max-diff-lines, or use --json)")
    if result.view_diff:
        text_diff = result.view_diff[2:]
        lines += ["", "text view diff:"] + [f"  {line}" for line in text_diff[:limit]]
        if len(text_diff) > limit:
            lines.append(f"  ... {len(text_diff) - limit} more lines")
    shown = result.xml_diff[:limit]
    lines += ["", f"XML diff ({result.edits} edits, {len(result.xml_diff)} diff lines):"] + shown
    if len(result.xml_diff) > len(shown):
        lines.append(f"... {len(result.xml_diff) - len(shown)} more diff lines (raise --max-diff-lines, or use --json)")
    lines.append("")
    if written:
        lines.append(f"written: {written}")
    else:
        lines.append(f"not written: this was a preview. Add --apply to change {args.file.name}, or --output PATH to write a copy.")
    _print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_ERROR
    if args.command == "patch" and args.kind is None:
        parser.parse_args(["patch", "--help"])
    try:
        book = open_workbook(args.file)
    except WorkbookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.command == "show":
        view = book.view()
        if args.json:
            _print(json.dumps(view, indent=2, ensure_ascii=False))
        else:
            _print(render_text(view, max_chars=args.max_chars, sheet=args.sheet, datasource=args.datasource,
                               sql_lines=args.sql_lines))
        return 0
    if args.command == "find":
        if bool(args.field) == bool(args.table):
            parser.error("find needs a FIELD or --table, not both")
        view = book.view()
        if args.table:
            result = find_table(view, args.table)
            if args.json:
                _print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                count = len(result['reads'])
                lines = [f"{args.table}: read by {count} {'relation' if count == 1 else 'relations'}"]
                lines += [f"  {r['datasource']}: {r['table']} ({'custom SQL ' + repr(r['relation']) if r['via'] == 'custom_sql' else 'table'})"
                          for r in result["reads"]]
                lines.append(f"  sheets: {', '.join(result['sheets']) or 'none'}")
                lines.append(f"  dashboards: {', '.join(result['dashboards']) or 'none'}")
                for u in result.get("unknown", []):
                    lines.append(f"  unknown: custom SQL {u['relation']!r} of {u['datasource']} could not be parsed ({u['reason']})")
                _print("\n".join(lines))
            return 0 if result["reads"] else EXIT_NOT_FOUND
        result = find_field(view, args.field, datasource=args.datasource)
        _print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render_find(result))
        return 0 if result["matches"] else EXIT_NOT_FOUND
    if args.command == "tables":
        result = tables_read(book.view())
        if args.json:
            _print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            lines = []
            for entry in result:
                lines.append(f"{entry['datasource']} ({entry['connection_class'] or 'no connection'}):")
                for t in entry["tables"]:
                    how = "table" if t["via"] == "table" else f"custom SQL {t['relation']!r}"
                    note = f", written {t['written']}, completed from the {t['completed_from']}" if t.get("written") else ""
                    lines.append(f"  {t['table']}  ({how}{note})")
                for u in entry["unparsed"]:
                    lines.append(f"  unknown: custom SQL {u['relation']!r} ({u['reason']})")
                if not entry["tables"] and not entry["unparsed"]:
                    lines.append("  no warehouse table")
            _print("\n".join(lines) or "no data sources")
        return 0
    return _patch(args, book)


if __name__ == "__main__":
    sys.exit(main())
