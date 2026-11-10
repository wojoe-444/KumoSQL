"""Incremental-versus-full-refresh correctness for Dataform incremental tables.

A Dataform incremental table runs its query in full the first time and later
runs only the ``when(incremental(), ...)`` form, appending (or merging on
``uniqueKey``) into the existing table. It is *correct* when, after every
batch of source changes, the table equals what a full refresh would produce.

This module models that run cycle in DuckDB and offers three things:

* :func:`parse_incremental_sqlx` reads a SQLX incremental model into its full
  and incremental queries (``when(incremental(), ...)``, ``${self()}``,
  ``${ref()}``, ``uniqueKey``, incremental ``pre_operations``).
* :class:`Simulation` replays batches of source DML and compares the
  incrementally maintained table with a full recompute after each one.
* :func:`check_incremental` decides, without a script, whether a model stays
  correct under a *contract* (the kinds of source change allowed). It answers
  ``safe`` only through a proof rule, ``diverges`` only with a minimised
  counterexample that replays in the simulator, and ``unknown`` otherwise.

BigQuery behaviour is explicit, not inherited from DuckDB: ``MERGE`` fails when
several source rows match one target row, NULL keys never match, and
``CURRENT_TIMESTAMP`` is pinned to a per-run clock so audit columns can be
excluded by name instead of making every run differ.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import datetime as dt
import random
import re
import time
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import spell_for_duckdb
from kumo_sql_prover.duckdb_load import small_database
from kumo_sql_prover.sqlx import _find_interpolation_end, split_sqlx_sections


class IncrementalError(ValueError):
    """The model or workload is outside what the simulator can run."""


class MergeConflict(IncrementalError):
    """BigQuery MERGE would fail: a target row matches several source rows.

    This is modelled BigQuery behaviour, so it counts as the run failing; every
    other :class:`IncrementalError` means the simulator could not run the model.
    """


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncrementalModel:
    """One incremental table: the SQL of its first/full run and of later runs."""

    target: str
    full_sql: str
    incremental_sql: str
    unique_key: tuple[str, ...] = ()
    # Statements from ``pre_operations`` that run on incremental runs (script variables and DML on the table).
    pre_operations: tuple[str, ...] = ()
    # Output columns allowed to differ (audit columns such as a load timestamp).
    ignore_columns: tuple[str, ...] = ()
    # sqlglot dialect the query and pre_operations are written in.
    dialect: str = "bigquery"
    # ``bigquery.updatePartitionFilter``: Dataform adds ``AND DATAFORM_DEST.<filter>`` to the MERGE condition,
    # so a target row outside it is never matched (a changed row then lands as a second copy).
    update_partition_filter: str = ""
    # Settings that change what an incremental run executes and that are not modelled (an ``insert_overwrite``
    # strategy, ``post_operations``, a ``uniqueKey`` that is not a literal list, ...). With any of them no
    # proof rule applies and the simulator refuses the model, so the answer is ``unsupported``, never ``safe``.
    unmodelled: tuple[str, ...] = ()
    # Statements from ``pre_operations`` that run on full builds. Only script variables (``DECLARE``/``SET``)
    # are modelled there; any other statement makes the model ``unmodelled``.
    full_pre_operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceTable:
    """A table the model reads. ``time_column`` is its event-time column, if any."""

    columns: dict[str, str]
    key: tuple[str, ...] = ()
    time_column: str | None = None
    # DuckDB default expressions for columns the source DML does not set.
    defaults: dict[str, str] = field(default_factory=dict)


_STRING = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`'
_CONFIG_STR = re.compile(rf"\b{{key}}\s*:\s*(?:{_STRING})")


def _js_string(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] in "'\"`" and token[-1] == token[0]:
        return token[1:-1]
    return token


def _split_args(text: str) -> list[str]:
    """Split call arguments on top-level commas, respecting quotes and brackets."""

    args: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            current.append(c)
            if c == "\\" and i + 1 < len(text):
                i += 1
                current.append(text[i])
            elif c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
            current.append(c)
        elif c in "([{":
            depth += 1
            current.append(c)
        elif c in ")]}":
            depth -= 1
            current.append(c)
        elif c == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    if "".join(current).strip():
        args.append("".join(current).strip())
    return args


def _resolve(text: str, target: str, incremental: bool) -> str:
    """Evaluate the SQLX interpolations the incremental pattern uses."""

    out: list[str] = []
    cursor = 0
    while True:
        opening = text.find("${", cursor)
        if opening < 0:
            out.append(text[cursor:])
            break
        out.append(text[cursor:opening])
        closing = _find_interpolation_end(text, opening)
        out.append(_evaluate(text[opening + 2 : closing].strip(), target, incremental))
        cursor = closing + 1
    return "".join(out)


def _evaluate(expression: str, target: str, incremental: bool) -> str:
    if re.fullmatch(r"self\(\s*\)", expression):
        return target
    ref = re.fullmatch(r"ref\(\s*((?:%s)(?:\s*,\s*(?:%s))*)\s*\)" % (_STRING, _STRING), expression)
    if ref:
        return _js_string(_split_args(ref.group(1))[-1])
    ref_object = re.fullmatch(r"ref\(\s*\{.*\bname\s*:\s*(%s).*\}\s*\)" % _STRING, expression, re.DOTALL)
    if ref_object:
        return _js_string(ref_object.group(1))
    when = re.fullmatch(r"when\((.*)\)", expression, re.DOTALL)
    if when:
        args = _split_args(when.group(1))
        if len(args) not in (2, 3):
            raise IncrementalError("when() takes two or three arguments")
        condition = args[0].replace(" ", "")
        if condition == "incremental()":
            chosen = incremental
        elif condition == "!incremental()":
            chosen = not incremental
        else:
            raise IncrementalError(f"unsupported when() condition: {args[0]}")
        branch = args[1] if chosen else (args[2] if len(args) == 3 else '""')
        return _resolve(_js_string(branch), target, incremental)
    if expression in {"incremental()", "!incremental()"}:
        raise IncrementalError("a bare incremental() is only supported inside when()")
    raise IncrementalError(f"unsupported SQLX interpolation: ${{{expression}}}")


_SEPARATOR = re.compile(r"(?m)^[ \t]*---[ \t]*$")


def split_statements(text: str) -> list[str]:
    """The SQL statements of a ``pre_operations``/``post_operations`` block.

    Statements are separated by ``;`` or by a line holding only ``---`` (SQLX's
    separator); quotes, brackets and comments are respected, and a statement
    that is only a comment is dropped.
    """

    out: list[str] = []
    for chunk in _SEPARATOR.split(text):
        current: list[str] = []
        depth = 0
        quote: str | None = None
        i = 0
        while i < len(chunk):
            c = chunk[i]
            nxt = chunk[i + 1] if i + 1 < len(chunk) else ""
            if quote:
                current.append(c)
                if c == "\\" and nxt:
                    current.append(nxt)
                    i += 1
                elif c == quote:
                    quote = None
            elif c == "-" and nxt == "-" or c == "#":
                end = chunk.find("\n", i)
                end = len(chunk) if end < 0 else end
                current.append(chunk[i:end])
                i = end
                continue
            elif c == "/" and nxt == "*":
                end = chunk.find("*/", i + 2)
                end = len(chunk) if end < 0 else end + 2
                current.append(chunk[i:end])
                i = end
                continue
            elif c in "'\"`":
                quote = c
                current.append(c)
            elif c in "([":
                depth += 1
                current.append(c)
            elif c in ")]":
                depth -= 1
                current.append(c)
            elif c == ";" and depth == 0:
                out.append("".join(current))
                current = []
            else:
                current.append(c)
            i += 1
        out.append("".join(current))
    return [s.strip() for s in out if _without_comments(s).strip()]


def _without_comments(statement: str) -> str:
    return re.sub(r"/\*.*?\*/|--[^\n]*|#[^\n]*", " ", statement, flags=re.DOTALL)


_PERMISSIONS = re.compile(r"(?is)^\s*(?:GRANT|REVOKE)\s")
_SET_OPTIONS = re.compile(
    r"(?is)^\s*ALTER\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|SCHEMA)\s+(?:IF\s+EXISTS\s+)?[^\s(]+\s+"
    r"(?:ALTER\s+COLUMN\s+(?:IF\s+EXISTS\s+)?[^\s(]+\s+)?SET\s+OPTIONS\s*\((?P<options>.*)\)\s*$"
)


def neutral_statement(statement: str) -> bool:
    """A statement that cannot change any table's rows: ``GRANT``/``REVOKE``, or ``ALTER ... SET OPTIONS``.

    Only a single ``SET OPTIONS (...)`` action is accepted, so ``ALTER TABLE t SET OPTIONS (...), ALTER
    COLUMN c SET DATA TYPE ...`` (which can change values) is not neutral.
    """

    text = _without_comments(statement)
    if _PERMISSIONS.match(text):
        return True
    found = _SET_OPTIONS.match(text)
    if not found:
        return False
    depth = 0
    quote: str | None = None
    for c in found.group("options"):
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False  # the options list closed early: another action follows
    return depth == 0 and quote is None


@dataclass(frozen=True)
class VariableStatement:
    """``DECLARE a[, b] [type] [DEFAULT expr]`` or ``SET a = expr`` in a BigQuery script."""

    verb: str  # declare | set
    names: tuple[str, ...]
    kind: str | None  # declared type (BigQuery spelling)
    value: exp.Expression | None  # DEFAULT or SET expression; None declares NULL


def variable_statement(statement: str, dialect: str = "bigquery") -> VariableStatement | None:
    """The script-variable statement ``statement`` is, or None when it is something else."""

    if not re.match(r"(?is)^\s*(?:DECLARE|SET)\s", _without_comments(statement)):
        return None
    try:
        tree = sqlglot.parse_one(statement, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    if isinstance(tree, exp.Declare) and len(tree.expressions) == 1:
        item = tree.expressions[0]
        names = item.this if isinstance(item.this, list) else [item.this]
        if not names or not all(isinstance(n, exp.Identifier) for n in names):
            return None
        kind = item.args.get("kind")
        default = item.args.get("default")
        return VariableStatement(
            "declare",
            tuple(n.name.lower() for n in names),
            kind.sql(dialect="bigquery") if isinstance(kind, exp.DataType) else None,
            default if isinstance(default, exp.Expression) else None,
        )
    if isinstance(tree, exp.Set) and len(tree.expressions) == 1:
        item = tree.expressions[0]
        eq = item.this if isinstance(item, exp.SetItem) else None
        if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Column) and not eq.this.table and isinstance(eq.expression, exp.Expression):
            return VariableStatement("set", (eq.this.name.lower(),), None, eq.expression)
    return None


def _config_list(config: str, key: str) -> tuple[str, ...]:
    found = re.search(rf"\b{key}\s*:\s*(\[[^\]]*\]|{_STRING})", config)
    if not found:
        return ()
    value = found.group(1)
    if value.startswith("["):
        return tuple(_js_string(a) for a in _split_args(value[1:-1]))
    return (_js_string(value),)


def parse_incremental_sqlx(sqlx: str, target: str) -> IncrementalModel:
    """Read a SQLX incremental model. ``target`` is what ``${self()}`` resolves to."""

    sections = split_sqlx_sections(sqlx)
    config = next((t for kind, t in sections if kind == "block" and t.lstrip().startswith("config")), "")
    if config and not re.search(r"\btype\s*:\s*[\"']incremental[\"']", config):
        raise IncrementalError("config type is not incremental")
    body = "".join(t for kind, t in sections if kind == "sql").strip()
    pre_blocks = [t for kind, t in sections if kind == "block" and t.lstrip().startswith("pre_operations")]
    pre: list[str] = []
    full_pre: list[str] = []
    unmodelled: list[str] = []
    for block in pre_blocks:
        inner = block[block.index("{") + 1 : block.rindex("}")]
        # Dataform runs ``incrementalPreOps`` (rendered with incremental() true) before an incremental run and
        # ``preOps`` before a full build, in one script with the table statement, so a DECLARE is visible to it.
        # Statements that cannot change rows (GRANT, ALTER ... SET OPTIONS) are dropped.
        pre.extend(s for s in split_statements(_resolve(inner, target, True)) if not neutral_statement(s))
        for statement in split_statements(_resolve(inner, target, False)):
            if neutral_statement(statement):
                continue
            if variable_statement(statement) is None:
                unmodelled.append("pre_operations that change data on full builds")
                break
            full_pre.append(statement)
    for block in (t for kind, t in sections if kind == "block" and t.lstrip().startswith("post_operations")):
        inner = block[block.index("{") + 1 : block.rindex("}")]
        # After the table statement only a statement that changes rows matters; setting a variable does not.
        statements = split_statements(_resolve(inner, target, True)) + split_statements(_resolve(inner, target, False))
        if any(not neutral_statement(s) and variable_statement(s) is None for s in statements):
            unmodelled.append("post_operations that change data")
            break
    unique_key = _config_list(config, "uniqueKey")
    if not unique_key and re.search(r"\buniqueKey\s*:", config):
        unmodelled.append("a uniqueKey that is not a literal list of names")
    partition_filter = ""
    if re.search(r"\bupdatePartitionFilter\s*:", config):
        found = re.search(rf"\bupdatePartitionFilter\s*:\s*({_STRING})\s*[,}}\n]", config)
        if found is None:
            unmodelled.append("an updatePartitionFilter that is not a plain string")
        elif unique_key:  # without a uniqueKey the run appends and the filter is never used
            partition_filter = _js_string(found.group(1)).strip()
    strategy = re.search(rf"\b(?:incrementalStrategy|strategy)\s*:\s*({_STRING})?", config)
    if strategy:
        value = _js_string(strategy.group(1) or "").strip().lower()
        if not ((value == "merge" and unique_key) or (value == "append" and not unique_key)):
            unmodelled.append(f"incremental strategy {value or '(not a plain string)'}")
    if re.search(r"\bincrementalPredicates?\s*:", config):
        unmodelled.append("incrementalPredicates")
    return IncrementalModel(
        target=target,
        full_sql=_resolve(body, target, False).strip().rstrip(";"),
        incremental_sql=_resolve(body, target, True).strip().rstrip(";"),
        unique_key=unique_key,
        pre_operations=tuple(pre),
        update_partition_filter=partition_filter,
        unmodelled=tuple(dict.fromkeys(unmodelled)),
        full_pre_operations=tuple(full_pre),
    )


_RUN_DEPENDENT = tuple(
    getattr(exp, name)
    for name in ("Rand", "CurrentTimestamp", "CurrentDate", "CurrentDatetime", "CurrentTime", "Uuid")
    if hasattr(exp, name)
)


def _definition_expression(value: exp.Expression) -> exp.Expression:
    """A variable's value as an expression to put where the variable is read.

    ``(SELECT x)`` becomes ``x``, and ``(SELECT COALESCE(agg, d) FROM t ...)`` becomes ``COALESCE((SELECT agg
    FROM t ...), d)``: an aggregate query with no ``GROUP BY`` or ``HAVING`` returns exactly one row, so the two
    are equal.
    """

    value = value.copy()
    if isinstance(value, exp.Subquery) and isinstance(value.this, exp.Select) and not any(
        v for k, v in value.args.items() if k != "this"
    ):
        inner = value.this
        others = {k for k, v in inner.args.items() if v and k not in ("expressions", "from", "from_", "where", "joins")}
        if len(inner.expressions) == 1 and not others:
            only = inner.expressions[0].unalias()
            if not (inner.args.get("from") or inner.args.get("from_")) and not inner.args.get("where"):
                value = only
            elif isinstance(only, exp.Coalesce) and isinstance(only.this, exp.AggFunc) and not any(
                isinstance(n, (exp.Column, exp.Subquery, exp.Select)) for e in only.expressions for n in e.walk()
            ):
                one = inner.copy()
                one.set("expressions", [only.this.copy()])
                value = exp.Coalesce(this=exp.Subquery(this=one), expressions=[e.copy() for e in only.expressions])
    return exp.Paren(this=value) if isinstance(value, (exp.Binary, exp.Unary)) else value


def _written_tables(tree: exp.Expression) -> set[str]:
    if isinstance(tree, (exp.Delete, exp.Update, exp.Insert, exp.Merge)):
        written = tree.this
        if isinstance(written, exp.Schema):
            written = written.this
        if isinstance(written, exp.Table):
            return {written.name.lower()}
    return {t.name.lower() for t in tree.find_all(exp.Table)}


def _substitute_script(statements: Iterable[str], query: str, dialect: str) -> tuple[str, list[str]] | None:
    """``query`` with each script variable it reads replaced by the variable's definition, and the
    remaining (non-variable) statements likewise; None when a read value is not that of its definition at
    the point of reading (a later statement wrote a table the definition reads, or the definition is
    run-dependent, such as RAND or the clock)."""

    parsed = [(s, variable_statement(s, dialect)) for s in statements]
    declared = {n for _, v in parsed if v is not None for n in v.names}
    env: dict[str, exp.Expression | None] = {}
    reads: dict[str, set[str]] = {}
    kinds: dict[str, str | None] = {}

    def substitute(tree: exp.Expression) -> exp.Expression | None:
        refs = _variable_references(tree, declared)
        if any(env.get(r) is None for r in refs):
            return None
        return tree.transform(
            lambda n: env[n.name.lower()].copy() if isinstance(n, exp.Column) and not n.table and n.name.lower() in refs else n
        )

    rest: list[str] = []
    for statement, variable in parsed:
        if variable is None:
            try:
                tree = sqlglot.parse_one(statement, read=dialect)
            except sqlglot.errors.SqlglotError:
                return None
            bound = substitute(tree)
            if bound is None:
                return None
            rest.append(bound.sql(dialect=dialect))
            written = _written_tables(tree)
            for name, tables in reads.items():
                if tables & written:
                    env[name] = None
            continue
        value = substitute(variable.value) if variable.value is not None else exp.Null()
        for name in variable.names:
            if variable.verb == "declare":
                kinds[name] = variable.kind
            if value is None or any(isinstance(n, _RUN_DEPENDENT) for n in value.walk()):
                env[name] = None
                continue
            kind = kinds.get(name)
            definition = _definition_expression(value)
            env[name] = exp.cast(definition, exp.DataType.build(kind, dialect="bigquery")) if kind else definition
            reads[name] = {t.name.lower() for t in value.find_all(exp.Table)}
    try:
        tree = sqlglot.parse_one(query, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    bound = substitute(tree)
    return None if bound is None else (bound.sql(dialect=dialect), rest)


def effective_model(model: IncrementalModel) -> IncrementalModel:
    """``model`` with script variables replaced by their definitions, which is what the proof rules read.

    A Dataform pre-operation such as ``DECLARE wm DEFAULT (SELECT MAX(ts) FROM ${self()})`` runs in the same
    script as the query, so ``WHERE ts > wm`` reads the value of that subquery at that point. Replacing the
    variable by its definition is exact when nothing between the two writes a table the definition reads
    and the definition is not run-dependent; otherwise the model is returned unchanged (its pre-operations
    then keep every proof rule from applying). DML pre-operations stay, with variables replaced.
    """

    variables = [s for s in model.pre_operations if variable_statement(s, model.dialect) is not None]
    if not variables and not model.full_pre_operations:
        return model
    full = _substitute_script(model.full_pre_operations, model.full_sql, model.dialect)
    incremental = _substitute_script(model.pre_operations, model.incremental_sql, model.dialect)
    if full is None or incremental is None or full[1]:
        return model
    return replace(
        model,
        full_sql=full[0],
        incremental_sql=incremental[0],
        pre_operations=tuple(incremental[1]),
        full_pre_operations=(),
    )


def modelled_exactly(model: IncrementalModel) -> bool:
    """Whether the proof rules may read the model as plain append (no key) or MERGE on its key."""

    return not model.unmodelled and not model.update_partition_filter


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

_DUCK_TYPES = {
    "INT64": "BIGINT",
    "INTEGER": "BIGINT",
    "FLOAT64": "DOUBLE",
    "NUMERIC": "DECIMAL(18,6)",
    "STRING": "VARCHAR",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "DATETIME": "TIMESTAMP",
}

_EPOCH = dt.datetime(2024, 1, 1)


def _connect():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise IncrementalError("duckdb is required; install kumosql[execution]") from exc
    return small_database()


def _to_duckdb(sql: str, clock: dt.datetime, read: str = "bigquery", bound: dict[str, str] | None = None) -> str:
    """Model SQL (BigQuery) or source DML (DuckDB/ANSI) to DuckDB, clock pinned.

    ``bound`` maps script variables to the one-row tables holding their values.
    """

    try:
        tree = sqlglot.parse_one(sql, read=read)
    except sqlglot.errors.SqlglotError as exc:
        raise IncrementalError(f"cannot parse: {exc}") from exc
    if bound:
        tree = _bind_variables(tree, bound)
    return _pin_clock(tree, clock, read).sql(dialect="duckdb")


def _variable_references(node: exp.Expression | None, names: set[str] | frozenset[str]) -> set[str]:
    """Script variables ``node`` reads: unqualified column names that are declared variables."""

    if node is None or not names:
        return set()
    return {c.name.lower() for c in node.find_all(exp.Column) if not c.table and c.name.lower() in names}


def _bind_variables(tree: exp.Expression, bound: dict[str, str]) -> exp.Expression:
    def bind(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table and node.name.lower() in bound:
            return exp.Subquery(this=exp.select("v").from_(exp.to_table(bound[node.name.lower()])))
        return node

    return tree.transform(bind)


def _script(statements: Iterable[str], query: str, dialect: str) -> list[tuple[str, VariableStatement | None]]:
    """The statements of one run that matter for ``query``: every DML statement, and each variable
    statement whose value is read later (by ``query`` or by a statement that runs). A dead ``DECLARE``
    is not evaluated, so one that reads the table before its first build is not run here (BigQuery would
    still evaluate it)."""

    parsed = [(s, variable_statement(s, dialect)) for s in statements]
    declared = {n for _, v in parsed if v is not None for n in v.names}
    try:
        live = _variable_references(sqlglot.parse_one(query, read=dialect), declared)
    except sqlglot.errors.SqlglotError:
        live = set(declared)
    keep: list[tuple[str, VariableStatement | None]] = []
    for statement, variable in reversed(parsed):
        if variable is None:
            keep.append((statement, None))
            try:
                live |= _variable_references(sqlglot.parse_one(statement, read=dialect), declared)
            except sqlglot.errors.SqlglotError:
                live |= declared
        elif set(variable.names) & live:
            keep.append((statement, variable))
            live -= set(variable.names)
            live |= _variable_references(variable.value, declared)
    return keep[::-1]


def _pin_clock(tree: exp.Expression, clock: dt.datetime, read: str) -> exp.Expression:
    literal = f"{clock:%Y-%m-%d %H:%M:%S}"

    def pin(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and read != "duckdb" and (node.args.get("db") or node.args.get("catalog")):
            # tables are local DuckDB tables named by their last part
            return exp.Table(this=node.this, alias=node.args.get("alias"))
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDatetime)):
            return exp.cast(exp.Literal.string(literal), "timestamp")
        if isinstance(node, exp.CurrentDate):
            return exp.cast(exp.Literal.string(literal[:10]), "date")
        return node

    tree = tree.transform(pin)
    if read == "bigquery":
        from kumo_sql_prover.bigquery_on_duckdb import faithful

        try:
            tree = faithful(tree)
        except sqlglot.errors.SqlglotError as exc:
            raise IncrementalError(f"cannot run as BigQuery does: {exc}") from exc
    return spell_for_duckdb(tree)


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_norm(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _norm(v)) for k, v in value.items()))
    if isinstance(value, float):
        return round(value, 9)
    if hasattr(value, "normalize") and hasattr(value, "as_tuple"):
        return float(value)
    return value


def _sort_key(row: tuple) -> tuple:
    return tuple((v is None, type(v).__name__, repr(v)) for v in row)


@dataclass
class BatchResult:
    index: int
    status: str  # agree | diverge | error
    only_incremental: tuple[tuple, ...] = ()
    only_full: tuple[tuple, ...] = ()
    error: str = ""


class Simulation:
    """Replay source changes and compare incremental output with full recompute."""

    def __init__(
        self,
        model: IncrementalModel,
        sources: dict[str, SourceTable],
        initial: Iterable[str] = (),
    ):
        if model.unmodelled:
            raise IncrementalError("the simulator does not model " + ", ".join(model.unmodelled))
        self.model = model
        self.sources = sources
        self.con = _connect()
        if model.dialect == "bigquery":
            from kumo_sql_prover.bigquery_on_duckdb import configure

            configure(self.con)
        self.clock = dt.datetime(2030, 1, 1)
        self.runs = 0
        self.failed: str | None = None
        for name, table in sources.items():
            cols = ", ".join(
                f'"{c}" {_DUCK_TYPES.get(t.upper(), t)}' + (f" DEFAULT {table.defaults[c]}" if c in table.defaults else "")
                for c, t in table.columns.items()
            )
            if table.defaults:
                self.con.execute("CREATE SEQUENCE IF NOT EXISTS _load_seq")
            self.con.execute(f'CREATE TABLE "{name}" ({cols})')
        self._check_variables()
        for statement in initial:
            self._dml(statement)

    def _check_variables(self) -> None:
        m = self.model
        statements = [*m.pre_operations, *m.full_pre_operations]
        names = {n for s in statements if (v := variable_statement(s, m.dialect)) is not None for n in v.names}
        columns = {c.lower() for table in self.sources.values() for c in table.columns}
        if names & columns:
            raise IncrementalError(f"a script variable shares its name with a column: {sorted(names & columns)[0]}")
        for _, variable in _script(m.full_pre_operations, m.full_sql, m.dialect):
            read = {t.name.lower() for t in variable.value.find_all(exp.Table)} if variable and variable.value else set()
            if m.target.lower() in read:
                raise IncrementalError("a full-build pre-operation reads the table before it is built")

    def _run_script(self, statements: Iterable[str], query: str, prefix: str) -> str:
        """Run one run's pre-operations in order and return its query as DuckDB SQL, variables bound."""

        m = self.model
        bound: dict[str, str] = {}
        kinds: dict[str, str | None] = {}
        for statement, variable in _script(statements, query, m.dialect):
            if variable is None:
                self.con.execute(_to_duckdb(statement, self.clock, m.dialect, bound))
                continue
            value = variable.value if variable.value is not None else exp.Null()
            for name in variable.names:
                if variable.verb == "declare":
                    kinds[name] = variable.kind
                select = _bind_variables(exp.select(value.copy().as_("v")), bound)
                duck = _pin_clock(select, self.clock, m.dialect).sql(dialect="duckdb")
                kind = (kinds.get(name) or "").upper()
                if kind in _DUCK_TYPES:
                    duck = f"SELECT CAST(v AS {_DUCK_TYPES[kind]}) AS v FROM ({duck})"
                table = f"__{prefix}_{name}"
                self.con.execute(f'CREATE OR REPLACE TEMP TABLE "{table}" AS {duck}')
                bound[name] = table
        return _to_duckdb(query, self.clock, m.dialect, bound)

    # -- statements -------------------------------------------------------

    def _dml(self, statement: str) -> None:
        try:
            self.con.execute(_to_duckdb(statement, self.clock, "duckdb"))
        except Exception as exc:
            raise IncrementalError(f"source statement failed: {exc}") from exc

    def _full_query(self) -> str:
        """The full refresh as DuckDB SQL, after the full-build pre-operations (script variables only)."""

        return self._run_script(self.model.full_pre_operations, self.model.full_sql, "fullvar")

    # -- the Dataform run cycle ------------------------------------------

    def run_incremental(self) -> None:
        """First run: full build. Later runs: the Dataform incremental statement."""

        m = self.model
        self.clock += dt.timedelta(hours=1)
        if self.runs == 0:
            self.con.execute(f'CREATE OR REPLACE TABLE "{m.target}" AS {self._full_query()}')
            self.runs += 1
            return
        self.runs += 1
        incremental = self._run_script(m.pre_operations, m.incremental_sql, "var")
        self.con.execute(f"CREATE OR REPLACE TEMP TABLE __incr AS {incremental}")
        columns = [r[0] for r in self.con.execute(f'SELECT column_name FROM information_schema.columns WHERE table_name = \'{m.target}\' ORDER BY ordinal_position').fetchall()]
        collist = ", ".join(f'"{c}"' for c in columns)
        if not m.unique_key:
            self.con.execute(f'INSERT INTO "{m.target}" ({collist}) SELECT {collist} FROM __incr')
            return
        # Dataform's MERGE: ON the key (plus ``DATAFORM_DEST.<updatePartitionFilter>``), WHEN MATCHED UPDATE
        # every column, WHEN NOT MATCHED INSERT. Matches are decided once, against the table before the run.
        on = " AND ".join(f't."{k}" = s."{k}"' for k in m.unique_key)
        if m.update_partition_filter:
            on += f" AND ({self._destination_condition(m.update_partition_filter)})"
        # BigQuery MERGE raises when one target row matches several source rows.
        dup = self.con.execute(
            f'SELECT count(*) FROM (SELECT t.rowid FROM "{m.target}" t JOIN __incr s ON {on} GROUP BY t.rowid HAVING count(*) > 1)'
        ).fetchone()[0]
        if dup:
            raise MergeConflict("MERGE: a target row matches more than one source row")
        self.con.execute(
            f'CREATE OR REPLACE TEMP TABLE __unmatched AS SELECT * FROM __incr s WHERE NOT EXISTS (SELECT 1 FROM "{m.target}" t WHERE {on})'
        )
        # Every matched target row is updated in place, so copies of a row already in the table stay copies.
        assignments = ", ".join(f'"{c}" = s."{c}"' for c in columns)
        self.con.execute(f'UPDATE "{m.target}" AS t SET {assignments} FROM __incr AS s WHERE {on}')
        self.con.execute(f'INSERT INTO "{m.target}" ({collist}) SELECT {collist} FROM __unmatched')

    def _destination_condition(self, text: str) -> str:
        """``DATAFORM_DEST.<text>`` as Dataform writes it, read against the target alias ``t``."""

        try:
            condition = sqlglot.parse_one(f"t.{text}", read=self.model.dialect)
        except sqlglot.errors.SqlglotError as exc:
            raise IncrementalError(f"cannot parse updatePartitionFilter: {exc}") from exc
        return _pin_clock(condition, self.clock, self.model.dialect).sql(dialect="duckdb")

    def full_rows(self) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(self._full_query())
        return [d[0] for d in cur.description], [tuple(map(_norm, r)) for r in cur.fetchall()]

    def target_rows(self) -> tuple[list[str], list[tuple]]:
        cur = self.con.execute(f'SELECT * FROM "{self.model.target}"')
        return [d[0] for d in cur.description], [tuple(map(_norm, r)) for r in cur.fetchall()]

    def compare(self, index: int) -> BatchResult:
        try:
            cols_f, full = self.full_rows()
            cols_t, got = self.target_rows()
        except Exception as exc:
            raise IncrementalError(f"cannot compare in DuckDB: {exc}") from exc
        keep = [i for i, c in enumerate(cols_f) if c.lower() not in {x.lower() for x in self.model.ignore_columns}]
        keep_t = [i for i, c in enumerate(cols_t) if c.lower() not in {x.lower() for x in self.model.ignore_columns}]
        if len(keep) != len(keep_t):
            return BatchResult(index, "diverge", error="column counts differ")
        full = [tuple(r[i] for i in keep) for r in full]
        got = [tuple(r[i] for i in keep_t) for r in got]
        a, b = Counter(got), Counter(full)
        extra, missing = a - b, b - a
        if extra or missing:
            return BatchResult(
                index,
                "diverge",
                tuple(sorted(extra.elements(), key=_sort_key))[:5],
                tuple(sorted(missing.elements(), key=_sort_key))[:5],
            )
        return BatchResult(index, "agree")

    # -- batches ----------------------------------------------------------

    def step(self, statements: Iterable[str], index: int) -> BatchResult:
        """Apply one batch of source DML, run the incremental table, compare."""

        for statement in statements:
            self._dml(statement)
        try:
            self.run_incremental()
        except MergeConflict as exc:
            return BatchResult(index, "error", error=str(exc))
        except IncrementalError:
            raise
        except Exception as exc:
            raise IncrementalError(f"cannot run the model in DuckDB: {exc}") from exc
        return self.compare(index)


