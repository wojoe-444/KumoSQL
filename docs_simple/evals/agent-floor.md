# Agent-facing server: analyst questions

[Simple eval index](README.md) · [Full reference](../../docs/evals/agent-floor.md)

This check asks: when an agent works on a Dataform project through the KumoSQL server, how much does it have to read, and how long does it wait?

## Example

An analyst asks "what breaks if I drop this column?". Without the server, an agent might read all 106 model files (about 141 KB) or the full report (about 895 KB). With the server, the answer is one message of under 2 KB that lists the affected models, says what it could not tell, and names the next step.

The check asks about twenty questions like this on the largest public project in the repository (what is this table, where does this column come from, what breaks if I change it, does one like it exist) and fails when an answer is bigger than 2,000 bytes, a question needs more than three calls, a call is slow, or the server answers with an error.

## What it does not show

It measures size and speed, not whether an answer is right. Many answers on this project say "unknown" with a reason, and that is fine for this check. Correctness has its own tests. It also does not measure how many turns a real agent uses; that is the field trial, which is a periodic report, not a gate.

See the [full reference](../../docs/evals/agent-floor.md) for the questions, the bounds and the recorded numbers.
