"""dead_columns never lists the output columns of a Dataform assertion, which nothing is meant to read."""

from kumosql.pipeline_loading import load_sqlx_project


def project(tmp_path, files):
    (tmp_path / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    for name, text in files.items():
        path = tmp_path / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


def test_an_assertion_a_model_depends_on_is_not_reported(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS unused\n',
        "c.sqlx": 'config { type: "assertion" }\nSELECT id, unused FROM ${ref("a")} WHERE id < 0\n',
        "r.sqlx": 'config { type: "table", dependencies: ["c"] }\nSELECT 1 AS x\n',
        "r2.sqlx": 'config { type: "table" }\nSELECT x FROM ${ref("r")}\n',
    })
    assert pl.models["p.ds.c"].kind == "assertion" and pl.upstream["p.ds.r"] == {"p.ds.c"}
    assert pl.dead_columns() == {}  # the assertion's id and unused are failures it reports, not columns to read


def test_a_table_next_to_an_assertion_is_still_checked(tmp_path):
    pl = project(tmp_path, {
        "a.sqlx": 'config { type: "table" }\nSELECT 1 AS id, 2 AS unused\n',
        "c.sqlx": 'config { type: "assertion" }\nSELECT id FROM ${ref("a")} WHERE id < 0\n',
        "r.sqlx": 'config { type: "table", dependencies: ["c"] }\nSELECT id, 5 AS spare FROM ${ref("a")}\n',
        "r2.sqlx": 'config { type: "table" }\nSELECT id FROM ${ref("r")}\n',
    })
    assert pl.dead_columns() == {"p.ds.a": ("unused",), "p.ds.r": ("spare",)}
