"""BigQuery-specific static checks of ``check_query`` (``kumosql.bq_static_checks``).

Each rule has a positive case, a negative case (no false finding) and a case about how sqlglot parses the construct.
The rules are tested on parsed text with a stub project; the end-to-end tests ask ``check_query`` itself.
"""

import json
from pathlib import Path

import pytest
import sqlglot

from kumosql import bq_static_checks, mcp_server
from kumosql.bq_static_checks import RESERVED_KEYWORDS, RULES, static_checks
from kumosql.mcp_answers import render
from kumosql.mcp_session import Session
from kumosql.pipeline_loading import load_sqlx_project

ROOT = Path(__file__).resolve().parent.parent
mcp_server.load_tools()  # registers the tools in the server's fixed order, whichever test of this process imports a tool module first


class StubPipeline:
    """Two tables: ``orders`` (a model with known columns) and ``customers`` (a source), nothing else."""

    models = {"p.ds.orders": object()}
    source_schema = {"p.ds.customers": {"id": "INT64", "region": "STRING"}}

    def output_columns(self, key):
        return ["id", "amount", "region"]


class StubSession:
    def __init__(self, hook=None):
        if hook is not None:
            self.column_is_nullable = hook

    def pipeline(self):
        return StubPipeline()


TABLES = {"orders": "p.ds.orders", "customers": "p.ds.customers", "elsewhere": None}


def run(sql, hook=None):
    """``(rule names of the findings, gaps)`` for a draft."""

    tree = sqlglot.parse_one(sql, read="bigquery")
    findings, gaps = static_checks(StubSession(hook), tree, sql, TABLES)
    return [f["check"] for f in findings], gaps


def findings(sql, hook=None):
    tree = sqlglot.parse_one(sql, read="bigquery")
    return static_checks(StubSession(hook), tree, sql, TABLES)[0]


def rules(sql, hook=None):
    return run(sql, hook)[0]


# ------------------------------------------------------------ 1. reserved aliases


@pytest.mark.parametrize("sql, word", [
    ("SELECT x AS new FROM orders", "NEW"),
    ("SELECT x nulls FROM orders", "NULLS"),
    ("SELECT x AS Groups FROM orders", "GROUPS"),
    ("SELECT o.id FROM orders AS qualify", "QUALIFY"),
    ("SELECT 1 FROM (SELECT 1 AS id) range", "RANGE"),
    ("WITH new AS (SELECT 1 AS id) SELECT id FROM new", "NEW"),
    ("SELECT 1 FROM UNNEST([1]) AS n WITH OFFSET AS rows", "ROWS"),
])
def test_a_reserved_keyword_used_unquoted_as_an_alias_is_found_with_its_fix(sql, word):
    [found] = findings(sql)
    assert found["check"] == "reserved_alias" and word in found["what"]
    assert "backticks" in found["fix"] and f"`{word.lower()}`" in found["fix"].lower()


def test_quoted_and_non_reserved_aliases_are_not_reported():
    for sql in (
        "SELECT x AS `new` FROM orders",
        "SELECT x AS `NULLS`, y AS `groups` FROM orders AS `range`",
        "WITH `new` AS (SELECT 1 AS id) SELECT id FROM `new`",
        "SELECT x AS date, y AS key, z AS value, w AS name FROM orders AS o",
        "SELECT o.new FROM orders o",  # a field after a dot is not an alias
        "SELECT STRUCT(1 AS new) AS s",  # not an alias of the query
    ):
        assert "reserved_alias" not in rules(sql), sql


def test_the_reserved_word_check_reads_the_source_text_for_the_backtick():
    # sqlglot parses the unquoted and the quoted spelling alike; only the Identifier's quoted flag and the text tell them apart
    unquoted, quoted = (sqlglot.parse_one(f"SELECT x AS {spelling} FROM orders", read="bigquery") for spelling in ("new", "`new`"))
    alias = lambda tree: tree.expressions[0].args["alias"]  # noqa: E731
    assert not alias(unquoted).args.get("quoted") and alias(quoted).args.get("quoted")
    assert alias(unquoted).meta["end"] != alias(quoted).meta["end"]  # the quoted token spans the backticks
    # the same word as a column reference is no alias, and a comment mentioning it changes nothing
    assert rules("SELECT `new` AS ok /* AS new */ FROM orders") == []


