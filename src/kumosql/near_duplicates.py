"""Near-duplicate SELECT logic across a pipeline.

:func:`kumosql.pipeline.Pipeline.duplicate_selects` finds SELECT subtrees
that are exactly the same after normalization. Copied logic usually drifts,
though: one copy gains a filter, another an extra column, a third a different
status literal. This module finds those near-duplicates:

1. every SELECT is taken one query level at a time (each nested CTE body or
   subquery collapses to a token naming its fingerprint, and is compared at
   its own level); levels of at least ``min_nodes`` nodes become a multiset of
   tree shingles: node labels plus parent/child and grandparent/parent/child
   label paths, with literal values kept as separate tokens so a changed
   constant costs little;
2. candidate pairs come from MinHash signatures with locality-sensitive
   banding (or from all pairs, for small pipelines), and every candidate is
   confirmed with the exact multiset Jaccard similarity;
3. confirmed pairs are clustered, and each cluster is compared clause by
   clause against its most central member to report what differs;
4. when the differences have a shape a shared model can absorb, the cluster
   carries a suggested shared SELECT:

   * ``literal_parameters``: identical except for constants, which become
     BigQuery ``@parameters`` (a table function or a templated include);
   * ``extra_filters``: identical except for extra WHERE conjuncts over a
     plain, non-aggregating SELECT; the shared model keeps the common filters
     and each copy applies its extra ones downstream;
   * ``extra_columns``: identical except for projections; the shared model
     selects the union of columns;
   * ``extra_columns_and_filters``: both of the above.

   Anything else is reported as ``mixed``, with its differences but no
   suggested SQL.

Suggestions are candidates, not proven rewrites: check them with
:func:`kumosql.prove_equivalent` or :func:`kumosql.check_rewrite`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import random
import re

from sqlglot import exp

from kumo_sql_prover.canonical import canonical_copy, rename_sources, scope_key, source_names
from .pipeline import _fingerprint, _select_location
from .pipeline_duplicates import _fingerprint_hash, _plain_sql


@dataclass(frozen=True)
class ClauseDifference:
    """One clause-level difference between a variant and its cluster's representative."""

    clause: str  # "columns", "where", "having", "qualify", "from", "joins", "group", ...
    change: str  # "added", "removed" or "changed"
    representative: str | None
    variant: str | None


@dataclass(frozen=True)
class QueryParameter:
    name: str
    type: str
    # The constant each variant uses, keyed by variant fingerprint.
    values: dict[str, str]


@dataclass(frozen=True)
class NearDuplicateVariant:
    """One distinct SELECT (up to normalization) and everywhere it occurs."""

    fingerprint: str
    sql: str
    node_count: int
    occurrences: tuple[tuple[str, str], ...]  # (model, location)
    similarity: float  # multiset Jaccard similarity to the representative
    differences: tuple[ClauseDifference, ...]
    # Filters this variant applies on top of the shared model, if suggested.
    residual_filters: tuple[str, ...] = ()


@dataclass(frozen=True)
class NearDuplicateCluster:
    kind: str
    representative: NearDuplicateVariant
    variants: tuple[NearDuplicateVariant, ...]  # representative first
    shared_sql: str | None
    parameters: tuple[QueryParameter, ...] = ()

    @property
    def min_similarity(self) -> float:
        return min(v.similarity for v in self.variants)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "min_similarity": round(self.min_similarity, 3),
            "variants": [
                {
                    "fingerprint": v.fingerprint,
                    "node_count": v.node_count,
                    "similarity": round(v.similarity, 3),
                    "occurrences": [f"{model} ({location})" for model, location in v.occurrences],
                    "sql": v.sql,
                    "differences": [
                        {
                            "clause": d.clause,
                            "change": d.change,
                            "representative": d.representative,
                            "variant": d.variant,
                        }
                        for d in v.differences
                    ],
                    **({"residual_filters": list(v.residual_filters)} if v.residual_filters else {}),
                }
                for v in self.variants
            ],
            "shared_sql": self.shared_sql,
            "parameters": [
                {"name": p.name, "type": p.type, "values": dict(p.values)} for p in self.parameters
            ],
        }


