"""Source-span preservation and safety checks for AST-backed rewrites."""

import pytest
from sqlglot import exp

from kumosql import apply_rule
from kumosql import rule_driver as engine
from kumosql.ast_utils import parse_statements
from kumosql.engine import RewriteRule


def test_noop_keeps_source_byte_for_byte():
    source = "-- header\r\nSELECT   id FROM `generic_table` WHERE active; -- tail"

    result = apply_rule("remove_trivial_predicates", source)

    assert result.sql == source
    assert result.changes == 0
    assert result.rule_success


def test_narrow_predicate_rewrite_preserves_text_outside_changed_span():
    source = "select   id from `generic_table` where 1 = 1 and flag"
    prefix = source[: source.index(" 1 = 1")]

    result = apply_rule("remove_trivial_predicates", source)

    assert result.rule_success
    assert result.sql.startswith(prefix)
    assert result.sql.endswith("flag")
    assert "SELECT\n    id\nFROM" not in result.sql
    assert "1 = 1" not in result.sql


def test_comment_in_removed_logic_stays_near_surviving_predicate():
    block = "SELECT id FROM `generic_table` WHERE flag AND /* keep this note */ TRUE"
    line = "SELECT id FROM `generic_table` WHERE flag AND -- keep this note\n TRUE"

    block_result = apply_rule("remove_trivial_predicates", block)
    line_result = apply_rule("remove_trivial_predicates", line)

    for result in (block_result, line_result):
        assert result.rule_success
        assert result.sql.count("keep this note") == 1
        assert "TRUE" not in result.sql
        assert parse_statements(result.sql)
        assert result.sql.index("flag") < result.sql.index("keep this note")
    assert "flag /* keep this note */" in block_result.sql
    assert "flag \n-- keep this note\n" in line_result.sql


def test_multiple_statements_and_literal_semicolons_keep_untouched_text():
    first = "SELECT   'a;b' AS `semi;column`;"
    middle = "\r\n-- separator\r\n"
    second_prefix = "SELECT   id FROM `generic_table` WHERE"
    source = first + middle + second_prefix + " 1 = 1 AND flag; -- final"

    result = apply_rule("remove_trivial_predicates", source)

    assert result.rule_success
    assert result.sql.startswith(first + middle + second_prefix)
    assert result.sql.endswith("flag; -- final")
    parsed = parse_statements(result.sql)
    assert len([statement for statement in parsed if not isinstance(statement, exp.Semicolon)]) == 2


def test_sqlx_sections_interpolations_and_unmodified_sql_stay_in_place():
    config = 'config { type: "table" }\r\n'
    header = "-- keep header\r\n"
    source = (
        config
        + header
        + 'select   q.id from (select id from ${ref("generic_table")} '
        + "where 1 = 1 and flag) as q"
    )

    result = apply_rule("remove_trivial_predicates", source)

    assert result.rule_success
    assert result.sql.startswith(config + header + "select   q.id from")
    assert '${ref("generic_table")}' in result.sql
    assert "1 = 1" not in result.sql
    assert "SELECT\n    q.id\nFROM" not in result.sql


@pytest.mark.parametrize(
    "spliced",
    [
        "SELECT * FROM",
        "SELECT id FROM `generic_table` WHERE FALSE",
    ],
)
def test_invalid_or_changed_ast_splice_fails_closed_to_original_input(monkeypatch, spliced):
    class DropWhereRule(RewriteRule):
        name = "test_invalid_source_splice"
        summary = "Test invalid source span output"

        def rewrite_statement(self, statement, index):
            statement.set("where", None)
            return 1, []

    source = "SELECT id FROM `generic_table` WHERE flag"
    monkeypatch.setattr(engine, "_splice_statement", lambda _source, _rendered: spliced)

    result = DropWhereRule().apply(source)

    assert not result.success
    assert result.sql == source
    assert result.changes == 0
    assert any(diagnostic.code == "output_parse_error" for diagnostic in result.diagnostics)