def test_the_reserved_keyword_list_is_the_documented_one():
    assert len(RESERVED_KEYWORDS) == 96 and {"NEW", "NULLS", "GROUPS", "QUALIFY", "STRUCT"} <= RESERVED_KEYWORDS and "GRAPH_TABLE" not in RESERVED_KEYWORDS
    assert not {"DATE", "KEY", "VALUE", "NAME"} & RESERVED_KEYWORDS and all(word == word.upper() for word in RESERVED_KEYWORDS)


# --------------------------------------------------------------------- 2. subqueries


def test_in_subquery_in_a_join_condition_is_found():
    [found] = findings("SELECT o.id FROM orders o JOIN customers c ON c.id IN (SELECT id FROM orders)")
    assert found["check"] == "subquery" and "JOIN condition" in found["what"] and "JOIN" in found["fix"]
    assert rules("SELECT o.id FROM orders o JOIN customers c ON o.id = c.id AND c.region NOT IN (SELECT region FROM customers)") == ["subquery"]


def test_in_subquery_in_where_and_in_literal_lists_in_a_join_are_fine():
    assert "subquery" not in rules("SELECT o.id FROM orders o WHERE o.id IN (SELECT id FROM customers)")
    assert "subquery" not in rules("SELECT o.id FROM orders o JOIN customers c ON c.id = o.id AND c.region IN ('eu', 'us')")
    assert "subquery" not in rules("SELECT o.id FROM orders o JOIN customers c ON c.id IN UNNEST([1, 2])")


def test_correlated_subqueries_bigquery_cannot_decorrelate_are_found():
    for sql, reason in (
        ("SELECT o.id FROM orders o LEFT JOIN customers c ON EXISTS (SELECT 1 FROM orders x WHERE x.id = c.id)", "JOIN condition"),
        ("SELECT o.id, (SELECT c.region FROM customers c WHERE c.id = o.id LIMIT 1) FROM orders o", "LIMIT"),
        ("SELECT o.id, (SELECT SUM(x.amount) FROM orders x WHERE x.id <= o.id) AS running FROM orders o", "inequality"),
    ):
        [found] = findings(sql)
        assert found["check"] == "subquery" and reason in found["what"] and "JOIN" in found["fix"], sql


def test_correlated_subqueries_that_are_fine_or_undecided_are_not_findings():
    ok = [
        "SELECT o.id, (SELECT SUM(x.amount) FROM orders x WHERE x.id = o.id) FROM orders o",  # equality: BigQuery de-correlates it
        "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.id)",
        "SELECT o.id, (SELECT SUM(x.amount) FROM orders x WHERE x.id = o.id AND x.amount <= o.amount) FROM orders o",
        "SELECT o.id, (SELECT COUNT(*) FROM UNNEST([1, 2]) AS n WHERE n <= o.id) FROM orders o",  # reads no table
        "SELECT o.id, (SELECT MAX(x.amount) FROM orders x) FROM orders o",  # not correlated
        "SELECT o.id FROM orders o JOIN (SELECT id FROM customers) c ON c.id = o.id",  # a derived table is no subquery expression
    ]
    for sql in ok:
        assert rules(sql) == [], sql
    # a correlated subquery of a shape the rule does not judge is counted, never silently passed
    _, gaps = run("SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.id = o.id)")
    assert gaps and "correlated subquer" in gaps[0][0] and "dry run" in gaps[0][1]


def test_subquery_shapes_in_sqlglots_tree_are_told_apart():
    # EXISTS holds the SELECT itself, IN holds a Subquery under "query", a scalar subquery is a Subquery in an expression slot
    tree = sqlglot.parse_one("SELECT (SELECT 1), EXISTS(SELECT 2) FROM orders o WHERE o.id IN (SELECT 3) AND o.id IN UNNEST([4])", read="bigquery")
    kinds = sorted(kind for kind, _node, _query in bq_static_checks._expression_subqueries(tree))
    assert kinds == ["exists", "in", "scalar"]


