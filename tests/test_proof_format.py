"""The independent check of formatting-only rewrites (``proof_format``).

Fault injection corrupts ``format_sql`` so that it changes a literal while the layout shortcut and the prover are
forced to say "proven": with the independent check off the rewrite is certified, with it on it is refused.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlglot

from kumosql import apply_rule, apply_rules, formatting, rewrite
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.formatting import FormatPreferences, FormatSqlRule
from kumosql.proof_format import FORMAT_ASSUMPTIONS, FORMAT_FAMILY, check_format_transition
from kumosql.proof_registry import FAMILIES, LEGACY_BASIS, RULE_FAMILIES
from kumosql.proof_steps import RewriteStep
from kumosql.rewrite import INDEPENDENT_CHECK, VerificationStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TREE = sqlglot.parse_one("SELECT 1", read="bigquery")


def check(before: str, after: str):
    step = RewriteStep("format_sql", FORMAT_FAMILY, 0, before, after, FORMAT_ASSUMPTIONS)
    return check_format_transition(step, TREE, TREE)


def independent(result) -> list:
    verification = getattr(result, "verification", result)
    return [record for record in verification.checks if record.kind == INDEPENDENT_CHECK]


# --- what is accepted -----------------------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    ("select a,b from t where x=1", "SELECT\n  a,\n  b\nFROM t\nWHERE x = 1"),
    ("SELECT   a\n\n\nFROM    t", "SELECT a FROM t"),
    ("select count(*) from t group by 1 order by 1", "SELECT COUNT(*) FROM t GROUP BY 1 ORDER BY 1"),
    ("SELECT CAST(a AS int64), safe_cast(b AS string) FROM t", "SELECT cast(a AS INT64), SAFE_CAST(b AS STRING) FROM t"),
    ("SELECT a FROM t -- note\nWHERE a > 1", "SELECT a FROM t -- note\nWHERE a > 1"),
    ("SELECT a, -- first\n b FROM t", "SELECT a,\n  b -- first\nFROM t"),  # a comment moves with its comma
    ("SELECT 1 /* a\n      b */", "SELECT 1 /* a\nb */"),  # a block comment re-indented line by line
    ("SELECT 'Abc', \"Dq\", r'\\d', b'x', '''t\n  Q''' FROM `My-Project.ds.T`", "SELECT\n 'Abc',\n \"Dq\",\n r'\\d',\n b'x',\n '''t\n  Q'''\nFROM `My-Project.ds.T`"),
    ("SELECT DATE_TRUNC(d, month) FROM t", "SELECT DATE_TRUNC(d, MONTH) FROM t"),
    ("CREATE view v AS select 1", "CREATE VIEW v AS SELECT 1"),
    ("SELECT x FROM t WHERE a is not null and b in (1,2)", "SELECT x FROM t WHERE a IS NOT NULL AND b IN (1, 2)"),
    ("SELECT @p, @@error.message FROM t", "SELECT\n  @p,\n  @@error.message\nFROM t"),
    ("@{FORCE_INDEX=i} SELECT 1", "@{FORCE_INDEX=i}\nSELECT 1"),
    ("SELECT * FROM my-project.ds.t", "SELECT *\nFROM my-project.ds.t"),
    ("SELECT 1;SELECT 2", "SELECT 1;\nSELECT 2"),
    ("merge t using s on t.id = s.id when matched then delete", "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN DELETE"),
    # a body KumoSQL keeps as raw text is compared by its tokens
    ("select * from graph_table(aml let x = 1 return x)", "SELECT * FROM graph_table(\n  aml\n  let x = 1\n  return x\n)"),
])
def test_reformatting_is_accepted(before, after):
    result = check(before, after)
    assert result.accepted, result.reason


def test_text_sqlglot_cannot_read_is_checked_by_tokens_alone():
    before = "CREATE ROW ACCESS POLICY p ON `d.t` GRANT TO ('a@b.c') FILTER USING (x > 1)"
    after = before.replace(" ON ", "\n  ON ").replace("create", "CREATE")
    result = check(before, after)
    assert result.accepted and "checked by tokens alone" in result.reason


# --- every kind of semantic change behind a layout change is refused -------------------------------------

@pytest.mark.parametrize("before,after,why", [
    ("SELECT a FROM t WHERE x = 1", "SELECT a\nFROM t\nWHERE x = 2", "changed literal"),
    ("SELECT 'abc' FROM t", "SELECT 'Abc'\nFROM t", "case of a string"),
    ("SELECT `Col` FROM t", "SELECT `col`\nFROM t", "case of a quoted identifier"),
    ("SELECT a FROM Orders", "SELECT a\nFROM orders", "case of a table"),
    ("SELECT a FROM `p.D.T`", "SELECT a FROM `p.d.t`", "case inside a backticked path"),
    ("SELECT Id FROM t", "SELECT id\nFROM t", "case of a column"),
    ("SELECT a AS Total FROM t", "SELECT a AS total FROM t", "case of an alias"),
    ("SELECT s.Field FROM t", "SELECT s.field FROM t", "case of a struct field"),
    ("SELECT JSON_VALUE(j, '$.Key') FROM t", "SELECT JSON_VALUE(j, '$.key') FROM t", "case of a JSON key"),
    ("SELECT a, b FROM t", "SELECT a FROM t", "dropped token"),
    ("SELECT a FROM t", "SELECT a, b FROM t", "added token"),
    ("SELECT a, b FROM t", "SELECT b, a FROM t", "reordered tokens"),
    ("SELECT a FROM t WHERE x AND y", "SELECT a FROM t WHERE y AND x", "reordered operands"),
    ("SELECT a FROM t WHERE x = 1", "SELECT a FROM t WHERE x == 1", "an operator"),
    ("SELECT a FROM t WHERE x > 1", "SELECT a FROM t WHERE x >= 1", "an operator, longer"),
    ("SELECT 1e5", "SELECT 1E5", "case of a number"),
    ("SELECT 1.0", "SELECT 1.00", "a number"),
    ("SELECT r'a\\b'", "SELECT R'a\\b'", "case of a string prefix"),
    ("SELECT a /*+ HINT(x) */ FROM t", "SELECT a /*+ HINT(y) */ FROM t", "comment inside a hint"),
    ("SELECT a /*+ HINT(x) */ FROM t", "SELECT a /*+  HINT(x) */ FROM t", "spacing inside a hint comment"),
    ("SELECT a -- keep this\nFROM t", "SELECT a -- changed\nFROM t", "an edited comment"),
    ("SELECT a -- keep this\nFROM t", "SELECT a\nFROM t", "a dropped comment"),
    ("SELECT a FROM t", "SELECT a -- new\nFROM t", "an added comment"),
    ("SELECT a /* x */ FROM t -- y", "SELECT a /* y */ FROM t -- x", "swapped comments"),
    ("SELECT a /* a\n b */ FROM t", "SELECT a /* a b */ FROM t", "a joined block comment"),
    ("@{FORCE_INDEX=i} SELECT 1", "@{FORCE_INDEX=j} SELECT 1", "a statement hint"),
    ("@{FORCE_INDEX=i} SELECT 1", "@{ FORCE_INDEX=i } SELECT 1", "spacing inside a statement hint"),
    ("SELECT a FROM ${ref('x')}", "SELECT a FROM ${ref('y')}", "template text"),
    ("SELECT a FROM ${ref('x')}", "SELECT a FROM ${ref( 'x' )}", "template spacing"),
    ("SELECT {{ var('x') }}", "SELECT {{ var('x') }}\n", "Jinja is not read"),
    ("SELECT a FROM t -- x\nWHERE b", "SELECT a FROM t -- x WHERE b", "a line comment swallows a token"),
    ("SELECT - -5", "SELECT --5", "spaced signs fused into a comment"),
    ("SELECT 'a' 'b'", "SELECT 'a''b'", "adjacent strings joined"),
    ("SELECT 'a''b'", "SELECT 'a' 'b'", "adjacent strings separated"),
    ("SELECT t.a FROM t", "SELECT t . a FROM t", "spacing around a dot"),
    ("SELECT * FROM my-project.ds.t", "SELECT * FROM my - project.ds.t", "a dashed project name split"),
    ("SELECT 'unterminated FROM t", "SELECT 'unterminated\nFROM t", "an unterminated string"),
    ("SELECT a /* open FROM t", "SELECT a /* open\nFROM t", "an unterminated comment"),
    ("SELECT x FROM t", "SELECT x FROM t;", "an added token"),
    ("SELECT 1", "SELECT 1; SELECT 1", "an added statement"),
])
def test_a_semantic_change_hidden_by_layout_is_refused(before, after, why):
    result = check(before, after)
    assert not result.accepted, why


@pytest.mark.parametrize("before,after", [
    # a name that sqlglot's trees keep with its case
    ("SELECT int64 FROM t", "SELECT INT64 FROM t"),
    ("SELECT x AS string FROM t", "SELECT x AS STRING FROM t"),
    ("SELECT date FROM t", "SELECT DATE FROM t"),
    ("SELECT my_fn(x) FROM t", "SELECT MY_FN(x) FROM t"),
    ("SELECT delete FROM t", "SELECT DELETE FROM t"),
    ("SELECT * FROM graph_table(aml let x = 1 return x)", "SELECT * FROM graph_table(aml let X = 1 return x)"),
    # a function the text creates is case sensitive even when it is spelled like a built-in
    ("CREATE TEMP FUNCTION abs(x INT64) AS (x + 1); SELECT abs(1)", "CREATE TEMP FUNCTION abs(x INT64) AS (x + 1); SELECT ABS(1)"),
    ("CREATE TEMP FUNCTION `abs`(x INT64) AS (x + 1); SELECT /* c */ abs(1)", "CREATE TEMP FUNCTION `abs`(x INT64) AS (x + 1); SELECT ABS(1)"),
    ("CREATE TEMP FUNCTION f(x INT64) AS (x); SELECT 1", "CREATE TEMP FUNCTION F(x INT64) AS (x); SELECT 1"),
    # a table function read in a FROM list is a name
    ("SELECT * FROM t, count(1)", "SELECT * FROM t, COUNT(1)"),
    ("SELECT * FROM generate_array(1, 3)", "SELECT * FROM GENERATE_ARRAY(1, 3)"),
    # a path part, a named argument, a variable
    ("SELECT ml.predict(1)", "SELECT ML.predict(1)"),
    ("SELECT safe.parse_date('%Y', 'x')", "SELECT SAFE.parse_date('%Y', 'x')"),
    ("SELECT UNNEST(x, max_depth => 2) FROM t", "SELECT UNNEST(x, MAX_DEPTH => 2) FROM t"),
    ("SELECT WITH(a AS 1, a + 1)", "SELECT WITH(A AS 1, a + 1)"),
    # code in a language the statement carries
    ("CREATE TEMP FUNCTION f(x INT64) RETURNS INT64 LANGUAGE js AS \"\"\"return x;\"\"\"", "CREATE TEMP FUNCTION f(x INT64) RETURNS INT64 LANGUAGE js AS \"\"\"return X;\"\"\""),
])
def test_a_changed_name_is_refused(before, after):
    assert not check(before, after).accepted


def test_a_type_name_is_accepted_through_the_trees_and_statement_keywords_without_them():
    # sqlglot reads CAST(.. AS int64) and CAST(.. AS INT64) alike, so the case of the type does not change the tree
    assert check("SELECT CAST(a AS int64) FROM t", "SELECT CAST(a AS INT64) FROM t").accepted
    # in a statement it cannot read, only reserved words, built-in calls and listed statement keywords may change
    unreadable = "CREATE ROW ACCESS POLICY p ON `d.t` GRANT TO ('a') FILTER USING (CAST(x AS int64) > 1)"
    assert check(unreadable, unreadable.replace("int64", "INT64").replace("CAST", "cast")).accepted
    assert not check(unreadable, unreadable.replace(" p ", " P ")).accepted
    assert not check(unreadable, unreadable.replace("(x AS", "(X AS")).accepted
    # a word right after one that introduces a table or routine name is a name, listed or not
    assert not check("DROP TABLE data", "DROP TABLE DATA").accepted
    assert check("drop table `Data`", "DROP TABLE `Data`").accepted


def test_text_that_parses_before_but_not_after_is_refused():
    assert not check("SELECT a FROM t", "SELECT a FROM t t t t").accepted


def test_wrong_family_or_assumptions_are_refused():
    step = RewriteStep("format_sql", FORMAT_FAMILY, 0, "select 1", "SELECT 1", FORMAT_ASSUMPTIONS)
    assert check_format_transition(step, TREE, TREE).accepted
    assert not check_format_transition(replace(step, family="unregistered"), TREE, TREE).accepted
    assert not check_format_transition(replace(step, assumptions=()), TREE, TREE).accepted
    assert not check_format_transition(replace(step, assumptions=FORMAT_ASSUMPTIONS[:-1]), TREE, TREE).accepted


def test_an_error_in_the_checker_is_a_refusal(monkeypatch):
    from kumosql import proof_format

    def broken(*args, **kwargs):
        raise RuntimeError("scanner unavailable")

    monkeypatch.setattr(proof_format, "_scan", broken)
    result = check("select 1", "SELECT 1")
    assert not result.accepted and "unavailable" in result.reason


def test_the_check_imports_no_rule_normalizer_or_prover():
    import kumo_sql_prover.proof_format as proof_format

    source = Path(proof_format.__file__).read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("from .", "import kumosql", "from kumosql", "import kumo_sql_prover", "from kumo_sql_prover"))]
    assert imports == ["from .proof_steps import RewriteStep, StepCheck, _key"]


def test_format_sql_is_registered_and_no_longer_legacy():
    assert RULE_FAMILIES["format_sql"] == FORMAT_FAMILY and FORMAT_FAMILY in FAMILIES
    assert "format_sql" not in LEGACY_BASIS


# --- through the acceptance layer -------------------------------------------------------------------------

def test_rule_results_record_the_independent_check():
    result = apply_rule("format_sql", "select a,b from t where x=1  and name='Bob' -- keep")
    assert result.verification.status is VerificationStatus.PROVEN
    assert [record.outcome for record in independent(result)] == ["passed"]
    assert result.verification.proof_checks[0].step.family == FORMAT_FAMILY


def test_a_script_and_statements_sqlglot_cannot_read_are_still_proven():
    script = "declare x int64 default 1;\nrepeat set x = x + 1; until x > 3 end repeat;\nselect x"
    result = apply_rule("format_sql", script)
    assert result.sql != script
    assert result.verification.status is VerificationStatus.PROVEN
    assert [record.outcome for record in independent(result)] == ["passed"]


def test_identifier_case_changes_are_not_certified():
    # sqlfluff's identifier capitalisation (CP02) renames identifiers; the checker never accepts that
    prefs = FormatPreferences(rules=("capitalisation.identifiers",), keyword_case="upper")
    result = apply_rule("format_sql", "SELECT Foo, bar FROM T", overrides={"format_sql": FormatSqlRule(prefs)})
    if result.sql != result.input_sql:
        assert result.verification.status is VerificationStatus.UNPROVEN
        assert [record.outcome for record in independent(result)] == ["failed"]


class _ChangesALiteral(RewriteRule):
    name = "format_sql"
    summary = "Fault injection: reformats and changes a literal"

    def apply(self, sql):
        return RuleOutput(sql.replace("= 1", "=\n  2"), 1, 1, 1, 0, ())


def _force_every_other_check_to_pass(monkeypatch):
    """The layout shortcut and the prover both say yes, as they would if they shared the formatter's bug."""

    monkeypatch.setattr(rewrite, "layout_only_change", lambda before, after: True)
    monkeypatch.setattr(rewrite, "prove_equivalent", lambda a, b: SimpleNamespace(proven=True, reason="", diagnostics=()))


