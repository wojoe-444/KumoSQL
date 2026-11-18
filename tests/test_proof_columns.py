"""The independent re-derivation of the provers' own bare-column resolution (``proof_columns``).

The SMT compiler picks a source for every bare column, the algebraic normalizer writes ``a.x`` for a bare ``x``
and the scoped-identity check hands the question to sqlglot's qualifier. ``proof_columns`` reads the statement's
text again, builds the scopes itself and says which FROM item owns each bare column. Fault injection corrupts a
prover's resolution and requires the check to refuse the proof; with the check off the same proof is certified.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from kumosql import algebraic_equivalence as alg
from kumosql import apply_rule, apply_rules
from kumosql.engine import RewriteRule, RuleOutput
from kumosql.rewrite import VerificationStatus
from kumosql import proof_columns, smt_equivalence as smt, structural_identity
from kumosql.algebraic_equivalence import prove_equivalent_algebraic
from kumosql.proof_columns import (
    AGREE,
    AMBIGUOUS,
    COLUMN_RESOLUTION_ASSUMPTIONS,
    COLUMN_RESOLUTION_FAMILY,
    DISAGREE,
    OUTPUT,
    OWNER,
    UNCHECKED,
    UNDECIDED,
    USING,
    Claim,
    StatementReader,
    check_added_qualifiers,
    check_column_resolution_transition,
    reader_for,
    recording,
    summarize,
    tag,
)
from kumosql.proof_steps import RewriteStep
from kumosql.prover_context import use_columns
from kumosql.smt_equivalence import SmtStatus, prove_equivalent_smt

FIXTURES = Path(__file__).resolve().parent / "fixtures"

TABLES = {
    "p.d.orders": ["id", "total", "cid"],
    "p.d.customers": ["name", "cid", "region"],
    "a": ["x", "k", "arr", "s"],
    "b": ["y", "k", "f"],
    "c": ["z", "w"],
    "t": ["a", "b"],
    "u": ["a", "c"],
}


def reader(sql: str, tables: dict | None = None) -> StatementReader:
    return reader_for(sql, TABLES if tables is None else tables)


def resolve(sql: str, name: str, nth: int = 0, tables: dict | None = None):
    """The independent reading of the ``nth`` bare column called ``name`` of ``sql``."""

    r = reader(sql, tables)
    column = [c for c in r.columns if c.name.lower() == name and not c.args.get("table")][nth]
    return r.resolve(column), r


def owner_name(sql: str, name: str, nth: int = 0, tables: dict | None = None) -> str:
    resolution, _ = resolve(sql, name, nth, tables)
    assert resolution.status == OWNER, resolution
    return resolution.name


# --- the independent resolver ------------------------------------------------------------------------------

@pytest.mark.parametrize("sql,name,expected", [
    ("SELECT id, name FROM `p.d.orders` AS o JOIN `p.d.customers` AS c ON o.cid = c.cid", "id", "o"),
    ("SELECT id, name FROM `p.d.orders` AS o JOIN `p.d.customers` AS c ON o.cid = c.cid", "name", "c"),
    ("SELECT id FROM `p.d.orders` JOIN `p.d.customers` AS c ON TRUE", "id", "orders"),  # no alias: the table's name
    ("SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b JOIN `p.d.orders` o ON b.k = o.cid WHERE y = x)", "y", "b"),
    # an inner source wins over an outer one that has the same name
    ("SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = 1)", "a", "u"),
    # a correlated name that no inner source has belongs to the outer select
    ("SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE c = b)", "b", "t"),
    # the select list, WHERE and ON never read a select alias
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k WHERE y > 1", "y", "b"),
    # the ON of a join reads the sources up to and including its own join
    ("SELECT 1 FROM a JOIN b ON x = y JOIN c ON z = w", "z", "c"),
    # CTEs and derived tables name their own columns, and a CTE shadows a table
    ("WITH a AS (SELECT 1 AS only_here) SELECT only_here FROM a JOIN b ON TRUE", "only_here", "a"),
    ("SELECT q FROM (SELECT 1 AS p) AS s JOIN (SELECT 2 AS q) AS r ON TRUE", "q", "r"),
    # an UNNEST element is a value of its own
    ("SELECT x, v FROM a CROSS JOIN UNNEST(a.arr) AS v", "v", "v"),
])
def test_the_owner_of_a_bare_column_is_read_from_the_text(sql, name, expected):
    assert owner_name(sql, name) == expected


def test_the_owner_is_the_from_item_not_just_a_name():
    sql = "SELECT x FROM a JOIN b ON TRUE"
    resolution, r = resolve(sql, "x")
    assert resolution.source == r.items.index(next(i for i in r.items if i.alias_or_name == "a"))


@pytest.mark.parametrize("sql,name,status", [
    # GROUP BY, HAVING, QUALIFY and ORDER BY read a select alias before a source column
    ("SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y", "y", OUTPUT),
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k ORDER BY y", "y", OUTPUT),
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k QUALIFY ROW_NUMBER() OVER (ORDER BY y) = 1", "y", OUTPUT),
    ("SELECT * REPLACE (x + 1 AS y) FROM a JOIN b ON TRUE ORDER BY y", "y", OUTPUT),
    ("SELECT s.f FROM a JOIN b ON a.k = b.k ORDER BY f", "f", OUTPUT),
    # two sources have the name
    ("SELECT k FROM a JOIN b ON a.x = b.y", "k", AMBIGUOUS),
    # a USING column is the merged value
    ("SELECT k FROM a LEFT JOIN b USING (k)", "k", USING),
    # not decidable from the text and the supplied columns
    ("SELECT x FROM a JOIN mystery ON TRUE", "x", UNDECIDED),  # a table with unknown columns hides a candidate
    ("SELECT zzz FROM a JOIN b ON TRUE", "zzz", UNDECIDED),  # no source has it
    ("SELECT a FROM a JOIN b ON TRUE", "a", UNDECIDED),  # the name of a source
    ("SELECT x FROM a NATURAL JOIN b", "x", UNDECIDED),
    ("SELECT x FROM (SELECT * FROM a) AS s JOIN `p.d.orders` AS o ON TRUE JOIN a ON TRUE", "x", UNDECIDED),  # a star hides the columns
    ("SELECT x FROM (SELECT AS STRUCT 1 AS x) AS s JOIN a ON TRUE", "x", UNDECIDED),  # a struct's fields are readable bare
    ("SELECT FOO(x, MONTH) FROM a JOIN b ON TRUE", "month", UNDECIDED),  # a date part, not a column
    ("SELECT 1 FROM UNNEST(arr) AS e CROSS JOIN a", "arr", UNDECIDED),  # an UNNEST in FROM reads no source
    ("SELECT x FROM a AS q JOIN b AS q ON TRUE", "x", UNDECIDED),  # two sources share a name
    ("SELECT x FROM a CROSS JOIN GENERATE_ARRAY(1, 2) AS g", "x", UNDECIDED),
    ("WITH RECURSIVE r AS (SELECT 1 AS p UNION ALL SELECT p + 1 FROM r) SELECT p FROM r JOIN b ON TRUE JOIN mystery ON TRUE", "p", UNDECIDED),
])
def test_what_the_text_cannot_settle_is_not_guessed(sql, name, status):
    assert resolve(sql, name)[0].status == status


def test_a_later_source_is_not_readable_in_an_earlier_on():
    # BigQuery reads z in the first ON as an outer column: the later source c cannot be its owner.
    resolution, _ = resolve("SELECT 1 FROM a JOIN b ON a.k = z JOIN c ON TRUE", "z")
    assert resolution.status == UNDECIDED


def test_an_only_source_of_unknown_columns_owns_the_name_by_elimination_unless_an_outer_select_could():
    assert resolve("SELECT x FROM mystery", "x")[0].status == OWNER
    assert resolve("SELECT x FROM mystery JOIN a ON TRUE", "x", tables={"a": ["k"]})[0].name == "mystery"
    nested = "SELECT 1 FROM a WHERE EXISTS (SELECT 1 FROM mystery WHERE x = 1)"
    assert resolve(nested, "x")[0].status == UNDECIDED  # x could be the outer a's column
    derived = "SELECT 1 FROM (SELECT x FROM mystery) AS s"
    assert resolve(derived, "x")[0].status == OWNER  # a derived table sees no outer column
    assert owner_name("SELECT x FROM (SELECT * FROM a) AS s JOIN b ON TRUE", "x") == "s"  # b lacks x: s is the only candidate
    assert owner_name("SELECT p FROM (SELECT AS VALUE 1 AS p) AS s JOIN b ON TRUE", "p") == "s"


def test_a_source_that_has_the_name_beats_one_with_unknown_columns_only_when_alone():
    assert resolve("SELECT x FROM a JOIN mystery ON TRUE", "x")[0].status == UNDECIDED


def test_names_are_compared_without_case():
    sql = "SELECT X FROM a AS Left_Side JOIN b AS right_side ON TRUE"
    assert owner_name(sql, "x") == "left_side"
    assert owner_name(sql, "x", tables={"a": ["X"], "b": ["Y"]}) == "left_side"


def test_a_select_alias_that_is_the_same_column_resolves_to_that_column():
    sql = "SELECT a.x AS x FROM a JOIN b ON a.k = b.k GROUP BY x"
    resolution, r = resolve(sql, "x")
    assert resolution.status == OUTPUT
    assert r.judge(next(c for c in r.columns if c.name == "x" and not c.table), Claim(qualifier="a")).kind == AGREE
    assert r.judge(next(c for c in r.columns if c.name == "x" and not c.table), Claim(qualifier="b")).kind == DISAGREE


def test_a_statement_that_is_not_bigquery_or_not_one_statement_has_no_reader():
    assert reader_for("SELECT x FROM a", TABLES, "mysql") is None
    assert reader_for("SELECT 1; SELECT 2", TABLES) is None
    assert reader_for("SELECT FROM WHERE (", TABLES) is None


# --- comparing with what a prover decided --------------------------------------------------------------------

def test_a_claim_is_judged_against_the_independent_owner():
    sql = "SELECT x, y FROM a JOIN b ON a.k = b.k"
    r = reader(sql)
    x, y = (next(c for c in r.columns if c.name == n) for n in "xy")
    tag_a, tag_b = (r.items.index(next(i for i in r.items if i.alias_or_name == n)) for n in "ab")
    assert r.judge(x, Claim(source=tag_a)).kind == AGREE
    assert r.judge(x, Claim(source=tag_b)).kind == DISAGREE
    assert r.judge(y, Claim(qualifier="B")).kind == AGREE
    assert r.judge(y, Claim(qualifier="a")).kind == DISAGREE
    assert r.judge(y, Claim(alias=True)).kind == DISAGREE
    assert r.judge(y, Claim()).kind == UNCHECKED  # the choice was not traced


def test_a_name_two_sources_share_is_refused_when_a_prover_chose_one_owner():
    r = reader("SELECT k FROM a JOIN b ON a.x = b.y")
    k = next(c for c in r.columns if c.name == "k" and not c.table)
    assert r.judge(k, Claim(qualifier="a")).kind == DISAGREE


def test_a_select_alias_is_read_as_an_alias_not_a_source_column():
    sql = "SELECT x AS y, COUNT(*) AS n FROM a JOIN b ON a.k = b.k GROUP BY y"
    r = reader(sql)
    y = [c for c in r.columns if c.name == "y"][0]
    assert r.judge(y, Claim(alias=True)).kind == AGREE
    assert r.judge(y, Claim(qualifier="b")).kind == DISAGREE


def test_tagged_columns_line_up_with_the_texts_columns():
    sql = "SELECT x, y FROM a JOIN b ON a.k = b.k WHERE y > 1"
    r = reader(sql)
    parsed = tag(sqlglot.parse_one(sql, read="bigquery"))
    tagged = [c for c in parsed.find_all(exp.Column) if proof_columns.column_tag(c) is not None]
    assert [c.name for c in tagged] == ["x", "y", "y"]  # the qualified a.k and b.k are not numbered
    for column in tagged:
        assert r.columns[proof_columns.column_tag(column)].name == column.name
    assert all(proof_columns.source_tag(i) is not None for i in parsed.find_all(exp.Table))


def test_a_tagged_column_that_does_not_line_up_is_unchecked_not_refused():
    r = reader("SELECT x FROM a JOIN b ON TRUE")
    stray = exp.column("y")
    stray.meta[proof_columns.COLUMN_TAG] = 0  # number 0 is x
    assert r.judge_tagged(stray, Claim(qualifier="b"), "test").kind == UNCHECKED
    assert r.judge_tagged(exp.column("x"), Claim(qualifier="b"), "test").kind == UNCHECKED  # not numbered at all


def test_the_numbers_survive_a_copy():
    parsed = tag(sqlglot.parse_one("SELECT x FROM a", read="bigquery"))
    copy = parsed.copy()
    assert proof_columns.column_tag(next(copy.find_all(exp.Column))) == 0
    assert proof_columns.source_tag(next(copy.find_all(exp.Table))) == 0


# --- a pass that only adds qualifiers --------------------------------------------------------------------------

@pytest.mark.parametrize("before,after", [
    ("SELECT x, y FROM a JOIN b ON a.k = b.k", "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k"),
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k WHERE y > 1", "SELECT a.x AS y FROM a JOIN b ON a.k = b.k WHERE b.y > 1"),
    ("SELECT x FROM a WHERE EXISTS (SELECT 1 FROM b WHERE y = x)", "SELECT a.x FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.y = a.x)"),
    ("SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = 1)", "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = 1)"),
])
def test_qualifiers_that_name_the_independent_owner_agree(before, after):
    verdict = check_added_qualifiers(before, after, TABLES)
    assert verdict.kind == AGREE and verdict.agreed >= 1, verdict


@pytest.mark.parametrize("before,after,reason", [
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT b.x FROM a JOIN b ON a.k = b.k", "belongs to 'a'"),
    ("SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = 1)", "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.a = 1)", "belongs to 'u'"),
    ("SELECT x AS y FROM a JOIN b ON a.k = b.k ORDER BY y", "SELECT x AS y FROM a JOIN b ON a.k = b.k ORDER BY b.y", "select alias"),
    ("SELECT k FROM a JOIN b ON a.x = b.y", "SELECT a.k FROM a JOIN b ON a.x = b.y", "more than one source"),
    ("SELECT k FROM a LEFT JOIN b USING (k)", "SELECT a.k FROM a LEFT JOIN b USING (k)", ""),
])
def test_a_qualifier_naming_another_owner_disagrees(before, after, reason):
    verdict = check_added_qualifiers(before, after, TABLES)
    if reason:
        assert verdict.kind == DISAGREE and reason in verdict.reason, verdict
    else:
        assert verdict.kind in (DISAGREE, UNCHECKED), verdict  # a merged USING column is not an owner's column


@pytest.mark.parametrize("before,after", [
    ("SELECT x FROM a JOIN mystery ON TRUE", "SELECT a.x FROM a JOIN mystery ON TRUE"),  # columns unknown
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x AS x2 FROM a JOIN b ON a.k = b.k"),  # more than qualifiers
    ("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT x FROM a JOIN b ON a.k = b.k"),  # nothing added
])
def test_what_cannot_be_decided_stays_unchecked_and_adds_no_refusal(before, after):
    verdict = check_added_qualifiers(before, after, TABLES)
    assert not verdict.refused
    assert verdict.kind in (UNCHECKED, AGREE)


def test_text_that_does_not_parse_is_unchecked():
    # the statement the prover was given is not text the reader reads; the prover's own output that cannot be read back is
    assert check_added_qualifiers("SELECT FROM WHERE (", "SELECT a.x FROM a", TABLES).kind == UNCHECKED
    assert check_added_qualifiers("SELECT x FROM a", "SELECT a.x FROM a WHERE (", TABLES).refused


def test_an_error_inside_the_checker_is_a_refusal_not_a_pass(monkeypatch):
    def broken(self, column):
        raise RuntimeError("scope analysis unavailable")

    monkeypatch.setattr(StatementReader, "_bare", broken)
    verdict = check_added_qualifiers("SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k", TABLES)
    assert verdict.refused and "check failed" in verdict.reason


# --- the registry family --------------------------------------------------------------------------------------

def step_of(before: str, after: str, tables: dict | None = None, **changes) -> RewriteStep:
    known = tuple(sorted((name, tuple(columns)) for name, columns in (TABLES if tables is None else tables).items()))
    step = RewriteStep("prover_normalization", COLUMN_RESOLUTION_FAMILY, 0, before, after, COLUMN_RESOLUTION_ASSUMPTIONS, known_columns=known)
    return replace(step, **changes)


def check(before: str, after: str, **changes):
    tree = sqlglot.parse_one(before, read="bigquery")
    return check_column_resolution_transition(step_of(before, after, **changes), tree, tree)


def test_a_registered_step_is_accepted_only_when_every_qualifier_is_re_derived():
    assert check("SELECT x, y FROM a JOIN b ON a.k = b.k", "SELECT a.x, b.y FROM a JOIN b ON a.k = b.k").accepted
    assert not check("SELECT x, y FROM a JOIN b ON a.k = b.k", "SELECT a.x, a.y FROM a JOIN b ON a.k = b.k").accepted
    assert not check("SELECT x FROM a JOIN mystery ON TRUE", "SELECT a.x FROM a JOIN mystery ON TRUE").accepted  # undecided
    assert not check("SELECT x FROM a JOIN b ON TRUE", "SELECT x FROM a JOIN b ON TRUE").accepted  # nothing added


def test_a_registered_step_with_the_wrong_family_or_assumptions_is_refused():
    before, after = "SELECT x FROM a JOIN b ON a.k = b.k", "SELECT a.x FROM a JOIN b ON a.k = b.k"
    assert check(before, after).accepted
    assert not check(before, after, family="unregistered").accepted
    for assumptions in ((), COLUMN_RESOLUTION_ASSUMPTIONS[:-1], COLUMN_RESOLUTION_ASSUMPTIONS + ("x_is_not_null",)):
        assert not check(before, after, assumptions=assumptions).accepted


def test_a_registered_step_reads_the_text_not_the_callers_tree():
    before, after = "SELECT x FROM a JOIN b ON a.k = b.k", "SELECT b.x FROM a JOIN b ON a.k = b.k"
    honest = sqlglot.parse_one("SELECT a.x FROM a JOIN b ON a.k = b.k", read="bigquery")
    assert not check_column_resolution_transition(step_of(before, after), honest, honest).accepted


# --- independence -----------------------------------------------------------------------------------------------

def test_the_checker_imports_no_rule_normalizer_or_prover():
    tree = ast.parse((Path(proof_columns.__file__)).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names if not node.module)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
    assert imported == {"__future__", "collections", "contextlib", "contextvars", "dataclasses", "sqlglot", "proof_qualify", "proof_steps"}


# --- the SMT compiler: an honest prover is never refused, a corrupted one is -------------------------------------

SCHEMA = {"t": ["a", "b"], "u": ["a", "c"]}
INNER_WINS = "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = t.b)"
INNER_WINS_QUALIFIED = "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a = t.b)"
OUTER_COLUMN = "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.a = t.b)"


def test_an_honest_smt_proof_is_certified_and_every_choice_agrees():
    with recording() as log:
        result = prove_equivalent_smt(INNER_WINS, INNER_WINS_QUALIFIED, schema=SCHEMA)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    kinds = summarize(log)["smt"]
    assert kinds.get(AGREE, 0) >= 1 and DISAGREE not in kinds


def test_the_check_does_not_turn_a_non_equivalence_into_anything_else():
    assert prove_equivalent_smt(INNER_WINS, OUTER_COLUMN, schema=SCHEMA).status is SmtStatus.NOT_PROVEN


def _resolve_the_shared_name_to_the_outer_source(monkeypatch):
    """The compiler's column lookup believes the inner source has no ``a``: the name falls out to the outer ``t``."""

    real = smt._Source.may_have

    def may_have(self, name):
        if self.occ is not None and self.occ.table == "u" and name == "a":
            return False
        return real(self, name)

    monkeypatch.setattr(smt._Source, "may_have", may_have)


