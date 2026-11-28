"""``compare_queries`` asks the pinned prover for what the command line already gets: the conditional verdict with its check
queries (``--conditional``), the "differs only when P" predicate (``--explain-difference``), the verifier SQL
(``--verifier-sql``), and which declared facts a proof rests on (``constraint_dependence.needed_guarantees``).

The prover runs for real on small pairs. The difference predicate is replaced by a fake where a predicate has to be found:
the pinned prover's ``explain_difference`` is still the contract that returns no predicate (issue #512 there).
"""

import json
import time

import pytest

from kumosql import mcp_answers, mcp_server
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project
from kumosql.prover_schema import from_pipeline
from kumosql.schema_snapshot import SchemaSnapshot, SnapshotColumn

# The registry lists tools in the order their modules are first imported. Importing kumosql.mcp_compare on its own first
# would put compare_queries ahead of find_tables for every test on this worker, so the server's order is set first.
mcp_server.load_tools()

from kumosql import mcp_compare  # noqa: E402

pytest.importorskip("z3")

BOUND = mcp_answers.DEFAULT_BYTES

FILES = {
    # a model whose own config asserts its key: Dataform checks it with a query when the pipeline runs
    "users.sqlx": 'config { type: "table", assertions: { uniqueKey: ["id"], nonNull: ["id"] } }\nSELECT 1 AS id, "a" AS name\n',
    # a table the project does not build, with a key somebody wrote down
    "orders.sqlx": 'config { type: "declaration", schema: "raw", name: "orders", assertions: { uniqueKey: ["id"] } }\n',
    "refunds.sqlx": 'config { type: "declaration", schema: "raw", name: "refunds" }\n',
    # no declared fact at all
    "plain.sqlx": 'config { type: "table" }\nSELECT 1 AS id, "a" AS name, 2 AS x\n',
}


def write_project(folder, files=FILES):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    for name, text in files.items():
        path = folder / "definitions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return folder


def snapshot_column(table: str, column: str, required: bool) -> SnapshotColumn:
    catalog, schema, name = table.split(".")
    return SnapshotColumn(catalog, schema, name, column, "INT64", is_nullable=not required)


def server(tmp_path, **session_options) -> mcp_server.AgentServer:
    return mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path / "proj")), **session_options))


def ask(agent, **arguments):
    """The parsed answer, whether it is an error, and its size in bytes."""

    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "compare_queries", "arguments": arguments}}
    result = agent.handle(message)["result"]
    text = result["content"][0]["text"]
    return json.loads(text), result["isError"], len(text.encode("utf-8"))


DISTINCT = ("SELECT DISTINCT id FROM p.ds.plain", "SELECT id FROM p.ds.plain")


# ------------------------------------------------------------------ the conditional verdict


def test_a_pair_equivalent_only_if_a_key_is_unique_is_answered_with_the_condition_and_its_check(tmp_path):
    body, failed, used = ask(server(tmp_path), left=DISTINCT[0], right=DISTINCT[1], time_limit=60)
    assert not failed and used <= BOUND
    # never presented as a proof: the verdict stays the plain one, and the answer leads with the condition
    assert body["verdict"] in ("unknown", "not_equivalent") and "proof" not in body
    assert body["answer"].startswith("Equivalent only if ") and ("(not checked);" in body["answer"] or " none checked);" in body["answer"])
    assert body["if_conditions_hold"] == "equivalent" and "searched" not in body  # the conditions are the search's result
    conditions = body["conditions"]
    assert any(c["condition"] == "(id) is unique in p.ds.plain" for c in conditions)
    for condition in conditions:
        assert condition["status"] == "not checked"  # this tool runs no query, so no condition is read as true
        assert condition["check"].startswith("SELECT COUNT(*) AS violations FROM ") and "`p`.`ds`.`plain`" in condition["check"]
    assert "assumes_declared" not in body  # the table has no declared fact
    assert body["next"] == ["Run each check on the warehouse (0 violations: it holds today). To rely on it, declare it (assertions: uniqueKey or nonNull) and ask again."]


