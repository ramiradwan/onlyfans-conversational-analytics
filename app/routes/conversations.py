"""  
Conversation routes for OnlyFans Conversational Analytics.  
  
Provides:  
- Fetching chats/messages from OnlyFans API or cache.  
- Fetching a full chat thread with messages.  
- Accepting raw IndexedDB dumps from the browser extension and converting them to typed models.  
- Updating backend's in-memory cache via REST POST.  
- Returning typed cached data for frontend sync.  
"""  
  
from typing import List, Union, Dict, Any  
from fastapi import APIRouter, HTTPException, Body  
  
from app.models.core import ChatThread, Message, SyncResponse  
from app.models.auth import AuthData  
from app.services.onlyfans_client import OnlyFansClient  
from app.services.data_ingest import DataIngestService  
from app.services.enrichment import EnrichmentService  
from app.services.graph_builder import GraphBuilder  
from app.utils.logger import logger  
  
router = APIRouter()  
data_ingest_service = DataIngestService()  
  
@router.post("/chats", response_model=List[ChatThread])  
async def fetch_chats(auth: AuthData, limit: int = 20, offset: int = 0):  
    """  
    Fetch chat threads from OnlyFans API or cache.  
  
    Args:  
        auth (AuthData): Authentication payload containing session cookies.  
        limit (int): Max number of chats to return.  
        offset (int): Pagination offset.  
  
    Returns:  
        List[ChatThread]: Validated chat thread models.  
    """  
    try:  
        logger.info(f"[ROUTE] fetch_chats limit={limit}, offset={offset}")  
        client = OnlyFansClient(auth.auth_cookie, use_extension=True)  
        chats = await client.get_chats(limit=limit, offset=offset)  
        return chats  
    except Exception as e:  
        logger.exception("Error fetching chats")  
        raise HTTPException(status_code=400, detail=str(e))  
  
@router.post("/chats/{chat_id}/messages", response_model=List[Message])  
async def fetch_messages(  
    chat_id: Union[int, str],  
    auth: AuthData,  
    limit: int = 20,  
    offset: int = 0  
):  
    """  
    Fetch messages for a specific chat thread.  
  
    Args:  
        chat_id (str|int): The chat ID to fetch messages from.  
        auth (AuthData): Authentication payload containing session cookies.  
        limit (int): Max number of messages to return.  
        offset (int): Pagination offset.  
  
    Returns:  
        List[Message]: Validated message models.  
    """  
    try:  
        logger.info(f"[ROUTE] fetch_messages chat_id={chat_id}, limit={limit}, offset={offset}")  
        client = OnlyFansClient(auth.auth_cookie, use_extension=True)  
        messages = await client.get_messages(chat_id=chat_id, limit=limit, offset=offset)  
        return messages  
    except Exception as e:  
        logger.exception("Error fetching messages")  
        raise HTTPException(status_code=400, detail=str(e))  
  
@router.post("/chats/{chat_id}/full", response_model=ChatThread)  
async def fetch_chat_with_messages(  
    chat_id: Union[int, str],  
    auth: AuthData,  
    message_limit: int = 20  
):  
    """  
    Fetch a chat thread along with its messages.  
  
    Args:  
        chat_id (str|int): The chat ID to fetch.  
        auth (AuthData): Authentication payload containing session cookies.  
        message_limit (int): Max number of messages to include.  
  
    Returns:  
        ChatThread: Validated chat thread model with messages.  
    """  
    try:  
        logger.info(f"[ROUTE] fetch_chat_with_messages chat_id={chat_id}, message_limit={message_limit}")  
        client = OnlyFansClient(auth.auth_cookie, use_extension=True)  
        chat = await client.get_chat_with_messages(chat_id=chat_id, message_limit=message_limit)  
        return chat  
    except Exception as e:  
        logger.exception("Error fetching chat with messages")  
        raise HTTPException(status_code=400, detail=str(e))  
  
