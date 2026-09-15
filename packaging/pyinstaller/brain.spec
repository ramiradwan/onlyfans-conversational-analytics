# PyInstaller one-directory build for Brain.
#
# Build through packaging/build-windows.ps1.  The spec intentionally consumes
# packaging/runtime-files.json instead of maintaining a second data allowlist.
# ruff: noqa

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath

from PyInstaller.building.api import COLLECT, EXE, PYZ  # type: ignore[import-not-found]
from PyInstaller.building.build_main import Analysis  # type: ignore[import-not-found]
from PyInstaller.utils.hooks import collect_dynamic_libs  # type: ignore[import-not-found]


_PROJECT_ROOT = Path(os.environ.get("BRAIN_PROJECT_ROOT", Path.cwd())).resolve()
_SOURCE_ROOT = Path(os.environ.get("BRAIN_SOURCE_ROOT", _PROJECT_ROOT)).resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.core.customer_release import load_customer_release_config


_POLICY_PATH = _PROJECT_ROOT / "packaging" / "runtime-files.json"
_POLICY = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
_ENTRY = _SOURCE_ROOT / "app" / "packaged_entry.py"
_INTERNAL_PREFIX = "_internal/"

# Release-mode detection follows the Agent artifact that packaging/build-windows.ps1
# has already built. A signed/legal Store candidate may never freeze a Brain with
# missing customer hosted routing, while development builds may keep it blank.
_AGENT_METADATA_PATH = _PROJECT_ROOT / "extension" / "dist" / "build-meta.json"
if _AGENT_METADATA_PATH.is_file():
    _agent_metadata = json.loads(_AGENT_METADATA_PATH.read_text(encoding="utf-8"))
    _release_mode = (
        _agent_metadata.get("signing_rule") is not None
        and _agent_metadata.get("legal_bindings") is not None
        and _agent_metadata.get("privacy_policy_configured") is True
    )
else:
    _release_mode = False
load_customer_release_config(
    _PROJECT_ROOT / "app" / "core" / "customer-release.json",
    require_hosted=_release_mode,
)


def _source_relative(staged_relative: str) -> PurePosixPath:
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


_DATAS: list[tuple[str, str]] = []
for _required_file in _POLICY["required_files"]:
    if _required_file.startswith(_INTERNAL_PREFIX):
        _add_file(_DATAS, _required_file)
# Customer routing is nonsecret release configuration owned by runtime
# composition. It is deliberately explicit until runtime-files.json grows a
# digest-bearing declaration for non-contract configuration files.
_add_file(_DATAS, "_internal/app/core/customer-release.json")

_SEEN_TREES: set[PurePosixPath] = set()
_add_tree(_DATAS, _SEEN_TREES, _POLICY["frontend"]["dist_path"])
for _catalog in _POLICY["sql_catalogs"]:
    _add_tree(_DATAS, _SEEN_TREES, _catalog["path"])
_add_tree(_DATAS, _SEEN_TREES, _POLICY["contracts"]["path"])

_HIDDEN_IMPORTS = [
    "anyio._backends._asyncio",
    "cryptography.hazmat.bindings._rust",
    "pydantic_core",
    "ofca_native_snow",
    "sqlcipher3.dbapi2",
    "uvicorn.lifespan.off",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

_EXCLUDES = [
    "black",
    "coverage",
    "hypothesis",
    "IPython",
    "jupyter",
    "matplotlib",
    "maturin",
    "noiseprotocol",
    "noise",
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
    pathex=[str(_SOURCE_ROOT), str(_PROJECT_ROOT)],
    binaries=(collect_dynamic_libs("cryptography") + collect_dynamic_libs("sqlcipher3")),
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
