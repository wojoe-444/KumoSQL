"""Shared sqlglot helpers used by rewrite rules and the equivalence prover."""

from __future__ import annotations

from contextlib import contextmanager
import decimal
import logging
import re

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.errors import UnsupportedError


class _UnknownSubqueryScope(logging.Filter):
    """sqlglot warns (with the SQL text) for every subquery lineage cannot scope; say it once, at debug, without the SQL."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.msg).startswith("Unknown subquery scope"):
            return True
        self.count += 1
        if self.count == 1:
            logging.getLogger("kumosql.lineage").debug("a subquery had no scope for lineage and was skipped (reported once)")
        return False


_scope_filter = _UnknownSubqueryScope()
logging.getLogger("sqlglot.lineage").addFilter(_scope_filter)


# The FROM argument's name: sqlglot 28 renamed it from "from" to "from_".
FROM_KEY = "from_" if "from_" in exp.Select.arg_types else "from"


def is_call(node: object, kind: str) -> bool:
    """``node`` calls the sqlglot function class named ``kind``, e.g. ``"DenseRank"``.

    Older sqlglot versions lack some classes (sqlglot 26 has no ``Rank``, ``DenseRank`` or ``Grouping``)
    and parse such a call as an anonymous function, matched here by its SQL name (``DENSE_RANK``).
    """

    cls = getattr(exp, kind, None)
    if cls is not None:
        return isinstance(node, cls)
    return isinstance(node, exp.Anonymous) and str(node.this).upper() == re.sub(r"(?<!^)(?=[A-Z])", "_", kind).upper()


def _duckdb_printing() -> tuple[bool, bool, bool]:
    with quiet_parser():
        division = sqlglot.parse_one("SELECT a / b", read="mysql").sql(dialect="duckdb")
        concat = sqlglot.parse_one("SELECT CONCAT(a, b)", read="mysql").sql(dialect="duckdb")
        stamp = sqlglot.parse_one("SELECT TIMESTAMP_SUB(a, INTERVAL 1 HOUR)", read="bigquery").sql(dialect="duckdb")
    return "NULLIF" in division, "||" in concat, "INTERVAL" in stamp


def spell_for_duckdb(tree: exp.Expression) -> exp.Expression:
    """Spell what DuckDB would read differently from the query's own engine, in place, where sqlglot does not.

    A division that is NULL on a zero divisor (MySQL's ``/``) becomes ``a / NULLIF(b, 0)``, and a
    ``CONCAT`` that is NULL when an argument is NULL becomes ``a || b``. ``TIMESTAMP_ADD`` and
    ``TIMESTAMP_SUB`` become ``a + INTERVAL ...``. Current sqlglot prints all of these that way itself;
    sqlglot 26 prints a plain ``/`` (infinity in DuckDB), ``CONCAT`` (which skips NULLs in DuckDB) and
    ``TIMESTAMP_SUB(a, '1', HOUR)`` (no such DuckDB function), so a query run there for a check would
    answer differently or not run.
    """

    global _DUCKDB_PRINTING
    if _DUCKDB_PRINTING is None:
        _DUCKDB_PRINTING = _duckdb_printing()
    division, concat, stamp = _DUCKDB_PRINTING
    if not division:
        for div in list(tree.find_all(exp.Div)):
            if div.args.get("safe"):
                div.set("expression", exp.Nullif(this=div.expression, expression=exp.Literal.number(0)))
                div.set("safe", None)
    if not concat:
        for node in list(tree.find_all(exp.Concat)):
            parts = list(node.expressions)
            if node.args.get("coalesce") or len(parts) < 2:
                continue
            chain = parts[0]
            for part in parts[1:]:
                chain = exp.DPipe(this=chain, expression=part)
            node.replace(exp.Paren(this=chain))
    if not stamp:
        for node in list(tree.find_all(exp.TimestampAdd, exp.TimestampSub)):
            amount = node.expression
            amount = exp.Literal.string(str(amount.name)) if isinstance(amount, exp.Literal) else exp.Paren(this=amount.copy())
            interval = exp.Interval(this=amount, unit=exp.var(str(node.args["unit"].name).upper())) if node.args.get("unit") is not None else None
            if interval is not None:
                operator = exp.Add if isinstance(node, exp.TimestampAdd) else exp.Sub
                replacement = operator(this=node.this.copy(), expression=interval)
                if node is tree:
                    tree = replacement
                else:
                    node.replace(replacement)
    return tree


_DUCKDB_PRINTING: tuple[bool, bool, bool] | None = None


def grouping_elements(group: exp.Group | None) -> list[exp.Expression]:
    """The elements of a ``GROUP BY``: plain keys and any ``ROLLUP``, ``CUBE`` or ``GROUPING SETS``.

    sqlglot 26 keeps ``ROLLUP(...)``, ``CUBE(...)`` and ``GROUPING SETS (...)`` under their own arguments
    of the Group node, where later versions list them with the plain keys. MySQL's ``GROUP BY a WITH
    ROLLUP`` stays under its argument in every version, as a ROLLUP with no keys of its own.
    """

    if group is None:
        return []
    return list(group.expressions) + [node for key in ("grouping_sets", "cube", "rollup") for node in group.args.get(key) or []]


# The EXCEPT list of ``SELECT * EXCEPT (...)``: sqlglot 30 renamed the argument from "except" to "except_".
EXCEPT_KEY = "except_" if "except_" in exp.Star.arg_types else "except"


def is_function_table(table: exp.Table) -> bool:
    """``FROM dataset.fn(...)``: a table-valued function call, whose name is not a table."""

    return table.this is not None and isinstance(table.this, exp.Func)


@contextmanager
def quiet_parser():
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def parse_statements(sql: str, *, recover: bool = False) -> list[exp.Expression]:
    """Parse BigQuery SQL, raising unless ``recover`` asks for recovery mode."""

    error_level = ErrorLevel.IGNORE if recover else ErrorLevel.RAISE
    with quiet_parser():
        parsed = sqlglot.parse(sql, read="bigquery", error_level=error_level)
    return [statement for statement in parsed if statement is not None]


def render_statement(statement: exp.Expression) -> str:
    """Render a transformed statement in the project's output style."""

    return statement.sql(dialect="bigquery", pretty=True, pad=4, identify=False)


_POSITION_META = ("line", "col", "start", "end")


def strip_positions(tree: exp.Expression) -> exp.Expression:
    """Drop the source positions the parser records on tokens, in place.

    A third of the nodes carry them and every ``copy()`` deep-copies them, which was about 30% of the
    prover's run time. Nothing in the equivalence pipeline reads positions; other ``meta`` keys are kept.
    """

    for node in tree.walk():
        meta = node._meta
        if meta:
            for key in _POSITION_META:
                meta.pop(key, None)
            if not meta:
                node._meta = None
    return tree


def canonical_negation(tree: exp.Expression) -> exp.Expression:
    """Spell ``x IS NOT NULL``, ``x NOT LIKE y`` and ``x NOT ILIKE y`` as ``NOT (...)``.

    Some dialects (PostgreSQL in current sqlglot releases) parse these as ``Is``, ``Like`` or
    ``ILike`` carrying ``negate=True``; code that reads only the node type would take them for the
    positive test. One spelling everywhere keeps the provers from reading ``IS NOT NULL`` as ``IS NULL``.
    """

    for node in list(tree.find_all(exp.Is, exp.Like, exp.ILike)):
        if node.args.get("negate"):
            positive = node.copy()
            positive.set("negate", None)
            if node is tree:
                return exp.Not(this=positive)
            node.replace(exp.Not(this=positive))
    return tree


class UnmodeledConstruct(UnsupportedError):
    """The query carries a sqlglot flag or clause that the provers do not model."""


# (node class, arg) pairs that change what a query returns and that neither prover reads. A query
# carrying one is declined, never proved with the flag ignored.
_UNMODELED_ARGS = (
    (exp.Between, "symmetric"),
    (exp.Cast, "format"),
    (exp.TryCast, "format"),
    (exp.Join, "match_condition"),
    (exp.Lateral, "ordinality"),
    (exp.Ordered, "with_fill"),
    (exp.Select, "exclude"),
    (exp.Table, "version"),
    (exp.Table, "system_time"),
    (exp.Table, "when"),
    (exp.Table, "partition"),
    (exp.Table, "changes"),
    (exp.Table, "rows_from"),
    (exp.Table, "only"),
    (exp.Table, "pattern"),
    (exp.Table, "ordinality"),
)


# sqlglot reads ``a = b IS TRUE`` as ``a = (b IS TRUE)``; BigQuery, MySQL, PostgreSQL, DuckDB and Calcite read
# ``(a = b) IS TRUE`` (see ``read_is_after_comparison``).
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ)


