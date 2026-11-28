"""Audit-column proposals: a load timestamp that changed on every row while nothing else did is proposed, never applied.

Snapshots, fingerprint comparisons and reconcile runs are real DuckDB tables. The fingerprint SQL is BigQuery and runs
on DuckDB the way tests/test_fingerprint.py runs it (FARM_FINGERPRINT replaced by DuckDB's hash).
"""

import json

import pytest
import sqlglot

from kumosql import reconcile_cli
from kumosql.audit_columns import (
    AuditCandidate,
    confirm,
    propose_from_comparison,
    propose_from_reconcile,
    propose_from_rows,
    propose_from_snapshots,
    rows_by_key,
    suggestion,
    timestamp_like,
)
from kumosql.cli import compare_outputs_main
from kumosql.fingerprint import compare_tables_sql, join_snapshots, summarize_comparison, table_fingerprint_sql
from kumosql.reconcile import DuckDBExecutor, reconcile

duckdb = pytest.importorskip("duckdb")


def load(con, name, day, *, amounts=None, stamp_rows=None, n=5):
    """``n`` orders loaded on ``day``; with ``stamp_rows``, the other rows keep the load time of day 2."""

    amounts = amounts or {}
    con.execute(f"CREATE TABLE {name} (id INTEGER, amount DOUBLE, status VARCHAR, _loaded_at TIMESTAMP)")
    for i in range(1, n + 1):
        stamped = stamp_rows is None or i in stamp_rows
        loaded = f"TIMESTAMP '2026-11-{day if stamped else 2:02d} 03:00:00'"
        con.execute(f"INSERT INTO {name} VALUES ({i}, {amounts.get(i, i * 10.0)}, 'paid', {loaded})")


def snapshot(con, name):
    cursor = con.execute(f"SELECT * FROM {name}")
    names = [c[0] for c in cursor.description]
    types = {c[0]: str(c[1]) for c in cursor.description}
    return rows_by_key([dict(zip(names, row)) for row in cursor.fetchall()], ["id"]), types


@pytest.fixture
def con():
    connection = duckdb.connect()
    connection.execute("CREATE MACRO farm_fp(x) AS CAST(hash(x) AS HUGEINT)")
    yield connection
    connection.close()


# ------------------------------------------------------------------ row snapshots


def test_one_pair_of_snapshots_is_low_confidence(con):
    load(con, "s1", 2)
    load(con, "s2", 3)
    (first, types), (second, _) = snapshot(con, "s1"), snapshot(con, "s2")
    [candidate] = propose_from_rows(first, second, types)
    assert candidate.column == "_loaded_at" and candidate.confidence == "low" and candidate.pairs == 1
    assert candidate.evidence == "rows" and "every one of 5 rows" in candidate.reason
    assert candidate.applied is False and candidate.to_json()["applied"] is False
    assert candidate.to_json()["suggested"] == 'ignore_columns=["_loaded_at"]'


def test_two_consecutive_agreeing_pairs_are_high_confidence(con):
    for name, day in (("s1", 2), ("s2", 3), ("s3", 4)):
        load(con, name, day)
    snaps = [snapshot(con, name)[0] for name in ("s1", "s2", "s3")]
    types = snapshot(con, "s1")[1]
    [candidate] = propose_from_snapshots(snaps, types)
    assert candidate.confidence == "high" and candidate.pairs == 2 and "earlier pair" in candidate.reason
    # Only the last two pairs count: an earlier pair that disagrees leaves the proposal low.
    load(con, "s0", 1, amounts={3: 0.0})  # s0 -> s1 also changes an amount: that pair proposes nothing
    [low] = propose_from_snapshots([snapshot(con, "s0")[0], snaps[0], snaps[1]], types)
    assert low.confidence == "low"


def test_a_column_that_changed_on_only_some_rows_is_not_proposed(con):
    load(con, "s1", 2)
    load(con, "s2", 3, stamp_rows={1, 2, 3})
    (first, types), (second, _) = snapshot(con, "s1"), snapshot(con, "s2")
    assert propose_from_rows(first, second, types) == ()


def test_nothing_is_proposed_when_another_column_also_changed(con):
    load(con, "s1", 2)
    load(con, "s2", 3, amounts={4: 99.0})
    (first, types), (second, _) = snapshot(con, "s1"), snapshot(con, "s2")
    assert propose_from_rows(first, second, types) == ()
    # A non-timestamp column that changed on every row is a real difference, not a load time.
    con.execute("CREATE TABLE s3 AS SELECT id, amount + 1 AS amount, status, _loaded_at FROM s1")
    assert propose_from_rows(first, snapshot(con, "s3")[0], types) == ()
    # Different keys, or too few rows, propose nothing either.
    assert propose_from_rows(first, {**second, (99,): second[(1,)]}, types) == ()
    assert propose_from_rows({(1,): first[(1,)]}, {(1,): second[(1,)]}, types) == ()


def test_timestamp_like_uses_type_then_values_then_name():
    assert timestamp_like("anything", "TIMESTAMP WITH TIME ZONE") and timestamp_like("x", "DATETIME")
    assert not timestamp_like("_loaded_at", "BIGINT")  # a known non-time type is never proposed
    assert timestamp_like("etl_load_date", "DATE") and not timestamp_like("order_date", "DATE")
    assert timestamp_like("ingested_at") and timestamp_like("etl_updated_at") and not timestamp_like("amount")
    with pytest.raises(ValueError):
        suggestion(["x"], "elsewhere")


# ------------------------------------------------------------------ fingerprint comparisons


def rows(con, sql):
    duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
    duck = duck.replace("FARM_FINGERPRINT(", "farm_fp(").replace("AS BIGDECIMAL)", "AS DECIMAL(38, 5))")
    cursor = con.execute(duck)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


