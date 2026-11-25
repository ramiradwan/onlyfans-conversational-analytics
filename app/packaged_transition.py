"""Continue the first passkey setup on Brain's owned loopback listener."""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from starlette.responses import Response
from starlette.routing import Router


logger = logging.getLogger(__name__)


class SetupRuntimeTransition:
    """Retire the setup surface before admitting the configured application."""

    def __init__(self, setup, *, load_runtime, exit_for_restart):
        self._setup = self._current = setup
        self._load_runtime = load_runtime
        self._exit_for_restart = exit_for_restart
        self._lifespan = Router(lifespan=self._run)
        self._ready = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._requests = 0
        self._accepting = True
        self._pending = None
        self.configured = False

    @property
    def state(self):
        return self._current.state

    def request(self) -> bool:
        """Called only after ordinary Connect and setup finalization commit."""
        if self._ready.is_set():
            return True
        self._pending = self._setup.state.desktop_setup_core.account_passkeys.take_initial_access()
        if self._pending is None:
            return False
        self._ready.set()
        return True

    @asynccontextmanager
    async def _run(self, _application):
        async with AsyncExitStack() as lifecycle:
            await lifecycle.enter_async_context(self._setup.router.lifespan_context(self._setup))
            transition = asyncio.create_task(self._continue(lifecycle))
            try:
                yield
            finally:
                transition.cancel()
                await asyncio.gather(transition, return_exceptions=True)

    async def _continue(self, lifecycle) -> None:
        await self._ready.wait()
        try:
            presentation, access = self._pending
            self._pending = None
            self._accepting = False
            self._setup.state.desktop_coordinator.begin_shutdown()
            async with asyncio.timeout(300):
                await self._idle.wait()
                await lifecycle.aclose()
                runtime = await self._load_runtime()
                await lifecycle.enter_async_context(runtime.router.lifespan_context(runtime))
                from app.api.endpoints.saved_data import stage_initial_access

                await asyncio.to_thread(
                    self._setup.state.desktop_admissions.continue_in_runtime,
                    presentation.identifier, presentation.generation,
                )
                stage_initial_access(access)
            self._current = runtime
            self.configured = self._accepting = True
        except Exception as error:
            # Setup is already durable. An unsuccessful transition can only
            # resume via the existing configured restart and a fresh assertion.
            logger.warning("setup_runtime_transition_refused error_type=%s", type(error).__name__)
            self._exit_for_restart()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        if not self._accepting:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            else:
                await Response(status_code=503, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        if self.configured:
            await self._current(scope, receive, send)
            return
        self._requests += 1
        self._idle.clear()
        try:
            await self._setup(scope, receive, send)
        finally:
            self._requests -= 1
            if not self._requests:
                self._idle.set()