def test_a_prover_that_resolves_a_shared_name_to_the_wrong_source_is_refused(monkeypatch):
    _resolve_the_shared_name_to_the_outer_source(monkeypatch)
    with recording() as log:
        result = prove_equivalent_smt(INNER_WINS, OUTER_COLUMN, schema=SCHEMA)
    assert result.status is SmtStatus.NOT_PROVEN
    assert "independent check of column resolution" in result.reason and "'u'" in result.reason
    assert summarize(log)["smt"][DISAGREE] >= 1


def test_the_same_corrupted_prover_certifies_a_false_proof_with_the_check_off(monkeypatch):
    _resolve_the_shared_name_to_the_outer_source(monkeypatch)
    with proof_columns.disabled():
        result = prove_equivalent_smt(INNER_WINS, OUTER_COLUMN, schema=SCHEMA)
    assert result.status is SmtStatus.PROVEN_EQUIVALENT  # the queries differ: this is the false proof the check removes


def test_a_select_alias_read_by_group_by_is_an_agreed_choice():
    with recording() as log:
        result = prove_equivalent_smt(
            "SELECT a AS y, COUNT(*) AS n FROM t GROUP BY y", "SELECT t.a AS y, COUNT(*) AS n FROM t GROUP BY t.a", schema=SCHEMA,
        )
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert DISAGREE not in summarize(log)["smt"]


