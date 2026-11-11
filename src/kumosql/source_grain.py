"""The grain a project, its docs or its Malloy models state for a table.

A warehouse table (a Dataform declaration, or a table no file mentions) has no query to read a grain from. Something else may
still say which columns identify a row. Each source is a *claim* that keeps where it came from, in this order of trust (the
first that exists for a table wins; with several keys at one level the shortest stands):

1. **config**: a declaration's ``assertions: { uniqueKey: [...] }`` (or ``uniqueKeys``), or a ``uniqueKey`` list in ``declare({...})``
   (read by the loader into ``Pipeline.source_unique_keys``);
2. **assertion**: an assertion file that names the table, ``SELECT k FROM ${ref("t")} GROUP BY k HAVING COUNT(*) > 1``, which is
   Dataform's own uniqueness check;
3. **Malloy**: ``primary_key`` of a source built on ``table('dataset.table')`` (:mod:`kumosql.malloy_keys`; ``--malloy DIR``);
4. **docs**: a ``Primary key: a, b`` line or a primary-key column in the Markdown docs of a table (:mod:`kumosql.docs_keys`;
   ``--docs DIR``).

A key a model states in its own config (``assertions: { uniqueKey }``) is used where its query does not show a grain, as
``table_profile`` already does. Every claim is something somebody wrote, not something checked against data: answers show it as
declared, with its origin. For a table the project does not have (a warehouse table in a draft), the claim is kept by the name it
was written with and matched by its trailing parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlglot import exp

RANKS = {"assume": 0, "config": 1, "assertion": 2, "malloy": 3, "docs": 4}


@dataclass(frozen=True)
class GrainClaim:
    #: the project's table key when the table is in the project, else the name as written
    table: str
    keys: tuple[str, ...]
    #: what states it, in words: ``declared uniqueKey in the declaration of orders``
    origin: str
    kind: str
    in_project: bool

    @property
    def rank(self) -> int:
        return RANKS[self.kind]


def _shortest(candidates: list[tuple[str, ...]]) -> tuple[str, ...]:
    return min(candidates, key=lambda k: (len(set(c.lower() for c in k)), [c.lower() for c in k]))


def assertion_key(pipeline, tree: exp.Expression) -> tuple[str, tuple[str, ...]] | None:
    """``(table key, columns)`` when ``tree`` is exactly a duplicate-key check, else ``None``.

    The shape is ``SELECT ... FROM one_table GROUP BY c1, c2 HAVING COUNT(*) > 1`` (``COUNT(1)``, ``COUNT(c)`` and ``>= 2`` read
    the same). Any WHERE, join, QUALIFY, LIMIT or DISTINCT makes the claim about a subset of the rows, so it is not read.
    """

    if not isinstance(tree, exp.Select):
        return None
    for name in ("where", "joins", "qualify", "limit", "offset", "distinct", "with_", "with", "laterals", "windows"):
        if tree.args.get(name):
            return None
    source = tree.args.get("from_") or tree.args.get("from")
    group, having = tree.args.get("group"), tree.args.get("having")
    if source is None or group is None or having is None or not isinstance(source.this, exp.Table):
        return None
    if group.args.get("all") or any(group.args.get(name) for name in ("rollup", "cube", "grouping_sets", "totals")):
        return None
    columns = []
    for item in group.expressions:
        if not isinstance(item, exp.Column) or isinstance(item.this, exp.Star):
            return None
        columns.append(item.name)
    condition = having.this
    while isinstance(condition, exp.Paren):
        condition = condition.this
    threshold = {exp.GT: 1, exp.GTE: 2}.get(type(condition))
    count = condition.this if threshold is not None else None
    if count is None or not isinstance(count, exp.Count) or count.args.get("distinct"):
        return None
    limit = condition.expression
    if not (isinstance(limit, exp.Literal) and not limit.is_string and limit.name == str(threshold)):
        return None
    if not columns:
        return None
    try:
        key = pipeline.resolve(source.this)
    except Exception:  # noqa: BLE001
        return None
    return (key, tuple(dict.fromkeys(columns))) if key else None


def collect(pipeline, resolve: Callable[[str], tuple[str | None, list]], docs: list | None = None, malloy: list | None = None) -> list[GrainClaim]:
    """Every claim the project, the Malloy sources and the docs make, unordered. ``resolve`` turns a table text into a project key."""

    claims: list[GrainClaim] = []
    for key, keys in (getattr(pipeline, "source_unique_keys", None) or {}).items():
        usable = [k for k in keys if k]
        if usable and key in pipeline.sources:
            claims.append(GrainClaim(key, _shortest(usable), f"declared uniqueKey in the declaration of {key.split('.')[-1]}", "config", True))
    try:
        parsed = pipeline._analyse().parsed
    except Exception:  # noqa: BLE001 - the assertions are optional evidence
        parsed = {}
    for model_key, model in pipeline.models.items():
        if model.kind != "assertion" or getattr(model, "disabled", False) or parsed.get(model_key) is None:
            continue
        hit = assertion_key(pipeline, parsed[model_key])
        if hit is not None:
            claims.append(GrainClaim(hit[0], hit[1], f"duplicate-key assertion {model.path or model_key.split('.')[-1]}", "assertion", True))
    for item in malloy or ():
        key, _ = resolve(item.table)
        claims.append(GrainClaim(key or item.table, (item.column,), f"Malloy primary_key in {item.file} (source {item.source})", "malloy", key is not None))
    for item in docs or ():
        key = _doc_table(item.table_text, resolve)
        if key is None:
            continue
        claims.append(GrainClaim(key[0], item.columns, f"primary key in docs file {item.file} (section {item.section})", "docs", key[1]))
    return claims


def _doc_table(text: str, resolve) -> tuple[str, bool] | None:
    """``(table, in project)`` for the table a docs section names, or ``None`` when it names none or several."""

    words = [w for w in dict.fromkeys(text.replace("`", " ").replace(":", " ").split()) if w.strip("()[],.")]
    found = {}
    for word in [text.strip(), *words]:
        key, _ = resolve(word.strip("()[],."))
        if key:
            found[key] = True
    if len(found) == 1:
        return next(iter(found)), True
    if not found and len(words) == 1 and "." in words[0]:  # dataset.table of the warehouse, not in the project
        return words[0].strip("()[],."), False
    return None


def best(claims: list[GrainClaim]) -> dict[str, GrainClaim]:
    """One claim per table: the most trusted kind, and among its keys the shortest. Keyed by project key, or the casefolded name."""

    by_table: dict[str, list[GrainClaim]] = {}
    for claim in claims:
        by_table.setdefault(claim.table if claim.in_project else claim.table.casefold(), []).append(claim)
    chosen = {}
    for table, group in by_table.items():
        top = min(c.rank for c in group)
        level = [c for c in group if c.rank == top]
        keys = _shortest([c.keys for c in level])
        chosen[table] = next(c for c in level if c.keys == keys)
    return chosen
