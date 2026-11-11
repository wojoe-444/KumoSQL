"""The loaded project behind the agent-facing server: parsed once, analysed once, asked many times.

A :class:`Session` holds the :class:`~kumosql.pipeline.Pipeline` and the results every tool needs
(table profiles, inferred roles, lineage records, unused columns), computed the first time they are asked for
and kept. It also turns what an agent types for a table (``orders``, ``sales.orders``, a full key in any case)
into the one model key the project uses, or says which tables are close.
"""

from __future__ import annotations

import difflib
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from . import data_profile_store as store
from .pipeline import Pipeline
from .pipeline_loading import load_compiled_graph, load_sqlx_project
from .resilience import PipelineLoadError, parse_json_or_raise
from .schema_snapshot import SchemaSnapshot, SchemaSnapshotError, load_schema_snapshot


if TYPE_CHECKING:
    from .boundary import Boundary, BoundarySpec


def load_project(root: Path, source_schema: Path | None = None) -> Pipeline:
    """The project the CLI's ``pipeline-report`` would load: a Dataform folder, a folder of ``.sql`` files or a compiled graph."""

    schema = parse_json_or_raise(source_schema, "source schema file") if source_schema else None
    if schema is not None and not isinstance(schema, dict):
        raise PipelineLoadError("source schema file must be a JSON object")
    if root.is_file():
        return load_compiled_graph(root, source_schema=schema)
    return load_sqlx_project(root, source_schema=schema)


def parse_assumed_grain(values: Sequence[str] | None) -> dict[str, tuple[str, ...]]:
    """``["dataset.table=id,day"]`` as ``{"dataset.table": ("id", "day")}``; raises ``ValueError`` on a malformed entry."""

    found: dict[str, tuple[str, ...]] = {}
    for value in values or ():
        table, sep, columns = str(value).rpartition("=")
        names = tuple(dict.fromkeys(c.strip() for c in columns.split(",") if c.strip()))
        if not sep or not table.strip() or not names:
            raise ValueError(f"expected TABLE=column[,column...], got {value!r}")
        found[table.strip()] = names
    return found


