"""  
Unified WebSocket hub for OnlyFans Conversational Analytics.  
  
Handles connections from:  
    - Browser extension (/ws/extension) → Agent  
    - Frontend React dashboard (/ws/frontend) → Bridge  
    - Chatwoot or other integrations (/ws/chatwoot)  
  
Implements:  
    - Stateless Redis Pub/Sub via encode/broadcaster  
    - Pydantic Discriminated Unions for type-safe message parsing  
    - Snapshot-then-delta ingestion via DataIngestService  
    - MV3 keepalive acceptance  
    - creator_id propagation for multi-creator environments  
"""  
  
from fastapi import APIRouter, WebSocket  
from pydantic import TypeAdapter, ValidationError  
from anyio import create_task_group, CancelScope  
import redis  
  
from app.models.wss import (  
    IncomingWssMessage,  
    OutgoingWssMessage,  
    ConnectionAckMsg,  
    SystemStatusMsg,  
    IncomingOnlineUsersUpdateMsg,  
    IncomingOnlineUsersUpdatePayload,  
    OutgoingOnlineUsersUpdateMsg,  
    OutgoingOnlineUsersUpdatePayload,  
)  
from app.models.core import ConnectionInfo, SystemStatus  
from app.services.data_ingest import DataIngestService  
from app.core.broadcast import broadcast  
from app.core.config import settings  
from app.utils.logger import logger  
from app.utils.ws_errors import broadcast_system_error  
  
router = APIRouter()  
  
incoming_adapter = TypeAdapter(IncomingWssMessage)  
outgoing_adapter = TypeAdapter(OutgoingWssMessage)  
  
ingest_service = DataIngestService()  
  
# Redis client for storing latest extension user_id  
r = redis.Redis.from_url(settings.redis.url)  
  
  
async def sender(websocket: WebSocket, channel: str, scope: CancelScope) -> None:  
    """  
    Subscribes to a Redis channel and forwards messages to the connected client.  
    Terminates if websocket disconnects or fatal error occurs.  
    """  
    try:  
        async with broadcast.subscribe(channel) as subscriber:  
            async for event in subscriber:  
                if websocket.client_state.name != "CONNECTED":  
                    scope.cancel()  
                    break  
                try:  
                    msg = outgoing_adapter.validate_json(event.message)  
                    await websocket.send_text(msg.model_dump_json())  
                except ValidationError as ve:  
                    logger.error(f"[WS:{channel}] Outgoing validation error: {ve}")  
                except Exception as e:  
                    logger.exception(f"[WS:{channel}] Send failed: {e}")  
                    scope.cancel()  
                    break  
    except Exception as e:  
        logger.exception(f"[WS:{channel}] Sender fatal error: {e}")  
        scope.cancel()  
        await websocket.close(code=1011, reason="Sender fatal error")  
  
  
