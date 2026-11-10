"""BigQuery (GoogleSQL) to DuckDB translation for executing BigQuery SQL in DuckDB.

sqlglot's BigQuery reader and DuckDB writer do most of the work. Where their translation computes a
different value from BigQuery, :func:`to_duckdb` fixes the BigQuery tree first, or refuses with
:class:`UntranslatableError` when it cannot (unknown beats wrong). Each fix was found by the BigQuery
Utils UDF eval (``docs/evals/bigquery-behavior-eval.md``), whose expected values were computed on BigQuery:

* Array subscripts. ``a[OFFSET(i)]`` with a non-literal ``i`` of unknown type was not shifted to DuckDB's
  1-based subscripts; every ``OFFSET``/``ORDINAL`` subscript is now shifted here. A plain ``a[i]`` with a
  non-literal index is refused unless ``a`` is known to be an array (it may be a JSON subscript).
* ``UNNEST(...) WITH OFFSET`` became ``WITH ORDINALITY``, which counts from 1; the offset is now
  ordinality minus one.
* Operator precedence. ``DIV(a + 1, b * 2)`` printed as ``a + 1 // b * 2`` and ``a & b << 2`` (BigQuery:
  ``a & (b << 2)``) as itself, which DuckDB reads left to right; operands that are operators are now
  parenthesized.
* ``FORMAT``: DuckDB's ``format`` is fmt-style (``{}``), BigQuery's is printf-style; it becomes
  ``printf`` when every specifier is one printf shares, and is refused otherwise (``%t``, ``%T``,
  ``%'d``, ``*`` widths, or a format that is not a literal).
* ``EXTRACT(DAYOFWEEK ...)`` is 1 (Sunday) to 7 in BigQuery and 0 to 6 in DuckDB. ``EXTRACT(WEEK ...)``
  (Sunday-based weeks in BigQuery, ISO weeks in DuckDB) is refused.
* ``REGEXP_EXTRACT`` returns NULL when nothing matches (DuckDB: an empty string); a pattern that is not
  a literal is refused, since whether it has a capture group decides what is returned.
* ``SPLIT(s, NULL)`` is NULL (DuckDB returned ``[s]``).
* Casting a STRUCT to a STRUCT type is positional in BigQuery and by field name in DuckDB (a field whose
  name does not match silently becomes NULL); struct casts are now rebuilt field by field.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from kumo_sql_prover import bigquery_syntax as _bigquery_syntax  # noqa: F401


class UntranslatableError(ValueError):
    """The query uses BigQuery behaviour the DuckDB translation would not reproduce."""


def to_duckdb(sql: str, *, fix: bool = True) -> str:
    """One BigQuery statement as DuckDB SQL (raises :class:`UntranslatableError` or a sqlglot error).

    ``fix=False`` is sqlglot's translation alone, the baseline the fixes are measured against.
    """

    tree = sqlglot.parse_one(sql, read="bigquery")
    if tree is None or isinstance(tree, exp.Command):
        raise sqlglot.errors.ParseError("not a statement sqlglot reads")
    return (fix_bigquery_tree(tree) if fix else tree).sql(dialect="duckdb")


def fix_bigquery_tree(tree: exp.Expression) -> exp.Expression:
    """``tree`` (parsed as BigQuery) rewritten so that sqlglot's DuckDB output keeps BigQuery's values."""

    tree = tree.copy()
    try:
        from sqlglot.optimizer.annotate_types import annotate_types

        annotate_types(tree, dialect="bigquery")
    except Exception:  # noqa: BLE001  (types stay unknown)
        pass
    # Reading first (operator precedence), then translation; children before parents, so a parent sees its
    # fixed children.
    for node in reversed(list(tree.walk(bfs=False))):
        new = _bitwise_precedence(node)
        if new is not None:
            node.replace(new)
    for node in reversed(list(tree.walk(bfs=False))):
        for fix in _FIXES:
            new = fix(node)
            if new is not None and new is not node:
                node.replace(new)
                node = new
                break
    return tree


# --- the fixes (each returns a replacement node, or None) --------------------------------------

_OPERATORS = (
    exp.Add, exp.Sub, exp.Mul, exp.Div, exp.IntDiv, exp.Mod, exp.DPipe,
    exp.BitwiseAnd, exp.BitwiseOr, exp.BitwiseXor, exp.BitwiseLeftShift, exp.BitwiseRightShift,
)


def _parenthesize_operands(node):
    if isinstance(node, _OPERATORS):
        for key in ("this", "expression"):
            child = node.args.get(key)
            if isinstance(child, (exp.Binary, exp.Not, exp.Neg)) and not isinstance(child, exp.Paren):
                node.set(key, exp.Paren(this=child))
    return None


_BITWISE_PRECEDENCE = {
    exp.BitwiseLeftShift: 4, exp.BitwiseRightShift: 4, exp.BitwiseAnd: 3, exp.BitwiseXor: 2, exp.BitwiseOr: 1,
}


def _bitwise_precedence(node):
    """sqlglot reads ``<<``, ``>>``, ``&``, ``^`` and ``|`` as one left-to-right level; in GoogleSQL shifts bind
    tightest, then ``&``, then ``^``, then ``|``. The top of each unparenthesized chain is rebuilt."""

    kind = type(node)
    if kind not in _BITWISE_PRECEDENCE:
        return None
    if type(node.parent) in _BITWISE_PRECEDENCE and node.arg_key == "this":
        return None  # not the top of the chain
    operands, operators = [node.expression], [kind]
    left = node.this
    while type(left) in _BITWISE_PRECEDENCE:
        operands.append(left.expression)
        operators.append(type(left))
        left = left.this
    if len(operators) == 1:
        return None
    operands.append(left)
    operands.reverse()
    operators.reverse()
    if all(_BITWISE_PRECEDENCE[a] >= _BITWISE_PRECEDENCE[b] for a, b in zip(operators, operators[1:])):
        return None  # left to right is already the GoogleSQL reading
    values, ops = [operands[0].copy()], []

    def reduce():
        right, left_ = values.pop(), values.pop()
        values.append(ops.pop()(this=left_, expression=right))

    for op, operand in zip(operators, operands[1:]):
        while ops and _BITWISE_PRECEDENCE[ops[-1]] >= _BITWISE_PRECEDENCE[op]:
            reduce()
        ops.append(op)
        values.append(operand.copy())
    while ops:
        reduce()
    return values[0]


def _string_value(node):
    while isinstance(node, exp.Paren) or (
            isinstance(node, exp.Cast) and not isinstance(node, exp.TryCast) and node.to.is_type(exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT)):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    if isinstance(node, exp.RawString):
        return node.this
    if isinstance(node, (exp.DPipe, exp.Concat)):  # 'a' || 'b', CONCAT('a', 'b')
        parts = [_string_value(p) for p in ([node.this, node.expression] if isinstance(node, exp.DPipe) else node.expressions)]
        return None if any(p is None for p in parts) else "".join(parts)
    return None


def _is_int_literal(node) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and re.fullmatch(r"-?\d+", node.this or "") is not None


def _subscript(node):
    if not isinstance(node, exp.Bracket) or len(node.expressions) != 1:
        return None
    index = node.expressions[0]
    offset = node.args.get("offset")
    if offset is None:
        if _is_int_literal(index) or (isinstance(index, exp.Literal) and index.is_string):
            return None  # sqlglot shifts integer literals itself; a string is a JSON field
        this_type = node.this.type
        if this_type is None or not this_type.is_type(exp.DataType.Type.ARRAY):
            raise UntranslatableError("a[i] with a non-literal index on a value not known to be an array")
        offset = 0
    shift = 1 - int(offset)
    if shift:
        if _is_int_literal(index):
            new_index = exp.Literal.number(int(index.this) + shift)
        else:
            new_index = exp.Paren(this=exp.Add(this=exp.Paren(this=index.copy()), expression=exp.Literal.number(shift)))
        node.set("expressions", [new_index])
    node.set("offset", 1)  # DuckDB subscripts are 1-based: nothing left for the generator to shift
    return None


def _unnest_offset(node):
    """``FROM UNNEST(a) AS x WITH OFFSET AS o``: DuckDB's ordinality counts from 1, BigQuery's offset from 0."""

    if not isinstance(node, exp.Unnest) or not isinstance(node.args.get("offset"), exp.Identifier):
        return None
    if not isinstance(node.parent, (exp.From, exp.Join)):
        raise UntranslatableError("UNNEST ... WITH OFFSET outside FROM")
    alias = node.args.get("alias")
    columns = (alias.args.get("columns") or []) if alias is not None else []
    if len(columns) > 1 or (alias is not None and alias.name):
        raise UntranslatableError("UNNEST ... WITH OFFSET with a table alias")
    inner = node.copy()
    if columns:
        element = exp.to_identifier(columns[0].name, quoted=columns[0].args.get("quoted"))
    else:
        # An element without a name can only be reached through SELECT *, which would now show the name.
        outer = node.parent.parent
        if not isinstance(outer, exp.Select) or any(isinstance(s, exp.Star) for s in outer.selects):
            raise UntranslatableError("UNNEST ... WITH OFFSET without an element alias under SELECT *")
        element = exp.to_identifier("__kumo_element")
        inner.set("alias", exp.TableAlias(columns=[element.copy()]))
    off = node.args["offset"].name
    select = exp.select(
        exp.Column(this=element),
        exp.alias_(exp.Sub(this=exp.column(off), expression=exp.Literal.number(1)), off),
    ).from_(inner)
    return exp.Subquery(this=select)


_PRINTF_SPEC = re.compile(r"%(?:%|[-+ #0]*\d*(?:\.\d+)?([dioxXfFeEgGs]))")


def _format(node):
    if not isinstance(node, exp.Format):
        return None
    fmt = node.this
    text = _string_value(fmt)
    bare = fmt
    while isinstance(bare, (exp.Paren, exp.Cast)):
        bare = bare.this
    if text is None and isinstance(bare, exp.Null):
        return exp.Cast(this=exp.Null(), to=exp.DataType.build("VARCHAR"))  # FORMAT(NULL, ...) is NULL
    if text is None:
        raise UntranslatableError("FORMAT with a format that is not a literal")
    rest = _PRINTF_SPEC.sub("", text)
    if "%" in rest:
        if _in_error_message(node):
            return exp.Literal.string(text)  # only the text of an error message changes
        raise UntranslatableError(f"FORMAT specifier printf does not share: {text!r}")
    return exp.Anonymous(this="PRINTF", expressions=[fmt.copy(), *[e.copy() for e in node.expressions]])


def _in_error_message(node) -> bool:
    while node is not None:
        if isinstance(node, exp.Anonymous) and node.name.upper() == "ERROR":
            return True
        node = node.parent
    return False


def _extract(node):
    if not isinstance(node, exp.Extract):
        return None
    part = node.this.sql(dialect="bigquery").upper()
    if part == "DAYOFWEEK":
        return exp.Paren(this=exp.Add(this=node.copy(), expression=exp.Literal.number(1)))
    if part.startswith("WEEK"):
        # BigQuery weeks start on the given day (Sunday by default) and days before the first one are week 0, as
        # strftime's %U (Sunday) and %W (Monday); DuckDB's own WEEK is the ISO week.
        directive = {"WEEK": "%U", "WEEK(SUNDAY)": "%U", "WEEK(MONDAY)": "%W"}.get(part.replace(" ", ""))
        if directive is None:
            raise UntranslatableError(f"EXTRACT({part} ...) with a week start other than Sunday or Monday")
        week = exp.Anonymous(this="STRFTIME", expressions=[node.expression.copy(), exp.Literal.string(directive)])
        return exp.Cast(this=week, to=exp.DataType.build("BIGINT"))
    return None


def _regexp_extract(node):
    if not isinstance(node, (exp.RegexpExtract, exp.RegexpExtractAll)):
        return None
    text = _string_value(node.expression)
    if text is None:
        raise UntranslatableError("REGEXP_EXTRACT with a pattern that is not a constant")
    if node.args.get("position") is not None or node.args.get("occurrence") is not None:
        raise UntranslatableError("REGEXP_EXTRACT with a position or an occurrence")
    # BigQuery returns the capture group when the pattern has one, else the whole match (two or more: an error).
    groups = capture_groups(text)
    if groups > 1:
        raise UntranslatableError("REGEXP_EXTRACT with more than one capture group (an error in BigQuery)")
    pattern = exp.Literal.string(text)
    node.set("expression", pattern)
    node.set("group", exp.Literal.number(1) if groups else None)
    if isinstance(node, exp.RegexpExtractAll):
        return None
    matches = exp.RegexpLike(this=node.this.copy(), expression=pattern.copy())
    return exp.Case(ifs=[exp.If(this=matches, true=node.copy())])


def capture_groups(pattern: str) -> int:
    """The number of capturing groups in an RE2 pattern (escapes and character classes skipped)."""

    count, i, n = 0, 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
        elif c == "(":
            if not pattern.startswith("(?", i) or pattern.startswith(("(?P<", "(?<"), i) and not pattern.startswith(("(?<=", "(?<!"), i):
                count += 1
        i += 1
    return count


def _split(node):
    if not isinstance(node, exp.Split):
        return None
    delimiter = node.expression
    if delimiter is None or _string_value(delimiter) is not None:
        return None
    is_null = exp.Is(this=delimiter.copy(), expression=exp.Null())
    return exp.Case(ifs=[exp.If(this=is_null, true=exp.Null())], default=node.copy())


def _struct_fields(data_type: exp.DataType):
    return [(f.name, f.args.get("kind")) for f in data_type.expressions if isinstance(f, exp.ColumnDef)]


def _positional(value: exp.Expression, target: exp.DataType, depth: int = 0) -> exp.Expression:
    """``value`` cast to ``target`` the BigQuery way: STRUCT fields by position."""

    if target.is_type(exp.DataType.Type.STRUCT):
        fields = _struct_fields(target)
        if not fields or len(fields) != len(target.expressions):
            raise UntranslatableError("STRUCT type without named fields")
        if isinstance(value, (exp.Struct, exp.Tuple)):
            items = [e.expression if isinstance(e, exp.PropertyEQ) else e.this if isinstance(e, exp.Alias) else e
                     for e in value.expressions]
            if len(items) != len(fields):
                raise UntranslatableError("STRUCT cast with a different number of fields")
            return _struct([(n, _positional(v.copy(), t, depth)) for v, (n, t) in zip(items, fields)])
        if isinstance(value, exp.Null):
            return exp.Cast(this=value, to=target.copy())
        field_values = [
            (n, _positional(exp.Anonymous(this="STRUCT_EXTRACT_AT", expressions=[value.copy(), exp.Literal.number(i)]), t, depth))
            for i, (n, t) in enumerate(fields, start=1)
        ]
        return exp.Case(
            ifs=[exp.If(this=exp.Is(this=value.copy(), expression=exp.Null()), true=exp.Cast(this=exp.Null(), to=target.copy()))],
            default=_struct(field_values),
        )
    if target.is_type(exp.DataType.Type.ARRAY) and target.expressions and _contains_struct(target.expressions[0]):
        element = target.expressions[0]
        if isinstance(value, exp.Array):
            # The fields now carry the target's names, so DuckDB's by-name cast only pins the type (of [] too).
            return exp.Cast(this=exp.Array(expressions=[_positional(e.copy(), element, depth) for e in value.expressions]), to=target.copy())
        if isinstance(value, exp.Null):
            return exp.Cast(this=value, to=target.copy())
        name = f"__kumo_e{depth}"
        each = exp.Lambda(this=_positional(exp.column(name), element, depth + 1), expressions=[exp.to_identifier(name)])
        return exp.Anonymous(this="LIST_TRANSFORM", expressions=[value.copy(), each])
    return exp.Cast(this=value, to=target.copy())


def _struct(fields) -> exp.Struct:
    return exp.Struct(expressions=[exp.PropertyEQ(this=exp.to_identifier(n), expression=v) for n, v in fields])


def _contains_struct(data_type) -> bool:
    return isinstance(data_type, exp.DataType) and any(
        isinstance(t, exp.DataType) and t.is_type(exp.DataType.Type.STRUCT) for t in data_type.find_all(exp.DataType)
    )


def _struct_cast(node):
    if not isinstance(node, exp.Cast) or not _contains_struct(node.to):
        return None
    if isinstance(node, exp.TryCast) or node.args.get("safe"):
        raise UntranslatableError("SAFE_CAST to a STRUCT type")
    source_type = node.this.type
    if source_type is not None and source_type == node.to:
        return None  # already that type
    return _positional(node.this, node.to)


_FIXES = (_parenthesize_operands, _subscript, _unnest_offset, _format, _extract, _regexp_extract, _split, _struct_cast)