def find_near_duplicates(
    parsed: dict[str, exp.Expression],
    *,
    min_nodes: int = 12,
    threshold: float = 0.7,
    num_perm: int = 64,
    bands: int = 16,
    exhaustive_limit: int = 200,
    seed: int = 1,
) -> list[NearDuplicateCluster]:
    """Cluster SELECT subtrees whose normalized shingle multisets are similar.

    ``parsed`` maps model keys to parsed queries. Clusters made only of
    exactly identical SELECTs are left to the exact duplicate report. With at
    most ``exhaustive_limit`` distinct SELECTs every pair is compared exactly;
    above that, MinHash banding (``num_perm`` hashes in ``bands`` bands)
    proposes the pairs to compare.
    """

    if num_perm % bands:
        raise ValueError("num_perm must be a multiple of bands")

    variants = _collect_variants(parsed, min_nodes)
    if len(variants) < 2:
        return []

    if len(variants) <= exhaustive_limit:
        candidates = {(i, j) for i in range(len(variants)) for j in range(i + 1, len(variants))}
    else:
        candidates = _lsh_candidates(
            [v.shingles for v in variants], num_perm=num_perm, bands=bands, seed=seed
        )

    similarity: dict[tuple[int, int], float] = {}
    sources_of: dict[int, tuple[frozenset[str], str]] = {}  # per variant: one render, not one per candidate pair
    for i, j in sorted(candidates):
        a, b = variants[i], variants[j]
        small, large = sorted((len(a.shingles), len(b.shingles)))
        if small < threshold * large:  # Jaccard <= |A| / |B|
            continue
        # Same sources first: one cached lookup per variant, against a set intersection per pair
        if not _same_sources(a, b, sources_of, i, j):
            continue
        score = _jaccard(a.shingles, b.shingles)
        if score >= threshold and _has_unnested_pair(a, b):
            similarity[(i, j)] = score

    clusters = _star_clusters(variants, similarity)

    result = []
    for members in clusters:
        # Copies that differ only inside a nested query are reported through
        # that query's own cluster, where the difference is visible.
        if len({_QUERY_TOKEN.sub("<query>", _render(variants[m].canonical_layer)) for m in members}) == 1:
            continue
        result.append(_describe_cluster([variants[m] for m in members], members, similarity))

    return sorted(
        result,
        key=lambda c: (-max(v.node_count for v in c.variants), c.representative.fingerprint),
    )


# ------------------------------------------------------------------ features


@dataclass
class _Variant:
    fingerprint: str
    sql: str
    node_count: int
    select: exp.Select  # first occurrence, used for clause comparison
    layer: exp.Select  # ``select`` with nested queries collapsed to tokens
    canonical_layer: exp.Select  # the same with aliases and operand order canonical, for comparison
    selects: list[exp.Select]
    occurrences: list[tuple[str, str]]
    shingles: frozenset[tuple[str, int]]  # multiset as (shingle, copy number)


def _collect_variants(parsed: dict[str, exp.Expression], min_nodes: int) -> list[_Variant]:
    by_fingerprint: dict[str, _Variant] = {}
    for key in sorted(parsed):
        for select in parsed[key].find_all(exp.Select):
            if sum(1 for _ in select.walk()) < min_nodes:  # its layer is smaller still
                continue
            fingerprint = _fingerprint_hash(select)
            variant = by_fingerprint.get(fingerprint)
            if variant is None:
                layer = _layer(select)
                if sum(1 for _ in layer.walk()) < min_nodes:
                    continue
                canonical_layer = _canonical_or_copy(layer)  # the small layer, not the whole nested query
                shingles = frozenset(
                    (key, n) for key, count in _shingles(canonical_layer).items() for n in range(count)
                )
                variant = by_fingerprint[fingerprint] = _Variant(
                    fingerprint,
                    _plain_sql(select),
                    sum(1 for _ in select.walk()),
                    select,
                    layer,
                    canonical_layer,
                    [],
                    [],
                    shingles,
                )
            variant.selects.append(select)
            variant.occurrences.append((key, _select_location(select)))
    return sorted(by_fingerprint.values(), key=lambda v: v.fingerprint)


