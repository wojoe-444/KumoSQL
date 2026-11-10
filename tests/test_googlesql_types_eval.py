"""GoogleSQL type inference: the dev split of the compliance-test labels must never be wrong, and its exact count only grows.

``python tools/googlesql_types_eval.py`` prints the full score. Each labelled output column of a compliance query is
exact (KumoSQL's type text equals the label), unknown (KumoSQL gave no type) or WRONG (a different type or width); WRONG
must stay at 0 because the typer answers "unknown" whenever GoogleSQL's rules do not fix a type. The held-out split is
never scored here: only its case count is pinned, so a changed fixture shows up without anyone reading its cases.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.eval

SPEC = importlib.util.spec_from_file_location(
    "googlesql_types_eval", Path(__file__).parent.parent / "tools" / "googlesql_types_eval.py"
)
ev = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ev  # dataclasses and typing look their module up here
SPEC.loader.exec_module(ev)

# BigQuery-typed dev columns whose type KumoSQL gives exactly. Raise it with the score, never lower it.
FLOOR_EXACT = 14755
# sqlglot 26.0.0, the oldest the project allows, cannot parse BY NAME / CORRESPONDING modes, ARRAY_ZIP's named
# arguments, GRAPH_TABLE and aggregate WHERE filters; those queries are unknown there, so its floor is lower. Still 0 wrong.
FLOOR_EXACT_OLD_SQLGLOT = 13364
# sqlglotc (the compiled sqlglot) raises a TypeError parsing `value.(pkg.extension)`, a PROTO extension access; those
# four queries have no types there, 3 exact BigQuery columns fewer than the interpreted parser gives.
COMPILED_SQLGLOT_SHORTFALL = 3
# Cases in each split: 285 compliance files, a file held out when sha256(stem) % 4 == 0 (see the eval's SPLIT_RULE).
DEV_CASES = 7878
HELDOUT_CASES = 2602


@pytest.fixture(scope="module")
def dev_counts():
    return ev.score(ev.load(ev.DEV))


def test_dev_split_has_no_wrong_column(dev_counts):
    assert dev_counts.get("wrong", 0) == 0
    assert dev_counts.get("bigquery wrong", 0) == 0


def test_dev_split_never_crashes_the_typer(dev_counts):
    assert dev_counts.get("crashed queries", 0) == 0
    assert dev_counts["queries"] == DEV_CASES


def _sqlglot_parses_set_operation_modes() -> bool:
    import sqlglot

    try:
        sqlglot.parse_one("SELECT 1 AS x INNER UNION ALL BY NAME SELECT 1 AS x", read="bigquery")
        return True
    except Exception:  # noqa: BLE001
        return False


def _sqlglot_is_compiled() -> bool:
    import sqlglot.parser

    return str(getattr(sqlglot.parser, "__file__", "")).endswith((".so", ".pyd"))


def test_dev_split_exact_floor(dev_counts):
    exact = dev_counts.get("bigquery exact", 0)
    floor = FLOOR_EXACT if _sqlglot_parses_set_operation_modes() else FLOOR_EXACT_OLD_SQLGLOT
    if _sqlglot_is_compiled():
        floor -= COMPILED_SQLGLOT_SHORTFALL
    assert exact >= floor, f"{exact} BigQuery columns exact, floor {floor}"
    # a column is exact, unknown or wrong, so the three account for every labelled one
    labelled = dev_counts["labelled columns"]
    assert dev_counts.get("exact", 0) + dev_counts.get("unknown", 0) + dev_counts.get("wrong", 0) == labelled
    assert dev_counts["bigquery columns"] <= labelled


def test_split_rule_and_case_counts_are_unchanged():
    """Only counts are read from the held-out file, never its cases."""

    dev = ev.load(ev.DEV)
    assert len(dev["cases"]) == DEV_CASES
    assert all(not ev.held_out(case["file"]) for case in dev["cases"])
    assert len(ev.load(ev.HELDOUT)["cases"]) == HELDOUT_CASES
