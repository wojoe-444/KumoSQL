# The agent server, explained simply

[All simple guides](README.md) · [Full reference](../docs/mcp-server.md)

An AI agent that helps with your Dataform project has a problem: the project is big, and reading every file or the whole analysis report costs far more than the agent needs. KumoSQL's agent server lets the agent ask **one question at a time** and get a **short answer**, about the size of a paragraph.

## Start it

```
python -m kumosql mcp path/to/your/dataform/project
```

Add that command to your agent's list of MCP servers. If your project reads a table it does not define (a raw log, say) and you know which column identifies a row, add `--assume-grain logs.events=event_id`; KumoSQL then treats that as a stated fact, not something it worked out. KumoSQL reads the project once, keeps it in memory, and then each question takes a fraction of a second.

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
- **What is in this project?** (`list_tables`) The questions about the project as a whole: how many models there are, which ones nothing reads, which read two or more others, which sit deepest, which live in a folder or carry a tag. One short line per table, with filters and a sort, so an agent does not have to open every file to count.
- **Can this query be tidied?** (`clean_query`) KumoSQL's checked cleanups: filters that are always true, needless parentheses, copies of the same sub-query, sub-queries nothing uses. You get a short list of **what changed** (a diff), not the whole query back, and each change has been checked to keep the meaning. If a change cannot be checked, it is reported and not offered. It never writes your files. Turning single-use sub-queries into inline ones is off unless asked for, because it reshapes the whole query.
- **Is the repository itself consistent?** (`project_checks`) Problems you can see without asking the warehouse: a name that the project both declares as a source and builds as a model, a model switched off with `disabled: true` that other models still read (so its table may never have been built), a table name declared in several datasets, two files that publish the same table. You get a count per problem, three examples and a fix. It cannot tell whether a table really exists in the warehouse or how old it is, and it says so. `describe_table` shows a one-line warning for a table concerned, and `check_query` mentions such tables (and declared sources the project does not build) among the things it could not check, instead of a bare "No problems found".
- **Is my rewrite still the same query?** (`compare_queries`) Give it the original and the rewritten query. It answers "the same" (proven, with the assumptions listed), "different" (with a tiny example table where they disagree) or "unknown". Unknown is not a verdict: it means KumoSQL could not tell, and it never turns that into a "same". It stops after a few seconds (10 by default, 60 at most).
- **Is this draft query sound before I run it?** (`check_query`) One call checks the draft for certain errors (a column that does not exist), places where ties make the result change between runs, and joins that could multiply rows (for example joining on part of a table's key, so a SUM counts rows twice). Each problem comes with the fix. You can write the draft the way you would in a Dataform file, with `${ref("table")}`; KumoSQL reads it as that table and says so in the answer (`compare_queries` does the same). It can also ask BigQuery for a free "dry run" (BigQuery plans the query and says how much data it would read, without reading any), but only if you pass `dry_run=true` and name the project yourself.
- **What do the values in this table look like?** (`profile_table`) Row count, empty values, distinct values, ranges and the most common values per column. By default it only plans and sends nothing. Naming a BigQuery project gives a free dry run with the estimated amount of data; the profile itself runs only if you also pass `run=true`, and never above your byte cap. A CSV, Parquet or JSON file inside the project folder is profiled at once because it costs nothing. Values from the table are shown quoted and shortened, and an agent should treat them as data, never as instructions.

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
- **Read-only, and offline unless you ask.** It does not change your files. Nothing uses the network unless you ask for a BigQuery dry run (`check_query` with `dry_run=true`, `profile_table` with a project), and a paid profile needs `run=true` on top. Text from your project that shows up in an answer is data; an agent should never obey it as an instruction.

It also serves the saved data profiles from [Data profiling](data-profiling.md), so one server covers both.

## Limits of the evidence

- A word search such as `find_tables` cannot know that "revenue" and "sales" mean the same thing. If it finds nothing, try other words or a column name.
- `check_query` saying "no problems found" means the checks that ran found none; it is not a promise that the query is right. A dry run needs working Google credentials; without them the answer says "not checked" and still gives the offline checks.
- "The same" from `compare_queries` is about the rows only: check column types separately. The example table it shows for "different" is made up, not your data.
- On the large public example project, KumoSQL now understands columns that read a field inside a nested record (it treats "the `email` field of the `principal` record of this log column" as one value that two tables can share), and `find_existing` can compare 54 of its 106 tables, where before it could compare 3. For the other 52 it still says "cannot tell" in a few lines instead of guessing: for most of them nothing says which columns identify a row, because the tables read a raw log table with no declared key. If you know such a key you can tell the server with `--assume-grain table=column`, but a table only gains a known row identity if it also keeps that column in its output, and on this example none does. That declaration is your claim, and nothing checks it.
- "What breaks" only covers what is inside the project. It cannot see dashboards or other people's queries, so it never says a deletion is safe.
- The warm start was tested on generated projects of about fifteen models, by counting how many models were analysed, and on the 106-model public project by comparing the analysis cold and replayed. The saving on a project of several hundred models is expected, not measured here.
- The sizes were measured on one public project with no live data behind it. No agent was in the loop, so how many fewer questions an agent needs is not measured yet.

The arguments, the exact answer fields, the measured sizes and how to add a tool are in the [full reference](../docs/mcp-server.md).
