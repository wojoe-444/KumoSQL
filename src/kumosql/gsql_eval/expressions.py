"""Operators, literals, names, conditionals and subqueries."""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlglot import exp

from . import types as T
from . import values as V
from .compiler import Compiler, Cx, E, EmptyScope, _only, handles, is_query, path_parts
from .errors import AnalysisError, EvalError, Unsupported
from .runtime import NULL, Env, const, is_constant

# --- literals ------------------------------------------------------------------------------------

_INTEGER = re.compile(r"^[0-9]+$")


@handles(exp.Literal)
def literal(c: Compiler, node: exp.Literal, cx: Cx) -> E:
    _only(node, "this", "is_string")
    text = node.this
    if node.is_string:
        if "\\" in text and not c.literals_decoded:
            raise Unsupported("string literal with a backslash (sqlglot keeps some escapes undecoded)")
        return const(T.STRING, text)
    if _INTEGER.match(text):
        value = int(text)
        if value > V.INT64_MAX:
            raise Unsupported(f"integer literal {text} beyond INT64")
        return const(T.INT64, value)
    try:
        value = float(text)
    except ValueError:
        raise Unsupported(f"number literal {text}") from None
    if math.isinf(value):
        raise AnalysisError(f"Invalid floating point literal: {text}")
    result = const(T.FLOAT64, value)
    try:
        result.exact = Decimal(text)
    except InvalidOperation:
        pass
    return result


@handles(exp.Boolean)
def boolean(c, node, cx):
    return const(T.BOOL, bool(node.this))


@handles(exp.Null)
def null(c, node, cx):
    return NULL


@handles(getattr(exp, "RawString", None))
def raw_string(c, node, cx):
    return const(T.STRING, node.this)


@handles(getattr(exp, "ByteString", None))
def byte_string(c, node, cx):
    from kumo_sql_prover.string_literals import _decode_bytes

    text = node.this
    if node.args.get("is_bytes") is False:
        raise Unsupported("byte string flag")
    if "\\" in text and not c.literals_decoded:
        raise Unsupported("bytes literal with a backslash (sqlglot keeps some escapes undecoded)")
    value = _decode_bytes(text, raw=False)
    if value is None:
        raise Unsupported("bytes literal escape")
    return const(T.BYTES, value)


@handles(getattr(exp, "HexString", None))
def hex_string(c, node, cx):
    _only(node, "this", "is_integer")
    value = int(node.this, 16)
    if value > V.INT64_MAX:
        raise AnalysisError(f"Invalid hex integer literal: 0x{node.this}")
    return const(T.INT64, value)


@handles(exp.Parameter, getattr(exp, "Placeholder", None))
def parameter(c: Compiler, node, cx):
    inner = node.this
    if isinstance(inner, exp.Parameter):
        raise Unsupported("system variable")
    name = inner.name if isinstance(inner, exp.Expression) else str(inner or "")
    if not name:
        raise Unsupported("positional parameter")
    entry = c.params.get(name.lower())
    if entry is None:
        raise AnalysisError(f"Query parameter '{name}' not found")
    typ, value = entry
    return const(typ, value)


# --- names -----------------------------------------------------------------------------------------


@handles(exp.Column)
def column(c: Compiler, node: exp.Column, cx: Cx) -> E:
    parts = path_parts(node)
    if parts is None:
        if isinstance(node.this, exp.Star):
            raise AnalysisError("* is not allowed here")
        raise Unsupported("column reference")
    value = c.resolve(parts, cx.scope)
    if value is None:
        raise AnalysisError(f"Unrecognized name: {parts[0]}")
    return value


@handles(exp.Dot)
def dot(c: Compiler, node: exp.Dot, cx: Cx) -> E:
    parts = path_parts(node)
    if parts is not None:
        value = c.resolve(parts, cx.scope)
        if value is None:
            raise AnalysisError(f"Unrecognized name: {parts[0]}")
        return value
    if not isinstance(node.expression, exp.Identifier):
        raise Unsupported("dot access")
    return c.field_access(c.expr(node.this, cx), node.expression.name)


@handles(exp.Paren)
def paren(c, node, cx):
    return c.expr(node.this, cx)


@handles(exp.Alias)
def alias(c, node, cx):
    raise AnalysisError("An alias is not allowed here")


# --- arithmetic ------------------------------------------------------------------------------------


def _overflow_checked(result: float, a: float, b: float) -> float:
    if math.isinf(result) and not (math.isinf(a) or math.isinf(b)):
        raise EvalError("double overflow")
    return result


def _fdiv(a: float, b: float) -> float:
    if b == 0:
        raise EvalError(f"division by zero: {V.format_double(a)} / {V.format_double(b)}")
    try:
        result = a / b
    except OverflowError:
        raise EvalError("double overflow") from None
    return _overflow_checked(result, a, b)


def _fmul(a: float, b: float) -> float:
    return _overflow_checked(a * b, a, b)


def _numeric_ops(kind: str) -> dict:
    if kind == "INT64":
        return {
            "+": lambda a, b: V.int64(a + b),
            "-": lambda a, b: V.int64(a - b),
            "*": lambda a, b: V.int64(a * b),
        }
    if kind in ("NUMERIC", "BIGNUMERIC"):
        q = V.decimal_of(kind)

        def div(a, b):
            if b == 0:
                raise EvalError(f"division by zero: {V.format_decimal(a)} / {V.format_decimal(b)}")
            return q(V.DEC.divide(a, b))

        return {
            "+": lambda a, b: q(V.DEC.add(a, b)),
            "-": lambda a, b: q(V.DEC.subtract(a, b)),
            "*": lambda a, b: q(V.DEC.multiply(a, b)),
            "/": div,
        }
    if kind == "FLOAT64":
        return {
            "+": lambda a, b: _overflow_checked(a + b, a, b),
            "-": lambda a, b: _overflow_checked(a - b, a, b),
            "*": _fmul,
            "/": _fdiv,
        }
    raise AnalysisError(f"No arithmetic on {kind}")


