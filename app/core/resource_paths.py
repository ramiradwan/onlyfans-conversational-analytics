"""Absolute resource resolution for source and frozen application runtimes."""

from __future__ import annotations

from pathlib import Path
import sys


class ResourcePathError(ValueError):
    """Raised when a resource reference escapes the declared resource root."""


def resource_root() -> Path:
    """Return the absolute root that contains runtime resources."""

    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root is None:
            raise RuntimeError("frozen application has no bundle resource root")
        root = Path(bundle_root).resolve()
    else:
        root = Path(__file__).resolve().parents[2]

    if not root.is_dir():
        raise FileNotFoundError(f"resource root does not exist: {root}")
    return root


def resource_path(reference: str | Path, *, root: Path | None = None) -> Path:
    """Resolve an existing resource inside an active declared resource root."""

    active_root = resource_root()
    declared_root = active_root if root is None else Path(root).resolve()
    try:
        declared_root.relative_to(active_root)
    except ValueError as error:
        raise ResourcePathError(
            f"declared resource root escapes active resource root: {declared_root}"
        ) from error
    if not declared_root.is_dir():
        raise FileNotFoundError(f"resource root does not exist: {declared_root}")

    resolved = (declared_root / reference).resolve()
    try:
        resolved.relative_to(declared_root)
    except ValueError as error:
        raise ResourcePathError(
            f"resource reference escapes resource root: {reference}"
        ) from error
    if not resolved.exists():
        raise FileNotFoundError(f"resource does not exist: {resolved}")
    return resolved
