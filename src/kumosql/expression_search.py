"""Find where a SQL expression, such as a CASE ladder, is computed across a project.

The answer to "was this CASE ladder copied?" is a count, the models, and what differs between the copies. An
:class:`ExpressionIndex` holds, for every model the project could read, a fingerprint of each computation inside its
select expressions, join conditions, WHERE, HAVING, QUALIFY and GROUP BY clauses (and every sub-expression of those,
such as a CASE inside ``COALESCE(...)`` or an aggregate's argument). It is built once from the parsed trees the
:class:`~kumosql.pipeline.Pipeline` already holds and then answers any number of questions without reparsing.

Normalization (every step keeps the meaning; the repository's canonicalizer does the rest):

* qualifiers that name a source of the query (``o.code``) are dropped, aliases and case do not matter;
* ``CASE x WHEN 'A' THEN 1`` is ``CASE WHEN x = 'A' THEN 1``, a missing ``ELSE`` is ``ELSE NULL``, redundant
  parentheses go, and ``kumo_sql_prover.canonical`` orders comparisons, ``AND`` / ``OR`` chains and ``IN`` lists;
* a ladder whose branches all compare one expression with constants (``x = 'A'``, ``x IN ('A', 'B')``,
  ``x IS NULL``) cannot have two true branches at once, so its branch order does not matter: it becomes a mapping
  from each constant to its result. Any other ladder keeps its order, because the first true branch wins.

Copies are grouped into *variants*: the same ladder (the same set of branch conditions) with different results,
default or order, or the same expression with different literals. A ladder with some of the branches of yours, or with
more, is reported separately (``partial``, ``extends``) and never as equal.

Everything is offline. Nothing here subclasses a sqlglot class.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from kumo_sql_prover.ast_utils import quiet_parser
from kumo_sql_prover.canonical import canonical_copy
from .pipeline_duplicates import _select_location

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .pipeline_types import Model

#: an expression smaller than this many nodes (a column, a literal, ``x``) is not a computation worth searching for
MIN_NODES = 3
#: text kept per indexed computation, for showing a difference; larger ones are matched by hash only
MAX_TEXT = 6000
#: version of the saved form of the index; bump it when normalization or the rows change, so a saved index is not reused
FORMAT = "expression-index-1"
#: the most ladder branches compared one by one (a larger ladder is compared by its signature only)
MAX_BRANCHES = 400

_TOKEN = re.compile(r"__sqlx_token_(\d+)__")
_INDEXED = (exp.Func, exp.Binary, exp.Unary, exp.Predicate, exp.Window, exp.Bracket, exp.Dot)
_CONSTANT = (exp.Literal, exp.Boolean)


class ExpressionError(ValueError):
    """The text is not an expression this search can use; the message says why."""


# ------------------------------------------------------------------ normalization


def _clause_sql(node: exp.Expression) -> str:
    return node.sql(dialect="bigquery", normalize=True, normalize_functions="upper", comments=False)


def _strip_qualifiers(tree: exp.Expression, is_source) -> None:
    """Drop the leading part of a column's qualifier when it names a source (``o.code`` is ``code``).

    Any remaining parts stay: they are a path into a struct column (``payload.kind``).
    """

    for column in list(tree.find_all(exp.Column)):
        parts = [column.args.get(name) for name in ("catalog", "db", "table")]
        parts = [part for part in parts if part is not None]
        if parts and is_source(parts[0].name.lower(), len(parts)):
            parts = parts[1:]
            for name, part in zip(("table", "db", "catalog"), (parts[::-1] + [None, None, None])):
                column.set(name, part)


def _tidy(tree: exp.Expression) -> exp.Expression:
    """Parentheses that cannot change the meaning go, a simple CASE becomes a searched one, ``ELSE NULL`` goes."""

    for paren in list(tree.find_all(exp.Paren)):
        parent, inner = paren.parent, paren.this
        atomic = not isinstance(inner, (exp.Binary, exp.Unary, exp.Predicate, exp.Case, exp.Connector))
        container = isinstance(parent, (exp.Func, exp.Select, exp.Tuple, exp.Alias, exp.In)) or parent is None
        if parent is not None and (atomic or container) and not isinstance(parent, (exp.Binary, exp.Unary)):
            paren.replace(inner)
    for case in list(tree.find_all(exp.Case)):
        subject = case.args.get("this")
        if subject is not None:
            for branch in case.args.get("ifs") or ():
                branch.set("this", exp.EQ(this=subject.copy(), expression=branch.this))
            case.set("this", None)
        default = case.args.get("default")
        if isinstance(default, exp.Null):
            case.set("default", None)
    return tree


def canonical_root(node: exp.Expression, is_source) -> exp.Expression:
    """A normalized copy of ``node``. Raises when the canonicalizer cannot follow the expression."""

    copy = node.copy()
    _strip_qualifiers(copy, is_source)
    copy = _tidy(copy)
    wrapper = exp.Select(expressions=[copy])
    canon = canonical_copy(wrapper).expressions[0]
    wrapper = exp.Select(expressions=[canon])
    return _tidy(wrapper).expressions[0]


def _nodes(root: exp.Expression) -> list[exp.Expression]:
    """The nodes of an expression in a fixed order, not entering a nested query (those are searched as their own selects)."""

    found: list[exp.Expression] = []
    stack = [root]
    while stack:
        node = stack.pop()
        found.append(node)
        children = [child for child in node.iter_expressions() if not isinstance(child, exp.Query)]
        stack.extend(reversed(children))
    return found


def _sizes(root: exp.Expression, nodes: list[exp.Expression]) -> list[int]:
    """The node count under each node of ``nodes`` (which is in the order :func:`_nodes` returns)."""

    index = {id(node): i for i, node in enumerate(nodes)}
    sizes = [1] * len(nodes)
    for i in range(len(nodes) - 1, -1, -1):
        parent = nodes[i].parent
        j = index.get(id(parent)) if parent is not None else None
        if j is not None and j != i:
            sizes[j] += sizes[i]
    return sizes


def _masked(root: exp.Expression) -> exp.Expression:
    copy = root.copy()
    for literal in list(copy.find_all(exp.Literal)):
        if literal is copy:
            continue
        literal.replace(exp.Var(this="?"))
    return copy


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _resolve(text: str, masked: tuple[str, ...]) -> str:
    """The Dataform expressions behind the loader's placeholders: two models' ``__sqlx_token_000__`` are not the same code."""

    if "__sqlx_token_" not in text:
        return text
    return _TOKEN.sub(lambda m: "${" + (masked[int(m.group(1))] if int(m.group(1)) < len(masked) else "?") + "}", text)


# --------------------------------------------------------------------- ladders


@dataclass(frozen=True)
class Branch:
    cond: str  #: normalized condition; what two ladders are matched by
    result: str
    show: str  #: how an answer names the branch: the constant for a mapping, else the condition


@dataclass(frozen=True)
class Ladder:
    branches: tuple[Branch, ...]
    default: str
    #: the compared expression when every branch compares it with constants (order then does not matter)
    subject: str | None
    signature: str = ""

    @property
    def conditions(self) -> frozenset[str]:
        return frozenset(branch.cond for branch in self.branches)


def _constant(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return _clause_sql(node)
    return _clause_sql(node) if isinstance(node, _CONSTANT) else None


def _keys(cond: exp.Expression, text) -> tuple[str, list[str]] | None:
    """``(subject, constants)`` for ``x = c``, ``x IN (c, ...)``, ``x IS NULL`` and an ``OR`` of those over one ``x``."""

    if isinstance(cond, exp.EQ):
        left, right = cond.this, cond.expression
        if _constant(left) is not None and _constant(right) is None:
            left, right = right, left
        value = _constant(right)
        return (text(left), [value]) if value is not None and not isinstance(left, _CONSTANT) else None
    if isinstance(cond, exp.In) and not cond.args.get("query") and not cond.args.get("unnest") and not cond.args.get("is_global"):
        values = [_constant(item) for item in cond.expressions]
        return (text(cond.this), values) if values and all(v is not None for v in values) else None  # type: ignore[arg-type]
    if isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null):
        return text(cond.this), ["NULL"]
    if isinstance(cond, exp.Or):
        left, right = _keys(cond.this, text), _keys(cond.expression, text)
        if left and right and left[0] == right[0]:
            return left[0], left[1] + right[1]
    return None


def ladder_of(case: exp.Case, masked: tuple[str, ...] = ()) -> Ladder | None:
    """The branches of a normalized CASE, or ``None`` for one this search cannot compare (huge, or no branches)."""

    ifs = case.args.get("ifs") or []
    if not ifs or len(ifs) > MAX_BRANCHES:
        return None

    def text(node: exp.Expression) -> str:
        return _resolve(_clause_sql(node), masked)

    default = text(case.args["default"]) if case.args.get("default") is not None else "NULL"
    parsed = [_keys(branch.this, text) for branch in ifs]
    subjects = {p[0] for p in parsed if p}
    branches: list[Branch] = []
    seen: set[str] = set()
    if all(parsed) and len(subjects) == 1:
        subject = next(iter(subjects))
        for branch, found in zip(ifs, parsed):
            result = text(branch.args["true"])
            for key in found[1]:  # type: ignore[index]
                if key in seen:
                    continue  # the first true branch wins: a later one for the same constant is dead
                seen.add(key)
                cond = f"{subject} IS NULL" if key == "NULL" else f"{subject} = {key}"
                branches.append(Branch(cond, result, key))
        branches.sort(key=lambda b: b.cond)
        signature = "map|" + subject + "|" + "|".join(f"{b.cond}>{b.result}" for b in branches) + "|" + default
        return Ladder(tuple(branches), default, subject, _digest(signature))
    for branch in ifs:
        cond = text(branch.this)
        if cond in seen:
            continue
        seen.add(cond)
        branches.append(Branch(cond, text(branch.args["true"]), cond))
    signature = "seq|" + "|".join(f"{b.cond}>{b.result}" for b in branches) + "|" + default
    return Ladder(tuple(branches), default, None, _digest(signature))


def _show(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def ladder_difference(mine: Ladder, other: Ladder, other_name: str) -> list[str]:
    """What ``mine`` does differently from ``other``, as short phrases (``'GOLD' maps to NULL (30 in variant 1)``)."""

    verb = "maps to" if mine.subject is not None else "gives"
    theirs = {b.cond: b for b in other.branches}
    ours = {b.cond: b for b in mine.branches}
    found = []
    for branch in mine.branches:
        match = theirs.get(branch.cond)
        if match is not None and match.result != branch.result:
            found.append(f"{_show(branch.show)} {verb} {_show(branch.result)} ({_show(match.result)} in {other_name})")
    only_mine = [b for b in mine.branches if b.cond not in theirs]
    only_theirs = [b for b in other.branches if b.cond not in ours]
    found += [f"has {_show(b.show)} -> {_show(b.result)}" for b in only_mine]
    found += [f"lacks {_show(b.show)}" for b in only_theirs]
    if mine.default != other.default:
        found.append(f"else {_show(mine.default)} ({_show(other.default)} in {other_name})")
    if mine.subject is None and not only_mine and not only_theirs and [b.cond for b in mine.branches] != [b.cond for b in other.branches]:
        found.append("branch order differs and it matters here: the conditions overlap, the first true one wins")
    return found


def relation(query: Ladder, other: Ladder) -> str | None:
    """``same`` (the same branch conditions), ``partial`` (a strict sub-ladder of ``query``), ``extends`` (``query`` plus more), ``overlap`` or ``None``."""

    mine, theirs = query.conditions, other.conditions
    if mine == theirs:
        return "same"
    shared = mine & theirs
    if len(shared) < 2:
        return None
    if theirs < mine:
        return "partial"
    if theirs > mine:
        return "extends"
    return "overlap" if len(shared) * 2 >= len(mine | theirs) else None


# ---------------------------------------------------------------------- the index


@dataclass(slots=True)
class Occurrence:
    model: str
    #: ``column n``, ``WHERE``, ``JOIN ON``, ``HAVING``, ``QUALIFY`` or ``GROUP BY``, with ``in cte x`` for a nested select
    place: str
    exact: str
    shape: str
    text: str | None
    ladder: Ladder | None = None


@dataclass
class ExpressionIndex:
    exact: dict[str, list[Occurrence]] = field(default_factory=dict)
    shape: dict[str, list[Occurrence]] = field(default_factory=dict)
    ladders: list[Occurrence] = field(default_factory=list)
    #: models whose SQL could not be read, with the reason code
    unread: dict[str, str] = field(default_factory=dict)
    #: models with other statements (pre and post operations, an incremental branch) that are not searched
    scripts: list[str] = field(default_factory=list)
    models_searched: int = 0
    #: clause roots the canonicalizer could not follow: they match only when written the same
    not_normalized: int = 0
    aliases: set[str] = field(default_factory=set)
    columns: set[str] = field(default_factory=set)
    #: each readable model's part, as plain data (see :func:`model_part`); what :meth:`to_json` saves
    parts: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        """The index as JSON-serialisable data keyed by model, each part tagged with the content hash of its model."""

        return {"format": FORMAT, "models": self.parts}

    def is_query_source(self, qualifier: str, parts: int) -> bool:
        """Whether a qualifier in the asked-for expression names a source rather than a struct column."""

        return qualifier in self.aliases or (parts == 1 and qualifier not in self.columns)


def _sources(select: exp.Select) -> set[str]:
    names = set()
    from_ = select.args.get("from_") or select.args.get("from")
    nodes = ([from_.this] if from_ is not None else []) + [join.this for join in select.args.get("joins") or ()]
    for node in nodes:
        if node.alias_or_name:
            names.add(node.alias_or_name.lower())
        if isinstance(node, exp.Table) and node.name:
            names.add(node.name.lower())
        alias = node.args.get("alias")
        if alias is not None:
            names.update(column.name.lower() for column in alias.args.get("columns") or ())
    return names


def _roots(select: exp.Select):
    """``(place, expression)`` for every clause of ``select`` that holds a computation."""

    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
            continue
        name = projection.alias_or_name
        yield (f"column {name}" if name else "select expression"), inner
    for join in select.args.get("joins") or ():
        if join.args.get("on") is not None:
            yield "JOIN ON", join.args["on"]
    for label, key in (("WHERE", "where"), ("HAVING", "having"), ("QUALIFY", "qualify")):
        if select.args.get(key) is not None:
            yield label, select.args[key].this
    group = select.args.get("group")
    if group is not None:
        for item in group.expressions:
            yield "GROUP BY", item


def _scope_note(select: exp.Select) -> str:
    where = _select_location(select)
    if where == "query":
        return ""
    kind, _, name = where.partition(":")
    return {"cte": f" in cte {name}", "subquery": f" in subquery {name}"}.get(kind, " in a nested query")


def content_hash(model: "Model") -> str:
    """What a model's part of the index depends on: its SQL and the Dataform expressions behind its placeholders."""

    return _digest("\0".join([FORMAT, model.sql or "", *model.masked_expressions]))


