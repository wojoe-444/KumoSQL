"""Scheduled-job SQL loaded beside a Dataform project (kumosql.job_sql): one pipeline, lineage through the jobs."""

import json
from pathlib import Path

import pytest

from kumosql import ColumnRef, load_sqlx_project
from kumosql.job_sql import JobScript, JobSqlError, ManifestStep, load_job_sql, read_manifest, script_steps, with_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "job_lineage"
DATAFORM = FIXTURE / "dataform"
JOBS = FIXTURE / "jobs"
HISTORY = FIXTURE / "history.jsonl"


@pytest.fixture(scope="module")
def project():
    return load_sqlx_project(DATAFORM)


@pytest.fixture(scope="module")
def joined(project):
    return with_jobs(project, [load_job_sql(JOBS)])


@pytest.fixture(scope="module")
def joined_with_history(project):
    return with_jobs(project, [load_job_sql(JOBS), load_job_sql(HISTORY)])


def gaps(pipeline):
    return {(gap["asset"], gap["code"]): gap for gap in pipeline.completeness()["gaps"]}


def hop_chain(trace):
    return [(hop.model, hop.column.column, hop.job_id, hop.step) for hop in trace.hops]


# ------------------------------------------------------------------ the whole chain


def test_trace_runs_from_a_dataform_model_through_every_job_step(joined):
    trace = joined.trace_column(ColumnRef("shop-analytics.marts.daily_revenue", "daily_revenue"))

    assert hop_chain(trace) == [
        ("shop-analytics.marts.daily_revenue", "daily_revenue", None, None),
        ("shop-legacy.sandbox.orders_daily", "revenue", "daily_orders", 2),
        ("shop-legacy.sandbox.orders_clean", "amount", "daily_orders", 1),
    ]
    assert trace.complete
    # through the temporary table ``paid`` to the real source
    assert {str(ref) for ref in trace.sources} == {"shop-legacy.raw.orders.amount"}
    assert trace.hops[1].transform == "aggregate"
    assert trace.hops[1].aggregation_keys == ("order_day",)


def test_hop_json_names_the_job_and_step(joined):
    trace = joined.trace_column(ColumnRef("shop-analytics.marts.daily_revenue", "daily_revenue"))
    rows = trace.to_json()["hops"]

    assert "job_id" not in rows[0] and "step" not in rows[0]
    assert (rows[1]["job_id"], rows[1]["step"]) == ("daily_orders", 2)
    assert (rows[2]["job_id"], rows[2]["step"]) == ("daily_orders", 1)


def test_job_models_carry_their_job_and_step(joined):
    model = joined.models["shop-legacy.sandbox.orders_daily"]

    assert (model.job_id, model.job_step) == ("daily_orders", 2)
    assert model.path == "daily_orders.sql"
    # the declaration is still a declaration of the project; the job step is what builds it
    assert "shop-legacy.sandbox.orders_daily" in joined.sources
    report = joined.report(include_duplicates=False)
    assert report["node_identities"]["shop-legacy.sandbox.orders_daily"]["job_id"] == "daily_orders"


def test_job_models_survive_a_saved_snapshot(joined):
    from kumosql.storage import pipeline_from_snapshot, pipeline_snapshot

    again = pipeline_from_snapshot(json.loads(json.dumps(pipeline_snapshot(joined, "k"))), "k")
    model = again.models["shop-legacy.sandbox.orders_daily"]
    assert (model.job_id, model.job_step) == ("daily_orders", 2)
    broken = pipeline_snapshot(joined, "k")
    broken["models"]["shop-legacy.sandbox.orders_daily"]["job_step"] = "2"
    with pytest.raises(ValueError, match="job"):
        pipeline_from_snapshot(json.loads(json.dumps(broken)), "k")


def test_a_step_keeps_only_the_temporary_tables_it_reads(joined):
    step_one = joined.models["shop-legacy.sandbox.orders_clean"].sql
    step_two = joined.models["shop-legacy.sandbox.orders_daily"].sql

    assert "paid" in step_one and "DECLARE" in step_one
    assert "paid" not in step_two  # step 2 reads the table step 1 wrote, not the temporary table
    # blanking keeps the script's lines, so a location in a message is a line of the script
    original = (JOBS / "daily_orders.sql").read_text(encoding="utf-8")
    assert step_two.count("\n") == original.count("\n")


