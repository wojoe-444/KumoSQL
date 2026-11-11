# GoogleSQL type inference

[Plain-language version](../docs_simple/type-inference.md)

`kumosql.googlesql_types` works out the output schema of a BigQuery query, STRUCT and ARRAY included, without running it or asking BigQuery: which columns the query returns, with which names and which types. It is a conservative type checker: it gives a type only when GoogleSQL's own rules fix one, and answers **unknown** for everything else. A wrong type would be worse than no type, because a caller (a schema-change check, a prover's column model, a lineage view) would build on it.

The checker is a library. The agent server's `check_query` is its first caller ([operand checks](#operand-checks-in-check_query)); the other hooks planned for it are [at the end](#planned-hooks). How well it does is scored by the [GoogleSQL type inference eval](evals/googlesql-types.md); the numbers are on that page and in `benchmarks/results/googlesql-types.json`, not here.

| Module | What it holds |
| --- | --- |
| `kumosql/googlesql_types.py` | `GType`, `parse_type`, `Catalog`, `infer`, `TypedQuery`; name resolution, `FROM` items, set operations, coercion and supertypes |
| `kumosql/googlesql_signatures.py` | The result type of each operator and built-in function, from the GoogleSQL function reference |
| `kumosql/googlesql_pipe_types.py` | Pipe syntax (`FROM t \|> WHERE ... \|> SELECT ...`), operator by operator |
| `kumosql/type_operands.py` | The comparison, `BETWEEN`, `IN`, join, `UNION` and signature checks that `check_query` runs on a typed query (not part of `infer`) |
| `kumosql/project_types.py`, `kumosql/schema_snapshot.py` | Column types for a whole project: from a schema snapshot, carried through the models in dependency order |
| `tools/googlesql_types_eval.py` | The labelled eval against the GoogleSQL compliance tests |
| `tools/googlesql_types_scan.py` | The unlabelled scan over real BigQuery SQL: findings must stay at zero |

## Quick start

```python
from kumosql.googlesql_types import Catalog, infer

catalog = Catalog.from_types({
    "shop.orders": {
        "id": "INT64",
        "amount": "NUMERIC",
        "tags": "ARRAY<STRING>",
        "buyer": "STRUCT<name STRING, age INT64>",
    },
})

typed = infer("SELECT id, amount * 2 AS twice, buyer.name, ARRAY_LENGTH(tags) AS n FROM shop.orders", catalog)
[(c.name, str(c.type)) for c in typed.columns]
# [('id', 'INT64'), ('twice', 'NUMERIC'), ('name', 'STRING'), ('n', 'INT64')]
typed.findings   # ()   nothing certainly wrong
typed.error      # None
typed.complete   # True: every output column has a complete type
```

Pipe syntax goes through the same call:

```python
infer("FROM shop.orders |> WHERE id > 1 |> EXTEND id * 2 AS d |> SELECT id, d", catalog).columns
# id INT64, d INT64
```

## API

### Types: `GType` and `parse_type`

`GType(kind, element=None, fields=())` is a frozen dataclass. `kind` is `INT64`, `FLOAT64`, `NUMERIC`, `BIGNUMERIC`, `BOOL`, `STRING`, `BYTES`, `DATE`, `DATETIME`, `TIME`, `TIMESTAMP`, `INTERVAL`, `JSON`, `GEOGRAPHY`, `ARRAY`, `STRUCT` or `RANGE`, or one of the GoogleSQL-only kinds a schema may carry (`INT32`, `UINT32`, `UINT64`, `FLOAT32`, `UUID`). `GType.array(element)`, `GType.range(element)` and `GType.struct([(name, type), ...])` build the parameterised ones. STRUCT field names are part of the type and keep the case they were written in; `t.field("name")` finds a field case-insensitively and gives `None` if it is absent, unknown or ambiguous.

`t.sql()` (also `str(t)`) prints the type as BigQuery does, with `?` for a part that is not known (`ARRAY<?>`). `t.complete` is false when any part is unknown.

`parse_type(text)` reads GoogleSQL type text and BigQuery schema spellings: `ARRAY<STRUCT<a INT64, b STRING>>`, `INTEGER`, `FLOAT` (FLOAT64 here, as in BigQuery schemas), `RECORD`, `STRING(10)`, `NUMERIC(10, 2)`. It returns `None` for text that is not a type.

### Catalog

A `Catalog` holds table schemas, found under each spelling of their name (`project.dataset.table`, `dataset.table`, `table`). A spelling that two tables share finds neither, so an ambiguous name is unknown rather than a guess.