def _layer(select: exp.Select, *, canonical: bool = False) -> exp.Select:
    """A copy of one query level, with each nested query replaced by a token.

    The token names the nested query's fingerprint, so a SELECT is compared
    on its own clauses: a thin wrapper does not resemble the CTE it wraps,
    and near-duplicate CTEs are clustered at their own level.
    """

    nested = []

    def visit(node: exp.Expression) -> None:
        for value in node.args.values():
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, exp.Expression):
                    continue
                if isinstance(child, exp.Query) and not isinstance(child, exp.Subquery):
                    nested.append(child)
                else:
                    visit(child)

    visit(select)
    # Copy the level alone: swap each nested query for its token, copy, put the queries back. Copying the
    # whole tree for every level is quadratic in how deeply CTEs nest.
    tokens = [(node, exp.Var(this=f"<query {_fingerprint_hash(node)[:8]}>")) for node in nested]
    for node, token in tokens:
        node.replace(token)
    try:
        copy = select.copy()
    finally:
        for node, token in reversed(tokens):
            token.replace(node)
    copy.meta.pop("kumosql_fingerprint", None)
    copy.meta.pop("kumosql_text", None)
    if canonical:
        try:
            copy = canonical_copy(copy)  # aliases by position, so renamed copies shingle alike
        except Exception:
            pass
    return copy


def _label(node: exp.Expression) -> str:
    if isinstance(node, exp.Identifier):
        return "id:" + node.name.lower()
    if isinstance(node, exp.Literal):
        return "lit:str" if node.is_string else "lit:num"
    if isinstance(node, exp.Anonymous):
        return "fn:" + str(node.name).upper()
    if isinstance(node, exp.Var):
        return "var:" + str(node.this)
    if isinstance(node, exp.Boolean):
        return f"bool:{node.this}"
    if isinstance(node, (exp.DataType,)):
        return "type:" + node.sql(dialect="bigquery").upper()
    return type(node).__name__


def _shingles(layer: exp.Select) -> Counter:
    """Node labels and 2- and 3-node downward label paths, as a multiset."""

    shingles: Counter = Counter()

    def visit(node: exp.Expression, path: tuple[str, ...]) -> None:
        label = _label(node)
        shingles[label] += 1
        if isinstance(node, exp.Literal):
            shingles["val:" + str(node.this)] += 1
        if path:
            shingles[path[-1] + ">" + label] += 1
        if len(path) >= 2:
            shingles[path[-2] + ">" + path[-1] + ">" + label] += 1
        child_path = (*path[-1:], label)
        for value in node.args.values():
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, exp.Expression):
                    visit(child, child_path)

    visit(layer, ())
    return shingles


def _jaccard(a: frozenset, b: frozenset) -> float:
    inter = len(a & b)
    union = len(a) + len(b) - inter
    return inter / union if union else 1.0


_QUERY_TOKEN = re.compile(r"<query [0-9a-f]+>")
_MERSENNE = (1 << 61) - 1


def _lsh_candidates(
    shingle_sets: list[frozenset], *, num_perm: int, bands: int, seed: int
) -> set[tuple[int, int]]:
    rng = random.Random(seed)
    perms = [(rng.randrange(1, _MERSENNE), rng.randrange(0, _MERSENNE)) for _ in range(num_perm)]
    rows = num_perm // bands
    buckets: dict[tuple, list[int]] = defaultdict(list)
    permuted: dict[tuple[str, int], list[int]] = {}  # templated pipelines repeat shingles: hash each once
    for index, shingles in enumerate(shingle_sets):
        columns = []
        for shingle in shingles:
            values = permuted.get(shingle)
            if values is None:
                h = int.from_bytes(hashlib.blake2b(f"{shingle[0]}#{shingle[1]}".encode(), digest_size=8).digest(), "big")
                values = permuted[shingle] = [(a * h + b) % _MERSENNE for a, b in perms]
            columns.append(values)
        signature = list(map(min, zip(*columns))) if columns else [0] * num_perm
        for band in range(bands):
            buckets[(band, *signature[band * rows : (band + 1) * rows])].append(index)
    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                pairs.add((members[x], members[y]))
    return pairs


def _is_ancestor(outer: exp.Expression, inner: exp.Expression) -> bool:
    parent = inner.parent
    while parent is not None:
        if parent is outer:
            return True
        parent = parent.parent
    return False


def _sources_of(variant: "_Variant") -> tuple[frozenset[str], str]:
    """The tables a SELECT reads at its own level (nested queries are compared at theirs).

    A CTE defined outside the SELECT counts by what it contains: the same text over two different
    CTEs is not one logic.
    """

    tables = frozenset(
        ".".join(part for part in (t.catalog, t.db, t.name) if part).lower()
        for t in variant.canonical_layer.find_all(exp.Table)
    )
    return tables, scope_key(variant.select, _render)


