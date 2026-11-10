"""One pre-pass of ``algebraic_equivalence.normalize`` for the BigQuery constructs the provers do not model.

The three rewrites run in this order, each in its own module (read the module doc for the soundness argument):

* ``unnest_literals.split_unnest_literals``: ``CROSS JOIN UNNEST([e1, .., en])`` as a union of one select per element;
  first, so that no later step reads the UNNEST as an opaque array source;
* ``struct_fields.split_struct_fields``: a derived table's STRUCT column read field by field, as plain columns; before
  any step could read ``s.f`` as a column ``f`` of a table ``s``;
* ``in_null_candidates.drop_null_candidate_filters``: an IN subquery's filter that only drops NULL candidates; before
  ``_isolate_windows`` moves the window out of its reach.
"""

from __future__ import annotations

from sqlglot import exp

from .in_null_candidates import drop_null_candidate_filters
from .struct_fields import split_struct_fields
from .unnest_literals import split_unnest_literals


def rewrite_bigquery_constructs(
    tree: exp.Expression,
    schema: dict[str, list[str]] | None = None,
    types: dict[str, dict[str, str]] | None = None,
    dialect: str = "bigquery",
) -> exp.Expression:
    tree = split_unnest_literals(tree, schema, types, dialect)
    tree = split_struct_fields(tree, schema, dialect)
    return drop_null_candidate_filters(tree, types)