COLUMNS = ["id", "amount", "status", "_loaded_at"]


def test_a_comparison_proposes_from_checksums_and_a_second_run_confirms(con):
    for name, day in (("b1", 2), ("a1", 3), ("b2", 4), ("a2", 5)):
        load(con, name, day)
    # A pipeline comparison labels rows by model; two runs of it compare the same model.
    first = [{**row, "model": "orders"} for row in rows(con, compare_tables_sql("b1", "a1", COLUMNS))]
    [diff] = summarize_comparison(first)
    assert diff.status == "mismatch" and diff.mismatched_columns == ("_loaded_at",)
    [low] = propose_from_comparison(first)[diff.model]
    assert low.confidence == "low" and low.evidence == "checksums" and "cannot show that every row changed" in low.reason
    second = [{**row, "model": "orders"} for row in rows(con, compare_tables_sql("b2", "a2", COLUMNS))]
    [high] = confirm(propose_from_comparison(first), propose_from_comparison(second))[diff.model]
    assert high.confidence == "high" and high.pairs == 2
    # Another column differing too: no proposal for that model.
    load(con, "a3", 3, amounts={2: 0.0})
    assert propose_from_comparison(rows(con, compare_tables_sql("b1", "a3", COLUMNS))) == {}


def test_fingerprint_snapshots_and_the_summarize_command(con, tmp_path, capsys):
    for name, day in (("t0", 1), ("t1", 2), ("t2", 3)):
        load(con, name, day)
    paths = []
    for name in ("t0", "t1", "t2"):
        path = tmp_path / f"{name}.json"
        fingerprint = [{**row, "model": "orders"} for row in rows(con, table_fingerprint_sql(name, COLUMNS))]
        path.write_text(json.dumps(fingerprint, default=str), encoding="utf-8")
        paths.append(str(path))
    joined = join_snapshots(json.loads(open(paths[1]).read()), json.loads(open(paths[2]).read()))
    assert list(propose_from_comparison(joined)) == ["orders"]
    # One pair: a low-confidence suggestion, and the comparison itself is unchanged (still a mismatch, exit 1).
    assert compare_outputs_main(["summarize", paths[1], paths[2]]) == 1
    out = capsys.readouterr().out
    assert "orders: mismatch (values differ in _loaded_at)" in out
    assert "suggestion: orders: _loaded_at looks like an audit column (low confidence" in out
    assert "--ignore-column _loaded_at" in out and "Nothing was applied" in out
    # The snapshot before the first one makes the two pairs consecutive: high confidence.
    assert compare_outputs_main(["summarize", paths[1], paths[2], "--previous", paths[0]]) == 1
    assert "(high confidence, two runs agree)" in capsys.readouterr().out


# ------------------------------------------------------------------ reconcile


def test_reconcile_suggests_ignore_in_its_result_and_applies_nothing(con, tmp_path, capsys):
    load(con, "orders_v1", 2, n=40)
    load(con, "orders_v2", 9, amounts={5: 51.0}, n=40)  # one real difference in 40 keys, under the 5% the rule allows
    result = reconcile("orders_v1", "orders_v2", "id", DuckDBExecutor(con))
    data = result.to_json()
    [suggested] = data["audit_column_suggestions"]
    assert suggested["column"] == "_loaded_at" and suggested["confidence"] == "low" and suggested["evidence"] == "counts"
    assert suggested["suggested"] == "--ignore _loaded_at" and suggested["applied"] is False
    assert "known-difference rule would explain whole rows" in suggested["reason"]
    # Nothing was applied: the column is still compared and every row still counts as different.
    assert "_loaded_at" in data["compared_columns"] and data["ignored_columns"] == []
    assert data["column_mismatches"]["_loaded_at"] == 40 and data["unexplained_rows"] == 40
    # Following the suggestion by hand leaves the one real difference.
    assert reconcile("orders_v1", "orders_v2", "id", DuckDBExecutor(con), ignore="_loaded_at").unexplained_rows == 1
    # The same suggestion through the command, on a DuckDB file.
    path = tmp_path / "w.duckdb"
    disk = duckdb.connect(str(path))
    load(disk, "orders_v1", 2)
    load(disk, "orders_v2", 9)
    disk.close()
    assert reconcile_cli.reconcile_main(["--left", "orders_v1", "--right", "orders_v2", "--key", "id", "--duckdb", str(path)]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert [s["column"] for s in printed["audit_column_suggestions"]] == ["_loaded_at"]


def test_reconcile_proposes_nothing_for_partial_or_mixed_differences(con):
    load(con, "l1", 2, n=40)
    load(con, "r1", 9, stamp_rows=set(range(1, 31)), n=40)  # the load time differs on 30 of 40 keys only
    assert reconcile("l1", "r1", "id", DuckDBExecutor(con)).audit_column_suggestions == []
    load(con, "r2", 9, amounts={1: 0.0, 2: 0.0, 3: 0.0}, n=40)  # differs everywhere, but so do 3 amounts (7.5%)
    assert reconcile("l1", "r2", "id", DuckDBExecutor(con)).audit_column_suggestions == []
    # The rule on counts alone: a non-timestamp column is never proposed, and the share is a parameter.
    assert propose_from_reconcile([("batch", "BIGINT")], {"batch": 100}, 100) == []
    assert propose_from_reconcile([("_loaded_at", "TIMESTAMP")], {"_loaded_at": 96}, 100)[0]["column"] == "_loaded_at"
    assert propose_from_reconcile([("_loaded_at", "TIMESTAMP")], {"_loaded_at": 96}, 100, share=0.99) == []
    assert AuditCandidate("x", "low", "counts", "r").applied is False
