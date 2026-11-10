"""Local validation for SQL sent by read-only BigQuery features."""

import re

from sqlglot import exp

from kumo_sql_prover.ast_utils import parse_statements


def validate_readonly_query(sql: str) -> None:
    """Require one SELECT, optionally with CTEs, subqueries or set operations.

    Check the whole tree: a query-shaped root can still contain a writable CTE
    or SELECT INTO. Parser failures deliberately omit SQL and parser excerpts.
    """
    message = "the query must be exactly one read-only SELECT statement"
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 20_000:
        raise ValueError(message)
    try:
        # sqlglot represents a comment after a trailing semicolon as an empty
        # Semicolon node. It has no statement or executable content.
        statements = [node for node in parse_statements(sql) if not isinstance(node, exp.Semicolon)]
    except Exception:
        raise ValueError(message) from None
    roots = (exp.Select, exp.SetOperation, exp.Subquery)
    if len(statements) != 1 or not isinstance(statements[0], roots):
        raise ValueError(message)
    forbidden = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Transaction, exp.Lock)
    for node in statements[0].walk():
        if isinstance(node, forbidden):
            raise ValueError(message)
        if isinstance(node, exp.CTE) and not isinstance(node.this, roots):
            raise ValueError(message)
        if isinstance(node, exp.Subquery) and not isinstance(node.this, roots):
            raise ValueError(message)
        if isinstance(node, exp.SetOperation) and not all(isinstance(branch, roots) for branch in (node.this, node.expression)):
            raise ValueError(message)


def quote_table_path(table: str) -> str:
    """Quote a strict project.dataset.table path, refusing SQL punctuation."""
    if not isinstance(table, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", table
    ):
        raise ValueError("table must be an unquoted project.dataset.table identifier")
    return f"`{table}`"
