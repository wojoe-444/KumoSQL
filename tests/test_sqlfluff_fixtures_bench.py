"""sqlfluff's rule fixtures: zero wrong verdicts and floors on what is proven (see tools/sqlfluff_fixtures_bench.py).

The 850 fail/fix pairs are stored in benchmarks/sqlfluff_rule_cases (MIT, credit in the licence file next to them), so
these tests need no download. ``FLOORS`` only ever goes up. Every case that is not proven or checked is a
regression case: it may improve, but it must never turn into a wrong verdict.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

pytest.importorskip("z3")
pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "sqlfluff_fixtures_bench.py"
_spec = importlib.util.spec_from_file_location("sqlfluff_fixtures_bench", _path)
bench = importlib.util.module_from_spec(_spec)
sys.modules["sqlfluff_fixtures_bench"] = bench
_spec.loader.exec_module(bench)

# measured 2026-10-02 over the whole corpus; a little room for solver timeouts under load
FLOORS = {
    "semantic_proven": 214,  # of 369 pairs that keep the meaning (218 measured)
    "semantic_design_refuted": 17,  # of 28 pairs that change it by design (18 measured)
    "layout_proven": 325,  # of 426 layout-only pairs (330 measured)
    "format_reproduced": 160,  # of 169 layout fixtures KumoSQL's preferences express (167 measured)
}

# Pairs the harness reads differently from sqlfluff (none: the four that sqlglot's BigQuery escapes made look
# different are fixed by kumosql.string_literals, issue #314). A pair added here is a parser finding, not a sqlfluff one.
PARSER_READS_DIFFERENTLY: set[str] = set()


def test_data_is_the_pinned_version():
    cases = bench.load_cases()
    assert len(cases) == 850
    assert len({c.id for c in cases}) == 850
    families = {c.family for c in cases}
    assert {"AL", "AM", "CV", "RF", "ST", "LT", "CP"} <= families
    assert len(bench.semantic_cases(cases)) + len(bench.layout_cases(cases)) == 850


def test_held_out_rules_are_a_fixed_fifth():
    cases = bench.load_cases()
    held = {c.rule for c in cases if c.held_out}
    assert {"LT01", "CV05", "CV06"} <= {r.split(",")[0] for r in held}
    assert 0.15 < sum(c.held_out for c in cases) / len(cases) < 0.3
    assert not [c for c in bench.split(cases, "dev") if c.held_out]


def test_meaning_labels_follow_the_rule_not_a_verdict():
    by_id = {c.id: c for c in bench.load_cases()}

    def label(case_id):
        case = by_id[case_id]
        return bench.meaning_label(case, bench.parse_all(case.fail, case.dialect), bench.parse_all(case.fix, case.dialect))

    assert label("CV05/test_equals_null_spaces")  # = NULL becomes IS NULL
    assert label("ST06/test_fail_select_statement_order_1")  # the select list is reordered
    assert label("ST07/test_fail_comma_join_before_using")  # USING under a bare *
    assert not label("ST07/test_fail_specify_join_keys_1")  # USING with an explicit select list keeps the meaning
    assert not label("AM02/test_fail_bare_union")


def test_schema_is_read_off_the_queries():
    trees = bench.parse_all("SELECT a.x, y FROM t AS a WHERE y > 1", "ansi")
    schema, _ = bench.infer_schema(trees)
    assert schema == {"t": ["x", "y", *bench.PLACEHOLDERS]}
    schema, kinds = bench.infer_schema(bench.parse_all("SELECT * FROM (SELECT * FROM b) AS a WHERE a.name = 'x'", "ansi"))
    assert schema["b"][0] == "name" and kinds["b.name"] == "text"  # read through a SELECT * derived table


def test_layout_check_tells_layout_from_everything_else():
    def check(rule, fail, fix, dialect="ansi"):
        return bench.decide_layout(bench.Case("x/t", rule, dialect, fail, fix, {}))

    assert check("LT01", "SELECT a,b FROM t", "SELECT a, b FROM t").outcome == "proven"
    assert check("CP01", "select a from t -- why\nwhere b = 1", "SELECT a FROM t -- why\nWHERE b = 1").outcome == "proven"
    assert check("LT01", "SELECT a FROM t -- one", "SELECT a FROM t -- two").outcome == "refuted"  # a comment changed
    assert check("LT01", "SELECT 'a b'", "SELECT 'a  b'").outcome == "refuted"  # a literal changed
    assert check("LT01", "SELECT a FROM t", "SELECT a FROM u").outcome == "refuted"  # the tree changed
    recased = check("CP02", "SELECT Foo FROM t", "SELECT foo FROM t")
    assert recased.label and recased.outcome == "refuted"  # re-casing a name is not layout
    assert not check("CP02", "SELECT a,b", "SELECT a, b").label  # a CP02 case that only fixes spacing
    renamed = check("CP02", "SELECT fooBar FROM t", "SELECT foo_bar FROM t")
    assert renamed.label and renamed.outcome == "refuted"  # so is snake-casing one


def test_templated_layout_cases_are_masked_not_skipped():
    case = bench.Case("JJ01/t", "JJ01", "ansi", "SELECT {{a}} FROM {{ t }}", "SELECT {{ a }} FROM {{ t }}", {})
    assert bench.decide_layout(case).outcome == "unsupported"
    assert bench.decide_layout_adapted(case).outcome == "proven"
    other = bench.Case("JJ01/t", "JJ01", "ansi", "SELECT {{a}} FROM t", "SELECT {{ b }} FROM t", {})
    assert bench.decide_layout_adapted(other).outcome == "refuted"  # a changed tag is not padding


def test_semantic_fixes():
    cases = bench.semantic_cases(bench.load_cases())
    report = bench.run_semantic(cases, workers=2)
    counts = bench.counts(report)
    wrong = [cases[i].id for i, v in {**report.verdicts}.items() if v.outcome == "wrong"]
    wrong += [cases[i].id + " (adapted)" for i, v in report.adapted.items() if v.outcome == "wrong"]
    assert wrong == [], wrong
    assert counts["keep"]["proven"] >= FLOORS["semantic_proven"], counts["keep"]
    assert counts["design"]["refuted"] >= FLOORS["semantic_design_refuted"], counts["design"]
    assert counts["design"]["proven"] == 0  # a fix that changes meaning is never proven equivalent
    # every refuted pair that should keep its meaning is a known parser difference or a real unsafe fix
    flagged = {cases[i].id for i, v in report.verdicts.items() if v.outcome == "refuted" and not v.label}
    assert flagged <= PARSER_READS_DIFFERENTLY, flagged - PARSER_READS_DIFFERENTLY
    # an unknown or unsupported case may improve; the one thing it may not do is become a false proof
    for i, v in report.adapted.items():
        assert v.outcome != "proven" or not v.label


def test_layout_fixes_and_kumosql_rules():
    cases = bench.load_cases()
    layout = bench.layout_cases(cases)
    report = bench.run_layout(layout, workers=2)
    counts = bench.counts(report)
    assert counts["keep"]["wrong"] == 0 and counts["design"]["wrong"] == 0
    assert counts["keep"]["proven"] >= FLOORS["layout_proven"], counts["keep"]
    assert counts["design"]["proven"] == 0
    result = bench.run_kumosql(cases, workers=2)
    formats = [v.outcome for v in result["format"].values()]
    assert "wrong" not in formats, [k for k, v in result["format"].items() if v.outcome == "wrong"]
    assert formats.count("reproduced") >= FLOORS["format_reproduced"]
    # sqlfluff before 4.4 turns `- - -5` into the comment `--5` (issue #313), and format_sql must then leave the text
    # alone; sqlfluff 4.4 spaces the signs correctly, so the fixture's own fix is reproduced and proven
    for case_id in ("LT01-operators/fail_consecutive_sign_indicators_outer_spacing", "LT01-operators/fail_consecutive_sign_indicators_trailing_code"):
        verdict = result["format"][case_id]
        assert (verdict.outcome, verdict.verified) in {("different", "unchanged"), ("reproduced", "proven")}, verdict
    for records in result["rules"].values():
        assert all(r["outcome"] != "wrong" for r in records), records