def _ladder_json(ladder: Ladder) -> dict:
    return {"b": [[b.cond, b.result, b.show] for b in ladder.branches], "d": ladder.default, "s": ladder.subject, "g": ladder.signature}


def _ladder_from_json(data: dict) -> Ladder:
    return Ladder(tuple(Branch(*row) for row in data["b"]), data["d"], data["s"], data["g"])


def model_part(model: "Model", tree: exp.Expression, memo: dict | None = None) -> dict:
    """One model's share of the index as plain JSON data: ``hash``, ``aliases``, ``not_normalized`` and ``rows``.

    A row is ``[place, exact hash, shape hash, canonical text or null, ladder or null]``. The part depends only on the
    model's own SQL (see :func:`content_hash`), so a saved part can be reused for as long as that hash is unchanged.
    ``memo`` (optional, shared by the models of one build) remembers the rows of a clause already seen: copied code is
    normalized once.
    """

    sources: set[str] = set()
    for select in tree.find_all(exp.Select):
        sources |= _sources(select)
    part: dict = {"hash": content_hash(model), "aliases": sorted(sources), "not_normalized": 0, "rows": []}
    for select in tree.find_all(exp.Select):
        note = _scope_note(select)
        for place, root in _roots(select):
            _index_root(part, place + note, root, sources, model.masked_expressions, memo)
    return part


