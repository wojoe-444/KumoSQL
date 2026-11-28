"""Cheap output comparison for a pipeline before and after a refactor.

A refactor should leave every model's output unchanged. Comparing tables row
by row is expensive, so this module generates BigQuery SQL in three tiers:

* fingerprints: one aggregate scan per table giving the row count, a
  whole-row checksum and one checksum per column. Checksums are
  ``SUM(FARM_FINGERPRINT(TO_JSON_STRING(value)))`` in ``BIGNUMERIC``, so they
  ignore row order, count duplicate rows, never overflow, and treat ``NULL``
  as a value. Whole-row checksums build the row with columns sorted by name,
  so a refactor that only reorders columns still matches.
* comparison: the fingerprints of both sides joined into one row per
  ``(model, column)`` with a ``matches`` flag. A mismatching column localises
  the difference; a whole-row mismatch with every column matching usually
  means values moved between rows, though the checksums cannot say why.
  A column or model that exists on one side only is fingerprinted on that
  side, so the summary reports it rather than a match.
* drill-down: for one table, the rows whose multiplicity differs between the
  sides, or with key columns, each key that is missing, duplicated or changed
  together with the names of the columns that changed (``"*"`` when only
  the pairing of values into rows changed within the key).

Fingerprint and comparison queries for a whole pipeline are single queries
that scan each table once, so they cost the bytes of the tables and return a
few rows per model. Nothing here runs a query; results are handed back to
:func:`summarize_comparison` or :func:`compare_snapshots`.

Checksums are probabilistic in principle (64-bit hashes) but a false match
needs a hash collision that cancels in a sum, which is negligible in
practice. A match is therefore strong evidence that the compared columns
hold the same rows, not a proof. Differences of representation are differences: ``1`` versus
``1.0`` after a type change, or a nested ``STRUCT`` whose field order
changed. Use ``normalize`` for expected noise such as float rounding, and
``ignore_columns`` for load timestamps (:mod:`kumosql.audit_columns` proposes
them from saved results; it never applies them).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .pipeline import Pipeline, Target


ROW = "*"
"""The ``column_name`` used for whole-row checksums."""

_COMPARED_KINDS = {"table", "view", "incremental", "sql"}


@dataclass(frozen=True)
class Location:
    """Where one side of the comparison lives, relative to each model's target.

    ``Location(dataset_suffix="_dev")`` maps ``proj.analytics.orders`` to
    ``proj.analytics_dev.orders``, which is how Dataform names development
    schemas. Unset parts keep the target's own value.
    """

    project: str | None = None
    dataset: str | None = None
    dataset_suffix: str = ""
    table_prefix: str = ""
    table_suffix: str = ""

    def table(self, target: Target) -> str:
        project = target.database if self.project is None else self.project
        dataset = target.schema if self.dataset is None else self.dataset
        if dataset:
            dataset += self.dataset_suffix
        name = f"{self.table_prefix}{target.name}{self.table_suffix}"
        return ".".join(part for part in (project, dataset, name) if part)


Locator = Location | Callable[[Target], str]


@dataclass(frozen=True)
class TableComparison:
    """One model's pair of tables and the columns compared between them."""

    model: str
    before_table: str
    after_table: str
    # ``None`` means the columns are unknown and only whole rows are compared.
    columns: tuple[str, ...] | None
    only_before: tuple[str, ...] = ()
    only_after: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComparisonDiagnostic:
    model: str
    code: str
    message: str


