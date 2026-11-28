"""check-malloy and the check_malloy tool: structural Malloy problems, and keys, joins and columns judged against the evidence held.

The structural cases were checked by hand against the Malloy compiler (``@malloydata/malloy`` 0.0.435, with stub table schemas):
every model under ``REJECTED`` fails to compile with the error its comment names, and ``ACCEPTED`` compiles. Everything here is
synthetic and offline: no warehouse is asked, and the tests block the network where a server or the CLI runs.
"""

import json
import sys
import types

import pytest

from kumosql import mcp_server

mcp_server.load_tools()  # register the tools in the server's order before the tool module is imported on its own
from kumosql import data_profile_store as store, join_profile_store as joins, malloy_check as mc
from kumosql.data_profile import ColumnProfile, DataProfile
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.schema_snapshot import parse_schema_snapshot
from test_mcp_server import BOUND, ask, block_network, write_project

PROJECT = {
    # a warehouse table declared with a two-column key
    "orders.js": 'declare({schema: "wh", name: "orders", uniqueKey: ["order_id", "line_no"]});\n',
    # a warehouse table nobody states a grain for
    "customers.js": 'declare({schema: "wh", name: "customers"});\n',
    # a model whose query shows one row per (customer_id, order_day), with a ratio of aggregates
    "daily.sqlx": 'config { type: "table" }\nSELECT customer_id, order_day, SUM(amount) AS revenue, SUM(discount) / SUM(amount) AS discount_rate\n'
                  'FROM ${ref("wh", "orders")} GROUP BY customer_id, order_day\n',
    # a model that declares its own key
    "customer_dim.sqlx": 'config { type: "table", assertions: { uniqueKey: ["customer_id"] } }\nSELECT customer_id, name, region FROM ${ref("wh", "customers")}\n',
}

