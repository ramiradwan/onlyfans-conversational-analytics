"""Real HTTP event streams must finish before the owned application drains."""
import asyncio
from contextlib import asynccontextmanager
import socket
import threading

import httpx
import pytest
from fastapi import FastAPI
import uvicorn

from app.onboarding.admission_routes import COOKIE_NAMES, CSRF, GENERATION, ORIGIN, csrf_token
from app.onboarding.coordinator import CommandRefused
from app.onboarding.routes import desktop_router
from app.provisioning.events import events
from app.packaged_entry import supervised_server, ObservedApplication
from app.core.bootstrap_status import BootstrapStatus
from test_desktop_coordinator import command, setup as coordinator_setup

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat, pytest.mark.serial]


@pytest.mark.asyncio
@pytest.mark.parametrize("held_stream", [False, True])
async def test_real_server_closes_presentation_before_draining_accepted_work(tmp_path, held_stream):
    coordinator, owner, session, _ = coordinator_setup.__wrapped__(tmp_path)
    owner.release = asyncio.Event()
    accepted = await coordinator.command(session, command(coordinator.read(session)))
    await asyncio.wait_for(owner.started.wait(), 1)
    draining = asyncio.Event()
    listeners_before = len(events._listeners)

    @asynccontextmanager
    async def lifecycle(application):
        application.state.runtime_shutdown_drained = False
        coordinator.start()
        try:
            yield
        finally:
            draining.set()
            await coordinator.stop()
            application.state.runtime_shutdown_drained = True

    application = FastAPI(lifespan=lifecycle)
    application.state.desktop_coordinator = coordinator
    application.include_router(desktop_router(coordinator))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    port = listener.getsockname()[1]
    status = BootstrapStatus(None, threading.Event())
    status.emit("starting")
    observed = ObservedApplication(application, status, threading.Event())
    configuration = uvicorn.Config(observed, host="127.0.0.1", port=port,
                                   access_log=False, log_level="critical", lifespan="on")
    server = supervised_server(configuration, application)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
        headers={"Host": "bridge.localhost:17871", "Origin": ORIGIN,
                 GENERATION: session.generation, CSRF: csrf_token(session.identifier),
                 "Cookie": COOKIE_NAMES["desktop"] + "=" + session.identifier})
    response = None
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.01)
        if held_stream:
            response = await client.send(client.build_request("GET", "/api/v1/desktop/setup/events"), stream=True)
            assert response.status_code == 200
            lines = response.aiter_lines()
            assert await asyncio.wait_for(anext(lines), 1) == "event: setup"
        server.should_exit = True
        # The open response must not prevent lifespan cleanup from beginning.
        await asyncio.wait_for(draining.wait(), 2)
        assert not serving.done(), "accepted owner work was abandoned"
        assert not observed.drained, "drain was acknowledged before accepted work finished"
        with pytest.raises(CommandRefused, match="refused"):
            await coordinator.command(session, command(coordinator.read(session)))
        owner.release.set()
        await asyncio.wait_for(serving, 2)
        assert observed.drained
        assert coordinator.receipt(session, accepted["operation_id"])["state"] == owner.result.state
        if response is not None:
            async def remaining_frames():
                async for _ in lines:
                    pass
            await asyncio.wait_for(remaining_frames(), 1)
        assert len(events._listeners) == listeners_before
    finally:
        owner.release.set()
        if response is not None:
            await response.aclose()
        await client.aclose()
        server.should_exit = True
        server.force_exit = True  # Failure cleanup for this owned test only.
        await asyncio.wait_for(serving, 3)
        await coordinator.stop()
        listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["owner_not_drained", "shutdown_handler_failed"])
async def test_real_lifespan_never_confirms_failed_owner_shutdown(failure):
    @asynccontextmanager
    async def lifecycle(application):
        application.state.runtime_shutdown_drained = False
        yield
        if failure == "shutdown_handler_failed":
            application.state.runtime_shutdown_drained = True
            raise RuntimeError("synthetic owned shutdown failure")
    application = FastAPI(lifespan=lifecycle)
    status = BootstrapStatus(None, threading.Event())
    status.emit("starting")
    observed = ObservedApplication(application, status, threading.Event())
    configuration = uvicorn.Config(observed, host="127.0.0.1", port=0,
        access_log=False, log_level="critical", lifespan="on")
    server = supervised_server(configuration, application)
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(2):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.01)
        server.should_exit = True
        await asyncio.wait_for(serving, 2)
        assert not observed.drained
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 2)
