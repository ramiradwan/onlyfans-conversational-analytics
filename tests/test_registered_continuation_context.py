"""Native continuation transactions use real SQLCipher; the key port is simulated."""

from concurrent.futures import ThreadPoolExecutor
import json
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.continuation_selection import ContinuationSelectionUnavailable
from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.provisioning.app import create_provisioning_app
from app.provisioning.progress import scoped_durable_provisioning_progress
from app.provisioning.session import (
    NATIVE_ENTRY_COOKIE_NAME, PROVISIONING_SESSION_COOKIE_NAME, PROVISIONING_ORIGIN,
    ProvisioningSessionManager, _session_csrf,
)
from app.security.installation_key import InstallationKeyUnavailable
from test_continuation_selection import NOW, add_candidate, enroll, finalize_fixture


pytestmark = [pytest.mark.ci_tier("integration")]
TOKEN = "t" * 32
PATH = "/api/v1/provisioning/native-entry"


def count(store, table):
    with store.database.read() as connection:
        return connection.execute("SELECT count(*) FROM " + table).fetchone()[0]


class ExistingKey:
    """Only the native reopening boundary is simulated, not durable storage."""
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.before_return = lambda: None

    def reopen_existing(self):
        self.calls += 1
        self.before_return()
        return self.store.installation_key_reference()

    def ensure_ready(self):
        pytest.fail("Continuation must never create or activate a key")


def app_for(owner, *, initial=None, continuation=None):
    def forbidden(**_):
        pytest.fail("Registered context reached an initial or global mutation")
    return create_provisioning_app(
        claim_submission=forbidden, creator_association_initiation=forbidden,
        creator_binding_acquisition=forbidden, completion_ready=lambda: False,
        finalize_action=forbidden, session_manager=owner.sessions,
        initial_enrollment=initial, registered_continuation=continuation,
        scoped_provisioning_progress=scoped_durable_provisioning_progress(lambda: owner.store),
    )


@pytest.fixture
def owner(tmp_path):
    clock = [NOW]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: clock[0])
    enroll(store)
    add_candidate(store)
    key = ExistingKey(store)
    journeys = OnboardingJourneyStore(store)
    sessions = ProvisioningSessionManager(TOKEN, journeys=journeys, continuation_key=key,
        monotonic=lambda: clock[0].timestamp())
    return SimpleNamespace(store=store, clock=clock, key=key, journeys=journeys,
        contexts=InstallationContinuationStore(store), sessions=sessions)


def request(owner, *, session=None, targeted=None):
    entry = owner.sessions.redeem_native_entry(owner.sessions.issue_native_entry(
        "Provisioning " + TOKEN, journey_id=targeted))
    cookie = f"{NATIVE_ENTRY_COOKIE_NAME}={entry.identifier}"
    if session is not None:
        cookie += f"; {PROVISIONING_SESSION_COOKIE_NAME}={session}"
    return entry, Request({"type": "http", "headers": [
        (b"host", b"bridge.localhost:17871"), (b"cookie", cookie.encode()),
        (b"origin", PROVISIONING_ORIGIN.encode()),
        (b"x-provisioning-csrf", _session_csrf(entry.identifier).encode()),
    ]})


def selected(owner):
    entry, incoming = request(owner)
    session = owner.sessions.select_native_workspace(incoming, None)
    return entry, session


def context_request(entry, session=None):
    cookie = f"{NATIVE_ENTRY_COOKIE_NAME}={entry.identifier}"
    if session:
        cookie += f"; {PROVISIONING_SESSION_COOKIE_NAME}={session}"
    return Request({"type": "http", "headers": [
        (b"host", b"bridge.localhost:17871"), (b"cookie", cookie.encode()),
    ]})


