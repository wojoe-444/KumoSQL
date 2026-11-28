"""``python -m kumosql notebook timeline FILE``: a Jupyter notebook as a compact timeline of cells, outputs and errors.

A notebook of 50,000 tokens does not fit in an agent's context, and most of it is output: images as base64, HTML tables,
progress bars, long tracebacks. The timeline keeps what a reader needs to follow the work:

* a header: cell counts, the kernel, which cells ran and whether they ran in order, and every error (cell, exception
  and message), so an error is never hidden by the size cap;
* one entry per cell, numbered from 1 as it appears in the notebook: its type, execution count, source and outputs;
* per-cell limits: source and each output are cut to a few lines, a long line to a few hundred characters, and every
  cut says what was left out (``... 37 more lines cut (1,204 characters)``); images and HTML are named with their size
  and never inlined; a traceback keeps its last lines, where the error is raised;
* a hard cap on the whole text (``--max-chars``, 12,000 characters by default): cells past it are not shown, and the
  last line says which cells were left out and how to see them (``--cells 41-120``).

READ-ONLY: it only reads the notebook and prints. It reads nbformat 4 (``.ipynb``) files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

__all__ = ["NotebookError", "read_notebook", "timeline", "render_timeline", "main", "DEFAULT_MAX_CHARS"]

DEFAULT_MAX_CHARS = 12_000
DEFAULT_SOURCE_LINES = 20
DEFAULT_OUTPUT_LINES = 12
DEFAULT_MARKDOWN_LINES = 6
LINE_CHARS = 300
NL = "\n"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class NotebookError(ValueError):
    """The file is not a notebook this module can read; the message says why."""


def read_notebook(path: str | Path) -> dict:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NotebookError(f"cannot read {path}: {exc}") from exc
    return parse_notebook(text, path.name)


def parse_notebook(text: str, name: str = "notebook.ipynb") -> dict:
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotebookError(f"{name} is not JSON: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        if isinstance(notebook, dict) and "worksheets" in notebook:
            raise NotebookError(f"{name} is an nbformat 3 notebook; open and save it in Jupyter to upgrade it to nbformat 4")
        raise NotebookError(f"{name} is JSON but not a Jupyter notebook (no list of cells)")
    notebook.setdefault("metadata", {})
    notebook["_name"] = name
    return notebook


def _text(value) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return "" if value is None else str(value)


def _clip_line(line: str) -> str:
    if len(line) <= LINE_CHARS:
        return line
    return line[:LINE_CHARS] + f" ... (+{len(line) - LINE_CHARS:,} characters)"


def _lines(text: str) -> list[str]:
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        if "\r" in line:  # a progress bar redraws its line: keep what was shown last
            line = line.rsplit("\r", 1)[-1]
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _head(lines: list[str], limit: int) -> tuple[list[str], dict | None]:
    """The first ``limit`` lines, and what was cut."""

    if len(lines) <= limit:
        return [_clip_line(line) for line in lines], None
    rest = lines[limit:]
    return [_clip_line(line) for line in lines[:limit]], {"lines": len(rest), "characters": sum(len(x) + 1 for x in rest)}


def _head_tail(lines: list[str], limit: int) -> tuple[list[str], dict | None]:
    """The first and last lines (a stream's start and its end), and what was cut from the middle."""

    if len(lines) <= limit:
        return [_clip_line(line) for line in lines], None
    first = max(1, limit // 3)
    last = limit - first
    cut = lines[first:len(lines) - last]
    kept = [_clip_line(line) for line in lines[:first]] + [None] + [_clip_line(line) for line in lines[len(lines) - last:]]
    return kept, {"lines": len(cut), "characters": sum(len(x) + 1 for x in cut), "where": "middle"}


def _tail(lines: list[str], limit: int) -> tuple[list[str], dict | None]:
    if len(lines) <= limit:
        return [_clip_line(line) for line in lines], None
    cut = lines[:len(lines) - limit]
    return [_clip_line(line) for line in lines[len(lines) - limit:]], {"lines": len(cut), "characters": sum(len(x) + 1 for x in cut),
                                                                       "where": "start"}


def _size(value) -> int:
    return len(_text(value).encode("utf-8"))


def _kb(size: int) -> str:
    return f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def _output(output: dict, output_lines: int) -> dict:
    kind = output.get("output_type", "?")
    if kind == "stream":
        lines, cut = _head_tail(_lines(_text(output.get("text"))), output_lines)
        if cut:  # say what was cut where it was cut
            lines = [_cut_note(cut, "output") if line is None else line for line in lines]
        item = {"type": "stream", "name": output.get("name", "stdout"), "text": lines}
    elif kind == "error":
        traceback = _lines("\n".join(_text(line) for line in output.get("traceback", [])))
        lines, cut = _tail(traceback, output_lines)
        item = {"type": "error", "ename": output.get("ename", "?"), "evalue": _clip_line(_ANSI.sub("", _text(output.get("evalue")))),
                "traceback": lines}
    elif kind in ("execute_result", "display_data"):
        data = output.get("data") or {}
        item = {"type": kind}
        cut = None
        if "text/plain" in data:
            item["text"], cut = _head(_lines(_text(data["text/plain"])), output_lines)
        others = []
        for mime in sorted(data):
            if mime == "text/plain":
                continue
            size = _size(data[mime])
            if mime.startswith("image/") and mime != "image/svg+xml":
                size = size * 3 // 4  # base64
            others.append(f"{mime} {_kb(size)}")
        if others:
            item["not_shown"] = others
    else:
        return {"type": kind}
    if cut:
        item["cut"] = cut
    return item


def _parse_cells(spec: str | None, count: int) -> set[int] | None:
    if not spec:
        return None
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        low, dash, high = part.partition("-")
        try:
            start = int(low)
            end = int(high) if dash and high else (count if dash else start)
        except ValueError as exc:
            raise NotebookError(f"--cells {spec!r}: use numbers and ranges such as 3,7-12,40-") from exc
        chosen.update(range(max(1, start), min(count, end) + 1))
    return chosen


def timeline(notebook: dict, source_lines: int = DEFAULT_SOURCE_LINES, output_lines: int = DEFAULT_OUTPUT_LINES,
             markdown_lines: int = DEFAULT_MARKDOWN_LINES, cells: str | None = None, errors_only: bool = False) -> dict:
    """The notebook as a timeline (the JSON form); :func:`render_timeline` applies the size cap."""

    raw_cells = notebook.get("cells", [])
    metadata = notebook.get("metadata", {}) or {}
    kernel = (metadata.get("kernelspec") or {}).get("name")
    language = (metadata.get("language_info") or {}).get("name") or (metadata.get("kernelspec") or {}).get("language")
    entries = []
    errors = []
    executed = []
    for number, cell in enumerate(raw_cells, 1):
        kind = cell.get("cell_type", "?")
        source = _lines(_text(cell.get("source")))
        entry: dict = {"cell": number, "type": kind}
        limit = source_lines if kind == "code" else markdown_lines
        entry["source"], cut = _head(source, limit)
        if cut:
            entry["source_cut"] = cut
        if kind == "code":
            count = cell.get("execution_count")
            entry["execution_count"] = count
            if count is not None:
                executed.append((count, number))
            outputs = [_output(o, output_lines) for o in cell.get("outputs", []) or [] if isinstance(o, dict)]
            if outputs:
                entry["outputs"] = outputs
            for o in outputs:
                if o["type"] == "error":
                    errors.append({"cell": number, "ename": o["ename"], "evalue": o["evalue"]})
        entries.append(entry)
    order = [number for _, number in sorted(executed)]
    in_order = order == sorted(order)
    late = [number for position, number in enumerate(order) if any(other > number for other in order[:position])]
    counts = {}
    for entry in entries:
        counts[entry["type"]] = counts.get(entry["type"], 0) + 1
    code = [e for e in entries if e["type"] == "code"]
    chosen = _parse_cells(cells, len(entries))
    shown = [e for e in entries if (chosen is None or e["cell"] in chosen)
             and (not errors_only or any(o["type"] == "error" for o in e.get("outputs", [])))]
    return {
        "file": notebook.get("_name"),
        "nbformat": f"{notebook.get('nbformat', '?')}.{notebook.get('nbformat_minor', '?')}",
        "kernel": kernel, "language": language,
        "cells": len(entries), "cell_types": dict(sorted(counts.items())),
        "code_cells_run": len(executed), "code_cells_not_run": len(code) - len(executed),
        "run_in_order": in_order, "run_order": order, "ran_after_a_later_cell": late,
        "errors": errors,
        "selected": None if chosen is None and not errors_only else [e["cell"] for e in shown],
        "entries": shown,
    }


def _cut_note(cut: dict, what: str) -> str:
    where = {"middle": " from the middle", "start": " from the start"}.get(cut.get("where", ""), "")
    return f"... {cut['lines']:,} more {what} lines cut{where} ({cut['characters']:,} characters)"


def _entry_lines(entry: dict) -> list[str]:
    if entry["type"] == "code":
        count = entry.get("execution_count")
        head = f"[{entry['cell']}] code, " + (f"run {count}" if count is not None else "not run")
    else:
        head = f"[{entry['cell']}] {entry['type']}"
    lines = [head]
    for line in entry["source"]:
        lines.append(f"    {line}")
    if entry.get("source_cut"):
        lines.append(f"    {_cut_note(entry['source_cut'], 'source')}")
    for output in entry.get("outputs", []):
        kind = output["type"]
        if kind == "error":
            lines.append(f"  ERROR {output['ename']}: {output['evalue']}")
            if output.get("cut"):
                lines.append(f"    | {_cut_note(output['cut'], 'traceback')}")
            lines.extend(f"    | {line}" for line in output["traceback"])
        elif kind == "stream":
            lines.append(f"  {output['name']}:")
            lines.extend(f"    > {line}" for line in output["text"])
        elif kind in ("execute_result", "display_data"):
            label = "result" if kind == "execute_result" else "display"
            text = output.get("text") or []
            note = f" (also {', '.join(output['not_shown'])}, not shown)" if output.get("not_shown") and text else ""
            if not text and output.get("not_shown"):
                lines.append(f"  {label}: {', '.join(output['not_shown'])}, not shown")
            else:
                lines.append(f"  {label}{note}:")
                lines.extend(f"    = {line}" for line in text)
                if output.get("cut"):
                    lines.append(f"    = {_cut_note(output['cut'], 'output')}")
        else:
            lines.append(f"  output of type {kind}")
    return lines


def render_timeline(data: dict, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The timeline as text, never longer than ``max_chars``: the header always, then cells until the cap."""

    types = ", ".join(f"{n} {t}" for t, n in data["cell_types"].items()) or "no cells"
    header = [f"notebook {data['file']} (nbformat {data['nbformat']}, kernel {data['kernel'] or '?'}, "
              f"language {data['language'] or '?'}): {data['cells']} cells ({types})"]
    run = f"code cells: {data['code_cells_run']} run, {data['code_cells_not_run']} not run"
    if data["run_order"]:
        if data["run_in_order"]:
            run += "; run top to bottom"
        else:
            late = data["ran_after_a_later_cell"]
            order = data["run_order"]
            shown = ", ".join(map(str, order[:30])) + (", ..." if len(order) > 30 else "")
            run += (f"; NOT run top to bottom: {_plural(len(late), 'cell')} ran after a cell below {'it' if len(late) == 1 else 'them'} "
                    f"({', '.join(map(str, late[:15]))}{', ...' if len(late) > 15 else ''}); run order: {shown}")
    header.append(run)
    if data["errors"]:
        header.append(f"errors ({len(data['errors'])}):")
        for error in data["errors"][:20]:
            header.append(f"  cell {error['cell']}: {error['ename']}: {error['evalue']}")
        if len(data["errors"]) > 20:
            header.append(f"  ... {len(data['errors']) - 20} more errors (use --errors-only)")
    else:
        header.append("errors: none")
    if data["selected"] is not None:
        header.append(f"showing {len(data['entries'])} of {data['cells']} cells")
    head = NL.join(header)
    if len(head) + 1 > max_chars:  # the cap is hard: even a header with many errors is cut to fit
        marker = NL + "... header cut at the character cap; use --errors-only or a larger --max-chars"
        if max_chars <= len(marker):
            return head[:max_chars]
        return head[:max_chars - len(marker)].rsplit(NL, 1)[0] + marker
    blocks: list[tuple[int, str]] = []
    used = len(head) + 1
    entries = data["entries"]

    def note_for(rest: list[int]) -> str:
        span = f"{rest[0]}-{rest[-1]}" if len(rest) > 1 else str(rest[0])
        return (f"... {_plural(len(rest), 'cell')} not shown ({span}): the {max_chars:,}-character cap was reached; "
                f"use --cells {span}, --errors-only or a larger --max-chars")

    for position, entry in enumerate(entries):
        block = NL.join(_entry_lines(entry))
        if used + len(block) + 2 > max_chars:
            rest = [e["cell"] for e in entries[position:]]
            while blocks and used + len(note_for(rest)) + 2 > max_chars:  # drop whole cells, never half of one
                cell, dropped = blocks.pop()
                used -= len(dropped) + 1
                rest.insert(0, cell)
            note = note_for(rest)
            if used + len(note) + 2 > max_chars:
                return head
            return NL.join([head, "", *(b for _, b in blocks), note])
        blocks.append((entry["cell"], block))
        used += len(block) + 1
    return NL.join([head, "", *(b for _, b in blocks)]).rstrip(NL)


def _plural(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kumosql notebook",
        description="A Jupyter notebook (.ipynb) as a compact timeline of cells, outputs and errors, under a hard size cap. "
                    "READ-ONLY: it reads the notebook and prints.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    show = commands.add_parser(
        "timeline", help="cells, outputs and errors as compact text",
        description="Print the notebook's cells in order with their execution counts, source and outputs, each cut to a few "
                    "lines with a note of what was cut; images and HTML are named with their size, never inlined. A "
                    "header lists every error and whether the cells ran top to bottom. The whole text stays under "
                    "--max-chars. Only prints.")
    show.add_argument("file", type=Path, help="an .ipynb file")
    show.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                      help=f"hard cap on the printed text (default {DEFAULT_MAX_CHARS:,} characters)")
    show.add_argument("--source-lines", type=int, default=DEFAULT_SOURCE_LINES,
                      help=f"source lines per code cell (default {DEFAULT_SOURCE_LINES})")
    show.add_argument("--markdown-lines", type=int, default=DEFAULT_MARKDOWN_LINES,
                      help=f"lines per markdown cell (default {DEFAULT_MARKDOWN_LINES})")
    show.add_argument("--output-lines", type=int, default=DEFAULT_OUTPUT_LINES,
                      help=f"lines per output; a traceback keeps its last lines (default {DEFAULT_OUTPUT_LINES})")
    show.add_argument("--cells", help="only these cells, numbered from 1: 3,7-12,40-")
    show.add_argument("--errors-only", action="store_true", help="only the cells that raised an error")
    show.add_argument("--json", action="store_true", help="print the timeline as JSON (per-cell limits apply; no total cap)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        notebook = read_notebook(args.file)
        data = timeline(notebook, source_lines=args.source_lines, output_lines=args.output_lines,
                        markdown_lines=args.markdown_lines, cells=args.cells, errors_only=args.errors_only)
    except NotebookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(data, indent=2, ensure_ascii=False) if args.json else render_timeline(data, max_chars=args.max_chars)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
