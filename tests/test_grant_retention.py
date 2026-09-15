from __future__ import annotations

import hashlib
import json
import shutil
import secrets
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence.auth import (
    AgentPairing,
    ProvisioningCandidate,
    ProvisioningCandidateState,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
)
from app.security.grant_types import AGENT_PAIRING_GRANT_TYPES
from app.security.grant_verifier import MAX_GRANT_CHARACTERS

_INSTALLATION_ID = "brain-installation-1"
_ACCOUNT_ID = "creator-1"
_ISSUER = "issuer.example"
_SUBJECT = "customer-subject"


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


def compact_jws(marker: str, *, payload_length: int = 24) -> str:
    """Build a structurally compact JWS of a chosen length."""

    body = (marker * payload_length)[:payload_length]
    return f"eyJhbGciOiJFUzI1NiJ9.{body}.c2lnbmF0dXJl"


_ENVELOPE_LENGTH = len(compact_jws("x", payload_length=0))


def compact_jws_of_length(marker: str, total: int) -> str:
    """Build a compact JWS of exactly `total` characters."""

    token = compact_jws(marker, payload_length=total - _ENVELOPE_LENGTH)
    assert len(token) == total
    return token


def grant(
    instant: datetime,
    *,
    reference_id: str = "grant-ref-1",
    grant_type: str = "creator_account_binding",
    installation_id: str = _INSTALLATION_ID,
    creator_account_id: str | None = _ACCOUNT_ID,
    token: str | None = None,
) -> VerifiedGrantReference:
    return VerifiedGrantReference(
        reference_id=reference_id,
        grant_identifier=f"grant-id-{reference_id}",
        grant_type=grant_type,
        grant_digest=hashlib.sha256((token or reference_id).encode()).hexdigest(),
        issuer=_ISSUER,
        subject=_SUBJECT,
        installation_id=installation_id,
        creator_account_id=creator_account_id,
        valid_from=instant - timedelta(minutes=1),
        expires_at=instant + timedelta(hours=2),
        verified_at=instant - timedelta(minutes=1),
        compact_jws=token,
    )


def pairing_grants(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    *,
    retained: bool = True,
) -> tuple[str, ...]:
    references: list[str] = []
    for index, grant_type in enumerate(AGENT_PAIRING_GRANT_TYPES):
        reference_id = f"pairing-grant-{index}"
        store.record_verified_grant(
            grant(
                instant,
                reference_id=reference_id,
                grant_type=grant_type,
                creator_account_id=(
                    _ACCOUNT_ID if grant_type == "creator_account_binding" else None
                ),
                token=compact_jws(str(index)) if retained else None,
            )
        )
        references.append(reference_id)
    return tuple(references)


def stored_token(store: SQLiteAuthenticationStore, reference_id: str) -> str | None:
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT compact_jws FROM verified_grant_references "
            "WHERE reference_id = ?",
            (reference_id,),
        ).fetchone()
    return None if row["compact_jws"] is None else str(row["compact_jws"])


