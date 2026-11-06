"""Access state pushed to open pages after authority commits and validity boundaries."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from app.api.endpoints import webauthn as webauthn_endpoints
from app.core.config import settings
from app.packaged_entry import supervised_server
from app.persistence.auth import SQLiteAuthenticationStore
from app.provisioning.events import events
from test_webauthn_access_state import ORIGIN, Clock, _application, _grant, _membership_reference, _store

pytestmark = [pytest.mark.ci_tier("fast")]

PATH = "/api/v1/webauthn/session-state/events"
HOST = "bridge.localhost:17871"


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 6, 9, tzinfo=timezone.utc))


@pytest.fixture
def renewal(monkeypatch):
    monkeypatch.setattr(settings, "bridge_origin", ORIGIN)
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(settings, "identity_binding_source", "verified_grants")


class FlowingClock:
    """Starts at one instant, then advances with real time."""

    def __init__(self, start: datetime) -> None:
        self.start, self.origin = start, time.monotonic()

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=time.monotonic() - self.origin)


def _streaming(store: SQLiteAuthenticationStore, clock) -> FastAPI:
    application = _application(store, clock)
    application.dependency_overrides[webauthn_endpoints.runtime_policy_reader] = (
        lambda: store.build_runtime_policy)
    return application


class Stream:
    """One access stream driven through the application without a socket."""

    def __init__(self, application: FastAPI, headers: dict[str, str] | None = None) -> None:
        self.status = asyncio.get_running_loop().create_future()
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        fields = {"host": HOST, "sec-fetch-site": "same-origin", **(headers or {})}
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": PATH, "raw_path": PATH.encode(),
            "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000),
            "server": ("bridge.localhost", 17871),
            "headers": [(name.encode(), value.encode()) for name, value in fields.items()],
        }
        self.task = asyncio.create_task(application(scope, self._receive, self._send))

    @staticmethod
    async def _receive():
        await asyncio.Event().wait()

    async def _send(self, message) -> None:
        if message["type"] == "http.response.start":
            self.status.set_result(message["status"])
        elif message["type"] == "http.response.body":
            for frame in message.get("body", b"").decode().split("\n\n"):
                if frame.startswith("event: access\ndata: "):
                    self.frames.put_nowait(json.loads(frame.split("data: ", 1)[1])["access"])

    async def next(self, timeout: float = 3.0) -> str:
        # Every wait here is shorter than the 15 second keepalive read.
        return await asyncio.wait_for(self.frames.get(), timeout)

    async def close(self) -> None:
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task


@pytest.mark.asyncio
async def test_a_renewal_commit_reaches_an_open_stream_without_another_request(tmp_path, clock, renewal):
    store = _store(tmp_path, clock)
    clock.now += timedelta(hours=2)
    lapsed = _membership_reference(store)
    listeners = len(events._listeners)
    stream = Stream(_streaming(store, clock))
    try:
        assert await asyncio.wait_for(stream.status, 3) == 200
        assert await stream.next() == "renewing"
        # The refresh worker commits from its own thread.
        renewed = _grant(clock, "membership_snapshot", 50)
        await asyncio.to_thread(store.replace_verified_grant, lapsed.reference_id, renewed)
        assert await stream.next() == "available"
    finally:
        await stream.close()
    assert len(events._listeners) == listeners


@pytest.mark.asyncio
async def test_a_validity_boundary_reaches_an_open_stream_without_a_commit(tmp_path, clock, renewal):
    membership = _membership_reference(_store(tmp_path, clock))
    flowing = FlowingClock(membership.expires_at - timedelta(seconds=1))
    timed = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=flowing)
    stream = Stream(_streaming(timed, flowing))
    try:
        assert await asyncio.wait_for(stream.status, 3) == 200
        assert await stream.next() == "available"
        assert await stream.next() == "renewing"
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_access_stream_refuses_other_sites_and_hosts(tmp_path, clock, renewal):
    # Driven without a client that reads the whole body, so an admitted stream fails fast.
    application = _streaming(_store(tmp_path, clock), clock)
    for headers in ({"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                    {"host": "attacker.example"}):
        stream = Stream(application, headers)
        try:
            assert await asyncio.wait_for(stream.status, 3) == 403, headers
            await asyncio.wait_for(stream.task, 3)
        finally:
            await stream.close()


@pytest.mark.asyncio
async def test_closing_ends_every_stream_and_refuses_new_ones(tmp_path, clock, renewal):
    application = _streaming(_store(tmp_path, clock), clock)
    listeners = len(events._listeners)
    streams = [Stream(application) for _ in range(8)]
    try:
        for stream in streams:
            assert await stream.next() == "available"
        extra = Stream(application)
        assert await asyncio.wait_for(extra.status, 3) == 429
        await asyncio.wait_for(extra.task, 3)
        application.state.webauthn_access_streams.close()
        await asyncio.wait_for(asyncio.gather(*(stream.task for stream in streams)), 3)
        late = Stream(application)
        assert await asyncio.wait_for(late.status, 3) == 503
        await asyncio.wait_for(late.task, 3)
    finally:
        for stream in streams:
            await stream.close()
    assert len(events._listeners) == listeners
    assert not application.state.webauthn_access_streams.active


@pytest.mark.asyncio
@pytest.mark.ci_tier("integration")
@pytest.mark.windows_compat
@pytest.mark.serial
async def test_real_server_ends_an_open_access_stream_when_it_shuts_down(tmp_path, clock, renewal):
    application = _streaming(_store(tmp_path, clock), clock)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    configuration = uvicorn.Config(application, host="127.0.0.1", port=port,
                                   access_log=False, log_level="critical", lifespan="on")
    server = supervised_server(configuration, application)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                               headers={"Host": HOST, "Sec-Fetch-Site": "same-origin"})
    response = None
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.01)
        response = await client.send(client.build_request("GET", PATH), stream=True)
        assert response.status_code == 200
        lines = response.aiter_lines()
        assert await asyncio.wait_for(anext(lines), 3) == "event: access"
        server.should_exit = True
        # Uvicorn waits for open responses, so an unclosed stream holds shutdown.
        await asyncio.wait_for(serving, 3)
        assert not application.state.webauthn_access_streams.active
    finally:
        if response is not None:
            await response.aclose()
        await client.aclose()
        server.should_exit = server.force_exit = True
        if not serving.done():
            await asyncio.wait_for(serving, 3)
        listener.close()
