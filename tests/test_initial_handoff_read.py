"""Read-only recovery of the exact saved desktop handoff."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.security.initial_handoff import PROFILE
from test_onboarding_enrollment_runtime import _enrollment_worker, _HandoffFixture, count
from test_webauthn_routes import _seeded_store, INSTANT

pytestmark = [pytest.mark.ci_tier("integration")]


def owner(tmp_path):
    store = _seeded_store(tmp_path / "auth.sqlite3", INSTANT)
    client = _HandoffFixture()
    worker = _enrollment_worker(store, client)
    return worker, client


def waiting(worker):
    journey = worker.journeys.open()["journey_id"]
    worker.journeys.update(journey, state="waiting", handoff_reference="a" * 43,
        prepare_json=json.dumps({"profile": PROFILE}),
        handoff_expires_at=(INSTANT + timedelta(minutes=10)).isoformat())
    return journey


@pytest.mark.asyncio
async def test_saved_entry_read_never_creates_prepares_signs_or_resumes(tmp_path):
    worker, client = owner(tmp_path)
    missing = str(uuid4())
    assert await worker.read_browser_entry(missing) == {"state": "unknown", "journey_id": missing}
    assert count(worker.store, "onboarding_journeys") == 0
    journey = waiting(worker)
    before = worker.journeys.get(journey)
    result = await worker.read_browser_entry(journey)
    assert result == {"state": "waiting", "journey_id": journey,
        "handoff_reference": "a" * 43}
    assert worker.journeys.get(journey) == before
    assert client.preparations == client.waits == []
    assert worker._tasks == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation", [None, "{", "[]", json.dumps({"profile": "urn:bridge-clean:initial-installation-handoff:v1"})])
@pytest.mark.parametrize("state", ["waiting", "unknown", "completing", "prepare-unknown"])
async def test_missing_malformed_or_v1_preparation_is_never_relabelled_as_current_session_setup(tmp_path, preparation, state):
    from app.security.initial_handoff import InitialHandoffRefused
    worker, client = owner(tmp_path)
    journey = waiting(worker)
    worker.journeys.update(journey, state=state, prepare_json=preparation)
    before = worker.journeys.get(journey)
    assert await worker.read_browser_entry(journey) == {"state": "unknown", "journey_id": journey}
    with pytest.raises(InitialHandoffRefused) as error:
        await worker.prepare(journey)
    assert error.value.code == "unsupported_profile"
    worker.resume(journey)
    await worker._run(journey)
    assert worker.journeys.get(journey) == before
    assert client.preparations == client.waits == []
    assert worker._tasks == {}


@pytest.mark.asyncio
async def test_completed_old_enrollment_keeps_its_registered_continuity_without_initial_v2_proof(tmp_path, monkeypatch):
    worker, client = owner(tmp_path)
    journey = waiting(worker)
    worker.journeys.update(journey, state="completed", prepare_json=None)
    continued = []
    async def continuity(row):
        continued.append(row["journey_id"])
    monkeypatch.setattr(worker, "_continuity", continuity)
    await worker._run(journey)
    assert continued == [journey]
    assert client.preparations == client.waits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["new", "preparing", "prepare-unknown", "completing", "unknown", "completed",
    "expired-locator", "expired-journey", "bad-reference", "receiving", "continuation"])
async def test_read_never_forwards_ineligible_or_receiving_entry(tmp_path, variant):
    worker, client = owner(tmp_path)
    journey = waiting(worker)
    if variant == "expired-locator":
        worker.journeys.update(journey, state="waiting", handoff_expires_at=INSTANT.isoformat())
    elif variant == "expired-journey":
        worker.store._now = lambda: INSTANT + timedelta(minutes=30)
    elif variant == "bad-reference":
        worker.journeys.update(journey, state="waiting", handoff_reference="invalid")
    elif variant in {"receiving", "continuation"}:
        with worker.store.database.transaction() as connection:
            connection.execute("INSERT INTO onboarding_transfer_intents (journey_id,request_json,previous_state,expires_at,continuation_json) VALUES (?,?,?,?,?)",
                (journey, "{}", "waiting", (INSTANT + timedelta(minutes=5)).timestamp(),
                 "{}" if variant == "continuation" else None))
    else:
        worker.journeys.update(journey, state=variant)
    assert await worker.read_browser_entry(journey) == {"state": "unknown", "journey_id": journey}
    assert client.preparations == client.waits == []
    assert worker._tasks == {}


@pytest.mark.asyncio
async def test_read_waits_for_prepare_lock_then_observes_commit(tmp_path):
    worker, _ = owner(tmp_path)
    journey = worker.journeys.open()["journey_id"]
    await worker._lock.acquire()
    read = asyncio.create_task(worker.read_browser_entry(journey))
    await asyncio.sleep(0)
    assert not read.done()
    worker.journeys.update(journey, state="waiting", handoff_reference="a" * 43,
        prepare_json=json.dumps({"profile": PROFILE}),
        handoff_expires_at=(INSTANT + timedelta(minutes=10)).isoformat())
    worker._lock.release()
    assert (await read)["state"] == "waiting"
