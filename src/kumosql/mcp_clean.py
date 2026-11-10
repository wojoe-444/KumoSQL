"""``clean_query``: tidy a query with KumoSQL's verified rewrites, and say only what changed.

The product's cleanup rules (``remove_trivial_predicates``, ``remove_redundant_parentheses``, ``deduplicate_ctes``,
``remove_unused_ctes``, ``remove_redundant_distinct``, and on request ``inline_single_use_ctes``) behind one call. Each change
is checked by the equivalence prover and an independent checker, exactly as ``rewrite-sql`` does. The answer is a small
unified diff (one line of context), not the whole query, so an agent that holds the original pays only for what changed; for a
draft short enough, the cleaned text itself is included.

* ``cleaned``: something changed and every change was proven to keep the query's meaning;
* ``already_clean``: the rules found nothing to change;
* ``unproven``: something would change but a proof was not established. No diff is offered: unknown is never offered as a rewrite;
* ``cannot_clean``: the text does not parse, or a rule failed.

Read-only: it never writes the file a ``table`` is read from. The checks run in a forked process under a time cap.
"""

from __future__ import annotations

import difflib
import time
from pathlib import Path

from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_compare import MAX_SECONDS, run_capped
from .mcp_session import Session
from .mcp_tools import need_table

MAX_TEXT = 100_000
DEFAULT_SECONDS = 20
PAGE = 60
INLINE_LIMIT = 1200  # a cleaned draft this short is returned whole
SAFE_RULES = ("remove_trivial_predicates", "remove_redundant_parentheses", "deduplicate_ctes", "remove_unused_ctes", "remove_redundant_distinct")


#: what the diagnostics a rule leaves mean for an agent (``docs/rewrite-rules.md``)
EXPLAIN = {
    "sqlx_expression_kept": "a ${...} expression other than ref(), resolve() or self() compiles to arbitrary SQL, so the rule does not touch that statement",
    "pipe_syntax_kept": "pipe syntax (|>) is read through a translation, so it is not rewritten",
    "templated_sql_kept": "a Jinja tag ({{ ... }}) is not SQL, so it is not rewritten",
    "recovered_parse": "the text only parsed in recovery mode",
}


def chosen_rules(inline_ctes: bool) -> tuple[str, ...]:
    """The rules to apply, in the product's canonical order."""

    from .rewrite import canonical_rule_order

    wanted = set(SAFE_RULES) | ({"inline_single_use_ctes"} if inline_ctes else set())
    return tuple(rule for rule in canonical_rule_order() if rule in wanted)


def clean_text(text: str, rules: tuple[str, ...]) -> dict:
    """Plain data for the cleaned text (runs in the forked child)."""

    from . import apply_rules

    result = apply_rules(rules, text)
    # every rule says the same about a text it cannot read: keep each distinct reason once, without the rule's name
    reasons = list(dict.fromkeys(clip(detail.split(": ", 1)[-1], 200) for detail in result.verification.details))[:3]
    if not reasons and not result.success:
        reasons = [clip(result.verification.reason, 200)]
    left_as_written = list(dict.fromkeys(f"{d.code}" for step in result.steps for d in step.diagnostics))
    return {
        "left_as_written": left_as_written,
        "sql": result.sql,
        "success": bool(result.success),
        "status": result.verification.status.value,
        "changes": {step.rule: step.changes for step in result.steps if step.changes},
        "reasons": reasons,
    }