def test_native_selection_commits_exact_existing_identity_session_and_receipt(owner):
    original_key = owner.store.installation_key_reference()
    entry, session = selected(owner)
    current = owner.contexts.require_current(session.journey_id)
    assert current["kind"] == "registered-continuation"
    assert current["installation_id"] == "installation-1"
    assert current["scope"]["source_onboarding_transaction_id"] == "source-transaction"
    assert current["scope"]["association_request_id"] == "request-1"
    assert current["operation_id"] and current["state"] == "new"
    assert current["prepare_json"] is current["scope_json"] is None
    assert owner.store.installation_key_reference() == original_key
    assert owner.key.calls == 1
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 1
    assert owner.contexts.native_receipt(entry.identifier)["journey_id"] == session.journey_id
    assert owner.store.database.path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert owner.sessions.native_entry_context(context_request(entry)) == {"state": "unconfirmed"}
    assert owner.sessions.native_entry_context(context_request(entry, session.identifier)) == {
        "state": "selected", "journey_id": session.journey_id,
    }


def test_restart_reads_same_committed_result_only_with_existing_cookie(owner):
    entry, session = selected(owner)
    restarted = ProvisioningSessionManager(TOKEN, journeys=owner.journeys, continuation_key=owner.key,
        monotonic=lambda: owner.clock[0].timestamp())
    assert restarted.native_entry_context(context_request(entry)) == {"state": "unconfirmed"}
    assert restarted.native_entry_context(context_request(entry, session.identifier)) == {
        "state": "selected", "journey_id": session.journey_id,
    }
    assert owner.key.calls == 1
    owner.clock[0] += timedelta(seconds=301)
    with pytest.raises(HTTPException) as raised:
        restarted.native_entry_context(context_request(entry, session.identifier))
    assert raised.value.status_code == 401


def test_unknown_prepare_survives_store_restart_and_fresh_admission_without_new_operation(owner):
    _, original = selected(owner)
    first = owner.contexts.begin_prepare(original.journey_id)
    assert first["state"] == "prepare-unknown"
    reopened = SQLiteAuthenticationStore(owner.store.database.path, clock=lambda: owner.clock[0])
    contexts = InstallationContinuationStore(reopened)
    assert contexts.begin_prepare(original.journey_id) is None
    owner.clock[0] += timedelta(minutes=6)
    _, replacement = selected(owner)
    assert replacement.journey_id == original.journey_id
    assert contexts.require_current(replacement.journey_id)["operation_id"] == first["operation_id"]
    assert replacement.expires_at == original.expires_at
    assert contexts.require_current(replacement.journey_id)["scope"] == first["scope"]
    assert count(owner.store, "onboarding_journeys") == 1


@pytest.mark.parametrize("failure", ["key-refusal", "key-change", "candidate-change", "account-binding", "native-expiry"])
def test_changed_source_or_key_reopen_refuses_and_creates_nothing(owner, failure):
    if failure == "key-refusal":
        def changed():
            raise InstallationKeyUnavailable("Fixture refusal")
    elif failure == "native-expiry":
        def changed():
            owner.clock[0] += timedelta(minutes=6)
    else:
        def changed():
            with owner.store.database.transaction() as connection:
                if failure == "key-change":
                    connection.execute("UPDATE installation_key_reference SET provider_key_name='changed'")
                elif failure == "candidate-change":
                    connection.execute("UPDATE provisioning_candidates SET state='cancelled',resolved_at=? WHERE association_request_id='request-1'", (NOW.isoformat(),))
                else:
                    connection.execute("UPDATE provisioning_candidates SET state='approved',resolved_at=? WHERE association_request_id='request-1'", (NOW.isoformat(),))
            if failure == "account-binding":
                finalize_fixture(owner.store)
    owner.key.before_return = changed
    entry, incoming = request(owner)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None)
    assert entry.consumed is False
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 0
    assert count(owner.store, "onboarding_native_selection_receipts") == 0


