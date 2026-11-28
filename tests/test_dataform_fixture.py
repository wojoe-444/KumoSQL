"""The synthetic Dataform repository used by the smoke test: deterministic, large-repository shaped, and loadable."""

import importlib.util
from pathlib import Path

from kumosql import resilience
from kumosql.pipeline import load_sqlx_project

SPEC = importlib.util.spec_from_file_location("make_dataform_fixture", Path(__file__).parent.parent / "tools" / "make_dataform_fixture.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_fixture_has_the_awkward_parts_and_loads(tmp_path):
    out = tmp_path / "fx"
    counts = fixture.generate(out, models=400, seed=3, config="both")
    assert (out / "workflow_settings.yaml").is_file() and (out / "dataform.json").is_file()
    assert sum(counts.get(k, 0) for k in ("sqlx-bom", "sqlx-latin1", "sqlx-config-only")) > 0
    listed = resilience.extended_path(out)  # without the prefix, Windows lists nothing past 260 characters
    assert max(len(p.relative_to(listed).as_posix()) for p in listed.rglob("*") if p.is_file()) > 260
    assert any(b"\r\n" in p.read_bytes() for p in listed.rglob("*.sqlx"))
    pipeline = load_sqlx_project(out)
    assert len(pipeline.models) > 200


def test_fixture_is_deterministic(tmp_path):
    fixture.generate(tmp_path / "a", models=120, seed=5)
    fixture.generate(tmp_path / "b", models=120, seed=5)

    def names(root):
        listed = resilience.extended_path(root)  # every file, the ones past 260 characters too
        return sorted(p.relative_to(listed).as_posix() for p in listed.rglob("*") if p.is_file())

    assert names(tmp_path / "a") == names(tmp_path / "b")


def test_byte_order_mark_and_js_comments_do_not_hide_config(tmp_path):
    (tmp_path / "definitions").mkdir()
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: scratch\ndefaultAssertionDataset: checks\n")
    (tmp_path / "definitions" / "a.sqlx").write_bytes(b'\xef\xbb\xbfconfig { type: "table", schema: "marts" }\nselect 1 as id\n')
    (tmp_path / "definitions" / "b.sqlx").write_text(
        'config { type: "table", schema: "marts" }\njs {\n  // don\'t lose the config: } {\n  const x = 1;\n}\nselect 2 as id\n')
    (tmp_path / "definitions" / "c.sqlx").write_text('config { type: "assertion" }\nselect 1 from ${ref("a")} where false\n')
    keys = set(load_sqlx_project(tmp_path).models)
    assert {"p.marts.a", "p.marts.b", "p.checks.c"} <= keys, keys


def test_fixture_declarations_resolve_from_files_or_stay_unresolved(tmp_path):
    out = tmp_path / "fx"
    fixture.generate(out, models=400, seed=3)
    keys = set(load_sqlx_project(out).sources)
    assert any(k.endswith("raw_loop.loop_declared_a") for k in keys)
    assert not any("computed_declared" in k for k in keys)  # computed in JavaScript: only the Dataform API can say
    via_api = load_sqlx_project(out, compiled_targets=lambda: [(("p", "raw_computed", "computed_declared_a"), True)])
    assert any(k.endswith("raw_computed.computed_declared_a") for k in via_api.sources)
