"""Known-difference rules: named SQL predicates for rows two tables are expected to disagree on.

A reconciliation (:mod:`kumosql.reconcile`) joins two tables on a key and sorts every row that
disagrees under the first rule whose predicate holds for it, or leaves it unexplained. A rules
file is YAML or JSON, a list of rules or a mapping with a ``rules`` list::

    rules:
      - name: refunds_dropped
        predicate: "l.status = 'refunded' AND r.order_id IS NULL"
        verified_at: 2026-11-02
        verified_count: 41
        description: The new table leaves out refunded orders.

``name`` is the rule's identifier (a crosswalk's ``explanation`` names it). ``predicate`` (also read
as ``sql`` or ``where``) is one SQL condition over the joined row: ``l.col`` is the left table's
value, ``r.col`` the right table's, and a bare ``col`` matches when the left row or the right row
satisfies it (a side with no row for the key does not count). ``verified_at`` (also ``verified`` or
``verified_date``) is the ``YYYY-MM-DD`` date someone checked the rule. ``verified_count`` is how many
rows it explained then, so ``python -m kumosql rules check`` can say when it has grown;
``max_growth_ratio`` overrides the growth factor for this rule; ``columns`` lists the columns it is
about.

A predicate is read with sqlglot in the BigQuery dialect and must be one read-only condition: no
semicolon, no subquery, no aggregate or window function, no query parameter.
:func:`rule_from_except_when` turns the "equivalent except when P" predicate of
``python -m kumosql prove-sql-equivalent --explain-difference`` into a rule in this format.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

__all__ = [
    "KnownDiffRule", "RuleError", "check_predicate", "dump_rules", "load_rules", "parse_rule", "parse_rules",
    "predicate_columns", "rule_from_except_when", "save_rules",
]

MAX_PREDICATE_CHARS = 4000
MAX_NAME_CHARS = 120
MAX_RULES = 500
MAX_FILE_BYTES = 2_000_000
SIDES = ("l", "r")

_KEYS = {
    "name": "name",
    "predicate": "predicate", "sql": "predicate", "where": "predicate",
    "verified_at": "verified_at", "verified": "verified_at", "verified_date": "verified_at",
    "description": "description",
    "verified_count": "verified_count",
    "max_growth_ratio": "max_growth_ratio",
    "columns": "columns",
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Nodes a predicate may not contain: anything that reads another table, writes, or needs a group or window.
_FORBIDDEN = (
    exp.Query, exp.Subquery, exp.DML, exp.DDL, exp.Command, exp.Into, exp.Transaction, exp.Lock,
    exp.AggFunc, exp.Window, exp.Parameter, exp.Placeholder, exp.Star,
    # Statements sqlglot does not class as DDL or DML.
    *(getattr(exp, name) for name in ("Drop", "TruncateTable", "Grant", "Revoke", "Alter", "Set", "Declare", "Use",
                                      "Copy", "Merge", "Pragma", "Analyze", "Describe") if hasattr(exp, name)),
)


class RuleError(ValueError):
    """A rule or rules file is malformed; the message says which rule and what to change."""


@dataclass(frozen=True)
class KnownDiffRule:
    """One named, dated predicate for rows that are expected to differ."""

    name: str
    predicate: str
    verified_at: str
    description: str = ""
    verified_count: int | None = None
    max_growth_ratio: float | None = None
    columns: tuple[str, ...] = ()

    def to_json(self) -> dict:
        data: dict = {"name": self.name, "predicate": self.predicate, "verified_at": self.verified_at}
        if self.description:
            data["description"] = self.description
        if self.verified_count is not None:
            data["verified_count"] = self.verified_count
        if self.max_growth_ratio is not None:
            data["max_growth_ratio"] = self.max_growth_ratio
        if self.columns:
            data["columns"] = list(self.columns)
        return data

    def tree(self) -> exp.Expression:
        """The predicate's parse tree (checked again, so a rule built by hand is held to the same rules)."""

        return check_predicate(self.predicate, self.name)