def test_a_bare_name_in_a_job_means_the_jobs_dataset_not_a_new_model(joined):
    # The Dataform project has its own model ``orders_clean`` (in marts); the old job's bare ``orders_clean`` is the sandbox table.
    trace = joined.trace_column(ColumnRef("shop-legacy.sandbox.orders_daily", "revenue"))

    assert [hop.model for hop in trace.hops] == ["shop-legacy.sandbox.orders_daily", "shop-legacy.sandbox.orders_clean"]
    assert "shop-analytics.marts.orders_clean" not in {ref.table for ref in trace.upstream}


# ------------------------------------------------------------------ gaps


def test_a_declaration_with_two_loaded_writers_is_a_gap_naming_them(joined):
    gap = gaps(joined)[("shop-legacy.sandbox.customer_totals", "job_writer_ambiguous")]

    assert gap["blocking"] and "lineage" in gap["effect"]
    assert "job customer_totals step 1" in gap["message"] and "job customer_totals_backfill step 1" in gap["message"]
    assert "shop-legacy.sandbox.customer_totals" not in joined.models
    # lineage stops at the declared table: nothing was guessed
    trace = joined.trace_column(ColumnRef("shop-analytics.marts.top_customers", "lifetime_amount"))
    assert {str(ref) for ref in trace.sources} == {"shop-legacy.sandbox.customer_totals.lifetime_amount"}
    assert not joined.completeness()["complete"]


def test_a_declaration_in_the_sandbox_with_no_loaded_writer_is_a_gap(joined):
    found = gaps(joined)

    assert ("shop-legacy.sandbox.returns_daily", "job_writer_missing") in found
    assert ("shop-legacy.sandbox.returns_weekly", "job_writer_missing") in found
    assert "no loaded job writes it" in found[("shop-legacy.sandbox.returns_daily", "job_writer_missing")]["message"]
    # a raw source outside the datasets the jobs write is a source, not a gap
    assert not [key for key in found if key[0] == "shop-legacy.raw.orders"]


def test_a_job_writing_a_table_the_project_builds_is_a_gap_and_the_model_stays(project, tmp_path):
    (tmp_path / "rebuild.sql").write_text("CREATE OR REPLACE TABLE marts.daily_revenue AS SELECT 1 AS order_day, 2 AS daily_revenue", encoding="utf-8")
    (tmp_path / "jobs.yaml").write_text("defaults: {project: shop-analytics}\njobs:\n  - {job_id: rebuild, script: rebuild.sql}\n", encoding="utf-8")
    joined = with_jobs(project, [load_job_sql(tmp_path)])

    gap = gaps(joined)[("shop-analytics.marts.daily_revenue", "job_writer_ambiguous")]
    assert "project's own model" in gap["message"] and "job rebuild step 1" in gap["message"]
    assert joined.models["shop-analytics.marts.daily_revenue"].job_id is None


# ------------------------------------------------------------------ job history


def test_repeated_runs_of_a_scheduled_query_are_one_writer():
    history = load_job_sql(HISTORY)
    by_destination = {script.destination: script for script in history.scripts}

    weekly = by_destination["shop-legacy.sandbox.returns_weekly"]
    assert weekly.runs == 3
    assert weekly.job_id == "scheduled_query_abc_3"  # the latest run's id names the writer
    assert history.summary["dry_runs"] == 1
    assert history.summary["runs_folded"] == 3  # two weekly runs and one script run folded away
    assert len(history.scripts) == 2


