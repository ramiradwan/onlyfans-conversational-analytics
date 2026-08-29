"""Covers the E2E grant seeder's provider probe and its synthetic-key gate.

The probe decides whether the host has a usable TPM-backed platform provider,
and that one decision separates seeding a synthetic installation key from
surfacing a real activation failure. Constructing the CNG adapter cannot make
that decision: it loads the API without opening the provider.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

from app.persistence.auth import InstallationKeyReservation, SQLiteAuthenticationStore
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    InstallationKeyPolicyError,
    InstallationKeyUnavailable,
)


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = (
    PRODUCT_ROOT / "tools" / "e2e-capture" / "helpers" / "seed_webauthn_grants.py"
)


def _load_helper() -> ModuleType:
    """Import the helper by path, since its directory is no import package."""

    specification = importlib.util.spec_from_file_location(
        "e2e_seed_webauthn_grants", HELPER_PATH
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


seed = _load_helper()


def _store(tmp_path: Path) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")


def _reserve(
    store: SQLiteAuthenticationStore, provider_name: str, provider_key_name: str
) -> InstallationKeyReservation:
    """Reserve without activating, as Brain does before it opens a provider."""

    return store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name=provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )


def _seed_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed_webauthn_grants", "--auth-database", str(tmp_path / "auth.sqlite3")],
    )


def test_provider_probe_opens_the_provider_and_accepts_an_absent_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            probed.append(provider_key_name)
            return None

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)

    assert seed._real_key_provider_available() is True
    assert probed == [seed.PROVIDER_PROBE_KEY_NAME]


def test_provider_probe_rejects_a_host_without_the_platform_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        def __init__(self) -> None:
            raise InstallationKeyUnavailable(
                "The TPM-backed platform provider is available only on Windows"
            )

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)

    assert seed._real_key_provider_available() is False


def test_provider_probe_rejects_a_provider_that_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction succeeds on any Windows host; opening the provider need not."""

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)

    assert seed._real_key_provider_available() is False


def test_provider_probe_rejects_a_provider_that_is_not_hardware_backed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyPolicyError(
                "The installation key provider is not hardware-backed"
            )

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)

    assert seed._real_key_provider_available() is False


def test_an_unusable_provider_seeds_a_synthetic_installation_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    store = _store(tmp_path)

    key = seed._ensure_installation_key_active(store)

    assert key is not None
    assert key.provider_name == seed.SYNTHETIC_KEY_PROVIDER_NAME
    assert store.installation_key_reference() == key


def test_a_usable_provider_never_seeds_a_synthetic_installation_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A usable provider that activated no key is a failure to surface, not to seed."""

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            return None

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    store = _store(tmp_path)

    assert seed._ensure_installation_key_active(store) is None
    assert store.installation_key_reference() is None


def test_an_unusable_provider_supersedes_a_stranded_platform_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Brain reserves the platform provider before discovering it cannot open it.

    The reservation outlives that failure, and the store binds activation to
    whichever reservation it holds, so the synthetic key is activatable only
    once the stranded one is released.
    """

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    store = _store(tmp_path)
    _reserve(store, PLATFORM_CRYPTO_PROVIDER, "bridge-clean.installation.v1.stranded")

    key = seed._ensure_installation_key_active(store)

    assert key is not None
    assert key.provider_name == seed.SYNTHETIC_KEY_PROVIDER_NAME
    assert store.installation_key_reservation().provider_name == (
        seed.SYNTHETIC_KEY_PROVIDER_NAME
    )


def test_an_unusable_provider_resumes_its_own_earlier_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A synthetic reservation from an earlier run is resumed, not superseded."""

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    store = _store(tmp_path)
    reserved = _reserve(
        store, seed.SYNTHETIC_KEY_PROVIDER_NAME, seed.SYNTHETIC_KEY_NAME
    )

    key = seed._ensure_installation_key_active(store)

    assert key is not None
    assert key.created_at == reserved.created_at


def test_a_usable_provider_leaves_a_platform_reservation_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only a provider proven unusable may strand a reservation worth releasing."""

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            return None

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    store = _store(tmp_path)
    reserved = _reserve(store, PLATFORM_CRYPTO_PROVIDER, "bridge-clean.installation.v1.live")

    assert seed._ensure_installation_key_active(store) is None
    assert store.installation_key_reservation() == reserved
    assert store.installation_key_reference() is None


def test_an_unrecognized_reservation_is_refused_rather_than_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    store = _store(tmp_path)
    reserved = _reserve(store, "Some Other Key Storage Provider", "someone-elses-key")

    with pytest.raises(RuntimeError, match="Refusing to replace"):
        seed._ensure_installation_key_active(store)

    assert store.installation_key_reservation() == reserved


def test_an_unusable_provider_reports_the_synthetic_key_it_seeded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    _seed_arguments(monkeypatch, tmp_path)

    assert seed.main() == 0

    reported = json.loads(capsys.readouterr().out)
    assert reported["authorized_creator_account_id"] == seed.ACCOUNT_ID
    assert reported["installation_key_id"].startswith(seed.SYNTHETIC_KEY_NAME)


def test_a_usable_provider_reports_that_the_synthetic_key_was_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The refusal is named, so an inactive key is not read as a seeding fault."""

    class Provider:
        def key_info(self, provider_key_name: str) -> None:
            return None

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Provider)
    _seed_arguments(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="probe succeeded, so no synthetic key"):
        seed.main()


def test_an_active_installation_key_is_returned_without_probing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Unusable:
        def key_info(self, provider_key_name: str) -> None:
            raise InstallationKeyUnavailable("opening the installation key provider")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Unusable)
    store = _store(tmp_path)
    seeded = seed._ensure_installation_key_active(store)
    assert seeded is not None

    class Forbidden:
        def __init__(self) -> None:
            raise AssertionError("An active key is returned before any probe")

    monkeypatch.setattr(seed, "WindowsCNGInstallationKeyProvider", Forbidden)

    assert seed._ensure_installation_key_active(store) == seeded
