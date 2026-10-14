"""Owner effects stay visible until the actual synchronous work finishes."""
import asyncio
import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock, current_thread
from types import SimpleNamespace

import pytest

from test_onboarding_enrollment_runtime import _continuity_diagnostic_fixture
from test_registered_continuation_context import owner


pytestmark = [pytest.mark.ci_tier("integration")]
JOURNEY = "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_completed_initial_owner_passive_stream_does_not_report_an_effect(tmp_path, monkeypatch):
    from app.security.onboarding_continuity import PROFILE
    worker, row, snapshot = _continuity_diagnostic_fixture(tmp_path, monkeypatch)
    row["journey_id"] = JOURNEY
    waiting = asyncio.Event()
    worker.continuity = SimpleNamespace(envelope=lambda request: {"request": request})

    class Stream:
        async def events(self, _):
            yield {"profile": PROFILE, "kind": "snapshot", "snapshot": snapshot}
            waiting.set()
            await asyncio.Event().wait()

    worker.continuity_stream = Stream()
    task = asyncio.create_task(worker._continuity(row))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not task.done()
        assert worker.accepted_work() == ()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["association", "binding", "initial-refresh", "registered-refresh"])
async def test_actual_owner_effect_survives_caller_cancellation_until_thread_finishes(
        tmp_path, monkeypatch, owner, effect):
    from app.persistence.auth import ProvisioningCandidateState
    from app.security.onboarding_continuity import PROFILE
    entered, release = Event(), Event()
    calls, closed = [], []

    def blocking(*_, **__):
        calls.append(effect)
        entered.set()
        assert release.wait(3), "Effect was not released"
        return "binding_acquisition_unavailable"

    if effect == "registered-refresh":
        from app.provisioning.installation_continuation import RegisteredInstallationContinuation
        worker = RegisteredInstallationContinuation(owner.store, hosted_origin="https://fixture.invalid",
            client=SimpleNamespace(key=owner.key, transport=object()), stream=object(),
            grants=SimpleNamespace(refresh_reference=blocking, close=lambda: closed.append(True)))
        monkeypatch.setattr(worker.store, "verified_grants", lambda: (
            SimpleNamespace(organization_id="organization-fixture", installation_id="installation-fixture",
                            reference_id="private-reference"),))
        worker.grants = SimpleNamespace(refresh_reference=blocking, close=lambda: closed.append(True))
        operation = worker._refresh_grants({"journey_id": JOURNEY,
            "scope": {"organization_id": "organization-fixture", "installation_id": "installation-fixture"}})
    else:
        initial_directory = tmp_path / "initial"
        initial_directory.mkdir()
        worker, row, snapshot = _continuity_diagnostic_fixture(initial_directory, monkeypatch)
        row["journey_id"] = JOURNEY
        scope = json.loads(row["scope_json"])
        worker.continuity = SimpleNamespace(envelope=lambda request: {"request": request})
        worker.grants.close = lambda: closed.append(True)
        if effect == "association":
            snapshot["facts"]["association"] = "none"
            worker.grants.request_creator_association = blocking
        elif effect == "binding":
            snapshot["facts"]["association"] = "approved"
            monkeypatch.setattr(worker.store, "provisioning_candidate",
                                lambda _: SimpleNamespace(state=ProvisioningCandidateState.PENDING))
            monkeypatch.setattr("app.provisioning.binding_acquisition.acquire_creator_account_binding", blocking)
        else:
            snapshot["facts"]["authorization"] = "denied"
            monkeypatch.setattr(worker, "_refresh_current_grants", type(worker)._refresh_current_grants.__get__(worker))
            monkeypatch.setattr(worker.store, "verified_grants", lambda: (
                SimpleNamespace(installation_id=scope["installation_id"], organization_id=scope["organization_id"],
                    valid_from=worker.store._now() - timedelta(minutes=1),
                    expires_at=worker.store._now() + timedelta(minutes=1), reference_id="private-reference"),))
            worker.grants.refresh_reference = blocking

        class Stream:
            async def events(self, _):
                yield {"profile": PROFILE, "kind": "snapshot", "snapshot": snapshot}
                await asyncio.Event().wait()

        worker.continuity_stream = Stream()
        operation = worker._continuity(row)

    task = asyncio.create_task(operation)
    stopping = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert worker.accepted_work() == ("external:" + JOURNEY,)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.accepted_work() == ("external:" + JOURNEY,)
        stopping = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
        assert closed == []
        release.set()
        await asyncio.wait_for(stopping, 1)
        assert worker.accepted_work() == ()
        assert calls == [effect]
        assert closed == [True]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        else:
            await worker.stop()


