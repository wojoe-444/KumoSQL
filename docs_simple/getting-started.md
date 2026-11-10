# Getting started

[All simple guides](README.md) · [Full reference](../docs/getting-started.md)

This first example runs on your computer. You need Python 3.11 or newer and git for the GitHub install. You do not need BigQuery credentials.

## 1. Install

Use a virtual environment: a separate place for this project's Python packages.

On macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "git+https://github.com/walterogozaly/KumoSQL.git"
```

Check that `python3 --version` is at least 3.11 first.

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install "git+https://github.com/walterogozaly/KumoSQL.git"
```

In the commands below, Windows users can replace `python` with `& .\.venv\Scripts\python.exe`. This works even if PowerShell does not allow activating the environment.

If you already have this repository checked out, install from its root with `python -m pip install .` instead.

## 2. Simplify a query

Save this text in `query.sql`:

```sql
SELECT customer_id
FROM orders
WHERE 1 = 1
```

Then run:

```sh
python -m kumosql rewrite-sql query.sql -r remove_trivial_predicates
```

The result removes `WHERE 1 = 1`: that condition is always true and filters out nothing. You should see `verification=proven` along with the new SQL.

The tool reads and checks the SQL without running it against your `orders` table. You do not need to create that table for this example.

To see what any command does without running it, add `--help`: it only prints text. Most commands only read and print. A file is written only when you pass an option that names it (`-o`, `--output`, `--patch`), and `reduce-project --write` is the only option that edits a project folder. `consolidate-tables` is a read-only preview with no option to write. The [full guide](../docs/getting-started.md) lists the details.

`proven` means the checker established equivalent results under its supported semantics. `unchanged` means no rewrite happened. Other labels need review; the rewrite command exits with status 3 for untrusted output. See [rewrite rules](rewrite-rules.md).

## 3. Open the browser app

```sh
python -m kumosql ui
```

The app opens at `http://127.0.0.1:8765/`. Paste your query into Workspace and choose the same rule. Stop the server with Ctrl+C in the terminal.

To analyze a folder of `.sql` files or a Dataform project:

```sh
python -m kumosql pipeline-report path/to/project -o report.json
python -m kumosql ui --project path/to/project
```

Replace `path/to/project` with your folder. The report shows dependencies, repeated queries, and anything the analysis could not understand. The [full getting-started guide](../docs/getting-started.md#3-analyze-a-whole-pipeline) includes a small project you can copy.

## 4. Add capabilities when you need them

From a local checkout:

```sh
python -m pip install ".[smt,execution]"
```

`smt` adds the Z3 solver for more proofs. `execution` adds DuckDB for local result comparisons. KumoSQL pins DuckDB below version 2 because version 2 breaks the checks that run queries; the pin is lifted once the code is ported (see the full guide for the details). For a GitHub install, use `"kumosql[smt,execution] @ git+https://github.com/walterogozaly/KumoSQL.git"` instead.

BigQuery features are off until you ask for them; looking up the columns of tables your project does not define needs an explicit opt-in (`--fetch-schema` on the pipeline report, the Settings checkbox, or `KUMOSQL_SCHEMA_FETCH=1`). BigQuery features have their own setup in [cost and change reports](cost-and-change-reports.md). Use `python -m kumosql COMMAND --help` to see options for any command.
