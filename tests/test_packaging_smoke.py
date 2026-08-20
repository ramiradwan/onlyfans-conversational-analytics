"""Behavioural falsifiers for clean-machine packaging smoke detection."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "tools" / "packaging-smoke" / "run.ps1"
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/health":
            self.send_error(404)
            return
        payload = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class _HealthServer:
    def __enter__(self) -> "_HealthServer":
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 17871), _HealthHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _run_smoke(
    tmp_path: Path,
    *,
    smoke_script: Path = SMOKE_SCRIPT,
    artifact_path: Path | None = None,
    executable_name: str | None = None,
    executable_contents: bytes | None = None,
    repository_marker: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    search_path = tmp_path / "search-path"
    search_path.mkdir()
    if executable_name is not None:
        candidate = search_path / executable_name
        candidate.write_bytes(executable_contents or b"")

    inspection_root = tmp_path / "inspection-root"
    inspection_root.mkdir()
    if repository_marker is not None:
        (inspection_root / ".git").write_bytes(repository_marker)

    artifact = artifact_path or (tmp_path / "artifact.exe")
    if artifact_path is None:
        artifact.write_bytes(b"fixture-artifact")
    transcript_path = tmp_path / "transcript.json"

    command = [
        "pwsh.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(smoke_script),
        "-ArtifactPath",
        str(artifact),
        "-PublishedSha256",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "-TranscriptPath",
        str(transcript_path),
        "-InspectionRoot",
        str(inspection_root),
        "-ExecutableSearchPath",
        str(search_path),
    ]
    with _HealthServer():
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    if not transcript_path.is_file():
        raise AssertionError(
            f"smoke script produced no transcript (exit {result.returncode}): "
            f"{result.stdout}{result.stderr}"
        )
    return result, json.loads(transcript_path.read_text(encoding="utf-8-sig"))


def _write_pyinstaller_standin(tmp_path: Path) -> Path:
    """Stage a runnable Windows executable for a real Inno Setup invocation."""

    standin = tmp_path / "pyinstaller_standin.py"
    standin.write_text(
        f"""
import os
import shutil
import sys
from pathlib import Path

