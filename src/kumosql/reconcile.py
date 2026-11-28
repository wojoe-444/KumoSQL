"""Reconcile two tables by key in one read-only query: every row matches, is explained by a named rule, or is unexplained.

    python -m kumosql reconcile --left proj.ds.orders_v1 --right proj.ds.orders_v2 --key order_id --rules rules.yml

The two sides are joined on the key (``FULL OUTER JOIN``). A joined row *matches* when both sides have
it and every compared column agrees (``IS NOT DISTINCT FROM``, so two NULLs agree). Any other row
(only on the left, only on the right, or a column differs) is tested against the known-difference
rules of :mod:`kumosql.reconcile_rules` in the order they are listed: the first rule whose predicate
holds explains it, and a row no rule explains is *unexplained*. All of it, with the per-column and
per-rule counts, is one SQL query, so BigQuery runs it as a single job; the column lists are read
first with dry runs, which are free.

Sample keys are off by default because key values can be sensitive: with ``include_samples=False``
no key value is selected or returned. With it on, up to ``sample_limit`` keys of *unexplained* rows
are returned per kind, never a row a rule already explains.

:func:`check_rules` re-runs the rules on current data in one query and flags a rule that no longer
explains any row, or that explains far more rows than when it was verified.

Every generated query passes :func:`kumosql.sql_validation.validate_readonly_query` before it runs,
on both engines, including its 20,000-character limit: a comparison too wide for it is refused with
a message that says how to narrow it, never sent unchecked.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

from . import data_profile
from .reconcile_rules import KnownDiffRule, RuleError, load_rules, parse_rules
from .scope_queries import MAX_QUERY_CHARS
from .sql_validation import quote_table_path, validate_readonly_query

__all__ = [
    "BigQueryExecutor", "ByteCapExceeded", "Column", "Crosswalk", "DEFAULT_GROWTH_FACTOR", "DEFAULT_MIN_GROWTH",
    "DEFAULT_SAMPLE_LIMIT", "DuckDBExecutor", "Input", "Link", "MAX_SAMPLE_LIMIT", "Plan", "ReconcileError",
    "ReconcileResult", "RuleError", "RulesCheckResult", "build_reconcile_sql", "check_rules", "estimate_bytes",
    "load_crosswalk", "plan_reconcile", "reconcile", "resolve_input",
]

DEFAULT_SAMPLE_LIMIT = 5
MAX_SAMPLE_LIMIT = 100
DEFAULT_GROWTH_FACTOR = 2.0
DEFAULT_MIN_GROWTH = 10
MAX_LABEL = 200
MARKER = "kumo_in"  # the column each side carries so the join can tell "no row" from "a row of NULLs"
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUERY_START = re.compile(r"^[\s(]*(select|with)\b", re.IGNORECASE)
_COMPLEX = ("ARRAY", "STRUCT", "RECORD", "JSON", "GEOGRAPHY", "RANGE")


class ReconcileError(ValueError):
    """The comparison could not be planned or run; the message says what to change."""


class ByteCapExceeded(ReconcileError):
    """BigQuery's dry run estimated more bytes than the cap allows; nothing was run."""


def _first_line(exc: BaseException) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0][:300]


def _wrapped(call, *args):
    try:
        return call(*args)
    except data_profile.ByteCapExceeded as exc:
        raise ByteCapExceeded(str(exc)) from None
    except data_profile.ProfileError as exc:
        raise ReconcileError(str(exc)) from None


def _ident(name: str, dialect: str) -> str:
    if dialect == "bigquery":
        if any(ch in name for ch in "`\\\n\r\0"):
            raise ReconcileError(f"a column name with a backtick, backslash or line break cannot be compared: {name!r}")
        return f"`{name}`"
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _readonly(sql: str) -> None:
    """The check every query KumoSQL runs passes, with its length limit stated plainly when that is what fails."""

    if len(sql) > MAX_QUERY_CHARS:
        raise ReconcileError(
            f"the comparison query is {len(sql):,} characters, over the {MAX_QUERY_CHARS:,}-character limit on every "
            "read-only query KumoSQL runs. Compare fewer columns (--ignore), leave sample keys off, or shorten the "
            "--left/--right SQL")
    try:
        validate_readonly_query(sql)
    except ValueError:
        raise ReconcileError("the comparison query did not pass the read-only check, so it was not run") from None


# ----------------------------------------------------------------- executors


@dataclass(frozen=True)
class Column:
    """One output column of a side: its name, type text and, for BigQuery, whether it is REPEATED."""

    name: str
    type: str = ""
    repeated: bool = False

    @property
    def complex(self) -> bool:
        """An array, STRUCT, JSON or GEOGRAPHY: BigQuery compares these as their JSON text."""

        return self.repeated or self.type.strip().upper().startswith(_COMPLEX)


class DuckDBExecutor(data_profile.DuckDBExecutor):
    """Runs the comparison on a DuckDB connection: tests, files (``read_csv_auto``) and local copies."""

    def table_sql(self, table: str) -> str:
        try:
            return super().table_sql(table)
        except data_profile.ProfileError as exc:
            raise ReconcileError(str(exc)) from None

    def columns(self, source: str) -> list[Column]:
        """The columns of ``source`` (a quoted table or a parenthesized query), read with ``DESCRIBE``."""

        try:
            rows = self.connection.execute(f"DESCRIBE SELECT * FROM {source} AS kumo_src").fetchall()
        except Exception as exc:  # noqa: BLE001 - surface DuckDB's reason
            raise ReconcileError(f"DuckDB could not read the columns: {_first_line(exc)}") from None
        return [Column(str(row[0]), str(row[1])) for row in rows]

    def run(self, sql: str) -> list[dict]:
        try:
            cursor = self.connection.execute(sql)
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise ReconcileError(f"DuckDB could not run the comparison: {_first_line(exc)}") from None