def parenthesize_is_operands(tree: exp.Expression) -> exp.Expression:
    """Wrap an ``IS`` test that is a comparison's right operand in parentheses, as the tree means it."""

    for node in list(tree.find_all(*_COMPARISONS)):
        operand = node.expression
        if isinstance(operand, exp.Is) or (isinstance(operand, exp.Not) and isinstance(operand.this, exp.Is)):
            operand.replace(exp.Paren(this=operand.copy()))
    return tree


def read_is_after_comparison(tree: exp.Expression) -> exp.Expression:
    """Read ``a = b IS TRUE`` as the engines do, ``(a = b) IS TRUE``, in place.

    sqlglot parses an ``IS`` test after a comparison as the comparison's right operand. In
    PostgreSQL, DuckDB and Calcite ``IS`` binds more loosely than ``=``; in MySQL they share a level
    and associate to the left: all read the test as applying to the whole comparison (so
    ``NULL = 1 IS NULL`` is true, not NULL). The tree is rebuilt as sqlglot parses the parenthesized
    text ``(a = b) IS TRUE``. An explicit ``a = (b IS TRUE)`` keeps its ``Paren`` and is left alone.

    ``a = b IS NOT TRUE`` and ``a = NOT b IS TRUE`` parse to the same tree but read differently
    (DuckDB: ``(a = b) IS NOT TRUE`` and ``a = NOT (b IS TRUE)``), so a ``NOT`` among the operand's
    ``IS`` tests is declined.
    """

    for node in list(tree.find_all(*_COMPARISONS)):
        tests, operand = [], node.expression
        while isinstance(operand, exp.Is):
            tests.append(operand)
            operand = operand.this
        if isinstance(operand, exp.Not) and isinstance(operand.this, exp.Is):
            raise UnmodeledConstruct("a comparison followed by IS NOT without parentheses reads differently across engines")
        if not tests:
            continue
        # comparison(a, IS_1(... IS_n(b) ...)) becomes IS_1(... IS_n((comparison(a, b))) ...)
        if node is tree:
            tree = tests[0]
        node.replace(tests[0])
        tests[-1].set("this", exp.Paren(this=node))
        node.set("expression", operand)
    return tree


def table_function_reads_cte(tree: exp.Expression) -> bool:
    """Whether a table-valued function call is handed a CTE by name.

    DuckDB passes a table to a function by its bare name (``histogram_values(cte, l)``) and BigQuery
    with ``TABLE cte``. Such a read is not a table reference, so code that tracks CTE use by table
    references would think the CTE unused, drop it, and compare queries that read different rows.
    """

    names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    if not names:
        return False
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Identifier) or table.this is None:
            continue
        for node in table.this.walk():
            if isinstance(node, exp.Column) and not node.table and node.name.lower() in names:
                return True
            if isinstance(node, exp.Table) and not node.db and node.name.lower() in names:
                return True
    return False