def strict2(fn, a: E, b: E):
    fa, fb = a.fn, b.fn

    def run(env):
        x = fa(env)
        if x is None:
            return None
        y = fb(env)
        if y is None:
            return None
        return fn(x, y)

    return run


def numeric_binary(c: Compiler, op: str, a: E, b: E, what: str) -> E:
    if op == "/" and a.type.kind == "INT64" and b.type.kind == "INT64":
        target = T.FLOAT64
    else:
        target = T.supertype([(a.type, a.lit), (b.type, b.lit)])
        if op == "/" and target == T.INT64:
            target = T.FLOAT64
    if not target.is_numeric:
        raise AnalysisError(f"No matching signature for operator {op} for argument types: {a.type}, {b.type}")
    a, b = c.coerce(a, target), c.coerce(b, target)
    return E(target, strict2(_numeric_ops(target.kind)[op], a, b))


def _temporal_plus(value_type: T.Type, interval_first: bool, a: E, b: E, sign: int) -> E:
    from . import datetimes as D

    if value_type.kind == "TIMESTAMP":
        result_type = T.TIMESTAMP

        def step(v, i, tz):
            return D.timestamp_add_interval(v, i if sign > 0 else -i, tz)
    elif value_type.kind in ("DATE", "DATETIME"):
        result_type = T.DATETIME

        def step(v, i, tz):
            return D.datetime_add_interval(v, i if sign > 0 else -i)
    else:
        raise AnalysisError(f"No matching signature for INTERVAL arithmetic with {value_type}")
    fa, fb = a.fn, b.fn

    def run(env):
        x = fa(env)
        if x is None:
            return None
        y = fb(env)
        if y is None:
            return None
        return step(y, x, env.ctx.tz) if interval_first else step(x, y, env.ctx.tz)

    return E(result_type, run)


def _plus_minus(c: Compiler, node, cx: Cx, op: str) -> E:
    _only(node, "this", "expression")
    a = c.expr(node.this, cx)
    b = c.expr(node.expression, cx)
    ka, kb = a.type.kind, b.type.kind
    if (a.lit == "null" and b.lit == "null") or ((a.lit == "null" or b.lit == "null") and c.mode == "googlesql"):
        raise AnalysisError(f"Operands of {op} cannot be literal NULL")
    if ka == "INTERVAL" and kb == "INTERVAL" and a.lit != "null" and b.lit != "null":
        sign = 1 if op == "+" else -1
        return E(T.INTERVAL, strict2(lambda x, y: V.Interval(x.months + sign * y.months, x.days + sign * y.days, x.micros + sign * y.micros).check(), a, b))
    if kb == "INTERVAL" and b.lit != "null" and ka in ("DATE", "DATETIME", "TIMESTAMP"):
        return _temporal_plus(a.type, False, a, b, 1 if op == "+" else -1)
    if op == "+" and ka == "INTERVAL" and a.lit != "null" and kb in ("DATE", "DATETIME", "TIMESTAMP"):
        return _temporal_plus(b.type, True, a, b, 1)
    if op == "-" and ka == kb and ka in ("DATE", "DATETIME", "TIMESTAMP", "TIME") and a.lit != "null":
        from . import datetimes as D

        return E(T.INTERVAL, strict2(D.difference_interval(ka), a, b))
    if (a.type.is_numeric or a.lit == "null") and (b.type.is_numeric or b.lit == "null"):
        if a.lit == "null":
            a = c.coerce(a, b.type)
        if b.lit == "null":
            b = c.coerce(b, a.type)
        return numeric_binary(c, op, a, b, op)
    if ka in ("DATE",) and kb == "INT64" or ka == "INT64" and kb == "DATE":
        raise Unsupported("DATE +/- INT64")
    raise AnalysisError(f"No matching signature for operator {op} for argument types: {a.type}, {b.type}")


@handles(exp.Add)
def add(c, node, cx):
    return _plus_minus(c, node, cx, "+")


@handles(exp.Sub)
def sub(c, node, cx):
    return _plus_minus(c, node, cx, "-")


def _interval_times_double(value: V.Interval, factor: float) -> V.Interval:
    """INTERVAL * FLOAT64: a fraction of a month is 30 days, a fraction of a day 24 hours; the microseconds round half away."""

    if math.isnan(factor) or math.isinf(factor):
        raise EvalError("Interval multiplication by a non-finite number")
    mul, add, sub = V.DEC.multiply, V.DEC.add, V.DEC.subtract
    f = Decimal(factor)
    months = mul(value.months, f)
    whole_months = int(months)  # truncates toward zero
    days = add(mul(value.days, f), mul(sub(months, whole_months), 30))
    whole_days = int(days)
    micros = add(mul(value.micros, f), mul(sub(days, whole_days), V.MICROS_PER_DAY))
    rounded = micros.to_integral_value(rounding=ROUND_HALF_UP, context=V.DEC)
    if abs(sub(micros, rounded)) == Decimal("0.5"):
        raise Unsupported("INTERVAL * FLOAT64 landing exactly between two microseconds")
    return V.Interval(V.int64(whole_months), V.int64(whole_days), V.int64(int(rounded))).check()


