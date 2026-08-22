"""Discovery for the Inno Setup command-line compiler (ISCC.exe).

Mirrors the search order in ``packaging/build-windows.ps1``'s
``Resolve-InnoSetupCompiler``: an explicit environment override, PATH, the
per-user LOCALAPPDATA install location, and the machine-wide Program Files
locations. A hardcoded LOCALAPPDATA-only lookup misses windows-latest GitHub
Actions runners, which ship Inno Setup machine-wide under Program Files
(x86).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


_ENVIRONMENT_VARIABLES = ("INNO_SETUP_COMPILER", "ISCC")
_PROGRAM_FILES_VARIABLES = ("ProgramFiles(x86)", "ProgramFiles")


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    for variable in _ENVIRONMENT_VARIABLES:
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    on_path = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if on_path:
        candidates.append(Path(on_path))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Inno Setup 6" / "ISCC.exe")
    for variable in _PROGRAM_FILES_VARIABLES:
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
    return candidates


def find_inno_setup_compiler() -> Path | None:
    """Return the first discovered Inno Setup compiler, or ``None``."""

    for candidate in _candidate_paths():
        if candidate.is_file():
            return candidate
    return None


def require_inno_setup_compiler(message: str) -> Path:
    """Return the discovered compiler, hard-failing with ``message`` if absent."""

    compiler = find_inno_setup_compiler()
    assert compiler is not None, message
    return compiler
