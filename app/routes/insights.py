from fastapi import APIRouter, Query, HTTPException  
from typing import Optional, List  
from datetime import datetime  
  
from app.models.graph import Topic  
from app.models.insights import (  
    TopicMetricsResponse,  
    SentimentTrendResponse,  
    ResponseTimeMetricsResponse  
)  
from app.services import insights_service  
  
router = APIRouter()  
  
@router.get("/topics", response_model=List[TopicMetricsResponse])  
async def get_topic_metrics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None)  
):  
    """  
    Get volume, % of total, and trend for each topic over the given date range.  
    """  
    try:  
        metrics = await insights_service.fetch_topic_metrics(start_date, end_date)  
        return metrics  
    except Exception as e:  
        raise HTTPException(status_code=500, detail=str(e))  
  
  
@router.get("/sentiment-trend", response_model=SentimentTrendResponse)  
async def get_sentiment_trend(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None)  
):  
    """  
    Get average sentiment score trend over time.  
    """  
    try:  
        trend = await insights_service.fetch_sentiment_trend(start_date, end_date)  
        return trend  
    except Exception as e:  
        raise HTTPException(status_code=500, detail=str(e))  
  
  
@router.get("/response-time", response_model=ResponseTimeMetricsResponse)  
async def get_response_time_metrics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None)  
):  
    """  
    Get average handling time (AHT), % silence, and turns metrics over the given date range.  
    """  
    try:  
        metrics = await insights_service.fetch_response_time_metrics(start_date, end_date)  
        return metrics  
    except Exception as e:  
        raise HTTPException(status_code=500, detail=str(e))  