def test_a_corrupted_formatter_is_certified_without_the_check_and_refused_with_it(monkeypatch):
    _force_every_other_check_to_pass(monkeypatch)
    source = "SELECT a FROM t WHERE x = 1"
    rule = {"format_sql": _ChangesALiteral()}

    with monkeypatch.context() as off:
        off.delitem(RULE_FAMILIES, "format_sql")
        certified = apply_rule("format_sql", source, overrides=rule)
    assert certified.sql != source and "2" in certified.sql
    assert certified.verification.status is VerificationStatus.PROVEN  # what the shared bug would have produced

    refused = apply_rule("format_sql", source, overrides=rule)
    assert refused.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(refused)] == ["failed"]
    assert not refused.success


def test_the_real_rule_with_its_own_guard_disabled_cannot_certify_a_changed_literal(monkeypatch):
    # sqlfluff's output is replaced by one with a changed literal, and the rule's own meaning guard says yes
    _force_every_other_check_to_pass(monkeypatch)
    monkeypatch.setattr(formatting, "_same_meaning", lambda before, after: True)
    monkeypatch.setattr(formatting, "format_statements", lambda sql, prefs: (sql.replace("1", "2").replace("select", "SELECT"), 0))
    result = apply_rule("format_sql", "select a from t where x = 1")
    assert result.sql != "select a from t where x = 1"
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(result)] == ["failed"]


