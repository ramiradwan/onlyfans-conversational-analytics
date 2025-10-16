from fastapi import APIRouter, HTTPException  
from typing import List, Union  
  
from app.models.core import ChatThread, Message  
from app.models.auth import AuthData  
from app.services.onlyfans_client import OnlyFansClient  
  
router = APIRouter()  
  
  
@router.post("/chats", response_model=List[ChatThread])  
async def fetch_chats(auth: AuthData, limit: int = 20, offset: int = 0):  
    """  
    Fetch a list of chat threads from the creator's OnlyFans account.  
    """  
    try:  
        client = OnlyFansClient(auth.auth_cookie)  
        chats = await client.get_chats(limit=limit, offset=offset)  
        return chats  
    except Exception as e:  
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
    """  
    try:  
        client = OnlyFansClient(auth.auth_cookie)  
        messages = await client.get_messages(chat_id=chat_id, limit=limit, offset=offset)  
        return messages  
    except Exception as e:  
        raise HTTPException(status_code=400, detail=str(e))  
  
  
@router.post("/chats/{chat_id}/full", response_model=ChatThread)  
async def fetch_chat_with_messages(  
    chat_id: Union[int, str],  
    auth: AuthData,  
    message_limit: int = 20  
):  
    """  
    Fetch a chat thread along with its messages.  
    """  
    try:  
        client = OnlyFansClient(auth.auth_cookie)  
        chat = await client.get_chat_with_messages(chat_id=chat_id, message_limit=message_limit)  
        return chat  
    except Exception as e:  
        raise HTTPException(status_code=400, detail=str(e))  