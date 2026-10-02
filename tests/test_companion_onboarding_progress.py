"""Companion first-capture readiness through the production transport path."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]

from app.api.endpoints.companion_session import ProtectedSocket
from app.core.config import settings
from app.persistence.auth import (
    AuthorizedAccountBinding,
    BridgeSessionIssue,
    ClaimSubmission,
    InstallationKeyReference,
    InstallationKeyReservation,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.persistence.companion_pairing import CompanionPairingPersistence
from app.persistence.factory import create_canonical_repositories
from app.protocol.common import CapabilityStatus
from app.protocol.payloads import ConfigAppliedPayload
from app.provisioning.progress_reporting import OnboardingProgressCoordinator
from app.security.companion_session_authority import CompanionSessionAuthority
from app.transport.account_bound_manager import AccountBoundTransportManager

GRANT_TYPES = ("installation_grant", "creator_account_binding", "membership_snapshot")


@dataclass
class Clock:
    at: datetime

    def __call__(self):
        return self.at


def _grant(clock, kind):
    token = "header.payload." + kind
    return VerifiedGrantReference(
        reference_id=kind,
        grant_identifier="id-" + kind,
        grant_type=kind,
        grant_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
        issuer="issuer",
        subject="subject",
        installation_id="brain",
        creator_account_id="creator" if kind == "creator_account_binding" else None,
        valid_from=clock.at - timedelta(minutes=1),
        expires_at=clock.at + timedelta(hours=2),
        verified_at=clock.at,
        organization_id="organization",
        installation_key_id="key-1",
        installation_key_jkt="thumbprint-1",
        allowed_creator_account_ids=(
            ("creator",) if kind == "membership_snapshot" else None
        ),
        compact_jws=token,
    )


def _admitted_companion(path):
    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0))
    store = SQLiteAuthenticationStore(path, clock=clock)
    store.reserve_installation_key(
        InstallationKeyReservation("test", "test", "ES256", clock.at)
    )
    store.activate_installation_key(
        InstallationKeyReference(
            "test", "test", "ES256", "key-1", "thumbprint-1", "{}", clock.at, clock.at
        )
    )
    grants = tuple(_grant(clock, kind) for kind in GRANT_TYPES)
    store.record_verified_grants(grants)
    store.register_webauthn_credential(
        WebAuthnCredential(
            "credential",
            "operator",
            "issuer",
            "subject",
            "brain",
            b"public",
            0,
            clock.at,
        )
    )
    bridge = store.issue_bridge_session(
        BridgeSessionIssue(
            "credential",
            "operator",
            "creator",
            "operator",
            clock.at + timedelta(hours=1),
            tuple(grant.reference_id for grant in grants),
        )
    )
    store.record_claim_submission(
        ClaimSubmission("claim", "onboarding", "organization", "brain", clock.at)
    )
    assert store.resolve_claim_submission("claim", outcome=None, resolved_at=clock.at)
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            "association",
            "brain",
            "onboarding",
            "organization",
            "creator",
            ProvisioningCandidateState.PENDING,
            clock.at,
        )
    )
    assert store.approve_provisioning_candidate("association", resolved_at=clock.at)
    store.record_authorized_account_binding(
        AuthorizedAccountBinding(
            "creator",
            "brain",
            "platform-creator",
            "association",
            "a" * 64,
            clock.at,
            GRANT_TYPES,
        )
    )
    pairing = CompanionPairingPersistence(store)
    pairing.open_authorized_window(
        session_id=bridge.session_id,
        creator_account_id="creator",
        pairing_id=hashlib.sha256(b"first").digest(),
        opened_at=clock.at,
        expires_at=clock.at + timedelta(minutes=5),
        brain_nonce=b"n" * 32,
        brain_noise_public_key=b"b" * 32,
        wrapped_brain_noise_private_key=b"protected-test-noise-private-key",
    )
    window = pairing.claim_request()
    window = pairing.offer_window(
        window.pairing_id,
        expected_version=window.version,
        agent_installation_id=str(uuid4()),
        agent_identity_jwk='{"kty":"EC"}',
        agent_identity_thumbprint="agent-thumbprint",
        agent_noise_public_key=b"a" * 32,
        agent_nonce=b"q" * 32,
        pairing_digest=b"p" * 32,
        grant_digest=b"g" * 32,
        installation_grant_reference_id=window.installation_grant_reference_id,
        creator_account_binding_reference_id=window.creator_account_binding_reference_id,
        offered_at=clock.at,
    )
    window = pairing.await_confirmation(
        window.pairing_id, expected_version=window.version, at=clock.at
    )
    pin = pairing.confirm_and_admit(
        window.pairing_id,
        expected_version=window.version,
        confirmation_principal_id="operator",
        confirmation_session_id=bridge.session_id,
        confirmed_at=clock.at,
    )
    return store, pin, clock


@pytest.mark.parametrize("evidence", ["config.applied", "hello-echo"])
def test_companion_installation_queues_first_capture_ready_without_licence(
    tmp_path, monkeypatch, evidence
):
    path = tmp_path / "auth.sqlite3"
    store, pin, clock = _admitted_companion(path)
    monkeypatch.setattr(settings, "auth_database_path", str(path))
    delivered = []

    class Client:
        def report_onboarding_progress(self, event):
            delivered.append(event.milestone)
            return "delivered"

    coordinator = OnboardingProgressCoordinator(
        lambda: store, lambda _store: Client(), clock=clock
    )
    manager = AccountBoundTransportManager(
        create_canonical_repositories(), onboarding_progress=coordinator
    )
    authority = CompanionSessionAuthority(store)
    session = authority.authorize(authority.prepare(pin.pairing_id))
    socket = ProtectedSocket(
        SimpleNamespace(authority=session, websocket=SimpleNamespace(client=None))
    )
    binding = session.binding
    required = manager.required_config_document(binding.creator_account_id)

    async def connect():
        lease = await manager.bind_agent(
            socket,
            principal_id=binding.principal_id,
            creator_account_id=binding.creator_account_id,
            agent_installation_id=UUID(binding.agent_installation_id),
            agent_stream_id=uuid4(),
            applied_config_revision=(
                required.config_revision if evidence == "hello-echo" else None
            ),
        )
        if evidence == "config.applied":
            assert all(
                event.milestone != "first-capture-ready"
                for event in store.onboarding_progress_events()
            )
            await manager.record_config_applied(
                lease,
                ConfigAppliedPayload(
                    connection_id=lease.connection_id,
                    fencing_token=lease.fencing_token,
                    creator_account_id=lease.creator_account_id,
                    config_revision=required.config_revision,
                    digest=required.digest,
                    outcome="applied",
                    capabilities=[
                        CapabilityStatus(
                            capability="capture.messages", status="active", detail=None
                        )
                    ],
                ),
            )

    asyncio.run(connect())
    ready = [
        event
        for event in store.onboarding_progress_events()
        if event.milestone == "first-capture-ready"
    ]
    assert len(ready) == 1
    assert ready[0].occurred_at == clock.at
    with store.database.read() as connection:
        assert not connection.execute(
            "SELECT 1 FROM verified_grant_references WHERE grant_type = 'license_entitlement'"
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM capability_license_references"
        ).fetchone()
    coordinator.flush()
    assert "first-capture-ready" in delivered


def _refreshed(clock, kind):
    token = "header.payload." + kind + "-refreshed"
    return replace(
        _grant(clock, kind),
        reference_id=kind + "-refreshed",
        grant_identifier="id-" + kind + "-refreshed",
        grant_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
        verified_at=clock.at + timedelta(seconds=1),
        compact_jws=token,
    )


def _companion_transport(store, pin, clock, monkeypatch, path):
    monkeypatch.setattr(settings, "auth_database_path", str(path))
    coordinator = OnboardingProgressCoordinator(
        lambda: store, lambda _store: None, clock=clock
    )
    manager = AccountBoundTransportManager(
        create_canonical_repositories(), onboarding_progress=coordinator
    )
    authority = CompanionSessionAuthority(store)
    session = authority.authorize(authority.prepare(pin.pairing_id))
    socket = ProtectedSocket(
        SimpleNamespace(authority=session, websocket=SimpleNamespace(client=None))
    )
    return coordinator, manager, socket, session.binding


async def _bind(manager, socket, binding, applied_config_revision=None):
    return await manager.bind_agent(
        socket,
        principal_id=binding.principal_id,
        creator_account_id=binding.creator_account_id,
        agent_installation_id=UUID(binding.agent_installation_id),
        agent_stream_id=uuid4(),
        applied_config_revision=applied_config_revision,
    )


def _config_applied(lease, revision, digest, outcome="applied"):
    return ConfigAppliedPayload(
        connection_id=lease.connection_id,
        fencing_token=lease.fencing_token,
        creator_account_id=lease.creator_account_id,
        config_revision=revision,
        digest=digest,
        outcome=outcome,
        capabilities=[
            CapabilityStatus(
                capability="capture.messages",
                status="active" if outcome == "applied" else "degraded",
                detail=None if outcome == "applied" else "capture unavailable",
            )
        ],
    )


def _milestones(store):
    return [event.milestone for event in store.onboarding_progress_events()]


def test_companion_facts_survive_grant_refresh(tmp_path, monkeypatch):
    path = tmp_path / "auth.sqlite3"
    store, pin, clock = _admitted_companion(path)
    # An installation admitted before account-bound facts were queued.
    with store.database.transaction() as connection:
        connection.execute(
            "DELETE FROM onboarding_progress_outbox WHERE milestone = 'account-bound'"
        )
    for kind in ("installation_grant", "creator_account_binding"):
        store.replace_verified_grant(kind, _refreshed(clock, kind))
    coordinator, manager, socket, binding = _companion_transport(
        store, pin, clock, monkeypatch, path
    )
    assert binding.grant_reference_ids == (
        "installation_grant-refreshed",
        "creator_account_binding-refreshed",
    )
    required = manager.required_config_document(binding.creator_account_id)

    async def connect():
        await coordinator.start()
        await coordinator.stop()
        lease = await _bind(manager, socket, binding)
        await manager.record_config_applied(
            lease, _config_applied(lease, required.config_revision, required.digest)
        )

    asyncio.run(connect())
    assert _milestones(store) == [
        "installed",
        "enrolled",
        "account-bound",
        "first-capture-ready",
    ]


def test_echo_behind_the_required_revision_does_not_queue_readiness(
    tmp_path, monkeypatch
):
    path = tmp_path / "auth.sqlite3"
    store, pin, clock = _admitted_companion(path)
    _, manager, socket, binding = _companion_transport(
        store, pin, clock, monkeypatch, path
    )
    applied = manager.required_config_document(binding.creator_account_id)

    async def reconnect():
        required = await manager.publish_config(
            binding.creator_account_id,
            capture_policy=applied.capture_policy.model_dump(mode="json"),
            command_policy=applied.command_policy.model_dump(mode="json"),
            signal_available=False,
        )
        lease = await _bind(manager, socket, binding, applied.config_revision)
        await manager.heartbeat(lease, applied.config_revision)
        return required

    required = asyncio.run(reconnect())
    record = manager.config_authority.installation(
        binding.creator_account_id, UUID(binding.agent_installation_id)
    )
    assert (
        record.required_config_revision,
        record.applied_config_revision,
        record.last_failure,
    ) == (required.config_revision, applied.config_revision, None)
    assert "first-capture-ready" not in _milestones(store)


@pytest.mark.parametrize("report", ["degraded", "unknown-revision"])
def test_invalid_config_report_does_not_queue_readiness(tmp_path, monkeypatch, report):
    path = tmp_path / "auth.sqlite3"
    store, pin, clock = _admitted_companion(path)
    _, manager, socket, binding = _companion_transport(
        store, pin, clock, monkeypatch, path
    )
    required = manager.required_config_document(binding.creator_account_id)
    revision = required.config_revision if report == "degraded" else "config-999999"
    outcome = "degraded" if report == "degraded" else "applied"
    observations = []
    observe = manager._observe_companion_progress

    async def record_observation(lease, record, *, echoed_revision):
        observations.append(echoed_revision)
        await observe(lease, record, echoed_revision=echoed_revision)

    async def connect():
        lease = await _bind(manager, socket, binding)
        monkeypatch.setattr(manager, "_observe_companion_progress", record_observation)
        await manager.record_config_applied(
            lease, _config_applied(lease, revision, required.digest, outcome)
        )
        record = manager.config_authority.installation(
            binding.creator_account_id, UUID(binding.agent_installation_id)
        )
        assert record.applied_config_revision is None
        assert record.last_failure is not None
        assert "first-capture-ready" not in _milestones(store)
        assert observations == [None]
        await manager.record_config_applied(
            lease, _config_applied(lease, required.config_revision, required.digest)
        )

    asyncio.run(connect())
    assert observations == [None, required.config_revision]
    assert _milestones(store).count("first-capture-ready") == 1