class Session:
    """One project, kept in memory. Pass ``pipeline`` (tests) or ``loader`` (called once, on first use)."""

    def __init__(
        self,
        pipeline: Pipeline | None = None,
        *,
        loader: Callable[[], Pipeline] | None = None,
        profiles: Path | None = None,
        assume_grain: Mapping[str, Sequence[str]] | None = None,
        root: Path | None = None,
        schema_snapshot: Path | SchemaSnapshot | None = None,
        boundary: "Path | BoundarySpec | None" = None,
        docs: Sequence[Path] = (),
        malloy: Sequence[Path] = (),
    ) -> None:
        self._assume_grain = dict(assume_grain or {})
        #: a file of column types (``--schema-snapshot``) or an already loaded snapshot; read on first use, never fetched
        self._snapshot_source = schema_snapshot
        #: why the snapshot could not be read, when it could not
        self.snapshot_error: str | None = None
        #: folders of Markdown docs and Malloy files that may state a table's primary key (``--docs``, ``--malloy``)
        self._docs, self._malloy = tuple(docs), tuple(malloy)
        self._pipeline = pipeline
        #: the project folder; local files a tool is asked to read must sit inside it (the working folder when unknown)
        self.root = root
        self._loader = loader
        self._lock = threading.RLock()
        self._cache: dict[str, object] = {}
        self.profiles_directory = profiles
        self.load_error: str | None = None
        #: the warm-start controller (:class:`kumosql.mcp_cache.Index`) and the factory of the trace memo each loaded project gets
        self.index = None
        self.trace_memo_factory: Callable[[], object] | None = None
        #: a producers-and-consumers file (``--boundary``), as a path (read on first use) or an already read spec
        self._boundary_source = boundary
        #: why the boundary file could not be applied (answers then say so under ``unknown``); ``None`` when it was or none was given
        self.boundary_error: str | None = None

    # ------------------------------------------------------------- the project

    @property
    def loaded(self) -> bool:
        return self._pipeline is not None

    @property
    def configured(self) -> bool:
        """Whether a project was given at all (a profiles-only server has none)."""

        return self._pipeline is not None or self._loader is not None

    def pipeline(self) -> Pipeline | None:
        """The project, loading it on first use. ``None`` when none was given or loading failed (``load_error`` says why)."""

        with self._lock:
            if self._pipeline is None and self._loader is not None and self.load_error is None:
                try:
                    self._pipeline = self._loader()
                    if self.trace_memo_factory is not None:
                        self._pipeline.trace_memo = self.trace_memo_factory()  # saved column traces of unchanged models are reused
                except PipelineLoadError as exc:
                    self.load_error = str(exc)
                except Exception as exc:  # noqa: BLE001 - a bad project must not stop the server
                    self.load_error = f"the project could not be loaded ({type(exc).__name__})"
            return self._pipeline

    def warm(self, stage: Callable[[str], None] | None = None) -> None:
        """Do the slow work now (parse, trace columns, profile tables), so the first question is fast.

        ``stage`` is told what is starting (``loading project``, ``tracing columns``, ``profiling tables``, ``inferring roles``).
        """

        stage = stage or (lambda _name: None)
        stage("loading project")
        pipeline = self.pipeline()
        if pipeline is None:
            return
        try:
            stage("tracing columns")
            self.records()
            stage("profiling tables")
            self.profiles()
            stage("inferring roles")
            self.roles()
            self.column_types()
            from .mcp_expression import expression_index

            expression_index(self)
            outside = self.boundary()
            if outside is not None:
                outside.warm()
        except Exception as exc:  # noqa: BLE001 - the first question computes it again and reports what failed
            print(f"kumosql mcp: warm-up stopped ({type(exc).__name__})", file=sys.stderr)

    def adopt(self, other: "Session") -> None:
        """Take over the project and everything computed for it from ``other`` (a session built for the edited project)."""

        with self._lock:
            self._pipeline, self._cache, self.load_error = other._pipeline, other._cache, other.load_error
            self.boundary_error = other.boundary_error

    def boundary(self) -> "Boundary | None":
        """The producers and consumers outside the project (see :mod:`kumosql.boundary`), or ``None`` when none were given or the file failed (``boundary_error`` says why)."""

        if self._boundary_source is None or self.pipeline() is None:
            return None
        return self._once("boundary", self._load_boundary)

    def _load_boundary(self) -> "Boundary | None":
        from . import boundary as outside

        try:
            spec = self._boundary_source
            if not isinstance(spec, outside.BoundarySpec):
                spec = outside.read_file(Path(spec))
            return outside.build(spec, self)
        except outside.BoundaryError as exc:
            self.boundary_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a bad boundary file must not stop the server
            self.boundary_error = f"the boundary file could not be applied ({type(exc).__name__})"
        print(f"kumosql mcp: --boundary not applied: {self.boundary_error}", file=sys.stderr)
        return None

    def _once(self, key: str, compute: Callable[[], object]):
        with self._lock:
            if key not in self._cache:
                self._cache[key] = compute()
            return self._cache[key]

    def records(self) -> dict:
        """One-hop lineage of every output column (``Pipeline.explain_lineage``)."""

        pipeline = self.pipeline()
        return self._once("records", pipeline.explain_lineage)

    def profiles(self) -> dict:
        """Grain, row scope and column meanings of every model (``profile_pipeline``)."""

        pipeline = self.pipeline()
        return self._once("profiles", lambda: self._profiles_with_claims(pipeline))

    def _profiles_with_claims(self, pipeline) -> dict:
        """Profiles with the stated grain of tables the project does not build; a model's query wins, a claim fills only a grain the query leaves unknown."""

        from .table_profile import profile_pipeline

        declared = self.declared_grain()
        profiles = profile_pipeline(pipeline, declared_grain=declared)
        extra = {key: claim.keys for key, claim in self.grain_claims().items()
                 if claim.in_project and key in pipeline.models and key in profiles and not profiles[key].grain.known and not (declared or {}).get(key)}
        return profile_pipeline(pipeline, declared_grain={**(declared or {}), **extra}) if extra else profiles

    def declared_grain(self) -> dict[str, tuple[str, ...]] | None:
        """The grains declared for a table, by table key; ``None`` when there are none.

        ``--assume-grain`` (the person starting the server) wins over what the project states for a table it does not build
        (:meth:`grain_claims`).

        A table the project does not have, or a column a model does not output, is dropped with a line on stderr:
        a declaration that cannot apply must not pass for knowledge.
        """

        return self._once("declared_grain", self._resolve_assumptions)

    def grain_claims(self) -> dict:
        """Where a grain the project does not show comes from: one claim per table (:mod:`kumosql.source_grain`), keyed by project key or, for a warehouse table outside the project, its casefolded name.

        Read from declarations and assertion files, ``--malloy`` and ``--docs`` folders and ``--assume-grain``. A claim whose columns the table does
        not have is dropped with a line on stderr.
        """

        return self._once("grain_claims", self._collect_claims)

    def grain_claim(self, key: str):
        return self.grain_claims().get(key)

    def _collect_claims(self) -> dict:
        from . import docs_keys, malloy_keys, source_grain
        from .source_grain import GrainClaim

        pipeline = self.pipeline()
        docs = [item for folder in self._docs for item in docs_keys.read_folder(folder)]
        malloy = [item for folder in self._malloy for item in malloy_keys.read_folder(folder)]
        claims = source_grain.collect(pipeline, self.resolve, docs, malloy)
        for text, columns in self._assume_grain.items():
            key, _close = self.resolve(text)
            if key is not None:
                claims.append(GrainClaim(key, tuple(columns), "--assume-grain", "assume", True))
            elif text.count(".") >= 1:
                claims.append(GrainClaim(text.strip("`"), tuple(columns), "--assume-grain", "assume", False))
        usable = []
        for claim in claims:
            if claim.in_project:
                have = {c.lower() for c in pipeline.output_columns(claim.table)} if claim.table in pipeline.models else {c.lower() for c in pipeline.source_schema.get(claim.table, {})}
                missing = [c for c in claim.keys if have and c.lower() not in have]
                if missing:
                    print(f"kumosql mcp: grain of {claim.table} ({claim.origin}): not columns of the table: {', '.join(missing)}", file=sys.stderr)
                    continue
            usable.append(claim)
        return source_grain.best(usable)

    def _resolve_assumptions(self) -> dict[str, tuple[str, ...]] | None:
        pipeline = self.pipeline()
        found: dict[str, tuple[str, ...]] = {key: claim.keys for key, claim in self.grain_claims().items() if claim.in_project and key in pipeline.sources}
        for text, columns in self._assume_grain.items():
            key, _close = self.resolve(text)
            if key is None:
                print(f"kumosql mcp: --assume-grain names no table of the project: {text}", file=sys.stderr)
                continue
            if key in pipeline.models:
                have = {c.lower() for c in pipeline.output_columns(key)}
                missing = [c for c in columns if c.lower() not in have]
                if missing:
                    print(f"kumosql mcp: --assume-grain {text}: not output columns of the model: {', '.join(missing)}", file=sys.stderr)
                    continue
            found[key] = tuple(columns)
        return found or None

    def schema_snapshot(self) -> SchemaSnapshot | None:
        """The schema snapshot the server was started with (``--schema-snapshot``), read once; ``None`` when there is none.

        A file that cannot be read gives ``None`` and sets ``snapshot_error``: checks that need types then say the snapshot
        is unreadable instead of passing.
        """

        if self._snapshot_source is None:
            return None
        return self._once("schema_snapshot", self._read_snapshot)

    def _read_snapshot(self) -> SchemaSnapshot | None:
        source = self._snapshot_source
        if isinstance(source, SchemaSnapshot):
            return source
        from .project_types import snapshot_filter

        try:
            # streamed; rows of tables the project does not use are dropped as they are read, never kept
            return load_schema_snapshot(source, keep=snapshot_filter(self.tables()))
        except SchemaSnapshotError as exc:
            self.snapshot_error = str(exc)
            return None

    def column_types(self):
        """The type of every column of every table of the project, computed once (:mod:`kumosql.project_types`):
        from the schema snapshot or the source declarations, carried through the models in dependency order."""

        from . import project_types

        return self._once("column_types", lambda: project_types.build(self))

    def column_type(self, key: str, column: str):
        """The :class:`~kumosql.googlesql_types.GType` of ``key.column`` (a table key of the project), or ``None``."""

        return self.column_types().type(key, column)

    def column_is_nullable(self, key: str, column: str) -> bool | None:
        """``True`` when ``key.column`` may be NULL, ``False`` when it never is, ``None`` when not known (the schema
        snapshot's ``is_nullable``, else what the model's query fixes)."""

        return self.column_types().nullable(key, column)

    def roles(self) -> dict:
        from .table_roles import infer_roles

        pipeline = self.pipeline()
        return self._once("roles", lambda: infer_roles(pipeline))

    def dead_columns(self) -> dict:
        pipeline = self.pipeline()
        return self._once("dead", pipeline.dead_columns)

    def downstream(self) -> dict:
        pipeline = self.pipeline()
        return self._once("downstream", lambda: pipeline.downstream)

    def order(self) -> dict[str, int]:
        """Each model's place in dependency order."""

        pipeline = self.pipeline()
        return self._once("order", lambda: {key: i for i, key in enumerate(pipeline.topological_order())})

    # ------------------------------------------------------------------ names

    def tables(self) -> dict[str, str]:
        """Every table key the project knows (models and declared sources), by key."""

        pipeline = self.pipeline()
        return self._once("tables", lambda: {key: key for key in (*pipeline.models, *pipeline.sources)})

    def resolve(self, text: object) -> tuple[str | None, list[str]]:
        """The table key for what an agent wrote, and when there is none, up to five close table keys.

        Accepts a full key, ``dataset.table`` or the bare table name, in any case, with or without backticks.
        A name that matches several tables is not guessed: ``None`` and the candidates.
        """

        pipeline = self.pipeline()
        name = str(text or "").strip().strip("`").strip()
        if not name:
            return None, []
        tables = self.tables()
        if name in tables:
            return name, []
        try:
            resolved = pipeline.resolve(name)
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved in tables:
            return resolved, []
        folded = name.casefold()
        exact = [key for key in tables if key.casefold() == folded]
        if len(exact) == 1:
            return exact[0], []
        parts = len(folded.split("."))
        tail = [key for key in tables if ".".join(key.casefold().split(".")[-parts:]) == folded]
        if len(tail) == 1:
            return tail[0], []
        if tail:
            return None, sorted(tail)[:5]
        return None, self._close(folded, tables)

    @staticmethod
    def _close(folded: str, tables: dict[str, str]) -> list[str]:
        """Up to five table keys near ``folded``, the closest first (a misspelling should name its likely target first)."""

        last = folded.split(".")[-1]
        by_tail: dict[str, list[str]] = {}
        for key in tables:
            by_tail.setdefault(key.casefold().split(".")[-1], []).append(key)
        near = set(difflib.get_close_matches(last, list(by_tail), n=5, cutoff=0.6))
        near.update(tail for tail in by_tail if last in tail)
        ranked = sorted(near, key=lambda tail: (-difflib.SequenceMatcher(None, last, tail).ratio(), tail))
        found: list[str] = []
        for tail in ranked:
            found.extend(sorted(by_tail[tail]))
        return found[:5]

    # ------------------------------------------------------- saved data profiles

    def saved_profile(self, key: str) -> dict | None:
        """The newest saved data profile of table ``key`` (see ``python -m kumosql profile-table``), or ``None``."""

        pipeline = self.pipeline()
        target = pipeline.models[key].target if key in pipeline.models else None
        wanted = {key.casefold()}
        if target is not None and target.name:
            wanted.add(".".join(part for part in (target.database or pipeline.default_project, target.schema or pipeline.default_dataset, target.name) if part).casefold())
        rows = self._profile_rows()
        for row in rows:
            if str(row["table"]).strip("`").casefold() in wanted:
                return row
        return None

    def _profile_rows(self) -> list[dict]:
        """``list_profiles``, read again only when a file in the folder was added, changed or removed."""

        try:
            folder = store._directory(self.profiles_directory)
            signature = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in sorted(folder.glob("*.json")))
        except OSError:
            return []
        with self._lock:
            cached = self._cache.get("profile_rows")
            if cached is None or cached[0] != signature:
                cached = (signature, store.list_profiles(self.profiles_directory))
                self._cache["profile_rows"] = cached
            return cached[1]

    # --------------------------------------------------- key observations from saved profiles

    def key_observations(self) -> list:
        """The key measurements in the saved data profiles and join profiles (:mod:`kumosql.join_profile_store`), re-read only when a file changed."""

        from . import join_profile_store as joins

        try:
            signature = joins.signature(self.profiles_directory)
        except OSError:
            return []
        with self._lock:
            cached = self._cache.get("key_observations")
            if cached is None or cached[0] != signature:
                cached = (signature, joins.load_observations(self.profiles_directory))
                self._cache["key_observations"] = cached
            return cached[1]

    def profile_folder_outside_project(self) -> Path | None:
        """The folder saved profiles go to, or ``None`` when it lies inside the project: the server never writes into the repository."""

        folder = store._directory(self.profiles_directory).resolve()
        if self.root is not None and folder.is_relative_to(Path(self.root).resolve()):
            return None
        return folder

    def grain_origin(self, key: str, grain) -> str | None:
        """Where a table's grain comes from, in words: the claim that states it, the model's own ``uniqueKey``, or its query."""

        if grain.status == "derived":
            for read in sorted(self.pipeline().upstream.get(key, ())):
                claim = self.grain_claim(read)
                if claim is not None:
                    return f"derived from the query over {read.split('.')[-1]}, whose grain is stated by {claim.origin}"
            return "derived from the query"
        if grain.status != "declared":
            return None
        claim = self.grain_claim(key)
        if claim is not None and {k.lower() for k in claim.keys} == {k.lower() for k in grain.keys}:
            return claim.origin
        model = self.pipeline().models.get(key)
        if model is not None and any({c.lower() for c in k} == {c.lower() for c in grain.keys} for k in model.unique_keys):
            return f"declared uniqueKey on model {key.split('.')[-1]}"
        return "--assume-grain"
