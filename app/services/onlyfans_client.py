"""  
OnlyFansClient:  
Client for retrieving chats and messages from a creator-owned OnlyFans account.  
  
Sources:  
- DataIngestService in-memory cache (populated via extension ingestion flow)  
- Direct API (legacy/testing, not implemented here)  
"""  
  
from typing import List, Union  
from app.models.core import ChatThread, Message  
from app.services.data_ingest import DataIngestService  
from app.utils.logger import logger, log_json  
  
  
class OnlyFansClient:  
    """Facade for retrieving OnlyFans chat data from ingestion cache or direct API."""  
  
    def __init__(self, auth_cookie: str = None, use_extension: bool = True):  
        self.auth_cookie = auth_cookie  
        self.use_extension = use_extension  
        self.data_ingest = DataIngestService()  
  
    async def get_chats(self, user_id: str, limit: int = 20, offset: int = 0) -> List[ChatThread]:  
        """  
        Get chats for a given user_id from ingestion cache.  
        """  
        logger.info(f"Fetching chats for user {user_id}: limit={limit}, offset={offset}")  
        chats = self.data_ingest.get_cached_chats(user_id)  
        if limit or offset:  
            chats = chats[offset:offset+limit]  
        log_json([c.model_dump() for c in chats], f"get_chats_user_{user_id}")  
        return chats  
  
    async def get_messages(self, user_id: str, chat_id: Union[int, str], limit: int = 20, offset: int = 0) -> List[Message]:  
        """  
        Get messages for a specific chat from ingestion cache.  
        """  
        logger.info(f"Fetching messages for user {user_id}, chat_id={chat_id}, limit={limit}, offset={offset}")  
        messages = [  
            m for m in self.data_ingest.get_cached_messages(user_id)  
            if str(m.chat_id) == str(chat_id)  
        ]  
        if limit or offset:  
            messages = messages[offset:offset+limit]  
        log_json([m.model_dump() for m in messages], f"get_messages_user_{user_id}_chat_{chat_id}")  
        return messages  
  
    async def get_chat_with_messages(self, user_id: str, chat_id: Union[int, str], message_limit: int = 20) -> ChatThread:  
        """  
        Get a chat with its messages from ingestion cache.  
        """  
        logger.info(f"Fetching chat with messages for user {user_id}, chat_id={chat_id}")  
        chats = await self.get_chats(user_id, limit=100)  
        chat = next((c for c in chats if str(c.id) == str(chat_id)), None)  
        if not chat:  
            logger.warning(f"Chat {chat_id} not found for user {user_id}")  
            raise ValueError(f"Chat {chat_id} not found.")  
        chat.messages = await self.get_messages(user_id, chat_id, limit=message_limit)  
        log_json(chat.model_dump(), f"get_chat_with_messages_user_{user_id}_chat_{chat_id}")  
        return chat  