# ---------------------------------------------------------------- predicates


def check_predicate(text: object, rule: str = "") -> exp.Expression:
    """Parse ``text`` as one read-only SQL condition (BigQuery dialect); raises :class:`RuleError` otherwise.

    Columns are ``l.col``, ``r.col`` or a bare ``col``; any other qualifier is refused."""

    where = f"rule {rule!r}: " if rule else ""
    if not isinstance(text, str) or not text.strip():
        raise RuleError(f"{where}the predicate must be a SQL condition such as `l.status IS DISTINCT FROM r.status`")
    if len(text) > MAX_PREDICATE_CHARS:
        raise RuleError(f"{where}the predicate is longer than {MAX_PREDICATE_CHARS:,} characters")
    try:
        tokens = sqlglot.tokenize(text, read="bigquery")
    except Exception:  # noqa: BLE001 - tokenizer errors quote SQL; say only that it did not read
        raise RuleError(f"{where}the predicate is not a SQL condition") from None
    if any(token.token_type == TokenType.SEMICOLON for token in tokens):
        raise RuleError(f"{where}the predicate must be one condition, with no semicolon")
    try:
        parsed = [node for node in sqlglot.parse(text, read="bigquery") if node is not None]
    except Exception:  # noqa: BLE001
        raise RuleError(f"{where}the predicate is not a SQL condition") from None
    if len(parsed) != 1:
        raise RuleError(f"{where}the predicate must be one SQL condition")
    tree = parsed[0]
    if isinstance(tree, (exp.Literal, exp.Null, exp.Tuple, exp.Array)):
        raise RuleError(f"{where}the predicate must be a condition, not a value")
    if not isinstance(tree, exp.Condition):
        raise RuleError(f"{where}the predicate must be a SQL condition, not a statement")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN):
            what = ("an aggregate or window function" if isinstance(node, (exp.AggFunc, exp.Window))
                    else "a query parameter" if isinstance(node, (exp.Parameter, exp.Placeholder))
                    else "a subquery or statement")
            raise RuleError(f"{where}the predicate cannot contain {what}; it is evaluated on one joined row")
        if isinstance(node, exp.Column):
            if node.args.get("db") is not None or node.args.get("catalog") is not None:
                raise RuleError(f"{where}`{node.sql('bigquery')}` names a nested field; write l.col, r.col or col")
            qualifier = node.table.lower()
            if qualifier and qualifier not in SIDES:
                raise RuleError(f"{where}`{node.sql('bigquery')}` is qualified by {node.table!r}; "
                                "use l.col for the left table, r.col for the right, or a bare col")
    return tree


def predicate_columns(tree: exp.Expression) -> list[tuple[str, str]]:
    """``(side, column)`` for each column the predicate reads, in order; side is ``l``, ``r`` or ``""``."""

    found: list[tuple[str, str]] = []
    for column in tree.find_all(exp.Column):
        item = (column.table.lower(), column.name)
        if item not in found:
            found.append(item)
    return found


# --------------------------------------------------------------------- rules


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleError(f"{where} needs a name")
    name = value.strip()
    if len(name) > MAX_NAME_CHARS or any(ord(ch) < 32 for ch in name):
        raise RuleError(f"{where}: the name must be one line of at most {MAX_NAME_CHARS} characters")
    return name


def _date(value: object, where: str) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and _DATE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise RuleError(f"{where}: verified_at must be the date the rule was checked, like 2026-11-02")