def test_a_column_the_text_cannot_settle_is_left_to_the_prover():
    # no schema: the check decides by elimination only where nothing outer could own the name
    with recording() as log:
        result = prove_equivalent_smt("SELECT x FROM mystery WHERE y > 1", "SELECT mystery.x FROM mystery WHERE mystery.y > 1")
    assert result.status is SmtStatus.PROVEN_EQUIVALENT
    assert DISAGREE not in summarize(log).get("smt", {})


def test_a_proof_with_no_bare_column_records_nothing():
    with recording() as log:
        prove_equivalent_smt("SELECT t.b FROM t", "SELECT t.b FROM t", schema=SCHEMA)
    assert log == []


def test_an_error_inside_the_check_refuses_the_proof(monkeypatch):
    def broken(self, column):
        raise RuntimeError("scope analysis unavailable")

    monkeypatch.setattr(StatementReader, "_bare", broken)
    result = prove_equivalent_smt(INNER_WINS, INNER_WINS_QUALIFIED, schema=SCHEMA)
    assert result.status is SmtStatus.NOT_PROVEN
    assert "independent check" in result.reason
    with proof_columns.disabled():
        assert prove_equivalent_smt(INNER_WINS, INNER_WINS_QUALIFIED, schema=SCHEMA).status is SmtStatus.PROVEN_EQUIVALENT


