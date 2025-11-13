"""  
Labeled Property Graph (LPG) vertex & edge Pydantic models.  
  
These models represent the nodes and edges in the conversational analytics graph,  
and are reused across REST and WebSocket payload schemas.  
  
Consistent with the Full‑Stack Communication Specification, ConversationNode includes:  
- Optional messages array (core.Message[]) for snapshot/delta modes.  
- Optional enrichment metadata (topics, actions, sentiment, outcomes)  
  so that WS payloads include all enrichment fields when available.  
  
Ensures WS payloads match:  
- "full_sync_response": all chats and messages  
- "append_message": single new or updated conversation node  
"""  
  
from __future__ import annotations  
from pydantic import BaseModel, Field  
from datetime import datetime  
  
from app.models.core import Message, UserRef  
  
  
class Fan(BaseModel):  
    """Represents a fan vertex in the LPG."""  
    fanId: str  
    joinDate: datetime | None = None  
    demographics: dict | None = Field(default_factory=dict)  
    sentimentProfile: float | None = None  
  
  
class Creator(BaseModel):  
    """Represents a creator vertex in the LPG."""  
    creatorId: str  
    niche: str | None = None  
    styleProfile: dict | None = Field(default_factory=dict)  
  
  
class Topic(BaseModel):  
    """Represents a topic vertex in the LPG."""  
    topicId: str  
    description: str  
    embedding: list[float]  
    category: str | None = None  
  
  
class EngagementAction(BaseModel):  
    """Represents an engagement action vertex in the LPG."""  
    actionId: str  
    name: str  
    embedding: list[float]  
    type: str | None = None  
  
  
class InteractionOutcome(BaseModel):  
    """Represents an interaction outcome vertex in the LPG."""  
    outcomeId: str  
    name: str  
    score: float | None = None  
    date: datetime | None = None  
  
  
class ConversationNode(BaseModel):  
    """  
    Represents a conversation in the LPG.  
  
    Snapshot mode (full_sync_response):  
        - Includes metadata and all messages for the conversation.  
    Delta mode (append_message):  
        - Includes metadata and only the new/updated messages.  
  
    Also optionally includes enrichment metadata (topics, actions, sentiment, outcomes)  
    when available from the enrichment pipeline.  
    """  
    conversationId: str  
    startDate: datetime  
    endDate: datetime | None = None  
    messageCount: int  
    averageResponseTime: float | None = None  
    turns: int | None = None  
    silencePercentage: float | None = None  
  
    # ✅ Spec compliance  
    messages: list[Message] | None = None  
  
    # ✅ Enrichment metadata  
    topics: list[Topic] | None = None  
    actions: list[EngagementAction] | None = None  
    sentiment: float | None = None  
    outcomes: list[InteractionOutcome] | None = None  
  
  
class ExtendedConversationNode(ConversationNode):  
    """  
    Extended ConversationNode including additional frontend/UI and prioritization fields.  
  
    Used in WS payloads when the frontend requires richer context,  
    such as displaying the other participant's profile or ranking conversations.  
    """  
    priorityScore: float | None = None  
    withUser: UserRef | None = None  
  
  
class GraphEdge(BaseModel):  
    """Represents an edge between two LPG vertices."""  
    from_id: str  
    to_id: str  
    label: str  
    properties: dict | None = Field(default_factory=dict)  
  
  
class EnrichmentResultPayload(BaseModel):  
    """  
    Payload for enrichment_result WS messages.  
    Contains enrichment metadata without full message history.  
    """  
    conversationId: str  # unified naming with ConversationNode  
    topics: list[Topic]  
    actions: list[EngagementAction]  
    sentiment: float  
    outcomes: list[InteractionOutcome]  