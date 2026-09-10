"""Contract tests for the reproducible fixed SQLCipher Windows supply path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import tomllib
from types import ModuleType

import pytest

from app.persistence.sqlcipher_runtime import (
    MINIMUM_SQLCIPHER,
    MINIMUM_SQLITE,
    _cipher_integrity_result,
    _version_tuple,
    qualification_report,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "packaging" / "sqlcipher" / "fixed-runtime-sources.json"


@pytest.fixture
def wheel_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fixed_wheel_builder", SOURCES.with_name("build-fixed-wheel.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_notices_survive_build_cleanup_and_match_provenance(
    wheel_builder: ModuleType, tmp_path: Path
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    with TemporaryDirectory(dir=tmp_path) as directory:
        build = Path(directory)
        files = {
            "binding/LICENSE": "Binding license\n",
            "cipher/LICENSE.md": "SQLCipher license\n",
            "cipher/LICENSE.txt": "SQLCipher license\n",
            "cipher/SQLITE_LICENSE.md": "SQLite notice\n",
            "installed/share/openssl/copyright": "OpenSSL license\n",
        }
        for relative, contents in files.items():
            source = build / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(contents, encoding="utf-8")
        notices = tmp_path / "THIRD_PARTY_NOTICES.md"
        notices.write_text("\n".join(files.values()), encoding="utf-8")
        retained = wheel_builder._retain_runtime_licenses(
            binding=build / "binding", cipher=build / "cipher",
            installed=build / "installed", wheelhouse=wheelhouse,
            version="0.6.2+ofca.1", distribution_notices=notices,
        )
        assert len(list((build / "binding/ofca-licenses").glob("*/*"))) == 4
    assert not build.exists()
    assert len(retained) == 5
    for relative, digest in retained.items():
        assert hashlib.sha256((wheelhouse / relative).read_bytes()).hexdigest() == digest


def test_builder_rejects_distributed_notices_that_omit_native_license(
    wheel_builder: ModuleType, tmp_path: Path
) -> None:
    binding = tmp_path / "binding"
    binding.mkdir()
    (binding / "LICENSE").write_text("new upstream binding notice", encoding="utf-8")
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("old upstream binding notice", encoding="utf-8")
    with pytest.raises(RuntimeError, match="omit binding/LICENSE"):
        wheel_builder._retain_runtime_licenses(
            binding=binding, cipher=tmp_path / "cipher", installed=tmp_path / "installed",
            wheelhouse=tmp_path / "wheelhouse", version="0.6.2+ofca.1",
            distribution_notices=notices,
        )


def test_rewritten_binding_metadata_includes_native_license_files(
    wheel_builder: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "setup.py").write_text("VERSION = '0.6.2'\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.6.2"\n', encoding="utf-8"
    )
    wheel_builder._rewrite_binding_version(tmp_path, "0.6.2+ofca.1")
    project = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["license-files"] == ["LICENSE", "ofca-licenses/*/*"]


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


def test_builder_cleans_its_automatic_work_root_but_keeps_a_debug_root() -> None:
    builder = (ROOT / "packaging" / "sqlcipher" / "build-fixed-wheel.py").read_text(
        encoding="utf-8"
    )
    assert "with tempfile.TemporaryDirectory(prefix=\"ofca-fixed-sqlcipher-\")" in builder
    assert "if arguments.work_root is not None:" in builder
    assert "root.mkdir(parents=True, exist_ok=True)" in builder


def test_cipher_integrity_no_rows_is_success_and_any_row_is_a_failure() -> None:
    assert _cipher_integrity_result([]) == "ok"
    assert _cipher_integrity_result([("HMAC verification failed",)]) == (
        "error: HMAC verification failed"
    )


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
    assert report["encryption"]["cipher_integrity_check"] == "ok"
    assert report["encryption"]["encrypted_readback"] is True
    assert report["encryption"]["stdlib_sqlite_rejected"] is True
    assert report["encryption"]["wrong_key_rejected"] is True
    assert report["qualified"] is True