@handles(exp.Mul)
def mul(c: Compiler, node, cx):
    _only(node, "this", "expression")
    a, b = c.expr(node.this, cx), c.expr(node.expression, cx)
    if (a.lit == "null" and b.lit == "null") or ((a.lit == "null" or b.lit == "null") and c.mode == "googlesql"):
        raise AnalysisError("Operands of * cannot be literal NULL")
    if a.type.kind == "INTERVAL" and b.type.kind == "INT64" or a.type.kind == "INT64" and b.type.kind == "INTERVAL":
        interval, count = (a, b) if a.type.kind == "INTERVAL" else (b, a)
        if interval.lit == "null" or count.lit == "null":
            pass
        else:
            def scale(i, n):
                return V.Interval(V.int64(i.months * n), V.int64(i.days * n), V.int64(i.micros * n)).check()

            return E(T.INTERVAL, strict2(scale, interval, count))
    if {a.type.kind, b.type.kind} == {"INTERVAL", "FLOAT64"} and a.lit != "null" and b.lit != "null" and c.mode == "googlesql":
        interval, factor = (a, b) if a.type.kind == "INTERVAL" else (b, a)  # GoogleSQL only: BigQuery has INTERVAL * INT64
        return E(T.INTERVAL, strict2(lambda i, f: _interval_times_double(i, f), interval, factor))
    if {a.type.kind, b.type.kind} == {"INTERVAL", "FLOAT64"}:
        raise Unsupported("INTERVAL * FLOAT64")
    if a.lit == "null":
        a = c.coerce(a, b.type)
    if b.lit == "null":
        b = c.coerce(b, a.type)
    return numeric_binary(c, "*", a, b, "*")


@handles(exp.Div)
def div(c: Compiler, node, cx):
    _only(node, "this", "expression", "typed")
    if node.args.get("safe"):
        raise Unsupported("safe division flag")
    a, b = c.expr(node.this, cx), c.expr(node.expression, cx)
    if a.lit == "null" and b.lit == "null":
        raise AnalysisError("Operands of / cannot be literal NULL")
    if a.type.kind == "INTERVAL" and b.type.kind == "INT64" and a.lit != "null":
        from . import datetimes as D

        return E(T.INTERVAL, strict2(D.interval_divide, a, b))
    if a.lit == "null":
        a = c.coerce(a, b.type)
    if b.lit == "null":
        b = c.coerce(b, a.type)
    return numeric_binary(c, "/", a, b, "/")


@handles(exp.Neg)
def neg(c: Compiler, node, cx):
    _only(node, "this")
    inner = node.this
    if isinstance(inner, exp.Literal) and not inner.is_string and _INTEGER.match(inner.this):
        value = -int(inner.this)
        if value < V.INT64_MIN:
            raise AnalysisError(f"Invalid integer literal: -{inner.this}")
        return const(T.INT64, value)
    value = c.expr(inner, cx)
    kind = value.type.kind
    fn = value.fn
    if value.lit == "literal" and value.value is not None and kind in ("FLOAT64",):
        negated = const(T.FLOAT64, -value.value)
        negated.exact = None if value.exact is None else -value.exact
        return negated
    if kind == "INT64":
        return E(T.INT64, lambda env: (lambda v: None if v is None else V.int64(-v))(fn(env)), "literal" if value.lit == "null" else None)
    if kind in ("NUMERIC", "BIGNUMERIC", "FLOAT64"):
        return E(value.type, lambda env: (lambda v: None if v is None else -v)(fn(env)))
    if kind == "INTERVAL":
        return E(T.INTERVAL, lambda env: (lambda v: None if v is None else -v)(fn(env)))
    raise AnalysisError(f"No matching signature for operator - for argument type {value.type}")


@handles(exp.DPipe)
def dpipe(c: Compiler, node, cx):
    _only(node, "this", "expression", "safe")
    a, b = c.expr(node.this, cx), c.expr(node.expression, cx)
    if a.type.kind == "ARRAY" or b.type.kind == "ARRAY":
        from .functions import array_concat

        return array_concat(c, [a, b])
    if c.mode == "googlesql" and a.lit != "null" and b.lit != "null":
        # GoogleSQL with mixed-type concatenation: a non-STRING operand is cast to STRING (BigQuery has no such overload)
        printable = ("INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64", "BOOL", "DATE", "DATETIME", "TIME", "TIMESTAMP")
        kinds = {a.type.kind, b.type.kind}
        if "STRING" in kinds and len(kinds) == 2 and kinds - {"STRING"} <= set(printable):
            a, b = (x if x.type.kind == "STRING" else cast_value(c, x, T.STRING, False) for x in (a, b))
        elif "STRING" not in kinds and kinds <= set(printable):
            raise Unsupported("|| over operands that are neither STRING nor BYTES")
    target = T.supertype([(a.type, a.lit), (b.type, b.lit)])
    if target.kind not in ("STRING", "BYTES"):
        raise AnalysisError(f"No matching signature for operator || for argument types: {a.type}, {b.type}")
    a, b = c.coerce(a, target), c.coerce(b, target)
    return E(target, strict2(lambda x, y: x + y, a, b))


def _int_binary(name: str, fn):
    def handler(c: Compiler, node, cx):
        _only(node, "this", "expression")
        a, b = c.expr(node.this, cx), c.expr(node.expression, cx)
        if a.lit == "null":
            a = c.coerce(a, T.INT64)
        if b.lit == "null":
            b = c.coerce(b, T.INT64)
        if a.type != T.INT64 or b.type != T.INT64:
            if a.type == T.BYTES or b.type == T.BYTES:
                raise Unsupported(f"bitwise {name} on BYTES")
            raise AnalysisError(f"No matching signature for operator {name} for argument types: {a.type}, {b.type}")
        return E(T.INT64, strict2(fn, a, b))

    return handler


def _to_signed(value: int) -> int:
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= 1 << 63 else value


def _shift_left(a: int, b: int) -> int:
    if b < 0:
        raise EvalError("Bitwise shift by negative offset.")
    if b >= 64:
        return 0
    return _to_signed(a << b)


def _shift_right(a: int, b: int) -> int:
    if b < 0:
        raise EvalError("Bitwise shift by negative offset.")
    if b >= 64:
        return 0
    return _to_signed((a & ((1 << 64) - 1)) >> b)