def test_the_conditional_answer_names_the_declared_facts_the_proof_also_assumed(tmp_path):
    body, failed, used = ask(server(tmp_path), left="SELECT DISTINCT id FROM p.raw.orders", right="SELECT id FROM p.raw.orders", time_limit=60)
    assert not failed and used <= BOUND and body["verdict"] != "equivalent"
    # the declaration states the key, so only the NOT NULL fact is a condition; the declared key is named with its origin
    assert [c["condition"] for c in body["conditions"]] == ["p.raw.orders.id is NOT NULL"]
    assert body["assumes_declared"] == [{
        "fact": "(id) is unique in p.raw.orders", "from": "declared uniqueKey in the declaration of orders", "status": "not checked",
        "check": "SELECT COUNT(*) AS violations FROM (SELECT `id` FROM `p`.`raw`.`orders` GROUP BY `id` HAVING COUNT(*) > 1) AS duplicated",
    }]
    assert "1 declared fact (assumes_declared)" in body["answer"]
    assert any("declared keys and NOT NULL columns hold in the data" in a for a in body.get("assumptions", []))


# ------------------------------------------------------------------ differs only when P


def test_a_refuted_pair_asks_for_the_difference_predicate_and_shows_only_a_verified_one(tmp_path, monkeypatch):
    from kumo_sql_prover import difference_explanation
    from kumo_sql_prover.difference_explanation import DifferenceExplanation

    agent = server(tmp_path)
    pair = {"left": "SELECT id FROM p.ds.plain WHERE x > 5", "right": "SELECT id FROM p.ds.plain WHERE x >= 5", "time_limit": 60}
    # the pinned prover: the search runs and finds no predicate, so nothing is claimed
    body, failed, used = ask(agent, **pair)
    assert not failed and used <= BOUND and body["verdict"] == "not_equivalent" and body["counterexample"]
    assert body["searched"]["conditions"] == "none found"
    if "differs_only_when" not in body:
        assert body["searched"]["difference"] == "none found" and body["answer"].startswith("Not equivalent")

    calls = []

    def explain(left, right, **options):
        calls.append((left, right, options))
        return DifferenceExplanation(sql="x = 5", atoms=("x = 5",), tables=("p.ds.plain",), exact=True)

    monkeypatch.setattr(difference_explanation, "explain_difference", explain)
    body, failed, used = ask(agent, **pair)
    assert not failed and used <= BOUND and body["verdict"] == "not_equivalent"
    assert body["differs_only_when"] == {"where": "x = 5", "exact": True, "tables": ["p.ds.plain"]}
    assert body["answer"].startswith("The same except on rows where x = 5: they differ exactly there (verified).")
    assert body["searched"]["difference"] == "found" and calls[0][:2] == (pair["left"], pair["right"]) and isinstance(calls[0][2]["timeout_ms"], int)

    calls.clear()  # an equivalent pair never asks
    body, _, _ = ask(agent, left="SELECT id FROM p.ds.plain WHERE x > 5 AND TRUE", right=pair["left"])
    assert body["verdict"] == "equivalent" and calls == [] and "searched" not in body and "differs_only_when" not in body


def test_a_refuted_pair_comes_with_the_verifier_query(tmp_path):
    body, _, used = ask(server(tmp_path), left="SELECT id FROM p.ds.plain WHERE x > 5", right="SELECT id FROM p.ds.plain WHERE x >= 5", time_limit=60)
    assert used <= BOUND and body["verdict"] == "not_equivalent"
    verifier = body["verifier"][0]
    assert verifier["sql"].startswith("WITH") and "SELECT id FROM p.ds.plain WHERE x > 5" in verifier["sql"] and "AS equivalent" in verifier["sql"]
    assert "not a proof" in verifier["returns"]