root = Path({str(ROOT)!r})
arguments = sys.argv[1:]
dist = Path(arguments[arguments.index('--distpath') + 1])
stage = dist / 'Brain'
(stage / '_internal').mkdir(parents=True)
shutil.copyfile(Path(os.environ['ComSpec']), stage / 'Brain.exe')
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
""".strip()
        + "\n",
        encoding="utf-8",
    )
    command = tmp_path / "pyinstaller.cmd"
    command.write_text(
        f'@echo off\r\n"{sys.executable}" "{standin}" %*\r\n', encoding="ascii"
    )
    return command


def _build_real_installer(tmp_path: Path) -> Path:
    """Use the production build script and real Inno compiler, not an installer stand-in."""

    compiler = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"
    assert compiler.is_file(), "Inno Setup is required for packaging-smoke installer falsifiers"
    pyinstaller = _write_pyinstaller_standin(tmp_path)
    output = tmp_path / "build-output"
    built = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-BuildPython",
            sys.executable,
            "-PyInstallerExecutable",
            str(pyinstaller),
            "-OutputRoot",
            str(output),
            "-SkipAssetBuild",
            "-InnoSetupCompiler",
            str(compiler),
        ],
        cwd=ROOT,
        env=os.environ | {"BRAIN_PROJECT_ROOT": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    installers = list((output / "installer").glob("*.exe"))
    assert len(installers) == 1, "build-windows.ps1 must emit one installer"
    return installers[0]


def _step(transcript: dict, name: str) -> dict:
    return next(step for step in transcript["steps"] if step["step"] == name)


def _assert_launcher_was_installed(transcript: dict) -> None:
    installation = _step(transcript, "install-artifact")
    opened = _step(transcript, "open-bridge")
    prefix = Path(installation["evidence"]["installation_prefix"])
    launcher = Path(opened["evidence"]["launcher_path"])
    assert installation["outcome"] == "pass"
    assert installation["evidence"]["launcher_exists"] is True
    assert launcher.is_relative_to(prefix), (
        f"launcher {launcher} was not installed beneath harness prefix {prefix}"
    )


def _assert_install_failure_exit(result: subprocess.CompletedProcess[str], transcript: dict) -> None:
    assert result.returncode == 32, result.stdout + result.stderr
    assert transcript["artifact"] == {"status": "failed", "reason": "installation_failed"}
    assert _step(transcript, "artifact-digest")["outcome"] == "pass"
    assert _step(transcript, "install-artifact")["evidence"]["finding"] == "installation_failed"


def test_real_interpreter_is_still_detected(tmp_path: Path) -> None:
    """A non-empty normal python.exe remains a toolchain finding."""

    result, transcript = _run_smoke(
        tmp_path,
        executable_name="python.exe",
        executable_contents=b"real-interpreter-fixture",
    )

    assert result.returncode == 21, result.stdout + result.stderr
    assert transcript["artifact"] == {"status": "aborted", "reason": "python_detected"}
    assert transcript["steps"][0]["outcome"] == "abort"
    assert transcript["steps"][0]["evidence"]["path"].endswith("python.exe")


def test_zero_length_python_stub_is_not_an_interpreter(tmp_path: Path) -> None:
    """A zero-length alias-shaped stub passes the clean-environment gate."""

    result, transcript = _run_smoke(tmp_path, executable_name="python.exe")

    assert result.returncode == 32, result.stdout + result.stderr
    assert not any(
        step["outcome"] == "abort"
        and step["evidence"].get("finding") == "python_executable_present"
        for step in transcript["steps"]
    )
    assert transcript["artifact"] == {"status": "failed", "reason": "installation_failed"}


def test_zero_length_node_stub_is_not_a_toolchain_interpreter(tmp_path: Path) -> None:
    """Node uses the same file-identity rule as Python."""

    result, transcript = _run_smoke(tmp_path, executable_name="node.exe")

    assert result.returncode == 32, result.stdout + result.stderr
    assert not any(
        step["outcome"] == "abort"
        and step["evidence"].get("finding") == "node_executable_present"
        for step in transcript["steps"]
    )


def test_repository_marker_file_remains_detected(tmp_path: Path) -> None:
    """A real non-empty .git file still identifies a repository checkout."""

    result, transcript = _run_smoke(
        tmp_path,
        repository_marker=b"gitdir: C:/worktree/.git\n",
    )

    assert result.returncode == 23, result.stdout + result.stderr
    assert transcript["artifact"] == {
        "status": "aborted",
        "reason": "repository_detected",
    }


def test_harness_launches_the_executable_its_real_installer_placed(
    tmp_path: Path,
) -> None:
    """A fixed external launcher makes the containment assertion red."""

    installer = _build_real_installer(tmp_path)
    result, transcript = _run_smoke(tmp_path, artifact_path=installer)

    assert result.returncode == 40, result.stdout + result.stderr
    _assert_launcher_was_installed(transcript)
    installation = _step(transcript, "install-artifact")
    assert not Path(installation["evidence"]["installation_prefix"]).parent.exists()

    mutated_script = tmp_path / "run-fixed-external-launcher.ps1"
    original = SMOKE_SCRIPT.read_text(encoding="utf-8")
    derivation = "$launcherPath = Join-Path -Path $Layout.InstallationPrefix -ChildPath 'Brain.exe'"
    mutation = "$launcherPath = [Environment]::GetEnvironmentVariable('ComSpec')"
    assert derivation in original
    mutated_script.write_text(original.replace(derivation, mutation, 1), encoding="utf-8")

    mutated_result, mutated_transcript = _run_smoke(
        tmp_path / "mutated", smoke_script=mutated_script, artifact_path=installer
    )

    assert mutated_result.returncode == 40, mutated_result.stdout + mutated_result.stderr
    with pytest.raises(AssertionError, match="was not installed beneath"):
        _assert_launcher_was_installed(mutated_transcript)


def test_install_failure_has_a_distinct_exit_code(tmp_path: Path) -> None:
    """Routing an actual post-digest install failure to generic failure turns red."""

    broken_installer = tmp_path / "not-an-installer.exe"
    broken_installer.write_bytes(b"published but not executable installer")
    result, transcript = _run_smoke(tmp_path, artifact_path=broken_installer)

    _assert_install_failure_exit(result, transcript)

    mutated_script = tmp_path / "run-generic-install-failure.ps1"
    original = SMOKE_SCRIPT.read_text(encoding="utf-8")
    route = "exit $ExitCode.InstallationFailed"
    assert route in original
    mutated_script.write_text(
        original.replace(route, "exit $ExitCode.AcceptanceFailed", 1), encoding="utf-8"
    )
    mutated_result, mutated_transcript = _run_smoke(
        tmp_path / "mutated", smoke_script=mutated_script, artifact_path=broken_installer
    )

    assert mutated_result.returncode == 41, mutated_result.stdout + mutated_result.stderr
    with pytest.raises(AssertionError):
        _assert_install_failure_exit(mutated_result, mutated_transcript)