def build_index(pipeline: "Pipeline", saved: dict | None = None) -> ExpressionIndex:
    """Fingerprint every computation of every readable model of ``pipeline`` (once; the parsed trees are reused).

    ``saved`` is an earlier :meth:`ExpressionIndex.to_json`: a model whose content hash is unchanged takes its part from
    there instead of being normalized again. Nothing is read from or written to disk here.
    """

    index = ExpressionIndex()
    parsed = pipeline._analyse().parsed
    memo: dict = {}
    old = saved.get("models", {}) if isinstance(saved, dict) and saved.get("format") == FORMAT else {}
    for key, model in pipeline.models.items():
        if model.scripts and model.kind not in ("assertion",):
            index.scripts.append(key)
        if key not in parsed:
            if model.sql and model.sql.strip():
                index.unread[key] = "unparsed_model"
            continue
        part = old.get(key)
        if not isinstance(part, dict) or part.get("hash") != content_hash(model):
            part = model_part(model, parsed[key], memo)
        index.parts[key] = part
        index.models_searched += 1
        index.columns.update(name.lower() for name in pipeline.output_columns(key))
        index.aliases.update(part["aliases"])
        index.not_normalized += part["not_normalized"]
        for place, exact, shape, text, ladder in part["rows"]:
            occurrence = Occurrence(key, place, exact, shape, text, _ladder_from_json(ladder) if ladder else None)
            index.exact.setdefault(exact, []).append(occurrence)
            index.shape.setdefault(shape, []).append(occurrence)
            if occurrence.ladder is not None:
                index.ladders.append(occurrence)
    for schema in pipeline.source_schema.values():
        index.columns.update(name.lower() for name in schema)
    return index