@router.post("/from-extension", response_model=SyncResponse)  
async def ingest_from_extension(payload: Dict[str, Any] = Body(...)) -> SyncResponse:  
    """  
    Accept a raw IndexedDB dump from the browser extension and convert to typed models.  
  
    Expected payload format:  
    {  
        "chats": [...],  
        "messages": [...]  
    }  
  
    Also runs enrichment + graph build for each conversation.  
  
    Returns:  
        SyncResponse: Validated chats and messages from payload.  
    """  
    try:  
        logger.info("[ROUTE] ingest_from_extension received payload")  
  
        chats = await data_ingest_service.parse_chats(payload.get("chats", []))  
        messages = await data_ingest_service.parse_messages(payload.get("messages", []))  
  
        # Ensure messages list exists for each chat  
        for c in chats:  
            if c.messages is None:  
                c.messages = []  
  
        # Attach messages to chats  
        chat_map = {c.id: c for c in chats}  
        for msg in messages:  
            if msg.chat_id in chat_map:  
                chat_map[msg.chat_id].messages.append(msg)  
  
        logger.debug(f"[ROUTE] Parsed {len(chats)} chats and {len(messages)} messages from extension payload")  
  
        # === Enrich + Build Graph ===  
        enrichment_service = EnrichmentService()  
        graph_builder = GraphBuilder(creator_id="creator_demo")  # TODO: pass real creator_id  
  
        for chat in chats:  
            enriched_data = enrichment_service.enrich_conversation(chat)  
            fan_id = getattr(chat.withUser, "id", None) or "fan_unknown"  
            graph_data = graph_builder.build_graph(enriched_data, fan_id=fan_id)  
            logger.info(  
                f"[ROUTE] Graph built for chat {chat.id}: "  
                f"{len(graph_data['vertices'])} vertices, {len(graph_data['edges'])} edges"  
            )  
  
        # ✅ Update cache and return typed cache contents  
        data_ingest_service.update_cache(payload.get("chats", []), payload.get("messages", []))  
        return SyncResponse(  
            chats=data_ingest_service.get_cached_chats(),  
            messages=data_ingest_service.get_cached_messages()  
        )  
  
    except Exception as e:  
        logger.exception("Error ingesting from extension")  
        raise HTTPException(status_code=400, detail=str(e))  
  
@router.post("/extension-cache")  
async def update_cache(payload: Dict[str, Any]):  
    """  
    Update backend's cache with raw dict data from the frontend or WS.  
  
    Args:  
        payload (dict): Contains 'chats' and 'messages' lists.  
  
    Returns:  
        dict: Status and counts of updated records.  
    """  
    try:  
        chats = payload.get("chats", [])  
        messages = payload.get("messages", [])  
        data_ingest_service.update_cache(chats, messages)  
        logger.info(f"[ROUTE] extension-cache updated: {len(chats)} chats, {len(messages)} messages")  
        return {"status": "ok", "chats": len(chats), "messages": len(messages)}  
    except Exception as e:  
        logger.exception("Error updating extension cache")  
        raise HTTPException(status_code=400, detail=str(e))  
  
@router.get("/sync", response_model=SyncResponse)  
async def sync_from_cache() -> SyncResponse:  
    """  
    Return the current chats/messages from the cache as validated Pydantic models.  
  
    Allows frontend to fetch initial data before WS updates.  
  
    Returns:  
        SyncResponse: Cached chats and messages.  
    """  
    try:  
        chats: List[ChatThread] = data_ingest_service.get_cached_chats()  
        messages: List[Message] = data_ingest_service.get_cached_messages()  
        logger.info(f"[ROUTE] sync_from_cache: {len(chats)} chats, {len(messages)} messages")  
        return SyncResponse(chats=chats, messages=messages)  
    except Exception as e:  
        logger.exception("Error syncing from cache")  
        raise HTTPException(status_code=400, detail=str(e))  