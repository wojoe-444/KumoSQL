# Editing Dataform config blocks in bulk (`sqlx-config`)

`python -m kumosql sqlx-config` changes the `config {}` block of many `.sqlx` files with one previewed command: add a BigQuery label, add or remove a tag, set a description, delete a key. It reads each block as the JavaScript object it is, splices the change into the original text, and keeps everything else byte for byte: line endings, a byte-order mark, indentation, quotes, comments and the trailing-comma habit. It leaves assertions alone unless you name them, and it refuses any file whose edit would touch a value only Dataform can compute.

It came from a data team's request: adding one label to 299 files took four script versions, because the blocks were laid out in different ways, 26 files had CRLF line endings, and assertions were labelled by mistake and had to be reverted.

## Example: one label on every table

```shell
python -m kumosql sqlx-config edit . 'labels.team = "finance"' --plan label-plan.json
python -m kumosql sqlx-config edit . --apply --plan label-plan.json
```

The first command writes nothing. It prints a unified diff of every file it would change, then a summary, and saves the plan with each file's sha256. This is part of its output on a generated 300-file project. `m001.sqlx` uses CRLF line endings, four-space indentation and single quotes, and its new lines keep all three:

```text
--- a/definitions/m001.sqlx
+++ b/definitions/m001.sqlx
@@ -1,5 +1,10 @@
 config {
-    type: 'incremental'
+    type: 'incremental',
+    bigquery: {
+        labels: {
+            team: 'finance'
+        }
+    }
 }
 SELECT id, ts FROM ${ref('src_1')} -- config { not: 'a block' }

command: labels.team = "finance"
  files read             300  (.sqlx under definitions/)
  matched                253
  changed                155
  already as asked        13  (no-op: left byte-identical)
  skipped: assertion      47  (assertion files are edited only when the where clause names type assertion)
  skipped: no labels      85  (labels apply to table, view, incremental only)
  refused                  0
  inline assertions blocks in changed files left as they were: 64
```

The second command writes exactly the saved plan. It refuses any file whose sha256 changed after the plan was made.

## The command

A command is one or more edits, separated by `;`, with an optional `where` clause:

| Edit | What it does |
| --- | --- |
| `PATH = VALUE` | Sets a key. A missing key is added, with any objects missing on the way (`bigquery: { labels: {...} }`) |
| `PATH += VALUE` | Adds a string (or number) to a list unless it is already there. A missing list is created, and a single string (`tags: "a"`) becomes a list |
| `PATH -= VALUE` | Removes every copy of a value from a list. Nothing happens when it is not there |
| `delete PATH` | Removes a key. Nothing happens when it is not there |

**Paths** are dotted keys inside the block: `tags`, `description`, `bigquery.partitionBy`, `assertions.nonNull`. Quote a key that is not a plain name: `labels."cost-center"`.

**`labels.` is an alias for `bigquery.labels.`.** Dataform's documentation puts table labels in the `bigquery` block ("add the label to the `bigquery` block in the `config` block": `bigquery: { labels: { department: "shipping" } }`, `IBigQueryOptions.labels` in the core reference). With `@dataform/cli` 3.0.47 that block compiles to the table's `bigquery.labels` and `actionDescriptor.bigqueryLabels`. Dataform has labels only for tables, views and incremental tables, so the alias edits only those types; other files are skipped and counted under `skipped: no labels`. (A path written in full, `bigquery.labels.KEY`, is applied as written, but 3.0.47 rejects a `bigquery` block on an `operations` action.) A top-level `labels` member compiles to the same labels in 3.0.47. A block that already has one is refused for the alias, because there it would be a guess. Write `bigquery.labels.KEY`, or `config.labels.KEY` for that member: the `config.` prefix turns the alias off.

**Values** are written out as JavaScript literals: strings in either quote, numbers, `true`, `false`, `null`, lists (`["a", "b"]`) and objects (`{team: "x"}`). A bare word is a string (`labels.team = finance`), since a command has no variables.

**The where clause** chooses files by:

- `type`, the action type. A block with no type is a table, as the loader reads it, and an unknown type name is an error.
- `name`, the config's `name`, else the file name.
- `file`, the path from the project folder.
- Any path into the block: `tags`, `labels.team`, `schema`.

| Condition | True when |
| --- | --- |
| `F in (a, b)` | The value is one of them (for a list, any element is) |
| `F not in (a, b)` | It is none of them |
| `F = V`, `F != V` | The whole value equals V, or does not |
| `F ~ "regex"` | A string value matches (Python regular expression, searched) |
| `F like "glob"` | A string value matches the glob: `**` crosses folders, `*` and `?` do not |
| `F has V` | A list contains V, a string equals V, or an object has the key V |

Join conditions with `and`, `or`, `not` and parentheses: `where type in (table, view) and name ~ "^stg_" and tags has "pii"`. `--files GLOB` (repeatable) restricts the files by path before the where clause, for example `--files 'definitions/staging/**'`.

## Assertions are left alone by default

- A file with `type: "assertion"` is never edited unless the where clause names the type: `where type in (table, assertion)`. The summary says how many assertion files were skipped.
- The inline `assertions: {...}` block of a table is edited only by a path that starts with `assertions` (`assertions.nonNull += "ts"`). The check after every edit confirms that nothing under it changed otherwise. The summary counts the changed files whose inline block was left as it was.
- A file whose `type` is computed might be an assertion, so it is refused unless the where clause names `assertion`.

## What is kept

