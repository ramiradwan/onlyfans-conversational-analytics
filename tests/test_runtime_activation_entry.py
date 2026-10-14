"""Runtime commercial entry preserves installed account and command outcomes."""
from datetime import timedelta
from dataclasses import replace
import asyncio
import json

import pytest

from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.provisioning.owner_effects import OwnerEffects
from threading import Event
from test_registered_continuation_context import owner
from test_continuation_selection import finalize_fixture
from test_installation_continuation_owner import approved, grant, worker, until, snapshot

pytestmark = [pytest.mark.ci_tier("integration")]
ACTOR = "a" * 64


def installed(owner):
    journey = approved(owner)
    owner.contexts.complete_binding(journey, grant(owner), membership_reference_id="membership")
    finalize_fixture(owner.store)
    return journey


def admission(owner, journey, *, create=True, actor="creator-1"):
    return owner.contexts.runtime_activation_context(previous_journey_id=journey,
        creator_account_id=actor, reopened_key=owner.key.reopen_existing() if create else None,
        authorize=lambda: None, create=create)


def test_runtime_admission_reuses_live_context_without_native_or_installation_changes(owner):
    previous = installed(owner)
    key = owner.store.installation_key_reference()
    candidate = owner.store.provisioning_candidate("request-1")
    with owner.store.database.read() as connection:
        sessions = list(connection.execute("SELECT * FROM provisioning_browser_sessions"))
        receipts = list(connection.execute("SELECT * FROM onboarding_native_selection_receipts"))
    row = admission(owner, previous)
    assert row["journey_id"] == previous and row["state"] == "completed"
    assert owner.store.installation_key_reference() == key
    assert owner.store.provisioning_candidate("request-1") == candidate
    with owner.store.database.read() as connection:
        assert list(connection.execute("SELECT * FROM provisioning_browser_sessions")) == sessions
        assert list(connection.execute("SELECT * FROM onboarding_native_selection_receipts")) == receipts


@pytest.mark.parametrize("local_expired", [False, True])
def test_runtime_entry_renews_expired_context_once_with_exact_saved_account(owner, local_expired):
    previous = installed(owner)
    owner.clock[0] += timedelta(minutes=31 if local_expired else 21)
    original = owner.store.installation_key_reference()
    first = admission(owner, previous)
    second = admission(owner, previous)
    assert first["journey_id"] != previous and second["journey_id"] == first["journey_id"]
    assert second["scope"]["creator_account_id"] == "creator-1"
    assert second["scope"]["source_onboarding_transaction_id"] == "source-transaction"
    assert second["target"].finalized and second["target"].candidate.state.value == "approved"
    assert owner.store.installation_key_reference() == original
    assert admission(owner, previous, create=False)["journey_id"] == first["journey_id"]


def test_runtime_entry_refuses_other_actor_key_revocation_or_unknown_source(owner):
    previous = installed(owner)
    with pytest.raises(OnboardingJourneyUnavailable):
        admission(owner, previous, actor="creator-other")
    with pytest.raises(OnboardingJourneyUnavailable):
        owner.contexts.runtime_activation_context(previous_journey_id=previous, creator_account_id="creator-1",
            reopened_key=replace(owner.store.installation_key_reference(), installation_key_id="other"),
            authorize=lambda: None, create=True)
    with pytest.raises(OnboardingJourneyUnavailable):
        admission(owner, "11111111-1111-4111-8111-111111111111")
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE authorized_account_bindings SET revoked_at=?", (owner.clock[0].isoformat(),))
    with pytest.raises(OnboardingJourneyUnavailable):
        admission(owner, previous)


def test_transition_receipt_is_bound_to_actor_scope_and_deadline(owner):
    previous = installed(owner)
    owner.clock[0] += timedelta(minutes=21)
    current = admission(owner, previous)
    receipt = owner.contexts.runtime_activation_receipt(previous_journey_id=previous,
        journey_id=current["journey_id"], actor_digest=ACTOR, authorize=lambda: None, create=True)
    assert receipt["journey_id"] == current["journey_id"]
    assert owner.contexts.runtime_activation_receipt(previous_journey_id=previous,
        journey_id=current["journey_id"], actor_digest="b"*64, authorize=lambda: None,
        receipt_id=receipt["receipt_id"]) is None
    replay = owner.contexts.runtime_activation_receipt(previous_journey_id=previous,
        journey_id=current["journey_id"], actor_digest=ACTOR, authorize=lambda: None, create=True)
    assert replay == receipt
    owner.clock[0] += timedelta(seconds=301)
    assert owner.contexts.runtime_activation_receipt(previous_journey_id=previous,
        journey_id=current["journey_id"], actor_digest=ACTOR, authorize=lambda: None,
        receipt_id=receipt["receipt_id"]) is None


