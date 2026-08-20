"""Behavioural falsifiers for the Windows packaging policy build gate."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.runtime_paths import runtime_data_directory


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"


def _authoritative_version() -> str:
    version = Settings.model_fields["version"].default
    assert isinstance(version, str) and version
    return version


def _write_pyinstaller_standin(tmp_path: Path) -> Path:
    """Provide a valid policy staging tree without installing PyInstaller in .venv."""

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
    source = root / relative
    shutil.copytree(source, stage / '_internal' / relative)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    command = tmp_path / "pyinstaller.cmd"
    command.write_text(
        f'@echo off\r\n"{sys.executable}" "{standin}" %*\r\n', encoding="ascii"
    )
    return command


def _run_build(
    script: Path,
    pyinstaller: Path,
    output: Path,
    *,
    test_injection: str = "DevelopmentConfiguration",
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"BRAIN_PROJECT_ROOT": str(ROOT)}
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-BuildPython",
        sys.executable,
        "-PyInstallerExecutable",
        str(pyinstaller),
        "-OutputRoot",
        str(output),
        "-SkipAssetBuild",
    ]
    if test_injection:
        command.extend(("-TestInjection", test_injection))
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_policy_gate_rejects_development_configuration_and_is_load_bearing(
    tmp_path: Path,
) -> None:
    """The same staged runtime.env is rejected only while the policy call exists."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)

    gated = _run_build(BUILD_SCRIPT, pyinstaller, tmp_path / "gated")

    assert gated.returncode != 0, gated.stdout + gated.stderr
    assert '"code": "forbidden_path_present"' in gated.stdout
    assert "_internal/app/runtime.env" in gated.stdout
    assert not (tmp_path / "gated" / "installer").exists(), (
        "generic-artifact falsifier must stop before an installer is produced"
    )

    without_gate = tmp_path / "build-windows-without-policy.ps1"
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    policy_call = "Invoke-PackagingPolicy -BuildPython $BuildPython -ProjectRoot $ProjectRoot -StagingRoot $stagingRoot"
    assert policy_call in source, "the executable falsifier must remove the real policy invocation"
    without_gate.write_text(source.replace(policy_call, "# policy invocation removed for falsifier"), encoding="utf-8")

    ungated = _run_build(without_gate, pyinstaller, tmp_path / "ungated")

    assert ungated.returncode == 0, ungated.stdout + ungated.stderr
    assert (tmp_path / "ungated" / "dist" / "Brain" / "_internal" / "app" / "runtime.env").is_file()


def _run_installer(installer: Path, prefix: Path, environment: dict[str, str]) -> None:
    result = subprocess.run(
        [str(installer), "/SILENT", f"/DIR={prefix}"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_uninstaller(prefix: Path, environment: dict[str, str]) -> None:
    uninstaller = prefix / "unins000.exe"
    assert uninstaller.is_file(), f"installer did not create an uninstaller: {uninstaller}"
    result = subprocess.run(
        [str(uninstaller), "/SILENT", "/SUPPRESSMSGBOXES"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_user_data_retained(data_file: Path) -> None:
    assert data_file.is_file(), "uninstaller must retain the redirected per-user data file"


def _assert_program_payload_removed(prefix: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        remaining = list(prefix.iterdir()) if prefix.exists() else []
        if not remaining:
            return
        time.sleep(0.05)
    assert not remaining, f"uninstall must leave no program payload behind: {remaining}"


@pytest.mark.skipif(os.name != "nt", reason="Inno Setup installer behavior is Windows-only")
def test_installer_excludes_agent_and_uninstall_retains_redirected_user_data(
    tmp_path: Path,
) -> None:
    """Exercise the generated installer and the red retention mutation end to end."""

    pyinstaller = _write_pyinstaller_standin(tmp_path)
    build_output = tmp_path / "build"
    built = _run_build(
        BUILD_SCRIPT,
        pyinstaller,
        build_output,
        test_injection="",
    )
    assert built.returncode == 0, built.stdout + built.stderr

    version = _authoritative_version()
    installer = (
        build_output
        / "installer"
        / f"OnlyFans-Conversational-Analytics-Setup-{version}-x64.exe"
    )
    assert installer.is_file(), "the passed packaging gate must produce the named installer"

    prefix = tmp_path / "installed"
    data_directory = tmp_path / "runtime-data"
    environment = os.environ | {"LOCAL_ANALYTICS_DATA_DIR": str(data_directory)}
    assert runtime_data_directory(environ=environment) == data_directory.resolve()
    _run_installer(installer, prefix, environment)
    try:
        assert (prefix / "Brain.exe").is_file()
        assert not (prefix / "Agent").exists(), "the installer must not install the Agent"
        data_directory.mkdir()
        retained_file = data_directory / "canonical.sqlite3"
        retained_file.write_bytes(b"authoritative local data")
    finally:
        _run_uninstaller(prefix, environment)
    assert not (prefix / "Brain.exe").exists(), "uninstall must remove program files"
    _assert_program_payload_removed(prefix)
    _assert_user_data_retained(retained_file)

    mutated_script = tmp_path / "brain-removes-data.iss"
    original = (ROOT / "packaging" / "inno" / "brain.iss").read_text(encoding="utf-8")
    deletion = 'Type: filesandordirs; Name: "{app}"'
    mutated = original.replace(
        deletion,
        deletion + f'\nType: filesandordirs; Name: "{data_directory}"',
        1,
    )
    assert mutated != original, "retention falsifier must mutate the actual uninstaller rule"
    mutated_script.write_text(mutated, encoding="utf-8")
    mutated_output = tmp_path / "mutated-installer"
    compiler = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if not compiler.is_file():
        pytest.skip("Inno Setup compiler is unavailable for the retention falsifier")
    compiled = subprocess.run(
        [
            str(compiler),
            f"/DStagingRoot={build_output / 'dist' / 'Brain'}",
            f"/DOutputRoot={mutated_output}",
            f"/DAppVersion={version}",
            str(mutated_script),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    mutated_installer = mutated_output / installer.name
    assert mutated_installer.is_file()

    mutated_prefix = tmp_path / "installed-with-deletion"
    _run_installer(mutated_installer, mutated_prefix, environment)
    data_directory.mkdir(exist_ok=True)
    retained_file.write_bytes(b"authoritative local data")
    try:
        pass
    finally:
        _run_uninstaller(mutated_prefix, environment)
    assert not (mutated_prefix / "Brain.exe").exists()
    _assert_program_payload_removed(mutated_prefix)
    with pytest.raises(AssertionError, match="uninstaller must retain"):
        _assert_user_data_retained(retained_file)
