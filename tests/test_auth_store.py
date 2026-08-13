from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

from app.persistence.auth import (
    AgentChallengeBinding,
    AgentPairing,
    AuthenticationStateError,
    BridgeSessionIssue,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    TicketBinding,
    TicketIssue,
    TicketPurpose,
    VerifiedGrantReference,
    WebAuthnChallengeBinding,
    WebAuthnCredential,
)
from app.security.runtime_policy import (
    AuthContext,
    RuntimePolicy,
    StaleRuntimePolicyError,
)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def instant() -> datetime:
    return datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(instant: datetime) -> MutableClock:
    return MutableClock(instant)


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)


def record_grant(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    *,
    reference_id: str = "grant-ref-1",
    grant_type: str = "creator_account_binding",
    issuer: str = "issuer.example",
    subject: str = "customer-subject",
    installation_id: str = "brain-installation-1",
    creator_account_id: str | None = "creator-1",
) -> str:
    store.record_verified_grant(
        VerifiedGrantReference(
            reference_id=reference_id,
            grant_identifier=f"grant-id-{reference_id}",
            grant_type=grant_type,
            grant_digest=hashlib.sha256(reference_id.encode()).hexdigest(),
            issuer=issuer,
            subject=subject,
            installation_id=installation_id,
            creator_account_id=creator_account_id,
            valid_from=instant - timedelta(minutes=1),
            expires_at=instant + timedelta(hours=2),
            verified_at=instant - timedelta(minutes=1),
        )
    )
    return reference_id


def register_credential(
    store: SQLiteAuthenticationStore, instant: datetime
) -> WebAuthnCredential:
    credential = WebAuthnCredential(
        credential_id="credential-1",
        principal_id="principal-1",
        external_issuer="issuer.example",
        external_subject="customer-subject",
        installation_id="brain-installation-1",
        public_key=b"public-key-material",
        signature_count=4,
        enrolled_at=instant - timedelta(days=1),
    )
    store.register_webauthn_credential(credential)
    return credential


def issue_session(
    store: SQLiteAuthenticationStore, instant: datetime
):
    grants = (
        record_grant(
            store,
            instant,
            reference_id="installation-grant-1",
            grant_type="installation_grant",
            creator_account_id=None,
        ),
        record_grant(
            store,
            instant,
            reference_id="membership-grant-1",
            grant_type="membership_snapshot",
            creator_account_id=None,
        ),
        record_grant(store, instant),
    )
    credential = register_credential(store, instant)
    session = store.issue_bridge_session(
        BridgeSessionIssue(
            credential_id=credential.credential_id,
            principal_id=credential.principal_id,
            creator_account_id="creator-1",
            role="creator",
            expires_at=instant + timedelta(hours=1),
            grant_reference_ids=grants,
        )
    )
    return session


def bridge_ticket_issue(session, instant: datetime) -> TicketIssue:
    return TicketIssue(
        purpose=TicketPurpose.BRIDGE_WEBSOCKET,
        principal_id=session.principal_id,
        role=session.role,
        creator_account_id=session.creator_account_id,
        parent_session_id=session.session_id,
        expected_bridge_session_id=session.session_id,
        expected_agent_installation_id=None,
        expires_at=instant + timedelta(minutes=2),
    )


def bridge_ticket_binding(session) -> TicketBinding:
    return TicketBinding(
        purpose=TicketPurpose.BRIDGE_WEBSOCKET,
        creator_account_id=session.creator_account_id,
        role=session.role,
        expected_bridge_session_id=session.session_id,
        expected_agent_installation_id=None,
    )


def register_pairing(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    *,
    activated: bool = True,
) -> AgentPairing:
    grants = (
        record_grant(
            store,
            instant,
            reference_id="agent-installation-grant-2",
            grant_type="installation_grant",
            creator_account_id=None,
        ),
        record_grant(
            store,
            instant,
            reference_id="agent-binding-grant-2",
        ),
    )
    pairing = AgentPairing(
        pairing_id="pairing-1",
        key_id="agent-key-1",
        principal_id="principal-1",
        creator_account_id="creator-1",
        agent_installation_id="agent-installation-1",
        external_issuer="issuer.example",
        external_subject="customer-subject",
        installation_id="brain-installation-1",
        public_key=b"agent-public-key",
        key_fingerprint="fingerprint-1",
        created_at=instant - timedelta(minutes=1),
        grant_reference_ids=grants,
    )
    store.register_agent_pairing(pairing)
    if activated:
        policy = store.build_runtime_policy(
            AuthContext(pairing.principal_id, pairing.creator_account_id, "agent")
        )
        assert store.activate_agent_pairing(policy, pairing.pairing_id) is True
    return pairing


