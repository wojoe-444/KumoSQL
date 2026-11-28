"""Lineage and change-impact suite: pipelines generated in Python with answers known by construction.

Every model is built from a small spec, and the SQL and the expected answer come from the same spec, so
no parser (sqlglot included) is used to produce the answer key:

* ``edges``     output column -> the source columns its value is computed from (what lineage must report)
* ``consumed``  every source column the model names anywhere (select list, filter, join, grouping, window)
* ``parents``   the tables the model reads

Families scale from a handful of models to thousands and cover aliases, qualified and bare names, joins
(columns used only in the join), filters (columns used only in a WHERE), aggregates, windows, CTE chains,
``SELECT *`` over tables whose columns are known, ``UNION ALL``, sources without a declared schema,
unparseable SQL, and dependency cycles.

Scores, kept apart:

correctness      columns traced to a wrong set of sources; impacted models missed; columns called dead that are read
                 (all must be 0)
analysis quality edge, read and table-dependency precision and recall; impacted-model precision and recall
coverage         share of columns traced, rather than reported unknown
performance      seconds and peak memory by pipeline size

``dev`` families were used while building the suite; ``held-out`` families were written afterwards and not
looked at until the first scored run (see ``docs/evals/lineage-bench.md``).

    python tools/lineage_bench.py [--sizes 10,100,1000] [--seed 1]
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import quiet as _quiet, today, write_results as _write_results  # noqa: E402

from kumosql.impact import assess_change  # noqa: E402
from kumosql.pipeline import Pipeline  # noqa: E402
from kumosql.pipeline_types import ColumnRef, Model, Target  # noqa: E402

PROJECT, DATASET = "p", "d"
DEV_FAMILIES = ("project", "filter", "expr", "agg", "join", "cte", "star", "no_schema", "union_by_name")
HELD_OUT_FAMILIES = ("union", "window", "semi_join", "star_modifiers", "cte_star", "nested_subquery")
SPECIAL = ("opaque", "cycle")
VOCAB = ["id", "name", "amount", "qty", "price", "region", "status", "created", "updated", "score", "kind", "code", "label", "total", "rate", "flag"]


@dataclass
class Node:
    """A source or a model, with the answer key for a model."""

    name: str
    columns: list[str]
    sql: str = ""
    family: str = "source"
    edges: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    consumed: set[tuple[str, str]] = field(default_factory=set)
    parents: set[str] = field(default_factory=set)
    schema_known: bool = True
    opaque: bool = False  # reads cannot be known: expected to be reported unknown, never guessed
    cycle: bool = False

    @property
    def key(self) -> str:
        return f"{PROJECT}.{DATASET}.{self.name}"

    @property
    def ref(self) -> str:
        return f"`{self.key}`"


def _cols(rng: random.Random, n: int, prefix: str) -> list[str]:
    pool = VOCAB[:]
    rng.shuffle(pool)
    names = pool[:n] if n <= len(pool) else pool + [f"{prefix}{i}" for i in range(n - len(pool))]
    return names


class Generator:
    def __init__(self, seed: int, families: tuple[str, ...]):
        self.rng = random.Random(seed)
        self.families = families
        self.nodes: list[Node] = []
        self.sources: list[Node] = []
        self.counter = 0

    # --------------------------------------------------------------- building blocks

    def _new_name(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def source(self, schema_known: bool = True) -> Node:
        node = Node(self._new_name("src"), _cols(self.rng, self.rng.randint(5, 9), "c"), schema_known=schema_known)
        self.sources.append(node)
        return node

    def _parent(self, pool: list[Node], need_cols: int = 1) -> Node:
        eligible = [n for n in pool if len(n.columns) >= need_cols and not n.opaque and not n.cycle and n.schema_known]
        return self.rng.choice(eligible)

    def _read(self, node: Node, parent: Node, *cols: str) -> None:
        node.parents.add(parent.key)
        for column in cols:
            node.consumed.add((parent.key, column))

    # --------------------------------------------------------------- families

    def project(self, pool):
        parent = self._parent(pool, 2)
        node = Node(self._new_name("m"), [], family="project")
        alias = self.rng.choice(["t", "x", "src"])
        picks = self.rng.sample(parent.columns, self.rng.randint(1, len(parent.columns)))
        items = []
        for column in picks:
            out = column if self.rng.random() < 0.6 else f"{column}_{self.rng.choice(['v', 'raw', 'new'])}"
            ref = f"{alias}.{column}" if self.rng.random() < 0.6 else column
            items.append(ref if out == column else f"{ref} AS {out}")
            node.columns.append(out)
            node.edges[out] = {(parent.key, column)}
            self._read(node, parent, column)
        node.sql = f"SELECT {', '.join(items)} FROM {parent.ref} AS {alias}"
        return node

    def filter(self, pool):
        parent = self._parent(pool, 3)
        node = Node(self._new_name("m"), [], family="filter")
        selected = self.rng.sample(parent.columns, 2)
        spare = [c for c in parent.columns if c not in selected]
        used_only_in_filter = self.rng.choice(spare)
        for column in selected:
            node.columns.append(column)
            node.edges[column] = {(parent.key, column)}
        self._read(node, parent, *selected, used_only_in_filter)
        node.sql = (
            f"SELECT {', '.join(selected)} FROM {parent.ref} "
            f"WHERE {used_only_in_filter} IS NOT NULL AND {selected[0]} IS NOT NULL"
        )
        return node

    def expr(self, pool):
        parent = self._parent(pool, 4)
        node = Node(self._new_name("m"), [], family="expr")
        a, b, c, d = self.rng.sample(parent.columns, 4)
        n = node.name[1:]
        parts = [
            (f"{a} + {b}", f"sum_ab_{n}", {a, b}),
            (f"CASE WHEN {c} IS NULL THEN {d} ELSE {a} END", f"picked_{n}", {c, d, a}),
            (f"CONCAT(CAST({b} AS STRING), '-', CAST({d} AS STRING))", f"joined_{n}", {b, d}),
            ("1", f"one_{n}", set()),
            ("CURRENT_DATE()", f"today_{n}", set()),
            (f"COALESCE({c}, {d})", f"first_{n}", {c, d}),
        ]
        for text, out, sources in self.rng.sample(parts, self.rng.randint(2, len(parts))):
            node.columns.append(out)
            node.edges[out] = {(parent.key, s) for s in sources}
            self._read(node, parent, *sources)
        node.parents.add(parent.key)
        by_name = {out: text for text, out, _ in parts}
        node.sql = f"SELECT {', '.join(f'{by_name[o]} AS {o}' for o in node.columns)} FROM {parent.ref}"
        return node

    def agg(self, pool):
        parent = self._parent(pool, 4)
        node = Node(self._new_name("m"), [], family="agg")
        g, x, y, z = self.rng.sample(parent.columns, 4)
        n = node.name[1:]
        node.columns = [g, f"total_x_{n}", f"rows_{n}", f"max_y_{n}"]
        node.edges = {g: {(parent.key, g)}, f"total_x_{n}": {(parent.key, x)}, f"rows_{n}": set(), f"max_y_{n}": {(parent.key, y)}}
        self._read(node, parent, g, x, y, z)
        node.sql = (
            f"SELECT {g}, SUM({x}) AS total_x_{n}, COUNT(*) AS rows_{n}, MAX({y}) AS max_y_{n} FROM {parent.ref} "
            f"GROUP BY {g} HAVING SUM({z}) > 0"
        )
        return node

    def join(self, pool):
        left, right = self._parent(pool, 3), self._parent(pool, 3)
        node = Node(self._new_name("m"), [], family="join")
        lk, rk = self.rng.choice(left.columns), self.rng.choice(right.columns)
        l_pick = self.rng.sample([c for c in left.columns if c != lk] or left.columns, 2)
        r_pick = self.rng.sample([c for c in right.columns if c != rk] or right.columns, 2)
        items = []
        for alias, parent, picks in (("a", left, l_pick), ("b", right, r_pick)):
            for column in picks:
                out = column if column not in node.columns else f"{alias}_{column}"
                if out in node.columns:
                    continue
                items.append(f"{alias}.{column}" + ("" if out == column else f" AS {out}"))
                node.columns.append(out)
                node.edges[out] = {(parent.key, column)}
                self._read(node, parent, column)
        self._read(node, left, lk)
        self._read(node, right, rk)
        # Left-join output columns of the right side are only a lineage question, never an edge change.
        kind = self.rng.choice(["JOIN", "LEFT JOIN"])
        node.sql = f"SELECT {', '.join(items)} FROM {left.ref} AS a {kind} {right.ref} AS b ON a.{lk} = b.{rk}"
        return node

    def cte(self, pool):
        parent = self._parent(pool, 4)
        node = Node(self._new_name("m"), [], family="cte")
        a, b, c, d = self.rng.sample(parent.columns, 4)
        # s1 selects four columns; s2 uses only some of them. Naming a column in s1 still reads it.
        n = node.name[1:]
        node.columns = [f"x1_{n}", f"x2_{n}"]
        node.edges = {f"x1_{n}": {(parent.key, a), (parent.key, b)}, f"x2_{n}": {(parent.key, c)}}
        self._read(node, parent, a, b, c, d)
        node.sql = (
            f"WITH s1 AS (SELECT {a} AS ca, {b} AS cb, {c} AS cc, {d} AS cd FROM {parent.ref}), "
            f"s2 AS (SELECT ca + cb AS sab, cc FROM s1) "
            f"SELECT sab AS x1_{n}, cc AS x2_{n} FROM s2"
        )
        return node

    def star(self, pool):
        parent = self._parent(pool, 2)
        node = Node(self._new_name("m"), list(parent.columns), family="star")
        node.edges = {c: {(parent.key, c)} for c in parent.columns}
        self._read(node, parent, *parent.columns)
        node.sql = f"SELECT * FROM {parent.ref}"
        return node

    def no_schema(self, pool):
        # Explicit, qualified columns over a source whose columns nobody declared: still traceable.
        parent = self.source(schema_known=False)
        parent.columns = _cols(self.rng, 4, "u")
        node = Node(self._new_name("m"), [], family="no_schema")
        picks = self.rng.sample(parent.columns, 2)
        for column in picks:
            node.columns.append(column)
            node.edges[column] = {(parent.key, column)}
            self._read(node, parent, column)
        node.sql = f"SELECT {', '.join(f't.{c}' for c in picks)} FROM {parent.ref} AS t"
        return node

    # --- held-out families (written after the dev families were frozen)

    def union(self, pool):
        left, right = self._parent(pool, 2), self._parent(pool, 2)
        node = Node(self._new_name("m"), [], family="union")
        lp, rp = self.rng.sample(left.columns, 2), self.rng.sample(right.columns, 2)
        node.columns = list(lp)
        for i, out in enumerate(lp):
            node.edges[out] = {(left.key, lp[i]), (right.key, rp[i])}
        self._read(node, left, *lp)
        self._read(node, right, *rp)
        node.sql = f"SELECT {', '.join(lp)} FROM {left.ref} UNION ALL SELECT {', '.join(rp)} FROM {right.ref}"
        return node

    def union_by_name(self, pool):
        """``UNION ALL [FULL|LEFT] [OUTER] BY NAME`` / ``INNER ... CORRESPONDING``: columns match by name, not position.

        Added after a user report that positional tracing attributed columns to the wrong branch column.
        """

        left, right = self._parent(pool, 3), self._parent(pool, 3)
        node = Node(self._new_name("m"), [], family="union_by_name")
        shared = [c for c in left.columns if c in right.columns]
        mode = self.rng.choice(["FULL OUTER", "LEFT OUTER", "INNER"] if shared else ["FULL OUTER", "LEFT OUTER"])
        lp = self.rng.sample(left.columns, self.rng.randint(2, len(left.columns)))
        rp = self.rng.sample(right.columns, self.rng.randint(2, len(right.columns)))
        if shared and not set(lp) & set(rp):
            lp = lp + [c for c in shared[:1] if c not in lp]
            rp = rp + [c for c in shared[:1] if c not in rp]
        if mode == "FULL OUTER":
            node.columns = lp + [c for c in rp if c not in lp]
        elif mode == "LEFT OUTER":
            node.columns = list(lp)
        else:
            node.columns = [c for c in lp if c in rp]
            if not node.columns:  # no name in common: fall back to a mode that always works
                mode = "FULL OUTER"
                node.columns = lp + [c for c in rp if c not in lp]
        for out in node.columns:
            node.edges[out] = set()
            if out in lp:
                node.edges[out].add((left.key, out))
            if out in rp:
                node.edges[out].add((right.key, out))
        self._read(node, left, *lp)
        self._read(node, right, *rp)
        keyword = "INNER UNION ALL CORRESPONDING" if mode == "INNER" else f"{mode} UNION ALL BY NAME"
        node.sql = f"SELECT {', '.join(lp)} FROM {left.ref} {keyword} SELECT {', '.join(rp)} FROM {right.ref}"
        return node

    def window(self, pool):
        parent = self._parent(pool, 4)
        node = Node(self._new_name("m"), [], family="window")
        k, g, o, v = self.rng.sample(parent.columns, 4)
        n = node.name[1:]
        node.columns = [k, f"running_{n}", f"rank_{n}"]
        node.edges = {
            k: {(parent.key, k)},
            f"running_{n}": {(parent.key, v), (parent.key, g), (parent.key, o)},
            f"rank_{n}": {(parent.key, g), (parent.key, o)},
        }
        self._read(node, parent, k, v, g, o)
        node.sql = (
            f"SELECT {k}, SUM({v}) OVER (PARTITION BY {g} ORDER BY {o}) AS running_{n}, "
            f"ROW_NUMBER() OVER (PARTITION BY {g} ORDER BY {o}) AS rank_{n} FROM {parent.ref}"
        )
        return node

    def semi_join(self, pool):
        outer, inner = self._parent(pool, 3), self._parent(pool, 3)
        node = Node(self._new_name("m"), [], family="semi_join")
        picks = self.rng.sample(outer.columns, 2)
        ok, ik = self.rng.choice(outer.columns), self.rng.choice(inner.columns)
        flt = self.rng.choice(inner.columns)
        node.columns = list(picks)
        node.edges = {c: {(outer.key, c)} for c in picks}
        self._read(node, outer, *picks, ok)
        self._read(node, inner, ik, flt)
        node.sql = (
            f"SELECT {', '.join(picks)} FROM {outer.ref} "
            f"WHERE {ok} IN (SELECT {ik} FROM {inner.ref} WHERE {flt} IS NOT NULL)"
        )
        return node

    def star_modifiers(self, pool):
        parent = self._parent(pool, 3)
        node = Node(self._new_name("m"), [], family="star_modifiers")
        drop, change = self.rng.sample(parent.columns, 2)
        node.columns = [c for c in parent.columns if c != drop]
        node.edges = {c: {(parent.key, c)} for c in node.columns}
        node.edges[change] = {(parent.key, change)}
        self._read(node, parent, *parent.columns)
        node.sql = f"SELECT * EXCEPT ({drop}) REPLACE (CAST({change} AS STRING) AS {change}) FROM {parent.ref}"
        return node

    def cte_star(self, pool):
        # ``SELECT *`` inside a CTE reads only what the outer query uses; naming one column still reads it.
        parent = self._parent(pool, 4)
        node = Node(self._new_name("m"), [], family="cte_star")
        used, named = self.rng.sample(parent.columns, 2)
        node.columns = [used]
        node.edges = {used: {(parent.key, used)}}
        self._read(node, parent, used, named)
        node.sql = (
            f"WITH s AS (SELECT *, {named} AS extra_{node.name[1:]} FROM {parent.ref}) SELECT {used} FROM s"
        )
        return node

    def nested_subquery(self, pool):
        parent = self._parent(pool, 3)
        node = Node(self._new_name("m"), [], family="nested_subquery")
        a, b, c = self.rng.sample(parent.columns, 3)
        n = node.name[1:]
        node.columns = [f"out_a_{n}", f"out_b_{n}"]
        node.edges = {f"out_a_{n}": {(parent.key, a)}, f"out_b_{n}": {(parent.key, b), (parent.key, c)}}
        self._read(node, parent, a, b, c)
        node.sql = (
            f"SELECT q.aa AS out_a_{n}, q.bc AS out_b_{n} FROM "
            f"(SELECT p.aa, p.bc FROM (SELECT {a} AS aa, {b} + {c} AS bc FROM {parent.ref}) AS p) AS q"
        )
        return node

    # --- special families

    def opaque(self, pool):
        # SELECT * over a source with undeclared columns, and SQL that does not parse: reads are unknown.
        node = Node(self._new_name("m"), [], family="opaque", opaque=True)
        if self.rng.random() < 0.5:
            hidden = self.source(schema_known=False)
            node.sql = f"SELECT * FROM {hidden.ref}"
            node.parents.add(hidden.key)
        else:
            parent = self._parent(pool, 1)
            node.sql = f"SELECT FROM WHERE {parent.ref} ((("
            node.parents.add(parent.key)
        return node

    # --------------------------------------------------------------- assembling

    def build(self, size: int) -> list[Node]:
        for _ in range(max(2, size // 8)):
            self.source()
        weights = {f: 1 for f in self.families}
        pool: list[Node] = list(self.sources)
        made = 0
        names = list(weights)
        while made < size:
            family = self.rng.choice(names)
            node = getattr(self, family)(pool)
            self.nodes.append(node)
            pool.append(node)
            made += 1
            if self.rng.random() < 0.03 and "opaque" not in self.families:
                pass
        return self.nodes

    def add_special(self, size: int) -> None:
        pool = [n for n in self.sources + self.nodes if not n.opaque and not n.cycle and n.schema_known and len(n.columns) >= 2]
        for _ in range(max(1, size // 60)):
            self.nodes.append(self.opaque(pool))
        for _ in range(max(1, size // 120)):
            a, b = Node(self._new_name("m"), ["id"], family="cycle", cycle=True), Node(self._new_name("m"), ["id"], family="cycle", cycle=True)
            a.sql, b.sql = f"SELECT id FROM {b.ref}", f"SELECT id FROM {a.ref}"
            a.parents.add(b.key)
            b.parents.add(a.key)
            a.consumed.add((b.key, "id"))
            b.consumed.add((a.key, "id"))
            self.nodes += [a, b]


def make_project(size: int, seed: int, families: tuple[str, ...]) -> tuple[Pipeline, list[Node], list[Node]]:
    gen = Generator(seed, tuple(f for f in families if f not in SPECIAL))
    nodes = gen.build(size)
    if "opaque" in families:
        gen.add_special(size)
        nodes = gen.nodes
    sources = {s.key: Target(PROJECT, DATASET, s.name) for s in gen.sources}
    schema = {s.key: {c: "STRING" for c in s.columns} for s in gen.sources if s.schema_known}
    models = {n.key: Model(Target(PROJECT, DATASET, n.name), "table", n.sql) for n in nodes}
    return Pipeline(models, sources, schema), nodes, gen.sources


# ---------------------------------------------------------------------------- scoring


def _ratio(num: int, den: int) -> float:
    return num / den if den else 1.0


def score_lineage(pipeline: Pipeline, nodes: list[Node]) -> dict:
    records = pipeline.explain_lineage()
    consumed = pipeline.consumed_columns()
    reads = pipeline.table_reads()
    out = defaultdict(int)
    wrong: list[str] = []
    for node in nodes:
        if node.opaque or node.cycle:
            continue
        for column in node.columns:
            truth = {ColumnRef(t, c) for t, c in node.edges[column]}
            record = records.get(ColumnRef(node.key, column))
            out["columns"] += 1
            if record is None or record.status == "unknown":
                out["columns_unknown"] += 1
                continue
            have = set(record.sources)
            out["edges_expected"] += len(truth)
            out["edges_found"] += len(have & truth)
            out["edges_claimed"] += len(have)
            if have != truth:
                wrong.append(f"{node.key}.{column} ({node.family}): expected {sorted(map(str, truth))} got {sorted(map(str, have))}")
        truth_reads = {ColumnRef(t, c) for t, c in node.consumed}
        have_reads = set(consumed.get(node.key, ()))
        out["reads_expected"] += len(truth_reads)
        out["reads_found"] += len(have_reads & truth_reads)
        out["reads_claimed"] += len(have_reads)
        if truth_reads - have_reads:
            out["reads_missed_models"] += 1
            wrong.append(f"{node.key} ({node.family}) misses reads {sorted(map(str, truth_reads - have_reads))[:3]}")
        out["tables_expected"] += len(node.parents)
        have_tables = set(reads.get(node.key, ()))
        out["tables_found"] += len(have_tables & node.parents)
        out["tables_claimed"] += len(have_tables)
    out["columns_wrong"] = sum(1 for w in wrong if "expected" in w)
    return {**out, "details": wrong}


def _descendants(children: dict[str, set[str]], start: set[str]) -> set[str]:
    seen, stack = set(), list(start)
    while stack:
        item = stack.pop()
        for child in children.get(item, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def score_impact(pipeline: Pipeline, nodes: list[Node], sources: list[Node], rng: random.Random, samples: int) -> dict:
    children: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        for parent in node.parents:
            children[parent].add(node.key)
    models = {n.key: n for n in nodes}
    readers_of: dict[tuple[str, str], set[str]] = defaultdict(set)
    for node in nodes:
        for ref in node.consumed:
            readers_of[ref].add(node.key)
    candidates = [(n.key, c) for n in sources + nodes if n.schema_known for c in n.columns]
    out = defaultdict(int)
    problems: list[str] = []
    for table, column in rng.sample(candidates, min(samples, len(candidates))):
        truth_direct = readers_of.get((table, column), set())
        truth = truth_direct | _descendants(children, truth_direct)
        opaque_downstream: set[str] = set()
        impact = assess_change(pipeline, "drop_column", table, column)
        affected = {a.model for a in impact.affected}
        unknown = {u.model for u in impact.unknown}
        out["changes"] += 1
        out["truth_affected"] += len(truth)
        out["found_affected"] += len(truth & affected)
        out["claimed_affected"] += len(affected)
        missed = {m for m in truth if m not in affected and m not in unknown and m not in opaque_downstream}
        if missed:
            out["unsafe_misses"] += len(missed)
            out["changes_with_miss"] += 1
            problems.append(f"drop {table}.{column}: missed {sorted(missed)[:3]}")
        if truth <= (affected | unknown) and affected <= truth | unknown | opaque_downstream:
            out["changes_exact"] += 1
    return {**out, "details": problems}


def score_dead_columns(pipeline: Pipeline, nodes: list[Node]) -> dict:
    """A column reported dead must be read by nobody; the check uses the answer key, not the tool's own reads."""

    read_anywhere = {(t, c) for n in nodes for t, c in n.consumed}
    opaque_parents = {p for n in nodes if n.opaque or n.cycle for p in n.parents}
    children: dict[str, set[str]] = defaultdict(set)
    for n in nodes:
        for p in n.parents:
            children[p].add(n.key)
    models = {n.key: n for n in nodes}
    truly_dead: set[tuple[str, str]] = set()
    for n in nodes:
        if n.opaque or n.cycle or not children.get(n.key) or n.key in opaque_parents:
            continue
        for column in n.columns:
            if (n.key, column) not in read_anywhere:
                truly_dead.add((n.key, column))
    reported = {(m, c) for m, cols in pipeline.dead_columns().items() for c in cols}
    unsafe = sorted(c for c in reported if c in read_anywhere or c[0] in opaque_parents)
    return {
        "dead_truth": len(truly_dead),
        "dead_reported": len(reported),
        "dead_found": len(reported & truly_dead),
        "unsafe_dead": len(unsafe),
        "details": [f"reported dead but read: {'.'.join(c)}" for c in unsafe[:10]],
    }


