"""Saved-context completion uses real encrypted persistence and simulated hosting."""

import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from app.persistence.auth import AuthenticationStateError, RevocationKey, RevocationScopeType, VerifiedGrantReference
from app.persistence.onboarding import OnboardingJourneyUnavailable
from app.provisioning.finalize import _one_grant_per_required_type
from app.provisioning.installation_continuation import RegisteredInstallationContinuation
from app.security.hosted_grants import HostedGrantUnavailable
from test_registered_continuation_context import owner, selected, NOW


pytestmark = [pytest.mark.ci_tier("integration")]
CONTEXT = "019a0934-7800-7000-8000-000000000001"
EPOCH = "019a0934-7800-7000-8000-000000000002"
EXPIRY = "2026-10-09T12:20:00.000Z"


def grant(owner, name="binding", kind="creator_account_binding"):
    key = owner.store.installation_key_reference()
    return VerifiedGrantReference(name, name, kind, hashlib.sha256(name.encode()).hexdigest(),
        "fixture-issuer", "fixture-subject", "installation-1", "creator-1" if kind == "creator_account_binding" else None,
        NOW - timedelta(seconds=1), NOW + timedelta(hours=1), NOW,
        organization_id="organization-1", installation_key_id=key.installation_key_id,
        installation_key_jkt=key.installation_key_jkt,
        membership_id="membership-1" if kind == "membership_snapshot" else None,
        approval_id="approval-1" if kind == "creator_account_binding" else None,
        approval_revision=1 if kind == "creator_account_binding" else None)


def approved(owner):
    _, session = selected(owner)
    owner.contexts.begin_prepare(session.journey_id)
    owner.contexts.bind_result(session.journey_id, continuation_id=CONTEXT, expires_at=EXPIRY,
        epoch=EPOCH, revision=1, provider_state="approved")
    owner.store.record_verified_grant(grant(owner, "membership", "membership_snapshot"))
    return session.journey_id


def test_fresh_reconciliation_replaces_binding_atomically_and_keeps_one_finalization_grant(owner):
    journey = approved(owner)
    owner.contexts.complete_binding(journey, grant(owner, "old"), membership_reference_id="membership")
    owner.contexts.complete_binding(journey, grant(owner, "fresh"), membership_reference_id="membership")
    bindings = tuple(item for item in owner.store.verified_grants() if item.grant_type == "creator_account_binding")
    assert len(bindings) == 1 and bindings[0].reference_id == "fresh"
    _one_grant_per_required_type(bindings, ("creator_account_binding",))
    assert owner.contexts.require_current(journey)["state"] == "completed"
    assert owner.store.scope_is_revoked(RevocationKey(RevocationScopeType.VERIFIED_GRANT, "old"))


def test_replayed_verified_binding_is_safe_without_duplicate_approval(owner):
    journey = approved(owner)
    first = grant(owner)
    owner.contexts.complete_binding(journey, first, membership_reference_id="membership")
    owner.contexts.complete_binding(journey, replace(first, verified_at=NOW + timedelta(seconds=1)),
                                    membership_reference_id="membership")
    assert len([item for item in owner.store.verified_grants() if item.grant_type == "creator_account_binding"]) == 1
    assert owner.store.provisioning_candidate("request-1").resolved_at == NOW


@pytest.mark.parametrize("scope,identifier", [
    (RevocationScopeType.PRINCIPAL, "fixture-subject"),
    (RevocationScopeType.INSTALLATION, "installation-1"),
    (RevocationScopeType.CREATOR_ACCOUNT, "creator-1"),
    (RevocationScopeType.VERIFIED_GRANT, "membership"),
    (RevocationScopeType.VERIFIED_GRANT, "binding"),
])
def test_local_revocation_prevents_grant_commit_and_false_completion(owner, scope, identifier):
    journey = approved(owner)
    owner.store.revoke(RevocationKey(scope, identifier), reason="fixture")
    with pytest.raises(AuthenticationStateError):
        owner.contexts.complete_binding(journey, grant(owner), membership_reference_id="membership")
    assert owner.contexts.require_current(journey)["state"] == "waiting"
    assert owner.store.provisioning_candidate("request-1").state.value == "pending"
    assert not any(item.grant_type == "creator_account_binding" for item in owner.store.verified_grants())


@pytest.mark.parametrize("changes", [
    {"creator_account_id": "creator-other"}, {"installation_id": "other"},
    {"installation_key_id": "other"}, {"organization_id": "other"},
    {"issuer": "other"}, {"subject": "other"}, {"expires_at": NOW},
])
def test_binding_scope_change_rolls_back_whole_completion(owner, changes):
    journey = approved(owner)
    with pytest.raises(AuthenticationStateError):
        owner.contexts.complete_binding(journey, replace(grant(owner), **changes), membership_reference_id="membership")
    assert owner.contexts.require_current(journey)["state"] == "waiting"
    assert owner.store.provisioning_candidate("request-1").state.value == "pending"


