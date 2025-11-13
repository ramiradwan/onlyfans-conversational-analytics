"""  
Models for AI-generated commands sent from the Brain to the Agent.  
"""  
  
from __future__ import annotations  
from pydantic import BaseModel, Field  
  
  
class SendMessageCommand(BaseModel):  
    """  
    Represents a command instructing the Agent to send a message to a fan.  
    """  
    chat_id: int | str = Field(  
        ...,  
        description="The target chat thread ID",  
        example="123456789"  
    )  
    text: str = Field(  
        ...,  
        description="The message text to send",  
        example="Hey, thanks for subscribing! 😊"  
    )  
    media_url: str | None = Field(  
        None,  
        description="Optional media attachment URL",  
        example="https://cdn.onlyfans.com/path/to/image.jpg"  
    )  