def test_a_reader_that_cannot_be_built_refuses_the_proof(monkeypatch):
    def broken(self, statement, known):
        raise RuntimeError("no scopes")

    monkeypatch.setattr(StatementReader, "__init__", broken)
    result = prove_equivalent_smt(INNER_WINS, INNER_WINS_QUALIFIED, schema=SCHEMA)
    assert result.status is SmtStatus.NOT_PROVEN and "independent reader could not be built" in result.reason


# --- through the rewrite pipeline: a rule's own override cannot opt out, and a later step cannot rescue --------------

class _SwapsTheOwner(RewriteRule):
    """Fault injection: a rule whose output reads the outer ``t.a`` where the input read the inner ``u.a``."""

    name = "swap_owner"
    summary = "Fault injection: changes which source a shared name reads"

    def apply(self, sql):
        return RuleOutput(OUTER_COLUMN, 1, 1, 1, 0, ())


class _Tidies(RewriteRule):
    name = "tidy"
    summary = "Fault injection: a harmless later step"

    def apply(self, sql):
        return RuleOutput(sql.replace("SELECT t.b", "SELECT t.b AS b", 1), 1, 1, 1, 0, ())


def test_a_rewrite_certified_only_by_a_prover_that_picks_the_wrong_owner_is_refused_with_the_check_on(monkeypatch):
    _resolve_the_shared_name_to_the_outer_source(monkeypatch)
    overrides = {"swap_owner": _SwapsTheOwner()}
    with proof_columns.disabled(), use_columns(SCHEMA):
        certified = apply_rule("swap_owner", INNER_WINS, overrides=overrides)
    assert certified.verification.status is VerificationStatus.PROVEN  # the false proof the check removes
    with use_columns(SCHEMA):
        refused = apply_rule("swap_owner", INNER_WINS, overrides=overrides)
    assert refused.verification.status is not VerificationStatus.PROVEN


