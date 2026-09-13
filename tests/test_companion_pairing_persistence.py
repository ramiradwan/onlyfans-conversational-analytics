from __future__ import annotations

import hashlib
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence import auth as auth_persistence
from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import (
    AgentChallengeBinding,
    AgentPairing,
    AuthenticationStateError,
    BridgeSessionIssue,
    InstallationKeyReference,
    InstallationKeyReservation,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    TicketPurpose,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.persistence.companion_pairing import (
    CompanionPairingCASMismatch,
    CompanionPairingConflict,
    CompanionPairingGenerationExhausted,
    CompanionPairingGrantUnavailable,
    CompanionPairingPersistence,
    CompanionPairingState,
    CompanionPairingStateError,
)
from app.security.runtime_policy import AuthContext


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def instant() -> datetime:
    return datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(instant: datetime) -> MutableClock:
    return MutableClock(instant)


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> SQLiteAuthenticationStore:
    authentication = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    _activate_installation_key(authentication, clock.value)
    return authentication


@pytest.fixture
def pairing(store: SQLiteAuthenticationStore) -> CompanionPairingPersistence:
    return CompanionPairingPersistence(store)


def _id(label: str) -> bytes:
    return hashlib.sha256(label.encode("utf-8")).digest()


def _activate_installation_key(
    store: SQLiteAuthenticationStore, at: datetime
) -> None:
    store.reserve_installation_key(
        InstallationKeyReservation("test-provider", "test-key", "ES256", at)
    )
    store.activate_installation_key(
        InstallationKeyReference(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            installation_key_id="installation-key-1",
            installation_key_jkt="installation-key-thumbprint",
            public_key_jwk='{"kty":"EC","crv":"P-256"}',
            created_at=at,
            activated_at=at,
        )
    )


def _check_protected_secret(actual: bytes | None, expected: bytes) -> None:
    if actual is None or not secrets.compare_digest(actual, expected):
        pytest.fail("protected candidate secret mismatch", pytrace=False)


def _grant(
    instant: datetime,
    *,
    reference_id: str,
    grant_type: str,
    retained: bool = True,
    installation_id: str = "brain-installation-1",
    creator_account_id: str | None = None,
) -> VerifiedGrantReference:
    token = f"header.payload.{reference_id}"
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"grant-{reference_id}",
        grant_type=grant_type,
        grant_digest=hashlib.sha256(
            token.encode("ascii") if retained else reference_id.encode("utf-8")
        ).hexdigest(),
        issuer="issuer.example",
        subject="customer-subject",
        installation_id=installation_id,
        creator_account_id=creator_account_id,
        valid_from=instant - timedelta(minutes=1),
        expires_at=instant + timedelta(hours=2),
        verified_at=instant - timedelta(minutes=1),
        compact_jws=token if retained else None,
        organization_id="organization-1",
        installation_key_id="installation-key-1",
        installation_key_jkt="installation-key-thumbprint",
    )


def _record_pairing_grants(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    *,
    retained: bool = True,
) -> tuple[str, str]:
    installation = _grant(
        instant,
        reference_id="installation-grant-1",
        grant_type="installation_grant",
        retained=retained,
    )
    binding = _grant(
        instant,
        reference_id="creator-binding-1",
        grant_type="creator_account_binding",
        retained=retained,
        creator_account_id="creator-1",
    )
    store.record_verified_grants((installation, binding))
    return installation.reference_id, binding.reference_id


def _open(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    *,
    label: str = "pairing-1",
    installation_id: str = "brain-installation-1",
    ttl: timedelta = timedelta(minutes=5),
):
    return pairing.open_window(
        pairing_id=_id(label),
        installation_id=installation_id,
        creator_account_id="creator-1",
        opened_at=clock.value,
        expires_at=clock.value + ttl,
        brain_nonce=b"n" * 32,
        brain_noise_public_key=b"b" * 32,
        wrapped_brain_noise_private_key=b"protected-brain-key",
    )


def _offer(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    window,
    grants: tuple[str, str],
):
    return pairing.offer_window(
        window.pairing_id,
        expected_version=window.version,
        agent_installation_id="agent-installation-1",
        agent_identity_jwk='{"kty":"EC","crv":"P-256","x":"x","y":"y"}',
        agent_identity_thumbprint="agent-identity-thumbprint",
        agent_noise_public_key=b"a" * 32,
        agent_nonce=b"q" * 32,
        pairing_digest=b"p" * 32,
        grant_digest=b"g" * 32,
        installation_grant_reference_id=grants[0],
        creator_account_binding_reference_id=grants[1],
        offered_at=clock.value,
    )


