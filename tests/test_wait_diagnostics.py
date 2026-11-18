"""Closed wait diagnostics preserve frames and failures without recording their contents."""
import asyncio
import logging
import re

import httpx
import pytest

from app.onboarding import timing
from app.security.initial_handoff import InitialHandoffStream
from test_browser_ceremony_wait_client import GOOD, client
from test_desktop_coordinator import Hints, _hint, _until, _waited_connect, setup  # noqa: F401

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


class Interrupted(httpx.AsyncByteStream):
    def __init__(self, error):
        self.error = error

    async def __aiter__(self):
        yield b": unlogged-detail\n\n"
        raise self.error


def messages(caplog, operation):
    return [message for message in caplog.messages if f"operation={operation} " in message]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ceremony", "handoff", "continuity"])
@pytest.mark.parametrize("error_type, phase", [
    (httpx.ReadTimeout, "stream-read-timeout"),
    (httpx.ConnectTimeout, "stream-connect-timeout"),
    (httpx.WriteTimeout, "stream-write-timeout"),
    (httpx.PoolTimeout, "stream-pool-timeout"),
    (httpx.RemoteProtocolError, "stream-transport-error"),
    (ValueError, "stream-error"),
    (asyncio.CancelledError, "stream-cancel"),
])
async def test_stream_failure_diagnostics_preserve_the_exception(kind, error_type, phase, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=timing.__name__)
    error = error_type("unlogged-detail")
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, stream=Interrupted(error), headers={"content-type": "text/event-stream"}))
    real_client = httpx.AsyncClient
    if kind == "ceremony":
        subject, _ = client(stream_transport=transport)
        stream = subject.hints({"request": GOOD["request"]})
        operation = "wait"
    else:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(**kwargs, transport=transport))
        stream = InitialHandoffStream("https://provider.invalid", continuity=kind == "continuity").events({})
        operation = "source" if kind == "continuity" else "handoff-wait"
    with pytest.raises(error_type) as caught:
        _ = [frame async for frame in stream]
    assert caught.value is error
    observed = messages(caplog, operation)
    assert any("phase=stream-open " in message for message in observed)
    assert any(f"phase={phase} " in message for message in observed)
    assert any("phase=keepalive " in message for message in observed)
    assert "unlogged-detail" not in "\n".join(observed)
    assert all(re.fullmatch(r"event=sign_in_phase operation=[a-z-]+ phase=[a-z-]+ "
        r"status=(?:ok|error|cancelled) elapsed_ms=\d+(?: http=\d{3})?", message) for message in observed)


@pytest.mark.asyncio
async def test_clean_eof_is_distinct_from_timeout_and_preserves_the_hint(caplog):
    import json
    from test_browser_ceremony_wait_client import CEREMONY
    from app.security.browser_ceremony_wait import PROFILE

    caplog.set_level(logging.INFO, logger=timing.__name__)
    wire = b": unlogged-detail\n\nevent: browser-ceremony\ndata: " + json.dumps({
        "profile": PROFILE, "ceremony_id": CEREMONY, "revision": 1,
        "presentation_state": "active", "domain_status": "awaiting-confirmation"}).encode() + b"\n\n"
    subject, key = client(stream_transport=httpx.MockTransport(lambda _: httpx.Response(
        200, content=wire, headers={"content-type": "text/event-stream"})))
    assert [frame async for frame in subject.hints({"request": GOOD["request"]})] == [
        (1, "active", "awaiting-confirmation")]
    observed = messages(caplog, "wait")
    assert any("phase=stream-eof status=ok " in message for message in observed)
    assert not any("timeout" in message for message in observed)
    assert "unlogged-detail" not in "\n".join(observed)
    assert key.signed == 0


@pytest.mark.asyncio
async def test_coordinator_records_eligibility_reopen_and_the_recovery_trigger(setup, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=timing.__name__)
    monkeypatch.setattr("app.onboarding.coordinator.RECOVERY_INTERVAL_SECONDS", 0.03)
    monkeypatch.setattr("app.onboarding.coordinator.CONFIRMATION_BACKOFF_CAP_SECONDS", 0.04)
    monkeypatch.setattr("app.onboarding.coordinator.WAIT_BACKOFF_CAP_SECONDS", 0.04)
    coordinator, owner, _, _ = setup
    lost = asyncio.Event()
    hints = Hints((_hint(1), lost, OSError("unlogged-detail")), (OSError("unlogged-detail"),), ("hold",))
    await _waited_connect(setup, hints)
    try:
        await _until(lambda: coordinator._held and not coordinator._workers)
        owner.confirmation_deadline = lambda _: coordinator.admissions._now() + 10
        before = len(owner.calls)
        lost.set()
        await _until(lambda: len(hints.opened) >= 3)
        await _until(lambda: len(owner.calls) >= before + 2)
        assert any("phase=wait-fallback " in message for message in messages(caplog, "worker"))
        observed = messages(caplog, "wait")
        for phase in ("eligible", "confirmation-live", "reopen", "released", "fallback", "backoff"):
            assert any(f"phase={phase} " in message for message in observed), phase
        assert "unlogged-detail" not in "\n".join(observed)
        owner.probe_interval = lambda _: None
        assert coordinator._probe_interval({}) is None
        assert any("phase=ineligible " in message for message in messages(caplog, "wait"))
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds, milliseconds", [(71, 71000), (3601, 3600000)])
async def test_wait_diagnostics_measure_the_stream_age(caplog, seconds, milliseconds):
    caplog.set_level(logging.INFO, logger=timing.__name__)
    clock = iter((0.0, float(seconds)))
    async with timing.stream("wait", clock=lambda: next(clock)):
        pass
    assert messages(caplog, "wait") == [
        "event=sign_in_phase operation=wait phase=stream-open status=ok elapsed_ms=0",
        f"event=sign_in_phase operation=wait phase=stream-eof status=ok elapsed_ms={milliseconds}"]
