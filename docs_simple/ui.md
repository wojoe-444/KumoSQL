# The browser app

[All simple guides](README.md) · [Full reference](../docs/ui.md)

The UI is a local website for working with SQL. Start it after installing KumoSQL:

```sh
python -m kumosql ui
```

It opens at `http://127.0.0.1:8765/`. Keep the terminal running; Ctrl+C stops the app. `--no-browser` starts the server without opening a browser, and `--port 8766` chooses another port.

The server accepts only its own local addresses and gives each running session a fresh secret that the browser sends with API requests. If you restart the server while a page is open, reload that page before using it again. Other websites cannot use a foreign address or origin to change the app's settings. Programs running on your computer can still read the local page, so this does not replace your computer's account security. If you call the API from a script, see the [local request boundary](../docs/ui.md) for the required session header.

## Pick the page for your task

| Page | Use it for |
| --- | --- |
| Workspace | Paste SQL, choose rules, inspect the changed lines and evidence |
| Query graph | Follow dependencies and assess the impact of a change |
| Cost | Look for repeated work; add job history to see measured usage |
| Change reports | Compare project versions and inspect proposed shared logic |
| Refactor | Protect important outputs and search for simpler pipelines |
| Shared models | Generate a checked patch to share a repeated CTE |
| Reduce | Pick the outputs you need and get a smaller project that still builds them |
| BigQuery | Browse the catalog your Google credentials can access |

In Workspace, start with one rule. Open **Details** to understand the verdict, then use **Diff** to see the changed lines. A green planning check alone does not establish equal results; see [provers](https://github.com/wojoe-444/Kumo_SQL_Prover/blob/main/docs_simple/provers.md).

To show a project in the graph, start with `--project path/to/project`, or connect a repository through **Settings → Repositories**. On large projects, the graph can appear before Cost and Change reports finish their analysis.

## Settings in everyday terms

- **Formatting** controls how SQL looks, including indentation and keyword case. SQL the formatter cannot read, such as Dataform SQLX, shows *Output unchanged* with the reason.
- **Scopes** choose which models, tables, or job records an analysis includes.
- **Tags** attach your own local labels to objects. They do not update BigQuery labels.
- **Catalogs** describe the objects your team owns, including objects outside Dataform.
- **Local data folder** chooses where settings, caches, and logs are stored.

Project files must have relative paths. Windows drive names, colons and paths that escape the temporary folder are refused. When loading a local project, KumoSQL skips linked files and directories and shows gaps for them. Keep the files you want analyzed inside the selected project.

Saved project snapshots use checked JSON data. Old pickle snapshots are ignored, so reload the project once after upgrading. A restart can restore the saved model definitions without fetching Git or reading SQLX again; SQL analysis and lineage are rebuilt.

The app remembers these choices on your computer. Its server listens on the local loopback address. Ordinary Workspace rewrites run locally; explicitly requested BigQuery, git, and Dataform features contact those services.

Scope queries and data sources accept one read-only SQL query, including a `WITH` query or a union. The app refuses scripts and statements that change data before contacting BigQuery. Queries still use the billing project and byte cap chosen in Settings.

Cached results belong to the billing project, location and credential identity that produced them. Changing that context discards old results. Expired entries are deleted when caches are opened or used; the default lifetime is 48 hours. Scope values stay only in memory, so they are fetched again after restarting the app; disk summaries contain counts and timings, without query previews or values. Data sources keep the rows needed by scopes and joins in the local data folder until expiry, but only when you sign in with a service account; with `gcloud auth application-default login` or an access token the rows are fetched again after a restart. A failed manual refresh can show an unexpired copy marked stale, but expired rows are never used. Credentials themselves are never stored in these caches.

If something fails, use **Settings → Diagnostics → Copy diagnostics**. Diagnostics redact names and secrets, but skim any log before sharing it. The [repository guide](dataform-repositories.md) explains authentication and cache problems.