def _bridge_session(
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    *,
    account: str = "creator-1",
    principal: str = "principal-1",
    role: str = "operator",
    lifetime: timedelta = timedelta(minutes=10),
    issuer: str = "issuer.example",
    subject: str = "customer-subject",
    installation_id: str = "brain-installation-1",
) -> str:
    label = f"{account}-{principal}-{role}"
    references = tuple(
        replace(
            _grant(
                clock.value,
                reference_id=f"bridge-{label}-{grant_type}",
                grant_type=grant_type,
                creator_account_id=(account if grant_type == "creator_account_binding" else None),
                retained=grant_type != "membership_snapshot",
                installation_id=installation_id,
            ),
            issuer=issuer,
            subject=subject,
        )
        for grant_type in (
            "installation_grant", "membership_snapshot", "creator_account_binding"
        )
    )
    store.record_verified_grants(references)
    credential_id = f"credential-{label}"
    store.register_webauthn_credential(
        WebAuthnCredential(
            credential_id=credential_id,
            principal_id=principal,
            external_issuer=issuer,
            external_subject=subject,
            installation_id=installation_id,
            public_key=b"test-webauthn-public-key",
            signature_count=0,
            enrolled_at=clock.value,
        )
    )
    return store.issue_bridge_session(
        BridgeSessionIssue(
            credential_id=credential_id,
            principal_id=principal,
            creator_account_id=account,
            role=role,
            expires_at=clock.value + lifetime,
            grant_reference_ids=tuple(grant.reference_id for grant in references),
        )
    ).session_id


def _advance_to_awaiting(pairing, clock, opened, grants):
    offered = _offer(pairing, clock, opened, grants)
    return pairing.await_confirmation(
        offered.pairing_id,
        expected_version=offered.version,
        at=clock.value,
    )


def _advance_to_confirmed(store, pairing, clock, opened, grants):
    awaiting = _advance_to_awaiting(pairing, clock, opened, grants)
    return pairing.confirm_window(
        awaiting.pairing_id,
        expected_version=awaiting.version,
        confirmation_principal_id="principal-1",
        confirmation_session_id=_bridge_session(store, clock),
        confirmed_at=clock.value,
    )