# ------------------------------------------------------------------ which declared facts a proof needs


def test_a_proof_given_declared_facts_names_each_fact_it_needs_and_where_it_came_from(tmp_path):
    body, failed, used = ask(server(tmp_path), left="SELECT DISTINCT id FROM p.ds.users", right="SELECT id FROM p.ds.users", time_limit=60)
    assert not failed and used <= BOUND
    assert body["verdict"] == "equivalent" and body["proof"] == "given declared project facts"
    assert body["answer"].startswith("Equivalent, given 2 declared facts: safe only while they hold: ")
    assert sorted(item["fact"] for item in body["rests_on"]) == ["(id) is unique in p.ds.users", "p.ds.users.id is NOT NULL"]
    by_fact = {item["fact"]: item for item in body["rests_on"]}
    assert by_fact["(id) is unique in p.ds.users"]["from"] == "uniqueKey assertion of model users"
    assert by_fact["p.ds.users.id is NOT NULL"]["from"] == "nonNull assertion of model users"
    assert {item["status"] for item in body["rests_on"]} == {"checked with a query"}
    assert all("check" not in item for item in body["rests_on"])  # the assertion is the check
    assert "last result is not read here" in body["status_note"]


def test_a_declared_key_and_a_required_column_are_named_with_their_own_status(tmp_path):
    snapshot = SchemaSnapshot(columns=(snapshot_column("p.raw.orders", "id", True), snapshot_column("p.raw.orders", "amount", False),
                                       snapshot_column("p.raw.refunds", "id", True)))
    agent = server(tmp_path, schema_snapshot=snapshot, assume_grain={"raw.refunds": ("id",)})
    body, failed, used = ask(agent, left="SELECT DISTINCT id FROM p.raw.orders", right="SELECT id FROM p.raw.orders", time_limit=60)
    assert not failed and used <= BOUND and body["verdict"] == "equivalent" and body["proof"] == "given declared project facts"
    by_fact = {item["fact"]: item for item in body["rests_on"]}
    assert by_fact["(id) is unique in p.raw.orders"] == {
        "fact": "(id) is unique in p.raw.orders", "from": "declared uniqueKey in the declaration of orders", "status": "not checked",
        "check": "SELECT COUNT(*) AS violations FROM (SELECT `id` FROM `p`.`raw`.`orders` GROUP BY `id` HAVING COUNT(*) > 1) AS duplicated",
    }
    assert by_fact["p.raw.orders.id is NOT NULL"]["status"] == "enforced by BigQuery"
    assert by_fact["p.raw.orders.id is NOT NULL"]["from"] == "schema snapshot: p.raw.orders.id is REQUIRED"
    assert any(step.startswith("Run the check query of each fact not checked by a query") for step in body["next"])

    body, _, _ = ask(agent, left="SELECT DISTINCT id FROM p.raw.refunds", right="SELECT id FROM p.raw.refunds", time_limit=60)
    assert body["verdict"] == "equivalent"
    assert {item["fact"]: (item["from"], item["status"]) for item in body["rests_on"]}["(id) is unique in p.raw.refunds"] == ("--assume-grain", "checked by the user")


def test_the_facts_are_those_from_pipeline_reads_when_nothing_else_is_declared(tmp_path):
    files = {name: text for name, text in FILES.items() if name != "orders.sqlx"}  # no declaration with a key: no grain claims
    session = Session(load_sqlx_project(write_project(tmp_path / "proj", files)))
    mine, theirs = mcp_compare.project_facts(session).schema, from_pipeline(session.pipeline(), [])
    assert (mine.columns, mine.constraints, mine.types) == (theirs.columns, theirs.constraints, theirs.types)
    assert mine.notes == theirs.notes


# ------------------------------------------------------------------ the time limit


