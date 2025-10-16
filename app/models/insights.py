# Response models for analytics endpoints

from pydantic import BaseModel  
from typing import Optional, List  
from datetime import datetime  
  
class TopicMetricsResponse(BaseModel):  
    topic: str  
    volume: int  
    percentage_of_total: float  
    trend: Optional[float]  # growth/drop percentage  
  
class SentimentTrendPoint(BaseModel):  
    date: datetime  
    sentiment_score: float  
  
class SentimentTrendResponse(BaseModel):  
    trend: List[SentimentTrendPoint]  
  
class ResponseTimeMetricsResponse(BaseModel):  
    average_handling_time_minutes: float  
    silence_percentage: float  
    turns: float  