def _same_sources(
    a: "_Variant", b: "_Variant", cache: dict[int, tuple[frozenset[str], str]], i: int, j: int
) -> bool:
    """Logic over different tables is a template, not a copy: only a shared source can be shared."""

    for index, variant in ((i, a), (j, b)):
        if index not in cache:
            cache[index] = _sources_of(variant)
    return cache[i] == cache[j]


def _has_unnested_pair(a: _Variant, b: _Variant) -> bool:
    """A SELECT and a thin wrapper around it are not duplicates of each other."""

    return any(
        not _is_ancestor(x, y) and not _is_ancestor(y, x) for x in a.selects for y in b.selects
    )


def _star_clusters(
    variants: list[_Variant], edges: dict[tuple[int, int], float]
) -> list[list[int]]:
    """Group variants around centres, each member similar to its centre.

    Single-linkage clustering would chain A~B~C into one cluster even when A
    and C have little in common. Instead, the variant with the most
    unassigned neighbours (then the highest total similarity, then the most
    occurrences, then the smallest query, so variants read as additions to it) becomes a centre and takes its unassigned neighbours; this
    repeats until no pair is left. Each cluster lists its centre first.
    """

    neighbours: dict[int, dict[int, float]] = defaultdict(dict)
    for (i, j), score in edges.items():
        neighbours[i][j] = score
        neighbours[j][i] = score
    assigned: set[int] = set()
    clusters: list[list[int]] = []
    while True:
        best = None
        for index, near in neighbours.items():
            if index in assigned:
                continue
            free = {other: score for other, score in near.items() if other not in assigned}
            if not free:
                continue
            key = (len(free), sum(free.values()), len(variants[index].occurrences), -variants[index].node_count, -index)
            if best is None or key > best[0]:
                best = (key, index, free)
        if best is None:
            return clusters
        _, centre, free = best
        members = sorted(free, key=lambda other: (-free[other], variants[other].fingerprint))
        clusters.append([centre, *members])
        assigned.update([centre, *members])


# -------------------------------------------------------------- differences


def _render(node: exp.Expression) -> str:
    canonical = node.copy()
    for item in canonical.walk():
        item.comments = None
    try:
        return canonical.sql(dialect="bigquery", normalize=True, normalize_functions="upper", comments=False)
    except AssertionError:
        # A nested query replaced by a token inside ``FROM (...) AS s`` is not a source sqlglot's
        # BigQuery generator can scope; the text is only compared, so any consistent rendering does.
        return canonical.sql(normalize=True, normalize_functions="upper", comments=False)


def _conjuncts(condition: exp.Expression | None) -> list[exp.Expression]:
    if condition is None:
        return []
    if isinstance(condition, (exp.Where, exp.Having, exp.Qualify)):
        condition = condition.this
    if isinstance(condition, exp.Paren) and isinstance(condition.this, exp.And):
        condition = condition.this
    if isinstance(condition, exp.And):
        return [*_conjuncts(condition.this), *_conjuncts(condition.expression)]
    return [condition]


_CONJUNCT_CLAUSES = ("where", "having", "qualify")


def _clause_key(key: str) -> str:
    return key.rstrip("_")  # sqlglot 27+ spells FROM as "from_"


@dataclass
class _Clauses:
    columns: dict[str, tuple[str, exp.Expression]]  # output name -> (sql, node)
    conjuncts: dict[str, dict[str, exp.Expression]]  # clause -> sql -> node
    other: dict[str, str]  # every other clause, rendered whole


def _clauses(select: exp.Select) -> _Clauses:
    columns: dict[str, tuple[str, exp.Expression]] = {}
    for index, projection in enumerate(select.expressions):
        name = (projection.alias_or_name or f"_col{index}").lower()
        columns.setdefault(name, (_render(projection), projection))
    conjuncts = {
        clause: {_render(c): c for c in _conjuncts(select.args.get(clause))}
        for clause in _CONJUNCT_CLAUSES
    }
    other: dict[str, str] = {}
    for key, value in select.args.items():
        if key == "expressions" or key in _CONJUNCT_CLAUSES or value in (None, [], False):
            continue
        items = value if isinstance(value, list) else [value]
        other[_clause_key(key)] = ", ".join(
            _render(item) if isinstance(item, exp.Expression) else str(item) for item in items
        )
    return _Clauses(columns, conjuncts, other)


