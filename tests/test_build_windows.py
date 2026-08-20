"""Behavioural falsifiers for the Windows packaging policy build gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"


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


def _run_build(script: Path, pyinstaller: Path, output: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"BRAIN_PROJECT_ROOT": str(ROOT)}
    return subprocess.run(
        [
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
            "-TestInjection",
            "DevelopmentConfiguration",
        ],
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

    without_gate = tmp_path / "build-windows-without-policy.ps1"
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    policy_call = "Invoke-PackagingPolicy -BuildPython $BuildPython -ProjectRoot $ProjectRoot -StagingRoot $stagingRoot"
    assert policy_call in source, "the executable falsifier must remove the real policy invocation"
    without_gate.write_text(source.replace(policy_call, "# policy invocation removed for falsifier"), encoding="utf-8")

    ungated = _run_build(without_gate, pyinstaller, tmp_path / "ungated")

    assert ungated.returncode == 0, ungated.stdout + ungated.stderr
    assert (tmp_path / "ungated" / "dist" / "Brain" / "_internal" / "app" / "runtime.env").is_file()
