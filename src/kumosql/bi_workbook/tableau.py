"""Read a Tableau workbook (``.twb``, or the ``.twb`` inside a ``.twbx``) into a compact, deterministic view.

The view is a plain dict (the JSON form) with:

* ``datasources``: each data source's caption and internal name, its connections (class, project, dataset), its
  relations (warehouse tables and custom SQL, with the tables the SQL reads), its fields (calculated fields with their
  formula as written and with internal names such as ``[Calculation_1368…]`` replaced by captions), data source filters,
  default formats and member colors;
* ``parameters``;
* ``worksheets``: the data sources each sheet uses, the fields it uses directly, the fields it reaches only through
  calculated fields, its filters, shelves, mark colors and number formats;
* ``dashboards``: the sheets on each dashboard or story, and its filter and parameter controls;
* ``problems``: what does not add up, such as a formula naming a field the data source does not have, or two copies of
  one field declaration that disagree. A workbook Tableau opens cleanly normally has none.

:func:`build_view` takes an optional :class:`Overrides`: the semantic change a patch intends ("this field's caption is
now X"). The patch module compares the view of the patched file against the view of the original with the override,
so a patch is accepted only when the file changed exactly as intended (see :mod:`kumosql.bi_workbook.patch`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable

from . import refs as R

__all__ = ["Overrides", "build_view", "local", "relation_table", "custom_sql_tables", "PARAMETERS"]

PARAMETERS = "Parameters"
_USER_NS = "{http://www.tableausoftware.com/xml/user}"


@dataclass
class Overrides:
    """What a patch means to change, keyed by internal names. Empty means "read the file as it is"."""

    captions: dict[tuple[str, str], str] = field(default_factory=dict)  # (datasource, field) -> caption
    connections: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)  # (datasource, connection) -> key -> value
    tables: dict[tuple[str, str], str] = field(default_factory=dict)  # (datasource, relation) -> project.dataset.table
    sql: dict[tuple[str, str], str] = field(default_factory=dict)  # (datasource, relation) -> custom SQL text
    remote_columns: dict[tuple[str, str], str] = field(default_factory=dict)  # (datasource, field) -> warehouse column
    default_formats: dict[tuple[str, str], str] = field(default_factory=dict)  # (datasource, field) -> format
    sheet_formats: dict[tuple[str, str, str], str] = field(default_factory=dict)  # (sheet, datasource, field) -> format
    mark_colors: dict[str, str] = field(default_factory=dict)  # sheet -> color
    member_colors: dict[tuple[str, str, str], str] = field(default_factory=dict)  # (datasource, field, member) -> color


def local(tag: str) -> str:
    """Tableau prefixes some tags with feature flags (``_.fcp.ObjectModelEncapsulateLegacy.false...relation``)."""

    return tag.rsplit("...", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if local(child.tag) == name]


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in element:
        if local(child.tag) == name:
            return child
    return None


def _descendants(element: ET.Element, name: str) -> Iterable[ET.Element]:
    return (node for node in element.iter() if local(node.tag) == name)


def _unbracket(name: str | None) -> str:
    parts = R.split_ref(name or "")
    return parts[-1] if parts else (name or "")


# ---------------------------------------------------------------------------------------------------------- connections

_CONNECTION_KEYS = (
    ("class", ("class",)),
    ("project", ("CATALOG", "project")),
    ("dataset", ("schema", "dataset")),
    ("billing_project", ("EXECCATALOG",)),
    ("server", ("server",)),
    ("dbname", ("dbname",)),
    ("warehouse", ("warehouse",)),
    ("filename", ("filename",)),
)


def _connection_view(conn: ET.Element) -> dict[str, str]:
    out = {}
    for key, attrs in _CONNECTION_KEYS:
        for attr in attrs:
            value = conn.get(attr)
            if value:
                out[key] = value
                break
    return out


def relation_table(table: str | None, connection: dict[str, str]) -> tuple[str | None, bool]:
    """The warehouse table of a ``table='[project.dataset].[table]'`` attribute, filled from the connection when the
    attribute leaves out the project or dataset. Returns the name and whether it was complete as written."""

    parts = R.split_ref(table or "")
    if not parts:
        return None, False
    names: list[str] = []
    for number, part in enumerate(parts):
        if number < len(parts) - 1 and "." in part:
            names.extend(part.rsplit(".", 1))  # "[acme.sales].[orders]"; a project id may hold ":" but never "."
        else:
            names.append(part)
    complete = len(names) >= 3
    if len(names) == 2 and connection.get("project"):
        names.insert(0, connection["project"])
    elif len(names) == 1:
        if connection.get("dataset"):
            names.insert(0, connection["dataset"])
        if connection.get("project") and len(names) == 2:
            names.insert(0, connection["project"])
    return ".".join(names), complete


_PARAMETER_IN_SQL = re.compile(r"<\[Parameters\]\.\[(?:[^\]]|\]\])*\]>")


def without_parameters(sql: str) -> str:
    """The SQL with each Tableau parameter replaced by ``NULL`` padded to the same length, so offsets stay the same."""

    return _PARAMETER_IN_SQL.sub(lambda m: "NULL".ljust(len(m.group(0))), sql)


def parse_bigquery(sql: str) -> list:
    """sqlglot's trees for ``sql`` (BigQuery dialect, errors raised), with sqlglot's fallback warnings kept quiet:
    a statement it does not understand comes back as ``exp.Command`` and the caller reports it."""

    import logging

    import sqlglot
    from sqlglot.errors import ErrorLevel

    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        return [tree for tree in sqlglot.parse(without_parameters(sql), read="bigquery", error_level=ErrorLevel.RAISE)
                if tree is not None]
    finally:
        logger.setLevel(level)


def custom_sql_tables(sql: str, connection: dict[str, str]) -> tuple[list[dict], str | None]:
    """The warehouse tables a custom SQL query reads, parsed with sqlglot in the BigQuery dialect.

    Returns ``(tables, None)``, or ``([], reason)`` when the SQL cannot be parsed: an unparsed query is reported, never
    guessed at. Tableau parameters (``<[Parameters].[Name]>``) are values, so each is read as ``NULL`` for parsing.

    BigQuery resolves ``dataset.table`` in the project that runs the query, which for a Tableau connection is its billing
    project (``EXECCATALOG``) when it has one, else its project; such a table is completed that way and keeps
    ``written`` (as the SQL spells it) and ``completed_from``. A bare ``table`` is completed with the connection's
    dataset too, marked ``completed_from: "connection dataset"``; BigQuery needs a default dataset for that, so check it."""

    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        statements = parse_bigquery(sql)
    except SqlglotError as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return [], f"sqlglot could not parse it (BigQuery dialect): {first[:200]}"
    except RecursionError:
        return [], "sqlglot could not parse it: the query nests too deeply"
    if not statements:
        return [], "the custom SQL is empty"
    for statement in statements:
        if isinstance(statement, exp.Command) or not isinstance(statement, (exp.Query, exp.DDL, exp.DML)):
            return [], f"the custom SQL is not a query sqlglot understands ({type(statement).__name__})"
    found: dict[str, dict] = {}
    for statement in statements:
        ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            if not table.name:
                continue
            if not table.db and not table.catalog and table.name.lower() in ctes:
                continue
            name, entry = complete_sql_table([part for part in (table.catalog, table.db, table.name) if part], connection)
            found.setdefault(name, {"table": name, **entry})
    return [found[name] for name in sorted(found)], None


def sql_default_project(connection: dict[str, str]) -> tuple[str | None, str]:
    """The project a ``dataset.table`` in custom SQL resolves in, and where that came from."""

    if connection.get("billing_project"):
        return connection["billing_project"], "billing project"
    return connection.get("project"), "connection project"


def complete_sql_table(names: list[str], connection: dict[str, str]) -> tuple[str, dict]:
    """``project.dataset.table`` for the parts a custom SQL query wrote, and ``{"written", "completed_from"}`` when filled in."""

    names = list(names)
    written = ".".join(names)
    sources = []
    if len(names) == 1 and connection.get("dataset"):
        names.insert(0, connection["dataset"])
        sources.append("connection dataset")
    project, source = sql_default_project(connection)
    if len(names) == 2 and project:
        names.insert(0, project)
        sources.append(source)
    name = ".".join(names)
    if name == written:
        return name, {}
    return name, {"written": written, "completed_from": " and ".join(sources)}


# ----------------------------------------------------------------------------------------------------------- the model


class _Model:
    """Everything :func:`build_view` reads, in one pass over the tree; captions resolved through the overrides."""

    def __init__(self, root: ET.Element, overrides: Overrides):
        self.root = root
        self.o = overrides
        self.problems: list[str] = []
        self.datasources: list[ET.Element] = []
        datasources = _child(root, "datasources")
        if datasources is not None:
            self.datasources = _children(datasources, "datasource")
        self.ds_by_name = {ds.get("name", ""): ds for ds in self.datasources}
        self.ds_caption = {name: (ds.get("caption") or name) for name, ds in self.ds_by_name.items()}
        # fields of each data source: internal name (no brackets) -> declaration
        self.fields: dict[str, dict[str, dict]] = {}
        for name, ds in self.ds_by_name.items():
            self.fields[name] = self._fields(name, ds)
        self._check_copies()

    # --------------------------------------------------------------------------------------------------- fields

    def _fields(self, ds_name: str, ds: ET.Element) -> dict[str, dict]:
        fields: dict[str, dict] = {}
        connection = _child(ds, "connection")
        if connection is not None:
            for record in _descendants(connection, "metadata-record"):
                if record.get("class") != "column":
                    continue
                local_name = _unbracket(_text(record, "local-name"))
                if not local_name:
                    continue
                fields.setdefault(local_name, {"name": local_name, "kind": "column"})
                entry = fields[local_name]
                remote = _text(record, "remote-name")
                parent = _unbracket(_text(record, "parent-name"))
                entry["_remote_record"] = (parent, remote)
                if _text(record, "local-type") and "datatype" not in entry:
                    entry["datatype"] = _text(record, "local-type")
            for cols in _descendants(connection, "cols"):
                for mapping in _children(cols, "map"):
                    key = _unbracket(mapping.get("key"))
                    value = R.split_ref(mapping.get("value") or "")
                    if key and value:
                        fields.setdefault(key, {"name": key, "kind": "column"})
                        fields[key]["_remote_map"] = (value[0], value[-1]) if len(value) == 2 else ("", value[-1])
        for column in _children(ds, "column"):
            name = _unbracket(column.get("name"))
            if not name:
                continue
            entry = fields.setdefault(name, {"name": name, "kind": "column"})
            entry["_declared"] = True
            for attr in ("datatype", "role", "type"):
                if column.get(attr):
                    entry[attr] = column.get(attr)
            if column.get("caption") is not None:
                entry["_caption"] = column.get("caption")
            if column.get("default-format") is not None:
                entry["default_format"] = column.get("default-format")
            if column.get("hidden") == "true":
                entry["hidden"] = True
            calculation = _child(column, "calculation")
            if calculation is not None:
                cls = calculation.get("class")
                if ds_name == PARAMETERS or column.get("param-domain-type"):
                    entry["kind"] = "parameter"
                    entry["value"] = column.get("value") if column.get("value") is not None else calculation.get("formula")
                    if column.get("param-domain-type"):
                        entry["domain"] = column.get("param-domain-type")
                elif cls == "tableau" and calculation.get("formula") is not None:
                    entry["kind"] = "calculated"
                    entry["formula_raw"] = calculation.get("formula")
                elif cls == "categorical-bin":
                    entry["kind"] = "group"
                    entry["_source"] = _unbracket(calculation.get("column"))
                elif cls in ("bin", "quantitative-bin"):
                    entry["kind"] = "bin"
                    entry["_source"] = _unbracket(calculation.get("column"))
                    if calculation.get("formula"):
                        entry["formula_raw"] = calculation.get("formula")
        for entry in fields.values():
            key = (ds_name, entry["name"])
            if key in self.o.remote_columns:
                record = entry.get("_remote_record") or entry.get("_remote_map")
                relation = record[0] if record else ""
                entry["remote"] = f"{relation}.{self.o.remote_columns[key]}" if relation else self.o.remote_columns[key]
            else:
                record, mapping = entry.get("_remote_record"), entry.get("_remote_map")
                chosen = record or mapping
                if chosen:
                    entry["remote"] = f"{chosen[0]}.{chosen[1]}" if chosen[0] else chosen[1]
                if record and mapping and (record[1] != mapping[1] or (record[0] and mapping[0] and record[0] != mapping[0])):
                    self.problems.append(
                        f"data source {self.ds_caption.get(ds_name, ds_name)!r}: field [{entry['name']}] maps to "
                        f"{mapping[0]}.{mapping[1]} in <cols> but its metadata record says {record[0]}.{record[1]}"
                    )
            if key in self.o.default_formats:
                entry["default_format"] = self.o.default_formats[key]
        return fields

    def caption(self, ds_name: str, name: str) -> str:
        override = self.o.captions.get((ds_name, name))
        if override is not None:
            return override
        entry = self.fields.get(ds_name, {}).get(name)
        if entry and entry.get("_caption"):
            return entry["_caption"]
        return name

    def known(self, ds_name: str, name: str) -> bool:
        return name in self.fields.get(ds_name, {})

    def _check_copies(self) -> None:
        """A sheet keeps a copy of each field declaration it depends on; the copies must agree with the data source."""

        for sheet in self.worksheets():
            for deps in _descendants(sheet, "datasource-dependencies"):
                ds_name = deps.get("datasource", "")
                for column in _children(deps, "column"):
                    name = _unbracket(column.get("name"))
                    entry = self.fields.get(ds_name, {}).get(name)
                    if entry is None:
                        continue
                    key = (ds_name, name)
                    if key in self.o.captions:
                        continue  # a caption patch states the copies' caption too
                    copy = column.get("caption")
                    own = entry.get("_caption")
                    if own is not None and copy is not None and copy != own:
                        self.problems.append(
                            f"sheet {sheet.get('name')!r} declares [{name}] with caption {copy!r}, the data source with {own!r}")
                    if (ds_name, name) not in self.o.default_formats:
                        own_format, copy_format = entry.get("default_format"), column.get("default-format")
                        if own_format is not None and copy_format is not None and own_format != copy_format:
                            self.problems.append(f"sheet {sheet.get('name')!r} declares [{name}] with default format "
                                                 f"{copy_format!r}, the data source with {own_format!r}")
                    calculation = _child(column, "calculation")
                    if calculation is not None and entry.get("formula_raw") is not None and calculation.get("formula") != entry["formula_raw"]:
                        self.problems.append(f"sheet {sheet.get('name')!r} has a different formula for [{name}] than the data source")

    def worksheets(self) -> list[ET.Element]:
        worksheets = _child(self.root, "worksheets")
        return _children(worksheets, "worksheet") if worksheets is not None else []

    def dashboards(self) -> list[ET.Element]:
        dashboards = _child(self.root, "dashboards")
        return _children(dashboards, "dashboard") if dashboards is not None else []

    # ------------------------------------------------------------------------------------------------- formulas

    def resolve(self, ds_name: str, ref: tuple[str, ...]) -> tuple[str, str] | None:
        """``(datasource, field)`` a formula reference names, or ``None`` when it names nothing known."""

        if len(ref) == 2:
            target_ds, name = ref
            if target_ds in self.ds_by_name and self.known(target_ds, name):
                return target_ds, name
            return None
        if self.known(ds_name, ref[0]):
            return ds_name, ref[0]
        return None

    def formula_text(self, ds_name: str, formula: str, owner: str) -> tuple[str, list[tuple[str, str]]]:
        """The formula with internal names replaced by captions, and the fields it references, in order."""

        out = []
        cursor = 0
        used: list[tuple[str, str]] = []
        for ref in R.formula_refs(formula):
            target = self.resolve(ds_name, ref.parts)
            out.append(formula[cursor:ref.start])
            if target is None:
                out.append(formula[ref.start:ref.end])
                if not (len(ref.parts) == 1 and ref.parts[0].startswith(":")):
                    self.problems.append(
                        f"the formula of {owner!r} refers to {formula[ref.start:ref.end]}, which is not a field of "
                        f"{self.ds_caption.get(ds_name, ds_name)!r}")
            else:
                target_ds, name = target
                caption = self.caption(target_ds, name)
                if target_ds != ds_name or len(ref.parts) == 2:
                    out.append(R.bracket(self.ds_caption.get(target_ds, target_ds)) + "." + R.bracket(caption))
                else:
                    out.append(R.bracket(caption))
                if target not in used:
                    used.append(target)
            cursor = ref.end
        out.append(formula[cursor:])
        return "".join(out), used

    def depends(self, ds_name: str, name: str) -> list[tuple[str, str]]:
        """The fields a field reads directly: those its formula references, or a group's or bin's source field."""

        entry = self.fields.get(ds_name, {}).get(name)
        if entry is None:
            return []
        if entry.get("formula_raw") is not None and entry.get("kind") in ("calculated", "bin"):
            used = []
            for ref in R.formula_refs(entry["formula_raw"]):
                target = self.resolve(ds_name, ref.parts)
                if target is not None and target not in used:
                    used.append(target)
            return used
        source = entry.get("_source")
        if source and self.known(ds_name, source):
            return [(ds_name, source)]
        return []

    def closure(self, start: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        todo = list(start)
        while todo:
            item = todo.pop()
            for dep in self.depends(*item):
                if dep not in seen:
                    seen.add(dep)
                    todo.append(dep)
        return seen


def _text(element: ET.Element, name: str) -> str | None:
    child = _child(element, name)
    return child.text if child is not None else None


# ----------------------------------------------------------------------------------------------------------- filters


def _member(value: str | None) -> str:
    return value if value is not None else "?"


def _filter_view(model: _Model, filt: ET.Element, ds_name: str, field_name: str, label: str) -> dict:
    """One filter as ``{"datasource", "field", "caption", "class", "summary"}``; the summary is a short readable rule."""

    cls = filt.get("class", "")
    summary = None
    if cls == "categorical":
        group = _child(filt, "groupfilter")
        summary = _groupfilter_summary(group, label) if group is not None else None
    elif cls == "quantitative":
        low, high = _text(filt, "min"), _text(filt, "max")
        included = filt.get("included-values")
        if low is not None and high is not None:
            summary = f"{label} between {low} and {high}"
        elif low is not None:
            summary = f"{label} >= {low}"
        elif high is not None:
            summary = f"{label} <= {high}"
        elif included:
            summary = f"{label}: {included}"
    elif cls == "relative-date":
        period = filt.get("period-type", "period")
        first, last = filt.get("first-period"), filt.get("last-period")
        if first is not None and last is not None:
            summary = f"{label}: {period}s {first} to {last} relative to today"
    if summary is None:
        summary = f"{label}: {cls or 'filter'} filter"
    return {"datasource": model.ds_caption.get(ds_name, ds_name), "field": R.bracket(field_name), "caption": label,
            "class": cls, "summary": summary}


def _groupfilter_summary(group: ET.Element, label: str) -> str | None:
    function = group.get("function")
    exclusive = group.get(_USER_NS + "ui-enumeration") == "exclusive"
    if function == "member":
        return f"{label} {'!=' if exclusive else '='} {_member(group.get('member'))}"
    if function == "union":
        members = [g.get("member") for g in _children(group, "groupfilter") if g.get("function") == "member"]
        if len(members) == len(_children(group, "groupfilter")):
            return f"{label} {'not in' if exclusive else 'in'} ({', '.join(_member(m) for m in members)})"
    if function == "except":
        inner = _children(group, "groupfilter")
        if len(inner) == 2 and inner[0].get("function") == "level-members":
            rest = _groupfilter_members(inner[1])
            if rest is not None:
                return f"{label} not in ({', '.join(rest)})"
    if function == "level-members":
        return f"{label}: all values"
    if function == "range":
        return f"{label} between {group.get('from', '?')} and {group.get('to', '?')}"
    return None


def _groupfilter_members(group: ET.Element) -> list[str] | None:
    if group.get("function") == "member":
        return [_member(group.get("member"))]
    if group.get("function") == "union":
        inner = _children(group, "groupfilter")
        if all(g.get("function") == "member" for g in inner):
            return [_member(g.get("member")) for g in inner]
    return None


# ------------------------------------------------------------------------------------------------------------ the view


def _instances(deps_list: list[ET.Element]) -> dict[tuple[str, str], str]:
    """``(datasource, instance name)`` -> field name, from the sheet's <column-instance> declarations."""

    out = {}
    for deps in deps_list:
        ds_name = deps.get("datasource", "")
        for instance in _children(deps, "column-instance"):
            name, column = _unbracket(instance.get("name")), _unbracket(instance.get("column"))
            if name and column:
                out[(ds_name, name)] = column
    return out


