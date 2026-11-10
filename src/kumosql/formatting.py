"""SQL formatting and complexity scoring powered by sqlfluff.

``format_sql`` runs sqlfluff's fixer with user-controllable preferences, and
is registered as the ``format_sql`` rewrite rule so it can be chosen and
verified like any other operation. ``complexity`` scores a query from its
sqlfluff parse tree.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
import re
import threading
from typing import Mapping

from .engine import RewriteRule, RuleDiagnostic, RuleOutput, register_rule
from kumo_sql_prover.layout_equivalence import layout_only_change, restore_function_case, tokenize_exactly
from kumo_sql_prover.sqlx import looks_like_sqlx

DIALECT = "bigquery"
RULE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{1,40}$")
MAX_RULES = 100


@dataclass(frozen=True)
class FormatPreferences:
    """Formatting preferences, each mapped onto a sqlfluff setting."""

    #: sqlfluff rule codes or group names to apply (e.g. ``LT01`` or ``layout``)
    rules: tuple[str, ...] = ("layout", "capitalisation")
    #: rule codes to skip even when selected
    exclude_rules: tuple[str, ...] = ()
    max_line_length: int = 80
    indent_unit: str = "space"  # or "tab"
    tab_space_size: int = 2
    keyword_case: str = "upper"  # upper | lower | consistent | capitalise
    comma_position: str = "trailing"  # trailing | leading

    def to_json(self) -> dict:
        data = asdict(self)
        data["rules"] = list(self.rules)
        data["exclude_rules"] = list(self.exclude_rules)
        return data


DEFAULT_PREFERENCES = FormatPreferences()
_CHOICES = {
    "indent_unit": ("space", "tab"),
    "keyword_case": ("upper", "lower", "consistent", "capitalise"),
    "comma_position": ("trailing", "leading"),
}


def _rule_list(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise ValueError(f"{name} must be a list of sqlfluff rule codes or groups")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not RULE_NAME_RE.match(item.strip()):
            raise ValueError(f"{name} contains an invalid sqlfluff rule: {item!r}")
        cleaned.append(item.strip())
    return tuple(dict.fromkeys(cleaned))


def parse_preferences(data: object) -> FormatPreferences:
    """Validate JSON-shaped preferences; missing keys keep their defaults."""

    if data is None:
        return DEFAULT_PREFERENCES
    if not isinstance(data, dict):
        raise ValueError("format preferences must be an object")
    prefs = DEFAULT_PREFERENCES
    unknown = set(data) - set(asdict(prefs))
    if unknown:
        raise ValueError(f"unknown format preference: {sorted(unknown)[0]}")
    for key in ("rules", "exclude_rules"):
        if key in data:
            prefs = replace(prefs, **{key: _rule_list(key, data[key])})
    if not prefs.rules:
        raise ValueError("select at least one sqlfluff rule to format with")
    for key, low, high in (("max_line_length", 20, 500), ("tab_space_size", 1, 8)):
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be a whole number between {low} and {high}")
            prefs = replace(prefs, **{key: value})
    for key, choices in _CHOICES.items():
        if key in data:
            if data[key] not in choices:
                raise ValueError(f"{key} must be one of: {', '.join(choices)}")
            prefs = replace(prefs, **{key: data[key]})
    return prefs


def load_preferences() -> FormatPreferences:
    """The saved preferences, falling back to defaults if none or invalid."""

    from . import state

    try:
        return parse_preferences(state.get_section("format"))
    except ValueError:
        return DEFAULT_PREFERENCES


def save_preferences(prefs: FormatPreferences) -> None:
    from . import state

    state.set_section("format", prefs.to_json())


# Rules that change what the SQL means in BigQuery, so a group such as ``capitalisation``
# never pulls them in. ``capitalisation.identifiers`` (CP02) re-cases unquoted names, but
# BigQuery table and dataset names are case sensitive and an alias's case is the output
# column's name. They run only when named on their own in ``rules``.
_MEANING_CHANGING_RULES = {"CP02": "capitalisation.identifiers"}


def _excluded_rules(prefs: FormatPreferences) -> tuple[str, ...]:
    named = {rule.lower() for rule in prefs.rules}
    extra = tuple(
        code for code, name in _MEANING_CHANGING_RULES.items()
        if code.lower() not in named and name not in named
    )
    return tuple(dict.fromkeys(prefs.exclude_rules + extra))


def _config(prefs: FormatPreferences):
    from sqlfluff.core import FluffConfig

    policy = prefs.keyword_case
    return FluffConfig(configs={
        "core": {
            "dialect": DIALECT,
            "rules": ",".join(prefs.rules),
            "exclude_rules": ",".join(_excluded_rules(prefs)) or None,
            "max_line_length": prefs.max_line_length,
        },
        "indentation": {"indent_unit": prefs.indent_unit, "tab_space_size": prefs.tab_space_size},
        "layout": {"type": {"comma": {"line_position": prefs.comma_position}}},
        "rules": {
            "capitalisation.keywords": {"capitalisation_policy": policy},
            "capitalisation.functions": {"extended_capitalisation_policy": policy},
            "capitalisation.literals": {"capitalisation_policy": policy},
            "capitalisation.types": {"extended_capitalisation_policy": policy},
        },
    })


# Rule categories that only apply to other SQL dialects; KumoSQL formats BigQuery.
_OTHER_DIALECT_CATEGORIES = frozenset({"tsql", "postgres", "oracle"})


def sqlfluff_rules() -> list[dict]:
    """Every sqlfluff rule that applies to BigQuery, from the installed sqlfluff.

    Each entry has the rule's code, name, one-line description, category (the
    part of the name before the dot), groups, legacy aliases, and whether
    sqlfluff can fix it. Rules it cannot fix only lint, so they never change
    formatted output.
    """

    import sqlfluff
    from sqlfluff.core.rules import get_ruleset

    # Fix support is not in the public rule list; read it from the registry
    # when available and assume fixable otherwise.
    registry = getattr(get_ruleset(), "_register", {})
    rules = []
    for rule in sorted(sqlfluff.list_rules(), key=lambda item: item.code):
        category = rule.name.split(".", 1)[0]
        if category in _OTHER_DIALECT_CATEGORIES:
            continue
        manifest = registry.get(rule.code)
        rules.append({
            "code": rule.code,
            "name": rule.name,
            "description": rule.description,
            "category": category,
            "groups": [group for group in rule.groups if group != "all"],
            "aliases": list(rule.aliases),
            "fixable": bool(getattr(manifest and manifest.rule_class, "is_fix_compatible", True)),
        })
    return rules


_MAX_FORMAT_PASSES = 5
_QUOTED_RE = re.compile(r"`[^`\n]*`")


def _restore_quoted(original: str, formatted: str) -> str:
    """Put back the exact text of every backtick-quoted name.

    sqlfluff changes the case of a quoted function name (`p.d.f`(x) becomes
    `P.D.F`(x)), but BigQuery treats routine and table paths as case
    sensitive. Quoted names are in the same order before and after formatting, so
    they are restored one for one; if the counts or spellings (ignoring case)
    differ, the output is left as sqlfluff made it.
    """

    before, after = _QUOTED_RE.findall(original), _QUOTED_RE.findall(formatted)
    if len(before) != len(after) or any(a.lower() != b.lower() for a, b in zip(before, after)):
        return formatted
    replacements = iter(before)
    return _QUOTED_RE.sub(lambda _: next(replacements), formatted)


def _comments(sql: str) -> list[str] | None:
    """The comment texts BigQuery reads in ``sql``, ``None`` when it cannot be tokenized."""

    import sqlglot

    try:
        # re-indenting the lines of a block comment is layout, so compare the lines' words only
        return ["\n".join(line.strip() for line in c.strip().splitlines()) for token in sqlglot.tokenize(sql, read="bigquery") for c in (token.comments or [])]
    except sqlglot.errors.SqlglotError:
        return None


def format_sql(sql: str, prefs: FormatPreferences = DEFAULT_PREFERENCES) -> str:
    """Format BigQuery SQL with sqlfluff. SQL with no statement sqlfluff can parse raises ``ValueError``."""

    return format_statements(sql, prefs)[0]


def _statement_spans(sql: str) -> list[tuple[int, int]]:
    """Where each statement's text starts and ends, between top-level semicolons (comments around it excluded)."""

    from sqlglot.tokens import TokenType

    tokens = tokenize_exactly(sql) or []
    spans, start, end = [], None, None
    for token in [*tokens, None]:
        if token is None or token.token_type == TokenType.SEMICOLON:
            if start is not None:
                spans.append((start, end))
            start = end = None
            continue
        start = token.start if start is None else start
        end = token.end + 1
    return spans


