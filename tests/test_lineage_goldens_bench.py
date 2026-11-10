"""DataHub and OpenLineage lineage goldens (docs/evals/lineage-goldens-bench.md): harvest checks and floors.

Zero wrong and zero confident misses in scope; ``disputed`` stays the four documented unused-CTE cases.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "lineage_goldens"


@pytest.fixture(autouse=True)
def quiet_timing(monkeypatch):
    monkeypatch.setenv("KUMOSQL_TIMING", "0")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load("lineage_goldens_bench")
harvest = _load("lineage_goldens_harvest")


def test_fixtures_are_pinned_and_complete():
    datahub = json.loads((FIX / "datahub.json").read_text(encoding="utf-8"))
    openlineage = json.loads((FIX / "openlineage.json").read_text(encoding="utf-8"))
    assert datahub["commit"].startswith("8a117ba1f5") and len(datahub["cases"]) == 99 and len(datahub["unharvested"]) == 2
    assert openlineage["commit"].startswith("7a84fd4d48") and len(openlineage["cases"]) == 140 and len(openlineage["unharvested"]) == 18
    assert len({c["id"] for c in datahub["cases"]}) == 99 and len({c["id"] for c in openlineage["cases"]}) == 140


def test_rust_literals_are_read_without_guessing():
    source = r'''
#[test]
fn a() {
    let q = r#"select "x" from t"#;
    assert_eq!(
        test_sql(q).unwrap().table_lineage,
        TableLineage { in_tables: tables(vec!["t"]), out_tables: vec![] }
    );
}
#[ignore]
#[test]
fn b() {
    let output = test_sql("SELECT a FROM t1").unwrap();
    assert_eq!(output.column_lineage, vec![ColumnLineage {
        descendant: ColumnMeta { origin: None, name: "a".to_string() },
        lineage: vec![ColumnMeta { origin: Some(table("t1")), name: "a".to_string() }]
    }]);
}
'''
    tokens = harvest._lex(source)
    funcs = {name: (body, ignored) for name, body, ignored in harvest._functions(tokens)}
    assert set(funcs) == {"a", "b"} and funcs["b"][1] and not funcs["a"][1]
    assert harvest._call_args(funcs["a"][0]) == (['select "x" from t'], "postgres")
    parser = harvest._Parser(funcs["a"][0])
    parser.i = [v for _, v in funcs["a"][0]].index("TableLineage")
    assert parser.table_lineage() == (["t"], [])
    values = [v for _, v in funcs["b"][0]]
    parser = harvest._Parser(funcs["b"][0])
    parser.i = next(i for i, v in enumerate(values) if v == "vec!" and funcs["b"][0][i + 2][1] == "ColumnLineage")
    assert parser.column_lineages() == [((None, "a"), [("t1", "a")])]


@pytest.fixture(scope="module")
def result():
    return bench.run()


def test_openlineage_independent_oracle_floor(result):
    t = result["openlineage"]["in"]
    assert t["wrong"] == 0 and t["missed"] == 0, [r for r in result["rows"] if r["corpus"] == "openlineage" and r["scope"] == "in" and r["outcome"] in {"wrong", "missed"}]
    # sqlglot 26 rejects two original forms that 30 accepts; their fixtures remain unchanged and left out.
    assert t["total"] in {92, 94} and t["exact"] == t["total"] - 4 and t["disputed"] == len(bench.DISPUTED) == 4


def test_datahub_goldens_floor(result):
    t = result["datahub"]["in"]
    assert t["wrong"] == 0 and t["missed"] == 0, [r for r in result["rows"] if r["corpus"] == "datahub" and r["scope"] == "in" and r["outcome"] in {"wrong", "missed"}]
    assert t["total"] == 18 and t["exact"] + t["coarse"] + t["finer"] == 18
    # The three struct cases that used to meet only the root column now name the field; the one `finer` case reads a
    # field where the golden names the root column.
    assert t["coarse"] == 0 and t["exact"] >= 17 and t["finer"] <= 1


def test_every_disputed_case_really_disagrees_with_the_oracle(result):
    rows = {r["id"]: r for r in result["rows"]}
    assert {i for i, r in rows.items() if r["outcome"] == "disputed"} == set(bench.DISPUTED)


def test_other_dialects_are_reported_but_never_in_the_headline(result):
    assert result["datahub"]["dialects"]["total"] == 80 and result["openlineage"]["dialects"]["total"] == 29
    assert all(r["scope"] != "in" for r in result["rows"] if r["dialect"] not in bench.IN_SCOPE_DIALECTS[r["corpus"]])