handles(exp.BitwiseAnd)(_int_binary("&", lambda a, b: a & b))
handles(exp.BitwiseOr)(_int_binary("|", lambda a, b: a | b))
handles(exp.BitwiseXor)(_int_binary("^", lambda a, b: a ^ b))
handles(getattr(exp, "BitwiseLeftShift", None))(_int_binary("<<", _shift_left))
handles(getattr(exp, "BitwiseRightShift", None))(_int_binary(">>", _shift_right))


@handles(exp.BitwiseNot)
def bitwise_not(c, node, cx):
    _only(node, "this")
    value = c.expr(node.this, cx)
    if value.lit == "null":
        value = c.coerce(value, T.INT64)
    if value.type != T.INT64:
        raise AnalysisError(f"No matching signature for operator ~ for argument type {value.type}")
    fn = value.fn
    return E(T.INT64, lambda env: (lambda v: None if v is None else ~v)(fn(env)))


@handles(exp.Mod)
def mod(c, node, cx):
    from .functions import call_named

    _only(node, "this", "expression")
    return call_named(c, "MOD", [c.expr(node.this, cx), c.expr(node.expression, cx)], node, cx)


@handles(exp.IntDiv)
def int_div(c, node, cx):
    from .functions import call_named

    _only(node, "this", "expression")
    return call_named(c, "DIV", [c.expr(node.this, cx), c.expr(node.expression, cx)], node, cx)


# --- comparison ------------------------------------------------------------------------------------


def less_fn(t: T.Type):
    if t.kind == "INTERVAL":
        return lambda a, b: a.total() < b.total()
    return lambda a, b: a < b


def comparison(c: Compiler, op: str, a: E, b: E) -> E:
    target, (a, b) = c.unify([a, b], f"Operand of {op}")
    if op in ("=", "!="):
        if not T.equatable(target):
            if target.kind == "ARRAY":
                raise Unsupported("array equality")
            raise AnalysisError(f"Equality is not defined for arguments of type {target}")
    elif not T.comparable(target):
        raise AnalysisError(f"Less than is not defined for arguments of type {target}")
    fa, fb = a.fn, b.fn
    if op == "=":
        return E(T.BOOL, lambda env: V.sql_equal(target, fa(env), fb(env)))
    if op == "!=":
        def ne(env):
            r = V.sql_equal(target, fa(env), fb(env))
            return None if r is None else not r

        return E(T.BOOL, ne)
    less = less_fn(target)
    if op == "<":
        f = less
    elif op == ">":
        f = lambda x, y: less(y, x)  # noqa: E731
    elif op == "<=":
        f = lambda x, y: less(x, y) or _eq_plain(target, x, y)  # noqa: E731
    else:
        f = lambda x, y: less(y, x) or _eq_plain(target, x, y)  # noqa: E731
    return E(T.BOOL, strict2(f, a, b))


def _eq_plain(t: T.Type, a, b) -> bool:
    if t.kind == "INTERVAL":
        return a.total() == b.total()
    return a == b


def _comparison_handler(op):
    def handler(c, node, cx):
        _only(node, "this", "expression")
        return comparison(c, op, c.expr(node.this, cx), c.expr(node.expression, cx))

    return handler


handles(exp.EQ)(_comparison_handler("="))
handles(exp.NEQ)(_comparison_handler("!="))
handles(exp.LT)(_comparison_handler("<"))
handles(exp.LTE)(_comparison_handler("<="))
handles(exp.GT)(_comparison_handler(">"))
handles(exp.GTE)(_comparison_handler(">="))


def distinct_from(c: Compiler, a: E, b: E, negate: bool) -> E:
    target, (a, b) = c.unify([a, b], "Operand of IS DISTINCT FROM")
    if not T.groupable(target):
        raise AnalysisError(f"IS DISTINCT FROM is not defined for {target}")
    fa, fb = a.fn, b.fn

    def run(env):
        same = V.group_key(target, fa(env)) == V.group_key(target, fb(env))
        return same if negate else not same

    return E(T.BOOL, run)


@handles(exp.NullSafeEQ)
def null_safe_eq(c, node, cx):
    _only(node, "this", "expression")
    return distinct_from(c, c.expr(node.this, cx), c.expr(node.expression, cx), True)


@handles(exp.NullSafeNEQ)
def null_safe_neq(c, node, cx):
    _only(node, "this", "expression")
    return distinct_from(c, c.expr(node.this, cx), c.expr(node.expression, cx), False)


@handles(exp.Is)
def is_(c: Compiler, node, cx):
    _only(node, "this", "expression")
    value = c.expr(node.this, cx)
    target = node.expression
    fn = value.fn
    if isinstance(target, exp.Null):
        return E(T.BOOL, lambda env: fn(env) is None)
    if isinstance(target, exp.Boolean):
        if value.lit == "null":
            value = c.coerce(value, T.BOOL)
            fn = value.fn
        if value.type != T.BOOL:
            raise AnalysisError(f"IS TRUE/FALSE needs a BOOL, got {value.type}")
        wanted = bool(target.this)
        return E(T.BOOL, lambda env: fn(env) is wanted)
    raise Unsupported("IS with this operand")


# --- logic -----------------------------------------------------------------------------------------


def as_bool(c: Compiler, value: E, what: str) -> E:
    if value.lit == "null":
        return c.coerce(value, T.BOOL)
    if value.type != T.BOOL:
        raise AnalysisError(f"{what} needs BOOL arguments, got {value.type}")
    return value


@handles(exp.Not)
def not_(c, node, cx):
    _only(node, "this")
    operand = c.expr(node.this, cx)
    if operand.lit == "null" and c.mode == "googlesql":
        raise AnalysisError("Operands of NOT cannot be literal NULL")
    value = as_bool(c, operand, "NOT")
    fn = value.fn
    return E(T.BOOL, lambda env: (lambda v: None if v is None else not v)(fn(env)))


