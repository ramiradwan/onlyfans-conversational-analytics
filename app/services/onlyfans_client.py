"""  
OnlyFansClient:  
Client for retrieving chats and messages from a creator-owned OnlyFans account.  
  
Sources:  
- DataIngestService in-memory cache (populated via extension ingestion flow)  

This legacy facade never accepts or performs direct platform authentication.
"""  
  
from typing import List, Union  
from app.models.core import ChatThread, Message  
from app.services.data_ingest import DataIngestService  
  
  
class OnlyFansClient:  
    """Legacy facade for retrieving chat data from the local ingestion cache."""
  
    def __init__(self):
        self.data_ingest = DataIngestService()  
  
    async def get_chats(self, user_id: str, limit: int = 20, offset: int = 0) -> List[ChatThread]:  
        """  
        Get chats for a given user_id from ingestion cache.  
        """  
        chats = self.data_ingest.get_cached_chats(user_id)  
        if limit or offset:  
            chats = chats[offset:offset+limit]  
        return chats  
  
    async def get_messages(self, user_id: str, chat_id: Union[int, str], limit: int = 20, offset: int = 0) -> List[Message]:  
        """  
        Get messages for a specific chat from ingestion cache.  
        """  
        messages = [  
            m for m in self.data_ingest.get_cached_messages(user_id)  
            if str(m.chat_id) == str(chat_id)  
        ]  
        if limit or offset:  
            messages = messages[offset:offset+limit]  
        return messages  
  
    async def get_chat_with_messages(self, user_id: str, chat_id: Union[int, str], message_limit: int = 20) -> ChatThread:  
        """  
        Get a chat with its messages from ingestion cache.  
        """  
        chats = await self.get_chats(user_id, limit=100)  
        chat = next((c for c in chats if str(c.id) == str(chat_id)), None)  
        if not chat:  
            raise ValueError("Requested chat was not found in the bound account cache")
        chat.messages = await self.get_messages(user_id, chat_id, limit=message_limit)  
        return chat
