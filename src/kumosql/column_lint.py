"""Lint a draft query against what the project declares about its columns (called once from ``check_query``).

Three rules, all offline:

* ``zones``: a comparison, a difference or a ``BETWEEN`` of two columns whose declared time zones differ is a finding that names
  both zones. When one side's zone is not declared, the pair is listed under ``not checked`` (never passed silently).
* ``time_zone_default``: a function that silently uses UTC because it was given no zone: ``CURRENT_DATE()``,
  ``CURRENT_DATETIME()``, ``DATE(ts)``, ``DATETIME(ts)``, ``TIMESTAMP_TRUNC(ts, DAY|MONTH|YEAR|...)``. A warning with the fix
  (pass the zone). It does not need any declaration.
* ``scale``: a column declared on a 0-100 scale multiplied or divided by 100. Where no scale is declared for a column that is
  multiplied or divided by 100, that is listed under ``not checked``.

Operands are followed through CTEs and subqueries to the table column they are (a computed value is not followed), and the
declared facts come from :mod:`kumosql.column_meaning`, which carries them along lineage.
"""

from __future__ import annotations

import re

from sqlglot import exp

from .column_meaning import TEMPORAL, Origin, Resolver, ref_text, zone_name
from .mcp_answers import clip
from .mcp_session import Session

EXAMPLES = 3

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.Sub)
_DIFFS = tuple(getattr(exp, name) for name in ("TimestampDiff", "DatetimeDiff", "DateDiff", "TimeDiff") if hasattr(exp, name))
_COARSE_UNITS = ("DAY", "WEEK", "MONTH", "QUARTER", "YEAR")
_PERCENT = re.compile(r"^0\s*(?:-|–|to|\.\.\.?)\s*100(?:\s*%)?$|^(?:percent|percentage|pct|%)$", re.I)


def is_percent_scale(text: str) -> bool:
    return bool(_PERCENT.match(" ".join(text.split())))


def lint(session: Session, tree: exp.Expression) -> tuple[list[dict], list[tuple[str, str]]]:
    """``(findings, not checked)`` for the draft ``tree``; a failure of the lint itself is reported as not checked."""

    try:
        return _lint(session, tree)
    except Exception as exc:  # noqa: BLE001 - a lint must not take the other checks down
        return [], [("time zone and scale lint", f"not checked: it failed ({type(exc).__name__})")]


def _lint(session: Session, tree: exp.Expression) -> tuple[list[dict], list[tuple[str, str]]]:
    resolver = Resolver(session, tree)
    findings: dict[str, dict] = {}
    undeclared: list[tuple[str, str]] = []  # (pair text, reason)
    unscaled: list[str] = []
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        for pair in _pairs(node):
            _zone_pair(resolver, node, pair, findings, undeclared)
        _default_zone(resolver, node, findings)
        if isinstance(node, (exp.Mul, exp.Div)):
            _scale(resolver, node, findings, unscaled)
    gaps: list[tuple[str, str]] = []
    if undeclared:
        by_reason: dict[str, list[str]] = {}
        for text, reason in undeclared:
            by_reason.setdefault(reason, []).append(text)
        for reason, items in by_reason.items():
            items = list(dict.fromkeys(items))
            gaps.append((f"{len(items)} comparison{'s' if len(items) != 1 else ''} of time columns: {_examples(items)}", reason))
    if unscaled:
        items = list(dict.fromkeys(unscaled))
        gaps.append((
            f"{len(items)} multiplication{'s' if len(items) != 1 else ''} or division{'s' if len(items) != 1 else ''} by 100: {_examples(items)}",
            "no scale is declared for the column (scale in --column-attributes or the columns config), so whether it is already on a 0-100 scale is not checked"))
    return list(findings.values()), gaps


def _examples(items: list[str]) -> str:
    return ", ".join(clip(i, 60) for i in items[:EXAMPLES]) + (", ..." if len(items) > EXAMPLES else "")


# ------------------------------------------------------------------- zones


def _pairs(node: exp.Expression) -> list[tuple[exp.Expression, exp.Expression]]:
    if isinstance(node, _COMPARISONS + _DIFFS):
        left, right = node.this, node.args.get("expression")
        return [(left, right)] if left is not None and right is not None else []
    if isinstance(node, exp.Between):
        return [(node.this, node.args[side]) for side in ("low", "high") if node.args.get(side) is not None]
    return []


def _zone_pair(resolver: Resolver, node, pair, findings: dict, undeclared: list) -> None:
    left, right = pair
    a, b = resolver.operand(left), resolver.operand(right)
    if "expression" in (a.kind, b.kind):
        return  # a literal or computed value has no declared zone to compare
    za, zb = resolver.zone(a), resolver.zone(b)
    text = f"{_label(left)} vs {_label(right)}"
    if za and zb:
        if zone_name(za) != zone_name(zb):
            what = (f"{_label(left)} is in {za} ({_origin(a)}) but {_label(right)} is in {zb} ({_origin(b)}): "
                    "comparing or subtracting them mixes time zones")
            fix = (f"Convert one side so both are in the same zone before comparing, e.g. DATETIME({_label(left)}, '{zb}') or "
                   f"TIMESTAMP({_label(right)}, '{za}'), or declare the right zone if a declaration is wrong.")
            findings.setdefault(what, {"check": "zones", "what": clip(what, 260), "fix": clip(fix, 240)})
        return
    if not za and not zb:
        if _timelike(resolver, a) or _timelike(resolver, b):
            undeclared.append((text, "no time zone is declared for either side, so a zone mismatch is not checked"))
        return
    known, other, which = (a, b, _label(right)) if za else (b, a, _label(left))
    if other.kind == "unknown":
        undeclared.append((text, f"{which} could not be traced to a table column ({other.reason}), so its zone is not known"))
    else:
        undeclared.append((text, f"{which} has no declared time zone while the other side has one, so a zone mismatch is not checked"))