def test_retained_grant_bytes_survive_a_round_trip(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    token = compact_jws("a")
    store.record_verified_grant(grant(instant, token=token))

    assert_secret_equal(store.verified_grant("grant-ref-1").compact_jws, token)


def test_retained_grant_bytes_stay_out_of_the_record_repr(
    instant: datetime,
) -> None:
    token = compact_jws("b")
    rendered = repr(grant(instant, token=token))

    assert_secret_absent(token, rendered)
    # Must-fail control: a field that is meant to be visible still renders,
    # so the absence above is redaction rather than an empty repr.
    assert hashlib.sha256(token.encode()).hexdigest() in rendered
    # Dataclass-aware assertion diagnostics inspect comparison fields even when
    # repr is disabled. The digest already supplies the comparison identity.
    secret_field = next(
        f for f in fields(VerifiedGrantReference) if f.name == "compact_jws"
    )
    assert not secret_field.compare


def test_a_grant_at_the_contract_maximum_is_retained(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    token = compact_jws_of_length("c", MAX_GRANT_CHARACTERS)
    store.record_verified_grant(grant(instant, token=token))

    assert_secret_equal(stored_token(store, "grant-ref-1"), token)


def test_a_grant_beyond_the_contract_maximum_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    token = compact_jws_of_length("d", MAX_GRANT_CHARACTERS + 1)

    with pytest.raises(ValueError) as caught:
        store.record_verified_grant(grant(instant, token=token))

    assert_secret_absent(token, str(caught.value))
    assert store.verified_grant("grant-ref-1") is None


@pytest.mark.parametrize(
    "token",
    ["not-a-compact-jws", "eyJhbGciOiJFUzI1NiJ9.påyload.c2ln"],
)
def test_a_malformed_retained_grant_is_refused(
    store: SQLiteAuthenticationStore, instant: datetime, token: str
) -> None:
    with pytest.raises(ValueError) as caught:
        store.record_verified_grant(grant(instant, token=token))

    assert_secret_absent(token, str(caught.value))


def test_refresh_leaves_only_the_replacement_token_current(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    previous = compact_jws("e")
    replacement = compact_jws("f")
    store.record_verified_grant(grant(instant, token=previous))

    store.replace_verified_grant(
        "grant-ref-1",
        grant(instant, reference_id="grant-ref-2", token=replacement),
    )

    assert stored_token(store, "grant-ref-1") is None
    assert_secret_equal(stored_token(store, "grant-ref-2"), replacement)


def test_revoking_a_grant_erases_its_retained_bytes(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    store.record_verified_grant(grant(instant, token=compact_jws("g")))

    store.revoke(
        RevocationKey(RevocationScopeType.VERIFIED_GRANT, "grant-ref-1"),
        reason="revoked",
    )

    assert stored_token(store, "grant-ref-1") is None


@pytest.mark.parametrize(
    ("scope_type", "scope_id"),
    [
        (RevocationScopeType.CREATOR_ACCOUNT, _ACCOUNT_ID),
        (RevocationScopeType.INSTALLATION, _INSTALLATION_ID),
    ],
)
def test_an_advancing_scope_erases_the_grants_it_covers(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    scope_type: RevocationScopeType,
    scope_id: str,
) -> None:
    store.record_verified_grant(grant(instant, token=compact_jws("h")))
    store.record_verified_grant(
        grant(
            instant,
            reference_id="other-ref",
            installation_id="brain-installation-2",
            creator_account_id="creator-2",
            token=compact_jws("i"),
        )
    )

    store.revoke(RevocationKey(scope_type, scope_id), reason="revoked")

    assert stored_token(store, "grant-ref-1") is None
    # Must-fail control: a grant outside the advancing scope keeps its bytes,
    # so the erasure above is scoped rather than a table-wide clear.
    assert stored_token(store, "other-ref") is not None


def test_pairing_eligibility_requires_both_retained_grants(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    grants = pairing_grants(store, instant)

    assert store.companion_pairing_grants_are_eligible(grants)


def test_pairing_eligibility_refuses_pre_retention_references(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    grants = pairing_grants(store, instant, retained=False)

    assert not store.companion_pairing_grants_are_eligible(grants)


def test_pairing_eligibility_refuses_a_partially_retained_set(
    store: SQLiteAuthenticationStore, instant: datetime
) -> None:
    grants = pairing_grants(store, instant)
    store.revoke(
        RevocationKey(RevocationScopeType.VERIFIED_GRANT, grants[0]),
        reason="revoked",
    )

    assert not store.companion_pairing_grants_are_eligible(grants)


def assert_secret_equal(actual: str | None, expected: str) -> None:
    # Suppress pytest operand rendering even if this assertion fails.
    if actual is None or not secrets.compare_digest(actual, expected):
        pytest.fail("retained secret mismatch", pytrace=False)


def assert_secret_absent(secret: str, rendered: str) -> None:
    if secret in rendered:
        pytest.fail("retained secret exposed", pytrace=False)


def test_retention_bound_matches_the_vendored_profile() -> None:
    root = Path(__file__).resolve().parents[1]
    profile = json.loads(
        (root / "contracts/companion-pairing-profile/profile.json").read_text()
    )
    assert MAX_GRANT_CHARACTERS == profile["limits"]["max_grant_characters"]


def test_retained_bytes_must_match_verified_digest(store, instant) -> None:
    record = replace(grant(instant, token=compact_jws("j")), grant_digest="0" * 64)
    with pytest.raises(ValueError, match="retained grant digest does not match"):
        store.record_verified_grant(record)
    assert store.verified_grant(record.reference_id) is None


def test_expired_or_missing_grants_cannot_pair(store, instant, clock) -> None:
    references = pairing_grants(store, instant)
    assert not store.companion_pairing_grants_are_eligible(references + ("missing",))
    clock.value = instant + timedelta(hours=2)
    assert not store.companion_pairing_grants_are_eligible(references)


def test_failed_refresh_rolls_back_insert_and_retention_clear(
    store, instant, monkeypatch
) -> None:
    previous = compact_jws("k")
    store.record_verified_grant(grant(instant, token=previous))
    replacement = grant(instant, reference_id="replacement", token=compact_jws("l"))

    def refuse_epoch(connection):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(store, "_increment_authorization_epoch", refuse_epoch)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        store.replace_verified_grant("grant-ref-1", replacement)
    assert_secret_equal(stored_token(store, "grant-ref-1"), previous)
    assert store.verified_grant("replacement") is None


def test_batch_failure_keeps_all_tokens_out_of_storage(store, instant) -> None:
    from app.persistence import sqlite_api as sqlite3

    first = grant(instant, token=compact_jws("m"))
    duplicate = replace(first, reference_id="duplicate")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_verified_grants((first, duplicate))
    assert store.verified_grants() == ()


def test_legacy_grants_keep_existing_pairing_behavior_but_not_companion_eligibility(
    store, instant
) -> None:
    references = pairing_grants(store, instant, retained=False)
    store.register_agent_pairing(
        AgentPairing(
            pairing_id="legacy-pairing",
            key_id="agent-key",
            principal_id="operator",
            creator_account_id=_ACCOUNT_ID,
            agent_installation_id="agent-install",
            external_issuer=_ISSUER,
            external_subject=_SUBJECT,
            installation_id=_INSTALLATION_ID,
            public_key=b"public-key",
            key_fingerprint="fingerprint",
            created_at=instant,
            grant_reference_ids=references,
        )
    )
    assert not store.companion_pairing_grants_are_eligible(references)


def test_migration_preserves_legacy_row_and_enforces_sql_length(
    tmp_path, instant, clock
) -> None:
    from app.persistence import auth
    from app.persistence import sqlite_api as sqlite3

    catalog = Path(auth.__file__).with_name("auth_sql")
    previous = tmp_path / "old-migrations"
    previous.mkdir()
    old = sorted(p for p in catalog.glob("*.sql") if int(p.name[:4]) < 10)
    assert len(old) == 9
    for migration in old:
        shutil.copyfile(migration, previous / migration.name)
    location = tmp_path / "upgraded.sqlite3"
    legacy = SQLiteAuthenticationStore(location, clock=clock, migrations_dir=previous)
    with legacy.database.transaction() as connection:
        connection.execute(
            """INSERT INTO verified_grant_references
            (reference_id,grant_identifier,grant_type,grant_digest,issuer,subject,
             installation_id,creator_account_id,valid_from,expires_at,verified_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy",
                "legacy-id",
                "creator_account_binding",
                "0" * 64,
                _ISSUER,
                _SUBJECT,
                _INSTALLATION_ID,
                _ACCOUNT_ID,
                (instant - timedelta(minutes=1)).isoformat(),
                (instant + timedelta(hours=1)).isoformat(),
                instant.isoformat(),
            ),
        )
    upgraded = SQLiteAuthenticationStore(location, clock=clock)
    assert upgraded.verified_grant("legacy").compact_jws is None
    assert not upgraded.companion_pairing_grants_are_eligible(("legacy",))
    with pytest.raises(sqlite3.IntegrityError):
        with upgraded.database.transaction() as connection:
            connection.execute(
                "UPDATE verified_grant_references SET compact_jws=? WHERE reference_id=?",
                ("x" * (MAX_GRANT_CHARACTERS + 1), "legacy"),
            )
    assert upgraded.verified_grant("legacy").compact_jws is None


@pytest.mark.parametrize("retention_state", ["present", "legacy", "revoked"])
def test_provisioning_replay_cannot_restore_cleared_grant_bytes(
    store, instant, retention_state
):
    reference = grant(instant, token=compact_jws("n"))
    store.record_verified_grant(
        replace(reference, compact_jws=None)
        if retention_state == "legacy"
        else reference
    )
    if retention_state == "revoked":
        store.revoke(
            RevocationKey(RevocationScopeType.INSTALLATION, _INSTALLATION_ID),
            reason="revoked",
        )
    store.record_provisioning_candidate(
        ProvisioningCandidate(
            association_request_id="candidate",
            installation_id=_INSTALLATION_ID,
            onboarding_transaction_id="onboarding",
            organization_id="organization",
            creator_account_id=_ACCOUNT_ID,
            state=ProvisioningCandidateState.PENDING,
            requested_at=instant,
        )
    )
    assert store.record_verified_grant_and_approve_provisioning_candidate(
        replace(reference, verified_at=instant),
        "candidate",
        resolved_at=instant,
    )
    if retention_state == "present":
        assert_secret_equal(
            stored_token(store, reference.reference_id), reference.compact_jws
        )
    else:
        assert stored_token(store, reference.reference_id) is None


def test_failed_candidate_approval_does_not_retain_a_token(store, instant):
    reference = grant(instant, token=compact_jws("o"))
    assert not store.record_verified_grant_and_approve_provisioning_candidate(
        reference,
        "missing-candidate",
        resolved_at=instant,
    )
    assert store.verified_grant(reference.reference_id) is None