class BigQueryExecutor(data_profile.BigQueryExecutor):
    """Runs the comparison as one job in the billing project, dry-run first and capped in bytes billed.

    The same guards as :class:`kumosql.data_profile.BigQueryExecutor`: ``project`` is the billing project
    (default: the one chosen in Settings), ``max_bytes`` the cap (default: Settings → Scopes), and the job
    location is the one in Settings. Column lists are read with dry runs, which process nothing."""

    def __init__(self, project: str | None = None, max_bytes: int | None = None) -> None:
        try:
            super().__init__(project, max_bytes)
        except data_profile.ProfileError as exc:
            raise ReconcileError(str(exc)) from None

    def table_sql(self, table: str) -> str:
        parts = str(table).split(".")
        if len(parts) == 2 and all(_PLAIN.match(part) for part in parts):
            return f"`{table}`"  # dataset.table, read from the billing project
        try:
            return quote_table_path(table)
        except ValueError:
            raise ReconcileError("a BigQuery table is named project.dataset.table, or dataset.table for one in the "
                                 "billing project") from None

    def columns(self, source: str) -> list[Column]:
        from . import dryrun, query_context

        sql = f"SELECT * FROM {source} AS kumo_src"
        _readonly(sql)
        location = query_context.execution_location()
        try:
            result = dryrun.dry_run(sql, self.project, **({"location": location} if location else {}))
        except Exception as exc:  # noqa: BLE001 - credentials or network; nothing ran
            raise ReconcileError(f"BigQuery's dry run could not read the columns: {_first_line(exc)}") from None
        if not result.ok:
            raise ReconcileError(f"BigQuery rejected the input: {result.error_message}")
        if not result.schema_observed or not result.schema:
            raise ReconcileError("BigQuery's dry run returned no columns for the input")
        return [Column(item.name, item.type, item.mode == "REPEATED") for item in result.schema]

    def estimate(self, sql: str) -> int | None:
        return _wrapped(super().estimate, sql)

    def run(self, sql: str) -> list[dict]:
        return _wrapped(super().run, sql)


# -------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Input:
    """One side: what was given (``spec``, shortened), its kind, and the SQL that follows ``FROM``."""

    spec: str
    kind: str  # "table", "file" or "query"
    source: str
    table: str | None = None


def _label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_LABEL else text[:MAX_LABEL] + "…"


def _query(text: str, flag: str) -> str:
    """A read-only query ready to sit inside parentheses: checked, trailing semicolons dropped."""

    sql = text.strip()
    try:
        validate_readonly_query(sql)
    except ValueError as exc:
        raise ReconcileError(f"{flag}: {exc}") from None
    tokens = sqlglot.tokenize(sql, read="bigquery")
    while tokens and tokens[-1].token_type == TokenType.SEMICOLON:
        sql = sql[:tokens.pop().start].rstrip()
    return sql


def resolve_input(spec: str, executor, side: str = "left") -> Input:
    """A table name, a path to a ``.sql`` file, or an inline ``SELECT``/``WITH`` query."""

    flag = f"--{side}"
    if not isinstance(spec, str) or not spec.strip():
        raise ReconcileError(f"{flag} needs a table name, a .sql file or a SELECT query")
    text = spec.strip()
    if _QUERY_START.match(text):
        # On their own lines, so a trailing -- comment in the query cannot swallow the closing parenthesis.
        return Input(_label(text), "query", f"(\n{_query(text, flag)}\n)")
    if text.lower().endswith(".sql"):
        try:
            body = Path(text).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ReconcileError(f"{flag}: the SQL file {text} cannot be read ({type(exc).__name__})") from None
        return Input(_label(text), "file", f"(\n{_query(body, flag)}\n)")
    table = text.strip("`")
    return Input(table, "table", executor.table_sql(table), table)


# ----------------------------------------------------------------- crosswalk


@dataclass(frozen=True)
class Link:
    """A left column and the right column it corresponds to; ``status: missing`` (or no partner) skips it."""

    left: str | None
    right: str | None
    status: str = ""
    explanation: str = ""

    @property
    def missing(self) -> bool:
        return self.status.strip().lower() == "missing" or not self.left or not self.right


@dataclass(frozen=True)
class Crosswalk:
    source: str
    links: tuple[Link, ...] = ()
    notes: tuple[str, ...] = ()


def _table_parts(name: str) -> list[str]:
    return [part for part in str(name).strip().strip("`").lower().split(".") if part]


def _tables_match(a: str, b: str) -> bool:
    """``orders`` matches ``proj.ds.orders``: the shorter name is a suffix of the longer one."""

    pa, pb = _table_parts(a), _table_parts(b)
    if not pa or not pb:
        return False
    short, long = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    return long[-len(short):] == short


