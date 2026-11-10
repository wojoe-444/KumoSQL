"""Loading Dataform/SQLX projects and compiled graphs into a Pipeline."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from sqlglot import exp

from .identity import NodeIdentity
from .pipeline_types import Model, PipelineDiagnostic, Target
from .resilience import (
    PipelineLoadError,
    describe_os_error,
    extended_path,
    find_assets,
    parse_json_or_raise,
    read_text_or_reason,
)
from kumo_sql_prover.sqlx import (
    _SQLX_BLOCK_RE as _SQLX_CONFIG_RE,
    mask_sqlx_interpolations as _mask_sqlx_interpolations,
    outside_sql_comments as _outside_sql_comments,
    split_sqlx_sections as _split_sqlx_sections,
)

if TYPE_CHECKING:
    from .pipeline import Pipeline


# --------------------------------------------------------------------- loading


def _reference_label(reference: object) -> str:
    if isinstance(reference, str):
        return reference.strip()
    if isinstance(reference, NodeIdentity):
        return reference.key
    if isinstance(reference, Mapping):
        nested = reference.get("tableReference")
        if isinstance(nested, Mapping):
            reference = nested
        parts = [
            str(reference.get(key, ""))
            for key in ("projectId", "datasetId", "tableId")
            if reference.get(key)
        ]
        if not parts:
            parts = [
                str(reference.get(key, ""))
                for key in ("database", "schema", "name")
                if reference.get(key)
            ]
        return ".".join(parts) if parts else str(reference)
    if all(hasattr(reference, key) for key in ("database", "schema", "name")):
        return ".".join(
            str(getattr(reference, key))
            for key in ("database", "schema", "name")
            if getattr(reference, key)
        )
    if getattr(reference, "parts", None) is not None:
        return ".".join(part.name for part in reference.parts)
    return str(reference)


def _is_asset_reference(reference: object) -> bool:
    if isinstance(reference, NodeIdentity):
        return reference.kind == "asset"
    if not isinstance(reference, str):
        return False
    normalized = reference.replace("\\", "/")
    return "/" in normalized or normalized.lower().endswith((".sql", ".sqlx"))


# The arguments may hold one level of calls: ``ref({schema: functions.baseSchema("ga4"), name: "event"})``.
_REF_ARGS = r"(?P<args>(?:[^()]|\([^()]*\))*?)"
_REF_RE = re.compile(r"\$\{\s*(?:ctx\.)?ref\(\s*" + _REF_ARGS + r"\s*\)\s*\}")
_RESOLVE_RE = re.compile(r"\$\{\s*(?:ctx\.)?resolve\(\s*" + _REF_ARGS + r"\s*\)\s*\}")
_SELF_RE = re.compile(r"\$\{\s*(?:ctx\.)?self\(\s*\)\s*\}")
_CONFIG_DEPENDENCIES_RE = re.compile(r"\bdependencies\s*:\s*(?:\[(?P<list>[^\]]*)\]|(?P<one>\{[^}]*\}|['\"`][^'\"`]*['\"`]))")
_DEPENDENCY_ITEM_RE = re.compile(r"\{[^}]*\}|['\"`][^'\"`]*['\"`]")
_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")


def _sub_outside_comments(pattern: re.Pattern[str], replace: Callable[[re.Match[str]], str], text: str) -> str:
    """``pattern.sub(replace, text)`` that leaves SQL comments alone: Dataform does not evaluate ``${...}`` in them."""

    pieces: list[str] = []
    cursor = 0
    for match in _outside_sql_comments(text, pattern):
        pieces.append(text[cursor : match.start()])
        pieces.append(replace(match))
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def _top_level(config: str) -> str:
    """The text directly inside a config's outer braces, with nested braces, brackets and strings blanked.

    A ``name:`` inside ``columns: {...}`` or a ``dependencies`` object must not be taken
    for the action's own ``name``.
    """

    start = config.find("{")
    if start < 0:
        return config
    out: list[str] = []
    depth = 0
    quote: str | None = None
    index = start
    while index < len(config):
        char = config[index]
        if quote:
            if char == "\\" and index + 1 < len(config):
                out.append(config[index : index + 2] if depth <= 1 else "  ")
                index += 2
                continue
            if char == quote:
                quote = None
            out.append(char if depth <= 1 else " ")
        elif char in "'\"`":
            quote = char
            out.append(char if depth <= 1 else " ")
        elif char in "{[(":
            out.append(char if depth == 0 else " ")
            depth += 1
        elif char in "}])":
            depth -= 1
            out.append(char if depth == 0 else " ")
        else:
            out.append(char if depth <= 1 else " ")
        index += 1
    return "".join(out)


def _config_value(config: str, key: str) -> str | None:
    match = re.search(rf"\b{key}\s*:\s*(['\"`])((?:\\.|(?!\1).)*)\1", _top_level(config))
    return match.group(2) if match else None


def _config_literal(config: str, key: str) -> tuple[str | None, bool]:
    """``(value, computed)`` for a config key: a quoted literal reads as itself, anything else is computed.

    ``type: dataform.projectConfig.vars.kind``, ``type: "a" + x`` and ``type: `${x}``` are computed: their value is
    only known by running the project, so no default may stand in for it. An absent key is ``(None, False)``.
    """

    match = re.search(rf"\b{key}\s*:\s*", _blank_strings(_top_level(config)))
    if not match:
        return None, False
    # _top_level starts at the outer brace and blanks without changing lengths, so its offsets shift by that start.
    value = _value_at(config, max(config.find("{"), 0) + match.end())
    quoted = re.fullmatch(r"""\s*(['"`])((?:\\.|(?!\1).)*)\1\s*""", value, re.S)
    if quoted and "${" not in quoted.group(2):
        return quoted.group(2), False
    return None, True


_PROJECT_DEFAULT_RE = re.compile(r"dataform\s*\.\s*projectConfig\s*\.\s*(defaultDatabase|defaultSchema|vars\s*\.\s*([A-Za-z_]\w*))")


def _config_identity(
    config: str, key: str, database: str, dataset: str, variables: Mapping[str, str] | None = None
) -> tuple[str | None, bool]:
    """``_config_literal`` for ``database``, ``schema`` and ``name``, also reading the project's own settings:
    ``dataform.projectConfig.defaultDatabase``, ``defaultSchema`` and ``vars.NAME`` (a string in the project's ``vars``).
    A setting the project does not hold stays computed."""

    value, computed = _config_literal(config, key)
    if not computed:
        return value, False
    match = re.search(rf"\b{key}\s*:\s*", _blank_strings(_top_level(config)))
    raw = _value_at(config, max(config.find("{"), 0) + match.end()).strip() if match else ""
    named = _PROJECT_DEFAULT_RE.fullmatch(raw)
    if named and named.group(2):
        found = (variables or {}).get(named.group(2))
        if found:
            return found, False
    elif named and key in ("database", "schema") and (named.group(1) == "defaultDatabase") == (key == "database"):
        known = database if key == "database" else dataset
        if known:
            return known, False
    return None, True


_JS_WORD_STRING = r"""(?:"[\w\- ]*"|'[\w\- ]*'|\d+)"""
# ``constants.SCHEMA``, ``functions.baseSchema("ga4")``: a module's member, maybe called with literals.
_JS_GLOBAL_EXPRESSION = re.compile(
    rf"(?P<root>[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)+(?:\((?:{_JS_WORD_STRING}(?:,{_JS_WORD_STRING})*)?\))?"
)


def _compact_expression(expression: str) -> str:
    """``expression`` without whitespace outside its strings."""

    return re.sub(r"\s+(?=[^\"']*(?:[\"'][^\"']*[\"'][^\"']*)*$)", "", expression.strip())


def _is_placeholder(part: str) -> bool:
    """Whether a table part is a :func:`_placeholder` rather than a value."""

    return part.startswith("{") and part.endswith("}")


def _placeholder(expression: str, modules: Iterable[str]) -> str | None:
    """What a computed ``schema`` or ``database`` stands for when it is not a string: a placeholder spelling it, or None.

    An expression of an ``includes/`` module, called with literal arguments at most, is the
    same table part wherever it is written, so it becomes ``{functions:baseSchema("ga4")}`` (dots made colons so the
    name stays one part). Two actions whose datasets come from different expressions then stay apart, and a ``ref()``
    written with the same expression finds its action. The placeholder is not a dataset: the action keeps its
    ``dynamic_config`` gap. An expression of anything else (a local variable, a computed argument) is None.
    """

    compact = _compact_expression(expression)
    found = _JS_GLOBAL_EXPRESSION.fullmatch(compact)
    if not found or found.group("root") not in set(modules):
        return None
    return "{" + compact.replace("'", '"').replace(".", ":") + "}"


def _config_expression(config: str, key: str) -> str | None:
    """The expression a config object sets ``key`` to when it is not a string literal; else None."""

    match = re.search(rf"\b{key}\s*:\s*", _blank_strings(_top_level(config)))
    if not match:
        return None
    expression = _value_at(config, max(config.find("{"), 0) + match.end()).strip()
    return None if not expression or expression[0] in "'\"`" else expression


def _config_flag(config: str, key: str) -> bool | None:
    """``True``/``False`` for a literal ``key: true|false`` of the config's own top level; ``None`` when absent or computed."""

    match = re.search(rf"\b{key}\s*:\s*", _blank_strings(_top_level(config)))
    if not match:
        return None
    value = _value_at(config, max(config.find("{"), 0) + match.end()).strip()
    return {"true": True, "false": False}.get(value)


def _value_at(text: str, start: int) -> str:
    """The JavaScript value starting at ``start``: up to the next comma or closing brace outside brackets and strings."""

    depth, quote, index = 0, "", start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return text[start:index]


_COLUMN_READING_KEYS = ("nonNull", "uniqueKey", "uniqueKeys", "rowConditions", "partitionBy", "clusterBy", "updatePartitionFilter")
_ANY_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""", re.S)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _blank_strings(text: str) -> str:
    """``text`` with the inside of every string replaced by spaces, so keys are only found outside strings."""

    return _ANY_STRING_RE.sub(lambda m: m.group(1) + " " * len(m.group(2)) + m.group(1), text)


# Top-level config keys @dataform/cli 3.0.71 accepts, by action type: every key it did not reject as "Unexpected property"
# when each was tried alone on a minimal project of that type (a key it rejects for its value, as with `protected: false` on a table,
# is still a known key).
_COMMON_CONFIG_KEYS = frozenset(['additionalOptions', 'assertions', 'bigquery', 'columns', 'database', 'dataset', 'dependOnDependencyAssertions', 'dependencies', 'dependencyTargets', 'description', 'disabled', 'filename', 'hermetic', 'iceberg', 'incrementalPredicates', 'labels', 'metadata', 'name', 'preserveGovernanceControls', 'project', 'protected', 'requirePartitionFilter', 'schema', 'tags', 'type'])
_CONFIG_KEYS = {
    "table": _COMMON_CONFIG_KEYS | frozenset(['clusterBy', 'fileName', 'partitionBy', 'partitionExpirationDays', 'reservation']),
    "view": _COMMON_CONFIG_KEYS | frozenset(['clusterBy', 'fileName', 'materialized', 'partitionBy', 'reservation']),
    "incremental": _COMMON_CONFIG_KEYS | frozenset(['clusterBy', 'fileName', 'incrementalStrategy', 'onSchemaChange', 'partitionBy', 'partitionExpirationDays', 'reservation', 'uniqueKey', 'updatePartitionFilter']),
    "assertion": _COMMON_CONFIG_KEYS | frozenset(['fileName', 'reservation']),
    "operations": _COMMON_CONFIG_KEYS | frozenset(['fileName', 'hasOutput', 'reservation']),
    "declaration": _COMMON_CONFIG_KEYS | frozenset([]),
}
_CONFIG_KEYS["test"] = _CONFIG_KEYS["table"]
_ACTION_TYPES = frozenset(_CONFIG_KEYS)
_CONFIG_KEY_RE = re.compile(r"(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*:")


def _config_problems(config: str, declared_type: str | None) -> list[str]:
    """What Dataform's compiler rejects in a config block, read without running it: an unrecognized action ``type``, or
    a top-level key the action's type does not accept (``bigqueryPolicy``, ``uniqueKey`` on a table, ``partitionBy`` on an
    assertion). A missing type is a table; the caller skips a computed type, since the accepted keys then depend on it."""

    if not config:
        return []
    if declared_type is not None and declared_type not in _ACTION_TYPES:
        return [f"type {declared_type!r} is not an action type Dataform recognizes"]
    allowed = _CONFIG_KEYS[declared_type or "table"]
    problems = []
    for key in dict.fromkeys(_CONFIG_KEY_RE.findall(_blank_strings(_top_level(config)))):
        if key not in allowed:
            problems.append(f"config key {key!r} is not accepted for type {declared_type or 'table'!r}")
    return problems


def _column_reads(config: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(words, unread keys)`` for the config values that read the table's own columns.

    Built-in assertions (``nonNull``, ``uniqueKey``, ``uniqueKeys``, ``rowConditions``), partitioning and clustering
    (``partitionBy``, ``clusterBy``), ``updatePartitionFilter`` and an incremental table's ``uniqueKey`` all name or
    use output columns. Dataform runs or applies them against the table, so dropping such a column breaks them even
    when no query reads it. ``words`` holds every identifier in those values (a superset of the columns, never
    fewer) and each quoted value whole (a column that is not a plain identifier). A value that is not a literal
    string or list of strings is listed in ``unread keys``: its columns are unknown.
    """

    blanked = _blank_strings(config)
    words: dict[str, None] = {}
    unread: dict[str, None] = {}
    for key in _COLUMN_READING_KEYS:
        for match in re.finditer(rf"(?<![\w$.])['\"`]?{key}['\"`]?\s*:\s*", blanked):
            value = _value_at(config, match.end())
            for literal in _ANY_STRING_RE.finditer(value):
                text = literal.group(2)
                words[text.strip().lower()] = None
                for found in _WORD_RE.findall(text):
                    words[found.lower()] = None
                for quoted in re.findall(r"`([^`]+)`", text):
                    words[quoted.lower()] = None
            if re.sub(r"[\s,\[\]]", "", _ANY_STRING_RE.sub("", value)):
                unread[key] = None
    return tuple(w for w in words if w), tuple(unread)


def _compiled_column_reads(item: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The same, from a compiled table entry: its ``assertions`` and ``bigquery`` blocks and a top-level ``uniqueKey``."""

    words: dict[str, None] = {}
    unread: dict[str, None] = {}

    def add(key: str, value: object) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, str):
            words[value.strip().lower()] = None
            for found in _WORD_RE.findall(value):
                words[found.lower()] = None
            for quoted in re.findall(r"`([^`]+)`", value):
                words[quoted.lower()] = None
        elif isinstance(value, list):
            for entry in value:
                add(key, entry.get("uniqueKey") if isinstance(entry, dict) and "uniqueKey" in entry else entry)
        else:
            unread[key] = None

    for block in (item, item.get("assertions"), item.get("bigquery")):
        if isinstance(block, dict):
            for key in _COLUMN_READING_KEYS:
                if key in block:
                    add(key, block[key])
    return tuple(w for w in words if w), tuple(unread)


def _config_tags(config: str) -> tuple[str, ...]:
    """Tags from a Dataform config block: ``tags: ["a", "b"]`` or ``tags: "a"``."""

    match = re.search(r"\btags\s*:\s*(?:\[(?P<list>[^\]]*)\]|(?P<one>['\"`][^'\"`]*['\"`]))", config)
    if not match:
        return ()
    # ``tags: []`` matches the list branch with an empty string; ``or`` would fall through to the unmatched ``one`` (None).
    listed, single = match.group("list"), match.group("one")
    return tuple(dict.fromkeys(re.findall(r"['\"`]([^'\"`]+)['\"`]", listed if listed is not None else single or "")))


def _braced(text: str, start: int) -> str:
    """The text of the ``{...}`` block opening at ``start`` (strings skipped); empty if unbalanced."""

    depth, index, quote = 0, start, ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
        index += 1
    return ""


def _name_list(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"['\"`]([^'\"`]+)['\"`]", text))


def _config_assertions(config: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """``(non_null columns, unique keys)`` from ``assertions: {nonNull, uniqueKey, uniqueKeys}``."""

    match = re.search(r"\bassertions\s*:\s*\{", config)
    if not match:
        return (), ()
    block = _braced(config, match.end() - 1)
    non_null: tuple[str, ...] = ()
    keys: list[tuple[str, ...]] = []
    found = re.search(r"\bnonNull\s*:\s*(\[[^\]]*\]|['\"`][^'\"`]*['\"`])", block)
    if found:
        non_null = _name_list(found.group(1))
    found = re.search(r"\buniqueKey\s*:\s*(\[[^\]]*\]|['\"`][^'\"`]*['\"`])", block)
    if found and _name_list(found.group(1)):
        keys.append(_name_list(found.group(1)))
    found = re.search(r"\buniqueKeys\s*:\s*\[((?:\s*\[[^\]]*\]\s*,?)+)\]", block)
    if found:
        keys.extend(_name_list(group) for group in re.findall(r"\[([^\]]*)\]", found.group(1)) if _name_list(group))
    return non_null, tuple(dict.fromkeys(keys))


def _config_dependencies(config: str) -> tuple[str, ...]:
    """Arguments of each entry in a config block's ``dependencies`` (a name or a ``{name, schema}`` object)."""

    match = _CONFIG_DEPENDENCIES_RE.search(config)
    if not match:
        return ()
    listed, single = match.group("list"), match.group("one")
    return tuple(_DEPENDENCY_ITEM_RE.findall(listed if listed is not None else single or ""))


def _read_project_defaults(
    root: Path, diagnostics: list[PipelineDiagnostic] | None = None
) -> tuple[str, str]:
    """Return (default project, default dataset) from Dataform settings, if any.

    An unreadable or malformed settings file yields empty defaults and a
    ``settings_unreadable`` diagnostic when ``diagnostics`` is given.
    """

    try:
        return _read_project_defaults_strict(root)
    except (OSError, UnicodeError, ValueError, AttributeError) as exc:
        if diagnostics is None:
            raise
        reason = (
            describe_os_error(exc)
            if isinstance(exc, (OSError, UnicodeError))
            else "settings file is not valid JSON"
        )
        diagnostics.append(
            PipelineDiagnostic("", "settings_unreadable", f"{reason}; project defaults were not applied")
        )
        return "", ""


@dataclass(frozen=True)
class _Naming:
    """Project settings that change every action's final target (not declarations'): ``projectSuffix`` /
    ``databaseSuffix``, ``datasetSuffix`` / ``schemaSuffix`` and ``namePrefix`` / ``tablePrefix``; and the default location."""

    database_suffix: str = ""
    schema_suffix: str = ""
    name_prefix: str = ""
    location: str = ""
    # The project's ``vars`` (``dataform.projectConfig.vars.NAME``), when they are plain strings.
    variables: dict[str, str] = field(default_factory=dict)

    def apply(self, target: Target) -> Target:
        """The target Dataform compiles an action to (``database_ps.schema_sandbox.prefix_name``)."""

        # A placeholder for a computed dataset (see ``_placeholder``) is not a name a suffix can be added to.
        database = (f"{target.database}_{self.database_suffix}"
                    if target.database and self.database_suffix and not _is_placeholder(target.database) else target.database)
        schema = (f"{target.schema}_{self.schema_suffix}"
                  if target.schema and self.schema_suffix and not _is_placeholder(target.schema) else target.schema)
        name = f"{self.name_prefix}_{target.name}" if target.name and self.name_prefix else target.name
        return Target(database, schema, name)


def _read_naming(root: Path) -> _Naming:
    """Prefix, suffix and location settings of ``workflow_settings.yaml`` or ``dataform.json``; none when unreadable.

    ``_read_project_defaults`` already reports an unreadable settings file, so this one stays quiet.
    """

    try:
        settings = root / "workflow_settings.yaml"
        legacy = root / "dataform.json"
        if settings.is_symlink() or legacy.is_symlink():
            return _Naming()
        if settings.is_file():
            text = settings.read_text(encoding="utf-8-sig")

            def value(*keys: str) -> str:
                for key in keys:
                    match = re.search(rf"(?m)^\s*{key}\s*:\s*['\"]?([^'\"\s#]+)", text)
                    if match:
                        return match.group(1)
                return ""

            variables: dict[str, str] = {}
            block = re.search(r"(?m)^vars\s*:[ \t]*(?:#.*)?\r?\n((?:[ \t]+\S.*(?:\r?\n|$)|[ \t]*(?:#.*)?\r?\n)*)", text)
            for entry in re.finditer(r"(?m)^[ \t]+([A-Za-z_]\w*)\s*:\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s#'\"][^#\r\n]*?))\s*(?:#.*)?$",
                                     block.group(1) if block else ""):
                variables[entry.group(1)] = next(g for g in entry.groups()[1:] if g is not None)
            return _Naming(value("projectSuffix", "databaseSuffix"), value("datasetSuffix", "schemaSuffix"),
                           value("namePrefix", "tablePrefix"), value("defaultLocation"), variables)
        if legacy.is_file():
            data = json.loads(legacy.read_text(encoding="utf-8-sig"))

            def field_(key: str) -> str:
                found = data.get(key)
                return found if isinstance(found, str) else ""

            raw_vars = data.get("vars")
            variables = {k: v for k, v in raw_vars.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw_vars, dict) else {}
            return _Naming(field_("databaseSuffix"), field_("schemaSuffix"), field_("tablePrefix"), field_("defaultLocation"), variables)
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return _Naming()


def _assertion_dataset(root: Path) -> str:
    """The project's assertion dataset, or ``""`` when it sets none: assertions then live in the default dataset
    (``@dataform/cli`` 3.0.71 compiles a project without ``defaultAssertionDataset`` that way)."""

    try:
        settings = root / "workflow_settings.yaml"
        if settings.is_symlink():
            return ""
        if settings.is_file():
            match = re.search(r"(?m)^\s*defaultAssertionDataset\s*:\s*['\"]?([^'\"\s#]+)", settings.read_text(encoding="utf-8-sig"))
            return match.group(1) if match else ""
        legacy = root / "dataform.json"
        if legacy.is_symlink():
            return ""
        if legacy.is_file():
            value = json.loads(legacy.read_text(encoding="utf-8-sig")).get("assertionSchema")
            return value if isinstance(value, str) and value else ""
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return ""


def _read_project_defaults_strict(root: Path) -> tuple[str, str]:
    settings = root / "workflow_settings.yaml"
    if settings.is_symlink():
        raise OSError("symbolic links are not read")
    if settings.is_file():
        text = settings.read_text(encoding="utf-8-sig")

        def value(key: str) -> str:
            match = re.search(rf"(?m)^\s*{key}\s*:\s*['\"]?([^'\"\s#]+)", text)
            return match.group(1) if match else ""

        return value("defaultProject"), value("defaultDataset")
    legacy = root / "dataform.json"
    if legacy.is_symlink():
        raise OSError("symbolic links are not read")
    if legacy.is_file():
        data = json.loads(legacy.read_text(encoding="utf-8-sig"))
        return data.get("defaultDatabase", ""), data.get("defaultSchema", "")
    return "", ""


# ------------------------------------------------------- JavaScript declarations

_JS_CALL_RE = re.compile(r"(?<![\w.$])(declare|publish)\s*\(")
_JS_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")


def _call_arguments(text: str, opening: int) -> str:
    """The text between the parenthesis at ``opening`` and its match (strings skipped); empty if unbalanced."""

    depth, quote, index = 0, "", opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
        index += 1
    return ""


def _split_top_level(text: str) -> list[str]:
    """``text`` split on commas that are not inside brackets or strings."""

    parts, depth, quote, start, index = [], 0, "", 0, 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _js_string(expression: str) -> str | None:
    match = re.fullmatch(r"""\s*(['"`])((?:\\.|(?!\1)[^$])*)\1\s*""", expression)
    return match.group(2) if match and "${" not in match.group(2) else None


def _js_bindings(text: str, name: str) -> list[str] | None:
    """The literal strings the identifier ``name`` can hold in this file, or None when it cannot be known."""

    found: list[list[str]] = []
    for match in re.finditer(rf"\bconst\s+{re.escape(name)}\s*=\s*(?P<value>['\"`][^'\"`$]*['\"`])\s*[;\n]", text):
        found.append([match.group("value")[1:-1]])
    loops = (
        rf"\[(?P<a>[^\[\]]*)\]\s*\.forEach\(\s*\(?\s*{re.escape(name)}\b",
        rf"\bfor\s*\(\s*(?:const|let|var)\s+{re.escape(name)}\s+of\s*\[(?P<a>[^\[\]]*)\]",
    )
    for pattern in loops:
        for match in re.finditer(pattern, text):
            items = _split_top_level(match.group("a"))
            values = [_js_string(item) for item in items]
            if not items or any(value is None for value in values):
                return None
            found.append([value for value in values if value is not None])
    # Any other binding of the same identifier (a parameter, a computed value) makes it unknowable.
    others = len(re.findall(rf"\b(?:const|let|var)\s+{re.escape(name)}\b", text))
    others += len(re.findall(rf"[(,]\s*{re.escape(name)}\s*(?:,[^)]*)?\)\s*=>", text))
    others += len(re.findall(rf"\bfunction\s*\w*\s*\([^)]*\b{re.escape(name)}\b", text))
    if len(found) != 1 or others > 1:
        return None
    return found[0]


def _js_field(config: str, text: str, key: str) -> list[str] | None | str:
    """Possible values of ``key`` in a config object: a list, ``""`` when absent, None when not knowable."""

    inner = config.strip()
    inner = inner[1:-1] if inner.startswith("{") and inner.endswith("}") else inner
    for entry in _split_top_level(inner):
        pair = re.match(r"""^(?:['"`]?)([\w$]+)(?:['"`]?)\s*(?::\s*(.*))?$""", entry, re.S)
        if not pair or pair.group(1) != key:
            continue
        value = pair.group(2)
        if value is None:  # shorthand ``{ name }``
            return _js_bindings(text, key)
        if value.strip() == "undefined":
            return ""
        literal = _js_string(value)
        if literal is not None:
            return [literal]
        if _JS_IDENT_RE.match(value.strip()):
            return _js_bindings(text, value.strip())
        return None
    return ""


_JS_LOOP_RES = (
    re.compile(r"(?P<src>[\w$.]+|\[[^\[\]]*\])\s*\.forEach\(\s*\(?\s*(?P<var>[\w$]+)"),
    re.compile(r"\bfor\s*\(\s*(?:const|let|var)\s+(?P<var>[\w$]+)\s+of\s+(?P<src>[\w$.]+|\[[^\[\]]*\])\s*\)"),
)


class _EmptyCompilation(Exception):
    """The Dataform API answered but its compilation listed no actions, which settles nothing."""


def js_declared_targets(
    text: str,
    default: Target,
    *,
    modules: Mapping[str, Mapping[str, object]] | None = None,
    path: str = "",
    placeholders: Callable[[str], str | None] | None = None,
) -> tuple[list[Target], list[Target], bool]:
    """Declarations and named actions found in one Dataform JavaScript file.

    Returns ``(declared, actions, complete)``. ``declared`` are ``declare({...})`` targets and ``actions`` the
    ``publish("name", {...})`` ones, both with project defaults filled in. ``complete`` is False when a call's
    name, schema or database cannot be read without running the code (computed in a function, a loop over a
    list that is not literal): the file then may declare tables that are not listed. A loop over a literal list
    of tables, in this file or in a module it ``require``s (``modules``: exports by project path), is expanded.
    A schema or database written as an includes module's member called with literals (``functions.baseSchema("ga4")``) is read as the
    placeholder ``placeholders`` gives for it (see :func:`_placeholder`), so such an action is listed, not unknown.
    """

    from . import js_literals

    declared: list[Target] = []
    actions: list[Target] = []
    complete = True
    scope: dict[str, object] | None = None

    def fields(config: str, name_args: list[str], kind: str, source: str):
        if kind == "declare":
            names = _js_field(config, source, "name") if config.startswith("{") else None
        else:
            first = _js_string(name_args[0])
            names = [first] if first is not None else (_js_bindings(source, name_args[0]) if _JS_IDENT_RE.match(name_args[0]) else None)
        schemas = _js_field(config, source, "schema") if config.startswith("{") else ""
        databases = _js_field(config, source, "database") if config.startswith("{") else ""
        if placeholders is not None and config.startswith("{"):
            if schemas is None and (expression := _config_expression(config, "schema")) and (stands_for := placeholders(expression)):
                schemas = [stands_for]
            if databases is None and (expression := _config_expression(config, "database")) and (stands_for := placeholders(expression)):
                databases = [stands_for]
        if not names or schemas is None or databases is None:
            return None
        return [(name, db, schema) for name in names for db in (databases or [default.database]) for schema in (schemas or [default.schema])]

    for match in _JS_CALL_RE.finditer(text):
        kind = match.group(1)
        args = _split_top_level(_call_arguments(text, match.end() - 1))
        if kind == "declare":
            config, name_args = (args[0] if args else ""), []
        else:
            if not args:
                continue
            config, name_args = (args[1] if len(args) > 1 and args[1].startswith("{") else "{}"), args
        found = fields(config, name_args, kind, text)
        if found is None:
            # Maybe a loop over a literal list of tables, here or in a required module.
            if scope is None:
                scope = {**js_literals.local_constants(text), **js_literals.required_bindings(text, path, dict(modules or {}))}
            found = []
            looped = False
            for pattern in _JS_LOOP_RES:
                headers = [m for m in pattern.finditer(text, 0, match.start()) if re.search(rf"(?<![\w$.]){re.escape(m.group('var'))}\b", config + " ".join(name_args))]
                if not headers:
                    continue
                header = headers[-1]
                items = js_literals.source_items(header.group("src"), scope)
                if items is js_literals._MISSING:
                    break
                looped = True
                for item in items:
                    one = js_literals.substitute(config, header.group("var"), item)
                    one_args = [js_literals.substitute(arg, header.group("var"), item) for arg in name_args]
                    if one is None or any(arg is None for arg in one_args):
                        found = None
                        break
                    each = fields(one, one_args, kind, one)
                    if each is None:
                        found = None
                        break
                    found += each
                break
            if not looped or found is None:
                complete = False
                continue
        for name, database, schema in found:
            (declared if kind == "declare" else actions).append(Target(database, schema, name))
    return declared, actions, complete


_JS_TEMPLATE_RE = re.compile(r"""\s*(['"`])((?:(?!\1)[^\\])*)\1\s*""", re.S)
_JS_ARROW_RE = re.compile(r"""\s*(?:\(\s*\w*\s*\)|\w+)\s*=>\s*(?P<body>.*)""", re.S)
_JS_FUNCTION_RE = re.compile(r"""\s*function\s*\w*\s*\(\s*\w*\s*\)\s*\{\s*return\s+(?P<body>.*?);?\s*\}\s*""", re.S)
_JS_BLOCK_ARROW_RE = re.compile(r"""\s*(?:\(\s*\w*\s*\)|\w+)\s*=>\s*\{\s*return\s+(?P<body>.*?);?\s*\}\s*""", re.S)


def _js_query_text(argument: str) -> str | None:
    """The SQL of a ``.query()`` argument written out in the file: a string or template literal, or a function of ``ctx``
    that returns one. ``None`` for anything computed (a call, a concatenation, an escape sequence)."""

    argument = argument.strip()
    for pattern in (_JS_BLOCK_ARROW_RE, _JS_FUNCTION_RE, _JS_ARROW_RE):
        found = pattern.fullmatch(argument)
        if found:
            argument = found.group("body").strip()
            break
    literal = _JS_TEMPLATE_RE.fullmatch(argument)
    if literal is None:
        return None
    quote, body = literal.group(1), literal.group(2)
    if quote != "`" and "${" in body:
        return None  # not an interpolation in a plain string
    return body


def _js_depth_at(text: str, position: int) -> int:
    """Bracket depth at ``position``, skipping strings and comments: 0 at the top level of the file."""

    depth, quote, index = 0, "", 0
    while index < position:
        char = text[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index < 0:
                break
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 1
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        index += 1
    return depth


def js_published_assets(text: str) -> list[tuple[str, str]]:
    """``(name, sqlx)`` for each top-level ``publish("name", {config}).query(...)`` in a JavaScript file whose query is
    written out as a literal, as the SQLX Dataform would read from a ``.sqlx`` file with the same config and body.

    Only a literal name, a literal config object and a literal query count; a publish inside a function or loop, a
    computed query and chained calls other than ``query`` and ``config`` are left to the declaration scan, which
    reports the names it cannot read.
    """

    found: list[tuple[str, str]] = []
    for match in _JS_CALL_RE.finditer(text):
        if match.group(1) != "publish" or _js_depth_at(text, match.start()) != 0:
            continue
        opening = match.end() - 1
        arguments = _split_top_level(_call_arguments(text, opening))
        name = _js_string(arguments[0]) if arguments else None
        if name is None or len(arguments) > 3:
            continue
        config, query = "", None
        for argument in arguments[1:]:
            if argument.startswith("{"):
                config = argument
            else:
                query = _js_query_text(argument)
        end = opening + len(_call_arguments(text, opening)) + 2
        chained = True
        while chained and (call := re.match(r"\s*\.\s*(query|config)\s*\(", text[end:])):
            inner_open = end + call.end() - 1
            inner = _call_arguments(text, inner_open)
            if call.group(1) == "query":
                query = _js_query_text(inner)
            elif inner.strip().startswith("{"):
                config = inner.strip()
            else:
                chained = False
            end = inner_open + len(inner) + 2
        if query is None:
            continue
        body = config.strip()[1:-1] if config.strip().startswith("{") else ""
        if _config_literal("config { " + body + " }", "name") != (None, False):
            continue  # a name in the config object as well as the argument: not read
        sqlx = "config { " + (body.strip().rstrip(",") + ", " if body.strip() else "") + f"name: {json.dumps(name)} }}\n{query}\n"
        found.append((name, sqlx))
    return found


_PLAIN_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""", re.S)


def _plain_strings(args: str, placeholders: Callable[[str], str | None] | None = None) -> list[str]:
    """The arguments of a ``ref()`` call, each a plain string literal; anything computed raises ``ValueError``.

    ``ref("f" + "eed")`` names the one table ``feed``, not two arguments ``f`` and ``eed``, and ``ref(name)`` names
    whatever ``name`` holds: neither is read by picking out the quoted pieces. The one exception is a database or
    dataset (never the name) written as an includes module's member called with literals, ``ref(functions.baseSchema("ga4"), "event")``:
    ``placeholders`` (see :func:`_placeholder`) gives the part that stands for it.
    """

    items = _split_top_level(args)
    found = []
    for index, part in enumerate(items):
        quoted = _PLAIN_STRING_RE.fullmatch(part)
        if quoted is not None and "${" not in quoted.group(2):
            found.append(quoted.group(2))
            continue
        stands_for = placeholders(part) if placeholders is not None and quoted is None and index < len(items) - 1 else None
        if stands_for is None:
            raise ValueError("ref() has a computed argument, so the table it names is not known; it was left unresolved")
        found.append(stands_for)
    return found


def _parse_ref_args(
    args: str,
    default: Target,
    known: dict[str, list[Target]] | None = None,
    *,
    names_may_be_missing: bool = False,
    schema_settles: bool = False,
    logical: Mapping[Target, tuple[str, ...]] | None = None,
    variables: Mapping[str, str] | None = None,
    placeholders: Callable[[str], str | None] | None = None,
) -> Target:
    """The target a ``ref()`` names.

    Dataform finds ``ref("name")`` by the action's name, wherever its config put
    it, so a name that ``known`` (every action and declaration by name) holds
    once resolves to that target; otherwise the project defaults apply. A name that
    several targets hold, or one that no action names while ``names_may_be_missing``
    (JavaScript declarations that could not be read), is not guessed: it raises, as does
    any argument that is not a plain string. ``known`` is keyed by the name as the config
    wrote it; ``logical`` gives the database, schema and name of a target that a project
    prefix or suffix renamed, which is what a ``ref()`` names it by. ``placeholders(expression)`` gives the part that
    stands for a computed database or dataset (see :func:`_placeholder`); a ref whose dataset is such a placeholder
    that no action of that name shares stays unresolved, since the expression may evaluate to another dataset.
    """

    def coordinates(target: Target) -> tuple[str, ...]:
        return (logical or {}).get(target, (target.database, target.schema, target.name))

    def candidates(name: str, schema: str | None, database: str | None) -> list[Target]:
        return [
            target for target in (known or {}).get(name, [])
            if (schema is None or coordinates(target)[1] == schema) and (database is None or coordinates(target)[0] == database)
        ]

    def unresolved(name: str, schema: str | None = None, database: str | None = None) -> None:
        matches = candidates(name, schema, database)
        if len(matches) > 1:
            raise ValueError("ref() names several tables; it was left unresolved")
        if not matches and any(part is not None and _is_placeholder(part) for part in (schema, database)):
            raise ValueError("ref() names its dataset by an expression that is no known action's dataset; it was left unresolved")
        if not matches and names_may_be_missing:
            if schema is not None and schema_settles:
                return  # the ref names its dataset and no declaration sets a database: the project default is exact
            raise ValueError("ref() names a table that a declaration may define; it was left unresolved")

    def by_name(name: str, schema: str | None = None, database: str | None = None) -> Target | None:
        matches = candidates(name, schema, database)
        return matches[0] if len(matches) == 1 else None

    if args.lstrip().startswith("{"):
        values = {}
        for key in ("name", "schema", "database"):
            value, computed = _config_identity(args, key, default.database, default.schema, variables)
            if computed:
                value = placeholders(expression) if placeholders and key != "name" and (expression := _config_expression(args, key)) else None
                if value is None:
                    raise ValueError(f"ref() has a computed {key}, so the table it names is not known; it was left unresolved")
            values[key] = value
        name, schema, database = values["name"] or "", values["schema"], values["database"]
        found = by_name(name, schema, database)
        if found is not None:
            return found
        unresolved(name, schema, database)
        return Target(database or default.database, schema or default.schema, name)
    parts = _plain_strings(args, placeholders)
    if len(parts) == 1:
        found = by_name(parts[0])
        if found is None:
            unresolved(parts[0])
        return found or Target(default.database, default.schema, parts[0])
    if len(parts) == 2:
        found = by_name(parts[1], parts[0])
        if found is None:
            unresolved(parts[1], parts[0])
        return found or Target(default.database, parts[0], parts[1])
    if len(parts) >= 3:
        found = by_name(parts[2], parts[1], parts[0])
        return found or Target(parts[0], parts[1], parts[2])
    raise ValueError("unsupported ref() arguments")


def load_sqlx_project(
    root: str | Path,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
    compiled_targets: Callable[[], Iterable[tuple[tuple[str, str, str], bool]]] | None = None,
) -> Pipeline:
    """Load a Dataform project (``definitions/**.sqlx``) or a folder of ``.sql`` files.

    ``${ref(...)}`` and ``${self()}`` are resolved to table names using the
    project defaults; other interpolations are masked so the SQL still
    parses. For exact compiled SQL, prefer :func:`load_compiled_graph` with
    the output of ``dataform compile --json``.
    """

    root = extended_path(root)
    if not root.is_dir():
        raise PipelineLoadError("project folder was not found or is not a directory")
    diagnostics: list[PipelineDiagnostic] = []
    database, dataset = _read_project_defaults(root, diagnostics)
    assertion_dataset = _assertion_dataset(root) or dataset
    naming = _read_naming(root)
    try:
        include_modules = {path.stem for path in (root / "includes").glob("*.js")}
    except OSError:
        include_modules = set()

    def placeholders(expression: str) -> str | None:
        """What a computed database or dataset stands for in a config or a ``ref()``, or None (see ``_placeholder``)."""

        return _placeholder(expression, include_modules)

    definitions = root / "definitions"
    # A definitions junction must be pruned relative to the selected project root,
    # including on Python versions without Path.is_junction().
    use_definitions = (definitions.is_dir() and not definitions.is_symlink()
                       and definitions.resolve().is_relative_to(root.resolve()))
    search_root = definitions if use_definitions else root
    models: dict[str, Model] = {}
    sources: dict[str, Target] = {}

    def unlistable(directory: Path, reason: str) -> None:
        try:
            label = str(directory.relative_to(root))
        except ValueError:
            label = "."
        file_link = directory.is_symlink() and not directory.is_dir()
        code = "read_error" if file_link else "unreadable_directory"
        effect = "asset was skipped" if file_link else "files inside were not analyzed"
        diagnostics.append(PipelineDiagnostic(label, code, f"{reason}; {effect}"))

    def add_model(model: Model) -> None:
        if model.key in models:
            diagnostics.append(
                PipelineDiagnostic(
                    model.path or model.key,
                    "duplicate_model",
                    f"defines the same table as {models[model.key].path or model.key}; the earlier definition was replaced",
                )
            )
        models[model.key] = model

    # Read every asset first: a ref() names an action wherever its config put it.
    known: dict[str, list[Target]] = {}
    renamed: dict[Target, tuple[str, ...]] = {}  # targets a project prefix or suffix changed -> as the config wrote them
    computed_identities = False  # an asset's name, schema or database is computed: refs to unlisted names stay unresolved

    def read_asset(path: Path):
        relative = str(path.relative_to(root))
        text, reason = read_text_or_reason(path)
        if text is None:
            diagnostics.append(PipelineDiagnostic(relative, "read_error", f"{reason}; asset was skipped"))
            return None
        return read_text_asset(relative, path.stem, path.suffix, text)

    def read_text_asset(relative: str, stem: str, suffix: str, text: str):
        if suffix == ".sql" and not _SQLX_CONFIG_RE.search(text):
            target = Target(name=stem)
            add_model(Model(target, "sql", text, relative))
            return None
        try:
            sections = _split_sqlx_sections(text)
        except ValueError as exc:
            diagnostics.append(PipelineDiagnostic(relative, "sqlx_parse_error", str(exc)))
            return None
        config = next(
            (body for kind, body in sections if kind == "block" and body.lstrip().startswith("config")),
            "",
        )
        declared_type, computed_type = _config_literal(config, "type")
        kind = declared_type or ("unknown" if computed_type else "table")
        for problem in () if computed_type else _config_problems(config, declared_type):
            diagnostics.append(PipelineDiagnostic(relative, "invalid_config", f"{problem}, so Dataform rejects the project"))
        identity, computed_identity, placed = {}, [], []
        for key in ("database", "schema", "name"):
            identity[key], computed = _config_identity(config, key, database, dataset, naming.variables)
            if computed:
                computed_identity.append(key)
                expression = _config_expression(config, key) if key != "name" else None
                if expression and (stands_for := placeholders(expression)):
                    identity[key] = stands_for
                    placed.append(key)
        logical = Target(
            identity["database"] or database,
            identity["schema"] or (assertion_dataset if kind == "assertion" else dataset),
            identity["name"] or stem,
        )
        # Declarations name an existing table: the project's prefix and suffix settings leave them alone.
        target = logical if kind == "declaration" else naming.apply(logical)
        if computed_identity:
            # A computed name is also what a ref() finds the action by, so it cannot be found at all; a computed database
            # or schema leaves the name known, so refs still find the action (at the project defaults, the best guess).
            diagnostics.append(PipelineDiagnostic(
                target.key, "dynamic_config",
                f"its config {' and '.join(computed_identity)} is computed (a project variable or a call), so which table it "
                "writes is not known" + ("; refs by name do not find it" if "name" in computed_identity else
                                         "; a placeholder spelling the expression stands in for it" if placed == computed_identity else
                                         "; the project default stands in for it")))
            if kind != "declaration":
                kind = "unknown"
            if "name" in computed_identity:
                nonlocal computed_identities
                computed_identities = True
        if "name" not in computed_identity:
            if target not in known.setdefault(logical.name, []):  # a JavaScript publish already listed it
                known[logical.name].append(target)
            if target != logical:
                renamed[target] = (logical.database, logical.schema, logical.name)
        if computed_type:
            diagnostics.append(PipelineDiagnostic(
                target.key, "dynamic_config",
                "its config type is computed (a project variable or a call), so it is not read as a table: it may be incremental"))
        if kind == "declaration":
            if "name" not in computed_identity:
                sources[target.key] = target
            return None
        return relative, sections, config, kind, target

    def load_asset(relative: str, sections, config: str, kind: str, target: Target) -> None:
        default = Target(database, dataset, "")
        body = "".join(section for kind_, section in sections if kind_ == "sql")
        dependencies: list[Target] = []

        def checked(ref: Target, what: str, *, needs_output: bool = False) -> Target:
            """``ref`` after noting a name Dataform would not resolve, or an operation without an output it reads."""

            if ref not in known_targets:
                spelled = sorted({name for name in known if name.lower() == ref.name.lower() and name != ref.name})
                hint = f"; the action is named {spelled[0]!r} (names are case-sensitive)" if spelled else ""
                diagnostics.append(PipelineDiagnostic(
                    target.key, "missing_ref",
                    f"{what} names {ref.name!r}, which no action or declaration of the project defines, so Dataform rejects "
                    f"the project{hint}"))
            elif needs_output:
                reads_output.append((target.key, ref))
            return ref

        def substitute(match: re.Match[str]) -> str:
            try:
                ref = _parse_ref_args(match.group("args"), default, known, **unknown_names())
            except ValueError as exc:
                diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", str(exc)))
                return match.group(0)  # left masked like any other interpolation
            dependencies.append(checked(ref, "ref()", needs_output=True))
            return ref.sql()

        body = _sub_outside_comments(_REF_RE, substitute, body)
        body = _sub_outside_comments(_SELF_RE, lambda match: target.sql(), body)
        if kind == "operations":
            # Dataform separates the statements of an operations file by a line of ``---`` as well as by semicolons.
            body = re.sub(r"(?m)^[ \t]*---[ \t]*\r?$", ";", body)
        # Dataform evaluates ref() in pre_operations and post_operations too, so what they ref is a dependency.
        for kind_, section in sections:
            if kind_ == "block" and re.match(r"\s*(?:pre|post)_operations\b", section):
                for match in _outside_sql_comments(section, _REF_RE):
                    try:
                        ref = _parse_ref_args(match.group("args"), default, known, **unknown_names())
                    except ValueError as exc:
                        diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", str(exc)))
                        continue
                    if ref not in dependencies:
                        dependencies.append(checked(ref, "ref()", needs_output=True))

        def resolve(match: re.Match[str]) -> str:
            try:
                return checked(_parse_ref_args(match.group("args"), default, known, **unknown_names()), "resolve()").sql()
            except ValueError:
                return match.group(0)  # computed argument: left masked like any other interpolation

        body = _sub_outside_comments(_RESOLVE_RE, resolve, body)

        def plain_ref(match: re.Match[str]) -> str:
            try:
                return _parse_ref_args(match.group("args"), default, known, names_may_be_missing=incomplete_js or computed_identities,
                                       logical=renamed, variables=naming.variables, placeholders=placeholders).sql()
            except ValueError:
                return match.group(0)

        operations: list[str] = []
        for kind_, section in sections:
            if kind_ == "block" and re.match(r"\s*(?:pre|post)_operations\b", section) and "{" in section and "}" in section:
                inner = section[section.index("{") + 1 : section.rindex("}")]
                inner = _sub_outside_comments(_REF_RE, plain_ref, inner)
                inner = _sub_outside_comments(_RESOLVE_RE, plain_ref, inner)
                inner = _sub_outside_comments(_SELF_RE, lambda match: target.sql(), inner)
                if "${" in inner:
                    inner, _restorations = _mask_sqlx_interpolations(inner)
                if inner.strip():
                    operations.append(inner)
        for entry in _config_dependencies(config):
            try:
                declared = _parse_ref_args(entry, default, known, **unknown_names())
            except ValueError as exc:
                diagnostics.append(PipelineDiagnostic(target.key, "unsupported_ref", f"config dependencies: {exc}"))
                continue
            if declared not in dependencies:
                dependencies.append(checked(declared, "a config dependency"))
        masked: tuple[str, ...] = ()
        if "${" in body:
            body, restorations = _mask_sqlx_interpolations(body)
            masked = tuple(item.original for item in restorations)
        try:
            tags = _config_tags(config)
        except Exception as exc:  # noqa: BLE001 - tags are optional; never lose the model over them
            diagnostics.append(PipelineDiagnostic(relative, "config_tags_unreadable", f"could not read tags ({type(exc).__name__}: {exc}); the model was kept without tags"))
            tags = ()
        try:
            non_null, unique_keys = _config_assertions(config)
        except Exception:  # noqa: BLE001 - assertions are optional evidence
            non_null, unique_keys = (), ()
        try:
            config_reads, config_reads_unread = _column_reads(config)
        except Exception:  # noqa: BLE001 - a config that cannot be read keeps every column of the model in use
            config_reads, config_reads_unread = (), ("config",)
        add_model(Model(target, kind, body, relative, tuple(dependencies), masked, tags, non_null, unique_keys, tuple(operations),
                        config_reads=config_reads, config_reads_unread=config_reads_unread,
                        logical=renamed.get(target, ()), disabled=_config_flag(config, "disabled") is True,
                        has_output=_config_flag(config, "hasOutput") is True))

    def unreadable(relative: str, exc: Exception) -> None:
        diagnostics.append(PipelineDiagnostic(
            relative, "asset_unreadable",
            f"could not be analyzed ({type(exc).__name__}: {exc}); asset was skipped"))

    incomplete_js = False
    js_sets_database = False

    def unknown_names() -> dict:
        return {"names_may_be_missing": incomplete_js or computed_identities, "schema_settles": not js_sets_database,
                "logical": renamed, "variables": naming.variables, "placeholders": placeholders}

    js_files: dict[str, str] = {}
    for path in find_assets(root, (".js",), unlistable):
        relative_parts = path.relative_to(root).parts
        if "node_modules" in relative_parts or ".git" in relative_parts:
            continue
        text, _reason = read_text_or_reason(path)
        if text is not None:
            js_files["/".join(relative_parts)] = text
    from . import js_literals

    modules = {name[:-3]: js_literals.module_exports(text) for name, text in js_files.items() if "module.exports" in text or "exports." in text}
    for relative_js, text in js_files.items():
        path = root / relative_js
        if not _JS_CALL_RE.search(text):
            continue
        declared, actions, complete = js_declared_targets(
            text, Target(database, dataset, ""), modules=modules, path=relative_js, placeholders=placeholders)
        for target in declared:
            known.setdefault(target.name, []).append(target)
            sources[target.key] = target
        for target in actions:
            final = naming.apply(target)
            if final not in known.setdefault(target.name, []):
                known[target.name].append(final)
            if final != target:
                renamed[final] = (target.database, target.schema, target.name)
        if not complete:
            incomplete_js = True
            js_sets_database = js_sets_database or bool(re.search(r"\bdatabase\b", text))
            diagnostics.append(PipelineDiagnostic(
                str(path.relative_to(root)), "js_declaration_dynamic",
                "declares or names tables that cannot be read without running the code; refs to unlisted names were left unresolved"))

    if incomplete_js and compiled_targets is None:
        diagnostics.append(PipelineDiagnostic(
            "", "compiled_graph_not_requested",
            "the Dataform API fallback for computed declarations was not used: no Dataform repository is connected to this "
            "load, so refs to unlisted names stay unresolved"))
    if incomplete_js and compiled_targets is not None:
        # Dataform's own compilation lists every action, which settles what the JavaScript could not.
        try:
            fetched = list(compiled_targets())
            if not fetched:
                raise _EmptyCompilation()
        except Exception as exc:  # noqa: BLE001 - no credentials, offline, no matching repository: stay unresolved
            reason = getattr(exc, "reason", None)
            outcome = "was skipped" if getattr(exc, "attempted", True) is False else "was tried and failed"
            detail = f" ({reason}): {exc}" if reason else f" ({type(exc).__name__})"
            if isinstance(exc, _EmptyCompilation):
                detail = " (empty_compilation): the compilation lists no actions"
            diagnostics.append(PipelineDiagnostic(
                "", "compiled_graph_unavailable",
                f"the Dataform API fallback for computed declarations {outcome}{detail}; refs to unlisted names stay unresolved"))
        else:
            for (target_db, target_schema, target_name), is_declaration in fetched:
                target = Target(target_db, target_schema, target_name)
                if target not in known.setdefault(target.name, []):
                    known[target.name].append(target)
                if is_declaration:
                    sources[target.key] = target
            incomplete_js = False
            diagnostics.append(PipelineDiagnostic(
                "", "compiled_graph_read",
                f"the Dataform API fallback for computed declarations read {len(fetched)} compiled actions "
                f"({sum(1 for _, is_declaration in fetched if is_declaration)} declarations)"))

    known_targets: set[Target] = set()
    reads_output: list[tuple[str, Target]] = []  # (reading action, table it ref()s): checked against operations once all load
    pending = []
    for path in find_assets(search_root, (".sqlx", ".sql"), unlistable):
        try:
            asset = read_asset(path)
        except Exception as exc:  # noqa: BLE001 - one odd file must not fail the whole project
            unreadable(str(path.relative_to(root)), exc)
            continue
        if asset is not None:
            pending.append(asset)
    for relative_js, text in js_files.items():
        if use_definitions and not relative_js.startswith("definitions/"):
            continue  # includes are required by definitions, not run as definitions
        if not _JS_CALL_RE.search(text):
            continue
        try:
            for name, sqlx in js_published_assets(text):
                asset = read_text_asset(relative_js, name, ".sqlx", sqlx)
                if asset is not None:
                    pending.append(asset)
        except Exception as exc:  # noqa: BLE001 - one odd file must not fail the whole project
            unreadable(relative_js, exc)
    known_targets.update(target for targets in known.values() for target in targets)
    for asset in pending:
        try:
            load_asset(*asset)
        except Exception as exc:  # noqa: BLE001 - one odd file must not fail the whole project
            unreadable(asset[0], exc)

    for reader, ref in reads_output:
        operation = models.get(ref.key)
        if operation is not None and operation.kind == "operations" and not operation.has_output:
            diagnostics.append(PipelineDiagnostic(
                reader, "ref_to_operation_without_output",
                f"ref() names the operations action {ref.name!r}, which has no output (hasOutput), so Dataform rejects the project"))

    from .pipeline import Pipeline  # deferred: pipeline imports this module

    return Pipeline(
        models,
        sources,
        dict(source_schema or {}),
        diagnostics,
        default_project=database,
        default_dataset=dataset,
        default_location=naming.location,
    )


def load_compiled_graph(
    graph: str | Path | dict,
    *,
    source_schema: dict[str, dict[str, str]] | None = None,
) -> Pipeline:
    """Load the JSON printed by ``dataform compile --json``."""

    if isinstance(graph, (str, Path)):
        graph = parse_json_or_raise(Path(graph), "compiled graph")
    if not isinstance(graph, dict):
        raise PipelineLoadError("compiled graph must be a JSON object")

    diagnostics: list[PipelineDiagnostic] = []

    def target_of(raw: object) -> Target:
        if not isinstance(raw, dict):
            raise ValueError("target is not an object")
        return Target(*(str(raw.get(field) or "") for field in ("database", "schema", "name")))

    def list_of(kind_key: str) -> list:
        value = graph.get(kind_key, [])
        if not isinstance(value, list):
            diagnostics.append(
                PipelineDiagnostic(kind_key, "invalid_entry", f"{kind_key!r} is not a list; its entries were skipped")
            )
            return []
        return value

    errors = graph.get("graphErrors")
    for entry in (errors.get("compilationErrors") if isinstance(errors, dict) else None) or []:
        # Dataform rejected the project: what is loaded below is partial, never a validated compilation.
        if isinstance(entry, dict):
            where = entry.get("actionName") or entry.get("fileName") or ""
            message = " ".join(str(entry.get("message") or "compilation error").split())
            diagnostics.append(PipelineDiagnostic(str(where), "compilation_error", message[:500]))
    if isinstance(errors, dict) and errors and not any(d.code == "compilation_error" for d in diagnostics):
        diagnostics.append(PipelineDiagnostic("", "compilation_error", "the compiler reported graph errors that could not be read"))

    models: dict[str, Model] = {}
    for kind_key, default_kind in (("tables", "table"), ("assertions", "assertion"), ("operations", "operations")):
        for index, item in enumerate(list_of(kind_key)):
            label = f"{kind_key}[{index}]"
            try:
                target = target_of(item.get("target", {}))
                label = target.key or label
                sql = item.get("query")
                if sql is None:
                    sql = ";\n".join(item.get("queries", []))
                if not isinstance(sql, str):
                    raise ValueError("query is not text")
                kind = item.get("type", default_kind) if kind_key == "tables" else default_kind
                file_name = item.get("fileName")
                asserted = item.get("assertions") if isinstance(item.get("assertions"), dict) else {}
                non_null = asserted.get("nonNull", [])
                non_null = (non_null,) if isinstance(non_null, str) else tuple(c for c in non_null if isinstance(c, str))
                unique = []
                if isinstance(asserted.get("uniqueKey"), list):
                    unique.append(tuple(c for c in asserted["uniqueKey"] if isinstance(c, str)))
                if isinstance(asserted.get("uniqueKeys"), list):
                    for entry in asserted["uniqueKeys"]:
                        columns = entry.get("uniqueKey") if isinstance(entry, dict) else entry
                        if isinstance(columns, list):
                            unique.append(tuple(c for c in columns if isinstance(c, str)))
                config_reads, config_reads_unread = _compiled_column_reads(item)
                model = Model(
                    target,
                    str(kind),
                    sql,
                    file_name if isinstance(file_name, str) else None,
                    tuple(target_of(dep) for dep in item.get("dependencyTargets", [])),
                    tags=tuple(tag for tag in item.get("tags", []) if isinstance(tag, str)),
                    non_null=non_null,
                    unique_keys=tuple(dict.fromkeys(k for k in unique if k)),
                    operations_sql=tuple(
                        text for key_ in ("preOps", "postOps") for text in (item.get(key_) or []) if isinstance(text, str) and text.strip()
                    ),
                    pre_operations=sum(1 for text in (item.get("preOps") or []) if isinstance(text, str) and text.strip()),
                    config_reads=config_reads,
                    config_reads_unread=config_reads_unread,
                    disabled=item.get("disabled") is True,
                    has_output=item.get("hasOutput") is True,
                    incremental_sql=tuple(
                        text for text in (
                            item.get("incrementalQuery"),
                            *(item.get("incrementalPreOps") or []),
                            *(item.get("incrementalPostOps") or []),
                        ) if isinstance(text, str) and text.strip()
                    ),
                )
            except (AttributeError, TypeError, ValueError):
                diagnostics.append(
                    PipelineDiagnostic(label, "invalid_entry", "entry is malformed; asset was skipped")
                )
                continue
            key = target.key or (model.identity.key if model.identity else "")
            if key:
                if key in models:
                    diagnostics.append(
                        PipelineDiagnostic(key, "duplicate_model", "defined more than once; the earlier definition was replaced")
                    )
                models[key] = model
    sources: dict[str, Target] = {}
    for index, item in enumerate(list_of("declarations")):
        try:
            target = target_of(item.get("target", {}))
        except (AttributeError, TypeError, ValueError):
            diagnostics.append(
                PipelineDiagnostic(f"declarations[{index}]", "invalid_entry", "entry is malformed; asset was skipped")
            )
            continue
        sources[target.key] = target
    project_config = graph.get("projectConfig") if isinstance(graph.get("projectConfig"), dict) else {}

    def defaults(*keys: str) -> str:
        """A project default: the compiler keeps them under ``projectConfig``; older output had them at the top."""

        for block in (project_config, graph):
            for key in keys:
                if isinstance(block.get(key), str) and block[key]:
                    return block[key]
        return ""

    from .pipeline import Pipeline  # deferred: pipeline imports this module

    return Pipeline(
        models,
        sources,
        dict(source_schema or {}),
        diagnostics,
        default_project=defaults("defaultDatabase", "defaultProject"),
        default_dataset=defaults("defaultSchema", "defaultDataset"),
        default_location=defaults("defaultLocation"),
    )

if TYPE_CHECKING:
    from .pipeline import Pipeline


# -------------------------------------------------------------------- analysis


class _TargetResolver:
    """Resolve full, dataset-qualified or bare table names to known keys."""

    def __init__(self, keys: list[str]):
        self._by_suffix: dict[str, set[str]] = defaultdict(set)
        for key in keys:
            parts = key.split(".")
            for start in range(len(parts)):
                self._by_suffix[".".join(parts[start:])].add(key)

    def _matches(self, parts: list[str], start: int) -> set[str]:
        found = self._by_suffix.get(".".join(parts[start:]), set())
        if start:
            found = {key for key in found if key.count(".") + 1 <= len(parts) - start}
        return found

    def resolve(self, table: exp.Table | str) -> str | None:
        if isinstance(table, exp.Table):
            name = ".".join(part.name for part in table.parts)
        else:
            name = table.strip("`")
        parts = name.split(".")
        # Try the most specific spelling first, then drop leading qualifiers. A dropped qualifier only
        # matches a model keyed with no more qualifiers than are left: ``raw.orders`` is not the model
        # ``p.staging.orders``, but ``p.ds.orders`` finds a model keyed ``ds.orders`` or ``orders``.
        for start in range(len(parts)):
            matches = self._matches(parts, start)
            if matches and len(matches) == 1:
                return next(iter(matches))
            if matches:
                return None
        return None

    def is_ambiguous(self, table: exp.Table | str) -> bool:
        """True when the name matches several known tables, not none."""

        if isinstance(table, exp.Table):
            name = ".".join(part.name for part in table.parts)
        else:
            name = table.strip("`")
        parts = name.split(".")
        for start in range(len(parts)):
            matches = self._matches(parts, start)
            if matches:
                return len(matches) > 1
        return False
