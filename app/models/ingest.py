# app/models/ingest.py  
"""  
Payload models for snapshot (cache_update) and delta (new_raw_message) ingestion.  
These are used in IncomingWssMessage from the Agent to the Brain.  
"""  
  
from pydantic import BaseModel  
from typing import List  
from app.models.core import ChatThread, Message  
  
  
class CacheUpdatePayload(BaseModel):  
    """  
    Full snapshot of all chats and messages from the Agent's local IndexedDB.  
    Sent once upon initial WebSocket connection.  
    """  
    chats: List[ChatThread]  
    messages: List[Message]  
  
  
class NewRawMessagePayload(BaseModel):  
    """  
    Single new message/event captured by the Agent.  
    Sent for each delta event after the snapshot.  
    """  
    message: Message  