def test_an_override_cannot_opt_out_of_the_check():
    result = apply_rule("format_sql", "SELECT a FROM t WHERE x = 1", overrides={"format_sql": _ChangesALiteral()})
    assert [record.outcome for record in independent(result)] == ["failed"]
    assert not result.success


def test_a_later_step_cannot_rescue_a_refused_format_step():
    class Restore(RewriteRule):
        name = "restore"
        summary = "Fault injection: puts the original SQL back"

        def apply(self, sql):
            return RuleOutput("SELECT a FROM t WHERE x = 1", 1, 1, 1, 0, ())

    result = apply_rules(
        ["format_sql", "restore"], "SELECT a FROM t WHERE x = 1",
        overrides={"format_sql": _ChangesALiteral(), "restore": Restore()},
    )
    assert result.sql == result.input_sql
    assert result.verification.status is VerificationStatus.UNPROVEN
    assert "safeguarded step was not accepted" in result.verification.reason


def test_a_layout_change_in_a_sqlx_section_is_checked_on_the_masked_text():
    before = "config { type: \"view\" }\nSELECT a FROM ${ref(\"t\")} WHERE b = 1"
    good = "config { type: \"view\" }\nSELECT a\nFROM ${ref(\"t\")}\nWHERE b = 1"
    bad = "config { type: \"view\" }\nSELECT a\nFROM ${ref(\"u\")}\nWHERE b = 1"
    accepted = rewrite._verify_rewrite(before, good, rule="format_sql")
    assert [record.outcome for record in independent(accepted)] == ["passed"]
    refused = rewrite._verify_rewrite(before, bad, rule="format_sql")
    assert refused.status is VerificationStatus.UNPROVEN
    assert [record.outcome for record in independent(refused)] == ["failed"]