def test_child_jobs_of_a_script_are_grouped_under_the_script(joined_with_history):
    trace = joined_with_history.trace_column(ColumnRef("shop-analytics.marts.returns_summary", "returns"))

    assert hop_chain(trace)[1] == ("shop-legacy.sandbox.returns_daily", "returns", "script_job_2", 1)
    assert {str(ref) for ref in trace.sources} == {"shop-legacy.raw.returns.order_id"}  # through the temporary table r
    # the history wrote the two tables that were gaps with the job folder alone
    found = gaps(joined_with_history)
    assert ("shop-legacy.sandbox.returns_daily", "job_writer_missing") not in found
    assert ("shop-legacy.sandbox.returns_weekly", "job_writer_missing") not in found


def test_a_scheduled_select_writes_its_destination_table(joined_with_history):
    trace = joined_with_history.trace_column(ColumnRef("shop-analytics.marts.returns_summary", "week_returns"))

    assert hop_chain(trace)[1] == ("shop-legacy.sandbox.returns_weekly", "returns", "scheduled_query_abc_3", 1)


def test_children_without_their_script_row_still_make_one_script(tmp_path):
    rows = [
        {"job_id": "s_1", "parent_job_id": "s", "creation_time": "2026-11-01T00:00:02Z", "project_id": "p",
         "query": "CREATE OR REPLACE TABLE d.out AS SELECT a FROM t"},
        {"job_id": "s_0", "parent_job_id": "s", "creation_time": "2026-11-01T00:00:01Z", "project_id": "p",
         "query": "CREATE TEMP TABLE t AS SELECT a FROM d.src"},
        {"job_id": "bad", "creation_time": "2026-11-01T00:00:03Z", "project_id": "p", "query": "SELECT 1",
         "destination_table": "p.d.other", "error_result": {"reason": "invalidQuery"}},
    ]
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    history = load_job_sql(path)

    assert [script.job_id for script in history.scripts] == ["s"]
    assert history.summary["failed"] == 1
    steps, problems = script_steps(history.scripts[0])
    assert not problems
    assert [(step.step, step.destination) for step in steps] == [(1, ("p", "d", "out"))]
    assert "CREATE TEMP" in steps[0].sql.upper().replace("TEMPORARY", "TEMP")  # the temporary table ran first