def _field_of_instance(model: _Model, instances: dict, ds_name: str, instance: str) -> str | None:
    if (ds_name, instance) in instances:
        return instances[(ds_name, instance)]
    if model.known(ds_name, instance):
        return instance
    name = R.instance_field(instance)
    return name if model.known(ds_name, name) else None


_DERIVATIONS = {
    "sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "cnt": "CNT", "ctd": "CNTD", "med": "MEDIAN", "attr": "ATTR",
    "yr": "YEAR", "qr": "QUARTER", "mn": "MONTH", "wk": "WEEK", "dy": "DAY", "tyr": "YEAR", "tqr": "QUARTER", "tmn": "MONTH",
    "twk": "WEEK", "tdy": "DAY", "agg": "AGG", "stdev": "STDEV", "var": "VAR",
}


def _instance_label(model: _Model, ds_name: str, instance: str, name: str | None) -> str:
    """How a sheet shows an instance: ``SUM(Sales)`` for ``sum:sales:qk``, ``Region`` for ``none:region:nk``."""

    if name is None:
        return instance
    caption = model.caption(ds_name, name)
    derivation = instance.split(":", 1)[0] if ":" in instance and instance != name else None
    return f"{_DERIVATIONS[derivation]}({caption})" if derivation in _DERIVATIONS else caption


