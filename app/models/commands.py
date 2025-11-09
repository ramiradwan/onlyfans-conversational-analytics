# app/models/commands.py  
"""  
Models for AI-generated commands sent from the Brain to the Agent.  
"""  
  
from pydantic import BaseModel, Field  
from typing import Optional  
  
  
class SendMessageCommand(BaseModel):  
    """  
    Represents a command instructing the Agent to send a message to a fan.  
    """  
    chat_id: str = Field(..., description="The target chat thread ID")  
    text: str = Field(..., description="The message text to send")  
    media_url: Optional[str] = Field(None, description="Optional media attachment URL")  