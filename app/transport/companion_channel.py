"""Noise-protected, bounded message stream for one admitted Agent session."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from starlette.websockets import WebSocketDisconnect

from app.transport.companion_records import Assembly, CompanionRecordError, fragments

MAX_RECORDS = 1_048_576


class CompanionChannel:
    def __init__(
        self, websocket, noise, authority, expires_at, *, clock=time.monotonic
    ):
        self.websocket, self.noise, self.authority = websocket, noise, authority
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + min(900, expires_at.timestamp() - time.time())
        self.last_clock = self.started
        self.last_wall = time.time()
        self.expires_at = expires_at.timestamp()
        self.closed = False
        self.records = 0
        self.assembly = Assembly(clock=clock)
        self.send_lock = asyncio.Lock()
        self.pending_sends = 0

    def check(self):
        now = self.clock()
        wall = time.time()
        if (
            self.closed
            or now < self.last_clock
            or wall < self.last_wall
            or now >= self.deadline
            or wall >= self.expires_at
        ):
            raise CompanionRecordError()
        self.last_clock = now
        self.last_wall = wall

    def count(self):
        self.check()
        self.records += 1
        if self.records > MAX_RECORDS:
            raise CompanionRecordError()

    async def receive(self):
        while True:
            self.check()
            timeout = self.deadline - self.clock()
            if self.assembly.remaining is not None:
                timeout = min(timeout, self.assembly.remaining)
            async with asyncio.timeout(max(0, timeout)):
                event = await self.websocket.receive()
            if event["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(event.get("code", 1000))
            frame = event.get("bytes")
            self.count()
            if not isinstance(frame, bytes) or not 17 <= len(frame) <= 4_096:
                raise CompanionRecordError()
            plain = self.noise.decrypt_transport(frame)
            if not plain or plain[0] != 1:
                raise CompanionRecordError()
            value = self.assembly.feed(plain[1:])
            if value is not None:
                await asyncio.to_thread(self.authority.current_policy)
                self.check()
                return value

    async def send(self, value):
        self.check()
        self.pending_sends += 1
        try:
            if self.pending_sends > 8:
                raise CompanionRecordError()
            async with asyncio.timeout(10):
                async with self.send_lock:
                    for part in fragments(value):
                        self.count()
                        frame = await asyncio.to_thread(self._seal, part)
                        self.check()
                        if len(frame) > 4_096:
                            raise CompanionRecordError()
                        await self.websocket.send_bytes(frame)
        finally:
            self.pending_sends -= 1

    def _seal(self, part):
        # Each record's admission serializes with revocation. No database lock
        # crosses an await or waits for network backpressure.
        with self.authority.operation():
            return self.noise.encrypt_transport(b"\x01" + part)

    async def watch_authority(self):
        while True:
            self.check()
            await asyncio.to_thread(self.authority.current_policy)
            await asyncio.sleep(min(0.25, max(0, self.deadline - self.clock())))

    async def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        self.assembly.clear()
        self.noise.close()
        # Revocation/closure is durable even if shutdown cancels this coroutine.
        cleanup = asyncio.create_task(asyncio.to_thread(self.authority.close))
        cleanup.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        with suppress(Exception):
            await asyncio.shield(cleanup)
        with suppress(Exception):
            async with asyncio.timeout(2):
                await self.websocket.close(
                    code=code, reason="" if code == 1000 else "session_refused"
                )