def _index_root(part: dict, place: str, root: exp.Expression, sources: set[str], masked: tuple[str, ...], memo: dict | None = None) -> None:
    if sum(1 for _ in _nodes(root)) < MIN_NODES and not isinstance(root, exp.Func):
        return
    memo_key = None
    if memo is not None:
        memo_key = (root.sql(dialect="bigquery"), tuple(sorted(sources)), masked)
        if memo_key in memo:
            rows, failed = memo[memo_key]
            part["not_normalized"] += failed
            part["rows"].extend([place, *row] for row in rows)
            return
    failed = 0
    try:
        canon = canonical_root(root, lambda name, parts: name in sources)
    except Exception:  # noqa: BLE001 - a shape the canonicalizer cannot follow keeps its own text and matches only itself
        failed = 1
        copy = root.copy()
        _strip_qualifiers(copy, lambda name, parts: name in sources)
        canon = copy
    rows = _root_rows(canon, masked)
    if memo_key is not None:
        memo[memo_key] = (rows, failed)
    part["not_normalized"] += failed
    part["rows"].extend([place, *row] for row in rows)


def _root_rows(canon: exp.Expression, masked: tuple[str, ...]) -> list[list]:
    """``[exact, shape, text, ladder]`` for every computation inside the normalized clause expression ``canon``."""

    nodes = _nodes(canon)
    if len(nodes) < MIN_NODES:
        return []
    sizes = _sizes(canon, nodes)
    shapes = _nodes(_masked(canon))
    rows = []
    for node, shaped, size in zip(nodes, shapes, sizes):
        if size < MIN_NODES or not isinstance(node, _INDEXED) or isinstance(node, exp.Paren):
            continue
        text = _resolve(_clause_sql(node), masked)
        shape = _resolve(_clause_sql(shaped), masked)
        ladder = ladder_of(node, masked) if isinstance(node, exp.Case) else None
        rows.append([_digest(text), _digest(shape), text if len(text) <= MAX_TEXT else None, _ladder_json(ladder) if ladder else None])
    return rows


