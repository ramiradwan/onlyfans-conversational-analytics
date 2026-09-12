from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import (
    AuthenticationStateError,
    BridgeSessionIssue,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.security.grant_types import VerifiedGrantDenial


@dataclass
class Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)


def _jti(number: int) -> str:
    return f"019937ce-7600-7000-8000-{number:012x}"


def _grant(clock: Clock, number: int = 1, kind: str = "creator_account_binding"):
    token = f"e30.Z3JhbnQ{number}.c2lnbmF0dXJl"
    return VerifiedGrantReference(
        reference_id=f"grant-{number}",
        grant_identifier=_jti(number),
        grant_type=kind,
        grant_digest=hashlib.sha256(token.encode("ascii")).hexdigest(),
        issuer="issuer.example",
        subject="operator-1",
        installation_id="installation-1",
        creator_account_id="account-1" if kind == "creator_account_binding" else None,
        valid_from=clock.value - timedelta(minutes=1),
        expires_at=clock.value + timedelta(days=9),
        verified_at=clock.value,
        organization_id="organization-1",
        installation_key_id="installation-key-1",
        installation_key_jkt="installation-thumbprint-1",
        membership_id="membership-1" if kind == "membership_snapshot" else None,
        approval_id="approval-1" if kind == "creator_account_binding" else None,
        approval_revision=1 if kind == "creator_account_binding" else None,
        entitlement_id="entitlement-1" if kind == "license_entitlement" else None,
        product_id="product-1" if kind == "license_entitlement" else None,
        allowed_creator_account_ids=("account-1",) if kind == "membership_snapshot" else None,
        membership_roles=("creator_operator",) if kind == "membership_snapshot" else None,
        compact_jws=token,
    )


def _denial(clock: Clock, grant: VerifiedGrantReference, reason="revoked", number=100):
    instant = int(clock.value.timestamp())
    return VerifiedGrantDenial(
        denial_jti=_jti(number),
        grant_type=grant.grant_type,
        revoked_jti=grant.grant_identifier,
        issued_at=instant,
        expires_at=instant + 600,
        effective_at=instant,
        reason_code=reason,
        evidence_sha256=hashlib.sha256(f"denial-{number}".encode()).hexdigest(),
    )


def _tombstones(store):
    with store.database.read() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM hosted_grant_tombstones")]


def _epoch(store):
    return store.build_runtime_policy().authorization_epoch


def _session(store, clock):
    grants = tuple(
        _grant(clock, number, kind)
        for number, kind in enumerate(
            ("installation_grant", "membership_snapshot", "creator_account_binding", "license_entitlement"),
            1,
        )
    )
    store.record_verified_grants(grants)
    store.register_webauthn_credential(WebAuthnCredential(
        credential_id="credential-1", principal_id="principal-1",
        external_issuer="issuer.example", external_subject="operator-1",
        installation_id="installation-1", public_key=b"test-public-key",
        signature_count=0, enrolled_at=clock.value - timedelta(hours=1),
    ))
    session = store.issue_bridge_session(BridgeSessionIssue(
        credential_id="credential-1", principal_id="principal-1",
        creator_account_id="account-1", role="creator",
        expires_at=clock.value + timedelta(hours=1),
        grant_reference_ids=tuple(grant.reference_id for grant in grants[:3]),
    ))
    return session, grants


def test_denial_evidence_is_durable_secret_free_and_idempotent(store, clock):
    grant = _grant(clock)
    denial = _denial(clock, grant, "approval_revoked")
    store.record_verified_grant(grant)
    epoch = _epoch(store)
    assert store.apply_hosted_grant_denial(grant, denial) == "applied"
    applied_epoch = _epoch(store)
    assert applied_epoch != epoch
    rows = _tombstones(store)
    assert len(rows) == 1
    assert rows[0]["source"] == "hosted_refresh"
    assert rows[0]["scope_type"] == "creator-approval"
    assert rows[0]["scope_id"] == grant.approval_id
    assert rows[0]["evidence_sha256"] == denial.evidence_sha256
    assert datetime.fromisoformat(rows[0]["retain_through"]) == grant.expires_at
    assert bool(grant.compact_jws not in json.dumps(rows))
    assert store.verified_grant(grant.reference_id).compact_jws is None
    assert store.verified_grants() == ()
    reconstructed = SQLiteAuthenticationStore(store.database.path, clock=clock)
    clock.value += timedelta(days=400)
    assert reconstructed.apply_hosted_grant_denial(grant, denial) == "already_applied"
    assert _epoch(reconstructed) == applied_epoch
    assert _tombstones(reconstructed) == rows
    fresh = replace(_grant(clock, 2), approval_id=grant.approval_id)
    with pytest.raises(AuthenticationStateError, match="authority is revoked"):
        reconstructed.record_verified_grant(fresh)


