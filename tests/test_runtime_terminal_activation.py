"""Expired waiting contexts reconcile only their exact noncreating outcome."""
from datetime import timedelta
import json

from test_registered_continuation_context import owner
from test_runtime_activation_entry import ACTOR, admission, installed
from test_installation_continuation_owner import CONTEXT, EPOCH, worker, until

def retired_waiting(owner, *, cleanup=True, elapsed_minutes=31):
    original = installed(owner)
    owner.clock[0] += timedelta(minutes=21)
    row = admission(owner, original)
    journey = row["journey_id"]
    owner.contexts.begin_prepare(journey)
    owner.contexts.bind_result(journey, continuation_id=CONTEXT,
        expires_at=(owner.clock[0] + timedelta(minutes=29)).isoformat(), epoch=EPOCH,
        revision=0, provider_state="awaiting-owner")
    owner.clock[0] += timedelta(minutes=elapsed_minutes)
    if cleanup:
        assert owner.journeys.get(journey) is None
    return journey


def evidence(owner, journey):
    with owner.store.database.read() as connection:
        return json.loads(connection.execute("SELECT evidence_json FROM onboarding_presentation_retirements WHERE journey_id=?",
            (journey,)).fetchone()[0])


def change_evidence(owner, journey, change):
    value = evidence(owner, journey)
    change(value)
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_presentation_retirements SET evidence_json=? WHERE journey_id=?",
            (json.dumps(value, sort_keys=True, separators=(",", ":")), journey))


def options(journey, *, authorize=lambda: None, prepare=False):
    return dict(previous_journey_id=journey, creator_account_id="creator-1", actor_digest=ACTOR,
        authorize=authorize, prepare=prepare)
