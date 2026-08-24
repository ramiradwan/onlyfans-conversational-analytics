"""Build-time witnesses for the frozen extension identity."""

from __future__ import annotations

import base64
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.extension_identity import extension_identity_from_manifest


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="drives build-windows.ps1 via powershell.exe"
)

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"


def _write_pyinstaller_standin(tmp_path: Path) -> Path:
    """Provide the release-gate staging shape without freezing in this fast test."""

    standin = tmp_path / "pyinstaller_standin.py"
    standin.write_text(
        f"""
import shutil
import sys
from pathlib import Path

root = Path({str(ROOT)!r})
arguments = sys.argv[1:]
dist = Path(arguments[arguments.index('--distpath') + 1])
stage = dist / 'Brain'
(stage / '_internal').mkdir(parents=True)
(stage / 'Brain.exe').write_bytes(b'frozen-brain')
for relative in (
    'app/templates',
    'app/static/dist',
    'app/persistence/sql',
    'app/persistence/auth_sql',
    'app/persistence/projection_sql',
    'app/analytics/sql',
    'contracts',
):
    shutil.copytree(root / relative, stage / '_internal' / relative)
provisioning = stage / '_internal' / 'app' / 'provisioning'
provisioning.mkdir()
for name in ('provisioning.html', 'provisioning.js'):
    shutil.copyfile(root / 'app' / 'provisioning' / name, provisioning / name)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    command = tmp_path / "pyinstaller.cmd"
    command.write_text(
        f'@echo off\r\n"{sys.executable}" "{standin}" %*\r\n', encoding="ascii"
    )
    return command


def _write_inno_setup_standin(tmp_path: Path) -> Path:
    standin = tmp_path / "inno_setup_standin.py"
    standin.write_text(
        """
import sys
from pathlib import Path

arguments = sys.argv[1:]
output = Path(next(argument.split("=", 1)[1] for argument in arguments if argument.startswith("/DOutputRoot=")))
version = next(argument.split("=", 1)[1] for argument in arguments if argument.startswith("/DAppVersion="))
output.mkdir(parents=True, exist_ok=True)
(output / f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe").write_bytes(b"installer")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    command = tmp_path / "iscc.cmd"
    command.write_text(
        f'@echo off\r\n"{sys.executable}" "{standin}" %*\r\n', encoding="ascii"
    )
    return command


def _project_copy_with_manifest_key(tmp_path: Path, key: str) -> Path:
    project_copy = tmp_path / "project-copy"
    shutil.copytree(
        ROOT,
        project_copy,
        ignore=shutil.ignore_patterns(
            ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "__pycache__", "node_modules"
        ),
    )
    manifest_path = project_copy / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["key"] = key
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return project_copy


def _run_build(
    script: Path,
    pyinstaller: Path,
    compiler: Path,
    output: Path,
    project_root: Path,
    *,
    test_injection: str = "",
) -> subprocess.CompletedProcess[str]:
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(script), "-BuildPython", sys.executable, "-PyInstallerExecutable",
        str(pyinstaller), "-InnoSetupCompiler", str(compiler), "-OutputRoot", str(output),
        "-SkipAssetBuild",
    ]
    if test_injection:
        command.extend(("-TestInjection", test_injection))
    return subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ | {"BRAIN_PROJECT_ROOT": str(project_root)},
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_embedded_identity_mismatch_is_rejected(
    result: subprocess.CompletedProcess[str],
) -> None:
    assert result.returncode != 0, (
        "a build with an embedded extension identity inconsistent with its manifest "
        "must be rejected"
    )
    assert "Embedded extension identity does not match the current manifest key" in (
        result.stdout + result.stderr
    )


def test_build_embeds_the_current_manifest_identity_and_rejects_a_mismatch(
    tmp_path: Path,
) -> None:
    """A changed manifest drives the package value; a stale value blocks the build."""

    changed_key = base64.b64encode(b"different pinned extension key for test").decode(
        "ascii"
    )
    project_copy = _project_copy_with_manifest_key(tmp_path, changed_key)
    manifest_path = project_copy / "extension" / "manifest.json"
    expected_identity = extension_identity_from_manifest(manifest_path)
    assert expected_identity != extension_identity_from_manifest(
        ROOT / "extension" / "manifest.json"
    )
    pyinstaller = _write_pyinstaller_standin(tmp_path)
    compiler = _write_inno_setup_standin(tmp_path)

    accepted = _run_build(
        BUILD_SCRIPT, pyinstaller, compiler, tmp_path / "accepted", project_copy
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    embedded_module = (
        tmp_path / "accepted" / "source" / "app" / "core" / "packaged_extension_identity.py"
    )
    assert runpy.run_path(str(embedded_module))["EXTENSION_ID"] == expected_identity

    rejected = _run_build(
        BUILD_SCRIPT,
        pyinstaller,
        compiler,
        tmp_path / "rejected",
        project_copy,
        test_injection="EmbeddedExtensionIdentityMismatch",
    )
    _assert_embedded_identity_mismatch_is_rejected(rejected)

    invocation = (
        "Assert-EmbeddedExtensionIdentity -BuildPython $BuildPython -ProjectRoot "
        "$ProjectRoot -ManifestPath $packagingSource.ManifestPath -EmbeddedIdentityPath "
        "$packagingSource.EmbeddedIdentityPath"
    )
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert invocation in source
    unchecked_script = tmp_path / "build-windows-unchecked-identity.ps1"
    unchecked_script.write_text(
        source.replace(invocation, "# embedded identity consistency check removed", 1),
        encoding="utf-8",
    )
    unchecked = _run_build(
        unchecked_script,
        pyinstaller,
        compiler,
        tmp_path / "unchecked",
        project_copy,
        test_injection="EmbeddedExtensionIdentityMismatch",
    )
    try:
        _assert_embedded_identity_mismatch_is_rejected(unchecked)
    except AssertionError:
        pass
    else:
        raise AssertionError(
            "identity consistency falsifier must make the rejection assertion red"
        )
    assert unchecked.returncode == 0, unchecked.stdout + unchecked.stderr
