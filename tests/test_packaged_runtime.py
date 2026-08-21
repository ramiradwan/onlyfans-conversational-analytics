"""Black-box runtime checks for an already-frozen Windows Brain artifact."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest


PACKAGED_ARTIFACT_ENVIRONMENT_VARIABLE = "BRAIN_PACKAGED_ARTIFACT_DIR"
INSTALLED_LAUNCHER_E2E_ENVIRONMENT_VARIABLE = "BRAIN_INSTALLED_LAUNCHER_E2E"
PACKAGED_BUILD_PYTHON_ENVIRONMENT_VARIABLE = "BRAIN_PACKAGED_BUILD_PYTHON"
_BRAIN_PORT = 17871
_STARTUP_TIMEOUT_SECONDS = 15
ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "packaging" / "build-windows.ps1"
SMOKE_SCRIPT = ROOT / "tools" / "packaging-smoke" / "run.ps1"
_RUNTIME_CONFIGURATION = """\
ENVIRONMENT="production"
WEBSOCKET_AUTH_MODE="local_session"
WEBSOCKET_BIND_HOST="127.0.0.1"
SECURITY_SIGNING_SECRET="0123456789abcdef0123456789abcdef"
LOCAL_SESSION_BOOTSTRAP_TOKEN="abcdefghijklmnopqrstuvwxyz0123456789abcdef"
LOCAL_PRINCIPAL_ID="principal:packaged-runtime-test"
LOCAL_BRIDGE_ROLE="creator"
IDENTITY_BINDING_SOURCE="verified_grants"
CANONICAL_PERSISTENCE_BACKEND="sqlite"
AUTH_DATABASE_PATH="{data_directory}/auth.sqlite3"
CANONICAL_DATABASE_PATH="{data_directory}/canonical.sqlite3"
PROJECTION_DATABASE_PATH="{data_directory}/projections.sqlite3"
ANALYTICS_PROJECTION_DATABASE_PATH="{data_directory}/analytics-projections.sqlite3"
EXTENSION_ID="abcdefghijklmnopabcdefghijklmnop"
BROADCAST_URL="memory://"
"""


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    body: str


@pytest.fixture
def packaged_artifact() -> Path:
    configured = os.environ.get(PACKAGED_ARTIFACT_ENVIRONMENT_VARIABLE)
    if not configured:
        pytest.skip(
            "packaged runtime artifact is unavailable; set "
            f"{PACKAGED_ARTIFACT_ENVIRONMENT_VARIABLE} to the Brain artifact directory"
        )
    artifact = Path(configured).resolve()
    executable = artifact / "Brain.exe"
    if not executable.is_file():
        pytest.fail(f"{PACKAGED_ARTIFACT_ENVIRONMENT_VARIABLE} has no Brain.exe: {artifact}")
    return artifact


@pytest.fixture
def packaged_build_python() -> Path:
    if os.environ.get(INSTALLED_LAUNCHER_E2E_ENVIRONMENT_VARIABLE) != "1":
        pytest.skip(
            "real installed launcher end-to-end test is disabled; set "
            f"{INSTALLED_LAUNCHER_E2E_ENVIRONMENT_VARIABLE}=1 to build, install, "
            "and exercise the frozen launcher"
        )
    configured = os.environ.get(PACKAGED_BUILD_PYTHON_ENVIRONMENT_VARIABLE)
    if not configured:
        pytest.fail(
            "real installed launcher end-to-end test requires an isolated build "
            f"interpreter in {PACKAGED_BUILD_PYTHON_ENVIRONMENT_VARIABLE}"
        )
    build_python = Path(configured).resolve()
    if not build_python.is_file():
        pytest.fail(
            f"{PACKAGED_BUILD_PYTHON_ENVIRONMENT_VARIABLE} is not a file: "
            f"{build_python}"
        )
    return build_python


def _write_runtime_configuration(data_directory: Path) -> None:
    data_directory.mkdir()
    configuration = _RUNTIME_CONFIGURATION.format(
        data_directory=data_directory.as_posix()
    )
    (data_directory / "runtime.env").write_text(configuration, encoding="utf-8")


def _start_brain(
    artifact: Path, *, data_directory: Path, working_directory: Path
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["LOCAL_ANALYTICS_DATA_DIR"] = str(data_directory)
    return subprocess.Popen(
        [str(artifact / "Brain.exe"), "--brain"],
        cwd=working_directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_brain(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _request(path: str) -> _HttpResponse | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{_BRAIN_PORT}{path}", timeout=1
        ) as response:
            return _HttpResponse(response.status, response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return _HttpResponse(error.code, error.read().decode("utf-8"))
    except urllib.error.URLError:
        return None


def _wait_for_response(
    process: subprocess.Popen[str], path: str, *, description: str
) -> _HttpResponse:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = _request(path)
        if response is not None:
            return response
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"{description}: Brain.exe exited before serving {path} "
                f"(exit {process.returncode}; stdout={stdout!r}; stderr={stderr!r})"
            )
        time.sleep(0.1)
    raise AssertionError(f"{description}: Brain.exe did not serve {path} before timeout")


def _assert_packaged_runtime_serves_homepage(
    artifact: Path, *, data_directory: Path, working_directory: Path
) -> None:
    process = _start_brain(
        artifact, data_directory=data_directory, working_directory=working_directory
    )
    try:
        health = _wait_for_response(process, "/health", description="configured runtime")
        assert health.status == 200, f"configured runtime health response was {health.status}"
        homepage = _wait_for_response(
            process, "/", description="configured runtime resource witness"
        )
        assert homepage.status == 200, (
            "resource_failure: the configured frozen runtime could not render its "
            f"bundled homepage (status {homepage.status})"
        )
        assert "<!doctype html" in homepage.body.lower()
    finally:
        _stop_brain(process)


def _assert_port_is_unused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        assert connection.connect_ex(("127.0.0.1", _BRAIN_PORT)) != 0, (
            f"port {_BRAIN_PORT} is already occupied; packaged runtime test cannot "
            "attribute responses to its Brain.exe process"
        )


def _build_real_installer(
    build_python: Path, output_root: Path, *, project_root: Path = ROOT
) -> Path:
    """Freeze with the release build script and return its real installer."""

    built = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project_root / "packaging" / "build-windows.ps1"),
            "-BuildPython",
            str(build_python),
            "-OutputRoot",
            str(output_root),
        ],
        cwd=project_root,
        env=os.environ | {"BRAIN_PROJECT_ROOT": str(project_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    installers = list((output_root / "installer").glob("*.exe"))
    assert len(installers) == 1, "build-windows.ps1 must emit one installer"
    return installers[0]


def _run_installed_launcher_smoke(
    installer: Path, run_root: Path
) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run the unmodified smoke harness with an isolated installer input."""

    inspection_root = run_root / "inspection-root"
    executable_search_path = run_root / "executable-search-path"
    transcript_path = run_root / "packaging-smoke-transcript.json"
    inspection_root.mkdir(parents=True)
    executable_search_path.mkdir()
    result = subprocess.run(
        [
            "pwsh.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SMOKE_SCRIPT),
            "-ArtifactPath",
            str(installer),
            "-PublishedSha256",
            hashlib.sha256(installer.read_bytes()).hexdigest(),
            "-TranscriptPath",
            str(transcript_path),
            "-InspectionRoot",
            str(inspection_root),
            "-ExecutableSearchPath",
            str(executable_search_path),
        ],
        cwd=run_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert transcript_path.is_file(), result.stdout + result.stderr
    return result, json.loads(transcript_path.read_text(encoding="utf-8-sig"))


def _smoke_step(transcript: dict, name: str) -> dict:
    return next(step for step in transcript["steps"] if step["step"] == name)


def _assert_launcher_listener_ownership(transcript: dict) -> dict:
    """Constrain the listener and its launcher-family ownership independently."""

    listener = _smoke_step(transcript, "provisioning-listener")
    assert listener["outcome"] == "pass", (
        "the frozen launcher must create the provisioning listener"
    )
    assert listener["evidence"]["status_code"] == 200
    assert listener["evidence"]["status"] == "ok"
    assert listener["evidence"]["listener_process_id"] in listener["evidence"][
        "launcher_family_process_ids"
    ]
    assert listener["evidence"]["listener_ownership"] in {
        "launcher",
        "launcher_descendant",
    }
    return listener


def _assert_installed_launcher_listener(
    result: subprocess.CompletedProcess[str], transcript: dict
) -> dict:
    """Constrain the launch, listener ownership, and smoke cleanup outcomes together."""

    installation = _smoke_step(transcript, "install-artifact")
    assert result.returncode == 40, result.stdout + result.stderr
    listener = _assert_launcher_listener_ownership(transcript)
    assert _smoke_step(transcript, "close-bridge")["evidence"]["port_released"] is True
    assert _smoke_step(transcript, "uninstall-artifact")["outcome"] == "pass"
    assert not Path(installation["evidence"]["installation_prefix"]).exists()
    assert not Path(installation["evidence"]["runtime_data_directory"]).exists()
    return listener


def _launcher_source_mode_falsifier_project(tmp_path: Path) -> Path:
    """Copy the product and restore the frozen branch's rejected source command."""

    project_copy = tmp_path / "source-mode-launcher-project"
    shutil.copytree(
        ROOT,
        project_copy,
        ignore=shutil.ignore_patterns(
            ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
            "node_modules"
        ),
    )
    launcher = project_copy / "app" / "launcher.py"
    source = launcher.read_text(encoding="utf-8")
    frozen_branch = '''brain_command = (
        (str(executable), "--brain")
        if getattr(sys, "frozen", False)
        else (str(executable), "-m", "app.packaged_entry", "--brain")
    )'''
    source_branch = '''brain_command = (
        str(executable), "-m", "app.packaged_entry", "--brain"
    )'''
    assert frozen_branch in source, "launcher falsifier must replace the frozen branch"
    launcher.write_text(source.replace(frozen_branch, source_branch, 1), encoding="utf-8")
    return project_copy


@pytest.mark.slow
def test_packaged_runtime_resolves_bundle_resources_from_an_unrelated_cwd(
    packaged_artifact: Path, tmp_path: Path
) -> None:
    """A configured frozen runtime serves its bundle without checkout/CWD access."""

    _assert_port_is_unused()
    data_directory = tmp_path / "runtime-data"
    unrelated_cwd = tmp_path / "unrelated-launch-directory"
    unrelated_cwd.mkdir()
    _write_runtime_configuration(data_directory)

    _assert_packaged_runtime_serves_homepage(
        packaged_artifact,
        data_directory=data_directory,
        working_directory=unrelated_cwd,
    )


@pytest.mark.slow
def test_real_installed_launcher_starts_the_frozen_brain_and_owns_its_listener(
    packaged_build_python: Path, tmp_path: Path
) -> None:
    """A real frozen installer starts Brain through its installed launcher."""

    _assert_port_is_unused()
    started_at = time.monotonic()
    build_output = tmp_path / "frozen-build-output"
    smoke_run_root = tmp_path / "installed-launcher-smoke"
    falsifier_root = Path(
        tempfile.mkdtemp(prefix="ofca-e2e-", dir=Path(tempfile.gettempdir()).anchor)
    )
    falsifier_build_output = falsifier_root / "build"
    falsifier_smoke_run_root = falsifier_root / "smoke"
    try:
        installer = _build_real_installer(packaged_build_python, build_output)
        result, transcript = _run_installed_launcher_smoke(installer, smoke_run_root)
        _assert_installed_launcher_listener(result, transcript)

        falsifier_project = _launcher_source_mode_falsifier_project(falsifier_root)
        falsifier_installer = _build_real_installer(
            packaged_build_python,
            falsifier_build_output,
            project_root=falsifier_project,
        )
        falsifier_result, falsifier_transcript = _run_installed_launcher_smoke(
            falsifier_installer, falsifier_smoke_run_root
        )
        with pytest.raises(
            AssertionError, match="frozen launcher must create the provisioning listener"
        ):
            _assert_launcher_listener_ownership(falsifier_transcript)
    finally:
        shutil.rmtree(build_output, ignore_errors=True)
        shutil.rmtree(smoke_run_root, ignore_errors=True)
        shutil.rmtree(falsifier_build_output, ignore_errors=True)
        shutil.rmtree(falsifier_smoke_run_root, ignore_errors=True)
        shutil.rmtree(falsifier_root, ignore_errors=True)
    _assert_port_is_unused()
    print(
        "real installed launcher end-to-end wall-clock seconds: "
        f"{time.monotonic() - started_at:.1f}"
    )


@pytest.mark.slow
def test_packaged_runtime_missing_template_is_a_resource_failure_not_configuration_refusal(
    packaged_artifact: Path, tmp_path: Path
) -> None:
    """A damaged copy fails after configuration is accepted, not in provisioning mode."""

    _assert_port_is_unused()
    damaged_artifact = tmp_path / "damaged-Brain"
    shutil.copytree(packaged_artifact, damaged_artifact)
    removed_template = damaged_artifact / "_internal" / "app" / "templates" / "index.html"
    removed_template.unlink()

    # No runtime configuration selects the bounded provisioning application.  It
    # deliberately refuses the runtime homepage (404), which is the observable
    # configuration_refusal state used to distinguish the next falsifier.
    no_configuration_cwd = tmp_path / "no-configuration-cwd"
    no_configuration_data_directory = tmp_path / "no-configuration-data"
    no_configuration_cwd.mkdir()
    refusal = _start_brain(
        packaged_artifact,
        data_directory=no_configuration_data_directory,
        working_directory=no_configuration_cwd,
    )
    try:
        refusal_homepage = _wait_for_response(
            refusal, "/", description="configuration_refusal"
        )
        assert refusal_homepage.status == 404, (
            "configuration_refusal: a missing runtime configuration unexpectedly "
            f"served the runtime homepage ({refusal_homepage.status})"
        )
    finally:
        _stop_brain(refusal)

    data_directory = tmp_path / "configured-runtime-data"
    unrelated_cwd = tmp_path / "damaged-unrelated-launch-directory"
    unrelated_cwd.mkdir()
    _write_runtime_configuration(data_directory)

    with pytest.raises(AssertionError, match="resource_failure") as resource_failure:
        _assert_packaged_runtime_serves_homepage(
            damaged_artifact,
            data_directory=data_directory,
            working_directory=unrelated_cwd,
        )

    assert "status 500" in str(resource_failure.value), (
        "resource_failure: removing _internal/app/templates/index.html did not make "
        "the configured frozen runtime fail while rendering the bundled homepage"
    )