def test_final_native_check_rolls_back_every_record(owner, monkeypatch):
    entry, incoming = request(owner)
    original = owner.contexts.journeys.record_session
    def expiring(**kwargs):
        original(**kwargs)
        owner.clock[0] += timedelta(minutes=6)
    monkeypatch.setattr(owner.sessions._continuations.journeys, "record_session", expiring)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, None)
    assert entry.consumed is False
    assert count(owner.store, "onboarding_journeys") == count(owner.store, "provisioning_browser_sessions") == 0
    assert count(owner.store, "installation_continuation_contexts") == 0


def test_two_independent_native_entries_commit_one_context_under_sqlite_lock(owner):
    barrier = Barrier(2)
    owner.key.before_return = lambda: barrier.wait(timeout=10)
    other = ProvisioningSessionManager(TOKEN, journeys=owner.journeys, continuation_key=owner.key,
        monotonic=lambda: owner.clock[0].timestamp())
    second_owner = SimpleNamespace(sessions=other)
    _, a = request(owner)
    _, b = request(second_owner)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(owner.sessions.select_native_workspace, a, None)
        second = pool.submit(other.select_native_workspace, b, None)
        sessions = first.result(timeout=20), second.result(timeout=20)
    assert sessions[0].journey_id == sessions[1].journey_id
    assert sessions[0].identifier != sessions[1].identifier
    assert count(owner.store, "onboarding_journeys") == 1
    assert count(owner.store, "provisioning_browser_sessions") == 2


def test_same_entry_replay_and_existing_session_never_reselect(owner):
    native, incoming = request(owner)
    session = owner.sessions.select_native_workspace(incoming, None)
    with pytest.raises(HTTPException) as raised:
        owner.sessions.select_native_workspace(incoming, None)
    assert raised.value.status_code == 409
    owner.store.approve_provisioning_candidate("request-1", resolved_at=NOW)
    add_candidate(owner.store, "2")
    retained_entry, incoming = request(owner, session=session.identifier)
    kept = owner.sessions.select_native_workspace(incoming, None)
    assert kept == session and owner.key.calls == 1
    assert native.consumed is True
    restarted = ProvisioningSessionManager(TOKEN, journeys=owner.journeys, continuation_key=owner.key,
        monotonic=lambda: owner.clock[0].timestamp())
    assert restarted.native_entry_context(context_request(retained_entry, session.identifier)) == {
        "state": "selected", "journey_id": session.journey_id,
    }
    assert restarted.native_entry_context(context_request(retained_entry)) == {"state": "unconfirmed"}


def test_scoped_progress_keeps_approved_target_beside_new_pending_account(owner):
    from test_installation_continuation_owner import grant

    owner.store.approve_provisioning_candidate("request-1", resolved_at=NOW)
    _, session = selected(owner)
    add_candidate(owner.store, "2")
    read = scoped_durable_provisioning_progress(lambda: owner.store)
    assert read(session.journey_id)["stage"] == "creator_approval_pending"
    owner.contexts.begin_prepare(session.journey_id)
    owner.contexts.bind_result(session.journey_id,
        continuation_id="01900000-0000-7000-8000-000000000099",
        expires_at=(NOW + timedelta(minutes=20)).isoformat(),
        epoch="provider-epoch", revision=1, provider_state="approved")
    owner.store.record_verified_grant(grant(owner))
    owner.contexts.mark_state(session.journey_id, "completed")
    assert read(session.journey_id) == {"stage": "finalization_ready",
        "association_request_id": "request-1", "creator_account_id": "creator-1"}
    _, reopened = selected(owner)
    assert reopened.journey_id == session.journey_id
    finalize_fixture(owner.store, revoked=True)
    assert read(session.journey_id)["stage"] == "recovery_required"


def test_initial_scoped_status_keeps_its_exact_candidate_beside_new_pending(owner):
    owner.store.approve_provisioning_candidate("request-1", resolved_at=NOW)
    add_candidate(owner.store, "2")
    initial = owner.journeys.open()
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET installation_id=?,scope_json=? WHERE journey_id=?", (
            "installation-1", json.dumps({"association_request_id": "request-1", "organization_id": "organization-1",
                "onboarding_transaction_id": "source-transaction", "intended_creator_id": "creator-1"}), initial["journey_id"],
        ))
    assert scoped_durable_provisioning_progress(lambda: owner.store)(initial["journey_id"]) == {
        "stage": "finalization_ready", "association_request_id": "request-1", "creator_account_id": "creator-1",
    }


