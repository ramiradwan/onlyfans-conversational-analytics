"""Boot dispatch coverage at the import boundary."""

from __future__ import annotations

import builtins
import asyncio
import os
import re
import subprocess
import sys
import types
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import packaged_entry
from app.core.config import Settings

pytestmark = [pytest.mark.ci_tier('fast'), pytest.mark.windows_compat]


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
# Every Settings field name, uppercased: the environment surface cleared from
# the child process below.
_SETTINGS_ENVIRONMENT_NAMES = {name.upper() for name in Settings.model_fields}

_FRESH_PROCESS_PROBE = """\
import sys

from app import packaged_entry

application = packaged_entry.select_brain_application()
boot_mode = "provisioning" if application.openapi_url is None else "configured"
print(f"boot_mode={boot_mode}")
print(f"config_imported={str('app.core.config' in sys.modules).lower()}")

from fastapi.testclient import TestClient

with TestClient(application) as client:
    response = client.get("/health")
    print(f"health_status={response.status_code}")
"""


@pytest.mark.parametrize("desktop_launcher", [False, True])
def test_fresh_process_unconfigured_provisioning_does_not_import_config(
    tmp_path, desktop_launcher,
) -> None:
    """A fresh interpreter with no runtime.env boots provisioning without
    reaching app.core.config: the provisioning import graph must not cross
    into configured-runtime settings before a bootstrap token can exist.
    """
    data_directory = tmp_path / "runtime-data"
    data_directory.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _SETTINGS_ENVIRONMENT_NAMES
    }
    environment["LOCAL_ANALYTICS_DATA_DIR"] = str(data_directory)
    # First-run journeys now open isolated SQLCipher storage. Keep the explicit
    # test-only key in its permitted mode without supplying runtime settings.
    environment["ENVIRONMENT"] = "test"
    if desktop_launcher:
        environment[packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE] = "x" * 43

    result = subprocess.run(
        [sys.executable, "-c", _FRESH_PROCESS_PROBE],
        cwd=PRODUCT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "boot_mode=provisioning" in result.stdout
    assert "config_imported=false" in result.stdout
    assert "health_status=200" in result.stdout


def test_missing_runtime_configuration_selects_provisioning_without_main_import(
    tmp_path, monkeypatch
) -> None:
    original_import = builtins.__import__

    def fail_if_runtime_is_imported(name, *args, **kwargs):
        if name == "app.main":
            raise AssertionError("app.main import boundary was crossed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_if_runtime_is_imported)

    application = packaged_entry.select_brain_application(tmp_path)

    assert application.openapi_url is None


@pytest.mark.parametrize("hosted_configured", [False, True])
@pytest.mark.parametrize("saved_setup", ["absent", "available", "provider-refused"])
def test_composed_native_admission_resolves_hardware_only_for_saved_setup(
    tmp_path: Path, monkeypatch, hosted_configured: bool, saved_setup: str,
) -> None:
    """Real composition and native admission retain the existing key boundary."""
    from app.core.customer_release import CustomerReleaseConfig
    from app.persistence.onboarding import OnboardingJourneyStore
    from app.provisioning import claim_submission
    from app.provisioning.completion import durable_authentication_store
    from app.onboarding.admission_routes import ORIGIN
    from app.security.installation_key import InstallationKeyAuthority, InstallationKeyUnavailable
    from test_continuation_selection import add_candidate, enroll
    from test_provisioning_claim_submission import EmulatedPlatformProvider

    token = "t" * 32
    monkeypatch.setenv(packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, token)
    release = CustomerReleaseConfig(
        "https://onboarding.example.test/public/onboarding/setup" if hosted_configured else "",
        "https://api.example.test" if hosted_configured else "",
    )
    monkeypatch.setattr(packaged_entry, "load_customer_release_config", lambda: release)
    store = durable_authentication_store(tmp_path)()
    provider = EmulatedPlatformProvider()
    original_key = None
    if saved_setup != "absent":
        original_key = InstallationKeyAuthority(store, provider).ensure_ready()
        enroll(store)
        add_candidate(store)
    provider_calls: list[str] = []

    def resolve_provider():
        provider_calls.append("open")
        if saved_setup == "provider-refused":
            raise InstallationKeyUnavailable("Test provider is unavailable")
        return provider

    def forbid_new_key(*_):
        pytest.fail("Saved setup must reopen the existing key")

    monkeypatch.setattr(claim_submission, "WindowsCNGInstallationKeyProvider", resolve_provider)
    monkeypatch.setattr(provider, "create_non_exportable_key", forbid_new_key)
    application = packaged_entry.select_brain_application(tmp_path)
    assert provider_calls == []
    with TestClient(application, base_url=ORIGIN) as client:
        issued = client.post("/api/v1/desktop/entry", json={}, headers={"Authorization": "Desktop " + token})
        assert issued.status_code == 200
        consumed = client.post("/api/v1/desktop/entry/consume", json={"code": issued.json()["entry_code"]}, headers={"Origin": ORIGIN})
        assert consumed.status_code == 200
        assert provider_calls == []
        assert store.installation_key_reference() == original_key
        # Presentation admission is read-only. The native owner's explicit
        # domain command reopens a saved key, never a page visit or locator.
        owner = application.state.desktop_setup_owner
        from app.onboarding.coordinator import CommandRefused
        def select():
            with store.database.transaction() as connection:
                return owner.admit(connection, None, action="account.connect", scope={"creator_account_id":"creator-1"})
        if saved_setup == "provider-refused":
            with pytest.raises(CommandRefused): select()
            with store.database.read() as connection:
                assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 0
        else:
            selected = select()
            journey = OnboardingJourneyStore(store).require_current(selected.scope["source_journey_id"])
            assert journey["kind"] == ("initial-enrollment" if saved_setup == "absent" else "registered-continuation")
        assert provider_calls == ([] if saved_setup == "absent" else ["open"])


def test_runtime_configuration_presence_selects_runtime_application(tmp_path, monkeypatch) -> None:
    configuration = tmp_path / "runtime.env"
    configuration.touch()
    sentinel = object()
    main_module = types.ModuleType("app.main")
    main_module.app = sentinel

    monkeypatch.setitem(sys.modules, "app.main", main_module)

    assert packaged_entry.select_brain_application(tmp_path) is sentinel


_RUNNING_MUTEX_PROBE = """\
import ctypes
import sys
from ctypes import wintypes

from app import packaged_entry

if "--hold" in sys.argv:
    packaged_entry.hold_running_application_mutex()

open_mutex = ctypes.windll.kernel32.OpenMutexW
open_mutex.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
open_mutex.restype = wintypes.HANDLE
SYNCHRONIZE = 0x00100000
handle = open_mutex(
    SYNCHRONIZE, False, packaged_entry.RUNNING_APPLICATION_MUTEX_NAME
)
print(f"published={str(bool(handle)).lower()}")
"""


def _run_mutex_probe(*arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _RUNNING_MUTEX_PROBE, *arguments],
        cwd=PRODUCT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the mutex is a Windows installer signal")
def test_running_application_mutex_is_published_only_while_held() -> None:
    """The installer's running-application signal exists only when the app runs."""

    assert "published=false" in _run_mutex_probe()
    assert "published=true" in _run_mutex_probe("--hold")


def test_main_publishes_the_running_application_mutex(monkeypatch) -> None:
    """Both process modes reach the mutex before dispatching."""

    published: list[bool] = []

    def record() -> bool:
        published.append(True)
        return True

    monkeypatch.setattr(packaged_entry, "hold_running_application_mutex", record)
    monkeypatch.setattr(packaged_entry, "run_brain", lambda: 0)

    assert packaged_entry.main(["--brain"]) == 0
    assert published == [True]


def test_installer_mutex_directive_matches_the_packaged_name() -> None:
    """The uninstaller guard is inert if the two names drift apart."""

    script = (PRODUCT_ROOT / "packaging" / "inno" / "brain.iss").read_text(
        encoding="utf-8"
    )
    defined = re.findall(r'^#define AppMutexName "([^"]+)"$', script, re.MULTILINE)
    assert defined == [packaged_entry.RUNNING_APPLICATION_MUTEX_NAME]
    # The directive and the uninstall-side check both resolve to that one define,
    # so neither can drift from the name the application publishes.
    assert "AppMutex={#AppMutexName}" in script
    assert "OpenRunningApplicationMutex(Synchronize, False, '{#AppMutexName}')" in script


@pytest.mark.parametrize("targeted", [False, True])
def test_composed_fresh_native_admission_keeps_extension_journey(tmp_path, monkeypatch, targeted):
    from uuid import uuid4
    from app.core.customer_release import CustomerReleaseConfig
    from app.onboarding.admission_routes import ORIGIN
    from app.provisioning import claim_submission
    from app.provisioning.completion import durable_authentication_store
    token = "t" * 32
    journey = str(uuid4()) if targeted else None
    monkeypatch.setenv(packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, token)
    monkeypatch.setattr(packaged_entry, "load_customer_release_config", lambda: CustomerReleaseConfig("", ""))
    def no_hardware(): pytest.fail("Presentation admission cannot touch hardware keys")
    monkeypatch.setattr(claim_submission, "WindowsCNGInstallationKeyProvider", no_hardware)
    application = packaged_entry.select_brain_application(tmp_path)
    with TestClient(application, base_url=ORIGIN) as client:
        issued = client.post("/api/v1/desktop/entry", json={"journey_id":journey}, headers={"Authorization":"Desktop "+token})
        assert issued.status_code == 200
        body = {"code":issued.json()["entry_code"]}
        selected = client.post("/api/v1/desktop/entry/consume", json=body, headers={"Origin":ORIGIN})
        assert selected.status_code == 200 and selected.json()["journey_id"] == journey
        assert client.post("/api/v1/desktop/entry/consume", json=body, headers={"Origin":ORIGIN}).status_code == 401
    store = durable_authentication_store(tmp_path)()
    assert store.installation_key_reference() is None
    assert not (tmp_path/"runtime.env").exists()
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM desktop_admission_sessions").fetchone()[0] == 1


@pytest.mark.parametrize("stage", ["setup", "runtime"])
@pytest.mark.parametrize("hosted", [False, True])
def test_boot_roots_share_owned_setup_services_and_close_once(tmp_path, monkeypatch, stage, hosted):
    from fastapi import FastAPI
    from app.core.customer_release import CustomerReleaseConfig
    from app.onboarding.runtime import RuntimeSetupOwner
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    release = CustomerReleaseConfig(
        "https://provider.example.test/public/onboarding/setup" if hosted else "",
        "https://provider.example.test" if hosted else "",
        ("google", "default"))
    if stage == "setup":
        monkeypatch.setenv(packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, "t" * 32)
        monkeypatch.setattr(packaged_entry, "load_customer_release_config", lambda: release)
        application = packaged_entry.select_brain_application(tmp_path)
    else:
        from app.api.endpoints import desktop_onboarding
        monkeypatch.setattr(desktop_onboarding, "settings", types.SimpleNamespace(
            auth_database_path=str(tmp_path / "auth.sqlite3"), extension_id=""))
        monkeypatch.setattr(desktop_onboarding, "load_customer_release_config", lambda: release)
        application = FastAPI()
        desktop_onboarding.configure_desktop_setup(application, launcher_token="t" * 32)
    core = application.state.desktop_setup_core
    assert core.admissions.stage == stage
    assert core.tasks.admissions is core.admissions
    assert core.coordinator is application.state.desktop_coordinator
    assert core.coordinator.owner is application.state.desktop_setup_owner
    assert core.recovery.browser_tasks is core.tasks
    assert core.recovery.ceremonies is core.ceremonies
    assert (core.ceremonies is not None) == hosted
    if stage == "setup":
        assert type(core.coordinator.owner) is ProvisioningSetupOwner
    else:
        owner = core.coordinator.owner
        assert type(owner) is RuntimeSetupOwner and owner.setup_owner is core.recovery
        assert owner.pairing is not None
        assert (owner.activation is not None) == hosted
        if hosted:
            assert owner.activation.registered is not core.recovery.registered
            assert owner.activation.registered.retain_current_binding
    if hosted:
        assert not core.recovery.registered.retain_current_binding
        assert core.recovery.initial.authorize_scope.__self__ is core.recovery
        assert core.ceremonies.client.key is core.recovery.key
        assert core.ceremonies.authentication.choices == {"google": True, "microsoft-personal": False, "default": True}
    # Observe the real graph's shutdown, including the separate activation
    # continuation, instead of substituting a model of its ownership.
    services = [core.coordinator, core.coordinator.owner]
    if hosted:
        services.extend([core.recovery.initial, core.recovery.registered, core.ceremonies])
        if stage == "runtime":
            services.append(core.coordinator.owner.activation.registered)
    stopped = []
    for service in services:
        service.stop = AsyncMock(wraps=service.stop)
        stopped.append(service.stop)
    async def close():
        core.coordinator.start()
        await asyncio.gather(core.close(), core.close())
        await core.close()
    asyncio.run(close())
    assert all(stop.await_count == 1 for stop in stopped)
    assert core.coordinator._closed and not core.coordinator._workers
    assert core.admissions.authentication.installation_key_reference() is None


def test_setup_core_shutdown_survives_cancelled_waiter(tmp_path):
    from app.onboarding.composition import build_desktop_setup
    from app.persistence.auth import SQLiteAuthenticationStore
    core = build_desktop_setup(store=SQLiteAuthenticationStore(tmp_path / "auth.sqlite3"),
        stage="setup", extension_id=None, hosted_origin="", authentication_providers=("default",),
        finalize=lambda **kwargs: None, completion_ready=lambda: False)
    async def close():
        entered, release = asyncio.Event(), asyncio.Event()
        original = core.coordinator.owner.stop
        async def drain():
            entered.set()
            await release.wait()
            await original()
        core.coordinator.owner.stop = AsyncMock(side_effect=drain)
        waiter = asyncio.create_task(core.close())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not core._close_task.done()
        release.set()
        await core.close()
        assert core.coordinator.owner.stop.await_count == 1
    asyncio.run(close())


@pytest.mark.parametrize("arguments", [[], ["--open-workspace", "ofca://onboarding/?journey=anything"]])
def test_brain_has_no_user_launcher_mode(arguments, monkeypatch):
    monkeypatch.setattr(packaged_entry, "hold_running_application_mutex", lambda: None)
    with pytest.raises(SystemExit, match="Bridge.exe"):
        packaged_entry.main(arguments)