def test_migrates_a_pre_0011_auth_database(
    tmp_path: Path, clock: MutableClock
) -> None:
    source = Path(auth_persistence.__file__).with_name("auth_sql")
    pre_0011 = tmp_path / "pre-0011"
    pre_0011.mkdir()
    for migration in source.glob("*.sql"):
        if int(migration.name[:4]) <= 10:
            shutil.copy2(migration, pre_0011 / migration.name)

    path = tmp_path / "auth.sqlite3"
    legacy = SQLiteAuthenticationStore(path, clock=clock, migrations_dir=pre_0011)
    with legacy.database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert (
            connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'companion_pairing_windows'
                """
            ).fetchone()
            is None
        )

    migrated = SQLiteAuthenticationStore(path, clock=clock)
    with migrated.database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 16
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "companion_pairing_generations",
            "companion_pairing_windows",
        } <= tables
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(agent_pairings)"
            ).fetchall()
        }
        assert {
            "pairing_generation",
            "pairing_digest",
            "agent_noise_static_public_key",
            "brain_noise_static_public_key",
            "protected_brain_noise_static_private_key",
            "confirmation_principal_id",
            "confirmation_session_id",
            "confirmed_at",
        } <= columns


def test_legacy_agent_pairing_challenge_behavior_is_unchanged(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    grants = _record_pairing_grants(store, instant, retained=False)
    legacy = AgentPairing(
        pairing_id="legacy-pairing-1",
        key_id="legacy-agent-key-1",
        principal_id="principal-1",
        creator_account_id="creator-1",
        agent_installation_id="agent-installation-1",
        external_issuer="issuer.example",
        external_subject="customer-subject",
        installation_id="brain-installation-1",
        public_key=b"legacy-agent-public-key",
        key_fingerprint="legacy-key-fingerprint",
        created_at=instant - timedelta(minutes=1),
        grant_reference_ids=grants,
    )
    store.register_agent_pairing(legacy)
    policy = store.build_runtime_policy(
        AuthContext(legacy.principal_id, legacy.creator_account_id, "agent")
    )
    assert store.activate_agent_pairing(policy, legacy.pairing_id) is True

    challenge = store.issue_agent_challenge(
        AgentChallengeBinding(
            principal_id=legacy.principal_id,
            pairing_id=legacy.pairing_id,
            request_method="GET",
            request_path="/api/v1/agent/config",
            request_body_digest="body-digest",
            ticket_purpose=TicketPurpose.AGENT_CONFIG,
            agent_installation_id=legacy.agent_installation_id,
            creator_account_id=legacy.creator_account_id,
            key_id=legacy.key_id,
            brain_audience=legacy.installation_id,
        ),
        expires_at=instant + timedelta(minutes=2),
    )
    assert challenge.challenge_id

    with store.database.read() as connection:
        row = connection.execute(
            "SELECT * FROM agent_pairings WHERE pairing_id = ?",
            (legacy.pairing_id,),
        ).fetchone()
    assert row is not None
    assert row["pairing_generation"] is None
    assert row["pairing_digest"] is None
    assert row["agent_noise_static_public_key"] is None
    assert row["brain_noise_static_public_key"] is None
    assert row["protected_brain_noise_static_private_key"] is None
    assert row["confirmation_principal_id"] is None
    assert row["confirmed_at"] is None


def test_staging_window_never_authorizes_an_agent_challenge(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    window = _open(pairing, clock)
    with pytest.raises(AuthenticationStateError):
        store.issue_agent_challenge(
            AgentChallengeBinding(
                principal_id="principal-1",
                pairing_id=window.pairing_id.hex(),
                request_method="GET",
                request_path="/api/v1/agent/config",
                request_body_digest="body-digest",
                ticket_purpose=TicketPurpose.AGENT_CONFIG,
                agent_installation_id="agent-installation-1",
                creator_account_id="creator-1",
                key_id="agent-key-1",
                brain_audience="brain-installation-1",
            ),
            expires_at=clock.value + timedelta(minutes=1),
        )


def test_first_and_subsequent_generation_allocation_is_monotonic(
    pairing: CompanionPairingPersistence,
) -> None:
    assert pairing.allocate_generation("installation-1") == 1
    assert pairing.allocate_generation("installation-1") == 2
    assert pairing.allocate_generation("installation-1") == 3
    assert pairing.highest_generation("installation-1") == 3


def test_cancelled_generation_is_never_reused(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    first = _open(pairing, clock, label="cancelled", installation_id="installation-1")
    cancelled = pairing.terminate_window(
        first.pairing_id,
        expected_version=0,
        expected_state=CompanionPairingState.OPEN,
        terminal_state=CompanionPairingState.CANCELLED,
        at=clock.value,
        reason="cancelled",
    )
    assert cancelled.generation == 1
    second = _open(
        pairing, clock, label="replacement", installation_id="installation-1"
    )
    assert second.generation == 2


def test_expired_window_can_be_replaced_without_reusing_generation(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    first = _open(
        pairing,
        clock,
        label="expired",
        installation_id="installation-1",
        ttl=timedelta(minutes=1),
    )
    clock.value += timedelta(minutes=2)

    second = _open(
        pairing, clock, label="replacement", installation_id="installation-1"
    )
    expired = pairing.window(first.pairing_id)

    assert expired is not None
    assert expired.state is CompanionPairingState.EXPIRED
    assert expired.wrapped_brain_noise_private_key is None
    assert second.generation == 2


def test_concurrent_generation_allocation_cannot_duplicate(
    pairing: CompanionPairingPersistence,
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        generations = list(
            pool.map(lambda _: pairing.allocate_generation("installation-1"), range(16))
        )
    assert sorted(generations) == list(range(1, 17))


def test_generation_high_water_survives_pairing_revocation(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    first = _open(pairing, clock, label="revoked", installation_id="installation-1")
    pairing.terminate_window(
        first.pairing_id,
        expected_version=0,
        expected_state=CompanionPairingState.OPEN,
        terminal_state=CompanionPairingState.REVOKED,
        at=clock.value,
        reason="revoked",
    )
    assert pairing.highest_generation("installation-1") == 1
    second = _open(pairing, clock, label="next", installation_id="installation-1")
    assert second.generation == 2


def test_second_live_window_creation_is_refused_without_burning_generation(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    _open(pairing, clock, label="first", installation_id="installation-1")
    with pytest.raises(CompanionPairingConflict):
        _open(pairing, clock, label="second", installation_id="installation-1")
    assert pairing.highest_generation("installation-1") == 1


def test_generation_rollback_is_blocked_by_the_database(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
) -> None:
    assert pairing.allocate_generation("installation-1") == 1
    assert pairing.allocate_generation("installation-1") == 2
    with pytest.raises(sqlite3.IntegrityError):
        with store.database.transaction() as connection:
            connection.execute(
                """
                UPDATE companion_pairing_generations
                SET highest_generation = 1
                WHERE installation_id = 'installation-1'
                """
            )
    assert pairing.highest_generation("installation-1") == 2


def test_generation_exhaustion_fails_closed(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
) -> None:
    with store.database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO companion_pairing_generations(
                installation_id, highest_generation
            ) VALUES (?, ?)
            """,
            ("installation-1", (1 << 53) - 1),
        )
    with pytest.raises(CompanionPairingGenerationExhausted):
        pairing.allocate_generation("installation-1")


