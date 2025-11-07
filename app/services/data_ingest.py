"""  
Data ingestion service.  
  
Bridges browser extension IndexedDB → FastAPI → internal Pydantic models.  
Stores most recent extension/API dump in memory so OnlyFansClient and routes can access it.  
"""  
  
from typing import List, Dict, Any, Optional  
from app.models.core import ChatThread, Message  
from app.utils.logger import logger  
from app.utils import normalization  
  
  
class DataIngestService:  
    """  
    Manages ingestion of chat/message data from the browser extension or OnlyFans API.  
  
    Responsibilities:  
    - Store latest chats/messages in an in-memory cache.  
    - Convert raw dict payloads into validated Pydantic models.  
    - Provide raw dicts for OnlyFansClient and typed models for routes.  
    """  
  
    def __init__(self) -> None:  
        """Initialize the in-memory cache."""  
        self._cache: Dict[str, List[Dict[str, Any]]] = {  
            "chats": [],  
            "messages": []  
        }  
  
    # ----------------------------------------------------------------------  
    # Update / Parse  
    # ----------------------------------------------------------------------  
  
    def update_cache(  
        self,  
        chats: Optional[List[Dict[str, Any]]],  
        messages: Optional[List[Dict[str, Any]]]  
    ) -> None:  
        """Store raw dicts in memory."""  
        self._cache["chats"] = chats or []  
        self._cache["messages"] = messages or []  
        logger.info(  
            f"[DATA_INGEST] Cache updated: "  
            f"{len(self._cache['chats'])} chats, "  
            f"{len(self._cache['messages'])} messages"  
        )  
  
    async def parse_chats(self, raw_chats: List[Dict[str, Any]]) -> List[ChatThread]:  
        """Convert raw chat dicts into validated ChatThread models."""  
        threads: List[ChatThread] = []  
        for c in raw_chats:  
            try:  
                normalized = normalization.normalize_chat_payload(c)  
                thread = ChatThread.model_validate(normalized)  
                if thread.messages is None:  
                    thread.messages = []  
                threads.append(thread)  
            except Exception as e:  
                logger.warning(f"[DATA_INGEST] Skipping invalid chat record: {e}")  
        return threads  
  
    async def parse_messages(self, raw_messages: List[Dict[str, Any]]) -> List[Message]:  
        """Convert raw message dicts into validated Message models."""  
        msgs: List[Message] = []  
        for m in raw_messages:  
            try:  
                normalized = normalization.normalize_message_payload(m)  
                msg = Message.model_validate(normalized)  
                msgs.append(msg)  
            except Exception as e:  
                logger.warning(f"[DATA_INGEST] Skipping invalid message record: {e}")  
        return msgs  
  
    # ----------------------------------------------------------------------  
    # Retrieval for OnlyFansClient (raw dicts)  
    # ----------------------------------------------------------------------  
  
    async def get_all_chats_from_db(self, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:  
        """Get chats from cache, or demo fallback if empty."""  
        if self._cache["chats"]:  
            return self._cache["chats"][offset:offset + limit]  
  
        # Fallback demo data  
        return [{  
            "id": "chat1",  
            "participants": ["fan123", "creator456"],  
            "withUser": {"name": "Demo Fan", "username": "demo_fan"},  
            "last_message": {  
                "id": "msg1",  
                "chat_id": "chat1",  
                "sender_id": "fan123",  
                "text": "Hey there!",  
                "created_at": "2024-06-01T12:00:00"  
            },  
            "unread_count": 0  
        }][offset:offset + limit]  
  
    async def get_all_messages_from_db(  
        self,  
        chat_id: str,  
        limit: int = 20,  
        offset: int = 0  
    ) -> List[Dict[str, Any]]:  
        """Get messages for a chat from cache, or demo fallback if empty."""  
        if self._cache["messages"]:  
            filtered = [  
                m for m in self._cache["messages"]  
                if str(m.get("chat_id")) == str(chat_id)  
            ]  
            return filtered[offset:offset + limit]  
  
        # Fallback demo data  
        return [  
            {  
                "id": "msg1",  
                "chat_id": chat_id,  
                "sender_id": "fan123",  
                "text": "Hey there!",  
                "created_at": "2024-06-01T12:00:00"  
            },  
            {  
                "id": "msg2",  
                "chat_id": chat_id,  
                "sender_id": "creator456",  
                "text": "Hi, how are you?",  
                "created_at": "2024-06-01T12:01:00"  
            }  
        ][offset:offset + limit]  
  
    # ----------------------------------------------------------------------  
    # Typed getters for routes & WS  
    # ----------------------------------------------------------------------  
  
    def get_cached_chats(self) -> List[ChatThread]:  
        """Return the cached chats as validated ChatThread models."""  
        chats_raw = self._cache.get("chats", [])  
        chats: List[ChatThread] = []  
        for c in chats_raw:  
            try:  
                normalized = normalization.normalize_chat_payload(c)  
                thread = ChatThread.model_validate(normalized)  
                if thread.messages is None:  
                    thread.messages = []  
                chats.append(thread)  
            except Exception as e:  
                logger.warning(f"[DATA_INGEST] Skipping invalid cached chat: {e}")  
        return chats  
  
    def get_cached_messages(self) -> List[Message]:  
        """Return the cached messages as validated Message models."""  
        msgs_raw = self._cache.get("messages", [])  
        messages: List[Message] = []  
        for m in msgs_raw:  
            try:  
                normalized = normalization.normalize_message_payload(m)  
                msg = Message.model_validate(normalized)  
                messages.append(msg)  
            except Exception as e:  
                logger.warning(f"[DATA_INGEST] Skipping invalid cached message: {e}")  
        return messages  