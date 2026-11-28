"""BI workbooks as compact text, one-call questions, and patches checked by parsing the result again.

Only Tableau is read today: ``.twb`` (XML) and ``.twbx`` (a zip holding one ``.twb``). The view is a plain dict with a
``format`` key, built by a format module (:mod:`kumosql.bi_workbook.tableau`); :func:`render_text`, :func:`find_field`
and :func:`tables_read` work on that dict, so another BI format can be added as a second reader with the same shape.

>>> book = open_workbook("sales_overview.twb")                       # doctest: +SKIP
>>> print(render_text(book.view()))                                   # doctest: +SKIP
>>> find_field(book.view(), "Profit Ratio")["matches"][0]["formula"]  # doctest: +SKIP
'SUM([Profit]) / SUM([Sales])'

Patches are in :mod:`kumosql.bi_workbook.patch`; the command line is ``python -m kumosql workbook``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import tableau

__all__ = ["Workbook", "WorkbookError", "open_workbook", "read_workbook", "render_text", "find_field", "find_table",
           "tables_read", "SUFFIXES"]

SUFFIXES = (".twb", ".twbx")
DEFAULT_MAX_CHARS = 12_000


class WorkbookError(ValueError):
    """The file is not a workbook this module can read; the message says why."""


@dataclass
class Workbook:
    """A loaded workbook: the bytes of its XML, where they came from, and the parsed tree."""

    name: str  # the file name, for messages
    kind: str  # "twb" or "twbx"
    xml: bytes  # the .twb document
    member: str | None = None  # the .twb's name inside a .twbx
    package: bytes | None = None  # the whole .twbx archive, for a .twbx
    path: Path | None = None  # where it was read from, when it came from a file
    root: ET.Element = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            self.root = ET.fromstring(self.xml)
        except ET.ParseError as exc:
            raise WorkbookError(f"{self.label} is not well-formed XML: {exc}") from exc
        if self.root.tag != "workbook":
            raise WorkbookError(f"{self.label} is XML but not a Tableau workbook (its root is <{self.root.tag}>)")

    @property
    def label(self) -> str:
        return f"{self.name}!{self.member}" if self.member else self.name

    @property
    def original(self) -> bytes:
        """The file's bytes as read: the archive for a .twbx, the XML for a .twb."""

        return self.package if self.package is not None else self.xml

    def view(self) -> dict:
        view = tableau.build_view(self.root, file_name=self.label)
        if self.kind == "twbx":
            view["package"] = {"workbook": self.member}
        return view


def _twbx_member(archive: zipfile.ZipFile, label: str) -> str:
    candidates = [info.filename for info in archive.infolist()
                  if info.filename.lower().endswith(".twb") and not info.is_dir()]
    top = [name for name in candidates if len(PurePosixPath(name).parts) == 1]
    chosen = top or candidates
    if len(chosen) != 1:
        found = ", ".join(sorted(candidates)) or "none"
        raise WorkbookError(f"{label} must hold exactly one .twb workbook at its top level; found {found}")
    return chosen[0]


def read_workbook(data: bytes, name: str = "workbook.twb") -> Workbook:
    """A workbook from bytes; ``name`` decides the format by its suffix."""

    suffix = Path(name).suffix.lower()
    label = Path(name).name
    if suffix == ".twbx":
        import io

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = _twbx_member(archive, label)
                xml = archive.read(member)
        except zipfile.BadZipFile as exc:
            raise WorkbookError(f"{label} is not a zip archive, so not a packaged workbook: {exc}") from exc
        return Workbook(name=label, kind="twbx", xml=xml, member=member, package=data)
    if suffix == ".twb":
        return Workbook(name=label, kind="twb", xml=data)
    raise WorkbookError(f"{label}: only Tableau workbooks are read ({', '.join(SUFFIXES)})")


def open_workbook(path: str | Path) -> Workbook:
    path = Path(path)
    if path.suffix.lower() not in SUFFIXES:
        raise WorkbookError(f"{path.name}: only Tableau workbooks are read ({', '.join(SUFFIXES)})")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise WorkbookError(f"cannot read {path}: {exc.strerror or exc}") from exc
    book = read_workbook(data, path.name)
    book.path = path
    return book


