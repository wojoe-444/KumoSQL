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
