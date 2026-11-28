"""Structured patches for Tableau workbooks, each checked by parsing the result again and diffing it.

Four patches: :func:`rename_field` (a field's caption, everywhere it is declared), :func:`swap_datasource` (a data
source's project, dataset, tables and warehouse columns, custom SQL included), :func:`set_color` (a sheet's mark color
or a dimension member's palette color) and :func:`set_format` (a field's default number format, or a sheet's own).

Every patch goes through :func:`run` in the same five steps:

1. **Plan** the edit as a few character spans of the original XML text (:mod:`kumosql.bi_workbook.xml_edit`), plus the
   *intent*: the same change stated on the structured view (:class:`~kumosql.bi_workbook.tableau.Overrides`), with no
   knowledge of where the XML holds it.
2. **Re-parse** the edited text with ``xml.etree`` and the span scanner.
3. **Diff the XML** element by element: the same elements in the same order, and the only attributes or texts that
   differ are the ones the plan touched.
4. **Diff the view**: the view of the edited file must equal the view of the original with the intent applied. A copy
   of a declaration the plan missed, a formula left pointing at a field that no longer resolves, or any other side effect
   makes the two differ, and the patch is refused.
5. **Show** the change as a diff of the text view and of the XML lines. Nothing is written unless the caller asks
   (``--apply`` or ``--output`` on the command line).

Unrelated bytes stay as they were: quoting, entities, attribute order, whitespace and comments are untouched outside
the edited values, so the XML diff of a one-field change is a few lines even in a 17,000-line workbook.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import Workbook, WorkbookError, render_text
from . import refs as R
from . import tableau
from .tableau import Overrides, local
from .twbx import PackageError, replace_member
from .xml_edit import Document, Edit, XmlEditError, apply_edits, scan

__all__ = ["PatchRefused", "PatchResult", "rename_field", "swap_datasource", "set_color", "set_format", "write_result",
           "view_changes"]


class PatchRefused(ValueError):
    """The patch was not applied; the message says why, and nothing was written."""


@dataclass
class _Plan:
    description: str
    edits: list[Edit] = field(default_factory=list)
    touched: set[tuple[int, str]] = field(default_factory=set)  # (element index, attribute name or "#text")
    overrides: Overrides = field(default_factory=Overrides)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # extra checks the plan ran, for the report


@dataclass
class PatchResult:
    """A verified patch: the new file's bytes and what changed. Nothing is written until :func:`write_result`."""

    kind: str
    description: str
    workbook: Workbook
    original: bytes  # the file's bytes (.twb or .twbx)
    patched: bytes
    changes: list[str]  # every difference between the structured views, as path: old -> new
    view_diff: list[str]
    xml_diff: list[str]
    edits: int
    checks: list[str]
    warnings: list[str]

    def as_dict(self, max_diff_lines: int | None = None) -> dict:
        diff = self.xml_diff if max_diff_lines is None else self.xml_diff[:max_diff_lines]
        return {"patch": self.kind, "description": self.description, "file": self.workbook.label, "edits": self.edits,
                "changes": self.changes, "view_diff": self.view_diff, "xml_diff": diff, "xml_diff_lines": len(self.xml_diff),
                "checks": self.checks, "warnings": self.warnings,
                "sha256_before": hashlib.sha256(self.original).hexdigest(),
                "sha256_after": hashlib.sha256(self.patched).hexdigest()}


# ------------------------------------------------------------------------------------------------------------ locating


