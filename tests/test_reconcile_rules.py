"""Known-difference rules: the file format, strict predicate checks, writing them back, and rules made from "except when P"."""

from datetime import date, datetime, timezone

import pytest

from kumo_sql_prover import difference_explanation
from kumo_sql_prover.difference_explanation import DifferenceExplanation
from kumosql.reconcile_rules import (
    KnownDiffRule, RuleError, check_predicate, dump_rules, load_rules, parse_rule, parse_rules, predicate_columns,
    rule_from_except_when, save_rules,
)

yaml = pytest.importorskip("yaml")


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------- reading


def test_a_yaml_rules_file_reads_its_aliases_and_an_unquoted_date(tmp_path):
    path = write(tmp_path, "rules.yml", """
rules:
  - name: refunds_zeroed
    sql: "l.status = 'refunded' AND r.amount = 0"
    verified: 2026-11-02
    verified_count: 41
    max_growth_ratio: 3
    columns: [amount, status]
    description: The new table zeroes refunded amounts.
  - name: test_orders
    where: status = 'test'
    verified_date: "2026-10-01"
""")
    first, second = load_rules(path)
    assert first == KnownDiffRule("refunds_zeroed", "l.status = 'refunded' AND r.amount = 0", "2026-11-02",
                                  "The new table zeroes refunded amounts.", 41, 3.0, ("amount", "status"))
    assert (second.name, second.predicate, second.verified_at, second.verified_count) == ("test_orders", "status = 'test'", "2026-10-01", None)


def test_a_json_file_and_a_bare_list_are_read_the_same_way(tmp_path):
    body = '[{"name": "a", "predicate": "l.x IS NULL", "verified_at": "2026-01-02"}]'
    listed = load_rules(write(tmp_path, "rules.json", body))
    wrapped = load_rules(write(tmp_path, "wrapped.json", '{"rules": ' + body + "}"))
    assert listed == wrapped == [KnownDiffRule("a", "l.x IS NULL", "2026-01-02")]
    assert load_rules(write(tmp_path, "empty.yml", "rules: []\n")) == []