SNAPSHOT = """table_catalog,table_schema,table_name,column_name,data_type
p,wh,orders,order_id,INT64
p,wh,orders,line_no,INT64
p,wh,orders,customer_id,INT64
p,wh,orders,amount,FLOAT64
p,wh,orders,status,STRING
p,wh,orders,margin_pct,FLOAT64
p,wh,orders,created_at,TIMESTAMP
p,wh,customers,id,INT64
p,wh,customers,name,STRING
p,wh,customers,region,STRING
p,wh,items,sku,STRING
p,wh,items,order_id,INT64
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    return write_project(tmp_path_factory.mktemp("malloy_check") / "proj", PROJECT)


@pytest.fixture(scope="module")
def session(project):
    return Session(load_sqlx_project(project), root=project, profiles=project.parent / "no-profiles")


def snapshot():
    return parse_schema_snapshot(SNAPSHOT, "csv")


def observation(table, columns, rows, duplicates, source="join_profile", nulls=0):
    return joins.KeyObservation(table, tuple(columns), rows, nulls, duplicates, "2026-11-20T10:00:00Z", source, "saved")


def run(model, session=None, snap=None, observations=(), files=None):
    return mc.check(files or [("m.malloy", model)], mc.Evidence(session, snap, observations))


def only(report, check, status=None):
    return [f for f in report.findings if f.check == check and (status is None or f.status == status)]


def contradicted(report):
    return [f for f in report.findings if f.status == mc.CONTRADICTED]


# ------------------------------------------------------------------------------------------- structural


CUSTOMERS = "source: customers is duckdb.table('wh.customers') extend { primary_key: id }\n"

REJECTED = [
    # join_one: Cannot use with unless source has a primary key
    ("join_without_key", "source: customers is duckdb.table('wh.customers')\nsource: orders is duckdb.table('wh.orders') extend { join_one: customers with customer_id }\n", 2),
    ("join_without_key", "source: orders is duckdb.table('wh.orders') extend { join_one: c is duckdb.table('wh.customers') with customer_id }\n", 1),
    # Foreign key join not legal in join_many
    ("join_many_with", CUSTOMERS + "source: orders is duckdb.table('wh.orders') extend { join_many: customers with customer_id }\n", 2),
    # Reference to undefined object (a source used before it is defined, a source joining or extending itself)
    ("undefined_source", "source: orders is duckdb.table('wh.orders') extend { join_one: customers with customer_id }\n" + CUSTOMERS, 1),
    ("join_cycle", "source: orders is duckdb.table('wh.orders') extend { primary_key: order_id\n  join_one: o2 is orders with order_id }\n", 1),
    ("join_cycle", "source: a is a extend { dimension: x is 1 }\n", 1),
    ("undefined_source", "source: child is nope extend { dimension: a is 1 }\n", 1),
    ("undefined_source", "run: nope -> { group_by: status }\n", 1),
    # Cannot redefine
    ("duplicate_name", CUSTOMERS + CUSTOMERS, 2),
    ("duplicate_name", "source: orders is duckdb.table('wh.orders') extend { dimension: a is 1\n  dimension: a is 2 }\n", 2),
    ("duplicate_name", "source: orders is duckdb.table('wh.orders') extend { dimension: a is 1\n  measure: a is count() }\n", 2),
    ("duplicate_name", "source: orders is duckdb.table('wh.orders') extend { measure: v is count()\n  view: v is { aggregate: v } }\n", 2),
    ("duplicate_name", CUSTOMERS + "source: orders is duckdb.table('wh.orders') extend { dimension: customers is 1\n  join_one: customers with customer_id }\n", 3),
    ("duplicate_name", "source: base is duckdb.table('wh.orders') extend { dimension: a is 1 }\nsource: child is base extend { dimension: a is 2 }\n", 2),
    # 'x' is not defined
    ("undefined_field", "source: orders is duckdb.table('wh.orders') extend { measure: revenue is amount.sum()\n  view: v is { aggregate: revenu } }\n", 2),
    ("undefined_field", "source: orders is duckdb.table('wh.orders') extend { measure: revenue is amount.sum()\n  view: v is { group_by: status; nest: nope } }\n", 2),
    ("undefined_field", "source: orders is duckdb.table('wh.orders') extend { measure: revenue is amount.sum() }\nrun: orders -> nope\n", 2),
    ("undefined_field", "source: orders is duckdb.table('wh.orders') extend { measure: revenue is amount.sum()\n  view: v1 is { aggregate: revenue }\n  view: v3 is nope + { limit: 3 } }\n", 3),
    # Cannot use a scalar field in an aggregate operation
    ("undefined_field", "source: orders is duckdb.table('wh.orders') extend { dimension: d is amount * 2\n  view: v is { aggregate: d } }\n", 2),
]


@pytest.mark.parametrize("check, model, line", REJECTED, ids=[f"{c}-{i}" for i, (c, _m, _l) in enumerate(REJECTED)])
def test_each_model_malloy_rejects_has_one_contradicted_structural_finding(check, model, line):
    report = run(model)
    [found] = contradicted(report)
    assert found.check == check and found.at == f"m.malloy:{line}"
    assert found.why.startswith("contradicted by the model text") and found.next


def test_a_two_source_cycle_is_one_finding_not_a_forward_reference_as_well():
    model = ("source: a is duckdb.table('wh.orders') extend { primary_key: order_id\n  join_one: b with customer_id }\n"
             "source: b is duckdb.table('wh.customers') extend { primary_key: id\n  join_one: a with id }\n")
    [found] = contradicted(run(model))
    assert found.check == "join_cycle" and found.what == "cycle a -> b -> a"


ACCEPTED = """
source: customers is duckdb.table('wh.customers') extend { primary_key: id
  measure: customer_count is count() }
