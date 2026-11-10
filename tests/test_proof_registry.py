"""Every rewrite rule is classified: an independent proof checker, or a named legacy basis."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

from kumosql.proof_registry import FAMILIES, LEGACY_BASIS, RULE_FAMILIES, classification
from kumosql.proof_steps import RewriteStep
from kumosql.rewrite import INDEPENDENT_CHECK_FAMILIES, available_rules

DOCS = Path(__file__).resolve().parent.parent / "docs" / "rewrite-rules.md"  # the families are documented with the prover (its tests/test_proof_families_documented.py)


def test_every_registered_rule_is_classified_exactly_once():
    rules = set(available_rules())
    assert rules == set(RULE_FAMILIES) | set(LEGACY_BASIS), "classify new rules in kumosql.proof_registry"
    assert not set(RULE_FAMILIES) & set(LEGACY_BASIS)


def test_the_acceptance_layer_reads_the_registry():
    assert INDEPENDENT_CHECK_FAMILIES is RULE_FAMILIES


def test_a_legacy_rule_names_its_basis():
    for rule, basis in LEGACY_BASIS.items():
        assert len(basis) > 20, rule
        assert classification(rule) == "legacy"


def test_format_sql_has_an_independent_checker_and_is_not_legacy():
    assert classification("format_sql") == "independent"
    assert "format_sql" not in LEGACY_BASIS and RULE_FAMILIES["format_sql"] in FAMILIES


def test_an_unclassified_rule_is_an_error():
    with pytest.raises(KeyError):
        classification("not_a_rule")


def test_every_rule_family_is_registered_and_used():
    assert set(RULE_FAMILIES.values()) <= set(FAMILIES)
    assert set(FAMILIES) <= set(RULE_FAMILIES.values()) | {"predicate_cleanup", "prover_column_resolution"}  # the provers also check their own steps


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_every_family_refuses_foreign_steps(name):
    family = FAMILIES[name]
    assert family.assumptions and family.label and family.summary
    tree = sqlglot.parse_one("SELECT 1", read="bigquery")
    base = RewriteStep("test", name, 0, "SELECT 1", "SELECT 1", family.assumptions)
    from dataclasses import replace

    assert not family.check(replace(base, assumptions=family.assumptions + ("extra",)), tree.copy(), tree.copy()).accepted
    assert not family.check(replace(base, assumptions=()), tree.copy(), tree.copy()).accepted
    assert not family.check(replace(base, family="unregistered"), tree.copy(), tree.copy()).accepted


def test_the_docs_list_every_rule():
    text = DOCS.read_text(encoding="utf-8")
    for rule in available_rules():
        assert f"`{rule}`" in text, f"docs/rewrite-rules.md does not mention the rule {rule}"