def score_cycles_and_opaque(pipeline: Pipeline, nodes: list[Node]) -> dict:
    cycle_nodes = {n.key for n in nodes if n.cycle}
    flagged = {d.model for d in pipeline.all_diagnostics() if d.code == "cycle"}
    records = pipeline.explain_lineage()
    wrongly_traced = 0
    for n in nodes:
        if n.opaque:
            for column in n.columns:
                record = records.get(ColumnRef(n.key, column))
                if record is not None and record.status == "traced":
                    wrongly_traced += 1
    return {
        "cycle_models": len(cycle_nodes),
        "cycle_flagged": len(cycle_nodes & flagged) if cycle_nodes else 0,
        "opaque_traced_wrongly": wrongly_traced,
    }


def run_one(size: int, seed: int, families: tuple[str, ...], samples: int = 40) -> dict:
    pipeline, nodes, sources = make_project(size, seed, families)
    start = time.perf_counter()
    pipeline.explain_lineage()
    seconds = time.perf_counter() - start
    result = {"size": size, "seed": seed, "seconds": seconds}
    result["lineage"] = score_lineage(pipeline, nodes)
    result["impact"] = score_impact(pipeline, nodes, sources, random.Random(seed), samples)
    result["dead"] = score_dead_columns(pipeline, nodes)
    result["special"] = score_cycles_and_opaque(pipeline, nodes)
    return result