def _column_name(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReconcileError(f"{where}: a column must be a name or null")
    return value.strip().strip("`")


def _link(item: object, where: str, status: str, explanation: str) -> Link:
    if isinstance(item, (list, tuple)) and 1 <= len(item) <= 2:
        left = _column_name(item[0], where)
        right = _column_name(item[1], where) if len(item) == 2 else None
        own_status, own_explanation = "", ""
    elif isinstance(item, dict):
        left = _column_name(item.get("left", item.get("left_column")), where)
        right = _column_name(item.get("right", item.get("right_column")), where)
        own_status, own_explanation = item.get("status") or "", item.get("explanation") or ""
    else:
        raise ReconcileError(f"{where}: a column pair is [left, right] or {{left: ..., right: ...}}")
    if left is None and right is None:
        raise ReconcileError(f"{where}: a column pair names no column")
    own_status, own_explanation = own_status or status, own_explanation or explanation
    if not isinstance(own_status, str) or not isinstance(own_explanation, str):
        raise ReconcileError(f"{where}: status and explanation must be text")
    return Link(left, right, own_status.strip(), own_explanation.strip())


def _crosswalk_from_data(data: object, source: str, left_table: str | None, right_table: str | None) -> Crosswalk:
    if isinstance(data, dict):
        if isinstance(data.get("crosswalk"), list):
            entries = data["crosswalk"]
        elif isinstance(data.get("columns"), list):
            entries = [data]
        elif all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in data.items()):
            entries = [{"columns": [[k, v] for k, v in data.items()]}]
        else:
            raise ReconcileError(f"crosswalk {source}: expected a list of entries or a {{left column: right column}} mapping")
    elif isinstance(data, list):
        entries = data
    else:
        raise ReconcileError(f"crosswalk {source}: expected a list of entries or a {{left column: right column}} mapping")
    links: list[Link] = []
    notes: list[str] = []
    skipped = unchecked = 0
    for index, entry in enumerate(entries, 1):
        where = f"crosswalk {source}, entry {index}"
        if not isinstance(entry, dict):
            raise ReconcileError(f"{where} must be a mapping")
        if not isinstance(entry.get("columns"), list):  # a column link on its own: {left: col, right: col}
            links.append(_link(entry, where, "", ""))
            continue
        status, explanation = entry.get("status") or "", entry.get("explanation") or ""
        tables = entry.get("left"), entry.get("right")
        swap = False
        if all(isinstance(name, str) and name.strip() for name in tables):
            if left_table and right_table:
                if _tables_match(tables[0], left_table) and _tables_match(tables[1], right_table):
                    pass
                elif _tables_match(tables[0], right_table) and _tables_match(tables[1], left_table):
                    swap = True
                else:
                    skipped += 1
                    continue
            else:
                unchecked += 1
        for number, item in enumerate(entry["columns"], 1):
            link = _link(item, f"{where}, column {number}", status, explanation)
            links.append(Link(link.right, link.left, link.status, link.explanation) if swap else link)
    if skipped:
        notes.append(f"{skipped} crosswalk entr{'y' if skipped == 1 else 'ies'} for other tables did not apply")
    if unchecked:
        notes.append("the crosswalk names tables, but --left or --right is a query: its column pairs were applied "
                     "without checking the table names")
    return Crosswalk(source, tuple(links), tuple(notes))


def _saved_crosswalk(left_table: str | None, right_table: str | None) -> Crosswalk:
    from . import equivalences

    if not (left_table and right_table):
        return Crosswalk("saved", (), ("saved equivalences name tables; --left or --right is a query, so none applied",))
    for item in equivalences.load():
        if _tables_match(item.left, left_table) and _tables_match(item.right, right_table):
            return Crosswalk("saved", tuple(Link(a, b, "saved") for a, b in item.columns),
                             (f"columns mapped by the saved equivalence {item.label}",))
        if _tables_match(item.left, right_table) and _tables_match(item.right, left_table):
            return Crosswalk("saved", tuple(Link(b, a, "saved") for a, b in item.columns),
                             (f"columns mapped by the saved equivalence {item.label}",))
    return Crosswalk("saved", (), ("no saved equivalence links these two tables; columns are matched by name",))


def load_crosswalk(spec: object, *, left_table: str | None = None, right_table: str | None = None) -> Crosswalk:
    """A column crosswalk from a YAML/JSON file, from data already read, or ``"saved"`` (the saved equivalences).

    A file is a list of entries ``{left: table, right: table, columns: [[left_col, right_col], ...], status,
    explanation}`` (an entry for other tables is skipped; one for the tables the other way round is reversed),
    a list of column links ``{left: col, right: col, status, explanation}``, or a plain ``{left_col: right_col}``
    mapping. A column whose status is ``missing``, or whose partner is null, is skipped, not compared."""

    if isinstance(spec, Crosswalk):
        return spec
    if isinstance(spec, (dict, list)):
        return _crosswalk_from_data(spec, "given", left_table, right_table)
    text = str(spec).strip()
    if text.lower() == "saved":
        return _saved_crosswalk(left_table, right_table)
    path = Path(text)
    suffix = path.suffix.lower()
    if suffix not in (".yml", ".yaml", ".json"):
        raise ReconcileError("--map takes a .yml, .yaml or .json crosswalk file, or `saved`")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReconcileError(f"the crosswalk {text} cannot be read ({type(exc).__name__})") from None
    try:
        if suffix == ".json":
            data = json.loads(raw)
        else:
            import yaml

            data = yaml.safe_load(raw)
    except ImportError:
        raise ReconcileError("a YAML crosswalk needs PyYAML (pip install pyyaml); JSON needs nothing") from None
    except Exception:  # noqa: BLE001 - json and yaml raise their own families
        raise ReconcileError(f"the crosswalk {path.name} is not valid {'JSON' if suffix == '.json' else 'YAML'}") from None
    return _crosswalk_from_data(data, path.name, left_table, right_table)


# ---------------------------------------------------------------------- plan


@dataclass
class Plan:
    """Everything resolved before the query runs, and the query itself (``sql``)."""

    left: Input
    right: Input
    dialect: str
    keys: list[tuple[str, str]]
    compared: list[tuple[Column, Column]]
    left_only: list[str]
    right_only: list[str]
    ignored: list[str]
    skipped: list[dict]
    rules: list[KnownDiffRule]
    explanations: dict[str, str]
    notes: list[str]
    include_samples: bool = False
    sample_limit: int = DEFAULT_SAMPLE_LIMIT
    standalone: bool = False
    crosswalk: str | None = None
    sql: str = ""


def _names(value: Iterable[str] | str | None) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    return list(dict.fromkeys(str(item).strip().strip("`") for item in items if str(item).strip()))


def _key_specs(keys: Iterable[str] | str) -> list[tuple[str, str | None]]:
    specs: list[tuple[str, str | None]] = []
    for item in _names(keys):
        left, sep, right = item.partition("=")
        left, right = left.strip().strip("`"), right.strip().strip("`")
        if not left or (sep and not right):
            raise ReconcileError(f"--key {item!r}: write col, or left_col=right_col")
        specs.append((left, right or None))
    if not specs:
        raise ReconcileError("--key needs at least one column (a,b, or left_col=right_col)")
    return specs