def test_with_no_time_left_the_searches_do_not_run_and_nothing_is_claimed(tmp_path):
    body, failed, _ = ask(server(tmp_path), left=DISTINCT[0], right=DISTINCT[1], time_limit=1)
    assert not failed and body["verdict"] != "equivalent"
    assert "conditions" not in body and "if_conditions_hold" not in body and "differs_only_when" not in body
    if "searched" in body:  # the plain check finished: the searches had under a second, so none started
        assert all(value.startswith("not run") for value in body["searched"].values())


def test_a_search_that_runs_out_of_time_says_so_and_claims_nothing(tmp_path, monkeypatch):
    plain = {"verdict": "unknown", "reason": "no row-preserving mapping between the queries was found", "assumptions": [],
             "verifier_sql": "", "searchable": True}
    monkeypatch.setattr(mcp_compare, "prove_pair", lambda *a, **k: dict(plain))
    monkeypatch.setattr(mcp_compare, "find_conditions", lambda *a, **k: time.sleep(30))
    began = time.monotonic()
    body, failed, _ = ask(server(tmp_path), left=DISTINCT[0], right=DISTINCT[1], time_limit=2)
    assert not failed and time.monotonic() - began < 10
    assert body["verdict"] == "unknown" and "conditions" not in body and "if_conditions_hold" not in body
    assert body["searched"]["conditions"].startswith("did not finish in the ") and body["searched"]["conditions"].endswith(" s left of the time limit")
    assert body["searched"]["difference"].startswith("not run")
    assert any("larger time_limit" in step for step in body["next"])


def test_a_needed_facts_search_that_runs_out_of_time_lists_every_fact_the_proof_may_rest_on(tmp_path, monkeypatch):
    proven = {"verdict": "equivalent", "proof": "with_project_facts", "how": "set semantics", "assumptions": ["FLOAT64 values are never NaN"]}
    monkeypatch.setattr(mcp_compare, "prove_pair", lambda *a, **k: dict(proven))
    monkeypatch.setattr(mcp_compare, "find_needed", lambda *a, **k: time.sleep(30))
    body, failed, used = ask(server(tmp_path), left="SELECT DISTINCT id FROM p.ds.users", right="SELECT id FROM p.ds.users", time_limit=2)
    assert not failed and used <= BOUND and body["verdict"] == "equivalent" and body["proof"] == "given declared project facts"
    assert body["answer"] == "Equivalent (proven, given the project's declared columns, keys and NOT NULL columns)."
    assert "rests_on" not in body  # not narrowed down, so not claimed as what the proof needs
    assert sorted(item["fact"] for item in body["may_rest_on"]) == ["(id) is unique in p.ds.users", "p.ds.users.id is NOT NULL"]
    assert body["unknown"][0]["what"] == "which declared facts the proof needs" and "did not finish" in body["unknown"][0]["reason"]


def test_where_no_process_can_be_forked_a_check_past_its_cap_is_stopped_not_left_running(monkeypatch):
    """A search left running would hold the interpreter, and the next question would time out behind it."""

    import threading

    def no_fork(method):
        raise ValueError(method)

    monkeypatch.setattr(mcp_compare.multiprocessing, "get_context", no_fork)
    stopped = threading.Event()

    def search():
        try:
            while True:
                try:
                    sum(range(1000))
                except Exception:  # noqa: BLE001 - like the prover's "a crash is a failure to prove": it must not hide the stop
                    pass
        finally:
            stopped.set()

    assert mcp_compare.run_capped(search, 0.5) == (None, "timeout")
    assert stopped.wait(10)
    assert mcp_compare.run_capped(lambda: 7, 5) == (7, None)


# ------------------------------------------------------------------ the size bound


