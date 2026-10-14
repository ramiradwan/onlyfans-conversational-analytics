"""Exact continuation finalization on SQLCipher with simulated verified grants."""
from dataclasses import replace
from datetime import timedelta

import pytest

from app.persistence.auth import RevocationKey, RevocationScopeType
from app.provisioning.finalize import FinalizationRefused, FinalizationRequest, verified_grant_bindings
from test_registered_continuation_context import owner, selected, NOW
from test_installation_continuation_owner import grant, CONTEXT, EPOCH, EXPIRY

pytestmark = [pytest.mark.ci_tier("integration")]


def completed(owner):
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE provisioning_claim_submissions SET claim_profile='urn:bridge-clean:installation-claim:v2'")
    _, session = selected(owner)
    owner.contexts.begin_prepare(session.journey_id)
    owner.contexts.bind_result(session.journey_id, continuation_id=CONTEXT, expires_at=EXPIRY,
        epoch=EPOCH, revision=1, provider_state="approved")
    owner.store.record_verified_grant(replace(grant(owner, "membership", "membership_snapshot"),
        membership_roles=("owner",), allowed_creator_account_ids=("creator-1", "creator-2")))
    owner.store.record_verified_grant(grant(owner, "installation", "installation_grant"))
    owner.contexts.complete_binding(session.journey_id, grant(owner), membership_reference_id="membership")
    return session.journey_id


def verify(owner, journey, **changes):
    return verified_grant_bindings(store=owner.store,
        request=FinalizationRequest("request-1", "creator-1"), trusted_journey_id=journey, **changes)


def test_registered_finalization_selects_exact_account_and_preserves_other_binding(owner):
    journey = completed(owner)
    other = replace(grant(owner, "other-binding"), creator_account_id="creator-2", approval_id="approval-2")
    owner.store.record_verified_grant(other)
    _, account, references = verify(owner, journey)
    assert account.creator_account_id == "creator-1" and "binding" in references and "other-binding" not in references
    assert {g.reference_id for g in owner.store.verified_grants()} >= {"binding", "other-binding"}
    with pytest.raises(FinalizationRefused) as raised:
        verified_grant_bindings(store=owner.store, request=FinalizationRequest("request-1", "creator-1"))
    assert raised.value.reason == "ambiguous_grant_set"


def test_exact_target_duplicates_still_refuse(owner):
    journey = completed(owner)
    owner.store.record_verified_grant(grant(owner, "duplicate"))
    with pytest.raises(FinalizationRefused) as raised:
        verify(owner, journey)
    assert raised.value.reason == "ambiguous_grant_set"


@pytest.mark.parametrize("change", ["expired", "revoked", "principal", "creator", "installation", "grant", "wrong-target", "pending-context"])
def test_current_context_and_independent_grant_authority_are_required(owner, change):
    journey = completed(owner)
    if change == "expired":
        owner.clock[0] += timedelta(minutes=21)
    elif change == "revoked":
        owner.contexts.mark_state(journey, "revoked")
    elif change == "pending-context":
        with owner.store.database.transaction() as connection:
            connection.execute("UPDATE onboarding_journeys SET state='waiting' WHERE journey_id=?", (journey,))
    elif change == "wrong-target":
        with pytest.raises(FinalizationRefused):
            verified_grant_bindings(store=owner.store, request=FinalizationRequest("request-1", "creator-2"),
                trusted_journey_id=journey)
        return
    else:
        kind, identifier = {
            "principal": (RevocationScopeType.PRINCIPAL, "fixture-subject"),
            "creator": (RevocationScopeType.CREATOR_ACCOUNT, "creator-1"),
            "installation": (RevocationScopeType.INSTALLATION, "installation-1"),
            "grant": (RevocationScopeType.VERIFIED_GRANT, "binding"),
        }[change]
        owner.store.revoke(RevocationKey(kind, identifier))
    with pytest.raises(FinalizationRefused):
        verify(owner, journey)


