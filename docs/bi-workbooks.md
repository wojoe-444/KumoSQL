# BI workbooks and notebooks as text

`python -m kumosql workbook` reads a Tableau workbook and prints it as compact text: data sources and their connections, custom SQL and the warehouse tables it reads, calculated fields with readable formulas, filters, worksheets and dashboards. It answers "which sheets use field X, and what is its formula?" in one call. It also changes a workbook without regular expressions: each patch is applied in memory, the result is parsed again, and the patch is accepted only when the structured view changed exactly as intended. `python -m kumosql notebook timeline` does the reading half for Jupyter notebooks: cells, outputs and errors as a timeline with a hard size cap.

Why it exists: a workbook of 17,000 XML lines is too long to read whole. Patching it with string replacement leaves no proof that only the intended thing changed, so people take screenshots to check, and screenshots fill an agent's context. A 50,000-token notebook does not load whole either. This page shows the commands, a worked example, and what the checks do and do not prove.

Everything here only reads and prints, except `workbook patch ... --apply` (writes the change into the file you name) and `workbook patch ... --output PATH` (writes a patched copy).

## Reading a workbook

```
python -m kumosql workbook show sales.twb                   # the whole workbook as text, capped at 12,000 characters
python -m kumosql workbook show sales.twb --sheet "Margin Table"
python -m kumosql workbook show sales.twbx --datasource Orders --json
python -m kumosql workbook find sales.twb "Profit Ratio"    # one field: formula, readers, sheets, filters, dashboards
python -m kumosql workbook find sales.twb --table sales.orders
python -m kumosql workbook tables sales.twb                 # the warehouse tables each data source reads
```

`.twb` files are read directly. A `.twbx` (a packaged workbook, a zip archive) is read through the one `.twb` at its top level. A package with zero or several workbooks is refused.

`show` prints the following. `--json` gives the same view as data, uncapped.

| Part | What it holds |
| --- | --- |
| Data sources | Caption and internal name. For each connection: class, project, dataset and billing project. Each table relation as `project.dataset.table`, completed from the connection when the XML leaves parts out. Each custom SQL query in full, with the tables it reads |
| Calculated fields | `Caption = formula` with internal names such as `[Calculation_1368249927221915648]` replaced by captions, the internal name, and in JSON the formula as stored and the fields it reads |
| Other fields | Captions, default number formats, groups and bins, member colors, data source filters |
| Parameters | Caption, type, current value |
| Worksheets | The fields the sheet uses directly, the fields it reaches only through calculated fields, its filters (`Region in ("East", "West")`, `SUM(Sales) >= 1000`), rows and columns shelves, marks, fixed mark colors and number formats |
| Dashboards and stories | The sheets they show, filter controls, parameter controls |
| Problems | Anything that does not add up: a formula naming a field the data source does not have, two copies of a field's declaration that disagree, two fields with the same caption. A workbook that Tableau saved normally has none |

The text is deterministic, so the same file always gives the same text. It is cut at `--max-chars` (12,000 by default) with a closing line that says how many lines were cut and how to see them (`--sheet`, `--datasource` or `--json`).

## Example: the "done when" question

The test fixture [`tests/fixtures/bi_workbooks/sales_overview.twb`](../tests/fixtures/bi_workbooks/sales_overview.twb) is a synthetic workbook written by hand. It has three BigQuery data sources, calculated fields that reference each other, a parameter, filters and two dashboards.

```
$ python -m kumosql workbook find sales_overview.twb "Profit Ratio"
Profit Ratio: calculated field of Orders (sales), internal name [Calculation_1368249927221915648]
  formula: SUM([Profit]) / SUM([Sales])
  as stored: SUM([profit]) / SUM([sales])
  reads: Profit, Sales
  read by calculated fields: High Margin, Margin Label
  used directly by 1 sheet: Margin Trend
  used through calculated fields by 1 sheet: Margin Table (via High Margin)
  filters on it: none
  on dashboards: Overview (Margin Trend), Margin Review (Margin Table)
```

A name matches a caption or an internal name, ignoring case, with or without brackets. When several data sources have a field of that name, every match is listed. `--datasource` narrows the search. When nothing matches, near names are suggested and the exit status is 1.

## Warehouse tables (the lineage hook)

`workbook tables` and the Python function `kumosql.bi_workbook.tables_read(view)` list the warehouse tables each data source reads. These are the tables of its connection, and the tables its custom SQL names, parsed with sqlglot in the BigQuery dialect. A later change will feed this into the lineage index (who reads which warehouse table). It is not part of `Pipeline` yet.

