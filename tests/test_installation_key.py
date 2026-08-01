from __future__ import annotations

import base64
import ctypes
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
)

import app.main as main_module
from app.core.config import Settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    SOFTWARE_KEY_STORAGE_PROVIDER,
    InstallationKeyAuthority,
    InstallationKeyPolicyError,
    InstallationKeyUnavailable,
    ProviderKeyInfo,
    WindowsCNGInstallationKeyProvider,
    verify_installation_proof,
)


class EmulatedPlatformProvider:
    provider_name = PLATFORM_CRYPTO_PROVIDER

    def __init__(self, *, export_policy: int = 0) -> None:
        self.export_policy = export_policy
        self.create_calls = 0
        self.created_names: list[str] = []
        self.creation_refused = False
        self.signing_refused = False
        self._keys: dict[str, ec.EllipticCurvePrivateKey] = {}

    def key_info(self, provider_key_name: str) -> ProviderKeyInfo | None:
        key = self._keys.get(provider_key_name)
        if key is None:
            return None
        numbers = key.public_key().public_numbers()
        return ProviderKeyInfo(
            provider_name=self.provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            export_policy=self.export_policy,
            hardware_backed=True,
            x=numbers.x.to_bytes(32, "big"),
            y=numbers.y.to_bytes(32, "big"),
        )

    def create_non_exportable_key(
        self, provider_key_name: str
    ) -> ProviderKeyInfo:
        self.create_calls += 1
        self.created_names.append(provider_key_name)
        if self.creation_refused:
            raise InstallationKeyUnavailable("emulated TPM creation refusal")
        self._keys[provider_key_name] = ec.generate_private_key(ec.SECP256R1())
        info = self.key_info(provider_key_name)
        assert info is not None
        return info

    def sign_digest(self, provider_key_name: str, digest: bytes) -> bytes:
        if self.signing_refused:
            raise InstallationKeyUnavailable("emulated TPM signing refusal")
        key = self._keys[provider_key_name]
        der = key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


@pytest.fixture
def store(tmp_path: Path) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3")