@dataclass
class ComparisonPlan:
    """The tables to compare for a pipeline, and the SQL to compare them."""

    tables: list[TableComparison]
    normalize: dict[str, str] = field(default_factory=dict)
    where: dict[str, str] = field(default_factory=dict)
    diagnostics: list[ComparisonDiagnostic] = field(default_factory=list)
    # Models in one pipeline only; the missing side's table is "". Each is fingerprinted on its
    # own side so the comparison reports it as missing instead of leaving it out.
    unmatched: list[TableComparison] = field(default_factory=list)

    def table(self, model: str) -> TableComparison:
        for table in self.tables:
            if table.model == model:
                return table
        raise KeyError(model)

    def fingerprint_sql(self, side: str) -> str:
        """One query fingerprinting every ``"before"`` or ``"after"`` table.

        Run it before the refactored pipeline overwrites anything, keep the
        rows, and compare them with :func:`compare_snapshots` later.
        """

        return self._fingerprint_union(side) + "\nORDER BY model, column_name"

    def compare_sql(self) -> str:
        """One query comparing every model's fingerprints on both sides."""

        return _compare_query(self._fingerprint_union("before"), self._fingerprint_union("after"))

    def _fingerprint_union(self, side: str) -> str:
        if side not in {"before", "after"}:
            raise ValueError("side must be 'before' or 'after'")
        parts = [
            _fingerprint_select(
                table.model,
                table.before_table if side == "before" else table.after_table,
                table.columns,
                where=self.where.get(table.model),
                normalize=self.normalize,
                extra=table.only_before if side == "before" else table.only_after,
            )
            for table in self.tables + self.unmatched
            if (table.before_table if side == "before" else table.after_table)
        ]
        return _union(parts)

    def drilldown_sql(
        self,
        model: str,
        *,
        keys: Sequence[str] = (),
        columns: Sequence[str] | None = None,
        limit: int = 100,
    ) -> str:
        """Rows that differ for one model; see :func:`diff_rows_sql`."""

        table = self.table(model)
        return diff_rows_sql(
            table.before_table,
            table.after_table,
            _drilldown_columns(table.columns, keys, columns),
            keys=keys,
            where=self.where.get(model),
            normalize=self.normalize,
            limit=limit,
        )


@dataclass(frozen=True)
class ModelDiff:
    """The outcome of comparing one model's fingerprints."""

    model: str
    status: str  # "match", "mismatch", "missing_before", "missing_after" or "incomplete"
    before_rows: int | None
    after_rows: int | None
    mismatched_columns: tuple[str, ...] = ()
    note: str = ""

    @property
    def matches(self) -> bool:
        return self.status == "match"


# ----------------------------------------------------------------- planning


def plan_output_comparison(
    before: Pipeline,
    after: Pipeline | None = None,
    *,
    before_location: Locator | None = None,
    after_location: Locator | None = None,
    models: Iterable[str] | None = None,
    normalize: Mapping[str, str] | None = None,
    ignore_columns: Iterable[str] = (),
    where: Mapping[str, str] | None = None,
) -> ComparisonPlan:
    """Plan a comparison of every model's output before and after a refactor.

    ``before`` is the original pipeline and ``after`` the refactored one; when
    ``after`` is omitted the same code is assumed on both sides (for example
    the same pipeline built from two commits into two datasets). Models are
    matched by target and compared on the output columns both sides share,
    so a column that was added or dropped is reported instead of breaking
    the query.

    ``before_location`` and ``after_location`` place each side's tables,
    defaulting to the model's own target. ``normalize`` maps a column name to
    a SQL template applied on both sides, such as ``{"amount": "ROUND({col},
    6)"}``. ``ignore_columns`` names columns to skip everywhere, such as load
    timestamps. ``where`` maps a model to a predicate that limits both scans,
    such as a partition filter.
    """

    before_location = before_location or Location()
    after_location = after_location or Location()
    after = after or before
    diagnostics: list[ComparisonDiagnostic] = []
    ignored = {name.lower() for name in ignore_columns}

    before_keys = [key for key in before.topological_order() if _compared(before, key)]
    after_keys = {key for key in after.models if _compared(after, key)}
    selected = None if models is None else set(models)
    if selected is not None:
        for key in sorted(selected - set(before.models) - set(after.models)):
            diagnostics.append(ComparisonDiagnostic(key, "unknown_model", "no such model"))

    tables: list[TableComparison] = []
    unmatched: list[TableComparison] = []
    for key in before_keys:
        if selected is not None and key not in selected:
            continue
        if key not in after_keys:
            diagnostics.append(
                ComparisonDiagnostic(key, "missing_after", "model is not in the refactored pipeline")
            )
            unmatched.append(
                _one_sided(key, _locate(before_location, before.models[key].target), "", before, ignored)
            )
            continue
        target = before.models[key].target
        before_table = _locate(before_location, target)
        after_table = _locate(after_location, after.models[key].target)
        if before_table == after_table:
            raise ValueError(f"{key}: before and after resolve to the same table {before_table}")
        tables.append(
            _table_comparison(key, before_table, after_table, before, after, ignored, diagnostics)
        )
    for key in sorted(after_keys - set(before_keys)):
        if selected is None or key in selected:
            diagnostics.append(
                ComparisonDiagnostic(key, "missing_before", "model is new in the refactored pipeline")
            )
            unmatched.append(
                _one_sided(key, "", _locate(after_location, after.models[key].target), after, ignored)
            )

    return ComparisonPlan(
        tables,
        {name.lower(): template for name, template in (normalize or {}).items()},
        dict(where or {}),
        diagnostics,
        unmatched,
    )


