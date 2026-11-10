# The field trial, explained simply

[All simple guides](README.md) · [Full reference](../docs/field-trial.md)

The question: does the [agent server](mcp-server.md) actually help an AI agent answer a data analyst's questions, or does it just look neat? To find out, ten small questions about a real example project were each given to an AI agent twice, once with the server and once without it (only file search and the older command line). We counted how many steps each took and checked each answer.

## An example

"Who reads the busiest table?" With the server, the agent asked once and got the list. Without it, the agent searched the files and ran a report, four steps. Both answers were right.

## What came out

- Every answer was right both ways, except one column-tracing question where neither answer matched exactly. The server's answer named the right column but dropped a placeholder prefix, and the other stopped one level short.
- With the server the agent usually needed fewer steps, but the difference is small and this is one run of each question.
- On three questions the agent ignored the server and read files, because the server had no way to list models or count them. That was a gap, and it has since been filled with a tool that lists and counts tables. Asking it the three questions directly gives the right answer in one short call each; whether an agent now chooses to use it needs another run with an agent.

## Limits

Ten questions, one run each, one model, no real BigQuery behind the project, and the questions were written by the people who built the server. Treat it as a first look and a way to track changes over time, not as proof. It is a report you re-run now and then, never a pass-or-fail check. The table of numbers is in the [full reference](../docs/field-trial.md).
