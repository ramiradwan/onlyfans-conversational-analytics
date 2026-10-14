"""Native continuation transactions use real SQLCipher; the key port is simulated."""

from concurrent.futures import ThreadPoolExecutor
import json
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.continuation_selection import ContinuationSelectionUnavailable
from app.persistence.installation_continuation import InstallationContinuationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.provisioning.progress import scoped_durable_provisioning_progress
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


@pytest.fixture
def owner(tmp_path):
    clock = [NOW]
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: clock[0])
    enroll(store)
    add_candidate(store)
    key = ExistingKey(store)
    journeys = OnboardingJourneyStore(store)
    return SimpleNamespace(store=store, clock=clock, key=key, journeys=journeys,
        contexts=InstallationContinuationStore(store))


def selected(owner):
    with owner.store.database.transaction() as connection:
        journey = owner.contexts.admit_desktop_context(connection, journey_id=None, reopen_key=owner.key.reopen_existing)
    return None, SimpleNamespace(journey_id=journey, expires_at=owner.journeys.require_current(journey)["expires_at"])


def test_typed_source_retains_registered_binding_and_refuses_expired_authority(owner):
    from dataclasses import FrozenInstanceError
    from app.provisioning.setup_sources import SetupSourceLifecycle
    _, selected_source = selected(owner)
    sources = SetupSourceLifecycle(owner.store, owner.journeys, owner.contexts)
    with pytest.raises(OnboardingJourneyUnavailable):
        sources.current(None)
    with owner.store.database.transaction() as connection:
        original = sources.current(selected_source.journey_id, connection=connection)
        assert original == sources.resolve(selected_source.journey_id, connection=connection)
        bound = owner.contexts.require_current(selected_source.journey_id, connection=connection)["scope"]
        assert original.kind == "registered-continuation" and original.provenance == "current"
        assert original.binding.creator_account_id == bound["creator_account_id"]
        assert original.binding.installation_key_id == bound["installation_key_id"]
        assert original.binding.installation_key_jkt is None  # Not invented from the current key.
        with pytest.raises(FrozenInstanceError):
            original.installation_id = "replacement"
    owner.clock[0] += timedelta(hours=1)
    with pytest.raises(OnboardingJourneyUnavailable):
        sources.current(selected_source.journey_id)
    expired = sources.resolve(selected_source.journey_id)
    assert expired.expires_at == original.expires_at
    assert expired.binding == original.binding and expired.terminal_read is None
    assert count(owner.store, "onboarding_journeys") == 1


def test_typed_retirement_preserves_registered_terminal_proof(owner):
    from app.persistence.installation_continuation import CONTINUATION_PROFILE
    from app.provisioning.setup_sources import RecordedRegisteredTerminalRead, SetupSourceLifecycle
    owner.store.approve_provisioning_candidate("request-1", resolved_at=NOW)
    finalize_fixture(owner.store)
    _, source = selected(owner)
    owner.contexts.begin_prepare(source.journey_id)
    original = owner.contexts.bind_result(source.journey_id, continuation_id="provider-1",
        expires_at=(NOW + timedelta(minutes=20)).isoformat(), reference="locator",
        epoch="epoch-1", revision=1, provider_state="awaiting-owner")
    owner.clock[0] += timedelta(minutes=31)
    with owner.store.database.transaction() as connection:
        owner.journeys._cleanup(connection, owner.clock[0])
    proof_scope = owner.contexts.runtime_terminal_read_scope(previous_journey_id=source.journey_id,
        creator_account_id="creator-1", authorize=lambda: None)
    owner.contexts.commit_runtime_terminal_closure(source=proof_scope, creator_account_id="creator-1",
        actor_digest="a" * 64, response={"profile": CONTINUATION_PROFILE,
            "operation_id": original["operation_id"], "result": "expired"}, authorize=lambda: None)
    resolution = SetupSourceLifecycle(owner.store, owner.journeys, owner.contexts).resolve(source.journey_id)
    assert resolution.provenance == "retired"
    assert resolution.expires_at.isoformat() == source.expires_at
    assert resolution.terminal_read == RecordedRegisteredTerminalRead(proof_scope["source_sha256"],
        original["operation_id"], owner.clock[0], "a" * 64)
    assert count(owner.store, "onboarding_journeys") == 0


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


def test_cancelled_candidate_cannot_be_replaced_by_fresh_native_admission(owner):
    owner.store.cancel_provisioning_candidate("request-1", resolved_at=NOW)
    with pytest.raises(ContinuationSelectionUnavailable):
        selected(owner)
    assert owner.key.calls == 0


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


def test_initial_owner_itself_refuses_registered_context_before_any_key_work(owner):
    import asyncio
    from app.provisioning.initial_handoff import InitialInstallationEnrollment
    from app.security.initial_handoff import InitialHandoffRefused
    initial = InitialInstallationEnrollment(owner.store, hosted_origin="https://setup.example",

        client=SimpleNamespace(key=owner.key, transport=SimpleNamespace()))
    _, session = selected(owner)
    with pytest.raises(InitialHandoffRefused):
        asyncio.run(initial.prepare(session.journey_id))
    initial.resume(session.journey_id)
    assert initial._tasks == {}