def agent_challenge_binding(pairing: AgentPairing) -> AgentChallengeBinding:
    return AgentChallengeBinding(
        principal_id=pairing.principal_id,
        pairing_id=pairing.pairing_id,
        request_method="GET",
        request_path="/api/v1/agent/config",
        request_body_digest="body-digest",
        ticket_purpose=TicketPurpose.AGENT_CONFIG,
        agent_installation_id=pairing.agent_installation_id,
        creator_account_id=pairing.creator_account_id,
        key_id=pairing.key_id,
        brain_audience="brain-installation-1",
    )


def test_advance_signature_count_rejects_a_reset_to_zero_from_the_callers_expected_count(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    """A caller-supplied non-zero `expected_count` paired with a zero assertion is
    rejected from the arguments alone, before the database is consulted, because a
    positive-counter authenticator presenting a zero counter is a possible clone or
    reset signal (app/persistence/auth.py:597).

    The credential is enrolled with `signature_count=0` in the database -- the same
    state the sibling test below uses to make the zero-path query's live re-check
    (`signature_count = 0`, app/persistence/auth.py:604) return True. Passing a
    mismatched, non-zero `expected_count` here means only the `if expected_count !=
    0` branch can produce the False this test asserts: if that branch were removed
    or inverted, execution would fall through to the same query used by the sibling
    test, find the row (the database really is at zero), and return True instead.
    """
    credential = WebAuthnCredential(
        credential_id="credential-stale-expectation",
        principal_id="principal-1",
        external_issuer="issuer.example",
        external_subject="customer-subject",
        installation_id="brain-installation-1",
        public_key=b"public-key-material",
        signature_count=0,
        enrolled_at=instant - timedelta(days=1),
    )
    store.register_webauthn_credential(credential)

    advanced = store.advance_webauthn_signature_count(
        credential.credential_id,
        principal_id=credential.principal_id,
        expected_count=4,
        asserted_count=0,
    )

    assert advanced is False
    stored = store.webauthn_credential(
        credential.credential_id, principal_id=credential.principal_id
    )
    assert stored is not None and stored.signature_count == 0


def test_advance_signature_count_rechecks_the_live_row_before_accepting_repeated_zero(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    """A repeated-zero assertion (`expected_count=0, asserted_count=0`) is only
    accepted if the credential's *current* stored `signature_count` is genuinely
    still zero; the zero-path query filters on `signature_count = 0` at read time
    (app/persistence/auth.py:604) instead of trusting the caller's `expected_count`
    argument. This guards against a stale `expected_count=0` presented after the
    counter has legitimately advanced elsewhere.

    The credential starts at zero, is advanced to a positive count out from under a
    caller that still believes it is zero, and the stale zero replay must then be
    rejected by the live re-check rather than by the `expected_count != 0` branch
    covered above (which does not fire here, since `expected_count` is 0).
    """
    credential = WebAuthnCredential(
        credential_id="credential-zero-start",
        principal_id="principal-1",
        external_issuer="issuer.example",
        external_subject="customer-subject",
        installation_id="brain-installation-1",
        public_key=b"public-key-material",
        signature_count=0,
        enrolled_at=instant - timedelta(days=1),
    )
    store.register_webauthn_credential(credential)

    advanced_positive = store.advance_webauthn_signature_count(
        credential.credential_id,
        principal_id=credential.principal_id,
        expected_count=0,
        asserted_count=7,
    )
    assert advanced_positive is True

    stale_zero_replay = store.advance_webauthn_signature_count(
        credential.credential_id,
        principal_id=credential.principal_id,
        expected_count=0,
        asserted_count=0,
    )

    assert stale_zero_replay is False
    stored = store.webauthn_credential(
        credential.credential_id, principal_id=credential.principal_id
    )
    assert stored is not None and stored.signature_count == 7


def test_schema_is_durable_and_opaque_secrets_are_never_stored(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    session = issue_session(store, instant)
    ticket = store.issue_ticket(bridge_ticket_issue(session, instant))
    webauthn_binding = WebAuthnChallengeBinding(
        principal_id=session.principal_id,
        credential_id="credential-1",
        relying_party_id="bridge.localhost",
        expected_origin="http://bridge.localhost:17871",
    )
    webauthn_challenge = store.issue_webauthn_challenge(
        webauthn_binding, expires_at=instant + timedelta(minutes=1)
    )
    pairing = register_pairing(store, instant)
    agent_challenge = store.issue_agent_challenge(
        agent_challenge_binding(pairing),
        expires_at=instant + timedelta(minutes=1),
    )
    path = tmp_path / "auth.sqlite3"

    reopened = SQLiteAuthenticationStore(path, clock=clock)
    active = reopened.read_bridge_session(
        session.session_value, csrf_value=session.csrf_value
    )
    assert active is not None
    assert active.session_id == session.session_id

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        stored_ticket = connection.execute(
            "SELECT secret_digest, consumed_at FROM runtime_tickets WHERE ticket_id = ?",
            (ticket.ticket_id,),
        ).fetchone()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = reopened.database.connect()
        try:
            foreign_keys_enabled = foreign_keys.execute("PRAGMA foreign_keys").fetchone()[0]
            synchronous = foreign_keys.execute("PRAGMA synchronous").fetchone()[0]
        finally:
            foreign_keys.close()

    assert {
        "auth_revocation_state",
        "verified_grant_references",
        "webauthn_credentials",
        "agent_pairings",
        "bridge_sessions",
        "auth_challenges",
        "runtime_tickets",
    } <= tables
    assert journal_mode.lower() == "wal"
    assert foreign_keys_enabled == 1
    assert synchronous == 2
    assert stored_ticket[0] != ticket.value
    assert stored_ticket[1] is None
    database_bytes = path.read_bytes()
    assert session.session_value.encode() not in database_bytes
    assert session.csrf_value.encode() not in database_bytes
    assert ticket.value.encode() not in database_bytes
    assert webauthn_challenge.value.encode() not in database_bytes
    assert agent_challenge.value.encode() not in database_bytes


def test_webauthn_challenge_compare_and_consume_contends_exactly_once(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    credential = register_credential(store, instant)
    binding = WebAuthnChallengeBinding(
        principal_id=credential.principal_id,
        credential_id=credential.credential_id,
        relying_party_id="bridge.localhost",
        expected_origin="http://bridge.localhost:17871",
    )
    challenge = store.issue_webauthn_challenge(
        binding, expires_at=instant + timedelta(minutes=1)
    )
    contenders = 12
    barrier = Barrier(contenders)

    def consume() -> bool:
        barrier.wait()
        return store.consume_webauthn_challenge(challenge.value, binding) is not None

    with ThreadPoolExecutor(max_workers=contenders) as executor:
        results = list(executor.map(lambda _: consume(), range(contenders)))

    assert results.count(True) == 1
    assert results.count(False) == contenders - 1


def test_runtime_ticket_compare_and_consume_contends_and_tombstone_survives_reopen(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    session = issue_session(store, instant)
    ticket = store.issue_ticket(bridge_ticket_issue(session, instant))
    binding = bridge_ticket_binding(session)
    contenders = 12
    barrier = Barrier(contenders)

    def consume() -> bool:
        barrier.wait()
        return store.consume_ticket(ticket.value, binding) is not None

    with ThreadPoolExecutor(max_workers=contenders) as executor:
        results = list(executor.map(lambda _: consume(), range(contenders)))

    assert results.count(True) == 1
    assert results.count(False) == contenders - 1
    reopened = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    assert reopened.consume_ticket(ticket.value, binding) is None
    with reopened.database.read() as connection:
        tombstone = connection.execute(
            "SELECT consumed_at FROM runtime_tickets WHERE ticket_id = ?",
            (ticket.ticket_id,),
        ).fetchone()
    assert tombstone["consumed_at"] is not None


def test_wrong_binding_does_not_consume_and_expiry_is_checked_on_read(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    clock: MutableClock,
) -> None:
    session = issue_session(store, instant)
    ticket = store.issue_ticket(bridge_ticket_issue(session, instant))
    wrong = TicketBinding(
        purpose=TicketPurpose.BRIDGE_WEBSOCKET,
        creator_account_id="different-account",
        role=session.role,
        expected_bridge_session_id=session.session_id,
    )
    assert store.consume_ticket(ticket.value, wrong) is None
    assert store.consume_ticket(ticket.value, bridge_ticket_binding(session)) is not None

    webauthn_binding = WebAuthnChallengeBinding(
        principal_id=session.principal_id,
        credential_id="credential-1",
        relying_party_id="bridge.localhost",
        expected_origin="http://bridge.localhost:17871",
    )
    challenge = store.issue_webauthn_challenge(
        webauthn_binding, expires_at=instant + timedelta(minutes=1)
    )
    expiring = store.issue_ticket(bridge_ticket_issue(session, instant))
    clock.value = challenge.expires_at
    assert store.consume_webauthn_challenge(challenge.value, webauthn_binding) is None
    clock.value = expiring.expires_at
    assert store.consume_ticket(expiring.value, bridge_ticket_binding(session)) is None
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT consumed_at FROM runtime_tickets WHERE ticket_id = ?",
            (expiring.ticket_id,),
        ).fetchone()
    assert row["consumed_at"] is None
    clock.value = session.expires_at
    assert store.read_bridge_session(session.session_value) is None


def test_agent_challenge_is_bound_and_pairing_revocation_invalidates_dependents(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    pairing = register_pairing(store, instant)
    binding = agent_challenge_binding(pairing)
    challenge = store.issue_agent_challenge(
        binding, expires_at=instant + timedelta(minutes=1)
    )
    ticket = store.issue_ticket(
        TicketIssue(
            purpose=TicketPurpose.AGENT_CONFIG,
            principal_id=pairing.principal_id,
            role="agent",
            creator_account_id=pairing.creator_account_id,
            parent_pairing_id=pairing.pairing_id,
            expected_agent_installation_id=pairing.agent_installation_id,
            expires_at=instant + timedelta(minutes=1),
        )
    )
    key = RevocationKey(RevocationScopeType.AGENT_PAIRING, pairing.pairing_id)

    assert store.revoke(key, reason="local pairing revoked") == 1
    assert store.consume_agent_challenge(challenge.value, binding) is None
    assert (
        store.consume_ticket(
            ticket.value,
            TicketBinding(
                purpose=TicketPurpose.AGENT_CONFIG,
                creator_account_id=pairing.creator_account_id,
                role="agent",
                expected_agent_installation_id=pairing.agent_installation_id,
            ),
        )
        is None
    )
    reopened = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    assert reopened.revocation_version(key) == 1


@pytest.mark.parametrize(
    ("mismatch", "value"),
    [
        ("creator_account_id", "different-account"),
        ("issuer", "different-issuer.example"),
        ("subject", "different-subject"),
        ("installation_id", "different-installation"),
    ],
)
def test_bridge_session_rejects_a_grant_outside_credential_scope(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    mismatch: str,
    value: str,
) -> None:
    grants = [
        record_grant(
            store,
            instant,
            reference_id="scoped-installation-grant",
            grant_type="installation_grant",
            creator_account_id=None,
        ),
        record_grant(
            store,
            instant,
            reference_id="scoped-membership-grant",
            grant_type="membership_snapshot",
            creator_account_id=None,
        ),
    ]
    overrides = {mismatch: value}
    grants.append(
        record_grant(
            store,
            instant,
            reference_id="out-of-scope-binding-grant",
            **overrides,
        )
    )
    credential = register_credential(store, instant)

    with pytest.raises(AuthenticationStateError):
        store.issue_bridge_session(
            BridgeSessionIssue(
                credential_id=credential.credential_id,
                principal_id=credential.principal_id,
                creator_account_id="creator-1",
                role="creator",
                expires_at=instant + timedelta(minutes=30),
                grant_reference_ids=tuple(grants),
            )
        )

def test_session_and_pairing_require_the_adr_grant_sets(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    binding_grant = record_grant(store, instant, reference_id="binding-only")
    credential = register_credential(store, instant)
    with pytest.raises(AuthenticationStateError, match="types are missing"):
        store.issue_bridge_session(
            BridgeSessionIssue(
                credential_id=credential.credential_id,
                principal_id=credential.principal_id,
                creator_account_id="creator-1",
                role="creator",
                expires_at=instant + timedelta(minutes=30),
                grant_reference_ids=(binding_grant,),
            )
        )

    with pytest.raises(AuthenticationStateError, match="types are missing"):
        store.register_agent_pairing(
            AgentPairing(
                pairing_id="incomplete-pairing",
                key_id="incomplete-key",
                principal_id="principal-1",
                creator_account_id="creator-1",
                agent_installation_id="agent-installation-1",
                external_issuer="issuer.example",
                external_subject="customer-subject",
                installation_id="brain-installation-1",
                public_key=b"agent-public-key",
                key_fingerprint="fingerprint-incomplete",
                created_at=instant,
                grant_reference_ids=(binding_grant,),
            )
        )


def test_creator_account_binding_cannot_be_account_neutral(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    with pytest.raises(ValueError, match="exact creator account"):
        record_grant(
            store,
            instant,
            reference_id="account-neutral-binding",
            creator_account_id=None,
        )

def test_pairing_activation_revalidates_grant_expiry(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    clock: MutableClock,
) -> None:
    pairing = register_pairing(store, instant, activated=False)
    policy = store.build_runtime_policy(
        AuthContext(pairing.principal_id, pairing.creator_account_id, "agent")
    )
    clock.value = instant + timedelta(hours=2)

    with pytest.raises(AuthenticationStateError, match="not current"):
        store.activate_agent_pairing(policy, pairing.pairing_id)
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT activated_at FROM agent_pairings WHERE pairing_id = ?",
            (pairing.pairing_id,),
        ).fetchone()
    assert row["activated_at"] is None


@pytest.mark.parametrize(
    ("mismatch", "value"),
    [
        ("issuer", "different-issuer.example"),
        ("subject", "different-subject"),
        ("installation_id", "different-installation"),
    ],
)
def test_agent_pairing_rejects_grants_outside_its_authority_scope(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    mismatch: str,
    value: str,
) -> None:
    grants = [
        record_grant(
            store,
            instant,
            reference_id="pairing-scope-installation",
            grant_type="installation_grant",
            creator_account_id=None,
        )
    ]
    grants.append(
        record_grant(
            store,
            instant,
            reference_id="pairing-scope-binding",
            **{mismatch: value},
        )
    )

    with pytest.raises(AuthenticationStateError, match="pairing authority"):
        store.register_agent_pairing(
            AgentPairing(
                pairing_id="out-of-scope-pairing",
                key_id="out-of-scope-key",
                principal_id="principal-1",
                creator_account_id="creator-1",
                agent_installation_id="agent-installation-1",
                external_issuer="issuer.example",
                external_subject="customer-subject",
                installation_id="brain-installation-1",
                public_key=b"agent-public-key",
                key_fingerprint="fingerprint-out-of-scope",
                created_at=instant,
                grant_reference_ids=tuple(grants),
            )
        )


def test_pairing_activation_rejects_a_revoked_authority_scope(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    pairing = register_pairing(store, instant, activated=False)
    store.revoke(
        RevocationKey(RevocationScopeType.PRINCIPAL, pairing.principal_id),
        reason="local principal revoked",
    )
    policy = store.build_runtime_policy(
        AuthContext(pairing.principal_id, pairing.creator_account_id, "agent")
    )

    with pytest.raises(AuthenticationStateError, match="scope is revoked"):
        store.activate_agent_pairing(policy, pairing.pairing_id)
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT activated_at FROM agent_pairings WHERE pairing_id = ?",
            (pairing.pairing_id,),
        ).fetchone()
    assert row["activated_at"] is None


def test_ticket_issue_rejects_parent_and_agent_binding_mismatches(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    session = issue_session(store, instant)
    with pytest.raises(AuthenticationStateError, match="outlive"):
        store.issue_ticket(
            replace(
                bridge_ticket_issue(session, instant),
                expires_at=session.expires_at + timedelta(seconds=1),
            )
        )

    pairing = register_pairing(store, instant)
    agent_issue = TicketIssue(
        purpose=TicketPurpose.AGENT_CONFIG,
        principal_id=pairing.principal_id,
        role="agent",
        creator_account_id=pairing.creator_account_id,
        parent_pairing_id=pairing.pairing_id,
        expected_agent_installation_id="different-installation",
        expires_at=instant + timedelta(minutes=1),
    )
    with pytest.raises(AuthenticationStateError, match="does not match pairing"):
        store.issue_ticket(agent_issue)
    with pytest.raises(ValueError, match="parent and expected binding"):
        store.issue_ticket(replace(agent_issue, role="creator"))


def test_revocation_wins_over_a_contending_consume_and_cached_authority(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    tmp_path: Path,
    clock: MutableClock,
) -> None:
    session = issue_session(store, instant)
    ticket = store.issue_ticket(bridge_ticket_issue(session, instant))
    binding = bridge_ticket_binding(session)
    key = RevocationKey(RevocationScopeType.BRIDGE_SESSION, session.session_id)
    barrier = Barrier(2)

    def consume():
        barrier.wait()
        return store.consume_ticket(ticket.value, binding)

    def revoke() -> int:
        barrier.wait()
        return store.revoke(key, reason="logout")

    with ThreadPoolExecutor(max_workers=2) as executor:
        consume_future = executor.submit(consume)
        revoke_future = executor.submit(revoke)
        consumed = consume_future.result()
        assert revoke_future.result() == 1

    if consumed is not None:
        assert store.runtime_policy_is_current(consumed.policy) is False
    assert store.read_bridge_session(session.session_value) is None
    reopened = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)
    assert reopened.revocation_version(key) == 1
    assert reopened.runtime_policy_is_current(session.policy) is False


def test_authorization_input_change_rejects_stale_policy_at_transition(
    store: SQLiteAuthenticationStore,
    instant: datetime,
) -> None:
    pairing = register_pairing(store, instant, activated=False)
    policy: RuntimePolicy = store.build_runtime_policy(
        AuthContext(pairing.principal_id, pairing.creator_account_id, "agent")
    )
    previous_epoch = policy.authorization_epoch

    record_grant(store, instant, reference_id="independent-grant-change")

    with pytest.raises(StaleRuntimePolicyError, match="epoch"):
        store.activate_agent_pairing(policy, pairing.pairing_id)
    assert store.build_runtime_policy(policy.identity).authorization_epoch > previous_epoch
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT activated_at FROM agent_pairings WHERE pairing_id = ?",
            (pairing.pairing_id,),
        ).fetchone()
    assert row["activated_at"] is None


@pytest.mark.parametrize("one_time_object", ["challenge", "ticket"])
def test_expiry_is_sampled_after_waiting_for_the_sqlite_write_lock(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
    one_time_object: str,
) -> None:
    session = issue_session(store, instant)
    if one_time_object == "challenge":
        binding = WebAuthnChallengeBinding(
            principal_id=session.principal_id,
            credential_id="credential-1",
            relying_party_id="bridge.localhost",
            expected_origin="http://bridge.localhost:17871",
        )
        issued = store.issue_webauthn_challenge(
            binding, expires_at=instant + timedelta(seconds=30)
        )
        consume = lambda: store.consume_webauthn_challenge(issued.value, binding)
    else:
        binding = bridge_ticket_binding(session)
        issued = store.issue_ticket(
            replace(
                bridge_ticket_issue(session, instant),
                expires_at=instant + timedelta(seconds=30),
            )
        )
        consume = lambda: store.consume_ticket(issued.value, binding)

    original_transaction = store.database.transaction
    attempted = Event()

    @contextmanager
    def observed_transaction(*, immediate: bool = True):
        attempted.set()
        with original_transaction(immediate=immediate) as connection:
            yield connection

    blocker = store.database.connect()
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(store.database, "transaction", observed_transaction)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(consume)
            assert attempted.wait(timeout=5)
            assert future.done() is False
            clock.value = issued.expires_at
            blocker.commit()
            assert future.result(timeout=5) is None
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