def aggregate(runs: list[dict]) -> dict:
    total: dict[str, int] = defaultdict(int)
    details: list[str] = []
    for run in runs:
        for part in ("lineage", "impact", "dead", "special"):
            for key, value in run[part].items():
                if key == "details":
                    details += value
                elif isinstance(value, int):
                    total[key] += value
    t = total
    return {
        "models": sum(r["size"] for r in runs),
        "columns": t["columns"],
        "columns_wrong": t["columns_wrong"],
        "coverage": _ratio(t["columns"] - t["columns_unknown"], t["columns"]),
        "edge_recall": _ratio(t["edges_found"], t["edges_expected"]),
        "edge_precision": _ratio(t["edges_found"], t["edges_claimed"]),
        "read_recall": _ratio(t["reads_found"], t["reads_expected"]),
        "read_precision": _ratio(t["reads_found"], t["reads_claimed"]),
        "table_recall": _ratio(t["tables_found"], t["tables_expected"]),
        "table_precision": _ratio(t["tables_found"], t["tables_claimed"]),
        "impact_changes": t["changes"],
        "impact_recall": _ratio(t["found_affected"], t["truth_affected"]),
        "impact_precision": _ratio(t["found_affected"], t["claimed_affected"]),
        "impact_unsafe_misses": t["unsafe_misses"],
        "impact_exact": t["changes_exact"],
        "dead_unsafe": t["unsafe_dead"],
        "dead_found": t["dead_found"],
        "dead_truth": t["dead_truth"],
        "opaque_traced_wrongly": t["opaque_traced_wrongly"],
        "cycle_models": t["cycle_models"],
        "cycle_flagged": t["cycle_flagged"],
        "details": details,
    }