def test_stale_cas_is_refused(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    window = _open(pairing, clock)
    with pytest.raises(CompanionPairingCASMismatch):
        pairing.offer_window(
            window.pairing_id,
            expected_version=1,
            agent_installation_id="agent-installation-1",
            agent_identity_jwk='{"kty":"EC"}',
            agent_identity_thumbprint="thumbprint",
            agent_noise_public_key=b"a" * 32,
            agent_nonce=b"q" * 32,
            pairing_digest=b"p" * 32,
            grant_digest=b"g" * 32,
            installation_grant_reference_id="missing-installation",
            creator_account_binding_reference_id="missing-binding",
            offered_at=clock.value,
        )


def test_database_rejects_non_cas_version_advance(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    window = _open(pairing, clock)
    with pytest.raises(sqlite3.IntegrityError):
        with store.database.transaction() as connection:
            connection.execute(
                """
                UPDATE companion_pairing_windows
                SET state = 'cancelled',
                    version = version + 2,
                    wrapped_brain_noise_private_key = NULL,
                    terminal_at = ?
                WHERE pairing_id = ?
                """,
                (clock.value.isoformat(timespec="microseconds"), window.pairing_id),
            )


def test_cancellation_beats_stale_confirmation(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    instant: datetime,
) -> None:
    grants = _record_pairing_grants(store, instant)
    opened = _open(pairing, clock)
    offered = _offer(pairing, clock, opened, grants)
    awaiting = pairing.await_confirmation(
        offered.pairing_id,
        expected_version=offered.version,
        at=clock.value,
    )
    cancelled = pairing.terminate_window(
        awaiting.pairing_id,
        expected_version=awaiting.version,
        expected_state=CompanionPairingState.AWAITING_CONFIRMATION,
        terminal_state=CompanionPairingState.CANCELLED,
        at=clock.value,
        reason="cancelled",
    )
    assert cancelled.state is CompanionPairingState.CANCELLED

    with pytest.raises(CompanionPairingCASMismatch):
        pairing.confirm_window(
            awaiting.pairing_id,
            expected_version=awaiting.version,
            confirmation_principal_id="principal-1",
            confirmation_session_id="session-1",
            confirmed_at=clock.value,
        )


def test_revocation_beats_stale_offer_update(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
) -> None:
    opened = _open(pairing, clock)
    revoked = pairing.terminate_window(
        opened.pairing_id,
        expected_version=opened.version,
        expected_state=CompanionPairingState.OPEN,
        terminal_state=CompanionPairingState.REVOKED,
        at=clock.value,
        reason="revoked",
    )
    assert revoked.state is CompanionPairingState.REVOKED

    with pytest.raises(CompanionPairingCASMismatch):
        pairing.offer_window(
            opened.pairing_id,
            expected_version=opened.version,
            agent_installation_id="agent-installation-1",
            agent_identity_jwk='{"kty":"EC"}',
            agent_identity_thumbprint="thumbprint",
            agent_noise_public_key=b"a" * 32,
            agent_nonce=b"q" * 32,
            pairing_digest=b"p" * 32,
            grant_digest=b"g" * 32,
            installation_grant_reference_id="missing-installation",
            creator_account_binding_reference_id="missing-binding",
            offered_at=clock.value,
        )


@pytest.mark.parametrize(
    "terminal_state",
    [
        CompanionPairingState.DECLINED,
        CompanionPairingState.CANCELLED,
        CompanionPairingState.EXPIRED,
        CompanionPairingState.REVOKED,
    ],
)
def test_candidate_secret_is_cleared_on_non_confirmed_terminal_states(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    terminal_state: CompanionPairingState,
) -> None:
    ttl = (
        timedelta(minutes=1)
        if terminal_state is CompanionPairingState.EXPIRED
        else timedelta(minutes=5)
    )
    opened = _open(pairing, clock, ttl=ttl)
    if terminal_state is CompanionPairingState.EXPIRED:
        clock.value += timedelta(minutes=2)

    terminal = pairing.terminate_window(
        opened.pairing_id,
        expected_version=opened.version,
        expected_state=CompanionPairingState.OPEN,
        terminal_state=terminal_state,
        at=clock.value,
        reason=terminal_state.value,
    )
    assert terminal.wrapped_brain_noise_private_key is None


def test_restart_preserves_active_window_version_and_high_water(
    tmp_path: Path,
    clock: MutableClock,
    instant: datetime,
) -> None:
    path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(path, clock=clock)
    _activate_installation_key(store, clock.value)
    persistence = CompanionPairingPersistence(store)
    grants = _record_pairing_grants(store, instant)
    offered = _offer(persistence, clock, _open(persistence, clock), grants)

    restarted_store = SQLiteAuthenticationStore(path, clock=clock)
    restarted = CompanionPairingPersistence(restarted_store)
    restored = restarted.window(offered.pairing_id)

    assert restored is not None
    assert restored.state is CompanionPairingState.OFFERED
    assert restored.version == offered.version == 1
    _check_protected_secret(restored.wrapped_brain_noise_private_key, b"protected-brain-key")
    assert restarted.highest_generation("brain-installation-1") == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pairing_id", b"x" * 31),
        ("brain_nonce", b"x" * 31),
        ("brain_noise_public_key", b"x" * 33),
        ("wrapped_brain_noise_private_key", b""),
    ],
)
def test_open_window_refuses_malformed_crypto_lengths(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    field: str,
    value: bytes,
) -> None:
    arguments = {
        "pairing_id": _id("pairing-1"),
        "installation_id": "brain-installation-1",
        "creator_account_id": "creator-1",
        "opened_at": clock.value,
        "expires_at": clock.value + timedelta(minutes=5),
        "brain_nonce": b"n" * 32,
        "brain_noise_public_key": b"b" * 32,
        "wrapped_brain_noise_private_key": b"protected",
    }
    arguments[field] = value
    with pytest.raises(ValueError):
        pairing.open_window(**arguments)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_noise_public_key", b"x" * 31),
        ("agent_nonce", b"x" * 33),
        ("pairing_digest", b"x" * 31),
        ("grant_digest", b"x" * 33),
    ],
)
def test_offer_refuses_malformed_crypto_lengths(
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    field: str,
    value: bytes,
) -> None:
    opened = _open(pairing, clock)
    arguments = {
        "expected_version": opened.version,
        "agent_installation_id": "agent-installation-1",
        "agent_identity_jwk": '{"kty":"EC"}',
        "agent_identity_thumbprint": "thumbprint",
        "agent_noise_public_key": b"a" * 32,
        "agent_nonce": b"q" * 32,
        "pairing_digest": b"p" * 32,
        "grant_digest": b"g" * 32,
        "installation_grant_reference_id": "installation-grant",
        "creator_account_binding_reference_id": "binding-grant",
        "offered_at": clock.value,
    }
    arguments[field] = value
    with pytest.raises(ValueError):
        pairing.offer_window(opened.pairing_id, **arguments)