def test_tombstones_cannot_be_edited_or_removed(store, clock):
    grant = _grant(clock)
    store.record_verified_grant(grant)
    store.apply_hosted_grant_denial(grant, _denial(clock, grant))
    for statement in (
        "DELETE FROM hosted_grant_tombstones",
        "UPDATE hosted_grant_tombstones SET reason_code = 'approval_revoked'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store.database.transaction() as connection:
                connection.execute(statement)
    assert len(_tombstones(store)) == 1


@pytest.mark.parametrize("field,value", [
    ("grant_digest", "f" * 64), ("organization_id", "different-organization"),
    ("installation_id", "different-installation"), ("approval_revision", 2),
    ("installation_key_jkt", "different-key"), ("subject", "different-operator"),
    ("verified_at", datetime(2026, 9, 12, 11, tzinfo=timezone.utc)),
])
def test_full_expected_reference_is_compared_before_denial(store, clock, field, value):
    grant = _grant(clock)
    store.record_verified_grant(grant)
    epoch = _epoch(store)
    assert store.apply_hosted_grant_denial(replace(grant, **{field: value}), _denial(clock, grant)) == "stale"
    assert _tombstones(store) == []
    assert _epoch(store) == epoch
    assert store.verified_grants() == (grant,)


def test_replacement_wins_over_late_denial(store, clock):
    grant, replacement = _grant(clock), _grant(clock, 2)
    store.record_verified_grant(grant)
    store.replace_verified_grant(grant.reference_id, replacement)
    epoch = _epoch(store)
    assert store.apply_hosted_grant_denial(grant, _denial(clock, grant, "approval_revoked")) == "stale"
    assert _epoch(store) == epoch
    assert _tombstones(store) == []
    assert store.verified_grants() == (replacement,)


@pytest.mark.parametrize("scope", [RevocationScopeType.VERIFIED_GRANT, RevocationScopeType.CREATOR_ACCOUNT, RevocationScopeType.INSTALLATION])
def test_local_revocation_wins_over_late_hosted_denial(store, clock, scope):
    grant = _grant(clock)
    store.record_verified_grant(grant)
    scope_id = {
        RevocationScopeType.VERIFIED_GRANT: grant.reference_id,
        RevocationScopeType.CREATOR_ACCOUNT: grant.creator_account_id,
        RevocationScopeType.INSTALLATION: grant.installation_id,
    }[scope]
    store.revoke(RevocationKey(scope, scope_id))
    epoch = _epoch(store)
    assert store.apply_hosted_grant_denial(grant, _denial(clock, grant)) == "stale"
    assert _epoch(store) == epoch
    assert _tombstones(store) == []


@pytest.mark.parametrize("kind,reason,scope_field,scope_name", [
    ("membership_snapshot", "membership_removed", "membership_id", "membership"),
    ("creator_account_binding", "approval_revoked", "approval_id", "creator-approval"),
    ("license_entitlement", "entitlement_inactive", "entitlement_id", "entitlement"),
])
def test_scope_cascade_and_future_insertion_are_isolated(store, clock, kind, reason, scope_field, scope_name):
    grant = _grant(clock, 1, kind)
    same_scope = _grant(clock, 2, kind)
    other_scope = replace(_grant(clock, 3, kind), **{scope_field: "other-scope"})
    other_installation = replace(_grant(clock, 4, kind), installation_id="other-installation")
    other_organization = replace(_grant(clock, 5, kind), organization_id="other-organization")
    store.record_verified_grants((grant, same_scope, other_scope, other_installation, other_organization))
    assert store.apply_hosted_grant_denial(grant, _denial(clock, grant, reason)) == "applied"
    assert _tombstones(store)[0]["scope_type"] == scope_name
    assert store.verified_grant(grant.reference_id).compact_jws is None
    assert store.verified_grant(same_scope.reference_id).compact_jws is None
    assert set(store.verified_grants()) == {other_scope, other_installation, other_organization}
    with pytest.raises(AuthenticationStateError, match="authority is revoked"):
        store.record_verified_grant(_grant(clock, 6, kind))
    recovered = replace(_grant(clock, 7, kind), **{scope_field: "new-authorized-scope"})
    store.record_verified_grant(recovered)
    assert recovered in store.verified_grants()


@pytest.mark.parametrize("kind,reason", [
    ("membership_snapshot", "role_reduced"),
    ("membership_snapshot", "revoked"),
    ("creator_account_binding", "revoked"),
    ("license_entitlement", "revoked"),
])
def test_individual_denial_allows_new_jti_under_same_scope(store, clock, kind, reason):
    grant = _grant(clock, 1, kind)
    other = _grant(clock, 2, kind)
    store.record_verified_grants((grant, other))
    assert store.apply_hosted_grant_denial(grant, _denial(clock, grant, reason)) == "applied"
    assert _tombstones(store)[0]["scope_type"] == "jti"
    assert store.verified_grants() == (other,)
    replay = replace(_grant(clock, 3, kind), grant_identifier=grant.grant_identifier)
    with pytest.raises(AuthenticationStateError, match="authority is revoked"):
        store.record_verified_grant(replay)
    replacement = _grant(clock, 4, kind)
    store.replace_verified_grant(other.reference_id, replacement)
    assert store.verified_grants() == (replacement,)


@pytest.mark.parametrize("number,reason", [(1, "revoked"), (2, "membership_removed"), (3, "approval_revoked")])
def test_identity_denials_invalidate_dependent_operator_sessions(store, clock, number, reason):
    session, grants = _session(store, clock)
    denied = grants[number - 1]
    assert store.read_bridge_session(session.session_value) is not None
    assert store.apply_hosted_grant_denial(denied, _denial(clock, denied, reason)) == "applied"
    assert store.read_bridge_session(session.session_value) is None
    if number == 1:
        assert store.verified_grants() == ()
        assert all(store.verified_grant(grant.reference_id).compact_jws is None for grant in grants)
        with pytest.raises(AuthenticationStateError, match="authority is revoked"):
            store.record_verified_grant(_grant(clock, 7, "license_entitlement"))


@pytest.mark.parametrize("reason", ["revoked", "entitlement_inactive"])
def test_license_denial_preserves_operator_existing_data_session(store, clock, reason):
    session, grants = _session(store, clock)
    entitlement = grants[-1]
    assert store.apply_hosted_grant_denial(entitlement, _denial(clock, entitlement, reason)) == "applied"
    assert store.read_bridge_session(session.session_value) is not None
    assert set(store.verified_grants()) == set(grants[:3])
    assert store.verified_grant(entitlement.reference_id).compact_jws is None


@pytest.mark.parametrize("boundary", ["denial_expiry", "grant_expiry", "issued_in_future", "effective_in_future"])
def test_denial_commit_requires_current_time_windows(store, clock, boundary):
    grant = _grant(clock)
    denial = _denial(clock, grant)
    if boundary == "denial_expiry":
        clock.value += timedelta(seconds=600)
    elif boundary == "grant_expiry":
        grant = replace(grant, expires_at=clock.value)
    elif boundary == "issued_in_future":
        denial = replace(denial, issued_at=denial.issued_at + 61, expires_at=denial.expires_at + 61)
    else:
        denial = replace(denial, effective_at=denial.effective_at + 1)
    store.record_verified_grant(grant)
    epoch = _epoch(store)
    assert store.apply_hosted_grant_denial(grant, denial) == "stale"
    assert _epoch(store) == epoch
    assert _tombstones(store) == []


def test_denial_is_accepted_immediately_before_its_deadline(store, clock):
    grant = _grant(clock)
    denial = _denial(clock, grant)
    store.record_verified_grant(grant)
    clock.value += timedelta(seconds=599, microseconds=999999)
    assert store.apply_hosted_grant_denial(grant, denial) == "applied"


@pytest.mark.parametrize("failure", ["storage", "time_boundary"])
def test_denial_transaction_rolls_back_every_effect(store, clock, monkeypatch, failure):
    session, grants = _session(store, clock)
    grant = grants[0]
    denial = _denial(clock, grant)
    epoch = _epoch(store)
    original = store._increment_authorization_epoch

    def fail(connection):
        original(connection)
        if failure == "storage":
            raise RuntimeError("injected transaction failure")
        clock.value += timedelta(seconds=600)

    monkeypatch.setattr(store, "_increment_authorization_epoch", fail)
    if failure == "storage":
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            store.apply_hosted_grant_denial(grant, denial)
    else:
        assert store.apply_hosted_grant_denial(grant, denial) == "stale"
    assert _epoch(store) == epoch
    assert _tombstones(store) == []
    assert set(store.verified_grants()) == set(grants)
    assert all(store.verified_grant(item.reference_id).compact_jws is not None for item in grants)
    assert store.read_bridge_session(session.session_value) is not None


def test_parallel_denial_replay_commits_one_evidence_record(store, clock):
    grant = _grant(clock)
    denial = _denial(clock, grant)
    store.record_verified_grant(grant)
    barrier = Barrier(2)

    def apply():
        barrier.wait(timeout=5)
        return store.apply_hosted_grant_denial(grant, denial)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(results) == ["already_applied", "applied"]
    assert len(_tombstones(store)) == 1


def test_conflicting_replayed_evidence_never_changes_enforcement(store, clock):
    grant = _grant(clock)
    denial = _denial(clock, grant)
    store.record_verified_grant(grant)
    assert store.apply_hosted_grant_denial(grant, denial) == "applied"
    epoch, rows = _epoch(store), _tombstones(store)
    assert store.apply_hosted_grant_denial(grant, replace(denial, evidence_sha256="f" * 64)) == "stale"
    assert _epoch(store) == epoch
    assert _tombstones(store) == rows


@pytest.mark.parametrize("operation", ["batch", "replacement"])
def test_scope_guard_rolls_back_grant_acquisition_and_replacement(store, clock, operation):
    denied = _grant(clock)
    current = replace(_grant(clock, 2), approval_id="unrelated-approval")
    store.record_verified_grants((denied, current))
    store.apply_hosted_grant_denial(denied, _denial(clock, denied, "approval_revoked"))
    blocked = _grant(clock, 3)
    fresh = replace(_grant(clock, 4), approval_id="new-approval")
    epoch = _epoch(store)
    with pytest.raises(AuthenticationStateError, match="authority is revoked"):
        if operation == "batch":
            store.record_verified_grants((fresh, blocked))
        else:
            store.replace_verified_grant(current.reference_id, blocked)
    assert store.verified_grants() == (current,)
    assert store.verified_grant(current.reference_id).compact_jws is not None
    assert store.verified_grant(blocked.reference_id) is None
    assert store.verified_grant(fresh.reference_id) is None
    assert _epoch(store) == epoch


def test_installation_tombstone_does_not_block_new_installation_identity(store, clock):
    denied = _grant(clock, 1, "installation_grant")
    store.record_verified_grant(denied)
    store.apply_hosted_grant_denial(denied, _denial(clock, denied))
    recovered = replace(
        _grant(clock, 2, "installation_grant"),
        installation_id="new-installation", installation_key_id="new-key",
        installation_key_jkt="new-thumbprint",
    )
    store.record_verified_grant(recovered)
    assert store.verified_grants() == (recovered,)


def test_bounded_grant_read_applies_sql_limit_and_preserves_active_defaults(store, clock):
    grants = tuple(_grant(clock, number) for number in range(1, 5))
    store.record_verified_grants(grants)
    store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, grants[0].reference_id))
    assert store.verified_grants() == grants[1:]
    assert store.verified_grants(limit=2) == grants[1:3]
    assert store.verified_grants(include_revoked=True, limit=2) == grants[:2]
    with store.database.read() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM verified_grant_references "
            "WHERE revoked_at IS NULL ORDER BY verified_at, reference_id LIMIT ?",
            (2,),
        ).fetchall()
    assert any("verified_grants_current_refresh" in row[3] for row in plan)