# ---------------------------------------------------------------------- asking


@dataclass
class Query:
    text: str
    exact: str
    shape: str
    ladder: Ladder | None
    canonical: str
    nested_case: bool


def prepare(index: ExpressionIndex, text: str) -> Query:
    """Parse and normalize the expression a person typed; raises :class:`ExpressionError` when it cannot be searched."""

    try:
        with quiet_parser():
            tree = sqlglot.parse_one(text, read="bigquery")
    except Exception as exc:  # noqa: BLE001 - sqlglot raises several kinds of error for bad text
        raise ExpressionError("the expression could not be parsed as BigQuery SQL") from exc
    if tree is None:
        raise ExpressionError("the expression is empty")
    if isinstance(tree, exp.Alias):
        tree = tree.this
    while isinstance(tree, exp.Paren):
        tree = tree.this
    if isinstance(tree, (exp.Query, exp.DDL, exp.Command)) or not isinstance(tree, exp.Expression):
        raise ExpressionError("this is a statement, not an expression; use sql= to look for a table that a query would build")
    if len(_nodes(tree)) < MIN_NODES or not isinstance(tree, _INDEXED):
        raise ExpressionError("this is a bare column or constant, too small to search for; describe it with find_tables instead")
    try:
        canon = canonical_root(tree, index.is_query_source)
    except Exception as exc:  # noqa: BLE001
        raise ExpressionError("the expression could not be normalized, so it cannot be compared") from exc
    shape = _nodes(_masked(canon))[0]
    ladder = ladder_of(canon) if isinstance(canon, exp.Case) else None
    nested = not isinstance(canon, exp.Case) and canon.find(exp.Case) is not None
    canonical = _clause_sql(canon)
    return Query(text, _digest(canonical), _digest(_clause_sql(shape)), ladder, canonical, nested)


