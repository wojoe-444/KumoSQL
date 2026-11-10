# Connecting a Dataform repository

[All simple guides](README.md) · [Full reference](../docs/dataform-repositories.md)

KumoSQL reads your Dataform project using git installed on your computer. Private repositories use the same credentials as your normal git commands.

## Connect through the app

1. Start `python -m kumosql ui`.
2. In **Settings → Local data folder**, choose a writable folder in your home directory.
3. In **Settings → Repositories**, enter your git URL and optionally a branch.
4. Activate the repository to use it on the graph, Cost, and Change reports pages.

A blank branch uses the remote's default. **Fetch latest** refreshes the saved copy. The app can keep several repositories, with one active at a time.

For a one-off load:

```sh
python -m kumosql ui --git https://github.com/owner/dataform-project.git --branch main
```

Replace the URL and branch with your own. Do not put a token, password or user name inside an https URL, or a password inside an ssh URL: those URLs are rejected, as is any URL with a query string. Use a Git credential helper for HTTPS, or an SSH key with `git@host:path` or `ssh://git@host/path`. Authentication must already work for git. If you use GitHub CLI for HTTPS authentication, `gh auth setup-git` lets git use that login; `gh auth login` alone is not enough.

## What gets stored

The local data folder holds settings, cached repository data, saved analyses, and logs. The app can show a saved project while it refreshes in the background. If a refresh fails, it can keep the cached copy and say why.

Choosing a home folder is especially helpful with Microsoft Store Python: Python and git can otherwise disagree about where AppData files live.

## Copies of the project inside the folder

If you point KumoSQL at a folder that also holds another copy of the project (a git worktree, a backup, a vendored copy), the copy is ignored. Before this, each source table looked declared twice and every `ref()` to it showed up as ambiguous. Only the project's own `definitions/` and `includes/` folders are read for declarations. This covers folders loaded from disk, not the evidence of any score.

## Why a dependency might look surprising

Dataform's `${ref("name")}` resolves to the action or declaration with that name. Its own `schema` and `database` settings matter. KumoSQL reads declarations and project defaults rather than assuming every reference belongs to the default dataset.

When Google credentials are available, KumoSQL also reads Dataform workflow configurations. A model gets a production-schedule marker when an active configuration selects it, directly or through dependencies. Here, active production means a scheduled, enabled configuration using the release named `production`.

## Tables that only JavaScript can name

Some Dataform projects list their source tables with JavaScript that only Dataform can run, for example a loop over a list the code builds. KumoSQL cannot read those names from the files, so it asks Dataform itself, when it can: it looks for the Dataform repository that matches your connected git repository, using the Google Cloud projects you chose on the BigQuery page, starting in one region (`us-central1` unless you set another) and then trying the others, and with your Google credentials. If that works, the names resolve and lineage shows the real source tables. Either way the load says what happened: it read the compilation, it was not asked because no repository is connected, or it could not be read and why (no projects chosen, no credentials, no matching repository in any region, no compilation yet, or an error from Dataform). The message never names your projects or tables.

Limit: the answer is Dataform's latest compilation, which can differ from the commit you loaded. The [full guide](../docs/dataform-repositories.md) lists each reason and the test that covers them.

## What KumoSQL will not guess

KumoSQL reads your `.sqlx` files without running Dataform's JavaScript. When a value would need that JavaScript, it says it does not know instead of guessing.

For example, say one action has `type: dataform.projectConfig.vars.kind` and another has `type: "table"`, and both select `1 AS id`. If the variable is `incremental`, the first one keeps its old rows between runs, so the two are not the same table. KumoSQL marks the first one as an unknown type and will not call the pair equivalent. It still reads the first query for lineage.

Three more things it handles the way Dataform does:

- A `${ref("base")}` written inside a `--` or `/* */` comment is only text. It is not a dependency. (A `#` comment is different: Dataform still evaluates it.)
- Built-in assertions such as `rowConditions: ["status > 0"]`, and a table's `partitionBy` and `clusterBy`, use that table's columns. A column named there is never reported as unused, even when no query reads it. If the setting is computed by JavaScript and cannot be read, KumoSQL reports no unused columns for that table.
- A table with such settings and nothing reading it is still treated as a final output.

More of the same, from the later rounds of the audit:

- If a table's name, schema or database is computed, KumoSQL does not pretend to know which table it is. The same goes for a `ref("f" + "eed")` that is built from pieces: it stays unresolved instead of being read as two separate names.
- A table whose dataset is written as a call into your own `includes/` code, such as `functions.baseSchema("ga4")`, is still not a dataset KumoSQL can name, but it can tell two such tables apart by the text of the call. Two tables called `event` in datasets built by `baseSchema("ga4")` and `productSchema("ga4")` stay two tables, and a `ref` written with the same call finds the right one. A `ref` whose call matches no table is left unresolved rather than guessed, because the call could produce any dataset. A dataset taken from a local variable is still unreadable.
- Settings such as a table-name prefix or a dataset suffix rename every table Dataform builds (not the declared source tables). KumoSQL applies them, so the names in lineage and impact match what Dataform creates. For example, with a prefix `t` and a suffix `sbx`, a table `a` in dataset `ds` is `ds_sbx.t_a`. Your `ref("a")` still finds it.
- A reference to something that does not exist, or that is spelled with the wrong capital letters, is reported, because Dataform would refuse the project. KumoSQL still loads it so the rest can be analysed.
- A compiled graph that Dataform rejected shows its errors instead of looking like an empty project. The query that an incremental table runs on later runs is read too, so a column only that query uses is not called unused.
- Windows line endings in `---` separators, and a stray `${` inside a comment, no longer break loading.

- A table made in JavaScript, such as `publish("report").query(ctx => \`SELECT 1 AS id\`)`, is read like a `.sqlx` file when its name and query are written out. Its columns then show up in lineage, and an unused column is found. A query built by code is not guessed.
- A config key that Dataform would refuse, such as `bigqueryPolicy`, or a `uniqueKey` on a plain table, is reported. The model still loads.
- A condition such as `WHERE ${when(incremental(), `ts >= checkpoint AND`)} ts >= x`, where the part inside `when()` ends with `AND`, is now read as one piece with that `AND`, so the statement parses instead of being read from its words alone. A rewrite that would separate the two is refused instead of guessed.
- Project variables and `includes` inside the SQL itself stay as placeholders. A variable can be overridden when Dataform compiles, so its value in the settings file is only a default, and KumoSQL will not prove two queries equal on a guess. (A table's name taken from a variable does use the settings value, since nothing else can name it.)

Limits: these checks come from small synthetic projects compared with Dataform's compiler, not from a benchmark. A hook that writes into a table it also references is still reported as a cycle, which is cautious rather than exact. Queries built by JavaScript code, and exact SQL from variables, need the compiled output. The [full guide](../docs/dataform-repositories.md#what-the-static-reader-does-not-guess) lists them.

## If loading fails

Run:

```sh
python -m kumosql.ui --diagnose-repo https://github.com/owner/dataform-project.git
```

This reports the steps and environment details. Logs use placeholders such as `repo#1` and report counts, timing, error categories and positions rather than SQL, row values, variables or exception messages. Quoted secrets, JSON credential fields and private-key blocks are removed. Copy diagnostics includes only summarized log entries and leaves older or free-form entries out; settings values and unknown field names are withheld. Repository URLs and cached clone origins do not retain embedded credentials. `redaction-map.json` contains the real-name mappings and should stay on your computer. The full guide covers HTTPS fallback, cache behavior, schedules, and diagnostic codes.
