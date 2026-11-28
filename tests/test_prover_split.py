"""The prover stays a separate package: its import closure reaches nothing of the product and never imports it
(step 8 of docs/roadmap.md). Before the split the same tests guarded the cut of the scripting edge."""

import pkgutil
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import prover_split  # noqa: E402

PACKAGE, SOURCE = prover_split.locate()


@pytest.fixture(scope="module")
def result():
    """One analysis for the file: it parses every module of the prover, which takes long on a slow machine."""

    return prover_split.analyse(SOURCE, PACKAGE)


def test_prover_closure_reaches_no_product_module(result):
    assert result["forbidden"] == {}, result["forbidden"]


def test_the_prover_never_imports_kumosql(result):
    assert result["imports_kumosql"] == {}, result["imports_kumosql"]


def test_scripting_and_the_rule_registry_stay_out_of_the_prover(result):
    modules = result["modules"]
    assert "scripts" not in modules
    assert "engine" not in modules
    # the driver the prover's lift uses is in the closure
    assert "rule_driver" in modules


def test_closure_is_bounded_by_the_roots_it_is_computed_from(result):
    modules = result["modules"]
    assert set(prover_split.ROOTS) <= set(modules)
    # The closure is the prover itself: about as many modules as the package holds at the top level (a few more for
    # the ones inside sub-packages), so it grows with the prover. Far more would mean it reaches into the product.
    top_level = len(list(pkgutil.iter_modules([str(SOURCE)])))
    assert 100 < len(modules) <= top_level + 10, (len(modules), top_level)


def test_check_exits_zero_on_the_source_tree(capsys):
    assert prover_split.main(["--check"]) == 0
    assert "prover closure:" in capsys.readouterr().out