@dataclass
class Group:
    """Copies that are the same after normalization."""

    key: str
    copies: list[Occurrence]
    ladder: Ladder | None
    text: str | None
    same_as_yours: bool = False
    relation: str = "same"
    differs: list[str] = field(default_factory=list)

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys(o.model for o in self.copies))


@dataclass
class Result:
    variants: list[Group]
    others: list[Group]  #: ladders that are a sub-ladder of yours, extend it or overlap it
    query: Query


def search(index: ExpressionIndex, query: Query) -> Result:
    """Every copy of the expression, grouped into variants, with what differs, and the related ladders."""

    groups: dict[str, Group] = {}
    others: dict[tuple[str, str], Group] = {}
    if query.ladder is not None:
        for occurrence in index.ladders:
            kind = relation(query.ladder, occurrence.ladder)  # type: ignore[arg-type]
            if kind is None:
                continue
            ladder = occurrence.ladder
            if kind == "same":
                group = groups.setdefault(ladder.signature, Group(ladder.signature, [], ladder, occurrence.text, ladder.signature == query.ladder.signature))
            else:
                group = others.setdefault((kind, ladder.signature), Group(ladder.signature, [], ladder, occurrence.text, False, kind))
            group.copies.append(occurrence)
    else:
        for occurrence in index.shape.get(query.shape, ()):
            group = groups.setdefault(occurrence.exact, Group(occurrence.exact, [], None, occurrence.text, occurrence.exact == query.exact))
            group.copies.append(occurrence)
    variants = sorted(groups.values(), key=lambda g: (-len(g.copies), not g.same_as_yours, g.key))
    _describe(variants, query)
    rest = sorted(others.values(), key=lambda g: ({"partial": 0, "extends": 1, "overlap": 2}[g.relation], -len(g.copies), g.key))
    for group in rest:
        group.differs = _against_query(group, query)
    return Result(variants, rest, query)


def _distance(a: Group, b: Group) -> list[str]:
    if a.ladder is not None and b.ladder is not None:
        return ladder_difference(a.ladder, b.ladder, "the other")
    return _literal_difference(a.text, b.text, "the other")


def _describe(variants: list[Group], query: Query) -> None:
    """Each variant's difference against its closest other variant (the one with the fewest differences, then the most copies)."""

    if len(variants) < 2:
        return
    for number, group in enumerate(variants, 1):
        best: tuple[int, int, int] | None = None
        pick = 0
        for other_number, other in enumerate(variants, 1):
            if other is group:
                continue
            distance = len(_distance(group, other))
            rank = (distance, other_number, 0)
            if best is None or rank < best:
                best, pick = rank, other_number
        other = variants[pick - 1]
        if group.ladder is not None and other.ladder is not None:
            group.differs = ladder_difference(group.ladder, other.ladder, f"variant {pick}")
        else:
            group.differs = _literal_difference(group.text, other.text, f"variant {pick}")
        group.differs.insert(0, f"vs variant {pick}")


def _against_query(group: Group, query: Query) -> list[str]:
    assert group.ladder is not None and query.ladder is not None
    found = ladder_difference(group.ladder, query.ladder, "yours")
    return found


def _literals(text: str | None) -> list[str] | None:
    if text is None:
        return None
    try:
        with quiet_parser():
            tree = sqlglot.parse_one(text, read="bigquery")
    except Exception:  # noqa: BLE001
        return None
    return [_clause_sql(node) for node in _nodes(tree) if isinstance(node, exp.Literal)]


def _literal_difference(mine: str | None, other: str | None, other_name: str) -> list[str]:
    ours, theirs = _literals(mine), _literals(other)
    if ours is None or theirs is None or len(ours) != len(theirs):
        return ["could not show what differs (a templated or very large expression)"]
    return [f"literal {_show(a)} ({_show(b)} in {other_name})" for a, b in zip(ours, theirs) if a != b]