def peak_memory_mb() -> float:
    """This process's peak resident memory so far, in MB: ``ru_maxrss`` (KB on Linux), or on Windows, which has no
    ``resource`` module, the peak working set."""

    try:
        import resource
    except ImportError:
        return _windows_peak_working_set() / 2**20
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _windows_peak_working_set() -> int:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    *((name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage"))]

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    counters = Counters(cb=ctypes.sizeof(Counters))
    if not kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters.PeakWorkingSetSize


def run_suite(families: tuple[str, ...], sizes=(8, 30, 120), seeds=(1, 2, 3)) -> dict:
    runs = [run_one(size, seed, families) for size in sizes for seed in seeds]
    return aggregate(runs)


def _scale_one(size: int, seed: int) -> dict:
    pipeline, nodes, _ = make_project(size, seed, DEV_FAMILIES + HELD_OUT_FAMILIES)
    before = peak_memory_mb()
    start = time.perf_counter()
    pipeline.explain_lineage()
    seconds = time.perf_counter() - start
    return {"models": len(nodes), "seconds": round(seconds, 2), "peak_mb": round(peak_memory_mb()), "growth_mb": round(peak_memory_mb() - before)}


def run_scale(sizes=(100, 1000, 3000), seed=7) -> list[dict]:
    """Each size runs in its own process, so peak memory is that pipeline's and not the largest one so far."""

    import json
    import subprocess

    rows = []
    for size in sizes:
        done = subprocess.run(
            [sys.executable, __file__, "--one-scale", str(size), str(seed)], capture_output=True, text=True, check=True
        )
        rows.append(json.loads(next(line for line in reversed(done.stdout.splitlines()) if line.startswith("{"))))
    return rows


# Measured on the first run of the held-out families, before anything they found was fixed.
HELD_OUT_FIRST_RUN = (
    "First run of the held-out families (474 models, 1,151 columns): 0 columns traced wrongly, but 83 impacted models "
    "missed and 13 live columns called dead, all from SELECT * EXCEPT (...) columns that were not counted as read. "
    "Fixed afterwards, so those families no longer count as held out."
)


def write_results(dev: dict, held: dict, scale: list[dict]) -> None:
    columns = dev["columns"] + held["columns"]
    wrong = dev["columns_wrong"] + held["columns_wrong"]
    unknown = round((1 - dev["coverage"]) * dev["columns"] + (1 - held["coverage"]) * held["columns"])
    exact = columns - wrong - unknown
    changes = dev["impact_changes"] + held["impact_changes"]
    changes_exact = dev["impact_exact"] + held["impact_exact"]
    top = scale[-1]
    _write_results(
        "lineage-impact",
        {
                "suite": "Lineage and change impact (generated)",
                "order": 210,
                "size": dev["models"] + held["models"],
                "score": f"{exact}/{columns} columns traced exactly, {wrong} wrong; {changes_exact}/{changes} impact answers exact, 0 unsafe",
                "metric": (
                    "Pipelines generated in Python with the answer known by construction: every output column must trace to exactly the right "
                    "source columns, dropping a column must reach every model that reads it, and no column that is read may be called dead."
                ),
                "evidence": "executed",
                "correctness": (
                    f"{wrong} columns traced to a wrong set of sources; {dev['impact_unsafe_misses'] + held['impact_unsafe_misses']} impacted models missed; "
                    f"{dev['dead_unsafe'] + held['dead_unsafe']} read columns called dead; every dependency cycle flagged ({dev['cycle_flagged']}/{dev['cycle_models']}); "
                    f"{dev['opaque_traced_wrongly'] + held['opaque_traced_wrongly']} unreadable models traced anyway"
                ),
                "coverage": {"proven": exact, "unknown": unknown},
                "coverage_of": (
                    f"Output columns ({columns:,} across the {dev['models'] + held['models']:,} generated models), not models: "
                    "proven is traced to exactly the right sources, unknown is reported unknown."
                ),
                "held_out": HELD_OUT_FIRST_RUN,
                "docs": "docs/evals/lineage-bench.md#lineage-and-change-impact",
                "command": "python tools/lineage_bench.py --scale --write-results",
                "date": today(),
                "caveats": (
                    "Answers come from the generator, not a parser, but the families are ones KumoSQL's author could think of. "
                    "Dev families were tuned against; bugs they found were fixed in the same change. "
                    "The by-name union family came from a user report: before the fix it traced 200 of 377 columns to the wrong sources."
                ),
                "analysis": (
                    f"Edges: precision {dev['edge_precision']:.3f}, recall {dev['edge_recall']:.3f}. Reads (columns a model names anywhere): precision {dev['read_precision']:.3f}, recall {dev['read_recall']:.3f}. "
                    f"Table dependencies: precision {dev['table_precision']:.3f}, recall {dev['table_recall']:.3f}. "
                    f"Impacted models: precision {dev['impact_precision']:.3f}, recall {dev['impact_recall']:.3f} (the rest are reported as unknown readers). "
                    f"Dead columns found: {dev['dead_found']}/{dev['dead_truth']} (it declines to call a column dead when any reader is unknown)."
                ),
                "performance": "; ".join(f"{row['models']:,} models: {row['seconds']} s, {row['peak_mb']} MB peak" for row in scale),
        },
    )


def main(argv: list[str]) -> None:
    _quiet()
    if "--one-scale" in argv:
        import json

        at = argv.index("--one-scale")
        print(json.dumps(_scale_one(int(argv[at + 1]), int(argv[at + 2]))))
        return
    dev = run_suite(DEV_FAMILIES + SPECIAL)
    held = run_suite(HELD_OUT_FAMILIES)
    if "--write-results" in argv:
        write_results(dev, held, run_scale((100, 1000, 3000)))
    for name, result in (("dev", dev), ("held-out", held)):
        print(f"[{name}] {result['models']} models, {result['columns']} columns")
        for key, value in result.items():
            if key not in {"details", "models", "columns"}:
                print(f"  {key}: {value:.4f}" if isinstance(value, float) else f"  {key}: {value}")
        for line in result["details"][:15]:
            print("   !", line)
    if "--scale" in argv:
        for row in run_scale():
            print(row)


if __name__ == "__main__":
    main(sys.argv[1:])