def test_consumed_claim_without_candidate_keeps_creator_confirmation(owner):
    owner.store.cancel_provisioning_candidate("request-1", resolved_at=NOW)
    _, session = selected(owner)
    assert owner.journeys.require_current(session.journey_id)["kind"] == "initial-enrollment"
    assert scoped_durable_provisioning_progress(lambda: owner.store)(session.journey_id)["stage"] == "creator_confirmation_required"
    assert owner.key.calls == 0


def test_ambiguous_saved_accounts_refuse_before_reopening_key(owner):
    owner.store.approve_provisioning_candidate("request-1", resolved_at=NOW)
    add_candidate(owner.store, "2", approved=True)
    _, incoming = request(owner)
    with pytest.raises(HTTPException) as raised:
        owner.sessions.select_native_workspace(incoming, None)
    assert raised.value.status_code == 409 and owner.key.calls == 0
    assert count(owner.store, "onboarding_journeys") == 0


def test_expiry_deletes_context_without_initial_recovery_or_cached_session_authority(owner):
    _, session = selected(owner)
    owner.contexts.begin_prepare(session.journey_id)
    owner.clock[0] += timedelta(minutes=31)
    assert owner.journeys.get(session.journey_id) is None
    assert count(owner.store, "installation_continuation_contexts") == 0
    assert count(owner.store, "onboarding_workspace_recovery") == count(owner.store, "onboarding_uncertain_receipts") == 0
    with pytest.raises(HTTPException):
        owner.sessions.require_session(context_request(SimpleNamespace(identifier=""), session.identifier))
    _, replacement = selected(owner)
    assert replacement.journey_id != session.journey_id
    assert owner.contexts.require_current(replacement.journey_id)["installation_id"] == "installation-1"


def test_context_expiry_does_not_revive_within_initial_recovery(owner):
    _, session = selected(owner)
    _, incoming = request(owner, targeted=session.journey_id)
    with pytest.raises(HTTPException):
        owner.sessions.select_native_workspace(incoming, session.journey_id, recover=True)
    assert count(owner.store, "onboarding_journeys") == 1


def test_provider_result_pins_identity_then_epoch_and_refuses_retargeting(owner):
    _, session = selected(owner)
    owner.contexts.begin_prepare(session.journey_id)
    expiry = (NOW + timedelta(minutes=20)).isoformat()
    first = owner.contexts.bind_result(session.journey_id, continuation_id="provider-1", expires_at=expiry, reference="locator")
    assert first["epoch"] is None
    owner.contexts.bind_result(session.journey_id, continuation_id="provider-1", expires_at=expiry,
        epoch="epoch-1", revision=3, provider_state="awaiting-owner")
    for change in ({"continuation_id": "provider-2"}, {"epoch": "epoch-2"}, {"revision": 2},
                   {"expires_at": (NOW+timedelta(minutes=21)).isoformat()}):
        with pytest.raises(OnboardingJourneyUnavailable):
            owner.contexts.bind_result(session.journey_id, **{
                "continuation_id": "provider-1", "expires_at": expiry, "epoch": "epoch-1", "revision": 3,
                "provider_state": "awaiting-owner", **change,
            })
    assert owner.contexts.begin_prepare(session.journey_id) is None
    owner.contexts.mark_state(session.journey_id, "completed")
    duplicate = owner.contexts.bind_result(session.journey_id, continuation_id="provider-1", expires_at=expiry,
        epoch="epoch-1", revision=3, provider_state="awaiting-owner")
    assert duplicate["state"] == "completed"
    with pytest.raises(OnboardingJourneyUnavailable):
        owner.contexts.bind_result(session.journey_id, continuation_id="provider-1", expires_at=expiry,
            epoch="epoch-1", revision=3, provider_state="approved", state="completed")
    owner.contexts.mark_state(session.journey_id, "revoked")
    with pytest.raises(OnboardingJourneyUnavailable):
        owner.contexts.mark_state(session.journey_id, "completed")
    with pytest.raises(OnboardingJourneyUnavailable):
        owner.journeys.update(session.journey_id, state="new", operation_id=owner.journeys.new_operation())


