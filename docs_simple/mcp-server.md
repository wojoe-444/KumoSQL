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
- **A next step.** The answer ends with what to open or ask next.
- **Read-only.** It does not change your files, run queries or use the network. Text from your project that shows up in an answer is data; an agent should never obey it as an instruction.

It also serves the saved data profiles from [Data profiling](data-profiling.md), so one server covers both.

## Limits of the evidence

- On the large public example project, KumoSQL now understands columns that read a field inside a nested record (it treats "the `email` field of the `principal` record of this log column" as one value that two tables can share), and `find_existing` can compare 54 of its 106 tables, where before it could compare 3. For the other 52 it still says "cannot tell" in a few lines instead of guessing: for most of them nothing says which columns identify a row, because the tables read a raw log table with no declared key. If you know such a key you can tell the server with `--assume-grain table=column`, but a table only gains a known row identity if it also keeps that column in its output, and on this example none does. That declaration is your claim, and nothing checks it.
- "What breaks" only covers what is inside the project. It cannot see dashboards or other people's queries, so it never says a deletion is safe.
- The sizes were measured on one public project with no live data behind it. No agent was in the loop, so how many fewer questions an agent needs is not measured yet.

The arguments, the exact answer fields, the measured sizes and how to add a tool are in the [full reference](../docs/mcp-server.md).