def unified(before: str, after: str, name: str) -> list[str]:
    return list(difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{name}", f"b/{name}", n=1, lineterm=""))


def model_text(session: Session, key: str) -> tuple[str, str]:
    """The file text of model ``key`` and its path in the project; a refusal when it cannot be read from inside the project folder."""

    model = session.pipeline().models.get(key)
    if model is None:
        raise ToolError(f"{key.split('.')[-1]} is a source: the project does not build it, so it has no query to clean")
    if not model.path or session.root is None:
        raise ToolError("this project's files are not available to read", ["Pass the query as sql=... instead."])
    root = Path(session.root).resolve()
    path = (root / model.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ToolError(f"cannot read {clip(model.path, 80)} from the project folder", ["Pass the query as sql=... instead."])
    return path.read_text(encoding="utf-8", errors="replace"), model.path


@tool(
    "clean_query",
    "Clean up a query",
    "Tidy a query with KumoSQL's verified rewrites (always-true filters, redundant parentheses, duplicate or unused CTEs, "
    "redundant DISTINCT; single-use CTE inlining only on request) and get back a small diff of what changed, each change proven "
    "to keep the query's meaning. Pass sql (a draft) or table (a model of the project, read from its file; the file is never "
    "written). A change that cannot be proven is reported, never offered.",
    {
        "sql": {"type": "string", "description": "The query text to clean (a draft; Dataform ${...} is left as written)."},
        "table": {"type": "string", "description": "Instead of sql: a model of the project, whose own file is cleaned."},
        "inline_ctes": {"type": "boolean", "description": "Also inline CTEs used once as subqueries (reprints the query; default false)."},
        "time_limit": {"type": "integer", "description": f"Seconds the proofs may take (default {DEFAULT_SECONDS}, from 1 to {MAX_SECONDS})."},
    },
)
def clean_query(session: Session, args: dict) -> Answer:
    if ("sql" in args) == ("table" in args):
        raise ToolError("pass exactly one of sql and table", ["sql=... cleans a draft; table=... cleans a model's own file."])
    seconds = args.get("time_limit", DEFAULT_SECONDS)
    if not 1 <= seconds <= MAX_SECONDS:
        raise ToolError(f"time_limit must be from 1 to {MAX_SECONDS} seconds", [f"Pass time_limit between 1 and {MAX_SECONDS}."])
    if "table" in args:
        key = need_table(session, args["table"])
        text, name = model_text(session, key)
    else:
        text, name = args["sql"], "draft.sql"
    if len(text) > MAX_TEXT:
        raise ToolError(f"the query is too long ({MAX_TEXT:,} characters at most)")
    rules = chosen_rules(bool(args.get("inline_ctes", False)))

    began = time.monotonic()
    out, failure = run_capped(lambda: clean_text(text, rules), seconds)
    took = time.monotonic() - began

    answer = Answer("")
    answer.counts = {"seconds": round(took, 1), "rules_tried": len(rules)}
    if out is None:
        answer.summary = f"Cannot clean: the check {'did not finish in %d s' % seconds if failure == 'timeout' else 'failed (' + str(failure).split(':')[-1] + ')'}."
        answer.fields["verdict"] = "cannot_clean"
        answer.unknown("whether the rules change anything safely", "no result within the time cap" if failure == "timeout" else "the rewrite stopped with an error")
        answer.next(f"Pass a larger time_limit (at most {MAX_SECONDS}) or clean a smaller piece.")
        return answer

    changed = out["sql"] != text
    if changed and out["success"]:
        diff = unified(text, out["sql"], name)
        offset = offset_wanted(args)
        answer.summary = f"Cleaned: {sum(out['changes'].values())} change{'s' if sum(out['changes'].values()) != 1 else ''}, every one proven to keep the query's meaning."
        answer.fields["verdict"] = "cleaned"
        answer.fields["changes"] = [f"{rule} x{count}" for rule, count in out["changes"].items()]
        answer.counts["diff_lines"] = len(diff)
        # lines are kept as written (indentation included): a diff that cannot be applied is of no use
        answer.add_list("diff", diff[offset:][:PAGE], total=len(diff), offset=offset, primary=True)
        if "sql" in args and len(out["sql"]) <= INLINE_LIMIT:
            answer.fields["cleaned_sql"] = out["sql"]
        answer.next("Apply the diff to the original, or take cleaned_sql." if "cleaned_sql" in answer.fields else "Apply the diff to the original; nothing was written.")
    elif not changed and out["success"]:
        answer.summary = "Already clean: the rules found nothing to change."
        answer.fields["verdict"] = "already_clean"
        if out["left_as_written"]:
            answer.summary = "Nothing changed, but some rules left the text as written."
            answer.fields["verdict"] = "already_clean"
            answer.unknown("whether the text is clean", f"rules left it as written ({', '.join(out['left_as_written'])}): " + EXPLAIN.get(out["left_as_written"][0], "see the rule diagnostics in docs/rewrite-rules.md"))
        answer.next("check_query looks for ties and join fan-out, which cleanup does not touch.")
    elif changed:
        answer.summary = "A change is possible but could not be proven to keep the query's meaning, so none is offered."
        answer.fields["verdict"] = "unproven"
        answer.unknown("whether the rewrite keeps the query's meaning", "; ".join(out["reasons"]) or out["status"])
        answer.next("Edit the query by hand and use compare_queries to check the edit.")
    else:
        answer.summary = "Cannot clean: a rule could not run on this text."
        answer.fields["verdict"] = "cannot_clean"
        answer.unknown("whether the rules apply", "; ".join(out["reasons"]) or out["status"])
        answer.next("Fix the syntax and ask again; the text must be one SELECT or a Dataform file.")
    return answer
