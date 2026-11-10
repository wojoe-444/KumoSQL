"""``kumosql.<moved module>`` is the same module object as ``kumo_sql_prover.<moved module>``."""

import importlib
import pkgutil

import pytest

import kumo_sql_prover
import kumosql
from kumosql import _prover_aliases


def test_the_alias_list_is_what_the_installed_package_contains():
    installed = {m.name for m in pkgutil.iter_modules(kumo_sql_prover.__path__)} - {"__main__"}
    assert installed == set(_prover_aliases.MOVED), "regenerate MOVED: python tools/prover_split.py --list"


@pytest.mark.parametrize("name", ["equivalence", "ast_utils", "smt_equivalence", "sqlx", "rule_driver", "uexpr", "uexpr.canon"])
def test_old_import_paths_resolve_to_the_same_module_object(name):
    assert importlib.import_module(f"kumosql.{name}") is importlib.import_module(f"kumo_sql_prover.{name}")


def test_from_imports_and_package_attributes_work():
    from kumosql import ast_utils
    from kumosql.equivalence import prove_equivalent

    import kumo_sql_prover.equivalence as equivalence

    assert ast_utils is importlib.import_module("kumo_sql_prover.ast_utils")
    assert prove_equivalent is equivalence.prove_equivalent is kumosql.prove_equivalent


def test_patching_either_spelling_patches_the_module(monkeypatch):
    import kumo_sql_prover.ast_utils as new

    monkeypatch.setattr("kumosql.ast_utils.parse_statements", lambda sql, **kw: "patched")
    assert new.parse_statements("SELECT 1") == "patched"


def test_the_function_keeps_the_package_attribute():
    """``kumosql.lift_subqueries`` is the function, as it always was, even after the submodule is imported by its old path."""

    importlib.import_module("kumosql.lift_subqueries")
    assert callable(kumosql.lift_subqueries)
    assert kumosql.lift_subqueries is kumo_sql_prover.lift_subqueries


def test_modules_that_stayed_are_not_aliased():
    assert importlib.import_module("kumosql.pipeline").__name__ == "kumosql.pipeline"
    assert importlib.import_module("kumosql.engine").__name__ == "kumosql.engine"
    assert "pipeline" not in _prover_aliases.MOVED
    assert "engine" not in _prover_aliases.MOVED
