from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline, _auth_store
from app.analytics.pipeline import AnalyticsPipeline
from app.core.config import settings
from app.persistence.auth import (
    AuthorizedAccountBinding,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.security import local_sessions
from app.security.analysis_authorization import (
    clear_analysis_policies,
    require_cached_analysis_run,
)
from app.security.capability_license_verifier import (
    CapabilityLicenseAuthorityService,
    CapabilityLicenseVerificationContext,
    CapabilityLicenseVerifier,
    FixtureCapabilityLicenseTrustProvider,
)
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import AuthContext

VECTORS = Path(__file__).resolve().parents[1] / "contracts" / "capability-license-v1"
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "creator-1"
PRINCIPAL = "principal-1"


def _context() -> CapabilityLicenseVerificationContext:
    data = json.loads(
        (VECTORS / "accepted" / "verification-context.json").read_text("utf-8")
    )
    subject = data["expected_subject"]
    return CapabilityLicenseVerificationContext(
        expected_subject=subject,
        expected_organization_id=data["expected_organization_id"],
        expected_installation_id=data["expected_installation_id"],
        expected_installation_key_id=data["expected_installation_key_id"],
        expected_installation_key_jkt=data["expected_installation_key_jkt"],
        expected_seat_id=subject.split(":seat:", 1)[1].split(":capability:", 1)[0],
        expected_seat_scope=data["expected_seat_scope"],
        expected_capability=data["expected_capability"],
        expected_target_major=data["expected_target_major"],
        expected_artifact_family=data["expected_artifact_family"],
        requested_update_mode=data["requested_update_mode"],
        expected_fallback_major=data.get("expected_fallback_major"),
    )


def _authority(store: SQLiteAuthenticationStore) -> CapabilityLicenseAuthorityService:
    return CapabilityLicenseAuthorityService(
        store,
        CapabilityLicenseVerifier(FixtureCapabilityLicenseTrustProvider()),
        clock=lambda: NOW,
        verification_source="conformance",
    )


def _grant(
    kind: str,
    context: CapabilityLicenseVerificationContext,
) -> VerifiedGrantReference:
    reference_id = f"{kind}-restart"
    membership = kind == "membership_snapshot"
    binding = kind == "creator_account_binding"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"jti-{reference_id}",
        grant_type=kind,
        grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
        issuer="urn:bridge-clean:commercial-control-plane",
        subject=PRINCIPAL,
        installation_id=context.expected_installation_id,
        creator_account_id=ACCOUNT if binding else None,
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=365),
        verified_at=NOW - timedelta(minutes=1),
        organization_id=context.expected_organization_id,
        installation_key_id=context.expected_installation_key_id,
        installation_key_jkt=context.expected_installation_key_jkt,
        membership_id="membership-restart" if membership else None,
        approval_id="approval-restart" if binding else None,
        approval_revision=1 if binding else None,
        allowed_creator_account_ids=(ACCOUNT,) if membership else None,
        membership_roles=("owner",) if membership else None,
    )


def _seed_durable_authority(path: Path) -> None:
    context = _context()
    store = SQLiteAuthenticationStore(path, clock=lambda: NOW)
    grants = tuple(
        _grant(kind, context) for kind in sorted(ACCOUNT_AUTHORITY_GRANT_TYPES)
    )
    store.record_verified_grants(grants)

    association_request_id = "association-restart"
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id=association_request_id,
            installation_id=context.expected_installation_id,
            onboarding_transaction_id="onboarding-restart",
            organization_id=context.expected_organization_id,
            creator_account_id=ACCOUNT,
            state=ProvisioningCandidateState.PENDING,
            requested_at=NOW - timedelta(minutes=3),
        )
    )
    store.approve_provisioning_candidate(
        association_request_id,
        resolved_at=NOW - timedelta(minutes=2),
    )
    reference_ids = tuple(item.reference_id for item in grants)
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            creator_account_id=ACCOUNT,
            installation_id=context.expected_installation_id,
            platform_creator_id=ACCOUNT,
            association_request_id=association_request_id,
            grant_bundle_sha256=hashlib.sha256(
                "\n".join(sorted(reference_ids)).encode()
            ).hexdigest(),
            authorized_at=NOW - timedelta(minutes=1),
            grant_reference_ids=reference_ids,
        )
    )

    token = (VECTORS / "accepted" / "token.jws").read_text("ascii").strip()
    _authority(store).accept(token, context=context)


class _UnusedSource:
    def account_read_model(self, creator_account_id: str):
        raise AssertionError("analytics source should be replaced in this admission test")

    def account_exists(self, creator_account_id: str) -> bool:
        return False

    def account_revisions(self) -> list[tuple[str, int]]:
        return []


def test_restart_offline_reconstructs_analysis_policy_from_durable_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    auth_path = tmp_path / "auth.sqlite3"
    _seed_durable_authority(auth_path)

    # Simulate full Brain process loss: both process-local policy caches and
    # cached auth-store handles disappear. No hosted transport/control-plane
    # object is available during the restarted path.
    clear_analysis_policies()
    local_sessions._store.cache_clear()
    _auth_store.cache_clear()
    monkeypatch.setattr(settings, "auth_database_path", auth_path)

    identity = AuthContext(PRINCIPAL, ACCOUNT, "agent")
    policy = local_sessions.build_runtime_policy(identity)
    recovered = require_cached_analysis_run(
        SQLiteAuthenticationStore(auth_path),
        ACCOUNT,
    )

    assert policy.commercial_authority is not None
    assert recovered.commercial_authority is not None
    assert recovered.commercial_authority.reference_id == (
        policy.commercial_authority.reference_id
    )

    # Exercise the production LicensedAnalyticsPipeline entry boundary. The
    # analytics computation itself is replaced with a sentinel so this test
    # stays focused on restart/offline commercial admission.
    calls: list[str] = []

    def _build_candidate(self, creator_account_id: str, **kwargs):
        calls.append(creator_account_id)
        return "candidate-built"

    monkeypatch.setattr(AnalyticsPipeline, "build_candidate", _build_candidate)
    pipeline = LicensedAnalyticsPipeline(_UnusedSource())
    assert pipeline.build_candidate(ACCOUNT) == "candidate-built"
    assert calls == [ACCOUNT]
