"""  
WebSocket hub for OnlyFans Conversational Analytics.  
  
Handles real-time connections from:  
- Browser extension (/ws/extension)  
- Frontend React dashboard (/ws/frontend)  
- Chatwoot or other integrations (/ws/chatwoot)  
  
Provides bidirectional messaging between these clients and  
routes messages based on client type and payload type.  
  
WS updates now mirror `/from-extension`:  
- Parse raw payloads into Pydantic models  
- Run enrichment + graph build  
- Update backend cache  
- Broadcast typed SyncResponse to frontend/chatwoot clients  
"""  
  
import json  
from typing import Dict, Any, List, Optional  
  
from fastapi import APIRouter, WebSocket, WebSocketDisconnect  
from fastapi.encoders import jsonable_encoder  
  
from app.models.core import SyncResponse  
from app.services.data_ingest import DataIngestService  
from app.services.enrichment import EnrichmentService  
from app.services.graph_builder import GraphBuilder  
from app.utils.logger import logger  
  
router = APIRouter()  
  
# Shared DataIngestService instance  
data_ingest_service = DataIngestService()  
  
# Track connected clients: { WebSocket: {"type": "extension"|"frontend"|"chatwoot"} }  
_ws_clients: Dict[WebSocket, Dict[str, Any]] = {}  
  
# ----------------------------------------------------------------------  
# Broadcast helper  
# ----------------------------------------------------------------------  
async def _broadcast(message: Dict[str, Any], target_types: Optional[List[str]] = None) -> None:  
    """Send a JSON message to all connected WS clients, optionally filtered by client type."""  
    dead_clients: List[WebSocket] = []  
    for ws, info in list(_ws_clients.items()):  
        if target_types and info["type"] not in target_types:  
            continue  
        try:  
            safe_message = jsonable_encoder(message)  
            await ws.send_text(json.dumps(safe_message))  
        except Exception as e:  
            logger.error(f"[WS] Failed to send to {info['type']}: {e}")  
            dead_clients.append(ws)  
  
    for ws in dead_clients:  
        info = _ws_clients.pop(ws, None)  
        logger.warning(f"[WS] Removed dead client {info.get('type') if info else 'unknown'}")  
  
  