def _differences(rep: _Clauses, var: _Clauses) -> list[ClauseDifference]:
    diffs: list[ClauseDifference] = []
    for name in [*rep.columns, *(n for n in var.columns if n not in rep.columns)]:
        left = rep.columns.get(name, (None,))[0]
        right = var.columns.get(name, (None,))[0]
        if left == right:
            continue
        change = "added" if left is None else "removed" if right is None else "changed"
        diffs.append(ClauseDifference("columns", change, left, right))
    for clause in _CONJUNCT_CLAUSES:
        left, right = rep.conjuncts[clause], var.conjuncts[clause]
        diffs.extend(ClauseDifference(clause, "removed", sql, None) for sql in left if sql not in right)
        diffs.extend(ClauseDifference(clause, "added", None, sql) for sql in right if sql not in left)
    for clause in sorted({*rep.other, *var.other}):
        left, right = rep.other.get(clause), var.other.get(clause)
        if left != right:
            change = "added" if left is None else "removed" if right is None else "changed"
            diffs.append(ClauseDifference(clause, change, left, right))
    return diffs


# -------------------------------------------------------------- suggestions


def _literal_shape(select: exp.Select) -> tuple[str, list[exp.Literal], exp.Select]:
    """SQL with every literal replaced by a placeholder, and the literals in order."""

    copy = select.copy()
    literals = list(copy.find_all(exp.Literal, bfs=False))
    shape = copy.copy()
    for node in list(shape.find_all(exp.Literal, bfs=False)):
        node.replace(exp.Placeholder())
    return _render(shape), literals, copy


def _parameter_type(literal: exp.Literal) -> str:
    if literal.is_string:
        return "STRING"
    text = str(literal.this).lower()
    return "FLOAT64" if any(ch in text for ch in ".e") else "INT64"


def _parameter_name(literal: exp.Literal, used: set[str]) -> str:
    base = "p"
    parent = literal.parent
    while isinstance(parent, (exp.Paren, exp.Neg, exp.Cast, exp.Tuple)):
        parent = parent.parent
    if isinstance(parent, (exp.Binary, exp.In, exp.Between)):
        column = parent.this if isinstance(parent.this, exp.Column) else parent.find(exp.Column)
        if isinstance(column, exp.Column):
            base = column.name.lower()
    elif isinstance(parent, exp.Limit):
        base = "limit"
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


def _literal_parameters(
    variants: list[_Variant],
) -> tuple[str, tuple[QueryParameter, ...]] | None:
    if len({v.node_count for v in variants}) != 1:
        return None  # replacing constants never changes the tree size
    shapes = [_literal_shape(v.select) for v in variants]
    if len({shape for shape, _, _ in shapes}) != 1:
        return None
    _, rep_literals, rep_copy = shapes[0]
    if any(len(literals) != len(rep_literals) for _, literals, _ in shapes):
        return None
    used: set[str] = set()
    parameters: list[QueryParameter] = []
    for position, literal in enumerate(rep_literals):
        values = {
            variant.fingerprint: literals[position].sql(dialect="bigquery")
            for variant, (_, literals, _) in zip(variants, shapes)
        }
        if len(set(values.values())) == 1:
            continue
        types = {_parameter_type(literals[position]) for _, literals, _ in shapes}
        kind = types.pop() if len(types) == 1 else "FLOAT64" if types <= {"INT64", "FLOAT64"} else "STRING"
        name = _parameter_name(literal, used)
        parameters.append(QueryParameter(name, kind, values))
        literal.replace(exp.Placeholder(this=name))
    if not parameters:
        return None
    sql = _render(rep_copy)
    # sqlglot walks clauses in its own order; list parameters as they read.
    parameters.sort(key=lambda p: re.search(rf"@{p.name}\b", sql).start())
    return sql, tuple(parameters)


def _filters_safe_downstream(select: exp.Select) -> bool:
    """Whether a WHERE conjunct commutes with the rest of this SELECT."""

    if any(select.args.get(key) for key in ("group", "having", "qualify", "distinct", "limit", "offset")):
        return False
    return not any(
        isinstance(node, (exp.AggFunc, exp.Window))
        for projection in select.expressions
        for node in projection.walk()
    )


