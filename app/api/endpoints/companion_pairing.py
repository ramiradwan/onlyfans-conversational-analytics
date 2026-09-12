"""Bounded HTTP and loopback WebSocket adapters for companion pairing.

Only pairing material crosses the WebSocket. This adapter does not admit Full
traffic or expose an unauthenticated key, ticket, or bootstrap endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import threading
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.api.activation import require_activated_runtime
from app.api.security import (
    get_authenticated_runtime_policy,
    get_runtime_policy,
    verify_csrf_token,
    verify_same_origin,
)
from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.companion_pairing import (
    CompanionPairingError,
    CompanionPairingService,
)
from app.security.installation_key import (
    InstallationKeyAuthority,
    WindowsCNGInstallationKeyProvider,
)

router = APIRouter(tags=["Companion pairing"])
STEP_TIMEOUT_SECONDS = 10.0
WINDOW_TIMEOUT_SECONDS = 300.0
OUTCOME_POLL_SECONDS = 0.1
HTTP_MAX_BODY_BYTES = 2_048
FRAME_MAX_BYTES = 36_864
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_TERMINAL = {"confirmed", "declined", "expired", "cancelled"}
_REFUSALS = {
    "pairing_account_refused",
    "pairing_generation_refused",
    "pairing_grant_refused",
    "pairing_key_refused",
    "pairing_message_invalid",
    "pairing_nonce_refused",
    "pairing_proof_refused",
    "pairing_state_refused",
    "pairing_storage_refused",
}
_STATUS_FIELDS = {
    "pairing_id",
    "creator_account_id",
    "generation",
    "version",
    "state",
    "expires_at",
    "comparison_code",
    "agent_identity_thumbprint",
}
_WIRE_FIELDS = {
    "pair.offer": {
        "type",
        "pairing_id",
        "generation",
        "creator_account_id",
        "brain_noise_key",
        "brain_nonce",
        "installation_jwk",
        "installation_grant",
        "creator_account_binding",
        "brain_proof",
    },
    "pair.result": {"type", "pairing_id", "outcome"},
}
_BACKGROUND: set[asyncio.Task] = set()


class _Refusal(Exception):
    def __init__(self, code: str = "pairing_message_invalid", status: int = 400):
        self.code = code if code in _REFUSALS else "pairing_state_refused"
        self.status = status
        super().__init__(self.code)


class _Ended(Exception):
    def __init__(self, outcome: str):
        self.outcome = outcome


def companion_pairing_service() -> CompanionPairingService:
    """Use the activated installation key without provisioning a replacement."""
    store = SQLiteAuthenticationStore(settings.auth_database_path)
    return CompanionPairingService(
        store, InstallationKeyAuthority(store, WindowsCNGInstallationKeyProvider())
    )


def _fixed_code(error: CompanionPairingError) -> str:
    code = getattr(error, "code", "pairing_state_refused")
    return code if code in _REFUSALS else "pairing_state_refused"


def _response(value: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status, headers=_NO_STORE)


def _public_status(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATUS_FIELDS:
        raise _Refusal("pairing_storage_refused", 503)
    return value


def _pairing_id(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise _Refusal()
    raw = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise _Refusal()
    return raw


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Refusal()
        result[key] = value
    return result


def _non_json_constant(_: str) -> None:
    raise _Refusal()


def _document(raw: bytes | str, maximum: int) -> dict[str, Any]:
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > maximum:
            raise _Refusal()
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_non_json_constant,
        )
        if not isinstance(document, dict):
            raise _Refusal()
        return document
    except (UnicodeError, ValueError, RecursionError):
        raise _Refusal() from None


async def _body(request: Request, field: str) -> dict[str, Any]:
    raw = bytearray()
    async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            if len(raw) + len(chunk) > HTTP_MAX_BODY_BYTES:
                raise _Refusal()
            raw.extend(chunk)
    body = _document(bytes(raw), HTTP_MAX_BODY_BYTES)
    if set(body) != {field}:
        raise _Refusal()
    if field == "version":
        if type(body[field]) is not int or not 0 <= body[field] <= 2**53 - 1:
            raise _Refusal()
    elif not isinstance(body[field], str) or not _ID.fullmatch(body[field]):
        raise _Refusal()
    return body


def _bridge_policy(request: Request, *, mutation: bool):
    require_activated_runtime()
    expected = urlsplit(settings.bridge_origin)
    if (
        request.headers.get("host", "").lower() != expected.netloc.lower()
        or request.query_params
    ):
        raise _Refusal("pairing_account_refused", 403)
    policy = get_authenticated_runtime_policy(get_runtime_policy(request))
    if mutation:
        if request.headers.get("origin") != f"{expected.scheme}://{expected.netloc}":
            raise _Refusal("pairing_account_refused", 403)
        verify_same_origin(request)
        verify_csrf_token(policy, request.headers.get("x-csrf-token"))
    return policy


async def _http_operation(
    request: Request, operation: str, pairing_id: str | None = None
):
    try:
        async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
            mutation = operation != "status"
            policy = await asyncio.to_thread(_bridge_policy, request, mutation=mutation)
            if pairing_id is not None:
                _pairing_id(pairing_id)
            if mutation:
                body = await _body(
                    request, "creator_account_id" if operation == "open" else "version"
                )
            else:
                async for chunk in request.stream():
                    if chunk:
                        raise _Refusal()
                body = {}
            service = await asyncio.to_thread(companion_pairing_service)
            if operation == "open":
                call = lambda: service.open(policy, body["creator_account_id"])
            elif operation == "status":
                call = lambda: service.status(policy, pairing_id)
            elif operation == "confirm":
                call = lambda: service.confirm(policy, pairing_id, body["version"])
            else:
                call = lambda: service.cancel(
                    policy, pairing_id, body["version"], decline=operation == "decline"
                )
            result = await _http_call(
                request,
                call,
                service=service,
                operation=operation,
                pairing_id=pairing_id,
            )
        return _response(_public_status(result), 201 if operation == "open" else 200)
    except _Refusal as error:
        return _response({"detail": error.code}, error.status)
    except HTTPException as error:
        code = (
            "pairing_storage_refused"
            if error.status_code == 503
            else "pairing_account_refused"
        )
        return _response({"detail": code}, error.status_code)
    except CompanionPairingError as error:
        code = _fixed_code(error)
        return _response(
            {"detail": code}, 403 if code == "pairing_account_refused" else 409
        )
    except TimeoutError:
        return _response({"detail": "pairing_state_refused"}, 408)
    except Exception:
        return _response({"detail": "pairing_storage_refused"}, 503)


async def _http_call(
    request, function: Callable, *, service, operation: str, pairing_id: str | None
):
    abandoned = threading.Event()
    result_lock = threading.Lock()
    results: list[Any] = []
    mutating = operation in {"open", "confirm"}

    def result_id(result):
        if operation == "confirm":
            return _pairing_id(pairing_id)
        if operation == "open" and isinstance(result, dict):
            return _pairing_id(result.get("pairing_id"))
        return None

    def work():
        result = function()
        with result_lock:
            results.append(result)
            stop = abandoned.is_set()
        if stop and mutating:
            known_id = result_id(result)
            if known_id is not None:
                service.abort(known_id)
        return result

    task = asyncio.create_task(asyncio.to_thread(work))
    disconnect = asyncio.create_task(request.receive())
    success = False
    try:
        done, _ = await asyncio.wait(
            {task, disconnect}, return_when=asyncio.FIRST_COMPLETED
        )
        if disconnect in done:
            raise _Refusal("pairing_state_refused", 408)
        result = _public_status(await task)
        success = True
        return result
    finally:
        await _stop(disconnect)
        disconnected = not disconnect.cancelled()
        if disconnected:
            success = False
        if not success:
            with result_lock:
                abandoned.set()
                result = results[0] if results else None
            if mutating and (result is not None or operation == "confirm"):
                with suppress(_Refusal):
                    known_id = result_id(result)
                    if known_id is not None:
                        await _abort(service, known_id)
        _detach(task)
        if disconnected:
            raise _Refusal("pairing_state_refused", 408) from None


@router.post("/api/v1/companion/pairings")
async def open_pairing(request: Request):
    return await _http_operation(request, "open")


@router.get("/api/v1/companion/pairings/{pairing_id}")
async def pairing_status(request: Request, pairing_id: str):
    return await _http_operation(request, "status", pairing_id)


@router.post("/api/v1/companion/pairings/{pairing_id}/confirm")
async def confirm_pairing(request: Request, pairing_id: str):
    return await _http_operation(request, "confirm", pairing_id)


@router.post("/api/v1/companion/pairings/{pairing_id}/cancel")
async def cancel_pairing(request: Request, pairing_id: str):
    return await _http_operation(request, "cancel", pairing_id)


@router.post("/api/v1/companion/pairings/{pairing_id}/decline")
async def decline_pairing(request: Request, pairing_id: str):
    return await _http_operation(request, "decline", pairing_id)


def _socket_origin(websocket: WebSocket) -> None:
    expected_host = urlsplit(settings.bridge_origin).netloc.lower()
    client = websocket.client
    try:
        loopback = client is not None and ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        loopback = False
    if (
        not loopback
        or settings.websocket_bind_host not in {"127.0.0.1", "localhost", "::1"}
        or not re.fullmatch(r"[a-p]{32}", settings.extension_id)
        or websocket.headers.get("host", "").lower() != expected_host
        or websocket.headers.get("origin")
        != f"chrome-extension://{settings.extension_id}"
        or websocket.query_params
        or any(
            name in websocket.headers
            for name in ("authorization", "cookie", "sec-websocket-protocol")
        )
    ):
        raise _Refusal("pairing_account_refused", 403)
    require_activated_runtime()


def _event_document(event: dict[str, Any]) -> dict[str, Any]:
    if event["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(event.get("code", 1000))
    if event.get("text") is None:
        raise _Refusal()
    return _document(event["text"], FRAME_MAX_BYTES)


class _SocketInput:
    """One pending ASGI receive survives every protocol-stage transition."""

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.task = asyncio.create_task(websocket.receive())

    def take(self):
        event = self.task.result()
        self.task = asyncio.create_task(self.websocket.receive())
        return _event_document(event)


async def _terminal_outcome(service, pairing_id: bytes) -> str:
    while True:
        outcome = await asyncio.to_thread(service.outcome, pairing_id)
        if outcome is not None:
            if outcome not in _TERMINAL:
                raise _Refusal("pairing_state_refused")
            return outcome
        await asyncio.sleep(OUTCOME_POLL_SECONDS)


def _detach(task: asyncio.Task) -> None:
    _BACKGROUND.add(task)

    def finished(done: asyncio.Task) -> None:
        _BACKGROUND.discard(done)
        with suppress(BaseException):
            done.result()

    task.add_done_callback(finished)


async def _stop(task: asyncio.Task | None) -> None:
    if task is not None:
        task.cancel()
        with suppress(BaseException):
            await task


async def _abort(service, pairing_id: bytes) -> None:
    # The shield lets the database fence finish even when the socket task itself
    # is cancelled. No key material or exception payload is sent to diagnostics.
    task = asyncio.create_task(asyncio.to_thread(service.abort, pairing_id))
    _detach(task)
    with suppress(Exception):
        await asyncio.shield(task)


async def _work_step(
    incoming: _SocketInput,
    function: Callable,
    *,
    service=None,
    pairing_id=None,
    claim=False,
):
    abandoned = threading.Event()
    result_lock = threading.Lock()
    claimed: list[Any] = []

    def work():
        result = function()
        if claim:
            with result_lock:
                claimed.append(result)
                stop = abandoned.is_set()
            if stop:
                service.abort(result.pairing_id)
        return result

    task = asyncio.create_task(asyncio.to_thread(work))
    inbound = incoming.task
    terminal = (
        asyncio.create_task(_terminal_outcome(service, pairing_id))
        if pairing_id is not None
        else None
    )
    success = False
    try:
        watched = {task, inbound} | ({terminal} if terminal is not None else set())
        done, _ = await asyncio.wait(
            watched, timeout=STEP_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED
        )
        if inbound in done:
            raise _Refusal("pairing_state_refused")
        if terminal in done:
            raise _Ended(await terminal)
        if task not in done:
            raise TimeoutError
        result = await task
        success = True
        return result
    finally:
        await _stop(terminal)
        # A frame can complete between FIRST_COMPLETED and cancellation. Never
        # discard it silently when moving to the next protocol step.
        unexpected_input = inbound.done()
        if unexpected_input:
            success = False
        if not success:
            with result_lock:
                abandoned.set()
                result = claimed[0] if claimed else None
            if result is not None:
                await _abort(service, result.pairing_id)
        _detach(task)
        if unexpected_input:
            raise _Refusal("pairing_state_refused") from None


async def _input_step(incoming: _SocketInput, service, pairing_id: bytes):
    inbound = incoming.task
    terminal = asyncio.create_task(_terminal_outcome(service, pairing_id))
    try:
        done, _ = await asyncio.wait(
            {inbound, terminal},
            timeout=STEP_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if terminal in done:
            raise _Ended(await terminal)
        if inbound not in done:
            raise TimeoutError
        return incoming.take()
    finally:
        await _stop(terminal)


async def _send(websocket: WebSocket, document: dict[str, Any]) -> None:
    if not isinstance(document, dict) or set(document) != _WIRE_FIELDS.get(
        document.get("type")
    ):
        raise _Refusal("pairing_state_refused")
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > FRAME_MAX_BYTES:
        raise _Refusal("pairing_state_refused")
    async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
        await websocket.send_text(encoded)


@router.websocket("/ws/agent/pairing")
async def companion_pairing_socket(websocket: WebSocket) -> None:
    service = None
    pairing_id = None
    complete = False
    incoming = None
    try:
        _socket_origin(websocket)
        await websocket.accept()
        incoming = _SocketInput(websocket)
        async with asyncio.timeout(WINDOW_TIMEOUT_SECONDS):
            async with asyncio.timeout(STEP_TIMEOUT_SECONDS):
                await asyncio.shield(incoming.task)
                request = incoming.take()
            if request.get("type") != "pair.request":
                raise _Refusal()
            service = await _work_step(incoming, companion_pairing_service)
            window = await _work_step(
                incoming, service.claim, service=service, claim=True
            )
            pairing_id = window.pairing_id
            offer = await _work_step(
                incoming,
                lambda: service.offer(window, request),
                service=service,
                pairing_id=pairing_id,
            )
            await _send(websocket, offer)
            confirm = await _input_step(incoming, service, pairing_id)
            await _work_step(
                incoming,
                lambda: service.confirm_agent(pairing_id, confirm),
                service=service,
                pairing_id=pairing_id,
            )
            inbound = incoming.task
            terminal = asyncio.create_task(_terminal_outcome(service, pairing_id))
            try:
                done, _ = await asyncio.wait(
                    {inbound, terminal}, return_when=asyncio.FIRST_COMPLETED
                )
                if inbound in done:
                    raise _Refusal("pairing_state_refused")
                outcome = await terminal
            finally:
                await _stop(terminal)
            if inbound.done():
                raise _Refusal("pairing_state_refused")
            await _send(
                websocket,
                {
                    "type": "pair.result",
                    "pairing_id": base64.urlsafe_b64encode(pairing_id)
                    .rstrip(b"=")
                    .decode("ascii"),
                    "outcome": outcome,
                },
            )
            if inbound.done():
                raise _Refusal("pairing_state_refused")
            complete = True
        await websocket.close(code=1000)
    except _Ended as ended:
        try:
            if incoming.task.done():
                raise _Refusal("pairing_state_refused")
            await _send(
                websocket,
                {
                    "type": "pair.result",
                    "pairing_id": base64.urlsafe_b64encode(pairing_id)
                    .rstrip(b"=")
                    .decode("ascii"),
                    "outcome": ended.outcome,
                },
            )
            if incoming.task.done():
                raise _Refusal("pairing_state_refused")
            complete = True
            await websocket.close(code=1000)
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1008, reason="pairing_state_refused")
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except (_Refusal, CompanionPairingError, HTTPException, TimeoutError) as error:
        code = (
            _fixed_code(error)
            if isinstance(error, CompanionPairingError)
            else error.code if isinstance(error, _Refusal) else "pairing_state_refused"
        )
        with suppress(Exception):
            await websocket.close(code=1008, reason=code)
    except Exception:
        with suppress(Exception):
            await websocket.close(code=1011, reason="pairing_storage_refused")
    finally:
        if incoming is not None:
            await _stop(incoming.task)
        if service is not None and pairing_id is not None and not complete:
            await _abort(service, pairing_id)
