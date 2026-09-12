"""Production refresh orchestration, independent of hosted wire fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.persistence.auth import RevocationKey, RevocationScopeType
from app.provisioning.completion import durable_authentication_store, durable_finalize_action
from app.security.grant_refresh import GrantRefreshLifecycle
from app.security.hosted_grants import GrantRefresh, grant_offline_grace_seconds
from app.security.runtime_policy import AuthContext
from tests.test_provisioning_completion import (
    ACCOUNT_ID, ASSOCIATION_REQUEST_ID, EXTENSION_ID, SUBJECT, INSTALLATION_KEY_JKT,
    grant, seed_verified_installation,
)


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.elapsed = 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds


class MemoryReferences:
    def __init__(self, values):
        self.values = values
        self.limits = []

    def installation_key_reference(self):
        return SimpleNamespace(installation_key_id="installation-key-1", installation_key_jkt=INSTALLATION_KEY_JKT)

    def verified_grants(self, *, limit=None):
        self.limits.append(limit)
        return tuple(self.values[:limit])


class Client:
    def __init__(self, store=None, outcomes=None, entered=None, release=None):
        self.store = store
        self.outcomes = list(outcomes or ["unknown"])
        self.entered, self.release = entered, release
        self.calls = self.commits = self.closed = 0

    def refresh_reference(self, reference_id, *, commit_guard):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        state = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        with commit_guard() as active:
            if not active or state == "unknown":
                return GrantRefresh("unknown", (reference_id,), None)
            self.commits += 1
            if state == "revoked":
                self.store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, reference_id), reason="test-denial")
                return GrantRefresh("revoked", (), None)
            previous = self.store.verified_grant(reference_id)
            replacement = replace(previous, reference_id=reference_id + "-refreshed",
                                  grant_identifier=previous.grant_identifier + "-refreshed",
                                  grant_digest=hashlib.sha256((reference_id + "-refreshed").encode()).hexdigest(),
                                  allowed_creator_account_ids=(ACCOUNT_ID,))
            self.store.replace_verified_grant(reference_id, replacement)
            return GrantRefresh("updated", (replacement.reference_id,), None)

    def close(self):
        self.closed += 1


def lifecycle(store, client, clock=None):
    clock = clock or Clock()
    return GrantRefreshLifecycle(lambda: store, lambda _: client,
                                 clock=lambda: clock.now, monotonic=lambda: clock.elapsed,
                                 jitter=lambda: 0.5)


def due_reference(clock):
    value = grant("membership_snapshot")
    return replace(value, valid_from=clock.now - timedelta(seconds=40),
                   expires_at=clock.now + timedelta(seconds=60 + grant_offline_grace_seconds(value.grant_type)))


def test_empty_membership_refresh_retries_after_durable_approval_then_finalizes(tmp_path):
    open_store = durable_authentication_store(tmp_path)
    store = open_store()
    seed_verified_installation(store)
    prior = store.verified_grant("membership_snapshot-current")
    empty = replace(prior, reference_id="empty-membership", grant_identifier="empty-membership",
                    grant_digest=hashlib.sha256(b"empty-membership").hexdigest(), allowed_creator_account_ids=())
    store.replace_verified_grant(prior.reference_id, empty)
    client = Client(store, ["unknown", "updated"])
    renewal = lifecycle(store, client)
    complete = durable_finalize_action(open_store, extension_id=EXTENSION_ID,
                                       data_directory=tmp_path, grant_refresh=renewal)
    arguments = dict(association_request_id=ASSOCIATION_REQUEST_ID,
                     detected_creator_account_id=ACCOUNT_ID, reported_platform_creator_id=None)
    assert complete(**arguments) == "membership_refresh_unavailable"
    assert not store.authorized_account_bindings()
    assert store.pending_provisioning_candidate(empty.installation_id) is None
    assert complete(**arguments) is None
    assert client.calls == 2
    assert len(store.authorized_account_bindings()) == 1
    renewal.close()


def test_refresh_alone_never_activates_account_and_wrong_account_does_not_send(tmp_path):
    store = durable_authentication_store(tmp_path)()
    seed_verified_installation(store)
    prior = store.verified_grant("membership_snapshot-current")
    store.replace_verified_grant(prior.reference_id, replace(prior, reference_id="empty",
        grant_digest=hashlib.sha256(b"empty").hexdigest(), grant_identifier="empty", allowed_creator_account_ids=()))
    client = Client(store, ["updated"])
    renewal = lifecycle(store, client)
    assert renewal.prepare_finalization(ASSOCIATION_REQUEST_ID, "unapproved-account") is None
    assert client.calls == 0
    assert renewal.prepare_finalization(ASSOCIATION_REQUEST_ID, ACCOUNT_ID) is not None
    assert client.calls == 1
    assert not store.authorized_account_bindings()
    renewal.close()


def test_renewal_precedes_half_signed_lifetime_and_unknown_uses_bounded_backoff():
    clock = Clock()
    reference = due_reference(clock)
    store = MemoryReferences([reference])
    client = Client()
    renewal = lifecycle(store, client, clock)
    clock.advance(-1)
    assert renewal.tick() == pytest.approx(1.0)
    assert client.calls == 0
    clock.advance(1)
    renewal.tick()
    assert client.calls == 1
    assert renewal.tick() == pytest.approx(30.0)
    clock.advance(29)
    assert renewal.tick() == pytest.approx(1.0)
    clock.advance(1)
    renewal.tick()
    assert client.calls == 2
    assert renewal.tick() == pytest.approx(30.0)
    assert store.values[0].expires_at == reference.expires_at
    assert set(store.limits) == {129}
    renewal.close()


def test_read_bound_and_wrong_key_refuse_outbound_work():
    clock = Clock()
    client = Client()
    store = MemoryReferences([replace(due_reference(clock), reference_id=str(index)) for index in range(129)])
    renewal = lifecycle(store, client, clock)
    assert renewal.tick() == 30.0
    assert client.calls == 0
    store.values = [replace(due_reference(clock), installation_key_id="another-key")]
    renewal.tick()
    assert client.calls == 0


@pytest.mark.parametrize("stop", [False, True])
def test_inflight_attempt_is_single_and_late_commit_fails_deadline_or_shutdown(stop):
    clock = Clock()
    reference = due_reference(clock)
    store = MemoryReferences([reference])
    entered, release = threading.Event(), threading.Event()
    client = Client(outcomes=["revoked"], entered=entered, release=release)
    renewal = lifecycle(store, client, clock)
    worker = threading.Thread(target=renewal.refresh, args=(reference.reference_id,), daemon=True)
    worker.start()
    assert entered.wait(2)
    assert renewal.refresh(reference.reference_id).state == "unknown"
    assert client.calls == 1
    if stop:
        asyncio.run(renewal.stop())
    else:
        clock.advance(26)
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert client.commits == 0
    renewal.close()
    assert client.closed == 1


def test_runtime_stop_interrupts_backoff_and_closes_one_worker():
    clock = Clock()
    store = MemoryReferences([due_reference(clock)])
    called = threading.Event()
    client = Client()
    original = client.refresh_reference
    def request(*args, **kwargs):
        result = original(*args, **kwargs)
        called.set()
        return result
    client.refresh_reference = request
    renewal = lifecycle(store, client, clock)
    renewal.start()
    worker = renewal._worker
    renewal.start()
    assert renewal._worker is worker and worker.daemon
    assert called.wait(2)
    asyncio.run(renewal.stop())
    assert not worker.is_alive()
    assert client.calls == 1 and client.closed == 1


def test_runtime_denial_invalidates_existing_policy_without_activation(tmp_path):
    store = durable_authentication_store(tmp_path)()
    references = seed_verified_installation(store)
    policy = store.build_runtime_policy_from_grants(AuthContext(SUBJECT, ACCOUNT_ID, "creator"), references)
    client = Client(store, ["revoked"])
    renewal = lifecycle(store, client)
    assert renewal.refresh("membership_snapshot-current").state == "revoked"
    assert not store.runtime_policy_is_current(policy)
    assert not store.authorized_account_bindings()
    renewal.close()
