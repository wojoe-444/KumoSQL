"""Cost and access before a query runs: byte estimates, readable/denied per source, remembered denials, full-scan sizes.

No test reaches a network: BigQuery is the fake transport of ``test_dryrun`` behind ``mcp_preflight.bigquery_dry_run``,
and it records every request, so "from the cache" means "zero extra requests".
"""

import json

import pytest

from kumosql import dryrun, mcp_cost_access, mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from test_dryrun import FakeBigQuery, failure, ok
from test_mcp_server import SMALL, ask, write_project

PLAN = "SELECT id FROM p.ds.raw"
JOIN = "SELECT r.id FROM p.ds.raw r JOIN p.ds.totals t ON t.id = r.id"
STAR_RAW = "SELECT * FROM `p.ds.raw`"
STAR_TOTALS = "SELECT * FROM `p.ds.totals`"
ID = [{"name": "id", "type": "INTEGER"}]


def denied(table="p:ds.raw"):
    return failure(f"Access Denied: Table {table}: User does not have permission to query table {table}, or perhaps it does not exist in location US.", reason="accessDenied", location="")


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def pass_minutes(self, minutes):
        self.now += minutes * 60


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A server on its own session, a fake BigQuery that records requests, and a clock the cache obeys."""

    server = mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path / "project", SMALL))))
    from kumosql import mcp_preflight  # only after the server has loaded its tools: importing it earlier registers check_query out of order

    fake = FakeBigQuery({})
    clock = Clock()
    cache = mcp_cost_access.cache_for(server.session)
    cache.clock = clock
    cache.identity = lambda: "account-a"
    monkeypatch.setattr(mcp_preflight, "bigquery_dry_run", lambda sql, project: dryrun.dry_run(sql, project, token="t", transport=fake))

    class World:
        pass

    w = World()
    w.server, w.fake, w.clock, w.cache = server, fake, clock, cache
    w.check = lambda sql, **kw: ask(server, "check_query", sql=sql, dry_run=True, project="demo-project", **kw)
    return w


def sources_of(body):
    return {line["table"]: line for line in body.get("sources", [])}


def test_check_query_with_a_connection_returns_the_estimate_and_readable_per_source(world):
    world.fake.responses.update({JOIN: ok(ID, "4096"), STAR_RAW: ok(ID, "2048"), STAR_TOTALS: ok(ID, "1024")})
    body, failed, used = world.check(JOIN)
    assert not failed and used <= 2000
    assert body["dry_run"]["estimated_bytes"] == 4096 and "dry_run" in body["checked"]
    assert sources_of(body) == {
        "p.ds.raw": {"table": "p.ds.raw", "access": "readable", "scan_bytes": 2048},
        "p.ds.totals": {"table": "p.ds.totals", "access": "readable", "scan_bytes": 1024},
    }
    assert len(world.fake.requests) == 3  # the plan and one free SELECT * per source


def test_a_denial_is_remembered_and_the_next_answers_come_from_the_cache_until_the_ttl_passes(world):
    world.fake.responses.update({PLAN: denied()})
    body, failed, _ = world.check(PLAN)
    [finding] = [f for f in body["findings"] if f["check"] == "access"]
    assert not failed and "p.ds.raw is not readable with the current account" in finding["what"] and "cached" not in finding["what"]
    assert sources_of(body)["p.ds.raw"]["access"] == "denied"
    calls = len(world.fake.requests)
    assert calls == 1

    world.clock.pass_minutes(12)
    body, _, _ = world.check(PLAN)
    [finding] = [f for f in body["findings"] if f["check"] == "access"]
    assert "not readable with the current account (cached, 12 min ago; recheck with refresh=true)" in finding["what"]
    assert body["verdict"] == "problems_found" and "dry_run" not in body["checked"]
    # even without a dry run the answer knows, and a different query over the same table too
    body, _, _ = ask(world.server, "check_query", sql="SELECT amount FROM p.ds.raw WHERE amount > 1")
    assert "not readable with the current account (cached, 12 min ago" in body["findings"][0]["what"]
    body, _, _ = ask(world.server, "describe_table", table="raw")
    assert "not readable with the current account (cached, 12 min ago" in body["access"] and "refresh=true" in body["access"]
    assert len(world.fake.requests) == calls  # zero extra requests

    world.clock.pass_minutes(60)  # past the one-hour TTL: BigQuery is asked again
    world.fake.responses[PLAN] = ok(ID, "512")
    world.fake.responses[STAR_RAW] = ok(ID, "512")
    body, _, _ = world.check(PLAN)
    assert body["dry_run"]["estimated_bytes"] == 512 and not [f for f in body.get("findings", []) if f["check"] == "access"]
    assert "access" not in ask(world.server, "describe_table", table="raw")[0]


def test_refresh_asks_again_and_a_granted_access_clears_the_denial(world):
    world.fake.responses.update({PLAN: denied()})
    world.check(PLAN)
    world.fake.responses.update({PLAN: ok(ID, "700"), STAR_RAW: ok(ID, "700")})
    before = len(world.fake.requests)
    body, _, _ = world.check(PLAN, refresh=True)
    assert len(world.fake.requests) > before and body["dry_run"]["estimated_bytes"] == 700
    assert sources_of(body)["p.ds.raw"]["access"] == "readable"
    assert "access" not in ask(world.server, "describe_table", table="raw")[0]


def test_a_denial_belongs_to_one_account(world):
    world.fake.responses.update({PLAN: denied()})
    world.check(PLAN)
    world.cache.identity = lambda: "account-b"
    world.fake.responses.update({PLAN: ok(ID, "10"), STAR_RAW: ok(ID, "10")})
    body, _, _ = world.check(PLAN)
    assert body["dry_run"]["estimated_bytes"] == 10 and not [f for f in body.get("findings", []) if f["check"] == "access"]


def test_other_sources_are_probed_when_one_is_denied(world):
    world.fake.responses.update({JOIN: denied("p:ds.totals"), STAR_RAW: ok(ID, "300"), STAR_TOTALS: denied("p:ds.totals")})
    body, _, _ = world.check(JOIN)
    lines = sources_of(body)
    assert lines["p.ds.totals"]["access"] == "denied" and lines["p.ds.raw"] == {"table": "p.ds.raw", "access": "readable", "scan_bytes": 300}
    assert [f["what"].split(" ")[0] for f in body["findings"] if f["check"] == "access"] == ["p.ds.totals"]


def test_an_estimate_is_remembered_by_normalized_sql(world):
    world.fake.responses.update({PLAN: ok(ID, "900"), STAR_RAW: ok(ID, "900")})
    world.check(PLAN)
    calls = len(world.fake.requests)
    body, _, _ = world.check("select   id\nfrom p.ds.raw -- same query")
    assert len(world.fake.requests) == calls
    assert body["dry_run"] == {"estimated_bytes": 900, "byte_cap": body["dry_run"]["byte_cap"], "cached": "under 1 min ago"}
    world.clock.pass_minutes(61)
    world.check(PLAN)
    assert len(world.fake.requests) > calls


def test_describe_table_shows_the_full_scan_size_once_a_dry_run_has_made_it_known(world):
    body, _, _ = ask(world.server, "describe_table", table="totals")
    assert "full_scan" not in body
    assert {"what": "full-scan size", "reason": "unknown until a dry run has been made"} in body["unknown"]
    assert any("check_query" in step and "dry_run=true" in step and STAR_TOTALS in step for step in body["next"])

    world.fake.responses.update({STAR_TOTALS: ok(ID, "1572864")})
    answer, _, _ = world.check(STAR_TOTALS)
    assert answer["dry_run"]["estimated_bytes"] == 1572864 and len(world.fake.requests) == 1  # a plain SELECT * needs no second request
    body, _, used = ask(world.server, "describe_table", table="totals")
    assert used <= 2000 and body["full_scan"].startswith("1,572,864 bytes (1.5 MiB)") and "cached under 1 min ago" in body["full_scan"]
    assert not any(item["what"] == "full-scan size" for item in body["unknown"])


def test_without_a_connection_the_byte_estimate_is_listed_as_not_checked(world):
    body, failed, _ = ask(world.server, "check_query", sql=PLAN)
    assert not failed and world.fake.requests == []
    [gap] = [item for item in body["unknown"] if "estimated bytes" in item["what"]]
    assert gap["reason"].startswith("not checked: no connection") and "dry_run=true" in gap["reason"]
    assert "dry_run" not in body["checked"] and body["counts"]["not_checked"] >= 1


def test_over_the_cap_the_answer_states_cap_estimate_and_the_cheapest_remedy_without_suggesting_a_higher_cap(world, monkeypatch):
    from kumosql import scope_queries

    monkeypatch.setattr(scope_queries, "get_settings", lambda: scope_queries.Settings(max_bytes_billed=1_000_000_000))
    world.fake.responses.update({"SELECT * FROM p.ds.raw": ok(ID, "5000000000"), PLAN: ok(ID, "5000000000"), STAR_RAW: ok(ID, "5000000000")})
    body, _, _ = world.check("SELECT * FROM p.ds.raw")
    [over] = [f for f in body["findings"] if f["check"] == "dry_run"]
    assert "5,000,000,000 bytes" in over["what"] and "1,000,000,000-byte cap" in over["what"]
    assert over["fix"].startswith("Name the columns you need instead of *") and "partition" in over["fix"] and "do not raise it" in over["fix"]
    assert "Settings" not in over["fix"]
    body, _, _ = world.check(PLAN, refresh=True)
    [over] = [f for f in body["findings"] if f["check"] == "dry_run"]
    assert over["fix"].startswith("Drop the columns you do not use")


def test_refresh_needs_a_dry_run(world):
    body, failed, _ = ask(world.server, "check_query", sql=PLAN, refresh=True)
    assert failed and "dry_run=true" in body["error"] and body["next"]


def test_the_disk_cache_keeps_only_table_outcome_bytes_and_time_and_a_new_session_reads_it(tmp_path):
    clock = Clock()
    path = tmp_path / "cache" / "access.json"
    cache = mcp_cost_access.AccessCache(clock=clock, identity=lambda: "account-a", path=path)
    cache.record_denied("p.ds.secret_table")
    cache.record_estimate("demo-project", "SELECT 1 AS token_like_value", 42, ["id INT64"])
    text = path.read_text()
    assert "account-a" in text and "p.ds.secret_table" in text
    assert "SELECT" not in text and "INT64" not in text and "demo-project" not in text.replace("|demo-project|", "")
    for entry in json.loads(text).values():
        assert set(entry) <= {"outcome", "bytes", "at"}

    clock.pass_minutes(30)
    again = mcp_cost_access.AccessCache(clock=clock, identity=lambda: "account-a", path=path)
    assert again.denial("p.ds.secret_table") is not None and again.estimate("demo-project", "SELECT 1 AS token_like_value")["bytes"] == 42
    clock.pass_minutes(40)
    assert mcp_cost_access.AccessCache(clock=clock, identity=lambda: "account-a", path=path).denial("p.ds.secret_table") is None
    assert mcp_cost_access.AccessCache(0, clock=clock, identity=lambda: "account-a", path=path).denial("p.ds.secret_table") is None  # ttl 0: off


def test_the_ttl_and_the_file_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(mcp_cost_access.TTL_ENV, "90")
    monkeypatch.setenv(mcp_cost_access.FILE_ENV, str(tmp_path / "a.json"))
    cache = mcp_cost_access.AccessCache.from_environment()
    assert cache.ttl == 90 and cache.path == tmp_path / "a.json"
    monkeypatch.setenv(mcp_cost_access.TTL_ENV, "soon")
    assert mcp_cost_access.AccessCache.from_environment().ttl == mcp_cost_access.DEFAULT_TTL_SECONDS


@pytest.mark.parametrize("message, table", [
    ("Access Denied: Table my-proj:ds.tbl: User does not have permission to query table my-proj:ds.tbl", "my-proj.ds.tbl"),
    ("Access Denied: Table my-proj.ds.tbl was not readable", "my-proj.ds.tbl"),
    ("Access Denied: Project my-proj: User does not have bigquery.jobs.create permission in project my-proj.", None),
    ("Not found: Table my-proj:ds.tbl was not found in location EU", None),
])
def test_only_an_access_error_that_names_a_table_is_a_table_denial(message, table):
    assert mcp_cost_access.denied_table(dryrun.DryRunResult(False, error_message=message)) == table


def test_two_processes_sharing_the_file_keep_each_others_entries(tmp_path):
    clock = Clock()
    path = tmp_path / "shared.json"
    first = mcp_cost_access.AccessCache(clock=clock, identity=lambda: "account-a", path=path)
    second = mcp_cost_access.AccessCache(clock=clock, identity=lambda: "account-a", path=path)
    first.record_denied("p.ds.one")
    clock.pass_minutes(1)
    second.record_denied("p.ds.two")  # merges what the first wrote before it replaces the file
    assert second.denial("p.ds.one") is not None
    assert first.denial("p.ds.two") is not None  # and the first sees the second's entry without a restart
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shared.json"]  # no temporary file is left behind
    path.write_text("not json")
    first.record_denied("p.ds.three")  # a damaged file is replaced, not fatal
    assert json.loads(path.read_text())


def test_a_cache_file_inside_the_project_folder_is_refused_and_user_means_the_user_cache_folder(tmp_path, monkeypatch, capsys):
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setenv(mcp_cost_access.FILE_ENV, str(project / "definitions" / "access.json"))
    assert mcp_cost_access.AccessCache.from_environment(project).path is None
    assert "stays in memory" in capsys.readouterr().err
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv(mcp_cost_access.FILE_ENV, "user")
    assert mcp_cost_access.AccessCache.from_environment(project).path == tmp_path / "cache" / "kumosql" / "access-cache.json"
    monkeypatch.delenv(mcp_cost_access.FILE_ENV)
    assert mcp_cost_access.AccessCache.from_environment(project).path is None  # memory only unless asked


def test_a_dry_run_is_billed_to_the_given_project_and_never_to_a_source_project(world, monkeypatch):
    from kumosql import mcp_preflight

    projects = []
    real = mcp_preflight.bigquery_dry_run
    world.fake.responses.update({PLAN: ok(ID, "5"), STAR_RAW: ok(ID, "5")})
    monkeypatch.setattr(mcp_preflight, "bigquery_dry_run", lambda sql, project: (projects.append(project), real(sql, project))[1])
    world.check(PLAN)
    assert projects == ["demo-project"] * 2 and all("/projects/demo-project/jobs" in url for url, _, _ in world.fake.requests)
    assert all(request[2]["configuration"]["dryRun"] is True for request in world.fake.requests)  # nothing but dry runs
