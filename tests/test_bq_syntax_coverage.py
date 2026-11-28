"""Every case in tests/fixtures/bq_syntax runs through each KumoSQL stage.

A stage may be ``pass`` or ``n/a``. ``unsupported`` is allowed only for gaps listed in
``known_gaps.json`` (an xfail, with the reason and who owns it), so a new gap fails the
suite until it is fixed or recorded. See docs/evals/bigquery-syntax-coverage.md.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "bq_syntax_coverage", Path(__file__).parent.parent / "tools" / "bq_syntax_coverage.py"
)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)

CASES = coverage.load_manifest()
GAPS = coverage.load_known_gaps()
CASE_STAGES = [(case["id"], stage) for case in CASES for stage in coverage.stages_for(case)]


BY_ID = {case["id"]: case for case in CASES}
_RESULTS: dict[str, dict] = {}


@pytest.fixture
def results():
    """Stage results by case id, each case run on first use.

    Running every case up front made each parallel worker repeat the whole ~1 minute run; a case now
    runs once per worker, and ``xdist_group`` on ``test_stage`` keeps a case's stages on one worker.
    """

    class Lazy(dict):
        def __missing__(self, case_id):
            if case_id not in _RESULTS:
                mp = pytest.MonkeyPatch()
                mp.setenv("KUMOSQL_TIMING", "0")
                try:
                    _RESULTS[case_id] = coverage.run_case(BY_ID[case_id])
                finally:
                    mp.undo()
            return _RESULTS[case_id]

    return Lazy()


def test_manifest_lists_every_fixture_file_once():
    listed = [case["file"] for case in CASES]
    assert len(listed) == len(set(listed)) and len({c["id"] for c in CASES}) == len(CASES)
    on_disk = {
        path.relative_to(coverage.FIXTURES).as_posix()
        for pattern in ("sql/**/*.sql", "dataform/sqlx/*.sqlx")
        for path in coverage.FIXTURES.glob(pattern)
    }
    assert on_disk == set(listed)
    assert all(case["tags"] for case in CASES)


def test_manifest_covers_each_statement_family():
    tags = {tag for case in CASES for tag in case["tags"]}
    wanted = {
        "qualify", "recursive", "pivot", "unpivot", "tablesample", "unnest", "rollup", "cube", "grouping-sets",
        "corresponding", "pipe", "merge", "window", "frame", "ddl", "dml", "script", "exception", "dynamic-sql",
        "transaction", "dcl", "export", "load", "search", "vector", "json", "geography", "ml", "udf", "javascript",
        "tvf", "procedure", "wildcard", "time-travel", "information-schema", "quoting",
        "table", "view", "incremental", "operations", "assertion", "declaration", "test", "pre-post", "includes",
    }
    assert wanted <= tags, sorted(wanted - tags)


def test_dry_run_results_cover_the_manifest_when_recorded():
    recorded = coverage.load_dry_runs()
    if not recorded:
        pytest.skip("no dry-run results recorded")
    assert set(recorded) <= {case["id"] for case in CASES}
    assert all(value["status"] in {"ok", "error", "not_run"} for value in recorded.values())
    # BigQuery may stop on a missing object, session or permission; it must never reject the SQL itself.
    rejected = [case_id for case_id, value in recorded.items() if value["status"] == "error" and "category" not in value]
    assert not rejected, rejected


@pytest.mark.parametrize(
    "case_id,stage",
    [pytest.param(c, s, id=f"{c}::{s}", marks=pytest.mark.xdist_group(c)) for c, s in CASE_STAGES],
)
def test_stage(results, case_id, stage):
    status, detail = results[case_id][stage]
    gap = GAPS.get(f"{case_id}::{stage}")
    if status in (coverage.PASS, coverage.NA):
        return
    if gap is not None:
        pytest.xfail(f"{gap['owner']}: {gap['reason']}")
    pytest.fail(f"{status}: {detail} (fix it, or record it in known_gaps.json with --update-known-gaps)")


def test_known_gaps_name_real_cases():
    ids = {case["id"]: coverage.stages_for(case) for case in CASES}
    for key, gap in GAPS.items():
        case_id, stage = key.split("::")
        assert case_id in ids and stage in ids[case_id], key
        assert gap["owner"] in {"sqlglot", "sqlfluff", "kumosql", "prover"} and gap["reason"], key


def test_gaps_file_is_json_sorted():
    text = coverage.known_gaps_path().read_text()
    assert list(json.loads(text)) == sorted(json.loads(text))