def _decode(book: Workbook) -> tuple[str, bytes]:
    data = book.xml
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, data = data[:3], data[3:]
    head = data[:200].decode("ascii", "replace").lower()
    declared = re.search(r"encoding\s*=\s*['\"]([\w.\-]+)['\"]", head)
    if declared and declared.group(1).replace("_", "-") not in ("utf-8", "utf8"):
        raise PatchRefused(f"the workbook is encoded as {declared.group(1)}; only UTF-8 workbooks are patched in place")
    try:
        return data.decode("utf-8"), bom
    except UnicodeDecodeError as exc:
        raise PatchRefused(f"the workbook is not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc


def _norm(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == "[" and text[-1] == "]" and "].[" not in text:
        text = text[1:-1].replace("]]", "]")
    return " ".join(text.lower().split())


def _datasource(view: dict, name: str | None, include_parameters: bool = False) -> list[dict]:
    items = list(view["datasources"])
    if include_parameters:
        items.append({"name": tableau.PARAMETERS, "caption": tableau.PARAMETERS,
                      "fields": [dict(p, kind="parameter") for p in view["parameters"]]})
    if name is None:
        return items
    found = [ds for ds in items if _norm(name) in (_norm(ds["caption"]), _norm(ds["name"]))]
    if not found:
        raise PatchRefused(f"no data source named {name!r}; data sources: {', '.join(d['caption'] for d in view['datasources'])}")
    return found


def _field(view: dict, query: str, datasource: str | None) -> tuple[dict, dict]:
    """The one ``(data source, field)`` a name means, or a refusal that lists the candidates."""

    matches = []
    for ds in _datasource(view, datasource, include_parameters=True):
        for item in ds["fields"]:
            if _norm(query) in (_norm(item["caption"]), _norm(item["name"])):
                matches.append((ds, item))
    if not matches:
        raise PatchRefused(f"no field named {query!r}" + (f" in data source {datasource!r}" if datasource else ""))
    if len(matches) > 1:
        where = ", ".join(f"{ds['caption']} {item['name']}" for ds, item in matches)
        raise PatchRefused(f"{query!r} names {len(matches)} fields ({where}); say which with --datasource")
    return matches[0]


def _root_children(doc: Document, name: str) -> list[ET.Element]:
    holder = next((child for child in doc.root if local(child.tag) == name), None)
    return list(holder) if holder is not None else []


def _ds_element(doc: Document, ds_name: str) -> ET.Element:
    for element in _root_children(doc, "datasources"):
        if local(element.tag) == "datasource" and element.get("name") == ds_name:
            return element
    raise PatchRefused(f"data source {ds_name!r} is not in the XML")


def _declarations(doc: Document, ds_name: str, field_name: str) -> tuple[ET.Element | None, list[ET.Element]]:
    """The data source's own ``<column>`` for a field, and every copy of it in a sheet's ``<datasource-dependencies>``."""

    bracketed = R.bracket(field_name)
    own = next((c for c in _ds_element(doc, ds_name) if local(c.tag) == "column" and c.get("name") == bracketed), None)
    copies = []
    for sheet in _root_children(doc, "worksheets"):
        for deps in sheet.iter():
            if local(deps.tag) == "datasource-dependencies" and deps.get("datasource") == ds_name:
                copies.extend(c for c in deps if local(c.tag) == "column" and c.get("name") == bracketed)
    return own, copies


def _set_attr(doc: Document, plan: _Plan, element: ET.Element, key: str, value: str) -> None:
    """Change or add an attribute; a new one goes where Tableau would put it (Tableau writes attributes alphabetically)."""

    node = doc.node(element)
    if key in node.attrs:
        plan.edits += doc.set_value(element, key, value)
    else:
        later = sorted(name for name in node.attrs if name > key and ":" not in name)
        plan.edits += doc.insert_attr(element, key, value, before=later[0] if later else None)
    plan.touched.add((node.index, key))


def _set_text(doc: Document, plan: _Plan, element: ET.Element, value: str) -> None:
    plan.edits += doc.set_value(element, "#text", value)
    plan.touched.add((doc.index(element), "#text"))


# ------------------------------------------------------------------------------------------------------------- patches


def rename_field(field_name: str, new_caption: str, datasource: str | None = None):
    """Rename a field (or parameter) as people see it: its caption in the data source and in every sheet's copy.

    Formulas, shelves, filters, sorts and tooltips refer to a field by its internal name (``[Calculation_1368…]``,
    ``[sales]``), which does not change, so they follow the new name without being edited; the view diff shows each
    formula, sheet and filter that now reads the new name. Refused when another field of the same data source already
    has that name (a formula naming it would become ambiguous) or when the data source has no declaration to hold a caption."""

    def plan(doc: Document, view: dict) -> _Plan:
        ds, item = _field(view, field_name, datasource)
        name = R.split_ref(item["name"])[0]
        caption = new_caption.strip()
        if not caption:
            raise PatchRefused("the new name is empty")
        if caption == item["caption"]:
            raise PatchRefused(f"{item['caption']!r} already has that name; nothing to change")
        for other in ds["fields"]:
            if other["name"] != item["name"] and _norm(caption) in (_norm(other["caption"]), _norm(other["name"])):
                raise PatchRefused(f"data source {ds['caption']!r} already has a field named {other['caption']!r} ({other['name']}); "
                                   f"renaming {item['caption']!r} to it would make formulas that name it ambiguous")
        own, copies = _declarations(doc, ds["name"], name)
        if own is None:
            raise PatchRefused(f"{item['caption']!r} has no <column> declaration in data source {ds['caption']!r} to hold a "
                               f"caption; rename it once in Tableau, then patches can rename it again")
        out = _Plan(f"rename {ds['caption']} field {item['caption']!r} ({item['name']}) to {caption!r}")
        for element in [own, *copies]:
            _set_attr(doc, out, element, "caption", caption)
        out.overrides.captions[(ds["name"], name)] = caption
        out.checks.append(f"caption set on the data source declaration and {len(copies)} sheet copies")
        return out

    return "rename-field", plan


def _hex_color(color: str) -> str:
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color.strip()):
        raise PatchRefused(f"{color!r} is not a color; give it as #rrggbb, for example #4e79a7")
    return color.strip()


def set_color(color: str, sheet: str | None = None, field_name: str | None = None, member: str | None = None,
              datasource: str | None = None):
    """Set a sheet's fixed mark color (``sheet``), or the palette color of one member of a dimension (``field_name`` and
    ``member``, wherever that dimension's colors are kept). Only a color the workbook already sets is changed: a sheet
    with no fixed mark color, or a member with no assigned color, is refused, because adding one means adding XML
    structure whose place Tableau decides."""

    def plan(doc: Document, view: dict) -> _Plan:
        value = _hex_color(color)
        if (sheet is None) == (field_name is None):
            raise PatchRefused("give either --sheet (the sheet's mark color) or --field with --member (a member's color)")
        if sheet is not None:
            sheets = [s for s in _root_children(doc, "worksheets") if _norm(s.get("name", "")) == _norm(sheet)]
            if not sheets:
                raise PatchRefused(f"no worksheet named {sheet!r}")
            target = sheets[0]
            formats = [fmt for pane in target.iter() if local(pane.tag) == "pane"
                       for fmt in pane.iter() if local(fmt.tag) == "format" and fmt.get("attr") == "mark-color"]
            if not formats:
                raise PatchRefused(f"sheet {target.get('name')!r} has no fixed mark color to change (its marks are colored "
                                   f"by a field or by Tableau's default); set one in Tableau first")
            out = _Plan(f"set the mark color of sheet {target.get('name')!r} to {value}")
            for fmt in formats:
                _set_attr(doc, out, fmt, "value", value)
            out.overrides.mark_colors[target.get("name", "")] = value
            return out
        if member is None:
            raise PatchRefused("--field needs --member: the value whose color to set")
        ds, item = _field(view, field_name, datasource)
        name = R.split_ref(item["name"])[0]
        out = _Plan(f"set the color of {item['caption']} = {member!r} ({ds['caption']}) to {value}")
        found = []
        for encoding in doc.root.iter():
            if local(encoding.tag) != "encoding" or encoding.get("attr") != "color":
                continue
            if _encoding_field(doc, encoding) != (ds["name"], name):
                continue
            for mapping in encoding:
                if local(mapping.tag) != "map":
                    continue
                if any(local(b.tag) == "bucket" and tableau._member_name(b.text) == member for b in mapping):
                    found.append(mapping)
        if not found:
            known = sorted({c["member"] for d in view["datasources"] for c in d.get("member_colors", [])
                            if c["datasource"] == ds["caption"] and c["field"] == item["name"]})
            raise PatchRefused(f"{item['caption']} = {member!r} has no assigned color to change"
                               + (f"; members with colors: {', '.join(known)}" if known else ""))
        for mapping in found:
            _set_attr(doc, out, mapping, "to", value)
        out.overrides.member_colors[(ds["name"], name, member)] = value
        return out

    return "set-color", plan


def _encoding_field(doc: Document, encoding: ET.Element) -> tuple[str, str] | None:
    """``(data source, field)`` of an ``<encoding field=...>``: qualified in a sheet, or unqualified inside a data source."""

    parts = R.split_ref(encoding.get("field") or "")
    if not parts:
        return None
    holder = doc.parent(encoding)
    while holder is not None and local(holder.tag) not in ("datasource", "worksheet"):
        holder = doc.parent(holder)
    if len(parts) == 2:
        ds_name, instance = parts
    elif holder is not None and local(holder.tag) == "datasource":
        ds_name, instance = holder.get("name", ""), parts[0]
    else:
        return None
    if holder is not None:
        for element in holder.iter():
            if local(element.tag) != "column-instance" or element.get("name") != R.bracket(instance):
                continue
            scope = doc.parent(element)
            if local(holder.tag) == "worksheet" and (scope is None or scope.get("datasource") != ds_name):
                continue
            column = R.split_ref(element.get("column") or "")
            if column:
                return ds_name, column[0]
    return ds_name, R.instance_field(instance)


def set_format(number_format: str, field_name: str, sheet: str | None = None, datasource: str | None = None):
    """Set a field's default number format (the data source's ``default-format``, in the declaration and every sheet's
    copy), or, with ``sheet``, the format that sheet already gives the field. A sheet without a format of its own for
    the field is refused: drop ``sheet`` to change the default every sheet falls back on."""

    def plan(doc: Document, view: dict) -> _Plan:
        value = number_format
        if not value.strip():
            raise PatchRefused("the format is empty")
        ds, item = _field(view, field_name, datasource)
        name = R.split_ref(item["name"])[0]
        if sheet is None:
            own, copies = _declarations(doc, ds["name"], name)
            if own is None:
                raise PatchRefused(f"{item['caption']!r} has no <column> declaration in data source {ds['caption']!r} to hold "
                                   f"a default format; format it once in Tableau first")
            if item.get("default_format") == value:
                raise PatchRefused(f"{item['caption']!r} already has default format {value!r}; nothing to change")
            out = _Plan(f"set the default format of {ds['caption']} field {item['caption']!r} to {value!r}")
            for element in [own, *copies]:
                _set_attr(doc, out, element, "default-format", value)
            out.overrides.default_formats[(ds["name"], name)] = value
            return out
        sheets = [s for s in _root_children(doc, "worksheets") if _norm(s.get("name", "")) == _norm(sheet)]
        if not sheets:
            raise PatchRefused(f"no worksheet named {sheet!r}")
        target = sheets[0]
        sheet_view = next(s for s in view["worksheets"] if s["name"] == target.get("name"))
        instances = {}
        for element in target.iter():
            if local(element.tag) == "column-instance":
                key = R.split_ref(element.get("name") or "")
                column = R.split_ref(element.get("column") or "")
                if key and column:
                    instances[key[0]] = column[0]
        found = []
        for element in target.iter():
            if local(element.tag) != "format" or element.get("attr") != "text-format":
                continue
            parts = R.split_ref(element.get("field") or "")
            if parts and len(parts) == 2 and parts[0] == ds["name"]:
                if instances.get(parts[1], R.instance_field(parts[1])) == name:
                    found.append(element)
        if not found:
            have = ", ".join(f["caption"] for f in sheet_view.get("formats", [])) or "none"
            raise PatchRefused(f"sheet {target.get('name')!r} has no format of its own for {item['caption']!r} (it has: {have}); "
                               f"leave out --sheet to change the field's default format")
        out = _Plan(f"set the format of {item['caption']!r} on sheet {target.get('name')!r} to {value!r}")
        for element in found:
            _set_attr(doc, out, element, "value", value)
        out.overrides.sheet_formats[(target.get("name", ""), ds["name"], name)] = value
        return out

    return "set-format", plan


def _parse_pairs(pairs: list[str] | None, what: str) -> list[tuple[str, str]]:
    out = []
    for pair in pairs or []:
        old, sep, new = pair.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise PatchRefused(f"{what} {pair!r} must be OLD=NEW")
        out.append((old.strip().strip("`"), new.strip().strip("`")))
    return out


def swap_datasource(datasource: str, project: str | None = None, dataset: str | None = None,
                    billing_project: str | None = None, tables: list[str] | None = None, columns: list[str] | None = None):
    """Point a data source at other warehouse tables.

    * ``project``: the connection's project, and every table it reads in that project (connection tables and the
      tables its custom SQL names) moves to the new project;
    * ``dataset``: the connection's dataset, and every table it reads in that dataset moves to the new one;
    * ``billing_project``: the project the queries run and are billed in;
    * ``tables``: ``OLD=NEW`` per table, matched on the last parts of the full name (``orders=orders_v2``,
      ``sales.orders=staging.orders``);
    * ``columns``: ``OLD=NEW`` per warehouse column of a connection table (a column renamed in the warehouse); the field
      keeps its internal name, so every formula, sheet and filter that uses it is untouched.

    Custom SQL is edited only where it names a table that moves, at that table's exact span, and checked by parsing the
    new SQL with sqlglot: the tree must equal the old tree with the table names mapped. A table the SQL writes without
    its project keeps that short form when it still resolves to the moved table. Only BigQuery connections are swapped."""

    def plan(doc: Document, view: dict) -> _Plan:
        ds = _datasource(view, datasource)[0]
        if not any((project, dataset, billing_project, tables, columns)):
            raise PatchRefused("nothing to swap: give --project, --dataset, --billing-project, --table or --column")
        table_map = _parse_pairs(tables, "--table")
        column_map = dict(_parse_pairs(columns, "--column"))
        element = _ds_element(doc, ds["name"])
        connections = tableau.connections_of(element)
        if not connections:
            raise PatchRefused(f"data source {ds['caption']!r} has no warehouse connection")
        out = _Plan(f"swap data source {ds['caption']!r}: " + ", ".join(
            part for part in (f"project {project}" if project else "", f"dataset {dataset}" if dataset else "",
                              f"billing project {billing_project}" if billing_project else "",
                              *(f"table {a} -> {b}" for a, b in table_map), *(f"column {a} -> {b}" for a, b in column_map.items()))
            if part))
        old_connections: dict[str, dict] = {}
        new_connections: dict[str, dict] = {}
        for conn_name, conn in connections:
            info = tableau._connection_view(conn)
            if info.get("class") != "bigquery":
                raise PatchRefused(f"data source {ds['caption']!r} connects with {info.get('class')!r}; only BigQuery "
                                   f"connections are swapped")
            old_connections[conn_name] = info
            changed = dict(info)
            for key, value, attrs in (("project", project, ("CATALOG", "project")), ("dataset", dataset, ("schema", "dataset", "bq_schema")),
                                      ("billing_project", billing_project, ("EXECCATALOG",))):
                if value is None:
                    continue
                present = [a for a in attrs if conn.get(a) is not None]
                if not present:
                    raise PatchRefused(f"connection {conn_name or ds['caption']!r} has no {key} attribute to change")
                for attr in present:
                    _set_attr(doc, out, conn, attr, value)
                changed[key] = value
            new_connections[conn_name] = changed
            if changed != info:
                out.overrides.connections[(ds["name"], conn_name)] = {k: v for k, v in changed.items() if info.get(k) != v}

        table_hits: set[str] = set()

        def move(full: str, conn_name: str) -> str:
            """Where table ``full`` goes: the project and dataset moves first, then a ``--table`` rename matched on the
            table's name before the swap."""

            original = full.split(".")
            parts = list(original)
            old = old_connections.get(conn_name) or next(iter(old_connections.values()))
            if len(parts) == 3:
                if project and parts[0] == old.get("project"):
                    parts[0] = project
                if dataset and original[0] == old.get("project") and parts[1] == old.get("dataset"):
                    parts[1] = dataset
            for old_name, new_name in table_map:
                want = old_name.split(".")
                if len(want) <= len(original) and [p.lower() for p in original[-len(want):]] == [p.lower() for p in want]:
                    table_hits.add(old_name)
                    replacement = new_name.split(".")
                    if len(replacement) > len(parts):
                        raise PatchRefused(f"--table {old_name}={new_name}: the new name has more parts than {full}")
                    parts = parts[:len(parts) - len(replacement)] + replacement
            return ".".join(parts)

        moved_tables = []
        for relation in tableau._relations(element):  # the data source's own connection, not an extract's
            conn_name = relation.get("connection", "")
            conn_info = old_connections.get(conn_name) or next(iter(old_connections.values()))
            new_info = new_connections.get(conn_name) or next(iter(new_connections.values()))
            if relation.get("type") == "table":
                full, _ = tableau.relation_table(relation.get("table"), conn_info)
                if full is None:
                    continue
                target = move(full, conn_name)
                if target == full and tableau.relation_table(relation.get("table"), new_info)[0] == full:
                    continue  # not moving, and the new connection reads the same table from what is written
                written = _write_relation_table(relation.get("table") or "", target, new_info)
                _set_attr(doc, out, relation, "table", written)
                out.overrides.tables[(ds["name"], relation.get("name", ""))] = target
                if f"{full} -> {target}" not in moved_tables:
                    moved_tables.append(f"{full} -> {target}")
            elif project or dataset or billing_project or table_map:  # a column remap alone leaves custom SQL alone
                new_sql, moved, kept = _swap_sql(relation.text or "", conn_info, new_info, lambda t: move(t, conn_name))
                for written, full in kept:
                    out.warnings.append(f"custom SQL {relation.get('name')!r} wrote {written}, which would now resolve elsewhere; "
                                        f"it is written out as {full} so it still reads the same table")
                if new_sql != (relation.text or ""):
                    _set_text(doc, out, relation, new_sql)
                    out.overrides.sql[(ds["name"], relation.get("name", ""))] = new_sql
                    moved_tables.extend(f"{a} -> {b} (custom SQL {relation.get('name')!r})" for a, b in moved)
                    out.checks.append(f"custom SQL {relation.get('name')!r} re-parsed with sqlglot: the same tree with "
                                      f"its table names mapped")
        for old_name, _ in table_map:
            if old_name not in table_hits:
                raise PatchRefused(f"--table {old_name}: data source {ds['caption']!r} reads no such table")
        if column_map:
            _swap_columns(doc, out, element, ds, column_map)
        if not out.edits:
            raise PatchRefused("the data source already reads those tables; nothing to change")
        if moved_tables:
            out.checks.append("tables moved: " + "; ".join(moved_tables))
        return out

    return "swap-datasource", plan


def _write_relation_table(written: str, target: str, connection: dict) -> str:
    """The ``table`` attribute for ``target`` in the shape the original used (``[p.d].[t]``, ``[p].[d].[t]``, ``[d].[t]``)."""

    parts = R.split_ref(written) or ()
    project, dataset, table = target.split(".")
    if len(parts) == 2 and "." in parts[0]:
        return f"{R.bracket(project + '.' + dataset)}.{R.bracket(table)}"
    if len(parts) == 3:
        return ".".join(R.bracket(p) for p in (project, dataset, table))
    if len(parts) == 2:
        if connection.get("project") != project:
            return f"{R.bracket(project + '.' + dataset)}.{R.bracket(table)}"
        return f"{R.bracket(dataset)}.{R.bracket(table)}"
    if connection.get("project") != project or connection.get("dataset") != dataset:
        return f"{R.bracket(project + '.' + dataset)}.{R.bracket(table)}"
    return R.bracket(table)


def _swap_sql(sql: str, old_conn: dict, new_conn: dict, move) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Custom SQL with each table that moves rewritten at its span; refused if the result does not re-parse to the same
    tree with the table names mapped."""

    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    _, reason = tableau.custom_sql_tables(sql, old_conn)
    if reason:
        raise PatchRefused(f"custom SQL cannot be parsed, so whether its tables move cannot be told: {reason}")
    trees = tableau.parse_bigquery(sql)  # offsets below are offsets in ``sql``: parameters are padded to their length
    replacements = []
    moved: list[tuple[str, str]] = []
    kept: list[tuple[str, str]] = []
    mapping: dict[tuple[str, ...], tuple[str, ...]] = {}
    for tree in trees:
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if not table.name or (not table.db and not table.catalog and table.name.lower() in ctes):
                continue
            written = [p for p in (table.catalog, table.db, table.name) if p]
            full, _ = tableau.complete_sql_table(written, old_conn)
            target = move(full)
            if target == full:
                still, _ = tableau.complete_sql_table(written, new_conn)
                if still == full:
                    continue
                target = full  # the swap changes what a short name resolves to: write the table out in full
                kept.append((".".join(written), full))
            short, _ = tableau.complete_sql_table(written, new_conn)
            if short == target:
                if target != full:
                    moved.append((full, target))
                continue  # the short form still resolves to the moved table
            identifiers = [p for p in table.parts if isinstance(p, exp.Identifier) and "start" in p.meta]
            if len(identifiers) != len(table.parts):
                raise PatchRefused(f"custom SQL table {'.'.join(written)}: sqlglot did not report where it is written")
            start = min(p.meta["start"] for p in identifiers)
            end = max(p.meta["end"] for p in identifiers) + 1
            source = sql[start:end]
            if source.replace("`", "").replace(" ", "").lower() != ".".join(written).lower():
                raise PatchRefused(f"custom SQL table {'.'.join(written)} is written as {source!r}; not edited")
            quote = source.startswith("`") or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in target.split("."))
            replacements.append((start, end, f"`{target}`" if quote else target))
            mapping[tuple(p.lower() for p in written)] = tuple(target.split("."))
            if target != full:
                moved.append((full, target))
    if not replacements:
        return sql, moved, kept
    replacements.sort()
    for (_, end, _), (start, _, _) in zip(replacements, replacements[1:]):
        if start < end:
            raise PatchRefused("two custom SQL tables overlap; not edited")
    out, cursor = [], 0
    for start, end, text in replacements:
        out.append(sql[cursor:start])
        out.append(text)
        cursor = end
    out.append(sql[cursor:])
    new_sql = "".join(out)

    def renamed(node):
        if isinstance(node, exp.Table):
            key = tuple(p.lower() for p in (node.catalog, node.db, node.name) if p)
            if key in mapping:
                project, dataset, table = mapping[key]
                node = node.copy()
                node.set("catalog", exp.to_identifier(project))
                node.set("db", exp.to_identifier(dataset))
                node.set("this", exp.to_identifier(table))
        return node

    def plain(tree):
        """The SQL with table names unquoted, since a quoted and an unquoted name are the same table."""

        tree = tree.copy()
        for table in tree.find_all(exp.Table):
            table.meta.pop("quoted_table", None)
            for part in table.parts:
                if isinstance(part, exp.Identifier):
                    part.set("quoted", False)
        return tree.sql(dialect="bigquery")

    try:
        new_trees = tableau.parse_bigquery(new_sql)
    except SqlglotError as exc:
        raise PatchRefused(f"the edited custom SQL does not parse: {exc}") from exc
    expected = [plain(t.transform(renamed)) for t in trees]
    actual = [plain(t) for t in new_trees]
    if expected != actual:
        raise PatchRefused("the edited custom SQL is not the old query with the table names mapped; not edited")
    return new_sql, moved, kept


def _swap_columns(doc: Document, out: _Plan, element: ET.Element, ds: dict, column_map: dict[str, str]) -> None:
    """Point fields at renamed warehouse columns: ``<cols><map value>``, ``<remote-name>`` and ``<remote-alias>``."""

    relations = {r["name"]: r for r in ds["relations"]}
    connection = next((c for c in element if local(c.tag) == "connection"), None)  # not an extract's connection
    if connection is None:
        raise PatchRefused(f"data source {ds['caption']!r} has no connection")
    maps: dict[str, list[ET.Element]] = {}
    for mapping in connection.iter():
        if local(mapping.tag) == "map" and mapping.get("key"):
            maps.setdefault(mapping.get("key"), []).append(mapping)
    hits = {old: 0 for old in column_map}
    in_sql: dict[str, str] = {}
    for record in connection.iter():
        if local(record.tag) != "metadata-record" or record.get("class") != "column":
            continue
        remote = next((c for c in record if local(c.tag) == "remote-name"), None)
        parent = next((c for c in record if local(c.tag) == "parent-name"), None)
        local_name = next((c for c in record if local(c.tag) == "local-name"), None)
        if remote is None or remote.text not in column_map:
            continue
        relation = R.split_ref(parent.text or "")[0] if parent is not None and R.split_ref(parent.text or "") else ""
        if relations.get(relation, {}).get("type") != "table":
            in_sql[remote.text] = relation  # a custom SQL column: its name comes from the SQL, which is not edited
            continue
        new = column_map[remote.text]
        hits[remote.text] += 1
        field_name = R.split_ref(local_name.text or "")[0] if local_name is not None and R.split_ref(local_name.text or "") else ""
        _set_text(doc, out, remote, new)
        alias = next((c for c in record if local(c.tag) == "remote-alias"), None)
        if alias is not None and alias.text == remote.text:
            _set_text(doc, out, alias, new)
        for mapping in maps.get(R.bracket(field_name), []):
            parts = R.split_ref(mapping.get("value") or "")
            if parts and parts[-1] == remote.text and (len(parts) == 1 or parts[0] == relation):
                _set_attr(doc, out, mapping, "value", ".".join(R.bracket(p) for p in (*parts[:-1], new)))
        out.overrides.remote_columns[(ds["name"], field_name)] = new
    for old, count in hits.items():
        if count:
            continue
        if old in in_sql:
            raise PatchRefused(f"column {old!r} comes from custom SQL {in_sql[old]!r}; change the SQL instead")
        raise PatchRefused(f"data source {ds['caption']!r} reads no warehouse column {old!r}")
    out.checks.append("columns remapped: " + ", ".join(f"{a} -> {b}" for a, b in column_map.items())
                      + "; fields keep their internal names, so formulas, sheets and filters are unchanged")


# ------------------------------------------------------------------------------------------------------------- running


def _xml_differences(before: Document, after: Document) -> set[tuple[int, str]]:
    if len(before.elements) != len(after.elements):
        raise PatchRefused(f"the edit changed the number of XML elements ({len(before.elements)} -> {len(after.elements)})")
    changed: set[tuple[int, str]] = set()
    for number, (old, new) in enumerate(zip(before.elements, after.elements)):
        if old.tag != new.tag:
            raise PatchRefused(f"element {number} changed from <{old.tag}> to <{new.tag}>")
        for key in set(old.attrib) | set(new.attrib):
            if old.attrib.get(key) != new.attrib.get(key):
                changed.add((number, key))
        if (old.text or "") != (new.text or ""):
            changed.add((number, "#text"))
        if (old.tail or "") != (new.tail or ""):
            changed.add((number, "#tail"))
    return changed


def _short(value, limit: int = 120) -> str:
    import json

    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


_IDENTITY = ("datasource", "name", "field", "member", "sheet")


def _identity(item) -> tuple | None:
    if not isinstance(item, dict):
        return None
    key = tuple((k, item[k]) for k in _IDENTITY if isinstance(item.get(k), str))
    return key or None


def _label(key: tuple) -> str:
    values = dict(key)
    return values.get("name") or values.get("member") or values.get("field") or values.get("sheet") or str(key)


def view_changes(before, after, path: str = "") -> list[str]:
    """Every difference between two views as ``path: old -> new``.

    List items that carry a name (fields, sheets, filters, colors) are matched by it, so a renamed field that sorts
    elsewhere is reported once, as a changed caption, and not as a shuffled list."""

    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for key in sorted(set(before) | set(after), key=str):
            if key not in before:
                out.append(f"{path}/{key}: added {_short(after[key])}")
            elif key not in after:
                out.append(f"{path}/{key}: removed (was {_short(before[key])})")
            elif before[key] != after[key]:
                out += view_changes(before[key], after[key], f"{path}/{key}")
        return out
    if isinstance(before, list) and isinstance(after, list):
        old_keys = [_identity(item) for item in before]
        new_keys = [_identity(item) for item in after]
        keyed = (None not in old_keys and None not in new_keys and len(set(old_keys)) == len(old_keys)
                 and len(set(new_keys)) == len(new_keys))
        if keyed:
            old_items, new_items = dict(zip(old_keys, before)), dict(zip(new_keys, after))
            out = []
            for key in old_keys + [k for k in new_keys if k not in old_items]:
                label = f"{path}[{_label(key)}]"
                if key not in new_items:
                    out.append(f"{label}: removed")
                elif key not in old_items:
                    out.append(f"{label}: added {_short(new_items[key])}")
                elif old_items[key] != new_items[key]:
                    out += view_changes(old_items[key], new_items[key], label)
            if not out and old_keys != new_keys:
                out.append(f"{path}: same items in another order")
            return out
        if len(before) == len(after):
            out = []
            for number, (a, b) in enumerate(zip(before, after)):
                if a != b:
                    out += view_changes(a, b, f"{path}[{number}]")
            return out
    return [f"{path or '/'}: {_short(before)} -> {_short(after)}"]


def _paths(before, after, path: str = "") -> list[str]:
    """Where two views differ, as short paths, for a refusal message."""

    if type(before) is not type(after):
        return [path or "/"]
    if isinstance(before, dict):
        out = []
        for key in sorted(set(before) | set(after), key=str):
            if before.get(key) != after.get(key):
                out += _paths(before.get(key), after.get(key), f"{path}/{key}")
        return out
    if isinstance(before, list):
        if len(before) != len(after):
            return [f"{path} ({len(before)} -> {len(after)} items)"]
        out = []
        for number, (a, b) in enumerate(zip(before, after)):
            if a != b:
                label = a.get("name") if isinstance(a, dict) and a.get("name") else number
                out += _paths(a, b, f"{path}[{label}]")
        return out
    return [path or "/"] if before != after else []


def unified_diff(old: list[str], new: list[str], name: str, context: int = 1) -> list[str]:
    """A unified diff of two texts. Most patches change values inside lines, so when the line counts agree the lines are
    compared pairwise (linear, where difflib on a 17,000-line workbook takes seconds); otherwise difflib does it."""

    if len(old) != len(new):
        return list(difflib.unified_diff(old, new, f"a/{name}", f"b/{name}", n=context, lineterm=""))
    changed = [number for number, (a, b) in enumerate(zip(old, new)) if a != b]
    if not changed:
        return []
    out = [f"--- a/{name}", f"+++ b/{name}"]
    groups: list[list[int]] = []
    for number in changed:
        if groups and number - groups[-1][-1] <= 2 * context + 1:
            groups[-1].append(number)
        else:
            groups.append([number])
    for group in groups:
        start, end = max(0, group[0] - context), min(len(old), group[-1] + context + 1)
        out.append(f"@@ -{start + 1},{end - start} +{start + 1},{end - start} @@")
        for number in range(start, end):
            if old[number] == new[number]:
                out.append(" " + old[number])
            else:
                out.append("-" + old[number])
                out.append("+" + new[number])
    return out


def run(book: Workbook, patch) -> PatchResult:
    """Plan, apply in memory, re-parse, diff and check one patch (see the module docstring). Raises :class:`PatchRefused`."""

    kind, planner = patch
    text, bom = _decode(book)
    try:
        doc = scan(text)
    except XmlEditError as exc:
        raise PatchRefused(f"the workbook cannot be edited in place: {exc}") from exc
    before = tableau.build_view(doc.root, file_name=book.label)
    try:
        plan = planner(doc, before)
        new_text = apply_edits(text, plan.edits)
    except XmlEditError as exc:
        raise PatchRefused(str(exc)) from exc
    try:
        new_doc = scan(new_text)
    except XmlEditError as exc:
        raise PatchRefused(f"the edited workbook does not parse again ({exc}); nothing was written") from exc
    changed = _xml_differences(doc, new_doc)
    if changed - plan.touched:
        extra = sorted(changed - plan.touched)[:5]
        raise PatchRefused(f"the edit changed XML it did not plan to: {extra}")
    touched_unchanged = {t for t in plan.touched - changed}
    expected = tableau.build_view(doc.root, plan.overrides, file_name=book.label)
    actual = tableau.build_view(new_doc.root, file_name=book.label)
    if actual != expected:
        where = _paths(expected, actual)[:8]
        raise PatchRefused("the patched workbook differs from the intended change at " + ", ".join(where)
                           + "; nothing was written")
    new_problems = sorted(set(actual["problems"]) - set(before["problems"]))
    if new_problems:
        raise PatchRefused("the patch would add problems: " + "; ".join(new_problems))
    new_xml = bom + new_text.encode("utf-8")
    original = book.original
    if book.kind == "twbx":
        try:
            patched = replace_member(original, book.member or "", new_xml)
        except PackageError as exc:
            raise PatchRefused(f"the packaged workbook cannot be rewritten safely: {exc}") from exc
        checks_package = ["the .twbx was read back: every other member is byte for byte the same"]
    else:
        patched = new_xml
        checks_package = []
    view_diff = [line for line in difflib.unified_diff(
        render_text(before, max_chars=10**9).splitlines(), render_text(actual, max_chars=10**9).splitlines(),
        "before", "after", n=0, lineterm="") if not line.startswith("@@")]
    name = book.member or book.name
    xml_diff = unified_diff(text.splitlines(), new_text.splitlines(), name)
    checks = [
        "re-parsed with xml.etree and the span scanner",
        f"XML diff: {len(changed)} {'value' if len(changed) == 1 else 'values'} changed, all of them planned; "
        f"same {len(new_doc.elements)} elements in the same order",
        "view diff: the patched view equals the original view with the intended change",
        *plan.checks, *checks_package,
    ]
    if touched_unchanged:
        checks.append(f"{len(touched_unchanged)} planned values already had the new value")
    return PatchResult(kind=kind, description=plan.description, workbook=book, original=original, patched=patched,
                       changes=view_changes(before, actual), view_diff=view_diff, xml_diff=xml_diff, edits=len(plan.edits), checks=checks, warnings=plan.warnings)


def write_result(result: PatchResult, target: Path) -> None:
    """Write the patched file atomically. Writing over the source checks first that it still holds what was read."""

    target = Path(target)
    source = result.workbook.path
    if source is not None and target.resolve() == source.resolve():
        if source.read_bytes() != result.original:
            raise PatchRefused(f"{source} changed on disk since it was read; nothing was written")
    elif target.exists() and target.is_dir():
        raise PatchRefused(f"{target} is a folder")
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent or Path(".")))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(result.patched)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def patch_workbook(book: Workbook, patch) -> PatchResult:
    """Alias of :func:`run` for callers that read better with it."""

    return run(book, patch)


__all__ += ["run", "patch_workbook", "WorkbookError"]
