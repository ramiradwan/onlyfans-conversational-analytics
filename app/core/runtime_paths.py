"""Platform-standard locations for mutable local runtime state."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


DATA_DIRECTORY_ENVIRONMENT_VARIABLE = "LOCAL_ANALYTICS_DATA_DIR"
RUNTIME_CONFIGURATION_FILENAME = "runtime.env"
_PRODUCT_ROOT = Path(__file__).resolve().parents[2]


def runtime_data_directory(
    override: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return a per-user data directory outside the installed application."""

    environment = os.environ if environ is None else environ
    configured = override or environment.get(DATA_DIRECTORY_ENVIRONMENT_VARIABLE)
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                f"{DATA_DIRECTORY_ENVIRONMENT_VARIABLE} must be an absolute path"
            )
    else:
        platform_name = sys.platform if platform is None else platform
        user_home = Path.home() if home is None else home
        if platform_name == "win32":
            base = Path(
                environment.get("LOCALAPPDATA", user_home / "AppData" / "Local")
            )
            candidate = base / "OnlyFans Conversational Analytics"
        elif platform_name == "darwin":
            candidate = (
                user_home
                / "Library"
                / "Application Support"
                / "OnlyFans Conversational Analytics"
            )
        else:
            base = Path(environment.get("XDG_DATA_HOME", user_home / ".local" / "share"))
            candidate = base / "onlyfans-conversational-analytics"

    resolved = candidate.resolve()
    if resolved == _PRODUCT_ROOT or _PRODUCT_ROOT in resolved.parents:
        raise ValueError("runtime data directory must be outside the installed application")
    return resolved


def runtime_configuration_file(
    data_directory: str | Path | None = None,
) -> Path:
    return runtime_data_directory(data_directory) / RUNTIME_CONFIGURATION_FILENAME