@pytest.mark.parametrize("item, message", [
    ({"predicate": "x = 1", "verified_at": "2026-01-01"}, "needs a name"),
    ({"name": "a", "verified_at": "2026-01-01"}, "needs a predicate"),
    ({"name": "a", "predicate": "x = 1"}, "needs verified_at"),
    ({"name": "a", "predicate": "x = 1", "verified_at": "11/02/2026"}, "verified_at must be"),
    ({"name": "a", "predicate": "x = 1", "verified_at": "2026-13-01"}, "verified_at must be"),
    ({"name": "a", "predicate": "x = 1", "verified_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}, "verified_at must be"),
    ({"name": "a", "predicate": "x = 1", "verified_at": "2026-01-01", "verified_count": -1}, "verified_count"),
    ({"name": "a", "predicate": "x = 1", "verified_at": "2026-01-01", "verified_count": True}, "verified_count"),
    ({"name": "a", "predicate": "x = 1", "verified_at": "2026-01-01", "max_growth_ratio": 0.5}, "max_growth_ratio"),
    ({"name": "a", "predicate": "x = 1", "verified_at": "2026-01-01", "verfied_count": 3}, "unknown field"),
    ({"name": "a", "predicate": "x = 1", "sql": "x = 2", "verified_at": "2026-01-01"}, "predicate twice"),
    ({"name": "a\nb", "predicate": "x = 1", "verified_at": "2026-01-01"}, "one line"),
    ("x = 1", "must be a mapping"),
])
def test_a_malformed_rule_is_refused_with_what_to_change(item, message):
    with pytest.raises(RuleError, match=message):
        parse_rule(item)


def test_names_are_unique_ignoring_case_and_the_file_shape_is_checked(tmp_path):
    rule = {"name": "Late", "predicate": "x = 1", "verified_at": "2026-01-01"}
    with pytest.raises(RuleError, match="two rules are named"):
        parse_rules([rule, {**rule, "name": "late"}])
    with pytest.raises(RuleError, match="one key, `rules`"):
        parse_rules({"rules": [rule], "extra": 1})
    with pytest.raises(RuleError, match="must end in"):
        load_rules(write(tmp_path, "rules.txt", "[]"))
    with pytest.raises(RuleError, match="does not exist"):
        load_rules(tmp_path / "missing.yml")
    with pytest.raises(RuleError, match="not valid YAML"):
        load_rules(write(tmp_path, "bad.yml", "rules: [\n"))
    with pytest.raises(RuleError, match="not valid JSON"):
        load_rules(write(tmp_path, "bad.json", "{"))


# ------------------------------------------------------------- predicates


@pytest.mark.parametrize("text", [
    "l.status = 'refunded' AND r.order_id IS NULL",
    "amount > 5",
    "TRUE",
    "note LIKE '%;%'",  # a semicolon inside a string is text, not a second statement
    "DATE_DIFF(l.updated, r.updated, DAY) <= 1",
    "flagged",
    "ABS(l.amount - r.amount) < 0.01 -- rounding",
])
def test_read_only_conditions_over_one_joined_row_are_accepted(text):
    check_predicate(text)


@pytest.mark.parametrize("text, message", [
    ("x = 1; DROP TABLE t", "no semicolon"),
    ("x = 1;", "no semicolon"),
    ("DELETE FROM t WHERE x = 1", "statement"),
    ("DROP TABLE t", "statement"),
    ("TRUNCATE TABLE t", "statement"),
    ("GRANT SELECT ON t TO x", "statement"),
    ("CALL p()", "statement"),
    ("EXISTS (SELECT 1 FROM t)", "subquery or statement"),
    ("l.id IN (SELECT id FROM other)", "subquery or statement"),
    ("SUM(amount) > 10", "aggregate or window"),
    ("ROW_NUMBER() OVER (ORDER BY id) = 1", "aggregate or window"),
    ("x = @param", "query parameter"),
    ("orders.status IS NULL", "qualified by 'orders'"),
    ("l.address.city = 'x'", "nested field"),
    ("42", "not a value"),
    ("'yes'", "not a value"),
    ("", "must be a SQL condition"),
    ("x = (", "not a SQL condition"),
])
def test_predicates_that_read_other_tables_write_or_aggregate_are_refused(text, message):
    with pytest.raises(RuleError, match=message):
        check_predicate(text)


def test_predicate_columns_name_their_side():
    tree = check_predicate("l.a = r.b AND c IS NULL AND l.a > 1")
    assert predicate_columns(tree) == [("l", "a"), ("r", "b"), ("", "c")]


# ---------------------------------------------------------------- writing


def test_rules_written_as_yaml_or_json_read_back_unchanged(tmp_path):
    rules = [
        KnownDiffRule("quotes \"and\" back\\slash", "l.note = 'it\\'s' OR r.note = \"x\"", "2026-11-28",
                      "multi\nline — ünïcode", 3, 2.5, ("note",)),
        KnownDiffRule("plain", "TRUE", "2026-01-01"),
    ]
    for name in ("rules.yml", "rules.yaml", "rules.json"):
        assert load_rules(save_rules(rules, tmp_path / name)) == rules
    text = dump_rules(rules)
    assert text.startswith("# Known-difference rules") and 'verified_at: "2026-11-28"' in text
    assert yaml.safe_load(dump_rules([]))["rules"] == []
    with pytest.raises(RuleError, match="must end in"):
        save_rules(rules, tmp_path / "rules.txt")


# ------------------------------------------------------- from except_when


def test_an_except_when_predicate_becomes_a_rule_in_the_same_format(tmp_path):
    payload = {"sql": "orders.status IS NULL OR proj.ds.orders.amount < 0", "atoms": ["status IS NULL", "amount < 0"],
               "tables": ["proj.ds.orders"], "exact": True}
    rule = rule_from_except_when(payload, verified_at="2026-11-28")
    assert rule.predicate == "status IS NULL OR amount < 0"  # the table's qualifiers become bare columns
    assert rule.name.startswith("except_when_") and rule.name == rule_from_except_when(payload).name
    assert rule.verified_at == "2026-11-28" and rule.verified_count is None
    assert rule.columns == ("status", "amount")
    assert "exactly where it holds" in rule.description and "proj.ds.orders" in rule.description
    assert load_rules(save_rules([rule], tmp_path / "emitted.yml")) == [rule]
    named = rule_from_except_when({"sql": "x = 5", "tables": ["t"], "exact": False}, name="x_is_five")
    assert named.name == "x_is_five" and named.verified_at == datetime.now(timezone.utc).date().isoformat()
    assert "may cover more" in named.description


@pytest.mark.parametrize("payload", [None, {}, {"sql": ""}, {"sql": "x = (", "tables": []}])
def test_an_empty_or_unreadable_explanation_is_refused(payload):
    with pytest.raises(RuleError):
        rule_from_except_when(payload)


# ------------------------------------------------- prove-sql-equivalent


LEFT, RIGHT = "SELECT id FROM t WHERE x > 5", "SELECT id FROM t WHERE x >= 5"


def sql_files(tmp_path, left, right):
    a, b = tmp_path / "a.sql", tmp_path / "b.sql"
    a.write_text(left, encoding="utf-8")
    b.write_text(right, encoding="utf-8")
    return str(a), str(b)


@pytest.fixture
def explained(monkeypatch):
    found = DifferenceExplanation(sql="t.x = 5", atoms=("x = 5",), tables=("t",), exact=True)
    monkeypatch.setattr(difference_explanation, "explain_difference", lambda left, right, **options: found)


def test_explain_difference_emits_the_predicate_as_a_rule(explained, tmp_path, capsys):
    from kumosql.cli import prove_main

    files = sql_files(tmp_path, LEFT, RIGHT)
    assert prove_main([*files, "--explain-difference"]) == 2
    assert "rules:" not in capsys.readouterr().out  # unchanged unless a rule is asked for
    assert prove_main([*files, "--emit-rule", "--rule-name", "x_is_five"]) == 2  # implies --explain-difference
    out = capsys.readouterr().out
    assert "except when: t.x = 5" in out
    document = yaml.safe_load(out[out.index("# Known-difference rules"):])
    assert document["rules"][0]["name"] == "x_is_five" and document["rules"][0]["predicate"] == "x = 5"
    target = tmp_path / "rule.json"
    assert prove_main([*files, "--rule-output", str(target)]) == 2
    assert f"written to {target}" in capsys.readouterr().out
    (rule,) = load_rules(target)
    assert rule.predicate == "x = 5" and date.fromisoformat(rule.verified_at) and rule.name.startswith("except_when_")


def test_no_rule_is_emitted_without_a_verified_predicate(tmp_path, capsys, monkeypatch):
    from kumosql.cli import prove_main

    monkeypatch.setattr(difference_explanation, "explain_difference", lambda left, right, **options: None)
    target = tmp_path / "rule.yml"
    assert prove_main([*sql_files(tmp_path, LEFT, RIGHT), "--rule-output", str(target)]) == 2
    assert "no verified difference predicate" in capsys.readouterr().err and not target.exists()
    assert prove_main([*sql_files(tmp_path, LEFT, LEFT), "--emit-rule"]) == 0
    assert "proven equivalent" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        prove_main([*sql_files(tmp_path, LEFT, RIGHT), "--rule-output", str(tmp_path / "rule.txt")])