def _rules(rules: object) -> list[KnownDiffRule]:
    if rules is None:
        return []
    if isinstance(rules, (str, Path)):
        return load_rules(rules)
    return parse_rules(list(rules))


def _listed(columns: Sequence[Column]) -> str:
    names = [column.name for column in columns]
    return ", ".join(names[:20]) + (f" and {len(names) - 20} more" if len(names) > 20 else "")


class _Sides:
    """Case-insensitive column lookups on both sides, with the crosswalk in both directions."""

    def __init__(self, left: Sequence[Column], right: Sequence[Column], mapping: dict[str, str]) -> None:
        self.columns = {"l": {c.name.casefold(): c for c in left}, "r": {c.name.casefold(): c for c in right}}
        self.mapping = mapping  # left name (folded) -> right name
        self.reverse = {right.casefold(): left for left, right in mapping.items()}

    def resolve(self, side: str, name: str) -> Column | None:
        """The column ``name`` means on ``side``: its own column, else the crosswalk partner of the other side's."""

        found = self.columns[side].get(name.casefold())
        if found is None:
            partner = (self.reverse if side == "l" else self.mapping).get(name.casefold())
            found = self.columns[side].get(partner.casefold()) if partner else None
        return found


def _rule_sql(rule: KnownDiffRule, sides: _Sides, dialect: str, needed: dict[str, dict[str, Column]]) -> str:
    """The rule's predicate over the joined row, never NULL. A bare column is tried on each side that has it."""

    tree = rule.tree()

    def bind(bare_side: str) -> tuple[exp.Expression, dict[str, dict[str, Column]]] | None:
        copy, used = tree.copy(), {"l": {}, "r": {}}
        for column in list(copy.find_all(exp.Column)):
            side = column.table.lower() or bare_side
            found = sides.resolve(side, column.name)
            if found is None:
                if column.table:
                    raise ReconcileError(f"rule {rule.name!r}: the {'left' if side == 'l' else 'right'} side has no "
                                         f"column {column.name!r}")
                return None
            used[side][found.name.casefold()] = found
            bound = exp.Column(this=exp.to_identifier(found.name, quoted=True), table=exp.to_identifier(side))
            if column is copy:  # the whole predicate is one boolean column
                copy = bound
            else:
                column.replace(bound)
        return copy, used

    def text(node: exp.Expression) -> str:
        return f"COALESCE(({node.sql(dialect=dialect, comments=False)}), FALSE)"

    def keep(used: dict[str, dict[str, Column]]) -> None:
        for side in ("l", "r"):
            needed[side].update(used[side])

    if all(column.table for column in tree.find_all(exp.Column)):
        bound = bind("l")
        keep(bound[1])
        return text(bound[0])
    parts = []
    for side in ("l", "r"):
        bound = bind(side)
        if bound is not None:
            keep(bound[1])
            parts.append(f"({side}.{MARKER} IS NOT NULL AND {text(bound[0])})")
    if not parts:
        bare = sorted({column.name for column in tree.find_all(exp.Column) if not column.table})
        raise ReconcileError(f"rule {rule.name!r}: no side has all of the columns {', '.join(bare)}")
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _differs(left: Column, right: Column, dialect: str) -> str:
    a, b = f"l.{_ident(left.name, dialect)}", f"r.{_ident(right.name, dialect)}"
    if dialect == "bigquery" and (left.complex or right.complex):
        return f"TO_JSON_STRING({a}) IS DISTINCT FROM TO_JSON_STRING({b})"
    return f"{a} IS DISTINCT FROM {b}"


def _sample(condition: str, plan: Plan, alias: str) -> str:
    """Up to ``sample_limit`` keys of the rows where ``condition`` holds, as JSON text: ``[{"id": 7}, ...]``."""

    order = ", ".join(f"kumo_k{i}" for i in range(len(plan.keys)))
    if plan.dialect == "bigquery":
        fields = ", ".join(f"kumo_k{i} AS {_ident(left, 'bigquery')}" for i, (left, _) in enumerate(plan.keys))
        return (f"TO_JSON_STRING(ARRAY_AGG(IF({condition}, STRUCT({fields}), NULL) IGNORE NULLS ORDER BY {order} "
                f"LIMIT {plan.sample_limit})) AS {alias}")
    pairs = ", ".join(f"{_literal(left)}, kumo_k{i}" for i, (left, _) in enumerate(plan.keys))
    return (f"CAST(to_json(array_slice(list(json_object({pairs}) ORDER BY {order}) FILTER (WHERE {condition}), 1, "
            f"{plan.sample_limit})) AS VARCHAR) AS {alias}")