def format_statements(sql: str, prefs: FormatPreferences = DEFAULT_PREFERENCES) -> tuple[str, int]:
    """Format ``sql`` and return it with the number of statements left as written.

    When sqlfluff cannot parse the whole text (a ``GRANT``, ``EXPORT MODEL`` or a property graph among
    queries it can), each statement is formatted on its own and the ones it cannot parse are kept.
    """

    try:
        return _format_whole(sql, prefs), 0
    except ValueError:
        spans = _statement_spans(sql)
        if len(spans) < 2:
            raise
    pieces, position, kept = [], 0, 0
    for start, end in spans:
        try:
            statement = _format_whole(sql[start:end], prefs)
        except ValueError:
            statement, kept = sql[start:end], kept + 1
        pieces += [sql[position:start], statement]
        position = end
    if kept == len(spans):
        raise ValueError("sqlfluff could not parse this SQL")
    return "".join(pieces) + sql[position:], kept


def _format_whole(sql: str, prefs: FormatPreferences) -> str:
    from sqlfluff.core import Linter

    if not sql.strip():
        return sql
    linter = Linter(config=_config(prefs))
    parsed = linter.parse_string(sql)
    if any(v.rule_code() == "PRS" for v in parsed.violations):
        raise ValueError("sqlfluff could not parse this SQL")
    # A pass must not create or remove a comment: LT01 on `- - -5` gives `---5`, and `--5` starts a comment
    # that swallows the rest of the line. The text before that pass is kept then.
    formatted = linter.lint_string(sql, fix=True).fix_string()[0]
    if _comments(formatted) != _comments(sql):
        formatted = sql
    # One sqlfluff pass is not always a fixed point (a fix can enable another),
    # which would make a second run change the output. Repeat until stable.
    for _ in range(_MAX_FORMAT_PASSES):
        again = linter.lint_string(formatted, fix=True).fix_string()[0]
        if again == formatted:
            break
        if _comments(again) != _comments(formatted):
            break
        formatted = again
    # sqlfluff ends files with a newline; keep the input's ending so diffs stay clean.
    if not sql.endswith("\n"):
        formatted = formatted.rstrip("\n")
    return restore_function_case(sql, _restore_quoted(sql, formatted))