# --- corpora ----------------------------------------------------------------------------------------------
#
# Tuned on test: the .sql files under tests/fixtures/bq_corpora, bq_syntax/sql, jaffle_shop and
# bigquery_utils_udfs, every 13th query of tests/fixtures/qualify_columns, a fixed sample of 60 GoogleSQL
# compliance queries (tests/fixtures/googlesql), and the development rules' fixtures from sqlfluff (held-out
# rules excluded). None of it is held out; the held-out fixtures were not read while the check was written.

def _corpus() -> list[tuple[str, str]]:
    import gzip
    import json
    import random

    found = []
    for pattern in ("bq_corpora/**/*.sql", "bq_syntax/sql/**/*.sql", "jaffle_shop/**/*.sql", "bigquery_utils_udfs/**/*.sql"):
        for path in sorted(FIXTURES.glob(pattern)):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "${" not in text and "{{" not in text and "{%" not in text and len(text) < 900:
                found.append((str(path.relative_to(FIXTURES)), text))
    for number, line in enumerate((FIXTURES / "qualify_columns" / "queries.jsonl").read_text().splitlines()):
        if number % 13 == 0:
            found.append((f"qualify_columns/{number}", json.loads(line)["sql"]))
    queries = json.loads(gzip.decompress((FIXTURES / "googlesql" / "queries.json.gz").read_bytes()))
    for item in random.Random(7).sample(queries, 60):
        if len(item["sql"]) < 900:
            found.append(("googlesql/" + item["id"], item["sql"]))
    return found


@pytest.mark.parametrize("shard,prefs", [
    (0, FormatPreferences()),
    (1, FormatPreferences(keyword_case="lower")),
    (2, FormatPreferences(comma_position="leading", indent_unit="tab")),
])
def test_no_real_format_sql_output_is_falsely_refused_on_the_corpus(shard, prefs):
    corpus = _corpus()[shard::3]
    rule = FormatSqlRule(prefs)
    changed = refused = 0
    failures = []
    for name, sql in corpus:
        output = rule.apply(sql)
        if output.sql == sql:
            continue
        changed += 1
        result = check(sql, output.sql)
        if not result.accepted:
            refused += 1
            failures.append((name, result.reason))
    assert changed >= 60, changed
    # words other than reserved keywords and built-in calls that the formatter re-cased and no tree can confirm
    # (ML.PREDICT, NET.HOST, dotted names, named arguments, variables) are refused on purpose; they must stay rare
    assert refused == 0, failures[:10]