def _and_or(is_and: bool):
    decisive = not is_and

    def handler(c: Compiler, node, cx):
        _only(node, "this", "expression")
        operands = []

        def flatten(n):
            if type(n) is type(node):
                flatten(n.this)
                flatten(n.expression)
            else:
                operands.append(n)

        flatten(node)
        values = [as_bool(c, c.expr(o, cx), "AND" if is_and else "OR").fn for o in operands]

        def run(env):
            unknown = False
            error = None
            decided = False
            for fn in values:
                try:
                    v = fn(env)
                except EvalError as e:
                    if decided:
                        env.ctx.nondet("AND/OR operand fails after another decided the result")
                        continue
                    error = e
                    continue
                if v is decisive:
                    if error is not None:
                        env.ctx.nondet("AND/OR operand fails while another decides the result")
                    decided = True
                    continue
                if v is None:
                    unknown = True
            if decided:
                return decisive
            if error is not None:
                raise error
            return None if unknown else (not decisive)

        return E(T.BOOL, run)

    return handler


handles(exp.And)(_and_or(True))
handles(exp.Or)(_and_or(False))


@handles(getattr(exp, "Xor", None))
def xor(c, node, cx):
    raise Unsupported("XOR")


# --- IN, BETWEEN, LIKE -------------------------------------------------------------------------------


def _in_values(target: T.Type, x, items) -> bool | None:
    if not items:
        return False
    if x is None:
        return None
    unknown = False
    for item in items:
        r = V.sql_equal(target, x, item)
        if r is True:
            return True
        if r is None:
            unknown = True
    return None if unknown else False


@handles(exp.In)
def in_(c: Compiler, node: exp.In, cx: Cx) -> E:
    _only(node, "this", "expressions", "query", "unnest", "is_global")
    left = c.expr(node.this, cx)
    query = node.args.get("query")
    unnest = node.args.get("unnest")
    if query is not None:
        inner = query.this if isinstance(query, exp.Subquery) and not any(query.args.get(k) for k in ("order", "limit", "offset", "with_", "alias")) else query
        plan = c.query(inner, cx.scope, c.ctes)
        if len(plan.columns) != 1:
            raise AnalysisError("Subquery of IN must have only one output column")
        target = T.supertype([(left.type, left.lit), (plan.columns[0][1], None)])
        if not T.equatable(target):
            raise AnalysisError(f"IN is not defined for {target}")
        left = c.coerce(left, target)
        convert = None if plan.columns[0][1] == target else V.caster(plan.columns[0][1], target)
        lf, run_plan = left.fn, plan.run

        def run(env):
            x = lf(env)
            rows = run_plan(env)
            items = [r[0] if convert is None or r[0] is None else convert(r[0], env.ctx.tz) for r in rows]
            return _in_values(target, x, items)

        return E(T.BOOL, run)
    if unnest is not None:
        _only(unnest, "expressions", "offset", "alias")
        if len(unnest.expressions) != 1 or unnest.args.get("alias") is not None or unnest.args.get("offset"):
            raise Unsupported("IN UNNEST form")
        array = _typed_empty(c, c.path_expr(unnest.expressions[0], cx), left)
        if array.lit == "null":
            raise AnalysisError("IN UNNEST of an untyped NULL")
        if array.type.kind != "ARRAY":
            raise AnalysisError("IN UNNEST needs an array")
        target = T.supertype([(left.type, left.lit), (array.type.elem, None)])
        if not T.equatable(target):
            raise AnalysisError(f"IN is not defined for {target}")
        left = c.coerce(left, target)
        convert = None if array.type.elem == target else V.caster(array.type.elem, target)
        lf, af = left.fn, array.fn

        def run(env):
            x = lf(env)
            items = af(env)
            if items is None:
                items = ()
            if convert is not None:
                items = [None if v is None else convert(v, env.ctx.tz) for v in items]
            return _in_values(target, x, items)

        return E(T.BOOL, run)
    items = c.exprs(node.expressions, cx)
    if not items:
        raise AnalysisError("IN list is empty")
    target, coerced = c.unify([left] + items, "IN")
    if not T.equatable(target):
        if target.kind == "ARRAY":
            raise Unsupported("IN over arrays")
        raise AnalysisError(f"IN is not defined for {target}")
    lf = coerced[0].fn
    fns = [e.fn for e in coerced[1:]]

    def run(env):
        x = lf(env)
        if x is None:
            return None
        unknown = False
        for fn in fns:
            r = V.sql_equal(target, x, fn(env))
            if r is True:
                return True
            if r is None:
                unknown = True
        return None if unknown else False

    return E(T.BOOL, run)


@handles(exp.Between)
def between(c: Compiler, node, cx):
    _only(node, "this", "low", "high", "symmetric")
    if node.args.get("symmetric"):
        raise Unsupported("BETWEEN SYMMETRIC")
    value, low, high = c.expr(node.this, cx), c.expr(node.args["low"], cx), c.expr(node.args["high"], cx)
    target, (value, low, high) = c.unify([value, low, high], "BETWEEN")
    if not T.comparable(target):
        raise AnalysisError(f"BETWEEN is not defined for {target}")
    less = less_fn(target)
    fv, fl, fh = value.fn, low.fn, high.fn

    def run(env):
        x, lo, hi = fv(env), fl(env), fh(env)
        # x >= lo AND x <= hi, each with the ordinary comparison rules (NaN compares false).
        ge = None if x is None or lo is None else (less(lo, x) or _eq_plain(target, x, lo))
        le = None if x is None or hi is None else (less(x, hi) or _eq_plain(target, x, hi))
        if ge is False or le is False:
            return False
        if ge is None or le is None:
            return None
        return True

    return E(T.BOOL, run)


def _like_match(target: T.Type, cache: dict):
    def match(value, pattern):
        regex = cache.get(pattern)
        if regex is None:
            regex = cache[pattern] = V.like_regex(pattern)
        return regex.fullmatch(value) is not None

    return match