def test_expired_unused_desktop_source_is_classified_then_explicitly_replaced(owner):
    _, original = selected(owner)
    before = owner.journeys.require_current(original.journey_id)
    owner.clock[0] += timedelta(minutes=31)
    with owner.store.database.transaction() as connection:
        proposal = owner.contexts.desktop_recovery_proposal(connection, journey_id=original.journey_id)
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 1
        successor = owner.contexts.admit_desktop_recovery(connection, proposal=proposal, reopen_key=owner.key.reopen_existing)
        old = dict(connection.execute("SELECT * FROM onboarding_journeys WHERE journey_id=?", (original.journey_id,)).fetchone())
    assert old == before
    assert successor != original.journey_id
    resumed = owner.contexts.require_current(successor)
    assert resumed["scope"]["creator_account_id"] == "creator-1"
    assert resumed["operation_id"] != before["operation_id"]
    with pytest.raises(OnboardingJourneyUnavailable):
        with owner.store.database.transaction() as connection:
            owner.contexts.admit_desktop_recovery(connection, proposal=proposal, reopen_key=owner.key.reopen_existing)


@pytest.mark.parametrize("state", ["prepare-unknown", "waiting", "completing", "unknown"])
def test_expired_uncertain_source_is_never_replaced(owner, state):
    _, original = selected(owner)
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE onboarding_journeys SET state=? WHERE journey_id=?", (state, original.journey_id))
    owner.clock[0] += timedelta(minutes=31)
    with pytest.raises(OnboardingJourneyUnavailable):
        with owner.store.database.transaction() as connection:
            owner.contexts.desktop_recovery_proposal(connection, journey_id=original.journey_id)
    assert count(owner.store, "onboarding_journeys") == 1


def test_desktop_recovery_rechecks_exact_classification_and_worker_guard(owner):
    _, original = selected(owner)
    owner.clock[0] += timedelta(minutes=31)
    with owner.store.database.transaction() as connection:
        proposal = owner.contexts.desktop_recovery_proposal(connection, journey_id=original.journey_id)
        with pytest.raises(OnboardingJourneyUnavailable):
            owner.contexts.admit_desktop_recovery(connection, proposal=proposal,
                reopen_key=owner.key.reopen_existing, work=("worker:" + original.journey_id,))
        with pytest.raises(OnboardingJourneyUnavailable):
            owner.contexts.admit_desktop_recovery(connection, proposal={**proposal, "creator_account_id": "other"},
                reopen_key=owner.key.reopen_existing)
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0] == 1


def test_native_account_command_resumes_only_the_explicit_classified_source(owner):
    from uuid import uuid4
    from app.onboarding.admission import ScopedAdmissions
    from app.onboarding.coordinator import DesktopOnboardingCoordinator
    from app.provisioning.desktop_onboarding import ProvisioningSetupOwner
    from test_desktop_browser_tasks import observation
    from test_desktop_coordinator import command
    _, original = selected(owner)
    root = str(uuid4())
    observed = observation(SimpleNamespace(task_id=str(uuid4()), journey_id=root, generation=str(uuid4())))
    observed["creator_account_id"] = "creator-1"
    browser = SimpleNamespace(extension_id="a" * 32, read=lambda _: observed)
    setup_owner = ProvisioningSetupOwner(owner.store, browser_tasks=browser, ceremonies=None,
        initial=None, registered=None, key=owner.key, finalize=lambda **_: None, completion_ready=lambda: False)
    admissions = ScopedAdmissions(owner.store, stage="setup")
    native = admissions.consume(admissions.prepare_desktop(root), role="desktop")
    with owner.store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_journeys (journey_id,revision,created_at,source_journey_id) VALUES (?,0,?,?)",
            (root, owner.clock[0].timestamp(), original.journey_id))
    owner.clock[0] += timedelta(minutes=31)
    native = admissions.consume(admissions.prepare_desktop(), role="desktop")
    coordinator = DesktopOnboardingCoordinator(admissions, setup_owner)
    snapshot = coordinator.read(native)
    assert snapshot["continuation_available"]
    assert "account.connect" in snapshot["allowed_actions"]
    operation, fresh = coordinator._accept(native, command(snapshot, "account.connect"))
    scope = json.loads(operation["owner_scope_json"])
    assert fresh and scope["source_journey_id"] != original.journey_id
    assert scope["creator_account_id"] == "creator-1"
    assert operation["journey_id"] == root
    assert owner.contexts.require_current(scope["source_journey_id"])["scope"]["creator_account_id"] == "creator-1"

@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_provider_terminal_source_can_continue_before_local_presentation_expiry(owner, state):
    _, original = selected(owner)
    owner.contexts.begin_prepare(original.journey_id)
    owner.contexts.bind_result(original.journey_id, continuation_id="provider-1",
        expires_at=(NOW + timedelta(minutes=10)).isoformat(), reference="locator")
    owner.contexts.mark_state(original.journey_id, state)
    before = owner.journeys.require_current(original.journey_id)
    owner.clock[0] += timedelta(minutes=11)
    with owner.store.database.transaction() as connection:
        proposal = owner.contexts.desktop_recovery_proposal(connection, journey_id=original.journey_id)
        replacement = owner.contexts.admit_desktop_recovery(connection, proposal=proposal, reopen_key=owner.key.reopen_existing)
    assert replacement != original.journey_id
    assert owner.journeys.require_current(original.journey_id) == before
    assert owner.contexts.require_current(replacement)["scope"]["creator_account_id"] == "creator-1"
