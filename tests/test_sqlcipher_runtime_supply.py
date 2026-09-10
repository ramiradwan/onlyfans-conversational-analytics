"""Contract tests for the reproducible fixed SQLCipher Windows supply path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.persistence.sqlcipher_runtime import (
    MINIMUM_SQLCIPHER,
    MINIMUM_SQLITE,
    _version_tuple,
    qualification_report,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "packaging" / "sqlcipher" / "fixed-runtime-sources.json"


def test_fixed_windows_runtime_sources_are_exact_and_static_md() -> None:
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    assert sources["binding"]["local_version"] == "0.6.2+ofca.1"
    assert len(sources["binding"]["sha256"]) == 64
    assert sources["sqlcipher"]["version"] == "4.17.0"
    assert len(sources["sqlcipher"]["commit"]) == 40
    assert len(sources["sqlcipher"]["sha256"]) == 64
    assert sources["vcpkg"]["triplet"] == "x64-windows-static-md"
    assert len(sources["vcpkg"]["commit"]) == 40
    assert len(sources["vcpkg"]["sha256"]) == 64


def test_windows_requirement_requires_the_local_fixed_wheel() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    readme = (ROOT / "packaging" / "sqlcipher" / "README.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'sqlcipher3==0.6.2+ofca.1 ; platform_system == "Windows"' in requirements
    assert "build-fixed-wheel.ps1" in readme
    assert "build-fixed-wheel.ps1" in workflow
    assert "--find-links" in workflow


def test_provenance_builder_records_the_resolved_windows_toolchain() -> None:
    builder = (ROOT / "packaging" / "sqlcipher" / "build-fixed-wheel.py").read_text(
        encoding="utf-8"
    )
    for field in (
        "target_python_version",
        "builder_python_version",
        "msvc_compiler_version",
        "vcpkg_openssl",
        "vcpkg.spdx.json",
    ):
        assert field in builder


@pytest.mark.parametrize(
    ("text", "expected"),
    [("3.51.3", (3, 51, 3)), ("4.17.0 community", (4, 17, 0)), ("4.14", (4, 14, 0))],
)
def test_runtime_version_parser_handles_driver_and_cipher_versions(text: str, expected: tuple[int, int, int]) -> None:
    assert _version_tuple(text) == expected


@pytest.mark.windows_production
def test_active_windows_driver_is_fixed_and_fails_closed() -> None:
    report = qualification_report()
    assert _version_tuple(str(report["sqlite_version"])) >= MINIMUM_SQLITE
    assert _version_tuple(str(report["sqlcipher_version"])) >= MINIMUM_SQLCIPHER
    assert report["encryption"]["cipher_integrity_check"] in {"ok", "unsupported"}
    assert report["encryption"]["encrypted_readback"] is True
    assert report["encryption"]["stdlib_sqlite_rejected"] is True
    assert report["encryption"]["wrong_key_rejected"] is True
    assert report["qualified"] is True