def _build_sql(plan: Plan, rule_sql: Sequence[str], needed: dict[str, dict[str, Column]]) -> str:
    d = plan.dialect
    q = lambda name: _ident(name, d)  # noqa: E731
    select_l = ", ".join(q(column.name) for column in needed["l"].values())
    select_r = ", ".join(q(column.name) for column in needed["r"].values())
    on = " AND ".join(f"l.{q(a)} = r.{q(b)}" for a, b in plan.keys)
    null_l = " OR ".join(f"l.{q(a)} IS NULL" for a, _ in plan.keys)
    null_r = " OR ".join(f"r.{q(b)} IS NULL" for _, b in plan.keys)
    not_null_l = " AND ".join(f"{q(a)} IS NOT NULL" for a, _ in plan.keys)
    not_null_r = " AND ".join(f"{q(b)} IS NOT NULL" for _, b in plan.keys)
    flags = [f"l.{MARKER} IS NOT NULL AS kumo_hl", f"r.{MARKER} IS NOT NULL AS kumo_hr",
             f"({null_l}) AS kumo_nl", f"({null_r}) AS kumo_nr"]
    flags += [f"{_differs(a, b, d)} AS kumo_m{i}" for i, (a, b) in enumerate(plan.compared)]
    flags += [f"{text} AS kumo_p{j}" for j, text in enumerate(rule_sql, 1)]
    if plan.include_samples:
        flags += [f"COALESCE(l.{q(a)}, r.{q(b)}) AS kumo_k{i}" for i, (a, b) in enumerate(plan.keys)]
    mismatch = " OR ".join(["NOT (kumo_hl AND kumo_hr)", *(f"kumo_m{i}" for i in range(len(plan.compared)))])
    first = ("CASE WHEN NOT kumo_mis THEN 0" + "".join(f" WHEN kumo_p{j} THEN {j}" for j in range(1, len(rule_sql) + 1))
             + " ELSE 0 END") if rule_sql else "0"
    totals = [
        "COUNTIF(kumo_hl AND NOT kumo_hr) AS only_left", "COUNTIF(kumo_hr AND NOT kumo_hl) AS only_right",
        "COUNTIF(kumo_b) AS both_sides", "COUNTIF(NOT kumo_mis) AS matched", "COUNTIF(kumo_mis) AS mismatched",
        "COUNTIF(kumo_rule > 0) AS explained", "COUNTIF(kumo_mis AND kumo_rule = 0) AS unexplained",
        "COUNTIF(kumo_hl AND NOT kumo_hr AND kumo_rule = 0) AS u_only_left",
        "COUNTIF(kumo_hr AND NOT kumo_hl AND kumo_rule = 0) AS u_only_right",
        "COUNTIF(kumo_b AND kumo_mis AND kumo_rule = 0) AS u_value",
        "COUNTIF(kumo_hl AND kumo_nl) AS null_left", "COUNTIF(kumo_hr AND kumo_nr) AS null_right",
    ]
    for i in range(len(plan.compared)):
        totals += [f"COUNTIF(kumo_b AND kumo_m{i}) AS c{i}", f"COUNTIF(kumo_b AND kumo_m{i} AND kumo_rule = 0) AS u{i}"]
    for j in range(1, len(rule_sql) + 1):
        totals.append(f"COUNTIF(kumo_rule = {j}) AS r{j}")
        if plan.standalone:
            totals.append(f"COUNTIF(kumo_mis AND kumo_p{j}) AS s{j}")
    if plan.include_samples:
        unexplained = "kumo_mis AND kumo_rule = 0"
        totals += [_sample(f"kumo_hl AND NOT kumo_hr AND {unexplained}", plan, "x_only_left"),
                   _sample(f"kumo_hr AND NOT kumo_hl AND {unexplained}", plan, "x_only_right"),
                   _sample(f"kumo_b AND {unexplained}", plan, "x_value")]
        totals += [_sample(f"kumo_b AND kumo_m{i} AND kumo_rule = 0", plan, f"x{i}") for i in range(len(plan.compared))]
    return "\n".join([
        f"WITH kumo_l AS (SELECT {select_l}, 1 AS {MARKER} FROM {plan.left.source} AS kumo_src),",
        f"kumo_r AS (SELECT {select_r}, 1 AS {MARKER} FROM {plan.right.source} AS kumo_src),",
        f"kumo_j AS (SELECT {', '.join(flags)} FROM kumo_l AS l FULL OUTER JOIN kumo_r AS r ON {on}),",
        f"kumo_f AS (SELECT *, kumo_hl AND kumo_hr AS kumo_b, {mismatch} AS kumo_mis FROM kumo_j),",
        f"kumo_c AS (SELECT *, {first} AS kumo_rule FROM kumo_f)",
        "SELECT * FROM",
        "(SELECT COUNT(*) AS left_rows FROM kumo_l) AS kumo_s1",
        "CROSS JOIN (SELECT COUNT(*) AS right_rows FROM kumo_r) AS kumo_s2",
        # Key values that repeat (NULL keys never join, so they cannot repeat a match).
        f"CROSS JOIN (SELECT COUNTIF(kumo_n > 1) AS dup_left FROM (SELECT COUNT(*) AS kumo_n FROM kumo_l "
        f"WHERE {not_null_l} GROUP BY {', '.join(q(a) for a, _ in plan.keys)}) AS kumo_g) AS kumo_s3",
        f"CROSS JOIN (SELECT COUNTIF(kumo_n > 1) AS dup_right FROM (SELECT COUNT(*) AS kumo_n FROM kumo_r "
        f"WHERE {not_null_r} GROUP BY {', '.join(q(b) for _, b in plan.keys)}) AS kumo_g) AS kumo_s4",
        f"CROSS JOIN (SELECT {', '.join(totals)} FROM kumo_c) AS kumo_t",
    ])


