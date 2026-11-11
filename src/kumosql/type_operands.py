"""Operand type checks for a typed query: what ``check_query`` reports as ``types`` findings beyond the type checker's own.

:func:`kumosql.googlesql_types.infer` gives every expression a type and reports only what is certainly invalid (it does
not yet emit ``incompatible_operands`` or ``no_matching_signature``). This module reads those types and finds the
mismatches that fail most often in BigQuery, each with the two sides named and a fix:

* a **comparison** (``= <> < <= > >= IS NOT DISTINCT FROM``), ``BETWEEN`` or ``IN`` whose sides have no common type:
  ``STRING`` against ``INT64``, a number or a quoted text against the wrong column, ``DATETIME`` against ``TIMESTAMP``,
  ``DATE`` against ``TIMESTAMP``. The sides may be a join condition (``ON`` or ``USING``);
* an **arithmetic operator or numeric function** whose argument types have no signature: ``MOD(FLOAT64, INT64)``,
  ``STRING + INT64``, ``SUM(STRING)``.

It is conservative: a check runs only when every operand's type is known and scalar, a literal is allowed the types
GoogleSQL coerces it to, and a pair between different numeric types or two STRUCT, ARRAY, JSON or GEOGRAPHY values is
never reported. An operand that is a column whose type is unknown is not guessed; it is returned in
:attr:`OperandReport.unknown` so the caller can say what it could not check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from .googlesql_signatures import BINARY_NUMERIC, UNARY_NUMERIC, _arguments, function_name
from .googlesql_types import NUMERIC_KINDS, GType, TypedQuery

#: kinds this module compares; anything else (STRUCT, ARRAY, JSON, GEOGRAPHY, INTERVAL, ...) is left alone
_SCALAR_FAMILIES = ("NUMBER", "STRING", "BYTES", "BOOL", "DATE", "DATETIME", "TIMESTAMP", "TIME")
_COMPARISONS = {
    exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
    exp.NullSafeEQ: "IS NOT DISTINCT FROM", exp.NullSafeNEQ: "IS DISTINCT FROM",
}
_ARITHMETIC = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.IntDiv: "DIV", exp.Mod: "MOD", exp.Pow: "POW",
               exp.SafeDivide: "SAFE_DIVIDE"}
_NOT_NUMBERS = ("STRING", "BYTES", "BOOL", "DATE", "DATETIME", "TIMESTAMP", "TIME")
#: the numeric kinds a number literal can take (GoogleSQL literal coercion)
_INT_LITERAL_KINDS = ("INT64", "INT32", "UINT32", "UINT64", "NUMERIC", "BIGNUMERIC", "FLOAT64")
_FLOAT_LITERAL_KINDS = ("FLOAT64", "NUMERIC", "BIGNUMERIC", "FLOAT32")


@dataclass(frozen=True)
class OperandFinding:
    code: str  # ``incompatible_operands`` or ``no_matching_signature``
    what: str
    fix: str


@dataclass
class UnknownOperand:
    """A column whose type is unknown where a check needed it."""

    column: exp.Column
    #: the project-table alias or name the column was read through (``None`` when it could not be told)
    table: exp.Table | None
    where: str


@dataclass
class OperandReport:
    findings: list[OperandFinding] = field(default_factory=list)
    #: columns of unknown type that stood in a comparison, join, set operation or numeric call
    unknown: list[UnknownOperand] = field(default_factory=list)
    #: how many operand pairs were compared (both types known)
    decided: int = 0
    #: how many pairs could not be compared because a type is unknown (columns are also in ``unknown``)
    undecided: int = 0
    #: of those, how many had an unknown operand that is not a column (a function of one, a subquery): nothing to name
    unnamed: int = 0


def check_operands(tree: exp.Expression, typed: TypedQuery) -> OperandReport:
    """Run the operand checks over ``tree``, which ``typed`` typed (``infer(tree, ...)``)."""

    return _Checker(tree, typed).run()


# ----------------------------------------------------------------------------------------------------- literals


def _bare(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _literal(node: exp.Expression) -> str | None:
    """``string`` or ``number`` for a quoted text or number literal (a leading minus allowed), else ``None``."""

    node = _bare(node)
    if isinstance(node, exp.Neg):
        node = _bare(node.this)
        return "number" if isinstance(node, exp.Literal) and not node.is_string else None
    if isinstance(node, exp.Literal):
        return "string" if node.is_string else "number"
    return None


def _family(kind: GType | None) -> str | None:
    if kind is None or not kind.complete:
        return None
    if kind.kind in NUMERIC_KINDS:
        return "NUMBER"
    return kind.kind if kind.kind in _SCALAR_FAMILIES else None


def _short(node: exp.Expression, limit: int = 40) -> str:
    text = node.sql(dialect="bigquery")
    return text if len(text) <= limit else text[: limit - 3] + "..."


class _Operand:
    """One side of a check: its node, a family, a printable type and whether it is a literal."""

    def __init__(self, node: exp.Expression, kind: GType | None) -> None:
        self.node = _bare(node)
        self.literal = _literal(node)
        self.family = _family(kind)
        self.kind = kind
        self.is_null = isinstance(self.node, exp.Null)

    @property
    def known(self) -> bool:
        return self.family is not None

    @property
    def unresolved(self) -> bool:
        """Whether the type is not known (an operand with a known type this module does not compare is not unresolved)."""

        return not self.is_null and (self.kind is None or not self.kind.complete)

    @property
    def label(self) -> str:
        base = self.kind.sql() if self.kind is not None else "?"
        return f"{_short(self.node)} ({base}{' literal' if self.literal else ''})"


def _comparison_problem(left: _Operand, right: _Operand) -> bool:
    """Whether GoogleSQL certainly has no common type for the two sides (a literal takes the types it coerces to)."""

    pair = (left, right)
    for one, other in (pair, pair[::-1]):
        if one.literal == "string":
            if other.literal == "string":
                return False
            return other.family in ("NUMBER", "BOOL")  # a quoted text becomes DATE, DATETIME, TIME, TIMESTAMP, STRING
        if one.literal == "number":
            if other.literal is not None:
                return other.literal == "string"
            return other.family in _NOT_NUMBERS
    if left.family == right.family:
        return False
    if left.family == "NUMBER" or right.family == "NUMBER":
        return True
    return {left.family, right.family} != {"DATE", "DATETIME"}


def _cast_fix(subject: _Operand, others: list[_Operand]) -> str:
    """The fix for a mismatch between ``subject`` and ``others`` (all typed): a matching literal or a CAST."""

    first = others[0]
    cast = lambda o, to: f"CAST({_short(o.node, 30)} AS {to})"  # noqa: E731
    if first.literal is not None:
        # the literal is the cheap side to change
        if subject.family == "STRING" and first.literal == "number":
            texts = ", ".join(f"'{_short(o.node, 20)}'" for o in others if o.literal == "number")
            return f"Write the literal as text ({texts}) to match the STRING column, or {cast(subject, 'INT64')} to compare as numbers."
        if first.literal == "string" and subject.family == "NUMBER":
            return f"Write the number without quotes, or {cast(subject, 'STRING')} to compare as text."
        return f"Write the literal as a {subject.kind.sql()} value, or {cast(subject, 'STRING')} if it is text."
    if "STRING" in (subject.family, first.family) and "NUMBER" in (subject.family, first.family):
        number = subject if subject.family == "NUMBER" else first
        text = first if number is subject else subject
        return f"Compare like with like: {cast(number, 'STRING')} to compare as text, or {cast(text, number.kind.sql())} as numbers."
    return f"Make both sides one type: {cast(subject, first.kind.sql())} or {cast(first, subject.kind.sql())} ({subject.kind.sql()} vs {first.kind.sql()})."


# ---------------------------------------------------------------------------------------------------- the walk


class _Checker:
    def __init__(self, tree: exp.Expression, typed: TypedQuery) -> None:
        self.tree = tree
        self.typed = typed
        self.report = OperandReport()
        self._seen_unknown: set[int] = set()

    def run(self) -> OperandReport:
        for node in self.tree.walk():
            node = node[0] if isinstance(node, tuple) else node
            kind = type(node)
            if kind in _COMPARISONS:
                self.compare(node, _COMPARISONS[kind], node.this, [node.expression])
            elif isinstance(node, exp.Between):
                self.compare(node, "BETWEEN", node.this, [node.args.get("low"), node.args.get("high")])
            elif isinstance(node, exp.In):
                self.in_list(node)
            elif isinstance(node, exp.Join) and node.args.get("using"):
                self.using(node)
            elif isinstance(node, exp.SetOperation):
                self.set_operation(node)
            if kind in _ARITHMETIC:
                self.signature(node, _ARITHMETIC[kind], [node.this, node.expression])
            elif isinstance(node, exp.Func) and not isinstance(node, tuple(_ARITHMETIC)):
                self.function(node)
        return self.report

    # -- operands

    def operand(self, node: exp.Expression | None) -> _Operand | None:
        if node is None:
            return None
        return _Operand(node, self.typed.type_of(node))

    def note_unknown(self, operand: _Operand, where: str) -> None:
        node = operand.node
        if isinstance(node, exp.Column) and id(node) not in self._seen_unknown:
            self._seen_unknown.add(id(node))
            self.report.unknown.append(UnknownOperand(node, self._table_of(node), where))

    def _table_of(self, column: exp.Column) -> exp.Table | None:
        """The table a column is read from: the one its qualifier names, or the only table of its query."""

        select = column.find_ancestor(exp.Select)
        while select is not None:
            tables = []
            from_ = select.args.get("from_") or select.args.get("from")
            for item in [from_.this if from_ is not None else None, *[j.this for j in select.args.get("joins") or []]]:
                if isinstance(item, exp.Table):
                    tables.append(item)
            qualifier = (column.table or "").casefold()
            if qualifier:
                for table in tables:
                    if (table.alias_or_name or "").casefold() == qualifier or table.name.casefold() == qualifier:
                        return table
            elif len(tables) == 1 and not (select.args.get("joins")):
                return tables[0]
            select = select.find_ancestor(exp.Select)
        return None

    # -- comparisons

    def compare(self, node: exp.Expression, op: str, subject_node: exp.Expression, other_nodes: list) -> None:
        subject = self.operand(subject_node)
        others = [o for o in (self.operand(n) for n in other_nodes) if o is not None]
        if subject is None or not others:
            return
        where = self._place(node)
        problems = []
        for other in others:
            if subject.is_null or other.is_null:
                continue
            if subject.unresolved or other.unresolved:
                self.undecided([subject, other], where)
                continue
            if not (subject.known and other.known):
                continue  # a STRUCT, ARRAY, JSON value ...: not compared here
            self.report.decided += 1
            if _comparison_problem(subject, other):
                problems.append(other)
        if problems:
            self.report.findings.append(self.mismatch(op, where, subject, problems))

    def undecided(self, operands: list[_Operand], where: str) -> None:
        """A check that needed a type it does not have: name each unknown column, and count the rest."""

        self.report.undecided += 1
        unnamed = False
        for operand in operands:
            if not operand.unresolved:
                continue
            if isinstance(operand.node, exp.Column):
                self.note_unknown(operand, where)
            elif not isinstance(operand.node, (exp.Parameter, exp.Placeholder)):
                unnamed = True
        self.report.unnamed += int(unnamed)

    def mismatch(self, op: str, where: str, subject: _Operand, others: list[_Operand]) -> OperandFinding:
        names = ", ".join(o.label for o in others[:3]) + (", ..." if len(others) > 3 else "")
        what = f"{where}: {op} compares {subject.label} with {names}; BigQuery has no implicit conversion between {subject.kind.sql()} and {others[0].kind.sql()}"
        return OperandFinding("incompatible_operands", what, _cast_fix(subject, others))

    @staticmethod
    def _place(node: exp.Expression) -> str:
        for ancestor in _ancestors(node):
            if isinstance(ancestor, exp.Join):
                return "join condition"
            if isinstance(ancestor, exp.Where):
                return "WHERE"
            if isinstance(ancestor, exp.Having):
                return "HAVING"
            if isinstance(ancestor, exp.Qualify):
                return "QUALIFY"
            if isinstance(ancestor, exp.Select):
                break
        return "expression"

    def in_list(self, node: exp.In) -> None:
        if node.args.get("expressions"):
            self.compare(node, "IN", node.this, list(node.args["expressions"]))
            return
        unnest = node.args.get("unnest")
        if isinstance(unnest, exp.Unnest) and unnest.expressions:
            array = self.typed.type_of(unnest.expressions[0])
            if array is not None and array.kind == "ARRAY" and array.element is not None:
                element = _Operand(unnest.expressions[0], array.element)
                subject = self.operand(node.this)
                if subject is not None and subject.known and element.known:
                    self.report.decided += 1
                    if _comparison_problem(subject, element):
                        element.node = unnest.expressions[0]
                        self.report.findings.append(self.mismatch("IN UNNEST", self._place(node), subject, [element]))

    def using(self, join: exp.Join) -> None:
        select = join.find_ancestor(exp.Select)
        if select is None or not isinstance(join.this, (exp.Table, exp.Subquery)):
            return
        right = self.typed.relation(join.this)
        left_items = []
        from_ = select.args.get("from_") or select.args.get("from")
        for item in [from_.this if from_ is not None else None, *[j.this for j in select.args.get("joins") or []]]:
            if item is join.this:
                break
            if item is not None:
                left_items.append(self.typed.relation(item))
        if right is None or not left_items or any(r is None for r in left_items):
            self.report.undecided += 1
            return
        for name in join.args["using"]:
            key = (getattr(name, "name", "") or "").casefold()
            have = [c for relation in left_items for c in relation if (c.name or "").casefold() == key]
            there = [c for c in right if (c.name or "").casefold() == key]
            if len(have) != 1 or len(there) != 1:
                continue
            a, b = _family(have[0].type), _family(there[0].type)
            if have[0].type is None or there[0].type is None:
                self.report.undecided += 1
                self.report.unknown.append(UnknownOperand(exp.column(name.name, table=None), None, "join condition"))
                continue
            if a is None or b is None:
                continue
            self.report.decided += 1
            if a != b and {a, b} != {"DATE", "DATETIME"}:
                what = (f"join condition: USING ({name.name}) joins a {have[0].type.sql()} column to a {there[0].type.sql()} column; "
                        "BigQuery has no implicit conversion between them")
                fix = (f"Join with ON and convert one side: ON CAST(left.{name.name} AS {there[0].type.sql()}) = right.{name.name}"
                       f" (or CAST(right.{name.name} AS {have[0].type.sql()}) = left.{name.name}).")
                self.report.findings.append(OperandFinding("incompatible_operands", what, fix))

    def set_operation(self, node: exp.SetOperation) -> None:
        """Branch columns by position whose type is unknown: the typer cannot compare them, so say so."""

        if isinstance(node.parent, exp.SetOperation) or node.args.get("by_name") or node.args.get("on"):
            return
        leaves = []

        def collect(item: exp.Expression) -> None:
            if isinstance(item, exp.SetOperation):
                collect(item.this)
                collect(item.expression)
            else:
                leaves.append(item)

        collect(node)
        selects = [leaf for leaf in leaves if isinstance(leaf, exp.Select)]
        if len(selects) != len(leaves) or len({len(s.expressions) for s in selects}) != 1:
            return
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for s in selects for e in s.expressions):
            return
        for position in range(len(selects[0].expressions)):
            column = [self.operand(s.expressions[position].unalias() if isinstance(s.expressions[position], exp.Alias) else s.expressions[position]) for s in selects]
            if any(o.unresolved for o in column):
                self.undecided(column, "UNION branch")

    # -- signatures

    def signature(self, node: exp.Expression, name: str, nodes: list) -> None:
        operands = [self.operand(n) for n in nodes]
        if any(o is None for o in operands) or len(operands) != 2:
            return
        left, right = operands
        if left.is_null or right.is_null:
            return
        if left.unresolved or right.unresolved:
            self.undecided([left, right], f"{name} argument")
            return
        if not (left.known and right.known):
            return
        self.report.decided += 1
        if self._numeric_signature_missing(name, left, right):
            what = (f"{name} has no signature for ({left.kind.sql()}, {right.kind.sql()}): {left.label} and {right.label}")
            self.report.findings.append(OperandFinding("no_matching_signature", what, self._signature_fix(name, left, right)))

    @staticmethod
    def _kinds(operand: _Operand) -> tuple[str, ...]:
        if operand.literal == "number":
            return _INT_LITERAL_KINDS if operand.kind.kind == "INT64" else _FLOAT_LITERAL_KINDS
        return (operand.kind.kind,)

    def _numeric_signature_missing(self, name: str, left: _Operand, right: _Operand) -> bool:
        if name not in BINARY_NUMERIC:
            return False
        if left.literal == "string" or right.literal == "string":
            return False  # a quoted text may be coerced; not certain
        families = {left.family, right.family}
        if families & {"BOOL", "BYTES", "STRING"}:
            # + - * / DIV MOD POW take numbers (and dates, times, intervals for + and -, handled by the typer); text, bytes and BOOL never
            return True
        if families & {"DATE", "DATETIME", "TIMESTAMP", "TIME"}:
            return False  # date arithmetic has its own signatures (typer)
        table = BINARY_NUMERIC[name]
        return not any((a, b) in table for a in self._kinds(left) for b in self._kinds(right))

    @staticmethod
    def _signature_fix(name: str, left: _Operand, right: _Operand) -> str:
        families = {left.family, right.family}
        if families & {"BOOL", "BYTES", "STRING"}:
            text = left if left.family in ("BOOL", "BYTES", "STRING") else right
            return f"{name} needs numbers: CAST({_short(text.node, 30)} AS INT64) (or NUMERIC / FLOAT64), or SAFE_CAST to get NULL instead of an error."
        return (f"Cast one argument so both have a type {name} accepts (INT64 or NUMERIC for MOD and DIV): "
                f"CAST({_short(left.node, 30)} AS INT64) if the values are whole numbers, or use FLOOR / TRUNC and a float-safe formula.")

    def function(self, node: exp.Func) -> None:
        name = function_name(node)
        if name is None:
            return
        if name in UNARY_NUMERIC:
            arguments = _arguments(node)
            if len(arguments) != 1:
                return
            operand = self.operand(arguments[0])
            if operand is None or operand.is_null or operand.literal is not None:
                return
            if operand.unresolved:
                self.undecided([operand], f"{name} argument")
                return
            if not operand.known:
                return
            self.report.decided += 1
            if operand.family in ("STRING", "BYTES", "BOOL", "DATE", "DATETIME", "TIMESTAMP", "TIME"):
                what = f"{name} has no signature for {operand.kind.sql()}: {operand.label}"
                fix = f"{name} needs a number: CAST({_short(operand.node, 30)} AS FLOAT64) (or INT64 / NUMERIC), or SAFE_CAST to get NULL instead of an error."
                self.report.findings.append(OperandFinding("no_matching_signature", what, fix))
        elif name in BINARY_NUMERIC and not isinstance(node, tuple(_ARITHMETIC)):
            arguments = _arguments(node)
            if len(arguments) == 2:
                self.signature(node, name, arguments)


def _ancestors(node: exp.Expression):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent
