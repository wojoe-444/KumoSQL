"""Floors for the open-source BigQuery projects in ``tests/fixtures/bq_corpora`` (``tools/bq_corpus_bench.py``)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bq_corpus_bench as bench  # noqa: E402

from kumosql.pipeline import load_sqlx_project  # noqa: E402
from kumosql.scripts import analyse_script, token_reads  # noqa: E402
from kumosql.sqlx import SqlxRestorationError, mask_sqlx_interpolations, restore_sqlx_interpolations  # noqa: E402

pytestmark = pytest.mark.eval


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


# Refs to tables that JavaScript publishes (``publish("location", {schema: functions.baseSchema("ga4")})``):
# KumoSQL does not run the JavaScript, so these actions are not graph nodes. Kept as known failures.
KNOWN_MISSES = [
    "definitions/digital_analytics_domain/product_v1/device.sqlx: ref('device_type') is not an edge",
    "definitions/digital_analytics_domain/product_v1/location.sqlx: ref('location') is not an edge",
    "definitions/digital_analytics_domain/product_v1/normalized_device.sqlx: ref('normalized_device_type') is not an edge",
]
# bigquery-utils' audit views are 37-51 KB single statements; formatting and cleaning them up takes minutes (sqlfluff),
# so the test loads and traces them only. ``python tools/bq_corpus_bench.py`` runs every stage on them.
SLOW = {"bqutils-views"}


def test_real_projects_load_clean_and_keep_their_floors():
    results = bench.run_all(skip=SLOW)
    totals = bench.totals(results)
    assert [f for r in results.values() for f in r["failures"]] == KNOWN_MISSES
    assert totals["dependencies_found"] == totals["dependencies_expected"] - len(KNOWN_MISSES) >= 402
    assert totals["statements_matched"] >= 282
    assert totals["columns_traced"] >= 2285
    assert totals["files"] >= 380
    assert totals["held_out"]["files"] >= 55


def test_every_copy_matches_its_pinned_sha256():
    """The pinned commit and the bytes under test agree: no file of a project was edited, added or removed."""

    import fetch_bq_corpora

    sources = json.loads((bench.CORPORA / "sources.json").read_text())["projects"]
    assert all(len(project["sha256"]) == 64 for project in sources)
    assert [p["name"] for p in sources if fetch_bq_corpora.digest(bench.CORPORA / p["name"]) != p["sha256"]] == []


def test_the_held_out_fifth_is_the_files_whose_sha1_is_0_mod_5():
    # Recorded: marketing-jumpstart's normalized_device.sqlx is in it (SHA-1 of project/path = 0 mod 5). Moving the rule
    # would move which files the eval calls held out, so the recorded split would no longer be the one scored.
    assert bench.held_out("marketing-jumpstart", "definitions/digital_analytics_domain/product_v1/normalized_device.sqlx")
    assert not bench.held_out("marketing-jumpstart", "definitions/digital_analytics_domain/product_v1/device.sqlx")


def test_slow_projects_load_and_trace():
    result = bench.run_project(bench.CORPORA / "bqutils-views", stages=False)
    assert result["failures"] == []
    assert (result["statements_matched"], result["columns_traced"]) == (5, 196)


def test_a_long_header_comment_does_not_hide_the_statement():
    header = "/*\n" + "Licence text. " * 60 + "\n*/\n-- " + "x" * 700 + "\n"
    statement = analyse_script(header + "CREATE VIEW d.v AS SELECT a FROM d.t").statements[0]
    assert (statement.kind, statement.disposition) == ("create_view", "kept")


def test_statements_inside_called_procedures_are_counted_apart():
    pipeline = load_sqlx_project(bench.CORPORA / "snowplow-web")
    messages = [d.message for d in pipeline.all_diagnostics() if d.code == "unparsed_operation" and d.model.endswith("03_commit_custom")]
    assert messages and messages[0].startswith("7 statements inside procedures they call could not be read")


def _project(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return load_sqlx_project(tmp_path)


def test_a_when_that_ends_with_its_connective_still_parses(tmp_path):
    # security-analytics: ``WHERE ${when(incremental(), `ts >= checkpoint AND`)} ts >= ...`` left 17 models unparsed.
    sql = "SELECT a FROM t WHERE\n  ${ when(incremental(), `ts >= cp AND`) }\n  ts >= x AND b = 1"
    masked, restorations = mask_sqlx_interpolations(sql)
    assert "__sqlx_token_000__ AND\n  ts >= x" in masked
    assert restore_sqlx_interpolations(masked, restorations) == sql
    with pytest.raises(SqlxRestorationError):  # a rewrite that parted the expression from its AND cannot be restored
        restore_sqlx_interpolations(masked.replace("__sqlx_token_000__ AND", "b = 2 AND __sqlx_token_000__"), restorations)
    pipeline = _project(tmp_path, {
        "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: d\n",
        "definitions/m.sqlx": 'config { type: "incremental" }\n' + sql.replace("FROM t", 'FROM ${ref("t")}'),
        "definitions/t.sqlx": 'config { type: "declaration" }',
    })
    assert not [d for d in pipeline.all_diagnostics() if d.code == "parse_error"]
    assert pipeline.upstream["p.d.m"] == {"p.d.t"}


def test_from_inside_extract_is_not_a_table():
    # An unparsed statement's tables come from its tokens; ``EXTRACT(DATE FROM timestamp)`` read a table "timestamp".
    reads, _ = token_reads("SELECT EXTRACT(DATE FROM timestamp), TRIM(BOTH 'x' FROM s) FROM a.b WHERE x IS DISTINCT FROM y ((")
    assert [".".join(part.name for part in table.parts) for table in reads] == ["a.b"]


def test_computed_datasets_keep_actions_apart_and_refs_find_them(tmp_path):
    # marketing-analytics-jumpstart: actions named "event" in datasets computed by ``functions.baseSchema("ga4")`` and
    # ``functions.productSchema("ga4")`` replaced each other, and refs naming such a dataset were not read at all.
    pipeline = _project(tmp_path, {
        "workflow_settings.yaml": 'defaultProject: p\ndefaultDataset: d\nvars:\n  RAW: "raw_data"\n',
        "includes/functions.js": "function baseSchema(d) { return 'b_' + d; }\nmodule.exports = { baseSchema };\n",
        "definitions/base_event.sqlx": 'config { type: "table", name: "event", schema: functions.baseSchema("ga4") }\nSELECT id FROM ${ref("src")}',
        "definitions/event.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref({schema: functions.baseSchema( "ga4" ), name: "event"})}',
        "definitions/pos.sqlx": "config { type: \"view\" }\nSELECT id FROM ${ref(functions.baseSchema('ga4'), \"event\")}",
        "definitions/src.sqlx": 'config { type: "declaration", schema: dataform.projectConfig.vars.RAW }',
        "definitions/other.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref({schema: functions.baseSchema("ads"), name: "event"})}',
    })
    base = 'p.{functions:baseSchema("ga4")}.event'
    assert not [d for d in pipeline.all_diagnostics() if d.code == "duplicate_model"]
    assert pipeline.upstream[base] == {"p.raw_data.src"}
    assert pipeline.upstream["p.d.event"] == pipeline.upstream["p.d.pos"] == {base}
    # Another computed dataset may or may not be one of those: left unresolved rather than guessed.
    assert pipeline.upstream["p.d.other"] == set()
    assert "unsupported_ref" in {d.code for d in pipeline.all_diagnostics() if d.model == "p.d.other"}


def test_a_js_publish_with_a_computed_dataset_is_listed_so_its_refs_resolve(tmp_path):
    # marketing-jumpstart publishes ``location`` from a .js file into ``functions.baseSchema("ga4")``: the file's names
    # were all unknowable, so every ref to an unlisted name stayed unresolved too.
    pipeline = _project(tmp_path, {
        "workflow_settings.yaml": "defaultProject: p\ndefaultDataset: d\n",
        "includes/functions.js": "function baseSchema(d) { return 'b_' + d; }\nmodule.exports = { baseSchema };\n",
        "definitions/base.js": 'publish("location", {type: "table", schema: functions.baseSchema("ga4")}).query(ctx => `SELECT ${compute()} AS id`);',
        "definitions/reader.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref({schema: functions.baseSchema("ga4"), name: "location"})}',
        "definitions/other.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref({schema: functions.baseSchema("ads"), name: "location"})}',
        "definitions/plain.sqlx": 'config { type: "view" }\nSELECT id FROM ${ref("other_table")}',
    })
    codes = {(d.model, d.code) for d in pipeline.all_diagnostics()}
    assert not [c for c in codes if c[0] == "p.d.reader" and c[1] in {"unsupported_ref", "missing_ref"}]
    assert 'p.{functions:baseSchema("ga4")}.location' in pipeline.models
    assert pipeline.upstream["p.d.reader"] == {'p.{functions:baseSchema("ga4")}.location'}
    # A different computed dataset may or may not be that one: unresolved, not guessed.
    assert ("p.d.other", "unsupported_ref") in codes
    assert not pipeline.upstream["p.d.other"]