| Constructor or method | Use |
| --- | --- |
| `Catalog.from_types({table: {column: type}}, functions=(), complete=False)` | Type text or `GType` per column; pass a list of `(column, type)` pairs to keep the column order and repeat a name |
| `Catalog.from_fields({table: [Field, ...]}, functions=(), complete=False)` | `dryrun.Field` objects (anything with name, type, mode and fields): REPEATED fields become arrays, RECORD fields structs, REQUIRED is kept |
| `catalog.add(name, columns)` | Add a table from `Column(name, type, required)` objects, for example the inferred columns of an upstream model, in dependency order |
| `functions=` | User-defined functions: a list of names (result type unknown) or a mapping from name to return type. A call to one is never typed as the built-in of the same name |
| `catalog.add_function_sql(statement)` | Register the function a `CREATE [TEMP] [AGGREGATE \| TABLE] FUNCTION` statement defines, from its `RETURNS` type or, without one, the type its SQL body gives for the declared parameter types. Returns `False` for a statement it cannot read |
| `catalog.add_table_function(name, columns)` | A table-valued function with its result schema |
| `complete=True` | Say that the catalog lists every table the query can read; see [unknown tables](#the-complete-flag-and-unknown-tables) |

### `infer`

`infer(sql_or_tree, catalog=None, dialect="bigquery")` takes SQL text or a parsed sqlglot tree (which it never modifies) and returns a `TypedQuery`:

| Member | Meaning |
| --- | --- |
| `columns` | A tuple of `Column(name, type, required)`, one per output column: `name` is `None` for an anonymous column, `type` is `None` when not known, `required` is `True` only for a column that can never be NULL. `columns` itself is `None` when even the width is unknown (`SELECT *` over a table with no schema, a statement that is not a query) |
| `complete` | Whether every output column has a complete type |
| `type_of(node)` | The type of any expression node of the typed tree (kept outside the AST, by node identity), or `None` |
| `relation(node)` | The columns of a `FROM` item (table, subquery, `UNNEST`, CTE reference), or `None` |
| `findings` | Errors the query certainly has; see [findings](#findings) |
| `error` | Why nothing was typed (`parse error`, `not a single statement`, `not a query`, `pipe syntax is not typed`, `Dataform placeholder in the query`, ...), or `None` |
| `tree` | The parsed tree |

Names follow GoogleSQL. A column reference or path takes its last identifier as its implicit alias; a range variable on its own is a STRUCT of its table's columns (or the row value of a value table such as `UNNEST`); range variables are looked up before columns; `UNNEST` of an array of structs makes the struct's fields columns. Output types are given as if the query were valid, since a query that is invalid has no type to be wrong about.

## How unknown works

The rule is one line: **anything uncertain is unknown.** A type is given only when

- GoogleSQL's [conversion rules](https://github.com/google/googlesql/blob/d82db99/docs/conversion_rules.md) (supertypes, coercion, literal coercion) fix it, or
- the function's signature in the GoogleSQL reference fixes it from the argument types (`googlesql_signatures.py`).

Everything else gives `None`: a function the signature table does not know, a call sqlglot built itself with no written name (unless its node class has a single source in BigQuery syntax), a user-defined function with no declared return type, a column of a table the catalog does not have, a construct the checker does not model. Unknown spreads cautiously: an operator on an unknown operand is unknown, a set operation with an unknown branch column is unknown in that column only, and `columns` is `None` only when the width itself is unknown. A partially known type prints with `?` and has `complete == False`; the eval counts it as unknown, not as partly right.

The costs and benefits are asymmetric on purpose. The [eval](evals/googlesql-types.md) scores an exact type as a hit, an unknown as a miss that is *not* a failure, and a different type as wrong, which must stay at zero. Work on the checker raises the exact count by teaching it more of GoogleSQL's rules, never by guessing.

## The `complete` flag and unknown tables

A catalog normally lists some of the tables a query reads, so a table or column it does not know is just unknown. `Catalog(..., complete=True)` says the catalog lists **every** table the query can read (a full schema fetch of a dataset, say). Only then does a missing table become a finding, and only then can a column missing from a table whose full schema is known be reported:

```python
partial = Catalog.from_types({"shop.orders": {"id": "INT64"}})
infer("SELECT id FROM other.t", partial).findings            # (): the catalog may just not know other.t
full = Catalog.from_types({"shop.orders": {"id": "INT64"}}, complete=True)
infer("SELECT id FROM other.t", full).findings               # unknown_table: table other.t is not in the catalog
```

The columns of an unknown table are unknown either way; `complete` only decides whether that is also reported as an error. Do not set it unless the catalog really is the whole world: a query can read a table in a dataset the catalog never fetched, a view, a wildcard table or an `INFORMATION_SCHEMA` view. `unknown_column` for a plain name needs every `FROM` item in scope to have a known schema.

## Literals, coercion and supertypes

These follow `docs/conversion_rules.md` in google/googlesql at the pinned commit.

- **Literals are not their types.** `1` is an INT64 literal, `1.5` a FLOAT64 literal, `'x'` a STRING literal, `NULL` an untyped NULL and `[]` an untyped empty array. A literal coerces where its type would not: an integer literal to NUMERIC, BIGNUMERIC, FLOAT64 (and INT32, UINT32, UINT64); a float literal to NUMERIC, BIGNUMERIC (and FLOAT32); a string literal to DATE, DATETIME, TIME and TIMESTAMP. An untyped NULL is whatever its neighbours need, and INT64 when nothing does; `[]` is `ARRAY<INT64>` likewise. A value read back from a column is no longer a literal.
- **Non-literal coercion.** INT64 coerces to NUMERIC, BIGNUMERIC and FLOAT64; NUMERIC to BIGNUMERIC and FLOAT64; BIGNUMERIC to FLOAT64; DATE to DATETIME. A string never coerces to another type except as a literal. INT64 and FLOAT32 have the supertype FLOAT64, as the documented examples say, though the supertype table does not list it.
- **The FLOAT64 / NUMERIC / BIGNUMERIC candidate rule.** The exact numeric types have several supertypes (INT64 has INT64, NUMERIC, BIGNUMERIC and FLOAT64), and GoogleSQL does not pick the widest: its common-supertype routine accepts **FLOAT64 only when some input is floating point (a float literal counts), NUMERIC only when some input is NUMERIC, and BIGNUMERIC only when some input is BIGNUMERIC**. The checker lists the candidates most specific first and takes the first that this rule allows and that every literal coerces to. So `INT64` with `INT64` stays `INT64`, `INT64` with a NUMERIC column is `NUMERIC`, `INT64` with `2.5` is `FLOAT64`, and `NUMERIC` with `2.5` is `NUMERIC` (the literal coerces down). `UINT64` with a signed integer has no supertype and stays unknown.
- **STRUCT supertypes.** Structs combine field by field, with the same arity. The field **names are those of the first argument that is not an untyped NULL**; later arguments' names are ignored (`STRUCT(1 AS a)` union `STRUCT(2 AS b)` is `STRUCT<a INT64>`), and a field that comes only from an untyped NULL gives its nested struct no names. A struct *literal* (all fields literals) coerces its fields as literals; any other struct's fields are typed values. When the checker cannot tell which a constructor is, the answer is trusted only if both readings agree. Arrays that differ only in struct field names are the same type; the first one's names are kept.
- **Operators and functions** take their result type from `googlesql_signatures.py`: the unary and binary numeric tables (`+`, `-`, `*`, `/`, `SUM`, `AVG`, `ROUND`, ...), comparison and logic (BOOL), string, date and time, array, JSON, window and aggregate functions, `CAST`/`SAFE_CAST` (including `FORMAT`), `COALESCE`, `IF`, `CASE`, `IFNULL`, `ARRAY_AGG`, `STRUCT`, and `WITH(var AS expr, result)`.

## Set operations

`UNION`, `INTERSECT` and `EXCEPT` (with `ALL` or `DISTINCT`) type each output column as the supertype of its branches, positionally, and the output is no longer a literal. Nested operations of the same kind and mode are one n-ary operation, as in GoogleSQL. Branches that are value tables (`SELECT AS VALUE`) combine as values; mixing a value table with a column list is unknown.

| Mode | Column matching |
| --- | --- |
| positional (no mode) | by position; a different width is a `set_operation_width` finding |
| `BY NAME`, `STRICT CORRESPONDING` | by name; every branch must have the same names |
| `CORRESPONDING` (`INNER`) | the names every branch has |
| `LEFT [OUTER]` | the first branch's names |
| `FULL [OUTER]` | every name, the first branch's first |
| `... BY (a, b)` / `ON (a, b)` | the listed names, in that order |

A name a branch lacks is padded with NULL, which does not affect the type. Anonymous or duplicate column names, or a name list the branches cannot satisfy, make the result unknown. When the types of a column have no common supertype the output is unknown, and `set_operation_type` is reported only when every branch type is complete and they certainly belong to different families (a number and a STRING column, say); a string *literal* beside a number is left alone because a literal can coerce to several types.

## Pipe syntax

`FROM t |> WHERE ... |> SELECT ...` is typed operator by operator by `googlesql_pipe_types.py`, working on the SQL text. sqlglot reads pipe queries by rewriting them into a chain of `__tmpN` CTEs, which loses information (it merges a `WHERE` or `LIMIT` into the step before and renames tables), so a tree that sqlglot already rewrote is left untyped (`error == "pipe syntax is not typed"`); pass the SQL **text** to `infer` to type pipe queries. The checker replaces each chain by a placeholder subquery, types the chain from its first query through each operator over the running relation, and reuses the ordinary machinery for each step.

Modelled: `SELECT`, `EXTEND`, `WINDOW`, `SET`, `DROP`, `RENAME`, `AS`, `AGGREGATE` (with `GROUP BY` and `GROUP AND ORDER BY`), `DISTINCT`, `PIVOT`, `UNPIVOT`, `MATCH_RECOGNIZE`, `ALIGN`, joins, `UNION`/`INTERSECT`/`EXCEPT`, `WITH`, `DESCRIBE`, and the operators that keep the relation as it is (`WHERE`, `ORDER BY`, `LIMIT`, `TABLESAMPLE`, `ASSERT`, `STATIC_DESCRIBE`). An operator not on the list, or one whose text cannot be read exactly, makes the relation unknown, and later operators on an unknown relation stay unknown. No findings are reported for pipe queries: a name the checker cannot resolve there is an unknown type, not an error. A range variable (`FROM t |> WHERE t.a`) survives only the operators that keep the table as it is, and a query where a range variable has a column's name is left unknown.

## Findings

A `Finding(code, message, node)` is a claim that the query is **certainly invalid**, so findings are rare and the [scan](#the-scan-tool) holds them at zero on queries that run. The codes in `FINDING_CODES`:

| Code | Reported when |
| --- | --- |
| `unknown_table` | the catalog is `complete` and the table is not in it |
| `unknown_column` | a name resolves in no range variable, in a scope where every table's schema is known |
| `ambiguous_column` | a name matches columns of more than one range variable |
| `set_operation_width` | set-operation branches have different numbers of columns |
| `set_operation_type` | a set-operation column has certainly no common type |
| `invalid_field_access` | a field access on a scalar value, or a field a complete STRUCT type lacks |
| `star_except_missing` | `SELECT * EXCEPT (c)` or `REPLACE` names a column the star does not have (columns become unknown) |
| `incompatible_operands` | **declared but not emitted yet** |
| `no_matching_signature` | **declared but not emitted yet** |

`incompatible_operands` and `no_matching_signature` are in the list so callers can already handle them; today an operator or function whose operands no signature accepts (`INT64 + STRING`, `SUBSTR(int_col, 1)`) simply gets an unknown type and no finding. The tests pin this: such a type must stay unknown whether or not the finding is later reported.

### Operand checks in `check_query`

`incompatible_operands` and `no_matching_signature` are still not emitted by `infer`. The agent server's `check_query` finds them itself from the types `infer` gives (`kumosql/type_operands.py`), so the library, its scan and its eval are unchanged. The checks run only when both operands have a known scalar type: a comparison, `BETWEEN` or `IN` between families that have no common type (a number, `STRING`, `BYTES`, `BOOL`, `DATE`, `DATETIME`, `TIMESTAMP`, `TIME`; `DATE` with `DATETIME` is allowed, and a quoted text literal may become a date or time), a number literal against a non-number column, a numeric operator or function whose argument types appear in no row of the signature tables (`MOD(FLOAT64, INT64)`, `STRING + INT64`, `SUM(STRING)`). Two different numeric types are never reported, and an operand of unknown type is returned to the caller as unknown rather than guessed. `USING` joins compare the two columns' types, and `UNION` branches that disagree are the `set_operation_type` finding above. A scan of every model of the fixture projects (with the types carried through the models) and of the Spider 2.0 gold queries gave no operand finding, which is the bar for any new check here: a finding on SQL that runs is a bug in the check.

## Dataform masks stay untyped

KumoSQL's Dataform loader replaces `${ref(...)}`, `${self()}` and other template expressions with identifiers of the form `__sqlx_token_N__` before parsing. A mask can stand for anything: a table, a column list, a whole expression. `infer` therefore returns no columns and `error == "Dataform placeholder in the query"` for any query that contains one. Raw `${...}` text does not parse and gives a parse error. To type a model, pass SQL whose references are real table names (for example after the project's references are resolved), or add the upstream models to the catalog under those names. In the scan of the open-source Dataform projects many models are untyped for this reason; that is the cautious behaviour, not a bug.

## sqlglot versions

The project allows `sqlglot>=26.0,<31`; the checker is developed on 30.x and its tests allow for 26.0.0, and the same code gives different coverage on the two because the parsers differ. The rule that makes this safe is the usual one: what an old sqlglot cannot parse is unknown, never wrong.

- On 26.0.0, queries using `BY NAME` / `CORRESPONDING` set-operation modes, `ARRAY_ZIP`'s named arguments, `GRAPH_TABLE` and aggregate `WHERE` filters do not parse, so they are unknown. The eval's test therefore has a lower exact floor for old sqlglot (see [the eval page](evals/googlesql-types.md#how-to-run)). It is still 0 wrong.
- `x.array[0]`, a field named like a type keyword: 26.0.0 drops the `x.` and reads an array literal; newer sqlglot rejects it. The checker finds the pattern in the SQL text and leaves the query untyped on either version.
- With the compiled sqlglot (`sqlglotc`) a PROTO extension access `value.(pkg.extension)` raises a `TypeError` in the parser; the query has no types there. The eval's floor allows for the few columns involved.
- Only SQL text is guaranteed; a pre-parsed tree carries whatever its sqlglot did, and a tree already rewritten from pipe syntax is rejected (above).

## The scan tool

`python tools/googlesql_types_eval.py` scores types against labels. `python tools/googlesql_types_scan.py` asks the opposite question on real SQL that has no labels: every finding is a claim that the query is invalid, and the corpora hold queries that run, so **the target is zero findings and zero crashes**. It types the Spider 2.0 BigQuery gold queries (`tests/fixtures/spider2/gold`) and the open-source Dataform and plain-SQL BigQuery projects (`tests/fixtures/bq_corpora/*`, each model's query, incremental query and operations), with an empty catalog by default, so a finding that needs a schema must stay silent.

```
python tools/googlesql_types_scan.py                 # both corpora, empty catalog
python tools/googlesql_types_scan.py --chain         # also add each model's inferred columns to the catalog, in dependency order
python tools/googlesql_types_scan.py --corpus spider2 --samples 10
python tools/googlesql_types_scan.py --json          # everything, for checking by hand
```

It prints the queries typed, scripts skipped (a script that declares variables can read names from outside the query), the reasons queries stayed untyped, crashes and findings per code with samples; the exit status is 1 if there is any finding or crash. A new finding on this scan is a bug in the checker until someone has read the query and shown it really is invalid.

## Limits and non-goals

- **Not a validator.** It reports invalidity only where it is certain, and a query it does not flag may still fail in BigQuery. It checks neither privileges, quotas, partition filters nor run-time errors.
- **Graph queries (`GRAPH_TABLE`, `MATCH`), PROTO and ENUM values, `MAP` and `MEASURE` columns** are not modelled; a query that depends on them is unknown. The eval's headline excludes the columns whose labels use these types.
- **GoogleSQL-only types** (`INT32`, `UINT32`, `UINT64`, `FLOAT32`, `UUID`) pass through when a column has one, but are rarely computed with. A `CAST(x AS UINT64)` or `FLOAT32` does not parse in the sqlglot versions in use, so the whole query is unknown.
- **Scripts.** `infer` takes one query. Multi-statement scripts, `DECLARE`d variables and parameters (`@x`) are the caller's to split and resolve (the scan shows one way).
- **Pure functions of the schema.** It never reads data, statistics or the live catalog, and makes no cloud call.
- **Names only as written.** Quoting and case follow BigQuery's rules for the cases the eval covers; a collation, a case-insensitive dataset setting or a wildcard-table suffix is not modelled.
- **The labels are one source.** The eval measures agreement with the reference implementation's printed types on the GoogleSQL compliance tests. It does not show agreement with BigQuery on every query.

## Planned hooks

None of these exist yet; this page changes when one lands.

- **schema_change**: use inferred output columns to tell whether a column retype reaches an output and changes its type, which the [schema-change bench](evals/schema-change-bench.md) lists as a limit today (retypes feeding a `UNION` are not scored because the supertype is not modelled).
- **set_operation_types**: use the supertype rules to give the output types of `UNION` models in the whole-pipeline views.
- **prover_schema / prover_context**: give the prover's schema and column model the inferred types of derived tables, so a column without a declared type stops being unknown.
- **infer_pipeline**: run `infer` over a loaded Dataform project in dependency order, putting each model's columns in the catalog for the ones downstream (the scan's `--chain` already does this by hand).