# Aggregates that older sqlglot versions parse as anonymous functions (sqlglot 26 knows none of these).
# The provers would read such a call as a row-level function, so a global one over no rows would seem
# to return no rows instead of one; a query that calls one outside a window is refused instead.
_NEWER_AGGREGATES = {
    "BitwiseAndAgg": ("BIT_AND", "BITWISE_AND_AGG"), "BitwiseOrAgg": ("BIT_OR", "BITWISE_OR_AGG"),
    "BitwiseXorAgg": ("BIT_XOR", "BITWISE_XOR_AGG"), "BoolxorAgg": ("BOOLXOR_AGG",), "GroupingId": ("GROUPING_ID",),
    "Mode": ("MODE",), "Kurtosis": ("KURTOSIS",), "Skewness": ("SKEWNESS",), "ArrayConcatAgg": ("ARRAY_CONCAT_AGG",),
    "ObjectAgg": ("OBJECT_AGG",), "ApproxQuantiles": ("APPROX_QUANTILES",), "ApproxTopSum": ("APPROX_TOP_SUM",),
    "HashAgg": ("HASH_AGG",), "Minhash": ("MINHASH",), "BitmapOrAgg": ("BITMAP_OR_AGG",),
    "BitmapConstructAgg": ("BITMAP_CONSTRUCT_AGG",), "RegrCount": ("REGR_COUNT",), "RegrAvgx": ("REGR_AVGX",),
    "RegrAvgy": ("REGR_AVGY",), "RegrIntercept": ("REGR_INTERCEPT",), "RegrR2": ("REGR_R2",), "RegrSlope": ("REGR_SLOPE",),
    "RegrSxx": ("REGR_SXX",), "RegrSxy": ("REGR_SXY",), "RegrSyy": ("REGR_SYY",),
}
_UNCLASSED_AGGREGATES = frozenset(name for cls, names in _NEWER_AGGREGATES.items() if not hasattr(exp, cls) for name in names)


def is_aggregate(node: object) -> bool:
    """An aggregate call, including one an older sqlglot version parses as an anonymous function."""

    return isinstance(node, exp.AggFunc) or (isinstance(node, exp.Anonymous) and str(node.this).upper() in _UNCLASSED_AGGREGATES)


# sqlglot parses IS [NOT] DISTINCT FROM (NullSafeEQ, NullSafeNEQ) beside LIKE, IN and BETWEEN: tighter than ``=`` and ``<``,
# left to right, and prints the tree back without parentheses. PostgreSQL and DuckDB read it below all of those
# (``a = b IS NOT DISTINCT FROM c`` is ``(a = b) IS NOT DISTINCT FROM c``, ``a IS DISTINCT FROM b IN (c)`` is
# ``a IS DISTINCT FROM (b IN (c))``) and refuse to chain it with another IS; MySQL puts ``<=>``, ``=``, LIKE and IN on
# one level, left to right. GoogleSQL's grouping was not confirmed; declining holds under any of them.
_COMPARISON_LEVEL = (
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Like,
    exp.ILike, exp.SimilarTo, exp.Glob, exp.RegexpLike, exp.RegexpILike, exp.In, exp.Between,
)


def _comparison_operator(node: exp.Expression | None) -> bool:
    if isinstance(node, exp.Not):
        node = node.this
    return isinstance(node, _COMPARISON_LEVEL)


def check_distinct_from_grouping(tree: exp.Expression) -> None:
    """Decline IS [NOT] DISTINCT FROM next to another comparison without parentheses between them.

    Engines group such text differently from sqlglot's tree, so it is not proved under either reading.
    An IN list item is in parentheses already.
    """

    for node in tree.find_all(exp.NullSafeEQ, exp.NullSafeNEQ):
        parent = node.parent
        nested = isinstance(parent, _COMPARISON_LEVEL) and not (isinstance(parent, exp.In) and node.arg_key != "this")
        if nested or _comparison_operator(node.this) or _comparison_operator(node.expression):
            raise UnmodeledConstruct("IS DISTINCT FROM next to another comparison without parentheses reads differently across engines")


def _uncorrelated(select: exp.Select) -> bool:
    """Whether ``select`` sits only under derived tables, CTEs and set operations, so its columns are its own."""

    node = select
    while node.parent is not None:
        parent = node.parent
        if isinstance(parent, (exp.Subquery, exp.CTE, exp.With, exp.From, exp.Join, exp.SetOperation)):
            pass
        elif isinstance(parent, exp.Select) and node.arg_key in ("from", "from_", "joins", "with", "with_"):
            pass
        else:
            return False
        node = parent
    return True


def expand_group_by_all(tree: exp.Expression) -> exp.Expression:
    """Spell ``GROUP BY ALL`` as the grouping keys it infers, in place, or raise :class:`UnmodeledConstruct`.

    The keys are the select items that reference a column and hold no aggregate, written as positions.
    With no keys the query has one group even on empty input, so an aggregate-only select becomes a
    plain global aggregate. Correlated selects, stars, windows, subqueries, unknown functions (which may
    be aggregates) and a select of only constants (engines differ on whether constants are keys) are declined.
    """

    for group in list(tree.find_all(exp.Group)):
        if not group.args.get("all"):
            continue
        select = group.parent
        if not isinstance(select, exp.Select) or group.expressions or not _uncorrelated(select):
            raise UnmodeledConstruct("GROUP BY ALL is not modeled here")
        keys: list[int] = []
        key_sql: set[str] = set()
        aggregated: list[exp.Expression] = []
        constants = False
        for position, item in enumerate(select.expressions, start=1):
            value = item.this if isinstance(item, exp.Alias) else item
            if isinstance(value, exp.Star) or (isinstance(value, exp.Column) and isinstance(value.this, exp.Star)):
                raise UnmodeledConstruct("GROUP BY ALL with a star select is not modeled")
            if value.find(exp.Window, exp.Subquery, exp.Select, exp.Exists, exp.Lambda):
                raise UnmodeledConstruct("GROUP BY ALL next to a window or subquery is not modeled")
            if value.find(exp.AggFunc):
                aggregated.append(value)
            elif value.find(exp.Anonymous):
                raise UnmodeledConstruct("GROUP BY ALL with an unknown function is not modeled")
            elif value.find(exp.Column):
                keys.append(position)
                key_sql.add(value.sql())
            else:
                constants = True
        for value in aggregated:
            # A column outside the aggregates must be a key, or the query is not valid.
            for column in value.find_all(exp.Column):
                if not column.find_ancestor(exp.AggFunc) and column.sql() not in key_sql:
                    raise UnmodeledConstruct("GROUP BY ALL with an ungrouped column is not modeled")
        if keys:
            group.set("all", None)
            group.set("expressions", [exp.Literal.number(p) for p in keys])
        elif aggregated and not constants:
            select.set("group", None)
        else:
            raise UnmodeledConstruct("GROUP BY ALL without grouping keys is not modeled")
    return tree