def test_offer_requires_pairing_eligible_retained_grant_references(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    instant: datetime,
) -> None:
    stale = _record_pairing_grants(store, instant, retained=False)
    opened = _open(pairing, clock)

    with pytest.raises(CompanionPairingGrantUnavailable):
        _offer(pairing, clock, opened, stale)

    assert pairing.window(opened.pairing_id).version == 0


def test_refresh_cannot_substitute_a_frozen_pairing_grant(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    instant: datetime,
) -> None:
    grants = _record_pairing_grants(store, instant)
    offered = _offer(pairing, clock, _open(pairing, clock), grants)
    replacement = _grant(
        instant,
        reference_id="installation-grant-2",
        grant_type="installation_grant",
        retained=True,
    )
    store.replace_verified_grant(grants[0], replacement)

    with pytest.raises(CompanionPairingCASMismatch):
        pairing.await_confirmation(
            offered.pairing_id,
            expected_version=offered.version,
            at=clock.value,
        )

    frozen = pairing.window(offered.pairing_id)
    assert frozen is not None
    assert frozen.version == offered.version + 1
    assert frozen.state is CompanionPairingState.REVOKED
    assert frozen.wrapped_brain_noise_private_key is None
    assert frozen.installation_grant_reference_id == grants[0]


def test_revoked_frozen_grant_blocks_later_confirmation_state(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    instant: datetime,
) -> None:
    grants = _record_pairing_grants(store, instant)
    offered = _offer(pairing, clock, _open(pairing, clock), grants)
    store.revoke(
        RevocationKey(RevocationScopeType.VERIFIED_GRANT, grants[1]),
        reason="revoked",
    )

    with pytest.raises(CompanionPairingCASMismatch):
        pairing.await_confirmation(
            offered.pairing_id,
            expected_version=offered.version,
            at=clock.value,
        )


def test_full_staging_lifecycle_persists_without_admitting_agent(
    store: SQLiteAuthenticationStore,
    pairing: CompanionPairingPersistence,
    clock: MutableClock,
    instant: datetime,
) -> None:
    grants = _record_pairing_grants(store, instant)
    opened = _open(pairing, clock)
    offered = _offer(pairing, clock, opened, grants)
    awaiting = pairing.await_confirmation(
        offered.pairing_id,
        expected_version=offered.version,
        at=clock.value,
    )
    confirmation_session = _bridge_session(store, clock)
    confirmed = pairing.confirm_window(
        awaiting.pairing_id,
        expected_version=awaiting.version,
        confirmation_principal_id="principal-1",
        confirmation_session_id=confirmation_session,
        confirmed_at=clock.value,
    )

    assert confirmed.state is CompanionPairingState.CONFIRMED
    assert confirmed.version == 3
    assert confirmed.generation == 1
    assert confirmed.pairing_digest == b"p" * 32
    assert confirmed.grant_digest == b"g" * 32
    assert confirmed.installation_grant_reference_id == grants[0]
    assert confirmed.creator_account_binding_reference_id == grants[1]
    assert confirmed.confirmation_principal_id == "principal-1"
    assert confirmed.confirmation_session_id == confirmation_session
    _check_protected_secret(confirmed.wrapped_brain_noise_private_key, b"protected-brain-key")

    with store.database.read() as connection:
        count = connection.execute("SELECT COUNT(*) FROM agent_pairings").fetchone()[0]
        assert count == 0