@handles(exp.Like)
def like(c: Compiler, node: exp.Like, cx: Cx) -> E:
    _only(node, "this", "expression", "negate")
    negate = bool(node.args.get("negate"))
    value = c.expr(node.this, cx)
    pattern_node = node.expression
    if isinstance(pattern_node, (exp.Any, exp.All)):
        return _quantified_like(c, value, pattern_node, negate, cx)
    pattern = c.expr(pattern_node, cx)
    target = T.supertype([(value.type, value.lit), (pattern.type, pattern.lit)])
    if target.kind not in ("STRING", "BYTES"):
        raise AnalysisError(f"No matching signature for operator LIKE for argument types: {value.type}, {pattern.type}")
    value, pattern = c.coerce(value, target), c.coerce(pattern, target)
    match = _like_match(target, {})
    fn = strict2(match, value, pattern)
    if negate:
        return E(T.BOOL, lambda env: (lambda r: None if r is None else not r)(fn(env)))
    return E(T.BOOL, fn)


def _typed_empty(c: Compiler, array: E, other: E) -> E:
    """The untyped literal ``[]`` searched against ``other`` takes ``other``'s type."""

    if array.lit == "literal" and array.type.kind == "ARRAY" and array.value == ():
        return c.coerce(array, T.array(other.type))
    return array


def _quantified_like(c: Compiler, value: E, node, negate: bool, cx: Cx) -> E:
    is_any = isinstance(node, exp.Any)
    inner = node.this
    if isinstance(inner, exp.Tuple):
        patterns = c.exprs(inner.expressions, cx)
        target, coerced = c.unify([value] + patterns, "LIKE")
        value, patterns = coerced[0], coerced[1:]
        pattern_fns = [p.fn for p in patterns]
        source = lambda env: [f(env) for f in pattern_fns]  # noqa: E731
    elif isinstance(inner, exp.Paren):
        patterns = [c.expr(inner.this, cx)]
        target, coerced = c.unify([value] + patterns, "LIKE")
        value, patterns = coerced[0], coerced[1:]
        pf = patterns[0].fn
        source = lambda env: [pf(env)]  # noqa: E731
    elif isinstance(inner, exp.Unnest):
        if len(inner.expressions) != 1:
            raise Unsupported("LIKE ANY UNNEST form")
        array = _typed_empty(c, c.expr(inner.expressions[0], cx), value)
        if array.type.kind != "ARRAY":
            raise AnalysisError("LIKE ANY UNNEST needs an array")
        target = T.supertype([(value.type, value.lit), (array.type.elem, None)])
        value = c.coerce(value, target)
        if array.type.elem != target:
            raise AnalysisError("LIKE pattern array of another type")
        af = array.fn
        source = lambda env: list(af(env) or ())  # noqa: E731
    else:
        raise Unsupported("LIKE ANY with a subquery")
    if target.kind not in ("STRING", "BYTES"):
        raise AnalysisError(f"LIKE is not defined for {target}")
    match = _like_match(target, {})
    vf = value.fn

    def run(env):
        x = vf(env)
        patterns = source(env)
        if not patterns:
            return not is_any  # ANY over nothing is FALSE, ALL over nothing TRUE
        results = []
        for p in patterns:
            if x is None or p is None:
                results.append(None)
            else:
                matched = match(x, p)
                results.append((not matched) if negate else matched)  # x NOT LIKE ANY (p...) is ANY (x NOT LIKE p)
        if is_any:
            return True if True in results else (None if None in results else False)
        return False if False in results else (None if None in results else True)

    return E(T.BOOL, run)


# --- conditionals ------------------------------------------------------------------------------------


@handles(exp.Case)
def case(c: Compiler, node: exp.Case, cx: Cx) -> E:
    _only(node, "this", "ifs", "default")
    operand = node.args.get("this")
    conditions = []
    results = []
    for branch in node.args.get("ifs") or []:
        _only(branch, "this", "true")
        conditions.append(branch.this)
        results.append(c.expr(branch.args["true"], cx))
    default_node = node.args.get("default")
    default = c.expr(default_node, cx) if default_node is not None else NULL
    result_type, coerced = c.unify(results + [default], "CASE result")
    branch_fns = [e.fn for e in coerced[:-1]]
    default_fn = coerced[-1].fn
    if operand is not None:
        op = c.expr(operand, cx)
        whens = c.exprs(conditions, cx)
        target, unified = c.unify([op] + whens, "CASE")
        if not T.equatable(target):
            raise AnalysisError(f"CASE operand of type {target} cannot be compared")
        of = unified[0].fn
        when_fns = [w.fn for w in unified[1:]]

        def run(env):
            x = of(env)
            for when, branch in zip(when_fns, branch_fns):
                if V.sql_equal(target, x, when(env)) is True:
                    return branch(env)
            return default_fn(env)

        return E(result_type, run)
    cond_fns = [as_bool(c, c.expr(cond, cx), "CASE WHEN").fn for cond in conditions]

    def run_searched(env):
        for cond, branch in zip(cond_fns, branch_fns):
            if cond(env) is True:
                return branch(env)
        return default_fn(env)

    return E(result_type, run_searched)


@handles(exp.If)
def if_(c: Compiler, node, cx):
    _only(node, "this", "true", "false")
    cond = as_bool(c, c.expr(node.this, cx), "IF").fn
    then = c.expr(node.args["true"], cx)
    other = c.expr(node.args["false"], cx) if node.args.get("false") is not None else NULL
    result_type, (then, other) = c.unify([then, other], "IF")
    tf, ef = then.fn, other.fn
    return E(result_type, lambda env: tf(env) if cond(env) is True else ef(env))


@handles(exp.Coalesce)
def coalesce(c: Compiler, node, cx):
    _only(node, "this", "expressions", "is_nvl", "is_null")
    values = c.exprs([node.this] + list(node.expressions), cx)
    result_type, coerced = c.unify(values, "COALESCE")
    fns = [e.fn for e in coerced]

    def run(env):
        for fn in fns:
            v = fn(env)
            if v is not None:
                return v
        return None

    return E(result_type, run)


