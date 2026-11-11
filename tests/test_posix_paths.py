"""Every file path the agent-facing server and the reports show is a POSIX path below the project folder, on every platform.

Windows is not needed: ``PureWindowsPath`` gives the text a Windows loader produces (``definitions\\orders.sqlx``), and the
tests feed that text to the display layer and check the answers show ``definitions/orders.sqlx``.
"""

import json
from pathlib import PureWindowsPath

from kumosql import boundary as outside, column_docs_files, docs_keys, malloy_keys, mcp_server
from kumosql.mcp_session import Session
from kumosql.paths import posix_path, relative_posix
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.pipeline_types import Model, Target
from test_boundary import BOUNDARY, make
from test_mcp_server import ask, write_project

PROJECT_FILES = {
    "raw.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS amount\n',
    "orders.sqlx": (
        'config { type: "view", columns: {id: "Order id."} }\n'
        'SELECT id, amount FROM ${ref("raw")} WHERE 1 = 1 AND amount > 0\n'
    ),
    "report.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("orders")}\n',
}


def strings(value):
    """Every text in an answer, however deep."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in (*value.keys(), *value.values()):
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def has_backslash(value) -> bool:
    return any("\\" in text for text in strings(value))


def windows_text(relative: str) -> str:
    """What ``str(path.relative_to(root))`` gives on Windows for ``relative``."""

    return str(PureWindowsPath(*relative.split("/")))


def windows_server(tmp_path):
    """A server whose models carry paths as a Windows loader spells them."""

    folder = write_project(tmp_path / "proj", PROJECT_FILES)
    pipeline = load_sqlx_project(folder)
    for model in pipeline.models.values():
        if model.path:
            model.path = windows_text(model.path)
            assert "\\" in model.path
    return mcp_server.AgentServer(Session(pipeline, root=folder)), folder


def test_the_path_helpers_turn_windows_paths_into_posix_paths_below_the_root():
    assert posix_path(PureWindowsPath("definitions", "orders.sqlx")) == "definitions/orders.sqlx"
    assert posix_path("definitions\\orders.sqlx") == "definitions/orders.sqlx"
    assert posix_path("definitions/orders.sqlx") == "definitions/orders.sqlx" and posix_path(None) == ""
    root = PureWindowsPath("C:\\work\\proj")
    assert relative_posix(root / "definitions" / "orders.sqlx", root) == "definitions/orders.sqlx"
    try:
        relative_posix(PureWindowsPath("C:\\elsewhere\\x.sqlx"), root)
    except ValueError:
        pass
    else:
        raise AssertionError("a path outside the root must not get a label")


def test_a_model_keeps_its_path_in_posix_form_whatever_the_loader_wrote():
    model = Model(Target("p", "ds", "orders"), "table", "SELECT 1", path="definitions\\orders.sqlx")
    assert model.path == "definitions/orders.sqlx"
    assert model.asset_identity.asset_path == "definitions/orders.sqlx"
    assert Model(Target("p", "ds", "orders"), "table", "SELECT 1").path is None


def test_describe_table_and_impact_show_forward_slashes_for_windows_style_paths(tmp_path):
    server, _ = windows_server(tmp_path)
    body, failed, _ = ask(server, "describe_table", table="orders")
    assert not failed and body["file"] == "definitions/orders.sqlx"
    assert "definitions/orders.sqlx" in json.dumps(body["next"]) and not has_backslash(body)
    body, failed, _ = ask(server, "impact", table="raw", column="amount")
    assert not failed and body["next"][0].startswith("Open definitions/orders.sqlx") and not has_backslash(body)


def test_clean_query_writes_a_valid_unified_diff_header_for_a_windows_style_path(tmp_path):
    server, folder = windows_server(tmp_path)
    before = (folder / "definitions" / "orders.sqlx").read_text()
    body, failed, _ = ask(server, "clean_query", table="orders")
    assert not failed and body["verdict"] == "cleaned"
    assert body["diff"][:2] == ["--- a/definitions/orders.sqlx", "+++ b/definitions/orders.sqlx"]
    assert not has_backslash(body)
    assert (folder / "definitions" / "orders.sqlx").read_text() == before  # read through the real path, never written


def test_list_tables_folder_filter_matches_forward_and_backslash_spellings(tmp_path):
    server, _ = windows_server(tmp_path)
    for spelling in ("definitions/or", "definitions\\or"):
        body, failed, _ = ask(server, "list_tables", folder=spelling)
        assert not failed and "ds.orders" in json.dumps(body) and "ds.report" not in json.dumps(body)


def test_the_loader_labels_files_and_diagnostics_with_posix_paths(tmp_path):
    folder = write_project(tmp_path / "proj", {"sub/deep/orders.sqlx": 'config { type: "table" }\nSELECT 1 AS id\n'})
    pipeline = load_sqlx_project(folder)
    assert [m.path for m in pipeline.models.values()] == ["definitions/sub/deep/orders.sqlx"]


def test_a_column_description_from_a_docs_page_names_the_page_with_forward_slashes(tmp_path):
    from kumosql import column_meaning

    folder = write_project(tmp_path / "proj", PROJECT_FILES)
    docs = tmp_path / "docs"
    (docs / "team").mkdir(parents=True)
    (docs / "team" / "views.md").write_text("## raw\n\n| Column | Description |\n| --- | --- |\n| `amount` | Amount in cents. |\n")
    pipeline = load_sqlx_project(folder)
    column_docs_files.apply_docs(pipeline, [docs])
    session = Session(pipeline, root=folder)
    assert column_meaning.item_of(session, "p.ds.raw", "amount", 200)["described_in"] == "docs team/views.md"
    facts = column_meaning.facts(session, "p.ds.raw", "amount")
    facts.labels["description"] = "docs team\\views.md"  # a label spelled as on Windows
    assert column_meaning.item_of(session, "p.ds.raw", "amount", 200)["described_in"] == "docs team/views.md"


def test_the_docs_and_malloy_key_readers_label_files_with_forward_slashes(tmp_path):
    (tmp_path / "docs" / "team").mkdir(parents=True)
    (tmp_path / "docs" / "team" / "keys.md").write_text("## `orders`\n\nPrimary key: order_id\n")
    (tmp_path / "bi" / "sales").mkdir(parents=True)
    (tmp_path / "bi" / "sales" / "m.malloy").write_text("source: orders is bigquery.table('ds.orders') extend { primary_key: order_id }\n")
    for item in docs_keys.read_folder(tmp_path / "docs"):
        assert "\\" not in repr(item) and "team/keys.md" in repr(item)
    for item in malloy_keys.read_folder(tmp_path / "bi"):
        assert "\\" not in repr(item) and "sales/m.malloy" in repr(item)


def test_boundary_evidence_and_producer_files_use_forward_slashes(tmp_path, monkeypatch):
    real_glob = outside.glob.glob

    def windows_glob(pattern, **kwargs):
        return [name.replace("/", "\\") for name in real_glob(pattern, **kwargs)]  # os.sep as Windows spells it

    monkeypatch.setattr(outside.glob, "glob", windows_glob)
    server, session, _ = make(tmp_path)
    body, failed, _ = ask(server, "impact", table="orders", column="amount", change="drop_column")
    assert not failed
    readers = {item["name"]: item for item in body["readers_outside_project"]}
    assert readers["sales_semantic_model"]["evidence"] == ["semantic/sales.malloy"]
    assert not has_backslash(body)


def test_boundary_sql_files_are_named_with_forward_slashes_in_the_report_and_the_answers(tmp_path):
    from kumosql import boundary_report

    assert outside.SqlFile("external\\region.sql", "SELECT 1").path == "external/region.sql"
    server, session, _ = make(tmp_path)
    section = boundary_report.report_section(session.boundary(), session)
    assert [item["file"] for item in section["external_models"]] == ["external/region.sql"]
    assert not has_backslash(section)
    body, failed, _ = ask(server, "impact", table="orders", column="region_name", change="drop_column")
    assert not failed and not has_backslash(body)
