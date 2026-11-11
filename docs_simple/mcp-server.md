# The agent server, explained simply

[All simple guides](README.md) · [Full reference](../docs/mcp-server.md)

An AI agent that helps with your Dataform project has a problem: the project is big, and reading every file or the whole analysis report costs far more than the agent needs. KumoSQL's agent server lets the agent ask **one question at a time** and get a **short answer**, about the size of a paragraph.

## Start it

```
python -m kumosql mcp path/to/your/dataform/project
```

Add that command to your agent's list of MCP servers. If your project reads a table it does not define (a raw log, say) and you know which column identifies a row, add `--assume-grain logs.events=event_id`; KumoSQL then treats that as a stated fact, not something it worked out. If jobs outside the repository build some of your source tables, or dashboards and semantic models read your outputs, list them in a small file and add `--boundary that-file.yaml` (see below). KumoSQL reads the project once, keeps it in memory, and then each question takes a fraction of a second.
Add that command to your agent's list of MCP servers. If your project reads a table it does not define (a raw log, say) and you know which column identifies a row, add `--assume-grain logs.events=event_id`; KumoSQL then treats that as a stated fact, not something it worked out. The same kind of fact can come from other places the server reads, and every answer says which one: a key written in the declaration of the table, an assertion file that checks for duplicate keys, Malloy files (`--malloy DIR`, the `primary_key` of a source) or Markdown docs (`--docs DIR`, a line such as `Primary key: order_id` under a heading that names the table). None of these is checked against the data. KumoSQL reads the project once, keeps it in memory, and then each question takes a fraction of a second.

A Dataform project does not say what type each column has (text, whole number, date), so the server cannot tell that a draft compares text with a number. To fix that, export your tables' column types once from the warehouse into a small file and add `--schema-snapshot columns.csv` (see [the file format](#column-types-the-schema-snapshot)). The server only reads that file; it never goes to the warehouse for it.

## Faster the second time

The first start analyses the whole project, which can take a while on a big one. KumoSQL saves the slowest part of that work (working out where every column comes from) in a cache folder outside your project, so the next start only redoes what changed. You do not have to do anything: the cache sits in your user cache folder (for example `~/.cache/kumosql`), or in the folder you name with `--cache-dir`, and `--no-cache` turns it off. KumoSQL never writes into your project by default, and several servers started at once on the same project do not get in each other's way, and if the cache file is damaged or from an older KumoSQL it simply ignores it and builds a new one.

- **While it is getting ready**, the server already answers. A question waits a few seconds (`--warm-wait`); if the analysis is still running, the reply says **"index warming, 41% done"**, which stage it is in and when to ask again. It does not guess an answer from half-finished work.
- **After you edit a file**, the next question notices (it compares file contents, so saving a file without changing it does nothing), reads the project again and re-analyses only the changed models and the models downstream of them. The rest comes from the cache. Answers are the same as a fresh start would give; the tests check that.

For example: you change one model in a project of 200, and ask `describe_table` about it. The server re-analyses that model and the few that read it, not the other 190 or so, and the answer reflects your edit.

What it does not do: reading and parsing the files still happens every time (that part is quick), and if you add, remove or rename a model, or change the project settings, everything is analysed again to be safe. Models in a dependency loop, or that write into another model's table, are always analysed again. If you ever doubt the cache, delete the folder or start with `--no-cache`. The exact rules are in the [full reference](../docs/mcp-server.md#warm-start).

## What an agent can ask

