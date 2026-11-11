"""The Malloy reader: sources, the tables they read, keys, columns, joins, and what it cannot read."""

from kumosql.malloy_reader import read_malloy

MODEL = """
// a semantic model
import "base.malloy"
source: sales is duckdb.table('conn:ds.orders') extend {
  primary_key: order_id
  join_one: customers is duckdb.table('ds.customers') on customer_id = customers.id
  join_one: regions with region_code
  # hidden
  dimension:
    week is order_week
    amount is amount * 2
    size is pick 'big' when total > 5 else 'small'
  measure: revenue is amount.sum()
  measure: n is count()
  where: status = 'paid' and created::date > @2020-01-01
  view: by_week is { group_by: week; aggregate: revenue, n; order_by: refunded_at desc }
}
source: big_sales is sales extend { where: amount > 100 and region_name != 'x' }
source: from_sql is duckdb.sql(\"\"\"select 1 as y\"\"\") extend { dimension: x is y }
source: orphan is imported_thing extend { dimension: q is z }
run: sales -> { group_by: week }
"""


def by_name(sources):
    return {source["name"]: source for source in sources}


def test_a_source_lists_its_table_primary_key_and_the_columns_its_definitions_read():
    sources = by_name(read_malloy(MODEL))
    sales = sources["sales"]
    assert sales["table"] == "ds.orders" and sales["primary_key"] == "order_id" and sales["extends"] is None
    # columns: keys, expressions, filters, joins and views; not the names the source defines, not functions or keywords
    assert sales["columns"] == ["amount", "created", "customer_id", "order_id", "order_week", "refunded_at", "region_code", "status", "total"]
    assert "week" not in sales["columns"] and "revenue" not in sales["columns"] and "sum" not in sales["columns"]


def test_a_name_a_dimension_defines_is_a_column_only_when_its_own_definition_reads_one_of_that_name():
    assert "amount" in by_name(read_malloy(MODEL))["sales"]["columns"]  # amount is amount * 2
    plain = by_name(read_malloy("source: s is table('d.t') extend { dimension: week is order_week\n measure: n is count()\n view: v is { group_by: week } }"))["s"]
    assert plain["columns"] == ["order_week"]


def test_joins_name_the_joined_table_or_source_and_the_columns_read_from_it():
    sales = by_name(read_malloy(MODEL))["sales"]
    customers, regions = sales["joins"]
    assert customers == {"name": "customers", "kind": "one", "table": "ds.customers", "source": None, "columns": ["id"]}
    assert regions["source"] == "regions" and regions["table"] is None
    joined = by_name(read_malloy("source: regions is table('d.region') extend { primary_key: code }\nsource: s is table('d.t') extend { join_one: regions with region_code }"))["s"]
    assert joined["joins"][0]["table"] == "d.region" and "region_code" in joined["columns"]


def test_a_source_that_extends_another_inherits_its_table_key_columns_and_joins():
    big = by_name(read_malloy(MODEL))["big_sales"]
    assert big["table"] == "ds.orders" and big["extends"] == "sales" and big["primary_key"] == "order_id"
    assert {"amount", "region_name", "order_week"} <= set(big["columns"]) and len(big["joins"]) == 2


def test_a_source_can_extend_one_from_another_file():
    first = read_malloy("source: base is table('d.t') extend { primary_key: id }")
    second = read_malloy("source: child is base extend { dimension: x is y }", known=by_name(first))
    assert second[0]["table"] == "d.t" and second[0]["primary_key"] == "id" and second.unknown == []


def test_what_the_reader_cannot_read_is_reported_never_skipped():
    sources = read_malloy(MODEL)
    assert sources.imports == ["base.malloy"]
    reasons = {u["what"]: u["reason"] for u in sources.unknown}
    assert "sql(...)" in reasons["from_sql"] and "not defined in the file" in reasons["orphan"]
    assert by_name(sources)["from_sql"]["table"] is None and by_name(sources)["orphan"]["table"] is None
    assert any("regions" in u["reason"] for u in sources.unknown if u["what"] == "sales")


def test_broken_text_is_reported_and_never_raises():
    for text, word in (("source: a is table('x.y') extend { dimension: q is 'oops", "never closes"), ("source: a is table('x.y') extend { dimension: q is (1 ", "balance"),
                       ("source: nonsense", "not `name is")):
        found = read_malloy(text)
        assert found == [] and any(word in u["reason"] for u in found.unknown), text
    unclosed = read_malloy("source: a is table('x.y')\n/* open")
    assert [s["table"] for s in unclosed] == ["x.y"] and "never closes" in unclosed.unknown[0]["reason"]
    assert read_malloy("").unknown == [] and read_malloy("run: x -> { select: * }").unknown == []
    assert read_malloy("run: duckdb.table('d.t') -> { group_by: a }").unknown[0]["reason"].startswith("a run:")


def test_comments_and_strings_do_not_make_columns_or_sources():
    text = "// source: ghost is table('d.ghost')\nsource: s is table('d.t') extend { where: note = 'amount // not a column' -- total\n}"
    sources = read_malloy(text)
    assert [s["name"] for s in sources] == ["s"] and sources[0]["columns"] == ["note"]