# ---------------------------------------------------------------------- 3. NOT IN


def test_not_in_with_a_null_in_the_list_is_found():
    [found] = findings("SELECT id FROM orders WHERE id NOT IN (1, 2, NULL)")
    assert found["check"] == "not_in_null" and "never returns a row" in found["what"] and "IS NULL" in found["fix"]
    assert rules("SELECT id FROM orders WHERE NOT id IN (NULL)") == ["not_in_null"]  # NOT x IN parses like x NOT IN
    assert rules("SELECT id FROM orders WHERE id NOT IN (1, 2) OR id IN (1, NULL)") == []


def test_not_in_a_subquery_needs_the_nullability_and_says_so_when_it_has_none():
    sql = "SELECT id FROM orders WHERE id NOT IN (SELECT id FROM customers)"
    found, gaps = run(sql)
    assert found == [] and gaps == [("NOT IN subqueries not checked", gaps[0][1])] and "nullability unknown" in gaps[0][1]
    # a hook that does not know is the same as no hook
    assert run(sql, hook=lambda table, column: None)[0] == [] and run(sql, hook=lambda table, column: None)[1]
    # a hook that raises is unknown, never a finding or a crash
    def broken(table, column):
        raise RuntimeError("no snapshot")
    assert run(sql, hook=broken)[0] == [] and run(sql, hook=broken)[1]


def test_not_in_a_nullable_subquery_column_is_found_when_a_schema_snapshot_says_nullable():
    asked = []

    def hook(table, column):
        asked.append((table, column))
        return True

    [found] = findings("SELECT id FROM orders WHERE id NOT IN (SELECT id FROM customers)", hook)
    assert found["check"] == "not_in_null" and "NOT EXISTS" in found["fix"] and "IS NOT NULL" in found["fix"]
    assert asked == [("p.ds.customers", "id")]
    # NOT NULL columns, a filter on the column and literals are fine and are not even gaps
    assert run("SELECT id FROM orders WHERE id NOT IN (SELECT id FROM customers)", hook=lambda t, c: False) == ([], [])
    assert run("SELECT id FROM orders WHERE id NOT IN (SELECT id FROM customers WHERE id IS NOT NULL)", hook=lambda t, c: True) == ([], [])
    assert run("SELECT id FROM orders WHERE id NOT IN (SELECT 1)") == ([], [])
    assert run("SELECT id FROM orders WHERE id NOT IN (SELECT COUNT(*) FROM customers GROUP BY region)") == ([], [])


# ------------------------------------------------------------------------ 4. LIMIT


def test_limit_without_order_by_in_a_cte_read_twice_is_found():
    sql = "WITH sample AS (SELECT id FROM orders LIMIT 10) SELECT a.id FROM sample a JOIN sample b ON a.id = b.id"
    [found] = findings(sql)
    assert found["check"] == "limit_reread" and "read 2 times" in found["what"] and "ORDER BY" in found["fix"]
    union = "WITH s AS (SELECT id FROM orders UNION ALL SELECT id FROM customers LIMIT 10) SELECT id FROM s WHERE id > 1 UNION ALL SELECT id FROM s"
    assert rules(union) == ["limit_reread"]  # the LIMIT of a UNION sits on the set operation


def test_limit_that_is_ordered_read_once_or_one_row_is_fine():
    for sql in (
        "WITH s AS (SELECT id FROM orders LIMIT 10) SELECT id FROM s",  # read once
        "WITH s AS (SELECT id FROM orders ORDER BY id LIMIT 10) SELECT a.id FROM s a JOIN s b ON a.id = b.id",
        "WITH s AS (SELECT MAX(id) AS id FROM orders LIMIT 1) SELECT a.id FROM s a JOIN s b ON a.id = b.id",
        "WITH s AS (SELECT 1 AS id LIMIT 1) SELECT a.id FROM s a JOIN s b ON a.id = b.id",
        "WITH s AS (SELECT id FROM orders) SELECT a.id FROM s a JOIN s b ON a.id = b.id",  # no LIMIT
        "WITH s AS (SELECT id FROM orders LIMIT 0) SELECT a.id FROM s a JOIN s b ON a.id = b.id",
    ):
        assert rules(sql) == [], sql