def test_json_lines_history_named_json_is_read_as_history(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(HISTORY.read_text(encoding="utf-8"), encoding="utf-8")

    assert len(load_job_sql(path).scripts) == 2


# ------------------------------------------------------------------ the manifest


def write_job(tmp_path, manifest: str, **scripts: str) -> Path:
    for name, text in scripts.items():
        (tmp_path / f"{name}.sql").write_text(text, encoding="utf-8")
    (tmp_path / "jobs.yaml").write_text(manifest, encoding="utf-8")
    return tmp_path


def test_steps_are_read_from_the_script_when_left_out(tmp_path):
    folder = write_job(
        tmp_path,
        "defaults: {project: p, dataset: sb}\njobs:\n  - {job_id: j, script: j.sql}\n",
        j="DECLARE x INT64 DEFAULT 1;\nCREATE OR REPLACE TABLE a AS SELECT 1 AS c;\nDELETE FROM b WHERE TRUE;\nINSERT INTO b (c) SELECT c FROM a;",
    )
    steps, problems = script_steps(load_job_sql(folder).scripts[0])

    assert not problems
    # DELETE writes no values, so it is not a step; the INSERT is
    assert [(step.step, step.statement, step.destination) for step in steps] == [
        (1, 2, ("p", "sb", "a")),
        (2, 4, ("p", "sb", "b")),
    ]


def test_a_statement_index_picks_the_step(tmp_path):
    folder = write_job(
        tmp_path,
        "jobs:\n  - job_id: j\n    script: j.sql\n    steps:\n      - {step: 7, destination: d.b, statement: 2}\n",
        j="CREATE TABLE d.b AS SELECT 1 AS c;\nINSERT INTO d.b (c) SELECT 2;",
    )
    steps, problems = script_steps(load_job_sql(folder).scripts[0])

    assert not problems
    assert [(step.step, step.statement) for step in steps] == [(7, 2)]


def test_a_scheduled_query_folder_job_names_its_destination(project, tmp_path):
    folder = write_job(
        tmp_path,
        "jobs:\n  - {job_id: weekly, script: weekly.sql, destination: shop-legacy.sandbox.returns_weekly}\n",
        weekly="SELECT DATE '2026-01-05' AS week, 3 AS returns",
    )
    joined = with_jobs(project, [load_job_sql(folder)])

    assert joined.models["shop-legacy.sandbox.returns_weekly"].job_id == "weekly"


@pytest.mark.parametrize(
    "manifest, scripts, message",
    [
        ("jobs:\n  - {job_id: j, script: j.sql, step: 1}\n", {"j": "SELECT 1"}, "unknown key 'step'; did you mean steps?"),
        ("jobs:\n  - {job_id: j, script: missing.sql}\n", {}, "script missing.sql is not a file"),
        ("jobs:\n  - {job_id: j, script: j.sql}\n  - {job_id: J, script: j.sql}\n", {"j": "CREATE TABLE d.t AS SELECT 1 AS c"}, "listed twice"),
        ("jobs:\n  - {job_id: j, script: j.sql}\n", {"j": "SELECT 1 AS c"}, "give `destination`"),
        ("jobs:\n  - job_id: j\n    script: j.sql\n    steps: [{step: 1, destination: d.t, statement: 9}]\n", {"j": "CREATE TABLE d.t AS SELECT 1 AS c"}, "statement 9 does not exist"),
        ("jobs:\n  - job_id: j\n    script: j.sql\n    steps: [{step: 1, destination: d.other, statement: 1}]\n", {"j": "CREATE TABLE d.t AS SELECT 1 AS c"}, "writes d.t, not d.other"),
        ("jobs:\n  - job_id: j\n    script: j.sql\n    steps: [{step: 1, destination: d.t}]\n", {"j": "CREATE TABLE d.t AS SELECT 1 AS c;\nINSERT INTO d.t (c) SELECT 2"}, "2 statements write d.t"),
        ("jobs:\n  - job_id: j\n    script: j.sql\n    steps: [{step: 0, destination: d.t}]\n", {"j": "CREATE TABLE d.t AS SELECT 1 AS c"}, "whole number from 1"),
        ("jobs: []\n", {}, "non-empty list"),
        ("jobs:\n  - {job_id: j, script: /abs/j.sql}\n", {}, "relative to the manifest"),
    ],
)
def test_a_manifest_problem_is_named(tmp_path, manifest, scripts, message):
    folder = write_job(tmp_path, manifest, **scripts)

    with pytest.raises(JobSqlError) as raised:
        read_manifest(folder / "jobs.yaml")
    assert message in str(raised.value)
    assert str(raised.value).startswith("job manifest jobs.yaml:")


def test_a_folder_without_a_manifest_is_named(tmp_path):
    with pytest.raises(JobSqlError, match="no manifest"):
        load_job_sql(tmp_path)


def test_same_table_two_statements_of_one_job_is_a_gap(project, tmp_path):
    folder = write_job(
        tmp_path,
        "defaults: {project: shop-legacy, dataset: sandbox}\njobs:\n  - {job_id: j, script: j.sql}\n",
        j="CREATE OR REPLACE TABLE orders_daily AS SELECT DATE '2026-01-01' AS order_day, 1 AS revenue;\n"
          "UPDATE orders_daily SET revenue = revenue * 2 WHERE TRUE;",
    )
    joined = with_jobs(project, [load_job_sql(folder)])

    gap = gaps(joined)[("shop-legacy.sandbox.orders_daily", "job_writer_ambiguous")]
    assert "job j step 1" in gap["message"] and "job j step 2" in gap["message"]


def test_a_destination_matching_several_project_tables_is_a_gap(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"declarations": [
        {"target": {"database": "a", "schema": "sb", "name": "t"}},
        {"target": {"database": "b", "schema": "sb", "name": "t"}},
    ]}), encoding="utf-8")
    from kumosql import load_compiled_graph

    folder = write_job(tmp_path, "jobs:\n  - {job_id: j, script: j.sql}\n", j="CREATE TABLE sb.t AS SELECT 1 AS c")
    joined = with_jobs(load_compiled_graph(graph), [load_job_sql(folder)])

    assert ("sb.t", "job_destination_ambiguous") in gaps(joined)