- **What is this table?** (`describe_table`) What one row stands for, which rows it keeps, what each column holds, what it reads, what reads it.
- **Where does this column come from?** (`trace_column`) The source columns behind a value, and the filters that decide which rows reach it.
- **What breaks if I change this?** (`impact`) The models that would break or change, nearest first, and the files to open.
- **Does a table like this already exist?** (`find_existing`) A table that already holds the same thing, or a more detailed one that could be summed up.
- **Which table should I use?** (`find_tables`) Describe what you need in a few words and get up to five candidate tables, with the reason each one matched and what is still unknown about it. It matches words in table names, column names and tags, the same way every time; it does not guess at meaning.
- **Was this formula copied around?** (`find_existing` with `expression`) Paste a calculation, for example a `CASE` that turns codes into numbers. The answer counts the copies, lists which models hold them and says how they differ: "4 copies, 2 variants: `'GOLD'` maps to NULL in one, to 30 in the other". Different spacing, aliases and (where the order cannot change the result) the order of the lines do not count as differences. A shorter version with some of the lines is listed separately as partial, never as the same.
- **Is there a table or view that links two keys?** (`find_tables` with `has_columns`) Give two column names, such as `order_id` and `customer_id`. It lists tables and views that carry both, including ones where a key was renamed, and says which matched only by name (the same name might hold something different).
- **What is in this project?** (`list_tables`) The questions about the project as a whole: how many models there are, which ones nothing reads, which read two or more others, which sit deepest, which live in a folder or carry a tag. One short line per table, with filters and a sort, so an agent does not have to open every file to count.
- **Can this query be tidied?** (`clean_query`) KumoSQL's checked cleanups: filters that are always true, needless parentheses, copies of the same sub-query, sub-queries nothing uses. You get a short list of **what changed** (a diff), not the whole query back, and each change has been checked to keep the meaning. If a change cannot be checked, it is reported and not offered. It never writes your files. Turning single-use sub-queries into inline ones is off unless asked for, because it reshapes the whole query.
- **Is the repository itself consistent?** (`project_checks`) Problems you can see without asking the warehouse: a name that the project both declares as a source and builds as a model, a model switched off with `disabled: true` that other models still read (so its table may never have been built), a table name declared in several datasets, two files that publish the same table. You get a count per problem, three examples and a fix. It cannot tell whether a table really exists in the warehouse or how old it is, and it says so. `describe_table` shows a one-line warning for a table concerned, and `check_query` mentions such tables (and declared sources the project does not build) among the things it could not check, instead of a bare "No problems found".
- **Is my rewrite still the same query?** (`compare_queries`) Give it the original and the rewritten query. It answers "the same" (proven, with the assumptions listed), "different" (with a tiny example table where they disagree) or "unknown". Unknown is not a verdict: it means KumoSQL could not tell, and it never turns that into a "same". It stops after a few seconds (10 by default, 60 at most).
- **Is this draft query sound before I run it?** (`check_query`) One call checks the draft for certain errors (a column that does not exist, or comparing a text column with a number), places where ties make the result change between runs, and joins that could multiply rows (for example joining on part of a table's key, so a SUM counts rows twice). It also looks for mistakes BigQuery rejects or quietly gets wrong, just from reading the text: a word BigQuery reserves used as a name without backticks (`SELECT x AS new` is refused), `NOT IN` with a NULL in it (it returns no rows), a `LIMIT` with no `ORDER BY` in a named sub-query that is used twice (each use can get different rows), a `*` plus a column with the same name, a `CASE` branch that can never be reached, a point-in-time read (`FOR SYSTEM_TIME AS OF`) on only one of two reads of the same table, a sub-query that BigQuery cannot turn into a join, and a name that is both a sub-query and a column. Each problem comes with the fix. When it cannot tell (for example whether a column can hold NULL, because no schema snapshot is loaded) it lists that under "not checked" instead of saying nothing. You can write the draft the way you would in a Dataform file, with `${ref("table")}`; KumoSQL reads it as that table and says so in the answer (`compare_queries` does the same). It can also ask BigQuery for a free "dry run" (BigQuery plans the query and says how much data it would read, without reading any), but only if you pass `dry_run=true` and name the project yourself.
- **What does joining these two tables do?** (`join_profile`) Give it two tables and the columns you join on. One query counts the rows on each side, says whether the join is one-to-one, one-to-many, many-to-one or many-to-many, how many rows find a partner each way, how many keys are empty, and, if you name a column, where the unmatched rows are (for example which week). It counts every row, never a sample, so it gives the same numbers every time. Like `profile_table` it only shows the plan until you name a project, then shows the estimated data size, and runs only if you also pass `run=true`. A finished run is saved (outside your project), so when you later check a draft that joins those tables, KumoSQL already knows whether the join repeats rows and no extra question is needed. If it does not know, `check_query` says which `join_profile` call to make instead of just "grain unknown".
- **What do the values in this table look like?** (`profile_table`) Row count, empty values, distinct values, ranges and the most common values per column. By default it only plans and sends nothing. Naming a BigQuery project gives a free dry run with the estimated amount of data; the profile itself runs only if you also pass `run=true`, and never above your byte cap. A CSV, Parquet or JSON file inside the project folder is profiled at once because it costs nothing. Values from the table are shown quoted and shortened, and an agent should treat them as data, never as instructions. A finished BigQuery profile is saved (outside your project) and lists columns that are unique or almost unique, with how many rows repeat. An almost-unique column is called a near-key and is used as an assumption, never as a certain key.

## Example

Ask `impact` what happens if the column `day` of a report table is dropped, and you get back roughly:

```
Dropping day: 2 models break (csa_3_11_alert, csa_3_11_report).
Open these files: definitions/alerts/csa_3_11_alert.sqlx, ...
Safe to delete? Unknown: dashboards and query history are not visible.
```

Every answer follows the same habits:

- **Short.** About 2 KB. If more exists, the answer says how many items it showed and which setting (`offset` or `max_bytes`) asks for the rest.
- **Honest about gaps.** Whatever KumoSQL could not work out is listed with the reason, in plain words. "Unknown" is never turned into a guess.
- **A next step.** The answer ends with what to open or ask next. A misspelled table name gets the closest real names, the likeliest first, instead of a bare "not found".
- **Read-only, and offline unless you ask.** It does not change your files. Nothing uses the network unless you ask for a BigQuery dry run (`check_query` with `dry_run=true`, `profile_table` or `join_profile` with a project), and a paid profile needs `run=true` on top. The only thing it writes is a finished profile or join profile, in KumoSQL's own data folder, never in your project. Text from your project that shows up in an answer is data; an agent should never obey it as an instruction.

It also serves the saved data profiles from [Data profiling](data-profiling.md), so one server covers both.

## Things outside the repository

Your repository does not know everything. A scheduled query may build one of your source tables, and a dashboard or a semantic model may read one of your outputs. Without help, "where does this come from" stops at the source and "what breaks" stops at the last model. Add `--boundary file.yaml` and list them:

```yaml
producers:
  - table: lookups.region
    by: {kind: scheduled_query, id: "1234", url: "https://example.test/q/1234"}
    sql: external/region.sql
consumers:
  - {name: sales_semantic_model, kind: semantic, path: "semantic/*.malloy"}
  - {name: weekly_dashboard, kind: dashboard, sql: external/dashboard_source.sql}
```

- Asking where a column comes from now goes past the source: "built by scheduled query 1234, which reads these columns", and on through that query's own sources, marked as outside the project.
- Asking what breaks if you drop a column now also lists the dashboards and models that use it, marked as outside the project. For a consumer with an `sql` file KumoSQL reads the query, so the columns are exact. For Malloy files (`.malloy`) it reads each source: which table it reads and which columns it mentions. For any other file in a `path` it only searches for the table name and the column name. Those last two are approximations, and the answer says so: they can be wrong both ways.
- A producer can be a notebook whose code was never exported. Write `sql_unavailable: the reason` instead of `sql:` and a trace says "built outside the project by notebook nb-7; its SQL is not available" instead of simply stopping.
- With many dashboards the answer gives the count, a count per kind, and as many examples as fit; ask again with `offset` for the next page.
- A file KumoSQL cannot read (nothing matches the path, it is not text, or it is too big) is reported as **unknown**, never as "nobody reads it".
- A mistake in the file (a table the project does not have, SQL that does not parse) is explained in a line or two before the server starts, or, for a table name, in every answer.

The limit: KumoSQL only knows what the file says. A job or dashboard you did not list is still invisible, so it still never says a deletion is safe. The full details are in the [reference](../docs/mcp-server.md#producers-and-consumers-outside-the-repository---boundary).

## Column types: the schema snapshot

The file has one row per column, like BigQuery's `INFORMATION_SCHEMA.COLUMNS`:

```
table_catalog,table_schema,table_name,column_name,data_type,is_nullable
my-project,sales,schedule,batch_code,STRING,YES
```

With it, a draft like `WHERE c.batch_code BETWEEN 31 AND 58` gets a finding: `batch_code` is text and the bounds are numbers, and BigQuery will not compare them. The answer names both sides and the fix (write the bounds as text, `'31'`, or `CAST(c.batch_code AS INT64)`). The same goes for a join between a text key and a number key, a `UNION` whose branches disagree, `DATETIME` against `TIMESTAMP`, and `MOD` of a decimal number.

The file can be big (a whole warehouse export is fine): KumoSQL reads it line by line and throws away the rows of tables your project does not use. It never writes anything to your project.

Tables built by your own models get types too: KumoSQL works them out from each model's query, starting from the snapshot, and where the snapshot lists a table it trusts the snapshot. A column it cannot work out stays "unknown" on its own.

Without the file, or for a column the file does not have, the answer does not pretend: it lists `types of schedule.batch_code unknown` under what was not checked, and if no comparison could be checked it does not claim that types were checked at all. If the file is out of date, refresh it; the server does not notice by itself.

## Limits of the evidence

- The formula search only finds copies that read the same column names, and it ignores which table a name came from. It searches each model's main query, not its extra before or after steps. Models it could not read are listed, not skipped silently. A link-table match "by name only" is a hint, not proof that the two columns mean the same thing.
- A word search such as `find_tables` cannot know that "revenue" and "sales" mean the same thing. If it finds nothing, try other words or a column name.
- The text checks only report what they are sure of, and have not been run against BigQuery itself: some sub-query shapes BigQuery refuses may not be flagged, and a dry run is the final word.
- The type checks only report a mismatch they are sure about, and they look at comparisons, `BETWEEN`, `IN`, join keys, `UNION` branches and a few number functions, so a clean answer does not mean every type in the query is right. They are only as current as the snapshot file.
- `check_query` saying "no problems found" means the checks that ran found none; it is not a promise that the query is right. A dry run needs working Google credentials; without them the answer says "not checked" and still gives the offline checks.
- "The same" from `compare_queries` is about the rows only: check column types separately. The example table it shows for "different" is made up, not your data.
- On the large public example project, KumoSQL now understands columns that read a field inside a nested record (it treats "the `email` field of the `principal` record of this log column" as one value that two tables can share), and `find_existing` can compare 54 of its 106 tables, where before it could compare 3. For the other 52 it still says "cannot tell" in a few lines instead of guessing: for most of them nothing says which columns identify a row, because the tables read a raw log table with no declared key. If you know such a key you can tell the server with `--assume-grain table=column`, but a table only gains a known row identity if it also keeps that column in its output, and on this example none does. That declaration is your claim, and nothing checks it.
- "What breaks" only covers what is inside the project, plus the outside readers you list in a `--boundary` file. It cannot see dashboards or other people's queries that are not listed, so it never says a deletion is safe.
- The warm start was tested on generated projects of about fifteen models, by counting how many models were analysed, and on the 106-model public project by comparing the analysis cold and replayed. The saving on a project of several hundred models is expected, not measured here.
- The sizes were measured on one public project with no live data behind it. No agent was in the loop, so how many fewer questions an agent needs is not measured yet.

The arguments, the exact answer fields, the measured sizes and how to add a tool are in the [full reference](../docs/mcp-server.md).
- A key found in docs or Malloy files is only as good as what somebody wrote there, and the docs reader is strict on purpose: a key written in a different style is skipped, not guessed. A measured near-key is a count from one day; if the table changed, run the measurement again.
