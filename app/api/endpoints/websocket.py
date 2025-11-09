"""  
Unified WebSocket hub for OnlyFans Conversational Analytics.  
  
Handles connections from:  
  - Browser extension (/ws/extension)  
  - Frontend React dashboard (/ws/frontend)  
  - Chatwoot or other integrations (/ws/chatwoot)  
  
Uses stateless Redis Pub/Sub via broadcaster.Broadcast.  
"""  
  
from fastapi import APIRouter, WebSocket  
from pydantic import TypeAdapter, ValidationError  
from anyio import create_task_group  
  
from app.models.wss import (  
    IncomingWssMessage,  
    OutgoingWssMessage,  
    ConnectionAckMsg,  
    SystemErrorMsg,  
)  
from app.services.data_ingest import DataIngestService  
from app.core.broadcast import broadcast  
from app.core.config import settings  
from app.utils.logger import logger  
  
router = APIRouter()  
  
# Pre-create adapters for speed  
incoming_adapter = TypeAdapter(IncomingWssMessage)  
outgoing_adapter = TypeAdapter(OutgoingWssMessage)  
  
# Single ingestion service instance (stateless except per-user caches)  
ingest_service = DataIngestService()  
  
  
async def sender(websocket: WebSocket, channel: str) -> None:  
    """  
    Receives messages from the Redis broadcast channel and sends them to the client.  
    """  
    async with broadcast.subscribe(channel) as subscriber:  
        async for event in subscriber:  
            try:  
                # Validate directly from JSON string  
                msg = outgoing_adapter.validate_json(event.message)  
                await websocket.send_text(msg.model_dump_json())  
            except ValidationError as ve:  
                logger.error(f"[WS] Outgoing message validation error: {ve}")  
            except Exception as e:  
                logger.error(f"[WS] Failed to send outgoing message: {e}")  
                break  
  
  
async def receiver(websocket: WebSocket, client_type: str, user_id: str) -> None:  
    """  
    Receives messages from the connected client and routes them appropriately.  
    """  
    while True:  
        try:  
            raw_data = await websocket.receive_text()  
        except Exception as e:  
            logger.warning(f"[WS:{client_type}] Receive error, closing connection: {e}")  
            break  
  
        try:  
            # Validate directly from JSON string  
            msg = incoming_adapter.validate_json(raw_data)  
        except ValidationError as ve:  
            logger.error(f"[WS:{client_type}] Incoming message validation error: {ve}")  
            error_msg = SystemErrorMsg(  
                type="system_error",  
                payload={"code": "validation_error", "message": str(ve)},  
            )  
            await websocket.send_text(error_msg.model_dump_json())  
            continue  
  
        if msg.type == "keepalive":  
            # Ignore keepalive payloads per spec  
            continue  
  
        # Route based on client_type and message type  
        if client_type == "extension":  
            payload_body = msg.payload  
            if payload_body is None:  
                logger.error(f"[WS:{client_type}] Missing 'payload' in incoming message: {raw_data}")  
                continue  
  
            if msg.type == "cache_update":  
                await ingest_service.handle_snapshot(user_id, payload_body)  
            elif msg.type == "new_raw_message":  
                await ingest_service.handle_delta(user_id, payload_body)  
            else:  
                logger.warning(f"[WS:{client_type}] Unhandled incoming type: {msg.type}")  
        else:  
            # For other client types, just publish the raw JSON message  
            target_channel = f"{client_type}_user_{user_id}"  
            await broadcast.publish(channel=target_channel, message=raw_data)  
  
  
@router.websocket("/ws/{client_type}/{user_id}")  
async def websocket_endpoint(websocket: WebSocket, client_type: str, user_id: str) -> None:  
    """  
    WebSocket endpoint for:  
      - /ws/extension/{user_id}  → Browser extension  
      - /ws/frontend/{user_id}   → React dashboard  
      - /ws/chatwoot/{user_id}   → Chatwoot integration  
    """  
    await websocket.accept()  
    logger.info(f"[WS] {client_type} connected for user {user_id}")  
  
    # Send connection_ack using proper model with required version field  
    ack_msg = ConnectionAckMsg(  
        type="connection_ack",  
        payload={  
            "client_type": client_type,  
            "user_id": user_id,  
            "version": settings.version,  # spec requires system version  
        },  
    )  
    await websocket.send_text(ack_msg.model_dump_json())  
  
    channel = f"{client_type}_user_{user_id}"  
  
    try:  
        async with create_task_group() as tg:  
            tg.start_soon(sender, websocket, channel)  
            tg.start_soon(receiver, websocket, client_type, user_id)  
    except Exception as e:  
        logger.error(f"[WS] Connection error for {client_type}/{user_id}: {e}")  
    finally:  
        logger.info(f"[WS] {client_type} disconnected for user {user_id}")  