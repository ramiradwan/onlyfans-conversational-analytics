"""Platform-standard locations for mutable local runtime state."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

from app.persistence.private_files import reject_path_aliases


DATA_DIRECTORY_ENVIRONMENT_VARIABLE = "LOCAL_ANALYTICS_DATA_DIR"
RUNTIME_CONFIGURATION_FILENAME = "runtime.env"
_PRODUCT_ROOT = Path(__file__).resolve().parents[2]


def runtime_data_directory(
    override: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    desktop_release=None,
) -> Path:
    """Return a per-user data directory outside the installed application."""

    environment = os.environ if environ is None else environ
    if desktop_release is None and getattr(sys, "frozen", False):
        from app.core.desktop_release import current_desktop_release
        desktop_release = current_desktop_release()
    platform_name = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else home
    if platform_name == "win32":
        base = Path(environment.get("LOCALAPPDATA", user_home / "AppData" / "Local"))
        product = base / "OnlyFans Conversational Analytics"
    elif platform_name == "darwin":
        product = user_home / "Library" / "Application Support" / "OnlyFans Conversational Analytics"
    else:
        base = Path(environment.get("XDG_DATA_HOME", user_home / ".local" / "share"))
        product = base / "onlyfans-conversational-analytics"
    qualification = desktop_release is not None and desktop_release.purpose == "qualification"
    default = product / "qualification" / desktop_release.identity.name if qualification else product
    configured = override or environment.get(DATA_DIRECTORY_ENVIRONMENT_VARIABLE)
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                f"{DATA_DIRECTORY_ENVIRONMENT_VARIABLE} must be an absolute path"
            )
    else:
        candidate = default

    absolute = reject_path_aliases(candidate)
    if os.name == "nt" and str(absolute).startswith("\\\\"):
        raise ValueError("runtime data directory must be on a local filesystem")
    resolved = absolute.resolve()
    if qualification:
        product_absolute = reject_path_aliases(product).resolve()
        own_absolute = reject_path_aliases(default).resolve()
        if resolved == product_absolute or (product_absolute in resolved.parents and
                                             resolved != own_absolute and own_absolute not in resolved.parents):
            raise ValueError("qualification cannot adopt production or another qualification root")
    installed_root = Path(sys.executable).parent if getattr(sys, "frozen", False) else _PRODUCT_ROOT
    if resolved == installed_root or installed_root in resolved.parents:
        raise ValueError("runtime data directory must be outside the installed application")
    return resolved


def runtime_configuration_file(
    data_directory: str | Path | None = None,
) -> Path:
    return runtime_data_directory(data_directory) / RUNTIME_CONFIGURATION_FILENAME