def test_an_override_cannot_opt_out_of_the_column_resolution_check(monkeypatch):
    # the check lives in the provers, so a differently configured rule instance has nothing to switch off
    _resolve_the_shared_name_to_the_outer_source(monkeypatch)
    with use_columns(SCHEMA):
        result = apply_rule("swap_owner", INNER_WINS, overrides={"swap_owner": _SwapsTheOwner()})
    assert not result.success or result.verification.status is not VerificationStatus.PROVEN


def test_a_later_step_cannot_rescue_a_step_the_check_refused(monkeypatch):
    _resolve_the_shared_name_to_the_outer_source(monkeypatch)
    overrides = {"swap_owner": _SwapsTheOwner(), "tidy": _Tidies()}
    with use_columns(SCHEMA):
        result = apply_rules(["swap_owner", "tidy"], INNER_WINS, overrides=overrides)
    assert result.verification.status is not VerificationStatus.PROVEN
    with proof_columns.disabled(), use_columns(SCHEMA):
        assert apply_rules(["swap_owner", "tidy"], INNER_WINS, overrides=overrides).verification.status is VerificationStatus.PROVEN


# --- the SMT compiler's own qualification of a bare column over one known table ----------------------------------

def test_the_compilers_one_table_qualification_agrees_with_the_owner_it_names():
    text = "SELECT a FROM t"
    parsed = tag(sqlglot.parse_one(text, read="bigquery"))
    with recording() as log:
        done = smt._canonical_aliases(parsed, {"t": ["a"]}, reader(text))
    assert done.sql() == "SELECT kq0.a FROM t AS kq0"
    assert summarize(log) == {"smt_schema_qualification": {AGREE: 1}}


