from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence.auth import (
    AuthenticationStateError,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.security.capability_license_verifier import (
    CapabilityLicenseAuthorityService,
    CapabilityLicenseVerificationContext,
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
)
from app.security.runtime_policy import (
    AnalysisRunContext,
    AuthContext,
    authorized_account,
    require_analysis_run,
)

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-license-v1"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "creator-1"


def _accepted() -> tuple[str, CapabilityLicenseVerificationContext]:
    case = VECTORS / "accepted"
    data = json.loads((case / "verification-context.json").read_text("utf-8"))
    subject = data["expected_subject"]
    seat_id = subject.split(":seat:", 1)[1].split(":capability:", 1)[0]
    context = CapabilityLicenseVerificationContext(
        expected_subject=subject,
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
        expected_seat_id=seat_id,
        expected_seat_scope=data["expected_seat_scope"],
        expected_capability=data["expected_capability"],
        expected_target_major=data["expected_target_major"],
        expected_artifact_family=data["expected_artifact_family"],
        requested_update_mode=data["requested_update_mode"],
        expected_fallback_major=data.get("expected_fallback_major"),
    )
    return (case / "token.jws").read_text("ascii").strip(), context


def _service(store: SQLiteAuthenticationStore) -> CapabilityLicenseAuthorityService:
    verifier = CapabilityLicenseVerifier(FixtureCapabilityLicenseTrustProvider())
    return CapabilityLicenseAuthorityService(
        store,
        verifier,
        clock=lambda: NOW,
        verification_source="conformance",
    )


def _grant(kind: str, context: CapabilityLicenseVerificationContext) -> VerifiedGrantReference:
    reference = f"{kind}-current"
    return VerifiedGrantReference(
        reference_id=reference,
        grant_identifier=f"jti-{reference}",
        grant_type=kind,
        grant_digest=hashlib.sha256(reference.encode()).hexdigest(),
        issuer="urn:bridge-clean:commercial-control-plane",
        subject="principal-1",
        installation_id=context.expected_installation_id,
        creator_account_id=ACCOUNT if kind == "creator_account_binding" else None,
        valid_from=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        verified_at=NOW - timedelta(minutes=1),
        organization_id=context.expected_organization_id,
        installation_key_id=context.expected_installation_key_id,
        installation_key_jkt=context.expected_installation_key_jkt,
        membership_id="membership-1" if kind == "membership_snapshot" else None,
        approval_id="approval-1" if kind == "creator_account_binding" else None,
        approval_revision=1 if kind == "creator_account_binding" else None,
        allowed_creator_account_ids=(ACCOUNT,) if kind == "membership_snapshot" else None,
        membership_roles=("owner",) if kind == "membership_snapshot" else None,
    )


def test_verified_capability_license_persists_idempotently_and_hides_jws(tmp_path: Path) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    token, context = _accepted()
    service = _service(store)

    first = service.accept(token, context=context)
    second = service.accept(token, context=context)
    durable = store.verified_capability_license(first.reference_id)

    assert second.reference_id == first.reference_id
    assert durable is not None
    assert durable.object_digest == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert durable.license_id == first.license_id
    assert durable.compact_jws == token
    assert token not in repr(durable)


def test_same_license_identifier_with_different_bytes_fails_closed(tmp_path: Path) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    token, context = _accepted()
    reference = _service(store).accept(token, context=context)
    conflicting_bytes = token + "x"
    conflicting = replace(
        reference,
        reference_id="caplic." + hashlib.sha256(conflicting_bytes.encode("ascii")).hexdigest(),
        compact_jws=conflicting_bytes,
        object_digest=hashlib.sha256(conflicting_bytes.encode("ascii")).hexdigest(),
    )

    with pytest.raises(AuthenticationStateError, match="different signed bytes"):
        store.record_verified_capability_license(conflicting)


def test_restart_reverifies_persisted_signed_bytes_before_runtime_use(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite3"
    token, context = _accepted()
    first_store = SQLiteAuthenticationStore(path, clock=lambda: NOW)
    reference = _service(first_store).accept(token, context=context)

    restarted = SQLiteAuthenticationStore(path, clock=lambda: NOW + timedelta(minutes=2))
    recovered = _service(restarted).recover(reference.reference_id, context=context)

    assert recovered.object_digest == reference.object_digest
    assert recovered.signer_kid == reference.signer_kid


def test_runtime_policy_composes_identity_and_commercial_authority_separately(tmp_path: Path) -> None:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: NOW)
    token, context = _accepted()
    grants = tuple(
        _grant(kind, context)
        for kind in ("installation_grant", "membership_snapshot", "creator_account_binding")
    )
    store.record_verified_grants(grants)
    reference = _service(store).accept(token, context=context)
    identity = AuthContext("principal-1", ACCOUNT, "creator")

    policy = _service(store).runtime_policy(
        identity,
        tuple(item.reference_id for item in grants),
        reference.reference_id,
        context=context,
    )
    selected = AnalysisRunContext(
        organization_id=context.expected_organization_id,
        installation_id=context.expected_installation_id,
        installation_key_id=context.expected_installation_key_id,
        installation_key_jkt=context.expected_installation_key_jkt,
        seat_id=context.expected_seat_id,
        seat_scope=context.expected_seat_scope,
        capability=context.expected_capability,
        selected_major_version=context.expected_target_major,
        artifact_family=context.expected_artifact_family,
        requires_update_rights=True,
    )

    require_analysis_run(policy, selected)
    assert policy.identity_authority is not None
    assert policy.commercial_authority is not None
    assert "license_entitlement" not in policy.identity_authority.grant_types
    assert authorized_account(policy, ACCOUNT) == ACCOUNT
