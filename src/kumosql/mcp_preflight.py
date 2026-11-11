"""``check_query``: is this draft sound before I run it?

One call instead of four commands, on a draft ``SELECT`` written against the project's tables:

* **types**: the output columns and their types (:mod:`kumosql.googlesql_types`), and the errors the query certainly has;
* **ties**: places where the result can change from run to run because rows tie
  (:mod:`kumosql.tie_determinism`), with the key that would pin them down;
* **fan-out**: joins that can multiply rows, judged from the grain of the joined tables (the project's table profiles);
* **BigQuery dry run**: only when ``dry_run=true`` *and* a ``project`` is given. It plans the query without running it
  (free, no data read), reports the estimated bytes against the byte cap of Settings → Scopes and the column types BigQuery
  returns. It is the only part that reaches the network; without credentials it says ``not checked``.

Every finding names its fix. Anything a check could not decide is listed under ``unknown`` with the reason. "No problems
found" means the checks that ran found none, never that the query is correct.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .mcp_answers import Answer, ToolError, clip, offset_wanted, tool
from .mcp_session import Session
from .mcp_tools import expand_refs, explain, leftover_expressions

MAX_SQL = 50_000
PAGE = 50

#: what to do about each error the type checker can certify
TYPE_FIXES = {
    "unknown_column": "Use a column the table has: describe_table table=... lists them.",
    "unknown_table": "Use a table of the project (find_tables looks for one).",
    "ambiguous_column": "Qualify the column with its table alias (alias.column).",
    "set_operation_width": "Make every branch of the UNION/INTERSECT/EXCEPT select the same number of columns.",
    "set_operation_type": "CAST the columns of the branches to one common type.",
    "invalid_field_access": "Check that the column is a STRUCT with that field; describe_table shows the columns.",
    "star_except_missing": "Remove that name from EXCEPT/REPLACE, or correct its spelling.",
}
_FAILED_META = frozenset({"unexpanded_star", "unparsed_model", "unparsed", "cycle", "qualify_error", "parse_error", "unknown_column"})
_AGGREGATES = (exp.Sum, exp.Count, exp.Avg)


# ----------------------------------------------------------------------- the draft


class Draft:
    """The draft parsed once, with the project tables it reads."""

    def __init__(self, session: Session, sql: str) -> None:
        self.session = session
        self.sql = sql
        self.tree: exp.Expression | None = None
        self.error: str | None = None
        pipeline = session.pipeline()
        try:
            statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None and not isinstance(s, exp.Semicolon)]
        except Exception as exc:  # noqa: BLE001
            text = re.sub(r"\x1b\[[0-9;]*m", "", f"{type(exc).__name__}: {exc}")
            self.error = re.sub(r"<Token [^>]*comments: \[[^\]]*\]>", "the end of the query", text)
            return
        if len(statements) != 1:
            self.error = "the draft must be one statement"
            return
        if not isinstance(statements[0], exp.Query):
            self.error = f"the draft must be a SELECT query, not {type(statements[0]).__name__.upper()}"
            return
        self.tree = statements[0]
        ctes = {cte.alias.casefold() for cte in self.tree.find_all(exp.CTE)}
        #: spelling in the draft (lower case) -> project key, or None when the project does not define the table
        self.tables: dict[str, str | None] = {}
        for table in self.tree.find_all(exp.Table):
            if not table.name or (not table.args.get("db") and table.name.casefold() in ctes):
                continue
            spelled = ".".join(p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p is not None and p.name)
            try:
                key = pipeline.resolve(table)
            except Exception:  # noqa: BLE001
                key = None
            self.tables.setdefault(spelled.casefold(), key if key in session.tables() else None)

    def keys(self) -> set[str]:
        return {key for key in self.tables.values() if key}


def _grain(session: Session, key: str):
    profile = session.profiles().get(key)
    return profile.grain if profile is not None else None


# ------------------------------------------------------------------------- types


def _catalog(session: Session, draft: Draft):
    from .googlesql_types import Catalog, Column, parse_type

    pipeline = session.pipeline()
    catalog = Catalog()
    for key in sorted(draft.keys()):
        if key in pipeline.models:
            profile = session.profiles().get(key)
            outputs = list(pipeline.output_columns(key))
            trusted = profile is not None and outputs and not any((a.reason or "").split(":")[0] in _FAILED_META for a in profile.attributes)
            if trusted:
                catalog.add(key, [Column(name, None) for name in outputs])
        else:
            columns = pipeline.source_schema.get(key) or {}
            if columns:
                catalog.add(key, [Column(name, parse_type(str(kind)) if kind else None) for name, kind in columns.items()])
    return catalog


def _type_findings(session: Session, draft: Draft) -> tuple[list[dict], list[str], list[tuple[str, str]]]:
    """Errors the query certainly has, its output columns (``name TYPE``), and what could not be typed."""

    from .googlesql_types import infer

    typed = infer(draft.tree, _catalog(session, draft))
    findings = [{"check": "types", "what": clip(f.message, 160), "fix": TYPE_FIXES.get(f.code, "Correct the expression it names.")} for f in typed.findings]
    columns: list[str] = []
    gaps: list[tuple[str, str]] = []
    if typed.columns is None:
        gaps.append(("output column types", clip(typed.error or "the query could not be typed", 160)))
        return findings, columns, gaps
    unknown_types = 0
    for column in typed.columns:
        name = column.name or "(unnamed)"
        if column.type is None or not column.type.complete:
            unknown_types += 1
            columns.append(f"{name} {column.type.sql() if column.type is not None else '?'}")
        else:
            columns.append(f"{name} {column.type.sql()}")
    if unknown_types:
        gaps.append((f"{unknown_types} of {len(columns)} output column types", "they read columns whose types the project does not declare (models have no stored types); a dry run returns them"))
    return findings, [clip(c, 90) for c in columns], gaps


# -------------------------------------------------------------------------- ties


def _tie_findings(session: Session, draft: Draft) -> tuple[list[dict], list[tuple[str, str]], list[str]]:
    from kumo_sql_prover.smt_equivalence import TableConstraints
    from kumo_sql_prover.tie_determinism import analyze

    pipeline = session.pipeline()
    constraints: dict[str, TableConstraints] = {}
    schema: dict[str, list[str]] = {}
    facts: list[str] = []
    for spelled, key in draft.tables.items():
        if key is None:
            continue
        outputs = list(pipeline.output_columns(key)) if key in pipeline.models else list(pipeline.source_schema.get(key, {}))
        if outputs:
            schema[spelled] = [c.lower() for c in outputs]
        grain = _grain(session, key)
        if grain is not None and grain.known and grain.keys:
            keys = tuple(k.lower() for k in grain.keys)
            constraints[spelled] = TableConstraints(not_null=frozenset(keys), keys=(keys,))
            facts.append(f"{key.split('.')[-1]} has one row per ({', '.join(grain.keys)})")
    report = analyze(draft.tree, schema=schema or None, constraints=constraints or None)
    if report.unsupported:
        return [], [("whether ties change the result", clip(report.unsupported, 160))], []
    if not report.sites:
        facts = []
    findings = []
    for site in report.undetermined:
        where = f" in {site.scope}" if site.scope and site.scope != "query" else ""
        findings.append({
            "check": "ties",
            "what": clip(f"{site.function}{where} can return different rows on different runs: {site.reason}", 230),
            "fix": clip(site.fix or "Add a unique key to the ORDER BY so no two rows tie.", 200),
        })
    return findings, [], facts[:3]


# ----------------------------------------------------------------------- fan-out


def _equalities(condition: exp.Expression | None) -> list[exp.EQ]:
    if condition is None:
        return []
    found = []
    stack = [condition]
    while stack:
        node = stack.pop()
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            stack += [node.left, node.right]
        elif isinstance(node, exp.EQ):
            found.append(node)
    return found


def _joined_columns(join: exp.Join, alias: str) -> set[str]:
    """The columns of the joined table (``alias``) that the join condition pins to something: equal to a column or a constant."""

    folded = alias.casefold()
    found = {c.name.casefold() for c in join.args.get("using") or [] if getattr(c, "name", None)}
    for equal in _equalities(join.args.get("on")):
        sides = [equal.left, equal.right]
        for side, other in (sides, sides[::-1]):
            if not isinstance(side, exp.Column):
                continue
            qualified = (side.table or "").casefold()
            other_alias = (other.table or "").casefold() if isinstance(other, exp.Column) else None
            if qualified == folded or (not qualified and other_alias not in (None, "", folded)) or (not qualified and isinstance(other, exp.Literal)):
                found.add(side.name.casefold())
    return found


def _fan_out_findings(session: Session, draft: Draft) -> tuple[list[dict], list[tuple[str, str]]]:
    findings: list[dict] = []
    gaps: list[tuple[str, str]] = []
    derived_cache: dict[str, object] = {}
    ctes = {cte.alias.casefold(): cte for cte in draft.tree.find_all(exp.CTE)}
    for select in draft.tree.find_all(exp.Select):
        multiplied = [a for a in (select.args.get("expressions") or []) for a in a.find_all(*_AGGREGATES) if not a.args.get("distinct")]
        for join in select.args.get("joins") or []:
            target = join.this
            side = str(join.args.get("side") or "").upper()
            if isinstance(target, exp.Unnest) or isinstance(target, exp.Lateral):
                continue  # unnesting an array multiplies rows by design
            alias = target.alias_or_name if hasattr(target, "alias_or_name") else ""
            label = _label(target)
            if side in ("RIGHT", "FULL"):
                gaps.append((f"fan-out of the {side} JOIN to {label}", "only INNER and LEFT joins are checked"))
                continue
            grain, reason = _target_grain(session, draft, target, ctes, derived_cache)
            if grain is None:
                gaps.append((f"whether the JOIN to {label} multiplies rows", reason))
                continue
            keys = {k.casefold() for k in grain}
            has_condition = join.args.get("on") is not None or bool(join.args.get("using"))
            joined = _joined_columns(join, alias) if has_condition else set()
            if not has_condition or str(join.args.get("kind") or "").upper() == "CROSS":
                what = f"CROSS JOIN to {label} pairs every row with every row of it"
                fix = "Add a join condition on its grain" + (f" ({', '.join(grain)})" if grain else "") + ", or use a scalar subquery if it has one row."
                if not grain:
                    continue  # a single-row table: a cross join cannot multiply rows
            elif keys <= joined:
                continue
            else:
                missing = [k for k in grain if k.casefold() not in joined]
                matched = ", ".join(sorted(joined)) or "no equality"
                what = (f"JOIN to {label} on ({matched}) can match several rows: it has one row per ({', '.join(grain)}) "
                        f"and {', '.join(missing)} {'are' if len(missing) > 1 else 'is'} not matched")
                fix = (f"Join on all of ({', '.join(grain)}), or first reduce {label} to one row per ({matched}) "
                       "(GROUP BY, or QUALIFY ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1).")
            if multiplied:
                what += f"; the {type(multiplied[0]).__name__.upper()} in this query would then count rows more than once"
            findings.append({"check": "fan_out", "what": clip(what, 260), "fix": clip(fix, 220)})
    return findings, gaps


def _label(target: exp.Expression) -> str:
    if isinstance(target, exp.Table):
        return target.alias_or_name if target.args.get("alias") else ".".join(p.name for p in (target.args.get("db"), target.this) if p is not None and p.name)
    return target.alias_or_name or "a subquery"


def _target_grain(session: Session, draft: Draft, target: exp.Expression, ctes: dict, cache: dict):
    """``(grain keys, "")`` of a joined table, subquery or CTE, or ``(None, reason)`` when it is not known."""

    from .table_profile import profile_query

    pipeline = session.pipeline()
    if isinstance(target, exp.Table):
        name = target.name.casefold()
        if not target.args.get("db") and name in ctes:
            sql = _cte_query(draft.tree, name)
        else:
            spelled = ".".join(p.name for p in (target.args.get("catalog"), target.args.get("db"), target.this) if p is not None and p.name).casefold()
            key = draft.tables.get(spelled)
            if key is None:
                return None, "the table is not defined in the project, so its grain is unknown"
            if key not in pipeline.models:
                return None, explain("source_without_grain")
            grain = _grain(session, key)
            if grain is None or not grain.known:
                return None, explain(grain.reason if grain is not None else "grain_unknown")
            return list(grain.keys), ""
    elif isinstance(target, exp.Subquery) and isinstance(target.this, exp.Query):
        sql = target.this.sql(dialect="bigquery")
    else:
        return None, "this kind of join source is not analysed"
    if sql not in cache:
        cache[sql] = profile_query(pipeline, sql)
    profile = cache[sql]
    if not profile.grain.known:
        return None, explain(profile.grain.reason or "grain_unknown")
    return list(profile.grain.keys), ""


def _cte_query(tree: exp.Expression, name: str) -> str:
    wrapped = exp.select("*").from_(exp.to_identifier(name))
    key = "with_" if tree.args.get("with_") is not None else "with"
    wrapped.set(key, tree.args[key].copy())
    return wrapped.sql(dialect="bigquery")


# ---------------------------------------------------------------------- dry run


def bigquery_dry_run(sql: str, project: str):
    """The only network call of the server: BigQuery plans ``sql`` in ``project`` (free, reads no data)."""

    from . import dryrun, query_context

    location = query_context.execution_location()
    return dryrun.dry_run(sql, project, **({"location": location} if location else {}))


def _dry_run(sql: str, project: str) -> tuple[list[dict], list[tuple[str, str]], dict | None]:
    """Findings, gaps and a summary of the dry run; ``not checked`` when BigQuery could not be asked."""

    from . import scope_queries
    from .sql_validation import validate_readonly_query

    try:
        validate_readonly_query(sql)
    except ValueError as exc:
        return [], [("BigQuery dry run", f"not checked: {clip(exc, 120)}")], None
    try:
        result = bigquery_dry_run(sql, project)
    except Exception as exc:  # noqa: BLE001 - no credentials, no network, a proxy: the answer says so instead of failing
        return [], [("BigQuery dry run", f"not checked: {clip(str(exc) or type(exc).__name__, 160)}")], None
    if not result.ok:
        where = f" (at {result.error_location})" if result.error_location else ""
        finding = {"check": "dry_run", "what": clip(f"BigQuery rejects the query{where}: {result.error_message}", 260),
                   "fix": "Fix what BigQuery reports; check_query without dry_run shows type problems it can see offline."}
        return [finding], [], {"ok": False}
    cap = scope_queries.get_settings().max_bytes_billed
    summary = {"ok": True, "estimated_bytes": result.total_bytes_processed, "byte_cap": cap,
               "columns": [clip(f"{f.name} {f.type}", 90) for f in result.schema] if result.schema_observed else None}
    findings = []
    if result.total_bytes_processed is not None and result.total_bytes_processed > cap:
        findings.append({"check": "dry_run", "what": f"the query would process about {result.total_bytes_processed:,} bytes, over the {cap:,}-byte cap",
                         "fix": "Filter on the table's partition column, select fewer columns, or raise the cap in Settings → Scopes."})
    gaps = [] if result.total_bytes_processed is not None else [("estimated bytes", "BigQuery returned no estimate")]
    return findings, gaps, summary


# ------------------------------------------------------------------------- tool


@tool(
    "check_query",
    "Check a draft query",
    "Is this draft sound before I run it? One call: the output column types and any certain errors, places where ties make the "
    "result change from run to run, joins that can multiply rows (judged from the grain of the joined tables), and, only when "
    "dry_run=true and a project is given, BigQuery's free dry run (estimated bytes against the byte cap). Every finding names its "
    "fix; what could not be checked is listed with the reason. Without dry_run it never touches the network.",
    {
        "sql": {"type": "string", "description": "The draft SELECT query, written against the project's tables (a table name, dataset.table or a Dataform ${ref(\"table\")})."},
        "dry_run": {"type": "boolean", "description": "Also ask BigQuery to plan the query (free, reads no data). Needs project. Default false."},
        "project": {"type": "string", "description": "With dry_run: the BigQuery project billed for the dry run. Never guessed."},
    },
    required=("sql",),
    open_world=True,
)
def check_query(session: Session, args: dict) -> Answer:
    sql = args["sql"]
    if len(sql) > MAX_SQL:
        raise ToolError(f"the query is too long ({MAX_SQL:,} characters at most)")
    want_dry = bool(args.get("dry_run", False))
    project = (args.get("project") or "").strip()
    if want_dry and not project:
        raise ToolError("dry_run needs an explicit project", ["Pass project=<the BigQuery project to bill>; it is not guessed. Leave dry_run out to check offline."])
    if project and not want_dry:
        raise ToolError("project is only used with dry_run=true", ["Pass dry_run=true to ask BigQuery for a dry run, or leave project out."])
    offset = offset_wanted(args)

    answer = Answer("")
    sql, refs = expand_refs(session, sql)
    if "${" in sql:
        left = leftover_expressions(sql)
        answer.summary = f"Cannot check the draft: it contains Dataform ${{...}} expressions other than ref() ({', '.join(left)})."
        answer.fields["verdict"] = "cannot_check"
        answer.add_list("findings", [{"check": "syntax", "what": "the draft contains template expressions, which are not SQL", "fix": "Write the SQL they stand for; only ${ref(\"table\")} and ${ref(\"dataset\", \"table\")} are read."}], primary=True)
        answer.unknown("every check", "they need SQL with real table names")
        answer.next("Replace the expression with the SQL it compiles to and ask again.")
        return answer
    if refs:
        answer.fields["refs_read_as"] = refs

    draft = Draft(session, sql)
    if draft.tree is None:
        answer.summary = "Cannot check the draft: it does not parse as one SELECT."
        answer.fields["verdict"] = "cannot_check"
        answer.add_list("findings", [{"check": "syntax", "what": clip(draft.error, 230), "fix": "Fix the syntax it points at; BigQuery GoogleSQL, one SELECT statement."}], primary=True)
        answer.unknown("every check", "they need a query that parses")
        answer.next("Correct the SQL and ask again.")
        return answer

    findings: list[dict] = []
    gaps: list[tuple[str, str]] = []
    checked = []
    type_findings, columns, type_gaps = _type_findings(session, draft)
    findings += type_findings
    gaps += type_gaps
    checked.append("types")
    tie_findings, tie_gaps, facts = _tie_findings(session, draft)
    findings += tie_findings
    gaps += tie_gaps
    checked.append("ties")
    fan_findings, fan_gaps = _fan_out_findings(session, draft)
    findings += fan_findings
    gaps += fan_gaps
    checked.append("fan_out")

    dry = None
    if want_dry:
        dry_findings, dry_gaps, dry = _dry_run(sql, project)
        findings = dry_findings + findings
        gaps += dry_gaps
        if dry is not None:
            checked.append("dry_run")
            if dry.get("columns"):
                columns = dry["columns"]
                gaps = [g for g in gaps if not g[0].endswith("output column types")]
                answer.fields["column_types_from"] = "BigQuery dry run"

    outside = sorted(spelled for spelled, key in draft.tables.items() if key is None)
    if outside:
        gaps.append((f"{len(outside)} table{'s' if len(outside) != 1 else ''} not in the project: {', '.join(outside[:3])}{', ...' if len(outside) > 3 else ''}", "their columns, types and grain are unknown, so checks that need them were skipped"))
    from . import project_checks  # imported here so the tool list keeps the order of TOOL_MODULES

    gaps += project_checks.draft_notes(session, draft.keys())  # disabled, declared and built, declared-only: the repository cannot vouch for them
    if facts:
        answer.fields["ties_judged_assuming"] = "; ".join(facts)

    counts: dict[str, int] = {}
    for item in findings:
        counts[item["check"]] = counts.get(item["check"], 0) + 1
    answer.fields["verdict"] = "problems_found" if findings else "no_problems_found"
    answer.fields["checked"] = checked
    answer.counts = {"findings": len(findings), **counts, "output_columns": len(columns), "not_checked": len(gaps)}
    if dry and dry.get("ok"):
        answer.fields["dry_run"] = {"estimated_bytes": dry["estimated_bytes"], "byte_cap": dry["byte_cap"]}
    answer.add_list("findings", findings[offset:][:PAGE], total=len(findings), offset=offset, primary=True)
    answer.add_list("columns", columns, total=len(columns))
    for what, reason in gaps:
        answer.unknown(what, reason)
    if findings:
        answer.summary = f"{len(findings)} problem{'s' if len(findings) != 1 else ''} found ({', '.join(f'{n} {c}' for c, n in counts.items())}); each names its fix."
        answer.next(f"Apply the fix of the first finding ({findings[0]['check']}), then run check_query again.")
    else:
        answer.summary = f"No problems found by the checks that ran ({', '.join(checked)}); {len(gaps)} thing{'s' if len(gaps) != 1 else ''} not checked. This is not a guarantee the query is correct."
        if dry is not None and dry.get("ok"):
            answer.next("Run the query once the estimate is acceptable; compare_queries tells whether a rewrite of it is the same query.")
        else:
            answer.next("Add dry_run=true and project=... to have BigQuery plan it (free) and return the real column types." if not want_dry else "BigQuery was not reached (see unknown); fix that and ask again with dry_run=true.")
    return answer
