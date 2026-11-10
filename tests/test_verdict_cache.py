"""The proof verdict cache: a hit replays a verdict only for the same pair and the same prover source."""

import json

import pytest

from kumosql import verdict_cache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv(verdict_cache.ENV, str(tmp_path / "verdicts"))
    return tmp_path / "verdicts"


def test_off_by_default_never_writes(tmp_path, monkeypatch):
    monkeypatch.delenv(verdict_cache.ENV, raising=False)
    calls = []
    assert verdict_cache.cached_verdict("k", {"a": 1}, lambda: calls.append(1) or True) is True
    assert verdict_cache.cached_verdict("k", {"a": 1}, lambda: calls.append(1) or True) is True
    assert len(calls) == 2


def test_second_ask_is_a_hit_and_a_different_pair_is_a_miss(cache):
    calls = []

    def prove(answer):
        return lambda: calls.append(answer) or answer

    assert verdict_cache.cached_verdict("k", {"q": "a"}, prove(True)) is True
    assert verdict_cache.cached_verdict("k", {"q": "a"}, prove(False)) is True  # the cached answer, prove() not called
    assert verdict_cache.cached_verdict("k", {"q": "b"}, prove(False)) is False
    assert calls == [True, False]


def test_a_changed_prover_source_is_a_miss(cache, monkeypatch):
    assert verdict_cache.cached_verdict("k", {"q": 1}, lambda: True) is True
    monkeypatch.setattr(verdict_cache, "prover_fingerprint", lambda: "another prover")
    assert verdict_cache.cached_verdict("k", {"q": 1}, lambda: False) is False


def test_a_damaged_or_foreign_file_is_a_miss(cache):
    key = verdict_cache.key("k", {"q": 1})
    path = cache / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    for text in ("not json", json.dumps({"key": "other", "proven": True}), json.dumps({"key": key, "proven": "yes"}), "[]"):
        path.write_text(text, encoding="utf-8")
        assert verdict_cache.lookup(key) is None
        assert verdict_cache.cached_verdict("k", {"q": 1}, lambda: False) is False
        path.unlink()


def test_the_fingerprint_follows_the_provers_imports():
    modules = verdict_cache.prover_modules()
    assert {"smt_equivalence", "algebraic_equivalence", "ast_utils", "verdict_cache"} <= set(modules)
    assert "ui" not in modules and "live_graph" not in modules
    assert len(verdict_cache.prover_fingerprint()) == 64


def test_the_parser_patch_is_part_of_the_fingerprint():
    """``bigquery_syntax`` is imported by the package's ``__init__`` alone, yet every parse a proof makes goes through it."""

    assert "bigquery_syntax" in verdict_cache.prover_modules()


def test_imports_are_followed_from_every_file_of_a_subpackage_and_under_any_package_name(tmp_path):
    """The prover is also packaged on its own under another name, and a subpackage's files import from its parent."""

    package = tmp_path / "some_prover"
    (package / "sub").mkdir(parents=True)
    sources = {
        "__init__.py": "",
        "root.py": "from .sub import thing\nimport some_prover.by_absolute_import\nfrom some_prover import by_from_package\n",
        "sub/__init__.py": "from .inner import thing\n",
        "sub/inner.py": "def thing():\n    from ..by_parent_import import helper\n    from .. import by_bare_parent_import\n",
        "by_absolute_import.py": "from .second_hop import x\n",
        "by_from_package.py": "",
        "by_parent_import.py": "",
        "by_bare_parent_import.py": "",
        "second_hop.py": "",
        "unrelated.py": "import kumosql.ui\n",
        "ui.py": "",
    }
    for name, text in sources.items():
        (package / name).write_text(text, encoding="utf-8")
    assert verdict_cache.prover_modules(("root",), package) == [
        "by_absolute_import", "by_bare_parent_import", "by_from_package", "by_parent_import", "root", "second_hop", "sub",
    ]
    # an import of another distribution that happens to share a module name is not this package's module
    assert verdict_cache.prover_modules(("unrelated",), package) == ["unrelated"]


def test_a_source_digest_follows_the_file(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text("dialect = 'mysql'\n", encoding="utf-8")
    first = verdict_cache.source_digest(runner)
    assert first == verdict_cache.source_digest(runner) and len(first) == 64
    runner.write_text("dialect = 'bigquery'\n", encoding="utf-8")
    assert verdict_cache.source_digest(runner) != first
    assert verdict_cache.source_digest(tmp_path / "gone.py") == verdict_cache.source_digest(tmp_path / "also_gone.py")


@pytest.mark.canary  # loads the runner for one pair; the family's floors stay in tests/test_sqlsolver_benchmarks.py
def test_a_changed_benchmark_runner_is_a_miss(cache, monkeypatch):
    """``tools/sqlsolver_bench.prove_result`` picks the dialect and the options of the proof: editing it changes the answer."""

    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "tools" / "sqlsolver_bench.py"
    spec = importlib.util.spec_from_file_location("sqlsolver_bench_for_verdict_cache", path)
    bench = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, bench)
    spec.loader.exec_module(bench)
    answers = iter([True, False])

    class Answer:
        def __init__(self, proven):
            self.proven = proven

    monkeypatch.setattr(bench, "prove_result", lambda *args, **kwargs: Answer(next(answers)))
    assert bench.default_prove("SELECT 1", "SELECT 1", {}) is True
    assert bench.default_prove("SELECT 1", "SELECT 1", {}) is True  # a hit: the runner is unchanged
    monkeypatch.setattr(verdict_cache, "source_digest", lambda *paths: "an edited runner")
    assert bench.default_prove("SELECT 1", "SELECT 1", {}) is False  # proved again
