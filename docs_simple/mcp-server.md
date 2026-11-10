# The agent server, explained simply

[All simple guides](README.md) · [Full reference](../docs/mcp-server.md)

An AI agent that helps with your Dataform project has a problem: the project is big, and reading every file or the whole analysis report costs far more than the agent needs. KumoSQL's agent server lets the agent ask **one question at a time** and get a **short answer**, about the size of a paragraph.

## Start it

```
python -m kumosql mcp path/to/your/dataform/project
```

Add that command to your agent's list of MCP servers. If your project reads a table it does not define (a raw log, say) and you know which column identifies a row, add `--assume-grain logs.events=event_id`; KumoSQL then treats that as a stated fact, not something it worked out. KumoSQL reads the project once, keeps it in memory, and then each question takes a fraction of a second.

## What an agent can ask

- **What is this table?** (`describe_table`) What one row stands for, which rows it keeps, what each column holds, what it reads, what reads it.
- **Where does this column come from?** (`trace_column`) The source columns behind a value, and the filters that decide which rows reach it.
- **What breaks if I change this?** (`impact`) The models that would break or change, nearest first, and the files to open.
- **Does a table like this already exist?** (`find_existing`) A table that already holds the same thing, or a more detailed one that could be summed up.
- **Which table should I use?** (`find_tables`) Describe what you need in a few words and get up to five candidate tables, with the reason each one matched and what is still unknown about it. It matches words in table names, column names and tags, the same way every time; it does not guess at meaning.
- **What is in this project?** (`list_tables`) The questions about the project as a whole: how many models there are, which ones nothing reads, which read two or more others, which sit deepest, which live in a folder or carry a tag. One short line per table, with filters and a sort, so an agent does not have to open every file to count.
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
- The sizes were measured on one public project with no live data behind it. No agent was in the loop, so how many fewer questions an agent needs is not measured yet.

The arguments, the exact answer fields, the measured sizes and how to add a tool are in the [full reference](../docs/mcp-server.md).
