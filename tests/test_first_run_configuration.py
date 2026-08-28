from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from app.core.config import Settings
from app.core.first_run import (
    FirstRunConflictError,
    VerifiedGrantBindings,
    initialize_production_configuration,
)
from app.core.runtime_paths import runtime_data_directory


EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
BINDINGS = VerifiedGrantBindings(
    principal_id="principal:verified-customer-1",
    bridge_role="creator",
)
# Declared here independently of the initializer so that removing a written key
# without updating this set fails `test_first_run_writes_exactly_the_declared_keys`.
_EXPECTED_CONFIGURATION_KEYS = {
    "ENVIRONMENT",
    "WEBSOCKET_AUTH_MODE",
    "WEBSOCKET_BIND_HOST",
    "SECURITY_SIGNING_SECRET",
    "LOCAL_SESSION_BOOTSTRAP_TOKEN",
    "LOCAL_PRINCIPAL_ID",
    "LOCAL_BRIDGE_ROLE",
    "IDENTITY_BINDING_SOURCE",
    "CANONICAL_PERSISTENCE_BACKEND",
    "AUTH_DATABASE_PATH",
    "CANONICAL_DATABASE_PATH",
    "PROJECTION_DATABASE_PATH",
    "ANALYTICS_PROJECTION_DATABASE_PATH",
    "EXTENSION_ID",
    "BROADCAST_URL",
}
# Names that must never reach installation configuration or Settings.
_ACCOUNT_ENVIRONMENT_NAMES = {
    "LOCAL_CREATOR_ACCOUNT_ID",
    "LOCAL_PLATFORM_CREATOR_ID",
    "VERIFIED_GRANT_BUNDLE_SHA256",
}
_SETTING_ENVIRONMENT_NAMES = _EXPECTED_CONFIGURATION_KEYS | _ACCOUNT_ENVIRONMENT_NAMES
_TEST_DATABASE_KEY_ENVIRONMENT_NAME = "OFCA_TEST_DATABASE_MASTER_KEY_HEX"


def test_shipped_example_is_an_explicit_development_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _SETTING_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    example = Path(__file__).resolve().parents[1] / "app" / ".env.example"
    configured = Settings(_env_file=example)
    content = example.read_text(encoding="utf-8")

    assert configured.environment == "development"
    assert configured.websocket_auth_mode == "development_stub"
    assert configured.canonical_persistence_backend == "memory"
    assert "replace-with-" not in content
    assert "LOCAL_SESSION_BOOTSTRAP_TOKEN=" not in content
    assert "SECURITY_SIGNING_SECRET=" not in content


@pytest.mark.skipif(
    os.name != "nt",
    reason="a win32 candidate is a relative path under PosixPath, so it resolves"
    " under the test CWD instead of as the drive-rooted path this asserts",
)
def test_windows_data_directory_uses_the_standard_per_user_location() -> None:
    windows = runtime_data_directory(
        environ={"LOCALAPPDATA": "C:/Users/example/AppData/Local"},
        platform="win32",
        home=Path("C:/Users/example"),
    )

    assert windows == Path(
        "C:/Users/example/AppData/Local/OnlyFans Conversational Analytics"
    )


def test_linux_and_macos_data_directories_use_the_standard_per_user_locations() -> None:
    linux = runtime_data_directory(
        environ={"XDG_DATA_HOME": "/home/example/.local/share"},
        platform="linux",
        home=Path("/home/example"),
    )
    macos = runtime_data_directory(
        environ={}, platform="darwin", home=Path("/Users/example")
    )

    assert linux.as_posix().endswith(
        "/home/example/.local/share/onlyfans-conversational-analytics"
    )
    assert macos.as_posix().endswith(
        "/Users/example/Library/Application Support/OnlyFans Conversational Analytics"
    )


def test_first_run_is_private_and_idempotent(tmp_path: Path) -> None:
    data_directory = tmp_path / "user-data"
    first = initialize_production_configuration(
        bindings=BINDINGS,
        extension_id=EXTENSION_ID,
        data_directory=data_directory,
    )
    first_bytes = first.configuration_file.read_bytes()
    values = dotenv_values(first.configuration_file, interpolate=False)
    first_bootstrap = values["LOCAL_SESSION_BOOTSTRAP_TOKEN"] or ""
    first_signing = values["SECURITY_SIGNING_SECRET"] or ""

    second = initialize_production_configuration(
        bindings=BINDINGS,
        extension_id=EXTENSION_ID,
        data_directory=data_directory,
    )

    assert first.created is True
    assert second.created is False
    assert second.configuration_file.read_bytes() == first_bytes
    assert len(first_bootstrap) >= 32
    assert len(first_signing) >= 32
    assert first_bootstrap != first_signing
    assert not first_bootstrap.startswith("replace-with-")
    assert not first_signing.startswith("replace-with-")
    assert Path(values["AUTH_DATABASE_PATH"] or "").parent == data_directory
    assert Path(values["CANONICAL_DATABASE_PATH"] or "").parent == data_directory
    assert first.configuration_file.parent == data_directory


