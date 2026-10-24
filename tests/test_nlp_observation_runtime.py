"""Saved observation reads share admission and never request recovery."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.analytics.errors import ProjectionBackpressure, ProjectionStorageUnavailable, ProjectionUnavailable
from app.analytics.evidence import EvidenceUnavailable
from app.analytics.query_runtime import QuestionResources
from app.security.local_sessions import build_runtime_policy
from app.security.runtime_policy import AuthContext
from app.services import insights_service

pytestmark = pytest.mark.ci_tier('integration')


@pytest.fixture
def observation_policy():
    return build_runtime_policy(AuthContext('principal.synthetic', 'account.synthetic', 'creator',
        platform_creator_id='platform.synthetic', session_id='session.synthetic'))


def test_observation_owner_shares_slots_foreground_and_releases_on_refusal(monkeypatch):
    from app.analytics import nlp_observation_read

    owner = QuestionResources(SimpleNamespace(), SimpleNamespace())
    policy, request, result = object(), object(), object()
    cancelled = lambda: False
    calls = []

    def read(resources, actual_policy, actual_request, *, cancellation_check):
        assert resources is owner and actual_policy is policy and actual_request is request
        assert cancellation_check is cancelled
        assert owner._work._active_foreground == 1
        calls.append(True)
        return result

    monkeypatch.setattr(nlp_observation_read, 'read', read)
    try:
        assert owner.observation_links(policy, request, cancellation_check=cancelled) is result
        assert owner._work._active_foreground == 0
        assert owner._slots.acquire(blocking=False) and owner._slots.acquire(blocking=False)
        try:
            with pytest.raises(ProjectionBackpressure):
                owner.observation_links(policy, request)
        finally:
            owner._slots.release()
            owner._slots.release()
        assert len(calls) == 1

        def refuse(*args, **kwargs):
            assert owner._work._active_foreground == 1
            raise ProjectionUnavailable()

        monkeypatch.setattr(nlp_observation_read, 'read', refuse)
        with pytest.raises(ProjectionUnavailable):
            owner.observation_links(policy, request)
        assert owner._work._active_foreground == 0
        assert owner._slots.acquire(blocking=False) and owner._slots.acquire(blocking=False)
        owner._slots.release()
        owner._slots.release()
    finally:
        owner.close()
        assert owner.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [None, ProjectionUnavailable(building=True),
                                  ProjectionStorageUnavailable(), EvidenceUnavailable()])
async def test_observation_service_cancellation_and_refusal_never_schedule(
        observation_policy, monkeypatch, error):
    callbacks, discarded = [], []
    result, request = object(), object()

    def read(policy, actual_request, *, cancellation_check):
        assert policy is observation_policy and actual_request is request
        assert cancellation_check() is False
        callbacks.append(cancellation_check)
        if error is not None:
            raise error
        return result

    forbidden = Mock(side_effect=AssertionError('unexpected recovery access'))
    resources = SimpleNamespace(closed=False, observation_links=read,
        discard=discarded.append, take_refresh=forbidden)
    runtime = SimpleNamespace(questions=resources,
        scheduler=SimpleNamespace(state=forbidden, request_recovery=forbidden),
        source=SimpleNamespace(account_revision=forbidden))
    monkeypatch.setattr(insights_service, 'analytics_runtime', lambda: runtime)
    if error is None:
        assert await insights_service.fetch_observation_links(observation_policy, request) is result
        assert discarded == []
    else:
        with pytest.raises(type(error)) as raised:
            await insights_service.fetch_observation_links(observation_policy, request)
        assert raised.value is error
        assert discarded == ['account.synthetic']
    assert len(callbacks) == 1 and callbacks[0]() is True
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_observation_service_rejects_closed_owner(observation_policy, monkeypatch):
    monkeypatch.setattr(insights_service, 'analytics_runtime',
        lambda: SimpleNamespace(questions=SimpleNamespace(closed=True)))
    with pytest.raises(ProjectionUnavailable):
        await insights_service.fetch_observation_links(observation_policy, object())


@pytest.mark.asyncio
async def test_cancelled_observation_service_signals_abandoned_read(observation_policy, monkeypatch):
    import asyncio
    from threading import Event

    started, release, finished = Event(), Event(), Event()
    callbacks = []

    def read(policy, request, *, cancellation_check):
        callbacks.append(cancellation_check)
        started.set()
        try:
            assert release.wait(5)
            assert cancellation_check() is True
        finally:
            finished.set()

    resources = SimpleNamespace(closed=False, observation_links=read)
    monkeypatch.setattr(insights_service, 'analytics_runtime', lambda: SimpleNamespace(questions=resources))
    task = asyncio.create_task(insights_service.fetch_observation_links(observation_policy, object()))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert callbacks[0]() is True
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