def test_a_cte_that_shadows_a_table_cannot_lend_it_the_columns_of_that_table():
    # The compiler reads ``u`` as the physical table, whose columns include ``a``; here ``u`` is a CTE with
    # only ``z``, so ``a`` is the outer ``t``'s column and qualifying it as ``u``'s changes the query.
    text = "WITH u AS (SELECT 1 AS z) SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = 1)"
    schema = {"t": ["a", "b"], "u": ["a", "c"]}
    parsed = tag(sqlglot.parse_one(text, read="bigquery"))
    with pytest.raises(smt.Unsupported, match="independent check of column resolution"):
        smt._canonical_aliases(parsed.copy(), schema, reader(text, schema))
    corrupted = smt._canonical_aliases(parsed.copy(), schema)  # no reader: the pass qualifies it as u's
    assert "WHERE u.a = 1" in corrupted.sql()


# --- the algebraic normalizer's qualification passes ---------------------------------------------------------------

ALG_SCHEMA = {"t": ["k", "v"], "u": ["k", "v", "w"]}
ALG_LEFT = "SELECT t.k FROM t WHERE EXISTS (SELECT 1 FROM u WHERE v = 1)"  # v is u's: the inner source wins
ALG_RIGHT = "SELECT t.k FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.v = 1)"  # t.v: the outer column


