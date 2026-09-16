"""Production Agent admission and RPC over an authenticated Noise session."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from uuid import UUID

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.api.endpoints.companion_pairing import _socket_origin
from app.bootstrap import transport_manager
from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.analysis_authorization import current_analysis_readiness
from app.security.companion_noise import create_noise_responder
from app.security.extension_storage import (
    UNLOCK_SCHEMA,
    extension_storage_key_base64,
    open_extension_storage_bootstrap,
    seal_extension_storage_bootstrap,
)
from app.transport.companion_channel import CompanionChannel
from app.transport.companion_records import (
    CompanionRecordError,
    document,
    encode_document,
)


def session_authority():
    from app.security.companion_session_authority import CompanionSessionAuthority

    return CompanionSessionAuthority(
        SQLiteAuthenticationStore(settings.auth_database_path)
    )


_BACKGROUND = set()


async def _owned(factory):
    """Dispose a native state/authority handle if its worker returns after cancel."""
    abandoned = threading.Event()
    lock = threading.Lock()
    values = []

    def work():
        value = factory()
        with lock:
            values.append(value)
            stop = abandoned.is_set()
        if stop:
            value.close()
        return value

    task = asyncio.create_task(asyncio.to_thread(work))
    _BACKGROUND.add(task)

    def completed(done):
        _BACKGROUND.discard(done)
        with suppress(BaseException):
            done.result()

    task.add_done_callback(completed)
    try:
        return await asyncio.shield(task)
    except BaseException:
        with lock:
            abandoned.set()
            value = values[0] if values else None
        if value is not None:
            cleanup = asyncio.create_task(asyncio.to_thread(value.close))
            _BACKGROUND.add(cleanup)
            cleanup.add_done_callback(completed)
            with suppress(Exception):
                await asyncio.shield(cleanup)
        raise


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise CompanionRecordError()
    return value


class ProtectedSocket:
    """Protocol-v2 adapter. Its only network output is the encrypted channel."""

    def __init__(self, channel):
        self.channel = channel
        self.authority = channel.authority
        self.queue = asyncio.Queue(maxsize=8)
        self.client = channel.websocket.client

    def authorization_guard(self):
        return self.authority.operation()

    async def accept(self):
        self.channel.check()

    async def receive_text(self):
        value = await self.queue.get()
        self.channel.check()
        await asyncio.to_thread(self.authority.current_policy)
        return encode_document(value).decode("utf-8")

    async def send_text(self, raw):
        await self.channel.send(document(raw))

    async def close(self, code=1000, reason=""):
        await self.channel.close(code)


class SessionRPC:
    def __init__(self, authority, pin):
        self.authority, self.pin = authority, pin
        self.auth_ticket = None
        self.reconnect_ticket = None
        self.config_ticket = None

    def hello(self, ticket, account, installation):
        result = self.authority.accept_hello(ticket, account, str(installation))
        self.reconnect_ticket, self.config_ticket = result[2:]
        return result

    def _bootstrap(self, ticket, kind):
        return seal_extension_storage_bootstrap(
            extension_id=settings.extension_id,
            creator_account_id=self.pin.creator_account_id,
            credential_kind=kind,
            auth_ticket=ticket,
        )

    def call(self, method, params):
        policy = self.authority.current_policy()
        if method == "agent.challenge":
            _exact(params, ())
            return self.authority.challenge()
        if method == "agent.authenticate":
            _exact(params, ("challenge_id", "signature"))
            result = self.authority.authenticate(
                params["challenge_id"], params["signature"]
            )
            self.auth_ticket = result["auth_ticket"]
            return {
                **result,
                "storage_bootstrap": self._bootstrap(self.auth_ticket, "pairing"),
            }
        if self.auth_ticket is None:
            raise CompanionRecordError()
        if method == "agent.analysis.readiness":
            _exact(params, ())
            identity = policy.identity
            if (
                identity is None
                or identity.role != "agent"
                or identity.creator_account_id != self.pin.creator_account_id
            ):
                raise CompanionRecordError()
            readiness = current_analysis_readiness(
                self.authority.authentication,
                identity,
            )
            return {
                "schema": "ofca-analysis-readiness/v1",
                "commercial_authority": readiness.commercial_authority,
                "analysis_admission": readiness.analysis_admission,
            }
        if method == "agent.config.get":
            from app.protocol import AgentConfigGetRequest

            _exact(
                params,
                (
                    "operation",
                    "protocol_version",
                    "auth_ticket",
                    "agent_installation_id",
                    "creator_account_id",
                    "current_etag",
                    "current_config_revision",
                    "supported_config_schema_versions",
                ),
            )
            request = AgentConfigGetRequest.model_validate_json(encode_document(params))
            self.authority.validate_config(
                request.auth_ticket,
                request.creator_account_id,
                str(request.agent_installation_id),
            )
            config = transport_manager.required_config_document(
                request.creator_account_id
            )
            matched = (
                request.current_etag is not None
                and request.current_etag.removeprefix("W/").strip('"') == config.etag
            )
            return {
                "status": 304 if matched else 200,
                "etag": config.etag,
                "document": None if matched else config.model_dump(mode="json"),
            }
        if method == "agent.storage.unseal":
            _exact(params, ("storage_bootstrap",))
            current = open_extension_storage_bootstrap(
                params["storage_bootstrap"], expected_extension_id=settings.extension_id
            )
            if current.creator_account_id != self.pin.creator_account_id:
                raise CompanionRecordError()
            with self.authority.operation():
                key = extension_storage_key_base64(
                    settings.auth_database_path,
                    extension_id=settings.extension_id,
                    creator_account_id=self.pin.creator_account_id,
                )
            return {
                "schema": UNLOCK_SCHEMA,
                "creator_account_id": self.pin.creator_account_id,
                "credential_kind": "pairing",
                "auth_ticket": self.auth_ticket,
                "storage_key_base64": key,
            }
        if method == "agent.storage.rotate":
            _exact(
                params,
                (
                    "protocol_version",
                    "creator_account_id",
                    "agent_installation_id",
                    "reconnect_auth_ticket",
                    "storage_bootstrap",
                    "config_auth_ticket",
                ),
            )
            if (
                params["protocol_version"] != "2"
                or params["reconnect_auth_ticket"] != self.reconnect_ticket
                or self.reconnect_ticket is None
            ):
                raise CompanionRecordError()
            self.authority.validate_config(
                params["config_auth_ticket"],
                params["creator_account_id"],
                params["agent_installation_id"],
            )
            current = open_extension_storage_bootstrap(
                params["storage_bootstrap"], expected_extension_id=settings.extension_id
            )
            if current.creator_account_id != self.pin.creator_account_id:
                raise CompanionRecordError()
            return {
                "schema": "ofca-extension-storage-rotation/v1",
                "storage_bootstrap": self._bootstrap(
                    self.reconnect_ticket, "reconnect"
                ),
            }
        raise CompanionRecordError()


async def _serve(channel, pin):
    from app.api.endpoints.transport_ws import _agent_socket

    socket = ProtectedSocket(channel)
    rpc = SessionRPC(channel.authority, pin)
    inbound = asyncio.Queue(maxsize=8)

    async def read():
        while True:
            value = await channel.receive()
            inbound.put_nowait(value)

    async def dispatch():
        identifiers = set()
        while True:
            value = await inbound.get()
            if value.get("type") != "rpc.request":
                socket.queue.put_nowait(value)
                continue
            _exact(value, ("type", "id", "method", "params"))
            if (
                not isinstance(value["id"], str)
                or str(UUID(value["id"])) != value["id"]
            ):
                raise CompanionRecordError()
            if value["id"] in identifiers or len(identifiers) >= 1_024:
                raise CompanionRecordError()
            identifiers.add(value["id"])
            try:
                async with asyncio.timeout(10):
                    result = await asyncio.to_thread(
                        rpc.call, value["method"], value["params"]
                    )
            except Exception:
                # Diagnostics and wire errors never contain the request or provider failure.
                await channel.send(
                    {
                        "type": "rpc.response",
                        "id": value["id"],
                        "error": "session_request_refused",
                    }
                )
                raise CompanionRecordError() from None
            await channel.send(
                {"type": "rpc.response", "id": value["id"], "result": result}
            )

    tasks = [
        asyncio.create_task(read()),
        asyncio.create_task(dispatch()),
        asyncio.create_task(_agent_socket(socket, authenticate=rpc.hello)),
        asyncio.create_task(channel.watch_authority()),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        try:
            await channel.close(1008)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def companion_session_socket(websocket: WebSocket):
    noise = authority = channel = None
    try:
        _socket_origin(websocket)
        await websocket.accept()
        deadline = asyncio.get_running_loop().time() + 2
        async with asyncio.timeout_at(deadline):
            first = await websocket.receive_bytes()
            if not 33 <= len(first) <= 4_096:
                raise CompanionRecordError()
            provider = await asyncio.to_thread(session_authority)
            snapshot = await asyncio.to_thread(provider.prepare, first[:32])
            noise = await _owned(lambda: create_noise_responder(snapshot.pin))
            noise.consume_first_handshake(first[32:])
            await websocket.send_bytes(noise.produce_responder_handshake())
            noise.enter_transport()
            confirmation = await websocket.receive_bytes()
            if (
                not 17 <= len(confirmation) <= 4_096
                or noise.decrypt_transport(confirmation) != b"\x00client-ready"
            ):
                raise CompanionRecordError()
            authority = await _owned(lambda: provider.authorize(snapshot))
            await websocket.send_bytes(noise.encrypt_transport(b"\x00server-ready"))
            authorization = encode_document(snapshot.authorization)
            if len(authorization) > 36_847:
                raise CompanionRecordError()

            def authorize_record():
                with authority.operation():
                    return noise.encrypt_transport(b"\x01" + authorization)

            frame = await asyncio.to_thread(authorize_record)
            if asyncio.get_running_loop().time() >= deadline:
                raise CompanionRecordError()
            await websocket.send_bytes(frame)
        channel = CompanionChannel(websocket, noise, authority, snapshot.expires_at)
        await _serve(channel, snapshot.pin)
    except (Exception, asyncio.CancelledError):
        with suppress(Exception):
            async with asyncio.timeout(2):
                await websocket.close(code=1008, reason="session_refused")
    finally:
        if channel is not None:
            await channel.close()
        else:
            if noise is not None:
                noise.close()
            if authority is not None:
                with suppress(Exception):
                    await asyncio.shield(asyncio.to_thread(authority.close))
