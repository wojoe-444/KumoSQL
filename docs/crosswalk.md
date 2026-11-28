# Saved equivalences and the crosswalk

KumoSQL keeps one store of statements about how two tables correspond. A statement with status `equal` ("column `amt` of `raw.orders` is column `amount` of `raw.orders_v2`") is an assumption that proofs use, across every layer of a pipeline. Statements with status `differs` or `missing` record how an old set of tables maps to a new one (a sandbox rebuilt as a Dataform mart, say), and they never enter a proof. `equivalence suggest` proposes such pairs from what the columns mean. It never saves anything.

## Record an entry

```
python -m kumosql equivalence add raw.orders raw.orders_v2 amt=amount id=order_id --whole
python -m kumosql equivalence add old.orders mart.orders created=created_at --status differs \
    --explanation timezone_utc_vs_est --evidence "SELECT ... WHERE a.created != b.created_at" --verified-at 2026-11-20
python -m kumosql equivalence add old.orders mart.orders legacy_flag= =channel --status missing
python -m kumosql equivalence add old.weekly_report - --status missing
```

| Status | Means | Columns | Enters a proof |
| --- | --- | --- | --- |
| `equal` (default) | The listed columns hold the same rows as a bag; with `--whole`, the tables hold the same rows and unlisted columns keep their names | `LEFTCOL=RIGHTCOL`, or none with `--whole` | Yes, as a listed assumption |
| `differs` | The listed column pairs correspond but their values differ; with no columns, the tables correspond but keep different rows | `LEFTCOL=RIGHTCOL`, or none | Never |
| `missing` | A column has no counterpart (`LEFTCOL=` or `=RIGHTCOL`), or a table has none (`-` for the other side) | one side of each pair | Never |

The other fields are optional on every status:

- `--explanation RULE`: the name of the [known-difference rule](reconcile.md#known-difference-rules) behind a `differs` or `missing` entry, one line, up to 120 characters (the longest name a rule may have). An `equal` entry refuses one.
- `--evidence TEXT`: a query or a link that shows the entry holds, up to 4,000 characters.
- `--verified-at YYYY-MM-DD`: when someone checked it. The date must exist on the calendar.

An entry that contradicts a saved one is refused, and the message names the entry to remove first. These count as contradictions: a second entry for the same column of the same pair of tables (in either direction); a `differs` or `missing` entry on a pair that has a `--whole` `equal` declaration, which says every column is equal; and any entry that names a table recorded as having no counterpart. As before, an `equal` declaration is one per right-hand table, and no cycles are allowed.

`python -m kumosql equivalence list` prints every entry as JSON. `--status S` keeps one status. `--table T` and `--column C` print only the entries naming them, through `lookup` below. `python -m kumosql equivalence remove RIGHT` removes every entry for `RIGHT`; `--left` and `--status` narrow it. For a table recorded as having no counterpart, give that table.

**Settings → Solver → Equivalent columns** in the [browser UI](ui.md) shows and saves `equal` declarations only, and its **Remove** button removes only those. `differs` and `missing` entries are available from the command line and from Python for now.

## Only `equal` reaches a proof

A declaration enters a proof by rewriting each read of the right-hand table into a read of the left-hand one, so the solver sees one relation. If a recorded difference were rewritten that way, the proof would be wrong. Three things keep it out:

- `equal` declarations are stored alone in `equivalences.json` in the data folder. `differs` and `missing` records go in `crosswalk.json` beside it. A KumoSQL from before statuses existed reads only the first file, so it cannot take a recorded difference for an equality.
- `equivalences.load()`, which the provers and `prove-tables` read, returns `equal` entries only. A `differs` entry pasted into `equivalences.json` by hand is skipped.
- `rewrite_tree` and `rewrite_sql` skip any entry that is not `equal`, even one passed to them directly, and `replacement` refuses one.

`tests/test_crosswalk.py` checks that a saved `differs` mapping proves neither a query pair nor a two-pipeline `prove-tables` comparison, and that the same mapping saved as `equal` proves both.

## Look up an entry

`kumosql.equivalences.lookup(table=None, column=None, *, status=None)` returns the entries that name `table` on either side. The full name and its last dotted parts both match: `orders`, `raw.orders` and `proj.raw.orders` all find an entry saved under any one of them. Results can be narrowed to those naming `column` and to one `status`. Each result is the entry's JSON plus:

- `label`;
- `matched_on`: the side `table` names, `left`, `right` or `both`;
- `counterpart`: the table on the other side, `null` when it has none;
- `column_matches`: one `{left_column, right_column, implied}` per pair. A `null` column has no counterpart. `implied` marks a column that a `--whole` declaration gives the same name on both sides without listing it; that only applies when a table is given.

`load_all()` returns every entry.

The listed JSON is a crosswalk file for [reconcile](reconcile.md):

```
python -m kumosql equivalence list --table mart.orders > orders-map.json
python -m kumosql reconcile --left my-proj.old.orders --right my-proj.mart.orders --key order_id --map orders-map.json --rules rules.yml
```

`reconcile` compares the `equal` and `differs` pairs under their mapped names, reports each `differs` pair's `explanation` under `column_explanations`, and skips `missing` columns instead of reporting them as on one side only. `tests/test_crosswalk.py` reads such a file with `reconcile.load_crosswalk`.

## Suggest pairs

```
python -m kumosql equivalence suggest --project DIR --left old --right mart [--top 20] [--min-score 0.3] [--source-schema FILE]
```

This compares every table of the left set with every table of the right set, using the column meanings of their [table profiles](pipeline-analysis.md). It prints ranked suggestions as JSON and writes nothing. `--left` and `--right` take comma-separated or repeated specs. Each spec is tried in this order, and the first rule that matches a table wins:

1. a glob (`proj.*.stg_*`);
2. a table (`orders`, `raw.orders`);
3. a dataset (`mart`, `proj.mart`);
4. a folder of the project (`definitions/old`);
5. a name prefix (`stg_`).

A spec that matches nothing is an error, and the output's `selection` says how each spec matched. Assertions and operations without an output are never selected. When the two sets share tables (`--left '*' --right '*'` searches a whole project), each pair is compared once, and never a table with itself. `kumosql.crosswalk.suggest_equivalences(pipeline, left_keys, right_keys)` and `select_tables` are the Python equivalents.

Columns are paired one to one, strongest first:

| Tier | `match_kind` | Example (left, right) |
| --- | --- | --- |
| The same meaning (`exact: true`) | `same_source_column`, `same_aggregate` or `same_expression` | `id AS order_key` and `id`; `SUM(amt)` and `SUM(amt)`; `UPPER(status)` and `upper(status)` |
| The same aggregate function over the same source columns | `same_aggregate` | `SUM(amt * 3)` and `SUM(amt * 2)` |
| The same source columns through another transformation (an aggregate is never paired with a plain column this way) | `same_source_column` | `ts` and `DATE(ts)` |
| The same expression over other source columns | `same_expression` | `UPPER(a.status)` and `UPPER(b.state)` |
| The same name only, worth nothing to the table's score | `same_name` | `amt` from `orders` and `amt` from `refunds` |

A shared name adds a little to a pair matched on meaning. A name alone never makes a suggestion. An aggregate pair counts for less when the two tables' grains differ or one is unknown, because the aggregates then cover different groups. Saved `equal` declarations are applied to the meanings first. With `raw.orders ≡ raw.orders_v2 (customer_id = cust_id, amt = amount)` saved, `SUM(amount)` over `orders_v2` matches `SUM(amt)` over `orders`, and the reason says so.

A table pair's `score` (0 to 1) is how much of both tables the matched pairs cover. It goes up a little when the grains (`checks.grain`) and the row filters (`checks.row_scope`) are known to agree. Suggestions are sorted by score, then by table name, so the same project gives the same order every time. For the `two_versions` project in `tests/test_crosswalk.py`, an old sandbox and its rebuild over one source, the result starts like this:

```json
{"left_table": "proj.old.o2", "right_table": "proj.mart.n2", "score": 0.874,
 "checks": {"grain": "matched", "row_scope": "matched"},
 "matched_columns": [{"left_column": "customer_id", "right_column": "cid", "score": 0.9,
                      "match_kind": "same_source_column", "exact": true,
                      "left_meaning": "col:proj.raw.orders.customer_id", "right_meaning": "col:proj.raw.orders.customer_id",
                      "reasons": ["same meaning"]}, "..."],
 "left_only_columns": [], "right_only_columns": [], "unknown_meaning": {"left": {}, "right": {}},
 "already_declared": false, "declared": [],
 "command_hint": "python -m kumosql equivalence add proj.old.o2 proj.mart.n2 customer_id=cid mx=biggest n=cnt revenue=total",
 "differs_hint": "python -m kumosql equivalence add proj.old.o2 proj.mart.n2 triple=dbl --status differs",
 "review": ["columns matched on less than the same meaning are proposed as differs: check how their values differ"]}
```

The second suggestion pairs `proj.old.o1` with `proj.mart.n1`. Its source has no declared key, so the grain is unknown, and its `command_hint` ends in `--status differs`.

Two columns with the same meaning row by row can still hold different rows, so the hints are conservative:

- `command_hint` proposes `equal` only for exact pairs, and only when both grains and both row filters are known to agree. Otherwise it proposes the same pairs with `--status differs`, which can never produce a wrong proof, and `review` says what to check before dropping `--status differs`.
- `differs_hint` holds the weaker pairs, always with `--status differs` and in a hint of its own, so editing the first hint never declares a transformed column equal.
- `left_only_columns` and `right_only_columns` are candidates for `--status missing`.
- `unknown_meaning` lists, per side, the columns whose lineage could not be traced, each with the profile's reason. Nothing is concluded about them.
- `already_declared` and `declared` name the entries the store already has for the pair.

Pairs that share no meaning, source column or expression shape are counted under `skipped.nothing_in_common`, pairs below `--min-score` under `skipped.below_min_score`, and `--top` caps how many are printed (`total` counts them all).

## Limits

- Suggestions come from lineage, not data. Use them to decide what to check, not as evidence. The score is a ranking heuristic, not a probability.
- A meaning names the source columns a model reads, so declarations act on source tables. One between two models changes no meaning.
- A table whose grain is unknown never gets an `equal` hint. This is the common case for a table that only passes source rows on, since a source's grain is known only when someone states it. The command profiles the project with no stated grains. The [agent server](mcp-server.md) takes them from `--assume-grain` and from a declared source's `uniqueKey`, and `suggest_equivalences(..., profiles=...)` accepts profiles built that way.
- A column whose meaning is unknown (see the [agent server's limits](mcp-server.md#limits) for how often lineage stops) cannot be matched, so a pair of tables built on such columns may be missed.
- An `explanation` is a name, saved without checking that a rule of that name exists: rules live in the files given to `reconcile --rules`, and given those, `reconcile` notes an explanation that names none of them.
- `reconcile --map saved` still reads the `equal` declarations only. Use the listed file (above) to bring `differs` and `missing` entries into a reconciliation.
- Every table of one set is compared with every table of the other. On large sets, narrow them with datasets or folders.
