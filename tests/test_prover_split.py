"""The prover's import closure stays free of the product (step 8 of docs/roadmap.md)."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import prover_split  # noqa: E402

SOURCE = Path(__file__).resolve().parent.parent / "src" / "kumosql"


def test_prover_closure_reaches_no_product_module():
    result = prover_split.analyse(SOURCE, "kumosql")
    assert result["forbidden"] == {}, result["forbidden"]


def test_scripting_and_the_rule_registry_stay_out_of_the_prover():
    result = prover_split.analyse(SOURCE, "kumosql")
    assert "scripts" not in result["modules"]
    assert "engine" not in result["modules"]
    # the driver the prover's lift uses is in the closure
    assert "rule_driver" in result["modules"]


def test_closure_is_bounded_by_the_roots_it_is_computed_from():
    result = prover_split.analyse(SOURCE, "kumosql")
    assert set(prover_split.ROOTS) <= set(result["modules"])
    assert 100 < len(result["modules"]) < 160


def test_check_exits_zero_on_the_source_tree(capsys):
    assert prover_split.main(["--check"]) == 0
    assert "prover closure:" in capsys.readouterr().out
