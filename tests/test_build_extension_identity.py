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
# Release inputs a Store candidate is never built without. They are taken from
# the tree being built, so a project copy supplies its own checked-in fixtures.
FIXTURE_DIRECTORY = Path("extension") / "tests" / "fixtures"
SIGNING_RULE_FIXTURE = FIXTURE_DIRECTORY / "packaged-signing-rule.json"
LEGAL_BINDINGS_FIXTURE = (
    FIXTURE_DIRECTORY / "legal-instrument-bindings.synthetic.json"
)
SYNTHETIC_PRIVACY_POLICY_URL = "https://legal-evidence.example.com/legal/privacy"


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
for name in (
    'provisioning.html',
    'creator-platform-data-risk-disclosure.html',
    'provisioning.js',
):
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


def _project_copy(tmp_path: Path, name: str, *, manifest_key: str | None = None) -> Path:
    """Copy the project so a build's side effects stay inside the test.

    ``manifest_key`` replaces the packaged Agent's manifest key; omitting it
    leaves the reserved identity the repository ships with.
    """

    project_copy = tmp_path / name
    shutil.copytree(
        ROOT,
        project_copy,
        ignore=shutil.ignore_patterns(
            ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            "__pycache__", "node_modules"
        ),
    )
    # A release build packages the Agent from this copy, so the Agent build
    # dependencies have to come with it. The frontend ones stay excluded.
    shutil.copytree(
        ROOT / "extension" / "node_modules",
        project_copy / "extension" / "node_modules",
    )
    if manifest_key is not None:
        manifest_path = project_copy / "extension" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["key"] = manifest_key
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
        "-PackagedSigningRule", str(project_root / SIGNING_RULE_FIXTURE),
        "-LegalReleaseBindings", str(project_root / LEGAL_BINDINGS_FIXTURE),
        "-PrivacyPolicyUrl", SYNTHETIC_PRIVACY_POLICY_URL,
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


def test_release_refuses_a_manifest_key_the_package_does_not_reserve(
    tmp_path: Path,
) -> None:
    """The packaged identity is derived from the manifest, not restated elsewhere.

    A release is built only against the reserved Store identity. Planting a
    different manifest key is therefore refused, and the refusal names the
    identity derived from the planted key: a build that restated the identity
    instead of deriving it would have accepted the planted key silently.
    """

    changed_key = base64.b64encode(b"different pinned extension key for test").decode(
        "ascii"
    )
    project_copy = _project_copy(tmp_path, "changed-key", manifest_key=changed_key)
    planted_identity = extension_identity_from_manifest(
        project_copy / "extension" / "manifest.json"
    )
    reserved_identity = extension_identity_from_manifest(
        ROOT / "extension" / "manifest.json"
    )
    assert planted_identity != reserved_identity
    pyinstaller = _write_pyinstaller_standin(tmp_path)
    compiler = _write_inno_setup_standin(tmp_path)

    refused = _run_build(
        BUILD_SCRIPT, pyinstaller, compiler, tmp_path / "refused", project_copy
    )

    output = refused.stdout + refused.stderr
    assert refused.returncode != 0, output
    assert planted_identity in output, output
    assert reserved_identity in output, output
    assert not (tmp_path / "refused" / "installer").exists(), (
        "a refused identity must stop before an installer is produced"
    )


def test_build_embeds_the_reserved_manifest_identity_and_rejects_a_stale_value(
    tmp_path: Path,
) -> None:
    """The manifest drives the package value; a stale value blocks the build."""

    project_copy = _project_copy(tmp_path, "reserved-key")
    expected_identity = extension_identity_from_manifest(
        project_copy / "extension" / "manifest.json"
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