def test_pairing_window_cannot_exceed_300_seconds(pairing, clock) -> None:
    with pytest.raises(ValueError):
        _open(pairing, clock, ttl=timedelta(seconds=300, microseconds=1))
    assert pairing.highest_generation("brain-installation-1") is None
    assert _open(pairing, clock, ttl=timedelta(seconds=300)).generation == 1


def test_generation_bound_matches_javascript_exact_integer_range(store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with store.database.transaction() as connection:
            connection.execute(
                "INSERT INTO companion_pairing_generations VALUES (?, ?)",
                ("out-of-range-installation", 1 << 53),
            )


def test_candidate_secret_is_excluded_from_diagnostics(pairing, clock, caplog) -> None:
    window = _open(pairing, clock)
    secret_field = next(
        item for item in fields(window)
        if item.name == "wrapped_brain_noise_private_key"
    )
    assert secret_field.repr is False
    assert secret_field.compare is False
    if "protected-brain-key" in repr(window) + caplog.text:
        pytest.fail("protected candidate secret exposed", pytrace=False)
    if window != replace(window, wrapped_brain_noise_private_key=b"other-protected-key"):
        pytest.fail("candidate equality expanded protected material", pytrace=False)


@pytest.mark.parametrize("transition", ["open", "offer", "await", "confirm"])
def test_deadline_is_rechecked_after_acquiring_transaction(
    store, pairing, clock, instant, monkeypatch, transition
) -> None:
    grants = _record_pairing_grants(store, instant)
    session_id = _bridge_session(store, clock)
    window = None
    if transition != "open":
        window = _open(pairing, clock)
    if transition in {"await", "confirm"}:
        window = _offer(pairing, clock, window, grants)
    if transition == "confirm":
        window = pairing.await_confirmation(
            window.pairing_id, expected_version=window.version, at=clock.value
        )
    expires_at = instant + timedelta(seconds=300)
    transaction = store.database.transaction

    @contextmanager
    def transaction_after_wait(**kwargs):
        with transaction(**kwargs) as connection:
            clock.value = expires_at
            yield connection

    monkeypatch.setattr(store.database, "transaction", transaction_after_wait)
    with pytest.raises(ValueError if transition == "open" else CompanionPairingStateError):
        if transition == "open":
            _open(pairing, clock)
        elif transition == "offer":
            _offer(pairing, clock, window, grants)
        elif transition == "await":
            pairing.await_confirmation(
                window.pairing_id, expected_version=window.version, at=instant
            )
        else:
            pairing.confirm_window(
                window.pairing_id,
                expected_version=window.version,
                confirmation_principal_id="principal-1",
                confirmation_session_id=session_id,
                confirmed_at=instant,
            )
    if window is None:
        assert pairing.highest_generation("brain-installation-1") is None
    else:
        assert pairing.window(window.pairing_id).version == window.version


@pytest.mark.parametrize("grant_index", [0, 1], ids=["installation", "account-binding"])
@pytest.mark.parametrize(
    "identity_field",
    ["issuer", "subject", "organization_id", "installation_key_id", "installation_key_jkt"],
)
@pytest.mark.parametrize("mutation", ["missing", "mismatch"])
def test_pairing_rejects_incomplete_or_inconsistent_grant_identity(
    store, pairing, clock, instant, grant_index, identity_field, mutation
) -> None:
    installation = _grant(
        instant, reference_id="installation-grant-1", grant_type="installation_grant"
    )
    binding = _grant(
        instant, reference_id="creator-binding-1", grant_type="creator_account_binding",
        creator_account_id="creator-1",
    )
    grants = [installation, binding]
    value = "different-identity" if mutation == "mismatch" else (
        "" if identity_field in {"issuer", "subject"} else None
    )
    grants[grant_index] = replace(grants[grant_index], **{identity_field: value})
    store.record_verified_grants(tuple(grants))
    window = _open(pairing, clock)
    with pytest.raises(CompanionPairingGrantUnavailable):
        _offer(pairing, clock, window, tuple(grant.reference_id for grant in grants))
    assert pairing.window(window.pairing_id).state is CompanionPairingState.OPEN


@pytest.mark.parametrize("key_field", ["installation_key_id", "installation_key_jkt"])
def test_pairing_rejects_grants_bound_to_another_installation_key(
    store, pairing, clock, instant, key_field
) -> None:
    grants = (
        _grant(instant, reference_id="other-installation", grant_type="installation_grant"),
        _grant(
            instant, reference_id="other-binding", grant_type="creator_account_binding",
            creator_account_id="creator-1",
        ),
    )
    store.record_verified_grants(tuple(
        replace(grant, **{key_field: "other-active-key"}) for grant in grants
    ))
    with pytest.raises(CompanionPairingGrantUnavailable):
        _offer(pairing, clock, _open(pairing, clock), tuple(g.reference_id for g in grants))


def test_pairing_requires_an_activated_installation_key(tmp_path, clock, instant) -> None:
    authentication = SQLiteAuthenticationStore(tmp_path / "unactivated.sqlite3", clock=clock)
    persistence = CompanionPairingPersistence(authentication)
    grants = _record_pairing_grants(authentication, instant)
    opened = _open(persistence, clock)
    with pytest.raises(CompanionPairingGrantUnavailable):
        _offer(persistence, clock, opened, grants)


@pytest.mark.parametrize(
    "terminal_state", [CompanionPairingState.CANCELLED, CompanionPairingState.REVOKED]
)
def test_confirmed_candidate_can_be_invalidated_before_admission(
    store, pairing, clock, instant, terminal_state
) -> None:
    grants = _record_pairing_grants(store, instant)
    confirmed = _advance_to_confirmed(store, pairing, clock, _open(pairing, clock), grants)
    invalidated = pairing.terminate_window(
        confirmed.pairing_id,
        expected_version=confirmed.version,
        expected_state=CompanionPairingState.CONFIRMED,
        terminal_state=terminal_state,
        at=clock.value,
    )
    assert invalidated.version == confirmed.version + 1
    assert invalidated.state is terminal_state
    assert invalidated.wrapped_brain_noise_private_key is None
    assert invalidated.confirmation_principal_id is None
    assert invalidated.confirmation_session_id is None
    assert invalidated.confirmed_at is None
    assert pairing.highest_generation("brain-installation-1") == confirmed.generation
    with pytest.raises(CompanionPairingCASMismatch):
        pairing.terminate_window(
            confirmed.pairing_id,
            expected_version=confirmed.version,
            expected_state=CompanionPairingState.CONFIRMED,
            terminal_state=terminal_state,
            at=clock.value,
        )


@pytest.mark.parametrize("stage", ["open", "offered", "awaiting", "confirmed"])
@pytest.mark.parametrize("scope", ["installation", "account", "grant"])
def test_auth_revocation_invalidates_affected_staging_in_same_transaction(
    store, pairing, clock, instant, stage, scope
) -> None:
    grants = _record_pairing_grants(store, instant)
    window = _open(pairing, clock)
    if stage == "confirmed":
        window = _advance_to_confirmed(store, pairing, clock, window, grants)
    elif stage == "awaiting":
        window = _advance_to_awaiting(pairing, clock, window, grants)
    elif stage == "offered":
        window = _offer(pairing, clock, window, grants)
    key = {
        "installation": RevocationKey(RevocationScopeType.INSTALLATION, "brain-installation-1"),
        "account": RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, "creator-1"),
        "grant": RevocationKey(RevocationScopeType.VERIFIED_GRANT, grants[1]),
    }[scope]
    store.revoke(key, reason="revoked")
    invalidated = pairing.window(window.pairing_id)
    if stage == "open" and scope == "grant":
        # No frozen references exist until an offer has been constructed.
        assert invalidated.state is CompanionPairingState.OPEN
        assert invalidated.version == window.version
        with pytest.raises(CompanionPairingGrantUnavailable):
            _offer(pairing, clock, invalidated, grants)
        return
    assert invalidated.state is CompanionPairingState.REVOKED
    assert invalidated.version == window.version + 1
    assert invalidated.wrapped_brain_noise_private_key is None
    assert invalidated.confirmation_principal_id is None
    assert invalidated.confirmation_session_id is None
    assert invalidated.confirmed_at is None
    assert pairing.highest_generation(window.installation_id) == window.generation