def plan_reconcile(left: str, right: str, keys: Iterable[str] | str, executor, *, ignore: Iterable[str] | str = (),
                   crosswalk: object = None, rules: object = None, include_samples: bool = False,
                   sample_limit: int = DEFAULT_SAMPLE_LIMIT, standalone: bool = False) -> Plan:
    """Resolve both sides, the key, the columns to compare and the rules, and write the one comparison query.

    Raises :class:`ReconcileError` (or :class:`~kumosql.reconcile_rules.RuleError`) with what to change."""

    dialect = executor.dialect
    if include_samples and (isinstance(sample_limit, bool) or not isinstance(sample_limit, int)
                            or not 1 <= sample_limit <= MAX_SAMPLE_LIMIT):
        raise ReconcileError(f"the sample limit must be a whole number from 1 to {MAX_SAMPLE_LIMIT}")
    lin, rin = resolve_input(left, executor, "left"), resolve_input(right, executor, "right")
    key_specs = _key_specs(keys)
    ignore_names = _names(ignore)
    rule_list = _rules(rules)
    walk = load_crosswalk(crosswalk, left_table=lin.table, right_table=rin.table) if crosswalk else None
    lcols, rcols = executor.columns(lin.source), executor.columns(rin.source)
    notes: list[str] = list(walk.notes) if walk else []

    links = list(walk.links) if walk else []
    mapping: dict[str, str] = {}
    for link in links:
        if link.missing:
            continue
        old = mapping.get(link.left.casefold())
        if old is not None and old.casefold() != link.right.casefold():
            raise ReconcileError(f"the crosswalk maps the left column {link.left!r} to both {old!r} and {link.right!r}")
        mapping[link.left.casefold()] = link.right
    sides = _Sides(lcols, rcols, mapping)
    lmap, rmap = sides.columns["l"], sides.columns["r"]

    key_pairs: list[tuple[str, str]] = []
    for lk, rk in key_specs:
        lc = lmap.get(lk.casefold())
        if lc is None:
            raise ReconcileError(f"the left side has no key column {lk!r}; it has {_listed(lcols)}")
        rname = rk or mapping.get(lk.casefold()) or lk
        rc = rmap.get(rname.casefold())
        if rc is None:
            raise ReconcileError(f"the right side has no key column {rname!r}; it has {_listed(rcols)}")
        key_pairs.append((lc.name, rc.name))
    key_l = {a.casefold() for a, _ in key_pairs}
    key_r = {b.casefold() for _, b in key_pairs}

    ignored_fold = {name.casefold() for name in ignore_names}
    ignored: list[str] = []
    for name in ignore_names:
        found = lmap.get(name.casefold()) or rmap.get(name.casefold())
        if found is None:
            notes.append(f"--ignore {name}: neither side has this column")
        elif name.casefold() in key_l | key_r:
            raise ReconcileError(f"--ignore {name}: it is part of the key")
        else:
            ignored.append(found.name)

    explanations: dict[str, str] = {}
    skip_left: dict[str, Link] = {}
    skip_right: dict[str, Link] = {}
    for link in links:
        if link.left and link.explanation:
            explanations[link.left.casefold()] = link.explanation
        if link.missing:
            if link.left:
                skip_left[link.left.casefold()] = link
            if link.right and not link.left:
                skip_right[link.right.casefold()] = link
    skipped: list[dict] = []
    compared: list[tuple[Column, Column]] = []
    left_only: list[str] = []
    used_right: set[str] = set()
    targets = {right.casefold() for right in mapping.values()}
    for column in lcols:
        fold = column.name.casefold()
        if fold in key_l or fold in ignored_fold:
            continue
        if fold in skip_left:
            entry = {"column": column.name, "side": "left", "reason": "missing"}
            if skip_left[fold].explanation:
                entry["explanation"] = skip_left[fold].explanation
            skipped.append(entry)
            continue
        if fold in mapping:
            partner = rmap.get(mapping[fold].casefold())
            if partner is None:
                notes.append(f"the crosswalk maps {column.name} to {mapping[fold]}, which the right side does not have")
        else:
            partner = None if fold in targets else rmap.get(fold)  # a crosswalk target belongs to its own pair
        if (partner is None or partner.name.casefold() in key_r or partner.name.casefold() in ignored_fold
                or partner.name.casefold() in used_right):
            left_only.append(column.name)
            continue
        used_right.add(partner.name.casefold())
        compared.append((column, partner))
    right_only: list[str] = []
    for column in rcols:
        fold = column.name.casefold()
        if fold in used_right or fold in key_r or fold in ignored_fold:
            continue
        if fold in skip_right:
            entry = {"column": column.name, "side": "right", "reason": "missing"}
            if skip_right[fold].explanation:
                entry["explanation"] = skip_right[fold].explanation
            skipped.append(entry)
            continue
        right_only.append(column.name)

    needed: dict[str, dict[str, Column]] = {"l": {}, "r": {}}
    for a, b in key_pairs:
        needed["l"][a.casefold()] = lmap[a.casefold()]
        needed["r"][b.casefold()] = rmap[b.casefold()]
    for a, b in compared:
        needed["l"][a.name.casefold()] = a
        needed["r"][b.name.casefold()] = b
    rule_sql = [_rule_sql(rule, sides, dialect, needed) for rule in rule_list]
    for side in ("l", "r"):
        if MARKER in needed[side]:
            raise ReconcileError(f"a column named {MARKER} clashes with the comparison's own marker column; rename it "
                                 "in a --left/--right query")

    names = {rule.name.casefold() for rule in rule_list}
    column_explanations = {}
    for fold, rule_name in explanations.items():
        column = lmap.get(fold)
        label = column.name if column else fold
        column_explanations[label] = rule_name
        if rule_list and rule_name.casefold() not in names:
            notes.append(f"the crosswalk explains {label} by {rule_name!r}, which is not one of the rules given")
    if not compared:
        notes.append("no column is compared besides the key: only which keys each side has is checked")
    if left_only:
        notes.append(f"{len(left_only)} column(s) only on the left were not compared")
    if right_only:
        notes.append(f"{len(right_only)} column(s) only on the right were not compared")

    plan = Plan(lin, rin, dialect, key_pairs, compared, left_only, right_only, ignored, skipped, rule_list,
                column_explanations, notes, include_samples, sample_limit, standalone,
                walk.source if walk else None)
    plan.sql = _build_sql(plan, rule_sql, needed)
    _readonly(plan.sql)
    return plan


def build_reconcile_sql(left: str, right: str, keys: Iterable[str] | str, executor, **options) -> str:
    """The one read-only query :func:`reconcile` would run (nothing runs; BigQuery reads the column lists with dry runs)."""

    return plan_reconcile(left, right, keys, executor, **options).sql


def estimate_bytes(plan: Plan, executor) -> int | None:
    """BigQuery's dry-run estimate for the comparison, refused over the byte cap; ``None`` on DuckDB."""

    if not hasattr(executor, "estimate"):
        return None
    _readonly(plan.sql)
    return _wrapped(executor.estimate, plan.sql)


# -------------------------------------------------------------------- results


def _int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _keys(value: object) -> list:
    """Sample keys from the JSON text the query returns; an empty or NULL aggregate is no sample."""

    if value is None:
        return []
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return list(data) if isinstance(data, list) else []


