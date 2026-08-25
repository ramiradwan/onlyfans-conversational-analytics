"""Behavioural falsifiers for clean-machine packaging smoke detection."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import exclusive_resource
import inno_setup_compiler
import visible_windows


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="drives a real Windows installer via pwsh.exe"
)

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "tools" / "packaging-smoke" / "run.ps1"
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"


@pytest.fixture(autouse=True)
def _provisioning_resources() -> Iterator[None]:
    """Serialize every smoke run against other suite runs on this machine.

    Each run drives the fixed provisioning port through `run.ps1`, so runs that
    overlap read one another's listeners as a port-preflight abort.
    """

    if os.name != "nt":
        yield
        return
    with exclusive_resource.exclusive_provisioning_resources():
        yield


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


class _HealthServerProcess:
    """A one-response process-owned impostor, isolated from the test runner.

    The ownership mutation must prove only that the harness accepted an external
    listener.  Once that listener serves the mutated health request it exits on
    its own, so the mutation does not also depend on the harness killing an
    unrelated process quickly enough for the cleanup deadline.
    """

    def __enter__(self) -> "_HealthServerProcess":
        program = """
import http.server

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_error(404)
            return
        payload = b'{\\"status\\":\\"ok\\"}'
        # The request socket is already accepted at this point. Closing the
        # listening socket here ensures that by the time the client can observe
        # a successful response, port 17871 is no longer listening.
        self.server.server_close()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self.server.health_served = True
    def log_message(self, format, *args):
        return

server = http.server.HTTPServer(('127.0.0.1', 17871), Handler)
server.timeout = 0.1
server.health_served = False
try:
    while not server.health_served:
        server.handle_request()
finally:
    server.server_close()
"""
        self.process = subprocess.Popen(
            [sys.executable, "-c", program],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if self.process.poll() is not None:
                raise AssertionError("impostor listener failed to start")
            try:
                with __import__("socket").create_connection(("127.0.0.1", 17871), 0.1):
                    return self
            except OSError:
                pass
        self.process.terminate()
        self.process.wait(timeout=5)
        raise AssertionError("impostor listener did not acquire port 17871")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)


def _run_smoke(
    tmp_path: Path,
    *,
    smoke_script: Path = SMOKE_SCRIPT,
    shell: str = "pwsh.exe",
    artifact_path: Path | None = None,
    executable_name: str | None = None,
    executable_contents: bytes | None = None,
    repository_marker: bytes | None = None,
    inspection_root: Path | None = None,
    use_script_default: bool = False,
    cwd: Path = ROOT,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    search_path = tmp_path / "search-path"
    search_path.mkdir()
    if executable_name is not None:
        candidate = search_path / executable_name
        candidate.write_bytes(executable_contents or b"")

    inspection_fixture_root = tmp_path / "inspection-root"
    inspection_fixture_root.mkdir()
    if repository_marker is not None:
        (inspection_fixture_root / ".git").write_bytes(repository_marker)
    if not use_script_default:
        inspection_root = inspection_fixture_root

    artifact = artifact_path or (tmp_path / "artifact.exe")
    if artifact_path is None:
        artifact.write_bytes(b"fixture-artifact")
    transcript_path = tmp_path / "transcript.json"

    command = [
        shell,
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
    ]
    if inspection_root is not None:
        command.extend(
            [
                "-InspectionRoot",
                str(inspection_root),
            ]
        )
    command.extend(
        [
            "-ExecutableSearchPath",
            str(search_path),
        ]
    )
    result = subprocess.run(
        command,
        cwd=cwd,
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


def _web_commands_without_basic_parsing(root: Path) -> list[str]:
    """Return web cmdlet lines that omit the Windows PowerShell compatibility switch."""

    findings: list[str] = []
    for script in (root / "tools").rglob("*.ps1"):
        lines = script.read_text(encoding="utf-8-sig").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not re.search(r"\bInvoke-(?:WebRequest|RestMethod)\b", line):
                continue
            if "-UseBasicParsing" not in line:
                findings.append(f"{script.relative_to(root).as_posix()}:{line_number}")
    return findings


def _write_listener_executable(tmp_path: Path) -> Path:
    """Compile a launcher fixture whose child owns the fixed local listener."""

    compiler = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    assert compiler.is_file(), "the Windows C# compiler is required for this fixture"
    source = tmp_path / "listener_launcher.cs"
    executable = tmp_path / "Brain.exe"
    source.write_text(
        r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;

public static class ListenerLauncher {
    public static void Main(string[] arguments) {
        if (arguments.Length == 1 && arguments[0] == "--brain") {
            Serve();
            return;
        }
        string image = Process.GetCurrentProcess().MainModule.FileName;
        Process child = Process.Start(new ProcessStartInfo(image, "--brain") {
            CreateNoWindow = true,
            UseShellExecute = false,
        });
        child.WaitForExit();
    }

    private static void Serve() {
        TcpListener listener = new TcpListener(IPAddress.Loopback, 17871);
        listener.Start();
        while (true) {
            using (TcpClient client = listener.AcceptTcpClient())
            using (NetworkStream stream = client.GetStream()) {
                byte[] request = new byte[4096];
                stream.Read(request, 0, request.Length);
                byte[] response = Encoding.ASCII.GetBytes(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"status\":\"ok\"}"
                );
                stream.Write(response, 0, response.Length);
                stream.Flush();
            }
        }
    }
}
'''.strip()
        + "\n",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        [str(compiler), "/nologo", "/target:exe", f"/out:{executable}", str(source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return executable


def _write_pyinstaller_standin(
    tmp_path: Path, *, listener_executable: Path | None = None
) -> Path:
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
shutil.copyfile(Path({str(listener_executable)!r}) if {listener_executable is not None!r} else Path(os.environ['ComSpec']), stage / 'Brain.exe')
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


def _build_real_installer(tmp_path: Path, *, serves_health: bool = False) -> Path:
    """Use the production build script and real Inno compiler, not an installer stand-in."""

    compiler = inno_setup_compiler.require_inno_setup_compiler(
        "Inno Setup is required for packaging-smoke installer falsifiers"
    )
    listener_executable = _write_listener_executable(tmp_path) if serves_health else None
    pyinstaller = _write_pyinstaller_standin(
        tmp_path, listener_executable=listener_executable
    )
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


# Every run installs beneath a run root carrying this prefix, so a window the
# installer or the launcher opens names it in the title or in the owning image.
_SMOKE_RUN_TOKEN = "ofca-packaging-smoke-"


def _opened_by_a_smoke_run(window: visible_windows.DesktopWindow) -> bool:
    """Attribute a window to a smoke run's installer or launcher."""

    return visible_windows.is_inno_setup_image(window.process_image) or window.mentions(
        _SMOKE_RUN_TOKEN
    )


