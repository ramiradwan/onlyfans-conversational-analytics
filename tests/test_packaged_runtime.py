"""Black-box runtime checks for an already-frozen Windows Brain artifact."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest


PACKAGED_ARTIFACT_ENVIRONMENT_VARIABLE = "BRAIN_PACKAGED_ARTIFACT_DIR"
_BRAIN_PORT = 17871
_STARTUP_TIMEOUT_SECONDS = 15
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