def test_partial_names_on_either_side_join(tmp_path):
    from kumosql import load_compiled_graph

    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({
        "declarations": [{"target": {"schema": "sandbox", "name": "t"}}],  # the project names no project for it
        "tables": [{"target": {"database": "p", "schema": "m", "name": "r"}, "query": "SELECT c FROM sandbox.t"}],
    }), encoding="utf-8")
    folder = write_job(tmp_path, "defaults: {project: legacy}\njobs:\n  - {job_id: j, script: j.sql}\n",
                       j="CREATE TABLE sandbox.t AS SELECT x AS c FROM raw.src")
    joined = with_jobs(load_compiled_graph(graph), [load_job_sql(folder)])

    assert joined.models["sandbox.t"].job_id == "j"
    trace = joined.trace_column(ColumnRef("p.m.r", "c"))
    assert {str(ref) for ref in trace.sources} == {"legacy.raw.src.x"}


def test_blocks_keep_what_their_conditions_read_and_empty_blocks_go():
    text = (
        "BEGIN\n"
        "  CREATE TEMP TABLE t AS SELECT id, v FROM raw.src;\n"
        "  IF (SELECT COUNT(*) FROM t) > 0 THEN\n"
        "    MERGE sandbox.target AS x USING t ON x.id = t.id\n"
        "      WHEN MATCHED THEN UPDATE SET v = t.v WHEN NOT MATCHED THEN INSERT (id, v) VALUES (t.id, t.v);\n"
        "  END IF;\n"
        "  CREATE OR REPLACE TABLE out AS SELECT id FROM sandbox.target;\n"
        "EXCEPTION WHEN ERROR THEN\n"
        "  SELECT @@error.message;\n"
        "END;"
    )
    steps, problems = script_steps(JobScript("j", text, project="p", dataset="sandbox"))

    assert not problems
    merge, out = steps
    assert merge.destination == ("p", "sandbox", "target")
    assert "CREATE TEMPORARY TABLE t" in merge.sql and "p.raw.src" in merge.sql  # the IF around it reads t
    assert "IF" not in out.sql and " t " not in out.sql  # nothing of the IF block is left for the second step
    assert out.sql.count("\n") == text.count("\n")


def test_script_steps_of_a_job_script_object():
    script = JobScript("j", "CREATE TABLE d.a AS SELECT 1 AS c;\nCREATE TABLE d.b AS SELECT c FROM d.a;", steps=(ManifestStep(3, "d.b"),))
    steps, problems = script_steps(script)

    assert not problems
    assert [(step.step, step.statement, step.destination) for step in steps] == [(3, 2, ("d", "b"))]


# ------------------------------------------------------------------ surfaces


def test_pipeline_report_loads_job_sql(tmp_path, capsys):
    from kumosql.cli import pipeline_main

    out = tmp_path / "impact.json"
    code = pipeline_main([
        str(DATAFORM), "--job-sql", str(JOBS), "--job-sql", str(HISTORY),
        "--assess", "drop_table", "--target", "shop-legacy.sandbox.orders_clean", "-o", str(out),
    ])

    assert code == 0
    impact = json.loads(out.read_text(encoding="utf-8"))
    # dropping an old sandbox table breaks the job step that reads it, and the Dataform model after that
    assert [(row["model"], row["depth"]) for row in impact["affected"]] == [
        ("shop-legacy.sandbox.orders_daily", 1),
        ("shop-analytics.marts.daily_revenue", 2),
    ]


def test_pipeline_report_names_a_bad_job_folder(tmp_path, capsys):
    from kumosql.cli import pipeline_main

    with pytest.raises(SystemExit) as raised:
        pipeline_main([str(DATAFORM), "--job-sql", str(tmp_path)])
    assert raised.value.code == 2
    assert "--job-sql: job folder" in capsys.readouterr().err


def test_agent_server_loader_takes_job_sql():
    from kumosql.mcp_session import load_project

    pipeline = load_project(DATAFORM, job_sql=[JOBS])

    assert pipeline.models["shop-legacy.sandbox.orders_daily"].job_step == 2
