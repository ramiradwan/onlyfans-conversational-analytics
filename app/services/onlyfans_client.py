"""  
OnlyFansClient:  
Client for fetching chats and messages from a creator-owned OnlyFans account.  
  
Sources:  
- Browser extension IndexedDB (preferred via `use_extension=True`)  
- Direct API (legacy/testing, not implemented here)  
"""  
  
from typing import List, Union  
from app.models.core import ChatThread, Message  
from app.services.data_ingest import DataIngestService  
from app.utils.logger import logger, log_json  
  
  
class OnlyFansClient:  
    """Facade for retrieving OnlyFans chat data from either the extension cache or direct API."""  
  
    def __init__(self, auth_cookie: str = None, use_extension: bool = True):  
        self.auth_cookie = auth_cookie  
        self.use_extension = use_extension  
        self.data_ingest = DataIngestService()  
  
    async def get_chats(self, limit: int = 20, offset: int = 0) -> List[ChatThread]:  
        logger.info(f"Fetching chats: limit={limit}, offset={offset}")  
        if self.use_extension:  
            raw_chats = await self.data_ingest.get_all_chats_from_db(limit=limit, offset=offset)  
            chats = await self.data_ingest.parse_chats(raw_chats)  
        else:  
            chats = []  
        log_json([c.model_dump() for c in chats], "get_chats")  
        return chats  
  
    async def get_messages(self, chat_id: Union[int, str], limit: int = 20, offset: int = 0) -> List[Message]:  
        logger.info(f"Fetching messages for chat_id={chat_id}, limit={limit}, offset={offset}")  
        if self.use_extension:  
            raw_messages = await self.data_ingest.get_all_messages_from_db(chat_id, limit=limit, offset=offset)  
            messages = await self.data_ingest.parse_messages(raw_messages)  
        else:  
            messages = []  
        log_json([m.model_dump() for m in messages], f"get_messages_chat_{chat_id}")  
        return messages  
  
    async def get_chat_with_messages(self, chat_id: Union[int, str], message_limit: int = 20) -> ChatThread:  
        logger.info(f"Fetching chat with messages: chat_id={chat_id}")  
        chats = await self.get_chats(limit=100)  
        chat = next((c for c in chats if str(c.id) == str(chat_id)), None)  
        if not chat:  
            logger.warning(f"Chat {chat_id} not found")  
            raise ValueError(f"Chat {chat_id} not found.")  
        chat.messages = await self.get_messages(chat_id, limit=message_limit)  
        log_json(chat.model_dump(), f"get_chat_with_messages_{chat_id}")  
        return chat  