def test_fresh_binding_cannot_change_an_existing_approval(owner):
    journey = approved(owner)
    owner.contexts.complete_binding(journey, grant(owner, "old"), membership_reference_id="membership")
    with pytest.raises(AuthenticationStateError):
        owner.contexts.complete_binding(journey, replace(grant(owner, "new"), approval_id="different"),
                                        membership_reference_id="membership")
    assert owner.store.verified_grant("new") is None
    assert owner.store.verified_grant("old") is not None


@pytest.mark.parametrize("replace_existing", [False, True])
def test_workflow_failure_rolls_back_kernel_admission_and_replacement(owner, monkeypatch, replace_existing):
    journey = approved(owner)
    if replace_existing:
        owner.contexts.complete_binding(journey, grant(owner, "old"), membership_reference_id="membership")
    before = owner.contexts.require_current(journey)
    candidate = owner.store.provisioning_candidate("request-1")
    with owner.store.database.read() as connection:
        epoch = owner.store._authorization_epoch(connection)
    original = owner.contexts.require_current
    calls = 0

    def fail_after_workflow_write(*args, **kwargs):
        nonlocal calls
        value = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            assert value["state"] == "completed"
            raise RuntimeError("workflow commit interrupted")
        return value

    with monkeypatch.context() as patch:
        patch.setattr(owner.contexts, "require_current", fail_after_workflow_write)
        with pytest.raises(RuntimeError, match="workflow commit interrupted"):
            owner.contexts.complete_binding(journey, grant(owner, "new"), membership_reference_id="membership")
    assert calls == 2
    assert owner.contexts.require_current(journey) == before
    assert owner.store.provisioning_candidate("request-1") == candidate
    assert owner.store.verified_grant("new") is None
    if replace_existing:
        assert owner.store.verified_grant("old") == grant(owner, "old")
        assert not owner.store.scope_is_revoked(RevocationKey(RevocationScopeType.VERIFIED_GRANT, "old"))
    with owner.store.database.read() as connection:
        assert owner.store._authorization_epoch(connection) == epoch


def test_kernel_continuation_admission_requires_the_workflow_transaction(owner):
    journey = approved(owner)
    target = owner.contexts.require_current(journey)["target"]
    with owner.store.database.read() as connection:
        assert not connection.in_transaction
        with pytest.raises(AuthenticationStateError, match="requires a transaction"):
            owner.store.admit_continuation_binding_in_transaction(
                connection, grant(owner), membership_reference_id="membership",
                claim=target.claim, candidate=target.candidate, key=target.key,
                binding_revoked_at=target.binding_revoked_at,
            )
    assert owner.contexts.require_current(journey)["state"] == "waiting"
    assert owner.store.verified_grant("binding") is None


def snapshot(state, revision=0):
    return {"continuation_id": CONTEXT, "epoch": EPOCH, "revision": revision,
            "committed_at": "2026-10-09T12:00:00.000Z", "expires_at": EXPIRY, "state": state}


class Client:
    def __init__(self, owner):
        self.key, self.transport = owner.key, None
        self.calls = []
        self.lost = False
        self.read_result = "found"
        self.before_binding_return = lambda: None

    def prepare(self, request, *, before_send):
        before_send()
        self.calls.append(("prepare", request))
        if self.lost:
            raise HostedGrantUnavailable("simulated lost response")
        return {"continuation_id": CONTEXT, "reference": "r" * 43, "expires_at": EXPIRY}

    def read(self, request, *, before_send):
        before_send()
        self.calls.append(("read", request))
        if self.read_result != "found":
            return {"result": self.read_result}
        return {"result": "found", "reference": "r" * 43, "snapshot": snapshot("awaiting-owner")}

    def envelope(self, operation, request, *, before_send):
        before_send()
        self.calls.append((operation, request))
        return {"request": request}

    def binding(self, request, *, before_send):
        before_send()
        self.calls.append(("binding", request))
        self.before_binding_return()
        return {"grant": "simulated-signed-token"}


class Stream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.opened = asyncio.Event()

    async def events(self, envelope, *, expires_at):
        self.opened.set()
        while True:
            yield {"snapshot": await self.queue.get()}


class Grants:
    def __init__(self, owner):
        self.owner = owner
        self.accepted = []

    def accept_continuation_binding(self, token, association, *, membership_reference_id):
        self.accepted.append((association, membership_reference_id))
        return grant(self.owner)

    def close(self):
        pass


