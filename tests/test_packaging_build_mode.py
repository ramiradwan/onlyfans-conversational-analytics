"""Exercise the build wrapper's explicit mode and child environment boundary."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat, pytest.mark.serial]

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging/build-windows.ps1"


@pytest.mark.skipif(os.name != "nt", reason="drives build-windows.ps1 via powershell.exe")
@pytest.mark.parametrize("external_executable", [False, True])
@pytest.mark.parametrize("development, ambient", [
    (False, "development"), (True, "release"), (False, None), (True, "invalid"),
])
def test_pyinstaller_child_receives_selected_mode_and_failure_restores_environment(
    tmp_path: Path, development: bool, ambient: str | None, external_executable: bool,
) -> None:
    """Run the wrapper's mode selection and invocation block with a failing child.

    Extracting the actual block exercises both child launch paths without building
    assets. Opposite ambient modes detect inherited downgrades and missing restore.
    """
    source = BUILD_SCRIPT.read_text(encoding="utf-8")
    command = re.search(r"^function Invoke-RequiredCommand \{.*?^\}", source, re.M | re.S)
    selection = re.search(r"^\$ReleaseMode = .*?$", source, re.M)
    assert command is not None and selection is not None
    start = source.index("$previousProjectRoot = ")
    stop = source.index("$stagingRoot = ", start)
    invocation = source[start:stop]
    child_result = tmp_path / "child.json"
    parent_result = tmp_path / "parent.json"
    child = tmp_path / "PyInstaller.py"
    child.write_text(
        "import json, os\nfrom pathlib import Path\n"
        f"Path({str(child_result)!r}).write_text(json.dumps({{"
        "'mode': os.environ.get('BRAIN_BUILD_MODE')}), encoding='utf-8')\n"
        "raise SystemExit(17)\n",
        encoding="utf-8",
    )
    executable = tmp_path / "pyinstaller.cmd"
    executable.write_text(
        f'@echo off\r\n"{sys.executable}" "{child}" %*\r\n', encoding="ascii"
    )

    def literal(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    harness = tmp_path / "invoke.ps1"
    harness.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        f"$DevelopmentAgentBundle = ${str(development).lower()}\n"
        f"$ProjectRoot = {literal(tmp_path)}\n"
        "$packagingSource = [pscustomobject]@{SourceRoot = $ProjectRoot}\n"
        "$distPath = Join-Path $ProjectRoot 'dist'\n"
        "$workPath = Join-Path $ProjectRoot 'work'\n"
        "$SpecPath = Join-Path $ProjectRoot 'brain.spec'\n"
        f"$BuildPython = {literal(sys.executable)}\n"
        f"$PyInstallerExecutable = {literal(executable) if external_executable else '$null'}\n"
        f"{command.group()}\n{selection.group()}\n"
        "$failure = $null\ntry {\n"
        f"{invocation}\n"
        "} catch { $failure = $_.Exception.Message }\n"
        "[pscustomobject]@{\n"
        "    mode = $env:BRAIN_BUILD_MODE\n"
        "    present = Test-Path Env:BRAIN_BUILD_MODE\n"
        "    failure = $failure\n"
        "} | ConvertTo-Json -Compress | Set-Content -Encoding utf8 "
        f"-LiteralPath {literal(parent_result)}\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path)
    if ambient is None:
        environment.pop("BRAIN_BUILD_MODE", None)
    else:
        environment["BRAIN_BUILD_MODE"] = ambient
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(harness)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(child_result.read_text(encoding="utf-8")) == {
        "mode": "development" if development else "release",
    }
    restored = json.loads(parent_result.read_text(encoding="utf-8-sig"))
    assert restored["mode"] == ambient
    assert restored["present"] is (ambient is not None)
    assert "Command failed (17)" in restored["failure"]


