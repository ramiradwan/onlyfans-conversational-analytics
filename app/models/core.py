# Core message & conversation Pydantic models

from pydantic import BaseModel, Field  
from typing import Optional, List, Union, Dict, Any  
from datetime import datetime  
  
class MediaItem(BaseModel):  
    """Represents a media attachment in a message."""  
    id: Optional[Union[int, str]] = None  
    url: Optional[str] = None  
    type: Optional[str] = None         # "image", "video", "audio", etc.  
    mime_type: Optional[str] = None  
    width: Optional[int] = None  
    height: Optional[int] = None  
    duration: Optional[float] = None   # seconds for audio/video  
    size: Optional[int] = None         # bytes  
    thumbnail_url: Optional[str] = None  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
class Message(BaseModel):  
    """Represents a single chat message."""  
    id: Union[int, str]  
    chat_id: Optional[Union[int, str]] = None  
    sender_id: Optional[Union[int, str]] = None  
    recipient_id: Optional[Union[int, str]] = None  
    text: Optional[str] = None  
    media: Optional[List[MediaItem]] = None  
    attachments: Optional[List[Dict[str, Any]]] = None  
    created_at: Optional[datetime] = None  
    updated_at: Optional[datetime] = None  
    is_read: Optional[bool] = None  
    direction: Optional[str] = None      # "inbound"/"outbound" or similar  
    is_system: Optional[bool] = None  
    is_deleted: Optional[bool] = None  
    extra: Dict[str, Any] = Field(default_factory=dict)  
  
class ChatThread(BaseModel):  
    """Represents a conversation (thread) between the creator and a fan."""  
    id: Union[int, str]  
    participants: Optional[List[Union[int, str]]] = None  
    last_message: Optional[Message] = None  
    last_message_time: Optional[datetime] = None  
    unread_count: Optional[int] = 0  
    is_blocked: Optional[bool] = None  
    is_muted: Optional[bool] = None  
    messages: Optional[List[Message]] = None  
    extra: Dict[str, Any] = Field(default_factory=dict)  