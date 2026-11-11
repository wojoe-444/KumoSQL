# Multi-statement scripts

[Plain-language version](../docs_simple/scripts.md)

BigQuery scripts (several statements run together, with variables, temporary tables, control flow and stored procedures) are broken apart wherever SQL comes in, and what they read and write is followed from statement to statement. The same code (`kumosql.scripts`) serves every entry point, so a script is understood the same way in each:

- a model written as a script in a `.sql` file or a Dataform `operations` action, and its `pre_operations` and `post_operations` blocks;
- stored-procedure bodies (`CREATE PROCEDURE`), when a script `CALL`s them;
- job history: a script's parent job and its child jobs (about 25,000 jobs in a large project) become one set of edges with the temporary tables folded away;
- observed usage (queries that are scripts are split before each statement is examined), the rewrite engine (rules apply inside blocks, one leaf statement at a time) and pasted SQL.

Table comparisons (`prove-tables`), the refactor search and table consolidation do not read a script model as a query: its result is its last statement and its DML changes tables, so they answer unknown (or refuse to fold) rather than compare its first `SELECT`.

## Splitting

KumoSQL has its own small lexer for this because sqlglot's BigQuery tokenizer treats `BEGIN` as a command and swallows the rest of the text. The lexer knows strings (single, double, triple-quoted, raw), comments (`--`, `#`, `/* */`), backticked names, and nested `BEGIN ... END`, `IF`, `CASE`, `LOOP`, `WHILE`, `REPEAT` and `FOR` blocks. A semicolon inside any of them never splits a statement. `split_script(text)` returns the statements with blocks opened; `parse_script(text)` keeps the nesting.

## What is kept, ignored and unknown

