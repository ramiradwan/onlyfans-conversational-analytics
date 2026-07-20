"""ADR-aligned Agent and Bridge transports."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import ValidationError

from app.core.config import settings
from app.protocol import (
    AGENT_TO_BRAIN_ADAPTER,
    BRIDGE_TO_BRAIN_ADAPTER,
    AgentConfigDocumentResponse,
    AgentConfigGetRequest,
    MAX_SNAPSHOT_FRAME_BYTES,
)
from app.persistence.history import InvariantViolation
from app.transport.manager import (
    DEV_ACCOUNT_ID,
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_TIMEOUT_SECONDS,
    AgentLease,
    AuthenticationError,
    AuthorizationError,
    BridgeBinding,
    transport_manager,
    utc_now,
)


router = APIRouter()

AGENT_TYPES = {
    "agent.hello",
    "agent.heartbeat",
    "ingest.snapshot",
    "ingest.delta",
    "presence.observed",
    "config.applied",
    "command.result",
}
BRIDGE_TYPES = {"bridge.hello", "state.resync"}
KNOWN_SERVER_TYPES = {
    "agent.session",
    "bridge.session",
    "sync.required",
    "ingest.ack",
    "ingest.rejected",
    "state.snapshot",
    "state.delta",
    "presence.state",
    "agent.state",
    "system.state",
    "protocol.error",
    "config.available",
    "command.execute",
    "command.result.ack",
}


def _safe_document(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _classify_validation_error(
    raw: str, role: Literal["agent", "bridge"]
) -> tuple[str, UUID | None, str]:
    document = _safe_document(raw)
    if document is None:
        return "validation_failed", None, "Frame is not a JSON object"
    message_id = _safe_uuid(document.get("message_id"))
    if document.get("protocol_version") != "2":
        return "unsupported_version", message_id, "Only protocol version 2 is supported"
    message_type = document.get("type")
    allowed = AGENT_TYPES if role == "agent" else BRIDGE_TYPES
    other = BRIDGE_TYPES if role == "agent" else AGENT_TYPES
    if message_type in other or message_type in KNOWN_SERVER_TYPES:
        return "wrong_role", message_id, f"{message_type!s} is not allowed on the {role} endpoint"
    if message_type not in allowed:
        return "validation_failed", message_id, "Unknown or malformed message discriminator"
    return "validation_failed", message_id, "Message failed protocol v2 validation"


async def _protocol_error(
    websocket: WebSocket,
    role: Literal["agent", "bridge"],
    *,
    code: str,
    related_message_id: UUID | None,
    detail: str,
    fatal: bool,
    retryable: bool = False,
) -> None:
    payload = {
        "code": code,
        "related_message_id": str(related_message_id) if related_message_id else None,
        "retryable": retryable,
        "fatal": fatal,
        "detail": detail,
    }
    try:
        sender = transport_manager.send_agent if role == "agent" else transport_manager.send_bridge
        await sender(websocket, "protocol.error", payload, correlation_id=related_message_id)
    finally:
        if fatal:
            close_code = 1008 if code in {"unauthorized", "identity_conflict"} else 1002
            await websocket.close(code=close_code, reason=code)


async def _invalid_ingest(
    websocket: WebSocket,
    lease: AgentLease,
    document: dict[str, Any],
    *,
    code: str = "invalid_payload",
    detail: str = "Ingest message failed protocol v2 validation",
    retryable: bool = False,
) -> bool:
    message_id = _safe_uuid(document.get("message_id"))
    if message_id is None:
        return False
    payload = document.get("payload")
    event_id = _safe_uuid(payload.get("event_id")) if isinstance(payload, dict) else None
    await transport_manager.send_agent(
        websocket,
        "ingest.rejected",
        {
            "connection_id": str(lease.connection_id),
            "creator_account_id": lease.creator_account_id,
            "rejected_message_id": str(message_id),
            "event_id": str(event_id) if event_id else None,
            "code": code,
            "retryable": retryable,
            "detail": detail,
        },
        correlation_id=message_id,
    )
    return True


def _identity_matches_agent(message: Any, lease: AgentLease) -> tuple[bool, str]:
    payload = message.payload
    if payload.creator_account_id != lease.creator_account_id:
        return False, "creator_account_id conflicts with the immutable socket binding"
    if payload.connection_id != lease.connection_id:
        return False, "connection_id conflicts with the immutable socket binding"
    if payload.fencing_token != lease.fencing_token:
        return False, "fencing token is stale"
    if message.type in {"ingest.snapshot", "ingest.delta"}:
        if payload.agent_installation_id != lease.agent_installation_id:
            return False, "agent_installation_id conflicts with the immutable socket binding"
        if payload.agent_stream_id != lease.agent_stream_id:
            return False, "agent_stream_id conflicts with the immutable socket binding"
    return True, ""


async def _handle_agent_message(websocket: WebSocket, lease: AgentLease, message: Any) -> bool:
    if message.type == "agent.hello":
        await _protocol_error(
            websocket,
            "agent",
            code="validation_failed",
            related_message_id=message.message_id,
            detail="agent.hello is only valid as the first frame",
            fatal=True,
        )
        return False

    matches, detail = _identity_matches_agent(message, lease)
    if not matches or not transport_manager.is_current_fence(lease):
        stale_fence = "fenc" in detail or not transport_manager.is_current_fence(lease)
        if message.type in {"ingest.snapshot", "ingest.delta"} and stale_fence:
            await _invalid_ingest(
                websocket,
                lease,
                {
                    "message_id": str(message.message_id),
                    "payload": message.payload.model_dump(mode="json"),
                },
                code="stale_fence",
                detail="The Agent connection no longer owns the active fencing token",
            )
            return True
        await _protocol_error(
            websocket,
            "agent",
            code="identity_conflict",
            related_message_id=message.message_id,
            detail=detail or "The Agent connection has been superseded",
            fatal=True,
        )
        return False

    if message.type == "agent.heartbeat":
        await transport_manager.heartbeat(lease, message.payload.applied_config_revision)
        return True

    if message.type == "presence.observed":
        accepted = await transport_manager.observe_presence(
            lease,
            observation_id=message.payload.observation_id,
            observed_at=message.payload.observed_at,
            online_platform_user_ids=message.payload.online_platform_user_ids,
        )
        if not accepted:
            await _protocol_error(
                websocket,
                "agent",
                code="validation_failed",
                related_message_id=message.message_id,
                detail="Presence observation is duplicate, out of order, or already expired",
                fatal=False,
            )
        return True

    if message.type in {"ingest.snapshot", "ingest.delta"}:
        try:
            if message.type == "ingest.snapshot":
                outcome = transport_manager.ingest_snapshot(lease, message.payload)
            else:
                outcome = transport_manager.ingest_delta(lease, message.payload)
        except (InvariantViolation, sqlite3.IntegrityError, ValueError) as error:
            await _invalid_ingest(
                websocket,
                lease,
                {
                    "message_id": str(message.message_id),
                    "payload": message.payload.model_dump(mode="json"),
                },
                code="invariant_failed",
                detail=str(error) or "Canonical ingestion invariant failed",
            )
            return True

        if outcome.status in {"gap", "rejected"}:
            await _invalid_ingest(
                websocket,
                lease,
                {
                    "message_id": str(message.message_id),
                    "payload": message.payload.model_dump(mode="json"),
                },
                code=outcome.code or "invariant_failed",
                detail=outcome.detail or "Ingestion commit failed",
                retryable=outcome.retryable,
            )
            return True

        snapshot_progress = None
        if message.type == "ingest.snapshot":
            snapshot_progress = {
                "snapshot_id": str(message.payload.snapshot_id),
                "next_expected_chunk_index": outcome.next_expected_chunk_index or 0,
                "committed": outcome.snapshot_committed,
            }
        await transport_manager.send_agent(
            websocket,
            "ingest.ack",
            {
                "connection_id": str(lease.connection_id),
                "creator_account_id": lease.creator_account_id,
                "agent_stream_id": str(lease.agent_stream_id),
                "snapshot_id": (
                    str(outcome.snapshot_id) if outcome.snapshot_id is not None else None
                ),
                "committed_source_seq": outcome.committed_source_seq,
                "snapshot_progress": snapshot_progress,
            },
            correlation_id=message.message_id,
        )
        if outcome.canonical_revision is not None:
            transport_manager.schedule_projection(lease.creator_account_id)
        return True

    if message.type == "config.applied":
        await transport_manager.record_config_applied(lease, message.payload)
        return True

    if message.type == "command.result":
        recorded = transport_manager.commands.record_result(message.payload)
        await transport_manager.send_agent(
            websocket,
            "command.result.ack",
            {
                "connection_id": str(lease.connection_id),
                "creator_account_id": lease.creator_account_id,
                "command_id": str(recorded.command_id),
                "result_id": str(recorded.result_id),
                "recorded_at": recorded.recorded_at.isoformat(),
            },
            correlation_id=message.message_id,
        )
        return True

    return True


async def _agent_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    lease: AgentLease | None = None
    try:
        raw = await websocket.receive_text()
        try:
            hello = AGENT_TO_BRAIN_ADAPTER.validate_json(raw)
        except ValidationError:
            code, message_id, detail = _classify_validation_error(raw, "agent")
            await _protocol_error(
                websocket,
                "agent",
                code=code,
                related_message_id=message_id,
                detail=detail,
                fatal=True,
            )
            return
        if hello.type != "agent.hello":
            await _protocol_error(
                websocket,
                "agent",
                code="pre_handshake",
                related_message_id=hello.message_id,
                detail="agent.hello must be the first frame",
                fatal=True,
            )
            return
        try:
            principal_id, account_id, reconnect_auth_ticket, config_auth_ticket = (
                transport_manager.authenticate_agent_handshake(
                hello.payload.auth_ticket,
                hello.payload.requested_creator_account_id,
                hello.payload.agent_installation_id,
                )
            )
        except AuthenticationError as error:
            await _protocol_error(
                websocket,
                "agent",
                code="unauthorized",
                related_message_id=hello.message_id,
                detail=str(error),
                fatal=True,
            )
            return
        lease = await transport_manager.bind_agent(
            websocket,
            principal_id=principal_id,
            creator_account_id=account_id,
            agent_installation_id=hello.payload.agent_installation_id,
            agent_stream_id=hello.payload.agent_stream_id,
            config_auth_ticket=config_auth_ticket,
            applied_config_revision=hello.payload.applied_config_revision,
        )
        checkpoint = transport_manager.checkpoint_for(lease)
        pending_snapshot = transport_manager.pending_snapshot_for(lease)
        resume_action = (
            "resume"
            if checkpoint is not None
            and pending_snapshot is None
            and hello.payload.last_acknowledged_source_seq <= checkpoint
            else "snapshot_required"
        )
        required_config = transport_manager.required_config_document(account_id)
        await transport_manager.send_agent(
            websocket,
            "agent.session",
            {
                "connection_id": str(lease.connection_id),
                "fencing_token": lease.fencing_token,
                "creator_account_id": account_id,
                "agent_installation_id": str(lease.agent_installation_id),
                "agent_stream_id": str(lease.agent_stream_id),
                "committed_source_seq": checkpoint or 0,
                "resume_action": resume_action,
                "pending_snapshot_id": (
                    None if pending_snapshot is None else str(pending_snapshot[0])
                ),
                "next_expected_chunk_index": (
                    0 if pending_snapshot is None else pending_snapshot[1]
                ),
                "required_config_revision": required_config.config_revision,
                "reconnect_auth_ticket": reconnect_auth_ticket,
                "config_auth_ticket": lease.config_auth_ticket,
                "lease": {
                    "heartbeat_interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
                    "lease_timeout_seconds": LEASE_TIMEOUT_SECONDS,
                },
            },
            correlation_id=hello.message_id,
        )

        if resume_action == "snapshot_required":
            await transport_manager.send_agent(
                websocket,
                "sync.required",
                {
                    "connection_id": str(lease.connection_id),
                    "creator_account_id": account_id,
                    "reason": (
                        "missing_checkpoint"
                        if checkpoint is None
                        else "local_reset"
                    ),
                    "expected_agent_stream_id": str(lease.agent_stream_id),
                    "expected_next_source_seq": (checkpoint or 0) + 1,
                    "pending_snapshot_id": (
                        None if pending_snapshot is None else str(pending_snapshot[0])
                    ),
                    "next_expected_chunk_index": (
                        0 if pending_snapshot is None else pending_snapshot[1]
                    ),
                    "snapshot": {
                        "include_chats": True,
                        "include_messages": True,
                        "include_coverage_evidence": True,
                        "max_records_per_chunk": 100,
                        "max_frame_bytes": MAX_SNAPSHOT_FRAME_BYTES,
                    },
                },
            )
        while True:
            raw = await websocket.receive_text()
            raw_document = _safe_document(raw)
            if (
                raw_document is not None
                and raw_document.get("type") == "ingest.snapshot"
                and len(raw.encode("utf-8")) > MAX_SNAPSHOT_FRAME_BYTES
            ):
                if await _invalid_ingest(
                    websocket,
                    lease,
                    raw_document,
                    code="frame_too_large",
                    detail="Snapshot frame exceeds the 512 KiB protocol limit",
                ):
                    continue
            try:
                message = AGENT_TO_BRAIN_ADAPTER.validate_json(raw)
            except ValidationError:
                document = raw_document
                if (
                    document is not None
                    and document.get("type") in {"ingest.snapshot", "ingest.delta"}
                    and await _invalid_ingest(websocket, lease, document)
                ):
                    continue
                code, message_id, detail = _classify_validation_error(raw, "agent")
                fatal = code in {"unsupported_version", "wrong_role"}
                await _protocol_error(
                    websocket,
                    "agent",
                    code=code,
                    related_message_id=message_id,
                    detail=detail,
                    fatal=fatal,
                )
                if fatal:
                    return
                continue
            if not await _handle_agent_message(websocket, lease, message):
                return
    except WebSocketDisconnect:
        pass
    finally:
        if lease is not None:
            await transport_manager.disconnect_agent(lease.connection_id)


def _identity_matches_bridge(message: Any, binding: BridgeBinding) -> bool:
    payload = message.payload
    return (
        payload.connection_id == binding.connection_id
        and payload.bridge_session_id == binding.bridge_session_id
        and payload.creator_account_id == binding.creator_account_id
    )


async def _bridge_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    binding: BridgeBinding | None = None
    try:
        raw = await websocket.receive_text()
        try:
            hello = BRIDGE_TO_BRAIN_ADAPTER.validate_json(raw)
        except ValidationError:
            code, message_id, detail = _classify_validation_error(raw, "bridge")
            await _protocol_error(
                websocket,
                "bridge",
                code=code,
                related_message_id=message_id,
                detail=detail,
                fatal=True,
            )
            return
        if hello.type != "bridge.hello":
            await _protocol_error(
                websocket,
                "bridge",
                code="pre_handshake",
                related_message_id=hello.message_id,
                detail="bridge.hello must be the first frame",
                fatal=True,
            )
            return
        try:
            principal_id, account_id = transport_manager.authenticate(
                hello.payload.auth_ticket,
                hello.payload.requested_creator_account_id,
                role="bridge",
                bridge_session_id=hello.payload.bridge_session_id,
            )
        except AuthenticationError as error:
            await _protocol_error(
                websocket,
                "bridge",
                code="unauthorized",
                related_message_id=hello.message_id,
                detail=str(error),
                fatal=True,
            )
            return
        binding = await transport_manager.bind_bridge(
            websocket,
            principal_id=principal_id,
            creator_account_id=account_id,
            bridge_session_id=hello.payload.bridge_session_id,
        )
        await transport_manager.send_bridge(
            websocket,
            "bridge.session",
            {
                "connection_id": str(binding.connection_id),
                "bridge_session_id": str(binding.bridge_session_id),
                "creator_account_id": account_id,
                "negotiated_protocol_version": "2",
                "server_version": settings.version,
            },
            correlation_id=hello.message_id,
        )
        await transport_manager.send_bridge(
            websocket, "state.snapshot", transport_manager.state_snapshot_payload(account_id)
        )
        await transport_manager.send_bridge(
            websocket, "presence.state", transport_manager.presence_state_payload(account_id)
        )
        await transport_manager.send_bridge(
            websocket, "agent.state", transport_manager.agent_state_payload(account_id)
        )
        await transport_manager.send_bridge(
            websocket, "system.state", transport_manager.system_state_payload(account_id)
        )
        # A fresh bind (reload / deep-link) that lands while durable projection work
        # is still pending would otherwise be stranded on the stale bind-time
        # readiness above: nothing re-drives the projection until the next commit.
        # Schedule a convergence pass so a caught-up generation re-broadcasts both
        # state.snapshot and a corrected system.state. It is a no-op when current.
        transport_manager.schedule_projection(account_id)

        while True:
            raw = await websocket.receive_text()
            try:
                message = BRIDGE_TO_BRAIN_ADAPTER.validate_json(raw)
            except ValidationError:
                code, message_id, detail = _classify_validation_error(raw, "bridge")
                fatal = code in {"unsupported_version", "wrong_role"}
                await _protocol_error(
                    websocket,
                    "bridge",
                    code=code,
                    related_message_id=message_id,
                    detail=detail,
                    fatal=fatal,
                )
                if fatal:
                    return
                continue
            if message.type == "bridge.hello":
                await _protocol_error(
                    websocket,
                    "bridge",
                    code="validation_failed",
                    related_message_id=message.message_id,
                    detail="bridge.hello is only valid as the first frame",
                    fatal=True,
                )
                return
            if not _identity_matches_bridge(message, binding):
                await _protocol_error(
                    websocket,
                    "bridge",
                    code="identity_conflict",
                    related_message_id=message.message_id,
                    detail="state.resync identity conflicts with the immutable socket binding",
                    fatal=True,
                )
                return
            # Projection activation and resynchronization use bounded v2 snapshots.
            await transport_manager.send_bridge(
                websocket,
                "state.snapshot",
                transport_manager.state_snapshot_payload(binding.creator_account_id),
                correlation_id=message.message_id,
            )
    except WebSocketDisconnect:
        pass
    finally:
        if binding is not None:
            await transport_manager.disconnect_bridge(binding.connection_id)


@router.websocket("/ws/agent", name="agentWebSocket")
async def agent_websocket(websocket: WebSocket) -> None:
    await _agent_socket(websocket)


@router.websocket("/ws/bridge", name="bridgeWebSocket")
async def bridge_websocket(websocket: WebSocket) -> None:
    await _bridge_socket(websocket)


@router.get("/api/v1/agent/config", response_model=AgentConfigDocumentResponse)
async def get_agent_config(
    request: Request,
    response: Response,
    protocol_version: str = Query("2"),
    agent_installation_id: UUID = Query(...),
    creator_account_id: str = Query(...),
    current_etag: str | None = Query(None),
    current_config_revision: str | None = Query(None),
    supported_config_schema_versions: list[str] = Query(["2"]),
    authorization: str | None = Header(None, alias="Authorization"),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
) -> AgentConfigDocumentResponse | Response:
    """Return the authenticated immutable configuration required for this Agent."""
    if "auth_ticket" in request.query_params:
        raise HTTPException(status_code=400, detail="Authentication ticket must not appear in the URL")
    if authorization is None:
        raise HTTPException(status_code=401, detail="Authentication ticket is required")
    scheme, separator, auth_ticket = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not auth_ticket:
        raise HTTPException(status_code=401, detail="Bearer authentication ticket is required")
    try:
        request_model = AgentConfigGetRequest.model_validate(
            {
                "operation": "agent.config.get",
                "protocol_version": protocol_version,
                "auth_ticket": auth_ticket,
                "agent_installation_id": agent_installation_id,
                "creator_account_id": creator_account_id,
                "current_etag": current_etag,
                "current_config_revision": current_config_revision,
                "supported_config_schema_versions": supported_config_schema_versions,
            }
        )
    except ValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        transport_manager.authenticate_agent_config(
            request_model.auth_ticket,
            request_model.creator_account_id,
            request_model.agent_installation_id,
        )
    except AuthenticationError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error

    document = transport_manager.required_config_document(
        request_model.creator_account_id
    )
    validators = [current_etag]
    if if_none_match:
        validators.extend(part.strip() for part in if_none_match.split(","))
    matched = any(
        candidate is not None
        and candidate.removeprefix("W/").strip('"') == document.etag
        for candidate in validators
    )
    headers = {
        "ETag": document.etag,
        "Cache-Control": "private, no-cache",
    }
    if matched:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return document


async def signal_config_available(account_id: str) -> bool:
    """Repeat the current required revision to the active Agent lease."""
    return await transport_manager.signal_config_available(account_id)

async def deliver_command(
    account_id: str,
    *,
    conversation_id: str,
    text: str,
    deadline: datetime,
    command_id: UUID | None = None,
) -> UUID | None:
    """Validate and deliver a command envelope to the fenced Agent."""
    lease = transport_manager.active_agents.get(account_id)
    if lease is None or lease.status == "disconnected":
        return None
    resolved_command_id = command_id or uuid4()
    await transport_manager.send_agent(
        lease.websocket,
        "command.execute",
        {
            "connection_id": str(lease.connection_id),
            "fencing_token": lease.fencing_token,
            "creator_account_id": lease.creator_account_id,
            "command_id": str(resolved_command_id),
            "deadline": deadline.isoformat(),
            "idempotency_policy": "deduplicate",
            "action": {
                "type": "message.send",
                "conversation_id": conversation_id,
                "text": text,
                "media_url": None,
            },
        },
    )
    return resolved_command_id