@pytest.mark.parametrize("role", ["creator", "operator"])
def test_current_bridge_identity_can_confirm_its_own_account(
    store, pairing, clock, instant, role
) -> None:
    grants = _record_pairing_grants(store, instant)
    awaiting = _advance_to_awaiting(pairing, clock, _open(pairing, clock), grants)
    session_id = _bridge_session(store, clock, role=role)
    confirmed = pairing.confirm_window(
        awaiting.pairing_id, expected_version=awaiting.version,
        confirmation_principal_id="principal-1", confirmation_session_id=session_id,
        confirmed_at=clock.value,
    )
    assert confirmed.state is CompanionPairingState.CONFIRMED


@pytest.mark.parametrize("refusal", ["missing", "expired", "revoked", "wrong-account", "wrong-principal"])
def test_confirmation_rechecks_bridge_session_authority(
    store, pairing, clock, instant, refusal
) -> None:
    grants = _record_pairing_grants(store, instant)
    awaiting = _advance_to_awaiting(pairing, clock, _open(pairing, clock), grants)
    session_id = "missing-session"
    if refusal != "missing":
        session_id = _bridge_session(
            store, clock,
            account="creator-2" if refusal == "wrong-account" else "creator-1",
            lifetime=timedelta(seconds=1) if refusal == "expired" else timedelta(minutes=10),
        )
    if refusal == "expired":
        clock.value += timedelta(seconds=1)
    elif refusal == "revoked":
        store.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, session_id))
    with pytest.raises(CompanionPairingStateError):
        pairing.confirm_window(
            awaiting.pairing_id, expected_version=awaiting.version,
            confirmation_principal_id="other-principal" if refusal == "wrong-principal" else "principal-1",
            confirmation_session_id=session_id, confirmed_at=clock.value,
        )
    assert pairing.window(awaiting.pairing_id).version == awaiting.version