def test_saved_source_cannot_hide_a_changed_selection_generation(owner):
    previous = installed(owner)
    owner.clock[0] += timedelta(minutes=31)
    owner.journeys.get(previous)
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE provisioning_candidates SET requested_at=?", (owner.clock[0].isoformat(),))
    with pytest.raises(OnboardingJourneyUnavailable):
        admission(owner, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
async def test_projection_work_remains_visible_without_blocking_protected_inventory(protected):
    effects = OwnerEffects()
    started, finish = Event(), Event()
    def work():
        started.set()
        finish.wait(3)
    task = asyncio.create_task(effects.run(work, journey_id="11111111-1111-4111-8111-111111111111", protected=protected))
    try:
        await until(started.is_set)
        assert effects.accepted_work()
        assert bool(effects.accepted_protected_work()) is protected
    finally:
        finish.set()
        await task
        await effects.drain()


def retired_source(owner):
    journey = installed(owner)
    owner.clock[0] += timedelta(minutes=31)
    assert owner.journeys.get(journey) is None
    return journey


@pytest.mark.parametrize("shadow_expired", [False, True])
def test_runtime_entry_correlates_only_exact_retired_scope_behind_an_empty_draft(owner, shadow_expired):
    previous = retired_source(owner)
    shadow = owner.journeys.open(previous)
    assert shadow["kind"] == "initial-enrollment" and shadow["state"] == "new"
    with owner.store.database.read() as connection:
        evidence = connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?",
            (previous,)).fetchone()[0]
    if shadow_expired:
        owner.clock[0] += timedelta(minutes=31)
        assert owner.journeys.get(previous) is None
    assert admission(owner, previous, create=False) is None
    first = admission(owner, previous)
    second = admission(owner, previous)
    assert first["journey_id"] != previous and first["journey_id"] == second["journey_id"]
    assert first["scope"]["installation_id"] == "installation-1" and first["scope"]["creator_account_id"] == "creator-1"
    assert owner.journeys.get(previous) == (None if shadow_expired else shadow)
    with owner.store.database.read() as connection:
        assert connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?",
            (previous,)).fetchone()[0] == evidence
    receipt = owner.contexts.runtime_activation_receipt(previous_journey_id=previous, journey_id=first["journey_id"],
        actor_digest=ACTOR, authorize=lambda: None, create=True)
    assert receipt["previous_journey_id"] == previous and receipt["journey_id"] == first["journey_id"]


@pytest.mark.parametrize("change", ["no-history", "other-history", "wrong-generation", "wrong-scope", "unresolved-history",
    "unapproved-history", "history-expired", "draft-operation", "draft-scope", "native-session", "transfer", "other-actor"])
def test_empty_draft_never_authorizes_activation_or_clears_protected_history(owner, change):
    previous = retired_source(owner)
    owner.journeys.open(previous)
    with owner.store.database.transaction() as connection:
        evidence = json.loads(connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?",
            (previous,)).fetchone()[0])
        if change == "no-history":
            connection.execute("DELETE FROM onboarding_presentation_retirements WHERE journey_id=?", (previous,))
        elif change == "history-expired":
            connection.execute("UPDATE onboarding_presentation_retirements SET expires_at=? WHERE journey_id=?",
                (owner.clock[0].timestamp()-1, previous))
        elif change == "draft-operation":
            connection.execute("UPDATE onboarding_journeys SET operation_id=? WHERE journey_id=?",
                (owner.journeys.new_operation(), previous))
        elif change == "draft-scope":
            connection.execute("UPDATE onboarding_journeys SET scope_json='{}' WHERE journey_id=?", (previous,))
        elif change == "native-session":
            connection.execute("INSERT INTO provisioning_browser_sessions VALUES (?,?,?,?)",
                ("s"*64, "c"*64, previous, owner.clock[0].timestamp()-1))
        elif change == "transfer":
            connection.execute("INSERT INTO onboarding_transfer_intents(journey_id,request_json,previous_state,expires_at) VALUES (?,?,?,?)",
                (previous, "{}", "new", owner.clock[0].timestamp()+30))
        elif change != "other-actor":
            if change == "other-history":
                evidence["journey"]["journey_id"] = "11111111-1111-4111-8111-111111111111"
            elif change == "wrong-generation":
                evidence["context"]["selection_json"] = "{}"
            elif change == "wrong-scope":
                evidence["context"]["scope_json"] = "{}"
            elif change == "unresolved-history":
                evidence["unresolved_work"] = ["journey-operation"]
            elif change == "unapproved-history":
                evidence["context"]["provider_state"] = "awaiting-approval"
            connection.execute("UPDATE onboarding_presentation_retirements SET evidence_json=? WHERE journey_id=?",
                (json.dumps(evidence), previous))
        before = tuple(tuple(row) for row in connection.execute("SELECT * FROM onboarding_journeys"))
    for create in (False, True):
        with pytest.raises(OnboardingJourneyUnavailable):
            admission(owner, previous, create=create, actor="creator-other" if change == "other-actor" else "creator-1")
    with owner.store.database.read() as connection:
        assert tuple(tuple(row) for row in connection.execute("SELECT * FROM onboarding_journeys")) == before
        assert connection.execute("SELECT count(*) FROM installation_continuation_contexts").fetchone()[0] == 0