| | Statements |
| --- | --- |
| Kept | queries, `CREATE [TEMP] TABLE/VIEW ... AS`, `INSERT` (`SELECT` or `VALUES`), `MERGE`, `UPDATE`, `DELETE`, `EXPORT DATA ... AS SELECT` (reads only), `CREATE [TEMP] FUNCTION` and `TABLE FUNCTION` (a definition: its body's tables are reads, it is never a skipped step), `CALL` of a procedure defined in the project or script, `EXECUTE IMMEDIATE` of literal text (also `||` and `CONCAT` of literals) |
| Ignored | `DECLARE` and `SET` of scalars, `ASSERT`, transactions, `LOAD DATA`, definitions outside tables/views, `RAISE`, `RETURN`, `LEAVE`/`ITERATE`/`BREAK`, and the shell of `IF`, loops and `BEGIN ... END` |
| Unknown | `EXECUTE IMMEDIATE` of dynamic text (a variable, `FORMAT`, concatenation with a variable), `CALL` of a procedure nobody defines, a statement that does not parse |

Unknown is never guessed: a dynamic statement adds no edges, and the script is reported incomplete. A statement that does not parse, or whose first word is not a statement at all (a misspelled keyword), is *degraded*, not dropped: the tables it reads and writes are taken from its tokens (names after `FROM`, `JOIN`, `USING`, `TABLE`, `INTO`, `UPDATE`, `MERGE`, `CREATE ... TABLE|VIEW`, `LIKE` and `CLONE`, minus the names of CTEs it declares where they are in scope: after the CTE's body up to the parenthesis that closes its query, or from the `WITH` on for `WITH RECURSIVE`), so its graph edges stay, its columns are unknown, and one `parse_error` says where sqlglot stopped (line and column, no SQL text). Names in comments and strings are never tables, and neither is the `FROM` of `EXTRACT(DATE FROM ts)`, `TRIM(BOTH 'x' FROM s)`, `SUBSTRING`, `SUBSTR`, `OVERLAY` or `POSITION`, or of `IS [NOT] DISTINCT FROM`.

### Roles

`ALTER`, `DROP` and `TRUNCATE` on tables or views retain their write targets without claiming assigned column values.
`ALTER TABLE old RENAME TO new` also retains `old` as a read. All script outputs are available through
`Pipeline.table_writes()`, including multiple targets and pre/post operations. A temporary table's DDL stays local
to the script. `DELETE table WHERE ...` is understood alongside `DELETE FROM table WHERE ...`.

Every statement plays one role, and the later steps (column tracing, diagnostics, which models are trusted for dead columns) follow the role, not the shape of the syntax tree:

| Role | Statements | Columns | Counted as skipped |
| --- | --- | --- | --- |
| Query | `SELECT`, set operations | traced; the output columns are the projections | no |
| Write | `CREATE ... AS`, `INSERT ... SELECT`, `MERGE` | traced into the target | only when it is not the traced statement of a script |
| Constant | `INSERT ... (cols) VALUES ...`, `CREATE TABLE (col defs)` | the insert list; each value has no source column (a scalar subquery among them reads its table) | no |
| Definition | `CREATE [TEMP] FUNCTION`, `TABLE FUNCTION` (a `PROCEDURE` is read when called) | none; a call is a transform over its arguments | never |
| Reader | `DELETE ... USING`, `UPDATE ... FROM`, `CREATE ... LIKE`/`CLONE`, delete-only `MERGE` | tables only; the tables count as fully used | `UPDATE`, `LIKE` and `CLONE` write columns that are not traced, so yes; a delete writes none, so no |
| Opaque | a statement that does not parse | unknown; tables from its tokens | yes, with its kind |

A statement that writes no column (a `DELETE`, a delete-only `MERGE`) and a function definition never report a skip; a lone `UPDATE`, an `INSERT ... VALUES` without a column list or a `CREATE PROCEDURE` that nothing calls do, because columns they write (or statements they hold) are not traced. The `skipped_statements` text counts only statements that matter: `N statements; 1 traced, K not traced (kinds: insert x1, merge x2)`.

### Table functions

`FROM fn(TABLE x, n => 3)` reads `x` (a table, a CTE or a subquery) and, when `fn` is a `CREATE TABLE FUNCTION` defined in the project or the script, is replaced by the function's query: the table parameter becomes a CTE over the argument, scalar parameters become the argument expressions, so columns trace to `x`'s sources and the tables the body reads become reads. A call whose arguments do not fit the definition, or whose definition is unknown, stays an opaque relation: the table argument is still read, its columns are unknown and a `SELECT *` over it is an `unexpanded_star`, never a parse error. The function itself is never a table: it gets no graph node, no edge and no `external_tables` entry, and the model carries an informational `table_function` diagnostic (how many calls have no definition in the project) so the unknown columns have a stated reason.

## Following data between statements

- **Temporary tables.** `CREATE TEMP TABLE t AS SELECT ...` remembers what it was built from. A later table written from `t` traces to the real sources, and the temporary table is spliced into the final query as a common table expression, so column lineage reaches real columns. Redefinitions (`CREATE OR REPLACE TEMP TABLE t AS SELECT ... FROM t`) are versioned, and `INSERT` into a schema-only temporary table adds its sources by column.
- **`UPDATE` or `MERGE` on a temporary table** makes it opaque: tables it was fed from remain dependencies at table level, but its columns are reported unknown with reason `temporary_table`.
- **Script variables.** `DECLARE x DEFAULT (SELECT ... FROM t)` and `SET x = (SELECT ...)` carry their source tables to the statements that use `x` (edges are marked `via_variable`), including a `DEFAULT` that names an earlier variable. A variable that no statement reads adds no edge. A variable declared in `BEGIN ... END` ends with the block, and a `FOR` row variable with its loop, so a later table alias of the same name is not the variable.
- **Branches and loops.** A statement inside `IF`, `CASE`, a loop or an exception handler is a possible edge, so every arm counts. The tables a condition reads (`IF`, `ELSEIF`, `CASE` and `WHEN`, `WHILE`, `UNTIL`, a `FOR` query) are reads of the script and sources of every table written, temporary table built and variable set inside it, since whether those happen depends on them. A loop body is run until its variables stop changing, so a value set late in one iteration reaches a statement early in the next; a loop that does not settle is unknown.
- **Procedures.** `CALL` expands the body in its own scope: parameters hide the caller's variables of the same name and the caller's variables are unchanged afterwards, except that an `OUT` or `INOUT` parameter's final value is assigned to the variable passed for it. Recursion is cut at depth 8. A procedure that is defined but never called adds nothing.
- **`EXECUTE IMMEDIATE`.** Literal text is read as the statement it holds; anything computed at run time is unknown. `USING` values feed the text's `@name` and `?` parameters, and `INTO` gives the listed variables what the query reads (unknown text leaves them unknown too).
- **Transactions.** `ROLLBACK` brings the temporary tables back to what they were at `BEGIN TRANSACTION`, so values a rolled-back statement inserted are not in column lineage; the tables it read stay reads of the script. A `ROLLBACK` that may not run (inside a branch or handler) keeps the changes as possible.
- **Names.** `_SESSION.t` is the temporary table `t`. A `DROP` of a temporary table that may not run leaves it in place. After `SET @@dataset_id = 'x'` (and `@@dataset_project_id`), unqualified names are tables of that dataset; a value that is not a constant string is an unknown statement.
- **Columns other statements read.** Column lineage follows the traced output query. When another statement, a condition or a variable reads a table, `assess_change` on a column of it whose name the script mentions (or any column, after a `SELECT *`) lists the model as unknown with reason `script_columns`, and no column of that table is called dead. The same holds for pre and post operations.
- **Job history.** A `MERGE`, `INSERT`, `UPDATE` or `DELETE` job that carries its query text but no destination table gets its destination and sources from the statement, so a MERGE-built table (every Dataform incremental run) has its upstream edge in job-history analysis. `expand_script_jobs` joins child jobs to their parent script and replaces anonymous temporary datasets (leading underscore) with the real tables they were built from, ordered by creation time.

## MERGE

A `MERGE` is read as the values that flow into the target's columns: each `WHEN` clause becomes one arm of a `UNION ALL` that selects what the clause assigns from the `USING` source (a table, a subquery with CTEs, `UNNEST`, or a temporary table of the script), joined to the target on the `ON` condition and filtered by the clause's own `AND` condition. The ordinary column tracing then does the rest.

| Clause | Target columns | Read |
| --- | --- | --- |
| `WHEN MATCHED ... UPDATE SET c = e` | `c` from the columns of `e` (`t.v`, `v` and a struct field `t.a.b` all name the root column) | `ON`, the clause condition, any subquery in them |
| `WHEN NOT MATCHED [BY TARGET] ... INSERT (c) VALUES (e)` | `c` from `e`, paired by position; bare names are the source's | the same |
| `... INSERT ROW` | every source column to the target column of the same name | the same |
| `WHEN NOT MATCHED BY SOURCE ... UPDATE SET` | `c` from `e` (constants stay constants) | `ON` and the condition, including target columns |
| `... DELETE` | none | the condition and `ON` |

Several clauses union their sources per column. A script's last unconditional `MERGE` defines its output columns like a final query; earlier ones keep their table dependencies and are counted in `skipped_statements`. An operation that merges into its own table is traced the same way. An operation (a Dataform `operations` action) that merges or inserts into a different table that is known to the project (a declared source or another model) writes lineage rows into that table: the columns the statement writes come from the source columns, joined with whatever else writes the same column, and the table gets an edge from every table the statement reads in the graph and in impact. The target keeps its own schema, so a `SELECT *` over it is not narrowed to the written columns. Only the written columns are known, so the merged table keeps its schema from elsewhere and a `SELECT *` over it is never narrowed to them. Not understood means unknown: `INSERT VALUES (...)` without column names, `INSERT ROW` mixed with explicit columns (by-name, conservative), a delete-only `MERGE` (no columns to trace) and anything that does not parse keep the tables as dependencies and claim no columns. The tables they read count as fully used, so nothing downstream is called dead because of them, and a statement that writes no column is not reported as skipped.

## Diagnostics

Nothing in the UI explains scripts. Each script gets an informational `script_summary` diagnostic with counts (for example `script of 12 statements: 7 kept, 5 ignored, 1 unknown (execute_immediate x1)`), never SQL text. Two codes block completeness: `skipped_statements` (a script where some statements that write columns are not traced: `N statements; 1 traced, K not traced (kinds: ...)`; a definition, a delete and a traced statement never count) and `unparsed_operation` (a statement of an operation that could not be read at all: dynamic text or a call nobody defines). An operation statement that does not parse is a degraded one and reports the located `parse_error`, as in a query model. The graph, impact and dead-column views treat a model with either code as incomplete, as for any other gap.

## Limits

- Columns of a temporary table that `UPDATE` or `MERGE` changed are unknown; only tables are traced. `DELETE` and `TRUNCATE` remove rows only, so the columns keep their sources (the spliced query is a column-lineage view, not the table's rows).
- A variable edge can be false if a column of the same name shadows the variable.
- Dynamic table names inside a loop are unknown. Statements after `RETURN`, `LEAVE` or an error are still followed, as possible edges.
- Pre/post operations that stay as unresolved Dataform templates are reported at info level, not guessed.
- `EXPORT DATA` is kept for its reads only; the destination is not an edge.
- Scalar SQL functions are not inlined: a call is a transform over its arguments, and the tables its body reads are reads of every statement that calls it from the same script (not of calls to functions defined elsewhere). A call that reads a table without a column (`COUNT(*)` of a lookup) is a constant in column lineage, with the table as a dependency.
- A table function is inlined only when a plain SQL definition is in the project or the script; one deployed only in BigQuery stays opaque.
- `INSERT ... VALUES` without a column list has unknown columns (the target schema is not consulted).
- Table names taken from the tokens of a statement that does not parse can include a column that follows `FROM`/`JOIN` by mistake; they appear as external tables, never as columns.

## Evaluation

`python tools/script_bench.py [--cases 100] [--seed 1] [--write-results]` builds scripts from specs in Python, so the answer is known by construction and no parser produces the key: tricky splitting (strings with semicolons, triple-quoted and raw strings, comments), temporary-table chains, redefinitions, `INSERT` into temporary tables, `UPDATE`/`MERGE` on them, `MERGE` into real tables with random clauses (updates, inserts, `INSERT ROW`, deletes, `BY SOURCE`) over a table or a subquery, variables, branches, loops, exception handlers, literal and dynamic `EXECUTE IMMEDIATE`, procedures, ignored statements, `INSERT ... VALUES` (constants and a scalar subquery), table functions defined in the script and called with table and scalar arguments, scalar function definitions whose bodies read tables, statements that do not parse (with names in comments and strings that must not become tables) and random mixes of those. A job-history family turns scripts into child jobs with anonymous temporary tables and shuffled order. A public family runs the 22 scripting examples in `tests/fixtures/bq_syntax/sql/script` against hand-written labels in `benchmarks/script_cases/public_scripting.json`.

Scored apart: **correctness** (an edge, read or column the key lacks, a phantom table from a comment, string or ignored statement, or a guessed dynamic statement; all must be 0), **coverage** (exact, or unknown where the key says unknown) and **analysis** (edge and column precision and recall). Current result: 2,022 of 2,022 exact, 0 wrong (1,800 development-family scripts including 100 each of `MERGE`, `INSERT ... VALUES`, table-function calls, function definitions and statements that do not parse, 100 mixed, 100 job-history, 22 public examples); edges 4,200 of 4,200, 1,770 of 1,770 traced columns exact (temporary-table chains, `MERGE` targets with `ON` and condition columns counted as read, constant inserts, table-function and scalar-function calls). The four newest families were measured on `master` first and failed there (every case). Caveat: the families are ones the feature's author could think of and were tuned against while building it; there is no held-out family. `tests/test_script_bench.py` holds the floors; the numbers live in `benchmarks/results/script-splitting.json` and feed the scoreboard page.
