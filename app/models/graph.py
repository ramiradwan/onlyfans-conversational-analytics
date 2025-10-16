# LPG vertex & edge Pydantic models

from pydantic import BaseModel  
from typing import Optional, List  
from datetime import datetime  
  
class Fan(BaseModel):  
    fanId: str  
    joinDate: Optional[datetime]  
    demographics: Optional[dict] = {}  
    sentimentProfile: Optional[float]  
  
class Creator(BaseModel):  
    creatorId: str  
    niche: Optional[str]  
    styleProfile: Optional[dict] = {}  
  
class ConversationNode(BaseModel):  
    conversationId: str  
    startDate: datetime  
    endDate: Optional[datetime]  
    messageCount: int  
    averageResponseTime: Optional[float]  
    turns: Optional[int]  
    silencePercentage: Optional[float]  
  
class Topic(BaseModel):  
    topicId: str  
    description: str  
    embedding: List[float]  
    category: Optional[str]  
  
class EngagementAction(BaseModel):  
    actionId: str  
    name: str  
    embedding: List[float]  
    type: Optional[str]  
  
class InteractionOutcome(BaseModel):  
    outcomeId: str  
    name: str  
    score: Optional[float]  
    date: Optional[datetime]  
  
class GraphEdge(BaseModel):  
    from_id: str  
    to_id: str  
    label: str  
    properties: Optional[dict] = {}  