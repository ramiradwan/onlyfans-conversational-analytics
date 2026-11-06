"""Why passkey state is unavailable when account authority has lapsed."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.activation import require_activated_runtime
from app.api.endpoints import webauthn as webauthn_endpoints
from app.api.security import get_runtime_policy
from app.core.config import settings
from app.persistence.auth import (
    InstallationKeyReference,
    InstallationKeyReservation,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.security.grant_refresh import renewal_runs
from app.security.grant_types import VerifiedGrantDenial
from app.security.webauthn import (
    WebAuthnAccessState,
    WebAuthnAuthorityPort,
    WebAuthnAuthorityResult,
)

pytestmark = [pytest.mark.ci_tier('fast')]

ORIGIN = "http://bridge.localhost:17871"
ISSUER = "https://identity.example"
SUBJECT = "subject-access"
ACCOUNT_ID = "creator-access"
INSTALLATION_ID = "installation-access"
KEY_ID = "installation-key-access"
KEY_JKT = "installation-thumbprint-access"
LIFETIMES = {
    "installation_grant": timedelta(days=30),
    "membership_snapshot": timedelta(hours=1),
    "creator_account_binding": timedelta(days=7),
}


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 6, 9, tzinfo=timezone.utc))


def _jti(number: int) -> str:
    return f"019937ce-7600-7000-8000-{number:012x}"


def _grant(clock: Clock, grant_type: str, number: int, **changes) -> VerifiedGrantReference:
    membership = grant_type == "membership_snapshot"
    binding = grant_type == "creator_account_binding"
    value = VerifiedGrantReference(
        reference_id=f"{grant_type}-access-{number}",
        grant_identifier=_jti(number),
        grant_type=grant_type,
        grant_digest=hashlib.sha256(f"{grant_type}-{number}".encode()).hexdigest(),
        issuer=ISSUER,
        subject=SUBJECT,
        installation_id=INSTALLATION_ID,
        creator_account_id=ACCOUNT_ID if binding else None,
        valid_from=clock.now - timedelta(days=1),
        expires_at=clock.now + LIFETIMES[grant_type],
        verified_at=clock.now - timedelta(minutes=5),
        organization_id="organization-access",
        installation_key_id=KEY_ID,
        installation_key_jkt=KEY_JKT,
        membership_id="membership-access" if membership else None,
        approval_id="approval-access" if binding else None,
        approval_revision=1 if binding else None,
        allowed_creator_account_ids=(ACCOUNT_ID,) if membership else None,
        membership_roles=("owner",) if membership else None,
    )
    return replace(value, **changes)


def _store(tmp_path: Path, clock: Clock, **changes) -> SQLiteAuthenticationStore:
    """Seed one installation whose membership lapses first, then its binding."""
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    store.reserve_installation_key(InstallationKeyReservation(
        provider_name="test-provider", provider_key_name="test-key", algorithm="ES256",
        created_at=clock.now - timedelta(days=2),
    ))
    store.activate_installation_key(InstallationKeyReference(
        provider_name="test-provider", provider_key_name="test-key", algorithm="ES256",
        installation_key_id=KEY_ID, installation_key_jkt=KEY_JKT, public_key_jwk='{"kty":"EC"}',
        created_at=clock.now - timedelta(days=2), activated_at=clock.now - timedelta(days=2),
    ))
    for number, grant_type in enumerate(LIFETIMES, 1):
        store.record_verified_grant(_grant(clock, grant_type, number, **changes.get(grant_type, {})))
    return store


def _access(store: SQLiteAuthenticationStore, clock: Clock):
    port = WebAuthnAuthorityPort(store, clock=clock)
    decision, access = port.session_access(store.build_runtime_policy())
    return decision.result, access


def _membership_reference(store: SQLiteAuthenticationStore) -> VerifiedGrantReference:
    return next(grant for grant in store.verified_grants() if grant.grant_type == "membership_snapshot")


def _leave_active(store: SQLiteAuthenticationStore, reference_id: str, *, keep_scope: bool) -> None:
    # The store revokes every withdrawn row it can match. These raw writes build
    # the inconsistent state the withdrawal checks still refuse to call renewable.
    with store.database.transaction() as connection:
        connection.execute(
            "UPDATE verified_grant_references SET revoked_at = NULL WHERE reference_id = ?", (reference_id,))
        if not keep_scope:
            connection.execute(
                "DELETE FROM auth_revocation_state WHERE scope_type = 'verified_grant' AND scope_id = ?",
                (reference_id,))


def test_current_authority_reports_enrollment_without_an_access_state(tmp_path, clock):
    store = _store(tmp_path, clock)
    assert _access(store, clock) == (WebAuthnAuthorityResult.CREDENTIAL_MISSING, None)


@pytest.mark.parametrize("after", [timedelta(hours=2), timedelta(days=8), timedelta(days=29)])
def test_lapsed_authority_on_a_usable_installation_grant_is_renewing(tmp_path, clock, after):
    store = _store(tmp_path, clock)
    clock.now += after
    assert _access(store, clock) == (
        WebAuthnAuthorityResult.REQUIRED_GRANT_EXPIRED, WebAuthnAccessState.RENEWING)


@pytest.mark.parametrize("changes,after", [
    pytest.param({}, timedelta(days=31), id="installation-grant-past-its-grace"),
    pytest.param({"membership_snapshot": {"subject": "subject-other"}}, timedelta(hours=2),
                 id="anchor-principal-differs"),
    pytest.param({grant_type: {"installation_key_id": "installation-key-other"} for grant_type in LIFETIMES},
                 timedelta(hours=2), id="grants-on-another-key"),
])
def test_lapsed_authority_without_a_usable_matching_installation_grant_has_expired(tmp_path, clock, changes, after):
    store = _store(tmp_path, clock, **changes)
    clock.now += after
    assert _access(store, clock) == (
        WebAuthnAuthorityResult.REQUIRED_GRANT_EXPIRED, WebAuthnAccessState.EXPIRED)


@pytest.mark.parametrize("withdrawal,after", [
    ("installation", timedelta(hours=2)),
    ("creator_account", timedelta(hours=2)),
    ("verified_grant", timedelta(hours=2)),
    ("tombstone", timedelta(hours=2)),
    pytest.param("creator_account", timedelta(days=31), id="creator_account-past-the-installation-grace"),
])
def test_withdrawn_authority_is_removed_even_while_another_grant_could_renew(tmp_path, clock, withdrawal, after):
    store = _store(tmp_path, clock)
    membership = _membership_reference(store)
    if withdrawal == "installation":
        store.revoke(RevocationKey(RevocationScopeType.INSTALLATION, INSTALLATION_ID), reason="test")
    elif withdrawal == "creator_account":
        store.revoke(RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_ID), reason="test")
    elif withdrawal == "verified_grant":
        store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, membership.reference_id), reason="test")
        _leave_active(store, membership.reference_id, keep_scope=True)
    else:
        instant = int(clock.now.timestamp())
        denial = VerifiedGrantDenial(
            denial_jti=_jti(100), grant_type=membership.grant_type, revoked_jti=membership.grant_identifier,
            issued_at=instant, expires_at=instant + 600, effective_at=instant,
            reason_code="membership_removed", evidence_sha256=hashlib.sha256(b"denial").hexdigest(),
        )
        assert store.apply_hosted_grant_denial(membership, denial) == "applied"
        _leave_active(store, membership.reference_id, keep_scope=False)
    clock.now += after
    assert _access(store, clock) == (
        WebAuthnAuthorityResult.REQUIRED_GRANT_EXPIRED, WebAuthnAccessState.REMOVED)


def test_revoked_required_grant_is_removed(tmp_path, clock):
    store = _store(tmp_path, clock)
    installation = next(grant for grant in store.verified_grants() if grant.grant_type == "installation_grant")
    store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, installation.reference_id), reason="test")
    assert _access(store, clock) == (
        WebAuthnAuthorityResult.REQUIRED_GRANT_REVOKED, WebAuthnAccessState.REMOVED)


def test_revoked_authority_with_an_enrolled_passkey_is_removed(tmp_path, clock):
    store = _store(tmp_path, clock)
    store.register_webauthn_credential(WebAuthnCredential(
        credential_id="credential-access", principal_id="principal-access", external_issuer=ISSUER,
        external_subject=SUBJECT, installation_id=INSTALLATION_ID, public_key=b"test-public-key",
        signature_count=0, enrolled_at=clock.now - timedelta(hours=1),
    ))
    assert _access(store, clock) == (WebAuthnAuthorityResult.AUTHORIZED, None)
    store.revoke(RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, ACCOUNT_ID), reason="test")
    assert _access(store, clock) == (
        WebAuthnAuthorityResult.AUTHORITY_REVOKED, WebAuthnAccessState.REMOVED)


@pytest.mark.parametrize("future_type,expected", [
    pytest.param("membership_snapshot", WebAuthnAuthorityResult.REQUIRED_GRANT_NOT_CURRENT,
                 id="membership-not-yet-valid"),
    pytest.param("creator_account_binding", WebAuthnAuthorityResult.REQUIRED_GRANT_EXPIRED,
                 id="renewable-membership-beside-a-binding-not-yet-valid"),
])
def test_authority_that_cannot_be_named_is_unavailable(tmp_path, clock, future_type, expected):
    later = clock.now + timedelta(hours=2)
    store = _store(tmp_path, clock, **{future_type: {
        "valid_from": later + timedelta(minutes=5), "expires_at": later + timedelta(days=1)}})
    clock.now = later
    assert _access(store, clock) == (expected, WebAuthnAccessState.UNAVAILABLE)


@pytest.mark.parametrize("source,mode,runs", [
    ("verified_grants", "local_session", True),
    ("development", "local_session", False),
    ("verified_grants", "development_stub", False),
])
def test_renewal_runs_only_in_the_configuration_that_starts_it(source, mode, runs):
    assert renewal_runs(SimpleNamespace(identity_binding_source=source, websocket_auth_mode=mode)) is runs


def _application(store: SQLiteAuthenticationStore, clock: Clock) -> FastAPI:
    application = FastAPI()
    application.include_router(webauthn_endpoints.router)
    application.dependency_overrides[require_activated_runtime] = lambda: None
    application.dependency_overrides[get_runtime_policy] = lambda: store.build_runtime_policy()
    application.dependency_overrides[webauthn_endpoints.authentication_store] = lambda: store
    application.dependency_overrides[webauthn_endpoints.webauthn_authority_port] = (
        lambda: WebAuthnAuthorityPort(store, clock=clock))
    return application


@pytest.mark.parametrize("renewal_source,lapse,access", [
    ("verified_grants", timedelta(hours=2), "renewing"),
    ("development", timedelta(hours=2), "unavailable"),
    ("verified_grants", timedelta(days=31), "expired"),
])
def test_session_state_names_one_closed_access_state(monkeypatch, tmp_path, clock, renewal_source, lapse, access):
    monkeypatch.setattr(settings, "bridge_origin", ORIGIN)
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(settings, "identity_binding_source", renewal_source)
    store = _store(tmp_path, clock)
    clock.now += lapse
    with TestClient(_application(store, clock), base_url=ORIGIN) as client:
        response = client.get("/api/v1/webauthn/session-state", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 409
    assert response.json() == {"detail": "Enrollment state is unavailable", "access": access}
    assert response.headers["cache-control"] == "no-store"
    assert WebAuthnAuthorityResult.REQUIRED_GRANT_EXPIRED.value not in response.text


def test_session_state_reports_removed_access(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(settings, "bridge_origin", ORIGIN)
    store = _store(tmp_path, clock)
    store.revoke(RevocationKey(RevocationScopeType.INSTALLATION, INSTALLATION_ID), reason="test")
    clock.now += timedelta(hours=2)
    with TestClient(_application(store, clock), base_url=ORIGIN) as client:
        response = client.get("/api/v1/webauthn/session-state", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 409
    assert response.json() == {"detail": "Enrollment state is unavailable", "access": "removed"}


def test_session_state_for_current_authority_keeps_its_three_facts(monkeypatch, tmp_path, clock):
    monkeypatch.setattr(settings, "bridge_origin", ORIGIN)
    store = _store(tmp_path, clock)
    with TestClient(_application(store, clock), base_url=ORIGIN) as client:
        response = client.get("/api/v1/webauthn/session-state", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200
    assert response.json() == {"authenticated": False, "enrolled": False, "csrf_token": None}