source: c2 is customers extend { dimension: z is 1 }
source: orders is duckdb.table('wh.orders') extend {
  primary_key: order_id
  join_one: customers with customer_id
  join_one: c2 with customer_id
  join_many: items is duckdb.table('wh.items') on order_id = items.order_id
  join_cross: everyone is customers
  dimension:
    big is amount > 100
    kind is pick 'big' when amount > 100 else 'small'
    st is status ? 'a' | 'b'
    month_of is created_at.month
    day_of is created_at::date
    k is case when status = 'a' then 1 else 2 end
    m is status ~ r'^a'
    cname is concat(customers.name, '-', status)
    recent is created_at > @2024-01-01
    nn is customer_id is not null
  measure:
    revenue is amount.sum()
    rev2 is sum(amount)
    paid_revenue is revenue { where: status = 'paid' }
    customers_seen is count(customer_id)
    c_rev is customers.customer_count
    share is revenue / all(revenue)
    item_rows is items.count()
  view: by_status is {
    group_by: status, customers.region
    aggregate: revenue, n is count()
    calculate: prev is lag(revenue)
    having: revenue > 10
    order_by: revenue desc
    limit: 10
    nest: inner_v is { group_by: kind; aggregate: rev2 }
  }
  view: top5 is by_status + { limit: 5 }
  view: local is { group_by: s is status; aggregate: c is count(); order_by: c desc }
}
source: lean is duckdb.table('wh.orders') extend { except: amount
  dimension: amount is 1 }
source: picked is duckdb.table('wh.orders') extend { accept: order_id, amount
  dimension: status is 'x' }