@pytest.mark.parametrize("limit", [False, True, 0, -1, 1.5, "2", 1 << 64])
def test_grant_read_refuses_invalid_limits(store, limit):
    with pytest.raises(ValueError, match="positive SQLite integer"):
        store.verified_grants(limit=limit)


@pytest.mark.parametrize("change", ["digest", "immutable_scope", "old_expiry", "new_expiry"])
def test_replacement_expected_context_is_compared_atomically(store, clock, change):
    grant, replacement = _grant(clock), _grant(clock, 2)
    expected = grant
    if change == "digest":
        expected = replace(expected, grant_digest="f" * 64)
    elif change == "immutable_scope":
        replacement = replace(replacement, approval_revision=2)
    elif change == "old_expiry":
        grant = replace(grant, expires_at=clock.value)
        expected = grant
    else:
        replacement = replace(replacement, expires_at=clock.value)
    store.record_verified_grant(grant)
    epoch = _epoch(store)
    with pytest.raises(AuthenticationStateError, match="context is stale"):
        store.replace_verified_grant(grant.reference_id, replacement, expected=expected)
    assert store.verified_grants() == (grant,)
    assert store.verified_grant(replacement.reference_id) is None
    assert _epoch(store) == epoch


@pytest.mark.parametrize("scope", [RevocationScopeType.INSTALLATION, RevocationScopeType.CREATOR_ACCOUNT])
def test_replacement_cannot_restore_bytes_after_local_scope_revocation(store, clock, scope):
    grant, replacement = _grant(clock), _grant(clock, 2)
    store.record_verified_grant(grant)
    scope_id = grant.installation_id if scope == RevocationScopeType.INSTALLATION else grant.creator_account_id
    store.revoke(RevocationKey(scope, scope_id))
    epoch = _epoch(store)
    with pytest.raises(AuthenticationStateError, match="authority is revoked"):
        store.replace_verified_grant(grant.reference_id, replacement, expected=grant)
    assert store.verified_grant(grant.reference_id).compact_jws is None
    assert store.verified_grant(replacement.reference_id) is None
    assert _epoch(store) == epoch