# Table reads a query makes once every CTE reference is expanded in place. Provers inline CTEs, so a CTE
# read twice per level doubles the work each level; past this the pair is declined rather than run for minutes.
MAX_EXPANDED_READS = 256


def expanded_reads(tree: exp.Expression, limit: int = MAX_EXPANDED_READS) -> int:
    """An upper bound on the table reads of ``tree`` with its CTEs inlined, counting stops past ``limit``."""

    definitions: dict[str, list[exp.CTE]] = {}
    for cte in tree.find_all(exp.CTE):
        definitions.setdefault(cte.alias_or_name.lower(), []).append(cte)
    if not definitions:
        return 0
    memo: dict[int, int] = {}

    def reads(node: exp.Expression) -> int:
        total = 0
        for table in node.find_all(exp.Table):
            ancestor = table.parent
            while ancestor is not None and ancestor is not node and not isinstance(ancestor, exp.CTE):
                ancestor = ancestor.parent
            if ancestor is not None and ancestor is not node:
                continue  # inside a CTE definition: counted where it is read
            name = table.name.lower()
            if not table.args.get("db") and not table.args.get("catalog") and name in definitions:
                total += max(cte_reads(cte) for cte in definitions[name])
            else:
                total += 1
            if total > limit:
                break
        return total

    def cte_reads(cte: exp.CTE) -> int:
        key = id(cte)
        if key not in memo:
            memo[key] = 1  # a recursive reference
            memo[key] = reads(cte.this)
        return memo[key]

    return reads(tree)


def check_modeled(tree: exp.Expression) -> exp.Expression:
    """Raise :class:`UnmodeledConstruct` for a flag the provers would silently ignore.

    Covers ``TABLESAMPLE``, time travel, ``SYMMETRIC`` ranges, ``WITH TIES`` and ``PERCENT``
    limits, ``OUTER APPLY`` and the like. sqlglot versions differ in which of these they parse, so
    anything carried on the node is refused whichever version produced it. ``GROUP BY ALL`` is
    spelled out first (see :func:`expand_group_by_all`). An ``IS`` test after a
    comparison is re-read as the engines read it (:func:`read_is_after_comparison`).
    """

    tree = expand_group_by_all(tree)
    for literal in tree.find_all(exp.Literal):
        if not literal.is_string and len(literal.this) > 100:
            # No FLOAT64, INT64 or NUMERIC needs this many characters; int() and Fraction() on it can take minutes.
            raise UnmodeledConstruct("a numeric literal longer than 100 characters is not modeled")
        if not literal.is_string and exponent_out_of_range(literal.this):
            raise UnmodeledConstruct(f"numeric literal {literal.this[:40]!r} is outside FLOAT64's exponent range")
    if expanded_reads(tree) > MAX_EXPANDED_READS:
        raise UnmodeledConstruct(f"the query reads more than {MAX_EXPANDED_READS} tables once its CTEs are expanded")
    check_distinct_from_grouping(tree)
    for node in tree.walk():
        for kind, arg in _UNMODELED_ARGS:
            if isinstance(node, kind) and node.args.get(arg):
                raise UnmodeledConstruct(f"{kind.__name__}.{arg} is not modeled")
        if getattr(node, "arg_types", None) and "sample" in node.arg_types and node.args.get("sample"):
            raise UnmodeledConstruct(f"{type(node).__name__}.sample is not modeled")
        if isinstance(node, exp.Lateral) and node.args.get("cross_apply") is False:
            raise UnmodeledConstruct("OUTER APPLY is not modeled")
        if isinstance(node, exp.Table) and any(isinstance(node.args.get(part), str) for part in ("db", "catalog")):
            # sqlglot reads ``t..u`` with an empty string where an identifier belongs; code that asks a part its name fails
            raise UnmodeledConstruct("a table name with an empty part (t..u) is not modeled")
        if isinstance(node, exp.Fetch) and (node.args.get("percent") or node.args.get("with_ties")):
            raise UnmodeledConstruct("PERCENT and WITH TIES limits are not modeled")
        if type(node).__name__ == "LimitOptions" and (node.args.get("percent") or node.args.get("with_ties")):
            raise UnmodeledConstruct("PERCENT and WITH TIES limits are not modeled")
        if isinstance(node, exp.Anonymous) and str(node.this).upper() in _UNCLASSED_AGGREGATES and not isinstance(node.parent, exp.Window):
            raise UnmodeledConstruct(f"{str(node.this).upper()} is an aggregate this sqlglot version does not know")
    tree = read_is_after_comparison(tree)
    if table_function_reads_cte(tree):
        raise UnmodeledConstruct("a table function that reads a CTE by name is not modeled")
    return tree


def exponent_out_of_range(text: str) -> bool:
    """Whether the numeric literal ``text`` is a nonzero number outside FLOAT64's exponent range (``1e100000000``).

    Every prover declines these (``Fraction(Decimal('1e100000000'))`` would build a hundred-million-digit integer);
    checking here makes the algebraic and structural paths decline them too, not only the SMT and bounded compilers.
    """

    try:
        value = decimal.Decimal(text)
    except decimal.InvalidOperation:
        return False
    return value.is_finite() and bool(value) and not -400 <= value.adjusted() <= 400


