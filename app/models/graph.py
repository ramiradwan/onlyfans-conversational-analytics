# LPG vertex & edge Pydantic models

from pydantic import BaseModel, Field  
from typing import Optional, List  
from datetime import datetime  
  
class Fan(BaseModel):  
    fanId: str  
    joinDate: Optional[datetime] = None  
    demographics: Optional[dict] = Field(default_factory=dict)  
    sentimentProfile: Optional[float] = None  
  
class Creator(BaseModel):  
    creatorId: str  
    niche: Optional[str] = None  
    styleProfile: Optional[dict] = Field(default_factory=dict)  
  
class ConversationNode(BaseModel):  
    conversationId: str  
    startDate: datetime  
    endDate: Optional[datetime] = None  
    messageCount: int  
    averageResponseTime: Optional[float] = None  
    turns: Optional[int] = None  
    silencePercentage: Optional[float] = None  
  
class Topic(BaseModel):  
    topicId: str  
    description: str  
    embedding: List[float]  
    category: Optional[str] = None  
  
class EngagementAction(BaseModel):  
    actionId: str  
    name: str  
    embedding: List[float]  
    type: Optional[str] = None  
  
class InteractionOutcome(BaseModel):  
    outcomeId: str  
    name: str  
    score: Optional[float] = None  
    date: Optional[datetime] = None  
  
class GraphEdge(BaseModel):  
    from_id: str  
    to_id: str  
    label: str  
    properties: Optional[dict] = Field(default_factory=dict)  