def test_replacement_compare_and_swap_allows_updated_membership_authority(store, clock):
    grant = _grant(clock, 1, "membership_snapshot")
    replacement = replace(
        _grant(clock, 2, "membership_snapshot"),
        allowed_creator_account_ids=(), membership_roles=("delegated_installer",),
    )
    store.record_verified_grant(grant)
    store.replace_verified_grant(grant.reference_id, replacement, expected=grant)
    assert store.verified_grants() == (replacement,)
    assert store.verified_grant(grant.reference_id).compact_jws is None


def test_replacement_crossing_grant_deadline_rolls_back(store, clock, monkeypatch):
    grant = replace(_grant(clock), expires_at=clock.value + timedelta(microseconds=1))
    replacement = _grant(clock, 2)
    store.record_verified_grant(grant)
    epoch = _epoch(store)
    original = store._increment_authorization_epoch

    def cross_boundary(connection):
        original(connection)
        clock.value += timedelta(microseconds=1)

    monkeypatch.setattr(store, "_increment_authorization_epoch", cross_boundary)
    with pytest.raises(AuthenticationStateError, match="context is stale"):
        store.replace_verified_grant(grant.reference_id, replacement, expected=grant)
    assert store.verified_grant(replacement.reference_id) is None
    assert store.verified_grant(grant.reference_id).compact_jws is not None
    assert _epoch(store) == epoch