def worker(owner):
    client, stream, grants = Client(owner), Stream(), Grants(owner)
    result = RegisteredInstallationContinuation(owner.store, hosted_origin="https://hosting.invalid",
        client=client, stream=stream, grants=grants)
    return result, client, stream, grants


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_unknown_prepare_is_read_only_even_after_owner_restart(owner):
    _, session = selected(owner)
    first, client, _, _ = worker(owner)
    client.lost = True
    with pytest.raises(HostedGrantUnavailable):
        await first.prepare(session.journey_id)
    operation = owner.contexts.require_current(session.journey_id)["operation_id"]
    await first.stop()
    resumed, next_client, _, _ = worker(owner)
    try:
        result = await resumed.prepare(session.journey_id)
        assert result["continuation_reference"] == "r" * 43
        assert next_client.calls[0][0] == "read"
        assert not any(name == "prepare" for name, _ in next_client.calls)
        assert next_client.calls[0][1]["operation_id"] == operation
    finally:
        await resumed.stop()


@pytest.mark.asyncio
async def test_new_context_read_does_not_prepare_or_create_hosted_authority(owner):
    _, session = selected(owner)
    service, client, _, _ = worker(owner)
    try:
        assert (await service.read_browser_entry(session.journey_id))["state"] == "unknown"
        service.resume(session.journey_id)
        assert client.calls == [] and not service._tasks
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_push_approval_uses_exact_binding_and_does_not_repeat_connect(owner):
    request_id = "019a0934-7800-7000-8000-000000000003"
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE provisioning_candidates SET association_request_id=? WHERE association_request_id='request-1'",
                           (request_id,))
    _, session = selected(owner)
    owner.store.record_verified_grant(grant(owner, "membership", "membership_snapshot"))
    service, client, stream, grants = worker(owner)
    try:
        await service.prepare(session.journey_id)
        await stream.opened.wait()
        task = service._tasks[session.journey_id]
        service.resume(session.journey_id)
        assert service._tasks[session.journey_id] is task
        await stream.queue.put(snapshot("awaiting-owner"))
        await until(lambda: owner.contexts.require_current(session.journey_id)["epoch"] is not None)
        assert owner.store.provisioning_candidate(request_id).state.value == "pending"
        await stream.queue.put(snapshot("approved", 1))
        await until(lambda: owner.contexts.require_current(session.journey_id)["state"] == "completed")
        await stream.queue.put(snapshot("approved", 1))
        await asyncio.sleep(0.03)
        assert len(grants.accepted) == 1
        assert grants.accepted[0][0].association_request_id == request_id
        assert [name for name, _ in client.calls].count("binding") == 1
        assert owner.store.provisioning_candidate(request_id).state.value == "approved"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_old_worker_generation_cannot_apply_a_snapshot(owner):
    _, session = selected(owner)
    service, _, stream, _ = worker(owner)
    try:
        await service.prepare(session.journey_id)
        await stream.opened.wait()
        previous = service._generation[session.journey_id]
        service._generation[session.journey_id] += 1
        with pytest.raises(OnboardingJourneyUnavailable):
            service._snapshot(session.journey_id, snapshot("approved"), generation=previous)
        assert owner.contexts.require_current(session.journey_id)["provider_state"] is None
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["revoked", "authentication-required"])
async def test_delayed_binding_does_not_block_newer_pushed_authority_state(owner, state):
    request_id = "019a0934-7800-7000-8000-000000000003"
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE provisioning_candidates SET association_request_id=?", (request_id,))
    _, session = selected(owner)
    owner.store.record_verified_grant(grant(owner, "membership", "membership_snapshot"))
    service, client, stream, grants = worker(owner)
    started, release = Event(), Event()
    def delayed():
        started.set()
        assert release.wait(3)
    client.before_binding_return = delayed
    try:
        await service.prepare(session.journey_id)
        await stream.opened.wait()
        await stream.queue.put(snapshot("approved", 1))
        await until(started.is_set)
        await stream.queue.put(snapshot(state, 2))
        await until(lambda: owner.contexts.require_current(session.journey_id)["provider_state"] == state)
        assert not release.is_set()
        assert owner.contexts.require_current(session.journey_id)["state"] == ("waiting" if state == "authentication-required" else state)
        if state == "authentication-required":
            assert owner.contexts.require_current(session.journey_id)["reason"] == state
        release.set()
        await asyncio.sleep(0.03)
        assert grants.accepted == []
        assert owner.store.provisioning_candidate(request_id).state.value == "pending"
    finally:
        release.set()
        await service.stop()