@handles(exp.Nullif)
def nullif(c: Compiler, node, cx):
    _only(node, "this", "expression")
    a, b = c.expr(node.this, cx), c.expr(node.expression, cx)
    target, (a2, b2) = c.unify([a, b], "NULLIF")
    if not T.equatable(target):
        raise AnalysisError(f"NULLIF is not defined for {target}")
    fa, fb = a2.fn, b2.fn

    def run(env):
        x = fa(env)
        if x is None:
            return None
        return None if V.sql_equal(target, x, fb(env)) is True else x

    return E(target, run)


# --- subqueries --------------------------------------------------------------------------------------


def _subquery_plan(c: Compiler, node, cx: Cx):
    inner = node
    if isinstance(node, exp.Subquery) and not any(node.args.get(k) for k in ("order", "limit", "offset", "with_", "alias")):
        inner = node.this
    return c.query(inner, cx.scope, c.ctes)


@handles(exp.Subquery)
def scalar_subquery(c: Compiler, node, cx):
    plan = _subquery_plan(c, node, cx)
    if len(plan.columns) != 1:
        raise AnalysisError("Scalar subquery cannot have more than one column unless using SELECT AS STRUCT")
    typ = plan.columns[0][1]
    run_plan = plan.run

    def run(env):
        rows = run_plan(env)
        if not rows:
            return None
        if len(rows) > 1:
            raise EvalError("Scalar subquery produced more than one element")
        return rows[0][0]

    return E(typ, run)


@handles(exp.Exists)
def exists(c: Compiler, node, cx):
    _only(node, "this")
    plan = c.query(node.this, cx.scope, c.ctes)
    run_plan = plan.run
    return E(T.BOOL, lambda env: len(run_plan(env)) > 0)