def test_first_run_writes_exactly_the_declared_keys(tmp_path: Path) -> None:
    """The written key set is compared with an independently declared set."""

    result = initialize_production_configuration(
        bindings=BINDINGS,
        extension_id=EXTENSION_ID,
        data_directory=tmp_path,
    )

    written = set(dotenv_values(result.configuration_file, interpolate=False))

    assert written == _EXPECTED_CONFIGURATION_KEYS
    assert not written & _ACCOUNT_ENVIRONMENT_NAMES


def test_installation_identity_settings_carry_no_account() -> None:
    """No account value is addressable as installation configuration."""

    field_names = {name.upper() for name in Settings.model_fields}

    assert not field_names & _ACCOUNT_ENVIRONMENT_NAMES
    for name in _ACCOUNT_ENVIRONMENT_NAMES:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Settings(_env_file=None, **{name.lower(): "account-value"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("principal_id", "principal:verified-customer-2"),
        ("bridge_role", "operator"),
    ],
)
def test_second_first_run_rejects_changed_verified_bindings(
    tmp_path: Path, field: str, value: str
) -> None:
    initialize_production_configuration(
        bindings=BINDINGS,
        extension_id=EXTENSION_ID,
        data_directory=tmp_path,
    )
    changed = replace(BINDINGS, **{field: value})

    with pytest.raises(FirstRunConflictError, match="does not match"):
        initialize_production_configuration(
            bindings=changed,
            extension_id=EXTENSION_ID,
            data_directory=tmp_path,
        )


def test_runtime_file_identity_cannot_be_overridden_by_ambient_environment(
    tmp_path: Path,
) -> None:
    result = initialize_production_configuration(
        bindings=BINDINGS,
        extension_id=EXTENSION_ID,
        data_directory=tmp_path,
    )
    values = dotenv_values(result.configuration_file, interpolate=False)
    generated_values = {
        values["LOCAL_SESSION_BOOTSTRAP_TOKEN"] or "",
        values["SECURITY_SIGNING_SECRET"] or "",
    }
    environment = dict(os.environ)
    environment.pop(_TEST_DATABASE_KEY_ENVIRONMENT_NAME, None)
    environment["LOCAL_ANALYTICS_DATA_DIR"] = str(tmp_path)
    environment["LOCAL_PRINCIPAL_ID"] = "hand-written-process-environment"
    check = _run_python(
        """
from app.core.config import settings
assert settings.local_principal_id == 'principal:verified-customer-1'
print('ambient_identity_override=false')
""",
        environment,
    )

    _require_secret_free_output(check, generated_values)
    if check.returncode != 0:
        pytest.fail(f"runtime authority check failed:\n{_redact(check.stderr, generated_values)}")
    assert "ambient_identity_override=false" in check.stdout


@pytest.mark.windows_production
def test_empty_tree_first_run_boots_production_and_placeholder_is_rejected(
    tmp_path: Path,
) -> None:
    result = initialize_production_configuration(
        bindings=BINDINGS,
        extension_id=EXTENSION_ID,
        data_directory=tmp_path,
    )
    values = dotenv_values(result.configuration_file, interpolate=False)
    generated_values = {
        values["LOCAL_SESSION_BOOTSTRAP_TOKEN"] or "",
        values["SECURITY_SIGNING_SECRET"] or "",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _SETTING_ENVIRONMENT_NAMES
        and key != _TEST_DATABASE_KEY_ENVIRONMENT_NAME
    }
    environment["LOCAL_ANALYTICS_DATA_DIR"] = str(tmp_path)
    boot = _run_python(
        """
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['environment'] == 'production'
print('production_boot=ok environment=production status=200')
""",
        environment,
    )
    _require_secret_free_output(boot, generated_values)
    if boot.returncode != 0:
        pytest.fail(f"production process failed:\n{_redact(boot.stderr, generated_values)}")
    assert "production_boot=ok environment=production status=200" in boot.stdout

    content = result.configuration_file.read_text(encoding="utf-8")
    signing = values["SECURITY_SIGNING_SECRET"] or ""
    result.configuration_file.write_text(
        content.replace(signing, "replace-with-invalid-signing-secret", 1),
        encoding="utf-8",
    )
    rejected = _run_python("from app.main import app", environment)
    _require_secret_free_output(rejected, generated_values)
    assert rejected.returncode != 0
    assert "generated security signing secret" in rejected.stderr


def _run_python(source: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _require_secret_free_output(
    completed: subprocess.CompletedProcess[str],
    generated_values: set[str],
) -> None:
    output = completed.stdout + completed.stderr
    if any(value and value in output for value in generated_values):
        pytest.fail("a generated secret appeared in process output")


def _redact(value: str, generated_values: set[str]) -> str:
    for secret in generated_values:
        if secret:
            value = value.replace(secret, "[redacted]")
    return value