def _qualify_a_shared_name_with_the_outer_source(tree, schema):
    """A faulty pass: a bare column that an inner source and an enclosing source both have is given to the enclosing one."""

    for column in list(tree.find_all(exp.Column)):
        if column.table or isinstance(column.this, exp.Star):
            continue
        inner = column.find_ancestor(exp.Select)
        outer = inner.find_ancestor(exp.Select) if inner is not None else None
        if outer is None:
            continue
        for source in alg._sources_of(outer):
            if column.name.lower() in schema.get(source.name.lower(), []):
                column.set("table", exp.to_identifier(source.alias_or_name))
    return tree


def test_an_honest_algebraic_proof_keeps_its_proof_and_records_agreement():
    schema = {"a": ["k", "x"], "b": ["k", "y"]}
    q1 = "SELECT a.k, SUM(y) AS s FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.k"
    q2 = "SELECT a.k, SUM(b.y) AS s FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.k"
    with recording() as log:
        result = prove_equivalent_algebraic(q1, q2, schema=schema)
    assert result.proven
    summary = summarize(log)
    # Whichever pass names the bare column first is the one recorded: the prover's own qualification, or the
    # alias-column reading that runs before it (aliased_columns, of the VeriEQL worker rules).
    qualification = summary.get("algebraic_qualification") or summary.get("aliased_column_qualification")
    assert qualification == {AGREE: 1}


def test_honest_algebraic_non_equivalence_is_unchanged():
    assert not prove_equivalent_algebraic(ALG_LEFT, ALG_RIGHT, schema=ALG_SCHEMA).proven


def test_an_algebraic_pass_that_gives_a_shared_name_to_the_wrong_source_is_refused(monkeypatch):
    monkeypatch.setattr(alg, "_qualify_correlated_columns", _qualify_a_shared_name_with_the_outer_source)
    with recording() as log:
        result = prove_equivalent_algebraic(ALG_LEFT, ALG_RIGHT, schema=ALG_SCHEMA)
    assert not result.proven
    assert "independent check of column resolution" in result.reason
    assert summarize(log)["algebraic_qualification"][DISAGREE] >= 1


def test_the_same_corrupted_algebraic_pass_certifies_a_false_proof_with_the_check_off(monkeypatch):
    monkeypatch.setattr(alg, "_qualify_correlated_columns", _qualify_a_shared_name_with_the_outer_source)
    with proof_columns.disabled():
        result = prove_equivalent_algebraic(ALG_LEFT, ALG_RIGHT, schema=ALG_SCHEMA)
    assert result.proven  # the queries differ (v is u's in the first, t's in the second)


def test_the_per_select_algebraic_pass_is_checked_too(monkeypatch):
    schema = {"a": ["k", "x"], "b": ["k", "y"]}
    wrong = "SELECT a.k, SUM(a.y) AS s FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.k"  # invalid: a has no y

    def first_source(tree, schema):
        for select in list(tree.find_all(exp.Select)):
            if not select.args.get("joins"):
                continue
            first = alg._sources_of(select)[0]
            for column in select.find_all(exp.Column):
                if not column.table and not isinstance(column.this, exp.Star) and column.find_ancestor(exp.Select) is select:
                    column.set("table", exp.to_identifier(first.alias_or_name))
        return tree

    monkeypatch.setattr(alg, "_qualify_outer_join_columns", first_source)
    q = "SELECT a.k, SUM(y) AS s FROM a LEFT JOIN b ON a.k = b.k GROUP BY a.k"
    result = prove_equivalent_algebraic(q, wrong, schema=schema)
    assert not result.proven
    with proof_columns.disabled():
        assert prove_equivalent_algebraic(q, wrong, schema=schema).proven


# --- the scoped-identity check, which hands the question to sqlglot's qualifier --------------------------------------