def _resolve_text(model: _Model, text: str, instances: dict) -> str:
    """Shelf text with ``[ds].[instance]`` replaced by its label (``SUM(Sales)``, ``Region``)."""

    out = []
    cursor = 0
    for ref in R.qualified_refs(text):
        ds_name, instance = ref.parts
        if ds_name not in model.ds_by_name:
            continue
        out.append(text[cursor:ref.start])
        out.append(_instance_label(model, ds_name, instance, _field_of_instance(model, instances, ds_name, instance)))
        cursor = ref.end
    out.append(text[cursor:])
    return " ".join("".join(out).split())


def _sheet_view(model: _Model, sheet: ET.Element, problems: list[str]) -> dict:
    name = sheet.get("name", "")
    table = _child(sheet, "table")
    view = _child(table, "view") if table is not None else None
    deps_list = list(_descendants(sheet, "datasource-dependencies"))
    instances = _instances(deps_list)
    used: list[tuple[str, str]] = []

    def use(ds_name: str, field_name: str | None) -> None:
        if field_name and (ds_name, field_name) not in used and not field_name.startswith(":"):
            used.append((ds_name, field_name))

    for (ds_name, _), field_name in instances.items():
        use(ds_name, field_name)
    skip = {id(node) for deps in deps_list for node in deps.iter()}
    for node in sheet.iter():
        if id(node) in skip:
            continue
        texts = [value for value in node.attrib.values()]
        if node.text and node.text.strip():
            texts.append(node.text)
        for text in texts:
            for ref in R.qualified_refs(text):
                ds_name, instance = ref.parts
                if ds_name in model.ds_by_name:
                    use(ds_name, _field_of_instance(model, instances, ds_name, instance))
    closure = model.closure(used)
    for deps in deps_list:
        ds_name = deps.get("datasource", "")
        for column in _children(deps, "column"):
            field_name = _unbracket(column.get("name"))
            if model.known(ds_name, field_name) and (ds_name, field_name) not in closure:
                use(ds_name, field_name)  # a declared dependency the shelves do not explain: still a use
    closure = model.closure(used) - set(used)

    def field_list(items: Iterable[tuple[str, str]]) -> list[dict]:
        return sorted(
            ({"datasource": model.ds_caption.get(ds, ds), "name": R.bracket(f), "caption": model.caption(ds, f)} for ds, f in items),
            key=lambda d: (d["datasource"].lower(), d["caption"].lower(), d["name"]),
        )

    datasources = []
    if view is not None:
        holder = _child(view, "datasources")
        for ds in _children(holder, "datasource") if holder is not None else []:
            ds_name = ds.get("name", "")
            datasources.append(model.ds_caption.get(ds_name, ds.get("caption") or ds_name))
    filters = []
    if view is not None:
        for filt in _children(view, "filter"):
            parts = R.split_ref(filt.get("column") or "")
            if not parts or len(parts) != 2:
                continue
            ds_name, instance = parts
            found = _field_of_instance(model, instances, ds_name, instance)
            label = _instance_label(model, ds_name, instance, found)
            filters.append(_filter_view(model, filt, ds_name, found or instance, label))
    shelves = {}
    if table is not None:
        for shelf in ("rows", "cols"):
            element = _child(table, shelf)
            if element is not None and element.text and element.text.strip():
                shelves[shelf] = _resolve_text(model, element.text, instances)
    marks, colors = [], []
    panes = _child(table, "panes") if table is not None else None
    for pane in _children(panes, "pane") if panes is not None else []:
        mark = _child(pane, "mark")
        if mark is not None and mark.get("class"):
            marks.append(mark.get("class"))
        for fmt in _formats(pane, "mark-color"):
            colors.append(fmt.get("value"))
    if name in model.o.mark_colors:
        colors = [model.o.mark_colors[name] for _ in colors]
    formats = []
    for fmt in _formats(table, "text-format"):
        parts = R.split_ref(fmt.get("field") or "")
        if not parts or len(parts) != 2:
            continue
        ds_name, instance = parts
        field_name = _field_of_instance(model, instances, ds_name, instance) or instance
        value = model.o.sheet_formats.get((name, ds_name, field_name), fmt.get("value"))
        formats.append({"datasource": model.ds_caption.get(ds_name, ds_name), "field": R.bracket(field_name),
                        "caption": model.caption(ds_name, field_name), "format": value})
    out = {"name": name, "datasources": datasources, "fields": field_list(used), "through_calculations": field_list(closure),
           "filters": filters}
    if shelves:
        out["shelves"] = shelves
    if marks:
        out["marks"] = marks
    if colors:
        out["mark_colors"] = colors
    if formats:
        out["formats"] = formats
    member_colors = _member_colors(model, sheet, instances, scope_ds=None)
    if member_colors:
        out["member_colors"] = member_colors
    return out


