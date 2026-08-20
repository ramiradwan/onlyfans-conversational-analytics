"""Behavioural falsifiers for clean-machine packaging smoke detection."""

from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "tools" / "packaging-smoke" / "run.ps1"


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
    executable_name: str | None = None,
    executable_contents: bytes | None = None,
    repository_marker: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    search_path = tmp_path / "search-path"
    search_path.mkdir()
    if executable_name is not None:
        candidate = search_path / executable_name
        candidate.write_bytes(executable_contents or b"")

    inspection_root = tmp_path / "inspection-root"
    inspection_root.mkdir()
    if repository_marker is not None:
        (inspection_root / ".git").write_bytes(repository_marker)

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"fixture-artifact")
    launcher = tmp_path / "launcher.cmd"
    launcher.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
    transcript_path = tmp_path / "transcript.json"

    command = [
        "pwsh.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SMOKE_SCRIPT),
        "-ArtifactPath",
        str(artifact),
        "-PublishedSha256",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "-LauncherPath",
        str(launcher),
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

    assert result.returncode == 40, result.stdout + result.stderr
    assert not any(
        step["outcome"] == "abort"
        and step["evidence"].get("finding") == "python_executable_present"
        for step in transcript["steps"]
    )
    assert transcript["artifact"] == {"status": "blocked"}


def test_zero_length_node_stub_is_not_a_toolchain_interpreter(tmp_path: Path) -> None:
    """Node uses the same file-identity rule as Python."""

    result, transcript = _run_smoke(tmp_path, executable_name="node.exe")

    assert result.returncode == 40, result.stdout + result.stderr
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