def replay(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    initial: Iterable[str],
    batches: Iterable[Iterable[str]],
) -> list[BatchResult]:
    """Run every batch; the first diverging or failing batch ends the replay."""

    sim = Simulation(model, sources, initial)
    results = [sim.step((), 0)]
    if results[0].status != "agree":
        return results
    for i, batch in enumerate(batches, start=1):
        result = sim.step(batch, i)
        results.append(result)
        if result.status != "agree":
            break
    return results


def first_divergence(results: list[BatchResult]) -> BatchResult | None:
    return next((r for r in results if r.status != "agree"), None)


# ---------------------------------------------------------------------------
# Contracts and counterexample search
# ---------------------------------------------------------------------------

#: Kinds of source change a contract can allow.
CHANGE_KINDS = (
    "insert_new",  # new rows, event time after everything seen
    "insert_late",  # rows whose event time is before the newest seen
    "insert_boundary",  # rows whose event time equals the newest seen
    "duplicate",  # exact re-delivery of an existing row
    "update",  # change non-key columns of an existing row
    "update_touch",  # change a row and move its event time to the newest
    "delete",  # remove an existing row
    "null_key",  # a new row whose key column is NULL
    "empty",  # a run with no source change
)


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value:%Y-%m-%d %H:%M:%S}'"
    if isinstance(value, dt.date):
        return f"DATE '{value:%Y-%m-%d}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return repr(value)