def test_the_scoped_identity_check_still_proves_honest_pairs_and_records_agreement():
    left = "SELECT a, b FROM t WHERE a > 1"
    right = "SELECT t.a, t.b FROM t WHERE t.a > 1"
    with recording() as log:
        assert structural_identity.same_scoped_query(left, right, schema=SCHEMA, dialect="bigquery", compare_names=True)
    assert summarize(log)["scoped_identity"][AGREE] >= 2


def test_a_qualifier_that_names_the_wrong_owner_defeats_the_scoped_identity_check(monkeypatch):
    real = structural_identity.qualify

    def qualify_with_the_outer_source(tree, **kwargs):
        done = real(tree, **kwargs)
        for column in done.find_all(exp.Column):
            if column.name == "a" and column.table == "u":
                column.set("table", exp.to_identifier("t"))
        return done

    monkeypatch.setattr(structural_identity, "qualify", qualify_with_the_outer_source)
    left = "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE a = 1)"
    right = "SELECT t.b FROM t WHERE EXISTS (SELECT 1 FROM u WHERE t.a = 1)"
    assert not structural_identity.same_scoped_query(left, right, schema=SCHEMA, dialect="bigquery", compare_names=True)
    with proof_columns.disabled():
        assert structural_identity.same_scoped_query(left, right, schema=SCHEMA, dialect="bigquery", compare_names=True)


# --- the independent reader and the qualify_columns rule were written separately and must agree ----------------------

def _key_of(table: exp.Table) -> str:
    parts = [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]
    return ".".join(parts)


def _invent_schema(tree: exp.Expression, variant: int) -> dict[str, list[str]]:
    columns: dict[str, set[str]] = {}
    for select in tree.find_all(exp.Select):
        from_ = select.args.get("from_") or select.args.get("from")
        if from_ is None:
            continue
        relations = [from_.this] + [join.this for join in select.args.get("joins") or []]
        tables = [r for r in relations if isinstance(r, exp.Table) and isinstance(r.this, exp.Identifier)]
        if not tables:
            continue
        by_alias = {(t.alias or t.name).lower(): t for t in tables}
        for column in select.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            if column.table:
                owner = by_alias.get(column.table.lower())
                if owner is not None:
                    columns.setdefault(_key_of(owner), set()).add(column.name.lower())
            else:
                digest = int(hashlib.md5(f"{variant}:{column.name.lower()}".encode()).hexdigest(), 16)
                columns.setdefault(_key_of(tables[digest % len(tables)]), set()).add(column.name.lower())
    return {name: sorted(names) for name, names in columns.items()}


def _corpus() -> list[str]:
    queries = [json.loads(line)["sql"] for line in (FIXTURES / "qualify_columns" / "queries.jsonl").read_text().splitlines()]
    for pattern in ("bq_corpora/**/*.sql", "bq_syntax/sql/query/*.sql", "jaffle_shop/**/*.sql"):
        for path in sorted(FIXTURES.glob(pattern)):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "${" not in text:
                queries.append(text)
    return queries


@pytest.mark.parametrize("variant", [0, 1])
def test_every_qualifier_the_rule_writes_on_the_corpus_is_the_independent_owner_and_every_wrong_one_disagrees(variant):
    from kumosql import apply_rule

    changed = mutations = 0
    for sql in _corpus():
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except Exception:  # noqa: BLE001 - a statement sqlglot cannot read is not a qualification case
            continue
        schema = _invent_schema(tree, variant)
        with use_columns(schema):
            result = apply_rule("qualify_columns", sql)
        if not result.changes:
            continue
        before = tree.sql(dialect="bigquery")
        verdict = check_added_qualifiers(before, result.sql, schema)
        assert verdict.kind == AGREE, (sql, result.sql, verdict)
        changed += 1
        after = sqlglot.parse_one(result.sql, read="bigquery")
        old, new = list(tree.find_all(exp.Column)), list(after.find_all(exp.Column))
        gained = [n for o, n in zip(old, new) if not o.table and n.table]
        names = {t.alias_or_name.lower() for t in after.find_all(exp.Table)}
        for column in gained[:3]:
            wrong = sorted(n for n in names | {"no_such_source"} if n != column.table.lower())[0]
            original = column.args["table"]
            column.set("table", exp.to_identifier(wrong))
            assert check_added_qualifiers(before, after.sql(dialect="bigquery"), schema).kind == DISAGREE, (sql, wrong)
            mutations += 1
            column.set("table", original)
    assert changed >= 40 and mutations >= 80, (changed, mutations)