@pytest.mark.parametrize("change", ["principal", "installation", "creator", "binding", "context", "deadline", "grant-tuple"])
def test_authority_is_rechecked_after_configuration_before_authorizing(owner, tmp_path, monkeypatch, change):
    from app.provisioning import finalize
    from app.provisioning.completion import durable_completion_reader, durable_finalize_action

    journey = completed(owner)
    initialize = finalize.initialize_production_configuration

    def initialize_then_change(**kwargs):
        result = initialize(**kwargs)
        if change == "context":
            owner.contexts.mark_state(journey, "revoked")
        elif change == "deadline":
            owner.clock[0] += timedelta(minutes=21)
        elif change == "grant-tuple":
            owner.store.record_verified_grant(grant(owner, "new-binding"))
        else:
            kind, identifier = {
                "principal": (RevocationScopeType.PRINCIPAL, "fixture-subject"),
                "installation": (RevocationScopeType.INSTALLATION, "installation-1"),
                "creator": (RevocationScopeType.CREATOR_ACCOUNT, "creator-1"),
                "binding": (RevocationScopeType.VERIFIED_GRANT, "binding"),
            }[change]
            owner.store.revoke(RevocationKey(kind, identifier))
        return result

    monkeypatch.setattr(finalize, "initialize_production_configuration", initialize_then_change)
    action = durable_finalize_action(
        lambda: owner.store,
        extension_id="abcdefghijklmnopabcdefghijklmnop",
        data_directory=tmp_path,
        now=lambda: owner.clock[0],
    )
    reason = action(association_request_id="request-1", detected_creator_account_id="creator-1",
                    reported_platform_creator_id=None, trusted_journey_id=journey)
    assert reason is not None
    assert owner.store.authorized_account_bindings() == ()
    assert not durable_completion_reader(lambda: owner.store, data_directory=tmp_path)()


@pytest.mark.parametrize("condition,expected", [
    ("approved", "finalization_ready"),
    ("authentication-required", "creator_approval_pending"),
    ("provider-expired", "recovery_required"),
])
def test_completed_binding_progress_tracks_current_provider_state(owner, condition, expected):
    from app.provisioning.progress import scoped_durable_provisioning_progress
    from test_installation_continuation_owner import snapshot, worker

    journey = completed(owner)
    if condition == "provider-expired":
        owner.clock[0] += timedelta(minutes=21)
    elif condition == "authentication-required":
        service, _, _, _ = worker(owner)
        service._snapshot(journey, snapshot(condition, 2))
    assert scoped_durable_provisioning_progress(lambda: owner.store)(journey)["stage"] == expected


@pytest.mark.parametrize("scope,identifier", [
    (RevocationScopeType.PRINCIPAL, "fixture-subject"),
    (RevocationScopeType.INSTALLATION, "installation-1"),
    (RevocationScopeType.CREATOR_ACCOUNT, "creator-1"),
    (RevocationScopeType.VERIFIED_GRANT, "binding"),
])
def test_completed_binding_progress_does_not_retry_finalization_after_revocation(owner, scope, identifier):
    from app.provisioning.progress import scoped_durable_provisioning_progress

    journey = completed(owner)
    owner.store.revoke(RevocationKey(scope, identifier))
    assert scoped_durable_provisioning_progress(lambda: owner.store)(journey)["stage"] == "recovery_required"


def test_expired_membership_can_still_enter_automatic_finalization_refresh(owner):
    from app.provisioning.progress import scoped_durable_provisioning_progress

    journey = completed(owner)
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE verified_grant_references SET expires_at=? WHERE reference_id='membership'",
                           ((owner.clock[0] + timedelta(seconds=5)).isoformat(),))
    owner.clock[0] += timedelta(seconds=10)
    assert scoped_durable_provisioning_progress(lambda: owner.store)(journey)["stage"] == "finalization_ready"
