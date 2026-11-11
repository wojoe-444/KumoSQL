"""``python -m kumosql pipeline-report --boundary FILE``: the producers and consumers file, for the command line.

Same file, same validation errors as ``python -m kumosql mcp --boundary``; the report gains one ``boundary`` key only when the
option is given.
"""

import json

import pytest

from kumosql import boundary as outside, mcp_server
from kumosql.cli import pipeline_main
from test_boundary import BOUNDARY, FILES, PROJECT, SCHEMA
from test_mcp_server import block_network, write_project


@pytest.fixture
def setup(tmp_path):
    root = write_project(tmp_path / "proj", PROJECT)
    folder = tmp_path / "outside"
    for name, text in FILES.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    spec = folder / "boundary.yaml"
    spec.write_text(BOUNDARY)
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    return root, spec, schema, tmp_path


def run(root, schema, tmp_path, *extra, name="out.json"):
    out = tmp_path / name
    code = pipeline_main([str(root), "--source-schema", str(schema), *extra, "-o", str(out)])
    return code, (json.loads(out.read_text()) if out.exists() else None)


def test_pipeline_report_with_boundary_lists_producer_sql_as_external_models_and_outside_readers(setup, monkeypatch):
    root, spec, schema, tmp_path = setup
    block_network(monkeypatch)
    code, data = run(root, schema, tmp_path, "--boundary", str(spec))
    assert code == 0
    section = data["boundary"]
    (producer,) = section["external_models"]
    assert producer["table"] == "p.lookups.region" and producer["status"] == "sql_parsed"
    assert producer["built_by"] == {"kind": "scheduled_query", "id": "1234", "url": "https://example.test/q/1234"}
    assert producer["file"] == "external/region.sql"
    assert producer["reads"] == ["legacy.geo.regions", "p.lookups.region_extra"]
    columns = {c["column"]: c for c in producer["columns"]}
    assert columns["region_name"]["sources"] == ["legacy.geo.regions.label"]
    readers = {entry["table"]: entry["readers"] for entry in section["readers_outside_project"]}
    assert readers["p.ds.orders"][0]["name"] == "sales_semantic_model" and readers["p.ds.orders"][0]["how"] == "reads (Malloy)"
    assert readers["p.ds.weekly"][0]["name"] == "weekly_dashboard" and readers["p.ds.weekly"][0]["how"] == "reads"
    assert section["counts"] == {"producers": 1, "consumers": 2, "tables_read_outside_project": 2, "outside_readers": 2}
    assert "invisible" in section["note"] and section["file"] == "boundary.yaml"


def test_the_report_json_keeps_its_shape_without_the_option_and_gains_only_the_boundary_key_with_it(setup):
    root, spec, schema, tmp_path = setup
    _, plain = run(root, schema, tmp_path, name="plain.json")
    _, with_boundary = run(root, schema, tmp_path, "--boundary", str(spec), name="with.json")
    assert "boundary" not in plain
    assert set(with_boundary) - set(plain) == {"boundary"} and set(plain) <= set(with_boundary)
    assert {key: value for key, value in with_boundary.items() if key != "boundary"} == plain


def test_assess_with_boundary_adds_the_readers_outside_the_project_for_the_target(setup):
    root, spec, schema, tmp_path = setup
    code, data = run(root, schema, tmp_path, "--assess", "drop_column", "--target", "p.ds.orders.amount", "--boundary", str(spec))
    assert code == 0
    (reader,) = data["boundary"]["readers_outside_project"]
    assert reader["name"] == "sales_semantic_model" and reader["columns"] == ["amount"]
    assert data["boundary"]["counts"] == {"outside_readers": 1}
    _, plain = run(root, schema, tmp_path, "--assess", "drop_column", "--target", "p.ds.orders.amount", name="plain.json")
    assert "boundary" not in plain and {k: v for k, v in data.items() if k != "boundary"} == plain
    # a column the outside readers do not read
    _, data = run(root, schema, tmp_path, "--assess", "drop_column", "--target", "p.ds.orders.region_name", "--boundary", str(spec), name="other.json")
    assert data["boundary"]["readers_outside_project"] == []