def _describe_cluster(
    ordered: list[_Variant], indexes: list[int], similarity: dict[tuple[int, int], float]
) -> NearDuplicateCluster:
    """Compare each variant with the first, the cluster's centre."""

    # Every variant is compared, and merged, in the centre's own aliases and with operands in one order.
    names = source_names(ordered[0].select)
    layers = [_clauses(rename_sources(v.canonical_layer.copy(), names)) for v in ordered]
    diffs = [_differences(layers[0], c) for c in layers]
    changed = {d.clause for ds in diffs for d in ds}

    kind = "mixed"
    shared_sql: str | None = None
    parameters: tuple[QueryParameter, ...] = ()
    residuals: list[tuple[str, ...]] = [() for _ in ordered]

    parameterized = _literal_parameters(ordered)
    if parameterized is not None:
        kind = "literal_parameters"
        shared_sql, parameters = parameterized
    elif changed and changed <= {"columns", "where"}:
        clauses = [_clauses(rename_sources(_canonical_or_copy(v.select), names)) for v in ordered]
        suggestion = _merge_columns_and_filters(ordered, clauses, changed)
        if suggestion is not None:
            shared_sql, residuals = suggestion
            kind = {
                frozenset({"columns"}): "extra_columns",
                frozenset({"where"}): "extra_filters",
            }.get(frozenset(changed), "extra_columns_and_filters")

    described = tuple(
        NearDuplicateVariant(
            fingerprint=variant.fingerprint,
            sql=variant.sql,
            node_count=variant.node_count,
            occurrences=tuple(variant.occurrences),
            similarity=similarity.get((min(indexes[0], index), max(indexes[0], index)), 1.0),
            differences=tuple(diff),
            residual_filters=residual,
        )
        for variant, index, diff, residual in zip(ordered, indexes, diffs, residuals)
    )
    return NearDuplicateCluster(kind, described[0], described, shared_sql, parameters)


def _canonical_or_copy(select: exp.Select) -> exp.Select:
    try:
        return canonical_copy(select)
    except Exception:
        return select.copy()


def _merge_columns_and_filters(
    variants: list[_Variant], clauses: list[_Clauses], changed: set[str]
) -> tuple[str, list[tuple[str, ...]]] | None:
    """A shared SELECT with the union of columns and the common filters."""

    rep = variants[0].select
    columns: dict[str, tuple[str, exp.Expression]] = {}
    for clause in clauses:
        for name, (sql, node) in clause.columns.items():
            if name in columns and columns[name][0] != sql:
                return None  # the same output name computed two ways
            columns.setdefault(name, (sql, node))
    if "columns" in changed and variants[0].select.args.get("distinct"):
        return None  # extra columns change which rows DISTINCT keeps

    residuals: list[tuple[str, ...]] = [() for _ in variants]
    common: list[exp.Expression] = list(clauses[0].conjuncts["where"].values())
    if "where" in changed:
        if not _filters_safe_downstream(rep):
            return None
        shared = set.intersection(*(set(c.conjuncts["where"]) for c in clauses))
        common = [node for sql, node in clauses[0].conjuncts["where"].items() if sql in shared]
        for position, clause in enumerate(clauses):
            extra = [node for sql, node in clause.conjuncts["where"].items() if sql not in shared]
            for node in extra:
                for column in node.find_all(exp.Column):
                    name = column.name.lower()
                    if name not in columns:
                        columns[name] = (_render(column), exp.column(column.name, table=column.table or None))
                        continue
                    output = columns[name][1].unalias()
                    if (
                        not isinstance(output, exp.Column)
                        or output.name.lower() != name
                        or (output.table and column.table and output.table.lower() != column.table.lower())
                    ):
                        return None  # the output name hides a different expression
            residuals[position] = tuple(_render(_unqualify(node)) for node in extra)

    shared_select = rep.copy()
    shared_select.set("expressions", [node.copy() for _, node in columns.values()])
    shared_select.set("where", exp.Where(this=exp.and_(*(n.copy() for n in common))) if common else None)
    return _render(shared_select), residuals


def _unqualify(condition: exp.Expression) -> exp.Expression:
    copy = condition.copy()
    for column in copy.find_all(exp.Column):
        column.set("table", None)
    return copy
