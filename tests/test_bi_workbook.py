"""Tableau workbooks as text, one-call questions, the lineage hook, and patches checked by parsing the result again.

The fixture (tests/fixtures/bi_workbooks/sales_overview.twb) is synthetic and written by hand: three BigQuery data
sources (one joining a table to a custom SQL query, one in both the legacy and object-model relation forms, one
custom SQL query with a Tableau parameter), calculated fields that reference each other, a parameter, filters, formats,
colors and two dashboards. Every test copies it into ``tmp_path`` before writing anything.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import zipfile
from pathlib import Path

import pytest

from kumosql import bi_workbook as B
from kumosql.bi_workbook import cli
from kumosql.bi_workbook import patch as P
from kumosql.bi_workbook import xml_edit

FIXTURE = Path(__file__).parent / "fixtures" / "bi_workbooks" / "sales_overview.twb"
ORDERS = "federated.0hgpf0j1fdpvv316shikk0mmdlec"
PROFIT_RATIO = "[Calculation_1368249927221915648]"


def _book(tmp_path: Path, name: str = "sales_overview.twb", data: bytes | None = None) -> tuple[Path, B.Workbook]:
    path = tmp_path / name
    path.write_bytes(FIXTURE.read_bytes() if data is None else data)
    return path, B.open_workbook(path)


def _changed_lines(before: bytes, after: bytes) -> list[tuple[bytes, bytes]]:
    """The lines that differ; the two files must have the same number of lines (no line was added or removed)."""

    old, new = before.split(b"\n"), after.split(b"\n")
    assert len(old) == len(new), "a patch must not add or remove lines"
    return [(a, b) for a, b in zip(old, new) if a != b]


def _assert_only(before: bytes, after: bytes, old: bytes, new: bytes, lines: int) -> None:
    """Exactly ``lines`` lines changed, each by replacing ``old`` with ``new`` and nothing else."""

    changed = _changed_lines(before, after)
    assert len(changed) == lines, [a.decode() for a, _ in changed]
    for a, b in changed:
        assert a.replace(old, new) == b, (a, b)


# --------------------------------------------------------------------------------------------------- the text view


def test_text_view_shows_sources_formulas_filters_sql_sheets_and_dashboards(tmp_path):
    _, book = _book(tmp_path)
    view = book.view()
    text = B.render_text(view)
    assert text == B.render_text(book.view())  # deterministic
    lines = text.splitlines()
    assert lines[0] == ("Tableau workbook sales_overview.twb (version 18.1): 3 data sources, 2 parameters, 5 worksheets, "
                        "2 dashboards, 0 problems")
    for line in [
        f"DATA SOURCE Orders (sales)  [{ORDERS}]",
        "  connection: bigquery project=acme-analytics dataset=sales billing_project=acme-bi",
        "  table orders: acme-analytics.sales.orders",
        "  custom SQL 'Customer Segments' reads: acme-analytics.crm.customers, acme-analytics.crm.segments",
        "    | FROM `acme-analytics.crm.customers` AS c",
        # internal names resolved to captions, a parameter by its caption, a blended field by its data source
        f"    Profit Ratio = SUM([Profit]) / SUM([Sales])   {PROFIT_RATIO}",
        "    High Margin = IF [Profit Ratio] >= [Parameters].[Margin Target] THEN \"High\" ELSE \"Normal\" END   [Calculation_5205031853421133824]",
        "    Target Gap = SUM([Sales]) - SUM([Targets].[Target])   [Calculation_7714630129385267200]",
        # brackets inside a string literal are text, not a reference
        "    Margin Label = \"[Profit Ratio] is \" + STR(ROUND([Profit Ratio] * 100, 1)) + \"%\"   [Calculation_2817004532180725760]",
        "  data source filter: Region in (\"Central\", \"East\", \"West\")",
        "  Margin Target (real) = 0.25   [Parameter 1]",
        "WORKSHEET Margin Trend",
        "  uses: Orders (sales): Order Year, Profit Ratio, Sales; Parameters: Margin Target",
        "  through calculations: Orders (sales): Order Date, Profit",
        "  filter: SUM(Sales) >= 1000",
        "  rows: Profit Ratio   cols: Order Year",
        "  marks: Line, color #e15759",
        "  filter: Region in (\"East\", \"West\")",
        "DASHBOARD Overview: Sales by Region, Margin Trend",
        "  filter control: Region (on Sales by Region)",
        "  parameter control: Margin Target",
        "DASHBOARD Margin Review: Margin Table, Targets vs Actual",
    ]:
        assert line in lines, line
    assert view["problems"] == []
    json.dumps(view)  # the JSON form is plain data


def test_text_view_is_capped_and_says_what_was_cut(tmp_path):
    _, book = _book(tmp_path)
    text = B.render_text(book.view(), max_chars=600)
    assert len(text) <= 600 + 200  # the cut note itself
    assert "... cut:" in text and "more lines" in text and "--sheet or --datasource" in text
    one = B.render_text(book.view(), sheet="margin table")
    assert "WORKSHEET Margin Table" in one and "WORKSHEET Margin Trend" not in one and "DATA SOURCE" not in one


def test_shelves_filters_and_fields_resolve_instances(tmp_path):
    _, book = _book(tmp_path)
    sheets = {s["name"]: s for s in book.view()["worksheets"]}
    assert sheets["Sales by Region"]["shelves"] == {"rows": "Region", "cols": "SUM(Sales)"}
    assert [f["caption"] for f in sheets["Margin Table"]["fields"]] == ["High Margin", "Region"]
    assert [f["caption"] for f in sheets["Margin Table"]["through_calculations"]] == ["Profit", "Profit Ratio", "Sales", "Margin Target"]
    blended = {(f["datasource"], f["caption"]) for f in sheets["Targets vs Actual"]["fields"]}
    assert ("Targets", "Target") in blended and ("Orders (sales)", "Target Gap") in blended


# --------------------------------------------------------------------------------------------------- find


def test_find_answers_which_sheets_use_a_calculated_field_and_its_formula_in_one_call(tmp_path, capsys):
    path, book = _book(tmp_path)
    result = B.find_field(book.view(), "Profit Ratio")
    [match] = result["matches"]
    assert match["formula"] == "SUM([Profit]) / SUM([Sales])"
    assert match["formula_raw"] == "SUM([profit]) / SUM([sales])"
    assert match["name"] == PROFIT_RATIO and match["datasource"] == "Orders (sales)"
    assert match["reads"] == ["Profit", "Sales"]
    assert match["read_by"] == ["High Margin", "Margin Label"]
    assert match["sheets"] == ["Margin Trend"]
    assert match["sheets_through_calculations"] == [{"sheet": "Margin Table", "via": ["High Margin"]}]
    assert match["dashboards"] == [{"name": "Overview", "sheets": ["Margin Trend"]},
                                   {"name": "Margin Review", "sheets": ["Margin Table"]}]
    # the internal name works too, with or without brackets and in any case
    assert B.find_field(book.view(), PROFIT_RATIO.lower())["matches"][0]["caption"] == "Profit Ratio"
    # one command-line call answers it
    assert cli.main(["find", str(path), "profit ratio"]) == 0
    out = capsys.readouterr().out
    assert "formula: SUM([Profit]) / SUM([Sales])" in out
    assert "used directly by 1 sheet: Margin Trend" in out
    assert "used through calculated fields by 1 sheet: Margin Table (via High Margin)" in out


def test_find_lists_every_field_of_that_name_and_suggests_near_names(tmp_path, capsys):
    path, book = _book(tmp_path)
    regions = B.find_field(book.view(), "Region")["matches"]
    assert [m["datasource"] for m in regions] == ["Orders (sales)", "Targets", "Returns"]
    orders = regions[0]
    assert orders["sheets"] == ["Sales by Region", "Margin Table", "Targets vs Actual"]
    assert {"sheet": "Sales by Region", "summary": 'Region in ("East", "West")'} in orders["filters"]
    assert {"datasource": "Orders (sales)", "summary": 'Region in ("Central", "East", "West")'} in orders["filters"]
    assert B.find_field(book.view(), "Region", datasource="targets")["matches"][0]["sheets"] == ["Targets vs Actual"]
    missing = B.find_field(book.view(), "profit r")
    assert missing["matches"] == [] and missing["suggestions"] == ["Profit Ratio"]
    assert cli.main(["find", str(path), "nope"]) == 1
    parameter = B.find_field(book.view(), "margin target")["matches"][0]
    assert parameter["kind"] == "parameter" and parameter["read_by"] == ["High Margin"]


# --------------------------------------------------------------------------------------------------- tables (lineage)


def test_tables_read_lists_connection_tables_and_custom_sql_tables(tmp_path):
    _, book = _book(tmp_path)
    tables = {entry["datasource"]: entry for entry in B.tables_read(book.view())}
    assert [(t["table"], t["via"], t["relation"]) for t in tables["Orders (sales)"]["tables"]] == [
        ("acme-analytics.sales.orders", "table", "orders"),
        ("acme-analytics.crm.customers", "custom_sql", "Customer Segments"),
        ("acme-analytics.crm.segments", "custom_sql", "Customer Segments"),
    ]
    # the object-model and legacy copies of one relation are one table
    assert [t["table"] for t in tables["Targets"]["tables"]] == ["acme-analytics.planning.sales_targets"]
    # the CTE is not a table; the Tableau parameter does not stop the parse; `sales.orders` runs in the billing project
    assert tables["Returns"]["tables"] == [
        {"table": "acme-analytics.sales.returns", "via": "custom_sql", "relation": "Recent Returns"},
        {"table": "acme-bi.sales.orders", "written": "sales.orders", "completed_from": "billing project",
         "via": "custom_sql", "relation": "Recent Returns"},
    ]
    assert all(entry["unparsed"] == [] and entry["connection_class"] == "bigquery" for entry in tables.values())


def test_custom_sql_that_does_not_parse_is_reported_never_guessed(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    start = text.index("SELECT c.customer_id,")
    end = text.index("</relation>", start)
    broken = text[:start] + "SELECT * FROM {{ ref('customers') }} WHERE region = 'West'" + text[end:]
    _, book = _book(tmp_path, data=broken.encode("utf-8"))
    orders = next(e for e in B.tables_read(book.view()) if e["datasource"] == "Orders (sales)")
    assert [t["table"] for t in orders["tables"]] == ["acme-analytics.sales.orders"]
    [unparsed] = orders["unparsed"]
    assert unparsed["relation"] == "Customer Segments" and "could not parse" in unparsed["reason"]
    assert B.find_table(book.view(), "customers")["unknown"][0]["relation"] == "Customer Segments"
    with pytest.raises(P.PatchRefused, match="cannot be parsed"):
        P.run(book, P.swap_datasource("Orders (sales)", project="acme-staging"))


def test_find_table_names_the_data_sources_sheets_and_dashboards_that_read_it(tmp_path):
    _, book = _book(tmp_path)
    result = B.find_table(book.view(), "acme-analytics.crm.segments")
    assert [(r["datasource"], r["relation"]) for r in result["reads"]] == [("Orders (sales)", "Customer Segments")]
    assert "Margin Trend" in result["sheets"] and "Returns by Reason" not in result["sheets"]
    assert B.find_table(book.view(), "returns")["sheets"] == ["Returns by Reason"]


# --------------------------------------------------------------------------------------------------- patches


def test_rename_field_changes_only_its_captions_and_every_reference_follows(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.rename_field("Profit Ratio", "Margin %"))
    assert path.read_bytes() == before  # a preview writes nothing
    _assert_only(before, result.patched, b"caption='Profit Ratio'", b"caption='Margin %'", lines=3)
    after = B.read_workbook(result.patched, "x.twb").view()
    fields = {f["name"]: f for f in after["datasources"][0]["fields"]}
    # formulas that read it now show the new name; what they store (the internal name) is unchanged
    assert fields["[Calculation_5205031853421133824]"]["formula"].startswith("IF [Margin %] >= ")
    assert fields["[Calculation_5205031853421133824]"]["formula_raw"].startswith("IF [Calculation_1368249927221915648] >= ")
    assert after["worksheets"][1]["shelves"]["rows"] == "Margin %"
    assert B.find_field(after, "Margin %")["matches"][0]["sheets"] == ["Margin Trend"]
    assert after["problems"] == []
    assert any(c.endswith("/caption: 'Profit Ratio' -> 'Margin %'") for c in result.changes)
    assert all("Profit Ratio" in c or "Margin %" in c for c in result.changes)


def test_rename_to_a_name_in_use_is_refused_and_nothing_is_written(tmp_path, capsys):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    with pytest.raises(P.PatchRefused, match="already has a field named 'Sales'"):
        P.run(book, P.rename_field("Profit Ratio", "sales"))
    assert cli.main(["patch", str(path), "rename-field", "Profit Ratio", "Profit", "--apply"]) == 3
    assert "REFUSED" in capsys.readouterr().err
    assert path.read_bytes() == before
    with pytest.raises(P.PatchRefused, match="say which with --datasource"):
        P.run(book, P.rename_field("Region", "Area"))


def test_the_check_refuses_an_edit_that_misses_a_copy_or_breaks_a_formula(tmp_path):
    """The re-parse and diff catch a wrong edit even when the planner is wrong: these planners are deliberately buggy."""

    _, book = _book(tmp_path)

    def caption_in_the_data_source_only(doc, view):
        own, _copies = P._declarations(doc, ORDERS, "Calculation_1368249927221915648")
        plan = P._Plan("rename without the sheet copies")
        P._set_attr(doc, plan, own, "caption", "Margin %")
        plan.overrides.captions[(ORDERS, "Calculation_1368249927221915648")] = "Margin %"
        return plan

    with pytest.raises(P.PatchRefused, match="differs from the intended change"):
        P.run(book, ("buggy", caption_in_the_data_source_only))

    def formula_to_a_field_that_does_not_exist(doc, view):
        ds = P._ds_element(doc, ORDERS)
        column = next(c for c in ds if c.get("name") == "[Calculation_5205031853421133824]")
        calculation = next(iter(column))
        plan = P._Plan("point High Margin at a missing field")
        old = calculation.get("formula")
        P._set_attr(doc, plan, calculation, "formula", old.replace("[Calculation_1368249927221915648]", "[Calculation_0]"))
        return plan

    with pytest.raises(P.PatchRefused, match="differs from the intended change"):
        P.run(book, ("buggy", formula_to_a_field_that_does_not_exist))

    def touches_more_than_it_plans(doc, view):
        own, _ = P._declarations(doc, ORDERS, "sales")
        plan = P._Plan("change an attribute without saying so")
        plan.edits += doc.set_value(own, "role", "dimension")
        return plan

    with pytest.raises(P.PatchRefused, match="did not plan to"):
        P.run(book, ("buggy", touches_more_than_it_plans))


def test_apply_and_output_write_only_when_asked(tmp_path, capsys):
    path, _ = _book(tmp_path)
    before = path.read_bytes()
    assert cli.main(["patch", str(path), "rename-field", "Profit Ratio", "Margin %"]) == 0
    out = capsys.readouterr().out
    assert "not written: this was a preview" in out and "view diff: the patched view equals" in out
    assert "+    Margin % = SUM([Profit]) / SUM([Sales])" in out
    assert path.read_bytes() == before
    copy = tmp_path / "copy.twb"
    assert cli.main(["patch", str(path), "rename-field", "Profit Ratio", "Margin %", "--output", str(copy)]) == 0
    assert f"written: {copy}" in capsys.readouterr().out
    assert path.read_bytes() == before and b"caption='Margin %'" in copy.read_bytes()
    assert cli.main(["patch", str(path), "rename-field", "Profit Ratio", "Margin %", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["written"] == str(path) and report["sha256_before"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == copy.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy.twb", "sales_overview.twb"]  # no temporary file left


def test_apply_refuses_when_the_file_changed_since_it_was_read(tmp_path):
    path, book = _book(tmp_path)
    result = P.run(book, P.set_color("#000000", sheet="Margin Trend"))
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(P.PatchRefused, match="changed on disk"):
        P.write_result(result, path)


def test_swap_project_moves_connection_relation_and_custom_sql_tables(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.swap_datasource("Orders (sales)", project="acme-staging"))
    changed = _changed_lines(before, result.patched)
    assert len(changed) == 4
    for old, new in changed:
        assert old.replace(b"acme-analytics", b"acme-staging") == new
    tables = next(e for e in B.tables_read(B.read_workbook(result.patched, "x.twb").view()) if e["datasource"] == "Orders (sales)")
    assert [t["table"] for t in tables["tables"]] == [
        "acme-staging.sales.orders", "acme-staging.crm.customers", "acme-staging.crm.segments"]
    assert any("re-parsed with sqlglot" in check for check in result.checks)
    # other data sources in the same project did not move
    assert b"acme-analytics.planning" in result.patched


def test_swap_dataset_edits_custom_sql_only_where_a_table_moves(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.swap_datasource("Returns", dataset="sales_v2"))
    changed = _changed_lines(before, result.patched)
    assert [(a.strip(), b.strip()) for a, b in changed] == [
        (a.strip(), a.strip().replace(b"schema='sales'", b"schema='sales_v2'")) for a, _ in changed[:1]
    ] + [(b"FROM `acme-analytics.sales.returns`", b"FROM `acme-analytics.sales_v2.returns`")]
    # `sales.orders` lives in the billing project, which did not move: it is left as written
    assert b"JOIN sales.orders AS o USING (order_id)" in result.patched
    # the parameter inside the SQL is kept byte for byte
    assert b"INTERVAL &lt;[Parameters].[Parameter 2]&gt; DAY" in result.patched


def test_swap_billing_project_keeps_what_short_names_read(tmp_path):
    _, book = _book(tmp_path)
    result = P.run(book, P.swap_datasource("Returns", billing_project="acme-bi-2"))
    assert b"JOIN `acme-bi.sales.orders` AS o USING (order_id)" in result.patched
    assert any("still reads the same table" in w for w in result.warnings)
    after = B.tables_read(B.read_workbook(result.patched, "x.twb").view())
    assert "acme-bi.sales.orders" in [t["table"] for e in after for t in e["tables"]]


def test_swap_table_changes_both_relation_copies(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.swap_datasource("Targets", tables=["sales_targets=sales_targets_2025"]))
    _assert_only(before, result.patched, b"[sales_targets]' type", b"[sales_targets_2025]' type", lines=2)
    with pytest.raises(P.PatchRefused, match="reads no such table"):
        P.run(book, P.swap_datasource("Targets", tables=["nope=other"]))


def test_swap_column_points_a_field_at_a_renamed_warehouse_column(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.swap_datasource("Orders (sales)", columns=["region=sales_region"]))
    changed = _changed_lines(before, result.patched)
    assert [b.strip() for _, b in changed] == [
        b"<map key='[region]' value='[orders].[sales_region]' />",
        b"<remote-name>sales_region</remote-name>",
        b"<remote-alias>sales_region</remote-alias>",
    ]
    assert result.changes == [f"/datasources[{ORDERS}]/fields[[region]]/remote: 'orders.region' -> 'orders.sales_region'"]
    with pytest.raises(P.PatchRefused, match="comes from custom SQL"):
        P.run(book, P.swap_datasource("Orders (sales)", columns=["segment_name=segment"]))


def test_swap_leaves_an_extract_alone_and_matches_tables_by_their_name_before_the_swap(tmp_path):
    extract = ("      <extract count='-1' enabled='true' units='records'>\n"
               "        <connection class='hyper' dbname='Data/Extracts/orders.hyper' schema='Extract' tablename='Extract'>\n"
               "          <relation name='Extract' table='[Extract].[Extract]' type='table' />\n"
               "        </connection>\n"
               "      </extract>\n")
    text = FIXTURE.read_text(encoding="utf-8")
    anchor = "      <aliases enabled='yes' />\n      <column caption='Order Year'"
    data = text.replace(anchor, extract + anchor, 1).encode("utf-8")
    _, book = _book(tmp_path, data=data)
    result = P.run(book, P.swap_datasource("Orders (sales)", project="acme-staging",
                                           tables=["acme-analytics.sales.orders=orders_v2"]))
    assert b"<relation name='Extract' table='[Extract].[Extract]' type='table' />" in result.patched
    assert b"table='[acme-staging.sales].[orders_v2]'" in result.patched
    # a column that a table and a custom SQL query both have is remapped in the table only
    result = P.run(book, P.swap_datasource("Orders (sales)", columns=["customer_id=cust_id"]))
    assert b"<map key='[customer_id]' value='[orders].[cust_id]' />" in result.patched
    assert b"<map key='[customer_id (Customer Segments)]' value='[Customer Segments].[customer_id]' />" in result.patched
    assert len(_changed_lines(data, result.patched)) == 3
    with pytest.raises(P.PatchRefused, match="reads no warehouse column 'nope'"):
        P.run(book, P.swap_datasource("Orders (sales)", columns=["nope=other"]))


def test_set_color_of_a_sheet_and_of_a_member(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.set_color("#ff0000", sheet="Margin Trend"))
    _assert_only(before, result.patched, b"value='#e15759'", b"value='#ff0000'", lines=1)
    result = P.run(book, P.set_color("#112233", field_name="Region", member="East", datasource="Orders (sales)"))
    _assert_only(before, result.patched, b"to='#f28e2b'", b"to='#112233'", lines=1)
    with pytest.raises(P.PatchRefused, match="no fixed mark color"):
        P.run(book, P.set_color("#ff0000", sheet="Sales by Region"))
    with pytest.raises(P.PatchRefused, match="no assigned color.*Central, East, West"):
        P.run(book, P.set_color("#ff0000", field_name="Region", member="North", datasource="Orders (sales)"))
    with pytest.raises(P.PatchRefused, match="not a color"):
        P.run(book, P.set_color("red", sheet="Margin Trend"))


def test_set_format_default_and_per_sheet_keep_entities(tmp_path):
    path, book = _book(tmp_path)
    before = path.read_bytes()
    result = P.run(book, P.set_format("p0.00%", "Profit Ratio"))
    _assert_only(before, result.patched, b"default-format='p0.0%'", b"default-format='p0.00%'", lines=3)
    # a format with quotes is escaped the way the file escapes it, and only the changed characters are rewritten
    result = P.run(book, P.set_format('c"€"#,##0.00', "Sales"))
    _assert_only(before, result.patched, "default-format='c&quot;$&quot;#,##0'".encode(),
                 "default-format='c&quot;€&quot;#,##0.00'".encode(), lines=5)  # the data source and four sheet copies
    result = P.run(book, P.set_format("p0.0%", "Profit Ratio", sheet="Margin Trend"))
    _assert_only(before, result.patched, b"value='p0%'", b"value='p0.0%'", lines=1)
    with pytest.raises(P.PatchRefused, match="no format of its own"):
        P.run(book, P.set_format("p0%", "Profit Ratio", sheet="Margin Table"))


def test_a_field_with_no_caption_gets_one_where_tableau_would_write_it(tmp_path):
    declared = "      <column caption='Return Reason' datatype='string' name='[return_reason]' role='dimension' type='nominal' />\n"
    text = FIXTURE.read_text(encoding="utf-8").replace(
        declared, "      <column datatype='string' name='[order_id]' role='dimension' type='nominal' />\n" + declared, 1)
    data = text.encode("utf-8")
    _, book = _book(tmp_path, data=data)
    result = P.run(book, P.rename_field("order_id", "Order Number", datasource="Returns"))
    # the data source declaration and the sheet's copy had no caption: it is added in alphabetical place, as Tableau writes it
    _assert_only(data, result.patched, b"<column datatype='string' name='[order_id]'",
                 b"<column caption='Order Number' datatype='string' name='[order_id]'", lines=2)
    # a field the data source never declares has nowhere to keep a caption
    with pytest.raises(P.PatchRefused, match="no <column> declaration"):
        P.run(book, P.rename_field("region", "Area", datasource="Returns"))


def test_line_endings_and_untouched_bytes_survive_a_patch(tmp_path):
    crlf = FIXTURE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    path, book = _book(tmp_path, data=crlf)
    result = P.run(book, P.swap_datasource("Returns", dataset="sales_v2"))
    assert result.patched.count(b"\r\n") == crlf.count(b"\r\n")
    assert len(_changed_lines(crlf, result.patched)) == 2
    bom = b"\xef\xbb\xbf" + FIXTURE.read_bytes()
    _, book = _book(tmp_path, "bom.twb", bom)
    assert P.run(book, P.set_color("#000000", sheet="Margin Trend")).patched.startswith(b"\xef\xbb\xbf<?xml")


def test_the_scanner_agrees_with_the_xml_parser_and_edits_in_place():
    text = ("<?xml version='1.0'?>\r\n<!-- c -->\r\n<a xmlns:u='http://u' b='1&amp;2' u:c=\"x&#10;y\r\nz\">\r\n"
            "  <d e='f &apos;q&apos;'>hi &lt;there&gt;</d><e/>\r\n</a>\r\n")
    doc = xml_edit.scan(text)
    d, e = doc.elements[1], doc.elements[2]
    assert doc.node(d).attrs["e"].value == "f 'q'"
    assert xml_edit.apply_edits(text, doc.set_value(d, "e", "f 'Q' & more")) == text.replace(
        "e='f &apos;q&apos;'", "e='f &apos;Q&apos; &amp; more'")
    assert xml_edit.apply_edits(text, doc.set_value(d, "#text", "hi <you>")) == text.replace("&lt;there&gt;", "&lt;you&gt;")
    assert xml_edit.apply_edits(text, doc.insert_attr(e, "k", "v")) == text.replace("<e/>", "<e k='v'/>")
    with pytest.raises(xml_edit.XmlEditError):
        xml_edit.scan("<a><b></a>")


# --------------------------------------------------------------------------------------------------- .twbx


def _package(tmp_path: Path, twb: bytes | None = None) -> Path:
    path = tmp_path / "Sales Overview.twbx"
    extract = bytes(range(256)) * 64
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("Sales Overview.twb", (2024, 5, 6, 7, 8, 10)), twb or FIXTURE.read_bytes(),
                         compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(zipfile.ZipInfo("Data/Extracts/orders.hyper", (2024, 5, 6, 7, 8, 10)), extract,
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr(zipfile.ZipInfo("Image/logo.png", (2024, 5, 6, 7, 8, 10)), b"\x89PNG not really" * 40,
                         compress_type=zipfile.ZIP_DEFLATED)
    return path


def _raw_members(data: bytes) -> dict[str, bytes]:
    """Each member's local header and stored bytes, read independently of the module under test."""

    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            name_len, extra_len = struct.unpack_from("<HH", data, info.header_offset + 26)
            start = info.header_offset + 30 + name_len + extra_len
            out[info.filename] = data[info.header_offset:start + info.compress_size]
    return out