def _remove_fixture_tree(path: Path, *, attempts: int = 10) -> bool:
    """Delete a fixture tree, tolerating transient Windows sharing violations."""

    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        if attempt < attempts - 1:
            time.sleep(0.2)
    return False


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


def _assert_port_preflight_abort(
    result: subprocess.CompletedProcess[str], transcript: dict
) -> None:
    assert result.returncode == 24, result.stdout + result.stderr
    assert transcript["artifact"] == {
        "status": "aborted",
        "reason": "provisioning_listener_port_occupied",
    }
    preflight = _step(transcript, "provisioning-listener-port-preflight")
    assert preflight["outcome"] == "abort"
    assert preflight["evidence"]["finding"] == "provisioning_listener_port_occupied"


def _without_port_preflight(script: str) -> str:
    route = "Assert-CleanEnvironment\nAssert-ArtifactDigest\nAssert-ProvisioningPortAvailable"
    assert route in script
    return script.replace(route, "Assert-CleanEnvironment\nAssert-ArtifactDigest", 1)


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


def test_windows_powershell_51_runs_the_smoke_harness(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 remains a supported harness interpreter."""

    if shutil.which("powershell.exe") is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")

    result, transcript = _run_smoke(
        tmp_path,
        shell="powershell.exe",
        executable_name="python.exe",
        executable_contents=b"real-interpreter-fixture",
    )

    assert result.returncode == 21, result.stdout + result.stderr
    assert transcript["artifact"] == {"status": "aborted", "reason": "python_detected"}


def test_tools_web_requests_use_windows_powershell_compatibility() -> None:
    """Every tools web request must avoid the Windows PowerShell IE engine."""

    assert _web_commands_without_basic_parsing(ROOT) == []


def test_tools_web_request_guard_detects_a_removed_compatibility_switch(
    tmp_path: Path,
) -> None:
    """The guard turns red when the compatibility switch is removed."""

    script = tmp_path / "tools" / "packaging-smoke" / "run.ps1"
    script.parent.mkdir(parents=True)
    script.write_text(
        SMOKE_SCRIPT.read_text(encoding="utf-8").replace(
            "-UseBasicParsing ", "", 1
        ),
        encoding="utf-8",
    )

    assert _web_commands_without_basic_parsing(tmp_path) == [
        "tools/packaging-smoke/run.ps1:419"
    ]


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


def test_default_inspection_roots_are_absolute_and_not_the_working_directory(
    tmp_path: Path,
) -> None:
    """The default scan finds a checkout at an absolute path, not the cwd."""

    clean_cwd = Path(r"C:\Windows\System32")
    result, transcript = _run_smoke(
        tmp_path, use_script_default=True, cwd=clean_cwd
    )

    assert result.returncode == 23, result.stdout + result.stderr
    clean_environment = _step(transcript, "clean-environment")
    assert clean_environment["outcome"] == "abort"
    repository_path = Path(clean_environment["evidence"]["path"])
    assert repository_path.is_absolute()
    assert repository_path.parent != clean_cwd

    mutated_script = tmp_path / "run-bare-system-drive-default.ps1"
    original = SMOKE_SCRIPT.read_text(encoding="utf-8")
    bounded_default = "    [string[]] $InspectionRoot = @([IO.Path]::GetFullPath((Join-Path -Path $env:SystemDrive -ChildPath '\\'))),"
    bare_drive_default = "    [string[]] $InspectionRoot = @($env:SystemDrive),"
    assert bounded_default in original
    mutated_script.write_text(
        original.replace(bounded_default, bare_drive_default, 1), encoding="utf-8"
    )

    mutated_result, mutated_transcript = _run_smoke(
        tmp_path / "mutated",
        smoke_script=mutated_script,
        use_script_default=True,
        cwd=clean_cwd,
    )

    assert mutated_result.returncode == 32, (
        mutated_result.stdout + mutated_result.stderr
    )
    assert mutated_transcript["artifact"] == {
        "status": "failed",
        "reason": "installation_failed",
    }


@pytest.mark.external_default_scope
def test_default_inspection_scope_detects_a_repository_marker(
    tmp_path: Path,
) -> None:
    """The default scope detects a checkout planted at the system-drive root."""

    # The name carries this run's process id and a random suffix so a marker
    # planted here is attributable and cannot collide with another run's.
    fixture_root = Path(
        tempfile.mkdtemp(prefix=f"!ofca-packaging-smoke-{os.getpid()}-", dir="C:\\")
    )
    marker = fixture_root / ".git"
    try:
        marker.mkdir()
        result, transcript = _run_smoke(
            tmp_path,
            use_script_default=True,
            cwd=Path(r"C:\Windows\System32"),
        )
    finally:
        removed = _remove_fixture_tree(fixture_root)
    assert removed, f"the system-drive fixture {fixture_root} could not be removed"

    assert result.returncode == 23, result.stdout + result.stderr
    clean_environment = _step(transcript, "clean-environment")
    assert clean_environment["outcome"] == "abort"
    assert clean_environment["evidence"]["finding"] == "repository_checkout_present"
    assert Path(clean_environment["evidence"]["path"]) == marker


def test_listener_port_preflight_has_a_distinct_exit_code(tmp_path: Path) -> None:
    """An existing fixed-port listener aborts before any installer is launched."""

    with _HealthServer():
        result, transcript = _run_smoke(tmp_path)
    _assert_port_preflight_abort(result, transcript)

    mutated_script = tmp_path / "run-generic-port-preflight.ps1"
    original = SMOKE_SCRIPT.read_text(encoding="utf-8")
    route = "exit $ExitCode.PortOccupied"
    assert route in original
    mutated_script.write_text(
        original.replace(route, "exit $ExitCode.InvalidInput", 1), encoding="utf-8"
    )
    with _HealthServer():
        mutated_result, mutated_transcript = _run_smoke(
            tmp_path / "mutated", smoke_script=mutated_script
        )

    assert mutated_result.returncode == 2, mutated_result.stdout + mutated_result.stderr
    with pytest.raises(AssertionError):
        _assert_port_preflight_abort(mutated_result, mutated_transcript)


def test_harness_launches_the_executable_its_real_installer_placed(
    tmp_path: Path,
) -> None:
    """A fixed external launcher makes the containment assertion red."""

    installer = _build_real_installer(tmp_path)
    result, transcript = _run_smoke(tmp_path, artifact_path=installer)

    assert result.returncode == 41, result.stdout + result.stderr
    _assert_launcher_was_installed(transcript)
    assert _step(transcript, "provisioning-listener")["outcome"] == "fail"
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

    assert mutated_result.returncode == 41, mutated_result.stdout + mutated_result.stderr
    with pytest.raises(AssertionError, match="was not installed beneath"):
        _assert_launcher_was_installed(mutated_transcript)


def test_the_real_smoke_cycle_shows_no_window(tmp_path: Path) -> None:
    """A default-tier install, launch and uninstall never takes desktop focus.

    Inno Setup displays its progress window under /SILENT, and Start-Process
    gives a console launcher a terminal window of its own.  This test drives a
    real installer and reads back every top-level window the run opened.
    """

    installer = _build_real_installer(tmp_path)
    with visible_windows.recording_windows(_opened_by_a_smoke_run) as observed:
        result, transcript = _run_smoke(tmp_path, artifact_path=installer)

    # The transcript establishes that a real install, launch and uninstall ran,
    # so an empty window recording means silence rather than an absent step.
    assert result.returncode == 41, result.stdout + result.stderr
    assert _step(transcript, "install-artifact")["outcome"] == "pass"
    assert _step(transcript, "open-bridge")["outcome"] == "pass"
    assert _step(transcript, "uninstall-artifact")["outcome"] == "pass"

    displayed = sorted(
        (window.class_name, window.title)
        for window in observed
        if window.steals_focus()
    )
    assert displayed == [], (
        f"the smoke run displayed {displayed}; a default-tier run must not open "
        "a window on the desktop"
    )


def test_unrelated_health_listener_cannot_satisfy_the_launcher_check(
    tmp_path: Path,
) -> None:
    """A 200 from an externally-owned listener is not launcher readiness."""

    installer = _build_real_installer(tmp_path)
    original = SMOKE_SCRIPT.read_text(encoding="utf-8")
    binding_script = tmp_path / "run-without-port-preflight.ps1"
    binding_script.write_text(_without_port_preflight(original), encoding="utf-8")

    with _HealthServerProcess():
        result, transcript = _run_smoke(
            tmp_path / "bound",
            smoke_script=binding_script,
            artifact_path=installer,
        )

    listener = _step(transcript, "provisioning-listener")
    assert result.returncode == 41, result.stdout + result.stderr
    assert listener["outcome"] == "fail"
    assert listener["evidence"]["finding"] == "listener_owned_by_unrelated_process"

    mutated_script = tmp_path / "run-without-listener-binding.ps1"
    derivation = "$listenerOwnedByLauncher = $ownerProcessId -in $ownedProcessIds"
    assert derivation in original
    mutated_script.write_text(
        _without_port_preflight(original).replace(
            derivation, "$listenerOwnedByLauncher = $true", 1
        ),
        encoding="utf-8",
    )
    with _HealthServerProcess():
        mutated_result, mutated_transcript = _run_smoke(
            tmp_path / "unbound",
            smoke_script=mutated_script,
            artifact_path=installer,
        )

    assert mutated_result.returncode == 40, mutated_result.stdout + mutated_result.stderr
    assert _step(mutated_transcript, "provisioning-listener")["outcome"] == "pass"
    assert _step(mutated_transcript, "close-bridge")["outcome"] == "pass"
    assert _step(mutated_transcript, "close-bridge")["evidence"]["port_released"] is True
    with pytest.raises(AssertionError):
        assert _step(mutated_transcript, "provisioning-listener")["outcome"] == "fail"


def test_installed_listener_is_attributed_to_the_launcher_family(tmp_path: Path) -> None:
    """A real installer passes only when its listener is attributed and stopped."""

    installer = _build_real_installer(tmp_path, serves_health=True)
    result, transcript = _run_smoke(tmp_path, artifact_path=installer)

    listener = _step(transcript, "provisioning-listener")
    listener_process_id = listener["evidence"]["listener_process_id"]
    assert result.returncode == 40, result.stdout + result.stderr
    assert listener["outcome"] == "pass"
    assert listener["evidence"]["listener_ownership"] == "launcher_descendant"
    assert listener_process_id in listener["evidence"]["launcher_family_process_ids"]

    close_bridge = _step(transcript, "close-bridge")
    assert close_bridge["outcome"] == "pass"
    assert listener_process_id in close_bridge["evidence"]["stopped_process_ids"]
    assert close_bridge["evidence"]["port_released"] is True

    mutated_script = tmp_path / "run-with-unrelated-attribution.ps1"
    original = SMOKE_SCRIPT.read_text(encoding="utf-8")
    derivation = "$listenerOwnedByLauncher = $ownerProcessId -in $ownedProcessIds"
    assert derivation in original
    mutated_script.write_text(
        original.replace(derivation, "$listenerOwnedByLauncher = $ownerProcessId -eq 0", 1),
        encoding="utf-8",
    )
    mutated_result, mutated_transcript = _run_smoke(
        tmp_path / "unrelated-attribution",
        smoke_script=mutated_script,
        artifact_path=installer,
    )

    assert mutated_result.returncode == 41, mutated_result.stdout + mutated_result.stderr
    assert _step(mutated_transcript, "provisioning-listener")["outcome"] == "fail"
    with pytest.raises(AssertionError):
        assert _step(mutated_transcript, "provisioning-listener")["outcome"] == "pass"


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