def test_the_conditional_answer_stays_inside_a_small_bound_and_keeps_the_first_condition(tmp_path):
    body, failed, used = ask(server(tmp_path), left=DISTINCT[0], right=DISTINCT[1], time_limit=60, max_bytes=mcp_answers.MIN_BYTES)
    assert not failed and used <= mcp_answers.MIN_BYTES
    assert body["answer"].startswith("Equivalent only if ") and body["verdict"] != "equivalent" and body["if_conditions_hold"] == "equivalent"
    first = body["conditions"][0]
    assert first["status"] == "not checked"
    # the check query is whole or not there: never cut short, since a cut query can still run and count something else
    assert first.get("check", "").endswith(") AS duplicated") or first.get("check", "").endswith("IS NULL") or "max_bytes=" in first["check_not_shown"]
    assert "max_bytes=" in body["more"]  # what was cut, and how to see it


def test_a_verifier_query_too_long_for_the_bound_is_cut_whole_and_the_answer_says_how_to_see_it(tmp_path):
    columns = ", ".join(f"column_with_a_long_name_{n}" for n in range(30))
    files = {"wide.sqlx": 'config { type: "table" }\nSELECT ' + ", ".join(f"1 AS column_with_a_long_name_{n}" for n in range(30)) + "\n"}
    agent = mcp_server.AgentServer(Session(load_sqlx_project(write_project(tmp_path / "wide", files))))
    pair = {"left": f"SELECT {columns} FROM p.ds.wide WHERE column_with_a_long_name_0 > 5",
            "right": f"SELECT {columns} FROM p.ds.wide WHERE column_with_a_long_name_0 >= 5", "time_limit": 60}
    body, failed, used = ask(agent, **pair)
    assert not failed and used <= BOUND and body["verdict"] == "not_equivalent" and "verifier" not in body
    assert body["verifier_shown"] == "0 of 1" and "max_bytes=" in body["more"]
    wanted = int(body["more"].split("max_bytes=")[1].split()[0])
    body, _, used = ask(agent, **pair, max_bytes=wanted)
    assert used <= wanted and pair["left"] in body["verifier"][0]["sql"] and "verifier_shown" not in body


def test_sql_the_bound_would_clip_is_taken_out_whole_and_the_item_says_how_to_see_it():
    from kumosql.mcp_answers import Answer, render, size

    long_check = "SELECT COUNT(*) AS violations FROM `p`.`ds`.`t` WHERE " + " AND ".join(f"`c{n}` IS NULL" for n in range(60))
    answer = Answer("Equivalent only if many columns are NOT NULL.")
    answer.fields["verdict"] = "unknown"
    answer.add_list("conditions", [{"condition": "c0 .. c59 are NOT NULL", "status": "not checked", "check": long_check}], keep=1)
    mcp_compare.keep_sql_whole(answer, mcp_answers.MIN_BYTES)
    out = render(answer, max_bytes=mcp_answers.MIN_BYTES)
    item = out["conditions"][0]
    assert size(out) <= mcp_answers.MIN_BYTES and out["verdict"] == "unknown" and "check" not in item
    wanted = int(item["check_not_shown"].split("max_bytes=")[1].split()[0])
    again = Answer(answer.summary, dict(answer.fields))
    again.add_list("conditions", [{"condition": "c0 .. c59 are NOT NULL", "status": "not checked", "check": long_check}], keep=1)
    mcp_compare.keep_sql_whole(again, wanted)
    assert render(again, max_bytes=wanted)["conditions"][0]["check"] == long_check

    # a predicate in a field is held to the same rule
    long_where = " OR ".join(f"c{n} = {n}" for n in range(200))
    answer = Answer("Every difference is on rows where the predicate under differs_only_when (verified).", {"verdict": "not_equivalent"})
    answer.fields["differs_only_when"] = {"where": long_where, "exact": False, "tables": ["p.ds.t"]}
    mcp_compare.keep_sql_whole(answer, mcp_answers.MIN_BYTES)
    out = render(answer, max_bytes=mcp_answers.MIN_BYTES)
    assert size(out) <= mcp_answers.MIN_BYTES and "where" not in out["differs_only_when"]
    assert "max_bytes=" in out["differs_only_when"]["where_not_shown"]
