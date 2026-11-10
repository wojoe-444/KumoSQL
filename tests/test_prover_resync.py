"""tools/prover_resync.py maps a KumoSQL change to the prover repository: which paths, and the renamed names."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("prover_resync", ROOT / "tools" / "prover_resync.py")
resync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resync)

NAMES = frozenset({"equivalence", "smt_equivalence", "ast_utils"})
EXPORTS = frozenset({"prove_equivalent"})


def test_a_moved_module_maps_to_the_package_and_a_product_module_does_not():
    assert resync.target("src/kumosql/smt_equivalence.py", NAMES) == "src/kumo_sql_prover/smt_equivalence.py"
    assert resync.target("src/kumosql/uexpr/translate.py", frozenset({"uexpr"})) == "src/kumo_sql_prover/uexpr/translate.py"
    assert resync.target("src/kumosql/rewrite.py", NAMES) is None
    assert resync.target("tests/test_x.py", NAMES) == "tests/test_x.py" and resync.target("README.md", NAMES) is None


def test_the_rename_follows_moved_names_only():
    rename = resync.renamer(NAMES, EXPORTS)
    text = "from kumosql import prove_equivalent\nimport kumosql.ast_utils\nfrom kumosql.rewrite import apply_rule\nsrc/kumosql/equivalence.py src/kumosql/rewrite.py\n"
    assert rename(text) == "from kumo_sql_prover import prove_equivalent\nimport kumo_sql_prover.ast_utils\nfrom kumosql.rewrite import apply_rule\nsrc/kumo_sql_prover/equivalence.py src/kumosql/rewrite.py\n"


def test_the_moved_names_are_the_installed_prover_s():
    names, exports = resync.moved_names()
    assert "smt_equivalence" in names and "prove_equivalent" in exports
