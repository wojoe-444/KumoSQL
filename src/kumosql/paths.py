"""One spelling for the file paths KumoSQL shows: POSIX, relative to the project folder, on every platform.

Reading and writing use real operating-system paths (``pathlib.Path``). Only the text that goes into an answer, a
diagnostic, a diff header or a JSON report passes through :func:`posix_path` or :func:`relative_posix`, so a Windows
checkout reports ``definitions/orders.sqlx`` and not ``definitions\\orders.sqlx`` (which is not a valid path in a
unified diff and does not match a ``folder`` filter written with ``/``).

This module imports nothing from KumoSQL, so any module may use it.
"""

from __future__ import annotations

from os import PathLike
from pathlib import PurePath


def posix_path(value: str | PathLike[str] | None) -> str:
    """``value`` with forward slashes. A path object keeps its parts (``PureWindowsPath`` too); text has ``\\`` read as a separator."""

    if value is None:
        return ""
    if isinstance(value, PurePath):
        return value.as_posix()
    return str(value).replace("\\", "/")


def relative_posix(path: PurePath, root: PurePath) -> str:
    """``path`` below ``root`` as a POSIX path. Raises ``ValueError`` when ``path`` is not inside ``root``, like ``relative_to``."""

    return path.relative_to(root).as_posix()
