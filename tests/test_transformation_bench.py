"""Checks on the TPC-H / TPC-DS / JOB transformation benchmark harness (no benchmark data needed)."""

from __future__ import annotations

import importlib.util

import pytest

from kumosql.result_equivalence import check_result_equivalence
from tools import transformation_bench as bench

pytestmark = pytest.mark.eval

duckdb_needed = pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb is required for result equivalence")

QUERY = (
    "SELECT MIN(t.title) AS movie, MIN(n.name) AS actor "
    "FROM title AS t, cast_info AS ci, name AS n "
    "WHERE ci.movie_id = t.id AND ci.person_id = n.id AND t.production_year > 2000 AND n.gender = 'f'"
)
SCHEMA = {
    "title": {"id": "INT64", "title": "STRING", "production_year": "INT64"},
    "cast_info": {"movie_id": "INT64", "person_id": "INT64"},
    "name": {"id": "INT64", "name": "STRING", "gender": "STRING"},
}


def test_nan_rows_compare_equal():
    # DuckDB returns NaN for some TPC-DS aggregates; NaN != NaN once reported query66 as a wrong rewrite.
    assert bench._bag([(float("nan"), 1.0)]) == bench._bag([(float("nan"), 1.0)])
    assert bench._bag([(0.1 + 0.2,)]) == bench._bag([(0.3,)])


@duckdb_needed
@pytest.mark.parametrize("form", sorted(bench.FORMS))
def test_each_job_form_changes_the_query_and_keeps_its_results(form):
    alternative = bench.FORMS[form](QUERY, SCHEMA)
    assert alternative and alternative != QUERY
    result = check_result_equivalence(QUERY, alternative, SCHEMA, seeds=(1, 2), rows_per_table=8)
    assert result.equivalent, (form, alternative, result.reason)


def test_explicit_joins_moves_join_predicates_into_on():
    alternative = bench.form_explicit_joins(QUERY)
    assert "JOIN cast_info AS ci ON ci.movie_id = t.id" in alternative
    assert "WHERE t.production_year > 2000 AND n.gender = 'f'" in alternative


def test_broken_forms_change_results():
    for build in bench.BROKEN_FORMS.values():
        broken = build(QUERY, SCHEMA)
        assert broken and broken != QUERY
        assert bench._prove(QUERY, broken, SCHEMA)[0] != "proven_equivalent", broken


def test_dropped_join_skips_predicates_the_others_imply():
    # each predicate of the triangle is implied by the other two, so dropping any of them is a valid rewrite
    triangle = "SELECT MIN(a.x) AS m FROM a, b, c WHERE a.id = b.id AND b.id = c.id AND a.id = c.id"
    assert bench.broken_dropped_join(triangle) is None
    chain = "SELECT MIN(a.x) AS m FROM a, b, c WHERE a.id = b.id AND b.id = c.id AND a.id = c.id AND c.k = a.k"
    assert bench.broken_dropped_join(chain).endswith("WHERE a.id = b.id AND b.id = c.id AND a.id = c.id")


def test_forms_skip_queries_that_are_not_plain_comma_joins():
    assert bench.form_explicit_joins("SELECT a FROM t JOIN u ON t.id = u.id") is None
    assert bench.form_filter_ctes("WITH x AS (SELECT 1 AS a) SELECT a FROM x") is None


def test_shared_root_limit_detection():
    assert bench._shares_root_limit("SELECT a FROM t ORDER BY a LIMIT 5", "SELECT a FROM t AS t ORDER BY a LIMIT 5")
    assert not bench._shares_root_limit("SELECT a FROM t ORDER BY a LIMIT 5", "SELECT a FROM t ORDER BY a LIMIT 6")
    assert bench._strip_root_limit("SELECT a FROM t ORDER BY a LIMIT 5") == "SELECT a FROM t"