def _count(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuleError(f"{where}: verified_count must be a whole number of rows (0 or more)")
    return value


def _ratio(value: object, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 1:
        raise RuleError(f"{where}: max_growth_ratio must be a number of at least 1 (2 flags a rule that doubled)")
    return float(value)


def _columns(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RuleError(f"{where}: columns must be a list of column names")
    return tuple(dict.fromkeys(item.strip() for item in value))


def parse_rule(item: object, index: int | None = None) -> KnownDiffRule:
    """One rule from a mapping; raises :class:`RuleError` naming the rule and the problem."""

    where = f"rule {index}" if index is not None else "a rule"
    if isinstance(item, KnownDiffRule):
        item = item.to_json()
    if not isinstance(item, dict):
        raise RuleError(f"{where} must be a mapping with name, predicate and verified_at")
    fields: dict[str, object] = {}
    for key, value in item.items():
        target = _KEYS.get(str(key).strip().lower()) if isinstance(key, str) else None
        if target is None:
            raise RuleError(f"{where} has an unknown field {key!r}; the fields are name, predicate (or sql, where), "
                            "verified_at (or verified, verified_date), description, verified_count, max_growth_ratio, columns")
        if target in fields:
            raise RuleError(f"{where} gives {target} twice (under two of its names)")
        fields[target] = value
    name = _name(fields.get("name"), where)
    where = f"rule {name!r}"
    if "predicate" not in fields:
        raise RuleError(f"{where} needs a predicate")
    check_predicate(fields["predicate"], name)
    if "verified_at" not in fields:
        raise RuleError(f"{where} needs verified_at, the date it was checked (YYYY-MM-DD)")
    description = fields.get("description") or ""
    if not isinstance(description, str):
        raise RuleError(f"{where}: description must be text")
    return KnownDiffRule(
        name=name,
        predicate=str(fields["predicate"]).strip(),  # as written; queries render it again from its parse tree
        verified_at=_date(fields["verified_at"], where),
        description=description.strip(),
        verified_count=_count(fields.get("verified_count"), where),
        max_growth_ratio=_ratio(fields.get("max_growth_ratio"), where),
        columns=_columns(fields.get("columns"), where),
    )


def parse_rules(data: object) -> list[KnownDiffRule]:
    """Rules from a list, or from a mapping with a ``rules`` list. Names must be unique (case ignored)."""

    if isinstance(data, dict):
        extra = sorted(str(key) for key in data if key != "rules")
        if "rules" not in data or extra:
            raise RuleError("a rules file is a list of rules or a mapping with one key, `rules`"
                            + (f" (found {', '.join(extra)})" if extra else ""))
        data = data["rules"]
    if data is None:
        return []
    if not isinstance(data, (list, tuple)):
        raise RuleError("the rules must be a list")
    if len(data) > MAX_RULES:
        raise RuleError(f"at most {MAX_RULES} rules can be checked at once")
    rules: list[KnownDiffRule] = []
    seen: set[str] = set()
    for index, item in enumerate(data, 1):
        rule = parse_rule(item, index)
        if rule.name.casefold() in seen:
            raise RuleError(f"two rules are named {rule.name!r}; a name identifies one rule")
        seen.add(rule.name.casefold())
        rules.append(rule)
    return rules


def load_rules(path: str | Path) -> list[KnownDiffRule]:
    """Rules from a ``.yml``/``.yaml`` (PyYAML) or ``.json`` file."""

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in (".yml", ".yaml", ".json"):
        raise RuleError(f"the rules file {path.name} must end in .yml, .yaml or .json")
    try:
        if not path.is_file():
            raise RuleError(f"the rules file {path} does not exist")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise RuleError(f"the rules file {path.name} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise RuleError(f"the rules file {path.name} is not UTF-8 text") from None
    except OSError as exc:
        raise RuleError(f"the rules file {path.name} cannot be read ({type(exc).__name__})") from None
    if suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            raise RuleError(f"the rules file {path.name} is not valid JSON") from None
    else:
        try:
            import yaml
        except ImportError:
            raise RuleError("a YAML rules file needs PyYAML (pip install pyyaml); JSON needs nothing") from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            raise RuleError(f"the rules file {path.name} is not valid YAML") from None
    return parse_rules(data)


# ------------------------------------------------------------------ writing


def _yaml_scalar(value: object) -> str:
    """A YAML scalar that reads back as the same value: strings are double-quoted with JSON escapes, which YAML accepts."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def dump_rules(rules: Iterable[KnownDiffRule], fmt: str = "yaml") -> str:
    """The rules as a rules file (``yaml`` or ``json``) that :func:`load_rules` reads back unchanged.

    YAML is written without PyYAML, with every string quoted, so a date stays text and nothing needs installing."""

    items = [rule.to_json() for rule in rules]
    if fmt == "json":
        return json.dumps({"rules": items}, indent=2, ensure_ascii=False) + "\n"
    if fmt != "yaml":
        raise ValueError("fmt must be 'yaml' or 'json'")
    lines = ["# Known-difference rules for python -m kumosql reconcile and rules check (docs/reconcile.md)"]
    if not items:
        return "\n".join([*lines, "rules: []"]) + "\n"
    lines.append("rules:")
    for item in items:
        first = True
        for key, value in item.items():
            lead = "  - " if first else "    "
            first = False
            if isinstance(value, list):
                lines.append(f"{lead}{key}: [{', '.join(_yaml_scalar(v) for v in value)}]")
            else:
                lines.append(f"{lead}{key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def save_rules(rules: Sequence[KnownDiffRule], path: str | Path) -> Path:
    """Write ``rules`` to ``path`` as YAML (``.yml``/``.yaml``) or JSON (``.json``)."""

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in (".yml", ".yaml", ".json"):
        raise RuleError(f"the rules file {path.name} must end in .yml, .yaml or .json")
    path.write_text(dump_rules(rules, "json" if suffix == ".json" else "yaml"), encoding="utf-8")
    return path


# ------------------------------------------------- from a difference predicate


def rule_from_except_when(payload: dict, *, name: str | None = None, verified_at: str | None = None) -> KnownDiffRule:
    """A rule from the ``{"sql", "atoms", "tables", "exact"}`` that ``except_when`` returns.

    The prover's predicate reads one table's columns. Qualifiers naming that table are dropped, so its
    columns become bare names: in a reconciliation the rule matches a row when either side's row
    satisfies P. ``name`` defaults to ``except_when_`` and a short hash of the predicate; ``verified_at``
    to today (UTC), the day the prover verified it. ``verified_count`` stays empty: the prover checked the
    predicate on query text, not on rows."""

    if not isinstance(payload, dict) or not isinstance(payload.get("sql"), str) or not payload["sql"].strip():
        raise RuleError("the difference explanation has no predicate (`sql`)")
    tables = {str(item).strip("`").lower() for item in payload.get("tables") or () if str(item).strip()}
    names = tables | {item.rsplit(".", 1)[-1] for item in tables}
    try:
        tree = sqlglot.parse_one(payload["sql"], read="bigquery")
    except Exception:  # noqa: BLE001
        raise RuleError("the difference predicate is not a SQL condition") from None
    for column in tree.find_all(exp.Column):
        qualifier = ".".join(part.name for part in column.parts[:-1]).lower()
        if qualifier and (qualifier in names or qualifier.rsplit(".", 1)[-1] in names):
            column.replace(exp.column(column.this.copy()))
    text = tree.sql(dialect="bigquery", comments=False)
    tree = check_predicate(text, name or "except_when")
    exact = bool(payload.get("exact"))
    described = sorted(tables)
    description = (
        "From python -m kumosql prove-sql-equivalent --explain-difference: the queries are equivalent except when "
        f"this holds ({'exactly where it holds' if exact else 'it covers every difference, and may cover more'})"
        + (f"; tables read: {', '.join(described)}" if described else "")
    )
    stamp = verified_at or datetime.now(timezone.utc).date().isoformat()
    columns = [column for side, column in predicate_columns(tree) if not side]
    return parse_rule({
        "name": name or "except_when_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
        "predicate": text,
        "verified_at": stamp,
        "description": description,
        "columns": columns or None,
    })