@pytest.mark.asyncio
async def test_completed_registered_owner_passive_stream_does_not_report_an_effect(owner):
    from app.provisioning.installation_continuation import RegisteredInstallationContinuation
    from test_installation_continuation_owner import approved
    journey = approved(owner)
    owner.contexts.mark_state(journey, "completed")
    waiting = asyncio.Event()

    class Stream:
        async def events(self, *_args, **_kwargs):
            waiting.set()
            await asyncio.Event().wait()
            yield {}

    worker = RegisteredInstallationContinuation(owner.store, hosted_origin="https://fixture.invalid",
        client=SimpleNamespace(key=owner.key, transport=object(), envelope=lambda *_args, **_kwargs: {}),
        stream=Stream(), grants=SimpleNamespace(close=lambda: None))
    worker.resume(journey)
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert not worker._tasks[journey].done()
        assert worker.accepted_work() == ()
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["provider-binding", "grant-verification"])
async def test_registered_binding_windows_remain_visible_after_cancellation(owner, effect):
    from app.provisioning.installation_continuation import RegisteredInstallationContinuation
    from test_installation_continuation_owner import approved
    with owner.store.database.transaction() as connection:
        connection.execute("UPDATE provisioning_candidates SET association_request_id=? WHERE association_request_id='request-1'",
                           ("019a0934-7800-7000-8000-000000000003",))
    journey = approved(owner)
    entered, release = Event(), Event()
    calls, closed = [], []

    def blocked(*_args, **_kwargs):
        calls.append(effect)
        entered.set()
        assert release.wait(10)
        return {"grant": "private-grant"}

    worker = RegisteredInstallationContinuation(owner.store, hosted_origin="https://fixture.invalid",
        client=SimpleNamespace(key=owner.key, transport=object(),
            binding=blocked if effect == "provider-binding" else lambda *_args, **_kwargs: {"grant": "private-grant"}),
        stream=object(), grants=SimpleNamespace(accept_continuation_binding=blocked, close=lambda: closed.append(True)))
    task = asyncio.create_task(worker._binding(journey, None))
    stopping = None
    try:
        started = await asyncio.to_thread(entered.wait, 5)
        if not started and task.done():
            task.result()
        assert started
        assert worker.accepted_work() == ("external:" + journey,)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.accepted_work() == ("external:" + journey,)
        stopping = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)
        assert not stopping.done() and closed == []
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert worker.accepted_work() == () and calls == [effect] and closed == [True]
        assert owner.contexts.require_current(journey)["state"] == "completing"
        assert not any(grant.grant_type == "creator_account_binding" for grant in owner.store.verified_grants())
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        else:
            await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True])
async def test_detached_effect_failure_is_consumed_without_replay_or_exception_logging(timeout):
    from app.provisioning.owner_effects import OwnerEffects
    entered, release = Event(), Event()
    inventory = OwnerEffects()
    calls, unhandled = [], []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    class PrivateFailure(RuntimeError):
        def __str__(self):
            raise AssertionError("Do not format the effect exception")

    def blocked():
        calls.append(True)
        entered.set()
        assert release.wait(3)
        raise PrivateFailure("private-response")

    task = asyncio.create_task(inventory.run(blocked, journey_id=JOURNEY))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if timeout:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, .001)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert inventory.accepted_work() == ("external:" + JOURNEY,)
        release.set()
        await asyncio.wait_for(inventory.drain(), 1)
        await asyncio.sleep(0)
        assert inventory.accepted_work() == () and calls == [True] and unhandled == []
    finally:
        release.set()
        await inventory.drain()
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("retired", [False, True])
def test_selection_gate_serializes_new_effect_admission_and_rechecks_lineage(retired):
    from app.persistence.onboarding import OnboardingJourneyUnavailable
    from app.provisioning.owner_effects import OwnerEffects
    inventory = OwnerEffects()
    attempted, entered, release = Event(), Event(), Event()
    calls = []

    class Gate:
        def __init__(self):
            self.lock = RLock()

        def __enter__(self):
            if current_thread().name.startswith("owner"):
                attempted.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    gate = Gate()
    lineage = {"retired": False}

    def guard(journey):
        assert journey == JOURNEY
        if lineage["retired"]:
            raise OnboardingJourneyUnavailable("journey_unavailable")

    def effect():
        calls.append(True)
        entered.set()
        assert release.wait(3)
        return "done"

    inventory.bind_effect_gate(gate, guard)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="owner") as executor:
        try:
            with gate:
                result = executor.submit(asyncio.run, inventory.run(effect, journey_id=JOURNEY))
                assert attempted.wait(1)
                assert calls == [] and inventory.accepted_work() == ()
                lineage["retired"] = retired
            if retired:
                with pytest.raises(OnboardingJourneyUnavailable):
                    result.result(1)
                assert calls == [] and inventory.accepted_work() == ()
            else:
                assert entered.wait(1)
                with gate:
                    assert inventory.accepted_work() == ("external:" + JOURNEY,)
                release.set()
                assert result.result(1) == "done"
                assert calls == [True] and inventory.accepted_work() == ()
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_stopped_owner_refuses_late_effect_without_touching_closed_transport(tmp_path, monkeypatch, owner, registered):
    from app.persistence.onboarding import OnboardingJourneyUnavailable
    if registered:
        from app.provisioning.installation_continuation import RegisteredInstallationContinuation
        from test_installation_continuation_owner import approved
        current_journey = approved(owner)
        worker = RegisteredInstallationContinuation(owner.store, hosted_origin="https://fixture.invalid",
            client=SimpleNamespace(key=owner.key, transport=object()), stream=object(), grants=SimpleNamespace(close=lambda: None))
    else:
        initial_directory = tmp_path / "initial"
        initial_directory.mkdir()
        worker, _, _ = _continuity_diagnostic_fixture(initial_directory, monkeypatch)
        current_journey = worker.journeys.open()["journey_id"]
    calls = []
    await worker.stop()
    with pytest.raises(OnboardingJourneyUnavailable):
        await worker._effects.run(lambda: calls.append(True), journey_id=JOURNEY)
    worker.resume(current_journey)
    assert worker._tasks == {}
    assert calls == [] and worker.accepted_work() == ()
