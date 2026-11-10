"""Duplicate-SELECT detection across a pipeline."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import re

from sqlglot import exp

from kumo_sql_prover.canonical import canonical_copy, scope_key, visible_ctes
from .pipeline_types import DuplicateGroup, DuplicateOccurrence

def _select_location(select: exp.Expression) -> str:
    parent = select.parent
    if isinstance(parent, exp.CTE):
        return f"cte:{parent.alias_or_name}"
    if isinstance(parent, exp.Subquery):
        return f"subquery:{parent.alias_or_name or '<anonymous>'}"
    if parent is None:
        return "query"
    return f"nested:{type(parent).__name__.lower()}"


def _plain_sql(node: exp.Expression, outside_ctes: frozenset[str] | None = None) -> str:
    """Normalized text: identifiers lower-cased, except the names of physical tables.

    BigQuery column names, aliases and CTE names have no case, but ``p.d.Orders`` and ``p.d.orders``
    are different tables, and so are a bare ``Orders`` and ``orders`` that a default catalog resolves.
    A one-part table name is lower-cased only when a CTE of that name is visible at that point: one
    defined inside ``node`` or in ``outside_ctes`` (the lower-case names visible around ``node``, by
    default those of its parents).
    """

    def cased_table_parts(tree: exp.Expression, outside: frozenset[str]) -> set[int]:
        keep = set()
        for table in tree.find_all(exp.Table):
            if not any(isinstance(p, exp.Identifier) and not p.quoted and p.name != p.name.lower() for p in table.parts):
                continue
            if not (table.args.get("db") or table.args.get("catalog")):
                bound = {name.lower() for name in visible_ctes(table)} | outside
                if table.name.lower() in bound:
                    continue
            keep.update(id(part) for part in table.parts if isinstance(part, exp.Identifier) and not part.quoted and part.name != part.name.lower())
        return keep

    if outside_ctes is None:
        outside_ctes = frozenset(name.lower() for name in visible_ctes(node))
    if not cased_table_parts(node, outside_ctes):  # nothing that lower-casing would change
        return node.sql(dialect="bigquery", normalize=True, normalize_functions="upper", comments=False)
    copy = node.copy()
    keep = cased_table_parts(copy, outside_ctes)
    for identifier in copy.find_all(exp.Identifier):
        if id(identifier) not in keep and not identifier.quoted:
            identifier.set("this", identifier.this.lower())
    return copy.sql(dialect="bigquery", normalize_functions="upper", comments=False)


def _outside_ctes(select: exp.Expression) -> frozenset[str]:
    return frozenset(name.lower() for name in visible_ctes(select))


def _remembered(select: exp.Expression, key: str, compute):
    """Compute once per parsed select: exact, near-duplicate and repeated-work analysis all ask again.

    The result lives in the node's own ``meta``, so it goes away with the parsed tree, and it is only
    kept for a select that has not been changed since (callers fingerprint the trees they parsed, and
    copies they edit are fingerprinted before the edit).
    """

    meta = select.meta
    if key not in meta:
        meta[key] = compute()
    return meta[key]


def _fingerprint_hash(select: exp.Expression) -> str:
    """A hash of the select's canonical text (aliases, conjunct order and the like ignored).

    Rendering a select costs a deep copy and a full render, so callers that only need the hash use this
    rather than :func:`_fingerprint`, which also renders the select's own text.
    """

    def compute() -> str:
        try:
            canonical = _plain_sql(canonical_copy(select), _outside_ctes(select)) + scope_key(select, _plain_sql)
        except Exception:  # a shape the canonicalizer cannot follow keeps its literal text
            canonical = _plain_sql(select)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

    return _remembered(select, "kumosql_fingerprint", compute)


def _fingerprint(select: exp.Expression) -> tuple[str, str]:
    """The canonical fingerprint of a select and its own normalized text."""

    return _fingerprint_hash(select), _remembered(select, "kumosql_text", lambda: _plain_sql(select, _outside_ctes(select)))


_TOKEN = re.compile(r"__sqlx_token_(\d+)__")


def _masked_suffix(select: exp.Expression, masked: tuple[str, ...]) -> str:
    """The Dataform expressions behind the loader's placeholders in ``select``.

    Placeholders are numbered per model, so ``__sqlx_token_000__`` stands for different expressions in
    different models; two copies are the same code only when the expressions are the same too.
    """

    found = _TOKEN.findall(_fingerprint(select)[1])
    if not found:
        return ""
    return "|".join(masked[int(n)] if int(n) < len(masked) else f"?{n}" for n in found)


def _find_duplicates(
    parsed: dict[str, exp.Expression], *, min_nodes: int, masked: dict[str, tuple[str, ...]] | None = None
) -> list[DuplicateGroup]:
    groups: dict[str, list[tuple[str, exp.Expression]]] = defaultdict(list)
    sql_of: dict[str, str] = {}
    size_of: dict[str, int] = {}
    fingerprint_of: dict[int, str] = {}

    for key, query in parsed.items():
        for select in query.find_all(exp.Select):
            size = sum(1 for _ in select.walk())
            if size < min_nodes:
                continue
            fingerprint, sql = _fingerprint(select)
            suffix = _masked_suffix(select, (masked or {}).get(key, ()))
            if suffix:  # a different identity, which shared-logic proposals cannot find a copy by
                fingerprint = hashlib.sha1(f"{fingerprint}|{suffix}".encode("utf-8")).hexdigest()[:16]
            groups[fingerprint].append((key, select))
            sql_of[fingerprint] = sql
            size_of[fingerprint] = size
            fingerprint_of[id(select)] = fingerprint

    duplicated = {fp for fp, members in groups.items() if len(members) > 1}

    def enclosing_duplicates(select: exp.Expression) -> set[str]:
        found: set[str] = set()
        parent = select.parent
        while parent is not None:
            if isinstance(parent, exp.Select) and fingerprint_of.get(id(parent)) in duplicated:
                found.add(fingerprint_of[id(parent)])
            parent = parent.parent
        return found

    result = []
    for fingerprint in duplicated:
        members = groups[fingerprint]
        # Reported through one larger duplicate that holds every copy. Copies nested in different
        # larger duplicates are what links those duplicates to each other, so the group stays.
        enclosing = [enclosing_duplicates(select) for _, select in members]
        if set.intersection(*enclosing):
            continue
        result.append(
            DuplicateGroup(
                fingerprint=fingerprint,
                node_count=size_of[fingerprint],
                sql=sql_of[fingerprint],
                occurrences=tuple(
                    DuplicateOccurrence(key, _select_location(select)) for key, select in members
                ),
            )
        )
    return sorted(result, key=lambda group: (-group.node_count, group.fingerprint))
