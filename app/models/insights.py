# Response models for analytics endpoints  
  
from pydantic import BaseModel  
from typing import Optional, List  
from datetime import datetime  
  
from app.models.graph import ConversationNode  # Needed for FullSyncResponse  
  
class TopicMetricsResponse(BaseModel):  
    topic: str  
    volume: int  
    percentage_of_total: float  
    trend: Optional[float] = None  # growth/drop percentage  
  
class SentimentTrendPoint(BaseModel):  
    date: datetime  
    sentiment_score: float  
  
class SentimentTrendResponse(BaseModel):  
    trend: List[SentimentTrendPoint]  
  
class ResponseTimeMetricsResponse(BaseModel):  
    average_handling_time_minutes: float  
    silence_percentage: float  
    turns: float  
  
class AnalyticsUpdate(BaseModel):  
    topics: List[TopicMetricsResponse]  
    sentiment_trend: SentimentTrendResponse  
    response_time_metrics: ResponseTimeMetricsResponse  
  
class FullSyncResponse(BaseModel):  
    """  
    Complete snapshot of all conversations, analytics, and graph data.  
    Sent once after processing a cache_update (snapshot ingestion).  
    """  
    conversations: List[ConversationNode]  
    analytics: AnalyticsUpdate  