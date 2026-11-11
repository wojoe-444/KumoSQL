"""The prover stays a separate package: its import closure reaches nothing of the product and never imports it
(step 8 of docs/roadmap.md). Before the split the same tests guarded the cut of the scripting edge."""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import prover_split  # noqa: E402

PACKAGE, SOURCE = prover_split.locate()


def test_prover_closure_reaches_no_product_module():
    result = prover_split.analyse(SOURCE, PACKAGE)
    assert result["forbidden"] == {}, result["forbidden"]


def test_the_prover_never_imports_kumosql():
    result = prover_split.analyse(SOURCE, PACKAGE)
    assert result["imports_kumosql"] == {}, result["imports_kumosql"]


def test_scripting_and_the_rule_registry_stay_out_of_the_prover():
    modules = prover_split.analyse(SOURCE, PACKAGE)["modules"]
    assert "scripts" not in modules
    assert "engine" not in modules
    # the driver the prover's lift uses is in the closure
    assert "rule_driver" in modules


def test_closure_is_bounded_by_the_roots_it_is_computed_from():
    modules = prover_split.analyse(SOURCE, PACKAGE)["modules"]
    assert set(prover_split.ROOTS) <= set(modules)
    assert 100 < len(modules) < 200  # the prover grows (163 at pin e380238); a closure of everything would be far larger


def test_check_exits_zero_on_the_source_tree(capsys):
    assert prover_split.main(["--check"]) == 0
    assert "prover closure:" in capsys.readouterr().out