run: orders -> by_status
query: q is orders -> { group_by: status; aggregate: revenue }
source: from_q is q extend { dimension: zz is 1 }
"""


def test_a_model_malloy_accepts_has_nothing_contradicted_even_with_every_column_list():
    report = run(ACCEPTED, snap=snapshot())
    assert contradicted(report) == []
    assert {f.status for f in only(report, "missing_column")} == {mc.CONFIRMED}
    assert any("from_q" in u["what"] and "built on q" in u["reason"] for u in report.unknown)


def test_a_source_imported_from_another_file_is_defined(tmp_path):
    (tmp_path / "base.malloy").write_text(CUSTOMERS)
    (tmp_path / "use.malloy").write_text('import "base.malloy"\nsource: orders is duckdb.table(\'wh.orders\') extend { join_one: customers with customer_id }\n')
    report = mc.check_path(tmp_path / "use.malloy")
    assert contradicted(report) == [] and report.unknown == []
    (tmp_path / "use.malloy").write_text('import "missing.malloy"\nsource: orders is duckdb.table(\'wh.orders\') extend { join_one: customers with customer_id }\n')
    report = mc.check_path(tmp_path / "use.malloy")
    assert contradicted(report) == [] and "the imported file was not read" in report.unknown[0]["reason"]


# ------------------------------------------------------------------------------------------- primary keys


def test_a_key_narrower_than_the_derived_grain_is_contradicted(session):
    [found] = only(run("source: daily is bigquery.table('ds.daily') extend { primary_key: customer_id }\n", session), "primary_key")
    assert found.status == mc.CONTRADICTED
    assert found.why.startswith("contradicted by the query of model daily, whose grain is (customer_id, order_day)")
    assert "customer_id repeats wherever one customer_id has several order_day" in found.why


def test_a_key_narrower_than_a_declared_unique_key_is_contradicted(session):
    [found] = only(run("source: orders is bigquery.table('wh.orders') extend { primary_key: order_id }\n", session), "primary_key")
    assert found.status == mc.CONTRADICTED
    assert found.why.startswith("contradicted by declared uniqueKey in the declaration of orders (order_id, line_no)")


def test_a_key_with_no_evidence_is_unconfirmed_and_names_the_command_that_would_measure_it(session):
    [found] = only(run("source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n", session), "primary_key")
    assert found.status == mc.UNCONFIRMED and found.why.startswith("unconfirmed: no grain evidence for wh.customers")
    assert found.next.startswith("python -m kumosql profile-table p.wh.customers --columns id --dry-run")
    # without any project the same key is unconfirmed too: a key is never called wrong without evidence
    [alone] = only(run("source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n"), "primary_key")
    assert alone.status == mc.UNCONFIRMED


def test_a_key_the_evidence_agrees_with_is_confirmed(session):
    [found] = only(run("source: dim is bigquery.table('ds.customer_dim') extend { primary_key: customer_id }\n", session), "primary_key")
    assert found.status == mc.CONFIRMED and found.why == "confirmed by declared uniqueKey on model customer_dim (customer_id)"
    [narrower] = only(run("source: d is bigquery.table('ds.daily') extend { dimension: k is 1\n  primary_key: order_day }\n", session), "primary_key")
    assert narrower.status == mc.CONTRADICTED  # order_day alone is part of (customer_id, order_day) too

    # the declared grain allows repeats, but the data had none when it was measured: neither alone decides
    [both] = only(run("source: o is bigquery.table('wh.orders') extend { primary_key: order_id }\n", session,
                      observations=[observation("p.wh.orders", ["order_id"], 10, 0)]), "primary_key")
    assert both.status == mc.UNCONFIRMED
    assert both.why.startswith("unconfirmed: the saved join profile of 2026-11-20: no repeats on (order_id) in 10 rows")
    assert "but declared uniqueKey in the declaration of orders (order_id, line_no)" in both.why


def test_saved_measurements_confirm_or_contradict_a_key_with_their_date(session):
    model = "source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n"
    [unique] = only(run(model, session, observations=[observation("p.wh.customers", ["id"], 50, 0, "profile_table")]), "primary_key")
    assert unique.status == mc.CONFIRMED and "saved data profile of 2026-11-20: no repeats on (id) in 50 rows" in unique.why
    [repeats] = only(run(model, session, observations=[observation("wh.customers", ["id"], 50, 3)]), "primary_key")
    assert repeats.status == mc.CONTRADICTED and repeats.why.startswith("contradicted by the saved join profile of 2026-11-20: (id) has 3 duplicate rows in 50")


def test_a_saved_data_profile_file_is_read_as_a_measurement(tmp_path, session):
    profile = DataProfile("p.wh.customers", "bigquery", "2026-11-21T09:00:00Z", 40,
                          [ColumnProfile(name="id", type="INT64", kind="numeric", non_null=40, null_count=0, null_fraction=0.0, distinct=38)])
    store.save(profile, directory=tmp_path)
    [found] = only(run("source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n", session,
                       observations=joins.load_observations(tmp_path)), "primary_key")
    assert found.status == mc.CONTRADICTED and "saved data profile of 2026-11-21: (id) has 2 duplicate rows in 40" in found.why


def test_a_filtered_source_is_not_contradicted_by_whole_table_repeats(session):
    model = "source: latest is bigquery.table('wh.customers') extend { where: region = 'eu'\n  primary_key: id }\n"
    [found] = only(run(model, session, observations=[observation("wh.customers", ["id"], 50, 3)]), "primary_key")
    assert found.status == mc.UNCONFIRMED and "does not decide a source that keeps only some rows" in found.why


def test_a_malloy_key_given_to_the_server_does_not_confirm_itself(tmp_path, project):
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "m.malloy").write_text("source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n")
    with_malloy = Session(load_sqlx_project(project), root=project, profiles=tmp_path / "profiles", malloy=[folder])
    assert with_malloy.grain_claim("p.wh.customers").kind == "malloy"  # the server itself takes the key as a stated grain
    [found] = only(mc.check_path(folder, mc.Evidence(with_malloy)), "primary_key")
    assert found.status == mc.UNCONFIRMED


# ------------------------------------------------------------------------------------------- join_one


JOINS = (CUSTOMERS.replace("duckdb", "bigquery")
         + "source: dim is bigquery.table('ds.customer_dim') extend { primary_key: customer_id }\n"
         + "source: sales is bigquery.table('ds.daily') extend {\n  join_one: dim with customer_id\n  join_one: c is customers on customer_id = c.id\n}\n")


def test_a_join_one_whose_key_repeats_in_the_saved_measurement_fans_out(session):
    report = run(JOINS, session, observations=[observation("p.wh.customers", ["id"], 50, 3)])
    found = {f.what: f for f in only(report, "join_fan_out")}
    bad = found["join_one c to wh.customers on (id)"]
    assert bad.status == mc.CONTRADICTED and "3 duplicate rows in 50" in bad.why and "Malloy expects at most one c row per sales row" in bad.why
    assert bad.next.startswith("Use join_many for c")
    good = found["join_one dim to ds.customer_dim on (customer_id)"]
    assert good.status == mc.CONFIRMED


def test_a_join_one_with_no_measurement_is_unconfirmed_and_offers_the_join_profile(session):
    model = "source: sales is bigquery.table('ds.daily') extend { join_one: c is bigquery.table('wh.customers') on customer_id = c.id and c.region = 'eu' }\n"
    [found] = only(run(model, session), "join_fan_out")
    assert found.status == mc.UNCONFIRMED and found.next.startswith("python -m kumosql profile-table p.wh.customers --columns id")
    model = "source: sales is bigquery.table('ds.daily') extend { join_one: c is bigquery.table('wh.orders') on customer_id = c.customer_id and order_day = c.created_at }\n"
    [pair] = only(run(model, session), "join_fan_out")
    assert pair.status == mc.UNCONFIRMED and pair.next.startswith("join_profile left=ds.daily right=wh.orders keys=customer_id,order_day=created_at")


def test_a_join_condition_that_is_not_column_equalities_is_never_decided(session):
    model = "source: sales is bigquery.table('ds.daily') extend { join_one: c is bigquery.table('wh.customers') on customer_id > c.id }\n"
    [found] = only(run(model, session, observations=[observation("p.wh.customers", ["id"], 50, 3)]), "join_fan_out")
    assert found.status == mc.UNCONFIRMED and "not only column equalities" in found.why


# ------------------------------------------------------------------------------------------- columns


def test_a_column_missing_from_the_schema_snapshot_is_contradicted():
    model = (CUSTOMERS + "source: orders is duckdb.table('wh.orders') extend { join_one: customers with customer_id\n"
             "  dimension: r is customers.nope\n  dimension: a is not_a_column + 1 }\n")
    found = {f.what: f for f in only(run(model, snap=snapshot()), "missing_column", mc.CONTRADICTED)}
    assert found["1 of 2 columns read from wh.orders missing"].why == "contradicted by the schema snapshot: not_a_column not among the columns of wh.orders"
    assert "nope not among the columns of wh.customers" in found["1 of 1 column read from wh.customers missing"].why


def test_a_column_missing_from_the_model_outputs_is_contradicted_and_one_present_is_confirmed(session):
    model = "source: daily is bigquery.table('ds.daily') extend { measure: r is revenue_x.sum()\n  dimension: d is order_day }\n"
    report = run(model, session)
    [found] = only(report, "missing_column")
    assert found.status == mc.CONTRADICTED and found.why == "contradicted by the output columns of model daily: revenue_x not among the columns of ds.daily"
    [fine] = only(run("source: dim is bigquery.table('ds.customer_dim') extend { dimension: r is region }\n", session), "missing_column")
    assert fine.status == mc.CONFIRMED and fine.why == "confirmed by the output columns of model customer_dim"


def test_columns_of_a_table_nothing_describes_are_unconfirmed(session):
    [found] = only(run("source: customers is bigquery.table('wh.customers') extend { dimension: n is name }\n", session), "missing_column")
    assert found.status == mc.UNCONFIRMED and found.why == "unconfirmed: no column list for wh.customers" and "--schema-snapshot" in found.next


def test_a_field_with_the_name_of_a_column_is_contradicted_by_the_column_list():
    [found] = contradicted(run("source: orders is duckdb.table('wh.orders') extend { dimension: amount is amount * 100 }\n", snap=snapshot()))
    assert found.check == "duplicate_name" and found.why.startswith("contradicted by the schema snapshot: wh.orders has a column amount")


# ------------------------------------------------------------------------------------------- ratio measures


def test_sum_or_average_of_a_ratio_of_aggregates_is_contradicted_by_lineage(session):
    model = "source: daily is bigquery.table('ds.daily') extend { measure: a is discount_rate.avg()\n  measure: s is sum(discount_rate)\n  measure: ok is revenue.sum() }\n"
    found = {f.what: f for f in only(run(model, session), "ratio_measure")}
    assert set(found) == {"measure a averages discount_rate", "measure s is discount_rate.sum()"}
    assert all(f.status == mc.CONTRADICTED and f.why.startswith("contradicted by the lineage of model daily: discount_rate is") for f in found.values())


def test_a_ratio_known_only_by_its_name_is_unconfirmed():
    [found] = only(run("source: orders is duckdb.table('wh.orders') extend { measure: m is margin_pct.avg() }\n"), "ratio_measure")
    assert found.status == mc.UNCONFIRMED and "the name margin_pct suggests a ratio" in found.why and "kumosql.ratio_check" in found.why


def test_the_ratio_detector_hook_decides_when_the_module_offers_a_verdict(session, monkeypatch):
    calls = []

    def malloy_verdict(session_, key, column, function):
        calls.append((key, column, function))
        return (mc.CONFIRMED, "confirmed by the ratio detector: weighted correctly")

    fake = types.ModuleType("kumosql.ratio_check")
    fake.malloy_verdict = malloy_verdict
    monkeypatch.setitem(sys.modules, "kumosql.ratio_check", fake)
    [found] = only(run("source: daily is bigquery.table('ds.daily') extend { measure: a is discount_rate.avg() }\n", session), "ratio_measure")
    assert found.status == mc.CONFIRMED and calls == [("p.ds.daily", "discount_rate", "avg")]


# ------------------------------------------------------------------------------------------- the CLI


def cli_files(tmp_path, model):
    folder = tmp_path / "models"
    folder.mkdir(exist_ok=True)
    (folder / "shop.malloy").write_text(model)
    return folder


def test_the_json_report_has_counts_findings_and_the_evidence_used(tmp_path, project, capsys, monkeypatch):
    block_network(monkeypatch)
    folder = cli_files(tmp_path, "source: orders is bigquery.table('wh.orders') extend { primary_key: order_id }\n"
                                 "source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n")
    code = mc.main([str(folder), "--project", str(project), "--profiles", str(tmp_path / "profiles"), "--json"])
    body = json.loads(capsys.readouterr().out)
    assert code == 1
    assert set(body) == {"checked", "evidence", "counts", "findings", "unknown"}
    assert body["checked"] == {"files": ["shop.malloy"], "sources": 2, "joins": 0, "measures": 0}
    assert body["evidence"] == {"project": str(project), "schema_snapshot": False, "saved_measurements": 0}
    assert body["counts"] == {"contradicted": 1, "unconfirmed": 3, "confirmed": 0}
    for item in body["findings"]:
        assert set(item) <= {"check", "status", "source", "at", "what", "why", "next"} and item["check"] in mc.CHECKS
        assert item["why"].startswith({"contradicted": "contradicted by ", "unconfirmed": "unconfirmed: ", "confirmed": "confirmed by "}[item["status"]])
    assert [f["status"] for f in body["findings"]] == sorted((f["status"] for f in body["findings"]), key=mc.STATUSES.index)


def test_exit_codes_say_whether_anything_is_contradicted_or_the_input_could_not_be_read(tmp_path, capsys, monkeypatch):
    block_network(monkeypatch)
    folder = cli_files(tmp_path, CUSTOMERS)
    assert mc.main([str(folder), "--profiles", str(tmp_path / "p")]) == 0  # an unconfirmed key is not a failure
    out = capsys.readouterr().out
    assert out.startswith("check-malloy: 0 contradicted, 2 unconfirmed, 0 confirmed (1 source in 1 file)") and "UNCONFIRMED  primary_key" in out
    (folder / "shop.malloy").write_text(CUSTOMERS + CUSTOMERS)
    assert mc.main([str(folder), "--profiles", str(tmp_path / "p")]) == 1
    assert mc.main([str(tmp_path / "missing"), "--profiles", str(tmp_path / "p")]) == 2
    (tmp_path / "notes.txt").write_text("x")
    assert mc.main([str(tmp_path / "notes.txt")]) == 2
    assert "is not a .malloy file" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        mc.main([str(folder), "--docs", str(tmp_path)])  # --docs needs --project


def test_the_command_is_declared_once_in_pyproject():
    from kumosql.__main__ import resolve

    assert resolve("check-malloy") == "kumosql.malloy_check:main"


# ------------------------------------------------------------------------------------------- the tool


def server(tmp_path, project, files=None, **extra):
    folder = tmp_path / "models"
    folder.mkdir(exist_ok=True)
    for name, text in (files or {}).items():
        (folder / name).write_text(text)
    session = Session(load_sqlx_project(project), root=project, profiles=tmp_path / "profiles", malloy=[folder], **extra)
    return mcp_server.AgentServer(session)


def test_check_malloy_reads_the_server_s_malloy_folders_and_stays_in_bounds(tmp_path, project, monkeypatch):
    block_network(monkeypatch)
    many = "".join(f"source: s{i} is bigquery.table('wh.customers')\nsource: t{i} is bigquery.table('ds.daily') extend {{ join_one: s{i} with customer_id }}\n"
                   for i in range(30))
    agent = server(tmp_path, project, {"many.malloy": many})
    body, failed, used = ask(agent, "check_malloy")
    assert not failed and used <= BOUND
    assert body["counts"]["contradicted"] == 30 and body["findings"][0]["status"] == "contradicted"
    assert body["findings_shown"].startswith("1-") and body["findings_shown"].endswith(" of 60")
    assert "offset=" in body["more"] and body["checked"].startswith("60 sources, 30 joins, 0 measures in 1 file (the --malloy folders)")
    assert body["next"] and any(u["what"] == "which columns the warehouse tables have" for u in body["unknown"])
    wider, _, used = ask(agent, "check_malloy", status="contradicted", max_bytes=20000)
    assert used <= 20000 and {f["status"] for f in wider["findings"]} == {"contradicted"} and len(wider["findings"]) > len(body["findings"])


def test_check_malloy_takes_text_or_a_path_inside_the_project_and_refuses_one_outside(tmp_path, project, monkeypatch):
    block_network(monkeypatch)
    agent = server(tmp_path, project)
    body, failed, used = ask(agent, "check_malloy", text="source: daily is bigquery.table('ds.daily') extend { primary_key: customer_id }\n")
    assert not failed and used <= BOUND and body["counts"]["contradicted"] == 1
    assert body["findings"][0]["why"].startswith("contradicted by the query of model daily")
    body, failed, _ = ask(agent, "check_malloy", path="../outside.malloy")
    assert failed and "inside the project folder" in body["error"]
    body, failed, _ = ask(agent, "check_malloy")
    assert failed and body["error"] == "the --malloy folders hold no .malloy files"
    (project / "semantic").mkdir(exist_ok=True)
    (project / "semantic" / "m.malloy").write_text("source: dim is bigquery.table('ds.customer_dim') extend { primary_key: customer_id }\n")
    body, failed, _ = ask(agent, "check_malloy", path="semantic")
    assert not failed and body["counts"] == {"contradicted": 0, "unconfirmed": 0, "confirmed": 2}
    assert body["answer"].startswith("Nothing contradicted or unconfirmed")


def test_an_unconfirmed_key_in_the_tool_names_the_profile_table_call(tmp_path, project, monkeypatch):
    block_network(monkeypatch)
    body, _, used = ask(server(tmp_path, project), "check_malloy", text="source: customers is bigquery.table('wh.customers') extend { primary_key: id }\n")
    assert used <= BOUND and body["counts"]["contradicted"] == 0
    assert body["next"][0].startswith("profile_table table=wh.customers columns=id")