def drop_case_conflicts(tables: dict | None) -> dict | None:
    """``tables`` (a schema, types or constraints mapping) without the entries whose names differ only in case and disagree.

    The provers look tables up case-insensitively, so ``{"t": ["a"], "T": ["b"]}`` used to mean whichever entry
    came last. Both entries are dropped instead (the prover then knows nothing about that table's columns);
    entries that agree once lower-cased are kept.
    """

    if not tables:
        return tables

    def folded(value):
        if isinstance(value, dict):
            return sorted((str(k).lower(), str(v)) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return [str(c).lower() for c in value]
        return value

    seen: dict[str, object] = {}
    clashes: set[str] = set()
    for name, value in tables.items():
        key = str(name).lower()
        if key in seen and seen[key] != folded(value):
            clashes.add(key)
        seen.setdefault(key, folded(value))
    if not clashes:
        return tables
    return {name: value for name, value in tables.items() if str(name).lower() not in clashes}


def _output_names(query: exp.Expression) -> list[str] | None:
    """The column names a derived query outputs, or ``None`` when they are not plain and distinct."""

    while isinstance(query, (exp.Subquery, exp.SetOperation)):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    names = []
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        names.append(item.alias_or_name)
    if "" in names or len({n.lower() for n in names}) != len(names):
        return None
    return names


def expand_alias_columns(tree: exp.Expression, schema: dict[str, list[str]] | None) -> exp.Expression:
    """Rewrite ``FROM t AS d(a, b)`` as ``FROM (SELECT c1 AS a, c2 AS b FROM t) AS d``.

    A column list on a table alias renames the source's columns by position, so ``dept AS d(name, x)``
    calls ``deptno`` ``name``. The provers resolve columns by name and would read the original
    ``name``; some sqlglot dialects also drop the list when printing. The list is made explicit
    here, for tables (columns in ``schema`` order), CTEs and derived tables; a list that cannot be
    resolved (unknown table, star select, more names than columns) is declined.
    """

    from .canonical import visible_ctes

    lowered = {k.lower(): v for k, v in (schema or {}).items()}
    # Innermost lists first, so a derived table copied into an outer expansion carries its own expanded.
    for alias in reversed(list(tree.find_all(exp.TableAlias))):
        renamed = [c.name for c in alias.args.get("columns") or []]
        source = alias.parent
        if not renamed or isinstance(source, exp.CTE):
            continue
        if not isinstance(source, (exp.Table, exp.Subquery)) or not isinstance(source.parent, (exp.From, exp.Join)):
            continue
        if isinstance(source, exp.Table):
            if not isinstance(source.this, exp.Identifier):
                raise UnmodeledConstruct("a column list on a table function alias is not modeled")
            key = ".".join(p.name for p in source.parts).lower()
            ctes: dict[str, exp.Expression] = {}
            for name, body in visible_ctes(source).items():  # the nearest WITH that defines the name
                ctes.setdefault(name.lower(), body)
            if not source.db and key in ctes:
                columns = _output_names(ctes[key])
            else:
                columns = lowered.get(key) or (lowered.get(source.name.lower()) if source.db else None)
        else:
            columns = _output_names(source.this)
        if not columns or len(renamed) > len(columns):
            raise UnmodeledConstruct("a column list on a table alias could not be resolved")
        inner_alias = "kq_renamed"
        items = [
            exp.alias_(exp.column(old, table=inner_alias), new if index < len(renamed) else old)
            for index, (old, new) in enumerate(zip(columns, renamed + list(columns[len(renamed):])))
        ]
        if isinstance(source, exp.Table):
            inner = source.copy()
            inner.set("alias", exp.TableAlias(this=exp.to_identifier(inner_alias)))
        else:
            inner = exp.Subquery(this=source.this.copy(), alias=exp.TableAlias(this=exp.to_identifier(inner_alias)))
        body = exp.select(*items).from_(inner)
        replacement = exp.Subquery(this=body, alias=exp.TableAlias(this=alias.this.copy()))
        if source is tree:
            return replacement
        source.replace(replacement)
    return tree


class LossySql(UnmodeledConstruct):
    """Printing a rewritten query in its dialect would change what it means, so it is declined."""


# Arguments that only record spelling or what the parser inferred about a type, not what is computed.
_IGNORED_ARGS = frozenset({"quoted", "comments", "big_int"})


def _shape(node: object) -> object:
    """A comparable form of a tree that ignores what does not change its meaning.

    Parentheses (the tree's own nesting already says how operators group), identifier quoting,
    comments and the case of names are left out; string literals keep their case.
    """

    if isinstance(node, exp.Paren) or (isinstance(node, exp.Subquery) and set(k for k, v in node.args.items() if v) == {"this"}):
        return _shape(node.this)  # grouping only: the tree's nesting already says how things group
    if isinstance(node, exp.Connector):
        # AND and OR are associative: a AND (b AND c) prints as a AND b AND c
        return (node.key, tuple(_shape(o) for o in _connected(node, type(node))))
    if isinstance(node, (exp.Union, exp.Intersect)) and not _modified(node):
        # UNION ALL, UNION and INTERSECT are associative, so a chain prints without inner parentheses
        return (node.key, bool(node.args.get("distinct")), tuple(_shape(o) for o in _chain(node, type(node), node.args.get("distinct"))))
    if isinstance(node, exp.DataType):
        # the provers read a type by its BigQuery spelling, so INT and SIGNED, or VARCHAR and CHAR, are one type
        return ("datatype", node.sql(dialect="bigquery").upper())
    if isinstance(node, (exp.LogicalAnd, exp.LogicalOr)):
        # MIN and MAX of booleans are LOGICAL_AND and LOGICAL_OR; MySQL spells them that way
        return (exp.Min.key if isinstance(node, exp.LogicalAnd) else exp.Max.key, (("this", _shape(node.this)),))
    if isinstance(node, exp.Fetch):
        return ("limit", (("expression", _shape(node.args.get("count"))),))
    if isinstance(node, exp.Limit) and not node.args.get("offset") and not node.args.get("expressions"):
        return ("limit", (("expression", _shape(node.args.get("expression"))),))
    if isinstance(node, list):
        return tuple(_shape(x) for x in node if x is not None and x is not False)
    if isinstance(node, exp.Expression):
        literal = isinstance(node, exp.Literal)
        items = []
        for key in sorted(node.args):
            if key in _IGNORED_ARGS or (isinstance(node, exp.Identifier) and key == "global_"):
                continue
            if isinstance(node, exp.Concat) and key == "safe":
                continue  # whether CONCAT raises on non-string arguments; errors are not modeled
            if isinstance(node, exp.Join) and key == "kind" and str(node.args[key]).upper() == "CROSS" and not node.args.get("on"):
                continue  # ", t" and "CROSS JOIN t" are one join
            value = node.args[key]
            if value is None or value is False or value == []:
                continue
            if isinstance(value, str) and not literal:
                value = value.lower()
            items.append((key, _shape(value) if isinstance(value, (exp.Expression, list)) else value))
        return (node.key, tuple(items))
    return node


def _modified(node: exp.Expression) -> bool:
    return any(node.args.get(k) for k in ("order", "limit", "offset", "with_", "with", "by_name", "side", "kind", "on"))


def _connected(node: exp.Expression, kind: type) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if type(node) is kind:
        return _connected(node.this, kind) + _connected(node.expression, kind)
    return [node]


def _chain(node: exp.Expression, kind: type, distinct) -> list[exp.Expression]:
    """The operands of a chain of one associative set operation, through grouping parentheses."""

    while isinstance(node, exp.Subquery) and set(k for k, v in node.args.items() if v) == {"this"}:
        node = node.this
    if type(node) is kind and bool(node.args.get("distinct")) == bool(distinct) and not _modified(node):
        return _chain(node.this, kind, distinct) + _chain(node.expression, kind, distinct)
    return [node]


# Other dialects whose printing a dialect's parser reads back, tried when its own printing does not round
# trip. MySQL's generator, for example, spells FULL JOIN as a LEFT JOIN UNION ALL a RIGHT JOIN (wrong under
# an aggregate), a DIV b as CAST(a / b AS SIGNED) (which rounds where DIV truncates) and CAST(x AS BOOLEAN)
# as an integer cast; Spark's prints all three as written, with the same quoting. (A generator subclass
# would do it directly, but a compiled sqlglot does not allow one.)
_NEIGHBOURS = ("spark", "", "duckdb", "postgres")


def faithful_sql(tree: exp.Expression, dialect: str) -> str:
    """Print ``tree`` as ``dialect`` SQL that parses back to the same query, or raise :class:`LossySql`.

    The provers hand rewritten queries to each other as text. sqlglot's generators rewrite some
    constructs for the target engine, and a few of those rewrites change the result, so the text is
    parsed back and compared with the tree before anyone reasons about it.
    """

    expected = _shape(tree)
    writers = [dialect] + [d for d in _NEIGHBOURS if d != dialect]
    # identify=True quotes every name, e.g. an unquoted BigQuery project name with a hyphen
    for writer, identify in [(dialect, False), (dialect, True)] + [(d, False) for d in writers[1:]]:
        try:
            with quiet_parser():
                text = tree.sql(dialect=writer, identify=identify)
                back = canonical_negation(sqlglot.parse_one(text, read=dialect))
        except sqlglot.errors.SqlglotError:
            continue
        if _shape(back) == expected:
            return text
    raise LossySql(f"the query cannot be printed faithfully in {dialect or 'the default dialect'}")


def identifier_name(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    value = getattr(node, "name", None)
    if value:
        return value
    value = getattr(node, "this", None)
    return value if isinstance(value, str) else None


def conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """The AND-parts of a predicate, with the parentheses around each part removed."""

    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return conjuncts(node.this) + conjuncts(node.expression)
    return [node]


def inside(node: exp.Expression, ancestor: exp.Expression) -> bool:
    """Whether ``ancestor`` is a strict ancestor of ``node``."""

    parent = node.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def select_sources(select: exp.Select) -> list[exp.Expression]:
    """The relations in a SELECT's FROM clause followed by its JOINs."""

    from_ = select.args.get("from_") or select.args.get("from")
    return ([from_.this] if from_ is not None else []) + [join.this for join in select.args.get("joins") or []]


def plain_distinct(select: exp.Expression) -> bool:
    """Whether ``select`` is ``SELECT DISTINCT``: duplicate removal over whole output rows.

    ``DISTINCT ON (k)`` is not: it keeps one row per ``k``, the first in the select's ``ORDER BY``, so it
    picks values (and its ORDER BY shows) rather than only dropping repeats.
    """

    distinct = select.args.get("distinct")  # a set operation's is a bool
    return isinstance(distinct, exp.Distinct) and not distinct.args.get("on")


def distinct_on(select: exp.Expression) -> bool:
    """Whether ``select`` is a ``SELECT DISTINCT ON (..)``."""

    distinct = select.args.get("distinct")
    return isinstance(distinct, exp.Distinct) and bool(distinct.args.get("on"))


def star_of(item: exp.Expression) -> exp.Star | None:
    """The Star of a select item ``*`` or ``t.*`` (``t.*`` keeps its EXCEPT, REPLACE .. on that Star), else ``None``."""

    if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
        return item.this
    return item if isinstance(item, exp.Star) else None


def star_modifier(item: exp.Expression, key: str):
    """The ``"except"``, ``"replace"``, ``"rename"`` or ``"ilike"`` modifier of ``*`` or ``t.*`` (``None`` without one).

    sqlglot 30 calls EXCEPT ``except_`` where 26 calls it ``except``: both spellings are read.
    """

    star = star_of(item)
    if star is None:
        return None
    return star.args.get(key) or star.args.get(f"{key}_")


def star_modified(item: exp.Expression) -> bool:
    """Whether ``*`` or ``t.*`` carries any modifier: EXCEPT, REPLACE, RENAME, ILIKE or one a later sqlglot adds."""

    star = star_of(item)
    return star is not None and any(star.args.values())


def table_parts(table: exp.Table) -> list[str]:
    """Lower-case catalog, dataset and table names of a table reference, skipping empty parts."""

    return [p.name.lower() for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name]


def same_table(a: exp.Table, b: exp.Table, dialect: str = "bigquery") -> bool:
    """Whether two table references certainly name one relation: every part (catalog, dataset, table) matches.

    A part one reference spells and the other leaves out may resolve to anything, so they count as
    different. BigQuery dataset and table names are case-sensitive and kept as written (project ids are
    lower case anyway); other dialects fold each part as they resolve it (unquoted parts, say).
    """

    def identity(table: exp.Table) -> list[str]:
        if (dialect or "bigquery") == "bigquery":
            return [p.name for p in table.parts]
        resolver = sqlglot.Dialect.get_or_raise(dialect)
        copy = table.copy()  # normalizing rewrites the identifier in place; the copy keeps its parent table
        return [resolver.normalize_identifier(p).name if isinstance(p, exp.Identifier) else p.sql(dialect=dialect) for p in copy.parts]

    return identity(a) == identity(b)


def with_arg_key(node: exp.Expression) -> str:
    """Return the sqlglot WITH argument name across supported versions."""

    args = getattr(node, "args", {})
    arg_types = getattr(node, "arg_types", {})
    if "with_" in args or "with_" in arg_types:
        return "with_"
    if "with" in args or "with" in arg_types:
        return "with"
    # Current sqlglot uses ``with_``. This fallback keeps construction
    # compatible with expression implementations that expose neither key until
    # the first value is assigned.
    return "with_"


def merge_wrapper_tails(node: exp.Expression) -> exp.Expression | None:
    """The query inside any parentheses around ``node``, with the wrappers' ORDER BY / LIMIT / OFFSET on it.

    In sqlglot the tail of ``(a UNION b) ORDER BY k LIMIT 1`` hangs on the enclosing ``Subquery``, so code that
    unwraps the parentheses to reach the set operation silently drops it. This returns the innermost query
    carrying the tail of whichever layer held the LIMIT or OFFSET (a copy when it had to move), or ``None``
    when the layers cannot be folded into one tail: two layers cut rows (``((q LIMIT 3) LIMIT 1)``), or an
    ORDER BY sits outside the layer that cuts them (``((q ORDER BY k LIMIT 3) ORDER BY k DESC)``) and so reorders
    the survivors. Without a LIMIT or OFFSET anywhere the ordering cannot change the bag and is left alone.
    """

    layers = [node]
    while isinstance(layers[-1], exp.Subquery):
        layers.append(layers[-1].this)
    inner = layers[-1]
    cutting = [i for i, layer in enumerate(layers) if layer.args.get("limit") or layer.args.get("offset")]
    if not cutting:
        return inner
    if len(cutting) > 1 or any(layer.args.get("order") for layer in layers[: cutting[0]]):
        return None
    holder = layers[cutting[0]]
    if holder is inner:
        return inner
    merged = inner.copy()
    outer_with = [layer.args[key] for layer in layers[:-1] for key in ("with", "with_") if layer.args.get(key)]
    if outer_with:
        if len(outer_with) > 1 or any(inner.args.get(key) for key in ("with", "with_")):
            return None
        merged.set(with_arg_key(merged), outer_with[0].copy())
    merged.set("limit", holder.args["limit"].copy() if holder.args.get("limit") else None)
    merged.set("offset", holder.args["offset"].copy() if holder.args.get("offset") else None)
    order = next((layer.args["order"] for layer in layers[cutting[0] :] if layer.args.get("order")), None)
    merged.set("order", order.copy() if order is not None else None)
    return merged


def top_level_query(statement: exp.Expression) -> exp.Expression | None:
    if isinstance(statement, (exp.Create, exp.Insert)):
        candidate = statement.expression
        while isinstance(candidate, exp.Subquery):
            candidate = candidate.this
        return candidate
    if isinstance(statement, (exp.Select, exp.Union, exp.Update, exp.Delete, exp.Merge)):
        return statement
    return None


def with_clause(query: exp.Expression) -> exp.With | None:
    return query.args.get(with_arg_key(query)) if hasattr(query, "args") else None


def set_with_clause(query: exp.Expression, clause: exp.With | None) -> None:
    query.set(with_arg_key(query), clause)


def cte_alias_name(cte: exp.CTE) -> str | None:
    return identifier_name(cte.args.get("alias"))


def is_cte_reference_candidate(table: exp.Table) -> bool:
    """Whether a table reference is one-part and can therefore name a CTE."""

    return not table.db and not table.catalog


def nearest_parent_cte(node: exp.Expression) -> exp.CTE | None:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            return parent
        parent = parent.parent
    return None


def nearest_root_cte(node: exp.Expression, root_ctes: list[exp.CTE]) -> exp.CTE | None:
    """Find the direct root CTE containing a node, including nested WITHs."""

    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE) and any(parent is cte for cte in root_ctes):
            return parent
        parent = parent.parent
    return None


def has_nested_with(query: exp.Expression) -> bool:
    root = with_clause(query)
    return any(isinstance(node, exp.With) and node is not root for node in query.walk())


def ambiguous_unnest_names(query: exp.Expression) -> set[str]:
    """Names of relations that also appear as a bare ``UNNEST`` argument.

    Older sqlglot releases (26.x) parse a repeated relation such as
    ``FROM shared JOIN shared AS s2`` as ``JOIN UNNEST(shared) AS s2``, so the
    AST no longer shows the second reference. A bare column inside UNNEST that
    matches a CTE or table name is therefore treated as a hidden reference.
    """

    names = {
        cte_alias_name(cte).lower()
        for cte in query.find_all(exp.CTE)
        if cte_alias_name(cte)
    }
    for table in query.find_all(exp.Table):
        if is_cte_reference_candidate(table):
            names.add(table.name.lower())
            if table.alias:
                names.add(table.alias.lower())
    hidden: set[str] = set()
    for unnest in query.find_all(exp.Unnest):
        for expression in unnest.expressions:
            if isinstance(expression, exp.Column) and not expression.table:
                if expression.name.lower() in names:
                    hidden.add(expression.name.lower())
    return hidden


def cte_dependency_errors(statement: exp.Expression) -> list[str]:
    """Find undefined or forward references in a statement's root CTE list."""

    query = top_level_query(statement)
    if query is None:
        return []
    clause = with_clause(query)
    if not clause:
        return []

    ctes = list(clause.expressions)
    positions = {
        name: index
        for index, cte in enumerate(ctes)
        if (name := cte_alias_name(cte))
    }
    recursive = bool(clause.args.get("recursive"))
    errors: list[str] = []

    for table in query.find_all(exp.Table):
        name = table.name
        if not is_cte_reference_candidate(table):
            continue
        if name.startswith("__lifted_subquery_") and name not in positions:
            errors.append(f"lifted CTE `{name}` is referenced but not defined")
            continue
        if name not in positions:
            continue

        owner = nearest_parent_cte(table)
        if owner is None:
            continue
        owner_name = cte_alias_name(owner)
        if owner_name is None or (name == owner_name and recursive):
            continue
        if positions[name] >= positions.get(owner_name, len(ctes)):
            errors.append(
                f"CTE `{name}` is referenced before it is defined by `{owner_name}`"
            )

    return list(dict.fromkeys(errors))


_EXTENDED_GROUPING = tuple(getattr(exp, name) for name in ("Rollup", "Cube", "GroupingSets") if hasattr(exp, name))


def extended_grouping(group: exp.Expression | None) -> bool:
    """``GROUP BY`` with ``ROLLUP``, ``CUBE``, ``GROUPING SETS`` or ``WITH TOTALS``: more than one grouping.

    Recent sqlglot keeps ``ROLLUP (x)`` as an item of ``group.expressions``, older versions (and MySQL's
    ``WITH ROLLUP``) in ``group.args``, so both are checked. Such a grouping can add a grand-total row
    that exists even over no input rows. So can the empty grouping set ``GROUP BY ()`` (a key-less
    ``Tuple`` item), which is counted too.
    """

    if group is None:
        return False
    if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
        return True
    return any(isinstance(e, _EXTENDED_GROUPING) or (isinstance(e, exp.Tuple) and not e.expressions) for e in group.expressions)


def visible_ctes(node: exp.Expression, top: exp.Expression | None = None) -> set[str]:
    """Names of the WITH tables in scope at ``node``, looking up to ``top`` (its own WITH included; default: the root).

    A non-recursive WITH table sees only the ones listed before it; the query under the WITH sees them all.
    """

    names: set[str] = set()
    child = node
    while child is not None and child is not top:
        parent = child.parent
        if isinstance(parent, exp.With):
            ctes = list(parent.expressions)
            seen = ctes if parent.args.get("recursive") else ctes[: next((i for i, c in enumerate(ctes) if c is child), len(ctes))]
            names |= {c.alias_or_name.lower() for c in seen}
        elif parent is not None:
            clause = parent.args.get("with_") or parent.args.get("with")
            if isinstance(clause, exp.With) and child is not clause:
                names |= {c.alias_or_name.lower() for c in clause.expressions}
        child = parent
    return names


def is_cte_reference(table: exp.Table) -> bool:
    """Whether ``table`` reads a WITH table that is in scope at that place (not a physical table of the same name).

    A WITH table is visible only inside its own query: a one-part read elsewhere in the statement that
    happens to share its name is still a read of the physical table.
    """

    return is_cte_reference_candidate(table) and bool(table.name) and table.name.lower() in visible_ctes(table)


def binding_cte(table: exp.Expression) -> exp.CTE | None:
    """The WITH table a one-part table reference reads, or ``None`` when it names a real table (or is not a table).

    Only a WITH in a scope enclosing ``table`` binds it, the nearest one first, with the visibility of
    :func:`visible_ctes`: a non-recursive WITH table's body sees only the ones listed before it, so
    ``WITH t AS (SELECT * FROM t)`` reads the real ``t``. Names collected from the whole statement would let a nested
    ``WITH t AS (...)`` hide a read of the real table ``t`` elsewhere in the statement.
    """

    if not isinstance(table, exp.Table) or not table.name or table.args.get("db") or table.args.get("catalog"):
        return None
    name = table.name.casefold()
    child, parent = table, table.parent
    while parent is not None:
        ctes: list = []
        if isinstance(parent, exp.With):
            ctes = list(parent.expressions)
            if not parent.args.get("recursive"):
                ctes = ctes[: next((i for i, c in enumerate(ctes) if c is child), len(ctes))]
        else:
            clause = parent.args.get("with_") or parent.args.get("with")
            if isinstance(clause, exp.With) and child is not clause:
                ctes = list(clause.expressions)
        for cte in reversed(ctes):
            if isinstance(cte, exp.CTE) and cte.alias_or_name.casefold() == name:
                return cte
        child, parent = parent, parent.parent
    return None


def free_reads(body: exp.Expression) -> set[str]:
    """One-part table names ``body`` reads that none of its own WITH tables binds: what an outer WITH could capture."""

    return {
        t.name.lower() for t in body.find_all(exp.Table)
        if t.name and not t.args.get("db") and not t.args.get("catalog") and t.name.lower() not in visible_ctes(t, body)
    }


def captured_names(body: exp.Expression, at: exp.Expression) -> set[str]:
    """Tables ``body`` reads by a one-part name that a WITH table in scope at ``at`` would capture if ``body``
    replaced ``at``."""

    return free_reads(body) & visible_ctes(at)


def field_path(column: exp.Column) -> tuple[str, ...]:
    """The struct fields read straight off a column, outermost first: ``a.b.c`` on column ``a`` gives ``("b", "c")``.

    Only a plain chain of field names counts. A subscript (``a[OFFSET(0)].b``), a function call or a ``.*`` ends the
    path there, so the read is attributed to the whole of what came before: unknown stays coarse, never guessed.
    """

    path: list[str] = []
    node: exp.Expression = column
    parent = node.parent
    while isinstance(parent, exp.Dot) and parent.this is node and isinstance(parent.expression, exp.Identifier):
        path.append(parent.expression.name)
        node = parent
        parent = node.parent
    return tuple(path)
