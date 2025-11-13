"""  
Payload models for snapshot (cache_update) and delta (new_raw_message) ingestion.  
Used in IncomingWssMessage from the Agent to the Brain.  
"""  
  
from __future__ import annotations  
from pydantic import BaseModel, Field  
from app.models.core import ChatThread, Message  
  
  
class CacheUpdatePayload(BaseModel):  
    """  
    Full snapshot of all chats and messages from the Agent's local IndexedDB.  
    Sent once upon initial WebSocket connection.  
  
    WS type: "cache_update"  
    """  
    chats: list[ChatThread] = Field(  
        ...,  
        description="Complete list of chat threads from local IndexedDB",  
        example=[{"id": "chat123", "withUser": {"id": "fan456"}}]  
    )  
    messages: list[Message] = Field(  
        ...,  
        description="Complete list of messages from local IndexedDB",  
        example=[{"id": "msg789", "chat_id": "chat123", "text": "Hello!"}]  
    )  
  
  
class NewRawMessagePayload(BaseModel):  
    """  
    Single new message/event captured by the Agent.  
    Sent for each delta event after the snapshot.  
  
    WS type: "new_raw_message"  
    """  
    message: Message = Field(  
        ...,  
        description="The new message/event",  
        example={"id": "msg790", "chat_id": "chat123", "text": "New message text"}  
    )  