def _same_meaning(before: str, after: str) -> bool:
    """Formatting only moves whitespace and case, so both texts must parse alike.

    sqlfluff joins tokens when it removes spaces: ``- -i`` becomes ``--i``, which
    BigQuery reads as a comment. SQL sqlglot cannot parse is not checked. Comments are compared by
    ``format_sql`` itself, which allows re-indenting the lines of a block comment.
    """

    import sqlglot

    if layout_only_change(before, after):
        # Same tokens and comments: covers statements sqlglot keeps as raw text, whose parse holds the whitespace.
        return True
    try:
        left = sqlglot.parse(before, read="bigquery")
    except Exception:
        return True
    try:
        right = sqlglot.parse(after, read="bigquery")
    except Exception:
        return False
    if len(left) != len(right):
        return False
    return all(
        (a is None and b is None) or (a is not None and b is not None and a.sql("bigquery", comments=False).upper() == b.sql("bigquery", comments=False).upper())
        for a, b in zip(left, right)
    )


@register_rule
class FormatSqlRule(RewriteRule):
    """Format SQL with sqlfluff using the configured preferences."""

    name = "format_sql"
    summary = "Format with sqlfluff (layout, keyword case; set in Settings)"

    def __init__(self, prefs: FormatPreferences | None = None) -> None:
        self.prefs = prefs

    def with_preferences(self, prefs: FormatPreferences) -> "FormatSqlRule":
        return FormatSqlRule(prefs)

    def apply(self, sql: str) -> RuleOutput:
        if not sql.strip():
            return RuleOutput("", 0, 0, 0, 0, ())
        if looks_like_sqlx(sql):
            return RuleOutput(sql, 0, 0, 0, 0, (
                RuleDiagnostic(0, "unsupported_sqlx", "sqlfluff cannot format Dataform SQLX; input left unchanged"),
            ))
        try:
            formatted, kept = format_statements(sql, self.prefs or load_preferences())
        except ValueError as exc:
            return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(0, "parse_error", str(exc)),))
        except Exception as exc:  # sqlfluff can assert on rare inputs; never take the pipeline down
            return RuleOutput(sql, 0, 0, 0, 0, (
                RuleDiagnostic(0, "format_error", f"sqlfluff failed ({type(exc).__name__}); input left unchanged"),
            ))
        if not _same_meaning(sql, formatted):
            return RuleOutput(sql, 0, 0, 0, 0, (
                RuleDiagnostic(0, "format_changed_meaning", "formatting would change how the SQL parses; input left unchanged"),
            ))
        changed = int(formatted != sql)
        notes = (
            (RuleDiagnostic(-1, "statements_not_formatted", f"sqlfluff cannot parse {kept} statement(s); they are left as written"),)
            if kept else ()
        )
        return RuleOutput(formatted, 1, changed, changed, 0, notes)


# ------------------------------------------------------------------ complexity