def _formats(element: ET.Element | None, attr: str) -> list[ET.Element]:
    if element is None:
        return []
    out = []
    for rule in _descendants(element, "style-rule"):
        for fmt in _children(rule, "format"):
            if fmt.get("attr") == attr:
                out.append(fmt)
    return out


def _member_colors(model: _Model, holder: ET.Element, instances: dict, scope_ds: str | None) -> list[dict]:
    """Palette colors assigned to members of a dimension: ``<encoding attr='color'><map to='#hex'><bucket>``."""

    out = []
    for encoding in _descendants(holder, "encoding"):
        if encoding.get("attr") != "color":
            continue
        parts = R.split_ref(encoding.get("field") or "")
        if not parts:
            continue
        if len(parts) == 2:
            ds_name, instance = parts
        elif scope_ds is not None:
            ds_name, instance = scope_ds, parts[0]
        else:
            continue
        field_name = _field_of_instance(model, instances, ds_name, instance) or instance
        for mapping in _children(encoding, "map"):
            for bucket in _children(mapping, "bucket"):
                member = _member_name(bucket.text)
                color = model.o.member_colors.get((ds_name, field_name, member), mapping.get("to"))
                out.append({"datasource": model.ds_caption.get(ds_name, ds_name), "field": R.bracket(field_name),
                            "caption": model.caption(ds_name, field_name), "member": member, "color": color})
    return out