```
$ python -m kumosql workbook tables sales_overview.twb
Orders (sales) (bigquery):
  acme-analytics.sales.orders  (table)
  acme-analytics.crm.customers  (custom SQL 'Customer Segments')
  acme-analytics.crm.segments  (custom SQL 'Customer Segments')
Targets (bigquery):
  acme-analytics.planning.sales_targets  (table)
Returns (bigquery):
  acme-analytics.sales.returns  (custom SQL 'Recent Returns')
  acme-bi.sales.orders  (custom SQL 'Recent Returns', written sales.orders, completed from the billing project)
```

Rules:

- A CTE is not a table.
- A Tableau parameter inside custom SQL (`<[Parameters].[Return Window (days)]>`) is read as a value, so it does not stop the parse.
- Custom SQL that sqlglot cannot parse is listed under `unparsed` with the reason, and its tables are unknown. They are never guessed. `find --table` names that query under `unknown`, since it might read the table.
- BigQuery runs custom SQL in the connection's billing project, so `dataset.table` is completed with the billing project (or with the project when there is no billing project). The entry keeps `written` and `completed_from`. In the example, `sales.orders` resolves to `acme-bi.sales.orders`, which is not the `acme-analytics.sales.orders` the table relation reads. That is worth knowing before anyone trusts the custom SQL.
- A bare `table` is completed with the connection's dataset too and marked `completed_from: "connection dataset"`. BigQuery needs a default dataset for that form, so check it.

## Patching a workbook

```
python -m kumosql workbook patch sales.twb rename-field "Profit Ratio" "Margin %"               # preview only
python -m kumosql workbook patch sales.twb rename-field "Profit Ratio" "Margin %" --apply       # writes sales.twb
python -m kumosql workbook patch sales.twbx swap-datasource "Orders (sales)" --project acme-staging --output staging.twbx
python -m kumosql workbook patch sales.twb swap-datasource Targets --table sales_targets=sales_targets_2025
python -m kumosql workbook patch sales.twb swap-datasource "Orders (sales)" --column region=sales_region
python -m kumosql workbook patch sales.twb set-color --sheet "Margin Trend" --color "#e15759"
python -m kumosql workbook patch sales.twb set-color --field Region --member East --color "#f28e2b" --datasource "Orders (sales)"
python -m kumosql workbook patch sales.twb set-format "Profit Ratio" "p0.0%"
python -m kumosql workbook patch sales.twb set-format "Profit Ratio" "p0%" --sheet "Margin Trend"
```

| Patch | What it changes |
| --- | --- |
| `rename-field FIELD NEW` | The field's or parameter's caption, in the data source and in every sheet's copy of the declaration. Formulas, shelves, filters, sorts and tooltips refer to a field by its internal name, which stays, so they follow without being edited. The diff shows each formula, sheet and filter that now reads the new name |
| `swap-datasource DS` | `--project` and `--dataset` move the connection and every table it reads there, connection tables and custom SQL alike. `--billing-project` changes where queries run. `--table OLD=NEW` renames a table (matched on the last parts of its name). `--column OLD=NEW` points the field that reads warehouse column OLD at NEW, and the field keeps its internal name, so nothing that uses it changes. BigQuery connections only |
| `set-color` | A sheet's fixed mark color (`--sheet`), or the palette color of one value of a dimension (`--field` and `--member`), wherever that palette is kept |
| `set-format FIELD FORMAT` | A field's default number format, in the data source and every sheet's copy. With `--sheet`, the format that sheet already gives the field |

Every patch runs the same five steps (`kumosql.bi_workbook.patch.run`):

1. **Plan** the edit as a few character spans of the original text, plus the intent: the same change stated on the structured view ("this field's caption is now *Margin %*"), with no knowledge of where the XML holds it.
2. **Re-parse** the edited text with Python's XML parser and with the span scanner.
3. **Diff the XML** element by element. The new file must have the same elements in the same order, and the only attributes or texts that differ must be the ones the plan touched.
4. **Diff the view.** The view of the edited file must equal the view of the original with the intent applied. A copy of a declaration the plan missed, a formula that no longer resolves, or any other side effect makes the two differ, and the patch is refused. Custom SQL that was edited is parsed again with sqlglot. Its tree must equal the old tree with the table names mapped.
5. **Show** the structured changes, a diff of the text view and a diff of the XML lines. Nothing is written without `--apply` or `--output`. Writing is atomic, and `--apply` first checks that the file still holds what was read.

The edit is made in place, so bytes outside the edited values stay as they are: attribute order, quoting, entities, whitespace, comments and line endings (CRLF too). A rename of a field declared in three places changes three lines of the file, even in a 17,000-line workbook. When an attribute is added (a caption for a field that had none), it goes in alphabetical place, where Tableau writes it. A `.twbx` is rewritten member by member. Only the `.twb` inside changes. Every other member (extracts, images) keeps its local header and stored bytes, byte for byte, and the new archive is read back to check it.

A patch is refused, and nothing is written (exit status 3), when:

- the new name is taken in that data source. A formula naming it would become ambiguous;
- the name matches fields in several data sources and `--datasource` is missing;
- the field has no `<column>` declaration in its data source to hold a caption or format (it was never edited in Tableau);
- the sheet has no fixed mark color, or the member has no assigned color, or the sheet has no format of its own for the field. Adding one would add XML structure whose place Tableau decides, so set it once in Tableau and patch it from then on;
- a `--table` names a table the data source does not read, or a `--column` comes from custom SQL (change the SQL instead);
- custom SQL that would have to move cannot be parsed;
- the connection is not BigQuery, the workbook is not UTF-8, or the `.twbx` uses zip64, encryption or several disks;
- any check in steps 2 to 4 fails.

## Reading a notebook

```
python -m kumosql notebook timeline analysis.ipynb
python -m kumosql notebook timeline analysis.ipynb --errors-only
python -m kumosql notebook timeline analysis.ipynb --cells 40-80 --max-chars 20000
python -m kumosql notebook timeline analysis.ipynb --json
```

The first lines are always a header. It gives the cell counts and the kernel, how many code cells ran, whether they ran top to bottom (and if not, the run order), and every error with its cell, exception and message. An error is never hidden by the cap. Then comes one entry per cell, numbered from 1 in notebook order. For the fixture [`region_analysis.ipynb`](../tests/fixtures/bi_workbooks/region_analysis.ipynb):

```
notebook region_analysis.ipynb (nbformat 4.5, kernel python3, language python): 7 cells (6 code, 1 markdown)
code cells: 5 run, 1 not run; NOT run top to bottom: 1 cell ran after a cell below it (5); run order: 2, 3, 4, 6, 5
errors (1):
  cell 6: KeyError: 'target'
...
[6] code, run 4
    targets = pd.read_csv("targets.csv")
    gap = by_region.merge(targets, on="region")["target"]
  ERROR KeyError: 'target'
    | ... 12 more traceback lines cut from the start (619 characters)
    |    3807     raise InvalidIndexError(key)
    | -> 3812 raise KeyError(key) from err
    |
    | KeyError: 'target'
```

- **Per-cell limits.** Source keeps its first 20 lines (`--source-lines`) and a markdown cell its first 6 (`--markdown-lines`). A stream output keeps its first and last lines (`--output-lines`, 12), a result keeps its first lines, and a traceback keeps its last lines, where the error is raised. A line longer than 300 characters is cut. Every cut says what was cut: `... 37 more source lines cut (1,204 characters)`.
- **No blobs.** Images and HTML are named with their size (`image/png 48.0 KB, not shown`) and never inlined. When a result has a text form (a DataFrame does), that is shown. ANSI colors are removed. A progress bar that redraws its line shows the last state.
- **A hard cap.** The whole text stays under `--max-chars` (12,000 by default). Cells past the cap are left out whole, never half shown. The last line names them and how to see them: `... 5 cells not shown (3-7): the 900-character cap was reached; use --cells 3-7, --errors-only or a larger --max-chars`. `--json` applies the per-cell limits but not the total cap.

## Limits

- **Tableau only.** The view is a plain dict with a `format` key, and the commands work on that dict, so another BI format (Power BI, Looker) could be added as a second reader with the same shape. None is built.
- **Tableau's XML is not a published format.** The reader handles the structures it knows: federated and plain connections, joined, unioned and custom SQL relations, the object-model `_.fcp...` duplicates of a relation, column instances such as `[sum:sales:qk]`, and categorical, quantitative and relative-date filters. Anything else is skipped, not guessed. A filter it cannot summarize is shown as `Field: <class> filter`. It has been tested only on hand-written synthetic workbooks, since the repository holds no real one.
- **What the checks prove.** A patch is accepted when the XML parses, only the planned values changed, and the structured view changed exactly as intended. It is not opened in Tableau. A change Tableau would read differently than this reader does, or a part of the workbook the view does not cover, is outside the check. The view covers the fields, formulas, filters, relations, connections, shelves, mark colors, member colors and formats listed above.
- **Rename changes captions, not internal names.** That is what Tableau's own *Rename* does. Renaming the internal name that formulas use is not offered. Use `swap-datasource --column` when the warehouse column itself was renamed.
- **Patches change existing values; they never add structure.** The one exception is an attribute on an existing declaration (a caption or a default format).
- **Custom SQL resolution** follows BigQuery's rule (the billing project for `dataset.table`). It assumes the connection's dataset as the default dataset for a bare table name and marks that assumption.
- **Size.** On one synthetic workbook of 11,800 lines (686 KB) on a laptop, reading took 0.14 seconds and a patch about 1 second, including both checks. This was measured once, on one machine.
- **Notebooks.** nbformat 4 only. An nbformat 3 notebook is refused with a hint to upgrade it. Widget state, attachments and cell metadata are not shown.