def test_limit_reads_are_counted_per_cte_name_and_a_reused_name_is_a_gap():
    two = "WITH s AS (SELECT id FROM orders LIMIT 10), t AS (SELECT id FROM s) SELECT id FROM t UNION ALL SELECT id FROM s"
    assert rules(two) == ["limit_reread"]  # the second read is inside another CTE
    twice = "WITH s AS (SELECT id FROM orders LIMIT 10) SELECT id FROM (WITH s AS (SELECT 1 AS id) SELECT id FROM s) JOIN s USING (id)"
    found, gaps = run(twice)
    assert found == [] and "repeated name" in gaps[0][0]


# ----------------------------------------------------------- 5. duplicate columns


def test_star_plus_an_alias_the_table_already_has_is_found():
    [found] = findings("SELECT o.*, SUM(amount) OVER () AS amount FROM orders o")
    assert found["check"] == "duplicate_column" and "o.*" in found["what"] and "amount_1" in found["what"] and "EXCEPT (amount)" in found["fix"]
    assert rules("SELECT o.*, c.region FROM orders o JOIN customers c ON c.id = o.id") == ["duplicate_column"]
    assert rules("WITH base AS (SELECT id, region FROM orders) SELECT base.*, 1 AS REGION FROM base") == ["duplicate_column"]  # names ignore case


def test_star_without_a_clash_is_fine_and_unknown_columns_are_a_gap():
    for sql in (
        "SELECT o.*, 1 AS extra FROM orders o",
        "SELECT o.* EXCEPT (amount), 1 AS amount FROM orders o",
        "SELECT o.* REPLACE (amount * 2 AS amount) FROM orders o",
        "SELECT o.*, c.id AS customer_id FROM orders o JOIN customers c ON c.id = o.id",
    ):
        assert rules(sql) == [], sql
    found, gaps = run("SELECT e.*, 1 AS id FROM elsewhere e")
    assert found == [] and "unknown columns" in gaps[0][0] and "not declared" in gaps[0][1]
    # a USING join merges columns in SELECT *, which is not modelled: a gap, not a guess
    assert rules("SELECT *, 1 AS id FROM orders o JOIN customers c USING (id)") == []


# ---------------------------------------------------------------------- 6. CASE


@pytest.mark.parametrize("case, fragment", [
    ("CASE WHEN id = 1 THEN 'a' WHEN id = 1 THEN 'b' END", "branch 2"),
    ("CASE WHEN id > 5 THEN 'a' WHEN id > 10 THEN 'b' END", "already taken by branch 1"),
    ("CASE WHEN id BETWEEN 1 AND 9 THEN 'a' WHEN id IN (2, 3) THEN 'b' END", "branch 2"),
    ("CASE WHEN region = 'eu' THEN 1 WHEN region IN ('eu') THEN 2 END", "branch 2"),
    ("CASE WHEN TRUE THEN 'a' WHEN id = 2 THEN 'b' END", "always true"),
    ("CASE WHEN id > 0 THEN 'a' WHEN 1 = 1 THEN 'b' WHEN id < 0 THEN 'c' END", "branch 2"),
    ("CASE WHEN 1 = 1 THEN 'a' ELSE 'b' END", "ELSE can never be reached"),
    ("CASE WHEN NULL THEN 'a' ELSE 'b' END", "never true"),
    ("CASE WHEN FALSE THEN 'a' END", "always false"),
    ("CASE WHEN 1 > 2 THEN 'a' END", "always false"),
    ("CASE WHEN id = 1 AND id = 2 THEN 'a' END", "always false"),
    ("CASE WHEN id > 5 AND id < 3 THEN 'a' END", "always false"),
    ("CASE WHEN id IS NULL THEN 'a' WHEN id IS NOT NULL THEN 'b' ELSE 'c' END", "IS NULL and IS NOT NULL"),
    ("CASE id WHEN 1 THEN 'a' WHEN 1 THEN 'b' END", "branch 2"),
    ("CASE id WHEN NULL THEN 'a' ELSE 'b' END", "never true"),
])
def test_case_branches_that_can_never_be_reached_are_found(case, fragment):
    [found] = findings(f"SELECT {case} AS c FROM orders")
    assert found["check"] == "case_branch" and fragment in found["what"] and found["fix"]


