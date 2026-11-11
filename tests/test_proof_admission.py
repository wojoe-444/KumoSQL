"""The admission ledger (tools/proof_admission.py) and the near-miss probe (tools/proof_mutants.py).

The ledgers and the results files they are compared with live in the prover repository, whose gate runs the
comparison (its ``tests/test_proof_ledger.py``). The tests here hold this tool to never comparing against
nothing: it needs a prover checkout, and a ledger without a results file is a failure, not a skip.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import proof_admission as A  # noqa: E402
import proof_mutants as M  # noqa: E402


def _checkout(root: Path) -> Path:
    (root / "benchmarks" / "results").mkdir(parents=True)
    (root / "benchmarks" / "admitted").mkdir()
    (root / "src" / "kumo_sql_prover").mkdir(parents=True)
    return root


def test_the_prover_checkout_is_named_never_guessed(tmp_path):
    root = _checkout(tmp_path / "prover")
    assert A.prover_checkout(root, environ={}) == root.resolve()
    assert A.prover_checkout(None, environ={A.PROVER_ENV: str(root)}) == root.resolve()
    assert A.prover_checkout(root, environ={A.PROVER_ENV: str(tmp_path / "other")}) == root.resolve()  # the option wins
    with pytest.raises(A.NoProverCheckout, match="--prover"):
        A.prover_checkout(None, environ={})
    with pytest.raises(A.NoProverCheckout, match="not a checkout"):
        A.prover_checkout(tmp_path, environ={})  # a folder without the results files
    with pytest.raises(A.NoProverCheckout, match="not a checkout"):
        A.prover_checkout(Path(__file__).resolve().parent.parent, environ={})  # this repository no longer holds them


@pytest.mark.parametrize("command", ["status", "check", "admit"])
def test_no_command_runs_without_a_prover_checkout(command, monkeypatch, capsys):
    monkeypatch.delenv(A.PROVER_ENV, raising=False)
    assert A.main([command] + (["demo"] if command == "admit" else [])) == 2
    assert "--prover" in capsys.readouterr().err


def test_the_command_line_check_reads_the_named_checkout(tmp_path, monkeypatch, capsys):
    root = _checkout(tmp_path / "prover")
    A.write_ledger("demo", {"p1": ("0" * 12, "checked")}, root / "benchmarks" / "admitted")
    monkeypatch.setenv(A.PROVER_ENV, str(root))
    assert A.main(["check"]) == 1  # the ledger has no results file
    assert "has no results file" in capsys.readouterr().out
    _results(root / "benchmarks" / "results", "demo", coverage={"proven": 1})
    assert A.main(["check"]) == 0 and "1 ledgers match" in capsys.readouterr().out
    assert A.main(["status", "--prover", str(root)]) == 0 and "results file counts 1" in capsys.readouterr().out
    _results(root / "benchmarks" / "results", "demo", coverage={"proven": 2})
    assert A.main(["check", "--prover", str(root)]) == 1


def test_admit_refuses_a_prover_other_than_the_checkout(tmp_path, capsys):
    root = _checkout(tmp_path / "prover")  # holds no prover code, so the installed prover cannot be this one
    assert A.installed_prover_is(root)[0] is False
    assert A.main(["admit", "sqlsolver-tpcc", "--prover", str(root), "--dry-run"]) == 2
    assert "is not the checkout" in capsys.readouterr().err
    assert not list((root / "benchmarks" / "admitted").iterdir())


def test_converted_pairs_are_read_from_the_named_prover_checkout(tmp_path, monkeypatch):
    from recheck import calcite_family as F

    root = tmp_path / "prover"
    qed, mined = root / "tests" / "fixtures" / "qed", root / "tests" / "fixtures" / "calcite_mined"
    qed.mkdir(parents=True)
    mined.mkdir(parents=True)
    row = {"sql_a": "SELECT 1", "sql_b": "SELECT 1"}
    (qed / "qed_calcite_pairs.jsonl").write_text(json.dumps({"name": "onlyThere", "ddl": "", **row}) + "\n", encoding="utf-8")
    (mined / "schemas.json").write_text(json.dumps({"s0": {"ddl": ""}}), encoding="utf-8")
    (mined / "pairs.jsonl").write_text(json.dumps({"name": "minedThere", "schema_id": "s0", "new": True, **row}) + "\n", encoding="utf-8")

    assert F.PROVER_ENV == A.PROVER_ENV
    assert F.converted_fixtures({F.PROVER_ENV: str(root)}) == root / "tests" / "fixtures"
    assert F.converted_fixtures({}) == F.FIXTURES
    monkeypatch.setenv(F.PROVER_ENV, str(root))
    assert [item["pair"] for item in F.Qed().items()] == ["onlyThere"]
    assert [(item["pair"], item["held_out"]) for item in F.Mined().items()] == [("minedThere", True)]
    monkeypatch.setenv(F.PROVER_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(FileNotFoundError):  # a named checkout is never replaced by this repository's copy
        F.Qed().items()
    monkeypatch.delenv(F.PROVER_ENV)
    assert len(F.Qed().items()) > 300 and len(F.Mined().items()) > 400  # this repository's copy


def test_admit_tells_the_adapters_which_checkout_it_was_given(tmp_path, monkeypatch, capsys):
    import os

    root = _checkout(tmp_path / "prover")
    seen = []

    class Demo:
        def items(self):
            seen.append(os.environ.get(A.PROVER_ENV))
            return []

    monkeypatch.setenv(A.PROVER_ENV, str(tmp_path / "elsewhere"))  # the option wins, and the test restores the variable
    monkeypatch.setattr(A, "installed_prover_is", lambda root: (True, str(root)))
    monkeypatch.setattr(A, "_adapters", lambda: {"demo": Demo()})

    class InProcess:  # no fork from the test process
        imap_unordered = staticmethod(map)

        def get_context(self, _method):
            return self

        def Pool(self, *_args, **_kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(A, "multiprocessing", InProcess())
    assert A.main(["admit", "demo", "--prover", str(root), "--dry-run", "--jobs", "1"]) == 0
    assert seen == [str(root.resolve())]
    assert "demo: 0 kept" in capsys.readouterr().out


def test_the_ledgers_are_not_kept_in_this_repository():
    here = Path(__file__).resolve().parent.parent / "benchmarks"
    assert not (here / "admitted").exists(), "the ledgers live in the prover repository, next to the results files they are compared with"


def _results(folder: Path, name: str, **fields) -> None:
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(fields), encoding="utf-8")


def test_check_compares_the_count_with_the_ledger(tmp_path):
    ledgers, results = tmp_path / "admitted", tmp_path / "results"
    A.write_ledger("demo", {"p1": ("0" * 12, "checked"), "p2": ("1" * 12, "baseline")}, ledgers)
    _results(results, "demo", coverage={"proven": 2}, score="2/3, 0 wrong")
    assert A.check(ledgers, results) == []
    _results(results, "demo", coverage={"proven": 3})
    (problem,) = A.check(ledgers, results)
    assert "counts 3 proofs and 2 are admitted" in problem and "admit demo" in problem
    _results(results, "demo", coverage={"proven": 1})  # a lost proof still listed leaves room for an unchecked one
    assert len(A.check(ledgers, results)) == 1
    _results(results, "demo", score="1,003/1,100 proved, 0 wrong")  # no coverage block: the number that opens the score
    assert A.counted("demo", results) == 1003 and len(A.check(ledgers, results)) == 1


def test_a_ledger_with_nothing_to_be_compared_with_fails_the_check(tmp_path):
    ledgers, results = tmp_path / "admitted", tmp_path / "results"
    A.write_ledger("demo", {"p1": ("0" * 12, "checked")}, ledgers)
    results.mkdir()
    (problem,) = A.check(ledgers, results)  # no results file: reported, where it used to be skipped
    assert "has no results file" in problem
    _results(results, "demo", score="not measured")
    (problem,) = A.check(ledgers, results)
    assert "gives no proven count" in problem
    _results(results, "demo", coverage={"proven": True})  # a flag is not a count
    assert len(A.check(ledgers, results)) == 1


def test_a_damaged_ledger_is_an_error(tmp_path):
    (tmp_path / "demo.txt").write_text("# header\n000000000000 checked p1\nnot-a-digest checked p2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="demo.txt:3"):
        A.read_ledger("demo", tmp_path)
    (tmp_path / "demo.txt").write_text("000000000000 checked p1\n111111111111 checked p1\n", encoding="utf-8")
    with pytest.raises(ValueError):  # one pair twice
        A.read_ledger("demo", tmp_path)
    (tmp_path / "demo.txt").write_text("000000000000 trusted p1\n", encoding="utf-8")
    with pytest.raises(ValueError):  # not one of the ways a proof is admitted
        A.read_ledger("demo", tmp_path)


def test_a_pair_name_may_hold_spaces(tmp_path):
    A.write_ledger("demo", {"case 7 (b)": ("a" * 12, "searched")}, tmp_path)
    assert A.read_ledger("demo", tmp_path) == {"case 7 (b)": ("a" * 12, "searched")}


ITEMS = [{"pair": name, "left": f"SELECT {name}", "right": f"SELECT {name} + 0"} for name in ("kept", "new", "false", "lost", "open", "slow")]
OPTIONS = {"prove_seconds": 1}


def _prove(job):
    _name, item, _options = job
    return item["pair"], item["pair"] != "lost"


def _examine(job):
    _name, item, _options = job
    verdicts = {
        "new": {"verdict": "admitted", "how": "checked"},
        "false": {"verdict": "differs", "why": "a database separates the two queries"},
        "slow": {"verdict": "unsettled", "why": "executed search: timeout"},
        "open": {"verdict": "not-proven"},
    }
    return {"pair": item["pair"], **verdicts[item["pair"]]}


def _seed(folder):
    digests = {item["pair"]: A.digest("demo", item) for item in ITEMS}
    A.write_ledger("demo", {"kept": (digests["kept"], "baseline"), "lost": (digests["lost"], "baseline")}, folder)
    return digests


def test_admit_adds_checked_proofs_and_drops_lost_ones(tmp_path):
    digests = _seed(tmp_path)
    report = A.admit("demo", ITEMS, OPTIONS, folder=tmp_path, prove=_prove, examine_job=_examine)
    assert report["kept"] == ["kept"] and report["admitted"] == ["new"] and report["lost"] == ["lost"]
    assert [r["pair"] for r in report["differs"]] == ["false"] and [r["pair"] for r in report["unsettled"]] == ["slow"]
    # the false proof, the unsettled one and the pair that is no longer proven are not in the ledger
    assert A.read_ledger("demo", tmp_path) == {"kept": (digests["kept"], "baseline"), "new": (digests["new"], "checked")}


def test_a_pair_whose_text_changed_is_checked_again(tmp_path):
    _seed(tmp_path)
    changed = [dict(item, right="SELECT 1") if item["pair"] == "kept" else item for item in ITEMS]
    seen = []

    def examine(job):
        seen.append(job[1]["pair"])
        return {"pair": job[1]["pair"], "verdict": "admitted", "how": "checked"}

    report = A.admit("demo", changed, OPTIONS, folder=tmp_path, prove=_prove, examine_job=examine)
    assert "kept" in seen and "kept" in report["admitted"]
    assert A.read_ledger("demo", tmp_path)["kept"] == (A.digest("demo", changed[0]), "checked")


def test_dry_run_reports_and_writes_nothing(tmp_path):
    _seed(tmp_path)
    before = (tmp_path / "demo.txt").read_text(encoding="utf-8")
    report = A.admit("demo", ITEMS, {**OPTIONS, "dry_run": True}, folder=tmp_path, prove=_prove, examine_job=_examine)
    assert report["unadmitted"] == ["false", "new", "open", "slow"] and report["lost"] == ["lost"] and report["admitted"] == []
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == before


def test_baseline_admits_without_the_checks(tmp_path):
    report = A.admit("demo", ITEMS, {**OPTIONS, "baseline": True}, folder=tmp_path, prove=_prove, examine_job=None)
    assert len(report["admitted"]) == 5
    assert {how for _, how in A.read_ledger("demo", tmp_path).values()} == {"baseline"}


class _Adapter:
    mutable = "mysql"


def _options():
    return {
        "budget": 10, "seconds": 1, "prove_seconds": 1, "seed": 0, "exhaustive_cap": 10, "per_pair": 4, "modes": ("right",),
        "mutant_budget": 10, "mutant_seconds": 1, "mutant_prove_seconds": 1,
    }


@pytest.mark.parametrize(
    "search, mutants, held_out, expected",
    [
        ("survived", [{"verdict": "survived", "mutant": "right:join_type@3"}], False, ("admitted", "checked")),
        ("survived", [{"verdict": "differs", "mutant": "right:join_type@3", "witness": {"t": []}}], False, ("differs", None)),
        ("differs", [], False, ("differs", None)),
        ("not-proven", [], False, ("not-proven", None)),
        ("timeout", [], False, ("unsettled", None)),
        ("unrunnable", [], False, ("admitted", "unrunnable")),
        ("survived", [{"verdict": "differs", "mutant": "right:join_type@3"}], True, ("admitted", "searched")),  # held-out: never mutated
    ],
)
def test_examine_runs_the_search_then_the_near_misses(search, mutants, held_out, expected):
    probed = []

    def probe(job):
        probed.append(job)
        return [{"summary": True, "pair": "p"}, *mutants]

    item = {"pair": "p", "left": "SELECT 1", "right": "SELECT 1", "held_out": held_out}
    result = A.examine("demo", item, _options(), adapter=_Adapter(), search=lambda job: {"verdict": search}, probe=probe)
    assert (result["verdict"], result.get("how")) == expected
    assert bool(probed) == (search == "survived" and not held_out)
    if expected[0] == "differs" and mutants:
        assert "right:join_type@3" in result["why"]


def test_an_eval_that_cannot_be_mutated_is_admitted_on_the_search_alone():
    class Fixed:
        mutable = ""

    result = A.examine("demo", {"pair": "p"}, _options(), adapter=Fixed(), search=lambda job: {"verdict": "survived"}, probe=lambda job: 1 / 0)
    assert (result["verdict"], result["how"]) == ("admitted", "searched")


def test_an_eval_that_scores_a_proven_pair_as_something_else_says_what_it_counts(monkeypatch):
    class Scored:  # proves every pair, but its eval refutes one first and scores it as different
        def case(self, item):
            return object()

        def counted(self, item):
            return item["pair"] == "counted"

    class Plain:
        def case(self, item):
            return object() if item["pair"] == "proven" else None

    monkeypatch.setattr(A, "_ADAPTERS", {"scored": Scored(), "plain": Plain()})
    assert A._prove(("scored", {"pair": "counted"}, OPTIONS)) == ("counted", True)
    assert A._prove(("scored", {"pair": "refuted first"}, OPTIONS)) == ("refuted first", False)
    assert A._prove(("plain", {"pair": "proven"}, OPTIONS)) == ("proven", True)
    assert A._prove(("plain", {"pair": "open"}, OPTIONS)) == ("open", False)


def test_a_proof_the_eval_itself_does_not_count_is_not_admitted():
    def search(job):
        return {"verdict": "survived", "meta": {"eval_verdict": "over-budget"}}

    result = A.examine("demo", {"pair": "p"}, _options(), adapter=_Adapter(), search=search, probe=lambda job: 1 / 0)
    assert result["verdict"] == "not-proven"


def test_the_verieql_suites_are_listed_under_their_results_files():
    pytest.importorskip("z3")
    pytest.importorskip("duckdb")
    from recheck import verieql

    for suite in ("calcite", "literature"):
        adapter = verieql.ADAPTERS[f"verieql-{suite}-proof"]
        assert adapter.suites == (suite,) and not M.supported(adapter)
        assert adapter.name == f"verieql-{suite}-proof"  # the name of its results file and ledger in the prover repository


# --- the near-miss probe -------------------------------------------------------------------------


def test_mutated_items_cover_each_side_and_name_the_mutation():
    pytest.importorskip("sqlglot")
    item = {"pair": "p", "left": "SELECT a FROM t WHERE a < 5", "right": "SELECT a FROM t WHERE 5 > a"}
    labelled = M.mutated_items(item, "mysql", ("right", "left", "self"), 0)
    modes = {label.split(":")[0] for label, _ in labelled}
    assert modes == {"right", "left", "self-left", "self-right"}
    for label, mutated in labelled:
        mode = label.split(":")[0]
        assert mutated["pair"] == "p" and (mutated["left"], mutated["right"]) != (item["left"], item["right"])
        if mode == "right":
            assert mutated["left"] == item["left"] and mutated["right"] != item["right"]
        if mode == "left":
            assert mutated["right"] == item["right"] and mutated["left"] != item["left"]
        if mode == "self-left":
            assert mutated["left"] == item["left"]
    assert any(":comparison_boundary@" in label for label, _ in labelled)


def test_mutated_items_respect_the_cap_and_unreadable_sql():
    item = {"pair": "p", "left": "SELECT a FROM t WHERE a < 5 AND a > 1 AND a <> 3", "right": "SELECT a FROM t WHERE a IN (2, 4)"}
    assert len(M.mutated_items(item, "mysql", ("right", "left"), 4)) <= 4
    assert M.mutated_items({"pair": "p", "left": "SELECT (", "right": "SELECT ("}, "mysql", ("right", "left", "self"), 0) == []


def test_spread_takes_every_operator_before_any_twice():
    class Mutant:
        def __init__(self, operator, site):
            self.operator, self.site = operator, site

    mutants = [Mutant("a", i) for i in range(10)] + [Mutant("b", 0), Mutant("c", 0)]
    picked = M.spread(mutants, 3)
    assert sorted(m.operator for m in picked) == ["a", "b", "c"]
    assert len(M.spread(mutants, 0)) == 12 and len(M.spread(mutants, 50)) == 12


def test_only_adapters_that_declare_a_dialect_are_probed():
    class Plain:
        mutable = ""

    assert not M.supported(Plain()) and M.supported(_Adapter()) and not M.supported(object())
    names = set(M.registry())
    assert {"qed-calcite", "sqlsolver-calcite", "rbot-calcite", "cosette"} <= names and "singh" not in names


def test_the_probe_catches_a_false_proof_of_a_near_miss(tmp_path, monkeypatch):
    """An adapter whose "prover" ignores the comparison operator: the probe must report the near miss it accepts."""

    pytest.importorskip("duckdb")
    from recheck.engine import Case, Column, Table

    class Careless:
        name, mutable = "careless", "mysql"

        def items(self):
            return [{"pair": "p", "left": "SELECT x FROM t WHERE x < 5", "right": "SELECT x FROM t WHERE 5 > x"}]

        def case(self, item):
            def shape(sql):
                return sql.replace("<=", "<").replace(">=", ">").replace("5 > x", "x < 5")

            if shape(item["left"]) != shape(item["right"]):
                return None
            return Case(self.name, item["pair"], item["left"], item["right"], {"t": Table("t", [Column("x", "int")])}, source=(item["left"], item["right"]))

    monkeypatch.setattr(M, "registry", lambda: {"careless": Careless()})
    monkeypatch.setattr(M, "_ADAPTERS", None)
    options = {"modes": ("right", "left"), "per_pair": 0, "budget": 300, "seconds": 20, "seed": 0, "exhaustive_cap": 300, "prove_seconds": 10}
    summary, *records = M._work(("careless", Careless().items()[0], options))
    assert summary["summary"] and summary["accepted"] >= 1
    assert any(r["verdict"] == "differs" and "comparison_boundary" in r["mutant"] for r in records)