def test_second_run_reuses_one_persisted_provider_key_and_public_binding(
    store: SQLiteAuthenticationStore,
) -> None:
    provider = EmulatedPlatformProvider()
    authority = InstallationKeyAuthority(store, provider)

    first = authority.ensure_ready()
    second = authority.ensure_ready()
    challenge = b"hosted-proof-challenge"
    proof = authority.sign_challenge(challenge)

    assert first == second
    assert provider.create_calls == 1
    assert first.installation_key_id.startswith("ik1.")
    public_members = json.loads(first.public_key_jwk)
    public_members.pop("kid")
    canonical_public = json.dumps(
        public_members, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    thumbprint = base64.urlsafe_b64decode(
        first.installation_key_jkt + "="
    )
    assert thumbprint == hashlib.sha256(canonical_public).digest()
    assert len(thumbprint) == 32
    assert first.installation_key_id == (
        "ik1."
        + base64.urlsafe_b64encode(thumbprint[:16]).rstrip(b"=").decode("ascii")
    )
    assert verify_installation_proof(first.public_key_jwk, challenge, proof)
    assert not verify_installation_proof(
        first.public_key_jwk, challenge + b"-tampered", proof
    )


def test_key_reference_persists_only_provider_and_public_data(
    store: SQLiteAuthenticationStore,
) -> None:
    provider = EmulatedPlatformProvider()
    reference = InstallationKeyAuthority(store, provider).ensure_ready()

    with store.database.read() as connection:
        row = connection.execute(
            "SELECT * FROM installation_key_reference WHERE singleton = 1"
        ).fetchone()

    assert row["provider_name"] == PLATFORM_CRYPTO_PROVIDER
    assert row["provider_key_name"] == reference.provider_key_name
    assert row["installation_key_id"] == reference.installation_key_id
    assert row["installation_key_jkt"] == reference.installation_key_jkt
    assert json.loads(row["public_key_jwk"])["kid"] == reference.installation_key_id
    assert set(row.keys()) == {
        "singleton",
        "provider_name",
        "provider_key_name",
        "algorithm",
        "installation_key_id",
        "installation_key_jkt",
        "public_key_jwk",
        "created_at",
        "activated_at",
    }


def test_creation_refusal_retries_the_same_durable_provider_name(
    store: SQLiteAuthenticationStore,
) -> None:
    provider = EmulatedPlatformProvider()
    provider.creation_refused = True
    authority = InstallationKeyAuthority(store, provider)

    with pytest.raises(InstallationKeyUnavailable, match="creation refusal"):
        authority.ensure_ready()
    reservation = store.installation_key_reservation()
    assert reservation is not None

    provider.creation_refused = False
    reference = authority.ensure_ready()

    assert provider.created_names == [
        reservation.provider_key_name,
        reservation.provider_key_name,
    ]
    assert reference.provider_key_name == reservation.provider_key_name


def test_existing_key_use_refusal_fails_without_minting_a_second_key(
    store: SQLiteAuthenticationStore,
) -> None:
    provider = EmulatedPlatformProvider()
    authority = InstallationKeyAuthority(store, provider)
    reference = authority.ensure_ready()
    provider.signing_refused = True

    with pytest.raises(InstallationKeyUnavailable, match="signing refusal"):
        authority.ensure_ready()

    assert provider.create_calls == 1
    assert store.installation_key_reference() == reference


def test_missing_existing_provider_key_fails_without_replacement(
    store: SQLiteAuthenticationStore,
) -> None:
    provider = EmulatedPlatformProvider()
    authority = InstallationKeyAuthority(store, provider)
    authority.ensure_ready()
    provider._keys.clear()

    with pytest.raises(InstallationKeyUnavailable, match="no longer available"):
        authority.ensure_ready()

    assert provider.create_calls == 1


def test_software_provider_is_rejected_before_cng_can_create_a_key() -> None:
    with pytest.raises(InstallationKeyPolicyError, match="TPM-backed"):
        WindowsCNGInstallationKeyProvider(SOFTWARE_KEY_STORAGE_PROVIDER)


def test_production_configuration_cannot_bypass_the_tpm_startup_gate() -> None:
    with pytest.raises(ValueError, match="TPM-gated local session"):
        Settings(environment="production", websocket_auth_mode="development_stub")


@pytest.mark.asyncio
async def test_runtime_startup_refuses_an_exportable_installation_key(
    store: SQLiteAuthenticationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = InstallationKeyAuthority(
        store, EmulatedPlatformProvider(export_policy=1)
    )
    connect = AsyncMock()
    monkeypatch.setattr(main_module.settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(main_module, "initialize_installation_key", authority.ensure_ready)
    monkeypatch.setattr(main_module.broadcast, "connect", connect)

    with pytest.raises(InstallationKeyPolicyError, match="permits private-key export"):
        await main_module.startup_event()

    connect.assert_not_awaited()
    assert store.installation_key_reference() is None


@pytest.mark.slow
def test_windows_tpm_provider_creates_non_exportable_signing_key() -> None:
    provider_key_name = f"bridge-clean.hardware-test.{uuid4()}"
    try:
        provider = WindowsCNGInstallationKeyProvider()
        info = provider.create_non_exportable_key(provider_key_name)
    except InstallationKeyUnavailable as error:
        pytest.skip(f"TPM-backed CNG provider unavailable: {error}")

    try:
        assert info.provider_name == PLATFORM_CRYPTO_PROVIDER
        assert info.hardware_backed is True
        assert info.export_policy == 0
        digest = hashlib.sha256(b"hardware-proof-challenge").digest()
        assert len(provider.sign_digest(provider_key_name, digest)) == 64
    finally:
        with provider._provider_handle() as provider_handle:
            key = ctypes.c_size_t()
            status = provider._api.open_key(
                provider_handle,
                ctypes.byref(key),
                provider_key_name,
                0,
                0x00000040,
            )
            if int(status) & 0xFFFFFFFF == 0:
                delete_status = provider._api.delete_key(key.value, 0)
                assert int(delete_status) & 0xFFFFFFFF == 0