def test_twbx_is_read_like_the_twb_it_holds(tmp_path):
    path = _package(tmp_path)
    book = B.open_workbook(path)
    assert book.kind == "twbx" and book.member == "Sales Overview.twb"
    view = book.view()
    assert view["package"] == {"workbook": "Sales Overview.twb"}
    plain = B.read_workbook(FIXTURE.read_bytes(), "Sales Overview.twb").view()
    assert {k: v for k, v in view.items() if k not in ("file", "package")} == {k: v for k, v in plain.items() if k != "file"}
    assert B.find_field(view, "Profit Ratio")["matches"][0]["sheets"] == ["Margin Trend"]


def test_twbx_patch_rewrites_only_the_workbook_member(tmp_path):
    path = _package(tmp_path)
    before = path.read_bytes()
    assert cli.main(["patch", str(path), "rename-field", "Profit Ratio", "Margin %", "--apply"]) == 0
    after = path.read_bytes()
    old, new = _raw_members(before), _raw_members(after)
    assert list(old) == list(new)
    for name in ("Data/Extracts/orders.hyper", "Image/logo.png"):
        assert old[name] == new[name]  # header and stored bytes, byte for byte
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        twb = archive.read("Sales Overview.twb")
    _assert_only(FIXTURE.read_bytes(), twb, b"caption='Profit Ratio'", b"caption='Margin %'", lines=3)