# ------------------------------------------------------------------------------------------------------- the lineage hook


def tables_read(view: dict) -> list[dict]:
    """The warehouse tables each data source reads, for lineage: who reads which table.

    One entry per data source: ``datasource`` (caption), ``name`` (internal), ``tables`` (``{"table", "via", "relation"}``
    with ``via`` ``table`` for a connection's table and ``custom_sql`` for a table named in custom SQL), and ``unparsed``
    (``{"relation", "reason"}`` for custom SQL sqlglot could not parse: its tables are unknown, never guessed)."""

    out = []
    for ds in view.get("datasources", []):
        tables, unparsed = [], []
        for relation in ds.get("relations", []):
            if relation["type"] == "table":
                if relation.get("table"):
                    item = {"table": relation["table"], "via": "table", "relation": relation["name"]}
                    if relation.get("completed_from"):
                        item["completed_from"] = relation["completed_from"]
                    tables.append(item)
            elif relation.get("unparsed"):
                unparsed.append({"relation": relation["name"], "reason": relation["unparsed"]})
            else:
                for table in relation.get("tables", []):
                    tables.append({**table, "via": "custom_sql", "relation": relation["name"]})
        classes = sorted({c["class"] for c in ds.get("connections", []) if c.get("class")})
        out.append({"datasource": ds["caption"], "name": ds["name"], "connection_class": ", ".join(classes) or None,
                    "tables": tables, "unparsed": unparsed})
    return out


# ------------------------------------------------------------------------------------------------------------- find


