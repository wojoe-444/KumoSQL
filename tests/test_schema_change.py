"""Schema-change assessment: regression cases, plus floors for the generated suite (docs/evals/schema-change-bench.md)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from kumosql.pipeline import Pipeline
from kumosql.pipeline_types import Model, Target

pytestmark = pytest.mark.eval

TOOLS = Path(__file__).resolve().parent.parent / "tools"


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def _bench():
    spec = importlib.util.spec_from_file_location("schema_change_bench", TOOLS / "schema_change_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["schema_change_bench"] = module
    spec.loader.exec_module(module)
    return module


def _pipeline(**sql):
    models = {f"p.d.{n}": Model(Target("p", "d", n), "table", q) for n, q in sql.items()}
    schema = {"p.d.raw": {"a": "INT64", "b": "STRING", "c": "INT64"}}
    return Pipeline(models, {"p.d.raw": Target("p", "d", "raw")}, schema)


def _names(effects):
    return sorted(e.model.split(".")[-1] for e in effects)


def test_star_passes_added_column_on_and_named_columns_do_not():
    p = _pipeline(star="SELECT * FROM `p.d.raw`", named="SELECT a, b FROM `p.d.raw`")
    r = p.assess_schema_change("add_column", "p.d.raw", "z")
    assert _names(r.output_changes) == ["star"] and not r.breaks and r.unaffected == 1


def test_dropping_a_named_column_breaks_and_a_star_changes():
    p = _pipeline(star="SELECT * FROM `p.d.raw`", named="SELECT a, b FROM `p.d.raw`", later="SELECT b FROM `p.d.star`")
    r = p.assess_schema_change("drop_column", "p.d.raw", "a")
    assert _names(r.breaks) == ["named"] and _names(r.output_changes) == ["star"]
    assert not r.unknown and r.unaffected == 1


def test_except_of_dropped_column_breaks():
    p = _pipeline(m="SELECT * EXCEPT (a) FROM `p.d.raw`")
    assert _names(p.assess_schema_change("drop_column", "p.d.raw", "a").breaks) == ["m"]


def test_join_alias_survives_and_new_shared_column_makes_bare_name_ambiguous():
    p = _pipeline(other="SELECT b AS k, a AS only_there FROM `p.d.raw`", j="SELECT only_there FROM `p.d.raw` AS x JOIN `p.d.other` AS y ON x.b = y.k")
    r = p.assess_schema_change("rename_column", "p.d.raw", "c", new_name="only_there")
    assert _names(r.breaks) == ["j"]


def test_union_arm_width_change_breaks():
    p = _pipeline(u="SELECT * FROM `p.d.raw` UNION ALL SELECT * FROM `p.d.raw`", t="SELECT a, b, c FROM `p.d.raw` UNION ALL SELECT * FROM `p.d.raw`")
    r = p.assess_schema_change("add_column", "p.d.raw", "z")
    assert _names(r.breaks) == ["t"] and _names(r.output_changes) == ["u"]


def test_retype_reaches_output_without_breaking():
    p = _pipeline(m="SELECT a + 1 AS x, b FROM `p.d.raw`")
    r = p.assess_schema_change("retype_column", "p.d.raw", "a", new_type="FLOAT64")
    assert not r.breaks and _names(r.output_changes) == ["m"] and r.output_changes[0].retyped == ("x",)


def test_unparsed_model_naming_the_table_is_unknown_never_safe():
    p = _pipeline(bad="SELECT FROM WHERE `p.d.raw` (((")
    r = p.assess_schema_change("drop_column", "p.d.raw", "a")
    assert _names(r.unknown) == ["bad"] and not r.complete


def test_unknown_columns_are_unknown_and_input_is_validated():
    p = _pipeline(m="SELECT * FROM `p.d.raw`")
    with pytest.raises(ValueError):
        p.assess_schema_change("drop_column", "p.d.raw", "missing")
    with pytest.raises(ValueError):
        p.assess_schema_change("explode", "p.d.raw", "a")


def test_generated_suites_are_safe_and_exact():
    bench = _bench()
    for families in (bench.DEV_FAMILIES + bench.SPECIAL, bench.HELD_OUT_FAMILIES):
        r = bench.run_suite(families)
        assert r["unsafe_misses"] == 0 and r["false_breaks"] == 0 and r["opaque_unsafe"] == 0, r["details"][:5]
        assert r["breaks_recall"] == 1.0 and r["changes_recall"] == 1.0
        assert r["breaks_precision"] == 1.0 and r["changes_precision"] == 1.0
        assert r["details_exact"] == r["details_total"]
