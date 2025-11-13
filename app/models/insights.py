"""  
Response models for analytics endpoints and WS payloads.  
Consistent with the Full‑Stack Communication Specification.  
"""  
  
from __future__ import annotations  
from pydantic import BaseModel  
from datetime import datetime  
  
from app.models.graph import ExtendedConversationNode  # richer payload for WS snapshot  
  
  
class TopicMetricsResponse(BaseModel):  
    """Aggregated metrics for a specific topic."""  
    topic: str  
    volume: int  
    percentage_of_total: float  
    trend: float | None = None  # growth/drop percentage  
  
  
class SentimentTrendPoint(BaseModel):  
    """Represents a single point in a sentiment trend timeline."""  
    date: datetime  
    sentiment_score: float  
  
  
class SentimentTrendResponse(BaseModel):  
    """Time series of sentiment scores."""  
    trend: list[SentimentTrendPoint]  
  
  
class ResponseTimeMetricsResponse(BaseModel):  
    """Metrics related to response times and conversation turns."""  
    average_handling_time_minutes: float  
    silence_percentage: float  
    turns: float  
  
  
class AnalyticsUpdate(BaseModel):  
    """  
    Granular update to analytics metrics.  
    Used for both snapshot and delta WS payloads.  
    """  
    topics: list[TopicMetricsResponse]  
    sentiment_trend: SentimentTrendResponse  
    response_time_metrics: ResponseTimeMetricsResponse  
  
    # Optional per-conversation metrics for inbox sorting and unread badges  
    priorityScores: dict[str, float] | None = None  
    unreadCounts: dict[str, int] | None = None  
  
  
class FullSyncResponse(BaseModel):  
    """  
    Complete snapshot of all conversations, analytics, and graph data.  
    Sent once after processing a cache_update (snapshot ingestion).  
    """  
    conversations: list[ExtendedConversationNode]  # was ConversationNode  
    analytics: AnalyticsUpdate  