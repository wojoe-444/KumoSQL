"""Stable identities and normalization for query graph references."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Mapping

from .paths import posix_path


IdentityKind = Literal["table", "wildcard", "system", "asset", "unresolved"]


@dataclass(frozen=True, order=True)
class NodeIdentity:
    """A case-preserving identity for a table, pattern, system view, or asset.

    ``key`` keeps the dotted spelling used by existing pipeline reports.
    ``stable_key`` namespaces identities that share the same dotted spelling,
    such as a wildcard pattern and a concrete table.
    """

    kind: IdentityKind
    parts: tuple[str, ...] = ()
    decorator: str = ""
    partial: bool = False
    defaulted: bool = field(default=False, compare=False)
    asset_path: str = ""
    raw: str = ""

    def __post_init__(self) -> None:
        # Accept the former ``NodeIdentity(kind, value)`` constructor while
        # keeping the structured fields used by the identity API.
        if isinstance(self.parts, str):
            if self.kind == "asset":
                object.__setattr__(self, "asset_path", self.parts)
                object.__setattr__(self, "parts", ())
            else:
                object.__setattr__(self, "parts", tuple(self.parts.split(".")))

    @classmethod
    def for_target(cls, project: str, dataset: str, table: str) -> "NodeIdentity":
        parts = tuple(part for part in (project, dataset, table) if part)
        return cls("table", parts, partial=len(parts) < 3)

    @classmethod
    def for_asset(cls, path: str) -> "NodeIdentity":
        normalized = PurePosixPath(posix_path(path)).as_posix()
        return cls("asset", asset_path=normalized)

    @classmethod
    def table(cls, target: object) -> "NodeIdentity":
        """Compatibility constructor for a pipeline target."""

        return cls.for_target(
            str(getattr(target, "database", "")),
            str(getattr(target, "schema", "")),
            str(getattr(target, "name", "")),
        )

    @classmethod
    def asset(cls, path: str) -> "NodeIdentity":
        """Compatibility alias for :meth:`for_asset`."""

        return cls.for_asset(path)

    @classmethod
    def unresolved(cls, raw: str) -> "NodeIdentity":
        return cls("unresolved", raw=raw)

    @property
    def key(self) -> str:
        if self.kind == "asset":
            return f"asset:{self.asset_path}"
        if self.kind == "unresolved":
            return f"unresolved:{self.raw}"
        name = ".".join(self.parts)
        if self.decorator and self.parts:
            name = f"{name}{self.decorator}"
        return name

    @property
    def stable_key(self) -> str:
        if self.kind in {"asset", "unresolved"}:
            return self.key
        return f"{self.kind}:{self.key}"

    @property
    def value(self) -> str:
        """Legacy value spelling, with structured table parts joined."""

        if self.kind == "asset":
            return self.asset_path
        if self.kind == "unresolved":
            return self.raw
        return ".".join(self.parts)

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.stable_key,
            "key": self.key,
            "kind": self.kind,
            "parts": list(self.parts),
            "partial": self.partial,
        }
        if self.decorator:
            result["decorator"] = self.decorator
        if self.defaulted:
            result["defaulted"] = True
        if self.kind == "asset":
            result["asset_path"] = self.asset_path
        if self.kind == "unresolved":
            result["raw"] = self.raw
        return result


@dataclass(frozen=True)
class IdentityResolution:
    """Result of reconciling one observed reference with known graph nodes."""

    reference: str
    identity: NodeIdentity
    status: Literal["exact", "via_default", "ambiguous", "unmatched", "pattern", "system"]
    candidates: tuple[NodeIdentity, ...] = ()
    node_kind: str = "external"
    decorator: str = ""

    @property
    def matched(self) -> bool:
        return self.status in {"exact", "via_default"}

    @property
    def diagnostic_code(self) -> str | None:
        return {
            "ambiguous": "ambiguous_reference",
            "unmatched": "unmatched_reference",
            "pattern": "wildcard_reference",
            "system": "system_reference",
        }.get(self.status)

    @property
    def diagnostic(self) -> object | None:
        """Return the pipeline diagnostic shape used by earlier callers."""

        code = self.diagnostic_code
        if code is None:
            return None
        from .pipeline import PipelineDiagnostic

        messages = {
            "ambiguous_reference": "Observed reference matches multiple known nodes; it was retained unresolved.",
            "unmatched_reference": "Observed reference did not match a known node; it was retained unresolved.",
            "wildcard_reference": "Observed wildcard reference remains an unresolved pattern.",
            "system_reference": "Observed system reference is retained separately from model tables.",
        }
        return PipelineDiagnostic(self.reference, code, messages[code])

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "reference": self.reference,
            "identity": self.identity.to_json(),
            "status": self.status,
            "node_kind": self.node_kind,
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "diagnostic": self.diagnostic_code,
        }
        if self.decorator:
            result["decorator"] = self.decorator
        return result


def _reference_parts(reference: object) -> tuple[str, ...] | None:
    if isinstance(reference, NodeIdentity):
        return reference.parts if reference.kind != "asset" else None

    if isinstance(reference, Mapping):
        nested = reference.get("tableReference")
        if isinstance(nested, Mapping):
            reference = nested
        if all(key in reference for key in ("projectId", "datasetId", "tableId")):
            return tuple(
                str(reference.get(key, ""))
                for key in ("projectId", "datasetId", "tableId")
                if reference.get(key)
            )
        # INFORMATION_SCHEMA.JOBS exports spell the API's fields in snake case.
        if all(key in reference for key in ("project_id", "dataset_id", "table_id")):
            return tuple(
                str(reference.get(key, ""))
                for key in ("project_id", "dataset_id", "table_id")
                if reference.get(key)
            )
        if all(key in reference for key in ("database", "schema", "name")):
            return tuple(str(reference.get(key, "")) for key in ("database", "schema", "name") if reference.get(key))
        return None

    if all(hasattr(reference, key) for key in ("database", "schema", "name")):
        return tuple(
            str(getattr(reference, key))
            for key in ("database", "schema", "name")
            if getattr(reference, key)
        )

    parts_attr = getattr(reference, "parts", None)
    if parts_attr is not None:
        parts = tuple(str(part.name) for part in parts_attr if getattr(part, "name", ""))
        return parts or None

    if not isinstance(reference, str):
        return None
    text = reference.strip().strip(";")
    if not text:
        return None
    if text.startswith("`") and text.endswith("`"):
        text = text[1:-1]
    parts = [part.strip().strip("`") for part in text.split(".")]
    if len(parts) == 2 and ":" in parts[0]:
        project, dataset = parts[0].split(":", 1)
        parts = [project, dataset, parts[1]]
    return tuple(part for part in parts if part) or None


def normalize_table_reference(
    reference: object,
    *,
    default_project: str = "",
    default_dataset: str = "",
) -> NodeIdentity | None:
    """Normalize a SQL/API table reference without folding identifier case.

    One- and two-part references use defaults when supplied. Missing parts
    remain partial identities. Partition and snapshot decorators are retained
    separately, while wildcards and ``INFORMATION_SCHEMA`` stay distinct node
    kinds so they cannot accidentally match concrete tables.
    """

    if isinstance(reference, NodeIdentity):
        return reference
    parts = _reference_parts(reference)
    if not parts:
        return None

    defaulted = False
    if len(parts) == 1 and default_dataset:
        parts = (default_project, default_dataset, parts[0]) if default_project else (default_dataset, parts[0])
        defaulted = True
    elif len(parts) == 2 and default_project:
        parts = (default_project, parts[0], parts[1])
        defaulted = True

    values = list(parts)
    decorator = ""
    if values:
        for marker in ("$", "@"):
            position = values[-1].find(marker)
            if position > 0:
                decorator = values[-1][position:]
                values[-1] = values[-1][:position]
                break

    if any(part.upper() == "INFORMATION_SCHEMA" for part in values):
        kind: IdentityKind = "system"
    elif any("*" in part for part in values):
        kind = "wildcard"
    else:
        kind = "table"
    return NodeIdentity(
        kind=kind,
        parts=tuple(values),
        decorator=decorator,
        partial=len(values) < 3,
        defaulted=defaulted,
    )


__all__ = ["IdentityResolution", "NodeIdentity", "normalize_table_reference"]