def _random_value(rng: random.Random, sql_type: str) -> Any:
    t = sql_type.upper()
    if t in ("INT64", "INTEGER"):
        return rng.randint(0, 3)
    if t in ("FLOAT64", "NUMERIC"):
        return float(rng.randint(0, 3))
    if t == "STRING":
        return rng.choice("abc")
    if t == "BOOL":
        return rng.random() < 0.5
    if t == "DATE":
        return (_EPOCH + dt.timedelta(days=rng.randint(0, 3))).date()
    return _EPOCH + dt.timedelta(hours=rng.randint(0, 3))


def _where(table: SourceTable, row: dict[str, Any]) -> str:
    return " AND ".join(
        f'"{c}" IS NULL' if v is None else f'"{c}" = {_literal(v)}' for c, v in row.items()
    )


def _insert(name: str, table: SourceTable, row: dict[str, Any]) -> str:
    cols = ", ".join(f'"{c}"' for c in row)
    return f'INSERT INTO "{name}" ({cols}) VALUES ({", ".join(_literal(v) for v in row.values())})'


class _Generator:
    """Random batches under a contract, tracking the source tables it writes."""

    def __init__(
        self,
        sources: dict[str, SourceTable],
        kinds: frozenset[str],
        rng: random.Random,
        tables: tuple[str, ...] | None = None,
        gap_rng: random.Random | None = None,
        gap_rate: float = 0.0,
    ):
        self.sources = sources
        self.tables = tuple(sorted(tables if tables else sources))
        self.kinds = kinds
        self.rng = rng
        # gaps draw from their own stream, so adding them leaves every main-stream choice alone
        self.gap_rng = gap_rng if gap_rng is not None else random.Random(0)
        self.gap_rate = gap_rate
        self.rows: dict[str, list[dict[str, Any]]] = {n: [] for n in sources}
        self.next_key = {name: 0 for name in sources}
        self.next_hour = 0

    def _fresh(self, name: str, hour: int | None) -> dict[str, Any]:
        table = self.sources[name]
        row: dict[str, Any] = {}
        for column, sql_type in table.columns.items():
            if column in table.key:
                row[column] = self.next_key[name]
                self.next_key[name] += 1
            elif column == table.time_column:
                row[column] = _EPOCH + dt.timedelta(hours=self.next_hour if hour is None else hour)
            else:
                row[column] = _random_value(self.rng, sql_type)
        return row

    def _newest(self, name: str) -> int | None:
        tc = self.sources[name].time_column
        hours = [int((r[tc] - _EPOCH).total_seconds() // 3600) for r in self.rows[name] if tc and r.get(tc)]
        return max(hours) if hours else None

    def batch(self) -> list[str]:
        statements: list[str] = []
        for _ in range(self.rng.randint(1, 2)):
            op = self.rng.choice(sorted(self.kinds))
            if op == "empty":
                continue
            name = self.rng.choice(self.tables)
            table, rows = self.sources[name], self.rows[name]
            newest = self._newest(name)
            if op == "insert_new":
                self.next_hour = (newest + 1) if newest is not None else 0
                if self.gap_rate and newest is not None and self.gap_rng.random() < self.gap_rate:
                    self.next_hour = newest + self.gap_rng.randint(GAP_HOURS[0], GAP_HOURS[1])
                row = self._fresh(name, None)
            elif op == "null_key":
                self.next_hour = (newest + 1) if newest is not None else 0
                row = self._fresh(name, None)
                for column in table.key:
                    row[column] = None
            elif op == "insert_boundary":
                if newest is None:
                    continue
                row = self._fresh(name, newest)
            elif op == "insert_late":
                if not newest:
                    continue
                row = self._fresh(name, self.rng.randint(0, newest - 1))
            elif op == "duplicate":
                if not rows:
                    continue
                row = dict(self.rng.choice(rows))
            elif op in ("update", "update_touch"):
                if not rows:
                    continue
                old = self.rng.choice(rows)
                new = dict(old)
                for column, sql_type in table.columns.items():
                    if column in table.key or column == table.time_column:
                        continue
                    new[column] = _random_value(self.rng, sql_type)
                if op == "update_touch" and table.time_column:
                    new[table.time_column] = _EPOCH + dt.timedelta(hours=(newest or 0) + 1)
                if new == old:
                    continue
                assignments = ", ".join(f'"{c}" = {_literal(new[c])}' for c in new if new[c] != old[c])
                statements.append(f'UPDATE "{name}" SET {assignments} WHERE {_where(table, old)}')
                for i, r in enumerate(rows):
                    if r == old:
                        rows[i] = new
                continue
            elif op == "delete":
                if not rows:
                    continue
                old = self.rng.choice(rows)
                statements.append(f'DELETE FROM "{name}" WHERE {_where(table, old)}')
                rows[:] = [r for r in rows if r != old]
                continue
            else:  # pragma: no cover
                continue
            statements.append(_insert(name, table, row))
            rows.append(row)
        return statements


@dataclass(frozen=True)
class Counterexample:
    """A replayable source-change sequence on which the model diverges."""

    initial: tuple[str, ...]
    batches: tuple[tuple[str, ...], ...]
    batch_index: int
    status: str
    detail: str

    @property
    def size(self) -> int:
        return len(self.initial) + sum(len(b) for b in self.batches)


def _diverges(model, sources, initial, batches) -> BatchResult | None:
    try:
        return first_divergence(replay(model, sources, initial, batches))
    except IncrementalError:
        return None


def minimize(model, sources, initial, batches) -> tuple[list[str], list[list[str]]]:
    """Greedy shrink: drop whole batches, then single statements, while it still diverges."""

    initial, batches = list(initial), [list(b) for b in batches]

    def still(i, b):
        return _diverges(model, sources, i, b) is not None

    changed = True
    while changed:
        changed = False
        for i in range(len(batches) - 1, -1, -1):
            trial = batches[:i] + batches[i + 1 :]
            if still(initial, trial):
                batches, changed = trial, True
        for i in range(len(initial) - 1, -1, -1):
            trial = initial[:i] + initial[i + 1 :]
            if still(trial, batches):
                initial, changed = trial, True
        for bi in range(len(batches)):
            for si in range(len(batches[bi]) - 1, -1, -1):
                trial = [list(b) for b in batches]
                del trial[bi][si]
                if still(initial, trial):
                    batches, changed = trial, True
                    break
    # trailing batches after the first divergence are never needed
    result = _diverges(model, sources, initial, batches)
    if result is not None:
        batches = batches[: result.index]
    return initial, batches


GAP_RATE = 0.2  # share of ``insert_new`` rows that jump ahead of the previous maximum event time
GAP_HOURS = (3, 30)  # how far ahead (inclusive hours); wider than the windows real models delete and reload


def random_sequence(
    sources: dict[str, SourceTable],
    kinds: frozenset[str],
    seed: int,
    batches: int,
    tables: tuple[str, ...] | None = None,
    gap_rate: float = GAP_RATE,
) -> tuple[list[str], list[list[str]]]:
    """A random initial load and ``batches`` batches of source DML allowed by ``kinds``.

    About ``gap_rate`` of the ``insert_new`` rows land 3 to 30 hours after the
    newest row instead of one hour after it. The gap decisions come from a
    separate generator seeded from ``seed``, so with ``gap_rate=0`` the output is
    exactly the sequence earlier versions produced, and with gaps the choices
    of operation, table and column values are the same (only event times move).
    """

    rng = random.Random(seed)
    gap_rng = random.Random(f"gap:{seed}")
    gen = _Generator(sources, kinds, rng, tables, gap_rng, gap_rate)
    initial: list[str] = []
    for name in sorted(sources):
        for _ in range(rng.randint(0, 2)):
            gen.next_hour = rng.randint(0, 2)
            row = gen._fresh(name, None)
            initial.append(_insert(name, sources[name], row))
            gen.rows[name].append(row)
    return initial, [gen.batch() for _ in range(batches)]


def search_divergence(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    *,
    seeds: int = 60,
    batches: int = 4,
    seed: int = 0,
    tables: tuple[str, ...] | None = None,
    time_limit: float | None = None,
) -> Counterexample | None:
    """Look for a source-change sequence allowed by ``kinds`` that makes the model diverge."""

    kinds = frozenset(kinds)
    unknown = kinds - set(CHANGE_KINDS)
    if unknown:
        raise IncrementalError(f"unknown change kinds: {sorted(unknown)}")
    deadline = None if time_limit is None else time.monotonic() + time_limit
    for s in range(seeds):
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("counterexample search ran out of time")
        initial, plan = random_sequence(sources, kinds, seed * 100003 + s, batches, tables)
        try:
            result = first_divergence(replay(model, sources, initial, plan))
        except IncrementalError:
            continue
        if result is not None:
            small_i, small_b = minimize(model, sources, initial, plan)
            final = _diverges(model, sources, small_i, small_b)
            if final is None:  # pragma: no cover - minimisation keeps divergence
                continue
            detail = final.error or f"incremental has {len(final.only_incremental)} extra and {len(final.only_full)} missing rows"
            return Counterexample(tuple(small_i), tuple(tuple(b) for b in small_b), final.index, final.status, detail)
    return None


# ---------------------------------------------------------------------------
# Static proof rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    outcome: str  # safe | diverges | nondeterministic | unknown | unsupported | timeout
    rule: str
    detail: str = ""
    counterexample: Counterexample | None = None


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def _watermark_predicate(conjunct: exp.Expression, target: str, source_column: str, target_column: str) -> str | None:
    """``ts >[=] [TIMESTAMP_SUB(]COALESCE((SELECT MAX(col) FROM self), <old date>)[, ...)]``.

    Returns ``">"`` or ``">="``. A lookback (``TIMESTAMP_SUB``) only lowers the
    boundary, so it reads a superset of the rows the plain watermark would.
    """

    if not isinstance(conjunct, (exp.GT, exp.GTE)):
        return None
    left, right = conjunct.left, conjunct.right
    if not (isinstance(left, exp.Column) and left.name.lower() == source_column.lower()):
        return None
    lookback = False
    if isinstance(right, (exp.TimestampSub, exp.DatetimeSub)):
        # Only a constant, non-negative amount lowers the boundary; ``INTERVAL -1 DAY`` raises it.
        if not _nonnegative_amount(right):
            return None
        right, lookback = right.this, True
    # An empty table gives MAX = NULL and ``ts > NULL`` reads nothing, so only a
    # COALESCE to a date before any event keeps the first rows from being lost.
    if not isinstance(right, exp.Coalesce) or len(right.expressions) != 1 or not _old_date(right.expressions[0]):
        return None
    right = right.this
    if isinstance(right, exp.Paren):
        right = right.this
    if isinstance(right, exp.Subquery):
        if any(v for k, v in right.args.items() if k != "this"):
            return None
        right = right.this
    if not isinstance(right, exp.Select) or len(right.expressions) != 1:
        return None
    # Exactly ``SELECT MAX(col) FROM self``: a WHERE, HAVING, join, LIMIT, ... can change or remove the one
    # row the aggregate returns (``HAVING FALSE`` makes it NULL, so every run re-reads everything).
    if any(v for k, v in right.args.items() if k not in ("expressions", "from", "from_")):
        return None
    agg = right.expressions[0]
    if isinstance(agg, exp.Alias):
        agg = agg.this
    if not (isinstance(agg, exp.Max) and isinstance(agg.this, exp.Column) and agg.this.name.lower() == target_column.lower()):
        return None
    if any(v for k, v in agg.args.items() if k != "this"):
        return None
    source = right.args.get("from_") or right.args.get("from")
    table = source.this if source is not None else None
    if not (isinstance(table, exp.Table) and table.name.lower() == target.lower()):
        return None
    if any(v for k, v in table.args.items() if k not in ("this", "alias")):
        return None  # another dataset's table of that name, a snapshot (FOR SYSTEM_TIME AS OF), a sample, ...
    if agg.this.table and agg.this.table.lower() not in (table.alias_or_name.lower(),):
        return None  # MAX over an outer column, not the table's own
    return ">=" if isinstance(conjunct, exp.GTE) or lookback else ">"


def _nonnegative_amount(node: exp.Expression) -> bool:
    """``TIMESTAMP_SUB(x, INTERVAL n unit)`` with ``n`` a constant at least zero."""

    amount = node.args.get("expression")
    if isinstance(amount, exp.Interval):
        amount = amount.this
    while isinstance(amount, exp.Paren):
        amount = amount.this
    if not isinstance(amount, exp.Literal):
        return False
    try:
        return float(amount.this) >= 0
    except ValueError:
        return False


_DATE_WRAPPERS = tuple(
    getattr(exp, name)
    for name in ("Cast", "DataType", "Timestamp", "Date", "Datetime", "TsOrDsToDatetime", "TsOrDsToTimestamp", "TsOrDsToDate", "StrToTime")
    if hasattr(exp, name)
)


def _old_date(node: exp.Expression) -> bool:
    """A literal date or timestamp (``TIMESTAMP('1999-01-01')``, ``TIMESTAMP '1999-01-01'``, ...) no later than 2000."""

    literals = [n for n in node.walk() if isinstance(n, exp.Literal)]
    if len(literals) != 1 or not literals[0].is_string:
        return False
    if any(not isinstance(n, (exp.Literal, *_DATE_WRAPPERS)) for n in node.walk()):
        return False
    found = re.fullmatch(r"(\d{4})-\d{2}-\d{2}(?:[ T][0-9:.]+)?", literals[0].this.strip())
    return bool(found) and int(found.group(1)) <= 2000


def _row_wise(select: exp.Select, ignore: frozenset[str] = frozenset()) -> bool:
    """A single-table select-project-filter with no state across rows.

    ``CURRENT_TIMESTAMP`` is allowed only as the value of an ignored audit column.
    """

    if not isinstance(select, exp.Select) or select.args.get("with_") or select.args.get("with"):
        return False
    if any(select.args.get(k) for k in ("joins", "group", "having", "qualify", "distinct", "limit", "offset", "order", "windows", "laterals")):
        return False
    source = select.args.get("from_") or select.args.get("from")
    if source is None or not isinstance(source.this, exp.Table):
        return False
    clock = (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime)
    banned = (exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Rand, exp.Unnest) + clock
    for node in select.walk():
        if node is select:
            continue
        if isinstance(node, clock):
            audit = isinstance(node.parent, exp.Alias) and node.parent.alias.lower() in ignore and node.parent.parent is select
            if not audit:
                return False
        elif isinstance(node, banned):
            return False
    return True


def _strip(select: exp.Select, drop: exp.Expression) -> exp.Expression:
    copy = select.copy()
    where = copy.args.get("where")
    kept = [c for c in _conjuncts(where.this if where else None) if c.sql() != drop.sql()]
    if kept:
        condition = kept[0]
        for c in kept[1:]:
            condition = exp.And(this=condition, expression=c)
        copy.set("where", exp.Where(this=condition))
    else:
        copy.set("where", None)
    return copy


def _strip_old_bounds(select: exp.Select, tc: str) -> exp.Select:
    """``select`` without ``WHERE`` conjuncts ``tc >[=] <literal date no later than 2000>``.

    Such a bound is the full-build branch of a watermark variable (``SELECT TIMESTAMP '1970-01-01'``); under
    the watermark rules' assumption that the default precedes every event time, it keeps every row.
    """

    where = select.args.get("where")
    old = [
        c
        for c in _conjuncts(where.this if where else None)
        if isinstance(c, (exp.GT, exp.GTE)) and isinstance(c.left, exp.Column) and c.left.name.lower() == tc.lower() and _old_date(c.right)
    ]
    for conjunct in old:
        select = _strip(select, conjunct)
    return select


def _projected_as(select: exp.Select, column: str) -> str | None:
    """Output name under which ``column`` is selected unchanged, or None."""

    for e in select.expressions:
        if isinstance(e, exp.Star):
            return column
        if isinstance(e, exp.Column) and e.name.lower() == column.lower():
            return e.name
        if isinstance(e, exp.Alias) and isinstance(e.this, exp.Column) and e.this.name.lower() == column.lower():
            return e.alias
    return None


def prove_watermark(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: frozenset[str]
) -> Verdict | None:
    """Proof rules for a watermark over a row-wise query.

    The incremental query must be the full query plus one conjunct
    ``ts >[=] [TIMESTAMP_SUB(] COALESCE((SELECT MAX(ts) FROM self), <old date>) [...)]``,
    the full query a single-table select-project-filter, and ``ts`` projected
    unchanged. The COALESCE default is assumed to precede every event time, and
    ``CURRENT_TIMESTAMP`` may appear only as an ignored audit column. Then:

    * **R1, append.** No ``uniqueKey``, strict ``>``, only ``insert_new`` (and
      empty runs). Each new row's time exceeds every earlier one, so a run
      appends exactly the full query's output on the new rows.
    * **R2, merge.** ``uniqueKey`` equal to the source's declared key (projected
      unchanged, never NULL, unique), and only ``insert_new``, ``update_touch``
      and empty runs with ``>``, or also ``insert_boundary`` with ``>=`` (or a
      lookback). Every changed row has a time at or after the table's maximum,
      so it is re-read, and the merge replaces the row with its current output.
      Re-read unchanged rows merge to themselves; the key is unique in the
      source, so no merge has two source rows for one target row.
      ``update_touch`` needs a query without ``WHERE``: an update that makes a
      row fail the filter would leave its old version in the table.
    """

    if model.pre_operations or not modelled_exactly(model):
        return None
    try:
        full = sqlglot.parse_one(model.full_sql, read=model.dialect)
        incremental = sqlglot.parse_one(model.incremental_sql, read=model.dialect)
    except sqlglot.errors.SqlglotError:
        return None
    ignore = frozenset(c.lower() for c in model.ignore_columns)
    if not _row_wise(full, ignore) or not isinstance(incremental, exp.Select):
        return None
    source = full.args.get("from_") or full.args.get("from")
    table = sources.get(source.this.name) if source is not None else None
    if table is None or table.time_column is None:
        return None
    tc = table.time_column
    target_tc = _projected_as(full, tc)
    if target_tc is None:
        return None
    where = incremental.args.get("where")
    marks = [(c, _watermark_predicate(c, model.target, tc, target_tc)) for c in _conjuncts(where.this if where else None)]
    marks = [(c, op) for c, op in marks if op]
    if len(marks) != 1:
        return None
    conjunct, op = marks[0]
    full = _strip_old_bounds(full, tc)
    if _strip(incremental, conjunct).sql() != _strip(full, exp.Null()).sql():
        return None
    if not model.unique_key:
        if op == ">" and kinds <= {"insert_new", "empty"}:
            return Verdict("safe", "R1 append-only strict watermark", "row-wise query, strict watermark on the table's own time column, in-order inserts only")
        return None
    if set(model.unique_key) != set(table.key) or any(_projected_as(full, k) != k for k in table.key):
        return None
    allowed = {"insert_new", "update_touch", "empty"} | ({"insert_boundary"} if op == ">=" else set())
    if "update_touch" in kinds and full.args.get("where"):
        return None  # an update can move a row out of the filter, and a merge never deletes it
    if kinds <= allowed:
        return Verdict("safe", "R2 merge on a watermark", "row-wise query, merge on the source's unique key, every change at or after the table's newest time")
    return None


def prove(
    model: IncrementalModel, sources: dict[str, SourceTable], kinds: Iterable[str], tables: tuple[str, ...] | None = None
) -> Verdict | None:
    """A ``safe`` verdict from the first proof rule that applies (read on :func:`effective_model`), or None."""

    kinds = frozenset(kinds)
    proof_model = effective_model(model)
    proof = prove_watermark(proof_model, sources, kinds)
    if proof is None:
        from .incremental_rules import prove_more

        proof = prove_more(proof_model, sources, kinds, tables)
    return proof


def check_incremental(
    model: IncrementalModel,
    sources: dict[str, SourceTable],
    kinds: Iterable[str],
    *,
    seeds: int = 60,
    batches: int = 4,
    tables: tuple[str, ...] | None = None,
    time_limit: float | None = 30.0,
) -> Verdict:
    """Decide whether the model equals its full refresh under every change in ``kinds``.

    ``tables`` limits which source tables the changes may touch (default: all). A model whose full
    refresh depends on tie-breaking (shown by a witness, see :mod:`kumosql.incremental_ties`) is
    ``nondeterministic``: there is no single full refresh for it to equal.
    """

    from .incremental_ties import model_tie_reasons, tie_witness

    kinds = frozenset(kinds)
    if model.unmodelled:
        return Verdict("unsupported", "configuration", "not modelled: " + ", ".join(model.unmodelled))
    try:
        proof = prove(model, sources, kinds, tables)
        if proof is not None:
            return proof
        reasons = model_tie_reasons(model, sources, kinds, tables)
        witness = tie_witness(model, sources, kinds, reasons=reasons, seeds=seeds, batches=batches, tables=tables) if reasons else None
        if witness is not None:
            return Verdict("nondeterministic", "tie witness", witness.detail, witness)
        found = search_divergence(model, sources, kinds, seeds=seeds, batches=batches, tables=tables, time_limit=time_limit)
        if found is not None and reasons:
            # a divergence may only be a different tie-break: look for a witness on its own states
            witness = tie_witness(model, sources, kinds, reasons=reasons, seeds=0, sequences=[(found.initial, found.batches)])
            if witness is not None:
                return Verdict("nondeterministic", "tie witness", witness.detail, witness)
    except TimeoutError as exc:
        return Verdict("timeout", "counterexample search", str(exc))
    except IncrementalError as exc:
        return Verdict("unsupported", "simulator", str(exc))
    if found is not None:
        return Verdict("diverges", "counterexample search", found.detail, found)
    return Verdict("unknown", "counterexample search", f"no divergence in {seeds} random sequences; no proof rule applies")