def _one_sided(key: str, before_table: str, after_table: str, pipeline: Pipeline, ignored: set[str]) -> TableComparison:
    columns = pipeline.output_columns(key)
    kept = tuple(c for c in columns if c.lower() not in ignored) if _known(columns) else None
    return TableComparison(key, before_table, after_table, kept)


def _compared(pipeline: Pipeline, key: str) -> bool:
    model = pipeline.models.get(key)
    return model is not None and model.kind in _COMPARED_KINDS


def _locate(locator: Locator, target: Target) -> str:
    return locator.table(target) if isinstance(locator, Location) else locator(target)


def _table_comparison(
    key: str,
    before_table: str,
    after_table: str,
    before: Pipeline,
    after: Pipeline,
    ignored: set[str],
    diagnostics: list[ComparisonDiagnostic],
) -> TableComparison:
    before_columns = before.output_columns(key)
    after_columns = after.output_columns(key)
    if not _known(before_columns) or not _known(after_columns):
        diagnostics.append(
            ComparisonDiagnostic(
                key,
                "unknown_columns",
                "output columns are unknown; comparing whole rows only, which is sensitive to "
                "column order and cannot apply normalize or ignore_columns",
            )
        )
        return TableComparison(key, before_table, after_table, None)
    after_lower = {column.lower() for column in after_columns}
    before_lower = {column.lower() for column in before_columns}
    shared = tuple(
        column
        for column in before_columns
        if column.lower() in after_lower and column.lower() not in ignored
    )
    only_before = tuple(c for c in before_columns if c.lower() not in after_lower and c.lower() not in ignored)
    only_after = tuple(c for c in after_columns if c.lower() not in before_lower and c.lower() not in ignored)
    if only_before or only_after:
        diagnostics.append(
            ComparisonDiagnostic(
                key,
                "columns_differ",
                f"only before: {list(only_before)}; only after: {list(only_after)}",
            )
        )
    return TableComparison(
        key,
        before_table,
        after_table,
        shared,
        only_before,
        only_after,
        tuple(c for c in before_columns if c.lower() in ignored),
    )


def _known(columns: tuple[str, ...]) -> bool:
    return bool(columns) and all(column and column != "*" for column in columns)


def _drilldown_columns(
    known: tuple[str, ...] | None, keys: Sequence[str], columns: Sequence[str] | None
) -> tuple[str, ...] | None:
    if columns is None:
        return known
    chosen = list(keys)
    lowered = {key.lower() for key in keys}
    chosen.extend(column for column in columns if column.lower() not in lowered and column != ROW)
    return tuple(chosen)


# ------------------------------------------------------------ SQL builders


def table_fingerprint_sql(
    table: str,
    columns: Sequence[str] | None = None,
    *,
    label: str | None = None,
    where: str | None = None,
    normalize: Mapping[str, str] | None = None,
) -> str:
    """Fingerprint one table: rows of ``(model, column_name, row_count, checksum)``.

    ``column_name`` is ``"*"`` for the whole row. Without ``columns`` only the
    whole row is fingerprinted, using the table's own column order.
    """

    return _fingerprint_select(
        label or table, table, columns, where=where, normalize=_lower_keys(normalize)
    ) + "\nORDER BY column_name"


def compare_tables_sql(
    before_table: str,
    after_table: str,
    columns: Sequence[str] | None = None,
    *,
    label: str | None = None,
    where: str | None = None,
    normalize: Mapping[str, str] | None = None,
) -> str:
    """Compare two tables' fingerprints: one row per column with a ``matches`` flag."""

    label = label or after_table
    normalize = _lower_keys(normalize)
    return _compare_query(
        _fingerprint_select(label, before_table, columns, where=where, normalize=normalize),
        _fingerprint_select(label, after_table, columns, where=where, normalize=normalize),
    )