def test_twbx_with_two_workbooks_is_refused(tmp_path):
    path = tmp_path / "two.twbx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("a.twb", FIXTURE.read_bytes())
        archive.writestr("b.twb", FIXTURE.read_bytes())
    with pytest.raises(B.WorkbookError, match="exactly one .twb"):
        B.open_workbook(path)


def test_other_files_are_refused_with_a_clear_message(tmp_path, capsys):
    (tmp_path / "x.pbix").write_bytes(b"PK")
    assert cli.main(["show", str(tmp_path / "x.pbix")]) == 2
    assert "only Tableau workbooks" in capsys.readouterr().err
    (tmp_path / "bad.twb").write_text("<workbook><datasources></workbook>", encoding="utf-8")
    assert cli.main(["show", str(tmp_path / "bad.twb")]) == 2
    assert "not well-formed" in capsys.readouterr().err


def test_show_json_and_tables_commands(tmp_path, capsys):
    path, _ = _book(tmp_path)
    shutil.copy(path, tmp_path / "again.twb")
    assert cli.main(["show", str(path), "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in view["worksheets"]] == ["Sales by Region", "Margin Trend", "Margin Table",
                                                        "Targets vs Actual", "Returns by Reason"]
    assert cli.main(["tables", str(path)]) == 0
    out = capsys.readouterr().out
    assert "acme-bi.sales.orders  (custom SQL 'Recent Returns', written sales.orders, completed from the billing project)" in out