@handles(exp.Array)
def array(c: Compiler, node, cx):
    _only(node, "expressions", "struct_name_inheritance")
    expressions = node.expressions
    if len(expressions) == 1 and isinstance(expressions[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        plan = c.query(expressions[0], cx.scope, c.ctes)
        if len(plan.columns) != 1:
            raise AnalysisError("ARRAY subquery cannot have more than one column unless using SELECT AS STRUCT")
        elem = plan.columns[0][1]
        if elem.kind == "ARRAY":
            raise AnalysisError("Cannot use array subquery with column of type ARRAY")
        array_type = T.array(elem)
        run_plan = plan.run
        ordered = plan.ordered

        def run(env):
            rows = run_plan(env)
            values = tuple(r[0] for r in rows)
            return values if ordered else V.UnorderedArray(values)

        return E(array_type, run)
    if any(is_query(e) and not isinstance(e, exp.Subquery) for e in expressions):
        raise Unsupported("ARRAY form")
    values = c.exprs(expressions, cx)
    return array_literal(c, values, None)


def array_literal(c: Compiler, values: list[E], declared: T.Type | None) -> E:
    if declared is not None:
        elem = declared
        values = [c.coerce(v, elem, "Array element") for v in values]
    elif values:
        elem, values = c.unify(values, "Array element")
    else:  # [] with no declared type: an INT64 array that takes the type of whatever it meets (see Compiler.coerce)
        return E(T.array(T.INT64), lambda env: (), "literal", ())
    fns = [v.fn for v in values]
    if all(is_constant(v) for v in values):
        payload = tuple(v.value for v in values)
        return E(T.array(elem), lambda env: payload)
    return E(T.array(elem), lambda env: tuple(f(env) for f in fns))


def _field_name(item: exp.Expression) -> str | None:
    if isinstance(item, exp.PropertyEQ):
        return item.this.name
    if isinstance(item, exp.Alias):
        return item.alias
    parts = path_parts(item)
    if parts is not None:
        return parts[-1]
    if isinstance(item, exp.Dot) and isinstance(item.expression, exp.Identifier):
        return item.expression.name
    return None


@handles(exp.Struct)
def struct(c: Compiler, node, cx):
    _only(node, "expressions")
    names = []
    values = []
    for item in node.expressions:
        names.append(_field_name(item))
        inner = item.expression if isinstance(item, exp.PropertyEQ) else (item.this if isinstance(item, exp.Alias) else item)
        values.append(c.expr(inner, cx))
    return struct_value(names, values)


def struct_value(names, values: list[E]) -> E:
    typ = T.struct(list(zip(names, [v.type for v in values])))
    fns = [v.fn for v in values]
    return E(typ, lambda env: tuple(f(env) for f in fns), None, None, tuple(v.info for v in values))


@handles(exp.Tuple)
def tuple_(c: Compiler, node, cx):
    _only(node, "expressions")
    if len(node.expressions) < 2:
        raise Unsupported("tuple")
    values = c.exprs(node.expressions, cx)
    return struct_value([None] * len(values), values)


@handles(getattr(exp, "Flatten", None))
def flatten(c: Compiler, node, cx):
    _only(node, "this")
    inner = node.this
    while isinstance(inner, exp.Paren):
        inner = inner.this
    parts = path_parts(inner)
    if not (isinstance(inner, (exp.Dot, exp.Bracket)) or (parts is not None and len(parts) > 1)):
        raise Unsupported("FLATTEN of something that is not a path")
    result = c.path_expr(inner, cx)
    if result.type.kind != "ARRAY":
        raise AnalysisError("FLATTEN needs a path that yields an array")
    return result


@handles(exp.Bracket)
def bracket(c: Compiler, node, cx):
    _only(node, "this", "expressions", "offset", "safe", "returns_list_for_maps")
    base = c.expr(node.this, cx)
    if len(node.expressions) != 1:
        raise Unsupported("subscript with several indexes")
    index_node = node.expressions[0]
    if base.type.kind != "ARRAY":
        if base.type.kind == "STRUCT":
            raise Unsupported("struct positional access")
        raise AnalysisError(f"Element access using [] is not supported on values of type {base.type}")
    index = c.expr(index_node, cx)
    if index.lit == "null":
        index = c.coerce(index, T.INT64)
    if index.type != T.INT64:
        raise AnalysisError(f"Array element access with array position of type {index.type} is not supported")
    base_offset = node.args.get("offset") or 0
    safe = bool(node.args.get("safe"))
    bf, xf = base.fn, index.fn
    elem = base.type.elem

    def run(env):
        array_value = bf(env)
        position = xf(env)
        if array_value is None or position is None:
            return None
        i = position - base_offset
        if i < 0 or i >= len(array_value):
            if safe:
                return None
            raise EvalError(f"Array index {position} is out of bounds")
        if not V.ordered_kind(array_value):
            env.ctx.nondet("element of an unordered array")
        return array_value[i]

    return E(elem, run)


# --- casts --------------------------------------------------------------------------------------------


def cast_value(c: Compiler, value: E, target: T.Type, safe: bool) -> E:
    if value.lit == "null":
        return E(target, value.fn)
    if not V.castable(value.type, target):
        raise AnalysisError(f"Invalid cast from {value.type} to {target}")
    convert = V.caster(value.type, target)
    fn = value.fn
    if safe:
        def run_safe(env):
            v = fn(env)
            if v is None:
                return None
            try:
                return convert(v, env.ctx.tz)
            except EvalError:
                return None

        return E(target, run_safe)
    return E(target, lambda env: (lambda v: None if v is None else convert(v, env.ctx.tz))(fn(env)))


@handles(exp.Cast, exp.TryCast)
def cast(c: Compiler, node, cx):
    _only(node, "this", "to", "safe", "format", "action", "default")
    if node.args.get("format") is not None:
        raise Unsupported("CAST ... FORMAT")
    if node.args.get("action") or node.args.get("default"):
        raise Unsupported("CAST option")
    safe = bool(node.args.get("safe")) or isinstance(node, exp.TryCast)
    target = T.from_sqlglot(node.args["to"])
    inner = node.this
    if isinstance(inner, exp.Struct) and target.kind == "STRUCT" and not safe:
        return _typed_struct(c, inner, target, cx)
    if isinstance(inner, exp.Array) and target.kind == "ARRAY" and not safe and not (
        len(inner.expressions) == 1 and is_query(inner.expressions[0]) and not isinstance(inner.expressions[0], exp.Subquery)
    ):
        values = c.exprs(inner.expressions, cx)
        for v in values:
            if not (v.lit or T.implicitly_coercible(v.type, target.elem)):
                raise Unsupported("ARRAY<T>[...] versus CAST of an array (sqlglot reads both the same)")
        return array_literal(c, values, target.elem)
    value = c.expr(inner, cx)
    if value.exact is not None and target.kind in ("NUMERIC", "BIGNUMERIC"):
        try:
            folded = V.decimal_of(target.kind)(value.exact)
        except EvalError:
            pass  # out of range: fails (or gives NULL under SAFE_CAST) when run, like any cast
        else:
            return E(target, lambda env: folded, None, folded)
    if value.lit == "literal" and value.value is not None and not safe:
        # a typed literal (DATE '2020-01-01') or a cast of one: fold it, failing at analysis as BigQuery does
        if not V.castable(value.type, target):
            raise AnalysisError(f"Invalid cast from {value.type} to {target}")
        try:
            folded = V.caster(value.type, target)(value.value, c.tz)
        except EvalError:
            return cast_value(c, value, target, safe)
        return E(target, lambda env: folded, None, folded)
    return cast_value(c, value, target, safe)


def _typed_struct(c: Compiler, node: exp.Struct, target: T.Type, cx: Cx) -> E:
    if len(node.expressions) != len(target.fields):
        raise AnalysisError("STRUCT constructor has the wrong number of fields")
    values = []
    for item, (_, ftype) in zip(node.expressions, target.fields):
        if isinstance(item, (exp.PropertyEQ, exp.Alias)):
            raise AnalysisError("STRUCT<...> constructor fields cannot have aliases")
        value = c.expr(item, cx)
        if not (value.lit or T.implicitly_coercible(value.type, ftype)):
            raise Unsupported("STRUCT<T>(...) versus CAST of a struct (sqlglot reads both the same)")
        values.append(c.coerce(value, ftype))
    fns = [v.fn for v in values]
    return E(target, lambda env: tuple(f(env) for f in fns))


@handles(getattr(exp, "SafeFunc", None))
def safe_func(c: Compiler, node, cx):
    from .functions import compile_call

    _only(node, "this")
    return compile_call(c, node.this, cx, safe=True)


# --- intervals ----------------------------------------------------------------------------------------


@handles(exp.Interval)
def interval(c: Compiler, node, cx):
    from . import datetimes as D

    _only(node, "this", "unit")
    unit = node.args.get("unit")
    if isinstance(unit, getattr(exp, "IntervalSpan", ())):
        if not (isinstance(node.this, exp.Literal) and node.this.is_string):
            raise Unsupported("interval span of a non-literal")
        start = unit.this.name.upper()
        end = unit.expression.name.upper()
        value = D.parse_interval_span(node.this.this, start, end)
        return const(T.INTERVAL, value)
    if unit is None:
        raise Unsupported("INTERVAL without a unit")
    name = unit.name.upper()
    count_node = node.this
    if isinstance(count_node, exp.Literal) and count_node.is_string:
        text = count_node.this.strip()
        return const(T.INTERVAL, D.interval_from_text(text, name))
    count = c.expr(count_node, cx)
    if count.lit == "null":
        count = c.coerce(count, T.INT64)
    if count.type != T.INT64:
        raise AnalysisError(f"INTERVAL count must be INT64, got {count.type}")
    make = D.interval_of(name)
    fn = count.fn
    return E(T.INTERVAL, lambda env: (lambda n: None if n is None else make(n))(fn(env)))