def diff_rows_sql(
    before_table: str,
    after_table: str,
    columns: Sequence[str] | None = None,
    *,
    keys: Sequence[str] = (),
    where: str | None = None,
    normalize: Mapping[str, str] | None = None,
    limit: int = 100,
) -> str:
    """Rows that differ between two tables, projected onto ``columns``.

    Without ``keys`` the result is a bag difference: each distinct row (as
    JSON) whose count differs, with ``status`` ``only_before``,
    ``only_after`` or ``count_differs``. With ``keys`` the rows are matched
    on the key columns, and each differing key is reported as
    ``only_before``, ``only_after``, ``row_count_differs`` (the key occurs a
    different number of times) or ``changed``, with ``changed_columns``
    listing the columns whose values differ. Duplicate keys are compared as
    bags of whole rows, so a key duplicated identically on both sides
    matches, and values swapped between rows that share a key are
    ``changed`` with ``changed_columns`` ``["*"]`` (whole rows differ while
    each column holds the same values).
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    normalize = _lower_keys(normalize)
    if keys and columns is None:
        raise ValueError("keyed drill-down needs the column list")
    if not keys:
        return _bag_diff(before_table, after_table, columns, where, normalize, limit)
    return _keyed_diff(before_table, after_table, columns, keys, where, normalize, limit)


def _bag_diff(before_table, after_table, columns, where, normalize, limit) -> str:
    def side(table: str) -> str:
        return (
            f"  SELECT {_row_json(columns, normalize)} AS row_json, COUNT(*) AS n\n"
            f"  FROM {_table(table)} AS t{_where(where)}\n"
            "  GROUP BY row_json"
        )

    return (
        f"WITH before_rows AS (\n{side(before_table)}\n),\n"
        f"after_rows AS (\n{side(after_table)}\n)\n"
        "SELECT\n"
        "  CASE WHEN a.row_json IS NULL THEN 'only_before'\n"
        "       WHEN b.row_json IS NULL THEN 'only_after'\n"
        "       ELSE 'count_differs' END AS status,\n"
        "  COALESCE(b.n, 0) AS before_count,\n"
        "  COALESCE(a.n, 0) AS after_count,\n"
        "  COALESCE(b.row_json, a.row_json) AS row_json\n"
        "FROM before_rows AS b\n"
        "FULL OUTER JOIN after_rows AS a ON b.row_json = a.row_json\n"
        "WHERE COALESCE(b.n, 0) != COALESCE(a.n, 0)\n"
        "ORDER BY row_json\n"
        f"LIMIT {int(limit)}"
    )


def _keyed_diff(before_table, after_table, columns, keys, where, normalize, limit) -> str:
    key_lower = {key.lower() for key in keys}
    values = [column for column in columns if column.lower() not in key_lower]
    key_json = f"TO_JSON_STRING(STRUCT({', '.join(_struct_field(k, normalize) for k in keys)}))"

    row_json = _row_json(columns, normalize)

    def side(table: str) -> str:
        # Per-column checksums say which columns changed; the whole-row
        # checksum compares each key's rows as a bag, so values swapped
        # between rows that share a key are still a difference.
        checksums = "".join(
            f",\n    {_checksum(_value_json(column, normalize))} AS c{i}"
            for i, column in enumerate(values)
        )
        return (
            f"  SELECT\n    {key_json} AS key_json,\n    COUNT(*) AS n{checksums},\n"
            f"    {_checksum(row_json)} AS row_checksum,\n"
            f"    ANY_VALUE({row_json}) AS row_json\n"
            f"  FROM {_table(table)} AS t{_where(where)}\n"
            "  GROUP BY key_json"
        )

    same = [f"b.c{i} = a.c{i}" for i in range(len(values))]
    same_rows = "b.row_checksum = a.row_checksum"
    # When every column holds the same values but whole rows differ, no
    # single column changed: report the whole row, ``ROW``.
    flags = ", ".join(
        [f"STRUCT({_string(column)} AS name, {same[i]} AS same)" for i, column in enumerate(values)]
        + [f"STRUCT({_string(ROW)} AS name, {same_rows} OR NOT ({' AND '.join(same) or 'TRUE'}) AS same)"]
    )
    changed = f"ARRAY(SELECT f.name FROM UNNEST([{flags}]) AS f WHERE NOT f.same)"
    differs = "".join(f"\n   OR NOT ({condition})" for condition in [*same, same_rows])
    return (
        f"WITH before_keys AS (\n{side(before_table)}\n),\n"
        f"after_keys AS (\n{side(after_table)}\n)\n"
        "SELECT\n"
        "  CASE WHEN a.key_json IS NULL THEN 'only_before'\n"
        "       WHEN b.key_json IS NULL THEN 'only_after'\n"
        "       WHEN b.n != a.n THEN 'row_count_differs'\n"
        "       ELSE 'changed' END AS status,\n"
        "  COALESCE(b.key_json, a.key_json) AS key_json,\n"
        "  COALESCE(b.n, 0) AS before_count,\n"
        "  COALESCE(a.n, 0) AS after_count,\n"
        f"  {changed} AS changed_columns,\n"
        "  b.row_json AS before_row,\n"
        "  a.row_json AS after_row\n"
        "FROM before_keys AS b\n"
        "FULL OUTER JOIN after_keys AS a ON b.key_json = a.key_json\n"
        "WHERE a.key_json IS NULL OR b.key_json IS NULL OR b.n != a.n"
        f"{differs}\n"
        "ORDER BY key_json\n"
        f"LIMIT {int(limit)}"
    )


def _fingerprint_select(
    label: str,
    table: str,
    columns: Sequence[str] | None,
    *,
    where: str | None,
    normalize: Mapping[str, str],
    extra: Sequence[str] = (),
) -> str:
    # ``extra`` columns exist on this side only: they get a checksum row (which then has no
    # partner on the other side) but stay out of the whole-row checksum.
    entries = [(ROW, _row_json(columns, normalize))]
    entries.extend((column, _value_json(column, normalize)) for column in (*(columns or ()), *extra))
    aggregates = "".join(
        f",\n    {_checksum(expression)} AS c{i}" for i, (_, expression) in enumerate(entries)
    )
    structs = ",\n    ".join(
        f"STRUCT({_string(name)} AS column_name, CAST(s.c{i} AS STRING) AS checksum)"
        for i, (name, _) in enumerate(entries)
    )
    return (
        f"SELECT {_string(label)} AS model, x.column_name, s.row_count, x.checksum\n"
        f"FROM (\n  SELECT\n    COUNT(*) AS row_count{aggregates}\n"
        f"  FROM {_table(table)} AS t{_where(where)}\n) AS s\n"
        f"CROSS JOIN UNNEST([\n    {structs}\n]) AS x"
    )


def _compare_query(before_sql: str, after_sql: str) -> str:
    return (
        f"WITH before_fp AS (\n{_indent(before_sql)}\n),\n"
        f"after_fp AS (\n{_indent(after_sql)}\n)\n"
        "SELECT\n"
        "  model,\n"
        "  column_name,\n"
        "  b.row_count AS before_rows,\n"
        "  a.row_count AS after_rows,\n"
        "  b.checksum AS before_checksum,\n"
        "  a.checksum AS after_checksum,\n"
        "  COALESCE(b.row_count = a.row_count AND b.checksum = a.checksum, FALSE) AS matches\n"
        "FROM before_fp AS b\n"
        "FULL OUTER JOIN after_fp AS a USING (model, column_name)\n"
        "ORDER BY model, column_name"
    )


def _checksum(json_expression: str) -> str:
    return f"COALESCE(SUM(CAST(FARM_FINGERPRINT({json_expression}) AS BIGNUMERIC)), 0)"


def _row_json(columns: Sequence[str] | None, normalize: Mapping[str, str]) -> str:
    if columns is None:
        return "TO_JSON_STRING(t)"
    ordered = sorted(columns, key=lambda column: (column.lower(), column))
    return f"TO_JSON_STRING(STRUCT({', '.join(_struct_field(c, normalize) for c in ordered)}))"


def _value_json(column: str, normalize: Mapping[str, str]) -> str:
    return f"TO_JSON_STRING({_value(column, normalize)})"


def _struct_field(column: str, normalize: Mapping[str, str]) -> str:
    return f"{_value(column, normalize)} AS {_identifier(column)}"


def _value(column: str, normalize: Mapping[str, str]) -> str:
    reference = f"t.{_identifier(column)}"
    template = normalize.get(column.lower())
    return template.replace("{col}", reference) if template else reference


def _identifier(name: str) -> str:
    # Always quoted, so reserved words such as ``order`` work as column names.
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _table(name: str) -> str:
    return "`" + name.strip("`").replace("`", "\\`") + "`"


def _string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _where(predicate: str | None) -> str:
    return f"\n  WHERE {predicate}" if predicate else ""


def _union(parts: list[str]) -> str:
    if not parts:
        raise ValueError("nothing to compare")
    return "\nUNION ALL\n".join(f"(\n{_indent(part)}\n)" for part in parts)


def _indent(sql: str) -> str:
    return "\n".join(f"  {line}" if line else line for line in sql.splitlines())


def _lower_keys(normalize: Mapping[str, str] | None) -> dict[str, str]:
    return {name.lower(): template for name, template in (normalize or {}).items()}


# ---------------------------------------------------------------- results


def summarize_comparison(rows: Iterable[Mapping]) -> list[ModelDiff]:
    """Summarise the rows of a comparison query into one result per model."""

    by_model: dict[str, list[Mapping]] = {}
    for row in rows:
        by_model.setdefault(str(row["model"]), []).append(row)

    results: list[ModelDiff] = []
    for model, model_rows in sorted(by_model.items()):
        whole = next((row for row in model_rows if row["column_name"] == ROW), None)
        if whole is None:
            results.append(
                ModelDiff(model, "incomplete", None, None, note="the results hold no whole-row checksum")
            )
            continue
        before_rows = _int(whole["before_rows"])
        after_rows = _int(whole["after_rows"])
        if whole["before_checksum"] is None:
            results.append(ModelDiff(model, "missing_before", None, after_rows))
            continue
        if whole["after_checksum"] is None:
            results.append(ModelDiff(model, "missing_after", before_rows, None))
            continue
        columns = [row for row in model_rows if row["column_name"] != ROW]
        mismatched = tuple(str(row["column_name"]) for row in columns if not _truthy(row["matches"]))
        only_before = [str(row["column_name"]) for row in columns if row["after_checksum"] is None]
        only_after = [str(row["column_name"]) for row in columns if row["before_checksum"] is None]
        if _truthy(whole["matches"]) and not mismatched:
            results.append(ModelDiff(model, "match", before_rows, after_rows))
            continue
        notes = []
        if only_before:
            notes.append("columns only before: " + ", ".join(only_before))
        if only_after:
            notes.append("columns only after: " + ", ".join(only_after))
        changed = [c for c in mismatched if c not in only_before and c not in only_after]
        if before_rows != after_rows:
            notes.append(f"row count {before_rows} before, {after_rows} after")
        elif changed:
            notes.append("values differ in " + ", ".join(changed))
        elif not _truthy(whole["matches"]):
            notes.append("whole-row checksums differ but every column checksum matches: values may have moved between rows")
        results.append(ModelDiff(model, "mismatch", before_rows, after_rows, mismatched, "; ".join(notes)))
    return results


def compare_snapshots(
    before_rows: Iterable[Mapping], after_rows: Iterable[Mapping]
) -> list[ModelDiff]:
    """Compare the saved results of two :meth:`ComparisonPlan.fingerprint_sql` runs."""

    return summarize_comparison(join_snapshots(before_rows, after_rows))


def join_snapshots(before_rows: Iterable[Mapping], after_rows: Iterable[Mapping]) -> list[dict]:
    """Two saved fingerprint runs joined into the rows a :meth:`ComparisonPlan.compare_sql` run returns."""

    def index(rows: Iterable[Mapping]) -> dict[tuple[str, str], Mapping]:
        return {(str(row["model"]), str(row["column_name"])): row for row in rows}

    before, after = index(before_rows), index(after_rows)
    joined = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        b_count = _int(b["row_count"]) if b else None
        a_count = _int(a["row_count"]) if a else None
        b_sum = str(b["checksum"]) if b else None
        a_sum = str(a["checksum"]) if a else None
        joined.append(
            {
                "model": key[0],
                "column_name": key[1],
                "before_rows": b_count,
                "after_rows": a_count,
                "before_checksum": b_sum,
                "after_checksum": a_sum,
                "matches": b is not None and a is not None and b_count == a_count and b_sum == a_sum,
            }
        )
    return joined


def _int(value) -> int | None:
    return None if value is None else int(value)


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)