def _member_name(bucket: str | None) -> str:
    """``"East"`` (quoted, as Tableau writes a string member) -> ``East``; a number stays as written."""

    text = (bucket or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('""', '"')
    return text


def _relations(ds: ET.Element) -> list[ET.Element]:
    connection = _child(ds, "connection")
    if connection is None:
        return []
    return [node for node in connection.iter() if local(node.tag) == "relation" and node.get("type") in ("table", "text")]


def connections_of(ds: ET.Element) -> list[tuple[str, ET.Element]]:
    """``(name, element)`` of each warehouse connection of a data source: named connections of a federated one, else
    the data source's own connection."""

    connection = _child(ds, "connection")
    if connection is None:
        return []
    named = list(_descendants(connection, "named-connection"))
    out = []
    for item in named:
        inner = _child(item, "connection")
        if inner is not None:
            out.append((item.get("name", ""), inner))
    if not out and connection.get("class") and connection.get("class") != "federated":
        out.append(("", connection))
    return out


def _datasource_view(model: _Model, ds: ET.Element) -> dict:
    ds_name = ds.get("name", "")
    connections = {}
    for conn_name, element in connections_of(ds):
        info = _connection_view(element)
        info.update(model.o.connections.get((ds_name, conn_name), {}))
        connections[conn_name] = info
    default_connection = next(iter(connections.values()), {})
    relations: list[dict] = []
    seen: set[tuple] = set()
    for relation in _relations(ds):
        rel_name = relation.get("name", "")
        connection = connections.get(relation.get("connection", ""), default_connection)
        if relation.get("type") == "table":
            if (ds_name, rel_name) in model.o.tables:
                table, complete = model.o.tables[(ds_name, rel_name)], True
            else:
                table, complete = relation_table(relation.get("table"), connection)
            item = {"name": rel_name, "type": "table", "table": table}
            if not complete:
                item["completed_from"] = "connection"
        else:
            sql = model.o.sql.get((ds_name, rel_name), relation.text or "")
            tables, reason = custom_sql_tables(sql, connection)
            item = {"name": rel_name, "type": "custom_sql", "sql": sql, "tables": tables}
            if reason:
                item["unparsed"] = reason
        if relation.get("connection"):
            item["connection"] = relation.get("connection")
        key = tuple(sorted((k, str(v)) for k, v in item.items()))
        if key not in seen:  # Tableau writes the same relation twice (legacy and object-model forms)
            seen.add(key)
            relations.append(item)
    fields = []
    for name in sorted(model.fields[ds_name], key=lambda n: (model.caption(ds_name, n).lower(), n)):
        entry = model.fields[ds_name][name]
        item = {"name": R.bracket(name), "caption": model.caption(ds_name, name), "kind": entry["kind"]}
        for key in ("datatype", "role", "remote", "default_format", "hidden", "value", "domain"):
            if key in entry:
                item[key] = entry[key]
        if entry.get("formula_raw") is not None:
            text, used = model.formula_text(ds_name, entry["formula_raw"], item["caption"])
            item["formula"] = text
            item["formula_raw"] = entry["formula_raw"]
        else:
            used = model.depends(ds_name, name)
        if used:
            item["references"] = [{"datasource": model.ds_caption.get(u[0], u[0]), "name": R.bracket(u[1]),
                                   "caption": model.caption(*u)} for u in used]
        if entry.get("_source"):
            item["source"] = model.caption(ds_name, entry["_source"])
        fields.append(item)
    filters = []
    for filt in _children(ds, "filter"):
        parts = R.split_ref(filt.get("column") or "")
        if not parts:
            continue
        instance = parts[-1]
        field_name = _field_of_instance(model, {}, ds_name, instance) or instance
        label = model.caption(ds_name, field_name) if model.known(ds_name, field_name) else instance
        filters.append(_filter_view(model, filt, ds_name, field_name, label))
    out = {"name": ds_name, "caption": model.ds_caption.get(ds_name, ds_name),
           "connections": [dict(info, name=name) if name else info for name, info in connections.items()],
           "relations": relations, "fields": fields, "filters": filters}
    colors = _member_colors(model, ds, {}, scope_ds=ds_name)
    if colors:
        out["member_colors"] = colors
    return out


def _dashboard_view(model: _Model, dashboard: ET.Element, sheet_names: set[str]) -> dict:
    sheets, filter_controls, parameter_controls = [], [], []
    for zone in _descendants(dashboard, "zone"):
        zone_name = zone.get("name")
        kind = zone.get("type-v2") or zone.get("type")
        if kind == "filter":
            parts = R.split_ref(zone.get("param") or "")
            if parts and len(parts) == 2 and parts[0] in model.ds_by_name:
                field_name = _field_of_instance(model, {}, parts[0], parts[1]) or parts[1]
                filter_controls.append({"sheet": zone_name, "caption": model.caption(parts[0], field_name)})
        elif kind == "paramctrl":
            parts = R.split_ref(zone.get("param") or "")
            if parts and len(parts) == 2 and parts[0] == PARAMETERS:
                parameter_controls.append(model.caption(PARAMETERS, parts[1]))
        elif zone_name in sheet_names and zone_name not in sheets:
            sheets.append(zone_name)
    out = {"name": dashboard.get("name", ""), "type": "story" if dashboard.get("type") == "storyboard" else "dashboard",
           "sheets": sheets}
    if filter_controls:
        out["filter_controls"] = filter_controls
    if parameter_controls:
        out["parameter_controls"] = parameter_controls
    return out


def build_view(root: ET.Element, overrides: Overrides | None = None, file_name: str | None = None) -> dict:
    """The structured view of a Tableau workbook's XML tree (see the module docstring)."""

    model = _Model(root, overrides or Overrides())
    datasources = [_datasource_view(model, ds) for ds in model.datasources if ds.get("name") != PARAMETERS]
    parameters = []
    if PARAMETERS in model.ds_by_name:
        for name in sorted(model.fields[PARAMETERS], key=lambda n: model.caption(PARAMETERS, n).lower()):
            entry = model.fields[PARAMETERS][name]
            item = {"name": R.bracket(name), "caption": model.caption(PARAMETERS, name)}
            for key in ("datatype", "value", "domain"):
                if key in entry:
                    item[key] = entry[key]
            parameters.append(item)
    problems: list[str] = []
    worksheets = [_sheet_view(model, sheet, problems) for sheet in model.worksheets()]
    names = {sheet["name"] for sheet in worksheets}
    dashboards = [_dashboard_view(model, dashboard, names) for dashboard in model.dashboards()]
    for ds in datasources:  # two fields one name: a formula reading that name is ambiguous
        seen: dict[str, str] = {}
        for item in ds["fields"]:
            key = item["caption"].lower()
            if key in seen and seen[key] != item["name"]:
                model.problems.append(f"data source {ds['caption']!r} has two fields named {item['caption']!r}: {seen[key]} and {item['name']}")
            seen.setdefault(key, item["name"])
    view = {"format": "tableau"}
    if file_name:
        view["file"] = file_name
    if root.get("version"):
        view["version"] = root.get("version")
    view.update({"datasources": datasources, "parameters": parameters, "worksheets": worksheets, "dashboards": dashboards,
                 "problems": sorted(set(model.problems + problems))})
    return view