def _timelike(resolver: Resolver, found: Origin) -> bool:
    return found.kind == "zone" or resolver.kind(found) in TEMPORAL


def _label(node: exp.Expression) -> str:
    return clip(node.sql(dialect="bigquery"), 40)


def _origin(found: Origin) -> str:
    return "set by a conversion in the draft" if found.kind == "zone" else ref_text(found.table, found.column)


# ----------------------------------------------------- functions with a default zone


def _default_zone(resolver: Resolver, node: exp.Expression, findings: dict) -> None:
    name = fix = ""
    if isinstance(node, (exp.CurrentDate, exp.CurrentDatetime, exp.CurrentTime)) and node.this is None:
        name = {exp.CurrentDate: "CURRENT_DATE", exp.CurrentDatetime: "CURRENT_DATETIME", exp.CurrentTime: "CURRENT_TIME"}[type(node)] + "()"
        what = f"{name} with no zone argument uses UTC: 'today' and any day or month boundary computed from it roll over at UTC midnight"
        fix = f"Pass the zone explicitly, e.g. {name[:-2]}('America/New_York') (the zone your reporting uses)."
    elif isinstance(node, exp.Date) and node.args.get("zone") is None and node.args.get("expression") is None and node.args.get("day") is None:
        argument = node.this
        if argument is not None and _may_be_timestamp(resolver, argument):
            what = f"{_label(node)} with no zone argument takes the date in UTC{_declared(resolver, argument)}"
            fix = f"Pass the zone explicitly: DATE({_label(argument)}, 'America/New_York') (the zone your reporting uses)."
    elif isinstance(node, exp.Datetime) and node.args.get("expression") is None:
        argument = node.this
        if argument is not None and _may_be_timestamp(resolver, argument):
            what = f"{_label(node)} with no zone argument converts in UTC{_declared(resolver, argument)}"
            fix = f"Pass the zone explicitly: DATETIME({_label(argument)}, 'America/New_York') (the zone your reporting uses)."
    elif isinstance(node, exp.TimestampTrunc) and node.args.get("zone") is None:
        unit = node.args.get("unit")
        unit_text = unit.sql(dialect="bigquery").upper() if unit is not None else ""
        argument = node.this
        if any(word in unit_text for word in _COARSE_UNITS) and argument is not None and _may_be_timestamp(resolver, argument):
            what = f"{_label(node)} with no zone argument cuts the boundary in UTC{_declared(resolver, argument)}"
            fix = f"Pass the zone as the third argument: TIMESTAMP_TRUNC({_label(argument)}, {unit.sql(dialect='bigquery')}, 'America/New_York')."
    if fix:
        findings.setdefault(what, {"check": "time_zone_default", "level": "warning", "what": clip(what, 240), "fix": clip(fix, 200)})


def _may_be_timestamp(resolver: Resolver, argument: exp.Expression) -> bool:
    """False when the argument is certainly not a TIMESTAMP (a string or date literal, a cast to a date type, a typed column)."""

    if isinstance(argument, exp.Literal):
        return False
    if isinstance(argument, (exp.Cast, exp.TryCast)):
        target = argument.args.get("to")
        return target is not None and target.this == exp.DataType.Type.TIMESTAMP
    found = resolver.operand(argument)
    kind = resolver.kind(found)
    return kind in ("", "TIMESTAMP")


def _declared(resolver: Resolver, argument: exp.Expression) -> str:
    found = resolver.operand(argument)
    zone = resolver.zone(found)
    return f"; {_label(argument)} is declared {zone}, so this is right only if UTC dates are wanted" if zone else ""


# --------------------------------------------------------------------- scale


def _is_hundred(node: exp.Expression) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.Literal) or node.is_string:
        return False
    try:
        return float(node.name) == 100.0
    except ValueError:
        return False


def _scale(resolver: Resolver, node: exp.Expression, findings: dict, unscaled: list) -> None:
    left, right = node.this, node.args.get("expression")
    if left is None or right is None:
        return
    if _is_hundred(right):
        other = left
    elif _is_hundred(left) and isinstance(node, exp.Mul):
        other = right
    else:
        return
    found = resolver.operand(other)
    if found.kind != "column":
        return
    from .column_meaning import facts

    scale = facts(resolver.session, found.table, found.column).values.get("scale", "")
    column = f"{_label(other)} ({ref_text(found.table, found.column)})"
    if not scale:
        unscaled.append(_label(node))
        return
    if not is_percent_scale(scale):
        return
    if isinstance(node, exp.Mul):
        what = f"{column} is declared on a {scale} scale; multiplying it by 100 scales it a second time (the result runs 0-10000)"
        fix = f"Remove the * 100: {_label(other)} is already a percentage. Multiply only if the declaration is wrong."
    else:
        what = f"{column} is declared on a {scale} scale; dividing it by 100 turns it into a 0-1 fraction"
        fix = f"If a 0-1 fraction is wanted, keep the division and label the result; otherwise use {_label(other)} as it is."
    findings.setdefault(what, {"check": "scale", "level": "warning", "what": clip(what, 240), "fix": clip(fix, 200)})
