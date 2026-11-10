"""R-Bot harness text keeps Calcite's ``a > b IS NOT TRUE`` as ``(a > b) IS NOT TRUE`` (``tools/rbot_bench.normalise``)."""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

duckdb = pytest.importorskip("duckdb")

_path = Path(__file__).resolve().parent.parent / "tools" / "rbot_bench.py"
_spec = importlib.util.spec_from_file_location("rbot_bench_normalise_under_test", _path)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

ROWS = '(VALUES (1, NULL), (2, 1), (0, 1), (NULL, NULL)) AS "t" ("a", "b")'


def _duck(sql: str) -> list:
    return duckdb.connect().execute(sql).fetchall()


def _normalised_in_duckdb(raw: str) -> list:
    return _duck(bench.sb.to_dialect(bench.normalise(raw), "duckdb"))


@pytest.mark.parametrize(
    "condition",
    [
        '"t"."a" > "t"."b" IS NOT TRUE',
        '"t"."a" = "t"."b" IS NOT FALSE',
        '"t"."a" <> "t"."b" IS TRUE',
        '"t"."a" <= "t"."b" IS NOT NULL',
        'NOT "t"."a" > "t"."b" IS NOT TRUE',
        '"t"."a" > "t"."b" IS NOT TRUE AND "t"."b" < "t"."a" IS NOT TRUE',
        '"t"."a" > "t"."b" IS NOT TRUE IS NULL',
    ],
)
def test_comparison_then_is_keeps_calcite_meaning(condition):
    # DuckDB reads the raw Calcite text as Calcite does: IS binds more loosely than a comparison
    raw = f'SELECT "t"."a", "t"."b", {condition} AS "x" FROM {ROWS}'
    assert sorted(_normalised_in_duckdb(raw), key=repr) == sorted(_duck(raw), key=repr)


def test_is_not_true_is_moved_onto_the_comparison():
    assert bench.normalise('SELECT "a" > "b" IS NOT TRUE FROM "t"') == "SELECT NOT (`a` > `b`) IS TRUE FROM `t`"


def test_explicit_parenthesized_test_is_left_alone():
    # must not fire: the source says a > (b IS TRUE)
    assert bench.normalise('SELECT "a" > ("b" IS TRUE) FROM "t"') == "SELECT `a` > (`b` IS TRUE) FROM `t`"


def test_is_after_arithmetic_is_left_alone():
    # must not fire: no comparison, sqlglot already reads (a + b) IS NULL
    assert bench.normalise('SELECT "a" + "b" IS NULL FROM "t"') == "SELECT `a` + `b` IS NULL FROM `t`"


def test_comparison_with_not_operand_is_refused():
    # a > NOT b IS TRUE parses to the same tree as a > b IS NOT TRUE, so it cannot be repaired faithfully
    with pytest.raises(ValueError):
        bench.normalise('SELECT "a" > NOT "b" IS TRUE FROM "t"')


@pytest.mark.parametrize("name", ["testSome", "testAnyInProjectNonNullable", "testSelectAnyCorrelated", "testWhereAnyCorrelatedInSelect"])
def test_rbot_pairs_keep_is_not(name):
    pair = next(p for p in bench.load_pairs() if p[0] == name)
    for raw in pair[1:]:
        text = bench.normalise(raw)
        assert "> NOT" not in text and "= NOT" not in text
