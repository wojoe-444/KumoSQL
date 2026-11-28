"""Files whose path is longer than Windows' 260-character limit must load, trace and report."""

from pathlib import Path

from kumosql import resilience
from kumosql.live_graph import pipeline_from_files
from kumosql.pipeline import load_sqlx_project

DEEP = "/".join(["definitions"] + [f"folder_{i:02d}_" + "x" * 30 for i in range(8)])  # over 260 characters
assert len(DEEP) > 260


def test_extended_prefix_forms():
    assert resilience._extended_text("C:\\a\\b") == "\\\\?\\C:\\a\\b"
    assert resilience._extended_text("\\\\?\\C:\\a") == "\\\\?\\C:\\a"
    assert resilience._extended_text("\\\\srv\\share\\a") == "\\\\?\\UNC\\srv\\share\\a"


def test_extended_path_is_unchanged_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(resilience.os, "name", "posix")
    # Built while ``os.name`` says posix, as ``extended_path`` builds its result: pathlib picks the path class from it.
    assert resilience.extended_path(tmp_path) == Path(tmp_path)


def test_ordinary_text_undoes_the_extended_prefix():
    for text in ("C:\\a\\b", "\\\\srv\\share\\a", "relative\\x"):
        assert resilience.ordinary_path_text(resilience._extended_text(text)) == text
        assert resilience.ordinary_path_text(text) == text


def _files():
    return {
        f"{DEEP}/orders.sqlx": 'config { type: "table", schema: "marts" }\nselect id, amount from ${ref("raw_orders")}\n',
        "definitions/raw_orders.sqlx": 'config { type: "table", schema: "raw" }\nselect 1 as id, 2 as amount\n',
        "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: scratch\n",
    }


def test_project_with_a_very_long_path_loads_and_traces():
    pipeline = pipeline_from_files(_files())
    model = next(m for m in pipeline.models.values() if m.path and m.path.endswith("orders.sqlx") and len(m.path) > 260)
    assert "raw_orders" in " ".join(pipeline.models)
    assert pipeline.lineage_report()


def test_local_folder_with_a_very_long_path_loads(tmp_path):
    root = resilience.extended_path(tmp_path)  # writing the files needs the prefix on Windows; loading must not
    for name, text in _files().items():
        target = Path(root, *name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    pipeline = load_sqlx_project(tmp_path)
    assert any(len(m.path or "") > 260 for m in pipeline.models.values())
    assert not [d for d in pipeline.diagnostics if d.code in resilience.ASSET_FAILURE_CODES]


def test_local_folder_decodes_latin1_files_like_the_git_reader(tmp_path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: d\n")
    (tmp_path / "definitions" / "old.sqlx").write_bytes('config { type: "table" }\n-- caf\xe9\nselect 1 as id\n'.encode("latin-1"))
    (tmp_path / "definitions" / "bom.sqlx").write_bytes(b'\xef\xbb\xbfconfig { type: "table" }\nselect 2 as id\n')
    pipeline = load_sqlx_project(tmp_path)
    assert {"p.d.old", "p.d.bom"} <= set(pipeline.models)
    assert not [d for d in pipeline.diagnostics if d.code == "read_error"]
    assert resilience.decode_text(b"caf\xe9") == "caf\xe9"
    assert resilience.decode_text(b"\xef\xbb\xbfx") == "x"