@dataclass
class ReconcileResult:
    left: str
    right: str
    dialect: str
    keys: list[str]
    ignored_columns: list[str]
    skipped_columns: list[dict]
    compared_columns: list[str]
    column_map: dict[str, str]
    left_only_columns: list[str]
    right_only_columns: list[str]
    left_rows: int
    right_rows: int
    keys_only_left: int
    keys_only_right: int
    keys_both: int
    matched_rows: int
    mismatched_rows: int
    explained_rows: int
    unexplained_rows: int
    unexplained_by_kind: dict[str, int]
    column_mismatches: dict[str, int]
    unexplained_column_mismatches: dict[str, int]
    rules: list[dict]
    duplicate_keys: dict[str, int]
    null_keys: dict[str, int]
    column_explanations: dict[str, str] = field(default_factory=dict)
    samples_included: bool = False
    samples: dict | None = None
    crosswalk: str | None = None
    estimated_bytes: int | None = None
    bytes_billed: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Every row matches or is explained by a rule."""

        return self.unexplained_rows == 0

    def to_json(self) -> dict:
        return {
            "left": self.left, "right": self.right, "dialect": self.dialect, "keys": list(self.keys),
            "ignored_columns": list(self.ignored_columns), "skipped_columns": list(self.skipped_columns),
            "compared_columns": list(self.compared_columns), "column_map": dict(self.column_map),
            "left_only_columns": list(self.left_only_columns), "right_only_columns": list(self.right_only_columns),
            "left_rows": self.left_rows, "right_rows": self.right_rows,
            "keys_only_left": self.keys_only_left, "keys_only_right": self.keys_only_right, "keys_both": self.keys_both,
            "matched_rows": self.matched_rows, "mismatched_rows": self.mismatched_rows,
            "explained_rows": self.explained_rows, "unexplained_rows": self.unexplained_rows,
            "unexplained_by_kind": dict(self.unexplained_by_kind),
            "column_mismatches": dict(self.column_mismatches),
            "unexplained_column_mismatches": dict(self.unexplained_column_mismatches),
            "rules": [dict(rule) for rule in self.rules],
            "column_explanations": dict(self.column_explanations),
            "duplicate_keys": dict(self.duplicate_keys), "null_keys": dict(self.null_keys),
            "crosswalk": self.crosswalk,
            "samples_included": self.samples_included, "samples": self.samples,
            "estimated_bytes": self.estimated_bytes, "bytes_billed": self.bytes_billed,
            "notes": list(self.notes),
        }


def _nonzero(counts: dict[str, int]) -> dict[str, int]:
    """Columns with at least one difference, most first: a wide table that mostly agrees stays a short answer."""

    return dict(sorted(((name, n) for name, n in counts.items() if n), key=lambda item: (-item[1], item[0])))


def _result(plan: Plan, row: dict, executor) -> ReconcileResult:
    n = lambda key: _int(row.get(key))  # noqa: E731
    column_counts = {a.name: n(f"c{i}") for i, (a, _) in enumerate(plan.compared)}
    unexplained_counts = {a.name: n(f"u{i}") for i, (a, _) in enumerate(plan.compared)}
    rules = [{"name": rule.name, "verified_at": rule.verified_at, "matched_rows": n(f"r{j}")}
             for j, rule in enumerate(plan.rules, 1)]
    notes = list(plan.notes)
    duplicates = {"left": n("dup_left"), "right": n("dup_right")}
    for side, count in duplicates.items():
        if count:
            notes.append(f"the key is not unique on the {side} ({count:,} key value(s) repeat): rows sharing a key are "
                         "joined pair by pair, so the counts of keys on both sides are joined pairs, not rows")
    nulls = {"left": n("null_left"), "right": n("null_right")}
    for side, count in nulls.items():
        if count:
            notes.append(f"{count:,} {side} row(s) have a NULL in the key; a NULL key never matches, so they count as "
                         f"only on the {side}")
    samples = None
    if plan.include_samples:
        by_column = {a.name: _keys(row.get(f"x{i}")) for i, (a, _) in enumerate(plan.compared)}
        samples = {
            "only_left": _keys(row.get("x_only_left")), "only_right": _keys(row.get("x_only_right")),
            "value_mismatch": _keys(row.get("x_value")),
            "by_column": {name: keys for name, keys in by_column.items() if keys},
        }
    column_map = {a: b for a, b in plan.keys if a != b}
    column_map.update({a.name: b.name for a, b in plan.compared if a.name != b.name})
    return ReconcileResult(
        left=plan.left.spec, right=plan.right.spec, dialect=plan.dialect, keys=[a for a, _ in plan.keys],
        ignored_columns=list(plan.ignored), skipped_columns=list(plan.skipped),
        compared_columns=[a.name for a, _ in plan.compared], column_map=column_map,
        left_only_columns=list(plan.left_only), right_only_columns=list(plan.right_only),
        left_rows=n("left_rows"), right_rows=n("right_rows"),
        keys_only_left=n("only_left"), keys_only_right=n("only_right"), keys_both=n("both_sides"),
        matched_rows=n("matched"), mismatched_rows=n("mismatched"),
        explained_rows=n("explained"), unexplained_rows=n("unexplained"),
        unexplained_by_kind={"only_left": n("u_only_left"), "only_right": n("u_only_right"), "value_mismatch": n("u_value")},
        column_mismatches=_nonzero(column_counts), unexplained_column_mismatches=_nonzero(unexplained_counts),
        rules=rules, duplicate_keys=duplicates, null_keys=nulls, column_explanations=dict(plan.explanations),
        samples_included=plan.include_samples, samples=samples, crosswalk=plan.crosswalk,
        estimated_bytes=executor.estimated_bytes, bytes_billed=executor.bytes_billed, notes=notes,
    )


def _execute(plan: Plan, executor) -> dict:
    _readonly(plan.sql)
    rows = executor.run(plan.sql)
    return rows[0] if rows else {}


def reconcile(left: str, right: str, keys: Iterable[str] | str, executor, *, ignore: Iterable[str] | str = (),
              crosswalk: object = None, rules: object = None, include_samples: bool = False,
              sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> ReconcileResult:
    """Compare ``left`` and ``right`` joined on ``keys`` in one query and sort every row (see the module docstring).

    ``left`` and ``right`` are table names, ``.sql`` files or inline ``SELECT``/``WITH`` queries; ``keys`` is
    ``"a,b"`` or ``["a", "left_b=right_b"]``; ``ignore`` names columns not to compare; ``crosswalk`` is a
    crosswalk file, ``"saved"`` or data (:func:`load_crosswalk`); ``rules`` a rules file or a list of
    :class:`~kumosql.reconcile_rules.KnownDiffRule`. Raises :class:`ReconcileError`, :class:`ByteCapExceeded`
    when a BigQuery dry run is over the cap, and :class:`~kumosql.reconcile_rules.RuleError`."""

    plan = plan_reconcile(left, right, keys, executor, ignore=ignore, crosswalk=crosswalk, rules=rules,
                          include_samples=include_samples, sample_limit=sample_limit)
    return _result(plan, _execute(plan, executor), executor)


# --------------------------------------------------------------- rules check


@dataclass
class RulesCheckResult:
    left: str
    right: str
    dialect: str
    keys: list[str]
    growth_factor: float
    min_growth: int
    rules: list[dict]
    mismatched_rows: int
    explained_rows: int
    unexplained_rows: int
    estimated_bytes: int | None = None
    bytes_billed: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> list[dict]:
        return [rule for rule in self.rules if rule["status"] == "flagged"]

    @property
    def ok(self) -> bool:
        return not self.flagged

    def to_json(self) -> dict:
        flags = [flag for rule in self.rules for flag in rule["flags"]]
        return {
            "left": self.left, "right": self.right, "dialect": self.dialect, "keys": list(self.keys),
            "growth_factor": self.growth_factor, "min_growth": self.min_growth,
            "summary": {"rules": len(self.rules), "ok": len(self.rules) - len(self.flagged), "flagged": len(self.flagged),
                        "no_longer_explains": flags.count("no_longer_explains"),
                        "grew_significantly": flags.count("grew_significantly")},
            "rules": [dict(rule) for rule in self.rules],
            "mismatched_rows": self.mismatched_rows, "explained_rows": self.explained_rows,
            "unexplained_rows": self.unexplained_rows,
            "estimated_bytes": self.estimated_bytes, "bytes_billed": self.bytes_billed, "notes": list(self.notes),
        }


def _growth(rule: KnownDiffRule, current: int, growth_factor: float, min_growth: int) -> tuple[bool, int | None]:
    """Whether ``current`` is far above the verified count, and the most rows the rule may explain before it is."""

    if rule.verified_count is None:
        return False, None
    if rule.verified_count == 0:
        return current > 0, 0
    ratio = rule.max_growth_ratio if rule.max_growth_ratio is not None else growth_factor
    limit = max(rule.verified_count * ratio, rule.verified_count + min_growth)
    return current > limit, math.floor(limit)


def check_rules(left: str, right: str, keys: Iterable[str] | str, executor, rules: object, *,
                ignore: Iterable[str] | str = (), crosswalk: object = None,
                growth_factor: float = DEFAULT_GROWTH_FACTOR, min_growth: int = DEFAULT_MIN_GROWTH) -> RulesCheckResult:
    """Run every rule against current data in one query and flag the stale ones.

    ``current_count`` is the rows a rule explains in first-match order (what :func:`reconcile` reports);
    ``standalone_count`` the differing rows its predicate holds for on its own, whatever comes first. A rule is
    flagged ``no_longer_explains`` when it explains no row, and ``grew_significantly`` when it explains more
    than ``max(verified_count * factor, verified_count + min_growth)`` rows (``factor`` is the rule's
    ``max_growth_ratio`` or ``growth_factor``), or any row when it was verified at 0."""

    if isinstance(growth_factor, bool) or not isinstance(growth_factor, (int, float)) or not math.isfinite(growth_factor) \
            or growth_factor < 1:
        raise ReconcileError("the growth factor must be a number of at least 1")
    if isinstance(min_growth, bool) or not isinstance(min_growth, int) or min_growth < 0:
        raise ReconcileError("the minimum growth must be a whole number of rows (0 or more)")
    rule_list = _rules(rules)
    if not rule_list:
        raise ReconcileError("there are no rules to check")
    plan = plan_reconcile(left, right, keys, executor, ignore=ignore, crosswalk=crosswalk, rules=rule_list,
                          standalone=True)
    row = _execute(plan, executor)
    result = _result(plan, row, executor)
    checked = []
    for j, rule in enumerate(rule_list, 1):
        current, standalone = _int(row.get(f"r{j}")), _int(row.get(f"s{j}"))
        grew, limit = _growth(rule, current, float(growth_factor), min_growth)
        flags = (["no_longer_explains"] if current == 0 else []) + (["grew_significantly"] if grew else [])
        entry = {"name": rule.name, "status": "flagged" if flags else "ok", "flags": flags,
                 "verified_at": rule.verified_at, "verified_count": rule.verified_count, "current_count": current,
                 "standalone_count": standalone, "growth_limit": limit}
        if current == 0 and standalone:
            entry["note"] = "the rows it matches are explained by an earlier rule"
        checked.append(entry)
    return RulesCheckResult(
        left=result.left, right=result.right, dialect=result.dialect, keys=result.keys,
        growth_factor=float(growth_factor), min_growth=min_growth, rules=checked,
        mismatched_rows=result.mismatched_rows, explained_rows=result.explained_rows,
        unexplained_rows=result.unexplained_rows, estimated_bytes=result.estimated_bytes,
        bytes_billed=result.bytes_billed, notes=result.notes,
    )

