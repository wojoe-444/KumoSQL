"""Rewrite-rule base class and the shared driver (no rule registry, no scripting).

A rule only describes how to rewrite one parsed statement. The driver in
``RewriteRule.apply`` owns everything rules have in common: SQLX sectioning
and interpolation masking, strict parsing with a visible recovery fallback,
per-statement error isolation, output formatting, byte-for-byte no-ops,
re-parsing the output, and CTE dependency checks.

Two things deliberately live outside this module so the equivalence prover (which normalizes queries with
``lift_subqueries``) does not depend on them:

* the registry of rules (``kumosql.engine``: ``register_rule``, ``get_rule``, ``available_rules``), and
* BigQuery scripting (``BEGIN ... END``, ``IF``, loops, procedures; ``kumosql.scripts``). A product that wants
  rules to run inside scripts calls ``set_script_support`` once, which ``kumosql.engine`` does. Without it a
  script is parsed like any other text, which is what the prover wants: it only ever sees one query.

Semantic verification lives one layer up in ``kumosql.rewrite`` so the
equivalence prover can itself use rules without an import cycle.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any, ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.tokens import TokenType

from .ast_utils import cte_dependency_errors, parse_statements, render_statement
from .sqlx import (
    opaque_tokens,
    SqlxRestorationError,
    looks_like_sqlx,
    mask_sqlx_interpolations,
    restore_sqlx_interpolations,
    split_sqlx_sections,
    with_preserved_whitespace,
)


_script_support: Any = None  # an object with ``has_blocks``, ``leaf_statements`` and ``is_rewriteable``; see ``set_script_support``


def set_script_support(support: Any) -> None:
    """Teach the driver to rewrite the statements inside BigQuery scripts.

    ``support`` provides ``has_blocks(sql)``, ``leaf_statements(sql)`` (nodes with ``header``, ``start`` and
    ``end``) and ``is_rewriteable(header)``: the interface of ``kumosql.scripts``. ``None`` turns it off.
    """

    global _script_support
    _script_support = support


_BLOCK_WORDS = re.compile(r"\b(?:BEGIN|LOOP|WHILE|REPEAT|THEN|DO)\b", re.IGNORECASE)  # cheap test before cutting a script apart


def _sql_comments(sql: str) -> list[str]:
    """Extract SQL comments without mistaking comment markers in literals."""

    return [comment for _, _, comment in _sql_comment_spans(sql)]


def _sql_comment_spans(sql: str) -> list[tuple[int, int, str]]:
    """Return comment spans while skipping quoted SQL strings and identifiers."""

    comments: list[tuple[int, int, str]] = []
    i = 0
    while i < len(sql):
        if sql.startswith("--", i):
            start = i
            end = sql.find("\n", i + 2)
            if end < 0:
                end = len(sql)
            comments.append((start, end, sql[start:end]))
            i = end
            continue
        if sql.startswith("/*", i):
            start = i
            end = sql.find("*/", i + 2)
            if end < 0:
                break
            end += 2
            comments.append((start, end, sql[start:end]))
            i = end
            continue
        if sql[i] in "'\"`":
            quote = sql[i]
            triple = sql.startswith(quote * 3, i)
            delimiter = quote * (3 if triple else 1)
            i += len(delimiter)
            while i < len(sql):
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql.startswith(delimiter, i):
                    # SQL's doubled quote escapes a quote in single/double
                    # quoted strings and identifiers.
                    if not triple and sql.startswith(quote * 2, i):
                        i += 2
                        continue
                    i += len(delimiter)
                    break
                i += 1
            continue
        i += 1
    return comments


_KEYWORD_TOKEN_TYPES = set(BigQuery.Tokenizer.KEYWORDS.values()) | {TokenType.ALIAS}


def _token_key(token) -> tuple[TokenType, str]:
    text = token.text.upper() if token.token_type in _KEYWORD_TOKEN_TYPES else token.text
    return token.token_type, text


def _render_or_source(statement: exp.Expression, source: str) -> str:
    """Render a statement no rule changed; where sqlglot cannot (a recovered, truncated predicate), use its source text.

    The rendering only checks that the output still parses to the same statements, so the text the
    statement was parsed from stands in for it.
    """

    try:
        return render_statement(_without_comments(statement))
    except Exception:  # noqa: BLE001 - the compiled generator raises TypeError on a missing operand
        return source


def _without_comments(statement: exp.Expression) -> exp.Expression:
    result = statement.copy()
    for node in result.walk():
        node.comments = None
    return result


def _same_ast(left: list[exp.Expression], right: list[exp.Expression]) -> bool:
    return len(left) == len(right) and all(
        _without_comments(a) == _without_comments(b) for a, b in zip(left, right)
    )


def _uses_pipe_syntax(sql: str) -> bool:
    try:
        return any(token.token_type is TokenType.PIPE_GT for token in sqlglot.tokenize(sql, read="bigquery"))
    except Exception:  # noqa: BLE001
        return "|>" in sql


def has_template_tags(sql: str) -> bool:
    """Whether ``sql`` holds a Jinja tag (``{{``, ``{%`` or ``{#``) outside string literals and comments.

    sqlglot reads ``{{ x }}`` as a nested struct literal and prints it back as ``STRUCT(STRUCT(x))``, so a rule
    would silently rewrite the template; templated SQL is left exactly as written instead.
    """

    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char == "{" and sql[i + 1 : i + 2] in ("{", "%", "#"):
            return True
        if char in "'\"`":
            quote = sql[i : i + 3] if sql[i : i + 3] in ("'''", '"""') else char
            j = i + len(quote)
            while j < n and not sql.startswith(quote, j):
                j += 2 if sql[j] == "\\" else 1
            i = j + len(quote)
            continue
        if char == "#" or sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        i += 1
    return False


def _rewriteable_statements(statements: list[exp.Expression]) -> list[tuple[int, exp.Expression]]:
    """Ignore parser-only semicolon nodes while keeping original indices."""

    return [
        (index, statement)
        for index, statement in enumerate(statements)
        if not isinstance(statement, exp.Semicolon)
    ]


def _statement_segments(
    sql: str, expected_count: int, *, recover: bool
) -> list[tuple[int, int, exp.Expression]] | None:
    """Locate each parsed statement in source without splitting literal semicolons."""

    try:
        tokens = sqlglot.tokenize(sql, read="bigquery")
        segments: list[tuple[int, int, exp.Expression]] = []
        cursor = 0
        for token in tokens:
            if token.token_type is not TokenType.SEMICOLON:
                continue
            parsed = parse_statements(sql[cursor : token.start], recover=recover)
            if len(parsed) > 1:
                return None
            if parsed:
                segments.append((cursor, token.start, parsed[0]))
            cursor = token.end + 1

        parsed = parse_statements(sql[cursor:], recover=recover)
        if len(parsed) > 1:
            return None
        if parsed:
            segments.append((cursor, len(sql), parsed[0]))
    except Exception:
        return None
    return segments if len(segments) == expected_count else None


def _format_preserved_comments(comments: list[str], *, line_start: bool) -> str:
    if not comments:
        return ""
    rendered = ""
    for comment in comments:
        if comment.startswith("--"):
            rendered += ("" if line_start and not rendered else "\n") + comment + "\n"
        else:
            rendered += comment + " "
    return rendered


def _splice_statement(source: str, rendered: str) -> str:
    """Patch only token-different spans, preserving all other source bytes."""

    source_tokens = sqlglot.tokenize(source, read="bigquery")
    target_tokens = sqlglot.tokenize(rendered, read="bigquery")
    if not source_tokens or not target_tokens:
        raise ValueError("a changed statement could not be aligned to SQL tokens")

    source_keys = [_token_key(token) for token in source_tokens]
    target_keys = [_token_key(token) for token in target_tokens]
    matcher = SequenceMatcher(a=source_keys, b=target_keys, autojunk=False)
    comment_spans = _sql_comment_spans(source)
    edits: list[tuple[int, int, str]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        has_left = i1 > 0
        has_right = i2 < len(source_tokens)
        if has_left:
            source_start = source_tokens[i1 - 1].end + 1
        elif i1 < len(source_tokens):
            source_start = source_tokens[i1].start
        else:
            source_start = len(source)
        if has_right:
            source_end = source_tokens[i2].start
        elif i2 > i1:
            source_end = source_tokens[i2 - 1].end + 1
        else:
            source_end = source_start

        target_has_left = j1 > 0
        target_has_right = j2 < len(target_tokens)
        if target_has_left:
            target_start = target_tokens[j1 - 1].end + 1
        elif j1 < len(target_tokens):
            target_start = target_tokens[j1].start
        else:
            target_start = len(rendered)
        if target_has_right:
            target_end = target_tokens[j2].start
        elif j2 > j1:
            target_end = target_tokens[j2 - 1].end + 1
        else:
            target_end = target_start

        replacement = rendered[target_start:target_end]
        enclosed = [
            (start, end, comment)
            for start, end, comment in comment_spans
            if source_start <= start and end <= source_end
        ]
        left_comments: list[str] = []
        right_comments: list[str] = []
        left_anchor = source_tokens[i1 - 1].end + 1 if has_left else source_start
        right_anchor = source_tokens[i2].start if has_right else source_end
        for start, end, comment in enclosed:
            if not has_right or (has_left and start - left_anchor <= right_anchor - end):
                left_comments.append(comment)
            else:
                right_comments.append(comment)

        if left_comments:
            leading = re.match(r"\s*", replacement).group(0)
            remainder = replacement[len(leading) :]
            if has_left and not leading:
                leading = " "
            replacement = leading + _format_preserved_comments(
                left_comments, line_start=not has_left
            ) + remainder
        if right_comments:
            trailing = re.search(r"\s*$", replacement).group(0)
            body = replacement[: len(replacement) - len(trailing)] if trailing else replacement
            if body and not body.endswith((" ", "\t", "\r", "\n")):
                body += " "
            replacement = body + _format_preserved_comments(
                right_comments, line_start=False
            ) + trailing

        edits.append((source_start, source_end, replacement))

    result = source
    for start, end, replacement in reversed(edits):
        result = result[:start] + replacement + result[end:]
    if sorted(_sql_comments(result)) != sorted(_sql_comments(source)):
        raise ValueError("a source comment could not be retained during span editing")
    return result


# Sentinels of the SQLX expressions (not table references) in the section a rule is rewriting.
_OPAQUE_SQLX: ContextVar[frozenset[str]] = ContextVar("kumosql_opaque_sqlx", default=frozenset())

FATAL_DIAGNOSTIC_CODES = frozenset(
    {
        "parse_error",
        "sqlx_parse_error",
        "sqlx_restore_error",
        "transform_error",
        "cte_dependency_error",
        "inline_subqueries_remaining",
        "output_parse_error",
        "source_splice_error",
    }
)


@dataclass(frozen=True)
class RuleDiagnostic:
    """One statement-level parse or transformation diagnostic."""

    statement_index: int
    code: str
    message: str


@dataclass(frozen=True)
class RuleOutput:
    """Syntactic result of applying one rule to a SQL or SQLX text."""

    sql: str
    statements: int
    changed_statements: int
    changes: int
    remaining: int
    diagnostics: tuple[RuleDiagnostic, ...]

    @property
    def success(self) -> bool:
        """Whether the rule ran cleanly and left nothing it was meant to remove."""

        return (
            not any(d.code in FATAL_DIAGNOSTIC_CODES for d in self.diagnostics)
            and self.remaining == 0
        )


class RewriteRule:
    """Base class for a deterministic, statement-local rewrite rule.

    Subclasses set ``name`` and ``summary`` and implement ``rewrite_statement``.
    Rules whose goal is to eliminate a construct (such as inline subqueries)
    also override ``count_remaining`` so leftovers are reported as failures.
    """

    name: ClassVar[str]
    summary: ClassVar[str]
    #: Rewrite statements written in pipe syntax. sqlglot parses ``|>`` into nested CTEs and prints them
    #: as standard SQL, so only analysis that never shows its output (the prover) turns this on.
    rewrite_pipe_syntax: ClassVar[bool] = False
    #: Only the prover's normalization reads the output, never a user: templated SQL is read the way sqlglot
    #: reads it.
    analysis_only: ClassVar[bool] = False
    #: Leave a SQLX statement alone when it holds a ``${...}`` expression other than ``ref()``/``self()``. The rule
    #: sees only a placeholder name there, but the expression can compile to a looser-binding predicate, a whole
    #: clause or a query that reads a CTE, so dropping parentheses, predicates or CTEs around it can break it.
    keep_sqlx_expressions: ClassVar[bool] = False
    #: Left out of ``canonical_rule_order()`` and of the benchmark and fuzz sweeps over every rule: a style
    #: choice, or a rule that needs facts (table columns) those inputs do not declare. Run it by name.
    opt_in: ClassVar[bool] = False

    def rewrite_statement(
        self, statement: exp.Expression, index: int
    ) -> tuple[int, list[RuleDiagnostic]]:
        """Rewrite ``statement`` in place; return the change count and diagnostics."""

        raise NotImplementedError

    def count_remaining(self, statements: list[exp.Expression]) -> int:
        """Count constructs this rule should have removed but did not."""

        return 0

    def apply(self, sql: str) -> RuleOutput:
        """Apply the rule to BigQuery SQL or Dataform SQLX."""

        if looks_like_sqlx(sql):
            return self._apply_sqlx(sql)
        return self._apply_sql(sql)

    def _apply_sql(self, sql: str) -> RuleOutput:
        if not sql or not sql.strip():
            return RuleOutput("", 0, 0, 0, 0, ())
        if not self.analysis_only and has_template_tags(sql):
            return RuleOutput(
                sql, 0, 0, 0, 0,
                (RuleDiagnostic(-1, "templated_sql_kept", "Jinja templating ({{ }}, {% %}, {# #}) is left as written"),),
            )
        if _BLOCK_WORDS.search(sql) and _script_support is not None and _script_support.has_blocks(sql):
            return self._apply_script(sql)

        try:
            statements = parse_statements(sql)
            recovered = False
        except Exception as exc:
            if re.search(r"\b(?:FROM|JOIN|SELECT|AS|WHERE|ON)\s*$", sql.strip(), re.IGNORECASE):
                return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(0, "parse_error", str(exc)),))
            try:
                statements = parse_statements(sql, recover=True)
            except Exception:
                return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(0, "parse_error", str(exc)),))
            recovered = True

        diagnostics: list[RuleDiagnostic] = []
        if recovered:
            diagnostics.append(
                RuleDiagnostic(
                    -1,
                    "recovered_parse",
                    "Strict BigQuery parsing failed; transformed statements using sqlglot recovery mode",
                )
            )

        initial_remaining = self.count_remaining(statements)
        rewriteable = _rewriteable_statements(statements)
        segments = _statement_segments(sql, len(rewriteable), recover=recovered)
        if segments is None or not _same_ast(
            [statement for _, statement in rewriteable],
            [statement for _, _, statement in segments],
        ):
            return RuleOutput(
                sql,
                len(statements),
                0,
                0,
                initial_remaining,
                (
                    *diagnostics,
                    RuleDiagnostic(
                        -1,
                        "source_splice_error",
                        "SQL statements could not be mapped to their original source spans",
                    ),
                ),
            )

        rewritten_statements: list[exp.Expression] = []
        rendered_statements: list[str] = []
        edits: list[tuple[int, int, str]] = []
        changed_statements = 0
        changes = 0
        opaque = _OPAQUE_SQLX.get() if self.keep_sqlx_expressions else frozenset()
        for (index, _), (start, end, statement) in zip(rewriteable, segments):
            if opaque and any(token in sql[start:end] for token in opaque):
                diagnostics.append(
                    RuleDiagnostic(index, "sqlx_expression_kept", "a ${...} expression other than ref() or self() is left as written")
                )
                rewritten_statements.append(statement)
                rendered_statements.append(_render_or_source(statement, sql[start:end]))
                continue
            if not self.rewrite_pipe_syntax and _uses_pipe_syntax(sql[start:end]):
                # sqlglot turns pipe syntax into nested CTEs and subqueries when it parses it, so a rule
                # would rewrite that translation (and print it as standard SQL), not what was written.
                diagnostics.append(
                    RuleDiagnostic(index, "pipe_syntax_kept", "pipe syntax (|>) is left as written")
                )
                rewritten_statements.append(statement)
                rendered_statements.append(_render_or_source(statement, sql[start:end]))
                continue
            before = statement.copy()
            try:
                count, statement_diagnostics = self.rewrite_statement(statement, index)
                diagnostics.extend(statement_diagnostics)
                for error in cte_dependency_errors(statement):
                    diagnostics.append(RuleDiagnostic(index, "cte_dependency_error", error))
                if count:
                    changed_statements += 1
                    changes += count
                    rendered_statement = render_statement(_without_comments(statement))
                    try:
                        replacement = _splice_statement(
                            sql[start:end], rendered_statement
                        )
                    except Exception as exc:
                        diagnostics.append(
                            RuleDiagnostic(
                                index,
                                "source_splice_error",
                                f"SQL source spans could not be safely edited: {exc}",
                            )
                        )
                        return RuleOutput(
                            sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics)
                        )
                    edits.append((start, end, replacement))
                rewritten_statements.append(statement)
            except Exception as exc:
                diagnostics.append(RuleDiagnostic(index, "transform_error", str(exc)))
                # Do not keep a partially mutated AST after a failed rule step.
                statement = before
                rewritten_statements.append(before)
            rendered_statements.append(_render_or_source(statement, sql[start:end]))

        if changes == 0:
            if not _same_ast(
                [statement for _, statement in rewriteable], rewritten_statements
            ):
                diagnostics.append(
                    RuleDiagnostic(
                        -1,
                        "source_splice_error",
                        "A rule changed an AST without reporting a source edit",
                    )
                )
                return RuleOutput(
                    sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics)
                )
            return RuleOutput(sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics))

        output = sql
        for start, end, replacement in reversed(edits):
            output = output[:start] + replacement + output[end:]
        try:
            output_statements = parse_statements(output, recover=recovered)
        except Exception as exc:
            diagnostics.append(RuleDiagnostic(-1, "output_parse_error", str(exc)))
            return RuleOutput(sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics))

        try:
            rendered_reference = ";\n\n".join(
                part.rstrip() for part in rendered_statements if part.strip()
            )
            rendered_reference_statements = parse_statements(
                rendered_reference, recover=recovered
            )
        except Exception as exc:
            diagnostics.append(RuleDiagnostic(-1, "output_parse_error", str(exc)))
            return RuleOutput(sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics))

        if not _same_ast(
            [statement for statement in rendered_reference_statements if not isinstance(statement, exp.Semicolon)],
            [statement for statement in output_statements if not isinstance(statement, exp.Semicolon)],
        ):
            diagnostics.append(
                RuleDiagnostic(
                    -1,
                    "output_parse_error",
                    "Spliced SQL did not preserve the rewritten statement structure",
                )
            )
            return RuleOutput(sql, len(statements), 0, 0, initial_remaining, tuple(diagnostics))

        remaining = self.count_remaining(output_statements)
        for index, statement in enumerate(output_statements):
            for error in cte_dependency_errors(statement):
                diagnostics.append(RuleDiagnostic(index, "cte_dependency_error", error))

        return RuleOutput(
            output,
            len(statements),
            changed_statements,
            changes,
            remaining,
            tuple(diagnostics),
        )

    def _apply_script(self, sql: str) -> RuleOutput:
        """A script with ``BEGIN ... END``, ``IF``, loops or procedures: the rule runs on each statement inside, in place.

        Declarations, variable assignments, transactions and control flow are left exactly as written.
        """

        edits: list[tuple[int, int, str]] = []
        diagnostics: list[RuleDiagnostic] = []
        statements = changed_statements = changes = remaining = 0
        for node in _script_support.leaf_statements(sql):
            if not _script_support.is_rewriteable(node.header):
                continue
            text = sql[node.start : node.end]
            result = self._apply_sql(text)
            index = statements
            statements += max(result.statements, 1)
            changed_statements += result.changed_statements
            changes += result.changes
            remaining += result.remaining
            diagnostics.extend(
                RuleDiagnostic(index if d.statement_index >= 0 else d.statement_index, d.code, d.message) for d in result.diagnostics
            )
            if result.sql != text:
                edits.append((node.start, node.end, result.sql))
        output = sql
        for start, end, replacement in reversed(edits):
            output = output[:start] + replacement + output[end:]
        return RuleOutput(output, statements, changed_statements, changes, remaining, tuple(diagnostics))

    def _apply_sqlx(self, sql: str) -> RuleOutput:
        try:
            sections = split_sqlx_sections(sql)
        except Exception as exc:
            return RuleOutput(sql, 0, 0, 0, 0, (RuleDiagnostic(-1, "sqlx_parse_error", str(exc)),))

        rendered: list[str] = []
        diagnostics: list[RuleDiagnostic] = []
        statements = 0
        changed_statements = 0
        changes = 0

        for kind, section in sections:
            if kind == "block" or not section.strip():
                rendered.append(section)
                continue
            try:
                masked, restorations = mask_sqlx_interpolations(section)
                reset = _OPAQUE_SQLX.set(opaque_tokens(restorations))
                try:
                    result = self._apply_sql(masked)
                finally:
                    _OPAQUE_SQLX.reset(reset)
                restored = restore_sqlx_interpolations(result.sql, restorations)
                rendered.append(with_preserved_whitespace(section, restored))
                statements += result.statements
                changed_statements += result.changed_statements
                changes += result.changes
                diagnostics.extend(result.diagnostics)
            except SqlxRestorationError as exc:
                diagnostics.extend(result.diagnostics)
                diagnostics.append(RuleDiagnostic(-1, "sqlx_restore_error", str(exc)))
                # Treat SQLX rewriting transactionally: a failed restoration
                # must not expose output with a missing template expression.
                return RuleOutput(sql, statements, 0, 0, 0, tuple(diagnostics))
            except Exception as exc:
                rendered.append(section)
                diagnostics.append(RuleDiagnostic(-1, "sqlx_parse_error", str(exc)))

        output = "".join(rendered)
        try:
            remaining = self._count_remaining_sqlx(output)
        except Exception as exc:
            remaining = 0
            diagnostics.append(RuleDiagnostic(-1, "output_parse_error", str(exc)))

        if changes == 0:
            output = sql

        return RuleOutput(
            output, statements, changed_statements, changes, remaining, tuple(diagnostics)
        )

    def _count_remaining_sqlx(self, sql: str) -> int:
        total = 0
        for kind, section in split_sqlx_sections(sql):
            if kind == "block" or not section.strip():
                continue
            masked, _ = mask_sqlx_interpolations(section)
            total += self.count_remaining(parse_statements(masked))
        return total