@pytest.mark.parametrize("case", [
    "CASE WHEN id > 10 THEN 'a' WHEN id > 5 THEN 'b' END",  # the wider one last
    "CASE WHEN id = 1 THEN 'a' WHEN id = 2 THEN 'b' ELSE 'c' END",
    "CASE WHEN id = 1 THEN 'a' WHEN o.id = 1 THEN 'b' END",  # a qualified and an unqualified name are not known to be one column
    "CASE WHEN id IS NULL THEN 'a' WHEN id > 5 THEN 'b' ELSE 'c' END",
    "CASE WHEN id >= 5 THEN 'a' WHEN id < 5 THEN 'b' ELSE 'c' END",  # ELSE still takes the NULLs
    "CASE WHEN id > 5 AND region = 'eu' THEN 'a' WHEN id > 10 AND region = 'us' THEN 'b' END",
    "CASE WHEN id > 5 AND LENGTH(region) > 2 THEN 'a' WHEN id > 10 THEN 'b' END",  # the first is not fully understood
    "CASE WHEN RAND() < 0.5 THEN 'a' WHEN RAND() < 0.5 THEN 'b' END",  # the same text, two different draws
    "CASE WHEN id = 1 THEN 'a' WHEN id <> 1 THEN 'b' ELSE 'c' END",
    "CASE WHEN id = 1 OR id = 2 THEN 'a' WHEN id = 2 OR id = 3 THEN 'b' END",
    "CASE WHEN region > 'a' THEN 'x' WHEN region > 'b' THEN 'y' END",  # string ordering is not judged
    "CASE id WHEN 1 THEN 'a' WHEN 2 THEN 'b' ELSE 'c' END",
    "CASE WHEN id IS NOT NULL THEN 'a' WHEN id IS NULL THEN 'b' END",
])
def test_case_expressions_that_are_fine_or_not_provable_are_not_reported(case):
    assert rules(f"SELECT {case} AS c FROM orders o") == []


def test_case_conditions_in_parentheses_and_numbers_in_other_spellings_are_read_the_same():
    assert rules("SELECT CASE WHEN (id > 5) THEN 1 WHEN ((id > 10)) THEN 2 END FROM orders") == ["case_branch"]
    assert rules("SELECT CASE WHEN id = 1 THEN 1 WHEN id = 1.0 THEN 2 END FROM orders") == ["case_branch"]
    assert rules("SELECT CASE WHEN id > -1 THEN 1 WHEN id > 0 THEN 2 END FROM orders") == ["case_branch"]
    assert rules("SELECT CASE WHEN id > 0 THEN 1 WHEN id > -1 THEN 2 END FROM orders") == []


# --------------------------------------------------------------- 7. FOR SYSTEM_TIME


def test_system_time_on_only_some_references_to_a_table_is_found():
    sql = "SELECT a.id FROM orders a FOR SYSTEM_TIME AS OF TIMESTAMP '2026-01-01 00:00:00 UTC' JOIN orders b ON b.id = a.id"
    [found] = findings(sql)
    assert found["check"] == "system_time" and "2 times" in found["what"] and "only 1" in found["what"] and "same FOR SYSTEM_TIME" in found["fix"]
    # references spelled differently are one table when the project resolves them to the same key
    assert rules("SELECT 1 FROM p.ds.orders FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP() JOIN orders USING (id)") == ["system_time"]
    # ... and when the project does not know them, different spellings are not claimed to be one table
    assert rules("SELECT 1 FROM x.orders FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP() JOIN orders USING (id)") == []