def _norm(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == "[" and text[-1] == "]" and "].[" not in text:
        text = text[1:-1].replace("]]", "]")
    return " ".join(text.lower().split())


def find_field(view: dict, query: str, datasource: str | None = None) -> dict:
    """Answer "which sheets use field X, and what is its formula?" in one call.

    Matches a field or parameter by caption or internal name (case and spacing ignored, brackets optional), optionally
    within one data source (caption or internal name). Each match lists its formula (with captions, and as stored), the
    fields it reads, the calculated fields that read it, the sheets that use it directly and those that reach it only
    through calculated fields (with the path), the filters on it, and the dashboards showing any of those sheets.
    Nothing matched: ``matches`` is empty and ``suggestions`` holds near names."""

    want = _norm(query)
    ds_filter = _norm(datasource) if datasource else None
    candidates = []
    for ds in view.get("datasources", []):
        if ds_filter and ds_filter not in (_norm(ds["caption"]), _norm(ds["name"])):
            continue
        for item in ds["fields"]:
            candidates.append((ds["caption"], item))
    if not ds_filter or ds_filter == "parameters":
        for item in view.get("parameters", []):
            candidates.append(("Parameters", {**item, "kind": "parameter"}))
    matches = [(ds, item) for ds, item in candidates if want in (_norm(item["caption"]), _norm(item["name"]))]
    result: dict = {"query": query, "matches": []}
    if not matches:
        near = sorted({item["caption"] for _, item in candidates if want and want in _norm(item["caption"])})
        result["suggestions"] = near[:10]
        return result
    # who reads what: (datasource caption, field name) -> fields it reads
    reads: dict[tuple[str, str], list[tuple[str, str]]] = {}
    captions = {(ds, item["name"]): item["caption"] for ds, item in candidates}
    for ds in view.get("datasources", []):
        for item in ds["fields"]:
            captions[(ds["caption"], item["name"])] = item["caption"]
            reads[(ds["caption"], item["name"])] = [(r["datasource"], r["name"]) for r in item.get("references", [])]

    def reached(start: tuple[str, str]) -> dict[tuple[str, str], list[str]]:
        """Every field ``start`` reads, directly or not, with the chain of captions that leads to it."""

        paths: dict[tuple[str, str], list[str]] = {}
        todo = [(start, [])]
        while todo:
            node, path = todo.pop(0)
            for nxt in reads.get(node, []):
                if nxt not in paths and nxt != start:
                    paths[nxt] = path + [captions.get(node, node[1])]
                    todo.append((nxt, paths[nxt]))
        return paths

    sheets = view.get("worksheets", [])
    for ds_caption, item in matches:
        key = (ds_caption, item["name"])
        entry = {"datasource": ds_caption, "name": item["name"], "caption": item["caption"], "kind": item["kind"]}
        for extra in ("formula", "formula_raw", "value", "datatype", "default_format", "remote"):
            if extra in item:
                entry[extra] = item[extra]
        entry["reads"] = [r["caption"] if r["datasource"] == ds_caption else f"{r['datasource']}.{r['caption']}"
                          for r in item.get("references", [])]
        entry["read_by"] = sorted(captions.get(k, k[1]) for k, targets in reads.items() if key in targets)
        direct, through = [], []
        for sheet in sheets:
            uses = [(f["datasource"], f["name"]) for f in sheet["fields"]]
            if key in uses:
                direct.append(sheet["name"])
                continue
            via = None
            for used in uses:
                path = reached(used).get(key)
                if path is not None and (via is None or len(path) < len(via)):
                    via = path
            if via is not None:
                through.append({"sheet": sheet["name"], "via": via})
        entry["sheets"] = direct
        entry["sheets_through_calculations"] = through
        filters = []
        for sheet in sheets:
            for filt in sheet.get("filters", []):
                if (filt["datasource"], filt["field"]) == key:
                    filters.append({"sheet": sheet["name"], "summary": filt["summary"]})
        for ds in view.get("datasources", []):
            for filt in ds.get("filters", []):
                if (filt["datasource"], filt["field"]) == key:
                    filters.append({"datasource": ds["caption"], "summary": filt["summary"]})
        entry["filters"] = filters
        shown = set(direct) | {t["sheet"] for t in through}
        entry["dashboards"] = [{"name": d["name"], "sheets": [s for s in d["sheets"] if s in shown]}
                               for d in view.get("dashboards", []) if shown & set(d["sheets"])]
        result["matches"].append(entry)
    return result


def find_table(view: dict, table: str) -> dict:
    """Which data sources read a warehouse table (``dataset.table`` or ``project.dataset.table``; a suffix matches), and
    which sheets and dashboards use those data sources."""

    want = table.strip().strip("`").lower()
    hits = []
    for entry in tables_read(view):
        for item in entry["tables"]:
            name = item["table"].lower()
            if name == want or name.endswith("." + want):
                hits.append({"datasource": entry["datasource"], **item})
    names = {hit["datasource"] for hit in hits}
    sheets = [s["name"] for s in view.get("worksheets", []) if names & set(s["datasources"])]
    dashboards = [d["name"] for d in view.get("dashboards", []) if set(sheets) & set(d["sheets"])]
    unparsed = [{"datasource": e["datasource"], **u} for e in tables_read(view) for u in e["unparsed"]]
    out = {"query": table, "reads": hits, "sheets": sheets, "dashboards": dashboards}
    if unparsed:
        out["unknown"] = unparsed  # custom SQL that could not be parsed might read the table too
    return out


# ------------------------------------------------------------------------------------------------------------- text


def _clip_lines(lines: list[str], max_chars: int, hint: str) -> str:
    out, used = [], 0
    for number, line in enumerate(lines):
        if used + len(line) + 1 > max_chars:
            rest = lines[number:]
            cut = sum(len(item) + 1 for item in rest)
            out.append(f"... cut: {len(rest)} more lines ({cut:,} characters) past the {max_chars:,}-character cap; {hint}")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _list(items: list[str], limit: int = 40) -> str:
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... and {len(items) - limit} more"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _sheet_fields(fields: list[dict], many: bool) -> str:
    if not many:
        return _list([f["caption"] for f in fields])
    groups: dict[str, list[str]] = {}
    for f in fields:
        groups.setdefault(f["datasource"], []).append(f["caption"])
    return "; ".join(f"{ds}: {_list(names)}" for ds, names in groups.items())


def _color_lines(colors: list[dict]) -> list[str]:
    groups: dict[str, list[str]] = {}
    for c in colors:
        groups.setdefault(c["caption"], []).append(f"{c['member']} {c['color']}")
    return [f"  colors of {caption}: {_list(items)}" for caption, items in groups.items()]


def render_text(view: dict, max_chars: int = DEFAULT_MAX_CHARS, sheet: str | None = None, datasource: str | None = None,
                sql_lines: int = 30) -> str:
    """The view as compact, deterministic text, cut at ``max_chars`` with a note saying how much was cut.

    ``sheet`` or ``datasource`` (caption or internal name) narrows the text to one sheet or one data source."""

    lines: list[str] = []
    datasources = view.get("datasources", [])
    worksheets = view.get("worksheets", [])
    dashboards = view.get("dashboards", [])
    parameters = view.get("parameters", [])
    if sheet:
        worksheets = [s for s in worksheets if _norm(s["name"]) == _norm(sheet)]
        if not worksheets:
            return f"no worksheet named {sheet!r}; sheets: {_list([s['name'] for s in view.get('worksheets', [])])}"
        datasources, dashboards, parameters = [], [d for d in dashboards if worksheets[0]["name"] in d["sheets"]], []
    elif datasource:
        datasources = [d for d in datasources if _norm(datasource) in (_norm(d["caption"]), _norm(d["name"]))]
        if not datasources:
            return f"no data source named {datasource!r}; data sources: {_list([d['caption'] for d in view.get('datasources', [])])}"
        keep = datasources[0]["caption"]
        worksheets = [s for s in worksheets if keep in s["datasources"]]
        dashboards, parameters = [], []
    head = f"Tableau workbook {view.get('file', '')}".rstrip()
    if view.get("version"):
        head += f" (version {view['version']})"
    lines.append(head + ": " + ", ".join([
        _count(len(view.get("datasources", [])), "data source"), _count(len(view.get("parameters", [])), "parameter"),
        _count(len(view.get("worksheets", [])), "worksheet"), _count(len(view.get("dashboards", [])), "dashboard"),
        _count(len(view.get("problems", [])), "problem")]))
    for ds in datasources:
        lines.append("")
        lines.append(f"DATA SOURCE {ds['caption']}  [{ds['name']}]")
        for conn in ds["connections"]:
            detail = " ".join(f"{k}={v}" for k, v in conn.items() if k not in ("class", "name"))
            lines.append(f"  connection: {conn.get('class', '?')} {detail}".rstrip())
        for relation in ds["relations"]:
            if relation["type"] == "table":
                lines.append(f"  table {relation['name']}: {relation['table']}")
                continue
            if relation.get("unparsed"):
                lines.append(f"  custom SQL {relation['name']!r}: tables unknown ({relation['unparsed']})")
            else:
                lines.append(f"  custom SQL {relation['name']!r} reads: {_list([t['table'] for t in relation['tables']]) or 'no table'}")
            sql = relation["sql"].splitlines() or [""]
            for line in sql[:sql_lines]:
                lines.append(f"    | {line}")
            if len(sql) > sql_lines:
                lines.append(f"    | ... {len(sql) - sql_lines} more lines of SQL (the JSON form has all of it)")
        calcs = [f for f in ds["fields"] if f["kind"] == "calculated"]
        if calcs:
            lines.append(f"  calculated fields ({len(calcs)}):")
            for f in calcs:
                formula = " ".join(f["formula"].split()) if "//" not in f["formula"] else " / ".join(
                    part.strip() for part in f["formula"].splitlines() if part.strip())
                lines.append(f"    {f['caption']} = {formula}   {f['name']}")
        others = [f for f in ds["fields"] if f["kind"] != "calculated"]
        if others:
            labels = []
            for f in others:
                label = f["caption"]
                if f["kind"] in ("group", "bin"):
                    label += f" ({f['kind']} of {f.get('source', '?')})"
                labels.append(label)
            lines.append(f"  other fields ({len(others)}): {_list(labels)}")
        formats = [f"{f['caption']} {f['default_format']}" for f in ds["fields"] if f.get("default_format")]
        if formats:
            lines.append(f"  default formats: {_list(formats)}")
        for filt in ds["filters"]:
            lines.append(f"  data source filter: {filt['summary']}")
        lines.extend(_color_lines(ds.get("member_colors", [])))
    if parameters:
        lines.append("")
        lines.append("PARAMETERS")
        for p in parameters:
            lines.append(f"  {p['caption']} ({p.get('datatype', '?')}) = {p.get('value', '?')}   {p['name']}")
    for s in worksheets:
        many = len({f["datasource"] for f in s["fields"] + s["through_calculations"]}) > 1
        lines.append("")
        lines.append(f"WORKSHEET {s['name']}")
        lines.append(f"  data sources: {_list(s['datasources'])}")
        lines.append(f"  uses: {_sheet_fields(s['fields'], many) or 'nothing'}")
        if s["through_calculations"]:
            lines.append(f"  through calculations: {_sheet_fields(s['through_calculations'], many)}")
        for filt in s["filters"]:
            lines.append(f"  filter: {filt['summary']}")
        shelves = s.get("shelves", {})
        if shelves:
            lines.append("  " + "   ".join(f"{k}: {v}" for k, v in shelves.items()))
        if s.get("marks"):
            lines.append(f"  marks: {_list(s['marks'])}" + (f", color {_list(s['mark_colors'])}" if s.get("mark_colors") else ""))
        for fmt in s.get("formats", []):
            lines.append(f"  format: {fmt['caption']} as {fmt['format']}")
        lines.extend(_color_lines(s.get("member_colors", [])))
    for d in dashboards:
        lines.append("")
        lines.append(f"{d['type'].upper()} {d['name']}: {_list(d['sheets']) or 'no sheets'}")
        for control in d.get("filter_controls", []):
            lines.append(f"  filter control: {control['caption']} (on {control['sheet']})")
        for control in d.get("parameter_controls", []):
            lines.append(f"  parameter control: {control}")
    if view.get("problems"):
        lines.append("")
        lines.append(f"PROBLEMS ({len(view['problems'])})")
        lines.extend(f"  {p}" for p in view["problems"])
    return _clip_lines(lines, max_chars, "narrow it with --sheet or --datasource, raise --max-chars, or use --json")


def render_find(result: dict) -> str:
    """:func:`find_field`'s answer as a few lines of text."""

    if not result["matches"]:
        near = result.get("suggestions") or []
        return f"no field or parameter named {result['query']!r}" + (f"; similar: {_list(near)}" if near else "")
    lines = []
    for m in result["matches"]:
        kind = {"calculated": "calculated field", "column": "field", "parameter": "parameter"}.get(m["kind"], m["kind"])
        lines.append(f"{m['caption']}: {kind} of {m['datasource']}, internal name {m['name']}")
        if "formula" in m:
            lines.append(f"  formula: {m['formula']}")
            if m.get("formula_raw") != m["formula"]:
                lines.append(f"  as stored: {m['formula_raw']}")
        if m.get("value") is not None and m["kind"] == "parameter":
            lines.append(f"  value: {m['value']}")
        if m.get("reads"):
            lines.append(f"  reads: {_list(m['reads'])}")
        if m.get("remote"):
            lines.append(f"  warehouse column: {m['remote']}")
        lines.append(f"  read by calculated fields: {_list(m['read_by']) or 'none'}")
        lines.append(f"  used directly by {_count(len(m['sheets']), 'sheet')}: {_list(m['sheets']) or 'none'}")
        through = [f"{t['sheet']} (via {' -> '.join(t['via'])})" for t in m["sheets_through_calculations"]]
        lines.append(f"  used through calculated fields by {_count(len(through), 'sheet')}: {_list(through) or 'none'}")
        filters = [f"{f.get('sheet') or 'data source ' + f.get('datasource', '')}: {f['summary']}" for f in m["filters"]]
        lines.append(f"  filters on it: {_list(filters) or 'none'}")
        dashboards = [f"{d['name']} ({', '.join(d['sheets'])})" for d in m["dashboards"]]
        lines.append(f"  on dashboards: {_list(dashboards) or 'none'}")
    return "\n".join(lines)