def test_a_consumer_whose_files_cannot_be_read_is_unknown_in_the_report_not_absent(setup):
    root, spec, schema, tmp_path = setup
    spec.write_text(BOUNDARY.replace("semantic/sales.malloy", "semantic/missing/*.malloy"))
    _, data = run(root, schema, tmp_path, "--boundary", str(spec))
    assert [u for u in data["boundary"]["unknown"] if u["what"] == "sales_semantic_model"] == [
        {"what": "sales_semantic_model", "reason": "no file matches the path"}
    ]
    assert all(entry["table"] != "p.ds.orders" for entry in data["boundary"]["readers_outside_project"])


def test_a_producer_without_sql_is_listed_with_why_nothing_can_be_followed(setup):
    root, spec, schema, tmp_path = setup
    spec.write_text(BOUNDARY.replace("consumers:", """  - table: raw.events
    by: {kind: notebook_job, id: nb-7}
    sql_unavailable: the notebook code was not exported
consumers:"""))
    _, data = run(root, schema, tmp_path, "--boundary", str(spec))
    events = next(m for m in data["boundary"]["external_models"] if m["table"] == "p.raw.events")
    assert events["status"] == "sql_not_available" and "notebook code was not exported" in events["unknown"]


def test_a_bad_boundary_file_stops_pipeline_report_with_the_message_the_mcp_command_gives(setup, tmp_path, capsys):
    root, _, schema, _ = setup
    bad = tmp_path / "bad.yaml"
    bad.write_text("producers:\n  - {tabel: a.b, by: {kind: q}}\n")
    with pytest.raises(SystemExit) as caught:
        pipeline_main([str(root), "--source-schema", str(schema), "--boundary", str(bad)])
    assert caught.value.code == 2
    cli_error = capsys.readouterr().err
    with pytest.raises(SystemExit):
        mcp_server.main([str(root), "--boundary", str(bad)])
    mcp_error = capsys.readouterr().err
    assert "--boundary: boundary file bad.yaml" in cli_error and "did you mean table" in cli_error
    assert cli_error.split("error:", 1)[1] == mcp_error.split("error:", 1)[1]
    with pytest.raises(outside.BoundaryError):
        outside.read_file(bad)


def test_a_missing_boundary_file_stops_pipeline_report_before_any_report_is_written(setup, tmp_path, capsys):
    root, _, schema, _ = setup
    out = tmp_path / "never.json"
    with pytest.raises(SystemExit) as caught:
        pipeline_main([str(root), "--source-schema", str(schema), "--boundary", str(tmp_path / "nope.yaml"), "-o", str(out)])
    assert caught.value.code == 2 and "--boundary" in capsys.readouterr().err and not out.exists()


def test_a_producer_table_the_project_does_not_have_stops_the_report_naming_close_tables(setup, capsys):
    root, spec, schema, tmp_path = setup
    spec.write_text(BOUNDARY.replace("lookups.region", "lookups.regions"))
    out = tmp_path / "never.json"
    with pytest.raises(SystemExit) as caught:
        pipeline_main([str(root), "--source-schema", str(schema), "--boundary", str(spec), "-o", str(out)])
    error = capsys.readouterr().err
    assert caught.value.code == 2 and "lookups.regions" in error and "close: p.lookups.region" in error and not out.exists()
    # a table the project builds itself cannot have an outside producer
    spec.write_text(BOUNDARY.replace("lookups.region", "orders"))
    with pytest.raises(SystemExit):
        pipeline_main([str(root), "--source-schema", str(schema), "--boundary", str(spec), "-o", str(out)])
    assert "built by the project" in capsys.readouterr().err


def test_a_scoped_report_lists_outside_readers_only_for_tables_in_the_scope(setup, monkeypatch):
    from kumosql import cli

    root, spec, schema, tmp_path = setup
    monkeypatch.setattr(cli, "get_scope", lambda name: _scope("orders only"))
    _, data = run(root, schema, tmp_path, "--boundary", str(spec), "--scope", "orders only")
    tables = [entry["table"] for entry in data["boundary"]["readers_outside_project"]]
    assert tables == ["p.ds.orders"] and "scope" in data["boundary"]["scope_note"]


def _scope(name):
    from kumosql.scopes import parse_scope

    return parse_scope({"name": name, "fields": {"name": ["orders"]}})