def test_system_time_on_every_reference_or_on_different_tables_is_fine():
    stamp = "FOR SYSTEM_TIME AS OF TIMESTAMP '2026-01-01 00:00:00 UTC'"
    assert rules(f"SELECT 1 FROM orders a {stamp} JOIN orders b {stamp} ON b.id = a.id") == []
    assert rules(f"SELECT 1 FROM orders a {stamp} JOIN customers b ON b.id = a.id") == []
    assert rules("SELECT 1 FROM orders a JOIN orders b ON b.id = a.id") == []
    cte = f"WITH s AS (SELECT * FROM orders {stamp}) SELECT 1 FROM s a JOIN s b ON a.id = b.id"
    assert rules(cte) == []  # the CTE name is not a table; orders is read once


def test_system_time_clause_is_the_version_of_the_table_in_sqlglots_tree():
    tree = sqlglot.parse_one("SELECT 1 FROM orders AS a FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP() JOIN orders AS b ON TRUE", read="bigquery")
    assert [t.args.get("version") is not None for t in tree.find_all(sqlglot.exp.Table)] == [True, False]


# ------------------------------------------------------------------ 8. CTE shadows


def test_a_cte_named_like_a_column_of_another_table_in_scope_is_found():
    sql = "WITH region AS (SELECT 'eu' AS name) SELECT region, o.id FROM orders o, region"
    [found] = findings(sql)
    assert found["check"] == "cte_shadow" and "STRUCT" in found["what"] and "region" in found["what"] and "alias the CTE" in found["fix"]
    assert rules("WITH region AS (SELECT 1 AS n) SELECT COUNT(*) FROM orders o JOIN region ON TRUE WHERE region IS NOT NULL") == ["cte_shadow"]


def test_a_cte_that_does_not_clash_is_fine_and_unknown_columns_are_a_gap():
    for sql in (
        "WITH region AS (SELECT 1 AS n) SELECT o.region FROM orders o, region",  # qualified: the column
        "WITH region AS (SELECT 1 AS n) SELECT region FROM orders o, region AS r",  # aliased: the range variable is r
        "WITH area AS (SELECT 1 AS n) SELECT region FROM orders o, area",  # no column named area
        "WITH region AS (SELECT 1 AS n) SELECT n FROM region",  # nothing else in scope
        "WITH region AS (SELECT 1 AS n) SELECT o.id FROM orders o, region",  # the column is never read bare
    ):
        assert rules(sql) == [], sql
    found, gaps = run("WITH region AS (SELECT 1 AS n) SELECT region FROM elsewhere e, region")
    assert found == [] and "may shadow" in gaps[0][0] and "not declare" in gaps[0][1]


# ------------------------------------------------------------ the rules as a whole


def test_every_rule_is_registered_once_and_a_crashing_rule_becomes_a_gap(monkeypatch):
    names = [name for name, _rule in RULES]
    assert len(names) == len(set(names)) == 8

    def boom(ctx):
        raise ValueError("boom")

    monkeypatch.setattr(bq_static_checks, "RULES", (("explodes", boom),) + RULES[:1])
    found, gaps = run("SELECT x AS new FROM orders")
    assert found == ["reserved_alias"] and gaps == [("static check explodes", "could not run (ValueError)")]


def test_findings_name_the_rule_what_and_fix_and_stay_short():
    sql = ("WITH new AS (SELECT 1 AS id) SELECT CASE WHEN TRUE THEN 1 ELSE 2 END AS x, o.*, 1 AS id "
           "FROM orders o JOIN customers c ON c.id IN (SELECT id FROM new) WHERE o.id NOT IN (1, NULL)")
    for item in findings(sql):
        assert set(item) == {"check", "what", "fix"} and item["check"] in {name for name, _ in RULES}
        assert 0 < len(item["what"]) <= 240 and 0 < len(item["fix"]) <= 200


# ------------------------------------------------------------------ in check_query


@pytest.fixture
def server(tmp_path):
    folder = tmp_path / "project"
    (folder / "definitions").mkdir(parents=True)
    (folder / "workflow_settings.yaml").write_text("defaultProject: p\ndefaultDataset: ds\n")
    (folder / "definitions" / "t.sqlx").write_text('config { type: "table" }\nSELECT 1 AS x, 2 AS y\n')
    (folder / "definitions" / "u.sqlx").write_text('config { type: "table" }\nSELECT 1 AS x, 3 AS z\n')
    return mcp_server.AgentServer(Session(load_sqlx_project(folder)))