# ----------------------------------------------------------------------  
# Message handler  
# ----------------------------------------------------------------------  
async def _handle_ws_message(websocket: WebSocket, client_type: str, payload: Dict[str, Any]) -> None:  
    msg_type = payload.get("type")  
  
    # === EXTENSION or FRONTEND → BACKEND (upstream data) ===  
    if msg_type == "cache_update" and client_type in ("extension", "frontend"):  
        chats_raw = payload.get("chats", [])  
        messages_raw = payload.get("messages", [])  
  
        logger.info(f"[WS:{client_type}] cache_update: chats={len(chats_raw)}, messages={len(messages_raw)}")  
  
        # 1. Parse raw payloads into typed models  
        chats = await data_ingest_service.parse_chats(chats_raw)  
        messages = await data_ingest_service.parse_messages(messages_raw)  
  
        # 2. Build lookup for chats with messages in this update  
        messages_by_chat = {}  
        for m in messages:  
            messages_by_chat.setdefault(str(m.chat_id), []).append(m)  
  
        # 3. Run enrichment + graph build only for chats with messages in this batch  
        enrichment_service = EnrichmentService()  
        graph_builder = GraphBuilder(creator_id="creator_demo")  # TODO: real creator_id  
        for chat in chats:  
            if str(chat.id) not in messages_by_chat:  
                continue  # Skip chats with no messages in this update  
            enriched_data = enrichment_service.enrich_conversation(chat)  
            fan_id = getattr(chat.withUser, "id", None) or "fan_unknown"  
            graph_builder.build_graph(enriched_data, fan_id=fan_id)  
  
        # 4. Update backend raw cache  
        data_ingest_service.update_cache(chats_raw, messages_raw)  
        logger.info(f"[WS] Cache updated from {client_type}: {len(chats_raw)} chats, {len(messages_raw)} messages")  
  
        # 5. Broadcast typed cache contents to frontend & chatwoot  
        sync_data = SyncResponse(  
            chats=data_ingest_service.get_cached_chats(),  
            messages=data_ingest_service.get_cached_messages()  
        )  
  
        # Attach messages directly to chats so frontend doesn't need extra request  
        messages_by_chat_all = {}  
        for m in sync_data.messages:  
            messages_by_chat_all.setdefault(str(m.chat_id), []).append(m)  
  
        for chat in sync_data.chats:  
            chat.messages = messages_by_chat_all.get(str(chat.id), [])  
  
        update_payload = sync_data.model_dump()  
        update_payload["type"] = "cache_update"  
  
        await _broadcast(  
            update_payload,  
            target_types=["frontend", "chatwoot"] if client_type == "extension" else ["chatwoot"]  
        )  
  
    # === FRONTEND → BACKEND: request messages for a given chat ===  
    elif client_type == "frontend" and msg_type == "get_messages_for_chat":  
        chat_id = payload.get("payload", {}).get("chat_id")  
        if not chat_id:  
            logger.warning(f"[WS:{client_type}] get_messages_for_chat missing chat_id")  
            return  
  
        logger.info(f"[WS:{client_type}] Sending messages for chat {chat_id}")  
  
        all_messages = data_ingest_service.get_cached_messages()  
        filtered_messages = [  
            m.model_dump()  
            for m in all_messages  
            if str(m.chat_id) == str(chat_id)  
        ]  
  
        response_payload = {  
            "type": "messages_for_chat",  
            "payload": {  
                "chat_id": chat_id,  
                "messages": filtered_messages  
            }  
        }  
  
        try:  
            await websocket.send_text(json.dumps(response_payload, default=str))  
        except Exception as e:  
            logger.error(f"[WS:{client_type}] Failed to send messages_for_chat: {e}", exc_info=True)  
  
    # === FRONTEND → BACKEND → EXTENSION (downstream commands) ===  
    elif client_type == "frontend" and msg_type == "send_command":  
        logger.debug(f"[WS] Frontend command: {payload}")  
        await _broadcast(payload, target_types=["extension"])  
  
    # === CHATWOOT → BACKEND → EXTENSION (downstream commands) ===  
    elif client_type == "chatwoot" and msg_type == "send_command":  
        logger.debug(f"[WS:chatwoot] Command: {payload}")  
        await _broadcast(payload, target_types=["extension"])  
  
    # === CHATWOOT → BACKEND (other messages) ===  
    elif client_type == "chatwoot":  
        logger.debug(f"[WS:chatwoot] Message: {payload}")  
  
    else:  
        logger.warning(f"[WS:{client_type}] Unhandled message type: {msg_type}")  
  
  
# ----------------------------------------------------------------------  
# WebSocket endpoint  
# ----------------------------------------------------------------------  
@router.websocket("/ws/{client_type}")  
async def websocket_endpoint(websocket: WebSocket, client_type: str) -> None:  
    """WebSocket endpoint for:  
    - /ws/extension  → Browser extension  
    - /ws/frontend   → React dashboard  
    - /ws/chatwoot   → Chatwoot integration  
    """  
    try:  
        await websocket.accept()  
    except Exception as e:  
        logger.exception(f"[WS:{client_type}] Failed to accept connection: {e}")  
        return  
  
    _ws_clients[websocket] = {"type": client_type}  
    logger.info(f"[WS] {client_type} connected")  
  
    try:  
        while True:  
            raw_data = await websocket.receive_text()  
            try:  
                payload = json.loads(raw_data)  
            except json.JSONDecodeError:  
                logger.warning(f"[WS:{client_type}] Received non-JSON message: {raw_data}")  
                continue  
  
            try:  
                await _handle_ws_message(websocket, client_type, payload)  
            except Exception as e:  
                logger.error(f"[WS:{client_type}] Error handling message {payload}: {e}", exc_info=True)  
  
    except WebSocketDisconnect:  
        logger.info(f"[WS] {client_type} disconnected")  
    finally:  
        _ws_clients.pop(websocket, None)  
        logger.debug(f"[WS] Removed client {client_type} from active connections")  