- **Everything outside the block, and every member the edit does not touch, byte for byte.** A file whose edit is a no-op (the label is already set, the tag is already there) is left byte-identical and counted as `already as asked`.
- **Line endings and the BOM.** New lines take the line ending of the line they follow, so a CRLF file stays CRLF. A byte-order mark is kept.
- **Indentation.** New members take their neighbours' indentation. New nested objects are indented by the block's own step (two spaces, four, or a tab, found from the block).
- **Quotes.** New strings use the quote their neighbours use (the same object first, then the block). Keys are quoted when every key around them is, and a key that needs quotes gets them.
- **Trailing commas.** An object that ends in a comma keeps ending in one. One that does not gets a comma after its old last member and none after the new one.
- **One-line blocks stay on one line:** `config { type: "table", bigquery: { labels: { team: "finance" } } }`.
- **Removals.** `delete` and `-=` remove an entry's whole line when the entry has a line to itself, with a `//` comment at the end of that line. Otherwise they remove the entry and one comma. An object or list left empty becomes `{}` or `[]`.

## Refusals

A file is refused, with its path and the reason, when:

- The edit touches a computed value: `bigquery: sharedOptions`, `labels: constants.LABELS`, a spread (`{...base, team: "x"}`), `tags: ["a", constants.T]` (whether `+=` adds a copy is unknown), `` description: `as of ${day}` ``, or a key written twice.
- The where clause cannot be decided because it reads a computed value. `tags has "pii"` with `tags: constants.TAGS` is unknown; with `tags: ["pii", x]` it is true. Computed values elsewhere in the block do not stop an edit.
- The block cannot be read: a JavaScript syntax error (a missing comma is named), two config blocks, a file that is not UTF-8, or no config block at all. A file with no block is a table, but this tool edits existing blocks only.

`edit` exits 3 when any file is refused, 2 for a command or option it cannot use, and 0 otherwise.

## Preview, plan and apply

- **Preview (the default).** A unified diff per changed file, then the summary. `--stat` prints one line per file instead, and `--json` prints the whole plan.
- **`--plan FILE`** saves the plan as JSON. It holds the command, the project folder, and for each file to change: its path, its sha256 before and after, the edits and the diff. Refused and skipped files are listed with their reasons.
- **`--apply --plan FILE`** writes exactly that plan, from the same project folder. A file whose sha256 differs from the plan's is refused, and so is a file the command would now edit differently. Files not in the plan are not touched, so a plan is applied once.
- **`--apply` alone** plans and writes in one run.
- **How files are written.** Each file goes to a temporary file in the same folder and replaces the original in one step (`os.replace`), keeping its permissions. The file is then read back: its bytes must be the planned ones, and its block must read back as the old block with exactly the intended change, with the same text around it and the same line endings. A failure is reported per file.
- **Shells.** Windows PowerShell 5.1 drops double quotes inside an argument it passes to a program. Write the command with single quotes inside (`"labels.team = 'finance'"`), use a bare word, or put the command in a file and pass `--command-file FILE` (`-` reads standard input).

## Showing values

`show` prints the files a where clause selects, with the values of the paths named before `where`. By default those are type, name, tags and labels. Assertion files are listed too, since `show` changes nothing:

```shell
python -m kumosql sqlx-config show . 'labels.team, tags where type in (table, view)'
```

```text
definitions/orders.sqlx  labels.team="finance"  tags=["daily"]
definitions/staging/stg_users.sqlx  labels.team=-  tags=<unknown: tags is computed (a variable: constants.TAGS)>
2 of 3 files selected
```

`-` is a missing key; the third file is an assertion, which `type in (table, view)` leaves out. `--json` prints the same rows as JSON, and `--limit N` caps the rows printed (100 by default).

## From Python

```python
from kumosql import sqlx_config

plan = sqlx_config.plan_edit("path/to/project", 'labels.team = "finance"')
print(plan.summary())                  # counts, as in the preview
written = sqlx_config.apply_plan(plan)  # one result per changed file
```

`sqlx_config.find_config(text)` returns a block whose every key and value carries its source span. `sqlx_config.edit_file(data, path, command)` plans one file's bytes without touching the disk.

## One tokenizer

The loader reads config blocks, and `includes/` modules, through the same JavaScript tokenizer (`kumosql.js_tokens`). The tokenizer reads a template literal's `${...}` as code, so a brace, backtick or comment marker inside one does not end it. On every config block (569) and `.js` file (25) in the repository's fixtures, it gives the same text as the two readers it replaced, and every fixture project loads the same with either. The loader's other config readers still use their own patterns, so their behaviour stays as recorded in [what the static reader does not guess](dataform-repositories.md#what-the-static-reader-does-not-guess).

## Limits of the evidence

- `tests/test_sqlx_config.py` covers hand-written fixtures in deliberately different layouts: one-line and multi-line blocks, comments, trailing commas, CRLF, BOM, tabs, quoted keys, nested `bigquery`, computed values, assertion files and inline blocks, idempotent `+=` and `-=`, `delete`, byte-identical no-ops, the plan's hash refusal and the filters. It also edits 300 generated files in a temporary folder. Every changed file must differ only inside its block, and no assertion file may change.
- The edited 300-file project was compiled once with `@dataform/cli` 3.0.47. That check is not part of the test suite, because the suite does not install the CLI. After the edit, every one of the 168 tables carried the label, and the 183 assertions (the inline ones included), 65 operations and 20 declarations compiled exactly as before, with no graph errors. The only other change Dataform reported was its own `partitionBy: ""`, which it writes into every `bigquery` block it compiles.
- Not covered:
  - `.js` files (`publish()`, `declare()`) and `.sql` files.
  - Files with no config block.
  - Files that are not UTF-8.
  - BigQuery's rules for label keys and values (lowercase, length), which are not checked.
- A `//` comment at the end of a removed entry's line is removed with the entry.
- Where Dataform and this reader could disagree, as with an unusual expression, the reader treats the value as computed and refuses the edit rather than guess.