def test_only_one_concurrent_owner_claims_prepare_send(owner):
    _, session = selected(owner)
    barrier = Barrier(2)
    def prepare():
        barrier.wait(timeout=10)
        return owner.contexts.begin_prepare(session.journey_id)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(prepare), pool.submit(prepare)
        outcomes = (a.result(timeout=20), b.result(timeout=20))
    assert sum(value is not None for value in outcomes) == 1
    assert owner.contexts.require_current(session.journey_id)["state"] == "prepare-unknown"


def test_registered_http_context_cannot_call_initial_mutations_or_global_binding(owner):
    _, session = selected(owner)
    headers = {"Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}",
        "Origin": PROVISIONING_ORIGIN, "X-Provisioning-CSRF": session.csrf_token}
    with TestClient(app_for(owner), base_url=PROVISIONING_ORIGIN) as browser:
        for path, body in [
            ("/api/v1/provisioning/initial-handoff", {}),
            ("/api/v1/provisioning/claim", {"package": "fixture"}),
            ("/api/v1/provisioning/creator-association", {"detected_creator_account_id": "other"}),
            ("/api/v1/provisioning/creator-association/acquire", {}),
            ("/api/v1/provisioning/finalize", {"association_request_id": "other", "detected_creator_account_id": "other"}),
        ]:
            assert browser.post(path, json=body, headers=headers).status_code == 409
        assert browser.get("/api/v1/provisioning/initial-handoff", headers=headers).status_code == 409
        assert browser.get("/api/v1/provisioning/status", headers=headers).json()["creator_account_id"] == "creator-1"


def test_registered_events_dispatch_only_its_owner(owner, monkeypatch):
    from app.provisioning.events import events
    called = []
    def initial_resume(_):
        pytest.fail("Registered context resumed initial enrollment")
    registered = SimpleNamespace(resume=lambda journey: called.append(journey))
    initial = SimpleNamespace(resume=initial_resume)
    async def finite(*args, **kwargs):
        yield "event: test\ndata: {}\n\n"
    monkeypatch.setattr(events, "stream", finite)
    _, session = selected(owner)
    app = create_provisioning_app(claim_submission=lambda **_: None, creator_association_initiation=lambda **_: None,
        creator_binding_acquisition=lambda: None, completion_ready=lambda: False, finalize_action=lambda **_: None,
        session_manager=owner.sessions, initial_enrollment=initial, registered_continuation=registered,
        onboarding_snapshot=lambda _: {})
    with TestClient(app, base_url=PROVISIONING_ORIGIN) as browser:
        response = browser.get("/api/v1/provisioning/events", headers={
            "Cookie": f"{PROVISIONING_SESSION_COOKIE_NAME}={session.identifier}"})
        assert response.status_code == 200 and called == [session.journey_id]


def test_initial_owner_itself_refuses_registered_context_before_any_key_work(owner):
    import asyncio
    from app.provisioning.initial_handoff import InitialInstallationEnrollment
    from app.security.initial_handoff import InitialHandoffRefused
    initial = InitialInstallationEnrollment(owner.store, hosted_origin="https://setup.example",
        hosted_start_url="https://setup.example/public/onboarding/setup/start",
        client=SimpleNamespace(key=owner.key, transport=SimpleNamespace()))
    _, session = selected(owner)
    with pytest.raises(InitialHandoffRefused):
        asyncio.run(initial.prepare(session.journey_id))
    initial.resume(session.journey_id)
    assert initial._tasks == {}
