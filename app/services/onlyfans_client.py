# HTTP client for OnlyFans API

import httpx  
from typing import List, Union  
  
from app.models.core import ChatThread, Message  
from app.utils.logger import log_json  
  
  
class OnlyFansClient:  
    """  
    Client for fetching chats and messages from a creator-owned OnlyFans account.  
    """  
  
    def __init__(self, auth_cookie: str):  
        self.base_url = "https://onlyfans.com/api2/v2"  
        self.headers = {  
            "user-agent": "Mozilla/5.0",  
            "accept": "application/json, text/plain, */*",  
            "cookie": auth_cookie  
        }  
  
    async def get_chats(self, limit: int = 20, offset: int = 0) -> List[ChatThread]:  
        url = f"{self.base_url}/chats?limit={limit}&offset={offset}"  
        async with httpx.AsyncClient(headers=self.headers) as client:  
            resp = await client.get(url)  
            resp.raise_for_status()  
            payload = resp.json()  
  
        log_json(payload, "get_chats")  
        return [ChatThread.model_validate(chat_obj) for chat_obj in payload.get("data", [])]  
  
    async def get_messages(self, chat_id: Union[int, str], limit: int = 20, offset: int = 0) -> List[Message]:  
        url = f"{self.base_url}/chats/{chat_id}/messages?limit={limit}&offset={offset}"  
        async with httpx.AsyncClient(headers=self.headers) as client:  
            resp = await client.get(url)  
            resp.raise_for_status()  
            payload = resp.json()  
  
        log_json(payload, f"get_messages_chat_{chat_id}")  
        return [Message.model_validate(msg_obj) for msg_obj in payload.get("data", [])]  
  
    async def get_chat_with_messages(self, chat_id: Union[int, str], message_limit: int = 20) -> ChatThread:  
        chats = await self.get_chats(limit=100)  
        chat = next((c for c in chats if str(c.id) == str(chat_id)), None)  
        if not chat:  
            raise ValueError(f"Chat {chat_id} not found.")  
  
        chat.messages = await self.get_messages(chat_id, limit=message_limit)  
  
        # Log combined chat+messages  
        log_json(chat.model_dump(), f"get_chat_with_messages_{chat_id}")  
        return chat  