@pytest.mark.parametrize("identity_field", ["issuer", "subject", "installation_id"])
def test_confirmation_rejects_a_bridge_session_for_another_installation_identity(
    store, pairing, clock, instant, identity_field
) -> None:
    grants = _record_pairing_grants(store, instant)
    awaiting = _advance_to_awaiting(pairing, clock, _open(pairing, clock), grants)
    session_id = _bridge_session(store, clock, **{identity_field: "other-identity"})
    with pytest.raises(CompanionPairingStateError):
        pairing.confirm_window(
            awaiting.pairing_id,
            expected_version=awaiting.version,
            confirmation_principal_id="principal-1",
            confirmation_session_id=session_id,
            confirmed_at=clock.value,
        )
    assert pairing.window(awaiting.pairing_id).version == awaiting.version


@pytest.mark.parametrize("scope", ["credential", "principal", "session", "session-grant"])
def test_loss_of_confirming_authority_clears_confirmed_candidate(
    store, pairing, clock, instant, scope
) -> None:
    grants = _record_pairing_grants(store, instant)
    confirmed = _advance_to_confirmed(store, pairing, clock, _open(pairing, clock), grants)
    key = {
        "credential": RevocationKey(
            RevocationScopeType.WEBAUTHN_CREDENTIAL,
            "credential-creator-1-principal-1-operator",
        ),
        "principal": RevocationKey(RevocationScopeType.PRINCIPAL, "principal-1"),
        "session": RevocationKey(
            RevocationScopeType.BRIDGE_SESSION, confirmed.confirmation_session_id
        ),
        "session-grant": RevocationKey(
            RevocationScopeType.VERIFIED_GRANT,
            "bridge-creator-1-principal-1-operator-membership_snapshot",
        ),
    }[scope]
    store.revoke(key)
    invalidated = pairing.window(confirmed.pairing_id)
    assert invalidated.state is CompanionPairingState.REVOKED
    assert invalidated.version == confirmed.version + 1
    assert invalidated.wrapped_brain_noise_private_key is None
    assert invalidated.confirmation_principal_id is None
    assert invalidated.confirmation_session_id is None
    assert invalidated.confirmed_at is None


def test_failed_revocation_rolls_back_candidate_erasure(
    store, pairing, clock, instant, monkeypatch
) -> None:
    grants = _record_pairing_grants(store, instant)
    offered = _offer(pairing, clock, _open(pairing, clock), grants)

    def fail_epoch_update(connection):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(store, "_increment_authorization_epoch", fail_epoch_update)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, grants[0]))
    restored = pairing.window(offered.pairing_id)
    assert restored.state is CompanionPairingState.OFFERED
    assert restored.version == offered.version
    _check_protected_secret(restored.wrapped_brain_noise_private_key, b"protected-brain-key")
    assert store.verified_grant(grants[0]).compact_jws is not None
    assert store.revocation_version(RevocationKey(RevocationScopeType.VERIFIED_GRANT, grants[0])) == 0


@pytest.mark.parametrize(
    "revocation_key",
    [
        RevocationKey(RevocationScopeType.INSTALLATION, "brain-installation-1"),
        RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, "creator-1"),
    ],
    ids=["installation", "creator-account"],
)
def test_fresh_grants_cannot_restore_a_revoked_pairing_scope(
    store, pairing, clock, instant, revocation_key
) -> None:
    store.revoke(revocation_key)
    grants = _record_pairing_grants(store, instant)
    assert store.companion_pairing_grants_are_eligible(grants) is True
    opened = _open(pairing, clock)
    with pytest.raises(CompanionPairingGrantUnavailable):
        _offer(pairing, clock, opened, grants)
    unchanged = pairing.window(opened.pairing_id)
    assert unchanged.state is CompanionPairingState.OPEN
    assert unchanged.version == opened.version