def ask(server, **arguments):
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "check_query", "arguments": arguments}})["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def test_check_query_no_longer_says_no_problems_for_a_reserved_word_alias(server):
    body, failed = ask(server, sql="SELECT x AS new FROM t")
    assert not failed and body["verdict"] == "problems_found" and "No problems found" not in body["answer"]
    [found] = [f for f in body["findings"] if f["check"] == "reserved_alias"]
    assert "NEW" in found["what"] and "alias" in found["what"] and "`new`" in found["fix"]
    assert "static" in body["checked"] and body["counts"]["reserved_alias"] == 1
    # the same query with the alias in backticks is clean again
    body, _ = ask(server, sql="SELECT x AS `new` FROM t")
    assert body["verdict"] == "no_problems_found" and "static" in body["checked"]


def test_check_query_uses_project_columns_for_stars_and_reports_what_it_could_not_decide(server):
    body, _ = ask(server, sql="SELECT t.*, u.z AS y FROM t JOIN u ON u.x = t.x")
    [found] = [f for f in body["findings"] if f["check"] == "duplicate_column"]
    assert "t.*" in found["what"] and "y" in found["what"]
    body, _ = ask(server, sql="SELECT x FROM t WHERE x NOT IN (SELECT x FROM u)")
    assert body["verdict"] == "no_problems_found"
    assert any(item["what"] == "NOT IN subqueries not checked" and "nullability unknown" in item["reason"] for item in body["unknown"])


def test_check_query_asks_the_session_for_nullability_when_it_can(server):
    server.session.column_is_nullable = lambda table, column: (table, column) == ("p.ds.u", "x")
    body, _ = ask(server, sql="SELECT x FROM t WHERE x NOT IN (SELECT x FROM u)")
    [found] = [f for f in body["findings"] if f["check"] == "not_in_null"]
    assert "NOT EXISTS" in found["fix"]
    assert not any(item["what"] == "NOT IN subqueries not checked" for item in body.get("unknown", []))


def test_static_findings_come_before_the_other_checks_and_fit_the_bound(server):
    sql = "SELECT x AS new, CASE WHEN TRUE THEN 1 ELSE 2 END AS c FROM t WHERE y NOT IN (1, NULL) AND nope = 1"
    body, failed = ask(server, sql=sql)
    assert not failed and body["findings"][0]["check"] in {name for name, _ in RULES}
    assert len(json.dumps(body, separators=(",", ":")).encode()) <= 2000
    assert {f["check"] for f in body["findings"]} >= {"reserved_alias", "case_branch", "not_in_null", "types"}


def test_the_docs_list_every_rule():
    reference = (ROOT / "docs" / "mcp-server.md").read_text(encoding="utf-8")
    plain = (ROOT / "docs_simple" / "mcp-server.md").read_text(encoding="utf-8")
    for name, _rule in RULES:
        assert f"`{name}`" in reference, name
    assert "reserves" in plain and "NOT IN" in plain


def test_no_static_finding_on_any_model_query_of_the_public_example_projects():
    """The corpora are real BigQuery Dataform code that runs: a finding here is a false positive, so the rule must be tightened."""

    from kumosql.mcp_preflight import Draft
    from kumosql.mcp_tools import expand_refs

    checked = 0
    for project in sorted((ROOT / "tests" / "fixtures" / "bq_corpora").iterdir()):
        if not project.is_dir() or project.name in ("security-analytics",):
            continue  # the largest project is covered by the agent floor; the others keep this test fast
        session = Session(load_sqlx_project(project))
        for key, model in session.pipeline().models.items():
            sql, _refs = expand_refs(session, model.sql or "")
            draft = Draft(session, sql) if sql.strip() and "${" not in sql else None
            if draft is None or draft.tree is None:
                continue
            found, _gaps = static_checks(session, draft.tree, draft.sql, draft.tables)
            assert found == [], (project.name, key, found)
            checked += 1
    assert checked > 100
