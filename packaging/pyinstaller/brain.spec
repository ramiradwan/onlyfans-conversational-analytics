# PyInstaller one-directory build for Brain.
#
# Build through packaging/build-windows.ps1.  The spec intentionally consumes
# packaging/runtime-files.json instead of maintaining a second data allowlist.
# ruff: noqa

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

from PyInstaller.building.api import COLLECT, EXE, PYZ  # type: ignore[import-not-found]
from PyInstaller.building.build_main import Analysis  # type: ignore[import-not-found]
from PyInstaller.utils.hooks import collect_dynamic_libs  # type: ignore[import-not-found]


_PROJECT_ROOT = Path(os.environ.get("BRAIN_PROJECT_ROOT", Path.cwd())).resolve()
_POLICY_PATH = _PROJECT_ROOT / "packaging" / "runtime-files.json"
_POLICY = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
_ENTRY = _PROJECT_ROOT / "app" / "packaged_entry.py"
_INTERNAL_PREFIX = "_internal/"


def _source_relative(staged_relative: str) -> PurePosixPath:
    """Map a declared onedir-internal path back to its checkout source."""

    if not staged_relative.startswith(_INTERNAL_PREFIX):
        raise ValueError(f"not a PyInstaller internal resource: {staged_relative}")
    return PurePosixPath(staged_relative.removeprefix(_INTERNAL_PREFIX))


def _add_file(entries: list[tuple[str, str]], staged_relative: str) -> None:
    source_relative = _source_relative(staged_relative)
    source = _PROJECT_ROOT / source_relative
    if not source.is_file():
        raise FileNotFoundError(f"declared runtime file is absent: {source}")
    entries.append((str(source), str(source_relative.parent)))


def _add_tree(
    entries: list[tuple[str, str]], seen: set[PurePosixPath], staged_relative: str
) -> None:
    source_relative = _source_relative(staged_relative)
    if source_relative in seen:
        return
    source = _PROJECT_ROOT / source_relative
    if not source.is_dir():
        raise FileNotFoundError(f"declared runtime directory is absent: {source}")
    entries.append((str(source), str(source_relative)))
    seen.add(source_relative)


# Data closure is declared by runtime-files.json.  Required directory entries
# are structural assertions; their contents are supplied by the specific
# frontend, SQL, and contract declarations below.  Agent is copied to the
# top-level staging directory by build-windows.ps1 because PyInstaller data is
# always inside _internal in one-dir mode.
_DATAS: list[tuple[str, str]] = []
for _required_file in _POLICY["required_files"]:
    if _required_file.startswith(_INTERNAL_PREFIX):
        _add_file(_DATAS, _required_file)

_SEEN_TREES: set[PurePosixPath] = set()
_add_tree(_DATAS, _SEEN_TREES, _POLICY["frontend"]["dist_path"])
for _catalog in _POLICY["sql_catalogs"]:
    _add_tree(_DATAS, _SEEN_TREES, _catalog["path"])
_add_tree(_DATAS, _SEEN_TREES, _POLICY["contracts"]["path"])

# Dynamic imports selected by Uvicorn and native Rust/OpenSSL extensions are
# not visible from the fixed entry point's static import graph.
_HIDDEN_IMPORTS = [
    "anyio._backends._asyncio",
    "cryptography.hazmat.bindings._rust",
    "pydantic_core",
    "uvicorn.lifespan.off",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

# Keep development and heavyweight analysis tooling out even if a future
# transitive import makes it visible to PyInstaller's module graph.  FastAPI
# and Uvicorn are deliberately absent: Brain requires both at runtime.
_EXCLUDES = [
    "black",
    "coverage",
    "hypothesis",
    "IPython",
    "jupyter",
    "matplotlib",
    "mypy",
    "notebook",
    "pip",
    "pytest",
    "pytest_asyncio",
    "ruff",
    "setuptools",
    "tests",
    "torch",
    "transformers",
    "sentence_transformers",
    "wheel",
]

a = Analysis(
    [str(_ENTRY)],
    pathex=[str(_PROJECT_ROOT)],
    binaries=collect_dynamic_libs("cryptography"),
    datas=_DATAS,
    hiddenimports=_HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Brain",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Brain",
)