# Weights per construct: how much each adds to the score. Nesting is scored by
# depth, so a subquery inside a subquery costs more than two side by side.
_WEIGHTS = {
    "joins": 2,
    "ctes": 1,
    "subqueries": 3,
    "set_operations": 2,
    "case_expressions": 1,
    "window_functions": 2,
    "predicates": 0.5,
    "max_nesting": 2,
}
_BANDS = ((10, "low"), (25, "moderate"), (50, "high"))


@dataclass(frozen=True)
class Complexity:
    score: float
    band: str
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"score": self.score, "band": self.band, "metrics": dict(self.metrics)}


def _band(score: float) -> str:
    return next((name for limit, name in _BANDS if score < limit), "very high")


def subqueries(tree) -> int:
    """Count SELECTs in parentheses that are not CTE bodies."""

    total = 0

    def walk(segment, parents: tuple[str, ...]) -> None:
        nonlocal total
        if segment.is_type("select_statement") and parents[-1:] == ("bracketed",) \
                and "common_table_expression" not in parents[-2:-1]:
            total += 1
        for child in segment.segments:
            walk(child, (*parents, segment.type))

    walk(tree, ())
    return total


_LINTERS = threading.local()


def _parse_linter():
    """A sqlfluff linter for parsing BigQuery, built once per thread.

    Its configuration is sqlfluff's defaults plus the dialect (no config files are read), so every parse gets the
    same tree a new linter would give; building the linter and its dialect took about a fifth of each parse.
    """

    linter = getattr(_LINTERS, "parse", None)
    if linter is None:
        from sqlfluff.core import FluffConfig, Linter

        linter = _LINTERS.parse = Linter(config=FluffConfig(overrides={"dialect": DIALECT}))
    return linter


@lru_cache(maxsize=4096)
def _table_score(sql: str) -> float:
    return complexity(sql).score


def pipeline_complexity(tables: Mapping[str, str]) -> dict:
    """Complexity of a pipeline of tables: ``complexity`` summed over its queries, plus one per table.

    The per-table term makes a pass-through table count (a bare ``SELECT *`` scores 0 on its own).
    Returns ``{"score", "structural", "tables"}``; lower is better. Raises ``ValueError`` if a query
    cannot be parsed.
    """

    structural = round(sum(_table_score(sql) for sql in tables.values()), 1)
    return {"score": round(structural + len(tables), 1), "structural": structural, "tables": len(tables)}


def complexity(sql: str) -> Complexity:
    """Score a query's structural complexity from its sqlfluff parse tree.

    The score is a weighted sum of joins (comma joins included), CTEs, subqueries,
    set operations, CASE expressions (IF included), window functions, boolean predicates and maximum SELECT
    nesting depth (weights in ``_WEIGHTS``). Bands: <10 low, <25 moderate,
    <50 high, otherwise very high. Raises ``ValueError`` if it cannot parse.
    """

    if looks_like_sqlx(sql):
        raise ValueError("complexity is not available for Dataform SQLX")
    parsed = _parse_linter().parse_string(sql)
    tree = parsed.tree
    if tree is None or any(v.rule_code() == "PRS" for v in parsed.violations):
        raise ValueError("sqlfluff could not parse this SQL")

    def count(*types: str) -> int:
        return len(list(tree.recursive_crawl(*types)))

    def depth(segment, level=0) -> int:
        level += segment.is_type("select_statement")
        return max([level, *(depth(child, level) for child in segment.segments)])

    selects = count("select_statement")
    metrics = {
        # A comma join (FROM a, b) is a join too: each FROM item after the first counts like JOIN ... ON.
        "joins": count("join_clause") + sum(
            max(0, len([s for s in clause.segments if s.is_type("from_expression")]) - 1)
            for clause in tree.recursive_crawl("from_clause")
        ),
        "ctes": count("common_table_expression"),
        # Every SELECT beyond the top-level ones (CTE bodies aside) is nested.
        "subqueries": subqueries(tree),
        "set_operations": count("set_operator"),
        # IF(c, a, b) is a CASE expression written as a function.
        "case_expressions": count("case_expression") + len([
            f for f in tree.recursive_crawl("function")
            if any(s.is_type("function_name") and s.raw.strip().upper() == "IF" for s in f.segments)
        ]),
        "window_functions": count("over_clause"),
        "predicates": len([
            op for op in tree.recursive_crawl("binary_operator") if op.raw.upper() in ("AND", "OR")
        ]),
        "max_nesting": max(0, depth(tree) - 1),
        "lines": len(sql.splitlines()),
    }
    score = round(sum(metrics[key] * weight for key, weight in _WEIGHTS.items()), 1)
    return Complexity(score, _band(score), metrics)