async def receiver(websocket: WebSocket, client_type: str, user_id: str, scope: CancelScope) -> None:  
    """  
    Receives messages from client and routes them appropriately.  
    Handles Agent ingestion events, presence updates, and generic WS forwarding.  
    """  
    while True:  
        try:  
            raw_data = await websocket.receive_text()  
        except Exception as e:  
            logger.warning(f"[WS:{client_type}/{user_id}] Receive error: {e}")  
            scope.cancel()  
            break  
  
        try:  
            msg = incoming_adapter.validate_json(raw_data)  
        except ValidationError as ve:  
            logger.error(f"[WS:{client_type}/{user_id}] Incoming validation error: {ve}")  
            await broadcast_system_error(user_id, "validation_error", str(ve))  
            continue  
        except Exception as e:  
            logger.exception(f"[WS:{client_type}/{user_id}] JSON parse error: {e}")  
            await broadcast_system_error(user_id, "json_parse_error", str(e))  
            continue  
  
        try:  
            # MV3 keepalive handling  
            if msg.type == "keepalive":  
                if client_type != "extension":  
                    logger.warning(f"[WS:{client_type}/{user_id}] Unexpected keepalive from non-extension client")  
                else:  
                    logger.debug(f"[WS:{client_type}/{user_id}] Keepalive received from extension client")  
                continue  
  
            if client_type == "extension":  
                creator_id = settings.onlyfans_creator_id or user_id  
                if settings.onlyfans_creator_id:  
                    logger.debug(f"[WS:{client_type}/{user_id}] Using creator_id from settings: {creator_id}")  
                else:  
                    logger.debug(f"[WS:{client_type}/{user_id}] No creator_id in settings — fallback to user_id")  
  
                if msg.type == "cache_update":  
                    # Immediate status for UX responsiveness  
                    status_msg = SystemStatusMsg(  
                        type="system_status",  
                        payload=SystemStatus(status="PROCESSING_SNAPSHOT")  
                    )  
                    await broadcast.publish(  
                        channel=f"frontend_user_{user_id}",  
                        message=status_msg.model_dump_json()  
                    )  
                    logger.info(f"[WS:{client_type}/{user_id}][creator={creator_id}] Snapshot received — starting ingestion")  
                    await ingest_service.handle_snapshot(user_id, msg.payload, creator_id)  
  
                elif msg.type == "new_raw_message":  
                    logger.info(f"[WS:{client_type}/{user_id}][creator={creator_id}] Delta received")  
                    await ingest_service.handle_delta(user_id, msg.payload, creator_id)  
  
                elif msg.type == "online_users_update":  
                    # Translate incoming presence to outgoing presence for Bridge  
                    out_msg = OutgoingOnlineUsersUpdateMsg(  
                        type="online_users_update",  
                        payload=OutgoingOnlineUsersUpdatePayload(  
                            user_ids=msg.payload.user_ids,  
                            timestamp=msg.payload.timestamp  
                        )  
                    )  
                    target_channel = f"frontend_user_{user_id}"  
                    await broadcast.publish(channel=target_channel, message=out_msg.model_dump_json())  
                    logger.info(f"[WS:{client_type}/{user_id}] Presence update broadcast to {target_channel}")  
  
                else:  
                    logger.warning(f"[WS:{client_type}/{user_id}] Unhandled type: {msg.type}")  
                    await broadcast_system_error(user_id, "unhandled_type", f"Unhandled type: {msg.type}")  
  
            else:  
                # Non-extension clients — forward raw message  
                target_channel = f"{client_type}_user_{user_id}"  
                await broadcast.publish(channel=target_channel, message=raw_data)  
  
        except Exception as e:  
            logger.exception(f"[WS:{client_type}/{user_id}] Receiver loop error: {e}")  
            await broadcast_system_error(user_id, "receiver_loop_error", str(e))  
            continue  
  
  
@router.websocket("/ws/{client_type}/{user_id}")  
async def websocket_endpoint(websocket: WebSocket, client_type: str, user_id: str) -> None:  
    """  
    WS endpoint for:  
        - /ws/extension/{user_id}  → Browser extension (Agent)  
        - /ws/frontend/{user_id}   → React dashboard (Bridge)  
        - /ws/chatwoot/{user_id}   → Chatwoot integration  
    """  
    await websocket.accept()  
    logger.info(f"[WS:{client_type}/{user_id}] Connected")  
  
    if client_type == "extension":  
        try:  
            r.set("latest_user_id", user_id)  
            logger.info(f"[WS:{client_type}/{user_id}] Stored latest_user_id in Redis")  
        except Exception as e:  
            logger.exception(f"[WS:{client_type}/{user_id}] Failed to store latest_user_id: {e}")  
  
    # Send connection acknowledgment  
    try:  
        ack_msg = ConnectionAckMsg(  
            type="connection_ack",  
            payload=ConnectionInfo(  
                version=settings.version,  
                clientType=client_type,  
                userId=user_id,  
                statusMessage="Connected successfully"  
            )  
        )  
        await websocket.send_text(ack_msg.model_dump_json())  
    except Exception as e:  
        logger.exception(f"[WS:{client_type}/{user_id}] Failed to send connection_ack: {e}")  
        await broadcast_system_error(user_id, "ack_failed", str(e))  
  
    channel = f"{client_type}_user_{user_id}"  
  
    try:  
        async with create_task_group() as tg:  
            with CancelScope() as scope:  
                tg.start_soon(sender, websocket, channel, scope)  
                tg.start_soon(receiver, websocket, client_type, user_id, scope)  
    except Exception as e:  
        logger.exception(f"[WS:{client_type}/{user_id}] Connection error: {e}")  
        await broadcast_system_error(user_id, "connection_error", str(e))  
    finally:  
        logger.info(f"[WS:{client_type}/{user_id}] Disconnected")  